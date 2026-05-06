# 19 — Multi-Tenancy

> Once the observability platform serves more than three teams, "everyone shares one Prometheus" stops working. Multi-tenancy is the discipline that makes a *platform* — quotas, isolation, RBAC, billing attribution, noisy-neighbor protection — turning a tool into a product the org consumes.

This chapter assumes `doc 18` (cardinality and cost) and the storage internals of `doc 06`/`doc 07`/`doc 08`. The cost discipline of 18 is what's *enforced* by the multi-tenancy machinery here.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [What "tenant" means in observability](#2-tenant-meaning)
3. [Logical vs physical isolation](#3-isolation)
4. [Per-signal multi-tenancy](#4-per-signal)
5. [Mimir tenants, Loki orgs, Tempo tenants](#5-stack-specifics)
6. [Tenant ID propagation](#6-tenant-id)
7. [Quotas and rate limits](#7-quotas)
8. [RBAC: read paths](#8-rbac)
9. [Auth at the write path](#9-auth-write)
10. [Noisy-neighbor protection](#10-noisy-neighbor)
11. [Per-tenant billing attribution](#11-billing)
12. [Cross-tenant queries](#12-cross-tenant)
13. [Tenant lifecycle: creation, migration, deletion](#13-lifecycle)
14. [Tenant SLOs](#14-tenant-slos)
15. [Anti-patterns](#15-anti-patterns)
16. [Worked example: 50-team Mimir + Loki deployment](#16-worked-example)
17. [Pitfalls](#17-pitfalls)
18. [Mental models](#18-mental-models)

---

## 1. Thesis

Three claims:

1. **Multi-tenancy is what turns a tool into a platform.** Without quotas and isolation, one team's runaway query takes down everyone's dashboards. Without RBAC, every team can read every team's data. Without billing attribution, cost grows monotonically.
2. **Logical isolation is sufficient for most.** Physical isolation (separate clusters per tenant) is overkill for almost every internal use case. Reserve it for regulated workloads, large customer segregation, or tenant-driven SLO differentiation.
3. **Tenant ID is the substrate.** Every signal flowing through the stack carries a tenant_id. Without it, none of the rest works — quotas, billing, RBAC, isolation all need that string.

If your observability stack can't answer "how much is team X using?" or "can team Y see team X's data?" or "if team Z runs a runaway query, does it affect everyone?" — you don't have multi-tenancy. You have a shared cluster.

---

## 2. What "Tenant" Means in Observability

The word is overloaded. Be specific.

### 2.1 Common tenancy axes

| Axis | Examples | Why |
|---|---|---|
| **Internal team** | "payments", "search", "data" | Most common; org-aligned |
| **Customer** | "acme-corp", "globex" | SaaS / B2B platforms with per-customer SLAs |
| **Environment** | "prod-us-east", "staging-eu" | Region/env separation |
| **Compliance** | "pci-zone", "hipaa-zone" | Regulated workloads with audit requirements |
| **Cost center** | "department-101", "department-202" | Finance-aligned, often == teams in mid-size orgs |

### 2.2 Choosing the axis

Pick *one* primary axis. Multi-axis tenancy (team × customer × env) gets unmanageable; the operational complexity grows like the product.

For most internal observability platforms, **team is the right primary axis.** Each team is a tenant. Customer-axis tenancy is for B2B SaaS that exposes telemetry *to* customers (rare; specialized).

### 2.3 Sub-tenants

Teams may want sub-tenancy: "payments has 3 services; can we separate cardinality by service?" Yes — most stacks support this with prefixed tenant IDs (`team-payments-svc-checkout`). Don't over-design; flat tenants with labels usually suffice.

---

## 3. Logical vs Physical Isolation

The architectural decision.

### 3.1 Logical (multi-tenant cluster, tenant_id-based separation)

```
One Mimir cluster:
  - tenant_id="payments" → series stored with that label
  - tenant_id="search"   → series stored with that label
  - Per-tenant quotas at ingester
  - Read path filters by tenant
```

**Pros:**
- One cluster to operate.
- Resource pooling: idle tenant capacity serves busy tenants.
- Easier upgrades.

**Cons:**
- Noisy-neighbor risk requires careful quota and rate-limit configuration.
- One bad tenant query can hurt many tenants if not isolated.
- Compliance / regulated tenants may need stronger boundary.

### 3.2 Physical (dedicated cluster per tenant or per tenant tier)

```
Cluster A (payments-only):
  - Dedicated ingesters, queriers, storage
Cluster B (search-only):
  - Dedicated everything
```

**Pros:**
- Strong isolation; no shared-fate.
- Compliance easier to argue.
- Per-cluster scaling.

**Cons:**
- N× operational cost.
- Inter-tenant queries hard.
- Capacity inefficient (each cluster sized for own peak).

### 3.3 The hybrid

Most production stacks: **logical for internal teams, physical for regulated tenants.**

```
Default cluster: 50 internal teams as logical tenants
Dedicated cluster: pci-zone (regulated)
Dedicated cluster: large-customer-X (per-SLA)
```

The default keeps cost down; physical exists for the cases where logical isn't enough.

### 3.4 The "noisy neighbor" question

The single hardest argument: *can a misbehaving tenant degrade neighbors?*

In a well-configured logical-tenant cluster: no. Per-tenant quotas, per-tenant rate limits, query timeouts, query memory limits, and circuit breakers prevent one tenant's runaway query from affecting others.

In a poorly-configured one: absolutely. Examples: a tenant submits a query that scans 100M series; query frontend uses all memory; other tenants' queries fail. Quota engineering is what prevents this.

---

## 4. Per-Signal Multi-Tenancy

Each signal type has its own tenancy machinery.

### 4.1 Metrics (Mimir, Cortex, VictoriaMetrics)

Mimir's tenancy model:
- HTTP header `X-Scope-OrgID: payments` identifies the tenant.
- All writes are stamped with that tenant.
- Reads honor the tenant for filter.
- Per-tenant configs: limits, retention, ingester sharding.

Configurable per-tenant:
- `max_global_series_per_user` — cardinality limit.
- `ingestion_rate` — samples/sec.
- `max_query_lookback` — how far back queries can reach.
- `max_query_length` — longest single query window.
- `max_samples_per_query` — query memory bound.

### 4.2 Logs (Loki, ClickHouse, Splunk)

Loki uses the same `X-Scope-OrgID` header. Per-tenant:
- Ingestion rate (GB/day).
- Stream cardinality (label combos).
- Query bandwidth.
- Retention.

ClickHouse on logs uses a `tenant_id` column; queries are filtered on it; per-tenant quotas are enforced at the auth layer.

### 4.3 Traces (Tempo, Jaeger)

Tempo: `X-Scope-OrgID` again; per-tenant retention, ingest rate, query rate.

Jaeger has weaker built-in tenancy; teams typically run separate Jaeger instances per tenant cluster.

### 4.4 Profiles (Pyroscope, Parca)

Newer tenancy support; Pyroscope tenants are similar to Loki orgs.

### 4.5 The unified tenancy layer

A common 2026 pattern: a *gateway* (OTel Collector, Grafana Alloy, custom proxy) that enforces auth and stamps the `X-Scope-OrgID` header on every signal. Below the gateway, all stores share the same tenant convention.

```
service → agent → gateway (auth + stamp) → mimir/loki/tempo
                          ↑
                          tenant_id derived from auth context
```

This is the cleanest way to ensure tenant_id is *trusted* (not spoofable by the writer) and *consistent* across signals.

---

## 5. Mimir Tenants, Loki Orgs, Tempo Tenants

Specifics for the Grafana stack — the most common in 2026.

### 5.1 Mimir tenants

Mimir is multi-tenant by default. Tenant configs in YAML:

```yaml
overrides:
  payments:
    max_global_series_per_user: 5_000_000
    ingestion_rate: 100_000
    ingestion_burst_size: 200_000
    max_query_lookback: 90d
    max_query_length: 60d
    max_samples_per_query: 50_000_000
    compactor_blocks_retention_period: 13M
  search:
    max_global_series_per_user: 8_000_000
    ingestion_rate: 200_000
    ingestion_burst_size: 400_000
```

Per-tenant retention, per-tenant cardinality, per-tenant query limits. The `max_samples_per_query` limit is the noisy-neighbor protection — a tenant cannot run a query that scans more than the limit.

### 5.2 Loki orgs

Loki uses "orgs" as its tenancy concept. Same `X-Scope-OrgID` header. Configs:

```yaml
overrides:
  payments:
    ingestion_rate_mb: 100
    ingestion_burst_size_mb: 200
    max_streams_per_user: 100_000
    max_chunks_per_query: 2_000_000
    retention_period: 720h  # 30d
```

Streams (label combos) are the cardinality unit in Loki, like series in Prometheus.

### 5.3 Tempo tenants

```yaml
overrides:
  payments:
    ingestion_rate_limit_bytes: 100_000_000  # 100 MB/s
    ingestion_burst_size_bytes: 200_000_000
    max_traces_per_user: 10_000_000
    max_search_bytes_per_trace: 1_000_000
```

### 5.4 The unified-config repo

Best practice: tenant configs in one Git repo, reviewed and applied via GitOps. Each PR adjusting limits is reviewed by the platform team. Limits never silently grow; cost trade-offs are explicit.

---

## 6. Tenant ID Propagation

The substrate.

### 6.1 The flow

```
Service emits signal (no tenant_id at SDK if instrumentation is generic)
   ↓
Agent enriches with tenant_id from k8s namespace / pod label / env var
   ↓
Gateway authenticates + stamps X-Scope-OrgID header
   ↓
Store ingests with tenant_id; reads honor it.
```

### 6.2 Source of truth

The `tenant_id` should be **derived from infrastructure**, not from the application:
- K8s namespace → tenant.
- Pod label `team=payments` → tenant.
- Env var `TENANT_ID=payments` → tenant.

Why: applications can be misconfigured; infrastructure is owned by the platform team. Trust the infrastructure.

### 6.3 The OTel Collector route

```yaml
processors:
  resource:
    attributes:
      - key: tenant
        from_attribute: k8s.namespace.name
        action: insert

exporters:
  otlphttp:
    endpoint: https://mimir:9009
    headers:
      X-Scope-OrgID: ${TENANT}
```

The collector reads the tenant from a resource attribute and stamps it as the request header.

### 6.4 Multi-tenant SDKs

Some application SDKs are multi-tenant *within* a process (e.g., a SaaS API server serving many customers). The application must propagate the customer tenant_id through traces and logs. This is application-layer concern, distinct from the platform tenant.

---

## 7. Quotas and Rate Limits

The protection layer.

### 7.1 The matrix

| Resource | Mimir | Loki | Tempo |
|---|---|---|---|
| Cardinality (series / streams) | `max_global_series_per_user` | `max_streams_per_user` | `max_traces_per_user` |
| Ingestion rate | `ingestion_rate` (samples/sec) | `ingestion_rate_mb` | `ingestion_rate_limit_bytes` |
| Burst size | `ingestion_burst_size` | `ingestion_burst_size_mb` | `ingestion_burst_size_bytes` |
| Query window | `max_query_length` | `max_query_length` | n/a |
| Lookback | `max_query_lookback` | `max_query_lookback` | n/a |
| Query memory | `max_samples_per_query` | `max_chunks_per_query` | `max_search_bytes_per_trace` |
| Retention | `compactor_blocks_retention_period` | `retention_period` | per-tenant retention |

Set every one of these per tenant. The defaults are usually too lax; tighten based on tenant tier.

### 7.2 Tier-based defaults

Most platforms have 2–3 tenant tiers:

```
Tier 1 (large, paying):  10M series, 1MB/s logs, 90d retention
Tier 2 (default team):   1M series,  100KB/s logs, 30d retention
Tier 3 (light usage):    100K series, 10KB/s logs, 7d retention
```

Tier assigned at tenant creation; can be upgraded with cost approval.

### 7.3 Burst vs steady

`burst_size` is the short-window allowance; `rate` is sustained. A tenant should be able to absorb a spike (deploy time, incident) without rate-limiting kicking in. Set burst ~2× steady.

### 7.4 Hard vs soft limits

- **Hard:** writes are rejected when limit hit. Used for cardinality, ingestion rate, retention.
- **Soft:** alerts fire at threshold; writes still accepted. Used for "approaching limit" warnings.

The soft alert at 80% of limit lets the team react before hard rejection. Always pair soft + hard.

### 7.5 What happens when limits are hit

The most important UX question. Three options:

1. **Reject silently.** Bad — team doesn't know data is being dropped.
2. **Reject with error in response.** Better — the agent sees the rejection and reports.
3. **Reject with metric + log.** Best — `cortex_distributor_ingester_append_failures_total{reason="..."}` increments; team can alert on it.

Most stacks support option 3. Configure team-side alerts on rejection metrics.

---

## 8. RBAC: Read Paths

Who can query which tenant's data.

### 8.1 The reading permission model

| Permission | Description |
|---|---|
| **Read own tenant** | Default; team sees its own data |
| **Read another tenant** | Cross-team queries; explicit grant |
| **Read all tenants** | Platform team / SRE; for cross-cutting ops queries |
| **Admin (limits, configs)** | Platform team only |

### 8.2 Implementation

- **Gateway with auth.** OAuth / OIDC / SSO; the gateway resolves user → tenants they can access; injects `X-Scope-OrgID` header.
- **Grafana datasource per tenant.** Each datasource pre-configured with a tenant ID; users see datasources they have permission for.
- **Service-account model.** For automation: service accounts have specific tenant grants.

The user never sees raw `X-Scope-OrgID` headers; the gateway / Grafana enforces it.

### 8.3 The "platform sees all" exception

The platform team needs cross-tenant visibility for ops queries ("which tenant is consuming the most cardinality?"). They have a privileged role that can read all tenants.

This privilege is *audited* — every cross-tenant read is logged. SREs reviewing for compliance can verify that platform team queries are operational, not exploratory access of customer data.

### 8.4 Sensitive-data tenants

Some tenants (security team, audit logs) shouldn't be readable by even the platform team without explicit need. These tenants have additional access controls — typically a different gateway path, manual approval for queries, audit logging at the query level.

---

## 9. Auth at the Write Path

Who can write to which tenant.

### 9.1 The threat model

Without write auth, *any service* can claim any tenant_id. A misconfigured service could pollute another tenant's data. Worse: malicious code could inject false metrics into the SLO of another team.

### 9.2 Implementation

- **Service accounts.** Each service has a credential; the credential maps to one (or a few) tenants.
- **Mutual TLS (mTLS).** Service identity proven by client cert; cert maps to tenant.
- **K8s ServiceAccount tokens.** The pod's identity is validated; tenant inferred from namespace / label.
- **Network policy.** Lower-level: only certain network paths can reach the gateway.

The gateway authenticates the writer, looks up their permitted tenants, and stamps the header. The store trusts only the gateway.

### 9.3 The infrastructure-trust line

The line between "trusted infra" and "untrusted apps" is critical:

```
Apps → agent → gateway (this line is the trust boundary) → store

Below the line: trusted, mTLS or internal network.
Above the line: app-supplied data; not trusted to set tenant.
```

Apps cannot set tenant; gateway derives it. Even a malicious or buggy app cannot pollute another tenant.

---

## 10. Noisy-Neighbor Protection

The structural protections that prevent one tenant's bad behavior from affecting others.

### 10.1 The threat list

| Threat | Mechanism |
|---|---|
| Runaway write rate | Per-tenant ingestion rate limit |
| Cardinality bomb | Per-tenant series limit |
| Long-running query | Query timeout + max samples limit |
| Memory-exhausting query | Query memory limit; query splitting |
| Storage-burning long retention | Per-tenant retention cap |
| Concurrent query flood | Per-tenant query concurrency limit |

Each one has a corresponding limit configuration.

### 10.2 The query frontend

Mimir / Cortex / Loki / Tempo all run a *query frontend* whose job is to:
- Queue queries per tenant.
- Split long queries into chunks.
- Cache results.
- Apply per-tenant concurrency limits.

The query frontend is the multi-tenant safety mechanism for reads. Without it, a tenant's bad query can starve queriers of CPU/memory and degrade other tenants' queries.

### 10.3 Per-tenant query queues

```yaml
query_scheduler:
  max_outstanding_per_tenant: 100
```

Tenant A's queue full → Tenant A's queries queue/reject; Tenant B's queries unaffected. *Different* from "max queries cluster-wide" — that has cross-tenant impact.

### 10.4 The query memory bomb

A query like `topk(10000, count by (label) ({__name__=~".+"}))` can use unbounded memory. Defenses:
- `max_samples_per_query`: hard cap.
- Query memory tracking; reject queries that exceed their tenant's memory budget.
- Streaming evaluation (Mimir's query engine in 2026 supports streaming for some operators).

### 10.5 Circuit breakers

When a tenant repeatedly causes problems (10 query timeouts in 5 minutes), the platform can temporarily *circuit-break* that tenant: reject queries with an error explaining the breaker, page the team. This protects the cluster while alerting the offender.

---

## 11. Per-Tenant Billing Attribution

The cost layer.

### 11.1 The signals

- **Active series count per tenant.**
- **Ingestion rate per tenant.**
- **Storage consumed per tenant.**
- **Query QPS / query memory per tenant.**

These are the inputs to a cost model. Convert to dollars (using internal allocation rates):

```
cost_per_tenant = (active_series × series_cost_rate)
                + (storage_GB × storage_cost_rate)
                + (queries × query_cost_rate)
```

### 11.2 The dashboard

Per-tenant cost dashboard, refreshed daily:

```
payments-team — Q3 spend so far: $11,200

Metrics: $5,400 (47%)
  - 6.2M active series @ $0.78/series/quarter
Logs:    $4,200 (38%)
  - 850 GB ingested
Traces:  $1,200 (10%)
  - 8M spans/day, 5% kept
Profiles: $400 (4%)

Trend: +8% vs Q2 (driven by new pricing-svc)
Top opportunity: Drop `customer_id` from 2 metrics → -$1,400/qtr
```

Showback / chargeback flow as in `doc 18 §12`. The infrastructure here makes the numbers possible.

### 11.3 Internal cost rates

The platform team publishes rates ($/series/month, $/GB/month, $/query). Teams can predict cost from their usage. Rates are reviewed annually as infrastructure costs change.

---

## 12. Cross-Tenant Queries

Sometimes users need to query across tenants. Two cases:

### 12.1 Platform / SRE: cross-cutting analytics

"What's the total active series across all tenants?" "Which tenant has the most cardinality growth?" These are *operational* queries by the platform team.

Implementation: privileged role; queries with a wildcard tenant; audited.

### 12.2 Org-wide journey: "checkout journey across teams"

The checkout journey crosses payments, identity, search. The journey-level dashboard needs data from all three.

Two implementations:
1. **Aggregate at write time:** journey-level metrics emitted to a *separate* "journeys" tenant that all teams can read.
2. **Federated read:** the dashboard fans out reads to multiple tenants, joins client-side.

Option 1 is simpler operationally; option 2 is more flexible. Most production stacks use a hybrid (aggregate where possible; federate for ad-hoc).

---

## 13. Tenant Lifecycle: Creation, Migration, Deletion

The procedural layer.

### 13.1 Creation

1. Team requests tenancy (often via IDP / Backstage).
2. Platform creates: tenant ID, default tier limits, RBAC, datasource in Grafana, service-account credentials.
3. Tenant entry in central catalog.
4. Onboarding doc shared.

A self-service flow: a Backstage form asks for team name, tier, contact, expected scale; submits PR to the tenant repo; CI applies via GitOps.

### 13.2 Migration

Sometimes tenants must move (cluster upgrade, capacity rebalance, regulated re-zoning).

- **Backfill window.** New cluster set up; old cluster keeps writing to both for 7-30 days.
- **Cutover.** Reads redirected to new cluster.
- **Verification.** Old data accessible during overlap; queries validated.
- **Deprecation.** Old cluster retired after retention window.

This is the same pattern as DB migration; observability isn't special.

### 13.3 Deletion

Tenant deletion is *the* GDPR-related operation. When a customer leaves a B2B SaaS, their telemetry must be deletable.

- Identify all data tagged with the tenant.
- Delete from hot store, warm, cold.
- Verify with audit query.
- Document deletion (compliance evidence).

For internal teams, deletion is easier — reorgs happen; the tenant fades. Plan a 90-day retention after deletion before fully purging, in case the team is reformed.

---

## 14. Tenant SLOs

Each tenant has its own SLOs *for the platform's service to them*.

### 14.1 Platform-side SLIs

The platform team owns these per tenant:

- **Ingest availability:** % of writes accepted in the last hour.
- **Query availability:** % of queries returning successfully.
- **Query latency:** P99 query time.
- **Tenant isolation:** is this tenant's QoS affected by neighbors? (measured via synthetic queries from this tenant)

### 14.2 Per-tier SLOs

```
Tier 1: 99.95% ingest availability, p99 query < 5s, 99.9% query availability
Tier 2: 99.9%  ingest, p99 query < 10s, 99.5% query availability
Tier 3: 99.5%  ingest, best-effort query, 99% query availability
```

Each tier is a paid tier (literally paid, in chargeback orgs; figuratively in showback). Higher tier = better SLOs; higher cost.

### 14.3 Per-tenant dashboards

Each tenant has a dashboard showing *their* observability platform health:
- Recent ingestion rate (vs limit).
- Active series (vs limit).
- Query QPS, query latency.
- Error rate of writes.
- Recent rejections.

This is the platform team's *product* — visible quality of service to each consumer.

---

## 15. Anti-Patterns

1. **No tenant_id on signals.** Multi-tenancy is impossible without it.
2. **Tenant_id set by the application.** Spoofable; should come from infrastructure.
3. **One huge tenant for everything.** Defeats the purpose; quotas can't be enforced.
4. **No per-tenant quotas.** Noisy neighbor crashes the cluster.
5. **Same retention for all tenants.** Cost untenable at scale.
6. **No RBAC.** Any team reads any team's data; compliance fail.
7. **No write auth.** Any service can pollute any tenant.
8. **No platform-side audit.** Cross-tenant reads invisible.
9. **No cost attribution.** Teams have no incentive to optimize.
10. **No tenant SLOs.** Platform team has no measurable promise.
11. **Physical isolation everywhere.** N× operational cost; usually unjustified.
12. **Cross-tenant queries via "just remove the filter".** Compliance violation; data leakage.
13. **No tenant deletion procedure.** GDPR compliance fails.
14. **Limits set once, never reviewed.** Drift; tenants outgrow limits silently.
15. **No noisy-neighbor circuit breaker.** Bad tenants take down others repeatedly.

---

## 16. Worked Example: 50-Team Mimir + Loki Deployment

Real-shape example.

### 16.1 The setup

- 50 internal teams as logical tenants.
- 5 tenants are tier-1 (high-traffic services); 30 are tier-2; 15 are tier-3.
- Mimir cluster: 12 ingesters, 6 queriers, 3 query frontends.
- Loki cluster: 8 ingesters, 4 queriers.
- Total active series: 80M.
- Total log volume: 4 TB/day.

### 16.2 Tenant config

Tier-1 (5 tenants):
- 10M series each
- 500 KB/s logs
- 90d retention
- p99 query < 5s SLO

Tier-2 (30 tenants):
- 1M series each
- 50 KB/s logs
- 30d retention
- p99 query < 10s SLO

Tier-3 (15 tenants):
- 100K series each
- 5 KB/s logs
- 7d retention
- best-effort query

### 16.3 Per-tenant access

- Each team has a Grafana datasource preconfigured with their tenant ID.
- 5 platform engineers have a privileged datasource that sees all tenants.
- Service accounts per service map to tenants via k8s namespace.

### 16.4 Operational metrics

Platform team's own dashboards:
- Per-tenant ingest rate vs limit.
- Per-tenant active series vs limit.
- Top-10 tenants by query QPS.
- Tenants approaching cardinality limit (alerted at 80%).
- Cross-tenant audit log.

### 16.5 Outcomes

- Each tenant runs its own observability without seeing others.
- Quota system catches cardinality bombs before they reach memory wall.
- Noisy-neighbor incidents: 2 in last year (both circuit-broken automatically).
- Per-tenant cost reporting accurate to ±5%.
- Grafana usage: each team uses their own dashboards; cross-team via journey aggregations.

The 50-tenant cluster operates with 1 platform engineer, ~30% of their time. Without multi-tenancy, this would be 50 separate clusters and 5+ engineers.

---

## 17. Pitfalls

1. **No tenant_id at all.** Restart from `doc 19 §6`.
2. **Tenant set by application.** Trust boundary wrong.
3. **Quotas not configured.** First runaway tenant takes everyone down.
4. **No RBAC enforcement.** Compliance violation.
5. **No audit on cross-tenant reads.** Privacy issue.
6. **Same retention all tenants.** Cost unbounded.
7. **No tier system.** All tenants are equal; pricing structure missing.
8. **No per-tenant SLOs.** Platform team's quality invisible.
9. **No isolation testing.** Quotas not actually verified.
10. **No tenant lifecycle docs.** Creation / deletion ad-hoc.
11. **Federation without auth.** Cross-cluster queries skip permission check.
12. **No noisy-neighbor circuit breaker.** Repeated bad tenants degrade the cluster.
13. **Per-tenant config drift.** Some tenants over-limited, others under.
14. **No cost rates published.** Teams can't predict their bill.
15. **Hard tenant migrations.** No backfill window; data loss possible.

---

## 18. Mental Models

> **Tenant_id is the substrate. Without it, none of multi-tenancy works.**

> **Logical isolation is the default; physical for compliance / large customers only.**

> **Set every quota, per tenant, per signal. Defaults are too lax.**

> **Auth at the write path. Gateway derives tenant; apps cannot set it.**

> **Noisy-neighbor protection: per-tenant rate, query, and memory limits. Without these, one bad tenant takes everyone down.**

> **RBAC at read; auth at write; audit cross-tenant access.**

> **Tier the platform; tier the cost. Tenants pay for their tier.**

> **Per-tenant SLOs make the platform a measurable product.**

> **Tenant lifecycle: create, migrate, delete. All three are platform-team APIs.**

> **The platform team's product is the tenant's observability. Treat it as such.**

Now go to `doc 20` (AIOps and frontier) — the 2026 frontier of anomaly detection, alert grouping, and LLM-assisted incident response.
