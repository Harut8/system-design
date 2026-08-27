# Primitive Sheet: Highly Available, Strongly Consistent Key-Value Store

Extracted from [`solutions/key-value-store-design.md`](../solutions/key-value-store-design.md).
Method and template: [`README.md`](README.md).

**Drill this, don't read it.** Cover the *Choice* line, read only *Forced by*,
and re-derive. That is the interview.

---

## 0. The meta-primitive: the consistency/availability trade is scoped, not binary

Worth putting first because it reframes every other entry in this sheet.

**Choice:** CP — during a partition, the minority side refuses linearizable
operations.
**Forced by:** The functional requirements. `CompareAndSwap`, `PutIfAbsent`,
leases, and gap-free ordered watches are *not implementable* without a total
order per key. Four of seven requirements would have to be deleted under AP.
**In one breath:** You can't be both correct and answerable when the network
splits, so you pick correct — then spend the whole design shrinking how often,
how long, and for whom "unanswerable" applies.
**The number:** 3 replicas across 3 AZs → an AZ partition leaves 2 of 3 = a
majority. The common failure mode leaves a quorum reachable from almost
everywhere.
**Cost accepted:** A client stranded on the minority side gets `NO_QUORUM` for
linearizable operations — for the duration of the partition.
**Flips when:** The workload is carts, sessions, telemetry, or user-generated
content — anything where a write must never be refused and conflicts are rare
or mergeable. Then AP, and you delete CAS from the API rather than shipping a
CAS that usually works.

**The reframe that actually transfers:** "CP means unavailable during
partitions" is a misreading. The precise statement is *"linearizable ops on
the affected partitions are unavailable to clients on the minority side, for
the duration."* Every clause is a lever:

| Lever | Mechanism | Effect |
|---|---|---|
| "affected partitions" | 100k independent Raft groups, not one cluster-wide quorum | Only ranges whose quorum broke are affected |
| "minority side" | 3 replicas / 3 AZs | The common failure leaves a majority |
| "linearizable ops" | Three named read levels | Weaker reads stay available on both sides |
| "for the duration" | 4.5 s failover | A transient partition costs seconds |

**Follow-ups you must survive:**
- *"So it's not highly available?"* → It's available whenever a majority is
  reachable, which is every failure short of losing two of three AZs. The
  availability budget goes to deploys and overload, not to CAP — see §11.
- *"Why not just use quorums and get both?"* → See primitive 3. `W+R>N` gives
  intersection, not linearizability. Different property.

---

## 1. Partitioning

**Choice:** Range partitioning, 512 MiB ranges, split on size *and* on load.
**Forced by:** Ordered `Scan` at P99 ≤ 50 ms. On a hash ring a 1000-key prefix
scan must query every partition, merge, and discard the rest — the target is
unreachable, not merely worse.
**In one breath:** Keys are kept in sorted order and the keyspace is cut into
contiguous chunks, so a scan touches a couple of chunks instead of all of them.
**The number:** 50 TB / 512 MiB ≈ 100k ranges → 300k replicas / 54 nodes ≈
5,600 replicas per node. A 1000-key scan touches 1–2 ranges.
**Cost accepted:** Sequential keys (`/events/<timestamp>`) all land in the last
range, and splitting does not help because the new last range takes all the
writes. Plus split/merge/rebalance machinery a hash ring never needs.
**Flips when:** No ordered-scan requirement → hash immediately, and it takes
the hot-spot problem, the allocator, and the split machinery with it. Three
things fall out, not one.

**Two sub-decisions worth carrying separately:**

- **Range size 512 MiB, not 64 MiB.** At 64 MiB you'd have 45k replicas per
  node and a Raft heartbeat storm. Rescued at 5,600 by *coalesced heartbeats*
  (one message per node pair carrying all shared groups) and *quiescence* (an
  idle, caught-up range stops ticking entirely).
- **Load-based splitting matters more than size-based.** A 20 MiB range serving
  50k reads/s is a far worse problem than a 2 GiB range serving 10. A
  size-only policy is blind to it.

