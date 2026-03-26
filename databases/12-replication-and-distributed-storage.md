# Replication & Distributed Storage: A Staff-Engineer Deep Dive

A comprehensive reference covering data distribution strategies, replication topologies, consensus protocols, sharding, distributed transactions, consistency models, and production architectures used in CockroachDB, TiDB, Spanner, Aurora, Vitess, and YugabyteDB.

---

## Table of Contents

1. [Why Distribute Data?](#1-why-distribute-data)
2. [Replication](#2-replication)
3. [Consensus Protocols](#3-consensus-protocols)
4. [Sharding / Partitioning](#4-sharding--partitioning)
5. [Distributed Transactions](#5-distributed-transactions)
6. [Distributed Query Processing](#6-distributed-query-processing)
7. [Real-World Systems](#7-real-world-systems)
8. [Failure Handling](#8-failure-handling)
9. [Consistency Models](#9-consistency-models)
10. [Comparison Table](#10-comparison-table)

---

## 1. Why Distribute Data?

A single-node database eventually hits a wall. Distribution solves three fundamental problems, but introduces complexity in every other dimension.

### 1.1 The Three Drivers

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Why Distribute Data?                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1. SCALABILITY                                                             │
│     ─ Single node: ~1TB RAM, ~64 cores, ~100K TPS for simple ops           │
│     ─ After vertical scaling is exhausted, horizontal is the only path     │
│     ─ Split data across N nodes -> N× storage, N× aggregate throughput     │
│                                                                             │
│  2. AVAILABILITY                                                            │
│     ─ Single node MTBF: ~3 years for commodity hardware                    │
│     ─ With 1000 nodes, expect a failure every ~1 day                       │
│     ─ Replicas ensure no single failure loses data or blocks reads/writes  │
│                                                                             │
│  3. LATENCY                                                                 │
│     ─ Speed of light: NYC -> London = ~28ms one-way (fiber)                │
│     ─ Place replicas near users to serve reads locally                     │
│     ─ Geo-distributed writes require consensus (adds latency)              │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Quantifying the single-node ceiling (2024 commodity hardware):**

| Resource | Practical Limit | Bottleneck |
|---|---|---|
| CPU cores | 128-256 (2-socket) | Context switching, lock contention |
| RAM | 2-4 TB | Cost, NUMA latency across sockets |
| Storage IOPS | ~1M (NVMe RAID) | PCIe bandwidth, CPU overhead |
| Network | 100 Gbps | Kernel stack overhead, serialization |
| Write TPS (OLTP) | 50K-200K | WAL fsync, lock manager |
| Dataset size | ~50 TB useful | Backup/restore time, VACUUM overhead |

When any of these becomes the bottleneck, you must distribute.

### 1.2 CAP Theorem

Formalized by Eric Brewer (2000) and proved by Gilbert & Lynch (2002). During a network partition, a distributed system must choose between consistency and availability.

```
                        CAP Theorem

                     Consistency (C)
                         /\
                        /  \
                       /    \
                      / CP   \
                     / systems \
                    /   (most   \
                   /   databases)\
                  /              \
                 /________________\
  Availability (A)                Partition
    AP systems                    Tolerance (P)
    (Dynamo, Cassandra)

  ┌──────────────────────────────────────────────────────────────────────┐
  │  P is not optional in a distributed system.                         │
  │  Network partitions WILL happen. You choose C or A during them.     │
  │                                                                     │
  │  CP: Refuse to serve requests if consistency cannot be guaranteed.  │
  │      Example: etcd, ZooKeeper, CockroachDB, Spanner                │
  │                                                                     │
  │  AP: Continue serving requests, but responses may be stale/diverge. │
  │      Example: Cassandra, DynamoDB, Riak                             │
  │                                                                     │
  │  CA: Only possible on a single node (no partitions by definition).  │
  │      Example: PostgreSQL single-node                                │
  └──────────────────────────────────────────────────────────────────────┘
```

**What CAP actually means in practice:**

- "Consistency" in CAP = linearizability (the strongest guarantee). Not ACID consistency.
- "Availability" in CAP = every non-failed node must return a response. Not "five nines uptime."
- Most systems are NOT purely CP or AP. They make nuanced trade-offs per operation.

### 1.3 PACELC: Extending CAP

Daniel Abadi (2012) observed that CAP only describes behavior during partitions, but you also make trade-offs during normal operation.

```
PACELC: if Partition, choose Availability or Consistency;
        Else (normal operation), choose Latency or Consistency.

┌──────────────────────────────────────────────────────────────────────┐
│  System          │ Partition (P) │ Normal (E)   │ Classification    │
├──────────────────┼───────────────┼──────────────┼───────────────────┤
│  Spanner         │ PC (consistent)│ EC (consistent)│ PC/EC           │
│  CockroachDB     │ PC            │ EC           │ PC/EC             │
│  Cassandra       │ PA (available)│ EL (low lat) │ PA/EL             │
│  DynamoDB        │ PA            │ EL           │ PA/EL             │
│  MongoDB         │ PC            │ EC           │ PC/EC             │
│  PostgreSQL+sync │ PC            │ EC           │ PC/EC             │
│  Yugabyte        │ PC            │ EC           │ PC/EC             │
│  NATS JetStream  │ PC            │ EC           │ PC/EC             │
└──────────────────┴───────────────┴──────────────┴───────────────────┘
```

**Key insight**: PACELC explains why Cassandra and DynamoDB are fast even without partitions -- they sacrifice consistency for latency at all times, not just during failures. Spanner pays the latency cost of consensus on every write to maintain consistency always.

### 1.4 CP vs AP: Choosing in Practice

```
Decision Tree: CP vs AP
═══════════════════════

Is data loss / inconsistency acceptable?
│
├── YES ──► How bad is stale data?
│           ├── Tolerable (social feeds, counters) ──► AP
│           └── Annoying but not fatal (caching) ──► AP with reconciliation
│
└── NO ───► Is multi-region required?
            ├── NO ──► CP (single-region Raft-based: etcd, CockroachDB)
            └── YES ──► How important is write latency?
                        ├── Critical (<10ms) ──► AP + conflict resolution
                        └── Acceptable (~100-300ms) ──► CP (Spanner, CockroachDB)
```

**Real-world trade-off examples:**

| Use Case | Choice | Reason |
|---|---|---|
| Bank account balance | CP | Cannot show wrong balance; overdraft = real money |
| Shopping cart | AP | Cart merges are cheap; unavailability loses sales |
| User profile | AP with read-repair | Stale name for 1s is fine; user eventually sees update |
| Inventory count (last item) | CP | Overselling = shipping problem + angry customers |
| Social media like counter | AP | Off-by-one like count is invisible to users |
| Distributed lock service | CP | Incorrect lock = data corruption |
| DNS | AP | Stale record for TTL is by design |

---

## 2. Replication

Replication copies data across multiple nodes. The three topologies -- single-leader, multi-leader, and leaderless -- make fundamentally different trade-offs.

### 2.1 Single-Leader (Primary-Secondary) Replication

The most common replication topology. All writes go to one node (the leader), which streams changes to followers.

```
Single-Leader Replication Architecture
═══════════════════════════════════════

    Writes                              Reads (can be served by any node)
      │                                   │
      ▼                                   ▼
┌──────────┐   replication stream   ┌──────────┐
│  Leader   │ ────────────────────► │ Follower  │
│ (Primary) │                       │ (Replica) │
│           │                       │           │
│  WAL ──►  │   ┌──────────┐       │  Apply    │
│  Commit   │──►│ Follower  │       │  WAL      │
│           │   │ (Replica) │       │  entries  │
└──────────┘   └──────────┘       └──────────┘
      │
      │         ┌──────────┐
      └────────►│ Follower  │
                │ (Replica) │
                └──────────┘

Write Path:
  1. Client sends write to leader
  2. Leader writes to local WAL
  3. Leader sends WAL entry to followers
  4. Followers apply WAL entry to local storage
  5. Leader acknowledges client (timing depends on sync mode)
```

#### Synchronous vs Asynchronous vs Semi-Synchronous

```
Sync/Async Replication Modes
═════════════════════════════

SYNCHRONOUS (all replicas):
  Client ──write──► Leader ──WAL──► Follower 1 ──ACK──┐
                      │                                 │
                      ├─────WAL──► Follower 2 ──ACK──┤
                      │                                 │
                      ◄─── waits for ALL ACKs ─────────┘
                      │
                      ▼
                   ACK to client

  + Zero data loss (RPO = 0)
  - Write latency = max(follower latencies)
  - Any follower failure stalls ALL writes
  - Rarely used with >2 replicas

ASYNCHRONOUS:
  Client ──write──► Leader ──ACK to client (immediately)
                      │
                      ├─────WAL──► Follower 1  (background)
                      └─────WAL──► Follower 2  (background)

  + Lowest write latency
  + Leader never blocks on followers
  - Data loss if leader crashes before replication (RPO > 0)
  - Followers may lag seconds/minutes behind

SEMI-SYNCHRONOUS (1 of N):
  Client ──write──► Leader ──WAL──► Follower 1 ──ACK──┐
                      │                                 │
                      ├─────WAL──► Follower 2           │
                      │       (async, no wait)          │
                      ◄─── waits for 1 ACK ────────────┘
                      │
                      ▼
                   ACK to client

  + At least one replica always up-to-date
  + If the sync replica fails, another is promoted to sync
  + Good balance of durability and latency
  - Write latency = RTT to closest replica
```

**Comparison table:**

| Mode | RPO | Write Latency | Availability | Use Case |
|---|---|---|---|---|
| Synchronous (all) | 0 | Highest (max RTT) | Lowest | Financial audit logs |
| Semi-synchronous (1 of N) | 0 | Medium (min RTT) | High | Production OLTP default |
| Asynchronous | > 0 (seconds) | Lowest | Highest | Read replicas, analytics |

#### Replication Lag and Its Consequences

When followers are behind the leader, clients reading from followers see stale data. This creates several anomalies.

```
Replication Lag Anomalies
══════════════════════════

1. READ-AFTER-WRITE INCONSISTENCY (read-your-writes violation)
   ─────────────────────────────────────────────────────────────
   Time ───────────────────────────────────────────────────────►

   Client:   WRITE(x=5) ──────────────── READ(x) → sees x=3 (stale!)
                │                              │
   Leader:   x=3 ──► x=5                      │
                │                              │
   Follower: x=3 ─────────── (lag) ──── x=3   ▲ (read hits follower)
                                                │
                                        follower hasn't caught up yet

2. NON-MONOTONIC READS
   ─────────────────────
   Time ───────────────────────────────────────────────────────►

   Client:   READ(x) → 5 ──────── READ(x) → 3  (time went backwards!)
                │                      │
   Follower1: x=5 (caught up)         │
   Follower2:                    x=3 (still lagging)
                                      │
                              load balancer sent to different follower

3. CAUSALITY VIOLATION
   ─────────────────────
   User A writes: "I'm moving to NYC"     (t=100)
   User B writes: "Great, let's meet up!" (t=101, references A's post)

   Follower receives B's write first → shows "Great, let's meet up!"
   without showing A's original post → confusing to readers
```

**Solutions for replication lag anomalies:**

| Anomaly | Solution | Implementation |
|---|---|---|
| Read-after-write | Read from leader after write | Route reads to leader for T seconds after a write |
| Read-after-write | Read from replica at known position | Track LSN of last write; wait for replica to reach it |
| Non-monotonic reads | Sticky sessions | Pin client to one replica (hash of user ID) |
| Non-monotonic reads | Monotonic read tokens | Client sends last-seen LSN; replica waits or redirects |
| Causality violation | Causal consistency | Dependency tracking between writes |

#### PostgreSQL Streaming Replication

```
PostgreSQL Streaming Replication Internals
═══════════════════════════════════════════

Primary                                  Standby
┌─────────────────────┐                 ┌─────────────────────┐
│                     │                 │                     │
│  Backend processes  │                 │  Startup process    │
│       │             │                 │  (WAL receiver)     │
│       ▼             │                 │       │             │
│  Shared Buffers     │                 │       ▼             │
│       │             │                 │  WAL receiver       │
│       ▼             │  WAL stream     │  process            │
│  WAL Writer ──► WAL │ ═══════════════►│       │             │
│       │         log │  (walsender     │       ▼             │
│       ▼             │   process)      │  Write WAL to       │
│  WAL Archiver ──►   │                 │  local disk         │
│  Archive storage    │                 │       │             │
│                     │                 │       ▼             │
│  pg_stat_replication│                 │  Recovery process   │
│  (monitoring view)  │                 │  replays WAL        │
│                     │                 │       │             │
└─────────────────────┘                 │       ▼             │
                                        │  Hot Standby        │
                                        │  (serves reads)     │
                                        └─────────────────────┘

Configuration (postgresql.conf on primary):
  wal_level = replica              # or 'logical' for logical replication
  max_wal_senders = 10             # max concurrent replication connections
  synchronous_commit = on          # 'on', 'remote_apply', 'remote_write', 'local', 'off'
  synchronous_standby_names = 'FIRST 1 (standby1, standby2)'

Synchronous commit levels:
  ┌──────────────┬──────────────────────────────────────────────────────┐
  │ Level        │ What primary waits for                              │
  ├──────────────┼──────────────────────────────────────────────────────┤
  │ off          │ Nothing (WAL may not even be flushed locally)       │
  │ local        │ WAL flushed to primary's disk                       │
  │ remote_write │ WAL received by standby (in OS buffer, not flushed) │
  │ on           │ WAL flushed to standby's disk                       │
  │ remote_apply │ WAL applied (visible to queries) on standby         │
  └──────────────┴──────────────────────────────────────────────────────┘
```

**Monitoring replication lag in PostgreSQL:**

```sql
-- On primary: check replication status
SELECT client_addr, state,
       sent_lsn, write_lsn, flush_lsn, replay_lsn,
       pg_wal_lsn_diff(sent_lsn, replay_lsn) AS replay_lag_bytes,
       reply_time
FROM pg_stat_replication;

-- On standby: check how far behind
SELECT now() - pg_last_xact_replay_timestamp() AS replication_delay;

-- Alert if lag exceeds threshold
SELECT CASE
  WHEN pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) > 1073741824
  THEN 'CRITICAL: >1GB lag'
  ELSE 'OK'
END FROM pg_stat_replication;
```

#### MySQL Replication

```
MySQL Replication Modes
════════════════════════

1. CLASSIC (Statement-Based / Row-Based / Mixed)
   Source ──► Binary Log ──► Replica I/O Thread ──► Relay Log ──► SQL Thread ──► Apply

2. GTID-BASED (Global Transaction Identifiers)
   Source ──► Binary Log (GTID: source_uuid:txn_id) ──► Replica

   Advantage: Replica knows exactly which transactions it has applied.
   Failover: New source says "give me everything after GTID X" -- no log position math.

3. GROUP REPLICATION (Multi-Primary / Single-Primary)
   ┌──────────┐    ┌──────────┐    ┌──────────┐
   │  Node 1  │◄──►│  Node 2  │◄──►│  Node 3  │
   │ (Primary)│    │ (Primary)│    │ (Primary)│
   └──────────┘    └──────────┘    └──────────┘
        Paxos-based group communication (XCom)

   - Certification-based conflict detection
   - Write sets are broadcast to all nodes
   - Conflicting transactions are rolled back on the originating node
   - Works as single-primary (recommended) or multi-primary

Binary Log Formats:
  ┌──────────┬───────────────────────────────────────────────────┐
  │ Format   │ Description                                       │
  ├──────────┼───────────────────────────────────────────────────┤
  │ STATEMENT│ Logs SQL statements. Compact but non-deterministic│
  │          │ functions (NOW(), RAND()) cause drift.            │
  │ ROW      │ Logs actual row changes (before/after image).    │
  │          │ Deterministic but verbose for bulk operations.    │
  │ MIXED    │ Statement by default; switches to ROW when needed│
  └──────────┴───────────────────────────────────────────────────┘
```

#### Leader Election and Failover

```
Leader Failover Sequence
═════════════════════════

Normal operation:
  Client ──► Leader (writes) ──replication──► Follower 1
                                              Follower 2

Leader fails:
  Client ──► Leader (DEAD) ✗

  Step 1: DETECTION
    - Followers detect leader is unreachable (heartbeat timeout)
    - Typical timeout: 10-30 seconds (too short = false positives)
    - Who detects? Followers, external monitor, or consensus group

  Step 2: ELECTION
    - Choose the follower with the most up-to-date data
    - Methods:
      a) Raft/Paxos consensus among replicas
      b) External coordinator (etcd, ZooKeeper)
      c) Manual promotion (safest, slowest)

  Step 3: RECONFIGURATION
    - New leader starts accepting writes
    - Other followers repoint to new leader
    - Clients redirect (DNS update, proxy reconfiguration, VIP failover)

  Step 4: OLD LEADER RECOVERY
    - When old leader comes back, it MUST become a follower
    - It may have writes that were never replicated (async case)
    - Those unreplicated writes must be discarded or reconciled

Timeline:
  0s          10s              15s              20s
  │ Leader    │ Heartbeat      │ Election       │ New leader
  │ crashes   │ timeout fires  │ completes      │ serving traffic
  │           │                │                │
  └───────────┴────────────────┴────────────────┘
              ~10-30s total downtime (automated)
              ~minutes with manual intervention
```

#### Split-Brain Problem

```
Split-Brain: Two Leaders Simultaneously
════════════════════════════════════════

Network partition between nodes:

  ┌─────────────────┐    PARTITION    ┌─────────────────┐
  │   Datacenter A  │ ═══╪═══════╪═══│   Datacenter B  │
  │                 │    ╪  BROKEN ╪  │                 │
  │  Leader         │    ╪ NETWORK ╪  │  Follower       │
  │  (still running)│    ╪         ╪  │  (promoted to   │
  │                 │               │  │   "leader")     │
  │  Clients here   │               │  │  Clients here   │
  │  write to A     │               │  │  write to B     │
  └─────────────────┘               │  └─────────────────┘

  Result: DIVERGENT DATA
    Client in A: UPDATE balance SET amount=500 WHERE user=1;
    Client in B: UPDATE balance SET amount=300 WHERE user=1;

    When partition heals: balance is 500 on A, 300 on B. Which is correct?

Prevention Strategies:

  1. FENCING TOKENS
     ─────────────────
     Monotonically increasing token issued with each leadership grant.
     Storage layer rejects writes with tokens lower than the highest seen.

     Epoch 1: Leader A writes with token=1 ──► Storage accepts
     Epoch 2: Leader B writes with token=2 ──► Storage accepts
              Leader A writes with token=1 ──► Storage REJECTS (stale token)

  2. QUORUM-BASED ELECTION
     ─────────────────────
     Require majority (N/2 + 1) votes to become leader.
     With 3 nodes: need 2 votes. A partition can have at most one majority.

     Nodes: [A, B, C]
     Partition: {A} vs {B, C}
     - A cannot get 2 votes (only has itself) ──► A steps down
     - B or C can get 2 votes ──► one becomes leader

  3. STONITH (Shoot The Other Node In The Head)
     ─────────────────────────────────────────
     Hardware-level fencing: physically power off the old leader via IPMI/iLO.
     Used in traditional HA clusters (Pacemaker, Corosync).
     Guarantees the old leader is truly dead, not just unreachable.
```

### 2.2 Multi-Leader Replication

Multiple nodes accept writes independently. Used primarily for multi-datacenter setups where write latency to a remote leader is unacceptable.

```
Multi-Leader Replication (Multi-DC)
════════════════════════════════════

  Datacenter US-East          Datacenter EU-West          Datacenter AP-SE
  ┌──────────────┐            ┌──────────────┐            ┌──────────────┐
  │  Leader A    │◄──────────►│  Leader B    │◄──────────►│  Leader C    │
  │              │  async     │              │  async      │              │
  │  Followers   │  repl      │  Followers   │  repl       │  Followers   │
  │  ┌──┐ ┌──┐  │            │  ┌──┐ ┌──┐  │            │  ┌──┐ ┌──┐  │
  │  │F1│ │F2│  │            │  │F3│ │F4│  │            │  │F5│ │F6│  │
  │  └──┘ └──┘  │            │  └──┘ └──┘  │            │  └──┘ └──┘  │
  └──────────────┘            └──────────────┘            └──────────────┘
       │                           │                           │
  Local writes               Local writes               Local writes
  (~1ms latency)             (~1ms latency)             (~1ms latency)

  vs. Single-Leader across DCs:
  Write in EU-West ──► Leader in US-East ──► ~80ms RTT each way
```

#### Conflict Resolution

When two leaders concurrently modify the same record, you get a write-write conflict. This is the fundamental challenge of multi-leader replication.

```
Multi-Leader Conflict Example
══════════════════════════════

  Leader A (US-East)                    Leader B (EU-West)
  ──────────────────                    ──────────────────
  t=100: UPDATE title = "Version A"    t=101: UPDATE title = "Version B"
         WHERE doc_id = 42                     WHERE doc_id = 42

  Both succeed locally.

  Replication delivers:
    A receives B's write at t=150
    B receives A's write at t=155

  CONFLICT: title should be "Version A" or "Version B"?

  ┌────────────────────────────────────────────────────────────────────┐
  │  Conflict Resolution Strategies                                    │
  ├────────────────────────────────────────────────────────────────────┤
  │                                                                    │
  │  1. LAST WRITER WINS (LWW)                                        │
  │     - Attach a timestamp to each write                            │
  │     - Highest timestamp wins, other is silently discarded         │
  │     - Simple but LOSES DATA                                       │
  │     - Used by: Cassandra, DynamoDB (default)                      │
  │     - Problem: clock skew can make "last" meaningless             │
  │                                                                    │
  │  2. MERGE / APPLICATION-LEVEL RESOLUTION                          │
  │     - Store both versions ("siblings" in Riak terminology)        │
  │     - Application reads both, presents merge UI or auto-merges   │
  │     - Google Docs: OT (Operational Transformation)                │
  │     - Shopping cart: union of items in both versions              │
  │                                                                    │
  │  3. CRDTs (Conflict-free Replicated Data Types)                   │
  │     - Data structures that mathematically guarantee convergence   │
  │     - G-Counter: grow-only counter (each node has its own slot)   │
  │     - PN-Counter: increment/decrement counter                     │
  │     - OR-Set: observed-remove set                                 │
  │     - LWW-Register: last-writer-wins register (with vector clock) │
  │     - Used by: Riak, Redis CRDB, Automerge, Yjs                  │
  │                                                                    │
  │  4. CUSTOM CONFLICT HANDLERS                                      │
  │     - Database calls application-provided function on conflict    │
  │     - Bucardo (PostgreSQL multi-leader) supports this             │
  │     - Most flexible, most complex                                 │
  │                                                                    │
  └────────────────────────────────────────────────────────────────────┘
```

**CRDT Example -- G-Counter (grow-only counter):**

```
G-Counter: Each node maintains its own count. Total = sum of all.

  Node A: {A: 5, B: 0, C: 0}  → total = 5
  Node B: {A: 0, B: 3, C: 0}  → total = 3
  Node C: {A: 0, B: 0, C: 7}  → total = 7

  Merge (element-wise max):
  Result: {A: 5, B: 3, C: 7}  → total = 15

  Properties:
  - Commutative: merge(A, B) = merge(B, A)
  - Associative: merge(merge(A, B), C) = merge(A, merge(B, C))
  - Idempotent: merge(A, A) = A

  → Always converges, regardless of message ordering or duplication.
```

#### Replication Topologies

```
Multi-Leader Replication Topologies
════════════════════════════════════

1. ALL-TO-ALL (most common)

   A ◄───► B
   │ ╲   ╱ │
   │  ╲ ╱  │
   │   ╳   │
   │  ╱ ╲  │
   │ ╱   ╲ │
   C ◄───► D

   + Every node replicates to every other
   + Fault tolerant (any link can fail)
   - O(N^2) connections
   - Causality issues: may receive effects before causes

2. STAR / HUB-AND-SPOKE

       B
       │
   C ──A── D
       │
       E

   + Simple, O(N) connections
   - Hub (A) is SPOF
   - Higher latency for non-hub pairs

3. CIRCULAR

   A ──► B ──► C ──► D ──► A

   + Simple, O(N) connections
   - Any node failure breaks the ring
   - Highest latency for distant pairs
   - Must tag writes with origin to prevent infinite loops
```

### 2.3 Leaderless (Dynamo-Style) Replication

No designated leader. Any node can accept reads and writes. Pioneered by Amazon's Dynamo paper (2007). Used by Cassandra, Riak, and Voldemort.

```
Leaderless Replication Architecture
════════════════════════════════════

  Client writes to multiple nodes simultaneously:

         ┌──────────────────────────────────────────┐
         │             Coordinator                   │
         │  (any node, or client-side library)       │
         └────┬────────────┬────────────┬───────────┘
              │            │            │
              ▼            ▼            ▼
         ┌────────┐  ┌────────┐  ┌────────┐
         │ Node 1 │  │ Node 2 │  │ Node 3 │
         │  (ACK) │  │  (ACK) │  │ (FAIL) │
         └────────┘  └────────┘  └────────┘

  Write succeeds if W nodes acknowledge (here W=2, N=3).
  Read queries R nodes and takes the most recent value.

  Quorum condition: W + R > N
  ──────────────────────────
  Guarantees at least one node in the read set has the latest write.
```

#### Quorum Reads and Writes

```
Quorum: W + R > N
══════════════════

  N = total replicas = 5
  W = write quorum  = 3
  R = read quorum   = 3

  W + R = 6 > 5 = N  ✓  (overlap of at least 1 node guaranteed)

  Write to 5 nodes, 3 must ACK:
  ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌────┐
  │ N1 │ │ N2 │ │ N3 │ │ N4 │ │ N5 │
  │ ✓  │ │ ✓  │ │ ✗  │ │ ✓  │ │ ✗  │
  └────┘ └────┘ └────┘ └────┘ └────┘
  ACK     ACK    fail   ACK    fail    → W=3 met, write succeeds

  Read from 5 nodes, need 3 responses:
  ┌────┐ ┌────┐ ┌────┐ ┌────┐ ┌────┐
  │ N1 │ │ N2 │ │ N3 │ │ N4 │ │ N5 │
  │v=5 │ │v=5 │ │v=4 │ │v=5 │ │v=4 │
  └────┘ └────┘ └────┘ └────┘ └────┘
  new     new    stale  new    stale

  3 responses with v=5 → return v=5 (latest)

  Common configurations:
  ┌──────────┬───┬───┬───┬────────────────────────────────────────┐
  │ Config   │ N │ W │ R │ Trade-off                              │
  ├──────────┼───┼───┼───┼────────────────────────────────────────┤
  │ Balanced │ 3 │ 2 │ 2 │ Good balance of consistency & speed   │
  │ Fast-R   │ 3 │ 3 │ 1 │ Fast reads, slow writes              │
  │ Fast-W   │ 3 │ 1 │ 3 │ Fast writes, slow reads, less durable│
  │ Large N  │ 5 │ 3 │ 3 │ Survives 2 node failures             │
  └──────────┴───┴───┴───┴────────────────────────────────────────┘
```

#### Sloppy Quorum and Hinted Handoff

```
Sloppy Quorum
═══════════════

  Normal quorum: write MUST go to the designated N nodes for a key.

  Sloppy quorum: if a designated node is down, write to a
  NON-designated node temporarily.

  Ring:  [A] [B] [C] [D] [E]    (key K maps to A, B, C)

  Normal:  Write K ──► A, B, C   (W=2 of these 3)

  If B is down:
  Sloppy:  Write K ──► A, C, D   (D is a temporary holder)
           D stores the value with a "hint": "this belongs to B"

  When B recovers:
  Hinted Handoff: D ──► B (sends the value to its rightful owner)
                  D deletes its temporary copy

  Trade-off:
  + Higher write availability (fewer write failures)
  - Sloppy quorum does NOT guarantee read-after-write consistency
    (reading from A, B, C might miss the value stored on D)
  - Cassandra: sloppy quorum OFF by default (strict quorum)
  - DynamoDB: uses sloppy quorum
```

#### Read Repair and Anti-Entropy

```
Read Repair
═════════════

  During a quorum read, coordinator detects stale replicas:

  Read K from N1, N2, N3:
    N1: {value: "foo", timestamp: 100}  ← stale
    N2: {value: "bar", timestamp: 200}  ← latest
    N3: {value: "bar", timestamp: 200}  ← latest

  Coordinator:
    1. Returns "bar" to client (latest value)
    2. Sends "bar" to N1 to fix it (read repair)

  Read repair is lazy -- only fixes stale replicas during reads.
  Rarely-read keys may stay stale forever.

Anti-Entropy (Background Repair)
═════════════════════════════════

  Merkle tree comparison between nodes:

  Node A                      Node B
  ┌──────────┐                ┌──────────┐
  │ Root: abc │                │ Root: xyz │  ← roots differ!
  │  ┌───┴───┐                │  ┌───┴───┐
  │  L:ab  R:cd               │  L:ab  R:ef   ← left subtrees match
  │                           │                  right subtrees differ
  │  → Only sync right        │                  → exchange only those keys
  │    subtree keys           │
  └──────────┘                └──────────┘

  Cassandra runs `nodetool repair` periodically.
  Recommended: at least once within gc_grace_seconds (default 10 days)
  to prevent zombie data from tombstone expiration.
```

#### Vector Clocks

```
Vector Clocks: Tracking Causality in Leaderless Systems
════════════════════════════════════════════════════════

  Each node maintains a vector of counters, one per node.

  Event at Node X: increment X's counter.

  Example with 3 nodes (A, B, C):

  Initial state: all nodes have {A:0, B:0, C:0}

  1. Client writes to A:
     A: {A:1, B:0, C:0}

  2. A replicates to B:
     B: {A:1, B:0, C:0}

  3. Client writes to B:
     B: {A:1, B:1, C:0}

  4. Concurrent write to C (no knowledge of A or B's writes):
     C: {A:0, B:0, C:1}

  5. Compare B and C's clocks:
     B: {A:1, B:1, C:0}
     C: {A:0, B:0, C:1}

     Neither dominates the other → CONFLICT (concurrent writes)

     If B were {A:1, B:1, C:1} and C were {A:1, B:0, C:1}:
       B dominates C (every element >=) → B happened after C, no conflict

  Dominance rule:
    V1 dominates V2 iff ∀i: V1[i] >= V2[i] AND ∃j: V1[j] > V2[j]
    If neither dominates → concurrent → conflict

  Problem: vector clocks grow with number of clients (or nodes).
  Riak solution: prune old entries, accept rare false conflicts.
  DynamoDB: simpler -- uses LWW with server-side timestamps.
```

---

## 3. Consensus Protocols

Consensus protocols allow a group of nodes to agree on a value (or a sequence of values) even when some nodes fail. They are the backbone of replicated state machines.

### 3.1 Raft (Detailed)

Raft was designed by Diego Ongaro and John Ousterhout (2014) as an understandable alternative to Paxos. It is used in etcd, CockroachDB, TiKV, Consul, and many others.

```
Raft Overview
══════════════

  Three roles:
  ┌──────────┐    ┌──────────┐    ┌──────────┐
  │  Leader   │    │ Follower │    │ Candidate│
  │           │    │          │    │          │
  │ Accepts   │    │ Receives │    │ Requests │
  │ all client│    │ log      │    │ votes to │
  │ requests  │    │ entries  │    │ become   │
  │           │    │ from     │    │ leader   │
  │ Replicates│    │ leader   │    │          │
  │ log to    │    │          │    │          │
  │ followers │    │ Votes in │    │          │
  └──────────┘    │ elections│    └──────────┘
                   └──────────┘

  State transitions:

  Follower ──(election timeout)──► Candidate
  Candidate ──(wins election)──► Leader
  Candidate ──(loses/timeout)──► Candidate (new term)
  Candidate ──(discovers leader)──► Follower
  Leader ──(discovers higher term)──► Follower
```

#### Leader Election (Detailed)

```
Raft Leader Election
═════════════════════

  Term: monotonically increasing logical clock. Each term has at most one leader.

  STEP 1: Election timeout fires on a follower (randomized: 150-300ms)

  Node B (follower) hasn't heard from leader:
    - Increments current term: term = 2
    - Transitions to Candidate
    - Votes for itself
    - Sends RequestVote RPC to all other nodes

  STEP 2: Other nodes vote

  ┌───────┐  RequestVote(term=2, lastLog=(idx:5,term:1))  ┌───────┐
  │       │ ─────────────────────────────────────────────► │       │
  │ B     │                                                │ A     │
  │ (Cand)│ ◄─────────────────────────────────────────────│(Follow)│
  │       │  VoteGranted=true                              │       │
  └───────┘                                                └───────┘

  ┌───────┐  RequestVote(term=2, lastLog=(idx:5,term:1))  ┌───────┐
  │       │ ─────────────────────────────────────────────► │       │
  │ B     │                                                │ C     │
  │ (Cand)│ ◄─────────────────────────────────────────────│(Follow)│
  │       │  VoteGranted=true                              │       │
  └───────┘                                                └───────┘

  B has 3 votes (A, C, self) out of 3 nodes → B is leader for term 2.

  Voting rules:
    1. Each node votes for at most ONE candidate per term
    2. Vote granted only if candidate's log is at least as up-to-date
       (compared by: last log term, then last log index)
    3. If node sees a higher term in any message → step down to follower

  Split vote scenario (even number of nodes or simultaneous candidates):
    - No candidate gets majority
    - All candidates' election timers expire (randomized to break symmetry)
    - New election with incremented term
    - Randomized timeouts make repeated split votes unlikely

  Election Timeline:
  ────────────────────────────────────────────────────────────────────►
  Term 1: Leader A          | A fails |  Term 2: B elected
  ─────────────────────────────────────────────────────────────────────
  A sends heartbeats        | timeout |  B sends RequestVote
  to B, C every 50ms        |         |  B wins, sends heartbeats
```

#### Log Replication

```
Raft Log Replication
═════════════════════

  The leader receives client commands and appends them to its log.
  It then replicates log entries to followers.

  Leader's Log:
  ┌───────┬───────┬───────┬───────┬───────┐
  │ idx=1 │ idx=2 │ idx=3 │ idx=4 │ idx=5 │
  │ t=1   │ t=1   │ t=1   │ t=2   │ t=2   │
  │ x←1   │ y←2   │ x←3   │ y←7   │ z←4   │
  └───────┴───────┴───────┴───────┴───────┘
                                      ▲ newest

  AppendEntries RPC:
  ┌─────────────────────────────────────────────────────────────────┐
  │  term:           2           (leader's current term)           │
  │  leaderId:       B                                             │
  │  prevLogIndex:   4           (index of entry before new ones)  │
  │  prevLogTerm:    2           (term of prevLogIndex entry)      │
  │  entries:        [{idx:5, term:2, cmd: z←4}]                  │
  │  leaderCommit:   4           (leader's commit index)           │
  └─────────────────────────────────────────────────────────────────┘

  Follower processing:
    1. Check prevLogIndex and prevLogTerm match local log
       - If mismatch → reject (follower is behind or diverged)
       - Leader decrements prevLogIndex and retries
    2. Append new entries to log
    3. Update commit index to min(leaderCommit, index of last new entry)
    4. Apply committed entries to state machine

  Commit rule:
    An entry is committed when the leader has replicated it to a MAJORITY.

    Leader:    [1][2][3][4][5]  committed through idx=4
    Follower A:[1][2][3][4][5]  ✓
    Follower B:[1][2][3][4]     ✓ (4 is on majority: Leader, A, B)
    Follower C:[1][2][3]        behind (3 of 4 still have idx=4)

    Safety: committed entries are NEVER overwritten.

  Log compaction via snapshots:
  ┌──────────────────────────────────────────────────────────────────┐
  │  Before: [1][2][3][4][5][6][7][8][9][10]                       │
  │                                                                  │
  │  Snapshot at idx=7:                                              │
  │  ┌──────────────────┐ [8][9][10]                                │
  │  │ Snapshot          │                                           │
  │  │ lastIncludedIdx=7 │                                           │
  │  │ lastIncludedTerm=3│                                           │
  │  │ State machine dump│                                           │
  │  └──────────────────┘                                           │
  │                                                                  │
  │  Entries 1-7 can be discarded from the log.                     │
  └──────────────────────────────────────────────────────────────────┘
```

#### Safety Properties

```
Raft Safety Guarantees
═══════════════════════

  1. ELECTION SAFETY
     At most one leader per term.
     Proof: each node votes once per term + leader needs majority
            → two leaders would need > N votes total → impossible

  2. LEADER APPEND-ONLY
     Leader never overwrites or deletes its own log entries.
     It only appends new entries.

  3. LOG MATCHING
     If two logs contain an entry with the same index and term,
     then all preceding entries are identical.
     Proof: AppendEntries consistency check (prevLogIndex/prevLogTerm)

  4. LEADER COMPLETENESS
     If an entry is committed in a given term, it will be present
     in the logs of all leaders of higher terms.
     Proof: committed = on majority; leader needs majority of votes;
            vote requires log to be at least as up-to-date
            → new leader's log must include all committed entries

  5. STATE MACHINE SAFETY
     If a node has applied an entry at index i to its state machine,
     no other node will ever apply a different entry at index i.
     Follows from log matching + leader completeness.
```

#### Membership Changes

```
Raft Membership Changes
════════════════════════

  Problem: switching from config C_old to C_new atomically is impossible
  in a distributed system (some nodes see old config, others see new).

  Solution 1: JOINT CONSENSUS (original Raft paper)

    C_old ──► C_old,new (joint) ──► C_new

    During joint consensus:
      - Log entries replicated to majorities of BOTH old AND new configs
      - Either config's majority can elect a leader

    Phase 1: Leader replicates C_old,new log entry
    Phase 2: After C_old,new is committed, leader replicates C_new
    Phase 3: After C_new is committed, nodes not in C_new shut down

  Solution 2: SINGLE-NODE CHANGES (simpler, used by etcd)

    Add or remove ONE node at a time.

    Claim: adding/removing one node from an N-node cluster always
    ensures old and new majorities overlap.

    Example: 3 nodes → 4 nodes
      Old majority: 2 of {A, B, C}
      New majority: 3 of {A, B, C, D}
      Any set of 2 from {A,B,C} and any set of 3 from {A,B,C,D}
      must share at least 1 member. ✓

    Sequence for adding node D to {A, B, C}:
      1. D starts as non-voting member (catches up on log)
      2. Leader proposes config change: {A,B,C} → {A,B,C,D}
      3. Config change committed using NEW config's majority (3 of 4)
      4. D is now a full voting member
```

### 3.2 Multi-Paxos

The original consensus protocol by Leslie Lamport (1989/1998). More general than Raft but harder to implement correctly.

```
Paxos Roles
═════════════

  PROPOSER: proposes a value (client request)
  ACCEPTOR: votes on proposals, stores accepted values
  LEARNER:  learns the decided value (often the same nodes)

  In practice, nodes play multiple roles simultaneously.

  Single-Decree Paxos (agree on ONE value):

  Phase 1: PREPARE
  ────────────────
  Proposer ──Prepare(n)──► Acceptors
                            │
                            If n > highest seen proposal number:
                              Promise not to accept proposals < n
                              Return any previously accepted (n', v')
                            Else:
                              Reject (or ignore)

  Acceptor ──Promise(n, accepted_n, accepted_v)──► Proposer

  Phase 2: ACCEPT
  ────────────────
  Proposer receives promises from majority of acceptors.
  If any acceptor already accepted a value: use the value from
    the highest-numbered accepted proposal.
  Otherwise: proposer can choose any value.

  Proposer ──Accept(n, v)──► Acceptors
                              │
                              If n >= highest promised:
                                Accept the value
                              Else:
                                Reject

  Acceptor ──Accepted(n, v)──► Learners

  Value is chosen when a majority of acceptors accept the same (n, v).

Multi-Paxos Optimization:
══════════════════════════

  Single-decree Paxos: 2 rounds per value (Prepare + Accept).
  Multi-Paxos: elect a stable leader, skip Phase 1 for subsequent values.

  Leader election:  Prepare(n) for all future slots → 1 round
  Subsequent writes: Accept(n, v) only → 1 round per value

  This is essentially what Raft does, but Raft makes the leader
  election and log management explicit and easier to understand.
```

### 3.3 Raft vs Paxos Comparison

| Aspect | Raft | Multi-Paxos |
|---|---|---|
| **Understandability** | Designed for clarity | Notoriously difficult |
| **Leader** | Required; single stable leader | Optional; multi-proposer possible |
| **Log ordering** | Entries committed in order | Slots can be filled out of order |
| **Reconfiguration** | Joint consensus or single-node | Separate protocol needed |
| **Liveness** | Randomized election timeout | Dueling proposers can livelock |
| **Implementations** | etcd, CockroachDB, TiKV, Consul | Chubby (Google), Spanner |
| **Latency (steady state)** | 1 RTT (AppendEntries) | 1 RTT (Accept only, with stable leader) |
| **Correctness proofs** | TLA+ spec available | Original proof by Lamport |
| **Industry adoption** | Dominant in open-source | Dominant at Google |

### 3.4 EPaxos (Egalitarian Paxos)

```
EPaxos (2013, Yale)
═══════════════════

  No designated leader. Any node can propose (egalitarian).
  Optimal for geo-distributed deployments: writes go to nearest node.

  Key insight: most commands don't conflict (touch different keys).
  Non-conflicting commands can commit in 1 RTT (fast path).
  Conflicting commands need 2 RTTs (slow path).

  Fast path (no conflict):
    Replica R1 receives command
    R1 sends PreAccept to fast quorum (⌊N/2⌋ + ⌊(⌊N/2⌋+1)/2⌋)
    All agree on ordering → R1 commits in 1 RTT

  Slow path (conflict detected):
    During PreAccept, another replica has a conflicting command
    R1 must run Paxos-Accept phase → 2 RTTs total

  Trade-offs:
    + No leader bottleneck
    + Geo-optimal: closest replica handles the command
    + 1 RTT for non-conflicting commands
    - Complex recovery protocol
    - Dependency tracking between commands
    - Rarely implemented in production (complexity)
    - CockroachDB evaluated EPaxos, chose Raft instead
```

---

## 4. Sharding / Partitioning

Sharding splits data across multiple nodes so each node stores a subset. Combined with replication (each shard is replicated), this provides both scalability and availability.

```
Sharding + Replication
═══════════════════════

  Data: keys A-Z

  Shard 1: A-H        Shard 2: I-P        Shard 3: Q-Z
  ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
  │  Leader      │     │  Leader      │     │  Leader      │
  │  Node 1      │     │  Node 4      │     │  Node 7      │
  ├─────────────┤     ├─────────────┤     ├─────────────┤
  │  Follower    │     │  Follower    │     │  Follower    │
  │  Node 2      │     │  Node 5      │     │  Node 8      │
  ├─────────────┤     ├─────────────┤     ├─────────────┤
  │  Follower    │     │  Follower    │     │  Follower    │
  │  Node 3      │     │  Node 6      │     │  Node 9      │
  └─────────────┘     └─────────────┘     └─────────────┘

  9 nodes total. Each shard has 3 replicas (Raft group).
  Tolerates 1 node failure per shard without data loss.

  CockroachDB, TiDB, YugabyteDB all use this architecture:
  each shard (called "range" or "region") is a Raft group.
```

### 4.1 Range-Based Partitioning

```
Range-Based Partitioning
═════════════════════════

  Keys are sorted. Contiguous ranges assigned to shards.

  Key space: [0, 1000)

  Shard 1: [0, 250)      │████████░░░░░░░░░░░░░░░░░░░░░░│
  Shard 2: [250, 500)     │░░░░░░░░████████░░░░░░░░░░░░░░│
  Shard 3: [500, 750)     │░░░░░░░░░░░░░░░░████████░░░░░░│
  Shard 4: [750, 1000)    │░░░░░░░░░░░░░░░░░░░░░░░░██████│

  Advantages:
    + Range scans are efficient (adjacent keys on same shard)
    + ORDER BY queries can be pushed down to individual shards
    + Easy to understand and debug

  Disadvantages:
    - HOT SPOTS: sequential keys (auto-increment IDs, timestamps)
      all land on the same shard
    - Manual rebalancing may be needed

  Hot Spot Example:
    Shard 4 handles all writes for keys 750-1000.
    If new users get IDs 900, 901, 902, ... all writes hit Shard 4.

    ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐
    │ S1   │  │ S2   │  │ S3   │  │ S4   │
    │ 10%  │  │ 10%  │  │ 10%  │  │ 70%  │  ← hot spot!
    │ load │  │ load │  │ load │  │ load │
    └──────┘  └──────┘  └──────┘  └──────┘

  Auto-splitting (used by CockroachDB, TiKV, HBase):
    When a shard exceeds a size threshold (e.g., 512MB in CockroachDB):
    1. Find the median key
    2. Split into two shards at the median
    3. Each half becomes its own Raft group
    4. Rebalance shards across nodes to even out load
```

### 4.2 Hash-Based Partitioning

```
Hash-Based Partitioning
════════════════════════

  Apply a hash function to the key; assign to shard based on hash.

  shard = hash(key) % num_shards

  Advantage: uniform distribution regardless of key pattern
  Disadvantage: range queries require scatter-gather to ALL shards

  Consistent Hashing (Karger et al., 1997):
  ══════════════════════════════════════════

  Avoids reshuffling all keys when nodes are added/removed.

  Hash ring (0 to 2^32):

                    0 / 2^32
                      │
              N1 ─────┼───── N2
             ╱                  ╲
            ╱     keys here      ╲
           ╱      belong to       ╲
          ╱       next node        ╲
   N4 ───┤       clockwise         ├─── N3 (new)
          ╲                        ╱
           ╲                      ╱
            ╲                    ╱
             ╲                  ╱
              N5 ──────────── N6

  Each node owns the range from its position to the next node clockwise.

  Adding N3: only keys between N2 and N3 move (from N4 to N3).
  Removing N6: only N6's keys move to N1 (next clockwise).

  Problem: uneven distribution with few nodes (nodes may be clustered).

  Solution: VIRTUAL NODES (vnodes)

  Each physical node gets V virtual positions on the ring:

                    0 / 2^32
                      │
            N1.a ─────┼───── N2.b
             ╱                  ╲
            ╱  N3.c              ╲
           ╱                      ╲
          ╱     N1.b               ╲
   N2.a ─┤                         ├─── N3.a
          ╲          N2.c          ╱
           ╲                      ╱
            ╲    N1.c            ╱
             ╲                  ╱
              N3.b ────────── N2.d

  With V=100-256 vnodes per physical node, load is nearly uniform.
  Used by Cassandra (default 256 vnodes per node).

  Trade-offs:
    More vnodes → better balance, but more metadata and repair overhead.
    Cassandra reduced default from 256 to 16 in newer versions for
    faster streaming during node replacement.
```

### 4.3 Hybrid: Compound Partition Keys (Cassandra)

```
Cassandra Compound Keys
════════════════════════

  PRIMARY KEY ((partition_key), clustering_col1, clustering_col2)

  Partition key: hashed → determines which node stores the data
  Clustering columns: sorted within a partition → enables range scans

  Example: Time-series data

  CREATE TABLE sensor_readings (
      sensor_id    UUID,
      reading_date DATE,
      reading_time TIMESTAMP,
      value        DOUBLE,
      PRIMARY KEY ((sensor_id, reading_date), reading_time)
  );

  ┌──────────────────────────┐     ┌──────────────────────────┐
  │ Partition:               │     │ Partition:               │
  │ (sensor_1, 2024-01-15)  │     │ (sensor_1, 2024-01-16)  │
  │                          │     │                          │
  │ reading_time  │ value    │     │ reading_time  │ value    │
  │ 08:00:00      │ 23.5     │     │ 08:00:00      │ 24.1     │
  │ 08:01:00      │ 23.6     │     │ 08:01:00      │ 24.0     │
  │ 08:02:00      │ 23.7     │     │ ...           │ ...      │
  │ ...           │ ...      │     │                          │
  └──────────────────────────┘     └──────────────────────────┘
       Node A (hash)                      Node C (hash)

  Efficient query: SELECT * FROM sensor_readings
                   WHERE sensor_id = ? AND reading_date = ?
                   AND reading_time >= '08:00' AND reading_time < '09:00';

  → Hits exactly ONE partition, does a range scan within it. O(log N).

  Inefficient query: SELECT * FROM sensor_readings
                     WHERE reading_time >= '08:00';

  → Full table scan across ALL partitions. Cassandra will reject this
    without ALLOW FILTERING.
```

### 4.4 Rebalancing Strategies

```
Rebalancing: Moving Data When Nodes Change
════════════════════════════════════════════

  1. FIXED NUMBER OF PARTITIONS
     ──────────────────────────
     Create many more partitions than nodes (e.g., 1000 partitions, 10 nodes).
     Each node owns ~100 partitions.
     Add a node: steal partitions from other nodes.

     Before (10 nodes):
       Node 1: P1-P100, Node 2: P101-P200, ..., Node 10: P901-P1000

     After adding Node 11:
       Steal ~91 partitions evenly from existing nodes.
       Node 1: P1-P91, Node 11: P92-P100, P192-P200, ...

     Used by: Riak, Elasticsearch, Couchbase, Voldemort

     Trade-off: partition count fixed at creation time.
       Too few: hot spots. Too many: overhead per partition.

  2. DYNAMIC PARTITIONING (Auto-Split / Merge)
     ──────────────────────────────────────────
     Partition splits when it exceeds a size threshold.
     Partition merges when it shrinks below a threshold.

     Used by: HBase, CockroachDB, TiKV, MongoDB

     CockroachDB:
       Default range size: 512 MB
       Split threshold: 512 MB
       Merge threshold: ~half of split threshold
       Rebalance: lease holder moves ranges to even out node loads

  3. PROPORTIONAL TO NODE COUNT
     ──────────────────────────
     Fixed number of partitions per node.
     Adding a node: new node splits existing partitions.

     Used by: Cassandra (with vnodes)

     When node joins:
       New node picks random positions on ring
       Existing owners of those ranges stream data to new node
```

---

## 5. Distributed Transactions

Transactions spanning multiple shards or nodes require coordination protocols. The fundamental challenge: atomic commit across unreliable participants.

### 5.1 Two-Phase Commit (2PC)

```
Two-Phase Commit (2PC) Protocol
════════════════════════════════

  Coordinator (transaction manager)
  Participants (resource managers -- typically shard leaders)

  Phase 1: PREPARE (voting phase)
  ────────────────────────────────

  Coordinator                    Participants
  ┌──────────┐  PREPARE ──────► ┌──────────┐
  │          │  ─────────────►  │ Part. A  │ → acquire locks, write
  │ Coord.   │  ─────────────►  │ Part. B  │   to WAL, but do NOT commit
  │          │                   │ Part. C  │
  │          │ ◄── VOTE YES ──  │          │ → "I can commit"
  │          │ ◄── VOTE YES ──  │          │   or
  │          │ ◄── VOTE NO ───  │          │ → "I must abort"
  └──────────┘                   └──────────┘

  Phase 2: COMMIT / ABORT (decision phase)
  ──────────────────────────────────────────

  If ALL votes = YES:
  ┌──────────┐  COMMIT ──────► ┌──────────┐
  │ Coord.   │  ─────────────►  │ Part. A  │ → commit and release locks
  │          │  ─────────────►  │ Part. B  │
  │          │                   │ Part. C  │
  │          │ ◄── ACK ────────  │          │
  └──────────┘                   └──────────┘

  If ANY vote = NO:
  ┌──────────┐  ABORT ───────► ┌──────────┐
  │ Coord.   │  ─────────────►  │ Part. A  │ → rollback and release locks
  │          │  ─────────────►  │ Part. B  │
  │          │                   │ Part. C  │
  │          │ ◄── ACK ────────  │          │
  └──────────┘                   └──────────┘

  Timeline:
  ─────────────────────────────────────────────────────────────────►
  │ Begin │ PREPARE │ wait... │ votes │ write │ COMMIT │ ACKs │ Done
  │ txn   │ sent    │         │ rcvd  │ commit│ sent   │ rcvd │
  │       │         │         │       │ record│        │      │
```

#### The Blocking Problem

```
2PC Blocking Problem
═════════════════════

  The critical weakness of 2PC:

  If the coordinator crashes AFTER sending PREPARE but BEFORE sending COMMIT/ABORT:

  ┌──────────┐  PREPARE ──────► ┌──────────┐
  │ Coord.   │  ─────────────►  │ Part. A  │ voted YES
  │ (crashes) │                  │ Part. B  │ voted YES
  │    💥     │                  │ Part. C  │ voted YES
  └──────────┘                   └──────────┘

  Participants are STUCK:
    - They voted YES → they PROMISED to commit if told to
    - They cannot unilaterally abort (coordinator might have decided COMMIT)
    - They cannot unilaterally commit (coordinator might have decided ABORT)
    - They MUST HOLD LOCKS and wait for coordinator to recover

  This is the "window of vulnerability" -- locks are held indefinitely.

  In practice:
    - Coordinator writes commit decision to durable log BEFORE Phase 2
    - If coordinator crashes, it recovers and resends the decision
    - Timeout: participants can ask other participants if anyone knows the decision
    - But if ALL participants only have YES votes and coordinator is down:
      TRULY STUCK until coordinator recovers

  Impact on availability:
    - 2PC is NOT partition-tolerant
    - Network partition between coordinator and participant = blocked transaction
    - This is why pure 2PC is avoided in highly-available systems
```

### 5.2 Three-Phase Commit (3PC)

```
Three-Phase Commit (3PC)
═════════════════════════

  Adds a PRE-COMMIT phase to avoid blocking:

  Phase 1: CAN-COMMIT (same as 2PC Prepare)
    Coordinator → Participants: "Can you commit?"
    Participants → Coordinator: "Yes" / "No"

  Phase 2: PRE-COMMIT (new phase)
    If all YES:
      Coordinator → Participants: "Pre-commit"
      Participants: acquire locks, prepare, but don't commit yet
      Participants → Coordinator: "ACK"

    Key difference: if coordinator fails HERE, participants know
    they all voted YES (because they received pre-commit).
    They can elect a new coordinator and proceed to commit.

  Phase 3: DO-COMMIT
    Coordinator → Participants: "Commit"
    Participants: commit and release locks.

  Why 3PC is rarely used:
    - Does NOT work with network partitions (only crash failures)
    - Partition can cause split: some nodes pre-committed, others didn't
    - More round trips = higher latency
    - In practice, Paxos-based commit (e.g., Spanner) is preferred
```

### 5.3 Saga Pattern

```
Saga Pattern: Long-Lived Distributed Transactions
═══════════════════════════════════════════════════

  Instead of a single atomic transaction across services, execute a
  sequence of local transactions. Each step has a compensating action.

  T1 → T2 → T3 → ... → Tn        (forward execution)
  C1 ← C2 ← C3 ← ... ← Cn        (compensation on failure)

  Example: E-Commerce Order

  Step 1: Create Order      (compensate: Cancel Order)
  Step 2: Reserve Inventory (compensate: Release Inventory)
  Step 3: Charge Payment    (compensate: Refund Payment)
  Step 4: Ship Order        (compensate: Cancel Shipment)

  If Step 3 fails:
    Execute C2: Release Inventory
    Execute C1: Cancel Order
    (Step 3 itself failed, so no C3 needed)
```

#### Choreography vs Orchestration

```
Saga Choreography (Event-Driven)
═════════════════════════════════

  Each service listens for events and acts autonomously.
  No central coordinator.

  Order         Inventory        Payment         Shipping
  Service       Service          Service         Service
    │               │                │               │
    │ OrderCreated  │                │               │
    ├──────────────►│                │               │
    │               │ InventoryReserved              │
    │               ├───────────────►│               │
    │               │                │ PaymentCharged│
    │               │                ├──────────────►│
    │               │                │               │ OrderShipped
    │◄──────────────┴────────────────┴───────────────┤
    │                                                │
    │ If payment fails:                              │
    │               │ PaymentFailed                   │
    │               │◄───────────────┤               │
    │               │ InventoryReleased               │
    │◄──────────────┤                                │
    │ OrderCancelled│                                │

  + Loose coupling; services are independent
  + No single point of failure
  - Hard to track overall saga state (distributed debugging)
  - Cyclic dependencies between services
  - Difficult to add new steps

Saga Orchestration (Central Coordinator)
═════════════════════════════════════════

  An orchestrator directs the saga steps.

  ┌──────────────┐
  │ Saga         │
  │ Orchestrator │
  └──────┬───────┘
         │
         ├── 1. CreateOrder ──────────► Order Service
         │                              │
         │◄── OrderCreated ────────────┘
         │
         ├── 2. ReserveInventory ─────► Inventory Service
         │                              │
         │◄── InventoryReserved ───────┘
         │
         ├── 3. ChargePayment ────────► Payment Service
         │                              │
         │◄── PaymentCharged ──────────┘
         │
         ├── 4. ShipOrder ────────────► Shipping Service
         │                              │
         │◄── OrderShipped ────────────┘
         │
         └── SAGA COMPLETE

  On failure at step 3:
         │
         │◄── PaymentFailed ───────────┘
         │
         ├── Compensate: ReleaseInventory ► Inventory Service
         │◄── InventoryReleased ───────────┘
         │
         ├── Compensate: CancelOrder ──────► Order Service
         │◄── OrderCancelled ──────────────┘
         │
         └── SAGA ROLLED BACK

  + Clear flow, easy to understand and debug
  + Orchestrator holds saga state (can be persisted/recovered)
  + Easy to add new steps
  - Orchestrator is a potential SPOF (mitigate with multiple instances)
  - Tighter coupling (orchestrator knows all participants)
```

**Comparison:**

| Aspect | Choreography | Orchestration |
|---|---|---|
| Coupling | Loose (event-driven) | Medium (orchestrator knows all) |
| Visibility | Hard to trace | Easy (centralized state) |
| Adding steps | Difficult (implicit flow) | Easy (modify orchestrator) |
| Single point of failure | None | Orchestrator (can be HA) |
| Complexity at scale | Grows fast (event mesh) | Manageable (explicit flow) |
| Best for | Simple sagas, 2-3 steps | Complex sagas, many steps |

### 5.4 Percolator (Google / TiDB)

```
Percolator: Snapshot Isolation Over Distributed Storage
════════════════════════════════════════════════════════

  Original Google paper (2010): built on Bigtable + Chubby.
  TiDB's transaction model is directly based on Percolator.

  Key idea: use a TIMESTAMP ORACLE (TSO) for globally ordered timestamps.
  Store data and locks in the same distributed KV store.

  Columns per key in the underlying KV store:
    data:    versioned values   (key, start_ts) → value
    lock:    lock record        (key) → {primary_key, start_ts, type}
    write:   commit record      (key, commit_ts) → start_ts

  Two-Phase Commit (optimistic):
  ──────────────────────────────

  Transaction: write key A=1, key B=2

  Phase 1: PREWRITE
    1. Get start_ts from TSO (e.g., start_ts = 100)
    2. Choose a primary key (e.g., A)
    3. For each key:
       a. Check for write-write conflicts (any committed write with ts > start_ts)
       b. Check for existing locks (another uncommitted txn)
       c. Write lock record: lock(A) = {primary=A, start_ts=100}
       d. Write data: data(A, 100) = 1
    4. Repeat for secondary keys (B)

  Phase 2: COMMIT
    1. Get commit_ts from TSO (e.g., commit_ts = 105)
    2. Commit PRIMARY key first:
       a. Check primary lock still exists (not rolled back)
       b. Write write record: write(A, 105) = 100 (points to data)
       c. Delete lock: lock(A) = null
    3. Commit secondary keys asynchronously:
       a. Write write record: write(B, 105) = 100
       b. Delete lock: lock(B) = null

  Read at timestamp T:
    1. Check lock column: if locked with start_ts < T, wait or clean up
    2. Find latest write record with commit_ts <= T
    3. Follow pointer to data column

  Crash recovery:
    - If transaction crashed after prewrite but before commit:
      Other transactions encountering the lock check the PRIMARY lock.
      If primary lock is gone → transaction was committed → resolve secondary.
      If primary lock exists and is old → transaction is abandoned → clean up.

  TiDB Implementation:
  ┌─────────────┐     ┌──────────────┐     ┌──────────────────┐
  │  TiDB       │────►│ Placement    │     │  TiKV Nodes      │
  │  (SQL layer)│     │ Driver (PD)  │     │  (storage layer) │
  │             │     │              │     │                  │
  │  Parses SQL │     │ TSO          │     │  Raft groups     │
  │  Plans query│     │ Scheduling   │     │  RocksDB per node│
  │  Coordinates│     │ Region mgmt  │     │  Percolator txns │
  │  2PC        │     │              │     │                  │
  └─────────────┘     └──────────────┘     └──────────────────┘
```

### 5.5 Google Spanner and TrueTime

```
Spanner TrueTime: Globally Consistent Timestamps
══════════════════════════════════════════════════

  Problem: in a geo-distributed system, clocks are not synchronized.
  NTP accuracy: ~1-10ms. This means you cannot use wall-clock timestamps
  for transaction ordering -- two events 5ms apart might be misordered.

  Spanner's solution: TrueTime API

  TT.now() returns an INTERVAL: [earliest, latest]
  The true time is guaranteed to be within this interval.

  ┌──────────────────────────────────────────────────────────────┐
  │  TrueTime API                                                │
  │                                                              │
  │  TT.now()    → TTinterval {earliest, latest}                │
  │  TT.after(t) → true if t has definitely passed              │
  │  TT.before(t)→ true if t has definitely not arrived         │
  │                                                              │
  │  Uncertainty: ε = (latest - earliest) / 2                   │
  │  Typically ε < 7ms (GPS + atomic clocks in each datacenter) │
  └──────────────────────────────────────────────────────────────┘

  How Spanner uses TrueTime for external consistency:

  Commit protocol:
    1. Transaction acquires locks, runs 2PC across Paxos groups
    2. Leader assigns commit timestamp s = TT.now().latest
    3. Leader WAITS until TT.after(s) is true (the "commit wait")
       This wait is at most 2ε (~14ms with GPS clocks)
    4. Release locks and return to client

  Why commit-wait works:
    If T1 commits at s1 and T2 starts after T1 returns:
      T2's start time > T1's commit time (real time)
      T1 waited until s1 definitely passed
      T2 gets start_ts > s1
      → T2 sees T1's writes (external consistency / linearizability)

  Clock infrastructure:
  ┌──────────────────────────────────────────────────────────────┐
  │  Each datacenter:                                            │
  │    - Multiple GPS receivers (antenna on roof)                │
  │    - Multiple atomic clocks (cesium/rubidium)                │
  │    - Time masters serve TrueTime to local servers            │
  │    - Servers poll multiple time masters, compute interval    │
  │    - ε is continuously measured and reported                 │
  │                                                              │
  │  If ε exceeds threshold → Spanner slows down (wider wait)   │
  │  In practice: ε averages 4ms, rarely exceeds 7ms            │
  └──────────────────────────────────────────────────────────────┘

  CockroachDB's alternative: Hybrid Logical Clocks (HLC)
    - Combines physical timestamp + logical counter
    - Does NOT need special hardware
    - Uses a "max clock offset" parameter (default 500ms)
    - If clocks drift beyond this → node is removed from cluster
    - Weaker guarantee: linearizability requires client-observed ordering
      (CockroachDB calls this "single-key linearizability")
```

---

## 6. Distributed Query Processing

When data is spread across shards, query execution must be coordinated across multiple nodes. This section covers the key patterns.

### 6.1 Distributed Joins

```
Distributed Join Strategies
════════════════════════════

  Query: SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id
         WHERE c.country = 'US';

  Orders: sharded by order_id (hash)
  Customers: sharded by customer_id (hash)

  Strategy 1: BROADCAST JOIN (small table)
  ─────────────────────────────────────────

  If customers table is small (~MBs):
    1. Send full customers table to every orders shard
    2. Each shard joins locally
    3. Coordinator collects results

    ┌──────────┐  broadcast   ┌──────────┐
    │Customers │─────────────►│Orders S1 │ → local join → partial result
    │(filtered)│─────────────►│Orders S2 │ → local join → partial result
    │          │─────────────►│Orders S3 │ → local join → partial result
    └──────────┘              └──────────┘
                                           → coordinator merges results

  Strategy 2: HASH REPARTITION JOIN (both tables large)
  ─────────────────────────────────────────────────────

  Both tables are large. Re-shard both by the JOIN key.
    1. Reshuffle orders by customer_id (matching customers' sharding)
    2. Now co-located on the same node → local join

    Orders (reshuffled by customer_id):
    Shard 1: customers 1-100 + their orders  → local join
    Shard 2: customers 101-200 + their orders → local join
    Shard 3: customers 201-300 + their orders → local join

    Cost: O(|orders| + |customers|) network transfer

  Strategy 3: COLOCATED JOIN (no shuffle needed)
  ──────────────────────────────────────────────

  If both tables are sharded by the SAME key (customer_id):
    Orders and customers for the same customer_id are on the same shard.
    Join executes entirely locally on each shard. Zero network transfer.

    This is why Vitess and CockroachDB encourage sharding related tables
    by the same key (tenant_id, customer_id, etc.).
```

### 6.2 Predicate Pushdown

```
Predicate Pushdown
═══════════════════

  Push WHERE clauses to the storage layer to reduce data transferred.

  Query: SELECT name, email FROM users WHERE country = 'DE' AND age > 30;

  Without pushdown:
    Coordinator asks each shard for ALL users
    → millions of rows transferred over network
    → coordinator filters locally (waste)

  With pushdown:
    Coordinator sends (country='DE' AND age>30) to each shard
    → each shard filters locally, returns only matching rows
    → only thousands of rows transferred

  ┌──────────┐                     ┌──────────┐
  │  Query   │  push filter        │  Shard 1 │
  │  Coord.  │────────────────────►│  Filter:  │──► 50 rows
  │          │  (country='DE'      │  country= │
  │          │   AND age>30)       │  'DE' AND │
  │          │                     │  age>30   │
  │          │────────────────────►│──────────│
  │          │                     │  Shard 2 │──► 80 rows
  │          │────────────────────►│──────────│
  │          │                     │  Shard 3 │──► 30 rows
  │          │◄─────────────────── └──────────┘
  │  Merge   │  160 rows total
  │  result  │  (instead of millions)
  └──────────┘

  TiDB: pushes filters, aggregations, TopN to TiKV coprocessor.
  CockroachDB: DistSQL engine pushes computation to storage nodes.
  Vitess: VTGate rewrites queries and routes to correct VTTablet.
```

### 6.3 Scatter-Gather Pattern

```
Scatter-Gather
═══════════════

  Generic pattern for queries that touch multiple shards.

  1. SCATTER: coordinator sends sub-query to each relevant shard
  2. GATHER: coordinator collects partial results from all shards
  3. MERGE: coordinator combines partial results into final result

  Example: SELECT COUNT(*), AVG(price) FROM products WHERE category = 'books';

  ┌──────────┐
  │ Coord.   │
  │          │──scatter──► Shard 1: count=1000, sum=25000 ──┐
  │          │──scatter──► Shard 2: count=800, sum=18000  ──┤──gather──► Coord.
  │          │──scatter──► Shard 3: count=1200, sum=30000 ──┘
  │          │
  │  MERGE:  │
  │  total_count = 3000                                     │
  │  total_sum = 73000                                      │
  │  avg_price = 73000/3000 = 24.33                         │
  └──────────┘

  Important: cannot average the averages! Must sum counts and sums separately.

  Operations that compose across shards:
    COUNT, SUM, MIN, MAX: directly mergeable
    AVG: compute from SUM / COUNT
    MEDIAN, PERCENTILE: requires ALL values or approximate (t-digest, HLL)
    DISTINCT: union of per-shard distinct sets
    ORDER BY + LIMIT: each shard returns top-K, coordinator merge-sorts
```

### 6.4 Distributed Sort

```
Distributed Sort (ORDER BY across shards)
═══════════════════════════════════════════

  Query: SELECT * FROM logs ORDER BY timestamp DESC LIMIT 100;

  Naive: each shard sends ALL rows → coordinator sorts → take top 100
         Problem: may transfer billions of rows over network

  Optimized: push ORDER BY + LIMIT to each shard

  Shard 1: SELECT * FROM logs ORDER BY timestamp DESC LIMIT 100 → 100 rows
  Shard 2: SELECT * FROM logs ORDER BY timestamp DESC LIMIT 100 → 100 rows
  Shard 3: SELECT * FROM logs ORDER BY timestamp DESC LIMIT 100 → 100 rows

  Coordinator: merge-sort 300 rows → take top 100

  For ORDER BY + OFFSET + LIMIT (pagination):
    SELECT * FROM logs ORDER BY timestamp DESC LIMIT 10 OFFSET 990;

  Each shard must return top 1000 rows (OFFSET+LIMIT).
  Coordinator merge-sorts 3000 rows, skips 990, returns 10.
  → Deep pagination is expensive in distributed systems.
  → Prefer cursor-based pagination: WHERE timestamp < :last_seen LIMIT 10
```

---

## 7. Real-World Systems

### 7.1 CockroachDB

```
CockroachDB Architecture
══════════════════════════

  ┌───────────────────────────────────────────────────────────────────┐
  │                        SQL Layer                                   │
  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐           │
  │  │   Gateway     │  │   Gateway     │  │   Gateway     │          │
  │  │   Node 1      │  │   Node 2      │  │   Node 3      │          │
  │  │              │  │              │  │              │           │
  │  │  SQL Parser  │  │  SQL Parser  │  │  SQL Parser  │           │
  │  │  Optimizer   │  │  Optimizer   │  │  Optimizer   │           │
  │  │  DistSQL     │  │  DistSQL     │  │  DistSQL     │           │
  │  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘           │
  │         │                  │                  │                    │
  ├─────────┼──────────────────┼──────────────────┼────────────────────┤
  │         │        Transaction Layer (KV)       │                    │
  │         │                  │                  │                    │
  │  ┌──────▼───────┐  ┌──────▼───────┐  ┌──────▼───────┐           │
  │  │  Range 1     │  │  Range 2     │  │  Range 3     │           │
  │  │  [a-f)       │  │  [f-p)       │  │  [p-z)       │           │
  │  │              │  │              │  │              │           │
  │  │  Raft Group  │  │  Raft Group  │  │  Raft Group  │           │
  │  │  Leader: N1  │  │  Leader: N2  │  │  Leader: N3  │           │
  │  │  Follow: N2,3│  │  Follow: N1,3│  │  Follow: N1,2│           │
  │  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘           │
  │         │                  │                  │                    │
  ├─────────┼──────────────────┼──────────────────┼────────────────────┤
  │         │          Storage Layer              │                    │
  │  ┌──────▼───────┐  ┌──────▼───────┐  ┌──────▼───────┐           │
  │  │   Pebble     │  │   Pebble     │  │   Pebble     │           │
  │  │  (LSM-Tree)  │  │  (LSM-Tree)  │  │  (LSM-Tree)  │           │
  │  │   Node 1     │  │   Node 2     │  │   Node 3     │           │
  │  └──────────────┘  └──────────────┘  └──────────────┘           │
  └───────────────────────────────────────────────────────────────────┘

  Key design choices:
  ─ Ranges: ~512MB each, auto-split/merge
  ─ Each range = Raft consensus group (3 or 5 replicas)
  ─ Any node can be gateway (accepts SQL) or leaseholder (serves reads)
  ─ Leaseholder: one replica per range handles reads without Raft (fast)
  ─ Writes go through Raft (majority acknowledgment)
  ─ Transactions: serializable isolation by default (MVCC + write intents)
  ─ Storage engine: Pebble (Go port of RocksDB/LevelDB)
  ─ Clock: Hybrid Logical Clock (HLC), max offset configurable

  Geo-partitioning (Enterprise):
  ─ Pin ranges to specific regions (e.g., EU data stays in EU)
  ─ Leaseholder preferences (reads served locally)
  ─ Follower reads (stale but fast, for analytics)
```

### 7.2 TiDB / TiKV

```
TiDB Architecture
══════════════════

  ┌──────────────────────────────────────────────────────────────────┐
  │                        TiDB Layer (SQL)                          │
  │  ┌────────────┐  ┌────────────┐  ┌────────────┐                │
  │  │  TiDB      │  │  TiDB      │  │  TiDB      │                │
  │  │  Server 1  │  │  Server 2  │  │  Server 3  │                │
  │  │            │  │            │  │            │                │
  │  │  Parser    │  │  Parser    │  │  Parser    │                │
  │  │  Optimizer │  │  Optimizer │  │  Optimizer │                │
  │  │  Executor  │  │  Executor  │  │  Executor  │                │
  │  │  Txn Coord │  │  Txn Coord │  │  Txn Coord │                │
  │  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘                │
  │        │                │                │                       │
  ├────────┼────────────────┼────────────────┼───────────────────────┤
  │        │        PD (Placement Driver)    │                       │
  │        │        ┌────────────────┐       │                       │
  │        │        │  TSO (Timestamp│       │                       │
  │        │        │   Oracle)      │       │                       │
  │        │        │  Region mgmt   │       │                       │
  │        │        │  Load balance  │       │                       │
  │        │        │  (Raft-based   │       │                       │
  │        │        │   HA cluster)  │       │                       │
  │        │        └────────────────┘       │                       │
  │        │                │                │                       │
  ├────────┼────────────────┼────────────────┼───────────────────────┤
  │        │         TiKV Layer (KV)         │                       │
  │  ┌─────▼──────┐  ┌─────▼──────┐  ┌─────▼──────┐               │
  │  │  TiKV      │  │  TiKV      │  │  TiKV      │               │
  │  │  Node 1    │  │  Node 2    │  │  Node 3    │               │
  │  │            │  │            │  │            │               │
  │  │  Regions   │  │  Regions   │  │  Regions   │               │
  │  │  (Raft)    │  │  (Raft)    │  │  (Raft)    │               │
  │  │  RocksDB   │  │  RocksDB   │  │  RocksDB   │               │
  │  └────────────┘  └────────────┘  └────────────┘               │
  │                                                                 │
  │  Optional: TiFlash (columnar analytics replica)                 │
  │  ┌────────────┐  ┌────────────┐                                │
  │  │  TiFlash 1 │  │  TiFlash 2 │  (Raft learner replicas)      │
  │  │  Column     │  │  Column     │  (real-time sync from TiKV)   │
  │  │  Store     │  │  Store     │                                │
  │  └────────────┘  └────────────┘                                │
  └──────────────────────────────────────────────────────────────────┘

  Key design choices:
  ─ TiDB servers are stateless (can scale horizontally)
  ─ TiKV regions: ~96MB each (smaller than CockroachDB ranges)
  ─ Transaction model: Percolator-style (optimistic by default)
  ─ TSO: single PD leader issues timestamps (potential bottleneck at extreme scale)
  ─ HTAP: TiFlash receives Raft log and stores in columnar format
  ─ Storage: RocksDB (two instances per TiKV: one for data, one for Raft log)
  ─ MySQL protocol compatible (drop-in replacement for reads)
```

### 7.3 Google Spanner

```
Google Spanner Architecture (Simplified)
══════════════════════════════════════════

  Global:
  ┌──────────────────────────────────────────────────────────────────┐
  │                     Universe Master                               │
  │            (monitoring, interactive debugging)                    │
  └──────────────────────────────────────────────────────────────────┘

  Per-zone:
  ┌──────────────────────────────────────────────────────────────────┐
  │  Zone 1 (US-East)        Zone 2 (EU-West)      Zone 3 (AP-SE)  │
  │  ┌──────────────┐       ┌──────────────┐       ┌──────────────┐│
  │  │ Span servers │       │ Span servers │       │ Span servers ││
  │  │ (100s-1000s) │       │ (100s-1000s) │       │ (100s-1000s) ││
  │  │              │       │              │       │              ││
  │  │ Each manages │       │ Each manages │       │ Each manages ││
  │  │ tablets (shards)     │ tablets      │       │ tablets      ││
  │  │              │       │              │       │              ││
  │  │ Paxos groups │       │ Paxos groups │       │ Paxos groups ││
  │  │ span zones   │       │ span zones   │       │ span zones   ││
  │  └──────────────┘       └──────────────┘       └──────────────┘│
  │                                                                 │
  │  Zone Master  Loc. Proxy  Zone Master  Loc. Proxy  Zone Master │
  │  (assign data) (route)   (assign data) (route)   (assign data)│
  └──────────────────────────────────────────────────────────────────┘

  TrueTime Servers (per datacenter):
  ┌──────────────────────────────────────────────────────────────────┐
  │  GPS Receiver ──► Time Master ──► Armageddon Master (atomic     │
  │  GPS Receiver ──► Time Master     clock for when GPS fails)     │
  │  Atomic Clock ──► Time Master                                    │
  │                                                                  │
  │  Each Span server polls multiple time masters                    │
  │  Computed ε typically < 7ms                                      │
  └──────────────────────────────────────────────────────────────────┘

  Key design choices:
  ─ Paxos for replication (not Raft -- predates Raft)
  ─ TrueTime for globally consistent timestamps
  ─ External consistency (linearizability) for all transactions
  ─ Schema with interleaved tables (parent-child co-location)
  ─ Read-only transactions: lock-free at a snapshot timestamp
  ─ Read-write transactions: pessimistic 2PL + 2PC across Paxos groups
  ─ Commit wait: ~7ms average extra latency per write transaction
```

### 7.4 Vitess

```
Vitess Architecture (MySQL Sharding Layer)
═══════════════════════════════════════════

  ┌─────────────────────────────────────────────────────────────────┐
  │                     Application Layer                            │
  │  ┌──────────┐  ┌──────────┐  ┌──────────┐                     │
  │  │  App 1   │  │  App 2   │  │  App 3   │                     │
  │  └────┬─────┘  └────┬─────┘  └────┬─────┘                     │
  │       │              │              │                            │
  │       └──────────────┼──────────────┘                            │
  │                      │                                           │
  │                ┌─────▼──────┐                                   │
  │                │  VTGate    │  (proxy / query router)           │
  │                │            │  - Parses SQL                     │
  │                │            │  - Routes to correct shard        │
  │                │            │  - Scatter-gather for multi-shard │
  │                │            │  - Connection pooling             │
  │                └─────┬──────┘                                   │
  │                      │                                           │
  │       ┌──────────────┼──────────────┐                           │
  │       │              │              │                            │
  │  ┌────▼─────┐  ┌────▼─────┐  ┌────▼─────┐                    │
  │  │ VTTablet │  │ VTTablet │  │ VTTablet │                     │
  │  │ Shard -80│  │ Shard 80-│  │ Shard    │                     │
  │  │          │  │          │  │ (lookup) │                     │
  │  │ ┌──────┐ │  │ ┌──────┐ │  │ ┌──────┐ │                     │
  │  │ │MySQL │ │  │ │MySQL │ │  │ │MySQL │ │                     │
  │  │ │Primary│ │  │ │Primary│ │  │ │Primary│ │                     │
  │  │ └──────┘ │  │ └──────┘ │  │ └──────┘ │                     │
  │  │ ┌──────┐ │  │ ┌──────┐ │  │ ┌──────┐ │                     │
  │  │ │MySQL │ │  │ │MySQL │ │  │ │MySQL │ │                     │
  │  │ │Replica│ │  │ │Replica│ │  │ │Replica│ │                     │
  │  │ └──────┘ │  │ └──────┘ │  │ └──────┘ │                     │
  │  └──────────┘  └──────────┘  └──────────┘                     │
  │                                                                 │
  │  Topology Service: etcd or ZooKeeper or Consul                  │
  │  (stores shard map, tablet locations, schema)                   │
  └─────────────────────────────────────────────────────────────────┘

  Key design choices:
  ─ Wraps existing MySQL instances (not a new storage engine)
  ─ Sharding is transparent to the application (VTGate handles routing)
  ─ VSchema defines sharding keys and vindexes (Vitess indexes)
  ─ Supports resharding online (split/merge shards without downtime)
  ─ Used at: YouTube, Slack, GitHub, Square, HubSpot
  ─ 2PC support: experimental (Vitess prefers single-shard transactions)
  ─ Limitation: cross-shard transactions are limited; JOINs across shards
    may not be supported or may be slow (scatter-gather)
```

### 7.5 Amazon Aurora

```
Amazon Aurora: "The Log Is the Database"
═════════════════════════════════════════

  Traditional MySQL replication:
    Primary ──► write pages ──► EBS ──► replicate ──► Replica EBS
    Each write: 5 network hops for 4 copies (in 2 AZs)
    Crash recovery: replay WAL to reconstruct pages

  Aurora insight: only ship the REDO LOG, not data pages.
  Storage layer reconstructs pages from the log on demand.

  ┌────────────────────────────────────────────────────────────────┐
  │                    Compute Layer                                │
  │                                                                │
  │  ┌───────────────┐     ┌───────────────┐                      │
  │  │  Primary       │     │  Read Replica  │ (up to 15)          │
  │  │  (Read/Write)  │     │  (Read Only)   │                     │
  │  │                │     │                │                      │
  │  │  MySQL/PG      │     │  MySQL/PG      │                     │
  │  │  compatible    │     │  compatible    │                      │
  │  │                │     │                │                      │
  │  │  Buffer Pool   │     │  Buffer Pool   │                     │
  │  └───────┬────────┘     └───────┬────────┘                     │
  │          │ (redo log only)      │ (reads from same storage)    │
  │          │                      │                               │
  ├──────────┼──────────────────────┼───────────────────────────────┤
  │          │       Storage Layer (distributed)                    │
  │          ▼                                                      │
  │  ┌───────────────────────────────────────────────────────────┐ │
  │  │  Log-Structured Distributed Storage                       │ │
  │  │                                                           │ │
  │  │  AZ 1          AZ 2          AZ 3                        │ │
  │  │  ┌─────┐ ┌─────┐  ┌─────┐ ┌─────┐  ┌─────┐ ┌─────┐   │ │
  │  │  │Seg 1│ │Seg 2│  │Seg 1│ │Seg 2│  │Seg 1│ │Seg 2│   │ │
  │  │  │copy1│ │copy1│  │copy2│ │copy2│  │copy3│ │copy3│   │ │
  │  │  └─────┘ └─────┘  └─────┘ └─────┘  └─────┘ └─────┘   │ │
  │  │                                                           │ │
  │  │  6 copies across 3 AZs per 10GB segment                 │ │
  │  │  Write quorum: 4 of 6     Read quorum: 3 of 6           │ │
  │  │  Survives: AZ failure + 1 node (AZ+1)                   │ │
  │  │                                                           │ │
  │  │  Storage nodes reconstruct pages from redo log on read   │ │
  │  │  Background: coalesce redo entries → materialized pages  │ │
  │  └───────────────────────────────────────────────────────────┘ │
  └────────────────────────────────────────────────────────────────┘

  Key innovations:
  ─ Network I/O reduced by 7/8: only redo log shipped (not pages)
  ─ Crash recovery: instant (no redo replay; storage layer is always current)
  ─ Replication lag: ~10-20ms (log shipping to read replicas)
  ─ Storage auto-scales: 10GB → 128TB, no pre-provisioning
  ─ Write throughput: 6× MySQL (fewer I/O amplifications)

  Write path:
    1. Primary writes redo log record to storage (4/6 quorum)
    2. Primary updates in-memory buffer pool
    3. Primary sends redo log to read replicas (async)
    4. Read replicas apply redo log to their buffer pools
    5. If replica needs a page not in buffer: request from storage
       (storage materializes page from redo log)

  Aurora vs traditional MySQL:
  ┌──────────────────┬────────────────────┬───────────────────┐
  │ Aspect           │ MySQL + EBS        │ Aurora             │
  ├──────────────────┼────────────────────┼───────────────────┤
  │ Replication unit │ Data pages         │ Redo log records  │
  │ Network writes   │ Pages + log        │ Log only (6× less)│
  │ Crash recovery   │ Replay WAL (min)   │ Instant            │
  │ Read replicas    │ Independent storage│ Shared storage     │
  │ Storage scaling  │ Pre-provision EBS  │ Auto-scale to 128T│
  │ Write latency    │ Sync to 2 AZs     │ 4/6 quorum         │
  │ Repl. lag        │ Seconds-minutes    │ ~10-20ms           │
  └──────────────────┴────────────────────┴───────────────────┘
```

### 7.6 YugabyteDB

```
YugabyteDB Architecture
════════════════════════

  ┌──────────────────────────────────────────────────────────────────┐
  │                     Query Layer                                   │
  │  ┌────────────────────────────┐  ┌────────────────────────────┐ │
  │  │  YSQL (PostgreSQL-compat)  │  │  YCQL (Cassandra-compat)  │ │
  │  │  Full SQL, joins, indexes  │  │  CQL API, flexible schema │ │
  │  └────────────┬───────────────┘  └──────────────┬─────────────┘ │
  │               │                                  │               │
  ├───────────────┼──────────────────────────────────┼───────────────┤
  │               │        DocDB (Document Store)    │               │
  │               ▼                                  ▼               │
  │  ┌──────────────────────────────────────────────────────────┐   │
  │  │  Tablet layer                                             │   │
  │  │                                                           │   │
  │  │  ┌──────────┐  ┌──────────┐  ┌──────────┐              │   │
  │  │  │ Tablet 1 │  │ Tablet 2 │  │ Tablet 3 │   ...        │   │
  │  │  │ Raft     │  │ Raft     │  │ Raft     │              │   │
  │  │  │ group    │  │ group    │  │ group    │              │   │
  │  │  └──────────┘  └──────────┘  └──────────┘              │   │
  │  │                                                           │   │
  │  │  Each tablet: Raft consensus + RocksDB storage            │   │
  │  └──────────────────────────────────────────────────────────┘   │
  │                                                                  │
  │  YB-Master (control plane):                                     │
  │  ┌──────────────────────────────────────────────────────────┐   │
  │  │  - Catalog management (tables, schemas, tablet locations) │   │
  │  │  - Tablet splitting / load balancing                      │   │
  │  │  - Raft-replicated for HA (3 or 5 masters)               │   │
  │  └──────────────────────────────────────────────────────────┘   │
  └──────────────────────────────────────────────────────────────────┘

  Key design choices:
  ─ Dual API: YSQL (PostgreSQL wire protocol) and YCQL (Cassandra CQL)
  ─ DocDB: document-oriented storage on top of RocksDB
  ─ Raft per tablet (similar to CockroachDB ranges)
  ─ Global transaction support via YSQL (serializable and snapshot)
  ─ Geo-distribution: tablespace-level placement policies
  ─ Tablet splitting: automatic based on size or load
  ─ Hybrid clock (HLC) like CockroachDB
```

---

## 8. Failure Handling

Distributed systems must handle partial failures gracefully. Unlike single-node systems where failure is total, distributed systems experience ambiguous failures.

### 8.1 Network Partitions

```
Types of Network Failures
══════════════════════════

  1. TOTAL PARTITION
     Node A can reach B but not C. C can reach B but not A.

     A ◄──► B ◄──► C
     A ═══╪════════ C   (A and C cannot communicate)

  2. ASYMMETRIC PARTITION
     A can send to B but B cannot send to A.

     A ───► B   (A's messages reach B)
     A ◄─╪─ B   (B's messages lost to A)

     Especially nasty: A thinks B is down; B thinks A is alive.

  3. PARTIAL PARTITION
     A can reach B and C. B can reach A but not C.

     A ◄──► B
     A ◄──► C
     B ═══╪═ C

  4. NETWORK DELAY (gray failure)
     All messages eventually arrive, but with variable delay.
     Heartbeat timeout fires → node declared dead → it's actually alive.
     Most common cause of false failovers in production.

  Detection is the hard part:
  ─ You cannot distinguish "node is slow" from "node is dead"
  ─ Phi-accrual failure detector: probability-based (Cassandra uses this)
  ─ Heartbeat timeout: fixed threshold (Raft, ZooKeeper)
  ─ SWIM protocol: gossip-based failure detection (Consul, Serf)
```

### 8.2 Node Failures

```
Node Failure Modes
═══════════════════

  1. CRASH-STOP
     Node stops and never recovers. (Disk failure, kernel panic, fire)
     Handled by: replication to other nodes.

  2. CRASH-RECOVERY
     Node stops, then restarts with persistent state intact.
     Handled by: WAL replay + catch up from leader.
     Most common failure mode.

  3. OMISSION (messages lost)
     Node is running but drops some messages.
     Handled by: retries, timeouts, quorum reads.

  4. BYZANTINE (arbitrary behavior)
     Node sends incorrect/malicious messages.
     Handled by: BFT protocols (expensive; used in blockchains, not databases).

  Recovery strategies by failure mode:
  ┌─────────────────┬──────────────────────────────────────────────┐
  │ Failure          │ Recovery                                      │
  ├─────────────────┼──────────────────────────────────────────────┤
  │ Single replica   │ Raft/Paxos continues with remaining majority│
  │ Minority of nodes│ System continues normally                   │
  │ Majority of nodes│ System is UNAVAILABLE for writes (CP)       │
  │ Leader failure   │ Election timeout → new leader (10-30s)      │
  │ Disk failure     │ Replace node, stream data from replicas     │
  │ Full AZ down     │ Surviving AZs form majority (if 3 AZs)     │
  │ Region failure   │ Multi-region: failover to surviving region  │
  └─────────────────┴──────────────────────────────────────────────┘
```

### 8.3 Split-Brain Prevention (Detailed)

```
Split-Brain Prevention Mechanisms
══════════════════════════════════

  1. FENCING TOKENS (detailed)

  Token Service (consensus-based):
    Epoch 1 → token=1 → Leader A
    Epoch 2 → token=2 → Leader B

  ┌──────────┐  write(x=5, token=2)  ┌──────────┐
  │ Leader B │ ──────────────────────►│ Storage  │
  │ (new)    │                        │          │
  └──────────┘                        │ max_token│
                                      │ = 2      │
  ┌──────────┐  write(x=3, token=1)  │          │
  │ Leader A │ ──────────────────────►│ REJECTED │
  │ (old)    │                        │ token 1  │
  └──────────┘                        │ < 2      │
                                      └──────────┘

  Storage MUST be fencing-aware:
    - ZooKeeper: uses zxid as fencing token
    - etcd: uses revision number
    - Lock services should embed fencing token in lock metadata

  2. LEASE-BASED LEADERSHIP

  Leader A acquires lease at t=0, valid until t=10.

  Timeline:
  ──────────────────────────────────────────────────────────────►
  t=0     t=5        t=10      t=12       t=22
  │ A gets│ A renews  │ A's lease│ B gets   │ B renews
  │ lease │ lease     │ expires  │ lease    │ lease
  │       │ (reset to │ (A must  │ (A cannot│
  │       │  t=15)    │  stop)   │  get it) │

  Critical requirement:
    CLOCKS MUST BE BOUNDED. If A's clock is slow, it may think its
    lease is still valid when it has actually expired.

    Safety: lease holder must use CONSERVATIVE clock.
    Holder assumes lease expires at (grant_time + duration - max_clock_drift).

  3. QUORUM-BASED (majority rules)

  5-node cluster, network partition:

  Partition A: {N1, N2}       Partition B: {N3, N4, N5}
  Cannot form majority (3)    CAN form majority (3)
  → Steps down               → Elects new leader

  Even partition {N1, N2, N3} vs {N4, N5}:
  {N1, N2, N3} has majority → continues
  {N4, N5} cannot form majority → becomes read-only/unavailable

  This is why 3 or 5 nodes (odd numbers) are strongly recommended.
  With even numbers, a perfect split leaves both sides unable to proceed.
```

### 8.4 Quorum Decisions and Failure Math

```
Failure Tolerance by Replication Factor
════════════════════════════════════════

  N = total replicas, F = max tolerated failures

  For Raft/Paxos (majority quorum):
    F = (N - 1) / 2   (integer division)

  ┌────┬────────────────┬──────────────────────────────────┐
  │ N  │ Tolerated (F)  │ Notes                            │
  ├────┼────────────────┼──────────────────────────────────┤
  │ 1  │ 0              │ No fault tolerance               │
  │ 2  │ 0              │ Worse than 1 (same tolerance +   │
  │    │                │  risk of split-brain)            │
  │ 3  │ 1              │ Most common (etcd, CockroachDB)  │
  │ 5  │ 2              │ Cross-AZ or cross-region         │
  │ 7  │ 3              │ Rarely needed; higher latency    │
  └────┴────────────────┴──────────────────────────────────┘

  For Aurora storage (4/6 write quorum):
    Write: tolerates 2 failures (needs 4 of 6)
    Read: tolerates 3 failures (needs 3 of 6)

  Availability calculation (independent failures):
    P(single node available) = 0.999 (three nines)
    P(Raft group with N=3, F=1):
      = P(at least 2 of 3 available)
      = 3 × 0.999² × 0.001 + 0.999³
      = 0.002997 + 0.997003
      = 0.999997 (six nines)

    N=5, F=2: ~0.9999999 (seven nines from three-nines nodes)
```

---

## 9. Consistency Models

Consistency models define the guarantees a distributed system provides about the order and visibility of operations. Stronger models are easier to program against but harder to implement efficiently.

```
Consistency Model Spectrum
═══════════════════════════

  STRONG ◄──────────────────────────────────────────────► WEAK
  (Easier to reason about)                (Higher performance)

  Linearizability
   │
   ▼
  Sequential Consistency
   │
   ▼
  Causal Consistency
   │
   ▼
  Bounded Staleness
   │
   ▼
  Eventual Consistency
```

### 9.1 Linearizability

```
Linearizability (Strongest)
════════════════════════════

  Every operation appears to take effect atomically at some point
  between its invocation and completion. All operations form a
  total order consistent with real-time ordering.

  Real time: ───────────────────────────────────────────────────►

  Client A:  ├── write(x=1) ──┤
  Client B:                          ├── read(x) ──┤ must return 1

  Since B's read starts AFTER A's write finishes (in real time),
  B MUST see A's write. This is linearizability.

  Non-linearizable (allowed under weaker models):
  Client A:  ├── write(x=1) ──┤
  Client B:                          ├── read(x) ──┤ returns 0 (stale!)

  Linearizability guarantees:
    - Real-time ordering respected
    - No stale reads (once a write is visible to anyone, it is visible to everyone)
    - Equivalent to having a single copy of the data

  Cost:
    - Requires consensus (Raft, Paxos) or synchronous replication
    - Latency lower-bound: 1 RTT to majority of replicas
    - Not composable (linearizable ops on different objects ≠ linearizable together)

  Systems providing linearizability:
    - etcd, ZooKeeper (for all operations)
    - CockroachDB, Spanner (for transactions)
    - DynamoDB (with consistent read flag)
```

### 9.2 Sequential Consistency

```
Sequential Consistency
═══════════════════════

  All operations appear in SOME total order that is consistent
  with each process's program order. But this total order does
  NOT need to match real-time ordering.

  Difference from linearizability:

  Real time: ───────────────────────────────────────────────────►

  Client A:  ├── write(x=1) ──┤
  Client B:  ├── write(x=2) ──┤   (concurrent with A)
  Client C:                          ├── read(x) ──┤ returns 2
  Client D:                          ├── read(x) ──┤ returns 2

  Under sequential consistency: valid if total order is [A:write(1), B:write(2)]
  All clients see the SAME order, but it need not reflect real time.

  Under linearizability: if A finishes before B starts, order must be [A, B].
  Under sequential consistency: order could be [B, A] as long as all agree.

  Zookeeper provides sequential consistency (not linearizability)
  for reads -- reads may be served from followers, which may be stale.
  But within a single client session, reads are monotonic and consistent.
```

### 9.3 Causal Consistency

```
Causal Consistency
═══════════════════

  Only causally related operations must be seen in the same order
  by all nodes. Concurrent operations may be seen in different orders.

  Causally related:
    - A writes x; B reads x; B writes y → write(x) "happened before" write(y)
    - A writes x; A reads y → both are causally ordered (same process)

  Concurrent (NOT causally related):
    - A writes x; B writes y (independently) → no causal relation

  Example:
    Alice posts: "I'm moving to NYC"              (post #1)
    Bob reads Alice's post and replies: "Cool!"    (post #2)

    Causal consistency guarantees:
      Everyone sees post #1 before post #2 (because #2 causally depends on #1)

    Carol independently posts: "Nice weather today" (post #3)
      Post #3 is concurrent with #1 and #2.
      Some users may see #3 before #1, others after. That's fine.

  Implementation:
    - Vector clocks / version vectors to track causality
    - Lamport timestamps (provide total order, but not minimal causal order)
    - Hybrid logical clocks (HLC): wall clock + logical counter

  Systems: MongoDB (with majority read concern + majority write concern)
```

### 9.4 Eventual Consistency

```
Eventual Consistency (Weakest Useful Guarantee)
════════════════════════════════════════════════

  If no new writes occur, all replicas will EVENTUALLY converge
  to the same value.

  "Eventually" means:
    - No upper bound on convergence time (in theory)
    - In practice: milliseconds to seconds for same-region
    - Seconds to minutes for cross-region

  Guarantees:
    ✓ All replicas eventually agree
    ✗ No guarantee WHEN
    ✗ No guarantee on read ordering
    ✗ Read may return any previously written value (or none)

  Strengths:
    + Highest availability (any replica can serve reads/writes)
    + Lowest latency (no coordination needed)
    + Works across unreliable networks

  Used by: Cassandra (default), DynamoDB (default), DNS, CDN caches

  Anti-patterns:
    - DO NOT use eventual consistency for counters that need accuracy
    - DO NOT use for distributed locks or leader election
    - DO NOT use for financial balances
```

### 9.5 Bounded Staleness

```
Bounded Staleness
══════════════════

  A middle ground: reads may be stale, but staleness is bounded.

  Two forms:
    1. Time-bounded: reads are at most T seconds behind
    2. Version-bounded: reads are at most K versions behind

  Example: Azure Cosmos DB "Bounded Staleness" consistency
    - Configure: reads at most 5 seconds or 100 versions behind
    - Within a region: behaves like strong consistency
    - Cross-region: bounded by the configured lag

  Spanner "stale reads":
    SELECT * FROM users AS OF SYSTEM TIME TIMESTAMP_SUB(CURRENT_TIMESTAMP, INTERVAL 15 SECOND);
    - Read at a timestamp 15 seconds in the past
    - Guaranteed to not need any coordination (all replicas have this data)
    - Useful for analytics queries that can tolerate slight staleness

  CockroachDB follower reads:
    SET CLUSTER SETTING kv.closed_timestamp.target_duration = '5s';
    SELECT * FROM users AS OF SYSTEM TIME '-5s';
    - Read from the nearest replica (not just leaseholder)
    - Must be at least 5 seconds stale
    - Dramatically reduces read latency for geo-distributed reads
```

### 9.6 Jepsen Testing

```
Jepsen: Distributed Systems Testing
═════════════════════════════════════

  Created by Kyle Kingsbury ("Aphyr"). The gold standard for testing
  distributed database correctness under failure conditions.

  What Jepsen does:
    1. Sets up a cluster of nodes (typically 5)
    2. Runs concurrent operations (reads, writes, transactions)
    3. Injects failures:
       - Network partitions (iptables rules)
       - Node crashes (kill -9)
       - Clock skew (ntpd manipulation)
       - Disk pauses (SIGSTOP on processes)
    4. Checks if results are consistent with the claimed model

  Consistency checkers:
    - Linearizability: Knossos checker (NP-complete but works for small histories)
    - Serializability: Elle checker (cycle detection in dependency graphs)
    - Monotonic reads, read-your-writes, etc.

  Notable findings:
  ┌────────────────────┬──────────────────────────────────────────────┐
  │ System             │ Issues Found                                 │
  ├────────────────────┼──────────────────────────────────────────────┤
  │ MongoDB (2013-15)  │ Lost writes, stale reads under partition    │
  │ Elasticsearch      │ Split-brain, lost writes                    │
  │ Cassandra          │ LWT (Paxos) correctness issues              │
  │ CockroachDB        │ Serializability violations (fixed)          │
  │ TiDB               │ Snapshot isolation anomalies (fixed)        │
  │ Redis Cluster      │ Lost writes during failover (by design)     │
  │ Galera Cluster     │ Inconsistency under partition               │
  │ YugabyteDB         │ Various issues in early versions (fixed)    │
  │ etcd               │ Generally clean results                     │
  │ ZooKeeper          │ Generally clean results                     │
  └────────────────────┴──────────────────────────────────────────────┘

  Why Jepsen matters:
    - Vendors claim "strong consistency" -- Jepsen verifies
    - Found bugs in nearly EVERY system tested
    - Shifted industry norms: databases now proactively Jepsen-test
    - CockroachDB, TiDB, YugabyteDB all run regular Jepsen tests
```

---

## 10. Comparison Table

### Full System Comparison

| Feature | CockroachDB | TiDB | Spanner | Vitess | Aurora | YugabyteDB | Cassandra | DynamoDB |
|---|---|---|---|---|---|---|---|---|
| **Type** | NewSQL | NewSQL | NewSQL | Sharding proxy | Cloud-native | NewSQL | Wide-column | Key-value/Doc |
| **SQL compatibility** | PostgreSQL | MySQL | Proprietary SQL | MySQL | MySQL / PG | PostgreSQL | CQL (not SQL) | Proprietary API |
| **Replication** | Raft | Raft | Paxos | MySQL repl | Quorum (4/6) | Raft | Leaderless | Leaderless |
| **Sharding** | Range (auto-split) | Range (auto-split) | Range | Hash (vindex) | None (shared storage) | Range/Hash | Consistent hash | Consistent hash |
| **Default isolation** | Serializable | Snapshot (SI) | External consistency | MySQL default (RR) | MySQL/PG default | Snapshot (SI) | N/A (per-row) | Eventual |
| **Strong consistency** | Yes (serial.) | Yes (SI by default) | Yes (external) | Per-shard only | Per-instance only | Yes (serial.) | Per-key (LWT) | Optional |
| **Distributed txns** | Yes (Percolator-like) | Yes (Percolator) | Yes (2PC + TrueTime) | Limited | Single-writer | Yes | No (LWT only) | Single-item only |
| **Multi-region** | Yes (native) | Yes (follower read) | Yes (native) | Manual | Limited (Global DB) | Yes (native) | Yes (native) | Yes (Global Tables) |
| **HTAP** | Follower reads | TiFlash (columnar) | No | No | No | No | No | No |
| **Clock** | HLC (500ms max offset) | TSO (centralized) | TrueTime (GPS+atomic) | N/A (MySQL clocks) | N/A (single writer) | HLC | NTP | Server-side |
| **Open source** | Yes (BSL) | Yes (Apache 2.0) | No | Yes (Apache 2.0) | No | Yes (Apache 2.0) | Yes (Apache 2.0) | No |
| **Max proven scale** | 100s of TBs | PBs (PingCAP) | Exabytes (Google) | PBs (YouTube) | 128TB per instance | 100s of TBs | PBs | PBs |
| **Write latency (same region)** | ~10-20ms | ~10-20ms | ~7-15ms (commit wait) | ~2-5ms (single shard) | ~2-5ms | ~10-20ms | ~1-2ms (CL=1) | ~5-10ms |
| **CAP classification** | CP | CP | CP | CP (per shard) | CP (single writer) | CP | AP (default) | AP (default) |
| **PACELC** | PC/EC | PC/EC | PC/EC | PC/EL | PC/EL | PC/EC | PA/EL | PA/EL |

### When to Use Which System

```
Decision Guide
═══════════════

  Need distributed SQL with strong consistency?
  ├── Budget for Google Cloud → Spanner
  ├── PostgreSQL compatibility → CockroachDB or YugabyteDB
  └── MySQL compatibility → TiDB

  Already have MySQL, need horizontal scaling?
  ├── Application-transparent sharding → Vitess
  └── Full distributed SQL → TiDB

  Need single-region high-performance with managed service?
  └── Aurora (MySQL or PostgreSQL)

  Need AP (high availability, eventual consistency)?
  ├── Self-managed → Cassandra
  └── Fully managed → DynamoDB

  Need HTAP (analytics + transactions)?
  └── TiDB + TiFlash

  Need global deployment with low-latency reads?
  ├── Strong consistency writes → Spanner or CockroachDB
  └── Eventual consistency OK → DynamoDB Global Tables or Cassandra
```

### Operational Complexity Comparison

| Dimension | CockroachDB | TiDB | Spanner | Vitess | Aurora | YugabyteDB | Cassandra |
|---|---|---|---|---|---|---|---|
| **Setup difficulty** | Medium | High (3 components) | Easy (managed) | High | Easy (managed) | Medium | Medium |
| **Backup/restore** | Built-in | BR tool | Automatic | Per-shard mysqldump | Automatic snapshots | Built-in | sstableloader |
| **Schema changes** | Online DDL | Online DDL | Online DDL | Ghost/pt-osc per shard | Standard MySQL/PG | Online DDL | Instant (schema-free) |
| **Monitoring** | DB Console + Prometheus | Grafana dashboards (official) | Cloud Console | VTAdmin + vtctld | CloudWatch | Built-in UI | nodetool + JMX |
| **Upgrade path** | Rolling upgrade | Rolling (TiKV first) | Automatic | Rolling per cell | Automatic | Rolling upgrade | Rolling upgrade |
| **Node replacement** | Automatic rebalancing | PD schedules repair | Automatic | Replace MySQL + restore | Automatic | Automatic rebalancing | nodetool repair |
| **Cost model** | Per-node (or CockroachDB Cloud) | Per-node (or TiDB Cloud) | Per-node-hour + storage + ops | Per MySQL instance | Per-instance + I/O + storage | Per-node (or Managed) | Per-node |

---

## Key Takeaways

```
Top 10 Principles for Distributed Storage
═══════════════════════════════════════════

  1. REPLICATION is for availability and read scalability.
     SHARDING is for write scalability and storage capacity.
     You almost always need BOTH.

  2. CAP is a spectrum, not a binary choice.
     Most systems let you tune consistency per-operation.

  3. Raft won the consensus war for open-source systems.
     Paxos won at Google. Both work. Raft is easier to implement correctly.

  4. Single-leader replication is the safe default.
     Multi-leader only when you need multi-DC write latency.
     Leaderless when you need extreme write availability.

  5. 2PC is necessary but dangerous.
     Modern systems (Spanner, CockroachDB, TiDB) wrap 2PC with
     consensus-replicated transaction managers to avoid blocking.

  6. The biggest operational pain in distributed databases is REBALANCING.
     Choose a system that auto-splits and auto-rebalances.

  7. Jepsen-test your database before betting production on it.
     Marketing claims are not the same as verified correctness.

  8. Clocks matter more than you think.
     NTP is insufficient for transaction ordering.
     TrueTime (Spanner), TSO (TiDB), and HLC (CockroachDB) are
     three different solutions to the same fundamental problem.

  9. Network partitions are not just "cable cut" events.
     They are slow NICs, overloaded switches, asymmetric routes,
     and GC pauses that look like network failures.

 10. Start with the simplest architecture that meets your requirements.
     Single-node PostgreSQL handles more than most teams think.
     Distribute only when you have evidence you need to.
```
