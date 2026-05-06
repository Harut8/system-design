# 41 — Brownfield Integration

> Greenfield observability is easy: pick the stack, set the standards, all teams adopt. Brownfield is the rest of reality: an acquisition brought their own Datadog account; one team uses Honeycomb; the legacy mainframe team has Splunk; the new ML platform uses Weights & Biases. Brownfield integration is the discipline of weaving these together — or deciding which to keep, sunset, or merge — without breaking the people who depend on each.

This chapter is about acquisitions, multi-vendor coexistence, deprecation, and the long, careful work of consolidating observability across an org that grew organically.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [The brownfield reality](#2-reality)
3. [The four scenarios](#3-scenarios)
4. [The acquisition integration playbook](#4-acquisition)
5. [Multi-vendor coexistence patterns](#5-coexistence)
6. [Federation: when the stacks must be queried as one](#6-federation)
7. [The unified-but-not-merged pattern](#7-unified-not-merged)
8. [The deprecation path](#8-deprecation)
9. [The "do not consolidate" cases](#9-dont-consolidate)
10. [Anti-patterns](#10-anti-patterns)
11. [Worked example: post-acquisition integration](#11-worked-example)
12. [Pitfalls](#12-pitfalls)
13. [Mental models](#13-mental-models)

---

## 1. Thesis

Three claims:

1. **Brownfield is the default; pure greenfield is the exception.** Most platform teams inherit observability mess; the discipline is consolidating it carefully without breaking ownership.
2. **Consolidation has costs.** Forcing every team onto the central stack costs months of engineering and team disruption. Sometimes the cost exceeds the value of consolidation.
3. **Federation is sometimes the right end-state.** Not every observability stack must merge. Sometimes "two vendors with a unified Grafana on top" is the right architecture for the next 5 years.

If your team is fresh from an acquisition, brownfield is your reality. If your org is 15 years old, brownfield is your reality. This chapter is the playbook.

---

## 2. The Brownfield Reality

What you inherit.

### 2.1 The typical mess

A 5-year-old org's observability landscape:
- Central platform: Datadog (the org default).
- Acquired-team: their own Datadog account; different settings.
- Acquired-team-2: brought New Relic; cancelled but data still there.
- Legacy mainframe team: Splunk (compliance-required).
- Data team: Spark with Datadog, Snowflake metrics in their warehouse.
- ML team: Weights & Biases for experiments; Grafana for serving.
- Security team: own Splunk + Sentinel.
- 5 engineers maintain 5 different on-call rotations across these.

This is normal. Don't panic.

### 2.2 The "what to do?" question

Three options:
1. **Consolidate.** Migrate everyone to the central stack.
2. **Coexist.** Each stack continues; some integration.
3. **Federate.** Stacks remain; unified queries / dashboards on top.

Most orgs end up with a hybrid of (2) and (3); pure (1) is expensive and rare.

### 2.3 The political dimension

Each stack has team(s) using it; teams have preferences; consolidation feels like loss. Approach with empathy:
- Acknowledge the team's expertise.
- Show benefits of consolidation (cost, integration).
- Provide migration support.
- Allow exceptions for legitimate reasons.

Consolidation by mandate breeds resistance. Consolidation by collaboration succeeds.

---

## 3. The Four Scenarios

The brownfield situations.

### 3.1 Acquisition

A company is acquired; brings its own observability stack.

**Decision factors:**
- Acquisition size: small (10 services) → consolidate; large (1000 services) → federate.
- Strategic plan: full integration into parent → consolidate; standalone subsidiary → coexist.
- Vendor overlap: same vendor → easy consolidation; different vendors → harder.

### 3.2 Organic growth

The org grew over years; teams chose tools; nobody enforced.

**Decision factors:**
- Cost of fragmentation (multi-vendor bills, training, migration risk).
- Team buy-in for consolidation.
- Strategic value of unification.

### 3.3 Compliance forks

A regulated workload requires a different stack (e.g., HIPAA-specific Splunk while rest is Datadog).

**Decision:** maintain the fork; legitimate compliance reason.

### 3.4 Specialty workloads

ML / data / security teams use specialty tools (W&B for ML, Snowflake for data, Splunk for security).

**Decision:** specialty tools serve their purpose; integrate at the federation layer.

---

## 4. The Acquisition Integration Playbook

The most common scenario.

### 4.1 Day-1 inventory

Within the first 30 days post-acquisition:
- What stack does the acquired company use?
- What signals do they collect?
- Who has access?
- What contracts are in place?
- What's the technical debt?
- What's their team's expertise?

Document; share with leadership.

### 4.2 The 90-day plan

Three options based on inventory:

**Option A: Quick consolidation (small acquisition).**
- Migrate to central stack within 90 days.
- Acquired team becomes part of the platform org.
- Cancel their vendor contracts.

**Option B: Gradual integration (medium acquisition).**
- Coexist for 6-12 months.
- Federate at top (unified Grafana).
- Consolidate gradually.
- Synthesize signals via OTel.

**Option C: Subsidiary / coexistence (large or strategic).**
- Maintain separate stacks long-term.
- Federate selectively.
- Org-wide reports aggregate.

### 4.3 The first conversation

Meet with the acquired team's platform / SRE leads. Listen first:
- What do they love about their current stack?
- What pain points do they have?
- What's their roadmap?

Don't lead with "you're moving to Datadog." Lead with curiosity.

### 4.4 The contract review

Vendor contracts:
- When does it expire? Cancellable?
- Auto-renew?
- Termination penalties?
- Data export rights?
- Existing data retention obligations?

Often determines the timeline. A 3-year contract with no early termination forces coexistence.

### 4.5 The data migration choice

(Cross-link to `doc 37 §8`.) For acquired-org's data:
- Don't migrate (most common): keep their stack read-only for retention; cancel after.
- Selective migration: critical metrics / compliance data only.
- Full migration: rare; expensive.

### 4.6 The team integration

Often the harder part. Acquired-team engineers:
- Embedded into central platform team.
- Maintained as a regional team with autonomy.
- Re-org per business unit.

Each has trade-offs. Match the org's culture.

---

## 5. Multi-Vendor Coexistence Patterns

When stacks must coexist.

### 5.1 The "common substrate" pattern

OTel as the common instrumentation; vendors as deployable backends. Apps emit OTel; collectors fan out:

```
service → OTel collector ──┬──→ Datadog (org default)
                           └──→ Honeycomb (acquired team's preference)
```

Each backend stays; instrumentation is unified. Future migrations are vendor-only.

### 5.2 The "primary + specialty" pattern

One stack for general observability (Datadog); specialty stacks for specialty workloads (Splunk for security, W&B for ML, Snowflake for analytics).

Each plays its strongest role. Cross-cutting integration via federation.

### 5.3 The "regional split" pattern

Different regions / business units use different vendors. Common in multinational orgs.

Federation via Grafana / cross-cluster queries.

### 5.4 The cost of coexistence

- Multiple vendor contracts.
- Multiple sets of expertise.
- Cross-tool integration overhead.
- Engineer training cost.

Worth it if the consolidation cost exceeds. Not worth it if consolidation is cheap.

### 5.5 The unification at the top

Even with coexisting stacks, *one place* engineers go:
- Grafana with multi-datasource support.
- Backstage catalog with all services.
- Slack with unified alert routing.

The unification layer reduces the user-experience cost of multi-vendor.

---

## 6. Federation: When the Stacks Must Be Queried as One

Cross-stack queries.

### 6.1 The problem

Engineers debugging a cross-team issue need data from both stacks. Without federation: tab juggling, manual correlation, mistakes.

### 6.2 The mechanisms

- **Grafana mixed datasources:** one dashboard, queries to multiple backends.
- **Trino / Presto:** SQL across multiple stores (if data is in compatible formats).
- **OTel Collector tee:** signals to multiple backends with shared trace_id, enabling cross-backend trace lookup.
- **Custom federation layer:** query proxy that routes to the right backend.

### 6.3 The trace federation

The hardest. A trace that crosses both stacks must be query-able from one place.

Solutions:
- Both stacks store the trace (dual-write).
- Stack A stores, Stack B has a reference.
- Bridge service that unifies trace search across both.

Usually: dual-write traces from cross-team services; native search in either stack.

### 6.4 The performance cost

Federated queries are slow. Cache aggressively.

### 6.5 The "single SLO source"

For cross-team SLOs: one stack must be the source of truth. Otherwise SLO calculations diverge.

Pick a primary; secondary feeds into it for unified SLO computation.

---

## 7. The Unified-But-Not-Merged Pattern

The realistic end-state.

### 7.1 The shape

```
Org-wide observability:
  - Apps emit OTel.
  - Collectors fan out:
    - Datadog (general; 70% of services).
    - Splunk (security + 1 compliance team).
    - Honeycomb (1 trace-heavy team).
  - Grafana as unified dashboards (datasources for each).
  - Backstage as unified catalog.
  - Single SLO platform (Sloth or vendor-neutral).
  - Unified alert routing (PagerDuty for all).
```

Tools coexist; experience unified. Engineers see "their" stack but with a common UX.

### 7.2 The benefits

- Each tool serves its strength.
- Migration cost amortized over time.
- Specialty teams keep their preferred tools.
- Engineers don't suffer fragmentation.

### 7.3 The ongoing cost

- Vendor contracts.
- Tool-specific operational expertise.
- Federation maintenance.

Manageable for mid-size orgs. Larger orgs sometimes consolidate further.

### 7.4 The "drift" risk

Without active management:
- Vendor-specific instrumentation creeps back.
- Federation breaks; nobody notices.
- New services pick whichever tool the team likes.

Defense: governance. Documented standards (`doc 34`); IDP-enforced templates (`doc 40`); annual audit.

---

## 8. The Deprecation Path

When to retire a stack.

### 8.1 The triggers

- Cost: this stack costs $X; we save $Y by consolidating.
- Strategic: leadership wants vendor consolidation.
- Risk: vendor lock-in or vendor instability.
- Skills: hard to maintain expertise.

### 8.2 The procedure

(Cross-link to `doc 37`.) Same as a migration:
1. Inventory.
2. Plan.
3. Dual-write phase.
4. Read parity.
5. Cutover.
6. Decommission.

### 8.3 The team conversation

Often the hardest part. The team using the deprecated stack feels:
- Loss of preferred tools.
- Loss of expertise (they were experts).
- Disrupted workflows.

Approach: framing matters. "We're consolidating to reduce engineer toil and cost; we want your input on what features must be preserved."

### 8.4 The exception

Sometimes deprecation can't happen:
- Compliance requirement.
- Vendor contract; locked in.
- Specialty function not available elsewhere.

Document the exception; revisit annually.

---

## 9. The "Do Not Consolidate" Cases

When coexistence is the right answer.

### 9.1 Specialty tools

ML experiment tracking (W&B, Comet) — purpose-built; general observability isn't a substitute.

Keep specialty tools; integrate at the federation layer.

### 9.2 Compliance-required tools

Splunk for security in regulated industries. The compliance regime *requires* the specific tool.

Keep; don't consolidate.

### 9.3 Massive cost-of-migration

Migrating 1000 services off Datadog when the org has 1500 services and a tight roadmap: not worth it for marginal cost savings.

Defer; revisit annually.

### 9.4 Team-skills concentration

A team's expertise is in their tool. Forcing migration loses their expertise; they're slower with the new tool for months.

Sometimes: keep their tool. Pick battles.

### 9.5 The "let it be" wisdom

Not every brownfield needs to become greenfield. Sometimes the optimal end-state is messy. The platform team's job: maintain the *unified experience* on top, not enforce uniform infrastructure.

---

## 10. Anti-Patterns

1. **Force consolidation by mandate.** Resistance; broken integration.
2. **Consolidate without listening.** Miss legitimate concerns.
3. **No inventory.** Action without understanding.
4. **No timeline plan.** Drifts indefinitely.
5. **No federation layer.** Fragmented UX.
6. **Forcing OTel without buy-in.** Resistance from teams.
7. **Migrating data without need.** Expensive; risky.
8. **Skipping the contract review.** Surprises.
9. **No team-integration plan.** People disrupted.
10. **No exception process.** Legitimate cases blocked.
11. **No annual revisit.** Drift.
12. **No documentation of multi-vendor.** Tribal knowledge.
13. **Ignoring acquired team's expertise.** Loss.
14. **Skipping the political work.** Failure.
15. **Vendor lock-in by accident.** Future migration harder.

---

## 11. Worked Example: Post-Acquisition Integration

Concrete and complete.

### 11.1 The scenario

Acme (1000 engineers, Datadog-based) acquires Globex (200 engineers, Honeycomb + Sentry-based). Acme's strategic plan: full integration over 18 months.

### 11.2 Day 1-30: inventory

- Globex's stack: Honeycomb for traces; Datadog for metrics (interestingly); Sentry for errors.
- Globex pays $300K/year; Acme pays $1.5M/year.
- Globex contract expires in 14 months.
- Globex team has 4 platform engineers.

### 11.3 Day 31-90: plan

Strategic decision: consolidate to Acme's stack at Globex's contract expiry.

Phases:
- Months 1-3: Globex teams adopt OTel (replacing Honeycomb-specific instrumentation).
- Months 4-9: dual-write to both Honeycomb and Acme's Datadog.
- Months 10-12: dashboards / alerts mirrored.
- Month 13: cutover; Honeycomb read-only.
- Month 14: Honeycomb contract expires; cancellation.

### 11.4 The team integration

Globex's 4 platform engineers join Acme's central platform team. They become the experts on the trace use cases Honeycomb served well; advocate for those features in Datadog's trace capability.

### 11.5 The federation

During months 4-12: Grafana with both Datadog and Honeycomb datasources. Engineers see unified dashboards.

### 11.6 The cutover

Month 13: Globex services' alerts switch to Acme's Datadog. Honeycomb retained for read-only retention through month 14.

### 11.7 The outcome

- Cost: Globex's $300K/year saved.
- Acme's Datadog grew by ~10% to absorb Globex's traffic.
- Net savings: ~$200K/year.
- Globex engineers: integrated; respected; one became a Datadog SME.
- Trace UX: some loss vs Honeycomb; mitigated with Datadog feature requests + Tempo for some workloads.
- Customer impact: zero.

### 11.8 The lessons

- Listen first; mandate later.
- OTel as the substrate made migration tractable.
- Engineering integration is the people part; pay attention.
- Some loss of capability is acceptable for consolidation benefit.

---

## 12. Pitfalls

1. **No inventory.** Don't know what exists.
2. **Force consolidation.** Resistance.
3. **No team integration plan.** People disrupted.
4. **No federation during transition.** Fragmented UX.
5. **Vendor contracts undisclosed.** Surprises.
6. **No exception path.** Legitimate cases blocked.
7. **Ignoring acquired team expertise.** Loss.
8. **No timeline.** Drift.
9. **Vendor-specific lock-in stays.** Future migration harder.
10. **No annual revisit.** Drift.
11. **Cost ignored.** Multi-vendor bill explodes.
12. **No standardization.** Per-team divergence.
13. **Cutover without dual-write.** Risky.
14. **Migration data forced.** Expensive.
15. **No "let it be" option.** Forcing where coexistence is fine.

---

## 13. Mental Models

> **Brownfield is the default. Pure greenfield is rare.**

> **Listen first; mandate later. Consolidation by collaboration succeeds.**

> **OTel is the unifying substrate. Adopt early; vendor choice becomes deployment detail.**

> **Federation is sometimes the right end-state. Coexistence isn't failure.**

> **Specialty tools earn their place. Don't force ML/security/data teams off purpose-built tools.**

> **The unified experience matters more than uniform infrastructure.**

> **Acquisition integration is months-to-years, not weeks.**

> **Team integration is the people work. Pay attention.**

> **Contract reviews drive timing. Auto-renewals constrain.**

> **Annual revisit; the right answer drifts.**

This is the last new chapter. Next: appendices and the ROADMAP update.
