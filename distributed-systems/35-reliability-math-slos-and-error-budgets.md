# Reliability Math: SLOs, SLIs, Error Budgets, MTBF/MTTR, and Availability Engineering

## Executive Summary

Reliability engineering is fundamentally a mathematical discipline. Gut feelings about uptime, vague commitments to "high availability," and reactive firefighting produce systems that oscillate between over-engineering and catastrophic failure. This chapter develops the complete mathematical framework that Staff+ engineers need to reason quantitatively about reliability: from failure rate theory and availability algebra through SLI/SLO design, error budget management, multi-window alerting, and the architecture decisions that each level of availability demands. Every formula includes worked numerical examples drawn from production systems.

---

## Table of Contents

1. [Reliability as a Mathematical Framework](#1-reliability-as-a-mathematical-framework)
2. [Core Reliability Metrics](#2-core-reliability-metrics)
3. [The Nines of Availability](#3-the-nines-of-availability)
4. [SLIs: Service Level Indicators](#4-slis-service-level-indicators)
5. [SLOs: Service Level Objectives](#5-slos-service-level-objectives)
6. [Error Budgets: The Key Innovation](#6-error-budgets-the-key-innovation)
7. [Failure Mode Analysis](#7-failure-mode-analysis)
8. [Redundancy and Replication Math](#8-redundancy-and-replication-math)
9. [Capacity Planning for Reliability](#9-capacity-planning-for-reliability)
10. [Production Reliability Architecture Patterns](#10-production-reliability-architecture-patterns)

---

## 1. Reliability as a Mathematical Framework

### Why Reliability Must Be Quantified

"We aim for high availability" is not a reliability strategy. It is a wish. Without quantification, teams cannot answer the questions that determine architecture, staffing, and investment:

- How much downtime can we tolerate before losing revenue?
- Is our current system reliable enough, or are we over/under-investing?
- Should we ship this feature now, or stabilize first?
- Does adding this dependency make us more or less reliable?

Reliability without math produces two pathological outcomes. Teams either over-engineer (building five-nines infrastructure for a best-effort analytics pipeline) or under-invest (running a payment system on single-instance databases because "it hasn't gone down yet"). Both waste money. The math tells you exactly where you stand and what to spend.

### The Exponential Cost of Each Nine

Each additional nine of availability roughly 10x the engineering cost. This is not hyperbole; it is an empirical pattern observed across the industry.

```
Cost vs. Availability (Approximate Industry Pattern)

Cost Multiplier
    │
 32x│                                                    *  (99.999%)
    │
 16x│                                        *  (99.99%)
    │
  8x│                            *  (99.9%)
    │
  4x│                *  (99%)
    │
  2x│    *  (95%)
    │
  1x│*  (90%)
    └──────────────────────────────────────────── Availability
```

Moving from 99.9% to 99.99% does not mean "a little more monitoring." It means multi-AZ deployments, automated failover, chaos testing, zero-downtime deploys, and an on-call rotation with sub-minute response times. Moving from 99.99% to 99.999% means multi-region active-active, consensus-based state replication, and formal verification of failure modes.

### Why 100% Is Impossible and Undesirable

100% availability is impossible for any system that depends on physical hardware, software updates, or network connectivity. But more importantly, it is *undesirable*. A system that never fails is a system that never changes. Zero downtime tolerance means:

- No deployments (every deploy carries nonzero risk)
- No dependency upgrades
- No schema migrations
- No experimentation

Google's SRE book states this plainly: *"100% is the wrong reliability target for basically everything."* The correct target is the point where additional reliability investment no longer produces proportional user or business value.

### The Fundamental Tension

Feature velocity and reliability are in direct tension. Every code change carries risk. Every deployment is an opportunity for failure. The error budget framework (Section 6) formalizes this tension into an objective decision procedure: ship when you have budget, stabilize when you do not.

---

## 2. Core Reliability Metrics

### MTBF: Mean Time Between Failures

**MTBF** measures the average time a system operates between consecutive failures.

```
Formula:  MTBF = Total Operational Time / Number of Failures

Example:  A service ran for 8,760 hours (1 year) and experienced 12 failures.
          MTBF = 8,760 / 12 = 730 hours (~30.4 days between failures)
```

**What affects MTBF**: hardware quality, software maturity, deployment frequency, change management rigor, dependency stability.

MTBF is useful for capacity planning and failure prediction, but it has a critical limitation: it tells you nothing about how quickly you recover. A system with MTBF of 1,000 hours and 10-hour recoveries is less available than one with MTBF of 100 hours and 1-minute recoveries.

### MTTR: Mean Time To Recovery

**MTTR** measures the average time from failure onset to full service restoration.

```
Formula:  MTTR = Total Downtime / Number of Failures

Example:  12 failures caused a total of 18 hours of downtime.
          MTTR = 18 / 12 = 1.5 hours per incident
```

MTTR decomposes into four stages, each of which can be independently optimized:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        MTTR Decomposition                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Failure   ──►  MTTD    ──►  Diagnosis  ──►  Repair    ──►  Verification   │
│  Occurs        (Detect)      (Identify       (Fix the       (Confirm fix     │
│                               root cause)     problem)       works)           │
│                                                                              │
│  ├─── Detection ───┤── Diagnosis ──┤─── Repair ───┤── Verify ──┤            │
│       Time              Time           Time           Time                   │
│                                                                              │
│  MTTR = Detection + Diagnosis + Repair + Verification                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

| Component | Typical Range | How to Reduce |
|-----------|--------------|---------------|
| Detection | 1-30 min | Better monitoring, multi-signal alerting, anomaly detection |
| Diagnosis | 5-120 min | Runbooks, structured logging, distributed tracing, dashboards |
| Repair | 5-180 min | Automated rollback, feature flags, capacity auto-scaling |
| Verification | 2-30 min | Canary deploys, synthetic probes, automated smoke tests |

### Why MTTR Investment Beats MTBF Investment

Consider two strategies for improving availability from 99.5% to 99.9%:

**Strategy A — Improve MTBF** (prevent failures):
- Current: MTBF=200h, MTTR=1h, Availability = 200/(200+1) = 99.50%
- Target 99.9%: Need MTBF = MTTR/(1-A) - MTTR = 1/(0.001) - 1 = 999h
- You must 5x your MTBF. This means eliminating most failure modes — a massive effort.

**Strategy B — Improve MTTR** (recover faster):
- Current: MTBF=200h, MTTR=1h, Availability = 200/(200+1) = 99.50%
- Target 99.9%: Need MTTR = MTBF(1-A)/A = 200(0.001)/0.999 = 0.2h (12 minutes)
- You must 5x your recovery speed. Automated rollback alone can achieve this.

Strategy B is almost always cheaper and more tractable. You cannot prevent all failures, but you can detect and recover from them faster. This is why Google, Amazon, and Netflix invest heavily in observability and automated remediation rather than trying to eliminate all failure modes.

### MTTD: Mean Time To Detect

**MTTD** is the often-neglected first component of MTTR. If detection takes 20 minutes and repair takes 5 minutes, your priority is detection, not repair.

```
Formula:  MTTD = Sum of Detection Times / Number of Incidents

Example:  12 incidents. Detection times: [2, 5, 1, 45, 3, 2, 8, 1, 15, 3, 2, 4] minutes.
          MTTD = 91 / 12 = 7.6 minutes average

          But note that outlier: one incident took 45 minutes to detect.
          Median MTTD = 3 minutes. The p90 detection time matters more than the average.
```

Detection failures are the silent killer of availability. An undetected outage is an outage that persists at full severity until a user reports it. Invest in synthetic monitoring, multi-signal alerting, and anomaly detection before investing in faster repair.

### The Availability Formula

```
                 MTBF                  Uptime
Availability = ──────────── = ────────────────────────
               MTBF + MTTR    Uptime + Downtime
```

**Worked example**:
- MTBF = 720 hours (a failure roughly every month)
- MTTR = 0.5 hours (30-minute recovery)
- A = 720 / (720 + 0.5) = 720 / 720.5 = 99.931%

This system achieves roughly three nines. To reach four nines with the same MTBF, you would need MTTR = 720 * 0.0001 / 0.9999 = 0.072 hours = 4.3 minutes. Possible with automated detection and rollback.

### Failure Rate and the Reliability Function

The **failure rate** (lambda) is the reciprocal of MTBF:

```
λ = 1 / MTBF

Example:  MTBF = 730 hours → λ = 0.00137 failures per hour
```

The **reliability function** gives the probability that a system survives without failure for duration *t*, assuming failures follow an exponential distribution (constant failure rate):

```
R(t) = e^(-λt)

Example:  λ = 0.00137 (MTBF = 730 hours)
          Probability of surviving 168 hours (1 week) without failure:
          R(168) = e^(-0.00137 × 168) = e^(-0.230) = 0.794 = 79.4%

          Probability of surviving 720 hours (1 month):
          R(720) = e^(-0.00137 × 720) = e^(-0.986) = 0.373 = 37.3%
```

This means: even a system with 30-day MTBF only has a 37% chance of making it through any given month without a failure. Reliability intuition is often wrong — the math keeps you honest.

---

## 3. The Nines of Availability

### The Complete Availability Table

| Availability | Common Name | Downtime/Year | Downtime/Month | Downtime/Week | Downtime/Day | Typical Systems |
|:---:|:---:|:---:|:---:|:---:|:---:|:---|
| 99% | Two Nines | 3d 15h 36m | 7h 18m | 1h 41m | 14m 24s | Internal tools, batch pipelines |
| 99.5% | Two-and-a-half | 1d 19h 48m | 3h 39m | 50m 24s | 7m 12s | Non-critical web apps |
| 99.9% | Three Nines | 8h 45m 36s | 43m 50s | 10m 5s | 1m 26s | SaaS products, APIs |
| 99.95% | Three-and-a-half | 4h 22m 48s | 21m 55s | 5m 2s | 43s | E-commerce, user-facing services |
| 99.99% | Four Nines | 52m 36s | 4m 23s | 1m 0.5s | 8.6s | Financial APIs, core infrastructure |
| 99.999% | Five Nines | 5m 15.6s | 26.3s | 6s | 0.86s | Telecom switches, payment rails |
| 99.9999% | Six Nines | 31.5s | 2.6s | 0.6s | 0.086s | Pacemakers, flight control |

### Architecture Requirements at Each Level

- **99%**: Single server, manual restart on failure, basic monitoring. Acceptable for internal dashboards.
- **99.9%**: Redundant services, health checks, automated restarts, basic alerting, load balancing.
- **99.99%**: Multi-AZ deployment, automated failover, chaos testing, zero-downtime deploys, sub-minute detection.
- **99.999%**: Multi-region active-active, consensus-based replication, formal verification of failure modes, dedicated SRE team, custom tooling.
- **99.9999%**: Purpose-built hardware, N+2 redundancy at every layer, formal proofs, real-time monitoring with sub-second detection.

### Composite Availability: Serial Systems

When services are chained in series (each depends on the next), availability *multiplies*:

```
Serial Availability:  A_total = A_1 x A_2 x A_3 x ... x A_n


                 ┌──────────┐    ┌──────────┐    ┌──────────┐
  Request ──────►│ Service A ├───►│ Service B ├───►│ Service C ├──────► Response
                 │  99.9%   │    │  99.9%   │    │  99.9%   │
                 └──────────┘    └──────────┘    └──────────┘

  A_total = 0.999 x 0.999 x 0.999 = 0.997 = 99.7%
```

**This is the microservices availability trap.** Five services at 99.9% each:

```
A_total = 0.999^5 = 0.995 = 99.5%   (only two-and-a-half nines!)
```

Ten services at 99.9% each:

```
A_total = 0.999^10 = 0.990 = 99.0%   (down to two nines)
```

Each additional serial dependency makes the system strictly less available than its least available component.

### Composite Availability: Parallel (Redundant) Systems

When components are redundant (the system works if *any* replica works), unavailability multiplies:

```
Parallel Availability:  A_total = 1 - (1 - A_1)(1 - A_2)...(1 - A_n)


                            ┌──────────┐
                       ┌───►│ Server A  ├───┐
                       │    │  99.9%   │   │
  Request ─────────────┤    └──────────┘   ├──────► Response
                       │    ┌──────────┐   │
                       └───►│ Server B  ├───┘
                            │  99.9%   │
                            └──────────┘

  A_total = 1 - (1 - 0.999)(1 - 0.999) = 1 - (0.001)^2 = 1 - 0.000001 = 99.9999%
```

Two 99.9% servers in active-active yield six nines. Three replicas:

```
A_total = 1 - (0.001)^3 = 1 - 0.000000001 = 99.9999999%   (nine nines!)
```

But this assumes *independent* failures. Correlated failures (same rack, same software bug, same config push) destroy these calculations. See Section 7.

### Mixed Serial-Parallel Calculations

Real systems combine serial and parallel components. Solve inside-out: compute parallel groups first, then multiply serially.

```
                        ┌──────────┐
                   ┌───►│  App-1   ├───┐
                   │    │  99.9%   │   │
┌──────────┐       │    └──────────┘   │       ┌──────────┐
│   LB     ├───────┤                   ├──────►│    DB    │
│  99.99%  │       │    ┌──────────┐   │       │  99.99%  │
└──────────┘       └───►│  App-2   ├───┘       └──────────┘
                        │  99.9%   │
                        └──────────┘

Step 1: App tier (parallel) = 1 - (0.001)(0.001) = 99.9999%
Step 2: Total (serial)      = 0.9999 x 0.999999 x 0.9999
                            = 0.9998 = 99.98%
```

The single-instance LB and DB become the bottleneck. Adding redundancy to the app tier beyond two replicas yields diminishing returns until you also add redundancy to the LB and DB.

### Breaking Serial Dependency Chains

To prevent serial multiplication from destroying availability in microservice architectures:

| Strategy | How It Helps | Example |
|----------|-------------|---------|
| **Caching** | Request succeeds from cache even if downstream is down | CDN serves stale content during origin outage |
| **Async processing** | Decouple request acceptance from processing | Queue writes, acknowledge immediately |
| **Circuit breakers** | Fail fast instead of cascading timeouts | Return cached/default response when dependency fails |
| **Graceful degradation** | Convert hard dependency to soft dependency | Show "recommendations unavailable" instead of 500 |
| **Timeouts + retries** | Bound the impact of slow dependencies | 200ms timeout with 1 retry to different instance |

---

## 4. SLIs: Service Level Indicators

### Definition

An **SLI** (Service Level Indicator) is a quantitative measure of a specific aspect of service quality, expressed as a ratio:

```
         Good Events
SLI = ─────────────────
        Total Events
```

SLIs are always between 0 and 1 (or 0% and 100%).

### Common SLI Types

| SLI Type | What It Measures | Formula | Typical Threshold |
|----------|-----------------|---------|-------------------|
| **Availability** | Request success rate | Successful requests / Total requests | 2xx+3xx responses, excluding 4xx |
| **Latency** | Speed within threshold | Requests < threshold / Total requests | p99 < 300ms |
| **Throughput** | Processing rate | Time at min rate / Total time | > 1000 req/s sustained |
| **Correctness** | Data accuracy | Correct responses / Total responses | Hash-verified responses |
| **Freshness** | Data recency | Data updated within window / Total data | Updated within 60s |

### Where to Measure SLIs

```
┌──────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────┐
│  Client  │────►│ Load Balancer │────►│   Service    │────►│ Database │
└──────────┘     └──────────────┘     └──────────────┘     └──────────┘
     ▲                  ▲                    ▲
     │                  │                    │
  Client-side       LB-side             Server-side
   SLI (best)    SLI (practical)     SLI (most common)
```

**Client-side SLIs** capture the true user experience: network latency, DNS failures, TLS handshake time, retries. They are the most accurate but hardest to collect (requires client instrumentation, mobile SDKs, or Real User Monitoring).

**Load-balancer SLIs** are the practical compromise. They see all traffic, include connection-level failures, and are easy to instrument. This is what most organizations use.

**Server-side SLIs** miss failures that happen before the request reaches the server (TCP timeouts, LB errors, network partitions).

### What Counts as a Request?

The **denominator problem**: your SLI is only as good as your definition of "total events."

- Exclude health check probes from load balancers
- Exclude synthetic monitoring traffic (unless measured separately)
- Decide whether 4xx responses are "good" (client error, not your fault) or "bad" (broken API contract)
- Decide whether requests rejected by rate limiting count against availability
- Define behavior during planned maintenance windows

A common approach: availability SLI counts 5xx responses and timeouts as bad events. 4xx responses are excluded from both numerator and denominator unless they indicate a server-side bug (e.g., a 404 for a resource that should exist).

### Latency Is a Distribution, Not a Number

Averages lie about latency. Consider two services:

```
Service A:  All requests complete in 50ms.  Average = 50ms.
Service B:  99% complete in 10ms, 1% take 5000ms.  Average = 10(0.99) + 5000(0.01) = 59.9ms.

Both have ~50-60ms "average" latency. Service B is a disaster for 1% of users.
```

**Percentiles reveal the truth**:

| Percentile | What It Tells You | Typical Use |
|:---:|---|---|
| p50 (median) | Typical user experience | Capacity planning |
| p95 | Worst experience for most users | SLO target for general APIs |
| p99 | Worst experience for power users | SLO target for critical paths |
| p99.9 | Tail latency — often indicates systemic issues | Debugging, not SLO targets |

**Coordinated omission**: most load testing tools (wrk, ab, JMeter in default mode) undercount tail latency. When the server is slow, the tool sends fewer requests, missing the worst-case measurements. Gil Tene's HdrHistogram and wrk2 correct for this. If your load test shows p99 = 50ms but production shows p99 = 2000ms, coordinated omission is the likely explanation.

---

## 5. SLOs: Service Level Objectives

### Definition

An **SLO** is a target value or range for an SLI, over a defined time window. It is an *internal* commitment that drives engineering decisions.

```
SLO = "99.9% of requests will return successfully within 300ms,
       measured over a rolling 30-day window."

Components:
  - SLI: Proportion of requests succeeding within 300ms
  - Target: 99.9%
  - Window: Rolling 30 days
```

### Setting SLOs

**Start from user expectations, not from current performance.** If your service currently runs at 99.99% but users only need 99.9%, your SLO should be 99.9%. The gap between current performance and SLO is your error budget for shipping features.

**Process for setting SLOs**:

1. **Identify the user journey**: What does the user experience when the service fails or is slow?
2. **Determine acceptable experience**: How much failure/slowness before users notice, complain, or leave?
3. **Analyze historical data**: What has actual performance been over the last 6-12 months?
4. **Set conservative initial targets**: Start below historical performance to create a usable error budget
5. **Differentiate by criticality**: Payment processing needs tighter SLOs than image thumbnails

**SLOs by request type**:

| Request Type | Availability SLO | Latency SLO (p99) | Rationale |
|:---|:---:|:---:|:---|
| Payment processing | 99.99% | 500ms | Direct revenue impact |
| User authentication | 99.95% | 200ms | Blocks all user activity |
| Search results | 99.9% | 300ms | Can degrade gracefully |
| Profile image serving | 99.5% | 1000ms | Cached, non-critical |
| Analytics ingestion | 99% | 5000ms | Async, can replay from queue |

### SLO Window Types

**Calendar window** (this calendar month): Simple to understand but creates perverse incentives. An outage on day 1 burns the budget early and creates 29 days of extreme caution. An outage on day 29 seems free because the budget resets tomorrow.

**Rolling window** (last 30 days): Every moment, the SLO considers the most recent 30 days. Yesterday's good performance gradually exits the window; today's incident stays for 30 days. This provides continuous, consistent pressure and is the recommended approach.

### Multi-Window, Multi-Burn-Rate Alerting

A single SLO threshold alert fires too late (budget exhausted) or too often (transient blips). The Google SRE approach uses multiple windows and burn rates to catch both acute outages and chronic degradation.

**Error budget burn rate** = the rate at which you are consuming your error budget relative to the steady-state rate.

```
                         Error Rate Observed
Burn Rate = ────────────────────────────────────────
              Error Rate Allowed by SLO

For a 99.9% SLO: allowed error rate = 0.1%

If current error rate = 1.44%:
  Burn Rate = 1.44% / 0.1% = 14.4x

  At this rate, a 30-day budget is exhausted in 30/14.4 = 2.08 days.
```

**The complete multi-window alerting table** (for a 30-day SLO window):

| Severity | Burn Rate | Long Window | Short Window | Budget Consumed | Action |
|:---:|:---:|:---:|:---:|:---:|:---|
| Page (critical) | 14.4x | 1 hour | 5 min | 2% in 1 hour | Immediate response, likely outage |
| Page (high) | 6x | 6 hours | 30 min | 5% in 6 hours | Significant degradation |
| Ticket (medium) | 3x | 1 day | 2 hours | 10% in 1 day | Chronic issue emerging |
| Ticket (low) | 1x | 3 days | 6 hours | 10% in 3 days | Slow sustained degradation |

**Why you need both long and short windows**: The long window determines if enough budget has been consumed to warrant attention. The short window confirms the problem is ongoing right now (not a resolved blip still inside the long window).

```
Error Budget Burn Rate Visualization

Budget
Remaining
100% ┤ ............
     │              .....
     │                   ....
     │                       ..
     │                         .                    ◄── 1x burn rate (normal)
     │                          ..
     │                            ...
     │                               .....
     │                                    ..........
  0% ┤─────────────────────────────────────────────── Time
     Day 1                                    Day 30

     vs.

100% ┤ .
     │   .
     │    .
     │     .        ◄── 14.4x burn rate (outage)
     │      .
  0% ┤───────.──────────────────────────────────────── Time
     Day 1  Day 2                             Day 30
```

**Alert math**: For a burn rate alert with parameters (burn_rate, long_window, short_window):
- The alert fires when the error rate over *both* windows exceeds `burn_rate * (1 - SLO)`
- For the 14.4x/1-hour alert with a 99.9% SLO: fires when error rate > 14.4 * 0.001 = 1.44% over both the 1-hour and 5-minute windows

---

## 6. Error Budgets: The Key Innovation

### Definition and Calculation

The **error budget** is the amount of unreliability you can afford within your SLO window.

```
Error Budget = 1 - SLO

Example: 99.9% SLO over 30 days
  Error Budget = 0.1% = 0.001
  Time Budget  = 30 days x 24 hours x 60 minutes x 0.001 = 43.2 minutes
  Request Budget = If you serve 10M requests/month: 10,000 allowed failures
```

| SLO | Error Budget (%) | Minutes/Month | Failed Requests (per 10M) |
|:---:|:---:|:---:|:---:|
| 99% | 1.0% | 432 min (7.2 hrs) | 100,000 |
| 99.5% | 0.5% | 216 min (3.6 hrs) | 50,000 |
| 99.9% | 0.1% | 43.2 min | 10,000 |
| 99.95% | 0.05% | 21.6 min | 5,000 |
| 99.99% | 0.01% | 4.32 min | 1,000 |
| 99.999% | 0.001% | 0.43 min (26s) | 100 |

### Error Budget as a Decision Framework

The error budget converts reliability from a binary (up/down) into a continuous resource that can be *spent* like any other resource.

```
Error Budget Decision Tree

Budget Status          │  Development Action        │  Reliability Action
───────────────────────┼────────────────────────────┼────────────────────────
> 50% remaining        │  Ship freely               │  Normal operations
                       │  Experiment aggressively    │  Continue reliability work
───────────────────────┼────────────────────────────┼────────────────────────
10-50% remaining       │  Ship with caution          │  Prioritize reliability
                       │  Require rollback plans     │  Review recent incidents
───────────────────────┼────────────────────────────┼────────────────────────
< 10% remaining        │  Freeze risky deploys       │  Mandatory reliability sprint
                       │  Bug fixes only             │  Root-cause all incidents
───────────────────────┼────────────────────────────┼────────────────────────
Budget exhausted       │  Full deployment freeze     │  All hands on reliability
                       │  No features shipped        │  SRE team has veto power
```

### Error Budget Policies

A written error budget policy answers:

1. **Who can spend the budget?** Product teams spend it by shipping features. SRE teams spend it by performing maintenance. Both should be tracked.
2. **What happens when it is exhausted?** The policy must have teeth. A deployment freeze that leadership can override "for business reasons" is not a policy.
3. **How are disputes resolved?** When product wants to ship and SRE wants to freeze, who decides? Typically, the VP of Engineering adjudicates, using the error budget as the objective criterion.
4. **What counts as spending?** Planned maintenance windows should be excluded (or given a separate budget). Failures caused by dependencies outside your control may be tracked separately.

### Budget Attribution

Tracking *which component consumed how much budget* is critical for directing reliability investment.

```
Error Budget Consumption Report — September 2026

Component               │ Budget Consumed │ Incidents │ Primary Cause
─────────────────────────┼─────────────────┼───────────┼──────────────────────
Payment service          │  35%            │  2        │ Database connection pool
API gateway              │  22%            │  1        │ Bad config deploy
Search service           │  18%            │  3        │ Memory leak (chronic)
Auth service             │   5%            │  1        │ Certificate rotation
Unattributed             │   3%            │  —        │ Network blips
─────────────────────────┼─────────────────┼───────────┼──────────────────────
Total consumed           │  83%            │  7        │
Remaining                │  17%            │           │
```

This report immediately tells you: invest in the payment service's database connection pool. That one component consumed a third of the entire budget.

### The Error Budget as a Negotiation Tool

The error budget aligns incentives between product and engineering:

- **Product teams** want to ship features. Features require deployments. Deployments spend error budget. Therefore, product teams have an incentive to support reliability investments that *preserve* error budget for feature work.
- **SRE teams** want reliability. Error budgets give them an objective, data-driven lever. "We cannot ship this feature because we have 3 minutes of budget left this month" is far more effective than "we feel like the system is fragile."
- **The balancing act**: If the error budget is consistently unspent, the SLO is too loose — tighten it, or redirect engineering effort to features. If the budget is consistently exhausted, either the SLO is too tight or reliability needs investment.

---

## 7. Failure Mode Analysis

### Identifying Failure Modes

Every system has a finite (though large) set of ways it can fail. Categorizing them:

| Category | Frequency | Typical MTTR | Examples |
|----------|:---------:|:------------:|---------|
| Hardware failures | Low | Hours | Disk death, NIC failure, power supply, rack switch |
| Software bugs | Medium | Minutes-Hours | Memory leak, race condition, null pointer |
| Configuration errors | High | Minutes | Bad feature flag, wrong connection string, typo in YAML |
| Capacity exhaustion | Medium | Minutes-Hours | OOM, disk full, connection pool exhaustion, thread starvation |
| Dependency failures | High | Minutes | Upstream API outage, DNS failure, certificate expiry |
| Operator error | High | Minutes-Hours | Wrong cluster, wrong command, forgot to update config |

### Human Error Dominance

Studies consistently show that 60-80% of production outages involve human error. Google's published incident data, Amazon's COE (Correction of Error) reports, and Microsoft Azure's RCAs all confirm this pattern.

The top human-error categories:

1. **Configuration changes** (40%+): YAML typo, wrong feature flag value, incorrect connection string.
2. **Failed deployments** (25%+): Untested code path, incompatible schema migration, missing environment variable.
3. **Operational procedures** (15%+): Wrong runbook, wrong cluster, misread dashboard.

Implication: invest in deployment safety (canary, progressive rollout, automated rollback) and configuration validation (schema validation, dry-run, diff review) before investing in hardware redundancy.

### Independent vs. Correlated Failures

The parallel availability formula `A = 1 - (1-a)^n` assumes **independent** failures. In practice, many failures are correlated:

```
Failure Probability Tree — Independent vs Correlated

Independent:                        Correlated:

     Server A fails: 0.001               Shared cause
     Server B fails: 0.001               (bad deploy, same rack)
     Both fail: 0.001 x 0.001                  │
             = 0.000001 (6 nines)              ▼
                                         Server A fails: 0.001
                                         Server B also fails: 0.95
                                           (given same cause)
                                         Both fail: 0.001 x 0.95
                                                 = 0.00095 (~3 nines)
```

**Common mode failures** — a single cause taking out multiple "redundant" systems — are the primary threat to highly available architectures:

- Same software version on all replicas (a bug affects all)
- Same configuration pushed to all instances simultaneously
- Same physical rack or power domain
- Same cloud provider AZ experiencing an outage
- Same certificate expiring on all nodes

**Mitigation**: staggered deployments, diverse failure domains (multi-AZ, multi-region), configuration rollout canaries, independent software versions across replica sets (blue-green at the version level).

### Blast Radius Analysis

For every component, answer: "If this fails, what else breaks?"

```
Component              │ Direct Impact        │ Indirect Impact         │ Blast Radius
───────────────────────┼──────────────────────┼─────────────────────────┼────────────
Single app instance    │ 1/N of traffic       │ Minimal                 │ Small
Database primary       │ All writes           │ All reads (if no replica│ Large
                       │                      │  or failover is slow)   │
DNS                    │ All services          │ All clients             │ Total
Shared config service  │ All dependent services│ Cascading failures      │ Total
Auth/Identity service  │ All authenticated reqs│ Every user-facing svc   │ Total
```

### Pre-mortem Exercises

Rather than waiting for failures to happen, systematically imagine them. For each critical component:

1. Assume it has failed completely right now.
2. How would you detect it? (Tests MTTD)
3. What is the user impact? (Tests blast radius understanding)
4. What is the recovery procedure? (Tests runbook readiness)
5. How long would recovery take? (Tests MTTR assumptions)

---

## 8. Redundancy and Replication Math

### Active-Active vs. Active-Passive

**Active-passive**: one instance handles traffic, the standby takes over on failure.

```
Availability = 1 - P(primary fails) x P(failover fails)

If primary = 99.9%, failover success rate = 99%:
  A = 1 - (0.001)(0.01) = 1 - 0.00001 = 99.999%

But if failover is untested and has only 90% success rate:
  A = 1 - (0.001)(0.10) = 1 - 0.0001 = 99.99%
```

Untested failover mechanisms are a leading cause of outages. If you never test failover, assume its success rate is well below 90%.

**Active-active**: all instances handle traffic. No failover step; a failed instance is simply removed from the pool.

```
Two active-active instances at 99.9%:
  A = 1 - (0.001)^2 = 99.9999%  (assuming independent failures)
```

Active-active is strictly superior in availability math but adds complexity in state management (split-brain, data consistency, conflict resolution).

### N+1 vs. N+2 Redundancy

- **N+1**: System can tolerate one simultaneous failure. Minimum for production systems.
- **N+2**: System can tolerate two simultaneous failures. Required when one component might be down for maintenance while another fails unexpectedly.

```
Example: 3 replicas serving traffic (N=3)

N+1 (4 total): Can handle peak load with any 1 replica down.
  Failure probability = C(4,2) x p^2 x (1-p)^2  (for 2+ simultaneous failures)
  With p=0.001: = 6 x 0.000001 x 0.998 = 0.000006 = 99.9994%

N+2 (5 total): Can handle peak load with any 2 replicas down.
  Need 3+ simultaneous failures to impact service.
  With p=0.001: P(3+) = C(5,3)(0.001)^3(0.999)^2 + ... ≈ 0.00000001 ≈ 99.999999%
```

### Quorum Math

Distributed consensus systems use **majority quorums**: `Q = floor(n/2) + 1`.

| Replicas (n) | Quorum (Q) | Tolerated Failures | Notes |
|:---:|:---:|:---:|:---|
| 3 | 2 | 1 | Minimum for consensus |
| 5 | 3 | 2 | Standard production deployment |
| 7 | 4 | 3 | High-reliability or geo-distributed |

**Why odd numbers**: With n=4, the quorum is still 3, and you only tolerate 1 failure — same as n=3 but with the cost of an extra node. Even replica counts waste resources.

### Replication Factor Tradeoffs

| Factor | Availability | Write Latency | Storage Cost | Consistency Complexity |
|:---:|:---:|:---:|:---:|:---:|
| RF=1 | Lowest | Fastest | 1x | None |
| RF=3 | High | Medium (quorum write) | 3x | Manageable |
| RF=5 | Very high | Higher (quorum=3) | 5x | Complex |

### Geographic Redundancy: Worked Example

"What availability do I get with 3 replicas across 2 regions?"

```
Setup: 3 replicas total, 2 in Region A, 1 in Region B.
       Per-replica availability: 99.9%
       Per-region availability: 99.99% (region-level infrastructure)

Scenario analysis:
  System needs quorum (2 of 3 replicas) to operate.

  P(any single replica down) = 0.001
  P(Region A down, taking out 2 replicas) = 0.0001

  P(system down) = P(Region A down) x P(Region B replica down)     [both regions]
                 + P(2+ individual replicas fail independently)      [no region outage]

  = 0.0001 x 0.001                    [both regions fail]
  + C(3,2)(0.001)^2(0.999)            [2 independent replica failures]
  + (0.001)^3                          [all 3 replicas fail independently]

  = 0.0000001 + 0.000002997 + 0.000000001
  = 0.000003098

  A = 1 - 0.000003098 = 99.9997%   (roughly 4.5 nines)
```

For five nines, you would need at least 2 replicas in each of 2+ independent regions, ensuring no single region failure loses quorum.

---

## 9. Capacity Planning for Reliability

### Headroom Rules

Running at high utilization leaves no room to absorb failures or traffic spikes. Production rules of thumb:

| Metric | Steady-State Max | Rationale |
|--------|:---:|---|
| CPU utilization | 60-70% | Headroom for GC pauses, burst traffic, failover absorption |
| Memory utilization | 70-80% | OOM kills are catastrophic and difficult to recover from |
| Disk utilization | 70-75% | Many filesystems degrade above 80%; compaction needs temp space |
| Network bandwidth | 50-60% | TCP throughput collapses with congestion near capacity |
| Connection pool | 60-70% | Connection exhaustion causes cascading failures |

### The N-1 Rule

**The system must handle peak traffic with one component down.**

```
Example: Peak load = 10,000 req/s. Each instance handles 2,500 req/s.

  Naive: 4 instances (10,000 / 2,500).
  N-1:   5 instances. With 1 down, 4 remaining handle 10,000 req/s.

  With headroom: Each instance should run at ~70% capacity at peak.
  Adjusted capacity per instance: 2,500 * 0.70 = 1,750 req/s effective.
  Required: ceil(10,000 / 1,750) + 1 = 6 + 1 = 7 instances.
```

The difference between 4 naive instances and 7 properly planned instances is the difference between "works in testing" and "survives production."

### Burst Absorption

Traffic does not arrive uniformly. Flash sales, viral content, news events, and DDoS attacks can produce 2-10x normal traffic within seconds. Autoscaling cannot react fast enough (typical cloud autoscaling takes 2-5 minutes to provision and warm new instances).

**Capacity plan for 2-3x peak within the pre-provisioned fleet.** Autoscaling handles sustained growth beyond that, but the initial spike must be absorbed by existing capacity.

### Cost vs. Reliability Tradeoff

```
                  Reliability
                      │
   99.999% ───────────┤                              * ─── Diminishing returns
                      │                         *
   99.99% ────────────┤                    *
                      │               *
   99.9% ─────────────┤          *
                      │      *
   99% ───────────────┤  *
                      │
                      └──────────────────────────────────── Cost
                      1x    2x    4x    8x   16x   32x
```

The right target is where the cost curve's slope exceeds the business value of additional reliability. For most consumer services, this is between 99.9% and 99.99%. For payment infrastructure, between 99.99% and 99.999%. For life-safety systems, cost is secondary.

---

## 10. Production Reliability Architecture Patterns

### Summary: Reliability Target to Architecture

| Target | Architecture Requirements | Monitoring | Deploy Strategy | Approximate Cost Multiplier |
|:---:|:---|:---|:---|:---:|
| **99%** | Single region, basic redundancy (N+1), manual restart | Health checks, basic dashboards | Rolling deploy, manual rollback | 1x |
| **99.9%** | Multi-AZ, load balancing, automated health checks, automated restart | Alerting with on-call rotation, structured logging | Blue-green or canary deploy, automated rollback | 2-3x |
| **99.99%** | Multi-AZ active-active, automated failover, connection draining, chaos testing | Multi-signal alerting, distributed tracing, SLO dashboards, error budget tracking | Progressive rollout (1% > 5% > 25% > 100%), feature flags, instant rollback | 4-8x |
| **99.999%** | Multi-region active-active, consensus-based replication, zero-downtime deploys, dedicated SRE team | Sub-minute detection, multi-window burn-rate alerts, synthetic monitoring from all regions | Per-region staged rollout, traffic draining before deploy, automated pre/post verification | 10-20x |
| **99.9999%** | Custom hardware, N+2 at every layer, formal verification, multiple independent implementations, real-time failover | Real-time monitoring with sub-second detection, hardware-level telemetry | Change advisory board, formal proof of deploy safety, independent verification | 30-100x |

### Cloud Provider Published SLAs (Reference Points)

| Provider/Service | Published SLA | Notes |
|:---|:---:|:---|
| AWS EC2 (single instance) | 99.5% | Single AZ; no redundancy |
| AWS EC2 (multi-AZ) | 99.99% | Across AZs in same region |
| AWS S3 | 99.9% (availability), 99.999999999% (durability) | 11 nines of durability, 3 nines of availability |
| AWS RDS Multi-AZ | 99.95% | Automated failover between AZs |
| Google Cloud Compute | 99.99% | Multi-zone |
| Azure VMs (Availability Zones) | 99.99% | Across zones |
| Stripe API | 99.999% (target) | Achieved through extensive redundancy |

Note: published SLAs are contractual minimums with financial credits. Actual performance is typically better. Your system's availability is bounded by the *worst* of your dependencies' *actual* availability (not their SLA), multiplied serially.

### Putting It All Together: A Worked Architecture Example

**Requirement**: E-commerce checkout must achieve 99.95% availability.

```
Step 1: Identify the critical path.
  Client → CDN → API Gateway → Auth → Cart Service → Payment Service → Order DB

Step 2: Assign per-component availability targets.
  If we have 6 serial components and need 99.95% total:
  Per-component target = 99.95%^(1/6)  ... this does not work directly.
  Instead: 0.9995 = A^6 → A = 0.9995^(1/6) = 0.99992 per component.

  Each component needs 99.992% — nearly four nines. This is the serial tax.

Step 3: Reduce serial dependencies.
  - CDN serves cached pages during API gateway outage (removes 1 serial dep)
  - Auth uses cached tokens with 5-minute TTL (converts hard dep to soft)
  - Cart service has local cache of product data (removes catalog dep)
  - Payment service uses async confirmation (decouple from order DB write)

  Effective serial depth reduced from 6 to 3-4 components.
  Per-component target: 0.9995^(1/4) = 0.99987 ≈ 99.987% — achievable with multi-AZ.

Step 4: Add redundancy where serial dependencies remain.
  - API Gateway: 3 instances, 2 AZs → parallel availability = 99.9999%
  - Payment Service: 2 active-active instances, circuit breaker → 99.999%
  - Order DB: Multi-AZ RDS with automated failover → 99.95% (per AWS SLA)

  Result: 0.999999 x 0.99999 x 0.9995 = 0.99949 ≈ 99.95%  ✓
```

---

## Key Takeaways

1. **Availability is multiplicative across serial dependencies.** In a microservices architecture, this is the dominant factor. Five services at three nines give you only two-and-a-half nines.

2. **MTTR matters more than MTBF.** You cannot prevent all failures, but you can detect and recover from them in seconds. Invest in automated detection, rollback, and graceful degradation.

3. **Error budgets convert reliability from a vague goal into a quantitative resource.** When you have budget, ship. When you do not, stabilize. This aligns incentives between product and engineering.

4. **Correlated failures destroy redundancy math.** The formula `1 - (1-p)^n` assumes independence. Shared infrastructure, shared software, and shared configuration create correlation. Design for independent failure domains.

5. **Each nine costs roughly 10x more.** Set your availability target based on business value, not engineering pride. Over-engineering reliability is as wasteful as under-engineering it.

6. **The math keeps you honest.** Intuition about reliability is consistently wrong. A 30-day MTBF means a 37% chance of surviving the month. Five three-nines services in series yield only two-and-a-half nines. Run the numbers.

---

## References

- Beyer, B., Jones, C., Petoff, J., Murphy, N.R. (2016). *Site Reliability Engineering: How Google Runs Production Systems*. O'Reilly.
- Beyer, B., Murphy, N.R., Rensin, D., Kawahara, K., Thorne, S. (2018). *The Site Reliability Workbook*. O'Reilly.
- Sloss, B. (2017). "SLOs, SLIs, SLAs, oh my!" — Google Cloud Blog.
- Tene, G. "How NOT to Measure Latency." Strange Loop 2015.
- Google Cloud Architecture Framework: Reliability Pillar (2024).
- AWS Well-Architected Framework: Reliability Pillar (2024).
- Nygard, M. (2018). *Release It! Design and Deploy Production-Ready Software*, 2nd Edition. Pragmatic Bookshelf.
