# 02 — OpenTelemetry Deep Dive

> The universal substrate. What OTel actually *is* under the hood — five separable pieces (API, SDK, OTLP, Collector, Semantic Conventions), how a trace context survives a process boundary, why your sampling decision must be encoded in 8 bits of the trace flags, and the production patterns that take a "we use OTel" claim from a getting-started copy-paste to a fleet-wide pipeline you'd hand to a Staff Engineer.

This is chapter 02. The roadmap (`ROADMAP.md`) wires every layer of an SRE stack together; this doc zooms into the layer marked "instrumentation + collector" because *every* signal in modern observability — metrics, logs, traces, and now profiles — eventually flows through OTel-shaped wire formats. The deeper chapters (`doc 03` instrumentation idioms, `doc 04` collector ops at scale, `doc 06`/`07`/`08` storage internals) all assume this one.

If you can't sketch the difference between OTel API and SDK on a whiteboard, you'll write a brittle integration. Read on.

---

## 1. Why OTel exists at all

Before OpenTelemetry, every observability vendor shipped its own SDK. Datadog's tracer was not New Relic's tracer was not Lightstep's tracer was not Jaeger's tracer. Replacing your APM vendor required re-instrumenting *every service* — months of work blocking a strategic decision.

The community tried twice to fix this:

| Year | Effort | Outcome |
|------|--------|---------|
| 2016 | **OpenTracing** (Sourcegraph, Lightstep, Uber) | Vendor-neutral *tracing API only*. Every vendor wrote a "tracer" that implemented it. Adoption: ~50% of new projects, but no story for metrics/logs. |
| 2017 | **OpenCensus** (Google) | Tracing *plus* metrics, single library, batteries-included exporters. Adoption: heavy inside Google + Microsoft, lighter outside. |
| 2019 | **The merge → OpenTelemetry** | OpenTracing + OpenCensus combined under CNCF. The OpenTracing API surface won; OpenCensus's "single SDK for everything" model won. Both prior projects were sunset by 2022. |

By 2026 OTel ships SDKs in **11+ languages** (Go, Java, Python, Node.js, .NET, Ruby, Rust, C++, PHP, Swift, Erlang/Elixir), a **wire protocol** (OTLP), a **vendor-neutral collector**, and **semantic conventions** that standardize attribute names. It is the second-largest CNCF project by contributor count, behind Kubernetes.

The architectural thesis is **data portability**: instrument *once*, swap backends *forever*. The same `tracer.Start()` call that flushes to Jaeger today flushes to Tempo, Honeycomb, Datadog, or ClickHouse tomorrow with a YAML change at the collector — no code redeploy, no re-instrumentation.

```
                  PRE-OTEL                                 OTEL
                  ────────                                 ────
   Service ──┬─ Datadog SDK ─→ Datadog                Service ─ OTel SDK ─┐
             ├─ Jaeger SDK ──→ Jaeger                                    │
             ├─ Prom client ─→ Prometheus                              OTLP
             └─ vendor SDK  ─→ vendor X                                  │
                                                                         ▼
   Re-instrument every service                            ┌── OTel Collector ──┐
   to swap any vendor.                                    │  receivers/        │
                                                          │  processors/       │
                                                          │  exporters         │
                                                          └─────────┬──────────┘
                                                                    │
                                       ┌──────────────┬─────────────┼────────────┐
                                       ▼              ▼             ▼            ▼
                                    Datadog        Jaeger        Tempo        Honeycomb
                                    (or any combination simultaneously)
```

> **Mental model:** OTel is to observability what SQL was to data warehouses — a *standard between two layers* that makes the layers swappable. Everything else (which TSDB, which trace store, which dashboard) is a downstream choice.

Beyond portability, OTel solves three subtler problems:

1. **Cross-signal correlation by construction.** A `trace_id` minted by the tracer SDK is automatically embedded in metrics (as exemplars) and logs (as a record attribute). One `trace_id` → all three signals, in any backend that supports the linkage.
2. **Polyglot semantics.** A Java service emitting `http.server.duration` and a Go service emitting `http.server.duration` use the *same* attribute names because both follow OTel semantic conventions. PromQL queries cross language boundaries.
3. **Out-of-band enrichment.** The collector can attach `k8s.pod.name`, `k8s.deployment.name`, `cloud.region` *after* leaving the SDK. The application doesn't need to know it's running on Kubernetes.

---

## 2. The OTel architecture: five separable pieces

The single most useful thing to internalize about OTel is that it is not one thing — it is five things, each with its own version, release cycle, and stability level.

```
┌─────────────────────────────────────────────────────────────────────┐
│                     YOUR APPLICATION CODE                           │
│                                                                     │
│   import "go.opentelemetry.io/otel"     ← depends only on the API   │
│   tracer := otel.Tracer("checkout")                                 │
│   ctx, span := tracer.Start(ctx, "POST /checkout")                  │
└──────────────────────────┬──────────────────────────────────────────┘
                           │  API call
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  (1) API   — interface only, no behavior                           │
│              go.opentelemetry.io/otel                              │
│              io.opentelemetry.api                                  │
│              opentelemetry.trace, opentelemetry.metrics            │
│                                                                     │
│  Stable. Libraries (HTTP clients, DB drivers, message queues)      │
│  depend ONLY on this package. They do not pin an SDK.              │
└──────────────────────────┬──────────────────────────────────────────┘
                           │  registered SDK provider
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  (2) SDK   — the implementation                                    │
│              go.opentelemetry.io/otel/sdk                          │
│              io.opentelemetry.sdk                                  │
│                                                                     │
│  - Span lifecycle: start, end, attribute set                       │
│  - Sampler interface (head sampling)                               │
│  - BatchSpanProcessor / PeriodicReader                             │
│  - Metric aggregators (Sum, LastValue, Histogram)                  │
│  - Resource detection                                              │
│  - Exporter abstraction                                            │
└──────────────────────────┬──────────────────────────────────────────┘
                           │  serialize via exporter
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  (3) OTLP  — the wire protocol                                     │
│              opentelemetry-proto                                   │
│                                                                     │
│  - protobuf schema for ResourceSpans, ResourceMetrics, ResourceLogs│
│  - gRPC and HTTP/protobuf flavors                                  │
│  - Stable since v1.0.0 (2023); v1.6 adds profiles (experimental)   │
└──────────────────────────┬──────────────────────────────────────────┘
                           │  network: gRPC :4317 or HTTP :4318
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  (4) Collector — vendor-neutral pipeline                           │
│              github.com/open-telemetry/opentelemetry-collector     │
│                                                                     │
│  Receivers ──→ Processors ──→ Exporters                            │
│  (OTLP,       (batch,         (OTLP,                                │
│   Prom,        memory_limiter, prom_remote_write,                  │
│   Jaeger,      tail_sampling,  loki, kafka,                        │
│   Filelog)     redaction,      datadog, ...)                       │
│                attributes)                                          │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  (5) Semantic Conventions — the schema                             │
│              opentelemetry-semantic-conventions                    │
│                                                                     │
│  - Attribute names: http.request.method, db.system, k8s.pod.name   │
│  - Span name conventions: "POST /api/{id}"                         │
│  - Resource attributes: service.name, deployment.environment       │
│  - schema_url for versioning                                       │
└─────────────────────────────────────────────────────────────────────┘
```

The **API/SDK split** is the load-bearing design choice. It is the same trick the JVM uses with SLF4J vs Logback, or that Python uses with `logging` (interface) vs handlers (implementation):

- **Library authors** depend only on the API. The `mongo-go-driver` instrumentation package imports `go.opentelemetry.io/otel/trace` (API), never the SDK. This means a library can ship instrumentation that costs zero at runtime if no SDK is registered — the API's no-op tracer is a stub.
- **Application authors** import the SDK once at `main()` and register it as the global provider. From that moment, every API call in every dependency starts producing real spans.

```go
// In main.go, application code (the only place SDK is imported):
import (
    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/sdk/trace"
    "go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
)

func main() {
    exp, _ := otlptracegrpc.New(ctx)
    tp := trace.NewTracerProvider(
        trace.WithBatcher(exp),
        trace.WithResource(res),
        trace.WithSampler(trace.ParentBased(trace.TraceIDRatioBased(0.1))),
    )
    otel.SetTracerProvider(tp)        // <- this is the registration
    defer tp.Shutdown(context.Background())
    // ... your app
}
```

```go
// In any library (no SDK import):
import "go.opentelemetry.io/otel"

func DoQuery(ctx context.Context, q string) {
    ctx, span := otel.Tracer("mongodb").Start(ctx, "mongo.find")
    defer span.End()
    // ...
}
```

> **Pitfall:** Library code that imports the SDK directly (instead of just the API) makes that library un-pluggable. Code review for `import .../sdk/...` in any package that isn't `main`.

---

## 3. The signal model: traces, metrics, logs, baggage

