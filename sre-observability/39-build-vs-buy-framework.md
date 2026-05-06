# 39 — Build vs Buy Framework

> Should we use Datadog or run Mimir + Loki + Tempo ourselves? Honeycomb or self-host? The answer is rarely religious; it's a TCO calculation that turns on org scale, team maturity, and strategic priorities. This chapter is the framework for making the decision honestly, then revisiting it as the org changes.

This chapter is about the meta-decision that drives everything else. Decisions that look "wrong" downstream (`doc 37` migrations) often trace to a build-vs-buy choice made years before, with a different team and different scale.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [The TCO model](#2-tco)
3. [The inflection point math](#3-inflection)
4. [The maturity dimension](#4-maturity)
5. [The strategic dimension](#5-strategic)
6. [The hybrid pattern](#6-hybrid)
7. [The 12-month forecast trigger](#7-12-month)
8. [The vendor-evaluation rubric](#8-rubric)
9. [The "we'll grow into it" trap](#9-grow-into-it)
10. [The "we'll save money" trap](#10-save-money)
11. [Anti-patterns](#11-anti-patterns)
12. [Worked example: a 200-engineer org's decision](#12-worked-example)
13. [Pitfalls](#13-pitfalls)
14. [Mental models](#14-mental-models)

---

## 1. Thesis

Three claims:

1. **The decision is a TCO calculation, not a religious one.** Vendor cost, engineer cost, opportunity cost. Add it up; pick the lower number.
2. **The right answer changes over time.** A 50-engineer org buys; a 5,000-engineer org self-hosts. Most orgs cross the inflection point and don't notice; the migration that follows is `doc 37`.
3. **Hybrid is not a compromise; it's the default.** Few mature orgs run pure-buy or pure-build. Vendor for some signals; self-hosted for others. Match per signal.

If your team's answer to "build or buy?" is reflexive (always buy / always build), you're not making the choice — the choice is making you. This chapter is the framework.

---

## 2. The TCO Model

The total-cost-of-ownership math.

### 2.1 The components

**Buy:**
- Vendor license fees.
- Per-host / per-GB / per-series costs.
- Premium-support fees.
- Vendor lock-in cost (the migration cost when you eventually leave).

**Build:**
- Infrastructure (compute, storage, network).
- Engineer time (initial build, ongoing operations).
- Opportunity cost (engineers not working on something else).
- Tooling and licensing for self-hosted (some are commercial OSS like Grafana Enterprise).

### 2.2 The example calculation

For a 100-service org, 10M active series, 1 TB logs/day, 5% trace sampling:

**Buy (Datadog):**
- Hosts: 1000 × $20/host/month = $20K/mo.
- Custom metrics: 10M × $0.05/100/month = $50K/mo.
- Logs: 1 TB × $1/GB = $30K/mo.
- APM: $10K/mo.
- Other: $10K/mo.
- Total: $120K/month = $1.44M/year.

**Build (Mimir + Loki + Tempo):**
- Compute: $30K/mo.
- Storage: $5K/mo.
- Network: $3K/mo.
- 1.5 platform engineers @ ~$300K loaded: $375K/year = $31K/month.
- Total: $69K/month = $828K/year.

Build wins by ~$612K/year, but with substantial engineering investment.

### 2.3 The "less obvious" costs

- **Buy:** vendor migration cost when you eventually leave (`doc 37`); usually $500K-$2M one-time.
- **Build:** initial-build cost (~6 months of 2 engineers = $300K).
- **Buy:** training cost on vendor's UX (lower).
- **Build:** training cost on the homegrown stack (higher).
- **Buy:** features-as-they-ship (faster).
- **Build:** features as platform team builds (slower).

### 2.4 The 3-year view

A 1-year TCO often looks neutral. A 3-year TCO often diverges:

- Buy: cost grows ~30% YoY (vendor pricing increases + traffic growth).
- Build: cost grows ~15% YoY (mostly infrastructure with growth; engineer cost flat).

A buy-decision today may be a build-decision in 3 years. Plan accordingly.

---

## 3. The Inflection Point Math

The "when does build become cheaper" calculation.

### 3.1 The variables

- Vendor unit price (per host, per series, per GB).
- Self-hosted unit price (mostly compute + storage).
- Engineer cost (loaded).
- Org engineer count (proxy for scale).
- Org service count.

### 3.2 The rough rule

Build becomes cheaper when:

```
vendor_cost > self_hosted_compute_cost + (1.5 × loaded_engineer_cost)
```

The 1.5 covers initial build + ongoing ops + opportunity cost. Adjust per org.

### 3.3 The typical inflection

For most orgs, the inflection is around **$1M/year of vendor spend** or **~100 services**.

Below that: vendor is cheaper (engineer time too valuable to spend on platform).
Above that: build starts to win.

This is rough; the actual inflection depends on engineer salaries, vendor discounts, signal volume.

### 3.4 The "we just crossed it" trap

Org grows from 80 to 200 services in two years. Vendor bill triples ($800K → $2.4M). Nobody notices the inflection point passed at $1.2M.

Defense: revisit the build-vs-buy decision annually as part of FinOps governance (`doc 31 §12.3`).

---

## 4. The Maturity Dimension

The non-cost factor.

### 4.1 The maturity question

Does the team have the skills to build and operate?

| Capability | Build feasible? |
|---|---|
| K8s expertise | Required |
| Time-series-DB ops | Required |
| Distributed-systems debugging | Required |
| 24/7 platform on-call | Required |
| FinOps / capacity planning | Required |
| Schema governance | Required |

Without these, build will fail or be painful. Buy is the right call until the team is ready.

### 4.2 The "build-then-fail" anti-pattern

Common: small team builds Mimir + Loki + Tempo; it works for a quarter; then crashes during traffic spike; team panics; rolls back to vendor; lost six months of work.

The lesson: maturity matters. Buy in the meantime; build when you have the team to operate it.

### 4.3 The progression

- 0-50 services: buy (probably).
- 50-200 services: hybrid (some signals self-hosted, some vendor).
- 200-1000 services: mostly self-hosted; vendor for specialty.
- 1000+ services: deeply custom; possibly multi-vendor for redundancy.

### 4.4 The hiring-vs-vendor trade

Self-hosting requires engineers. If hiring is fast, build. If hiring is slow, buy.

Some orgs find it easier to hire engineers; others find it easier to negotiate vendor contracts. Match the business reality.

---

## 5. The Strategic Dimension

Beyond cost.

### 5.1 The data-gravity argument

If observability data is one of your strategic assets (e.g., for ML, security analysis, customer insights), keeping it in your warehouse is valuable. Self-hosted lakehouse architectures keep data in your platform.

Vendor data is harder to integrate. Some vendors offer APIs and exports; latency and completeness vary.

### 5.2 The vendor-lock-in argument

Long-term vendor commitment is risk. Vendor pricing changes, vendor gets acquired, vendor changes strategy. Self-hosted is more controllable.

Reaction: many orgs adopt OTel + open-source-compatible storage to ensure portability, then choose vendor or self-host as a deployment detail.

### 5.3 The compliance argument

Some regulated workloads (FedRAMP-high, certain government, certain financial) require self-hosted. Vendor cannot handle the data. The decision is forced.

### 5.4 The capability argument

If the vendor has a feature you need (specific integration, specific UI, specific compliance certification), buy.

If the vendor lacks something critical, build (or look for another vendor first).

### 5.5 The strategic-non-strategic trade

Observability is strategic if:
- Your product *is* observability (you're an observability company).
- Your scale is hyperscale; in-house tooling is competitive advantage.
- Compliance prohibits vendor.

Otherwise, observability is *operational support*. Buy when it's cheaper / faster.

---

## 6. The Hybrid Pattern

The most common 2026 architecture.

### 6.1 The mix

```
Metrics:  self-hosted Mimir (cost-driven)
Logs:     hybrid (Loki for app; Splunk for security)
Traces:   self-hosted Tempo (cost-driven)
Profiles: vendor (Pyroscope managed)
RUM:      vendor (Datadog RUM)
Errors:   vendor (Sentry)
APM:      OTel + self-hosted (no vendor APM)
LLM:      vendor (Helicone) + self-hosted dashboards
```

Per signal, the right answer.

### 6.2 The advantages

- Cost-optimized per signal.
- Vendor leverage for specialty (RUM, errors).
- Self-hosted for high-volume.
- Migration risk distributed.

### 6.3 The disadvantages

- More tools to operate.
- Cross-tool integration cost.
- Some users have to learn multiple UIs.

### 6.4 The unification layer

The hybrid is more livable with:
- Grafana as a unified dashboard (datasources for all).
- OTel as a unified instrumentation.
- Single SLO platform (Sloth/Pyrra) feeding both.

Without these, the hybrid is fragmented.

---

## 7. The 12-Month Forecast Trigger

When to revisit.

### 7.1 The forecast question

Cross-link to `doc 31 §6`. Annually:

- Project vendor cost 12 months out.
- Project self-hosted cost 12 months out.
- Compare.

### 7.2 The trigger

If forecast vendor cost > 2× current within 12 months, build-vs-buy is on the table. Revisit.

If forecast vendor cost slightly grows but is still under self-hosted: stay.

If self-hosted forecast is rising too (engineer count, infra cost): the trade-off shifted; reconsider.

### 7.3 The "renewal-aware" timing

Vendor contract renewals are 90+ days out. Decision must happen then to influence the contract.

Annual rhythm:
- Q1: forecast next 12 months.
- Q2: build-vs-buy review.
- Q3: vendor negotiation (if buy).
- Q4: migration kickoff (if build).

### 7.4 The "non-decision is a decision" reality

Not deciding = stay with vendor. Often the right call. But: be deliberate about it. Document the choice. Revisit next year.

---

## 8. The Vendor-Evaluation Rubric

When evaluating buy.

### 8.1 The dimensions

| Dimension | Weight | Notes |
|---|---|---|
| **Functional fit** | High | Does it do what we need? |
| **Cost (3-year TCO)** | High | Discount for committed contracts |
| **Lock-in / portability** | Medium | Can we leave? OTel-compatible? |
| **Reliability of vendor** | High | Outages, SLAs, history |
| **Roadmap alignment** | Medium | Are they investing in our needs? |
| **Support quality** | Medium | Response time, expertise |
| **Compliance posture** | High (if regulated) | SOC2, HIPAA, FedRAMP |
| **Operational complexity** | Medium | How much ops do we still own? |
| **UI / DX** | Medium | Engineer satisfaction |

### 8.2 The pilot

Don't decide on demos. Pilot for 30-90 days with real workloads. Measure.

### 8.3 The reference checks

Talk to other customers at similar scale. What's the lived experience?

### 8.4 The exit cost

Always ask: "if we leave, what's the cost?" Vendors don't volunteer this; ask explicitly. Plan for the exit before you sign.

---

## 9. The "We'll Grow Into It" Trap

The buy-side risk.

### 9.1 The trap

Org buys vendor at small scale. Cost is fine. Plan: "we'll worry about cost when we scale."

Org grows. Vendor cost compounds. Migration is now a $1M+ project with months of work.

### 9.2 The defense

- Pricing model awareness: how does the vendor's price scale with our growth?
- Forecast updates with each significant scale change.
- Architecture choices that preserve portability (OTel; not vendor-specific).

### 9.3 The "hidden contract"

Some vendors have extreme overage rates. A 2× volume spike costs 4×. Read the contract; understand non-linear pricing.

### 9.4 The "feature pricing"

Vendor adds new features; price comes with them. The bill grows even at constant scale. Track.

---

## 10. The "We'll Save Money" Trap

The build-side risk.

### 10.1 The trap

Org self-hosts to "save vendor cost." Underestimates engineer cost. Build takes 18 months. Net result: similar cost; less reliable; team distracted from real work.

### 10.2 The defense

- Honest engineer-cost estimation (loaded, multiplied for opportunity cost).
- Realistic timeline (typically 1.5-2× initial estimate).
- Pilot first; build slowly.
- Compare 3-year TCOs, not 1-year.

### 10.3 The "features lag" reality

Self-hosted lacks vendor's polish. Engineers want vendor's UI. Slowly demoralized. Loss in productivity.

Defense: use Grafana / open-source UIs that are competitive; don't build your own UI.

### 10.4 The "ops cost surprise"

Self-hosting at scale is non-trivial. Mimir cluster needs FT design, capacity planning, backup, DR, alerting. The platform team grows.

Plan for 1.5 engineers at minimum for a 100-service self-hosted observability stack. Scale up.

---

## 11. Anti-Patterns

1. **Religious decision.** Always buy or always build, regardless.
2. **No TCO model.** Decision is gut feel.
3. **Single-year analysis.** 3-year picture different.
4. **No exit-cost calculation.** Vendor lock-in surprise.
5. **No team-maturity assessment.** Build with insufficient team.
6. **No annual revisit.** Old decision becomes increasingly wrong.
7. **All-vendor when scaled.** Bill 5× higher than needed.
8. **All-build at small scale.** Distraction from product.
9. **No pilot.** Decision based on demos.
10. **No reference checks.** Surprises after signing.
11. **No exit plan.** Stuck when need to leave.
12. **No data-gravity consideration.** Strategic data trapped in vendor.
13. **Hybrid by accident, not design.** Fragmented stack.
14. **No leadership buy-in.** Decision reversed mid-project.
15. **No retrospective on past decision.** Learn nothing.

---

## 12. Worked Example: A 200-Engineer Org's Decision

Concrete and complete.

### 12.1 The setup

- 200 engineers, 100 services.
- Currently on Datadog at $1.2M/year.
- 30% YoY growth in services.
- Mature platform team (5 engineers).
- No specific compliance requirement.

### 12.2 The forecast

12-month projection:
- Vendor cost: $1.2M × 1.30 = $1.56M.
- Plus 20% pricing increase at renewal: $1.87M.
- Self-hosted (Mimir + Loki + Tempo): $200K compute + $300K (1 engineer): $500K.

Difference: $1.87M − $500K = $1.37M/year saved.

### 12.3 The 3-year view

| Year | Vendor | Self-hosted | Savings |
|---|---|---|---|
| 1 | $1.87M | $500K + $300K migration = $800K | $1.07M |
| 2 | $2.4M | $650K | $1.75M |
| 3 | $3.1M | $850K | $2.25M |

3-year savings: $5M. Strong build case.

### 12.4 The decision

Build. Migration plan from `doc 37` adopted.

### 12.5 The rollout

- Q1: foundation; OTel rollout to all services.
- Q2-Q3: dual-write phase.
- Q4: cutover; Datadog kept for read-only retention.

### 12.6 The actual outcome

- Migration cost: $400K (above estimate; engineering surprise).
- Year-1 savings: $900K (below forecast due to migration cost).
- Year-2 savings: $1.6M (close to forecast).
- 3-year cumulative: ~$4M (slightly below forecast).

Still strongly positive. Decision validated.

### 12.7 The annual revisit

After year 3, the org is at 400 services. Self-hosted comfortably operating. No reconsideration.

---

## 13. Pitfalls

1. **No TCO.** Gut decision.
2. **No annual revisit.** Drift.
3. **Single-year analysis.** Misleading.
4. **No exit cost.** Lock-in surprise.
5. **No maturity check.** Build fails.
6. **All-vendor at scale.** Cost explosion.
7. **All-build at small scale.** Distraction.
8. **No pilot.** Demo-driven.
9. **No references.** Surprises.
10. **No data-gravity.** Strategic data trapped.
11. **Hybrid unmanaged.** Fragmentation.
12. **No leadership alignment.** Reversal.
13. **No retrospective.** No learning.
14. **Vendor pricing model misunderstood.** Surprise.
15. **Engineer-cost underestimated.** Build over-budget.

---

## 14. Mental Models

> **It's a TCO calculation. Not religious.**

> **The right answer changes over time. Annual revisit.**

> **Hybrid is the default for mature orgs. Match per signal.**

> **The inflection is around $1M/year vendor spend or ~100 services.**

> **Maturity matters. Build needs the team to operate.**

> **3-year view shows divergence; 1-year often neutral.**

> **OTel + open-source storage preserves portability. Bake-in independence.**

> **Pilot before deciding. Demos lie.**

> **Always know the exit cost. Lock-in is real.**

> **The decision is reversible — but the migration costs $1M+. Don't make it twice.**

Now go to `doc 40` (IDP and golden paths).
