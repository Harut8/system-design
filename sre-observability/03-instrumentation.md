# 03 — Instrumentation

> Where signals are *born*. Every later layer (collection, storage, query, alert) is polishing whatever this layer produces. If a counter is missing, mislabeled, or merely "kind of right," no Grafana panel and no PromQL trick downstream can repair it. This is the chapter where most teams' observability quality is actually decided — usually by accident, in a 50-line PR titled `add metrics`.

This document is about **instrumentation practice**, not SDK internals. Doc 02 already covers the OpenTelemetry SDK mechanics (resource detection, exporter pipelines, batch span processor, OTLP wire format). Here we focus on the questions a Staff Engineer answers in code review: *what* to instrument, *where* in the code, *how* to label it, and the surprisingly long list of *what not* to instrument. We cover all four signals (metrics, logs, traces, profiles) plus the boundary signals (RUM, mesh, eBPF) that don't fit cleanly in any one of them.

If you only ever take three rules out of this chapter:

1. Instrument the **boundary**, not the internals — every retry, fallback, queue handoff, and timeout deserves a counter and a span.
2. Keep metric **cardinality bounded** at instrumentation time — never put `user_id`, `request_id`, or any unbounded value in a label.
3. Carry **`trace_id` through every signal** — a log line without `trace_id` is a stranded artifact.

---

## Table of Contents