OTel models telemetry as four signal types. They share Resource and Context but have separate SDKs, separate exporters, and separate stability levels.

| Signal | Stable since | Purpose | Cardinality scaling |
|--------|--------------|---------|---------------------|
| **Traces** | 1.0 (Feb 2021) | Causal chain of operations, one span per unit of work | Per-request × span count |
| **Metrics** | 1.0 (Apr 2022) | Aggregated numerical signals over time | Per unique (name, attrs) tuple |
| **Logs** | 1.0 (Sept 2023) | Discrete records with structured attributes | Per emission |
| **Profiles** | Experimental (1.6, 2024) | Stack-trace-weighted samples of CPU/heap/etc | Per (process, time) |
| **Baggage** | Stable | Cross-cutting attributes propagated alongside context | Not exported; piggybacks |

### 3.1 What is a span, really?

A span is a structured record describing one unit of work. The OTLP protobuf for a span (lightly trimmed from `opentelemetry/proto/trace/v1/trace.proto`):

```protobuf
message Span {
  bytes  trace_id            = 1;   // 16 bytes, globally unique per trace
  bytes  span_id             = 2;   //  8 bytes, unique within trace
  string trace_state         = 3;   // W3C tracestate, vendor-specific KVs
  bytes  parent_span_id      = 4;   //  8 bytes, empty for root span
  uint32 flags               = 16;  // sampled bit + random bit (W3C)
  string name                = 5;   // e.g. "GET /users/{id}"
  SpanKind kind              = 6;   // INTERNAL/SERVER/CLIENT/PRODUCER/CONSUMER
  fixed64 start_time_unix_nano = 7;
  fixed64 end_time_unix_nano   = 8;
  repeated KeyValue attributes = 9; // arbitrary K/V pairs
  uint32 dropped_attributes_count = 10;
  repeated Event events       = 11; // timestamped points within the span
  repeated Link  links        = 13; // references to other spans (e.g. async)
  Status status               = 15; // OK | ERROR | UNSET + message
}

message Status {
  string message = 2;
  enum StatusCode { UNSET = 0; OK = 1; ERROR = 2; }
  StatusCode code = 3;
}
```

Five things to internalize:

1. **TraceID is 16 bytes (128 bits) of randomness.** Encoded as 32 hex chars on the wire. Birthday collision becomes possible only at ~2^64 traces — i.e., never in practice.
2. **SpanID is 8 bytes (64 bits).** Unique within a trace, not globally. Forty billion spans in one trace would still be safe (your trace store is not).
3. **Attributes vs Events vs Links.** Attributes are static K/V on the span ("http.url=/foo"). Events are timestamped log-line-like records *within* the span ("cache.miss" at T+12ms). Links are pointers to *other* spans, used for fan-in or async correlation (e.g., a Kafka consumer span links back to N producer spans).
4. **SpanKind is load-bearing.** `SERVER` and `CLIENT` create the natural service-graph edges; `INTERNAL` spans don't. `PRODUCER` and `CONSUMER` model async messaging. The collector's `servicegraph` processor uses these to derive RED metrics per edge — get the kind wrong and your service graph silently lies.
5. **Status.code = ERROR is the *only* sanctioned error signal.** Setting an attribute like `error=true` doesn't make the span show up red in a UI. `span.SetStatus(codes.Error, msg)` does.

### 3.2 What is a metric, really?

A metric is a *time series* — a (name, attributes) pair that produces points over time. The OTLP wire shape:

```protobuf
message ResourceMetrics {
  Resource resource                       = 1;
  repeated ScopeMetrics scope_metrics     = 2;
  string schema_url                       = 3;
}

message ScopeMetrics {
  InstrumentationScope scope              = 1;
  repeated Metric metrics                 = 2;
}

message Metric {
  string name        = 1;       // e.g. "http.server.request.duration"
  string description = 2;
  string unit        = 3;       // UCUM unit, e.g. "ms" or "By"
  oneof data {
    Gauge       gauge       = 5;
    Sum         sum         = 7;
    Histogram   histogram   = 9;
    ExponentialHistogram exponential_histogram = 10;
    Summary     summary     = 11;  // legacy, Prometheus-only
  }
}

message Sum {
  repeated NumberDataPoint data_points = 1;
  AggregationTemporality   aggregation_temporality = 2;  // CUMULATIVE | DELTA
  bool                     is_monotonic = 3;
}

message NumberDataPoint {
  repeated KeyValue attributes  = 7;
  fixed64 start_time_unix_nano  = 2;
  fixed64 time_unix_nano        = 3;
  oneof value { double as_double = 4; sfixed64 as_int = 6; }
  repeated Exemplar exemplars   = 5;  // <- trace_id linkage lives here
}
```

The two non-obvious fields:

- **AggregationTemporality.** A Sum can be CUMULATIVE (value monotonically grows from process start, like Prometheus counters) or DELTA (each point is the increment over the previous interval, like StatsD). This is the source of more wire-format bugs than every other field combined. §7 covers it.
- **Exemplars on data points.** The exemplar carries a `trace_id` and `span_id` of one specific request that contributed to the bucket. This is the magic that lets a Grafana dashboard panel jump directly to a trace.

### 3.3 What is a log, really?

A LogRecord is much simpler — it predates OTel-the-framework, and OTel essentially borrowed the structured-logging shape:

```protobuf
message LogRecord {
  fixed64 time_unix_nano        = 1;
  fixed64 observed_time_unix_nano = 11;  // when collector saw it
  SeverityNumber severity_number = 2;
  string severity_text          = 3;
  AnyValue body                 = 5;    // the message
  repeated KeyValue attributes  = 6;
  uint32 dropped_attributes_count = 7;
  uint32 flags                  = 8;
  bytes  trace_id               = 9;    // <- correlation
  bytes  span_id                = 10;   // <- correlation
}
```

The TraceContext fields (`trace_id`, `span_id`) are first-class on the LogRecord. When a logger emits within a span context, the OTel "log bridge" populates them automatically — no manual code needed. This is the mechanical basis of every "click log → see trace" feature in modern observability UIs.

### 3.4 Baggage — the underused fifth signal

Baggage is not exported. It rides alongside Context across process boundaries via the `baggage:` HTTP header (W3C Baggage spec) and is intended to carry small cross-cutting values — `tenant_id`, `experiment_variant`, `user_segment` — that downstream services should be *aware of* but not necessarily emit.

```
baggage: tenant=acme,experiment=variant_b,user_segment=pro
```

> **Pitfall:** Baggage is set by *whoever is upstream*, including the browser. Never trust it across a security boundary. A user can send `baggage: tenant=other_company` and your services will happily propagate it. Strip baggage at the gateway; re-inject the trusted value server-side.

---

## 4. Context propagation: the most important piece

Pick exactly one thing to get right in OTel and it's this. Break propagation once and your trace becomes two disconnected traces — neither one is useful.

### 4.1 W3C Trace Context — the headers on the wire

There are two W3C-standardized HTTP headers that every OTel SDK emits and parses by default:

```
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
             ^^  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^  ^^
             |   trace-id (16 bytes / 32 hex)     parent-id (8b/16h) flags
             version

tracestate: rojo=00f067aa0ba902b7,congo=t61rcWkgMzE
            ^^^^^^^^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^
            vendor-specific list of key=value pairs (≤ 32 entries)
```

The `traceparent` byte layout is fixed:

```
 ┌──────┬──────────────────────────────────┬────────────────┬──────┐
 │  00  │ trace-id (128 bits, hex)         │ span-id (64 b) │  XX  │
 │ vers │ "4bf92f...4736"                  │ "00f067...02b7"│ flag │
 └──────┴──────────────────────────────────┴────────────────┴──────┘
   1 B            16 B                            8 B          1 B

 Trace flags (the last byte) — bits, MSB→LSB:
   bit 7  reserved    bit 6  reserved    ...    bit 1  random  bit 0  sampled
                                                       (vRFC)         (THE bit)

 sampled = 1 (0x01)  → downstream MUST treat this trace as captured
 sampled = 0 (0x00)  → downstream MAY drop it
```

The **sampled bit** is the single most important byte in observability. It is set by the *first* sampler that makes a decision (typically at the entry gateway) and is preserved verbatim through every subsequent service. The default OTel sampler (`ParentBased`) uses it to make every downstream service inherit the upstream decision — without this, half your trace would sample-in and the other half would sample-out.

### 4.2 The diagram

