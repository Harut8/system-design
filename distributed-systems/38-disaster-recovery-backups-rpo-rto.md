# Chapter 38: Disaster Recovery — Backups, Point-in-Time Recovery, RPO/RTO, and Restore Drills

What to do when the data itself is wrong or gone: the primary database was dropped, a migration
wiped a column, a deploy has been corrupting rows for three weeks, or an attacker with admin
credentials deleted production and then the backups. This chapter separates the disasters that
replication handles (a machine, zone or region dies) from the ones it makes worse (every replica
faithfully copies the mistake), defines RPO and RTO precisely and shows how to measure the real
values instead of the ones in the slide deck, and works through backup types, PostgreSQL
continuous archiving and point-in-time recovery (pgBackRest, WAL-G, managed services), the other
state a company forgets to back up (object storage, Kafka, Redis, search, encryption keys, config),
backups that survive a ransomware operator, restore drills, four step-by-step recovery runbooks,
GDPR erasure versus backups, DR observability, and a stage-by-stage decision guide that starts with
the cheapest option that works.

Scope boundary. Active-active, warm standby across regions, failover and region evacuation are in
[`36-multi-region-active-active-and-geo-replication.md`](36-multi-region-active-active-and-geo-replication.md);
replication mechanics and lag in [`04-replication-and-consistency.md`](04-replication-and-consistency.md);
WAL internals (records, checkpoints, segment lifecycle, archiving hooks) in
[`../databases/14-write-ahead-log-internals.md`](../databases/14-write-ahead-log-internals.md);
availability math and error budgets in [`35-reliability-math-slos-and-error-budgets.md`](35-reliability-math-slos-and-error-budgets.md);
Kubernetes-level backup with Velero and etcd snapshots in
[`../kubernetes/32-cluster-lifecycle-and-day2.md`](../kubernetes/32-cluster-lifecycle-and-day2.md) §19–§24;
DR for the telemetry stack in [`../sre-observability/36-dr-for-observability-stack.md`](../sre-observability/36-dr-for-observability-stack.md).
This chapter is about **recovering data and service from a known-good copy**.

---

