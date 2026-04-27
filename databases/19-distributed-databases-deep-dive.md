# Distributed Databases: A Staff-Engineer Deep Dive

A comprehensive reference covering distributed database **architectures** (shared-nothing, shared-disk, shared-storage, disaggregated), **algorithms** (HLC, TrueTime, Percolator, Calvin, CRDTs, Merkle anti-entropy, SWIM gossip), **patterns** (leaseholders, follower reads, bounded staleness, online schema change, CDC at scale), **protocols** (2PC/3PC, parallel commit, deterministic execution, quorum reads/writes), **modern systems** (Spanner, CockroachDB, TiDB, YugabyteDB, FoundationDB, DynamoDB, Cassandra, ScyllaDB, Aurora, Neon, AlloyDB, FaunaDB, DSQL), **operational nuances**, **failure modes**, and **best practices**.

Prerequisites: file 12 (replication, consensus, sharding basics), file 16 (failure detection, leader election), file 05 (transactions, MVCC).

This document is the *architectural and modern-systems* counterpart to file 12, going deeper on algorithms specific to distributed databases (vs. general replication) and surveying the modern landscape.

---

## Table of Contents

1. [The Distributed Database Landscape](#1-the-distributed-database-landscape)
2. [Architectural Patterns](#2-architectural-patterns)
3. [Time, Clocks, and Ordering](#3-time-clocks-and-ordering)
4. [Distributed Transaction Protocols Deep Dive](#4-distributed-transaction-protocols-deep-dive)
5. [Distributed Concurrency Control](#5-distributed-concurrency-control)
6. [Replication Patterns Beyond Leader-Follower](#6-replication-patterns-beyond-leader-follower)
7. [Conflict Resolution and CRDTs](#7-conflict-resolution-and-crdts)
8. [Anti-Entropy and Repair](#8-anti-entropy-and-repair)
9. [Membership and Cluster Management](#9-membership-and-cluster-management)
10. [Distributed Schema Changes](#10-distributed-schema-changes)
11. [Change Data Capture and Streaming](#11-change-data-capture-and-streaming)
12. [Multi-Region and Geo-Distribution](#12-multi-region-and-geo-distribution)
13. [Disaggregated Storage Architectures](#13-disaggregated-storage-architectures)
14. [NewSQL and Distributed SQL Systems](#14-newsql-and-distributed-sql-systems)
15. [Wide-Column and Key-Value at Scale](#15-wide-column-and-key-value-at-scale)
16. [Document and Multi-Model Distributed Databases](#16-document-and-multi-model-distributed-databases)
17. [Operational Patterns and Anti-Patterns](#17-operational-patterns-and-anti-patterns)
18. [Failure Modes and War Stories](#18-failure-modes-and-war-stories)
19. [Comparison Matrix](#19-comparison-matrix)
20. [Decision Frameworks](#20-decision-frameworks)

---

## 1. The Distributed Database Landscape

### 1.1 Taxonomy

Distributed databases are not monolithic — they span at least four orthogonal axes. Understanding which axes a system lives on tells you what tradeoffs it has made.

```
┌────────────────────────────────────────────────────────────────────────────┐
│                  Four Axes of Distributed Database Design                   │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│  AXIS 1: DATA MODEL                                                        │
│    Relational (SQL)  ──  Key-Value  ──  Wide-Column  ──  Document  ──  Graph│
│                                                                            │
│  AXIS 2: CONSISTENCY                                                       │
│    Linearizable  ──  Sequential  ──  Snapshot  ──  Causal  ──  Eventual    │
│                                                                            │
│  AXIS 3: REPLICATION TOPOLOGY                                              │
│    Single-Leader  ──  Multi-Leader  ──  Leaderless  ──  Consensus-per-Range│
│                                                                            │
│  AXIS 4: STORAGE COUPLING                                                  │
│    Shared-Nothing  ──  Shared-Disk  ──  Shared-Storage (Disaggregated)    │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

A few example placements:

| System | Model | Consistency | Replication | Storage |
|---|---|---|---|---|
| Spanner | Relational | External (linearizable) | Per-range Paxos | Shared-nothing on Colossus |
| CockroachDB | Relational | Serializable | Per-range Raft | Shared-nothing |
| DynamoDB | KV/Document | Eventual or strong (per request) | Multi-leader (sloppy quorum) | Shared-nothing |
| Cassandra | Wide-column | Tunable | Leaderless quorum | Shared-nothing |
| Aurora | Relational | Snapshot/serializable | Single-leader compute, 6-way storage | Shared-storage |
| FoundationDB | KV (ordered) | Strict serializable | Single-leader sequencer + log servers | Shared-nothing |
| MongoDB (replicaset) | Document | Linearizable (with `w:majority, readConcern:linearizable`) | Single-leader | Shared-nothing |
| Cosmos DB | Multi-model | 5 levels (strong → eventual) | Multi-leader | Shared-nothing |

### 1.2 Why "Distributed Database" Resists a Single Definition

A "distributed database" can mean any of:

1. **Replicated database**: same data on multiple nodes for fault-tolerance/read-scaling. PostgreSQL with streaming replication.
2. **Sharded database**: data partitioned across nodes to scale storage/writes. Vitess, Citus.
3. **Distributed SQL**: transparent sharding + replication + distributed transactions, presenting as a single logical database. CockroachDB, Spanner, TiDB.
4. **Distributed KV/wide-column**: simpler model, horizontally scalable, often weaker consistency. Cassandra, DynamoDB.
5. **Disaggregated database**: compute and storage scale independently across a fast network. Aurora, Snowflake, Neon.
6. **Multi-region database**: replicas in geographically distant regions, often with per-row or per-range placement controls. Spanner, YugabyteDB, Cosmos DB.

These categories overlap. CockroachDB is *all five at once*: replicated, sharded, distributed-SQL, with optional multi-region placement, and recent versions support disaggregated storage.

### 1.3 The Five Hard Problems

Every distributed database confronts the same five problems. The differences between systems are largely **which solution they pick for each**:

```
┌──────────────────────────────────────────────────────────────────────────┐
│  PROBLEM             │  SOLUTION SPACE                                    │
├──────────────────────┼────────────────────────────────────────────────────┤
│  1. Where does data  │  Hash partitioning / range partitioning /          │
│     live?            │  consistent hashing / directory service            │
│                      │                                                    │
│  2. How do we agree  │  Paxos / Raft / ZAB / EPaxos / Viewstamped Repl /  │
│     on order?        │  causal consistency / vector clocks / HLC          │
│                      │                                                    │
│  3. How do we commit │  2PC / 3PC / Percolator / Calvin / Spanner-style   │
│     transactions     │  TrueTime + Paxos / Parallel Commit / FDB pipeline │
│     across shards?   │                                                    │
│                      │                                                    │
│  4. How do we detect │  Heartbeats / Phi-accrual / SWIM gossip /          │
│     and recover from │  lease-based leadership / quorum failure detector  │
│     failures?        │                                                    │
│                      │                                                    │
│  5. How do we handle │  Synchronous quorums / async replication +         │
│     network          │  conflict resolution / CRDTs / read repair /       │
│     partitions?      │  hinted handoff / Merkle anti-entropy              │
└──────────────────────┴────────────────────────────────────────────────────┘
```

---

## 2. Architectural Patterns

### 2.1 Shared-Nothing

Each node owns its own CPU, memory, and storage. Nodes communicate only via the network. This is the dominant architecture for horizontally scalable databases.

```
SHARED-NOTHING ARCHITECTURE

  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
  │   Node 1    │    │   Node 2    │    │   Node 3    │    │   Node N    │
  │  ┌───────┐  │    │  ┌───────┐  │    │  ┌───────┐  │    │  ┌───────┐  │
  │  │  CPU  │  │    │  │  CPU  │  │    │  │  CPU  │  │    │  │  CPU  │  │
  │  ├───────┤  │    │  ├───────┤  │    │  ├───────┤  │    │  ├───────┤  │
  │  │  RAM  │  │    │  │  RAM  │  │    │  │  RAM  │  │    │  │  RAM  │  │
  │  ├───────┤  │    │  ├───────┤  │    │  ├───────┤  │    │  ├───────┤  │
  │  │ Disk  │  │    │  │ Disk  │  │    │  │ Disk  │  │    │  │ Disk  │  │
  │  │ shard │  │    │  │ shard │  │    │  │ shard │  │    │  │ shard │  │
  │  │  1-K  │  │    │  │ K+1.. │  │    │  │  ..   │  │    │  │  ..N  │  │
  │  └───────┘  │    │  └───────┘  │    │  └───────┘  │    │  └───────┘  │
  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘    └──────┬──────┘
         │                  │                  │                  │
         └──────────────────┴────────┬─────────┴──────────────────┘
                                     │
                              Network (TCP/RPC)

  PROS:                                  CONS:
   ─ Linear horizontal scalability        ─ Cross-shard ops require coordination
   ─ No shared bottleneck                 ─ Rebalancing requires data movement
   ─ Failure isolation                    ─ Heterogeneous hardware → hot spots
   ─ Commodity hardware                   ─ Backup is per-node (complex)
```

Examples: CockroachDB, Cassandra, MongoDB sharded, BigTable, Spanner, FoundationDB, TiKV.

### 2.2 Shared-Disk

Multiple compute nodes access a shared storage subsystem (SAN, NAS, or distributed block storage). Locking and cache coherency become the central problems.

```
SHARED-DISK ARCHITECTURE

  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
  │   Node 1    │  │   Node 2    │  │   Node 3    │  │   Node N    │
  │ ┌─────────┐ │  │ ┌─────────┐ │  │ ┌─────────┐ │  │ ┌─────────┐ │
  │ │ CPU+RAM │ │  │ │ CPU+RAM │ │  │ │ CPU+RAM │ │  │ │ CPU+RAM │ │
  │ │  + buf  │ │  │ │  + buf  │ │  │ │  + buf  │ │  │ │  + buf  │ │
  │ │  cache  │ │  │ │  cache  │ │  │ │  cache  │ │  │ │  cache  │ │
  │ └─────────┘ │  │ └─────────┘ │  │ └─────────┘ │  │ └─────────┘ │
  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
         └────────────────┴─────────┬──────┴────────────────┘
                                    │
                ┌───────────────────▼──────────────────┐
                │  Distributed Lock Manager (DLM)      │
                │  + Cache Coherency Protocol          │
                │  (Oracle RAC: Cache Fusion)          │
                └───────────────────┬──────────────────┘
                                    │
                ┌───────────────────▼──────────────────┐
                │       Shared Storage (SAN)           │
                │       Single namespace               │
                └──────────────────────────────────────┘

  PROS:                                  CONS:
   ─ Any node can serve any query         ─ DLM is a coordination bottleneck
   ─ No re-sharding needed                ─ Storage subsystem must scale 10x compute
   ─ Failover is fast (no data move)      ─ Cross-node cache invalidation = chatty
   ─ Simpler programming model            ─ Hardware cost (SAN) is high
```

Examples: Oracle RAC, IBM DB2 pureScale, older Sybase ASE clusters.

**Why shared-disk fell out of fashion**: at hundreds of nodes, the DLM and cache-coherency traffic dominates. Modern cloud workloads scale better with shared-nothing or disaggregated storage.

### 2.3 Shared-Storage (Disaggregated)

A modern hybrid: stateless or near-stateless compute nodes operate against a *log-structured distributed storage layer* that handles durability, replication, and (optionally) page materialization. Pioneered by Aurora (2014).

```
DISAGGREGATED ARCHITECTURE (Aurora-style)

   READER         READER         WRITER (single)        READER
  ┌──────┐      ┌──────┐      ┌──────────────┐       ┌──────┐
  │ SQL  │      │ SQL  │      │     SQL      │       │ SQL  │
  │ +buf │      │ +buf │      │  + buffer    │       │ +buf │
  │      │      │      │      │  + tx mgr    │       │      │
  └───┬──┘      └───┬──┘      └──────┬───────┘       └───┬──┘
      │             │                │                   │
      │   reads     │   reads        │  writes (REDO log only)
      │   (pages)   │   (pages)      │
      │             │                ▼
      │             │       ┌──────────────────┐
      │             │       │  Log Stream      │
      │             │       │  (sequenced)     │
      │             │       └────────┬─────────┘
      │             │                │
      └─────────────┴──────┬─────────┴─────────────┐
                           │                        │
              ┌────────────▼──────────┐             │
              │  Distributed Storage  │◄────────────┘
              │  (6 replicas across   │
              │   3 AZs, quorum 4/6   │
              │   for write, 3/6 for  │
              │   read)               │
              │                       │
              │  - Page service       │
              │  - Continuous redo    │
              │    log application    │
              │  - Storage-side       │
              │    crash recovery     │
              └───────────────────────┘

  KEY INSIGHT: writer ships ONLY the redo log over the network.
  Pages are reconstructed by storage nodes on demand.
  This makes writes 10-100x cheaper in network bytes than
  traditional replication.
```

**Aurora's six core innovations:**

1. **The log is the database**: writers send only redo log records to storage. No pages ever traverse the network on the write path.
2. **6-replica storage with quorum 4/6**: tolerates entire AZ failure (loses 2 replicas) plus one additional disk failure.
3. **Continuous backup**: storage layer continuously snapshots to S3 with no checkpoint stalls.
4. **Fast clones**: copy-on-write at the storage layer makes instant database clones cheap.
5. **Crash recovery offloaded to storage**: redo apply happens in storage; compute restarts in seconds, not minutes.
6. **Read replicas share the storage**: replicas don't need their own copy of data; they only need cache invalidation messages.

**Variations and successors:**

| System | Variation |
|---|---|
| Aurora (AWS) | Original design; MySQL/Postgres-compatible; single writer |
| Aurora Serverless v2 | Adds elastic compute autoscaling on the same storage |
| Aurora DSQL | Multi-region active-active on top of disaggregated storage with optimistic concurrency control |
| Socrates (Azure SQL Hyperscale) | Adds page server tier between log and durable storage; supports >100TB |
| AlloyDB (GCP) | Postgres compatibility; columnar engine layered on disaggregated storage; AI-aware |
| Neon | Open-source disaggregated Postgres; Pageserver + Safekeepers; copy-on-write branching as a primary feature |
| PlanetScale Metal | NVMe-direct compute pools backing Vitess shards |
| Yellowbrick | Disaggregated MPP for analytics |
| Snowflake | Disaggregated MPP; storage + compute warehouses fully decoupled |
| BigQuery | Compute (Dremel slots) decoupled from storage (Capacitor on Colossus) |

### 2.4 Disaggregated vs Shared-Nothing: When Each Wins

```
DISAGGREGATED                            SHARED-NOTHING
═══════════════════════════              ═══════════════════════════

✓ Elastic compute scaling                ✓ Linear scale to many writers
✓ Fast read replicas (no copy)           ✓ No shared storage bottleneck
✓ Fast cloning/branching                 ✓ Cheaper at large scale
✓ Storage outlives compute               ✓ Predictable per-node performance
✓ Simpler operations                     ✓ Mature operationally
✗ Write throughput limited by single     ✗ Re-sharding requires data movement
  writer (Aurora)                        ✗ Heterogeneous hardware → hotspots
✗ Storage layer is a SPOF (mitigated     ✗ Cross-shard ops require distributed
  by replication, but architecturally)     transactions
✗ Network bandwidth between compute      ✗ Read replicas need full storage copy
  and storage matters

USE DISAGGREGATED WHEN:                  USE SHARED-NOTHING WHEN:
  - Variable compute load                  - Write throughput > 1 node can
  - Need cheap clones (dev/test, ML)        handle
  - Want to scale reads without            - Multi-region active-active needed
    full data copies                       - Cost optimization at large scale
  - Operational simplicity matters         - Workload fits a single shard well
```

### 2.5 The HTAP Twist: Tiered Engines

Modern HTAP systems (TiDB, AlloyDB, SingleStore) often combine *two* storage engines under one query layer:

```
HTAP TIERED ARCHITECTURE

  ┌─────────────────────────────────────────────────────────┐
  │              Query Coordinator (SQL)                    │
  └────────┬────────────────────────────┬───────────────────┘
           │                            │
           ▼                            ▼
  ┌─────────────────┐          ┌─────────────────┐
  │  Row Engine     │          │  Column Engine  │
  │  (TiKV/Aurora   │  CDC/    │  (TiFlash/      │
  │   row pages)    │  Raft──► │   AlloyDB       │
  │                 │          │   columnar)     │
  │  OLTP path      │          │  OLAP path      │
  │  Strong         │          │  Eventually     │
  │  consistency    │          │  consistent     │
  │                 │          │  (~ms lag)      │
  └─────────────────┘          └─────────────────┘
```

The row engine handles transactions; a Raft/CDC pipeline asynchronously feeds a columnar replica. Queries are routed to the appropriate engine. (See file 09 for HTAP details.)

---

## 3. Time, Clocks, and Ordering

Distributed databases live or die by their ability to order events. There is no global "now" — the question becomes how close you can get.

### 3.1 The Three Clock Models

```
┌─────────────────────────────────────────────────────────────────────────┐
│  CLOCK TYPE        │ ORDERING CAPTURED      │ COST          │ EXAMPLE   │
├────────────────────┼────────────────────────┼───────────────┼───────────┤
│  Physical (NTP)    │ Approximate real time  │ Free          │ Most apps │
│                    │ ±10-100ms drift        │               │           │
│                    │                        │               │           │
│  Logical (Lamport) │ Causal partial order   │ Cheap         │ Riak,     │
│                    │ Cannot detect          │               │ Dynamo    │
│                    │ concurrent events      │               │           │
│                    │                        │               │           │
│  Vector            │ Full causal order;     │ O(N) per ts   │ Riak,     │
│                    │ detects concurrency    │ N = nodes     │ COPS      │
│                    │                        │               │           │
│  Hybrid Logical    │ Causal + close to      │ Cheap         │ Cockroach,│
│  Clock (HLC)       │ wall clock             │               │ MongoDB,  │
│                    │                        │               │ YugaByte  │
│                    │                        │               │           │
│  TrueTime          │ Bounded uncertainty:   │ Atomic clocks │ Spanner   │
│                    │ guarantees real-time   │ + GPS in DC   │           │
│                    │ ordering across nodes  │               │           │
│                    │                        │               │           │
│  ClockBound        │ Same idea on AWS,      │ AWS Time Sync │ Aurora    │
│                    │ uses precision time    │ Service       │ DSQL      │
│                    │ protocol (PTP)         │               │           │
└────────────────────┴────────────────────────┴───────────────┴───────────┘
```

### 3.2 Lamport Clocks

The simplest logical clock. Each node maintains a counter; on every event it increments. On every send it includes the counter; on receive it sets `local = max(local, received) + 1`.

```
LAMPORT CLOCK EXAMPLE

  Node A:  e1 (1) ──── e2 (2) ──────────────► msg(2) ────┐
                                                          │
  Node B:                          e3 (1) ──── e4 (2) ◄───┘ e5 (3)
                                                            (max(2,2)+1=3)

  Property: if a ──happens-before──► b, then C(a) < C(b)
  But:      C(a) < C(b) does NOT imply a happens-before b
            (C(e3)=1 < C(e2)=2 but they are concurrent)
```

Useful for total order broadcast and total ordering of writes (with node ID as a tiebreaker), but **cannot detect concurrent events**. This matters when you need to reconcile divergent replicas.

### 3.3 Vector Clocks

Each node tracks the latest counter it has seen *from every other node*. A timestamp is a vector `[c_1, c_2, ..., c_N]`.

```
VECTOR CLOCK EXAMPLE (3 nodes: A, B, C)

  Node A:  [1,0,0] ──► [2,0,0] ──msg──┐
                                       │
  Node B:                              ▼
           [0,1,0] ──► [0,2,0] ──► [2,3,0] ──msg──┐
                                                   ▼
  Node C:                                          [2,3,1] ──► [2,3,2]

  Comparing [2,0,0] and [0,2,0]:
    Neither dominates → CONCURRENT writes (a conflict)

  Comparing [2,3,0] and [2,3,2]:
    Latter dominates → second is causally later
```

**Detecting concurrent writes** is what makes vector clocks essential for AP systems. When Riak or Dynamo finds two values for a key whose vector clocks are concurrent (no domination), it surfaces *both* as siblings to the application.

**Cost**: O(N) per timestamp where N = number of writers. This grows unboundedly in clusters with churn. Riak's solution: *dotted version vectors* (DVVs), which include only entries for nodes that have actually written.

### 3.4 Hybrid Logical Clocks (HLC)

Invented by Kulkarni et al. (2014). Combines a physical timestamp with a logical counter, giving you:

- Timestamps that are close to wall-clock time (so they're meaningful for humans and TTL)
- Strict causal ordering (so they preserve happens-before)
- Constant size (one int64 + one int)

```
HLC ALGORITHM

  Each node maintains: (l, c) where l ≈ physical time, c is logical counter

  ON LOCAL EVENT:
    l_new = max(l_old, physicalTime)
    c_new = (l_new == l_old) ? c_old + 1 : 0

  ON SEND: include (l, c) in message
  ON RECEIVE (l_msg, c_msg):
    l_new = max(l_old, l_msg, physicalTime)
    if l_new == l_old == l_msg: c_new = max(c_old, c_msg) + 1
    if l_new == l_old:           c_new = c_old + 1
    if l_new == l_msg:           c_new = c_msg + 1
    else:                        c_new = 0

  GUARANTEES:
    - HLC(a) < HLC(b) for any a happens-before b
    - HLC(t) is within bounded skew of wall-clock time at t
    - Comparable as a single int64 (l shifted left, c in low bits)
```

**Where HLCs are used:**
- **CockroachDB**: every transaction gets an HLC timestamp; commit timestamps are HLCs.
- **YugabyteDB**: same. Inherits from HLC's role in DocDB.
- **MongoDB**: cluster time uses HLCs for causal consistency across the replica set.
- **NATS JetStream**, **etcd-raft**, many internal systems.

**HLC's killer property**: with NTP keeping clocks within ~10ms, the logical counter rarely advances much, so HLCs stay within ~10ms of wall-clock time. This is what makes "timestamp-based snapshot reads" feasible without atomic clocks.

### 3.5 TrueTime (Spanner)

Google built atomic clocks and GPS receivers into every Spanner datacenter to bound clock uncertainty. The TrueTime API returns an *interval* `[earliest, latest]` instead of a point in time.

```
TRUETIME API:

  TT.now() returns (earliest, latest) such that:
    earliest ≤ true_time_now() ≤ latest

  Typical uncertainty (latest - earliest): 1-7 ms

  COMMIT WAIT:
    1. Acquire commit timestamp s = TT.now().latest
    2. Wait until TT.now().earliest > s   (this delays commit by ~ε)
    3. Release locks, ack client

  This guarantees: if T1 commits before T2 begins,
    then T1's commit timestamp < T2's commit timestamp
    (because T2 cannot start until everyone sees TT.now() > T1's ts)

  This gives EXTERNAL CONSISTENCY (linearizability) globally,
  even across continents, with no coordination.
```

**Why this is amazing**: Spanner can serve consistent read-only queries from *any local replica* anywhere in the world without contacting a leader. The replica just needs to ensure its local time has passed the read timestamp.

**Why it took until 2012 for someone to do this**: it requires capital expense most companies can't justify (atomic clocks in every datacenter, redundant GPS antennas).

### 3.6 ClockBound (AWS) and Modern PTP

AWS Time Sync Service + PTP (Precision Time Protocol) gives sub-millisecond clock accuracy in EC2. The `ClockBound` daemon exposes a TrueTime-style API on top of it. Aurora DSQL is built on this.

```
PTP vs NTP

  NTP:  ±10-100ms accuracy, software timestamping, request-response
  PTP:  ±100µs accuracy (often <10µs), hardware timestamping, master-driven

  AWS Time Sync (PTP) is available on Nitro instances since 2023.
  Microsoft has similar (Azure Precision Time Service).
```

This levels the playing field: cloud providers are now offering TrueTime-class guarantees as a service, removing Spanner's monopoly on external consistency.

### 3.7 Choosing a Clock

```
DECISION TREE: WHICH CLOCK?

  Need to detect concurrent writes for conflict resolution?
  ├── YES ──► Vector clocks or DVVs (Cassandra LWW + node ID is a degenerate case)
  └── NO ───► Need bounded staleness for snapshot reads?
              ├── YES ──► HLC (cheap) or TrueTime (strict, requires hardware)
              └── NO ───► Lamport clocks suffice
```

---

## 4. Distributed Transaction Protocols Deep Dive

### 4.1 Two-Phase Commit (2PC) Recap

Already covered in file 12. Quick recap of the failure modes:

```
2PC FAILURE MODES

  PREPARE phase:
    - Any participant votes NO → ABORT (safe)
    - Coordinator crashes → participants block holding locks (the BIG problem)

  COMMIT phase:
    - Coordinator decides COMMIT, sends to participants
    - Coordinator crashes after deciding, before all participants notified
    - Participants who got the message commit; others are stuck

  THE BLOCKING PROBLEM:
    A participant in PREPARED state cannot unilaterally decide.
    It must wait for the coordinator's decision (or timeout + recover from log).
    Locks are held the whole time.
```

This is why "vanilla" 2PC is rarely used in modern systems. Modern systems either avoid distributed commit, integrate it with consensus, or use deterministic execution.

### 4.2 Percolator (Google, 2010)

The transaction layer behind Google's web index. Built on top of BigTable, which has no transactions. Uses a single global timestamp oracle and stores transaction state *in the data table itself*.

```
PERCOLATOR DATA LAYOUT (per row)

  Column          Description
  ─────────────   ─────────────────────────────────────────────────────
  data:<ts>       Actual cell value at timestamp ts
  lock:<ts>       Transaction lock; format: (primary_row, primary_col)
  write:<ts>      Pointer to the data:<ts'> that this commit installs

PERCOLATOR PROTOCOL (snapshot isolation):

  BEGIN:
    start_ts = oracle.get_timestamp()

  PREWRITE (for each cell to write):
    1. Read row at start_ts; abort if there's a newer write or any lock
    2. Write data:<start_ts> with new value
    3. Write lock:<start_ts> = (primary_row, primary_col)
       (the FIRST cell written becomes the "primary"; all others reference it)

  COMMIT:
    1. commit_ts = oracle.get_timestamp()
    2. Atomically: replace primary's lock with write:<commit_ts> -> data:<start_ts>
       (this single CAS on the primary IS the commit point)
    3. Asynchronously: for every secondary, replace lock with write entry

  CRASH RECOVERY:
    If you read a row with a stale lock, look up the primary:
      - If primary has been committed, "roll forward" the secondary
      - If primary is still locked or aborted, "roll back" the secondary
```

**Why Percolator matters:**

1. **No distributed commit protocol per se** — the primary row's atomic compare-and-swap *is* the commit. All other writes are just cleanup.
2. **No coordinator** — every transaction is self-coordinating via the primary row.
3. **Works on top of any single-row CAS store** — BigTable, HBase, TiKV, FoundationDB.

**Performance characteristics:**
- 2 RPCs per row written (prewrite + finalize)
- Plus 2 RPCs to the timestamp oracle per transaction
- Oracle becomes a bottleneck at very high TPS (Google's scaled past 50k tx/s with batching)

**Used by:**
- **TiDB / TiKV**: classic Percolator with optimizations.
- **YugabyteDB** (DocDB layer): Percolator-inspired with HLC instead of an oracle.
- **OceanBase**: variation with localized timestamps.

### 4.3 Calvin (Yale, 2012) — Deterministic Execution

Calvin flips 2PC on its head. Instead of negotiating commits across replicas, it pre-orders transactions globally via consensus, then has every replica execute them deterministically in the same order.

```
CALVIN ARCHITECTURE

   Clients
      │
      ▼
  ┌─────────────────────────────────────────────────────┐
  │           Sequencer Layer (Paxos)                   │
  │  - Receives all transaction requests                │
  │  - Batches them into "epochs" (e.g., 10ms)          │
  │  - Globally orders epochs via Paxos                 │
  └────────────────────┬────────────────────────────────┘
                       │
                       ▼
  ┌─────────────────────────────────────────────────────┐
  │           Scheduler Layer                           │
  │  - Reads each transaction's read/write set          │
  │  - Determines lock-acquisition order DETERMINISTICALLY│
  │    based on transaction ordering                    │
  │  - Hands transactions to workers                    │
  └────────────────────┬────────────────────────────────┘
                       │
                       ▼
  ┌─────────────────────────────────────────────────────┐
  │           Storage Layer (Workers, replicated)       │
  │  - Each replica executes the SAME ordered sequence  │
  │  - No need for 2PC: outcome is deterministic        │
  └─────────────────────────────────────────────────────┘

  KEY REQUIREMENT: read/write sets must be known IN ADVANCE.
    For SQL with secondary index lookups, this requires a
    "reconnaissance" pre-execution phase.
```

**Calvin's claim**: by pre-ordering and executing deterministically, you eliminate distributed commit entirely. Every replica reaches the same conclusion independently.

**Used by:**
- **FaunaDB**: production Calvin implementation, multi-region serializable.
- **VoltDB** has similar ideas (deterministic execution of SQL on partitioned data).
- Influence on **Aurora DSQL** (which uses optimistic + ClockBound).

**Tradeoffs:**
- Sequencer is a coordination point (Paxos); typical commit latency = network RTT to majority of sequencers
- Read/write set discovery is hard for general SQL (Fauna restricts via FQL)
- Throughput can exceed 100k tx/s/region because there's no per-tx coordination

### 4.4 Spanner-Style: TrueTime + 2PC over Paxos Groups

Spanner combines all the techniques: each shard ("Paxos group") replicates via Paxos. Cross-shard transactions use 2PC, but each "participant" is itself a Paxos group, so the prepare/commit decisions are themselves replicated. TrueTime gives every transaction a globally meaningful timestamp.

```
SPANNER TRANSACTION (cross-shard)

  Coordinator group (one of the participants is elected coordinator)

         Participant A           Coordinator P           Participant B
         (Paxos group)           (Paxos group)           (Paxos group)
              │                        │                        │
              │◄──── client ───────────│                        │
              │       Begin Tx         │                        │
              │                        │                        │
   Reads ──►  │ acquire read locks     │                        │
              │ at start_ts            │                        │
              │                        │                        │
   Writes ─►  │ buffered locally       │ buffered locally       │
              │                        │                        │
              │   ── PREPARE phase ──  │                        │
              │                        │                        │
              │ Replicate prepare      │ Replicate prepare      │
              │ via Paxos              │ via Paxos              │
              │ vote = YES + prep_ts   │ vote = YES + prep_ts   │
              │                        │                        │
              │ ─────────────────────► │ ◄──────────────────────│
              │                        │                        │
              │                        │ commit_ts =            │
              │                        │   max(prep_ts) ; but   │
              │                        │   also > TT.now().latest│
              │                        │                        │
              │                        │ COMMIT WAIT until      │
              │                        │   TT.now().earliest    │
              │                        │   > commit_ts          │
              │                        │                        │
              │ ◄── COMMIT(commit_ts) ─│ ── COMMIT(commit_ts)─►│
              │                        │                        │
              │ Apply via Paxos        │                        │ Apply via Paxos
              │ Release locks          │                        │ Release locks
```

**Two key innovations Spanner adds to 2PC:**

1. **Each participant is a Paxos group**, so a participant crash doesn't lose the prepared state — a successor leader takes over.
2. **Commit wait** uses TrueTime to guarantee the commit timestamp is strictly later than any concurrent transaction that could have observed the commit. This gives external consistency.

**Latency cost**: ~2 RTTs minimum + commit wait (~7ms typical). But once committed, *any* read at `commit_ts` anywhere in the world sees the result without coordinating.

### 4.5 CockroachDB Parallel Commit

CockroachDB (since 2.1) reduced cross-range commit latency from 2 RTTs to ~1 RTT via parallel commit.

```
TRADITIONAL 2PC (CockroachDB pre-2.1):       PARALLEL COMMIT:

   Client ──BEGIN──► Coordinator                Client ──BEGIN──► Coordinator
                          │                                            │
   Client ──WRITE──►      │                     Client ──WRITE──►      │
                          │ writes to ranges                           │ writes to ranges +
                          │ (1 RTT)                                    │ STAGING transaction record
                          │                                            │ in PARALLEL (1 RTT)
   Client ──COMMIT──►     │                                            │
                          │ writes COMMIT      Client ──COMMIT──►      │
                          │ record (1 RTT)                             │ ack immediately
                          │                                            │ (transaction "implicitly committed"
   Client ◄──ack──        │                    Client ◄──ack──         │  if all writes succeeded)
                                                                       │
                                                                       │ Async: rewrite txn record
                                                                       │ from STAGING to COMMITTED
```

**The trick**: CockroachDB writes the transaction record in `STAGING` state in parallel with the data writes. If a reader encounters a `STAGING` record, it checks whether all the listed writes succeeded; if yes, the transaction is "implicitly committed" and the reader helps finalize it. This eliminates the synchronous COMMIT round-trip from the critical path.

### 4.6 FoundationDB's Pipeline

FoundationDB (acquired by Apple, open-sourced 2018) uses a fundamentally different architecture: a single logical sequencer with horizontally scalable proxies and storage.

```
FDB TRANSACTION PIPELINE

  CLIENT
     │
     │ 1. Get read version (from a Proxy, which gets it from the Sequencer)
     ▼
  PROXY  ◄── Sequencer (single global clock; thousands per second batched)
     │
     │ 2. Reads go directly to Storage Servers at read version
     ▼
  STORAGE
     │
     │ 3. Writes are buffered locally on the client
     │
     │ 4. COMMIT: client sends entire write set + read version to Proxy
     ▼
  PROXY → Resolver(s): check no committed writes overlap our read set
                       between read_version and commit_version
        ↓ (if OK)
  Log Servers: persist commit (Paxos-replicated)
        ↓ (after persist)
  Storage Servers: tail the log and apply writes asynchronously

  GUARANTEES:
    Strict serializability via OCC (optimistic concurrency control)
    via the Resolver checking conflicts at commit time
    No locks held during transaction execution
    Throughput ~ proxies × resolvers × log servers
```

**FDB's wild design choice**: it has a *single sequencer* and a *single set of resolvers*, but they're stateless and run in microseconds. The sequencer can issue 1M+ versions/sec. The bottleneck moves to the resolvers and log servers, both of which scale horizontally by partitioning the key space.

**Used as a building block by:**
- **Snowflake**: metadata service
- **Apple iCloud**: backing store for many services
- **Wavefront, Tigris, Tile38**, others
- **Tigris**: open-source alternative to DynamoDB on FDB
- **Apple's CloudKit**

### 4.7 Comparison: Distributed Commit Approaches

| Approach | Latency | Throughput | Coord. Failure | Used By |
|---|---|---|---|---|
| Vanilla 2PC | 2+ RTTs, blocking | Limited by coord. | Blocks holding locks | Almost nobody (alone) |
| 2PC + Paxos per participant | 2 RTTs + commit wait | High (per-shard) | Recovers in seconds | Spanner |
| Parallel Commit | ~1 RTT | High | Same as Spanner | CockroachDB |
| Percolator | 2 RPCs/row + oracle | Limited by oracle | Self-recovering | TiDB, YugabyteDB |
| Calvin | 1 RTT to sequencer | Very high (batched) | Sequencer is replicated | FaunaDB |
| FDB Pipeline | 2 RTTs (read+commit) | Very high | All tiers replicated | FoundationDB |
| Aurora DSQL | OCC + ClockBound | High (no locks) | Stateless compute | DSQL |
| Saga (compensation) | Async | Highest | Application-managed | Microservice patterns |

---

## 5. Distributed Concurrency Control

### 5.1 Pessimistic vs Optimistic in Distributed Systems

Single-node tradeoffs (file 05) get amplified in distributed systems:

```
PESSIMISTIC (locking):
  ─ Locks acquired EARLY → high contention with cross-shard txns
  ─ Distributed deadlock detection is expensive (cycle detection across nodes)
  ─ Lease-based locks (e.g., Spanner) tie locks to leader leases
  ─ Holds locks across network round-trips (slow!)

  WHEN GOOD: high-contention workloads, large transactions, write-heavy

OPTIMISTIC (OCC, validate at commit):
  ─ No locks during execution → no blocking
  ─ Aborts under contention → can starve under high write conflict
  ─ Read set must be tracked → memory cost per long-running tx
  ─ Validation requires comparing against committed writes

  WHEN GOOD: low-contention, short txns, read-heavy

HYBRID:
  Spanner: pessimistic for writes, snapshot reads from any replica
  CockroachDB: optimistic by default, can fall back to lock-based with
               SELECT FOR UPDATE
  FoundationDB: pure OCC
  Aurora DSQL: pure OCC + ClockBound
```

### 5.2 Distributed MVCC

Most modern distributed SQL systems use MVCC where versions are tagged with HLC or TrueTime timestamps and stored alongside the data.

```
DISTRIBUTED MVCC (CockroachDB / YugabyteDB style)

  Key K stores multiple versions:
    K @ ts5 = "v3" (committed)
    K @ ts3 = "v2" (committed)
    K @ ts1 = "v1" (committed)
    K @ ts7 = INTENT (provisional, written by tx T7)

  READ at ts4:
    Skip ts5, ts7 (newer); read ts3 → "v2"
    No locks needed for reads.

  READ at ts8 encounters intent at ts7:
    Resolve: contact transaction T7's coordinator.
    If T7 committed → use intent; if aborted → skip; if pending → wait or push.

  GARBAGE COLLECTION:
    Older versions removed once no transaction can read them.
    GC threshold = max transaction duration + safety margin.
    In CockroachDB, default 25h. Transactions running longer ABORT.
```

**Storage cost**: keeping versions inflates storage. CockroachDB stores versions inline with the key in RocksDB; YugabyteDB does similar in DocDB. Aurora DSQL uses a different approach (no in-place MVCC; transaction log is the source of truth).

### 5.3 Distributed Deadlock Detection

In a single-node DB, deadlocks are detected via a wait-for graph maintained by the lock manager. In a distributed DB, the wait-for graph spans nodes.

**Approaches:**

1. **Centralized cycle detector**: every node ships its local wait-for edges to a single detector. Simple but a SPOF.
2. **Distributed cycle detection (Chandy-Misra-Haas)**: probe-based. Doesn't scale beyond medium clusters.
3. **Timeout-based**: just abort transactions that wait too long. Pragmatic; what most systems do.
4. **Wound-wait / Wait-die (Spanner uses wound-wait)**:
   - Each transaction has a timestamp.
   - **Wound-wait**: older tx wounds (aborts) younger tx that holds a lock it wants.
   - **Wait-die**: younger tx waits for older; older tx dies (aborts) rather than wait.
   - Both prevent deadlock entirely by using the timestamp ordering as a tiebreaker.

```
WOUND-WAIT EXAMPLE (Spanner)

  Tx_A (ts=100) wants lock held by Tx_B (ts=200)
    → A is older → A wounds B → B aborts and retries
    → A acquires lock → no deadlock possible

  Tx_C (ts=300) wants lock held by Tx_A (ts=100)
    → C is younger → C waits → still no deadlock
```

### 5.4 Snapshot Isolation in Distributed Systems

Distributed snapshot isolation (SI) and serializable snapshot isolation (SSI) are subtle. The key challenge: ensuring the snapshot timestamp `s` reflects a *consistent* point in time across all shards.

```
DISTRIBUTED SI WITH HLC (CockroachDB-style)

  1. BEGIN: tx gets read_ts from coordinator's HLC
  2. READS: every shard read at read_ts
     - If a shard has not yet observed read_ts (its HLC is behind),
       it must wait until its HLC catches up before serving the read.
     - This is "uncertainty interval" handling.
  3. WRITES: each write is provisional (intent) until commit
  4. COMMIT: assign commit_ts; check no conflicts with our reads
     - For SSI, also check no read-write antiphase (skew) cycles

  UNCERTAINTY INTERVAL:
    Because clocks aren't perfectly synced, a write made on node X at "time t"
    could appear to node Y as "time t-5ms" (Y's clock lags by 5ms).
    Reads must conservatively widen their interval to cover this skew.
    Otherwise: missed writes appear as "newer" and break linearizability.

    CockroachDB: uses max_offset (default 500ms) as the uncertainty width.
    A read at t can encounter writes in [t, t+max_offset]; these force a
    read timestamp push and possible retry.
```

### 5.5 Read Committed (Distributed)

The default for many traditional DBs but historically tricky to implement well in distributed systems. Recent systems (CockroachDB 23.x, MongoDB) added it for compatibility and reduced contention. The key challenge: per-statement read snapshots vs per-transaction; and ensuring the per-statement snapshot is consistent across shards.

```
DISTRIBUTED READ COMMITTED:
  Each statement gets a fresh read timestamp.
  Within a statement, reads see a consistent snapshot.
  Between statements, you can see new committed data.

  PROS: less write-write conflict abort; matches Postgres default
  CONS: phantoms within a tx; lost updates without explicit FOR UPDATE
```

---

## 6. Replication Patterns Beyond Leader-Follower

File 12 covers leader-follower in depth. Here we focus on patterns that file doesn't cover.

### 6.1 Chain Replication

Used by Microsoft Azure Storage, several internal systems, and Hibari. Replicas are arranged in a chain; writes flow head-to-tail; reads are served by the tail.

```
CHAIN REPLICATION

  Write ──► HEAD ──► MIDDLE ──► TAIL ──► ack to client
  Read ─────────────────────────► TAIL ──► response

  PROPERTIES:
   ─ Strong consistency: tail is always up-to-date
   ─ Read throughput limited to one node (the tail)
   ─ Failure handling:
     * Head fails: middle becomes head
     * Tail fails: previous node becomes tail
     * Middle fails: skip it; replicate to next node
   ─ Recovery requires reliable failure detector

  CRAQ (Chain Replication with Apportioned Queries):
    Allow reads from any node, but if the node has uncommitted writes,
    it asks the tail for the latest committed version. Combines high
    read throughput with strong consistency.
```

### 6.2 Quorum Replication (Dynamo-style)

```
QUORUM REPLICATION

  N = total replicas
  R = read quorum (number of replicas that must respond to a read)
  W = write quorum (number of replicas that must ack a write)

  STRONG CONSISTENCY: R + W > N
  EVENTUAL CONSISTENCY: R + W ≤ N

  Common configurations:
    N=3, R=2, W=2  → R+W=4 > N=3, strong consistency
    N=3, R=1, W=1  → R+W=2 ≤ N=3, fast but eventual
    N=5, R=3, W=3  → R+W=6 > N=5, tolerates 2 failures + strong

  SLOPPY QUORUM (Dynamo):
    If preferred nodes for a key are down, write to OTHER nodes temporarily
    (hinted handoff). Sacrifices strict quorum for higher availability.
    Risk: writes succeed without reaching the "right" nodes; reads might
    not see them until handoff completes.
```

### 6.3 Multi-Leader (Active-Active)

Multiple nodes accept writes for the same data. Conflict resolution required.

```
MULTI-LEADER PATTERNS

  STAR / HUB-AND-SPOKE:
    All leaders sync via a central hub. Hub is SPOF.

  ALL-TO-ALL (mesh):
    Every leader replicates to every other. Conflict resolution everywhere.
    Common in geo-distributed Active-Active (e.g., MySQL Group Replication,
    BDR for Postgres, Cosmos DB multi-region writes).

  RING:
    Leaders form a ring, replication flows around the ring.
    Latency = N hops; rare in modern systems.

  PER-RECORD LEADERSHIP:
    Each record has a designated leader; writes route there.
    Effectively single-leader per key; conflict-free.
    Used by CockroachDB (leaseholders), Spanner (Paxos leaders),
    Cassandra (per-key coordinator with LWW).
```

**Multi-leader's defining problem: write conflicts.**

```
CONFLICT EXAMPLE

  At leader A:           At leader B (concurrent):
    UPDATE accts          UPDATE accts
    SET balance = 100     SET balance = 50
    WHERE id = 1;         WHERE id = 1;

  After replication, both leaders see both updates.
  Which value wins?

  Resolution strategies:
    - LWW (Last-Write-Wins): use timestamp; latest wins. Data loss possible.
    - Application-defined: surface conflict to app for resolution.
    - CRDT: structure data so concurrent ops commute.
    - Reject writes that would conflict (effectively single-leader).
```

### 6.4 Leaderless (Dynamo/Cassandra)

Any node can accept any write. Coordinator picks N replicas; sends write to W of them. Reads ask R replicas and merge results.

```
LEADERLESS WRITE

  Client ──► Coordinator (any node)
                │
                ├──► Replica 1 (one of N preferred for this key)
                ├──► Replica 2
                ├──► Replica 3
                │
                Wait for W acks
                │
                Return success to client

  Conflict resolution:
    - LWW (Cassandra): per-cell timestamps, latest wins
    - Vector clocks (Riak, Dynamo): preserve siblings, app resolves
    - CRDTs: merge automatically

  READ REPAIR:
    During a read, if replicas disagree, write the merged value back
    to stale replicas before returning to client.

  ANTI-ENTROPY:
    Periodic background process to reconcile divergent replicas
    (Merkle trees, see Section 8).
```

### 6.5 Per-Range / Per-Shard Consensus

The dominant pattern in modern distributed SQL: **each shard runs its own consensus group**. The cluster has thousands of independent Raft/Paxos groups.

```
PER-RANGE CONSENSUS (CockroachDB, Spanner, TiDB)

  Cluster of 9 nodes, key space split into 1000 ranges.
  Each range is replicated 3 ways (3 of the 9 nodes hold each range).

  Each range is a Raft group:
    - Has its own leader (called "leaseholder" in CRDB)
    - Has its own log
    - Heartbeats happen between range replicas (not whole cluster)

  ─ Tens of thousands of Raft groups per cluster: how to scale?
    Solution: COALESCED HEARTBEATS — pack heartbeats for all groups
    on a node into a single message. One Cockroach node can be in
    50,000+ Raft groups efficiently.

  ─ Splits and merges:
    When a range grows past size threshold, split into two.
    Each half becomes its own Raft group.
    Range metadata is itself a Raft-replicated table (recursive!).
    The "meta1" range bootstraps the cluster.
```

This pattern lets the cluster scale to PBs while keeping per-tx latency low (only the relevant range's leader is contacted).

---

## 7. Conflict Resolution and CRDTs

### 7.1 The Conflict Problem

Whenever two replicas accept independent writes to the same key, they will eventually see each other's writes and must reconcile. There are three approaches:

1. **Prevent conflicts** (single-leader, per-key leadership): no conflicts arise.
2. **Detect and resolve** (vector clocks + application logic, LWW): conflicts arise but get resolved.
3. **Make conflicts impossible** (CRDTs): concurrent operations commute.

### 7.2 Last-Write-Wins (LWW)

The simplest. Each value has a timestamp; the latest wins. Used by Cassandra, DynamoDB (with conditions), many caches.

```
LWW SEMANTICS

  Replica A: SET x = "alice" @ ts=100
  Replica B: SET x = "bob" @ ts=99

  After reconciliation, both replicas: x = "alice" (ts=100 wins)

  PROBLEMS:
    1. Lost writes: bob's write is silently discarded
    2. Clock skew: if A's clock is ahead, A always "wins"
    3. Concurrent writes to different fields are conflated:
       Replica A: SET name = "alice" @ ts=100
       Replica B: SET email = "bob@example.com" @ ts=101
       After LWW per row: only B's write survives → "name" lost

  WORKAROUNDS:
    - Per-cell LWW (Cassandra): each column has its own timestamp
    - Use HLCs to bound clock skew impact
    - CRDTs for richer semantics
```

### 7.3 CRDTs (Conflict-Free Replicated Data Types)

CRDTs are data structures designed so that any two replicas, when they see the same set of operations (in any order), converge to the same state.

**Two flavors:**

```
STATE-BASED CRDT (CvRDT):
  Replicas exchange entire state.
  Merge function must be:
    - Commutative: merge(a, b) = merge(b, a)
    - Associative: merge(merge(a, b), c) = merge(a, merge(b, c))
    - Idempotent: merge(a, a) = a

OPERATION-BASED CRDT (CmRDT):
  Replicas exchange operations.
  Operations must commute: applying ops in any order yields same state.
  Requires reliable causal broadcast.

DELTA-CRDT: hybrid that ships deltas of state changes (more efficient).
```

**Common CRDTs:**

```
G-COUNTER (grow-only counter):
  State: { node_id: count } per replica
  Increment: increment local node's count
  Value: sum of all counts
  Merge: per-key max
  Use: page view counters, like counts

PN-COUNTER (positive-negative):
  Two G-counters: one for increments, one for decrements
  Value: sum(P) - sum(N)
  Use: shopping cart item counts, votes

G-SET (grow-only set):
  Add: insert element
  Merge: union
  Use: "users who ever liked this", append-only audit logs

OR-SET (Observed-Remove set):
  Each element has unique tag(s); add/remove tracks tags
  Merge: union of (element, tag) pairs; remove tag wins only if observed
  Solves "concurrent add/remove" without losing the add

LWW-REGISTER:
  Single value with timestamp; merge picks latest
  Use: simple key-value with eventual consistency

RGA / TREEDOC / LOGOOT:
  Sequence CRDTs for collaborative text editing
  Use: Google Docs, Figma, real-time collaboration

JSON CRDT (Automerge, Yjs):
  Recursive CRDT for arbitrary JSON
  Use: local-first apps, offline-first mobile
```

### 7.4 Where CRDTs Are Used in Databases

| System | CRDT Use |
|---|---|
| Riak | OR-Set, PN-Counter, LWW-Register, Map (composition) |
| Redis Enterprise (Active-Active) | CRDB based on G-counter, PN-counter, registers, sets |
| AntidoteDB | Research database; full CRDT primitives + CC consistency |
| Cosmos DB | Optional CRDT-based merge for multi-region writes |
| MongoDB Atlas (mobile sync via Realm) | CRDT-based conflict resolution for edge devices |
| ScyllaDB | LWW per cell (degenerate CRDT) |
| YDB | CRDT-style counters for some metrics tables |

### 7.5 The Practical Limits of CRDTs

CRDTs are not magic:

- **Constraints don't compose**: a CRDT can't enforce "balance >= 0" without coordination. Concurrent valid withdrawals can drive balance negative.
- **Memory cost**: many CRDTs grow with the number of distinct operations or replicas. OR-Sets grow with the number of removes ever performed. Tombstone GC is delicate.
- **Causal delivery requirements**: most operation-based CRDTs require reliable causal broadcast, which is itself a hard problem.
- **Convergence ≠ correctness**: replicas converge, but the converged state may not match user expectations. ("Add to cart" + "remove from cart" → ?)

**Modern compromise**: many systems use CRDTs for *some* data types (counters, sets) while keeping consensus for transactional state.

---

## 8. Anti-Entropy and Repair

In an AP system, replicas diverge. Background processes reconcile them. Two main mechanisms: **read repair** (synchronous, on the read path) and **anti-entropy** (asynchronous, background).

### 8.1 Merkle Trees for Anti-Entropy

```
MERKLE TREE STRUCTURE

                  H_root = hash(H_left, H_right)
                  /                       \
        H_left = hash(H_LL, H_LR)    H_right = hash(H_RL, H_RR)
        /              \                  /              \
     H_LL          H_LR              H_RL              H_RR
   (hash of    (hash of           (hash of           (hash of
   range 0-25%) range 25-50%)      range 50-75%)      range 75-100%)

ANTI-ENTROPY EXCHANGE:

  Replica 1 ──► Replica 2: "Send me your root hash"
  R2 ──► R1:  H_root_R2

  if H_root_R1 == H_root_R2: done (no divergence)
  else: descend the tree
        R1 ──► R2: "Send children of root"
        compare child hashes; descend into mismatched subtrees

  At leaves: identify exactly which keys differ → exchange just those.
  Network cost: O(log n) for divergence-free, O(d log n) for d differences.
```

**Used by:**
- **Cassandra**: nodetool repair builds Merkle trees and exchanges with replicas.
- **DynamoDB**: internal anti-entropy.
- **Riak**: AAE (Active Anti-Entropy).
- **Many object stores** (e.g., Ceph for scrub).

### 8.2 Read Repair

```
READ REPAIR FLOW

  Client ──► Coordinator: GET key K
                │
                ├──► Replica 1: returns value V1 @ ts=100
                ├──► Replica 2: returns value V2 @ ts=99   (stale)
                └──► Replica 3: returns value V1 @ ts=100
                │
                │ Coordinator picks V1 (latest timestamp)
                │ Sends V1 to Replica 2 (write-back)
                │
                ▼
            Returns V1 to client

  TYPES:
    BLOCKING: wait for repair before returning to client (slower, safer)
    ASYNC: return to client immediately, repair in background (faster, brief inconsistency)

  TUNABLE: read_repair_chance in Cassandra (probability of repair per read)
```

### 8.3 Hinted Handoff

```
HINTED HANDOFF

  Replica A is supposed to hold key K but is down.
  Coordinator writes the value to Replica X (a neighbor) with a "hint":
    "deliver to A when it comes back"

  When A recovers, X replays the hint to A.

  RISKS:
    - If X also crashes before delivering, write may be lost
    - Hints can pile up during long outages → memory/disk pressure
    - "Hinted handoff window" limits how long hints are kept

  Cassandra: max_hint_window_in_ms (default 3 hours)
  After window expires: anti-entropy must clean up
```

### 8.4 Sloppy Quorums

Strict quorum requires writing to W of the *N preferred replicas*. Sloppy quorum allows writing to W of *any* nodes when preferred nodes are down.

```
STRICT vs SLOPPY QUORUM

  Preferred replicas for key K: {A, B, C}
  N=3, W=2

  Scenario: A and B are partitioned away.

  STRICT QUORUM:
    Write fails (cannot reach W=2 of {A,B,C})
    → CP behavior

  SLOPPY QUORUM:
    Coordinator picks {C, D, E} (D, E are not preferred but available)
    Write succeeds (W=2 reached: C and D ack)
    Hinted handoff: D and E hold writes until A and B recover
    → AP behavior; reads may not see writes until handoff completes
```

Trade-off: sloppy quorum keeps writes available during partitions but breaks the R+W>N consistency guarantee. Cassandra exposes this choice; DynamoDB hides it.

---

## 9. Membership and Cluster Management

How does the cluster track which nodes are alive, which ranges live where, and how to route requests? File 16 covers failure detection algorithms; this section focuses on cluster-wide coordination.

### 9.1 Centralized Coordination (ZooKeeper / etcd Pattern)

```
CENTRALIZED MEMBERSHIP

   ┌───────────┐  ┌───────────┐  ┌───────────┐
   │  Node A   │  │  Node B   │  │  Node C   │
   └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
         │              │              │
         └──────────────┼──────────────┘
                        │ heartbeats / leases
                        ▼
              ┌───────────────────┐
              │   Coordination    │ ◄── Raft-replicated
              │   Service         │     (e.g., 5-node etcd)
              │ (etcd/ZK/Consul)  │
              └───────────────────┘
                        │
                        │ watches / queries
                        ▼
                 (clients learn topology)

  USE CASES:
    Kafka: ZooKeeper (legacy) or KRaft (Raft-based, replaces ZK)
    HBase: ZooKeeper for region server registry
    Kubernetes: etcd for all cluster state
    HDFS: ZooKeeper for NameNode HA
```

**Trade-offs**: simple to reason about; coordination service is a hard dependency; etcd/ZK have practical scale ceilings (~thousands of watchers, ~10k writes/s).

### 9.2 Gossip-Based Membership (SWIM)

For very large clusters (10k+ nodes), centralized coordination breaks down. Gossip protocols scale to millions of nodes by having each node only talk to a few others, but information propagates exponentially.

```
SWIM PROTOCOL (Scalable Weakly-consistent Infection-style Membership)

  Each node, every T seconds:
    1. Pick a random other node X; send PING
    2. If X responds within timeout: X is alive, gossip update
    3. If X does not respond:
       a. Pick K other nodes; ask each to PING X on our behalf (indirect probe)
       b. If any get a response: X is alive
       c. If none: X is "suspected"
    4. Suspected nodes are gossiped about
    5. After confirmation timeout: suspected → confirmed dead

  GOSSIP:
    Every PING/ACK piggybacks recent membership changes
    Information spreads in O(log N) rounds across the cluster

  USED BY:
    Hashicorp Serf (and thus Consul)
    Cassandra (variant)
    ScyllaDB
    Many internal Uber/Twitter services
```

**Why gossip wins at scale**: a 10k-node cluster with centralized heartbeats means 10k nodes pinging a coordinator every second. With SWIM, each node makes ~1 ping/sec regardless of cluster size.

### 9.3 Consistent Hashing and Virtual Nodes

```
CONSISTENT HASHING

  Map both nodes and keys onto a ring (e.g., 2^64 positions).
  Each key is owned by the first node clockwise on the ring.

           Node A (pos 0x2000...)
              ▲
              │
     Node D ──┼── Node B (pos 0x6000...)
   (0xE000)   │
              │
           Node C (pos 0xA000...)

  Key K at hash 0x4000 → owned by Node B (first node clockwise)

  PROBLEM: with few nodes, distribution is uneven (some nodes hold more keys).
  SOLUTION: virtual nodes (vnodes).
    Each physical node owns N virtual positions on the ring.
    Distribution becomes uniform; rebalancing is incremental.

  Cassandra: ~256 vnodes per node (configurable num_tokens)
  Riak: 64 partitions per node (typical)
  DynamoDB: hidden, but uses consistent hashing internally
  Memcached: classic consistent hashing client-side

  ADVANTAGES:
    Adding/removing a node only moves 1/N of keys
    No global re-partitioning event
    Hotspots easier to mitigate (move some vnodes)
```

### 9.4 Range-Based Sharding with Coordination

Modern distributed SQL systems (CockroachDB, Spanner, TiDB) use **range-based sharding** with a separate metadata layer that tracks ranges.

```
RANGE-BASED SHARDING (CockroachDB)

  Key space (sorted): a ─── m ─── z
  Ranges:           [a, e)  [e, m)  [m, t)  [t, z]
                      r1     r2      r3      r4

  Each range:
    - Has 3 (or 5) replicas across the cluster
    - Has a Raft group, with one replica as leaseholder
    - Is split when it grows past a threshold (default 512MB)
    - Is merged when adjacent ranges shrink (under 32MB combined)

  RANGE METADATA:
    Stored in special tables (meta1, meta2) that are themselves ranges.
    meta1 → meta2 → data
    Bootstrapping: every node knows where meta1 starts.

  RANGE LEASE:
    A leaseholder is the only replica that can serve consistent reads
    without coordinating with other replicas. Held for ~6 seconds.
    Lease must be renewed before expiration.

  REBALANCING:
    A separate "allocator" decides when to move ranges between nodes:
    - Diversify across failure domains (rack/zone/region)
    - Balance load (# of ranges, byte size, QPS)
    - React to node addition/removal/failure
```

### 9.5 The Metadata Service Bottleneck

Every distributed DB has a metadata service (range map, shard location, schema). If the metadata service is itself centralized, it becomes a bottleneck.

**Solutions:**
- **TiDB's PD (Placement Driver)**: standalone Raft cluster handling all metadata
- **CockroachDB**: metadata is itself ranges, stored in the cluster
- **Spanner**: zone master + Universe Master (metadata of metadata)
- **FoundationDB**: cluster controller is replicated; coordinates via Paxos

---

## 10. Distributed Schema Changes

Schema changes are surprisingly hard in distributed systems: every node must agree on the schema at every point in time, but they can't all switch at the same instant.

### 10.1 The Naïve Problem

```
NAÏVE SCHEMA CHANGE

  All nodes must apply ALTER TABLE Foo ADD COLUMN bar INT.

  PROBLEM:
    Some nodes have applied; others haven't.
    Node A (new schema) writes a row with bar=5.
    Node B (old schema) reads it: doesn't know about bar.
    Node A reads a row written by B: "missing" bar column.

  RESULT: data corruption / undefined behavior.
```

### 10.2 The F1 Schema Change Protocol

Google's F1 paper (2013) introduced a multi-state schema change protocol that lets the cluster transition smoothly without taking writes offline.

```
F1 SCHEMA CHANGE STATES

  ABSENT:
    Schema element does not exist. Nobody can use it.

  DELETE-ONLY:
    Element exists. Reads/writes ignore it; deletes process it.
    Why: to allow a future state to safely remove old index entries.

  WRITE-ONLY:
    Element exists. Writes update it; reads ignore it.
    Why: build up consistent state before exposing to readers.

  PUBLIC:
    Element fully usable; reads and writes both honor it.

  TRANSITIONS:
    ABSENT → DELETE-ONLY → WRITE-ONLY → (backfill data) → PUBLIC

  KEY INVARIANT:
    Adjacent states are SAFE TO COEXIST.
    At any moment, some nodes may be on state N and others on state N+1.
    The transition only proceeds when all nodes have advanced.

  LEASE-BASED ENFORCEMENT:
    Schema versions have leases (e.g., 60 seconds).
    A node renewing its lease commits to the current version.
    A schema change waits 1 lease period between transitions to ensure
    all nodes have caught up.
```

This protocol (or variants) is used by **CockroachDB**, **TiDB**, **YugabyteDB**, and others. It's why these systems can do `ALTER TABLE ADD COLUMN` online without taking the table offline.

### 10.3 Adding Indexes Online

```
ONLINE INDEX BUILD

  1. Add index in DELETE-ONLY state.
     - All nodes know about the index.
     - Inserts/updates do NOT write to it.
     - Deletes DO process it (in case of partial state).

  2. Transition to WRITE-ONLY.
     - All new writes go to the index.
     - Reads still don't use the index.

  3. BACKFILL: scan the entire table, write existing rows to the index.
     - Done in batches; throttled to avoid overload.
     - Conflicts with concurrent writes resolved via MVCC.

  4. Transition to PUBLIC.
     - Reads now use the index.

  TYPICAL TIMELINE for a 100GB table: hours.
  No downtime; small write amplification during backfill.
```

### 10.4 Type Changes and Renames

These are hard. Most distributed SQL systems either:
- Disallow direct type changes (you must add a new column, copy, drop, rename)
- Restrict to "compatible" type changes (int32 → int64) with explicit verification
- Provide async tools to batch-rewrite (CockroachDB's `ALTER COLUMN ... TYPE ...`)

---

## 11. Change Data Capture and Streaming

Modern distributed systems are rarely the source of truth alone — they feed analytics, search indexes, caches, microservices. CDC is how data flows out.

### 11.1 The CDC Patterns

```
CDC PATTERN COMPARISON

  TRIGGER-BASED:
    DB triggers write to a side table; consumer polls.
    Downsides: write amplification, schema coupling.
    Use: legacy systems with no other option.

  TIMESTAMP POLLING:
    Consumer queries WHERE updated_at > last_seen_ts.
    Downsides: misses deletes; clock skew; ignores intermediate values.
    Use: simple ETL with daily/hourly batches.

  LOG-BASED:
    Consumer tails the database's WAL/binlog/changefeed.
    Provides every change in commit order; captures deletes; no app changes.
    Use: production CDC pipelines (Debezium, Kafka Connect, native).

  OUTBOX PATTERN:
    Application writes business state + outbox row in same tx.
    Separate process reads outbox and publishes to message bus.
    Use: when you need exactly-once delivery to external systems.
```

### 11.2 CDC in Distributed Databases

```
CDC IMPLEMENTATIONS

  PostgreSQL: logical replication (wal2json, pgoutput, Debezium).
    Single WAL stream. Easy because single writer.

  MySQL: binlog (row format).
    Replicas tail; CDC tools tail binlog.

  CockroachDB: CHANGEFEED.
    Per-range changefeeds, merged into a single ordered stream
    via "resolved timestamps" — every range periodically emits
    "I am up to date through ts T", and the changefeed knows it can
    advance the watermark when all ranges have reported >= T.

  Spanner: Change Streams (since 2022).
    Similar resolved-timestamp pattern.

  TiDB: TiCDC.
    Pulls from TiKV's Raft logs; outputs to Kafka, Pulsar, MySQL, etc.

  DynamoDB: Streams.
    24-hour ordered change log per shard; consumers read via shard iterators.

  MongoDB: Change Streams.
    Tails the oplog (operation log) of the replica set.

  Cassandra: CDC table feature, plus CDC log.
    Per-replica logs; deduplication is the consumer's problem.
```

### 11.3 The Resolved Timestamp Pattern

For distributed databases, ordering changes across shards is the central challenge.

```
RESOLVED TIMESTAMP MECHANIC

  Range R1 emits changes:
    @ ts=100: UPDATE A
    @ ts=105: INSERT B
    "RESOLVED: 110" ← (means: no future change will have ts < 110)

  Range R2 emits changes:
    @ ts=102: UPDATE C
    "RESOLVED: 108"

  CONSUMER:
    Knows global watermark = min(R1's resolved, R2's resolved) = 108
    Can safely emit all changes ≤ 108 in timestamp order
    Must hold back R1's @110 until R2 also resolves past 110

  WHY THIS MATTERS:
    Consumers can do "exactly-once" or "consistent snapshot" processing
    by reading up to the watermark, then advancing.
    Without resolved timestamps, you'd see commits out of order or
    have to wait indefinitely.
```

---

## 12. Multi-Region and Geo-Distribution

The hardest mode for distributed databases. Latency between regions (50-300ms) makes synchronous coordination expensive. Different systems make very different trade-offs.

### 12.1 The Latency Reality

```
INTER-REGION LATENCIES (one-way, typical)

  Same AZ (within DC):           <1 ms
  Cross-AZ same region:           1-2 ms
  US East ↔ US West:             ~30 ms
  US East ↔ EU West:             ~40 ms
  US East ↔ AP Singapore:        ~100 ms
  US East ↔ AP Tokyo:            ~70 ms
  Antipodes (e.g., NYC ↔ Perth): ~150-200 ms

  Consensus across 3 regions: 1 RTT to nearest majority.
  US-east + US-west + EU: write commit ~ 60-80 ms (US-E + US-W majority).
  Add a write to a globally-replicated row: typically ~100ms+.
```

### 12.2 Multi-Region Patterns

```
MULTI-REGION PATTERNS

  ACTIVE-PASSIVE:
    Primary in one region; standby in another.
    Async replication; failover is manual or controlled.
    RPO > 0 (data loss possible).
    Examples: most traditional Postgres/MySQL multi-region setups.

  ACTIVE-ACTIVE WITH GLOBAL WRITES:
    Every region accepts writes for any data.
    Synchronous global consensus → high write latency.
    Examples: Spanner (default), CockroachDB GLOBAL tables.

  ACTIVE-ACTIVE WITH REGIONAL OWNERSHIP:
    Each row has a "home region" with a local leader.
    Writes from home region: fast.
    Writes from elsewhere: slow (proxy to home).
    Examples: CockroachDB REGIONAL BY ROW, Spanner default for sharded tables.

  ACTIVE-ACTIVE WITH ASYNC REPLICATION:
    Writes accepted in any region, replicated async.
    Conflicts resolved via LWW or CRDTs.
    Examples: DynamoDB Global Tables, Cosmos DB multi-master, Cassandra.

  FOLLOWER READS:
    Reads served from local follower at slightly stale timestamp
    (e.g., 5s ago). Avoids cross-region read latency.
    Requires bounded staleness model.
    Examples: CockroachDB AS OF SYSTEM TIME, Spanner stale reads.
```

### 12.3 CockroachDB's Multi-Region Primitives

CockroachDB exposes the placement decisions as SQL, which is unique in its detail.

```sql
-- DATABASE LEVEL
ALTER DATABASE app PRIMARY REGION "us-east1";
ALTER DATABASE app ADD REGION "us-west1";
ALTER DATABASE app ADD REGION "europe-west1";

-- TABLE LEVEL
ALTER TABLE users SET LOCALITY REGIONAL BY ROW;
   -- each row has a "crdb_region" column; row lives near its region
   -- writes from that region are fast; cross-region writes slow

ALTER TABLE products SET LOCALITY GLOBAL;
   -- writable from any region with low READ latency from anywhere
   -- writes are slow (cross-region consensus); reads are local
   -- great for read-heavy reference data

ALTER TABLE orders SET LOCALITY REGIONAL BY TABLE IN "us-east1";
   -- entire table lives in us-east1; reads/writes from there are fast
   -- reads from us-west1 must hit us-east1
```

This level of explicit control is rare. Spanner offers similar via "placement IDs". Most systems give you only a coarse "where should I replicate" knob.

### 12.4 Bounded Staleness and Follower Reads

```
BOUNDED-STALENESS READ

  Client: SELECT ... FROM users AS OF SYSTEM TIME '-5s'

  System: serve from any local replica that has applied data through (now - 5s)
  No coordination with leader; no waiting for replication lag.

  USE CASES:
    Dashboards / analytics queries
    Read-after-write where lag is acceptable
    Geo-distributed serving where strict freshness isn't needed

  TRADE-OFF:
    Inconsistent with the latest writes
    But VERY fast and locally servable
```

Spanner extended this idea: you can ask for a snapshot at any specific timestamp, and as long as it's recent enough that the data hasn't been GC'd, it's serviceable from any replica with no coordination.

---

## 13. Disaggregated Storage Architectures

Already introduced in Section 2.3. Here we go deeper into the variations.

### 13.1 Aurora's Storage Layer

```
AURORA STORAGE INTERNALS

  Each database "volume" is striped across many "Protection Groups" (PGs).
  Each PG = 6 storage nodes across 3 AZs (2 nodes per AZ).
  Volume = 100s of PGs (each PG is 10GB segment).

  WRITE PATH:
    Writer sends a redo log record (LR) tagged with LSN.
    Each PG receives the LR for its segment.
    LR is durable when 4 of 6 nodes ack (4/6 quorum).
    Writer can ack to client when ALL relevant PGs have ack'd.

  READ PATH:
    Reader requests a page at a specific LSN.
    Routes to the closest storage node for that PG.
    Storage node materializes the page from base + redo logs.

  PAGE GC:
    Storage continuously garbage-collects old log records once the
    page has been fully materialized.

  CRASH RECOVERY:
    Compute restart: connect to storage, get current LSN, resume.
    No redo replay on the compute side. Recovery in seconds.
```

### 13.2 Socrates (Azure SQL Hyperscale)

Adds a "Page Server" tier between log servers and durable storage. Each Page Server caches pages for a range of the database; the buffer pool of the compute layer hits the Page Servers, which hit cold storage as needed.

```
SOCRATES TIERS (4 tiers vs Aurora's 2)

  ┌──────────────────────────┐
  │   Compute (SQL)          │
  └────────────┬─────────────┘
               │
  ┌────────────▼─────────────┐
  │   XLOG (log service)     │  ← writes only (single landing zone)
  └────────────┬─────────────┘
               │
  ┌────────────▼─────────────┐
  │   Page Servers           │  ← page materialization + caching
  │   (sharded by page range)│
  └────────────┬─────────────┘
               │
  ┌────────────▼─────────────┐
  │   Azure Storage (XStore) │  ← durable cold storage
  └──────────────────────────┘

  ADVANTAGE OVER AURORA:
    DB size scales to 100+ TB (Aurora caps near 128TB).
    Page Servers can be added independently of compute.
```

### 13.3 Neon (Open-source disaggregated Postgres)

```
NEON ARCHITECTURE

  COMPUTE (stateless Postgres)
    │ writes: WAL records
    ▼
  SAFEKEEPERS (Paxos-like quorum for WAL durability)
    │
    ▼
  PAGESERVER (page materialization; LSM-like layered storage)
    │
    ▼
  S3 (long-term storage; cold layers)

  KEY FEATURES:
    - Compute can be paused / resumed (cold-start ~5s)
    - Branches: copy-on-write at the Pageserver level (cheap clones)
    - PITR: any LSN within the retention window
    - Storage is multi-tenant; compute is per-customer
```

### 13.4 AlloyDB

GCP's disaggregated Postgres with two innovations:

1. **Columnar engine**: a separate columnar storage layer for analytic queries, populated from the row store.
2. **AI integration**: built-in vector search and embedding generation.

### 13.5 Aurora DSQL (the new model)

Released in late 2024, Aurora DSQL is the most ambitious disaggregated database yet:
- **Active-active multi-region writes**
- **Optimistic concurrency control** (no leases for writes)
- **ClockBound** (PTP) for ordering
- **Disaggregated storage** AND **disaggregated transaction processing**
- **Serverless**: no instance sizing

It's a glimpse of where the industry is headed: Spanner-class semantics with cloud-native operations.

---

## 14. NewSQL and Distributed SQL Systems

### 14.1 Spanner

```
SPANNER ARCHITECTURE (simplified)

  ┌──────────────────────────────────────────────────────────────┐
  │                       Spanner Universe                        │
  │  ┌────────────────────┐  ┌────────────────────┐              │
  │  │   Zone (DC)        │  │   Zone (DC)        │   ...        │
  │  │  ┌──────────────┐  │  │  ┌──────────────┐  │              │
  │  │  │ Spanservers  │  │  │  │ Spanservers  │  │              │
  │  │  │  (each owns  │  │  │  │              │  │              │
  │  │  │ ~100 tablets)│  │  │  │              │  │              │
  │  │  └──────────────┘  │  │  └──────────────┘  │              │
  │  │  Zone Master       │  │  Zone Master       │              │
  │  │  Location Proxy    │  │  Location Proxy    │              │
  │  └────────────────────┘  └────────────────────┘              │
  │              │                        │                       │
  │              └─────────┬──────────────┘                       │
  │                        │                                      │
  │              Universe Master                                  │
  │              Placement Driver                                 │
  └──────────────────────────────────────────────────────────────┘

  TABLET = unit of replication; Paxos group across zones.
  DIRECTORY = group of tablets that live together (data co-location).
  TRANSACTION = 2PC across tablet leaders + commit wait via TrueTime.
```

**Key claims:**
- External consistency (linearizable globally) via TrueTime
- 99.999% availability across many regions
- SQL with strong typing, transactions, and secondary indexes
- Horizontally scalable to PB-class

### 14.2 CockroachDB

```
COCKROACHDB LAYERED ARCHITECTURE

  ┌────────────────────────────────────────────────────┐
  │  SQL Layer                                          │
  │  - Parser, planner, optimizer                       │
  │  - Distributed execution (DistSQL)                  │
  └────────────────────────┬───────────────────────────┘
                           │
  ┌────────────────────────▼───────────────────────────┐
  │  Transaction Layer                                  │
  │  - HLC-based MVCC                                   │
  │  - Parallel commit                                  │
  │  - Lock table per range                             │
  └────────────────────────┬───────────────────────────┘
                           │
  ┌────────────────────────▼───────────────────────────┐
  │  Distribution Layer                                 │
  │  - Range cache; meta1/meta2 lookup                  │
  │  - DistSender (request routing)                     │
  └────────────────────────┬───────────────────────────┘
                           │
  ┌────────────────────────▼───────────────────────────┐
  │  Replication Layer                                  │
  │  - Per-range Raft groups                            │
  │  - Leaseholder per range                            │
  │  - Coalesced heartbeats                             │
  └────────────────────────┬───────────────────────────┘
                           │
  ┌────────────────────────▼───────────────────────────┐
  │  Storage Layer                                      │
  │  - Pebble (Go LSM, RocksDB-inspired)                │
  └────────────────────────────────────────────────────┘
```

**Differentiators:**
- Postgres wire protocol; familiar SQL
- HLC instead of TrueTime (no atomic clocks needed)
- Multi-region SQL with explicit placement DDL
- Open source (BSL → Enterprise license; recent move to ELv2)

### 14.3 TiDB / TiKV

```
TIDB ECOSYSTEM

  ┌────────┐    ┌────────┐    ┌────────┐
  │ TiDB   │    │ TiDB   │    │ TiDB   │  ← stateless SQL nodes
  │ Server │    │ Server │    │ Server │
  └────┬───┘    └────┬───┘    └────┬───┘
       │             │             │
       └─────────────┴─────┬───────┘
                           │
                ┌──────────▼─────────┐
                │   PD (Placement    │  ← Raft-replicated metadata
                │     Driver)        │     timestamp oracle, scheduler
                └──────────┬─────────┘
                           │
       ┌───────────────────┼───────────────────┐
       │                   │                   │
   ┌───▼───┐           ┌───▼───┐           ┌───▼───┐
   │ TiKV  │           │ TiKV  │           │ TiKV  │  ← KV storage
   │ Region│           │ Region│           │ Region│     (each region
   │ Raft  │           │ Raft  │           │ Raft  │     is a Raft group)
   └───────┘           └───────┘           └───────┘
                           │
                  ┌────────▼────────┐
                  │     TiFlash     │  ← columnar engine
                  │  (CDC from TiKV)│     for HTAP
                  └─────────────────┘
```

**Differentiators:**
- MySQL-compatible (not Postgres)
- Pure Percolator transaction model
- TiFlash columnar for HTAP
- PD as a centralized but Raft-replicated metadata service

### 14.4 YugabyteDB

```
YUGABYTEDB LAYERS

  Postgres (YSQL) or Cassandra-compatible (YCQL) front-end
            │
            ▼
  DocDB: distributed document store
  - Per-tablet Raft groups (same idea as Cockroach ranges)
  - HLC timestamps
  - Percolator-style writes (provisional records)
  - RocksDB underneath each tablet

  KEY DIFFERENCES FROM COCKROACH:
    - YSQL is Postgres CODE (not just protocol) — more compatibility
      but also inherits Postgres limitations (single-writer-per-tablet
      per-table because it reuses Postgres's transaction infrastructure)
    - DocDB layer can serve YSQL or YCQL (Cassandra) interfaces
    - Master service (like PD) for metadata
```

### 14.5 The Distributed SQL "Family Tree"

```
                     Spanner (2012, internal Google)
                          │
                          │ (ideas, not code)
                          ▼
  CockroachDB (2014) ──── F1 (Google, on Spanner)
       │                          │
       │                          ▼
       │                       Vitess (YouTube, then PlanetScale)
       │
       ▼
  YugabyteDB (2017)
  TiDB (2017, MySQL-flavored, by PingCAP)
  FaunaDB (2017, Calvin-based)
  OceanBase (Alibaba, 2010s)
  Aurora DSQL (2024, AWS)
```

---

## 15. Wide-Column and Key-Value at Scale

### 15.1 DynamoDB

```
DYNAMODB INTERNALS (high-level)

  Each table is hash-partitioned by partition key.
  Each partition has 3 replicas (default 3 AZs in a region).
  WRITE: routed to partition leader; replicated to 2 followers (W=2 of 3).
  READ:
    Eventually consistent (default): can read any replica
    Strongly consistent: must read leader

  AUTOSCALING:
    Partition is the unit of capacity; ~10 GB or 3000 RCU/1000 WCU.
    Splits when hot or full.
    Heat-based splitting (since 2018): splits busy keys.

  GLOBAL TABLES:
    Multi-region active-active.
    Async replication; LWW conflict resolution.
    Per-item version vectors internally.

  TRANSACTIONS:
    TransactWriteItems / TransactGetItems: 2PC across up to 100 items.
    Adapt: 2× the cost of a normal write.

  STREAMS:
    24-hour ordered change log per partition.
```

### 15.2 Cassandra / ScyllaDB

```
CASSANDRA WRITE PATH

  Client → Coordinator (any node)
              │
              ▼
       Token-based partitioning → identify replicas (RF nodes)
              │
              ▼
       Send write to all RF replicas; wait for W acks
              │
              ├─► Each replica:
              │   1. Append to commit log (durable)
              │   2. Update memtable
              │   3. Ack to coordinator
              │
              ▼
       Return success when W replicas have ack'd

  COMPACTION:
    Memtable flushes to SSTable; multiple SSTables per node.
    Background compaction merges SSTables (LSM-style).
    Strategies: Size-tiered, Leveled, Time-window.

  TUNABLE CONSISTENCY (per request):
    ONE, TWO, THREE, QUORUM, LOCAL_QUORUM, EACH_QUORUM, ALL
    Each impacts latency and consistency.
```

**ScyllaDB**: Cassandra-compatible rewrite in C++ with shard-per-core architecture (Seastar framework). 10x throughput, 1/10 nodes. Eliminates JVM and per-thread locks.

### 15.3 FoundationDB

Already covered in Section 4.6. Key insight: a *layered* approach where the core is a strict-serializable ordered KV, and higher-level data models (document, SQL, graph) are built as "layers" on top.

```
FDB LAYERED ARCHITECTURE

  ┌─────────────────────────────────────────┐
  │  Layers (Document, SQL, Index, etc.)    │ ← user code
  ├─────────────────────────────────────────┤
  │  FDB Client API                         │
  ├─────────────────────────────────────────┤
  │  FDB Core: ordered KV with ACID txns    │
  ├─────────────────────────────────────────┤
  │  Storage Servers / Log Servers / Proxies│
  └─────────────────────────────────────────┘
```

**Used by**: Snowflake (metadata), Apple (iCloud, CloudKit), JanusGraph, Tigris, etc.

### 15.4 BigTable / HBase

```
BIGTABLE / HBASE

  - Wide-column store on top of distributed file system (GFS / HDFS).
  - Single-row atomic operations (multi-row requires application coordination).
  - Each Tablet is owned by one Tablet Server at a time (no replication
    at the Tablet Server level — replication is in the FS).
  - Uses Chubby / ZooKeeper for tablet server coordination.
  - Schema-less columns; column families have schemas.

  USE CASES:
    Time-series, IoT, web index data, analytics with wide rows.
    Not for cross-row transactions.
```

---

## 16. Document and Multi-Model Distributed Databases

### 16.1 MongoDB

```
MONGODB ARCHITECTURE

  REPLICA SET (one shard):
    1 Primary, N Secondaries
    Primary takes all writes; replicates oplog
    Failover via Raft-like protocol (since 3.4)

  SHARDED CLUSTER:
    mongos (router) ↔ Config Servers (3-node replica set holding metadata)
    Each shard is a replica set
    Routing by shard key (hash or range)

  TRANSACTIONS:
    Single-shard: snapshot isolation
    Multi-shard: 2PC across shard primaries (since 4.2)

  CHANGE STREAMS:
    Tail the oplog; resumable via tokens
```

### 16.2 Couchbase

Originally a fork of memcached + CouchDB. Today: distributed document DB with N1QL (SQL-like query language), eventing, search, and analytics services.

```
COUCHBASE SERVICES (multi-dimensional scaling)

  Different server roles can scale independently:
    Data Service (KV + view engine)
    Query Service (N1QL)
    Index Service (GSI)
    Search Service (FTS)
    Analytics Service
    Eventing Service

  XDCR: Cross-Datacenter Replication for multi-region.
```

### 16.3 Cosmos DB

Microsoft's globally distributed multi-model database.

```
COSMOS DB FEATURES

  CONSISTENCY: 5 levels (per request):
    Strong → Bounded Staleness → Session → Consistent Prefix → Eventual

  APIs:
    SQL (Core), MongoDB, Cassandra, Gremlin, Table
    Same engine; different wire protocols

  GLOBAL DISTRIBUTION:
    Multi-region writes (active-active) with conflict resolution
      (LWW, custom procedure, or merge-on-read)

  PARTITIONING:
    Logical partitions (group of items with same partition key)
    Physical partitions (autoscaled by Cosmos)

  RU-BASED PRICING:
    Request Units abstract over CPU/IOPS/memory
```

---

## 17. Operational Patterns and Anti-Patterns

### 17.1 Hot Partition / Hot Range

```
HOT PARTITION: WHAT AND WHY

  Symptom: one partition gets 10x traffic of others; that node saturates.

  CAUSES:
    - Sequential primary key (auto-increment, timestamp)
      → all new writes go to the highest range/partition
    - Heavy read on a single popular item (celebrity, viral content)
    - Bad shard key choice (e.g., country code with US dominant)

  MITIGATIONS:
    - Hash the key (DynamoDB-style; but then range scans become impossible)
    - Add a random prefix or suffix ("hash-stretching")
    - Use a UUID or ULID instead of auto-increment
    - In CockroachDB: PRIMARY KEY (id) where id is a UUID v7 (time-prefixed
      but with random suffix) — gives sortability with reduced hot-spotting
    - Read replicas / caching for hot reads

  IN COCKROACHDB:
    Heat-based load splitting since v22 — auto-splits hot ranges
```

### 17.2 The "Shard Key Cannot Be Changed" Trap

In most distributed DBs, the shard key (or partition key) is immutable for the life of the table. Picking it wrong is *very* expensive to fix.

```
WHEN YOU PICKED THE WRONG SHARD KEY

  Option 1: Live with it (often the answer)
  Option 2: Build a new table with the right key, dual-write, migrate
  Option 3: Some systems (CockroachDB) allow "ALTER TABLE PRIMARY KEY"
            but it requires backfill into a new physical layout

  RULES OF THUMB FOR SHARD KEY SELECTION:
    1. High cardinality (many distinct values)
    2. Even distribution (no value dominates)
    3. Aligned with most common query patterns
       (so queries hit one shard, not all)
    4. Stable (don't pick something updateable)
    5. Not monotonic (avoid timestamps, auto-increment alone)
```

### 17.3 Cross-Shard Queries

```
THE FAN-OUT PROBLEM

  SELECT count(*) FROM users WHERE country = 'US'

  In a sharded system:
    Coordinator must query EVERY shard, sum results.
    Latency = max(shard latencies) + coordination overhead.
    Throughput = inverse of fanout cost.

  WITH 100 SHARDS, A QUERY THAT TOUCHES ALL OF THEM IS ~100x EXPENSIVE.

  MITIGATIONS:
    - Co-locate related data (same shard key on related tables)
    - Pre-aggregate via materialized views or CDC pipelines
    - Use covering secondary indexes that ARE shard-aligned with the query
    - Push predicates into the shard (don't return raw rows to coordinator)
    - For ad-hoc analytics: replicate to a column store (HTAP)
```

### 17.4 Distributed Joins

```
JOIN STRATEGIES IN A DISTRIBUTED DB

  COLLOCATED JOIN (best):
    Both tables sharded on the join key.
    Each shard joins locally; results merged.
    Cost: O(shard count × per-shard work)

  BROADCAST JOIN:
    Smaller table broadcast to every shard.
    Larger table joined locally with the broadcast.
    Cost: small_size × shard_count + large_local_work

  SHUFFLE / REPARTITION JOIN (worst):
    Both tables re-partitioned on join key, shipped over network.
    Cost: total data sent over network = both tables fully.

  IMPLICATIONS FOR SCHEMA DESIGN:
    Shard related tables on the same key:
      orders sharded by customer_id
      order_items sharded by customer_id  ← not order_id
    This makes orders ⨝ order_items collocated.
```

### 17.5 Backup and Restore

```
BACKUP/RESTORE IN DISTRIBUTED DBs

  PROBLEM: how to take a CONSISTENT snapshot across all nodes?

  APPROACH 1: COORDINATED SNAPSHOT
    Pick a global timestamp; every node snapshots data ≤ that timestamp.
    With MVCC + HLC: each node's local snapshot at ts is naturally consistent.
    Examples: CockroachDB BACKUP, Spanner export.

  APPROACH 2: LOG-BASED CONTINUOUS BACKUP
    Continuously ship WAL/changefeed to durable storage.
    Restore: replay log to any point in time within retention.
    Examples: Aurora continuous backup, Neon PITR.

  APPROACH 3: SHARD-LEVEL PARALLEL EXPORT
    Each shard backs up independently; metadata records the timestamp.
    Restore: each shard restores from its own backup.
    Examples: most NoSQL backups (DynamoDB, Cassandra).

  TIME COSTS:
    A TB-scale cluster: backup ~minutes (parallel); restore ~hours
    (must rebuild Raft state, replay logs, etc.)
```

### 17.6 Operations Anti-Patterns

```
COMMON OPERATIONAL MISTAKES

  1. UPGRADING ALL NODES SIMULTANEOUSLY
     Problem: lose quorum during upgrade
     Fix: rolling upgrade with version compatibility checks

  2. CHANGING REPLICATION FACTOR WITHOUT BACKFILL
     Problem: under-replicated data until rebalance completes
     Fix: throttled rebalance; monitor replica diversity

  3. RUNNING `nodetool repair` ON ALL NODES SIMULTANEOUSLY
     Problem: massive disk + network spike; cluster instability
     Fix: stagger repair; use incremental or subrange repair

  4. NOT MONITORING CLOCK SKEW
     Problem: HLCs / TrueTime degrade silently with bad clocks
     Fix: alert on NTP/PTP drift; CockroachDB exposes max_offset metric

  5. IGNORING SCHEMA CHANGE LIVENESS
     Problem: long-running schema change blocks DDL
     Fix: monitor schema change progress; cancel stuck changes

  6. DEPLOYING WITHOUT LOCALITY HINTS
     Problem: nodes spread randomly across AZs; one AZ outage takes 50% data offline
     Fix: configure locality (zone/region/rack) explicitly

  7. USING DEFAULT TIMEOUTS
     Problem: defaults assume LAN; cross-region needs longer timeouts
     Fix: tune raft heartbeat, lease duration, max_offset for actual latency
```

---

## 18. Failure Modes and War Stories

### 18.1 The Classic Distributed System Failures

```
FAILURE TAXONOMY

  1. NETWORK PARTITION
     Replicas can't reach each other; minority loses quorum
     Behavior depends on CP/AP choice

  2. SLOW DISK / SLOW NODE ("brown-out")
     Node is "alive" but processing 10x slower than peers
     Drags down the whole cluster (waits for slow replica's ack)

  3. CLOCK SKEW / CLOCK JUMP
     HLC-based ordering breaks when clocks jump backward
     TrueTime widens uncertainty; reads/commits stall

  4. GC PAUSE
     JVM-based DBs (Cassandra, HBase, Elasticsearch): multi-second STW pauses
     Manifest as "node is dead" then "node is alive again 30s later"

  5. SPLIT BRAIN
     Two leaders elected simultaneously due to network partition + bad fencing
     Both accept writes; data divergence

  6. CASCADING FAILURE
     One node fails; load redistributes; remaining nodes overload; more fail
     Classic positive-feedback loop

  7. METADATA SERVICE OUTAGE
     etcd/PD/zookeeper down → no new range assignments, no new schema changes
     Existing operations may continue but cluster is "frozen"

  8. POISON PILL DATA
     A specific request crashes a worker; load balancer retries; crashes another
     Eventually all workers crash
```

### 18.2 Real-World Postmortems Worth Studying

| Incident | Lesson |
|---|---|
| GitHub 2018 (43-second partition → 24h recovery) | MySQL Orchestrator failover decisions made under partition ambiguity led to write divergence. Two-region replication needed external fencing. |
| Cloudflare 2020 (Quicksilver KV outage) | Centralized KV system became dependency for everything; one bad config propagated globally. |
| Stripe 2017 (Mongo failover) | Mongo replica set election made under unusual conditions led to 3-hour outage. |
| AWS DynamoDB 2015 (us-east-1) | Metadata service overload during partition expansion caused cascading failure. |
| Google Spanner Pub/Sub 2019 | Quota exhaustion in metadata layer caused cluster-wide write failures. |
| CockroachDB outage 2022 (multi-region) | Lease transfer protocol bug caused some ranges to lose leadership during region failover. |
| Atlassian 2022 (multi-week outage) | Bad delete script permanently destroyed data; backups insufficient for the size of damage. |

### 18.3 Lessons That Repeat

```
PATTERNS FROM DECADES OF POSTMORTEMS

  1. THE METADATA LAYER IS THE FRAGILE PART
     Most outages aren't data corruption; they're metadata service overload
     Replicate metadata services aggressively; cap their load

  2. FAILURE DETECTORS ARE NEVER PERFECT
     Tune for false positives (spurious failovers) vs false negatives
     (slow failure detection); pick conservatively for stability

  3. DEPENDENCIES BETWEEN SHARDS CAUSE CASCADES
     A single hot shard can take down the cluster via shared coordinator
     Avoid shared bottlenecks (timestamp oracle, lock manager, etc.)

  4. RECOVERY IS USUALLY HARDER THAN FAILOVER
     The system "fails over" in seconds; "recovers" over hours/days
     Design for recovery: throttling, backpressure, partial-state handling

  5. BACKUPS THAT YOU HAVEN'T RESTORED ARE NOT BACKUPS
     Test restore in staging regularly
     Measure RTO / RPO realistically

  6. WHEN IN DOUBT, FENCE
     Fencing tokens, leases, generation IDs prevent the rare-but-fatal
     "old leader writes after a new one is elected" scenarios
```

---

## 19. Comparison Matrix

```
┌──────────────────┬─────────┬─────────────┬──────────────┬─────────────┬──────────────┐
│ System           │ Model   │ Consistency │ Replication  │ Multi-Region│ Best For     │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ Spanner          │ SQL     │ External    │ Per-tablet   │ First-class │ Globally     │
│                  │         │ (linearizble)│ Paxos        │ active-actv │ consistent   │
│                  │         │             │              │             │ enterprise   │
│                  │         │             │              │             │ (and only on │
│                  │         │             │              │             │ GCP)         │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ CockroachDB      │ SQL     │ Serializable│ Per-range    │ First-class │ Distributed  │
│                  │ (PG)    │             │ Raft         │ active-actv │ SQL anywhere │
│                  │         │             │              │             │ (cloud-      │
│                  │         │             │              │             │ neutral)     │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ TiDB             │ SQL     │ Snapshot    │ Per-region   │ Yes         │ MySQL-       │
│                  │ (MySQL) │ (default)   │ Raft         │ (limited    │ compatible   │
│                  │         │ Serializable│              │ multi-rgn)  │ HTAP         │
│                  │         │ (opt-in)    │              │             │              │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ YugabyteDB       │ SQL     │ Serializable│ Per-tablet   │ Yes         │ PG-compat    │
│                  │ (PG)+CQL│             │ Raft         │             │ + Cassandra  │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ Aurora (classic) │ SQL     │ Snapshot    │ Single writer│ Read replicas│ AWS         │
│                  │ (PG/MY) │             │ + 6-way      │ only        │ workloads    │
│                  │         │             │ storage      │             │              │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ Aurora DSQL      │ SQL     │ Strict      │ OCC + multi- │ First-class │ Cloud-native │
│                  │ (PG)    │ serializable│ region storage│ active-actv │ globally-    │
│                  │         │             │              │             │ consistent   │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ FoundationDB     │ Ordered │ Strict      │ Single seq + │ Single-rgn  │ Building     │
│                  │ KV      │ serializable│ stateless    │ (mostly)    │ block        │
│                  │         │             │ pipeline     │             │              │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ DynamoDB         │ KV/Doc  │ Tunable     │ Quorum (3-rep)│ Global Tab │ AWS-native   │
│                  │         │             │              │ (LWW)       │ scale        │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ Cassandra        │ Wide-col│ Tunable     │ Leaderless   │ Yes         │ Write-heavy, │
│                  │         │             │ quorum       │ (LWW)       │ AP, OSS      │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ ScyllaDB         │ Wide-col│ Tunable     │ Leaderless   │ Yes         │ Cassandra    │
│                  │         │             │ quorum       │             │ replacement, │
│                  │         │             │              │             │ better perf  │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ MongoDB          │ Document│ Tunable     │ Single       │ Sharded +   │ Document     │
│                  │         │ (default    │ leader/RS    │ async repl  │ workloads    │
│                  │         │ snapshot)   │              │             │              │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ Cosmos DB        │ Multi-  │ 5 levels    │ Multi-leader │ First-class │ Azure        │
│                  │ model   │             │              │ (5 conflict │ all-in-one   │
│                  │         │             │              │ strategies) │              │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ FaunaDB          │ Doc/Rel │ Strict      │ Calvin       │ First-class │ Serverless   │
│                  │         │ serializable│ deterministic│             │ globally     │
│                  │         │             │              │             │ consistent   │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ Riak             │ KV/CRDT │ Eventual    │ Leaderless   │ Yes         │ AP store     │
│                  │         │             │ + CRDTs      │             │ with rich    │
│                  │         │             │              │             │ data types   │
├──────────────────┼─────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ Neon             │ SQL     │ Snapshot    │ Disaggregated│ Read        │ Postgres,    │
│                  │ (PG)    │             │ (Pageserver) │ replicas    │ branching    │
└──────────────────┴─────────┴─────────────┴──────────────┴─────────────┴──────────────┘
```

---

## 20. Decision Frameworks

### 20.1 Choosing a Distributed Database

```
DECISION TREE

  Q1: Do you need strong consistency (linearizable) for any operations?
      ├── NO → consider AP systems (Cassandra, DynamoDB, Riak)
      └── YES → continue

  Q2: Do you need cross-shard transactions?
      ├── NO → KV stores (DynamoDB, BigTable, FoundationDB) are sufficient
      └── YES → continue

  Q3: Do you need multi-region active-active writes?
      ├── NO → single-region distributed SQL works (CockroachDB, TiDB)
      └── YES → continue

  Q4: Are you on a specific cloud?
      ├── GCP → Spanner is a default
      ├── AWS → Aurora DSQL or CockroachDB on AWS
      ├── Azure → Cosmos DB or CockroachDB
      └── Multi-cloud / OSS → CockroachDB or YugabyteDB

  Q5: What's your write throughput target?
      ├── < 10k tx/s → most options work
      ├── 10k-100k tx/s → Spanner, CockroachDB, FaunaDB, FoundationDB
      └── > 100k tx/s → consider AP (Cassandra, ScyllaDB) or
                          partitioned approach (Vitess, Citus)
```

### 20.2 When NOT to Use a Distributed Database

```
DON'T DISTRIBUTE WHEN:

  ─ Your data fits on one machine (under ~1TB working set)
  ─ Your write rate fits on one machine (under ~10k TPS for OLTP)
  ─ You don't need multi-region
  ─ You can tolerate <1 hour of downtime per year (HA via streaming repl)
  ─ Your team has limited operational distributed-systems experience

  → Single Postgres + read replicas + good backups will outperform
    a distributed DB on cost, latency, and operational complexity.

  → Add distribution when you HIT a wall, not before.
```

### 20.3 The Real Cost of Distribution

```
WHAT DISTRIBUTION COSTS YOU

  ─ Latency: every cross-node operation adds RTT (1-100ms+)
  ─ Operational complexity: 5x-10x the operational burden vs single-node
  ─ Hardware cost: replication factor 3 means 3x storage cost minimum
  ─ Engineer time: cluster monitoring, capacity planning, upgrade testing
  ─ Schema design: every choice has cluster-wide implications
  ─ Debugging: distributed traces, clock skew, partial failures
  ─ Reasoning about correctness: weaker consistency → harder app code

  REWARD:
  ─ Horizontal scaling
  ─ Fault tolerance
  ─ Geographic locality
  ─ Operational uptime

  Make sure the reward is worth the cost for YOUR workload.
```

---

## Further Reading

**Foundational papers:**
- Spanner (OSDI 2012): TrueTime + 2PC over Paxos
- F1 (VLDB 2013): SQL on Spanner; schema change protocol
- Percolator (OSDI 2010): distributed transactions on BigTable
- Calvin (SIGMOD 2012): deterministic distributed transactions
- Aurora (SIGMOD 2017): "the log is the database"
- Dynamo (SOSP 2007): leaderless, eventual consistency at scale
- Chain Replication (OSDI 2004)
- SWIM (DSN 2002): scalable membership
- "There is no now" (ACM Queue, Justin Sheehy)

**Books:**
- *Designing Data-Intensive Applications* (Kleppmann)
- *Database Internals* (Petrov)
- *Distributed Systems* (van Steen & Tanenbaum)

**Related files in this directory:**
- `12-replication-and-distributed-storage.md` — replication and consensus fundamentals
- `16-failure-detection-and-leader-election.md` — failure detection deep dive
- `05-transactions-and-concurrency.md` — single-node transaction internals
- `09-htap-databases.md` — HTAP systems
- `13-lsm-trees-and-compaction.md` — LSM internals (used by most distributed KV/SQL stores)