1. [The Instrumentation Hierarchy](#1-the-instrumentation-hierarchy)
2. [What to Instrument: Golden Signals + RED + USE](#2-what-to-instrument-golden-signals--red--use)
3. [Metrics Instrumentation Patterns](#3-metrics-instrumentation-patterns)
4. [Logging Instrumentation Patterns](#4-logging-instrumentation-patterns)
5. [Tracing Instrumentation Patterns](#5-tracing-instrumentation-patterns)
6. [Profiling Instrumentation Patterns](#6-profiling-instrumentation-patterns)
7. [Auto-Instrumentation vs Manual](#7-auto-instrumentation-vs-manual)
8. [Library and SDK Design Considerations](#8-library-and-sdk-design-considerations)
9. [eBPF as Instrumentation](#9-ebpf-as-instrumentation)
10. [Frontend / Mobile / RUM Instrumentation](#10-frontend--mobile--rum-instrumentation)
11. [Database / Data-Pipeline Instrumentation](#11-database--data-pipeline-instrumentation)
12. [Service Mesh as Instrumentation](#12-service-mesh-as-instrumentation)
13. [The "Instrument the Boundary" Principle](#13-the-instrument-the-boundary-principle)
14. [PII, Redaction, and Compliance at the Instrumentation Layer](#14-pii-redaction-and-compliance-at-the-instrumentation-layer)
15. [Versioning and Rollout Discipline](#15-versioning-and-rollout-discipline)
16. [A Complete Walked Example](#16-a-complete-walked-example)
17. [Anti-Patterns Checklist](#17-anti-patterns-checklist)
18. [What This Chapter Does Not Cover](#18-what-this-chapter-does-not-cover)

---

## 1. The Instrumentation Hierarchy

Signals can be produced at four very different levels of the system. Each level has different access to context (kernel state vs application semantics), different overhead, and different cost to maintain. A mature observability practice uses *all four*, picking each per signal based on what's cheapest to acquire at sufficient fidelity.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Level 4 — APPLICATION CODE                                               │
│   Manual spans, business metrics, audit logs                             │
│   Owners: feature engineers                                              │
│   Access:  full domain context (user, tenant, feature flag, $$ amount)   │
│   Cost:    code change per signal; high human attention                  │
│   Examples: span("checkout.apply_promo"); counter("orders_paid_total")   │
└──────────────────────────────────────────────────────────────────────────┘
                                   ▲
┌──────────────────────────────────────────────────────────────────────────┐
│ Level 3 — FRAMEWORK / AUTO-INSTRUMENTATION                               │
│   gRPC interceptor, Express middleware, Spring AOP, ASP.NET filters      │
│   Owners: platform team picks the package; service team installs it     │
│   Access:  request shape (method, path, status), no business semantics   │
│   Cost:    one-time setup; near-zero per-feature                         │
│   Examples: opentelemetry-instrumentation-fastapi; @opentelemetry/grpc   │
└──────────────────────────────────────────────────────────────────────────┘
                                   ▲
┌──────────────────────────────────────────────────────────────────────────┐
│ Level 2 — RUNTIME                                                        │
│   Go runtime/metrics, JVM JFR, .NET EventCounters, Python sys.monitoring │
│   Owners: platform / language SIGs                                       │
│   Access:  GC, scheduler, allocator, threadpool, fd count                │
│   Cost:    enable a flag; emits at fixed interval                        │
│   Examples: go_gc_duration_seconds; jvm_gc_pause; dotnet.gc.heap_size    │
└──────────────────────────────────────────────────────────────────────────┘
                                   ▲
┌──────────────────────────────────────────────────────────────────────────┐
│ Level 1 — HARDWARE / KERNEL                                              │
│   eBPF, perf_event, NVML/DCGM (GPU), PMU, /proc, cAdvisor                │
│   Owners: infra / platform                                                │
│   Access:  syscalls, packets, TCP retransmits, page faults, GPU SM       │
│   Cost:    one agent per node; no app change                             │
│   Examples: tcp_retrans_total; oom_kill_total; DCGM_FI_PROF_SM_ACTIVE    │
└──────────────────────────────────────────────────────────────────────────┘
```

The key trade-off: **information moves up, overhead moves down**. Hardware/kernel signals are nearly free per app but blind to "did the user actually finish checkout?" Application signals know everything about the user but cost a code change per question.

| Level | Ease | Precision | Overhead | Owns | Best for |
|---|---|---|---|---|---|
| 1 — Hardware/kernel | Hard (kernel coupling) | Coarse to medium | Very low | Infra/platform | "Is the box on fire?" — TCP retransmits, OOMs, GPU temp, page faults |
| 2 — Runtime | Easy (flag) | Medium | Low | Platform | GC pauses, threadpool starvation, heap size, scheduler latency |
| 3 — Framework auto | Easy (lib) | Medium-high | Low | Service team | RED metrics + spans for ingress/egress and DB clients |
| 4 — Application | Slow (code) | Highest (domain) | Per-feature human cost | Feature team | Business semantics, derived SLIs, audit events |

> **Mental model:** When asked "should we add a metric?" the right reflex is *which level should produce it?* If the question is "is the GC stalling?" the answer is level 2 (runtime), and writing a level-4 manual gauge is wrong. If the question is "did the user convert from free to paid?" only level 4 has the context. Most observability anti-patterns are level mismatches — manually emitting metrics that the runtime already produces, or trying to read business state from eBPF.

> **Pitfall:** Auto-instrumentation looks like it covers level 4 because it produces lots of spans. It does not. It covers ingress/egress and infrastructure clients. Domain semantics — "the cart total exceeded the credit limit" — are invisible to it. See §7.

The rest of this chapter walks each signal class (metrics, logs, traces, profiles) and shows what level produces it, what to put in it, and what to keep out.

---

## 2. What to Instrument: Golden Signals + RED + USE

The roadmap restated the three classical frames. Here is the concrete translation: for each common service shape, what counters, gauges, and histograms should always exist before the service ships to production.

### 2.1 The frames in one paragraph

- **Four Golden Signals** (Google SRE): **latency, traffic, errors, saturation**. Per *user-visible* surface.
- **RED** (Tom Wilkie): **rate, errors, duration**. Per *service*. Subset of golden signals; ignores saturation.
- **USE** (Brendan Gregg): **utilization, saturation, errors**. Per *resource* (CPU, disk, NIC, GPU). Doesn't measure user-facing latency.

In practice you instrument **RED at every service** and **USE at every resource** and they meet at the saturation column. Latency is the SLI; the rest is diagnosis.

### 2.2 Minimum viable instrumentation per service shape

This is the table to print and tape to the desk. Every service must emit at least these signals before being eligible for an SLO.

| Service shape | Counters | Histograms | Gauges | Logs | Spans |
|---|---|---|---|---|---|
| **HTTP server** | `http_requests_total{method,route,status_class}` | `http_request_duration_seconds{method,route}` (SLO buckets); `http_response_size_bytes` | `http_inflight_requests` | request log w/ `trace_id`, `route`, `status`, `latency_ms`, `user_id`(attr) | `SERVER` span per request |
| **gRPC server** | `grpc_server_handled_total{method,code}` | `grpc_server_handling_seconds{method}` | `grpc_inflight_streams` | as HTTP, plus `grpc.method`, `grpc.code` | `SERVER` span; nested `INTERNAL` for handlers |
| **gRPC client / outbound HTTP** | `*_client_requests_total{peer,method,code}`; `*_client_retries_total{peer,reason}` | `*_client_duration_seconds` | `*_client_inflight` | call log on error and slow path | `CLIENT` span per call |
| **Async consumer (Kafka/RMQ/SQS)** | `messages_consumed_total{topic,result}`; `messages_dlq_total{topic,reason}`; `consumer_lag_messages{topic,partition}` | `message_processing_seconds{topic}`; `message_e2e_latency_seconds` (produce→ack) | `consumer_active_workers`; `consumer_paused` | per-message log on error or sample | one span per message; `link` to producer trace |
| **Batch / cron job** | `job_runs_total{job,result}`; `job_records_processed_total{job}` | `job_duration_seconds{job}` | `job_last_success_timestamp`; `job_records_in_flight` | structured begin/end + per-error | one root span per run; child spans per stage |
| **Scheduled worker / poller** | `poll_attempts_total{result}`; `poll_records_total{kind}` | `poll_interval_drift_seconds`; `poll_processing_seconds` | `poll_last_success_timestamp` | begin/end per cycle | one root span per cycle |
| **WebSocket service** | `ws_connections_total{result}`; `ws_messages_in_total{kind}`; `ws_messages_out_total{kind}` | `ws_session_duration_seconds`; `ws_message_processing_seconds` | `ws_active_connections`; `ws_active_subscriptions` | connect/disconnect/error events | one span per connection (long); child spans per inbound message |
| **DB client (per pool)** | `db_queries_total{op,table,result}` | `db_query_duration_seconds{op,table}` | `db_pool_acquired`; `db_pool_idle`; `db_pool_max`; `db_pool_wait` | slow-query + error logs | `CLIENT` span per query |
| **Cache client** | `cache_ops_total{op,result=hit\|miss\|error}` | `cache_op_duration_seconds{op}` | `cache_hit_ratio` (recording rule, not gauge) | error logs only | optional span per op (sample) |
| **Internal queue (in-process)** | `inproc_queue_enqueues_total`; `inproc_queue_drops_total{reason}` | `inproc_queue_wait_seconds` | `inproc_queue_depth` | drop events | none |

Notes on this table that matter:

- Every counter ends in `_total`. Every histogram ends in `_seconds` or `_bytes` (units in the name; this is OTel convention and Prometheus best practice).
- `status_class` is `2xx/3xx/4xx/5xx`, **not** `status`. The full status code goes in logs/spans, not metric labels — see §3.4 on cardinality.
- `route` is the *templated* path (`/users/{id}`), never the raw URL. Raw URLs are unbounded cardinality.
- Saturation gauges (`*_inflight`, `*_pool_acquired`) are non-negotiable. Without them, the alert "we have a queueing problem" is invisible until users complain.
- "Consumer lag" gets two metrics: count (offset distance) and time (how stale is the head we're processing). See §11.

> **Mental model:** Every service is a function from inputs to outputs. RED measures the function (rate, errors, duration). USE measures the resources it consumes (utilization, saturation, errors). If you lack RED, you can't see your users. If you lack USE, you can't see your future scaling cliff. Both, always.

---

## 3. Metrics Instrumentation Patterns

Metrics are the cheapest signal to operate at scale (a few bytes per scrape, regardless of traffic) and the most expensive to get wrong (a bad label rolls forward forever; alerts misfire for years). This section is about *correct* metric instrumentation.

### 3.1 Counter vs gauge vs histogram vs summary

| Type | Semantics | When to use | When NOT |
|---|---|---|---|
| **Counter** | Monotonically increasing total since process start | Anything you'll `rate()` over: requests, errors, bytes sent | If the value can decrease (use gauge) |
| **Gauge** | Snapshot of a value that goes up and down | In-flight requests, queue depth, pool size, last-success timestamp | If you want averages — use histogram instead |
| **Histogram** | Bucketed counts of observed values | Latency, request size, response size, queue wait | If you only want one quantile and budget is tight (rare) |
| **Summary** | Client-side computed quantiles (p50, p95, p99) | Almost never. Aggregation is broken across instances. | Whenever you have more than one replica |

> **Pitfall:** Summaries (Prometheus client `Summary`, OTel `_summary` if you ever see one) compute quantiles **per instance**, then expose them as gauges. You **cannot meaningfully aggregate** the p99 across 50 replicas — the average of p99s is not the fleet p99. Histograms aggregate correctly because the bucket *counts* sum. Use histograms. The only legitimate use for summaries is single-instance debugging where you know exactly what's running.

### 3.2 The histogram bucket tax

Histograms cost N+1 series per (metric, label set) combination, where N is the number of buckets. Choosing buckets is therefore a cardinality decision, not just a precision decision.

The bucket selection rule:

1. **Span the SLO.** If your SLO says "p99 < 500ms," you need a bucket boundary at 500ms (otherwise `histogram_quantile(0.99, ...)` interpolates across a coarse bucket and lies). Common SLO grid: `[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]` seconds.
2. **Don't use the client default unless your SLO matches it.** Prometheus Go client's default histogram buckets target 1ms–10s. If your service is sub-millisecond, the default puts everything in the bottom bucket — useless.
3. **More buckets ≠ better quantile.** Each extra bucket is +1 series × cardinality of all other labels. 10 buckets × 5 routes × 4 status_class = 200 series. 30 buckets × same = 600. Audit annually.
4. **Native histograms (Prometheus 2.40+ / OTel exponential histograms)** sidestep the budget — they auto-grow buckets logarithmically and serialize as a sparse structure. Prefer them for new instrumentation if your storage supports it.

```go
// Go: SLO-aligned buckets for an HTTP service with 500ms SLO
var (
    httpDuration = prometheus.NewHistogramVec(
        prometheus.HistogramOpts{
            Name:    "http_request_duration_seconds",
            Help:    "Server-side HTTP request duration",
            // Buckets straddle 500ms (the SLO) and 1s, 2.5s for tail.
            // No bucket below 5ms — we don't ship that fast.
            Buckets: []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5},
        },
        []string{"method", "route", "status_class"}, // 4 × ~20 × 4 = ~320 buckets+1, ok.
    )
)
```

### 3.3 Exemplars: the 10× debugging upgrade

An **exemplar** is a single sample attached to a histogram bucket that carries a `trace_id`. When the metric panel shows a latency spike, you click the exemplar dot and jump straight into the slow trace. No grep. No guess.

Exemplars are cheap (one per bucket per scrape, reservoir-replaced) and turn the metric → trace handoff from "search Loki by timestamp" into "click the dot." Prometheus, OTel, and most modern stores (Mimir, Tempo, Honeycomb) support them.

```go
// Go: emit an exemplar from inside a request handler.
import (
    "go.opentelemetry.io/otel/trace"
    "github.com/prometheus/client_golang/prometheus"
)

func recordLatency(ctx context.Context, route string, statusClass string, d time.Duration) {
    obs := httpDuration.WithLabelValues("POST", route, statusClass)
    if sc := trace.SpanContextFromContext(ctx); sc.HasTraceID() {
        // Histogram exemplars require the ExemplarObserver interface.
        obs.(prometheus.ExemplarObserver).ObserveWithExemplar(
            d.Seconds(),
            prometheus.Labels{"trace_id": sc.TraceID().String()},
        )
        return
    }
    obs.Observe(d.Seconds())
}
```

```python
# Python: prometheus_client + OTel — exemplar from the active span.
from prometheus_client import Histogram
from opentelemetry import trace

H = Histogram(
    "http_request_duration_seconds",
    "Server-side HTTP request duration",
    ["method", "route", "status_class"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)

def record(method, route, status_class, seconds):
    span = trace.get_current_span()
    sc = span.get_span_context() if span else None
    exemplar = {"trace_id": format(sc.trace_id, "032x")} if sc and sc.is_valid else {}
    (H.labels(method, route, status_class)
       .observe(seconds, exemplar=exemplar or None))
```

> **Diagnostic patterns:** Click the dot in Grafana → land on a single trace whose duration was 740ms in the spike at 14:32. The whole metric → trace → log triangle is one click each, because every signal carries the trace_id.

### 3.4 Cardinality: the only knob that matters

A metric's cost is the number of unique `(metric, label_set)` series it produces. Series count drives memory, ingest CPU, and storage cost roughly linearly, and query cost roughly linearly. **Series count is the budget.**

Practical rules, in order of strictness:

1. **Never put unbounded-domain values in labels.** No `user_id`, `request_id`, `customer_id`, `session_id`, `email`, `ip`, raw `path`, `error_message`, `query_text`, `commit_sha`. These belong in **logs and span attributes**, where they cost a few bytes per record, not a series for life.
2. **Keep label dimensions ≤ ~10 per metric**, and the cartesian product ≤ ~100k for a single metric (rule of thumb, varies by storage budget). Audit with `topk(50, count by (__name__) ({}))`.
3. **Bucket high-cardinality dimensions.** Instead of `customer_id`, emit `customer_tier="free|paid|enterprise"`. Instead of `error_message`, emit `error_class="timeout|conn_reset|5xx|4xx"`.
4. **Prefer status_class over status_code.** 5 × 4 × 20 = 400 vs 5 × 60 × 20 = 6,000.
5. **Pre-flight cardinality in CI.** A test that loads the binary, hits every endpoint, and counts `len(prometheus.Gather())` will catch most explosions before they ship.

```python
# Python: this is wrong (kills cardinality).
REQUESTS = Counter("http_requests_total", "...",
                   ["method", "route", "status", "user_id"])  # NO

# Right.
REQUESTS = Counter("http_requests_total", "...",
                   ["method", "route", "status_class"])
# user_id goes in logs and span attrs, not labels.
```

> **Pitfall:** "We'll just add `tenant_id` as a label, it's only ~500 tenants" — until marketing onboards 200k SMB customers and your TSDB head block doubles overnight. The right answer is *aggregation by tenant happens at query time on logs/traces, or via a recording rule on a downsampled series.* See doc 18 for the budget enforcement story.

### 3.5 Worked example: counter + histogram + exemplar end-to-end

```go
// Go: a single instrumented inbound HTTP handler. Production-shaped.
var (
    reqs = prometheus.NewCounterVec(
        prometheus.CounterOpts{Name: "http_requests_total"},
        []string{"method", "route", "status_class"},
    )
    dur = prometheus.NewHistogramVec(
        prometheus.HistogramOpts{
            Name:    "http_request_duration_seconds",
            Buckets: []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5},
        },
        []string{"method", "route", "status_class"},
    )
    inflight = prometheus.NewGaugeVec(
        prometheus.GaugeOpts{Name: "http_inflight_requests"},
        []string{"route"},
    )
)

func instrument(route string, h http.HandlerFunc) http.HandlerFunc {
    return func(w http.ResponseWriter, r *http.Request) {
        start := time.Now()
        inflight.WithLabelValues(route).Inc()
        defer inflight.WithLabelValues(route).Dec()

        rec := &statusRecorder{ResponseWriter: w, status: 200}
        h(rec, r)

        sc := classify(rec.status) // "2xx" / "4xx" / "5xx"
        reqs.WithLabelValues(r.Method, route, sc).Inc()

        d := time.Since(start).Seconds()
        obs := dur.WithLabelValues(r.Method, route, sc).(prometheus.ExemplarObserver)
        if span := trace.SpanContextFromContext(r.Context()); span.HasTraceID() {
            obs.ObserveWithExemplar(d, prometheus.Labels{"trace_id": span.TraceID().String()})
        } else {
            obs.Observe(d)
        }
    }
}
```

That's the complete level-3-or-4 metric story for one inbound surface: counter, histogram with SLO-aligned buckets and exemplars, and a saturation gauge. Replicate this shape across every ingress.

---

## 4. Logging Instrumentation Patterns

Logs are events with structure. They are *cardinality-unbounded* (every line can have a unique `user_id`) and *retention-bounded* (most are useless after a week). The job of instrumentation is to make every log line **machine-readable, joinable to traces, and free of PII**.

### 4.1 Structured logging is non-negotiable

Every log line is a JSON object with stable keys. The line `2024-05-03 14:32:01 ERROR could not process user 42 because db down` is a debugging dead-end at scale. The same event as JSON:

```json
{
  "ts": "2024-05-03T14:32:01.123Z",
  "level": "error",
  "msg": "db.unavailable",
  "service": "checkout",
  "trace_id": "0af7651916cd43dd8448eb211c80319c",
  "span_id": "b7ad6b7169203331",
  "request_id": "req-91ad3",
  "user_id": "42",
  "db.system": "postgresql",
  "db.peer": "checkout-db-primary",
  "error.type": "ConnectionRefused"
}
```

…joins to a trace, filters to a tenant, aggregates by `error.type`, all in one query. Mandatory fields:

| Field | Source | Why |
|---|---|---|
| `ts` | logger | RFC3339Nano, UTC. The time of the event, not the time of ingest. |
| `level` | call site | `trace`/`debug`/`info`/`warn`/`error`/`fatal`. Standard 6. |
| `msg` | call site | A short, **stable** event identifier (`user.login`, `db.unavailable`). Not the prose. |
| `service` | resource attr | Set once at startup from `OTEL_SERVICE_NAME`. |
| `trace_id`, `span_id` | active span | Read from context per log call. Without these the log is stranded. |
| `request_id` | middleware | Optional if you have `trace_id`; useful for non-traced subsystems. |
| domain attrs | call site | `user.id`, `tenant.id`, `order.id`, `error.type`, `db.statement` (redacted). |

Use a fixed taxonomy from OTel's [semantic conventions](https://opentelemetry.io/docs/specs/semconv/) (`http.*`, `db.*`, `messaging.*`). Don't invent `userId` if `user.id` is already standardized — log queries break across teams when names disagree.

### 4.2 Per-language idioms

```go
// Go — slog (stdlib) with a JSON handler and OTel trace context.
import "log/slog"
import "go.opentelemetry.io/otel/trace"

logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
    Level: slog.LevelInfo,
})).With("service", "checkout")

func logCtx(ctx context.Context) *slog.Logger {
    sc := trace.SpanContextFromContext(ctx)
    if !sc.HasTraceID() {
        return logger
    }
    return logger.With(
        "trace_id", sc.TraceID().String(),
        "span_id", sc.SpanID().String(),
    )
}

logCtx(ctx).Error("db.unavailable",
    "db.system", "postgresql",
    "db.peer", peer,
    "error.type", classifyErr(err),
)
```

```python
# Python — structlog with OTel context.
import structlog
from opentelemetry import trace

def _otel(_, __, event_dict):
    sc = trace.get_current_span().get_span_context()
    if sc.is_valid:
        event_dict["trace_id"] = format(sc.trace_id, "032x")
        event_dict["span_id"]  = format(sc.span_id, "016x")
    return event_dict

structlog.configure(processors=[
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.add_log_level,
    _otel,
    structlog.processors.JSONRenderer(),
])

log = structlog.get_logger().bind(service="checkout")
log.error("db.unavailable", **{"db.system": "postgresql", "error.type": "ConnRefused"})
```

```java
// Java — log4j2 JSON layout with OTel auto-injecting trace_id via MDC.
// log4j2.xml uses <JsonTemplateLayout eventTemplateUri="classpath:LogstashJsonEventLayoutV1.json"/>
// OTel java agent populates MDC keys "trace_id" and "span_id" automatically.

private static final Logger log = LogManager.getLogger(CheckoutService.class);

log.atError()
   .withMarker(MarkerManager.getMarker("DB_UNAVAILABLE"))
   .log("db.unavailable",
        StructuredArguments.kv("db.system", "postgresql"),
        StructuredArguments.kv("db.peer", peer),
        StructuredArguments.kv("error.type", classify(e)));
```

```javascript
// Node — pino with OTel trace context as a mixin.
const pino = require("pino");
const { trace, context } = require("@opentelemetry/api");

const log = pino({
  base: { service: "checkout" },
  timestamp: pino.stdTimeFunctions.isoTime,
  formatters: { level: (label) => ({ level: label }) },
  mixin: () => {
    const span = trace.getSpan(context.active());
    if (!span) return {};
    const sc = span.spanContext();
    return { trace_id: sc.traceId, span_id: sc.spanId };
  },
});

log.error({ "db.system": "postgresql", "db.peer": peer, "error.type": classify(err) },
          "db.unavailable");
```

```csharp
// .NET — Serilog with OTel enricher.
Log.Logger = new LoggerConfiguration()
    .Enrich.WithProperty("service", "checkout")
    .Enrich.With<OpenTelemetryTraceEnricher>()           // adds TraceId, SpanId
    .WriteTo.Console(new CompactJsonFormatter())
    .CreateLogger();

Log.ForContext("db.system", "postgresql")
   .ForContext("db.peer", peer)
   .ForContext("error.type", Classify(e))
   .Error("db.unavailable");
```

### 4.3 Levels: what to log when

| Level | Default in prod? | Use for |
|---|---|---|
| `trace` | off | Per-statement debug, only when a debug flag is on for one request |
| `debug` | off | Local dev; behind a flag in prod (see §4.5 on tail-based sampling) |
| `info` | on | Events still interesting in two weeks (started, stopped, deployed, completed N orders) |
| `warn` | on | Recoverable degradation; will appear in the outage timeline if there is one |
| `error` | on | The user saw a failure, or a background job failed irrecoverably |
| `fatal` | on | The process is exiting |

> **Pitfall:** "Everything at `INFO`" is the most common log anti-pattern. The signal-to-noise ratio at incident time is what determines whether you mitigate in 5 minutes or 50, and an `INFO` flood is what killed it.

### 4.4 Logs are for events, metrics are for measurements

A common mistake: logging a value just so a log-based metric tool can extract it. If you want to measure latency, use a histogram. If you want to measure error rate, use a counter. Logs supplement metrics with **specifics** — *which* user hit the timeout, *which* db connection died — not aggregate quantities.

The split:

- **Metric** answers "how often / how slow / how many in flight."
- **Log** answers "what specifically happened in this one event."
- **Trace** answers "where in the call graph did this one request spend its time."

Counting `grep ERROR` to make a fleet error rate is fragile and expensive. Counting `errors_total{class="timeout"}` is correct.

### 4.5 Tail-based log sampling: log debug *only* if the request errored

A high-leverage pattern. In production, `DEBUG` is off — except when something went wrong, in which case you want every breadcrumb. Implement with a per-request **buffered logger** that flushes at the end of the request *only* if the outcome was an error or slow path.

```go
// Go: buffered debug logger. Flushes only on error.
type bufLogger struct {
    base   *slog.Logger
    buf    []slog.Record
    parent context.Context
}

func (b *bufLogger) Debug(msg string, attrs ...slog.Attr) {
    rec := slog.NewRecord(time.Now(), slog.LevelDebug, msg, 0)
    rec.AddAttrs(attrs...)
    b.buf = append(b.buf, rec)
}

func (b *bufLogger) Info(msg string, attrs ...slog.Attr) {
    b.base.LogAttrs(b.parent, slog.LevelInfo, msg, attrs...)
}

// Flush at request end:
func (b *bufLogger) Flush(ctx context.Context, errored bool) {
    if !errored { return } // drop all DEBUG on success
    for _, r := range b.buf {
        _ = b.base.Handler().Handle(ctx, r)
    }
}
```

You now pay log volume only when the request was interesting. Combined with trace tail sampling (doc 04), you keep ≈100% of *useful* signal at ≈5% of the cost.

### 4.6 PII redaction at instrumentation time

Redact at the **source**, not at the agent or store. By the time it's been written to a buffer, it has already been to disk somewhere (kernel buffers, container logs, replicated journal) and "we'll fix it at the agent" leaks through every gap.

The pattern:

- **Don't log the credit card.** Log `card.last4` and `card.bin`.
- **Don't log the email.** Log `user.email_hash` (HMAC with rotating key) or `user.id`.
- **Don't log the JWT.** Log `auth.subject`, never the bearer token.
- **Don't log SQL query parameters.** OTel `db.statement` should be the *parameterized* statement; values go to span attrs only if non-PII.
- **Allowlist, don't blocklist.** "Log only fields in this struct's `loggable=true` allowlist" is far safer than "redact things matching this regex." Allowlists fail closed.

```go
// Go: allowlist via struct tags.
type LoggableUser struct {
    ID    string `log:"user.id"`
    Email string `log:"-"`            // never logged
    Tier  string `log:"user.tier"`
}
```

A blocklist regex on the agent is the *last line of defense*, not the first.

---

## 5. Tracing Instrumentation Patterns

Traces answer "where did this request spend its time?" and "what did it call?" The dominant failure mode is *broken propagation* — one process forgets to forward `traceparent` and the trace truncates. The second-most-common failure is *over-instrumentation* — every internal function becomes a span and the trace UI becomes a wall of noise.

### 5.1 Span granularity

The rules of thumb:

- **One `SERVER` span per inbound request.** Started by middleware, ended by middleware. Always.
- **One `CLIENT` span per outbound RPC / DB query / cache call.** This is where 90% of useful spans live.
- **One `INTERNAL` span per *significant* in-process operation.** Significant = "I'd want to see this in a flamegraph." A complex pricing calc, a JSON deserialize on a 10MB payload, a batch loop. Not "every function."
- **`PRODUCER` and `CONSUMER` spans** for queue operations.
- **Span events** (not spans) for moments inside a span: "cache miss," "fallback engaged," "retry #2."

> **Mental model:** A trace should have on the order of **10–50 spans per request** for a typical web service. If yours has 5, you're underinstrumented at the outbound boundary. If yours has 500, you're treating every function as a span — delete most of them. The right granularity is "one span per network hop or per blocking call ≥1ms."

### 5.2 Attributes vs events

- **Attributes** describe the operation: `http.method=POST`, `http.route=/checkout`, `db.system=postgresql`, `db.statement="SELECT ..."`, `peer.service=pricing`.
- **Events** mark moments inside a span with their own timestamp: `cache.miss`, `retry.attempt`, `circuit.opened`, `fallback.engaged`.

Don't use a span where an event is right. A retry inside a single outbound call does not need three nested spans; it needs one `CLIENT` span with three `retry.attempt` events. The flamegraph stays readable.

### 5.3 Per-language idioms

```go
// Go — outbound HTTP with proper status, attributes, and events.
import "go.opentelemetry.io/otel"
import "go.opentelemetry.io/otel/attribute"
import "go.opentelemetry.io/otel/codes"

tracer := otel.Tracer("checkout/pricing-client")

func fetchPricing(ctx context.Context, sku string) (*Price, error) {
    ctx, span := tracer.Start(ctx, "pricing.GET",
        trace.WithSpanKind(trace.SpanKindClient),
        trace.WithAttributes(
            attribute.String("peer.service", "pricing"),
            attribute.String("sku", sku),
        ),
    )
    defer span.End()

    for attempt := 1; attempt <= 3; attempt++ {
        resp, err := httpc.Do(buildReq(ctx, sku))
        if err == nil && resp.StatusCode < 500 {
            span.SetAttributes(attribute.Int("http.status_code", resp.StatusCode))
            return decode(resp), nil
        }
        span.AddEvent("retry.attempt", trace.WithAttributes(
            attribute.Int("attempt", attempt),
            attribute.String("error.type", classifyErr(err)),
        ))
    }
    span.RecordError(errExhausted)
    span.SetStatus(codes.Error, "retries_exhausted")
    return nil, errExhausted
}
```

```python
# Python — DB call with parameterized statement and error recording.
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer("checkout/db")

def get_cart(conn, user_id: str):
    with tracer.start_as_current_span("db.query.cart_by_user",
                                      kind=trace.SpanKind.CLIENT) as span:
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.operation", "SELECT")
        span.set_attribute("db.sql.table", "cart")
        # parameterized; values are NOT in the statement.
        span.set_attribute("db.statement", "SELECT * FROM cart WHERE user_id = $1")
        span.set_attribute("user.id", user_id)
        try:
            return conn.execute("SELECT * FROM cart WHERE user_id = $1", user_id)
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, type(e).__name__))
            raise
```

```java
// Java — Spring AOP wrapping a payment call. RecordException + SetStatus.
@Around("@annotation(WithSpan)")
public Object trace(ProceedingJoinPoint pjp) throws Throwable {
    Span span = tracer.spanBuilder(pjp.getSignature().getName())
                      .setSpanKind(SpanKind.CLIENT)
                      .startSpan();
    try (Scope ignored = span.makeCurrent()) {
        span.setAttribute("peer.service", "payments");
        return pjp.proceed();
    } catch (Throwable t) {
        span.recordException(t);
        span.setStatus(StatusCode.ERROR, t.getClass().getSimpleName());
        throw t;
    } finally {
        span.end();
    }
}
```

```rust
// Rust — tracing crate + OTel layer. The macro inserts a span around the function.
#[tracing::instrument(skip(repo), fields(peer.service = "users", user.id = %id))]
async fn fetch_user(repo: &Repo, id: UserId) -> Result<User, Error> {
    match repo.get(id).await {
        Ok(u)  => Ok(u),
        Err(e) => {
            tracing::error!(error.type = %e.kind(), "fetch_user failed");
            Err(e)
        }
    }
}
```

The canonical error pattern across all of them: **`record_exception` + `set_status(ERROR)` + re-raise/propagate**. Two of three is wrong — recording without setting status leaves the span green in the UI; setting status without recording loses the stack trace.

### 5.4 Async propagation

Context propagation through threadpools, queues, and scheduled tasks is the single most-broken instrumentation pattern. The trace breaks when the context is implicit (`thread-local`) and the work moves to a thread that didn't inherit it.

The rules:

- **Capture context at handoff time, restore at execution time.** OTel SDKs all expose `Context.current()` and `attach(ctx)`.
- **Carry context through queue messages** as headers or message attributes. Use `TextMapPropagator.inject` on produce, `extract` on consume.
- **Use span links (not parent)** for fan-out and fan-in. If one Kafka message triggers 100 downstream operations, each gets its own root span linked to the producer's span.

```python
# Python — Kafka producer/consumer with W3C context propagation.
from opentelemetry.propagate import inject, extract
from opentelemetry import context, trace

def produce(producer, topic, payload):
    headers = {}
    inject(headers)                                  # writes traceparent into dict
    producer.send(topic, payload, headers=list(headers.items()))

def consume(msg):
    ctx = extract({k: v for k, v in msg.headers})    # read traceparent
    token = context.attach(ctx)
    try:
        with trace.get_tracer("worker").start_as_current_span(
            "consume", kind=trace.SpanKind.CONSUMER,
            links=[trace.Link(trace.get_current_span().get_span_context())],
        ):
            handle(msg.value)
    finally:
        context.detach(token)
```

```go
// Go — schedule a goroutine that inherits ctx (and therefore the span).
func enqueueAsync(ctx context.Context, item Work) {
    // capture
    snapshot := ctx
    go func() {
        // restore
        ctx, span := otel.Tracer("worker").Start(snapshot, "process.async",
            trace.WithSpanKind(trace.SpanKindInternal))
        defer span.End()
        process(ctx, item)
    }()
}
```

```java
// Java — Executor wrapped to propagate Context.
ExecutorService traced = Context.taskWrapping(Executors.newFixedThreadPool(8));
```

For Celery: use the `opentelemetry-instrumentation-celery` package, which auto-injects/extracts. For RabbitMQ: pass headers explicitly (no auto-instr for some clients). For SQS: encode `traceparent` in `MessageAttributes`.

### 5.5 Error recording is canonical

```python
# The one-liner you see in every codebase:
try:
    do_thing()
except Exception as e:
    span.record_exception(e)
    span.set_status(Status(StatusCode.ERROR, type(e).__name__))
    raise
```

If you remember nothing else about tracing instrumentation, remember this triple. Every span that *could* fail must follow it.

---

## 6. Profiling Instrumentation Patterns

Profiles answer "where in the code did the CPU/memory go?" Continuous profiling makes that answer queryable across time and commits, turning "is this perf regression real?" into a SQL query.

### 6.1 In-process vs system-wide

| Mode | Tools | Pros | Cons |
|---|---|---|---|
| **In-process** | Go `runtime/pprof`, JVM async-profiler / JFR, Python py-spy / pyroscope, Ruby rbspy, .NET dotnet-trace | Language-aware (knows function names, GC events); accurate symbols | Per-language; needs library; pays runtime cost |
| **System-wide eBPF** | Parca-agent, Pyroscope eBPF, Grafana Beyla, Polar Signals | Zero code change; covers all processes incl. C/Rust/Go without rebuild | Requires kernel ≥4.18 (BTF preferred); symbolization is harder; no managed-language internals (JVM frames need extra work) |

Production answer for a polyglot fleet: **eBPF agent on every node** for baseline CPU profiling, plus **in-process pprof endpoints** for languages where you want allocation/contention/block profiles (Go) or async stack-walking that eBPF can't do well.

### 6.2 Build flags that matter

Frame-pointer-based stack walking is far cheaper at sample time than DWARF. Compilers default to omitting frame pointers for one extra register, and that one register costs you flame graphs. Re-enable them.

| Language / runtime | Flag |
|---|---|
| Go | Frame pointers on by default since Go 1.7 (amd64). Use `-buildvcs=true` to embed VCS info; build with debug info for symbolization. |
| C / C++ / Rust | `-fno-omit-frame-pointer` and `-g`. Rust: `RUSTFLAGS="-Cforce-frame-pointers=yes -Cdebuginfo=2"`. |
| JVM | `-XX:+PreserveFramePointer`. Without it, async-profiler relies on AsyncGetCallTrace, which is fine, but eBPF cannot walk JVM frames. |
| .NET | Frame pointers on by default in .NET 6+. Symbols via `DOTNET_PerfMapEnabled=1`. |
| glibc | `-fno-omit-frame-pointer` propagates only if rebuilt; distros are increasingly shipping FP-enabled libc (Fedora 38+, Ubuntu 24.04). |

> **Pitfall:** A flame graph that looks like a tower of `[unknown]` frames is a symbolization or frame-pointer failure, not a bug in the profiler. Check `objdump -h binary | grep .eh_frame` and `readelf -h binary | grep "Frame pointer"`.

### 6.3 Symbolization

The data you sample is *addresses*. Turning them into function names requires:

1. The binary's **build ID** (`readelf -n binary | grep "Build ID"`).
2. Debug info, either embedded (`-g`) or via debuginfod / on-disk debug bundle keyed by build ID.
3. For dynamically-generated code (JVM, V8): a side-channel (perf-map files, JFR events, `--perf-basic-prof`) that maps JIT addresses to symbols.

Sentry-style **symbol uploads** (push DWARF tarballs to a server keyed by build ID) is the most operable approach in a polyglot fleet. Don't try to ship debug symbols inside production containers — they triple image size.

### 6.4 Profile labels: split flame graphs by tenant or feature flag

Go's `pprof.Labels` lets you tag samples with arbitrary key/value pairs. Continuous profiling stores will let you slice flame graphs by these labels — "show me CPU only when tenant=acme and feature_flag=new_pricing=true." This is the difference between "checkout is slow" and "checkout is slow *for this customer's payload size*."

```go
// Go: tag a section with profiler labels.
import "runtime/pprof"

func handlePricing(ctx context.Context, req Req) {
    labels := pprof.Labels(
        "tenant", req.Tenant,
        "endpoint", "POST_/pricing",
        "feature.new_pricing", strconv.FormatBool(req.NewPricing),
    )
    pprof.Do(ctx, labels, func(ctx context.Context) {
        compute(ctx, req)
    })
}
```

The corresponding eBPF profilers (Parca-agent, Pyroscope eBPF) read these labels via Go runtime introspection (the `runtime.labelMap`). For Java, async-profiler's `--threadlabel` and JFR events provide an analogue.

### 6.5 When to enable what

- **Always-on, every service:** eBPF system-wide CPU profiler at 19–99 Hz. Cost: <1% CPU overhead per node.
- **On-demand:** Go `/debug/pprof/heap`, `/debug/pprof/goroutine`, `/debug/pprof/block`. Wire to internal-only port; keep behind authn.
- **Continuous, top-N services:** `runtime/pprof` continuous profiler at 1 sample/min, pushed to Parca/Pyroscope. Cost: rounding error.
- **Allocation / heap profiles:** keep, but rate-limit. Heap dumps are expensive to take and to ship.

See doc 09 for the storage and query side.

---

## 7. Auto-Instrumentation vs Manual

Auto-instrumentation packages (OTel's `opentelemetry-instrumentation-*`) patch popular libraries — HTTP clients, gRPC, web frameworks, DB drivers, message brokers — and emit RED metrics + spans without code change. They are level-3 instrumentation in the hierarchy.

**Auto wins for:** ingress/egress patterns that look the same in every service. HTTP server, HTTP client, gRPC, JDBC, SQLAlchemy, redis-py, MongoDB driver, Kafka client. The signal shape is *identical* across services; auto is correct.

**Auto loses for:** business semantics. "The user converted from free to paid," "the basket exceeded the credit limit," "the model fell back to the older checkpoint." None of these are visible from a library patch.

The hybrid model:

```
        ┌────────────────────────────────────────────┐
        │  Auto-instrumentation                      │
        │  (HTTP/gRPC/DB/cache/queue, runtime metrics)│
        └────────────────────────────────────────────┘
                          ▼
        ┌────────────────────────────────────────────┐
        │  Manual instrumentation                    │
        │  (business spans, domain counters, audit)  │
        └────────────────────────────────────────────┘
                          ▼
        ┌────────────────────────────────────────────┐
        │  RED + USE on every boundary;              │
        │  domain on every business event            │
        └────────────────────────────────────────────┘
```

> **Pitfall:** Double-wrapping. The OTel HTTP server middleware adds a span; the OTel HTTP client middleware on the outbound call adds another; the gRPC instrumentation underneath adds a third. Some auto-instrumentation packages are aware of each other (the Java agent dedups), some are not (mixing OTel + a vendor agent often produces nested duplicate spans). Symptom: every trace has 2× the spans you expect, latency attribution is split. Audit by counting span kinds per request.

> **Diagnostic patterns:** If a single inbound HTTP request produces both a `SERVER` span named `POST /checkout` and an `INTERNAL` span named `POST /checkout` immediately under it, you have framework + middleware double-wrapping. Disable one.

What OTel auto-instrumentation actually patches (selected; full list in the OTel registry):

| Package | What it patches | What it emits |
|---|---|---|
| `opentelemetry-instrumentation-requests` (Py) | `requests.Session.send` | `CLIENT` span, `http.*` attrs |
| `opentelemetry-instrumentation-fastapi` (Py) | FastAPI middleware | `SERVER` span, `http.route` |
| `opentelemetry-instrumentation-sqlalchemy` (Py) | SQLAlchemy events | `CLIENT` span per query, `db.statement` |
| `@opentelemetry/instrumentation-http` (Node) | Node `http` module | `SERVER` + `CLIENT` spans |
| `@opentelemetry/instrumentation-pg` (Node) | `pg` driver | `CLIENT` span per query |
| `opentelemetry-javaagent` (Java) | bytecode instr at load time, ~150 libraries | universal coverage |
| `otelhttp` (Go) | `http.Handler` and `http.RoundTripper` wrappers | `SERVER` / `CLIENT` |

Go has *no bytecode auto-instrumentation* (the language doesn't support it cleanly); you wrap handlers and clients explicitly. This is fine — the wrapping is two lines. The Go ecosystem treats explicit wrapping as the auto-instrumentation pattern.

---

## 8. Library and SDK Design Considerations

If you're authoring a library that other services will pull in (an internal SDK for your payment engine, an open-source HTTP client, a database driver), the question is: **do I instrument internally, or expose hooks?**

The answer for *libraries* (not applications): **use the OTel API, not the SDK.**

```
┌────────────────┐                ┌────────────────┐
│ OTel API       │   produced by  │ OTel SDK       │
│ (interface)    │ ◀────────────  │ (impl, exporter│
│  Tracer.span() │                │  configured by │
│  Meter.counter │                │  the consumer) │
└────────────────┘                └────────────────┘
        ▲
        │ depends on
        │
┌────────────────┐
│ Your library   │
│ (no SDK dep)   │
└────────────────┘
```

The library imports `opentelemetry-api` only; the application imports the SDK and configures the exporter. Why this matters:

1. **No runtime conflict.** If your library hard-pinned the OTel SDK, two libraries in the same app pinning different SDK versions = build break or dual exporters.
2. **No vendor lock.** The application chooses the destination (Tempo, Jaeger, Honeycomb, Datadog).
3. **No accidental telemetry leak.** A library that ships its own exporter sends data wherever the *library author* configured, not where the application owner wants it.

The same logic applies to metrics: depend on the OTel **Meter API**, not on `prometheus_client` directly. If you must support a Prometheus-only consumer, expose a *registry hook* and let them register your collectors.

```go
// Go: a library that exposes counters via OTel Meter, not Prometheus directly.
package paykit

import "go.opentelemetry.io/otel/metric"

type Client struct {
    meter   metric.Meter
    charges metric.Int64Counter
}

func NewClient(meter metric.Meter) (*Client, error) {
    c, err := meter.Int64Counter("paykit.charges_total",
        metric.WithDescription("payment charge attempts"))
    if err != nil { return nil, err }
    return &Client{meter: meter, charges: c}, nil
}

// Consumer wires it:
//   meterProvider := otelsdkmetric.NewMeterProvider(...)
//   client, _ := paykit.NewClient(meterProvider.Meter("paykit"))
```

Avoid:

```go
// BAD — ships Prometheus dep transitively, may break consumer's vendoring.
import "github.com/prometheus/client_golang/prometheus"
var charges = promauto.NewCounter(...)
```

For logging libraries: depend on `slog.Logger` (Go) / `Logger` interface (Java SLF4J, .NET ILogger, Python logging) — never a concrete logger.

---

## 9. eBPF as Instrumentation

eBPF lets you attach programs to kernel events (syscalls, network packets, scheduler ticks, perf samples) and to user-space probes (uprobes on functions) **without changing the application**. For SRE this is the answer to "I can't change that third-party binary, but I need to know what it's doing."

### 9.1 What eBPF covers well

| Use case | Tools | What you get |
|---|---|---|
| Per-function CPU profiling | Parca-agent, Pyroscope eBPF | Flame graphs across all processes, no code change |
| Latency histograms (any syscall, any function) | bpftrace, BCC, Pixie | Per-process or per-PID histograms of `read()` durations, etc. |
| Network flow telemetry | Cilium, Hubble, Beyla | L4/L7 RED metrics per service-to-service edge |
| Syscall tracing / security | Tetragon, Falco | Audit which processes did what syscall when |
| TCP retransmits, drops, RTT | bpftrace, node_exporter eBPF | Network-layer signals for "is the network the problem?" |
| OOM kill, page fault, scheduler stall | `oomkill.bt`, `runqlat.bt` | Reasons a process slowed down or died that the app can't see |

### 9.2 What eBPF misses

- **Business semantics.** No "user_id," no "order_total," no feature flag.
- **Managed-runtime internals.** JVM and V8 stacks need extra unwinding (perf-map files); Python stacks need `pyperf` / py-spy (uprobes on the interpreter).
- **TLS-encrypted payload content.** You can see the TCP timing but not the HTTP body unless you uprobe libcrypto.

### 9.3 Kernel constraints

eBPF programs need:

- Kernel **≥4.18** for full BPF type info; **≥5.4** strongly preferred for CO-RE (Compile Once – Run Everywhere).
- **BTF** (BPF Type Format) in `/sys/kernel/btf/vmlinux` for portable programs. Most distros ship this since 2021; some hardened kernels strip it.
- **Capabilities**: `CAP_BPF` + `CAP_PERFMON` (modern kernels), or `CAP_SYS_ADMIN` (old-style). In Kubernetes this means a privileged DaemonSet or a `securityContext` with these caps.

> **Pitfall:** "We'll use eBPF for everything" — until the security team enforces seccomp profiles that block `bpf()`, or you run on a node with a stripped kernel, or a JVM service shows up as `[unknown]` frames. eBPF is best as a **floor** (always-on, language-agnostic baseline) supplemented by in-process instrumentation where you need richer semantics.

### 9.4 Where eBPF earns its keep

The two clearest wins:

1. **Filling instrumentation gaps in third-party services.** A vendor binary you can't recompile gets RED metrics from Cilium service mesh + a flame graph from Parca-agent. Zero code change.
2. **Detecting failures the application can't see.** SYN-cookie events, `oom_kill`, runqueue stall, TCP retransmit storms. The app sees only "my call hung"; eBPF sees the kernel-level cause.

---

## 10. Frontend / Mobile / RUM Instrumentation

User-perceived performance lives in the browser and the device, not in the server log. RUM (Real User Monitoring) is the only signal that captures "the page actually felt slow to actual users." For most consumer products it is the *highest-stakes* observability surface.

### 10.1 Browser: Web Vitals

Google's Web Vitals are the de facto standard for user-perceived web performance:

| Metric | What | Threshold (good) |
|---|---|---|
| **LCP** (Largest Contentful Paint) | Time to render the largest above-the-fold element | < 2.5 s |
| **INP** (Interaction to Next Paint) | Worst input → next-paint delay across the session | < 200 ms |
| **CLS** (Cumulative Layout Shift) | Sum of unexpected visual shifts | < 0.1 |
| **FCP** (First Contentful Paint) | Time to first painted text/image | < 1.8 s |
| **TTFB** (Time to First Byte) | Network start → first response byte | < 800 ms |

These are derived from the [Performance Observer API](https://developer.mozilla.org/en-US/docs/Web/API/PerformanceObserver) and packaged in the [`web-vitals`](https://github.com/GoogleChrome/web-vitals) library:

```javascript
// Browser: emit Web Vitals to your RUM endpoint with trace context.
import { onCLS, onINP, onLCP, onFCP, onTTFB } from "web-vitals";
import { trace, context } from "@opentelemetry/api";

function send(metric) {
  const span = trace.getActiveSpan();
  const sc = span ? span.spanContext() : null;
  navigator.sendBeacon("/rum", JSON.stringify({
    name: metric.name,
    value: metric.value,
    id: metric.id,
    ts: Date.now(),
    page: location.pathname,
    trace_id: sc?.traceId,
    user_session: getSessionId(),       // hashed, no PII
  }));
}
[onLCP, onINP, onCLS, onFCP, onTTFB].forEach((fn) => fn(send));
```

### 10.2 Mobile

| Platform | Source | Useful signals |
|---|---|---|
| **Android** | Play Console Vitals + Firebase Performance + Sentry/Datadog SDK | App startup time, ANR rate, crash rate, slow frame %, frozen frame % |
| **iOS** | MetricKit (since iOS 13) | Hang rate, launch time, scroll hitches, memory usage at termination |

MetricKit delivers daily aggregated reports straight from the OS — these are the *user's* metrics, not the app's measurements of itself, and that distinction matters for SLO truthfulness.

### 10.3 trace_id propagation: browser → backend

The single most valuable RUM-to-backend pattern: when the browser fires a fetch, inject the `traceparent` header so the backend trace and the RUM event share a `trace_id`. Now "this slow page load" links to "this slow API call" with one click.

```javascript
// Browser: instrument fetch with traceparent.
import { context, trace } from "@opentelemetry/api";
import { ZoneContextManager } from "@opentelemetry/context-zone";

const _fetch = window.fetch;
window.fetch = async (input, init = {}) => {
  const tracer = trace.getTracer("browser");
  const span = tracer.startSpan(`fetch ${typeof input === "string" ? input : input.url}`);
  return context.with(trace.setSpan(context.active(), span), async () => {
    init.headers = { ...init.headers, traceparent: makeTraceparent(span.spanContext()) };
    try {
      const r = await _fetch(input, init);
      span.setAttribute("http.status_code", r.status);
      return r;
    } finally { span.end(); }
  });
};
```

> **Pitfall:** Ad blockers and corporate proxies block calls to common RUM endpoints (`/datadog/`, `*.sentry.io`, `*.newrelic.com`). Real-world RUM beacon loss is 10–30%. Work around it by serving the beacon endpoint from your own domain (`/rum`) and proxying server-side.

> **Pitfall:** `user_id` as a high-cardinality dimension in RUM dashboards is fine *in the log/event store* but a death sentence in the *metric store*. Hash it for cohorts; never as a label.

---

## 11. Database / Data-Pipeline Instrumentation

Databases produce some of the highest-leverage signals in the stack: a slow query is often an entire incident's root cause. The trick is layering DB-level signals (slow log, `pg_stat_statements`) with *client-side* instrumentation (per-query span + duration histogram) so you have both the DB's view ("this query ran 12k times averaging 4ms") and the application's view ("our checkout p99 hit 800ms because the cart query degraded").

### 11.1 Per-database signal sources

| Database | Server-side | Client-side hook |
|---|---|---|
| PostgreSQL | `pg_stat_statements`, `pg_stat_activity`, slow query log, `auto_explain` | OTel JDBC/SQLAlchemy/asyncpg/pgx instrumentation |
| MySQL | `performance_schema`, slow query log | OTel JDBC/SQLAlchemy/mysqlclient |
| MongoDB | profiler (slowops), `currentOp` | OTel mongo / mongoose instrumentation |
| Redis | `SLOWLOG`, `INFO` (memory, evictions, hit rate) | OTel redis instrumentation per language |
| Cassandra | system tables (`system.peers`, `system.local`), JMX | DataStax driver metrics |

`pg_stat_statements` is a query-level rollup (calls, total_time, rows, blocks_hit) since the last reset. Sample it every minute via an exporter:

```sql
-- A useful pg_stat_statements query the exporter (or you) runs:
SELECT
  queryid,
  substring(query, 1, 200) AS query_short,
  calls,
  total_exec_time / NULLIF(calls, 0) AS avg_ms,
  rows / NULLIF(calls, 0) AS avg_rows,
  shared_blks_hit + shared_blks_read AS blocks_touched
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

### 11.2 Connection pool: the saturation signal

Pool exhaustion is the most-missed cause of latency cliffs. Every pool needs four gauges:

- `db_pool_max` (constant)
- `db_pool_acquired` (in-use)
- `db_pool_idle`
- `db_pool_wait` (count of callers currently blocked acquiring)

If `db_pool_wait > 0` for any sustained period, you have a queue forming, latency is climbing, and the histogram alone won't tell you why.

### 11.3 Replication lag

For replicated stores: emit lag in *seconds* (time-based), not bytes or rows. Customers experience "five seconds behind," not "1.4 GiB of WAL." On Postgres: `EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))`. On MySQL: `Seconds_Behind_Master` from `SHOW REPLICA STATUS` (but read with caution — counts only when the IO thread is running).

### 11.4 ORM instrumentation: what they emit and what they miss

| ORM | Emits | Misses |
|---|---|---|
| GORM (Go) | Per-query callback hooks; OTel plugin emits `CLIENT` span + `db.statement` | N+1 queries (each is a separate span; you have to detect the pattern in the trace) |
| SQLAlchemy (Python) | `before_cursor_execute` event; OTel instr emits span and `db.statement` | Lazy loads inside the response serializer |
| Hibernate (Java) | StatementInspector + OTel; spans per statement | `@OneToMany` lazy fetches at view layer |
| Entity Framework (.NET) | `DbCommand` interceptor; OTel emits spans | Tracking-disabled queries that bypass the interceptor |

The pattern they all miss: **the N+1 query**. The trace will show 50 sequential 2ms DB spans inside one request — easy to spot in the flamegraph, invisible in metric aggregates. A simple SLO-violating query is harder to find than a structurally wrong query loop.

### 11.5 Spark / Flink instrumentation

- **Spark**: `SparkListener` API exposes stage/task events. Plug a custom listener that emits `spark.stage.duration`, `spark.task.failed`, `spark.executor.memory_pressure` to your metric store. Spark's built-in metrics sink can write to Graphite/Prometheus/StatsD via configuration in `metrics.properties`.
- **Flink**: `MetricReporter` API. Built-in reporters for Prometheus and OTel. Per-operator metrics (`numRecordsIn`, `numRecordsOut`, `currentOutputWatermark`, `backPressuredTimeMsPerSecond`) are the headline signals.

### 11.6 Kafka consumer lag — the most-misunderstood metric

Two metrics, not one:

- **Lag (count)** = `log_end_offset - committed_offset`. "We are 4,213 messages behind."
- **Lag (time)** = `now - timestamp_of_oldest_uncommitted_message`. "We are 12 seconds behind."

Lag in messages is the obvious metric; lag in time is the one that matters for SLOs, because 4,213 messages is meaningless without knowing the production rate. A consumer at 4k msg/s with 4k lag count is half a second behind. A consumer at 1 msg/s with 4k lag count is over an hour behind.

The Kafka exporters (Burrow, kafka-lag-exporter) emit count by default; the time variant requires either:

1. The consumer producing a metric of `now - record.timestamp()` per consumed record (recommended), or
2. The exporter sampling the offset corresponding to "now-1m" and computing the lag.

```java
// Java: per-record lag-time, emitted on consume.
ConsumerRecords<String, byte[]> recs = consumer.poll(Duration.ofMillis(500));
long now = System.currentTimeMillis();
for (ConsumerRecord<String, byte[]> r : recs) {
    lagTimeHist.record(now - r.timestamp(),
        Attributes.of(AttributeKey.stringKey("topic"), r.topic()));
    process(r);
}
```

> **Pitfall:** Alerting on "lag count > 10000" is fine for steady-state, but breaks on bursty publishers. Alerting on "lag time > 30s" is the SLO-aligned form.

---

## 12. Service Mesh as Instrumentation

Sidecar service meshes (Istio, Linkerd, Cilium) intercept every service-to-service call at the L4/L7 boundary. They can emit standardized RED metrics and traces for *every* edge in your service graph **with no application change**.

```
┌──────────────────────┐     mesh sidecar     ┌──────────────────────┐
│  service A           │   intercepts L4/L7   │  service B           │
│  ┌──────────┐        │   ┌──────────┐       │  ┌──────────┐        │
│  │ app code │──HTTP─▶│   │ Envoy    │──TLS─▶│  │ Envoy    │──HTTP─▶│  app code  │
│  └──────────┘        │   └─────┬────┘       │  └─────┬────┘        │
│                      │         │            │        │             │
└──────────────────────┘         │            │        │             │
                                 ▼            │        ▼             │
                       request_total          │   request_total      │
                       request_duration       │   request_duration   │
                       upstream_rq_5xx        │   upstream_rq_5xx    │
                       (uniform across fleet) │   (uniform)          │
```

**What you get for free:**

- `*_request_total`, `*_request_duration_milliseconds`, `*_request_bytes`, `*_response_bytes`, per source/destination/response_code.
- L7 traces with `traceparent` propagation handled by the proxy (the mesh extracts/injects, but the *user code still has to forward the header* — see pitfall).
- A service graph: every edge in the mesh emits a metric, so the graph is auto-derived.
- Retries, circuit breaker state, and outlier detection events become metrics without app changes.

**What you don't get:**

- App-level metrics (orders processed, items in cart). The mesh has no idea what an "order" is.
- Database calls (the proxy is between *services*, not between service and DB).
- Frontend metrics (the proxy is in the cluster).
- **Trace context propagation through your code.** The proxy sets `traceparent` on the inbound side and reads it on the outbound side, but if your application code does not carry `traceparent` from request to outbound HTTP call, the trace is broken in the middle. *The mesh propagates between proxies, not between your handlers.*

> **Pitfall:** "We have Istio, we don't need app instrumentation" — until you discover that the handler grabbed a fresh `context.Background()` for an outbound call and the mesh-to-mesh trace is two unconnected traces. Mesh tracing is a complement to app instrumentation, not a replacement.

**When to delete app-level HTTP metrics in favor of mesh metrics:** when (a) every service is in the mesh, (b) the mesh metrics have label parity with what you had, and (c) you've validated dashboards/alerts work off the mesh series. This is usually a 3–6 month migration with both running in parallel — see §15 on rollout discipline.

---

## 13. The "Instrument the Boundary" Principle

Most teams over-instrument internals — every helper function as a span, every loop iteration as a counter — and under-instrument boundaries. This is backwards. **The leverage is at the boundary**, because:

- Internal latency is visible in profiles.
- Boundary failures (timeouts, retries, fallbacks, circuit breaker trips) are *invisible* in profiles and only sometimes visible in traces.
- Boundary events are exactly where decisions get made under load.

**Boundaries that always deserve dedicated instrumentation:**

| Boundary | Counter | Why |
|---|---|---|
| Outbound HTTP / RPC retry | `*_retries_total{peer, attempt, reason}` | Retry storms are silent until they aren't |
| Circuit breaker state transition | `circuit_state_changes_total{name, from, to}` | "Why did we shed traffic at 14:32?" |
| Fallback engagement | `fallback_used_total{path, reason}` | Distinguishes "degraded" from "broken" |
| Timeout (deadline exceeded) | `deadline_exceeded_total{op, peer}` | Different from generic 5xx |
| Cache outcome | `cache_ops_total{op, result=hit\|miss\|refresh\|error}` | Cache hit ratio is the cheapest perf signal in the stack |
| Queue handoff (enqueue/dequeue) | `queue_drops_total{queue, reason}`, `queue_depth` gauge | Backpressure visibility |
| Rate limit hit | `rate_limited_total{limiter, key_class}` | Tells you when you're shedding load |
| Auth decision | `auth_decisions_total{outcome=allow\|deny\|error}` | Audit + perf signal |
| Bulkhead reject | `bulkhead_rejects_total{name}` | Saturation by design |

Each of these is roughly 5 lines of code and replaces an entire category of "we don't know what's happening" outage. They cost nothing in cardinality (the dimensions are bounded — a circuit breaker has 3 states, a cache has 4 outcomes).

> **Mental model:** A request flowing through your service goes through a sequence of *decisions*: auth allow/deny, cache hit/miss, db acquire/queue, primary/fallback, timeout/respond. Each decision is a boundary. Instrument the decision, not the line of code that implements it.

---

## 14. PII, Redaction, and Compliance at the Instrumentation Layer

The compliance pattern: **redact at the source** (instrumentation time), not at the agent or the store. Once data is in a buffer it has been to disk, container logs, kernel, and possibly a replicated journal. "We'll fix it at the agent" is the line every compliance auditor smiles at.

### 14.1 Allowlist vs blocklist

**Allowlist (correct):** every field is private by default; you mark loggable fields explicitly.
**Blocklist (fragile):** every field is public by default; you regex-match and strip the dangerous ones.

Allowlists fail closed — a new field added to a struct is *not* logged unless someone says it's safe. Blocklists fail open — a new field is logged until someone notices.

```go
// Go: allowlist via struct tags + a small reflective marshaler.
type User struct {
    ID      string  `log:"user.id"`         // allowed
    Email   string  `log:"-"`                // never
    Phone   string  `log:"-"`
    Tier    string  `log:"user.tier"`        // allowed
    Address Address // not tagged → not logged
}
```

### 14.2 Hash-and-bucket high-cardinality identifiers

Instead of `user_id` directly:

- For *correlation* across logs: keep `user.id` (it's already in your DB; the threat model is about who can read logs, solved by access controls).
- For *metric labels*: never emit user_id; bucket to `tier`, `cohort`, `region`.
- For *cross-system join* without disclosure: `user.email_hash = HMAC(rotating_key, email)`. The hash changes per key rotation, breaking long-term re-identification.

### 14.3 Structured taxonomy

OTel semantic conventions give you a vocabulary to be precise: `user.id`, `user.email`, `user.email_hash`, `client.address`, `client.port`. Pick from the standard list; don't invent. Logs and span attrs are searchable across the whole org if everyone uses the same names.

### 14.4 Compliance-class tagging

Some attributes are regulated (GDPR, HIPAA, PCI). Tag them at instrumentation time:

```python
# Python: a wrapper that classifies each attribute.
def log_event(name, attrs: dict):
    classified = {}
    for k, v in attrs.items():
        cls = ATTR_TAXONOMY.get(k, "private")
        if cls == "private":
            continue                 # drop
        if cls == "pii_hashed":
            v = hash_with_key(v)
        classified[k] = v
    log.info(name, **classified)
```

The agent (doc 04) gets a *second* layer of redaction as defense-in-depth, but the source is authoritative.

---

## 15. Versioning and Rollout Discipline

A change to instrumentation is a change to the **dashboard contract**. Renaming `http_requests_total` to `http_server_requests_total` breaks every PromQL query. Removing the `route` label breaks every drilldown. Renaming a span breaks every saved trace search.

The safe-rollout pattern is **emit both, migrate, remove**:

```
                 [time]
   ┌──────────────────────────────────────────┐
   │  v1: emit only OLD metric                 │
   ├──────────────────────────────────────────┤
   │  v2: emit BOTH old AND new metrics        │  ← deploy this; soak ≥1 week
   │      (for one full alert/release cycle)  │
   ├──────────────────────────────────────────┤
   │  v3: migrate dashboards & alerts to NEW   │  ← parallel; verify panels
   ├──────────────────────────────────────────┤
   │  v4: emit only NEW metric                 │  ← remove OLD
   └──────────────────────────────────────────┘
```

**The deprecation CI check.** Build a check that:

1. Loads the latest binary.
2. Scrapes `/metrics` and records every metric+label combination.
3. Compares to the previous main-branch baseline.
4. Fails the PR if a metric or label was *removed* without a corresponding deprecation note in the changelog.

The same check for spans (run a synthetic load against the binary, dump the span names; diff). For OTel emitting via OTLP, the resource and instrumentation scope name are also part of the contract — diff those too.

> **Pitfall:** A metric rename ships, dashboards still query the old name, panels go blank, on-call doesn't notice for 4 days, then a real incident happens and the dashboards are useless. Treat metric/span schemas like API schemas — versioned, deprecation period, CI-enforced.

> **Diagnostic patterns:** Maintain a `METRICS.md` (or generate it from code annotations) listing every metric, its labels, and its consumer dashboards. PRs that touch instrumentation update this file. Reviewers see the impact at the diff line.

---

## 16. A Complete Walked Example

A single Go HTTP API for a checkout endpoint, instrumented properly across all four signals. This is the "if you do nothing else, copy this" reference.

```go
// checkout.go — minimum-viable production instrumentation for one endpoint.
package main

import (
    "context"
    "encoding/json"
    "errors"
    "log/slog"
    "net/http"
    "os"
    "strconv"
    "time"

    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/promauto"
    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/attribute"
    "go.opentelemetry.io/otel/codes"
    "go.opentelemetry.io/otel/trace"
)

// ---------- Signals: metrics ----------
var (
    reqs = promauto.NewCounterVec(prometheus.CounterOpts{
        Name: "http_requests_total",
    }, []string{"method", "route", "status_class"})

    dur = promauto.NewHistogramVec(prometheus.HistogramOpts{
        Name:    "http_request_duration_seconds",
        Buckets: []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5},
    }, []string{"method", "route", "status_class"})

    inflight = promauto.NewGaugeVec(prometheus.GaugeOpts{
        Name: "http_inflight_requests",
    }, []string{"route"})

    cacheOps = promauto.NewCounterVec(prometheus.CounterOpts{
        Name: "cache_ops_total",
    }, []string{"op", "result"})

    retries = promauto.NewCounterVec(prometheus.CounterOpts{
        Name: "outbound_retries_total",
    }, []string{"peer", "reason"})
)

// ---------- Signals: logs ----------
var logger = slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
    Level: slog.LevelInfo,
})).With("service", "checkout")

func logCtx(ctx context.Context) *slog.Logger {
    sc := trace.SpanContextFromContext(ctx)
    if !sc.HasTraceID() {
        return logger
    }
    return logger.With("trace_id", sc.TraceID().String(),
                       "span_id", sc.SpanID().String())
}

// ---------- Tracing handle ----------
var tracer = otel.Tracer("checkout")

// ---------- The handler ----------
type checkoutReq struct {
    UserID string  `json:"user_id"`
    SKU    string  `json:"sku"`
    Amount float64 `json:"amount"`
}

func handleCheckout(w http.ResponseWriter, r *http.Request) {
    const route = "/checkout"
    start := time.Now()
    inflight.WithLabelValues(route).Inc()
    defer inflight.WithLabelValues(route).Dec()

    ctx, span := tracer.Start(r.Context(), "POST /checkout",
        trace.WithSpanKind(trace.SpanKindServer))
    defer span.End()

    var req checkoutReq
    if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
        finish(ctx, span, w, route, "POST", 400, start, err)
        return
    }
    span.SetAttributes(
        attribute.String("user.id", req.UserID),     // PII-safe (internal id)
        attribute.String("checkout.sku", req.SKU),
        // amount intentionally not in span attrs — moves to log only.
    )
    logCtx(ctx).Info("checkout.start", "user.id", req.UserID, "sku", req.SKU)

    // ---- cache lookup ----
    if hit := lookupCache(ctx, req.SKU); !hit {
        cacheOps.WithLabelValues("get", "miss").Inc()
    } else {
        cacheOps.WithLabelValues("get", "hit").Inc()
    }

    // ---- DB query ----
    cart, err := loadCart(ctx, req.UserID)
    if err != nil {
        finish(ctx, span, w, route, "POST", 500, start, err)
        return
    }

    // ---- outbound call to pricing service ----
    price, err := fetchPriceWithRetry(ctx, req.SKU, 3)
    if err != nil {
        finish(ctx, span, w, route, "POST", 502, start, err)
        return
    }

    // ---- redacted business log line ----
    logCtx(ctx).Info("checkout.priced",
        "user.id", req.UserID,
        "sku", req.SKU,
        "items", len(cart),
        "amount.cents", int(price*100),               // never log raw amount/PII
    )

    w.WriteHeader(200)
    finish(ctx, span, w, route, "POST", 200, start, nil)
}

func finish(ctx context.Context, span trace.Span, w http.ResponseWriter,
            route, method string, status int, start time.Time, err error) {
    if err != nil {
        span.RecordError(err)
        span.SetStatus(codes.Error, classifyErr(err))
        logCtx(ctx).Error("checkout.failed", "status", status,
                          "error.type", classifyErr(err))
        if w.Header().Get("Content-Type") == "" {
            http.Error(w, "internal error", status)
        }
    }
    sc := strconv.Itoa(status/100) + "xx"
    reqs.WithLabelValues(method, route, sc).Inc()
    obs := dur.WithLabelValues(method, route, sc).(prometheus.ExemplarObserver)
    if tsc := trace.SpanContextFromContext(ctx); tsc.HasTraceID() {
        obs.ObserveWithExemplar(time.Since(start).Seconds(),
            prometheus.Labels{"trace_id": tsc.TraceID().String()})
    } else {
        obs.Observe(time.Since(start).Seconds())
    }
}

func lookupCache(ctx context.Context, sku string) bool {
    _, span := tracer.Start(ctx, "cache.get", trace.WithSpanKind(trace.SpanKindClient))
    defer span.End()
    span.SetAttributes(attribute.String("cache.key", sku))
    return false // simulate miss
}

func loadCart(ctx context.Context, userID string) ([]string, error) {
    ctx, span := tracer.Start(ctx, "db.query.cart_by_user",
        trace.WithSpanKind(trace.SpanKindClient),
        trace.WithAttributes(
            attribute.String("db.system", "postgresql"),
            attribute.String("db.statement", "SELECT * FROM cart WHERE user_id = $1"),
            attribute.String("user.id", userID),
        ))
    defer span.End()
    // ... real query ...
    return []string{"sku-1", "sku-2"}, nil
}

func fetchPriceWithRetry(ctx context.Context, sku string, maxAttempts int) (float64, error) {
    ctx, span := tracer.Start(ctx, "pricing.GET",
        trace.WithSpanKind(trace.SpanKindClient),
        trace.WithAttributes(attribute.String("peer.service", "pricing")))
    defer span.End()
    for attempt := 1; attempt <= maxAttempts; attempt++ {
        price, err := callPricing(ctx, sku)
        if err == nil {
            return price, nil
        }
        retries.WithLabelValues("pricing", classifyErr(err)).Inc()
        span.AddEvent("retry.attempt", trace.WithAttributes(
            attribute.Int("attempt", attempt),
            attribute.String("error.type", classifyErr(err)),
        ))
    }
    err := errors.New("retries exhausted")
    span.RecordError(err)
    span.SetStatus(codes.Error, "retries_exhausted")
    return 0, err
}
```

What this 80-line example demonstrates:

- **All four signals.** Metrics (5 instruments), logs (`slog` JSON with trace_id), traces (one server span + three client spans + retry events), and the entire thing is profilable via `import _ "net/http/pprof"`.
- **The boundary discipline (§13).** Cache outcome counter, retry counter, error counter — every interesting decision is a metric.
- **Cardinality discipline (§3.4).** Labels are bounded (`route`, `status_class`, `op`, `result`, `peer`, `reason`); user_id and SKU never appear as labels.
- **Exemplars (§3.3).** Every histogram observation carries the `trace_id` for one-click metric→trace.
- **PII discipline (§14).** Internal IDs are logged; raw amounts converted to integer cents; no email/card.
- **Error pattern (§5.5).** `RecordError` + `SetStatus(Error)` everywhere, then either return the error or write the response.

Replicate this shape across every endpoint and you have a service with the floor described in §2.2 covered.

---

## 17. Anti-Patterns Checklist

A pattern catalog of things to delete on sight in code review. Each one is "looks innocent, hurts forever."

| # | Smell | Why it's bad | Fix |
|---|---|---|---|
| 1 | Metric label = `user_id` / `request_id` / `customer_id` | Unbounded cardinality; TSDB OOM | Move to log/span attribute |
| 2 | Metric label = raw URL path | Same; every query string explodes series count | Use templated `route` |
| 3 | All logs at `INFO` | Signal-to-noise ratio at incident time is destroyed | Use levels; tail-sample debug |
| 4 | Stringly-typed log lines (`"user 42 failed because db down"`) | Not searchable, not joinable | Structured JSON, stable `msg`, attrs |
| 5 | Manual `correlation_id` instead of `trace_id` | Two correlation systems; logs don't join to traces | Use OTel `trace_id` everywhere |
| 6 | Every function is a span | Trace UI noise; storage bloat; nothing more visible | Span only at boundaries + ≥1ms in-process ops |
| 7 | Default histogram buckets that don't span the SLO | `histogram_quantile` interpolates across coarse bucket → wrong p99 | SLO-aligned buckets, or native histograms |
| 8 | Client-side summary instead of server-side histogram | Cannot aggregate p99 across replicas | Histogram, always |
| 9 | `record_exception` without `set_status(ERROR)` (or vice versa) | Span looks green in UI / loses stack | Always both |
| 10 | Logging inside a hot loop | One slow request → 10k log lines, log volume bill spikes | Aggregate; log once with summary attrs |
| 11 | Logging the request body / SQL params / JWT | PII leak; auditor finding | Redact at source; allowlist |
| 12 | `INFO` at request start AND end on every request | 2× log volume; same data is in metrics | Log only on error/slow path; sample on success |
| 13 | Manual emission of metrics the runtime already provides (heap size, GC pause) | Drift from runtime truth; double cost | Use runtime/metrics, JFR, EventCounters |
| 14 | App-level HTTP metric + mesh metric for the same call | Double-counted RED; confused alerts | Pick one, document, delete the other |
| 15 | New metric ships, old metric removed in same PR | Dashboards/alerts break silently | Both for one cycle, then remove |
| 16 | Library imports `prometheus_client` directly | Dep conflicts in consumer | Depend on OTel API only |
| 17 | Sampling decision per-process | Trace truncates mid-flow | Propagate decision via tracestate |
| 18 | Counting sucesses with `_success_total` and failures with `_failure_total` as separate metrics | Cannot compute error rate cleanly | One counter with `result=success\|fail` label |
| 19 | Log lines without `service` field | Cannot filter to the service that emitted | Set once at logger init from `OTEL_SERVICE_NAME` |
| 20 | Profiles enabled without frame pointers | Flame graph is `[unknown] [unknown] [unknown]` | Build with `-fno-omit-frame-pointer` |

---

## 18. What This Chapter Does Not Cover

This chapter is about *producing* signals. It does not cover what happens after `Observe()` or `Emit()`. Where to read next:

- **OTel SDK internals**: how the batch span processor batches, how the exporter retries, how the meter provider aggregates, how resource detection works → **doc 02**.
- **Collection topology**: agents (Fluent Bit, Vector, OTel agent), gateways, tail sampling, edge transformations → **doc 04**.
- **Transport / buffering**: Kafka between collectors and storage, replay, fan-out → **doc 05**.
- **Storage internals**: TSDB compression and indexing, log inverted indexes, span stores → **doc 06–09**.
- **Query and dashboards**: PromQL, LogQL, TraceQL, exemplar UI → **doc 10–11**.
- **Alerting**: how to *act* on the signals you produce → **doc 12**.
- **SLO design**: turning metrics into SLIs and SLOs → **doc 13**.
- **Cardinality budgets and cost**: enforcement at the platform layer → **doc 18**.

A bad signal can't be fixed downstream. A good signal can be queried, sampled, stored, alerted, and learned from for years. That's why this chapter is the long one.

---

> **TL;DR.** Instrumentation is decided at four levels: hardware/kernel, runtime, framework auto, application. Every service emits **RED + USE** with **bounded cardinality** — `user_id` belongs in logs and spans, never labels. Logs are **structured JSON** with mandatory `ts/level/msg/service/trace_id/span_id`; redact PII at the source via allowlist. Spans are **boundary-driven**: one per inbound, one per outbound, span events for moments inside; `record_exception + set_status(ERROR)` is the canonical error pattern; propagate context through queues and threadpools or the trace truncates. Profiles need **frame pointers and build IDs**; eBPF gives you a free baseline floor; in-process pprof gives you allocation and contention details. Auto-instrumentation handles ingress/egress universally; manual handles domain semantics. Libraries depend on OTel **API**, never the SDK. Mesh metrics are a complement, not a replacement, for app instrumentation. Ship instrumentation changes with the **emit-both-then-deprecate** pattern, gated by a CI metric/span-schema diff. The single most-leveraged change in 90% of stacks is "make every log line carry `trace_id`."
