# 00 — Mental Models: The Vocabulary Every Later Chapter Assumes

> Before any tool, any YAML, any architecture diagram: a shared vocabulary. This chapter is the dictionary the rest of this folder writes against. If a Staff Engineer cannot use these terms with surgical precision in a 1:1 with leadership, every later argument becomes a vocabulary fight rather than a technical one.

The single most expensive failure mode in observability is not a bad tool choice. It is two senior engineers arguing for thirty minutes because one of them means "any alert" by the word *page* and the other means "an automated, after-hours escalation that wakes a human." This chapter exists so that argument never happens on your team.

Everything later in the folder — from `doc 01`'s architecture diagrams to `doc 13`'s SLO math to `doc 18`'s cardinality budgets — assumes this vocabulary. Skim it on first read; come back the moment a later chapter uses a term in a way that surprises you.

---

## Table of Contents

1. [The four signals (and why "logs vs metrics" is a false dichotomy)](#1-the-four-signals)
2. [Observability vs monitoring](#2-observability-vs-monitoring)
3. [The golden signals: latency, traffic, errors, saturation](#3-the-golden-signals)
4. [USE method (resources) vs RED method (services)](#4-use-vs-red)
5. [Cardinality, dimensionality, and the cost of a label](#5-cardinality)
6. [Sampling: head, tail, adaptive, reservoir](#6-sampling)
7. [Aggregation, downsampling, and the lossiness ladder](#7-aggregation)
8. [Pull vs push: the architectural argument that won't die](#8-pull-vs-push)
9. [Push-gateway, scrape, exposition format: micro-vocabulary](#9-exposition-format)
10. [Histogram, summary, exemplar: the three faces of a percentile](#10-histogram-summary-exemplar)
11. [SLI, SLO, SLA, error budget, burn rate](#11-sli-slo-sla)
12. [MTT* salad: detect, acknowledge, mitigate, recover, between failures](#12-mtt-salad)
13. [Incident, outage, degradation, near-miss, page, alert, ticket](#13-incident-vocabulary)
14. [Toil, glue work, and the 50% rule](#14-toil)
15. [Reliability math you should know cold](#15-reliability-math)
16. [Latency math: percentiles, the long tail, Little's law](#16-latency-math)
17. [The observability tetrahedron and the correlation skeleton](#17-tetrahedron)
18. [Common confusions to inoculate against](#18-common-confusions)
19. [The one-page glossary](#19-glossary)

---

## 1. The Four Signals

Modern observability has converged on **four signal types**. Each answers a different question, scales differently, and stores differently. Conflating them is the single most common architectural mistake in homegrown stacks.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ SIGNAL    │ ANSWERS                       │ SCALES WITH       │ COSTLIEST   │
├──────────────────────────────────────────────────────────────────────────────┤
│ Metric    │ "what is the rate / latency  │ cardinality       │ memory     │
│           │  / error count over time?"   │ (unique series)   │ per series │
│           │                              │                   │            │
│ Log       │ "what specifically happened  │ traffic volume    │ storage    │
│           │  on this request / box?"     │ (events × bytes)  │ + index    │
│           │                              │                   │            │
│ Trace     │ "where did the time go       │ traffic × spans   │ storage    │
│           │  across services?"           │ × propagation     │ + bandwidth│
│           │                              │                   │            │
│ Profile   │ "where in the code did the   │ services ×        │ storage    │
│           │  CPU / memory go?"           │ profile types     │ + symbol DB│
└──────────────────────────────────────────────────────────────────────────────┘
```

A metric is a *named time series* — a sequence of `(timestamp, value)` tuples sharing a label set. A log is an *event with structure* — a record at a point in time with arbitrary attributes. A trace is a *DAG of spans* — operations with parent-child relationships across processes. A profile is a *weighted set of stack traces* over a time window.

> **Mental model:** metrics are what you alert on, logs are what you read during triage, traces are how you follow a single request across services, profiles are how you find the line of code at fault. You need all four. Pretending you can replace any one with another is the source of long, expensive arguments.

The "logs vs metrics" debate is misframed. The right question is *what aggregation has been pre-applied?* A counter is a log line, pre-aggregated by the SDK. A histogram is many log lines, bucketed and compressed. Once you see them on the same axis (raw events ↔ pre-aggregated), the architectural choice — what to store with which fidelity — becomes obvious.

### 1.1 Why four, not three?

Pre-2019 observability was the "three pillars" (metrics, logs, traces). Continuous profiling crossed the maturity threshold around 2021 (Pyroscope, Parca, Polar Signals, Datadog Profiling), and the ecosystem now treats it as the fourth signal. Some practitioners add **events** (deploy markers, config changes) as a fifth — but events are best modelled as a special class of log with high prior on relevance. Don't fragment the taxonomy further than you need to.

---

## 2. Observability vs Monitoring

These are not synonyms. Mixing them up is the single most common vocabulary error in this field.

| Term | Strict definition | What it lets you do |
|---|---|---|
| **Monitoring** | Watching *predefined* signals against *predefined* thresholds, raising alarms on *known failure modes*. | Catch problems you've seen before. Page when CPU > 90%, disk > 80%, p99 > 500ms. |
| **Observability** | The property that internal state can be *inferred* from external outputs, *without* knowing in advance what questions will be asked. | Diagnose problems you've never seen. Ask "why was this user's checkout 4× slower than usual yesterday?" with no pre-existing dashboard. |

Monitoring is a *strict subset* of observability. A system can be heavily monitored and still un-observable: dozens of dashboards, dozens of alerts, but the moment a novel failure happens nobody can answer "why?" without ssh-ing to a box. Conversely, a system can be highly observable with very few alerts — the alerts are the thin tip of a deep query layer.

> **Mental model:** Monitoring is a *finite list of pre-canned questions*. Observability is the *infinite question budget* the data lets you ask. Most stacks are over-monitored and under-observable: they pay storage costs for signals nobody can query in a way that answers a new question.

### 2.1 The Cindy Sridharan rule

Cindy Sridharan, in her 2018 Distributed Systems Observability paper (the canonical reference), framed it as: *monitoring tells you when something is broken; observability tells you why*. The Staff Engineer addition: *monitoring scales with the number of failure modes you predict; observability scales with your ability to ask new questions*. The first is bounded; the second is the platform's actual differentiator.

### 2.2 The "how observable is this?" rubric

Three questions. If you can't answer "yes" to all three, you are monitoring, not observing.

1. **Cardinality.** Can I filter or group by an *unanticipated* high-cardinality dimension (user_id, request_id, region × tenant) without re-instrumenting?
2. **Correlation.** Can I jump from a metric anomaly to the specific traces that caused it in one click?
3. **Replay.** Can I reconstruct the state of any specific request that happened up to 7 days ago?

A typical Datadog deployment is strong on (1), weak on (3). A typical Loki deployment is strong on (3), weak on (1). A mature OTel + ClickHouse lakehouse is strong on all three but expensive. The trade-off is real and the chapter on lakehouses (`doc 35`) returns to it.

---

## 3. The Golden Signals

Coined in the Google SRE Book (Beyer et al., 2016) — the **four golden signals** are the universal "first dashboard" for any service.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  LATENCY    │ How long does a request take? (use percentiles, NEVER    │
│             │  mean — the mean lies about tail behavior)               │
│             │                                                          │
│  TRAFFIC    │ How much demand is on the service? (RPS, msg/sec,        │
│             │  concurrent sessions)                                    │
│             │                                                          │
│  ERRORS     │ Rate of failed requests (HTTP 5xx, gRPC non-OK,          │
│             │  business-level "wrong answer", silent corruption)      │
│             │                                                          │
│  SATURATION │ How "full" is the service? (queue depth, mem pressure,   │
│             │  thread pool exhaustion). Approaches 1.0 → outage near.  │
└─────────────────────────────────────────────────────────────────────────┘
```

Two non-negotiable rules:

**Rule 1: Latency must be split into success and failure latencies.** A failed request that returns in 1ms because the auth check rejected it should not pollute your "successful checkout time" graph. Compute two histograms: `latency_seconds_bucket{outcome="success"}` and `{outcome="error"}`.

**Rule 2: Errors must be expressed as a ratio, not a count.** "We had 47 errors yesterday" is meaningless. "0.04% of requests failed" is actionable. Always alert on `errors / total`, never on `errors` alone — otherwise alert thresholds break the moment traffic shifts.

### 3.1 Saturation is the most misunderstood

Saturation is *not* utilization. It's the *queueing* that happens when utilization approaches 1. Examples:

| Resource | Utilization | Saturation |
|---|---|---|
| CPU | 65% busy | run-queue length > 0 (load average exceeding cores) |
| Memory | 80% used | swap pressure, oom-killer near, page-fault rate climbing |
| Disk | 60% IOPS | await > device capability, iowait > 5% |
| Connection pool | 40 of 100 in use | pool wait time > 0, connection acquisition latency > 0 |
| Goroutine scheduler | N goroutines | runnable but not running (sched_lat) |

**Why it matters.** Utilization saturates linearly; latency saturates exponentially as you approach 100% (Little's law, §16). A queue at 90% utilization has 10× the wait time of one at 50%. *Saturation* signals tell you "an outage is near" *before* the latency-and-error symptoms show up. They are the leading indicator; latency and errors are the lagging ones.

---

## 4. USE vs RED

Two complementary methods, both mandatory.

### 4.1 USE — for resources (Brendan Gregg, 2012)

For *every resource*, check three things:

- **Utilization** — fraction of time / capacity busy
- **Saturation** — degree of queued / overflowing demand
- **Errors** — error count

A resource here means: a CPU core, a NIC, a disk, a memory bank, a thread pool, a connection pool, an object pool, a kernel structure. Anything finite that work queues for.

### 4.2 RED — for services (Tom Wilkie, 2015, originally for microservices)

For *every service*, check three things:

- **Rate** — requests per second
- **Errors** — error rate (ratio)
- **Duration** — latency distribution (percentiles)

### 4.3 USE × RED is a 2-D matrix, not a choice

```
                 ┌───────────┬──────────────────────────────────────┐
                 │    USE    │           RED                        │
                 │  (Brendan │     (Tom Wilkie's RED)               │
                 │   Gregg)  │                                      │
┌────────────────┼───────────┼──────────────────────────────────────┤
│ Subject        │ Resources │ Services / endpoints / user journeys │
│ Per-instance?  │ Yes       │ Yes (also aggregate per service)     │
│ Three letters  │ U·S·E     │ R·E·D                                │
│ When to use    │ Capacity  │ User-experience health               │
│                │ planning, │ SLO definition, alerting on impact   │
│                │ saturation│                                      │
│                │ alarms    │                                      │
└────────────────┴───────────┴──────────────────────────────────────┘
```

You need both, always. RED tells you "users are unhappy"; USE tells you "this resource is why." A platform team that only built RED dashboards will catch outages but spend an hour during each one finding the saturated resource. A platform team that only built USE dashboards will know which CPU is pegged but not whether it's affecting users. Build both, link them at the dashboard layer (`doc 11`).

---

## 5. Cardinality

Cardinality is the single biggest cost driver in metrics-land, and the second-biggest in logs/traces. Internalize this section or you will rebuild your TSDB cluster twice.

### 5.1 The definition

For a metric, **cardinality = the number of unique time series**. A series is uniquely identified by `(metric_name, label_1=value_1, label_2=value_2, ...)`. Add one new label with N unique values and your cardinality multiplies by N.

```
http_requests_total{method, status, route}
                     ↑5     ↑10    ↑200
                     = 10,000 series

http_requests_total{method, status, route, customer_id}
                     ↑5     ↑10    ↑200    ↑1,000,000
                     = 10,000,000,000 series  ← Prometheus dies
```

`customer_id` as a metric label = death. `customer_id` as a log attribute or trace attribute = fine, because logs and traces don't index by it the same way.

### 5.2 The intuition

A TSDB stores one *chunk* per series per ~2-hour block, plus an inverted index entry per (label_name, label_value) → set of series. Memory cost is roughly `O(active_series × metadata_per_series)` — a healthy Prometheus uses ~3 KB RAM per active series. 10M series = 30 GB RAM minimum, before any actual sample storage.

The reason `customer_id` kills you isn't sample storage — it's that the inverted index has to fit in memory at query time. A `topk(10, ...)` query that has to scan 10B label combinations to find the top 10 will OOM the query node.

### 5.3 The defenses (in order of preference)

1. **Don't add the label.** Move high-cardinality dimensions to logs/traces.
2. **Aggregate at the collector.** Drop the label there before ingestion.
3. **Top-K via sketch.** HyperLogLog or Count-Min-Sketch to capture distribution without storing every value.
4. **Per-tenant cardinality limits** in the TSDB (Mimir, Cortex, VictoriaMetrics support this).
5. **Reservoir sampling for exemplars** — a metric bucket carries N sample trace_ids that link back to specific traces (`doc 06`).

Chapter `doc 18` is dedicated to cardinality. Don't skip it.

### 5.4 The "is this a label?" rubric

Three questions. If any answer is "no," it's not a label.

- **Will I aggregate over it?** (sum, rate, group by) — if not, it doesn't need to be in the index.
- **Is its value-space bounded?** ( <100 unique values is comfortable, <1000 is OK, >10k is danger). If unbounded (user_id, IP, session_id, request_id), no.
- **Will I alert on it?** Alerts must be evaluated on bounded series. Alerting on `error_rate{user_id="..."}` means N million alert rules.

If "I just want to filter by it sometimes," that's a *log* attribute, not a metric label. Promote it back later if a real aggregation use-case emerges.

---

## 6. Sampling

Three different problems sharing one word.

### 6.1 The four kinds of sampling

| Kind | Decided where | Decides what to keep | Loses |
|---|---|---|---|
| **Head sampling** | At the SDK, *before* propagation | All spans of selected traces | Visibility into rare/error traces if not selected |
| **Tail sampling** | At the gateway, *after* trace assembly | Traces matching a policy (errors, slow, rare) | Real-time export (must wait the assembly window) |
| **Adaptive sampling** | Dynamically (load-based) | Higher rate for rare classes, lower for hot paths | Reproducibility |
| **Reservoir sampling** | At metric SDK | N sample exemplars per histogram bucket | Statistical fairness if writes are bursty |

The **head vs tail** distinction is the one most often confused. A head decision is fast and cheap but commits before you know if the trace was interesting. A tail decision is correct but adds 30s of latency and requires holding all spans in memory until the trace is "complete." Almost always you want both: head-sample 1% as a baseline, then tail-keep 100% of error/slow traces.

### 6.2 The cardinal sampling rule

**The sampling decision must be consistent across all services in a single trace.** If service A samples-in and service B independently samples-out, you have a *broken* trace — half its spans missing, useless. Encode the decision in the lower bits of `trace_id`, or in the `tracestate` header, so service B sees what A decided and inherits.

This is the single biggest bug I see in homegrown tracing — engineers sample per-process and never realize their trace coverage is full of holes.

### 6.3 Sampling is also a logs problem

Logs sampling is rarer but emerging — the pattern is *dynamic verbosity*: emit DEBUG-level logs only when (a) the request was sampled-in for tracing, or (b) the request errored, or (c) the request crossed the latency P99. Otherwise drop DEBUG at the SDK. This pattern lets you keep DEBUG visibility on the rare/important traffic while INFO is the baseline for the bulk. Tools: zerolog with conditional output, slog with a custom handler, OTel logs SDK with a custom processor.

---

## 7. Aggregation

Aggregation is the trade-off between fidelity and cost. The fundamental rule:

> **Aggregation is one-way.** Once you've aggregated, you cannot un-aggregate. So aggregate at the *latest reasonable point* and keep raw signals for hot windows.

The lossiness ladder:

```
RAW EVENTS                   ← can ask any question (logs)
   │
   ▼  bucket by minute
COUNTERS / GAUGES            ← can ask "rate" questions (metrics)
   │
   ▼  bucket by 5 minutes
DOWNSAMPLED METRICS           ← can ask trends, not bursts
   │
   ▼  histogram buckets
PERCENTILES                   ← can ask "what's p99?", not "exactly this trace"
   │
   ▼  rolling window stats
ANOMALY SCORES                ← can ask "is anything weird?", not "what was the value?"
```

Each step down loses information; each step down lowers storage cost. The retention tier strategy from `doc 18` is just this ladder applied across time: hot raw, warm 1-min, cold 5-min, archive 1-hour.

### 7.1 The dimensional collapse problem

A common bug: aggregating early, then needing the dimension you collapsed. Example: dropping `region` to save cardinality, then a regional outage hits and nobody can confirm impact-by-region. Defense: *every metric should ship with its dimensions documented*, including which ones are dropped at which stage. Treat dropped dimensions as a deliberate trade, not a default.

---

## 8. Pull vs Push

The pull-vs-push debate is older than Prometheus. Both work; both have failure modes; the right answer is "both, in different layers."

### 8.1 Pull (Prometheus model)

```
Prometheus server  ─── HTTP GET /metrics ───→  service:8080/metrics
                                                  ↑
                                          service exposes
                                          /metrics endpoint
```

**Pros:**
- Server controls scrape interval and concurrency.
- Service health is observable as `up{job="..."}` (scrape failure is its own metric).
- Easy to aggregate at the server (one place to look).

**Cons:**
- Doesn't work for short-lived jobs (CronJobs, batch); they finish before the scraper arrives.
- Doesn't work for pure client-side workloads (mobile apps, JS browsers); those can't be scraped.
- Service discovery becomes the server's problem.

### 8.2 Push (OTLP model)

```
Service  ─── OTLP/gRPC push ───→  Collector / backend
```

**Pros:**
- Works for ephemeral / client-side workloads (mobile, batch, edge).
- Service controls when it emits — bursty workloads can backpressure themselves.
- Decouples from server-side discovery.

**Cons:**
- Service health is *not* automatically observable (no equivalent of `up`).
- Receiver must scale to handle aggregate push load.
- Backpressure is harder — if the receiver is slow, the service has to buffer.

### 8.3 The hybrid that wins in practice

In a healthy production stack:
- Long-lived services and Kubernetes pods → **pull** (Prometheus scrape).
- Short-lived jobs, Lambdas, edge, mobile → **push** (OTLP, Pushgateway).
- Logs and traces → **push** (always — there's no static endpoint to scrape).

Don't burn ink on which is "better." Pick by the workload's lifetime: anything <30s lifetime, push; anything >30s, prefer pull.

---

## 9. Exposition Format / Pushgateway / Scrape

Micro-vocabulary that comes up constantly.

| Term | Meaning |
|---|---|
| **Exposition format** | The text format Prometheus parses at `/metrics`. One metric per block, `# HELP`, `# TYPE`, then samples. |
| **OpenMetrics** | The CNCF standardization of exposition format. Mostly a superset; supports exemplars, native histograms. |
| **Scrape** | One HTTP GET against `/metrics`, parse, ingest. Default interval 15s. |
| **Scrape interval** | The cadence (e.g., 15s). Shorter = higher cardinality cost (more samples per series), tighter alerts; longer = cheaper, blurrier. |
| **Pushgateway** | A Prometheus-shipped buffer that *accepts* pushes from short-lived jobs and *exposes* them for scraping. Anti-pattern for everything except cronjobs. Don't use it as a general push endpoint. |
| **Native histogram** | New (Prometheus 2.40+, 2022) sparse histogram format. ~10× cheaper than classic bucketed histograms; sub-millisecond resolution. Adoption is growing through 2026. |
| **OTLP** | OpenTelemetry's wire protocol. Push-based, gRPC or HTTP/protobuf, supports all four signals. |
| **`remote_write`** | Prometheus's outbound push protocol. Used to ship from a local Prometheus to Mimir/Thanos/etc. Snappy-compressed protobuf over HTTP. |

---

## 10. Histogram, Summary, Exemplar

The three constructs you need to express "what's the latency distribution?" The choice determines what queries you can answer.

### 10.1 Counter

Monotonically increasing integer (`http_requests_total`). Cheap. The PromQL `rate()` function turns it into per-second velocity.

### 10.2 Gauge

Instantaneous value, can go up or down (`memory_used_bytes`, `queue_length`). Read directly.

### 10.3 Summary

Pre-computes percentiles at the SDK (P50, P95, P99). One sample per percentile per scrape. **Cheap on storage, useless across instances.** You cannot aggregate summaries across N pods to get the fleet's p99 — percentiles don't compose. Avoid summaries unless you have one process and don't care about aggregation.

### 10.4 Histogram

Bucketed counts (`<= 0.1s: 130`, `<= 0.5s: 412`, ...). Computes percentiles at *query time* via `histogram_quantile()`. **Composable across instances** — sum the buckets, then query. The default choice for service latency.

**Bucket layout matters.** Default Prometheus buckets are not great for sub-millisecond services. Pick buckets to bracket your SLO:

```
SLO: p99 < 200ms
Buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 1, 2, 5, 10]
                                                  ↑ tight resolution near the SLO
```

**Native histograms** (Prometheus 2.40+) replace bucket choice with auto-scaled exponential buckets, much smaller on the wire. Use these for new services; classic histograms still dominate older deployments through 2026.

### 10.5 Exemplar

A pointer attached to a histogram bucket linking to a *specific trace_id* whose latency landed in that bucket. Powers the "click on the slow point in Grafana → jump to the trace" flow. Critical for triage; covered in `doc 06` and `doc 11`.

```
http_request_duration_seconds_bucket{le="0.5", route="/checkout"} 4521 # {trace_id="a1b2c3"} 0.43 1700000000
                                                                    ↑      ↑                   ↑
                                                                    count  trace_id of one     trace's actual
                                                                           sample in bucket    latency value
```

**Exemplars are the single highest-leverage feature most teams forget to enable.** They cost ~2% storage overhead and turn metric panels into trace-jumping triage tools.

---

## 11. SLI / SLO / SLA

These are not synonyms. The Staff Engineer who uses them precisely earns trust quickly.

```
SLI (Indicator)    A measurement of user experience.
                   "Proportion of HTTP requests to /checkout that
                   returned 2xx and completed within 500ms."
                   Always a ratio: good_events / total_events.

SLO (Objective)    Internal target on an SLI over a window.
                   "99.9% of /checkout requests over rolling 28 days."
                   Set by engineering + product. The bar.

SLA (Agreement)    External, contractual promise to customers.
                   Usually laxer than the SLO, with credits for breach.
                   "We credit 10% if uptime < 99.5% calendar month."

Error budget       Allowed bad events in the window.
                   = (1 - SLO) × total events
                   The lever that makes velocity vs reliability explicit.

Burn rate          (current bad event rate) / (steady-state allowed rate)
                   Burn rate of 1 = budget exhausts on schedule.
                   Burn rate of 14.4 = 30-day budget burns in 50 hours.
                   Multi-window multi-burn-rate alerting (doc 12, 13).
```

### 11.1 The five SLI design rules

1. **SLIs measure the user's experience.** "CPU < 80%" is *not* an SLI. "Page load < 1s" is.
2. **SLIs are ratios.** `good / total`. Ratios alert and budget cleanly.
3. **2–4 SLIs per critical user journey.** Not per service. Per *journey* — checkout, signup, search, etc.
4. **The SLO target is a business choice.** 99.9% vs 99.99% is a 10× cost difference. Engineering doesn't pick alone.
5. **An SLO without an error budget is decoration.** The budget is the lever that governs velocity.

`doc 13` is dedicated to SLO engineering. It's the chapter that turns this folder from "monitoring" into "SRE."

---

## 12. MTT* Salad

Mean Time To something. Practitioners conflate these constantly. The **definitions diverge** during incidents.

| Term | Stands for | Definition | Starts | Ends |
|---|---|---|---|---|
| **MTTD** | Detect | Time from outage to *anyone noticing* | event begins | first signal (alert / human) |
| **MTTA** | Acknowledge | Time from page to the *person responding* | page fires | on-call ack |
| **MTTM** | Mitigate | Time from page to the *user impact ending* | page fires | mitigation in place |
| **MTTR** | Resolve / Recover | Time from page to the *root cause fixed* | page fires | full recovery, no recurrence |
| **MTBF** | Between Failures | Time between distinct incidents | one incident ends | next begins |

**The Staff Engineer addition.** *MTTM matters most.* Customers don't care about MTTR — they care about the bleed stopping. SRE optimizes MTTM aggressively (better runbooks, automated rollback, kill-switches), then optimizes MTTR (postmortem action items). Tracking MTTM separately from MTTR is the cultural marker that an org has matured beyond "all incidents look the same."

### 12.1 Why "MTTR is dead" is a recurring claim

Štěpán Davidovič's 2019 Google paper *Incident Metrics in SRE* argued MTTR is *misleading at low incident counts*: with N=4 incidents per quarter, the mean is dominated by one outlier, statistics on it are nonsense, and improvement is unmeasurable. The replacement is **distribution-of-incidents** thinking — bucket incidents by impact tier, track the count and 90th-percentile-MTTM per tier. This is the modern view; cite it correctly when leadership asks for "our MTTR trend."

---

## 13. Incident Vocabulary

Every word in this section gets misused weekly somewhere. Use them precisely.

| Term | Strict definition | Common misuse |
|---|---|---|
| **Incident** | An unplanned event causing user-impacting degradation | Any bug, any outage, any oncall ticket |
| **Outage** | A subset of incidents: complete unavailability | Used interchangeably with degradation |
| **Degradation** | Partial impairment: some users, some functionality | "It's not down, it's just slow" — that's a degradation incident |
| **Near-miss** | An event that *could have* caused impact but didn't | Underreported. Treat as incidents. |
| **Page** | An automated, urgent escalation to a human (off-hours capable) | Used to mean "any alert" |
| **Alert** | An automated rule firing — may or may not page | Used to mean "page" |
| **Ticket** | A non-urgent, queue-based notification | Used to mean alert |
| **Notification** | Any of the above | Often the right word; people say "alert" instead |
| **Postmortem** | A blameless retrospective document | "Writeup" — but without the *blameless* discipline |
| **RCA / Root Cause Analysis** | Structured analysis of contributing factors | "The root cause" (singular) — most outages have ≥3 contributing factors; the singular usage misleads |
| **Incident commander (IC)** | The single human running the incident | Confused with "person fixing it" — different roles, must be different humans for non-trivial incidents |

### 13.1 The blameless rule

A blameless postmortem assumes good intent and focuses on **systemic contributing factors**. It never names a person as a root cause. The reasoning: if a single person *could* take down production, the *system* failed (no review, no canary, no rollback automation, no permission boundary). Naming the person fixes nothing and makes future engineers hide mistakes — eroding the very honesty postmortems depend on.

`doc 15` covers postmortem mechanics in depth.

---

## 14. Toil and the 50% Rule

**Toil** is a precise term, not "work I don't like." From the Google SRE Book:

> Toil is the kind of work tied to running a production service that tends to be **manual, repetitive, automatable, tactical, devoid of enduring value, and grows linearly with service size**.

All six attributes must be present.

| Attribute | Test |
|---|---|
| Manual | A human runs it |
| Repetitive | Done >once, will be done again |
| Automatable | A computer could do it |
| Tactical | Reactive, interrupt-driven |
| Devoid of enduring value | The system isn't permanently better after |
| Scales with service growth | More traffic → more of this work |

**The 50% rule:** SREs spend ≤50% of time on toil; the rest on engineering (automation, design, capacity planning). If toil exceeds 50%, the team is operations staff — and the system gets worse, because there's no engineering bandwidth to fix the cause of the toil.

**Measure toil.** Have SREs log time per category for one quarter. Show toil-hours-per-team in dashboards. Make automating the top toil item the on-call's reward at the end of the rotation.

### 14.1 Glue work

A related anti-pattern coined by Tanya Reilly (2019): **glue work** = the un-glamorous coordination, mentorship, project management, doc-writing, and review-load-bearing that holds an org together. It is mostly *not* toil (it's not automatable in the same way), but it shares one property: it is *under-credited* in promotion processes. Staff Engineers must protect the team from doing too much glue work *in lieu of* the work that promotes them. The SRE-toil and Reilly-glue conversations rhyme; Staff-Eng-grade leaders track both.

---

## 15. Reliability Math You Should Know Cold

A surprising amount of SRE practice rests on five numerical facts. Memorize them.

### 15.1 The "nines" table

| SLO | Allowed downtime per year | Per quarter | Per month | Per week |
|---|---|---|---|---|
| 99% | 3.65 days | 21.6 hours | 7.2 hours | 1.68 hours |
| 99.5% | 1.83 days | 10.8 hours | 3.6 hours | 50 min |
| 99.9% | 8.77 hours | 2.16 hours | 43.2 min | 10.1 min |
| 99.95% | 4.38 hours | 65.7 min | 21.6 min | 5 min |
| 99.99% | 52.6 min | 13.2 min | 4.32 min | 1 min |
| 99.999% | 5.26 min | 1.32 min | 25.9 sec | 6 sec |

**Two non-obvious truths.**
1. Going from 99.9% to 99.99% is *10× as expensive* in engineering effort. Nobody talks about this when the SLA negotiation starts.
2. **You cannot be more reliable than your dependencies.** If your DB has 99.95% SLO, your service cannot reliably promise more *unless* you build redundancy on top.

### 15.2 Composition: serial vs parallel

If a request must succeed at A *and* B *and* C (serial dependency):

```
P(success) = P(A) × P(B) × P(C)

A=99.9%, B=99.9%, C=99.9%  →  99.7%
A=99.99% × 5 services      →  99.95%
```

Three nines per dependency, three dependencies = barely two nines combined.

If A *or* B can serve (parallel redundancy):

```
P(failure) = P(A_fail) × P(B_fail)

A=99%, B=99%  →  99.99% combined
```

This is *why* hot-standby and multi-region matter for high-SLO services.

### 15.3 The MTBF / MTTR identity

```
Availability = MTBF / (MTBF + MTTR)
```

Two ways to improve availability: *fail less often* (raise MTBF) or *recover faster* (lower MTTR). The latter is almost always cheaper. This is why mature SRE practice obsesses about runbooks, automation, and rollback speed — and why "shift-left" reliability investments (better testing, slower rollouts) target MTBF, while "shift-right" investments (better monitoring, automated rollback) target MTTR.

### 15.4 The marginal cost of nines

Roughly:

```
Cost per nine ≈ (cost of current architecture) × 3 to 10
```

Going from 99% to 99.9% is 3-10× more engineering effort. From 99.9% to 99.99% is again 3-10×. This compounds: 99.999% costs ~100-1000× what 99% costs. The reason the SLO target is *a business choice*, not an engineering one (§11) is that the business has to fund the cost curve.

---

## 16. Latency Math

Latency lies more than any other signal. Five facts.

### 16.1 Always use percentiles. Never use mean.

Mean latency for a request distribution `[10ms, 11ms, 12ms, 13ms, 14ms, 5000ms]` is 843ms. The 50th-percentile (median) is 12.5ms, the 99th is 5000ms. The mean lives nowhere on the distribution; it does not describe any user's experience. Always: p50, p95, p99, p99.9.

### 16.2 The tail is where the pain is

For most production services, p50 is fine, p99 is 10–50× p50, and the p99.9 *is the user's experience* — because most users hit several services per page-view, and the slowest one dominates. Jeff Dean's 2013 *The Tail at Scale* paper is the canonical reference; the math is sobering: a service with p99=10s, called 100 times per page, has a *50% chance* of one call hitting that 10s on every page view.

### 16.3 Little's Law

```
L = λ × W

L = items in the system (concurrent requests)
λ = arrival rate (rps)
W = average time in system (latency)
```

So: a service handling 100 rps with 200ms average latency holds 20 concurrent requests in flight on average. If your thread pool has 10 threads, you're saturating; latency will rise non-linearly. This is *why* saturation precedes latency spikes.

### 16.4 The M/M/1 queue (and why utilization > 80% kills you)

Average wait time in an M/M/1 queue at utilization ρ:

```
W = (1/μ) × ρ / (1 - ρ)

ρ=0.50  → W = 1× service time
ρ=0.80  → W = 4×
ρ=0.90  → W = 9×
ρ=0.95  → W = 19×
ρ=0.99  → W = 99×
```

This is why "don't run resources hotter than 80%" is *not* a superstition. Past 80%, queueing latency (saturation) explodes geometrically. The number "80%" comes from this curve.

### 16.5 Percentiles don't compose

`p99(A) + p99(B) ≠ p99(A+B)`. You cannot sum the p99 of two services to estimate end-to-end p99. The math doesn't work. To estimate end-to-end percentiles, you must trace (or simulate). This is one big reason traces matter — they're the only way to get true end-to-end latency distributions across a microservice mesh.

---

## 17. The Observability Tetrahedron

The triangle (metrics / logs / traces) is the 2018 model. The 2026 model is a tetrahedron with profiles. The four signals are the *vertices*; the *edges* are correlation IDs that let you jump between them.

```
                      METRICS
                       /│\
                      / │ \
                     /  │  \
                    /   │   \
              trace_id  │  service.name
                  /     │     \
                 /      │      \
                /       │       \
              LOGS──tenant_id──TRACES
                \       │       /
                 \      │      /
                  \     │     /
              service.name service.name
                    \   │   /
                     \  │  /
                      \ │ /
                       \│/
                     PROFILES
```

The **edges matter more than the vertices**. A stack with all four signals but no shared identifiers is unmonitorable in practice; you can see anomalies in metrics but never jump to the trace, log, or profile that explains them. The mandatory correlation skeleton is:

1. Every log carries `trace_id` and `span_id`.
2. Every metric histogram has exemplar trace_ids attached.
3. Every span carries `service.name` and `tenant_id`.
4. Every profile is keyed by `(service.name, tenant_id)` and aligned to UTC time windows that match metric/trace queries.

Lose any one edge and you've broken triage. The single highest-leverage one-week project at most stacks is "get `trace_id` into every log line" — it's the difference between debugging in 5 minutes vs 5 hours.

---

## 18. Common Confusions to Inoculate Against

A short list of mistakes I've watched experienced engineers make. None are subtle once seen.

1. **"Saturation = utilization."** No — saturation is queueing past 100%. Utilization is the input; saturation is the symptom.
2. **"P99 is good enough."** P99.9 is where the long tail lives. For high-traffic services, billions of requests/day make p99.9 the experience for *millions of users*.
3. **"Logs and metrics are the same thing."** No — metrics are aggregates; logs are events. They scale on different axes (cardinality vs traffic) and have wildly different cost shapes.
4. **"Errors-per-second is the right alert."** No — error *rate* (ratio) is. A counter alert breaks the moment traffic shifts.
5. **"100% trace sampling is fine if storage is cheap."** Storage is cheap; *bandwidth* and *query latency* are not. 100% sampling makes your trace store unqueryable past a certain scale.
6. **"Retention = how long we keep raw."** No — retention is *tiered*. Raw for hours, downsampled for weeks, aggregated for years.
7. **"SRE is a job title."** No — SRE is a *practice*. SWEs do SRE work; SREs do SWE work. The split is org-by-org; the practices (SLOs, postmortems, error budgets) are universal.
8. **"Observability is what we install."** No — observability is a *property of the system being observed*. Tools enable it; practices realize it.
9. **"We have no SLOs because the product is too new."** This is the *most* important time to set them — to set the bar before product habits ossify.
10. **"Postmortems shame people."** Only badly run ones. The literature on blameless culture (Allspaw 2012, Beyer 2016, Lund 2018) is unambiguous: blame surfaces less truth, slows recovery, and inflates MTTR.
11. **"RED only applies to web services."** It applies to anything with a request boundary: queue consumers, schedulers, even functions. Treat any unit-of-work boundary as a "request."
12. **"USE only applies to hardware."** Connection pools, thread pools, goroutine schedulers, file descriptors, ephemeral ports — all are USE subjects.
13. **"We don't need traces; logs have everything."** Logs without span structure can't answer "which span took 300ms" without manual reconstruction. Traces are the structure logs lack.
14. **"Alerting on cause is fine if the cause is the right level."** No — *always* alert on symptom (user impact), use cause as *diagnosis*. Otherwise the day the cause changes (new failure mode), you stop alerting on real outages.
15. **"Histograms are too expensive."** Native histograms (Prometheus 2.40+) are 10× cheaper than classic. Use them.

---

## 19. The One-Page Glossary

The compact reference. Bookmark this section.

| Term | Strict meaning |
|---|---|
| **Observability** | Property: internal state inferable from external outputs |
| **Monitoring** | Practice: watching predefined signals against thresholds |
| **Telemetry** | The raw data emitted (metrics, logs, traces, profiles) |
| **SLI** | Indicator — measurement of user experience as good/total ratio |
| **SLO** | Objective — internal target on an SLI over a window |
| **SLA** | Agreement — external contractual promise (laxer than SLO) |
| **Error budget** | (1 − SLO) × total events; the budget for failure |
| **Burn rate** | (current bad rate) / (steady-state allowed bad rate) |
| **Cardinality** | Number of unique time series for a metric |
| **Series** | One metric's unique label-set combination |
| **Histogram** | Bucketed counts; percentiles computed at query time, composable |
| **Summary** | Pre-computed percentiles at SDK; not composable across instances |
| **Exemplar** | Pointer from a histogram bucket to a specific trace_id |
| **Head sampling** | Sampling decision at the SDK before propagation |
| **Tail sampling** | Sampling decision at the gateway after trace assembly |
| **Adaptive sampling** | Dynamic rate based on load / class |
| **Reservoir sampling** | Statistical sampling of N items from a stream |
| **Latency** | Time from request start to response |
| **Saturation** | Degree of queueing past resource capacity |
| **Utilization** | Fraction of time a resource is busy |
| **Golden signals** | Latency, traffic, errors, saturation (Google) |
| **RED** | Rate, Errors, Duration (per service) |
| **USE** | Utilization, Saturation, Errors (per resource) |
| **Toil** | Manual, repetitive, automatable work scaling with traffic |
| **Glue work** | Coordination/mentorship/review work; under-credited |
| **MTTD/MTTA/MTTM/MTTR** | Detect / Acknowledge / Mitigate / Recover (different!) |
| **MTBF** | Mean time between failures |
| **Page** | Automated, urgent, off-hours-capable escalation |
| **Alert** | Any rule firing — may or may not page |
| **Ticket** | Non-urgent queue-based notification |
| **Incident** | Unplanned event with user impact |
| **Outage** | Complete unavailability subset of incidents |
| **Degradation** | Partial impairment — still an incident |
| **Near-miss** | Could-have-been-impact event |
| **Postmortem** | Blameless retrospective |
| **Root cause** | Always plural in mature analysis |
| **IC (Incident Commander)** | The single human running the response |
| **Runbook** | Versioned, linked-from-alert procedure |
| **Trace** | A DAG of spans across services |
| **Span** | One operation: name, kind, parent_id, attributes, duration |
| **Trace context** | W3C `traceparent` + `tracestate` headers |
| **Baggage** | W3C key/value context propagated across services (separate from trace) |
| **OTLP** | OpenTelemetry's wire protocol |
| **OpenMetrics** | CNCF standardization of Prometheus exposition |
| **Native histogram** | Prometheus 2.40+ sparse histogram (10× cheaper) |
| **Pull / scrape** | Server-initiated metric fetch (Prometheus model) |
| **Push** | Client-initiated metric/log/trace export (OTLP model) |
| **`remote_write`** | Prometheus's outbound push protocol |
| **WAL** | Write-ahead log — durability layer in TSDBs/log stores |
| **Compaction** | Background merging of small blocks into larger ones |
| **Inverted index** | label → set of series IDs (or token → docs) |
| **Forward index** | series ID → samples |
| **Block** | Immutable on-disk unit of storage in a TSDB / log store |
| **Chunk** | Compressed run of samples within a block |
| **Tenant** | Logical isolation unit — owns a slice of writes / reads |
| **Quota** | Per-tenant limit (samples/sec, GB/day, queries/sec) |
| **Federation** | Higher-level Prometheus scraping lower-level Prometheus |
| **Service graph** | Auto-derived map of services from trace data |
| **RUM** | Real User Monitoring — telemetry from real client sessions |
| **Synthetic** | Telemetry from scripted, periodic probes |
| **Continuous profiling** | Stack-trace sampling stored over time |

---

## TL;DR

Every later chapter in this folder leans on these definitions. If you find yourself in a meeting and someone says "p99," "SLO," "page," "tail sampling," or "saturation" with imprecision, gently correct or restate. The cost of that politeness is one sentence; the cost of the alternative is a one-hour vocabulary fight at the wrong time.

Now go to `doc 01` for the architecture, or `doc 13` for the SLO math, or `doc 18` for cardinality. Vocabulary in hand, the rest of the folder reads cleanly.
