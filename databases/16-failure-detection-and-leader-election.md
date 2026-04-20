# Failure Detection & Leader Election: A Staff-Engineer Deep Dive

A comprehensive reference covering failure detection algorithms (heartbeats, Phi-accrual, SWIM/gossip), leader election protocols (Raft, Multi-Paxos, ZAB, Bully, Ring), lease-based leadership, fencing, pre-vote optimizations, split-brain prevention, and production implementations in etcd, ZooKeeper, CockroachDB, Consul, Cassandra, and Redis Sentinel.

Prerequisites: familiarity with replication and consensus fundamentals from `12-replication-and-distributed-storage.md`.

---

## Table of Contents

1. [The Fundamental Problem](#1-the-fundamental-problem)
2. [Failure Models](#2-failure-models)
3. [Failure Detection Mechanisms](#3-failure-detection-mechanisms)
4. [Leader Election Algorithms](#4-leader-election-algorithms)
5. [Lease-Based Leadership](#5-lease-based-leadership)
6. [Fencing and Split-Brain Prevention](#6-fencing-and-split-brain-prevention)
7. [Advanced Election Mechanics](#7-advanced-election-mechanics)
8. [Failure Detection in Leaderless Systems](#8-failure-detection-in-leaderless-systems)
9. [Real-World Implementations](#9-real-world-implementations)
10. [Failure Scenarios and War Stories](#10-failure-scenarios-and-war-stories)
11. [Tuning and Operational Guidance](#11-tuning-and-operational-guidance)
12. [Comparison Tables](#12-comparison-tables)

---

## 1. The Fundamental Problem

### 1.1 Why Failure Detection Is Hard

In a distributed system, you cannot distinguish between a node that is dead, a node that is slow, and a network link that is broken. This is the fundamental impossibility that drives everything in this document.

```
THE AMBIGUITY OF SILENCE:

  Node A sends heartbeat to Node B. No response.

  Possible causes:
  ┌─────────────────────────────────────────────────────────────────────┐
  │  1. B crashed (process died, kernel panic, hardware failure)       │
  │  2. B is alive but overloaded (GC pause, CPU saturation, swap)    │
  │  3. B is alive but its network interface is down                   │
  │  4. Network between A and B is partitioned                         │
  │  5. A's outbound message was dropped                               │
  │  6. B's response was dropped                                       │
  │  7. B's response is delayed (congestion, routing change)           │
  │  8. A's clock jumped forward → premature timeout                   │
  └─────────────────────────────────────────────────────────────────────┘

  All of these look IDENTICAL to A: silence.

  This is not a solvable problem — it's a fundamental limitation:
  Fischer-Lynch-Paterson (FLP) impossibility (1985):
    No deterministic algorithm can guarantee consensus in an
    asynchronous system where even ONE node may crash.

  Practical systems work around FLP by using:
    - Timeouts (partial synchrony assumption)
    - Randomization (randomized election timeouts in Raft)
    - Failure detectors (unreliable but useful abstractions)
```

### 1.2 The Cost of Getting It Wrong

```
CONSEQUENCES OF FAILURE DETECTION ERRORS:

  FALSE POSITIVE (declare alive node as dead):
  ┌─────────────────────────────────────────────────────────────────────┐
  │  ─ Unnecessary leader election → brief unavailability (seconds)    │
  │  ─ Unnecessary data rebalancing → massive network/disk I/O         │
  │  ─ Cascading failures: rebalancing overloads remaining nodes       │
  │  ─ Client disruption: connections dropped, transactions aborted    │
  │  ─ In extreme cases: thrashing (repeated false failovers)          │
  │                                                                     │
  │  Real cost: GitHub 2012 outage — MySQL failover loop caused by     │
  │  false positives from a network switch firmware bug.                │
  └─────────────────────────────────────────────────────────────────────┘

  FALSE NEGATIVE (fail to detect a dead node):
  ┌─────────────────────────────────────────────────────────────────────┐
  │  ─ Stale leader continues accepting writes → data loss             │
  │  ─ Clients timeout waiting for responses from dead leader          │
  │  ─ Prolonged unavailability until detection catches up             │
  │  ─ In leaderless systems: read/write quorums silently degrade      │
  │                                                                     │
  │  Real cost: longer detection time = longer outage.                 │
  │  A 30-second detection timeout means minimum 30s of downtime.      │
  └─────────────────────────────────────────────────────────────────────┘

  THE TENSION:
  ────────────────────────────────────────────────────────────────────
  Aggressive detection (short timeouts):  more false positives
  Conservative detection (long timeouts): more false negatives

  There is NO setting that is optimal for both. Every system must
  choose where on this spectrum to sit.
```

### 1.3 Failure Detector Theory

Chandra and Toueg (1996) formalized failure detectors with two properties:

```
FAILURE DETECTOR PROPERTIES:

  COMPLETENESS:
    Every faulty process is eventually suspected by every correct process.
    ─ Strong completeness: every correct process eventually suspects
      every faulty process.
    ─ Weak completeness: some correct process eventually suspects
      every faulty process.

  ACCURACY:
    Correct processes are not falsely suspected.
    ─ Strong accuracy: no correct process is ever suspected.
      (impossible in asynchronous systems)
    ─ Eventual strong accuracy: after some unknown time, no correct
      process is suspected. (achievable in partially synchronous systems)
    ─ Weak accuracy: some correct process is never suspected.

  ┌──────────────────────────────────────────────────────────────────┐
  │  Class          │ Completeness  │ Accuracy                       │
  ├──────────────────┼───────────────┼────────────────────────────────┤
  │  Perfect (P)     │ Strong        │ Strong                         │
  │  Eventually      │ Strong        │ Eventual strong                │
  │  Perfect (◇P)    │               │                                │
  │  Strong (S)      │ Strong        │ Weak                           │
  │  Eventually      │ Strong        │ Eventual weak                  │
  │  Strong (◇S)     │               │                                │
  └──────────────────┴───────────────┴────────────────────────────────┘

  Key result: ◇S (eventually strong) is the WEAKEST failure detector
  that can solve consensus. This is what real systems approximate.

  In practice: all failure detectors are unreliable. They are a
  useful abstraction, not a perfect oracle.
```

---

## 2. Failure Models

### 2.1 Taxonomy of Failures

```
FAILURE MODEL HIERARCHY (from easiest to hardest to handle):

  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │  CRASH-STOP                                                         │
  │  ─ Node halts and never recovers                                   │
  │  ─ Simplest model: once dead, stays dead                           │
  │  ─ Tolerated by: any replication scheme                            │
  │  ─ Example: disk on fire, decommissioned server                    │
  │                                                                     │
  │  CRASH-RECOVERY                                                     │
  │  ─ Node halts, then restarts with durable state intact             │
  │  ─ Most common real-world failure mode                             │
  │  ─ Requires: stable storage (WAL), state reconciliation on rejoin  │
  │  ─ Example: OOM kill, kernel panic followed by reboot              │
  │                                                                     │
  │  OMISSION                                                           │
  │  ─ Node is running but fails to send or receive some messages      │
  │  ─ Send omission: node doesn't send messages it should             │
  │  ─ Receive omission: node drops incoming messages                  │
  │  ─ Example: full network buffer, NIC driver bug                    │
  │                                                                     │
  │  TIMING / PERFORMANCE                                               │
  │  ─ Node responds, but too slowly                                   │
  │  ─ Gray failure: partially degraded, not fully down                │
  │  ─ Hardest to detect: node appears "alive" to some observers       │
  │  ─ Example: GC pause, disk I/O stall, CPU throttling               │
  │                                                                     │
  │  BYZANTINE                                                          │
  │  ─ Node behaves arbitrarily (including maliciously)                │
  │  ─ Requires BFT protocols (PBFT, HotStuff) — expensive            │
  │  ─ Used in blockchains, not in databases                           │
  │  ─ Tolerates f faults with 3f+1 nodes (vs f faults with 2f+1)    │
  │                                                                     │
  └─────────────────────────────────────────────────────────────────────┘

  Most database systems assume CRASH-RECOVERY with OMISSION.
  They do NOT handle Byzantine faults.
```

### 2.2 Gray Failures: The Hardest to Detect

```
GRAY FAILURES — THE SILENT KILLERS:

  A gray failure is a partial failure where the system appears healthy
  to some observers but degraded or failed to others.

  Examples:
  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │  1. SLOW DISK                                                       │
  │     ─ Node responds to heartbeats (in memory)                      │
  │     ─ But fsync takes 5 seconds → all writes stall                 │
  │     ─ Failure detector says: ALIVE (heartbeats OK)                 │
  │     ─ Clients say: DEAD (operations timeout)                       │
  │                                                                     │
  │  2. GC PAUSE (Java-based systems: ZooKeeper, Cassandra, Kafka)     │
  │     ─ Full GC pauses the world for 200ms — 30+ seconds             │
  │     ─ During pause: no heartbeats sent or received                 │
  │     ─ After pause: node thinks it was gone for milliseconds        │
  │     ─ But: its session/lease may have expired during the pause     │
  │     ─ Node continues acting as leader when it is no longer leader  │
  │                                                                     │
  │  3. NETWORK ASYMMETRY                                               │
  │     ─ A can send to B, but B cannot send to A                      │
  │     ─ A's failure detector: B is alive (I send, B processes)       │
  │     ─ B's failure detector: A is dead (I never hear from A)        │
  │     ─ Both are correct from their own perspective                  │
  │                                                                     │
  │  4. CPU STARVATION                                                  │
  │     ─ Noisy neighbor on shared host steals CPU                     │
  │     ─ Heartbeat thread doesn't get scheduled for 10 seconds        │
  │     ─ Node is technically alive but cannot serve requests           │
  │                                                                     │
  │  5. PARTIAL NETWORK FAILURE                                         │
  │     ─ Node can reach 2 of 4 peers but not the other 2              │
  │     ─ Health check to load balancer succeeds                       │
  │     ─ But consensus operations fail (cannot reach quorum)          │
  │                                                                     │
  └─────────────────────────────────────────────────────────────────────┘

  Microsoft Research (2017) study of Azure incidents:
    ─ ~33% of failures were gray failures
    ─ Median detection time for gray failures: 28 minutes
    ─ Median detection time for full failures: 5 minutes
    ─ Gray failures caused disproportionate user impact

  Defense strategies:
    1. Multi-dimensional health checks (not just heartbeat — also I/O,
       latency percentiles, error rates)
    2. Peer-based detection (let OTHER nodes report your health)
    3. Application-level checks (can you actually serve a request?)
    4. Watchdog processes that verify liveness end-to-end
```

---

## 3. Failure Detection Mechanisms

### 3.1 Fixed-Timeout Heartbeats

The simplest and most widely used approach. A node is declared dead if it hasn't sent a heartbeat within a fixed time window.

```
FIXED-TIMEOUT HEARTBEAT PROTOCOL:

  Parameters:
    heartbeat_interval = T      (e.g., 500ms)
    failure_timeout    = k × T  (e.g., k=6 → 3 seconds)

  Leader sends heartbeat to every follower every T ms:

  Time ──────────────────────────────────────────────────────────────►
  Leader:  HB    HB    HB    HB    ✗     ✗     ✗     (crashed at t=2s)
           │     │     │     │
           ▼     ▼     ▼     ▼
  Follwr:  ✓     ✓     ✓     ✓     ...   ...   ... TIMEOUT! (t=5s)
           reset reset reset reset ─────── 3s silence ──────►
           timer timer timer timer

  Detection latency:
    Best case:  failure_timeout                    (crash right after HB)
    Worst case: failure_timeout + heartbeat_interval (crash right before HB)
    Average:    failure_timeout + heartbeat_interval/2

  ┌──────────────────────────────────────────────────────────────────┐
  │  System          │ HB Interval  │ Election Timeout │ Detection   │
  ├──────────────────┼──────────────┼──────────────────┼─────────────┤
  │  etcd (Raft)     │ 100ms        │ 1000-2000ms      │ 1-2s        │
  │  CockroachDB     │ 500ms        │ 3000ms           │ 3-6s        │
  │  ZooKeeper       │ 2000ms (tick)│ 2×tick × initLmt │ 4-40s       │
  │  Redis Sentinel  │ 1000ms       │ configurable     │ 5-30s       │
  │  Consul (Raft)   │ 1000ms       │ 5×HB = 5000ms   │ 5-10s       │
  │  MongoDB         │ 2000ms       │ 10000ms          │ 10-12s      │
  │  Kafka (KRaft)   │ 500ms        │ 15 × interval    │ 7.5-15s     │
  └──────────────────┴──────────────┴──────────────────┴─────────────┘

  Problems with fixed timeout:
    1. Too short → false positives during GC pauses / network blips
    2. Too long → slow failure detection → prolonged outage
    3. No adaptation: a spike in network latency causes false failures
    4. Clock skew: if the suspect's clock runs fast, it sends heartbeats
       "too often" (harmless); if slow, it sends "too rarely" (dangerous)
```

### 3.2 Phi-Accrual Failure Detector

Developed by Hayashibara et al. (2004). Instead of a binary alive/dead decision, it outputs a continuous suspicion level (phi, φ) that represents the probability that the monitored node has failed.

```
PHI-ACCRUAL FAILURE DETECTOR:

  Core idea: track the DISTRIBUTION of heartbeat arrival times,
  not just a fixed timeout.

  Step 1: Collect heartbeat inter-arrival times

    Maintain a sliding window of recent inter-arrival times:
    Window = [98ms, 102ms, 99ms, 150ms, 101ms, 97ms, 200ms, ...]

    Compute: μ (mean) and σ (std deviation)
    Example: μ = 120ms, σ = 35ms

  Step 2: When a heartbeat is "late", compute φ

    t_last = timestamp of last heartbeat received
    t_now  = current time
    Δt     = t_now - t_last  (time since last heartbeat)

    φ = -log₁₀(P(X > Δt))

    where P(X > Δt) is the probability that a heartbeat would arrive
    later than Δt under the normal distribution N(μ, σ²).

    ┌─────────────────────────────────────────────────────────────────┐
    │  φ value   │ P(alive)     │ Interpretation                      │
    ├────────────┼──────────────┼─────────────────────────────────────┤
    │  φ = 1     │ 90%          │ Slightly late, probably fine        │
    │  φ = 2     │ 99%          │ Unusual delay, getting suspicious   │
    │  φ = 3     │ 99.9%        │ Very suspicious                     │
    │  φ = 5     │ 99.999%      │ Almost certainly dead               │
    │  φ = 8     │ 99.999999%   │ Definitely dead                     │
    └────────────┴──────────────┴─────────────────────────────────────┘

    Decision: declare node as failed when φ > φ_threshold.
    Cassandra default: φ_threshold = 8

  Step 3: Adaptive behavior

    Network gets congested → heartbeats arrive later → μ and σ increase
    → φ stays low despite longer inter-arrival times
    → no false positive!

    Network recovers → heartbeats arrive faster → μ and σ decrease
    → detection becomes faster again

  Diagram:

    Normal:  HB  HB  HB  HB  HB    (inter-arrival ≈ 100ms, σ ≈ 10ms)
             φ=0 φ=0 φ=0 φ=0 φ=0

    Congestion: HB    HB      HB        HB   (inter-arrival ≈ 300ms)
                φ=0   φ=1.2   φ=0.8     φ=0
                                         ↑ window adapted, μ ≈ 300ms now

    Real failure:  HB    HB    HB    ✗ (dead)
                   φ=0   φ=0   φ=0   .... φ=1 ... φ=3 ... φ=8 → DEAD

  ADVANTAGES OVER FIXED TIMEOUT:
    ✓ Adapts to changing network conditions automatically
    ✓ No manual tuning of timeout values
    ✓ Single parameter (φ_threshold) controls false positive rate
    ✓ Works across different network environments (LAN vs WAN)

  DISADVANTAGES:
    ✗ Requires sufficient sample size (cold start problem)
    ✗ Normal distribution assumption may not hold for all networks
    ✗ More complex implementation than simple timeout
    ✗ Does not handle bimodal latency distributions well
       (solution: use exponential distribution or log-normal instead)

  USED BY: Cassandra, Akka (actor framework), Amazon (internal services)
```

### 3.3 SWIM Protocol (Scalable Weakly-consistent Infection-style Membership)

Developed by Das et al. (2002). A gossip-based failure detection protocol designed for large clusters where direct heartbeats from every node to every other node would be too expensive.

```
THE SCALABILITY PROBLEM WITH DIRECT HEARTBEATS:

  N nodes, each heartbeating to all others:
    Messages per interval: N × (N-1)
    10 nodes:    90 messages/interval     (manageable)
    100 nodes:   9,900 messages/interval  (heavy)
    1000 nodes:  999,000 messages/interval (unsustainable)

  SWIM solves this: O(N) messages per protocol period.


SWIM PROTOCOL — HOW IT WORKS:

  Each protocol period (e.g., every 1 second), each node Mi:

  STEP 1: DIRECT PROBE
    Mi randomly selects one target Mj and sends a PING.

    Mi ────PING────► Mj
    Mi ◄───ACK─────  Mj   (if Mj responds → Mj is alive, done)

  STEP 2: INDIRECT PROBE (if direct probe fails)
    If Mj doesn't respond within timeout, Mi asks k random nodes
    to probe Mj on its behalf:

    Mi ──PING-REQ(Mj)──► Mk₁
    Mi ──PING-REQ(Mj)──► Mk₂
    Mi ──PING-REQ(Mj)──► Mk₃

    Each Mk sends PING to Mj:

    Mk₁ ────PING────► Mj
    Mk₁ ◄───ACK─────  Mj   (maybe)
    Mk₁ ────ACK(Mj)──► Mi  (relays result)

  STEP 3: DECLARE SUSPECT or DEAD
    If NO indirect probes get an ACK from Mj:
      → Mark Mj as SUSPECT (not immediately dead)
      → Disseminate suspicion through gossip (piggyback on protocol msgs)

    Mj has a SUSPICION TIMEOUT to refute:
      → If Mj hears it's suspected, it sends an ALIVE message
      → If timeout expires with no refutation → declare Mj DEAD

  DIAGRAM — FULL SWIM ROUND:

    ┌──────────────────────────────────────────────────────────────────┐
    │  Protocol Period for Node Mi                                     │
    │                                                                  │
    │  Mi ──PING──► Mj        (direct probe)                          │
    │      wait T_ping...                                              │
    │      no ACK                                                      │
    │                                                                  │
    │  Mi ──PING-REQ(Mj)──► Mk₁, Mk₂, Mk₃   (indirect probes)      │
    │      Mk₁ ──PING──► Mj                                           │
    │      Mk₂ ──PING──► Mj                                           │
    │      Mk₃ ──PING──► Mj                                           │
    │      wait T_indirect...                                          │
    │                                                                  │
    │  Case A: Some Mk got ACK from Mj                                │
    │      → Mj is alive. False alarm (maybe Mi↔Mj link is down).    │
    │                                                                  │
    │  Case B: No Mk got ACK from Mj                                  │
    │      → Mark Mj as SUSPECT                                       │
    │      → Disseminate via gossip piggyback                          │
    │      → After suspicion_timeout with no refutation → CONFIRM DEAD │
    └──────────────────────────────────────────────────────────────────┘

  MEMBERSHIP DISSEMINATION (Infection-style / Gossip):

    Membership changes (join, suspect, dead, alive) are piggybacked
    on PING, PING-REQ, and ACK messages. No separate gossip protocol.

    Propagation speed: O(log N) protocol periods for full dissemination.
    With 1000 nodes and 1s period: ~10 seconds for all nodes to learn.

  PROPERTIES:
    ✓ O(N) messages per period per node (just 1 PING + k PING-REQs)
    ✓ Distributed: no central failure detector
    ✓ Complete: every failure is eventually detected
    ✓ False positive rate bounded by indirect probe count (k)
    ✓ Network-partition-aware: indirect probes detect asymmetric partitions

  USED BY:
    ─ Consul (HashiCorp): memberlist library
    ─ Serf (HashiCorp): cluster membership
    ─ Uber's Ringpop: application-level sharding
    ─ ScyllaDB: gossip-based failure detection
```

### 3.4 Gossip-Based Failure Detection

A broader category that includes SWIM. Pure gossip protocols periodically exchange state with random peers.

```
GOSSIP FAILURE DETECTION:

  Each node maintains a heartbeat counter and a local timestamp
  for every known node.

  Node state table (at node A):
  ┌──────┬───────────────┬──────────────────┐
  │ Node │ HB Counter    │ Last Updated     │
  ├──────┼───────────────┼──────────────────┤
  │ A    │ 42            │ (self, always ↑) │
  │ B    │ 37            │ 1500ms ago       │
  │ C    │ 51            │ 200ms ago        │
  │ D    │ 29            │ 8000ms ago ←!!   │
  │ E    │ 44            │ 400ms ago        │
  └──────┴───────────────┴──────────────────┘

  Every gossip_interval (e.g., 1 second):
    1. Increment own heartbeat counter
    2. Pick k random peers (typically k=1 to 3)
    3. Send full state table to them
    4. Merge received table: take max(counter) for each node
    5. Update "last updated" for any node whose counter increased

  Failure detection:
    If (now - last_updated[X]) > T_fail → mark X as SUSPECT
    If (now - last_updated[X]) > T_cleanup → remove X from table

  Cassandra example:
    ─ Gossip every 1 second
    ─ Sends to 1 live node + 1 seed node + 1 unreachable node
    ─ Uses phi-accrual on top of gossip timestamps
    ─ Three states: UP, DOWN, removed

  ADVANTAGES:
    ✓ Decentralized, no single point of failure
    ✓ Scalable: each node does O(1) work per interval
    ✓ Robust to network partitions (information flows through any path)

  DISADVANTAGES:
    ✗ Slow detection: O(log N) rounds to propagate info
    ✗ Inconsistent views: different nodes may disagree about who is alive
    ✗ Not suitable for leader election (no consensus guarantee)
    ✗ State table grows with cluster size (O(N) per node)
```

### 3.5 Application-Level Health Checks

```
MULTI-DIMENSIONAL HEALTH CHECKS:

  Simple heartbeat: "I am alive" (process is running)
  Application health: "I can serve requests" (functionally correct)

  ┌─────────────────────────────────────────────────────────────────────┐
  │  Level 0: PROCESS LIVENESS                                         │
  │    ─ TCP connection accepted? PID exists?                          │
  │    ─ Cheapest check, least informative                             │
  │    ─ Catches: crash-stop failures                                  │
  │    ─ Misses: everything else                                       │
  │                                                                     │
  │  Level 1: HEARTBEAT/PING RESPONSE                                  │
  │    ─ Node responds to lightweight ping within timeout              │
  │    ─ Catches: process liveness + basic network connectivity        │
  │    ─ Misses: slow disk, deadlocked worker threads, corrupted state │
  │                                                                     │
  │  Level 2: READ PROBE                                                │
  │    ─ Execute a lightweight read (SELECT 1, GET health_key)         │
  │    ─ Catches: storage engine responsiveness, query path health     │
  │    ─ Misses: write path issues, replication lag                    │
  │                                                                     │
  │  Level 3: WRITE PROBE                                               │
  │    ─ Execute a write + read back (INSERT INTO health_check ...)    │
  │    ─ Catches: full write path, WAL, storage, commit                │
  │    ─ Cost: actually writes data, need cleanup                      │
  │                                                                     │
  │  Level 4: END-TO-END LATENCY + THROUGHPUT                          │
  │    ─ Measure p99 latency of representative operations              │
  │    ─ Compare to historical baseline                                │
  │    ─ Catches: gray failures, performance degradation               │
  │    ─ Most expensive, most informative                              │
  └─────────────────────────────────────────────────────────────────────┘

  PRODUCTION PATTERN — LAYERED HEALTH CHECKS:

    Load Balancer → Level 2 check every 5s (remove from pool if fails)
    Failure Detector → Level 1 heartbeat every 1s (trigger election)
    Monitoring → Level 4 continuous (alert on degradation)
    Watchdog → Level 3 probe every 30s (page on-call if fails)

  CockroachDB example:
    ─ Raft heartbeats (Level 1) for leader election
    ─ Node liveness table (Level 3): each node writes epoch + expiration
    ─ Liveness record not renewed → node considered dead
    ─ This is a heartbeat-through-consensus: proves the node can
      participate in Raft (not just respond to pings)
```

---

## 4. Leader Election Algorithms

### 4.1 Why Leader Election Matters for Databases

```
LEADER ELECTION IN DATABASE CONTEXT:

  Databases need a single leader (for a shard/partition) to:
    1. Serialize writes → provide linearizability
    2. Manage WAL → ensure durability ordering
    3. Coordinate replication → push log entries to followers
    4. Serve consistent reads → leaseholder optimization

  Requirements for a correct leader election:
  ┌─────────────────────────────────────────────────────────────────────┐
  │  SAFETY:                                                            │
  │    At most ONE leader per term/epoch at any point in time.          │
  │    (Two leaders = split-brain → data corruption/loss)              │
  │                                                                     │
  │  LIVENESS:                                                          │
  │    Eventually, some node becomes leader.                            │
  │    (The system makes progress)                                      │
  │                                                                     │
  │  LEADER COMPLETENESS:                                               │
  │    The new leader has ALL committed entries.                        │
  │    (No committed data is lost during election)                      │
  │                                                                     │
  │  LEADER STICKINESS (practical):                                     │
  │    Don't change leaders unnecessarily.                              │
  │    (Elections are expensive and cause brief unavailability)         │
  └─────────────────────────────────────────────────────────────────────┘

  Election downtime = time with no leader = unavailability for writes:
    ─ Raft: typically 1-10 seconds
    ─ ZooKeeper (ZAB): 2-10 seconds
    ─ MongoDB: 10-12 seconds (configurable)
    ─ Kafka (KRaft): 5-15 seconds
    ─ Redis Sentinel: 5-30 seconds
```

### 4.2 Raft Leader Election (Deep Dive)

Raft election is covered at a high level in `12-replication-and-distributed-storage.md`. Here we go deeper into edge cases, timing, and optimizations.

```
RAFT ELECTION — DETAILED MECHANICS:

  TERMS: logical clock for the cluster
  ────────────────────────────────────────────────────────────────────
  Term 1        Term 2        Term 3        Term 4
  ──────────────┼─────────────┼─────────────┼─────────────
  Leader: A     │ Leader: B   │ no leader   │ Leader: C
  (normal op)   │ (A failed)  │ (split vote)│ (C wins)

  Each term has at most one leader. Terms only increase, never decrease.
  When a node sees a higher term → it immediately steps down to follower.


  ELECTION TIMEOUT — THE CRITICAL PARAMETER:

    Raft requires: broadcastTime << electionTimeout << MTBF

    broadcastTime: time for one RPC round-trip (1-20ms LAN, 50-500ms WAN)
    electionTimeout: randomized in range [T, 2T] (e.g., [150ms, 300ms])
    MTBF: mean time between failures (~months for commodity servers)

    WHY RANDOMIZED:
      If all nodes used the same timeout, they'd all start elections
      simultaneously → split votes → no progress.

      Randomization breaks symmetry:
      ┌──────────────────────────────────────────────────────────────┐
      │  Node A: election timeout = 217ms                           │
      │  Node B: election timeout = 283ms                           │
      │  Node C: election timeout = 156ms  ← fires first!         │
      │                                                              │
      │  C starts election, sends RequestVote to A, B               │
      │  A and B haven't timed out yet → vote for C                 │
      │  C wins before A and B even become candidates               │
      └──────────────────────────────────────────────────────────────┘

    In practice, the first node to timeout wins >99% of elections
    (if there are no log divergence issues).


  REQUEST VOTE RPC — DETAILED:

    RequestVote message:
    ┌─────────────────────────────────────────────────────┐
    │  term:          candidate's current term             │
    │  candidateId:   candidate's node ID                  │
    │  lastLogIndex:  index of candidate's last log entry  │
    │  lastLogTerm:   term of candidate's last log entry   │
    └─────────────────────────────────────────────────────┘

    Voter's decision algorithm:
    ┌─────────────────────────────────────────────────────────────────┐
    │  if request.term < currentTerm:                                │
    │      return voteGranted = false   // stale candidate           │
    │                                                                │
    │  if already voted for different candidate in this term:         │
    │      return voteGranted = false   // one vote per term         │
    │                                                                │
    │  if candidate's log is LESS up-to-date than mine:              │
    │      return voteGranted = false   // election restriction      │
    │                                                                │
    │  // "at least as up-to-date" comparison:                       │
    │  //   1. Higher lastLogTerm wins                               │
    │  //   2. If same term, higher lastLogIndex wins                │
    │                                                                │
    │  return voteGranted = true                                     │
    └─────────────────────────────────────────────────────────────────┘

    The election restriction is CRITICAL for safety:
      It guarantees the new leader has all committed entries.

      Example (why it matters):
        Node A (leader, term 1): log = [1:x←1, 1:y←2, 1:z←3]
        Node B (follower):       log = [1:x←1, 1:y←2]           (lagging)
        Node C (follower):       log = [1:x←1, 1:y←2, 1:z←3]

        A crashes. B and C start election for term 2.

        B's lastLog = (idx:2, term:1)
        C's lastLog = (idx:3, term:1)

        If B asks C for vote: C compares (2,1) vs (3,1) → C has more
        entries in same term → C denies vote to B.

        C wins election → C has all committed entries → safe.


  EDGE CASE: SIMULTANEOUS CANDIDATES (SPLIT VOTE)

    5-node cluster: A, B, C, D, E. Leader crashes.

    ┌─────────────────────────────────────────────────────────────────┐
    │  B's timeout fires at t=200ms. C's timeout fires at t=203ms.  │
    │                                                                │
    │  B starts term 2 election, votes for self.                     │
    │  C starts term 2 election, votes for self.                     │
    │                                                                │
    │  D receives B's RequestVote first → votes for B                │
    │  E receives C's RequestVote first → votes for C                │
    │                                                                │
    │  B: 2 votes (self + D). Needs 3. Fails.                       │
    │  C: 2 votes (self + E). Needs 3. Fails.                       │
    │                                                                │
    │  Both back off with new randomized timeouts.                   │
    │  Term 3: one will fire first and likely win.                   │
    │                                                                │
    │  Probability of repeated split votes:                          │
    │  With [150, 300] ms range: P(split) ≈ 3ms/150ms ≈ 2%         │
    │  P(2 consecutive splits) ≈ 0.04%                               │
    │  P(3 consecutive splits) ≈ 0.0008%                             │
    └─────────────────────────────────────────────────────────────────┘


  PARTITIONED LEADER STEP-DOWN:

    Leader A gets partitioned from the majority:

    Partition 1: {A}           Partition 2: {B, C, D, E}
    A is leader (term 5)       No leader yet (term 5)

    ─ A cannot replicate to majority → its writes fail
    ─ A continues sending heartbeats to itself (no followers respond)
    ─ Meanwhile: B/C/D/E elect new leader (term 6)
    ─ When partition heals: A receives message with term 6 → steps down

    Without pre-vote (Section 7.1):
      During partition, A's election timeout may fire repeatedly,
      incrementing its term: 5 → 6 → 7 → 8 → ...
      When partition heals, A's high term disrupts the healthy leader.
      This is called "term inflation" — solved by Pre-Vote.
```

### 4.3 ZAB (ZooKeeper Atomic Broadcast) Leader Election

```
ZAB LEADER ELECTION (ZooKeeper):

  ZAB is the protocol underlying ZooKeeper. It differs from Raft in
  several important ways but solves the same problem.

  KEY DIFFERENCES FROM RAFT:
    ─ ZAB separates DISCOVERY, SYNCHRONIZATION, and BROADCAST phases
    ─ Election uses the TRANSACTION ID (zxid) not just log length
    ─ Leader must synchronize ALL followers before accepting new writes
    ─ ZAB guarantees all committed transactions are delivered in order

  ZXID: 64-bit transaction ID
    ─ High 32 bits: epoch (equivalent to Raft term)
    ─ Low 32 bits: counter within epoch
    ─ Example: zxid = (epoch=3, counter=47) = 0x0000000300000002F

  ELECTION ALGORITHM (Fast Leader Election):

  Phase 1 — VOTE
    Each node proposes itself as leader with its vote:
      vote = (proposedLeaderId, proposedZxid, proposedEpoch)

    Initially: every node votes for itself.

    ┌─────────────────────────────────────────────────────────────────┐
    │  Node A: vote(A, zxid=0x3_2F, epoch=3)                        │
    │  Node B: vote(B, zxid=0x3_30, epoch=3)  ← higher zxid        │
    │  Node C: vote(C, zxid=0x3_2E, epoch=3)                        │
    │                                                                 │
    │  Comparison rule (strict ordering):                             │
    │    1. Higher epoch wins                                         │
    │    2. If same epoch, higher zxid wins                           │
    │    3. If same zxid, higher server ID wins (tie-break)          │
    │                                                                 │
    │  A receives B's vote: (B, 0x3_30) > (A, 0x3_2F) → A changes  │
    │     its vote to B.                                              │
    │  C receives B's vote: (B, 0x3_30) > (C, 0x3_2E) → C changes  │
    │     its vote to B.                                              │
    │                                                                 │
    │  B receives majority votes for itself → B is the new leader.   │
    └─────────────────────────────────────────────────────────────────┘

  Phase 2 — DISCOVERY
    New leader contacts all followers, collects their last zxid.
    Leader determines the most up-to-date transaction history.

  Phase 3 — SYNCHRONIZATION
    Leader sends any missing transactions to lagging followers.
    Followers must be fully caught up BEFORE new writes are accepted.

    This is a KEY DIFFERENCE from Raft:
      Raft: leader immediately starts accepting writes, repairs
            followers in the background via log replication.
      ZAB: leader WAITS until a quorum is synchronized, THEN accepts
           new writes. This adds latency to failover but simplifies
           the consistency model.

  Phase 4 — BROADCAST
    Normal operation: leader receives proposals, broadcasts to followers
    using atomic broadcast (2-phase commit within ZAB).

  TIMELINE OF ZAB FAILOVER:

  ──────────────────────────────────────────────────────────────────────►
  Leader A    │ A crashes │ Election │ Discovery │ Sync     │ Broadcast
  operating   │           │ (1-5s)   │ (< 1s)   │ (1-10s)  │ (normal op)
              │           │          │           │          │
              │           └────────── Downtime ──────────── │
                                   (typically 2-10 seconds)
```

### 4.4 Bully Algorithm

```
BULLY ALGORITHM (Garcia-Molina, 1982):

  Simple algorithm where the node with the highest ID always wins.
  Not used in modern consensus systems, but appears in older designs
  and is educationally important.

  RULES:
    1. When a node detects the leader is dead:
       ─ It sends ELECTION message to all nodes with HIGHER IDs
    2. If it receives OK from any higher-ID node:
       ─ It backs off (a higher node will take over)
    3. If no higher-ID node responds:
       ─ It declares itself leader (sends COORDINATOR to all nodes)
    4. If a node receives ELECTION from a lower-ID node:
       ─ It responds OK and starts its own election

  EXAMPLE — 5 nodes (IDs: 1-5), leader is node 5:

    Node 5 crashes. Node 2 detects it.

    Step 1: Node 2 sends ELECTION to {3, 4, 5}
    Step 2: Node 3 replies OK, Node 4 replies OK, Node 5 no reply
    Step 3: Node 2 backs off (got OK responses)

    Step 4: Node 3 sends ELECTION to {4, 5}
    Step 5: Node 4 replies OK, Node 5 no reply
    Step 6: Node 3 backs off

    Step 7: Node 4 sends ELECTION to {5}
    Step 8: Node 5 no reply
    Step 9: Node 4 declares itself COORDINATOR → sends to all nodes

    ┌───────────────────────────────────────────────────────────────┐
    │  N2──ELECTION──►N3  N2──ELECTION──►N4  N2──ELECTION──►N5   │
    │  N2◄───OK───────N3  N2◄───OK───────N4       (no reply)     │
    │                                                              │
    │  N3──ELECTION──►N4  N3──ELECTION──►N5                       │
    │  N3◄───OK───────N4       (no reply)                         │
    │                                                              │
    │  N4──ELECTION──►N5                                           │
    │       (no reply)                                             │
    │                                                              │
    │  N4──COORDINATOR──►N1, N2, N3    (N4 is new leader)         │
    └───────────────────────────────────────────────────────────────┘

  PROBLEMS:
    ✗ Highest-ID node always wins → not necessarily the best leader
    ✗ No log comparison → new leader may be missing committed data
    ✗ O(N²) messages in worst case
    ✗ Not partition-tolerant: both sides may elect a leader
    ✗ Livelock: if a high-ID node keeps crashing and recovering

  WHERE STILL USED:
    ─ MongoDB replica sets use a variant (with priority weighting)
    ─ Some legacy systems
    ─ Educational contexts
```

### 4.5 Ring-Based Election

```
RING-BASED ELECTION (Chang-Roberts, 1979):

  Nodes are arranged in a logical ring. Election messages travel
  around the ring. The node with the highest ID/priority wins.

  ALGORITHM:
    1. Initiator sends ELECTION(myId) to next node in ring
    2. Each node compares its ID:
       ─ If message ID > myId: forward the message (someone better)
       ─ If message ID < myId: replace with ELECTION(myId) and forward
       ─ If message ID = myId: I won! Send ELECTED(myId) around ring

  EXAMPLE — Ring: A(3) → B(1) → C(4) → D(2) → A

    A detects leader failure, sends ELECTION(3):

    A──ELECTION(3)──►B   B: 1 < 3, forward as-is
    B──ELECTION(3)──►C   C: 4 > 3, replace
    C──ELECTION(4)──►D   D: 2 < 4, forward as-is
    D──ELECTION(4)──►A   A: 3 < 4, forward as-is
    A──ELECTION(4)──►B   B: 1 < 4, forward as-is
    B──ELECTION(4)──►C   C: 4 = 4, I WIN!
    C──ELECTED(4)───►D──►A──►B──►C  (all nodes learn C is leader)

  PROPERTIES:
    ─ O(N) messages per election (best case)
    ─ O(N²) messages (worst case: every node starts election)
    ─ Ring topology means single node failure breaks the ring
    ─ Used in: token ring networks (historical), some DHTs

  NOT USED in modern databases (too fragile, no log comparison).
```

---

## 5. Lease-Based Leadership

### 5.1 How Leases Work

```
LEASE-BASED LEADERSHIP:

  A lease is a time-bounded lock. The holder is the leader for the
  duration of the lease. Must be explicitly renewed.

  LEASE LIFECYCLE:

  ──────────────────────────────────────────────────────────────────►
  t=0        t=5         t=10       t=15        t=20       t=25
  │ A gets   │ A renews  │ A renews │ A crashes │ Lease    │ B gets
  │ lease    │ lease     │ lease    │           │ expires  │ lease
  │ (10s)    │ (→ t=15)  │ (→ t=20) │          │ (t=20)   │ (10s)
  │          │           │          │           │          │
  │←── A is leader ────────────────────────────►│←─ gap ──►│← B leads

  KEY PROPERTIES:
    1. Mutual exclusion: only one node holds the lease at a time
    2. Time-bounded: lease expires automatically if not renewed
    3. No deadlock: crash of lease holder releases the lock (after expiry)
    4. Gap between leaders: unavailability period = time until expiry

  LEASE DURATION TRADE-OFF:
    Short (1-3s):  fast failover, but requires frequent renewals
                   and is sensitive to clock drift
    Long (10-30s): resilient to transient issues, but slow failover
    Typical: 5-15 seconds

  CRITICAL DANGER — CLOCK DRIFT:

    ┌─────────────────────────────────────────────────────────────────┐
    │  Leader A acquires lease at t=0, valid until t=10.              │
    │                                                                 │
    │  Scenario: A's clock runs SLOW by 2 seconds.                   │
    │                                                                 │
    │  Real time:  0   2   4   6   8   10  12                        │
    │  A's clock:  0   1   3   5   7   9   11                        │
    │  Lease srv:  0   2   4   6   8   10  12                        │
    │                                                                 │
    │  At real_time=10: lease server says lease expired.              │
    │  At real_time=10: A's clock says 9, A thinks lease is valid!   │
    │  At real_time=10: B acquires new lease.                        │
    │                                                                 │
    │  → TWO leaders for ~1 second. Data corruption possible.        │
    │                                                                 │
    │  SOLUTION: Conservative expiry on the holder side.             │
    │  A considers its lease expired at:                              │
    │    A_expiry = grant_time + duration - max_clock_drift           │
    │  If max_clock_drift = 3s, A treats lease as valid until t=7.   │
    │  Buffer of 3 seconds covers the drift.                          │
    │                                                                 │
    │  Google Spanner: uses GPS + atomic clocks → drift < 7ms.       │
    │  Everyone else: NTP → drift can be 10-250ms (usually < 100ms). │
    └─────────────────────────────────────────────────────────────────┘
```

### 5.2 Lease vs Consensus-Based Election

```
LEASE VS CONSENSUS — COMPARISON:

  ┌────────────────────┬──────────────────────┬──────────────────────┐
  │ Property           │ Lease-based          │ Consensus (Raft/Paxos)│
  ├────────────────────┼──────────────────────┼──────────────────────┤
  │ Mutual exclusion   │ Depends on clocks    │ Guaranteed (terms)   │
  │ Failure detection  │ Implicit (expiry)    │ Explicit (timeout)   │
  │ Failover speed     │ = lease duration     │ = election timeout   │
  │ Clock dependency   │ YES (critical)       │ NO (logical clocks)  │
  │ Complexity         │ Low                  │ High                 │
  │ Renewal overhead   │ 1 RPC per renewal    │ N heartbeats         │
  │ External dependency│ Lease service        │ None (self-contained)│
  │ Split-brain risk   │ Yes (clock drift)    │ No (quorum + terms)  │
  └────────────────────┴──────────────────────┴──────────────────────┘

  WHEN TO USE LEASES:
    ─ External coordination (microservices picking a leader)
    ─ When you already have a consensus store (etcd, ZooKeeper)
    ─ Distributed locks for exclusive access to a resource
    ─ When simplicity outweighs the clock risk

  WHEN TO USE CONSENSUS:
    ─ Database replication (must be correct under all conditions)
    ─ When clock drift is unacceptable (safety-critical systems)
    ─ When the consensus infrastructure is already in place (Raft group)

  HYBRID APPROACH (common in practice):
    ─ Use consensus (Raft) for leader election
    ─ Use leases for read optimization (leaseholder serves reads
      without going through Raft, as long as lease is valid)
    ─ CockroachDB, YugabyteDB use this pattern
```

### 5.3 CockroachDB Epoch-Based Leases

```
COCKROACHDB EPOCH-BASED LEASES:

  CockroachDB uses a hybrid system: Raft for log replication,
  plus a node liveness table for range leases.

  NODE LIVENESS TABLE (stored in a special system range):
  ┌──────────┬───────┬──────────────┬──────────────┐
  │ NodeID   │ Epoch │ Expiration   │ Draining?    │
  ├──────────┼───────┼──────────────┼──────────────┤
  │ 1        │ 5     │ t + 9s       │ false        │
  │ 2        │ 3     │ t + 4s       │ false        │
  │ 3        │ 7     │ t + 8s       │ true         │
  └──────────┴───────┴──────────────┴──────────────┘

  HOW IT WORKS:
    1. Each node heartbeats its liveness record (every 4.5s, 9s lease)
    2. Heartbeat = Raft write to the liveness range
    3. If a node doesn't heartbeat → its epoch doesn't advance
    4. Range lease is valid only while the leaseholder's epoch matches
       the epoch in the liveness table
    5. If the leaseholder's node fails → its epoch eventually becomes
       stale → any other replica can acquire the lease

  RANGE LEASE:
    ┌─────────────────────────────────────────────────────────────────┐
    │  Range [a-m]:                                                   │
    │    Leaseholder: Node 1                                          │
    │    Lease epoch: 5 (matches Node 1's liveness epoch)             │
    │                                                                 │
    │  Node 1 heartbeats liveness → epoch stays 5 → lease valid      │
    │  Node 1 crashes → liveness epoch becomes stale                  │
    │  Node 2 notices → acquires new lease (epoch 6 for Node 2)      │
    │                                                                 │
    │  Advantage over time-based leases:                              │
    │  ─ No clock dependency for safety                               │
    │  ─ Epoch comparison is a logical check, not a time check        │
    │  ─ But still uses time for liveness expiration (availability)   │
    └─────────────────────────────────────────────────────────────────┘
```

---

## 6. Fencing and Split-Brain Prevention

### 6.1 The Split-Brain Problem

```
SPLIT-BRAIN — THE WORST-CASE SCENARIO:

  Two nodes both believe they are the leader for the same data:

  ┌──────────────┐        ┌──────────────┐
  │  Leader A     │        │  Leader B     │
  │  (old, stale) │        │  (new, valid) │
  └──────┬───────┘        └──────┬───────┘
         │                        │
    Client 1:                Client 2:
    "set x=5"               "set x=10"
         │                        │
         ▼                        ▼
    ┌─────────┐             ┌─────────┐
    │ x = 5   │             │ x = 10  │
    │ (on A's │             │ (on B's │
    │  storage)│             │  storage)│
    └─────────┘             └─────────┘

  Now x=5 on A's replicas, x=10 on B's replicas.
  Which is correct? Neither? Both? Data is DIVERGED.

  CAUSES OF SPLIT-BRAIN:
    1. Network partition + no quorum check
    2. GC pause: old leader pauses, new leader elected, old wakes up
    3. Clock drift: lease appears valid to old leader but has expired
    4. Bug: leadership state corrupted or not properly checked
    5. Asymmetric partition: old leader can reach some nodes but not others
```

### 6.2 Fencing Tokens

```
FENCING TOKEN PROTOCOL:

  A monotonically increasing token attached to every leadership grant.
  Storage nodes reject writes with stale tokens.

  HOW IT WORKS:

  Step 1: Leader election produces a fencing token
    Election round 1 → Leader A, token = 1
    Election round 2 → Leader B, token = 2
    (Token can be: Raft term, ZooKeeper zxid, lease epoch, etc.)

  Step 2: Leader includes token in all write requests
    Leader B: write(key="x", value=10, token=2)

  Step 3: Storage validates token
    Storage maintains: max_seen_token = 0 (initially)

    Receives write(token=2): 2 > 0 → accept, max_seen_token = 2
    Receives write(token=1): 1 < 2 → REJECT (stale leader)

  FULL SCENARIO:

  ┌─────────────────────────────────────────────────────────────────────┐
  │  t=0:  A is leader, token=1                                        │
  │  t=1:  A sends write(x=5, token=1) to storage                     │
  │        Storage: max_token=1, accepts                                │
  │  t=2:  A gets GC-paused for 30 seconds                             │
  │  t=3:  B detects A is unresponsive, wins election, token=2         │
  │  t=4:  B sends write(x=10, token=2) to storage                    │
  │        Storage: max_token=2, accepts                                │
  │  t=32: A wakes up from GC pause, thinks it's still leader         │
  │  t=33: A sends write(x=5, token=1) to storage                     │
  │        Storage: token=1 < max_token=2 → REJECTED                  │
  │  t=34: A receives rejection → realizes it's stale → steps down    │
  └─────────────────────────────────────────────────────────────────────┘

  WHAT SERVES AS A FENCING TOKEN:
    ─ Raft: term number (RequestVote, AppendEntries carry term)
    ─ ZooKeeper: zxid or session ID
    ─ etcd: revision number or lease ID
    ─ CockroachDB: epoch in node liveness table
    ─ Spanner: Paxos ballot number

  REQUIREMENT:
    The storage layer MUST be fencing-aware.
    If storage blindly accepts all writes, fencing tokens are useless.

    In Raft: followers reject AppendEntries from leaders with old terms.
    In ZooKeeper: ephemeral nodes tied to sessions provide fencing.
```

### 6.3 STONITH and Hardware Fencing

```
STONITH — "SHOOT THE OTHER NODE IN THE HEAD":

  Used in traditional HA clustering (Pacemaker/Corosync, Oracle RAC).
  When a node is suspected dead, PHYSICALLY POWER IT OFF to guarantee
  it cannot interfere.

  HOW IT WORKS:
    1. Node A suspects Node B is dead
    2. A sends command to B's IPMI/iLO/DRAC management interface
    3. B is forcibly powered off
    4. A knows WITH CERTAINTY that B is not running
    5. A takes over B's resources (VIP, shared storage, etc.)

  ┌─────────────────────────────────────────────────────────────────────┐
  │  Node A                            Node B                          │
  │    │                                  │                             │
  │    │  Heartbeat timeout!              │                             │
  │    │──────────IPMI POWER OFF─────────►│                             │
  │    │                                  X  (powered off)              │
  │    │                                                                │
  │    │  Confirmed: B is physically off                                │
  │    │  Safe to take over B's resources                              │
  │    │                                                                │
  │    │  Acquire VIP, mount shared storage, start services             │
  └─────────────────────────────────────────────────────────────────────┘

  USED BY:
    ─ Pacemaker/Corosync (Linux HA clusters)
    ─ Oracle RAC (via CSS kill)
    ─ Windows Server Failover Clustering (via IPMI/WMI)
    ─ VMware vSphere HA (via vCenter API)

  ADVANTAGES:
    ✓ Absolute certainty: dead node CANNOT cause split-brain
    ✓ No clock dependency, no fencing tokens needed

  DISADVANTAGES:
    ✗ Requires out-of-band management (IPMI, etc.)
    ✗ IPMI itself can be unreachable (network issue)
    ✗ Fencing can fail → cluster freezes (must wait for human)
    ✗ Not applicable in cloud environments (no IPMI access)
    ✗ Aggressive: powers off a node that might just be slow

  CLOUD EQUIVALENT:
    ─ AWS: ec2 stop-instances / ec2 terminate-instances
    ─ GCP: gcloud compute instances stop
    ─ Azure: az vm stop
    ─ But: API call may fail during outage (cloud control plane down)
```

---

## 7. Advanced Election Mechanics

### 7.1 Pre-Vote (Raft Extension)

```
PRE-VOTE — PREVENTING TERM INFLATION:

  PROBLEM: A partitioned node keeps incrementing its term.

  5-node cluster: {A, B, C, D, E}. A is leader (term 5).
  E gets network-partitioned.

  ┌──────────────────────────────────────────────────────────────────┐
  │  Partition: {A, B, C, D} | {E}                                  │
  │                                                                  │
  │  E: election timeout fires → becomes candidate, term 6         │
  │  E: sends RequestVote(term=6) → no responses (partitioned)     │
  │  E: timeout → term 7                                            │
  │  E: timeout → term 8                                            │
  │  ...                                                             │
  │  E: timeout → term 42                                           │
  │                                                                  │
  │  Partition heals. E sends RequestVote(term=42) to all.          │
  │  A (leader, term=5) sees term 42 → STEPS DOWN.                 │
  │  E's log is behind → E cannot win.                              │
  │  But A already stepped down → needless disruption.              │
  │  New election needed → cluster unavailable during election.     │
  └──────────────────────────────────────────────────────────────────┘

  SOLUTION — PRE-VOTE:

  Before starting a real election, send a PRE-VOTE request.
  If you can't get a majority of pre-votes, don't start the election.

  Pre-Vote message:
    "If I were to start an election for term (currentTerm + 1),
     would you vote for me?"

  Voter responds:
    "Yes, if your log is at least as up-to-date as mine AND
     I haven't heard from the current leader recently."

  KEY RULE: Pre-Vote does NOT increment the term.

  ┌──────────────────────────────────────────────────────────────────┐
  │  WITH PRE-VOTE:                                                  │
  │                                                                  │
  │  E: election timeout → sends PreVote(term=6) → no responses    │
  │  E: timeout → sends PreVote(term=6) → no responses             │
  │  ...  (term stays at 5, PreVote keeps saying term 6)            │
  │                                                                  │
  │  Partition heals.                                                │
  │  E sends PreVote(term=6) → "Would you vote for me?"            │
  │  A: "I'm a healthy leader, heard from followers recently. NO."  │
  │  B, C, D: "I've heard from leader A recently. NO."              │
  │                                                                  │
  │  E: Cannot get majority pre-votes → stays follower.             │
  │  A: Remains leader undisturbed. No disruption.                  │
  └──────────────────────────────────────────────────────────────────┘

  IMPLEMENTED BY:
    ─ etcd (since v3.4)
    ─ TiKV (enabled by default)
    ─ CockroachDB
    ─ Consul (since 1.16)

  NOT IMPLEMENTED BY:
    ─ Original Raft paper (it's an extension, Section 9.6 of thesis)
    ─ Some simpler Raft implementations
```

### 7.2 Check Quorum (Leader Self-Check)

```
CHECK QUORUM — LEADER PROACTIVELY STEPS DOWN:

  PROBLEM: A partitioned leader continues accepting client requests
  that will never be committed.

  SOLUTION: Leader checks that it can communicate with a majority.
  If not, it steps down proactively.

  MECHANISM:
    Every election_timeout period, leader checks:
    "Have I received messages from a majority of followers recently?"

    If YES → continue as leader.
    If NO  → step down to follower.

  ┌──────────────────────────────────────────────────────────────────┐
  │  5-node cluster: Leader A, Followers B, C, D, E                 │
  │                                                                  │
  │  Normal:  A receives AppendEntries responses from B, C, D, E   │
  │           → communicating with 4/4 followers → healthy          │
  │                                                                  │
  │  Partition: {A, B} | {C, D, E}                                  │
  │           A receives responses from B only (1/4 followers)      │
  │           1 + 1(self) = 2 < majority(3)                         │
  │           → A steps down proactively                             │
  │                                                                  │
  │  Result: {C, D, E} elect new leader quickly                    │
  │          No stale leader serving requests                       │
  └──────────────────────────────────────────────────────────────────┘

  COMBINED WITH PRE-VOTE:
    ─ Check Quorum: prevents stale leader from serving requests
    ─ Pre-Vote: prevents partitioned follower from disrupting cluster
    ─ Together: both sides of a partition are well-behaved

  USED BY: etcd, TiKV, CockroachDB (all with both Pre-Vote + Check Quorum)
```

### 7.3 Leader Transfer

```
LEADER TRANSFER — GRACEFUL LEADERSHIP HANDOFF:

  Use case: draining a node for maintenance, rebalancing load,
  placing the leader closer to clients.

  MECHANISM (Raft):

  Step 1: Admin sends TransferLeadership(targetId) to current leader.

  Step 2: Leader stops accepting new client requests.

  Step 3: Leader brings target up to date:
    ─ Sends remaining log entries to target
    ─ Waits for target to acknowledge

  Step 4: Leader sends TimeoutNow to target:
    ─ Target immediately starts election (no waiting for timeout)
    ─ Target's log is up to date → guaranteed to win

  Step 5: Target wins election, becomes new leader.

  ┌──────────────────────────────────────────────────────────────────┐
  │  Current Leader A                    Target B                    │
  │       │                                 │                        │
  │       │──AppendEntries (catch up)──────►│                        │
  │       │◄──ACK────────────────────────── │                        │
  │       │                                 │                        │
  │       │──TimeoutNow───────────────────►│                        │
  │       │                                 │ (starts election       │
  │       │                                 │  immediately)          │
  │       │◄──RequestVote (term N+1)───────│                        │
  │       │──VoteGranted─────────────────► │                        │
  │       │                                 │                        │
  │       │  (steps down to follower)       │ (new leader!)         │
  └──────────────────────────────────────────────────────────────────┘

  DOWNTIME: typically < 1 second (no election timeout wait)

  USED BY:
    ─ etcd: etcdctl move-leader
    ─ CockroachDB: ALTER RANGE ... RELOCATE LEASE TO ...
    ─ TiKV/PD: pd-ctl operator add transfer-leader
    ─ Kafka (KRaft): kafka-leader-election.sh --preferred-replica
```

### 7.4 Priority-Based Election

```
PRIORITY-BASED ELECTION:

  Some systems allow assigning priorities to nodes, influencing
  which node becomes leader.

  MONGODB REPLICA SET PRIORITIES:

    members = [
      { _id: 0, host: "dc1-node1:27017", priority: 10 },  // preferred
      { _id: 1, host: "dc1-node2:27017", priority: 5  },  // backup
      { _id: 2, host: "dc2-node1:27017", priority: 1  },  // last resort
      { _id: 3, host: "dc2-arb:27017",   priority: 0  },  // ARBITER (cannot be leader)
    ]

    Rules:
      ─ Higher priority nodes are preferred as leader
      ─ Priority 0 = can never become leader (arbiter or hidden)
      ─ A lower-priority leader will step down if a higher-priority
        member catches up and is reachable
      ─ This is NOT strict: a high-priority node with a stale log
        cannot win election (data safety always wins)

  RAFT WITH PRIORITIES (extension):
    ─ Not in the original Raft paper
    ─ TiKV implements priorities via leader transfer:
      PD (placement driver) detects suboptimal leader placement
      → triggers leader transfer to preferred node
    ─ CockroachDB rebalances leases based on zone configurations

  TRADE-OFF:
    ✓ Co-locate leader with clients for lower latency
    ✓ Place leader on more powerful hardware
    ✗ More complex election logic
    ✗ Potential for thrashing if priority node flaps
```

---

## 8. Failure Detection in Leaderless Systems

### 8.1 Dynamo-Style Systems (Cassandra, Riak, DynamoDB)

```
FAILURE DETECTION IN LEADERLESS SYSTEMS:

  Leaderless systems don't elect a leader, but they still need
  failure detection to:
    1. Route requests away from dead nodes
    2. Trigger hinted handoff to temporary holders
    3. Initiate data rebalancing / repair
    4. Update cluster membership

  CASSANDRA — GOSSIP + PHI-ACCRUAL:

    Gossip protocol:
      ─ Every 1 second, each node contacts 1-3 random peers
      ─ Exchanges heartbeat state: {nodeId → (generation, version)}
      ─ Piggybacks: node status, schema version, load info, tokens

    Phi-accrual failure detector:
      ─ Applied on top of gossip heartbeat arrival times
      ─ Default threshold: φ = 8
      ─ Adjustable: lower for faster detection, higher for fewer
        false positives

    Node lifecycle:
      NORMAL → SUSPECT (φ > threshold)
             → DOWN (gossip propagation complete)
             → REMOVED (administrator decommissions)

    ┌──────────────────────────────────────────────────────────────────┐
    │  Time  │ Event                                                   │
    ├────────┼─────────────────────────────────────────────────────────┤
    │  t=0   │ Node C stops responding to gossip                      │
    │  t=2s  │ Node A: φ(C) = 3.2 (suspicious but not dead)          │
    │  t=5s  │ Node A: φ(C) = 6.1 (probably dead)                    │
    │  t=8s  │ Node A: φ(C) = 9.4 → marks C as DOWN                 │
    │  t=8s  │ Node A gossips C's DOWN status to B, D, E             │
    │  t=12s │ All nodes aware: C is DOWN                             │
    │        │ Hinted handoff active for C's token ranges             │
    │  t=??  │ C comes back: gossip NORMAL, replay hints             │
    └────────┴─────────────────────────────────────────────────────────┘

    Total detection time: 8-15 seconds (gossip + phi convergence)
```

### 8.2 Consistent Hashing and Virtual Nodes Impact

```
FAILURE IMPACT ON CONSISTENT HASH RING:

  In a leaderless system using consistent hashing, when a node fails,
  its token ranges must be covered by other nodes.

  BEFORE FAILURE (6 nodes, 3 vnodes each):

     ┌──────────────────────────────────────┐
     │           Hash Ring                   │
     │                                       │
     │       A₁         B₁                  │
     │    F₃    ╲     ╱    C₁               │
     │   ╱        ●●●        ╲              │
     │  E₂       ╱   ╲       C₂            │
     │   ╲      ╱     ╲     ╱               │
     │    E₁  D₂      D₁  B₂              │
     │       A₃   F₂   A₂                  │
     │           F₁  B₃                     │
     └──────────────────────────────────────┘

  NODE D FAILS:
    ─ D₁, D₂ token ranges must be served by successor nodes
    ─ With replication factor 3: hinted handoff writes to next 3 nodes
    ─ Anti-entropy (Merkle tree repair) catches up remaining data
    ─ If D doesn't return: administrator decommissions → data rebalances

  KEY DIFFERENCE FROM LEADER-BASED:
    ─ No election needed. System continues serving reads/writes.
    ─ Availability maintained (if enough replicas survive).
    ─ Consistency may degrade (fewer replicas to form quorum).
```

---

## 9. Real-World Implementations

### 9.1 etcd (Raft)

```
ETCD LEADER ELECTION — PRODUCTION DETAILS:

  Configuration:
    heartbeat-interval:  100ms (default)
    election-timeout:    1000ms (default, randomized to [1000, 2000])
    Pre-Vote:            enabled (since v3.4)
    Check Quorum:        enabled (since v3.4)

  Election flow:
    1. Follower misses heartbeats for 1000-2000ms
    2. Sends PreVote to all peers
    3. If majority pre-votes received → starts real election
    4. Increments term, sends RequestVote
    5. Wins with majority → starts sending heartbeats (every 100ms)

  Monitoring:
    etcd_server_leader_changes_seen_total    (should be rare)
    etcd_server_is_leader                    (1 if this node is leader)
    etcd_server_proposals_pending            (should be 0 normally)
    etcd_network_peer_round_trip_time_seconds (latency between peers)

  PRODUCTION TUNING:
    ─ election-timeout should be 10× heartbeat-interval minimum
    ─ In high-latency networks (cross-DC): increase both
    ─ Example cross-DC: heartbeat=500ms, election-timeout=5000ms
    ─ NEVER set election-timeout < 5× one-way network latency

  COMMON FAILURES:
    ─ Disk I/O stall: WAL fsync blocks heartbeat → false election
      Fix: dedicated SSD for etcd data directory
    ─ CPU starvation: heartbeat thread not scheduled
      Fix: cpuset isolation, priority scheduling
    ─ Clock jump: NTP correction > election timeout → split
      Fix: use chronyd with step=0.1 (small steps only)
```

### 9.2 ZooKeeper (ZAB)

```
ZOOKEEPER FAILURE DETECTION AND ELECTION:

  Configuration:
    tickTime:     2000ms (base time unit)
    initLimit:    10 (ticks for initial sync: 10 × 2s = 20s)
    syncLimit:    5 (ticks for sync during operation: 5 × 2s = 10s)

  Session management:
    ─ Clients maintain sessions with ZooKeeper
    ─ Session timeout: configurable (default: 30 seconds)
    ─ Client sends heartbeats every sessionTimeout/3
    ─ If ZooKeeper doesn't hear from client for sessionTimeout
      → session expires → ephemeral nodes deleted

  Leader election:
    ─ Fast Leader Election (FLE) algorithm
    ─ Based on zxid comparison (not log length)
    ─ Requires majority agreement

  EPHEMERAL NODES FOR LEADER ELECTION (client-side pattern):

    Application nodes use ZooKeeper for leader election:

    ┌─────────────────────────────────────────────────────────────────┐
    │  1. Each app node creates an ephemeral sequential znode:        │
    │     /election/candidate-000000001  (App A)                     │
    │     /election/candidate-000000002  (App B)                     │
    │     /election/candidate-000000003  (App C)                     │
    │                                                                 │
    │  2. Lowest sequence number = leader                             │
    │     App A (001) is the leader.                                  │
    │                                                                 │
    │  3. Each non-leader watches the NEXT LOWER znode:              │
    │     App C watches candidate-000000002 (B's node)               │
    │     App B watches candidate-000000001 (A's node)               │
    │     (This avoids herd effect — only one watcher fires)         │
    │                                                                 │
    │  4. If App A crashes → session expires → ephemeral node deleted│
    │     App B's watch fires → B checks if it's now lowest → B leads│
    │                                                                 │
    │  5. If App C crashes → session expires → node deleted           │
    │     No one is watching C's node (no one has higher seq)        │
    │     No election needed.                                         │
    └─────────────────────────────────────────────────────────────────┘

  HERD EFFECT PROBLEM:
    ─ Naive approach: all nodes watch /election and react on change
    ─ If leader dies → ALL nodes wake up, read children, try to act
    ─ With 100 nodes → 100 simultaneous ZooKeeper reads → thundering herd
    ─ Solution: chain watching (each watches predecessor only)

  SESSION EXPIRY DANGERS:
    ─ GC pause > session timeout → session expires
    ─ App wakes up from GC, thinks it's still leader
    ─ But ZooKeeper deleted its ephemeral node during GC pause
    ─ Another node is now leader
    ─ MUST check znode existence before every leader-only operation
    ─ Or use fencing tokens (zxid of the ephemeral node)
```

### 9.3 Redis Sentinel

```
REDIS SENTINEL — FAILURE DETECTION AND FAILOVER:

  Redis Sentinel is a separate process that monitors Redis instances
  and orchestrates failover. Not consensus-based — uses gossip + quorum.

  ARCHITECTURE:
    ┌──────────┐  ┌──────────┐  ┌──────────┐
    │Sentinel 1│  │Sentinel 2│  │Sentinel 3│
    └────┬─────┘  └────┬─────┘  └────┬─────┘
         │             │             │
    PING every 1s  PING every 1s  PING every 1s
         │             │             │
         ▼             ▼             ▼
    ┌──────────┐  ┌──────────┐  ┌──────────┐
    │  Redis   │  │  Redis   │  │  Redis   │
    │  Master  │  │  Replica │  │  Replica │
    └──────────┘  └──────────┘  └──────────┘

  FAILURE DETECTION (two stages):

  Stage 1: SUBJECTIVE DOWN (SDOWN)
    ─ Sentinel sends PING to Redis instance every 1 second
    ─ If no valid reply within down-after-milliseconds (default 30s)
    ─ That Sentinel marks the instance as SDOWN (subjectively down)

  Stage 2: OBJECTIVE DOWN (ODOWN)
    ─ Sentinel asks other Sentinels: "Is this instance down?"
    ─ If quorum Sentinels agree instance is SDOWN → ODOWN
    ─ Quorum: configurable (typically majority: 2 of 3 sentinels)

  ┌──────────────────────────────────────────────────────────────────┐
  │  t=0:   Master stops responding                                 │
  │  t=30s: Sentinel 1: Master is SDOWN (30s no PING reply)        │
  │  t=31s: Sentinel 2: Master is SDOWN                             │
  │  t=31s: Sentinel 1 asks Sentinel 2, 3: "Is master SDOWN?"      │
  │         Sentinel 2: "Yes"                                        │
  │         Sentinel 3: "Not yet" (its timeout hasn't fired)        │
  │         Quorum = 2: Sentinel 1 + Sentinel 2 agree → ODOWN      │
  │  t=31s: Sentinel 1 starts failover as leader sentinel           │
  └──────────────────────────────────────────────────────────────────┘

  SENTINEL LEADER ELECTION (for failover coordination):

    Not full consensus — uses a simpler Raft-like mechanism:
    ─ Sentinel that detects ODOWN requests votes from other Sentinels
    ─ Each Sentinel votes for the first Sentinel that asks per epoch
    ─ If majority votes received → becomes failover leader
    ─ Failover leader selects best replica and promotes it

  REPLICA SELECTION (choosing new master):
    Priority: (in order)
      1. replica-priority (configurable, lower = preferred)
      2. Most data (highest replication offset)
      3. Lowest run_id (tie-breaker)

  FAILOVER STEPS:
    1. Sentinel leader selects best replica
    2. REPLICAOF NO ONE → promote replica to master
    3. Other replicas: REPLICAOF new-master-host new-master-port
    4. Sentinel updates its own configuration
    5. Sentinel publishes +switch-master on pub/sub channel

  TOTAL FAILOVER TIME:
    Detection:  down-after-milliseconds (30s default)
    Election:   ~1 second
    Promotion:  ~1-5 seconds
    TOTAL:      31-36 seconds (with defaults!)

    Aggressive: down-after-milliseconds=5000 → ~6-10 second failover
    Trade-off:  short timeout → false failovers during network blips

  LIMITATIONS:
    ─ NOT a true consensus protocol → split-brain possible
    ─ During failover: clients may write to old master
    ─ Those writes are LOST when old master becomes replica
    ─ Redis acknowledges: "Redis Sentinel is AP, not CP"
    ─ For CP: use Redis Cluster (gossip-based) or external lock (Redlock)
```

### 9.4 Consul (Raft + SWIM)

```
CONSUL — DUAL-LAYER FAILURE DETECTION:

  Consul uses TWO different protocols for different purposes:

  Layer 1: RAFT (Server Nodes Only)
    ─ Leader election among Consul server nodes (3 or 5)
    ─ Replicated log for KV store, service catalog, ACLs
    ─ Standard Raft: heartbeats, election timeout, terms

  Layer 2: SWIM/Gossip (All Nodes — Servers + Clients)
    ─ Cluster membership: who's alive, who's dead
    ─ Health check results propagation
    ─ Uses HashiCorp's memberlist library (SWIM implementation)

  ARCHITECTURE:
    ┌──────────────────────────────────────────────────────────────────┐
    │  Consul Server 1 (leader) ◄──Raft──► Server 2 ◄──Raft──► Srv 3│
    │       │                          │                    │          │
    │       │ SWIM gossip              │ SWIM gossip        │ SWIM    │
    │       │                          │                    │          │
    │  ┌────┴────┐  ┌────────┐  ┌────┴────┐  ┌────────┐  ┌┴───────┐ │
    │  │Client A │  │Client B│  │Client C │  │Client D│  │Client E│ │
    │  │(agent)  │  │(agent) │  │(agent)  │  │(agent) │  │(agent) │ │
    │  └─────────┘  └────────┘  └─────────┘  └────────┘  └────────┘ │
    └──────────────────────────────────────────────────────────────────┘

  SWIM memberlist parameters:
    Protocol period:        1 second
    Probe timeout:          500ms
    Indirect probes (k):    3
    Suspicion multiplier:   4 (suspicion_timeout = 4 × log(N) × period)
    Retransmit multiplier:  4 (gossip retransmits = 4 × log(N))

  WHY TWO PROTOCOLS:
    ─ Raft is too expensive for 1000+ nodes (O(N) messages from leader)
    ─ SWIM scales to large clusters (O(1) per node per period)
    ─ Raft handles critical state (consensus, linearizable KV)
    ─ SWIM handles membership (eventually consistent, but fast)

  HEALTH CHECKING:
    Each Consul agent performs health checks on local services:
    ─ TCP check (port open?)
    ─ HTTP check (returns 200?)
    ─ Script check (exit code 0?)
    ─ gRPC check (serving status?)
    ─ TTL check (service self-reports within TTL)

    Results propagated via gossip → available in DNS / API within seconds.
```

### 9.5 MongoDB Replica Set Election

```
MONGODB REPLICA SET ELECTION:

  MongoDB uses a modified Raft-like protocol for replica set elections.

  PARAMETERS:
    electionTimeoutMillis:  10000ms (default, configurable)
    heartbeatIntervalMillis: 2000ms
    catchUpTimeoutMillis:   -1 (infinite, wait for catch-up)

  NODE TYPES:
    ─ Primary: accepts reads and writes (the leader)
    ─ Secondary: replicates from primary, can serve reads (if configured)
    ─ Arbiter: votes in elections but holds no data (priority=0)
    ─ Hidden: not visible to clients, used for analytics/backup
    ─ Delayed: intentionally behind by N seconds (for recovery)

  ELECTION TRIGGER:
    1. Secondaries check primary via heartbeats every 2 seconds
    2. If no heartbeat response within electionTimeoutMillis (10s)
    3. Eligible secondary calls an election

  ELECTION PROTOCOL:
    ─ Based on Raft but with modifications:
      ─ Uses optime (operation timestamp) instead of log index
      ─ Supports priority-based preferences
      ─ Includes catch-up phase after election

  ┌──────────────────────────────────────────────────────────────────┐
  │  Step 1: Secondary detects primary failure (10s timeout)         │
  │                                                                  │
  │  Step 2: Dry-run election (similar to Pre-Vote)                  │
  │    ─ "Would you vote for me?" without incrementing term          │
  │    ─ If no majority → don't bother with real election            │
  │                                                                  │
  │  Step 3: Real election (if dry-run succeeds)                     │
  │    ─ Increment term                                               │
  │    ─ Send requestVote to all members                             │
  │    ─ Include last applied optime for comparison                  │
  │                                                                  │
  │  Step 4: Win election with majority                              │
  │                                                                  │
  │  Step 5: CATCH-UP PHASE (unique to MongoDB)                      │
  │    ─ New primary may be slightly behind the freshest secondary   │
  │    ─ New primary pulls missing oplogs from other members          │
  │    ─ Timeout configurable (catchUpTimeoutMillis)                 │
  │    ─ After catch-up → new primary starts accepting writes        │
  │                                                                  │
  │  Total failover time: typically 10-12 seconds                    │
  └──────────────────────────────────────────────────────────────────┘

  ROLLBACK ON OLD PRIMARY:
    ─ When old primary reconnects, it may have writes that weren't
      replicated before it failed
    ─ These writes are ROLLED BACK (saved to a rollback directory)
    ─ With w:majority write concern → no rollback needed
    ─ With w:1 (default) → data loss possible during failover

    ┌──────────────────────────────────────────────────────────────┐
    │  w:1 (acknowledge after primary only):                       │
    │    Client writes → Primary ACKs → Primary crashes            │
    │    Write not yet on any secondary → LOST                     │
    │                                                              │
    │  w:majority:                                                 │
    │    Client writes → Primary replicates → majority ACK → ACK  │
    │    If primary crashes, majority has the write → SAFE         │
    │    Cost: higher write latency (wait for replication)          │
    └──────────────────────────────────────────────────────────────┘
```

### 9.6 Kafka (KRaft)

```
KAFKA KRAFT — METADATA LEADER ELECTION:

  KRaft replaced ZooKeeper as Kafka's metadata management layer
  (fully production-ready since Kafka 3.5+).

  ARCHITECTURE:
    ─ Controller quorum: 3 or 5 dedicated controller nodes
    ─ Controllers run Raft for metadata consensus
    ─ One controller is the ACTIVE controller (Raft leader)
    ─ Brokers fetch metadata from the active controller

  PARAMETERS:
    controller.quorum.election.timeout.ms:   1000 (randomized)
    controller.quorum.fetch.timeout.ms:      2000
    controller.quorum.election.backoff.max.ms: 1000

  BROKER FAILURE DETECTION:
    ─ Brokers send heartbeats to controller
    ─ broker.session.timeout.ms: 9000 (default)
    ─ If no heartbeat within timeout → broker fenced
    ─ Fenced broker: its partitions trigger leader election
      for those partitions (different from controller election)

  PARTITION LEADER ELECTION (different from controller election):

    Each Kafka partition has replicas. One replica is the LEADER.
    The controller decides partition leaders (not via Raft vote).

    ┌──────────────────────────────────────────────────────────────┐
    │  Partition P0: replicas = [Broker 1, Broker 2, Broker 3]    │
    │                ISR (In-Sync Replicas) = [1, 2, 3]           │
    │                Leader = Broker 1                             │
    │                                                              │
    │  Broker 1 fails:                                             │
    │    Controller detects (heartbeat timeout)                    │
    │    Controller picks new leader from ISR: Broker 2            │
    │    Controller updates metadata log (Raft commit)             │
    │    Brokers fetch updated metadata                            │
    │    Broker 2 starts serving as leader for P0                  │
    │                                                              │
    │  Preferred leader: if unclean.leader.election.enable=false   │
    │    and ALL ISR are dead → partition is UNAVAILABLE            │
    │    (safety over availability: no data loss)                  │
    │                                                              │
    │  Unclean leader election (enable=true):                      │
    │    If ALL ISR are dead → pick ANY replica (even out-of-sync) │
    │    → availability but POTENTIAL DATA LOSS                    │
    └──────────────────────────────────────────────────────────────┘
```

---

## 10. Failure Scenarios and War Stories

### 10.1 Classic Failure Patterns

```
PATTERN 1: CASCADING FAILURE FROM AGGRESSIVE DETECTION

  Scenario (real incident pattern):
    ─ Node A is under heavy load (CPU 95%)
    ─ Heartbeat thread doesn't get scheduled for 3 seconds
    ─ Failure detector marks A as dead
    ─ A's data is rebalanced to B and C
    ─ B and C now handle extra load → they become slow
    ─ B's heartbeats are delayed → B is marked dead
    ─ B's data is rebalanced to C and D
    ─ CASCADE → entire cluster goes down

  Prevention:
    ─ Longer timeouts under load (backpressure-aware detection)
    ─ Rate-limit rebalancing (don't move data immediately)
    ─ Circuit breaker: if > N nodes fail simultaneously, STOP and alert
    ─ Pre-provisioned capacity: don't run at 95% CPU normally


PATTERN 2: SPLIT-BRAIN FROM ASYMMETRIC PARTITION

  Scenario:
    ─ 3-node cluster: A (leader), B, C
    ─ Asymmetric partition: A→B works, B→A fails
    ─ A sends heartbeats to B → B thinks A is alive
    ─ B's responses to A are lost → A doesn't know B got them
    ─ C can reach both A and B
    ─ A's election timeout fires (no responses from B, C)
    ─ But A can still send to B!
    ─ A calls election → B and C see A's RequestVote → they vote
    ─ A is re-elected → BUT the asymmetry remains

  Prevention:
    ─ Check Quorum: leader must hear FROM followers (bidirectional)
    ─ Symmetric heartbeat: both sides must confirm communication
    ─ SWIM indirect probes detect asymmetric failures


PATTERN 3: GC PAUSE CAUSING STALE LEADERSHIP

  Scenario (Java-based systems: ZooKeeper, Cassandra, Kafka):
    ─ Leader has 60GB heap, running G1GC
    ─ Full GC pause: 35 seconds (stop-the-world)
    ─ During pause:
      ─ No heartbeats sent (process frozen)
      ─ Followers elect new leader (after 10s timeout)
      ─ New leader accepts writes for 25 seconds
    ─ Old leader wakes up from GC
    ─ Old leader's in-memory state says it's still leader
    ─ Old leader tries to serve requests
    ─ WITHOUT fencing: old leader corrupts data

  Prevention:
    ─ Fencing tokens (term/epoch check on every operation)
    ─ Smaller heaps + concurrent GC (ZGC, Shenandoah)
    ─ Non-JVM implementations (ScyllaDB instead of Cassandra)
    ─ Lease validation before every leader action
    ─ Dedicated GC tuning: max pause time targets


PATTERN 4: CLOCK SKEW BREAKING LEASE-BASED LEADERSHIP

  Scenario:
    ─ Leader acquires 10-second lease at leader_clock=0
    ─ Leader's clock drifts: 100ms slow per second
    ─ After 10 real seconds: leader's clock shows 9 seconds
    ─ Leader thinks: "1 second left on my lease"
    ─ Lease server (correct clock): "lease expired"
    ─ New leader acquires lease
    ─ TWO leaders for ~1 second

  Prevention:
    ─ Conservative lease expiry (subtract max_drift)
    ─ Tightly synchronized clocks (GPS, PTP, chrony)
    ─ Use logical clocks instead of wall clocks when possible
    ─ Google's TrueTime (7ms max drift guaranteed by hardware)


PATTERN 5: NETWORK PARTITION HEALS → DISRUPTION

  Scenario (without Pre-Vote):
    ─ 5-node cluster: leader A, partition isolates E
    ─ E's election timeouts fire repeatedly: term 10, 11, 12, ... 50
    ─ Partition heals after 5 minutes
    ─ E sends RequestVote(term=50) to all nodes
    ─ A (leader, term=5) sees term 50 → steps down
    ─ E can't win (log is behind) → new election needed
    ─ Unnecessary disruption

  Prevention:
    ─ Pre-Vote: E can't inflate its term without majority support
    ─ Implemented in etcd, TiKV, CockroachDB
```

### 10.2 Production Incident Patterns

```
REAL INCIDENT PATTERNS FROM MAJOR OUTAGES:

  ┌─────────────────────────────────────────────────────────────────────┐
  │  GITHUB (2012): MySQL Failover Loop                                │
  │                                                                     │
  │  ─ Network switch firmware bug caused intermittent packet loss      │
  │  ─ MySQL failover triggered (primary unreachable)                  │
  │  ─ New primary promoted, old primary fenced                        │
  │  ─ Network briefly recovered → old primary looked healthy          │
  │  ─ Failover triggered AGAIN (back to old primary)                  │
  │  ─ Loop repeated multiple times → extended outage                  │
  │                                                                     │
  │  Lesson: Add failover cooldown period. Don't flip back immediately.│
  └─────────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────────┐
  │  CLOUDFLARE (2020): etcd Leader Election Storm                     │
  │                                                                     │
  │  ─ etcd cluster experienced repeated leader elections              │
  │  ─ Cause: disk I/O latency spikes (noisy neighbor on shared SSD)  │
  │  ─ WAL fsync took > election timeout → leader stepped down        │
  │  ─ New leader elected → same I/O problem → stepped down again     │
  │  ─ Thrashing: no stable leader → control plane unavailable        │
  │                                                                     │
  │  Lesson: Dedicated SSDs for consensus stores. Monitor fsync        │
  │  latency. Set election timeout > max observed fsync latency.       │
  └─────────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────────┐
  │  STRIPE (2019): Single-Node Failure → Cascading Database Outage   │
  │                                                                     │
  │  ─ One database node had a hardware issue (slow memory)            │
  │  ─ Queries to that node were slow → connection pool exhausted      │
  │  ─ Connection exhaustion propagated to application tier             │
  │  ─ Retry storms from applications overloaded remaining nodes       │
  │  ─ Failure detection was correct but recovery caused more damage   │
  │                                                                     │
  │  Lesson: Circuit breakers at every layer. Exponential backoff      │
  │  with jitter. Shedding load is better than cascading failure.      │
  └─────────────────────────────────────────────────────────────────────┘
```

---

## 11. Tuning and Operational Guidance

### 11.1 Election Timeout Tuning

```
ELECTION TIMEOUT TUNING GUIDELINES:

  Rule of thumb (Raft):
    broadcastTime << electionTimeout << MTBF

    broadcastTime = 1 round-trip to all peers
    electionTimeout = 10× to 20× broadcastTime
    MTBF = mean time between failures (months)

  ┌──────────────────────────────────────────────────────────────────┐
  │  Environment        │ Network RTT │ Recommended Election Timeout │
  ├──────────────────────┼─────────────┼──────────────────────────────┤
  │  Same rack          │ 0.1-0.5ms   │ 50-200ms                     │
  │  Same datacenter    │ 0.5-2ms     │ 150-500ms                    │
  │  Same region (AZs)  │ 1-5ms       │ 500-2000ms                   │
  │  Cross-region       │ 50-150ms    │ 2000-10000ms                 │
  │  Global (US-EU-AP)  │ 100-300ms   │ 5000-30000ms                 │
  └──────────────────────┴─────────────┴──────────────────────────────┘

  TUNING PROCESS:
    1. Measure p99 network RTT between consensus nodes
    2. Measure p99 disk fsync latency (WAL writes)
    3. Set heartbeat_interval = 2× max(RTT_p99, fsync_p99)
    4. Set election_timeout = 10× heartbeat_interval
    5. Monitor leader election rate: should be < 1/month in steady state
    6. If elections happen too often: increase timeout
    7. If failover is too slow: decrease timeout (accept more false positives)

  ANTI-PATTERNS:
    ✗ election_timeout = 100ms on cross-region network (constant elections)
    ✗ election_timeout = 60 seconds (minute-long outages on failure)
    ✗ heartbeat_interval > election_timeout / 3 (too few heartbeats)
    ✗ Same timeout for all environments (LAN vs WAN needs different values)
```

### 11.2 Monitoring Checklist

```
WHAT TO MONITOR FOR HEALTHY FAILURE DETECTION:

  ┌─────────────────────────────────────────────────────────────────────┐
  │  METRIC                          │ ALERT CONDITION                  │
  ├──────────────────────────────────┼──────────────────────────────────┤
  │  Leader changes per hour         │ > 0 (investigate any election)  │
  │  Time since last leader change   │ < 1 hour (frequent elections)   │
  │  Heartbeat RTT (p99)             │ > election_timeout / 5         │
  │  WAL fsync latency (p99)         │ > heartbeat_interval           │
  │  Network packet loss             │ > 0.1%                         │
  │  NTP clock offset                │ > 100ms (lease-based systems)  │
  │  Proposals pending (Raft)        │ > 0 for > 5 seconds            │
  │  Uncommitted log entries         │ > 100 entries                  │
  │  Follower lag (bytes/entries)    │ > 1000 entries                 │
  │  GC pause duration (JVM)         │ > heartbeat_interval           │
  │  CPU utilization on leader       │ > 80% (heartbeat thread starved)│
  │  File descriptor count           │ approaching ulimit              │
  │  Disk space for WAL              │ < 20% free                     │
  └──────────────────────────────────┴──────────────────────────────────┘

  DASHBOARD ESSENTIALS:
    1. Leader identity over time (who is leader and when did it change?)
    2. Heartbeat RTT histogram (are we close to timing out?)
    3. Election timeline (overlay with system events — deploys, GC, etc.)
    4. Split-brain detector (are multiple nodes claiming leadership?)
    5. Cluster membership changes (nodes joining/leaving)
```

### 11.3 Testing Failure Handling

```
CHAOS ENGINEERING FOR FAILURE DETECTION:

  TOOLS:
    ─ tc (Linux traffic control): add latency, packet loss, partitions
    ─ iptables: simulate network partitions between specific nodes
    ─ kill -STOP / kill -CONT: simulate GC pause (freeze process)
    ─ stress / stress-ng: simulate CPU/memory/disk pressure
    ─ toxiproxy (Shopify): programmable TCP proxy for fault injection
    ─ Jepsen (Kyle Kingsbury): formal distributed systems testing

  TEST SCENARIOS (must-have):

    1. KILL LEADER
       kill -9 leader_pid
       Verify: new leader elected within expected timeout
       Verify: no data loss for committed transactions
       Verify: clients reconnect to new leader

    2. NETWORK PARTITION (majority vs minority)
       iptables -A INPUT -s follower_ip -j DROP
       Verify: majority side elects new leader
       Verify: minority side steps down / stops serving writes
       Verify: partition heals → cluster reconverges

    3. ASYMMETRIC PARTITION
       tc qdisc add dev eth0 root netem loss 100% (one direction only)
       Verify: no split-brain
       Verify: affected node detects the asymmetry

    4. SLOW DISK (gray failure)
       Use dm-delay or fault injection to add 5-second fsync latency
       Verify: leader steps down if it can't maintain heartbeats
       Verify: system doesn't cascade (only the slow node is affected)

    5. CLOCK SKEW
       chronyc makestep 10 (jump clock forward/backward)
       Verify: lease-based systems handle the skew safely
       Verify: Raft-based systems are unaffected (logical clocks)

    6. PROCESS PAUSE (simulate GC)
       kill -STOP leader_pid; sleep 30; kill -CONT leader_pid
       Verify: new leader elected during pause
       Verify: old leader steps down when it wakes up
       Verify: no split-brain during the transition

    7. ROLLING RESTART
       Restart nodes one at a time
       Verify: at most one election per restart
       Verify: no data loss, no unavailability window > election timeout

  JEPSEN TESTS (gold standard):
    ─ Tests linearizability under partitions, crashes, clock skew
    ─ Has found bugs in: etcd, CockroachDB, MongoDB, Redis, Kafka,
      Cassandra, YugabyteDB, TiDB, and dozens more
    ─ If your system hasn't been Jepsen-tested, it probably has bugs
```

---

## 12. Comparison Tables

### 12.1 Failure Detection Mechanisms

```
┌──────────────────────┬────────────────┬───────────────┬──────────────┬───────────────┐
│ Mechanism            │ Fixed Timeout  │ Phi-Accrual   │ SWIM/Gossip  │ App-Level     │
├──────────────────────┼────────────────┼───────────────┼──────────────┼───────────────┤
│ Complexity           │ Very simple    │ Moderate      │ Moderate     │ Variable      │
│ Adaptiveness         │ None           │ Automatic     │ Indirect     │ Manual        │
│ Scalability          │ O(N) per node  │ O(N) per node │ O(1) per node│ O(1) per node │
│ False positive rate  │ Fixed          │ Tunable (φ)   │ Low          │ Low           │
│ Detection speed      │ = timeout      │ Adaptive      │ O(log N)     │ Variable      │
│ Network awareness    │ No             │ Yes           │ Yes          │ No            │
│ Used by              │ Raft, ZK       │ Cassandra,    │ Consul, Serf │ CockroachDB,  │
│                      │                │ Akka          │              │ custom apps   │
└──────────────────────┴────────────────┴───────────────┴──────────────┴───────────────┘
```

### 12.2 Leader Election Protocols

```
┌──────────────────────┬─────────────┬──────────────┬─────────────┬──────────────┐
│ Property             │ Raft        │ ZAB          │ Bully       │ Ring         │
├──────────────────────┼─────────────┼──────────────┼─────────────┼──────────────┤
│ Safety guarantee     │ Yes (terms) │ Yes (epochs) │ No          │ No           │
│ Log comparison       │ Yes         │ Yes (zxid)   │ No          │ No           │
│ Split-brain safe     │ Yes         │ Yes          │ No          │ No           │
│ Message complexity   │ O(N)        │ O(N²)        │ O(N²)       │ O(N)         │
│ Partition tolerant   │ Yes (quorum)│ Yes (quorum) │ No          │ No           │
│ Election speed       │ 1-10s       │ 2-10s        │ O(N) rounds │ O(N) rounds  │
│ Understandability    │ High        │ Medium       │ Very high   │ High         │
│ Used in production   │ etcd, CRDB, │ ZooKeeper    │ MongoDB     │ Historical   │
│                      │ TiKV, Consul│              │ (variant)   │              │
└──────────────────────┴─────────────┴──────────────┴─────────────┴──────────────┘
```

### 12.3 Production Systems Comparison

```
┌──────────────────┬────────────────┬────────────────┬────────────────┬──────────────┐
│ System           │ Detection      │ Election       │ Failover Time  │ Split-Brain  │
│                  │ Mechanism      │ Protocol       │ (typical)      │ Prevention   │
├──────────────────┼────────────────┼────────────────┼────────────────┼──────────────┤
│ etcd             │ Fixed timeout  │ Raft+PreVote   │ 1-2s           │ Term+Quorum  │
│ ZooKeeper        │ Tick-based     │ ZAB (FLE)      │ 2-10s          │ Epoch+Quorum │
│ CockroachDB      │ Epoch liveness │ Raft+PreVote   │ 3-9s           │ Epoch+Term   │
│ TiKV/TiDB        │ Fixed timeout  │ Raft+PreVote   │ 3-10s          │ Term+Quorum  │
│ MongoDB          │ Fixed timeout  │ Raft variant   │ 10-12s         │ Term+Optime  │
│ Redis Sentinel   │ PING+Gossip    │ Sentinel vote  │ 30-36s*        │ Quorum only  │
│ Cassandra        │ Gossip+φ       │ None (leaderl.)│ N/A            │ N/A          │
│ Consul           │ SWIM+Raft      │ Raft           │ 5-10s          │ Term+Quorum  │
│ Kafka (KRaft)    │ Fixed timeout  │ Raft           │ 5-15s          │ Term+Quorum  │
│ Spanner          │ Chubby leases  │ Paxos          │ ~10s           │ Ballot+Quorum│
│ YugabyteDB       │ Fixed timeout  │ Raft           │ 3-10s          │ Term+Quorum  │
└──────────────────┴────────────────┴────────────────┴────────────────┴──────────────┘

* Redis Sentinel default (down-after-milliseconds=30000). Can be tuned to 5-10s.
```

### 12.4 Decision Guide

```
CHOOSING A FAILURE DETECTION + ELECTION STRATEGY:

  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │  Need strong consistency (linearizability)?                        │
  │  ├── YES → Raft or Paxos (with fencing tokens)                    │
  │  │         ├── Small cluster (3-7 nodes) → Raft (simpler)         │
  │  │         ├── Google-scale → Paxos (more flexible)               │
  │  │         └── Latency-sensitive → EPaxos (leaderless consensus)  │
  │  │                                                                 │
  │  └── NO → What scale?                                              │
  │           ├── < 20 nodes → Fixed heartbeat timeout                 │
  │           ├── 20-500 nodes → SWIM (gossip-based)                   │
  │           └── 500+ nodes → SWIM + hierarchical (Consul model)      │
  │                                                                     │
  │  Need fast failover (< 3 seconds)?                                 │
  │  ├── YES → Raft with aggressive timeout (100-500ms HB)            │
  │  │         But: higher false positive rate                         │
  │  │         Consider: dedicated network, SSD, CPU isolation         │
  │  │                                                                 │
  │  └── NO → Conservative timeout (1-10s) for stability              │
  │                                                                     │
  │  Running in the cloud (no IPMI)?                                   │
  │  ├── YES → Software fencing (tokens, epochs, quorum checks)       │
  │  └── NO (bare metal) → Consider STONITH for absolute safety       │
  │                                                                     │
  │  Cross-region deployment?                                          │
  │  ├── YES → Long election timeouts (5-30s)                         │
  │  │         Consider: region-local leaders with witness regions     │
  │  │         CockroachDB / Spanner model: per-range leaders in      │
  │  │         the region closest to the data                          │
  │  └── NO → Standard timeouts work fine                              │
  │                                                                     │
  └─────────────────────────────────────────────────────────────────────┘
```

---

## Key Takeaways

```
PRINCIPLES FOR FAILURE DETECTION AND LEADER ELECTION:

  1. You CANNOT distinguish slow from dead. Accept this.
     Design for the ambiguity, don't try to eliminate it.

  2. Every failure detector has a false positive rate.
     Tune for your use case: fast detection OR stability, not both.

  3. Consensus-based election (Raft, Paxos, ZAB) is the gold standard.
     Use it for any system where split-brain is unacceptable.

  4. Fencing tokens are NON-NEGOTIABLE for leader-based systems.
     Without them, GC pauses and partitions WILL cause split-brain.

  5. Pre-Vote + Check Quorum are essential Raft extensions.
     Use them. They're not optional in production.

  6. Gray failures are harder than total failures.
     Monitor beyond heartbeats: disk I/O, latency, error rates.

  7. Test your failure handling under realistic conditions.
     Chaos engineering is not optional for distributed databases.
     Jepsen is the gold standard for correctness testing.

  8. Clock drift breaks leases. Use conservative expiry or
     logical clocks. If you must use wall clocks, synchronize
     them tightly (NTP chrony, PTP, or GPS/atomic like Spanner).

  9. Failover time = detection time + election time + catch-up time.
     Optimize each component separately.

  10. The simplest correct solution wins. Raft won over Paxos not
      because it's better, but because it's easier to get right.
```
