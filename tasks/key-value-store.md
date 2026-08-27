## System Design Task: Highly Available, Strongly Consistent Distributed Key-Value Store

### Problem Statement

Design a **distributed key-value store** that is both **highly available** and
**strongly consistent** — the storage layer that dozens of internal teams will
build on for configuration, metadata, session state, ledgers, inventory, and
idempotency keys.

The store must survive **node failures, rack failures, availability-zone loss,
and cross-region network partitions** while still returning correct answers. It
must scale horizontally to hundreds of terabytes and millions of operations per
second, and it must be operable by a small team.

The headline requirement contains a deliberate tension. CAP says you cannot have
both total availability and linearizability during a partition. **Part of the
task is to resolve that tension explicitly** rather than hand-wave it: state
which side you give up, on which operations, for how long, and show the
arithmetic that gets you to the availability target anyway.

Assume this is a **platform** — once teams depend on it, the API and its
guarantees are effectively frozen for a decade.

---

### Functional Requirements

Your system must support:

1. **Core Key-Value Operations**

   * `Get(key)` — read the current value
   * `Put(key, value)` — write or overwrite
   * `Delete(key)`
   * Keys: arbitrary byte strings up to 4 KB
   * Values: arbitrary bytes up to 1 MB

2. **Conditional / Atomic Operations**

   * `CompareAndSwap(key, expected_version, new_value)`
   * `PutIfAbsent(key, value)` — for locks, leader election, idempotency keys
   * Atomic counters (`Increment(key, delta)`)
   * Every mutation returns a **version / revision** the client can reason about

3. **Range and Batch Access**

   * `Scan(start_key, end_key, limit)` in sorted key order
   * `BatchGet(keys[])` — up to 1000 keys per call
   * `BatchWrite(mutations[])` — atomic **within one partition**
   * Define precisely what atomicity you offer *across* partitions, if any

4. **Watches / Change Notification**

   * Clients can subscribe to a key or key prefix and receive ordered change
     events
   * Events must be **gap-free** and resumable from a revision after a
     disconnect

5. **Expiry and Lifecycle**

   * Per-key TTL
   * Leases: a set of keys tied to a client heartbeat, deleted when it stops

6. **Tenancy and Isolation**

   * Namespaces per team, with independent quotas
   * Per-tenant rate limiting so one team cannot starve another

7. **Read Consistency Levels** (client-selectable per call)

   * `linearizable` — reads see all completed writes
   * `bounded_staleness(t)` — cheaper, may lag by at most `t`
   * `eventual` — cheapest, any replica
   * Explain what each costs and which failure modes each survives

---

### Non-Functional Requirements

1. **Scale**

   * 100 billion keys, ~50 TB logical data (pre-replication)
   * Sustained: 1M writes/sec, 5M reads/sec
   * Peak: 2M writes/sec, 10M reads/sec
   * Single keys up to 1 MB; single namespaces up to 10 TB

2. **Latency** (single-region client, same-region data)

   * `Put` P99 ≤ **10 ms**, P99.9 ≤ **50 ms**
   * Linearizable `Get` P99 ≤ **5 ms**
   * Bounded-staleness `Get` P99 ≤ **2 ms**
   * `Scan` of 1000 keys P99 ≤ **50 ms**

3. **Availability**

   * ≥ **99.99%** for reads and writes (≈ 52 minutes/year)
   * Survive with **zero downtime**: single node loss, single rack loss, single
     AZ loss
   * Survive **with degraded but defined behavior**: full region loss,
     cross-region partition
   * State the availability of each consistency level separately — they are not
     the same number

4. **Consistency**

   * Default: **linearizable** for single-key operations
   * Session guarantees (read-your-writes, monotonic reads) must hold even when
     a client is bounced between replicas
   * No lost updates, no resurrected deletes, no split-brain double-writes

5. **Durability**

   * An acknowledged write must survive the immediate loss of any one node and
     any one AZ
   * Target ≤ 1 durability incident per 10⁹ writes
   * Point-in-time restore to any second within the last 7 days

6. **Operability**

   * Rolling upgrades with no write downtime
   * Add or drain a node without operator-authored rebalancing
   * Every guarantee must be **testable**, and you should say how

---

### What You Should Deliver

1. **Requirement clarification & assumptions**

   * Define "consistent" precisely — linearizability, sequential, causal,
     read-your-writes — and say which you are promising for which call
   * State the failure model: crash-stop or Byzantine, fail-fast disks, clock
     assumptions

2. **The CAP decision, made explicit**

   * Which side you sacrifice during a partition, and for which operations
   * PACELC: what you trade for latency when there is *no* partition
   * Why the alternative was rejected

3. **High-level architecture**

   * Components, request path, control plane vs data plane
   * Where the routing decision is made (client, proxy, or server redirect)

