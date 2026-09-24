# Consensus (Raft) and Distributed Locking: A Complete Interview-Ready Deep Dive

A production-grade reference covering the Raft consensus algorithm end-to-end (leader election, log replication, safety, membership changes, read optimizations), distributed locking primitives (leases, fencing tokens, lock services), and the coordination systems built on top of them (etcd, ZooKeeper, Consul). Every section ties back to how these primitives appear in system design interviews -- from "how does your metadata store stay consistent?" to "how do you prevent two workers from processing the same job?"

Prerequisites: familiarity with distributed system models from the `README.md` roadmap and failure detection from `29-failure-detection-phi-accrual.md`. For leader election algorithms at a higher level, see `../databases/16-failure-detection-and-leader-election.md`.

---

## Table of Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
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
17. [Real-world cases — incidents with numbers](#17-real-world-cases--incidents-with-numbers)

---

## Start here — the whole chapter in plain words

**The problem.** Many systems need several machines to agree on one thing: who the leader is, who
holds a lock, what the current config is, what order writes happened in. Machines crash, networks
split, and a process can freeze for seconds without knowing it. If two machines both believe "I am
in charge", they both act, and you get double payments, lost writes, or corrupted data. This chapter
covers Raft (the standard way a small group of servers agrees), and locks built on top of it, and
how they still fail if you use them carelessly.

**A real-world example.** A payments company runs a nightly "send payouts" job. Three worker
machines can run it, but only one may run it at a time, or sellers get paid twice.

- **No coordination.** Each worker checks a flag in a database row, sees "nobody is running", and
  starts. Two workers start within the same 50 ms. 8,000 sellers are paid twice.
- **One lock server (a single Redis).** The workers take a lock with a 15 s expiry. It works until
  the Redis machine dies at 02:00; now no one can take the lock, or (after a failover that lost the
  last write) two workers get the "same" lock.
- **A Raft cluster (etcd, 5 servers).** The lock lives in a store that copies every change to a
  majority (3 of 5) before saying "done". Two servers can die and the lock still works. If the
  leader server dies, the other 4 notice after about 1 s (the *election timeout*) and elect a new
  leader; the lock record is not lost.
- **Still broken: the frozen worker.** Worker A holds the lock, then freezes for 20 s (a garbage
  collection pause). Its 15 s lock expires; worker B takes it. A wakes up and keeps paying. The lock
  service did its job; A just doesn't know it lost the lock.
- **Fencing tokens fix it.** Every time the lock is granted, the lock service hands out a bigger
  number: A got 41, B got 42. The payouts database remembers the biggest number it has seen and
  rejects any write carrying a smaller one. A's late write with 41 is refused. No double payment.

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Consensus | a group of servers agreeing on the same list of changes, in the same order | a committee that only acts on motions a majority voted for |
| Majority / quorum | more than half the servers: 2 of 3, 3 of 5 | "need 3 of 5 board members to sign" |
| Leader | the one server that accepts writes and tells the others what to copy | the meeting chair who writes the minutes |
| Follower / candidate | a server that copies the leader / a server asking to become leader | committee members / a member running for chair |
| Term | an election round number; higher always wins | the "2026 board" overrides anything the "2025 board" says |
| Log / log entry | the ordered list of changes every server keeps | numbered lines in the meeting minutes |
| Committed | stored on a majority, so it can never be lost | minutes signed by most members |
| Election timeout | how long a follower waits without hearing from the leader before calling an election | "if the chair hasn't spoken for 1 minute, someone else takes over" |
| Heartbeat | small "I'm still here" message from the leader | the chair tapping the microphone every few seconds |
| Split brain | two servers both acting as leader | two people both think they are driving the car |
| Lease | a lock that expires by itself unless renewed | a parking ticket valid for 2 hours |
| Fencing token | an ever-growing number handed out with each lock grant; storage rejects older numbers | numbered tickets at a deli counter: number 41 can't be served after 42 |
| Learner | a new server that copies data but doesn't vote yet | a new hire who shadows before getting a vote |
| ReadIndex / lease read | ways for the leader to answer reads safely without writing a log entry | the chair checking "am I still chair?" before answering |
| Ephemeral node (ZooKeeper) / lease-attached key (etcd) | a record that disappears when its owner disconnects | a "seat taken" jacket that leaves with its owner |
| Watch | subscribe and be told when a key changes | a doorbell instead of checking the door every minute |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| `N` (cluster size) | number of voting servers | 3 or 5 (etcd advises against more than 7) | Kubernetes etcd with 3 servers |
| majority `⌊N/2⌋ + 1` | votes / copies needed to elect or commit | 2 of 3, 3 of 5 | a write is done once 3 of 5 have it |
| `f`, "2f+1 nodes" | failures tolerated; `2f+1` servers tolerate `f` crashes (§16 writes this as 2N+1) | f = 1 or 2 | 5 servers survive 2 crashes; 4 servers survive only 1 |
| term | election round number, only goes up | 1, 2, 3 … | node sees term 8 while it is on 7 → steps down |
| index | position of an entry in the log | 1, 2, 3 … | entry 7 = "y=9" |
| `lastLogIndex`, `lastLogTerm` | index and term of a candidate's last entry, sent with a vote request | — | (47, 2): "my log ends at 47, written in term 2" |
| `prevLogIndex`, `prevLogTerm` | the entry just before new entries; follower must have it to accept | — | "do you have entry 5 from term 3?" |
| `commitIndex` / `leaderCommit` | highest entry known to be committed / the leader's value sent to followers | — | commitIndex = 6 → entries 1–6 are safe |
| `nextIndex` | per follower: next entry the leader will send | — | follower lags → nextIndex = 6 |
| `readIndex` | commitIndex recorded at the moment a read arrives | — | read waits until entries up to 47 are applied |
| `heartbeat-interval` | how often the leader pings followers | etcd default 100 ms | 10 pings per second |
| `election-timeout` | silence before a follower starts an election | Raft paper 150–300 ms; etcd default 1000 ms | leader dies → new election ~1 s later |
| `broadcastTime` | time for one round of RPCs to all followers | 0.5–20 ms | same-datacenter round trip ≈ 1 ms |
| MTBF | mean time between server failures | months | one server fails every ~6 months |
| clock drift bound | how much faster one clock may run than another | 1.001 (0.1%) | 1000 ms lease → trust only 999 ms |
| `lease_duration` | time a leader may serve reads without checking | `election_timeout / drift_bound` | 1000 / 1.001 ≈ 999 ms |
| TTL | lifetime of a lease or lock unless renewed | 10–30 s, renew at TTL/3 to TTL/2 | TTL 15 s, renew every 5 s |
| fencing token | increasing number per lock grant | etcd `create_revision`, ZooKeeper sequence number | A = 33, B = 34 → 33 rejected |
| `create_revision` / `mod_revision` | etcd's global counter value when a key was created / last changed | grows with every write | lock key created at revision 1042 |
| `NX`, `PX 30000` | Redis: set only if missing; expire after 30,000 ms | — | `SET lock id NX PX 30000` |
| `T1`, `T2`, clock_drift | Redlock: start / end time of acquiring on all instances; allowance for clock error | — | TTL 30 s, took 0.2 s → valid ≈ 29.8 s minus drift |
| `C_old`, `C_new`, `C_old,new` | old, new, and joint cluster configurations | — | 3 servers → 5 servers |
| RTT | network round-trip time | 0.5 ms in a datacenter, 20–80 ms across regions | ReadIndex costs 1 RTT |
| fsync latency | time to force a write to disk | < 10 ms p99 recommended for etcd WAL | slow cloud disk: 200 ms → elections |
| p99 | 99% of operations are faster than this | — | fsync p99 = 5 ms |
| etcd DB size | total stored data | 2 GB default quota, 8 GB suggested max | — |
| range / region size | chunk of keys with its own Raft group (CockroachDB / TiKV) | 512 MB (CockroachDB); TiKV smaller, version-dependent | 1,000 ranges → 1,000 Raft groups |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. Why Consensus Exists

> **In plain words.** Several servers must agree on one answer even when some crash or the network splits. Without a rule like "only a majority can decide", two halves of a split cluster both keep working and later disagree.
>
> **Real-world example.** A bank ledger runs on 5 servers. A switch failure splits them 2 and 3. With a majority rule, only the side with 3 keeps accepting transfers; the side with 2 refuses, so no account is debited twice.

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

Fischer, Lynch, and Paterson (1985) proved that no deterministic consensus algorithm can guarantee termination in a fully asynchronous system with even one crash failure. Practical consensus algorithms such as Raft, Multi-Paxos, and Zab work around FLP by relying on timeouts (partial synchrony): they are always safe (never disagree), but they only make progress when the network eventually delivers messages within some bound, even if that bound is unknown. (Randomization is the other known way around FLP.)

### 1.3 Consensus vs. Coordination

| Concept | What it solves | Example |
|---|---|---|
| Consensus | Agreement on a sequence of values (replicated log) | Raft, Paxos, Zab |
| Leader election | Choosing one leader from a set of candidates | Built on consensus |
| Distributed locking | Mutual exclusion across processes/nodes | Built on consensus or leases |
| Service discovery | Agreeing on which services are alive and where | Built on consensus + health checks |
| Configuration management | Agreeing on the current system configuration | Built on consensus |

All of these are built on top of consensus. That's why etcd (Raft-based) and ZooKeeper (Zab-based) have served as the coordination layer for Kubernetes, Kafka (before KRaft), and many other distributed systems.

---

## 2. Raft Fundamentals

> **In plain words.** Raft splits the job into three parts: pick one leader, have the leader copy every change to the others, and make sure a change that was confirmed is never lost. Every server is a follower, a candidate, or the leader, and an ever-growing *term* number tells everyone which leader is the current one.
>
> **Real-world example.** A ride-hailing dispatch service keeps driver assignments in a 3-server Raft store. Server A is leader in term 4. If A dies and B wins term 5, any late message from A still stamped "term 4" is ignored.

### 2.1 Design Goal: Understandability

Raft was designed by Diego Ongaro and John Ousterhout ("In Search of an Understandable Consensus Algorithm", USENIX ATC 2014) as an alternative to Paxos that is easier to understand, implement, and reason about. The key design decision: decompose consensus into three independent subproblems:

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

> **In plain words.** If followers stop hearing the leader's heartbeat, one of them waits a random short time, then asks the others for votes. It wins with a majority, but only voters whose own log is not newer than its log will vote for it. Random waits stop everyone from running at once and splitting the vote.
>
> **Real-world example.** etcd defaults: leader heartbeat every 100 ms, election timeout 1000 ms. The leader of a 5-server cluster crashes; about 1 s later one follower starts an election, gets 3 votes (itself + 2) within a few ms, and writes resume after roughly 1–2 s in total.

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

A partition-isolated node keeps incrementing its term and calling elections. When the partition heals, it has a very high term number that forces the entire cluster to step down momentarily (disrupting the healthy leader). The Pre-Vote extension (described in Ongaro's 2014 PhD dissertation and implemented in etcd's Raft library, usually together with CheckQuorum) prevents this:

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
  
  Nodes refuse if they heard from a live leader within the last election
  timeout (or if the asker's log is behind). A minority side can never
  collect a majority of pre-votes.
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
  - Disk fsync stalls (and GC pauses in JVM-based systems like ZooKeeper)
    can exceed 200ms
  - Too-aggressive timeout → unnecessary elections → instability
  - etcd's tuning guide: heartbeat ≈ 0.5-1.5 × average RTT between members,
    election timeout at least 10 × RTT; the defaults (100ms / 1000ms)
    keep a 10× ratio between them
```

---

## 4. Raft Log Replication

> **In plain words.** The leader numbers each change and sends it to every follower along with "the entry just before this one". A follower only accepts if it has that previous entry, so logs can never silently diverge. Once a majority has stored an entry from the leader's current term, it is committed and can be applied.
>
> **Real-world example.** An e-commerce checkout writes "order 881 = paid" to a 5-server cluster. The leader and 2 followers store it within 3 ms; that is 3 of 5, so the client gets "OK". The 2 slow followers catch up on the next heartbeat.

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
       Leader decrements nextIndex for this follower (so prevLogIndex
       moves back by one) and retries (log repair)
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

1. Client sends command "y=9" to Leader
2. Leader appends to its log: index=7, term=3, cmd="y=9" (as in §4.1)
3. Leader sends AppendEntries to all followers in parallel

   Node1 (leader):  stored at index 7  ✓
   Node2:           receives, stores   ✓  (2/5 = not yet committed)
   Node3:           receives, stores   ✓  (3/5 = COMMITTED!)
   Node4:           slow, not yet      ✗
   Node5:           slow, not yet      ✗

4. Leader advances commitIndex to 7 (majority have it, and it is
   from the leader's current term)
5. Leader applies "y=9" to state machine
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

> **In plain words.** Raft promises that a confirmed change is never lost or replaced, even across leader changes. The trick: a server only votes for a candidate whose log is at least as up to date as its own, and any two majorities share at least one server, so every new leader already has every confirmed change.
>
> **Real-world example.** In a 5-server cluster, a transfer is stored on servers 1, 2, 3. To win, a new leader needs 3 votes, so at least one of 1, 2, 3 must vote, and that server refuses anyone missing the transfer.

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
  Term 2: Node1 crashes. Node5 becomes leader (did not have index 2),
          accepts a client entry at index 2 in term 2, then crashes
          before replicating it
  Term 3: Node1 recovers, becomes leader again
  
  Can Node1 now commit its term-1 entry at index 2 by replicating it
  to Node3?
  
  NO! Even though 3/5 nodes now have it, Node5 can still win a later
  election: its last log term (2) beats their last log term (1), so
  Node2/3/4 would vote for it. Node5 would then overwrite index 2
  with its own term-2 entry. (This is Figure 8 of the Raft paper.)
  
  SAFE rule: Leader only commits entries from its current term.
  When a current-term entry is committed, all previous entries
  are implicitly committed (they precede it in the log).

  Practical impact: after election, leader appends a no-op entry
  in the new term and replicates it. Once the no-op commits,
  all previous entries are safely committed.
```

---

## 6. Raft Membership Changes

> **In plain words.** Adding or removing servers is risky because for a moment some servers use the old member list and some the new one, and each group might elect its own leader. Raft avoids this either by changing one server at a time or by a two-step "joint" phase where decisions need a majority of both lists.
>
> **Real-world example.** Growing a chat app's metadata cluster from 3 to 5 servers: add server D as a non-voting learner, wait until it has copied all 2 GB, promote it (now 4 voters), then repeat for E.

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

A simpler alternative, described in Ongaro's dissertation and used by etcd's member add/remove API: change one node at a time. (etcd's Raft library also supports joint consensus.) Adding or removing a single node from any majority-based cluster is safe because the old and new majorities always overlap:

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

  Caveat (found after publication, fixed in etcd and other libraries):
  a new leader must commit an entry in its own term before it starts
  a configuration change, and only one change may be in progress at a time.
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

> **In plain words.** Reading from the leader is only safe if it is still the leader; a leader cut off by a partition might return old data. Running each read through the log is safe but as slow as a write. ReadIndex checks leadership with one heartbeat round; lease reads skip even that by trusting clocks.
>
> **Real-world example.** A config service handles 50,000 reads/s. Logging each read would need a disk sync per read; ReadIndex costs one ~0.5 ms network round trip, shared by all reads that arrive during that round, and needs no disk write.

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
  
  0. (Once per term) leader must have committed an entry in its current
     term, e.g. the no-op from §5.3, so its commitIndex is up to date
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
  Leader records start = now() BEFORE sending a heartbeat round.
  After a majority of followers respond, the lease runs from that start
  (not from when the replies arrive):
    lease_start = start
    lease_duration = election_timeout / clock_drift_bound
                   = 1000ms / 1.001 ≈ 999ms
  
  During the lease period, no other node can become leader: followers
  that heard from the leader recently refuse to vote (this needs
  CheckQuorum / leader stickiness to hold).
  
  Leader can serve reads directly from local state machine:
    if now() < lease_start + lease_duration:
      return local_state_machine.get(key)
    else:
      fall back to ReadIndex

  Cost: 0 RTTs, 0 disk I/O
  
  Risk: depends on clock accuracy.
  If the leader's clock runs fast, it might think the lease is valid
  when followers have already timed out and elected a new leader.
  
  CockroachDB uses a related idea (range leases held by a leaseholder)
  that depends on a configured maximum clock offset between nodes.
  etcd uses ReadIndex by default (safer, 1 RTT overhead); its Raft
  library also offers a lease-based read option.
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
  TiKV's Follower Read works this way. CockroachDB's follower reads are
  different: they return slightly stale data at a "closed timestamp"
  without contacting the leaseholder.
```

---

## 8. Raft in Production: etcd, CockroachDB, TiKV

> **In plain words.** etcd runs one Raft group for a small, critical dataset (Kubernetes' state). Databases like CockroachDB and TiKV cut the data into many small ranges and run a separate Raft group for each, so leadership and load are spread across machines.
>
> **Real-world example.** A 10-node CockroachDB cluster holding 5 TB in ranges of at most 512 MB has at least 10,000 ranges; with 3 copies each, every node takes part in at least 3,000 Raft groups.

### 8.1 etcd

etcd is one of the most widely deployed Raft implementations. It is the metadata store for Kubernetes (every pod, service, configmap, secret is an etcd key).

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
  - Default storage quota 2 GB; suggested maximum 8 GB
  - Not designed for high-throughput data storage — it's a coordination service
```

**etcd releases that change operations (2025–2026).**

| Release | What changed | What to do |
|---|---|---|
| **v3.6.0** (2025-05-15), the first minor release since v3.5.0 in June 2021 | Average memory **down at least 50%**, mainly because the default `--snapshot-count` fell from 100,000 to 10,000 (it keeps about 10% of the history in memory). About **10% higher** read and write throughput. **Full downgrade support**. Cluster membership moved to the v3 store and the v2 API flags were removed. etcd also became a Kubernetes SIG | Upgrade to **v3.5.20 or later before** going to v3.6. Move any leftover v2 API clients to v3 first |
| **v3.7.0** (2026-07-08) | **RangeStream**: large range reads (big Kubernetes `LIST`s) come back in chunks instead of one buffered response, so memory is predictable. The server now boots **entirely from the v3 store**, with no v2 remnants. **`LeaseRevoke` is prioritized under overload** and a faster lease keep-alive path was added, so leases expire on time when the cluster is busy | Check that backup and restore tooling is current (snapshot handling changed with the v2 store's removal). Lock and election TTLs (§9.3) now hold more reliably during overload |

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
  │  Region 2   │     │  Region 2*  │     │  Region 2   │
  │  Region 3   │     │  Region 3   │     │  Region 3*  │
  │             │     │             │     │             │
  │  RocksDB    │     │  RocksDB    │     │  RocksDB    │
  │  (Raft log) │     │  (Raft log) │     │  (Raft log) │
  │  RocksDB    │     │  RocksDB    │     │  RocksDB    │
  │  (State)    │     │  (State)    │     │  (State)    │
  └─────────────┘     └─────────────┘     └─────────────┘
  
  Historically two RocksDB instances per node (newer TiKV versions
  default to a purpose-built "Raft Engine" for the log instead):
    1. Raft log engine (sequential writes, periodic compaction)
    2. State machine engine (actual key-value data)
  * = Raft leader of that region. Each region has 3 replicas.
  
  PD (Placement Driver): central coordinator that tracks region locations,
  triggers splits/merges/rebalancing. CockroachDB has no such central
  component; it spreads this information via gossip and meta ranges.
```

---

## 9. Distributed Locking Fundamentals

> **In plain words.** A distributed lock is "only one worker may do this at a time". The hard part: a worker can freeze or lose the network and not know its lock has expired. A lock alone can't stop that stale worker; the storage it writes to must reject it, using a fencing token.
>
> **Real-world example.** A video platform's transcoding job holds a 10 s lock. The worker freezes for 15 s; another worker takes the lock (token 34) and starts. The first worker wakes and writes with token 33; the storage sees 33 < 34 and refuses.

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
  Time 20s: lease expires (10s after the last renewal at 10s),
            lock released automatically
  
  Key property: no manual "unlock" required.
  Even if the lock holder crashes permanently, the lock
  is eventually released. A crashed holder cannot block others forever.

  Trade-off: TTL must balance between:
    - Too short (1s): healthy clients lose the lock during transient issues
    - Too long (60s): unhealthy client blocks others for 60 seconds
    - Typical production: 10-30 seconds with renewal at half the TTL
```

### 9.4 Where the Fence Lives: Compare-and-Swap on Storage You Already Have

§9.2 assumes the storage checks a fencing token. Most don't do that out of the box, but almost
every store now offers **compare-and-swap (CAS)**, which does the same job:

| Store | CAS primitive | Fenced write |
|---|---|---|
| Postgres / MySQL | `UPDATE ... WHERE` a version or token column | `UPDATE jobs SET state = $1, fence = $2 WHERE id = $3 AND fence <= $2`. Zero rows updated means a newer holder exists |
| DynamoDB | `ConditionExpression` | `attribute_not_exists(pk) OR fence <= :t` |
| etcd | `Txn` with `Compare` | Compare the lock key's `create_revision` (or that it still exists) in the same transaction as the write. This is the mitigation Jepsen recommended for etcd locks (§17.7) |
| S3, GCS, Azure Blob | Conditional `PUT` on the object's ETag / generation | S3 added `If-None-Match: *` (create only if absent) in **August 2024** and `If-Match: <etag>` (replace only if unchanged) in **November 2024**. GCS and Azure Blob have long had equivalents |

S3's addition matters most because many systems keep their state *only* in object storage: table
formats (Delta, Iceberg), object-storage-native stores and queues. They can now elect a leader
and commit atomically **without running etcd or ZooKeeper at all**.

A lease on any CAS store takes about 30 lines. The epoch it returns is the fencing token:

```python
import json
from dataclasses import dataclass
from typing import Protocol

from botocore.exceptions import ClientError


class CasStore(Protocol):
    """Any storage with compare-and-swap: S3/GCS/Azure Blob (ETags), DynamoDB
    (ConditionExpression), Postgres (UPDATE ... WHERE version = $n), etcd (Txn)."""
    def get(self, key: str) -> tuple[bytes, str] | None: ...                 # (body, etag)
    def put(self, key: str, body: bytes, if_match: str | None) -> bool: ...  # None = must not exist


@dataclass(frozen=True)
class Lease:
    holder: str
    epoch: int            # the fencing token: +1 every time leadership changes hands
    expires_at: float


class LeaderLease:
    def __init__(self, store: CasStore, key: str, me: str, ttl_s: float = 15.0,
                 skew_margin_s: float = 2.0):
        self.store, self.key, self.me = store, key, me
        self.ttl, self.margin = ttl_s, skew_margin_s

    def try_lead(self, now: float) -> int | None:
        """Acquire or renew. Returns the epoch to stamp on every write, or None."""
        cur = self.store.get(self.key)
        if cur is None:
            nxt, etag = Lease(self.me, 1, now + self.ttl), None
        else:
            lease, etag = Lease(**json.loads(cur[0])), cur[1]
            if lease.holder == self.me:
                nxt = Lease(self.me, lease.epoch, now + self.ttl)            # renew
            elif now > lease.expires_at + self.margin:                     # margin: clock skew
                nxt = Lease(self.me, lease.epoch + 1, now + self.ttl)        # take over
            else:
                return None
        ok = self.store.put(self.key, json.dumps(nxt.__dict__).encode(), if_match=etag)
        return nxt.epoch if ok else None


class S3CasStore:
    """S3 conditional writes (If-None-Match since Aug 2024, If-Match since Nov 2024)."""
    def __init__(self, s3, bucket: str):
        self.s3, self.bucket = s3, bucket

    def get(self, key):
        try:
            r = self.s3.get_object(Bucket=self.bucket, Key=key)
        except self.s3.exceptions.NoSuchKey:
            return None
        return r["Body"].read(), r["ETag"]

    def put(self, key, body, if_match):
        cond = {"IfMatch": if_match} if if_match else {"IfNoneMatch": "*"}
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, **cond)
            return True
        except ClientError as e:
            # 412: someone else won. 409: a concurrent conditional write, re-read and retry.
            if e.response["ResponseMetadata"]["HTTPStatusCode"] in (409, 412):
                return False
            raise
```

Walk through §9.1's pause scenario with a 15 s TTL. Worker A leads with epoch 1 and renews at
t = 5 s (lease now ends at 20 s). A freezes. At t = 23 s, past the end plus the 2 s skew margin,
B takes over with **epoch 2**. When A wakes at t = 24 s, its renewal fails because the ETag
changed. Tested against an S3 mock (moto): two writers with the same ETag, exactly one wins.

Three rules keep it correct:

1. **The lease picks who *tries*. The CAS on the data decides who *succeeds*.** A's work in
   progress is still dangerous until its *writes* are fenced. Either stamp the epoch on every
   write and check it (the Postgres row above), or make the commit itself a CAS on the object
   holding the state (a manifest or metadata pointer). A stale leader's commit then fails on the
   ETag. This is how lakehouse table formats commit safely.
2. **Expiry is judged on the challenger's clock,** so leave a margin larger than your worst clock
   skew. Have the leader stop taking new work before `expires_at - margin`. The fence is what makes
   a wrong clock merely slow rather than unsafe.
3. **Price and latency.** Each renewal is a PUT: tens of milliseconds, and $0.005 per 1,000 on
   S3 Standard. Renewing every 5 s is 17,280 PUTs a day, about $2.60 a month per lease (the GETs
   add about $0.20). Fine for seconds-scale leadership (a compactor, a scheduler, a singleton
   job). Too slow for per-request locks, which belong in etcd or the database.

**Kubernetes leader election is a lease, not a fence.** Controllers use `coordination.k8s.io`
`Lease` objects through client-go's `leaderelection` package, whose documentation states that it
**does not guarantee only one client is acting as leader** (no fencing). The mechanism is the
same as above: CAS on the object's `resourceVersion`, with expiry judged from timestamps. For an
operator whose actions must not overlap (a database failover, a payment batch), fence the
*effects*: conditional writes on the resources it changes, or idempotency keys.

---

## 10. Lock Service Implementations

> **In plain words.** Three common ways to build a lock: etcd (a key tied to a lease, queue ordered by revision), ZooKeeper (ephemeral sequential nodes that vanish when the client disappears), and Redis (`SET ... NX PX`, simple and fast but weaker guarantees).
>
> **Real-world example.** An IoT firmware-rollout job uses an etcd lock with a 15 s lease renewed every 5 s. If the rollout machine dies, the lock frees itself within at most 15 s and a standby takes over.

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
  If Redis crashes, the lock is lost. Adding a replica does not fully
  fix this: replication is asynchronous, so a failover can promote a
  replica that never saw the lock, and a second client can acquire it.
```

---

## 11. ZooKeeper Coordination Primitives

> **In plain words.** ZooKeeper stores small records in a folder-like tree. Records can vanish automatically when their owner disconnects (ephemeral) and can get automatic increasing numbers (sequential). From those two features you build leader election, locks, service discovery, and config updates.
>
> **Real-world example.** A payments service has 12 instances; each creates `/services/payment/instance-NNN` as an ephemeral node. When one crashes, its node disappears once its session times out, and the load balancer's watch fires, removing it from rotation.

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
  (ZooKeeper 3.5+/3.6+ also adds Container and TTL node types)
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
| Consensus | Zab (atomic broadcast; Paxos-like) | Raft |
| Language | Java | Go |
| Data model | Hierarchical (tree) | Flat key-value with prefix ranges |
| Watch mechanism | One-time watches (must re-register); persistent and recursive watches since 3.6 | Long-lived watch streams |
| Session model | Client sessions with heartbeats | Leases with TTL |
| Ephemeral nodes | Yes (auto-delete on session expire) | Via lease attachment |
| Sequential nodes | Native | Must implement with revision numbers |
| Max data per node | ~1 MB (default `jute.maxbuffer`) | ~1.5 MiB per request by default (`--max-request-bytes`); 2 GB default DB quota, 8 GB suggested max |
| Linearizable reads | No by default: any server answers reads from its local copy, which may lag; call `sync()` first for an up-to-date read. Writes are linearizable | Yes by default (ReadIndex); serializable local reads optional |
| MVCC | No (current state only) | Yes (revision history, compact-able) |
| Used by | Kafka up to 3.9 (4.0 removed it), HBase, Hadoop, Solr | Kubernetes, CoreDNS, Vitess (CockroachDB and TiKV reuse etcd's Raft library, not etcd itself) |
| Operational complexity | High (JVM tuning, GC pauses) | Lower (single binary, Go runtime) |

**ZooKeeper after Kafka 4.0.** Apache Kafka 4.0 (2025-03-18) **removed ZooKeeper entirely**.
KRaft, Kafka's own Raft-based metadata quorum, is the only mode. A ZooKeeper-based cluster must
first migrate on the 3.9 bridge release. Kafka was ZooKeeper's largest user, and ClickHouse had
already moved to ClickHouse Keeper (Raft, ZooKeeper-compatible protocol). The pattern is clear:
systems now **embed Raft** rather than depend on an external coordinator. For a new system:
use the coordination built into the platform you already run (Kubernetes → etcd-backed `Lease`
objects, §9.4; your database → advisory locks and conditional writes). Run etcd yourself only when
you need its watch and transaction API. Choose ZooKeeper only for software that still requires it
(HBase, Solr, older Hadoop stacks).

---

## 12. etcd Coordination Primitives

> **In plain words.** etcd gives you two main tools: watches (a live stream of changes to keys, which can resume after a disconnect) and small transactions ("if this key is unchanged, then write X, else read Y"), which make compare-and-swap operations safe.
>
> **Real-world example.** Kubernetes' API server watches etcd; when a pod record changes at revision 1042, every interested controller hears about it within milliseconds instead of polling.

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
    (unless that revision was already compacted; then re-list)
  - Ordered: events arrive in the order they were committed to Raft
  - Multiplexed: many watches share one gRPC stream

  Kubernetes informer pattern (the kube-apiserver watches etcd;
  controllers list+watch the apiserver, whose resourceVersion is the
  etcd revision):
  1. List all pods (returns revision=1000)
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
  - Atomic counter: read value and mod_revision, write value+1 only if
    mod_revision is unchanged
  - Configuration update: CAS with expected revision
```

---

## 13. The Redlock Controversy

> **In plain words.** Redlock tries to make a safer Redis lock by taking it on a majority of 5 independent Redis servers. Critics showed it still breaks if a client freezes or a clock jumps, and it can't hand out fencing tokens. Use it at most to avoid duplicate work, not to protect data.
>
> **Real-world example.** Lock TTL 30 s; acquiring on 3 of 5 instances took 200 ms, so the client may trust the lock for about 29.8 s minus a clock-drift allowance. If one Redis server's clock jumps 30 s forward, its copy expires early and another client can grab 3 of 5.

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
     just as good: an occasional double run is acceptable for efficiency,
     so running 5 Redis masters buys little.

Sanfilippo's response:
  - Redlock does not depend on synchronized clocks, only on "roughly correct"
    time passing (bounded clock drift); admins should avoid clock steps
  - A pause AFTER the lock is acquired hurts every lease-based lock,
    not just Redlock; the time check in step 4 covers pauses during
    acquisition
  - If the storage can check a fencing token, it can equally check the
    lock's unique random value with a compare-and-set
  - The algorithm is safe under these assumptions

Practical conclusion:
  - For efficiency locks (prevent duplicate work, best-effort): Redis single instance
  - For correctness locks (prevent data corruption): etcd or ZooKeeper
  - Redlock occupies an awkward middle ground that satisfies neither fully
```

---

## 14. Distributed Locking Patterns for ML/AI Systems

> **In plain words.** ML platforms use the same locks: one model rollout at a time, one worker per training trial, one writer per feature table, one active scheduler. Each still needs a lease (so crashes free the lock) and a fencing check where data is written.
>
> **Real-world example.** A recommendation model deploy holds a 300 s lease renewed every 60 s. The deployer crashes mid-rollout; within 300 s the lease expires and the next deploy starts, and the model registry rejects the dead deployer's older token.

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
    (Wall-clock timestamps are a weak fence because clocks skew;
    prefer the lock's etcd revision as the token.)
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

Election gives you a single leader *most of the time*. If two overlapping singletons would do damage, fence the singleton's writes with its election revision (§9.4). In Kubernetes, the built-in `Lease`-based leader election is explicitly unfenced.

---

## 15. Failure Modes and Debugging

> **In plain words.** Most Raft failures cost a short pause (about one election timeout), not data loss. Most lock failures come from the holder losing the lock without knowing. The etcd and ZooKeeper tools below show who is leader, how many elections happened, and how slow the disk is.
>
> **Real-world example.** An on-call engineer sees 30 leader changes in an hour on a 3-node etcd. `etcd_disk_wal_fsync_duration_seconds` p99 is 400 ms (target: under 10 ms). The disk, not the network, is the cause.

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
          (reduces the chance, does not remove it)
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
                                   # (3.5+: four-letter words must be
                                   #  allowed in 4lw.commands.whitelist)
  zkCli.sh ls /locks               # list lock nodes
  zkCli.sh get /locks/my-lock      # read lock data
  
etcd Raft metrics (Prometheus):
  etcd_server_has_leader                   # 1 if this member sees a leader
  etcd_server_leader_changes_seen_total    # leader changes seen
  etcd_server_proposals_committed_total    # committed proposals
  etcd_server_proposals_failed_total       # failed proposals
  etcd_disk_wal_fsync_duration_seconds     # WAL fsync latency (p99 < 10ms)
  etcd_disk_backend_commit_duration_seconds  # backend commit latency
```

---

## 16. Interview Patterns

> **In plain words.** Interviewers want three things: you know a majority quorum prevents split brain, you know a lock can be lost without the holder noticing, and you add fencing or idempotency at the data layer. Give one number ("5 nodes, tolerates 2 failures") and one trade-off.
>
> **Real-world example.** "Our job scheduler elects a leader through etcd with a 15 s lease; every job write carries the leader's revision as a fencing token, so a paused old leader's writes are rejected."

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
    - 1000 instances polling every 1s = 1000 QPS, average delay 0.5s
    - 1000 instances polling every 10s = 100 QPS, average delay 5s
      (average delay = half the poll interval)
    - etcd watch: 0 QPS steady-state, ~100ms propagation
```

### 16.4 Pattern: Distributed Rate Limiter

```
"Design a rate limiter that works across multiple API gateway instances"

  Two approaches:

  1. CENTRALIZED (Redis):
     All gateway instances check/increment a Redis counter.
     Lua script (or MULTI/EXEC): atomic INCR + EXPIRE.
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
     
     Tradeoff: some accuracy loss (illustratively ~10%, depends on
     the rebalance interval) vs 0ms additional latency
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
| Kafka controller election | KRaft (the only mode since Kafka 4.0, March 2025) | Built into Kafka, no external coordinator |
| Kubernetes control plane | etcd (3 or 5 nodes) | All K8s state lives here |
| Singleton service | etcd Election or K8s leader-for-life | One active instance cluster-wide |

---

## 17. Real-world cases — incidents with numbers

> **In plain words.** Consensus and locking bugs rarely look like "Raft is broken". They look like slow disks causing elections, a frozen worker writing late, or a cluster laid out so one outage removes the majority. Each case shows the symptom you would actually see, the number that gave it away, and the fix.
>
> **Real-world example.** Case 17.1: a Kubernetes control plane kept "losing" its etcd leader 30 times an hour; the cause was a 400 ms disk, not the network.

These are **composite scenarios** built from failure modes this chapter describes; numbers are illustrative but internally consistent. Case 17.6 is a public postmortem, and 17.7–17.8 are public Jepsen analyses.

Quick index: frequent leader elections → 17.1 · double processing despite a lock → 17.2 · a healthy leader keeps stepping down → 17.3 · whole cluster read-only after one region fails → 17.4 · lock service CPU at 95% when locks are released → 17.5 · cross-region failover after a short network blip → 17.6 · lock holders overlap even on etcd → 17.7 · acknowledged messages lost after a crash → 17.8

### 17.1 Slow disk, constant elections (Kubernetes control plane)

- **Setup.** 3-member etcd for a 400-node Kubernetes cluster, on network-attached cloud disks. Defaults: heartbeat 100 ms, election timeout 1000 ms.
- **Symptom.** `kubectl` calls time out with `etcdserver: request timed out`; deployments stall for a few seconds several times an hour.
- **Measurement/Diagnosis.** `etcd_server_leader_changes_seen_total` rose by 30 in one hour. Network RTT between members was 0.6 ms, so the network was fine. `etcd_disk_wal_fsync_duration_seconds` p99 was 400 ms, against etcd's guidance of under 10 ms. Other workloads on the same disk caused stalls; when a stall on the leader passed 1000 ms, its heartbeats stopped and followers called an election (§3.4, §15.1).
- **Fix.** Moved etcd to dedicated local SSDs. fsync p99: 400 ms → 3 ms. Leader changes: 30/hour → 0 in the following week. API server write p99: 1.8 s → 60 ms.
- **Lesson.** In etcd, "leader lost" usually means "disk slow". Check fsync latency before touching timeouts; raising the election timeout only hides the problem and slows real failover.

### 17.2 Double payouts behind a Redis lock (payments service)

- **Setup.** A payouts worker takes a Redis lock `SET payout-batch <uuid> NX PX 10000` (10 s TTL), then pays each seller in the batch. Two workers run for redundancy. No fencing.
- **Symptom.** Finance finds 312 sellers paid twice in one night.
- **Measurement/Diagnosis.** JVM logs on worker A show a 14 s full GC pause mid-batch. A's lock expired at 10 s; worker B acquired it and started the same batch; A woke up and kept paying (§9.1). 312 duplicates × $40 average = $12,480 overpaid.
- **Fix.** Moved the lock to etcd and used the lock key's `create_revision` as a fencing token. The payouts table stores `last_token` per batch and the write is `UPDATE ... WHERE batch_id = ? AND last_token <= ?` — a stale worker's write matches 0 rows. Each payout also carries an idempotency key per seller. GC tuning cut max pause 14 s → 200 ms. Duplicates: 312 → 0 over the next 90 nights.
- **Lesson.** A lock only limits who *starts*. Only a check at the storage (fencing token or idempotency key) stops a stale holder from *finishing*.

### 17.3 A flapping node keeps knocking out a healthy leader (dispatch service)

- **Setup.** 5-member Raft cluster holding driver-assignment state for a ride-hailing dispatch system. Pre-Vote and CheckQuorum disabled. Node 5 has a faulty NIC that drops out for 5–20 s at a time.
- **Symptom.** Dispatch writes fail in short bursts about 12 times an hour, though nodes 1–4 are healthy.
- **Measurement/Diagnosis.** Node 5's term climbs by several each time it is cut off (each election timeout it runs a new election). When it reconnects, its higher term makes the leader step down (§3.3). Each forced election cost about 1.2 s of no writes: 12 × 1.2 s = 14.4 s per hour, 0.4% of the time.
- **Fix.** Enabled Pre-Vote and CheckQuorum, and replaced the NIC. Disruptive elections from node 5: 12/hour → 0; write unavailability from this cause: 14.4 s/hour → 0.
- **Lesson.** One bad node should not be able to take out the leader. Pre-Vote makes a node prove it could win before it raises its term.

### 17.4 An even split across two regions (bank ledger)

- **Setup.** A ledger's Raft cluster has 4 members: 2 in region East, 2 in region West. Majority = 3 of 4.
- **Symptom.** Region West loses power. The 2 East members are healthy, yet every ledger write fails for 45 minutes (2,700 s) until West returns.
- **Measurement/Diagnosis.** 2 of 4 is not a majority. The 4th member added no fault tolerance: 4 members tolerate 1 failure, the same as 3 (§6, symbols table).
- **Fix.** 5 members across 3 regions (2 East, 2 West, 1 Central). Losing any one region leaves at least 3 of 5. The cost: a commit now needs a second region to acknowledge, so write latency rose from about 1 ms (in-region) to the RTT to the nearest other region, e.g. 1 ms → 12 ms.
- **Lesson.** Use an odd number of members, and spread them so losing any one region still leaves a majority. Count survivors per failure, not total members.

### 17.5 Thundering herd on lock release (IoT telemetry)

- **Setup.** 500 ingestion workers compete for a per-device-group lock in etcd. Each waiter watches the whole prefix `/locks/group-7/` and, on any change, re-lists all keys to see if it is now first.
- **Symptom.** etcd CPU at 95% and request latency p99 jumps from 5 ms to 900 ms whenever locks change hands.
- **Measurement/Diagnosis.** Every release wakes all 500 waiters, and each lists ~500 keys: 500 × 500 = 250,000 key reads per release. At 20 releases/s that is 5,000,000 key reads/s (§10.2 "thundering herd").
- **Fix.** Each waiter watches only the key just before its own (next-lower `create_revision`), which is what etcd's `concurrency` lock does. Per release: 1 notification and 1 small read instead of 500 notifications and 250,000 key reads. etcd CPU 95% → 10%; p99 900 ms → 6 ms.
- **Lesson.** Queue-style locks should wake exactly one waiter. Watching the predecessor is the whole point of the sequential-key design.

### 17.6 Public postmortem: GitHub, October 2018

- **Setup.** GitHub ran MySQL clusters with primaries in a US East Coast data center, replicas in other sites, and automated failover through Orchestrator.
- **Symptom.** During network maintenance on 21 October 2018, connectivity between the US East Coast network hub and the primary US East Coast data center was lost for **43 seconds**.
- **Measurement/Diagnosis.** In that window the failover system promoted primaries in the US West Coast data center. Some writes accepted in the East had not replicated West, and the application now paid cross-country latency on every write. Restoring consistent data took time; GitHub reported **24 hours and 11 minutes** of degraded service.
- **Fix.** GitHub's postmortem says they changed Orchestrator's configuration to stop promoting database primaries across regional boundaries, and began work on more resilient multi-region operation.
- **Lesson.** A failover mechanism that can decide in seconds can turn a 43-second blip into a day-long recovery. Decide which failovers must never happen automatically (e.g. across regions), and make sure failover never promotes a node missing acknowledged writes — exactly what Raft's voting rule (§5.2) guarantees within one log.

### 17.7 Public analysis: Jepsen on etcd 3.4.3 — locks are leases (2020)

- **Setup.** Jepsen tested etcd's key-value store and its lock API. The workload used etcd
  mutexes with **2-second lease TTLs** to protect updates to a shared set, while Jepsen paused
  processes **every 5 seconds**.
- **Symptom.** The key-value operations were **strict serializable** as claimed. The locks were
  not mutual exclusion: about **18% of acknowledged updates were lost**.
- **Measurement/Diagnosis.** Two causes. Fundamentally, an etcd lock is a lease (§9.3), so a
  paused holder keeps working after its lease has expired and another client has the lock (§9.1).
  There was also a bug: after waiting in the queue, the lock call did not re-check that the
  client's lease was still valid, so a client could be told it held a lock whose lease had
  already expired.
- **Fix.** The etcd team fixed the bug and documented the limits. The mitigation, which is also
  the general rule: do the protected write in an etcd **transaction that compares the lock key**
  (it still exists with the expected revision), so the write fails if the lock was lost (§9.4).
- **Lesson.** Even a linearizable, Raft-backed lock service can't stop a paused holder. Only a
  check at the write can.

### 17.8 Public analysis: Jepsen on NATS 2.12.1 — acknowledged before it was on disk (2025)

- **Setup.** NATS JetStream replicates streams with Raft. By default it **acknowledges writes
  immediately but calls `fsync` only every 2 minutes**, relying on replication across nodes for
  durability.
- **Symptom.** Under Jepsen's fault injection, **acknowledged, committed writes were lost**.
- **Measurement/Diagnosis.** A coordinated power failure (every replica loses its unsynced page
  cache at once) loses the last seconds to minutes of acknowledged messages. Worse, an OS crash on
  a **single** node combined with network delays or process pauses could lose committed writes
  and cause **persistent split-brain**. Raft's safety argument (§5) assumes a node has **flushed
  an entry to disk before it acknowledges it**. A node that forgets entries it voted for breaks
  that assumption.
- **Fix.** Set `sync_interval: always` where acknowledged means durable, and accept the
  throughput cost, or keep the default only for data you can afford to lose. Run replication
  factor 3 across failure domains that don't share power, and never 1 or 2 for important
  streams.
- **Lesson.** "Uses Raft" is not a durability guarantee. Check the fsync policy of any consensus
  system as carefully as its quorum size. etcd's advice to watch `wal_fsync` latency (§17.1)
  exists for the same reason.

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
