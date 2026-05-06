# SRE & Observability: End-to-End Roadmap

A Staff-Engineer-level deep dive into Site Reliability Engineering and Observability — from the kernel counter that produced a sample, to the SLO that decides whether your team ships next quarter. The other docs in this folder (planned) go deep on individual layers. **This file is the map**: it shows how every layer connects, the order to build them in, and which production systems implement each piece.

If you only ever read one page in this folder, read this one.

---

## Table of Contents

1. [The One-Page Picture](#1-the-one-page-picture)
2. [The Four Universal Telemetry Pipelines](#2-the-four-universal-telemetry-pipelines)
3. [The Build Order: Phase 0 → Phase 20](#3-the-build-order-phase-0--phase-20)
4. [Component Responsibility Map](#4-component-responsibility-map)
5. [Cross-Cutting Concerns (the 6 Hard Problems)](#5-cross-cutting-concerns-the-6-hard-problems)
6. [The SRE Lifecycle: SLOs, Error Budgets, On-Call, Postmortem](#6-the-sre-lifecycle-slos-error-budgets-on-call-postmortem)
7. [Variant Decision Tree](#7-variant-decision-tree)
8. [End-to-End Trace of One Request](#8-end-to-end-trace-of-one-request)
9. [Linear Reading Order](#9-linear-reading-order)
10. [Common Pitfalls When Building Your Own Platform](#10-common-pitfalls-when-building-your-own-platform)
11. [Glossary of Terms a Staff Engineer Should Use Correctly](#11-glossary-of-terms-a-staff-engineer-should-use-correctly)

---

## 1. The One-Page Picture

Observability is a stack of layers. Each layer talks only to its neighbors. SRE practice (SLOs, on-call, postmortems) wraps the whole thing — you cannot run reliable systems without **both** sides of the diagram.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  WORKLOAD  (services, batch jobs, CronJobs, Lambdas, mobile/web clients) │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │  emit signal at the source
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  INSTRUMENTATION                                              ─── doc 03 │
│   OpenTelemetry SDK · Prometheus client · log libraries · eBPF auto-instr│
│   (counters, histograms, spans, structured log records, profiles)        │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │  OTLP / Prometheus exposition / syslog
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  COLLECTION & EDGE PROCESSING                                 ─── doc 04 │
│   OTel Collector · Fluent Bit · Vector · node_exporter · DCGM-exporter   │
│   (batch, retry, redact PII, tail-sample traces, drop high-cardinality)  │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │  buffered, encoded (protobuf / JSON)
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  TRANSPORT / FAN-OUT                                          ─── doc 05 │
│   Kafka · Kinesis · GCP Pub/Sub · gRPC streams · in-memory queues        │
│   (decouple producers from storage; survive backend outages)             │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │
        ┌──────────────────────┼──────────────────────┬─────────────────────┐
        ▼                      ▼                      ▼                     ▼
┌────────────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌───────────────┐
│  METRICS       │   │  LOGS            │   │  TRACES          │   │  PROFILES     │
│  ─── doc 06    │   │  ─── doc 07      │   │  ─── doc 08      │   │  ─── doc 09   │
│  TSDB:         │   │  Inverted index  │   │  Span store      │   │  pprof store  │
│  Prometheus,   │   │  or columnar:    │   │  + service graph:│   │  Parca,       │
│  VictoriaMetric│   │  Loki, ES,       │   │  Jaeger, Tempo,  │   │  Pyroscope    │
│  Mimir, M3,    │   │  ClickHouse,     │   │  Honeycomb,      │   │  (eBPF +      │
│  Thanos, Cortex│   │  Splunk, OpenSrch│   │  ClickHouse      │   │  symbolizer)  │
└────────┬───────┘   └────────┬─────────┘   └────────┬─────────┘   └───────┬───────┘
         │                    │                      │                     │
         └────────────────────┴───────┬──────────────┴─────────────────────┘
                                      ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  QUERY LAYER                                                  ─── doc 10 │
│   PromQL · LogQL · TraceQL · SQL (ClickHouse, BigQuery on lakehouse)     │
│   (range vectors, log streams, span trees, flamegraphs)                  │
└──────────────────────────────┬───────────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  CONSUMPTION                                                  ─── doc 11 │
│  Dashboards (Grafana) · Alerting (Alertmanager, PagerDuty) · APM UIs     │
│  Anomaly detection · Notebooks · runbook automation · ChatOps            │
└──────────────────────────────┬───────────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  SRE PRACTICE LOOP                                       ─── docs 13–17  │
│   SLO/SLI definition → error budget tracking → burn-rate alerting →      │
│   on-call → incident response → postmortem → reliability backlog → SLO   │
└──────────────────────────────────────────────────────────────────────────┘

         ╔══════════════════════════════════════════════════════════════╗
         ║   CROSS-CUTTING (orthogonal — wraps every layer above)       ║
         ║   ─── docs 18, 19, 20                                        ║
         ║   Cardinality control · Sampling · Retention tiers · Cost    ║
         ║   Multi-tenancy · PII / compliance · Schema governance       ║
         ║   Auth / RBAC · Tenant isolation · BCDR for telemetry        ║
         ╚══════════════════════════════════════════════════════════════╝
```

**The key intuition.** Every observability stack reduces to: **emit a signal, ship it, store it cheaply, query it fast, alert humanly.** Every higher concept — SLOs, golden signals, distributed tracing, anomaly detection — is a clever choreography of those five primitives. The reason production observability is hard is that *every* one of those primitives must work at three to five orders of magnitude beyond what a textbook example demonstrates.

---

## 2. The Four Universal Telemetry Pipelines

Observability has exactly four hot paths. Memorize these flows and you can reason about any system.

### 2.1 Metric Pipeline — "what is the rate / latency / error count?"

```
Application code
  │   counter.Inc(),  histogram.Observe(latency_ms)
  ▼  [Instrumentation — doc 03]
In-process aggregator (counter, gauge, histogram, summary)
   - merges samples in memory at line rate (millions/sec)
   - exposes /metrics or pushes via OTLP
  │
  ▼  [Scrape or Push — doc 04]
PULL: Prometheus scrape every 15s — pulls /metrics, parses exposition format
PUSH: OTLP exporter → OTel Collector → remote_write
  │
  ▼  [Edge processing]
Relabel · drop high-cardinality labels · enforce tenant_id · batch
  │
  ▼  [TSDB ingest — doc 06]
WAL append → memtable (head block) → mmap'd chunks (XOR / Gorilla compression)
  - 2-hour blocks → compacted into 24h → uploaded to object storage (Thanos/Mimir)
  - Index: postings list (label → series IDs), inverted index per label name/value
  │
  ▼  [Query — doc 10]
PromQL: rate(http_requests_total{status="500"}[5m])
   → resolve label matchers via inverted index
   → fetch chunks from blocks intersecting [now-5m, now]
   → window functions (rate, increase, histogram_quantile)
   → return instant or range vector
  │
  ▼  [Consumption]
Grafana dashboard panel  ·  Alertmanager rule (for: 5m)  ·  recording rule
```

**Where each doc fits:** instrumentation → 03 · scrape/push → 04 · TSDB internals → 06 · PromQL → 10 · alerting → 12.

> **Mental model:** A metric is a *named time series*. Every unique combination of `(metric_name, label_set)` is one series. Series count (cardinality) is the only knob that matters for cost. Everything else is detail.

### 2.2 Log Pipeline — "what happened on this specific request?"

```
Application code
  │   logger.info("user.login", user_id=42, latency_ms=87)
  ▼  [Structured logging library — doc 03]
JSON-encoded record: {ts, level, msg, attrs..., trace_id, span_id}
  │   (CRITICAL: include trace_id so logs join to traces)
  ▼  [stdout / journald / file]
Container runtime captures stdout → /var/log/containers/*.log
  │
  ▼  [Agent — doc 04]
Fluent Bit / Vector / Promtail tails files
   - parse, enrich (k8s metadata: namespace, pod, node)
   - redact PII (regex or column-based)
   - batch + compress (gzip/zstd)
  │
  ▼  [Transport — doc 05]
Kafka topic OR direct ingest to log store
   (Kafka if you have >1 backend, multi-tenant, or want replay)
  │
  ▼  [Log store ingest — doc 07]
Two architectures:
  A) Index-everything (Elasticsearch, OpenSearch, Splunk)
     - inverted index on every term + doc store
     - $$$ but flexible queries
  B) Index-the-labels (Loki, ClickHouse-on-logs)
     - index only metadata; brute-force grep across columnar/object store
     - 10× cheaper, narrower query patterns
  │
  ▼  [Query — doc 10]
LogQL:  {app="checkout"} |= "error" | json | latency_ms > 500
SQL:    SELECT * FROM logs WHERE service='checkout' AND ts > now()-1h
  │
  ▼  [Consumption]
Live tail · Log panel in Grafana · Saved searches · Log-based alerts
```

> **Mental model:** Logs are *cardinality unbounded* and *retention bounded*. The architectural question is always "how do I make this $$$ thing $". Two answers: index less (Loki), or compress harder + columnar (ClickHouse).

### 2.3 Trace Pipeline — "where did this request spend its time?"

```
Inbound request arrives at gateway
  │   W3C traceparent header: 00-{trace_id}-{span_id}-01
  ▼  [Context propagation — doc 03]
Middleware extracts traceparent → starts root span
  │   (if absent, root span generates new trace_id)
  ▼  [Span lifecycle]
For each operation (RPC, DB call, kafka publish):
  - start span: name, kind=CLIENT|SERVER|INTERNAL, parent_span_id
  - attach attributes: db.statement, http.url, peer.service
  - on completion: record duration + status; emit to exporter
  - PROPAGATE traceparent to downstream callees (HTTP header, gRPC metadata)
  │
  ▼  [Local sampler]
Head-based: keep all spans of every Nth trace OR
            keep based on rate(parent_decision)
Probabilistic sampling decision is encoded in trace_id (consistent across services)
  │
  ▼  [Batch exporter]
Buffer N spans / Tms → OTLP/gRPC → OTel Collector
  │
  ▼  [Tail sampling — doc 04]
Wait ~30s for full trace to assemble in collector
   → keep if: status=ERROR, latency > P99, contains specific service
   → drop the boring 95%
  │
  ▼  [Span store — doc 08]
Tempo/Jaeger:
  - object store keyed by trace_id (cheap, immutable)
  - bloom filter per block to skip irrelevant blocks
  - service-graph metrics auto-derived (RED metrics per edge)
ClickHouse:
  - columnar table partitioned by hour, ordered by (service, ts)
  - rich SQL (impossible in Tempo) but more $$ to operate
  │
  ▼  [Query — doc 10]
TraceQL:  { span.http.status_code = 500 && resource.service.name = "auth" }
  - find candidate traces via spanmetrics + label index
  - fetch full trace by trace_id (one object store get)
  │
  ▼  [Consumption]
Flamegraph / waterfall view · Service graph · Exemplars on metric panels
```

> **Mental model:** A trace is a *DAG of spans*. The two non-negotiables are (1) **context propagation** at every process boundary — break the chain once and the trace is useless, and (2) **sampling that survives collation** — head sampling decides per-process, tail sampling decides after assembly; you almost always want tail.

### 2.4 Profile Pipeline — "where in the code did the CPU/memory go?"

```
Process running with frame pointers OR DWARF unwind info
  │
  ▼  [Sampler — doc 09]
Two sources:
  A) In-process: pprof endpoint, runtime/pprof, async-profiler (JVM)
  B) System-wide: eBPF (perf_event sampler) — kernel + userspace, language-agnostic
  │
  ▼  [Stack walk]
At sample time (e.g., 99 Hz), walk the stack:
  - kernel frames (kprobes, syscalls)
  - userspace frames (frame ptr or .eh_frame DWARF)
  - userspace symbol table missing? send raw addrs + buildID
  │
  ▼  [Symbolization]
At ingest: resolve addr → function name using:
  - debuginfod (upstream debug symbols)
  - on-disk debug info bundle keyed by buildID
  - language-specific (Go: gopclntab; JVM: perf-map; Python: py-spy)
  │
  ▼  [Profile store — doc 09]
Parca / Pyroscope:
  - merge stack traces into a *call graph DAG*; store as compressed pprof
  - dictionary-encode function names (huge compression win)
  - index by (service, profile_type, ts)
  │
  ▼  [Query]
Diff profile (commit A vs B) · flamegraph over time window · top-N hot functions
  │
  ▼  [Consumption]
Continuous profiling UI · regression detection in CI · cost attribution per function
```

> **Mental model:** A profile is a *weighted set of stack traces*. Continuous profiling is just *profiles every minute, kept for weeks*. The hard part is symbolization in a polyglot fleet where binaries are stripped.

---

## 3. The Build Order: Phase 0 → Phase 20

If you sat down to build an observability + SRE practice from scratch — at a startup, a new team in a big org, or a green-field cluster — this is the order. Each phase depends on the previous ones. Skipping ahead is what makes "we have observability" feel like a lie despite a six-figure Datadog bill.

| Phase | What you build | Why now | Doc | Concrete tools |
|---|---|---|---|---|
| **0** | Mental models: signals, golden 4 (latency, traffic, errors, saturation), USE/RED, the observability ≠ monitoring distinction | Without shared vocabulary every later argument is a vocabulary fight. | [00](./00-mental-models.md) | — |
| **1** | One process, one metric: a service exposes `/metrics` with `http_requests_total` and `http_request_duration_seconds` (histogram) | This is the smallest unit of observability. If this hurts, every later step will hurt 10×. | [03](./03-instrumentation.md) | Prometheus client lib, OTel SDK |
| **2** | Local Prometheus scraping that one process; Grafana panel showing P95 latency | First end-to-end loop. Sample → store → query → render. | [06](./06-metrics-storage.md), [10](./10-query-layer.md) | Prometheus, Grafana |
| **3** | Structured logs (JSON) with a correlation field (request_id) | Once you have >1 request flowing, you need to follow one. Stringly logs become a debugging tarpit fast. | [03](./03-instrumentation.md), [07](./07-logs-storage.md) | zerolog, structlog, slog, Serilog |
| **4** | Log shipping: Fluent Bit / Vector → Loki or ELK | First centralized view. The "ssh + tail" anti-pattern dies here. | [04](./04-collection-and-edge.md), [07](./07-logs-storage.md) | Fluent Bit, Loki, OpenSearch |
| **5** | Distributed tracing with OTel auto-instrumentation; trace_id injected into logs | The moment you can answer "where did this request actually go?" instead of guessing. | [03](./03-instrumentation.md), [08](./08-traces-storage.md) | OTel, Jaeger, Tempo |
| **6** | OTel Collector (gateway pattern): every signal flows through one binary you control | Prevents N×M coupling between SDKs and backends. Future-proofs vendor swaps. | [04](./04-collection-and-edge.md) | otelcol, agent + gateway |
| **7** | Standardize the four golden signals per service: latency, traffic, errors, saturation | First time the platform is *uniform*: every service answers the same four questions the same way. | [00](./00-mental-models.md), [11](./11-dashboards.md) | RED method dashboards |
| **8** | Define your first SLI and SLO for one critical user journey (e.g., checkout) | This is when "monitoring" becomes "SRE". An SLI without an SLO is decoration. | [13](./13-slo-engineering.md) | Sloth, Pyrra, Nobl9 |
| **9** | Multi-window multi-burn-rate alerting based on the SLO | Replaces threshold alerts ("p99 > 500ms for 5m"). Catches fast burns *and* slow burns without flapping. | [12](./12-alerting.md), [13](./13-slo-engineering.md) | Alertmanager, PagerDuty |
| **10** | On-call rotation, runbooks linked from alerts, postmortem template | Reliability is now a *team practice*, not just a stack. Without this, the stack rots. | [14](./14-on-call.md), [15](./15-incident-response.md) | PagerDuty, Opsgenie, incident.io |
| **11** | Cardinality budget per service; relabel/drop rules at the collector | Day-one Prometheus is fine; day-365 Prometheus has 50M series and 2 TB RAM. Budget early. | [18](./18-cardinality-and-cost.md) | Prometheus relabel, OTel filter |
| **12** | Long-term metric storage: Thanos/Mimir/VictoriaMetrics with downsampling tiers | Default Prometheus retention is 15 days. SLO compliance reports need 90+. Capacity planning needs 12+ months. | [06](./06-metrics-storage.md) | Thanos, Mimir, VM, M3 |
| **13** | Tail sampling for traces; head sampling becomes a fallback | At 1k RPS × 100% sampling = your trace bill > your AWS bill. Tail keeps the interesting 5%. | [04](./04-collection-and-edge.md), [08](./08-traces-storage.md) | OTel tail_sampling processor |
| **14** | Continuous profiling rolled out on top 10 services | Closes the "observability triangle → tetrahedron". Tells you *which line of code* is the issue. | [09](./09-profiling.md) | Parca, Pyroscope, Polar Signals |
| **15** | Synthetic + Real User Monitoring (RUM) for top user journeys | Server-side health is not user-perceived health. Synthetic catches outages before users do; RUM catches what only some users see. | [11](./11-dashboards.md) | Pingdom, Checkly, Sentry, Datadog RUM |
| **16** | Capacity planning loop: weekly review of headroom, growth, saturation | Reliability vs cost is a *forecasting* problem. SREs that don't forecast end up firefighting capacity. | [16](./16-capacity-planning.md) | recording rules, Grafana, Notebooks |
| **17** | Production Readiness Reviews (PRR) gating service launches | Codifies "what observability and reliability bar must a service meet before it serves traffic?" | [17](./17-production-readiness.md) | PRR checklist, launch tooling |
| **18** | Chaos engineering: scheduled failure injection in non-prod, then prod | You don't know your runbooks work until something has gone wrong with humans watching. | [15](./15-incident-response.md) | Chaos Mesh, Gremlin, LitmusChaos |
| **19** | Multi-tenant platform: per-tenant quotas, RBAC, billing attribution | The platform now serves N teams. Without isolation, one team's runaway log volume is everyone's outage. | [19](./19-multi-tenancy.md) | Mimir tenants, Loki orgs |
| **20** | AIOps / anomaly detection / LLM-assisted incident response | The frontier. Statistical anomaly detection on series, ML-driven alert grouping, LLM postmortems. Use *only after* phases 0–19 are healthy. | [20](./20-aiops.md) | Sigma, BigPanda, custom ML |

**The sentence to remember.** *Phases 0–7 give you observability. Phase 8 turns it into SRE. Phases 9–17 industrialize it. Phases 18–20 stretch it.* Most "we have observability gaps" complaints are actually mis-tuned phase 11 + 13 + 15. Most outages-during-on-call are phase 9 + 10 + 14.

---

## 4. Component Responsibility Map

When something breaks (or when you read someone else's observability stack), this is how to attribute blame.

| Component | Owns | Doesn't own | Doc | Common production tools |
|---|---|---|---|---|
| **Instrumentation SDK** | Producing signals at the source with correct semantic conventions | Sampling decisions for the whole system, transport reliability | 03 | OTel SDK, Prometheus client, slog |
| **Agent (per node)** | Reading host signals (`/proc`, journald), tailing files, k8s metadata enrichment | Long-term storage, query | 04 | node_exporter, Fluent Bit, Vector, otel-collector-agent |
| **Collector (gateway)** | Batching, retry, redaction, tail sampling, vendor fan-out | Storage, alerting | 04 | OTel Collector (gateway), Vector aggregator |
| **Transport / queue** | Decoupling producers from storage, durability across backend outage | Querying, transformation logic | 05 | Kafka, Kinesis, Pub/Sub, Redpanda |
| **TSDB (metrics)** | Time-series ingest, compression, indexing by label set, range queries | Logs, traces, alert evaluation | 06 | Prometheus, VictoriaMetrics, Mimir, M3, Thanos, InfluxDB |
| **Log store** | High-volume append, label-indexed search OR full-text search, retention tiering | Stateful query results, dashboards | 07 | Loki, Elasticsearch, OpenSearch, Splunk, ClickHouse, S3+Athena |
| **Trace store** | Span ingest keyed by trace_id, service graph derivation, span search | Cross-trace analytics that aren't service-graph-shaped | 08 | Jaeger, Tempo, Honeycomb, Lightstep, ClickHouse |
| **Profile store** | Stack-trace aggregation, symbol resolution, diff queries | Real-time alerting | 09 | Parca, Pyroscope (now Grafana), Polar Signals |
| **Query engine** | Translating PromQL/LogQL/TraceQL/SQL to physical fetches | Storage layout (that's the store's job) | 10 | Prometheus engine, LogQL, TraceQL, ClickHouse |
| **Alerting engine** | Evaluating rules at fixed intervals, deduping, routing, silencing | The notification UI | 12 | Alertmanager, Grafana alerts, Sensu |
| **Notification / paging** | Reaching the right human within SLA | Alert content quality (that's the rule's job) | 12, 14 | PagerDuty, Opsgenie, Squadcast |
| **Dashboard / UI** | Composing panels, query templating, exploration UX | Single source of truth for SLOs (that's the SLO doc) | 11 | Grafana, Datadog, Honeycomb, Splunk |
| **SLO definition store** | Source-of-truth for SLI/SLO/error budget targets, generated alerts | Alert delivery, raw signals | 13 | Sloth, Pyrra, Nobl9, OpenSLO |
| **Runbook system** | Linkable, versioned playbooks; one click from alert to action | Telemetry storage | 14, 15 | Confluence, Notion, runbooks-as-code, Backstage |
| **Incident management** | Declaring incident, paging chain, war-room, comms updates, timeline | Postmortem analysis | 15 | incident.io, FireHydrant, PagerDuty Inc Resp |
| **Postmortem system** | Capturing timeline, contributing factors, action items, follow-up tracking | Future avoidance (that's the action items' job) | 15 | Jeli, Howie, Confluence templates |
| **Capacity / FinOps** | Forecasting headroom, attribution to teams/services, cost trending | Real-time alerting | 16 | custom recording rules, Vantage, Cloudability |
| **Production Readiness** | Pre-launch checklist enforcement, "is this service ready?" signal | Post-launch reliability (that's all the rest) | 17 | PRR template, Backstage scorecards |

The diagonal observation: each component owns *exactly one* concern. When two components seem to overlap (e.g., "should the collector enforce cardinality, or the TSDB?"), production stacks split it the way the table above does. Crossing that line is the source of most platform bugs.

---

## 5. Cross-Cutting Concerns (the 6 Hard Problems)

Every observability platform, no matter the variant, must solve six problems simultaneously. The docs in this folder mostly exist because each problem has many possible solutions.

### 5.1 Cardinality — "how many unique time series exist?"

The single biggest cost driver and the single biggest reason a Prometheus dies. Cardinality = product of distinct values across all label dimensions for a metric.

```
http_requests_total{method, status, route, customer_id, region}
                     ↑5     ↑10     ↑200   ↑1,000,000  ↑3
                     = 30,000,000,000 series
```

`customer_id` as a label = death. As a *log attribute* or *trace attribute* = fine.

**Defenses (in order of preference):**
1. **Don't add the label.** Move high-cardinality dimensions to logs/traces.
2. **Aggregate at the collector.** Drop labels you'll never query on.
3. **Hash + bucket.** Top-K via HyperLogLog or Top-K sketch in the agent.
4. **Per-tenant cardinality limits** in the TSDB; reject series above quota.
5. **Reservoir sampling** for "exemplar" samples that link a metric bucket back to specific traces.

> **Pitfall:** You only see the cost three weeks after the bad label rolls out, when ingestion blows past memory budgets at peak. By then you have customers depending on the dashboards. Pre-flight cardinality in CI.

### 5.2 Sampling — "we cannot keep everything; what do we keep?"

Three different problems wearing the same word.

| Sampling type | Where decided | Decides what to keep | Loses |
|---|---|---|---|
| **Head sampling** | At the SDK, before context propagation | All spans of selected traces | Tail visibility into the rare error if not selected |
| **Tail sampling** | At the collector, after full trace assembly | Traces matching a policy (errors, slow, rare service) | Real-time export (must wait for assembly window) |
| **Adaptive sampling** | Dynamically | Increases for rare classes, decreases for hot paths | Reproducibility |
| **Aggregation/Histogram bucketing** | At metric SDK | Statistical summary, not raw samples | The ability to ask new questions later |
| **Logs: dynamic verbosity** | At the application | Debug logs only when error or trace is sampled | Coherent log streams across processes |

**Critical rule for traces:** the sampling decision must be **consistent across services**. Encode it in the `trace_id` low bits or in the `tracestate` header so service B doesn't sample-out a request that service A sampled-in.

### 5.3 Retention — "for how long?"

Telemetry value drops *fast*. Cost stays *flat*. Build a tiered storage strategy:

| Tier | Resolution | Retention | Cost driver | Used for |
|---|---|---|---|---|
| **Hot** | Raw (15s) | 7–15 days | Memory + SSD | Live debugging, dashboards, alerts |
| **Warm** | 1m downsampled | 30–90 days | SSD | Recent capacity reviews, SLO calculations |
| **Cold** | 5m or 1h downsampled | 1–2 years | Object storage (S3/GCS) | Year-over-year, audit, compliance |
| **Archive** | Full or aggregated | 7+ years | Glacier / cold object | Compliance only |

Logs differ: most logs are useless after 7 days, but some classes (audit, security, billing) need years. **Solve this with two pipelines, not one giant retention.**

### 5.4 Cost & Cardinality Budget — "the bill is now bigger than the service it observes"

The classic failure mode: observability bill grows super-linearly with traffic. Solutions:

- **Per-team / per-service quotas** for series, log GB/day, span volume.
- **Show the bill in the dashboard.** When teams see "your service costs $14k/mo to observe," they self-regulate.
- **Charge back.** Showback first, then chargeback.
- **Sample harder on hot paths, keep all signals on rare/error paths.**
- **Re-evaluate every alert quarterly:** if it never fired or never led to action, delete it. Alerts cost.

### 5.5 Multi-tenancy & Isolation — "one team's runaway query took down everyone's dashboards"

Once you serve >3 teams, you need:
- **Auth at every layer** (read and write).
- **Per-tenant rate limits** (queries/sec, samples/sec, log GB/sec).
- **Per-tenant resource quotas** (memory in TSDB, query timeout, max series).
- **Logical isolation** via tenant_id in storage; physical isolation only when required (regulated workloads).
- **Noisy-neighbor circuit breakers**: kill expensive queries automatically.

### 5.6 PII, Compliance, Schema Governance — "we logged the credit card again"

- **Redaction at the agent**, not at the store. By the time it's stored, it's leaked.
- **Schema-as-code** for log fields (use OTel semantic conventions).
- **Field-level access control** in log stores that support it (Splunk, Snowflake on logs).
- **Audit trail for queries** (who searched for which user_id when?) — required for SOC2, HIPAA, etc.
- **Right-to-erasure**: GDPR/CCPA mean you may need to delete events tied to a user. Plan storage layout for this.

---

## 6. The SRE Lifecycle: SLOs, Error Budgets, On-Call, Postmortem

This section is the half of the diagram that tools don't give you. SRE without these practices is just monitoring with a fancier badge.

### 6.1 SLI / SLO / SLA — the three S's

```
SLI (Indicator) — a measurement      "the proportion of HTTP requests
                                      to /checkout that returned 2xx
                                      and completed in <500ms"

SLO (Objective) — internal target    "99.9% of /checkout requests over
                                      a rolling 28-day window"

SLA (Agreement)  — external promise  "we credit your account if uptime
                                      drops below 99.5% in a calendar month"
                                      (almost always laxer than SLO; SLO
                                       is the bar engineering aims at)
```

**Design rules** (from Google SRE Book; refined in *Implementing SLOs*, Beyer):
1. **SLIs measure the user's experience.** "CPU < 80%" is not an SLI. "Page load < 1s" is.
2. **The SLI is a ratio of good events / total events.** Easy to alert on, easy to budget.
3. **Pick 2–4 SLIs per critical user journey.** Not per service.
4. **The SLO target is a business choice**, not an engineering one. 99.9% vs 99.99% is a 10× cost difference.
5. **An SLO without an error budget is decoration.** The budget is the lever.

### 6.2 Error Budget — the lever that makes SRE work

```
SLO = 99.9% over 28 days
Total requests in window = 100M
Allowed bad events       = 100M × (1 - 0.999) = 100,000

Error budget remaining = 100,000 - actual_bad_events_so_far
Burn rate = (bad events in last hour) / (allowed per hour)
```

The budget governs three things:
1. **Alerting threshold** (multi-window multi-burn-rate; see 6.3).
2. **Release velocity.** When the budget is exhausted, freeze risky deploys until it recovers.
3. **Investment priority.** Persistent budget exhaustion is the strongest possible signal that reliability work needs to outrank features.

> **Mental model:** Error budget is a *currency*. Engineering can spend it on velocity (ship faster, accept some failures) or save it (slower, more conservative). The point is *making the trade-off explicit*.

### 6.3 Multi-Window Multi-Burn-Rate Alerting

The single best alerting pattern in modern SRE. Replaces brittle "p99 > X for 5m" rules.

```
Burn rate = (error rate observed) / (error rate budgeted)

If your 30-day budget burns in 1 hour, burn rate = 720× normal.

Alert pages if BOTH:
  - 1-hour burn rate ≥ 14.4 AND 5-minute burn rate ≥ 14.4   → fast burn, page now
  - 6-hour burn rate ≥ 6 AND 30-minute burn rate ≥ 6        → slow burn, page later
  - 3-day burn rate ≥ 1 AND 6-hour burn rate ≥ 1            → ticket, fix this week
```

The dual window prevents flapping (short window only) and prevents 8-hour delay before catching a fast burn (long window only). This is the SRE answer to "alerts are noisy."

### 6.4 Toil and the 50% rule

**Toil** = work that is manual, repetitive, automatable, tactical, devoid of long-term value, scaling linearly with service growth.
**Rule:** SREs spend ≤50% of their time on toil; the rest on engineering. If toil exceeds 50%, SREs are call-center workers, not engineers, and the system gets worse.

Measure toil. Show toil hours per team in dashboards. Make automating the top toil item the on-call's reward at the end of the rotation.

### 6.5 On-Call — the human reliability layer

Principles a Staff Engineer should hold the line on:
- **Sustainable rotation:** ≥6 people per primary; ≤25% of waking hours per quarter.
- **Compensation:** on-call is paid work outside business hours. (Yes, even at the principal level.)
- **Two-tier:** primary handles initial response; secondary backstops, escalates, hands off cleanly.
- **Every page is a question:** Was it actionable? If not — why did it fire? Delete it or fix it.
- **A bad on-call shift is a *system* problem, not a *person* problem.** The fix is in the alert rules, the runbook, the architecture — not in the human.

### 6.6 Incident Response (the first 60 minutes matter)

```
DETECT     → page fires (or human notices)
DECLARE    → declare incident; assign Incident Commander (IC)
TRIAGE     → IC: roles (IC, comms, ops, scribe); customer impact assessment
MITIGATE   → STOP THE BLEED. Roll back, drain traffic, fail over.
            (Don't debug the root cause yet. Mitigation > understanding.)
COMMUNICATE → status page update every 30 min minimum, even if "no change"
RESOLVE    → primary symptoms cleared; impact ended
HANDOFF    → IC writes timeline; ops ensure backoff; postmortem scheduled
POSTMORTEM → blameless write-up; action items with owners and dates
FOLLOW UP  → action items must close within their committed window
            (track this; an unclosed action item is a re-incident waiting)
```

> **Pitfall:** Senior engineers love to debug *during* the incident. Resist. The longer the customer is impacted, the worse. Mitigate first, learn second.

### 6.7 Postmortem — the institution's memory

**Blameless** means: assume good intent, focus on system contributing factors, never name a person as a cause.

A good postmortem answers:
1. **What happened?** (timeline with timestamps and links to data)
2. **What was the customer impact?** (in dollars or users where possible)
3. **What contributing factors enabled this?** (multiple — outages are never one cause)
4. **What did we do well?** (don't only cover failure modes)
5. **What did we get lucky on?** (the next-incident catalyst hides here)
6. **What action items will we take, by when, owned by whom?** (each must be a JIRA ticket with a deadline)

Track action item closure rate. <70% is a sign your postmortems are theater.

---

## 7. Variant Decision Tree

"Build my own observability stack" only makes sense once you've decided which stack. Same building blocks, different mix.

```
What's the primary constraint?
│
├── "I want one vendor to own everything; cost is secondary"
│   → SaaS APM stack (Datadog, New Relic, Dynatrace, Honeycomb)
│   Pros: fastest to value, integrated, great UX
│   Cons: $50k–$5M/yr at scale; vendor lock-in; data gravity
│   Best for: <500 services, <2000 engineers, "cost of an outage" >> "cost of telemetry"
│
├── "We are large enough that vendor cost > engineer cost"
│   → Self-hosted on Prometheus/Grafana family
│   Metrics:  Mimir or VictoriaMetrics (clustered) + Prometheus (collection)
│   Logs:     Loki for low-cost or ClickHouse for SQL flexibility
│   Traces:   Tempo (object store) or Grafana / Honeycomb hybrid
│   Profiles: Parca or Pyroscope (now in Grafana)
│   Pros: 5–10× cheaper at scale; full control; OTLP-native
│   Cons: dedicated platform team needed; on-call for the observability stack itself
│   Best for: 500+ services, mature platform team
│
├── "Massive scale, regulated, multi-region, multi-cloud"
│   → Custom + lakehouse architecture
│   Hot path: Mimir/VictoriaMetrics + Loki + Tempo
│   Cold path: OTel → Kafka → ClickHouse / BigQuery / Snowflake
│              → SQL on logs, traces, metric exemplars
│   Pros: every signal queryable in SQL; integrates with data platform
│   Cons: highest engineering cost; >5 engineers full-time
│   Best for: hyperscalers, large fintech, regulated data
│
├── "Workload is GPU / ML / accelerated"
│   → Standard stack + DCGM-exporter, NVML/RoCM, framework metrics (PyTorch, JAX)
│   See sister folder: `gpu-observability/` for the layer-by-layer guide
│
└── "We don't have engineers; we just need pages to fire when prod is broken"
    → Synthetic monitoring (Pingdom, Checkly) + uptime alerting
    + one log shipper to a SaaS log store
    Best for: <20 services, founders are also on-call
```

**Picking is mostly about scale and team maturity.** Everything else (language, cloud provider, OTel vs Prometheus-native) is implementation detail. Don't pick the architecture for the team you wish you had.

---

## 8. End-to-End Trace of One Request

Concrete trace for a single `POST /checkout` request flowing through a 6-service stack with full telemetry. Every line ties back to a doc and a tool.

```
T+0ms     Browser sends POST /checkout
            Headers include W3C traceparent (RUM-generated trace_id)   [doc 03]

T+5ms     CloudFront → ALB → Envoy gateway
            Envoy emits ACCESS_LOG to stdout (parsed by Vector)        [doc 04]
            Envoy starts root span "POST /checkout"; injects baggage    [doc 03]

T+8ms     Auth service receives request
            OTel middleware extracts traceparent, starts child span
            Validates JWT → log line {trace_id, user_id, action="auth.ok"}
            Auth gauge `auth_active_sessions` incremented              [doc 06]

T+12ms    Auth → Cart service (gRPC)
            client span for the call; tracestate propagated
            Cart server span begins; Redis GET span as grandchild
            Redis MISS — cache_miss counter +1                          [doc 06]
            DB span: SELECT cart WHERE user_id=$1
              - Postgres slow query? captured in pg_stat_statements
              - Span attributes: db.statement, db.rows=14
            Cart returns 14 items in 9ms

T+30ms    Cart → Pricing service (HTTP)
            Pricing fetches discounts via 3 parallel calls
              - 2 succeed in 8ms
              - 1 takes 950ms (degraded vendor; circuit breaker opens)
            Pricing emits log: {level=warn, msg="vendor.timeout", trace_id, ...}
            Pricing latency histogram: 950ms lands in [800, 1000) bucket
            EXEMPLAR attached: bucket sample → trace_id (clickable in Grafana)  [doc 06,11]

T+985ms   Pricing → Checkout service
            Checkout span attribute: pricing.degraded=true
            Calls Payments service (Stripe); span includes peer.service
            Stripe returns OK in 220ms
            Checkout span completes; status=OK

T+1210ms  Checkout → Order service
            DB INSERT to orders + outbox table (transactional)
            Outbox row read by Debezium → Kafka topic "order.created"
            Async consumers (notifications, analytics) — same trace_id
              propagated via Kafka headers; spans link to root trace    [doc 03]

T+1215ms  Response returns to browser; total user-perceived latency 1215ms

  ───── observability happens AFTER the request ─────

T+1216ms  All spans batched at OTel SDK; flushed every 1s or 512 spans

T+1.5s    OTel SDKs → OTel Collector (gateway pod)                      [doc 04]
            Collector tail-sampling policy:
              - status=ERROR → keep 100%
              - latency > P99 → keep 100%
              - else → keep 1%
            This trace had pricing.degraded → caught by latency policy → kept

T+2s      Collector fans out:
            - traces → Tempo (object store write)                       [doc 08]
            - metrics → Mimir via remote_write                          [doc 06]
            - logs → Loki                                               [doc 07]

T+2.5s    Mimir ingester:
            - WAL append, head block update
            - http_request_duration_seconds_bucket{le="1.0",route="/checkout"} +1
              (this exemplar carries the trace_id)
            - The `pricing_vendor_degraded_total` counter +1

T+3s      Alertmanager evaluates rules every 1m:
            sum(rate(checkout_errors_total[5m])) by (service)
              / sum(rate(checkout_requests_total[5m])) by (service)
              > 0.005
            — not firing yet (one slow request ≠ a burn rate event)

T+5m      Burn rate rule evaluates: 5m window vs 1h window
            6× 30m and 6× 6h thresholds — not breached
            (SLO budget burning slightly faster than steady-state, but within bound)  [doc 13]

T+1h      On-call engineer Mary opens Grafana dashboard
            "Checkout p99 spiked at 14:32" — clicks exemplar
            → jumps directly to the slow trace in Tempo
            → sees pricing.degraded=true on the slow span
            → opens Loki: {service="pricing"} |= "vendor.timeout"
              filtered by trace_id
            → sees 3 warnings; vendor name in attribute
            → Mary opens runbook for "pricing.vendor degraded"
              → step 1: enable kill-switch flag for vendor
              → step 2: page vendor liaison
            → ticket filed; action item on roadmap; postmortem scheduled
              if cumulative impact ≥ SLO threshold for the day            [doc 14, 15]
```

**What you just watched:**
- 6 services, 1 root trace, ~30 spans
- Three signals (metric counter, log line, span) all carry the same trace_id — that's the *correlation* that makes triage possible
- One exemplar made the metric → trace jump instant; without it, the engineer searches blindly
- Tail sampling kept this trace; it would have been the 99% dropped if not for `pricing.degraded`
- Burn-rate alerting prevented a flapping page on a single slow request, but caught it once it became sustained
- The runbook turned a debug session into a 5-minute mitigation
- Postmortem completes the loop and the action items mature back into the SLO/error-budget story

This is what observability looks like when it's healthy. Strip out any one of these layers and the engineer is back to grep + guess.

---

## 9. Linear Reading Order

If you want to read every doc once, this order minimizes "wait, what is X?" moments. (Doc numbers refer to the planned deep-dive chapters in this folder.)

1. **ROADMAP.md** ← you are here. Don't skip.
2. **00 — Mental models.** Signals, golden 4, USE/RED, observability vs monitoring. Sets the vocabulary.
3. **01 — Architecture & stack overview.** End-to-end signal flow, push vs pull, where every box lives.
4. **02 — OpenTelemetry deep dive.** SDK, Collector, semantic conventions, OTLP wire format.
5. **03 — Instrumentation.** Metrics, logs, traces, profiles at the source. Per-language idioms.
6. **04 — Collection & edge processing.** Agents (Fluent Bit, Vector, OTel agent), gateways, tail sampling.
7. **05 — Transport & buffering.** Kafka, Pub/Sub, when and why to put a queue between collection and storage.
8. **06 — Metrics storage internals.** Prometheus TSDB, Gorilla compression, postings index, Mimir/VM/Thanos comparison.
9. **07 — Logs storage internals.** Inverted index (ES) vs label-index + brute force (Loki) vs columnar (ClickHouse).
10. **08 — Trace storage internals.** Span store + service graph; Tempo's index-less architecture.
11. **09 — Profiling.** pprof, eBPF, symbolization, continuous profiling.
12. **10 — Query layer.** PromQL, LogQL, TraceQL, SQL on telemetry. Engine internals.
13. **11 — Dashboards.** Grafana, RED/USE patterns, exemplars, mobile vs ops layouts.
14. **12 — Alerting.** Alertmanager, multi-window multi-burn-rate, page hygiene.
15. **13 — SLO engineering.** SLIs, SLO math, error budgets, the four golden burn-rate windows.
16. **14 — On-call.** Rotation design, runbook standards, page response.
17. **15 — Incident response & postmortem.** The 60-minute loop and the blameless aftermath.
18. **16 — Capacity planning.** Forecasting, headroom, growth modelling.
19. **17 — Production readiness reviews.** PRR template, launch gating.
20. **18 — Cardinality & cost.** The hardest single problem in this stack.
21. **19 — Multi-tenancy.** Quotas, RBAC, isolation, billing.
22. **20 — AIOps & frontier topics.** Anomaly detection, alert grouping, LLM-assisted incident response.
23. **Appendix A — Glossary** (planned).
24. **Appendix B — Reference architectures** (small / mid / hyperscale).
25. **Appendix C — PromQL / LogQL / TraceQL recipe book.**

For "I just want to build it" mode, follow phases 0–20 in §3 instead of reading docs end-to-end.

### 9.1 Beyond-Roadmap Chapters (21+)

The original roadmap was the *plumbing* and *practice* of observability. The chapters below cover what a Staff Engineer is repeatedly asked about that the original roadmap doesn't cover.

26. **21 — Frontend / RUM / mobile observability.** Web Vitals, browser SDKs, mobile crash reporting, source maps, beacon transport, session replay.
27. **22 — Service mesh observability.** Istio, Linkerd, Cilium; sidecar vs eBPF; mesh metrics vs app metrics; cross-region trace assembly.
28. **23 — Database observability.** pg_stat_statements, slow query log, plan capture, connection pool, replica lag, per-query SLOs.
29. **24 — Network observability.** TCP retransmits, conntrack, DNS, eBPF flow telemetry, NetFlow / VPC flow logs, who-talks-to-whom.
30. **25 — Streaming / Kafka observability.** Consumer lag, partition skew, async-trace propagation, DLQ, schema registry.
31. **26 — LLM and AI observability.** Token accounting, eval harness, faithfulness, RAG, agent traces, vector-DB observability.
32. **27 — Security observability.** Audit logs, SIEM integration, MITRE ATT&CK, SRE-vs-SOC line, eBPF security.
33. **28 — Telemetry pipeline reliability.** Observe the observer; tier-0 alerts; synthetic canaries; independent paging path.
34. **29 — Synthetic monitoring.** HTTP / browser / multi-step checks; CI / pre-deploy gates; geographic distribution.
35. **30 — Error tracking.** Sentry / Rollbar / Bugsnag; fingerprint grouping; release health; source maps.

### 9.2 Enterprise Patterns (31+)

For orgs at scale (typically 200+ services).

36. **31 — FinOps for observability.** Allocation, showback, chargeback, forecasting, vendor levers.
37. **32 — Compliance and privacy.** GDPR right-to-erasure, HIPAA, audit-log integrity, schema classification, DPAs.
38. **33 — Federated multi-region.** Independent regions; hub-and-spoke; cross-region trace assembly; per-region SLOs.
39. **34 — Schema and semantic-conventions governance.** OTel SemConv, attribute registry, breaking-change policy, contract tests.
40. **35 — Telemetry lakehouse.** OTel → Kafka → Iceberg → BigQuery / Snowflake / ClickHouse; SQL on telemetry.
41. **36 — DR for the observability stack.** RPO / RTO; cross-region replication; backfill; game days.
42. **37 — Vendor migration patterns.** Datadog → self-hosted; dual-write; read-first; decommissioning.
43. **38 — Continuous verification.** Chaos engineering with measurable hypotheses; deploy markers; canary verification.
44. **39 — Build-vs-buy framework.** TCO modeling; inflection point; hybrid; annual revisit.
45. **40 — IDP and golden paths.** Backstage; service catalog; scorecards; templates with observability built in.
46. **41 — Brownfield integration.** Acquisitions; multi-vendor coexistence; deprecation; "do not consolidate" cases.

### 9.3 Appendices

47. **Appendix A — Glossary** (full vocabulary across the folder).
48. **Appendix B — Reference architectures** (small / mid / hyperscale).
49. **Appendix C — PromQL / LogQL / TraceQL recipe book.**

---

## 10. Common Pitfalls When Building Your Own Platform

The list of mistakes you (and every textbook stack) will make on the first try.

1. **Treating observability as a tool problem.** It's a *practice* problem. The tools enable the practice, but no tool will save a team that hasn't agreed on what an SLO is. → §6.
2. **Threshold alerts everywhere.** "p99 > 500ms for 5m" pages on every Tuesday deploy and on no real outages. Replace with multi-window multi-burn-rate against an SLO. → §6.3.
3. **Adding a new label to every metric to make a new dashboard work.** Cardinality explodes silently; the Prometheus dies in 6 weeks. Push high-cardinality dimensions to logs/traces. → §5.1.
4. **100% trace sampling in production.** Bill is 10× higher than it needs to be; storage chokes. Use head sampling for the hot path + tail sampling for the rare/error path. → §5.2.
5. **Logs and metrics are the same thing.** No. A log line is an *event with structure*. A metric is an *aggregate over events*. Logs scale with traffic; metrics scale with cardinality. Mixing them up wastes either money or queries. → §2.1, §2.2.
6. **No correlation IDs.** Logs without `trace_id` cannot be joined to traces. The single highest-leverage change in 90% of stacks is "make every log line carry trace_id." → §2.2.
7. **Dashboards before SLOs.** Engineers stare at panels with no numerical decision criteria. Define the SLO first, then the dashboard becomes obvious. → §6.1.
8. **Vendor everything until cost panic, then cancel everything.** Both extremes are bad. Run a 12-month cost projection at the *current* vendor before signing. → §7.
9. **Alerting on causes, not symptoms.** "Disk usage > 80%" pages even when the user is fine. Alert on the symptom (latency, errors); use saturation as a *secondary diagnostic*. → §6.1.
10. **Collector-less direct push from SDKs to vendor.** Couples every service to one vendor's API; complicates redaction. Always run a collector layer. → §4 (component map).
11. **Nobody owns the observability stack.** It's "everyone's responsibility" — meaning nobody's. The platform needs an owning team with its own SLOs (yes, the observability platform also has SLOs).
12. **Postmortem theater.** Documents written, never read, action items never closed. Track closure rate and treat <70% as a fire. → §6.7.
13. **Confusing availability with quality.** A service can be 100% "up" while serving wrong answers. SLIs must measure the user's *successful experience*, not the process's heartbeat. → §6.1.
14. **Adding profiling last.** Continuous profiling closes the "we know it's slow but not where" gap with one rollout. It's much higher leverage than its position in most stacks suggests. → Phase 14.
15. **No chaos engineering until after a P0.** Once you've already had the outage, you have less appetite, less budget, and less trust. Inject failure on a Tuesday afternoon when the room is calm. → Phase 18.
16. **Not measuring on-call health.** Pages/week, sleep-pages, time-to-ack, incident MTTM — track these as platform SLIs. A degrading on-call experience is a platform regression. → §6.5.
17. **Sampling decisions made per-process.** Service A samples in, Service B samples out — the trace is broken halfway through. Propagate the sampling decision via tracestate. → §5.2.
18. **Using mean latency for anything.** Means hide tail behavior. Always use percentiles (p50, p95, p99, p99.9). → §6.1.
19. **Collecting telemetry that nobody queries.** Every metric, log, and span has an opportunity cost. Quarterly: list all metrics no dashboard or alert references. Delete them. → §5.4.
20. **Skipping production readiness review.** Services launch with no SLO, no runbook, no dashboard, no alert. They cause outages. PRR is the cheapest reliability investment a platform team makes. → Phase 17.

---

## 11. Glossary of Terms a Staff Engineer Should Use Correctly

A short list — the per-term semantic precision of which betrays seniority in a 1:1 with leadership.

| Term | Precise meaning | Common misuse |
|---|---|---|
| **Observability** | The property that internal state can be inferred from external outputs | Used as a synonym for "monitoring"; it isn't |
| **Monitoring** | Watching predefined signals against predefined thresholds | Used to mean the whole stack |
| **Telemetry** | The raw data emitted by systems (metrics/logs/traces/profiles) | Used to mean "dashboards" |
| **Cardinality** | Number of unique time series for a metric | Used to mean "size in bytes" |
| **SLI** | A measurement of the user's experience as good_events/total_events | Used as a synonym for "metric" |
| **SLO** | Internal target on an SLI | Used to mean "SLA" |
| **SLA** | External, contractual promise (often with credits) | Used to mean "SLO" |
| **Error budget** | Allowed bad events in a window = (1 - SLO) × total | Used as "the time we're allowed to be down" — close, but the unit matters |
| **Toil** | Manual, repetitive, automatable work that scales with traffic | Used to mean "any work I don't like" |
| **Page** | An automated, urgent escalation to a human | Used to mean "any alert" |
| **MTTR / MTTM / MTTD** | Mean time to recover / mitigate / detect (different!) | Used interchangeably; they're not |
| **Incident** | An unplanned event causing user-impacting degradation | Used for any bug |
| **Postmortem** | A blameless retrospective document | Used to mean "writeup" without the blamelessness |
| **Burn rate** | Rate of error budget consumption relative to steady-state | Used to mean "rate of errors" |
| **Tail sampling** | Decision after trace assembly | Conflated with head sampling |
| **Continuous profiling** | Stack-trace sampling stored over time | Used to mean any profiler invocation |
| **Saturation** | How "full" a resource is (queue depth, mem pressure) | Used as a synonym for "utilization" |
| **Utilization** | Fraction of time a resource is busy | Used as a synonym for "saturation" |
| **Golden signals** | Latency, traffic, errors, saturation (Google SRE) | Used loosely for "important metrics" |
| **RED** | Rate, Errors, Duration (per service)        | Used for non-service-level signals |
| **USE** | Utilization, Saturation, Errors (per resource) | Used for non-resource-level signals |

---

**TL;DR pipeline.** *Workload → instrument → collect → transport → store (metrics/logs/traces/profiles) → query → dashboards/alerts → on-call → incident → postmortem → SLO → instrument better*. With **OpenTelemetry** as the universal SDK, **cardinality + sampling + retention** as the three knobs that govern cost, **multi-window multi-burn-rate** as the alerting pattern that doesn't lie, and **error budgets** as the lever that makes the whole practice an engineering discipline instead of a cost center. Build it in that order. Every later doc in this folder is one of the boxes seen up close.
