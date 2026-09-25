# Chapter 36: Multi-Region Systems — Active-Active, Geo-Replication, Quorum Placement, and Region Evacuation

A Staff-level architecture and operations chapter on running one product in several cloud regions.
It covers when multi-region is worth it (and when it is not), the latency physics that constrain
every design, how replication topology and quorum placement decide what survives a region loss,
how to partition data by geography (home regions, residency, cells), how to handle conflicts and
global invariants in active-active systems, and how to actually evacuate a region without making
the outage worse.

Scope boundary. The internals are covered elsewhere and linked, not repeated: clocks (HLC,
TrueTime) in [`../databases/19-distributed-databases-deep-dive.md` §3](../databases/19-distributed-databases-deep-dive.md#3-time-clocks-and-ordering),
CRDT math in [§7](../databases/19-distributed-databases-deep-dive.md#7-conflict-resolution-and-crdts),
anti-entropy in [§8](../databases/19-distributed-databases-deep-dive.md#8-anti-entropy-and-repair),
and database-level multi-region primitives in [§12](../databases/19-distributed-databases-deep-dive.md#12-multi-region-and-geo-distribution).
Replication fundamentals (leader/follower, multi-leader, leaderless, lag, session guarantees, LWW,
version vectors) are in [`04-replication-and-consistency.md`](04-replication-and-consistency.md);
CAP/PACELC and quorum definitions in [`00-primitives-and-system-models.md`](00-primitives-and-system-models.md);
sagas, outbox and idempotency in [`06-distributed-transactions-sagas-outbox-idempotency.md`](06-distributed-transactions-sagas-outbox-idempotency.md).
This chapter is about the **multi-region angle**: which topology, where the replicas go, what it
costs in milliseconds and dollars, and how to operate it.

---

## Table of Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Why Go Multi-Region — and Why Not](#1-why-go-multi-region--and-why-not)
2. [Latency Physics and the Cross-Region Budget](#2-latency-physics-and-the-cross-region-budget)
3. [Cross-Region Replication Topologies](#3-cross-region-replication-topologies)
4. [Quorum Placement Across Regions](#4-quorum-placement-across-regions)
5. [Partitioning by Geography: Home Regions, Residency, Cells, Routing](#5-partitioning-by-geography-home-regions-residency-cells-routing)
6. [Active-Active Conflict Handling and Global Invariants](#6-active-active-conflict-handling-and-global-invariants)
7. [Region Evacuation and Failover Operations](#7-region-evacuation-and-failover-operations)
8. [Observability for Multi-Region Systems](#8-observability-for-multi-region-systems)
9. [Decision Matrix: Requirement to Architecture](#9-decision-matrix-requirement-to-architecture)
10. [Production Pitfalls / War Stories](#10-production-pitfalls--war-stories)
11. [Interview Questions — Multi-Region Systems](#11-interview-questions--multi-region-systems)
12. [Real-world cases — incidents with numbers](#12-real-world-cases--incidents-with-numbers)
13. [Key Takeaways](#key-takeaways)
14. [Cross-References](#cross-references)

---

## Start here — the whole chapter in plain words

**The problem.** A cloud region is a group of data centers in one metro area. Spreading a service
over several availability zones (AZs) inside one region survives a building losing power, but not
the whole region having a bad day, and regions do have bad days (§12). Running in several regions
fixes that and also puts servers closer to far-away users. But regions are tens to hundreds of
milliseconds apart, and that distance is set by the speed of light, not by engineering. Every
design choice in this chapter is a trade between three things: **how much data you can lose**
when a region dies, **how slow every write becomes**, and **how much it costs and how hard it is
to run**.

**A real-world example.** A B2B project-management SaaS has 3 million users: 55% in North America,
35% in Europe, 10% in Asia-Pacific. Peak traffic is 20,000 requests/s, 10% of them writes
(2,000 writes/s). Everything runs in `us-east-1` across 3 AZs. Its SLO is 99.95% per year
(about 4.4 hours of allowed downtime). (All numbers illustrative; RTTs are typical, not
guaranteed; §2.)

- **Today, one region, multi-AZ (§1).** It survives an AZ loss with no data loss. A single 4-hour
  regional outage spends about **91% of the yearly error budget** in one afternoon. European users
  pay about 75 ms per round trip to Virginia; a page that makes 4 sequential API calls pays
  **~300 ms of pure distance**.
- **Pilot light / warm standby in `us-west-2` (§1, §3).** An asynchronous database replica in a
  second region, lag p99 about 1 s. If `us-east-1` dies, the team promotes the replica and loses
  roughly **2,000 writes/s × 1 s ≈ 2,000 writes** (the RPO). Recovery takes 30–60 minutes for
  pilot light (compute must be scaled up) or 10–15 minutes for warm standby. Extra cost roughly
  15–40%.
- **Synchronous replication to `us-west-2` (§2, §3).** RPO drops to zero, but every commit now
  waits one cross-country round trip: **+65 ms per write**. A flow with 5 sequential writes gets
  **+325 ms**. And if `us-west-2` is unreachable, writes either stop or silently fall back to async.
- **Two regions cannot hold a quorum that survives either one (§4).** With 3 or 5 voters over two
  regions, one region always holds the majority; lose it and writes stop. A small third
  **witness** region fixes that.
- **Write-home, read-local (§5).** EU tenants get `eu-west-1` as their **home region**: their data
  lives there (a contract requirement) and their writes commit locally in about 2 ms. US tenants
  stay home in `us-east-1`. Only a small **global table** (tenant directory, login emails)
  replicates everywhere.
- **Global invariants (§6).** Usernames must be unique worldwide. Two regions accepting
  "alice" at the same moment is a conflict no merge can fix, so the username registry is a single
  consensus-backed table: ~70–150 ms per signup write, harmless at 5 signups/s.
- **Evacuation (§7).** With 3 active regions, each must run at **≤ 66% of its capacity** so the
  other two can absorb its traffic; with a 70% ceiling after failover, **≤ 47%**. Traffic moves in
  steps (10% → 25% → 50% → 100%) so cold caches and databases in the surviving regions are not
  flattened.
- **Observability (§8).** Replication lag gets its own SLO (p99 < 1 s), every region gets its own
  availability SLO, and the dashboards used during an evacuation do not live in the region being
  evacuated.

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Region | a cloud provider's cluster of data centers in one metro area (`us-east-1`) | a city |
| Availability zone (AZ) | one or more data centers in a region with separate power and network | a neighbourhood of that city |
| RPO (recovery point objective) | how much recent data you may lose, measured in time | "we may lose the last 5 seconds of typing" |
| RTO (recovery time objective) | how long until service is back | "the shop reopens within 1 hour" |
| Pilot light | a second region with data replicated but almost no compute running | a gas burner's pilot flame: small, but the big flame lights fast |
| Warm standby | a second region running the full stack at reduced size | a spare car with the engine idling |
| Active-passive | one region serves; another holds a full copy and takes over on failure | a co-pilot who flies only if the pilot is out |
| Active-active | several regions serve users at the same time | two checkout lanes both open |
| Home region | the one region that owns (accepts writes for) a given user, tenant or record | a person's home bank branch |
| Write-home, read-local | writes go to the owner region; reads are served from the nearest copy | you change your address at your branch; any branch can show your balance |
| Geo-replication | copying data between regions | photocopying files to a second office |
| Synchronous / asynchronous replication | the write waits (or does not wait) for the remote copy | waiting for the receipt vs posting a letter |
| Replication lag | how far behind a copy is | how many pages behind the photocopier is |
| Quorum | the majority of voters that must agree before a write counts | a committee's minimum attendance to vote |
| Witness / tiebreaker | a voter that holds no (or little) data, placed in a third site to break ties | an umpire with no team |
| Leader / leaseholder | the replica that orders writes (and serves consistent reads) for a piece of data | the chair of the committee |
| Follower read / bounded staleness | read a nearby copy that may be up to T seconds old | reading yesterday's newspaper at home instead of calling the newsroom |
| Data residency | a rule that some data must stay in a jurisdiction | medical files that may not leave the hospital |
| Cell | a complete, isolated copy of the stack serving a subset of customers | one self-contained branch office |
| GeoDNS / anycast | send each user to a nearby region by DNS answer / by routing one IP to many sites | a phone number that rings the nearest branch |
| Evacuation | moving all traffic out of a region on purpose | evacuating a building during a fire drill |
| Failover / failback | switch to the other region / switch back later | the co-pilot takes over / hands back control |
| Fencing | making sure the old owner can no longer write | changing the locks after a tenant leaves |
| Split brain | two regions both believe they own the same data and both accept writes | two people both think they are the house-sitter |
| LWW (last-writer-wins) | on conflict, keep the write with the latest timestamp, drop the other | the last note stuck on the fridge replaces the others |
| CRDT | a data type whose concurrent updates always merge cleanly (counters, sets) | tally marks from several people added together |
| Static stability | the surviving regions already have what they need, so recovery needs no new provisioning | keeping a full spare tank rather than planning to buy fuel during the storm |
| Game day / evacuation drill | a planned, real exercise of failing over | a fire drill with the real alarm |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| `d` | fiber path length between two sites | 500 – 20,000 km | Virginia ↔ Ireland ≈ 5,500 km great circle |
| `v_f` | speed of light in fiber (≈ c / 1.47) | ≈ 200 km/ms | 5,500 km → 27.5 ms one way |
| `RTT(a,b)` | round-trip time between regions a and b | 1 – 250 ms | `us-east-1` ↔ `eu-west-1` ≈ 70–85 ms |
| `RTT_floor` | physics lower bound `= 2d / v_f` = 1 ms per 100 km | — | 5,500 km → 55 ms |
| `k_path` | path inflation: real RTT / great-circle floor | 1.2 – 2.0 | 75 / 55 ≈ 1.4 |
| `n` | number of voting replicas | 3 or 5 | 5 |
| `Q` | majority quorum `= floor(n/2) + 1` | 2 or 3 | n = 5 → Q = 3 |
| `f` | voter failures tolerated `= n − Q` | 1 or 2 | n = 5 → 2 |
| `R` | number of regions used | 1 – 5 | 3 |
| `T_fsync` | local durable write time | 0.1 – 2 ms | 0.5 ms (NVMe) |
| `T_commit` | leader's commit time `= T_fsync + (Q−1)-th smallest leader→follower RTT` | 1 – 150 ms | 5 voters 2+2+1 → ≈ 65 ms |
| `T_write` | client-observed write latency `= RTT(client, leader) + T_commit` | — | EU client, US leader: 75 + 65 = 140 ms |
| `λ_w` | write rate | 100 – 100,000 /s | 2,000 writes/s |
| `L` | replication lag at the moment of failure | 0.05 – 30 s | p99 1 s |
| `W_lost` | writes lost on async failover `≈ λ_w × L` | — | 2,000 × 1 = 2,000 |
| RPO / RTO | tolerated data loss / tolerated downtime | 0 – hours | RPO 5 s, RTO 15 min |
| `TTL` | DNS record time-to-live | 30 – 300 s | 60 s |
| `N` | number of active regions sharing load | 2 – 5 | 3 |
| `u` | steady-state utilization of one region | 30 – 70% | 45% |
| `u_max` | highest utilization you accept right after losing a region | 70 – 90% | 70% |
| `u_safe` | highest steady-state utilization that still absorbs one region loss `= u_max × (N−1)/N` | — | 70% × 2/3 ≈ 47% |
| `h` | cache hit ratio | 0.8 – 0.99 | 0.95 warm, 0.3 cold |
| `ε` | clock uncertainty / skew bound | 1 – 250 ms | TrueTime ε: a few ms |
| `S` | staleness bound for follower reads | 1 – 10 s | "at most 5 s old" |
| `A_r` | availability of one region | 99.9 – 99.99% | 99.95% |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. Why Go Multi-Region — and Why Not

> **In plain words.** There are four honest reasons to run in more than one region: users far
> away are slow, a whole region can go down, the law or a contract says data must stay somewhere,
> and you want a failure to hurt fewer customers. There are also strong reasons not to: it
> roughly doubles the infrastructure bill, it makes every write either slower or riskier, and it
> multiplies the ways things break. Most products should climb a ladder one rung at a time and
> stop at the first rung that meets their real requirement.
>
> **Real-world example.** The SaaS from "Start here" does not need active-active on day one. A
> pilot-light copy in `us-west-2` (+15% cost) takes its region-loss RTO from "rebuild from
> backups, maybe a day" to "under an hour". An EU region is justified later by an enterprise
> contract that requires EU data residency, not by latency alone.

### 1.1 The four reasons to go multi-region

| Reason | What it buys | What it does **not** buy | Signal that it is real |
|---|---|---|---|
| **Latency** for distant users | Removes 70–250 ms per round trip for users on other continents | Nothing for users near the existing region; a CDN may already fix static content | p75 page load in the far market is measurably hurting conversion or retention |
| **Availability** beyond one region | Survives a full-region outage (rare, but often hours long when it happens) | Protection from global failures: bad config pushed everywhere, a global control plane, your own bad deploy (§12) | SLO ≥ 99.95% where one regional outage would blow the yearly budget |
| **Data residency / sovereignty** | Keeps regulated or contractually bound data in a jurisdiction | Compliance by itself: logs, backups, analytics, support tools leak data too (§5.3) | A signed contract, a regulator, or a procurement requirement names a jurisdiction |
| **Blast radius** | A region (or cell) failure hurts a fraction of customers | Isolation if the regions share a control plane, config pipeline or database | Large enterprise customers whose outage costs are individually material |

**Availability arithmetic that justifies (or kills) the project.** Take the running example's
99.95% SLO: `0.0005 × 8,760 h ≈ 4.38 h` per year. Public regional incidents at the big providers
have lasted from under an hour to more than half a day (§12). One 4-hour regional event spends
`4 / 4.38 ≈ 91%` of the budget. At 99.9% (8.76 h/year) the same event spends 46%, which many
products can absorb without a second region. At 99.99% (52.6 min/year), one multi-hour regional
outage breaks the SLO several times over, so **99.99% on one region is a promise you
cannot keep**; see [`35-reliability-math-slos-and-error-budgets.md`](35-reliability-math-slos-and-error-budgets.md)
§3 and §8 for the composition math.

### 1.2 The reasons not to

- **Cost.** Active-active across N regions provisions `N/(N−1)` times peak capacity (§7.4): 2x for
  two regions, 1.5x for three. Add cross-region data transfer (replication traffic is billed per
  GB between regions on the major clouds), duplicated managed services, and the people to run it.
  Pilot light is the cheap exception, which is why it is the usual first step.
- **Write latency or data loss, pick one.** Synchronous cross-region replication adds one
  cross-region RTT to every commit (§2). Asynchronous replication loses `λ_w × L` writes on failover
  (§3). No topology avoids both, for the same data, in the same moment (PACELC: in normal operation
  you trade latency against consistency; see [`00-primitives-and-system-models.md`](00-primitives-and-system-models.md)).
- **Complexity and new failure modes.** Split brain, conflicting writes, replication lag bugs,
  config drift between regions, a failover path that nobody has exercised in a year. An untested
  failover has a low success rate, and a failed failover turns a region outage into a longer one
  (35 §8: availability `= 1 − P(primary fails) × P(failover fails)`).
- **Global dependencies you forgot.** Identity provider, secrets manager, feature flags, container
  registry, DNS provider, the CI/CD system that would deploy the fix. If any of these lives in one
  region, your "multi-region" service has a single-region dependency (§10.2).

### 1.3 The phased path

Each rung solves a specific problem and has a specific cost. Climb only when a requirement forces
the next rung.

```
  RUNG 0            RUNG 1              RUNG 2             RUNG 3              RUNG 4                 RUNG 5
  single region  →  backup & restore →  pilot light     →  warm standby /   →  read-local,         →  active-active
  multi-AZ          cross-region        (data live,        active-passive      write-home             (every region
                    (backups copied)    compute off)       (compute on)        (per-tenant home)       writes its own
                                                                                                        data, all serve)
  ┌─────────┐       ┌─────────┐         ┌─────────┐        ┌─────────┐         ┌─────────┐            ┌─────────┐
  │ us-east │       │ us-east │         │ us-east │        │ us-east │         │ us-east │ US homes   │ us-east │
  │ 3 AZs   │       │ 3 AZs   │         │ 3 AZs   │        │ 3 AZs   │         │  (R/W)  │            │  (R/W)  │
  └─────────┘       └────┬────┘         └────┬────┘        └────┬────┘         └────┬────┘            └────┬────┘
                         │ snapshots         │ async repl       │ async repl        │ async repl           │ repl
                         ▼ (hours)           ▼ (seconds)        ▼ (seconds)         ▼ of global tables     ▼ both ways
                    ┌─────────┐         ┌─────────┐        ┌─────────┐         ┌─────────┐            ┌─────────┐
                    │ us-west │         │ us-west │        │ us-west │         │ eu-west │ EU homes   │ eu-west │
                    │ (cold)  │         │ DB only │        │ small or│         │  (R/W)  │            │  (R/W)  │
                    └─────────┘         └─────────┘        │ full    │         └─────────┘            └─────────┘
                                                           └─────────┘
```

| Rung | Region-loss RPO | Region-loss RTO | Extra cost (rough) | Complexity | Right answer when |
|---|---|---|---|---|---|
| 0. Single region, multi-AZ | Lose region = lose everything since last off-region backup (or everything) | Hours to days | Baseline | Low | SLO ≤ 99.9%, one market, no residency |
| 1. Backup & restore cross-region | Hours (backup interval) | Hours to a day (restore + rebuild infra from code) | +2–5% | Low | You need a region-loss story but can tolerate a long outage |
| 2. Pilot light | Seconds (async lag) | 30–60 min (scale compute from ~0, warm caches) | +10–20% | Medium | Region loss must be survivable within an hour |
| 3a. Warm standby | Seconds | 10–20 min (scale up a running stack) | +30–60% | Medium | RTO in minutes; budget below full duplication |
| 3b. Active-passive (hot standby) | Seconds (async) or 0 (sync, +RTT per write) | 2–10 min (promote + shift traffic) | +80–100% | Medium-high | RTO in minutes with confidence; data has one writer |
| 4. Read-local, write-home | Seconds for the home region's data | Minutes per region (promote that region's replicas elsewhere) | +50–100% | High | Distant users, residency, or blast radius per geography |
| 5. Active-active (all regions write) | 0 with cross-region consensus, seconds with async multi-leader | ~0 for stateless tiers; seconds to minutes for data | +50–100% plus engineering | Highest | 99.99%+ with global users, or writes that must be local everywhere |

Notes on the table:

- **RPO in seconds** means "equal to replication lag at the moment of failure", which is usually
  sub-second and occasionally minutes (bulk backfills, network congestion; §10.5). Put a lag SLO
  on it (§8), or the real RPO is unknown.
- **RTO** is dominated by decision time and by the steps that were not automated, not by the
  database promotion itself (which is seconds to a minute). The most common RTO killer is a
  human waiting for more evidence (§7.1).
- **Cost** percentages are compute-and-storage rough orders of magnitude; they exclude cross-region
  transfer and engineering time. Multi-region's engineering cost is the part that surprises teams.
- The rungs are not strictly sequential. **Read-local, write-home (rung 4) is often a better second
  region than active-passive (rung 3b)**, because both regions serve real traffic every day, so
  the "passive" region's configuration, capacity and runbooks do not rot.

**The simpler tier is often the right answer.** A B2B product at 99.9% with customers on one
continent should stop at rung 1 or 2. A consumer app with global users but no residency rules
often gets 80% of the latency win from a CDN, edge TLS termination and regional read replicas for
the heaviest read paths, without moving writes at all.

---

## 2. Latency Physics and the Cross-Region Budget

> **In plain words.** Light in glass fiber covers about 200 km per millisecond, so a round trip
> costs at least 1 ms per 100 km of cable, and real cables take detours. Nothing in software can
> beat that. Any write that must be confirmed in another region pays at least one such round
> trip, and any design that crosses regions several times per request pays it several times.
>
> **Real-world example.** Virginia to Ireland is about 5,500 km in a straight line: at least 55 ms
> round trip in fiber, 70–85 ms in practice. A European user whose request needs 4 sequential calls
> to a database in Virginia waits about 300 ms before the server has done any work.

### 2.1 The speed of light in fiber

Light travels at `c ≈ 299,792 km/s` in vacuum. Silica fiber has a refractive index of about 1.47,
so light in fiber travels at about `c / 1.47 ≈ 204,000 km/s ≈ 200 km/ms`. That gives the most
useful rule of thumb in this chapter:

```
one-way:    1 ms per 200 km of fiber
round trip: 1 ms per 100 km of fiber      RTT_floor = 2 × d / v_f
```

Real paths are longer than the great circle (cables follow coastlines, rail lines and landing
stations) and add switching, queuing and middlebox delay. Measured RTTs are typically 1.2–2x the
floor (`k_path`). Microwave and hollow-core fiber links shave some of this for trading firms; they
do not change cloud inter-region numbers.

### 2.2 Realistic inter-region RTTs

The "typical RTT" column is **illustrative**: approximate ranges seen between major cloud regions
at quiet times. They move with provider backbones, routing changes and congestion. Measure your
own (provider inter-region latency dashboards, or a mesh of probes) before designing to a number.

| Route (example regions) | Great-circle distance | Fiber floor RTT (`d/100`) | Typical RTT (illustrative) | `k_path` |
|---|---|---|---|---|
| Same AZ | < 1 km | ~0 | 0.1 – 0.5 ms | — |
| Cross-AZ, same region | 10 – 100 km | 0.1 – 1 ms | 0.5 – 2 ms | — |
| N. Virginia ↔ Ohio (`us-east-1` ↔ `us-east-2`) | ~480 km | ~5 ms | 10 – 15 ms | ~2–3 (short routes inflate most) |
| N. Virginia ↔ Oregon (`us-east-1` ↔ `us-west-2`) | ~3,500 km | ~35 ms | 60 – 75 ms | ~1.9 |
| N. Virginia ↔ Ireland (`us-east-1` ↔ `eu-west-1`) | ~5,500 km | ~55 ms | 70 – 85 ms | ~1.4 |
| Ireland ↔ Frankfurt (`eu-west-1` ↔ `eu-central-1`) | ~1,100 km | ~11 ms | 20 – 30 ms | ~2 |
| Oregon ↔ Tokyo (`us-west-2` ↔ `ap-northeast-1`) | ~8,000 km | ~80 ms | 95 – 115 ms | ~1.3 |
| N. Virginia ↔ Tokyo | ~10,900 km | ~109 ms | 145 – 170 ms | ~1.4 |
| Frankfurt ↔ Singapore (`eu-central-1` ↔ `ap-southeast-1`) | ~10,300 km | ~103 ms | 150 – 180 ms | ~1.6 |
| N. Virginia ↔ Singapore | ~15,500 km | ~155 ms | 210 – 240 ms | ~1.45 |
| N. Virginia ↔ Sydney | ~15,700 km | ~157 ms | 195 – 230 ms | ~1.35 |

Three consequences follow directly:

1. **Intra-continental regions are 10–75 ms apart; inter-continental ones 70–240 ms.** A
   synchronous design that is tolerable within North America (+65 ms per commit) is painful across
   the Atlantic and unusable across the Pacific for interactive writes.
2. **Latency is not a bandwidth problem.** A 100 Gbps link between Virginia and Ireland still takes
   ~75 ms to return an acknowledgment.
3. **Tail latency is worse than the table.** Cross-region paths see occasional loss and rerouting;
   TCP retransmission on a 75 ms path costs hundreds of milliseconds. Budget p99 at 1.5–3x the
   typical RTT for anything that must cross regions synchronously.

### 2.3 Round trips per request: the multiplier that matters

The distance is paid once per **sequential** round trip, not once per request.

```
EU user → app in us-east-1 → DB in us-east-1         (app and DB co-located)
  user ↔ app:   1 × 80 ms                  =  80 ms
  app ↔ DB:     10 queries × 1 ms           =  10 ms
  total network                              ≈  90 ms

EU user → app in eu-west-1 → DB in us-east-1         (app moved, DB not)  ← the classic mistake
  user ↔ app:   1 × 5 ms                   =   5 ms
  app ↔ DB:     10 queries × 80 ms          = 800 ms
  total network                              ≈ 805 ms   (9x worse than doing nothing)
```

The rule: **never put a region boundary between an application and its chatty dependencies.**
Move the whole request path (app, cache, database, or at least a read replica) together, and cross
regions at most once per request, ideally asynchronously.

Connection setup multiplies the RTT further. A fresh TCP connection costs 1 RTT; TLS 1.3 adds
1 more (TLS 1.2: 2 more), so the first byte of a cold HTTPS request to a region 80 ms away arrives
after about 3 RTTs = 240 ms. TCP slow start then needs several more RTTs to reach full throughput
on large responses. That is why global deployments terminate TLS at an edge close to the user and
keep long-lived, pre-warmed connection pools between regions
([`17-networking-protocols-and-communication.md`](17-networking-protocols-and-communication.md) §1, §3, §10).

### 2.4 The latency budget of a synchronous cross-region write

A client in region C writes a record whose leader is in region A, replicated synchronously to a
majority that includes another region.

```
T_write = RTT(C, A)                      client/app to the leader's region (0 if C = A)
        + T_fsync                        leader's local durable write
        + T_commit_wait                  (Spanner-style TrueTime only: ~2ε, a few ms; else 0)
        + (Q−1)-th smallest RTT(A, follower)   waiting for enough acknowledgments
        + T_apply                        applying and responding (sub-ms to ms)
```

Worked example (running example, illustrative RTTs: `us-east-1` = E, `us-west-2` = W,
`eu-west-1` = U; E–W 65 ms, E–U 75 ms, W–U 140 ms; `T_fsync` = 0.5 ms):

| Placement | Client region | `RTT(C,A)` | Commit wait for acks | `T_write` |
|---|---|---|---|---|
| 3 voters in E (3 AZs) | E | 0 | 2nd voter cross-AZ: ~1 ms | **~1.5 ms** |
| 3 voters in E (3 AZs) | U | 75 | ~1 ms | **~77 ms** |
| 1+1+1 in E, W, U; leader E | E | 0 | nearest of W, U: 65 ms | **~66 ms** |
| 1+1+1 in E, W, U; leader E | U | 75 | 65 ms | **~141 ms** |
| 1+1+1; leader moved to U (EU-heavy data) | U | 0 | nearest of E, W: 75 ms | **~76 ms** |

Two lessons. First, **commit latency is the RTT from the leader to the nearest remote majority**,
so the second-closest region to the leader sets your write floor. Second, **leader placement is a
latency knob**: moving the leader next to the writers removes the `RTT(C, A)` term entirely
(§4.4).

For Spanner-style commit wait and why it is only a few milliseconds with TrueTime, see
[`../databases/19-distributed-databases-deep-dive.md` §3.5 and §4.4](../databases/19-distributed-databases-deep-dive.md#3-time-clocks-and-ordering).

### 2.5 What the budget means for product design

| Operation | Typical latency budget | Can it afford a cross-region synchronous write? |
|---|---|---|
| Keystroke / cursor sync in a collaborative editor | < 50 ms | No: local write + async merge (CRDT or OT) |
| Interactive form save, "like", comment | 100 – 300 ms | Within a continent, maybe; across oceans, no |
| Checkout / payment authorization | 300 ms – 2 s | Yes for the one step that needs it (the charge), not for every step |
| Signup / username claim | 300 ms – 1 s | Yes; it is rare and needs a global invariant (§6.4) |
| Admin/config change | seconds | Yes |
| Background job, analytics | minutes | Async replication is fine |

Design principle: **decide per operation, not per system.** A product that pays 140 ms on the
2 operations per session that need global correctness, and 2 ms on everything else, feels fast.
One that pays 140 ms on every write does not.

---

## 3. Cross-Region Replication Topologies

> **In plain words.** There are five ways to keep copies in several regions. One region writes
> and ships changes later (fast, loses the last few seconds on failover). One region writes and
> waits for another to confirm (no loss, every write slower). A voting group spread over regions
> agrees on each write (no loss, survives a region, every write slower). Every region writes and
> they swap changes later (fast everywhere, but two regions can change the same thing). Or any
> replica takes writes and reads compare several copies (flexible, conflicts again). The right
> choice depends on which data it is, not on the company.
>
> **Real-world example.** The SaaS keeps project data in async primary-replica per home region,
> the username registry in a 5-voter consensus group across 3 regions, and "seen" counters as
> multi-leader CRDT counters. Three topologies in one product, each chosen per table.

The mechanics of each (log shipping, quorum math, lag, session guarantees) are in
[`04-replication-and-consistency.md`](04-replication-and-consistency.md). This section is only
about what each one means **across regions**.

### 3.1 The five topologies at a glance

```
(a) ASYNC PRIMARY → REPLICA            (b) SYNC / SEMISYNC                  (c) CONSENSUS ACROSS REGIONS
    ┌──────┐   log   ┌──────┐              ┌──────┐  wait ack ┌──────┐          ┌──────┐     ┌──────┐
    │ E: P │ ──────▶ │ W: R │              │ E: P │ ◀───────▶ │ W: S │          │ E: L │◀───▶│ W: F │
    └──────┘ (later) └──────┘              └──────┘  per txn  └──────┘          └──┬───┘     └──────┘
    commit = local                         commit = local + RTT(E,W)               │ majority of 3 or 5
                                                                                ┌──▼───┐
                                                                                │ U: F │
                                                                                └──────┘
                                                                         commit = RTT to nearest majority

(d) MULTI-LEADER (active-active async)  (e) LEADERLESS (Dynamo-style, multi-DC)
    ┌──────┐  both ways ┌──────┐           ┌──────┐   ┌──────┐   ┌──────┐
    │ E: L │ ◀────────▶ │ U: L │           │ E: r │   │ E: r │   │ U: r │ ...
    └──────┘  (later)   └──────┘           └──────┘   └──────┘   └──────┘
    each region commits locally;           coordinator writes to W replicas,
    concurrent writes to one key conflict  often LOCAL_QUORUM in-region + async to others
```

| Topology | Region-loss RPO | Write latency | Region-loss availability for writes | Conflicts | Examples |
|---|---|---|---|---|---|
| (a) Async primary-replica | `λ_w × L` (seconds of writes) | Local (ms) | After a promotion (manual or automated), minutes | None while one primary; split brain possible on bad failover | Postgres/MySQL cross-region replicas, Aurora Global Database, RDS cross-region read replicas |
| (b) Sync / semisync to a remote standby | 0 (if it never degrades) | Local + 1 RTT | Standby promotable with no loss; if standby region dies, primary must stall or degrade to async | None | Postgres `synchronous_standby_names`, MySQL semisync, SQL Server AGs in sync mode |
| (c) Consensus spanning regions | 0 | RTT to nearest majority (+ client→leader) | Automatic, seconds (new leader elected) as long as a majority survives | None (single serial order) | Spanner, CockroachDB, YugabyteDB, TiDB, etcd/ZooKeeper stretched across regions |
| (d) Multi-leader async | Seconds of writes unique to the lost region (recovered if it comes back) | Local | Immediate: other regions already take writes | Yes: needs LWW, CRDTs or app merge (§6) | DynamoDB Global Tables, Cosmos DB multi-region writes, MySQL/Postgres bidirectional replication tools, Redis Enterprise Active-Active |
| (e) Leaderless multi-DC | Depends on consistency level: `EACH_QUORUM` ≈ 0; `LOCAL_QUORUM` = seconds | `LOCAL_QUORUM`: local; `EACH_QUORUM`: slowest region's RTT | Immediate with `LOCAL_*` levels | Yes: per-cell LWW, read repair, anti-entropy | Cassandra/ScyllaDB `NetworkTopologyStrategy`, Riak |

### 3.2 Async primary-replica across regions

The default for relational databases and the backbone of rungs 2–4. Things that matter
specifically across regions:

- **RPO is `λ_w × L` at the moment of failure,** and `L` is largest exactly when things go wrong:
  a degrading network or a region under stress slows replication before it fails. A replica that
  runs at 200 ms lag on a normal day may be 10–30 s behind when the region falls over. The running
  example at 2,000 writes/s × 10 s is 20,000 lost writes, not 2,000.
- **Lost writes are not gone, they are stranded.** They sit in the old primary's log. If that
  region comes back, you can extract them (binlog/WAL position after the last replicated point)
  and replay or reconcile them (§7.3). If you rejoin the old primary as a replica with a tool
  that rewinds it (`pg_rewind`, MySQL clone), those writes are discarded unless captured first.
- **Replication is single-stream per database** in most engines. One region's primary replaying a
  heavy write burst serially can fall behind even with plenty of bandwidth. Parallel apply helps,
  but large transactions and DDL still serialize.
- **Read replicas in other regions give read-local for free,** with read-your-writes broken for
  users who just wrote in the home region. Route a user's reads to the home region for a few
  seconds after they write, or read with a session token that carries the LSN/GTID they need
  (details in [`04-replication-and-consistency.md`](04-replication-and-consistency.md)).

Managed variants move the replication below the database. Aurora Global Database replicates at
the storage layer; AWS documents typical cross-region lag under one second and a managed
cross-region switchover/failover. The architecture is still rung 3b: one writer region, RPO equal
to lag for an unplanned failover.

### 3.3 Synchronous and semisynchronous across regions

Every commit waits for at least one remote acknowledgment: `T_commit ≈ T_fsync + RTT(primary,
standby)`. With two regions this raises a question the vendor docs answer differently: **what
happens when the standby region is unreachable?**

| Behaviour on standby loss | Effect | Where you see it |
|---|---|---|
| Block writes until the standby returns | RPO 0 kept; the primary region's availability now depends on the standby region, so two regions are **less** available for writes than one | Postgres with a single named sync standby |
| Fall back to async after a timeout | Availability kept; RPO silently becomes "whatever lag builds up" | MySQL semisync: `rpl_semi_sync_master_timeout` (called `rpl_semi_sync_source_timeout` in newer versions) defaults to 10 s, then replication continues asynchronously |
| Wait for any 1 of several standbys (quorum commit) | Survives one standby region being down | Postgres `synchronous_standby_names = 'ANY 1 (w1, u1)'` |

The fallback-to-async row is the dangerous one: the system reports "synchronous replication" in
every architecture diagram while running async during exactly the incidents that matter. Alert on
the degradation (MySQL exposes semisync status variables; Postgres exposes `sync_state` in
`pg_stat_replication`), and count time spent degraded against the RPO objective (§8).

Semi-synchronous also only guarantees the standby **received** the log, not that it applied it.
Promotion must replay the received-but-unapplied tail before serving, which adds to RTO.

### 3.4 Consensus spanning regions

Raft/Paxos groups whose voters sit in different regions (Spanner, CockroachDB, YugabyteDB, TiDB
with placement rules). Properties that matter across regions:

- **RPO 0 and automatic failover** as long as a majority of voters survives. No human needs to
  promote anything; a new leader is elected in seconds (election timeouts across regions are set
  higher than in-region ones, e.g. seconds, to avoid spurious elections over a lossy WAN).
- **Write latency = RTT to nearest majority** (§2.4). Placement decides everything (§4).
- **The group is unavailable when a majority is unreachable.** A 2-region deployment cannot
  survive the loss of the majority region (§4.2).
- **Leaders move.** After a failover, the leader may now be in a region far from the writers,
  and every write pays `RTT(client, leader)` until leadership is moved back (lease preferences in
  CockroachDB, default leader region in Spanner).
- **Clocks matter for reads and transaction ordering, not for log safety.** Raft/Paxos agreement on
  the log does not depend on clocks, but lease-based local reads assume bounded clock drift,
  follower reads use closed timestamps, and external consistency relies on bounded uncertainty
  (Spanner's commit wait, CockroachDB's uncertainty intervals). See 19 §3 and
  [`03-consensus-raft-and-distributed-locking.md`](03-consensus-raft-and-distributed-locking.md) §7.

### 3.5 Multi-leader and leaderless across regions

Every region accepts writes locally and ships them to the others. This is the only topology that
gives **local write latency everywhere and immediate write availability after a region loss**,
and it pays with conflicts: two regions can modify the same key within one replication-lag
window. Section 6 is the multi-region playbook for that; the conflict mechanics themselves are in
[`04-replication-and-consistency.md`](04-replication-and-consistency.md) and
[19 §7](../databases/19-distributed-databases-deep-dive.md#7-conflict-resolution-and-crdts).

Leaderless stores (Cassandra, ScyllaDB) make the per-request trade explicit through consistency
levels in a multi-DC cluster:

| Consistency level | Waits for | Latency | Survives a region loss for this op |
|---|---|---|---|
| `LOCAL_ONE` / `LOCAL_QUORUM` | Replicas in the coordinator's DC only | Local | Yes; other DCs converge asynchronously (hints, repair) |
| `QUORUM` | Majority of **all** replicas across DCs | Cross-region RTT | Depends on replica counts per DC (same arithmetic as §4) |
| `EACH_QUORUM` (writes) | A quorum in **every** DC | Slowest DC's RTT | No: one DC down fails the write |
| `SERIAL` / `LOCAL_SERIAL` (lightweight transactions, Paxos) | Paxos across all DCs / within the local DC | Several RTTs / local | `SERIAL`: needs a global majority; `LOCAL_SERIAL`: linearizable only within one DC |

`LOCAL_SERIAL` is a common trap: a compare-and-set that is linearizable inside one DC can race
with the same compare-and-set in another DC. It does not make a global invariant safe (§6.4).

### 3.6 Choosing per table, not per system

| Data class | Typical choice | Why |
|---|---|---|
| Per-tenant business data (projects, documents, orders) | (a) async, per home region; or (c) consensus with home-region leader | One writer per tenant; RPO of seconds acceptable, or zero with consensus if the budget allows |
| Money, ledgers, entitlements | (c) consensus, or (a) with RPO accepted in writing and a reconciliation process | A lost payment is expensive; a lost "last seen" timestamp is not |
| Global uniqueness (usernames, emails, slugs) | (c) small consensus group or a single-home registry | Needs one serial order (§6.4) |
| Counters, presence, likes, carts | (d) multi-leader with CRDT types | Commutative; conflicts merge by construction |
| Reference data (product catalog, feature flags, config) | Single writer + async fan-out to all regions; read locally | Written rarely, read everywhere; see §10.2 on validating before fan-out |
| Sessions, caches | Regional, not replicated (rebuild on evacuation) or replicated best-effort | Losing them costs a re-login or a cache miss, not correctness |

---

## 4. Quorum Placement Across Regions

> **In plain words.** A voting group keeps working only while more than half its members can talk.
> Where you put the members decides which failures it survives and how long each vote takes. Put
> the majority in one region and votes are fast, but losing that region stops everything. Spread
> them evenly over three regions and you survive any one region, but every vote waits for a
> cross-region answer. With only two regions, no arrangement survives losing either one.
>
> **Real-world example.** A ledger with 3 voters, 2 in `us-east-1` and 1 in `us-west-2`, commits
> in ~1.5 ms and survives the loss of `us-west-2`, but a `us-east-1` outage leaves 1 of 3 voters:
> no quorum, no writes, for as long as the outage lasts.

Quorum sizes and the `R + W > N` overlap argument are defined in
[`00-primitives-and-system-models.md`](00-primitives-and-system-models.md); availability math for
placements is in [`35-reliability-math-slos-and-error-budgets.md`](35-reliability-math-slos-and-error-budgets.md)
§8. This section is about **where** the voters go.

### 4.1 Commit latency per placement

Illustrative RTTs as in §2.4: E–W 65 ms, E–U 75 ms, W–U 140 ms; a fourth and fifth region for the
5-region row: Ohio (O, E–O 12 ms, W–O 50 ms, U–O 85 ms) and Frankfurt (F, E–F 90 ms, U–F 25 ms,
W–F 150 ms, O–F 100 ms). Cross-AZ RTT 1 ms, `T_fsync` 0.5 ms. `Q−1` acknowledgments needed besides
the leader.

| # | Placement (voters per region) | n / Q | Leader | Acks needed | Commit (leader's region) | Survives AZ loss | Survives loss of any 1 region | After losing the leader's region |
|---|---|---|---|---|---|---|---|---|
| P1 | E:3 | 3 / 2 | E | 1 cross-AZ | **~1.5 ms** | Yes | No | Down |
| P2 | E:2, W:1 | 3 / 2 | E | 1 cross-AZ | **~1.5 ms** | Yes | Only W | Down (1 of 3 left) |
| P3 | E:1, W:1, U:1 | 3 / 2 | E | nearest remote = W | **~66 ms** | Yes | Yes | W+U: commit = RTT(W,U) = **~140 ms**, zero further tolerance |
| P4 | E:2, W:2, U:1 | 5 / 3 | E | local peer + nearest remote | **~66 ms** | Yes | Yes | W:2+U:1 = 3 = Q: commit ~140 ms, zero further tolerance |
| P5 | E:3, W:1, U:1 | 5 / 3 | E | 2 local peers | **~1.5 ms** | Yes | Not E | Down (2 of 5 left) |
| P6 | E:2, W:2, witness in O | 5 / 3 | E | local peer + O (12 ms) | **~13 ms** | Yes | Yes | W:2 + O:1 = 3: commit = RTT(W,O) ≈ 50 ms |
| P7 | E, O, W, U, F: 1 each | 5 / 3 | E | 2nd-nearest of {O 12, W 65, U 75, F 90} = W | **~66 ms** | Yes | Yes, and any 2 | Survives 2 region losses |

Reading the table:

- **P1 and P5 are fast because the quorum is inside one region.** P5's two remote voters hold the
  data and keep follower reads local in W and U, but they are not needed for commit, so they may
  lag, and a region-E loss leaves 2 of 5: no quorum. Worse, a committed write may exist only on
  the 3 E voters, so P5 is not even region-durable without extra care. This is exactly the shape
  of CockroachDB's `ZONE` survival goal (§4.5), and it is a legitimate choice when it is labeled
  honestly: "fast, survives zones, not regions".
- **P3, P4 and P7 pay one cross-region RTT per commit and survive a region.** P4 costs 5 copies
  but, unlike P3, still has an in-region spare, so a single node or AZ failure in E does not force
  the leader out of E. That is why five voters is the common production shape for region
  survival.
- **P6 is the "close tiebreaker" trick.** Putting a third, small site close to the leader's region
  (here Ohio, 12 ms from Virginia) cuts commit latency from ~66 ms to ~13 ms while still surviving
  any single region. The price: E and O are close, so they are more likely to share a disaster
  (same power grid, same fiber route, same weather). Choose tiebreaker sites that are near in
  latency but independent in failure modes.
- **After a region loss, latency changes and tolerance drops.** P3/P4 fall back to a W–U majority
  at ~140 ms with no remaining fault tolerance. Plan capacity and SLOs for the degraded mode, not
  only the healthy one (§7.4).

General formula for the leader's commit latency:

```
T_commit(leader in region A) = T_fsync + RTT_(Q−1)(A)

  where RTT_(k)(A) is the k-th smallest RTT from A to the OTHER voters (in-region voters count ~1 ms).

  Example P7, leader E, voters at {O:12, W:65, U:75, F:90}, Q−1 = 2:
     2nd smallest = 65 ms  →  T_commit ≈ 65.5 ms
```

### 4.2 Why two regions cannot survive a region loss with a majority quorum

Put `a` voters in region A and `b` in region B, `n = a + b`, majority `Q = floor(n/2) + 1`.

```
To keep writing after losing A:   b ≥ Q
To keep writing after losing B:   a ≥ Q
Both:                              a + b ≥ 2Q = 2 × (floor(n/2) + 1) > n     → impossible
```

So with two regions you get exactly one of these:

| Split | Behaviour | What it is |
|---|---|---|
| Majority in A (2+1, 3+2) | Survives B's loss; A's loss stops writes | Effectively single-region availability with a remote copy |
| Even split (2+2) | Q = 3 needs both regions for every write; losing either stops writes | Worse than one region for write availability |
| Manual override | Operator forces the minority side to become authoritative ("unsafe recovery", "force quorum") | Possible loss of acknowledged writes and split-brain risk if the other side is alive |

The fixes:

1. **A third site as tiebreaker (witness).** The witness votes and stores the log (or a subset),
   but serves no reads and is not eligible to lead. It can be a small footprint and can sit in a
   cheaper region. This is how Spanner's dual-region-plus-witness multi-region configurations,
   AWS's multi-Region strong consistency for DynamoDB global tables, and Aurora DSQL's
   multi-region clusters (two peered regions plus a witness region) avoid the two-region trap, as
   documented by their vendors.
2. **Accept manual promotion.** Two regions with async replication and a human decision (rung 3b)
   is a perfectly good design for RTO in minutes. It is honest about the fact that the decision
   is manual.
3. **Use a quorum-free design for that data.** Multi-leader with CRDTs, where a region can keep
   writing its own data without anyone's vote (§6).

The same argument applies to 2-AZ deployments inside one region: a stretched cluster over two
zones cannot survive the loss of the majority zone. Most regions have 3+ AZs for this reason.

### 4.3 Placement availability, quickly

From 35 §8 (99.9% per replica, 99.99% per region, illustrative): 2+1 over two regions gives about
**99.990%**, no better than one region, because the majority region is a single point of failure.
1+1+1 over three regions gives about **99.9996%**. The cheapest large jump in availability is not
more replicas, it is **making sure no single region holds a majority**.

### 4.4 Leader and leaseholder placement

Each consensus group (range, tablet, split) has one leader that orders writes and, with a lease,
serves linearizable reads without a quorum round (see 03 §7.3).

- **Put the leader where the writes come from.** Every write from another region pays
  `RTT(client, leader)` on top of the commit. For per-tenant data with a home region, pin the
  leader to the home region (CockroachDB lease preferences, Spanner default leader region,
  YugabyteDB preferred zones). Automatic "follow the workload" rebalancing helps when access
  shifts over the day, but pinning is predictable.
- **Put the leader's region next to the tiebreaker or another voter region.** The leader's
  second-nearest voter sets commit latency (§4.1). In a US-EU-AP triangle, a leader in the US with
  a voter in another US region commits in ~13–65 ms; a leader in AP whose nearest voter is in the
  US commits in ~100+ ms.
- **Linearizable reads from another region cost `RTT(client, leaseholder)`.** A read in U of data
  led from E costs ~75 ms even though a replica sits in U, because only the leaseholder knows the
  latest committed state without a quorum round.

### 4.5 Follower reads and bounded staleness

A local replica can serve a read without contacting the leader if the read is allowed to be
slightly stale. Two flavours:

| Read type | Guarantee | Latency | Use for |
|---|---|---|---|
| Exact staleness ("as of 5 s ago") | Consistent snapshot at `now − S` | Local | Dashboards, listings, search results, anything where "5 s old" is invisible |
| Bounded staleness ("at most 10 s old, as fresh as locally possible") | Snapshot at the freshest timestamp the local replica can prove it has fully applied, no older than `now − S` | Local (falls back to leader or errors if the replica is too far behind) | Read-mostly pages that want fresh data but must stay local |
| Linearizable (leaseholder) | Latest committed value | `RTT(client, leaseholder)` | Balances before a debit, "did my payment go through" |

The replica knows it is safe to serve a timestamp once the leader has promised no further writes
at or below it (closed timestamps in CockroachDB, safe time in Spanner). Details:
[19 §12.4](../databases/19-distributed-databases-deep-dive.md#12-multi-region-and-geo-distribution).
In the running example, project listing pages use 5 s-stale follower reads in every region (local,
~2 ms); the project detail page a user just edited reads from the leaseholder for 10 s after that
user's write (session stickiness), then goes back to follower reads.

### 4.6 A concrete example: CockroachDB survival goals and table localities

CockroachDB exposes the placement decisions of §4.1 as SQL, which makes it a good vocabulary even
if you use something else. (Behaviour summarized from its documentation; defaults can change
between versions, so check yours.)

**Survival goals (per database):**

| Goal | Voting replicas | Commit latency | Survives | Maps to |
|---|---|---|---|---|
| `SURVIVE ZONE FAILURE` (default) | 3 voters in the home region, spread across its zones; non-voting replicas in other regions for local stale reads | In-region (~ms) | An AZ loss | P5-like (quorum in one region) |
| `SURVIVE REGION FAILURE` | 5 voters spread so no region holds a majority, with the home region holding 2 so the leaseholder has a local peer; needs at least 3 regions | One cross-region RTT to the nearest other voter region | A full region loss | P4 |

**Table localities (per table):**

| Locality | Where the data's leader lives | Reads | Writes | Use for |
|---|---|---|---|---|
| `REGIONAL BY TABLE IN <region>` (default: primary region) | One region for the whole table | Fast in that region; stale follower reads elsewhere | Fast in that region | Tables used mostly from one region |
| `REGIONAL BY ROW` | Per row, via a hidden `crdb_region` column (by default the region of the gateway that inserted the row) | Fast in the row's home region | Fast in the row's home region | Per-user / per-tenant data: this is write-home, read-local in one statement |
| `GLOBAL` | Replicated for strongly consistent local reads in every region | Fast (consistent) everywhere | Slow: writes are pushed into the future and wait it out, typically hundreds of ms | Reference data read everywhere, written rarely (currencies, plans, config) |

```sql
ALTER DATABASE app SET PRIMARY REGION "us-east1";
ALTER DATABASE app ADD REGION "us-west1";
ALTER DATABASE app ADD REGION "europe-west1";
ALTER DATABASE app SURVIVE REGION FAILURE;           -- P4-style voters: pay ~1 RTT per commit

ALTER TABLE projects SET LOCALITY REGIONAL BY ROW;  -- each tenant's rows live in its home region
ALTER TABLE plans    SET LOCALITY GLOBAL;           -- read everywhere, written by admins only
```

For data residency, CockroachDB also offers restricted placement options that keep non-voting
replicas out of other regions, and "super regions" that confine a row's replicas to a group of
regions (e.g. EU only) even under `SURVIVE REGION FAILURE`. The general lesson: **residency and
region survival conflict when a jurisdiction has fewer than three regions.** You then need three
regions inside the jurisdiction, or accept zone survival for that data.

### 4.7 Spanner multi-region configurations, at a high level

Spanner offers regional configurations (read-write replicas in three zones of one region) and
multi-region configurations. The documented pattern for the continental multi-region
configurations is: **two read-write regions with two read-write replicas each, plus a witness
replica in a third region**, five voters in total, one read-write region designated as the default
leader region. Some configurations add read-only replicas in further regions to make stale reads
local there. Google publishes a higher availability SLA for multi-region instances than for
regional ones (99.999% vs 99.99% at the time of writing).

Commit latency follows §4.1 directly: Q = 3 of 5, so a leader needs its in-region peer plus one
more voter; the regions in a configuration are chosen near each other so that the "one more" is a
short hop. Intercontinental configurations keep the read-write replicas and leader on one
continent and put read-only replicas on others: writes stay continental, reads are local
everywhere, and a write from another continent pays the RTT to the leader.

### 4.8 Placement rules of thumb

1. **No failure domain you intend to survive may hold a majority.** Zone survival: no zone holds
   Q voters. Region survival: no region holds Q voters.
2. **Five voters over three regions (2+2+1) is the default region-survivable shape;** make the "1"
   a witness if storage cost matters, and put it near the leader's region but on independent
   infrastructure.
3. **The leader belongs with its writers;** its second-nearest voter sets commit latency.
4. **Serve reads locally with bounded staleness** by default; pay the leaseholder RTT only for
   reads that must be linearizable, and know them by name.
5. **Plan for degraded mode**: after a region loss, latency rises and fault tolerance can drop to
   zero; a second failure (a routine node restart) then stops writes. Freeze deploys and
   maintenance while degraded.

---

## 5. Partitioning by Geography: Home Regions, Residency, Cells, Routing

> **In plain words.** The easiest way to make many regions work is to make sure that, for any
> given piece of data, only one region is in charge of it. Give every user or company a home
> region. Their writes go there; their reads can be served from anywhere nearby. Data that truly
> belongs to everyone (the list of companies, the username registry) goes into a small global
> table. Then add a router that knows who lives where.
>
> **Real-world example.** The SaaS assigns each tenant a home region at signup: EU tenants
> `eu-west-1`, everyone else `us-east-1`. A German customer's documents are written and stored in
> Ireland; when that customer's employee travels to New York, reads come from the nearest copy
> allowed by the residency rules (for this customer: Ireland only, ~75 ms), and writes still go
> home.

### 5.1 Home region per user or tenant

Geographic partitioning is sharding (see [`10-sharding-and-consistent-hashing.md`](10-sharding-and-consistent-hashing.md))
where the shard key is chosen to match **who writes the data and where they are**.

| Partition key | Good when | Watch out for |
|---|---|---|
| Tenant (company) | B2B: a tenant's users are mostly in one geography; most queries are tenant-scoped | Global enterprises with users on every continent: some users always pay the RTT to home |
| User | B2C: each user's data is mostly their own | Interactions between users in different homes (messages, follows, shared docs) cross regions |
| Resource (document, channel, game room) | Collaboration: home the object where most of its editors are | Objects whose audience moves; re-homing needs a migration |
| Merchant / seller | Marketplaces | Buyers are global; the order write belongs to one side, so pick the side that owns the invariant (inventory: seller) |

**Choosing and changing the home.**

- At signup: from the billing address, the contract's residency clause, or the signup IP, in that
  order of authority. Record the home explicitly in the directory (§5.4); never derive it at
  request time from where the request came from.
- Re-homing a tenant is a **data migration**, not a config change: copy, replicate the tail,
  verify, then atomically flip the directory entry with a brief write freeze for that tenant. It
  is the same playbook as moving a logical shard (10 §6.3 and §6.5). Build it early; you will need
  it for rebalancing, residency changes, and evacuation of a single noisy tenant.
- Cross-home interactions (a US user comments on an EU tenant's document) are **writes to the
  owner's home**. The US user pays one RTT for that write; the comment is stored in the EU. Do not
  try to "write locally and sync" data that belongs to someone else's home unless it is a
  commutative type (§6.3).

### 5.2 Write-home, read-local

```
┌───────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ global directory: tenant -> home region. Small, replicated to every                                   │
│ region, read locally; written rarely (consensus, or single writer + fan-out)                          │
└───────────────────────────────────────────────────────────────────────────────────────────────────────┘

 EU user (tenant T1, home = EU)                         US user (tenant T2, home = US)
         │                                                      │
         ▼                                                      ▼
 ┌─ eu-west-1 ────────────────────────────────────┐     ┌─ us-east-1 ────────────────────────────────────┐
 │ edge/LB -> app                                 │     │ edge/LB -> app                                 │
 │   T1 write -> EU primary (local, ~2 ms)        │     │   T2 write -> US primary (local, ~2 ms)        │
 │   T1 read  -> EU primary/replica (local)       │     │   T2 read  -> US primary/replica (local)       │
 │   T2 read  -> replica of US-homed data,        │ <-> │   T1 read  -> forwarded to eu-west-1 (EU data  │
 │               stale <= S (if residency allows) │     │               has no replica outside the EU)   │
 │   T2 write -> forwarded to us-east-1 (+75 ms)  │ <-> │   T1 write -> forwarded to eu-west-1 (+75 ms)  │
 │ EU-homed data: primary here; DR replica in     │     │ US-homed data: primary here; async replicas    │
 │   eu-central-1 (stays inside the EU)           │     │   in us-west-2 (DR) and eu-west-1 (reads)      │
 └────────────────────────────────────────────────┘     └────────────────────────────────────────────────┘
```

Properties:

- **No write conflicts on tenant data by construction:** each record has exactly one writable home
  at a time (§6.1). This is why write-home is usually preferable to multi-leader.
- **Reads are local and bounded-stale** for everyone; read-your-writes for the writer is handled by
  routing that user's reads home for a few seconds after a write, or by session tokens (§4.5).
- **Each region's DR copy lives in another region.** For EU-homed data with strict residency, the
  DR region must also be in the EU (e.g. `eu-central-1`), which is why residency pushes you toward
  at least two regions per jurisdiction.
- **Evacuating a region = promoting its homed data elsewhere** (§7): the replicas of EU-homed data
  in `eu-central-1` are promoted and the directory entry for all EU tenants flips.

### 5.3 Data residency pinning

Residency means the **primary copy, every replica, every backup and every derived copy** of some
data stays in a jurisdiction. Legal framing varies: the GDPR restricts transfers of personal data
out of the EEA (they need an adequacy decision or safeguards such as standard contractual clauses)
rather than mandating EU storage outright, but contracts, sector regulators and procurement
questionnaires frequently demand in-region storage anyway. Treat the requirement as a list of data
classes and allowed locations agreed with legal, not as an engineering guess.

Where residency leaks in practice (each one has shipped EU personal data to a US region in some
real system):

| Leak path | Fix |
|---|---|
| Async replicas / DR copies in another jurisdiction | DR region inside the jurisdiction; placement constraints enforced by the database (§4.6) |
| Backups and snapshots copied to a "central" bucket | Per-jurisdiction backup buckets; bucket policies deny cross-region copy |
| Application logs and traces containing payloads, emails, IPs | Scrub at the collector; per-region telemetry backends ([`../sre-observability/32-compliance-and-privacy.md`](../sre-observability/32-compliance-and-privacy.md), [`../sre-observability/33-federated-multi-region.md`](../sre-observability/33-federated-multi-region.md)) |
| Search indexes, caches, analytics warehouses, ML feature stores built centrally | Build them per region, or feed them only pseudonymized/aggregated data |
| Global tables that "just hold the email for login" | Global tables hold opaque IDs and the home-region pointer; the email lives in the home region, with a global **hash** of it for uniqueness (§6.4) |
| Support and admin tools querying all regions from one place | Tools query through the home region's API; access is logged per region |
| Message queues and event buses mirrored globally | Topic-level routing rules; mirror only non-personal events |

### 5.4 Global vs regional tables

Every schema in a multi-region system splits into two kinds of tables:

| | Regional (homed) tables | Global tables |
|---|---|---|
| Content | Tenant/user business data | Directory (tenant → home), username/email-hash registry, plans, feature flags, currency tables |
| Size | Almost all of the data | Small (MB–GB) |
| Write rate | High | Low (signups, admin changes) |
| Writes | Local in the home region | Cross-region: consensus (slow, correct) or single writer + fan-out (fast reads, one write region) |
| Reads | Local in home; bounded-stale elsewhere | Local everywhere |
| Failure mode | Home region down → that region's tenants degraded until promotion | Global table unavailable → signups/admin fail everywhere; **reads must keep working from the local copy** |

Keep the global set **small and read-mostly**. Every table promoted to "global" becomes a
cross-region write path and a shared dependency that couples the regions' failure modes. A good
test: if this table's write path were down for an hour, could existing users keep working? If
not, it must not be global, or its reads must be served from a local, possibly stale copy.

### 5.5 Cells inside regions

Regions are a coarse blast-radius unit: losing one takes out a third or half of customers. Cells
cut it finer. A **cell** is a complete, independent copy of the stack (compute, database, cache,
queues, config) serving a subset of tenants; a thin **cell router** maps tenants to cells. The
mechanics, and shuffle sharding as the lighter-weight alternative, are in
[`34-adaptive-load-control-and-backpressure.md` §9.5](34-adaptive-load-control-and-backpressure.md#95-shuffle-sharding-and-cells-limiting-the-blast-radius).

```
  global cell router / directory:  tenant → (home region, cell)
         │
  ┌──────┴──────────── us-east-1 ───────────────┐   ┌────────────── eu-west-1 ──────────────┐
  │ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ │   │ ┌────────┐ ┌────────┐ ┌────────┐       │
  │ │ cell 1 │ │ cell 2 │ │ cell 3 │ │ cell 4 │ │   │ │ cell 5 │ │ cell 6 │ │ cell 7 │       │
  │ │ app+DB │ │ app+DB │ │ app+DB │ │ app+DB │ │   │ │ app+DB │ │ app+DB │ │ app+DB │       │
  │ └────────┘ └────────┘ └────────┘ └────────┘ │   │ └────────┘ └────────┘ └────────┘       │
  └─────────────────────────────────────────────┘   └───────────────────────────────────────┘
     each cell's DB replicates to a partner cell in another region (or another zone set)
```

How cells and regions compose:

- **Cells bound the blast radius of your own changes** (bad deploy, bad migration, poison tenant):
  deploy cell by cell. Regions bound the blast radius of the provider's failures. You want both;
  cells usually come first because self-inflicted incidents are far more frequent than regional
  outages.
- **A cell is the unit of evacuation and re-homing.** Moving one cell's tenants to its partner
  cell is a smaller, rehearsable version of a region evacuation (§7.9).
- **Size cells so that one cell's capacity fits in the headroom of others** (§7.4 applies at cell
  level too).
- **Nothing shared** between cells except the router and the directory, and those must degrade to
  "serve from the last known mapping" when their own write path fails.

### 5.6 Routing users to regions

| Mechanism | How it picks a region | Failover speed | Caveats |
|---|---|---|---|
| **GeoDNS / latency-based DNS** | DNS answer depends on the resolver's location (or the client subnet, via EDNS Client Subnet if supported) or measured latency | TTL + client caching: seconds to many minutes (§7.2) | Resolver location ≠ user location (corporate VPNs, public resolvers); long-lived connections never re-resolve |
| **Anycast** | One IP announced via BGP from many sites; the network delivers to the "nearest" by routing policy | Seconds (BGP withdrawal) | Nearest by BGP, not by latency; mostly for edge/CDN/DNS/global LB front doors, not for your region's origin directly |
| **Global load balancer (anycast front door + health checks)** | Edge PoP near the user terminates TLS, forwards to a healthy region by latency/weights | Seconds (health-check driven), independent of client DNS caching | The global LB's own control plane and config are a shared dependency (§12) |
| **Client-side region list** | Mobile/desktop client holds a list of regional endpoints, pins to one, fails over on errors | As fast as the client's retry policy | Needs careful retry budgets to avoid herds; stale app versions keep old lists |
| **Application-level home routing** | Any region receives the request, looks up the tenant's home in the directory, serves reads locally and forwards writes home | Immediate after the directory flips | The forwarding hop costs one RTT; directory must be local and cached |

In practice these stack: an anycast global front door picks a nearby healthy region; the app
there looks up the tenant's home and either serves locally or forwards. The front door decides
**where the request lands**; the directory decides **where the data lives**. Evacuation changes
the first; promotion and re-homing change the second.

DNS behaviour and TTL mechanics are in
[`17-networking-protocols-and-communication.md`](17-networking-protocols-and-communication.md) §8.

---

## 6. Active-Active Conflict Handling and Global Invariants

> **In plain words.** If two regions can change the same thing at the same time, one day they
> will, and something has to decide what the final value is. The best fix is to arrange things so
> that it can't happen (each thing has one home). Next best is data that merges cleanly, like
> counters and sets. Timestamps ("latest wins") are the last resort, because clocks disagree and
> a correct write can silently vanish. And rules like "usernames are unique" or "stock never goes
> below zero" cannot be merged after the fact at all: they need one place, or a vote, to decide.
>
> **Real-world example.** A shopping cart replicated in two regions: a user adds a charger on
> their phone (routed to `us-east-1`) and removes a cable on their laptop (routed to `us-west-2`)
> within the same second. With whole-item LWW the cart ends up as one of the two versions and one
> action is lost; with an add/remove set CRDT both actions survive.

The mechanics (LWW, version vectors, siblings, CRDT definitions) are in
[`04-replication-and-consistency.md`](04-replication-and-consistency.md); the math of CRDTs is in
[19 §7](../databases/19-distributed-databases-deep-dive.md#7-conflict-resolution-and-crdts).
This section is the architecture playbook, in the order you should reach for the tools.

### 6.1 First: avoid conflicts by design

A conflict needs two writers of the same key inside one replication window. Remove one of the two
conditions:

| Technique | How it avoids conflicts | Cost |
|---|---|---|
| **Single home per record** (§5) | Only the home region writes; other regions forward | Non-home writers pay one RTT |
| **Single writer per field group** | Split a record so each part has one owner (profile → owned by user's home; moderation flags → owned by the moderation region) | Schema discipline |
| **Append-only / event-sourced data** | Each region appends events with globally unique IDs; no updates in place, so no overwrites | Readers must fold events; compaction |
| **Region-scoped identifiers** | IDs embed region bits (Snowflake-style) or are random (UUIDv4/UUIDv7), so inserts never collide | Avoid database auto-increment across regions (or use offset/increment pairs as a stopgap) |
| **Sticky routing per user** | A user's requests go to one region at a time, so their own writes do not race | Breaks for multi-device users during routing changes; not a guarantee |

Sticky routing reduces conflicts; only ownership **eliminates** them. Use sticky routing to make
the rare remaining conflicts rarer, not as the correctness mechanism.

### 6.2 Last-writer-wins and its data-loss pitfalls across regions

LWW keeps the write with the highest timestamp and **silently discards the other**. It is the
default conflict rule in several multi-region stores: DynamoDB global tables reconcile concurrent
updates to the same item with last-writer-wins; Cassandra applies LWW per cell (column) using
write timestamps. It is acceptable when losing one of two concurrent writes is harmless (a "last
seen" timestamp, a user's theme preference). It is dangerous when it isn't, for three reasons that
are all worse across regions:

1. **Clock skew picks the winner, not real time.** Region clocks are typically within milliseconds
   with NTP or a provider time service, but hosts do drift and step (29 §7, §10.3). Example:
   a user changes their shipping address in `eu-west-1` at real time 10:00:00.100 (EU host clock
   correct), then corrects a typo 50 ms later via `us-east-1`, whose host clock is 200 ms slow and
   stamps 09:59:59.950. LWW keeps the **older, wrong** address. No error, no log line.
2. **The window is the replication lag, not the request time.** Two writes 800 ms apart in
   different regions are "concurrent" if replication takes 1 s. Across regions, the conflict
   window is 100x wider than inside one.
3. **Granularity silently widens the loss.** Whole-item LWW turns "EU changed the name, US changed
   the email" into "one of the two changes is lost". Per-attribute LWW (Cassandra cells) keeps
   both, but can produce a row that no single writer ever wrote (name from one version, email from
   another).

Mitigations if you must use LWW:

- Use **hybrid logical clocks** so causally-later writes get later timestamps even with skew
  (19 §3.4); bound skew and alert on it.
- LWW **per field**, not per record, for independent fields.
- **Conditional writes are region-local.** A "write only if version = 7" condition in a multi-leader
  store is typically checked against the local replica only, so two regions can both pass it.
  DynamoDB documents that its transactions on global tables are ACID only in the region where they
  are issued, and concurrent writes in other regions are resolved by LWW afterwards.
- Log discarded versions (some stores expose them via CDC) and reconcile the ones that matter.

### 6.3 Version vectors and siblings; CRDTs for specific types

When losing a write is not acceptable and the data is not owned by one region:

- **Version vectors + siblings.** The store detects concurrency (neither version descends from the
  other) and keeps both as siblings; the application merges on read. Amazon's Dynamo paper used
  this for the shopping cart, and reported the classic side effect: a merge that unions items can
  **resurrect deleted items**. Siblings push complexity into every reader; use them for a small
  number of high-value record types.
- **CRDTs for types with a natural merge.** Use them where concurrent operations commute:

| Data | CRDT (practical choice) | Behaviour under concurrent cross-region writes |
|---|---|---|
| View / like / download counts | G-counter or PN-counter (per-region slots summed) | All increments counted; no lost updates |
| Presence, "users who reacted", tags | OR-set (observed-remove set) | Concurrent add and remove of the same element: the add survives unless the remove had seen it |
| Shopping cart lines | OR-map of item → PN-counter quantity | Adds and removes both survive; quantity can need a floor at 0 in the read path |
| Independent profile fields | Map of per-field LWW registers | Fields merge independently; within one field, LWW rules still apply |
| Collaborative text / JSON documents | Sequence/JSON CRDTs (RGA-family, Yjs, Automerge) or server-side OT | Concurrent edits interleave deterministically |

Managed options include Redis Enterprise Active-Active (CRDT-based types) and Cosmos DB's custom
merge policies; many teams implement counters and sets themselves on top of a multi-leader store
by giving each region its own slot. Two cautions from 19 §7.5 that bite especially across regions:
CRDTs **cannot enforce invariants** (two regions can each decrement stock to 0 from 1, giving −1),
and tombstones/metadata grow with the number of regions and removes.

### 6.4 Uniqueness and global invariants need a single home or consensus

Some rules are about **all** writes, not one record: "no two accounts share a username", "no more
than 1,000 tickets sold", "balance never below zero", "a seat is booked once". No merge function
can repair a violation after both regions have told users "yes". These need a serialization point:

| Invariant | Design | Latency cost | Failure behaviour |
|---|---|---|---|
| Unique username / email | A **global registry** keyed by the normalized name (or its hash, for residency), in a consensus group across 3+ regions (§4) or with a single write-home region | One cross-region commit per signup/rename, ~70–150 ms | Registry unavailable → signups and renames fail; logins still work from local copies |
| Inventory / ticket count | **Single home per SKU/event** (seller's or event's region); or **escrow**: split the stock into per-region allotments (500 US, 300 EU, 200 AP), each region sells locally from its allotment, a coordinator rebalances unused allotment | Home: one RTT for remote buyers. Escrow: local until an allotment runs low | Home down → that SKU unsellable (or sold from a pre-allocated regional allotment) |
| Account balance ≥ 0 | **Home region per account**; debits run there with a local transaction; transfers between accounts homed in different regions become a saga with reservations and compensations ([`06-distributed-transactions-sagas-outbox-idempotency.md`](06-distributed-transactions-sagas-outbox-idempotency.md)) | Local for same-home transfers; cross-region transfers are asynchronous multi-step | Home down → that account's debits pause (credits can queue) |
| Seat / slot booking | Home per venue/resource; or conditional write in a consensus-backed store | One RTT for remote bookers | Home down → bookings for that venue pause |
| Idempotency keys (exactly-once side effects) | Keys stored in the **home region of the resource they protect**, so a retry landing in another region is checked in the same place | Same as the write | A retry during failover can double-apply if the key store failed over with lag; include key tables in the RPO discussion (06) |

Two design notes:

- **Escrow turns a global invariant into many local ones.** "Total sold ≤ 1,000" becomes "each
  region sold ≤ its allotment", which each region can check alone. The coordinator only moves
  unused allotment between regions, which is rare and can be slow. The same idea underlies quota
  and rate-limit sharding across regions.
- **Keep the global serialization point off the hot path.** Signups are a few per second; logins
  are thousands. Reserve the name globally at signup, then everything else about the account is
  regional.

### 6.5 Two real systems, in multi-region terms

**DynamoDB global tables** (default multi-Region eventual consistency mode): every replica region
accepts writes; changes replicate asynchronously (AWS describes propagation as typically within
about a second); concurrent writes to one item are resolved by last-writer-wins; transactions and
conditional writes are evaluated in the issuing region. It is topology (d). Use it for data that
is per-user and region-sticky, or naturally LWW-safe; route writes for a given item to one region
if lost updates matter. AWS has also added a multi-Region **strong** consistency mode for global
tables that uses a three-region (or two-plus-witness) quorum; check its current constraints
(supported regions, feature limitations) before designing on it.

**Cassandra multi-DC:** replication per DC via `NetworkTopologyStrategy` (e.g. RF 3 in each DC),
`LOCAL_QUORUM` reads and writes for local latency, hinted handoff and repair to converge the
other DCs, per-cell LWW for conflicts, `SERIAL` (cross-DC Paxos) for the rare compare-and-set
that must be globally linearizable. The operational burden across regions is anti-entropy: run
repair on schedule and within `gc_grace_seconds`, or deleted data can reappear after a DC was
partitioned longer than that window (19 §8).

---

## 7. Region Evacuation and Failover Operations

> **In plain words.** Having a second region is not the same as being able to use it. You have to
> notice the problem, decide to move (people are slow to decide, automation can move too
> eagerly), steer users away while their devices still remember the old address, promote the copy
> of the data (and accept losing whatever had not been copied yet), make sure the old region
> cannot keep writing, and have enough spare capacity and warm caches on the other side. Then,
> later, move back. None of this works reliably unless you practise it.
>
> **Real-world example.** The SaaS evacuates `eu-west-1` during a regional networking incident.
> Stateless traffic moves to `eu-central-1` in 4 steps over 20 minutes. EU tenant databases are
> promoted in `eu-central-1` at minute 25 after the team decides the region will not recover soon;
> about 1,400 writes (700 writes/s of EU traffic × 2 s of lag) are stranded in Ireland and
> reconciled two days later.

### 7.1 Detection vs decision

Detection is a measurement problem; the decision is a judgement about the cost of acting on a
possibly wrong measurement (the same trade as failure detection inside a cluster, in
[`29-failure-detection-phi-accrual.md`](29-failure-detection-phi-accrual.md) §1, at a much larger
scale). Separate them, and separate the **reversible** action from the **irreversible** one:

| Action | Reversible? | Cost if the call was wrong | Who decides |
|---|---|---|---|
| Drain stateless traffic away from a region (front door weights, DNS) | Yes, in minutes | Some latency for moved users; load on survivors | Automation with conservative thresholds, or on-call with one command |
| Stop background/batch work in the region | Yes | Delayed jobs | Automation |
| Promote replicas of homed data in another region | **Hard to reverse**: stranded writes, split-brain risk, failback later | Lost writes (`λ_w × L`), reconciliation, a second risky switch to fail back | Humans, with pre-agreed criteria and a clock |
| Re-home tenants permanently | Hard | Migration effort | Planned work, not incident response |

**Detection signals** (use several; one source lies):

- Per-region SLIs measured **from outside the region**: synthetic probes from at least 3 other
  regions or external vantage points (see [`../sre-observability/29-synthetic-monitoring.md`](../sre-observability/29-synthetic-monitoring.md)).
- Real-user error and latency rates per serving region.
- Replication lag and replication-link errors out of the region.
- Provider status pages: useful confirmation, but historically slow and sometimes themselves
  impaired (§12.2).

**Decision rules that avoid flapping:**

- **Corroboration:** act only when at least 2 independent signal sources agree (e.g. external
  probes from 2 of 3 vantage points **and** real-user error rate).
- **Duration:** a sustained condition (e.g. error rate > 5% for 3–5 minutes), not a single spike.
- **Hold-down:** once a region is drained, it stays drained for a minimum period (e.g. 1 hour)
  regardless of recovery signals; return is a manual, stepped decision (§7.8).
- **No automatic failback,** ever. Automatic failover plus automatic failback on a flapping
  network is how one incident becomes five.
- **A decision clock for promotion,** agreed before the incident: "if the region is not healthy
  within 15 minutes of declaring the incident and there is no credible ETA under 15 more minutes,
  promote". Without a clock, teams wait for certainty that never comes.

Why the clock matters, with the running example's numbers (illustrative): promotion and traffic
shift take `T_p` ≈ 10 minutes. If the regional incident lasts 3 hours, waiting for recovery costs
180 minutes of EU-tenant outage; promoting at minute 15 costs 15 + 10 = 25 minutes plus
reconciliation of the stranded writes. If the incident lasts 12 minutes, the clock never fires and
nothing irreversible happened. The GitHub 2018 incident (§12.1) shows the opposite failure:
automation promoted across regions after a 43-second partition, and the cost was a day of
degraded service.

### 7.2 DNS TTL and client caching realities

DNS-based failover is only as fast as the slowest cache between you and the client:

| Layer | Behaviour | Effect on evacuation |
|---|---|---|
| Authoritative TTL | You set it (30–300 s typical for failover records) | Lower bound on the switch time for well-behaved resolvers |
| Recursive resolvers | Usually honour TTL; some enforce a minimum TTL or serve stale answers when the authoritative server is slow | A tail of clients sees the old answer beyond the TTL |
| OS and runtime caches | E.g. the JVM caches successful lookups per `networkaddress.cache.ttl` (30 s by default without a security manager; forever with one on older setups) | Services on misconfigured JVMs never move |
| Long-lived connections (HTTP keep-alive, HTTP/2, gRPC, WebSockets, DB pools) | **Never re-resolve** while the connection is open | A degraded-but-reachable region keeps receiving traffic on existing connections indefinitely |
| Mobile apps | Their own caches, retries, and occasionally hard-coded endpoints | Stragglers for hours; old app versions forever |

Practical consequences:

- **Set failover records' TTL to 30–60 s before you need it.** Lowering a 1-day TTL during an
  incident does nothing for clients that cached it yesterday.
- **Drain connections from the server side:** cap connection age (e.g. gRPC's
  `MAX_CONNECTION_AGE` of a few minutes), and during evacuation send HTTP/2 `GOAWAY` or
  `Connection: close` so clients reconnect and re-resolve. Evacuation time ≈ TTL + max connection
  age + client retry delay, not TTL alone.
- **Prefer an anycast global front door** for user traffic so the switch happens in the provider's
  network in seconds, independent of client DNS behaviour. Keep DNS failover as the backup path,
  and remember the front door's control plane is itself a shared dependency.
- **A hard-down region evacuates itself faster than a gray one:** connections to a dead region
  fail and clients reconnect; connections to a slow region stay open and keep suffering. Gray
  regional failures need explicit server-side draining.

### 7.3 Promoting replicas and replication-lag data loss

Promotion of an async replica in another region:

```
 t0  region E impaired; replica in W last applied position P_W; E's primary had reached P_E
 t1  decision to promote (7.1)
 t2  fence E's primary (7.5)                       ← must not be skipped
 t3  promote W's replica: replay received log, open for writes
 t4  flip the directory / routing: E-homed tenants → W
 t5  verify: writes succeed, lag of the NEW replicas (if any) is healthy

 stranded writes = log between P_W and P_E  ≈  λ_w × L(t0)
```

Worked numbers (running example): EU tenants write 700 writes/s; lag at the moment of failure is
2 s, so about **1,400 writes** are stranded in the old primary. Of those, a few may be
externally visible (emails sent, webhooks fired, payments captured by a third party) even though
the database that recorded them is gone.

What to do with stranded writes:

1. **Before promotion, if the old region is reachable at all**, try to ship the tail (some
   managed systems do a controlled "switchover" with zero loss when the source is healthy; an
   unplanned "failover" does not wait).
2. **After the old region returns, extract before rejoining.** Dump the log range `(P_W, P_E]`
   from the old primary (binlog/WAL, or CDC stream) **before** rewinding it into a replica.
3. **Reconcile by type:** idempotent upserts can be replayed if no newer write exists; conflicting
   ones go to a queue for human or app-level review; externally visible side effects are matched
   against third-party records (payment provider, email logs).
4. **Tell affected customers** if data they saw as saved is missing. The number of affected
   tenants is small and computable; silence is worse than the loss.

**RPO accounting:** the number that belongs in the design doc is not "RPO = seconds" but
"`λ_w × L_p99.9` writes, lag measured continuously, with the lag SLO in §8". For the running
example: 2,000 writes/s × 1 s (p99) = 2,000 writes on a normal day; × 10–30 s during a degraded
network = 20,000–60,000.

For consensus-replicated data (§3.4) there is nothing to promote: the surviving majority elects a
leader and committed writes are not lost. What remains is §7.4–§7.7: capacity, caches and
latency in degraded mode.

### 7.4 Capacity headroom: the N+1 region math

For `N` active regions with equal shares, each running at utilization `u`, losing one region
spreads its load over the remaining `N − 1`:

```
u_after = u × N / (N − 1)          require  u_after ≤ u_max

u_safe  = u_max × (N − 1) / N      (highest steady-state utilization that survives one region loss)
total provisioned capacity = N / (N − 1) × peak / u_max
```

| Active regions `N` | `u_safe` with `u_max` = 100% (absolute ceiling) | `u_safe` with `u_max` = 80% | `u_safe` with `u_max` = 70% | Capacity multiplier `N/(N−1)` |
|---|---|---|---|---|
| 2 | 50% | 40% | 35% | 2.0x |
| 3 | **66.7%** | 53% | **47%** | 1.5x |
| 4 | 75% | 60% | 52.5% | 1.33x |
| 5 | 80% | 64% | 56% | 1.25x |

With three regions, each region can never run above about 66% of its capacity at peak, and above
~47% if you want the survivors at a sane 70% after the failover. More regions make each one
cheaper to protect, which is part of why large services run many regions or many cells.

**The equal-share assumption is usually false.** Evacuated traffic goes to the **nearest** healthy
region, not evenly to all. Running example, peak shares: `us-east-1` 45%, `us-west-2` 20%,
`eu-west-1` 35%. If `eu-west-1` is evacuated and its users are routed to `us-east-1` (nearest with
capacity):

```
us-east-1 load after = 45% + 35% = 80% of global peak
required us-east-1 capacity at u_max = 70%:  0.80 / 0.70 = 114% of global peak
```

So `us-east-1` alone must be provisioned for more than the entire global peak, or the evacuation
plan must split EU traffic explicitly (e.g. 60% to `eu-central-1`, 40% to `us-east-1`). Write the
**evacuation mapping** down per region and size each survivor for its worst case.

Capacity is not only CPU:

| Resource | Why it breaks during evacuation |
|---|---|
| Promoted databases | Replicas are often smaller instances than primaries; a promoted replica must be primary-sized |
| Cloud quotas | Instance, IP and load-balancer limits in the survivor region were sized for its own traffic |
| Autoscaling | Takes minutes, needs the provider's control plane, and competes with every other customer evacuating to the same region; pre-provision (**static stability**, 34 §11.5) |
| Third-party limits | Payment, SMS, email providers' per-account or per-region rate limits |
| Cross-region links and NAT/egress | Replication catch-up and forwarded writes saturate them |
| Connection limits | Survivor databases hit `max_connections` as app fleets scale up |

See [`35-reliability-math-slos-and-error-budgets.md`](35-reliability-math-slos-and-error-budgets.md)
§9 (N−1 rule, headroom) and [`../sre-observability/16-capacity-planning.md`](../sre-observability/16-capacity-planning.md)
§10.

### 7.5 Fencing the old primary

The old primary must be **unable** to accept writes before the new one accepts them, or two
regions accept writes for the same data (split brain). Across regions this is harder than inside
one, because the failed region is exactly the one you cannot reach to shut down. Layers, strongest
last:

| Fence | Mechanism | Works when the old region is unreachable? |
|---|---|---|
| Routing | Front door / directory stops sending requests to the old region | Partially: clients and jobs **inside** the old region can still write locally |
| Access revocation | Revoke the old primary's credentials, security-group rules, or storage access via the provider's API | Only if that region's control plane works (often not) |
| STONITH-style | Power off / isolate the old primary | Same limitation |
| **Self-fencing via lease** | The primary holds a lease renewed through a quorum **outside its own region** (e.g. a 3-region coordination service); if renewal fails, it demotes itself to read-only | **Yes**: it needs no message to arrive |
| **Epoch / fencing token at every downstream** | Each promotion increments an epoch; writes carry it; downstreams (other databases, queues, idempotency stores) reject lower epochs | Yes, for writes that pass through those downstreams |

Lease timing, with drift bound `ρ` (e.g. 1%):

```
old primary stops writing at:   last_renewal + T_lease                 (its own clock)
new primary may start at:       last_renewal + T_lease × (1 + ρ) + margin   (as measured by the promoter)

T_lease = 10 s, ρ = 1%, margin = 2 s  →  promotion waits ≥ 12.1 s after the last successful renewal
```

That wait is a few seconds of RTO bought in exchange for not having split brain. Mechanics of
leases, epochs and fencing tokens: [`03-consensus-raft-and-distributed-locking.md`](03-consensus-raft-and-distributed-locking.md)
§9.2–§9.4 and [`29-failure-detection-phi-accrual.md`](29-failure-detection-phi-accrual.md) §8. Consensus
systems (§3.4) fence automatically: a leader that cannot reach a majority cannot commit, and a
leaseholder stops serving reads when its lease lapses.

### 7.6 Cache warmup and cold dependencies

Survivor regions have caches warm for **their own** users. Evacuated users arrive with keys the
cache has never seen.

```
Survivor before:  10,000 reads/s at h = 0.95  →  DB sees 500 reads/s   (DB capacity 3,000 reads/s)
Evacuated users:  +7,000 reads/s arriving cold at h ≈ 0.30
All at once:      DB sees 500 + 7,000 × 0.70 = 5,400 reads/s  →  1.8x capacity: overload, timeouts,
                  retries (34 §1.5), and possibly a metastable state that persists after the cause
Stepped (10%):    +700 reads/s × 0.70 = +490 reads/s  →  990 reads/s; hit rate on moved keys climbs
                  toward 0.9 within minutes as the working set loads; then the next step
```

Mitigations, cheapest first:

- **Shift in steps** (§7.7) so the cache fills while the database has headroom.
- **Request coalescing / single-flight** on cache misses, so 1,000 concurrent misses for one key
  become one database read ([`08-caching-strategies-and-patterns.md`](08-caching-strategies-and-patterns.md)).
- **Pre-warm** from a list of each region's hottest keys, replayed against the survivor's cache
  at the start of an evacuation.
- **Keep a warm follower cache** for another region's hottest data in active-active designs, at the
  cost of cross-region cache invalidation traffic.
- **Budget database capacity for the cold-cache miss rate,** not the steady-state one, in the
  survivor sizing of §7.4.

The same "cold" problem applies to JIT-compiled runtimes, connection pools, DNS caches and
autoscaling baselines in the survivor region.

### 7.7 Traffic shifting in steps

| Step | Share of the evacuated region's traffic moved | Hold | Check before continuing | Abort / roll back if |
|---|---|---|---|---|
| 0 | 0% (pre-checks) | — | Survivor headroom, quotas, replication health, runbook owner assigned | Survivor already > `u_safe` |
| 1 | 10% | 3–5 min | Error rate, p99 latency, DB CPU and connections, cache hit rate on moved traffic | Survivor error rate > 2x baseline |
| 2 | 25% | 3–5 min | Same, plus replication lag of survivor's replicas | DB CPU > 70% |
| 3 | 50% | 5 min | Same | p99 > SLO threshold |
| 4 | 100% | — | Same; then freeze deploys in all regions | — |

In a hard-down region, steps 1–3 compress (the traffic is failing anyway, so moving it faster is
better than not moving it), but still ramp the **database-heavy** paths gradually if the survivor
is cold. In a gray failure or a planned evacuation, take the full ladder. Weighted records at the
global load balancer or DNS make the steps a one-line change; script them in advance.

### 7.8 Failback

Failback is a second failover, done by tired people who think the incident is over. Rules:

1. **Not during the incident.** Wait until the old region has been healthy for a sustained period
   (hours) with the provider's all-clear.
2. **Rebuild, don't reuse.** Rebuild replicas in the old region from the current primaries. Rejoin
   an old primary only after extracting its stranded writes (§7.3).
3. **Verify parity**: configuration, secrets, feature flags, schema version and deployed code in the
   old region match the survivors (config drift accumulates fast while a region is drained).
4. **Planned switchover, not failover,** for homed data: brief per-tenant write freeze, wait for
   lag = 0, flip the directory. RPO 0 is achievable when both sides are healthy.
5. **Step traffic back** with the same ladder as §7.7, in business hours.
6. **Restore leader/leaseholder preferences** that moved during the incident, or the region pays
   cross-region write latency for weeks without anyone noticing.

### 7.9 Game days and evacuation drills

An untested failover is a hypothesis. From 35 §8: with a primary at 99.9% and a failover that works
99% of the time, availability is `1 − 0.001 × 0.01 = 99.999%`; if the failover really works 90% of
the time, it is 99.99%. Drills are how you learn which number is true.

A progression that teams actually sustain:

| Level | Exercise | Frequency | What it proves |
|---|---|---|---|
| 1 | Tabletop: walk the runbook with the on-call rotation | Quarterly | People know the steps and who decides |
| 2 | Drain 10% of one region's stateless traffic in production, then return it | Monthly | Routing controls work; survivors absorb load |
| 3 | Full stateless evacuation of one region for 1 hour | Quarterly | Capacity, quotas, caches, connection draining |
| 4 | Planned switchover of homed data for one cell, then back | Quarterly | Promotion tooling, directory flip, failback |
| 5 | Full region evacuation including data, announced | Twice a year | End-to-end RTO; the numbers in your DR claims |
| 6 | Unannounced drill during business hours | Yearly, when level 5 is boring | Detection and decision, not just mechanics |

Measure every drill: detection time, decision time, time per step, error budget spent, and every
manual step that was not in the runbook. Those manual steps are the backlog.

Things drills reliably find: a break-glass credential that depends on the evacuated region's
identity provider; a cron job or batch worker that only runs in one region; a quota at 80% in the
survivor; a dashboard hosted in the evacuated region; a DNS record with a 1-day TTL nobody knew
about; a replica that was quietly 40 minutes behind.

### 7.10 Region evacuation runbook checklist

**Before any incident (standing readiness):**

- [ ] Evacuation mapping per region: where each region's users and homed data go (§7.4).
- [ ] Each survivor sized for its worst-case mapping at `u_max`, including databases, quotas and
      third-party limits; verified by the last drill.
- [ ] Failover DNS records at TTL ≤ 60 s; max connection age configured; front-door weights
      scripted.
- [ ] Replication lag SLO and alerting in place (§8); semisync degradation alerts on.
- [ ] Promotion, fencing and directory-flip procedures automated behind one command each, with
      dry-run modes.
- [ ] Break-glass access that does not depend on any single region (identity provider, VPN,
      secrets, CI/CD).
- [ ] Observability for the evacuation hosted outside the region being evacuated.
- [ ] Decision owner per region and a pre-agreed promotion clock (§7.1).

**During the incident:**

1. [ ] Declare the incident; name an incident commander and a separate operator for the
       evacuation ([`../sre-observability/15-incident-response-and-postmortem.md`](../sre-observability/15-incident-response-and-postmortem.md)).
2. [ ] Confirm with at least 2 independent signals (external probes, real-user errors, provider
       status).
3. [ ] Freeze deploys and config changes in **all** regions.
4. [ ] Stop batch and background jobs in the impaired region.
5. [ ] Check survivor headroom against the mapping; pre-scale if the control plane allows.
6. [ ] Drain stateless traffic in steps (§7.7), with connection draining on.
7. [ ] Start the promotion clock. At expiry, or earlier with evidence the region will not recover:
8. [ ] Record replication positions (`P_W`, and `P_E` if readable) for every homed database.
9. [ ] Fence the old primaries (lease expiry wait, epoch bump, access revocation where possible).
10. [ ] Promote replicas; flip the directory for affected tenants; verify writes end to end.
11. [ ] Check derived systems: queues, search indexes, caches, idempotency stores, schedulers now
        run in the survivor.
12. [ ] Post status updates; list tenants with stranded writes.
13. [ ] Watch degraded-mode risk: reduced fault tolerance, no maintenance, capacity alarms.

**After:**

- [ ] Extract and reconcile stranded writes before rejoining any old primary (§7.3).
- [ ] Fail back per §7.8, planned, stepped, in business hours.
- [ ] Postmortem with measured detection, decision and execution times; update the runbook and
      the drill plan.

---

## 8. Observability for Multi-Region Systems

> **In plain words.** A multi-region system needs a few numbers that a single-region system never
> had: how far behind each copy is (that is your real data-loss exposure), how each region is
> doing on its own (a global average hides a small region being completely down), and whether the
> other regions could take over right now. And the screens you will use during an evacuation must
> not live in the region you are evacuating.
>
> **Real-world example.** The SaaS's global availability for a month reads 99.97%, comfortably above
> its 99.95% SLO. The APAC edge region, carrying 10% of traffic, was fully down for 1 hour: 60
> minutes × 10% = 6 minutes of global-equivalent downtime, invisible in the global number and a
> 1-hour outage for every APAC customer.

### 8.1 Replication lag as a first-class SLI

Lag is the RPO you actually have. Measure it in **time**, not bytes:

- **Heartbeat method:** the primary writes `now()` to a heartbeat row every second; each replica
  computes `now() − replicated_heartbeat`. It measures end-to-end apply lag, including a stalled
  apply thread. Its error is the clock skew between primary and replica hosts (small with a
  provider time service; check it).
- **Position method:** byte/LSN distance between primary and replica, divided by the current
  write rate. Cheaper, misleading when the write rate changes.
- **Consensus systems:** there is no lag for committed data; watch instead the **closed-timestamp
  / safe-time lag** (how stale follower reads are) and **under-replicated ranges**.

Example SLOs (illustrative, running example):

| SLI | SLO | Page when | Why |
|---|---|---|---|
| Cross-region apply lag, per homed database | p99 < 1 s over 5-minute windows; 99.9% of minutes < 5 s | > 30 s for 2 min | Bounds RPO to ~2,000 writes normally, ~60,000 worst case tolerated |
| Writes at risk (`λ_w × lag`) | < 10,000 | > 50,000 | Puts lag in business units |
| Time spent with semisync / sync degraded to async | 0 minutes per week | Any degradation > 1 min | The "sync" promise is false while degraded (§3.3) |
| Follower-read staleness | < `S` (e.g. 5 s) | > 2 × `S` | Reads served locally are staler than the product promised |
| Stranded-write reconciliation backlog | 0 after 7 days | — | Unreconciled data from the last failover |

### 8.2 Per-region SLIs and SLOs

Compute every user-facing SLI **per serving region and per home region** as well as globally,
exactly because the traffic-weighted global number hides small regions (see
[`../sre-observability/33-federated-multi-region.md`](../sre-observability/33-federated-multi-region.md)
§8.3). Burn-rate alerting (35 §5) runs per region; a regional page goes to the team that can
evacuate that region.

| View | Answers | Example |
|---|---|---|
| Per **serving** region | Is region X healthy for requests that land there? | `eu-west-1` edge error rate |
| Per **home** region | Are tenants homed in X getting service, wherever they connect from? | EU-homed tenants' write success rate from all regions |
| Cross-region request path | Are forwarded writes / global-table writes healthy? | Success and p99 of writes forwarded to a non-local home |
| Global (traffic-weighted) | Overall product health and the external SLA | The number in the SLA report |

The "per home region" view is the one people forget: during an evacuation of `eu-west-1`, EU users
may connect happily to `eu-central-1` while their writes fail because their home database has
not been promoted yet.

### 8.3 Failover-readiness metrics

Readiness decays silently. Track it like any other SLI:

| Metric | Target |
|---|---|
| Survivor headroom vs evacuation mapping (§7.4), per region | Every survivor ≤ `u_safe` at the last peak |
| Cloud quota headroom in each survivor for its worst-case mapping | ≥ 1.2x of the required amount |
| Config/secret/schema/code-version drift between regions | 0 unexplained differences (compare hashes) |
| Leaseholders / leaders outside their preferred region | Near 0 outside maintenance (each costs a cross-region RTT per write) |
| Cross-region commit latency p99 (consensus data) | Within the budget of §2.4 |
| Inter-region clock offset | Well under the database's configured maximum offset; consensus databases may refuse to serve or shut a node down when it is exceeded |
| Days since last successful evacuation drill, per region | < 90 |
| Measured RTO from the last drill | ≤ the RTO in the DR plan |

### 8.4 Telemetry must survive the region

If the metrics backend, the dashboards, the alert manager or the status page live in the region
that fails, the team is blind exactly when it needs to see (§12.2 shows a provider's own status
page failing this way). Minimum: alerting and the evacuation dashboards run in (or are replicated
to) a region other than the one they watch, and external synthetic probes run from outside all of
your regions. Architectures for this are in
[`../sre-observability/33-federated-multi-region.md`](../sre-observability/33-federated-multi-region.md)
(§4.4 the hub's failure mode, §10 failure-domain isolation) and
[`../sre-observability/36-dr-for-observability-stack.md`](../sre-observability/36-dr-for-observability-stack.md).

---

## 9. Decision Matrix: Requirement to Architecture

> **In plain words.** Start from the requirement you can actually name (a number, a contract, a
> market) and pick the cheapest architecture that meets it. If you can't name the requirement,
> you probably need the cheaper option.
>
> **Real-world example.** "We need active-active" usually turns out to mean "EU customers need their
> data in the EU" plus "a region outage must not stop us for more than 30 minutes". That is
> write-home with a pilot light per jurisdiction, not multi-leader everywhere.

| Requirement (be specific) | Architecture | Data topology | Cost / complexity | Simpler alternative to rule out first |
|---|---|---|---|---|
| SLO ≤ 99.9%, one market, no residency | Single region, 3 AZs; cross-region backups | Multi-AZ primary + standby | Low | — (this is the default) |
| Must survive region loss; RTO of hours acceptable | Backup & restore to a second region; infrastructure as code | Snapshots + log archive copied cross-region | Low | — |
| RTO < 1 h, RPO of seconds acceptable | Pilot light | Async replica cross-region | Low–medium | Backup & restore if RTO can stretch |
| RTO < 15 min, RPO of seconds acceptable | Warm standby or active-passive; automated promotion behind a human decision | Async replica; stepped traffic shift | Medium | Pilot light with pre-provisioned compute |
| RPO = 0 on region loss, writers on one continent | Consensus 2+2+1 (or close witness) within the continent; or sync commit to any 1 of 2 standbys | Consensus / quorum sync | Medium–high; +10–70 ms per write | Accept seconds of RPO with reconciliation, if the business can |
| RPO = 0 and fast writes for global users | Home region per tenant **with** region-survivable consensus per home (e.g. `REGIONAL BY ROW` + `SURVIVE REGION FAILURE`) | Consensus, leaders pinned to homes | High; ~1 RTT to nearest other voter per write | RPO seconds via async per home |
| Low read latency for distant users | Read-local: regional read replicas / follower reads, CDN, edge TLS | Async or consensus followers; bounded staleness | Medium | CDN + edge TLS alone |
| Low write latency for distant users | Write-home: home region near the user | Per-home primary (async or consensus) | High | Measure first: is write latency really the problem? |
| Data residency (EU, etc.) | Write-home with homes pinned by jurisdiction; DR region inside the jurisdiction | Placement constraints; per-jurisdiction backups and telemetry | High | Single region inside the jurisdiction for those customers |
| Residency **and** region survival | Three regions (or two + witness) inside the jurisdiction | Consensus with placement restricted to the jurisdiction | High | Zone survival inside one in-jurisdiction region + async DR in another |
| Writes must succeed locally everywhere, even when partitioned (collaboration, counters, carts, presence) | Multi-leader active-active | CRDT types or siblings; no LWW for data that matters | High | Write-home with an offline queue on the client |
| Global uniqueness / inventory / balances | Single home per invariant, or a small consensus registry; escrow for stock | Consensus or single-home + sagas across homes | Medium (scoped to a few tables) | — (do not try to merge these) |
| Limit blast radius of own deploys and bad tenants | Cells within each region; deploy cell by cell | Per-cell databases | Medium | Shuffle sharding at the poisonable tier |
| 99.99%+ availability for a global product | ≥ 3 active regions for stateless tiers, region-survivable data, per-region SLOs, quarterly evacuation drills | Mix per table (§3.6) | Highest | Is 99.95% with a 15-minute RTO actually enough? |

A compact way to walk it:

```
Can you name a requirement that one region with 3 AZs cannot meet?
 ├─ No  → stay single-region; copy backups cross-region; revisit yearly.
 └─ Yes → which one?
     ├─ Survive region loss ──► RTO hours? backup&restore · < 1 h? pilot light · minutes? warm/active-passive
     │                          RPO must be 0? → consensus with a 3rd site (never 2 regions alone)
     ├─ Residency ────────────► write-home by jurisdiction; DR + backups + telemetry inside it
     ├─ Latency ──────────────► reads: follower reads / replicas / CDN · writes: home near the user
     └─ Local writes everywhere ► multi-leader only for CRDT-friendly data; invariants stay single-home
```

---

## 10. Production Pitfalls / War Stories

> **In plain words.** Multi-region systems rarely fail because the replication algorithm was wrong.
> They fail because something nobody listed was still in one region, because a "synchronous"
> setting quietly turned asynchronous, because the other region was too small or too cold, or
> because a wrong guess triggered an expensive failover.
>
> **Real-world example.** A team's DR plan promised a 15-minute RTO. The first real evacuation took
> 2 hours: the break-glass login went through an identity provider hosted in the failed region
> (§10.2).

These are **composite scenarios** built from failure modes described in this chapter; numbers are
illustrative but internally consistent. Real, public incidents are in §12.

**10.1 The chatty app moved, the database did not.** A team "went multi-region" by deploying the
app tier in `eu-west-1` pointed at the `us-east-1` database. The page that made 12 sequential
queries went from 120 ms to about 1 s for European users (12 × ~75 ms). Fix: read replica plus
follower reads in the EU for the read path; writes forwarded home once per request, not once per
query (§2.3).

**10.2 The single-region dependency nobody listed.** Stateless services ran in 3 regions; the
identity provider, the secrets manager's primary, the feature-flag service and the CI/CD system
ran only in `us-east-1`. When that region failed, the other regions kept serving existing
sessions but could not log users in, rotate a secret, or deploy a fix. Fix: an inventory of every
dependency with its regions, a rule that every runtime dependency is either multi-region or
degrades to a cached local copy, and break-glass access that does not route through any single
region. Drills find these in an afternoon (§7.9).

**10.3 Semisync that was async when it mattered.** A MySQL pair across two regions ran semisync
"for RPO 0". During a week of cross-region packet loss, acknowledgments timed out after the
default 10 s and replication silently fell back to async several times a day. The eventual
failover lost about 40 s of writes. Fix: alert on semisync status changes, count degraded time
against the RPO objective, and move the ledger tables to a quorum-commit or consensus setup with
three sites (§3.3).

**10.4 The two-region quorum.** A 5-node etcd/consensus cluster was "spread for resilience" as 3
nodes in region A and 2 in region B. Region A's network outage stopped every write in both regions
for 70 minutes (2 of 5 is not a quorum). Fix: 2+2+1 with a witness in a third region (§4.2).

**10.5 RPO is lag, and lag spikes when you backfill.** A data backfill wrote 40,000 rows/s for
3 hours; cross-region replication fell 25 minutes behind. Nobody watched lag because "RPO is
seconds". Had the region failed that afternoon, 25 minutes of writes would have been stranded.
Fix: lag SLO with paging (§8.1), and throttle bulk jobs on replication lag, not only on primary
CPU (the backpressure pattern in 34 §5).

**10.6 Auto-increment IDs in multi-leader.** Two regions accepting inserts with auto-increment
primary keys produced duplicate IDs within minutes of enabling bidirectional replication.
Fix: UUIDv7 / Snowflake-style IDs with region bits; auto-increment offset/increment pairs only as
a stopgap (§6.1).

**10.7 LWW ate the correction.** A profile service used a multi-leader store with LWW. Support
tickets about "my address change didn't stick" traced to one host whose clock drifted 300 ms slow
after a misconfigured time daemon; its writes lost every race within that window. Fix: host clock
offset alerting, per-field merge, and routing each user's writes to their home region (§6.2).

**10.8 The survivor was cold.** A planned evacuation moved 100% of a region's traffic in one step.
The survivor's cache hit rate for the moved tenants started near 20%; database CPU reached 100%,
retries tripled the load, and the survivor itself breached its SLO for 25 minutes, turning a
one-region problem into a two-region one. Fix: stepped shifts (§7.7), request coalescing, and a
cold-cache term in survivor sizing (§7.6).

**10.9 Quota, not capacity.** The survivor region had spare hardware budget but hit the account's
instance quota at 80% of the required scale-up during an evacuation drill. Raising a quota during a
real regional event competes with every other customer doing the same. Fix: quotas sized to the
evacuation mapping and checked as a readiness metric (§8.3).

**10.10 Failback forgotten.** After an evacuation, leaseholders for EU tenants stayed in the US for
six weeks. EU write latency rose by ~75 ms and nobody connected the regression to the incident.
Fix: failback checklist item for leader preferences, and a "leaders outside preferred region"
metric (§7.8, §8.3).

**10.11 Residency leak through telemetry.** EU tenant data was homed correctly in the EU, but
request logs containing email addresses were shipped to a central US logging cluster. Fix: scrub
at the collector; per-jurisdiction telemetry backends (§5.3).

**10.12 Cross-region transfer bill.** Replicating 50 MB/s of change data to two other regions is
`50 MB/s × 86,400 s × 2 ≈ 8.6 TB/day`. At inter-region list prices on the order of $0.01–0.02 per
GB (varies by provider and route; check current pricing), that is roughly $2,600–5,200 per month
before any forwarded application traffic. Fix: replicate only what the topology needs (not every
table to every region), compress, and include transfer in the multi-region business case (§1.2).

---

## 11. Interview Questions — Multi-Region Systems

> **In plain words.** Interviewers want to see that you start from requirements and numbers
> (RPO, RTO, latency, residency), that you know the speed of light sets a floor, that you can
> place replicas so a region loss doesn't kill the quorum, that you avoid conflicts by ownership
> before reaching for CRDTs, and that you can run an evacuation, not just draw one.
>
> **Real-world example.** "Make our SaaS active-active in the US and EU." A strong answer asks what
> "active-active" is for, proposes write-home with EU residency, puts uniqueness in a small global
> registry, and sizes each region to absorb the other.

### Conceptual questions

**Q1. What is the minimum latency cost of a synchronous write between Virginia and Ireland, and why?**
*Sections: §2.*
One round trip at the speed of light in fiber: ~5,500 km great circle, ~200 km/ms one way, so a
floor of ~55 ms RTT; real paths measure ~70–85 ms. Any synchronous replication or consensus commit
that needs an Irish acknowledgment pays at least that, per commit, plus more for sequential round
trips.

**Q2. What sets the commit latency of a consensus group spread across regions?**
*Sections: §2.4, §4.1.*
The leader waits for `Q − 1` acknowledgments, so commit latency is `T_fsync` plus the `(Q−1)`-th
smallest RTT from the leader to the other voters, i.e. the RTT to the nearest majority. Clients in
other regions add `RTT(client, leader)`. Moving the leader next to the writers and placing a voter
(or witness) close to the leader are the two levers.

**Q3. Why can't a two-region deployment survive the loss of either region with a majority quorum?**
*Sections: §4.2.*
With `a` and `b` voters, surviving the loss of A requires `b ≥ Q` and surviving B requires `a ≥ Q`,
so `a + b ≥ 2Q > n`, a contradiction. One region always holds the majority (or an even split needs
both). The fix is a third site (a witness is enough), or accepting a manual, possibly lossy
promotion.

**Q4. Compare async primary-replica, sync replication and cross-region consensus.**
*Sections: §3.*
Async: local write latency, RPO = `λ_w × lag`, promotion needed (usually a human decision).
Sync to one standby: RPO 0, +1 RTT per write, and the standby region's failure blocks or degrades
writes. Consensus with ≥ 3 sites: RPO 0, automatic failover, +RTT to nearest majority, but no
availability without a majority.

**Q5. What does "write-home, read-local" mean and why is it the default for active-active?**
*Sections: §5.2, §6.1.*
Every tenant/record has one home region that accepts its writes; other regions serve bounded-stale
reads from replicas and forward writes home. There are no write conflicts by construction, local
latency for most users, and natural data-residency pinning. The cost is one RTT for writes from
outside the home and a directory to maintain.

**Q6. When is last-writer-wins acceptable across regions, and what goes wrong otherwise?**
*Sections: §6.2.*
Acceptable when losing one of two concurrent writes is harmless (preferences, "last seen").
Otherwise: clock skew picks the winner rather than real time, the conflict window is the
replication lag (hundreds of ms to seconds), and whole-item LWW discards unrelated field changes.
All of it is silent.

**Q7. How do you keep usernames globally unique in an active-active system?**
*Sections: §6.4.*
You can't merge a uniqueness violation after both regions said "yes", so uniqueness needs a
serialization point: a small global registry (consensus across 3+ regions, or a single write-home
region) keyed by the normalized name or its hash. Signup pays one cross-region commit
(~70–150 ms), which is fine because signups are rare; everything else about the account is
regional.

**Q8. How do you sell limited inventory from several regions without overselling?**
*Sections: §6.4.*
Either a single home per SKU (remote buyers pay one RTT), or escrow: split the stock into
per-region allotments, sell locally against the allotment, rebalance unused allotment through a
coordinator. CRDT counters alone cannot enforce "≥ 0".

**Q9. With 3 active regions, how busy may each region be?**
*Sections: §7.4.*
Losing one multiplies each survivor's load by `N/(N−1)` = 1.5, so `u ≤ u_max × 2/3`: 66.7% at an
absolute ceiling, ~47% if survivors should stay at 70%. Then check the real evacuation mapping,
since traffic moves to the nearest region, not evenly, and check non-CPU limits (quotas, DB size,
third-party limits).

**Q10. Why is DNS failover slower than its TTL?**
*Sections: §7.2.*
Resolvers and runtimes may cache beyond the TTL, and long-lived connections (HTTP/2, gRPC,
WebSockets, DB pools) never re-resolve. Evacuation time is roughly TTL + max connection age +
client retry delay. Use an anycast global front door, cap connection age, and send GOAWAY during
evacuation.

**Q11. How do you fence an old primary in a region you cannot reach?**
*Sections: §7.5.*
You can't rely on reaching it, so it must fence itself: it holds a lease renewed through a quorum
outside its region and demotes itself when renewal fails; the new primary waits
`T_lease × (1 + ρ) + margin` before accepting writes. Add epochs checked by downstream stores, and
routing changes as defense in depth.

**Q12. Should region failover be automatic?**
*Sections: §7.1.*
Split it. Draining stateless traffic is reversible and can be automated with corroborated,
sustained signals and a hold-down. Promoting data is hard to reverse (lost writes, split brain,
risky failback), so a human decides against pre-agreed criteria and a decision clock. Never
automate failback. Consensus databases are the exception: their leader election is automatic and
safe by construction.

### System design prompts

**Prompt A. A B2B SaaS in `us-east-1` signs its first large EU customer, who requires EU data
residency and a 30-minute RTO for region loss.**
*Sections: §1, §5, §7, §9.*
Add `eu-west-1` as the EU tenants' home (write-home), with an in-EU DR copy (`eu-central-1`
pilot light or warm standby, async replication, RPO seconds with a lag SLO). US stays as is, with
its own pilot light. Global tables: tenant directory and a hashed-email registry, no personal
data. Per-jurisdiction backups and telemetry. Tenant re-homing tooling. Evacuation runbook and a
quarterly drill per region. Do not build multi-leader.

**Prompt B. A payments ledger must have RPO 0 and survive a region loss; most traffic is in the
US.**
*Sections: §3.4, §4.*
Consensus with 5 voters: 2 in `us-east-1`, 2 in `us-east-2` (or `us-west-2`), witness in a third
US region chosen for independent infrastructure; leader pinned to `us-east-1`. Commit ≈ RTT to the
nearest other voter region (~12–65 ms depending on choice). Balance checks read the leaseholder;
statements use follower reads. Cross-region transfers are single-home transactions if both
accounts share a home; otherwise a saga (06). Plan capacity for degraded mode (zero remaining
fault tolerance after a region loss).

**Prompt C. A collaborative whiteboard with users on every continent needs sub-50 ms interaction.**
*Sections: §2.5, §6.3.*
No synchronous cross-region path can meet 50 ms for everyone. Edits apply locally and merge with a
sequence/JSON CRDT (or OT via a per-board server placed near most participants). The board's home
is the region of most participants, holding the durable log; clients connect to the nearest edge.
Permissions and board ownership stay single-home.

### Rapid-fire

- Speed of light in fiber? **~200 km/ms one way; RTT ≈ 1 ms per 100 km.**
- `us-east-1` ↔ `us-west-2` RTT (typical)? **~60–75 ms.** `us-east-1` ↔ `eu-west-1`? **~70–85 ms.**
- Quorum of 5? **3; tolerates 2 failures.**
- Minimum regions for majority-quorum region survival? **3 (one can be a witness).**
- Lost writes on async failover? **`λ_w × lag` at the moment of failure.**
- `u_safe` for 3 regions at `u_max` 70%? **~47%.**
- Capacity multiplier for 4 active regions? **4/3 ≈ 1.33x.**
- Two regions, 2+1 voters, majority region lost? **Writes stop.**
- LWW's silent failure? **Discards a concurrent write; skew picks the winner.**
- What goes in a global table? **Small, read-mostly, non-personal: directory, registries, config.**

### Common mistakes

- Designing for "active-active" without naming the requirement it serves.
- Moving the app tier to a new region while leaving the database behind.
- Two-region consensus clusters, or a majority of voters in one region, labeled "region-resilient".
- Trusting semisync or "sync" without alerting on its degradation.
- Using LWW for data where a lost write matters; relying on conditional writes in multi-leader
  stores for global invariants.
- Sizing survivors with `N/(N−1)` while the real evacuation mapping sends everything to one region.
- Automatic failback; no decision clock; no drills.
- Forgetting that telemetry, backups and logs are part of data residency.

---

## 12. Real-world cases — incidents with numbers

> **In plain words.** Real, publicly documented incidents where regions, partitions or global
> dependencies behaved in the ways this chapter describes. Each one is here for its multi-region
> lesson, not for blame.
>
> **Real-world example.** A 43-second network partition led to 24 hours of degraded service at
> GitHub, because the automated response (cross-region promotion) was far more expensive than the
> problem (§12.1).

All cases below are based on the operators' own public postmortems or post-event summaries. Times
are theirs; where this chapter is not certain of a detail it says so or leaves it out.

**Quick index:** automated cross-region promotion after a short partition → 12.1 · regional
service down, many "global" sites down with it, status page broken → 12.2 · regional control
plane and monitoring impaired → 12.3 · a global backbone change takes every region down → 12.4 ·
a whole site lost, backups with it → 12.5 · globally replicated config crashes every region →
12.6 · single-region dependency with a long recovery tail → 12.7 · a "highly available" control
plane that depended on one facility → 12.8

### 12.1 GitHub, October 21, 2018: 43 seconds of partition, 24 hours degraded

- **Setup.** MySQL clusters managed by Orchestrator, which detects failed primaries and promotes
  replicas; data centers on the US East and West Coasts.
- **What happened.** Connectivity between the East Coast network hub and the primary East Coast
  data center was lost for **43 seconds** during network maintenance. Orchestrator promoted West
  Coast replicas to primary. When connectivity returned, the East Coast primaries held a brief
  period of writes that had not replicated west, and application traffic was now writing to
  primaries across the country.
- **Numbers.** GitHub ran in a degraded state for **24 hours and 11 minutes** while it restored
  from backups, re-synchronized replicas and reconciled the unreplicated writes, choosing data
  integrity over faster recovery.
- **Multi-region lesson.** The detection was correct and the action was catastrophic relative to
  the problem. Cross-region promotion of async-replicated data is an irreversible decision that
  strands writes (§7.3) and moves the leader far from its writers (§4.4). GitHub's follow-ups
  included configuring Orchestrator not to promote primaries across regional boundaries. This is
  the case for "automate the drain, humans decide the promotion" (§7.1). Also covered from the
  detector's side in [`29-failure-detection-phi-accrual.md`](29-failure-detection-phi-accrual.md) §10.5.

### 12.2 AWS S3, us-east-1, February 28, 2017: one regional service, much of the internet

- **What happened.** At 9:37 AM PST an operator running an established playbook mistyped an input
  and removed far more servers than intended from S3 subsystems in `us-east-1`, including the
  index and placement subsystems. Both needed a full restart.
- **Numbers.** The index subsystem was fully recovered at 1:18 PM PST and the placement subsystem
  at 1:54 PM PST, a little over **four hours** of S3 impairment in the region. AWS could not
  update service status on its own Service Health Dashboard for part of the event, because the
  dashboard's administration console depended on S3 in that region.
- **Multi-region lesson.** Many applications that looked global were single-region underneath,
  directly or through a dependency. The status page failure is the §8.4 lesson in its purest
  form: tooling you need during a regional outage must not depend on that region. AWS's
  remediation included making the tool remove capacity more slowly with minimum-capacity
  safeguards, and partitioning the index subsystem into smaller **cells** to reduce blast radius
  (§5.5).

### 12.3 AWS us-east-1, December 7, 2021: the regional control plane and monitoring

- **What happened.** At 7:30 AM PST, an automated capacity-scaling activity for a service in
  AWS's main network triggered unexpected behaviour from a large number of clients in AWS's
  internal network, and the resulting surge of connection activity congested the devices between
  the internal and main networks.
- **Numbers.** Network devices had fully recovered by **2:22 PM PST**, about **seven hours**
  later. Existing workloads in the region were largely less affected than **control-plane**
  operations (for example, EC2 APIs for launching instances), and AWS's internal monitoring was
  impaired, which slowed diagnosis and delayed updates to the status dashboard.
- **Multi-region lesson.** A failover plan that needs to call the impaired region's control plane
  (launch instances, change security groups, revoke credentials there) may not work during a
  regional event. Pre-provision survivor capacity (static stability, §7.4), fence by self-fencing
  leases rather than by API calls into the failed region (§7.5), and host monitoring outside the
  region it watches (§8.4).

### 12.4 Facebook, October 4, 2021: a global backbone change, every region at once

- **What happened.** During routine maintenance, a command intended to assess global backbone
  capacity unintentionally took down all connections in Facebook's backbone network, disconnecting
  its data centers from each other and from the internet. An audit tool meant to block such
  commands had a bug and let it through. Facebook's DNS servers, which withdraw their BGP
  advertisements when they cannot reach the data centers (a sensible health rule for a single
  site), then made Facebook's DNS unreachable worldwide.
- **Numbers.** Facebook, Instagram and WhatsApp were down for about **six hours**. Recovery
  required engineers on site at data centers, because remote-access tooling depended on the
  network that was down. Facebook described bringing traffic back **in stages** to avoid power and
  load surges, and credited prior "storm" drills for making that possible.
- **Multi-region lesson.** Regions protect against regional failures, not against a shared global
  component (backbone, DNS, config pipeline) failing everywhere. Out-of-band access and staged
  restoration (§7.7) are part of the design. Health rules that are correct locally ("withdraw if I
  can't reach my backend") can be catastrophic when every site applies them at once.

### 12.5 OVHcloud Strasbourg, March 10, 2021: the whole site is the failure domain

- **What happened.** A fire at OVHcloud's Strasbourg campus destroyed the SBG2 data center and
  damaged part of SBG1; the neighbouring buildings were shut down.
- **Numbers.** Services hosted there were down for days to weeks; customers whose backups were
  stored on the same campus lost data permanently. (Aggregate figures for affected sites and
  customers vary by source; this chapter does not quote one.)
- **Multi-region lesson.** Several buildings on one campus are not independent failure domains, and
  a backup in the same site is not a disaster-recovery copy. Rung 1 of §1.3 (backups in another
  region) exists for exactly this, and is cheap.

### 12.6 Google Cloud, June 12, 2025: globally replicated configuration

- **What happened.** A policy change with unintended blank fields replicated to every region
  within seconds and hit a new, unflagged code path in Service Control, crashing it everywhere.
- **Numbers.** Most regions recovered in about **two hours**; `us-central1` took up to **2 h 40 min**
  because restarting tasks overloaded the regional database they all read at once.
- **Multi-region lesson.** Multi-region serving does not help when the same bad data reaches every
  region at once. Global tables and config (§5.4) must propagate **incrementally, region by
  region, with validation**, like a deploy. Full write-up, from the overload angle:
  [`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md)
  §16, Case 7.

### 12.7 AWS us-east-1, October 19–20, 2025: long recovery inside one region

- **What happened.** A race condition in DynamoDB's automated DNS management left the regional
  endpoint's DNS record empty; DynamoDB in `us-east-1` became unreachable, and with it many
  services that depend on it. DynamoDB recovered in about three hours, but EC2 launches,
  networking and load balancers stayed impaired until **14:20 PDT on October 20**, about
  **14.5 hours** after the start.
- **Multi-region lesson.** An in-region recovery tail can be far longer than the trigger, so a
  pre-agreed promotion clock (§7.1) that fires at 15–30 minutes would have moved homed data long
  before in-region recovery completed, **if** the evacuation did not depend on launching capacity
  or calling APIs in the failed region. Full write-up:
  [`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md)
  §16, Case 8.

### 12.8 Cloudflare, November 2, 2023: a highly available design that depended on one facility

- **What happened.** A power failure at one of the three data centers in the Portland, Oregon
  area that hosted Cloudflare's control plane and analytics took those services down. The control
  plane was designed to be highly available across the three facilities, but a number of services,
  especially newer ones, had critical dependencies that ran only in the failed facility, and
  failover to the other sites had not been fully tested.
- **Numbers.** Cloudflare's edge network kept serving customer traffic, because it is designed to
  run without the control plane. Control-plane functions were restored over roughly the following
  day, partly by failing over to a disaster-recovery site in Europe, and some analytics services
  took until about November 4 (times approximate; see Cloudflare's postmortem for exact figures).
- **Multi-region lesson.** An HA claim is only as good as the least-tested dependency (§10.2).
  Separate the data plane from the control plane so the data plane survives control-plane loss
  (static stability), and prove each site's failure regularly with drills (§7.9).

---

## Key Takeaways

1. **Name the requirement first.** Latency, region-loss availability, data residency and blast
   radius lead to different architectures. Most products should stop at pilot light or write-home,
   not full active-active.
2. **Physics sets the floor.** ~200 km/ms in fiber, ~1 ms RTT per 100 km; real inter-region RTTs are
   10–75 ms within a continent and 70–240 ms across oceans. Count sequential round trips per
   request, not just the distance.
3. **For the same data, you pick one:** local write latency, or zero data loss on region failure.
   Async RPO is `λ_w × lag`; sync and consensus add one cross-region RTT per commit.
4. **Commit latency = RTT to the nearest majority.** Put leaders with their writers and a voter or
   witness close to the leader, on independent infrastructure.
5. **Two regions cannot survive the loss of either with a majority quorum.** Use a third site
   (a witness is enough), or accept manual, possibly lossy promotion.
6. **Avoid conflicts by ownership.** Give each tenant/record a home region; write home, read local;
   keep global tables small, read-mostly and free of personal data.
7. **LWW silently drops writes and lets clocks pick winners.** Use CRDTs for commutative types, and
   a single home, consensus, or escrow for invariants (uniqueness, stock, balances).
8. **Evacuation is an operation, not a diagram.** Automate the reversible drain, give humans a clock
   for the irreversible promotion, fence the old primary by lease, shift traffic in steps, and never
   fail back automatically.
9. **Size survivors for the real evacuation mapping.** `u_safe = u_max × (N−1)/N` (≤ 66% absolute,
   ~47% practical with 3 regions), plus quotas, databases, cold caches and third-party limits.
10. **Observe lag, per-region SLOs and readiness,** from outside the region being watched, and drill
    until the measured RTO matches the one in the plan.

---

## Cross-References

### Within distributed-systems/

- [`00-primitives-and-system-models.md`](00-primitives-and-system-models.md): CAP/PACELC,
  consistency models and quorum (`R + W > N`) definitions assumed in §1.2 and §4.
- [`03-consensus-raft-and-distributed-locking.md`](03-consensus-raft-and-distributed-locking.md):
  Raft, lease reads and follower reads (§7), fencing tokens and leases (§9.2–§9.4) used in §4.4
  and §7.5.
- [`04-replication-and-consistency.md`](04-replication-and-consistency.md): leader/follower,
  multi-leader and leaderless replication, lag, session guarantees, LWW, version vectors and CRDT
  basics that §3 and §6 build on.
- [`06-distributed-transactions-sagas-outbox-idempotency.md`](06-distributed-transactions-sagas-outbox-idempotency.md):
  sagas for cross-home transfers and idempotency keys during failover (§6.4).
- [`08-caching-strategies-and-patterns.md`](08-caching-strategies-and-patterns.md): request
  coalescing and cache warming for evacuation (§7.6).
- [`10-sharding-and-consistent-hashing.md`](10-sharding-and-consistent-hashing.md): partition keys,
  directory-based routing and moving logical shards, the basis of home regions and re-homing (§5.1).
- [`17-networking-protocols-and-communication.md`](17-networking-protocols-and-communication.md):
  TCP/TLS round trips, DNS TTL and caching, cross-region replication protocols (§2.3, §5.6, §7.2).
- [`29-failure-detection-phi-accrual.md`](29-failure-detection-phi-accrual.md): detection vs
  decision, fencing with epochs, and the GitHub 2018 case from the detector's side (§7.1, §7.5).
- [`33-resilience-patterns-circuit-breakers.md`](33-resilience-patterns-circuit-breakers.md):
  retry budgets and circuit breakers for forwarded cross-region calls and evacuation herds.
- [`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md):
  cells and shuffle sharding (§9.5), static stability (§11.5), and the 2025 cloud incidents (§16).
- [`35-reliability-math-slos-and-error-budgets.md`](35-reliability-math-slos-and-error-budgets.md):
  availability of placements (§8), failover success math, N−1 headroom (§9), burn-rate alerting.

### From databases/

- [`../databases/19-distributed-databases-deep-dive.md`](../databases/19-distributed-databases-deep-dive.md):
  clocks (§3), Spanner and CockroachDB commit protocols (§4.4–§4.5), multi-leader and leaderless
  patterns (§6), CRDT math (§7), anti-entropy (§8), multi-region primitives and follower reads
  (§12), Spanner/CockroachDB/DynamoDB/Cassandra system notes (§14–§15).
- [`../databases/12-replication-and-distributed-storage.md`](../databases/12-replication-and-distributed-storage.md):
  replication and consensus foundations, failure handling (§8).
- [`../databases/16-failure-detection-and-leader-election.md`](../databases/16-failure-detection-and-leader-election.md):
  leader election and failover mechanics inside a database.

### From sre-observability/

- [`../sre-observability/33-federated-multi-region.md`](../sre-observability/33-federated-multi-region.md):
  multi-region telemetry architectures and per-region vs global SLOs (§8).
- [`../sre-observability/36-dr-for-observability-stack.md`](../sre-observability/36-dr-for-observability-stack.md):
  keeping observability alive through a regional failure.
- [`../sre-observability/13-slo-engineering.md`](../sre-observability/13-slo-engineering.md): SLO
  authoring and burn-rate derivations for per-region SLOs.
- [`../sre-observability/16-capacity-planning.md`](../sre-observability/16-capacity-planning.md):
  multi-region capacity (§10).
- [`../sre-observability/15-incident-response-and-postmortem.md`](../sre-observability/15-incident-response-and-postmortem.md):
  incident command during an evacuation.
- [`../sre-observability/29-synthetic-monitoring.md`](../sre-observability/29-synthetic-monitoring.md):
  external probes for per-region detection.
- [`../sre-observability/32-compliance-and-privacy.md`](../sre-observability/32-compliance-and-privacy.md):
  residency and privacy for telemetry.

### From solutions/

- [`../solutions/key-value-store-design.md`](../solutions/key-value-store-design.md) §15: replica
  topologies (3×1 region, 1+1+1, 3+1+1, 2+2+1) with write latency and region survival for a
  Raft-based store.
- [`../solutions/distributed-counter-design.md`](../solutions/distributed-counter-design.md) §14:
  multi-region counters with per-region aggregation.
