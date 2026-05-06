# 18 — Cardinality and Cost: The Hardest Single Problem

> The single hardest problem in observability at scale is not "how do I store more data" but "how do I store *less* data without losing the answers I need." Every TSDB death, every Datadog bill panic, every "why does Loki cost more than the service it observes" question reduces to cardinality + retention + sampling. This chapter is the staff-engineer-grade treatment.

This chapter assumes vocabulary from `doc 00` (cardinality, sampling, retention), the storage internals from `doc 06`/`07`/`08`, and the collector practices from `doc 04`. It pulls them together into the cost-aware story.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [The cost equation, decomposed](#2-cost-equation)
3. [Cardinality math you should know cold](#3-cardinality-math)
4. [Where cardinality enters](#4-where-cardinality-enters)
5. [The cardinality budget](#5-budget)
6. [Detection: how to see cardinality before it kills you](#6-detection)
7. [Defenses, in order of preference](#7-defenses)
8. [Logs: cost dynamics differ from metrics](#8-logs-cost)
9. [Traces: cost dynamics again differ](#9-traces-cost)
10. [Profiles: surprisingly cheap](#10-profiles-cost)
11. [Retention tiers and downsampling](#11-retention)
12. [Showback and chargeback architecture](#12-showback)
13. [The quarterly hygiene cycle](#13-hygiene)
14. [The "is this label worth it?" rubric](#14-rubric)
15. [Anti-patterns](#15-anti-patterns)
16. [Worked example: a 10× cost reduction](#16-worked-example)
17. [Pitfalls](#17-pitfalls)
18. [Mental models](#18-mental-models)

---

## 1. Thesis

Three claims:

1. **Cost grows super-linearly with naive instrumentation.** Add a `customer_id` label, double traffic, add 3 new services — cost doesn't go 12× (the linear ratio); it goes 50–500×. Cardinality compounds.
2. **The lever is cardinality, not storage.** Storage is cheap (S3 is pennies per GB); *index memory* is expensive. A TSDB doesn't die from data volume; it dies from active-series count. Optimize for the index.
3. **Showback creates self-regulation.** Once a team sees "your service costs $14k/mo to observe," they cut their own cardinality. Without showback, cardinality grows monotonically.

If your observability bill is growing faster than your traffic and you don't have a per-team breakdown, your cost problem is structural, not technical. This chapter is the structural fix.

---

## 2. The Cost Equation, Decomposed

A typical observability cost stack:

```
Cost = (compute for ingest)
     + (memory for indexes / hot data)
     + (storage for warm + cold data)
     + (network egress)
     + (per-vendor licensing)
     + (engineering time to operate)
```

Each component has a different scaling driver:

| Component | Scales with |
|---|---|
| Ingest compute | Sample / event rate |
| Index memory | **Active series cardinality** (metrics) / token count (logs) |
| Hot storage | Sample rate × retention |
| Warm/cold storage | (Sample rate × retention × compression ratio) |
| Network egress | Cross-region traffic |
| Licensing | Vendor formula (per host? per GB? per series?) |
| Engineering time | Operational complexity |

### 2.1 The dominant term

For metrics: **index memory.** A Prometheus / Mimir / VictoriaMetrics ingester uses ~3 KB RAM per active series. 10M series = 30 GB just for the index, before any actual sample storage.

For logs: **storage volume + index size.** Elasticsearch indexes every term; that's a 5–10× multiplier on raw bytes. Loki indexes only labels, dramatically cheaper.

For traces: **storage volume + bandwidth.** Sample rate × span size × retention.

For profiles: **storage volume.** Smaller than expected because of dictionary compression of stack traces.

### 2.2 The cost-attribution chain

```
Service emits signal
    ↓
Agent buffers (cheap)
    ↓
Collector batches (cheap)
    ↓
Ingest writes WAL + index (compute + memory cost)  ← biggest variable cost
    ↓
Compaction merges blocks (compute, periodic)
    ↓
Storage (cheap per GB; compounds with retention)
    ↓
Query reads + aggregates (compute, depends on query patterns)
```

The biggest *variable* cost is the index. The biggest *fixed-per-volume* cost is storage. Different optimizations for each.

---

## 3. Cardinality Math You Should Know Cold

### 3.1 The product rule

For a metric with N labels, each with `kᵢ` distinct values, max cardinality is the *product*:

```
cardinality = k₁ × k₂ × … × k_N
```

Reality is usually *much less* than the product (most label combinations don't exist), but the worst case is the product. Plan for it.

### 3.2 The "added label" multiplier

Adding one new label with `k` values multiplies cardinality by ≤ k.

```
http_requests_total{method, status, route}              = 10,000 series
http_requests_total{method, status, route, customer_id} = 10,000 × 1M = 10B series
```

A million-customer label in a metric = death. **Test this in CI before merging the change.**

### 3.3 The growth rate

Often more important than current cardinality is *cardinality velocity*. A service with growing user count has growing label cardinality. Track it weekly.

```
Today:    10K series
+1 week:  12K (20% growth)
+1 month: ~25K (compound)
+1 year:  Potentially millions, if growth continues.
```

A 20% week-over-week cardinality growth means cardinality 4×s every 2 months. Linear extrapolation underestimates.

### 3.4 The active-series cost

For Prometheus / Mimir:

```
RAM per series ≈ 3 KB (varies; mostly index overhead)
Storage per series per day ≈ ~100 KB (raw), ~10 KB (compacted)

10M active series:
  RAM:  30 GB
  Storage:  1 TB raw / day, 100 GB compacted / day, ~3 TB / month after compaction

For Mimir with default 13-month retention: ~40 TB
At S3 pricing ($0.023/GB/mo): ~$900/month for the storage alone.
At ingester memory cost (~$0.10/GB-mo for memory-backed instances): ~$3/month per ingester for 30 GB.
```

The storage cost compounds with retention. The memory cost is paid every second the index is hot.

### 3.5 The query-side cost

Cardinality doesn't only affect ingest. Queries that scan all series (`topk(10, sum(rate(...)) by (customer_id))`) must traverse every series. At 10M series, this is a query that takes seconds even on warm caches.

The query cost grows linearly with active series visited, not with raw data volume. Cardinality is the *worst* dimension to be expensive on.

---

## 4. Where Cardinality Enters

The five entry points. Audit each in your stack.

### 4.1 Application labels

```python
# anti-pattern
counter.labels(method=request.method, status=resp.status, route=request.route, user_id=request.user_id).inc()
                                                                              ↑↑↑ cardinality death
```

Engineers add labels because "it would be nice to filter by user_id sometimes." The cost is invisible until cardinality hits the ingester memory wall.

### 4.2 Auto-instrumentation labels

Frameworks (Spring, Express, Django middleware) often add labels you didn't ask for: full URL paths (with query params, IDs), client IP, request ID. Default behaviors generate millions of series.

Audit the framework defaults. Override aggressively.

### 4.3 Kubernetes label enrichment

The OTel collector and Promtail enrich with k8s metadata: pod, namespace, container, node. This is usually fine — pod count is bounded by replica count. But:

- `pod` cardinality grows with deploys (each deploy = N new pods); old metrics linger.
- Job names from short-lived pods cause permanent cardinality.

Cleanup: drop `pod` from metrics that aren't pod-specific.

### 4.4 Status / error codes

`status="500"` is ~10 distinct values; fine. But `status_message="Database connection refused: connection reset by peer (connection 12345)"` includes a connection ID. Each unique message = a new series. 

Always *categorize* status messages, never store raw.

### 4.5 Dynamic dimensions

Anything with high natural cardinality:
- `customer_id` (millions)
- `request_id` (every request, infinite)
- `email`, `phone`, `username` (privacy and cardinality)
- `ip_address` (millions)
- `session_id` (per-session)
- `transaction_id` (per-tx)
- `path` if URL templating fails (e.g., `/users/12345/profile` instead of `/users/{id}/profile`)

These belong in *logs* and *traces*, not metric labels. Repeat: logs and traces.

---

## 5. The Cardinality Budget

The structural defense.

### 5.1 What it is

A per-service, per-metric, or per-tenant *limit* on active series, enforced at the ingester.

```
checkout-svc:
  metrics_max_active_series: 50,000
  logs_max_GB_per_day:       100
  traces_max_spans_per_sec:  10,000
```

### 5.2 Where it's enforced

Multiple places in the chain:

1. **In the SDK / agent.** Drop labels above a threshold (e.g., OTel metric views to limit cardinality per metric).
2. **At the collector.** `transform` or `attributes` processors to drop or hash high-cardinality fields.
3. **At the ingester.** Reject writes that exceed the per-tenant limit (Mimir, Cortex, VictoriaMetrics support this).
4. **At the gateway.** Per-tenant rate limit on series creation.

The most reliable enforcement is at the ingester — *before* the data is committed but *after* the data has traveled the network. Earlier enforcement (SDK, agent) is cheaper but harder to coordinate.

### 5.3 The budget conversation

When a team's cardinality grows past their budget:

1. Alerts fire (the platform team's "cardinality limit approaching" alert).
2. The platform team meets with the service team.
3. Options: cut cardinality, buy more budget (with cost approval), or move data to a different store (logs / traces).

The conversation is *gated by a hard limit*. Without the limit, the team has no incentive to cut. With it, they own the trade-off.

### 5.4 The CI cardinality check

For the brave: run a cardinality estimator in CI on every metric-touching PR.

```
PR adds: counter.labels(method, status, route, customer_tier).inc()
Estimator: would add ~50 new series per minute at current traffic
Acceptable: yes
```

```
PR adds: counter.labels(method, status, route, customer_id).inc()
Estimator: would add ~10M series at current customer count
Acceptable: NO. PR rejected.
```

This catches cardinality bombs before they ship. Tools: custom static analysis, OpenObserve cardinality lints, or query-vs-historical-trends bots.

---

## 6. Detection

How to see cardinality before it kills the cluster.

### 6.1 Per-metric cardinality

```
prometheus_tsdb_head_series                  total active series
sum by (job) (count_over_time({...}[1m]))    series per ingester
```

Mimir, Cortex, VictoriaMetrics expose per-tenant series counts:

```
cortex_ingester_memory_series                active series in ingester memory
cortex_ingester_memory_series_created_total  rate of new series creation
```

**Series creation rate** is the leading indicator. A spike in series creation = a new high-cardinality label rolled out. Alert on it.

### 6.2 Top-N high-cardinality metrics

Periodic query:

```promql
topk(20, count by (__name__) ({__name__=~".+"}))
```

The top metrics by cardinality. Often surprises:
- A single metric using 30% of the cluster's series.
- A metric that should have ~100 series has 50,000.
- An old metric still being written but never queried.

### 6.3 Top-N high-cardinality labels per metric

```promql
topk(20, count by (label_name) (label_replace({metric=~"http_request.*"}, ...)))
```

Identifies which label is the culprit. Often: `route`, `customer_id`, `path`.

### 6.4 The "killed" series

Some TSDBs report when they reject writes due to cardinality limits:

```
cortex_distributor_ingester_append_failures_total{reason="per_user_series_limit"}
```

If this is non-zero, your tenant is *already* exceeding budget. Alerts on this fire, telling teams to clean up.

### 6.5 The "abandoned" series

Series that exist (in the index) but receive no writes (the metric stopped emitting). They consume index memory until next compaction.

```
Active series with no recent samples / total active series
```

> 5% = a sign of label churn. Investigate which metric is generating one-shot series.

---

## 7. Defenses, in Order of Preference

The hierarchy. Pick the highest-leverage defense first.

### 7.1 Don't add the label

The cheapest defense. Before adding a label, ask: *will I aggregate by it?* If not, it doesn't belong in a metric.

### 7.2 Move the dimension to logs / traces

`customer_id` doesn't need to be in a metric label. It can be in:
- Log lines (one per request).
- Trace spans (one per span).
- Exemplars (one trace_id per histogram bucket).

Logs and traces have their own cost dynamics (§8, §9), but they don't suffer the active-series-index explosion.

### 7.3 Aggregate at the source

If you really need the dimension, *pre-aggregate*:

```
# bad: per-customer metric
http_requests_total{customer_id="...", status="500"}

# good: per-customer-tier metric
http_requests_total{customer_tier="enterprise", status="500"}
```

`customer_tier` has 5 values; `customer_id` has millions. The tier is what you'd actually aggregate by anyway.

### 7.4 Hash and bucket

For dimensions where you want *some* attribution but not full granularity:

```
hash(customer_id) % 100 → bucket
```

Now you have 100 buckets, not millions. You can find which bucket has anomalies, then drill via logs/traces.

### 7.5 Top-K via sketch

For "top-N customers by errors":

- Server-side aggregation: use a Top-K sketch (Misra-Gries) to keep the top-100 customers by error rate, dropping the long tail.
- Output: a small set of metrics for the top customers + an "other" bucket.

This is what Datadog's "top-k metric" feature is, internally.

### 7.6 Per-tenant cardinality limits

Hard limit at the ingester. Forces the trade-off conversation when the limit is reached. Mimir, Cortex, VictoriaMetrics, Thanos all support this.

### 7.7 Reservoir sampling for exemplars

Want to keep some specific examples (one trace_id per histogram bucket) but bounded? Reservoir sampling gives you N exemplars per bucket, statistically fair across the input.

---

## 8. Logs: Cost Dynamics Differ

Logs are about *volume* and *index*, not cardinality.

### 8.1 The two log architectures

**Index-everything** (Elasticsearch, OpenSearch, Splunk):
- Every term in every log line is indexed.
- Storage cost: 5–10× raw size (index overhead).
- Query cost: O(matching docs). Fast lookups.
- Infrastructure: Lucene-based index, memory-hungry.

**Index-the-labels** (Loki, ClickHouse):
- Only the metadata (labels) is indexed.
- Log content is brute-force searched.
- Storage cost: ~1× raw, very compressible (zstd/snappy on column store).
- Query cost: O(log volume in window). Slower for ad-hoc; great for label-filtered queries.
- Infrastructure: object-store-backed; fits the cheap-storage model.

The 10× cost difference is real. For most production logs (where you query by `service`, `pod`, `namespace`, occasionally `trace_id`), Loki / ClickHouse-on-logs is dramatically cheaper.

### 8.2 The log-cardinality trap

Loki labels are *like* Prometheus labels. Same cardinality rules apply. A label with millions of values is just as deadly.

```yaml
# bad
labels:
  customer_id: "..."  # million-cardinality label

# good
labels:
  service: "checkout"
  level: "error"
```

The actual log content is *content*, not labels. Use `LogQL`'s text filtering for content; labels are for routing / partitioning.

### 8.3 Tiered log retention

Most logs are useless after 7 days. A few classes (audit, security, billing) need years. **Two pipelines, not one giant retention.**

```
Default:        7 days   in hot store (Loki)
Audit:          7 years  in cold archive (S3 + Athena)
Security:       1 year   warm (ClickHouse)
Application:    7 days   hot
Compliance:     7 years  immutable cold (S3 with Object Lock)
```

Misconfiguring this once costs ~$1M / year at scale.

### 8.4 Log redaction at the source

PII redaction (emails, credit cards, SSN) at the *agent*, not the store. Reasons:
- Removes liability before data leaves the box.
- Reduces transmitted volume (smaller logs).
- Avoids "we found PII in our log store" incidents.

Tools: Fluent Bit `record_modifier` filters, Vector VRL, OTel attribute processor with regex.

### 8.5 Log sampling

Underused in most stacks. Sample debug logs aggressively; keep ERROR / WARN at 100%.

```
Conditional:
  if level == "error":   keep 100%
  if level == "warn":    keep 100%
  if level == "info":    keep 10%
  if level == "debug":   keep 1% (and only if request was traced)
```

For high-traffic services this can be a 10× cost reduction.

---

## 9. Traces: Cost Dynamics Again Differ

Traces are about *sample rate* and *span size*.

### 9.1 The tail-sampling lever

Head sampling at 100% = trace bill 10× too high. Tail sampling at the gateway:

```
Keep 100% of:  errors, slow (p99), specific service paths
Keep 5%   of:  baseline successful requests
Keep 0%   of:  health checks, internal probes
```

Net result: ~5–10× cost reduction with no loss of debugging quality.

### 9.2 The span-attribute size

Each span carries attributes (`db.statement`, `http.url`, `peer.service`). Limit:

- Drop verbose attributes (full SQL queries) at the gateway.
- Truncate long string values (>1KB) at the SDK.
- Drop attributes with very low query value (e.g., `http.user_agent` if you never query by it).

A span that's 2 KB vs 200 bytes = 10× cost difference. Easy win.

### 9.3 Trace storage architecture

| Architecture | Cost dynamics |
|---|---|
| **Tempo** (object-store) | Cheap storage; key by trace_id; service-graph derived from spanmetrics |
| **Jaeger / ES** | Index-everything; expensive but flexible |
| **ClickHouse** | Columnar; rich SQL; per-attribute querying; medium cost |

Tempo is dramatically cheaper at scale, with the trade-off that you can't ad-hoc query span attributes — you can only fetch by trace_id or service-graph metric. For most teams, that's fine; the slow paths are: search → dashboard → trace_id → fetch.

### 9.4 Trace retention

Traces drop in value fast. After 7 days, ~1% of traces are revisited. Tier:

```
Hot:        7 days   raw spans   debugging
Warm:       30 days  sampled     SLO compliance
Cold:       1 year   aggregated  capacity / trends
```

---

## 10. Profiles: Surprisingly Cheap

Continuous profiling has the best cost-to-value ratio of the four signals.

### 10.1 Why it's cheap

- Profiles dictionary-compress stack traces (function names appear once, referenced by ID).
- Sampling rate is low (e.g., 99 Hz for 10s every 30s = 30 samples/min/process).
- Storage per profile: ~50 KB compressed.

A typical fleet of 1000 processes profiling continuously: ~50 MB/min, ~70 GB/day, ~25 TB/year. At S3 pricing: ~$50/month.

### 10.2 The catch: symbolization

Stripped binaries require external symbol tables. The symbol DB grows, but slowly (one entry per build). Manageable.

### 10.3 The recommendation

Enable continuous profiling on every service unless there's a specific reason not to. The cost is negligible relative to the debugging value (`doc 09`).

---

## 11. Retention Tiers and Downsampling

The structural lever for time-series cost.

### 11.1 The tier ladder

| Tier | Resolution | Retention | Cost driver | Used for |
|---|---|---|---|---|
| Hot | Raw (15s) | 7–15 days | Memory + SSD | Live debugging, dashboards, alerts |
| Warm | 1m down-sampled | 30–90 days | SSD | SLO calculations, recent capacity |
| Cold | 5m / 1h | 1–2 years | Object storage | Year-over-year, audit |
| Archive | Aggregated | 7+ years | Glacier / cold | Compliance |

Each tier 5–10× cheaper per data point than the one above. Without tiering, you pay hot prices for cold data.

### 11.2 Downsampling

When metrics are downsampled, *some* fidelity is lost — but you can ask different questions efficiently.

```
Raw 15s data: can answer "what was p99 at 14:32:15?"
1m down: can answer "what was p99 at 14:32?"
5m down: can answer "what was p99 at 14:30?"
1h down: can answer "what was p99 at 14:00?"
```

For year-over-year queries, 1h resolution is fine. For incident investigation, 15s is essential. Tier accordingly.

### 11.3 Native histogram storage savings

Native histograms (Prometheus 2.40+, in 2026 widely adopted) store sparse buckets and use ~10× less storage than classic histograms. For services with many histograms, the migration is worth months of work.

---

## 12. Showback and Chargeback Architecture

The cultural lever.

### 12.1 What it is

- **Showback:** show each team how much they cost to observe. No bill, no penalty — just visibility.
- **Chargeback:** the team's budget is debited for their observability cost.

Showback first; chargeback after the org is mature enough not to game it.

### 12.2 The architecture

```
1. Tag every signal with team/service ownership.
2. Aggregate cost per (team × signal × tier).
3. Publish a dashboard: $$$ per team.
4. Send a weekly report.
5. Quarterly review: trend, top growth, top reduction wins.
```

### 12.3 What changes when teams see the bill

Concretely (from real deployments):
- Teams aggressively delete unused metrics.
- Cardinality reductions happen *without* the platform team asking.
- Trace sampling rates get tuned.
- Alert hygiene improves (alerts are also cost).

A typical 30–50% cost reduction in the first year of showback at scale.

### 12.4 Per-team observability dashboards

```
checkout-svc — Observability cost summary

Last month: $14,200
  Metrics: $4,500 (32%)
    - 28K active series, $0.16 / series / month
    - Top metric: http_request_duration_seconds (8K series, 28%)
  Logs: $6,200 (44%)
    - 850 GB ingested
    - 65% INFO-level (consider sampling)
  Traces: $2,800 (20%)
    - 12M spans / day
    - Tail-sampling: 8% kept
  Profiles: $700 (5%)

Trend: +12% MoM (driven by new pricing-engine deploy)

Top opportunities:
  1. Drop http_request_user_agent label: -$800/mo
  2. Sample INFO logs at 10%: -$2,000/mo
  3. Tighten trace tail-sample policy: -$500/mo

Total potential: -$3,300 / month (-23%)
```

This is the format that drives behavior. Teams act on $-savings.

### 12.5 Chargeback tradeoffs

Done well, chargeback aligns incentives. Done badly, it incentivizes:
- Hiding signals (turn off useful telemetry to save cost).
- Gaming attribution (move costs to "shared" buckets).
- Fighting over rate cards.

Roll out chargeback only after a year of showback, with strong cultural norms.

---

## 13. The Quarterly Hygiene Cycle

The structured cleanup ritual.

### 13.1 The agenda

90 minutes per quarter, per team:

1. **Top-cost metrics review.** Top 20 metrics by cardinality / cost. For each: kept, dropped, or modified.
2. **Unused metrics review.** Metrics not referenced by any dashboard or alert in the last quarter. Deletion candidates.
3. **Unused alerts review.** Alerts that didn't fire (false positives) or fired without action. Delete or retune.
4. **Retention adjustment.** Are tiers right? Anything moved hot → warm or warm → cold?
5. **Sampling review.** Trace sample rates, log sample rates. Are they tight enough? Too tight?
6. **Cost trend.** Where is cost growing? Why?

### 13.2 The "delete" PR

Output of the hygiene cycle: one PR per team per quarter that deletes unused metrics, alerts, and dashboards. Often hundreds of lines removed.

### 13.3 What gets measured gets cleaned

Without the cycle, observability artifacts grow monotonically. With it, the platform stays sharp.

The platform team's role: *force* the cycle. Calendar invites; published agenda; tracked output. Without enforcement, teams skip it.

---

## 14. The "Is This Label Worth It?" Rubric

Three-question test:

```
1. Will I aggregate over it?
   sum / rate / group_by — if yes, label.
   if no — log/trace attribute.

2. Is value-space bounded?
   < 100 unique values: comfortable label.
   100-1000: ok with monitoring.
   1000-10K: probably too much; verify.
   > 10K: not a label. log/trace attribute.

3. Will I alert on it?
   If yes: it must be in metrics, with bounded cardinality.
   If no: maybe a log attribute is sufficient.
```

If the answer to #1 is "no," it doesn't go in metrics. End of discussion.

---

## 15. Anti-Patterns

1. **Adding labels for "future flexibility."** Cost is now; benefit is "maybe later."
2. **Per-customer / per-user labels in metrics.** Death.
3. **Full URL paths as labels.** Templating failure.
4. **No cardinality budget per service.** Growth has no cap.
5. **No CI cardinality check.** Bombs ship undetected.
6. **No tiering on logs.** Pay hot prices for week-old data.
7. **100% trace sampling.** Bill 10× too high.
8. **No tail sampling.** Storage absorbs the burden.
9. **Index-everything for application logs.** 5–10× more expensive than label-index.
10. **No retention policy.** Default-keep-forever.
11. **Showback not implemented.** Teams have no incentive to cut.
12. **Fighting over chargeback.** Cultural; needs maturity first.
13. **No quarterly hygiene cycle.** Artifacts grow forever.
14. **Same retention for all log classes.** Audit logs expire too soon, debug logs kept too long.
15. **Vendor cost surprise.** No forecasting; finance reacts.

---

## 16. Worked Example: A 10× Cost Reduction

A real-shape case study.

### 16.1 Starting state (Q1)

- Self-hosted Mimir + Loki + Tempo
- 200 services
- Total observability cost: $480k / year (~$40k / month)
- Top contributors:
  - Logs: $24k/mo (60%) — Elasticsearch
  - Metrics: $11k/mo (28%) — Mimir cluster + 3 ingesters at 200 GB RAM each
  - Traces: $4k/mo (10%) — Tempo + S3
  - Profiles: $1k/mo (2%)

### 16.2 Audit findings

- Logs: 1.5 TB/day; 70% INFO-level; no sampling.
- Metrics: 12M active series; top 20 metrics own 60% of cardinality; 2 metrics each have 1M+ series due to `customer_id` labels.
- Traces: head-sampled at 100%, tail-sampling not configured.
- Many alerts unused; no quarterly hygiene cycle.

### 16.3 Actions

1. **Migrate logs Elasticsearch → Loki.** 4-week migration. ~10× cost reduction on logs.
2. **Sample INFO logs at 10%.** Additional 35% reduction on the surviving log volume.
3. **Drop `customer_id` from 2 metrics; replace with `customer_tier`.** Cardinality drops from 12M to 2M.
4. **Configure tail sampling: keep 100% errors, 5% baseline.** Trace cost drops 80%.
5. **Implement showback dashboards.**

### 16.4 Outcome (Q3)

- Total cost: $48k / year (~$4k / month)
- ~10× reduction
- Team satisfaction: dashboards still work; debugging still fast.
- Logs cost: $1.5k/mo
- Metrics cost: $1.5k/mo
- Traces cost: $0.6k/mo
- Profiles cost: $0.4k/mo
- Operational complexity: simpler (fewer Elasticsearch nodes; smaller Mimir cluster)

### 16.5 The lesson

The cost reduction came from *no individual heroic technical change* — it was systematic application of cardinality and sampling discipline. The platform team's role was to *force* the audit and provide tools (showback, cardinality alerts, hygiene cycle).

10× cost reductions are achievable and *common* in stacks that have grown organically. The investment is platform-team time, not vendor switching.

---

## 17. Pitfalls

1. **Cardinality grows monotonically.** Without a budget, no team cuts.
2. **Per-team cost invisible.** No accountability.
3. **No CI checks for cardinality.** Bombs ship.
4. **No tiering.** Hot prices for stale data.
5. **No tail sampling.** Trace bill explodes.
6. **No log sampling.** Default 100% retention.
7. **No retention policy.** Storage grows forever.
8. **Wrong log architecture.** Index-everything when label-index would do.
9. **Vendor lock-in cost trap.** Vendor-specific labels can't migrate easily.
10. **No quarterly hygiene cycle.** Artifacts accumulate.
11. **No showback.** Teams have no signal.
12. **Showback without trends.** Teams see one snapshot, no urgency.
13. **Chargeback too early.** Gaming, hiding, fighting.
14. **Native histograms not adopted.** 10× storage savings left on the table.
15. **Profile usage skipped.** Cheapest signal, often unused.

---

## 18. Mental Models

> **Cardinality is the lever. Storage is cheap; index memory isn't.**

> **A label costs forever. Add reluctantly; delete eagerly.**

> **Move high-cardinality dimensions to logs / traces. Metrics are for aggregates.**

> **Tier retention. Hot is for the last week; cold is for the last year; archive is for compliance.**

> **Tail-sample traces. 5–10× cost reduction, no debug loss.**

> **Sample logs by level. Errors at 100%; debug at 1%.**

> **Showback first; chargeback after maturity.**

> **Quarterly hygiene cycle. Without it, the system rots.**

> **Native histograms cut storage 10×. Adopt them.**

> **Continuous profiles are cheap. Run them.**

Now go to `doc 19` (multi-tenancy) — once the platform serves many teams with different cardinality profiles, isolation becomes the next problem.
