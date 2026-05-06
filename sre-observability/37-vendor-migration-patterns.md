# 37 — Vendor Migration Patterns

> Migrating between observability vendors is one of the most disruptive engineering projects a platform team takes on. Datadog → self-hosted Mimir; Splunk → Grafana stack; Honeycomb → Datadog. Each can take 6-18 months and break things along the way. This chapter is the patterns that make the migration tractable, and the anti-patterns that doom it.

This chapter assumes the rest of the folder. You're not picking observability for the first time; you're moving from one to another, in production, while everyone keeps shipping features.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [The five reasons to migrate](#2-reasons)
3. [Migration patterns: the four shapes](#3-shapes)
4. [Dual-write with reconciliation](#4-dual-write)
5. [Read migration first](#5-read-first)
6. [Service-by-service vs all-at-once](#6-service-by-service)
7. [The "what to migrate" inventory](#7-inventory)
8. [Backfill economics](#8-backfill)
9. [Cutover and freeze windows](#9-cutover)
10. [The decommissioning step](#10-decommission)
11. [Common migrations and their gotchas](#11-common-migrations)
12. [Anti-patterns](#12-anti-patterns)
13. [Worked example: Datadog → Mimir + Loki + Tempo](#13-worked-example)
14. [Pitfalls](#14-pitfalls)
15. [Mental models](#15-mental-models)

---

## 1. Thesis

Three claims:

1. **Vendor migration is a 6-18 month project.** Pretending otherwise is how teams get stuck mid-migration with two vendors and double the bill.
2. **Migrate reads before writes.** A team that can read the new system but writes only the old can't migrate. Build query parity first; switch writes last.
3. **Most of the cost is dashboards, alerts, and runbooks, not data.** The data is replayable; the human-built artifacts are not. Plan their migration explicitly.

If your team is starting a vendor migration with "we'll figure it out as we go," budget for an extra 6 months and a quarter of unplanned outages. This chapter is the better way.

---

## 2. The Five Reasons to Migrate

The honest motivations.

### 2.1 Cost

The most common. SaaS bill grows past the threshold where self-hosted (or another SaaS) is cheaper. Math: typically 3-10× cheaper at scale.

### 2.2 Capability

The current vendor lacks a needed feature: continuous profiling, lakehouse-grade SQL, certain integrations, regulatory compliance.

### 2.3 Strategic

Vendor consolidation in the org; multi-cloud move; acquisition.

### 2.4 Operational

Current vendor's UX, support, or reliability is unacceptable.

### 2.5 Lock-in resistance

Team wants vendor-agnostic stack; OTel as substrate; less platform risk.

### 2.6 The "wrong reasons"

- "I just heard X is better" (without comparable scale data).
- "The vendor disappointed us once" (one incident isn't enough).
- "We want to consolidate to fewer tools" (without quantifying current pain).

The right reason: a quantified, leadership-aligned motivation with clear success criteria.

---

## 3. Migration Patterns: The Four Shapes

How migrations are structured.

### 3.1 Big bang

Cut everyone over on a single day. Old vendor off; new vendor on.

**Pros:** Short pain; clear before/after.
**Cons:** All risk concentrated; rollback hard; teams blindsided.

Almost never the right choice for production observability migrations. Reserved for very small orgs (10 services or fewer).

### 3.2 Service-by-service

Per-service migration. Each team migrates its own services on its schedule.

**Pros:** Risk distributed; teams own their migration; rollback per service.
**Cons:** Long total duration; two stacks coexist for the duration; dashboards split across vendors.

The right default for most orgs.

### 3.3 Dual-write with strangler

Both vendors receive every signal during transition. Reads gradually shift. Eventually old vendor is decommissioned.

**Pros:** Zero data loss; reads testable on new before commitment; rollback easy.
**Cons:** Bill is doubled during transition; cardinality / quota at both sides.

The right pattern for cost-sensitive but risk-averse migrations.

### 3.4 Read-only parallel

New vendor receives all signals; old vendor remains the alerting/operational source. After read parity is confirmed, writes also fully migrate.

**Pros:** New vendor verified before any operational dependency; no rollback risk during build.
**Cons:** Slow; old vendor isn't decommissioned until very late.

Used when the migration is risk-averse and cost-tolerant.

### 3.5 The choice

| Org size | Risk tolerance | Pattern |
|---|---|---|
| Small | High | Big bang |
| Mid | Medium | Service-by-service |
| Large | Low | Dual-write |
| Highly regulated | Very low | Read-only parallel + dual-write |

Most production migrations end up dual-write or service-by-service.

---

## 4. Dual-Write With Reconciliation

The pattern in detail.

### 4.1 The architecture

```
service → OTel collector ──┬──→ Old vendor (Datadog)
                           └──→ New vendor (Mimir)
```

The OTel collector fans out to both. Both ingest in parallel. Bill is roughly 2×.

### 4.2 The reconciliation

For each signal:
- Periodically run the same query on both vendors.
- Compare results.
- Flag differences.

```
new = query("rate(http_requests_total[5m])", new_vendor)
old = query("rate(http_requests_total[5m])", old_vendor)
diff_pct = abs(new - old) / old
if diff_pct > 0.02: alert  # > 2% divergence
```

### 4.3 The expected drift

Expect 1-3% drift between vendors:
- Different sampling implementations.
- Slightly different timing.
- Aggregation rounding.
- Cardinality enforcement differences.

> 5% drift is suspicious. Investigate before trusting new vendor for SLOs.

### 4.4 The cardinality match

The single highest-leverage check: does new vendor see the same active series count as old?

```
old: 12,450,000 active series
new: 11,890,000 active series
diff: -4.5%

→ investigate; either drop rules in collector or quota difference at new vendor
```

### 4.5 The dual-write duration

Typical: 60-180 days.
- 60 days: fast migration; high confidence in tooling.
- 180 days: cautious; many edge cases to verify.

After dual-write, *cutover* writes (the new vendor becomes the primary source); *deprecation* of old happens later.

---

## 5. Read Migration First

The discipline that makes migrations work.

### 5.1 The principle

Engineers must be able to *read* the new vendor before any operational dependency moves there. Otherwise, the moment writes flip, engineers are locked out of their own data.

### 5.2 The phases

```
Phase 1: dual-write only
  - Both vendors ingest.
  - Old is operational; new is invisible.
  - Reconciliation runs.

Phase 2: read parity
  - Dashboards mirrored on both.
  - Queries return matching results.
  - Engineers familiar with new vendor's UI.

Phase 3: read primary
  - Dashboards on new are canonical.
  - Old kept for disaster recovery.

Phase 4: write primary
  - Old vendor write is shut off.
  - Old is decommissioned (after retention).
```

### 5.3 The dashboard migration

The hardest engineering of the migration. Dashboards are bespoke; per-team.

The shortcut: code-generate from the same source.

- For Grafana → Grafana (same UI, different datasources): just change datasource references.
- For Datadog → Grafana: more work; each dashboard re-built.

Tools: `dashboards-as-code` (Grafonnet, Grafana CDK, Terraform). Once dashboards are code, swapping datasources is easier.

### 5.4 The alert migration

Like dashboards: bespoke; team-owned. Same approach: alerts-as-code (`doc 12 §10`); swap datasources or rewrite expressions.

The alert rules in PromQL → Datadog Monitor language is non-trivial. PromQL → PromQL (Mimir) is trivial. Direction matters.

### 5.5 The runbook migration

Runbooks reference dashboards and alerts. After the migration, links must be updated. Often missed; surfaces during the next incident.

A pre-migration sweep: identify all runbook URLs; update at cutover.

---

## 6. Service-by-Service vs All-At-Once

The granularity choice.

### 6.1 The service-by-service approach

Each team migrates its services. Order:
1. Smallest, most autonomous teams first (proof of concept).
2. Scale to larger teams.
3. Complex / shared services last.

Each service migrates dashboards, alerts, runbooks.

### 6.2 The shared-services problem

Some services span teams: database observability, k8s control plane, mesh. These don't have one team to migrate them; the platform team does.

Plan these as separate workstreams.

### 6.3 The trace-graph problem

Traces span services. If half are on old vendor and half on new, the trace can't be assembled.

Solution: traces migrate via dual-write throughout the entire migration; cutover happens *after* every service has dual-write enabled.

### 6.4 The cardinality / quota problem

Old vendor has team A and B both reporting. New vendor must accept both before A and B can stop the dual-write. If new vendor's team A quota is set assuming B has migrated, and B hasn't: capacity issue.

Plan capacity for the maximum: both teams reporting fully.

### 6.5 The communication overhead

Service-by-service requires per-team communication: when, what, how. Platform team coordinates dozens of mini-projects.

A program-management role (or rotating champion) is essential.

---

## 7. The "What to Migrate" Inventory

Before any work, the inventory.

### 7.1 The components

- **Metrics:** which metrics, which labels, which retention.
- **Logs:** which log streams, which retention, which redaction rules.
- **Traces:** which services, which sampling rules.
- **Profiles:** which services.
- **Dashboards:** which (and how many).
- **Alerts:** which.
- **SLOs:** how defined.
- **Runbooks:** referencing what.
- **Integrations:** Slack, PagerDuty, GitHub Actions, etc.
- **Custom data:** custom events, custom integrations.

Each line item is a migration task.

### 7.2 The estimation

Per item:
- Effort to migrate (engineer-hours).
- Owner.
- Migration window.
- Verification.

Sum: total project effort. Often shocking. Plan accordingly.

### 7.3 The "delete instead of migrate" pattern

For each item, ask: is this still useful? If not, *delete* before migrating.

The migration is a forced hygiene cycle. Often 30%+ of items are deletable. Saves migration cost + ongoing cost.

### 7.4 The "promote to OTel" prerequisite

If services use vendor-specific instrumentation (Datadog SDK, New Relic agents): convert to OTel first. Then the migration is "swap the backend"; without it, the migration is also a re-instrumentation project.

This is the single most-important pre-migration investment.

---

## 8. Backfill Economics

The historical-data question.

### 8.1 The choice

Three options for old data:
- **Don't migrate.** Old vendor kept read-only for retention period; new vendor only has post-migration data.
- **Selective migration.** Only critical data (SLO compliance, audit) migrated.
- **Full migration.** All historical data moved.

### 8.2 The cost

Full backfill is *expensive*:
- Egress from old vendor (often $/GB).
- Ingest into new vendor (counts against quota).
- Compute for the migration.

Typical: $50k-$500k for a full backfill.

### 8.3 The recommendation

For most orgs: don't migrate historical data. Keep old vendor read-only for the retention period; cancel after.

Selective migration only for compliance / audit data that must be queryable in the new system.

### 8.4 The "two systems coexist" period

After cutover:
- New vendor: live data.
- Old vendor: read-only; pay for retention; engineers refer for historical questions.

Duration: until old vendor's retention period elapses (typically 1-2 years).

### 8.5 The double-bill window

During dual-write *and* the post-cutover retention period: both vendors charged. Plan for it.

---

## 9. Cutover and Freeze Windows

The high-risk transition.

### 9.1 The cutover decision

When all of:
- Reconciliation < 2% drift.
- Read parity confirmed.
- Dashboards / alerts mirrored.
- Runbooks updated.
- Team trained.
- Game day passed.

Then: cutover writes.

### 9.2 The freeze window

During cutover:
- No new feature deploys (one variable changing at a time).
- No new alerts / dashboards (focus on migration).
- Increased platform-team availability.
- Status updates to all engineering.

Typical: 1-7 days.

### 9.3 The rollback plan

Cutover doesn't go right? Rollback to dual-write. Old vendor is still receiving signals (kept dual-write longer than strictly needed for this reason). Reads switch back to old. Cutover retried later.

### 9.4 The cutover itself

```
T-7d   announce cutover window
T-1d   final reconciliation; team training
T-0    cutover starts
T+1h   verify ingestion happy on new
T+4h   verify alerts firing correctly on new
T+24h  declare success or rollback
```

### 9.5 The "cutover succeeded" vs "we forgot to migrate X"

A successful cutover often surfaces forgotten items: an old runbook still pointing at old vendor; an alert evaluator still reading old; a custom integration nobody documented.

Each is a follow-up task. The migration isn't done at cutover; it's done at decommission.

---

## 10. The Decommissioning Step

The often-skipped final step.

### 10.1 The criteria

Old vendor can be decommissioned when:
- All reads have migrated.
- All writes have migrated.
- Retention period (compliance) has elapsed.
- No services / runbooks / dashboards reference it.
- Final data export (if needed) complete.

### 10.2 The procedure

1. Notify users (final warning).
2. Disable writes (if not already).
3. Run final reconciliation.
4. Export any remaining data.
5. Cancel vendor contract.
6. Remove credentials.
7. Update documentation.
8. Audit for stragglers.

### 10.3 The "old runbook resurfaces" failure

A year later, an incident response runs an old runbook that references the old vendor. The vendor is gone; the runbook fails.

Defense: aggressive runbook search before decommission. Periodic audit after.

### 10.4 The cost-savings realization

The decommission is when the *promised* cost savings actually realize. Track:
- New vendor cost.
- Old vendor cost (going to zero).
- Net savings.

Without decommission, you're paying both forever.

---

## 11. Common Migrations and Their Gotchas

The notable cases.

### 11.1 Datadog → self-hosted (Mimir + Loki + Tempo)

The most common 2026 migration. Driven by cost.

Gotchas:
- Datadog's vendor-specific instrumentation (DD APM tracer) → OTel.
- Datadog's metric naming (mostly compatible with PromQL but quirks).
- Datadog's powerful UI features missing in Grafana.
- Operational overhead of self-hosted.

### 11.2 Splunk → Loki / Elastic / Grafana

Driven by cost (Splunk historically expensive). Loki cheaper for label-based; Elastic for full-text.

Gotchas:
- SPL → LogQL/ES query rewrite.
- Splunk-specific dashboards.
- Compliance certifications must transfer.

### 11.3 New Relic / AppDynamics → Datadog

Driven by capability or strategic. Within-SaaS migration.

Gotchas:
- All vendor-specific instrumentation must convert.
- APM features differ (Datadog APM vs NR APM).
- Runbook and dashboard re-creation.

### 11.4 Honeycomb → Datadog (or vice versa)

Driven by team preference / unification.

Gotchas:
- Honeycomb's high-cardinality model → Datadog's metric-tag-cardinality limits.
- Trace-centric workflows differ.
- Pricing models very different.

### 11.5 Prometheus + Grafana → Datadog (reverse migration)

Driven by team-preference for unified UX. Less common.

Gotchas:
- PromQL → Datadog query language.
- Self-hosted alert rules → Datadog Monitors.
- Cost goes *up*.

### 11.6 Cloud-native (CloudWatch / Stackdriver) → multi-cloud unified

Driven by multi-cloud strategy.

Gotchas:
- Cloud-native instrumentation per-cloud.
- Cost of cross-cloud egress.
- Each cloud's quirks remain.

---

## 12. Anti-Patterns

1. **Big bang at scale.** All risk concentrated.
2. **Cutover writes before reads.** Engineers locked out.
3. **No reconciliation.** Drift undetected.
4. **No dashboard migration plan.** Discovery during incident.
5. **No runbook updates.** Old runbooks fail at next incident.
6. **No decommission step.** Both vendors paid forever.
7. **Vendor-specific instrumentation.** Migration is also re-instrumentation.
8. **No pre-migration hygiene.** Migrating obsolete dashboards.
9. **No game day.** Cutover untested.
10. **No rollback plan.** Stuck if cutover fails.
11. **No budget for double-bill.** Surprise.
12. **No migration champion.** Coordination chaos.
13. **No team training.** Engineers struggle on new tool.
14. **No SLO comparison.** Quality of migration unmeasured.
15. **No clear motivation.** Migration drifts; cancelled mid-flight.

---

## 13. Worked Example: Datadog → Mimir + Loki + Tempo

Concrete and complete.

### 13.1 The starting state

- 200 services on Datadog.
- $2.5M / year.
- 12M active series; 4 TB logs/day; 100% trace sampling (cost reason).
- Mature platform team; OTel some-services.

### 13.2 The plan

Phase 1: prep (3 months)
- Convert all Datadog APM tracers → OTel.
- Build self-hosted Mimir + Loki + Tempo (in DR-region first).
- Tune capacity; multi-region active-active.

Phase 2: dual-write (3 months)
- OTel collector fans out to both Datadog and self-hosted.
- Reconciliation queries running.
- Drift < 2% achieved.

Phase 3: read parity (3 months)
- Dashboards re-created in Grafana (using grafonnet).
- Alerts re-created in Mimir (using sloth).
- Engineers trained.
- Read on Grafana; alerts cross-checked on both.

Phase 4: cutover writes (1 week)
- Stop Datadog ingest.
- All writes to self-hosted only.
- 7-day intensive monitoring.

Phase 5: decommission (12 months)
- Datadog kept read-only for retention.
- Selective historical data export (audit).
- Final cancellation Q3 2027.

### 13.3 The cost trajectory

```
Year 1 (2026): $2.5M Datadog + $400K self-hosted = $2.9M (peak)
Year 2 (2027): $1.5M Datadog (decreasing) + $600K self-hosted = $2.1M
Year 3 (2028): $0 Datadog + $700K self-hosted = $700K (target)
```

3-year total: $5.7M. Without migration: $7.5M (with growth). Net savings: $1.8M.

### 13.4 The risks managed

- Read-first prevented cutover surprises.
- Reconciliation caught 3 mismatched metrics during dual-write.
- Game day surfaced 2 critical missing alerts.
- Rollback capability used once (during a debug session); pivot smooth.

### 13.5 The team experience

- Initial confusion learning Grafana / PromQL.
- Strong support from platform team (office hours, training).
- Resistance from one team (used Datadog for 6 years); resolved with leadership.
- Net: improved skills; less vendor lock-in fear; reduced cost.

---

## 14. Pitfalls

1. **No clear motivation.** Migration drifts.
2. **Big bang.** All risk.
3. **Writes before reads.** Locked out.
4. **No reconciliation.** Drift.
5. **No dashboard migration.** Surprise.
6. **No runbook update.** Failed runbooks.
7. **No decommission.** Double-pay.
8. **Vendor-specific instrumentation.** Double work.
9. **No pre-migration hygiene.** Migrating obsolete.
10. **No game day.** Untested.
11. **No rollback.** Stuck.
12. **No budget for double-bill.** Surprise.
13. **No champion.** Coordination chaos.
14. **No team training.** Struggling engineers.
15. **No SLO comparison.** Quality unmeasured.

---

## 15. Mental Models

> **Migrations are 6-18 months. Plan accordingly.**

> **Read first; write later. Engineers must be able to read the new system.**

> **OTel-first instrumentation makes vendor migration tractable. Without it, every migration is a reinstrumentation.**

> **Dual-write with reconciliation is the safe pattern. Bill 2× during transition.**

> **Most cost is dashboards, alerts, runbooks. Plan their migration explicitly.**

> **Service-by-service distributes risk. Big bang concentrates it.**

> **Backfill of historical data is rarely worth the cost. Keep old vendor read-only.**

> **Decommission is the final step. Without it, savings don't realize.**

> **Pre-migration hygiene is the cheapest engineering hour. Delete before migrating.**

> **Game day before cutover. Untested cutovers fail.**

Now go to `doc 38` (continuous verification).