4. **Partitioning**

   * Hash vs range partitioning, and the consequences of each for `Scan`
   * Partition sizing, split and merge policy
   * How partitions are placed across nodes, racks, AZs, and regions

5. **Replication and consensus**

   * Replication protocol, replication factor, and quorum sizing
   * Membership changes without losing quorum
   * How a leader is elected and how long failover takes — with numbers

6. **Write path**

   * Every hop from client call to acknowledgement
   * Where the durability point is (WAL fsync? quorum ack? both?)
   * Batching, pipelining, and their effect on P99

7. **Read path**

   * How a **linearizable read** is served without paying a full consensus round
     for every read — and what that optimization assumes
   * How bounded-staleness reads are made safe
   * Follower and cross-region reads

8. **Storage engine**

   * LSM vs B-tree for this workload, with justification
   * Key encoding, MVCC, tombstones, and garbage collection
   * Compaction strategy and its effect on tail latency and space amplification

9. **Failure detection and recovery**

   * How a dead node is distinguished from a slow one
   * Re-replication policy and how you avoid a metastable failure cascade
   * What happens to in-flight writes during a failover

10. **Multi-region**

    * Replica topology, and the latency arithmetic of a cross-region commit
    * Data pinning / locality
    * Behavior during a region partition, per consistency level

11. **Hot keys and hot partitions**

    * Detection and mitigation
    * Why a single hot key is a fundamentally different problem than a hot
      partition

12. **Capacity estimates**

    * Node count, disk, memory, network — and the calculation, not just the
      answer
    * Partition count and its per-node overhead

13. **Client library design**

    * Routing cache and staleness handling
    * Retries, backoff, idempotency, and how you avoid a retry storm

14. **Anti-entropy, backup, restore**

    * Detecting and repairing silent divergence
    * Backup mechanics and restore time for 50 TB

15. **Observability and SLOs**

    * The handful of metrics that actually tell you the store is healthy
    * How you would *prove* linearizability holds in production

16. **Trade-offs**

    * What you deliberately did not build, and what breaks if a team needs it

---

### Variants (Design These Too)

These are separate design tasks, not footnotes. Each one changes the answer.

**Variant A — The AP Store.**
The same API, but availability wins during a partition: every replica accepts
writes at all times. Design the conflict model — last-writer-wins, version
vectors, or CRDTs — and show which of the functional requirements above
(CAS, watches, leases, atomic counters) you can still honor, which become
approximate, and which you must remove from the API entirely.

**Variant B — Global Linearizability.**
Replicas span three regions and the store must be linearizable globally. Do the
latency arithmetic for a cross-region commit. Then design the escape hatches
(region-pinned partitions, leader placement, follower reads with closed
timestamps) that make it usable, and state the write latency a client in the
wrong region must accept.

**Variant C — Small Scale.**
The same guarantees, 3 nodes, 100 GB, 10k ops/sec, one AZ, one part-time
operator. What collapses out of the design? Justify every component you keep,
and name the exact scale threshold at which each dropped component must come
back.

**Variant D — The Migration.**
An existing team runs 4 TB on a sharded MySQL setup with application-level
routing and no cross-shard transactions. Design the cutover to your store with
zero downtime and a working rollback at every step.

---

### Stretch Problems

1. **Multi-key transactions.** Extend to serializable transactions across
   partitions. Which protocol, what it costs, and how you keep it from becoming
   the default path teams reach for.
2. **The clock question.** Your fast-read optimization probably assumes bounded
   clock skew. Design the version that assumes nothing about clocks and quantify
   what it costs. Then design what happens when a machine's clock jumps 40
   seconds.
3. **Quorum loss.** Two of three replicas of one partition are permanently gone
   and the data is not in a backup. Design the unsafe-recovery procedure, its
   guardrails, and what you tell the affected tenant.
4. **Metastable failure.** A cache-miss storm after a failover pushes the
   cluster into a state where load stays above capacity even after the trigger
   is removed. Design the load-shedding that prevents it.

---

### Expectations

* **Do the arithmetic.** Quorum sizes, failover budgets, replica counts per
  node, and cross-region RTTs should appear as numbers, not adjectives.
* **Name concrete mechanisms** — Raft, leader leases, ReadIndex, closed
  timestamps, hinted handoff, Merkle anti-entropy, LSM compaction — and say what
  each one buys.
* **Be precise about guarantees.** "Strongly consistent" without a definition is
  the single most common failure in this design.
* **Show the failure walkthrough.** For each failure class, state what a client
  in flight observes.
* Prefer a **boring design that a small team can operate** over an elegant one
  that needs its own on-call rotation.
* Assume this system will be maintained for a decade by people who did not write
  it.

---
