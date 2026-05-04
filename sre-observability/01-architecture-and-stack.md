# 01 — Architecture & Stack Overview

> The map of every layer in a modern observability stack — what each layer does, where signals enter and exit, what breaks at each hop, and the load-bearing decisions a platform team has to make before any line of YAML is written.

This chapter wires the boxes together. Subsequent chapters zoom into individual layers — `doc 02` (OpenTelemetry), `doc 03` (instrumentation), `doc 04` (collection), `doc 06`–`doc 09` (per-store internals), `doc 12` (alerting), `doc 13` (SLO engineering). If the picture in §1 is fuzzy when you finish this doc, read it again before going further; the rest of the series assumes it.

Vocabulary in this chapter is defined in `doc 00` (mental models). If "SLI", "cardinality", "head vs tail sampling", or "exemplar" are not crisp in your head, start there.

---

## 1. The End-to-End Signal Flow

A production observability stack has three immutable properties:

1. **Four signal types** travel mostly-independent pipelines: metrics, logs, traces, profiles. They cross only at the *correlation* points — typically `trace_id`, `service.name`, `tenant_id`.
2. **Two physical tiers** of agents handle data movement: a **node-local agent** (one per host) and a **central gateway/collector** (a fleet, behind a load balancer). Smaller deployments collapse these; larger deployments add a third tier per region.
3. **Four storage backends** sit behind the gateway, each tuned for one signal shape. They share *nothing* internally — different on-disk format, different query language, different scaling story. They share *everything* externally — the same identifiers (`trace_id`, `service`, time window) make cross-store joins possible at query time.

Here is the full picture for a mid-size production deployment. Memorize this; every later diagram is a subset.

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                              WORKLOAD                                              │
│   services · batch jobs · CronJobs · Lambdas · sidecars · mobile/web clients       │
└──────────────────────────────────┬─────────────────────────────────────────────────┘
                                   │  emit at the source
                                   ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│  L1  INSTRUMENTATION                                                  ── doc 03    │
│      OTel SDK (counters/histograms/spans)  ·  Prometheus client  ·  slog/zerolog   │
│      eBPF auto-instrumentation (Beyla, Pixie, Parca)                               │
│      → emits OTLP-gRPC, Prometheus exposition, JSON logs to stdout                 │
└──────────────────────────────────┬─────────────────────────────────────────────────┘
                                   │  loopback / unix socket / stdout
                                   ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│  L2  NODE-LOCAL AGENT  (DaemonSet, one per host)                      ── doc 04    │
│      Fluent Bit · Vector · OTel Collector "agent" mode · node_exporter             │
│      jobs: tail files, scrape /metrics, enrich with k8s metadata, redact PII,      │
│            batch/compress, hold a small disk buffer (e.g., 1 GB) for backpressure  │
└──────────────────────────────────┬─────────────────────────────────────────────────┘
                                   │  OTLP-gRPC over mTLS, or remote_write
                                   ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│  L3  GATEWAY / AGGREGATOR CLUSTER  (Deployment, behind a Service)     ── doc 04    │
│      OTel Collector "gateway" pods  ·  Vector aggregator                           │
│      jobs: tail-sample traces (30s window), fan out to N backends, enforce         │
│            tenancy headers, vendor abstraction, per-tenant rate limits, SDK→export │
│            translation (e.g., OTLP → remote_write, Loki push, Tempo push)          │
└──────────────────────────────────┬─────────────────────────────────────────────────┘
                                   │   (optional, see §3 and §5)
                                   ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│  L4  TRANSPORT / DURABLE BUFFER                                       ── doc 05    │
│      Kafka / Redpanda · Kinesis · Pub/Sub                                          │
│      jobs: decouple producers from storage; survive a 4-hour Mimir outage without  │
│            losing data; replay for backfill; multi-consumer fan-out (hot store +   │
│            cold lake)                                                              │
└──────────────────────────────────┬─────────────────────────────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┬───────────────────────┐
        ▼                          ▼                          ▼                       ▼
┌──────────────────┐    ┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────┐
│  METRICS         │    │  LOGS                │   │  TRACES              │   │  PROFILES        │
│  ── doc 06       │    │  ── doc 07           │   │  ── doc 08           │   │  ── doc 09       │
│                  │    │                      │   │                      │   │                  │
│  Prometheus      │    │  Loki   (label-idx + │   │  Tempo  (object-     │   │  Pyroscope /     │
│  Mimir / Cortex  │    │          object str) │   │          store, no   │   │  Parca / Polar   │
│  VictoriaMetrics │    │  ClickHouse / Splunk │   │          inv. index) │   │  Signals         │
│  Thanos          │    │  Elasticsearch /     │   │  Jaeger (Cassandra/  │   │  (eBPF +         │
│                  │    │   OpenSearch         │   │          ES)         │   │   symbolizer)    │
│  WAL → block →   │    │  S3 + Athena (cold)  │   │  ClickHouse (rich    │   │  pprof in S3     │
│  object storage  │    │                      │   │          SQL traces) │   │                  │
└────────┬─────────┘    └──────────┬───────────┘   └──────────┬───────────┘   └────────┬─────────┘
         │                         │                          │                        │
         └─────────────────────────┴──────────┬───────────────┴────────────────────────┘
                                              ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│  L5  QUERY LAYER                                                      ── doc 10    │
│      PromQL · LogQL · TraceQL · SQL (ClickHouse, BigQuery)                         │
│      query frontend: split-by-time, parallel shards, result cache (memcached)      │
│      query scheduler: per-tenant queue, max in-flight, query timeout               │
└──────────────────────────────────┬─────────────────────────────────────────────────┘
                                   ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│  L6  CONSUMPTION                                                      ── doc 11+12 │
│      Grafana dashboards · Alertmanager / Grafana alerts · APM UIs (Tempo/Jaeger)   │
│      PagerDuty · Opsgenie · ChatOps (Slack) · runbook automation · notebooks       │
└──────────────────────────────────┬─────────────────────────────────────────────────┘
                                   ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│  L7  SRE PRACTICE LOOP                                                ── doc 13–17 │
