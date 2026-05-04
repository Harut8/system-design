# 11 — Dashboards: From Telemetry Wallpaper to a Question You Can Answer

A dashboard is not a display. It is a *question*, posed visually, that an on-call engineer can answer in five seconds at three in the morning. Every panel that does not contribute to answering a specific question should be deleted. The single most-broken thing in production observability is not the storage layer or the alert routing — it is the dashboard culture: hundreds of panels nobody reads, dozens of duplicates per service, half of them hardcoded to thresholds that drifted from the SLO two quarters ago.

This chapter is the staff-engineer view of how to build dashboards that survive contact with reality, integrate with the SRE practice (SLOs, error budgets, runbooks), and don't decay into wallpaper.

> **Mental model:** A good dashboard is a *triage tree*. The top of the page tells you *whether* there is a problem. The middle tells you *which subsystem*. The bottom (and the linked dashboards) tell you *why*. If a panel doesn't help you descend that tree, it doesn't belong.

---

## Table of Contents

1. [The Big Picture: Dashboard Taxonomy](#1-the-big-picture-dashboard-taxonomy)
2. [The Five (or Seven) Archetypes](#2-the-five-or-seven-archetypes)
3. [RED Method Deep Dive](#3-red-method-deep-dive)
4. [USE Method Deep Dive](#4-use-method-deep-dive)
5. [The Golden Signals Pattern](#5-the-golden-signals-pattern)
6. [Layout Principles](#6-layout-principles)
7. [Exemplars: The Metric → Trace Jump](#7-exemplars)
8. [Variables, Templating, and Repeating Panels](#8-variables-templating-and-repeating-panels)
9. [Annotations: The "What Changed?" Superpower](#9-annotations)
10. [Dashboards-as-Code](#10-dashboards-as-code)
11. [Anti-Patterns and Dashboard Sprawl](#11-anti-patterns-and-dashboard-sprawl)
12. [The Grafana Object Model in Depth](#12-the-grafana-object-model-in-depth)
13. [Other Dashboard Tools — When Each Shines](#13-other-dashboard-tools)
14. [RUM and Synthetic Dashboards](#14-rum-and-synthetic-dashboards)
15. [Mobile / On-Call Dashboard Pattern](#15-mobile-on-call-dashboard-pattern)
16. [Incident-Time Dashboard Hygiene](#16-incident-time-dashboard-hygiene)
17. [Worked Example: `/checkout`](#17-worked-example-checkout)
18. [Pitfalls](#18-pitfalls)
19. [Mental Models](#19-mental-models)
20. [Production-Ready Dashboard Checklist](#20-production-ready-dashboard-checklist)

---

## 1. The Big Picture: Dashboard Taxonomy

The dashboards in a healthy observability stack fall into a small number of well-defined families. Mixing families on the same page is the leading cause of dashboard rot.

```
┌───────────────────────────────────────────────────────────────────────┐
│                          DASHBOARD TAXONOMY                           │
│                                                                       │
│  AUDIENCE        DASHBOARD TYPE          ASKS                         │
│  ─────────────   ──────────────────      ─────────────────────────    │
│  Exec / PM      → Executive / business   "Are we hitting SLOs?        │
│                                            How much did the outage    │
│                                            cost?"                     │
│                                                                       │
│  SRE / On-call  → SLO / burn rate        "Is anything burning         │
│                                            error budget?"             │
│                                                                       │
│  Service team   → RED                    "How is my service doing?"   │
│                                                                       │
│  Platform team  → USE / capacity         "Is any resource saturated?  │
│                                            How much headroom left?"   │
│                                                                       │
│  Incident       → Incident / war-room    "What's broken right NOW     │
│                                            and what changed?"         │
│                                                                       │
│  Release        → Build / deploy         "Did the new version regress?│
│                                            Show before/after."        │
└───────────────────────────────────────────────────────────────────────┘
```

The hierarchy is not arbitrary. An on-call engineer woken by a page reads SLO → RED → USE → traces, in that order. Each level zooms in. The top tells you *whether*; the next *where*; the next *what*; the bottom *why*.

> **Mental model:** Dashboards are a triage tree, not a museum. Every panel must be reachable by an obvious mental path from a higher dashboard, and must lead naturally to a more specific one. If your dashboard is a single 200-panel wall, you have a museum.

---

## 2. The Five (or Seven) Archetypes

Each archetype has a specific audience and a specific question. The point of the taxonomy is that every panel inherits its place from the dashboard it lives on.

| Archetype | Audience | Refresh | Time range default | Goal |
|---|---|---|---|---|
| **SLO / burn rate** | SRE, on-call, leadership | 30s | 28d | Are we burning error budget faster than allowed? |
| **RED (per service)** | Service owners | 10–30s | 1h | How is *my* service performing right now? |
| **USE (per resource)** | Platform / infra | 10–30s | 1h | Is any node/pod/disk/GPU saturated? |
| **Capacity / forecasting** | Platform, FinOps | hourly–daily | 30d–90d | Where will we run out, and when? |
| **Incident / war-room** | IC, ops | 5–10s | 1h, narrowable | What is currently failing, what changed? |
| **Build / deploy** | Release engineering | 10s | 24h | Did this deploy regress? Compare before/after. |
| **Executive** | Leadership, PM, customers | 5m–1h | 30d–quarter | Reliability ↔ revenue, customer impact |

The two "or" archetypes (build/deploy and executive) appear in larger orgs; smaller setups fold the deploy view into the RED dashboard and skip exec dashboards entirely.

### What does NOT belong on each:

- **SLO**: raw infra metrics (those are USE), service-internal counters (those are RED), node-level data.
- **RED**: cross-service comparisons (different service owners look at different boards), infra-level metrics, business KPIs.
- **USE**: per-request latency (RED), application-level errors (RED).
- **Capacity**: real-time metrics (use the live RED/USE board), incident-specific drilldowns.
- **Incident**: long-range views (you're in the moment), aggregated business metrics.
- **Build/deploy**: anything not tied to a release artifact / commit SHA / version label.
- **Executive**: anything that requires understanding a metric formula. If a VP needs `histogram_quantile(0.99, ...)` to read it, redesign.

---

## 3. RED Method Deep Dive

**RED** = **R**ate, **E**rrors, **D**uration. Coined by Tom Wilkie at Weaveworks, now the *de facto* per-service dashboard convention. Every service should expose the same three signals the same way; once the platform is uniform, on-call mental load drops dramatically.

### 3.1 The three panels

```
┌───────────────────────────────────────────────────────────────────────┐
│  Service: checkout    env: prod    cluster: us-east-1                 │
├───────────────┬───────────────────────┬───────────────────────────────┤
│  RATE         │  ERRORS               │  DURATION                     │
│  req/s        │  err/s + ratio        │  p50 / p95 / p99 latency      │
│  (line chart) │  (line + stat panel)  │  (line chart, 3 series)       │
│               │                       │                               │
│  ┌────────┐   │  ┌────────┐ ┌──────┐  │  ┌────────────────────────┐   │
│  │   /\   │   │  │ /\     │ │ 0.42%│  │  │      ____p99           │   │
│  │  /  \_ │   │  │/  \____│ │      │  │  │ ____/                  │   │
│  └────────┘   │  └────────┘ └──────┘  │  │/____p95__              │   │
│               │                       │  │_____p50___             │   │
│               │                       │  └────────────────────────┘   │
└───────────────┴───────────────────────┴───────────────────────────────┘
```

### 3.2 PromQL templates

```promql
# RATE: total requests per second, broken down by route (or method)
sum by (route) (
  rate(http_requests_total{service="$service", env="$env"}[5m])
)

# ERRORS: error count per second
sum by (route) (
  rate(http_requests_total{service="$service", env="$env", status=~"5.."}[5m])
)

# ERROR RATIO: errors as fraction of total (for the stat panel)
sum(rate(http_requests_total{service="$service", env="$env", status=~"5.."}[5m]))
/
sum(rate(http_requests_total{service="$service", env="$env"}[5m]))

# DURATION: percentiles from a classic histogram
histogram_quantile(0.50,
  sum by (le, route) (
    rate(http_request_duration_seconds_bucket{service="$service", env="$env"}[5m])
  )
)
# … same for 0.95, 0.99
```

### 3.3 Why duration as histogram, not summary

Two ways to expose latency: classic histograms (cumulative buckets) or summaries (precomputed quantiles). Histograms win for production for one reason: they aggregate.

| | Histogram | Summary |
|---|---|---|
| Aggregation across instances | Yes (`sum by (le)`) | No (quantiles don't aggregate) |
| Aggregation across services | Yes | No |
| Cost | More series (one per bucket) | Fewer series |
| Precision | Bucket-bounded | Exact (per-instance) |
| Re-query a different quantile | Yes | No (must re-instrument) |

Summaries are tempting because the math is precise per instance. But the moment you have two instances, you cannot compute a fleet-wide p99 from per-instance summaries (you'd need to merge the underlying t-digests, which most clients don't expose). Histograms give that up willingly to gain aggregation.

> **Pitfall:** A team migrates from histograms to summaries to "save cardinality." Six months later a service scales out, and the dashboard p99 is the average of per-instance p99s — which understates the real fleet p99 by a wide margin. Stick with histograms unless you have a very specific reason.

### 3.4 Native histograms (Prometheus 2.40+)

If you're on Prometheus 2.40+ throughout your pipeline, native histograms collapse the per-bucket series fan-out to one series per metric. The PromQL is identical except no `_bucket` suffix and no `by (le)`:

```promql
histogram_quantile(0.99,
  sum by (route) (
    rate(http_request_duration_seconds{service="$service"}[5m])
  )
)
```

### 3.5 RED panel layout discipline

- **Three columns, equal width.** Always Rate-Errors-Duration in that order. When every team's RED dashboard looks identical, on-call brains develop spatial memory.
- **Same time range** across all three. A common bug: the duration panel set to `[1h]` and the rate panel to `[5m]`. The visual story lies.
- **Y-axis: rate in req/s, errors in err/s and percentage, duration in seconds (not ms).** Be consistent across services.
- **Color the percentile lines** the same way everywhere: p50 cool blue, p95 warm yellow, p99 red. Builds visual recognition.
- **Set thresholds tied to the SLO**, not arbitrary numbers. If your SLO is p99 < 500 ms, the threshold band on the duration panel reads 500 ms.

---

## 4. USE Method Deep Dive

**USE** = **U**tilization, **S**aturation, **E**rrors. Coined by Brendan Gregg for resource-level analysis. Where RED is per-service, USE is per-resource: CPU, memory, disk I/O, network, GPU, queue.

### 4.1 The matrix

| Resource | Utilization | Saturation | Errors |
|---|---|---|---|
| **CPU** | `1 - rate(node_cpu_seconds_total{mode="idle"}[5m])` | `node_load1 / count(node_cpu_seconds_total{mode="idle"})` | `rate(node_softnet_dropped_total[5m])` (less common) |
| **Memory** | `1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes` | `rate(node_vmstat_pgmajfault[5m])` (page faults) | `node_vmstat_oom_kill` |
| **Disk I/O** | `rate(node_disk_io_time_seconds_total[5m])` | `rate(node_disk_io_time_weighted_seconds_total[5m])` (avg queue length) | `rate(node_disk_io_errors_total[5m])` |
| **Network** | `rate(node_network_receive_bytes_total[5m]) / link_speed_bytes` | `rate(node_network_receive_packets_dropped_total[5m])` | `rate(node_network_receive_errs_total[5m])` |
| **GPU (NVIDIA, DCGM)** | `DCGM_FI_DEV_GPU_UTIL` | `DCGM_FI_DEV_FB_USED / DCGM_FI_DEV_FB_TOTAL` (memory pressure) | `DCGM_FI_DEV_XID_ERRORS` |
| **Queue (Kafka)** | `kafka_consumergroup_lag / kafka_consumergroup_capacity` | `kafka_consumergroup_lag` itself (depth as saturation proxy) | `kafka_topic_partition_under_replicated_partitions` |
| **Container** | CPU/mem ratios from cgroup | `container_cpu_cfs_throttled_periods_total` (throttling = saturation) | `kube_pod_container_status_terminated_reason{reason="OOMKilled"}` |

### 4.2 Why "saturation" is not "utilization + 1"

Utilization is *fraction of time the resource is busy*. Saturation is *how much extra work is queuing because the resource cannot keep up*. They are different measurements; conflating them hides outages.

```
A web server at 70% CPU utilization with no run-queue depth: healthy.
A web server at 70% CPU utilization with 200 threads waiting on the runqueue: in trouble.
```

Saturation requires queue-depth data: load average, page faults, CFS throttle counts, GC pauses, syscall blocking. None of these are "utilization."

### 4.3 USE dashboard layout

Per resource, three panels (Utilization, Saturation, Errors) in three columns — visually identical to RED, intentionally. Repeat the row per resource type. Use Grafana's "Repeating rows" with a `$resource` template variable to avoid copying panels.

```
┌───────────────────────────────────────────────────────────────┐
│  Cluster: us-east-1 prod    Node: ip-10-0-1-42                │
├──────────────────────────────────────────────────────────────-┤
│  CPU       UTIL ░░░ 70%   SAT  load1=8 / 16cores=0.5  ERR  0  │
│  MEMORY    UTIL ░░░ 84%   SAT  pgmajfault=0/s         ERR  0  │
│  DISK      UTIL ░░░ 45%   SAT  await=12ms             ERR  0  │
│  NETWORK   UTIL ░░░ 30%   SAT  drops=0/s              ERR  0  │
│  GPU 0     UTIL ░░░ 95%   SAT  fb_used=92%            ERR  0  │
└───────────────────────────────────────────────────────────────┘
```

### 4.4 USE for non-physical resources

USE generalizes beyond hardware. Connection pools, thread pools, queue topics, file descriptors all admit USE analysis:

- **Connection pool**: utilization = `pool_in_use / pool_max`; saturation = `pool_wait_queue_depth`; errors = `pool_acquire_timeout_total`.
- **Thread pool**: utilization = `active_threads / max_threads`; saturation = `pool_queue_size`; errors = `rejected_executions_total`.
- **Kafka topic**: utilization = consumer-side throughput / capacity; saturation = lag; errors = under-replicated partitions.

The pattern is: *for any contended resource, three signals; same dashboard layout.*

---

## 5. The Golden Signals Pattern

**Golden Signals** (Google SRE) = Latency, Traffic, Errors, Saturation. RED + Saturation, essentially. The point is to define *the four numbers every service must answer* so a new service onboards in hours, not days.

| Signal | Per-service question | Where it lives on the dashboard |
|---|---|---|
| **Latency** | How long do successful requests take? (p50/95/99) | Duration panel (RED) |
| **Traffic** | How many requests per second? | Rate panel (RED) |
| **Errors** | How many failures per second + ratio? | Errors panel (RED) |
| **Saturation** | How "full" is the service? (CPU, queue depth, GC pause) | Bottom row, USE-style |

The pattern: every service exposes Golden Signals. The RED dashboard renders them. The dashboard template is shared across services. New service = new datasource variable + new copy of the template = working dashboard in 10 minutes.

> **Mental model:** Standardization is the leverage. Each service's RED dashboard should look identical, only the labels differ. If your team's RED dashboard is hand-crafted differently from the next team's, you've reinvented dashboards-as-data instead of dashboards-as-code.

---

## 6. Layout Principles

The science of dashboard design is mostly about reducing the time from "a panel is on screen" to "the engineer knows what it means."

### 6.1 The Five-Second Test

A useful self-check: show a screenshot of the dashboard to someone who's never seen it. In five seconds, can they tell:

1. Is anything wrong?
2. If yes, which subsystem?

If the answer is no, the dashboard is too dense, the wrong things are on top, or status panels lack visual differentiation. Common fixes:

- Big-number stat panels at the top with red/yellow/green thresholds.
- Health summaries above detail panels.
- Move time-series detail to lower rows.
- Color the things that matter; gray the things that don't.

### 6.2 The information pyramid

```
┌──────────────────────────────────────────────────────────────────┐
│  ROW 1 - HEADLINE                                                │
│   ┌──────┐ ┌──────┐ ┌──────┐ ┌──────────────────────────────┐    │
│   │SLO 🟢│ │RATE  │ │ERR % │ │     OVERALL HEALTH 🟢        │    │
│   │99.94%│ │1.2k/s│ │ 0.04%│ │                              │    │
│   └──────┘ └──────┘ └──────┘ └──────────────────────────────┘    │
│                                                                  │
│  ROW 2 - PRIMARY TIME SERIES                                     │
│   ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐    │
│   │ rate over time  │ │ error rate      │ │ p50/p95/p99     │    │
│   └─────────────────┘ └─────────────────┘ └─────────────────┘    │
│                                                                  │
│  ROW 3 - BREAKDOWNS                                              │
│   per-route table | per-pod heatmap | top-N error log lines      │
│                                                                  │
│  ROW 4 - DEPENDENCIES                                            │
│   downstream services RED summary | DB / cache health            │
│                                                                  │
│  ROW 5 - SATURATION (collapsed by default)                       │
│   CPU / mem / threads / GC / pool                                │
└──────────────────────────────────────────────────────────────────┘
```

The principle: scroll-to-zero contains the answer to "is this fine?". Everything below is for diagnosis, not detection.

### 6.3 Display target matters

| Target | Layout | Panel size | Refresh |
|---|---|---|---|
| **NOC wall display** | 4–8 wide, 1080p–4K | Big, few panels per row | 30s |
| **Engineer workstation** | 12-wide grid, dual monitor | Default | 10s–30s |
| **On-call laptop** | Same as workstation | Default | 30s |
| **Phone (4 a.m. page)** | Single column, big numbers | Stat panels, no grids | 30s, manual refresh OK |

Most teams design only for the workstation case. The phone case is the one that matters during incidents.

### 6.4 Color, axis, time discipline

- **Color is semantic, not decorative.** Red = bad, green = good, gray = neutral. Stop using "auto colors" that randomize per panel.
- **Same time range** on every panel of the dashboard. Mismatched time ranges produce visual lies.
- **Y-axis units fixed** (req/s, err/s, seconds, bytes, percent). Don't let Grafana auto-scale to "ops" when you mean "req/s."
- **Y-axis lower bound** is usually 0 (not auto). Auto can hide a flatline by zooming the visible range.
- **Threshold lines** drawn from the SLO definition, not hand-tuned.
- **No green `OK` panels for things you don't actually monitor.** A green panel that means "I have no data" is the most dangerous panel on the dashboard.

---

## 7. Exemplars

The **metric → trace** jump. The single biggest UX win in modern observability.

### 7.1 What an exemplar physically is

An exemplar is a sample point attached to a histogram bucket increment that carries:

```
exemplar = {
  value:     0.847,           // the original measurement
  timestamp: 1714765432.123,
  labels: { trace_id="...", span_id="..." }
}
```

When the SDK observes a sample (say, a 847 ms request latency), it increments the appropriate histogram bucket *and* attaches an exemplar with the trace_id of the request being measured. The exemplar travels with the bucket sample through remote_write into the TSDB.

### 7.2 Configuration: SDK side

#### Prometheus client (Go)
```go
hist := prometheus.NewHistogramVec(prometheus.HistogramOpts{
    Name:    "http_request_duration_seconds",
    Buckets: prometheus.DefBuckets,
}, []string{"method", "route"})

// On each request, also include the trace_id in the exemplar
hist.WithLabelValues("POST", "/checkout").(prometheus.ExemplarObserver).
    ObserveWithExemplar(latency, prometheus.Labels{"trace_id": spanCtx.TraceID().String()})
```

#### OpenTelemetry SDK
Configure an exemplar reservoir on the histogram aggregator. Most SDKs default to a `TraceBased` reservoir that keeps exemplars only for sampled spans.

### 7.3 Storage and rendering

- **Prometheus / Mimir**: exemplars are stored alongside samples; query via `/api/v1/query_exemplars`.
- **Grafana**: enable exemplars on the Prometheus datasource; they render as small dots overlaid on the latency panel.
- **Click → Tempo / Jaeger**: configure the data source to map `trace_id` exemplar labels to the tracing data source; one click jumps to the trace view.

The result: an engineer sees a latency spike, clicks the exemplar dot in the bucket where the spike landed, and lands on the actual offending trace in Tempo. No SQL, no copy-paste.

### 7.4 When NOT to use exemplars

| Scenario | Reason |
|---|---|
| Service has no tracing | The exemplar carries trace_ids you can't follow; pointless |
| Trace sampling = 0% on the path | Exemplar trace_id leads to a not-found |
| Synthetic / RUM-only metrics | No trace_id at the source |
| High-frequency metric (1 ms histograms) | Exemplar reservoir overhead is noticeable |
| Pre-aggregated / federated metrics | Exemplars often dropped by intermediate aggregators |

> **Pitfall:** Exemplars from metrics that were sampled out of tracing are confusing — the dot exists, the link breaks. Either align sampling decisions, or filter exemplars to only those whose trace_id is known to be retained.

---

## 8. Variables, Templating, and Repeating Panels

Variables turn one dashboard into N. The convention matters because variables become part of the URL, shared in incident channels, and remembered by muscle memory.

### 8.1 Convention

| Variable | Source | Default convention |
|---|---|---|
| `$cluster` | label values from the metric label | required, always first |
| `$namespace` | depends on `$cluster` | required for k8s dashboards |
| `$service` | depends on `$namespace` | required for service dashboards |
| `$env` | static list (prod, staging, dev) | required, sticky default = prod |
| `$instance` / `$pod` | depends on `$service` | optional, for drill-down |
| `$route` | depends on `$service` | optional, RED breakdown |
| `$interval` | static (1m, 5m, 1h, $__rate_interval) | for adaptive rate windows |

### 8.2 Dependent variables

Variables can depend on each other. Configure the data source query for `$namespace` to filter by `$cluster`:

```promql
label_values(kube_namespace_status_phase{cluster="$cluster"}, namespace)
```

Now choosing `cluster` updates the namespace dropdown automatically. This is what makes the same dashboard usable across 50 clusters without duplication.

### 8.3 Repeating rows / panels

`Repeat by variable: $service` on a row tells Grafana to render that row once per value of `$service`. Combined with multi-value variables, one dashboard renders RED panels for every service in a namespace.

```
┌──────────────────────────────────────────────────────────────┐
│   [namespace: prod-checkout ▼]  [services: ALL ▼]            │
│                                                              │
│   ── checkout-api ────────────────────────────────────────── │
│   [rate]   [errors]   [duration]                             │
│                                                              │
│   ── checkout-worker ────────────────────────────────────────│
│   [rate]   [errors]   [duration]                             │
│                                                              │
│   ── checkout-db-proxy ──────────────────────────────────────│
│   [rate]   [errors]   [duration]                             │
└──────────────────────────────────────────────────────────────┘
```

### 8.4 URL-shareable state

All variables, time range, and refresh interval serialize into the URL. This means an incident comm can paste:

```
https://grafana.example.com/d/checkout-red?
  var-cluster=us-east-1&var-env=prod&from=1714760000000&to=1714765000000
```

…and every recipient sees exactly what the sender saw. Make sure your dashboards always work from URL state alone — no "click here first" preamble.

> **Pitfall:** A dashboard that requires the engineer to manually pick `cluster` and `env` after loading is one that *won't be loaded* during an incident. Sticky defaults (e.g. `prod`, current cluster) save real time.

---

## 9. Annotations

Annotations overlay events onto charts. They are the visual answer to "what changed?" — the most common diagnostic question in any incident.

### 9.1 Sources

| Source | What it annotates |
|---|---|
| **Deploy pipeline** | Vertical line on every panel at the moment a deploy completed |
| **Feature flag platform** | Flag flip events (LaunchDarkly, Statsig, Unleash) |
| **Incident management** | Incident start/end (incident.io, FireHydrant) |
| **Alert fires** | Alertmanager → annotation source |
| **Manual** | Engineer adds a note: "scaled HPA from 10 to 30" |
| **Cloud events** | Spot reclaim, region failover, AZ events |

### 9.2 Configuration

Grafana annotations come from a query against any datasource:

```promql
# Annotate every successful deploy
deploy_event{service="$service", env="$env", status="success"}
```

Or via the annotations API for ad-hoc events. Each annotation has a time, optional time-range (for ranged annotations like an incident window), text, and tags.

### 9.3 The "what changed" rule

For every panel that goes red, the engineer's first question is "what changed in the last hour?" If your annotations layer is comprehensive, the answer is on screen — vertical lines for the last deploys, flag flips, infra events. If it isn't, the engineer chases ghosts in the source repo and the wiki.

> **Mental model:** Annotations are the dashboard's memory of recent change. Without them, every chart is a cold read. With them, every chart is a story.

---

## 10. Dashboards-as-Code

Hand-built dashboards do not scale past a single team. The Grafana JSON model is verbose, opaque, and impossible to review. The only sustainable model at scale is **dashboards generated from code**, version-controlled, PR-reviewed, deployed via CI.

### 10.1 The tooling landscape

| Tool | Language | Strengths | Caveats |
|---|---|---|---|
| **Grafonnet** | Jsonnet | Mature, Grafana-blessed, full coverage | Jsonnet learning curve; verbose |
| **Tanka** | Jsonnet (Grafana Labs) | Wraps Grafonnet + k8s; first-class for Mimir/Loki/Tempo dashboards | Same Jsonnet caveats |
| **Grizzly** | YAML/Jsonnet/TS | CLI for declarative Grafana resources, drift detection | Newer, smaller community |
| **Terraform grafana provider** | HCL | Familiar to ops; integrates with TF state | HCL is awkward for nested panel structures |
| **Pulumi** | TS/Python/Go | Real programming language | Less idiomatic than Terraform for Grafana |
| **K6 Dashboard Builder / GitHub Actions** | YAML | Lightweight; pipeline-friendly | Limited panel-type coverage |
| **OTel semantic-convention generators** | Various | Auto-build dashboards from instrumentation manifest | Cutting-edge, evolving fast |

### 10.2 The pattern

```
service.yaml (in service catalog)            ─┐
  service: checkout                            │
  team: payments                               │
  language: go                                 │  inputs
  exposes: http,grpc                           │
  slo: { availability: 99.9, latency_p99: 0.5} │
                                              ─┘
              │
              ▼  generator (Jsonnet / TS)
              │
service-checkout-red.json                     ─┐
service-checkout-slo.json                      │  outputs (pushed to Grafana via API)
service-checkout-saturation.json              ─┘
```

Wins:
- Every service gets the same dashboards automatically.
- Adding a new SLO updates every relevant dashboard.
- Drift between code and live dashboards is detectable and reversible.
- Code review catches cardinality bombs, broken queries, missing thresholds before they ship.

### 10.3 Library panels

Grafana 8+ supports library panels: a panel definition lives once and is referenced by N dashboards. Update the library panel → all referrers update. Pair with dashboards-as-code: panels are library panels generated from templates; dashboards are compositions.

### 10.4 Auto-generation from semantic conventions

OpenTelemetry semantic conventions standardize attribute names (`http.method`, `db.system`, `messaging.system`, etc.). With a manifest of which conventions a service emits, you can mechanically generate the right dashboards: HTTP attributes → HTTP RED dashboard, DB attributes → DB-driver dashboard.

This is the endgame: instrumentation declares semantics; dashboards are derived. The team writes no JSON.

---

## 11. Anti-Patterns and Dashboard Sprawl

The catalogue of dashboard mistakes that drag platform productivity down. Almost every team has at least five of these.

| # | Anti-pattern | Symptom | Cure |
|---|---|---|---|
| 1 | "Every team makes their own checkout dashboard" | N teams × M dashboards; nobody knows which is canonical | Dashboards-as-code; one canonical per service via service catalog |
| 2 | The 200-panel dashboard | Loads slowly; no one reads past row 3 | Split by audience (RED, USE, SLO); link from each |
| 3 | Threshold colors that lie | Everything green during an outage | Tie thresholds to SLO; alert on the SLO, not the panel color |
| 4 | Hardcoded thresholds drift from SLOs | Panel says 500 ms; SLO says 250 ms; nobody knows which is right | Single source of truth (SLO definition); panel reads from it |
| 5 | Mismatched time ranges across panels | Visual lie ("rate is high but errors are low") | Enforce uniform time range; one panel per time scope |
| 6 | Dashboards that try to page | "I monitor this dashboard" | Use alerts; humans cannot watch a dashboard 24/7 |
| 7 | Mean instead of percentile | Mean hides tail; outages look fine | Always p95/p99 for latency; mean only when you can defend it |
| 8 | High-cardinality query blowups | Dashboard takes 30s to load; queriers OOM | Recording rules; trim labels |
| 9 | Cluster of duplicate panels with different filters | 12 versions of the same RED panel | Use variables and repeating rows |
| 10 | Stat panels with auto thresholds | Thresholds invented by Grafana defaults | Set explicitly tied to SLOs |
| 11 | Dashboards that depend on a specific Grafana plugin | Plugin abandoned; dashboard breaks | Stick to core panel types; minimize plugin reliance |
| 12 | "Test" dashboards in prod folders | Drift, confusion, accidental sharing during incidents | Org policy: test in dedicated folder, promote via dashboards-as-code |
| 13 | Logs panel without filters | Live tail of millions of lines/sec | Pre-filter to relevant streams |
| 14 | Dashboard owned by a person who left | No updates, broken, nobody knows whether it's used | Ownership = team, not person; quarterly review |
| 15 | No time annotations | "What changed?" is a manual git-log session | Annotations from deploy + flag platform |
| 16 | "Useful" panels that nobody queries | Chart for a metric no alert reads, no human reads | Quarterly: delete panels with zero views over 90d |
| 17 | Ad-hoc query in alert rule | Alert silently broken when a label changes | Promote alert queries to recording rules |
| 18 | Dashboards built before SLOs | "What does this color mean?" — nobody knows | Define SLO first, then the dashboard assembles itself |
| 19 | Variables with no defaults | Empty dashboard on first load | Sticky defaults + URL-state |
| 20 | "Nice-to-have" sub-second refresh | Dashboard hammers backend at 1s refresh | 10–30s is plenty for 99% of dashboards |

---

## 12. The Grafana Object Model in Depth

Grafana is by far the most common dashboard tool in self-hosted observability. Understanding its internal model is required for dashboards-as-code.

### 12.1 Object hierarchy

```
Org
 └── Folder (RBAC unit)
      ├── Dashboard (JSON document)
      │    ├── time range, refresh, tags, variables
      │    ├── annotations: [...]
      │    ├── templating.list: [variable, ...]
      │    └── panels: [
      │         { id, type, title, gridPos, targets, fieldConfig, ... },
      │         ...
      │       ]
      └── Library Panel (referenced from N dashboards)

Datasource (Org-scoped)
 └── PluginID (prometheus, loki, tempo, clickhouse-grafana, ...)

Alert Rule (Grafana unified alerting)
 └── Annotated with dashboard panel link
```

### 12.2 Datasources and the data-source-router pattern

In a multi-cluster, multi-env setup, you don't want one Grafana datasource per (cluster × env). You want one *logical* datasource that routes to the right physical backend based on a variable.

Pattern: a Mimir/Thanos query frontend with `Cluster` header support, and a Grafana datasource that injects the header from `$cluster`. One datasource serves N clusters. Switching clusters in the variable picks the right backend at query time.

### 12.3 Transformations

Grafana's transformation pipeline runs *after* query execution and lets you reshape data without re-querying:

| Transformation | Use |
|---|---|
| Reduce | turn a time series into a single value (last, mean, max) |
| Group by | aggregate across labels |
| Join by field | combine results from two queries |
| Organize fields | rename, hide, reorder for table display |
| Filter by name | regex on series name |
| Add field from calculation | compute a derived field client-side |

Heavy use of transformations is a smell — push the work into the query. But for stitching together two datasources (e.g., metric data + Loki count), transformations are the only option.

### 12.4 Panel-type matrix

| Panel | Best for | Avoid for |
|---|---|---|
| Time series | rate, latency, anything over time | single-value summaries |
| Stat | one big number with threshold color | trend visualization |
| Gauge | bounded percentage (utilization) | unbounded values |
| Bar gauge | top-N comparison | continuous time data |
| Table | tabular detail (top routes, top errors) | trend visualization |
| Heatmap | latency distribution over time | aggregate single-line views |
| Logs | log streams from Loki | metric data |
| Traces | trace waterfall from Tempo | non-trace data |
| Geomap | regional latency / status | non-geographic data |
| Pie chart | rarely | almost everything (humans read pie poorly) |

### 12.5 Unified alerting

Grafana 9+ unified alerting reads alert rules from any datasource (Prom, Loki, Mimir-rules, Tempo, ClickHouse). This means dashboard-time queries and alert-time queries share the same definition; the panel and the alert can never drift.

Best practice: alerts authored alongside the dashboard, version-controlled in the same repo. Promote alerts to the underlying TSDB ruler (Mimir/Prom recording-rule layer) for production reliability — don't depend on Grafana availability for alerts to fire.

### 12.6 Public dashboards

Grafana public dashboards expose a read-only URL with no auth. Useful for status pages and exec sharing; dangerous if PII or business-sensitive metrics are on the page. The default should be off.

---

## 13. Other Dashboard Tools

Grafana is the open-source default. The vendor / specialized landscape:

| Tool | Strength | Where it shines |
|---|---|---|
| **Datadog dashboards** | Tight integration with Datadog APM, polished UX, "Notebook" workflow | Vendor-uniform shops; investigation flow |
| **Honeycomb** | BubbleUp / heatmap-driven trace exploration, no preconfigured dashboards needed | High-cardinality event analysis; debugging novel issues |
| **Splunk dashboards** | SPL is powerful for complex log analytics; enterprise governance | Security, compliance, audit-heavy environments |
| **Kibana / OpenSearch Dashboards** | Faceted search, Lens drag-and-drop | ES-native log exploration |
| **CloudWatch dashboards / GCP Cloud Monitoring** | Native cloud metric integration | Cloud-only workloads, infra-heavy |
| **NewRelic One** | APM-integrated, business-flow visualizations | Enterprise APM; SaaS-focused |
| **Lightstep / Dynatrace / AppDynamics** | Trace-driven, high-end APM | Large enterprise budgets, proprietary semantics |

The general comparison:

| Capability | Grafana | Datadog | Honeycomb | Splunk |
|---|---|---|---|---|
| Multi-datasource | Yes (any) | Datadog only | Honeycomb only | Splunk + plugins |
| Dashboards-as-code | Mature | Terraform provider, JSON | UI-driven; YAML for definitions | YAML for SimpleXML |
| Free tier | OSS (full) | Limited | Limited | Limited |
| Trace exploration | Tempo plugin | Built-in APM | First-class | Limited |
| RUM | Plugin | Built-in | Plugin | Plugin |
| Anomaly detection | Plugins, manual rules | Built-in (Watchdog) | Built-in (BubbleUp) | Plugins |

> **Mental model:** Pick Grafana for an open, multi-vendor stack. Pick Datadog/Dynatrace for vendor-integrated speed-to-value. Pick Honeycomb when your problems are high-cardinality "weird" issues that pre-built dashboards can't capture. Pick Splunk when audit/governance are non-negotiable.

---

## 14. RUM and Synthetic Dashboards

Server-side dashboards measure the *system*. RUM and synthetic dashboards measure the *user experience*.

### 14.1 RUM (Real User Monitoring)

Captured from real browser/mobile sessions: page load, web vitals, JS errors, AJAX failures.

| Web Vital | What it measures | SLO target (Google) |
|---|---|---|
| **LCP** (Largest Contentful Paint) | Time to render largest visible element | ≤ 2.5 s |
| **INP** (Interaction to Next Paint) | Worst-case input latency over the session | ≤ 200 ms |
| **CLS** (Cumulative Layout Shift) | Layout instability | ≤ 0.1 |
| **TTFB** (Time to First Byte) | Server response time | ≤ 0.8 s |
| **FCP** (First Contentful Paint) | First text/image rendered | ≤ 1.8 s |

A RUM dashboard breaks each Vital down by:
- Geographic region (LCP differs wildly by location)
- Device type (mobile is slower than desktop)
- Browser (Safari ≠ Chrome)
- Network type (4G ≠ wifi)
- Page route (`/checkout` vs `/`)
- Build version (regressions on deploy)

### 14.2 Synthetic monitoring

Scripted user journeys (login → search → checkout) executed at fixed intervals from multiple regions.

Dashboard pattern:
- Top: world map with regional pass/fail status.
- Middle: per-step latency over time (where in the journey is slow?).
- Bottom: failure log + screenshot from the most recent failure.

Both RUM and synthetic dashboards live in their own namespace (separate from server-side RED). Cross-link: from the RUM dashboard for `/checkout`, link to the server-side RED for the checkout service.

### 14.3 RUM vs Server-side: the gap

Server-side P99 latency: 250 ms. RUM P99 LCP: 4.2 s. The gap is *the network, the rendering, the JS execution, the third-party scripts*. Without RUM, you don't see the gap; you ship "fast" services that users experience as slow.

---

## 15. Mobile / On-Call Dashboard Pattern

The hardest dashboard to design well. Used at 4 a.m. on a phone in a dark room; must surface what matters in seconds.

### 15.1 Layout

```
┌────────────────────────────────┐
│  ALERT: checkout p99 burning   │
│  burn-rate 14.4× | budget 12%  │
│  --------------------------    │
│  CURRENT STATUS                │
│  ┌──────┐ ┌──────┐ ┌──────┐    │
│  │RATE  │ │ERR % │ │ p99  │    │
│  │1.2k/s│ │ 3.4% │ │820ms │    │
│  └──────┘ └──────┘ └──────┘    │
│  --------------------------    │
│  WHAT CHANGED (last 1h)        │
│  ▸ deploy v2.34.1 at 03:42     │
│  ▸ pricing-svc canary 03:48    │
│  --------------------------    │
│  TOP ERRORS (last 5m)          │
│  • UpstreamTimeout: pricing    │
│    ............ 487 events     │
│  • DBConnRefused ... 12 events │
│  --------------------------    │
│  ACTIONS                       │
│  [ Open runbook ]              │
│  [ Roll back v2.34.1 ]         │
│  [ Page secondary ]            │
└────────────────────────────────┘
```

### 15.2 Principles

- **Single column.** Side-by-side panels are unreadable on a 6-inch screen.
- **Big numbers.** Stat panels with thresholds; not time series.
- **What changed.** Annotation list at the top, not buried.
- **One-click actions.** Runbook link, rollback link, escalation link.
- **No sub-second refresh.** Save the engineer's data plan.

### 15.3 PagerDuty / Opsgenie integration

The page itself should embed the dashboard URL with pre-set time range and `$service` variable. The engineer taps the page → mobile dashboard → triage. Latency from "page received" to "first useful information" is the metric to optimize.

---

## 16. Incident-Time Dashboard Hygiene

During an incident, dashboards take a different role: not "is this fine" (we know it isn't), but "what is happening, what changed, what mitigations to try."

### 16.1 Pre-built per failure mode

For every well-understood failure mode, build a dashboard *in advance*. Examples:

- **Database degraded**: pg_stat_statements top queries, lock chains, replication lag, connection pool saturation.
- **Cache cold start**: cache hit ratio, backend QPS, memcached/redis evictions.
- **Vendor degradation**: per-vendor latency, success rate, circuit-breaker state, fallback path traffic.
- **DDoS / traffic spike**: per-source IP traffic, WAF rule fires, edge connection state.
- **K8s control plane**: API server latency, etcd commit duration, scheduler queue.

Linked from runbooks; linked from alert annotations.

### 16.2 The status tile

A simple binary panel at the top of an incident dashboard: are we currently meeting our SLO over the past 5 minutes? Big red/green block, no nuance. During an active incident, this is the "is it over?" indicator.

### 16.3 Comms board

For long incidents (> 30 minutes), a dedicated dashboard for the comms lead:
- Status page metrics (subscriber count, recent updates)
- Customer ticket inflow rate
- Twitter / social mentions (if integrated)
- Internal Slack channel message rate

---

## 17. Worked Example: `/checkout`

Concretizing all of the above: the dashboard set for the `/checkout` request from the ROADMAP §8 trace.

### 17.1 Folder structure (Grafana)

```
Folder: payments
 ├── checkout/
 │    ├── checkout-slo                  (audience: SRE, leadership)
 │    ├── checkout-red                  (audience: service team)
 │    ├── checkout-saturation           (audience: service team)
 │    ├── checkout-deploy               (audience: release eng)
 │    ├── checkout-incident             (audience: IC during incident)
 │    └── checkout-rum                  (audience: web team)
 └── pricing/
      └── ...
```

### 17.2 SLO dashboard (top of the triage tree)

```
┌─────────────────────────────────────────────────────────────────┐
│  CHECKOUT  /  SLO  /  prod  / us-east-1                         │
├─────────────────────────────────────────────────────────────────┤
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐     │
│  │ AVAILABILITY   │  │ LATENCY P99    │  │ ERROR BUDGET   │     │
│  │ 99.93%         │  │ 412 ms         │  │ 67% remaining  │     │
│  │ SLO: 99.9%     │  │ SLO: < 500ms   │  │ window: 28d    │     │
│  └────────────────┘  └────────────────┘  └────────────────┘     │
│                                                                 │
│  Burn rate (multi-window)                                       │
│  ┌────────────────────────────────────────────────────────┐     │
│  │  ─── 5m   ─── 30m  ─── 6h                              │     │
│  │           14.4 ░░░░░░░░░░░░░░░░░░░ critical            │     │
│  │            6.0 ─░░──────────────── high                │     │
│  │            1.0 ─────────────────── steady              │     │
│  └────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────┘
```

PromQL:
```promql
# Availability SLI (success ratio over window)
sum(rate(checkout_requests_total{status!~"5.."}[5m]))
/ sum(rate(checkout_requests_total[5m]))

# Burn rate (5m window)
(1 - sum(rate(checkout_requests_total{status!~"5.."}[5m]))
   / sum(rate(checkout_requests_total[5m])))
/ (1 - 0.999)

# Error budget remaining (28d window)
1 - (
  sum(increase(checkout_requests_total{status=~"5.."}[28d]))
  / (sum(increase(checkout_requests_total[28d])) * (1 - 0.999))
)
```

### 17.3 RED dashboard

Standard three-column layout. Per-route breakdown via `$route` variable. Exemplars enabled on the duration panel — clicking a slow latency dot jumps to Tempo.

### 17.4 Saturation dashboard

USE for the checkout service:
- CPU / memory / GC pause time
- HTTP server thread pool utilization
- DB connection pool usage
- Outbound HTTP connection pool usage (to pricing, payments)
- Kafka producer queue depth (to outbox)

### 17.5 Deploy dashboard

A compare-by-version layout: `$version_old` and `$version_new` variables; every panel renders both side-by-side with a vertical annotation at the deploy moment.

### 17.6 Incident dashboard

Pre-built for known failure modes:
- Pricing vendor degraded → upstream latency, circuit breaker state, fallback path traffic.
- Database slow → pg_stat_statements top queries, connection pool, lock waits.
- Cache cold → cache hit ratio over time, origin QPS spike.

### 17.7 Executive dashboard

```
┌──────────────────────────────────────────────────────────────┐
│  CHECKOUT  /  EXECUTIVE  /  this quarter                     │
├──────────────────────────────────────────────────────────────┤
│  Successful checkouts ...... 41.2 M                          │
│  SLO compliance ............. 99.94% (target 99.9%) ✓        │
│  Failed checkouts ..........  24,800 (~$1.2M lost*)          │
│  Open incidents (≥SEV-2) ...      2                          │
│  MTTM (this quarter) .......  12 min                         │
│  Largest contributing factor: pricing vendor (3 incidents)   │
└──────────────────────────────────────────────────────────────┘
```

The exec dashboard contains no PromQL the reader needs to understand. Numbers are precomputed via recording rules; the dashboard just renders them.

---

## 18. Pitfalls

A consolidated list specific to dashboards (orthogonal to the query-layer pitfalls in chapter 10).

| # | Pitfall | Impact |
|---|---|---|
| 1 | Building a dashboard before defining the question | Telemetry wallpaper |
| 2 | Mean instead of percentile | Tail behavior hidden |
| 3 | Mismatched time ranges across panels | Visual lies |
| 4 | Hardcoded thresholds drift from SLOs | Color says fine, SLO says alarm |
| 5 | Auto-axis lower bound | A flatline looks normal at zoom |
| 6 | Using stat panels for trends | Loses temporal context |
| 7 | Pie charts | Humans read them poorly |
| 8 | Dashboards owned by individuals | Bit-rot when the person leaves |
| 9 | No annotations | Can't answer "what changed" |
| 10 | Sub-second auto-refresh | Backend overload |
| 11 | One mega-dashboard for all audiences | Useful to nobody |
| 12 | No URL-state defaults | Empty dashboard on first load |
| 13 | Hand-built, not version-controlled | Drift, no review, no rollback |
| 14 | Unfiltered logs panel | Live tail of millions/sec |
| 15 | "Test" dashboards in prod folder | Confusion during incidents |
| 16 | Plugin-only panel types | Plugin abandonment kills the panel |
| 17 | Live-tail metric over 90d | Storage scan + dashboard hang |
| 18 | Dashboards as a substitute for alerts | Humans can't watch 24/7 |
| 19 | High-cardinality groupings | Panel slow + queriers OOM |
| 20 | No mobile/incident layout | Useless during the moments that matter |

---

## 19. Mental Models

> **A dashboard is a question, not a display.** If you cannot state the question in one sentence, redesign.

> **Dashboards are a triage tree.** Top tells you whether, next tells you which subsystem, bottom tells you why. Each level zooms in.

> **Standardization is leverage.** Every service's RED panel should be visually identical; only labels differ.

> **The five-second test is non-negotiable.** Open the dashboard cold; in five seconds, can you tell if anything is wrong?

> **The dashboard's color is a contract with the SLO.** Red means budget burning; green means budget safe. Anything else is opinion.

> **Annotations are the dashboard's memory of recent change.** Without them, every chart is a cold read.

> **Exemplars close the metric → trace gap.** One click from "p99 spiked at 14:32" to the actual offending trace.

> **Dashboards that page are dashboards that fail.** Humans cannot watch 24/7. Use alerts.

> **Dashboards-as-code is the only sustainable model.** Hand-built dashboards do not scale past one team.

> **The incident-time dashboard is a different artifact than the daily-driver dashboard.** Build both.

---

## 20. Production-Ready Dashboard Checklist

Print this. Run it on every dashboard before it goes into a prod folder.

- [ ] **Audience is named** (SLO / RED / USE / capacity / incident / exec) and matches the panels.
- [ ] **One sentence** describes the question this dashboard answers.
- [ ] **Title and tags** match the convention; folder placement matches audience.
- [ ] **Variables** have sticky defaults; first-load is not empty.
- [ ] **Time range default** matches audience (1h for RED, 28d for SLO, etc.).
- [ ] **Refresh interval** is 10s–30s, not sub-second.
- [ ] **Five-second test** passes: a cold reader sees status in 5 seconds.
- [ ] **Mobile layout** works (single column, stat panels prominent).
- [ ] **All panels** use the same time range (no panel-level overrides unless intentional).
- [ ] **Y-axis units fixed**, lower bound set explicitly.
- [ ] **Colors are semantic** (red = bad, green = good); no auto-rotated palettes.
- [ ] **Thresholds tied to SLOs**, not invented; sourced from the SLO definition.
- [ ] **Annotations enabled** for deploys, flag flips, incidents.
- [ ] **Exemplars enabled** on histogram panels (where tracing exists).
- [ ] **Linked to runbook** at the top of the dashboard.
- [ ] **Linked to next dashboard** in the triage tree (RED → USE; SLO → RED).
- [ ] **No high-cardinality `group by`** that explodes at scale.
- [ ] **Heavy queries** moved to recording rules.
- [ ] **Dashboards-as-code** source-of-truth lives in a repo; manual edits prohibited.
- [ ] **Owner is a team, not a person**; ownership in the dashboard description.
- [ ] **Last-reviewed date** in the description; quarterly review on the calendar.

---

**TL;DR dashboards.** *A dashboard is a question for a specific audience. Five archetypes (SLO, RED, USE, incident, exec) cover almost everything; build each separately. RED + USE are the daily drivers. Standardize layout so on-call brains have spatial memory. Variables and dashboards-as-code make N services scale to one template. Exemplars close the metric → trace gap; annotations close the "what changed" gap. The dashboard's job is to start the triage tree, not to replace it. The single best test of dashboard quality is the five-second test on a phone at 4 a.m.*
