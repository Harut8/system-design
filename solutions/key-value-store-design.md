# Highly Available, Strongly Consistent Key-Value Store: Design Document

> Solution to [`tasks/key-value-store.md`](../tasks/key-value-store.md).

---

## Table of Contents

1. [Requirements Clarification](#1-requirements-clarification)
2. [The CAP Decision, Made Explicit](#2-the-cap-decision-made-explicit)
3. [Architecture Selection: Two Candidates](#3-architecture-selection-two-candidates)
4. [Capacity Estimates](#4-capacity-estimates)
5. [High-Level Architecture](#5-high-level-architecture)
6. [Partitioning](#6-partitioning)
7. [Replication and Consensus](#7-replication-and-consensus)
8. [The Write Path](#8-the-write-path)
9. [The Read Path](#9-the-read-path)
10. [Consistency Model, Stated Precisely](#10-consistency-model-stated-precisely)
11. [Storage Engine](#11-storage-engine)
12. [Failure Detection and Recovery](#12-failure-detection-and-recovery)
13. [Availability Arithmetic](#13-availability-arithmetic)
14. [Hot Keys and Hot Partitions](#14-hot-keys-and-hot-partitions)
15. [Multi-Region](#15-multi-region)
16. [Watches, Leases, and TTL](#16-watches-leases-and-ttl)
17. [API Design](#17-api-design)
18. [Client Library](#18-client-library)
19. [Multi-Tenancy and Admission Control](#19-multi-tenancy-and-admission-control)
20. [Anti-Entropy, Backup, Restore](#20-anti-entropy-backup-restore)
21. [Observability and SLOs](#21-observability-and-slos)
22. [Failure Walkthrough](#22-failure-walkthrough)
23. [Trade-offs and Design Decisions](#23-trade-offs-and-design-decisions)
24. [Evolution Path](#24-evolution-path)
25. [Variant A: The AP Store](#25-variant-a-the-ap-store)
26. [Variant B: Global Linearizability](#26-variant-b-global-linearizability)
27. [Variant C: Small Scale](#27-variant-c-small-scale)
28. [Variant D: The Migration](#28-variant-d-the-migration)
29. [Stretch Problems](#29-stretch-problems)
30. [Exercises](#30-exercises)

---

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|----------|----------|--------|
| **Consistency** | What does "strongly consistent" mean here? | **Linearizable** per key: every operation appears to take effect atomically at some instant between its invocation and its response |
| **Consistency** | Multi-key atomicity? | Atomic within a partition. Cross-partition transactions are explicitly **out of scope** for v1 (see §29.1) |
| **Consistency** | Session guarantees across replicas? | Read-your-writes and monotonic reads must hold even on weaker read levels — enforced by a client-carried revision token |
| **Scale** | Logical data? | 50 TB, ~100 billion keys, avg record ~500 B |
| **Scale** | Sustained write rate? | 1M writes/sec; 2M peak |
| **Scale** | Sustained read rate? | 5M reads/sec; 10M peak |
| **Latency** | Write P99 / P99.9? | 10 ms / 50 ms, same-region |
| **Latency** | Linearizable read P99? | 5 ms |
| **Availability** | Target? | 99.99% (52 min/year) for linearizable ops, ≥ 99.999% for eventual reads |
| **Availability** | Failure domains to survive with zero downtime? | Node, rack, AZ |
| **Durability** | Acknowledged-write loss tolerance? | ≤ 1 per 10⁹ writes; must survive simultaneous loss of one node *and* one AZ |
| **Failure model** | Byzantine? | No — crash-stop with fail-fast disks. Checksums catch corruption; we do not defend against malicious replicas |
| **Clocks** | May we assume bounded skew? | **We design so the correctness of reads does not depend on it.** Clocks are used for TTLs and for bounded-staleness *labels*, never for linearizability |
| **Ops** | Team size? | 4–6 engineers. This constrains the design more than the scale numbers do |

### Key Assumptions

1. **Read-heavy but not read-only** — 5:1 read:write. Both paths matter; neither dominates enough to sacrifice the other.
2. **Keys are small, values are mostly small** — P50 value 200 B, P99 ~8 KB, hard cap 1 MB. The 1 MB tail is rare and must not be allowed to set the tail latency for everyone.
3. **Access is skewed** — the top 1% of keys take ~40% of the reads. Caching and follower reads matter enormously; a design that ignores skew will be sized wrong by 3×.
4. **Scans are prefix scans, not full-table scans.** Teams enumerate `/tenant/foo/*`, not the keyspace. This is the single strongest argument for range partitioning.
5. **Most tenants do not need global linearizability.** Regional linearizability plus explicit geo-replication for the few that do.
6. **Crash-stop, not Byzantine.** A replica may be arbitrarily slow, may lose its unsynced buffer, may come back with a stale disk — but it does not lie deliberately.

### What We Are Explicitly *Not* Promising

Writing this list first prevents the API from being over-sold, which is the way storage platforms actually fail:

- **Not** serializable multi-key transactions across partitions.
- **Not** a global total order of watch events across partitions (per-partition order plus a resolved timestamp — §16).
- **Not** availability of linearizable operations to a client stranded in a minority partition.
- **Not** a queue, a search index, or an analytics store. Range scans are for enumeration, not aggregation.

---

## 2. The CAP Decision, Made Explicit

The task asks for a store that is both highly available and strongly consistent. CAP says that during a network partition you may have one or the other. This is not a paradox to be argued away; it is a design decision to be *scoped*.

### The decision

**We choose CP.** During a partition, the minority side refuses linearizable operations rather than returning a value that might be stale or accepting a write that might be lost.

But "CP" is often taken to mean "unavailable during partitions," which is a misreading. The precise statement is:

> A partition makes **linearizable operations on the affected partitions** unavailable **to clients on the minority side**, for the duration of the partition.

Everything in that sentence is a lever, and the whole design is an exercise in shrinking each one:

| Lever | Mechanism | Result |
|---|---|---|
| "affected partitions" | 100k independent Raft groups, not one cluster-wide consensus | A partition affects only the ranges whose quorum it breaks |
| "minority side" | 3 replicas across 3 AZs, so an AZ partition leaves 2/3 = majority | The common failure leaves a majority reachable from almost everywhere |
| "linearizable operations" | Three named read levels; `bounded_staleness` and `eventual` are served by any replica | Reads stay available on the minority side, explicitly labeled |
| "for the duration" | Fast failover (§13): ~4.5 s to move a leader | A transient partition costs seconds, not the length of the outage |

So the honest headline is: **linearizable operations are available whenever a majority of replicas is reachable, and we engineer the topology so that a majority is reachable in essentially every failure short of losing two of three AZs.** That gets us to 99.99% without lying about consistency.

### PACELC

CAP only describes the partitioned case. PACELC asks what you trade the rest of the time.

**We are PC/EL.** Under Partition, Consistency. Else, Latency.

The "EL" half is where most of the engineering goes. A naive linearizable read costs a full consensus round trip on every read — at 10M reads/sec that is both slow and absurd. §9 spends its length on making linearizable reads cost approximately one local disk lookup while remaining genuinely linearizable.

### Why not AP

An AP store (Variant A, §25) is a legitimate design and we cost it out fully. It loses:

- **CompareAndSwap.** Without a total order per key, CAS is not implementable; you get "CAS that usually works," which is worse than no CAS because teams will build locks on it.
- **Leases and leader election.** Same reason. These are the two things internal platform teams most want from a KV store.
- **Atomic counters** become CRDT counters — correct, but they cannot be decremented below zero, cannot be conditionally applied, and cannot be read exactly.
- **Gap-free ordered watches** become "eventually you see all versions, in an order you must reconcile."

Four of the seven functional requirements would have to be deleted or footnoted. The store's purpose — being the thing teams build coordination on — is exactly the purpose AP cannot serve.

---

## 3. Architecture Selection: Two Candidates

Two shapes dominate this problem. Choosing between them is the design's first real decision.

### Candidate 1: Dynamo-style quorum replication

Consistent hashing ring, N replicas per key, coordinator-driven `W`/`R` quorums, read repair, hinted handoff, Merkle-tree anti-entropy. Cassandra, Riak, original Dynamo.

```
Put:  coordinator → N replicas in parallel → wait for W acks
Get:  coordinator → N replicas in parallel → wait for R responses → reconcile
```

**With W + R > N you get quorum intersection, but not linearizability.** The distinction is worth being precise about, because it is the most commonly fudged point in this design:

- A read quorum is guaranteed to *intersect* a write quorum, so it will see the latest **committed** value. But a failed or in-flight write that reached only some replicas leaves the system in a state where two successive reads can return new-then-old. There is no commit point and no rollback.
- Concurrent writes to the same key produce siblings that must be reconciled by last-writer-wins (which silently drops data and depends on clocks) or by version vectors (which push reconciliation into every client).
- Read repair makes convergence *eventual*, not immediate.

Getting real linearizability out of a quorum store requires adding a consensus protocol on top of it anyway (Cassandra's LWT uses Paxos, at roughly 4× the latency of a normal write).

### Candidate 2: Partitioned consensus (Raft per range)

The keyspace is split into ranges; each range is an independent Raft group with 3 voting replicas; the Raft leader (holding a lease) serves all reads and writes for that range. Spanner, CockroachDB, TiKV, YugabyteDB, etcd (single group).

```
Put:  client → leaseholder → Raft append → quorum fsync → apply → ack
Get:  client → leaseholder → local MVCC read (after a leadership check)
```

**Linearizability falls out of the protocol** rather than being bolted on: the Raft log is a total order, and every operation on the key goes through it.

### The comparison

| Dimension | Dynamo quorums | Raft per range |
|---|---|---|
| Linearizable single-key ops | Requires an extra Paxos round (~4× cost) | Native |
| CAS / PutIfAbsent / leases | Only via that extra round | Native |
| Write availability during partition | Total (sloppy quorum + hinted handoff) | Majority side only |
| Write latency, healthy | 1 RTT to the `W`-th replica | 1 RTT to the quorum replica — same |
| Read latency, linearizable | `R` replicas + reconcile + repair | 1 local read (with lease) |
| Ordered scans | Awkward — hash-partitioned | Native — range-partitioned |
| Gap-free watches | Hard; no per-key total order | Native — the Raft log *is* the event stream |
| Conflict handling | Client-visible siblings or LWW data loss | None; conflicts are ordered, not merged |
| Failure of a majority of one partition's replicas | Still writable (sloppy quorum) | Unavailable until recovery |
| Operational complexity | Anti-entropy repair is a permanent background chore and a permanent source of pages | Membership changes and snapshot traffic are the chores |

### Decision

**Range-partitioned Raft groups**, with three named read levels layered on top so that weaker-but-always-available reads remain a first-class, explicitly-requested option.

This buys the four functional requirements AP cannot serve (CAS, leases, ordered watches, exact counters), gives ordered scans for free from range partitioning, and confines the availability cost to "the minority side of a partition loses linearizable ops on the affected ranges" — which §13 shows is a few seconds per year in practice.

The cost we accept: **hot ranges are a real constraint** (a range has exactly one leaseholder, §14), and **range splits/merges/rebalancing is machinery we must build and operate** that a hash ring does not need.

---

## 4. Capacity Estimates

### Data volume

```
Keys:                     100 × 10⁹
Avg key length:            60 B
Avg value length:         440 B
MVCC overhead:            ~24 B (timestamp + version header)
Logical bytes/record:     ~524 B  →  round to 500 B

Logical dataset:          100e9 × 500 B          =  50 TB
Replicated (RF=3):        50 TB × 3              = 150 TB
MVCC garbage (25h GC TTL,
  1M writes/s × 90ks × 500B × 3)                 =  ~135 TB  ← see note
LSM space amplification:  × 1.3 (leveled)
```

**The MVCC-garbage line is the surprise, and it is the first thing a naive estimate gets wrong.** At 1M writes/sec with a 25-hour garbage-collection TTL, retained old versions weigh more than the live dataset. Two mitigations:

- **Cut the GC TTL to 4 hours** for namespaces that do not use long bounded-staleness reads or incremental backups off the MVCC store: 4h × 1M/s × 500 B × 3 = 21.6 TB.
- **GC aggressively per key**, keeping only versions newer than the TTL *plus one*, rather than a fixed window of all versions.

With a 4-hour default TTL:

```
Physical bytes  = (150 TB live + 22 TB garbage) × 1.3  ≈ 224 TB
```

### Node count — set by write amplification, not by capacity

The obvious sizing is by disk:

```
Per node: 8 TB NVMe, target 55% fill (compaction + rebalance headroom) = 4.4 TB
224 TB / 4.4 TB = 51 nodes
```

But that number is wrong until you check the write path against SSD endurance:

```
Replica-level write rate (sustained):  1M w/s × 3 replicas    = 3M/s
Bytes into the LSM:                    3M/s × 500 B           = 1.5 GB/s
× write amplification:
    leveled compaction   (WA ≈ 15)  →  22.5 GB/s cluster-wide
    tiered compaction    (WA ≈ 5)   →   7.5 GB/s cluster-wide

Endurance budget: 8 TB drive at 2 DWPD = 16 TB/day = 185 MB/s sustained
Nodes required:
    leveled:  22.5 GB/s ÷ 185 MB/s  =  122 nodes
    tiered:    7.5 GB/s ÷ 185 MB/s  =   41 nodes
```

**Compaction strategy, not dataset size, sets the node count — and it moves it by 3×.** Leveled compaction would force us to buy 122 nodes for 224 TB of data, i.e. to buy 2.4× the disk we need in order to buy write endurance.

**Decision: tiered (universal) compaction for the hot levels, leveled for the cold bottom level.** This is a read-amplification trade — tiered means more overlapping files to check per read — paid for with memory (bloom filters, block cache) rather than with 80 extra machines.

```
Final: 51 nodes (capacity-bound), rounded to 54 = 18 per AZ × 3 AZs
```

Capacity binds again, which is the right place to be: it means the cluster grows with data, predictably, rather than with a write-amplification cliff.

### Per-node resources

```
Disk:     8 TB NVMe (2 DWPD), ~4.1 TB used at 54 nodes
RAM:      256 GB
  block cache            96 GB
  memtables (write buf)  16 GB
  bloom filters          ~9 GB   (6.2e9 keys/node × 10 bits, partitioned+pinned)
  Raft state + inflight  16 GB
  OS / page cache / heap remainder

CPU:      32 vCPU
  writes:  2M/s peak ÷ 54 × 3 replicas ≈ 110k replica-applies/s/node
  reads:   10M/s peak ÷ 54            ≈ 185k reads/s/node
  → ~10 µs CPU budget per op at 60% utilization on 32 cores. Tight but real;
    this is why the read path must not allocate.

Network:  25 Gbps
  replication out: 110k/s × 500 B × 2 followers = 110 MB/s
  client traffic:  185k reads/s × 500 B          =  93 MB/s
  rebalance/snapshots: throttled to 256 MB/s per node
  → ~2 Gbps steady, 25 Gbps sized for recovery bursts
```

### Range count

```
Target range size:     512 MiB
Live data:             50 TB / 512 MiB      ≈ 100,000 ranges
Replicas:              × 3                  = 300,000 replicas
Per node:              300,000 / 54         ≈ 5,600 replicas
Leaseholders per node: ≈ 1,850
```

**Why 512 MiB and not 64 MiB.** Range size trades rebalance granularity against per-replica overhead. Raft needs a heartbeat per group per tick; at 64 MiB we would have 45,000 replicas per node and a heartbeat storm. Two mechanisms make even 5,600 comfortable:

- **Coalesced heartbeats** — one message per node pair per tick carrying heartbeats for all shared groups, turning O(replicas) messages into O(nodes).
- **Quiescence** — a range with no writes and a fully-caught-up followers set stops ticking entirely until the next write. At any moment the large majority of 100k ranges are quiescent.

512 MiB also keeps a full range snapshot (for re-replication) to a ~2-second transfer at 256 MB/s, which keeps recovery granular.

### Latency budget: `Put` P99 ≤ 10 ms

```
client → gateway (same AZ)                     0.3 ms
gateway → leaseholder (cross-AZ within region) 0.7 ms
leaseholder: sequence, MVCC encode, propose    0.1 ms
Raft append + local WAL fsync (NVMe)           0.3 ms
  ‖ parallel: replicate to 2 followers
    network cross-AZ RTT                       1.0 ms
    follower WAL fsync                         0.3 ms
  → first follower ack at                      ~1.4 ms
commit (leader + 1 follower = quorum of 3)     1.4 ms
apply to memtable + respond                    0.2 ms
return path                                    1.0 ms
──────────────────────────────────────────────────────
P50 total                                      ~3.5 ms
```

The 10 ms P99 headroom absorbs queueing, Raft batch waits, and — the real one — **LSM write stalls when L0 file count spikes**. That is the mechanism that will actually blow the P99, and §21 makes L0 file count a first-class SLO signal for exactly that reason.

### Latency budget: linearizable `Get` P99 ≤ 5 ms

```
client → leaseholder (routing cache hit, same AZ)  0.4 ms
leadership validation (lease valid: free;
  ReadIndex path: +1 quorum RTT, batched)          0.0–1.0 ms
MVCC read: memtable + block cache hit              0.05 ms
  (block cache miss: + 0.15 ms NVMe read)
return                                             0.4 ms
──────────────────────────────────────────────────────
P50 with lease       ~0.9 ms
P50 with ReadIndex   ~1.9 ms
```

Both fit. This is the number that justifies §9's conclusion that we can afford the clock-free read path.

---

## 5. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                            CLIENT APPLICATIONS                                │
│              (smart client library: routing cache, retries, tokens)           │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │  gRPC
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                          GATEWAY / ROUTING LAYER                              │
│         stateless · auth · per-tenant quota · request → range lookup          │
│    (co-located with storage nodes; any node can gateway for any range)        │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
        ┌─────────────────────────────┼─────────────────────────────┐
        ▼                             ▼                             ▼
┌───────────────┐             ┌───────────────┐             ┌───────────────┐
│     AZ-a      │             │     AZ-b      │             │     AZ-c      │
│  18 nodes     │             │  18 nodes     │             │  18 nodes     │
└───────────────┘             └───────────────┘             └───────────────┘
        │                             │                             │
        └─────────────────────────────┴─────────────────────────────┘
                                      │
┌──────────────────────────────────────────────────────────────────────────────┐
│                              STORAGE NODE                                     │
│                                                                               │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  Range Replicas  (≈5,600 per node, ≈1,850 of them leaseholders)         │  │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐                                │  │
│  │  │ Raft grp │ │ Raft grp │ │ Raft grp │   …  coalesced heartbeats,     │  │
│  │  │  r-0412  │ │  r-0413  │ │  r-9981  │      quiescence when idle      │  │
│  │  └──────────┘ └──────────┘ └──────────┘                                │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                                    │                                          │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  Admission Control  — per-tenant token buckets, queue-latency shedding  │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                                    │                                          │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  Storage Engine — ONE LSM instance per node (Pebble / RocksDB)          │  │
│  │  ranges are key-prefix subspaces, not separate engines                  │  │
│  │  shared WAL · shared block cache · shared compaction scheduler          │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                                    │                                          │
│                         ┌──────────┴──────────┐                              │
│                         │   NVMe (8 TB)       │                              │
│                         └─────────────────────┘                              │
└──────────────────────────────────────────────────────────────────────────────┘

  Cluster-wide services, all self-hosted on the same ranges:
  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐  ┌──────────────┐
  │  Meta ranges   │  │ Node liveness  │  │   Allocator    │  │   Gossip     │
  │ (range → node) │  │  (one range)   │  │ (rebalancing)  │  │ (topology)   │
  └────────────────┘  └────────────────┘  └────────────────┘  └──────────────┘
```

### Three architectural choices visible in that diagram

**1. One storage engine per node, not one per range.**
5,600 independent LSM instances per node would mean 5,600 memtables, 5,600 WALs, and 5,600 compaction schedulers competing blindly. Instead each range is a key-prefix subspace of a single node-wide engine: one WAL (one fsync amortized across all ranges' concurrent writes — a large P99 win), one block cache sized by actual hotness rather than statically partitioned, one compaction scheduler that can see global I/O pressure.

The cost: a range cannot be moved by copying a file. Snapshots are logical range scans. Given 512 MiB ranges at 256 MB/s, that is fine.

**2. No separate coordination cluster.**
A ZooKeeper or etcd cluster holding the range map is the obvious design and we reject it: it is a second consensus system to operate, a second thing to upgrade, and a hard scaling ceiling on range-map lookups.

Instead the range map is **stored in the store itself**, in ranges that are addressed by a two-level index:

```
meta1 (single range, never splits, replicated to every node's gossip)
  └─→ meta2 (ranges covering the range-map keyspace)
        └─→ user ranges
```

Bootstrapping: every node gossips the location of `meta1`. One `meta1` lookup finds the right `meta2` range; one `meta2` lookup finds the user range. Both are cached in every client and gateway, so the steady-state cost is zero. With 100k ranges at ~256 B of descriptor each, the whole `meta2` layer is ~25 MB — a handful of ranges, trivially cacheable.

**3. Gateways are not a separate tier.**
Every storage node can act as a gateway for any request. The client library talks directly to the node holding the leaseholder in the common case; if it is wrong, whatever node it hit forwards the request and returns a routing hint. This removes a network hop and a tier to capacity-plan, at the cost of putting tenant auth and quota logic on the storage nodes.

---

## 6. Partitioning

### Range partitioning, not hash partitioning

| | Hash | Range |
|---|---|---|
| `Scan(start, end)` | Fan-out to every partition, merge, discard | Contiguous — touches ⌈span/512 MiB⌉ ranges |
| Load distribution | Uniform by construction | Requires active splitting |
| Sequential-key hot spotting | Immune | Vulnerable — needs mitigation |
| Partition count changes | Resharding the whole ring, or virtual nodes | Split/merge a single range, locally |

Requirement 3 asks for ordered `Scan`. On a hash-partitioned store, a scan of a 1000-key prefix must query all ~100k partitions (or, with virtual nodes, all 54 physical nodes), gather everything in the range, sort, and discard the rest — turning a 50 ms P99 into an impossibility. **Range partitioning is not a preference here; it is forced by the scan requirement.**

The price is the sequential-write hot spot: a tenant writing `/events/2026-08-27T14:23:11Z` monotonically drives every write into the last range. §14 handles it.

### Split policy

A range splits when **either** trigger fires:

```
Size trigger:  range size > 768 MiB
               → split at the median key by size (computed from
                 SST-level key-count statistics, not by scanning)

Load trigger:  range QPS > 4,000/s sustained over 5 min
               → split at the key that most evenly divides observed
                 request load, sampled by a reservoir over request keys
```

The **load-based split** matters more than the size-based one. A 20 MiB range serving 50k reads/sec is a far worse problem than a 2 GiB range serving 10, and a size-only policy is blind to it.

Split is a Raft operation on the range itself: the leaseholder proposes a `Split(key)` command, which commits through the log; on apply, both halves exist with the same replica set, the same Raft group having been cloned. No data moves. Rebalancing the halves apart happens afterward, independently.

**Merges** run the reverse when two adjacent ranges are both under 128 MiB and jointly under 1,000 QPS, with a hysteresis window of 30 minutes to prevent split/merge thrash on a bursty workload. Merges require co-locating the two ranges' replica sets first, so they are slower and lower priority than splits — the asymmetry is deliberate: a missed split hurts now, a missed merge costs a little metadata.

### Placement constraints

The allocator places the 3 replicas of every range subject to hard constraints, then optimizes within them:

```
HARD (never violated, even under pressure):
  - no two replicas of a range on the same node
  - no two replicas of a range in the same AZ
  - replicas honor the tenant's placement policy (region pinning, §15)

SOFT (optimized continuously):
  - equalize replica count per node       (±5%)
  - equalize disk bytes per node          (±10%)
  - equalize leaseholder count per node   (±10%)
  - equalize QPS per node                 (±20%)
  - move leases toward the AZ issuing the most reads for that range
```

The AZ diversity constraint is what converts "3 replicas" into "survives an AZ loss." Enforcing it as *hard* means that when an AZ is down, the allocator will refuse to place the third replica rather than double up in a surviving AZ — accepting under-replication over losing the diversity guarantee. That is the right call, and it must be a deliberate one: a naive allocator "helpfully" restores replication factor by putting two replicas in one AZ, and the next AZ failure loses quorum.

### Rebalancing

Continuous, allocator-driven, no operator input. Every node reports its store metrics via gossip every 10 s. The allocator (a role held by whichever node holds the lease on a designated range — so it is singleton and failover is free) computes desired moves and issues them at a throttled rate.

```
Rebalance throttle:  256 MB/s per sending node, 2 concurrent snapshots
Priority order:      1. ranges with < 3 replicas   (recovery)
                     2. ranges violating a hard constraint
                     3. disk-fullness balance
                     4. QPS/lease balance
```

**Priority 1 must be able to preempt everything else**, and its throttle must be separate: recovery traffic competing with cosmetic balance traffic is how a single node failure becomes a cluster-wide latency event.

---

## 7. Replication and Consensus

### Raft, RF=3, one group per range

Each range is an independent Raft group with 3 voting replicas across 3 AZs. Quorum is 2.

**Why 3 and not 5.** RF=5 tolerates 2 simultaneous failures instead of 1, at 1.67× the storage cost and — more importantly — a *worse* write latency tail, since the commit waits for the 3rd-fastest of 5 rather than the 2nd-fastest of 3. Given 3 AZs, RF=5 also cannot maintain AZ diversity (some AZ holds 2). We use RF=3 as the default and RF=5 as a per-namespace option for tenants who want to survive an AZ failure *and* a concurrent node failure without any window of vulnerability.

**Why Raft and not Paxos/Viewstamped Replication.** Not a technical superiority claim — Multi-Paxos is equivalent in the steady state. Raft's advantage is that its membership-change protocol and log-matching invariants are precisely specified, so the implementation is auditable and the failure modes are ones we can reason about at 3am. For a 4–6 person team, "we can reason about it" outranks "it saves a message in an uncommon path."

### Membership changes without losing quorum

The classic bug: to replace a failed replica in a 3-node group, you remove the dead one (leaving 2, quorum 2 — one more failure is fatal) and then add the new one (3 with an empty log, quorum 2, but the new member cannot vote usefully until caught up).

We use **learners plus single-voter changes**:

```
1. Add the new replica as a LEARNER (non-voting).
   Group is still 3 voters, quorum 2. Availability unchanged.
2. Send it a snapshot of the range; catch it up on the Raft log.
   May take seconds. Availability unchanged throughout.
3. Atomic joint change: promote learner to voter AND demote the
   dead replica to a removed member, in one configuration entry.
   (Raft joint consensus: quorum during the transition requires a
   majority of BOTH the old and new configurations.)
4. Garbage-collect the old replica's data when it comes back.
```

Step 3 is where joint consensus earns its complexity: a single configuration entry that changes two members at once is safe only if quorum is computed over both configurations during the transition. Doing this as two separate single-member changes is also safe (and simpler) but passes through a 4-voter configuration with quorum 3 — briefly *less* fault tolerant than where we started. We use joint consensus.

### Leases

Raft guarantees a single leader per term, but a leader that has been partitioned away does not *know* it has been deposed, so it cannot safely serve reads from local state without checking. That check is the entire read-path problem (§9).

We layer a **range lease** on top of Raft. The leaseholder is normally the Raft leader (co-location is enforced by the allocator, since a non-co-located lease means every write pays an extra hop). The lease grants the exclusive right to serve reads and to propose writes for that range.

**Epoch-based leases, not time-based.** A time-based lease ("I hold this until wall-clock T") requires bounded clock skew for safety — if the old leaseholder's clock runs slow, it may serve a stale read after a new leaseholder was granted the lease. That is a correctness dependency on NTP, which we rejected in §1.

Instead, each node maintains a **liveness record** in one designated, always-replicated range:

```
node_liveness/{node_id} → { epoch: 7, expiration: <hlc> }
```

- A node heartbeats its own liveness record every 3 s, extending `expiration` by 9 s. This heartbeat is a Raft write to the liveness range, so it is linearizable.
- A range lease says "held by node 12, epoch 7."
- The lease is valid exactly as long as node 12's liveness record still shows epoch 7 and is unexpired.
- To steal the lease, another node must first **increment node 12's epoch** — a linearizable write. Once that write commits, node 12's old lease is permanently invalid, by the total order of the liveness range, regardless of what any clock says.

This converts "trust every node's clock" into "trust one Raft-replicated record," which is a much smaller assumption. Clock skew now affects only *liveness* (a badly-skewed node may fail to heartbeat and get its epoch bumped — an availability event, not a correctness one).

The residual clock dependency: the *expiration check* uses local time, so a node whose clock jumps forward may consider a valid lease expired and try to steal it — which is safe (the steal requires an epoch bump) but wasteful. §29.2 covers the clock-jump scenario in full.

### Write pipelining and batching

Raft's naive loop — propose, wait for commit, propose next — caps a range at 1/RTT ≈ 700 writes/sec. Two mechanisms lift that by two orders of magnitude:

- **Batching.** The leaseholder accumulates proposals for up to 500 µs (or 1 MB) and appends them as one Raft entry with one fsync and one round of AppendEntries. At 50k writes/sec to one range, that is 100 writes per entry: one fsync amortized 100×.
- **Pipelining.** The leader sends entry N+1 without waiting for N's quorum ack, tracking commit indices independently. In-flight window capped at 64 entries per group to bound memory across 1,850 leaseholders.

Together a single range sustains ~50k writes/sec, which is what makes §14's hot-key mitigation viable.

---

## 8. The Write Path

### End to end

```
1.  CLIENT
      Put(key="/inv/sku-4471", value=..., idempotency_token=<uuid>)
      Routing cache: key → range r-0412 → leaseholder node-27
      Send directly to node-27.

2.  NODE-27: ADMISSION CONTROL
      Per-tenant token bucket. Queue-latency check (§19).
      Reject early with RESOURCE_EXHAUSTED rather than queue deeply.

3.  NODE-27: LEASEHOLDER CHECK
      Do I hold the lease for r-0412?
        no  → return NotLeaseholderError{hint: node-41, range_desc}
              (client updates its cache and retries — one extra RTT,
               no server-side forwarding, so the client learns)
        yes → continue

4.  LATCHING
      Acquire a write latch on the key. Latches are per-range,
      in-memory, and held only for the duration of the operation —
      they order concurrent operations on the same key so the MVCC
      timestamps we assign are consistent with the Raft order.

5.  IDEMPOTENCY CHECK
      Look up idempotency_token in the range's recent-token cache
      (in-memory ring, 5 min TTL, also durably recorded in the Raft
      log alongside the write).
        hit  → return the ORIGINAL result. Do not re-apply.
        miss → continue

6.  READ-MODIFY (conditional ops only)
      For CAS / PutIfAbsent / Increment: read the current version
      from the local MVCC state. Because we hold the lease and the
      latch, this read is linearizable and no other write can
      interleave.
        precondition fails → return FAILED_PRECONDITION now,
                             WITHOUT a Raft round. Cheap rejection.

7.  PROPOSE
      Assign HLC timestamp. Encode the MVCC key:
          /inv/sku-4471 @ 1756304591.000000042
      Append to the range's Raft log (batched, §7).

8.  DURABILITY
      Leader appends to the node-wide WAL and fsyncs      (~0.3 ms)
      In parallel, AppendEntries to both followers; each
      appends to its own WAL and fsyncs before acking     (~1.4 ms)
      Commit when the leader has 2 of 3 (itself + one follower).

      ── THE DURABILITY POINT IS HERE ──
      An acknowledged write is on stable storage on ≥2 nodes in
      ≥2 AZs. It survives the immediate, total loss of either one.

9.  APPLY
      All three replicas apply the committed entry to their
      memtable independently. The leaseholder does not wait for
      followers to apply — only to durably append.

10. ACK
      Return { revision: 88214, timestamp: <hlc> } to the client.
      Release the latch. Record the idempotency token.

11. ASYNC
      Notify watchers on this range (§16).
      Advance the closed timestamp (§9).
      Memtable flush → L0 SST when the memtable fills.
```

### Points worth defending

**Why fsync on followers before acking.** Skipping it (acking on memory receipt) would cut ~0.3 ms from the commit path. It also means a correlated power event across two nodes — a rack PDU, a bad kernel update rolled out simultaneously, a cloud-provider host maintenance batch — loses committed writes. Correlated failures are exactly the ones that hit multiple replicas, so the optimization is unsafe precisely when it matters. **We fsync.** The 0.3 ms is bought back by batching (one fsync per ~100 writes under load).

**Why conditional operations evaluate before the Raft round.** A CAS whose precondition fails does not need to be replicated — nothing changed. Evaluating locally (safe, because we hold both the lease and the latch) turns a failed CAS into a sub-millisecond local operation. For contended locks, where most CAS attempts fail, this is the difference between a workable primitive and one that melts a range.

**Why the client is told to retry rather than being forwarded.** Server-side forwarding hides the routing error, so the client's cache stays wrong forever and every request pays two hops. Returning `NotLeaseholderError` with a hint costs one extra RTT once, then the client is correct. This is the same reasoning as an HTTP 307 with a `Location` header.

**Idempotency tokens are mandatory for retryable writes.** A client that times out on a `Put` does not know whether it committed. Without a token, the retry may double-apply an `Increment`. The token is recorded *in the Raft log entry*, so the deduplication survives a leaseholder failover — an in-memory-only token cache would silently stop deduplicating at exactly the moment (failover) when retries are most likely.

---

## 9. The Read Path

This is where PACELC's "EL" is earned. A read that goes through the Raft log is trivially linearizable and costs a full consensus round; at 10M reads/sec it would triple the cluster's fsync load to answer questions that change nothing.

### The problem

A leaseholder that has been network-partitioned from its followers still believes it holds the lease. If it serves a local read, and meanwhile a new leaseholder on the majority side has accepted writes, the read returns a stale value — a linearizability violation. Local reads therefore require a *proof of current leadership*.

Three mechanisms, in increasing cheapness and increasing assumption:

### (a) ReadIndex — the clock-free baseline

```
1. Leader records its current commit index as `read_index`.
2. Leader sends a heartbeat round to all followers and waits for
   a quorum of responses.  ← proves it is still leader NOW
3. Leader waits until its applied index ≥ read_index.
4. Serve the read from local state.
```

Cost: one quorum round trip of small messages, **no disk writes**. Crucially, **the heartbeat round is batched across all reads that arrive during it** — one confirmation round serves every pending read on that range. At 50k reads/sec on a range with a 1 ms heartbeat RTT, that is one round per ~50 reads, and the per-read marginal cost is nil.

From §4's budget: ReadIndex reads land around 1.9 ms P50, comfortably inside the 5 ms P99 target.

**This is our default.** It assumes nothing about clocks.

### (b) Leader lease reads — removing the round trip

If the lease is known valid, the leaseholder can serve immediately with no round trip at all (~0.9 ms P50). With epoch-based leases (§7), validity means "node N's liveness record still shows epoch E and has not expired," which is a local check against a gossiped/cached liveness state.

The subtlety: the *expiration* half of that check reads a local clock. Safety requires that a leaseholder stop serving before another node can bump its epoch and steal the lease. We enforce this with a **stasis interval**: a lease is treated as expired by its *holder* 2 s before it can be treated as expired by a *stealer*. Since the steal itself is a linearizable Raft write to the liveness range, and the holder has already stopped serving, no overlap is possible unless a clock is off by more than the 2 s stasis margin — a condition we detect and act on (§29.2: a node whose measured offset exceeds 500 ms removes itself from service).

**Available as a per-namespace opt-in** for latency-critical tenants who accept the stasis-margin assumption. Not the default.

### (c) Follower reads with closed timestamps — reads that scale past the leaseholder

Both mechanisms above funnel every linearizable read through one node per range. For read-heavy, latency-sensitive, staleness-tolerant workloads, we want reads served by *any* replica, including one in the client's own AZ or region.

The mechanism is the **closed timestamp**:

```
Every 200 ms, the leaseholder publishes to its followers:
    "I will never again accept a write at a timestamp ≤ T."
    where T = now − 3 s   (the closed-timestamp target lag)

A follower that has applied all entries up to the closed timestamp
can serve any read at a timestamp ≤ T from local state, and that
read is guaranteed to see exactly the same data the leaseholder
would have returned for that timestamp.
```

This is not a stale read in the "might be wrong" sense. It is an **exact read of a slightly earlier consistent snapshot** — every follower reading at timestamp T returns identical results, and those results reflect a real prefix of the total order. That distinction is what makes bounded staleness safe to build on.

The 3-second lag is a tunable: lower lag gives fresher follower reads but requires tighter closed-timestamp propagation and gives writers less slack.

### The three read levels, resolved

| Level | Mechanism | Latency P50 | Served by | Available during partition? |
|---|---|---|---|---|
| `linearizable` | ReadIndex (default) or lease | 1.9 ms / 0.9 ms | Leaseholder only | Majority side only |
| `bounded_staleness(t)` | Closed timestamp, `t ≥ 3 s` | 0.6 ms | Nearest replica | **Yes, both sides** (data ages during the partition; the API reports actual staleness) |
| `eventual` | Local read, no timestamp check | 0.5 ms | Any replica | **Yes, both sides** |

**This table is the practical answer to "highly available AND consistent."** The consistency level is a per-call decision, its cost is visible, and its availability during a partition is documented. A client on the minority side of a partition is not dead — it is degraded in a way it can detect (`bounded_staleness` returns the actual staleness with every response) and reason about.

### Session guarantees on weak reads

A client that writes at revision 88214 and then issues a `bounded_staleness` read could see a snapshot from before its own write — a read-your-writes violation, and the single most confusing thing a store can do to an application developer.

Fix: the client library tracks the **highest revision it has observed** (from writes and from reads) and attaches it to every subsequent request as `min_revision`. A replica that has not yet applied `min_revision` either waits briefly (up to 50 ms) or returns `RETRY_WITH_LEASEHOLDER`. This gives read-your-writes and monotonic reads at every consistency level, for free in the common case, without the client having to think about it.

---

## 10. Consistency Model, Stated Precisely

The contract, in the terms the literature uses, so that it can be tested rather than argued about:

### What we guarantee

**Linearizability, per key, for `linearizable`-level operations.**
There exists a single total order over all `Put`, `Delete`, `CAS`, `Increment`, and `linearizable Get` operations on a given key, consistent with real time: if operation A returns before operation B begins, A precedes B in the order. Every read returns the value written by the immediately preceding write in that order.

**Atomicity within a partition.**
A `BatchWrite` whose keys all fall in one range commits as a single Raft entry: all mutations apply or none do, and no read observes a partial state.

**Session guarantees at every consistency level.**
Read-your-writes, monotonic reads, monotonic writes, and writes-follow-reads, enforced by the client-carried `min_revision` token (§9). These hold even when the client is rebalanced onto a different replica or a different gateway mid-session.

**Prefix consistency for bounded-staleness reads.**
A bounded-staleness read at timestamp T reflects a genuine prefix of the total order — never a mixture of "some writes from time T and some from T+5."

**Durability of acknowledgement.**
An acknowledged write is fsynced on ≥2 nodes in ≥2 AZs (§8, step 8).

### What we do *not* guarantee

**No cross-range atomicity.** A `BatchWrite` spanning ranges is rejected, not silently split. (Rejecting is the important part — the alternative teaches teams that it works, until the day it half-applies.)

**No global total order across keys.** Two writes to keys in different ranges have no defined relative order unless one causally precedes the other via a client that observed the first. Watch events across a multi-range prefix are ordered by resolved timestamp, not by a global log position (§16).

**No linearizability for `bounded_staleness` or `eventual` reads.** They are labeled, they report their actual staleness, and they are the caller's explicit choice.

**Nothing during quorum loss.** If 2 of 3 replicas of a range are unreachable, that range serves no linearizable operations. It is not "eventually consistent" in that state — it is unavailable, deliberately (§29.3 covers the unsafe-recovery escape hatch).

### Why this precision matters

The failure mode this section prevents is real and common: a team reads "strongly consistent" on the tin, builds a distributed lock on `PutIfAbsent`, and does not learn until an incident that (a) the lock's TTL expiring does not stop a GC-paused holder from acting (§16 requires fencing tokens), or (b) their `bounded_staleness` read of the lock state was never linearizable in the first place. Documented guarantees, phrased in testable terms, are the mitigation.

---

## 11. Storage Engine

### LSM, not B-tree

| | B-tree | LSM |
|---|---|---|
| Write amplification | 1 page (4–16 KB) per 500 B write ≈ 8–32× | Tiered ≈ 5× |
| Write pattern | Random | Sequential |
| Read amplification | ~1 (index cached) | 1–10 (bloom-filtered) |
| Space amplification | 1.3–2× (fragmentation) | 1.1–1.3× (leveled), higher (tiered) |
| Range scan | Excellent — clustered | Good — merge iterator over sorted runs |
| MVCC / versions | Awkward — versions fragment pages | Natural — versions are just more keys |
| Deletes | In place | Tombstones (which must be reasoned about) |

Two arguments decide it:

1. **Random 500-byte writes at 3M/sec are the worst case for a B-tree.** Every write dirties a whole page; the write amplification calculation in §4 would be 3–6× worse, and the node count worse with it.
2. **MVCC wants an LSM.** Our key encoding is `user_key | timestamp_desc`, so old versions are simply adjacent keys that compaction will eventually drop. In a B-tree, versions of a hot key fragment its page and force splits.

**Engine: Pebble** (or RocksDB). One instance per node.

### Key encoding

```
Physical key:  <range_prefix> <user_key> 0x00 <hlc_timestamp_desc:12B>
Physical val:  <value> | <tombstone marker>

  hlc_timestamp_desc = bitwise-inverted (wall_clock:8B, logical:4B)
  so that DESCENDING timestamp order is ASCENDING byte order, and a
  seek to (user_key, 0) lands on the NEWEST version first.
```

The inversion matters: reading a key is a single forward seek that lands on the newest version, rather than a seek-to-end-and-scan-backward. Backward iteration in an LSM merge iterator is substantially more expensive than forward.

A read at timestamp T seeks to `(user_key, T_inverted)` and takes the first entry — which is, by construction, the newest version at or before T. Bounded-staleness reads and linearizable reads use the identical code path with a different T.

### Compaction

Per §4, compaction strategy sets the node count:

```
L0        tiered  — flushed memtables, overlapping ranges
L1–L4     tiered  — overlapping sorted runs, WA ≈ 5
L5+       leveled — non-overlapping, holds ~90% of bytes, compacted rarely
```

Tiered on top (where write pressure is), leveled at the bottom (where the bytes are, and where space amplification would otherwise be paid on 200 TB).

**Read amplification is the bill.** A read may check several overlapping runs. Paid for with:

- **Bloom filters, 10 bits/key**, ~1% false positive rate, partitioned with the top level pinned in memory (9 GB/node per §4). A bloom miss skips a run entirely without touching disk.
- **96 GB block cache per node.** With the assumed 1%-of-keys-take-40%-of-reads skew, the hot working set per node is roughly 4.1 TB × 1% ≈ 41 GB of key-space, comfortably resident.
- **Compaction priority by read pressure**: runs that are frequently probed get compacted sooner, so read-hot key spans naturally converge toward fewer overlapping runs.

### The L0 problem

The mechanism that will actually break the P99 SLO:

```
Write burst → memtables flush faster than compaction drains L0
           → L0 file count climbs
           → every read must check every L0 file (they overlap)
           → read latency climbs
           → at the stall threshold, the engine THROTTLES OR STOPS WRITES
           → write latency goes from 3 ms to seconds
```

Three defenses:

1. **L0 file count is a primary SLO signal** (§21), alarmed well before the stall threshold, not after.
2. **Admission control is driven by L0 health** (§19). When L0 depth exceeds a soft threshold, we shed low-priority writes *at admission* — a clean `RESOURCE_EXHAUSTED` to a background job — rather than letting the engine stall everyone including the latency-critical tenant.
3. **Compaction gets a reserved I/O budget** that foreground traffic cannot starve. Compaction is not background work; it is the mechanism that keeps foreground work fast, and treating it as preemptible is how a cluster enters the metastable state in §29.4.

### MVCC garbage collection

```
GC TTL default:     4 hours  (per-namespace override up to 25 h)
Policy:             retain all versions newer than now − TTL,
                    PLUS the single newest version at or before that
                    cutoff (so a read at the TTL boundary still works)
Tombstones:         retained for the full TTL, then dropped
Mechanism:          a compaction filter, so GC costs no extra I/O —
                    it happens during compaction we were doing anyway
```

The "plus one" rule is easy to omit and catastrophic if omitted: without it, a key written once a year and never touched again has *all* its versions garbage-collected and the key disappears.

**Tombstone accumulation** is the classic LSM operational trap. A tenant that writes and deletes a million keys per hour in the same key span leaves a million tombstones that every scan of that span must iterate over. Mitigations: `DeleteRange` for bulk deletes (one tombstone covering a span, not one per key), and a scan-time metric that alarms when the tombstone-to-live-key ratio in a range exceeds 10:1, triggering a targeted compaction.

---

## 12. Failure Detection and Recovery

### Distinguishing dead from slow

The hardest problem in the design, because the two are indistinguishable from outside and the correct responses are opposite: a dead node should be replaced immediately; a slow node should be left alone, because replacing it adds recovery load to an already-struggling cluster.

We use **two independent signals with different thresholds**:

```
LIVENESS (correctness-critical, fast, conservative):
    Node heartbeats its own liveness record every 3 s, extending
    expiration by 9 s. On expiry, another node may bump its epoch,
    invalidating its leases.
    → Detects "cannot participate in consensus" in ≤ 9 s.
    → This signal is CHEAP TO GET WRONG: a false positive costs a
      lease transfer (~4.5 s for the affected ranges), not data.

REPLACEMENT (expensive, slow, patient):
    A node's replicas are re-replicated elsewhere only after it has
    been non-live for 5 MINUTES continuously.
    → This signal is EXPENSIVE TO GET WRONG: re-replicating one
      node's 4.1 TB moves 4.1 TB of data across the cluster.
```

The 5-minute gap between the two is the single most important operational constant in the design. It means a node reboot, a 90-second GC pause, a kernel upgrade, or a brief network blip costs a few seconds of lease movement and *no* data movement. Only a genuinely departed node triggers the expensive path.

### Avoiding the recovery cascade

The failure mode: a node dies, re-replication saturates the network, latency rises across the cluster, more nodes miss heartbeats, more re-replication is triggered, and the cluster collapses. This is a metastable failure — the cluster does not recover when the original trigger is removed.

Defenses:

```
1. Rate limit:      256 MB/s per sending node, 2 concurrent snapshots
2. Circuit breaker: if > 20% of nodes are simultaneously non-live,
                    STOP all re-replication. This is not a node
                    failure; it is a network or control-plane event,
                    and moving data will make it worse.
3. Reserved capacity: recovery traffic uses a separate I/O and network
                    class with a floor, so it neither starves nor is
                    starved by foreground traffic.
4. Backpressure:    re-replication defers when target nodes report
                    L0 depth above the soft threshold.
```

Defense 2 is the one that specifically prevents the cascade, and it is worth stating as a principle: **when a large fraction of the cluster looks broken, the correct action is to do less, not more.**

### Failover timeline

```
t=0.0s   Leaseholder node fails (power loss)
t=0.0s   In-flight writes on ~1,850 ranges are now unacknowledged.
         Committed ones are durable on 2 other replicas; uncommitted
         ones will be resolved by the new leader (kept or discarded
         per the Raft log-matching rules) — the client sees a timeout
         and MUST retry with its idempotency token to learn the outcome.
t≤9.0s   Liveness record expires (worst case; typically ~6 s since
         the last successful heartbeat is on average 1.5 s old).
t+0.1s   Another node bumps the dead node's epoch (Raft write to the
         liveness range). All its leases are now invalid.
t+0.2s   Raft election timeouts fire on the affected groups
         (randomized 1.0–1.5 s to prevent simultaneous elections
         across 1,850 groups — without randomization, 1,850 groups
         campaign at once and produce a message storm).
t+1.5s   New leaders elected; new leases acquired.
t+2.0s   New leaseholders serve reads and writes.
         ── total write unavailability per affected range: ~4.5 s ──
t+5min   If still non-live: re-replication begins, restoring RF=3.
t+45min  RF=3 restored across all 5,600 ranges.
```

**Graceful shutdown skips almost all of this.** A node being drained for an upgrade proactively transfers each of its 1,850 leases and its Raft leaderships to a healthy peer before exiting. Per-range unavailability drops from 4.5 s to roughly the duration of one lease transfer, ~10 ms. This is why a rolling upgrade of all 54 nodes causes no measurable write downtime — and why "always drain, never `kill -9`" is a hard operational rule.

### What an in-flight write observes

| Timing | Client observes | Actual outcome |
|---|---|---|
| Failure before Raft commit | Timeout | Write did not happen |
| Failure after quorum commit, before ack | Timeout | **Write DID happen** |
| Failure after ack | Success | Write happened |

Row 2 is why idempotency tokens are non-negotiable. A client that times out cannot distinguish rows 1 and 2. Retrying with the same token yields the correct answer in both cases: if the write committed, the new leaseholder finds the token in the replicated log and returns the original result; if it did not, the retry applies it once.

---

## 13. Availability Arithmetic

The 99.99% target is 52.6 minutes per year. Where does it actually go?

### Consensus-induced unavailability is negligible

```
Per-range write unavailability from an ungraceful leaseholder loss:  4.5 s
Ungraceful node failures per node per year:                          ~2
A given range's leaseholder sits on one node, so:
    2 events/yr × 4.5 s = 9 s/yr  →  99.99997% availability

Rolling upgrades (12/yr, graceful lease transfer):
    12 × 10 ms = 0.12 s/yr  →  negligible

Range splits (lease held throughout, no unavailability):             0 s
Rebalancing (lease transferred gracefully):                          0 s
```

**Raft failover spends 0.3% of the error budget.** If the design's availability story ended here, we would be at five nines.

### Where the budget actually goes

```
Consensus failover                             9 s/yr    (0.3%)
AZ loss (2 events/yr × 4.5 s for the ~1/3
  of ranges whose leaseholder was there)       9 s/yr    (0.3%)
Bad config push / bad deploy
  (2 events/yr × 4 min mean, with fast rollback) 480 s/yr (15%)
Overload / metastable events
  (2 events/yr × 8 min)                        960 s/yr  (30%)
Cluster-wide network events                    300 s/yr  (10%)
Operator error                                 600 s/yr  (19%)
──────────────────────────────────────────────────────────
Total                                         ~2360 s/yr  = 99.9925%
Budget                                         3156 s/yr  = 99.99%
```

**The conclusion is the point of this section: the availability risk in a CP store is not CAP. It is deploys, overload, and humans.** Roughly 65% of the budget goes to things a consensus protocol has no opinion about.

Which means the availability engineering that matters is:

1. **Admission control and load shedding** (§19) — the single largest line item.
2. **Progressive rollout with automatic rollback** — canary one node, then one AZ, with SLO-triggered auto-revert. Turns a 30-minute bad-deploy outage into a 4-minute one.
3. **Every destructive operation gated and reversible** — decommission requires the cluster to confirm RF=3 elsewhere first; `DeleteRange` on a namespace requires a two-person approval; unsafe recovery (§29.3) requires an explicit override flag and writes an audit record.
4. **Drain, never kill.**

### Availability by consistency level

| Operation | Node loss | AZ loss | Region partition (single-region deploy) | 2-of-3 AZ loss |
|---|---|---|---|---|
| `linearizable` read/write | Available (4.5 s blip) | Available (4.5 s blip on ⅓ of ranges) | Unavailable | Unavailable |
| `bounded_staleness` read | Available | Available | Available, staleness grows | Available, staleness grows |
| `eventual` read | Available | Available | Available | Available |

### Durability arithmetic

Data loss requires all 3 replicas to be permanently lost before re-replication completes.

```
Node annualized failure rate (permanent):        2%
Cluster of 54 nodes:                             ~1.1 permanent failures/yr
Time at risk (detection 5 min + re-replication
  of 4.1 TB at ~2 GB/s aggregate ≈ 34 min):      ~39 min = 6.5e-4 of a year

P(a second specific-enough node fails in that window):
  1.1 failures/yr × 6.5e-4 yr                    ≈ 7.2e-4
  → at 100k ranges spread over 54 nodes, essentially any two nodes
    share ranges, so this is the probability of SOME range dropping
    to 1 replica: ~7 × 10⁻⁴ per year.
  Note this is UNAVAILABILITY (2 of 3 gone, no quorum), not loss —
  the data still exists on the surviving replica.

P(a third also fails within the remaining window):  ≈ 5 × 10⁻⁷/yr
```

Against a target of ≤1 loss per 10⁹ writes at 3×10¹³ writes/year, i.e. ≤3×10⁴ lost writes/year: a 5×10⁻⁷ annual probability of losing one range (≈500 MB, ≈10⁶ records) yields an expected ~0.5 lost records/year. Four orders of magnitude inside budget.

**The dominant real-world durability risk is not correlated hardware failure. It is software: a bug in compaction, GC, or the apply path that corrupts all three replicas identically**, since all three run the same code on the same input. Mitigations: block checksums verified on every read, the periodic cross-replica consistency checker (§20), and — the one that actually saves you — **backups to a different storage system in a different format** (§20), which is the only defense against a bug that all three replicas share.

---

## 14. Hot Keys and Hot Partitions

These are different problems with different solutions, and conflating them is the most common error in this design.

### Hot partition (a range gets too much traffic)

**Solvable by the system, automatically.** Load-based splitting (§6) detects a range above 4,000 QPS and splits it at the load median. Two ranges, two leaseholders, potentially two nodes. Repeat until the load per range is acceptable. The allocator then spreads the resulting ranges apart.

Sequential-key hot spots (`/events/<timestamp>`) are the pathological case: splitting the last range produces a new last range that receives *all* the writes, and the split does not help. Three responses:

1. **Detect and warn.** A range whose writes are ≥95% appends at the upper bound is flagged, and the tenant is told, with the specific key pattern, at onboarding rather than at 3am.
2. **Offer a hash-prefixed key helper in the client library.** `/events/<hash(id) % 64>/<timestamp>` spreads across 64 ranges. Scans become a 64-way merge, which the library does transparently. The tenant chooses this consciously, trading scan cost for write throughput.
3. **Pre-split on namespace creation** when the tenant declares a sequential access pattern.

### Hot key (one key gets too much traffic)

**Not solvable by splitting — a single key cannot be split.** It lives in one range, with one leaseholder, on one node. That node's single-range write ceiling is ~50k writes/sec (§7, with batching) and its read ceiling ~150k/sec.

Solutions, by workload:

**Read-hot key** (a feature flag read by every service, 2M reads/sec):
- `bounded_staleness` reads served by all 3 replicas: 3× headroom.
- **Watch-plus-cache is the real answer.** The client library caches the value and opens a watch on the key; reads are served from process memory at zero network cost and are invalidated within milliseconds of a change. A 2M reads/sec feature flag becomes ~1,000 open watches and one event per change. This is what the watch primitive exists for, and it should be documented as the *first* recommendation for read-hot keys, not the last.

**Write-hot key** (a counter incremented 500k times/sec):
- **Raft entry batching does most of the work.** 500k increments/sec batched at 500 µs produces 2,000 Raft entries/sec, each carrying ~250 increments. The leaseholder can further *coalesce* them: 250 increments to the same key collapse into one `+250` before the entry is even proposed. The Raft path sees 2,000 writes/sec. This works, and it is why a single-leaseholder design is not immediately disqualified for counters.
- Beyond that ceiling: **sharded counters as a first-class type.** `Increment(key, delta)` on a key declared `sharded(64)` writes to `key/<random 0..63>`, and `Get` sums the 64 shards (a single-range scan if the shards are kept adjacent, or a 64-way batch get). Exact, unlike a CRDT counter, because each shard is itself linearizable. The cost is that `CAS` on a sharded counter is not offered — which is the honest trade and must be in the API docs.

**Contended lock key** (10k clients racing on `PutIfAbsent`):
- The pre-Raft precondition check (§8, step 6) means 9,999 of those 10,000 attempts are rejected locally in microseconds without touching the log. Only the winner replicates.
- Additionally: the client library backs off with jitter on `FAILED_PRECONDITION` and, where the use case allows, converts polling into a **watch on the lock key**, so the 9,999 losers wait for an event instead of spinning.

### Detection

Every leaseholder maintains a decaying top-K sketch of per-key request rates and reports the top 20 keys per range via gossip. A key exceeding 10k ops/sec is surfaced in the tenant's dashboard with the specific mitigation for its access pattern. **Hot keys are a workload problem that the store must diagnose for the tenant** — a store that merely gets slow, without saying which key did it, forces the tenant to guess.

---

## 15. Multi-Region

### Default: single-region, three AZs

Most tenants get 3 replicas in 3 AZs of one region. Intra-AZ RTT ~0.3 ms, cross-AZ ~1 ms, so a Raft commit costs ~1.4 ms and the latency budgets in §4 hold. This survives a full AZ loss with no data loss and a 4.5 s blip.

**Making this the default is a deliberate choice.** Multi-region replication is a 40× write-latency increase, and most internal tenants do not need it. Offering it as an opt-in per namespace — rather than making everyone pay — is the difference between a store people use and one they route around.

### Cross-region latency arithmetic

```
us-east-1  ↔  us-west-2       ~60 ms RTT
us-east-1  ↔  eu-west-1       ~75 ms RTT
us-east-1  ↔  ap-southeast-1 ~230 ms RTT
```

A Raft commit needs the leader plus one more replica. With 3 replicas in 3 regions and the leader in `us-east-1`:

```
commit latency = min(RTT to us-west-2, RTT to eu-west-1) + fsync
               = 60 ms + 0.3 ms  ≈  60 ms
```

A write from a client in `ap-southeast-1` to a leader in `us-east-1` costs 230 ms of client RTT **plus** 60 ms of commit = ~290 ms. That is the number that must be stated plainly to any tenant considering global linearizability.

### Replica topologies offered

**(a) Regional (default) — 3 replicas, 3 AZs, 1 region.**
Write 3.5 ms. Survives AZ loss. Loses everything on region loss (mitigated only by backups, §20).

**(b) Multi-region, 3 regions × 1 replica.**
Write ~60 ms from the leader's region. Survives a full region loss with zero data loss. Read latency is excellent everywhere *if* the tenant uses follower reads; linearizable reads still cost a trip to the leaseholder's region.

**(c) Multi-region, home-region 3 + 1 + 1 (RF=5).**
Quorum is 3, and the home region holds 3 replicas → **local quorum, ~3.5 ms writes.** But losing the home region leaves 2 of 5 — no quorum — and the range is unavailable until unsafe recovery. This topology gives fast writes and survives AZ loss, but explicitly does *not* survive region loss for writes. It is a legitimate choice for data with a natural home (per-region user data, regional inventory), and it must be labeled as "fast, region-durable for reads, not region-available for writes."

**(d) Multi-region, 2 + 2 + 1 (RF=5).**
Quorum 3: leader + local peer + one remote ack = ~60 ms writes. Survives any single region loss with quorum intact (worst case 5 − 2 = 3 = quorum). This is the topology for data that must be both globally durable and globally available. It is the expensive one, in both latency and storage.

| Topology | Write latency (home) | Survives AZ loss | Survives region loss | Storage cost |
|---|---|---|---|---|
| (a) 3 × 1 region | 3.5 ms | Yes | No | 3× |
| (b) 1+1+1 | 60 ms | Yes | Yes | 3× |
| (c) 3+1+1 | 3.5 ms | Yes | Reads only | 5× |
| (d) 2+2+1 | 60 ms | Yes | Yes | 5× |

### Making multi-region usable

Three mechanisms rescue the latency:

**Leaseholder placement follows the load.** The allocator tracks which region issues the most requests per range and moves the leaseholder there. For data with regional locality — the common case — most requests become local, and the cross-region cost is paid only on the commit.

**Follower reads (§9) are the multi-region workhorse.** A `bounded_staleness(5s)` read is served by the local-region replica at ~1 ms instead of 60–230 ms. For read-heavy globally-distributed workloads this converts an unusable store into a fast one, and it is exact-as-of-a-timestamp rather than approximate. **The realistic multi-region design is: writes go to the home region and are globally committed; reads are local and bounded-stale; the handful of operations that genuinely need global linearizability pay 60–290 ms and are known by name.**

**Region pinning.** A namespace can declare `placement: region=eu-west-1`, and all its ranges keep all replicas there — for data residency requirements as much as for latency.

### Behavior during a region partition

```
Topology (b), regions {us-east, us-west, eu-west}, network splits
eu-west from the other two:

  us-east + us-west (majority, 2 of 3):
    linearizable ops        AVAILABLE (leases move here if needed)
    bounded_staleness       AVAILABLE
  eu-west (minority, 1 of 3):
    linearizable ops        UNAVAILABLE — returns
                            UNAVAILABLE{reason: NO_QUORUM,
                                        last_known_revision: N}
    bounded_staleness(t)    AVAILABLE until staleness exceeds t,
                            then returns STALE{actual_staleness: 47s}
                            so the caller can decide
    eventual                AVAILABLE throughout
```

The error responses are part of the design, not an afterthought. A client in `eu-west` can distinguish "the store is broken" from "I am on the minority side of a partition and here is how stale my data is" — and application code can make a sensible decision (serve degraded, fail the request, queue for later) that it simply cannot make from a generic timeout.

---

## 16. Watches, Leases, and TTL

### Watches

The Raft log per range is already an ordered, gap-free, durable change stream. Watches expose it.

```
Watch(key_or_prefix, start_revision) → stream of events

Server side, per range:
  - A watcher registry keyed by key span.
  - On apply of each committed entry, matching watchers receive
    { key, value, prev_value, revision, timestamp, type }.
  - History for catch-up comes from the MVCC store itself: a watch
    starting at revision N scans versions with timestamp > N.
    This is bounded by the GC TTL (§11) — a watcher that has been
    disconnected longer than the TTL gets COMPACTED{min_revision},
    and must re-read the current state and resume from there.
```

**Gap-free and resumable** follow directly: the client tracks the last revision it processed, and on reconnect resumes from `last + 1`. If the store cannot honor that (GC has passed it), it says so explicitly rather than silently skipping events — silent gaps are how cache-invalidation watchers develop permanent staleness bugs.

**The multi-range ordering problem.** A watch on a prefix spanning 40 ranges gets 40 independent event streams with 40 independent revision sequences. There is no global log position to order them by (§10).

We solve it with **resolved timestamps**. Each range periodically publishes "no event with timestamp ≤ T will ever be emitted by me again" — the same closed-timestamp machinery as follower reads (§9). The watch aggregator holds events in a buffer, and emits everything with timestamp ≤ min(resolved timestamps across all 40 ranges), in timestamp order. The result is a **globally timestamp-ordered stream with a bounded lag** (~3 s, the closed-timestamp target).

So the contract is: single-range watches are immediate and ordered; multi-range watches are ordered but lag by the resolved-timestamp interval. The tenant picks. Stating this honestly is better than the alternative, which is a multi-range watch that appears ordered until two events arrive close together across ranges.

### Client leases (ephemeral keys)

```
LeaseGrant(ttl_seconds) → lease_id
Put(key, value, lease_id)          # key dies with the lease
LeaseKeepAlive(lease_id)           # stream, client heartbeats
LeaseRevoke(lease_id)
```

The lease object lives in a range determined by hashing `lease_id`. Keep-alives extend it via a Raft write to that range — so expiry is linearizable and there is exactly one moment, in one total order, at which the lease dies and its keys are deleted.

To keep keep-alive traffic from dominating the write load, keep-alives for a lease with a 30 s TTL are sent every 10 s and **batched across leases sharing a range** into a single Raft entry.

### The fencing token requirement

This is the correctness warning that belongs in bold in the API documentation:

> **A lease expiring does not stop the client that held it.**

The classic sequence:

```
t=0    Client A acquires lock via PutIfAbsent(/lock/job, A, lease=L)
t=1    Client A begins work
t=2    Client A enters a 40-second GC pause
t=32   Lease L expires. /lock/job is deleted. (Correct, per the store.)
t=33   Client B acquires the lock. Begins work.
t=42   Client A wakes up, still believing it holds the lock,
       and writes to the protected resource.
       → Two writers. The store did nothing wrong.
```

The store cannot prevent this — it has no control over what A does to a third-party resource. What it *can* do is provide the primitive that makes it preventable:

**Every mutation returns a monotonically increasing revision.** The lock acquisition returns revision `R`. The protected resource must reject any write carrying a fencing token lower than the highest it has seen. When A wakes at t=42 with token `R`, and B holds `R+1`, A's write is rejected by the resource.

The client library exposes this as a first-class `DistributedLock` type that carries and passes the fencing token automatically, so the correct pattern is the default path rather than something each team has to rediscover after an incident.

### TTL

Per-key TTL is implemented as an attribute on the MVCC version, enforced at two layers:

- **Read-time filtering**: an expired version is invisible to reads immediately at its expiry timestamp, so the *semantic* expiry is exact and requires no background work to have run.
- **Compaction-time removal**: the compaction filter physically drops expired versions during compaction we were doing anyway, so space is reclaimed with zero additional I/O.

Making the semantics read-time and the reclamation compaction-time is what avoids the "expired keys still visible for 20 minutes because the reaper is behind" behavior common in TTL implementations that only sweep.

---

## 17. API Design

```protobuf
service KVStore {
  // ---- Reads ----
  rpc Get(GetRequest) returns (GetResponse);
  rpc BatchGet(BatchGetRequest) returns (BatchGetResponse);
  rpc Scan(ScanRequest) returns (stream ScanResponse);

  // ---- Writes ----
  rpc Put(PutRequest) returns (PutResponse);
  rpc Delete(DeleteRequest) returns (DeleteResponse);
  rpc DeleteRange(DeleteRangeRequest) returns (DeleteRangeResponse);
  rpc BatchWrite(BatchWriteRequest) returns (BatchWriteResponse);

  // ---- Conditional ----
  rpc CompareAndSwap(CasRequest) returns (CasResponse);
  rpc PutIfAbsent(PutIfAbsentRequest) returns (PutResponse);
  rpc Increment(IncrementRequest) returns (IncrementResponse);

  // ---- Watch ----
  rpc Watch(WatchRequest) returns (stream WatchEvent);

  // ---- Leases ----
  rpc LeaseGrant(LeaseGrantRequest) returns (LeaseGrantResponse);
  rpc LeaseKeepAlive(stream KeepAlive) returns (stream KeepAliveAck);
  rpc LeaseRevoke(LeaseRevokeRequest) returns (LeaseRevokeResponse);
}

enum ReadConsistency {
  LINEARIZABLE      = 0;  // default; leaseholder only
  BOUNDED_STALENESS = 1;  // any replica; specify max_staleness_ms
  EVENTUAL          = 2;  // any replica, no bound
}

message GetRequest {
  bytes            key              = 1;
  string           namespace        = 2;
  ReadConsistency  consistency      = 3;
  uint32           max_staleness_ms = 4;  // BOUNDED_STALENESS only, min 3000
  uint64           min_revision     = 5;  // session guarantee (§9)
  uint64           at_revision      = 6;  // 0 = latest; else a time-travel read
}

message GetResponse {
  bool    found            = 1;
  bytes   value            = 2;
  uint64  revision         = 3;   // version of THIS key
  int64   timestamp_hlc    = 4;
  uint32  actual_staleness_ms = 5; // populated for non-linearizable reads
}

message PutRequest {
  bytes   key               = 1;
  bytes   value             = 2;
  string  namespace         = 3;
  string  idempotency_token = 4;  // REQUIRED for safe retry
  uint64  lease_id          = 5;  // 0 = no lease
  uint32  ttl_seconds       = 6;  // 0 = no TTL
}

message PutResponse {
  uint64  revision      = 1;   // fencing token — pass to protected resources
  int64   timestamp_hlc = 2;
  bytes   prev_value    = 3;   // if return_prev was set
}

message CasRequest {
  bytes   key               = 1;
  uint64  expected_revision = 2;  // 0 = expect absent
  bytes   new_value         = 3;
  string  namespace         = 4;
  string  idempotency_token = 5;
}

message CasResponse {
  bool    succeeded        = 1;
  uint64  revision         = 2;  // new revision on success
  uint64  current_revision = 3;  // on failure, what it actually was
  bytes   current_value    = 4;  // on failure, saves a follow-up Get
}
```

### Error model

Errors are typed and actionable, because a generic `UNAVAILABLE` forces every caller to guess:

```
NOT_LEASEHOLDER      { hint_node, range_descriptor }
                       → client updates routing cache, retries immediately

NO_QUORUM            { range_id, last_known_revision, replicas_reachable }
                       → this range has lost quorum. Retrying will not help
                         until recovery. Fail fast or degrade.

STALE_READ_EXCEEDED  { actual_staleness_ms, requested_max_ms }
                       → the caller asked for 5 s and we can only offer 47 s.
                         Caller decides: accept, escalate, or fail.

FAILED_PRECONDITION  { current_revision, current_value }
                       → CAS lost the race. Do not blind-retry.

RESOURCE_EXHAUSTED   { retry_after_ms, reason: QUOTA | ADMISSION | HOT_RANGE }
                       → shed by admission control. Back off by retry_after_ms.

RANGE_SPLITTING      { }  → transient, retry in ~50 ms
```

### API decisions worth defending

**`idempotency_token` is required, not optional, on every mutating call.** Making it optional means most callers omit it and discover during their first failover that their retries double-applied. A required field with a library that generates it automatically costs the caller nothing and removes an entire class of production bug.

**Revisions are returned everywhere and are the fencing token.** One concept — the revision — serves as the CAS precondition, the watch resume point, the session-guarantee token, and the lock fencing token. Fewer concepts is a real API virtue at platform scale.

**`CasResponse` returns the current value on failure.** Nearly every CAS-retry loop needs it, and returning it saves a round trip on the contended path — precisely where round trips hurt most.

**`max_staleness_ms` has a floor of 3000.** It equals the closed-timestamp target. Allowing a caller to request 50 ms of staleness would make the call fall back to a leaseholder read every time while *appearing* to be a cheap follower read. A floor makes the cost model honest.

**No cross-range `BatchWrite`.** It returns `INVALID_ARGUMENT` with the offending key boundaries rather than silently splitting into two non-atomic batches.

---

## 18. Client Library

The client library is part of the system's correctness, not a convenience wrapper. Most of the guarantees in §10 are jointly enforced by server and client.

### Routing cache

```
Cache:   sorted map of range boundaries → replica set + leaseholder hint
Fill:    lazily on first access to a key span, via meta1/meta2 (§5)
Size:    a client touching 10k ranges holds ~2.5 MB of descriptors
Invalidate: on NOT_LEASEHOLDER (use the hint), on RANGE_SPLITTING
            (re-resolve the span), and on a 10-minute background refresh
```

Being wrong is cheap and self-correcting: one extra RTT and an updated entry. This is why we can cache aggressively and skip a routing tier entirely.

### Retry policy — the part that prevents outages

```
RETRYABLE, with the same idempotency token:
    UNAVAILABLE, DEADLINE_EXCEEDED, NOT_LEASEHOLDER, RANGE_SPLITTING

RETRYABLE after retry_after_ms:
    RESOURCE_EXHAUSTED

NOT RETRYABLE (retrying is a bug):
    FAILED_PRECONDITION, INVALID_ARGUMENT, PERMISSION_DENIED,
    NO_QUORUM (until a quorum is restored — retrying adds load
               to a cluster that is already in trouble)

Backoff:  exponential, base 10 ms, factor 2, cap 2 s, FULL JITTER
Budget:   retries capped at 10% of the client's own request rate over
          a 10 s window. Beyond that, fail immediately without retrying.
Breaker:  per-node circuit breaker — 20 consecutive failures opens it
          for 5 s, and requests route to another replica.
```

**The retry budget is the single most important line.** Without it, a cluster that slows down receives 3× the traffic from retries, slows down further, and receives more — the retry-storm metastable failure. A budget converts that positive feedback loop into a hard ceiling: when the cluster is unhealthy, clients send *at most* 1.1× normal load, never 3×.

Full jitter (`sleep = random(0, backoff)`, not `backoff/2 + random(0, backoff/2)`) matters at 10k clients: without it, clients that failed together retry together forever.

### Session tokens

The library tracks the highest revision it has observed across all operations in the session and attaches it as `min_revision` on every read (§9). This gives read-your-writes and monotonic reads at every consistency level without the application doing anything. Applications that need cross-process session continuity can export and import the token.

### Helper types

Encoding the correct patterns in the library is how a platform team scales its correctness knowledge past the people who wrote the design doc:

- **`DistributedLock`** — `PutIfAbsent` + lease + keep-alive + **automatic fencing-token propagation** (§16). Refuses to be used without a fencing sink.
- **`CachedValue`** — watch-backed local cache for read-hot keys (§14), the recommended answer to "this flag is read 2M times/sec."
- **`ShardedCounter`** — transparent shard fan-out and fan-in for write-hot counters.
- **`PrefixWatcher`** — multi-range watch with resolved-timestamp ordering and automatic re-sync on `COMPACTED`.

---

## 19. Multi-Tenancy and Admission Control

Per §13, overload is ~30% of the error budget — the largest single line item. Admission control is therefore a primary availability mechanism, not a nicety.

### Isolation layers

```
1. NAMESPACE     Key prefix isolation: /{namespace}/{user_key}.
                 Enforced at the gateway; a token is scoped to
                 namespaces and cannot address outside them.

2. QUOTA         Per namespace: total bytes, key count, max value size,
                 max ranges. Exceeded → RESOURCE_EXHAUSTED on writes,
                 reads unaffected. (Failing writes but not reads is
                 deliberate: a tenant over quota can still serve
                 traffic while they clean up.)

3. RATE LIMIT    Per-namespace token buckets for read units and write
                 units, enforced per node. A "unit" is normalized by
                 size so a 1 MB write costs more than a 200 B write.

4. PRIORITY      Each request carries a priority class:
                   CRITICAL   — user-facing reads/writes
                   NORMAL     — default
                   BACKGROUND — bulk loads, migrations, scans
                 Shed strictly in reverse priority order.
```

### Queue-latency-based shedding

Rate limits handle the expected case. They do not handle the case where the *cluster* got slower — a compaction backlog, a failover, a noisy neighbour — and the previously-fine request rate is now too much.

For that we shed on **observed queue latency**, not on a configured rate:

```
Per node, per priority class, measure request queue-wait time.

  queue_wait_p50 < 5 ms    → admit everything
  5 ms – 20 ms             → shed BACKGROUND
  20 ms – 50 ms            → shed BACKGROUND + NORMAL over its token rate
  > 50 ms                  → shed all but CRITICAL, and return
                             retry_after_ms proportional to the overload

Additional gates:
  L0 file count > soft threshold  → shed BACKGROUND writes (§11)
  Raft proposal queue > 1000      → shed NORMAL writes
```

This is the CoDel insight applied to a storage node: **the queue itself is the signal.** A rate limit tuned for a healthy cluster is wrong the moment the cluster is unhealthy, which is exactly when it needs to be right. Latency-based shedding adapts automatically and requires no operator to re-tune anything mid-incident.

**Shedding must be cheap.** A rejected request must consume no disk I/O and no Raft work — the check happens before the leaseholder check, at the very front of the node's request path (§8, step 2). A shed that costs as much as an admit does not shed.

---

## 20. Anti-Entropy, Backup, Restore

### Anti-entropy: a different problem than in an AP store

In a Dynamo-style store, replicas legitimately diverge and Merkle-tree repair is core machinery running constantly. Under Raft, **divergence is impossible by protocol** — all replicas apply the identical log in the identical order.

So any divergence we find is a *bug or corruption*, and that changes what the mechanism is for: it is not a repair loop, it is a **detector**, and its most important output is an alarm, not a fix.

```
Consistency checker:
  For each range, once per 24h (staggered):
    1. Leaseholder proposes a CheckConsistency command at index I.
    2. Each replica, on applying index I, computes a SHA-256 over
       its entire range content (MVCC keys + values, in order) and
       reports the digest.
    3. Digests must match exactly — same log, same order, same state.
    4. Mismatch → PAGE IMMEDIATELY. Mark the range read-only.
       Do not auto-repair: an automatic "fix" would destroy the
       evidence needed to find the bug that caused it.

Cost: 100k ranges / 86400 s ≈ 1.2 checks/s cluster-wide,
      each scanning 512 MiB → ~600 MB/s cluster-wide, throttled
      and scheduled at low I/O priority.
```

Plus continuous, cheap defenses: **block checksums verified on every read** catch bit rot at the point of use, and a background scrubber reads cold blocks at a low rate so corruption in never-read data is found before it is needed.

### Backup

The essential property: **backups defend against the failure mode replication cannot** — a software bug that corrupts all three replicas identically, or an operator deleting a namespace. Both are inside the replication guarantee and outside the backup's.

```
FULL BACKUP (weekly)
  Consistent snapshot at a chosen HLC timestamp T:
    - every range exports all MVCC versions visible at T
    - ranges export in parallel from FOLLOWERS, not leaseholders,
      so backup does not compete with foreground traffic
    - written as sorted, immutable, self-describing files to object
      storage, in a DIFFERENT format than the LSM's
  50 TB at ~5 GB/s aggregate → ~3 hours

INCREMENTAL (every 15 min)
  Export MVCC versions with timestamp in (T_prev, T_now].
  This is a native LSM operation — the versions are already there,
  timestamp-ordered, in the same key span.
  Requires GC TTL ≥ the incremental interval (§11: 4h ≫ 15min ✓).
  15 min at 1M writes/s × 500 B ≈ 450 GB → ~2 min

RETENTION
  Incrementals 7 days · fulls 4 weeks · monthly fulls 12 months
  Stored in a different account with a different credential and
  object-lock enabled, so the credential that can delete the
  cluster cannot delete the backups.
```

The "different format" and "different account" details are the ones that matter. A backup written by the same code that corrupted the data, into a bucket the same credential can delete, defends against neither of the two failure modes it exists for.

### Restore

```
PITR to any timestamp within 7 days:
  1. Locate the most recent full backup at or before T.
  2. Apply incrementals up to T.
  3. Because everything is MVCC-timestamped, "restore to T" is a
     filter on the import, not a replay of operations.

Restore time for 50 TB:
  read from object storage @ ~10 GB/s aggregate (54 nodes)  ≈ 83 min
  ingest as pre-sorted SSTs directly into the LSM
    (bypassing the write path and Raft entirely — the data is
     already sorted, so it becomes bottom-level SSTs)         ≈ 30 min
  re-replicate to RF=3                                        ≈ 40 min
  ──────────────────────────────────────────────────────────
  ~2.5 hours to a fully-replicated cluster

Namespace-level restore (the common case — one team's mistake):
  restore only that key span into a staging namespace, let the
  tenant verify, then swap. Minutes, not hours, and no impact on
  any other tenant.
```

**Namespace-level restore is the feature that actually gets used.** Full-cluster restore is the disaster plan; single-namespace restore is Tuesday. Building only the former is a common and expensive omission.

**Restores are rehearsed monthly** against a scratch cluster, with the restore time recorded as an SLI. An unrehearsed restore path is an untested one, and the first test should not be during the incident.

---

## 21. Observability and SLOs

### The metrics that actually indicate health

Most storage dashboards are 200 panels of which 6 matter. These are the 6:

```
1. UNDER-REPLICATED RANGES
   Ranges with < 3 live replicas. Should be 0 in steady state,
   nonzero and DECREASING after a failure.
   Page if > 0 for more than 10 minutes.

2. RANGES WITHOUT QUORUM
   Ranges with < 2 live replicas. This is unavailability, right now.
   Page immediately at > 0.

3. L0 FILE COUNT (p99 across nodes)
   The leading indicator of write-latency collapse (§11).
   Warn at 20, page at 40, engine stalls near 60.
   This predicts an incident ~10 minutes before latency moves.

4. CLOSED-TIMESTAMP LAG (p99)
   How far behind follower reads are. Growing lag means a range's
   leaseholder is struggling, and it degrades bounded-staleness
   reads cluster-wide before anything else shows.

5. ADMISSION QUEUE WAIT (p99, per priority)
   The overload signal (§19). Rising CRITICAL queue wait means
   shedding is not keeping up.

6. LEASE TRANSFERS PER SECOND
   Steady state is a trickle. A spike means leases are thrashing —
   usually a flapping node or an allocator fighting itself — and
   it causes latency spikes that look inexplicable from latency
   metrics alone.
```

Notably absent: raw QPS and CPU. They are useful for capacity planning and nearly useless for detecting the failure modes that actually cause outages here.

### SLOs

```
SLI                                  Target      Window
─────────────────────────────────────────────────────────
Linearizable write availability      99.99%      30 days
Linearizable read availability       99.99%      30 days
Bounded-staleness read availability  99.999%     30 days
Write latency P99 < 10 ms            99%         30 days
Lin. read latency P99 < 5 ms         99%         30 days
Durability (acked writes lost)       0           forever
Closed-timestamp lag P99 < 10 s      99.9%       30 days
```

Availability is measured **per namespace from the client's perspective**, via the client library reporting its own success rate. A server-side measurement misses precisely the failures that matter most — the ones where clients cannot reach the server at all.

### Proving linearizability

Documented guarantees that are never tested decay into folklore. Three layers:

**1. In CI: Jepsen / Elle.**
Every release runs a linearizability check with concurrent clients, randomized operations, and injected faults (partitions, clock skew, process pauses, disk failures). The history is checked against a linearizable model. This gates the release.

**2. In CI: deterministic simulation.**
The FoundationDB approach — the entire cluster runs single-threaded in simulated time with a deterministic scheduler, so a randomized fault schedule can explore thousands of failure interleavings per minute and any failure is reproducible from its seed. Far more effective per CPU-hour than Jepsen, and this is where the design's non-obvious bugs (membership changes during partitions, lease handoff races, split-during-failover) are actually found.

**3. In production: a sampled online checker.**
A small fleet of verifier clients performs randomized read/write/CAS operations against a dedicated namespace, records real-time-annotated histories, and checks them for linearizability violations continuously. Volume is tiny; the value is that it is checking the *real* cluster with the real hardware, real clocks, and real network. A violation here is a page at any hour.

---

## 22. Failure Walkthrough

| Failure | What the system does | What an in-flight client observes | Recovery |
|---|---|---|---|
| **Single node crash** | Liveness expires ≤9 s; epoch bumped; leases move; Raft elects new leaders on ~1,850 groups | Timeouts on those ranges for ~4.5 s, then normal. Uncommitted writes must be retried with the idempotency token | Re-replication after 5 min; RF=3 restored in ~45 min |
| **Node slow (GC pause, bad disk)** | Liveness may expire → leases move away. Replacement does NOT trigger (5 min threshold) | Brief latency spike; requests route to the new leaseholder | Node rejoins as a follower, catches up from the Raft log |
| **Disk failure on one node** | Engine detects checksum failures, node marks itself unhealthy and sheds leases proactively | Latency spike during lease transfer only | Node decommissioned; ranges re-replicated |
| **Rack loss (≈6 nodes)** | Placement guarantees at most 1 replica per range per AZ, so no range loses quorum | ~4.5 s blip on ranges whose leaseholder was in that rack | Re-replication after 5 min, throttled |
| **AZ loss (18 nodes)** | Every range keeps 2 of 3. ⅓ of leases move | ~4.5 s blip on ⅓ of ranges | Under-replicated until the AZ returns. **Allocator will NOT restore RF=3 by doubling up in a surviving AZ** (§6) — it waits, preserving diversity |
| **Network partition, AZ-c isolated** | AZ-a + AZ-b hold majority and serve everything. Clients in AZ-c can reach only 1 replica | AZ-c clients: linearizable ops → `NO_QUORUM`; bounded-staleness → served with growing reported staleness; eventual → served | Heals automatically; AZ-c replicas catch up from the Raft log |
| **2 of 3 replicas of one range lost permanently** | That range has no quorum; it is unavailable. All other ranges are unaffected | `NO_QUORUM{range_id, last_known_revision}` for keys in that span only | Restore from backup, or unsafe recovery (§29.3) with explicit operator override |
| **Clock jumps +40 s on one node** | Node's own offset monitor detects >500 ms disagreement with peers and removes itself from service before it can act on the bad clock. Epoch-based leases mean no correctness impact even if it does not | Nothing, if the range had other replicas | NTP resync; node rejoins |
| **Write burst 5× capacity** | Admission control sheds BACKGROUND, then NORMAL, on queue latency. L0 gate sheds background writes | CRITICAL traffic served normally; lower classes get `RESOURCE_EXHAUSTED{retry_after_ms}` | Automatic as the burst subsides |
| **Retry storm after a failover** | Client-side retry budget caps retries at 10% of base rate; per-node breakers open | Fast failures instead of long queues | Automatic — the budget prevents the positive feedback loop |
| **Bad deploy corrupting the apply path** | Consistency checker detects digest mismatch within 24 h; canary should catch it within minutes | Depends on the bug — this is the dangerous one | Roll back; restore affected ranges from backup (the only defense, §20) |
| **Full region loss (topology b)** | Remaining 2 regions hold quorum; leases move | ~4.5 s blip; write latency changes as leaseholders relocate | Automatic |
| **Full region loss (topology a)** | Total loss for that cluster | Complete unavailability | Restore from backup into another region: ~2.5 h |

---

## 23. Trade-offs and Design Decisions

| Decision | Chosen | Rejected | Why | What we give up |
|---|---|---|---|---|
| CAP posture | CP | AP | CAS, leases, ordered watches, and exact counters are the reason teams want this store; none survive AP | Linearizable ops unavailable on the minority side of a partition |
| Replication | Raft per range | Dynamo quorums | Linearizability native rather than bolted on; no sibling reconciliation; the log is the watch stream | Hot ranges bound by one leaseholder; split/merge machinery to build |
| Partitioning | Range | Hash | `Scan` is a hard requirement and is impossible on a hash ring at this latency | Sequential-key hot spots need explicit mitigation |
| Replication factor | 3 | 5 | 5 gives worse write tails (3rd-of-5 vs 2nd-of-3), 1.67× storage, and cannot maintain AZ diversity across 3 AZs | No tolerance for a second failure during the recovery window |
| Read path default | ReadIndex | Leader leases | No clock dependency; batching makes the marginal cost ~0 | ~1 ms extra latency vs leases (still inside SLO) |
| Lease mechanism | Epoch-based | Time-based | Converts "trust every clock" into "trust one Raft-replicated record" | An extra liveness range that must always be available |
| Storage engine | LSM, tiered-over-leveled | LSM leveled / B-tree | Leveled would need 122 nodes for write endurance vs 51 (§4); B-tree amplifies 500 B random writes 8–32× | Higher read amplification, paid in RAM |
| Engine instances | One per node | One per range | 5,600 memtables and WALs per node is untenable; shared WAL amortizes fsync | Range moves are logical scans, not file copies |
| Range size | 512 MiB | 64 MiB | 45k replicas/node is a heartbeat storm even with coalescing | Coarser rebalance granularity |
| Coordination | Self-hosted meta ranges | External etcd/ZooKeeper | One consensus system to operate, not two; no lookup ceiling | Bootstrap complexity |
| Multi-key atomicity | Single range only | Cross-range 2PC | 2PC becomes the default path teams reach for and then the store's performance is a transaction coordinator's performance | Teams needing cross-key atomicity must model around it |
| Multi-region | Opt-in per namespace | Default | 60 ms writes for everyone to serve the minority who need it | Tenants must choose consciously; a wrong choice is a migration |
| GC TTL | 4 h default | 25 h | 25 h of MVCC garbage at 1M writes/s outweighs the live dataset (§4) | Shorter watch-resume and time-travel window |
| Anti-entropy | Detect and page | Detect and auto-repair | A mismatch is a bug; auto-repair destroys the evidence | A human is needed for every (rare) mismatch |
| Idempotency token | Required | Optional | Optional means omitted means double-applied increments at the first failover | Slightly noisier API |
| Retries | Client budget, 10% cap | Unbounded exponential backoff | Unbounded retries are the retry-storm metastable failure | Some requests fail that a retry would have served |

### What we explicitly did not build

- **Cross-partition transactions.** §29.1 designs them; we do not ship them in v1. The reason is cultural as much as technical: once available, they become the default, and the store's performance envelope becomes a transaction coordinator's.
- **Secondary indexes.** This is a KV store. An index is a second keyspace the application maintains, and doing it in the store means cross-range atomicity, which we do not have.
- **Server-side aggregation / query language.** Scans are for enumeration. Analytics belongs in a system built for it.
- **Automatic global rebalancing across regions.** Placement is declarative per namespace. Automatic cross-region movement based on observed load sounds helpful and is how data ends up in a jurisdiction it may not legally be in.
- **Byzantine fault tolerance.** 3× the replicas and a much harder protocol for a threat model that does not apply inside our own cluster.

---

## 24. Evolution Path

Each stage is a real deployment that serves real traffic. Nothing here is scaffolding to be thrown away.

**Stage 0 — Single Raft group (3 nodes, 100 GB, 10k ops/s).**
One range, no splitting, no rebalancing, no meta ranges. This is etcd. It delivers every consistency guarantee in §10 and every API in §17. Ship it, get tenants, learn what they actually do.
*Exit trigger:* dataset > 50 GB or > 20k ops/s — the point where one group's single leader saturates.

**Stage 1 — Range splitting and the meta layer.**
Add size-based splits, the two-level meta index, and client routing caches. Still manual placement.
*Exit trigger:* > 20 nodes, where manual placement stops being possible.

**Stage 2 — The allocator.**
Automatic rebalancing, AZ-diversity constraints, load-based splitting, learner-based membership changes.
*Exit trigger:* read load exceeds what leaseholders can serve, or P99 read latency becomes AZ-topology-sensitive.

**Stage 3 — Closed timestamps and follower reads.**
`bounded_staleness` becomes real. Read capacity triples. This is also the prerequisite for multi-range watch ordering (§16).
*Exit trigger:* a tenant needs region failure tolerance.

**Stage 4 — Multi-region.**
Region-aware placement, the four topologies of §15, leaseholder-follows-load.
*Exit trigger:* multiple tenants independently build the same broken cross-key coordination pattern.

**Stage 5 — Cross-partition transactions (§29.1), if and only if the evidence demands it.**

**Ordering note.** Admission control (§19), the retry budget (§18), and backups (§20) are **not** on this ladder — they belong in Stage 0. They are what makes the store survivable at every stage, and per §13 they defend the majority of the error budget. A team that defers admission control until "we have scale problems" has inverted the order: admission control is what prevents the scale problem from becoming an outage.

---

## 25. Variant A: The AP Store

*Same API, availability wins during a partition: every replica accepts writes at all times.*

### Architecture

Consistent hashing with virtual nodes (256 vnodes per physical node for smooth rebalancing), N=3, `W=2`, `R=2`. No leader. Any node coordinates any request.

```
Put:  coordinator hashes key → 3 preference-list nodes
      writes to all 3 in parallel, acks after W=2 respond
      if a preference-list node is down, write to the next node
      on the ring with a HINT (sloppy quorum + hinted handoff)

Get:  read from all 3, wait for R=2
      if versions disagree → reconcile → return → write back the
      reconciled version (read repair)
```

### Conflict model

The central question. Three options, and the choice cascades:

**Last-writer-wins by wall-clock timestamp.** Simple, and it silently discards concurrent writes. It also makes correctness depend on clock synchronization across all writers, so a node with a fast clock wins every conflict forever. Acceptable only for genuinely idempotent overwrite workloads (a cache, a session store where any recent value works). **Not acceptable as a default**, because the data loss is invisible.

**Version vectors + client reconciliation.** Each write carries the causal context it observed. Concurrent writes produce siblings, and `Get` returns all of them for the client to merge. Correct — no silent loss — but it pushes reconciliation into every application, and applications get it wrong. Vectors also grow with the number of writers and need pruning, which reintroduces the possibility of false conflicts.

**CRDTs.** The store understands the value type (counter, set, register, map) and merges deterministically. Correct and invisible to the application, but only for types with a lattice merge. Requires giving up opaque byte values as the default, which is a substantial API change.

**Recommendation for this variant: CRDT types as the primary API, version vectors as the escape hatch for opaque values, LWW only as an explicitly-labeled per-namespace opt-in.**

### What survives, what changes, what must be deleted

| Requirement | Status in the AP store |
|---|---|
| `Get` / `Put` / `Delete` | **Survives.** Delete becomes a tombstone with causal context, and tombstone GC becomes genuinely hard: dropping a tombstone too early lets a partitioned replica resurrect the key. Requires a GC barrier that all replicas have acknowledged |
| `CompareAndSwap` | **Deleted.** No total order per key ⇒ no CAS. Offering "usually-correct CAS" is worse than offering none, because teams will build locks on it |
| `PutIfAbsent` | **Deleted.** Same reason. Two partitioned clients both succeed |
| `Increment` | **Changes.** Becomes a PN-Counter CRDT: correct sum, but cannot be conditionally applied, cannot be bounded below zero, and a read returns a value that is exact only once all replicas have converged |
| `Scan` | **Changes.** Hash partitioning makes ordered scans a full fan-out. Either drop ordered scans or add a separate ordered index, which reintroduces the consistency problem |
| `BatchWrite` atomicity | **Deleted.** No atomicity at any granularity |
| Watches, gap-free and ordered | **Deleted as specified.** No per-key total order exists. Best available: "eventually you observe all versions, in an order you must reconcile," which cannot drive cache invalidation correctly |
| Leases / ephemeral keys | **Deleted.** Expiry needs a single agreed moment. In AP, two partitions disagree about whether a lease is alive |
| `linearizable` read level | **Deleted.** `R+W>N` gives quorum intersection, not linearizability (§3) |
| Session guarantees | **Survives** with client-carried causal context |

**Four of the seven functional requirements are deleted and two are materially weakened.** That is the honest accounting, and it is the argument in §2 rendered concrete.

### Additional machinery required

**Merkle-tree anti-entropy**, running permanently rather than as a bug detector. Each node maintains a Merkle tree per vnode range; neighbours exchange root hashes periodically and descend only into differing subtrees. This is a continuous background cost and a continuous operational one — it is the top source of pages in production Cassandra deployments.

**Hinted handoff.** Writes destined for a down node go to a substitute with a hint; when the node returns, hints are replayed. Hint storage becomes a capacity problem during long outages, and dropping hints silently converts an availability event into a durability one.

### When this variant is actually right

Session stores, caches, user-generated content feeds, telemetry ingestion, shopping carts — workloads where a write must never be refused, where conflicts are rare or naturally mergeable, and where nothing coordinates on the data. That is a large and legitimate class of workloads. It is simply not the class that the task's functional requirements describe.

---

## 26. Variant B: Global Linearizability

*Three regions, linearizable globally.*

### The arithmetic first

```
Regions: us-east-1, eu-west-1, ap-southeast-1
RTTs:    us↔eu 75 ms · us↔ap 230 ms · eu↔ap 170 ms

RF=3, one replica per region, leader in us-east-1:
  commit = nearest remote ack = 75 ms
  client in us-east-1:      75 ms + ~1 ms   =  76 ms
  client in eu-west-1:      75 ms + 75 ms   = 150 ms
  client in ap-southeast-1: 75 ms + 230 ms  = 305 ms

Compare to single-region: 3.5 ms.
Global linearizability costs 20×–90× on writes. There is no
protocol that avoids this; it is the speed of light plus a
quorum round.
```

**State this number before anything else in the design.** Most requests for "globally consistent" evaporate when the requester sees 305 ms.

### Making it usable

**1. Leaseholder placement follows the write load.** For data with a natural home region (a European user's records), put the leader in `eu-west-1`: local clients pay 75 ms (the commit) instead of 150 ms (commit plus a transcontinental client hop). Cuts the common case in half.

**2. Follower reads are the entire read story.** A `bounded_staleness(5 s)` read is served from the local-region replica at ~1 ms rather than 75–305 ms. Since most global workloads are read-heavy, this converts an unusable store into a fast one for the great majority of operations. The closed-timestamp lag must be tuned above the max inter-region RTT — 5 s is comfortable at 230 ms RTT.

**3. Region-pinned namespaces.** Data that does not need global consensus (the majority) gets all 3 replicas in one region and single-region latency. Global topology is applied per namespace, only where the requirement is real.

**4. Witness replicas.** A witness participates in Raft voting and durability but stores only the log, not the applied state. A `2 + 2 + witness` topology across 3 regions gives quorum-3 with two nearby full replicas plus a cheap tiebreaker in the third region — region-failure survivable at closer to 3× storage than 5×.

**5. Non-blocking transactions for the read-hot global case.** For data read globally and written rarely (configuration, feature flags, schema), writes can be committed at a *future* timestamp: the write is proposed at `now + max_clock_uncertainty + propagation`, and readers at timestamps below it never block. Writers pay the latency; readers everywhere pay nothing. This inverts the usual trade and is the right shape for globally-read configuration.

### Clock uncertainty

Spanner uses TrueTime (GPS/atomic clocks, ~7 ms uncertainty) so that a writer can wait out the uncertainty interval and guarantee external consistency. Without that hardware, uncertainty is 100–250 ms with plain NTP, and commit-wait becomes ruinous.

Our design (§7) avoids the dependency entirely for *linearizability* by using epoch-based leases and Raft ordering. What we do **not** get without synchronized clocks is **strict serializability across independent keys with real-time ordering** — i.e. if transaction A on key X commits before transaction B on key Y begins, our timestamps may not reflect that. Per-key linearizability holds regardless.

Given that we ruled out cross-partition transactions (§23), we do not need TrueTime. **The scope decision in v1 is what makes the clock decision affordable** — worth noting, because a later decision to add cross-partition transactions re-opens the clock question.

### Region partition behavior

Per §15, with 1+1+1 and `ap-southeast-1` isolated: `ap` serves bounded-staleness and eventual reads with reported staleness, and refuses linearizable ops with `NO_QUORUM`. The other two regions are fully available. When the partition heals, `ap` catches up from the Raft log with no reconciliation, because nothing diverged.

---

## 27. Variant C: Small Scale

*Same guarantees. 3 nodes, 100 GB, 10k ops/sec, one AZ, one part-time operator.*

### What collapses out

| Component | Keep? | Reasoning | Reinstate when |
|---|---|---|---|
| Raft, RF=3 | **Keep** | This is the guarantee. Nothing here is optional | — |
| Range splitting | **Drop** | 100 GB in one Raft group is fine; one range means no meta layer, no routing cache, no split/merge | Dataset > 50 GB **or** > 20k ops/s |
| Meta ranges / routing | **Drop** | 3 nodes; the client tries all 3 and follows the leader hint | With range splitting |
| The allocator | **Drop** | With 3 nodes and 3 replicas, placement is determined | > 5 nodes |
| Closed timestamps / follower reads | **Drop** | ReadIndex from the leader handles 10k reads/s easily | Read load > ~50k/s or cross-AZ read latency matters |
| Multi-region | **Drop** | — | A region-failure requirement appears |
| Load-based splitting | **Drop** | With splitting | With splitting |
| Consistency checker | **Keep** (weekly) | Cheap at 100 GB, and it is the only detector for a corrupting bug | — |
| **Admission control** | **KEEP** | 3 nodes are *easier* to overload than 54. This is not a scale feature | — |
| **Retry budget** | **KEEP** | Retry storms are worse at small scale — less headroom to absorb them | — |
| **Backups + restore rehearsal** | **KEEP** | A part-time operator makes operator error *more* likely, not less | — |
| **Idempotency tokens** | **KEEP** | Failovers happen at every scale | — |
| **Typed errors** | **KEEP** | Free, and they are what makes debugging possible without an expert | — |

### The result

This is essentially **etcd**: one Raft group, one storage engine, ReadIndex reads, leases, watches, and typed errors. Perhaps 15% of the full design's code, and it delivers 100% of the consistency guarantees in §10.

**The lesson is worth stating explicitly**: everything dropped is about *scale* — splitting, placement, follower reads, multi-region. Nothing dropped is about *consistency*, *durability*, or *survivability*. The scale machinery is 85% of the code and 0% of the guarantees.

And the four items marked KEEP in bold are the ones a small team is most tempted to defer. Per §13, they defend roughly two-thirds of the error budget at any scale.

### Operational shape

```
Deployment:      3 VMs, 500 GB NVMe each, one AZ
                 (one AZ means a single AZ failure is total loss —
                  which is the correct trade at this scale, but must
                  be a stated, accepted risk, backed by backups)
Upgrades:        rolling, one node at a time, drain first, 15 minutes
Monitoring:      6 metrics (§21), 2 pages (no quorum, backup failed)
Backups:         hourly incremental, nightly full, to object storage
                 in a different account
Restore drill:   monthly, 20 minutes
On-call load:    target < 1 page/month
```

---

## 28. Variant D: The Migration

*4 TB on sharded MySQL with application-level routing, no cross-shard transactions. Zero downtime, rollback at every step.*

The absence of cross-shard transactions is the crucial detail: the application already assumes single-key atomicity only, which is exactly what our store provides. The semantic gap is small; the risk is entirely operational.

### Phase 0 — Shadow reads (2 weeks, zero risk)

Dual-read: every production read goes to MySQL (authoritative, response returned) and asynchronously to the KV store (result compared, discrepancy logged). Nothing is written to the KV store yet — this phase exists to validate the **key encoding, the client library, and the capacity model** under real traffic patterns.

*Rollback:* delete the shadow code path. Zero production impact at any moment.

### Phase 1 — Backfill (1 week)

Bulk-load 4 TB from MySQL snapshots. Data is exported sorted by the target key encoding and ingested as pre-sorted SSTs directly into the LSM, bypassing the write path and Raft entirely (§20). 4 TB at ~2 GB/s ≈ 35 minutes of ingest, plus export time.

Concurrently, stream MySQL binlog changes into the KV store so the backfill converges to live rather than to a fixed snapshot.

*Rollback:* truncate the namespace.

### Phase 2 — Dual write, MySQL authoritative (2–4 weeks)

```
Write path:  1. write MySQL (authoritative — success/failure returned here)
             2. write KV store with an idempotency token
                (async, failures logged and retried, never surfaced)
Read path:   MySQL only. KV store shadow-read and compared.

Success criterion: discrepancy rate < 1 in 10⁶ over 7 consecutive days.
```

**The discrepancy rate is the gate, and it must be near-zero before proceeding.** Persistent discrepancies mean a semantic mismatch between the two stores — usually MySQL's implicit type coercion, its collation-dependent key comparison, or a write path that bypasses the dual-write wrapper. Every one of those must be understood, not tolerated.

*Rollback:* disable the KV write. MySQL never stopped being authoritative.

### Phase 3 — Read cutover, per shard (2 weeks)

Flip reads to the KV store one MySQL shard at a time — 1%, 10%, 50%, 100% of shards, with a bake period at each step. Writes still go to both, MySQL still authoritative.

*Rollback:* flip reads back. Instantaneous, since MySQL is still current.

### Phase 4 — Write cutover (the only irreversible-ish step)

```
Per shard:
  1. Set the shard read-only in the application (seconds).
  2. Wait for the binlog stream to fully drain into the KV store.
  3. Verify: row counts and a sampled checksum comparison.
  4. Flip the shard's writes to KV-authoritative.
  5. REVERSE the replication: stream KV changes back into MySQL.
```

Step 5 is what makes this reversible. MySQL stays current — read-only but continuously updated — for 30 days after cutover. Rollback is flipping authority back, with the same brief read-only window in reverse.

*Rollback window: 30 days.*

### Phase 5 — Decommission (after 30 days)

Stop reverse replication, take a final MySQL backup, retain it for 12 months, decommission the shards.

### Key risks

| Risk | Mitigation |
|---|---|
| Key encoding mismatch (collation, type coercion, trailing whitespace) | Phase 0 shadow reads surface these before any write |
| Hot shard becomes a hot range | Load-based splitting (§6); pre-split on known-hot spans identified in Phase 0 |
| Application relies on MySQL behavior not in the contract (implicit ordering, auto-increment, `SELECT ... FOR UPDATE`) | Audit every query in Phase 0; `FOR UPDATE` maps to CAS, and each instance must be converted by hand |
| Dual-write divergence under partial failure | Idempotency tokens plus a reconciliation job that re-reads MySQL as truth for any key with a logged write failure |
| Capacity surprise | Phase 0 measures real read patterns; Phase 1 measures real data size and compression |

---

## 29. Stretch Problems

### 29.1 Multi-key transactions across partitions

**Protocol: two-phase commit layered over Raft groups, with the transaction record itself stored in a Raft group** — so the coordinator is not a single point of failure. This is the Percolator/CockroachDB shape.

```
1. Client picks a coordinator range and writes a TRANSACTION RECORD
   { txn_id, status: PENDING, timestamp, key_spans[] }
   (one Raft write)

2. For each key: write a PROVISIONAL VALUE (an "intent") into the
   key's own range — a normal Raft write carrying a pointer to the
   transaction record.

3. Commit = a SINGLE Raft write flipping the transaction record
   to COMMITTED. This is the atomic moment. Everything before it
   is invisible; everything after it is visible.

4. Asynchronously, resolve intents into real values.
   Crash here is harmless: a reader that encounters an unresolved
   intent follows the pointer to the transaction record, learns
   the outcome, and resolves it itself.
```

**Cost:** a 5-key transaction across 5 ranges costs 7 Raft rounds (1 record + 5 intents + 1 commit) versus 1 for a single-key write. At 1.4 ms per round with parallelism across the intents, ~4 ms versus ~1.4 ms — plus the contention cost, which is the real one: a contended key with unresolved intents blocks other transactions, and the store acquires a distributed deadlock problem it did not have.

**Preventing it from becoming the default.** This is the part that matters more than the protocol:

1. **Require an explicit namespace-level capability** to use transactions at all. Off by default.
2. **Surface transaction cost in the tenant's dashboard** as a distinct, prominently-priced metric.
3. **Hard-cap** keys per transaction (16) and transaction duration (5 s), with the cap enforced by aborting.
4. **Make the single-key path visibly faster** in documentation and benchmarks, with the multiple written out.

Without these, within two quarters every write is a transaction, the store's performance is a transaction coordinator's performance, and the design's entire latency story is gone.

### 29.2 The clock question

**What our design assumes.** Per §7, linearizability depends on Raft ordering and epoch-based leases, not on clocks. Clocks are used for: TTL semantics, HLC timestamps (which are logical-with-physical-hint and remain monotonic regardless of clock behavior), closed-timestamp targets, and the lease *expiration* check.

**The fully clock-free version** replaces the lease expiration check with pure ReadIndex on every read (§9a) and drops closed-timestamp follower reads entirely (they define staleness in time, which requires a clock to mean anything). Cost: ~1 ms extra per linearizable read, and the total loss of bounded-staleness reads — which means the loss of the multi-region read story (§26) and of the read-scaling story (§9c). That is a large price, and it is why we keep the *bounded* clock dependency for follower reads while keeping linearizability clock-free. **The clock affects how stale a stale read is, never whether a linearizable read is correct.**

**When a clock jumps 40 seconds forward:**

```
1. The node's HLC absorbs it: HLC = max(physical, last_hlc + 1),
   so timestamps stay monotonic. No ordering violation. But the
   node now issues timestamps 40 s in the future, and — because
   HLCs propagate on every message — it drags the whole cluster's
   HLC forward with it. This is the real damage.

2. Offset monitor: each node continuously measures its offset
   against every peer it exchanges messages with. If a node finds
   it disagrees with a majority of peers by > 500 ms, it
   IMMEDIATELY REMOVES ITSELF FROM SERVICE — stops serving reads,
   drops its leases, and refuses to propose. Self-removal, not
   removal by others, so it works even if it is partitioned.

3. Its leases are stolen normally via epoch bump. Correctness is
   preserved throughout — the epoch mechanism never consulted the
   bad clock.

4. Damage assessment: TTLs it evaluated in the bad window may have
   expired keys early. Closed timestamps it published may be too
   aggressive, which is the one genuinely dangerous case — a
   closed timestamp 40 s in the future would let followers serve
   reads at timestamps the leaseholder has not actually closed.
   Mitigation: closed timestamps are capped at
   (local_now − target_lag) AND at (max HLC observed from peers
   − target_lag), so a single fast clock cannot push the closed
   timestamp beyond what the cluster collectively believes.

5. Recovery: NTP resync (step), then rejoin as a follower.
```

**A jump backward** is handled by the HLC's monotonicity invariant with no cluster impact — the node's HLC simply does not move backward — and by the same offset monitor.

### 29.3 Quorum loss and unsafe recovery

Two of three replicas of a range are permanently gone; the data is not in a backup.

**Default behavior: the range stays unavailable, indefinitely.** This is correct. The surviving replica may be missing committed writes — it might have been the follower that never received the last entries the other two committed — and promoting it silently rolls those writes back, violating durability for writes we acknowledged.

**Unsafe recovery** is offered as an explicit operator action with the guardrails making the trade-off visible:

```
Preconditions (all required):
  - The operator supplies --i-understand-this-may-lose-committed-writes
  - The cluster has confirmed the 2 replicas are gone, not partitioned
    (they have been non-live for > 30 min AND the operator confirms
    they are physically destroyed)
  - Two-person approval, recorded

Procedure:
  1. Read the surviving replica's last applied index and its
     Raft log. Compute the maximum POSSIBLE data loss: entries
     committed by the other two but not present here cannot be
     enumerated, but the surviving replica's commit index versus
     its last log index bounds it.
  2. Force a new Raft configuration with the survivor as the sole
     voter (a local, unreplicated configuration change — this is
     the unsafe step).
  3. The range becomes available with 1 replica; the allocator
     restores RF=3 from it.
  4. Emit a permanent, immutable audit record: range ID, key span,
     timestamp, operators, and the bound on possible loss.

What the tenant is told:
  - The exact key span affected
  - The time window in which writes may have been lost
    (from the survivor's last-known-committed timestamp to the
     moment of failure)
  - The bound on the number of possibly-lost writes
  - A recommendation to reconcile that key span against any
    upstream source of truth
```

The audit record and the specific tenant notification are the design, as much as the mechanism. An unsafe recovery that is not loudly recorded becomes an unexplained data inconsistency six months later, and nobody will connect the two.

**Prevention beats recovery**: RF=5 for critical namespaces, AZ diversity as a hard constraint (§6), a 5-minute replacement threshold that is not too eager but not too patient, and backups that make "not in a backup" a false premise.

### 29.4 Metastable failure

**The scenario.** A failover causes a cache-miss storm: the new leaseholder's block cache is cold, so reads that were served from memory now hit disk. Latency rises 10×, clients time out and retry, load triples, disk saturates, more nodes miss liveness heartbeats, more leases move, more caches are cold. Load stays above capacity *even after the original failover is long past.* The system has two stable states, and it is now in the bad one.

**Why it is metastable and not merely overload:** removing the trigger does not fix it. The retries are now the cause.

**Defenses, in order of importance:**

**1. Client-side retry budget (§18).** The single most effective one, because it directly caps the feedback loop's gain. Retries capped at 10% of base request rate means offered load can never exceed 1.1× normal, no matter how bad things get. Without it, offered load is unbounded and no amount of server-side cleverness helps.

**2. Queue-latency-based load shedding (§19).** The server sheds based on how backed up it actually is, not on a threshold tuned when things were healthy. Shedding must be cheaper than serving, or it does not help.

**3. Cache warming on lease transfer.** A graceful lease transfer pre-warms the target's block cache for that range's hot keys before completing the handoff. This attacks the trigger rather than the loop.

**4. Bounded queues everywhere.** Every queue has a depth limit and rejects when full. An unbounded queue converts an overload into a latency-explosion, which converts into timeouts, which converts into retries. Small bounded queues fail fast, which is the behavior that lets the loop damp out.

**5. Admission-control priorities (§19).** Under sustained overload, keep CRITICAL serving even if it means BACKGROUND gets nothing for an hour.

**Detection:** the signature is *request rate above baseline while success rate is below baseline* — the system is doing more work and accomplishing less. That ratio is a better metastability alarm than either metric alone, and it should page.

**Breaking out, if it happens anyway:** shed to CRITICAL-only for long enough for caches to warm and queues to drain (typically 2–5 minutes), then ramp admission back up gradually — not all at once, which simply re-enters the bad state. This should be an automated runbook with a manual trigger, not a decision made from first principles at 3am.

---

## 30. Exercises

Working through these in order builds the design from the bottom up. Each is verifiable.

**1. Single-group Raft (Stage 0).**
Implement a 3-node Raft group with `Get`/`Put` over an in-memory map. Verify: kill the leader mid-write and confirm that either the write is visible on all survivors or on none. Then confirm that a client retrying with an idempotency token gets a consistent answer in both cases.

**2. Prove ReadIndex is necessary.**
Serve reads directly from the Raft leader's local state with no leadership check. Partition the leader from its followers, let the majority elect a new leader and accept a write, then read from the old leader. Observe the stale read. Now implement ReadIndex and show the same test returns an error instead. *This is the single most instructive experiment in the whole design.*

**3. Measure quorum-versus-linearizability.**
Build a Dynamo-style `N=3, W=2, R=2` store. Construct the interleaving where a failed write leaves 1 of 3 replicas updated, then show two successive reads returning new-then-old. Confirm that `W+R>N` did not prevent it.

**4. Epoch-based leases under clock skew.**
Implement time-based leases, then skew one node's clock backward by 10 s and demonstrate two nodes simultaneously believing they hold the lease. Replace with epoch-based leases and repeat.

**5. Compaction and the L0 cliff.**
Load a RocksDB/Pebble instance with sustained random writes. Plot write latency against L0 file count. Find the stall threshold. Then implement L0-based admission control and show that the P99 stays flat where it previously went vertical.

**6. Write amplification, measured.**
Measure actual bytes written to disk per byte of user data under leveled versus tiered compaction on this workload. Compare to the WA≈15 and WA≈5 assumptions in §4 and recompute the node count from your own numbers.

**7. Range splitting under load.**
Implement size-based splitting on a running range receiving continuous writes. Verify no write is lost, no read returns an error other than a retryable `RANGE_SPLITTING`, and that the split point actually divides the load rather than just the bytes.

**8. Closed timestamps.**
Implement closed-timestamp propagation and follower reads. Verify that a follower read at timestamp T returns byte-identical results to a leaseholder read at T, across many T and under concurrent writes.

**9. The retry storm.**
Build a load generator with unbounded exponential-backoff retries. Inject a 5-second cluster stall and observe the offered load after the stall clears. Then add a 10% retry budget and repeat. Plot both.

**10. Jepsen it.**
Run a linearizability check against your implementation with partitions, clock skew, and process pauses injected. Expect to find at least one real bug. If the checker reports no violations on the first run, verify your test is actually exercising failures — a green Jepsen run on a first attempt almost always means the fault injection is not working.

**11. Fencing tokens.**
Implement `PutIfAbsent`-plus-lease locking. Simulate a GC pause longer than the lease TTL in the lock holder and demonstrate the two-writers outcome against a mock protected resource. Add fencing tokens and show the stale writer's write being rejected.

**12. Restore, timed.**
Back up a populated cluster, destroy it, and restore to a specific timestamp. Record the wall-clock time. Compare to your §20 estimate and account for the difference.

---
