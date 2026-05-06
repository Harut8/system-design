# 28 — Telemetry Pipeline Reliability: Who Observes the Observer

> The most expensive failure mode in observability is *blind during incident*. Mimir is down; the very signals you need to debug Mimir are stored in Mimir. Pages don't fire because the pager depends on the broken pipeline. The on-call engineer is debugging by guessing. This chapter is about preventing that — the meta-observability layer, where the platform observes itself.

This chapter is the platform team's own reliability discipline, applied to its own product.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [The dogfooding paradox](#2-paradox)
3. [The independent observation path](#3-independent-path)
4. [Telemetry pipeline SLOs](#4-pipeline-slos)
5. [Per-component observability of the stack itself](#5-component-obs)
6. [Backpressure and queue depth](#6-backpressure)
7. [Loss budgets: when dropping is acceptable](#7-loss-budgets)
8. [Synthetic canaries for the platform](#8-synthetic-canaries)
9. [The "what's broken in the platform" dashboard](#9-platform-dashboard)
10. [Cardinality of meta-telemetry](#10-meta-cardinality)
11. [Failure modes specific to telemetry pipelines](#11-failure-modes)
12. [Incident response when the platform is the incident](#12-platform-incident)
13. [Capacity for the platform itself](#13-platform-capacity)
14. [Cross-region / DR for telemetry](#14-dr)
15. [Anti-patterns](#15-anti-patterns)
16. [Worked example: detecting the silent loss](#16-worked-example)
17. [Pitfalls](#17-pitfalls)
18. [Mental models](#18-mental-models)

---

## 1. Thesis

Three claims:

1. **The observability platform is itself a service with SLOs.** Ingest availability, query availability, data freshness, retention compliance. The platform team must own these the same way service teams own theirs.
2. **The platform must observe itself with an *independent* path.** A side-channel that doesn't depend on the platform's main pipeline. Otherwise you're blind exactly when you need vision.
3. **The platform is the upper bound on every other team's reliability.** If the platform is at 99.5%, no service can credibly promise 99.9% — because they can't prove their own SLOs without telemetry.

If your team can't answer "is the observability platform healthy right now?" without using the platform itself, you have a circular dependency that will hurt at the worst possible moment.

---

## 2. The Dogfooding Paradox

The platform team observes itself with the same tools it builds. This is good — eats own food, finds bugs, has authentic empathy for users.

It's also bad — when the platform fails, the *observation of the platform* fails too.

### 2.1 The classic failure mode

```
14:30  Mimir ingester memory pressure begins.
14:35  Ingester OOMKills cascade.
14:36  Pages should fire ("ingest failure rate > 1%")
       — but the alert evaluator depends on Mimir.
       Alert can't evaluate. No page.
14:50  Customer-team SREs notice their dashboards are stale.
       They ask in #observability-team Slack.
14:52  Platform team investigates. Discovers what's been wrong for 22 minutes.
14:55  Mitigation begins.
```

Twenty-two minutes of customer impact, invisible to the platform team. The cause: monitoring of Mimir relies on Mimir.

### 2.2 The fix: dual-path observation

A *minimal* independent path observes the main path.

```
Main pipeline:  apps → collector → Mimir → Grafana → alerts
                                    ↑
Mini pipeline:  ─── independent Prometheus + Alertmanager ───┘
                scrapes Mimir's /metrics directly,
                separate from Mimir; pages on platform health.
```

The mini pipeline is small (single Prometheus, ~1 GB RAM, 7 days retention). Its only job: alert when the big pipeline is broken.

### 2.3 The "tier-0 alerts" pattern

A *small* set of alerts (5-10) on the absolute essentials of the platform:
- Ingest availability.
- Query availability.
- Data freshness (lag from emit to query).
- Retention enforcement.
- Per-tenant isolation.

These are evaluated on the independent path. They page directly to platform on-call, bypassing the main alerting system.

---

## 3. The Independent Observation Path

The architecture.

### 3.1 What's independent

- Separate compute (different cluster ideally).
- Separate storage (own Prometheus / VictoriaMetrics).
- Separate alerting (own Alertmanager).
- Separate paging path (different PagerDuty service / direct SMS).

### 3.2 What it observes

The main platform's components:
- Ingester memory, ingestion rate, queue depth.
- Querier query rate, query latency.
- Compactor lag.
- Object-storage health (S3 5xx rate).
- Alertmanager itself (cluster membership, queue).

Plus *synthetic* checks (§8): does a known-test metric arrive at the platform within 30s of emission?

### 3.3 The "tiny Prometheus"

The minimal independent Prometheus:
- Single replica.
- 7-15 day retention.
- ~50 metrics scraped (tier-0 only).
- ~1 GB memory.
- Cost: trivial.

For multi-cluster / multi-region: one tiny Prometheus per region.

### 3.4 The independent paging path

The page from the tiny Prometheus must reach humans even if the main pipeline is broken. Implementations:

- Direct PagerDuty integration (PagerDuty's API is independent).
- SMS via Twilio (independent of PagerDuty).
- Phone call via PagerDuty Voice.
- Backup paging via external cron job that pings a dead-man's-switch service (Healthchecks.io, Cronitor).

A multi-path page is good practice. The platform's main paging path *includes* the platform's own alerting; you need a backup.

---

## 4. Telemetry Pipeline SLOs

The SLOs the platform team commits to.

### 4.1 The four core SLIs

| SLI | Definition | Target |
|---|---|---|
| **Ingest availability** | % of writes accepted in the last hour | 99.95% |
| **Query availability** | % of queries returning successfully | 99.9% |
| **Freshness** | Time from emit to queryable | p99 < 60s |
| **Retention compliance** | % of data preserved through configured retention | 100% |

### 4.2 Per-tenant SLOs (revisited)

`doc 19 §14`: each tenant has its own SLO. The platform's *aggregate* SLOs are the per-tenant SLOs, summed.

### 4.3 The query-availability subtlety

A query can fail several ways:
- Backend unreachable.
- Timeout.
- Limit exceeded (per tenant).
- Internal error.

Limit-exceeded is *not* a platform failure — it's the user's request hitting their cap. Distinguish:

```
query_total{outcome="success"}
query_total{outcome="error", reason="backend"}      # platform fault
query_total{outcome="error", reason="timeout"}      # platform / query fault
query_total{outcome="error", reason="limit"}        # user fault
```

SLO numerator = `success`; SLO denominator = `success + reason="backend" or "timeout"`.

### 4.4 The freshness SLO

```yaml
- name: freshness
  metric: percent_of_metric_samples_queryable_within_60s
  target: 0.99
```

Computed by the synthetic canary (§8). A test metric is emitted with a known timestamp; the canary queries for it. The lag is the freshness measurement.

### 4.5 The retention SLO

```yaml
- name: retention_compliance
  metric: data_loss_events_total
  target: 0
```

Any data loss = SLO breach. Detection: occasional sweep that checks "data from 28 days ago is queryable per tenant SLA."

---

## 5. Per-Component Observability of the Stack Itself

The components the platform comprises, observed.

### 5.1 OTel Collector (gateway)

Key metrics:
```
otelcol_processor_batch_batch_send_size_bucket
otelcol_exporter_send_failed_*_total
otelcol_exporter_queue_size
otelcol_exporter_queue_capacity
otelcol_receiver_accepted_*_total
otelcol_receiver_refused_*_total
```

Saturation: `queue_size / queue_capacity > 0.8` → backpressure imminent.

### 5.2 Mimir / Cortex / VictoriaMetrics

Ingester:
```
cortex_ingester_memory_series
cortex_ingester_ingested_samples_total
cortex_distributor_received_requests_total
cortex_ingester_chunk_utilization
prometheus_tsdb_compactions_failed_total
```

Querier:
```
cortex_query_frontend_queue_length
cortex_querier_queries_total
cortex_querier_query_duration_seconds_bucket
```

Compactor:
```
cortex_compactor_runs_completed_total
cortex_compactor_runs_failed_total
cortex_compactor_blocks_remaining
```

Object storage:
```
thanos_objstore_bucket_operation_failures_total
thanos_objstore_bucket_operation_duration_seconds_bucket
```

### 5.3 Loki

```
loki_distributor_lines_received_total
loki_distributor_bytes_received_total
loki_ingester_chunks_flushed_total
loki_ingester_chunk_age_seconds
loki_ingester_streams_count
```

### 5.4 Tempo

```
tempo_distributor_spans_received_total
tempo_ingester_blocks_total
tempo_querier_external_endpoint_duration_seconds_bucket
```

### 5.5 Alertmanager

```
alertmanager_alerts_received_total
alertmanager_notifications_total{integration}
alertmanager_notifications_failed_total{integration}
alertmanager_cluster_members
alertmanager_notification_latency_seconds_bucket
```

`alertmanager_cluster_members` should equal replica count; mismatch = gossip broken.

### 5.6 Grafana

```
grafana_api_response_status_total{code}
grafana_database_total_seconds_bucket
grafana_proxy_response_status_total
```

Dashboards loading slow → user complaints. SLO on dashboard render time.

### 5.7 The platform-stack dashboard

Single dashboard with row per component:
- Ingest rate / failure rate.
- Queue depth / saturation.
- Recent errors.
- Recent restarts.

Bookmark in every platform engineer's home page.

---

## 6. Backpressure and Queue Depth

The "we're falling behind" signal.

### 6.1 Where queues live

```
agent disk buffer (cheap, large)
  ↓
collector memory queue (small, fast)
  ↓
exporter queue (per-backend, per-batch)
  ↓
backend ingest WAL
  ↓
Kafka topic (if present)
  ↓
backend ingester
```

Each queue can fill independently. Each fill triggers different consequences.

### 6.2 The propagation model

A slow backend → exporter queue fills → collector memory queue fills → collector OOMKilled → agent has nowhere to send → agent disk buffer fills → agent drops.

Each step in this chain is an alertable signal *before* the final drop.

### 6.3 The mandatory queue depth alerts

```
otelcol_exporter_queue_size / otelcol_exporter_queue_capacity > 0.7  → ticket
otelcol_exporter_queue_size / otelcol_exporter_queue_capacity > 0.9  → page
agent_disk_buffer_used_bytes / agent_disk_buffer_capacity_bytes > 0.7  → ticket
agent_disk_buffer_used_bytes / agent_disk_buffer_capacity_bytes > 0.9  → page
kafka_consumer_lag_seconds > 60                                       → page
```

These let you act *before* data is dropped.

### 6.4 The "drop" metric

When data *is* dropped (last resort), measure it:

```
otelcol_exporter_send_failed_metric_points_total
otelcol_processor_batch_dropped_records_total
agent_buffer_dropped_records_total
```

Any non-zero value is an SLO event. Alert.

---

## 7. Loss Budgets: When Dropping Is Acceptable

The cost-vs-completeness trade-off.

### 7.1 The honest stance

Telemetry has a loss budget — typically 0.001% of samples can be dropped without operational impact. Treating loss as zero-tolerance is unrealistic at scale.

### 7.2 The tier of loss

| Signal | Acceptable loss | Why |
|---|---|---|
| Audit logs | 0% | Compliance |
| SLI metrics | 0% | SLO calculations |
| Application logs (debug) | up to 1% | High volume, low marginal value |
| Traces (sampled) | up to 5% | Already sampled; small additional loss tolerable |
| Profiles | up to 1% | Statistical signal |

Per-signal loss SLO. Audit and SLI are zero-tolerance. Others are bounded.

### 7.3 Where loss is acceptable

- Drop low-cardinality debug logs at the agent under pressure.
- Drop low-value sampled traces under pressure.
- Drop redundant metrics that are recoverable from raw events.

Keep:
- Errors.
- Audit.
- SLI metrics.
- Recent transactions.

### 7.4 The "shed-load policy"

A documented policy: when the platform is under pressure, what gets dropped first?

```
Tier 1: drop debug logs at agent
Tier 2: drop INFO logs sample 50%
Tier 3: drop sampled traces sample 50%
Tier 4: drop low-priority metrics (defined per service)
Never: errors, SLI, audit
```

Configurable via the collector config. When the platform is healthy, no shedding. Under pressure, ramp through tiers.

---

## 8. Synthetic Canaries for the Platform

The active-measurement layer.

### 8.1 The canaries

A small service emits known-shaped telemetry continuously:

```
canary emits:
  metric_v1{canary="true"} = (current timestamp seconds)
  log_v1{canary="true"}: "{ts: ..., trace_id: ..., msg: 'canary-tick'}"
  trace span: "canary.tick"
  profile sample: stack containing "canary.fn"
```

Frequency: every 30 seconds.

### 8.2 The verifier

A second service queries the platform for the canary data and verifies:

- Was the metric written?
- Was the timestamp within freshness SLO?
- Was the log queryable?
- Was the trace queryable?
- Was the profile queryable?

Outputs:

```
canary_metric_freshness_seconds
canary_log_queryable{outcome}
canary_trace_queryable{outcome}
canary_profile_queryable{outcome}
```

These feed the platform's own SLOs.

### 8.3 The independent verifier

The verifier runs in a separate cluster. Its outputs go to the *tiny* Prometheus (§3.3), not the main platform. Page if the canary fails.

### 8.4 The cardinality

Canary metrics use a single label `canary="true"`. Bounded cardinality; no risk to the main platform.

### 8.5 Per-region canaries

For multi-region: one canary per region; verifier checks all. Catches regional outages independently.

---

## 9. The "What's Broken in the Platform" Dashboard

The platform-team home dashboard.

### 9.1 The structure

Top of dashboard: summary status.
- Ingest: 🟢 / 🟡 / 🔴
- Query: 🟢 / 🟡 / 🔴
- Freshness: 🟢 / 🟡 / 🔴
- Retention: 🟢 / 🟡 / 🔴

Per-component panels:
- OTel Collector queue depth and error rate.
- Mimir ingester memory and series count.
- Loki ingester chunk age.
- Tempo distributor errors.
- Alertmanager cluster status.
- Object storage error rate.

Per-tenant panels:
- Top 10 tenants by error rate.
- Top 10 tenants by ingest rate.
- Top 10 tenants by query QPS.

Recent incidents:
- Last 24 hours of platform-tier alerts.

### 9.2 The "platform health summary" page

Simpler version, single page, refreshed every 10 seconds. Designed for executives or non-platform engineers asking "is the platform okay?"

### 9.3 Public status page

For internal users: a status page like `status.observability.example.com` that shows current health. When platform issues arise, this is the source of truth.

---

## 10. Cardinality of Meta-Telemetry

The platform's telemetry is *itself* a cost driver.

### 10.1 The trap

A platform with 12M active series across 50 tenants could easily emit 100K self-metrics: per-ingester, per-tenant, per-metric-type, per-tenant × per-status, etc.

### 10.2 Defenses

- Per-tenant meta-metrics aggregated to tier (small / medium / large) where granularity isn't critical.
- Per-component meta-metrics emitted only on transitions (not continuously).
- Sample meta-traces (the platform's own traces) at low rate.

### 10.3 The "platform tenant"

Treat the platform as its own tenant in the system. Bounded cardinality budget. Charged back to the platform team's budget.

---

## 11. Failure Modes Specific to Telemetry Pipelines

The pathologies.

### 11.1 The "metric storm"

A misbehaving service emits 1M metrics in a second (a debug accident, label injection bug). The platform absorbs the storm or breaks.

Defense: per-tenant ingestion rate limits (`doc 19 §7`). Reject excess; alert the tenant.

### 11.2 The "compactor stuck"

Compactor needs to merge old blocks. If stuck, `head_block_age` grows. Eventually queries can't reach old data.

Defense:
```
prometheus_tsdb_compactions_failed_total > 0      → ticket
head_block_age_seconds > 7200                      → page
```

### 11.3 The "query of death"

A user's query consumes all memory; querier OOMs; cluster degrades.

Defense: per-tenant memory limits (`doc 19 §10.4`). Query frontend rejects on resource exceedance.

### 11.4 The "schema mismatch"

A version upgrade introduces incompatible internal schema. New ingesters can't read old blocks. Effectively, retention is gone.

Defense: pre-upgrade canaries; staged upgrade; rollback path tested.

### 11.5 The "alertmanager partition"

Alertmanager replicas can't gossip; each thinks it's alone; sends pages 3×.

Defense: monitor `alertmanager_cluster_members`; alert on drift; tested mTLS.

### 11.6 The "object storage throttle"

S3 throttles for excessive PUT rate. Compactor can't upload; queues fill; ingester memory grows.

Defense: rate-limit at the object-storage exporter; multiple buckets / partitioning; backoff with jitter.

### 11.7 The "secret rotation outage"

Cert / API key rotated; one component missed the rotation; gossip breaks; pipeline hard-fails.

Defense: cert-rotation as a tested workflow; alert on connection failures with 1-day buffer before expiry.

### 11.8 The "kafka lag explodes"

Kafka consumer (the storage ingester) can't keep up. Lag grows. Eventually retention rolls older messages off; data lost.

Defense: lag SLO + auto-scale of ingesters.

---

## 12. Incident Response When the Platform Is the Incident

The procedure differs from regular incidents.

### 12.1 The challenge

Standard runbooks reference dashboards, alerts, queries. *All* of these depend on the platform. When the platform is broken, the runbook is partly broken.

### 12.2 The "platform broken" runbook

A specific runbook that uses *only* the independent path:
- Tiny Prometheus dashboard (raw URL, not Grafana).
- Direct kubectl access to platform pods.
- Direct S3 / object-store inspection.
- Direct Kafka tooling.
- Manual checking of pod status.

This runbook is *paper / wiki* — never depends on the platform.

### 12.3 The "platform incident" team

A subset of the platform team trained for platform incidents:
- Knows the underlying tools (kubectl, AWS console, k8s control plane).
- Can debug Mimir / Loki / Tempo internals.
- Has direct alerting paths (not via the platform).

### 12.4 Communication during platform incidents

Service teams want to know: "Are my dashboards down because the platform is down, or because my service is down?"

Status page (independent of platform) updates immediately. Internal Slack / email push notifications. Shared communication channel.

### 12.5 The "everyone is paged" cascade

When the platform is down, hundreds of service-level alerts may fire (or fail to fire). Either way, lots of confusion.

Mitigation: when the platform itself is in incident, *suppress* service-level pages temporarily (Alertmanager silence broadly applied). Otherwise on-call pagers across the org all light up.

---

## 13. Capacity for the Platform Itself

The platform has its own capacity plan (`doc 16` applied to the platform).

### 13.1 The growth drivers

- Org-wide service count.
- Service-side cardinality growth (per-team).
- New signals (profiles, RUM, etc.).
- Retention extensions.

### 13.2 The bottlenecks

- Ingester memory.
- Compactor throughput.
- Object storage QPS.
- Query frontend / querier capacity.
- Alertmanager replicas.

### 13.3 The capacity SLI

```
platform_headroom_pct{component}
```

Below 30% on any component = capacity action this quarter.

### 13.4 The procurement lead time

Self-managed: weeks for new clusters; months for new region. Cloud-managed: hours for vertical scaling, days for horizontal capacity reservations.

Plan accordingly. The platform's own capacity surprise is an org-wide outage.

---

## 14. Cross-Region / DR for Telemetry

`doc 36` covers DR in depth; this is the platform-specific subset.

### 14.1 The concern

Regional failure → telemetry from that region missing. Alerts in that region don't fire. SLOs uncomputable.

### 14.2 The patterns

- **Regional independence:** each region has its own platform stack. Cross-region queries via federation.
- **Active-active multi-region:** all regions ingest into a single cross-region store (Mimir with multi-zone replication).
- **Active-passive:** primary region serves; secondary takes over on failure.

For telemetry specifically, regional independence is often sufficient. Within-region failures are rare; cross-region queries during incidents are nice-to-have, not load-bearing.

### 14.3 The recovery path

After a regional outage:
- Backfill data from the affected region (if the backlog survived).
- Recompute SLOs over the affected window.
- Document the gap in the SLO report.

---

## 15. Anti-Patterns

1. **Platform observed only by itself.** Circular dependency.
2. **No tier-0 alerts.** Platform failures invisible.
3. **No independent paging path.** Pages don't fire when needed most.
4. **No platform SLOs.** Quality unmeasured.
5. **No queue-depth alerts.** Drops happen without warning.
6. **No drop metrics.** Loss invisible.
7. **No load-shed policy.** Pressure causes random failures.
8. **No synthetic canary.** End-to-end health unmeasured.
9. **No platform-incident runbook.** Improvisation during outage.
10. **No multi-cluster paging.** Whole-region failure → silence.
11. **Meta-telemetry cardinality unbounded.** Platform observes its own observability problem.
12. **No upgrade canary path.** Schema changes break unexpectedly.
13. **No capacity plan for the platform.** Org-wide outages.
14. **Audit-log retention shared with operational logs.** Compliance fail.
15. **Public status page absent or stale.** Service teams confused during incident.

---

## 16. Worked Example: Detecting the Silent Loss

The story.

### 16.1 The setup

The org runs Mimir + Loki + Tempo. Platform team has tier-0 alerts on the independent Prometheus. Synthetic canaries run every 30s.

### 16.2 The incident

A subtle bug in the OTel Collector (post-upgrade) starts dropping ~3% of metric samples without raising an error metric. Tenants don't notice immediately — their dashboards still work, just with slightly wrong values.

### 16.3 The detection

The synthetic canary emits a metric every 30s. The verifier expects a sample every 30s (with up to 60s lag tolerated). When samples started intermittently missing (3% of canaries failed verification), the freshness SLO burn alert fired.

T+0     Canary samples dropping at 3%
T+5m    Burn-rate alert on freshness SLO fires
T+5m    Page to platform on-call
T+10m   On-call investigates; spots that drop is in the collector
T+25m   Rollback to prior collector version
T+30m   Canaries pass; samples flowing
T+45m   Postmortem ticket filed

### 16.4 The lesson

Without the synthetic canary, this 3% drop would have gone undetected for weeks. Tenants would have noticed eventually as their SLO calculations drifted. The platform-team SLO discipline caught it in 5 minutes.

The cost of the canary: 1 small pod, ~50 metrics. The benefit: detected a silent regression that would have eroded org-wide trust.

---

## 17. Pitfalls

1. **Circular dependency in monitoring.** Blind during the worst moment.
2. **No tier-0 alerts.** Platform failures invisible.
3. **No independent paging.** Pagers tied to platform.
4. **No queue-depth alerts.** Surprise drops.
5. **No drop metrics.** Loss invisible.
6. **No synthetic canary.** End-to-end unmeasured.
7. **No platform-incident runbook.** Confusion during outage.
8. **No load-shed policy.** Drops random.
9. **No platform SLOs.** Quality unmeasurable.
10. **No multi-cluster paging.** Regional outages silent.
11. **No capacity plan for platform.** Whole-org outages.
12. **No upgrade canary.** Schema regressions ship.
13. **Public status page absent.** Service teams confused.
14. **No silence cascade for platform incidents.** All-org pager storm.
15. **No retention-compliance check.** Data loss undetected.

---

## 18. Mental Models

> **The platform is a service with SLOs. It has its own product, its own customers (other teams), its own SLAs.**

> **Observe the observer with an independent path. Tiny Prometheus + tiny Alertmanager + direct paging.**

> **The synthetic canary is mandatory. It's the only way to catch silent loss.**

> **Tier-0 alerts on platform health, evaluated independently, page directly.**

> **Backpressure is a cascade; every queue is a separate alert opportunity.**

> **Loss budget is real. Audit and SLI are zero-tolerance; logs / traces have small bounded loss.**

> **Load-shed policy in advance. Don't decide what to drop during the incident.**

> **Platform-incident runbook uses only the independent path. Don't depend on the broken thing.**

> **Cardinality applies to the platform too. Bound meta-telemetry.**

> **The platform is the upper bound on every other team's reliability.**

Now go to `doc 29` (synthetic monitoring) — the active-measurement complement to RUM.
