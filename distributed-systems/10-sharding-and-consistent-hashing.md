# Sharding and Consistent Hashing

A production-grade reference covering data partitioning from first principles through the consistent hashing algorithm and its modern variants, rebalancing mechanics, routing strategies, and the interaction between sharding and replication. Includes deep dives into how DynamoDB, Cassandra, Redis Cluster, Kafka, and CockroachDB partition data in production, with dedicated sections on partitioning for ML/AI systems (feature stores, embedding indexes, model serving, training data distribution). Written for senior and Staff+ engineers who need to reason precisely about data placement, hotspot mitigation, and capacity planning under interview pressure.

Prerequisites: familiarity with distributed system fundamentals from `00-primitives-and-system-models.md`, replication from `05-replication-strategies.md`, and consistency models from `04-consistency-models-linearizability-to-eventual.md`. This chapter is referenced heavily by the solutions for recommendation systems, feature stores, ML inference platforms, agent orchestration, AI search engines, and parallel ML training.

---

## Table of Contents

1. [Why Partition Data](#1-why-partition-data)
2. [Partitioning Strategies](#2-partitioning-strategies)
3. [Consistent Hashing -- The Core Algorithm](#3-consistent-hashing--the-core-algorithm)
4. [Virtual Nodes (Vnodes)](#4-virtual-nodes-vnodes)
5. [Consistent Hashing Variants](#5-consistent-hashing-variants)
6. [Partition Assignment and Rebalancing](#6-partition-assignment-and-rebalancing)
7. [Routing: How Clients Find the Right Partition](#7-routing-how-clients-find-the-right-partition)
8. [Replication + Partitioning Interaction](#8-replication--partitioning-interaction)
9. [Secondary Indexes on Partitioned Data](#9-secondary-indexes-on-partitioned-data)
10. [Real-World Systems Deep Dives](#10-real-world-systems-deep-dives)
11. [Partitioning for ML/AI Systems](#11-partitioning-for-mlai-systems)
12. [Capacity Planning Math](#12-capacity-planning-math)
13. [Failure Walkthroughs](#13-failure-walkthroughs)
14. [Interview Patterns](#14-interview-patterns)

---

## 1. Why Partition Data

### 1.1 Vertical Scaling Hits a Wall

Every database starts on a single machine. You scale vertically -- more CPU, more RAM, bigger disks -- until you cannot. The wall is not hypothetical. It has concrete coordinates:

- **Storage**: A single machine tops out around 64TB of NVMe (8 drives x 8TB). Your 200TB dataset does not fit.
- **Memory**: Even the largest cloud instances (e.g., AWS u-24tb1.112xlarge) cap at 24TB of RAM. If your working set is 50TB, you cannot keep it in memory on one node.
- **Write throughput**: A single SSD sustains roughly 500K-1M random IOPS. If your workload demands 10M random writes/sec, one machine cannot deliver it.
- **Read throughput**: A single node has finite CPU and network bandwidth. At 500K QPS per node with p99 < 5ms, your 5M QPS target requires at least 10 nodes.
- **Availability**: A single machine is a single point of failure. Hardware MTBF for enterprise SSDs is roughly 2 million hours, but a cluster of 100 nodes will see a drive failure roughly every 2.3 days on average.

Vertical scaling is also nonlinear in cost. Doubling a machine's RAM and CPU rarely doubles its price -- it often quadruples it. Horizontal scaling with commodity hardware gives linear cost scaling.

### 1.2 Horizontal Scaling: Distribute the Problem

Horizontal scaling means splitting the dataset across multiple machines, each holding a fraction. This is **partitioning** (also called **sharding**). The benefits are direct:

```
VERTICAL vs HORIZONTAL SCALING:

Vertical (Scale Up):                    Horizontal (Scale Out):
┌────────────────────────┐              ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐
│                        │              │ 25%  │ │ 25%  │ │ 25%  │ │ 25%  │
│     One Big Machine    │              │ data │ │ data │ │ data │ │ data │
│                        │              │      │ │      │ │      │ │      │
│  100% of data          │              │ 25%  │ │ 25%  │ │ 25%  │ │ 25%  │
│  100% of traffic       │              │ QPS  │ │ QPS  │ │ QPS  │ │ QPS  │
│  Single point of       │              │      │ │      │ │      │ │      │
│  failure               │              │ Cheap│ │Cheap │ │Cheap │ │Cheap │
│                        │              │ node │ │ node │ │ node │ │ node │
│  $$$$$$$$$             │              │  $   │ │  $   │ │  $   │ │  $   │
└────────────────────────┘              └──────┘ └──────┘ └──────┘ └──────┘

Total cost: $$$$$$$$$$                  Total cost: $$$$
Single failure domain                   Survive node failures
Hard ceiling on capacity                Add nodes for more capacity
```

### 1.3 When to Shard vs When to Replicate

Sharding and replication solve different problems, and confusing them is a common interview mistake.

**Replication** (copies of the same data on multiple nodes) solves:
- **Read scalability**: Distribute reads across replicas.
- **Fault tolerance**: If one replica dies, others serve the data.
- **Latency**: Place replicas closer to users (geo-distribution).

Replication does NOT solve:
- **Write scalability**: All replicas must receive every write. More replicas does not increase write throughput.
- **Storage capacity**: Every replica stores the full dataset. 10 replicas of 10TB is still 10TB of unique data.

**Partitioning** (different slices of data on different nodes) solves:
- **Write scalability**: Each partition handles writes for only its slice. Total write throughput scales with partition count.
- **Storage capacity**: Each partition stores only its slice. Total capacity is the sum of partition capacities.
- **Compute parallelism**: Queries that touch different partitions run in parallel on different CPUs.

In practice, you almost always combine both: partition the data across nodes, then replicate each partition for fault tolerance.

```
SHARD + REPLICATE (the standard production pattern):

Data: keys A-Z, 3 partitions, replication factor = 3

Partition P1 (A-H):   Node 1 (leader)  │  Node 2 (follower)  │  Node 3 (follower)
Partition P2 (I-P):   Node 2 (leader)  │  Node 3 (follower)  │  Node 4 (follower)
Partition P3 (Q-Z):   Node 3 (leader)  │  Node 4 (follower)  │  Node 1 (follower)

- Writes to keys A-H go to Node 1 (P1 leader)
- Writes to keys I-P go to Node 2 (P2 leader)
- Writes to keys Q-Z go to Node 3 (P3 leader)
- Each partition survives 2 node failures
- Write throughput = 3x a single node (3 independent leaders)
```

### 1.4 The Three Things You Are Distributing

When you shard a system, you are distributing three distinct concerns, and they may be partitioned differently:

1. **Data**: The bytes on disk. Partitioned by key. This is what most people think of when they hear "sharding."
2. **Compute**: The CPU cycles to process queries. A scatter-gather query fans out compute to every partition. A point lookup concentrates compute on one partition.
3. **State**: In-memory state like caches, connection pools, in-flight transactions, and hot data. State follows data but can lag (e.g., after a rebalance, the cache on the new owner is cold).

The distinction matters. In an ML feature store, the data (feature values) is partitioned by entity_id. The compute (feature lookups at serving time) is partitioned the same way. But the state (the local cache of hot features) takes time to warm after rebalancing, and your p99 latency will spike during that window.

---

## 2. Partitioning Strategies

There are three fundamental strategies for deciding which partition owns which key. Every production system uses one of these, or a hybrid.

### 2.1 Range Partitioning

Assign each partition a contiguous range of the key space. Keys are sorted, and each partition owns a range: Partition 1 owns [A, G), Partition 2 owns [G, N), etc.

```
RANGE PARTITIONING:

Key space: [A ──────────────────────────────────────────────── Z]

Partition 1    │    Partition 2    │    Partition 3    │   Partition 4
[A ─── F]      │    [G ─── L]     │    [M ─── R]     │   [S ─── Z]

Key "Charlie" → P1     Key "Hotel" → P2     Key "Tango" → P4
```

**Advantages**:
- Range queries are efficient. "Give me all users with names starting A through C" hits one partition.
- Data locality: keys near each other in sort order live on the same partition, enabling efficient scans.
- Natural for time-series data: partition by time range (e.g., one partition per day/week).

**Disadvantages**:
- **Hotspots from skewed access patterns**: If all writes have keys in the same range (e.g., all today's timestamps go to the "current day" partition), one partition gets all the write load while others sit idle.
- **Uneven partition sizes**: Key distribution is rarely uniform. If you partition user IDs alphabetically, the "S" partition will be larger than the "Q" partition because more names start with S.
- **Manual or complex auto-splitting**: Range boundaries must be chosen carefully, and rebalancing requires splitting or merging ranges.

**How BigTable/HBase do range splits**: BigTable and HBase use range partitioning with automatic splitting. Each tablet (BigTable) or region (HBase) is responsible for a contiguous range of row keys. When a tablet exceeds a size threshold (default: 8GB for Cloud Bigtable, configurable in HBase), the system automatically splits it at a midpoint into two tablets. The split is transparent to clients -- the metadata table is updated, and future requests are routed to the correct half. HBase also supports merge operations when adjacent regions become too small.

**CockroachDB's range-based approach**: CockroachDB partitions data into ranges (default 512MB target). Ranges automatically split when they exceed the threshold and merge when they shrink below half. The split point is chosen at the key that best divides the range's data by size. This is coupled with an automatic leaseholder transfer mechanism: the node with the most recent data for a range holds the lease and serves reads directly.

**Auto-splitting on size vs load**: Splitting only on size misses a critical case -- a small partition with a hot key. DynamoDB recognized this and added adaptive capacity, which splits partitions based on throughput pressure, not just storage. If a single partition is throttled because one key receives disproportionate traffic, DynamoDB splits the partition even if it is well under the size limit, isolating the hot key.

### 2.2 Hash Partitioning

Apply a hash function to the key and use the hash value to determine the partition. The goal is uniform distribution regardless of key patterns.

#### 2.2.1 Modular Hashing: The Simple (and Broken) Approach

The simplest hash partitioning: `partition = hash(key) % N` where N is the number of partitions/nodes.

```
MODULAR HASHING (hash(key) % N):

With N = 3 nodes:
  hash("user:1001") = 7842391  →  7842391 % 3 = 1  →  Node 1
  hash("user:1002") = 2938472  →  2938472 % 3 = 0  →  Node 0
  hash("user:1003") = 5629103  →  5629103 % 3 = 2  →  Node 2
  hash("user:1004") = 1294857  →  1294857 % 3 = 0  →  Node 0
```

This gives uniform distribution. The problem is what happens when N changes.

#### 2.2.2 The Fatal Resize Problem

When you add or remove a node, N changes, and `hash(key) % N` produces a completely different assignment for almost every key.

```
THE RESIZE CATASTROPHE:

Before (N=3):                         After adding one node (N=4):
  hash("user:1001") % 3 = 1          hash("user:1001") % 4 = 3  ← MOVED
  hash("user:1002") % 3 = 0          hash("user:1002") % 4 = 0  ← same (lucky)
  hash("user:1003") % 3 = 2          hash("user:1003") % 4 = 3  ← MOVED
  hash("user:1004") % 3 = 0          hash("user:1004") % 4 = 1  ← MOVED
  hash("user:1005") % 3 = 1          hash("user:1005") % 4 = 2  ← MOVED
  hash("user:1006") % 3 = 2          hash("user:1006") % 4 = 2  ← same (lucky)

  Result: ~67% of keys moved (in general, (N-1)/N keys move ≈ 100% for large N)
```

With modular hashing, adding one node to a 100-node cluster moves approximately 99% of all keys. If you have 10TB of data distributed across 100 nodes, adding node 101 means shuffling approximately 9.9TB of data across the network. During this reshuffling window, the old location no longer has the data, but the new location has not received it yet -- leading to cache misses, request failures, or requiring a complex dual-read strategy.

This is why modular hashing is unsuitable for dynamic clusters. You need consistent hashing.

### 2.3 Directory-Based Partitioning

Maintain an explicit lookup table (directory) that maps each key (or key range) to its partition. The client or a routing layer consults the directory for every request.

```
DIRECTORY-BASED PARTITIONING:

┌─────────────────────────────────┐
│        Partition Directory       │
│  (stored in ZooKeeper/etcd)     │
├──────────────┬──────────────────┤
│ Key/Range    │ Partition (Node) │
├──────────────┼──────────────────┤
│ user:1-1000  │ Node A           │
│ user:1001-2K │ Node B           │
│ user:2001-5K │ Node C           │
│ user:5001-8K │ Node A           │
│ user:8001-10K│ Node D           │
└──────────────┴──────────────────┘

Client → lookup("user:3500") → directory says Node C → route to Node C
```

**Advantages**:
- Maximum flexibility: you can place any key on any node.
- Supports arbitrary rebalancing without rehashing.
- Can encode complex policies (e.g., "all data for customer X goes to the EU region").

**Disadvantages**:
- **Single point of failure**: The directory must be highly available. If it is down, no requests can be routed.
- **Bottleneck**: Every request requires a directory lookup, adding latency. Caching helps but introduces staleness.
- **Scale limit on the directory itself**: If you have billions of keys with individual assignments, the directory becomes enormous.

In practice, directory-based partitioning is used when the partition mapping is relatively coarse (e.g., tens of thousands of ranges, not billions of individual keys). HBase's hbase:meta table is essentially a directory. MongoDB's config servers hold a directory of chunk-to-shard mappings.

---

## 3. Consistent Hashing -- The Core Algorithm

Consistent hashing was introduced by Karger et al. (1997) to solve the exact problem that modular hashing creates: how to distribute keys across a changing set of nodes with minimal disruption when nodes are added or removed.

### 3.1 The Hash Ring Concept

The core idea: imagine the output space of a hash function as a circle (ring) rather than a line. For a hash function producing values in [0, 2^128), we conceptually connect position 0 to position 2^128 - 1, forming a ring.

Both nodes and keys are hashed onto this ring using the same hash function. A node's position is determined by hashing its identifier (e.g., IP:port). A key's position is determined by hashing the key.

```
THE HASH RING:

        Hash space [0, 2^128) arranged as a circle:

                        0 / 2^128
                          |
                     _____|_____
                   /      |      \
                  /       |       \
                /    Node A        \
               |    (pos: 50)       |
               |         |         |
    3/4 ·2^128 ─ ─ ─ ─ ─+─ ─ ─ ─ ─  1/4 · 2^128
               |         |         |
               |         |         |
                \   Node C        /
                  \ (pos: 200)  /
                   \     |    /
                     \___|__/
                          |
                    1/2 · 2^128
                   Node B
                  (pos: 170)

(Positions simplified for illustration. Real positions are 128-bit integers.)
```

### 3.2 The Assignment Rule

To determine which node owns a key, hash the key to find its position on the ring, then walk clockwise until you encounter a node. That node owns the key.

```
KEY ASSIGNMENT ON THE HASH RING:

Nodes: A (pos 50), B (pos 170), C (pos 200)
Ring: 0 ──── 50(A) ──── 170(B) ──── 200(C) ──── 2^128/0

                          0
                          │
                     _____|_____
                   /   ↑  │      \
                  /  k1=30│       \        k1 (pos 30): walk clockwise → A (50)
                /    Node A(50)    \       k2 (pos 60): walk clockwise → B (170)
               |         │         |       k3 (pos 180): walk clockwise → C (200)
               |  k4=240 │  k2=60  |       k4 (pos 240): walk clockwise past C,
               |    ↑    │    ↓    |            wraps around → A (50)
                \  Node C(200)    /
                  \       │     /          Node A owns: keys in (200, 50]
                   \_____|____/            Node B owns: keys in (50, 170]
                     ↑   │                 Node C owns: keys in (170, 200]
                  Node B(170)
                  k3=180→↗

Ownership arcs:
  Node A: (200 ──── 0 ──── 50]     (wraps around)
  Node B: (50 ──── 170]
  Node C: (170 ──── 200]
```

### 3.3 Node Addition: Minimal Key Movement

When a new node D is added at position 120, only the keys in the arc (50, 120] need to move -- they were assigned to Node B (the next node clockwise after position 50), and now they belong to Node D.

```
ADDING NODE D AT POSITION 120:

Before:                                 After:
  A(50) ──── B(170) ──── C(200)          A(50) ── D(120) ── B(170) ── C(200)

  B owned: (50, 170]                     D now owns: (50, 120]
                                         B now owns: (120, 170]  (smaller arc)

  Keys in (50, 120] move from B to D.
  Keys in (120, 170] stay on B.
  Keys on A and C: UNCHANGED.

        0                                        0
        │                                        │
   _____|_____                              _____|_____
  /     │      \                           /     │      \
 /      │       \                         /      │       \
/    A(50)       \                       /    A(50)       \
|       │         |                     |       │  D(120)  |
|       │         |                     |       │  (NEW)   |
 \   C(200)      /                       \   C(200)       /
  \      │     /                          \      │      /
   \_____|___/                             \_____|____/
         │                                       │
      B(170)                                  B(170)

Moved: only keys in (50, 120]
Untouched: keys in (120, 170], (170, 200], (200, 50]
```

### 3.4 Node Removal: Minimal Key Movement

When Node B (position 170) is removed, only B's keys need to move -- they transfer to B's successor (Node C at position 200). If we are in the state with D present: B owned (120, 170], and those keys now go to C.

```
REMOVING NODE B (pos 170):

Before: A(50) ── D(120) ── B(170) ── C(200)
After:  A(50) ── D(120) ──────────── C(200)

  B's arc (120, 170] → now owned by C (next clockwise node)
  C's new arc: (120, 200]  (absorbs B's range)

  Keys on A and D: UNCHANGED.
  Only B's keys move, and they all go to C.
```

### 3.5 The Math: Why This Is Optimal

With modular hashing (`hash(key) % N`), adding one node to N nodes moves approximately `K * (N-1)/N` keys, where K is the total number of keys. For large N, this approaches K -- nearly every key moves.

With consistent hashing, adding one node to N existing nodes moves approximately `K/N` keys on average -- only the keys in the new node's arc, which is 1/N of the ring on average.

```
KEY MOVEMENT COMPARISON:

  K = 1,000,000 keys, N = 100 nodes, adding 1 node:

  Modular hashing:    keys moved ≈ K × (N)/(N+1) ≈ 990,099  (~99%)
  Consistent hashing: keys moved ≈ K / (N+1)     ≈ 9,901    (~1%)

  That is a 100x reduction in data movement.

  For a system with 10TB across 100 nodes:
    Modular:    ~9.9TB of data migration
    Consistent: ~100GB of data migration
```

This is the fundamental reason consistent hashing exists: it makes cluster resizing practical by reducing data movement from O(K) to O(K/N).

### 3.6 Worked Example with Concrete Numbers

Let us walk through a concrete example with small numbers.

```
WORKED EXAMPLE:

Hash ring: positions 0-999 (simplified from 2^128 for readability)

Initial state: 3 nodes
  Node A → hash("NodeA") = 100
  Node B → hash("NodeB") = 400
  Node C → hash("NodeC") = 750

Ownership:
  Node A: (750, 100]  → positions 751-999, 0-100  (350 positions)
  Node B: (100, 400]  → positions 101-400          (300 positions)
  Node C: (400, 750]  → positions 401-750          (350 positions)

10 keys and their assignments:
  hash("user:1")   = 50   → walk clockwise → Node A (100)
  hash("user:2")   = 150  → walk clockwise → Node B (400)
  hash("user:3")   = 320  → walk clockwise → Node B (400)
  hash("user:4")   = 450  → walk clockwise → Node C (750)
  hash("user:5")   = 720  → walk clockwise → Node C (750)
  hash("user:6")   = 800  → walk clockwise → Node A (100)  [wraps]
  hash("user:7")   = 90   → walk clockwise → Node A (100)
  hash("user:8")   = 200  → walk clockwise → Node B (400)
  hash("user:9")   = 600  → walk clockwise → Node C (750)
  hash("user:10")  = 950  → walk clockwise → Node A (100)  [wraps]

Distribution: A=4, B=3, C=3  (reasonably balanced for 3 nodes)

ADD NODE D at hash("NodeD") = 250:

New ownership:
  Node A: (750, 100]  → unchanged (4 keys)
  Node D: (100, 250]  → positions 101-250  (NEW)
  Node B: (250, 400]  → positions 251-400  (shrank from 101-400)
  Node C: (400, 750]  → unchanged

Key reassignments:
  hash("user:2") = 150 → was Node B → NOW Node D  ← MOVED
  hash("user:8") = 200 → was Node B → NOW Node D  ← MOVED
  All other keys: UNCHANGED

Result: only 2 of 10 keys moved (20%, close to 1/N = 25% for N=4)
```

---

## 4. Virtual Nodes (Vnodes)

### 4.1 The Load Imbalance Problem

With only 3 physical nodes on the ring, the arcs are unlikely to be equal. One node might own 50% of the key space while another owns 10%. The standard deviation of load is proportional to `1/sqrt(N)` for N points on the ring, and with N=3, the imbalance is severe.

```
IMBALANCE WITH FEW PHYSICAL NODES:

3 nodes, unlucky hash positions:

  0 ───── A(50) ────────────────────────── B(800) ── C(900) ── 999

  Node A: (900, 50]   = 150 positions (15% of ring)
  Node B: (50, 800]   = 750 positions (75% of ring)  ← 5x overloaded!
  Node C: (800, 900]  = 100 positions (10% of ring)

  Node B handles 75% of all traffic. This defeats the purpose of sharding.
```

### 4.2 The Solution: Virtual Nodes

Instead of placing each physical node at one point on the ring, place it at V points (virtual nodes). A physical node with V=4 has 4 positions on the ring, each independently hashed (e.g., hash("NodeA-0"), hash("NodeA-1"), hash("NodeA-2"), hash("NodeA-3")).

```
VIRTUAL NODES:

Physical nodes: A, B, C
Virtual nodes per physical node: V = 4

Ring positions (simplified 0-999):
  A-0: 50     B-0: 120    C-0: 200
  A-1: 350    B-1: 500    C-1: 650
  A-2: 700    B-2: 820    C-2: 910
  A-3: 980    B-3: 440    C-3: 280

Sorted on ring:
  50(A) 120(B) 200(C) 280(C) 350(A) 440(B) 500(B) 650(C) 700(A) 820(B) 910(C) 980(A)

  │A│ B │ C │ C │ A │ B │ B │  C  │ A │  B  │  C  │ A │
  0   120  200 280  350 440 500   650  700   820   910  999

Now the ring is interleaved -- A, B, and C each own multiple small arcs
scattered around the ring. The total load per physical node is the sum
of its arcs, which converges toward 1/3 each as V increases.

Distribution of ring space (approximate):
  Node A: 70+70+50+70 = 260 positions (26%)
  Node B: 80+60+120+90 = 350 positions (35%)
  Node C: 80+70+150+70 = 370 positions (37%)

Better than the 15/75/10 split without vnodes, though V=4 is still
too few for excellent balance. V=150+ is needed in production.
```

### 4.3 How V Affects Balance

The standard deviation of load per physical node is inversely proportional to the square root of the number of virtual nodes:

```
LOAD BALANCE vs VIRTUAL NODE COUNT:

  std_dev(load) ∝ 1 / sqrt(V × N)

  where V = virtual nodes per physical node, N = physical nodes

  For N = 10 physical nodes:
  ┌──────────┬─────────────────────────────┬──────────────────────┐
  │ V (vnodes│ Total points on ring        │ Approx max load      │
  │ per node)│                             │ imbalance (%)        │
  ├──────────┼─────────────────────────────┼──────────────────────┤
  │    1     │    10                       │  ±50-100%            │
  │   10     │   100                       │  ±15-25%             │
  │   50     │   500                       │  ±8-12%              │
  │  150     │  1500                       │  ±3-5%               │
  │  256     │  2560                       │  ±2-3%               │
  │ 1000     │ 10000                       │  ±1-2%               │
  └──────────┴─────────────────────────────┴──────────────────────┘

  Diminishing returns: going from V=150 to V=256 improves balance by ~1%,
  but doubles the metadata size of the ring.
```

### 4.4 Practical V Values in Production Systems

- **Cassandra**: Default 256 vnodes per node (configurable via `num_tokens` in cassandra.yaml). Cassandra 4.0 introduced a more sophisticated token allocation algorithm that achieves good balance with fewer tokens.
- **Redis Cluster**: Uses a fixed 16,384 hash slots (effectively a pre-allocated set of virtual nodes). Each physical node owns a subset of these slots. This is a fixed-partition scheme rather than true vnodes, but the effect is similar.
- **Riak**: Default 64 vnodes per ring (not per node -- the total ring size is fixed, and vnodes are distributed across nodes).
- **DynamoDB**: Internal implementation, but automatic partition splitting means the system effectively manages its own "virtual" partitions.

### 4.5 Weighted Virtual Nodes for Heterogeneous Hardware

If your cluster has machines with different capacities (e.g., some nodes have 256GB RAM and others have 64GB), you can assign proportionally more virtual nodes to more powerful machines.

```
WEIGHTED VNODES:

  Node A: 256GB RAM, 8 cores  → V = 200 vnodes  (weight: 4)
  Node B:  64GB RAM, 2 cores  → V =  50 vnodes  (weight: 1)
  Node C: 128GB RAM, 4 cores  → V = 100 vnodes  (weight: 2)

  Expected load distribution:
    Node A: 200/350 = 57% of data  (proportional to capacity)
    Node B:  50/350 = 14% of data
    Node C: 100/350 = 29% of data
```

This is how you handle heterogeneous clusters without wasting capacity on small nodes or overloading them.

### 4.6 Trade-offs: More Vnodes Is Not Free

More virtual nodes means:
- **Better load balance** (the primary benefit).
- **Larger routing table**: With 100 physical nodes and V=256, the ring has 25,600 entries. Each entry is a token + node_id, typically 20-30 bytes, so the full ring is ~500KB-750KB. This must be stored and transmitted to every node and client.
- **More metadata during topology changes**: When a node joins or leaves, more vnode assignments change, generating more metadata updates.
- **Slower streaming during bootstrap**: A new node joining a Cassandra cluster with V=256 must stream data from up to 256 different source ranges, potentially from many different nodes. This was a significant operational pain point, leading Cassandra 4.0 to default to fewer tokens with a smarter allocation algorithm.
- **More repair overhead**: Repair operations in Cassandra must process each vnode range independently.

The sweet spot for most systems is V=128-256 per physical node, balancing load uniformity against operational complexity.

---

## 5. Consistent Hashing Variants

The original consistent hashing algorithm (Karger et al.) is not the only option. Several variants optimize for specific use cases.

### 5.1 Jump Consistent Hash (Google, 2014)

Published by Lamping and Veach at Google. Jump hash is beautifully simple: O(1) memory (no ring or table stored), O(ln N) computation time, and perfectly uniform distribution.

```python
def jump_consistent_hash(key: int, num_buckets: int) -> int:
    """Returns the bucket (0 to num_buckets-1) for the given key.
    O(ln num_buckets) time, O(1) memory."""
    b, j = -1, 0
    while j < num_buckets:
        b = j
        key = ((key * 2862933555777941757) + 1) & 0xFFFFFFFFFFFFFFFF
        j = int((b + 1) * (float(1 << 31) / float((key >> 33) + 1)))
    return b
```

**How it works**: The algorithm simulates a process where each key "jumps" forward through buckets in increasingly large steps, deterministically choosing whether to stay or advance. The mathematical properties guarantee that when you increase `num_buckets` from N to N+1, each key has exactly a 1/(N+1) probability of moving to the new bucket.

**Critical limitation**: Jump hash only supports adding or removing the *last* bucket. You can grow the cluster from N to N+1, but you cannot remove an arbitrary node in the middle. If node 3 out of 10 fails, you must either replace it in-place or rebuild the mapping with 9 buckets, which reshuffles more keys than consistent hashing would.

**Ideal for**: Stateless cache pools (like memcached) where nodes are interchangeable and you grow/shrink at the tail. Not suitable for general distributed storage where arbitrary nodes can fail.

### 5.2 Rendezvous Hashing (Highest Random Weight)

Each key is assigned to the node that produces the highest hash value when combined with the key: `node = argmax_n(hash(key, n))`.

```
RENDEZVOUS HASHING:

Key: "user:42"
Nodes: A, B, C, D

  hash("user:42", "A") = 847291    ← not highest
  hash("user:42", "B") = 293847    ← not highest
  hash("user:42", "C") = 951032    ← HIGHEST → assign to C
  hash("user:42", "D") = 582910    ← not highest

When Node C is removed:
  hash("user:42", "A") = 847291    ← HIGHEST → reassign to A
  hash("user:42", "B") = 293847
  hash("user:42", "D") = 582910

Only keys whose winner was C are affected. All other assignments stable.
```

**Properties**:
- O(N) per lookup (must compute hash for every node). This is fine for small N (< 100 nodes) but prohibitive for large N.
- O(1) memory per node (no ring structure).
- Minimal disruption: removing a node moves only that node's keys.
- Simple to implement and reason about.
- Naturally handles weighted nodes: multiply the hash by a weight factor.

**Used in**: Microsoft's CRUSH algorithm (Ceph) is a generalization. Rendezvous hashing is common in load balancers and DNS-based routing.

### 5.3 Maglev Hashing (Google, 2016)

Developed for Google's network load balancer (Maglev). It generates a fixed-size lookup table (typically a large prime like 65537 entries) with the property that when a backend is added or removed, only a minimal number of table entries change.

**How it works**: Each backend generates a permutation of table positions. The algorithm fills the table by iterating through backends in round-robin, each claiming the next unclaimed position in its permutation. The result is a table where each backend owns roughly `table_size / N` entries.

```
MAGLEV HASHING (simplified):

Lookup table (size M = 7, N = 3 backends):

  Slot:    0    1    2    3    4    5    6
  Owner:   A    B    C    A    B    C    A

  Lookup: hash(key) % 7 = slot → owner

  When backend B is removed:
  Slot:    0    1    2    3    4    5    6
  Owner:   A    C    C    A    A    C    A

  Only B's slots changed (slots 1 and 4). A and C absorbed them.
  Disruption ≈ 1/N of the table.
```

**Properties**:
- O(1) lookup time (direct table index).
- O(M * N) time to build the table (where M is table size, N is backend count).
- Minimal disruption: adding/removing a backend changes O(M/N) table entries.
- Fixed table size means fixed memory regardless of key count.

**Used in**: Google's Maglev load balancer, Envoy proxy, Katran (Facebook's L4 load balancer).

### 5.4 Ketama: The Original Memcached Consistent Hash

Ketama, developed by Last.fm, was the first widely deployed consistent hashing implementation for memcached. It uses MD5 hashing to place each node at 100-200 points on a ring, then uses standard ring-lookup for key assignment.

**Properties**:
- 100-200 virtual nodes per server (tunable).
- MD5 hash function (produces 128-bit output, used as ring position).
- Became the de facto standard for memcached consistent hashing.
- Simple ring-walk implementation, well-tested in production.

### 5.5 Comparison Table

```
CONSISTENT HASHING VARIANTS -- COMPARISON:

┌─────────────────┬──────────┬───────────┬──────────┬──────────────┬────────────────────┐
│ Algorithm       │ Lookup   │ Memory    │ Balance  │ Disruption   │ Best For           │
│                 │ Time     │           │          │ on resize    │                    │
├─────────────────┼──────────┼───────────┼──────────┼──────────────┼────────────────────┤
│ Ring (Karger)   │ O(log P) │ O(P)      │ Good     │ K/N keys     │ General-purpose    │
│ + vnodes        │ (P=N*V)  │           │ (V≥128)  │              │ distributed storage│
├─────────────────┼──────────┼───────────┼──────────┼──────────────┼────────────────────┤
│ Jump Hash       │ O(ln N)  │ O(1)      │ Perfect  │ K/(N+1) keys │ Stateless cache    │
│                 │          │           │          │ (append only)│ pools, grow-only   │
├─────────────────┼──────────┼───────────┼──────────┼──────────────┼────────────────────┤
│ Rendezvous      │ O(N)     │ O(N)      │ Perfect  │ K/N keys     │ Small N (<100),    │
│ (HRW)           │          │           │          │              │ load balancers     │
├─────────────────┼──────────┼───────────┼──────────┼──────────────┼────────────────────┤
│ Maglev          │ O(1)     │ O(M)      │ Near-    │ ~M/N entries │ Network load       │
│                 │          │ (table)   │ perfect  │              │ balancers, L4/L7   │
├─────────────────┼──────────┼───────────┼──────────┼──────────────┼────────────────────┤
│ Ketama          │ O(log P) │ O(P)      │ Good     │ K/N keys     │ Memcached clusters │
│                 │          │           │ (V~150)  │              │                    │
└─────────────────┴──────────┴───────────┴──────────┴──────────────┴────────────────────┘

P = total points on ring (N nodes × V vnodes)
M = Maglev lookup table size (prime, typically 65537)
K = total key count, N = node count
```

**Decision guide**:
- Default choice for distributed databases/storage: **Ring with vnodes** (battle-tested, flexible).
- Stateless cache tier growing monotonically: **Jump hash** (simplest, perfect balance).
- Load balancer with small backend pool: **Rendezvous** (simple, no state) or **Maglev** (O(1) lookup).
- Memcached-specific: **Ketama** (the standard).

---

## 6. Partition Assignment and Rebalancing

### 6.1 Static vs Dynamic Partitioning

**Static (fixed) partitioning**: The number of partitions is determined at creation time and does not change. Each partition is small enough that rebalancing means moving entire partitions between nodes, not splitting them.

- **Kafka**: Topic partition count is set at creation. Adding partitions is possible but breaks key ordering guarantees and is rarely done. Partitions are assigned to brokers and rebalanced by moving whole partitions.
- **Redis Cluster**: Fixed 16,384 hash slots. Slots are assigned to nodes and can be migrated, but the slot count never changes.
- **Riak**: Fixed ring size (default 64 or 256 partitions). The ring is divided at creation and partitions are redistributed among nodes.

The key design decision for fixed-partition systems: **choose the partition count correctly at creation time**. Too few partitions limit your scaling ceiling. Too many waste resources and increase metadata overhead. The rule of thumb: set partition count to several times your maximum expected node count (e.g., 10x). Kafka's recommendation is: start with `max(expected_throughput / partition_throughput, expected_node_count * partitions_per_node)`.

**Dynamic partitioning**: Partitions split and merge automatically based on size and load.

- **DynamoDB**: Partitions split automatically when they exceed 10GB or their provisioned throughput limits. Splits are transparent to the application.
- **CockroachDB**: Ranges split at 512MB (default) and merge when adjacent ranges are both below 256MB. This is fully automatic.
- **HBase**: Regions split when they exceed a configurable size threshold (default 10GB). Splits require a brief unavailability window for the splitting region.

```
STATIC vs DYNAMIC PARTITIONING:

Static (e.g., Kafka with 12 partitions, 3 nodes):

  Initial:  Node A: [P0, P1, P2, P3]    Scale to 4 nodes:
            Node B: [P4, P5, P6, P7]    Node A: [P0, P1, P2]
            Node C: [P8, P9, P10, P11]  Node B: [P3, P4, P5]
                                         Node C: [P6, P7, P8]
  Move whole partitions, never split.   Node D: [P9, P10, P11]

Dynamic (e.g., CockroachDB):

  Initial:  One range [A-Z] on Node A.

  As data grows:
    [A-Z] splits into [A-M] and [N-Z].
    [A-M] splits into [A-F] and [G-M].
    ... continues splitting as needed.

  Ranges can also be moved across nodes for load balancing.
```

### 6.2 Rebalancing Strategies

#### 6.2.1 Fully Automatic (DynamoDB)

DynamoDB manages all partition splitting, merging, and placement automatically. The operator has no direct control over partition assignment. Adaptive capacity automatically redistributes throughput across partitions to handle hot partitions. The advantage is zero operational burden; the disadvantage is less predictability and the inability to manually control data placement.

#### 6.2.2 Semi-Automatic (CockroachDB)

CockroachDB automatically splits, merges, and rebalances ranges based on configurable thresholds, but operators can influence behavior through zone configurations (e.g., pin certain data to specific regions) and can manually trigger rebalancing. The system provides visibility into range distribution and allows operators to set constraints.

#### 6.2.3 Manual (Kafka)

Kafka's partition reassignment is manual by default. Operators use tools like `kafka-reassign-partitions.sh` to create a reassignment plan and execute it. Cruise Control (LinkedIn's open-source tool) automates this with goal-based rebalancing (balance disk, CPU, network, and leader distribution). Manual control gives operators full predictability but requires operational expertise and monitoring.

### 6.3 Rebalancing Without Downtime

The fundamental challenge: during rebalancing, a partition is being moved from Node A to Node B. While data is in transit, requests for that partition must still be served.

```
ZERO-DOWNTIME REBALANCING PATTERN:

Phase 1: COPY
  Node A (source): continues serving reads and writes for partition P
  Node B (target): receives a copy of partition P's data in the background

  ┌──────┐  all reads/writes  ┌──────┐
  │Client├───────────────────→│Node A│──── background copy ───→ Node B
  └──────┘                    └──────┘

Phase 2: CATCH-UP
  Node A: still primary, continues serving
  Node B: replays the write-ahead log to catch up on writes that
          occurred during the copy phase

Phase 3: CUTOVER (brief)
  Node A: pauses writes for partition P (typically < 100ms)
  Node B: finishes applying the last writes
  Routing table updated: partition P → Node B

Phase 4: CLEANUP
  Node B: now serves all reads and writes for partition P
  Node A: deletes its copy of partition P (after a safety period)
```

**Redis Cluster's MIGRATING/IMPORTING approach**: During slot migration from Node A to Node B, the slot is marked as MIGRATING on A and IMPORTING on B. Requests for keys already migrated are redirected to B with an ASK redirect. New writes can go to either node depending on key migration status. This allows key-by-key migration without a full copy phase.

### 6.4 The Hot Partition Problem

A partition receiving disproportionately high traffic relative to other partitions. This is the most common production sharding problem.

**Causes**:
- Celebrity/power-law keys: One user has 100M followers and their profile is read 100x more than average.
- Temporal hotspots: Today's date is the write key for a time-series system; all writes go to one partition.
- Poor partition key choice: Partitioning by country and 40% of users are in the US.

**Solutions**:

**Key salting / compound keys**: Append a random suffix (0-9) to hot keys, spreading them across 10 partitions. The reader must scatter-gather across all 10 variants and merge results.

```
KEY SALTING:

Hot key: "celebrity:12345"

Without salting: all reads/writes → one partition.

With salting (salt range 0-9):
  Write: choose random salt → "celebrity:12345:7" → hash → partition
  Read:  query all 10 variants:
         "celebrity:12345:0" → partition P3
         "celebrity:12345:1" → partition P7
         "celebrity:12345:2" → partition P1
         ...
         "celebrity:12345:9" → partition P5
         Merge results client-side.

Trade-off: 10x read amplification for 10x write throughput.
Only salt keys that are actually hot.
```

**Split on load, not just size**: DynamoDB's adaptive capacity detects that a partition is being throttled due to a hot key and splits the partition to isolate the hot key on its own partition, which then receives a larger share of throughput capacity.

**Local aggregation / write buffering**: For counters and aggregations on hot keys, batch writes locally (e.g., accumulate 100 increments) and flush periodically, reducing write QPS by the batch factor.

**Read replicas for hot keys**: Route reads for known-hot keys to dedicated read replicas. Instagram does this for celebrity profiles -- they are cached in a dedicated cache tier separate from the general user cache.

---

## 7. Routing: How Clients Find the Right Partition

Once data is partitioned, every request must be routed to the correct partition. There are three fundamental approaches, and every system uses one or a combination.

### 7.1 Approach 1: Client-Side Routing

The client maintains a copy of the partition map and routes directly to the correct node. No intermediate hop.

```
CLIENT-SIDE ROUTING:

Client has partition map: {P0: Node A, P1: Node B, P2: Node C, ...}

  ┌──────────────────┐
  │     Client       │
  │                  │
  │ Partition map:   │
  │  P0 → Node A    │──── key "foo" → hash → P1 → direct to Node B
  │  P1 → Node B    │
  │  P2 → Node C    │
  └──────────────────┘
         │
    Direct connection
         │
         ▼
  ┌──────────────────┐
  │     Node B       │
  │  (owns P1)       │
  └──────────────────┘
```

**Examples**:
- **Redis Cluster**: The client library (e.g., redis-py-cluster, Jedis) maintains a map of hash slots to nodes. The client computes `CRC16(key) % 16384` and routes to the slot owner directly.
- **Cassandra driver**: The DataStax driver maintains a token map and routes requests to the correct coordinator or directly to the replica owner (token-aware routing).
- **Kafka producer**: The producer computes `hash(key) % partition_count` and sends directly to the partition leader.

**Advantages**: Lowest latency (no extra hop). No routing tier to provision and scale.
**Disadvantages**: Client must be "smart" and keep the partition map up-to-date. Every client library in every language must implement routing logic. Map staleness causes misdirected requests.

### 7.2 Approach 2: Routing Tier / Proxy

The client sends all requests to a proxy layer, which knows the partition map and forwards to the correct node.

```
PROXY-BASED ROUTING:

  ┌──────────┐     ┌──────────────┐     ┌──────────┐
  │  Client  │────→│    Proxy     │────→│  Node B  │
  │ (simple) │     │              │     │ (owns P1)│
  └──────────┘     │ Partition map│     └──────────┘
                   │ P0→A, P1→B  │
                   │ P2→C, ...   │
                   └──────────────┘
```

**Examples**:
- **MongoDB mongos**: A query router that receives queries from clients, determines which shard holds the relevant data using the config servers, and routes accordingly.
- **Vitess (YouTube's MySQL sharding)**: vtgate proxy routes queries to the correct vttablet based on the vindex (virtual index) mapping.
- **Twemproxy (nutcracker)**: A proxy for memcached/Redis that handles consistent hashing on behalf of dumb clients.
- **Envoy / HAProxy with consistent hashing**: Can route requests based on a hash of a header or URL parameter to a consistent backend.

**Advantages**: Simple clients (any HTTP client works). Centralized routing logic -- easier to update. Can add cross-cutting concerns (rate limiting, authentication, logging) at the proxy.
**Disadvantages**: Extra network hop (adds 0.1-1ms latency). Proxy is a potential bottleneck and must be horizontally scaled. Proxy failure requires failover.

### 7.3 Approach 3: Any-Node Routing (Coordinator Pattern)

The client connects to any node. That node acts as a coordinator: it determines which node owns the requested key and forwards the request internally. The client does not need to know the partition map.

```
ANY-NODE ROUTING (Coordinator):

  ┌──────────┐     ┌──────────┐  internal forward  ┌──────────┐
  │  Client  │────→│  Node A  │───────────────────→│  Node B  │
  │          │     │(coordinator)                   │(owns P1) │
  └──────────┘     └──────────┘                     └──────────┘
                        │                                │
                        │←────────── response ───────────┘
                        │
                   return to client
```

**Examples**:
- **Cassandra coordinator**: Any node can serve as coordinator. The coordinator determines which replicas hold the requested data (based on the token ring), forwards the request, and assembles the response.
- **Elasticsearch**: Any node can receive a request and coordinate a scatter-gather across the relevant shards.
- **CockroachDB**: Any node can receive a SQL query. The gateway node plans the query and distributes it to the leaseholders of the relevant ranges.

**Advantages**: Simple client (connect to any node, no routing knowledge needed). Built-in load balancing (if client round-robins across nodes). No separate proxy tier to manage.
**Disadvantages**: Extra internal hop for requests that happen to land on a non-owner node. The coordinator node uses CPU and network for forwarding. Under high load, coordinator nodes can become bottlenecks.

### 7.4 Partition Map Propagation

How do nodes and clients learn and update the partition map?

**Gossip-based propagation**: Each node maintains its view of the partition map and periodically exchanges it with random peers. Eventually, all nodes converge on the same map. Used by Cassandra, Riak, and DynamoDB (internally).

```
GOSSIP-BASED PROPAGATION:

  Time T=0: Node D joins, only Node A knows.
  
  Node A: "D owns tokens [500-600]"
       │
       ├──gossip──→ Node B (now knows about D)
       │
  T=1: Node B
       ├──gossip──→ Node C (now knows about D)
       │
  T=2: All nodes know about D.

  Convergence time: O(log N) gossip rounds for N nodes.
  For 1000 nodes with 1-second gossip intervals: ~10 seconds to converge.
```

**Centralized metadata (ZooKeeper / etcd)**: A separate coordination service holds the authoritative partition map. Nodes register themselves and their partition assignments. Clients watch for changes and update their local copy.

```
CENTRALIZED METADATA:

  ┌──────────┐  register    ┌─────────────┐  watch    ┌──────────┐
  │  Node A  │─────────────→│  ZooKeeper  │←──────────│  Client  │
  │  Node B  │─────────────→│  / etcd     │←──────────│  Client  │
  │  Node C  │─────────────→│             │←──────────│  Client  │
  └──────────┘              └─────────────┘           └──────────┘

  Authoritative, consistent, but adds a dependency on the coordinator.
```

- **Kafka**: Uses ZooKeeper (or KRaft in newer versions) to store partition leader assignments. Clients fetch metadata from any broker.
- **Redis Cluster**: Uses gossip (the CLUSTER protocol) -- no external dependency.
- **MongoDB**: Config servers (a replica set) hold the chunk-to-shard mapping.

### 7.5 Handling Stale Routing Tables: MOVED and ASK Redirects

When a client sends a request to the wrong node (because its partition map is stale), the node must either forward the request or tell the client where to go.

**Redis Cluster's approach**: If a client sends a command for a key in slot X to a node that does not own slot X, the node responds with:
- `MOVED slot host:port` -- a permanent redirect. The slot has moved; update your routing table.
- `ASK slot host:port` -- a temporary redirect. The slot is being migrated; try the other node for this one request, but keep your routing table as-is.

```
REDIS CLUSTER REDIRECT FLOW:

Client partition map (stale): slot 5000 → Node A

  Client ──── GET foo (slot 5000) ────→ Node A
  Node A ──── -MOVED 5000 10.0.0.2:6379 ────→ Client

  Client updates map: slot 5000 → 10.0.0.2 (Node B)
  Client ──── GET foo ────→ Node B
  Node B ──── "bar" ────→ Client

During migration (slot 5000 migrating from A to B):
  Client ──── GET foo ────→ Node A
  If key "foo" already migrated to B:
    Node A ──── -ASK 5000 10.0.0.2:6379 ────→ Client
    Client ──── ASKING ────→ Node B
    Client ──── GET foo ────→ Node B
    Node B ──── "bar" ────→ Client
  (Client does NOT update its routing table for ASK)
```

This pattern is common across distributed systems. Cassandra uses a similar approach where the coordinator transparently forwards to the correct replica, shielding the client from routing details.

---

## 8. Replication + Partitioning Interaction

In production, every partition is replicated. This section covers how replication and partitioning interact.

### 8.1 Each Partition Is a Replication Group

A partition with replication factor RF=3 has 3 copies: one leader and two followers. The leader handles writes (and often reads). Followers replicate the leader's log and serve as failover targets.

```
PARTITION REPLICATION:

Cluster: 5 nodes, 4 partitions, RF=3

  ┌────────┬────────┬────────┬────────┬────────┐
  │ Node 1 │ Node 2 │ Node 3 │ Node 4 │ Node 5 │
  ├────────┼────────┼────────┼────────┼────────┤
  │ P1(L)  │ P1(F)  │ P1(F)  │        │        │
  │ P2(F)  │ P2(L)  │        │ P2(F)  │        │
  │        │ P3(F)  │ P3(L)  │        │ P3(F)  │
  │ P4(F)  │        │        │ P4(L)  │ P4(F)  │
  └────────┴────────┴────────┴────────┴────────┘

  L = leader, F = follower

  - Each partition has exactly 1 leader and 2 followers.
  - Leaders are spread across nodes for write load distribution.
  - Any node failure loses at most 1 leader and some followers.
```

### 8.2 Quorum Operations Across Replicas

For a partition with RF=3, a quorum write requires W=2 acknowledgments (from the leader and at least one follower), and a quorum read requires R=2 responses. The rule W + R > RF guarantees that a read sees the latest write.

```
QUORUM WRITE (W=2, RF=3):

Client ──── WRITE ────→ P1 Leader (Node 1)
                            │
                   ┌────────┴────────┐
                   ▼                 ▼
              P1 Follower       P1 Follower
              (Node 2)          (Node 3)
                   │                 │
              ACK ─┘                 │ (slow, but W=2 already met)
                                     └─ ACK (arrives later)

Leader ACK + 1 Follower ACK = 2 ≥ W → write acknowledged to client.
Third follower catches up asynchronously.
```

### 8.3 The Dynamo Replica Placement Model

Amazon's Dynamo (the design paper behind DynamoDB, Riak, and Cassandra) places replicas on the consistent hash ring. For a key, the N replicas are placed on the next N **distinct physical nodes** clockwise from the key's position.

```
DYNAMO REPLICA PLACEMENT (N=3):

Hash ring with virtual nodes, but replicas must be on distinct physical nodes:

Ring positions (vnodes):
  A1(50) B1(120) A2(200) C1(350) B2(500) C2(650) A3(800) B3(900)

Key K hashes to position 160.

Walk clockwise from 160:
  1st vnode: A2(200) → physical node A → Replica 1: Node A
  2nd vnode: C1(350) → physical node C → Replica 2: Node C  (different from A)
  3rd vnode: B2(500) → physical node B → Replica 3: Node B  (different from A, C)

Done. K is stored on nodes A, C, and B.

Note: if we had A2(200), A3(250), C1(350)...
  We skip A3 because Node A is already in the replica set.
  The 2nd replica goes to C1(350) → Node C.
```

### 8.4 Rack-Aware and AZ-Aware Replica Placement

Placing all 3 replicas in the same rack or availability zone means a rack failure or AZ outage loses all copies. Production systems enforce placement constraints:

```
AZ-AWARE REPLICA PLACEMENT:

3 Availability Zones, RF=3:

  AZ-1              AZ-2              AZ-3
  ┌──────────┐      ┌──────────┐      ┌──────────┐
  │ Node 1   │      │ Node 3   │      │ Node 5   │
  │ Node 2   │      │ Node 4   │      │ Node 6   │
  └──────────┘      └──────────┘      └──────────┘

  Partition P1: Leader on Node 1 (AZ-1),
                Follower on Node 3 (AZ-2),
                Follower on Node 5 (AZ-3)

  Rule: no two replicas of the same partition in the same AZ.
  Survives: any single AZ outage without data loss.
```

Cassandra implements rack-aware placement with the `NetworkTopologyStrategy` replication strategy. You specify how many replicas per datacenter, and Cassandra places them on distinct racks within each datacenter using a strategy that walks the ring and skips nodes in racks already represented.

CockroachDB uses zone configurations to specify diversity constraints (e.g., "replicas must be in at least 3 distinct regions").

---

## 9. Secondary Indexes on Partitioned Data

When data is partitioned by a primary key, queries on other fields (secondary indexes) become complicated. There are two fundamental approaches.

### 9.1 Local (Document-Partitioned) Indexes

Each partition maintains its own secondary index covering only the data in that partition. A query on the secondary index must scatter to all partitions and gather results.

```
LOCAL (DOCUMENT-PARTITIONED) INDEX:

Data partitioned by user_id. Secondary index on city.

Partition 1 (user 1-1000):         Partition 2 (user 1001-2000):
  Data: user 42 {city: "NYC"}        Data: user 1500 {city: "NYC"}
        user 99 {city: "SF"}               user 1700 {city: "LA"}

  Local index:                        Local index:
    "NYC" → [42]                        "NYC" → [1500]
    "SF"  → [99]                        "LA"  → [1700]

Query: "Find all users in NYC"
  → Must scatter to ALL partitions
  → P1 returns [42], P2 returns [1500]
  → Client merges: [42, 1500]

  Scatter-gather across all partitions. Latency = max(partition response times).
```

**Used by**: MongoDB (each shard maintains its own secondary indexes), Elasticsearch (each shard has its own inverted index), Cassandra (secondary indexes are local to each node).

**Trade-off**: Writes are simple (update only the local index), but reads on the secondary index are expensive (scatter-gather). With 1000 partitions, every secondary index query fans out to 1000 nodes.

### 9.2 Global (Term-Partitioned) Indexes

The secondary index itself is partitioned, but by the indexed term rather than by the document's primary key. A query for a specific term hits only the partition that owns that index term.

```
GLOBAL (TERM-PARTITIONED) INDEX:

Data partitioned by user_id. Global index on city, partitioned by city name.

Data partitions:                     Global index partitions:
  P1: user 42 {city: "NYC"}           Index P-A: cities A-L
  P2: user 1500 {city: "NYC"}            "LA" → [user 1700 on P2]
  P3: user 1700 {city: "LA"}           Index P-B: cities M-Z
                                          "NYC" → [user 42 on P1, user 1500 on P2]
                                          "SF"  → [user 99 on P1]

Query: "Find all users in NYC"
  → Hash/range lookup on index: "NYC" → Index P-B
  → Index P-B returns: [user 42, user 1500]
  → Single partition hit! No scatter-gather.

Write: user 42 moves from NYC to LA
  → Update data on P1
  → Update global index P-B: remove user 42 from "NYC"
  → Update global index P-A: add user 42 to "LA"
  → Write touches multiple partitions → distributed transaction or async update
```

**Used by**: DynamoDB Global Secondary Indexes (GSIs), Google's Spanner.

**Trade-off**: Reads on the secondary index are efficient (single partition), but writes are expensive (must update the data partition AND the relevant index partitions, potentially requiring distributed transactions or accepting eventual consistency of the index).

### 9.3 The Design Decision

```
LOCAL vs GLOBAL SECONDARY INDEX:

┌─────────────────────┬──────────────────────┬──────────────────────┐
│                     │ Local (per-partition) │ Global (partitioned  │
│                     │                      │ by term)             │
├─────────────────────┼──────────────────────┼──────────────────────┤
│ Write cost          │ Low (local update)   │ High (cross-partition│
│                     │                      │ update)              │
├─────────────────────┼──────────────────────┼──────────────────────┤
│ Read cost           │ High (scatter-gather │ Low (single partition│
│ (secondary query)   │ across all partitions│ for exact match)     │
│                     │ )                    │                      │
├─────────────────────┼──────────────────────┼──────────────────────┤
│ Consistency         │ Immediate (same      │ Often eventual       │
│                     │ partition)           │ (async index update) │
├─────────────────────┼──────────────────────┼──────────────────────┤
│ Best for            │ Write-heavy, reads   │ Read-heavy on        │
│                     │ mostly by primary key│ secondary attributes │
└─────────────────────┴──────────────────────┴──────────────────────┘
```

In interviews, state this trade-off explicitly. If your system is read-heavy on a secondary attribute (e.g., "find all items by category"), a global index is worth the write overhead. If secondary queries are rare, local indexes avoid cross-partition writes.

---

## 10. Real-World Systems Deep Dives

### 10.1 DynamoDB

**Partitioning scheme**: Hash partitioning on the partition key. The hash function maps the partition key to a position in the internal hash space, and each partition owns a contiguous range of that space.

**Partition sizing**: Each partition can hold up to 10GB of data and supports up to 3000 RCU (read capacity units) or 1000 WCU (write capacity units). When either limit is exceeded, the partition automatically splits.

**Adaptive capacity**: DynamoDB monitors throughput at the partition level. If a partition is being throttled because a hot key is consuming most of the partition's capacity, DynamoDB automatically splits the partition to isolate the hot item, giving it dedicated capacity. This is "burst capacity" plus "adaptive capacity" working together.

**Global secondary indexes**: DynamoDB GSIs are essentially separate tables with their own partition key. When you write to the base table, DynamoDB asynchronously updates all GSIs. GSIs are eventually consistent (you cannot do a strongly consistent read on a GSI).

**Practical implications for interviews**: When designing with DynamoDB, the partition key is the most critical decision. A bad partition key (e.g., date for a time-series workload) creates hotspots. A good partition key has high cardinality and relatively uniform access (e.g., user_id). Composite keys (partition key + sort key) enable range queries within a partition.

```
DYNAMODB PARTITIONING:

Table: UserActivity
  Partition key: user_id
  Sort key: timestamp

Write: {user_id: "U123", timestamp: "2024-01-15T10:30:00Z", action: "click"}
  → hash("U123") → internal hash space → Partition P7
  → Within P7, sorted by timestamp → efficient range queries per user

Query: "Get all activity for U123 between Jan 1 and Jan 31"
  → hash("U123") → P7 → range scan on sort key within P7
  → Hits exactly one partition. Efficient.

Anti-pattern: Partition key = date ("2024-01-15")
  → All writes today go to one partition → hot partition → throttling
```

### 10.2 Cassandra

**Partitioning scheme**: Consistent hashing with virtual nodes (Murmur3 hash function). Each node owns `num_tokens` (default 256) tokens on the ring. The Murmur3 partitioner hashes the partition key to a 64-bit value, which is mapped to the ring.

**Token assignment**: When a node joins the cluster, it is assigned `num_tokens` random positions on the ring (or computed positions using the new token allocation algorithm in Cassandra 4.0+). The gossip protocol propagates the updated token ring to all nodes.

**Replication**: Controlled by the replication strategy. `SimpleStrategy` places replicas on the next N distinct nodes clockwise on the ring. `NetworkTopologyStrategy` places a specified number of replicas per datacenter, walking the ring and selecting nodes in distinct racks.

**Consistency levels**: Per-query tunable. `ONE` (fast, eventual), `QUORUM` (W/2+1 replicas), `ALL` (strong but slow), `LOCAL_QUORUM` (quorum within the local datacenter).

```
CASSANDRA TOKEN RING:

4 nodes (A, B, C, D), num_tokens=2 each (simplified), RF=3

Token ring (Murmur3, 64-bit, range [-2^63, 2^63)):
  A1(-8000) B1(-4000) C1(0) D1(3000) A2(5000) B2(7000) C2(9000) D2(11000)

Key "user:42" → Murmur3("user:42") = 1500 → walk clockwise → D1(3000)
  Replicas (RF=3): D (at 3000), A (at 5000), B (at 7000)
  → Write at QUORUM: must write to 2 of {D, A, B}
  → Read at QUORUM: must read from 2 of {D, A, B}

Gossip protocol:
  Every second, each node contacts a random peer and exchanges
  its view of the cluster state (tokens, load, status).
  New nodes converge into the cluster view in O(log N) rounds.
```

### 10.3 Redis Cluster

**Partitioning scheme**: Fixed 16,384 hash slots. Key assignment: `slot = CRC16(key) % 16384`. Each node is assigned a subset of slots. There is no consistent hash ring -- slots are explicitly mapped to nodes.

**Hash tags**: Redis Cluster allows grouping related keys on the same slot using hash tags: `{user:42}.profile` and `{user:42}.sessions` both hash on `user:42`, so they land on the same slot. This enables multi-key operations (which are only supported within a single slot).

**Resharding**: Slots can be migrated between nodes one at a time. During migration, the source node marks the slot as MIGRATING and the target marks it as IMPORTING. Keys are moved individually with the MIGRATE command. MOVED and ASK redirects handle requests for keys in transit.

```
REDIS CLUSTER:

16,384 hash slots distributed across 3 masters (each with a replica):

Master A (+ Replica A'): slots 0-5460
Master B (+ Replica B'): slots 5461-10922
Master C (+ Replica C'): slots 10923-16383

  GET mykey
  → CRC16("mykey") = 50839 → 50839 % 16384 = 1687 → slot 1687 → Master A

  SET {order:123}.items "..."
  SET {order:123}.total 42.50
  → Both hash on "order:123" → same slot → multi-key atomic ops work.

Resharding (moving slots 0-1000 from A to B):
  1. Mark slots 0-1000 as MIGRATING on A, IMPORTING on B.
  2. For each key in slots 0-1000: MIGRATE key from A to B.
  3. After all keys moved: update slot ownership in cluster config.
  4. All nodes informed via gossip; clients get MOVED for stale routes.
```

### 10.4 Kafka

**Partitioning scheme**: Topic partitions (fixed count at creation). Each partition is assigned to a broker (leader) with replication. The partition for a record is determined by: (1) if the key is non-null: `hash(key) % partition_count` (Murmur2 by default), (2) if the key is null: round-robin or sticky partitioning (batches go to the same partition for better compression).

**Consumer group rebalancing**: When a consumer joins or leaves a group, partitions are reassigned among the remaining consumers. This is the "rebalance" that every Kafka user dreads.

- **Eager (stop-the-world)**: All consumers revoke all partitions, then reassign. Brief period where no consumer processes anything. Legacy default.
- **Cooperative (incremental)**: Only the partitions that need to move are revoked. Other consumers continue processing. Available since Kafka 2.4.
- **Sticky assignment**: Tries to minimize partition movement between rebalances by keeping existing assignments stable. Combined with cooperative, this is the recommended strategy.

```
KAFKA CONSUMER GROUP REBALANCING:

Topic: events (6 partitions)
Consumer Group: analytics (3 consumers)

Steady state:
  Consumer 1: [P0, P1]
  Consumer 2: [P2, P3]
  Consumer 3: [P4, P5]

Consumer 3 crashes:

Eager rebalance:
  1. Consumers 1 and 2 revoke ALL partitions.    ← Full stop!
  2. Reassign: C1 gets [P0, P1, P4], C2 gets [P2, P3, P5].
  3. Consumers resume.
  Downtime: entire rebalance duration (seconds to minutes).

Cooperative sticky rebalance:
  1. Only P4 and P5 need new owners.              ← Minimal disruption
  2. C1 keeps [P0, P1], gets P4.
  3. C2 keeps [P2, P3], gets P5.
  4. C1 and C2 continue processing P0-P3 throughout.
  Downtime: zero for P0-P3; brief for P4, P5.
```

### 10.5 CockroachDB

**Partitioning scheme**: Range-based. All data is sorted by key (the primary key in encoded form) and divided into ranges (default target: 512MB). Each range is a unit of replication and distribution.

**Automatic split/merge**: Ranges split when they exceed the size threshold (512MB) or the write load threshold. Ranges merge when adjacent ranges are both well below the threshold. Splits and merges are automatic and transparent.

**Leaseholder routing**: For each range, one replica holds the "lease" -- the right to serve reads and coordinate writes. The leaseholder is typically the replica closest to where the load originates. The gateway node (the node that receives the SQL query) routes sub-requests to the leaseholder of each relevant range.

**Geo-partitioning**: CockroachDB supports partitioning tables by location (e.g., rows with `region='eu'` stored in EU datacenters) using zone configurations. This enables data residency compliance while maintaining a single logical table.

```
COCKROACHDB RANGE MANAGEMENT:

Table: users (primary key: user_id)

Stored as sorted key-value pairs:
  /users/1 → {name: "Alice", ...}
  /users/2 → {name: "Bob", ...}
  ...
  /users/1000000 → {name: "Zara", ...}

Ranges (auto-split at 512MB):
  Range 1: /users/1 - /users/250000      (Node 1 leaseholder)
  Range 2: /users/250001 - /users/500000  (Node 2 leaseholder)
  Range 3: /users/500001 - /users/750000  (Node 3 leaseholder)
  Range 4: /users/750001 - /users/1000000 (Node 1 leaseholder)

  Each range replicated RF=3 across nodes.
  Ranges automatically split/merge as data grows/shrinks.

Query: SELECT * FROM users WHERE user_id = 600000
  → Gateway node determines: key /users/600000 → Range 3 → Node 3
  → Routes to Node 3's leaseholder for Range 3
  → Single-node read, no scatter-gather.
```

---

## 11. Partitioning for ML/AI Systems

This section bridges the general partitioning theory to the specific needs of ML/AI systems. These patterns appear directly in the solutions for feature stores, recommendation systems, embedding search, model serving, and parallel training.

### 11.1 Feature Store Online Serving

The online store of a feature store (see `solutions/feature-store-design.md`) serves pre-computed features at inference time with strict latency requirements (p99 < 10ms).

**Partition key**: `entity_id` (user_id, item_id, session_id, etc.)

**Storage backend**: Typically Redis Cluster or DynamoDB.
- Redis Cluster: `CRC16(entity_id) % 16384 → hash slot → node`. Each entity's features are stored as a hash or serialized blob at that slot.
- DynamoDB: `entity_id` is the partition key. Features are columns. Sort key can be feature group name or version timestamp for point-in-time lookups.

**Why this partition key works**: Feature lookups at inference time are point queries: "give me all features for user U123." The entity_id distributes uniformly (user IDs are typically UUIDs or incrementing integers), and each lookup hits exactly one partition -- no scatter-gather.

**Hot entity problem**: In recommendation systems, a small number of entities (popular items, viral content) receive disproportionate feature lookups. Solutions:
- Read replicas for the online store (RF=3 with reads distributed across replicas).
- Dedicated cache tier in front of the feature store for the top-K hottest entities.
- For DynamoDB: DAX (DynamoDB Accelerator) as an in-memory cache.

```
FEATURE STORE ONLINE SERVING:

Inference request: "Recommend items for user U42"

  ML Model needs features:
    user_features(U42)  → entity_id = "user:U42"
    item_features(I789) → entity_id = "item:I789"

  Feature Store (Redis Cluster):
    CRC16("user:U42") % 16384 = slot 3821 → Node B → HGETALL "user:U42"
    CRC16("item:I789") % 16384 = slot 9100 → Node C → HGETALL "item:I789"

  Two point lookups, two different partitions, parallel execution.
  Total latency: max(lookup_1, lookup_2) ≈ 1-3ms each.
```

### 11.2 Embedding Index Partitioning (HNSW / IVF)

Vector similarity search (used in AI search engines, recommendation candidate retrieval, and RAG systems) requires special partitioning because the query is a vector, not a discrete key.

**IVF (Inverted File Index) partitioning**: The embedding space is clustered into C clusters using k-means. Each cluster centroid defines a partition. At query time, the query vector is compared to all centroids, and the top-`nprobe` clusters are searched.

```
IVF-BASED EMBEDDING PARTITIONING:

Training: k-means clusters 10M embeddings into 1000 clusters.

Cluster assignment:
  Partition 0: embeddings closest to centroid_0  (~10K vectors)
  Partition 1: embeddings closest to centroid_1  (~10K vectors)
  ...
  Partition 999: embeddings closest to centroid_999 (~10K vectors)

Query: "Find 10 nearest neighbors of query vector q"
  1. Compare q to all 1000 centroids → find top nprobe=20 closest.
  2. Search within those 20 partitions (scatter to 20 nodes).
  3. Merge results, return global top-10.

  Scatter-gather is inherent but bounded (nprobe << total partitions).
```

**HNSW partitioning**: HNSW graphs do not partition naturally. Options:
- **Replicate the full index**: If it fits in memory on one node, replicate it for read throughput. Simple, but capped by single-node memory.
- **Shard by entity range**: Split the corpus (e.g., by entity_id range) and build independent HNSW indexes per shard. Query all shards and merge. This is what Elasticsearch / OpenSearch does with its vector search across shards.
- **Hybrid**: Use IVF for coarse partitioning (which cluster), then HNSW within each cluster for fine search. This is the approach used by Milvus and Pinecone internally.

**Recall vs partition count trade-off**: More partitions means each partition's HNSW graph is smaller (faster) but the scatter-gather has more overhead, and boundary effects reduce recall (nearest neighbors may be in adjacent partitions). Typical target: 10-100 partitions for most production vector search systems.

### 11.3 Model Serving Routing

In multi-model serving platforms (see `solutions/ml-inference-platform-design.md`), different models may be loaded on different GPU nodes. Request routing must send each inference request to a node that has the correct model loaded.

**Model-based partitioning**: Each GPU node hosts a subset of models. A routing layer (Envoy, custom proxy) maintains a model-to-node map and routes requests accordingly. This is effectively directory-based partitioning where the partition key is the model name.

**A/B testing and canary routing**: For model experiments, consistent hashing on user_id ensures the same user always hits the same model variant, preventing inconsistent behavior within a session.

```
MODEL SERVING ROUTING:

Model registry:
  model_v1 → GPU Nodes [A, B]  (replicated for throughput)
  model_v2 → GPU Nodes [C]     (canary, 10% traffic)
  model_v3 → GPU Nodes [D, E]  (different model type)

Request routing:
  Request {model: "v1", user: "U42", input: ...}
  → Routing proxy → consistent_hash("U42") among [A, B] → Node A

  A/B test routing (10% canary):
  → hash("U42") % 100 = 37 → ≥ 10 → route to model_v1 (Node A or B)
  → hash("U99") % 100 = 7  → < 10 → route to model_v2 (Node C)
```

### 11.4 Training Data Sharding

In distributed ML training (see `solutions/parallel-ml-training-design.md`), the training dataset must be split across worker nodes. The partitioning strategy affects training efficiency.

**Data parallelism**: Each worker gets a roughly equal-sized shard of the training data. Sharding approaches:
- **File-based**: Training data is stored as many small files (e.g., TFRecord shards, Parquet files). Files are assigned to workers round-robin or by consistent hash of filename.
- **Row-based**: A single large dataset is split into chunks. Worker i reads rows `[i * chunk_size, (i+1) * chunk_size)`.
- **Streaming shuffle**: For each epoch, the data is reshuffled and re-partitioned. This prevents workers from seeing the same examples in the same order, improving training convergence.

**Key concern**: Load balance across workers. If one worker's shard takes longer to process (due to longer sequences, more complex examples, or data skew), all workers wait at the synchronization barrier (in synchronous training). Solutions: equal-size shards (by bytes, not by record count), dynamic work stealing, and overpartitioning (more shards than workers, assign dynamically).

### 11.5 Recommendation Candidate Generation

Large-scale recommendation systems (see `solutions/recommendation-system-design.md`) partition the item corpus for parallel candidate retrieval.

**Pattern**: The item corpus (e.g., 100M items) is partitioned across retrieval nodes. Each node builds an ANN index over its partition. At query time, the user embedding is sent to all retrieval nodes (scatter), each returns its local top-K candidates, and the results are merged (gather).

```
RECOMMENDATION CANDIDATE RETRIEVAL:

Item corpus: 100M items, partitioned across 50 retrieval nodes.
Each node: 2M items with a local HNSW index.

Query: "Find 200 candidate items for user U42"
  1. Compute user embedding for U42.
  2. Scatter: send embedding to all 50 retrieval nodes.
  3. Each node: search local HNSW index → return top-200 local candidates.
  4. Gather: merge 50 × 200 = 10,000 candidates.
  5. Re-rank: score top-200 from the merged set using the ranking model.

  Parallelism: 50 nodes searched simultaneously.
  Per-node latency: ~5ms for HNSW search of 2M vectors.
  Total latency: ~5ms (parallel) + ~2ms (merge) = ~7ms.
  vs single node with 100M vectors: ~50ms+ (if it even fits in memory).
```

---

## 12. Capacity Planning Math

Concrete formulas for partition sizing and throughput estimation. These are the numbers you should be able to produce in an interview whiteboard session.

### 12.1 Storage Sizing

```
STORAGE FORMULA:

  Total storage = N_items × size_per_item × replication_factor

  Example:
    N_items = 1 billion (10^9)
    size_per_item = 2 KB (a typical feature store entity)
    RF = 3

    Total storage = 10^9 × 2 KB × 3 = 6 TB

  Partition count:
    target_partition_size = 1 GB (reasonable for most systems)
    partitions = total_storage / target_partition_size = 6 TB / 1 GB = 6,000

  Nodes:
    target_storage_per_node = 1 TB (with headroom for compaction, etc.)
    nodes = total_storage / target_storage_per_node = 6 TB / 1 TB = 6

    Partitions per node = 6,000 / 6 = 1,000 partitions per node.
```

### 12.2 Throughput Sizing

```
THROUGHPUT FORMULA:

  QPS per partition = total_QPS / partition_count  (uniform distribution)

  Example:
    total_read_QPS = 500,000
    partition_count = 6,000

    QPS per partition = 500,000 / 6,000 ≈ 83 reads/sec per partition

    Assuming a single Redis node handles ~100K simple reads/sec:
    reads_per_node = 83 × 1,000 partitions/node = 83,000 reads/sec/node
    → Fits within Redis single-node capacity. Good.

  Write throughput:
    total_write_QPS = 50,000
    writes_per_partition = 50,000 / 6,000 ≈ 8 writes/sec per partition
    writes_per_node = 8 × 1,000 = 8,000 writes/sec/node
    → With RF=3, each write is replicated: actual write load = 8,000 × 3 = 24,000
       (if this node is replica for other partitions too)
```

### 12.3 Hotspot Factor

Uniform distribution is the best case. Real workloads follow power laws.

```
HOTSPOT ESTIMATION:

  Zipf distribution: top 1% of keys receive ~20-50% of traffic (varies by alpha).

  Example (conservative: top 1% gets 30% of traffic):
    total_QPS = 500,000
    partition_count = 6,000
    keys_per_partition ≈ 10^9 / 6,000 ≈ 167,000 keys

    If keys are uniformly distributed across partitions, the top 1% of keys
    (1,670 keys) in a hot partition receive 30% of that partition's traffic.

    But the truly dangerous case: a single hot KEY.

    If one key receives 1% of total traffic:
      hot_key_QPS = 500,000 × 0.01 = 5,000 reads/sec on ONE partition.

    If one key receives 10% of total traffic (celebrity/viral):
      hot_key_QPS = 50,000 reads/sec on ONE partition.

    This is why partition-level metrics matter more than cluster-average metrics.
    A cluster averaging 83 QPS/partition may have one partition at 50,000 QPS.

  EFFECTIVE HOTSPOT FACTOR:
    hotspot_factor = max_partition_QPS / average_partition_QPS

    Design target: hotspot_factor < 5x (manageable with replicas)
    Danger zone: hotspot_factor > 20x (needs key salting or dedicated caching)
```

### 12.4 Complete Capacity Planning Example

```
SCENARIO: Feature store for a recommendation system.

Requirements:
  - 500M users, 50M items
  - User features: 4 KB per user (200 features × 20 bytes average)
  - Item features: 2 KB per item (100 features × 20 bytes average)
  - Read QPS: 200K user feature lookups + 2M item feature lookups = 2.2M total
  - Write QPS: 10K feature updates/sec (batch updates from feature pipelines)
  - Latency: p99 < 5ms for reads
  - Availability: 99.99%

Storage:
  User features: 500M × 4 KB = 2 TB
  Item features:  50M × 2 KB = 100 GB
  Total raw: 2.1 TB
  With RF=3: 6.3 TB

Partition strategy: Redis Cluster (hash partitioning)
  16,384 hash slots.
  Target: 10-50 GB per node (Redis is memory-bound).
  Nodes: 6.3 TB / 30 GB per node ≈ 210 nodes (with Redis memory overhead).

  But also constrained by QPS:
    2.2M reads / 210 nodes ≈ 10,500 reads/node/sec → easily within Redis capacity.

  Hotspot: top 0.1% items (50K items) get 50% of item reads = 1M QPS.
    These 50K items span 50K / (50M / 16,384) ≈ 16 slots.
    Per slot: 1M / 16 ≈ 62,500 reads/sec → hot but manageable with replicas.

  Final architecture:
    210 Redis nodes (masters), 210 replicas (RF=2 for Redis Cluster).
    16,384 slots distributed evenly (~39 slots per master).
    Read replicas serve read traffic for hot slots.
    Total memory: ~6.3 TB + overhead ≈ 8 TB across 420 instances.
    Instance type: r6g.2xlarge (64 GB RAM) → 420 / 8 ≈ 53 instances minimum.
    With 64 GB RAM, each instance runs ~8 Redis processes (one per slot group).

  COST ESTIMATE (AWS, on-demand):
    53 × r6g.2xlarge × $0.4032/hr ≈ $21.37/hr ≈ $15,400/month.
    With reserved instances (1-year): ~$9,200/month.
```

---

## 13. Failure Walkthroughs

Understanding how partitioned systems behave under failure is critical for interviews. Walk through each scenario step by step.

### 13.1 Node Failure

```
NODE FAILURE SCENARIO:

Cluster: 5 nodes, 12 partitions, RF=3

  Node 1: P1(L) P3(F) P5(F) P7(L) P9(F)  P11(F)
  Node 2: P1(F) P4(L) P6(F) P8(L) P10(F) P12(F)
  Node 3: P2(L) P4(F) P6(L) P9(L) P11(L) P12(F)
  Node 4: P2(F) P3(L) P5(L) P8(F) P10(L) P12(L)
  Node 5: P1(F) P2(F) P3(F) P7(F) P9(F)  P11(F)

Node 3 crashes.

IMMEDIATE IMPACT:
  - P2, P6, P9, P11: lost their LEADER. Need leader election.
  - P4, P12: lost a FOLLOWER. Reduced redundancy but still available.

RECOVERY STEPS:
  1. Failure detection: phi accrual detector / heartbeat timeout (see Ch. 29).
     Typically 10-30 seconds to declare a node dead.

  2. Leader election for affected partitions:
     - P2: followers on Node 4 and Node 5. Promote Node 4 (most caught up).
     - P6: follower on Node 2. Promote Node 2.
     - P9: followers on Node 1 and Node 5. Promote Node 1.
     - P11: followers on Node 1 and Node 5. Promote Node 1.

  3. Post-election: all partitions have leaders again.
     P4 and P12 now have only 2 replicas (below RF=3).

  4. Re-replication: the cluster creates new replicas for P4 and P12
     on surviving nodes to restore RF=3.
     - P4 new follower on Node 5 (was on Nodes 2, 3; Node 3 dead → add Node 5).
     - P12 new follower on Node 1.

  5. Data streaming: the new followers receive a full snapshot of the partition
     data from the leader, then catch up on the write-ahead log.
     For a 1 GB partition at 100 MB/s network: ~10 seconds to rebuild.

  Total recovery time to full redundancy:
    Detection: 10-30s + election: 1-5s + re-replication: seconds to minutes.
    Availability impact: only partitions whose leader was on Node 3 see a
    brief (1-5 second) write unavailability during leader election.
    Reads from followers (if supported) may continue uninterrupted.
```

### 13.2 Network Partition (Split Brain)

```
NETWORK PARTITION SCENARIO:

5-node cluster splits into two groups:

  Group A: [Node 1, Node 2]          Group B: [Node 3, Node 4, Node 5]
  ──────────────────────────          ──────────────────────────────────
  Can communicate with each other.    Can communicate with each other.
  Cannot reach Group B.               Cannot reach Group A.

Partition P1 (RF=3): Leader on Node 1, Followers on Node 3 and Node 5.

SPLIT BRAIN RISK:
  Group A has the leader (Node 1) but only 1/3 replicas.
  Group B has 2/3 replicas (Nodes 3, 5) but no leader.

WITH MAJORITY QUORUM (W=2, R=2):
  Group A: Leader on Node 1 cannot reach any follower.
           Write quorum = 1 (only self) < W=2 → WRITES BLOCKED.
           The partition self-heals: Node 1 steps down as leader.

  Group B: Nodes 3 and 5 notice the leader is unreachable.
           They elect a new leader (say Node 3) among themselves.
           Write quorum = 2 (Node 3 + Node 5) ≥ W=2 → WRITES PROCEED.

  Result: The majority partition (Group B) continues serving.
          The minority partition (Group A) becomes read-only or unavailable.
          No split brain -- the quorum requirement prevents conflicting writes.

WITH SLOPPY QUORUM (Dynamo-style):
  Writes that cannot reach the preferred replicas are temporarily stored
  on other available nodes (hinted handoff).

  Group A: Node 1 writes to P1 locally and stores a "hinted" copy for
           Node 3 and Node 5 on Node 2.

  Group B: New leader on Node 3 accepts writes.

  DANGER: Both groups accept writes → conflicting versions of the same key.
  Resolution: When the partition heals, conflicting versions are detected
  and resolved using vector clocks or last-writer-wins (application-specific).

  This is the AP trade-off: availability over consistency during partitions.
```

### 13.3 Rebalancing Storm

```
REBALANCING STORM SCENARIO:

A cluster of 50 nodes, 10,000 partitions, 50 TB of data.
Operator adds 10 new nodes simultaneously for capacity expansion.

WHAT HAPPENS:
  Rebalancing starts: each new node must receive ~1/6 of the data (since
  the cluster is growing from 50 to 60 nodes, each new node should get
  50/60 × 200 partitions ≈ 167 partitions).

  Total data movement: 10 nodes × 167 partitions × 1 GB/partition ≈ 1.67 TB.

  If all 10 nodes start rebalancing simultaneously:
    - Network: 1.67 TB flowing across the cluster network at once.
      At 10 Gbps per node, this takes ~13 minutes of network saturation.
    - Disk: source nodes reading partition data while serving production traffic.
      Disk I/O contention increases read latency.
    - CPU: checksumming, compressing, and transferring data consumes CPU.
    - Memory: receiving nodes cache incoming data, potentially evicting hot data.

  CASCADING EFFECTS:
    1. Production read/write latency increases as nodes are I/O bound.
    2. Clients see timeouts, start retrying, increasing load further.
    3. Health checks fail due to increased latency → false node failure detection.
    4. If a node is incorrectly declared dead during rebalancing, its partitions
       trigger ADDITIONAL rebalancing → positive feedback loop.
    5. Monitoring alerts fire, on-call engineers scramble.

PREVENTION:
  - Add nodes one at a time, waiting for rebalancing to complete between additions.
  - Throttle rebalancing bandwidth (e.g., Cassandra: stream_throughput_outbound
    defaults to 200 Mbps per node, tunable).
  - Use rate-limited rebalancing (CockroachDB: kv.snapshot_rebalance.max_rate).
  - Schedule rebalancing during low-traffic hours.
  - Monitor rebalancing progress and pause if production latency exceeds SLO.
```

### 13.4 Hot Partition Cascade

```
HOT PARTITION CASCADE:

1. A viral post causes celebrity user X's partition to receive 100x normal traffic.

2. The node hosting partition P-hot saturates its CPU.
   - Request queue grows.
   - Latency increases from 2ms to 200ms.

3. Upstream services (API gateway, ML inference) have a 50ms timeout.
   - Requests to P-hot start timing out.
   - Clients retry (standard retry policy: 3 retries).
   - Effective load on P-hot: 100x × 3 retries = 300x normal.

4. The node hosting P-hot also hosts 50 other partitions.
   - Those 50 partitions share the same CPU, network, and memory.
   - Requests to UNRELATED partitions on this node start failing.
   - "Noisy neighbor" problem: hot partition poisons the node.

5. Clients for the 50 unrelated partitions start retrying.
   - Load on the node multiplies further.
   - Node becomes completely unresponsive.

6. Failure detector declares the node dead.
   - All 51 partitions (hot + 50 innocent) are failed over.
   - 51 new leaders elected on other nodes.
   - But the hot partition is still hot on its new node.
   - The cascade continues on the new node.

MITIGATION:
  a. Per-partition resource isolation: CPU and memory cgroups per partition
     (CockroachDB's admission control does this at the request level).
  b. Backpressure: reject requests for an overloaded partition with a
     429 (Too Many Requests) instead of queuing them.
  c. Circuit breaker (see Ch. 33): stop retrying to P-hot after N failures.
  d. Adaptive load shedding (see Ch. 34): drop low-priority requests first.
  e. Detect hot keys and activate key salting dynamically.
  f. Request hedging only against a DIFFERENT partition/node, never retry
     against the same overloaded target.
```

---

## 14. Interview Patterns

### 14.1 The Partition Key Decision Framework

When an interviewer asks "how would you partition this data?", use this structured approach:

```
PARTITION KEY DECISION FRAMEWORK:

Step 1: Identify the primary access pattern.
  "What query runs 90% of the time?"
  → This determines the partition key.

  Example: Feature store → "Get features for entity X" → partition by entity_id.
  Example: Analytics → "Get all events for user Y in time range" →
           partition by user_id, sort by timestamp.

Step 2: Check for hotspots.
  "Will any single key receive disproportionate traffic?"
  → If yes: can you salt the key? Add a secondary cache? Split on load?

  Example: Social feed → celebrity users → salt hot user keys or
           dedicated cache for top-1000 users.

Step 3: Check cardinality.
  "Does the key have enough distinct values?"
  → Low cardinality (e.g., country code, status enum) → bad partition key.
  → Need at least 10x more distinct key values than partition count.

Step 4: Check for secondary access patterns.
  "What other queries exist?"
  → If frequent: global secondary index (extra write cost).
  → If infrequent: scatter-gather acceptable.
  → If both: consider denormalization (store data twice, partitioned differently).

Step 5: Size the partitions.
  "Will partitions be roughly equal in size?"
  → If not: use hash partitioning or composite keys to spread data.
  → Calculate: expected_data_per_partition and expected_QPS_per_partition.

Step 6: Consider operational requirements.
  "Does data have locality requirements?"
  → Geo-partitioning for compliance (GDPR → EU data stays in EU).
  → Co-locate related data (all of a user's data on the same partition for
     transactions within a user's scope).
```

### 14.2 Common Mistakes

**Mistake 1: Choosing a low-cardinality partition key.**
Partitioning a global user table by `country_code`. The US partition gets 40% of data, Luxembourg gets 0.001%. Extreme imbalance. Fix: use a high-cardinality key (user_id) or a compound key.

**Mistake 2: Ignoring the scatter-gather cost.**
"We'll add a secondary index and query by city." If the primary partition key is user_id, a query for "all users in NYC" must scatter to every partition. With 10,000 partitions, that is 10,000 network round-trips. Fix: if this query is frequent, use a global secondary index partitioned by city, or maintain a denormalized table partitioned by city.

**Mistake 3: Assuming uniform distribution without checking.**
"We hash user_id so it is uniform." The hash distribution is uniform, but the access pattern may not be. If 1% of users generate 50% of traffic, the partitions holding those users are 50x hotter than average, regardless of hash uniformity.

**Mistake 4: Not accounting for replication in capacity math.**
"10TB of data across 10 nodes, 1TB each." With RF=3, you need 30TB of raw storage -- 3TB per node if evenly distributed, meaning you actually need 30 nodes at 1TB each (or 10 nodes at 3TB each).

**Mistake 5: Over-sharding prematurely.**
A system with 100GB of data and 1000 QPS does not need 100 partitions. It fits comfortably on a single node with replication for availability. Sharding adds operational complexity (routing, rebalancing, cross-partition queries). Shard when you must, not when you can.

### 14.3 Template Answer Structure

Use this template when answering partitioning questions in interviews:

```
TEMPLATE ANSWER:

1. STATE THE PRIMARY ACCESS PATTERN:
   "The dominant query is [X], so we partition by [key] to ensure
   that query is a single-partition lookup."

2. CHOOSE THE STRATEGY:
   "We use [hash/range/directory] partitioning because [reason].
   Hash gives uniform distribution for point lookups.
   Range enables efficient scans for time-series queries.
   Directory for complex placement policies."

3. SIZE THE PARTITIONS:
   "With [N] items at [S] bytes each, total data is [T].
   With RF=[R], total storage is [T × R].
   At [target_partition_size] per partition, we need [P] partitions
   across [M] nodes."

4. ADDRESS HOTSPOTS:
   "The top [X%] of keys receive [Y%] of traffic. We mitigate by
   [key salting / dedicated cache / split on load / read replicas]."

5. HANDLE SECONDARY QUERIES:
   "The secondary query [Q] requires [scatter-gather / global index /
   denormalized table]. We accept [trade-off] because [reason]."

6. DISCUSS OPERATIONS:
   "Rebalancing on node addition: [strategy]. On node failure:
   leader election in [T] seconds, re-replication in [T] minutes.
   We monitor partition-level QPS and latency to detect hotspots."
```

### 14.4 Quick Reference: System to Partition Strategy Mapping

```
┌──────────────────────────┬────────────────────────┬───────────────────────────┐
│ System Type              │ Typical Partition Key   │ Strategy                  │
├──────────────────────────┼────────────────────────┼───────────────────────────┤
│ User data store          │ user_id (hash)         │ Hash, uniform lookups     │
│ Time-series DB           │ metric + time bucket   │ Range on time, hash on    │
│                          │                        │ metric for parallelism    │
│ Feature store (online)   │ entity_id (hash)       │ Hash, point lookups       │
│ Chat / messaging         │ conversation_id (hash) │ Hash, all messages in     │
│                          │                        │ same partition             │
│ E-commerce orders        │ order_id or user_id    │ Hash on order_id for      │
│                          │                        │ writes, user_id for reads │
│ Recommendation system    │ item_id (hash) for     │ Hash for items, IVF for   │
│                          │ features, IVF cluster  │ vector search             │
│                          │ for embeddings         │                           │
│ Search index             │ document_id (hash)     │ Hash, scatter-gather for  │
│                          │                        │ text search               │
│ Analytics / OLAP         │ date + dimension       │ Range on date, hash on    │
│                          │                        │ dimension for parallelism │
│ Distributed cache        │ cache_key (hash)       │ Consistent hash ring      │
│ Event log (Kafka)        │ entity_id or event_type│ Hash to partition, ordered│
│                          │                        │ within partition           │
│ Agent orchestration      │ session_id (hash)      │ Hash, session state       │
│                          │                        │ co-located                │
└──────────────────────────┴────────────────────────┴───────────────────────────┘
```

---

## Summary of Key Numbers

These are the constants and rules of thumb you should have at your fingertips:

```
NUMBERS TO KNOW:

Consistent hashing:
  - Keys moved on node addition: K/N (vs K×(N-1)/N for modular hash)
  - Virtual nodes for good balance: 128-256 per physical node
  - Load std dev with V vnodes: proportional to 1/sqrt(V × N)

Production systems:
  - Redis Cluster hash slots: 16,384
  - Cassandra default vnodes: 256 (num_tokens)
  - DynamoDB partition size limit: 10 GB
  - DynamoDB partition throughput limit: 3000 RCU / 1000 WCU
  - CockroachDB default range size: 512 MB
  - Kafka default hash: Murmur2
  - Cassandra default hash: Murmur3

Capacity planning rules of thumb:
  - Target partition size: 1-10 GB (varies by system)
  - Partition count: 10x expected max node count (for static partition systems)
  - Replication factor: 3 (standard), 5 (high-durability use cases)
  - Hot partition threshold: > 5x average QPS → investigate
  - Rebalancing bandwidth limit: 200 Mbps per node (default in many systems)

Failure timing:
  - Node failure detection: 10-30 seconds (tunable)
  - Leader election: 1-5 seconds
  - Re-replication of 1 GB partition: ~10 seconds at 100 MB/s
  - Full cluster rebalance (50 nodes, add 10): 10-60 minutes (throttled)
```

---

## Cross-References

### Within distributed-systems/
- **Chapter 7**: Kafka partitioning details and consumer group rebalancing.
- **Chapter 8**: Caching consistent hashing (Section 8), direct application of this chapter.
- **Chapter 22**: Stream processing — how Flink parallelism maps to Kafka partitions.
- **Chapter 29**: Failure detection. How nodes are declared dead, triggering partition failover.
- **Chapter 33**: Circuit breakers. How to prevent hot partition cascades.
- **Chapter 34**: Backpressure and load shedding. Essential for hot partition mitigation.

### From databases/
- **`databases/12-replication-and-distributed-storage.md` §4**: Sharding/Partitioning foundations — range-based (§4.1) and hash-based (§4.2) partitioning with consistent hashing. Covers ISR-based replication per partition.
- **`databases/19-distributed-databases-deep-dive.md` §9.3-9.4**: Consistent hashing with virtual nodes (Cassandra, Riak, DynamoDB) and range-based sharding with coordination (CockroachDB, Spanner, TiDB).
- **`databases/10-in-memory-databases.md`**: Redis Cluster hash slots and resharding without downtime, Memcached client-side consistent hashing, VoltDB single-threaded partitions.
- **`databases/06-indexing-internals.md`**: Secondary index structures — context for local vs global index partitioning trade-offs (§9 of this chapter).
- **`databases/11-vector-search-internals.md`**: ANN index partitioning (IVF clusters, HNSW sharding) for embedding search at scale.

### From solutions/
- **solutions/feature-store-design.md**: Feature store online serving partition design.
- **solutions/recommendation-system-design.md**: Item corpus partitioning for retrieval.
- **solutions/ml-inference-platform-design.md**: Model serving routing.
- **solutions/parallel-ml-training-design.md**: Training data sharding across workers.
- **solutions/ai-search-engine-design.md**: Embedding index partitioning.