```
   Service A (gateway)                       Service B (cart)
   ───────────────────                       ─────────────────
   inbound HTTP request
        │
        ▼
   [ extract traceparent from headers ]
        │   (none present → mint root: trace_id=R, span_id=S1, sampled=1)
        ▼
   Sampler decides:   keep
        │
        ▼
   start span "GET /checkout"  (parent=none, span=S1)
        │
        │   internal work...
        │
        ▼
   outbound HTTP call to B
        │
        │   [ inject traceparent into headers ]
        │   traceparent: 00-R-S2-01     where S2 is a NEW span_id for the CLIENT span
        ▼  ───────────────────────────────────►   inbound request
                                                       │
                                                       ▼
                                                 [ extract traceparent ]
                                                       │
                                                       ▼  trace_id=R, parent=S2, sampled=1
                                                 Sampler: ParentBased → keep (because parent kept)
                                                       │
                                                       ▼
                                                 start span "GET /carts/123" (parent=S2, span=S3)
                                                       │
                                                       │   internal work...
                                                       │
                                                       ▼
                                                 end span S3
        ◄──────────────────────────────────────  return response

    end CLIENT span S2
   end span S1
```

### 4.3 Multiple propagation formats

The W3C Trace Context spec is the modern default, but real-world fleets often have legacy services emitting other formats. OTel's `Composite` propagator parses all of them simultaneously:

| Format | Header(s) | Used by |
|--------|-----------|---------|
| **W3C Trace Context** | `traceparent`, `tracestate` | Default for all OTel SDKs ≥ 0.16 |
| **W3C Baggage** | `baggage` | Default for all OTel SDKs |
| **B3 single-header** | `b3: <trace-id>-<span-id>-<sampled>-<parent>` | Zipkin, Istio (older configs) |
| **B3 multi-header** | `x-b3-traceid`, `x-b3-spanid`, `x-b3-sampled`, `x-b3-parentspanid` | Zipkin (legacy) |
| **Jaeger** | `uber-trace-id: <trace>:<span>:<parent>:<flags>` | Jaeger native instrumentation |
| **AWS X-Ray** | `x-amzn-trace-id: Root=1-...;Parent=...;Sampled=1` | AWS-native services |
| **GCP Cloud Trace** | `x-cloud-trace-context: TRACE/SPAN;o=1` | Google Cloud |

```go
otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
    propagation.TraceContext{},   // W3C
    propagation.Baggage{},        // W3C baggage
    b3.New(),                     // legacy B3
    jaegerprop.Jaeger{},          // legacy Jaeger
))
```

### 4.4 gRPC vs HTTP

gRPC propagation uses the *same* logical headers, transmitted as `Metadata` entries. The OTel gRPC instrumentation (`otelgrpc`) injects/extracts via interceptors automatically:

```go
// server side
grpc.NewServer(grpc.StatsHandler(otelgrpc.NewServerHandler()))

// client side
conn, _ := grpc.Dial(addr, grpc.WithStatsHandler(otelgrpc.NewClientHandler()))
```

### 4.5 Async propagation: the hardest case

For Kafka, RabbitMQ, SQS, and batch jobs, there is no synchronous request to inject headers into. The pattern is:

```
Producer side:
  msg := kafka.Message{Value: payload}
  otel.GetTextMapPropagator().Inject(ctx, kafkaCarrier(&msg))   // sets headers on the message
  producer.Send(msg)

Consumer side (often minutes/hours later):
  msg := consumer.Poll()
  ctx := otel.GetTextMapPropagator().Extract(ctx, kafkaCarrier(msg))
  ctx, span := tracer.Start(ctx, "process",
      trace.WithSpanKind(trace.SpanKindConsumer),
      trace.WithLinks(trace.LinkFromContext(ctx)),  // or as parent, see below
  )
  defer span.End()
```

Two design choices for how the consumer span relates to the producer:

