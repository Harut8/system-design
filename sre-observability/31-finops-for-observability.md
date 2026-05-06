# 31 — FinOps for Observability

> Observability is one of the top-three line items in most engineering budgets. Datadog at $5M/year is normal at scale. Self-hosted at $2M-$10M including engineer time. The platform team that doesn't manage cost as a first-class concern will find their budget cut, their tools changed, and their roadmap dictated by finance.

This chapter is about the discipline that turns observability from a black-box cost center into a forecastable, allocatable, optimizable engineering product.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [The four cost questions](#2-four-questions)
3. [Allocation: who pays for what](#3-allocation)
4. [The chargeback / showback ladder](#4-chargeback-ladder)
5. [The unit-economics model](#5-unit-economics)
6. [Forecasting observability cost](#6-forecasting)
7. [Vendor cost levers](#7-vendor-levers)
8. [Self-hosted cost levers](#8-self-hosted-levers)
9. [The "what should we delete?" routine](#9-what-to-delete)
10. [Reservations, commitments, spot](#10-reservations)
11. [The cross-team negotiation](#11-cross-team)
12. [FinOps governance](#12-governance)
13. [Anti-patterns](#13-anti-patterns)
14. [Worked example: a $2M platform reduced to $800K](#14-worked-example)
15. [Pitfalls](#15-pitfalls)
16. [Mental models](#16-mental-models)

---

## 1. Thesis

Three claims:

1. **Observability cost is a function of choices, not infrastructure.** Cardinality, sampling, retention, signal sources — all are policy decisions. Manage the policies; the bill follows.
2. **Allocation is the lever that drives behavior.** When teams see "your service costs $14k/month to observe," they self-regulate. Without allocation, cost grows monotonically.
3. **The platform team is the FinOps practitioner.** Service teams optimize their services; the platform team optimizes the cost-allocation system, the cost forecasts, the contracts. Both layers required.

If your team can't answer "how much did this team cost in observability last quarter?" — your cost story is broken, regardless of what the bill says.

---

## 2. The Four Cost Questions

The platform team must answer these:

1. **What did we spend?** (history)
2. **Why did we spend it?** (allocation)
3. **What will we spend?** (forecast)
4. **What can we save?** (opportunity)

A FinOps practice answers all four monthly.

---

## 3. Allocation: Who Pays for What

The fundamental layer.

### 3.1 The dimensions

- Per service.
- Per team.
- Per business unit / cost center.
- Per customer (B2B SaaS only).
- Per signal type.
- Per environment (prod, staging, dev).

### 3.2 The substrate

Allocation requires *tagging at the source*. Every signal must carry a tag identifying its owner.

```yaml
# k8s namespace label
team: payments
cost_center: department-201
```

Mesh injects labels via OTel collector resource attribution. Per-tenant in Mimir/Loki/Tempo (`doc 19`). Without these tags, allocation is guesswork.

### 3.3 The cost-engine

Daily aggregation:

```
For each (team, signal, tier):
  cost = (active_series × series_cost_rate)
       + (storage_GB × storage_cost_rate)
       + (queries × query_cost_rate)
       + (ingest_GB × ingest_cost_rate)
```

This produces a cost-attribution data set: per (team, day, dimensions), $.

### 3.4 The dashboards

Per-team:
```
payments-team — Q3 spend: $34,200

By signal:
  Metrics: $14,800 (43%)
  Logs:    $11,700 (34%)
  Traces:  $5,200 (15%)
  RUM:     $1,800 (5%)
  Profiles: $700 (2%)

Trend: +6% MoM
Top growth: trace volume from new pricing-svc
```

Cross-team:
```
Top spenders this quarter:
  data-pipeline   $58,200   (+15%)
  search          $44,100   (+5%)
  payments        $34,200   (+6%)
  ...
```

The visibility drives behavior.

---

## 4. The Chargeback / Showback Ladder

The progression.

### 4.1 The four levels

| Level | What it is | When |
|---|---|---|
| **Invisible** | No allocation; one bill | Day 1 |
| **Showback** | Visibility only; no charging | Most orgs at ~50 services |
| **Chargeback** | Internal billing; team budgets debited | Mature orgs at scale |
| **Pricing-tier** | Teams choose their tier; quality + cost trade explicit | Frontier orgs |

### 4.2 The order matters

Showback first; chargeback after. Skipping showback to chargeback creates:
- Gaming (teams hide their costs).
- Fighting (teams contest allocations).
- Friction (teams can't predict their bill).

A year of showback first builds the trust and process for chargeback to work.

### 4.3 The "show me the bill" effect

Concrete data: when teams see allocation, average savings of 20-30% in the first quarter.

Mechanism: teams self-audit, find unused metrics, sample harder, drop labels. The platform team didn't have to negotiate; the team saw their own bill.

### 4.4 The cross-functional negotiation

Chargeback requires:
- Finance buy-in (allocation methodology).
- Leadership commitment (teams must own their budgets).
- Engineer buy-in (process not adversarial).
- Tooling (the dashboard / report).

A year of relationship-building. Not a quarterly project.

---

## 5. The Unit-Economics Model

The deeper analysis.

### 5.1 Cost per request / per user / per session

```
$ per request = total_observability_cost / total_requests
```

Per service:
```
checkout-svc:    $0.0008 / request
search-svc:      $0.0001 / request
data-pipeline:   $0.012  / request   ← outlier; investigate
```

The outliers are the targets.

### 5.2 Cost per business unit

For B2B: $ per customer, $ per ARR-dollar.

```
Tier-1 customer:  $80 / month observability cost / customer
Tier-2 customer:  $20 / month
Tier-3 customer:  $4  / month
```

If observability cost per customer exceeds 10% of revenue per customer, that's an unhealthy ratio. Drives architectural / pricing changes.

### 5.3 The cost-vs-traffic ratio

```
observability_cost / total_revenue
or
observability_cost / total_compute_cost
```

Industry benchmark (rough): observability is 10-20% of total infra cost. Above that = over-instrumented or bad architecture.

### 5.4 The trend signal

Unit economics over time:
- Per-request cost trending up: cardinality / retention growing faster than traffic.
- Per-request cost trending down: efficiency improving.

Track quarterly. Action plans on regressions.

---

## 6. Forecasting Observability Cost

The 12-month projection.

### 6.1 The drivers

- Service count growth.
- Cardinality per service growth.
- Retention extensions.
- New signals (RUM, profiles, LLM).
- Vendor pricing changes.
- Org headcount → traffic correlation.

### 6.2 The model

Linear isn't enough (`doc 16 §4`). Use compound:

```
forecast(t) = baseline × (1 + growth_per_period)^t
            + step_changes_at_known_dates
            + seasonal_adjustments
```

Step changes: known new initiatives (new product line, new customer segment).

### 6.3 The "10× scenario" stress test

What if traffic 10×s? What if cardinality 5×s? What if retention doubles?

The forecast envelope (`doc 16 §4.5`) makes the upper bound visible. Plan capacity for the upper bound; budget for the expected.

### 6.4 The vendor-renewal lookahead

Vendor contracts negotiated 3-6 months in advance. The forecast at renewal time = the negotiation input. Without it: surprise cost; weak negotiating position.

---

## 7. Vendor Cost Levers

What you can change in vendor relationships.

### 7.1 Volume tiers

Most vendors offer volume discounts at scale. Negotiate:
- $/host: tiered.
- $/GB ingest: tiered.
- $/series: tiered.

### 7.2 Multi-year commitments

Annual or multi-year contracts: 20-40% discount. Trade-off: less flexibility.

### 7.3 Per-feature pricing

A la carte: pay for traces only, metrics only. Often cheaper than the full bundle.

### 7.4 Excess-overage rates

The "you exceeded your contracted volume" penalty. Often 2-3× normal. Negotiate the cap or extend the contract.

### 7.5 The competitive bid

Datadog → Splunk → New Relic. Get bids; share with your incumbent. Often unlocks pricing the rep "couldn't" offer otherwise.

### 7.6 The migration threat

A credible plan to migrate (e.g., to Mimir self-hosted) is the strongest leverage. Vendors discount aggressively when faced with a real exit.

---

## 8. Self-Hosted Cost Levers

What you can change in your stack.

### 8.1 Compute right-sizing

Already-mentioned `doc 16 §11.4`. Quarterly. Typically 15-30% savings.

### 8.2 Storage tiering

Hot SSD → warm HDD → cold object → archive. Each tier 5-10× cheaper than the one above. Move data aggressively.

### 8.3 Compression upgrades

Zstd > snappy > gzip. Native histograms in Prom 2.40+. Each transition is ~10-30% storage saving.

### 8.4 Cardinality reduction

`doc 18`. The largest lever. 10× possible.

### 8.5 Sampling tightening

Trace sampling 5% → 2%. Log INFO sampling 100% → 10%. Massive savings if not yet applied.

### 8.6 Retention shortening

7 days → 3 days for non-critical. Often drops storage 60%.

### 8.7 Workload migration

A common pattern: high-cost workloads on vendor (Datadog) migrated to self-hosted (Mimir + Loki + Tempo). 5-10× cost reduction.

### 8.8 Spot / preemptible for non-critical

Querier replicas can run on spot. Compactor too (with retry). Saves 50-70%.

---

## 9. The "What Should We Delete?" Routine

The quarterly hygiene cycle (revisit from `doc 18 §13`).

### 9.1 The audit list

- Metrics not referenced by any dashboard or alert.
- Alerts that haven't fired in 90 days.
- Dashboards not viewed in 90 days.
- Logs from services no longer running.
- Traces with sampling rules nobody owns.
- Old retention rules unchanged for years.

### 9.2 The deletion PR

One PR per team per quarter; deletions reviewed; merged. Often hundreds of lines removed.

### 9.3 The savings tracking

Per quarter, track:
- $ saved by deletions.
- $ avoided by capacity tuning.
- $ unlocked by vendor renegotiation.

These numbers go to leadership. They justify the FinOps role / time investment.

---

## 10. Reservations, Commitments, Spot

The cloud-FinOps levers, applied to observability.

### 10.1 Reserved instances / committed-use

For self-hosted: 1-3 year commitments on EC2 / GCE → 30-60% discount. Steady-state observability infrastructure is a great fit (predictable load, long-running).

### 10.2 Savings plans / flexible reservations

AWS Savings Plans / GCP CUDs cover compute across instance types. Lower commitment risk; lower discount.

### 10.3 Spot

For interruptible workloads: queriers, compactors. ~70% discount. Trade-off: occasional preemption; design for resilience.

### 10.4 The "buy in advance" calculation

If you'll definitely use 100 cores for the next year: reserved is 50% cheaper than on-demand. Net savings: tens to hundreds of $K depending on scale.

The math: forecast confidence × discount = expected savings. Don't over-commit; do commit for steady state.

### 10.5 Object storage tiering

S3 Standard → S3 Standard-IA (infrequent access) → Glacier. Each step ~50% cheaper. Lifecycle policies move data automatically.

For observability: most data > 30 days old can move to IA without query-pattern impact.

---

## 11. The Cross-Team Negotiation

When team budget conflicts with platform constraints.

### 11.1 The "we need more cardinality" ask

Service team: "we need to add `customer_id` as a label."

Platform team's response process:
1. Quantify cardinality impact.
2. Quantify cost impact.
3. Offer alternatives (logs, traces, top-K).
4. If team still wants the label: budget approval for additional cost.

This conversation is collaborative, not adversarial. Platform team explains; service team decides; cost flows.

### 11.2 The "we need longer retention" ask

Per-tenant retention extensions. Cost is roughly linear in retention. The team requesting it pays.

### 11.3 The "we need a different vendor" ask

A team wants Honeycomb (different from the org's Datadog). The platform team:
- Options: integrate Honeycomb alongside (cost added); migrate fully (org-wide change); no.
- Decision based on impact, scale, alternatives.

### 11.4 The escalation path

When teams disagree, escalate to engineering leadership with both options and costs. Decision is theirs; data is platform-team's contribution.

---

## 12. FinOps Governance

The institutional structure.

### 12.1 The FinOps role

A platform engineer (or a small team) owns:
- The cost dashboards.
- The forecast.
- The vendor relationships.
- The hygiene cycle.
- The cross-team negotiations.

In small orgs: a fraction of one engineer. In large orgs: a full team.

### 12.2 The quarterly review

Every quarter, with engineering leadership and finance:
- Last quarter spend vs forecast.
- Top growth drivers.
- Top efficiency wins.
- Next quarter forecast.
- Vendor commitments.

### 12.3 The annual budget cycle

Annually:
- 12-month forecast.
- Vendor renewals.
- Architecture investments (e.g., migration projects).
- Cost-reduction targets.

### 12.4 The cost-as-a-feature

Modern observability platforms increasingly expose cost-related signals as first-class:
- Per-team dashboards.
- Per-query cost previews.
- Cost alerts when teams approach budgets.

The "your $14k/month bill" message at PR time changes engineer behavior more than any post-facto report.

---

## 13. Anti-Patterns

1. **No allocation.** Cost grows monotonically; no incentive to optimize.
2. **No tagging at source.** Allocation impossible.
3. **Chargeback before showback.** Gaming and fighting.
4. **No forecast.** Surprise; weak negotiation.
5. **Vendor lock-in surprise.** Renewal increase shocks.
6. **No quarterly hygiene.** Artifacts accumulate.
7. **No right-sizing.** Compute over-paid.
8. **No tiering.** Hot prices for stale data.
9. **No reserved instances.** Predictable load on on-demand pricing.
10. **No vendor competitive bid.** Discount left on table.
11. **No cost dashboards per team.** Visibility absent.
12. **No FinOps role.** Optimization happens by accident.
13. **No vendor renewal calendar.** Reactive negotiation.
14. **No unit economics.** Per-customer cost invisible.
15. **No cost discussion at design time.** Architecture choices made cost-blind.

---

## 14. Worked Example: A $2M Platform Reduced to $800K

The story.

### 14.1 Starting state

- $2M / year on Datadog.
- 200 services.
- 12M active series; 4 TB logs/day; 100% trace sampling.
- No per-team allocation.
- Annual contract auto-renewing.

### 14.2 The audit

Three months of work:
1. Implement allocation tags everywhere.
2. Build cost-attribution dashboard.
3. Compute per-team spend.
4. Identify outliers (one team consumed 40% of budget).

### 14.3 The actions

**Cardinality reduction:**
- Drop `customer_id` from 5 metrics. 10× cardinality reduction.
- Move user-attribution to logs / traces.

**Sampling:**
- Tail-sample traces: 100% → 5% kept.
- INFO logs sample: 100% → 10%.

**Retention:**
- Default retention 30d → 14d.
- Audit logs unchanged (compliance).

**Vendor:**
- Negotiated 3-year deal: 25% discount.
- Migrated some workloads to self-hosted Mimir for ~70% cost reduction on those.

**Hygiene:**
- Quarterly delete cycle established.
- Annual review.

### 14.4 The outcome

- Year 1: $2M → $1.4M (-30%).
- Year 2: $1.4M → $1.0M (-30%).
- Year 3: $1.0M → $800K (-20%).

Three-year reduction: 60%. Net change: $1.2M/year saved.

Investment: 1 engineer at 50% allocation for 6 months (one-time), 20% steady state.

### 14.5 The lessons

- Most savings came from cardinality + sampling (the cheapest interventions).
- Vendor negotiation contributed but wasn't the largest lever.
- Showback drove behavior change in service teams.
- Quarterly hygiene catches drift; annual review catches strategy gaps.

---

## 15. Pitfalls

1. **No source tagging.** Allocation impossible.
2. **No showback.** Teams uninformed.
3. **Chargeback rushed.** Friction.
4. **No forecast.** Surprises.
5. **No vendor calendar.** Reactive renewal.
6. **No right-sizing.** Compute over-paid.
7. **No tiering.** Storage over-paid.
8. **No reserved instances.** Spot opportunity missed.
9. **No competitive bid.** Discount missed.
10. **No quarterly hygiene.** Drift.
11. **No FinOps role.** Optimization sporadic.
12. **No unit economics.** Per-customer cost unknown.
13. **No cost-aware design reviews.** Architecture cost-blind.
14. **No annual review.** Strategy drift.
15. **Gaming via tag manipulation.** Allocation undermined.

---

## 16. Mental Models

> **Cost is a policy, not infrastructure. Cardinality, sampling, retention — choices.**

> **Allocate first; charge later. Showback for a year before chargeback.**

> **The visibility itself drives behavior. 20-30% reduction in the first quarter is normal.**

> **Forecast 12 months. Vendor renewals depend on it.**

> **Every architecture decision has a cost dimension. Surface it at design time.**

> **Per-team dashboards. Per-customer dashboards if B2B.**

> **Quarterly hygiene + annual review. Drift is the enemy.**

> **Vendor negotiation is most powerful with a credible alternative.**

> **Reserved + spot for the predictable + interruptible. 30-70% discount.**

> **Cost-as-a-feature: surface bills at PR time. Engineer behavior follows.**

Now go to `doc 32` (compliance and privacy) — the legal/regulatory layer that bounds what telemetry can do.
