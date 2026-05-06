# 17 — Production Readiness Reviews (PRR)

> The cheapest reliability investment a platform team makes. A 4-hour review that prevents the 4-week postmortem cascade. PRR is the gate that ensures *every* new service crossing into production meets a baseline observability, reliability, and operability bar — set once, enforced everywhere.

This chapter assumes the practices from `doc 12`–`doc 16`. PRR is where they're audited and signed.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [What PRR is and isn't](#2-what-prr-is)
3. [The PRR lifecycle](#3-lifecycle)
4. [The canonical PRR checklist](#4-checklist)
5. [Scoring and remediation](#5-scoring)
6. [PRR for changes (mid-life), not just launches](#6-prr-for-changes)
7. [PRR for sunsetting / deprecation](#7-sunsetting)
8. [Service-maturity scorecards (ongoing PRR)](#8-scorecards)
9. [The PRR meeting](#9-meeting)
10. [Self-service PRR (the IDP angle)](#10-self-service)
11. [Anti-patterns](#11-anti-patterns)
12. [Worked example: PRR for a new pricing service](#12-worked-example)
13. [Pitfalls](#13-pitfalls)
14. [Mental models](#14-mental-models)

---

## 1. Thesis

Three claims:

1. **Production readiness is gated, not optional.** A service that doesn't meet the bar doesn't go live. The gate is the platform team's lever for raising the floor across the org.
2. **PRR is for new launches *and* for changes.** Major architectural changes, traffic-pattern shifts, dependency swaps, regulatory regime changes — all warrant re-PRR. Not just first-launch.
3. **The output is a scorecard, not a binary pass/fail.** Some items are blocking; many are "must address within 90 days." A scorecard makes the trade-offs explicit and trackable.

If your org launches services without a PRR — or runs PRR but doesn't enforce its conclusions — you have observability and reliability spread *unevenly*. The good services are great; the bad services cause your incidents. PRR is the leveling mechanism.

---

## 2. What PRR Is and Isn't

### 2.1 What it is

- A structured *review* of a service's production readiness against a checklist.
- A *gate* that the platform team enforces.
- A *scorecard* showing which items are met, which are blocked, which are pending.
- A *contract* with the service team about what they own going forward.

### 2.2 What it isn't

- A code review (separate process).
- A security review (related but separate; typically runs in parallel).
- A performance review of the engineers.
- A one-time event (the scorecard is ongoing; revisited at major changes).

### 2.3 Who runs it

The platform / SRE team. Specifically, a PRR reviewer — typically a senior SRE or Staff Engineer who knows the org's reliability bar. Not the service team itself; the conflict of interest is real.

### 2.4 Why it works

PRR is *upstream* of every reliability outcome. By the time an incident happens, the service is in production with whatever level of readiness it has. PRR shifts the work to *before* launch, when fixes are cheap. Empirically (from the Google SRE book and various org case studies), PRR-gated services have 30–50% lower SEV-1 rates in their first year.

---

## 3. The PRR Lifecycle

```
┌─────────────────────────┐
│ Service team initiates  │  ~6 weeks before planned launch
│ PRR request             │  fills out the self-assessment
└──────────┬──────────────┘
           ▼
┌─────────────────────────┐
│ Reviewer assigned       │
│ Initial doc review       │  ~1 week
└──────────┬──────────────┘
           ▼
┌─────────────────────────┐
│ Deep-dive meeting        │  2-4 hours
│ ("the PRR meeting")     │  team + reviewer + relevant stakeholders
└──────────┬──────────────┘
           ▼
┌─────────────────────────┐
│ Scorecard issued        │
│ (with action items)     │
└──────────┬──────────────┘
           ▼
┌─────────────────────────┐
│ Remediation              │  blocking items must close
│ (1-4 weeks typically)   │
└──────────┬──────────────┘
           ▼
┌─────────────────────────┐
│ Follow-up sign-off       │
│ Service goes live        │
└──────────┬──────────────┘
           ▼
┌─────────────────────────┐
│ 6-month follow-up         │  scorecard re-evaluated
│ + ongoing scorecard      │  ongoing in IDP
└─────────────────────────┘
```

### 3.1 The 6-week lead time

Six weeks before launch is the right window. Closer than 4 weeks and the team can't fix blocking items. Earlier than 8 weeks and the service isn't yet stable enough to review meaningfully.

### 3.2 Re-PRR cadence

After initial PRR:
- 6-month follow-up: did blocking items close? Did near-blocking items close? Any new gaps?
- Per-major-change: any change that materially affects readiness re-triggers PRR.

The scorecard is *living*, not a one-time report. Maintained in the org's IDP / service catalog.

---

## 4. The Canonical PRR Checklist

The single artifact that defines the bar. Versioned, in Git, in the platform team's repo.

### 4.1 Observability

| Check | Requirement |
|---|---|
| 4.1.1 | Every service exposes the four golden signals (`doc 00 §3`) |
| 4.1.2 | RED dashboard exists and is linked from the service catalog entry |
| 4.1.3 | USE dashboard for the underlying resources |
| 4.1.4 | Logs are structured (JSON) and include `trace_id`, `span_id`, `service.name`, `tenant_id` if multi-tenant |
| 4.1.5 | Trace spans propagate W3C `traceparent` across all process boundaries |
| 4.1.6 | Histogram buckets are tuned to bracket the latency SLO |
| 4.1.7 | Exemplars enabled for histogram metrics |
| 4.1.8 | Continuous profiling enabled (or has a documented opt-out reason) |
| 4.1.9 | Metric cardinality budget agreed; high-cardinality dimensions excluded |
| 4.1.10 | Logs PII-redacted at the source (or has a documented schema for retained PII) |

### 4.2 SLOs and alerting

| Check | Requirement |
|---|---|
| 4.2.1 | At least one journey-level SLO defined for the service's contribution |
| 4.2.2 | Service-level SLO defined for the service itself |
| 4.2.3 | Multi-window multi-burn-rate alerts generated from the SLO |
| 4.2.4 | All paging alerts have `team`, `severity`, `runbook`, `dashboard` annotations |
| 4.2.5 | Quarterly SLO review scheduled |
| 4.2.6 | Error-budget policy signed |

### 4.3 On-call

| Check | Requirement |
|---|---|
| 4.3.1 | On-call rotation set up in PagerDuty / Opsgenie / etc. |
| 4.3.2 | Primary + secondary tier |
| 4.3.3 | ≥ 6 people in the rotation (or documented exception) |
| 4.3.4 | Compensation policy documented |
| 4.3.5 | Onboarding plan for new on-call members |
| 4.3.6 | Synthetic page test scheduled weekly |

### 4.4 Runbooks

| Check | Requirement |
|---|---|
| 4.4.1 | Runbook exists for every paging alert |
| 4.4.2 | Runbooks include immediate actions, branching by symptom, mitigation criteria |
| 4.4.3 | Runbooks linked from the alerts and the service catalog |
| 4.4.4 | Quarterly runbook review scheduled |

### 4.5 Capacity and performance

| Check | Requirement |
|---|---|
| 4.5.1 | Load test completed at projected peak |
| 4.5.2 | Capacity plan exists for the next 6 months |
| 4.5.3 | Headroom ≥ 30% on all critical resources at projected peak |
| 4.5.4 | Autoscaling configured (or documented why not) |
| 4.5.5 | Bottleneck resources identified and monitored |
| 4.5.6 | Provisioning lead time documented |

### 4.6 Reliability and rollback

| Check | Requirement |
|---|---|
| 4.6.1 | Canary deploy strategy in place (or documented exception) |
| 4.6.2 | Rollback procedure documented and tested |
| 4.6.3 | Feature flags / kill switches for risky features |
| 4.6.4 | Graceful shutdown handling for SIGTERM |
| 4.6.5 | Health and readiness probes configured |
| 4.6.6 | Retry / timeout configured for all dependencies |
| 4.6.7 | Circuit breakers around critical dependencies |

### 4.7 Multi-region / DR

| Check | Requirement |
|---|---|
| 4.7.1 | If service is tier-1: multi-region active/active or active/passive plan |
| 4.7.2 | Failover procedure documented |
| 4.7.3 | RTO / RPO targets documented |
| 4.7.4 | Failover tested in last 6 months |
| 4.7.5 | Backups verified by test restore |

### 4.8 Security

| Check | Requirement |
|---|---|
| 4.8.1 | Authentication enforced; no anonymous endpoints (or documented public surface) |
| 4.8.2 | TLS in flight; encryption at rest |
| 4.8.3 | Secrets in a secrets manager, not in env or config files |
| 4.8.4 | Audit logging for sensitive operations |
| 4.8.5 | Dependencies scanned for known vulnerabilities |
| 4.8.6 | Data classification and retention documented |

### 4.9 Multi-tenancy

| Check | Requirement |
|---|---|
| 4.9.1 | Tenant isolation enforced in code (logical boundary explicit) |
| 4.9.2 | Per-tenant quotas / rate limits configurable |
| 4.9.3 | Tenant-id propagated through telemetry |
| 4.9.4 | Cross-tenant data access prohibited and tested |

### 4.10 Operational

| Check | Requirement |
|---|---|
| 4.10.1 | Service entry in catalog (Backstage / equivalent) |
| 4.10.2 | Owner / team / contact info up to date |
| 4.10.3 | Documentation site / wiki linked |
| 4.10.4 | Configuration declarative (GitOps / IaC) |
| 4.10.5 | Deploy pipeline auditable |
| 4.10.6 | First-incident playbook reviewed by on-call |

The list is intentionally long. Every item earns its place by being a *reproducible failure mode* — i.e., something the platform team has seen go wrong before. Extend the list with each new lesson.

---

## 5. Scoring and Remediation

Not all items are equal. The scorecard captures severity.

### 5.1 The scoring scheme

| Score | Meaning | Action |
|---|---|---|
| **Met** | Item passes | None |
| **Pending** | Will be met before launch | Track to launch |
| **Blocked** | Cannot meet; needs alternative | Discuss exception |
| **Exception** | Item waived with documented reason | Reviewer + senior leader sign |
| **Not applicable** | Item doesn't apply to this service | Document why |

### 5.2 Blocking vs non-blocking

The reviewer marks each item as **blocking for launch** or **non-blocking** (must address within N days).

Typical blocking items:
- Any observability gap (SLOs, dashboards, runbooks)
- Any rollback gap (can't roll back = can't launch)
- Any security gap (unauthenticated endpoints)
- Capacity gap (no load test, or load test failed)

Typical non-blocking (90-day):
- Continuous profiling not yet enabled
- Some runbooks rough but exist
- Multi-region not yet (if tier 2 service)

### 5.3 Exceptions

For items that *cannot* be met, the team documents:
1. Why the item doesn't apply, or why it can't be met.
2. What mitigation is in place.
3. Who signs off (a senior leader, not just the team lead).

Exceptions are tracked in the service catalog. Anyone can read them. *Visibility is what makes exceptions safe* — it's not a way to skip the bar; it's a way to articulate the trade-off and make it reviewable.

### 5.4 The remediation timeline

After PRR, the team owns a list of action items with deadlines. The platform team tracks closure (like postmortem action items, `doc 15 §13`). Items not closed by deadline:
- Blocking (pre-launch): launch postponed.
- Non-blocking: escalate to leadership; service flagged as "below readiness bar" in the catalog.

---

## 6. PRR for Changes (Mid-Life), Not Just Launches

A service-changes PRR ("CR-PRR" or just "review") is triggered by:

- **Architecture changes:** new dependency, dependency removed, sharding scheme changed, replication topology changed.
- **Traffic shifts:** user base 5×; new geography; new tier of customer.
- **Regulatory changes:** entering a new jurisdiction; data residency requirement; SOC2 scope change.
- **Capacity changes:** scaling beyond original plan envelope.
- **Owner changes:** team handoff, acquisition.

The full PRR isn't always needed; often a *delta review* — what changed, what readiness items are affected — is enough.

### 6.1 The "trigger" list

The platform team defines what constitutes a re-PRR trigger. Codify it; otherwise teams will rationalize that "this isn't really a change worth re-reviewing."

### 6.2 Why this matters

Most services pass first-launch PRR and then *drift*. The architecture changes; the runbooks don't. The dependency list expands; the rollback plan doesn't. The traffic 10×s; the capacity plan was for last year. Re-PRR catches drift.

---

## 7. PRR for Sunsetting / Deprecation

The often-forgotten case: turning a service *off* is also a readiness event.

### 7.1 The deprecation checklist

| Check | Requirement |
|---|---|
| 7.1.1 | All callers identified (via traces, logs, audit) |
| 7.1.2 | Callers notified with sunset date |
| 7.1.3 | Migration path documented (alternative service or feature) |
| 7.1.4 | Data retention plan (move, archive, delete) |
| 7.1.5 | Compliance review (right-to-erasure, audit log retention) |
| 7.1.6 | Decommission steps documented |
| 7.1.7 | Final shutdown date + verification plan |
| 7.1.8 | Telemetry / dashboard cleanup plan |
| 7.1.9 | DNS / endpoint redirects for graceful traffic loss |

### 7.2 Why deprecation needs a review

The single most common cause of "we accidentally took something down" is incomplete deprecation. A caller you didn't know about pages when the service goes away. A data set that should have been migrated wasn't.

### 7.3 The "freeze first" pattern

Before turning a service off, *freeze* it:
- No new feature work.
- No new callers (block at the API gateway).
- Existing callers given a deadline to migrate.
- Telemetry watch for anyone still calling.

Then turn off. Often the freeze period reveals callers nobody knew existed.

---

## 8. Service-Maturity Scorecards (Ongoing PRR)

The PRR scorecard, alive over time, in the IDP.

### 8.1 What the scorecard looks like

```
checkout-svc — Production Readiness Score: 87/100

Observability:        92/100 (1 item: continuous profiling not enabled)
SLOs and alerting:    100/100
On-call:              100/100
Runbooks:             80/100 (1 stale runbook: "vendor-degradation"; review overdue)
Capacity:             100/100
Reliability:          85/100 (canary not yet enforced)
Multi-region:         70/100 (passive only; active planned Q3)
Security:             100/100
Multi-tenancy:        N/A
Operational:          100/100

Last PRR: 2026-01-15
Next review: 2026-07-15
Open action items: 2 (PROFILE-12, RUNBOOK-CHECKOUT-VENDOR)
```

### 8.2 Why ongoing matters

A point-in-time PRR atrophies. Continuous scoring catches drift in real time. The scorecard:
- Pulls live data from the observability stack (does the SLO exist? are alerts current? are runbooks recent?)
- Surfaces missing items in the IDP UI.
- Sends nudges to the team when items go stale.

### 8.3 The scoreboard

Cross-team scoreboard:

```
Service               PRR score    Tier   Open actions
checkout-svc          87           1      2
order-svc             92           1      1
inventory-svc         71           2      5      ← red, needs attention
auth-svc              95           1      0
search-svc            88           2      1
```

Visibility creates pressure. Teams compare scores; the platform team uses scoreboard data to decide where to invest support.

### 8.4 Tiering

Services are tiered (1 / 2 / 3) based on customer impact. Tier-1 services have higher PRR bars (e.g., multi-region required); tier-3 services have lower (e.g., async batch jobs may not need synthetic monitoring). The PRR checklist has *tier-conditional* items.

---

## 9. The PRR Meeting

The 2-4 hour deep-dive.

### 9.1 Attendance

- Service team lead, primary engineer(s).
- PRR reviewer (platform / SRE).
- Optional: security reviewer, capacity-team rep, runbook expert.

### 9.2 Agenda

1. **Service overview** (15 min). Team walks through what the service does, who depends on it, traffic profile.
2. **Architecture** (30 min). Dependencies, data flow, failure modes.
3. **Observability walk-through** (30 min). Open the dashboards; live demo. Validate SLOs, alerts, runbooks.
4. **Capacity discussion** (30 min). Load test results, plan, headroom.
5. **Reliability / failure modes** (45 min). What can go wrong? Walk through deployment, rollback, partial failures.
6. **Security review** (30 min). Authentication, authorization, secrets, audit.
7. **Q&A and scoring** (30 min). Reviewer scores live; team has chance to clarify.

### 9.3 The reviewer's role

The reviewer is *educational, not adversarial*. The goal is the service launches well, not "I caught the team out." A good reviewer:

- Asks "what happens when X fails?" rather than asserting "X will fail."
- Connects gaps to known incident patterns ("in 2024, service Y had a similar setup and outage Z").
- Suggests minimum viable fixes for blocking items.
- Documents what's good as well as what's missing.

### 9.4 The output

A scorecard document, posted to the team and the service catalog. Action items filed. Follow-up scheduled.

---

## 10. Self-Service PRR (the IDP angle)

Mature platforms automate as much of PRR as possible.

### 10.1 What can be automated

- **SLO defined?** Check the SLO repo for an entry matching the service.
- **Alerts defined?** Check the rules repo.
- **Runbooks?** Check the runbook directory; verify links resolve.
- **Dashboard?** Check Grafana for the standard panels.
- **Catalog entry?** Check Backstage.
- **Capacity plan?** Check the planning repo.
- **Tier and ownership?** Check the catalog.

A bot or scorecard service queries these sources and computes a score. Teams see the score in the IDP UI; the platform team sees the scoreboard.

### 10.2 What still needs a human

- Architecture review.
- Failure-mode discussion.
- Security review (mostly).
- Judgment calls on exceptions.

The split: automation surfaces the *factual* gaps; humans decide the *qualitative* questions.

### 10.3 PRR-as-code

The checklist itself lives in YAML. Each check has:
- A reference to the requirement.
- A query / probe to verify it.
- A severity (blocking / 90-day / 30-day).
- A tier filter (some checks only apply to tier-1).

```yaml
checks:
  - id: obs.slo.defined
    description: "At least one SLO defined for the service"
    probe: |
      slo-repo: journeys/${service}/slo.yaml exists
    severity: blocking
    tiers: [1, 2]
  - id: obs.runbook.linked
    description: "Every paging alert has a runbook annotation"
    probe: |
      alerts-repo: rules with severity=page and missing runbook annotation == 0
    severity: blocking
  ...
```

The PRR YAML is the source of truth. It versions; it's reviewed; checks are added as new failure modes are learned.

---

## 11. Anti-Patterns

### 11.1 "PRR on launch only"

Symptom: services pass PRR and never re-reviewed.
Fix: 6-month follow-up; scorecard maintained ongoing.

### 11.2 "PRR is a rubber stamp"

Symptom: every service passes; no item is blocking.
Fix: blocking items defined explicitly; reviewer training.

### 11.3 "PRR is adversarial"

Symptom: teams hide gaps; review is feared.
Fix: cultural reset. Reviewer is educational; team and reviewer are on the same side.

### 11.4 "No exceptions process"

Symptom: items that can't be met cause launch delays; team rationalizes around them.
Fix: documented exception process with senior signer.

### 11.5 "PRR happens once, never tracked"

Symptom: scorecard not in the IDP; nobody knows current state.
Fix: scorecard service. Continuous, visible.

### 11.6 "All services held to the same bar"

Symptom: tiny internal tool gets full tier-1 treatment; team frustrated.
Fix: tiering. Bar scales with customer impact.

### 11.7 "PRR doesn't include capacity"

Symptom: services launch without load tests; capacity-related outages.
Fix: load test is blocking item.

### 11.8 "PRR doesn't include rollback"

Symptom: services that can't roll back; bad deploys cause long outages.
Fix: rollback is blocking item, with verification.

### 11.9 "No follow-up"

Symptom: 90-day items aren't tracked; never close.
Fix: tracking system; visible in scorecard.

### 11.10 "PRR is the platform team's problem, not the service team's"

Symptom: service team treats PRR as someone else's checklist.
Fix: service team self-assesses first; the review is collaborative.

---

## 12. Worked Example: PRR for a New Pricing Service

A real-shaped example.

### 12.1 The service

`pricing-svc` — new internal microservice, computes per-customer prices for the checkout flow. Tier 1 (in the checkout journey's critical path).

### 12.2 First PRR (T-6 weeks)

Self-assessment shows:
- SLO not yet defined (blocking).
- Runbook missing (blocking).
- Load test not run (blocking).
- Continuous profiling not enabled (90-day).
- Multi-region not yet (90-day; planned for Q3).
- Canary deploy enabled.
- Auth/TLS/secrets all met.
- Catalog entry partial.

PRR meeting outcome:
- 4 blocking items, 2 non-blocking.
- Action plan: SLO drafted by next week; runbook by week 3; load test in week 4.
- Re-review in 4 weeks.

### 12.3 Second PRR (T-2 weeks)

All blocking items closed:
- SLO: 99.9% availability, p99 < 200ms over 28d.
- Runbook: covers vendor outage, DB lag, deploy regression.
- Load test: passed at 1500 RPS (target 800 mean, 2400 peak).

Reviewer signs off. Service goes live on schedule.

### 12.4 6-month follow-up

- Continuous profiling enabled (caught a 30% CPU regression in week 3).
- Multi-region active/passive in place.
- 1 SEV-3 incident in 6 months (vendor outage; runbook worked perfectly).
- SLO compliance: 99.94% (above target).

### 12.5 The lesson

The pre-launch PRR cost ~20 engineer-hours total (team + reviewer). The 1 SEV-3 in 6 months cost ~4 hours of incident-response. *Without* PRR, this service would likely have launched without an SLO, without a runbook, and probably without a tested rollback. The first incident would have been a SEV-1 of unknown duration.

The ROI on PRR is overwhelmingly positive. Quantify it for your org once and the funding conversation is over.

---

## 13. Pitfalls

1. **Optional PRR.** Some services pass; others skip; floor stays uneven.
2. **No tiering.** Tier-3 services held to tier-1 bar; team friction.
3. **No exceptions.** Items that can't be met cause launch delay; team works around.
4. **No follow-up tracking.** 90-day items expire and nobody notices.
5. **Static checklist.** Doesn't evolve with learned failure modes.
6. **No automation.** Human-only verification; doesn't scale past 50 services.
7. **No re-PRR for changes.** Drift accumulates; old PRR becomes stale.
8. **No deprecation PRR.** Sunset failures.
9. **Single reviewer.** Bus factor; single point of failure.
10. **Reviewer untrained.** Inconsistent reviews.
11. **No scoreboard.** Org-wide visibility absent; pressure for improvement weak.
12. **Blocking items ambiguous.** Teams negotiate around requirements.
13. **No record of past exceptions.** Same exceptions granted twice independently.
14. **PRR seen as adversarial.** Teams hide gaps; review is theater.
15. **PRR doesn't tie to incidents.** Failure modes from incidents don't update the checklist.

---

## 14. Mental Models

> **PRR is the cheapest reliability investment a platform team makes.** ~20 hours pre-launch saves dozens of hours of incident response.

> **Gates, not theater.** Items are blocking *or* non-blocking. The bar is real.

> **Living scorecard, not point-in-time review.** Drift is the long-term enemy.

> **Tiering scales the bar to impact.** Tier-1 stricter than tier-3.

> **Exceptions are visible and signed.** Trade-offs are explicit, not hidden.

> **Re-PRR for changes.** Drift catches what first-PRR couldn't predict.

> **Deprecation is also a readiness event.** Don't sunset blind.

> **Automate the factual; keep humans for judgment.** The IDP scoreboard is the platform team's force multiplier.

> **The reviewer is educational, not adversarial.** Good reviews build the team's skill, not just the score.

> **Postmortems update the checklist.** Each new failure mode learned becomes the next service's gate.

Now go to `doc 18` (cardinality and cost) — the single hardest cross-cutting problem the PRR's "cardinality budget" line item references.
