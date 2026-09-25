# Chapter 04: Replication and Consistency — Leaders, Quorums, Lag, and What Clients Actually See

How copies of data are kept, how far behind they fall, and exactly what a client is allowed to observe as a result. This chapter goes from the mechanics of log shipping and failover, through the arithmetic of quorums (overlap, availability, tail latency, stale-read probability), to the precise meaning of linearizability, serializability, causal, session, and eventual consistency, and ends with a feature-by-feature decision guide.

Prerequisites: the system and failure models, CAP, and PACELC in `00-primitives-and-system-models.md`; Raft and fencing in `03-consensus-raft-and-distributed-locking.md`. This chapter deliberately skips material that `../databases/12-replication-and-distributed-storage.md` (§2 Replication, §9 Consistency Models) and `../databases/19-distributed-databases-deep-dive.md` (§3 clocks, §6 replication patterns, §7 CRDTs, §8 anti-entropy) already cover, and links to them instead. What it adds is depth: the math, the anomaly timelines, the configuration knobs, and how to measure and route around lag.

---

## Table of Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [The replication log and the three topologies](#1-the-replication-log-and-the-three-topologies)
2. [Single-leader replication: log shipping, catch-up, and failover](#2-single-leader-replication-log-shipping-catch-up-and-failover)
3. [Multi-leader replication: topologies, causality, and conflict detection](#3-multi-leader-replication-topologies-causality-and-conflict-detection)
4. [Leaderless replication (Dynamo-style)](#4-leaderless-replication-dynamo-style)
5. [Synchronous, asynchronous, and semi-synchronous replication](#5-synchronous-asynchronous-and-semi-synchronous-replication)
6. [Replication lag: causes, measurement, and anomalies](#6-replication-lag-causes-measurement-and-anomalies)
7. [Session guarantees and how to implement them](#7-session-guarantees-and-how-to-implement-them)
8. [Quorum math: overlap, availability, latency, and staleness](#8-quorum-math-overlap-availability-latency-and-staleness)
9. [Why quorum overlap is not linearizability](#9-why-quorum-overlap-is-not-linearizability)
10. [Linearizability](#10-linearizability)
11. [Serializability vs linearizability](#11-serializability-vs-linearizability)
12. [Causal consistency](#12-causal-consistency)
13. [Eventual consistency, bounded staleness, and the consistency ladder](#13-eventual-consistency-bounded-staleness-and-the-consistency-ladder)
14. [Conflict resolution: avoid, overwrite, keep siblings, merge, or CRDT](#14-conflict-resolution-avoid-overwrite-keep-siblings-merge-or-crdt)
15. [Decision guide: feature to guarantee to mechanism](#15-decision-guide-feature-to-guarantee-to-mechanism)
16. [Production pitfalls / war stories](#production-pitfalls--war-stories)
17. [Interview questions](#interview-questions)
18. [Real-world cases — incidents with numbers](#real-world-cases--incidents-with-numbers)
19. [Key Takeaways](#key-takeaways)
20. [Cross-References](#cross-references)

---

## Start here — the whole chapter in plain words

**The problem.** You keep several copies (replicas) of your data so that one machine dying does not
lose it, and so that more machines can answer reads. But a change reaches the copies one at a
time, over a network that can be slow or broken. For a while, the copies disagree. Every
replication design is a set of answers to three questions: *who is allowed to accept a write*,
*how long does the writer wait before saying "done"*, and *which copy answers a read*. Those
answers decide what a user can see: their own edit vanishing, a number going backwards, a reply
appearing before the message it answers, two people buying the last seat, or a confirmed payment
disappearing after a crash. This chapter is about predicting those effects and choosing the
cheapest design that rules out the ones your product cannot tolerate.

**A running example.** An invoicing SaaS for small businesses runs PostgreSQL: one primary and two
read replicas in the same region, plus one replica in a second region for disaster recovery.
Peak load is 200 writes/s and 2,000 reads/s; reads go to the replicas. Normal replication lag is
20–50 ms. During the nightly reporting job, one replica falls up to 8 s behind. (Illustrative
numbers, used throughout the chapter.)

- **"I saved it and it's gone."** A user edits invoice #1042 from 900 to 950 and is redirected to
  the invoice list, which reads from a replica 3 s behind. The list shows 900. This is a
  *read-your-writes* violation (§6.4, §7.2). Fix: carry the commit's WAL position (LSN) in the
  user's session and only read from a replica that has replayed past it (a 50-line router, §7.2).
- **"The total keeps jumping."** The dashboard refreshes every 5 s and the load balancer
  alternates between a replica 30 ms behind and one 6 s behind. The monthly total flips between
  12,400 and 12,350. That is a *monotonic reads* violation (§7.3). Fix: pin the session to one
  replica, or remember the highest LSN seen.
- **"The reply is there but the question isn't."** An accountant's comment "Approved, see
  question above" shows up before the client's question it answers, because the two were stored
  on different shards with different lag. That is a *consistent prefix / causal* violation
  (§7.6, §12).
- **"We lost the last half second."** The primary's disk controller dies. Replication is
  asynchronous and the most up-to-date replica was 400 ms behind: about 200 writes/s × 0.4 s =
  80 committed writes, including 3 payments marked "paid", are gone after failover (§2.5).
  Fix: semi-synchronous commit to at least one replica (`synchronous_standby_names = 'ANY 1 (r1, r2)'`),
  costing about 1 ms per commit in-region (§5.2).
- **"Two people redeemed the same coupon."** The redemption check `SELECT used FROM coupons` ran
  on a replica 50 ms behind, saw "unused", and both requests proceeded. No amount of
  session guarantee fixes this; the check needs a *linearizable* read-and-write on the primary,
  such as a conditional `UPDATE ... WHERE used = false` (§10.6, §15).
- **"Why not use quorums everywhere?"** With 3 replicas, writing to 2 and reading from 2 means every
  read overlaps the last completed write (§8.1), survives 1 node failure for both reads and writes
  (§8.3), and its tail latency ignores the slowest replica (§8.5). But it is still not
  linearizable without an extra write-back step (§9).

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Replica | one full copy of a piece of data on one machine | a photocopy of the master ledger kept in another office |
| Leader (primary) | the one replica allowed to accept writes; the others copy from it | the head office that approves every change |
| Follower (replica, standby, secondary) | a replica that applies the leader's changes in order | a branch office updating its copy from the head office's memos |
| Replication log (WAL, binlog) | the ordered list of changes the leader sends to followers | the numbered memos the head office sends out |
| LSN / GTID / offset | the position of a change in that log | memo number 4,711 |
| Synchronous replication | the writer waits until a replica confirms before saying "done" | not leaving the post office until you get the delivery receipt |
| Asynchronous replication | the writer says "done" as soon as its own copy is safe | dropping the letter in the box and walking away |
| Replication lag | how far a follower is behind the leader, in time or bytes | how many memos the branch office has not processed yet |
| Failover | promoting a follower to leader when the leader dies | the deputy taking over when the manager is out |
| Split brain | two replicas both believe they are the leader and accept writes | two managers both signing contracts for the same office |
| Fencing | making sure an old leader's writes are rejected | changing the locks after a new tenant moves in |
| Multi-leader | several replicas accept writes and exchange them | several branch offices that can each approve changes and sync later |
| Leaderless / quorum | any replica accepts writes; a write counts once W replicas confirm | asking 3 colleagues to note a decision and trusting it once 2 have |
| Quorum overlap (R + W > N) | any read group and any write group share at least one replica | if 2 of 3 people wrote it down and you ask 2 of 3, one of them knows |
| Read repair | a reader that sees a stale replica writes the newer value back | correcting a colleague's notes when you notice they are out of date |
| Linearizability | the system behaves like one copy; once anyone sees a new value, everyone does | one whiteboard in one room |
| Serializability | concurrent transactions produce a result some one-at-a-time order would produce | a bank teller serving customers one by one, in some order |
| Causal consistency | if B was caused by A, nobody sees B without A | nobody reads the reply before the question |
| Eventual consistency | if writes stop, all copies end up the same; no promise about when | gossip: everyone hears the news, eventually |
| Session guarantees | promises about what one user sees across their own requests | your own notebook is always up to date for you |
| Conflict | two replicas accepted different writes to the same thing concurrently | two branch offices each sold the same car |
| LWW (last writer wins) | keep the write with the biggest timestamp, silently drop the other | the latest-dated form wins, even if the clock on the stamp was wrong |
| Version vector | a small per-replica counter map that tells "happened before" from "concurrent" | each office's memo counters: "I've seen head office up to 12, branch B up to 7" |
| CRDT | a data type whose concurrent updates always merge to the same result | a tally sheet where each office only adds to its own column |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| `N` | number of replicas of one item (replication factor) | 3 or 5 | 3 copies of each invoice row |
| `W` | replicas that must confirm a write before it is acknowledged | majority | 2 of 3 |
| `R` | replicas that must answer a read | majority | 2 of 3 |
| `f` | number of replica failures tolerated | 1–2 | N = 3 majority quorums: f = 1 |
| `p` | probability that one replica is unavailable at a random moment | 0.1%–5% | p = 1% |
| `q` | probability that one replica's response is slow (e.g. GC pause) | 0.1%–2% | 1% of responses take 100 ms |
| `k` | "wait for the k-th fastest of N responses" | W or R | k = 2 of 3 |
| `A_k` | probability that at least k of N replicas are up | — | N = 3, k = 2, p = 1%: 99.97% |
| `L` | replication lag (time) | ms to minutes | 50 ms normally, 8 s at night |
| LSN | PostgreSQL WAL position, printed as `hi/lo` hex | — | `16/B374D848` |
| GTID | MySQL global transaction ID `source_uuid:txn_no` | — | `3E11FA47-...:1-23` |
| `T_pin` | read-from-leader window after a write | 1–10 s | 5 s |
| `RPO` | recovery point objective: how much committed data a failover may lose | 0 – seconds | async, lag 400 ms: RPO ≈ 400 ms of writes |
| `RTO` | recovery time objective: how long a failover takes | seconds – minutes | 30 s |
| `RTT` | network round-trip time | 0.1–0.5 ms in a rack/AZ, 1–2 ms across AZs, 30–150 ms across regions | — |
| `t_fsync` | time to make the log durable on local disk | 0.05–2 ms (SSD), up to 10 ms (cloud network disk) | 0.5 ms |
| `T` / `K` | bounded-staleness limits: at most `T` seconds or `K` versions behind | 5 s / 100 versions | Cosmos DB bounded staleness |
| `VV` | version vector, a map `{replica: counter}` | — | `{A: 3, B: 1}` |
| `ts` | a write's timestamp (wall clock, HLC, or logical) | — | `12:00:03.000` |
| `ε` | clock uncertainty (max skew between nodes) | 1–250 ms | TrueTime ε ≈ a few ms |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. The replication log and the three topologies

> **In plain words.** Almost every replication scheme ships a *log* of changes from the replica that accepted a write to the others. The design differences are who may accept writes (one node, several, or any), and when the writer says "done". Everything a client can observe follows from those two choices.
>
> **Real-world example.** PostgreSQL ships WAL from one primary (single leader). An offline-capable notes app lets every phone accept edits and sync later (multi-leader, one "leader" per device). Cassandra lets any of the 3 replicas of a row accept a write and counts it done when 2 have it (leaderless quorum).

### 1.1 One log, applied in order, gives identical copies

If every replica starts from the same state and applies the same deterministic changes in the same
order, every replica ends in the same state. This is the *state machine replication* idea, and it is
the reason every system below talks about "the log": the WAL in PostgreSQL, the binlog in MySQL, the
oplog in MongoDB, the partition log in Kafka, the Raft log in etcd.

```
           the log (positions increase)
           ┌─────┬─────┬─────┬─────┬─────┬─────┐
  Leader   │ 101 │ 102 │ 103 │ 104 │ 105 │ 106 │  ◄── new writes appended here
           └─────┴─────┴─────┴─────┴─────┴─────┘
                                 ▲           ▲
  Follower A has applied up to ──┘           │   lag(A) = 2 entries
  Follower B has applied up to ──────────────┘   lag(B) = 0 entries

  A follower's state is always a PREFIX of the leader's history.
  "Stale" means "a shorter prefix", never "a different history" (single leader only).
```

That last line is the most useful property of single-leader replication and it is lost in the
other two topologies: with several writers, replicas can hold histories that are not prefixes of
each other, and someone has to reconcile them.

### 1.2 The three topologies at a glance

```
  SINGLE-LEADER                 MULTI-LEADER                    LEADERLESS
  ─────────────                 ────────────                    ──────────
   writes                        writes      writes              writes/reads
     │                             │           │                  (any node, via
     ▼                             ▼           ▼                   a coordinator)
  ┌──────┐                     ┌──────┐ ◄──► ┌──────┐             │   │   │
  │  L   │                     │  L1  │ async │  L2  │             ▼   ▼   ▼
  └──┬───┘                     └──┬───┘       └──┬───┘          ┌──┐┌──┐┌──┐
     │ log                        │ log          │ log          │A ││B ││C │
  ┌──▼──┐ ┌─────┐              ┌──▼──┐        ┌──▼──┐           └──┘└──┘└──┘
  │ F1  │ │ F2  │              │ F1  │        │ F2  │          ok when W answer
  └─────┘ └─────┘              └─────┘        └─────┘          read R, pick newest
```

| | Single-leader | Multi-leader | Leaderless |
|---|---|---|---|
| Who accepts a write | one leader per shard | one leader per region/device; several per item | any replica of the item |
| Conflicting concurrent writes possible? | no (leader orders them) | yes | yes |
| Replica history is a prefix of the leader's? | yes | no | no |
| Write availability when the leader is unreachable | none until failover | other leaders keep writing | yes if W replicas reachable |
| Typical write latency | local fsync + optional replica ack | local | k-th fastest of N acks |
| Typical systems | PostgreSQL, MySQL, MongoDB replica sets, Kafka partitions, Raft groups, DynamoDB (leader per partition) | BDR/PGD, MySQL Group Replication (multi-primary), CouchDB, Cosmos DB multi-region writes, offline-first apps | Cassandra, ScyllaDB, Riak, Voldemort, the original Dynamo |
| Read freshness knob | which replica you read | which region you read | R and read repair |

A frequent confusion: **Amazon DynamoDB (the service) is not a leaderless Dynamo-style store.** Its
2022 USENIX ATC paper describes a leader per partition, elected with Multi-Paxos; strongly
consistent reads go to the leader and eventually consistent reads may go to any replica. The
leaderless design comes from the 2007 *Dynamo* paper, and survives in Cassandra, ScyllaDB and Riak.

### 1.3 What "consistency" is asked to mean

The rest of the chapter uses four separate questions. Mixing them up is the main source of
confusion about consistency models.

| Question | Name of the property family | Example guarantee |
|---|---|---|
| Does a read see the latest completed write, in real time? | **recency** | linearizability, bounded staleness |
| Do reads see writes in an order that respects cause and effect? | **ordering** | causal, consistent prefix, monotonic reads |
| Do concurrent multi-object transactions behave as if run one at a time? | **isolation** | serializability, snapshot isolation (see `../databases/05-transactions-and-concurrency.md`) |
| Do replicas end up identical once writes stop? | **convergence** | eventual, strong eventual consistency |

Linearizability is about recency of single objects; serializability is about isolation of
multi-object transactions (§11). Causal consistency is about ordering and is available under
partitions (§12). Eventual consistency is only about convergence (§13). A system can be strong on
one axis and weak on another: a PostgreSQL read replica gives each query a consistent snapshot,
shows writes in commit order (it replays one log), and yet can be seconds stale.

---

## 2. Single-leader replication: log shipping, catch-up, and failover

> **In plain words.** One node takes every write and streams its log to followers. The hard parts are not the streaming. They are: what exactly goes in the log, how a follower that fell behind (or a brand-new one) catches up, and how to replace a dead leader without losing writes or ending up with two leaders.
>
> **Real-world example.** The invoicing app adds a third replica: it copies a 2 TB snapshot (about 70 minutes), then replays 80 GB of WAL that accumulated meanwhile. If the primary deletes that WAL before the replica asks for it, the copy is useless and must start over.

### 2.1 Forms of the replication log

What the leader ships determines what the follower must be, what can go wrong, and what else you
can do with the stream (for example change data capture).

```
  Client: UPDATE invoices SET amount = amount * 1.2, updated_at = now() WHERE tenant_id = 7;

  STATEMENT-BASED        "UPDATE invoices SET amount = amount * 1.2, updated_at = now() WHERE tenant_id = 7"
  (re-execute SQL)       follower re-runs it: now() differs, row order may differ, triggers re-fire

  PHYSICAL / WAL         "page 88213 of file 16384/24576, offset 312: bytes 0x...  (x 40 pages)"
  (byte changes)         follower must be the same engine version, same on-disk format

  LOGICAL / ROW          "tenant 7, invoice 1042: amount 900 -> 1080, updated_at -> 2026-09-25 10:00:00.123"
  (row images)           one record per changed row; engine-version independent
```

| Form | Examples | Deterministic? | Follower must be | Size | Selective (some tables)? | Feeds CDC? |
|---|---|---|---|---|---|---|
| Statement | MySQL `binlog_format=STATEMENT`, VoltDB (deterministic stored procedures) | only if every statement is (no `NOW()`, `RAND()`, `UUID()`, `LIMIT` without `ORDER BY`, auto-increment races, side-effecting triggers) | same schema | smallest for bulk updates | yes | poorly |
| Physical (WAL shipping) | PostgreSQL streaming replication, Oracle Data Guard physical standby | yes | byte-identical engine: same major version, same architecture | includes index and page-level changes; full-page images after checkpoints | no, whole cluster | no |
| Logical (row-based) | MySQL `binlog_format=ROW` (default since 5.7.7), PostgreSQL logical replication (`pgoutput`, PG 10+), MongoDB oplog | yes | any engine version that understands the rows | one record per row changed; a 10M-row `UPDATE` is 10M records | yes (publications, filters) | yes (Debezium, `wal2json`) |
| Trigger-based | Slony, Bucardo, older custom setups | depends on triggers | anything | extra write per write on the leader | yes | yes |

Practical consequences:

- **Physical replication cannot be used for a major-version upgrade**, because both sides must share
  the on-disk format. Logical replication can: replicate from PG 14 to PG 17, then switch over.
- **Logical replication in PostgreSQL does not replicate DDL, and (through at least PostgreSQL
  17) does not replicate sequence values.** After a failover to a logical subscriber, the schema
  may differ and sequences may hand out IDs that already exist. Check your version's documentation
  before relying on a logical subscriber as a failover target.
- **Statement-based replication is a determinism contract** your application can break without
  knowing. MySQL's `MIXED` format switches to row images for statements it knows are unsafe, but
  only for the unsafe patterns it knows about.
- **Physical replication ships index maintenance too**, so a follower's apply work is similar to the
  leader's write work, which matters for lag (§6.2).

### 2.2 Follower catch-up: positions, retention, and the "fell off the log" failure

A follower does not need a snapshot to recover from a short disconnect. It remembers the log
position it has durably received, reconnects, and asks for everything after it.

```
  Follower                                     Leader
     │ (disconnected for 90 s)                   │ log grows: ...  0/5A000000 ... 0/5F000000
     │                                           │
     ├── START_REPLICATION from 0/5A120000 ─────►│  still has that WAL?
     │                                           │    yes → stream from there, follower catches up
     │                                           │    no  → "requested WAL segment has already been removed"
     │                                           │           follower must be rebuilt from a new snapshot
```

The leader keeps log only for a while, so every system has a retention knob, and it is a trade
between "a follower that was away can catch up" and "the leader's disk fills up":

| System | What retains log for followers | The failure on each side |
|---|---|---|
| PostgreSQL | replication slots (retain WAL until the consumer confirms it); `wal_keep_size` (PG 13+, formerly `wal_keep_segments`); WAL archive + `restore_command` as a fallback | a slot whose consumer is gone retains WAL forever and fills the disk; cap it with `max_slot_wal_keep_size` (PG 13+). Without slots, a follower away too long cannot resume. |
| MySQL | binlog retention (`binlog_expire_logs_seconds`, default 30 days in 8.0) | purged binlogs → replica must be re-cloned |
| MongoDB | the oplog, a capped collection sized in bytes (plus an optional minimum retention period in newer versions) | a secondary that falls off the oplog needs a new initial sync |
| Kafka | topic retention (`retention.ms` / `retention.bytes`); followers fetch from their log end offset | a follower out of the ISR catches up from the leader's log; truncation on rejoin uses leader epochs |

**Divergent tails.** A follower may have received log entries that the new leader does not have
(for example, it was connected to the old leader a moment longer). Before following a new leader
it must find the last common position and discard its own tail. PostgreSQL uses timeline IDs
(`pg_rewind` rewinds an old primary to the fork point); Kafka uses leader epochs (KIP-101) to
truncate correctly; Raft does it by construction (the leader overwrites conflicting entries, see
`03-consensus-raft-and-distributed-locking.md` §4.3). A system that does not do this ends up with
replicas that silently disagree.

### 2.3 Adding a replica: snapshot plus position

A new replica needs a copy of the data *and* the exact log position that copy corresponds to.
Copying files from a running leader without a position gives an inconsistent, unusable copy.

```
  time ─────────────────────────────────────────────────────────────────────────►

  Leader    ──── writes keep coming ─────────────────────────────────────────────
               │ start backup:           │ backup done
               │ checkpoint, note        │ (files are a fuzzy copy,
               │ START LSN = 0/7000028   │  fixed up by replaying WAL)
               ▼                         ▼
  New       [ copy 2 TB of data files ..... ][ replay WAL from 0/7000028 ......... ][ streaming ]
  replica                                     ▲ needs every WAL byte since START LSN   lag ~ ms
                                              │ retained by a slot or wal_keep_size
```

Tools that do this correctly: `pg_basebackup -X stream` (streams the WAL generated during the
backup alongside it), MySQL's clone plugin (8.0.17+) or Percona XtraBackup (record the GTID set /
binlog position), MongoDB initial sync, Kafka's follower fetch from the start of the retained log.

**Catch-up math.** The backlog only shrinks if the replica applies faster than the leader
generates. Illustrative numbers for the invoicing app:

```
  snapshot size                 S  = 2 TB
  copy throughput               c  = 500 MB/s     → copy time        = 2,000,000 MB / 500   ≈ 4,000 s (67 min)
  leader WAL generation rate    g  = 20 MB/s      → backlog at end   = 20 × 4,000          = 80 GB
  replica apply rate            a  = 60 MB/s      → net drain rate   = a − g               = 40 MB/s
                                                   → catch-up time    = 80,000 MB / 40      = 2,000 s (33 min)

  WAL that must be retained     ≥ g × (copy time + catch-up time) = 20 × 6,000 = 120 GB, plus margin
  If a ≤ g (e.g. the replica is a smaller instance, or apply is single-threaded, §6.2):
     the replica NEVER catches up. Check a > g before you start.
```

### 2.4 Reads from followers

Followers can serve reads, which is the main reason to have more than one. The price is that every
follower read may be stale by the current lag, and different followers are stale by different
amounts. §6 and §7 are about living with that. Two facts to keep in mind:

- A single follower in a single-leader system always shows a **consistent prefix** of the leader's
  history (§1.1). Staleness is the problem, not garbage.
- Switching between followers, or reading two shards with separate logs, can show a **mixture** of
  prefixes. That is where "time goes backwards" and "reply before question" come from.

### 2.5 Failover and its dangers

Failover is four steps: detect that the leader is gone, choose a replacement, make it the leader,
and move clients to it. Each step has a characteristic way to go wrong.

```
  0 s          ~10 s                ~15 s                    ~20–40 s
  │ leader     │ detector decides   │ most up-to-date        │ clients reconnect: DNS/VIP/proxy
  │ dies (or   │ "dead" (timeout,   │ replica promoted,      │ switch, pools drained, caches of
  │ is only    │ phi, consensus)    │ others re-pointed      │ "who is primary" refreshed
  │ paused)    │                    │                        │
  └────────────┴────────────────────┴────────────────────────┴──────────────►
     RTO ≈ detection + election + promotion + client switchover
```

**Danger 1: losing acknowledged asynchronous writes.** With async replication, the leader
acknowledges a commit before any follower has it. If it dies, whatever the best follower had not
received is gone *after the client was told "committed"*.

```
  Client        Leader (async)                  Follower F1 (lag 400 ms)
    │               │                                  │
    ├─ pay #881 ───►│ commit LSN 900 ── ack ──► client │ has up to LSN 860
    │◄── "paid" ────┤                                  │
    │               │ ✗ disk controller dies           │
    │               │                                  │ promoted: last LSN 860
    │                                                  │ LSN 861..900 never existed here
    ├─ GET #881 ──────────────────────────────────────►│ "unpaid"   ← a confirmed payment vanished

  Expected loss ≈ write rate × lag at failure = 200 writes/s × 0.4 s ≈ 80 writes (illustrative)
```

It gets worse when the lost writes had side effects elsewhere. If the new leader's auto-increment
counter is behind, it **reuses IDs** that the old leader already handed out and that other systems
(caches, search indexes, emails, partners) still reference. GitHub hit exactly this in 2012
(Real-world cases below). Mitigations: semi-synchronous commit so at least one follower has every
acknowledged write (§5); promote only a replica within a lag bound (Patroni's
`maximum_lag_on_failover`, default 1 MB); use IDs that do not depend on a counter surviving
failover (UUIDv7, or a counter bumped past the old maximum on promotion).

**Danger 2: the old leader comes back.** An old leader that was only partitioned or paused (a
40 s GC pause, a VM migration, a hung disk) still believes it is the leader. It may hold
unreplicated writes that must be discarded (`pg_rewind`, or rebuild), and it may accept new
writes if clients still reach it.

**Danger 3: split brain.** Two nodes accept writes at the same time, and the data forks.

```
  time ──────────────────────────────────────────────────────────────────────────►

  Old leader L1   ─── accepting writes ──── (partitioned from F1, still reachable by app servers in zone a)
                        │ writes x=5, invoice 1043 created
  F1 → leader L2         promoted at t=12s ─── accepting writes from app servers in zone b
                                              │ writes x=7, invoice 1043 created (different content!)
  Heal at t=60s:  two histories, same IDs, no automatic way to merge them.
```

Prevention, strongest first (details in `03-consensus-raft-and-distributed-locking.md` §9 and
`../databases/16-failure-detection-and-leader-election.md` §5–§6):

| Mechanism | How it stops the old leader | Weak spot |
|---|---|---|
| Leadership through consensus (Raft/Paxos, or a lease in etcd/ZooKeeper/Consul as Patroni does) | a leader must hold a majority-granted lease or term; a minority side cannot elect or keep a leader | the old leader must *check* its lease before every write and read, and lease expiry assumes bounded clock drift |
| Fencing tokens / epochs checked by the storage | every write carries the leader's term; storage rejects older terms | only works where the storage (or the next hop) checks the token |
| STONITH ("shoot the other node in the head") | power off or detach the old leader's disk via IPMI or the cloud API before promoting | the fencing call itself can fail or time out; you must not promote until it succeeds |
| Old leader self-demotion | leader stops accepting writes when it cannot see a majority for longer than the election timeout | a paused process cannot demote itself; it wakes up and writes once before checking |

**Danger 4: detecting death is guessing.** A timeout that is too short causes failovers for GC
pauses and network blips (each one risking dangers 1–3); one that is too long makes a real crash an
outage. Choosing and tuning the detector is the topic of `29-failure-detection-phi-accrual.md`.
A rule that follows from this chapter: **the more a failover can lose (async replication,
cross-region promotion), the more evidence you should demand before doing it.**

**Danger 5: clients keep talking to the old leader.** DNS TTLs, connection pools with long-lived
connections, and application caches of "the primary's address" keep sending writes to the demoted
node. A demoted PostgreSQL primary restarted as a standby rejects writes (`cannot execute INSERT in
a read-only transaction`), which is the good outcome. Route writes through something that learns
about the promotion (a proxy that checks `pg_is_in_recovery()`, libpq's
`target_session_attrs=read-write` with multiple hosts, or a leader key in the DCS).

---

## 3. Multi-leader replication: topologies, causality, and conflict detection

> **In plain words.** Several replicas accept writes and forward them to each other asynchronously. Every writer gets local latency and keeps working when disconnected, but two leaders can accept conflicting writes to the same item, and writes can arrive at a third leader in an order that breaks cause and effect.
>
> **Real-world example.** A field-service app lets technicians edit work orders offline on tablets. Each tablet is effectively a leader. Two technicians change the same order's status while both are in a basement with no signal; when they reconnect, the server must decide what the order's status is.

### 3.1 When multi-leader is worth its cost

| Use case | Why a single leader does not work | What the "leaders" are |
|---|---|---|
| Multiple data centers or regions | a write in Frankfurt to a leader in Virginia pays ~90 ms RTT and fails when the link is down | one leader per region (topology and placement are in `36-multi-region-active-active-and-geo-replication.md`) |
| Offline-capable clients | the device has no connection at all | each device's local database |
| Collaborative editing | every keystroke would wait for a server round trip | each editor's local document state |
| Migration between clusters | both old and new must take writes during cutover | old and new clusters, briefly |

If none of these applies, the conflicts are pure cost; use a single leader per shard and route
writes to it. Even inside a multi-leader deployment the best practice is to make conflicts rare by
routing all writes for one item to one "home" leader (§14.1), and use multi-leader only for
availability when the home is unreachable.

### 3.2 Topologies and the causality problem

```
  ALL-TO-ALL (mesh)            RING (circular)                 STAR (hub and spoke)
   L1 ◄──────► L2               L1 ──► L2                        L2
   ▲ ╲       ╱ ▲                ▲       │                         │
   │   ╲   ╱   │                │       ▼                  L3 ── L1 ── L4   (L1 = hub)
   │   ╱   ╲   │                L4 ◄── L3                         │
   ▼ ╱       ╲ ▼                                                  L5
   L3 ◄──────► L4
  N(N-1) links; any link can     N links; one dead node         N-1 links; the hub is a
  fail; messages race on         stops propagation for          single point of failure and
  different paths                everyone downstream            adds a hop for spoke-to-spoke
```

Ring and star forward writes through intermediate nodes. Each write is tagged with the node IDs it
has passed through (MySQL uses the originating `server_id` or the GTID's source UUID) so a node
ignores writes it has already seen, which prevents infinite loops.

**All-to-all has a causality problem.** Messages travel on independent links with independent
delays, so a write and a later write that depends on it can arrive at a third leader in reverse
order:

```
  time ─────────────────────────────────────────────────────────────────►

  L1 (EU):   INSERT invoice 1043 (amount 500)
                 │ ────────── slow link (congested) ──────────────────► arrives at L3 at t=900 ms
                 │ ── fast link ──► L2 at t=40 ms
  L2 (US):                          UPDATE invoice 1043 SET status='sent'
                                        │ ── fast link ──► L3 at t=110 ms
  L3 (APAC):                                             UPDATE for a row that does not exist yet
                                                         → error, dropped, or parked?          at t=900 ms the INSERT arrives
```

Timestamps do not fix this: clock skew can make the update look older than the insert. What fixes
it is tracking *causal dependencies*: the update carries "I depend on L1's write #1043" (a version
vector, §12.3, or an explicit dependency), and L3 buffers it until the dependency has been applied.
Ring and star topologies avoid this particular race because writes follow one path, but they pay
with fragility (one dead node or hub stops propagation until the topology is repaired, by hand in
many systems).

### 3.3 Conflict detection

A **write conflict** is two leaders accepting writes to the same item without either having seen
the other's. Types you must plan for:

| Conflict type | Example | Why it is dangerous |
|---|---|---|
| Update-update, same field | two agents set invoice 1043's due date to different days | one value must win or both must be shown |
| Update-update, different fields | one sets `amount`, the other sets `notes` | row-level LWW silently drops one field; per-field merge keeps both |
| Update-delete | one leader edits a line item, another deletes the invoice | resurrect the row, or lose the edit? |
| Insert-insert on a unique key | two regions both register username `acme` | the uniqueness invariant is broken; it cannot be merged, one user must be told |
| Invariant across rows | two regions each book the last free slot for a technician | each write is valid alone; together they violate the constraint |

**When conflicts are detected:**

- **At write time, synchronously** — MySQL Group Replication (multi-primary mode) and Galera
  certify each transaction's write set against concurrent ones through a group-communication round
  before commit; the loser gets an error and can retry. This is really a form of coordination, and
  it pays a round trip per commit. It is not async multi-leader.
- **At replication time, asynchronously** — BDR/PGD-style PostgreSQL multi-master compares incoming
  row changes with local rows (using origin and commit timestamp) and applies a configurable
  resolver; CouchDB keeps a revision tree per document, picks a deterministic winner, and keeps the
  losers as `_conflicts` for the application to resolve; Cosmos DB with multi-region writes applies
  LWW on a chosen property by default or calls a custom merge procedure.
- **At read time** — Riak and the original Dynamo keep all concurrent versions (siblings) and hand
  them to the next reader to merge (§14.3).

How a system tells "concurrent" from "one after the other": a **version vector** per item. Each
leader increments its own entry when it accepts a write. If one vector is greater than or equal to
the other in every entry, that write happened later and simply replaces the older one; otherwise
the writes are concurrent and it is a real conflict (§14.3 has the code). Wall-clock timestamps
cannot make this distinction; they impose an order even on concurrent writes, which is why
timestamp-based systems "resolve" conflicts by silently dropping data (§14.2).

Conflicts that break invariants (unique keys, "no double booking", "balance ≥ 0") **cannot be
merged after the fact**. For those, either route all writes for the constrained item to one
leader (§14.1) or use a consensus-backed operation (§10).

---

## 4. Leaderless replication (Dynamo-style)

> **In plain words.** There is no leader. A coordinator (any node, or the client library) sends each write to all N replicas of the key and calls it done after W confirm; a read asks R replicas and returns the newest version it sees. Replicas that missed a write are fixed later by readers, by hints, and by a background comparison process.
>
> **Real-world example.** A Cassandra cluster stores device telemetry with replication factor 3 and `QUORUM` reads and writes. One node reboots for a kernel patch. Writes keep succeeding on the other two; when the node returns, stored hints replay the 10 minutes of writes it missed, and the weekly repair fixes anything the hints did not cover.

### 4.1 The write and read paths

```
  WRITE (N=3, W=2)                                     READ (N=3, R=2)
  client ──► coordinator                               client ──► coordinator
               │  send to ALL 3 replicas                            │  ask R (or all, use first R)
     ┌─────────┼──────────┐                              ┌──────────┼──────────┐
     ▼         ▼          ▼                              ▼          ▼          ▼
    A ✓       B ✓        C (slow)                       A v2       B v1       (C not asked)
     └────┬────┘                                         └────┬─────┘
   2 acks → "ok" to client; C still gets it later      newest = v2 → return v2
                                                       B is stale → read repair: write v2 to B
```

- The coordinator sends the write to **all N** replicas and waits for W acknowledgments; W controls
  when the client hears "ok", not how many replicas eventually get the write.
- Versions are compared by timestamp (Cassandra: per-cell, microsecond timestamps from the
  coordinator or client, last write wins) or by version vector (Riak, Dynamo: concurrent versions
  kept as siblings).
- Which replicas hold a key comes from consistent hashing: the first N distinct nodes clockwise
  from the key's token form its *preference list* (`10-sharding-and-consistent-hashing.md` §8).

### 4.2 How stale replicas get fixed

| Mechanism | When it runs | What it fixes | What it misses |
|---|---|---|---|
| **Read repair** | on reads that see disagreeing replicas | the replicas that took part in that read | keys that nobody reads |
| **Hinted handoff** | when a replica is down during a write; another node stores a "hint" and replays it on recovery | writes missed during short outages | outages longer than the hint window (Cassandra `max_hint_window`, default 3 h); the hint holder dying |
| **Anti-entropy (Merkle-tree repair)** | scheduled (Cassandra `nodetool repair`, Riak active anti-entropy) | everything, eventually | expensive; must run within the tombstone grace period (Cassandra `gc_grace_seconds`, default 10 days) or deleted data can come back |

Read repair comes in two forms, and the difference matters for consistency (§9):
**blocking** (the coordinator writes the newest value back to the stale replicas it contacted
*before* answering the client; Cassandra does this for reads above `ONE` that find a mismatch,
controlled in 4.0 by the table option `read_repair`, `BLOCKING` by default) and **asynchronous**
(answer first, repair in the background). Only the blocking form helps make quorum reads monotonic.

Internals of Merkle-tree comparison, hint storage, and sloppy-quorum mechanics are in
`../databases/19-distributed-databases-deep-dive.md` §8.

### 4.3 Tunable consistency, per request

Cassandra lets each request pick how many replicas to involve. The names map directly to R and W:

| Consistency level | Replicas that must answer (N = 3 per DC) | Notes |
|---|---|---|
| `ONE` / `TWO` / `THREE` | 1 / 2 / 3 | fastest; `ONE` read + `ONE` write gives no overlap |
| `QUORUM` | ⌊total N across DCs / 2⌋ + 1 | overlaps with any other `QUORUM`; crosses DCs |
| `LOCAL_QUORUM` | majority in the coordinator's DC | overlap only among requests in the same DC |
| `EACH_QUORUM` (writes) | a majority in every DC | a DC outage blocks writes |
| `ALL` | all N | no failure tolerance |
| `SERIAL` / `LOCAL_SERIAL` | Paxos round (lightweight transactions) | linearizable compare-and-set per partition, ~4 round trips |

The overlap rule of §8.1 tells you which pairs are "strong enough": `QUORUM`+`QUORUM`,
`ONE` write + `ALL` read, `ALL` write + `ONE` read. Anything else can return stale data even with
no failures at all, which the simulation in §8.8 quantifies.

---

## 5. Synchronous, asynchronous, and semi-synchronous replication

> **In plain words.** The only question is: when the database says "committed", how many copies exist, and how durable are they? Waiting for more copies protects against more failures and costs at least one network round trip per commit; waiting for a copy that is down means not committing at all.
>
> **Real-world example.** The invoicing app switches from async to "wait for any 1 of 2 in-region replicas to flush". Commits go from about 0.5 ms to about 1.5 ms; a primary crash can no longer lose an acknowledged payment; if both replicas die, writes stop until someone intervenes.

### 5.1 The three modes and what they buy

```
  ASYNC                         SEMI-SYNC (k of N)                 SYNC (all)
  client  leader  F1  F2        client  leader  F1  F2             client  leader  F1  F2
    │──w──►│       │   │          │──w──►│       │   │               │──w──►│       │   │
    │      │fsync  │   │          │      │fsync  │   │               │      │fsync  │   │
    │◄─ok──│       │   │          │      │──────►│   │               │      │──────►│   │
    │      │──────►│   │          │      │──────────►│               │      │──────────►│
    │      │──────────►│          │      │◄──ack─│   │ (1st ack)     │      │◄──ack─│   │
    │      │       │   │          │◄─ok──│       │   │               │      │◄──────ack─│
                                                                     │◄─ok──│
  ack after local durability   ack after k replicas confirm       ack after every replica confirms
  RPO = lag at crash           RPO = 0 if ≤ k-1 extra failures     RPO = 0
  a dead replica: no effect    a dead replica: fine while k remain one dead replica: writes stop
```

Commit latency, ignoring queueing:

$$t_{commit} \approx \max\left(t_{fsync}^{local},\ \text{k-th fastest of } \{RTT_i + t_{fsync,i}^{remote}\}\right)$$

```
  Illustrative numbers (local fsync 0.5 ms, remote fsync 0.5 ms):
    async                                    ≈ 0.5 ms
    semi-sync, replica in same AZ (RTT 0.3)  ≈ 0.3 + 0.5          ≈ 0.8 ms
    semi-sync, replica in other AZ (RTT 1.5) ≈ 1.5 + 0.5          ≈ 2.0 ms
    semi-sync, replica in other region (70)  ≈ 70 + 0.5           ≈ 70.5 ms

  One connection committing serially:  1 / t_commit
    0.5 ms → 2,000 commits/s;   2 ms → 500 commits/s;   70.5 ms → ~14 commits/s
  Many connections: group commit lets concurrent commits share one wait, so total throughput
  falls far less than per-connection throughput. Batch or parallelize writers if you add a
  cross-region synchronous replica.
```

Two consequences of the `k-th fastest` term: waiting for **any** k of several replicas hides a
slow or dead one (order statistics, §8.5); waiting for a **named** replica does not.

### 5.2 PostgreSQL: `synchronous_commit` and `synchronous_standby_names`

PostgreSQL makes the wait per transaction (`synchronous_commit`) and the set of replicas that
count cluster-wide (`synchronous_standby_names`). The remote levels only mean something when
`synchronous_standby_names` is non-empty; otherwise they behave like `local`.

| `synchronous_commit` | Commit returns after | Survives primary crash? | Survives primary loss + standby OS crash at once? | Read-your-writes on the sync standby? |
|---|---|---|---|---|
| `off` | WAL written to the OS later by the WAL writer | may lose the last few hundred ms of commits (up to 3 × `wal_writer_delay`) even with no failover; no corruption | no | no |
| `local` | primary's WAL flushed | yes locally; a failover loses unreplicated commits | no | no |
| `remote_write` | standby received the WAL and wrote it to its OS (not fsynced) | yes, unless the standby's OS also crashes | no | no |
| `on` (default) | standby flushed the WAL to disk | yes | yes | no: flushed is not yet replayed |
| `remote_apply` | standby replayed the WAL; changes visible to its queries | yes | yes | **yes**, on that standby |

Which standbys count:

```
  synchronous_standby_names = 'FIRST 1 (r1, r2)'   -- priority: wait for r1; if r1 is down, r2 takes over
  synchronous_standby_names = 'ANY 1 (r1, r2)'     -- quorum: wait for whichever answers first (PG 10+)
  synchronous_standby_names = 'ANY 2 (r1, r2, r3)' -- wait for any 2 of 3
  synchronous_standby_names = 'r1, r2'             -- old syntax, same as FIRST 1 (r1, r2)
  synchronous_standby_names = ''                   -- async
```

`ANY` is almost always what you want for latency: commit time becomes the fastest of the listed
replicas, and one slow replica does not slow every commit. `FIRST` is useful when one replica is
special (for example the only one in another AZ that you require to hold every commit).

Behavior that surprises people:

- **No automatic fallback.** If fewer than the required standbys are connected, commits *wait
  indefinitely*. This is the correct durability behavior and an availability hazard. Tools such as
  Patroni's `synchronous_mode` manage the list so that it only contains healthy standbys, and can be
  told whether to allow degrading to async (`synchronous_mode_strict`).
- **Cancelling a waiting commit does not undo it.** The transaction is already committed locally; if
  the client cancels or the connection drops while waiting for the standby, the primary reports a
  warning and the data *is visible on the primary* but may not be on any standby. An application
  that retries on timeout must be idempotent (see `06-distributed-transactions-sagas-outbox-idempotency.md`).
- **Per-transaction choice.** `SET LOCAL synchronous_commit = off` inside a transaction that writes
  an unimportant row (a "last seen" timestamp) skips the wait for that transaction only. Use it
  deliberately: it is an explicit statement that losing this row is acceptable.
- **`remote_apply` is the only level that gives read-your-writes on a standby for free**, and it
  couples commit latency to the standby's *replay* speed, including replay stalls from query
  conflicts (§6.2).

### 5.3 MySQL semi-synchronous replication

MySQL's semi-sync plugin (names changed to `source`/`replica` in 8.0.26) makes the source wait for
at least `rpl_semi_sync_source_wait_for_replica_count` (default 1) replicas to acknowledge that they
**received and wrote the event to their relay log**. That is "durably received", not "applied".

| Setting | Behavior | The trap |
|---|---|---|
| `rpl_semi_sync_source_wait_point = AFTER_SYNC` (default since 5.7, "lossless") | the source syncs the binlog, waits for the replica ack, *then* commits in the storage engine; other sessions cannot see the transaction until a replica has it | none of the below |
| `rpl_semi_sync_source_wait_point = AFTER_COMMIT` | commits in the engine first, then waits; other sessions already see the transaction | on crash and failover, data that other users *saw* can disappear ("phantom read") |
| `rpl_semi_sync_source_timeout` (default 10,000 ms) | if no ack arrives in time, the source **silently switches to asynchronous** and keeps committing | durability quietly degrades exactly when the network or replica is struggling; alert on `Rpl_semi_sync_source_status = OFF` |

The timeout is the key design difference from PostgreSQL: MySQL chooses availability (fall back to
async), PostgreSQL chooses durability (block). Neither is wrong, but you must know which one you have.

### 5.4 Kafka: `acks`, the ISR, and `min.insync.replicas`

Kafka's partitions are single-leader logs. The leader tracks the **in-sync replicas (ISR)**: the
followers that have caught up to its log end within `replica.lag.time.max.ms` (30 s default in
recent versions).

| Producer `acks` | Leader acknowledges after | Loss window |
|---|---|---|
| `0` | nothing (fire and forget) | anything in flight |
| `1` | the leader appended to its log | leader dies before followers fetch: acknowledged records lost |
| `all` (`-1`) | **every current ISR member** has the record | none while ISR ≥ `min.insync.replicas` and unclean election is off |

`acks=all` alone is not enough: if the ISR shrinks to the leader alone, "all ISR" means "the
leader", and `acks=all` behaves like `acks=1`. `min.insync.replicas=2` makes the leader reject
`acks=all` writes with `NotEnoughReplicas` when fewer than 2 replicas are in sync. With
`unclean.leader.election.enable=false` (the default since 0.11), an out-of-sync replica is never
elected leader, so acknowledged records are not truncated away.

| RF | `min.insync.replicas` | Broker failures before `acks=all` writes stop | Failures an acknowledged write survives |
|---|---|---|---|
| 3 | 1 | 2 | 0 (ISR may be the leader alone) |
| 3 | 2 | 1 | 1 |
| 3 | 3 | 0 | 2 |
| 5 | 3 | 2 | 2 |

Also note that Kafka does not fsync each record by default; durability comes from having the
record in the page cache of several brokers, which is a bet on independent failures. Producer
settings, idempotence, and ISR shrink/unclean election incidents are in
`07-kafka-and-event-streaming.md` §3.5, §3.6 and §12.2.

### 5.5 Chain replication

Chain replication (van Renesse and Schneider, OSDI 2004) orders replicas in a line. Writes enter
at the head and flow to the tail; the tail acknowledges; reads go to the tail.

```
  write ──► HEAD ──► MID ──► TAIL ──► ack to client
  read  ─────────────────────► TAIL ──► value     (the tail only has fully replicated writes)

  Count one-way network hops per write, h = one-way latency:
    Primary-backup fan-out: client→P, P→backups (parallel), backups→P, P→client  = 4h for any N
    Chain of N replicas:    client→head, N−1 hops down the chain, tail→client   = (N+1)h
      N = 3: 4h vs 4h (same);   N = 5: 4h vs 6h;   plus every node's fsync is on the path in series
```

Why use it: reads from the tail are linearizable without a separate read protocol, the head does
less network work (one outgoing copy instead of N−1), and failure handling is simple *given* an
external configuration master (usually a consensus service) that tells nodes their position.
Costs: latency grows with chain length, and one slow node slows every write. CRAQ (Terrace and
Freedman, USENIX ATC 2009) lets any node serve reads for "clean" objects and asks the tail only for
objects with in-flight writes. HDFS's write pipeline has the same shape. More detail:
`../databases/19-distributed-databases-deep-dive.md` §6.1.

### 5.6 Choosing a mode

| Mode | RPO | Commit latency | A replica fails | Typical fit |
|---|---|---|---|---|
| Async | lag at crash (ms – s) | local fsync | no effect | read replicas, cross-region DR copy, analytics |
| Semi-sync, `ANY 1` of 2 in-region | 0 (single failure) | + 1 in-region RTT | no effect while one remains | default for OLTP holding money or orders |
| Semi-sync with async fallback (MySQL default) | 0 normally, lag after fallback | + 1 RTT | falls back to async after the timeout | when blocking writes is worse than a small loss window |
| Sync to all | 0 | slowest replica's RTT | writes stop | rarely; small clusters with manual ops |
| Quorum / consensus (Raft, Paxos, `acks=all` + minISR) | 0 with ≤ f failures | majority's RTT | no effect while a majority remains | metadata, coordination, logs, distributed SQL |
| Cross-region sync | 0 including region loss | + cross-region RTT (30–150 ms) | per quorum placement | ledgers that must survive a region; see `36-multi-region-active-active-and-geo-replication.md` |

---

## 6. Replication lag: causes, measurement, and anomalies

> **In plain words.** Lag is the distance between what the leader has committed and what a follower shows. It is usually milliseconds, and it spikes to seconds or minutes for predictable reasons: one huge transaction, a follower that applies changes one at a time, a slow network, or queries on the follower that block replay. Measure it at every stage of the pipeline, in both bytes and seconds, and measure "how old is the newest data a reader can see" directly with a heartbeat.
>
> **Real-world example.** The invoicing app's nightly report runs a 20-minute query on replica r2. Replay on r2 needs to remove row versions that query still uses, so replay pauses for up to `max_standby_streaming_delay` (30 s), then cancels the report. Lag on r2 saw-tooths between 0 and 30 s all night, and every user routed to r2 sees half-minute-old invoices.

### 6.1 Anatomy: lag is a pipeline, not a number

```
  PRIMARY                                                    STANDBY
  pg_current_wal_lsn()  0/9A000000  ── generated
        │ walsender reads WAL and sends
        ▼
  sent_lsn              0/99F00000  ── sent ─────────────►   walreceiver
                                                                │ write()
  write_lsn             0/99E00000  ◄─ reported ──────────   written to standby OS
                                                                │ fsync()
  flush_lsn             0/99D00000  ◄─ reported ──────────   durable on standby
                                                                │ startup process replays
  replay_lsn            0/95000000  ◄─ reported ──────────   VISIBLE to standby queries

  generated − sent    large → walsender or network cannot keep up (bandwidth, TLS CPU)
  sent − flush        large → network latency or the standby's disk
  flush − replay      large → apply bottleneck: single-threaded replay, I/O misses, query conflicts
```

Durability cares about `flush_lsn` (what survives if the primary dies now). Readers care about
`replay_lsn` (what a query on the standby can see). They can differ by minutes. The "lag" metric on
a dashboard is often only one of these; know which.

### 6.2 Causes of lag

| Cause | Mechanism | Symptom | Fix or mitigation |
|---|---|---|---|
| Large or long transactions | MySQL writes a transaction to the binlog at commit, so a 10-minute batch ships all at once and the replica then spends minutes applying it; PostgreSQL streams WAL as it is generated but the rows become visible only when the commit record is replayed, and replaying gigabytes takes time | lag is ~0, then jumps by minutes; later small transactions queue behind the big one | batch big updates into chunks of a few thousand rows with commits in between |
| Single-threaded apply | PostgreSQL's startup process replays WAL in one process, while the primary wrote it with hundreds of backends in parallel; MySQL applies in parallel only transactions it can prove independent (`replica_parallel_workers`, default 4 since 8.0.27, with write-set dependency tracking) | lag grows steadily under sustained write load even though the replica's CPU looks mostly idle (one core busy) | faster replica storage; PostgreSQL 15+ `recovery_prefetch`; more MySQL workers; spread hot rows; shard |
| Replica I/O misses | replay must read each page it modifies; a replica with a colder cache or fewer IOPS than the primary is slower at the same work | replay_lsn lags while flush_lsn is current | size replicas like the primary; do not put the failover target on a smaller instance |
| Network | cross-region bandwidth; WAL volume spikes after each checkpoint because of full-page images (`full_page_writes`) | sent_lsn lags; lag correlates with checkpoints | `wal_compression`; longer checkpoint intervals; more bandwidth |
| Query conflicts on a hot standby | replay wants to remove row versions a standby query still needs (after VACUUM on the primary), or needs an `ACCESS EXCLUSIVE` lock (DDL, or VACUUM truncating empty pages at the end of a table) that a standby query blocks | replay pauses up to `max_standby_streaming_delay` (default 30 s), then cancels the query with "canceling statement due to conflict with recovery"; `pg_stat_database_conflicts` counts them | separate replicas for long analytics queries; `hot_standby_feedback = on` (prevents cleanup conflicts but causes bloat on the primary); `vacuum_truncate = off` on affected tables |
| `max_standby_streaming_delay = -1` | replay waits forever for conflicting queries | unbounded lag on a replica that also serves user reads | never use -1 on a replica in the user read pool |
| Logical replication apply | a subscription has a single apply worker by default (PostgreSQL 16 adds parallel apply for large streamed transactions) | subscriber lags on write-heavy tables | several subscriptions over disjoint tables; `streaming = parallel` where available |
| Bulk operations | index builds, `VACUUM FULL`, big `COPY`, backfills generate WAL faster than replicas apply | lag spikes during maintenance windows | throttle backfills on replica lag (pause when lag > threshold) |

A useful rule for backfills and batch jobs: **the job should read replica lag and slow down when
it grows**, the same way a well-behaved client backs off under load
(`34-adaptive-load-control-and-backpressure.md`). GitHub's `gh-ost` online schema change tool
throttles on replica lag for exactly this reason.

### 6.3 Measuring lag

**PostgreSQL, on the primary** (one row per connected standby; PostgreSQL 10+ adds the time
columns):

```sql
SELECT application_name, state, sync_state,
       pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn)  AS send_backlog_bytes,
       pg_wal_lsn_diff(sent_lsn, flush_lsn)            AS in_flight_bytes,
       pg_wal_lsn_diff(flush_lsn, replay_lsn)          AS replay_backlog_bytes,
       write_lag, flush_lag, replay_lag                -- intervals
FROM pg_stat_replication;
```

`write_lag`, `flush_lag` and `replay_lag` measure how long it took recent WAL to reach each stage
after the primary flushed it. When the primary goes idle and the standby has caught up, they show
the last measured value briefly and then become `NULL`. And a standby that disconnects simply
**disappears from the view**: alert on the expected number of rows, not only on the values.

**PostgreSQL, on the standby:**

```sql
SELECT pg_is_in_recovery()                                   AS is_standby,
       pg_last_wal_receive_lsn()                             AS received,
       pg_last_wal_replay_lsn()                              AS replayed,
       pg_wal_lsn_diff(pg_last_wal_receive_lsn(),
                       pg_last_wal_replay_lsn())             AS replay_backlog_bytes,
       now() - pg_last_xact_replay_timestamp()               AS since_last_replayed_commit;
```

The classic pitfall: `now() - pg_last_xact_replay_timestamp()` is the time since the last
*replayed commit*, not lag. On an idle primary it grows by one second per second although the
standby is fully caught up, which pages someone at 3 a.m. The common patch
(`CASE WHEN received = replayed THEN 0 ...`) hides real lag when the receiver itself is behind
because of the network. The robust answer is a heartbeat.

**MySQL.** `SHOW REPLICA STATUS` reports `Seconds_Behind_Source` (formerly
`Seconds_Behind_Master`). It is computed from the timestamp of the event the applier is currently
executing, compared with the replica's clock (corrected by the clock difference measured when the
replica connected). Its pitfalls:

| Pitfall | Consequence |
|---|---|
| It measures the **applier** only | if the receiver thread is behind (slow network), the applier catches up with the relay log and reports **0** while the replica is minutes behind the source |
| It is `NULL` when a replication thread is stopped | a broken replica is not "0 lag"; alert on `NULL` and on `Replica_IO_Running`/`Replica_SQL_Running` |
| It jumps with long transactions | shows 0, then the full age of the transaction's events at once |
| It depends on clocks | a clock change after connection skews it |

Better MySQL signals: `performance_schema.replication_applier_status_by_worker` (8.0) exposes each
worker's last applied transaction with its original commit timestamp on the source and the apply
end time; comparing `gtid_executed` sets (`GTID_SUBTRACT`) tells you exactly which transactions a
replica is missing.

**Heartbeat tables work everywhere** (and are what Percona's `pt-heartbeat` does): the primary
writes the current time into a one-row table every second; each replica reports how old the
replicated value is. This measures what readers actually experience, "how old is the newest data I
can see", including when the primary is idle, through cascading replicas, and through logical
replication.

```sql
-- once
CREATE TABLE replication_heartbeat (id int PRIMARY KEY, ts timestamptz NOT NULL);

-- on the primary, every 1 s (pg_cron, a sidecar, or the app's scheduler)
INSERT INTO replication_heartbeat VALUES (1, clock_timestamp())
ON CONFLICT (id) DO UPDATE SET ts = excluded.ts;

-- on each replica: an upper bound on staleness, accurate to about 1 s + clock skew
SELECT clock_timestamp() - ts AS staleness FROM replication_heartbeat WHERE id = 1;
```

Alert on two different things, because they fail differently:

| Signal | Unit | Why it matters | Example threshold (illustrative) |
|---|---|---|---|
| Reader staleness (heartbeat age, `replay_lag`) | seconds | user-visible anomalies, read-your-writes windows | page if > 5 s for 5 min on a replica in the read pool; remove it from the pool at > 10 s |
| Backlog (bytes between flush and replay, or retained WAL) | bytes | catch-up time, failover data loss, primary disk filling via slots | warn when `backlog / apply rate` > 10 min, or retained WAL > 50% of free disk |

Replica freshness as an SLI is covered in `../sre-observability/23-database-observability.md` §7;
step-by-step diagnosis of a lag incident is in `37-distributed-systems-debugging.md`.

### 6.4 The anomalies lag causes, as timelines

Every anomaly below happens with **zero failures**, only lag. Section 7 gives the fix for each.

**(a) Reading your own write fails.**

```
  User           Primary                 Replica r2 (lag 3 s)
   │── UPDATE invoice 1042 amount=950 ─►│ commit @ LSN 500
   │◄─ 200 OK, redirect to /invoices ───│
   │── GET /invoices ──────────────────────────────────────►│ replayed up to LSN 460
   │◄──────────────────────────── amount = 900 ─────────────│  "my change was lost"
```

**(b) Time goes backwards (non-monotonic reads).**

```
  User            Replica r1 (lag 30 ms)      Replica r2 (lag 6 s)
   │── GET total ─►│ 12,400 (includes payment P)
   │── GET total ────────────────────────────►│ 12,350 (P not replayed yet)
   │   "the payment disappeared"
```

**(c) Effect before cause (consistent prefix violated).** Question and answer live on different
shards, each with its own lag.

```
  Client asks Q  ──► shard A primary ──► shard A replica (lag 4 s)
  Accountant answers A (after reading Q) ──► shard B primary ──► shard B replica (lag 20 ms)

  Observer reading both replicas 1 s later:
      shard B replica: "Approved — see question above"
      shard A replica: (no question yet)
```

**(d) A stale read drives a write.** This is the dangerous one, because the result is persisted.

```
  Request 1                         Primary                   Replica (lag 50 ms)
   │ SELECT used FROM coupons ──────────────────────────────►│ used = false
   │                                │◄── UPDATE coupons SET used = true (Request 2, 20 ms earlier)
   │ INSERT redemption ─────────────►│ second redemption of a single-use coupon

  Same shape: read balance 100 on a replica, compute 100 - 30, write 70 to the primary,
  overwriting a deposit that the replica had not seen yet (a lost update).
```

Rule: **never read-modify-write through a replica**. Do the read on the primary in the same
transaction as the write, or make the write conditional (`UPDATE ... WHERE used = false`,
`UPDATE ... SET balance = balance - 30 WHERE balance >= 30`), and check the affected row count.

**(e) Failover rewinds what users already saw.** A user reads "paid" from the old primary; the
primary dies; the promoted replica was 400 ms behind and never got that write. The user now sees
"unpaid", and no session guarantee can help, because the data is gone (§2.5). Only
semi-synchronous commit (§5) prevents it.

---

## 7. Session guarantees and how to implement them

> **In plain words.** Most complaints about replica reads come from one user seeing their own world behave strangely: their edit vanishes, a number goes backwards, a reply shows up before its question. Four "session guarantees" (plus consistent prefix) fix exactly this, per user, without making the whole system strongly consistent. The best implementation is a small token (a log position) that travels with the user's requests.
>
> **Real-world example.** After adding a 50-line LSN-token router, the invoicing app keeps 95% of reads on replicas; the 5% of reads that come right after a write either wait a few milliseconds for a replica to catch up or fall back to the primary, and "my edit disappeared" tickets stop.

### 7.1 The guarantees

The four session guarantees come from the Bayou project (Terry et al., "Session Guarantees for
Weakly Consistent Replicated Data", 1994). Consistent prefix is from Terry's later "Replicated Data
Consistency Explained Through Baseball" (2013).

| Guarantee | Promise (within one session) | Violated in §6.4 by | Automatic with a single leader? |
|---|---|---|---|
| **Read-your-writes** | a read reflects every earlier write of this session | (a) | only if you read from the leader |
| **Monotonic reads** | a read never reflects an older state than an earlier read of this session | (b) | only if you stay on one replica |
| **Monotonic writes** | this session's writes are applied everywhere in the order issued | reordering across leaders or client retries | yes, if the client does not send them concurrently |
| **Writes-follow-reads** | a write issued after reading X is ordered after X everywhere | a reply applied where the post is not | yes (the leader already has anything a replica showed) |
| **Consistent prefix** | reads see some prefix of the write history, never a gap | (c) | per shard yes; across shards no |

These are **per session**. Two different users can still see different states at the same
moment. That is almost always acceptable for user experience, and never acceptable for
invariants that involve other users' writes (coupons, stock, balances), which need §10.

### 7.2 Read-your-writes: five implementations

**(1) Read from the leader for `T_pin` seconds after a write.** Store `last_write_at` in the
session; route reads to the primary while `now − last_write_at < T_pin`.

```
  Choose T_pin above the lag you are willing to tolerate, e.g. the p99.9 of replica staleness.
  Cost: the fraction of reads that land on the primary ≈ fraction of active sessions that wrote
        in the last T_pin seconds.
      5,000 active sessions, 400 of them wrote in the last 5 s → 8% of reads go to the primary.
  Breaks when: actual lag > T_pin (the nightly 8 s spike vs a 5 s pin), silently.
```

Keep `last_write_at` in a **server-side session store keyed by user**, not a device cookie, or the
user's phone will not know about the write they just made on their laptop.

**(2) Read "my own things" from the leader.** Profile pages, settings, a user's own drafts: route by
resource ownership. Simple and robust; the cost is primary load proportional to how much of your
traffic is self-reads.

**(3) Log-position tokens (LSN, GTID, cluster time).** After a write, remember the log position of
the commit. For a read, use any replica that has replayed at least that far; if none has within a
small wait, use the primary. This is exact (no guessing a `T_pin`), survives replica churn, and
extends to monotonic reads by also recording the position after each read.

- PostgreSQL write side: immediately after `COMMIT`, in the same session, `SELECT
  pg_current_wal_insert_lsn()`. That position is at or after the end of your commit record.
  (`pg_current_wal_lsn()` returns the *write* position, which is also past your commit when
  `synchronous_commit` is `local` or stronger, but can be behind it under `synchronous_commit = off`.)
- PostgreSQL read side: `SELECT pg_last_wal_replay_lsn() >= $1::pg_lsn` on the replica; the `pg_lsn`
  type compares correctly. Unless your PostgreSQL version provides a server-side wait-for-LSN
  command (proposals have appeared in recent development cycles; check your version's
  documentation), poll.
- MySQL: capture the transaction's GTID (`session_track_gtids = OWN_GTID` returns it in the OK packet),
  then on the replica `SELECT WAIT_FOR_EXECUTED_GTID_SET('<gtid-set>', 0.05)`, which returns 0 once
  applied and 1 on timeout. ProxySQL 2.x can route by GTID automatically.
- MongoDB: causally consistent sessions carry `operationTime`/`clusterTime` and send
  `afterClusterTime` on reads (§12.4).
- Cosmos DB: the session token (`x-ms-session-token`) returned on writes and sent on reads (§13.4).

A minimal router, runnable with the standard library (the fake primary and replicas stand in for
the SQL calls noted in the comments):

```python
import random
import time

def parse_pg_lsn(text: str) -> int:
    """'16/B374D848' -> int, so LSN tokens compare with >=."""
    hi, lo = text.split("/")
    return (int(hi, 16) << 32) | int(lo, 16)

class FakePrimary:
    name = "primary"
    def __init__(self):
        self.lsn, self.history = 0, []            # history: (commit time, lsn)
    def commit(self, nbytes: int = 200) -> int:
        self.lsn += nbytes
        self.history.append((time.monotonic(), self.lsn))
        return self.lsn
    def position(self) -> int:                    # real: SELECT pg_current_wal_insert_lsn()
        return self.lsn

class FakeReplica:
    def __init__(self, name, primary, lag_s):
        self.name, self.primary, self.lag_s = name, primary, lag_s
    def position(self) -> int:                    # real: SELECT pg_last_wal_replay_lsn()
        cutoff = time.monotonic() - self.lag_s
        return max((l for t, l in self.primary.history if t <= cutoff), default=0)

class Router:
    def __init__(self, primary, replicas, max_wait_s=0.05, poll_s=0.005):
        self.primary, self.replicas = primary, replicas
        self.max_wait_s, self.poll_s = max_wait_s, poll_s

    def write(self, session: dict) -> None:
        self.primary.commit()                     # real: run the txn, COMMIT, then read position
        session["min_lsn"] = max(session.get("min_lsn", 0), self.primary.position())

    def read(self, session: dict, query):
        need = session.get("min_lsn", 0)
        deadline = time.monotonic() + self.max_wait_s
        node = None
        while node is None:
            ready = [r for r in self.replicas if r.position() >= need]
            if ready:
                node = random.choice(ready)
            elif time.monotonic() >= deadline:
                node = self.primary               # bounded wait, then fall back
            else:
                time.sleep(self.poll_s)
        result = query(node)
        # Measured AFTER the query, so it covers everything the read could have seen.
        session["min_lsn"] = max(need, node.position())   # gives monotonic reads too
        return result

if __name__ == "__main__":
    assert parse_pg_lsn("16/B374D848") == 0x16B374D848
    p = FakePrimary()
    router = Router(p, [FakeReplica("fast", p, 0.01), FakeReplica("slow", p, 2.0)])
    s = {}
    router.write(s)
    print([router.read(s, lambda n: n.name) for _ in range(5)])  # never 'slow'
    router.max_wait_s = 0.0
    router.write(s)
    print(router.read(s, lambda n: n.name))       # no replica caught up, no wait: 'primary'
```

Design notes for production:

- **Where the token lives.** In the server-side session (covers all of a user's devices), or in a
  response header or cookie that the client echoes (works for stateless APIs). A token that is
  dropped by one hop (a gateway, a background job, a second service) silently turns the guarantee
  off.
- **Bound the wait and count the fallbacks.** A 50–100 ms wait covers normal lag; a rising fallback
  rate is an early warning that a replica is falling behind and that primary load is about to rise.
- **Failover.** After a failover that lost writes, a session's token can be *ahead of the new
  primary*: no node will ever reach it. Detect `token > primary position` and reset the token (and
  accept that the user will see data go backwards, because it did).
- **Measure after the query** for monotonic reads, as the code does: the replica may advance between
  the check and the query, and the token must cover whatever the query saw.

**(4) Make the replica wait at commit time.** PostgreSQL `synchronous_commit = remote_apply` with
*every* read replica required by `synchronous_standby_names` (for two replicas, `ANY 2 (r1, r2)`,
not `ANY 1`): every commit returns only after those replicas have replayed it, so any later read on
them sees it. Zero routing logic; every commit pays the
replicas' replay latency and stalls when a replica stalls (§5.2).

**(5) Sticky sessions to the leader after a write** (a load-balancer cookie). Equivalent to (1) with
the pin enforced at the proxy. It breaks on the same things (lag longer than the pin, second
device) and adds load-balancer state.

### 7.3 Monotonic reads

| Implementation | How | Breaks when |
|---|---|---|
| Pin session to a replica | `replica = replicas[hash(user_id) % len(replicas)]`; the same user lands on the same replica from every device | the replica fails, is removed, or the pool is resized: the user moves to another replica, maybe one that is further behind, with no error; load is uneven |
| Highest-position token | record the position after each read (code above); only use replicas at or past it | same failover caveat as §7.2 |
| Read at a timestamp | read "as of" a timestamp no newer than every replica has (CockroachDB follower reads at a closed timestamp, Spanner stale reads); keep the session's timestamp non-decreasing | the timestamp is too fresh for a lagging replica, so it must wait or redirect |

Pinning by hash is a *scheduling* choice that happens to give monotonic reads while the topology
is stable; the token makes it a *guarantee*. Use the hash for cache locality and the token for
correctness.

### 7.4 Monotonic writes

With one leader, a session's writes are applied in the order the leader received them. Two things
still break it:

- **The client sends writes concurrently.** A browser that fires "create invoice" and "add line
  item" as two parallel requests has no order; the second may arrive first. Either serialize them
  on the client (send the second after the first is acknowledged) or make the server reject writes
  whose predecessor is missing (a per-session sequence number, or `UPDATE ... WHERE version = 7`).
- **Different writes go to different leaders** (multi-leader, or a DNS switch mid-session). A
  third leader can receive "edit invoice" before "create invoice" (§3.2). Fix: route a session's
  writes to one home leader, or attach version vectors so the edit waits for the create.

### 7.5 Writes-follow-reads

"If I read X and then write Y, anyone who sees Y also sees X." With a single leader it holds
automatically: the replica that showed X is a prefix of the leader, so the leader already has X
when Y arrives, and every replica applies X before Y. It breaks when X and Y live in different
logs (different shards or regions). The fix is to carry the read's position as a dependency of the
write, and to make readers of Y wait until X is visible where they read. That is causal consistency
(§12), limited to one session.

### 7.6 Consistent prefix

One log replayed in order is always a consistent prefix, so a single replica of a single shard
never shows the "answer before question" anomaly. It appears when a read combines data from two
logs (two shards, two regions, a database plus a search index). Options, cheapest first:

1. **Put causally related data in one partition.** Partition comments by `thread_id`, not by
   `comment_id`; question and answer then share a log.
2. **Read both at one snapshot timestamp.** Systems with global timestamps (Spanner, CockroachDB,
   YugabyteDB) can read several ranges at the same timestamp.
3. **Track dependencies** (§12): the answer carries "depends on question Q", and a reader that
   has the answer but not Q either waits or hides the answer.

### 7.7 The guarantee must cover the whole read path

The database can be perfectly session-consistent while the user still sees stale data, because the
read came from somewhere else:

| Layer | How it breaks the guarantee | Fix |
|---|---|---|
| Cache (Redis, in-process) | serves a value older than the user's write | invalidate on write and version-check entries against the token, or bypass the cache for the pinned window (`08-caching-strategies-and-patterns.md` §3) |
| Search index fed by CDC | the index consumer lags the database | show the user's own new item from the primary merged into search results, or say "indexing"; do not pretend |
| CDN / HTTP cache | serves a cached page | `Cache-Control: private, no-store` on pages that show the user's own recently changed data |
| Another service's replica | the token was not forwarded | propagate the token in request context like a trace ID |

### 7.8 Summary: cost and failure of each implementation

| Guarantee | Cheapest mechanism | Extra cost | How it fails |
|---|---|---|---|
| Read-your-writes | leader reads for `T_pin` after a write | leader read load | lag > `T_pin`; second device if the flag is per device |
| Read-your-writes | LSN/GTID token | one field; occasional wait or fallback | token not propagated; reset needed after lossy failover |
| Read-your-writes | `remote_apply` | commit latency tied to replica replay | replica stall stalls commits |
| Monotonic reads | hash pinning | uneven load | pool changes, silently |
| Monotonic reads | highest-seen token | small | as above |
| Monotonic writes | single leader + serialized client | client-side ordering | parallel requests, multi-leader |
| Writes-follow-reads | single leader | none | multi-shard or multi-region data |
| Consistent prefix | co-locate related data in one partition | constrains partitioning | reads spanning partitions or derived stores |

---

## 8. Quorum math: overlap, availability, latency, and staleness

> **In plain words.** With N copies, if every write waits for W of them and every read asks R of them, and W + R is more than N, then any read group and any write group share at least one copy, so a read can always find the latest completed write. The same simple counting tells you how many failures reads and writes survive, how likely an operation is to be available, and why waiting for "2 of 3" has a much better worst case than waiting for "all 3".
>
> **Real-world example.** With 3 replicas that are each down 1% of the time (independently), a "2 of 3" operation is available 99.97% of the time, and "all 3" only 97.03%. If each replica has a 1% chance of a 100 ms hiccup on a request, "2 of 3" has a p99 of 2 ms while "all 3" has a p99 of 100 ms.

Throughout this section "quorum" means a **strict quorum**: W and R are counted among the key's
fixed set of N replicas (its preference list), not among whichever nodes happen to be reachable.
§8.7 shows what goes wrong otherwise.

### 8.1 Overlap: why R + W > N makes reads find the last write

Let `S_W` be the set of replicas that acknowledged a completed write and `S_R` the set that answered
a later read. Both are subsets of the same N replicas. By inclusion–exclusion:

$$|S_W \cap S_R| \;=\; |S_W| + |S_R| - |S_W \cup S_R| \;\ge\; W + R - N$$

because the union cannot contain more than N replicas. If `W + R > N` the right side is at least 1:
the sets must share a replica (pigeonhole: `W + R` "slots" cannot fit into `N` replicas without
some replica filling two). That shared replica holds the write, so the reader sees its version
among the replies and, *if versions are totally ordered and it picks the highest*, returns it.

```
  N = 5, W = 3, R = 3:   overlap ≥ 3 + 3 − 5 = 1
      write acked by:   [A] [B] [C]  D   E
      read answered by:  A   B  [C] [D] [E]     C is in both, so the read sees the write

  N = 5, W = 2, R = 3:   overlap ≥ 0  → possible miss
      write acked by:   [A] [B]  C   D   E
      read answered by:  A   B  [C] [D] [E]     no replica in common: stale read, no failure needed
```

The proof quietly assumes four things. Each is a way real systems lose the guarantee:

| Assumption | Broken by | Section |
|---|---|---|
| The write had **completed** (W acks) before the read started | concurrent reads during an in-flight write | §9 |
| Every ack means the replica **still has** the write later | acking from page cache and losing it in a crash (restart with "amnesia") | §5.4 |
| Replicas are counted among the **same N** | sloppy quorums and hinted handoff | §8.7 |
| Versions are **totally and correctly ordered** | clock-skewed LWW timestamps; concurrent writes | §14.2 |

(If replicas may lie or return corrupted data, overlap of 1 is not enough: Byzantine "masking"
quorum systems need every pair of quorums to overlap in at least `2f + 1` replicas, Malkhi and
Reiter 1998.)

### 8.2 W > N/2: why write quorums should overlap each other

Two write quorums overlap when `2W > N`. Without that, two concurrent writes can be acknowledged by
disjoint replica sets, and no replica ever sees both at write time:

```
  N = 4, W = 2 during a partition {A, B} | {C, D}
      client 1 writes x = 1 → acked by A, B     (a complete quorum)
      client 2 writes x = 2 → acked by C, D     (another complete quorum)
  Both writes "succeeded"; nobody saw the conflict. Like split brain, with no leader involved.
```

With `2W > N` some replica sees both writes. That replica *can* detect the conflict, and a
protocol that makes replicas refuse the second proposal (Paxos promises, Raft's vote and
log-matching rules) turns overlap into agreement. Plain Dynamo-style stores do not refuse; they
store both versions (siblings) or let the higher timestamp win. So `2W > N` is necessary for
conflict-free ordering, not sufficient. Majority quorums, `W = R = ⌊N/2⌋ + 1`, satisfy both
`R + W > N` and `2W > N`, which is why they are the default everywhere.

(Consensus protocols need less than "every pair of write quorums intersects": Flexible Paxos,
Howard, Malkhi and Spiegelman 2016, shows only leader-election quorums and replication quorums
must intersect, which lets a system trade a larger election quorum for a smaller commit quorum.)

### 8.3 Failure tolerance of common configurations

Writes survive `N − W` unavailable replicas; reads survive `N − R`.

| N | W | R | R + W > N | 2W > N | Writes survive | Reads survive | Use |
|---|---|---|---|---|---|---|---|
| 3 | 2 | 2 | yes | yes | 1 | 1 | the default |
| 3 | 3 | 1 | yes | yes | 0 | 2 | read-heavy, can pause writes on any failure |
| 3 | 1 | 3 | yes | no | 2 | 0 | write-heavy; concurrent writes undetected |
| 3 | 1 | 1 | no | no | 2 | 2 | caches, metrics; eventual only |
| 5 | 3 | 3 | yes | yes | 2 | 2 | survive 2 failures, e.g. one zone plus one node |
| 5 | 4 | 2 | yes | yes | 1 | 3 | read-optimized |
| 6 | 4 | 3 | yes | yes | 2 | 3 | Amazon Aurora: 2 copies in each of 3 AZs; writes survive losing an AZ, reads survive an AZ plus one more node (SIGMOD 2017) |

The Aurora row shows the real reason to pick N: **correlated failures**. Independent node failures
are rare; losing a whole availability zone takes out every replica in it at once. Choose N and
replica placement so that one failure domain (an AZ, a rack) plus one more independent failure
still leaves a quorum.

### 8.4 Availability of a quorum operation

If each replica is independently unavailable with probability `p`, an operation that needs `k` of
`N` replicas is available with probability

$$A_k(N, p) \;=\; \sum_{i=k}^{N} \binom{N}{i}\,(1-p)^{i}\,p^{\,N-i}$$

Worked numbers (illustrative `p`; 1% is about 88 hours per year per replica, including restarts and
maintenance):

| N | k needed | p = 1% | downtime/yr at p = 1% | p = 5% |
|---|---|---|---|---|
| 1 | 1 | 99% | 88 h | 95% |
| 3 | 1 (R=1) | 99.9999% | 32 s | 99.9875% |
| 3 | 2 (majority) | 99.9702% | 2.6 h | 99.275% |
| 3 | 3 (all) | 97.03% | 10.8 days | 85.74% |
| 5 | 3 (majority) | 99.99902% | 5.2 min | 99.884% |
| 5 | 4 | 99.902% | 8.6 h | 97.74% |
| 5 | 5 (all) | 95.10% | 17.9 days | 77.38% |

```
  Check one value by hand, N = 3, k = 2, p = 0.01:
    P(3 up) = 0.99^3                 = 0.970299
    P(2 up) = 3 × 0.99^2 × 0.01      = 0.029403
    A       = 0.970299 + 0.029403    = 0.999702   → 99.9702%
```

Three lessons:

1. **Requiring all replicas is worse than one replica.** Each replica you must wait for is one more
   thing that can fail: `A_N = (1−p)^N`. Synchronous replication to all is less available than no
   replication (§5.1).
2. **Majorities improve fast with N.** Going from 3 to 5 replicas moves majority availability from
   three-and-a-half nines to five nines at `p = 1%`, because two simultaneous independent failures
   are rare.
3. **Independence is the load-bearing assumption.** A shared switch, zone, deploy, or bad config
   makes failures correlated; then the formula is optimistic by orders of magnitude. Reliability
   math for dependent components is in `35-reliability-math-slos-and-error-budgets.md` §8.

### 8.5 Latency: waiting for the k-th fastest of N

If the coordinator sends a request to all N replicas and proceeds after `k` answers, the operation
takes as long as the **k-th fastest** response, the k-th order statistic. For independent response
times with CDF `F(t)`:

$$P\left(T_{(k)} \le t\right) \;=\; \sum_{j=k}^{N} \binom{N}{j}\,F(t)^{j}\,\bigl(1-F(t)\bigr)^{N-j}$$

It is the same binomial as availability, with "slow" in place of "down". Illustrative model: each
replica answers in 2 ms, except that with probability `q = 1%` it is in a 100 ms pause (GC,
compaction, a noisy neighbor). The k-th response is slow only if more than `N − k` replicas are slow:

| N | wait for k | P(operation is slow) | p99 | p99.9 | p99.99 |
|---|---|---|---|---|---|
| 3 | 1 | 0.0001% | 2 ms | 2 ms | 2 ms |
| 3 | 2 | 0.0298% | 2 ms | 2 ms | 100 ms |
| 3 | 3 | 2.97% | **100 ms** | 100 ms | 100 ms |
| 5 | 3 | 0.00099% | 2 ms | 2 ms | 2 ms |
| 5 | 5 | 4.90% | **100 ms** | 100 ms | 100 ms |

```
  N = 3, k = 3: slow if ANY replica is slow:      1 − 0.99^3                     = 2.97%   (> 1% → p99 is slow)
  N = 3, k = 2: slow if ≥ 2 replicas are slow:    3 × 0.01^2 × 0.99 + 0.01^3    = 0.0298% (< 0.1% → p99.9 is fast)
```

This is why quorum systems have good tail latency and why "wait for all replicas" has bad tail
latency even when every replica is healthy on average. It also shows what to avoid:

- **Asking only R replicas.** If the coordinator sends the read to exactly R replicas (to save load),
  it waits for the *slowest of those R*, not the R-th fastest of N. Cassandra sends one full-data
  request and R − 1 digest requests, and hedges with **speculative retry** (table option
  `speculative_retry`, default `99p`): if a replica has not answered within that table's p99, it asks
  another. That is Dean and Barroso's hedged request idea ("The Tail at Scale", CACM 2013).
- **Correlated slowness.** If the coordinator itself pauses, every response is late; order
  statistics only help against independent slowness.
- **Waiting for a named replica.** PostgreSQL `FIRST 1 (r1, r2)` waits for r1 whenever r1 is
  connected, so r1's hiccups are every commit's hiccups; `ANY 1 (r1, r2)` gets the order-statistic
  benefit (§5.2).

### 8.6 Load: what quorums cost

Every write is sent to N replicas and every read to at least R (N if you send to all and use the
first R). Per logical operation, cluster work is roughly `N` write I/Os and `R` read I/Os
(`N` if reads fan out to all). Moving from `R = 1` to `R = 2` doubles read work for the benefit of
overlap; that trade is why many systems default to `ONE` reads and let applications opt into
`QUORUM` where it matters.

### 8.7 Sloppy quorums break the overlap

A sloppy quorum counts acknowledgments from **any** reachable nodes, including ones outside the
key's preference list, which store the write with a hint to hand it off later. Writes stay
available; overlap is gone.

```
  Key k: preference list {A, B, C}; N = 3, W = 2, R = 2. Ring order A B C D E.

  t=0   coordinator cannot reach A or B (partial partition)
  t=1   write k=v2 → acked by C and D (D stores it with a hint "belongs to A")   W = 2 ✓
  t=2   partition flips: the reading coordinator reaches A and B only
  t=3   read k from {A, B} → both have v1                                      R = 2 ✓
        returns v1: stale, although R + W = 4 > N = 3
  t=9   hinted handoff delivers v2 from D to A; later reads see v2
```

Riak and the original Dynamo use sloppy quorums for writes. Cassandra does not count hints toward
the consistency level except for `ANY`, whose only promise is that the write is stored somewhere.
Treat "sloppy" as "eventually consistent with high write availability", whatever R and W say.

### 8.8 Stale-read probability when R + W ≤ N

Partial quorums (`R + W ≤ N`) are common because they are cheap. How stale are they? Bailis et al.
("Probabilistically Bounded Staleness for Practical Partial Quorums", VLDB 2012) answered this
with production latency distributions. A simplified version is a few lines of Python: the write is
sent to all N replicas, replica `i` applies it after `1 ms + Exp(mean 10 ms)`, the client is
acknowledged when the W-th replica has it, and a read `gap` ms later asks R random replicas.

```python
import random

def stale_probability(n, r, w, gap_ms, mean_lag_ms=10.0, base_ms=1.0,
                      trials=20_000, seed=42):
    """Chance that a read started gap_ms after a write was acknowledged
    misses that write. Model: the write goes to all n replicas; replica i
    applies it after base_ms + Exp(mean_lag_ms). The client is acked when
    the w-th replica has applied it. The read asks r random replicas."""
    rng = random.Random(seed)
    stale = 0
    for _ in range(trials):
        apply_at = [base_ms + rng.expovariate(1.0 / mean_lag_ms) for _ in range(n)]
        ack_at = sorted(apply_at)[w - 1]          # w-th fastest replica
        read_at = ack_at + gap_ms
        asked = rng.sample(range(n), r)
        if all(apply_at[i] > read_at for i in asked):
            stale += 1
    return stale / trials

if __name__ == "__main__":
    configs = [(3, 1, 1), (3, 1, 2), (3, 2, 1), (3, 2, 2), (3, 1, 3),
               (5, 1, 1), (5, 2, 2), (5, 1, 3), (5, 3, 3)]
    gaps = [0, 1, 5, 20, 50]
    print("N R W  R+W>N  " + "  ".join(f"gap={g:>2}ms" for g in gaps))
    for n, r, w in configs:
        row = [stale_probability(n, r, w, g) for g in gaps]
        print(f"{n} {r} {w}  {'yes' if r + w > n else 'no ':>5}  "
              + "  ".join(f"{p:9.4f}" for p in row))
```

Output (about 3 s with CPython):

```
N R W  R+W>N  gap= 0ms  gap= 1ms  gap= 5ms  gap=20ms  gap=50ms
3 1 1    no      0.6714     0.6087     0.4079     0.0922     0.0044
3 1 2    no      0.3383     0.3065     0.2058     0.0450     0.0024
3 2 1    no      0.3392     0.2782     0.1231     0.0068     0.0000
3 2 2    yes     0.0000     0.0000     0.0000     0.0000     0.0000
3 1 3    yes     0.0000     0.0000     0.0000     0.0000     0.0000
5 1 1    no      0.8006     0.7214     0.4800     0.1075     0.0050
5 2 2    no      0.3089     0.2512     0.1117     0.0051     0.0001
5 1 3    no      0.3932     0.3559     0.2369     0.0543     0.0023
5 3 3    yes     0.0000     0.0000     0.0000     0.0000     0.0000
```

Reading the table:

- **`R + W > N` rows are exactly 0** at every gap: overlap works when there are no failures,
  no sloppy substitutions, and no concurrent writes.
- **`N = 3, R = W = 1` right after the ack is stale about two thirds of the time**: the ack came from
  the fastest replica and the read picked one of the other two, which had not applied it yet
  (2/3 exactly, in this model). The staleness decays with the lag distribution: under 0.5% at 50 ms,
  five mean lags later.
- **Raising W or R helps, R faster**: `(3, 1, 2)` and `(3, 2, 1)` both halve the stale rate of
  `(3, 1, 1)` at gap 0, but `(3, 2, 1)` falls much faster with time, because a read of 2 replicas
  misses only if both are still lagging.
- **More replicas with the same R and W makes things worse**: `(5, 1, 1)` is stale 80% of the time
  immediately after the ack, because 4 of 5 replicas have not applied the write yet.
- The absolute numbers depend entirely on the lag distribution you plug in. Measure your real
  apply-delay distribution (§6.3) before claiming "eventually" means "within 20 ms".

---

## 9. Why quorum overlap is not linearizability

> **In plain words.** Overlap guarantees that a read sees the last write that *finished* before the read started. It says nothing about a write that is still in flight. During that window, one reader can see the new value and a later reader the old one, which a single copy would never do. The fix is for readers to finish the job: write the value they return back to a quorum before returning it.
>
> **Real-world example.** A feature-flag service on a leaderless store with N=3, W=2, R=2. An operator flips a flag; while the write is in flight, a request in one data center sees "on", and a request 2 ms later in another sees "off" again. Blocking read repair would have prevented the second request from seeing "off".

### 9.1 The anomaly timeline

`N = 3` replicas `A, B, C`, `W = 2`, `R = 2`. `x` starts at 0. A client writes `x = 1`; the message
reaches A quickly but is delayed on the way to B and C, so the write is not acknowledged until
t = 10 ms. Two readers read during that window:

```
  time (ms)     0    1    2    3    4    5    6    7    8    9    10
  Writer        [───────────────── write x = 1 ──────────────────────]  ack at 10 (B's ack)
  replica A          x=1
  replica B                                                       x=1
  replica C                                                            x=1 (11)

  Reader 1                [ ask A,B: A→(ts1, 1), B→(ts0, 0) ]
                          returns 1 (newest)            done at 3
  Reader 2                                  [ ask B,C: both (ts0, 0) ]
                                            starts at 4, returns 0
```

Reader 2 started **after** Reader 1 finished, in real time, and yet saw an older value. With a
single copy, once any read returns 1, every later read must return 1. This is a violation of
linearizability, sometimes called a new/old inversion. Quorum overlap was not violated: the write
had not completed, so there was nothing to overlap with.

### 9.2 The fix: readers write back before returning (ABD)

Attiya, Bar-Noy and Dolev (1995) showed how to build a linearizable read/write register from
majority quorums without consensus. For a single writer:

```
  WRITE(v):  ts := ts + 1
             send (ts, v) to all replicas; wait for W acks                     1 round trip

  READ():    phase 1: ask all replicas; wait for R replies; pick max (ts, v)
             phase 2: send (ts, v) to all replicas; wait for W acks            (write-back)
             return v                                                         2 round trips

  Optimization: if all R replies in phase 1 already carry the same (ts, v), that value is
  already on a quorum; skip phase 2.
```

In the timeline above, Reader 1 would have pushed `x = 1` to B (A already had it) before
returning. Reader 2's quorum {B, C} then includes B, which has 1. Every read that returns a value
has first made sure a write quorum holds it, so no later read can return anything older.

For **multiple writers**, a write also needs a first phase: ask a quorum for the highest timestamp,
then write with `ts + 1` (ties broken by writer ID). That makes writes 2 round trips as well.

What ABD does **not** give you is compare-and-set or any read-modify-write. A register built from
reads and writes cannot solve consensus between two processes (Herlihy's consensus hierarchy), so
"increment if equal", "insert if absent", or "take the lock" need a consensus protocol: Paxos in
Cassandra's lightweight transactions, Raft in etcd, conditional writes on a leader in DynamoDB.

### 9.3 What Cassandra's quorum reads actually give

Cassandra's **blocking read repair** (§4.2) is the write-back half of ABD for the replicas the read
contacted, and with `QUORUM` reads and writes it makes quorum reads monotonic. It is still not a
linearizable register, because:

- Versions are **timestamps**, typically from the coordinator's or client's clock. A write that
  finishes later in real time can carry a smaller timestamp (clock skew) and lose (§14.2).
- **Failed writes are not rolled back.** A write that reached 1 replica and returned an error can
  later be spread by read repair and "win" (see `../databases/12-replication-and-distributed-storage.md`
  §2.3, "What W + R > N does NOT buy you").
- **Concurrent writes** are ordered by timestamp, not by any agreement, so two clients doing
  read-modify-write both succeed and one update is lost.

For linearizable conditional updates, Cassandra offers `IF` clauses (lightweight transactions)
with `SERIAL` consistency, at roughly 4 round trips.

### 9.4 What each addition buys

| Mechanism | Guarantee for a single key |
|---|---|
| `R + W ≤ N` | eventual consistency; stale reads with measurable probability (§8.8) |
| `R + W > N`, strict quorum, correct version order | a read sees every write that completed before it started |
| + blocking write-back of the returned value (ABD read) | linearizable reads and writes (single writer, or writers that pick timestamps via a quorum phase) |
| + consensus (Paxos/Raft) per key or per shard | linearizable read-modify-write: compare-and-set, uniqueness, locks |
| + multi-key transactions over consensus with a global order | strict serializability (§11) |

---

## 10. Linearizability

> **In plain words.** A linearizable system behaves as if there were one copy of the data and every operation happened instantly at some moment between its start and its end. The practical consequence: once any client has seen a new value, no client that starts later can see an older one. It is what "strongly consistent" should mean for a single key, and it is what you need for uniqueness, locks, and "last unit in stock".
>
> **Real-world example.** Two customers click "redeem" on the same single-use coupon 10 ms apart, through different app servers. With a linearizable compare-and-set (`UPDATE coupons SET used = true WHERE code = 'X7' AND used = false`, on the primary), exactly one gets row count 1. With a stale replica read followed by a write, both can succeed.

### 10.1 Definition

Herlihy and Wing (1990): a concurrent history is **linearizable** if there is a total order of all
its operations such that

1. the order respects real time: if operation `a` completed before operation `b` started, `a`
   comes before `b`; and
2. the order is legal for the object: every read returns the value of the most recent write before
   it in that order.

Equivalently, each operation appears to take effect atomically at one instant, its
**linearization point**, somewhere between its invocation and its response. Operations that
overlap in time may be ordered either way; operations that do not overlap must be ordered as they
happened.

```
  real time ────────────────────────────────────────────────────────────────►

  Client A:  |────────────── write(x, 1) ──────────────|
                        ▲ linearization point of the write (somewhere in here)
  Client B:     |── read → 0 ──|                            OK: its point can be before the write's
  Client C:                 |── read → 1 ──|                OK: its point can be after the write's
  Client D:                                  |── read → 0 ──|   VIOLATION: D started after C returned 1,
                                                                 so D's point is after C's, which is
                                                                 after the write's; D must see 1
```

Where the linearization point sits in real implementations:

| System / operation | Linearization point |
|---|---|
| Raft write | the moment the entry is committed on a majority (the leader then applies it) |
| Raft ReadIndex read | after the leader confirms it is still leader and has applied up to the recorded commit index |
| PostgreSQL write on the primary | the moment the commit becomes visible to other sessions |
| ABD quorum read | when the write-back reaches a write quorum (§9.2) |
| Compare-and-set with a row lock | while holding the lock, at commit |

### 10.2 Two properties that matter in design

- **Linearizability is local (composable).** A system of objects is linearizable if and only if
  each object is (Herlihy and Wing). You can reason key by key, and shard linearizable keys
  independently. What it does *not* give you is atomicity across keys: two linearizable writes to
  two keys can still be observed half-done. That is a transaction property (§11).
- **Linearizability is about recency, not ordering alone.** Sequential consistency keeps a single
  order that every client agrees on but drops the real-time rule, so a read may legally return a
  value that was overwritten long ago as long as everyone agrees on the order
  (`../databases/12-replication-and-distributed-storage.md` §9.2). ZooKeeper reads served by a
  follower are of this kind.

### 10.3 Testing for it: Jepsen, Knossos, Porcupine

You cannot test linearizability by reading a value and comparing it to what you "expect", because
concurrent operations allow several legal outcomes. You record a **history** and search for a
legal order.

```
  1. Several client threads issue random operations against the system:
       write(k, v), read(k), cas(k, old, new)
  2. A "nemesis" injects faults: partitions, process kills, pauses (SIGSTOP), clock skew
  3. Record every invocation and completion with timestamps. A timeout is NOT a failure: the op is
     "indeterminate" and may or may not have taken effect, and the checker must allow both.
  4. A checker searches for a total order satisfying §10.1 for each key.
```

Checking linearizability of a history is NP-complete in general (Gibbons and Korach, 1997), so
practical checkers exploit structure. **Knossos** (Clojure, part of Jepsen) searches the space of
orders with pruning; **Porcupine** (Go) is much faster and splits histories by key, which is valid
because linearizability is local (§10.2). For transactional histories Jepsen uses **Elle**
(Kingsbury and Alvaro, VLDB 2020), which checks isolation levels by finding cycles in a dependency
graph instead of searching orders. For your own service, recording histories in a fault-injection
test and feeding them to Porcupine is a realistic afternoon of work.

### 10.4 What provides it

| Mechanism | Reads | Writes | Systems |
|---|---|---|---|
| Single leader, all operations on the leader, leader verified | ReadIndex (1 RTT to a majority) or a lease (0 RTT, assumes bounded clock drift) | commit on a majority | etcd (linearizable reads by default; `serializable` reads are local and may be stale), CockroachDB leaseholder, TiKV, Consul (`consistent` mode) |
| Consensus log for every operation | through the log (as slow as a write) | through the log | simplest correct Raft/Paxos service |
| Single primary database (no failover) | on the primary | on the primary | PostgreSQL, MySQL; see the failover caveat below |
| Paxos per key for conditional writes | `SERIAL` reads | `IF` conditions | Cassandra lightweight transactions |
| Leader per partition | `ConsistentRead=true` | conditional writes | DynamoDB (single item) |
| Global timestamps with bounded uncertainty | at a timestamp after commit wait | commit wait of about 2ε | Spanner (TrueTime), also strict serializable |
| Object store | read-after-write and list-after-write | — | Amazon S3 (strong consistency for all objects since December 2020) |

Two caveats that trip people up:

- **A single primary with asynchronous failover is linearizable only until it fails over.** A
  linearizable system never un-does an acknowledged write; an async failover can (§2.5).
- **"Reads from the leader" is not enough if the leader might be deposed:**

```
  t=0.0  L1 is leader (term 5). A partition isolates L1 together with client X.
  t=1.5  F1 and F2 time out, elect F1 as leader (term 6).
  t=1.8  Client Y writes x = 2 through F1; committed on F1 + F2; Y gets "ok".
  t=2.0  Client X reads x from L1. L1 still believes it is leader and answers x = 1.

  Y's write completed before X's read started, and X saw the old value: not linearizable.
  ReadIndex would make L1 contact a majority first and discover it is no longer leader.
  A lease makes L1 stop serving once its lease (shorter than the election timeout) expires.
```

Details of ReadIndex, leases, and follower reads: `03-consensus-raft-and-distributed-locking.md` §7.

### 10.5 What it costs

- **Latency.** Every linearizable write needs at least one round trip to a majority; every read
  needs either a round trip (ReadIndex) or a lease that depends on clock accuracy. Attiya and
  Welch proved that linearizable operations have response times proportional to the uncertainty
  of network delay, so no clever protocol makes this free. Across regions it means 30–150 ms per
  operation (`36-multi-region-active-active-and-geo-replication.md`).
- **Availability under partition.** Replicas on the minority side of a partition cannot serve
  linearizable reads or writes (CAP, `00-primitives-and-system-models.md`). During a partition you
  choose between answering (possibly stale) and waiting.
- **Throughput.** All linearizable operations on a key go through its leader; a hot key is
  limited by one machine (`10-sharding-and-consistent-hashing.md` §6.4).

### 10.6 When you need it

| Need | Why weaker models fail | Cheapest linearizable mechanism |
|---|---|---|
| Uniqueness (username, email, idempotency key, coupon code) | two partitions can each accept the "same" new value | unique index or conditional insert on the leader of that key |
| Taking a lock or electing a leader | two holders at once | etcd/ZooKeeper lease **plus a fencing token checked by the resource** (`03-consensus-raft-and-distributed-locking.md` §9) |
| Decrementing the last unit of stock | two buyers both see 1 left | `UPDATE stock SET qty = qty - 1 WHERE sku = ? AND qty > 0`, check row count |
| Compare-and-set on configuration | two admins overwrite each other | etcd transaction on `mod_revision`, or a `version` column |
| **Two communication channels** | a message arrives before the data it refers to is visible | read the data through a linearizable path, or put a version/LSN in the message and have the consumer wait for it |

The last row deserves an example because nothing in it looks like a consistency problem:

```
  Web server:   1. store photo v2 under key "p/881"        (storage with async replicas)
                2. enqueue job {"resize": "p/881"}         (message queue: a second channel)
  Worker:       3. receives the job in 5 ms
                4. reads "p/881" from a replica 200 ms behind  → gets v1, or "not found"
                5. writes a thumbnail of the OLD photo; nobody notices for weeks
```

The queue is faster than replication. Fixes: read the object from the leader, use storage with
read-after-write consistency, or put the object's version in the message and have the worker
retry until it sees that version.

---

## 11. Serializability vs linearizability

> **In plain words.** Serializability is about transactions: several reads and writes across many rows, running concurrently, must produce a result that some one-at-a-time order would produce. It says nothing about *which* order, so a serializable system may put your transaction "before" one that finished earlier in real time. Linearizability is about single objects and real time: once a write is done, everyone sees it. Strict serializability is both at once.
>
> **Real-world example.** A serializable database answers a read-only report from a snapshot taken 10 s ago on a follower. The report is internally consistent (no half-done transfers), so it is serializable, but it misses the invoices created in the last 10 s, so it is not linearizable.

### 11.1 Definitions side by side

| | Serializability | Linearizability | Strict serializability |
|---|---|---|---|
| About | transactions (groups of operations on many objects) | single operations on single objects | transactions |
| Promise | the outcome equals *some* serial order of the transactions | each operation takes effect at one instant between its start and end | the outcome equals a serial order *that respects real time* |
| Real-time constraint | none | yes | yes |
| Family | isolation | recency | both |
| Typical mechanism | two-phase locking, serializable snapshot isolation, optimistic validation | consensus, leader with lease/ReadIndex | consensus per shard plus a global commit order (timestamps or a sequencer) |

Weaker isolation levels (read committed, snapshot isolation, and the "serializable" of some
vendors, which is really snapshot isolation) are covered with their anomalies in
`../databases/05-transactions-and-concurrency.md` §2–§3; this chapter does not re-derive them.

### 11.2 The 2×2 matrix

| | **Not linearizable** | **Linearizable** |
|---|---|---|
| **Not serializable** | *Cell 1.* Cassandra at `QUORUM` without LWT; reads from async replicas at read committed; most eventually consistent stores. **Anomaly:** a stale read feeds a write and an update is lost (§6.4 d). | *Cell 2.* Single-key linearizable stores without multi-key transactions: Cassandra LWT per partition, DynamoDB strongly consistent single-item operations (outside `TransactWriteItems`), a plain Raft key-value store. **Anomaly:** read skew across keys: a transfer done as two linearizable writes (debit A, then credit B) is observed between the two, and 30 units are missing from the total. |
| **Serializable** | *Cell 3.* Serializable but may be stale or reorder unrelated transactions: read-only transactions at a past snapshot (Spanner stale reads, CockroachDB `AS OF SYSTEM TIME` follower reads); CockroachDB's default serializable isolation for transactions on disjoint keys. **Anomaly:** "causal reverse": T1 writes key A and commits; T2 starts after T1 finished and writes key B; a reader running concurrently with both sees B's write but not A's. | *Cell 4.* Strict serializable: Spanner read-write transactions and strong reads, FoundationDB, etcd's key-value operations and transactions (per Jepsen's 2020 analysis), CockroachDB for transactions that touch overlapping keys. **Anomaly:** none of the above; the cost is coordination (for Spanner, commit wait of about twice the clock uncertainty). |

```
  Cell 2 anomaly, as a timeline (two keys, each linearizable, no transaction):

  Transfer:   |─ write A = 70 ─|          |─ write B = 130 ─|
  Auditor:                        |─ read A → 70 ─| |─ read B → 100 ─|
              total seen = 170, but the "true" total is always 200.
  Every single operation was linearizable; the pair was not atomic.

  Cell 3 anomaly (causal reverse), as a timeline:

  T1:          |─ write A, commit ─|
  T2:                                  |─ write B, commit ─|     starts after T1 returned; disjoint keys
  T3:   |─ begins early ...................................... reads A and B → sees B's write, not A's ─|

  T3 saw T2, so T2 is before T3 in any order that explains it. Real time puts T1 before T2, so a
  strictly serializable system must also put T1 before T3, and T3 would have to see A. A merely
  serializable system may use the order T2, T3, T1 instead, and every result is explained.
  This is possible when commit timestamps come from clocks that may disagree by up to the allowed
  offset and T1 happened to get the larger timestamp; CockroachDB documents it for its default
  serializable isolation.
```

### 11.3 Which one do you need

- **Invariants inside one database transaction** (double-entry ledger, "a doctor must stay on
  call", "seat count ≤ capacity"): serializability. Real-time order rarely matters for these.
- **Single-key coordination seen by different clients** (locks, uniqueness, "is this job taken?"):
  linearizability, usually as a conditional write.
- **Several clients that talk to each other outside the database**, or external effects ordered
  by the database (a payment confirmed to a user, then a report that must include it):
  strict serializability, or linearizability plus careful transaction design.

---

## 12. Causal consistency

> **In plain words.** If one event could have influenced another (someone read the post and then replied), everyone must see them in that order. Events that could not have influenced each other can be seen in any order. That is causal consistency, and it is the strongest guarantee a system can keep while every replica continues to accept reads and writes during a network partition.
>
> **Real-world example.** On a community forum served from three regions, Bob in the US replies "Congrats!" to Alice's post from the EU. A reader in APAC must never see "Congrats!" under an empty thread. It is fine if two unrelated posts appear in a different order in APAC than in the EU.

### 12.1 Happens-before

Lamport (1978): event `a` **happens before** `b` (`a → b`) if they are in the same process and `a`
came first, or `a` is sending a message and `b` is receiving it, or there is a chain of such steps.
If neither `a → b` nor `b → a`, they are **concurrent**. For data systems, "sending a message" is
usually "writing a value" and "receiving" is "reading it": if a client read `a`'s effect and then
issued `b`, then `a → b`.

**Causal consistency:** every client sees causally related writes in happens-before order;
concurrent writes may be seen in different orders by different clients. **Causal+** (Lloyd et al.,
COPS, SOSP 2011) adds convergence: replicas that have seen the same writes agree on concurrent
ones (via a deterministic conflict rule).

Causal consistency includes all four session guarantees (§7) and extends them *between* sessions:
what Bob read becomes a dependency of what Bob writes, and anyone who sees Bob's write must also
see what he read.

### 12.2 Why it is the strongest model available under partition

Mahajan, Alvisi and Dahlin ("Consistency, Availability, and Convergence", 2011) showed that no
consistency model stronger than (real-time) causal consistency can be implemented by a system that
stays available and converges when the network partitions. The intuition:

- Linearizability needs a replica to know about writes happening on the other side of a
  partition; it cannot, so it must refuse or risk staleness.
- Causal consistency only needs a replica to **delay making a write visible until its
  dependencies are visible locally**. That decision uses local information only. Writes created
  on this side of the partition have dependencies that are already here by construction, so this
  side keeps working; writes from the other side wait until the partition heals.

What causal consistency cannot do, for the same reason: enforce invariants across concurrent
writes. Two sides of a partition can each register username `acme`, each consistent with
everything it has seen.

### 12.3 Implementations

**(1) One log per causal domain.** If all writes that can depend on each other go through one
shard's log (a thread's post and its replies partitioned by `thread_id`), single-leader
replication already delivers them in order (§7.6). The cheapest and most common "implementation"
of causal consistency is a partitioning choice.

**(2) Explicit dependencies (COPS style).** The client library remembers the versions it has read
and written. Each write carries that dependency list. A remote replica stores an incoming write
but makes it visible only after checking that each dependency is visible locally.

```
  Alice (EU)   write P = "Got the job!"                      version P@EU:17
  Bob (US)     read P@EU:17  →  write C = "Congrats!"         C depends on {P@EU:17}
  APAC         receives C first (fast US→APAC link)
               dependency check: is P@EU:17 visible here? no → buffer C
               receives P@EU:17 → make P visible → make C visible
  Reader in APAC: sees P, or P and C, never C alone.
```

To bound the metadata, COPS tracks only *nearest* dependencies (those not already implied by
others).

**(3) Version vectors and causal delivery.** Each replica keeps a vector with one counter per
origin replica. A write created at replica `j` carries the vector of `j` at creation, with entry
`j` incremented. Replica `i` applies the write when it is the next write from `j` and everything
it depends on from other origins has been applied:

```
  deliver write w from origin j at replica i when:
      w.vv[j] == local_vv[j] + 1            (next write from j, none skipped)
      w.vv[k] <= local_vv[k]  for all k ≠ j  (every dependency from other origins already applied)
  otherwise buffer w and retry after each delivery
```

The vector has one entry per writing replica, so it is cheap with a few regions and expensive with
thousands of clients writing directly. Lamport clocks, vector clocks, hybrid logical clocks and
their trade-offs are in `../databases/19-distributed-databases-deep-dive.md` §3.

**(4) One scalar timestamp plus "read at or after".** Hybrid logical clocks (HLC) give every write
a timestamp such that `a → b` implies `ts(a) < ts(b)` (not the converse). If a session remembers the
highest timestamp it has seen and each read asks a replica for a state "at or after" that
timestamp, the session sees a causally consistent view. This is what MongoDB does.

### 12.4 MongoDB causally consistent sessions

Since MongoDB 3.6, a client session can be causally consistent. The protocol, as a sketch:

```
  session = start_session(causal_consistency = true)

  write {..., writeConcern: {w: "majority"}}        → reply carries operationTime = T1
                                                       (the session remembers T1 and $clusterTime)
  read on a secondary:
      {find: ..., readConcern: {level: "majority", afterClusterTime: T1}}
                                                    → the secondary waits until its majority-committed
                                                       snapshot has reached T1, then answers
```

The guarantees (read-your-writes, monotonic reads and writes, writes-follow-reads) hold across
failover **only with majority read concern and majority write concern**. With weaker settings, a
failover can roll back writes the session already observed, and Jepsen's 2018 analysis of MongoDB
3.6.4 demonstrated causal-session anomalies under partitions in exactly that configuration
(Real-world cases below).

### 12.5 Costs

| Cost | Where it comes from | Mitigation |
|---|---|---|
| Metadata per write | dependency lists or vectors | nearest dependencies; one vector entry per region, not per client |
| Visibility delay | a remote write waits for its slowest dependency's replication | keep causal domains small (one thread, one account) |
| False dependencies | a client that read 1,000 items makes its next write depend on all of them | scope sessions narrowly; do not share one session across unrelated work |
| No invariants | concurrent writes on two sides are both accepted | use a linearizable operation for the invariant (§10.6) |

---

## 13. Eventual consistency, bounded staleness, and the consistency ladder

> **In plain words.** Eventual consistency only promises that if writes stop, all copies end up the same. It promises nothing about when, or what you see in the meantime. Bounded staleness adds a maximum age. Commercial databases expose a ladder of levels between "eventual" and "strong", each with a price.
>
> **Real-world example.** A "likes" counter shown on a product page can be eventually consistent: if it shows 1,203 for a few seconds while the true value is 1,207, nobody is harmed. The number of seats left in a paid webinar cannot.

### 13.1 What eventual consistency does and does not promise

| Promised | Not promised |
|---|---|
| If no new writes arrive, all replicas eventually return the same value (a *liveness* property) | how long "eventually" is |
| | that a read reflects any particular write, including your own |
| | that successive reads move forward in time |
| | that the converged value is one you would call correct (LWW may pick the "wrong" one) |
| | that writes are seen in cause-and-effect order |

Two practical points:

- **Convergence needs a deterministic conflict rule.** Replicas that receive concurrent writes in
  different orders must still pick the same result: the merge must be commutative, associative and
  idempotent (last-writer-wins with a tie-break on replica ID, a CRDT merge, or keeping all
  siblings). Without that, "eventually" never comes.
- **"Eventually" is measurable.** Its distribution is the replication-lag distribution (§6.3) and,
  for partial quorums, the stale-read curve of §8.8. Put a number on it before you design around it.

### 13.2 Strong eventual consistency

Shapiro, Preguiça, Baquero and Zawirski (2011) defined **strong eventual consistency (SEC)**:
eventual delivery (every update reaches every replica) plus **strong convergence**, a *safety*
property: any two replicas that have received the same set of updates are in the same state,
immediately, with no rollback or arbitration step. CRDTs (§14.5) are the data types that provide it.
SEC is what makes offline-first and collaborative applications tractable: a device can apply
updates in any order and still agree with everyone else once it has the same set.

### 13.3 Bounded staleness

Bounded staleness caps how old a read can be: at most `T` seconds, or at most `K` versions, behind.
It is a recency bound without an ordering promise by itself (it does not imply causal), though real
implementations usually add consistent prefix.

To serve a bounded-stale read, a replica must *know* that it has everything up to some point. The
common mechanism is a **closed timestamp**: the leader promises "no more writes will commit below
timestamp `t`", and a follower that has applied everything up to that promise can serve reads at
`t` without contacting the leader.

| System | Interface |
|---|---|
| CockroachDB | follower reads with `AS OF SYSTEM TIME follower_read_timestamp()`; bounded-staleness reads with `AS OF SYSTEM TIME with_max_staleness('10s')` |
| Spanner | read-only transactions with `max_staleness` or `exact_staleness` |
| Cosmos DB | account-level Bounded Staleness (next section) |
| PostgreSQL / MySQL replicas | none built in; approximate by removing replicas whose heartbeat age exceeds `T` from the read pool (§6.3) |

### 13.4 The consistency ladder, using Cosmos DB as the worked example

Azure Cosmos DB exposes five levels. The default is set per account; a request can ask for a
*weaker* level than the account's, not a stronger one. Each partition is served by a set of four
replicas per region. (Figures below follow Microsoft's documentation at the time of writing; check
the current docs for limits.)

| Level | What a read may return | Still possible | Read cost | Notes |
|---|---|---|---|---|
| **Strong** | the latest committed write (linearizable) | nothing on this list | higher: reads are served by two replicas (a local minority quorum) | not available with multiple write regions |
| **Bounded staleness** | at most `K` versions or `T` time behind, in order (consistent prefix) | staleness up to the bound | higher (same two-replica reads) | minimums: `K` = 10 and `T` = 5 s for a single-region account; `K` = 100,000 and `T` = 300 s for multi-region |
| **Session** | the session's own writes and everything it has already seen (the four session guarantees via a session token) | other sessions' writes may be stale | single replica | default; tokens must be passed along if requests hop between clients or processes |
| **Consistent prefix** | some prefix of the writes, never out of order (for writes made together in a transaction or batch) | staleness; non-monotonic across replicas | single replica | |
| **Eventual** | any version | out-of-order and backwards-moving reads | single replica | cheapest |

The invoicing app mapped onto the ladder:

| Feature | Level | Why |
|---|---|---|
| Uniqueness of invoice numbers per tenant | Strong (or a single write region with a conditional write) | invariant across users |
| Invoice list after editing | Session | the user must see their own edit; others can lag |
| Tenant dashboard totals refreshed every 30 s | Bounded staleness (`T` = 30 s, allowed for a single-region account) or Session | stale by up to 30 s is the product's promise |
| Activity feed ("Ana commented…") | Consistent prefix | order matters, freshness does not |
| Page-view counters | Eventual | approximate is fine |

The ladder generalizes beyond Cosmos DB: for any system, write down for each feature which
anomaly it cannot tolerate, and pick the weakest level that rules it out (§15).

---

## 14. Conflict resolution: avoid, overwrite, keep siblings, merge, or CRDT

> **In plain words.** When two replicas accept different writes to the same thing without seeing each other's, something must decide the final state, and every replica must decide the same way. The options, from best to worst default: do not let it happen (route each item's writes to one place), use a data type whose updates always combine (CRDT), keep both versions and let the application merge, or pick one by timestamp and silently throw the other away.
>
> **Real-world example.** A clinic-booking SaaS with EU and US regions lets each clinic's receptionists edit appointments. Routing every clinic's writes to its home region removes almost all conflicts; the remaining ones (the home region is down and the other region takes over) are rare enough to show to a human.

### 14.1 Avoidance: one home leader per key

The cheapest conflict is the one that cannot happen. Give each item (a user, a tenant, a document)
a **home** replica or region, route all its writes there, and let other replicas serve reads.
Per item this is single-leader replication, so there are no conflicts; across items the system is
still multi-leader, so every region takes writes for its own users locally.

```
  tenant 7   → home EU   : writes from anywhere go to EU (a US user pays ~90 ms per write)
  tenant 12  → home US   : writes go to US
  reads      → any region, with the session guarantees of §7
```

Where it breaks: **moving the home**. When the EU region is down and tenant 7's home moves to US,
writes that EU accepted but had not yet replicated can arrive after US has accepted new writes. Treat
the home assignment like leadership: give it an epoch, and have replicas reject writes carrying an
old epoch (fencing, §2.5). Region evacuation and home placement are covered in
`36-multi-region-active-active-and-geo-replication.md`.

### 14.2 Last writer wins, and how clock skew makes it lose the wrong write

LWW attaches a timestamp to every write and keeps the highest (ties broken by replica ID). It
converges, it is simple, and it silently discards every other concurrent write. With wall-clock
timestamps it can also discard writes that were **not** concurrent, but later:

```
  Real time      Node A (clock correct)                 Node B (clock 3.000 s fast)
  12:00:00.000                                          owner sets price = 100
                                                        stamped 12:00:03.000
  12:00:00.200   A receives it (ts 03.000), applies: price = 100
  12:00:01.500   support agent SEES 100, sets price = 120
                 stamped 12:00:01.500
  12:00:01.600                                          B receives it: 01.500 < 03.000 → ignored
  replication done: price = 100 on every replica.

  The agent's write came 1.5 s AFTER the owner's, in real time and causally (the agent had read
  100). It lost anyway. Both clients got "ok". Nothing logs an error.
  Every write to this key through A loses until A's clock passes 12:00:03.000: a window equal
  to the skew.
```

The same mechanism causes the "deleted, then re-created, and the new row is invisible" bug: a
delete (tombstone) stamped by a fast clock shadows any insert with a smaller timestamp until real
time catches up with the tombstone.

| Mitigation | Effect |
|---|---|
| Hybrid logical clocks for timestamps | if the writer had seen the previous value, its timestamp is guaranteed higher; LWW then loses only truly *concurrent* writes, not causally later ones |
| Per-field (per-cell) LWW, as Cassandra does per column | concurrent edits to different fields both survive |
| Reject timestamps too far in the future | limits the damage of one bad clock |
| Monitor clock offset and remove skewed nodes | turns a silent data-loss mode into an alert (`29-failure-detection-phi-accrual.md` §7 discusses clock steps) |
| Use LWW only where losing a concurrent write is acceptable | immutable, write-once keys (UUID-keyed events); caches; "last seen" presence; idempotent writes of the same value |

### 14.3 Version vectors and siblings

To avoid throwing data away, the store must first know whether two writes are concurrent. Version
vectors tell it (§3.3). When they are concurrent, the store keeps **both** as siblings and returns
them on the next read, with a causal context; the writer that merges them sends that context back,
so the merged value supersedes both.

```python
def compare(a: dict, b: dict) -> str:
    """Compare two version vectors {replica: counter}."""
    keys = a.keys() | b.keys()
    a_ge = all(a.get(k, 0) >= b.get(k, 0) for k in keys)
    b_ge = all(b.get(k, 0) >= a.get(k, 0) for k in keys)
    if a_ge and b_ge:
        return "equal"
    if a_ge:
        return "a supersedes b"      # keep a, drop b
    if b_ge:
        return "b supersedes a"
    return "concurrent"              # keep both as siblings

assert compare({"X": 2, "Y": 1}, {"X": 1}) == "a supersedes b"
assert compare({"X": 1, "Y": 1}, {"X": 2}) == "concurrent"
assert compare({"X": 1}, {"X": 1}) == "equal"
print("ok")
```

The Dynamo paper's shopping cart shows both the benefit and the trap:

```
  1. laptop adds milk via node X                     cart {milk}               vv {X:1}
  2. phone reads {X:1}, adds eggs via node Y         cart {milk, eggs}         vv {X:1, Y:1}
  3. laptop (still at {X:1}) removes milk,
     adds bread via node X                           cart {bread}              vv {X:2}
  4. compare {X:1, Y:1} with {X:2}: concurrent → two siblings
  5. next read merges siblings by set union          cart {milk, eggs, bread}
                                                      ▲ the removed milk is back
```

Nothing was lost (good: eggs and bread both survived, which LWW would not do), but the removal was
undone. The Dynamo paper states this outcome directly: deleted items can resurface. The fix is to
merge with a data type that remembers removals, the OR-Set of §14.5, which gives `{eggs, bread}`.

Operational notes:

- The client must send back the causal context it read (Riak calls it the vector clock or
  context); a write without it looks concurrent with everything and creates a sibling.
- Many clients writing without reading create **sibling explosion**. Riak 2.0 introduced dotted
  version vectors to keep sibling counts bounded under such workloads; still cap and alert on
  sibling counts.
- Vectors keyed by *server* replicas stay small; vectors keyed by *client* grow without bound and
  need pruning, which can create false conflicts.

### 14.4 Application merge functions

When the data has business meaning, the application should decide. Where the merge runs:

| When | Systems | Suited to |
|---|---|---|
| On read (siblings handed to the reader) | Riak with `allow_mult`, the original Dynamo | data the reader is about to modify anyway |
| On replication (resolver runs as changes arrive) | BDR/PGD conflict resolvers, Cosmos DB custom conflict-resolution procedures | server-side rules that need no user |
| Winner picked deterministically, losers kept for later | CouchDB `_conflicts` | apps that can resolve lazily or show a conflict UI |

Rules that make merge functions safe:

1. **Merge per field, not per record.** `amount` from one side and `notes` from the other.
2. **Store operations, not states, where you can.** "Add 30 to the balance" merges; "balance = 130"
   does not.
3. **Make the merge deterministic, commutative and idempotent**, or replicas will not converge.
4. **Keep a conflict log.** You will need it to explain what happened to a customer.
5. **Invariant conflicts cannot be merged, only compensated.** Two regions each booked the last slot:
   cancel one and notify the customer (an apology workflow; see sagas and compensations in
   `06-distributed-transactions-sagas-outbox-idempotency.md`), or prevent it with §14.1 or §10.

### 14.5 CRDTs

Conflict-free replicated data types (Shapiro et al., 2011) are data types whose concurrent updates
merge deterministically with no coordination, giving strong eventual consistency (§13.2).

| CRDT | State | Merge | Good for | Watch out for |
|---|---|---|---|---|
| G-Counter | one counter per replica | per-replica max; value = sum | views, likes that only go up | cannot decrement |
| PN-Counter | two G-Counters (increments, decrements) | merge each | votes, quantities | cannot enforce "≥ 0" |
| G-Set | a set that only grows | union | "users who ever did X" | no removal |
| OR-Set (observed-remove) | (element, unique tag) adds, removed tags | union both; element present if some add tag is not removed | carts, tags, memberships | tombstones grow until garbage-collected |
| LWW-Register | (value, timestamp) | keep higher timestamp | single-valued fields where loss is acceptable | the §14.2 skew problem |
| MV-Register | set of concurrent values with version vectors | keep all maximal values | fields that need user resolution | the application must resolve |
| Maps, sequences (RGA, Yjs, Automerge) | nested CRDTs; per-character IDs | per-key / per-element merge | JSON documents, collaborative text | metadata overhead per element |

A minimal OR-Set, runnable with the standard library (state-based; add wins over a concurrent
remove):

```python
import itertools

class ORSet:
    """State-based observed-remove set (add wins over a concurrent remove)."""
    _ids = itertools.count()

    def __init__(self, replica: str):
        self.replica = replica
        self.adds: set[tuple[str, str]] = set()      # (element, unique tag)
        self.removes: set[tuple[str, str]] = set()   # tombstoned (element, tag)

    def add(self, element: str) -> None:
        self.adds.add((element, f"{self.replica}:{next(self._ids)}"))

    def remove(self, element: str) -> None:
        # remove only the tags this replica has OBSERVED for the element
        self.removes |= {(e, t) for (e, t) in self.adds if e == element}

    def value(self) -> set[str]:
        return {e for (e, t) in self.adds - self.removes}

    def merge(self, other: "ORSet") -> None:
        self.adds |= other.adds
        self.removes |= other.removes

if __name__ == "__main__":
    phone, laptop = ORSet("phone"), ORSet("laptop")
    phone.add("milk"); laptop.merge(phone)          # both see {milk}
    phone.remove("milk")                            # phone removes milk ...
    laptop.add("milk")                              # ... laptop re-adds it concurrently
    laptop.add("eggs")
    phone.merge(laptop); laptop.merge(phone)
    assert phone.value() == laptop.value() == {"milk", "eggs"}
    print(sorted(phone.value()))                    # ['eggs', 'milk']: the add won
```

The remove only tombstones tags it has seen, so the laptop's concurrent re-add (a new tag) survives,
and the merge is a union of two sets: commutative, associative and idempotent. Run the §14.3 cart
through it and milk stays removed, because nobody re-added it.

What CRDTs cannot do is keep an invariant that depends on concurrent updates: two replicas can each
decrement a PN-Counter stock of 1 and end at −1. Production uses (Riak data types, Redis Enterprise
Active-Active, Automerge, Yjs), delta-state and operation-based variants, and the garbage-collection
problem are in `../databases/19-distributed-databases-deep-dive.md` §7.

### 14.6 Operational transformation vs CRDTs

Collaborative editors must merge concurrent edits to the same text. **Operational transformation
(OT)**, used by Google Docs and descending from Ellis and Gibbs (1989) and the Jupiter system,
sends operations such as "insert 'x' at position 5" and *transforms* each incoming operation
against concurrent ones already applied ("someone inserted 3 characters before position 5, so this
becomes position 8"). It works well with a central server that imposes one order, and its
correctness conditions are notoriously hard to get right without one. **Sequence CRDTs** (RGA,
the structures in Yjs and Automerge) give every character a unique, ordered identifier so
concurrent inserts commute by construction, which suits peer-to-peer and offline editing, at the
cost of per-character metadata that implementations work hard to compress. Rule of thumb: a
server-mediated editor can use either; an offline-first or peer-to-peer one should use a CRDT.

### 14.7 Choosing a strategy

| Data | Strategy |
|---|---|
| Anything with an invariant (stock, balance, uniqueness, bookings) | avoid conflicts: single leader per item (§14.1) or consensus (§10) |
| Per-user documents edited from several devices | home leader per user; OR-Map/OR-Set CRDT if offline editing is required |
| Counters that may be approximate | PN-Counter or sharded counters |
| Sets (tags, carts, followers) | OR-Set |
| Single fields where the latest edit should win and loss is acceptable | LWW with HLC timestamps, per field |
| Free text edited concurrently | OT with a server, or a sequence CRDT |
| Business records with complex rules | siblings or a conflict log plus an application or human resolver |

---

## 15. Decision guide: feature to guarantee to mechanism

> **In plain words.** Do not choose one consistency level for the whole product. For each feature, name the anomaly it cannot tolerate, pick the weakest guarantee that rules that anomaly out, and implement it with the cheapest mechanism. Most features need only session guarantees on replicas; a few need a linearizable or serializable operation on the leader; some are fine with eventual consistency.

### 15.1 Four questions

```
  1. Does the operation enforce a rule that involves OTHER users' concurrent writes?
     (stock, uniqueness, balance, booking, lock)
        yes → linearizable / serializable operation on the item's leader:
              a conditional write or a transaction on the primary. If it is money or an order,
              also make the commit durable on a second replica (semi-sync, §5).
        no  ↓
  2. Is the user looking at data they just changed themselves?
        yes → session guarantees on replicas: LSN/GTID token or leader-for-T_pin (§7).
        no  ↓
  3. Does the order of items matter (replies, feeds, event logs)?
        yes → consistent prefix / causal: keep related items in one partition and read with a cursor (§7.6, §12).
        no  ↓
  4. Is a bounded amount of staleness acceptable?
        yes → replicas with a staleness bound (heartbeat-based pool admission, §6.3) or eventual.
              With concurrent writers on several replicas, use a CRDT (§14.5).
```

### 15.2 SMB SaaS features

| Feature | Anomaly that hurts | Guarantee needed | Cheapest mechanism | Do not |
|---|---|---|---|---|
| Login / session creation | logged in, next request says "not logged in" because the session row is not on the replica yet | read-your-writes for the session record | signed stateless session token (no read at all), or read session rows from the primary | read the sessions table from a lagging replica |
| Logout, password change, permission revoke | a revoked session keeps working on replicas | recency for security decisions | check revocation on the primary (or a linearizable store); short-lived tokens | cache permissions for minutes without invalidation |
| Profile / settings edit | "my change disappeared", values flicker | read-your-writes + monotonic reads | LSN/GTID token router (§7.2) or leader reads for `T_pin` | send all reads to the primary "to be safe" |
| Shopping cart | lost adds from a second device, resurrected removals | session guarantees; convergence of concurrent edits | route each user's cart to one leader + RYW; OR-Set if carts are edited offline or multi-region | whole-cart LWW |
| Inventory decrement | overselling the last unit | linearizable conditional update per SKU | `UPDATE stock SET qty = qty - :n WHERE sku = :s AND qty >= :n` on the leader, check the row count; reservations with expiry for checkout flows | read quantity from a replica, then write |
| Account balance / payments | lost update, double spend, confirmed payment lost on failover | serializable transaction + durable commit | single-leader SQL; append-only ledger rows; conditional balance update or `SERIALIZABLE`; semi-sync `ANY 1` in-region; idempotency keys (`06-distributed-transactions-sagas-outbox-idempotency.md`) | multi-leader writes, LWW, async-only primary |
| Likes counter | none meaningful; approximate is fine | eventual (and "like once per user" if required) | per-user like row with a unique key; count asynchronously or with sharded / CRDT counters (`../solutions/distributed-counter-design.md`) | a linearizable counter on one hot row |
| Notification feed | a reply shown before its post; items appearing and disappearing on refresh | consistent prefix + monotonic reads | per-user feed in one partition (a Kafka partition or one shard), cursor-based pagination, token for monotonic reads | read each page from a random replica |
| Audit log | missing or reordered entries; entries lost after failover | durability + total order per stream | append to a replicated log with `acks=all` and `min.insync.replicas=2`, or a semi-sync database table; per-stream sequence numbers | async-only storage, LWW overwrite |
| Username / unique handle | two accounts with the same handle | linearizable uniqueness | unique index on the primary (single write region) or a conditional put in a consensus-backed store | uniqueness checks on replicas or across regions with async replication |
| Feature flags / configuration | servers disagree on a flag for a while | bounded staleness + monotonic per server | versioned config pushed from etcd/ZooKeeper watches; servers never go back to an older version | assume changes are instant everywhere |
| Search results | a new item is not found immediately | bounded staleness, documented | CDC into the index with a lag SLO; merge the user's own recent items from the primary | promise read-your-writes from the index |
| Tenant reporting dashboards | numbers a few seconds old | bounded staleness | replicas admitted to the pool only while heartbeat age < `T` | run heavy reports on the primary |

A useful sanity check on any design review: for every read that goes to a replica, ask "what
happens if this read is `L` seconds old, where `L` is the worst lag we saw last month?" If the
answer is "a user is confused", use a session guarantee. If the answer is "we persist a wrong
decision", move the read to the leader and into the write.

---

## Production pitfalls / war stories

1. **Check-then-act on a replica.** Reading "is this coupon used / is stock > 0 / what is the
   balance" from a replica and then writing to the primary is the most common replication bug in
   CRUD applications. It passes every test because test replicas have no lag. Make the check part of
   the write (§6.4 d).
2. **`Seconds_Behind_Source = 0` on a replica that is minutes behind.** The applier is caught up
   with the relay log while the receiver is starved by the network. Alert on a heartbeat table, not
   on this field alone (§6.3).
3. **3 a.m. lag pages on an idle database.** `now() - pg_last_xact_replay_timestamp()` grows when no
   writes happen. Use a heartbeat, which also exercises the replication path end to end.
4. **An abandoned replication slot fills the primary's disk.** A decommissioned replica or a CDC
   connector that stopped consuming keeps its slot, and the primary retains WAL until the disk is
   full, which stops all writes. Set `max_slot_wal_keep_size` and alert on
   `pg_replication_slots` where `active = false` or retained WAL is growing.
5. **Semi-sync that is not.** MySQL falls back to asynchronous after
   `rpl_semi_sync_source_timeout` (10 s default) without failing anything. Durability degrades
   exactly during the network trouble that precedes many failovers. Alert on
   `Rpl_semi_sync_source_status`.
6. **Sync replication that stops the world.** PostgreSQL with one synchronous standby listed and
   that standby rebooting: every commit hangs. Use `ANY 1 (r1, r2)` with two candidates, or let
   Patroni manage the list, and decide explicitly whether degrading to async is allowed.
7. **Failover promotes a replica that was far behind.** Without a lag limit, an automatic failover
   can promote the only reachable replica even if it is minutes behind, and lose those minutes. Set
   a maximum lag for promotion and prefer a human decision when it is exceeded.
8. **The old primary keeps taking writes.** A primary that was paused (GC, VM migration) wakes up,
   and connection pools that never noticed the failover write to it. Fence it: a lease that it must
   renew, STONITH, or a proxy that routes writes only to the node the DCS names as leader.
9. **Hot standby tuning trade-off.** `hot_standby_feedback = off` means long replica queries get
   cancelled; `on` means the primary cannot vacuum rows those queries might need, and tables bloat.
   Separate "user read" replicas (short queries, strict lag limit) from "analytics" replicas (long
   queries, lag accepted).
10. **LWW plus a clock step.** A node whose clock jumped forward stamps writes in the future; every
    later write to those keys from correct nodes loses silently until real time catches up (§14.2).
    Monitor clock offset as a correctness signal, not a hygiene metric.
11. **A cache in front of a session-consistent database.** The router does everything right and the
    Redis cache still serves the pre-edit value. Include caches in the read-your-writes design (§7.7).
12. **`acks=all` with `min.insync.replicas=1`.** When the ISR shrinks to the leader, `acks=all`
    means "the leader has it", and a leader crash loses acknowledged records. Use
    `min.insync.replicas=2` with RF 3.
13. **Tombstones outlive repair.** In Cassandra, if a replica misses a delete and repair does not run
    within `gc_grace_seconds` (10 days default), the tombstone is purged elsewhere and the stale
    replica's old value comes back during the next repair.
14. **Logical replica as a failover target.** DDL and sequence values are not replicated by
    PostgreSQL logical replication (through at least version 17); after failover, inserts collide
    on primary keys. Verify schema and bump sequences as part of the failover runbook.
15. **Backfills that ignore lag.** A migration that updates 200 million rows at full speed pushes
    replicas minutes behind and turns every read-your-writes fallback into primary load. Throttle on
    replica lag (§6.2).

---

## Interview questions

**1. What is the difference between synchronous and asynchronous replication, and what does
semi-synchronous mean in PostgreSQL and MySQL?**
Synchronous: the commit is acknowledged only after replicas confirm; RPO 0, higher latency, and a
missing replica can block writes. Asynchronous: acknowledged after local durability; lowest latency;
a crash loses whatever the replicas had not received. Semi-sync: wait for k of N replicas.
PostgreSQL: `synchronous_standby_names = 'ANY k (...)'` or `FIRST k`, with `synchronous_commit`
choosing received / flushed / applied; it blocks if too few standbys. MySQL semi-sync waits for a
relay-log write on k replicas and falls back to async after a timeout.

**2. A user edits their profile and the page still shows the old value. Why, and how do you fix it
without sending all reads to the primary?**
The read went to a lagging replica: a read-your-writes violation. Record the commit's LSN (or
GTID) in the user's session; route reads to a replica whose replay position is at or past it, with a
short bounded wait and fallback to the primary. Cheaper but less exact: read from the primary for a
few seconds after a write.

**3. Prove that R + W > N guarantees that a read sees the latest completed write.**
The write set and read set are subsets of the same N replicas, so their intersection has at least
`W + R − N ≥ 1` members (pigeonhole). That replica has the write, and the reader returns the highest
version it sees. Assumptions: strict quorum, the write completed before the read started, acks are
durable, versions are correctly ordered.

**4. Is a system with N=3, W=2, R=2 linearizable?**
Not by itself. During an in-flight write, one reader can see the new value from the one replica
that has it, and a later reader can pick two replicas that do not. Fix: readers write the value back
to a write quorum before returning (ABD), and use correctly ordered versions. Compare-and-set still
needs consensus.

**5. Why use W > N/2?**
So that any two write quorums share a replica; otherwise two concurrent writes can be accepted by
disjoint sets and nobody sees the conflict. Overlap is what consensus protocols use to make a
replica refuse the second proposal.

**6. With 5 replicas each down 1% of the time, how available is a majority operation?**
`Σ_{i=3..5} C(5,i) 0.99^i 0.01^(5−i)` ≈ 99.999%, about 5 minutes per year, assuming independent
failures. Requiring all 5 gives only 95.1%.

**7. Why is quorum tail latency better than waiting for all replicas?**
The operation takes the k-th fastest of N responses. If each replica is slow 1% of the time, 2 of 3
is slow only when two replicas are slow at once (about 0.03%), while 3 of 3 is slow when any is
(about 3%). Same binomial as availability.

**8. What is a sloppy quorum and what does it cost?**
Counting acks from nodes outside the key's replica set (with hinted handoff) when the right nodes
are unreachable. Writes stay available; R + W > N no longer guarantees overlap, so reads can be
stale until hints are delivered.

**9. Linearizability vs serializability?**
Linearizability: single-object, real-time recency; once a write completes, every later operation
sees it. Serializability: multi-object transactions equivalent to some serial order, with no
real-time constraint. Strict serializability is both. Spanner and FoundationDB are strict
serializable; a Raft key-value store without transactions is linearizable only; a serializable
database serving reads from past snapshots is serializable only.

**10. Why is causal consistency called the strongest model that stays available under partition?**
It only requires delaying a write's visibility until its dependencies are visible locally, which a
replica can decide without contacting anyone; stronger models (linearizability) require knowledge of
writes on the other side of the partition. (Mahajan, Alvisi, Dahlin, 2011.)

**11. How does LWW lose data, even without concurrent writes?**
It keeps the highest timestamp. With clock skew, a later, causally dependent write can carry a
smaller timestamp and be discarded, silently. Use HLC timestamps, per-field LWW, or a data type that
merges.

**12. When would you use version vectors and siblings instead of LWW?**
When losing a concurrent write is unacceptable and the data can be merged (carts, sets, documents).
Vectors detect concurrency; siblings preserve both writes; the application or a CRDT merges them.

**13. How do you measure replication lag correctly?**
Per stage: sent, written, flushed, replayed (bytes via `pg_wal_lsn_diff`, time via `replay_lag`), plus
a heartbeat table that measures the age of the newest visible data. Beware `Seconds_Behind_Source`
(0 when the receiver is behind, NULL when broken) and `pg_last_xact_replay_timestamp` (grows when
idle).

**14. What can go wrong in a leader failover?**
Loss of acknowledged async writes; reuse of IDs the old leader already handed out; split brain if
the old leader is not fenced; false failovers from aggressive timeouts; clients still writing to the
old leader.

**15. Kafka: why is `acks=all` not enough?**
`acks=all` waits for the current ISR, which can shrink to the leader alone. Set
`min.insync.replicas=2` (with RF 3) so writes fail rather than become single-copy, and keep unclean
leader election disabled.

**16. Which consistency would you choose for a likes counter, a coupon redemption, and a comment
thread?**
Likes: eventual (CRDT or sharded counter; a unique row per user if "like once" matters). Coupon:
linearizable conditional update on the leader. Comments: consistent prefix/causal by partitioning
the thread into one shard, plus read-your-writes for the author.

---

## Real-world cases — incidents with numbers

Only publicly documented incidents and published analyses are listed. Numbers are those reported by
the sources named; where a source gave no number, none is invented.

**Quick index:** stale replica promoted, IDs reused → GitHub 2012 · cross-region promotion after a
short partition → GitHub 2018 · replica fell off the log, then a human error → GitLab 2017 ·
`acks` to a shrunken ISR → Jepsen Kafka 2013 · causal sessions need majority concerns → Jepsen
MongoDB 3.6.4 · LWW vs siblings → Jepsen Riak 2013 · replicas order commits differently → Jepsen
RDS PostgreSQL 2025

### GitHub, 2012: an out-of-date follower promoted, primary keys reused

- **What happened.** As recounted in Kleppmann's *Designing Data-Intensive Applications* from
  GitHub's own report, an out-of-date MySQL follower was promoted to leader. Its auto-increment
  counter was behind the old leader's, so it reissued primary keys the old leader had already
  assigned. Those keys were also used in a Redis store, so MySQL and Redis disagreed, and some private
  data was disclosed to the wrong users.
- **Lesson.** Lost async writes are not just "missing rows": identifiers derived from the lost
  state are reused, and every external system that stored them now points at the wrong thing (§2.5).

### GitHub, October 21, 2018: 43 seconds of partition, 24 hours of degradation

- **What happened.** Connectivity between GitHub's US East Coast network hub and its primary East
  Coast data center was lost for 43 seconds. The cluster manager (Orchestrator) promoted West Coast
  replicas to primary. When connectivity returned, the East Coast databases held a brief period of
  writes that had not been replicated to the West Coast, while the West Coast primaries had taken
  new writes: two histories.
- **Numbers.** GitHub reported running degraded for 24 hours and 11 minutes while restoring and
  reconciling data.
- **Lesson.** An asynchronous cross-region failover converts a short partition into a data
  reconciliation problem. GitHub's follow-ups included not promoting primaries across regions
  automatically. See also `29-failure-detection-phi-accrual.md` (Real-world cases, 10.5) and
  `03-consensus-raft-and-distributed-locking.md` §17.6.

### GitLab.com, January 31, 2017: a replica that could not catch up

- **What happened.** Under heavy load (spam), the secondary database fell behind and replication
  broke because the primary had already removed WAL segments the secondary still needed. While
  trying to rebuild the secondary, an engineer ran a directory deletion on the primary instead of
  the secondary. Several backup mechanisms turned out not to be working, and the site was restored
  from a snapshot taken about six hours earlier.
- **Numbers.** GitLab's postmortem reported losing about six hours of database data, affecting
  roughly 5,000 projects, 5,000 comments and 700 new user accounts.
- **Lesson.** WAL retention is a replication parameter (§2.2): a replica that falls off the log
  needs a full rebuild, which is when tired humans make mistakes. Retain enough WAL (or use slots with
  a size cap), and alert on lag long before it becomes "rebuild".

### Jepsen: Kafka 0.8 beta (2013): `acks` to an ISR of one

- **What happened.** Kingsbury's analysis showed Kafka could lose acknowledged writes when the ISR
  shrank to only the leader, which kept acknowledging writes, and that leader then failed and an
  out-of-sync replica took over.
- **Outcome.** Kafka later added `min.insync.replicas` and made unclean leader election
  configurable (and eventually disabled by default), which address exactly this failure (§5.4).

### Jepsen: MongoDB 3.6.4 (2018): causal sessions need majority concerns

- **What happened.** Jepsen tested MongoDB's then-new causally consistent sessions and found that,
  with the default (non-majority) read and write concerns, sessions could observe violations of the
  causal guarantees during partitions, because writes they had seen could be rolled back.
- **Outcome.** MongoDB's documentation states that the causal-consistency guarantees hold across
  failures with `majority` read concern and `majority` write concern (§12.4).

### Jepsen: Riak (2013): last-write-wins vs siblings

- **What happened.** Under partitions, Riak configured for last-write-wins (`allow_mult=false`)
  lost acknowledged writes to the same key, as LWW must. With siblings enabled (`allow_mult=true`)
  and a merge function (set union, the CRDT approach), the writes were preserved.
- **Lesson.** The quorum settings were identical in both runs; the conflict-resolution strategy
  alone decided whether data was lost (§14.2–§14.5).

### Jepsen: Amazon RDS for PostgreSQL Multi-AZ clusters (2025): replicas disagree on commit order

- **What happened.** Jepsen found "Long Fork" anomalies at snapshot isolation in healthy clusters
  with readable standbys: the primary and a replica could make two concurrent transactions visible
  in opposite orders. The cause is community PostgreSQL behavior (visibility order on the primary vs
  WAL commit-record order on standbys), not an RDS-specific bug.
- **Lesson.** A replica can be stale *and* order concurrent commits differently from the primary.
  Decisions that combine reads from several nodes need the primary. Full write-up:
  `../databases/12-replication-and-distributed-storage.md` §9.8.

---

## Key Takeaways

1. **Single-leader replication gives every follower a prefix of one history.** Its problems are
   staleness and failover, not conflicts. Multi-leader and leaderless give up that property for
   availability and must resolve conflicts.
2. **Choose the log form deliberately**: physical for identical replicas and failover, logical for
   upgrades, CDC, and partial replication; statement-based only with deterministic statements.
3. **Asynchronous failover loses acknowledged writes, by design.** The amount is the lag at failure
   times the write rate. Semi-synchronous commit to any one of two in-region replicas removes that
   loss for about one in-region round trip per commit.
4. **Know your system's degraded behavior**: PostgreSQL blocks when sync standbys are missing;
   MySQL semi-sync silently falls back to async; Kafka `acks=all` needs `min.insync.replicas`.
5. **Lag is a pipeline** (sent, written, flushed, replayed). Measure every stage in bytes and seconds,
   and measure reader staleness directly with a heartbeat.
6. **Most replica-read anomalies are per-user**, and session guarantees fix them cheaply. An LSN or
   GTID token that travels with the user is the most robust implementation.
7. **Never read-modify-write through a replica.** Invariants involving other users' writes need a
   linearizable or serializable operation on the leader, usually a conditional write.
8. **Quorum math is one binomial**: overlap needs `R + W > N`, conflict detection needs `2W > N`,
   availability and tail latency both come from "at least k of N" probabilities.
9. **Quorum overlap is not linearizability.** In-flight writes, sloppy quorums, failed partial
   writes and clock-ordered versions all break it; ABD-style write-back fixes reads, consensus is
   needed for compare-and-set.
10. **Linearizability is about single-object recency; serializability about multi-object isolation.**
    Strict serializability is both, and costs cross-shard coordination.
11. **Causal consistency is the strongest model available under partition**, and often it is
    achieved simply by keeping causally related data in one partition.
12. **LWW with wall clocks can discard causally later writes.** Use it only where loss is
    acceptable; otherwise use version vectors, merge functions, or CRDTs.
13. **Pick guarantees per feature**, by naming the anomaly each feature cannot tolerate.

---

## Cross-References

### Within `distributed-systems/`
- `00-primitives-and-system-models.md`: system and failure models, FLP, CAP and PACELC, which this chapter assumes.
- `03-consensus-raft-and-distributed-locking.md`: Raft log replication (§4), ReadIndex and lease reads (§7), fencing tokens (§9.2), the GitHub 2018 case (§17.6).
- `06-distributed-transactions-sagas-outbox-idempotency.md`: idempotent retries after ambiguous commits, sagas and compensation for conflicts that cannot be merged.
- `07-kafka-and-event-streaming.md`: `acks` and idempotent producers (§3.5–§3.6), replication factor (§9.4), ISR shrink and unclean election (§12.2).
- `08-caching-strategies-and-patterns.md`: cache invalidation (§3), which decides whether read-your-writes survives a cache.
- `10-sharding-and-consistent-hashing.md`: preference lists and replication plus partitioning (§8), hot keys (§6.4).
- `29-failure-detection-phi-accrual.md`: deciding that a leader is dead, timeout tuning, clock steps, fencing with epochs.
- `34-adaptive-load-control-and-backpressure.md`: throttling backfills and batch jobs on replica lag.
- `35-reliability-math-slos-and-error-budgets.md`: redundancy math (§8) behind the quorum availability formula.
- `36-multi-region-active-active-and-geo-replication.md`: cross-region topologies, quorum placement, home regions and evacuation.
- `37-distributed-systems-debugging.md`: diagnosing replication lag and consistency anomalies in production.

### Within `databases/`
- `../databases/05-transactions-and-concurrency.md`: isolation levels and their anomalies (§2–§3), referenced by §11.
- `../databases/12-replication-and-distributed-storage.md`: replication basics (§2), "What W + R > N does NOT buy you" (§2.3), consistency model overview (§9), the RDS PostgreSQL Long Fork analysis (§9.8), invariant-first design playbook (§11).
- `../databases/14-write-ahead-log-internals.md`: LSN arithmetic (§2) and WAL-based replication (§12).
- `../databases/16-failure-detection-and-leader-election.md`: lease-based leadership (§5) and fencing (§6).
- `../databases/19-distributed-databases-deep-dive.md`: clocks (§3), chain and quorum replication (§6), CRDTs (§7), anti-entropy, read repair, hinted handoff and sloppy quorums (§8), bounded staleness and follower reads (§12.4), Cosmos DB (§16.3).

### Within `solutions/`
- `../solutions/distributed-counter-design.md`: an eventually consistent like counter at scale (§15 likes row).
- `../solutions/key-value-store-design.md`: a replicated key-value store design exercising quorums and consistency choices.
- `../solutions/instagram-feed-design.md`: feed fan-out, where consistent prefix and monotonic reads matter.
- `../solutions/database-design-best-practices.md`: schema-level choices (keys, uniqueness) that interact with replication.

### Within `sre-observability/`
- `../sre-observability/23-database-observability.md`: replica lag metrics and the freshness SLI (§7).
- `../sre-observability/13-slo-engineering.md`: turning replica freshness into an SLO with burn-rate alerts.
- `../sre-observability/15-incident-response-and-postmortem.md`: running the incident when a failover loses data.