## Table of Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Replication Is Not Backup — A Disaster Taxonomy](#1-replication-is-not-backup--a-disaster-taxonomy)
2. [RPO and RTO Precisely — Definitions, Real Values, RTO Decomposition](#2-rpo-and-rto-precisely--definitions-real-values-rto-decomposition)
3. [DR Tiers, the Cost Curve, and Mapping Criticality to a Tier](#3-dr-tiers-the-cost-curve-and-mapping-criticality-to-a-tier)
4. [Backup Types — Full, Incremental, Logical, Physical, Snapshots, PITR](#4-backup-types--full-incremental-logical-physical-snapshots-pitr)
5. [PostgreSQL Backup and PITR in Practice](#5-postgresql-backup-and-pitr-in-practice)
6. [Backing Up Everything Else](#6-backing-up-everything-else)
7. [Backups That Survive an Attacker — 3-2-1-1-0, Immutability, Ransomware](#7-backups-that-survive-an-attacker--3-2-1-1-0-immutability-ransomware)
8. [Restore Drills — Untested Backups Are Not Backups](#8-restore-drills--untested-backups-are-not-backups)
9. [Recovery Playbooks — Four Runbooks](#9-recovery-playbooks--four-runbooks)
10. [GDPR, Data Residency and Compliance](#10-gdpr-data-residency-and-compliance)
11. [Observability of DR — Backup and Restore SLOs and Alerts](#11-observability-of-dr--backup-and-restore-slos-and-alerts)
12. [Decision Guide — DR by Company Stage](#12-decision-guide--dr-by-company-stage)
13. [Production pitfalls / war stories](#13-production-pitfalls--war-stories)
14. [Interview questions](#14-interview-questions)
15. [Real-world cases — incidents with numbers](#15-real-world-cases--incidents-with-numbers)
16. [Sandbox experiments — run these yourself](#16-sandbox-experiments--run-these-yourself)

- [Key Takeaways](#key-takeaways)
- [Cross-References](#cross-references)
- [References](#references)

---

## Start here — the whole chapter in plain words

**The problem.** Most "high availability" work protects against *hardware* going away: a disk, a
server, a data center. Replicas, multi-AZ databases and multi-region setups all work by copying
every change to another place as fast as possible. That is exactly wrong when the change itself is
the disaster. A `DELETE` without a `WHERE`, a migration that drops the wrong column, a bug that
writes garbage, an attacker who encrypts or deletes data: all of these are copied to every replica
within milliseconds. The only protection is a copy of the data **from before** the mistake, kept
somewhere the mistake (or the attacker) cannot reach, plus a practiced way to get it back in time.
That is disaster recovery (DR): backups, point-in-time recovery, and drills that prove both work.

**A real-world example.** A B2B invoicing SaaS with about 2,500 business customers runs
PostgreSQL 16 on a primary in one AZ with one asynchronous streaming replica in another AZ. The
database is 300 GB and generates about 1 GB of WAL per hour. Backups: pgBackRest takes a full
backup on Sunday at 01:00 and an incremental every other night at 01:00, and archives WAL
continuously to S3; retention is 14 days. (All numbers illustrative.)

- **14:05 Tuesday — the incident.** Migration `0142_move_tax_ids` should copy `customers.tax_id`
  into a new `customer_tax_profiles` table and then drop the old column. A test filter
  (`WHERE country = 'DE'`) was left in the copy step, so only 2,100 of 18,400 tax IDs are copied
  before `ALTER TABLE customers DROP COLUMN tax_id` runs. 16,300 VAT numbers are gone. The replica
  applies the same WAL 40 ms later.
- **14:05–16:30 — the undetected window.** Nothing crashes. The app keeps issuing invoices, now
  without VAT numbers: 1,300 invoices, 90 new customers, 40 customers editing their tax profile,
  hundreds of payments. At 16:30 support escalates "invoices are missing our VAT ID".
- **Why failover does not help (§1).** The replica is healthy and identical to the primary: it has
  the same dropped column. Replication protected against the primary's disk dying, not against
  the primary being told to delete data.
- **Why "restore last night's backup" is wrong (§2, §9.1).** Restoring the 01:00 backup loses
  13 hours of legitimate work. Restoring to 14:04 with PITR loses the 2 h 25 min of invoices and
  payments written after the incident. Both are full rollbacks, and both destroy good data.
- **What actually works (§5, §9.1).** Restore the backup plus WAL to a **side instance**, stopped
  just before the migration's transaction. Read `(id, tax_id)` from it and insert the missing
  profiles into production with `ON CONFLICT DO NOTHING`, so the 40 post-incident edits win. Then
  re-issue the 1,300 affected invoices. No legitimate write is lost.
- **RPO and RTO for this incident (§2).** Recovery point: 14:04:59, zero data loss after the
  surgical merge. Time from detection to repair: about 95 minutes (restore 300 GB, replay 13 h of
  WAL, verify, merge). The *impact* lasted four hours, because detection took 2 h 25 min, and no
  backup setting shortens that part.
- **What would have made it worse (§7, §9.2).** If the bug had been a slow corruption noticed three
  weeks later, 14-day retention would have held no clean copy. If an attacker with the AWS admin
  role had done this deliberately, backups in the same account would have been deleted first.
- **Drills (§8).** Every number above (95 minutes, restore throughput, replay rate) is only known
  because the team restores last night's backup into a scratch environment every night and records
  how long it takes.

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Disaster recovery (DR) | getting data and service back after an event that normal redundancy does not absorb | rebuilding after a flood, not changing a flat tire |
| Replica | a live copy that applies every change immediately | a carbon copy: it copies your mistakes too |
| Backup | a copy of data at a past moment, stored separately | a photocopy of the contract kept in a different building |
| Full / incremental / differential backup | everything / what changed since the last backup / what changed since the last full | a full house inventory / today's new purchases / everything bought since the inventory |
| Logical backup | data exported as SQL or rows (`pg_dump`) | a recipe you can cook again |
| Physical backup | a copy of the database's files (`pg_basebackup`, pgBackRest) | a photo of the finished dish |
| WAL | write-ahead log: every change, in order, before it hits data files | a ship's logbook |
| WAL archiving | shipping each finished WAL file to backup storage | mailing each filled logbook page to the head office |
| PITR | point-in-time recovery: restore a base backup, then replay WAL up to a chosen moment | rewinding a recording to just before the mistake |
| Recovery target | the exact moment, transaction or LSN at which replay stops | "stop the tape at 14:04:59" |
| RPO | recovery point objective: the most data (measured in time) you accept losing | how many pages of the notebook you can afford to lose |
| RTO | recovery time objective: the longest you accept being down | how long the shop may stay closed after the flood |
| RPA / RTA | recovery point / time *actual*: what a drill or incident really achieved | the promised delivery date versus the real one |
| Retention | how long backups are kept before deletion | how many years of tax records you keep |
| Immutable backup | a copy that nobody, including an admin, can delete or change until a date | a document in a time-locked safe |
| Air gap | a copy with no network path from production | a USB drive in a drawer |
| Delayed replica | a replica that deliberately applies changes hours late | a friend who reads your letters a day after you send them, so you can call and say "ignore that" |
| Crypto-shredding | deleting data by destroying the key that decrypts it | burning the only key to a locked diary |
| Restore drill | actually restoring a backup, on a schedule, and checking the result | a fire drill |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| RPO | maximum acceptable data loss, as a time window before the incident | 0 s – 24 h | 5 min for the invoicing DB |
| RTO | maximum acceptable time from disruption (or declaration) to restored service | minutes – days | 4 h |
| RPA, RTA | actual recovery point and time achieved in an incident or drill | measured | RTA 95 min in the example |
| `t_detect` | time from the incident to someone knowing about it | seconds (crash) to weeks (silent corruption) | 145 min |
| `D` | size of the data to restore | GB – TB | 300 GB |
| `B` | effective restore throughput: minimum of source read, network, decompression, target disk | 100 MB/s – 1 GB/s | 200 MB/s |
| `W` | WAL generation rate | 0.1 – 50 GB/h | 1 GB/h |
| `t_base` | time between the base backup used and the recovery target | hours – days | 13 h (from the 01:00 incremental) |
| `R_replay` | WAL replay rate during recovery | tens to low hundreds of MB/s; measure yours | 40 MB/s |
| `archive_timeout` | max seconds before Postgres forces a WAL segment switch so it can be archived | 60 s | bounds RPO of the archive on a quiet DB |
| `L_archive` | archive lag: newest WAL generated minus newest WAL safely archived | seconds normally; hours when broken | 30 s |
| `T_ret` | retention window: how far back you can restore | 7 – 35 days (PITR), months–years (long-term) | 14 days |
| `delay` | apply delay of a delayed replica (`recovery_min_apply_delay`) | 1 – 24 h | 4 h |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. Replication Is Not Backup — A Disaster Taxonomy

> **In plain words.** Replication answers "what if this machine disappears?". Backups answer
> "what if the data is wrong?". They are different questions. A system with five replicas and no
> backups has five copies of whatever the last bad command left behind.
>
> **Real-world example.** At 14:05 the migration drops `tax_id` on the primary; 40 ms later the
> replica drops it too. The replica did its job perfectly, which is the problem.

### 1.1 Three families of disaster

| Family | Examples | What goes wrong | Typical detection time |
|---|---|---|---|
| **Infrastructure** | disk failure, node crash, AZ power loss, region outage, fire, provider account suspended or deleted | the copy of the data (or the place it runs) disappears; the data that remains is correct | seconds to minutes |
| **Logical** | human error (`DELETE` without `WHERE`, `DROP TABLE` in the wrong terminal, wrong `rm -rf`), buggy deploy or migration, malicious insider or attacker, ransomware, application bugs writing wrong values, silent storage corruption (bit rot, firmware bugs) | the data is present everywhere but **wrong**; every replica agrees on the wrong answer | minutes to weeks |
| **Dependency** | SaaS vendor loses your data or terminates the account, DNS provider or registrar problem, expired or revoked TLS certificate or CA, KMS key disabled or scheduled for deletion, identity provider outage | something you do not run holds state you need; recovery depends on someone else or on a copy you kept | minutes to days |

The families differ in one crucial way: **infrastructure disasters destroy copies, logical
disasters corrupt the source of truth**. Adding copies fixes the first and does nothing for the
second, and synchronous replication makes the logical case slightly *worse* (the bad write is
guaranteed to be everywhere before the client even hears "OK").

```
  Infrastructure disaster                     Logical disaster

  primary ──WAL──► replica                    primary ──WAL──► replica
     X  (disk dies)                           DROP COLUMN ─────► DROP COLUMN (40 ms later)
     │                                           │                  │
  promote replica: data intact                promote replica: same damage
  protection = more copies, elsewhere         protection = a copy from BEFORE, out of reach
```

### 1.2 Which protection covers which disaster

Columns are protection mechanisms; "same account" means the copy lives in the same cloud account
or project, with the same administrators and credentials as production.

| Disaster | Sync/async replica (same AZ/region) | Multi-AZ | Multi-region replica | Storage snapshot (same account) | PITR (same account) | Logical dump offsite | Immutable copy in separate account |
|---|---|---|---|---|---|---|---|
| Disk / node failure | Yes | Yes | Yes | Yes (slow) | Yes | Yes (stale) | Yes |
| AZ loss | No (if same AZ) | Yes | Yes | Yes (regional service) | Yes | Yes | Yes |
| Region loss | No | No | Yes | No, unless copied cross-region | No, unless copied | Yes, if other region | Yes, if other region |
| Provider account loss or suspension | No | No | No | No | No | Yes, if other account/provider | Yes |
| `DELETE` / bad migration, noticed within hours | No | No | No | Partial (to snapshot time) | Yes | Partial (to dump time) | Yes (if PITR-capable) |
| Bug corrupting data, noticed after weeks | No | No | No | Only if retained that long | Only if `T_ret` > `t_detect` | Only if retained | Yes, if long retention |
| Attacker with admin credentials / ransomware | No | No | No | No (deleted first) | No (deleted first) | Depends on who can delete it | Yes |
| Silent bit rot on primary storage | Partial (physical replicas usually do not copy on-disk corruption, but can via full-page images) | Partial | Partial | No (snapshots copy corrupt blocks) | Partial (clean base + WAL) | Yes (if the dump read clean data) | Partial |
| KMS key deleted | No | No | No | No (encrypted with same key) | No | Yes, if encrypted with a separate key | Yes, if separate key |

Read the matrix column-wise to see that **only the rightmost column covers every row**, and it is
the one most small companies do not have. Read it row-wise to see that the two most common real
data-loss events (human error and bad code) are covered only by PITR and older copies.

### 1.3 Why logical disasters dominate in practice

- **Hardware redundancy is now a commodity.** Managed databases give multi-AZ failover with a
  checkbox; cloud block storage is replicated inside the AZ. The remaining failures are the ones
  redundancy cannot see.
- **Change is the main cause of incidents.** Deploys, migrations, config pushes and manual
  operations cause a large share of outages in most postmortem collections
  ([`../sre-observability/15-incident-response-and-postmortem.md`](../sre-observability/15-incident-response-and-postmortem.md)).
  Data-changing mistakes are a subset of those, and they are the expensive ones.
- **Detection is slow.** A crashed disk pages someone within a minute. A wrong value in 3% of rows
  may be noticed by a customer three weeks later. `t_detect`, not restore speed, often dominates
  total impact.
- **Attackers target backups.** Ransomware operators routinely look for and delete backups before
  encrypting (§7.4), so "we have backups" must mean "we have backups that survive an admin
  account in hostile hands".

---

## 2. RPO and RTO Precisely — Definitions, Real Values, RTO Decomposition

> **In plain words.** RPO is "how much work can we lose?", measured as time. RTO is "how long can
> we be down?". Companies write both into contracts and slide decks; few measure what they would
> actually get. The real numbers come from drills and from arithmetic on bytes and throughput.
>
> **Real-world example.** The invoicing SaaS promises customers RPO 1 hour and RTO 4 hours. Its
> real RPO for a destroyed primary is about one minute (archive lag). Its real RPO for "the
> archive has been silently failing since Friday" is four days. Its real RTO for a 300 GB restore
> is 70–100 minutes, but only if someone who knows the runbook is awake.

### 2.1 Definitions

- **Recovery Point Objective (RPO)**: the maximum acceptable amount of data loss, expressed as a
  time window before the disruption. RPO = 15 min means "after recovery, at most the last 15
  minutes of acknowledged writes before the incident may be missing".
- **Recovery Time Objective (RTO)**: the maximum acceptable time from the disruption until the
  service is restored to an agreed level. Be explicit about the start of the clock: for crash-type
  events it is the incident start; for logical disasters many teams start it at *declaration*,
  because `t_detect` is not controllable by the recovery process. Write down which one you use.
- **Recovery Point Actual (RPA)** and **Recovery Time Actual (RTA)**: what a drill or a real
  incident achieved. Only these are evidence.
- **Maximum Tolerable Downtime (MTD)**: the business limit beyond which the damage is severe
  (contract penalties, regulatory breach, customer churn). RTO must sit comfortably inside MTD,
  leaving time for work that happens after the systems are back (re-issuing invoices, reconciling
  payments, answering customers).

```
         last good        incident     detected   declared            service back   business normal
         backup point        │            │          │                     │               │
  ───────────┬───────────────┼────────────┼──────────┼─────────────────────┼───────────────┼────►
             │◄─── data lost ►│            │          │                     │               │
             │   (RPA; ≤ RPO) │◄ t_detect ►│◄ decide ►│◄──── restore ──────►│◄─ catch-up ──►│
                              │                       │◄─────── RTA ─────────►│               │
                              │◄──────────────────── total impact (≤ MTD) ────────────────────►│
```

For logical disasters there is a second data-loss question that the RPO number hides: **the good
data written after the incident**. A full rollback to the recovery point also throws away
everything from the incident until the rollback (2 h 25 min in the example). Surgical repair (§9.1)
exists to avoid that.

### 2.2 The real RPO, per mechanism

RPO is not a setting; it is the age of the newest *restorable* copy at the worst moment.

| Mechanism | Nominal RPO | What the real RPO depends on | Worst realistic case |
|---|---|---|---|
| Synchronous replica (failover) | 0 | only for infrastructure loss; 0 only if the sync standby is actually in sync (`synchronous_standby_names` set and standby connected) | if the standby silently disconnected and the primary was configured to continue, async loss |
| Asynchronous replica (failover) | replica lag | lag at the moment of failure | seconds; minutes during heavy writes or a slow replica |
| Continuous WAL archiving + PITR | `archive_timeout` + archive lag | `L_archive`; whether archiving is failing | hours or days if `archive_command` has been failing unnoticed |
| Managed PITR (RDS, Cloud SQL) | about 5 minutes | the provider's log upload interval; check `LatestRestorableTime` | as documented by the provider, plus region-level issues |
| Nightly physical backup, no WAL | 24 h | backup success rate | 48 h+ if last night's backup failed |
| Nightly `pg_dump` | 24 h + dump duration | the dump reflects the *start* of its snapshot | 48 h+ on silent failure |
| Weekly snapshot copied offsite | 7 days | copy job success | weeks |

Two rules follow:

1. **Real RPO = nominal RPO + time a broken backup goes unnoticed.** Monitoring backup age (§11)
   is therefore part of the RPO, not an optional extra.
2. **For logical disasters, RPO is bounded below by granularity.** Snapshot-only strategies can
   restore only to snapshot times. If the bad write came 50 minutes after the hourly snapshot,
   you choose between losing 50 minutes or restoring the damage. PITR removes the granularity
   problem: any second within `T_ret`.

### 2.3 RTO decomposition with worked math

```
RTA = t_decide + t_provision + t_restore + t_replay + t_verify + t_cutover + t_warm
      (t_detect precedes this and is reported separately)

t_restore ≈ D / B        where B = min(source read, network, decompression CPU, target disk write)
t_replay  ≈ (W × t_base) / R_replay
```

| Phase | What happens | Invoicing SaaS (300 GB, surgical repair) | What shrinks it |
|---|---|---|---|
| `t_detect` | somebody notices | 145 min (customer ticket) | data validation checks, anomaly alerts on row counts and null rates |
| `t_decide` | triage: what broke, since when, full rollback or surgical fix | 20 min | a runbook with the decision tree (§9); a named incident commander |
| `t_provision` | get a machine and disk for the restore | 10 min (IaC template, pre-built image) | pre-provisioned scratch instance, IaC |
| `t_restore` | copy and decompress the base backup | 300 GB / 200 MB/s ≈ 25 min | parallel restore (`process-max`), fast target disk, smaller `D` |
| `t_replay` | replay WAL from the base backup's end to the target | 13 GB / 40 MB/s ≈ 6 min (+ fetch) | more frequent incremental/differential backups (smaller `t_base`) |
| `t_verify` | check the restored data is right | 15 min | pre-written validation queries (§8.3) |
| `t_cutover` | merge data back or switch traffic | 20 min (merge + re-issue job) | rehearsed merge scripts; for full restores, DNS/connection-string switch |
| `t_warm` | caches, buffer pool, lazily loaded disks catch up | ~0 (side instance, prod stays hot) | pre-warming, `pg_prewarm`; for cloud disks restored from snapshots, initialization |
| **RTA from detection** | | **≈ 95 min** | |

**The target disk is often the bottleneck.** The same arithmetic for a 2 TB database:

```
Target: AWS gp3 volume at its baseline 125 MB/s (throughput is provisioned separately)
  t_restore = 2,000,000 MB / 125 MB/s = 16,000 s ≈ 4.4 h

Same restore to gp3 provisioned at 1,000 MB/s, 16 parallel restore processes,
network and S3 keeping up:
  t_restore = 2,000,000 MB / 1,000 MB/s = 2,000 s ≈ 33 min
```

**Backup frequency controls RTO through replay, not only RPO.** With WAL at 5 GB/h and replay at
50 MB/s (both illustrative; measure yours):

```
Weekly full only, incident 6.5 days after it:
  WAL to replay = 5 GB/h × 156 h = 780 GB → 780,000 / 50 ≈ 15,600 s ≈ 4.3 h
Daily incremental, incident 13 h after the last one:
  WAL to replay = 5 GB/h × 13 h = 65 GB  → 65,000 / 50  ≈ 1,300 s  ≈ 22 min
```

PostgreSQL replays WAL in a single startup process, so `R_replay` does not scale with cores; it
depends on the workload (random reads for pages not in cache, full-page images after checkpoints).
PostgreSQL 15 added `recovery_prefetch`, which reads ahead referenced blocks and helps on
I/O-bound replay. Measure `R_replay` in drills: it is the least predictable term.

### 2.4 Cost versus RPO and RTO

```
  cost / month
    ▲
    │█                                             active-active, RPO≈0, RTO≈0
    │ █                                            (§3, chapter 36)
    │  █
    │   ██                                          warm standby: RPO s, RTO min
    │     ███
    │        ████                                   pilot light: RPO min, RTO 10s of min
    │            ██████
    │                  ██████████                   backup + PITR: RPO min, RTO hours
    │                            ████████████████   nightly dump: RPO 24h, RTO hours–day
    └──────────────────────────────────────────────►  tolerated RPO / RTO
     0          minutes          hours          days
```

The curve is steep at the left: going from RTO 4 h to 15 min usually means paying for a second
running environment; going from 15 min to near zero means active-active with all its correctness
costs (chapter 36). Going from RPO 24 h to 5 min, by contrast, is nearly free: WAL archiving costs
storage, not servers. **Buy RPO cheaply first; buy RTO only as far as the business needs.**

---

## 3. DR Tiers, the Cost Curve, and Mapping Criticality to a Tier

> **In plain words.** There are four standard levels of DR readiness, from "we have backups and a
> plan to rebuild" to "we already run in two places". Each step up cuts downtime and multiplies
> cost. Pick per system, not per company.

### 3.1 The four tiers

The names follow the AWS disaster recovery whitepaper; other clouds use similar ones.

| Tier | What runs in the recovery location before a disaster | Typical RPO | Typical RTO | Relative cost | Detail |
|---|---|---|---|---|---|
| **Backup and restore** | nothing; backups (ideally PITR) and IaC to rebuild | minutes (with PITR) to 24 h | hours to a day | 1× (storage only) | this chapter |
| **Pilot light** | data replicated continuously (DB replica or backups copied); compute defined but off or minimal | seconds to minutes | tens of minutes to hours | ~1.1–1.2× | this chapter §9.3; chapter 36 §1.3 |
| **Warm standby** | a scaled-down but working copy of the whole stack, receiving replicated data | seconds | minutes | ~1.3–1.6× | [`36-multi-region-active-active-and-geo-replication.md`](36-multi-region-active-active-and-geo-replication.md) §1.3, §7 |
| **Multi-site active-active** | full capacity in two or more regions, all serving traffic | ≈ 0 to seconds | ≈ 0 to minutes | 2×+ plus engineering | chapter 36 |

The single most important fact about this table: **tiers 2–4 protect only against infrastructure
disasters.** A warm standby receives the bad migration in milliseconds, exactly like a same-AZ
replica. Every tier still needs tier 1's backups and PITR underneath it for logical disasters.

### 3.2 Map criticality to a tier

Classify each system (not the whole company) by what an hour of downtime and an hour of lost data
cost.

| Class | Examples | Cost of downtime | Suggested RPO / RTO | Tier |
|---|---|---|---|---|
| **Tier 0 — money and identity** | primary OLTP database, payments ledger, auth | revenue stops; contractual penalties | RPO ≤ 5 min / RTO ≤ 4 h (SMB) or ≤ 15 min (scale) | backup + PITR always; warm standby when justified (below) |
| **Tier 1 — core product** | main app services, object storage with user uploads | product unusable | RPO ≤ 15 min / RTO ≤ 8 h | backup + PITR; pilot light when growing |
| **Tier 2 — internal** | admin tools, BI, CI | staff productivity | RPO 24 h / RTO 1–3 days | backup and restore |
| **Tier 3 — derived** | caches, search indexes, analytics copies, ML feature snapshots | degraded, not down | rebuild from source; no backup, or a snapshot if rebuild takes too long | rebuild |

**When is a warm standby worth it?** Compare expected annual loss with the standby's cost:

```
Expected annual loss avoided ≈ P(region-level outage per year) × (RTO_backup − RTO_standby) × cost/hour

Illustrative: P = 0.3/yr, backup-and-restore RTO 6 h, standby RTO 0.5 h, cost $4,000/h
  → 0.3 × 5.5 h × $4,000 ≈ $6,600 / year avoided
Warm standby for a 300 GB Postgres + app tier: often $1,500–4,000 / month = $18,000–48,000 / year
```

For most SMBs the numbers say: do tier 1 excellently (PITR, cross-account immutable copies,
tested restores, IaC) and accept a multi-hour RTO for the rare full-region event. Revisit when
contracts demand a lower RTO or when cost per hour of downtime grows by an order of magnitude.

### 3.3 SMB-realistic defaults

1. Managed PostgreSQL with automated backups and PITR (7–35 days), deletion protection on.
2. A second, independent copy outside the production account (different account, ideally
   different region, with Object Lock), produced by a tool you control (§7).
3. Everything else rebuildable from code: IaC (Terraform/OpenTofu), GitOps manifests, a documented
   secrets inventory.
4. An automated weekly (then nightly) restore test with a timing metric.
5. A written runbook per disaster in §9, rehearsed once a quarter.

This covers every row of the §1.2 matrix except "RTO under an hour after a full region loss", for
a few hundred dollars a month (§12).

---

## 4. Backup Types — Full, Incremental, Logical, Physical, Snapshots, PITR

> **In plain words.** You can copy the database's *files* (physical) or export its *rows*
> (logical). You can copy everything each time (full) or only what changed (incremental). You can
> ask the storage layer for an instant snapshot. And you can keep the change log (WAL) so you can
> stop replay at any second you like. Real setups combine several.

### 4.1 Full, incremental, differential

```
 Sun          Mon          Tue          Wed          (restore Wednesday)
 FULL ──────► INCR(Sun→Mon) INCR(Mon→Tue) INCR(Tue→Wed)
 restore needs: FULL + Mon + Tue + Wed        (chain: every link must be intact)

 FULL ──────► DIFF(Sun→Mon) DIFF(Sun→Tue) DIFF(Sun→Wed)
 restore needs: FULL + Wed                    (bigger backups, shorter chain)
```

| Type | Backup size and time | Restore needs | Risk |
|---|---|---|---|
| Full | largest, slowest | one set | none from chains |
| Differential | grows during the week | full + latest differential | medium |
| Incremental | smallest, fastest | full + every incremental since | a single broken link breaks every later restore |
| Block-level incremental (pgBackRest block incremental, PG17 `pg_basebackup --incremental`) | only changed blocks | full + chain; the tool reassembles | same chain risk; tool must verify |

A common schedule: weekly full, daily differential or incremental, continuous WAL. The chain
length directly bounds `t_replay` (§2.3).

### 4.2 Physical versus logical

| | Physical (`pg_basebackup`, pgBackRest, WAL-G, storage snapshots) | Logical (`pg_dump`, `pg_dumpall`) |
|---|---|---|
| What is copied | data files, byte for byte | SQL / rows, schema definitions |
| Point-in-time recovery | yes, combined with WAL | no: only the moment the dump's snapshot started |
| Restore speed | fast: copy files, replay WAL | slow for large DBs: reload every row, rebuild every index, re-validate constraints |
| Granularity of restore | whole cluster (all databases) | per table, per schema, per database |
| Portable across major versions / architectures | no (same major version, same platform) | yes; the standard upgrade and migration path |
| Detects silent corruption | no (copies bad pages faithfully, unless checksums are verified during backup) | partly: reading every row fails loudly on some corruption |
| Consistency | consistent after WAL replay to the backup's end point | consistent snapshot (a single repeatable-read transaction) |
| Includes roles and tablespaces | yes | `pg_dump` no; add `pg_dumpall --globals-only` |
| Good for | primary DR, PITR, large DBs | second, independent copy; small DBs; table-level restore; version-independent archive |

A logical dump is a valuable **independent** copy: produced by a different tool, readable by any
future PostgreSQL version, and immune to bugs in the physical backup chain. For databases under
~100 GB, a nightly `pg_dump -Fc` shipped to another account or provider is the cheapest second
line of defense there is.

```bash
# Logical: custom format (compressed, supports selective and parallel restore)
pg_dump -h db.internal -U backup -d app -Fc -Z 6 -f app-$(date +%F).dump
pg_dumpall -h db.internal -U postgres --globals-only > globals-$(date +%F).sql

# Parallel dump needs directory format
pg_dump -d app -Fd -j 8 -f app-$(date +%F).dir

# Restore one table from a custom-format dump into a scratch DB
pg_restore -d scratch --table=customers --data-only app-2026-03-09.dump

# Physical: base backup with WAL streamed alongside, fast checkpoint
pg_basebackup -h db.internal -U replicator -D /backups/base-$(date +%F) \
  -Fp -X stream -c fast -P
pg_verifybackup /backups/base-2026-03-09      # checks files and WAL against backup_manifest;
                                              # plain format in PG13-17, tar format from PG18
```

### 4.3 Storage snapshots: crash-consistent versus application-consistent

Cloud volume snapshots (EBS, Persistent Disk, Azure managed disks) and filesystem snapshots (LVM,
ZFS) capture the block device at one instant.

- **Crash-consistent**: the snapshot looks like the disk after a sudden power loss. PostgreSQL is
  designed to recover from exactly that state by replaying WAL, **provided the data directory and
  `pg_wal` are captured at the same instant**. If they are on separate volumes, you need a
  multi-volume crash-consistent snapshot (on AWS, `ec2 create-snapshots` for an instance's
  volumes) or the application-consistent method below.
- **Application-consistent**: the application is told a backup is happening. For PostgreSQL:
  `SELECT pg_backup_start('snap', fast => true);` (named `pg_start_backup` before v15), take the
  snapshot, then `SELECT * FROM pg_backup_stop();` and **save the returned `backup_label`
  contents with the snapshot**; without them recovery may start from the wrong checkpoint. The
  session that called `pg_backup_start` must stay open until `pg_backup_stop`.
  Filesystem freeze (`fsfreeze -f`) on its own gives write-quiescence, not a database-level
  guarantee.

Snapshot trade-offs:

- Taking one is near-instant and incremental at the block level; **restoring** one on AWS creates
  a volume whose blocks are fetched lazily from S3 on first read, so the first full scan of a
  freshly restored volume is slow unless you pay for Fast Snapshot Restore or pre-read the volume.
- Snapshots live in the same account and region by default, so they fail the account-loss,
  region-loss and attacker rows of §1.2 until copied elsewhere.
- Snapshot granularity is the snapshot interval. Combine with WAL archiving to get PITR.

### 4.4 Continuous WAL archiving and PITR

```
  base backup (Sun 01:00)                       recovery target: Tue 14:04:59
        │                                                  │
        ▼                                                  ▼
  ┌───────────┐  WAL 0001 ─ 0002 ─ 0003 ─ ... ─ 00A7 ─ 00A8 │ 00A9 (contains the DROP COLUMN)
  │ data files│  ─────────── replayed in order ───────────────►│ stop
  └───────────┘                                            (recovery_target_time or _xid)

  RPO ≈ archive lag (seconds);  restore to ANY moment in [end of oldest retained base, now − L_archive]
```

PITR = a physical base backup + every WAL segment since it. Recovery copies the base backup into
place, fetches WAL segments via `restore_command`, replays them, and stops at the target:

| Target parameter | Stops at | Use when |
|---|---|---|
| `recovery_target_time` | the first commit after the given timestamp (with `recovery_target_inclusive = on`, commits at exactly that time are included) | you know roughly when |
| `recovery_target_xid` | a specific transaction ID; `recovery_target_inclusive = off` stops just *before* it commits | you found the bad transaction (§9.1) |
| `recovery_target_lsn` | a WAL position | you found the LSN with `pg_waldump` |
| `recovery_target_name` | a named restore point created with `pg_create_restore_point('before_mig_0142')` | before risky migrations: create one first |
| `recovery_target = 'immediate'` | as soon as the base backup is consistent | fastest restore to the backup's own time |

`recovery_target_action` decides what happens at the target: `pause` (the default; with
`hot_standby = on` you can connect read-only, inspect, then call `pg_wal_replay_resume()` to end
recovery and open for writes, or shut down, move the target, and restart), `promote` (open for writes on a new timeline), or
`shutdown`. When restoring to investigate, **use `pause`**: you can look before you commit to
the point.

Every promotion after PITR creates a new **timeline** (`00000002.history`); WAL from the old and
the new history can coexist in the archive. `recovery_target_timeline = 'latest'` (the default
since v12) follows the newest timeline; to restore along an older history, name it.

---

## 5. PostgreSQL Backup and PITR in Practice

> **In plain words.** Don't write your own `archive_command` with `cp` and `aws s3 cp` in
> production. Use a tool that compresses, encrypts, checksums, runs in parallel, manages retention
> and knows how to restore: pgBackRest or WAL-G for self-managed Postgres, the provider's PITR for
> managed Postgres, plus an independent copy the provider cannot lose.

### 5.1 Why not a hand-rolled `archive_command`

The documentation's example `test ! -f /archive/%f && cp %p /archive/%f` teaches the contract, and
the contract has sharp edges:

- The command must return 0 **only after** the segment is durably stored; returning 0 early loses
  WAL silently.
- It must never overwrite an existing segment with different content (the `test ! -f` part), and
  must succeed idempotently if the same segment is retried after a crash.
- If it keeps failing, Postgres keeps every unarchived segment in `pg_wal` **until the disk is
  full and the primary stops**. A broken archive is both an RPO problem and an availability
  problem.
- It runs serially per segment; at high WAL rates a slow upload falls behind. Tools add async,
  parallel push with a local spool.

### 5.2 pgBackRest

A configuration sketch for the running example, with a primary repository in the production
account and a second repository in a separate backup account and region (§7.3):

```ini
# /etc/pgbackrest/pgbackrest.conf
[global]
# repo1: same account, same region; fast restores, 14 days of PITR
repo1-type=s3
repo1-s3-bucket=acme-prod-pgbackrest
repo1-s3-region=eu-central-1
repo1-s3-endpoint=s3.eu-central-1.amazonaws.com
repo1-s3-key-type=auto                # use the instance's IAM role
repo1-path=/main
repo1-retention-full-type=time
repo1-retention-full=14               # days; keeps what is needed to PITR across the window
repo1-cipher-type=aes-256-cbc
repo1-cipher-pass=<from secret store; escrowed outside this account>
repo1-bundle=y
repo1-block=y                         # block-level incremental

# repo2: backup account, other region, versioned bucket with Object Lock (§7)
repo2-type=s3
repo2-s3-bucket=acme-vault-pgbackrest
repo2-s3-region=eu-west-1
repo2-s3-endpoint=s3.eu-west-1.amazonaws.com
repo2-s3-key-type=auto
repo2-path=/main
repo2-retention-full-type=time
repo2-retention-full=35
repo2-cipher-type=aes-256-cbc
repo2-cipher-pass=<different passphrase, escrowed outside the production account>

compress-type=zst
process-max=4
start-fast=y
archive-async=y                       # push WAL in parallel from a local spool
spool-path=/var/spool/pgbackrest
log-level-console=info

[main]
pg1-path=/var/lib/postgresql/16/main
```

```ini
# postgresql.conf
wal_level = replica
archive_mode = on
archive_command = 'pgbackrest --stanza=main archive-push %p'
archive_timeout = 60           # force a segment switch at least every minute on a quiet DB
```

```bash
# One-time setup and a sanity check (verifies archive_command end to end)
sudo -u postgres pgbackrest --stanza=main stanza-create
sudo -u postgres pgbackrest --stanza=main check

# Schedule (cron, as postgres): weekly full, daily incremental, to each repository
0 1 * * 0    pgbackrest --stanza=main --repo=1 --type=full backup
0 1 * * 1-6  pgbackrest --stanza=main --repo=1 --type=incr backup
30 2 * * 0   pgbackrest --stanza=main --repo=2 --type=full backup
30 2 * * 1-6 pgbackrest --stanza=main --repo=2 --type=diff backup

# Inventory: backup sets, sizes, WAL range available for PITR, per repo
pgbackrest --stanza=main info

# Periodic integrity check of repository contents
pgbackrest --stanza=main --repo=2 verify
```

WAL is pushed to every configured repository by `archive-push`; backups are taken per repository
as scheduled. Restores:

```bash
# PITR to a side host (empty data dir), stop before the target time and pause for inspection.
# --archive-mode=off keeps the restored instance from archiving a new timeline into prod's repo.
pgbackrest --stanza=main --repo=1 \
  --type=time "--target=2026-03-10 14:04:59+00" --target-action=pause \
  --archive-mode=off --process-max=8 restore
pg_ctlcluster 16 main start          # replays WAL, then pauses at the target

# Restore to just before a specific transaction (found with pg_waldump, §9.1)
pgbackrest --stanza=main --type=xid --target=48213377 --target-exclusive \
  --target-action=pause --archive-mode=off restore

# Full in-place restore of a damaged primary, reusing unchanged files (much faster)
pgbackrest --stanza=main --delta --type=time "--target=2026-03-10 14:04:59+00" \
  --target-action=promote restore

# Restore from the vault if the production account is unavailable
pgbackrest --stanza=main --repo=2 --type=time "--target=..." restore
```

pgBackRest picks the newest backup set that ends before a time target in current versions; pin a
specific set with `--set=20260308-010002F` when you need to. The restored host needs the
repository configuration (and the cipher passphrase) available **without** the production account:
keep a copy of `pgbackrest.conf` and the passphrases in the break-glass store (§7.5).

### 5.3 WAL-G

WAL-G is a single binary configured by environment variables; it is common in Kubernetes
operators and for teams that prefer delta backups and many storage backends.

```bash
# /etc/wal-g.d/  (envdir: one file per variable, shown here as NAME=value)
WALG_S3_PREFIX=s3://acme-prod-walg/main
AWS_REGION=eu-central-1
WALG_COMPRESSION_METHOD=zstd
WALG_DELTA_MAX_STEPS=6               # up to 6 delta backups before the next full
WALG_LIBSODIUM_KEY_PATH=/etc/wal-g/libsodium.key   # client-side encryption
PGHOST=/var/run/postgresql
```

```ini
# postgresql.conf
archive_command = 'envdir /etc/wal-g.d wal-g wal-push %p'
```

```bash
envdir /etc/wal-g.d wal-g backup-push /var/lib/postgresql/16/main   # base (full or delta)
envdir /etc/wal-g.d wal-g backup-list --detail
envdir /etc/wal-g.d wal-g wal-verify integrity timeline              # gaps in the WAL chain?
envdir /etc/wal-g.d wal-g delete retain FULL 4 --confirm             # keep 4 full chains

# Restore: fetch the base, then let Postgres pull WAL on demand
envdir /etc/wal-g.d wal-g backup-fetch /var/lib/postgresql/16/main LATEST
cat >> /var/lib/postgresql/16/main/postgresql.auto.conf <<'EOF'
restore_command = 'envdir /etc/wal-g.d wal-g wal-fetch %f %p'
recovery_target_time = '2026-03-10 14:04:59+00'
recovery_target_action = 'pause'
EOF
touch /var/lib/postgresql/16/main/recovery.signal
```

| | pgBackRest | WAL-G |
|---|---|---|
| Configuration | INI file, stanzas, multiple repos natively | environment variables, one storage per config |
| Backup types | full, differential, incremental, block incremental | full, delta (page-level) |
| Restore of a delta/incremental chain | automatic | automatic |
| Integrity | checksums per file, `verify` command, page checksum validation during backup | `wal-verify`, `backup-mark`, checksums |
| Parallelism | `process-max` for backup, restore, async archive | `WALG_UPLOAD_CONCURRENCY`, `WALG_DOWNLOAD_CONCURRENCY` |
| Typical home | VMs and bare metal; also operators | Kubernetes operators, multi-database (also MySQL, MongoDB, Redis) |

Either is a good choice. Pick one, learn its restore path thoroughly, and do not run both against
the same cluster (two archive commands means two sources of truth about what is archived).

### 5.4 Managed PostgreSQL

| | Amazon RDS for PostgreSQL | Aurora PostgreSQL | Google Cloud SQL |
|---|---|---|---|
| Automated backups | daily snapshot + transaction logs; retention 1–35 days (0 disables them) | continuous; retention 1–35 days | daily backups (retention configurable) + PITR from write-ahead logs; retention limits depend on edition |
| PITR granularity | any second up to `LatestRestorableTime`, typically within the last 5 minutes | similar | any second in the log retention window |
| PITR output | **a new instance**; production is untouched | **a new cluster** (add an instance to it) | a new instance (clone) |
| Survives deletion of the instance | automated backups are deleted with the instance unless you choose to retain them; manual snapshots persist | similar; final snapshot optional | check the current "retain backups after deletion" behavior; historically backups went with the instance |
| Cross-region | cross-Region automated backup replication; snapshot copy | Aurora Global Database (a replica, not a backup); snapshot copy | cross-region backup location configurable |
| Cross-account | share or copy snapshots; AWS Backup copy to another account with a vault lock | same | export or copy to another project |

```bash
# RDS: PITR to a new instance next to production
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier invoicing-prod \
  --target-db-instance-identifier invoicing-pitr-0310 \
  --restore-time 2026-03-10T14:04:59Z \
  --db-subnet-group-name prod-db \
  --vpc-security-group-ids sg-0abc123 \
  --db-parameter-group-name invoicing-pg16 \
  --no-multi-az

aws rds describe-db-instances --db-instance-identifier invoicing-prod \
  --query 'DBInstances[0].LatestRestorableTime'

# Aurora: restore creates a cluster with no instances; add one
aws rds restore-db-cluster-to-point-in-time \
  --source-db-cluster-identifier invoicing-aurora \
  --db-cluster-identifier invoicing-aurora-pitr \
  --restore-to-time 2026-03-10T14:04:59Z
aws rds create-db-instance --db-cluster-identifier invoicing-aurora-pitr \
  --db-instance-identifier invoicing-aurora-pitr-1 \
  --db-instance-class db.r6g.large --engine aurora-postgresql

# Cloud SQL: PITR by cloning to a new instance
gcloud sql instances clone invoicing-prod invoicing-pitr-0310 \
  --point-in-time '2026-03-10T14:04:59Z'
```

Managed-service specifics that bite:

- **A PITR restore is a new endpoint.** If you choose a full rollback, the application must be
  pointed at it (rename the instances, or change the connection string), and parameter groups,
  security groups, IAM auth, extensions and monitoring must match. Pass them explicitly; defaults
  are not your production settings.
- **Restored RDS/EBS-backed instances load data lazily**: the first read of each block comes from
  S3, so the first hours after a restore can be much slower than production. Warm the hot tables
  (`pg_prewarm`, sequential scans) before cutting traffic over.
- **Managed backups share fate with the account.** An attacker (or a mistaken automation) with
  admin rights on the account can delete instances and their automated backups. Keep an
  independent copy under different credentials (§7).
- **Aurora MySQL's Backtrack** rewinds a cluster in place for up to 72 hours; Aurora PostgreSQL
  does not have it. For PostgreSQL, PITR to a new cluster is the tool.

### 5.5 Delayed replicas: the cheapest fast "undo"

```ini
# on a dedicated standby
recovery_min_apply_delay = '4h'
```

The standby receives WAL immediately (so it also helps RPO for infrastructure loss) but applies it
four hours late. When a logical disaster is noticed within the delay:

1. `SELECT pg_wal_replay_pause();` on the delayed standby, immediately.
2. Its data is still from before the incident (at 16:30 it shows the state of about 12:30), and it
   is readable as a hot standby. Extract the lost data read-only, exactly as in §9.1.
3. For a precise target, copy or snapshot it and restart the copy with a recovery target instead of
   `standby.signal`.

Costs and caveats: one extra instance; useless when `t_detect > delay`; do not use it for failover
(it is hours behind); long delays keep more WAL on the standby. It does not replace PITR, but it
turns a 90-minute restore into a 5-minute query for the most common case (a mistake noticed the
same afternoon).

### 5.6 MySQL in one paragraph

MySQL PITR = a full backup (Percona XtraBackup or MySQL Enterprise Backup for physical;
`mysqldump --single-transaction --source-data=2` for logical, which records the binlog position)
plus the binary logs since it. Keep `binlog_format=ROW`, set `binlog_expire_logs_seconds` longer
than the backup interval, and ship binlogs off the host (for example `mysqlbinlog
--read-from-remote-server --raw --stop-never`). To recover: restore the full backup, then replay
binlogs up to just before the bad event: `mysqlbinlog --start-position=<from backup>
--stop-datetime="2026-03-10 14:04:59" binlog.000812 binlog.000813 | mysql` (or find the exact
event with `mysqlbinlog -v` and use `--stop-position`; with GTIDs, `--exclude-gtids` skips the bad
transaction). MariaDB's `mysqlbinlog --flashback` can generate inverse row events for a narrow undo.

---

## 6. Backing Up Everything Else

> **In plain words.** The database is rarely the only state. Uploaded files, event streams, caches
> that turned into stores, search indexes, encryption keys, DNS records and infrastructure
> definitions all have to come back too, and some of them (keys) make all the other backups
> useless if lost.

For each piece of state, ask two questions: **is it a source of truth or derived?** and **if
derived, how long does a rebuild take compared with the RTO?**

| State | Source of truth? | Protection | Restore path | Notes |
|---|---|---|---|---|
| OLTP database | yes | PITR + independent copy | §5, §9 | |
| Object storage (uploads, documents) | yes | versioning + Object Lock + cross-account replication | restore versions; point readers at replica bucket | §6.1 |
| Kafka topics | usually no (derived from DBs or producers); sometimes yes (event-sourced) | replay from source; sink to object storage; MirrorMaker 2 for site loss | re-produce or rehydrate from archive | §6.2 |
| Redis | depends: cache (no), sessions/queues/rate limits (maybe), primary store (yes) | RDB/AOF + off-host copies for stores | load RDB/AOF; or warm cache | §6.3 |
| Search index | no | rebuild from DB; snapshot if rebuild is slow | restore snapshot, then catch up from change stream | §6.4 |
| Encryption keys (KMS, app keys, backup passphrases) | yes, and they gate everything else | multi-region keys, deletion windows, escrow | §6.5 | |
| Secrets (DB passwords, API keys) | yes | secret manager replication; inventory; re-issuable | re-issue where possible | |
| IaC, config, GitOps manifests | yes (in git) | git hosting + mirror | re-apply | §6.6 |
| DNS zones, registrar, TLS certificates | yes, often outside your cloud | zone export, registrar lock, second provider | re-import zone | §6.6 |
| Kubernetes objects and PVs | mixed | GitOps + Velero + etcd snapshots | see kubernetes chapters | §6.7 |

### 6.1 Object storage (S3 and compatible)

S3 is designed for very high durability, which protects against hardware loss, not against
`aws s3 rm --recursive` or an overwrite by buggy code. The protections, cheapest first:

1. **Versioning.** Overwrites and deletes keep the previous version (a delete adds a *delete
   marker*). Recover by copying an old version back or removing the delete marker. Costs storage
   for noncurrent versions; bound it with a lifecycle rule.
2. **Lifecycle for noncurrent versions.** For example, keep noncurrent versions 30 days, then
   expire; move old data to cheaper classes. Mind minimum storage durations (90 days for Glacier
   Flexible Retrieval, 180 days for Glacier Deep Archive) and retrieval times (hours for Deep
   Archive) when planning RTO.
3. **Object Lock.** WORM retention per object version. **Governance mode** can be bypassed by
   principals with a special permission; **compliance mode** cannot be shortened or removed by
   anyone, including the account root user, until the retain-until date. A default retention on
   the bucket applies to every new version. A plain `DELETE` still succeeds by adding a delete
   marker, but the locked versions underneath cannot be removed.
4. **Replication to another account and region.** S3 Replication copies new versions
   asynchronously (Replication Time Control offers a 15-minute SLA). Delete markers are **not**
   replicated unless you enable it, and deletions of specific versions are never replicated, which
   is what you want for protection. Use "owner override" / bucket-owner-enforced ownership so the
   backup account owns the replicas.
5. **MFA Delete** (root-only to configure) and SCPs denying `s3:PutBucketVersioning`,
   `s3:PutLifecycleConfiguration` and `s3:DeleteBucket*` for everyone except a break-glass role.

```json
{
  "Rules": [{
    "ID": "noncurrent-30d",
    "Status": "Enabled",
    "Filter": {},
    "NoncurrentVersionExpiration": { "NoncurrentDays": 30 },
    "AbortIncompleteMultipartUpload": { "DaysAfterInitiation": 7 }
  }]
}
```

```bash
aws s3api put-object-lock-configuration --bucket acme-vault-uploads \
  --object-lock-configuration \
  '{"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"COMPLIANCE","Days":35}}}'
```

Test compliance mode on a throwaway bucket first: a 10-year default retention set by mistake is
a 10-year storage bill nobody can cancel.

### 6.2 Kafka: replication is not backup here either

Replication factor 3 with `min.insync.replicas=2` survives broker and AZ loss. It does nothing
against a producer writing garbage, a mistaken `kafka-topics --delete`, a retention change that
deletes a week of data, or a compacted topic losing history by design. Options, by preference:

1. **Treat Kafka as derived and replay from the source of truth.** If topics are fed by the
   outbox or CDC from Postgres (Debezium), recovery = restore the DB, then re-snapshot the
   connector. Consumers must be idempotent
   ([`06-distributed-transactions-sagas-outbox-idempotency.md`](06-distributed-transactions-sagas-outbox-idempotency.md)).
2. **Archive topics to object storage** with a sink connector (for example an S3 sink writing
   Parquet/Avro per topic-partition-offset range) into a versioned, locked bucket. Replay by
   re-producing from the archive. This is the backup for topics that *are* the source of truth
   (event sourcing, audit streams).
3. **MirrorMaker 2** to a second cluster covers site or region loss and translates consumer
   offsets, but mirrors bad data too; it is a replica, not a backup.
4. **Tiered storage** (KIP-405) makes long retention cheap by offloading segments to object
   storage, but the cluster still controls deletion; it is long retention, not an independent copy.

Details of Kafka replication and delivery semantics: [`07-kafka-and-event-streaming.md`](07-kafka-and-event-streaming.md).

### 6.3 Redis: cache or store?

Decide which one it is and write it down.

- **Cache**: no backup. The DR concern is the cold start: after a restore, every request misses
  and hits the database. Plan warm-up and request coalescing
  ([`08-caching-strategies-and-patterns.md`](08-caching-strategies-and-patterns.md)).
- **Store** (sessions you cannot drop, job queues, rate-limit state, leaderboards, primary data):
  enable persistence and copy it off the host.
  - **RDB**: point-in-time snapshots (`BGSAVE`, `save` rules). The file is written to a temp file
    and renamed, so copying the latest `dump.rdb` is safe. RPO = snapshot interval.
  - **AOF**: logs every write; `appendfsync everysec` loses at most about a second on a crash.
    Redis 7 uses a multi-part AOF (base + incremental files plus a manifest); back up the whole
    directory. AOF protects against crashes, not against `FLUSHALL`, which is also logged; after
    an accidental `FLUSHALL`, stop Redis before an AOF rewrite and remove the command from the
    AOF tail, or restore the last RDB.
  - Managed Redis/Valkey offerings provide scheduled snapshots; export them to storage you control.

### 6.4 Search indexes: rebuild or snapshot

A search index (Elasticsearch/OpenSearch) is almost always derived from the database. The question
is only speed: if a full reindex of 400 million documents takes 14 hours and the RTO is 4 hours,
you need snapshots.

```bash
# Register an S3 snapshot repository and take a snapshot (incremental at the segment level)
curl -XPUT localhost:9200/_snapshot/s3_repo -H 'Content-Type: application/json' -d '
{ "type": "s3", "settings": { "bucket": "acme-search-snapshots", "base_path": "prod" } }'
curl -XPUT 'localhost:9200/_snapshot/s3_repo/nightly-2026.03.10?wait_for_completion=false'

# Restore, then catch up from the change stream (CDC/outbox) from the snapshot's timestamp
curl -XPOST 'localhost:9200/_snapshot/s3_repo/nightly-2026.03.10/_restore' \
  -H 'Content-Type: application/json' -d '{ "indices": "invoices-v7" }'
```

Automate snapshots with snapshot lifecycle management (SLM in Elasticsearch, ISM/snapshot
management in OpenSearch). The catch-up step needs a replayable change feed with a position you
can map to the snapshot time; a reindex job from the database with an `updated_at >=` filter works
if every write updates `updated_at` (including deletes, as tombstones).

### 6.5 Encryption keys: deleting the key deletes the data

With envelope encryption, data is encrypted with data keys, and data keys are encrypted with a
key-encryption key in a KMS. Lose the KMS key and every data key, every encrypted volume, every
encrypted snapshot and every encrypted backup copy becomes unreadable. That is the basis of
crypto-shredding (§10.2) and also the most efficient way to destroy a company by accident or by
malice.

- **AWS KMS key deletion** requires a waiting period of 7–30 days (default 30), during which it can
  be cancelled. Alert on `ScheduleKeyDeletion` and `DisableKey` events in CloudTrail; deny
  `kms:ScheduleKeyDeletion` by SCP to everyone except a break-glass role.
- **KMS keys are regional and non-exportable** (unless you imported the key material). A snapshot
  copied to another region or account must be re-encrypted with a key usable there; a backup
  encrypted with a key in the lost region or account is unreadable. Use multi-Region keys or
  re-encrypt on copy with a key owned by the backup account.
- **Backup-tool passphrases** (pgBackRest `cipher-pass`, WAL-G libsodium key) are the only way to
  read those backups. Store them in the break-glass location of §7.5, outside the production
  account, and test that a drill can read them.
- **Don't encrypt the backups with a key the attacker can delete.** The vault's copies should use a
  key owned by the backup account.

### 6.6 IaC, configuration, DNS as recoverable state

- **Infrastructure as code** is the restore procedure for everything stateless. If the production
  account disappears, `terraform apply` into a new account should rebuild networks, IAM, clusters
  and managed services, and the IaC state file itself (for example in S3 with versioning) must be
  recoverable too, or rebuildable by import.
- **Git hosting** is a dependency: keep a mirror of critical repositories (including IaC and
  runbooks) somewhere else. Runbooks stored only in the wiki that runs in the lost account are not
  available during the disaster.
- **DNS**: export zones regularly (`aws route53 list-resource-record-sets`, or zone files), enable
  registrar lock, and know how to move NS records. Keep DNS for the recovery path outside the
  failure domain you are recovering from.
- **Certificates**: automated issuance (ACME) is the backup; know which names need pre-issued
  certificates in the recovery environment.

### 6.7 Kubernetes

Cluster state is a mix of desired state (in git, via GitOps), API objects (in etcd) and volume
data. Use GitOps to recreate objects, etcd snapshots for full control-plane loss, and Velero (with
CSI snapshots or file-level copies) for namespaces and volumes; databases inside Kubernetes still
need the database-native PITR of §5, because a volume snapshot of a running database is at best
crash-consistent. Details:
[`../kubernetes/32-cluster-lifecycle-and-day2.md`](../kubernetes/32-cluster-lifecycle-and-day2.md)
§19 (etcd snapshot versus Velero), §20 (Velero architecture) and §24 (DR scenarios);
[`../kubernetes/19-storage-csi-pv-pvc.md`](../kubernetes/19-storage-csi-pv-pvc.md) §20 (Velero and
CSI snapshots); [`../kubernetes/13-statefulset-deep-dive.md`](../kubernetes/13-statefulset-deep-dive.md)
§24 (losing pod-0 and its volume); [`../kubernetes/04-etcd-internals.md`](../kubernetes/04-etcd-internals.md)
for etcd snapshot mechanics.

---
## 7. Backups That Survive an Attacker — 3-2-1-1-0, Immutability, Ransomware

> **In plain words.** A backup that production credentials can delete is protection against
> accidents, not against attackers. Keep at least one copy in a different account, under different
> credentials, that nobody can delete for a fixed period, and prove you can restore from it.
>
> **Real-world example.** The invoicing SaaS's pgBackRest `repo1` is in the production AWS
> account. Anyone holding the production admin role can delete the bucket in one command. `repo2`
> lives in a separate backup account with S3 Object Lock in compliance mode for 35 days; the
> production role can write new objects there but cannot delete versions, change the lock or read
> the passphrase.

### 7.1 The 3-2-1-1-0 rule

| Digit | Rule | For the invoicing SaaS |
|---|---|---|
| 3 | at least three copies of the data (production counts as one) | primary DB, `repo1`, `repo2` (plus the weekly `pg_dump`) |
| 2 | on two different media or storage systems | block storage (DB), object storage (repos) |
| 1 | one copy offsite | `repo2` in another region |
| 1 | one copy offline, air-gapped or immutable | `repo2` with Object Lock compliance mode in a separate account |
| 0 | zero errors in verified restores | nightly automated restore test (§8) |

The original 3-2-1 rule predates cloud; the "1-0" additions (popularized by backup vendors) are the
parts that matter most today. In cloud terms, "offsite" means **another region and another
account**, and "offline" usually means **immutable under a separate administrative domain**
rather than a tape in a truck, though a periodic copy to a different provider gives similar
independence cheaply.

### 7.2 The ransomware threat model

Assume the attacker:

1. Gets credentials with broad rights (phished admin, leaked CI token, compromised laptop with
   cloud CLI sessions, over-privileged service account).
2. Spends days or weeks inside (dwell time): maps the environment, **finds the backups**, disables
   logging and alerting where possible.
3. Deletes or encrypts backups and snapshots first, then encrypts or deletes production, then
   demands payment. Data theft for double extortion is common, so restoring does not end the
   incident.

Consequences for the design:

- Anything the compromised identity can delete, it will. Separate the **write path** (production
  can add backups) from the **delete path** (only time, via retention, can remove them).
- The restore point must predate the intrusion, not just the encryption: restored systems may
  contain backdoors, new admin users or poisoned data from the dwell period. Retention must cover
  dwell time plus detection, which argues for weeks to months, not days.
- Restore into a **clean environment** with fresh credentials; restoring into the compromised
  account hands the attacker a second chance.

### 7.3 A separate backup account

```
  AWS Organization
  ├── prod account          (app, DB, repo1)
  │     role: pg-backup-writer  ──PutObject/GetObject/ListBucket──┐
  │                                                               ▼
  ├── backup account        (repo2 vault bucket: versioned, Object Lock COMPLIANCE 35 d,
  │     no human IAM users;  bucket-owner-enforced; SSE-KMS with a key owned HERE;
  │     access via SSO       AWS Backup vault with Vault Lock for RDS/EBS copies)
  │     break-glass role only (MFA, two-person approval, alerts on use)
  │
  └── SCPs on the backup account: deny s3:PutBucketObjectLockConfiguration,
      s3:PutBucketVersioning, s3:PutLifecycleConfiguration, s3:DeleteBucket*,
      kms:ScheduleKeyDeletion, kms:DisableKey, backup:DeleteBackupVault* ... except break-glass
```

Design points:

- **Different credentials and different humans.** The backup account has no long-lived IAM users;
  access goes through the identity provider with a small group and MFA. Losing the production
  admin role must not grant anything in the backup account.
- **The production writer can only add.** The bucket policy grants the production backup role
  `s3:PutObject`, `s3:GetObject` and `s3:ListBucket` (tools read their own manifests). With
  versioning and compliance-mode Object Lock, a delete from that role creates a delete marker
  and removes nothing. Verify that your backup tool can write to an Object Lock bucket (S3 requires
  an integrity checksum header on such uploads) and that its retention logic does not fail on
  locked objects.
- **Recovering from a delete-marker attack** means listing object versions and removing the
  markers (or copying the latest locked versions to a clean bucket). Script it and rehearse it.
- **Managed service copies.** For RDS/Aurora/EBS, AWS Backup can copy recovery points to a vault
  in another account and region; AWS Backup Vault Lock in compliance mode prevents deletion or
  shortening retention after its grace period. Other clouds have equivalents (immutable vaults,
  retention locks); check the exact semantics.
- **A different provider** for one copy (a weekly `pg_dump` to another cloud's object storage with
  object lock) is the only protection against losing the whole cloud account (§15.5).

### 7.4 Encryption and least privilege for restore

- **Encrypt backups client-side** (pgBackRest cipher, WAL-G libsodium) in addition to server-side
  encryption, so a leaked bucket is not a leaked database. Losing the passphrase is losing the
  backup: escrow it (§7.5).
- **Restore needs read access to the vault; nobody needs it daily.** Grant it via a restore role
  in the backup account, assumable only during an incident or drill, logged and alerted.
- **Deletion and retention changes need two people.** Shortening retention or disabling
  Object Lock is exactly what an attacker would do; make it a break-glass, two-person action.
- **Backups contain everything the database contains**, including personal data and secrets in
  tables. Their access control must be at least as strict as production's.

### 7.5 The break-glass kit

Keep, outside the production account and outside the identity provider it depends on:

- backup-account access path (a break-glass user or hardware key, sealed procedure);
- backup tool configuration and **encryption passphrases**;
- the DR runbooks (§9) and the contact list, in a form readable during an outage (a printed copy
  or an offline password manager vault counts);
- IaC repository mirror and the instructions to bootstrap a new account;
- registrar and DNS provider credentials.

Test the kit in the quarterly drill: "restore with production credentials revoked" is the only
way to find the step that silently depends on the thing that is gone.

---

## 8. Restore Drills — Untested Backups Are Not Backups

> **In plain words.** A backup is only a hypothesis until you restore it. Restore automatically,
> every night if you can, into a throwaway environment, check that the data is right, and record
> how long it took. That number is your real RTO.
>
> **Real-world example.** The invoicing SaaS runs a nightly job in a scratch account: restore
> `repo2` to a random point in the last 24 hours, check row counts, run `pg_amcheck`, run the
> app's read-only smoke tests, then destroy the instance. The job emits
> `restore_test_duration_seconds` (median 58 minutes) and a pass/fail metric. The 95-minute RTA in
> §0 was predicted by it.

### 8.1 What goes wrong with untested backups

Every item below has happened to real companies (§13, §15):

- The backup job has been failing for months and the alert goes to an unread mailbox.
- The backup "succeeds" but produces empty or truncated files (version mismatch between tool and
  server, a changed path, a permissions error written to a log nobody reads).
- The backup is fine but the WAL chain has a gap, so PITR stops early.
- The backup is fine but the encryption key or passphrase is gone.
- Everything is fine, but the restore takes 30 hours instead of the 4 assumed, because of disk
  throughput, lazy loading or single-threaded replay.
- The data restores but the application cannot use it: missing roles, extensions, sequences
  behind, secrets that changed.

### 8.2 Automated restore testing

```
  nightly, in a scratch account (not production)
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │ 1. provision instance from IaC (same size class as prod, fast disk)          │
  │ 2. pick a target: random time in the last 24 h (exercises WAL, not just base)│
  │ 3. restore from the VAULT repo (repo2), using the break-glass-style config   │
  │ 4. wait for recovery to reach the target; record t_restore, t_replay         │
  │ 5. validate: structural → data → application (§8.3)                          │
  │ 6. push metrics: success, duration, recovery point reached, data checks      │
  │ 7. destroy everything                                                        │
  └──────────────────────────────────────────────────────────────────────────────┘
```

A **heartbeat table** turns "did PITR reach the right point?" into a query. Production writes one
row a minute:

```sql
CREATE TABLE dr_heartbeat (ts timestamptz PRIMARY KEY DEFAULT now());
-- cron / pg_cron every minute on the primary:
INSERT INTO dr_heartbeat DEFAULT VALUES;
```

After restoring to target `T`, `SELECT max(ts) FROM dr_heartbeat` must be within about a minute of
`T`. The gap between the newest heartbeat available at restore time and `now()` in a restore of
"latest" is a direct measurement of RPA for a destroyed primary.

### 8.3 Validation levels

| Level | Checks | Example |
|---|---|---|
| Structural | server starts, recovery reached target, no errors in log; page and index integrity | `pg_amcheck --all --heapallindexed` (PG14+); data checksums enabled so corrupt pages fail loudly |
| Data | row counts and checksums of key tables versus values recorded by production at a known time; invariants | `SELECT count(*), sum(amount_cents) FROM invoices WHERE created_at < $T` compared with a nightly production snapshot of the same query; `SELECT count(*) FROM customer_tax_profiles` > threshold; no `NULL` in NOT-NULL-by-business columns |
| Application | the real app (read-only mode) can log in, render key pages, run reports | the smoke-test suite pointed at the restored DB |
| Operational | the restore used only documented steps and the break-glass kit | the job uses the same script as the runbook, not a special path |

Checking the "business invariants" in the data level is also how slow logical corruption gets
detected early: a nightly restore that compares yesterday's and today's null rates, sums and
counts is a cheap anomaly detector (§9.2).

### 8.4 Game days and runbooks

Automation proves the bytes come back; game days prove the *people and process* work. Once a
quarter, run one of the §9 scenarios end to end with the on-call engineer who did not write the
runbook, a timer and an observer:

- "The primary database was dropped at 10:00 and it is now 10:20." Measure RTA to service.
- "The production AWS account is inaccessible." Restore from the vault into a fresh account using
  only the break-glass kit.
- "A bug has corrupted `invoices.total` for three weeks." Find the first bad day (§9.2) and repair.

Record RTA, RPA, and every step where the runbook was wrong; fix the runbook the same week. Game
day design, blast radius and the verification loop are covered in
[`../sre-observability/38-continuous-verification.md`](../sre-observability/38-continuous-verification.md)
(§6 game days versus continuous chaos, §12 a worked drill), and incident roles and tabletop
exercises in [`../sre-observability/15-incident-response-and-postmortem.md`](../sre-observability/15-incident-response-and-postmortem.md)
(§4, §10). A good DR runbook has: the trigger and decision criteria, who decides, exact commands
with placeholders, expected output and durations at each step, verification queries, the rollback
of the recovery itself, and the communications template.

---

## 9. Recovery Playbooks — Four Runbooks

> **In plain words.** When data is damaged, the first job is to stop more damage, the second is to
> preserve evidence and options, and only then to restore. Restoring to a side instance and
> copying back only what was lost is almost always better than rolling the whole database back.

### 9.1 Runbook A: accidental `DELETE`, bad migration, bad deploy (noticed within the PITR window)

**Decision: surgical repair or full rollback?**

| Choose | When |
|---|---|
| **Surgical repair** (default) | damage is confined to identifiable rows/columns/tables; post-incident writes are valuable; the lost data can be merged back with clear conflict rules |
| **Full rollback** (PITR of production) | damage is widespread or unbounded (the corruption touched many tables, or you cannot tell what is wrong); post-incident writes are few, or can be replayed from an event log/outbox; the business accepts losing them |
| **Roll forward** (fix with code) | the lost information can be recomputed from other data (a derived column, a cache table) |

**Steps (surgical repair, PostgreSQL):**

1. **Stop the bleeding.** Halt deploys and the migration pipeline; disable the job or feature that
   writes bad data; if necessary put the affected feature in read-only or maintenance mode. Do
   **not** fail over to a replica (it has the same damage). If a delayed replica exists, pause it
   now: `SELECT pg_wal_replay_pause();`.
2. **Protect the options.** Make sure backups and WAL are not about to expire (extend retention on
   the relevant backup set if the window is close); take a fresh backup or snapshot of the current
   state, because you are about to write to production.
3. **Find the exact moment.** Sources: migration logs, deploy timestamps, the Postgres log
   (`log_statement = 'ddl'` records DDL with timestamps), application logs. For precision, find the
   transaction in the WAL:

   ```bash
   # which commits happened around 14:05, and which transaction touched many rows?
   pg_waldump -p /path/to/wal/archive 00000001000000A7000000A9 | grep COMMIT
   pg_waldump -p /path/to/wal/archive -r Heap 00000001000000A7000000A9 \
     | grep -o 'tx: *[0-9]*' | sort | uniq -c | sort -rn | head   # heap records per transaction
   ```

   The recovery target becomes that transaction ID with `recovery_target_inclusive = off` (stop just
   before it commits), or a timestamp a few seconds earlier.
4. **Restore to a side instance**, paused at the target, never over production:
   `pgbackrest --type=xid --target=<xid> --target-exclusive --target-action=pause
   --archive-mode=off restore` on a scratch host (§5.2), or an RDS/Cloud SQL PITR to a new
   instance (§5.4).
5. **Verify the side instance** has the good data (`SELECT count(tax_id) FROM customers`), and
   the heartbeat (§8.2) confirms the recovery point.
6. **Extract and merge** with an explicit rule for conflicts with post-incident writes:

   ```sql
   -- on production, with postgres_fdw pointing at the side instance (or \copy out/in)
   CREATE EXTENSION IF NOT EXISTS postgres_fdw;
   CREATE SERVER pitr FOREIGN DATA WRAPPER postgres_fdw
     OPTIONS (host 'pitr-0310.internal', dbname 'app');
   CREATE USER MAPPING FOR CURRENT_USER SERVER pitr OPTIONS (user 'restore_ro', password '...');
   CREATE SCHEMA pitr_0310;
   IMPORT FOREIGN SCHEMA public LIMIT TO (customers) FROM SERVER pitr INTO pitr_0310;

   BEGIN;
   INSERT INTO customer_tax_profiles (customer_id, tax_id, source)
   SELECT c.id, c.tax_id, 'restored-2026-03-10'
   FROM pitr_0310.customers c
   WHERE c.tax_id IS NOT NULL
   ON CONFLICT (customer_id) DO NOTHING;     -- post-incident edits win
   -- check the count matches expectations (≈ 16,300) before committing
   COMMIT;
   ```

   Conflict rules must be decided per table: "newer write wins" for customer-edited fields,
   "restored value wins" for data customers could not have changed, and manual review for money.
7. **Repair the consequences.** Downstream effects of the bad data (invoices issued without VAT
   IDs, emails sent, caches, search indexes, analytics, data sent to partners) need their own
   fixes. List them in the runbook for each critical table.
8. **Close out.** Remove the foreign server and side instance, record RPA/RTA, write the
   postmortem. Typical prevention items: expand/contract migrations (add the new column, backfill,
   switch reads, drop the old column in a *later* release), `pg_create_restore_point()` before
   risky migrations, a row-count and null-rate check after backfills, `lock_timeout` and
   `statement_timeout` on migrations, a delayed replica, and no production write access from
   laptops.

**Full rollback variant.** Restore production itself to the target (pgBackRest `--delta` in place,
or PITR to a new managed instance and switch the endpoint), then decide what to do with writes
since the incident: export them first from the damaged database (`WHERE created_at >= '14:05'`),
and replay them through the application or an event log after the rollback. Communicate that the
service was rolled back; customers may repeat actions.

### 9.2 Runbook B: corruption discovered three weeks late

The dangerous case: a bug introduced on 18 February has been writing wrong `invoices.total` values
for some currencies; it is noticed on 11 March. PITR retention is 14 days, so the oldest restorable
point (25 February) is already after the bug.

```
  Feb 18         Feb 25                   Mar 11
  bug ships      oldest PITR point        detected
    │               │                        │
  ──┼───────────────┼────────────────────────┼──►
    │◄─ no clean ──►│◄──── PITR window: every point here is already corrupted ───►│
       copy in PITR
  Long-term copies (weekly for 8 weeks, monthly for 12 months) still have Feb 15.
```

**Retention must be longer than your worst plausible detection time.** Use tiered (grandfather-
father-son) retention: PITR for 14–35 days, weekly fulls for 8–13 weeks, monthly for 12 months
(in a cold storage class), each in the vault. Silent logical corruption and ransomware dwell times
are measured in weeks.

**Steps:**

1. **Stop the writer.** Fix or disable the bug first; otherwise the repair is overwritten.
2. **Bound the damage with an invariant.** Write a query that is true on good data and false on
   bad (`total_cents = sum(line_items.amount_cents) + tax_cents`). Run it on production to count
   bad rows and find the earliest bad `created_at`/`updated_at`.
3. **Find the last clean copy by bisection.** Restore long-term copies to scratch instances and run
   the invariant. Binary search over 90 days of daily copies needs about `log2(90) ≈ 7` restores;
   automated restore tooling (§8) makes this an afternoon rather than a week.
4. **Repair forward, never roll back.** Three weeks of legitimate writes cannot be discarded.
   Recompute corrupted values from source data where possible (line items were correct; totals were
   wrong), or take the pre-bug values from the clean copy for rows not modified since, and flag
   the rest for manual review.
5. **Repair consequences** (invoices sent, payments captured, reports filed) with finance/legal.
6. **Prevention.** Invariant checks as scheduled jobs and in the nightly restore test; data
   checksums on (`initdb --data-checksums`; PostgreSQL 18 enables them by default for new
   clusters; `pg_checksums --enable` on an offline cluster otherwise); `pg_amcheck` weekly; audit
   columns (`updated_by`, `updated_at`); longer retention for the vault.

### 9.3 Runbook C: region or cloud-account loss

Pre-requisites (without these, this runbook is a rebuild from memory): backups and WAL in another
region **and** another account (§7.3); IaC that can target the recovery region/account; DNS and
registrar outside the failed domain; secrets and keys available there (§6.5); container images
replicated or rebuildable; runbook and break-glass kit reachable.

1. **Declare.** Decide it is a DR event, not a wait-it-out event. Criteria and who decides must be
   written in advance: for example, "region impairment affecting the database for more than 60
   minutes with no provider ETA". Detection-versus-decision and failover mechanics for warm
   standby and active tiers are in
   [`36-multi-region-active-active-and-geo-replication.md`](36-multi-region-active-active-and-geo-replication.md)
   §7; this runbook covers backup-and-restore and pilot light.
2. **Freeze the old side.** If the old region comes back mid-recovery, it must not accept writes
   (fence: disable the old DB user, block the old endpoint in DNS and security groups). Two
   primaries writing is worse than either outage.
3. **Provision** the recovery environment from IaC in the target region/account.
4. **Restore the database** from the vault repository (`pgbackrest --repo=2 ... restore`) to the
   latest point, or from cross-region automated backups / copied snapshots for managed databases.
   RPA = what reached the vault before the region failed (archive lag plus replication lag of the
   vault).
5. **Restore other state**: point object storage readers to the replica bucket; restore Redis if
   it is a store; restore or rebuild search (§6.4); rehydrate Kafka from the source of truth.
6. **Secrets and keys.** Load secrets from the replicated secret store or re-issue them; confirm
   KMS keys usable in this region decrypt the restored data.
7. **Verify** with the §8.3 checks and smoke tests, then **cut over** DNS (low TTLs set in advance
   help; many clients ignore them) and warm caches before full traffic.
8. **Reconcile** after the old region returns: transactions acknowledged in the old region after
   the last archived WAL are lost to the new primary; extract them from the old database (if it
   survived) and re-apply or report them. Failback is a separate, planned operation.

For the 2 TB case in §2.3 with a prepared pilot light (IaC tested, vault in the target region),
realistic RTA is dominated by `t_restore` and `t_replay`: roughly 1–6 hours depending on disk
throughput and backup frequency. If that is too long for the business, the answer is a warm
standby (chapter 36), not a faster backup tool.

### 9.4 Runbook D: ransomware or malicious deletion

1. **Contain.** Isolate affected systems (network, not power-off, to preserve evidence); revoke
   sessions and rotate credentials of every identity that could have been used; disable
   compromised CI tokens and access keys. Engage security incident response, legal, and (depending
   on jurisdiction and data) regulators and law enforcement; ransom payment decisions involve
   legal and sanctions questions and are not an engineering call.
2. **Verify the vault is intact.** Use the backup account's break-glass path from a clean device.
   Check Object Lock status, version counts and the last good backup. Do not mount or connect the
   vault to any compromised environment.
3. **Determine the clean restore point.** Not "just before encryption" but "before the intrusion",
   using security logs (CloudTrail, database audit logs) to find first attacker activity. Newer
   data may need to be salvaged selectively and inspected.
4. **Build a clean environment.** New account or thoroughly cleaned one, fresh credentials, IaC
   from a trusted commit, base images from a trusted registry, patched.
5. **Restore** in criticality order (Tier 0 first), from the vault, to the chosen point; scan
   restored data and binaries.
6. **Rotate everything.** Database passwords, API keys, OAuth client secrets, signing keys,
   customer-facing tokens if exposure is possible. Secrets inside the restored database (for
   example integration tokens stored in tables) are compromised too.
7. **Bring service back** with heightened monitoring; expect the attacker to try again.
8. **Postmortem and notification.** Data exfiltration usually triggers breach-notification duties
   (GDPR Article 33: supervisory authority within 72 hours of becoming aware, where required).

---

## 10. GDPR, Data Residency and Compliance

> **In plain words.** Privacy law says you must delete a person's data when asked, and backups
> are designed never to forget. The accepted answers are: keep backups for a limited, documented
> time; do not restore erased people back into production; or make their data unreadable by
> destroying a per-person key. Also keep backups in the same legal region as the data.

### 10.1 Right to erasure versus backups

GDPR Article 17 gives data subjects a right to erasure. Rewriting every backup to remove one
person is usually impractical and would undermine backup integrity. Regulators have accepted
practical approaches; the UK ICO's guidance, for example, describes putting data "beyond use" when
immediate deletion from backups is not possible. A defensible approach:

1. **Finite, documented retention.** Backups expire on a fixed schedule (for example 35 days PITR,
   12 months monthly copies). Erased data disappears from backups when they age out. Document this
   in the records of processing and the privacy notice.
2. **Beyond use.** Backups are not accessed except for restore; restored data is not used for
   other purposes.
3. **Erasure log re-applied on restore.** Keep a minimal list of erased subject identifiers (for
   example the internal user ID, or a keyed hash of it) with the erasure date. Every restore
   procedure (full or surgical) re-runs the erasures for subjects erased after the recovery
   point, before the restored data serves traffic. Put this step in the runbooks of §9 and test it
   in drills.
4. **Crypto-shredding (§10.2)** where retention must be long.

### 10.2 Crypto-shredding

Encrypt each subject's (or tenant's) sensitive fields with a per-subject data key; store the data
keys in a key store encrypted by a KMS key. To erase a subject, delete their data key. Every copy of
their ciphertext, in production, replicas, backups and exports, becomes unreadable without touching
the backups.

```
  users.email_enc = AES-GCM(key_user_42, "ana@example.com")
  key_store: user_42 → KMS-encrypt(key_user_42)
  erase(user 42): DELETE FROM key_store WHERE user_id = 42  → every backup copy of email_enc is noise
```

The pitfalls:

- **The key store is backed up too.** If the key store's backups keep deleted keys for 12 months,
  the shredding is delayed by 12 months. Give the key store a short backup retention (with its own
  immutable copy), or use a KMS that manages key durability and deletion itself, and re-apply key
  deletions after any key-store restore (the erasure log again).
- **Lose the key store and you have shredded everyone.** Its availability and backup deserve the
  highest tier.
- **Searchability and performance**: encrypted fields cannot be indexed or queried by value;
  use keyed hashes (blind indexes) for lookup fields.
- **Legal view**: whether encrypted data with a destroyed key counts as erased is widely treated
  as an acceptable way to put data beyond use, but it is not uniformly settled; document the
  reasoning and get privacy counsel to sign off.

Per-tenant keys serve the B2B case well: a customer leaving the platform can be shredded in one
operation, and per-tenant restores (§13) become easier to scope.

### 10.3 Data residency of backups

Backups are processing of personal data. If you promise EU residency, a cross-region copy to a
US region breaks the promise even if nobody ever restores it. Choose DR regions inside the same
jurisdiction (for example `eu-central-1` primary, `eu-west-1` vault), check where managed services
store automated backups and snapshot copies, and include backup locations in the data-processing
records and in customer contracts. Residency and geo-partitioned designs are covered in
[`36-multi-region-active-active-and-geo-replication.md`](36-multi-region-active-active-and-geo-replication.md) §5.

### 10.4 Other regimes in brief

- **PCI DSS (v4.0)** applies to backups containing cardholder data: stored PAN must be unreadable
  wherever it is stored, backups included (Requirement 3.5); retention must be limited by policy,
  with a process at least every three months to delete data past retention (3.2.1); media with
  cardholder data must be physically secured, and offline backup locations reviewed at least
  every 12 months (9.4). The cheapest compliance strategy is tokenization: keep PAN out of your
  databases and your backups leave PCI scope.
- **HIPAA** requires a data backup plan, a disaster recovery plan, and testing and revision
  procedures (the contingency plan standard of the Security Rule).
- **SOC 2** audits (availability and confidentiality criteria) typically ask for backup
  configuration, retention, restore-test evidence and access reviews. The metrics in §11 double
  as that evidence.

Compliance for telemetry specifically (logs containing personal data, erasure in log stores) is in
[`../sre-observability/32-compliance-and-privacy.md`](../sre-observability/32-compliance-and-privacy.md).

---

## 11. Observability of DR — Backup and Restore SLOs and Alerts

> **In plain words.** Measure three things continuously: how old is the newest good backup, how
> far behind is WAL archiving, and did last night's restore test pass. Alert on each. Those three
> numbers are your real RPO and RTO in dashboard form.

### 11.1 DR SLOs

| SLI | SLO (example) | Why |
|---|---|---|
| Age of newest successful backup, per database and repo | < 26 h for daily, 99% of the time | a missed backup is found the next morning, not during the disaster |
| WAL archive lag (`L_archive`) | < 5 min, 99.9% of the time | this *is* the RPO for a destroyed primary |
| Archive failures | 0 sustained failures for > 15 min | failing archives also fill `pg_wal` and stop the primary |
| Restore test pass rate | ≥ 95% of nightly runs over 30 days; never 2 failures in a row | evidence that backups restore |
| Restore test duration (RTA proxy) | p95 < 60% of RTO | headroom for human steps |
| Vault copy age (repo2, AWS Backup copy jobs, S3 replication latency) | < 26 h / < 15 min | the attacker-proof copy is current |
| Retention coverage | oldest restorable point ≥ `T_ret` ago | retention misconfigurations shorten the window silently |

### 11.2 Where the metrics come from

- **PostgreSQL archiver**: `pg_stat_archiver` (`archived_count`, `failed_count`,
  `last_archived_time`, `last_failed_time`); segments waiting to be archived:
  `SELECT count(*) FROM pg_ls_archive_statusdir() WHERE name LIKE '%.ready';` (PG12+). Exporters
  (postgres_exporter or custom queries) expose these; metric names below follow a custom query
  and may differ in your exporter.
- **Backup jobs**: have the backup and restore-test scripts push their own metrics (Pushgateway or
  the node-exporter textfile collector): `backup_last_success_timestamp_seconds{db,repo,type}`,
  `backup_size_bytes`, `restore_test_last_success_timestamp_seconds`,
  `restore_test_duration_seconds`, `restore_test_recovery_point_lag_seconds` (target versus
  newest heartbeat, §8.2). pgBackRest's `info --output=json` and WAL-G's `backup-list --json`
  make this easy; dedicated exporters exist too.
- **Managed services**: RDS `LatestRestorableTime` (poll via API), AWS Backup job and copy job
  states (CloudWatch/EventBridge), S3 replication metrics (`ReplicationLatency`,
  `OperationsFailedReplication`).
- **Security signals for DR assets**: CloudTrail events `DeleteBucket`, `PutBucketLifecycle`,
  `PutObjectLockConfiguration`, `DeleteDBSnapshot`, `DeleteRecoveryPoint`, `ScheduleKeyDeletion`,
  `DisableKey` on backup resources should page someone
  ([`../sre-observability/27-security-observability.md`](../sre-observability/27-security-observability.md)).

### 11.3 Alert rules

```yaml
groups:
- name: disaster-recovery
  rules:
  - alert: BackupTooOld
    expr: time() - max by (db, repo) (backup_last_success_timestamp_seconds) > 26 * 3600
    for: 15m
    labels: { severity: page }
    annotations:
      summary: "No successful backup of {{ $labels.db }} to {{ $labels.repo }} in 26h"

  - alert: WalArchiveFailing
    expr: increase(pg_stat_archiver_failed_count[15m]) > 0
          and increase(pg_stat_archiver_archived_count[15m]) == 0
    for: 15m
    labels: { severity: page }
    annotations:
      summary: "WAL archiving failing on {{ $labels.instance }}: RPO growing, pg_wal filling"

  - alert: WalArchiveBacklog
    # custom query: count of .ready files in pg_wal/archive_status
    expr: pg_wal_archive_ready_segments > 20
    for: 10m
    labels: { severity: page }

  - alert: WalArchiveStale
    # on a DB with archive_timeout = 60 and steady writes; idle DBs produce no segments
    expr: time() - pg_stat_archiver_last_archive_time > 600
    for: 5m
    labels: { severity: ticket }

  - alert: RestoreTestStale
    expr: time() - restore_test_last_success_timestamp_seconds > 2 * 86400
    labels: { severity: page }
    annotations:
      summary: "No passing restore test for {{ $labels.db }} in 2 days"

  - alert: RestoreTestTooSlow
    expr: restore_test_duration_seconds > on(db) group_left() (0.6 * dr_rto_seconds)
    labels: { severity: ticket }

  - alert: BackupSizeAnomaly
    # a backup half the size of yesterday's is a data-loss signal or a broken job
    expr: backup_size_bytes{type="full"} < 0.5 * (backup_size_bytes{type="full"} offset 7d)
    labels: { severity: ticket }
```

Notes: combine the "failed" and "archived" counters so a single transient failure does not page;
alert on the `.ready` backlog as well as on age, because `last_archive_time` also grows on an idle
database with no WAL to archive; and treat a sudden drop in backup size or table row counts as a
possible logical disaster, not only as a backup problem. Alerting design and routing:
[`../sre-observability/12-alerting.md`](../sre-observability/12-alerting.md); database telemetry
in general: [`../sre-observability/23-database-observability.md`](../sre-observability/23-database-observability.md).

---

## 12. Decision Guide — DR by Company Stage

> **In plain words.** Start with managed PITR, one immutable copy somewhere else, and a restore
> test. Add automation, cross-account vaults and drills as the company grows. Add standby regions
> only when the cost of an hour of downtime justifies paying for idle servers.

### 12.1 Recommended setups

| Stage | Typical shape | Recommended DR setup | Realistic RPO / RTO | Rough monthly cost drivers |
|---|---|---|---|---|
| **MVP** (1–5 engineers, DB < 50 GB) | one managed Postgres, one region, object storage | managed automated backups + PITR (7–14 days), deletion protection; S3 versioning; nightly `pg_dump` to a different account or provider with object lock (30 days); IaC for everything; a manual restore test monthly with the time written down | RPO ≈ 5 min (PITR) / 24 h (independent copy); RTO 2–8 h | backup storage (tens of GB × copies) and a little transfer: **tens of dollars** |
| **Growth** (10–50 engineers, 100 GB–1 TB, B2B contracts with DR clauses) | managed or self-managed Postgres with replicas, Redis, search, Kafka or queues | PITR 14–35 days; vault copy in a separate account and region with compliance-mode lock (pgBackRest `repo2` or AWS Backup cross-account + Vault Lock); GFS long-term copies; nightly automated restore test with metrics and heartbeat checks; DR alerts (§11); runbooks A–D; quarterly game day; optional delayed replica; pilot light if RTO < 4 h after region loss is contractual | RPO ≤ 5 min / RTO 1–4 h (logical), 4–12 h (region loss) | vault storage and replication transfer, restore-test compute (a few instance-hours a night), engineer time for drills: **hundreds to low thousands of dollars** |
| **Scale** (100+ engineers, multi-TB, many tenants, regulated) | many databases, multi-region | everything above, automated per database from a platform template; warm standby or active-active for Tier 0 (chapter 36); per-tenant restore tooling; crypto-shredding with per-tenant keys; long-term immutable archive in cold storage; DR program with owners, audited evidence, cross-provider copy for Tier 0 | RPO seconds / RTO minutes for Tier 0 infrastructure loss; hours for logical | standby compute (the dominant term), cross-region transfer, people: **tens of thousands of dollars and up** |

### 12.2 A worked cost sketch (growth stage, illustrative list prices)

For the invoicing SaaS: 300 GB database, zstd-compressed full about 100 GB, daily block-incremental
about 5 GB compressed, WAL about 24 GB/day raw, about 8 GB/day compressed.

```
repo1 (14 days, S3 Standard ~ $0.023/GB-month):
  2-3 fulls × 100 GB + 12 incrementals × 5 GB + 14 days × 8 GB WAL ≈ 470 GB  → ≈ $11/month
repo2 vault (35 days, other region, Object Lock):
  ~5-6 fulls + diffs + 35 days WAL                                ≈ 1.1 TB   → ≈ $25/month
  cross-region transfer of new data ≈ 700 GB/month × ~$0.02/GB               → ≈ $14/month
Long-term monthly fulls, 12 months, Glacier-class (~$0.004/GB-month)  1.2 TB → ≈ $5/month
Nightly restore test: an instance for ~2 h/night + fast disk, torn down       → $30–100/month
Total backup + verification: roughly $85–155 / month
Warm standby of the same stack in a second region, for comparison:            → $1,500–4,000 / month
```

The lesson matches §2.4: **copies and tests are cheap; idle standby capacity is expensive**. Spend
on the first before the second.

### 12.3 One-page checklist

- [ ] Every stateful system classified (source of truth or derived, tier, RPO, RTO, owner).
- [ ] PITR enabled with retention ≥ realistic detection time; long-term copies for slow corruption.
- [ ] One copy in a separate account (and region) that production credentials cannot delete.
- [ ] Encryption keys and backup passphrases recoverable without the production account.
- [ ] Automated restore test with validation; its duration tracked against RTO.
- [ ] Alerts on backup age, archive lag/backlog, restore-test staleness, and deletions of DR assets.
- [ ] Runbooks A–D written, stored outside production, rehearsed this quarter.
- [ ] Erasure log re-applied on restore; backup locations match residency promises.

---
## 13. Production pitfalls / war stories

Composite lessons (except where a public source is named); numbers illustrative.

1. **"We have replicas, so we have backups."** A team with a three-node Patroni cluster and no
   PITR runs a data-fix script against the wrong environment variable and updates every row's
   `status`. All three nodes agree within a second. The only copy is a two-week-old dump someone
   made before an upgrade. Replication count is not a backup count (§1).
2. **The archive that filled the disk.** An IAM policy change breaks `archive_command`. Postgres
   retains every unarchived segment in `pg_wal`; four hours later the volume is full and the
   primary stops. The DR mechanism caused the outage. Alert on archive failures and the `.ready`
   backlog (§11), and size `pg_wal` with headroom.
3. **Backups that succeed and contain nothing.** A backup script's exit code comes from the last
   command in a pipeline (`pg_dump ... | gzip | aws s3 cp - ...`), so a failing `pg_dump` still
   "succeeds" with a 20-byte gzip file. Use `set -o pipefail`, check sizes against yesterday, and
   let the restore test be the real success signal. GitLab's 2017 incident included a variant of
   this: dumps failing on a version mismatch, with failure emails never delivered (§15.1).
4. **The restore that took ten times the plan.** A 1.5 TB restore was budgeted at 1 hour from a
   calculation using S3 bandwidth, but the target volume was provisioned at its baseline
   throughput and the snapshot-backed volume loaded lazily. Real time: over 9 hours. The
   bottleneck is `min(...)` of every stage (§2.3); only a drill finds it.
5. **PITR target past the end of WAL.** A restore to "15:00" fails because archiving stopped at
   11:40 after a credential rotation. PostgreSQL 13+ refuses to promote when the configured target
   is not reached (earlier versions silently ended recovery at the last available WAL). Know your
   newest restorable point before promising one (`pgbackrest info`, `LatestRestorableTime`).
6. **The side instance that archived into production's repository.** A PITR side instance is
   promoted with production's `archive_command` still configured, and pushes a new timeline into
   the production repository. Usually harmless because of timelines, occasionally very confusing
   during a real incident. Restore side instances with archiving off (`--archive-mode=off`).
7. **Per-tenant restore that does not exist.** A multi-tenant SaaS can restore the whole database
   to a point but has never restored *one tenant* into a live shared database. When a script
   deletes 40 tenants' data, engineers write the per-tenant merge under pressure for days. If you
   are multi-tenant, build and test the per-tenant surgical path (Atlassian 2022, §15.4).
8. **KMS key scheduled for deletion by a cleanup script.** An "unused keys" cleanup job schedules
   deletion of a key that encrypted last year's snapshots. The 30-day waiting period and a
   CloudTrail alert on `ScheduleKeyDeletion` are the only reasons the snapshots survive. Keys are
   data (§6.5).
9. **Toy Story 2 (1998, as publicly recounted by people involved).** An `rm` command started
   deleting the film's files; the backups turned out not to have been working, and most of the
   work was recovered from a copy that an employee working from home happened to have on her
   workstation. The oldest lesson in this chapter: backups are proven only by restores.
10. **Restoring the attacker.** A company restores from the newest clean-looking backup after
    ransomware and is re-encrypted a week later: the backup already contained the attacker's
    persistence. The restore point must predate the intrusion, and restores go into a clean
    environment with rotated credentials (§9.4).
11. **Retention shorter than the audit cycle.** A billing bug is found by the quarterly finance
    reconciliation, 70 days after it started. PITR retention is 7 days; no long-term copies exist.
    Tie retention to how slow your slowest detection mechanism is (§9.2).
12. **Erased users coming back.** A restore brings back accounts that were deleted under GDPR
    requests after the recovery point, and they start receiving marketing email. The erasure log
    must be part of every restore runbook (§10.1).

---

## 14. Interview questions

**Q1. Why is replication not a backup?**
Replication copies every change, including destructive ones, to every replica within milliseconds;
it protects against loss of a copy (disk, node, AZ, region), not against wrong data. A backup is a
copy from an earlier point, stored so that the destructive change and the people or credentials
that caused it cannot reach it. You need both.

**Q2. Define RPO and RTO. What is the difference between objective and actual?**
RPO is the maximum acceptable data loss, measured as time before the incident; RTO is the maximum
acceptable time until service is restored. Objectives are targets; RPA and RTA are what a drill or
incident achieved. Real RPO includes the time a broken backup goes unnoticed; real RTO includes
decision, provisioning, verification and cutover, not just copying bytes.

**Q3. Estimate the RTO for restoring a 2 TB PostgreSQL database from S3.**
Decompose: provision (10–20 min), restore bytes `D/B` where `B` is the slowest of S3 read, network,
decompression and target disk (2 TB at 125 MB/s is about 4.4 h; at 1 GB/s about 33 min), WAL replay
`W × t_base / R_replay` (can exceed the restore itself with weekly fulls), verification, cutover and
cache warm-up. Then say you would measure it in a drill, because replay rate and lazy-loaded disks
dominate and are hard to predict.

**Q4. A developer ran `DELETE FROM orders` without a `WHERE` 20 minutes ago. Walk through recovery.**
Stop writers to the affected table; do not fail over; pause a delayed replica if one exists. Find
the transaction (logs or `pg_waldump`). PITR a side instance to just before it
(`recovery_target_xid`, `inclusive = off`, `action = pause`). Verify. Copy the deleted rows back
with `INSERT ... ON CONFLICT DO NOTHING`, handling rows created since. Repair downstream effects,
record RTA/RPA, add guardrails (no ad hoc production writes, `pg_create_restore_point` before
risky operations).

**Q5. Full PITR rollback or surgical repair: how do you decide?**
Surgical when the damage is identifiable and post-incident writes matter; full rollback when damage
is widespread or unknowable and post-incident writes are few or replayable. Full rollback trades a
simpler procedure for losing everything written since the incident.

**Q6. How do you protect backups against an attacker with production admin credentials?**
A separate account with separate identities; production can write but not delete; Object Lock in
compliance mode (or an equivalent vault lock) with retention longer than likely dwell time;
backups encrypted with keys owned by the backup account; SCPs blocking retention and lock changes;
alerts on deletion attempts; a break-glass restore path tested with production credentials revoked.

**Q7. What is the 3-2-1-1-0 rule?**
Three copies, two media, one offsite, one offline or immutable, zero errors in verified restores.
In the cloud: different region and different account, immutability instead of tape, and automated
restore verification.

**Q8. How do you test backups?**
Automated restores on a schedule into a scratch environment, to a random point in time, with
structural checks (`pg_amcheck`, checksums), data checks (row counts, sums and invariants compared
with production values, a heartbeat table to confirm the recovery point) and application smoke
tests; record duration as the RTO proxy; plus quarterly human game days following the runbook.

**Q9. How long should backups be retained?**
Longer than the slowest realistic detection of logical corruption or intrusion, and no longer than
privacy obligations allow. Typical: PITR 14–35 days, weekly copies for 2–3 months, monthly for a
year in cold storage; shorter for data classes with strict erasure requirements, or use
crypto-shredding.

**Q10. How do you reconcile GDPR erasure with immutable backups?**
Finite documented retention so erased data ages out; backups kept beyond use; an erasure log
re-applied after every restore; crypto-shredding with per-subject or per-tenant keys where
retention is long. Keep backups within the promised jurisdiction.

**Q11. Which stateful systems do teams forget to back up?**
Encryption keys and backup passphrases, secrets, DNS zones, IaC state, Redis used as a store,
Kafka topics that are the source of truth, SaaS data (CRM, ticketing, identity provider
configuration), and the runbooks themselves.

**Q12. When is a warm standby region worth it for a small company?**
When expected annual downtime cost avoided (probability of a long regional outage × hours saved ×
cost per hour) exceeds the standby's yearly cost, or when a contract requires it. Otherwise
invest in PITR, immutable cross-account copies, IaC and drills, which cover more disaster types for
much less.

---

## 15. Real-world cases — incidents with numbers

### 15.1 GitLab.com, January 31, 2017: database deletion and five backup mechanisms

- **What happened.** While fighting database load from spam and trying to re-seed a lagging
  PostgreSQL secondary, an engineer ran a directory removal on the **primary** instead of the
  secondary. GitLab's postmortem reports that about 300 GB of data was removed before the command
  was stopped.
- **What the backups turned out to be.** According to the postmortem: regular `pg_dump` backups
  were failing because they ran a PostgreSQL 9.2 `pg_dump` against a 9.6 server, and the failure
  emails were rejected (DMARC), so nobody noticed; disk snapshots were not enabled for the database
  servers; replication had broken, which was the reason for the work in the first place. Recovery
  came from an LVM snapshot that an engineer had happened to take manually about six hours earlier
  for staging.
- **Numbers.** About six hours of database data were lost (roughly 5,000 projects, 5,000 comments
  and 700 new user accounts, per GitLab), and GitLab.com was down for about 18 hours, partly because
  copying data from the staging environment was slow. Git repositories themselves were not lost.
- **Lessons.** Backups that are never restored are hypotheses (§8); failure notifications are part
  of the backup system (§11); the dangerous moment is often *during* a recovery or maintenance
  operation, on the wrong host. GitLab's openness (a public live document and stream) made this
  one of the most instructive DR incidents on record.

### 15.2 OVHcloud Strasbourg, March 10, 2021: fire destroys a data center

- **What happened.** A fire destroyed the SBG2 data center and damaged part of SBG1 on OVHcloud's
  Strasbourg campus; the site's other buildings were shut down. Netcraft estimated that about
  3.6 million websites across 464,000 domains were affected by the outage.
- **Data loss.** Customers whose only copies were on servers in the destroyed buildings lost data
  permanently; some found that their backups had been stored on the same campus. The game studio
  Facepunch, for example, reported that some servers for its game Rust were destroyed and their
  data could not be recovered.
- **Lessons.** "Offsite" means a different site, and ideally a different region and failure domain,
  not a different rack or building on the same campus (§7.1). Multi-AZ-style redundancy inside one
  campus does not survive a campus-level event.

### 15.3 Code Spaces, June 2014: backups in the same account

- **What happened.** Code Spaces, a code-hosting company on AWS, suffered a DDoS combined with an
  extortion attempt by someone who had gained access to its AWS control panel. When the company
  tried to regain control, the attacker deleted resources: according to the company's statement,
  most data, backups, machine configurations and offsite backups were partially or completely
  deleted.
- **Outcome.** The company announced it could no longer operate and ceased trading.
- **Lessons.** Backups administered by the same credentials as production are not a defense
  against an attacker (§7.2). A separate account with deletion-proof retention is the minimum.

### 15.4 Atlassian, April 2022: deletion of hundreds of customer sites

- **What happened.** A script intended to delete data of a deprecated app was run with a list of
  site identifiers instead of app identifiers, permanently deleting 883 sites belonging to about
  775 customers, according to Atlassian's post-incident review.
- **Recovery.** Backups existed. But restoring many individual customer sites into the live
  multi-tenant environment, without disturbing other customers, was not an automated, rehearsed
  procedure at that scale; restoration took up to 14 days for the last customers.
- **Lessons.** RPO can be met while RTO is missed by days. In multi-tenant systems, design and test
  per-tenant restores, not just whole-database restores (§13 item 7); deletion tooling should
  default to soft delete with a delay.

### 15.5 UniSuper and Google Cloud, May 2024: the provider deleted the account

- **What happened.** UniSuper, an Australian pension fund with more than half a million members,
  lost its Google Cloud VMware Engine private cloud, which was deleted across both of its
  configured geographies. A joint statement from UniSuper and Google Cloud called it an isolated,
  one-of-a-kind occurrence; Google later attributed it to a parameter left blank during
  provisioning with an internal tool, which gave the subscription a fixed term after which it was
  automatically deleted.
- **Recovery.** UniSuper restored from backups it held with an additional service provider,
  outside Google Cloud. Services were down or degraded for about two weeks.
- **Lessons.** Geographic redundancy inside one provider account shares fate with that account.
  The "provider/account loss" row of §1.2 is real; an independent copy at another provider is what
  made recovery possible at all (§7.3).

### 15.6 Maersk and NotPetya, June 2017: the domain controller that happened to be offline

- **What happened.** The NotPetya malware spread through Maersk's global network within hours,
  wiping thousands of servers and tens of thousands of PCs. Maersk estimated the total cost at
  $250–300 million.
- **Recovery.** As reported by Wired (2018), Maersk's Active Directory domain controllers had been
  wiped too; the recovery used one domain controller in Ghana that had been offline during the
  attack because of a local power outage, and its data was physically carried to the recovery team.
- **Lessons.** Identity infrastructure is a Tier 0 system that needs its own offline backup, and
  "an offline copy" should be a design decision, not luck (§7.1, §7.5).

---

## 16. Sandbox experiments — run these yourself

Needs Docker. The experiment reproduces the chapter's incident in miniature: WAL archiving, a base
backup, good writes, a bad `UPDATE`, and PITR to a timestamp just before it. The same procedure
was verified on PostgreSQL 16 while writing this chapter.

```bash
# 1. Primary with WAL archiving to a shared volume
docker volume create pgdata && docker volume create pgarchive
docker run -d --name pg-primary -e POSTGRES_PASSWORD=pw \
  -v pgdata:/var/lib/postgresql/data -v pgarchive:/archive postgres:16 \
  -c wal_level=replica -c archive_mode=on -c archive_timeout=10 \
  -c archive_command='test ! -f /archive/%f && cp %p /archive/%f'
docker exec pg-primary chown postgres:postgres /archive
sleep 5
PSQL="docker exec -u postgres pg-primary psql -Atq"

# 2. Data, then a base backup
$PSQL -c "CREATE TABLE customers(id int PRIMARY KEY, tax_id text);
          INSERT INTO customers SELECT g, 'VAT'||g FROM generate_series(1,1000) g;"
docker exec -u postgres pg-primary pg_basebackup -D /archive/base -Fp -X stream -c fast

# 3. Good writes after the backup (must be recovered by WAL replay), then the incident
$PSQL -c "INSERT INTO customers SELECT g, 'VAT'||g FROM generate_series(1001,1500) g;"
T_GOOD=$($PSQL -c "SELECT now()"); echo "recovery target: $T_GOOD"
sleep 2
$PSQL -c "UPDATE customers SET tax_id = NULL;"        # the disaster
$PSQL -c "SELECT pg_switch_wal();"                    # make sure it is archived
sleep 3
$PSQL -c "SELECT count(*), count(tax_id) FROM customers;"          # 1500|0
$PSQL -c "SELECT archived_count, failed_count FROM pg_stat_archiver;"

# 4. Find the bad transaction in the archived WAL (optional)
docker exec -u postgres pg-primary bash -c \
  'pg_waldump -p /archive $(ls /archive | grep -E "^[0-9A-F]{24}$" | tail -1) | grep COMMIT | tail -3'

# 5. PITR into a NEW data directory; production is never touched
docker volume create pgrestore
docker run --rm -v pgarchive:/archive -v pgrestore:/restore postgres:16 bash -c \
  'cp -a /archive/base/. /restore/ && touch /restore/recovery.signal &&
   chown -R postgres:postgres /restore && chmod 700 /restore'
docker run -d --name pg-pitr -e POSTGRES_PASSWORD=pw \
  -v pgrestore:/var/lib/postgresql/data -v pgarchive:/archive postgres:16 \
  -c "restore_command=cp /archive/%f %p" \
  -c "recovery_target_time=$T_GOOD" \
  -c recovery_target_action=promote
sleep 5
docker exec -u postgres pg-pitr psql -Atq \
  -c "SELECT count(*), count(tax_id), pg_is_in_recovery() FROM customers;"   # 1500|1500|f
docker logs pg-pitr 2>&1 | grep -E "recovery stopping|timeline"
```

Variations worth doing:

- **Pause and inspect.** Use `recovery_target_action=pause`, connect, check the data, then run
  `SELECT pg_wal_replay_resume();` to end recovery.
- **Exact transaction.** Take the `tx:` of the bad commit from step 4 and restore with
  `-c recovery_target_xid=<tx> -c recovery_target_inclusive=off` instead of a time.
- **Surgical merge.** Keep the primary running with the damage, insert a few new rows into it, and
  merge `tax_id` back from `pg-pitr` without losing them (§9.1).
- **Break archiving.** `docker exec pg-primary chmod 000 /archive`, generate writes with
  `pgbench`, and watch `pg_stat_archiver.failed_count`, the `.ready` files in
  `pg_wal/archive_status`, and `pg_wal` growing (§13 item 2). Restore permissions afterwards.
- **Target beyond the WAL.** Set `recovery_target_time` to a future timestamp and observe that
  recovery refuses to finish (§13 item 5).
- **Measure `R_replay`.** Run `pgbench -i -s 50` and `pgbench -T 300` after the base backup, then
  time the PITR and divide the replayed WAL volume by the replay time.

Clean up: `docker rm -f pg-primary pg-pitr && docker volume rm pgdata pgarchive pgrestore`.

---

## Key Takeaways

1. **Replication protects copies; backups protect correctness.** Replicas, multi-AZ and
   multi-region copy logical disasters faithfully. Every tier of DR still needs PITR and an older,
   independent copy underneath.
2. **RPO and RTO are measured, not declared.** Real RPO includes how long a broken backup goes
   unnoticed; real RTO is the sum of decision, provisioning, restore, replay, verification and
   cutover, and only drills give the numbers.
3. **Do the arithmetic.** `t_restore = D / min(stage throughputs)`, `t_replay = W × t_base /
   R_replay`. Target disk throughput and backup frequency often matter more than the backup tool.
4. **Buy RPO cheaply, buy RTO carefully.** WAL archiving takes RPO from a day to about a minute for
   the cost of storage. Faster RTO for region loss means paying for standby capacity.
5. **Restore to the side, merge back surgically.** Full rollbacks destroy the good writes made after
   the incident. Pause at the target, inspect, extract, and merge with explicit conflict rules.
6. **Retention must exceed detection time.** Slow corruption and attacker dwell times are measured
   in weeks; keep grandfather-father-son copies, not only 7 days of PITR.
7. **Assume the attacker has admin.** One copy in a separate account, immutable (compliance-mode
   lock), encrypted with keys the attacker cannot delete, restorable with production credentials
   revoked. Consider a copy at another provider.
8. **Back up the things that gate everything else.** Encryption keys, backup passphrases, secrets,
   DNS, IaC and runbooks. Deleting a KMS key deletes every backup encrypted with it.
9. **Untested backups are not backups.** Automate nightly restores with structural, data and
   application checks, track duration against RTO, and run quarterly game days.
10. **Make DR observable and compliant.** Alert on backup age, archive lag and backlog, restore-test
    staleness and deletions of DR assets; re-apply erasures after restores; keep backups in the
    promised jurisdiction.

---

## Cross-References

### Within distributed-systems/

- [`00-primitives-and-system-models.md`](00-primitives-and-system-models.md): failure models
  (crash-stop, crash-recovery, Byzantine) and the RPO definition for replication failover; logical
  disasters are the case no replication failure model covers.
- [`04-replication-and-consistency.md`](04-replication-and-consistency.md): replication mechanics,
  lag and sync versus async, which determine RPO for infrastructure failover (§2.2).
- [`06-distributed-transactions-sagas-outbox-idempotency.md`](06-distributed-transactions-sagas-outbox-idempotency.md):
  the outbox and idempotent consumers that make "replay from the source of truth" safe (§6.2, §9.1).
- [`07-kafka-and-event-streaming.md`](07-kafka-and-event-streaming.md): Kafka replication and
  retention, relevant to why a replicated topic is not a backup (§6.2).
- [`08-caching-strategies-and-patterns.md`](08-caching-strategies-and-patterns.md): cold caches and
  stampedes after a restore (§6.3).
- [`35-reliability-math-slos-and-error-budgets.md`](35-reliability-math-slos-and-error-budgets.md):
  availability math, SLOs and error budgets behind the DR SLOs in §11 and the cost reasoning in §3.
- [`36-multi-region-active-active-and-geo-replication.md`](36-multi-region-active-active-and-geo-replication.md):
  the phased path from backup and restore through pilot light and warm standby to active-active
  (§1.3), replication topologies (§3), residency (§5), region evacuation,
  fencing and failback (§7). The active DR tiers of §3.1 live there.
- [`37-distributed-systems-debugging.md`](37-distributed-systems-debugging.md): finding when and
  where data went wrong across services, the investigation that precedes §9.1 step 3 and §9.2
  step 2.

### From databases/

- [`../databases/14-write-ahead-log-internals.md`](../databases/14-write-ahead-log-internals.md):
  WAL records and LSNs (§2, §3), checkpoints and full-page images (§6), segment lifecycle and
  `archive_command` (§8), WAL-based replication (§12): the machinery PITR replays.
- [`../databases/07-oltp-databases.md`](../databases/07-oltp-databases.md): PostgreSQL and MySQL
  logging (WAL, binlog, redo) and recovery models.
- [`../databases/12-replication-and-distributed-storage.md`](../databases/12-replication-and-distributed-storage.md):
  replication and failure handling in storage systems.
- [`../databases/19-distributed-databases-deep-dive.md`](../databases/19-distributed-databases-deep-dive.md):
  continuous backup and PITR in cloud-native databases (Aurora, Neon) and multi-region primitives.

### From kubernetes/

- [`../kubernetes/32-cluster-lifecycle-and-day2.md`](../kubernetes/32-cluster-lifecycle-and-day2.md):
  etcd snapshot versus Velero (§19), Velero architecture (§20), DR scenarios and procedures (§24).
- [`../kubernetes/19-storage-csi-pv-pvc.md`](../kubernetes/19-storage-csi-pv-pvc.md): backup
  integration with Velero and CSI snapshots (§20).
- [`../kubernetes/13-statefulset-deep-dive.md`](../kubernetes/13-statefulset-deep-dive.md): losing
  pod-0 and its volume (§24).
- [`../kubernetes/04-etcd-internals.md`](../kubernetes/04-etcd-internals.md): etcd snapshot and
  restore mechanics.
- [`../kubernetes/44-secrets-and-configmaps-deep-dive.md`](../kubernetes/44-secrets-and-configmaps-deep-dive.md):
  secrets encryption at rest and external secret managers, part of the key and secret recovery
  story (§6.5).

### From sre-observability/

- [`../sre-observability/36-dr-for-observability-stack.md`](../sre-observability/36-dr-for-observability-stack.md):
  RPO/RTO per telemetry signal and keeping observability alive during a regional failure.
- [`../sre-observability/38-continuous-verification.md`](../sre-observability/38-continuous-verification.md):
  game days versus continuous chaos (§6), the verification loop (§10), a worked drill (§12).
- [`../sre-observability/15-incident-response-and-postmortem.md`](../sre-observability/15-incident-response-and-postmortem.md):
  incident roles, the first 60 minutes, game days and tabletop exercises (§10), postmortems.
- [`../sre-observability/12-alerting.md`](../sre-observability/12-alerting.md): alert design and
  routing for the rules in §11.3.
- [`../sre-observability/23-database-observability.md`](../sre-observability/23-database-observability.md):
  database telemetry, including replication and WAL metrics.
- [`../sre-observability/27-security-observability.md`](../sre-observability/27-security-observability.md):
  detecting deletion of DR assets and attacker activity (§9.4, §11.2).
- [`../sre-observability/32-compliance-and-privacy.md`](../sre-observability/32-compliance-and-privacy.md):
  right to erasure (§6) and cross-border data flow (§8) for telemetry.

---

## References

1. PostgreSQL Documentation. *Continuous Archiving and Point-in-Time Recovery (PITR)*; *Recovery
   Target* configuration; `pg_basebackup`, `pg_verifybackup`, `pg_waldump`, `pg_amcheck`.
2. pgBackRest. *User Guide* and *Command Reference* (backup types, multiple repositories,
   retention, restore targets, `verify`). https://pgbackrest.org
3. WAL-G. *PostgreSQL documentation* (backup-push, backup-fetch, wal-verify, delete).
   https://github.com/wal-g/wal-g
4. Amazon Web Services. *Disaster Recovery of Workloads on AWS: Recovery in the Cloud*
   (whitepaper; backup and restore, pilot light, warm standby, multi-site active/active).
5. Amazon Web Services. *Amazon RDS User Guide*: backups, point-in-time recovery, cross-Region
   automated backup replication; *Amazon S3 User Guide*: versioning, Object Lock, replication;
   *AWS Backup Developer Guide*: cross-account copy and Vault Lock; *AWS KMS Developer Guide*:
   deleting keys, multi-Region keys.
6. Google Cloud. *Cloud SQL for PostgreSQL*: backups and point-in-time recovery.
7. MySQL Reference Manual. *Point-in-Time (Incremental) Recovery Using the Binary Log*.
8. Redis Documentation. *Redis persistence* (RDB, AOF).
9. Elastic. *Snapshot and restore*; OpenSearch *Snapshots*.
10. UK Information Commissioner's Office. *Right to erasure* guidance (backups, "beyond use").
11. Regulation (EU) 2016/679 (GDPR), Articles 17 and 33.
12. PCI Security Standards Council. *PCI DSS v4.0*, Requirements 3 and 9.
13. GitLab (2017). *Postmortem of database outage of January 31*. GitLab Blog.
14. Netcraft (2021). Reporting on the OVHcloud Strasbourg data center fire.
15. Atlassian (2022). *Post-Incident Review on the Atlassian April 2022 outage*.
16. UniSuper and Google Cloud (2024). Joint statement; Google Cloud (2024), *Details of Google Cloud
    VMware Engine incident affecting UniSuper*.
17. Greenberg, A. (2018). *The Untold Story of NotPetya, the Most Devastating Cyberattack in
    History*. Wired.
18. Kleppmann, M. (2017). *Designing Data-Intensive Applications*. O'Reilly. Chapters 5 and 11.