- **As parent** (consumer's parent_span_id = producer's span_id). Best when the producer span is short-lived; the trace gets one continuous tree. Risk: the producer span has already ended and been exported, so the consumer joining it can produce traces that "back-fill" historical traces in the store.
- **As link** (consumer creates a *new root* trace, attaches a Link to the producer's span). Best for fan-out and high-volume streaming. Each consumer becomes its own trace; aggregate analysis happens via the link relationship.

Most messaging instrumentation libraries default to "as parent" for synchronous-ish patterns and "as link" for streaming.

### 4.6 The `Context` object — the in-process carrier

OTel doesn't pass spans by name. It passes a `Context` object that contains the active span (and active baggage, and active TracerProvider) implicitly:

```go
// Go: explicit context.Context (idiomatic Go)
ctx, span := tracer.Start(ctx, "operation")
// span is implicit in ctx; child operations pass ctx down
defer span.End()
```

```python
# Python: contextvars (asyncio-aware)
from opentelemetry import trace
tracer = trace.get_tracer(__name__)
with tracer.start_as_current_span("operation") as span:
    # span is the *active* span; nested calls pick it up via contextvars
    do_work()
```

```java
// Java: ThreadLocal (with Scope)
Span span = tracer.spanBuilder("operation").startSpan();
try (Scope scope = span.makeCurrent()) {
    // span is the active span on this thread
    doWork();
} finally {
    span.end();
}
```

```javascript
// Node.js: AsyncLocalStorage (since v14)
const tracer = trace.getTracer('my-app');
tracer.startActiveSpan('operation', (span) => {
    try { doWork(); }
    finally { span.end(); }
});
```

> **Pitfall:** In Java, every async hop (CompletableFuture, ExecutorService) breaks the ThreadLocal. You must wrap the executor with `Context.taskWrapping()` or use the OTel `ContextPropagatingExecutor`. Async Java + naive OTel = silent context loss.

> **Pitfall:** In Node.js, packages that use callback-based APIs (older MongoDB drivers, older AWS SDK v2) can lose AsyncLocalStorage context. Either upgrade the dep or use the OTel auto-instrumentation patches that monkey-patch the callbacks.

---

## 5. Resource and Instrumentation Scope

The two pieces of metadata that *aren't* on every span/metric/log because they're factored out one level up.

### 5.1 Resource — "who emitted this?"

A `Resource` is a set of attributes describing the *entity producing telemetry*: the service instance. It is attached *once per OTLP batch*, not per signal. Per-span attributes describe per-span things; per-resource attributes describe per-process things.

The semantic-convention required keys:

| Resource attribute | Required? | Example | Source |
|--------------------|-----------|---------|--------|
| `service.name` | **required** | `checkout` | env var `OTEL_SERVICE_NAME`, manual config |
| `service.instance.id` | strongly recommended | `pod-abc123` | hostname, k8s.pod.uid |
| `service.namespace` | optional | `payments-team` | manual config |
| `service.version` | recommended | `2025.10.3-a4f7` | git SHA, CI variable |
| `deployment.environment` | recommended | `production` / `staging` | env var |
| `host.name` | auto-detected | `ip-10-0-1-23` | OS hostname |
| `host.id` | auto-detected | EC2 instance ID, k8s node UID | cloud SDK detector |
| `cloud.provider` | auto-detected | `aws`, `gcp`, `azure` | cloud detector |
| `cloud.region` | auto-detected | `us-east-1` | cloud detector |
| `k8s.namespace.name` | auto-detected (in K8s) | `default` | downward API or operator |
| `k8s.pod.name` | auto-detected | `checkout-7d8f-x9k2j` | downward API |
| `k8s.deployment.name` | auto-detected | `checkout` | k8s API |
| `k8s.node.name` | auto-detected | `node-12` | downward API |

```go
res, _ := resource.New(ctx,
    resource.WithFromEnv(),              // OTEL_RESOURCE_ATTRIBUTES env var
    resource.WithProcess(),              // process.pid, runtime.name, runtime.version
    resource.WithOS(),                   // os.type, os.description
    resource.WithContainer(),            // container.id from cgroup
    resource.WithHost(),                 // host.name
    resource.WithAttributes(
        semconv.ServiceName("checkout"),
        semconv.ServiceVersion(buildSHA),
        semconv.DeploymentEnvironment("production"),
    ),
)
```

> **Mental model:** A Resource attribute is *constant for the process lifetime*. If you find yourself wanting to put a value on every span that's actually a property of the service ("our checkout service runs in EU-west-1"), it belongs on the Resource. Putting it on every span multiplies wire bytes and storage cost.

> **Pitfall:** Conflicting `service.name` between SDK config and collector enrichment. SDK says `checkout`, collector's `resource` processor says `cart`. OTel doesn't merge: whoever wrote last wins, and the answer is non-deterministic across signals. Pick one source of truth (usually the SDK) and have the collector's resource processor only *add* attributes, never overwrite.

### 5.2 InstrumentationScope — "what code emitted this?"

Originally called "InstrumentationLibrary," renamed in OTel 1.10 to acknowledge that scopes can be finer than libraries (e.g., per-package within a library). It identifies the *instrumentation*, not the application code.

```protobuf
message InstrumentationScope {
  string name    = 1;   // e.g. "go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
  string version = 2;   // e.g. "0.46.1"
  repeated KeyValue attributes = 3;
}
```

```go
tracer := otel.Tracer("github.com/myorg/checkout/handlers", trace.WithInstrumentationVersion("v1.4.0"))
```

The scope name+version is what shows up on a span as `otel.library.name` / `otel.library.version`. Used by:

- Backends to render "this span came from the otelhttp library v0.46.1" — useful for "is this an instrumentation bug or app bug?" debugging.
- Sampling policies that want to drop spans from one specific noisy library while keeping everything else.
- `ScopeMetrics`/`ScopeLogs` aggregation: all metrics from the same scope share serialization.

---

## 6. Sampling

You will not store every span. At 1k RPS × ~30 spans/request, you produce **2.6 billion spans/day**. At ~500 bytes/span on the wire that is 1.3 TB/day of trace data per service. Multiply by your fleet. Storage is the easy problem; the SDK's CPU budget for serializing the spans you'll throw away is the harder one.

OTel has two sampling layers that play very different roles.

### 6.1 Head sampling — at the SDK, before context propagation

A `Sampler` is an interface called *before* a span is even started. The SDK passes the would-be span's parent context, name, kind, attributes, and links; the sampler returns a `SamplingResult{Decision, Attributes, TraceState}`.

```go
type Sampler interface {
    ShouldSample(parameters SamplingParameters) SamplingResult
    Description() string
}

type SamplingResult struct {
    Decision   SamplingDecision  // Drop | RecordOnly | RecordAndSample
    Attributes []attribute.KeyValue
    Tracestate trace.TraceState
}
```

The three decisions:

| Decision | Span created? | Exported? | Notes |
|----------|---------------|-----------|-------|
| `Drop` | No | No | Cheapest; spans aren't even constructed |
| `RecordOnly` | Yes | No | Span exists in-process; useful for in-process metrics derived from spans |
| `RecordAndSample` | Yes | Yes | Normal sampled path |

Built-in samplers:

| Sampler | Behavior | Use when |
|---------|----------|----------|
| `AlwaysOn` | sample everything | dev only |
| `AlwaysOff` | sample nothing | "kill switch" |
| `TraceIDRatioBased(ratio)` | hash trace_id, sample if hash < ratio | uniform random sampling, deterministic across services |
| `ParentBased(root)` | follow parent's sampled bit; if no parent, defer to `root` sampler | **default and correct choice** for every service that isn't a root |

The crucial design: `TraceIDRatioBased` derives its decision from the *trace_id itself* (specifically, the low 8 bytes interpreted as a uint64, compared to ratio × MaxUint64). This means service A and service B, given the same trace_id, will make the *same decision* — head sampling becomes consistent across the trace.

`ParentBased` then says: if the upstream service made a decision (sampled bit set in `traceparent` flags), inherit it. Only the root span's service makes the decision.

```go
// Idiomatic: 10% root sampling, parent-based downstream
trace.WithSampler(
    trace.ParentBased(trace.TraceIDRatioBased(0.1)),
)
```

### 6.2 Tail sampling — at the collector, after assembly

Head sampling has a fundamental limitation: at the moment of decision, you don't yet know if the trace will be interesting. The 1% slow request, the 0.01% error trace — head sampling drops them with the same probability as the boring 99%.

Tail sampling fixes this by making the decision *after* the trace finishes. The collector buffers spans by trace_id for a configurable window (typically 30s), then evaluates policies:

```yaml
processors:
  tail_sampling:
    decision_wait: 30s
    num_traces: 100000
    expected_new_traces_per_sec: 1000
    policies:
      - name: errors
        type: status_code
        status_code: { status_codes: [ERROR] }
      - name: slow
        type: latency
        latency: { threshold_ms: 500 }
      - name: rare-service
        type: string_attribute
        string_attribute:
          key: service.name
          values: [payment-fraud-detector]
      - name: probabilistic
        type: probabilistic
        probabilistic: { sampling_percentage: 1 }
```

The cost: the collector now needs to *assemble* full traces in memory for `decision_wait` seconds before deciding. This is doable up to ~10–50k traces/second per collector replica, but requires careful memory budgeting (`num_traces` × average spans × 500 bytes). Above that scale, you shard by trace_id across multiple collectors using a `loadbalancing` exporter that hashes trace_id consistently.

> **Mental model:** Head sampling decides *whether to spend the SDK's CPU and the wire bandwidth*. Tail sampling decides *what to spend the storage cost on*. You almost always want both: head sample to ~10–25% to control SDK overhead, then tail sample at the collector to keep the interesting 5%.

### 6.3 The trace-flags bit layout

```
trace flags byte (the "01" at the end of traceparent):
   ┌───┬───┬───┬───┬───┬───┬───┬───┐
   │ 7 │ 6 │ 5 │ 4 │ 3 │ 2 │ 1 │ 0 │
   └───┴───┴───┴───┴───┴───┴───┴───┘
                              │   │
                              │   └── sampled bit (W3C v0)
                              └────── random bit (W3C v1, Sept 2024+):
                                        when 1, indicates trace_id is uniform-random,
                                        enabling consistent random sampling at intermediaries
```

Only the low 2 bits are defined. The rest is reserved. Vendors who tried to encode metadata in higher bits got nuked when v1 added the random bit; don't be that vendor.

---

## 7. Metrics SDK internals

The metrics SDK is the most subtly complex part of OTel. Spans are conceptually simple (start, end, batch, send); metrics involve aggregation, temporality, and a query-time-vs-write-time conflict between Prometheus-style backends and Datadog/StatsD-style backends.

### 7.1 Instruments: synchronous vs asynchronous

| Instrument | Type | Sync/async | Aggregation | Example |
|------------|------|------------|-------------|---------|
| **Counter** | monotonic Sum | sync | Sum | `requests_total.Add(1)` |
| **UpDownCounter** | non-monotonic Sum | sync | Sum | `queue_depth.Add(+1) / .Add(-1)` |
| **Histogram** | distribution | sync | Histogram or ExpHistogram | `latency.Record(0.087)` |
| **Gauge** (sync, since 1.27) | last value | sync | LastValue | `temperature.Record(72.5)` |
| **ObservableCounter** | monotonic Sum | async (callback) | Sum | scrape `/proc/self/io` total bytes |
| **ObservableUpDownCounter** | Sum | async | Sum | poll current open files |
| **ObservableGauge** | LastValue | async | LastValue | poll memory in use |

Async instruments are read by the SDK calling a user-supplied callback at *export time* (e.g. every 60s for OTLP, every 15s for Prometheus). The user code never calls `.Record()` directly — instead it returns the current value when polled. This is how you bridge polling-based data sources (kernel counters, `/proc`) into the metric pipeline.

```go
meter := otel.Meter("checkout")

// Synchronous counter
reqCounter, _ := meter.Int64Counter("checkout.requests",
    metric.WithDescription("Total checkout requests"))
reqCounter.Add(ctx, 1, metric.WithAttributes(
    attribute.String("status", "ok"),
    attribute.String("payment_method", "card"),
))

// Synchronous histogram
latency, _ := meter.Float64Histogram("checkout.duration",
    metric.WithUnit("s"),
    metric.WithExplicitBucketBoundaries(0.01, 0.05, 0.1, 0.5, 1, 5, 10))
latency.Record(ctx, elapsed.Seconds(), metric.WithAttributes(...))

// Asynchronous gauge
_, _ = meter.Int64ObservableGauge("checkout.queue_depth",
    metric.WithInt64Callback(func(ctx context.Context, o metric.Int64Observer) error {
        o.Observe(int64(queue.Len()))
        return nil
    }),
)
```

```python
from opentelemetry import metrics
meter = metrics.get_meter("checkout")

req_counter = meter.create_counter("checkout.requests")
req_counter.add(1, {"status": "ok", "payment_method": "card"})

latency_hist = meter.create_histogram(
    "checkout.duration", unit="s",
    explicit_bucket_boundaries_advisory=[0.01, 0.05, 0.1, 0.5, 1, 5, 10],
)
latency_hist.record(elapsed_seconds, {"status": "ok"})

def queue_depth_callback(options):
    yield metrics.Observation(queue.qsize(), {})
meter.create_observable_gauge("checkout.queue_depth", callbacks=[queue_depth_callback])
```

### 7.2 Histograms: explicit-bucket vs exponential

The two histogram aggregations:

**Explicit-bucket histogram** — the classic Prometheus shape. You declare boundaries (`[0.005, 0.01, 0.025, 0.05, 0.1, ...]`); each observation bumps the bucket counters at and above its value.

```
buckets:  [≤0.005, ≤0.01, ≤0.025, ≤0.05, ≤0.1, ≤0.25, ≤0.5, ≤1, ≤2.5, ≤5, ≤10, +Inf]
counts:   [    14,    23,     91,    341,  812,  1241, 1342, 1399,..., total]
```

Pros: fully Prometheus-compatible, query-friendly with `histogram_quantile()`. Cons: you must pick the boundaries up front; mismatches between expected and actual distribution destroy quantile accuracy. Memory ~ N buckets × cardinality.

**Exponential histogram** (the new hotness, stable in OTel 1.4) — base-2 exponential bucketing with auto-scaling. A single histogram covers any range from microseconds to days with constant memory (typically 160 buckets). The base is `2^(2^-scale)`; scale is auto-adjusted to fit observations.

```
scale = 4  →  base = 2^(1/16) ≈ 1.0443
buckets    [1.0, 1.0443, 1.0905, 1.1387, 1.1892, ...]   exponential boundaries
```

Pros: no upfront knowledge of distribution; merges across services exactly; native quantiles. Cons: not all backends support it (Prometheus 2.40+ does as "native histograms"; many APM vendors do; some legacy backends don't). Wire format is a bit more complex.

Use exponential histograms unless you have a backend constraint. They are strictly better for new instrumentation.

### 7.3 Temporality: cumulative vs delta — the hidden landmine

```
                CUMULATIVE                              DELTA
                ──────────                              ─────
T=0    counter = 0                            counter[0..1) = 5
T=1    counter = 5                            counter[1..2) = 7
T=2    counter = 12                           counter[2..3) = 3
T=3    counter = 15                           counter[3..4) = 8
T=4    counter = 23

Used by: Prometheus (every counter)           Used by: StatsD, Datadog,
Used by: OpenCensus default                            CloudWatch (sometimes)
```

**Cumulative** is what Prometheus mandates: every counter is monotonically increasing from a "start time," and rate is computed at query time as `(value - older_value) / time_delta`. The SDK keeps a running total and exports it on every interval.

**Delta** is per-interval increments: each export carries only the change since the last. Datadog ingests this directly. CloudWatch's "sum" statistic is delta.

OTel SDKs support both, configurable per-instrument-kind via the SDK's `temporality_selector`. The default depends on the exporter:

```go
// OTLP exporter for Prometheus / Mimir target
otlpmetricgrpc.New(ctx,
    otlpmetricgrpc.WithTemporalitySelector(func(kind metric.InstrumentKind) metricdata.Temporality {
        return metricdata.CumulativeTemporality   // default
    }),
)

// Same exporter, configured for a Datadog / delta backend
otlpmetricgrpc.New(ctx,
    otlpmetricgrpc.WithTemporalitySelector(func(kind metric.InstrumentKind) metricdata.Temporality {
        switch kind {
        case metric.InstrumentKindCounter, metric.InstrumentKindHistogram:
            return metricdata.DeltaTemporality
        default:
            return metricdata.CumulativeTemporality
        }
    }),
)
```

> **Pitfall:** Switching temporality mid-flight resets counters. If your collector flips a counter from cumulative to delta because of a config change, downstream Prometheus sees a negative delta and produces phantom rate spikes / drops. Pick once per pipeline; document the choice.

### 7.4 Views — reshaping the metric stream

A `View` is the metrics-SDK escape hatch. It lets you:

- Rename a metric (`http.server.duration` → `http_request_duration_seconds`)
- Drop attributes (cardinality control at the SDK)
- Change aggregation (force a specific histogram boundary set)
- Filter out instruments entirely

```go
view := metric.NewView(
    metric.Instrument{Name: "http.server.duration"},
    metric.Stream{
        Aggregation: metric.AggregationExplicitBucketHistogram{
            Boundaries: []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10},
        },
        AttributeFilter: attribute.NewAllowKeysFilter(
            "http.method", "http.route", "http.status_code",
            // explicitly drop high-cardinality keys like http.target
        ),
    },
)
mp := metric.NewMeterProvider(metric.WithView(view), metric.WithReader(...))
```

Views are how you fix instrumentation bugs without touching the application code. A library author emits a metric with `user_id` as an attribute by mistake — a View at the application level drops the attribute before export.

### 7.5 Cardinality limits

OTel SDKs enforce a per-instrument cardinality cap (default 2000 distinct attribute sets). Beyond that, observations land in a single "overflow" series tagged `otel.metric.overflow=true`. This prevents a single bad attribute (e.g. `user_id` accidentally on a counter) from blowing up the SDK's memory before the operator even notices.

You can tune this:

```go
metric.WithCardinalityLimit(5000)
```

But the right answer is usually a View that drops the offending attribute, not a higher limit.

---

## 8. Logs

Logs are the most recent OTel signal to stabilize, and the integration story is fundamentally different from traces and metrics: in most existing fleets, **logs already exist** and flow via stdout → agent → store. OTel doesn't try to replace that pipe overnight. It bridges into it.

### 8.1 The log bridge pattern

The dominant pattern is *not* "rewrite your logger to use the OTel logs SDK." It's "keep your existing logger; wire the OTel log bridge to capture its records and inject trace_id/span_id."

| Language | Native logger | OTel bridge |
|----------|---------------|-------------|
| Go | `log/slog` (Go 1.21+) | `go.opentelemetry.io/contrib/bridges/otelslog` |
| Java | Log4j 2 / Logback | `opentelemetry-log4j-appender`, `opentelemetry-logback-appender` |
| Python | `logging` | `opentelemetry-instrumentation-logging` (auto-injects fields) |
| Node.js | pino / winston | `@opentelemetry/instrumentation-pino`, `-winston` |
| .NET | Microsoft.Extensions.Logging | `OpenTelemetry.Logs` |

```go
// Go: slog bridge — the bridge pipes every slog record through the OTel logs SDK
import (
    "log/slog"
    "go.opentelemetry.io/contrib/bridges/otelslog"
)

logger := otelslog.NewLogger("checkout")  // emits to OTel SDK + carries trace/span IDs
slog.SetDefault(logger)

// Application code is unchanged; trace_id is injected automatically when called from a span ctx
slog.InfoContext(ctx, "user.login", "user_id", uid)
```

The bridge does three things automatically:

1. Reads the current span from `Context`
2. Sets `trace_id` and `span_id` on the OTLP LogRecord
3. If the log severity is ERROR and the span is still open, may also bump the span's status (configurable)

### 8.2 Two flow paths

```
Flow A (the future state):
    app → OTel logs SDK → OTLP/gRPC → collector → log backend

Flow B (the bridge state, most fleets in 2026):
    app → existing logger → stdout → agent (Vector/Fluent Bit) → backend
                                                  ↑
                                                  └── trace_id/span_id injected at the agent
                                                      via JSON parsing of the record
```

Both work. Flow A is more efficient (no JSON serialization round-trip; structured attributes preserved). Flow B is operationally cheaper (no SDK upgrade, agents already exist). Most teams run Flow B for now and migrate to Flow A on the next refactor.

### 8.3 LogRecord schema (revisited)

Beyond the protobuf in §3.3, the most-used OTel log attributes follow semantic conventions:

| Attribute | Meaning | Example |
|-----------|---------|---------|
| `body` | The free-text or structured message | `"user.login.failed"` |
| `severity_number` | OTel-canonical level (1–24) | `9` (INFO), `17` (ERROR) |
| `severity_text` | Original level name | `"INFO"`, `"ERROR"` |
| `event.name` | Convention for structured event | `"http.request"` |
| `code.function` | Function emitting the log | `"handleCheckout"` |
| `code.filepath` | Source file | `"handlers/checkout.go"` |
| `code.lineno` | Line number | `142` |
| `exception.type` | Class of exception | `"NullPointerException"` |
| `exception.message` | Exception message | `"user_id is null"` |
| `exception.stacktrace` | Full stack trace | `"at ..."` |

The `severity_number` standardization is more useful than it looks: it lets a Loki query for `severity_number >= 17` work identically across Java (Logback level ERROR=40000), Go (slog level ERROR=8), and Python (logging level ERROR=40), without per-language casing.

---

## 9. OTLP wire protocol

OTLP is the opinionated transport. There is no JSON-over-WebSocket variant, no "lightweight" mode — the wire format is protobuf, and the only choice is the carrier.

### 9.1 The four endpoints

| Signal | gRPC method | HTTP path | Default port |
|--------|-------------|-----------|--------------|
| Traces | `opentelemetry.proto.collector.trace.v1.TraceService/Export` | `/v1/traces` | 4317 (gRPC), 4318 (HTTP) |
| Metrics | `opentelemetry.proto.collector.metrics.v1.MetricsService/Export` | `/v1/metrics` | 4317 / 4318 |
| Logs | `opentelemetry.proto.collector.logs.v1.LogsService/Export` | `/v1/logs` | 4317 / 4318 |
| Profiles (experimental) | `opentelemetry.proto.collector.profiles.v1development.ProfilesService/Export` | `/v1development/profiles` | 4317 / 4318 |

### 9.2 gRPC vs HTTP/protobuf vs HTTP/JSON

| Variant | Wire format | When |
|---------|-------------|------|
| **OTLP/gRPC** | protobuf over HTTP/2 | Default. Best perf, native bidirectional streaming, server can apply backpressure. |
| **OTLP/HTTP/protobuf** | protobuf body, content-type `application/x-protobuf` | Use when gRPC is blocked by an L7 proxy (some old AWS ALBs, some firewalls). Same payloads, single request per export. |
| **OTLP/HTTP/JSON** | JSON-encoded protobuf, content-type `application/json` | Browsers (RUM SDK), debugging via curl. ~3× larger on the wire; not recommended in prod for backends. |

The protobuf-on-the-wire shape for a trace export request is just `ExportTraceServiceRequest { ResourceSpans[] resource_spans }` — a list of resource-grouped span batches. The collector accepts an `ExportTraceServiceResponse` back containing per-span partial failure information.

### 9.3 Compression

```
OTLP supported compression algorithms:
  - none   (the default, for small batches in trusted networks)
  - gzip   (universally supported; ~70% reduction for trace data)
  - zstd   (better ratio + faster decode; supported as of OTel 1.10+)
```

zstd typically wins by ~15% over gzip on trace data with similar CPU. Most production gateways enable zstd.

### 9.4 Batching

The SDK's `BatchSpanProcessor` (and its metric/log equivalents) controls outbound shape:

| Knob | Default | Notes |
|------|---------|-------|
| `MaxQueueSize` | 2048 spans | If the queue fills, new spans are dropped (with a counter) |
| `ScheduledDelay` | 5s | Timer-driven flush |
| `MaxExportBatchSize` | 512 spans | Per-network-call cap |
| `ExportTimeout` | 30s | Fail-the-export deadline |

A typical service emitting 100 spans/s sends one OTLP request every 5 seconds carrying 500 spans, ~250 KB compressed. At 1k services that's ~50 MB/s into the gateway — sustainable on a single collector replica with batching.

### 9.5 Authentication

The OTLP spec is silent on authentication. Two patterns dominate:

- **Bearer token in `Authorization` header.** The SDK ships an `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer xxx` configuration. Used by SaaS vendors (Honeycomb, Datadog) for tenant-id + API key in one header.
- **mTLS.** The collector terminates TLS with client certificate verification. Used inside K8s clusters or between trusted regions. Easier to rotate via service mesh (Linkerd, Istio).

A surprising number of fleets run OTLP unauthenticated *inside the cluster* — relying on K8s NetworkPolicies for the security boundary. Defensible if your cluster has a strong network policy story; lazy if not.

---

## 10. The OTel Collector

The collector is a single Go binary with a YAML pipeline. The architecture is **receivers → processors → exporters**, where each is a plugin.

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │                      OTel Collector pipeline                         │
   │                                                                      │
   │   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐                │
   │   │ Receivers   │   │ Processors  │   │ Exporters   │                │
   │   │             │   │             │   │             │                │
   │   │ otlp        │──▶│ memory_     │──▶│ otlp/tempo  │──▶ Tempo       │
   │   │ prometheus  │   │   limiter   │   │ otlp/datadog│──▶ Datadog     │
   │   │ jaeger      │   │ batch       │   │ prometheus_ │                │
   │   │ filelog     │   │ resource    │   │   remote_   │──▶ Mimir       │
   │   │ kafka       │   │ attributes  │   │   write     │                │
   │   │ syslog      │   │ filter      │   │ loki        │──▶ Loki        │
   │   │ statsd      │   │ tail_       │   │ kafka       │──▶ Kafka       │
   │   │             │   │   sampling  │   │ debug       │                │
   │   │             │   │ transform   │   │             │                │
   │   │             │   │ redaction   │   │             │                │
   │   └─────────────┘   └─────────────┘   └─────────────┘                │
   │                                                                      │
   │   Configured via YAML; each pipeline is per-signal-type.             │
   └──────────────────────────────────────────────────────────────────────┘
```

### 10.1 Agent vs gateway — usually both

| Pattern | Where it runs | Job |
|---------|---------------|-----|
| **Agent** | DaemonSet (one per node) or sidecar | Per-node receiver: scrape /metrics endpoints, tail logs from disk, accept OTLP from same-node apps. Cheap forwarder; no heavy processing. |
| **Gateway** | Deployment (3–N replicas behind a service) | Aggregation point: tail sampling, redaction, fan-out to multiple backends, rate limiting. |

Most production fleets run **both**: app → local agent (UDP/Unix socket, ~100µs) → gateway (gRPC, batched) → backends. This isolates per-node failures from the central gateway and lets the gateway focus on cross-trace processing without per-node noise.

### 10.2 The processors that matter

| Processor | Purpose | Order |
|-----------|---------|-------|
| `memory_limiter` | Backpressure: drop incoming data if collector is OOM-bound | **first** in pipeline |
| `batch` | Accumulate before export to reduce export RPC count | **last** before exporters |
| `resource` | Add/modify Resource attributes (k8s, cloud) | early |
| `attributes` | Add/modify span/metric/log attributes | early |
| `filter` | Drop signals matching OTTL conditions | mid |
| `transform` | Powerful OTTL-based reshaping (rename, extract, compute) | mid |
| `redaction` | Hash or remove PII attributes (credit cards, emails) | mid |
| `tail_sampling` | Trace-level sampling after assembly | mid (traces only) |
| `k8sattributes` | Enrich with pod/namespace/deployment from K8s API | early |
| `routing` | Route signals to different exporters by attribute | last |

> **Pitfall:** Pipeline order matters. `memory_limiter` must be *first* (it can only protect downstream stages). `batch` must be *last* (or a tail_sampling decision built on a batch may straddle decision windows). The collector won't validate your order; it'll just behave weirdly.

### 10.3 A real gateway config

```yaml
# otel-collector-gateway.yaml — fans out traces to Tempo, metrics to Mimir,
# logs to Loki, with tail sampling and PII redaction.

receivers:
  otlp:
    protocols:
      grpc:  { endpoint: 0.0.0.0:4317 }
      http:  { endpoint: 0.0.0.0:4318 }

processors:
  memory_limiter:
    check_interval: 1s
    limit_percentage: 80
    spike_limit_percentage: 25

  k8sattributes:
    auth_type: "serviceAccount"
    extract:
      metadata: [k8s.pod.name, k8s.namespace.name, k8s.deployment.name, k8s.node.name]

  resource:
    attributes:
      - key: deployment.environment
        value: production
        action: upsert

  redaction:
    allow_all_keys: true
    blocked_values:
      - '4[0-9]{12}(?:[0-9]{3})?'                    # Visa CCs
      - '[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-z]{2,}'  # email
    summary: silent

  tail_sampling:
    decision_wait: 30s
    num_traces: 100000
    expected_new_traces_per_sec: 5000
    policies:
      - { name: errors,     type: status_code, status_code: { status_codes: [ERROR] } }
      - { name: slow,       type: latency,     latency: { threshold_ms: 1000 } }
      - { name: keep-all-checkout, type: string_attribute,
          string_attribute: { key: service.name, values: [checkout] } }
      - { name: probabilistic, type: probabilistic, probabilistic: { sampling_percentage: 1 } }

  batch:
    send_batch_size: 8192
    timeout: 5s

exporters:
  otlp/tempo:
    endpoint: tempo-distributor.observability:4317
    tls: { insecure: true }

  prometheusremotewrite/mimir:
    endpoint: https://mimir-distributor.observability/api/v1/push
    headers: { X-Scope-OrgID: "platform" }

  loki:
    endpoint: https://loki-gateway.observability/loki/api/v1/push
    headers: { X-Scope-OrgID: "platform" }

service:
  pipelines:
    traces:
      receivers:  [otlp]
      processors: [memory_limiter, k8sattributes, resource, redaction, tail_sampling, batch]
      exporters:  [otlp/tempo]
    metrics:
      receivers:  [otlp]
      processors: [memory_limiter, k8sattributes, resource, batch]
      exporters:  [prometheusremotewrite/mimir]
    logs:
      receivers:  [otlp]
      processors: [memory_limiter, k8sattributes, resource, redaction, batch]
      exporters:  [loki]

  telemetry:
    metrics: { level: detailed, address: 0.0.0.0:8888 }
```

That's ~60 lines. It does:

- accepts OTLP from in-cluster agents
- enriches every signal with K8s metadata it didn't have
- redacts credit-card and email-shaped strings from attributes
- tail-samples traces (errors + slow + checkout-service + 1% baseline)
- fans out traces to Tempo, metrics to Mimir, logs to Loki

The collector itself emits its own metrics on `:8888` — remember to scrape *those* (the observer must be observable; see `doc 04`).

---

## 11. Semantic Conventions

Semantic conventions are the *schema* for telemetry attribute names. Without them, OTel would just be a pile of strings. With them, a Java service emitting `http.request.method=POST` and a Go service emitting `http.request.method=POST` cross-correlate without translation.

### 11.1 Why this matters more than people realize

The default failure mode of un-conventioned telemetry: every team invents its own attribute name. One service emits `httpMethod`, another `http_method`, another `method`, another `request.method`. Now your dashboard pinning P99 latency by HTTP method requires `coalesce(httpMethod, http_method, method, request.method)` — and that's just for one attribute. Multiply across 50 attributes × 200 services and your query layer is unusable.

Convention names are deliberately **dotted, lowercase, and snake-case-free**:

```
GOOD: http.request.method, db.system, k8s.pod.name, network.peer.address
BAD : httpMethod, dbSystem, k8sPodName, peer_address
```

### 11.2 The most-used conventions

| Domain | Attributes | Stability |
|--------|------------|-----------|
| **HTTP server** | `http.request.method`, `http.response.status_code`, `http.route`, `url.path`, `url.full`, `server.address`, `server.port`, `network.protocol.version` | **Stable (1.23, Nov 2023)** |
| **HTTP client** | `http.request.method`, `http.response.status_code`, `url.full`, `server.address` | Stable |
| **gRPC** | `rpc.system=grpc`, `rpc.service`, `rpc.method`, `rpc.grpc.status_code` | Stable |
| **Database** | `db.system`, `db.namespace`, `db.collection.name`, `db.operation.name`, `db.query.text` | Stable (1.30, May 2024) |
| **Messaging** | `messaging.system`, `messaging.destination.name`, `messaging.operation`, `messaging.kafka.partition` | Stable for core; Kafka/RabbitMQ variants in dev |
| **K8s** | `k8s.cluster.name`, `k8s.namespace.name`, `k8s.pod.name`, `k8s.deployment.name`, `k8s.node.name`, `k8s.container.name` | Stable |
| **Cloud** | `cloud.provider`, `cloud.region`, `cloud.account.id`, `cloud.availability_zone` | Stable |
| **Service** | `service.name`, `service.namespace`, `service.instance.id`, `service.version` | Stable |
| **GenAI** (experimental) | `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.prompt_tokens`, `gen_ai.usage.completion_tokens` | Experimental |

The HTTP semconv stabilization in November 2023 was a big deal: many fields got renamed (`http.method` → `http.request.method`, `http.status_code` → `http.response.status_code`). Your dashboards, queries, and alert rules need to handle both during the transition. The migration window for HTTP semconv is still active in many fleets in 2026.

### 11.3 schema_url and versioning

Every batch exports a `schema_url` like `https://opentelemetry.io/schemas/1.27.0`. The receiving side can use this to know which version of the convention applies and (in principle) apply automatic transformations to normalize old → new names. In practice, almost no backend implements automatic translation; you handle the migration via collector `transform` rules.

```yaml
processors:
  transform:
    error_mode: ignore
    trace_statements:
      - context: span
        statements:
          # Old-style HTTP semconv → new style
          - set(attributes["http.request.method"], attributes["http.method"]) where attributes["http.method"] != nil
          - delete_key(attributes, "http.method") where attributes["http.request.method"] != nil
```

> **Pitfall:** Don't invent a custom attribute name if a convention exists. If you need `app.feature_flag`, fine — but `app.http.method` when `http.request.method` exists is technical debt-on-arrival.

---

## 12. Auto-instrumentation

The fastest way to get OTel coverage is to not write any code. OTel's auto-instrumentation strategies vary per language:

| Language | Mechanism | Coverage | Cost |
|----------|-----------|----------|------|
| **Java** | `-javaagent:opentelemetry-javaagent.jar` (bytecode rewriting via Byte Buddy) | ~90 frameworks (Spring, JDBC, Kafka, Redis, etc.) | ~5–20 MB heap, ~5% CPU |
| **.NET** | CLR profiler (`OpenTelemetry.AutoInstrumentation`) | ASP.NET Core, EF Core, gRPC, HttpClient | similar overhead |
| **Python** | `opentelemetry-instrument` (monkey-patches via `wrapt`) | requests, urllib3, Django, Flask, SQLAlchemy, psycopg2, asyncio... | per-call ~1µs |
| **Node.js** | `@opentelemetry/auto-instrumentations-node` (require-hook patches) | http, express, fastify, mysql, mongodb, redis... | per-call ~1µs |
| **Ruby** | `opentelemetry-instrumentation-all` (monkey-patch) | Rails, Sinatra, Faraday, ActiveRecord | similar |
| **Go** | `go.opentelemetry.io/auto` (eBPF + uprobes, since v0.10 in 2024) | net/http, database/sql, gRPC | needs kernel ≥5.4 + privileged init |

### 12.1 The Kubernetes Operator pattern

The OTel Operator (`opentelemetry-operator`) automates auto-instrumentation injection in K8s:

```yaml
apiVersion: opentelemetry.io/v1alpha1
kind: Instrumentation
metadata:
  name: java-instrumentation
  namespace: payments
spec:
  exporter:
    endpoint: http://otel-agent.observability:4318
  propagators: [tracecontext, baggage, b3]
  sampler: { type: parentbased_traceidratio, argument: "0.1" }
  java: { image: ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-java:1.32.0 }
  python: { image: ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-python:0.43b0 }
```

```yaml
# In the application Deployment
metadata:
  annotations:
    instrumentation.opentelemetry.io/inject-java: "true"
```

The operator's admission webhook intercepts pod creation, mutates the spec to add an init container that copies the Java agent, sets `JAVA_TOOL_OPTIONS=-javaagent:/otel-auto-instrumentation/javaagent.jar`, and the app starts up with auto-instrumentation. Zero code change.

### 12.2 Auto vs manual: when to mix

Auto-instrumentation gives you ~80% coverage of your dependencies (HTTP, RPC, DB, queue) for free. The remaining 20% — your business-domain spans (`payment.authorize`, `inventory.reserve`) — needs manual code.

**Almost every production stack uses both.** Auto for the boilerplate, manual for the spans that actually narrate your business logic. The auto SDK's tracer is the same one your manual `tracer.Start()` uses; they coexist transparently.

> **Pitfall:** Auto-instrumentation often generates *too many* spans. Every Redis `GET`, every JDBC call. At a few hundred spans per request, your trace store cost balloons. Either disable specific auto-instrumentation packages, or use SDK sampling more aggressively, or use views/filters to drop the boring spans at the collector.

---

## 13. Performance characteristics

Real numbers, measured across multiple SDKs on commodity hardware (cloud VM, Go 1.22, Java 21, Python 3.12; absolute numbers vary, ratios are stable).

| Operation | Approx cost | Notes |
|-----------|-------------|-------|
| `tracer.Start()` (sampled) | ~1 µs (Go), ~5 µs (Java), ~20 µs (Python) | Allocates span, attaches to ctx |
| `tracer.Start()` (dropped, AlwaysOff) | ~50 ns (Go), ~200 ns (Java) | Returns no-op span |
| `span.SetAttribute()` | ~50–200 ns per call | Hashmap insert |
| `span.End()` | ~500 ns | Enqueues for batch processor |
| `Counter.Add()` | ~50 ns (Go), ~100 ns (Java), ~500 ns (Python) | Atomic add to attribute-keyed cell |
| `Histogram.Record()` (explicit) | ~200 ns | Bucket lookup + atomic add |
| `Histogram.Record()` (exponential) | ~300 ns | Log2 + bucket scale logic |
| `OTLP serialize 512 spans` | ~3 ms | One-shot per export |
| `OTLP send (gRPC)` | dominated by network | Async; doesn't block app |

### 13.1 The "synchronous export" anti-pattern

Every OTel SDK ships with a `SimpleSpanProcessor` (and `SimpleLogRecordProcessor`) that exports synchronously on every span end. **It is for testing only.** Using it in production:

- adds the export RPC's latency (≥1ms) to every span.End() call
- couples your application's request latency to your collector's availability
- amplifies a momentary collector slowdown into a fleet-wide latency spike

The production processor is `BatchSpanProcessor` (and equivalents). It owns:

- A bounded queue (default 2048 spans)
- A worker goroutine/thread that drains the queue every `ScheduledDelay` or when batch_size is reached
- Drop-on-overflow with a counter — never block the application

```go
// WRONG (don't do this in prod)
tp := trace.NewTracerProvider(trace.WithSyncer(exp))

// RIGHT
tp := trace.NewTracerProvider(trace.WithBatcher(exp,
    trace.WithMaxQueueSize(2048),
    trace.WithBatchTimeout(5*time.Second),
    trace.WithMaxExportBatchSize(512),
))
```

### 13.2 Backpressure

When the collector is slow (disk full, downstream timeout), the SDK's queue fills. The default behavior is **drop new spans** — the SDK exposes a counter (`otelsdk_span_processor_dropped`) so you can alert. The app keeps running.

The wrong thing is to raise queue size to "absorb the spike" — at 100k queued spans × 500 bytes you're at 50 MB of in-process buffer per service, just for spans. Fix the collector or fix the network; don't make the app the buffer.

### 13.3 Attribute cardinality and memory

Each unique attribute *value* on a metric grows the SDK's internal map by one entry. Bad:

```go
counter.Add(ctx, 1, attribute.String("user_id", userID))  // unbounded
```

The SDK's cardinality limit (default 2000) catches this, but only after 2000 distinct values. Fix at code review, not at the SDK.

For traces, each unique attribute *key* costs serialization bytes per span; values can be high-cardinality without exploding cost (each span is its own row), but you still pay storage. The trace store is much more forgiving than the metric SDK here.

---

## 14. Common production patterns and pitfalls

The list a senior engineer expects you to know.

1. **Enrich with K8s metadata at the collector, not the SDK.** The SDK doesn't know its node, deployment, or replica set; the K8s API does. Use `k8sattributesprocessor` in the collector, fed by a service account with read access to pods.

2. **`service.name` should be the *application name*, not the K8s deployment.** In one app deployed to 5 namespaces, `service.name=checkout` everywhere; `k8s.namespace.name` distinguishes envs. Otherwise your dashboards split a single service into five.

3. **Never propagate `baggage` across trust boundaries.** A user-controllable browser can set `baggage:` headers; if your gateway forwards them blindly, internal services start trusting user-supplied tenancy. Strip at the edge; re-inject the trusted version.

4. **Don't put high-cardinality attributes on metrics.** `user_id`, `request_id`, `session_id` belong on spans and logs. The SDK's 2000-cardinality limit will silently overflow your metric into one bucket if you don't.

5. **Tail-sample on errors AND on P99 latency, not just one.** Errors-only misses the slow-but-successful traces that explain the SLO burn. Latency-only misses the rare 4xx that takes 10ms but matters.

6. **The "log every span" anti-pattern.** Some teams emit a structured log line for every span event. At 30 spans/request × 1k RPS = 30k log lines/sec — your log bill 4× the trace bill. Logs and traces are different signals; don't duplicate.

7. **Conflicting Resource attributes between agent enrichment and SDK.** SDK emits `service.name=payments`; collector's `resource` processor sets `service.name=cart`. Last-write-wins; non-deterministic. Pick one source.

8. **Exporting metrics with delta temporality to a Prometheus backend.** Prometheus expects cumulative; the negative deltas show up as resets. Match temporality to backend; the SDK's `temporality_selector` is your tool.

9. **Forgetting to flush on shutdown.** A process that exits without `tp.Shutdown(ctx)` loses its in-flight batches. Always call shutdown on SIGTERM, with a timeout.

10. **Trusting the collector to deduplicate.** If two collectors receive the same span (e.g., agent failover), neither dedupes. The trace store usually accepts duplicates and the UI hides them, but spanmetrics-derived RED metrics double-count. Use exactly-once semantics at the agent (one path per span).

11. **Mixing W3C and B3 propagation across boundaries.** A service that only knows B3 receives `traceparent` but no `b3:` — the trace fragments. Use the `Composite` propagator everywhere.

12. **OTLP/HTTP/JSON in production.** It works; it's just 3× the bytes for no benefit. Use it only for browser RUM where binary protobuf is awkward.

13. **Large attribute values.** A 1 MB SQL statement attribute on every span is a multi-GB/day blow-up. The SDK truncates by default at 4096 chars; tune `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`. Don't disable.

14. **No `service.instance.id`.** Without it, multiple replicas of the same service look like one entity in metrics — you can't see "instance 3 is slow." Set `service.instance.id` to the pod name (downward API).

15. **Span duration > 1 hour.** The SDK keeps un-ended spans in memory. A bug that forgets to call `span.End()` in an error path is a slow memory leak. Recording-only spans + a "max age" enforcement at the collector help.

---

## 15. Migrating to OTel

A real-world migration for an organization with 200+ services takes **6–18 months**. Doing it as a big-bang is malpractice.

### 15.1 Strangler-fig: collector first

```
Phase 0: Existing state.
   App → Datadog SDK → Datadog
   App → Prometheus client → Prometheus

Phase 1: Collector-as-translator. (~2 weeks)
   App → Datadog SDK → Datadog       (unchanged)
   App → Prometheus client → Prometheus (unchanged)
   ALSO:
   App (new services) → OTel SDK → Collector → Datadog (via datadog exporter)
                                            → Prometheus (via remote_write)

Phase 2: Collector-as-bus. (~2 months)
   All new services emit OTLP to the collector.
   Collector fans out to existing backends; no app re-instrumentation needed yet.
   You can now swap a backend by changing collector YAML, not app code.

Phase 3: SDK migration, service by service. (~6–12 months)
   Replace Datadog SDK / Prometheus client with OTel SDK in old services
   on a normal refactor cadence. Each migration is a 1–2 day PR.

Phase 4: Vendor change. (when budget says)
   Add a new backend (e.g., Tempo + Mimir) as an additional exporter.
   Run dual-write for 30 days; verify dashboards/alerts work on both.
   Remove the old vendor exporter.
```

Why this works: **Phase 1 alone gives you the OTel option without rewriting anything.** The collector translates OTLP-in to vendor-out. New code uses OTel; old code doesn't have to. You buy time to migrate without a freeze.

> **Mental model:** Migration is not "rip out one SDK, plug in another." It's "introduce a translator layer; flip producers and consumers independently." The collector is that translator.

---

## 16. What this chapter intentionally does not cover

This chapter has been about *what OTel is* and *why it's shaped the way it is*. The neighboring chapters go further:

- **`doc 03` Instrumentation.** Per-language idioms, custom span design, propagation across thread/coroutine/async-local boundaries, common patterns (HTTP middleware, RPC interceptors, DB drivers).
- **`doc 04` Collection & edge processing.** Operating the collector at scale: HA topology, k8s deployment, autoscaling, tail-sampling sharding, the `loadbalancing` exporter, when to put Kafka in front of the collector, debugging a hot collector replica.
- **`doc 06` Metrics storage.** Where the OTLP metric stream lands: Prometheus TSDB internals, Mimir's distributor/ingester architecture, exemplar storage, native-histogram support.
- **`doc 07` Logs storage.** Loki's label-index design, ClickHouse-on-logs, full-text vs label-only architectures, the cost equation.
- **`doc 08` Trace storage.** Tempo's index-less architecture, Jaeger's Cassandra/ES schema, ClickHouse as a span store, service-graph derivation.
- **`doc 09` Profiling.** OTel profiles (still experimental), pprof format, eBPF-based continuous profiling, symbolization.

If you want to know *how to use* OTel, this chapter is enough. If you want to know *how to operate* the resulting telemetry pipeline at scale, keep reading.

---

## TL;DR

> **OpenTelemetry = API + SDK + OTLP + Collector + Semantic Conventions.** Five pieces, decoupled by design. The API is what libraries depend on (no SDK in libs!); the SDK is what `main()` registers; OTLP (protobuf over gRPC, port 4317) is the wire format; the collector is a YAML pipeline that translates and routes; the semantic conventions make attribute names cross-language-portable.
>
> **Context propagation is the load-bearing piece.** W3C `traceparent` (16-byte trace_id, 8-byte span_id, 1-byte flags). The sampled bit governs whether downstream services keep the trace. `ParentBased(TraceIDRatioBased(0.1))` is the default-correct sampler.
>
> **Metrics gotchas: cumulative vs delta temporality, exponential vs explicit-bucket histograms, cardinality limit at 2000.** Get these wrong and dashboards lie.
>
> **Always run a collector.** Direct SDK → vendor coupling kills your portability story. Run agent + gateway. Use `tail_sampling` for trace economy, `redaction` for PII, `k8sattributes` for enrichment.
>
> **Migration is collector-first, SDK-second.** Get OTLP into the collector translating to your existing backend; then migrate SDKs at refactor pace; then swap backends with a YAML change.
>
> **Performance: ~1µs per sampled span, ~50ns per counter increment, async batching always.** Synchronous export is a production-incident waiting to happen.

The next doc (`03 — Instrumentation`) takes this foundation and shows what well-instrumented Go, Java, Python, and Node code actually looks like — and what bad instrumentation does to your bill.

---

## Sources

- [OpenTelemetry Specification (v1.33+)](https://github.com/open-telemetry/opentelemetry-specification)
- [OTLP Protocol Specification](https://github.com/open-telemetry/opentelemetry-proto)
- [OTel Collector — Architecture](https://opentelemetry.io/docs/collector/architecture/)
- [W3C Trace Context Recommendation](https://www.w3.org/TR/trace-context/)
- [W3C Baggage](https://www.w3.org/TR/baggage/)
- [Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/)
- [OTel Operator (K8s)](https://github.com/open-telemetry/opentelemetry-operator)
- [tail_sampling processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/tailsamplingprocessor)
- [k8sattributes processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/k8sattributesprocessor)
- [Exponential Histograms — OTEP 0149](https://github.com/open-telemetry/oteps/blob/main/text/0149-exponential-histogram-aggregation.md)
- [HTTP semantic conventions stabilization (Nov 2023)](https://opentelemetry.io/blog/2023/http-conventions-declared-stable/)
