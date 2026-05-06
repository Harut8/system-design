# 22 — Service Mesh Observability

> Service meshes (Istio, Linkerd, Cilium, Kuma, Consul Connect) are the L7 data plane that sits between every service-to-service call. They generate vast amounts of *zero-instrumentation* telemetry — RED metrics per edge, traces for every hop, logs for every request — without a single line of application code change. The promise is "observability for free." The reality is more complex: the mesh observes one specific layer well, and obscures others.

This chapter is about the observability story of the service mesh — what it gives you, what it doesn't, who owns what, and the integration patterns that combine mesh telemetry with application telemetry into one coherent picture.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [What a service mesh is, briefly](#2-what-mesh-is)
3. [The mesh's three signals](#3-three-signals)
4. [Sidecar vs eBPF / kernel-mode](#4-sidecar-vs-ebpf)
5. [The Istio observability stack](#5-istio)
6. [The Linkerd observability stack](#6-linkerd)
7. [The Cilium / eBPF observability stack](#7-cilium)
8. [Mesh metrics vs application metrics: who owns what](#8-ownership)
9. [Tracing in a mesh](#9-tracing)
10. [Access logs](#10-access-logs)
11. [The service graph (auto-derived topology)](#11-service-graph)
12. [Multi-cluster mesh observability](#12-multi-cluster)
13. [Mesh control plane observability (the often-forgotten)](#13-control-plane)
14. [Anti-patterns](#14-anti-patterns)
15. [Worked example: Istio + OTel + Tempo](#15-worked-example)
16. [Pitfalls](#16-pitfalls)
17. [Mental models](#17-mental-models)

---

## 1. Thesis

Three claims:

1. **The mesh observes one layer well: HTTP/gRPC traffic between services.** It does not observe what happens *inside* the service (business logic, DB calls in some cases, async work). It observes the *edges* of the service graph, not the nodes.
2. **Mesh telemetry is *complementary* to application telemetry, not a replacement.** A service still needs RED dashboards for its internals; the mesh gives you RED for its external edges. Different views of the same system.
3. **Mesh observability has its own cost dynamics.** Sidecars double pod count and per-call overhead. eBPF-based meshes (Cilium) are cheaper but harder to operate. Pick consciously.

If your platform team thinks "we have Istio, so we have observability," you'll discover at the next incident that you only have *half* the observability — and the half you have is the half that's easiest to add a posteriori, not the half that hurts most.

---

## 2. What a Service Mesh Is, Briefly

A service mesh inserts a *proxy* between every service-to-service call. The proxy handles:
- mTLS (encryption + identity).
- Load balancing.
- Retries and circuit breakers.
- Traffic shifting (canaries, A/B).
- Authorization policies.
- **Observability** — emits metrics, traces, logs for every call.

Two architectures:

| Pattern | Description | Examples |
|---|---|---|
| **Sidecar** | A proxy container (Envoy, linkerd2-proxy) injected into every pod | Istio, Linkerd, Consul Connect |
| **Node-level / kernel** | A single agent per node intercepting at the kernel | Cilium, Kuma (some configurations) |

The choice is operationally significant; the observability output is broadly similar.

---

## 3. The Mesh's Three Signals

What you get for free.

### 3.1 RED metrics per service edge

For every (source, destination) pair, the mesh emits:

```
istio_requests_total{
  source_app, destination_app,
  source_workload, destination_workload,
  request_protocol, response_code,
  ...
}

istio_request_duration_milliseconds_bucket{
  source_app, destination_app, le, ...
}
```

Per-edge rate, errors, duration. *For free.* For every service-to-service relationship in your mesh.

This is the most universally valuable mesh feature. Service teams that didn't instrument get RED metrics anyway.

### 3.2 Distributed traces

The proxy starts a span for every request it routes. With proper context propagation (the application must forward `traceparent` in many meshes — the proxy doesn't do this for you in HTTP middleware), the proxy spans show up in the trace at every hop.

Limitations:
- The proxy span is *coarse-grained* — one span per HTTP request, not per database call inside the request.
- The proxy doesn't see the request body content (encrypted payloads).
- Async work (queues, background jobs) isn't traced.

### 3.3 Access logs

Per-request structured logs, equivalent to nginx access logs but for mesh traffic. Useful for debugging specific requests, but volume is large; sampling required.

```
{"start_time":"...","method":"POST","path":"/checkout",
 "protocol":"HTTP/2","response_code":200,"bytes_received":234,
 "duration":120,"upstream_service_time":98,"upstream_host":"...",
 "request_id":"...","x_b3_traceid":"..."}
```

### 3.4 What it *doesn't* give

- **Application-internal latency.** The proxy sees the call boundary, not internal compute or DB time.
- **Application errors that aren't surfaced as HTTP errors.** A 200-with-error-body looks success.
- **Async work.** Background jobs, queues, scheduled tasks.
- **DB / cache calls** unless your DB sits behind the mesh (rare; mTLS + DBs is operationally hairy).
- **Out-of-mesh traffic.** Calls to vendors, SaaS APIs, anything outside the cluster.

The mesh observability boundary is *the cluster's L7 surface*. Useful, partial.

---

## 4. Sidecar vs eBPF / Kernel-Mode

The architecture choice with observability implications.

### 4.1 Sidecar (Istio default, Linkerd default)

```
[Pod]
  ┌───────────┐    ┌──────────┐
  │ App       │ ↔ │ Sidecar  │ ↔ network ↔ Sidecar ↔ App
  │ Container │    │ (Envoy)  │
  └───────────┘    └──────────┘
```

**Observability properties:**
- Excellent metrics, traces, logs at the edge.
- Pure userspace; portable.
- Per-pod resource overhead (~50-200 MB memory, 1-5% CPU per pod).
- Latency added: ~0.5-2ms per hop.

### 4.2 eBPF / kernel-mode (Cilium, Kuma in some modes)

```
[Pod]
  ┌───────────┐
  │ App       │ ─── kernel-level eBPF program ─── network
  │ Container │
  └───────────┘
```

**Observability properties:**
- Lower overhead (no userspace proxy).
- Observes everything *at the kernel* — including non-HTTP traffic.
- More complex to operate and reason about.
- Requires modern kernels (5.x+).
- Less mature L7 visibility (HTTP parsing in eBPF is still evolving).

### 4.3 The 2026 trend

Cilium (and the broader eBPF-mesh story) is gaining ground. Sidecar-based Istio remains the production standard but has added an "ambient mesh" mode (Istio 1.18+, 2023) that uses a node-level proxy instead of per-pod sidecars — closing the cost gap.

The decision matrix:

| Need | Pick |
|---|---|
| Standard, well-supported | Istio (sidecar) |
| Lowest overhead, modern kernel | Cilium / Istio ambient |
| Simplest, lowest operational cost | Linkerd |
| Multi-cluster, multi-cloud | Kuma, Istio (with Istio's multi-cluster setup) |
| L4-only is fine | Cilium kproxy |

---

## 5. The Istio Observability Stack

The most-deployed mesh in 2026.

### 5.1 The signals

Istio Envoy sidecars emit:
- Prometheus-compatible metrics (`istio_requests_total`, `istio_request_duration_milliseconds`, etc.).
- OTLP traces (with proper tracer config).
- Access logs to stdout (parseable by Fluent Bit).
- TCP-level metrics (`istio_tcp_*` for non-HTTP traffic).

### 5.2 The telemetry config

Istio 1.5+ uses the `Telemetry` CRD to configure observability per workload:

```yaml
apiVersion: telemetry.istio.io/v1alpha1
kind: Telemetry
metadata:
  name: checkout-tracing
spec:
  selector:
    matchLabels:
      app: checkout
  tracing:
    - providers:
        - name: otel-collector
      randomSamplingPercentage: 100  # 100% — tail-sample at collector
  metrics:
    - providers:
        - name: prometheus
      overrides:
        - tagOverrides:
            response_code:
              value: response.code
        - disabled: false
  accessLogging:
    - providers:
        - name: otel
      filter:
        expression: "response.code >= 400"  # only log errors
```

This config decouples telemetry from app code and from the proxy implementation.

### 5.3 The "Kiali" service graph

Kiali is Istio's official service-graph dashboard — auto-derives the topology from mesh metrics, shows real-time RED data per edge, traffic shifts, mTLS health, etc. Not a replacement for general observability, but the canonical mesh-specific dashboard.

### 5.4 The gotchas

- **Tracing requires app cooperation.** The Envoy proxy creates a span, but the app must propagate `traceparent` on outgoing calls. Without this, the trace splits into disconnected fragments per hop.
- **Sampling decisions vary.** Istio's sampling, OTel's sampling, app-level sampling — all interact. Tail-sample at the collector to recover.
- **Cardinality.** Default Istio metrics include `source_workload` × `destination_workload` × `response_code` × `request_protocol` — high cardinality. Drop unused labels at the collector.

---

## 6. The Linkerd Observability Stack

Simpler than Istio; widely admired for operational clarity.

### 6.1 The signals

Linkerd emits Prometheus metrics natively (its data plane is a Rust proxy, not Envoy). Metric names differ from Istio (`request_total`, `response_latency_ms_bucket`).

### 6.2 The "Tap" feature

Linkerd's `tap` lets you observe live traffic per service — equivalent to a `tcpdump` for HTTP. Useful for debugging; not for steady-state observability.

### 6.3 The "Linkerd Viz" dashboard

Linkerd's bundled Grafana + Prometheus + dashboards stack. Great out-of-the-box; opinionated about retention and storage (small Prometheus, short retention). For production-scale, integrate with your central observability.

### 6.4 The trade-offs

Linkerd has a smaller feature surface than Istio — fewer policy primitives, simpler traffic management. The observability story is correspondingly tighter and less configurable. For most teams, that's a feature.

---

## 7. The Cilium / eBPF Observability Stack

The kernel-mode story.

### 7.1 Hubble

Cilium's observability layer. Emits:
- Flow logs at L3-L7 (every packet → flow → request).
- Metrics derived from flows.
- Traces (limited; HTTP-only).
- Service map.

### 7.2 The advantages

- **L3-L7 visibility.** See not just HTTP but also DNS, TCP-level health, MTU drops.
- **Lower overhead.** No proxy hop.
- **Captures non-mesh traffic.** Anything on the node, not just mesh-routed.

### 7.3 The challenges

- **Less mature L7 parsing.** HTTP/gRPC parsing in eBPF is improving but lags Envoy.
- **Node-level state.** Per-node Hubble agent must be tuned for memory.
- **Different mental model.** Operators familiar with sidecars need re-training.

### 7.4 Hubble + standard stack

Hubble integrates with Prometheus for metrics, OTel for traces, and Loki for flow logs. The integration story has matured through 2025-2026; treats Cilium-derived telemetry as another OTLP source.

---

## 8. Mesh Metrics vs Application Metrics: Who Owns What

The single most important conceptual line.

### 8.1 The boundary

```
        ┌──────────────────────────────────────────────────────┐
        │                  POD                                  │
        │   ┌──────────────────────┐   ┌──────────────────┐    │
        │   │  Application         │   │   Sidecar (proxy)│    │
        │   │   - Business metrics │   │   - Edge RED     │    │
        │   │   - DB calls         │   │   - mTLS metrics │    │
        │   │   - Custom labels    │   │   - L4/L7 traffic│    │
        │   │   - Internal latency │   │                  │    │
        │   └──────────────────────┘   └──────────────────┘    │
        │      ↑                            ↑                   │
        │      │                            │                   │
        │      │                            │                   │
        │   App metrics                Mesh metrics             │
        │   (high-value, low-volume)   (universal, voluminous)  │
        └──────────────────────────────────────────────────────┘
```

**App metrics own:**
- Business-level KPIs (orders, revenue, signups).
- DB query timing and outcome.
- Cache hit/miss rates.
- Custom dimensions (customer_tier, feature_flag, A/B variant).
- Internal queue depths and worker pool stats.
- Errors not visible in HTTP responses (silent corruption, partial failures).

**Mesh metrics own:**
- Per-edge RED (request rate, error rate, latency).
- mTLS / authorization outcomes.
- Connection-level health.
- Topology / service graph.

Both are needed. Don't double-instrument — pick the right layer for each metric.

### 8.2 The "duplicate metric" anti-pattern

Common mistake: an app emits `http_requests_total` *and* the mesh emits `istio_requests_total` for the same calls. Now you have two metrics measuring the same thing with different label sets.

Resolution:
- Use mesh metrics for *edge* RED (the standard view).
- Use app metrics for *internal* RED (e.g., per-handler within the service that the mesh can't see).
- Document which one is the canonical SLI source.

### 8.3 The SLO-source decision

For SLO-defining SLIs (`doc 13`), pick *one* source per SLI. Mixing app and mesh data for the same SLI causes drift.

Default: **mesh metrics for cross-service edge SLIs; app metrics for service-internal SLIs.**

---

## 9. Tracing in a Mesh

The cleanest part of mesh observability — when it works.

### 9.1 The flow

```
Request enters cluster (gateway)
  ↓
Gateway proxy creates root span (or extracts traceparent if present)
  ↓
Forwards to destination pod
  ↓
Destination's sidecar receives, creates server span
  ↓
App receives request (traceparent in headers)
  ↓
App may create internal spans (DB calls, etc.)
  ↓
App calls another service (must include traceparent)
  ↓
Outbound sidecar creates client span; forwards
  ↓
... and so on
```

### 9.2 The "context propagation" requirement

The mesh creates spans on its own, but the *application must propagate the traceparent header* from incoming to outgoing requests. Without this, every hop creates a *new* trace, disconnected from the previous.

Languages with auto-instrumentation (OTel SDK) handle this automatically. Languages without need explicit forwarding.

```python
# Bad: no propagation
def handler(request):
    response = requests.post("downstream-svc/...")  # new trace, not linked
    
# Good: propagation
def handler(request):
    headers = {"traceparent": request.headers.get("traceparent")}
    response = requests.post("downstream-svc/...", headers=headers)
```

OTel SDK with auto-instrumentation makes this implicit. Without it, every team must re-implement propagation, and they will get it wrong.

### 9.3 Span sampling

Mesh proxies head-sample at a configured rate (often 100% by default — cost trap). Tail-sample at the OTel collector to keep what matters (errors, slow, rare paths).

Don't run the mesh at 1% head sampling and the app at 10% — you'll get inconsistent traces. Either sample at the collector (recommended) or align rates.

### 9.4 The async problem

Background jobs, queue consumers, async retries — none of these flow through the mesh. They need application-level tracing, with `traceparent` propagated via message headers.

Most teams underinstrument async work. The mesh doesn't help here; app instrumentation is the only path.

---

## 10. Access Logs

The mesh access log is high-volume per-request data.

### 10.1 What's in them

Per-request: timestamp, method, path, status, duration, source/dest IPs, request size, response size, traceparent, custom Envoy filters' outputs.

### 10.2 The volume

A high-traffic mesh emits *terabytes* of access logs per day. Cost is real:
- 100 services × 1000 RPS × 500 bytes/log × 86400 sec = ~5 TB/day.

### 10.3 Sampling

Default-sample aggressively:
- 100% of `4xx` and `5xx` (errors are rare and rich).
- 1-5% of `2xx` (the bulk).
- 100% of slow requests (>p99).
- Use a Loki / ClickHouse-on-logs architecture for cost.

### 10.4 The "do I need them?" question

Many teams ship 100% of access logs and never query them. Consider whether traces (with the same data, structured) are sufficient.

A reasonable position: traces for forward debugging; access logs for compliance and forensic investigation; aggressive sampling on both.

---

## 11. The Service Graph

The mesh's killer dashboard feature.

### 11.1 What it is

A graph derived from RED metrics: nodes are services, edges are call relationships, edge weights are RPS / error rate / latency.

```
┌──────────┐      ┌──────────┐      ┌──────────┐
│ frontend │ ─→  │ checkout │ ─→  │ payments │
│          │      │          │      │  (5xx ↑) │ ← red on edge
└──────────┘      └────┬─────┘      └──────────┘
                        ↓
                  ┌──────────┐
                  │   auth   │
                  └──────────┘
```

### 11.2 Why it's useful

- **Topology discovery.** New engineers see the system at a glance.
- **Anomaly localization.** A red edge points to where the problem is.
- **Capacity dependencies.** Fan-out factor visible.
- **Dependency drift.** New unexpected edges flag architecture creep.

### 11.3 Implementations

- **Kiali** (Istio).
- **Linkerd Viz**.
- **Hubble UI** (Cilium).
- **Tempo's auto service graph** (derived from spanmetrics).
- **Datadog Service Map** (proprietary).
- **Honeycomb's BubbleUp** (different but similar use case).

### 11.4 The "outside the mesh" gap

Services and dependencies *outside* the mesh don't appear: managed databases (RDS, Cloud SQL), external SaaS APIs, in-cluster services without sidecars. The graph is partial.

Some tools (Tempo, Datadog) augment the mesh-derived graph with trace-derived data, filling in external calls. Worth configuring.

---

## 12. Multi-Cluster Mesh Observability

Most production deployments span multiple clusters (regions, environments, isolation domains).

### 12.1 The architectures

| Pattern | Description |
|---|---|
| **Multi-cluster mesh (one mesh)** | One mesh spans multiple clusters; cross-cluster service discovery |
| **Federated meshes** | Each cluster has its own mesh; federated at boundary |
| **Independent meshes per cluster** | No federation; cross-cluster calls via standard load balancers |

The observability story differs per pattern.

### 12.2 Single-mesh, multi-cluster

The mesh treats both clusters as one logical fabric. Service graph spans clusters. Telemetry flows to a central observability stack.

Pros: unified view; easy cross-cluster traces.
Cons: control plane complexity; failure-domain coupling.

### 12.3 Federated meshes

Each cluster's mesh emits to its cluster's observability; a federation layer aggregates (e.g., Thanos / Mimir / Grafana global view).

Pros: failure-domain isolation; independent scaling.
Cons: cross-cluster traces need careful traceparent forwarding at federation boundaries.

### 12.4 The "cluster" label

Crucial for multi-cluster: every metric, log, span carries `cluster=us-east-1` or similar. Without it, cross-cluster anomalies are invisible.

---

## 13. Mesh Control Plane Observability

The often-forgotten layer.

### 13.1 What the control plane is

The brain of the mesh: Istio's `istiod`, Linkerd's controller, Cilium's operator. Distributes configuration to data-plane proxies, manages mTLS certs, evaluates policy.

### 13.2 What can go wrong

- **Slow config push:** policy changes take minutes to propagate; debugging is hard.
- **Cert expiry:** mTLS breaks if certs aren't rotated.
- **Memory pressure:** large meshes (10k+ workloads) saturate the control plane's memory.
- **Webhook failures:** sidecar injection fails on new pods.

### 13.3 The control-plane SLOs

The platform team owns these:
- **Config-push p99:** time from CRD apply to all proxies updated. Target: ≤ 10s.
- **Sidecar injection success rate:** target 99.9%.
- **mTLS cert rotation:** target 100% (any failure = security gap).
- **Control plane availability:** 99.9%+.

Without these, the mesh degrades silently. Pages on data-plane symptoms; root cause in control plane.

### 13.4 The mesh-itself observability

The mesh observes everything; the platform team must observe *the mesh*. Include in the platform-team's own SLOs:
- Sidecar memory regressions (do new sidecar versions OOM?).
- Telemetry pipeline lag (collector backlog, dropped spans).
- Per-proxy ingest rate vs capacity.

---

## 14. Anti-Patterns

1. **Treating mesh telemetry as complete.** It only sees edge L7.
2. **Duplicate metrics (app + mesh).** Drift; SLO source ambiguous.
3. **No traceparent propagation in app.** Trace breaks per hop.
4. **100% mesh access logs.** Storage explosion.
5. **No mesh control-plane SLOs.** Control-plane failures invisible.
6. **No `cluster` label.** Multi-cluster anomalies merged.
7. **Sampling inconsistency between mesh and app.** Bizarre traces.
8. **No external-call tracing.** Out-of-mesh dependencies invisible.
9. **No integration with central observability.** Mesh dashboards isolated.
10. **Ignoring cardinality on mesh metrics.** `source_workload × destination_workload × code` explodes.
11. **Async work uninstrumented.** Background jobs invisible.
12. **No mesh-version regression testing.** Sidecar upgrade breaks observability.
13. **Mesh cert rotation untested.** mTLS outage at expiry.
14. **No service-graph review.** Dependency drift unmonitored.
15. **Mesh seen as "the observability solution."** Application observability deprioritized.

---

## 15. Worked Example: Istio + OTel + Tempo

Concrete integration.

### 15.1 The architecture

- Istio mesh on EKS (3 clusters, multi-region).
- OTel Collector deployed as a DaemonSet (agent) and Deployment (gateway).
- Mimir (metrics) + Loki (logs) + Tempo (traces).
- Kiali for mesh-specific topology.

### 15.2 The config

`Telemetry` CRD configured:
- Tracing: 100% rate; tail-sample at gateway collector.
- Metrics: standard set; cardinality reduced by dropping `request_protocol`, `connection_security_policy`.
- Access logs: 5xx-only at 100%; 2xx at 1%.

OTel collector receives:
- Mesh telemetry from Envoy via OTLP.
- Application telemetry from app SDKs.

Tail sampling policy: keep errors, p99-slow, 1% baseline.

### 15.3 The trace topology

A `/checkout` request:
1. Browser → ingress gateway (root span; from RUM).
2. Gateway sidecar → checkout sidecar (mesh client span).
3. Checkout sidecar → checkout app (mesh server span).
4. Checkout app → DB (app span).
5. Checkout app → payments service (mesh client span via outbound sidecar).
6. ... (continues)

The mesh contributes the edge spans; the app contributes the internal spans (DB call, business logic). End-to-end traceparent propagation is required.

### 15.4 The dashboards

- Kiali: real-time mesh topology, RED per edge.
- Grafana: aggregate dashboards from Mimir, exemplar links to Tempo.
- Tempo: trace search and visualization.
- Loki: log search; access logs filtered by `trace_id`.

### 15.5 The result

Engineers debugging a slow checkout can:
1. See the slow trace in Tempo (exemplar from the burn-rate alert).
2. Identify which span is slow (mesh edge or app internal?).
3. Jump to logs filtered by trace_id.
4. See the service-graph context in Kiali.

The mesh enables this *without each service team writing tracing code* — but only because the platform team enforced traceparent propagation.

---

## 16. Pitfalls

1. **No traceparent propagation.** Every trace breaks at every hop.
2. **Duplicate metrics.** Drift between app and mesh sources.
3. **Cardinality blowup on mesh metrics.** Default labels are too rich.
4. **No control-plane SLOs.** Slow config push, cert expiry invisible.
5. **No tail sampling.** Trace volume explodes.
6. **No `cluster` label.** Multi-cluster confusion.
7. **External calls untraced.** Half the dependency map invisible.
8. **No async tracing.** Background jobs unobserved.
9. **Mesh upgrades break instrumentation.** No regression test.
10. **Mesh seen as complete.** Application observability rotted.
11. **No service-graph review.** Architecture creep.
12. **Access logs at 100%.** Bill explodes.
13. **mTLS cert rotation untested.** Outage at expiry.
14. **Single mesh-team owns it all.** Bottleneck for service teams.
15. **Sidecar resource limits too tight.** OOMs during traffic spikes.

---

## 17. Mental Models

> **Mesh telemetry observes edges, not interiors. Application telemetry observes interiors. Both are necessary.**

> **Traceparent propagation in the app is non-negotiable. Otherwise the mesh emits beautiful disconnected fragments.**

> **The service graph is the mesh's killer feature. Use it; review it.**

> **Mesh access logs are voluminous. Sample aggressively or skip them in favor of traces.**

> **The control plane has SLOs too. Slow config push is a real outage class.**

> **Cardinality control applies to mesh metrics like any other. Audit the default labels.**

> **Multi-cluster requires a `cluster` label everywhere.**

> **eBPF mesh observability is rising; sidecar mesh is still the default.**

> **Don't double-instrument. Pick the right layer per metric.**

> **The mesh isn't "the observability solution." It's one signal source.**

Now go to `doc 23` (database observability) — the next layer down, where the mesh's "downstream call took 800ms" becomes "this query plan is wrong."
