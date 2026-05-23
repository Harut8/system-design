# etcd Internals: The Heart of Kubernetes

The single stateful component of a Kubernetes cluster. Everything you reason about above — every Pod, every controller, every watch event, every admission decision — is a derivative of what etcd contains and how fast it can mutate. Lose etcd, lose the cluster. Make etcd slow, the whole cluster is slow (apiserver writes block on etcd, controllers block on apiserver watches, kubelets block on controllers). Make etcd fast, none of the rest matters.

This chapter is a staff-level deep-dive into etcd as it actually runs underneath kube-apiserver. We go through the Raft consensus core, the bbolt B+tree storage layer, the MVCC revision model that makes `resourceVersion` and `kubectl get -w` work, the watch implementation, leases, transactions, compaction, defragmentation, snapshots, observability metrics, and the failure scenarios that have taken down real clusters. By the end you should be able to: read an etcd metric dashboard and predict the cluster's next failure mode, write the etcdctl command to fix it, and understand precisely which knob in etcd you are turning when you tune `--auto-compaction-mode`, `--quota-backend-bytes`, or `--heartbeat-interval`.

Prerequisites: familiarity with Raft fundamentals (covered in databases/16-failure-detection-and-leader-election.md §4.1), OS storage primitives — mmap, fsync, page cache (databases/00-os-and-hardware-internals.md §§10–13), and the Kubernetes architecture map in 03-kubernetes-architecture-overview.md.

---

## Table of Contents

