# 13 — SLO Engineering: The Discipline That Turns Monitoring into SRE

> An SLO without an error budget is decoration. An error budget without burn-rate alerts is a sticky note. Burn-rate alerts without an SLO platform are a forest of YAML. The whole stack only earns its keep when these four pieces — SLI definition, SLO target, error budget, generated alerts — are wired together as **one declarative artifact** that engineering, product, and SRE can look at simultaneously and trust.

This chapter is the engineering version of *how to make SLOs work*. The previous chapter (`doc 12`) built the alerting engine. This one *generates the rules that engine evaluates* from a single source of truth: the SLO definition.

If chapter 12 gave you the tool, chapter 13 gives you the practice.

---

## Table of Contents

1. [The thesis](#1-thesis)
2. [SLI taxonomy: choosing the right one](#2-sli-taxonomy)
3. [Event-based vs windowed SLIs](#3-event-vs-windowed)
4. [The seven-step SLO authoring process](#4-authoring-process)
5. [The error-budget calculus](#5-error-budget-calculus)
6. [Burn-rate alert generation (the math, fully derived)](#6-burn-rate-derivation)
7. [SLO compilers: Sloth, Pyrra, Nobl9, OpenSLO](#7-slo-compilers)
8. [The SLO repository pattern](#8-slo-repo)
9. [Multi-objective SLOs and composite SLIs](#9-composite)
10. [User-journey SLOs vs service SLOs](#10-journey-vs-service)
11. [Dependency math: serial, parallel, fan-out](#11-dependency-math)
12. [Error-budget policy: the social contract](#12-error-budget-policy)
13. [SLO reviews and reliability backlog](#13-slo-reviews)
14. [Common SLO formulations (cookbook)](#14-cookbook)
15. [Anti-patterns](#15-anti-patterns)
16. [Worked example: full /checkout SLO file](#16-worked-example)
17. [Pitfalls](#17-pitfalls)
18. [Mental models](#18-mental-models)

---

## 1. Thesis

Three claims a Staff Engineer must defend:

1. **The SLO is the artifact, not the dashboard.** A YAML file in Git declares the SLI, the target, the window, the budget policy. From it, the platform *generates*: alerts, dashboards, reports, error-budget burn graphs. Hand-rolling any of those is anti-pattern.
2. **An SLO target is a business decision, not an engineering one.** 99.9% vs 99.99% is roughly a 10× cost difference (`doc 00 §15`). The product team must own the choice; engineering owns the *cost curve* that informs it.
3. **The error budget is the lever.** Reliability work vs feature work is no longer an argument when the budget is exhausted — it's a *policy*: "if budget < 0, freeze risky deploys." This is the cultural bit. Without it, SLOs are theater.

If your team has SLOs but still has the "should we slow down?" debate every quarter, you do not yet have SLO engineering. You have SLO documentation. This chapter closes the gap.

---

## 2. SLI Taxonomy

Not every measurement is an SLI. Pick from this taxonomy.

### 2.1 The five classic SLI types

| Type | Question it answers | Typical formula |
|---|---|---|
| **Availability** | Did the request succeed? | `successes / total` |
| **Latency** | Did it complete in time? | `requests_below_threshold / total` |
| **Quality** | Did the response have correct content? | `correct_responses / total` |
| **Freshness** | Is the data current? | `events_within_age_threshold / total` |
| **Throughput / Coverage** | Did we process the work we were given? | `processed / received` (durable jobs) |

Different services need different SLIs. A web API needs availability + latency. A search index needs availability + latency + freshness. A batch ETL needs throughput + freshness, but *not* per-event latency.

### 2.2 The "good event / total event" structure

Every SLI in this list reduces to a *ratio of good events over total events*. This is not aesthetic — it's a hard requirement. Burn-rate math, multi-window alerting, error-budget arithmetic all depend on the ratio formulation. *Always* express SLIs this way.

```
SLI = good / total

Bad:  "average latency"           ← cannot compute a budget on this
Good: "fraction below 500ms"      ← a clean ratio with a clean budget
```

If your candidate SLI cannot be expressed as a ratio, it is not yet an SLI. Either redefine it as one (impose a threshold) or it's a *health metric*, not an SLI.

### 2.3 Latency SLIs: the threshold hides the work

A latency SLI requires a threshold: "fraction below 500ms." Choosing the threshold is more important than the SLO target itself.

| Threshold pick | Implication |
|---|---|
| Service p99 historical | "We promise to be no worse than yesterday." Weak — fails to drive improvement. |
| Customer-facing UX research ("page load < 1s") | Strong — ties to user experience. |
| Per-endpoint, per-tier (browse < 200ms, checkout < 500ms) | Strongest — recognizes endpoints have different tolerances. |

Don't pick one threshold for the whole service. Different endpoints have different latency profiles and tolerances; one threshold either flatters fast endpoints or punishes slow-but-fine ones.

### 2.4 Multi-threshold latency: the better idiom

Modern practice: *multiple* latency objectives.

```
checkout:
  latency:
    objective_1:
      threshold: 200ms
      target:    99%       # 99% of checkouts under 200ms
    objective_2:
      threshold: 1000ms
      target:    99.9%     # 99.9% under 1s (catastrophic-tail bound)
```

Two thresholds = two budgets = two burn-rate alerts. Catches both gradual drift (the 99% goal) and rare-tail catastrophe (the 99.9%). This idiom is what serious latency engineering looks like.

---

## 3. Event-Based vs Windowed SLIs

Two different mathematical formulations. Use the right one.

### 3.1 Event-based (the default)

```
SLI = good_events / total_events
```

The numerator and denominator are *counters*; you compute the ratio at any window. Easy to alert on, easy to budget.

```promql
sli = sum(rate(http_requests_total{outcome="success"}[5m]))
      /
      sum(rate(http_requests_total[5m]))
```

This is what burn-rate alerts are designed for. **Default to this**.

### 3.2 Windowed (occupancy)

```
SLI = (window_seconds_with_service_healthy) / (total_window_seconds)
```

Here the SLI measures *how long the service was healthy*, not how many requests succeeded. Used when:
- Traffic is sparse or bursty (a 1 RPS service can't event-based meaningfully).
- The product promise is uptime, not request success ("99.9% uptime").
- The system is internal and traffic shape isn't predictable.

The math is different: a 30-day window with 99.9% target = 43.2 minutes of allowable downtime. A 1-second outage during a 5-second window of traffic costs you the whole window. **Windowed SLIs over-penalize brief outages on low-traffic services.**

### 3.3 The choice in 2026

Use **event-based for high-traffic services** (web, API gateway, mobile backends). Use **windowed for low-traffic / heartbeat services** (cron jobs, internal admin APIs). For batch / streaming, use *throughput-based* SLIs (events processed within deadline).

A common mistake: using windowed for a high-traffic service because it "feels simpler." The result: every brief deploy blip costs minutes of budget, alerting becomes flappy, and engineers conclude "SLOs don't work."

---

## 4. The Seven-Step SLO Authoring Process

A repeatable workflow. Use it the first time you introduce SLOs to a team; use it again when adding a new service.

```
1.  Identify the user journey (not the service)
       ↓
2.  Choose 2–4 SLIs for that journey
       ↓
3.  For each SLI, propose 3 candidate targets
       (the "tight / current / loose" triad)
       ↓
4.  Cost-model each candidate for the next 12 months
       ↓
5.  Product + Eng decision: pick a target
       ↓
6.  Write the SLO YAML; commit to the SLO repo
       ↓
7.  Generate alerts, dashboards, reports;
    review with on-call before going live
```

### 4.1 Identify the user journey

The mistake here: writing an SLO per service. Services aren't what users see. *Journeys* are. A "checkout" journey crosses 6 services; a "browse" journey crosses 3 different ones. The user notices the journey breaking, not which microservice was at fault.

Start by listing the top 5 journeys by traffic + business value:

```
1. Browse / search           (read path, high traffic, low risk)
2. Checkout                  (write path, lower traffic, high revenue)
3. Sign-up                   (low traffic, very high acquisition cost)
4. Login                     (every session, high blast radius)
5. Account management        (low traffic, GDPR/compliance-relevant)
```

Then write SLOs *per journey*. The implementation will pull metrics from many services; that's fine.

### 4.2 Choose 2–4 SLIs per journey

Three is the sweet spot. Common combinations:

| Journey | SLI 1 | SLI 2 | SLI 3 |
|---|---|---|---|
| Browse | Availability | Latency P99 < 1s | Freshness (catalog ≤ 5 min stale) |
| Checkout | Availability | Latency P99 < 2s | Quality (order written to DB) |
| Sign-up | Availability | Latency P95 < 3s | Email-delivery (confirmation in 60s) |

More than 4 SLIs per journey = nobody remembers them = they don't drive behavior. Less than 2 = you're missing a dimension.

### 4.3 The "tight / current / loose" triad

For each SLI, propose three candidate targets:

```
SLI: checkout availability

  TIGHT:    99.99%    (52 minutes / year of allowable error)
            cost: ~$X to add fail-over for X dependency
  CURRENT:  99.9%     (8.77 hours / year — what we hit in last 90d)
            cost: ~$X/3 — basically free
  LOOSE:    99.5%     (1.83 days / year)
            cost: $0 — but customer NPS may suffer
```

Force the conversation. Product picks; Eng commits. The choice is rarely "tight" — usually "current" with one bumped tighter as the strategic priority. The tight option exists to prove there's a real cost to higher SLOs.

### 4.4 Cost-model each candidate

This is the step most teams skip. The rough rule (`doc 00 §15`): *each additional 9 costs 3-10× engineering effort*. Concretely:

- 99.0 → 99.9% may require: better deploy automation, single-region HA.
- 99.9 → 99.99% adds: multi-region active/active, zero-downtime migrations, queue-backed writes, cell architecture.
- 99.99 → 99.999% adds: fault domains beyond regions, custom retry / hedge layers, 24/7 dedicated SRE.

A 12-month cost projection ($, headcount, deferred features) per candidate makes the decision honest. Don't write SLOs without it.

---

## 5. The Error-Budget Calculus

Math you can do in your head, on a whiteboard, in a meeting.

### 5.1 Budget formula

```
total_events_in_window  = traffic_per_second × window_seconds
allowed_bad_events       = total_events × (1 − SLO)
remaining_budget         = allowed_bad − bad_events_so_far_in_window
budget_consumed_fraction = bad_events_so_far / allowed_bad
```

Concrete: 1000 RPS, 28-day window, SLO = 99.9%.

```
total      = 1000 × 86400 × 28 = 2.42 billion events
allowed    = 2.42B × 0.001     = 2.42 million bad events
hourly_rate_at_steady_state = allowed / hours_in_window = 2.42M / 672 ≈ 3603 / hour
```

3603 errors per hour = the *steady-state allowed*. Faster than that = burning budget; slower than that = saving. Burn rate is just the multiplier.

### 5.2 Budget lookahead

A useful derived metric: *days remaining in budget at current burn rate*.

```
days_remaining = remaining_budget / (current_bad_events_rate × seconds_per_day)
```

If the dashboard shows "1.2 days of budget left at current burn rate," you have leadership's attention much more reliably than "budget consumed: 84%."

### 5.3 Rolling vs calendar windows

Two flavors:

- **Rolling 28 days.** "How are we doing right now, looking back?" Smooth; alerts based on this don't reset arbitrarily. Default for engineering.
- **Calendar month.** "How were we in May?" Resets each month. Useful for compliance / SLA reporting.

Pick rolling for engineering use. Use calendar windows only for external reports.

### 5.4 Why "28 days" instead of "30"?

28 = 4 × 7 days. Removes the day-of-week effect (a Friday outage doesn't get diluted by a quiet weekend in the budget math). The convention came from Google's *Implementing SLOs* (Beyer 2018); it's now standard. 7 / 28 / 90 are the typical windows.

---

## 6. Burn-Rate Alert Generation: The Math, Fully Derived

Chapter 12 introduced burn rate. This section derives the thresholds from first principles, so you can defend them to a product manager who asks "where does the 14.4 come from?"

### 6.1 The setup

Given:
- SLO = 99.9% over 28 days (window W = 30 × 24 = 720 hours; using 30 for simpler math)
- Allowed bad fraction = 1 − 0.999 = 0.001
- We want a "fast burn" page that fires when X% of the budget would be consumed in time T.

### 6.2 The derivation

Fast-burn rule: *fire if* `(bad_events_in_T) / (total_events_in_T) > acceptable_consumption_rate`.

Reframe in terms of the burn-rate multiplier.

```
budget_consumed_fraction = (current_bad_rate × T) / (allowed_bad_in_full_window)
                         = (current_bad_rate × T) / (steady_state_rate × W)
                         = (burn_rate × steady_state_rate × T) / (steady_state_rate × W)
                         = burn_rate × (T / W)

We want fire-iff: budget_consumed_fraction ≥ X
                  burn_rate × (T / W) ≥ X
                  burn_rate ≥ X × (W / T)
```

For X = 2% (page on 2% budget consumed in 1 hour, 30-day window):

```
burn_rate ≥ 0.02 × (720 / 1) = 14.4
```

That's the 14.4. Generalizes:

| Budget % per page | Window T | W (hours) | Threshold |
|---|---|---|---|
| 2% | 1h | 720 | 14.4 |
| 5% | 6h | 720 | 6 |
| 10% | 3 days = 72h | 720 | 1 |
| 100% (silent failure) | 14 days = 336h | 720 | 1 (long-tail catch) |

For a **28-day** window (W = 672), 2% / 1h = 13.44; 5% / 6h = 5.6. Most generators round to 14.4 / 6 — close enough.

### 6.3 The two-window protection

Why both 1h *and* 5m must be hot for fast-burn? Without the short window, the alert fires *after* the burst has stopped (long window still elevated, but the bleed already healed). This causes pages on already-resolved problems — the worst kind, because on-call investigates a phantom.

The short window confirms *ongoing* burn. Both must be hot to page; either alone is insufficient.

### 6.4 Alert latency math

```
First page latency = max(short_window_size, evaluation_interval) + group_wait

Fast burn:    max(5m, 15s) + 30s     ≈ 5m30s
Slow burn:    max(30m, 15s) + 30s    ≈ 30m30s
Ticket:       max(6h, 1m) + ...      ≈ 6h
```

**Fast-burn pages cannot fire faster than the short-window length.** If you want a 1-minute page, your short window must be 1 minute. But a 1-minute window is statistically noisy on low-traffic services — you'll false-page on a single bad request. Sub-5-minute pages are reserved for very-high-RPS services.

---

## 7. SLO Compilers: Sloth, Pyrra, Nobl9, OpenSLO

The tools that turn the SLO YAML into rule files, dashboards, and reports.

| Tool | Open-source | Generates | Best for |
|---|---|---|---|
| **Sloth** | Yes | Prom/Mimir rules + Grafana dashboards | Pure Prometheus shops, simple file-based workflow |
| **Pyrra** | Yes | Prom rules + Kubernetes-native CRDs | K8s-native shops; SLO-as-CRD |
| **Nobl9** | No (SaaS) | Multi-data-source SLOs, reports, error budget tracking | Enterprise; multi-vendor stacks; non-Prom data sources |
| **OpenSLO** | Yes (spec) | Itself a *spec* — Sloth/Pyrra/Nobl9 implement it | The shared YAML schema |

### 7.1 OpenSLO is the format that lets you switch tools

`OpenSLO` is a CNCF-incubating spec (≥ v1) for SLO YAML. Sloth, Nobl9, Pyrra all consume it (with extensions). Writing OpenSLO-compliant YAML is the cheapest hedge against tool churn.

```yaml
apiVersion: openslo/v1
kind: SLO
metadata:
  name: checkout-availability
  displayName: Checkout availability
spec:
  description: Fraction of /checkout requests that succeed.
  service: checkout
  indicator:
    metadata: { name: checkout-availability }
    spec:
      ratioMetric:
        counter: true
        good:
          metricSource:
            type: Prometheus
            spec:
              query: 'sum(rate(http_requests_total{service="checkout",code=~"2.."}[5m]))'
        total:
          metricSource:
            type: Prometheus
            spec:
              query: 'sum(rate(http_requests_total{service="checkout"}[5m]))'
  timeWindow:
    - duration: 28d
      isRolling: true
  budgetingMethod: Occurrences
  objectives:
    - displayName: "99.9% over 28d"
      target: 0.999
```

Run `sloth generate -i checkout-availability.yaml -o rules.yaml` and you get a couple-hundred-line rule file with recording rules and burn-rate alerts. Commit both source and generated to the same repo so the PR diff shows what changed.

---

## 8. The SLO Repository Pattern

A canonical layout for SLO-as-code:

```
slo-repo/
  README.md
  policies/
    error-budget-policy.md         # the social contract (§12)
    review-cadence.md
  journeys/
    checkout/
      slo.yaml                     # OpenSLO file
      generated/
        rules.yaml                 # produced by sloth/pyrra
        dashboard.json             # grafana dashboard
      runbooks/
        availability-burn.md
        latency-burn.md
    browse/
      slo.yaml
      generated/
        rules.yaml
        dashboard.json
  tools/
    generate.sh                    # CI step: regenerate all
    test.sh                        # promtool test rules
```

A change to an SLO is a PR. CI:
1. Validates OpenSLO YAML.
2. Regenerates rules + dashboards.
3. Runs `promtool test rules` against the rules.
4. Asks for two reviewers: the journey owner + a platform/SRE reviewer.

This sounds heavy for a 3-person team. For 50+ engineers it's the difference between SLOs that are real and SLOs that drift.

---

## 9. Multi-Objective SLOs and Composite SLIs

A journey often needs *multiple objectives on the same SLI* (the multi-threshold latency from §2.4) or *one SLI that is itself a composition* of multiple measurements.

### 9.1 Multiple objectives, one SLI

```yaml
indicator:
  ratioMetric:
    good:   { ... below 500ms ... }
    total:  { ... all ... }
objectives:
  - target: 0.99       # 99% under 500ms
    displayName: "Standard"
  - target: 0.999
    indicator:
      ratioMetric:
        good:  { ... below 2000ms ... }
        total: { ... all ... }
    displayName: "Catastrophic-tail"
```

Two budgets, two burn rates, two pages. Catches gradual drift and rare-tail catastrophe with one SLO file.

### 9.2 Composite SLIs

Sometimes a single ratio is not enough. *Quality* may require multiple checks: response is 2xx *and* contains the right schema *and* latency < 1s.

```yaml
indicator:
  spec:
    composite:
      method: AND        # all conditions must be true for "good"
      components:
        - { ratioMetric: { good: '...status_2xx...', total: '...all...' } }
        - { ratioMetric: { good: '...schema_valid...', total: '...all...' } }
        - { ratioMetric: { good: '...latency_below_1s...', total: '...all...' } }
```

Composite SLIs are powerful but risky: more components = more places to go wrong without anyone noticing. Limit to 2-3 components; document each.

---

## 10. User-Journey SLOs vs Service SLOs

This is one of the most common arguments. Resolve it once, in the platform-team manifesto.

### 10.1 The journey SLO (preferred)

Measures the *user-perceived* outcome. Computed at the gateway / API edge / RUM layer.

```
Numerator: requests with end-to-end success (2xx and full content)
Denominator: total requests
Where: at the edge (CDN, ALB, Envoy gateway)
```

**Pro:** measures what the user actually feels.
**Con:** doesn't pinpoint which service is at fault when budget burns. (That's fine — that's diagnosis, not SLO design.)

### 10.2 The service SLO (per-service)

Measures the SRE-handoff: "did this service's slice of the chain succeed?"

```
Numerator: successful responses from this service to its caller
Denominator: total requests received by this service
Where: at the service itself
```

**Pro:** clear ownership; this team owns this number.
**Con:** the user doesn't care that "service B was 99.9%" if the journey was broken upstream.

### 10.3 The right configuration

Use **both**:
- One **journey SLO per top-5 journey**, owned by the journey's product/eng leadership.
- One **service SLO per service**, owned by the service team. Targets are usually tighter than the journey (because chained dependencies multiply downward).

Pages on journey-SLO budget burns. Tickets on service-SLO budget burns (so service owners know they're contributing to journey budget consumption). Don't conflate.

---

## 11. Dependency Math

The SLO target you can *commit* to depends on what you depend on.

### 11.1 Serial dependency

`Journey = A → B → C`. All three must succeed. SLO multiplies:

```
SLO_journey ≤ SLO_A × SLO_B × SLO_C

Three nines × three: 0.999³ ≈ 99.7%   ← *less* than three nines
Four nines × three:  0.9999³ ≈ 99.97%
```

Three components at 99.9% give *barely* two-and-a-half nines combined. **You cannot commit to higher SLO than your deps allow without redundancy.**

### 11.2 Parallel (redundant) dependency

`Journey = A or B`. Either succeeds. SLO multiplies on the *failure* probabilities:

```
P(failure) = (1 - SLO_A) × (1 - SLO_B)

Two nines × two: (0.01)² = 0.0001 → 99.99%   ← four nines from two!
```

This is *why* multi-region active/active is the only path to 99.99%+ SLOs.

### 11.3 Fan-out

`Journey = A → (B AND C in parallel) → D`. Inner steps are concurrent; both must succeed.

```
SLO_inner = SLO_B × SLO_C        (because *all* fan-outs must succeed)
SLO_journey = SLO_A × SLO_inner × SLO_D
```

Common in microservice meshes — a single page hits 30 services, all must respond. The math gets unforgiving fast. Defenses: *graceful degradation* (a fan-out failure doesn't fail the journey, it just removes a feature), *response hedging*, *dependency budget allocation* (each step gets a fraction of the journey budget).

### 11.4 Budget allocation

If `Journey` SLO is 99.9%, and it has four serial deps, each dep needs ~99.975% to keep the journey above 99.9%. **Each service team should know its allocated budget.** Make it explicit in the SLO repo.

---

## 12. Error-Budget Policy: The Social Contract

The single most important *non-technical* document in an SRE practice. The error-budget policy is the rulebook for what happens when budgets are exhausted.

### 12.1 What a policy looks like

```markdown
# Error Budget Policy — checkout journey

## Healthy budget (>50% remaining)
- Normal velocity
- Risk-taking encouraged
- New feature deploys at any cadence

## Half-burn (25–50% remaining)
- Engineering review for risky deploys
- Slack thread to platform team for any cross-cutting change
- Increased synthetic monitoring

## Low budget (<25% remaining)
- All risky deploys (schema migrations, infra changes) require
  team-lead sign-off
- Reliability work prioritized in next sprint planning
- Daily dashboard review at standup

## Exhausted budget (<0%)
- No new feature deploys until budget recovers
- Active incident response: dedicated reliability sprint
- Postmortem on the burn (even if no single incident)
- Status update to leadership weekly

## Recovery
- Budget back > 25%: lift restrictions
- Postmortem actions verified closed
```

### 12.2 The signing ceremony

The policy must be **signed by Product, Engineering, and SRE leadership.** Without this, the moment a freeze is invoked, Product will push back and engineering will cave. With it, the freeze is a pre-agreed mechanism, not an argument.

### 12.3 The budget-as-resource analogy

Frame the budget like any other resource (compute, storage, headcount). Engineering can *spend* it on velocity (ship faster, accept some failures) or *save* it (slow down, more conservative). The policy makes the spend rate visible and bounded.

---

## 13. SLO Reviews and Reliability Backlog

Quarterly cadence. Without it, SLOs drift.

### 13.1 The quarterly SLO review

90 minutes. Per top journey:

1. **Budget burn over the quarter.** Show the burn-down chart.
2. **Top contributors to burn.** Which incidents consumed the most budget?
3. **SLO target review.** Was the target right? Tighten, loosen, or hold?
4. **Dependencies changed.** New deps added; their SLOs in line with ours?
5. **Reliability backlog status.** What were last quarter's actions? Did they close?

The output: a **reliability backlog** — concrete eng work that drives down future budget burn. This backlog gets prioritized alongside features in the next quarter's planning. A team without a reliability backlog at the end of the SLO review didn't actually do the review.

### 13.2 What makes the backlog

- **Top-3 incident contributors → action items.** From postmortems.
- **Capacity headroom shortfalls** that risked an outage.
- **Toil items** identifiable as automation candidates.
- **Tech-debt pieces** that the budget burn fingered (e.g., "every cache miss adds 800ms; need to fix the TTL invalidation").

### 13.3 SLO target adjustment

It is fine — *good* — to adjust SLO targets at the quarterly review. The targets are not sacred. Tightening signals confidence; loosening signals reality. Loosening is *not* shame — it's a recalibration to ship more honest engineering. The bad version is *silent* drift: not enforcing the SLO and not loosening it either.

---

## 14. Common SLO Formulations (Cookbook)

Recipes you can drop in.

### 14.1 HTTP API availability

```
good:  http_requests_total{code!~"5..", service="..."}
total: http_requests_total{service="..."}
```

### 14.2 HTTP API latency (p99 < 500ms)

```
good:  histogram_count(rate(http_request_duration_seconds_bucket{le="0.5"}[5m]))
total: histogram_count(rate(http_request_duration_seconds_bucket[5m]))
```

(Or with native histograms: `histogram_fraction(0, 0.5, ...)`.)

### 14.3 gRPC availability

```
good:  grpc_server_handled_total{grpc_code="OK"}
total: grpc_server_handled_total
```

### 14.4 Async job freshness

```
good:  job_runs_total{lag_seconds_bucket="le_300", outcome="success"}
total: job_runs_total
```

### 14.5 Kafka consumer (lag-based)

```
good:  sum(rate(kafka_messages_consumed_total[5m]))
total: sum(rate(kafka_messages_produced_total[5m]))
```

(The "lag closes within tolerated window" SLI.)

### 14.6 Database availability

```
good:  pg_query_total{outcome="success"}
total: pg_query_total
```

### 14.7 RUM page-load

```
good:  rum_page_load_seconds_bucket{le="2.0"}
total: rum_page_load_seconds_bucket
```

### 14.8 Cron / batch correctness

```
good:  cronjob_runs_total{outcome="success"}
total: cronjob_runs_total{outcome=~"success|failure"}
```

(Excludes "skipped" / "still running.")

### 14.9 ML model freshness

```
good:  model_serve_total{model_age_below_threshold="true"}
total: model_serve_total
```

### 14.10 Synthetic uptime

```
good:  synthetic_check_total{outcome="success"}
total: synthetic_check_total
```

The key common pattern: **two PromQL queries that are both Prometheus *counters***. Anything that's a gauge (memory, queue depth) won't generate a clean SLI.

---

## 15. Anti-patterns

A consolidated list. Don't ship any of these.

1. **One target per service, hand-picked at random.** No cost model, no business buy-in.
2. **SLO target = current performance.** No room to improve, no signal of stretch.
3. **Tightening targets without cost discussion.** Engineering eats it; quality of work degrades.
4. **No error-budget policy.** SLOs become decoration; freeze never invoked.
5. **One SLI per service.** Misses dimensions (latency without availability, etc.).
6. **SLO scoped to internal service, not journey.** User feels broken journey; service SLOs all green.
7. **No multi-burn-rate alerts.** Either flapping or under-paging.
8. **SLO YAML hand-written without a compiler.** Recording rules drift; alerts get out of sync.
9. **No journey ownership.** "Who owns checkout availability?" "...nobody, it's everyone's job." That means no one.
10. **Targets set at first-launch and never revisited.** Drift. Six quarters later the target reflects nothing real.
11. **Dependency SLOs inconsistent with journey SLO.** Math doesn't work; surprises during incidents.
12. **Error-budget policy not signed.** Freeze never enforced; budget exhaustion is theater.
13. **Latency SLI on the mean.** Hides tail; goal misaligned with user pain.
14. **Mixing alerting on SLOs with old threshold alerts.** Pages double; on-call has two languages.
15. **No reliability backlog.** SLO reviews happen, nothing changes.

---

## 16. Worked Example: Full /checkout SLO File

Concrete, complete, production-ready.

```yaml
apiVersion: openslo/v1
kind: SLO
metadata:
  name: checkout-availability
  displayName: Checkout — Availability
  labels:
    journey: checkout
    tier: "1"
    team: payments
  annotations:
    runbook: https://runbooks.example.com/checkout-availability
    dashboard: https://grafana.example.com/d/checkout
    incident_tag: checkout-budget-burn
spec:
  description: |
    Fraction of /checkout requests that complete successfully (HTTP 2xx, body
    valid, end-to-end). Measured at the API gateway.
  service: checkout
  indicator:
    spec:
      ratioMetric:
        counter: true
        good:
          metricSource:
            type: Prometheus
            spec:
              query: |
                sum(rate(api_gateway_requests_total{
                  journey="checkout",
                  outcome="success"
                }[5m]))
        total:
          metricSource:
            type: Prometheus
            spec:
              query: |
                sum(rate(api_gateway_requests_total{
                  journey="checkout"
                }[5m]))
  timeWindow:
    - duration: 28d
      isRolling: true
  budgetingMethod: Occurrences
  objectives:
    - displayName: "99.9% over 28 days"
      target: 0.999

---
apiVersion: openslo/v1
kind: SLO
metadata:
  name: checkout-latency
  displayName: Checkout — Latency
  labels:
    journey: checkout
    tier: "1"
    team: payments
spec:
  description: |
    Fraction of /checkout requests completing in under 2 seconds end-to-end.
  service: checkout
  indicator:
    spec:
      ratioMetric:
        counter: true
        good:
          metricSource:
            type: Prometheus
            spec:
              query: |
                sum(rate(api_gateway_request_duration_seconds_bucket{
                  journey="checkout",
                  le="2.0"
                }[5m]))
        total:
          metricSource:
            type: Prometheus
            spec:
              query: |
                sum(rate(api_gateway_request_duration_seconds_count{
                  journey="checkout"
                }[5m]))
  timeWindow:
    - duration: 28d
      isRolling: true
  budgetingMethod: Occurrences
  objectives:
    - displayName: "99% under 2 s"
      target: 0.99
    - displayName: "99.9% under 5 s (catastrophic-tail)"
      target: 0.999
      indicator:
        spec:
          ratioMetric:
            counter: true
            good:
              metricSource:
                type: Prometheus
                spec:
                  query: |
                    sum(rate(api_gateway_request_duration_seconds_bucket{
                      journey="checkout",
                      le="5.0"
                    }[5m]))
            total:
              metricSource:
                type: Prometheus
                spec:
                  query: |
                    sum(rate(api_gateway_request_duration_seconds_count{
                      journey="checkout"
                    }[5m]))
```

Sloth or Pyrra ingests this and produces:
- 12 recording rules (5m, 30m, 1h, 6h, 1d, 3d windows × 3 SLI types).
- 6 alert rules (fast burn, slow burn, ticket; one per objective).
- A Grafana dashboard with budget burn-down, latency distribution, and recent incidents.
- A 28-day budget report.

The CI runs `sloth generate`, commits the generated outputs, and runs `promtool test rules` against test fixtures. The on-call sees consistent dashboards and pages every time.

---

## 17. Pitfalls

1. **Picking SLOs without a journey lens.** Service SLOs without journey SLOs are blind to user impact.
2. **No cost model behind targets.** Engineering pays the price of the SLO product chose.
3. **Composite SLIs that nobody understands.** Document every component; limit to 2-3.
4. **Calendar windows for engineering use.** Use rolling.
5. **Burn-rate alerts without dwell-time / `for:` clauses.** Flap.
6. **No anchor on long-window catch.** Slow-burn rule misses gradual drift.
7. **SLO YAML diverges from generated rules.** Always commit both; CI must re-generate.
8. **No reliability backlog.** SLOs are aspirational, no one invests in them.
9. **Error-budget freeze ignored.** Treat it as a hard rule, not a request.
10. **SLI at one tier (service), SLO at another (journey).** Math doesn't work; surprises.
11. **No DR / multi-region story for ≥99.99% SLOs.** You can't do it without redundancy.
12. **SLO targets set ambitious and never reviewed.** Become "stretch goals" engineering ignores.
13. **Latency SLO with one threshold.** Add a catastrophic-tail second threshold.
14. **Composite SLI counting "skipped" as bad.** Excludes legitimate non-events; corrupts ratios.
15. **No ownership.** "Who owns this SLO?" must always be one human (a team lead), not "the team."

---

## 18. Mental Models

> **The SLO is the artifact, not the dashboard.** YAML in Git → generated rules + dashboards + reports. Treat anything else as drift.

> **Targets are business decisions; engineering owns the cost curve.** Each nine costs 3-10× more. The product team picks; engineering shows the price tag.

> **The error budget is the lever.** Velocity vs reliability isn't an argument when budget < 0; it's a policy.

> **Burn rate is unitless.** It composes across services with different SLOs; it's the universal alert primitive.

> **Two windows, multiple burn rates.** Catches both fast and slow burns without flap.

> **User-journey SLOs page; service SLOs ticket.** The user notices journeys, not microservices.

> **Multi-region active/active is the only path to four nines.** Anything less is wishful thinking.

> **Quarterly reviews or it's theater.** Without a cadence, SLOs drift.

> **Reliability backlog is the output.** Without concrete engineering work falling out of the review, you didn't actually review.

Now go to `doc 12` (alerting) for the engine, `doc 14` (on-call) for the human layer that consumes the pages SLOs generate, or `doc 15` (incident response) for what happens when the budget burns hard.