**Follow-ups you must survive:**
- *"How do you pick the split point?"* → By the median key by *load* for hot
  ranges (reservoir-sampled over request keys), by median-by-size otherwise.
- *"Doesn't splitting cause downtime?"* → No. Split is a Raft command on the
  range itself; both halves inherit the same replica set and no data moves.

---

## 2. Replication & consensus

**Choice:** Raft per range, RF=3, one Raft group per partition.
**Forced by:** Linearizability plus CAS. A consensus log gives a total order
per key for free; anything weaker requires bolting consensus on later anyway
(Cassandra's LWT is Paxos at ~4× a normal write).
**In one breath:** Each chunk of the keyspace has three copies and its own
elected leader; every write goes through that leader's ordered log, so all
three copies apply identical operations in identical order.
**The number:** Quorum = 2 of 3. Commit = leader fsync (0.3 ms) ‖ nearest
follower ack (~1.4 ms) → ~3.5 ms P50 write.
**Cost accepted:** One leaseholder per range means a single range is a
throughput ceiling (~50k writes/s with batching). Hot keys become a real
constraint — see primitive 8.
**Flips when:** RF=5 for namespaces needing to survive an AZ loss *and* a
concurrent node failure. Not the default, because…

**Why 3 and not 5** — the non-obvious part:
- 5 replicas means the commit waits for the **3rd-fastest of 5** rather than
  the **2nd-fastest of 3**. The write tail gets *worse*, not better.
- 1.67× storage.
- With only 3 AZs, RF=5 cannot maintain AZ diversity — some AZ holds two.

**Membership changes without losing quorum:** add the new replica as a
non-voting **learner** → snapshot and catch it up (availability unchanged
throughout) → single atomic joint-consensus change promoting it and removing
the dead one. Naively removing-then-adding passes through a 2-voter state
where one more failure is fatal.

**Follow-ups you must survive:**
- *"Raft or Paxos?"* → Equivalent in the steady state. Raft's membership
  protocol and log-matching invariants are precisely specified, so a small team
  can audit the implementation and reason about it at 3am. That outranks saving
  a message in an uncommon path.
- *"What happens to an in-flight write during failover?"* → See primitive 10.

---

## 3. Quorums — and what they do *not* give you

**Choice:** Reject `W+R>N` quorum replication as the consistency mechanism.
**Forced by:** The requirement is linearizability. Quorums give something
weaker and the difference is invisible until it bites.
**In one breath:** Overlapping read and write sets guarantee a read *sees* the
newest committed write — but a write that failed halfway leaves some replicas
updated with no commit point and no rollback, so two successive reads can
return new-then-old.
**The number:** N=3, W=2, R=2 → any read set intersects any write set in ≥1
replica. True, and insufficient.
**Cost accepted:** N/A — this is the rejected option.
**Flips when:** You genuinely only need "recent," not "correct" — then quorums
are cheaper and stay available under partition.

**This is the single most commonly fudged point in this design**, and it is a
frequent interview probe. The failure isn't hypothetical:

```
W=2, R=2, N=3.  Client writes v2. Reaches replica A only, then crashes.
Read 1: reads {A, B} → sees v2 (A has it)     → returns v2
Read 2: reads {B, C} → neither has v2         → returns v1
No partition. No concurrent write. Just a failed write and two reads.
```

**Follow-ups you must survive:**
- *"Isn't W+R>N strong consistency?"* → It's quorum intersection. It
  guarantees you see the latest *committed* value; it doesn't define a commit
  point, so it isn't linearizable. Cassandra needs Paxos-based LWT on top for
  exactly this reason.

---

## 4. Consistency levels as a priced menu

**Choice:** Three per-call levels — `linearizable`, `bounded_staleness(t)`,
`eventual` — each with documented cost *and documented partition behavior*.
**Forced by:** 10M reads/s cannot all go through one leaseholder per range, and
minority-side clients need *something* rather than a timeout.
**In one breath:** Let the caller say how fresh they need it; charge them
accordingly, and tell them how stale what they got actually was.
**The number:**

| Level | Latency P50 | Served by | Available in a partition? |
|---|---|---|---|
| `linearizable` | 1.9 ms | Leaseholder only | Majority side only |
| `bounded_staleness(t)` | 0.6 ms | Nearest replica | **Both sides** |
| `eventual` | 0.5 ms | Any replica | **Both sides** |

**Cost accepted:** API surface area, and the risk that teams pick the wrong
level. Mitigated by `max_staleness_ms` having a **floor of 3000** — matching
the closed-timestamp target — so a caller can't request 50 ms of staleness and
silently get a leaseholder read while believing they got a cheap one.
**Flips when:** A single-purpose store with one workload — then one level, and
the API is smaller and better.

**The design move that makes this work:** a bounded-staleness read is *not* a
"might be wrong" read. It is an **exact read of a slightly earlier consistent
snapshot** — every follower reading at timestamp T returns identical results
reflecting a real prefix of the total order. That distinction is what makes it
safe to build on, and it is the thing to say out loud.

**Session guarantees on top:** the client library carries the highest revision
it has seen and attaches it as `min_revision`. A replica that hasn't applied it
yet waits or redirects. Read-your-writes holds at *every* level, for free.
Without this, a client that writes then does a cheap read can see its own write
missing — the most confusing thing a store can do to an application developer.

---

## 5. Write path vs read path asymmetry

**Choice:** Reads get three specialised mechanisms; writes get one path.
**Forced by:** 5:1 read:write, and the observation that a naive linearizable
read costs a full consensus round — absurd at 10M reads/s for an operation that
changes nothing.
**In one breath:** A leader that's been partitioned away doesn't *know* it, so
serving a read from local state requires proving you're still leader. The whole
read-path design is about making that proof cheap.
**The number:**

| Mechanism | Cost | Assumes |
|---|---|---|
| Through the Raft log | Full round + fsync | Nothing |
| **ReadIndex** (default) | 1 quorum round of small messages, **no disk**, batched across all pending reads | Nothing |
| Leader lease | Zero round trips (0.9 ms) | Bounded clock skew (2 s stasis margin) |
| Follower reads | Local, any replica (0.6 ms) | Staleness ≥ 3 s acceptable |

**Cost accepted:** ReadIndex costs ~1 ms more than leases. Accepted, because
5 ms budget − 1.9 ms actual = plenty of headroom, and it buys a clock-free
correctness story.
**Flips when:** The read budget tightens below ~2 ms → leases become necessary
and you take on the clock assumption deliberately.

**The batching insight is the whole trick:** ReadIndex's confirmation round is
shared by *every read that arrives during it*. At 50k reads/s on a range with
1 ms heartbeat RTT, that's one round per ~50 reads — marginal cost ≈ 0. "One
RTT per read" would be unaffordable; "one RTT per batch" is free.

**Closed timestamps** (the follower-read mechanism), in one breath: the leader
periodically announces *"I will never again accept a write at a timestamp ≤ T."*
Any follower caught up to T can serve reads at T from local state, and get
byte-identical results to the leader.

---

## 6. Storage engine & the write-amplification trap

**Choice:** LSM, tiered compaction on the hot levels, leveled on the bottom.
**Forced by:** 3M replica-writes/s of 500-byte records. Random small writes are
the *worst* case for a B-tree — every write dirties a whole 4–16 KB page.
**In one breath:** Buffer writes in memory, flush sorted runs to disk, merge
them in the background — sequential I/O instead of random, at the cost of
checking several files per read.
**The number — this is the one to remember:**

```
Bytes into the LSM:    3M/s × 500 B                    = 1.5 GB/s
× write amplification:
    leveled (WA ≈ 15)  →  22.5 GB/s
    tiered  (WA ≈ 5)   →   7.5 GB/s

SSD endurance budget:  8 TB drive @ 2 DWPD = 185 MB/s sustained
Nodes required:        leveled → 122     tiered → 41
Nodes by capacity:     51
```

**Compaction strategy — not dataset size — sets the node count, and it moves
it by 3×.** Leveled would force buying 122 nodes for 224 TB of data: paying for
2.4× the disk you need in order to buy write endurance.

**Cost accepted:** Tiered means higher read amplification. Paid for in RAM
(10 bits/key bloom filters ≈ 9 GB/node, 96 GB block cache) rather than in 80
extra machines.
**Flips when:** A read-dominated, write-light workload → leveled, and you buy
back the space amplification and read simplicity.

**The L0 cliff — the mechanism that actually breaks the P99:**

```
Write burst → memtables flush faster than compaction drains L0
           → L0 file count climbs (they overlap, so every read checks all)
           → read latency climbs
           → at the stall threshold the engine THROTTLES OR STOPS WRITES
           → write latency: 3 ms → seconds
```

Defense: L0 file count is a **primary SLO signal** (warn 20, page 40, stall
~60) and drives admission control. It predicts an incident ~10 minutes before
latency moves. Compaction gets a **reserved** I/O budget — it is not background
work, it is the mechanism that keeps foreground work fast.

---

## 7. Capacity arithmetic — the binding resource is rarely the obvious one

**Choice:** Size by write amplification against SSD endurance, then check
capacity. Not the reverse.
**Forced by:** Nothing in the requirements — this is a *method*, and it is the
transferable part.
**In one breath:** Compute every resource independently, then find which one
binds. The obvious one usually doesn't.
**The number — two places the obvious answer was wrong in this design:**

**(a) MVCC garbage outweighs live data.**
```
25h GC TTL × 1M writes/s × 500 B × 3 replicas = 135 TB of garbage
                                     vs  50 TB of live data
→ cut the TTL to 4h: 22 TB. Now the estimate is right.
```
A capacity estimate that counts only live data is wrong by 3× here.

**(b) Node count set by endurance, not disk** — primitive 6.

**Cost accepted:** Shorter GC TTL shrinks the watch-resume and time-travel
window. Must stay above the incremental-backup interval (4h ≫ 15min ✓).
**Flips when:** Never. This is a method, not a decision.

**The habit to carry:** compute disk, IOPS, endurance, network, RAM, and CPU
*separately*, then look for the smallest. Then ask "what's accumulating that I
forgot to count?" — garbage, tombstones, indexes, replicas, retries.

---

## 8. Hot key vs hot partition — two different problems

**Choice:** Hot partitions are solved by the system automatically; hot keys are
solved by *diagnosing for the tenant* and offering primitives.
**Forced by:** A key cannot be split. It lives in one range, one leaseholder,
one node.
**In one breath:** Too much traffic to a *span* of keys is a placement problem
the system can fix by cutting the span. Too much traffic to *one* key is a
physics problem the system can only help you route around.
**The number:** Single range ceiling ≈ 50k writes/s (with batching), ≈ 150k
reads/s.

| | Hot partition | Hot key |
|---|---|---|
| Fixable by splitting? | **Yes** — that's the whole answer | **No** — nothing to split |
| Mechanism | Load-based split at the load median | Batching, then fan-out |
| Who acts | The allocator, automatically | The tenant, with library help |

**Solutions by workload — the specificity is the point:**

- **Read-hot key** (feature flag, 2M reads/s): the real answer is
  **watch-plus-cache**, not more replicas. Client caches the value and opens a
  watch; reads served from process memory at zero network cost, invalidated in
  milliseconds. 2M reads/s becomes ~1,000 open watches and one event per
  change. *This is what the watch primitive exists for.*
- **Write-hot key** (counter, 500k incr/s): Raft entry **batching plus
  coalescing** does most of it — 250 increments to the same key collapse into
  one `+250` before the entry is proposed, so the Raft path sees 2,000
  writes/s. Beyond that: sharded counters (`key/0..63`), exact because each
  shard is itself linearizable — unlike a CRDT counter. Cost: no CAS on a
  sharded counter, and that must be in the docs.
- **Contended lock key** (10k clients racing on `PutIfAbsent`): evaluate the
  precondition **before** the Raft round. 9,999 attempts are rejected locally
  in microseconds; only the winner replicates. This is the difference between
  a workable primitive and one that melts a range.

**Cost accepted:** Sequential-key workloads need explicit client-side hash
prefixing, which makes scans a 64-way merge.
**Flips when:** Hash partitioning → hot *partitions* vanish entirely; hot
*keys* remain exactly as bad. The two problems have independent solutions.

**Detection:** decaying top-K sketch per leaseholder, top 20 keys per range
gossiped. **A store that just gets slow, without naming the key that did it,
forces the tenant to guess.**

---

## 9. Failure detection — dead vs slow, and the cost of guessing wrong

**Choice:** Two independent signals with very different thresholds.
**Forced by:** Dead and slow are indistinguishable from outside, and the
correct responses are *opposite*.
**In one breath:** Cheap-to-undo reactions fire fast; expensive-to-undo
reactions wait.
**The number — the most important operational constant in the design:**

```
LIVENESS      9 s     → leases move away.
                        Cheap to get wrong: costs a 4.5 s lease transfer.
REPLACEMENT   5 min   → re-replicate the node's ranges.
                        Expensive to get wrong: moves 4.1 TB.
```

That 5-minute gap means a reboot, a 90-second GC pause, or a kernel upgrade
costs a few seconds of lease movement and **zero** data movement.

**Cost accepted:** A genuinely dead node leaves ranges under-replicated for
5 minutes.
**Flips when:** Never — but the *constants* scale with recovery cost. If
re-replication took 30 seconds instead of 40 minutes, you'd shorten the gap.

**The cascade defense — worth carrying to every distributed design:**

```
if > 20% of nodes are simultaneously non-live:
    STOP all re-replication.
```

That is not a node failure; it's a network or control-plane event, and moving
data will make it worse. **When a large fraction of the system looks broken,
the correct action is to do less, not more.**

**Failover timeline:** liveness expiry (≤9 s) → epoch bump → randomized
election timeout 1.0–1.5 s (randomized because 1,850 groups campaigning
simultaneously is a message storm) → new leases → **~4.5 s total per range**.
Graceful shutdown transfers leases proactively and drops that to ~10 ms —
which is why "always drain, never `kill -9`" is a hard rule.

---

## 10. Idempotency & dedup

**Choice:** `idempotency_token` is a **required** field on every mutating call,
and it is recorded *in the Raft log entry*.
**Forced by:** A client that times out cannot distinguish "didn't commit" from
"committed but the ack was lost." Both look identical.
**In one breath:** The client names the write; the server remembers the name
and the result, so a retry returns the original answer instead of applying
twice.
**The number:**

| Timing of failure | Client sees | Actually happened |
|---|---|---|
| Before Raft commit | Timeout | Nothing |
| **After commit, before ack** | Timeout | **The write happened** |
| After ack | Success | Happened |

Rows 1 and 2 are indistinguishable to the client. That's the entire argument.

**Cost accepted:** A slightly noisier API and a token cache per range.
**Flips when:** Never for mutations. Naturally idempotent operations
(`Put` of a fixed value) can skip it; `Increment` absolutely cannot.

**Two sub-decisions that are easy to get wrong:**
- **Required, not optional.** Optional means most callers omit it and discover
  the problem during their first failover. A required field with a
  library-generated default costs the caller nothing.
- **In the replicated log, not just in memory.** An in-memory-only token cache
  silently stops deduplicating at exactly the moment retries are most likely:
  failover.

---

## 11. Backpressure, overload, and metastability

**Choice:** Client-side retry budget (10% cap) + server-side shedding on
*observed queue latency*, not on a configured rate.
**Forced by:** The availability arithmetic. This is the single largest line
item in the error budget.
**In one breath:** When the system slows down, clients naturally send *more*
traffic, which slows it further. Cap the loop's gain at the client, and shed
at the server based on how backed up you actually are.
**The number — the finding that reorders the whole design's priorities:**

```
Consensus failover        9 s/yr      0.3%   ← what everyone studies
AZ loss                   9 s/yr      0.3%
Bad deploy              480 s/yr       15%
Overload / metastable   960 s/yr       30%   ← the largest single item
Network events          300 s/yr       10%
Operator error          600 s/yr       19%
──────────────────────────────────────────
Budget (99.99%)        3156 s/yr
```

**Raft failover spends 0.3% of the error budget. Deploys, overload, and humans
spend ~65%.** In a CP store, the availability risk is not CAP.

**Cost accepted:** Some requests fail that a retry would have served.
**Flips when:** Never. This applies at *every* scale — 3 nodes are easier to
overload than 54, not harder.

**The two mechanisms, both worth carrying everywhere:**

```
RETRY BUDGET (client):  retries ≤ 10% of the client's own request rate.
   Offered load can never exceed 1.1× normal, no matter how bad it gets.
   Without it: unbounded. This is THE metastability defense.
   Plus FULL jitter — sleep = random(0, backoff), not backoff/2 + random.
   At 10k clients, clients that failed together retry together forever.

QUEUE-LATENCY SHEDDING (server):
   queue_wait p50 < 5 ms   → admit all
   5–20 ms                 → shed BACKGROUND
   20–50 ms                → shed BACKGROUND + NORMAL over token rate
   > 50 ms                 → CRITICAL only
```

**A rate limit tuned for a healthy cluster is wrong the moment the cluster is
unhealthy — which is exactly when it needs to be right.** The queue itself is
the signal. And shedding must be *cheaper* than serving: the check happens at
the very front of the request path, before the leaseholder check.

**Metastability signature to alarm on:** request rate *above* baseline while
success rate is *below* baseline. The system is doing more work and
accomplishing less. Better than either metric alone.

---

## 12. Failure domains & placement

**Choice:** AZ diversity is a **hard** constraint the allocator will never
violate, even under pressure.
**Forced by:** "Survives an AZ loss with zero downtime." That guarantee is
*produced* by the placement constraint — it does not come from RF=3 alone.
**In one breath:** Three copies only survive a datacenter loss if they're in
three datacenters, and the system must refuse to "helpfully" fix
under-replication by breaking that.
**The number:** 1 replica per range per AZ → an AZ loss leaves 2 of 3 = quorum.
Rebuilding into a surviving AZ would leave 2 replicas there, and the *next* AZ
failure loses quorum.
**Cost accepted:** During an AZ outage, ranges stay under-replicated rather
than being restored to RF=3. Deliberately.
**Flips when:** More AZs available → RF=5 across 5 AZs becomes possible and
tolerates two simultaneous AZ losses.

**The trap, stated plainly:** a naive allocator restores replication factor by
doubling up in a surviving AZ. It looks correct on the "under-replicated
ranges" dashboard — the number goes to zero — and it has silently converted a
survivable failure into a fatal one. **Accept under-replication over losing
diversity.**

---

## 13. Geo-distribution — what the speed of light charges

**Choice:** Single-region default (3 AZs); multi-region **opt-in per
namespace**, four named topologies.
**Forced by:** Cross-region consensus costs 20–90× on writes. Making everyone
pay it to serve the minority who need it is how a platform gets routed around.
**In one breath:** A quorum needs a round trip to another region, and no
protocol beats the speed of light — so keep writes local unless the requirement
is real, and serve reads locally from slightly-stale replicas.
**The number — state this before anything else in a geo discussion:**

```
us-east ↔ eu-west  75 ms      us-east ↔ ap-southeast  230 ms

RF=3, one per region, leader in us-east:
  commit                = nearest remote ack        =  75 ms
  client in us-east     = 75 + 1                    =  76 ms
  client in ap-southeast= 75 + 230                  = 305 ms
vs single-region: 3.5 ms
```

Most requests for "globally consistent" evaporate when the requester sees
305 ms.

| Topology | Write (home) | Survives AZ loss | Survives region loss | Storage |
|---|---|---|---|---|
| 3 × 1 region | 3.5 ms | Yes | No | 3× |
| 1+1+1 | 60 ms | Yes | Yes | 3× |
| 3+1+1 (RF=5) | 3.5 ms | Yes | **Reads only** | 5× |
| 2+2+1 (RF=5) | 60 ms | Yes | Yes | 5× |

**Cost accepted:** Tenants must choose consciously, and a wrong choice is a
migration.
**Flips when:** Data has a legal residency requirement → region pinning is
mandatory regardless of latency.

**The trap in row 3:** `3+1+1` gives fast local-quorum writes, but losing the
home region leaves 2 of 5 — **no quorum**. Fast *and* AZ-durable *and* not
region-available. Legitimate, but it must be labelled.

**What rescues multi-region:** follower reads. `bounded_staleness(5s)` served
locally at ~1 ms instead of 75–305 ms. The realistic design is *writes go to
the home region and commit globally; reads are local and bounded-stale; the
handful of operations that truly need global linearizability pay 305 ms and
are known by name.*

---

## 14. Backup vs replication — different threat models

**Choice:** Backups in a **different format**, in a **different account**, with
object-lock. Plus namespace-level restore, not just full-cluster.
**Forced by:** Replication does not defend against the two most likely causes
of actual data loss.
**In one breath:** Three replicas run the same code on the same input, so a bug
that corrupts one corrupts all three — and the credential that can delete the
cluster can usually delete its backups too.
**The number:**

```
P(3 correlated hardware failures)  ≈ 5 × 10⁻⁷/yr   ← what replication covers
P(a compaction/GC/apply-path bug)  ≫ that          ← what it doesn't
P(an operator deleting a namespace) ≫ that          ← what it doesn't
```

**Cost accepted:** Backup export competes for I/O (mitigated: export from
*followers*, not leaseholders). 3 hours for a 50 TB full backup.
**Flips when:** Never. The "different format, different account" detail is the
entire point — a backup written by the same code that corrupted the data, into
a bucket the same credential can delete, defends against neither threat.

**The feature that actually gets used:** namespace-level restore into a staging
namespace, verified by the tenant, then swapped. Minutes, no impact on other
tenants. Full-cluster restore (~2.5 h for 50 TB) is the disaster plan;
single-namespace restore is Tuesday. **Building only the former is a common
and expensive omission.**

**Related:** anti-entropy here is a *detector*, not a repair loop. Under Raft,
divergence is impossible by protocol — so a digest mismatch is a **bug**, and
the response is to page and mark the range read-only. Auto-repairing would
destroy the evidence needed to find the bug. (In a Dynamo-style store, Merkle
repair is core machinery running constantly — same mechanism, opposite
purpose.)

---

## The index card

Everything above, condensed to what you'd produce in the first five minutes of
a whiteboard. If you can recite this cold, the sheet has done its job.

```
SCALE      100B keys · 50 TB · 1M w/s · 5M r/s · 54 nodes · 3 AZs

PARTITION  Range (forced by Scan), 512 MiB → 100k ranges, 5.6k replicas/node
REPLICATE  Raft per range, RF=3, quorum 2, 1 replica per AZ (HARD constraint)
WRITE      3.5 ms P50: leader fsync 0.3 ‖ follower ack 1.4 · batch+pipeline
READ       ReadIndex default 1.9 ms (clock-free, batched)
           leases 0.9 ms (opt-in) · follower reads 0.6 ms (≥3 s stale)
ENGINE     LSM tiered-over-leveled. WA 5 not 15 → 41 nodes not 122
CAPACITY   Endurance binds, not disk. MVCC garbage: 25h TTL = 135 TB → use 4h

HOT KEY    Can't split. Read → watch+cache. Write → batch+coalesce, then shard
HOT RANGE  Load-based split. Automatic. Different problem.
FAILOVER   Liveness 9 s (cheap) / replacement 5 min (expensive) → 4.5 s blip
           Graceful drain → 10 ms. Stop re-replication if >20% non-live.
IDEMPOTENT Token REQUIRED, stored IN the Raft log (survives failover)
OVERLOAD   Retry budget 10% + queue-latency shedding. 30% of the error budget.
GEO        75–305 ms for global writes. Opt-in. Follower reads rescue it.
BACKUP     Different format, different account. Defends what RF=3 cannot.

BUDGET     Raft failover = 0.3% of downtime. Deploys+overload+humans = 65%.
```