1. [Why etcd: Requirements and Alternatives](#1-why-etcd-requirements-and-alternatives)
2. [Raft, Deep Enough to Operate It](#2-raft-deep-enough-to-operate-it)
3. [The Storage Layer: WAL, Snapshot, bbolt](#3-the-storage-layer-wal-snapshot-bbolt)
4. [MVCC and the Revision Model](#4-mvcc-and-the-revision-model)
5. [Watch: The Nervous System](#5-watch-the-nervous-system)
6. [Leases: TTL-Based Expiration](#6-leases-ttl-based-expiration)
7. [Transactions: Compare-Then-Do-Else](#7-transactions-compare-then-do-else)
8. [Compaction: Reclaiming Revisions](#8-compaction-reclaiming-revisions)
9. [Defragmentation: Reclaiming Disk](#9-defragmentation-reclaiming-disk)
10. [Backup and Restore](#10-backup-and-restore)
11. [Performance Tuning](#11-performance-tuning)
12. [Operational Topology](#12-operational-topology)
13. [The apiserver ↔ etcd Relationship](#13-the-apiserver--etcd-relationship)
14. [Observability: Metrics That Matter](#14-observability-metrics-that-matter)
15. [Failure Scenarios](#15-failure-scenarios)
16. [Pitfalls and Anti-Patterns](#16-pitfalls-and-anti-patterns)
17. [TL;DR](#17-tldr)

---

## 1. Why etcd: Requirements and Alternatives

### 1.1 The Requirements Kubernetes Imposes

Kubernetes did not pick etcd because it was fashionable. It picked etcd because the apiserver needs exactly the following set of properties from its backing store, and the set is unusual enough that no general-purpose database fits cleanly.

```
THE KUBERNETES STATE STORE CONTRACT

  1. Strongly consistent reads
     ─ A controller that just wrote a Pod must, on its next List,
       see that Pod. No "eventually consistent" wiggle room.
     ─ Linearizable reads are the default for kube-apiserver "Get"
       calls; the cheaper "ResourceVersion=0" mode trades strict
       consistency for cache-local reads.

  2. Strongly consistent writes with optimistic concurrency
     ─ resourceVersion CAS: "update this Pod, but only if its
       version is still 12345". The store must do compare-and-swap
       atomically across all keys (multi-key transactions).

  3. Cluster-wide ordered revision
     ─ Every mutation gets a globally-monotonic integer revision.
       resourceVersion exposed to clients == etcd revision.
     ─ This is how watches resume, how Lists are point-in-time
       consistent, how apiserver detects "your watch is too old".

  4. Long-lived watch streams with reliable delivery
     ─ Clients open a watch and expect to receive every mutation
       in revision order until they explicitly close.
     ─ A 5000-node cluster has ~50 000 active watches. The store
       must fan out efficiently.

  5. Lease + TTL
     ─ Node heartbeats, leader-election Leases, event TTLs,
       SA token expiration: all driven by automatic key
       expiration on a lease that the client renews.

  6. ~10 000 writes/second peak, multi-GiB working set
     ─ Modest by OLTP standards. But the writes are tiny (single
       Pod ~10 KiB) and the workload is dominated by watches,
       not by writes.

  7. Operationally simple, embeddable, single binary
     ─ Sits next to kube-apiserver (stacked control plane) or
       on its own VMs (external etcd). No DBA team. Backup is
       a single command. Restore is a single command.
```

These requirements eliminate most candidates immediately. A relational database (Postgres, MySQL) gives you (1), (2), (6) easily but fails (4) — there is no efficient watch protocol over SQL, and the polling alternatives saturate the database CPU long before reaching 10k writes/s on tiny rows. A document store (MongoDB, Cassandra) gives you (6) and (4) but fails (1) under partition. A blob store (S3, GCS) fails (1), (2), (3), and (4) all at once.

The two real alternatives are **ZooKeeper** and **Consul**, both of which also offer (1)–(5). Kubernetes briefly supported ZooKeeper in early drafts (the "Borg legacy" period); the choice of etcd was driven by these specifics:

```
WHY NOT ZOOKEEPER

  ─ ZooKeeper's data model is hierarchical (znodes) and not
    transactional across the tree. etcd is flat KV with
    transactional multi-key compare-and-swap.

  ─ ZooKeeper watches are ONE-SHOT. A client must re-register
    after each event. Kubernetes wants long-lived streaming
    watches; building that on top of ZK requires polling +
    re-registration loops that don't scale.

  ─ ZooKeeper has no MVCC. Reads see a "live" state with a
    zxid timestamp but you cannot easily ask "give me the
    state at zxid X" or "watch from zxid X". Kubernetes
    needs exactly this for List-then-Watch consistency.

  ─ Operationally, ZooKeeper requires a JVM, separate tuning
    for heap vs young gen, plus log/snapshot directory care.
    etcd is a single Go binary with predictable RSS.

  ─ ZooKeeper's data size limit per znode is 1 MiB and
    discouraged at any size. Kubernetes objects regularly
    push past 100 KiB (Pods with many env vars, CRDs).

WHY NOT CONSUL

  ─ Consul historically used a similar Raft + KV model,
    BUT its primary purpose is service discovery and the KV
    is a secondary surface. Watches were less robust.

  ─ Consul's KV transaction semantics are weaker than etcd's
    v3 Txn (no else-clause until late, no nested compares).

  ─ HashiCorp/Consul has its own ecosystem; etcd is a CNCF
    project and aligned with the Kubernetes release cadence.

WHY NOT POSTGRES (the eternal LinkedIn-post suggestion)

  ─ Postgres has no native watch. LISTEN/NOTIFY is push-only,
    payload-bounded, not durable across reconnect, and
    delivers no ordering guarantee that survives crashes.

  ─ Optimistic concurrency on resourceVersion would need
    a serial column + extra trigger logic + an outbox table
    + a separate fan-out process. You are now rebuilding etcd.

  ─ MVCC: Postgres has MVCC of course, but the "revision"
    needed is cluster-global monotonic, not per-table xmin.

  ─ Operationally: Postgres needs WAL archiving, pg_basebackup,
    streaming replication, replication slot management, vacuum
    tuning. The cognitive surface alone disqualifies it for
    a control-plane store.

  Postgres-as-K8s-backend exists (k3s with kine), but it
  re-implements an etcd v3 facade over Postgres/MySQL/SQLite
  and is targeted at edge / single-node, not production HA.
```

The net of (1)–(7) is: **etcd is a deliberately narrow Raft+MVCC+watch KV designed for control-plane workloads**. It is not a database. It is a coordination store. Treating it like a database (storing application data, big blobs, log entries) is the single largest source of etcd outages, and we will come back to that in §16.

### 1.2 The etcd Version Cliff

There are two etcd APIs and they have nothing in common architecturally. Kubernetes >= 1.13 uses **etcd v3**, which is gRPC, protobuf-encoded, MVCC, and is what this chapter describes. The legacy **etcd v2** API was a REST/HTTP/JSON tree with no MVCC; it was removed from etcd 3.6 entirely. If you see documentation referencing `etcdctl` flags without `ETCDCTL_API=3`, you are reading v2 documentation and should discard it.

```bash
# Always pin v3 explicitly. Modern etcdctl (>=3.4) defaults to v3,
# but defensive scripts still set it.
export ETCDCTL_API=3
export ETCDCTL_ENDPOINTS=https://127.0.0.1:2379
export ETCDCTL_CACERT=/etc/kubernetes/pki/etcd/ca.crt
export ETCDCTL_CERT=/etc/kubernetes/pki/etcd/server.crt
export ETCDCTL_KEY=/etc/kubernetes/pki/etcd/server.key
etcdctl endpoint status --write-out=table
```

Sample output from a healthy 3-member kubeadm-built cluster:

```
+-----------------------+------------------+---------+---------+-----------+
|       ENDPOINT        |        ID        | VERSION | DB SIZE | IS LEADER |
+-----------------------+------------------+---------+---------+-----------+
| https://10.0.0.1:2379 | 8e9e05c52164694d |  3.5.10 |  187 MB |     true  |
| https://10.0.0.2:2379 | b329d05c52e69a4d |  3.5.10 |  187 MB |    false  |
| https://10.0.0.3:2379 | c11ad11c5e6b1a4f |  3.5.10 |  186 MB |    false  |
+-----------------------+------------------+---------+---------+-----------+
```

Three things to read off this every time: (a) all VERSIONs agree; mixed-version clusters are supported only during upgrades, (b) DB SIZE is comparable across members (large divergence usually means a follower lagging in compaction), (c) exactly one IS LEADER is true. We will use endpoint status repeatedly through this chapter; memorize it.

---

## 2. Raft, Deep Enough to Operate It

Raft is described in databases/16-failure-detection-and-leader-election.md §4.1. We assume you understand the basic structure: a single leader, a replicated log, election by majority, terms as monotonic election numbers. This section covers the etcd-specific operational consequences.

### 2.1 The State Machine

Each etcd member is in exactly one of three Raft states at any moment.

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                       RAFT STATE MACHINE                         │
  └─────────────────────────────────────────────────────────────────┘

           timeout, start election
       ┌──────────────────────────────┐
       │                              │
       │                              ▼
   ┌────────┐  heard from   ┌──────────────┐
   │FOLLOWER│ ◄────────────│  CANDIDATE   │
   │        │  higher term  │              │
   │        │               │ (vote for    │
   └────────┘               │  self, RPC   │
       ▲                    │  RequestVote)│
       │                    └──────┬───────┘
       │                           │
       │ heard from leader,        │ majority votes
       │ stepped down              │ received
       │                           ▼
       │                    ┌──────────────┐
       └────────────────────│   LEADER     │
        higher term seen    │              │
                            │ AppendEntries│
                            │ heartbeats   │
                            │ every 100ms  │
                            └──────────────┘

  ELECTION CONDITIONS:
  ─ Follower's election timer fires (randomized in [election-timeout,
    2 * election-timeout]).
  ─ Follower increments currentTerm, votes for self, broadcasts
    RequestVote with (term, lastLogIndex, lastLogTerm).
  ─ Receiver grants vote iff:
      candidate's term >= own term, AND
      hasn't voted yet in this term, AND
      candidate's log is "at least as up-to-date" as own
      (lastLogTerm higher, or same term and lastLogIndex >= own).
  ─ On majority of votes: become leader, immediately send
    empty AppendEntries to claim leadership and reset all
    followers' election timers.

  STEP-DOWN:
  ─ Any RPC carrying a term > currentTerm immediately steps
    the recipient down to follower and updates currentTerm.
  ─ This is how stale leaders learn they have been deposed
    (e.g., after a partition heals).
```

In etcd, the parameters that drive this state machine are:

- `--heartbeat-interval` (default 100 ms): leader sends empty AppendEntries every 100 ms.
- `--election-timeout` (default 1000 ms): a follower that has not heard from the leader for at least 1000 ms (randomized up to 2000 ms) starts an election. Must be at least 5× the heartbeat interval; etcd refuses to start if it's smaller, and the recommended ratio is 10×.

The election timeout is the floor on apiserver write latency during a leader failure: nothing commits until a new leader is elected, which requires at least one randomized election round-trip. Real-world: with the default 1 s timeout, expect 1–3 seconds of write unavailability on a clean leader crash; 5–10 seconds if the first election split-votes and has to redo.

### 2.2 The Log

Raft's mental model is that every member maintains an append-only log. Each entry has:

```go
// In etcd-io/raft, raftpb.Entry — github.com/etcd-io/raft/raftpb/raft.proto
message Entry {
  uint64 Term  = 2;
  uint64 Index = 3;
  EntryType Type = 1;  // Normal, ConfChange, ConfChangeV2
  bytes Data   = 4;
}
```

`Term` is the election term during which the entry was created. `Index` is the position in the log. Together (Term, Index) uniquely identifies an entry across the cluster — this is the Raft invariant: *if two members have an entry with the same (Term, Index), the entries are identical and so are all preceding entries.*

```
  THE RAFT LOG (per member)

      index:   1    2    3    4    5    6    7    8    9
      term:    1    1    2    2    2    3    3    3    3
            ┌────┬────┬────┬────┬────┬────┬────┬────┬────┐
   leader:  │ X=1│ Y=2│ Z=3│ X=4│ del│ X=5│ Y=6│ Z=7│ X=8│ ◄── leader's view
            └────┴────┴────┴────┴────┴────┴────┴────┴────┘
                                  ▲                        ▲
                                  commit index = 5         lastIndex = 9
                                  applied index ≤ commit   only 1-5 are committed

  COMMIT INDEX:
  ─ The leader advances commitIndex once a majority (including
    itself) has appended an entry. Followers learn the new
    commitIndex via the next AppendEntries (the leader piggybacks
    it).
  ─ Only entries up to commitIndex are visible to clients
    (applied to the state machine, i.e., MVCC writes happen).

  APPEND ENTRIES (RPC body)
    term, leaderId, prevLogIndex, prevLogTerm,
    entries[], leaderCommit
  ─ Follower rejects if prevLog{Index,Term} don't match its
    own log → leader decrements its nextIndex for that follower
    and retries. This converges in O(log mismatch) round-trips.
  ─ Once a follower accepts, leader can extend matchIndex[i].

  COMMIT RULE:
    Leader's commitIndex = max N such that
      matchIndex[i] >= N for majority of members, AND
      log[N].term == currentTerm
    The second clause prevents the "Figure 8" pathology
    where a leader could commit an entry from an earlier term
    that gets overwritten by a more recent election.
```

When you see a long etcd outage that "feels like writes are queued forever", the most common cause is that a follower's log has fallen so far behind that the leader is shipping it AppendEntries faster than the follower's disk can fsync. The leader has the entries; the cluster cannot commit them because the slow follower's matchIndex won't advance, and the third member alone is not a majority. (Quorum on a 3-member cluster is 2: leader + 1 follower.)

### 2.3 Pre-Vote, ReadIndex, Lease-Based Reads

Three optimizations that are critical for production etcd behavior.

**Pre-Vote** (enabled by default in etcd ≥ 3.4). The vanilla election protocol has a pathology: a partitioned node will increment its term repeatedly while isolated, then on heal it will be far ahead of the cluster, and its term increment alone forces the existing leader to step down. PreVote inserts a phase before the real RequestVote: the candidate asks peers "*would* you vote for me if I started an election?", and only increments its term once it has a majority of yeses.

```
  WITHOUT PRE-VOTE                WITH PRE-VOTE

  partition heals →               partition heals →
  M3 (term 47) joins              M3 (term 12) joins
  M3 sends RequestVote(t=47) →    M3 sends PreVote(t=13) →
  M1,M2 (term 12) update to 47    M1,M2 reject (M3's log
  and step down                   stale, OR they hear from leader)
  → unnecessary election          → no term bump, no disruption
```

Without pre-vote, every brief network blip can cost you a leader election. With pre-vote enabled, only legitimately-electable nodes disturb the cluster. There is no reason to disable it.

**ReadIndex** is the protocol for serving a linearizable read without a full Raft round-trip writing to the log. The flow:

```
  LINEARIZABLE READ via ReadIndex

  client → leader: Range(key)
                     │
                     │ 1. record current commitIndex as readIndex
                     │
                     │ 2. broadcast a heartbeat to majority,
                     │    confirm we are still leader
                     │    (otherwise reads from a deposed leader
                     │     could be stale)
                     │
                     │ 3. wait until appliedIndex >= readIndex
                     │    (i.e., the state machine has caught up
                     │     to where we were when we received the read)
                     │
                     │ 4. read from local MVCC and return
                     ▼
                  response

  Cost: one heartbeat round-trip; no fsync; cheaper than a write.
  Latency floor: max(network RTT, time to apply pending writes).
```

ReadIndex is the default for `etcdctl get`. The cheaper option, `--consistency=s` (serializable), skips steps 2 and 3 and reads directly from local state — fast, but you might read stale data from a follower if you happen to query one that hasn't applied the latest commits. Kubernetes apiserver uses linearizable Gets by default for safety, and explicitly switches to serializable for List operations that pass `resourceVersion=0` (the watch cache fallback).

**Lease-based reads** are a further optimization where the leader, instead of doing a heartbeat round-trip per read, relies on a *leader lease* (not to be confused with etcd's user-facing Lease object — naming collision). If the leader knows it has heard from a majority of followers within the last `election-timeout/clock-drift-factor` window, it can serve reads locally without an extra heartbeat, because no other leader could possibly have been elected in that window. etcd implements this; it's why successive reads from a leader can be sub-millisecond.

### 2.4 Configuration Changes (Joint Consensus)

Adding or removing a member is itself a Raft operation, and a dangerous one if done naively. Imagine a 3-member cluster {A, B, C}. We want to add D and E to make a 5-member cluster. If membership changes were applied to each member independently, there is a moment where some members think the config is {A,B,C} (quorum 2) and others think it's {A,B,C,D,E} (quorum 3). Two disjoint majorities can form: {A,B} thinks it has quorum on the old config; {C,D,E} thinks it has quorum on the new config. Split-brain.

The original Raft paper describes **joint consensus**: a transitional configuration `C_old,new` is committed first, in which any decision must be approved by majorities in BOTH the old and new config. Once stable, transition to `C_new`. This guarantees no two disjoint majorities can exist.

In practice, etcd uses a simpler variant: **single-member changes**. Add or remove exactly one member at a time. With one change at a time and the rule that a config-change entry must be committed before the next is proposed, the safety property holds without the joint-consensus machinery. The corollary is that to go from 3 → 5 members, you do two separate `member add` operations; never both at once.

```bash
# Add a new member (one at a time)
etcdctl member add infra3 \
  --peer-urls=https://10.0.0.4:2380
# Returns: ETCD_INITIAL_CLUSTER, ETCD_INITIAL_CLUSTER_STATE, etc.
# Use these in the new member's startup command line.

etcdctl member list -w table
# +------------------+---------+--------+--------------------------+
# |        ID        | STATUS  |  NAME  |       PEER ADDRS         |
# +------------------+---------+--------+--------------------------+
# | 8e9e05c52164694d | started | infra0 | https://10.0.0.1:2380    |
# | b329d05c52e69a4d | started | infra1 | https://10.0.0.2:2380    |
# | c11ad11c5e6b1a4f | started | infra2 | https://10.0.0.3:2380    |
# | d52a8d05c52e69ab | unstarted| infra3 | https://10.0.0.4:2380   |
# +------------------+---------+--------+--------------------------+

# Only after infra3 has joined and become "started" do you add infra4.
```

The `unstarted` status is the moment of vulnerability: the cluster now has 4 voting members but only 3 are reachable. Quorum is 3; if any of the original 3 fails, you cannot make progress. Always plan member additions during quiet periods, and consider the new etcd-learner feature (§2.5).

### 2.5 Learners (Non-Voting Members)

Since etcd 3.4, you can add a member as a **learner** — receives the log, applies it to its state machine, but does not count toward quorum. Use this whenever you replace or add a member to avoid the quorum-window risk.

```bash
etcdctl member add infra3 \
  --peer-urls=https://10.0.0.4:2380 \
  --learner

# Wait until learner is caught up (status shows IS LEARNER true, raft applied
# index within a few thousand of leader's). Then promote:
etcdctl member promote d52a8d05c52e69ab
```

Promotion is the dangerous step; before that, the learner adds zero risk because quorum still counts only the original voting members.

### 2.6 Leadership Changes and Apiserver Write Latency

Every leader change is a brief write outage. To make it predictable:

```bash
# Voluntary leadership transfer (graceful, no election needed)
# Use BEFORE defragging the current leader, before draining
# its node for maintenance, before kernel upgrade reboots.
etcdctl move-leader b329d05c52e69a4d
# Leadership transferred from 8e9e05c52164694d to b329d05c52e69a4d
```

Move-leader does a single AppendEntries round-trip, transferring leadership without an election. Write latency blip is typically <50 ms versus the 1–3 seconds of an unplanned election.

In etcd-io source: `server/etcdserver/raft.go` and `server/etcdserver/server.go` drive the raft module. The raft module itself lives in a separate repo, `github.com/etcd-io/raft` — what you import as `go.etcd.io/raft/v3`. The state machine is in `raft.go::raft.Step()`, the leader logic in `raft.go::raft.becomeLeader()`, log replication in `node.go` and `progress.go`. The `progress.go` per-follower state machine (Probe/Replicate/Snapshot) is where the leader tracks each follower's catch-up state and is one of the highest-yield reads to understand etcd internals.

### 2.7 The etcd-io/raft Module Source Layout

For staff engineers who will actually read the code, here is the orientation map.

```
  github.com/etcd-io/raft/                  (separate repo; module
                                             go.etcd.io/raft/v3)
  ├── raft.go               core state machine: raft struct,
  │                         Step(), becomeFollower/Candidate/Leader,
  │                         tickElection, tickHeartbeat
  ├── node.go               Node interface, the public API that
  │                         server/etcdserver uses; manages the
  │                         Ready channel, the apply loop boundary
  ├── log.go                in-memory raft log (unstable + storage)
  ├── log_unstable.go       entries not yet persisted to stable storage
  ├── storage.go            Storage interface (etcd uses WAL+snap
  │                         as its implementation)
  ├── progress.go +         per-follower tracking: nextIndex,
  │   tracker/progress.go   matchIndex, Probe/Replicate/Snapshot
  ├── tracker/              configuration & joint-consensus tracking
  ├── confchange/           ConfChange & ConfChangeV2 logic
  ├── rafttest/             test harness (DSL for fuzz scenarios)
  └── raftpb/raft.proto     wire format: Message, Entry, Snapshot

  Embedding in etcd:
  github.com/etcd-io/etcd/server/etcdserver/
  ├── raft.go               wraps raft.Node, runs the Ready loop:
  │                            for r := range Node.Ready():
  │                                persist r.HardState + r.Entries to WAL
  │                                send r.Messages
  │                                apply r.CommittedEntries to MVCC
  │                                ack via Node.Advance()
  ├── server.go             EtcdServer: the apply loop, the lessor,
  │                         the v3 RPC handlers
  └── api/v3rpc/            gRPC service implementations
                            (KV, Watch, Lease, Maintenance, Cluster)
```

The single most important file to internalize is `raft.go::raft.Step()`. It is a switch on the incoming message type (MsgVote, MsgVoteResp, MsgApp, MsgAppResp, MsgHeartbeat, …) crossed with the current state (follower/candidate/leader). Every Raft event, including timer ticks, is funneled through it. If you can mentally execute Step() for a vote-then-leader-elect sequence, you understand etcd's failover behavior.

The Ready loop in the etcdserver is the second crucial pattern: it pulls a `Ready` from the raft module, persists what needs persisting (WAL append, snapshot install), broadcasts messages over the network, applies committed entries to the MVCC state machine, then calls `Advance()` to tell the raft module the cycle is complete. This separation lets the raft module remain a pure-logic state machine (testable, deterministic) while the host (etcd) handles all I/O.

---

## 3. The Storage Layer: WAL, Snapshot, bbolt

etcd persists three kinds of data, on three separate file types, with three different durability strategies. Confusing them is the most common operational mistake.

```
  ON-DISK LAYOUT (default: --data-dir=/var/lib/etcd)

  /var/lib/etcd/
  ├── member/
  │   ├── wal/                    ← Raft log entries (write-ahead log)
  │   │   ├── 0000000000000000-0000000000000000.wal
  │   │   ├── 0000000000000001-00000000001cc4e1.wal
  │   │   └── 0000000000000002-000000000038b3c1.wal
  │   ├── snap/                   ← Raft snapshots (state machine + Raft metadata)
  │   │   ├── 00000000000003e7-0000000000000064.snap
  │   │   ├── db                  ← The bbolt B+tree file (MVCC backend)
  │   │   └── db.tmp              ← (during snapshot rewrite)
  │   └── lock                    ← Single-writer lock
  └── ...

  WHAT EACH IS FOR:

    WAL       Every Raft proposal (every write to etcd) is appended
              here BEFORE being applied to the state machine. fsync'd
              on every entry. This is the durability boundary.

    SNAP      Periodically (every --snapshot-count entries; default
              100 000), etcd takes a snapshot: the entire state
              machine plus Raft metadata. After a snapshot completes,
              the corresponding WAL segments can be truncated.

    db        The bbolt B+tree backing the MVCC store. This is where
              keys-by-revision live. Modified asynchronously by the
              apply loop; durability is checkpointed by Raft snapshots
              and bbolt's own fsync-on-commit cycle.
```

### 3.1 The WAL: Append-Only, Fsync-Per-Entry

The WAL is the canonical persistent log. Every Raft entry the leader proposes is written here, fsync'd, and only THEN is the proposal counted toward replication progress. If the leader fsync stalls, the entire cluster stalls. This is why disk performance is the dominant factor in etcd write latency.

WAL implementation details, from `server/wal/wal.go`:

- WAL is a sequence of 64-MiB segment files, named `{seq}-{first-index}.wal`.
- Each record is length-prefixed and CRC32-checksummed.
- Writes go: `write()` to the file → `fsync()` on the file descriptor. No mmap.
- Why no mmap? mmap writes are not durable until msync, and the failure mode of a partial msync on crash is implementation-defined and filesystem-defined. The WAL needs absolute, predictable durability: write, fsync, success means "on stable storage even if I crash this nanosecond". `write+fsync` is the simplest path that gives that guarantee. (This is the same reason Postgres and most other serious databases avoid mmap for WALs; see databases/00-os-and-hardware-internals.md §11.)

The metric to watch is `etcd_disk_wal_fsync_duration_seconds`. p99 should be < 10 ms. Anything > 100 ms means the disk cannot keep up with cluster writes; you will start to see election timeouts and unavailability.

### 3.2 Snapshots: Truncating the WAL

If the WAL grew forever, etcd would have to replay every write since cluster creation on every restart. Snapshots solve this by checkpointing the state machine. After a snapshot is durable, the WAL entries preceding it can be discarded.

```
  WAL + SNAPSHOT INTERACTION

  WAL entries:    1 ... 100000 100001 ... 200000 200001 ... 300000
                  └──────────────┘ └──────────────┘ └──────────────┘
                  snap @ 100000    snap @ 200000     active region
                                                     (since last snap)

  ─ Default --snapshot-count = 100 000 entries
  ─ When the apply loop has applied 100 000 entries past the last
    snapshot, etcd creates a new snapshot (an atomic copy of
    Raft state + a hash pointer to bbolt's then-current state).
  ─ etcd keeps the last N snapshots (default 5) for recovery
    redundancy; older ones are deleted.
  ─ WAL segments older than the oldest retained snapshot are deleted.

  Why is this knob significant?
    Larger --snapshot-count = longer WAL between snapshots, longer
      replay on restart, more memory pressure during replay.
    Smaller --snapshot-count = more frequent snapshot I/O bursts,
      more frequent bbolt full-state operations.
```

The snapshot itself is small (Raft metadata + a pointer/transaction ID into bbolt — bbolt is *not* copied per snapshot; that's where confusion arises). The bbolt file `db` is shared across all Raft snapshots and is the actual state.

### 3.3 bbolt: The MVCC Backend

bbolt is etcd-io's fork of Ben Johnson's boltdb. It is a single-file embedded B+tree on top of a memory-mapped file. Source: `github.com/etcd-io/bbolt`. The relevant parts:

```
  BBOLT FILE LAYOUT (file: db, mmap'd into the etcd process)

  Page 0: Meta Page A   ┐
  Page 1: Meta Page B   ├── two redundant root pointers for crash safety
  Page 2: Freelist      ─── pages currently free for reuse
  Page 3..N: data       ─── B+tree internal + leaf pages,
                            and "bucket" trees (etcd uses a few buckets:
                            "key" for MVCC keyValue, "lease" for leases,
                            "meta" for revision counters, etc.)

  PAGE SIZE: 4096 bytes (the OS page size; matches mmap unit).

  CHECKPOINT MODEL (the boltdb commit protocol):
    1. Start transaction → reserve pages for any writes (allocate
       from freelist or extend file).
    2. Write all new pages via mmap stores (these are dirty in
       the page cache).
    3. msync the data pages, then fsync the file.
    4. Update the OTHER meta page (alternating A/B) to point at
       the new root.
    5. msync + fsync the meta page.

    If the process crashes between steps 1-4, the OLD meta page
    still points at a fully-consistent tree. After a crash,
    the loader inspects both meta pages, picks the one with the
    highest valid txid, and proceeds. This is the COW (copy-on-write)
    + alternating-meta scheme that makes bbolt crash-safe without
    a separate WAL of its own.
```

A page in bbolt is either a leaf node, an internal node, a bucket page, or a free page. Modifications never overwrite a live page in place; they allocate a new page (COW) and the old page becomes free after the meta-page update commits. This is what makes the file size monotonic until you defragment (see §9).

Why bbolt uses mmap and the WAL doesn't:
- The WAL's writes are sequential and small (one Raft entry at a time). Direct write+fsync is the cheapest way to durably append.
- bbolt's writes are scattered across B+tree pages. mmap means writes are absorbed into the OS page cache and coalesced by msync. The file is also read-heavy (every MVCC Get walks the tree), and mmap lets the OS manage page cache automatically.
- The COW + alternating-meta-page scheme means a partial msync that fails to update the new meta page leaves the OLD meta page valid; no corruption window. The WAL would have no equivalent escape hatch because Raft expects monotonic, gap-free appends.

This is a textbook case of using mmap precisely where its semantics fit (large, structured, mostly-read with copy-on-write writes) and avoiding it where they don't (small, write-heavy, append-only with strict per-entry durability).

```
  THE THREE STORAGE FILES TOGETHER

   apiserver write
         │
         ▼
   etcdserver receives, leader proposes
         │
         ▼
   ┌─────────────────┐
   │ Raft module     │ ── persists entry to ──►  WAL (fsync per batch)
   │ (raft.go)       │                            │
   └─────────────────┘                            │ entry committed
         │                                        │ (majority appended)
         ▼                                        ▼
   ┌─────────────────┐                      Apply loop reads
   │ Apply loop      │ ◄─── pulls from raft.Ready
   │ (server.go)     │                      committed entries
   └─────────────────┘
         │
         ▼
   ┌─────────────────┐
   │ MVCC + bbolt    │ ── mutates B+tree, COW pages,
   │ (mvcc/backend)  │     periodic bbolt commit (fsync)
   └─────────────────┘
         │
         ▼ when --snapshot-count entries applied
   ┌─────────────────┐
   │ Snapshot writer │ ── writes .snap file pointing at
   │                 │     current bbolt txid, truncates WAL
   └─────────────────┘
```

A single client write thus touches the WAL fsync (always), the bbolt page cache (always, via the apply loop), and the bbolt fsync (deferred and batched, typically every few hundred ms or when a certain number of pages have been dirtied). The metric `etcd_disk_backend_commit_duration_seconds` measures the bbolt commit specifically; the `wal_fsync_duration` metric measures the WAL. Both matter and they have different distributions; we cover them in §14.

---

## 4. MVCC and the Revision Model

This is the section that explains why `kubectl get pod -w` works the way it does, what `resourceVersion` means, and what the "required revision has been compacted" error you have certainly seen actually means.

### 4.1 Every Key Has a History

In etcd v3, a key does not have a single value. It has a sequence of (revision, value) entries. The revision is a 64-bit integer that is monotonic ACROSS THE ENTIRE KEYSPACE — not per-key. Every mutation, no matter which key, advances the global revision by exactly 1.

```
  THE GLOBAL REVISION

     time →

     rev:   1    2    3    4    5    6    7    8    9
     op:    PUT  PUT  PUT  PUT  DEL  PUT  PUT  PUT  PUT
     key:   /a   /b   /a   /c   /a   /a   /b   /d   /a
     val:   1    2    3    4    -    5    6    7    8

   Per-key history (the "keyIndex"):
     /a  →  rev 1, rev 3, rev 5 (tombstone), rev 6, rev 9
     /b  →  rev 2, rev 7
     /c  →  rev 4
     /d  →  rev 8

   Current revision of the keyspace: 9.
   Compact revision: (initially 0; advances as compaction happens)
```

The MVCC backend has two main components:

1. **The keyIndex** (in-memory, btree, source: `server/storage/mvcc/key_index.go`). For each logical key, an ordered list of `generation` structures; each generation is the lifetime of the key between creation and tombstone, holding the revisions at which it was modified. `find(key, atRev)` returns the revision of `key` that was current at or before `atRev`.
2. **The keyValue store** (in bbolt, source: `server/storage/mvcc/kvstore_txn.go`). The actual values, indexed by encoded revision. Lookup: `bbolt.Get("key", revisionBytes(rev))` returns the stored `mvccpb.KeyValue` protobuf.

```
  MVCC TWO-LEVEL LOOKUP

      Get("/registry/pods/default/nginx")
                │
                ▼
        keyIndex (in-memory)
                │
                │  find latest generation, last revision = (mainRev=8472, sub=0)
                │
                ▼
        bbolt.Get(bucket="key", key=encode(8472, 0))
                │
                ▼
        mvccpb.KeyValue {
          key:           "/registry/pods/default/nginx",
          create_revision: 5921,
          mod_revision:    8472,
          version:         12,  // per-key counter, increments per put
          value:           <protobuf-encoded Pod object>,
          lease:           0,
        }
```

The fields you see on a Kubernetes object map directly:
- `metadata.resourceVersion` == etcd `mod_revision`. It's a stringified int.
- The "creationTimestamp" is not the revision; it's a separate timestamp Kubernetes assigns.
- The cluster-wide `resourceVersion` returned on List operations == etcd's current global revision.

This explains a subtle behavior: two Pods updated in different namespaces in quick succession will have resourceVersions that are close but not consecutive (because writes to other keys, like Lease heartbeats or Event objects, fall in between). The resourceVersion is a *cluster-wide ordering token*, not a per-object counter.

### 4.2 The Encoded Revision

In bbolt, revisions are encoded as 17 bytes:

```
  REVISION BYTES (mvcc/revision.go)

    [8 bytes main]  [1 byte separator]  [8 bytes sub]

    main: the global revision (uint64 big-endian)
    sub:  the index within a Txn (a single Txn that touches
          N keys produces N keyValues, all with the same main
          revision but sub = 0, 1, 2, ..., N-1)

  Why big-endian?  So that bbolt's lexicographic key order
  equals revision numeric order. This lets etcd do range scans
  over the "key" bucket sorted by revision — used during
  compaction, watch resumption, and snapshot transfer.
```

### 4.3 Watch and the Revision Stream

When a watcher says "watch from rev=8400", etcd does:

```
  WATCH RESUME LOGIC

  1. Check: is 8400 still available?
       If 8400 < compactRevision → ERROR ErrCompacted
          ("required revision has been compacted")
       Otherwise proceed.

  2. Stream all key mutations with mod_revision >= 8400, in
     revision order. These are read by range-scanning the
     "key" bucket from encode(8400, 0) forward.

  3. Once caught up to currentRevision, transition the watcher
     to "synced" state: deliver events in real time as they happen.
```

This is the engine behind every `kubectl get -w` and every controller's informer. We dive deeper in §5.

### 4.4 Tombstones and the Cost of Delete

A delete in MVCC is not a removal — it's a tombstone. The keyIndex grows a "tombstone" entry for the key at the deletion revision; the keyValue store gets a row with a `KeyValue` marked deleted. Subsequent gets at any revision >= deletion revision return "not found"; gets at earlier revisions return the previous value.

```
  DELETE /foo at revision 100

    keyIndex /foo:
      generation 1:
        revs: [50, 75, 100 (TOMBSTONE)]
      (any future PUT /foo starts generation 2)

    bbolt "key" bucket:
      encode(50, 0)  → {key=/foo, value=..., mod_rev=50}
      encode(75, 0)  → {key=/foo, value=..., mod_rev=75}
      encode(100, 0) → {key=/foo, value=nil, mod_rev=100,
                        type=DELETE}

  Until compaction at rev>=100, all three entries remain on disk.
  After compaction, the entire generation is removed (because
  it is closed AND ends before the compact revision).
```

This is why uncompacted etcds grow forever and why Kubernetes objects with frequent updates (Lease objects, Event objects, Node status) are the dominant on-disk consumers. A leader-election Lease that renews every 2 seconds for a year produces ~15 million revisions for a single key. Compaction (§8) is what makes this sustainable.

### 4.4a Visualizing the keyIndex

```
  THE KEYINDEX (in-memory btree, mvcc/key_index.go)

  KeyIndex { key }                       ── e.g., key = "/registry/pods/default/nginx"
    │
    └─── []generation                    ── ordered list of generations
                                            (a generation is "create → ... → tombstone")
            │
            ├── generation[0]
            │     created: rev 12         ← first PUT
            │     revs:    [12, 47, 88]   ← subsequent PUTs (same key, same lifetime)
            │     tombstone: rev 102      ← DELETE; generation closed
            │
            ├── generation[1]
            │     created: rev 155        ← key re-created (PUT after DELETE)
            │     revs:    [155, 200]
            │     tombstone: rev 0        ← still alive (0 = not yet tombstoned)

  COMPACT(C=180) effect:
    ─ Walk every keyIndex. For each generation:
        if tombstone <= C and tombstone != 0:
          drop the generation entirely (and all its bbolt entries)
        else if generation is open and some revs < C:
          keep at least the LATEST rev <= C (so reads at C work)
          drop earlier revs
    ─ After: generation[0] is fully gone (tombstone=102 <= 180).
             generation[1] retains rev 155 and 200 (155 is the
             latest <= 180; 200 is > 180 so retained too).

  RANGE QUERIES (kubectl get pods):
    ─ Walk the keyIndex btree in [startKey, endKey).
    ─ For each KeyIndex, find rev <= atRev.
    ─ Batch bbolt reads for the matching revisions.
    ─ Decode and stream to client.
```

### 4.5 Why Cluster-Global Revisions Make Sense Here

The decision to use one cluster-wide monotonic revision (rather than per-key versions) is a deliberate trade-off:

```
  CLUSTER-WIDE REVISION

  PRO:
    + Watch ordering across all keys is trivially defined.
      A controller can ask: "give me every event after rev X"
      and get exactly one totally-ordered stream.
    + List+Watch consistency: List at rev=X, then Watch from
      rev=X+1 gives gap-free, duplicate-free history.
    + Transactions are simple: all keys in a Txn share one
      main revision; sub orders them within the Txn.

  CON:
    - The revision counter is a single sequential resource;
      all writes coordinate through it. (But Raft already
      serializes all writes through one leader, so this is
      not an additional bottleneck.)
    - You cannot do per-key sharding by revision range —
      all keys live in the same revision sequence.

  PER-KEY VERSIONS (the alternative)
    + Concurrent writes to disjoint keys could parallelize.
      (Useless here: Raft serializes anyway.)
    - Watch ordering across keys is undefined; "after X for
      key A" doesn't compose with "after Y for key B".
    - List+Watch is hard: there is no single token X such
      that List@X + Watch from X is consistent.

  For a single-leader Raft store with cluster-wide watch,
  the global revision is the only sensible design.
```

---

## 5. Watch: The Nervous System

Every controller, every informer, every `kubectl get -w` is built on the etcd watch primitive. Understanding the etcd-side and apiserver-side caching that wrap it is the difference between "I deployed something and it's slow" and "I deployed something and it's costing my apiserver 12 cores".

### 5.1 The etcd Watch Protocol

Watches in etcd v3 are streaming gRPC over a single multiplexed connection. One client opens one `Watch` stream and creates many logical watchers on it.

```
  WATCH STREAM (gRPC bidirectional)

  client                                          etcd server
    │                                                 │
    │  Create(key=/registry/pods/, prefix=true,       │
    │         start_revision=8400)                    │
    │ ───────────────────────────────────────────────► │
    │                                                 │
    │  ◄───────────  CreateResponse(watch_id=42)  ─── │
    │                                                 │
    │  ◄───────────  Event(mod_rev=8400, key=...)  ── │
    │  ◄───────────  Event(mod_rev=8412, key=...)  ── │
    │  ◄───────────  Event(mod_rev=8430, key=...)  ── │
    │                                                 │
    │     ... happens forever, server-pushed ...      │
    │                                                 │
    │  Cancel(watch_id=42)                            │
    │ ───────────────────────────────────────────────► │
    │  ◄─────  CancelResponse(watch_id=42, reason)  ─ │
```

Server-side, each watcher is in one of two buckets:

```
  SERVER-SIDE WATCHER STATE  (mvcc/watcher_group.go)

   SYNCED          UNSYNCED
   ────────         ────────
   Caught up to     Behind by N revisions; replaying from
   current rev.     bbolt to catch up.
   Receives new     Once caught up → promoted to SYNCED.
   events as they
   happen.

   STATE TRANSITION:

      Create(start_rev=X) where X < currentRev
                          │
                          ▼
                       UNSYNCED  (scheduled in a separate goroutine
                          │       that range-scans bbolt from X to
                          │       currentRev, emitting events)
                          │
                          │ caught up
                          ▼
                       SYNCED

      Create(start_rev=X) where X >= currentRev
                          │
                          ▼  (skip directly; nothing to catch up)
                       SYNCED

  Why separate buckets?
    ─ SYNCED watchers receive events via a single broadcast loop
      on the write path: every committed mutation fans out to
      matching SYNCED watchers immediately, O(matching watchers).
    ─ UNSYNCED watchers are catching up asynchronously and must
      not block the write path. They each have their own goroutine
      pulling from bbolt.
    ─ When an UNSYNCED watcher catches up, the system carefully
      ensures the boundary event (the last replayed + the first
      live) has no gap and no duplicate.
```

### 5.2 Watch Event Coalescing — and the Important "It Doesn't"

A common misconception: "etcd coalesces multiple updates to the same key into one event." It does not, by default. Every mutation produces exactly one event with its mod_revision. What CAN coalesce is the SERIALIZATION of events on the network: if many events fire in a tight burst, etcd batches them into fewer protobuf messages on the wire (each `WatchResponse` may contain many events). But every distinct event still reaches the watcher.

The apiserver, on the other hand, DOES coalesce in its watch cache. The watch cache (covered in chapter 05 in depth) is an in-memory ring buffer of the last N watch events per resource type. When apiserver sends events out to its watcher clients, two events for the same key with the same resourceVersion are not generated (because there is only one), but the apiserver makes its own decisions about delivering all intermediate states or skipping. With server-side apply and conflict detection, the apiserver's behavior is "deliver every event in order"; nothing is dropped on the happy path.

### 5.3 Bookmarks and Progress Notifications

A "synced" watcher might receive no events for hours if no matching keys change. After a long quiet period, when the watcher reconnects after a network blip, it tries to resume from its last seen revision — but that revision might have been compacted, and the watcher receives `ErrCompacted` and has to do a full re-list. To prevent this, etcd supports two related features:

**Bookmark events** (`WithProgressNotify`, etcdctl `--prog-notify`). Periodically, the server sends a watch event marked `IsBookmark=true` with the current revision but no key data. The client can use this to advance its "last seen revision" forward even when no real changes happen, so a future reconnect uses a fresher revision that is less likely to be compacted.

**Watch progress notifications** (`Watcher.RequestProgress()`). The client can explicitly ask "what's the current revision on the server?" to update its bookmark on demand.

In Kubernetes, the apiserver uses both: it sets `WithProgressNotify` for its etcd watches, and the watch cache in turn sends `Bookmark` events to its own clients (controllers, kubelets). This is why a healthy informer's resourceVersion ticks forward even when nothing in its watched resource is changing.

### 5.3a The Watcher Group Internals

The synced/unsynced split, drawn:

```
  WATCHER GROUP STATE  (mvcc/watcher_group.go in etcd-io/etcd)

   write path
       │
       │  every committed mutation arrives here
       ▼
  ┌────────────────────────────────────┐
  │  SYNCED watchers (map)             │
  │  ─ indexed by watcher ID           │
  │  ─ also indexed by key+range       │
  │    via an in-memory interval tree  │
  │  ─ broadcast loop:                 │
  │      for each event:               │
  │        find matching watchers      │
  │        push event to each chan     │
  └────┬───────────────────────────────┘
       │
       │  if a watcher's channel is full
       │  (slow consumer), the watcher is
       │  KICKED to "victim" status:
       │  it falls back to UNSYNCED.
       ▼
  ┌────────────────────────────────────┐
  │  UNSYNCED watchers (map)           │
  │  ─ each has its own goroutine that │
  │    range-reads from bbolt:         │
  │      from (lastSentRev + 1) to     │
  │      currentRev                    │
  │  ─ sends events as it catches up   │
  │  ─ on catch-up, promotes back to   │
  │    SYNCED atomically (using the    │
  │    current rev as the boundary)    │
  └────────────────────────────────────┘

  STARVATION DEFENSE:
    An UNSYNCED watcher whose start_rev is older than
    compactRevision is immediately failed with ErrCompacted.
    It is NEVER allowed to consume the WAL or block compaction.

  BACKPRESSURE:
    Slow consumers eventually get kicked to UNSYNCED. If they
    can't catch up fast enough and compaction passes their
    start_rev, they get the dreaded ErrCompacted. This is
    deliberate: a slow consumer cannot stall the cluster's
    compaction or block other watchers.
```

The implication for Kubernetes operators is subtle: a controller that occasionally GC-pauses for several seconds may have its etcd watch (via apiserver) drop into unsynced, and if it pauses long enough during heavy write traffic, it can fall off the watch cache window and have to re-list. This is invisible most of the time but observable as periodic `apiserver_watch_events_sizes` spikes and informer `Reflector ListAndWatch` log lines.

### 5.4 Fragmented Events and Large Watch Responses

If a single key's value is large (a CRD with 1 MiB of status, an over-stuffed ConfigMap), and many such events fire in a short window, the resulting `WatchResponse` could exceed the gRPC message limit (default 1 MiB on etcd). etcd v3 can **fragment** a watch response: a single logical batch is sent as multiple `WatchResponse` messages with a `Fragment=true` field on all but the last. The client library reassembles them. This is mostly invisible, but it shows up in metrics: very high event throughput on a fragmented stream means very high message rates as well.

### 5.5 The "required revision has been compacted" Error

This is the most famous watch-related error in Kubernetes, and the one that operators most often see.

```
  THE FATAL ERROR (server-side)

    Client opens watch with start_revision = 8400
    Server's current compactRevision = 9500
    8400 < 9500 → ErrCompacted

  Server response:
    WatchResponse {
      created: false,
      canceled: true,
      compact_revision: 9500,
      cancel_reason: "etcdserver: mvcc: required revision has been compacted"
    }

  Client must:
    1. Discard the in-memory watch state.
    2. Do a fresh List with no resourceVersion (or = "0"), which
       returns the current state at the current revision.
    3. Open a new Watch starting from the resourceVersion in
       that List response.

  This is the "informer re-list" — expensive but bounded:
    O(objects of that type) re-fetch from apiserver,
    plus rebuild of the local informer cache.
```

In Kubernetes the apiserver's watch cache holds, by default, the most recent ~1000 events per resource (`--default-watch-cache-size`). If a client's watch falls more than 1000 events behind, the apiserver itself returns the "too old resource version" error to the client even before going to etcd. That is essentially the same error, surfaced one layer up.

The frequent culprit: a client that pauses (long GC pause, deep kernel sleep, network blip) while etcd is compacting aggressively. With a 5-minute compaction interval and high write rates, you can compact past a client's last revision in a few seconds. The defenses are: tune `--auto-compaction-retention` upward (keep more history), tune the apiserver's watch cache size upward, and design controllers to handle re-list gracefully.

### 5.6 The Double-Layer Watch: etcd + apiserver Watch Cache

The Kubernetes watch path is two layers of watch:

```
  THE TWO-LAYER WATCH

   etcd (rev=R)
     │  one watch per resource type, opened ONCE by apiserver
     │
     ▼
   kube-apiserver watch cache (per resource type)
     │  ring buffer of last N events, indexed by resourceVersion
     │  also holds a snapshot of the current state
     │
     ▼  fan-out: each apiserver watcher client gets a subset
     │
   ┌────────────────────────────────────────────────────────────┐
   │ scheduler · controller-mgr · kubelet · operators · etc.    │
   │ each runs informers, each opens a watch on apiserver       │
   └────────────────────────────────────────────────────────────┘

  Crucial property: N apiserver clients watching Pods do NOT
  produce N etcd watches. apiserver has ONE etcd watch per
  resource type, and fans out to N clients from its in-memory
  cache. This is the single most important scaling trick.

  Consequence: more clients => more apiserver memory + CPU,
  but NOT more etcd load. etcd load scales with mutations,
  not with watcher count.
```

This is why a 5000-node cluster (5000+ kubelets, each watching Pods + Nodes + ConfigMaps + Secrets, plus 50 controllers, plus operators) puts ~50 000 watch streams on the apiserver but the apiserver opens only ~30 watches against etcd (one per resource type). It is also why apiserver memory scales with the number of objects and the watch cache size, not with cluster headcount.

---

## 6. Leases: TTL-Based Expiration

A Lease is a server-side object with a TTL. Keys can be attached to a lease, and when the lease expires, all attached keys are deleted atomically. Clients keep leases alive by periodically calling `LeaseKeepAlive`.

```
  THE LEASE OBJECT (etcdserverpb.Lease)

    ID            uint64    // server-generated, monotonic
    TTL           int64     // seconds before expiration
    GrantedTTL    int64     // original TTL (TTL counts down)
    Keys          []bytes   // (server-side only) attached keys

  TYPICAL FLOW:

    1. LeaseGrant(TTL=10s) → Lease(ID=abc123, TTL=10)
    2. Put(key=/leader/lock, value=node-1, lease=abc123)
    3. Every 3 seconds: LeaseKeepAlive(ID=abc123)
                        → TTL reset to 10
    4. If client dies / can't reach etcd / process pauses:
       After 10s, lease expires → /leader/lock deleted
       → watchers see DELETE event → another node can claim
```

### 6.1 What Kubernetes Uses Leases For

```
  KUBERNETES LEASE USAGE

  Object                             What it stores                Lease TTL
  ─────────────────────────────────  ────────────────────────────  ─────────
  coordination.k8s.io/v1/Lease       Leader election object         15s typical
   (kube-controller-manager,          (renewed every ~10s)
    kube-scheduler, custom leaders)

  Node lease (Node.status heartbeat) Per-Node Lease object that     ~40s
                                     kubelet renews; replaces the
                                     old "patch Node.status every
                                     10s" approach (big load
                                     reduction at scale).

  Event TTL (Event objects)          1 hour default; etcd lease     1 hour
                                     handles auto-delete.

  ServiceAccount projected token     Token expiration tied to       1 hour - 24h
   audience+expiration                lease; rotation handled by
                                     the kubelet projection driver.
```

The Lease object in Kubernetes (`coordination.k8s.io/v1/Lease`) is NOT the same as the etcd lease. It is a Kubernetes object stored in etcd that contains fields like `holderIdentity`, `leaseDurationSeconds`, `renewTime`. The Kubernetes Lease is one of many uses of the etcd lease mechanism, but it is a higher-level abstraction. The leader-election library (k8s.io/client-go/tools/leaderelection) updates the renewTime field via apiserver PATCH; if a leader stops renewing, peers detect this by observing the stale renewTime, not by etcd lease expiration.

The Node lease (introduced in Kubernetes 1.13 as `NodeLease`) was a major scale fix. Before, every kubelet sent a full `Node.status` PATCH every 10 seconds (40 KiB writes × 5000 nodes × every 10s = 20 MB/s of etcd writes just for heartbeats). NodeLease replaces that with a tiny `Lease.spec.renewTime` PATCH (a few hundred bytes), dropping the heartbeat write volume by ~95%. Node status updates still happen but at a much slower cadence (every few minutes), driven by actual state change rather than the heartbeat.

### 6.2 Lease Implementation in etcd

Source: `server/lease/lessor.go`. The lessor maintains an in-memory priority queue (heap) of leases ordered by expiration time. A background goroutine wakes up periodically (default every 500ms) and pops expired leases.

When a lease expires:
1. The lessor proposes a Raft entry "revoke lease X".
2. On commit, the apply loop deletes every key attached to lease X.
3. Watchers see ordinary DELETE events for those keys.

Lease revocation is Raft-replicated (not just a local clock decision), so all members agree on the moment of expiration. The clock drift between members is bounded by the heartbeat interval — typically tens of ms — and any drift is reconciled at commit time. There is no "split-brain on lease expiration" hazard.

```bash
# See current leases
etcdctl lease list
# 6e9e05c52164694d
# 6e9e05c52164694e
# 6e9e05c521646950

etcdctl lease timetolive 6e9e05c52164694d --keys
# lease 6e9e05c52164694d granted with TTL(15s), remaining(8s),
#   attached keys([/kube-system/leases/kube-controller-manager])
```

### 6.3 Lease Limits

A single etcd cluster can hold tens of thousands of active leases without trouble, but each lease has a small fixed cost (the heap entry, the keepalive goroutine on the client side, the periodic Raft proposal on renewal). At ~100k leases the lessor's pop-loop and the Raft propose rate become a measurable fraction of CPU. In practice, NodeLease aggregation (one lease per node) keeps this well-bounded for ~10k-node clusters.

---

## 7. Transactions: Compare-Then-Do-Else

etcd's Txn primitive is the foundation of Kubernetes' optimistic concurrency. Every Create, Update, Delete on a Kubernetes object lands as a Txn against etcd.

### 7.1 The Txn gRPC Message

From `etcdserverpb/rpc.proto`:

```protobuf
message TxnRequest {
  repeated Compare       compare = 1;  // all must be true
  repeated RequestOp     success = 2;  // executed if compares all true
  repeated RequestOp     failure = 3;  // executed otherwise
}

message Compare {
  enum CompareResult { EQUAL = 0; GREATER = 1; LESS = 2; NOT_EQUAL = 3; }
  enum CompareTarget { VERSION = 0; CREATE = 1; MOD = 2; VALUE = 3; LEASE = 4; }
  CompareResult result = 1;
  CompareTarget target = 2;
  bytes key            = 3;
  oneof target_union {
    int64 version          = 4;  // per-key counter
    int64 create_revision  = 5;
    int64 mod_revision     = 6;
    bytes value            = 7;
    int64 lease            = 8;
  }
  bytes range_end = 64;  // optional: compare over a range
}

message RequestOp {
  oneof request {
    RequestRange       request_range       = 1;
    RequestPut         request_put         = 2;
    RequestDeleteRange request_delete_range = 3;
    TxnRequest         request_txn         = 4;  // nestable
  }
}
```

A Txn is **atomic, isolated, and serializable**. All compares evaluate against a single point-in-time snapshot of the keyspace (the moment the Txn is applied). If all compares are true, all success ops are applied as a single Raft entry (same main revision; sub = 0, 1, 2, …); otherwise all failure ops are applied. Either way, exactly one Raft commit happens.

### 7.2 Kubernetes Update as Txn

When the apiserver does `Update(pod)` with `pod.metadata.resourceVersion = 8472`, the storage layer issues this Txn:

```
  KUBERNETES UPDATE PATH

  apiserver internal:
    storage.GuaranteedUpdate(ctx, key="/registry/pods/default/nginx", obj=pod, ...)
       │
       ▼
    Read current object from etcd (rev=8472)
       │
       ▼
    Apply mutation (e.g., update from admission, default fields)
       │
       ▼
    Txn:
      compare: [
        { key="/registry/pods/default/nginx",
          target=MOD,
          result=EQUAL,
          mod_revision=8472 }
      ]
      success: [
        { type=PUT,
          key="/registry/pods/default/nginx",
          value=<protobuf-encoded updated pod> }
      ]
      failure: [
        { type=RANGE,
          key="/registry/pods/default/nginx" }
        # On compare failure: read the current value, return
        # it to the apiserver so it can either retry or return
        # a 409 Conflict to the caller.
      ]

  If compare passes: pod is updated; new revision = 8473.
  If compare fails: someone else updated the pod between
    the apiserver's read and write. The Txn returns the
    current state and apiserver retries (limited to a small
    number of attempts) or returns HTTP 409 Conflict.
```

This is the entire mechanism behind Kubernetes' "optimistic concurrency on resourceVersion". The apiserver is stateless about it — every concurrent control is in etcd's Txn engine.

### 7.3 Create as Txn

A Create checks that the object doesn't already exist:

```
  Txn:
    compare:
      - { key="/registry/pods/default/nginx",
          target=CREATE_REVISION, result=EQUAL, create_revision=0 }
        # create_revision=0 means "this key has never existed"
        # (or has been fully tombstoned and compacted away)
    success:
      - { type=PUT, key="/registry/pods/default/nginx",
          value=<pod>, lease=0 }
    failure:
      - { type=RANGE, key="/registry/pods/default/nginx" }
```

If the compare fails (key exists), the apiserver returns HTTP 409 AlreadyExists.

### 7.4 Delete with Precondition

Delete with `--field-selector` or `--resource-version=X`:

```
  Txn:
    compare:
      - { key="/registry/pods/default/nginx",
          target=MOD, result=EQUAL, mod_revision=8472 }
    success:
      - { type=DELETE_RANGE, key="/registry/pods/default/nginx" }
    failure:
      - { type=RANGE, key="/registry/pods/default/nginx" }
```

### 7.5 Txn Limits

By default, etcd limits a single Txn to:
- 128 operations total
- 128 compares
- These limits are configurable via `--max-txn-ops`

Kubernetes hits these limits rarely, but Server-Side Apply with deeply nested objects, or large garbage-collection cascades, can approach them. The limits exist because a single Txn becomes a single Raft entry, and unbounded Txn size could create unbounded Raft log entries that exhaust memory.

---

## 8. Compaction: Reclaiming Revisions

Recall §4: every mutation adds a new revision; deletes add tombstones. Without compaction, the keyValue store grows monotonically forever. Compaction is the process of declaring a revision threshold `compactRev` below which all historical data is dropped.

### 8.1 What Compaction Actually Does

```
  COMPACTION at revision C

  For each key K in the keyIndex:
    For each (revision, value) pair in K's history:
      If revision < C AND there exists a LATER revision for K
                          OR K is tombstoned at some rev <= C:
        → remove this (revision, value) pair from keyIndex
        → remove the bbolt entry encode(revision, sub) for K
    Adjust K's keyIndex; if K is now empty (tombstoned and
    nothing left), remove K entirely.

  Update compactRevision = C in the "meta" bucket.

  CONSEQUENCES:
    ─ Watches with start_revision < C now fail with ErrCompacted.
    ─ Reads with revision < C also fail with ErrCompacted.
    ─ Reads with revision >= C, or "current", work normally.
    ─ The latest version of each key is ALWAYS retained, even
      if it predates C, because every key needs at least one
      live revision to be queryable.

  IMPORTANT: Compaction frees LOGICAL pages in bbolt, but bbolt
  does NOT return them to the filesystem. The file stays the
  same size. The pages just go on the freelist for reuse by
  future writes. To physically shrink the file, you must
  defragment (§9).
```

### 8.2 Auto-Compaction Policies

`--auto-compaction-mode` and `--auto-compaction-retention`:

```
  MODE = "periodic"  (default)
    Retention is a time duration (e.g., "5m", "1h", "8h").
    Every cycle (default: retention/10), etcd compacts to
    the revision that was current at "now - retention".

  MODE = "revision"
    Retention is a number of revisions (e.g., 1000000).
    Every cycle (default: retention/10), etcd compacts to
    (currentRev - retention).

  DEFAULT in etcd alone:    auto-compaction-mode unset (no compaction).
  DEFAULT in kubeadm:       --auto-compaction-mode=periodic
                            --auto-compaction-retention=5m
  Managed K8s (EKS/GKE/AKS): typically 8h or 24h; tuned per provider.
```

```bash
# Manual compaction (rare; auto-compaction is the right answer):
etcdctl compact 12345678
# compacted revision 12345678
```

### 8.3 Why Frequent Compaction Is Necessary

Two reasons compaction matters:

1. **Storage size.** Every Lease renewal, every Node lease ping, every Event creation adds a revision. At 10 000 writes/s × 86 400 s/day = 864 million revisions per day. At an average ~500 bytes per keyValue, that's ~430 GiB/day of accumulated data. Without compaction the bbolt file blows past the 2 GiB default quota in under 7 minutes.

2. **B+tree depth.** bbolt's B+tree contains an entry for every (revision, key) pair. As the number of entries grows, the tree gets deeper, and individual lookups get slower. Compaction is what keeps the tree at a reasonable depth and ensures the working set fits in OS page cache.

The dominant operational mistake here is leaving auto-compaction off (the etcd default if not configured) or setting retention too long. A cluster with `--auto-compaction-retention=24h` and a high-update workload (frequent Lease renewals, many Pods cycling) can accumulate hundreds of GiB of revisions over a day. The bbolt file grows, write latency creeps up, and eventually the cluster trips the `--quota-backend-bytes` ceiling and goes into a no-space-left alarm mode (writes refused until manual `etcdctl alarm disarm`).

### 8.4 The `etcd_mvcc_db_total_size_in_bytes` vs `..._in_use_bytes` Pair

These two metrics tell the compaction-vs-defrag story:

```
  etcd_mvcc_db_total_size_in_bytes         on-disk file size of bbolt's db
  etcd_mvcc_db_total_size_in_use_bytes     logical bytes used (excluding free)

  Compaction reduces in_use.
  Defragmentation reduces total to match in_use.

  HEALTHY RATIO: total / in_use ≈ 1.0 - 1.3
  TIME TO DEFRAG: total / in_use > 1.5

  EXAMPLE EVOLUTION:

    State          total      in_use     Ratio
    ────────────────────────────────────────────
    Fresh cluster  10 MB      8 MB       1.25
    1 hour, no compact  500 MB     500 MB     1.00  ← growing equally
    1 day, compaction on   500 MB     200 MB     2.50  ← compaction did
                                                       its job, defrag pending
    After defrag      210 MB     200 MB     1.05  ← back to healthy
```

---

## 9. Defragmentation: Reclaiming Disk

Compaction frees pages inside bbolt's freelist. Defragmentation walks the live data, copies it into a new contiguous bbolt file, replaces the old file atomically, and returns the freed space to the filesystem.

### 9.1 What Defrag Does Internally

```
  DEFRAG FLOW (server/storage/backend/backend.go)

  1. Pause writes to the backend (acquire exclusive lock).

  2. Open a new bbolt file (db.tmp) in the data directory.

  3. Walk every live bucket and every live key/value in the
     current db, copy each into db.tmp in sequential order.
     This produces a maximally-packed B+tree with no holes.

  4. fsync db.tmp.

  5. Atomically rename db.tmp over db (POSIX rename(2)
     guarantees atomicity at the directory level).

  6. Reopen the backend pointing at the new db.

  7. Release the lock; writes resume.

  DURATION: depends on live data size. For a 2 GiB in_use db,
  expect 10-60 seconds on NVMe, 1-5 minutes on networked SSDs,
  and "don't" on spinning disks.

  IMPACT: this is a STOP-THE-WORLD operation for the member.
  Reads can proceed from other members; writes go through if
  the defragging member is a follower (the leader serves them).
  But the defragging member cannot vote on commits during this
  window. If it is the leader, the cluster loses its leader
  for the duration of the defrag — UNAVAILABLE for writes.
```

### 9.2 The Operational Recipe

```
  SAFE DEFRAG PROCEDURE (3-member cluster)

  Step 1: Check the cluster is healthy.
    etcdctl endpoint health
    etcdctl endpoint status -w table
    All members: healthy, similar DB sizes, one leader.

  Step 2: Identify the current leader.
    etcdctl endpoint status -w table | awk -F'|' '$NF~/true/ {print $2}'

  Step 3: Defrag the followers FIRST, one at a time.
    For each follower endpoint:
      etcdctl --endpoints=<follower> defrag
      # Watch the metric etcd_disk_backend_commit_duration_seconds
      # and etcd_server_proposals_committed_total to confirm the
      # rest of the cluster is still committing writes.

  Step 4: Move leadership AWAY from the would-be-defragged member,
          THEN defrag what is now a follower.
    etcdctl move-leader <some-other-member-id>
    # Wait a few seconds for the move to settle.
    etcdctl --endpoints=<former-leader> defrag

  Step 5: Verify.
    etcdctl endpoint status -w table
    # DB SIZE should now reflect the on-disk shrinkage on
    # all three members.
```

```bash
# DON'T do this:
etcdctl defrag --cluster
# (defrags all members in parallel — guaranteed write outage
#  while the leader defrags)
```

### 9.3 Why Defrag Is the Most Common Cause of etcd Outages

The pattern that takes clusters down:

```
  THE FAMOUS OUTAGE TIMELINE

  T-2 weeks   Cluster nominal. quota-backend-bytes=2GiB.
              auto-compaction-mode=periodic, retention=5m.

  T-1 week    Application starts using etcd-via-Kubernetes-CR for
              high-frequency state (a misuse pattern: see §16).
              Update rate climbs. Compaction working, in_use stays
              flat. total_size climbs slowly.

  T-1 day     total_size at 1.7 GiB; in_use 600 MiB. Ratio 2.8.
              No defrag has been scheduled. Alerts not configured.

  T-0 hour 0  total_size hits 2 GiB. etcd raises NOSPACE alarm.
              All writes refused. Apiserver returns 500. Cluster
              is read-only.

  T-0 hour 0  Operator notices, runs:
                etcdctl alarm disarm                # clears alarm
                etcdctl defrag --cluster            # WRONG
              All members defrag simultaneously. Leader unavailable
              for ~30s; both followers also unavailable for ~30s.
              Cluster has no quorum. Writes still refused, plus
              now reads from the defragging members fail too.

  T-0 hour 0  After ~1 minute, members finish defragging, return.
              Cluster recovers. total_size now matches in_use.
              Writes resume.

  WHAT WENT WRONG:
    1. No alarm on total_size growth.
    2. No defrag schedule.
    3. defrag --cluster instead of one at a time + move-leader.

  WHAT THE PLAYBOOK SHOULD HAVE BEEN:
    1. Alert on db total / in_use > 1.5 OR
       db total > 0.75 × quota-backend-bytes.
    2. Scheduled weekly defrag, rolling, automated.
    3. NEVER use --cluster on production.
```

The combination of "quota tripped" + "panicked operator running --cluster defrag" has been called the most common etcd self-inflicted outage. It's preventable with a few CronJobs and dashboard alarms.

### 9.4 Compaction + Defrag Timeline Visualized

```
  ON-DISK SIZE OVER TIME (in_use vs total bytes)

   bytes
     ▲
  3G ┤
     │                                                    ░░░░░░
  2G ┤ ───────────────── quota-backend-bytes ─────────────░░░░░░
     │                                            ░░░░░░░░     ▼ NOSPACE alarm
     │                                     ░░░░░░░       ┌──────────┐
 1.5G┤                              ░░░░░░░              │  defrag  │
     │                       ░░░░░░░                     │ rolling  │
     │                ░░░░░░░ ░░░░░░░░░░░░░░░░░░░░░░░░░░ │ all 3    │
   1G┤         ░░░░░░░                                   │ members  │
     │   ▲                                               └──────────┘
     │   │ in_use stays                                       ▲
     │   │ flat (compaction                                   │
 500M┤   │ doing its job)                                  drops back
     │   │                                               to ~in_use
   0 ┼───┼────────────────────────────────────────────────────────────►
       T₀     T₁              T₂                T₃             time
       cluster started        compaction enabled,             defrag run:
       writes begin           in_use stabilizes,              total → in_use
                              total drifts up

   in_use:  ░░░░░░░░░░░░░░░  ← logical data size
   total :  ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔  ← bbolt file size on disk

   THE PATTERN TO RECOGNIZE:
     in_use flat + total slowly climbing → compaction OK, defrag DUE
     in_use climbing                     → compaction off or lagging
     in_use ≈ total ≈ flat               → healthy steady state
     in_use ≈ total climbing             → workload growing, plan capacity
```

The dashboard you want is two stacked timeseries (`db_total_size`, `db_total_size_in_use`) on the same panel, with a horizontal line at `quota-backend-bytes`. The gap between the two lines is "defrag debt". The distance from `total` to the quota line is "time until outage if you do nothing".

---

## 10. Backup and Restore

etcd backup is a single command. Restore is a single command. The subtlety is what they actually do.

### 10.1 Taking a Snapshot

```bash
# Take a snapshot from any LIVE member of the cluster.
etcdctl --endpoints=https://10.0.0.1:2379 snapshot save /backup/etcd-snap-$(date +%F).db
# Snapshot saved at /backup/etcd-snap-2026-05-23.db

# Confirm it
etcdctl snapshot status /backup/etcd-snap-2026-05-23.db -w table
# +----------+----------+------------+------------+
# |   HASH   | REVISION | TOTAL KEYS | TOTAL SIZE |
# +----------+----------+------------+------------+
# | 4c4f3e2a | 12345678 |     94521  |   187 MB   |
# +----------+----------+------------+------------+
```

The snapshot file is a copy of the bbolt db file plus integrity metadata. It does NOT include the WAL. The snapshot reflects the state at the moment the snapshot transaction began (a point-in-time consistent view, courtesy of bbolt's MVCC inside a read transaction).

Snapshots can be taken without affecting cluster availability. They are I/O-heavy (one full bbolt copy), so they spike disk read traffic; schedule them during quiet periods or from a dedicated follower.

### 10.2 Restoring from Snapshot

```bash
# 1. STOP every etcd member. Yes, every one.
systemctl stop etcd  # on every member

# 2. On each member, run:
etcdctl snapshot restore /backup/etcd-snap-2026-05-23.db \
  --name infra0 \
  --initial-cluster=infra0=https://10.0.0.1:2380,infra1=https://10.0.0.2:2380,infra2=https://10.0.0.3:2380 \
  --initial-cluster-token=etcd-cluster-RESTORED \
  --initial-advertise-peer-urls=https://10.0.0.1:2380 \
  --data-dir=/var/lib/etcd-restored

# 3. Reconfigure etcd to use the new data-dir, start.
systemctl start etcd

# 4. Cluster is now alive with the snapshot's contents.
```

Critically, **`snapshot restore` creates a NEW CLUSTER**. The cluster gets a fresh cluster-id (different from the original). New member-ids are generated. Any external system tracking the old cluster-id (monitoring, alerting, the apiserver's `etcd-cafile` references) sees a new cluster and may need reconfiguration.

```
  WHAT RESTORE DOES UNDER THE HOOD

  1. Verify snapshot integrity (hash + bbolt sanity).
  2. Generate a new cluster-id (random uint64).
  3. For each member name in --initial-cluster, generate a
     new member-id.
  4. Initialize a fresh WAL containing exactly one config-change
     entry: "this is the new cluster, with these members".
  5. Copy the snapshot's bbolt file into the data-dir as db.
  6. Write a Raft snapshot pointing at the new cluster config.

  CONSEQUENCES:
    ─ The new cluster's first Raft index might overlap with
      old indices — but it's a new cluster-id, so no client
      will be confused.
    ─ Watches that were open before the disaster receive
      stream errors and must re-list/re-watch.
    ─ Lease IDs change (leases stored in the snapshot retain
      their IDs, but any in-flight LeaseKeepAlive RPCs from
      old client sessions fail and clients must re-grant).
```

The `--initial-cluster-token` should be DIFFERENT from the original cluster's token. This prevents accidental cross-pollination: if an old member somehow joined the new cluster, the token mismatch would cause it to refuse.

### 10.3 What to Back Up Beyond etcd

```
  THE FULL CONTROL-PLANE BACKUP PICTURE

  ─ etcd snapshot                    ← what we just did
  ─ /etc/kubernetes/pki/             ← apiserver + etcd certs
                                       (without these, you can't
                                        even connect to the
                                        restored etcd)
  ─ /etc/kubernetes/manifests/       ← static-pod manifests
                                       (if using kubeadm)
  ─ Encryption-at-rest keys          ← if you have configured
                                       encryption providers for
                                       Secrets, the snapshot's
                                       Secrets are encrypted and
                                       unreadable without the key.

  RPO (recovery point objective):
    snapshot frequency → typical: every 30 min to 1 hour
  RTO (recovery time objective):
    snapshot restore time + cluster reformation time
    → typical: 10-30 minutes for a fresh control plane.
```

For DR (cross-region/cross-cloud), schedule snapshots via a CronJob, ship them out-of-cluster (object store like S3 with versioning), and periodically TEST the restore on a sandbox cluster. A backup you've never restored is not a backup; it's a wish.

---

## 10a. Encryption at Rest

Strictly speaking this is an apiserver concern, not etcd's, but the consequences land on etcd and the operational interactions matter.

```
  ENCRYPTION AT REST (apiserver feature, not etcd)

  Configured via --encryption-provider-config on the apiserver:

    apiVersion: apiserver.config.k8s.io/v1
    kind: EncryptionConfiguration
    resources:
      - resources:
          - secrets
          - configmaps
        providers:
          - aescbc:                    ← active write provider
              keys:
                - name: key1
                  secret: <base64 32-byte key>
          - identity: {}               ← fallback for old objects

  How it works:
    ─ apiserver encrypts each value before writing to etcd.
    ─ On read, it tries providers in order until one decrypts.
    ─ "identity" means plaintext; placing it LAST lets you
      gradually rotate from plaintext to encrypted.

  CONSEQUENCES FOR ETCD:
    ─ Values in bbolt are ciphertext; etcdctl get returns
      bytes that you cannot decode without the key.
    ─ Backups (snapshots) contain ciphertext. The encryption
      key is NOT in the backup; you must back it up separately
      (and store it separately, ideally in a KMS).
    ─ Restore from snapshot requires having the corresponding
      key configuration on the new apiserver.
    ─ Compaction and watch are unaffected — encryption is
      transparent at the etcd layer.

  KMS PROVIDER (kms-v2):
    ─ Same encryption-at-rest mechanism, but the actual data
      key is wrapped by an external KMS (cloud KMS, HSM).
    ─ Apiserver calls the KMS plugin's Encrypt/Decrypt over
      a Unix socket.
    ─ Higher security (key never on disk), higher latency
      (KMS RPC per object on cache miss).
```

The pitfall: losing the encryption key is identical to losing the data. A snapshot you can no longer decrypt is just bytes. Treat the key with the same care as the snapshot itself; ideally rotate periodically (the encryption config supports key rotation via re-keying: add a new key as the first provider, re-write every object once, then remove the old key).

---

## 11. Performance Tuning

The handful of knobs that actually matter. Most of them are interrelated; tuning one in isolation usually creates a new bottleneck elsewhere.

### 11.1 Heartbeat and Election Timeouts

```
  --heartbeat-interval         (default: 100ms)
  --election-timeout           (default: 1000ms)

  RULES:
    election-timeout >= 5 × heartbeat-interval
    election-timeout >= 10 × max network RTT between members
    heartbeat-interval >= 1.5 × max network RTT

  COMMON SETTINGS:

    Same AZ, healthy network (<5 ms RTT):
      heartbeat 100ms, election 1000ms (defaults)

    Multi-AZ, same region (5-10 ms RTT):
      heartbeat 200ms, election 2000ms

    Cross-region (50+ ms RTT):
      heartbeat 500ms, election 5000ms
      (and: don't do this. multi-region etcd has bad RPO
       trade-offs; prefer per-region clusters + federated
       or DR-style replication at the Kubernetes layer.)

  IMPACT:
    Lower heartbeat = faster failure detection, more network
                      packets per second (negligible on modern
                      networks).
    Higher election = longer outage on a real leader failure;
                      fewer false positives from transient
                      slowness.
```

### 11.2 Snapshot Count

```
  --snapshot-count             (default: 100000 entries)

  WHAT IT CONTROLS:
    How often etcd creates a Raft snapshot (state checkpoint).
    Smaller = more frequent snapshots = more snapshot I/O,
              less memory used to hold uncompacted log,
              faster restart (less WAL to replay).
    Larger  = less frequent snapshots, longer restart times,
              more memory used by the in-memory raft log.

  WHEN TO CHANGE:
    Production high-write clusters often raise this to
    300000-500000 to reduce snapshot I/O frequency.
    Most clusters: leave at default.
```

### 11.3 Quota

```
  --quota-backend-bytes        (default: 2GiB = 2*1024*1024*1024)

  HARD CEILING on the bbolt file's logical size. When the
  in_use bytes exceed this, etcd raises a NOSPACE alarm
  and refuses writes until:
    1. Alarm is disarmed: etcdctl alarm disarm
    2. Compaction reclaims space (or defrag runs).

  PRACTICAL VALUES:
    Small cluster (< 500 nodes):        2 GiB (default)
    Medium cluster (500-2000 nodes):    4 GiB
    Large cluster (2000-5000 nodes):    8 GiB
    XL cluster (5000+ nodes):           8-16 GiB
    Reference: GKE/AKS default:         6-8 GiB.

  CONSTRAINT: ABSOLUTE max is 8 GiB. Past that, bbolt
  performance degrades sharply (B+tree depth, mmap pressure)
  and you should be sharding workloads across multiple
  clusters instead.
```

### 11.4 Auto-Compaction

```
  --auto-compaction-mode       (default: periodic if kubeadm,
                                else unset)
  --auto-compaction-retention  (default: 0 = disabled)

  RECOMMENDATIONS:
    periodic, 5m         small clusters, default
    periodic, 1h         clusters with rare changes
    revision, 1000000    clusters with very high write rates
                         (use revision-based to bound by
                          load, not time)

  DON'T disable compaction. Ever. There is no scenario where
  unbounded growth is desirable.
```

### 11.5 Backend Batch Limits

```
  --backend-batch-limit        (default: 10000)
  --backend-batch-interval     (default: 100ms)

  Controls how many ops the apply loop coalesces into a single
  bbolt transaction. Larger batches = higher throughput, but
  longer apply latency (events arrive later at watchers).

  Tune only after you've maxed everything else. Rarely necessary.
```

### 11.6 Max Request Size

```
  --max-request-bytes          (default: 1.5 MiB)

  Maximum size of a single client request (i.e., a single
  Put or Txn). Apiserver's per-object limit (1 MiB) is set
  to be inside this. If you raise it (for huge CRDs), also
  raise the apiserver's --max-request-bytes.

  STRONG ADVICE: don't raise this. Large objects break
  the assumption that watch events fit in a single gRPC
  message and stress every layer. Fix the application.
```

### 11.7 Disk: The Single Biggest Factor

Nothing in the above tuning matters if the disk is slow. etcd's write path is:
1. Append to WAL → fsync.
2. Apply to bbolt → eventually fsync.

Both fsyncs hit the disk. The fsync latency directly determines write throughput.

```
  DISK GUIDANCE

   GOOD                         BAD
   ─────                         ─────
   Local NVMe SSD                Networked block storage with high latency
   Dedicated disk for etcd       Shared disk with other workloads
                                 ("noisy neighbors" stall fsyncs)
   WAL on its own disk           WAL + db on the same disk under load
   (--wal-dir=/etcd/wal,
    --data-dir=/etcd/data)

   Avoid:
     ─ AWS gp2/gp3 with iops below 3000 (you will hit burst limits)
     ─ Azure Premium SSDs with cached read mode (caching breaks fsync semantics)
     ─ Spinning HDDs (every fsync = 10+ ms; you will not survive)
     ─ Network-mounted filesystems (NFS, EFS): fsync semantics
       are weaker, and latency variability kills Raft.

  TARGET: p99 fsync latency < 10 ms.
  ALARM THRESHOLD: p99 > 50 ms sustained.
```

This is why CPU is rarely the bottleneck for etcd. The leader does serialize all writes through one core (Raft proposal + serialization), but at 10 000 writes/s on a modern CPU, that core is ~30% utilized. Disk fsync, network RTT between members, and bbolt commit are the dominant costs.

---

## 12. Operational Topology

### 12.1 The Quorum Math

```
  CLUSTER SIZE  | QUORUM | FAULT TOLERANCE | WRITE LATENCY
  ─────────────────────────────────────────────────────────
  1 member       | 1      | 0 failures       | 1 fsync
                                              (no replication)
  3 members      | 2      | 1 failure        | 1 fsync + 1 net RTT
  5 members      | 3      | 2 failures       | 1 fsync + 1 net RTT
                                              (to 2nd fastest)
  7 members      | 4      | 3 failures       | 1 fsync + 1 net RTT
                                              (to 3rd fastest)
  9 members      | 5      | 4 failures       | 1 fsync + 1 net RTT
                                              (to 4th fastest)

  EVEN NUMBERS: never use them.
    2 members: quorum = 2 → ANY failure = no quorum. Strictly
               worse than 1 member.
    4 members: quorum = 3 → tolerates 1 failure (same as 3-member)
               but uses 33% more network and disk on every write.
    6 members: quorum = 4 → tolerates 2 failures (same as 5-member)
               but uses more resources.

  WHY MORE MEMBERS ≠ FASTER:
    Writes commit when MAJORITY ack. A 5-node cluster commits
    when 3 nodes ack. So write latency = max(local fsync,
    network RTT to median ack). More members can mean a slower
    median if any are remote.
```

The classic choices:
- **3 members**: most production K8s. Survives any single failure. Fits in one AZ trivially or spans 3 AZs in a region.
- **5 members**: large clusters or strict availability requirements. Survives 2 failures (e.g., an AZ outage + a member failure during recovery).
- **7+ members**: only seen in very large multi-region setups, and even then questionable. Diminishing returns plus higher write latency.

### 12.2 Failure Domain Placement

```
  3-MEMBER CLUSTER: ALWAYS spread across 3 failure domains.

  GOOD (3-AZ region):
    AZ-a: 1 member
    AZ-b: 1 member
    AZ-c: 1 member
    → Any 1 AZ failure: cluster keeps quorum.

  BAD:
    AZ-a: 2 members
    AZ-b: 1 member
    → If AZ-a fails: 2 members down at once, no quorum.
       Cluster is down until AZ-a recovers.

  SAME LOGIC for racks, host machines, hypervisor hosts.
  Never co-locate two etcd members on the same physical host
  or network switch.

  5-MEMBER CLUSTER, 3 AZs (common):
    AZ-a: 2 members
    AZ-b: 2 members
    AZ-c: 1 member
    → Any 1 AZ failure: 3 members remain → quorum.
    → AZ-a OR AZ-b failure + 1 other failure: still 3 → quorum.
    → AZ-a AND AZ-b failure together: 1 remaining → no quorum.
       (Multi-AZ correlated failure is the residual risk.)

  RTT BUDGET:
    Within an AZ: typically <1 ms RTT.
    Cross-AZ within region: 1-5 ms RTT.
    KEEP RTT ≤ 8 ms across all member pairs.
    Higher RTT = longer write latency, more election timeouts.
```

### 12.3 Stacked vs External etcd

```
  STACKED  (kubeadm default)
  ─────────
  Same node runs: kube-apiserver + etcd + scheduler + controller-manager
  Pros: simpler topology, one node = one "control plane unit"
        kube-apiserver talks to LOCAL etcd over loopback (no TLS overhead
        if 127.0.0.1 in noCAFile mode; usually still TLS for uniformity)
  Cons: a node loss = loss of one apiserver AND one etcd at once;
        resource contention between apiserver and etcd on the same host

  Common for: small/medium clusters (< 1000 nodes), kubeadm/managed
              K8s offerings, single-team clusters.

  EXTERNAL  (3+ dedicated etcd VMs, apiservers separate)
  ─────────
  etcd cluster: 3 or 5 nodes, ONLY etcd, dedicated hardware.
  apiservers: separate fleet (often horizontally scaled, behind LB).

  Pros: independent scaling of apiserver vs etcd;
        better failure isolation;
        easier to give etcd the dedicated NVMe it needs.
  Cons: more nodes to manage; cross-node TLS for apiserver→etcd;
        operational complexity.

  Common for: large clusters (> 1000 nodes), regulated environments,
              shops that already operate etcd separately.
```

### 12.4 Sizing

```
  REFERENCE SIZING FOR ETCD MEMBERS

  Small (< 500 nodes):
    CPU: 2 cores, Memory: 4 GiB, Disk: NVMe SSD, 50 GiB,
    Network: 1 Gbps

  Medium (500-2000 nodes):
    CPU: 4 cores, Memory: 8 GiB, Disk: NVMe SSD, 100 GiB,
    Network: 1 Gbps

  Large (2000-5000 nodes):
    CPU: 8 cores, Memory: 16 GiB, Disk: NVMe SSD, 200 GiB,
    Network: 10 Gbps recommended

  XL (5000+ nodes):
    CPU: 16 cores, Memory: 32-64 GiB, Disk: enterprise NVMe,
    dedicated WAL disk, 500 GiB+, Network: 10 Gbps

  Bottleneck order:
    1. Disk fsync latency           ← almost always
    2. Network RTT between members  ← occasionally
    3. Apiserver write rate         ← rare
    4. CPU                          ← almost never
```

---

## 13. The apiserver ↔ etcd Relationship

This section ties etcd internals back to the Kubernetes objects that drive every operator's daily life.

### 13.1 The Storage Layout

The apiserver stores every object under a single, predictable key prefix:

```
  ETCD KEY LAYOUT (from kubernetes/staging/src/k8s.io/apiserver/pkg/
                   storage/etcd3/store.go and friends)

  /registry/<resource>/<namespace>/<name>     (namespaced resources)
  /registry/<resource>/<name>                 (cluster-scoped resources)

  Examples:
    /registry/pods/default/nginx-deployment-6c5b54f95-xz7vp
    /registry/services/kube-system/kube-dns
    /registry/namespaces/default
    /registry/clusterroles/cluster-admin
    /registry/leases/kube-system/kube-controller-manager
    /registry/events.k8s.io/default/nginx.17b3c2d4e5f6...
    /registry/<crd-group>/<crd-resource>/<namespace>/<name>

  PREFIX is configurable via --etcd-prefix (default "/registry").
  Multi-tenant scenarios sometimes use a custom prefix per
  cluster sharing a single etcd, but this is uncommon.
```

```bash
# Get a Pod directly from etcd, as the apiserver sees it.
etcdctl get /registry/pods/default/nginx-deployment-6c5b54f95-xz7vp \
  --print-value-only | hexdump -C | head -5
# 00000000  6b 38 73 00 0a 0c 0a 02  76 31 12 03 50 6f 64 12  |k8s.....v1..Pod.|
# 00000010  ...

# The "k8s\0" prefix is the protobuf storage prefix; what follows is
# a protobuf-encoded runtime.Object (specifically a Pod in apps/v1).

# Get the same Pod with structured decoding using auger
# (https://github.com/etcd-io/auger):
auger decode < <(etcdctl get /registry/pods/default/nginx-deployment-6c5b54f95-xz7vp --print-value-only)
# Returns the Pod object as YAML.
```

### 13.2 Protobuf vs JSON in Storage

The apiserver stores objects in **protobuf** (not JSON) into etcd. This is one of the most important performance optimizations:

```
  STORAGE ENCODING

  ─ Kubernetes API surface to clients: JSON or protobuf (clients choose
                                       via Accept/Content-Type).
  ─ Watch over apiserver:              protobuf preferred (smaller).
  ─ Apiserver → etcd:                  always protobuf.
                                       (Configured via --storage-media-type;
                                        only legacy installs use JSON.)

  WHY:
    ─ Protobuf is 2-5x smaller than equivalent JSON for typed objects.
    ─ Protobuf encode/decode is 5-10x faster than JSON.
    ─ etcd stores VALUES as opaque bytes; protobuf-in / protobuf-out
      means no per-write JSON serialization on the apiserver hot path.

  SOURCE: kubernetes/staging/src/k8s.io/apimachinery/pkg/runtime/serializer/
          and the Resource configuration in
          kubernetes/staging/src/k8s.io/apiserver/pkg/registry/generic/
          registry/store.go
```

CRDs are the exception: they are stored as their as-submitted JSON (or "Structural" subset), not as protobuf, because the apiserver doesn't have generated protobuf marshalers for arbitrary CRD types. This is one reason large CRDs (e.g., 100+ KiB Spec/Status) are disproportionately expensive in etcd.

### 13.3 The Update CAS Path End-to-End

```
  PUT /api/v1/namespaces/default/pods/nginx (an UPDATE)

  ┌─────────────────────────────────────────────────────────────┐
  │ kube-apiserver                                              │
  │                                                             │
  │  1. Decode incoming JSON/protobuf → in-memory Pod object.   │
  │  2. AuthN, AuthZ, mutating admission, validation,           │
  │     validating admission. (ch 06, 07)                       │
  │  3. storage.Update(ctx, key, obj, ...)                      │
  │       │                                                     │
  │       │  GuaranteedUpdate(): "read - modify - write loop"   │
  │       │  with up to N retries on Txn failure.               │
  │       │                                                     │
  │       │  a. Get current value at key; decode protobuf.      │
  │       │  b. Call user's tryUpdate function (apply changes,  │
  │       │     compute resource version, defaults, etc.).      │
  │       │  c. Build Txn:                                      │
  │       │     compare key MOD == fromRev,                     │
  │       │     success: PUT key = newValue,                    │
  │       │     failure: RANGE key (refetch).                   │
  │       │  d. Submit Txn to etcd.                             │
  │       │  e. If Txn.succeeded → return new object.           │
  │       │     If Txn.failed → use returned value, loop to b.  │
  │       │     If retries exhausted → return error.            │
  │       ▼                                                     │
  └─────────────────────────────────────────────────────────────┘
                            │
                            ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ etcd                                                        │
  │                                                             │
  │  Txn arrives at leader.                                     │
  │  Apply compares against current bbolt state.                │
  │  Generate Raft entry. Append to WAL. Replicate.             │
  │  On commit: apply loop:                                     │
  │    ─ MVCC PUT: add new revision for key.                    │
  │    ─ Increment global revision.                             │
  │    ─ Fan out to SYNCED watchers.                            │
  │  Return TxnResponse with succeeded=true and new revision.   │
  └─────────────────────────────────────────────────────────────┘
                            │
                            ▼
                  apiserver returns 200 OK + new object
                            │
                            ▼
                  watch fan-out to all interested clients
                  (each apiserver instance's watch cache
                   is the next hop)
```

The fact that this entire pipeline takes < 50 ms p50 in a healthy cluster, including the Raft commit, is the foundational performance property that makes Kubernetes feel "fast".

### 13.4 The Watch Cache: apiserver In-Memory Mirror

```
  APISERVER WATCH CACHE (per resource type)

      etcd
        │ one watch per (resource type), opened at apiserver startup
        ▼
   ┌────────────────────────────────────────────────────────────┐
   │ cacher (k8s.io/apiserver/pkg/storage/cacher)               │
   │                                                            │
   │  ─ ring buffer of last N watch events                      │
   │    (default N = 100 or auto-tuned per resource)            │
   │  ─ btree/store: in-memory copy of all current objects      │
   │    for the resource (indexed by namespace + name)          │
   │  ─ goroutines:                                             │
   │      reflector → consume etcd watch, update store + ring   │
   │      dispatcher → fan out events to each client watcher    │
   │                                                            │
   └────────────────────────────────────────────────────────────┘
        │
        │ N client watchers (controllers, kubelets, ...)
        │
        ▼
   each receives a stream of events filtered by:
     namespace, label selector, field selector, resourceVersion

  WHY THIS EXISTS:
    ─ Fan-out from one etcd watch to thousands of clients.
    ─ List operations served from memory (no etcd round-trip).
    ─ "Stale list" optimization: List with resourceVersion=0
      returns the watch cache's current contents, no etcd hit.

  COST:
    ─ Memory: proportional to number of objects × object size.
      Big cluster, big secrets, big CRDs → tens of GiB.
    ─ CPU: protobuf decode of every event (once per apiserver).
    ─ Latency: events delivered to clients with apiserver-side
      buffering; can be 1-10 ms behind etcd.
```

This is the structural reason a 5000-node cluster runs only ~30 etcd watches: the apiserver has a watch cache for each of ~30 resource types (Pods, Services, ConfigMaps, Secrets, Endpoints, EndpointSlices, Leases, Events, …), and every higher-up client multiplexes through those caches.

### 13.5 End-to-End Watch Flow: apiserver ↔ etcd

Putting the two-layer watch together for a concrete event:

```
  T+0       Controller A patches Pod nginx (Update).
  T+1ms     Apiserver-1 receives PATCH, runs admission/validation.
  T+5ms     Apiserver-1 calls storage.Update():
              storage.etcd3.store.GuaranteedUpdate()
              → builds Txn (compare resourceVersion, success=PUT).
  T+6ms     Txn sent to etcd over gRPC.

  T+6ms     etcd leader receives Txn.
              Proposes Raft entry (term=T, index=I).
  T+8ms     Leader appends entry to WAL, fsyncs.
              In parallel: replicates to 2 followers via AppendEntries.
  T+10ms    First follower acks (WAL fsync done).
              Quorum reached → leader's commitIndex advances to I.
  T+10ms    Apply loop pops committed entry I, applies to MVCC:
              ─ new revision R generated
              ─ keyIndex updated for /registry/pods/default/nginx
              ─ bbolt write (deferred fsync)
              ─ Txn response sent back to apiserver-1
  T+11ms    Apply loop fans out to SYNCED watchers:
              ─ apiserver-1's etcd watch on /registry/pods/
                receives Event(mod_rev=R, key=..., type=PUT, kv=...).
              ─ apiserver-2's etcd watch on /registry/pods/
                receives the same event.

  T+11ms    Apiserver-1's Pod-storage cacher receives the event.
              ─ Updates internal btree store (Pod nginx).
              ─ Appends to ring buffer (resourceVersion=R).
              ─ Dispatcher fans out to all client watchers
                whose selectors match.

  T+12ms    Apiserver-1 returns 200 OK to Controller A.

  T+12-15ms Each interested kubelet (the one on Pod nginx's
            node) receives a WatchResponse over its long-poll
            connection to apiserver-1 (or -2). It updates its
            local cache and triggers syncLoop work.

  TOTAL APISERVER-TO-WATCHER LATENCY: ~10-15ms p50, healthy cluster.
                                       30-50ms p99.
                                       Higher under load or with slow disk.
```

This timeline is the operational reality behind every Kubernetes feature that "just works": Deployment rolling updates, Service endpoint propagation, Pod scheduling, status reporting. Every one is the same loop applied to a different resource.

---

## 14. Observability: Metrics That Matter

etcd exposes a Prometheus `/metrics` endpoint with ~200 series. These are the ones you alert on.

### 14.1 The Core Five

```
  1. etcd_disk_wal_fsync_duration_seconds (histogram)
     What: how long each WAL fsync takes.
     Alert: p99 > 50ms for 5+ minutes.
     Why: the WAL is the durability bottleneck. Slow fsyncs
          mean slow writes for the whole cluster.

  2. etcd_disk_backend_commit_duration_seconds (histogram)
     What: how long each bbolt commit takes.
     Alert: p99 > 250ms for 5+ minutes.
     Why: bbolt commits are batched fsyncs. Slow commits
          mean the apply loop falls behind.

  3. etcd_network_peer_round_trip_time_seconds (histogram, per peer)
     What: RTT to each peer member.
     Alert: p99 > 250ms.
     Why: drives Raft heartbeats and replication. High RTT
          → election timeouts → false leader changes.

  4. etcd_server_proposals_failed_total (counter)
     What: number of Raft proposals that failed to commit.
     Alert: rate > 0 for 5+ minutes.
     Why: a failed proposal is a write that didn't happen.
          Usually means leader lost contact with majority.

  5. etcd_mvcc_db_total_size_in_bytes vs
     etcd_mvcc_db_total_size_in_use_bytes
     What: db file size vs logical in-use size.
     Alert: in_use / quota > 0.75; or total / in_use > 1.5.
     Why: the first warns of approaching quota → impending
          NOSPACE alarm. The second warns of defrag-due.
```

### 14.2 The Next Tier

```
  etcd_server_has_leader (gauge, 0 or 1)
    Alert: any value of 0 for any member > 30s.
    Means: this member doesn't currently know who the leader is.
           Brief 0s during elections are normal.

  etcd_server_leader_changes_seen_total (counter)
    Alert: rate > 1/hour.
    Means: leader is flapping. Usually a network or disk issue.

  etcd_server_slow_apply_total (counter)
    Alert: rate increasing.
    Means: the apply loop is taking > 100ms per entry —
           bbolt is slow, the cluster is overloaded, or
           there are very large transactions.

  etcd_server_slow_read_indexes_total (counter)
    Alert: rate > 0.
    Means: linearizable reads are timing out. Usually correlated
           with disk or network problems.

  etcd_disk_backend_defrag_duration_seconds
    Watch during scheduled defrags. Tells you how long defrag
    took. Anomalies hint at file-size growth or disk slowness.

  etcd_grpc_proxy_events_coalescing_*
    If you run grpc-proxy in front of etcd, these tell you
    how effective the proxy's event aggregation is.

  process_resident_memory_bytes
    The etcd process's RSS. Should be roughly proportional to
    the bbolt in_use size + watch state. Sudden growth = leak
    or accidentally-enormous client watch.
```

### 14.2a A Sample Dashboard Layout

```
  ┌──────────────────────────────────────────────────────────────────┐
  │  ROW 1: HEALTH                                                   │
  │  ┌────────────┐ ┌────────────┐ ┌─────────────┐ ┌──────────────┐ │
  │  │ has_leader │ │  members   │ │ leader_id   │ │ leader       │ │
  │  │ per member │ │  reachable │ │             │ │ changes/hr   │ │
  │  └────────────┘ └────────────┘ └─────────────┘ └──────────────┘ │
  │                                                                  │
  │  ROW 2: LATENCY                                                  │
  │  ┌─────────────────────────┐ ┌──────────────────────────────────┐│
  │  │ wal_fsync p50/p99/p999  │ │ backend_commit p50/p99/p999      ││
  │  │  (target p99 < 10ms)    │ │  (target p99 < 250ms)            ││
  │  └─────────────────────────┘ └──────────────────────────────────┘│
  │  ┌─────────────────────────┐ ┌──────────────────────────────────┐│
  │  │ peer_rtt p99 per peer   │ │ apiserver_request_duration       ││
  │  │  (target < 100ms)       │ │   apiserver-side, for context    ││
  │  └─────────────────────────┘ └──────────────────────────────────┘│
  │                                                                  │
  │  ROW 3: SIZE                                                     │
  │  ┌──────────────────────────────────────────────────────────────┐│
  │  │  db_total_size  vs  db_in_use_size  (both lines)             ││
  │  │  horizontal line at quota-backend-bytes                       ││
  │  │  annotation: scheduled defrag windows                         ││
  │  └──────────────────────────────────────────────────────────────┘│
  │                                                                  │
  │  ROW 4: THROUGHPUT                                               │
  │  ┌──────────────────────┐ ┌─────────────────────────────────────┐│
  │  │ proposals_committed  │ │ proposals_failed (alert any > 0)    ││
  │  │  per second          │ │                                     ││
  │  └──────────────────────┘ └─────────────────────────────────────┘│
  │                                                                  │
  │  ROW 5: WATCH                                                    │
  │  ┌──────────────────────────────────────────────────────────────┐│
  │  │ watcher count, total events/s sent to watchers, slow_apply   ││
  │  └──────────────────────────────────────────────────────────────┘│
  └──────────────────────────────────────────────────────────────────┘
```

The first three rows are existential: a problem on any of them is an active or impending outage. Rows 4 and 5 are diagnostic — they help you understand WHY rows 1-3 are misbehaving.

### 14.3 What Each Layer Tells You

```
  WHEN WRITES ARE SLOW, READ IN THIS ORDER:

    1. etcd_disk_wal_fsync_duration_seconds.histogram_quantile(0.99)
       → If > 50ms: disk problem. Check IOPS, dedicated disk,
         neighbors. This is your first stop, 80% of the time.

    2. etcd_network_peer_round_trip_time_seconds (per peer)
       → If > 100ms: network problem. Check inter-AZ traffic,
         packet loss, member placement.

    3. etcd_disk_backend_commit_duration_seconds.p99
       → If > 250ms: bbolt under pressure. Check
         mvcc_db_total_size_in_bytes; if approaching quota,
         compaction lagging.

    4. etcd_server_proposals_pending
       → If growing: writes piling up. Backpressure from
         disk or network.

    5. process_cpu_seconds_total + process_open_fds
       → Sanity: is the process under CPU pressure? Out of
         file descriptors? Rare but checked last.
```

---

## 15. Failure Scenarios

A taxonomy of how real etcd clusters break and what happens.

### 15.1 Single Follower Failure (Disk, Network, Crash)

```
  EFFECT:
    ─ Leader stops getting heartbeats from this follower.
    ─ After max(election-timeout × 2) the leader logs the
      follower as inactive; matchIndex stops advancing for it.
    ─ Other follower + leader = 2 of 3 = still majority.
    ─ Cluster keeps serving reads and writes.
    ─ Watch events keep flowing.
    ─ etcd_server_has_leader on remaining members = 1.

  RECOVERY:
    ─ Restart the follower or replace it.
    ─ It rejoins, replays the gap (or learns via snapshot
      transfer if its log lag exceeds --snapshot-count).
    ─ Once caught up, matchIndex advances, cluster is whole.

  ALARMS YOU EXPECT:
    ─ etcd_server_has_leader = 0 on the failed member.
    ─ etcd_network_peer_round_trip_time_seconds spikes from
      its peers.
```

### 15.2 Leader Failure (Clean Crash)

```
  EFFECT:
    ─ Both followers' election timers fire.
    ─ One (randomized) starts an election first → votes for
      itself, sends RequestVote.
    ─ Other follower votes yes.
    ─ New leader emerges within ~1.5-3s (election-timeout +
      a bit of variance).
    ─ Apiserver writes block during the gap; return 500 or
      timeout to clients.

  RECOVERY:
    ─ Automatic, as above.
    ─ Apiserver retries reconnect to the new leader transparently
      (it pings all configured etcd endpoints).
    ─ Controllers that were mid-reconcile retry their PATCHes.

  CLIENT IMPACT:
    ─ p99 write latency spike to election-timeout + ~100ms.
    ─ Brief flap of error rates in apiserver logs.
```

### 15.3 Leader Disk Failure (Stall, not Crash)

This is the harder case. The leader's process keeps running but fsyncs take 10 seconds each.

```
  EFFECT:
    ─ Heartbeats still go out (they don't fsync).
    ─ But proposals can't commit (write to WAL stalls).
    ─ Followers see heartbeats → don't start election.
    ─ Apiserver writes hang for tens of seconds.

  WHY THIS IS BAD:
    ─ Pre-vote / election won't fire because the leader looks
      alive from heartbeats.
    ─ The cluster is functionally write-unavailable but the
      Raft state machine doesn't know.

  DETECTION:
    ─ etcd_disk_wal_fsync_duration_seconds p99 climbs.
    ─ etcd_server_slow_apply_total rate increases.
    ─ Pending proposals accumulate.

  RECOVERY:
    ─ Operator manually moves leadership:
        etcdctl move-leader <healthy-follower-id>
    ─ Or, in worst case, restarts the slow leader: it crashes,
      a new leader emerges from the remaining two.
    ─ This is why heartbeat-based liveness is insufficient
      and why dedicated NVMe matters: avoid the gray-failure
      regime entirely.
```

### 15.4 Network Partition (Minority Side)

```
  Setup: A, B, C 3-member cluster. Partition isolates A.

  EFFECT (on the minority side, A):
    ─ A's election timer fires; A becomes candidate.
    ─ Without pre-vote: A increments term, sends RequestVote,
      gets no response → loses election. Repeats. Term
      escalates indefinitely.
    ─ With pre-vote: A pings B and C, gets no response, stays
      a candidate at the same term. (Better behavior.)
    ─ A serves NO reads (linearizable reads require contacting
      a majority) and NO writes.

  EFFECT (on the majority side, B + C):
    ─ One of them is leader, the other a follower.
    ─ Cluster keeps committing writes (2 of 3 = majority).
    ─ Watchers connected to B/C continue receiving events.

  HEAL:
    ─ Network returns. A receives heartbeat from current
      leader with current term.
    ─ A steps down (if it was a candidate), updates term,
      becomes follower.
    ─ Leader sees A is far behind, sends AppendEntries or
      a snapshot transfer.
    ─ A catches up; cluster is whole.

  NO SPLIT-BRAIN:
    ─ Because only the majority side can elect a leader and
      commit writes. The minority is permanently stuck without
      quorum until partition heals. This is the FUNDAMENTAL
      guarantee of Raft.

  STALENESS:
    ─ Clients reading from A during partition DO NOT get
      stale data, because A refuses to serve linearizable
      reads. They get errors or timeouts.
    ─ Clients reading with serializable consistency (rare in
      Kubernetes) from A DO get stale data: A's local MVCC
      reflects pre-partition state. This is the trade-off
      of --consistency=s.
```

### 15.5 All Members Restart Simultaneously

```
  EFFECT:
    ─ No leader. No quorum.
    ─ Members come up, read their WAL + snapshots, restore
      Raft state.
    ─ As soon as a majority is up, elections proceed.
    ─ Cluster recovers.

  TIMING:
    ─ Restart time per member: WAL replay + bbolt mmap open
      + log catch-up = typically 5-30s for a multi-GiB db.
    ─ Once a majority is up: election runs, leader elected
      within election-timeout.

  GOTCHA:
    ─ Apiserver may be configured with --etcd-servers as a
      list. If apiservers are also restarting (e.g., the whole
      control plane fell over), they retry-connect with
      backoff. Initial control-plane recovery may take
      30s-2min.
```

### 15.6 Lost Majority (More than f Members Down)

This is the disaster case. The cluster has lost quorum and cannot recover automatically.

```
  EFFECT:
    ─ No leader can be elected.
    ─ No writes can commit.
    ─ Existing data is intact on surviving members but
      inaccessible for writes.
    ─ Reads MIGHT work in serializable mode on a surviving
      member, but linearizable reads fail.

  RECOVERY (DANGEROUS):
    Force a new cluster from a single surviving member.
    1. Pick the surviving member with the highest Raft index
       (etcdctl endpoint status to compare).
    2. Stop etcd on it.
    3. Restart it with: --force-new-cluster
       This rewrites the cluster config to {this member alone},
       resets term, and starts as a single-member cluster.
    4. Add the other (replacement) members one at a time.

  CAVEATS:
    ─ --force-new-cluster discards any writes that were
      committed on lost members but not on the chosen survivor.
      (Raft normally guarantees committed writes survive any
       minority failure; force-new-cluster is for when MAJORITY
       was lost and that guarantee no longer applies.)
    ─ Members that come back later will see a different cluster-id
      and refuse to rejoin. They must be wiped and re-added.

  PREVENTION:
    Mostly: 3-AZ placement, monitoring, alerting on member
    health. Lost majority should not happen during routine ops.
```

### 15.7 Corruption

```
  CAUSES:
    ─ Disk-level corruption (bit rot, controller bug).
    ─ bbolt bug (rare).
    ─ Manual etcdctl writes to wrong keys (operator error).

  DETECTION:
    ─ etcd checks integrity at startup and during snapshot.
    ─ etcdctl check perf  (a load-test cross-check)
    ─ etcdctl endpoint hashkv → compute a hash of the keyValue
      bucket. All members should report the same hash; mismatch
      = corruption.

  RECOVERY:
    ─ If only one member is corrupt: remove it, replace it.
      It will be re-synced from peers.
    ─ If multiple members are corrupt: restore from the most
      recent good snapshot (§10).
```

---

## 16. Pitfalls and Anti-Patterns

The greatest hits of "things you will do wrong on the way to mastery".

### 16.1 Defragging the Leader

Covered in §9. Always `move-leader` first, then defrag the (now-)follower. A leader undergoing defrag is unavailable for writes for the duration of the defrag — typically tens of seconds. Defragging followers first, then a quick leader move + defrag, keeps the cluster's leader-side latency invariant.

### 16.2 Networked Storage for etcd

```
  THE TRAP:
    "We have a fleet of VMs with EBS volumes. Let's run etcd
     stacked on the apiserver nodes and use the default EBS."

  WHY IT FAILS:
    ─ EBS gp2/gp3 fsync latency is variable (1-30ms p99).
    ─ Raft + bbolt fsync 2-3 times per write.
    ─ p99 latencies stack: a single write can take 100ms.
    ─ At 1000 writes/s sustained, the disk queue fills up,
      fsync latency climbs further, election timeouts trip,
      leadership flaps.

  THE FIX:
    ─ Local NVMe (AWS i3/i4i instance store, GCP local SSD,
      Azure Lsv3).
    ─ Or io2 Block Express / Premium SSD v2 with provisioned
      IOPS sized for your write rate × 3 (for amplification).
    ─ Or dedicated database-class storage with separate disk
      for WAL.
```

### 16.3 Letting db Size Grow Past 2 GiB Without Compaction

Covered in §8.3. Without `--auto-compaction-mode`, every revision lives forever. Workloads with frequent updates (Lease renewals × thousands of nodes, Event objects, Node status, ConfigMap updates) blow past 2 GiB rapidly. Always set `--auto-compaction-mode` and monitor `in_use` size.

### 16.4 Too Many Watches

```
  THE TRAP:
    Operator framework users start 50 controllers in a single
    process. Each one creates several informers. Each informer
    opens a watch against apiserver. Apiserver creates one
    etcd watch per resource type, but the apiserver-side
    watch cache and event fan-out cost CPU.

  AT SCALE:
    ─ 50 controllers × 10 informers = 500 client watchers
      on apiserver.
    ─ Apiserver memory grows with watcher count × per-watcher
      buffer.
    ─ CPU grows with event rate × number of watchers receiving
      that event.

  THE FIX:
    ─ Use a shared informer factory: one informer per
      (resource, namespace, selector) shared across controllers.
    ─ Limit each informer's resource scope (namespace, selector)
      to only what's needed.
    ─ In controller-runtime, use the Manager's shared cache.
```

### 16.5 Too Many CRDs Writing Tiny Objects

```
  THE TRAP:
    Operator stores per-pod state as individual CRs of a
    custom Status resource. 5000 pods × 1 CR each = 5000
    CRs, each updating every reconcile.

  WHY IT HURTS:
    ─ Every CR update is an etcd Txn = a Raft entry = a fsync.
    ─ CRs are stored as JSON (not protobuf) → larger bbolt entries.
    ─ Compaction has to deal with 5000 keys × hundreds of
      revisions each.
    ─ Watches over the CR type fire frequently on every update.

  THE FIX:
    ─ Store derived state in the controller's own database
      (or even an in-memory cache + periodic snapshot to a
      ConfigMap).
    ─ Use a single CR with a map field, not 5000 CRs (with
      caveats about max object size and write contention).
    ─ Push status into a single "Aggregated Status" CR
      reconciled separately.
```

### 16.6 Unbounded Lists Without Pagination

```
  THE TRAP:
    A client lists all Pods cluster-wide:
      GET /api/v1/pods
    in one shot. Apiserver returns 10 000 Pods × ~10 KiB =
    100 MiB response.

  WHY IT HURTS:
    ─ Apiserver must marshal the entire list once per List call.
    ─ With multiple concurrent List clients, apiserver memory
      spikes.
    ─ If served from etcd (resourceVersion != "0"), etcd does
      a range read over the entire prefix. That's expensive.

  THE FIX:
    ─ Use chunked listing (continue tokens):
        GET /api/v1/pods?limit=500
        ... 'continue': 'eyJ2I...'
        GET /api/v1/pods?limit=500&continue=eyJ2I...
    ─ Use label/field selectors to scope.
    ─ Prefer Watch over repeated List for change tracking.
    ─ For dashboards: List with resourceVersion=0 from the
      apiserver watch cache (no etcd hit).
```

### 16.7 Large Objects (Approaching `--max-request-bytes`)

A Pod or CR approaching 1 MiB hits several issues at once:
- Apiserver Decode + Encode CPU spike per write.
- etcd Txn approaching max request size.
- Watch events that exceed 1 MiB fragment over the wire.
- Network buffer pressure on every recipient.

The advice: keep object size below ~256 KiB. If you have larger state, store it externally (a Secret in a separate store, a database, a blob store) and keep only a reference in the Kubernetes object.

### 16.8 Treating etcd Like a Database

```
  ANTI-PATTERN:
    Storing application data in etcd via CRDs because "it's
    already there". Storing logs. Storing metrics. Storing
    user profiles.

  WHY:
    etcd is a coordination store, sized for the control-plane
    state (counts in MiB-to-GiB). Application data scales
    differently, has different access patterns (high read
    throughput, batch writes, range queries), and uses much
    cheaper backends.

  THE LIMIT:
    Stay under 8 GiB of in_use bytes. Past that, switch to
    a real database.
```

### 16.9 Skipping the Pre-Upgrade Snapshot

A snapshot is cheap. Take one before every cluster upgrade, every kubeadm upgrade, every etcd version bump. If the upgrade fails or corrupts state, you can `snapshot restore` and lose only the time since the snapshot. The recovery procedure is documented in §10.

### 16.10 Running 2 Members

Already covered in §12.1, but worth repeating: a 2-member cluster is strictly worse than a 1-member cluster (quorum = 2, so any single failure = no quorum = unavailable). If you must shrink, go to 1; never sit at 2.

---

## 17. TL;DR

- **etcd is a single-leader Raft replicated MVCC KV store with watch, lease, and transactions.** It is the only stateful component in Kubernetes. Lose etcd, lose the cluster.
- **Raft serializes all writes through one leader, replicates them to a majority, then applies them to a state machine.** Operationally, you tune heartbeat-interval (default 100ms), election-timeout (1000ms, must be 5–10× heartbeat), and you arrange leadership transfer via `etcdctl move-leader` before doing anything disruptive.
- **The storage layer is three things in one directory.** A WAL (append-only, fsync-per-batch, write-only) for durability; a sequence of Raft snapshots for log truncation; a bbolt B+tree (4 KiB pages, COW, mmap'd, alternating-meta crash safety) for MVCC. The WAL uses write+fsync because it's small and write-heavy; bbolt uses mmap because it's large and read-heavy with COW writes.
- **MVCC: every mutation gets a globally-monotonic revision.** The keyIndex maps logical keys to lists of revisions; bbolt stores keyValues by revision. `metadata.resourceVersion` in Kubernetes equals the etcd mod_revision. The cluster-wide revision (not per-key) is what makes List+Watch consistency possible.
- **Watch is a streaming gRPC over a single connection.** Server tracks SYNCED vs UNSYNCED buckets; the apiserver layers its own watch cache on top so N clients = 1 etcd watch + N apiserver watchers. The dreaded "required revision has been compacted" error means a client asked to resume from a revision older than the current compactRevision; the only recovery is a full re-list.
- **Leases give TTL-based expiration of attached keys.** Kubernetes uses them for the Lease object (leader election), Node leases (heartbeats — a major scale fix from per-Node status PATCHes), Event TTL, and SA token rotation.
- **Transactions are compare-and-then-do-else.** Every Kubernetes write is a Txn with a `compare MOD == resourceVersion` clause. This IS the optimistic concurrency control of the Kubernetes API.
- **Compaction reclaims revisions; defrag reclaims disk.** Compaction drops historical revisions below a threshold, freeing bbolt pages onto its freelist. Defrag rewrites the bbolt file to physically shrink it. Always defrag followers first, then move-leader, then defrag the former leader. NEVER `defrag --cluster` in production.
- **Operational topology: 3 members spread across 3 failure domains, NVMe local disk, ≤8ms RTT between members.** Bigger clusters go to 5 members; never 2, never 4, never 6. The bottleneck is disk fsync, not CPU.
- **apiserver ↔ etcd: apiserver stores everything under `/registry/<resource>/<ns>/<name>` in protobuf.** Reads come from the apiserver watch cache; writes go through Txn with resourceVersion CAS. The apiserver opens one watch per resource type against etcd and fans out to thousands of clients.
- **The five metrics:** `etcd_disk_wal_fsync_duration_seconds`, `etcd_disk_backend_commit_duration_seconds`, `etcd_network_peer_round_trip_time_seconds`, `etcd_server_proposals_failed_total`, `etcd_mvcc_db_total_size_in_bytes` vs `_in_use_bytes`. Wal fsync p99 < 10ms, backend commit p99 < 250ms, peer RTT p99 < 250ms, no proposal failures, db ratio total/in_use < 1.5. Alert on each; the first one to break is almost always the disk.
- **The most common cause of etcd outages is operator-induced: defrag at the wrong moment, no compaction tuning, no quota monitoring, no leadership transfer before maintenance.** Read §9.3 and §15 to know the failure modes before they happen.

**One sentence:** *etcd is Raft + MVCC + watch + lease in one process; Kubernetes is the apiserver's pattern of using exactly these primitives for everything; tune the disk, schedule the compaction, defrag rolling, and the cluster will mostly run itself — get any of those wrong and the entire control plane is down within hours.*
