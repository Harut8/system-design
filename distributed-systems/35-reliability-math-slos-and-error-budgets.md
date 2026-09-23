# Reliability Math: SLOs, SLIs, Error Budgets, MTBF/MTTR, and Availability Engineering

## Executive Summary

Reliability engineering is fundamentally a mathematical discipline. Gut feelings about uptime, vague commitments to "high availability," and reactive firefighting produce systems that oscillate between over-engineering and catastrophic failure. This chapter develops the complete mathematical framework that Staff+ engineers need to reason quantitatively about reliability: from failure rate theory and availability algebra through SLI/SLO design, error budget management, multi-window alerting, and the architecture decisions that each level of availability demands. Every formula includes worked numerical examples drawn from production systems.

---

## Table of Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
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
11. [Interview Questions — Reliability Math, SLOs and Error Budgets](#11-interview-questions--reliability-math-slos-and-error-budgets)
12. [Real-world cases — incidents with numbers](#12-real-world-cases--incidents-with-numbers)

---

## Start here — the whole chapter in plain words

**The problem.** Every system fails sometimes. The questions that matter are: how much failure is
acceptable, how do you measure it, and what do you do when you are close to the limit? This chapter
turns "we want high availability" into numbers: a measurement (SLI), a target (SLO), an allowance
for failure (error budget), and alerts that fire when that allowance is being used up too fast. It
also shows how to multiply availabilities through a chain of services and how redundancy helps
only when copies fail independently.

**A real-world example.** An e-commerce checkout gets 1,000 requests per minute, which is
43,200,000 requests in a 30-day window. The team sets an SLO of 99.9% successful requests.

- **Error budget (§6):** 0.1% of 43.2M = **43,200 failed requests** allowed per 30 days (the same as
  43.2 minutes of total outage).
- **Serial chain (§3):** checkout calls gateway (99.95%) → cart (99.9%) → payment (99.9%) → order DB
  (99.95%). Multiplied: 0.9995 x 0.999 x 0.999 x 0.9995 = **99.70%**. That is about 129 minutes of
  expected failure per 30 days, 3x the budget, before a single bad deploy. The team must remove or
  harden links (caching, async writes, redundancy) before the SLO is even possible.
- **A bad deploy:** a release makes 5% of checkouts fail. Burn rate = 5% / 0.1% = **50x**: the whole
  30-day budget would be gone in 30 / 50 = 0.6 days (14.4 hours).
  - **Without SLO alerts:** nobody notices until customer complaints arrive 3 hours later.
    180 min x 1,000 x 5% = **9,000 failed checkouts = 20.8% of the month's budget**.
  - **With a multi-window burn-rate alert (§5)** (page when the error rate is above 14.4 x 0.1% =
    1.44% over both the last 1 hour and the last 5 minutes): the 1-hour average passes 1.44% after
    about 17 minutes, so the page fires having spent **864 failures = 2% of budget**. Automatic
    rollback 10 minutes later brings the total to 1,364 failures, about **3.2%** of budget.
- **Error budget policy (§6):** with 97% of the budget left, the team keeps shipping, but adds a
  canary step so the next bad release hits 1% of traffic instead of 100%.

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Availability | share of time (or of requests) the service works | share of days the bus actually runs |
| Nines | 99.9% = "three nines"; each nine cuts allowed downtime 10x | a stricter and stricter punctuality promise |
| MTBF | average time between failures | how many months between car breakdowns |
| MTTR | average time to get back to working | how long the car is in the garage each time |
| MTTD | average time until someone notices the failure | how long before you notice a flat tyre |
| SLI | the thing you measure: good events / all events | share of trains that arrive within 5 minutes of schedule |
| SLO | your internal target for the SLI over a window | "95% of trains on time, each month" |
| SLA | a contract with customers, with money back if missed | a refund if your parcel is late |
| Error budget | failure allowed by the SLO (1 - SLO) | a monthly allowance you are allowed to spend |
| Burn rate | how fast you spend the budget compared with the steady rate | spending your monthly allowance in 2 days |
| Serial dependency | request needs A **and** B **and** C | a relay race: one dropped baton loses the race |
| Parallel redundancy | request needs A **or** B | a spare tyre |
| Correlated failure | one cause breaks all the copies at once | the spare tyre is flat too, for the same reason |
| Quorum | a majority of replicas must agree | a committee needs more than half its members present to vote |
| Headroom | spare capacity kept for spikes and failures | not booking every seat on a plane, in case a flight is cancelled |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| `A` | availability, 0 to 1 (or %) | 0.999 – 0.9999 | 0.999 = 99.9% |
| `A_1 ... A_n` | availabilities of individual components | 0.999 each | 3 services at 0.999 in series → 0.997 |
| `MTBF` | mean time between failures (here: mean uptime) | hours to months | 730 h ≈ one failure a month |
| `MTTF` | mean time to failure (strict name for mean uptime) | same as above | — |
| `MTTR` | mean time to recovery | minutes to hours | 1.5 h per incident |
| `MTTD` | mean time to detect | 1 – 30 min | 7.6 min average, 3 min median |
| `λ` (lambda) | failure rate = 1 / MTBF | per hour | MTBF 730 h → λ = 0.00137/h |
| `t` | length of a time period | hours | 168 h = one week |
| `R(t)` | chance of no failure during `t` = e^(-λt) | 0 – 1 | R(720 h) = 37% for MTBF 730 h |
| `e` | Euler's number, 2.718... | — | e^(-1) = 0.37 |
| SLI | good events / total events | 0 – 1 | 998,500 / 1,000,000 = 99.85% |
| SLO | target for the SLI over a window | 99% – 99.99% | 99.9% over 30 days |
| Error budget | 1 - SLO | 0.1% for 99.9% | 43.2 min or 10,000 of 10M requests per 30 days |
| Burn rate | observed error rate / (1 - SLO) | 1x = on track | 1.44% errors on a 99.9% SLO → 14.4x |
| Long / short window | the two look-back periods an alert checks | 1 h / 5 min, 6 h / 30 min, 3 d / 6 h | page only if both windows are above the threshold |
| `n` | number of replicas or components | 2 – 7 | 5 replicas |
| `p` | probability one component is down | 0.001 | a 99.9% server has p = 0.001 |
| `C(n,k)` | number of ways to pick `k` of `n` | — | C(4,2) = 6 |
| `Q` | quorum size = floor(n/2) + 1 | — | n = 5 → Q = 3 |
| N+1, N+2 | capacity for load (N) plus 1 or 2 spares | — | need 4, run 5 |
| RF | replication factor (copies of each piece of data) | 3 | RF=3: three copies |
| p50 / p95 / p99 / p99.9 | latency that 50% / 95% / 99% / 99.9% of requests beat | ms | p99 = 300 ms |
| Utilization | how busy a resource is (0 – 100%) | keep CPU ≤ 60 – 70% | 1,750 of 2,500 req/s = 70% |
| Month (in tables) | 30 days for error budgets; 30.44 days in the nines table | — | 99.9%: 43.2 min vs 43.8 min |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. Reliability as a Mathematical Framework

> **In plain words.** Saying "we want high availability" does not help anyone decide anything. Put a number on it, such as 99.9% of requests succeed, and you can decide how much to spend, when to ship and when to stop. 100% is never the right goal, because it would mean never changing anything.
>
> **Real-world example.** A video platform promises 99.9% uptime. That allows about 43 minutes of failure per 30 days. The team can see that a 10-minute risky migration fits in that allowance, while a plan to go multi-region (for 99.99%, about 4 minutes) would cost far more than the few extra minutes are worth to viewers.

### Why Reliability Must Be Quantified

"We aim for high availability" is not a reliability strategy. It is a wish. Without quantification, teams cannot answer the questions that determine architecture, staffing, and investment:

- How much downtime can we tolerate before losing revenue?
- Is our current system reliable enough, or are we over/under-investing?
- Should we ship this feature now, or stabilize first?
- Does adding this dependency make us more or less reliable?

Reliability without math produces two pathological outcomes. Teams either over-engineer (building five-nines infrastructure for a best-effort analytics pipeline) or under-invest (running a payment system on single-instance databases because "it hasn't gone down yet"). Both waste money. The math tells you exactly where you stand and what to spend.

### The Exponential Cost of Each Nine

Each additional nine of availability costs much more than the previous one. A common rule of thumb says "each nine costs about 10x"; the illustrative chart below uses a gentler doubling. The exact multiplier varies by system and is not a law, but the direction is consistent: every nine is harder than the last.

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

> **In plain words.** Two numbers describe most outages: how often things break (MTBF) and how long they stay broken (MTTR). Availability is uptime divided by total time. Cutting the time to recover is usually cheaper than preventing every failure.
>
> **Real-world example.** A payments API fails about every 200 hours and takes 1 hour to fix: 200 / 201 = 99.50% available. To reach 99.9% they could make failures 5x rarer (every ~1,000 hours), or make recovery 5x faster (12 minutes) with automatic rollback. The second is usually much easier.

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

Strategy B is almost always cheaper and more tractable. You cannot prevent all failures, but you can detect and recover from them faster. This is why large operators invest heavily in observability and automated remediation rather than trying to eliminate all failure modes.

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

Note on definitions: in this formula "MTBF" means the average *uptime* between failures (strictly, MTTF — mean time to failure). Some texts define MTBF = MTTF + MTTR, in which case A = MTTF / MTBF. When MTTR is tiny compared with MTBF, the difference is negligible.

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

> **In plain words.** "Three nines" (99.9%) means the service may be down 0.1% of the time. Each extra nine cuts the allowed downtime by 10x. When a request passes through several services one after another, their availabilities multiply and the total gets worse; when you run copies side by side, it gets better.
>
> **Real-world example.** A checkout goes through 3 services at 99.9% each: 0.999^3 = 99.7%, about 2.2 hours down per month instead of 43 minutes. Run two copies of one service instead of one: 1 - 0.001^2 = 99.9999% for that step, if the copies fail independently.

### The Complete Availability Table

| Availability | Common Name | Downtime/Year | Downtime/Month | Downtime/Week | Downtime/Day | Typical Systems |
|:---:|:---:|:---:|:---:|:---:|:---:|:---|
| 99% | Two Nines | 3d 15h 36m | 7h 18m | 1h 41m | 14m 24s | Internal tools, batch pipelines |
| 99.5% | Two-and-a-half | 1d 19h 48m | 3h 39m | 50m 24s | 7m 12s | Non-critical web apps |
| 99.9% | Three Nines | 8h 45m 36s | 43m 50s | 10m 5s | 1m 26s | SaaS products, APIs |
| 99.95% | Three-and-a-half | 4h 22m 48s | 21m 55s | 5m 2s | 43s | E-commerce, user-facing services |
| 99.99% | Four Nines | 52m 34s | 4m 23s | 1m 0.5s | 8.6s | Financial APIs, core infrastructure |
| 99.999% | Five Nines | 5m 15.6s | 26.3s | 6s | 0.86s | Telecom switches, payment rails |
| 99.9999% | Six Nines | 31.5s | 2.6s | 0.6s | 0.086s | Pacemakers, flight control |

How the columns are computed: year = 365 days; month = 1/12 of a 365.25-day year = 30.44 days (730.5 hours). That is why 99.9% gives 43m 50s (43.8 min) per month here but **43.2 min** in the error-budget tables of Section 6, which use a 30-day window. Both are correct; always state which window you mean.

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

> **In plain words.** An SLI is the thing you measure: good events divided by all events, for example "requests that succeeded in under 300 ms" divided by "all requests". The hard part is deciding what counts as good and what counts at all. Measure latency with percentiles, not averages.
>
> **Real-world example.** A chat app serves 1,000,000 messages a day and 998,500 are delivered in under 1 second: SLI = 99.85%. If load-balancer health checks were counted as requests too, the number would look better than what users feel.

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

> **In plain words.** An SLO is the target for an SLI over a time window, such as "99.9% of requests succeed, measured over the last 30 days". Set it from what users need, not from what the system happens to do today. Alerts should fire when you are using up the allowed failures too fast, not on every blip.
>
> **Real-world example.** A 99.9% SLO allows 0.1% errors. If errors jump to 1.44%, you are failing 14.4x faster than allowed and would use a month's allowance in about 2 days. That deserves a page. A 5-minute blip at 0.2% does not.

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

Budget consumed = burn rate x long window / SLO window, e.g. 14.4 x 1h / 720h = 2%, 6 x 6h / 720h = 5%, 1 x 72h / 720h = 10%. The short window is 1/12 of the long window. The Google SRE Workbook (Chapter 5, "Alerting on SLOs") recommends the three rows 14.4x (1h/5m, page), 6x (6h/30m, page) and 1x (3d/6h, ticket); the 3x / 1-day row is an optional extra tier built with the same rule.

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

> **In plain words.** The error budget is the amount of failure the SLO allows: 1 minus the SLO. Teams spend it on risky changes. While budget is left, ship; when it runs out, stop feature work and fix reliability.
>
> **Real-world example.** An e-commerce site with a 99.9% SLO and 10M requests per 30 days may fail 10,000 requests (or be fully down about 43 minutes). A bad deploy already caused 8,300 failures this month, so 83% is spent: the team freezes risky deploys until the window rolls forward.

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

> **In plain words.** List how things can break, how often, and what else breaks with them. Most big outages start with a change (a deploy or a config push), and a single shared cause can take down all your "redundant" copies at once.
>
> **Real-world example.** Two database replicas at 99.9% each should give 99.9999% together. But if the same bad config is pushed to both, the chance of both failing is close to the chance of one failing: about 99.9%, a 1,000x difference in downtime.

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

A large share of production outages start with a change made by people. The Google SRE book estimates that roughly 70% of outages are due to changes in a live system. Published postmortems from many providers show the same pattern: config pushes and deploys, not hardware, cause most big incidents.

The most common change-related categories (order varies by organization):

1. **Configuration changes**: YAML typo, wrong feature flag value, incorrect connection string.
2. **Failed deployments**: Untested code path, incompatible schema migration, missing environment variable.
3. **Operational procedures**: Wrong runbook, wrong cluster, misread dashboard.

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

> **In plain words.** Extra copies help only if the system can actually switch to them and they do not fail for the same reason. Quorum systems (Raft, etcd, ZooKeeper) need a majority alive, so where you place replicas matters as much as how many you have.
>
> **Real-world example.** A bank ledger runs 3 replicas: 2 in Region A, 1 in Region B. If Region A goes down, 1 of 3 is left, which is not a majority, so the ledger stops. Spreading them 1+1+1 across three regions lets it survive any single region outage.

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

  Trap: Region A holds 2 of the 3 replicas. If Region A goes down, only
  1 replica is left, and 1 of 3 is NOT a quorum. So a Region A outage
  alone takes the whole system down.

  P(system down) ≈ P(Region A down)                                  [loses 2 replicas]
                 + P(Region B down) x P(any Region A replica down)  [loses 1 + 1]
                 + P(2+ replicas fail independently, regions up)

  ≈ 0.0001
  + 0.0001 x 0.002
  + C(3,2)(0.001)^2(0.999) + (0.001)^3

  ≈ 0.0001 + 0.0000002 + 0.000003
  ≈ 0.000103     (exact enumeration of all cases: 0.0001032)

  A ≈ 1 - 0.000103 = 99.990%   (four nines — no better than one region)

  A naive calculation that forgets the region holding the majority
  (only counting "both regions fail" + independent replica failures)
  gives 99.9997%, which is about 30x too optimistic on downtime.
```

With two regions, **no** placement survives the loss of the region that holds the majority. To survive any single region outage you need a third region: for example 1+1+1 replicas across 3 regions. With the same numbers (replica 99.9%, region 99.99%), 1+1+1 gives about 99.9996%, because now two things must fail at once to lose quorum.

---

## 9. Capacity Planning for Reliability

> **In plain words.** A service that is 95% busy on a normal day has nothing left when a server dies or traffic spikes. Plan so that peak traffic still fits after losing one server, with spare room on each one.
>
> **Real-world example.** An IoT ingest service must handle 10,000 messages/s at peak. Each server can do 2,500/s but should run at most 70% busy (1,750/s). That needs 6 servers, plus 1 spare for failures: 7, not the naive 4.

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

> **In plain words.** Each availability target implies a certain architecture, monitoring setup and deploy process. Work out the critical path, multiply the availabilities, and remove or harden the weakest links until the product meets the target.
>
> **Real-world example.** A ride-hailing dispatch path with gateway 99.95%, matching 99.99%, pricing 99.99% and trip DB 99.95% multiplies to about 99.88%, short of a 99.95% goal. Making pricing a soft dependency (fall back to a cached fare estimate) and adding a DB standby moves it closer.

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
| Payment processor APIs (typical) | Often advertise 99.99%–99.999% | Advertised uptime targets, not always contractual SLAs; check the vendor's status page and contract |

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

  Result: 0.999999 x 0.99999 x 0.9995 = 0.999489 ≈ 99.949%

  This is just UNDER the 99.95% target: the Order DB alone, at its SLA
  of 99.95%, uses the whole budget. Either rely on the DB's measured
  (usually better) availability, or remove it from the synchronous path
  (e.g., accept the order into a durable queue and write to the DB async).
  The lesson: a dependency whose SLA equals your SLO leaves you zero room.
```

---

## Key Takeaways

1. **Availability is multiplicative across serial dependencies.** In a microservices architecture, this is the dominant factor. Five services at three nines give you only two-and-a-half nines.

2. **MTTR matters more than MTBF.** You cannot prevent all failures, but you can detect and recover from them in seconds. Invest in automated detection, rollback, and graceful degradation.

3. **Error budgets convert reliability from a vague goal into a quantitative resource.** When you have budget, ship. When you do not, stabilize. This aligns incentives between product and engineering.

4. **Correlated failures destroy redundancy math.** The formula `1 - (1-p)^n` assumes independence. Shared infrastructure, shared software, and shared configuration create correlation. Design for independent failure domains.

5. **Each nine costs much more than the last** (rule of thumb: up to ~10x). Set your availability target based on business value, not engineering pride. Over-engineering reliability is as wasteful as under-engineering it.

6. **The math keeps you honest.** Intuition about reliability is consistently wrong. A 30-day MTBF means a 37% chance of surviving the month. Five three-nines services in series yield only two-and-a-half nines. Run the numbers.

---

## References

- Beyer, B., Jones, C., Petoff, J., Murphy, N.R. (2016). *Site Reliability Engineering: How Google Runs Production Systems*. O'Reilly.
- Beyer, B., Murphy, N.R., Rensin, D., Kawahara, K., Thorne, S. (2018). *The Site Reliability Workbook*. O'Reilly.
- "SLOs, SLIs, SLAs, oh my — CRE life lessons." Google Cloud Blog (2017).
- Tene, G. "How NOT to Measure Latency." Strange Loop 2015.
- Google Cloud Architecture Framework: Reliability Pillar (2024).
- AWS Well-Architected Framework: Reliability Pillar (2024).
- Nygard, M. (2018). *Release It! Design and Deploy Production-Ready Software*, 2nd Edition. Pragmatic Bookshelf.
