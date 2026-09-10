# Consensus (Raft) and Distributed Locking: A Complete Interview-Ready Deep Dive

A production-grade reference covering the Raft consensus algorithm end-to-end (leader election, log replication, safety, membership changes, read optimizations), distributed locking primitives (leases, fencing tokens, lock services), and the coordination systems built on top of them (etcd, ZooKeeper, Consul). Every section ties back to how these primitives appear in system design interviews -- from "how does your metadata store stay consistent?" to "how do you prevent two workers from processing the same job?"

Prerequisites: familiarity with distributed system models from the `README.md` roadmap and failure detection from `29-failure-detection-phi-accrual.md`. For leader election algorithms at a higher level, see `../databases/16-failure-detection-and-leader-election.md`.

---

## Table of Contents

1. [Why Consensus Exists](#1-why-consensus-exists)
2. [Raft Fundamentals](#2-raft-fundamentals)
3. [Raft Leader Election](#3-raft-leader-election)
4. [Raft Log Replication](#4-raft-log-replication)
5. [Raft Safety Guarantees](#5-raft-safety-guarantees)
6. [Raft Membership Changes](#6-raft-membership-changes)
7. [Raft Read Optimizations](#7-raft-read-optimizations)
8. [Raft in Production: etcd, CockroachDB, TiKV](#8-raft-in-production-etcd-cockroachdb-tikv)
9. [Distributed Locking Fundamentals](#9-distributed-locking-fundamentals)
10. [Lock Service Implementations](#10-lock-service-implementations)
11. [ZooKeeper Coordination Primitives](#11-zookeeper-coordination-primitives)
12. [etcd Coordination Primitives](#12-etcd-coordination-primitives)
13. [The Redlock Controversy](#13-the-redlock-controversy)
14. [Distributed Locking Patterns for ML/AI Systems](#14-distributed-locking-patterns-for-mlai-systems)
15. [Failure Modes and Debugging](#15-failure-modes-and-debugging)
16. [Interview Patterns](#16-interview-patterns)

---

## 1. Why Consensus Exists

### 1.1 The Core Problem

Multiple nodes must agree on a single value (or sequence of values) despite node crashes and network failures. Without consensus, you get split-brain: two nodes both think they are the leader, two workers both process the same job, two replicas diverge permanently.

```
THE SPLIT-BRAIN PROBLEM:

Network partition splits a 5-node cluster:

  Partition A: [Node1, Node2]    Partition B: [Node3, Node4, Node5]
  
  Without consensus:
    Node1 thinks it's leader → accepts writes
    Node3 thinks it's leader → accepts writes
    When partition heals: conflicting data, data loss
  
  With consensus (Raft, majority quorum = 3):
    Node1 cannot get majority (only 2 nodes) → rejects writes
    Node3 gets majority (3 nodes) → accepts writes
    When partition heals: Node1 and Node2 catch up from Node3's log
    No data loss, no conflicts
```

### 1.2 FLP Impossibility

Fischer, Lynch, and Paterson (1985) proved that no deterministic consensus algorithm can guarantee termination in a fully asynchronous system with even one crash failure. Every practical consensus algorithm (Raft, Paxos, Zab) circumvents FLP by using timeouts (partial synchrony) — they assume the network will eventually deliver messages within some bound, even if that bound is unknown.

### 1.3 Consensus vs. Coordination

| Concept | What it solves | Example |
|---|---|---|
| Consensus | Agreement on a sequence of values (replicated log) | Raft, Paxos, Zab |
| Leader election | Choosing one leader from a set of candidates | Built on consensus |
| Distributed locking | Mutual exclusion across processes/nodes | Built on consensus or leases |
| Service discovery | Agreeing on which services are alive and where | Built on consensus + health checks |
| Configuration management | Agreeing on the current system configuration | Built on consensus |

All of these are built on top of consensus. That's why etcd (Raft-based) and ZooKeeper (Zab-based) serve as the foundation for Kubernetes, Kafka, and most distributed systems.

---

## 2. Raft Fundamentals

### 2.1 Design Goal: Understandability

Raft was designed by Diego Ongaro and John Ousterhout (2014) as an alternative to Paxos that is easier to understand, implement, and reason about. The key design decision: decompose consensus into three independent subproblems:

```
Raft decomposition:

1. LEADER ELECTION
   How to choose a leader when the current one fails.
   
2. LOG REPLICATION
   How the leader replicates its log to followers.
   
3. SAFETY
   How to guarantee that committed entries are never lost
   and all nodes apply the same sequence of commands.
```

### 2.2 Server States

Every Raft node is in exactly one of three states at any time:

```
                    ┌────────────────────────────────────────────┐
                    │                                            │
                    │  timeout,              receives votes      │
                    │  start election        from majority       │
                    ▼                                            │
┌──────────┐    ┌──────────────┐    ┌──────────────┐           │
│ FOLLOWER │───>│  CANDIDATE   │───>│   LEADER     │           │
│          │    │              │    │              │           │
│ - Passive│    │ - Requests   │    │ - Sends      │           │
│ - Waits  │    │   votes      │    │   heartbeats │           │
│   for    │    │ - Votes for  │    │ - Replicates │           │
│   RPCs   │    │   self       │    │   log entries│           │
└──────────┘    └──────────────┘    └──────────────┘           │
      ▲               │                    │                    │
      │               │                    │                    │
      │         discovers leader      discovers server         │
      │         or new term          with higher term           │
      │               │                    │                    │
      └───────────────┘                    └────────────────────┘

Invariant: at most one leader per term.
```

### 2.3 Terms

Raft divides time into terms of arbitrary length. Each term begins with an election. Terms act as a logical clock — if a node receives a message with a higher term number, it immediately steps down to follower and updates its term.

```
Time ──────────────────────────────────────────────────>

Term 1        Term 2      Term 3     Term 4
┌──────────┐  ┌────────┐  ┌──────┐   ┌──────────────────
│ Election │  │Election│  │Elect.│   │ Election │ Normal
│ + Normal │  │(split  │  │  +   │   │ + Normal operation
│ operation│  │ vote,  │  │Normal│   │ operation│
│          │  │no      │  │      │   │          │
│  Leader: │  │leader) │  │  L:  │   │  Leader: │
│  Node A  │  │        │  │Node C│   │  Node B  │
└──────────┘  └────────┘  └──────┘   └──────────────────

Properties:
  - Each term has at most one leader (may have zero if election fails)
  - Term numbers only increase, never decrease
  - A node's current term is included in every RPC
  - If a node sees a higher term → step down to follower
  - If a node receives a stale RPC (lower term) → reject it
```

---

## 3. Raft Leader Election

### 3.1 Election Mechanism

```
Step-by-step leader election:

1. FOLLOWER TIMEOUT
   Follower has not received heartbeat from leader for
   election_timeout (randomized: 150-300ms).
   
   Follower increments its term and becomes Candidate.

2. CANDIDATE REQUESTS VOTES
   Candidate sends RequestVote RPC to all other nodes:
   {
     term:          3,           // candidate's new term
     candidateId:   "node2",
     lastLogIndex:  47,          // index of candidate's last log entry
     lastLogTerm:   2            // term of candidate's last log entry
   }

3. VOTE DECISION (each node votes at most once per term)
   Vote YES if:
     - Candidate's term >= voter's current term
     - Voter has not already voted in this term
     - Candidate's log is at least as up-to-date as voter's log
       (compare lastLogTerm first, then lastLogIndex)
   
   Vote NO otherwise.

4. ELECTION OUTCOME
   a) Candidate receives majority of votes → becomes Leader
      Immediately sends heartbeat to all nodes to establish authority.
   
   b) Another node becomes leader (receives AppendEntries with >= term)
      Candidate steps down to Follower.
   
   c) Election timeout expires with no winner (split vote)
      Candidate starts new election with incremented term.
      Randomized timeouts make split votes unlikely to repeat.
```

### 3.2 Why Randomized Timeouts Matter

```
Without randomization (all nodes timeout at 150ms):

  Time 0ms:    Leader crashes
  Time 150ms:  Node2, Node3, Node4 ALL become candidates simultaneously
               Each votes for itself → 3-way split → no majority → no leader
  Time 300ms:  All three try again → another split
  ...          This can repeat indefinitely (livelock)

With randomization (timeout between 150-300ms):

  Time 0ms:    Leader crashes
  Time 167ms:  Node3 times out first → becomes candidate
  Time 167ms:  Node3 sends RequestVote to Node2 (timeout 231ms) and Node4 (timeout 289ms)
  Time 168ms:  Node2 receives RequestVote, has not timed out yet → votes YES
  Time 169ms:  Node4 receives RequestVote, has not timed out yet → votes YES
  Time 170ms:  Node3 has 3 votes (including self) out of 4 → becomes Leader

  Election completes in ~3ms after first timeout.
```

### 3.3 Pre-Vote Extension

A partition-isolated node keeps incrementing its term and calling elections. When the partition heals, it has a very high term number that forces the entire cluster to step down momentarily (disrupting the healthy leader). The Pre-Vote extension (added in etcd) prevents this:

```
Without Pre-Vote:

  Partition:  [Node1(leader), Node2, Node3]  |  [Node4, Node5]
  
  Node4 and Node5 repeatedly time out and increment term:
    Term 5, 6, 7, 8, 9, 10, 11, ...
  
  Partition heals. Node4 sends RequestVote with term=47.
  Node1 (term=5) sees higher term → steps down!
  Entire cluster disrupted for a new election.

With Pre-Vote:

  Before incrementing term, candidate sends PreVote RPC:
    "Would you vote for me IF I started an election?"
  
  Nodes that can still reach the current leader respond NO.
  Node4's PreVote gets rejected → does not increment term.
  Partition heals: Node4 quietly rejoins with its original term.
  No disruption.
```

### 3.4 Election Timing Configuration

```
Raft timing requirements:

  broadcastTime << electionTimeout << MTBF

  broadcastTime:    0.5-20ms  (one round-trip RPC, datacenter)
  electionTimeout:  150-300ms (randomized within this range)
  MTBF:            months     (mean time between failures)

Production settings (etcd):
  heartbeat-interval:   100ms   (leader sends heartbeat every 100ms)
  election-timeout:     1000ms  (follower starts election after 1s)
  
  Why 1000ms and not 150ms?
  - Datacenter networks have occasional latency spikes
  - GC pauses in Java-based systems (ZooKeeper) can exceed 200ms
  - Too-aggressive timeout → unnecessary elections → instability
  - etcd recommends election-timeout = 10 × heartbeat-interval
```

---

## 4. Raft Log Replication

### 4.1 The Replicated Log

The log is the core data structure. Every state machine command is first appended to the log, then replicated to a majority, then applied to the state machine.

```
Raft replicated log:

Leader (Node1):
  Index:  1     2     3     4     5     6     7
  Term:  [1]   [1]   [1]   [2]   [3]   [3]   [3]
  Cmd:   [x=1] [y=2] [x=3] [y=7] [x=5] [z=1] [y=9]
                                          ▲
                                     commitIndex=6
                                     (replicated to majority)

Follower (Node2):
  Index:  1     2     3     4     5     6
  Term:  [1]   [1]   [1]   [2]   [3]   [3]
  Cmd:   [x=1] [y=2] [x=3] [y=7] [x=5] [z=1]
                                          ▲
                                     Matches leader through index 6

Follower (Node3 — lagging):
  Index:  1     2     3     4     5
  Term:  [1]   [1]   [1]   [2]   [3]
  Cmd:   [x=1] [y=2] [x=3] [y=7] [x=5]
                                   ▲
                                  Behind, needs entries 6-7
```

### 4.2 AppendEntries RPC

The leader replicates log entries using AppendEntries RPCs (also used as heartbeats when the entries list is empty):

```
AppendEntries RPC:

Leader → Follower:
{
  term:           3,        // leader's current term
  leaderId:       "node1",
  prevLogIndex:   5,        // index of entry immediately before new ones
  prevLogTerm:    3,        // term of prevLogIndex entry
  entries:        [         // new entries to append (empty for heartbeat)
    {index: 6, term: 3, cmd: "z=1"},
    {index: 7, term: 3, cmd: "y=9"}
  ],
  leaderCommit:   6         // leader's commit index
}

Follower response:
{
  term:     3,
  success:  true    // if prevLogIndex/prevLogTerm matched
}

Consistency check:
  Follower checks: "Do I have an entry at index 5 with term 3?"
  YES → append entries 6 and 7, respond success=true
  NO  → respond success=false
       Leader decrements prevLogIndex and retries (log repair)
```

### 4.3 Log Repair (Conflicting Entries)

When a follower has conflicting entries (from a previous leader that crashed), the new leader must repair the follower's log:

```
Log divergence scenario:

Leader (Node1, term 3):
  [1:1] [2:1] [3:1] [4:2] [5:3] [6:3] [7:3]

Follower (Node2 — was briefly leader in term 2):
  [1:1] [2:1] [3:1] [4:2] [5:2] [6:2]
                              ▲     ▲
                           Entries from old term 2, not committed,
                           conflict with leader's entries

Repair process:
  1. Leader sends AppendEntries(prevLogIndex=6, prevLogTerm=3)
     Follower: entry 6 has term 2, not 3 → reject (success=false)
  
  2. Leader backs up: AppendEntries(prevLogIndex=5, prevLogTerm=3)
     Follower: entry 5 has term 2, not 3 → reject
  
  3. Leader backs up: AppendEntries(prevLogIndex=4, prevLogTerm=2)
     Follower: entry 4 has term 2 → match! success=true
     Follower deletes entries 5-6, appends leader's entries 5-7.
  
After repair:
  [1:1] [2:1] [3:1] [4:2] [5:3] [6:3] [7:3]  ← matches leader

Optimization: follower includes conflicting term and first index of that term
in rejection, so leader can skip backward faster than one entry at a time.
```

### 4.4 Commit Rules

An entry is committed when the leader has replicated it to a majority of servers. Once committed, the entry is durable and will eventually be applied to every server's state machine.

```
Commit process for a 5-node cluster (majority = 3):

1. Client sends command "x=5" to Leader
2. Leader appends to its log: index=7, term=3, cmd="x=5"
3. Leader sends AppendEntries to all followers in parallel

   Node1 (leader):  stored at index 7  ✓
   Node2:           receives, stores   ✓  (2/5 = not yet committed)
   Node3:           receives, stores   ✓  (3/5 = COMMITTED!)
   Node4:           slow, not yet      ✗
   Node5:           slow, not yet      ✗

4. Leader advances commitIndex to 7 (majority have it)
5. Leader applies "x=5" to state machine
6. Leader responds to client: success
7. Next heartbeat tells followers about new commitIndex
8. Followers apply committed entries to their state machines

Critical safety rule:
  A leader NEVER commits entries from previous terms by counting replicas.
  It only commits entries from its own term, and commitment of a current-term
  entry implicitly commits all preceding entries (Log Matching Property).
```

---

## 5. Raft Safety Guarantees

### 5.1 The Five Raft Guarantees

```
1. ELECTION SAFETY
   At most one leader can be elected in a given term.
   Proof: each node votes at most once per term, majority required.

2. LEADER APPEND-ONLY
   A leader never overwrites or deletes entries in its log.
   It only appends new entries.

3. LOG MATCHING
   If two logs contain an entry with the same index and term,
   then the logs are identical in all entries through that index.
   Proof: AppendEntries consistency check (prevLogIndex, prevLogTerm).

4. LEADER COMPLETENESS
   If an entry is committed in a given term, that entry will be
   present in the logs of the leaders for all higher-numbered terms.
   Proof: voting restriction — candidates must have all committed entries.

5. STATE MACHINE SAFETY
   If a server has applied a log entry at a given index to its state machine,
   no other server will ever apply a different log entry for the same index.
   Follows from Log Matching + Leader Completeness.
```

### 5.2 The Voting Restriction (Why It's Critical)

The voting restriction is what prevents committed entries from being lost after a leader change:

```
RequestVote includes: (lastLogTerm, lastLogIndex)

Voter grants vote only if candidate's log is at least as up-to-date:

  Compare lastLogTerm first:
    If candidate's lastLogTerm > voter's lastLogTerm → vote YES
    If candidate's lastLogTerm < voter's lastLogTerm → vote NO
  
  If terms are equal, compare lastLogIndex:
    If candidate's lastLogIndex >= voter's lastLogIndex → vote YES
    If candidate's lastLogIndex <  voter's lastLogIndex → vote NO

Why this works:
  A committed entry exists on a majority of servers.
  A winning candidate must get votes from a majority.
  These two majorities overlap in at least one server.
  That server will not vote for a candidate missing the committed entry.
  Therefore, every leader has all committed entries.
```

### 5.3 The Commitment Rule for Previous Terms

```
DANGEROUS scenario without the commitment rule:

  Term 1: Node1 is leader, replicates entry at index 2 to Node2 only (2/5)
  Term 2: Node1 crashes. Node5 becomes leader (did not have index 2)
  Term 3: Node1 recovers, becomes leader again
  
  Can Node1 now commit the entry at index 2 by replicating it to Node3?
  
  NO! Even though 3/5 nodes now have it, if Node5 becomes leader again,
  it could overwrite that entry (it won election without it).
  
  SAFE rule: Leader only commits entries from its current term.
  When a current-term entry is committed, all previous entries
  are implicitly committed (they precede it in the log).

  Practical impact: after election, leader appends a no-op entry
  in the new term and replicates it. Once the no-op commits,
  all previous entries are safely committed.
```

---

## 6. Raft Membership Changes

### 6.1 The Problem with Direct Switchover

Changing cluster membership (adding/removing nodes) cannot be done atomically across all nodes. During the transition, there could be two disjoint majorities, each electing its own leader:

```
UNSAFE direct switchover from 3 nodes to 5 nodes:

  Time T: Some nodes use old config [A,B,C], others use new [A,B,C,D,E]
  
  Old config majority: 2 of 3 (e.g., A and B)   → elect leader
  New config majority: 3 of 5 (e.g., C, D, E)   → elect leader
  
  TWO LEADERS simultaneously. Data corruption.
```

### 6.2 Joint Consensus (Ongaro's Original Approach)

```
Two-phase membership change:

Phase 1: Transition to joint configuration C_old,new
  - Leader creates C_old,new log entry
  - Both old and new configs must agree (double majority)
  - Decisions require majority of C_old AND majority of C_new
  
Phase 2: Transition to C_new
  - Once C_old,new is committed, leader creates C_new entry
  - After C_new is committed, old nodes can be removed

                C_old          C_old,new          C_new
  Timeline: ──────────────|─────────────────|─────────────
                          ▲                  ▲
                   C_old,new entry      C_new entry
                   committed            committed
  
  During C_old,new: no single majority from either old or new config
  can make a decision alone. Safety preserved.
```

### 6.3 Single-Node Membership Changes (etcd's Approach)

A simpler alternative used by etcd: change one node at a time. Adding or removing a single node from any majority-based cluster is safe because the old and new majorities always overlap:

```
Single-node change safety proof:

  3 nodes → 4 nodes:
    Old majority: 2 of 3
    New majority: 3 of 4
    Overlap guaranteed: 2 + 3 - 4 = 1 (at least 1 common node)
  
  4 nodes → 5 nodes:
    Old majority: 3 of 4
    New majority: 3 of 5
    Overlap guaranteed: 3 + 3 - 5 = 1
  
  Any N → N+1:
    Old majority: ⌊N/2⌋ + 1
    New majority: ⌊(N+1)/2⌋ + 1
    Overlap: always ≥ 1

  To go from 3 nodes to 5 nodes:
    Step 1: 3 → 4 (add Node D)
    Step 2: 4 → 5 (add Node E)
    
  Each step is safe. Two separate Raft log entries.
```

### 6.4 Learner Nodes

Before promoting a new node to a voting member, it should catch up on the log as a non-voting learner. Otherwise, the new node's empty log drags down the cluster:

```
Adding a node without learner phase:

  Cluster: [A, B, C] with 1M log entries
  Add D (empty log) as voting member
  
  New majority: 3 of 4
  D has 0 entries, needs to replicate 1M entries
  During replication: D cannot contribute to commits
  If B goes down: only A and C are functional (2 of 4 = no majority!)
  
  Cluster is effectively less available during catch-up.

With learner phase:
  1. Add D as learner (non-voting, receives log but cannot vote)
  2. D catches up to leader's log (may take minutes for large state)
  3. Once D is caught up: promote D to voting member
  4. Now D can immediately participate in consensus
```

---

## 7. Raft Read Optimizations

### 7.1 The Problem: Linearizable Reads Are Expensive

A naive linearizable read must go through the Raft log (propose a read command, replicate to majority, then read). This is the same cost as a write — unacceptable for read-heavy workloads.

```
Naive linearizable read (SLOW):

  Client → Leader: read(key="x")
  Leader: append "read x" to log
  Leader: replicate to majority
  Leader: commit, apply, return value of x
  
  Cost: 1 RTT to followers + disk fsync = 2-10ms per read
  
  For a key-value store doing 100K reads/sec: impossible
```

### 7.2 ReadIndex

The leader confirms it is still the leader by exchanging a round of heartbeats, then reads from its local state machine without committing a log entry:

```
ReadIndex optimization:

  Client → Leader: read(key="x")
  
  1. Leader records current commitIndex as readIndex
  2. Leader sends heartbeat to all followers
  3. Majority of followers respond (confirms leader is still leader)
  4. Leader waits until state machine has applied through readIndex
  5. Leader reads "x" from local state machine, returns to client
  
  Cost: 1 RTT (heartbeat round), no disk I/O, no log entry
  
  Why step 2 is needed:
  Without confirmation, a partitioned ex-leader might serve stale reads.
  The heartbeat round proves no new leader has been elected.
```

### 7.3 Lease-Based Reads

If the leader holds a time-based lease, it can skip even the heartbeat round and serve reads directly from local state:

```
Lease read optimization:

  Leader sends heartbeats every 100ms.
  After majority of followers respond, leader starts a lease:
    lease_start = now()
    lease_duration = election_timeout / clock_drift_bound
                   = 1000ms / 1.001 ≈ 999ms
  
  During the lease period, no other node can become leader
  (followers won't time out and start an election).
  
  Leader can serve reads directly from local state machine:
    if now() < lease_start + lease_duration:
      return local_state_machine.get(key)
    else:
      fall back to ReadIndex

  Cost: 0 RTTs, 0 disk I/O
  
  Risk: depends on clock accuracy.
  If the leader's clock runs fast, it might think the lease is valid
  when followers have already timed out and elected a new leader.
  
  CockroachDB uses this with clock uncertainty bounds.
  etcd uses ReadIndex by default (safer, 1 RTT overhead).
```

### 7.4 Follower Reads

Serve reads from followers to distribute load, while maintaining linearizability:

```
Follower read:

  Client → Follower: read(key="x")
  
  1. Follower asks Leader: "What is the current commitIndex?"
     (ReadIndex RPC)
  2. Leader confirms leadership (heartbeat round), returns commitIndex=47
  3. Follower waits until its state machine has applied through index 47
  4. Follower reads "x" from its local state machine
  
  Cost: 1 RTT to leader + leader's heartbeat RTT = 2 RTTs
  
  Benefit: distributes read load across all nodes.
  CockroachDB and TiKV use this for follower reads.
```

---

## 8. Raft in Production: etcd, CockroachDB, TiKV

### 8.1 etcd

etcd is the most widely deployed Raft implementation. It is the metadata store for Kubernetes (every pod, service, configmap, secret is an etcd key).

```
etcd architecture:

  ┌─────────────────────────────────────────────────────┐
  │                     etcd cluster                     │
  │                                                      │
  │  ┌──────────┐  ┌──────────┐  ┌──────────┐          │
  │  │  etcd-1  │  │  etcd-2  │  │  etcd-3  │          │
  │  │ (leader) │  │(follower)│  │(follower)│          │
  │  │          │  │          │  │          │          │
  │  │ ┌──────┐ │  │ ┌──────┐ │  │ ┌──────┐ │          │
  │  │ │ Raft │ │  │ │ Raft │ │  │ │ Raft │ │          │
  │  │ └──┬───┘ │  │ └──┬───┘ │  │ └──┬───┘ │          │
  │  │    │     │  │    │     │  │    │     │          │
  │  │ ┌──▼───┐ │  │ ┌──▼───┐ │  │ ┌──▼───┐ │          │
  │  │ │ WAL  │ │  │ │ WAL  │ │  │ │ WAL  │ │          │
  │  │ └──┬───┘ │  │ └──┬───┘ │  │ └──┬───┘ │          │
  │  │    │     │  │    │     │  │    │     │          │
  │  │ ┌──▼───┐ │  │ ┌──▼───┐ │  │ ┌──▼───┐ │          │
  │  │ │BoltDB│ │  │ │BoltDB│ │  │ │BoltDB│ │          │
  │  │ │(MVCC)│ │  │ │(MVCC)│ │  │ │(MVCC)│ │          │
  │  │ └──────┘ │  │ └──────┘ │  │ └──────┘ │          │
  │  └──────────┘  └──────────┘  └──────────┘          │
  └─────────────────────────────────────────────────────┘

Key design decisions:
  - BoltDB (now bbolt) for on-disk key-value storage
  - MVCC: every key has a revision history (enables watches)
  - Watch API: clients subscribe to key changes (Kubernetes informers)
  - Lease API: time-bounded key ownership (distributed locks, leader election)
  - Default: 3 or 5 nodes. More than 7 is not recommended.
  - Max recommended DB size: 8 GB
  - Not designed for high-throughput data storage — it's a coordination service
```

### 8.2 CockroachDB (Multi-Raft)

CockroachDB runs a separate Raft group per range (contiguous key range, default 512 MB). A single node participates in thousands of Raft groups simultaneously:

```
CockroachDB Multi-Raft:

  Key space: [a────────────────────────────────────────z]
  
  Range 1: [a──────f]   Raft group: {Node1*, Node2, Node3}
  Range 2: [f──────m]   Raft group: {Node2*, Node3, Node4}
  Range 3: [m──────s]   Raft group: {Node3*, Node1, Node4}
  Range 4: [s──────z]   Raft group: {Node4*, Node1, Node2}
  
  * = leaseholder (serves reads and coordinates writes)

  Each range is independently replicated via Raft.
  Leadership is distributed across nodes for load balancing.
  A node with 1000 ranges runs 1000 Raft state machines.
  
  Optimization: batch Raft messages between the same pair of nodes.
  Instead of 1000 separate RPCs, send one RPC with 1000 Raft messages.
```

### 8.3 TiKV

TiKV (the storage layer of TiDB) uses a similar Multi-Raft design with RocksDB as the storage engine instead of BoltDB:

```
TiKV architecture:

  ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
  │   TiKV-1    │     │   TiKV-2    │     │   TiKV-3    │
  │             │     │             │     │             │
  │  Region 1*  │     │  Region 1   │     │  Region 1   │
  │  Region 4   │     │  Region 2*  │     │  Region 3   │
  │  Region 5*  │     │  Region 4*  │     │  Region 3*  │
  │             │     │             │     │             │
  │  RocksDB    │     │  RocksDB    │     │  RocksDB    │
  │  (Raft log) │     │  (Raft log) │     │  (Raft log) │
  │  RocksDB    │     │  RocksDB    │     │  RocksDB    │
  │  (State)    │     │  (State)    │     │  (State)    │
  └─────────────┘     └─────────────┘     └─────────────┘
  
  Two RocksDB instances per node:
    1. Raft log engine (sequential writes, periodic compaction)
    2. State machine engine (actual key-value data)
  
  PD (Placement Driver): central coordinator that tracks region locations,
  triggers splits/merges/rebalancing. Similar to CockroachDB's gossip layer.
```

---

## 9. Distributed Locking Fundamentals

### 9.1 Why Distributed Locks Are Hard

A distributed lock seems simple: acquire a lock before doing work, release it when done. The difficulty is in the failure modes:

```
THE PROCESS PAUSE PROBLEM (Martin Kleppmann, 2016):

  1. Client A acquires lock (lease TTL = 10 seconds)
  2. Client A starts working...
  3. Client A enters a GC pause (or page fault, or swap) for 15 seconds
  4. Lock expires (TTL elapsed)
  5. Client B acquires the same lock
  6. Client B starts working on the same resource
  7. Client A wakes up from GC pause, thinks it still has the lock
  8. Client A and Client B both modify the resource → DATA CORRUPTION

  The lock did not provide mutual exclusion.
  Client A held a belief ("I have the lock") that was no longer true.
```

### 9.2 Fencing Tokens

The solution to the process pause problem is fencing tokens — monotonically increasing numbers attached to each lock acquisition:

```
Fencing token pattern:

  1. Client A acquires lock, receives fencing token = 33
  2. Client A's GC pause...
  3. Lock expires
  4. Client B acquires lock, receives fencing token = 34
  5. Client A wakes up, writes to storage with token = 33
  6. Storage: "I last saw token 34. Token 33 is stale. REJECT."
  7. Client B writes to storage with token = 34 → ACCEPTED

  ┌──────────┐     ┌──────────────┐     ┌──────────────────┐
  │ Client A │     │  Lock Service │     │  Storage/DB      │
  │          │     │              │     │                  │
  │ acquire()│────>│ grant token=33│    │                  │
  │   ...    │     │              │     │                  │
  │ (paused) │     │ TTL expires  │     │                  │
  │          │     │              │     │                  │
  │ Client B │────>│ grant token=34│    │                  │
  │ write()  │     │              │────>│ accept token=34  │
  │          │     │              │     │                  │
  │ (resumed)│     │              │     │                  │
  │ write()  │────────────────────────>│ REJECT token=33  │
  │          │     │              │     │ (< 34)           │
  └──────────┘     └──────────────┘     └──────────────────┘

  Requirements:
  - Lock service issues monotonically increasing tokens
  - Storage system checks token on every write
  - Stale tokens are rejected
```

### 9.3 Lease-Based Locks

A lease is a time-bounded lock. The holder must renew the lease before it expires, or it is automatically released:

```
Lease lifecycle:

  acquire(key, ttl=10s) → lease_id=12345
  
  Time 0s:  lease acquired, TTL = 10s
  Time 5s:  renew(lease_id=12345) → TTL reset to 10s
  Time 10s: renew(lease_id=12345) → TTL reset to 10s
  Time 15s: (client crashes, no renewal)
  Time 25s: lease expires, lock released automatically
  
  Key property: no manual "unlock" required.
  Even if the lock holder crashes permanently, the lock
  is eventually released. Deadlock is impossible.

  Trade-off: TTL must balance between:
    - Too short (1s): healthy clients lose the lock during transient issues
    - Too long (60s): unhealthy client blocks others for 60 seconds
    - Typical production: 10-30 seconds with renewal at half the TTL
```

---

## 10. Lock Service Implementations

### 10.1 etcd Distributed Lock

etcd provides distributed locks built on its lease and MVCC primitives:

```
etcd lock implementation:

  1. Create a lease:
     lease_id = etcd.lease.grant(ttl=15)
  
  2. Create a key with the lease, using a unique prefix:
     etcd.put(
       key   = "/locks/my-resource/" + lease_id,
       value = "holder-identity",
       lease = lease_id
     )
  
  3. Get all keys under the lock prefix, ordered by create_revision:
     keys = etcd.get_prefix("/locks/my-resource/", sort=CREATE_REVISION)
  
  4. If your key has the lowest create_revision → you hold the lock.
     Otherwise, watch the key with the next-lower revision.
     When that key is deleted → you're next in line.
  
  5. Keep the lease alive:
     background goroutine calls etcd.lease.keepalive(lease_id) every 5s
  
  6. Release:
     etcd.delete("/locks/my-resource/" + lease_id)
     or: lease expires if client crashes

  Properties:
  - Fair (FIFO): waiters served in order of create_revision
  - Deadlock-free: leases auto-expire
  - Built on Raft consensus: linearizable, survives minority failures
```

### 10.2 ZooKeeper Distributed Lock

```
ZooKeeper lock using ephemeral sequential nodes:

  1. Create ephemeral sequential node:
     path = zk.create("/locks/my-resource/lock-", ephemeral=True, sequential=True)
     → creates "/locks/my-resource/lock-0000000042"
  
  2. Get all children of the lock path:
     children = zk.get_children("/locks/my-resource/")
     → ["lock-0000000040", "lock-0000000041", "lock-0000000042"]
  
  3. If your node has the lowest sequence number → you hold the lock.
  
  4. Otherwise, watch the node with the next-lower sequence number:
     zk.exists("/locks/my-resource/lock-0000000041", watch=True)
     Wait for watch event (node deleted = lock released).
  
  5. Lock is automatically released when:
     - Client explicitly deletes the node
     - Client's session expires (ephemeral node auto-deleted)
     - Client crashes (ZooKeeper detects via session heartbeat)

  Why watch only the next-lower node (not all nodes)?
    → Avoids "thundering herd": when the lock holder releases,
      only the next waiter is notified, not all N waiters.
```

### 10.3 Redis Distributed Lock (Simple)

```
Simple Redis lock (single instance):

  ACQUIRE:
    SET resource_name unique_value NX PX 30000
    
    NX: set only if key does not exist (atomic test-and-set)
    PX: expire after 30000ms (auto-release)
    unique_value: UUID, used to prevent releasing someone else's lock
  
  RELEASE (must be atomic):
    -- Lua script to atomically check-and-delete
    if redis.call("GET", KEYS[1]) == ARGV[1] then
      return redis.call("DEL", KEYS[1])
    else
      return 0
    end
  
  Why unique_value matters:
    Without it: Client A's lock expires, Client B acquires,
    Client A (thinking it still has the lock) calls DEL → deletes B's lock.
    
    With unique_value: Client A's DEL checks the value first,
    sees it belongs to B, and does nothing.

  Limitation: single Redis instance is a single point of failure.
  If Redis crashes, the lock is lost.
```

---

## 11. ZooKeeper Coordination Primitives

### 11.1 ZooKeeper Data Model

```
ZooKeeper namespace (hierarchical, similar to a filesystem):

  /
  ├── /kafka
  │   ├── /kafka/brokers
  │   │   ├── /kafka/brokers/ids
  │   │   │   ├── /kafka/brokers/ids/0   (ephemeral: broker 0 is alive)
  │   │   │   ├── /kafka/brokers/ids/1   (ephemeral: broker 1 is alive)
  │   │   │   └── /kafka/brokers/ids/2   (ephemeral: broker 2 is alive)
  │   │   └── /kafka/brokers/topics
  │   │       ├── /kafka/brokers/topics/orders
  │   │       └── /kafka/brokers/topics/events
  │   ├── /kafka/controller              (ephemeral: current controller)
  │   └── /kafka/consumers
  ├── /hbase
  │   └── /hbase/master                  (ephemeral: current master)
  └── /services
      └── /services/payment-service
          ├── /services/payment-service/instance-001  (ephemeral)
          └── /services/payment-service/instance-002  (ephemeral)

Node types:
  Persistent:            survives client disconnect, must be explicitly deleted
  Ephemeral:             automatically deleted when client session expires
  Persistent Sequential: persistent + auto-incrementing suffix
  Ephemeral Sequential:  ephemeral + auto-incrementing suffix (used for locks, queues)
```

### 11.2 Key ZooKeeper Primitives

```
1. SERVICE DISCOVERY (ephemeral nodes):
   Client creates ephemeral node under /services/my-service/
   Other clients watch /services/my-service/ for child changes
   When client crashes → ephemeral node deleted → watchers notified
   
2. LEADER ELECTION (ephemeral sequential):
   Each candidate creates ephemeral sequential node under /election/
   Node with lowest sequence number is the leader
   If leader crashes → ephemeral node deleted → next in line becomes leader

3. DISTRIBUTED BARRIER:
   Create /barrier node
   Clients watch /barrier, block until it's deleted
   Coordinator deletes /barrier → all clients unblock simultaneously

4. DISTRIBUTED QUEUE:
   Producers: create persistent sequential nodes under /queue/
   Consumers: get children, process lowest sequence number, delete it

5. GROUP MEMBERSHIP:
   Each member creates ephemeral node under /groups/my-group/
   Watch children → notified when members join or leave

6. CONFIGURATION MANAGEMENT:
   Store config in /config/my-service
   All instances watch the node
   Update config → all instances notified in ~100ms
```

### 11.3 ZooKeeper vs etcd

| Feature | ZooKeeper | etcd |
|---|---|---|
| Consensus | Zab (Paxos-derived) | Raft |
| Language | Java | Go |
| Data model | Hierarchical (tree) | Flat key-value with prefix ranges |
| Watch mechanism | One-time watches (must re-register) | Persistent watches (stream of events) |
| Session model | Client sessions with heartbeats | Leases with TTL |
| Ephemeral nodes | Yes (auto-delete on session expire) | Via lease attachment |
| Sequential nodes | Native | Must implement with revision numbers |
| Max data per node | 1 MB (default) | No per-key limit (8 GB total) |
| Linearizable reads | Yes (leader serves all reads by default) | Yes (ReadIndex or lease reads) |
| MVCC | No (current state only) | Yes (revision history, compact-able) |
| Used by | Kafka (legacy), HBase, Hadoop, Solr | Kubernetes, CoreDNS, Vitess, CockroachDB |
| Operational complexity | High (JVM tuning, GC pauses) | Lower (single binary, Go runtime) |

---

## 12. etcd Coordination Primitives

### 12.1 Watch API

etcd's watch API is the foundation of Kubernetes' informer pattern — the mechanism by which every Kubernetes controller learns about changes:

```
etcd watch:

  watcher = etcd.watch_prefix("/registry/pods/", start_revision=1000)
  
  for event in watcher:
    event.type:      PUT or DELETE
    event.kv.key:    "/registry/pods/default/nginx-abc123"
    event.kv.value:  serialized Pod spec
    event.kv.mod_revision: 1042

  Key properties:
  - Persistent: watch remains active, no re-registration needed
  - Resumable: if client disconnects, reconnect with last-seen revision
  - Ordered: events arrive in the order they were committed to Raft
  - Multiplexed: many watches share one gRPC stream

  Kubernetes informer pattern:
  1. List all pods (GET /registry/pods/, returns revision=1000)
  2. Cache all pods in memory
  3. Watch from revision 1000 (get all changes since the list)
  4. Apply each watch event to the in-memory cache
  5. Controllers react to cache changes (reconciliation loop)
  
  This is how Kubernetes achieves near-instant reaction to state changes
  across thousands of controllers without polling.
```

### 12.2 Transactions (Mini-Transactions)

etcd supports atomic compare-and-swap operations via mini-transactions:

```
etcd mini-transaction:

  txn = etcd.txn()
    .if(
      key="/leader", compare=CREATE_REVISION, value=0  // key does not exist
    )
    .then(
      put("/leader", "node-1", lease=lease_id)          // acquire leadership
    )
    .else(
      get("/leader")                                    // see who the leader is
    )
  
  result = txn.commit()
  
  This is atomic: either the key did not exist and we created it,
  or it did exist and we read the current leader.
  No race condition, no TOCTOU bug.

  Use cases:
  - Leader election: CAS on leader key
  - Lock acquisition: CAS on lock key with lease
  - Atomic counter: read value, CAS with value+1
  - Configuration update: CAS with expected revision
```

---

## 13. The Redlock Controversy

### 13.1 How Redlock Works

Redlock (proposed by Salvatore Sanfilippo, Redis creator) attempts to build a distributed lock using N independent Redis instances (typically N=5):

```
Redlock algorithm:

  1. Get current time T1
  2. Sequentially try to acquire lock on all 5 Redis instances:
     SET resource unique_value NX PX 30000
  3. Get current time T2
  4. Lock is acquired if:
     a) Lock acquired on >= 3 of 5 instances (majority)
     b) Total time elapsed (T2 - T1) < lock TTL
  5. Effective lock validity = TTL - (T2 - T1) - clock_drift
  6. If lock not acquired: release on all instances

  Release: run DEL (with unique_value check) on all 5 instances
```

### 13.2 Kleppmann's Critique

Martin Kleppmann's "How to do distributed locking" (2016) argued Redlock is fundamentally flawed:

```
Kleppmann's arguments:

  1. PROCESS PAUSES:
     Client acquires Redlock.
     Client has a long GC pause.
     Lock expires on all Redis instances.
     Another client acquires the same lock.
     First client wakes up, thinks it has the lock.
     → Two clients in the critical section simultaneously.
     
     Fix: fencing tokens. But Redis does not issue monotonically
     increasing fencing tokens. Adding them requires consensus,
     at which point you might as well use etcd/ZooKeeper.

  2. CLOCK ASSUMPTIONS:
     Redlock depends on the assumption that time passes at
     roughly the same rate on all nodes.
     
     But: NTP can jump clocks, VMs can have clock skew,
     leap seconds cause issues.
     
     A clock jump on one Redis instance can cause it to
     expire the lock early or keep it too long.

  3. IF YOU NEED CORRECTNESS: use a proper consensus system (etcd, ZooKeeper).
     IF YOU ONLY NEED EFFICIENCY: a single Redis instance is simpler and
     just as good (both fail if Redis crashes; N instances only helps
     if crashes are independent, which clock skew violations break).

Sanfilippo's response:
  - Redlock does not depend on synchronized clocks, only on "roughly correct"
    time passing (bounded clock drift)
  - Process pauses are bounded in practice
  - The algorithm is safe under these assumptions

Practical conclusion:
  - For efficiency locks (prevent duplicate work, best-effort): Redis single instance
  - For correctness locks (prevent data corruption): etcd or ZooKeeper
  - Redlock occupies an awkward middle ground that satisfies neither fully
```

---

## 14. Distributed Locking Patterns for ML/AI Systems

### 14.1 Model Deployment Lock

```
Pattern: ensure only one model version deploys at a time

  Problem: two deployments of different model versions simultaneously
  can leave the cluster in a mixed state (some replicas serving v2,
  some v3).

  Implementation:
    lock_key = "/deployments/recommendation-model"
    
    1. Acquire etcd lock with lease (TTL=300s, renew every 60s)
    2. Run deployment:
       - Upload new model artifacts to S3
       - Update model server config
       - Rolling restart replicas (canary → full rollout)
       - Validate inference accuracy on shadow traffic
    3. Release lock
    
    If deployer crashes: lock expires in 300s, next deployment proceeds.
    Fencing: deployment system checks fencing token against
    model registry before activating a model version.
```

### 14.2 Training Job Coordination

```
Pattern: distributed hyperparameter search with work stealing

  ZooKeeper/etcd structure:
    /hparam-search/job-123/
      /trials/
        /trial-001  (data: {lr: 0.001, batch: 32}, ephemeral)
        /trial-002  (data: {lr: 0.01,  batch: 64}, ephemeral)
      /results/
        /trial-001  (data: {accuracy: 0.92, loss: 0.34})
      /best
        (data: {trial: "trial-001", accuracy: 0.92})

  Worker loop:
    1. List /trials/, find unclaimed trials (no ephemeral owner)
    2. Create ephemeral child under trial node (claim it)
    3. Run training with those hyperparameters
    4. Write results to /results/trial-NNN
    5. CAS update /best if this trial is better
    6. Delete ephemeral claim, pick next trial

  If worker crashes: ephemeral node deleted, trial becomes available
  for another worker to claim.
```

### 14.3 Feature Store Write Lock

```
Pattern: prevent concurrent writes to the same feature group

  Problem: two Spark jobs computing features for the same entity
  can produce inconsistent feature vectors if they run concurrently.

  Implementation:
    lock_key = f"/feature-store/groups/{feature_group}/write-lock"
    
    1. Acquire distributed lock (etcd lease, TTL=600s)
    2. Compute features (Spark job, 5-30 minutes)
    3. Atomic swap of feature table in online store
    4. Release lock
    
    Fencing: online store checks write timestamp.
    If a stale writer tries to swap after lock expiry,
    the store rejects writes older than the current table.
```

### 14.4 Singleton Service Pattern

```
Pattern: ensure exactly one instance of a service runs cluster-wide

  Examples:
  - Kafka controller (exactly one per cluster)
  - Scheduler (one scheduler assigns work to workers)
  - Aggregator (one process writes aggregated metrics)

  etcd implementation:
    campaign_key = "/singletons/metrics-aggregator"
    
    1. All instances create a key with a lease under the campaign prefix
    2. Instance with lowest create_revision becomes the active singleton
    3. Active instance does the work
    4. Other instances watch, ready to take over
    5. If active instance crashes: lease expires, next instance promoted

  This is exactly etcd's Election API:
    election = etcd.election("/singletons/metrics-aggregator")
    election.campaign("instance-1")  // blocks until elected
    // ... do singleton work ...
    election.resign()
```

---

## 15. Failure Modes and Debugging

### 15.1 Raft Failure Scenarios

```
1. LEADER CRASH (most common):
   Leader stops sending heartbeats.
   Followers time out after election_timeout (1-2s in production).
   New election, new leader elected.
   Availability gap: election_timeout + election duration ≈ 1-3 seconds.
   No data loss (committed entries exist on majority).

2. NETWORK PARTITION (split-brain prevention):
   Cluster [A,B,C,D,E] splits into [A,B] and [C,D,E].
   
   If leader was A (in minority partition):
     A cannot commit (only 2 nodes).
     C, D, E elect new leader. System continues.
     When partition heals, A steps down, syncs from new leader.
   
   If leader was C (in majority partition):
     C continues to commit (3 nodes). System continues.
     A, B cannot elect (only 2 nodes). They block.
     When partition heals, A and B sync from C.

3. SLOW FOLLOWER:
   One follower falls behind due to disk I/O, network, or GC.
   Leader maintains nextIndex for each follower.
   If follower falls too far behind, leader sends a snapshot instead
   of individual log entries (InstallSnapshot RPC).
   
   No impact on availability or commit latency (majority is fine).

4. DISK FAILURE ON LEADER:
   Leader loses its WAL/log.
   Leader cannot recover, steps down (or crashes).
   New election. New leader has all committed entries.
   Failed node must rejoin as empty and receive snapshot.

5. BYZANTINE FAILURE (Raft does NOT handle this):
   A node sends incorrect data (corrupted or malicious).
   Raft assumes crash-stop (nodes either work correctly or stop).
   For Byzantine tolerance: use PBFT, HotStuff, or other BFT protocols.
```

### 15.2 Distributed Lock Failure Scenarios

```
1. LOCK HOLDER CRASHES:
   Lease/TTL expires → lock released automatically.
   Unavailability window = remaining TTL when crash occurred.
   Design: set TTL to balance between fast release and false expiry.

2. LOCK SERVICE LEADER FAILOVER:
   etcd leader election: 1-3 seconds of lock unavailability.
   Existing leases survive leader failover (persisted in Raft log).
   New lock acquisitions block until new leader is elected.

3. GC PAUSE DURING CRITICAL SECTION:
   Lock holder enters GC pause → lease expires → another acquires.
   Two processes in critical section simultaneously.
   
   Fix 1: fencing tokens (storage rejects stale tokens)
   Fix 2: keep critical section shorter than half the TTL
   Fix 3: heartbeat-based renewal (detects stale lock faster)

4. NETWORK PARTITION FROM LOCK SERVICE:
   Client holds lock but cannot renew (partitioned from etcd).
   Lease expires → another client acquires lock.
   Partitioned client still thinks it has the lock.
   
   Fix: client must check lock validity before every write.
   Fix: fencing tokens on the storage side.

5. CLOCK SKEW (Redlock-specific):
   NTP step adjustment jumps clock forward on one Redis instance.
   Lock expires early on that instance.
   Majority may no longer hold the lock.
   
   Fix: use consensus-based locks (etcd, ZooKeeper).
```

### 15.3 Debugging Tools

```
etcd:
  etcdctl endpoint status          # leader, raft term, DB size
  etcdctl endpoint health          # health check all endpoints
  etcdctl member list              # cluster membership
  etcdctl lease list               # active leases
  etcdctl lease timetolive <id>    # remaining TTL
  etcdctl get --prefix /locks/     # all active locks
  etcdctl watch --prefix /locks/   # watch lock changes in real time

ZooKeeper:
  echo stat | nc localhost 2181    # server statistics
  echo mntr | nc localhost 2181    # monitoring data
  zkCli.sh ls /locks               # list lock nodes
  zkCli.sh get /locks/my-lock      # read lock data
  
Raft metrics (Prometheus):
  raft_leader_changes_total        # leader elections count
  raft_proposals_committed_total   # committed proposals
  raft_proposals_failed_total      # failed proposals (leader not found)
  etcd_server_leader_changes_seen  # etcd-specific leader changes
  etcd_disk_wal_fsync_duration_seconds  # WAL write latency
```

---

## 16. Interview Patterns

### 16.1 Pattern: "How do you prevent split-brain in your system?"

```
Framework:
  1. Identify the coordination need (leader election, mutual exclusion, config)
  2. Choose coordination service (etcd for new systems, ZK if Kafka/Hadoop ecosystem)
  3. Explain majority quorum: 2N+1 nodes, tolerate N failures
  4. Mention fencing for correctness (not just the lock, but fencing at the data layer)

Example answer:
  "The metadata service runs a 5-node etcd cluster. Leader election uses
   etcd's Election API with a 15-second lease. The leader key includes a
   fencing token (etcd's create_revision). Every write to the data store
   includes this token, and the store rejects writes with stale tokens.
   If the leader's lease expires (network partition or GC pause), a new
   leader is elected within 1-3 seconds, and the old leader's writes
   are rejected by the fencing check."
```

### 16.2 Pattern: Distributed Task Queue

```
"Design a task queue where each task is processed exactly once"

Architecture:
  ┌──────────┐    ┌──────────┐    ┌──────────────┐
  │ Producer │───>│  Task     │───>│  Workers     │
  │          │    │  Queue    │    │  (N replicas) │
  └──────────┘    │  (Kafka/  │    └──────────────┘
                  │   DB)     │          │
                  └──────────┘          │
                       ▲                │
                       │                ▼
                  ┌──────────┐    ┌──────────────┐
                  │   etcd   │    │  Result      │
                  │  (locks) │    │  Store       │
                  └──────────┘    └──────────────┘

  Exactly-once processing:
  1. Worker reads task from queue (Kafka consumer)
  2. Worker acquires etcd lock: /tasks/{task_id}
  3. Worker checks result store: already processed? → skip
  4. Worker processes task
  5. Worker writes result to store WITH fencing token
  6. Worker commits Kafka offset
  7. Worker releases lock

  If worker crashes at step 4: lock expires, another worker retries.
  If worker crashes at step 6: task redelivered, step 3 catches duplicate.
  Idempotency key = task_id. Fencing token = etcd create_revision.
```

### 16.3 Pattern: Configuration Management

```
"How do you push config changes to 1000 service instances?"

  etcd-based:
    1. Store config in etcd: /config/recommendation-service/v42
    2. All 1000 instances watch /config/recommendation-service/ prefix
    3. Operator updates config via etcd txn (atomic CAS on revision)
    4. All watchers receive the event within ~100ms
    5. Each instance validates new config locally before applying
    6. Instances report health check with new config version
    7. Operator monitors: GET /health?config_version=42 on all instances

  vs. polling-based (inferior):
    - 1000 instances × 1 poll/second = 1000 QPS on config service
    - Average delay: half the poll interval (5s average for 10s interval)
    - etcd watch: 0 QPS steady-state, ~100ms propagation
```

### 16.4 Pattern: Distributed Rate Limiter

```
"Design a rate limiter that works across multiple API gateway instances"

  Two approaches:

  1. CENTRALIZED (Redis):
     All gateway instances check/increment a Redis counter.
     Lua script: MULTI/EXEC atomic increment + expiry.
     Problem: Redis is a SPOF. If Redis fails, no rate limiting.
     Problem: network latency to Redis on every request (1-2ms).

  2. LOCAL + COORDINATION (preferred for low latency):
     Each gateway gets a quota allocation from etcd:
       Total limit: 10,000 req/s across 5 gateways
       Each gateway: 2,000 req/s local budget
     
     Periodic rebalancing (every 5-10s):
       Gateway reports actual usage to etcd
       Coordinator redistributes budget based on actual demand
       Gateway with 500 req/s gives budget to gateway with 3000 req/s
     
     Lock: etcd lock during rebalancing to prevent double-allocation
     Fencing: each allocation includes a revision number
     
     Tradeoff: ~10% accuracy loss vs 0ms additional latency
```

### 16.5 Quick-Reference: When to Use What

| Need | Solution | Why |
|---|---|---|
| Leader election (general) | etcd Election API | Raft-backed, simple API, automatic failover |
| Distributed lock (correctness) | etcd lease + fencing token | Linearizable, fencing prevents stale writes |
| Distributed lock (efficiency) | Redis SETNX + TTL | Simple, fast, acceptable if rare double-processing is OK |
| Service discovery | etcd (watches) or Consul | Real-time notifications, health checking |
| Configuration management | etcd (watch + txn) | Atomic updates, instant propagation |
| Work queue coordination | etcd lock + Kafka | Lock for claiming, Kafka for durability and ordering |
| Kafka controller election | ZooKeeper (legacy) / KRaft (new) | Kafka's native integration |
| Kubernetes control plane | etcd (3 or 5 nodes) | All K8s state lives here |
| Singleton service | etcd Election or K8s leader-for-life | One active instance cluster-wide |

---

## Cross-References

### Within `distributed-systems/`
- `07-kafka-and-event-streaming.md`: Kafka controller election, consumer group coordination
- `10-sharding-and-consistent-hashing.md`: Partition assignment, coordinator routing
- `17-networking-protocols-and-communication.md`: gRPC for Raft RPCs, TCP keep-alive for failure detection
- `22-stream-processing-flink-watermarks-eos.md`: Flink JobManager HA via ZooKeeper
- `29-failure-detection-phi-accrual.md`: Heartbeat tuning, failure detector accuracy
- `33-resilience-patterns-circuit-breakers.md`: Circuit breakers around lock acquisition
- `35-reliability-math-slos-and-error-budgets.md`: Availability math for consensus clusters

### Within `databases/`
- `../databases/16-failure-detection-and-leader-election.md`: Leader election algorithms, fencing tokens, split-brain prevention
- `../databases/12-replication-and-distributed-storage.md`: Replication protocols, quorum writes
- `../databases/19-distributed-databases-deep-dive.md`: CockroachDB Raft ranges, Spanner Paxos groups
- `../databases/05-transactions-and-concurrency.md`: 2PC, distributed transactions built on consensus
- `../databases/14-write-ahead-log-internals.md`: WAL mechanics underlying Raft log persistence

### Within `sre-observability/`
- `../sre-observability/13-slo-engineering.md`: SLO for coordination service availability
