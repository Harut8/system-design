# 33 — Federated Multi-Region Observability

> Once your services span regions, your observability must too. Each region must have local observability (for low-latency operations and regional autonomy); global observability must aggregate (for cross-region SLOs and platform-wide dashboards). Getting the federation right — without coupling failure domains — is one of the harder architectural problems for a platform team.

This chapter is about the patterns: hub-and-spoke, mesh, federated query, replication, and the trade-offs each makes.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [Why multi-region observability is hard](#2-why-hard)
3. [The three architectures](#3-three-architectures)
4. [Hub-and-spoke](#4-hub-and-spoke)
5. [Federated query](#5-federated-query)
6. [Replicated single-cluster](#6-replicated)
7. [Cross-region trace assembly](#7-cross-region-traces)
8. [Per-region SLOs and global SLOs](#8-slos)
9. [The "global query" performance problem](#9-global-query)
10. [Failure-domain isolation](#10-failure-domains)
11. [Data residency intersection (cross-link to compliance)](#11-residency)
12. [Multi-cloud observability](#12-multi-cloud)
13. [Anti-patterns](#13-anti-patterns)
14. [Worked example: 5-region active-active stack](#14-worked-example)
15. [Pitfalls](#15-pitfalls)
16. [Mental models](#16-mental-models)

---

## 1. Thesis

Three claims:

1. **Per-region observability is the unit; global is an aggregation layer.** Each region runs its own stack; cross-region queries are a federation, not a flat namespace.
2. **Failure domains must not couple.** A regional outage shouldn't kill global observability. Otherwise you're blind exactly when you most need vision.
3. **Trace assembly across regions is the hardest correctness problem.** A request that crosses regions must produce a single trace, with all spans visible in one query. The architecture must support it explicitly.

If your team treats "multi-region" as "we run two Prometheus instances," you'll discover at the next regional outage that half your observability is gone, the other half is uncoupled, and nobody can reconcile the two.

---

## 2. Why Multi-Region Observability Is Hard

| Concern | Single-region | Multi-region |
|---|---|---|
| Latency | Local | Cross-region adds 50-200ms |
| Reliability | One failure domain | One per region |
| Data residency | One regime | Per-region rules |
| Cardinality | Local | Aggregate or per-region |
| Trace context | Single namespace | Must propagate across regions |
| Query | Local store | Federation or aggregation |
| Cost | Linear | Cross-region transfer cost |
| Operational complexity | One stack | N stacks + federation layer |

Each adds engineering work that doesn't exist single-region.

---

## 3. The Three Architectures

The decision tree.

### 3.1 Architecture A: Independent regions, federated query

Each region runs its own complete stack. Queries fan out to regions and aggregate.

```
Region 1: full stack (Mimir, Loki, Tempo, Grafana)
Region 2: full stack
Region 3: full stack
                 ↓
Global Grafana / query frontend → fans out to regional stacks
```

**Pros:** Strong failure-domain isolation; low operational coupling; simple per-region.
**Cons:** Cross-region queries slower; cross-region trace assembly hard.

Default for most orgs.

### 3.2 Architecture B: Per-region ingest, central global store

Each region ingests locally; replicates to a central store for global queries.

```
Region 1 ingest → buffer → central global store (in one region or replicated)
Region 2 ingest → buffer → central
Region 3 ingest → buffer → central
                              ↓
                          Global Grafana
```

**Pros:** Single query namespace; simpler global queries.
**Cons:** Central is SPOF (or expensive to multi-region); cross-region replication cost.

Used by some hyperscalers; rare in mid-size orgs.

### 3.3 Architecture C: Truly distributed (multi-region single-cluster)

Mimir / VictoriaMetrics with multi-region replication; queries served from any region.

```
Multi-region Mimir cluster:
  - 3 ingesters in Region 1
  - 3 ingesters in Region 2
  - 3 ingesters in Region 3
  - Replicated S3 / object storage
  - Queriers in any region serve any data
```

**Pros:** Single namespace; geographic redundancy.
**Cons:** Network cost; complex; latency-sensitive.

Used by the platform-team-as-a-product model; rare.

### 3.4 The choice

| Org size | Pattern |
|---|---|
| < 100 services, 1-2 regions | Architecture A (independent regions) |
| 100-1000 services, 3-5 regions | Architecture A or B |
| > 1000 services, 5+ regions | Architecture A with strong federation |
| Hyperscale | Custom |

Most orgs land on A (independent regions, federated query).

---

## 4. Hub-and-Spoke

A specific instance of Architecture A.

### 4.1 The pattern

- Each region (spoke) runs its own observability stack.
- A central region (hub) hosts the global Grafana, federation layer.
- Spokes are autonomous; hub aggregates for cross-region views.

### 4.2 The advantages

- Spokes survive hub outage (with degraded global view).
- Hub survives most spoke outages (loses one region's data, others continue).
- Per-region SLOs run locally.
- Global SLOs run at the hub.

### 4.3 The hub's role

- Global Grafana (datasources point to spoke stacks).
- Cross-region rollup metrics (recording rules at the hub aggregate spoke metrics).
- Cross-region trace search (querying spoke trace stores via federation).
- Global alerting (alerts on aggregated metrics).

### 4.4 The hub's failure mode

When the hub goes down:
- Spokes still operate normally.
- Global queries fail.
- Per-region engineers continue.
- Global SLO calculations stall.

The hub is failure-isolated *from* the spokes; not vice versa. Acceptable if spoke stacks are self-sufficient.

### 4.5 The hub's location

- One region of the org (often headquarters or biggest market).
- Or a dedicated "control plane" region (separate from any production region).

Don't pick a region with strict data residency for the hub; cross-region aggregation may not be allowed.

---

## 5. Federated Query

The mechanism.

### 5.1 Prometheus federation (classic)

A Prometheus scrapes another Prometheus for a subset of metrics:

```yaml
scrape_configs:
  - job_name: 'federate'
    honor_labels: true
    metrics_path: '/federate'
    params:
      'match[]':
        - '{__name__=~"job:.*"}'   # only recording rules
    static_configs:
      - targets: ['region1-prom:9090', 'region2-prom:9090', ...]
```

The hub Prometheus scrapes recording-rule metrics from each spoke. *Only the aggregates* — not raw cardinality.

### 5.2 Mimir / Cortex multi-tenant federation

Mimir's federation: a query frontend can be configured to fan out queries to multiple Mimir clusters, treat their tenants as one.

### 5.3 Thanos query

Thanos Querier fans out queries to multiple Thanos sidecars (each next to a Prometheus) plus the cold object store. Single query interface; multiple data sources.

### 5.4 Grafana mixed datasources

Grafana 10+ supports cross-datasource queries: combine results from multiple Prometheus / Loki / Mimir instances in one panel.

### 5.5 The query-cost dimension

Federated queries are expensive:
- Cross-region network.
- Multiple stacks must respond.
- Slowest stack determines total latency.

Cache aggressively at the hub.

### 5.6 The "recording rules at the spoke" pattern

Pre-aggregate at the spoke; federate only the aggregates. Reduces volume; reduces query latency.

```yaml
# At each spoke
- record: region:checkout:errors_per_request:rate5m
  expr: |
    sum(rate(checkout_errors_total[5m]))
      /
    sum(rate(checkout_requests_total[5m]))

# At the hub
sum by (region) (region:checkout:errors_per_request:rate5m)
```

The hub queries one aggregated metric per region instead of all raw series.

---

## 6. Replicated Single-Cluster

The Architecture C path.

### 6.1 Mimir multi-zone

Mimir supports zone-aware replication: ingester replicas in different zones; queries served from any zone.

For multi-region: extend zones to be regions. With replication factor ≥ 3, lose any one region; data still queryable.

### 6.2 The cost dimension

Cross-region replication is expensive:
- 3× the data flow (replication factor 3).
- Cross-region network charges.

For hot signals (metrics, recent traces): often justifiable.
For cold signals (long retention logs): too expensive; tier to per-region cold.

### 6.3 The latency dimension

Writes must reach replicas in N regions before acknowledged. Adds 50-100ms per write.

For metric ingestion (high throughput): acceptable if write path is async.
For interactive queries: query routes to local region; cross-region only for misses.

### 6.4 When this is right

- Org wants strict global SLO views without per-region drift.
- Engineering team has the depth to operate it.
- Cost is not the limiting factor.

Most orgs don't fit. Federated remains the default.

---

## 7. Cross-Region Trace Assembly

The hardest correctness problem.

### 7.1 The challenge

A request enters region 1; calls a service in region 2; calls another in region 3. The trace has spans in three regional trace stores.

When debugging, you want one view of the entire trace.

### 7.2 The solutions

#### Solution 1: traces stored in one region

Tail-sample at a global gateway; ship all kept traces to a single region's Tempo. Per-region Tempo doesn't store the trace.

**Pros:** Single trace store; easy cross-region queries.
**Cons:** Cross-region trace transfer cost; failure-domain coupling (one region's failure = no traces).

#### Solution 2: Per-region storage with global index

Each region stores its own spans. A global index records "trace_id X has spans in regions [1, 2, 3]." Queries fan out.

**Pros:** Failure-domain isolation.
**Cons:** Index is its own service; query performance.

#### Solution 3: Replicate trace_id-to-region mapping

Every region maintains a map: which trace_ids did it see? On query: ask each region.

**Pros:** Distributed; resilient.
**Cons:** Each region carries some metadata; queries fan out.

### 7.3 The traceparent propagation

Even with the right storage architecture, the trace header must propagate across regional boundaries. Inter-region calls must include `traceparent` and `tracestate`.

### 7.4 The recommendation

For most orgs, **solution 2 (per-region storage with federated query)** balances cost, complexity, and reliability. Tempo's recent features (cross-cluster query) implement this.

For very small orgs with cross-region traffic: ship cross-region traces to a single store.

---

## 8. Per-Region SLOs and Global SLOs

The SLO architecture.

### 8.1 Per-region SLOs

Each region computes its own SLI/SLO over its local data:

```yaml
- name: checkout_availability_us_east
  scope: region=us-east-1
  target: 0.999
```

Used by the regional team for regional SLOs.

### 8.2 Global SLOs

Aggregated across regions:

```yaml
- name: checkout_availability_global
  metric: sum_across_regions(checkout_good) / sum_across_regions(checkout_total)
  target: 0.999
```

Used by the journey owner for end-to-end visibility.

### 8.3 The "weighted by traffic" gotcha

A user in a small region sees their bad experience the same as a user in a big region. The global SLO is a *traffic-weighted* average; small regions' issues hide.

Defense: per-region SLOs in addition to global. Small-region degradation is visible regionally even if invisible globally.

### 8.4 The cross-region SLO

For multi-region requests, a different SLO:

```yaml
- name: cross_region_request_success
  filter: source_region != destination_region
  target: 0.99
```

Different target than intra-region (cross-region is expected to be slightly less reliable).

### 8.5 The error-budget federation

Each region has its own budget; the global budget is also tracked. A regional burn doesn't always burn global budget proportionally (small region high errors but small total).

The on-call must understand which budget is burning. Default: per-region pages stay in-region; global pages go to the journey owner.

---

## 9. The "Global Query" Performance Problem

Cross-region queries are slow.

### 9.1 The latency floor

Single-region query: 100ms - 5s.
Cross-region (5 regions): 500ms - 30s.

Slowest regional response determines total. Network adds 50-200ms each way.

### 9.2 Defenses

- Cache aggressively at the hub (per-query result cache).
- Pre-aggregate via recording rules at spokes.
- Limit query scope by region (don't query all if not needed).
- Async / progressive UI ("here's region 1's results; loading 2-5...").
- Time-window short queries first; longer in background.

### 9.3 The "region selector" UX

Most cross-region dashboards default to one region; user explicitly opts into multi-region. Reduces accidental slow queries.

### 9.4 Query frontend tuning

Mimir / Cortex / Thanos all have query frontend caching. Tune:
- Cache TTL (30s-300s typical).
- Cache size.
- Per-tenant cache isolation.

---

## 10. Failure-Domain Isolation

The architectural test.

### 10.1 The "could one region's failure...?"

Run the thought experiment for each scenario:

| Failure | Should affect |
|---|---|
| Region 1 entire outage | Region 1 observability degraded; regions 2, 3 unaffected |
| Hub region outage | Global queries fail; regional queries unaffected |
| Cross-region network partition | Each side operates independently; reconcile when healed |
| Region 1 Mimir cluster fails | Region 1 metrics unavailable; logs/traces continue; regions 2, 3 unaffected |

If any "should not affect" is actually affected, the architecture is too coupled.

### 10.2 Game-day testing

Quarterly: simulate region failure. Verify other regions and the hub continue. Verify alerts fire correctly. Verify recovery procedure.

`doc 38` covers chaos in depth.

### 10.3 The cross-cutting tests

- Kill the hub: do regional dashboards work?
- Kill a region: do other regions and hub work?
- Network partition: does each side operate?
- Cert rotation across regions: does it succeed?

Document, test, automate.

---

## 11. Data Residency Intersection

(Cross-link to `doc 32 §8`.)

### 11.1 The constraint

EU users' data must stay in EU. Some regions are operationally EU-only.

### 11.2 The architectural implication

- EU region's stack stays in EU.
- Cross-region replication of EU data: only to other EU regions.
- The hub: must be in a jurisdiction allowed for cross-border aggregation. Often the EU itself for EU-strict orgs.

### 11.3 The federation gotcha

A federated query that includes EU data sent to a US-based hub for aggregation may be a violation. Architectures:

- The hub for EU-data queries lives in EU.
- US users / hub queries get only non-EU data.
- Or: aggregation at the EU spoke; only the aggregated number leaves EU (legal under most regimes).

This is hard. Get legal involved during design.

---

## 12. Multi-Cloud Observability

The cousin of multi-region.

### 12.1 The shape

- Region 1 on AWS.
- Region 2 on GCP.
- Region 3 on Azure.

Observability per cloud's native services *and* a unified view.

### 12.2 The patterns

- **Cloud-native + federation.** Each cloud uses its native (CloudWatch, Stackdriver, Azure Monitor); a global tool federates (Datadog, Grafana with multiple datasources).
- **Cloud-agnostic stack.** Same OTel + Mimir + Loki + Tempo on each cloud; federate as in `doc 33 §4`.

The cloud-agnostic path is more work upfront; cleaner long-term.

### 12.3 The cost dimension

Multi-cloud observability is expensive:
- Egress between clouds.
- Per-cloud licensing if SaaS.
- Per-cloud operations.

Often justified for vendor independence; rarely for cost.

### 12.4 The trace-across-clouds

W3C trace context propagates the same regardless of cloud. The destination matters: aggregate to one place; respect data residency.

---

## 13. Anti-Patterns

1. **Single global cluster without region awareness.** Failure-domain coupling.
2. **No regional autonomy.** Hub failure cascades.
3. **No cross-region trace assembly.** Trace breaks at boundaries.
4. **No per-region SLOs.** Small-region issues hidden.
5. **Federated queries unbounded in scope.** Slow / expensive.
6. **No cache on federation layer.** Repeated slow queries.
7. **Hub in a strict-residency region.** Aggregation illegal.
8. **No game-day testing of region failure.** Fail untested.
9. **Cross-region data without legal review.** Compliance violation.
10. **Multi-cloud without OTel.** Vendor-specific instrumentation in each.
11. **No regional Grafana.** Hub is SPOF for dashboards.
12. **No regional alerting.** Hub fail = silent regions.
13. **No regional capacity plan.** One region runs out before others.
14. **No regional cost attribution.** Cost-by-region invisible.
15. **No traceparent propagation across regions.** Trace assembly broken.

---

## 14. Worked Example: 5-Region Active-Active Stack

Concrete and complete.

### 14.1 The org

- Global SaaS; 5 regions (us-east, us-west, eu-west, ap-southeast, sa-east).
- 200 services per region.
- HIPAA-compliant; data residency strict.

### 14.2 The architecture

Per-region stack (independent):
- OTel collector (DaemonSet + gateway).
- Mimir (multi-tenant, region-tagged).
- Loki (per-region).
- Tempo (per-region).
- Alertmanager (per-region; per-region paging).
- Grafana (per-region).

Hub (us-east, the largest market; non-EU compliant):
- Global Grafana (datasources point to per-region Grafanas).
- Global recording rules aggregating per-region metrics.
- Global alertmanager for cross-region alerts.

### 14.3 The federation rules

```yaml
# At each spoke: pre-aggregate journey-level SLI metrics
- record: journey:checkout:availability:1h
  expr: |
    sum(rate(checkout_success_total[1h]))
      /
    sum(rate(checkout_total[1h]))

# At the hub: aggregate
sum by (region) (journey:checkout:availability:1h{region=~".+"})
sum (journey:checkout:availability:1h{region=~".+"}) / count(...)
```

Hub queries 5 series per metric (one per region), not millions of raw samples.

### 14.4 Cross-region trace assembly

Trace gateway in each region tail-samples; sends kept traces to the *region of origin* (where the request entered) plus a copy of any cross-region spans.

For cross-region calls, the originating region's Tempo holds the full trace. Spans from other regions arrive via OTLP push.

The user query for trace_id X: hits the originating region's Tempo (often inferred from the trace_id partition). Single query; full trace.

### 14.5 Per-region SLOs

Each region:
- Service-level SLOs (per region's view of each service).
- Journey-level SLOs (per region's experience of the journey).

Hub:
- Global journey SLOs (aggregated across regions).
- Cross-region request SLOs.

### 14.6 Failure-domain test

Simulated: us-east region full outage.
- us-east observability stack offline.
- Hub global queries: us-east's data missing; other regions visible.
- us-west, eu-west, ap-southeast, sa-east: continue normally.
- Per-region SLOs in other regions unaffected.
- Regional pages in other regions continue.

Recovery:
- us-east restores.
- Backfill from buffer (24h Kafka retention; survived).
- Global SLO recomputed for the affected window.
- Documented gap; report.

### 14.7 The cost

5 regional stacks × ~$100k/year each = $500k/year base.
Hub + federation = ~$50k/year.
Cross-region replication for hot trace data = ~$100k/year.
Total: ~$650k/year. ~3× single-region cost; vs ~5× cost of replicated single-cluster.

The federation pattern is most cost-effective at this scale.

---

## 15. Pitfalls

1. **Coupled failure domains.** One region's failure cascades.
2. **No regional autonomy.** Hub failure = total blindness.
3. **Trace assembly broken across regions.** Cross-region debugging impossible.
4. **No per-region SLOs.** Small-region issues invisible.
5. **Federated queries too broad.** Slow / expensive.
6. **No cross-region capacity plan.** Headroom uneven.
7. **No game-day testing.** Failures untested.
8. **Hub location violates residency.** Compliance issue.
9. **No traceparent across regions.** Trace fragments.
10. **No cost-by-region.** Attribution missing.
11. **Multi-cloud without OTel.** Vendor lock-in per cloud.
12. **No regional alerting.** Hub-only alerts.
13. **No regional dashboards.** Hub-only Grafana.
14. **No backfill plan.** Region-outage data loss.
15. **No cross-region trace search optimization.** Slow queries.

---

## 16. Mental Models

> **Per-region observability is the unit; global is aggregation.**

> **Failure domains must not couple. Hub failure ≠ regional blindness.**

> **Architecture A (federated) for most; Architecture C (replicated single-cluster) for hyperscale.**

> **Recording rules at spokes; federate only the aggregates.**

> **Cross-region trace assembly: per-region storage + originating-region as the trace home.**

> **Per-region and global SLOs both. Small regions' pain hidden by aggregates alone.**

> **Game-day regional outages quarterly.**

> **Hub location respects data residency.**

> **Multi-cloud is harder than multi-region. OTel + cloud-agnostic stack required.**

> **Cross-region cost is real. Plan for it.**

Now go to `doc 34` (schema and semantic-conventions governance).
