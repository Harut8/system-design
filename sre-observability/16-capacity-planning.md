# 16 — Capacity Planning

> Capacity planning is the discipline that turns reliability from firefighting into forecasting. SREs that don't forecast end up firefighting capacity. The capacity-planning loop is the upstream feed that prevents `doc 15` from being your weekly reading.

This chapter assumes the queueing math from `doc 00 §16` (Little's law, M/M/1 saturation) and the SLO machinery from `doc 13`. Where SLOs tell you *if you're failing*, capacity planning tells you *when you will*.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [The capacity loop](#2-loop)
3. [Headroom: the only metric that matters](#3-headroom)
4. [Forecasting models](#4-forecasting)
5. [USE-driven capacity (the per-resource walk)](#5-use-driven)
6. [Queue theory in production](#6-queue-theory)
7. [Load testing: closed-loop, open-loop, ramp](#7-load-testing)
8. [Provisioning patterns: vertical, horizontal, autoscaling, cells](#8-provisioning)
9. [The capacity planning artifact](#9-artifact)
10. [Multi-region capacity](#10-multi-region)
11. [Cost-bounded capacity](#11-cost-bounded)
12. [Capacity SLIs and platform SLOs](#12-capacity-slos)
13. [The chicken-and-egg of new services](#13-new-services)
14. [Worked example: a 12-month checkout forecast](#14-worked-example)
15. [Pitfalls](#15-pitfalls)
16. [Mental models](#16-mental-models)

---

## 1. Thesis

Three claims:

1. **Headroom is a leading indicator; latency and errors are lagging.** A capacity-planned org sees the iceberg coming; a capacity-reactive org hits it.
2. **Capacity planning is forecasting + a buffer.** Not "we have N CPUs"; "we have headroom for 6 months at projected growth, with a 50% buffer for surprise."
3. **The forecast is wrong.** All forecasts are wrong. The point is *bounded* wrong — calibrated to known uncertainty — and revisited often. A monthly capacity review with a forecast you constantly update beats a perfect annual one.

If your team only knows it's out of capacity when latency burns the SLO, you don't have capacity planning. You have capacity *fire suppression*. This chapter is the upstream practice.

---

## 2. The Capacity Loop

```
   ┌──────────────┐
   │  measure     │  current usage by resource per service
   │  current     │  (CPU %, mem RSS, queue depth, p99 latency,
   │  usage       │  conn pool wait, disk IOPS, etc.)
   └──────┬───────┘
          │
          ▼
   ┌──────────────┐
   │  forecast    │  project usage forward N months at expected growth
   │  growth      │  (linear, seasonal, business-driven)
   └──────┬───────┘
          │
          ▼
   ┌──────────────┐
   │  determine   │  required capacity = forecast × buffer factor (1.5-2×)
   │  required    │
   │  capacity    │
   └──────┬───────┘
          │
          ▼
   ┌──────────────┐
   │  procure /   │  scale-out, larger instances, new region, new cells
   │  provision   │  (procurement lead time matters!)
   └──────┬───────┘
          │
          ▼
   ┌──────────────┐
   │  validate    │  load test that capacity actually delivers
   │  via load    │  (claimed != actual, especially with autoscaling)
   │  test        │
   └──────┬───────┘
          │
          ▼
   ┌──────────────┐
   │  re-measure  │  track forecast accuracy; adjust models
   └──────┬───────┘
          │
          └─────────► back to top, monthly cadence
```

The loop runs forever. Skipping any step means the next iteration is reactive.

---

## 3. Headroom: The Only Metric That Matters

**Headroom = (current capacity − current usage) / current capacity.**

If your service runs at 60% CPU, headroom is 40%. If queue depth is 200 of a 1000-slot queue, headroom is 80%.

### 3.1 Why headroom, not utilization

Utilization is the present tense. Headroom is the *forward-looking* form: *how much margin do I have before saturation kills me?*

The queueing math (`doc 00 §16.4`) shows utilization > 80% causes geometric latency growth. So the operational target is **≥ 30% headroom** at peak — meaning peak utilization ≤ 70%. Below 30% headroom, you're in the geometric-degradation zone; one bad day breaks SLOs.

### 3.2 Headroom by resource

Headroom is per-resource, not per-service. A service can have 80% CPU headroom and 5% connection-pool headroom — and the latter will kill it. The capacity dashboard shows headroom per (service × resource) cell.

```
                  CPU    MEM    CONN POOL    QUEUE   IOPS
checkout-svc      45%    62%    15%          80%     70%
auth-svc          80%    60%    35%          90%     80%
order-svc         30%    25%    65%          70%     60%   ← red on CPU+MEM
```

The `order-svc` here has a capacity emergency. Two resources below 30% headroom; scale-out is needed *now*, not next quarter.

### 3.3 The 30/50/70 rule

A practical heuristic:

- **≥ 70% headroom**: comfortable. New features fine.
- **50–70% headroom**: monitor monthly; plan scaling for next quarter.
- **30–50% headroom**: scaling action this quarter; alerts to capacity team.
- **< 30% headroom**: capacity emergency; immediate action; potentially freeze risky deploys.

These thresholds are *resource-by-resource*, not aggregate. Any one resource below 30% is the emergency.

---

## 4. Forecasting Models

The science of "how much will we need?"

### 4.1 Linear

```
usage_at(t) = baseline + growth_rate × t
```

Simple. Wrong, but a useful default. Works when:
- Traffic grows steadily with linear customer acquisition.
- Service fan-out is constant.
- No seasonality.

Fails when traffic has spikes, holidays, viral moments. The default is "good enough for 80% of services for next 6 months." Use as the baseline; layer corrections on top.

### 4.2 Seasonal

Decompose into trend + season + residual (statsmodels: STL decomposition; or just visual inspection).

```
usage_at(t) = trend(t) + season(t mod period) + ε
```

Standard for:
- E-commerce (Black Friday, Cyber Monday, holidays)
- Media (sports playoffs, election nights, premieres)
- Public sector (April tax deadlines)

The hard part is *the period*. Daily, weekly, monthly, quarterly, annual all have different signals. Tools: Prophet, statsmodels, internal forecasting libraries. Most observability vendors offer some seasonal forecasting in dashboards (Datadog, Honeycomb).

### 4.3 Business-driven

Sometimes the forecast comes from product/sales: "we're acquiring 5,000 enterprise customers next quarter, each at ~10× current per-user load."

Capacity planning takes the business plan as an *input*, not an output. The question becomes: *given this business plan, what capacity do we need?* The math is straightforward; the input is what's hard.

### 4.4 Compound growth

Many services grow by feature usage *and* user count *and* per-user activity. The compound:

```
usage = users × features_per_user × activity_per_feature
        × concurrent_factor × tail_factor
```

Each factor has its own growth rate. Compounding 3% monthly user growth × 5% monthly feature growth = ~8% monthly compound, ~150% annual. Linear forecasts massively under-estimate.

### 4.5 The forecast envelope

Always express forecasts as *ranges*, not points:

```
6-month projected usage:
  Best case:    +15%
  Expected:     +35%
  Stretch:      +60%
  Black-swan:   +120%
```

Plan for the *expected* case; have a buffer that covers *stretch*; have a contingency plan for *black-swan*. A point estimate is a lie.

### 4.6 The "wait and see" anti-pattern

Many teams under-forecast and *plan to scale reactively*. This works only if:
- Provisioning is < 1 day (autoscaling, cloud).
- Load tests confirm scaling actually delivers.
- The cost of being wrong is small.

For self-managed infra, multi-region, or hardware-bound (GPU, TPU), reactive doesn't work — lead times are weeks to months. Plan ahead.

---

## 5. USE-Driven Capacity

The per-resource walk. For *every* resource, ask: utilization, saturation, errors. Headroom is the gap to capacity.

### 5.1 The per-service capacity matrix

```
service: checkout-svc
─────────────────────────────────────────────────────────────────────
RESOURCE        UTILIZATION   SATURATION   ERRORS   HEADROOM   FORECAST
CPU (cores)     45%           load_avg=2.1 0        55%        +30% in 6m
Memory          62%           swap_in=0    OOM=0    38%        +20%
Connection pool 35/200 used   wait_ms=0.5  0        82%        +50%
Goroutines      12k           sched_lat=µs 0        OK         +25%
Queue depth     150/1000      0            0        85%        +30%
DB connections  8/20          wait_ms=2    0        60%        +50%
Disk IOPS       400/1000      0            0        60%        +30%
File descriptors 200/65k      0            0        99%        OK
─────────────────────────────────────────────────────────────────────
```

Read top to bottom. The lowest headroom in the column is the *bottleneck*. The forecast column projects out — even if today's headroom is 38%, if growth is +50%, you're at 0 headroom in 6 months.

### 5.2 The bottleneck migrates

A common pattern: scale CPU (the obvious bottleneck), and now memory becomes the bottleneck. Scale memory; now connection-pool saturates. Scale pool; now DB connections saturate. Scale DB; now disk IOPS.

**The bottleneck always migrates.** Capacity planning is the discipline of identifying which one is *next*, not just the current one. The matrix above shows you all of them at once.

### 5.3 USE for non-obvious resources

Easy resources (CPU, mem, disk) get attention. Sneaky resources (often the actual cause of outages):

- **Connection pools.** The DB driver might have a 10-conn pool that's totally invisible to ops dashboards.
- **Thread pools / goroutine schedulers.** Scheduling latency under load.
- **Kernel structures.** Conntrack table, file descriptors, ephemeral ports, socket buffers.
- **Object pools** in languages like Java (heap, PermGen) or Go (sync.Pool).
- **Cache warm state.** A cache "with capacity X" often serves at full utility only when warm; cold, it's effectively saturated.
- **Quotas at downstream services.** S3 list-bucket QPS, rate limits on third-party APIs.

USE-walking these every quarter catches surprises. `node_exporter` covers the kernel side; instrumentation in app code covers the rest.

---

## 6. Queue Theory in Production

The math that explains *why* utilization > 80% breaks things.

### 6.1 Little's law (revisited)

```
L = λ × W

L = average concurrent items
λ = arrival rate
W = average time in system
```

Practical use: instrument *all three*. Verify that `requests in flight × service time = throughput × latency`. When the equation breaks, you have a measurement bug.

### 6.2 M/M/1 wait time formula

```
W = (1 / μ) × ρ / (1 - ρ)

ρ = utilization (0 to 1)
μ = service rate
```

The fundamental capacity equation:

| Utilization ρ | Wait time multiplier |
|---|---|
| 0.50 | 1.0× |
| 0.70 | 2.3× |
| 0.80 | 4.0× |
| 0.90 | 9.0× |
| 0.95 | 19× |
| 0.99 | 99× |

*A service running at 99% CPU has 99× the queueing latency of one at 50%.* This is why "run hotter to save money" backfires — past 80%, latency is no longer linear in cost.

### 6.3 M/M/c (multi-server)

For c parallel servers (e.g., a worker pool of c workers):

```
W ∝ ρ^c / ((1 - ρ) × c × s)   approximately, for ρ < 1
```

More servers = more graceful degradation. A 1-server pool at 90% utilization is broken; a 10-server pool at 90% utilization is just busy. This is why "scale horizontally" is generally better than "scale up" for latency-sensitive services.

### 6.4 Queueing-aware autoscaling

Most autoscalers target CPU (e.g., scale to keep CPU at 70%). For latency-sensitive services, target *queueing*:

- HPA + custom metric: queue depth or scheduling latency.
- Scale up before CPU saturates to keep wait time low.

This is the right policy for latency SLOs, even though it costs more steady-state.

---

## 7. Load Testing

The only way to validate that "claimed capacity" matches "actual capacity."

### 7.1 Types

| Type | Pattern | Reveals |
|---|---|---|
| **Smoke** | Light load, normal traffic | Are we instrumented correctly? |
| **Capacity / load** | Steady-state at projected load | Will this hold during normal use? |
| **Stress** | Push past capacity until something breaks | Where does the bottleneck actually live? |
| **Spike** | Sudden 5×–10× burst, then back to baseline | Does autoscaling kick in fast enough? |
| **Soak** | Steady moderate load for hours/days | Memory leaks, slow degradation |
| **Chaos** | Add fault injection during load | What happens when a region fails under peak? |

All five are necessary at different points in the service lifecycle. Most teams do only "load" — and thus get surprised by leaks (caught only by soak), and outages (caught only by chaos).

### 7.2 Closed-loop vs open-loop

- **Closed-loop** generators: a fixed number of virtual users; each waits for a response before sending the next. Scales with backend latency.
- **Open-loop** generators: traffic at a fixed RPS, regardless of backend response time.

**Open-loop is the realistic model.** Real customers don't slow down because your backend is slow. Closed-loop *masks* capacity issues — it slows down with you, so the test never reveals queueing.

### 7.3 Tools

| Tool | Strength | Notes |
|---|---|---|
| **k6** | Open-source, scriptable, modern | The default for new teams |
| **Locust** | Python, distributed | Good for complex scenarios |
| **JMeter** | Mature, GUI | Showing age; still common in enterprise |
| **Gatling** | Scala, Akka-based | Good performance; smaller community |
| **wrk / wrk2** | Tiny, very fast | Open-loop by default; great for raw HTTP |
| **Vegeta** | Go-based, simple, open-loop | Excellent for raw HTTP load |
| **Artillery** | Node-based | Solid in JS shops |
| **Locust + custom** | Custom-instrumented for service | Used at scale |

`wrk2` (or `wrk`'s open-loop alternatives) is what you reach for when you need to verify the M/M/1 prediction holds. The default `wrk` is closed-loop and lies about latency at high load.

### 7.4 Load testing in production

Mature orgs run load against production. Reasoning: pre-prod doesn't have prod data, prod scale, prod dependencies. Load tests in pre-prod often miss real bottlenecks.

Production load testing requires:
- Synthetic test traffic (tagged, doesn't pollute analytics).
- Kill switch (stop in 1 second).
- Limited scope (one region, one tenant).
- Run during low-traffic windows initially.

It's high-leverage but high-risk. Net Reliability Engineering (Netflix's approach), Stress Tests (Google), GameDays — all variants.

---

## 8. Provisioning Patterns

### 8.1 Vertical scaling

Bigger instances. Easy mentally; limited by largest available SKU; usually expensive on $/req.

When: small services where horizontal scaling adds operational complexity not worth it.

### 8.2 Horizontal scaling

More instances. The default for stateless services.

When: stateless services. Coordination cost (load balancers, service mesh) is the trade-off.

### 8.3 Autoscaling

Automatic horizontal scaling based on metrics (CPU, queue depth, custom). Cloud-native (AWS ASG, GKE HPA, K8s HPA).

When: variable load. Trade-offs:
- Reactive lag — autoscaling adds capacity *after* the metric crosses; tail latency suffers during the lag.
- Cold starts — new instances need warm-up; routing fresh traffic to cold instances spikes latency.
- Cost noise — bills become harder to predict.

The 2026 maturity bar: predictive autoscaling (scale *before* the metric crosses, based on forecast / schedule).

### 8.4 Cell architecture

Many smaller, isolated copies of the service ("cells"), each serving a subset of traffic. AWS, Google, Slack, GitHub all run cell architectures for highly-available services.

Properties:
- Blast radius: a bug or outage affects one cell, not all.
- Independent capacity per cell.
- Independent scaling per cell.
- Operational complexity: many cells = many things to manage.

When: high-SLO services (≥ 99.99%). Below that, the operational cost is hard to justify.

### 8.5 Provisioning lead time

Critical and often forgotten:

| Resource | Typical lead time |
|---|---|
| Cloud VMs (autoscale) | seconds |
| Cloud VMs (new SKU) | hours |
| Cloud reserved instances | days |
| Self-managed bare metal | weeks |
| New region / new datacenter | months |
| Custom hardware (GPU, TPU) | months to years |

Capacity plans must account for lead time. "We'll need 200 GPUs in 6 months" requires placing the order *now* if the lead time is 6 months. Reactive doesn't work for long-lead resources.

---

## 9. The Capacity Planning Artifact

A capacity plan is a *document*, refreshed quarterly. Live in the SRE / platform repo.

### 9.1 The template

```markdown
# Capacity Plan — checkout-svc — Q3 2026

## Current state (as of 2026-07-01)
- RPS: 1,200 mean, 4,500 peak
- p99 latency: 380ms
- CPU headroom (peak): 38%
- Memory headroom (peak): 42%
- Connection pool headroom: 75%
- DB IOPS headroom: 60%

## Forecast (next 6 months)
- Traffic: +30% (driven by Q4 holiday)
- Feature: +5% per-request CPU (new pricing engine)
- Combined: +37% load

## Required capacity
- Peak load forecast: 4500 × 1.37 = 6,165 RPS
- Required CPU headroom at forecast peak: ≥ 30%
- Implication: scale from 50 pods to 75 pods (50% increase)
- Database: connection pool max from 200 to 300; verify DB IOPS ceiling

## Risks
- DB IOPS ceiling unclear; load test in August
- Pricing engine deploy in September; canary first
- Black Friday peak may exceed forecast; spare capacity in zone B

## Action items
- [ ] Pre-scale to 60 pods by Aug 1
- [ ] Pre-scale to 75 pods by Oct 15
- [ ] Run capacity load test by Aug 30
- [ ] Verify autoscaling can hit 100 pods within 2 minutes
- [ ] Negotiate DB connection pool with platform team

## Buffer
- Operating with 30% buffer above forecast through Q4
- Black Friday: provision 2× steady-state for the week
```

### 9.2 The plan review

Quarterly review. Attended by the service team, platform team, finance / FinOps, leadership.

- Validate forecast against actuals (last quarter's plan vs reality).
- Approve the next quarter's plan.
- Approve any pre-procurement / pre-provisioning.

This is one of the only places where reliability is *negotiated with finance*. Treat the meeting as such.

---

## 10. Multi-Region Capacity

Each region has its own capacity plan; the *aggregate* must absorb single-region failures.

### 10.1 N+1 vs N+2

```
N regions handling traffic
+1 (or +2) regions of spare capacity, ready to absorb failure
```

For 99.99% SLO across 3 active regions, you need each region to handle full load if any one fails — i.e., each region at ≤ 1/2 capacity for N=3 (2 remaining must handle 100%).

### 10.2 The geometric capacity cost

For active/active across N regions, total capacity = (N / (N-1)) × peak load.

| N | Capacity multiplier |
|---|---|
| 2 | 2.0× (one region must handle 100%) |
| 3 | 1.5× |
| 4 | 1.33× |
| 5 | 1.25× |
| 10 | 1.11× |

More regions = lower over-provisioning per region = lower cost-per-9. *This is one of several reasons cell / multi-region architecture is the only path to high SLOs (revisited from `doc 13 §11.2`).*

### 10.3 Active/passive

Cheaper but slower failover.

```
N regions handling traffic
1 standby region, idle, ready to take traffic on failure
```

Failover takes minutes; SLO impact during failover is real. Used when the cost of N+1 active is too high. Common for legacy databases, expensive analytics.

### 10.4 Capacity per failure domain

Multi-region is one failure-domain dimension. Others:
- AZ (within a region)
- Cell (within an AZ)
- Pod (within a cell)
- Node (the underlying host)

For high-SLO services, plan capacity at every failure-domain level: *can the system absorb the loss of one (zone, cell, node)?* Express it as: "each cell is at 60% utilization; we can lose 2 of 5 cells."

---

## 11. Cost-Bounded Capacity

The honest equation: capacity costs money. Provisioning forever is unsustainable.

### 11.1 The trade-off

```
Reliability ←──────────→ Cost
                ↑
            Capacity headroom
```

More headroom = more reliability *and* more cost. Less headroom = cheaper *and* riskier.

The cost-aware capacity plan answers: *what's the minimum headroom that meets the SLO?* That's the optimum operating point. Above it, you're spending for unused reliability; below it, you're risking budget burn.

### 11.2 The cost projection

Every capacity plan has a cost line:

```
Current quarterly cost: $120k
Forecast quarter:        $156k (+30%)
With 30% headroom:       $203k

Net: $83k additional spend per quarter to maintain headroom.
```

This number goes to finance. They either approve, push back, or invest in efficiency work (lower per-request cost). Capacity planning is, at this point, financial planning.

### 11.3 Spot / pre-emptible capacity

Mature orgs use cheaper, pre-emptible capacity (AWS Spot, GCP Preemptible, Azure Spot) for non-critical workloads. Capacity plans separate "must be on-demand" from "can be spot."

A pattern: 80% on-demand for the steady state, 20% spot for burst. Spot's preemption is acceptable because the steady state covers SLOs.

### 11.4 Right-sizing

Most services run on instances 2-3× larger than they need. Quarterly right-sizing:
- Per service, look at last quarter's peak resource use.
- If peak < 50% of instance, downsize to next SKU.
- Run a load test to verify.

This is one of the highest-ROI activities for any platform team — typical savings of 15-30% of compute cost.

---

## 12. Capacity SLIs and Platform SLOs

Capacity itself has SLIs. The platform team owns these.

### 12.1 Capacity-quality SLIs

| SLI | Definition | Target |
|---|---|---|
| **Headroom compliance** | % of (service × resource) cells with ≥ 30% headroom | ≥ 95% |
| **Forecast accuracy** | abs(actual - forecast) / actual | ≤ 20% |
| **Provisioning lead time** | Time from "we need more" to "available and validated" | ≤ 1 week for cloud, ≤ 1 month for self-managed |
| **Capacity-related incidents** | Incidents whose contributing factor is capacity | ≤ 1 per quarter |

These are the platform team's measure of capacity-planning health. If forecast accuracy is consistently > 20%, the model is wrong; revisit. If capacity-related incidents are rising, headroom is too tight or forecasts too optimistic.

### 12.2 The "capacity dashboard"

A single, cross-service dashboard showing:
- Headroom heatmap per service × resource.
- Forecast curves for the next 6 months.
- Recent capacity-related incidents.
- Procurement / provisioning in flight.

This dashboard is for leadership. It's the macro view of organizational reliability investment. Ship it; review it monthly.

---

## 13. The Chicken-and-Egg of New Services

How do you capacity-plan a service that doesn't exist yet?

### 13.1 The bootstrapping options

1. **Analogous services.** "This is like our existing browse service in shape." Use that as the baseline; adjust.
2. **Synthetic load.** Build the service; load test against projected load *before* launch.
3. **Conservative initial provision.** Provision 5× expected; let the early operational data inform the right size.
4. **Soft launch / canary.** Release to 1% of traffic; measure; extrapolate.

In practice: a combination. Analogous estimate → conservative initial provision → soft launch + canary → adjust within first month. The PRR (`doc 17`) bakes capacity planning into the launch checklist.

### 13.2 The "unknown traffic shape" trap

A new service often has an unknown traffic shape — bursty? steady? 10× peak-to-mean? Don't pre-commit to autoscaling thresholds; instrument heavily, watch the first month, then tune.

---

## 14. Worked Example: A 12-Month Checkout Forecast

Concrete, end-to-end.

### 14.1 Inputs

- Q1 traffic: 800 RPS mean, 2400 peak
- Year-over-year growth: +25%
- Q4 holiday seasonality: +60% over Q3 baseline
- New feature in Q3: +10% per-request CPU
- New feature in Q4: +5% per-request memory

### 14.2 Forecast model

```
Q1 baseline:     800 RPS mean, 2400 peak
Q2:              800 × 1.05 (org growth) = 840 mean, 2520 peak
Q3:              840 × 1.05 × 1.10 (CPU feature) = 970 effective load
Q4:              970 × 1.05 × 1.05 × 1.60 (holiday) = 1,710 effective load

Peak:           Q4 peak ≈ 1710 × 3 = 5,130 effective RPS-equivalent CPU
```

### 14.3 Capacity plan

```
Q2 needs: scale from 30 pods → 32 pods (small adjustment)
Q3 needs: scale to 40 pods (CPU feature accounted)
Q4 needs: scale to 65 pods, possibly additional region

Procurement:
- More AWS reserved instances by Aug 31 for Q4 capacity
- Validate auto-scale to 80 pods in Q4 (load test in October)
- Negotiate DB connection pool: 200 → 350

Cost:
- Q1: $80k/quarter
- Q4: $156k/quarter (+95%)
```

### 14.4 Validation

October load test: actual peak handled at 60 pods with 25% headroom. Plan was conservative; release 5 pods of pre-provisioning for Q4 to keep cost in check. Adjust forecast model: holiday seasonality was +50%, not +60%. Re-calibrate next year.

This is what mature capacity planning looks like — quantitative, iterative, calibrated.

---

## 15. Pitfalls

1. **Looking at utilization, not headroom.** Latency surprises before utilization tells you anything.
2. **Forecasting linearly when growth is compound.** Massively under-estimates.
3. **One bottleneck only.** Forgetting connection pools, FDs, kernel structures.
4. **No load test.** Provisioning capacity that doesn't deliver.
5. **Closed-loop load tests.** Mask queueing.
6. **Reactive only.** Lead times bite when the world surprises.
7. **No multi-region capacity.** Single-region failure means full outage.
8. **No quarterly review.** Plan drifts; team learns nothing.
9. **No cost line.** Finance pushes back during budget cycle; everyone surprised.
10. **Right-sizing skipped.** 30%+ of compute spend wasted on oversized instances.
11. **No forecast validation.** Models perpetually wrong; nobody recalibrates.
12. **Capacity SLIs not measured.** Platform team can't show value.
13. **No game day for capacity loss.** Failover untested; surprises during real outage.
14. **No procurement plan for long-lead resources.** GPU / hardware requests delayed by months.
15. **All resources sized to peak.** Pays for capacity that's idle 95% of the year.

---

## 16. Mental Models

> **Headroom, not utilization.** The forward-looking metric.

> **30/50/70.** Below 30% = emergency; 30–50% = act this quarter; 50–70% = plan ahead.

> **The bottleneck migrates.** Plan for which one is *next*, not which one is current.

> **Forecasts are wrong; the point is bounded wrong.** Express ranges, not points; recalibrate monthly.

> **Open-loop load testing reveals queueing; closed-loop hides it.**

> **Lead time is part of the plan.** Long-lead resources need orders months in advance.

> **Capacity is finance.** The cost line is non-negotiable; the headroom-vs-cost trade-off is explicit.

> **Multi-region is the only path to high SLOs.** Geometric capacity costs make it cheaper as N grows.

> **The capacity dashboard is for leadership.** Ship it; review monthly.

> **Right-size quarterly.** It's the highest-ROI cost-saving activity.

Now go to `doc 17` (production readiness) — the gate that ensures every new service has a capacity plan *before* it serves traffic.
