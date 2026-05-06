# 36 — Disaster Recovery for the Observability Stack

> When the platform fails during the outage you need it for, you discover whether you've planned for it. Most teams haven't. This chapter is about RPO/RTO for telemetry, cross-region replication, the "blind during incident" failure mode, and the discipline of treating the platform as a production service.

This chapter assumes `doc 28` (telemetry pipeline reliability) and `doc 33` (federated multi-region). DR builds on both.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [Why telemetry DR is special](#2-special)
3. [RPO and RTO for telemetry](#3-rpo-rto)
4. [The "blind during incident" failure mode](#4-blind)
5. [Backup and restore patterns](#5-backup-restore)
6. [Cross-region replication](#6-replication)
7. [Failover procedure](#7-failover)
8. [Backfill from durable buffer](#8-backfill)
9. [Game day testing](#9-game-day)
10. [Recovery validation](#10-validation)
11. [Anti-patterns](#11-anti-patterns)
12. [Worked example: a regional failure with successful recovery](#12-worked-example)
13. [Pitfalls](#13-pitfalls)
14. [Mental models](#14-mental-models)

---

## 1. Thesis

Three claims:

1. **The observability stack must survive its consumers' worst day.** When customer-facing services are down, the platform is needed *more*, not less. Architect for that.
2. **RPO ≠ 0 is acceptable for most telemetry.** A 5-minute gap during a regional failure is recoverable; pretend otherwise and the architecture cost is not justified.
3. **Game days are mandatory.** The DR plan that's never tested is the DR plan that fails. Quarterly minimum.

If your platform team can describe how things would fail but hasn't actually triggered a failover in production, your plan is theoretical. This chapter is about making it real.

---

## 2. Why Telemetry DR Is Special

| Dimension | Customer-facing service | Telemetry platform |
|---|---|---|
| **Failure visibility** | Customers complain | Service teams complain (slowly) |
| **Pressure during incident** | Lots of attention | Often forgotten until needed |
| **Data loss tolerance** | Often near-zero | Often 5-15 min acceptable |
| **Failover speed** | Minutes to hours | Hours acceptable for hot stack |
| **Customer comms** | External status page | Internal notification |

The constraints are different. Don't apply customer-facing-service DR rigor blindly.

### 2.1 The "needed during outage" property

When the customer-facing stack is down, the platform is the *most* important system in the org. Engineers can't debug without it. That's the failure mode that justifies investment.

### 2.2 The "platform is not customer-facing" advantage

Internal users have higher patience than customers. A 30-minute platform recovery is acceptable when external services would be measured in seconds.

---

## 3. RPO and RTO for Telemetry

The targets.

### 3.1 The definitions

- **RPO (Recovery Point Objective):** maximum acceptable data loss. "We lose at most X minutes of telemetry."
- **RTO (Recovery Time Objective):** maximum acceptable downtime. "Telemetry is restored within Y minutes."

### 3.2 The targets per signal

| Signal | RPO | RTO |
|---|---|---|
| **Audit logs** | 0 (no loss) | 1 hour |
| **SLI metrics** | 5 minutes | 30 minutes |
| **Application metrics** | 15 minutes | 2 hours |
| **Application logs** | 15 minutes | 2 hours |
| **Traces** | 30 minutes | 4 hours |
| **Profiles** | 1 hour | 12 hours |

Audit logs: zero data loss; compliance-critical.
SLI metrics: tight RPO; SLO calculations depend.
Profiles: lax; not load-bearing.

### 3.3 The RPO mechanism

RPO is achieved via:
- Durable buffering (Kafka) before storage.
- Replication of storage.
- Backups of cold tier.

Without one of these, RPO = "whatever happens to be in flight when the failure hit."

### 3.4 The RTO mechanism

RTO is achieved via:
- Hot standby (warm or active).
- Documented failover procedure.
- Tested recovery.

Without these, RTO = "however long it takes to set up a new cluster."

### 3.5 The cost trade-off

Tighter RPO/RTO = more cost. RPO=0 needs synchronous replication; RTO=0 needs hot active failover.

For most observability platforms: RPO 5-15 min, RTO 30-120 min is sufficient. Don't over-invest.

---

## 4. The "Blind During Incident" Failure Mode

The single most expensive observability failure.

### 4.1 The scenario

Production has a real outage. Engineers turn to dashboards. Dashboards are stale or fail. Engineers are blind. The outage extends.

### 4.2 The causes

- Platform broken before / during the incident.
- Pipeline backed up; recent data not yet queryable.
- Alerting evaluator failing; no pages.
- Cross-region failover broken.

### 4.3 The defenses

Cross-link to `doc 28`:
- Independent observation path (tier-0 alerts).
- Synthetic canaries.
- Internal status page.
- Separate paging for platform itself.

Plus DR-specific:
- Hot replicas in another region.
- Pre-tested failover.
- Documented runbook.

### 4.4 The "platform incident" simulation

Once a quarter, simulate the platform itself going down. Verify:
- Independent path triggers tier-0 alerts.
- Synthetic canary fails.
- Backup paging path works.
- Engineers can find the runbook.
- Failover procedure is up to date.

The first time you test this in production, you'll find it broken.

---

## 5. Backup and Restore Patterns

The mechanics.

### 5.1 What needs backing up

- **Configuration.** Tenant configs, alert rules, dashboards, recording rules, SLO definitions.
- **Index state.** TSDB blocks (Mimir / Cortex blocks in object storage *are* the backup).
- **Audit logs** (write-once is a backup).
- **Catalog state** (Iceberg metadata, Hive metastore).

### 5.2 What's recoverable from upstream

- **Raw telemetry** can be replayed from Kafka if retained long enough.
- **Compacted data** is in object storage; survives most cluster failures.
- **Hot ingester state** lost; rebuild from upstream.

### 5.3 The "config in Git" principle

Configuration as code:
- Mimir / Loki / Tempo configs in Git.
- Alert rules in Git.
- Dashboards as JSON in Git.
- SLO definitions in Git.
- Tenant configs in Git.

Any cluster can be rebuilt from Git + the object store backups.

### 5.4 The runbook

The DR runbook lives in the *platform-incident runbook* (`doc 28 §12.2`). Specific steps:

1. Provision new infrastructure in DR region.
2. Apply config from Git.
3. Connect to backup object store.
4. Verify data accessibility.
5. Redirect traffic.
6. Monitor; validate.

Documented; tested; updated.

### 5.5 Object store for backup

Object storage (S3, GCS) is durable by design (11+ nines). For most telemetry, *the active object store is the backup*. No separate copy needed; cross-region replication for RPO.

For audit logs: separate immutable bucket (S3 Object Lock).

---

## 6. Cross-Region Replication

The continuous-recovery mechanism.

### 6.1 The replication options

| Method | RPO | RTO | Cost |
|---|---|---|---|
| **Synchronous replication** | 0 | Seconds | Highest (latency, network) |
| **Asynchronous replication** | minutes | minutes | Medium |
| **Periodic backup + restore** | hours | hours | Low |

For observability: usually async (RPO few minutes acceptable).

### 6.2 Cross-region object storage

S3 Cross-Region Replication: source bucket → dest bucket. Async; minutes lag.

```
us-east-1 bucket (primary) → us-west-2 bucket (replica)
```

If us-east-1 fails, telemetry data is in us-west-2. Spin up Mimir in us-west-2; point at the replica.

### 6.3 The Kafka mirror

For Kafka: Confluent's MirrorMaker 2 / Cluster Linking replicates topics across regions.

```
Primary Kafka (us-east) → mirror → Secondary Kafka (us-west)
```

If primary fails, secondary has all messages. Resume ingestion from there.

### 6.4 The configuration replication

Config is in Git, which is replicated by the Git host (GitHub, GitLab) automatically. CI applies on demand.

### 6.5 The cost

Cross-region replication: usually $0.01-$0.02/GB transfer. For 1 TB/day of telemetry: ~$300/month. Cheap insurance.

---

## 7. Failover Procedure

The runbook.

### 7.1 The triggers

- Primary region down (multiple AZ failures).
- Sustained latency / error rate from primary.
- Disaster declared by leadership.

### 7.2 The procedure

```
1. Confirm primary is genuinely down (avoid false failover).
2. Page DR lead + platform team.
3. Activate DR runbook.
4. Spin up DR cluster (or scale up warm standby).
5. Apply config from Git.
6. Point DNS / load balancer to DR region.
7. Verify ingestion accepting (synthetic canary check).
8. Verify queries serving (Grafana check).
9. Notify users via status page.
10. Continuously verify until primary is restorable.
```

### 7.3 The decision threshold

Failover is *not free*: cost + risk of getting it wrong (data inconsistency, false alarm).

Threshold: primary down for > 30 minutes, no clear restoration path. Otherwise wait.

### 7.4 The fail-back

When primary is restored:
1. Backfill primary from secondary (replay).
2. Verify primary catches up.
3. Switch traffic back.
4. Decommission DR (back to standby state).

Fail-back is often *harder* than fail-over. Plan it explicitly.

### 7.5 Active-active alternative

Skip the failover entirely: both regions ingest and query continuously. One failing leaves the other operational without a switch.

Cross-link to `doc 33 §3.3`. More expensive; simpler operationally during failure.

---

## 8. Backfill From Durable Buffer

The recovery layer.

### 8.1 Kafka as the durability backbone

Kafka retains messages for N hours/days. If downstream storage failed for those hours, replay from Kafka:

```
1. Storage restored.
2. Reset consumer offsets to start of incident window.
3. Re-consume into storage.
4. Storage catches up to current.
```

A 4-hour outage with 24-hour Kafka retention: recoverable. Without Kafka, the data is lost.

### 8.2 The backfill SLO

```
backfill_complete_within_X_hours_of_recovery = ≤ 4 hours
```

This is *the* DR SLO for ingest. If you can't catch up within X hours after recovery, something is wrong with the system.

### 8.3 The capacity for backfill

Backfill rate must exceed normal ingestion rate; otherwise you never catch up. Typical: 2-5× capacity for backfill.

### 8.4 The "out of order" tolerance

Backfilled data may arrive out of order. Storage must accept it (idempotent or out-of-order-tolerant). Most modern TSDBs are.

### 8.5 The "missing data" report

Document gaps explicitly. SLO calculations during the gap window are estimates, not measurements. Postmortems reference the gap.

---

## 9. Game Day Testing

The discipline.

### 9.1 The cadence

Quarterly minimum. Variants:

| Type | What | When |
|---|---|---|
| **Tabletop** | Walk through the runbook in a meeting | Monthly |
| **Limited drill** | Failover one component (e.g., one ingester) | Quarterly |
| **Regional drill** | Simulate one region's failure | Bi-annually |
| **Full disaster** | Total platform failure simulation | Annually |

### 9.2 The format

1. Schedule.
2. Plan: what's the simulated failure?
3. Inject (or simulate).
4. Team responds per runbook.
5. Observe what fails.
6. Recover.
7. Postmortem.
8. Update runbooks; close gaps.

### 9.3 The "did the runbook work?" question

Most game days reveal:
- Stale runbook steps.
- Missing tooling.
- Dependencies on the broken thing.
- Unclear decision criteria.
- Single-points-of-failure not previously known.

Each finding becomes a fix.

### 9.4 The learning value

Even when the game day "succeeds," the team learns:
- Which steps are slow.
- Where automation would help.
- What runbook content is unclear.
- Who knows what.

The exercise is the value.

---

## 10. Recovery Validation

How you know it worked.

### 10.1 The validation checklist

- Ingest accepting (write succeeds).
- Query returning (read succeeds).
- Recent data present (no gap beyond RPO).
- Older data accessible (object store reachable).
- Alerts evaluating (recording rules working).
- Dashboards loading.
- Tenants isolated correctly.
- Synthetic canaries passing.

### 10.2 The validation script

Automate the checks:

```bash
./validate-recovery.sh
  - sends test metric, queries it, expects sub-30s appearance
  - sends test log, queries, expects same
  - sends test trace, queries, expects same
  - calls each tenant's quota; expects enforcement
  - runs a representative recording rule; expects success
  - runs a representative dashboard query; expects success
```

Pass = recovery verified. Fail = continue investigation.

### 10.3 The communications

When validated, status-page-update: "Telemetry platform fully recovered. Backfill complete. SLO impact during the window: TBD; full report by end of week."

### 10.4 The followup

Recovery is the start, not the end:
- Postmortem.
- Action items.
- Game day update.
- Runbook update.
- Capacity / config change if needed.

---

## 11. Anti-Patterns

1. **No DR plan.** Hope-based recovery.
2. **DR plan untested.** Theoretical only.
3. **No game day cadence.** Plan rots.
4. **Single-region storage.** No backup.
5. **No Kafka durability.** Data lost during outage.
6. **No config in Git.** Cluster rebuild slow.
7. **No backfill capacity.** Catch-up impossible.
8. **No internal status page.** Service teams confused.
9. **No tier-0 alerts on platform.** Failures invisible.
10. **No active failover testing.** Plan fails on first real use.
11. **Tight RPO/RTO without budget.** Architecture under-funded.
12. **Loose RPO/RTO without explicit policy.** SLO calculations diverge.
13. **No backup verification.** Backups exist; can't restore.
14. **No fail-back plan.** After failover, can't return.
15. **DR responsibility undefined.** When trigger hits, no clear owner.

---

## 12. Worked Example: A Regional Failure With Successful Recovery

Concrete and complete.

### 12.1 The setup

- Active-active 3-region observability platform.
- Mimir + Loki + Tempo per region; cross-region replication via Kafka MirrorMaker.
- Object storage replicated (S3 CRR).
- 3-region paging path (independent).

### 12.2 The incident

us-east-1 has an AZ failure cascading to multiple AZs. Primary Mimir cluster degrades.

T+0     us-east-1 AZ-A fails
T+5m    Mimir us-east ingest error rate climbs to 30%
T+8m    Tier-0 alert fires (independent path): "us-east ingest unhealthy"
T+10m   Platform on-call investigates; identifies AZ failure
T+15m   Decision: failover us-east traffic to us-west-2
T+18m   DNS updated; new ingest goes to us-west
T+20m   us-west catching up Kafka backlog
T+30m   Backlog cleared; full ingest at us-west
T+45m   us-east AZ-A recovers
T+60m   Backfill us-east from Kafka; verify data integrity
T+90m   us-east traffic restored; us-west reverts to standby
T+120m  Postmortem timeline written

### 12.3 The data integrity

- Audit logs: zero loss (multi-region replication; Kafka durable).
- SLI metrics: ~5 minutes gap during the failover (within RPO).
- Application logs: ~5 minutes gap.
- Traces: ~10 minutes (some in-flight lost; acceptable).

### 12.4 The customer impact

- Internal users: dashboards stale for ~10 minutes during failover.
- Service teams' SLO calculations: gap noted; recomputed for window.
- No customer-facing impact.

### 12.5 The lessons

- Tier-0 alerts triggered correctly.
- Failover took 18 min (target was 30; better than planned).
- Backfill took ~30 min (target was 60).
- Two minor runbook gaps surfaced; updated.

### 12.6 The postmortem

Standard postmortem (`doc 15 §12`). Action items:
- Automate failover decision (some steps still manual).
- Improve catch-up rate.
- Document the AZ-level resilience plan.

---

## 13. Pitfalls

1. **No DR plan.** Hope-based.
2. **Untested.** Theoretical.
3. **No game day.** Drift.
4. **No durability buffer (Kafka).** Data loss.
5. **No config in Git.** Slow rebuild.
6. **Single-region.** No backup.
7. **No tier-0 alerts.** Failures invisible.
8. **No internal status page.** Confusion.
9. **No fail-back plan.** Stuck in DR.
10. **No backup verification.** False confidence.
11. **Tight RPO/RTO under-funded.** Architecture mismatch.
12. **Loose RPO without policy.** Drift.
13. **No backfill capacity.** Stuck behind.
14. **No data integrity check.** Corruption undetected.
15. **No DR ownership.** Confusion at trigger.

---

## 14. Mental Models

> **The platform is needed most during outages. Architect accordingly.**

> **RPO ≠ 0 is fine for telemetry. Don't over-invest.**

> **Config in Git + object-store backup = rebuildable cluster.**

> **Kafka is the durability layer. Without it, in-flight loss is permanent.**

> **Cross-region replication for RPO; warm standby for RTO.**

> **Game days are mandatory. Untested plans fail.**

> **The independent observation path tells you when the platform is failing.**

> **Active-active beats failover for high-bar SLOs. More expensive but simpler operationally.**

> **Backfill SLO is the recovery measure. "We caught up within X hours."**

> **Validate recovery explicitly. Documented checklist; not just "looks ok."**

Now go to `doc 37` (vendor migration patterns).