│      SLI/SLO definition → error-budget burn → on-call → postmortem → backlog       │
└────────────────────────────────────────────────────────────────────────────────────┘
```

> **Mental model:** every observability stack is **emit → ship → store → query → alert**. Everything else — SLOs, golden signals, exemplars, anomaly detection — is choreography on top of those five primitives. Production observability is hard because each primitive must work at three to five orders of magnitude beyond a textbook example.

### 1.1 The five-to-eight hops to memorize

For each hop, what its budget is, what breaks, and how you detect it.

| # | Hop | Typical latency | Typical volume (1k svc / 10k pod org) | What can break | First symptom |
|---|-----|-----------------|---------------------------------------|----------------|---------------|
| 1 | Workload → SDK in-process | <1 µs | 100k events/sec/pod (logs+spans+counters) | SDK bug, dropped batches, `BatchSpanProcessor` queue full | `otelcol_exporter_send_failed_spans_total` rises, app log "span queue full" |
| 2 | SDK → node agent (loopback) | ~50 µs | 5–20 MB/s/node | Agent crash, socket EPIPE, agent OOMKilled | `up{job="otel-agent"} == 0`, k8s pod CrashLoopBackOff |
| 3 | Node agent → gateway (intra-cluster) | 0.5–2 ms | 50–500 MB/s aggregate | Network partition, mTLS expiry, gateway HPA underscaled | `otelcol_exporter_queue_size` near `queue_capacity`, 5xx from gateway |
| 4 | Gateway tail-sampler → exporter | 30 s window then 1–5 ms | 1–10% of trace volume after sampling | Misconfigured policy drops everything (incl. errors), assembly-window overflow | trace volume in Tempo flatlines, error traces missing |
| 5 | Gateway → Kafka | 1–3 ms | 100k–1M msgs/sec | Broker overloaded, partition skew, ISR shrink | `kafka_producer_buffer_bytes`, `under_replicated_partitions > 0` |
| 6 | Kafka → store ingester (Mimir/Loki/Tempo) | 5–50 ms | 1–10 GB/min ingest | Ingester OOM, WAL corruption, S3 throttling | `*_ingester_memory_series` plateau, S3 503 rate-limit errors |
| 7 | Store WAL → block → object storage | 2 h compaction cycle | 100 GB–10 TB/day across signals | Compactor stuck, S3 MultipartUpload failure | head block age > 4 h, `prometheus_tsdb_compactions_failed_total > 0` |
| 8 | Query → store → result | 100 ms – 10 s | 10–500 QPS | Slow query, hot tenant, query frontend cache miss | `cortex_query_frontend_queue_length` rises, p99 query time spikes |

Latency is end-to-end *visibility* delay (workload event → queryable in store): **typically 5–60 seconds** in steady state, **two-hour tail** for compacted historical data. SLO alerts fire on the 5–60s path; capacity-planning queries hit the 2h+ path. Confusing the two is a common source of "why is my dashboard 90 minutes behind?" tickets.

> **Pitfall:** the latency you measure with `time curl /metrics` is hop 3. The latency the on-call engineer experiences is hops 1 → 8. They differ by ~four orders of magnitude. Always measure end-to-end ingest latency as a platform SLI.

---

## 2. Layer Responsibilities

Each layer below has a strict job description. The diagonal observation is that *each component owns exactly one concern*; when components seem to overlap (e.g., "should the agent enforce cardinality, or the TSDB?"), production stacks pick the one closest to the source. Crossing the line is the source of most platform bugs.

### 2.1 Instrumentation (L1)

**Owns:** emitting signals at the source with correct semantic conventions; producing the raw `(name, attributes, value, timestamp)` tuples; *propagating context* (W3C `traceparent`, baggage) across process boundaries.

**Doesn't own:** sampling decisions for the whole system (only its own head-sampling decision); transport reliability; retention; query.

**Common production implementations:**
- **OpenTelemetry SDKs** for Go, Java, Python, Node, .NET, Rust, Ruby, PHP, C++. The default for new code in 2026.
- **Prometheus client libraries** (`prometheus/client_golang`, `prometheus_client_python`, etc.) for metrics-only services.
- **Structured-log libraries:** `slog` (Go 1.21+), `structlog` (Python), `zerolog`/`zap` (Go), `Serilog` (.NET), Logback with JSON layout (Java).
- **eBPF auto-instrumentation:** Grafana Beyla, Pixie, Parca, Cilium Tetragon. Zero code change, kernel-level visibility.

**Why this layer exists at all:** because the only place where the program knows what it *meant* to do is inside the program. Out-of-band tools (eBPF, packet capture, sidecar proxies) can recover *some* of that intent, but they cannot see business-meaningful labels (`customer_tier`, `feature_flag`) without help. Instrumentation is the contract between the developer's mental model and the observability stack — every other layer just moves bytes.

### 2.2 Node-local agent (L2)

**Owns:** reading host-level signals (`/proc`, `/sys`, journald, kubelet's `/metrics/cadvisor`); tailing log files (`/var/log/containers/*.log`); enriching with k8s metadata (namespace, pod, node, labels); light filtering (drop debug logs, redact emails, downsample); a small disk buffer (~1 GB) for survivability across short network blips.

**Doesn't own:** tail sampling (needs a full trace's spans, which are scattered across nodes); cross-tenant aggregation; long-term storage; query.

**Common production implementations:**
- **Fluent Bit** (CNCF graduated) — log-focused, ~5 MB RSS, written in C; the default log shipper in EKS/GKE/AKS.
- **Vector** (Datadog) — multi-signal, written in Rust, richer transform DSL (VRL), heavier footprint (~50 MB).
- **OTel Collector "agent" mode** — multi-signal, the OTel-native choice. Heaviest footprint (~80–150 MB) but uniform config across signals.
- **node_exporter** — Prometheus's official host-stats exporter. Pull-based; not really an "agent" but lives in the same slot.
- **Promtail** — Loki's log shipper. Being deprecated in favor of Grafana Alloy (which is OTel Collector + Prometheus + Loki client merged).

**Why this layer exists at all:** because the cheapest place to drop a signal is *before it leaves the box*. A 70 % drop ratio at the agent (drop DEBUG logs, sample 1-in-10 200-OK request logs) cuts your logging bill 3× before the first byte hits the network. It's also the only place that has cheap, reliable access to host-level context (kernel cgroup IDs, NUMA topology, k8s pod metadata via the kubelet API).

### 2.3 Gateway / collector cluster (L3)

**Owns:** trace tail-sampling (needs all spans for a `trace_id` to converge); per-tenant rate limiting; vendor fan-out (one OTLP stream → Mimir + Honeycomb + an S3 archive simultaneously); TLS termination; auth header rewriting; SDK-to-backend protocol translation (OTLP → Prometheus `remote_write`, OTLP → Loki push, OTLP → Tempo push).

**Doesn't own:** host-level signals (those are L2's); long-term storage; per-trace query.

**Common production implementations:**
- **OTel Collector** in "gateway" deployment mode — same binary as the agent, different config. Run 3–20+ replicas behind a Service.
- **Vector aggregator** — Vector deployed as a stateful aggregator. Strong for log routing; weaker for traces (no native tail-sampling).
- **Grafana Alloy** — OTel-Collector-derived, with first-class Prometheus, Loki, Tempo support. Increasingly the default in Grafana-stack shops.

**Why this layer exists at all:** because some decisions can only be made *after data has been collated*. Tail sampling needs the full trace before deciding to keep it. Cardinality enforcement needs to see *all* series from *all* nodes to apply a global cap. Vendor abstraction needs a single point that owns the contract. Without L3, every node-local agent has to know about every backend — N × M coupling that becomes unmaintainable at >50 services.

### 2.4 Transport / durable buffer (L4)

**Owns:** decoupling producers from storage; surviving a 4-hour storage outage without losing a byte; multi-consumer fan-out (the same Kafka topic feeds the hot Mimir and a cold ClickHouse lake); replay for backfill (rewind a topic offset, re-ingest a day).

**Doesn't own:** schema enforcement (it's bytes-in, bytes-out); query; transformation logic.

**Common production implementations:**
- **Apache Kafka** — the default. 3 – 9 brokers per region; topics partitioned by `tenant_id` or `service.name`.
- **Redpanda** — Kafka-API-compatible, written in C++, lower per-broker cost, no ZooKeeper.
- **AWS Kinesis Data Streams** — the managed variant; cheaper to operate, more constrained partition model.
- **GCP Pub/Sub** — managed, push-or-pull, simpler but less throughput per topic.
- **NATS JetStream** — lightweight alternative for sub-100k msgs/sec deployments.

**Why this layer exists at all:** because the failure mode "storage went down for 4 hours" is unrecoverable without a durable queue in front. Without Kafka, the agents either drop data or backpressure all the way to the application (which now has its own latency spike). With Kafka, agents keep producing, the lag grows, and when storage recovers it catches up. Kafka also makes "send the same data to two backends" a config change rather than a re-architecture.

> **Pitfall:** small teams skip L4 and run agent → store directly. This works at <50 services. The day Mimir's S3 bucket throttles for 30 minutes — and S3 *will* throttle eventually — every agent on every node spills its disk buffer and starts dropping. Adding Kafka after the first such incident is *more* work than starting with it.

### 2.5 Metrics store (L5a)

**Owns:** time-series ingest, compression (Gorilla / XOR for floats, varint for timestamps), inverted index keyed by label set, range-vector queries (`rate`, `increase`, `histogram_quantile`).

**Doesn't own:** logs, traces, alert evaluation (that's a separate engine that *queries* the metrics store).

**Common production implementations:**
- **Prometheus** — the reference. Single binary, 15-day retention default, no built-in HA or long-term storage.
- **Mimir** (Grafana) — Cortex fork, 6 microservices, multi-tenant by design, S3-backed, supports billions of active series. As of Mimir 3.0 (Nov 2025), distributors and ingesters are decoupled by Kafka.
- **VictoriaMetrics** — single-binary or clustered; 2–5× cheaper RAM than Prometheus per series; PromQL-compatible (mostly).
- **Thanos** — Prometheus-sidecar-based; uses Prometheus as the ingester, S3 as the long-term store, and a global querier to fan out.
- **InfluxDB** — own query language (Flux/InfluxQL); historically strong, less interoperable with the OTel/Prom ecosystem.

**Why this layer exists at all:** because the access pattern for time-series data — "give me a function over a window for these label matchers" — is unlike anything an OLTP or OLAP database is optimized for. A purpose-built TSDB compresses 16-byte (timestamp, float64) tuples to ~1.3 bytes/sample with Gorilla; PostgreSQL stores the same as ~80 bytes/row. At 50M samples/sec, that's the difference between a $30k and a $2M ingest cluster.

### 2.6 Logs store (L5b)

**Owns:** high-volume append (TB/day), search (label-indexed *or* full-text inverted index), retention tiering, schema-on-read (most stores) or schema-on-write (ClickHouse).

**Doesn't own:** dashboards (those are Grafana panels with log queries), alerts on log content (that's the alerting engine reading log-derived metrics).

**Common production implementations:**
- **Loki** (Grafana) — index only labels, brute-force grep over object-storage chunks. 5–10× cheaper than Elasticsearch; narrower query patterns.
- **Elasticsearch / OpenSearch** — inverted index on every term + doc store. Most flexible queries; most expensive.
- **Splunk** — proprietary; the gold standard for security/audit logs; six-figure list price at scale.
- **ClickHouse** — columnar, schema-on-write, full SQL. Increasingly the choice for "logs as data" stacks.
- **S3 + Athena / BigQuery** — cheapest cold tier; query-time cost; minutes-not-seconds latency.

**Why this layer exists at all:** because logs are *cardinality unbounded* (every log line is unique) and *retention bounded* (most are useless after 7 days). The architectural question is always "how do I make this $$$ thing $". Two answers: index less (Loki), or compress harder + columnar (ClickHouse). See `doc 07`.

### 2.7 Traces store (L5c)

**Owns:** span ingest keyed by `trace_id`; service-graph derivation (spanmetrics: per-edge RED metrics auto-computed from spans); trace search by attributes (service name, status, duration, custom tag).

**Doesn't own:** cross-trace analytics that aren't service-graph-shaped (those go to a SQL store like ClickHouse).

**Common production implementations:**
- **Tempo** (Grafana) — index-less; trace-id lookup via S3 layout + bloom filters; spanmetrics generated upstream by the collector.
- **Jaeger** — Cassandra/Elasticsearch backend; the original. Heavier to operate; richer attribute search.
- **ClickHouse-on-spans** — store spans in a wide table partitioned by hour, queryable in SQL. Used by Uber's M3 successor and several large fintechs.
- **Honeycomb / Lightstep / Datadog APM** — SaaS; generally use bespoke columnar stores tuned for span analytics.

**Why this layer exists at all:** because a trace is a DAG of spans that needs to be reassembled by `trace_id`, and the typical query ("find slow traces matching this attribute") is poorly served by either a TSDB or a log index. Tempo's bet is that you almost never need a full inverted index across spans — you query by trace_id (cheap) or by spanmetrics-derived metrics (cheap). The few times you need full-attribute search, you use ClickHouse.

### 2.8 Profiles store (L5d)

**Owns:** stack-trace aggregation; symbol resolution (binary → function name); diff queries (commit A vs commit B); time-windowed flamegraphs.

**Doesn't own:** real-time alerting (profiles are sampled at 10–100 Hz and aggregated over minutes).

**Common production implementations:**
- **Pyroscope** (now part of Grafana) — pprof-native, language-agnostic, eBPF support.
- **Parca** (Polar Signals) — eBPF-first, pprof-native.
- **Polar Signals Cloud** — managed Parca.

**Why this layer exists at all:** because once your latency dashboard says "service X is slow", you still don't know *which line of code*. Profiling closes that gap with one rollout. See `doc 09`.

### 2.9 Query layer (L5e)

**Owns:** translating PromQL/LogQL/TraceQL/SQL to physical fetches; query splitting (by time range, by series shard); result caching; per-tenant query queue; query timeout enforcement.

**Doesn't own:** storage layout (that's the store's job); UI (that's Grafana's job).

**Common production implementations:**
- **Prometheus query engine** (built-in)
- **Mimir query frontend + querier** — splits 24h queries into 24×1h subqueries, runs in parallel, caches per-shard.
- **Grafana Loki query frontend** — same pattern for LogQL.
- **ClickHouse SQL engine** — for SQL-on-telemetry stacks.
- **Trickster / Promxy** — caching reverse proxy in front of vanilla Prometheus.

**Why this layer exists at all:** because a 30-day PromQL query over 1B series is unacceptable as a single sequential scan. Splitting into 30 parallel 1-day chunks (each backed by a separate ingester or block) is what makes Grafana panels render in under 5 seconds.

### 2.10 Consumption (L6)

**Owns:** dashboards (Grafana), alert evaluation and routing (Alertmanager), notification delivery (PagerDuty), exploration UX (Grafana Explore, log live-tail, flamegraph viewer), runbook linkage.

**Doesn't own:** the source of truth for SLOs (that lives in `doc 13`'s SLO definition store like Sloth/Pyrra/Nobl9), the runbooks themselves (those are versioned docs).

**Why this layer exists at all:** because a query is useless if the human can't see it. This layer is also where most "wait, why is this dashboard so slow?" complaints land — typically because someone wrote a panel that hits 100k series with no aggregation. See `doc 11`.

---

## 3. Push vs Pull, and Where Each Belongs

The single most religious argument in observability. The right answer is **pull for steady-state metrics, push for everything else**, and a hybrid is the production norm.

### 3.1 The pull model (Prometheus)

```
   ┌──────────────┐                  ┌────────────────┐
   │  Prometheus  │ ── HTTP GET ───▶ │  /metrics on   │
   │              │                  │  every target  │
   │  scheduler   │ ◀── 200 OK ───   │                │
   └──────────────┘   exposition fmt └────────────────┘
   every 15s
```

**What pull buys you:**
- **Free `up{}` series.** Every scrape is a liveness probe. When the agent dies, `up == 0` immediately — indistinguishable from a real outage, which is what you want.
- **Server-side rate limiting.** Prometheus controls the cadence. A bursty client can't melt the ingester.
- **Service-discovery-driven targets.** Kubernetes `kubernetes_sd_configs`, EC2 SD, Consul SD — Prometheus learns about new pods automatically; the pods don't need to know about Prometheus.
- **Deterministic relabeling.** Drop labels, hash-based sharding, target filtering — all happen on the scraper, before ingest.

**What pull cannot do:**
- **Short-lived jobs.** A 10-second batch job finishes before any 15s scrape window. Pull misses it entirely.
- **Mobile/web clients.** You can't scrape a phone.
- **NAT'd or firewalled workloads.** Lambdas, on-prem behind a NAT. The scraper can't reach them.

### 3.2 The push model (OTLP, Pushgateway)

```
   ┌────────────────┐                  ┌──────────────┐
   │  Application   │ ── OTLP/gRPC ─▶  │  Collector / │
   │  (OTel SDK)    │  protobuf,       │  Pushgateway │
   │                │  batched         │              │
   └────────────────┘                  └──────────────┘
   every 1s or 512 events
```

**What push buys you:**
- **Reaches anywhere.** The client connects out — works behind NAT, on mobile, from a Lambda.
- **Captures short-lived work.** Batch jobs flush before they exit; their metrics survive.
- **Aligns with traces and logs.** OTLP is one protocol for all three signal types.
- **Exporter-free.** No `/metrics` endpoint, no exposition format, no scrape config.

**What push cannot do well:**
- **Liveness.** "The metric stopped updating" is indistinguishable from "the metric is constant". Detection requires a *separate* heartbeat.
- **Backpressure.** Without retries + buffer, a slow ingester drops samples on the producer.
- **Multi-tenancy enforcement.** The producer claims its `tenant_id` header; the gateway has to trust-but-verify.

### 3.3 The hybrid (what real stacks do)

```
        ┌────────────────────┐         ┌────────────────────┐
        │   Long-running     │         │   Short / mobile / │
        │   services         │         │   batch / serverless│
        └─────────┬──────────┘         └─────────┬──────────┘
                  │ /metrics                     │ OTLP push
                  ▼                              ▼
        ┌────────────────────┐         ┌────────────────────┐
        │  Prometheus scrape │         │  OTel Collector    │
        │                    │         │  (push receiver)   │
        └─────────┬──────────┘         └─────────┬──────────┘
                  │                              │
                  └──────────────┬───────────────┘
                                 ▼
                       remote_write to Mimir
```

**Rules of thumb:**
- **Steady-state HTTP/gRPC services:** scrape `/metrics`. Free liveness, deterministic.
- **Batch jobs / cron / Lambdas:** push to Pushgateway or OTLP gateway, scoped to `job_completion_*` metrics only. Do NOT push counters that should accumulate forever — Pushgateway breaks that semantic.
- **Traces:** always push (OTLP). There is no pull model for traces; the SDK has to flush spans.
- **Logs:** always push (or have an agent tail files, which is a push from the agent's perspective).
- **Profiles:** push (pprof endpoint pull is a degenerate case used in eBPF setups).

> **Mental model:** "pull vs push" is really "who decides cadence?". Pull = the platform decides; push = the producer decides. Pull is better for *capacity* (the platform protects itself); push is better for *coverage* (no producer left out). Hybrid lets each signal use the right answer.

---

## 4. The "Agent + Gateway" Topology

The single most important deployment-shape decision. Get this right and the rest of the stack scales. Get it wrong and you'll be debugging a hairball of N-by-M coupling between SDKs and backends in 18 months.

### 4.1 Why two tiers

```
   ┌──────────────────────────────────────────────────────────────┐
   │                       NODE (one of N)                        │
   │   ┌──────────┐   ┌──────────┐   ┌──────────┐                 │
   │   │  pod A   │   │  pod B   │   │  pod C   │                 │
   │   │ OTel SDK │   │ OTel SDK │   │ slog→    │                 │
   │   │          │   │          │   │ stdout   │                 │
   │   └────┬─────┘   └────┬─────┘   └────┬─────┘                 │
   │        │ OTLP         │ OTLP         │ file                  │
   │        ▼              ▼              ▼                       │
   │   ┌────────────────────────────────────────┐                 │
   │   │   NODE-LOCAL AGENT  (DaemonSet)        │                 │
   │   │   - tail logs, scrape /metrics          │                │
   │   │   - enrich w/ k8s metadata             │                 │
   │   │   - drop debug, redact PII             │                 │
   │   │   - 1 GB disk buffer                   │                 │
   │   └─────────────────┬──────────────────────┘                 │
   └─────────────────────┼────────────────────────────────────────┘
                         │  one TCP connection per node
                         ▼
   ┌──────────────────────────────────────────────────────────────┐
   │   GATEWAY CLUSTER  (Deployment, 3–20 replicas, behind a Svc) │
   │   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
   │   │  gateway    │  │  gateway    │  │  gateway    │          │
   │   │  pod  1     │  │  pod  2     │  │  pod  3     │          │
   │   │             │  │             │  │             │          │
   │   │ tail-sample │  │ tail-sample │  │ tail-sample │          │
   │   │ vendor f/o  │  │ vendor f/o  │  │ vendor f/o  │          │
   │   │ rate-limit  │  │ rate-limit  │  │ rate-limit  │          │
   │   └─────┬───────┘  └─────┬───────┘  └─────┬───────┘          │
   │         └────────────────┼────────────────┘                  │
   └──────────────────────────┼───────────────────────────────────┘
                              ▼
                    Mimir / Loki / Tempo / S3 archive / Kafka
```

**Why two tiers?**

1. **Scale boundary.** Agents scale with *node count* (≤10k nodes). Gateways scale with *signal volume* (≤100 GB/s aggregate). These are different scaling axes; co-locating them couples them.
2. **Failure isolation.** A gateway pod can OOM and Kubernetes restarts it; the disk buffer in agents covers the gap. If the agents *were* the gateway, every pod's data is at risk.
3. **Tail sampling needs collation.** A trace's spans are scattered across N pods on N nodes. Only a *centralized* point can wait 30 seconds and decide "this trace had an error in service-X, keep it". Per-node agents can't tail-sample alone.
4. **Vendor abstraction.** The gateway is the one config file that knows about Mimir, Loki, Tempo, the cold S3 archive, and the third-party APM. Agents only know "send to gateway".
5. **Per-tenant rate limits.** The gateway sees the global flow per tenant; agents only see one node's flow. Global enforcement requires the gateway.

### 4.2 When single-tier is acceptable

Small fleets — say <50 nodes, <500 services, single region, single tenant — can run **agent-only**: every node's agent ships directly to the storage backend. This is fine until any of these is true:

- You have >2 storage backends (now every agent has N exporters configured).
- You need tail sampling.
- You have >1 tenant.
- You ship cross-region.

When you cross any of those, add the gateway. It's a 1-day project before the spaghetti, a 2-month project after.

### 4.3 When three-tier is needed

Hyperscale (>10k services, multi-region, regulated):

```
   per-node agent  →  per-region gateway  →  global aggregator  →  storage
```

The middle tier (per-region gateway) does tail sampling and per-region enforcement; the global aggregator handles cross-region dedup, multi-tenant routing, and bridges to the cold lake. Used at FAANG scale and at hyperscaler-internal observability platforms. Most orgs do not need three tiers — and adding one prematurely is the most expensive mistake in §10.

---

## 5. Multi-Region and Multi-Cluster Architecture

Once you have >1 region or >1 Kubernetes cluster, you have to decide *where the source of truth lives* for each signal and *who can query it from where*. There are four production patterns; pick one before you have eight.

### 5.1 Pattern A — Prometheus federation (deprecated for the hot path)

```
   region-A Prom ─┐
   region-B Prom ─┼─▶  global Prom  (scrapes /federate, only rollups)
   region-C Prom ─┘
```

**Status:** still works, no longer recommended for hot-path queries. Used by Flipkart at 80M series (InfoQ, Oct 2025) and a long tail of pre-2020 stacks.

**Pros:** zero new infra; pure Prometheus.
**Cons:** central Prom has only the *rollups* the leaves chose to expose; high-cardinality drilldown across regions is impossible; `/federate` is a synchronous pull and slow leaves block fast ones.

### 5.2 Pattern B — `remote_write` to a central Mimir/VictoriaMetrics

```
   region-A Prom ──remote_write──┐
   region-B Prom ──remote_write──┼──▶ Mimir distributor → ingester → S3
   region-C Prom ──remote_write──┘
```

**Status:** the modern default for self-hosted stacks.

**Pros:** every sample queryable from one query frontend; multi-tenancy native; per-tenant cardinality enforcement; horizontally scalable to billions of active series.
**Cons:** cross-region `remote_write` egress is real money (10 TB/month at $0.02/GB = $200/month per region pair just for samples); a Mimir outage takes everyone down (mitigated with regional Mimirs + global query layer, see Pattern D).

### 5.3 Pattern C — Thanos sidecar (per-region, query-time fan-out)

```
   region-A: [Prom + Thanos sidecar] ──▶ S3 ◀── store gateway ◀── Querier
   region-B: [Prom + Thanos sidecar] ──▶ S3 ◀──┘                    ▲
   region-C: [Prom + Thanos sidecar] ──▶ S3 ◀──┘                    │
                                                                    Grafana
```

**Pros:** full fidelity globally; cheapest object-storage-backed option; uploads are async block uploads (no per-sample remote_write firehose).
**Cons:** query latency is bounded by the slowest sidecar; cluster-local Prom is the durability story for the last 2h.

### 5.4 Pattern D — Regional storage + cross-region query (the "read-aside" pattern)

```
   region-A Prom → region-A Mimir   ──┐
   region-B Prom → region-B Mimir   ──┼─▶ global query proxy (Promxy / Mimir tenant-federation)
   region-C Prom → region-C Mimir   ──┘
```

**Pros:** each region is *autonomous* — losing a region doesn't lose visibility into the others; "central pane of glass" via the global query proxy when you need cross-region views; DR story is "lose one region, lose 33% of telemetry but everything else is intact".
**Cons:** highest operational complexity; need to reconcile per-tenant configs across regions; cross-region queries are slower.

### 5.5 The "central pane of glass" vs "regional autonomy" trade-off

| Property | Central (Pattern B) | Regional (Pattern D) |
|---|---|---|
| Single dashboard for the whole fleet | Free | Needs a federating query proxy |
| Loss of central region | Loses *all* visibility | Loses 1 region's visibility |
| Cross-region query latency | Fast (one store) | Slow (cross-region fan-out) |
| Per-region operator autonomy | Limited | Strong |
| `remote_write` egress cost | High | Zero (intra-region only) |
| Suitable for regulated data residency | No (data leaves region) | Yes |

**The decision rule:** if any single region's outage *also taking out telemetry for the other regions* would block your incident response, you need Pattern D. Otherwise B is simpler and cheaper.

### 5.6 Disaster recovery for telemetry

The cardinal rule: **the observability stack must survive the failure of the largest thing it observes.** If your prod region is `us-east-1` and your Mimir lives in `us-east-1`, you cannot debug an `us-east-1` outage with your own dashboards. Production-grade options:

- **Mimir/Loki/Tempo in a different region from prod.** Ideally a different cloud provider or at least a different account.
- **A "platform-of-platform" stack** — a small SaaS observability account (Grafana Cloud, Honeycomb, Datadog) that receives a *copy* of critical signals (SLO burn rates, control-plane logs). When self-hosted goes down, on-call still has paged alerts and the most-important dashboards.
- **Secondary alerting path.** Alertmanager in a separate region; PagerDuty integrations that don't depend on the primary stack's HTTP egress.

See §10 for why this is non-negotiable.

---

## 6. Cardinality and Throughput Budgets

Numbers you should be able to sketch on a whiteboard for a typical 1k-service / 10k-pod org. Cardinality, not throughput, is what kills you — but both matter.

### 6.1 Metrics

```
Per service:
  ~150 unique metric names (Prometheus client + framework defaults)
  ~5 labels per metric, each with ~10 values
  → ~150 × 10^5 / 10  ≈ 15,000 series per service nominal
  In practice: ~3,000–8,000 active series per service

1,000 services × 5,000 active series  =  5,000,000 active series
At 15s scrape interval:               ≈  333,000 samples/sec ingest
At ~1.3 bytes/sample (Gorilla):      ≈  430 KB/sec on disk
                                      ≈  37 GB/day, ~13 TB/year (raw)
After 5m downsampling (1/20):        ≈  0.65 TB/year warm tier
```

Cardinality, not byte volume, drives RAM in the TSDB. Mimir/VM use roughly 3–5 KB resident RAM per active series; 5M series ≈ 15–25 GB RAM in the ingester ring. That's ~3 ingester pods at 8 GB each (with replication factor 3, so 9 pods). Manageable.

**The cardinality bomb:** a single bad label rolls out and adds `customer_id` (1M values) to one metric.

```
Before: http_requests_total{method, status, route}           ≈ 200 series
After:  http_requests_total{method, status, route, customer} ≈ 200,000,000 series
```

That's a 1000× explosion on one metric. The first you'll notice is the ingester OOMing 6 hours later. See `doc 18`.

### 6.2 Logs

```
Per pod:
  ~5–50 lines/sec at info level (HTTP access + business events)
  ~500 bytes/line average (JSON-encoded, with attributes)

10,000 pods × 20 lines/sec × 500 bytes  =  100 MB/sec
                                         =  ~8.6 TB/day
                                         =  ~3.1 PB/year (raw)
After zstd compression (~4–6×):           =  ~520 TB/year
```

Loki indexes only labels; the body is gzipped chunks in S3 at ~$0.023/GB-month.
Elasticsearch indexes everything; the same 8.6 TB/day might land at ~3× the bytes after the inverted index, hot-tier SSD ≈ $0.10/GB-month.

```
Loki cold:     520 TB × $0.023 ≈ $12,000/month storage
ES hot:      1,500 TB × $0.10  ≈ $150,000/month storage
```

That ratio (10–20×) is why Loki exists.

### 6.3 Traces

```
1,000 services @ 100 RPS average                 =  100,000 spans/sec
                                                 (assuming 1 root span/req, ignore inner spans)
Inner spans (DB, cache, downstream RPC): ~10×    =  1,000,000 spans/sec
At ~600 bytes/span (OTLP protobuf):              =  600 MB/sec
                                                 =  52 TB/day (uncompressed!)
After 1% tail sampling:                          =  520 GB/day
After zstd compression in Tempo:                 =  ~100–150 GB/day to S3
```

Trace volume is what teams underestimate. Without tail sampling, a 1k-service mid-size org generates >50 TB/day of spans — far more than logs. **Tail sampling is not optional**; it's a 50–100× cost reduction with a smart policy.

### 6.4 Profiles

```
10,000 pods × 1 sample/min × ~5 KB/profile (compressed pprof, eBPF)
                                          =  50 MB/min
                                          =  ~70 GB/day
                                          =  ~25 TB/year
```

Profiles are the cheapest signal by far. The cost is in symbolization infrastructure (debuginfod, buildID-keyed symbol bundles), not storage.

### 6.5 Putting the budget together

| Signal | Hot retention | Cold retention | Daily volume | Year-1 storage | Cost driver |
|---|---|---|---|---|---|
| Metrics | 15s × 7d | 5m × 13mo | 37 GB/day | ~14 TB | RAM (active series) |
| Logs | 7d | 30–90d | 8.6 TB/day | ~600 TB warm | Object storage + index |
| Traces | 24h | 7–14d | 100 GB/day after sampling | ~30 TB | Compute (sampler RAM) |
| Profiles | 30d | 90d | 70 GB/day | ~25 TB | Symbolizer compute |

**The intuition.** Logs *appear* the largest by raw bytes. Metrics *appear* the smallest. But if your TSDB is full of high-cardinality labels, metrics RAM cost can dwarf log storage cost — easily by 5–10× in pathological cases. Tune cardinality first; tune retention second; tune sampling third.

---

## 7. Storage Tiering

Telemetry value drops fast; cost stays flat. Build a tiered strategy or pay for hot-tier storage on data nobody queries.

### 7.1 The four tiers

| Tier | Resolution | Retention | Backing store | Cost / GB-month | Used for |
|---|---|---|---|---|---|
| **Hot** | Raw (15s metrics, raw logs, full traces) | 6 h – 7 d | NVMe SSD (ingester local) | ~$0.10–0.30 | Live debugging, dashboards, alerts |
| **Warm** | 1 m downsampled metrics, full logs/traces | 7–30 d | SSD-backed object storage / S3 IA | ~$0.013 | Recent capacity reviews, SLO calculations |
| **Cold** | 5 m or 1 h rollups; sampled logs/traces | 90 d – 2 y | S3 standard / GCS standard | ~$0.023 | Year-over-year, audit, compliance |
| **Archive** | Aggregated only | 7+ y | S3 Glacier / GCS Coldline | ~$0.004 | Compliance only |

### 7.2 How downsampling works (Mimir / Thanos)

Mimir's compactor produces three resolutions in object storage:

```
        2h block (raw 15s)              <- written by ingesters
         │
         ▼
        24h merged block (raw 15s)      <- compactor compaction level 1
         │
         ▼
        24h block (1m resolution)       <- compactor downsample step 1
         │
         ▼
        7d block (5m resolution)        <- compactor downsample step 2
```

The querier *picks* the right resolution based on the query's time range:

| Query span | Resolution served | Reason |
|---|---|---|
| 0 – 6 h | raw 15s | precision needed for live drilldown |
| 6 h – 30 d | 1 m | smooth charts; alerts already fired or didn't |
| 30 d – 13 m | 5 m | trend analysis, capacity planning |

Downsampling is **lossy** for non-monotonic signals: histograms must be aggregated as `histogram_quantile` per-bucket sums, not arbitrary aggregates; gauges average; counters use `rate` *before* downsampling. Naïvely averaging a counter is a classic bug — Mimir/Thanos handle this correctly, but if you roll your own, beware.

### 7.3 SSD vs object storage

Rule of thumb: **anything queried by an alert lives on SSD; anything queried by a human dashboard tolerates object storage**. Alerts need <1 s evaluation; humans tolerate 5–10 s panels. Object storage GET latency (50–100 ms first byte, throughput-bound thereafter) is fine for human-facing queries, fatal for alert evaluation.

This is why all the major TSDBs keep the *recent* head block on local SSD and ship compacted blocks to S3.

> **Pitfall:** retention is not a single dial. A common anti-pattern is "we keep everything for 30 days" — which forces hot-tier cost on data nobody queries past day 7. Always tier.

---

## 8. Cost Shape of the Stack

Where the dollars actually go for a $1M/year observability budget at a mid-size (1k-service) self-hosted org. Numbers are illustrative but reflect production reality across multiple teams.

| Bucket | $/year | % | What drives it |
|---|---|---|---|
| Metrics ingest + storage (Mimir cluster) | $90,000 | 9% | Active series count, ingester RAM, S3 storage |
| Log ingest + storage (Loki / ES) | $280,000 | 28% | Bytes/day, indexed fields, hot-tier retention |
| Trace ingest + storage (Tempo) | $180,000 | 18% | Span volume after sampling, S3 PUTs |
| Profile storage (Pyroscope) | $25,000 | 2.5% | Symbolizer compute, S3 |
| Collector fleet (OTel gateway) | $80,000 | 8% | Tail-sampler memory; replication for HA |
| Kafka transport | $60,000 | 6% | Brokers, network, EBS |
| Cross-region network egress | $70,000 | 7% | `remote_write` and trace push across regions |
| SaaS DR layer (Grafana Cloud / Honeycomb) | $50,000 | 5% | Backup metrics + trace + critical logs |
| Grafana, Alertmanager, control plane | $25,000 | 2.5% | Compute + RDS for Grafana DB |
| Engineering on-call for the platform | $140,000 | 14% | 1.5 FTE-equivalent on-call burden + pages |

**What teams underestimate:**
- **Trace storage** ($180k) — most teams budget 2–3% and end up at 15–20% after they stop sampling.
- **Cross-region egress** ($70k) — invisible until the AWS bill arrives.
- **The platform's own on-call cost** — running the observability stack reliably *is* a real on-call rotation (§10).

**What teams overestimate:**
- **Metric scrape cost** — raw scrape volume is cheap; high-cardinality labels are what blow the budget. Most teams fix this with a quarterly cardinality review.
- **Dashboard query cost** — Grafana itself is a small line item; it's the queries it sends downstream that matter.

Charge-back / show-back this to the consuming teams and self-regulation kicks in within a quarter. See `doc 18`.

---

## 9. Failure Modes per Hop

A platform team's runbook starts here. The diagonal: every layer breaks in roughly two flavors — *quiet* (data goes missing) and *loud* (errors visible). Quiet failures are worse, because the dashboard still shows green.

| Hop | What can break | How you detect it | How you survive it |
|---|---|---|---|
| SDK in-process | `BatchSpanProcessor` queue full → spans dropped | App log "span queue full"; `otel_sdk_processor_batch_dropped` counter | Increase queue capacity; tune flush interval; alert on drop counter |
| SDK → agent | Agent unreachable, EPIPE | `up{job="otel-agent"}==0`, k8s pod `CrashLoopBackOff` | DaemonSet with PDB; node-local readiness probe; SDK retries with bounded buffer |
| Agent backpressure | Disk buffer fills, agent starts dropping | `vector_buffer_events_dropped_total` rises | Increase buffer; reduce log verbosity; alert before disk fills |
| Agent → gateway | Network partition, mTLS expiry, slow gateway | Connection error count, gateway p99 spike, `otelcol_processor_dropped_spans` | mTLS rotation automation; gateway HPA on memory; agent retry with exponential backoff |
| Gateway tail-sampler misconfig | Policy drops 100% of error traces (bad regex, wrong attribute name) | Trace error volume in Tempo flatlines while metric error rate is high | Canary new policies; "shadow sample" 1% with old + new policy; require error/latency policies always present |
| Gateway memory exhaustion (tail buffer) | Trace assembly buffer overflows, traces dropped silently | `otelcol_processor_tail_sampling_sampling_trace_dropped_too_early` | Cap assembly window (30s); reject traces with too many spans; HPA on memory |
| Gateway → Kafka | Kafka unavailable | Producer error rate, gateway buffer growing | Gateway disk buffer; Kafka client `acks=all`; cross-AZ Kafka |
| Kafka cluster | Broker dies, ISR shrinks | `under_replicated_partitions > 0`, consumer lag | Replication factor ≥ 3; rack awareness; alert on ISR shrink |
| Mimir distributor | Hot tenant DOSes the write path | Per-tenant `cortex_distributor_received_samples_total` rate | Per-tenant rate limits enforced by distributor; cardinality cap |
| Mimir ingester | Memory exhaustion from cardinality bomb | Ingester pod OOMKilled; `cortex_ingester_memory_series` near limit | Cardinality cap per tenant; `max_global_series_per_user`; pre-flight cardinality in CI |
| Loki / Tempo S3 throttling | S3 returns 503 SlowDown | Ingester retry rate, S3 4xx/5xx | Multi-bucket sharding; exponential backoff; warn before hitting AWS account limits |
| Query layer hot tenant | One team's `topk(1000, ...)` melts the queriers | `cortex_query_frontend_queue_length`, per-tenant inflight | Per-tenant query queue; max-samples-per-query; query frontend split-by-time |
| Cross-region partition | A region cannot reach the central Mimir | `remote_write` queue depth, regional Prom WAL near full | Regional autonomy (Pattern D §5); per-region storage; out-of-band alert path |
| Alertmanager outage | Alerts fire but nobody is paged | "watchdog" alert (always-firing canary) goes silent | Run two AM clusters in active-active; PagerDuty as the canary monitor |
| Scrape timeout (Prometheus) | Target is slow to expose `/metrics`; partial samples | `scrape_duration_seconds` near `scrape_timeout`; `up==0` flapping | Increase scrape timeout per job; reduce target's metric count |
| Agent crash with un-flushed buffer | Last 30s of logs/spans gone | Agent restart count; gap in ingest | Persistent disk buffer (Vector / Fluent Bit `storage.type=filesystem`) |
| Schema drift | New field in log breaks downstream parser | Parser error rate at the gateway | Schema-as-code; OTel semantic conventions; CI lint on log fields |

> **Diagnostic patterns:**
>
> 1. **"The dashboard is green but customers are complaining."** Almost always a *quiet* failure. Check the watchdog alerts, check the ingest-lag SLI, check `up{}` and exporter heartbeats before believing the dashboard.
> 2. **"Tail sampler dropped all the errors."** Look for a recent collector config push. The sampling policy probably has a typo in an attribute name (`status_code` vs `http.status_code`). Run with the dual-policy shadow sampler in staging before pushing.
> 3. **"Ingester is OOMing every 6 hours."** Cardinality bomb. Find the new label by diffing `prometheus_tsdb_head_series` against the prior week, grouped by metric name.

---

## 10. The "Platform Owns the Platform" Principle

The observability stack itself is a production system. It has SLOs. It has on-call. It has runbooks. Forgetting this is the most expensive mistake in §11's pitfall list.

### 10.1 Meta-observability — observe the observer

```
   ┌────────────────────────┐         ┌──────────────────────────┐
   │   Primary stack        │         │   Secondary stack         │
   │   (self-hosted)        │ ──────▶ │   (SaaS or other region)  │
   │   - Mimir, Loki, Tempo │         │   - Grafana Cloud, or     │
   │   - OTel collectors    │         │   - Datadog, Honeycomb    │
   │   - Alertmanager       │         │                            │
   └─────────┬──────────────┘         └────────────┬───────────────┘
             │                                     │
             │ self-monitor (own metrics,         │ receives copy of
             │ own logs, own traces)              │ critical signals only:
             │                                     │ - SLO burn rates
             │                                     │ - Alertmanager up/down
             │                                     │ - ingest-lag SLI
             │                                     │ - on-call paging health
             ▼                                     ▼
   ┌────────────────────────────────────────────────────────────────┐
   │   Out-of-band paging: PagerDuty (with multi-source integrations)│
   │   When primary stack is down, secondary still pages on-call.    │
   └────────────────────────────────────────────────────────────────┘
```

**The two non-negotiables:**

1. **The observability platform must monitor itself.** Mimir scrapes Mimir. Loki ships its own logs to Loki. Tempo traces its own gRPC calls. This is the first stack you implement, and it must have its own SLOs (ingest lag p99 < 30s, query availability > 99.9%).
2. **There must be a path that survives the primary stack's outage.** Either (a) a secondary region/account hosts a duplicate of the most-critical signals, or (b) a SaaS receives a copy. The cost is small (<$5k/month for most orgs); the alternative is "the dashboards went dark right when we needed them most".

### 10.2 The platform team's SLOs (examples)

| SLI | SLO target | Window | Source signal |
|---|---|---|---|
| Ingest lag (sample → queryable) p95 | < 30 s | 28 d | synthetic ingest probe |
| Query availability (200 OK / total) | 99.9% | 28 d | query frontend HTTP |
| Alertmanager delivery latency p99 | < 60 s | 28 d | watchdog alert round-trip |
| Tail sampler drop rate (errors only) | < 0.01% | 28 d | sampler counter / actual error counter |
| Cardinality budget compliance per tenant | 100% within budget | rolling | per-tenant active-series gauge |

### 10.3 On-call for the platform team

- **Distinct rotation** from product on-call. The skills are different.
- **Runbooks for every alert.** The platform's alerts page humans; if the runbook is missing, the alert is broken.
- **Postmortem on every incident** — including incidents that affected only the platform's own monitoring (not customer traffic). These are the ones that catch the next P0.

> **Mental model:** if no one's pager goes off when Mimir is down, Mimir isn't being run as production. That's the test.

---

## 11. Reference Architectures (three sizes)

Three ways the same boxes get arranged at three scales. The graduation criteria between tiers are concrete.

### 11.1 Small — <50 services, <500 pods, single region

```
   ┌──────────────────────────────────────────────────────────────┐
   │                  Workloads (k8s, single cluster)             │
   │   - OTel SDK in apps                                         │
   │   - structured logs to stdout                                │
   └──────────────────────────────┬───────────────────────────────┘
                                  │
                                  ▼  (DaemonSet)
   ┌──────────────────────────────────────────────────────────────┐
   │   Grafana Alloy (single-tier: agent does it all)             │
   │   - tail logs, scrape /metrics, receive OTLP                 │
   │   - ship directly to SaaS                                    │
   └──────────────────────────────┬───────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │   Grafana Cloud (or Datadog, or Honeycomb)                   │
   │   metrics + logs + traces + dashboards + alerting            │
   └──────────────────────────────────────────────────────────────┘
```

- **No gateway.** Single-tier agent → SaaS.
- **No Kafka.** SaaS handles transport.
- **No object storage.** SaaS owns it.
- **No tail sampling.** Either head-sample at the SDK or accept the SaaS bill.
- **Cost:** $1k–$15k/month at this size.
- **Engineering cost:** ~0.2 FTE.

**Graduate when:** monthly SaaS bill > 0.5 FTE-month of engineering ($8k–$15k/month), OR you have >2 clusters, OR you need data-residency control.

### 11.2 Mid — 500–5000 services, multi-cluster, single or two regions

```
   ┌──────────────────────────────────────────────────────────────┐
   │           Workloads (k8s, 5–20 clusters, 1–2 regions)        │
   └──────────────────────────────┬───────────────────────────────┘
                                  │
                                  ▼  (DaemonSet, per node)
   ┌──────────────────────────────────────────────────────────────┐
   │   OTel Collector / Alloy (agent mode)                        │
   └──────────────────────────────┬───────────────────────────────┘
                                  │
                                  ▼  (Deployment, 5–20 replicas)
   ┌──────────────────────────────────────────────────────────────┐
   │   OTel Collector (gateway mode)                              │
   │   - tail-sample 1–5%                                         │
   │   - per-tenant rate limit                                    │
   │   - fan out to 3 stores                                      │
   └─┬─────────────────────────┬──────────────────────────┬───────┘
     │                         │                          │
     ▼                         ▼                          ▼
   ┌──────────┐           ┌──────────┐               ┌──────────┐
   │ Mimir    │           │ Loki     │               │ Tempo    │
   │ (S3)     │           │ (S3)     │               │ (S3)     │
   │ + Prom   │           │          │               │          │
   │ scrapers │           │          │               │          │
   └────┬─────┘           └────┬─────┘               └────┬─────┘
        └────────────┬─────────┴──────────────────────────┘
                     ▼
             ┌────────────┐  ┌────────────────┐
             │  Grafana   │  │ Alertmanager   │
             │            │  │ → PagerDuty    │
             └────────────┘  └────────────────┘
```

- **Two-tier: agent + gateway.** Tail sampling at the gateway.
- **Kafka optional.** Add it when storage outages start dropping data.
- **Self-hosted Mimir + Loki + Tempo.** S3 for cold tier.
- **Pyroscope** added in phase 14 (`doc 09`).
- **Cost:** $100k–$500k/year (storage + compute + ~1.5 FTE).
- **Engineering cost:** 1–3 FTE on a platform team.

**Graduate when:** >10k services or >2 regions or regulated data residency forces per-region storage, OR ingest exceeds ~10 GB/s aggregate.

### 11.3 Hyperscale — 10k+ services, multi-region, multi-tenant

```
   ┌──────────────────────────────────────────────────────────────┐
   │                Workloads (multi-region, multi-cluster)        │
   └──────────────────────────────┬───────────────────────────────┘
                                  │
                                  ▼  (per-node agent)
   ┌──────────────────────────────────────────────────────────────┐
   │   OTel Collector agent + custom enrichment                   │
   └──────────────────────────────┬───────────────────────────────┘
                                  │
                                  ▼  (per-region gateway, 50–500 replicas)
   ┌──────────────────────────────────────────────────────────────┐
   │   Regional gateway cluster (tail-sample, per-tenant limits)  │
   └──────────────────────────────┬───────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │   Kafka per region (durable buffer, 50–500 brokers)          │
   └──────┬─────────────────────────────────────────────┬─────────┘
          │ hot path                                    │ cold path
          ▼                                             ▼
   ┌──────────────────────┐               ┌──────────────────────────┐
   │ Regional hot storage │               │ Lakehouse cold path       │
   │ - Mimir (per region) │               │ - ClickHouse / BigQuery   │
   │ - Loki  (per region) │               │   on logs, spans, metric  │
   │ - Tempo (per region) │               │   exemplars               │
   └──────────┬───────────┘               │ - Iceberg + S3            │
              │                           └──────────────────────────┘
              ▼                                    ▲
   ┌──────────────────────────────────────────────┴───────────────┐
   │   Global query layer (Promxy / Mimir tenant federation /     │
   │   custom Trino-on-lakehouse)                                 │
   └──────────────────────────────┬───────────────────────────────┘
                                  ▼
                     Grafana, custom UIs, Alertmanager (per region, active-active)
```

- **Three-tier: per-node agent → per-region gateway → global aggregator/query.**
- **Kafka in the path** is mandatory.
- **Per-region hot storage (Pattern D §5).**
- **Lakehouse cold path** for SQL-on-telemetry. Logs and spans land in ClickHouse/BigQuery for ad-hoc analytics.
- **Custom code is real.** At this scale you have engineers writing OTel processors, Mimir patches, custom query proxies.
- **Cost:** $5M–$50M/year.
- **Engineering cost:** 5–30 FTE platform team.

**The graduation rule:** don't graduate until you have to. Each tier doubles operational complexity. Most orgs that *think* they need hyperscale are actually solving the wrong problem at the mid-tier.

---

## 12. Decision Log: Questions a Staff Engineer Must Answer Before YAML

These are the architectural decisions you should be able to answer in writing before any Helm chart is committed. Each has a real trade-off; getting them wrong is recoverable but expensive.

1. **OTel-first or Prometheus-first?**
   *Trade-off:* OTel is the future and unifies all four signal types; Prometheus is mature and has the deepest tooling for metrics. Most stacks land on "OTel SDKs for new code, Prom client for existing, OTel Collector as the gateway, Prometheus exposition for `/metrics`." Pure-OTel only when you're greenfield.

2. **Single OTel collector tenancy or per-team gateways?**
   *Trade-off:* Single = cheaper, one config to debug. Per-team = noisy-neighbor safety, faster team-level iteration. Most orgs start single and split when one team's tail-sample bursts melt everyone's traces.

3. **Kafka in the path, or direct gateway → store?**
   *Trade-off:* Kafka = durable across storage outages, multi-consumer, replay. Direct = simpler, ~$60k/year cheaper at mid scale. Add Kafka the day you have your second ingest backend, or after the first storage outage drops data.

4. **What is our cardinality budget per service / per tenant?**
   *Trade-off:* Tight budgets force discipline; loose budgets cost RAM. A reasonable starting point: 10k active series per service, 1M per team, 50M per tenant. Enforce in the gateway (drop labels) AND in the TSDB (reject series).

5. **Trace tail-sampling or head-sampling?**
   *Trade-off:* Tail = better data, requires gateway memory and 30s assembly window. Head = simpler, misses rare errors. Production answer: tail at the gateway with a head-sample fallback at the SDK for emergency rate limiting.

6. **Push (OTLP) or pull (Prom) for `/metrics`?**
   *Trade-off:* Pull = free liveness, deterministic relabeling. Push = works behind NAT, captures short-lived jobs. Hybrid: pull for steady state, push for batch and Lambdas.

7. **Self-hosted or SaaS for each signal?**
   *Trade-off:* SaaS = fast time to value, no on-call for the platform itself, expensive at scale ($50k–$5M/year). Self-hosted = 5–10× cheaper at scale, requires a dedicated platform team. Mix is common: SaaS for traces + RUM, self-hosted for metrics + logs.

8. **One region for the observability stack, or per-region?**
   *Trade-off:* One = simpler, single pane of glass. Per-region = survives regional outages, regulated-data compliance. Pick per-region if regional outage of *prod* must not also kill *visibility into prod*.

9. **Loki or Elasticsearch for logs?**
   *Trade-off:* Loki = 5–10× cheaper, narrower queries (label-indexed). Elasticsearch = full-text, expensive. Loki for high-volume application logs; Elasticsearch for low-volume security/audit if full-text is required.

10. **How long do we keep each signal at hot, warm, cold tiers?**
    *Trade-off:* Longer hot = better debugging, more $$. Pick by use case: hot 7d for live debugging, warm 30d for postmortems, cold 13mo for capacity planning, archive 7y for compliance only.

11. **Where do SLOs live — in code (Sloth/Pyrra) or in vendor UIs?**
    *Trade-off:* Code = reviewable, version-controlled, portable. UI = faster to author. Code wins for any SLO that drives an actual page.

12. **Who owns the platform's on-call?**
    *Trade-off:* "Platform team" = clear ownership, requires staffing. "Whoever is on-call this week" = always shifting, no expertise. Always pick a dedicated team.

13. **Do we run a secondary, out-of-band path for the most critical alerts?**
    *Trade-off:* Cost (~$5k/month) vs. blindness during the primary stack's outage. The primary stack will fail at some point. If "fail blind" is unacceptable, run a secondary.

14. **Per-tenant quotas: enforced at gateway, at TSDB, or both?**
    *Trade-off:* Gateway = early enforcement, drops before storage. TSDB = last line of defense. Enforce at *both*; gateway prevents the storm, TSDB protects the cluster.

15. **Profiling — eBPF (system-wide) or in-process (per-language SDK)?**
    *Trade-off:* eBPF = zero code change, language-agnostic, requires kernel ≥ 5.4 and frame pointers / DWARF. In-process = per-language, more accurate (no symbolization gap), requires deploy. Most orgs do eBPF for breadth + in-process for the top 10 services.

16. **Is the `trace_id` in every log line a hard requirement?**
    *Trade-off:* Yes (almost always). It's a one-line middleware change and the single highest-leverage correlation field in the stack. If your log library doesn't emit it, fix the library.

---

## 13. What This Chapter Does Not Cover

By design, this chapter is the wiring diagram. The depth lives elsewhere:

- **OpenTelemetry SDK and Collector internals, OTLP wire format, semantic conventions** → `doc 02`.
- **Per-language instrumentation idioms, structured logging, span lifecycle, context propagation** → `doc 03`.
- **Agent and gateway internals, tail sampling policies, edge processing recipes** → `doc 04`.
- **Kafka / Pub/Sub patterns specific to telemetry transport, replay, multi-consumer fan-out** → `doc 05`.
- **TSDB internals: WAL, head block, Gorilla compression, postings index, Mimir vs VM vs Thanos** → `doc 06`.
- **Log store internals: inverted index vs label-index vs columnar, retention tiers, compaction** → `doc 07`.
- **Trace store internals: index-less designs, spanmetrics, service graph derivation** → `doc 08`.
- **Profiling pipelines: pprof, eBPF, symbolization, continuous profiling** → `doc 09`.
- **Query languages: PromQL, LogQL, TraceQL, SQL-on-telemetry recipes** → `doc 10`.
- **Dashboards: Grafana patterns, RED/USE templates, exemplars, mobile vs ops layouts** → `doc 11`.
- **Alerting: Alertmanager, multi-window multi-burn-rate, page hygiene** → `doc 12`.
- **SLO engineering: SLI math, error budgets, the four golden burn-rate windows** → `doc 13`.
- **On-call, runbooks, incident response, postmortems** → `doc 14`, `doc 15`.
- **Capacity planning, PRR, cardinality and cost** → `doc 16`–`doc 18`.
- **Multi-tenancy, RBAC, isolation** → `doc 19`.
- **AIOps and frontier topics** → `doc 20`.

If a question in this chapter felt unanswered (e.g., "but what *exactly* does tail sampling do?"), the answer is in the doc cross-referenced at the point the question came up.

---

> **TL;DR**
>
> An observability stack is **emit → ship → store → query → alert**, with four mostly-independent signal pipelines (metrics, logs, traces, profiles) and two physical tiers (per-node agent, gateway cluster) that scale on different axes. Pull for steady-state metrics, push for everything else. The agent + gateway split exists because tail sampling, vendor abstraction, and per-tenant rate limits all need a centralized point — but that point is not the storage backend. Cardinality kills metrics, sampling saves traces, retention tiering saves logs, and cross-region architecture decides whether your dashboards survive losing a region. The platform itself is a production system: it has SLOs, on-call, and runbooks, and it observes itself through a secondary path that survives its own outage. Every architectural decision in §12 should have a written answer before any Helm chart is merged. The rest of this series zooms into one box at a time; this chapter is the map you use to navigate them.
