# Chapter 00: Primitives, System Models & Consistency Models

The shared vocabulary the rest of the distributed-systems track assumes. This chapter defines what
"distributed" means (partial failure), the timing, network and failure models that every protocol
is proven against, the quorum sizes those models force (f+1, 2f+1, 3f+1) and why, the three
impossibility-and-trade-off results everyone quotes and many misquote (FLP, CAP, PACELC), a compact
map of consistency models, the delivery-semantics vocabulary, and a decision guide for choosing
the guarantee a product feature actually needs. Later chapters go deep on each mechanism; this one
makes sure the words mean the same thing everywhere.

Prerequisites: none. Read this first. For depth on replication, the full consistency spectrum,
session guarantees and quorum math, continue with `04-replication-and-consistency.md`.

---

## Table of Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [What "Distributed" Means — Partial Failure and the Eight Fallacies](#1-what-distributed-means--partial-failure-and-the-eight-fallacies)
2. [Timing Models — Synchronous, Asynchronous, Partially Synchronous](#2-timing-models--synchronous-asynchronous-partially-synchronous)
3. [Network Models — Loss, Reordering, Duplication, Partitions](#3-network-models--loss-reordering-duplication-partitions)
4. [Failure Models and Quorum Sizes — Crash-Stop to Byzantine](#4-failure-models-and-quorum-sizes--crash-stop-to-byzantine)
5. [Gray Failure — Alive to the Detector, Dead to the User](#5-gray-failure--alive-to-the-detector-dead-to-the-user)
6. [FLP Impossibility — What It Forbids and What It Does Not](#6-flp-impossibility--what-it-forbids-and-what-it-does-not)
7. [CAP Stated Precisely](#7-cap-stated-precisely)
8. [PACELC — Latency Versus Consistency When Nothing Is Broken](#8-pacelc--latency-versus-consistency-when-nothing-is-broken)
9. [Consistency Models at a Glance — Linearizability to Eventual](#9-consistency-models-at-a-glance--linearizability-to-eventual)
10. [Delivery Semantics and Idempotence — Definitions](#10-delivery-semantics-and-idempotence--definitions)
11. [Decision Guide — Which Guarantee Does This Feature Need?](#11-decision-guide--which-guarantee-does-this-feature-need)
12. [Production pitfalls / war stories](#12-production-pitfalls--war-stories)
13. [Interview questions](#13-interview-questions)
14. [Real-world cases — incidents with numbers](#14-real-world-cases--incidents-with-numbers)

- [Key Takeaways](#key-takeaways)
- [Cross-References](#cross-references)
- [References](#references)

---

## Start here — the whole chapter in plain words

**The problem.** A program on one computer either works or crashes, and when it crashes you know.
A system spread over several computers connected by a network can be *partly* broken: one machine
is down, one is slow, one link drops messages, and nobody can see the whole picture. The worst
case is not "it failed" but "I don't know whether it failed": a request timed out, and the other
side may or may not have done the work. Everything in distributed systems (replication, consensus,
retries, consistency levels) is a way of living with that uncertainty. This chapter gives you the
words to describe the uncertainty precisely and to choose, per feature, how much of it the product
can tolerate.

**A real-world example.** A small online sneaker shop runs one PostgreSQL primary with two
asynchronous read replicas in the same region, a Redis cache, and an external payment provider. It
takes about 2,000 orders a day; at peak it does 30 writes/s and 600 reads/s. Replica lag is
usually 20 ms, but 1–2 s while the nightly report job runs. (Numbers are illustrative.)

- **Partial failure (§1).** The payment call times out after 10 s. Did the customer get charged?
  The shop cannot know from the timeout alone. Retrying blindly risks charging twice; not retrying
  risks shipping for free. Fix: an idempotency key on the payment request (§10).
- **Failure models and quorums (§4).** The team considers "just add a second database and fail
  over". With two nodes, any rule that keeps working when one is unreachable also lets both halves
  work alone during a network split. Majority rules need 3 nodes to survive 1 failure.
- **FLP and CAP (§6, §7).** If the link between the primary and replicas breaks, the shop must
  pick: refuse checkouts until it heals (consistent) or keep selling from both sides and fix
  conflicts later (available). No design gets both during the split.
- **PACELC (§8).** Even with no failures, reading from a replica is faster for a far-away user but
  may be 2 s stale. That is a latency-versus-consistency choice made on every request.
- **Consistency models (§9).** A customer adds shoes to the cart (write goes to the primary), the
  page reloads from a replica 1.5 s behind, and the cart is empty. That violates *read-your-writes*.
  Two customers buy the last pair of size 44 at the same moment; both see "1 left". Preventing
  that oversell needs a *linearizable* conditional update, not a replica read.
- **Decision guide (§11).** The cart needs read-your-writes; stock for the last unit needs
  linearizability; the "1,203 likes" counter only needs to be eventually right.
- **Where to go deeper.** Replication and the full consistency spectrum with anomaly walkthroughs,
  session-guarantee implementations and quorum math: `04-replication-and-consistency.md`. Retries,
  idempotency keys, 2PC, sagas and the outbox: `06-distributed-transactions-sagas-outbox-idempotency.md`.
  Consensus internals: `03-consensus-raft-and-distributed-locking.md`.

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Node / process | one running program on one machine that takes part in the system | one employee in an office |
| Partial failure | some parts are broken while others keep running | two of five checkout lanes closed, the shop still open |
| Network partition | some nodes can't reach others, though each may still be running | a road closure splitting a town into two halves |
| System model | the assumptions a protocol is designed and proven under (timing, faults) | the rules of the game before you pick a strategy |
| Synchronous | message delays and processing speed have known upper bounds | a train timetable that is always met |
| Asynchronous | no bounds at all: a message may take any finite time | a letter sent with no delivery estimate |
| Partially synchronous | bounds exist but are unknown, or hold only after some unknown point | "the post is usually on time, except around the holidays" |
| GST | Global Stabilization Time: the unknown moment after which bounds hold | the day the holiday mail backlog clears |
| Crash-stop | a node fails by halting forever | an employee who quits and never returns |
| Crash-recovery | a node halts and later restarts, keeping only what it wrote to disk | an employee who goes home sick and comes back with only their notebook |
| Omission failure | a node silently fails to send or receive some messages | a mail carrier who loses some letters |
| Byzantine failure | a node behaves arbitrarily: lies, corrupts, contradicts itself | an employee who tells different colleagues different stories |
| Gray failure | partly broken: healthy by some checks, broken for real work | a shop with the lights on and nobody at the till |
| Quorum | the minimum number of nodes that must agree for an action to count | the minimum number of board members for a valid vote |
| Consensus | getting nodes to agree on one value despite failures | a jury reaching one verdict |
| FLP | proof that no deterministic protocol can guarantee consensus terminates in a fully asynchronous system with one crash | you can't guarantee a committee ever decides if members may leave silently and mail has no deadline |
| CAP | during a partition, a replicated register can't be both linearizable and always answering | during a road closure, a two-branch shop can't both keep selling and keep one exact stock count |
| PACELC | CAP, plus: when there is no partition, you still trade latency against consistency | even on normal days, calling head office for the exact count is slower than guessing locally |
| Linearizable | every operation looks instant and happens in real-time order, as if on one copy | one shared whiteboard everyone looks at |
| Eventual consistency | if writes stop, copies eventually agree; no promise about what you see before that | gossip that eventually reaches everyone |
| Read-your-writes | you always see your own earlier writes | after you post a letter, your own copy shows it sent |
| Idempotent | doing it twice has the same effect as once | pressing an elevator button twice |
| At-least-once | retried until acknowledged, so possibly duplicated | a courier who re-delivers when unsure you got it |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| `n` | number of nodes (replicas) in a group | 3, 5, 7 | a 5-node etcd cluster |
| `f` | maximum number of faulty nodes the design tolerates | 1 – 3 | f = 2 with n = 5 (crash faults) |
| `q` | quorum size: nodes that must respond for an operation to count | majority = ⌊n/2⌋ + 1 | q = 3 of n = 5 |
| `N`, `R`, `W` | Dynamo-style: replicas per key, read and write quorum sizes | N = 3, R = W = 2 | R + W > N means read and write sets overlap |
| `Δ` (delta) | upper bound on message delay in a synchronous model | known constant | "every message arrives within 100 ms" |
| GST | Global Stabilization Time (partial synchrony) | unknown | after GST every message arrives within Δ |
| `ρ` (rho) | clock drift rate bound | 10⁻⁶ – 10⁻⁴ (1–100 ppm) | 50 ppm ≈ 4.3 s/day |
| `ε` (epsilon) | clock uncertainty (how wrong a clock may be) | 1 – 7 ms (Spanner TrueTime), 100s of ms (NTP over WAN) | Spanner waits out ε at commit |
| RTT | round-trip time between two nodes | 0.1–1 ms same AZ, 1–2 ms cross-AZ, 60–150 ms cross-continent | a majority write costs ≥ 1 RTT to the quorum |
| LSN | log sequence number: position in a write-ahead log | monotonically increasing | replica has applied up to LSN 0/3A000128 |
| replica lag | how far a replica is behind the leader | ms to seconds (async) | 1.5 s during a report job |
| RPO | recovery point objective: how much acknowledged data a failover may lose | 0 (sync) to seconds (async) | lag 300 ms × 30 writes/s ≈ 9 writes at risk |
| timeout `T` | how long a caller waits before treating a call as failed | 100 ms – 30 s | payment call: 10 s |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. What "Distributed" Means — Partial Failure and the Eight Fallacies

> **In plain words.** A system is distributed when its parts talk over a network and can fail
> independently. The defining problem is not scale; it is that a request can end in "I don't know",
> and your code must decide what to do without knowing.
>
> **Real-world example.** The sneaker shop's checkout calls the payment provider. The call times
> out. The request may have been lost on the way, processed with the reply lost, or still be
> running. All three look identical to the shop.

### 1.1 A definition that matters

Lamport's quip is still the best definition: *a distributed system is one in which the failure of a
computer you didn't even know existed can render your own computer unusable.* More usefully:

> A **distributed system** is a set of processes that communicate only by sending messages over a
> network, where processes and links can fail **independently**, and no process can observe the
> global state directly.

Three consequences follow, and every later chapter is a response to them:

1. **No global clock.** Each node has its own clock that drifts; "which happened first?" across
   nodes has no free answer (clocks and ordering: `../databases/19-distributed-databases-deep-dive.md` §3).
2. **No global view.** A node knows only what messages told it, and those messages describe the
   past. By the time you act on "the leader is node 3", it may not be.
3. **Partial failure.** Some parts fail while others run. A single computer is engineered to be
   deterministic: a hardware fault usually becomes a crash (kernel panic, machine check), not a
   wrong answer. A distributed system cannot "crash as a whole" on a fault, so it must keep running
   with some parts broken and some parts merely *suspected* broken.

### 1.2 The three outcomes of a remote call

```
  Client                         Network                         Server
    │                                                              │
    │──── request ──────────X  (1) request lost                    │
    │                                                              │
    │──── request ─────────────────────────────────────────────────►  processes, commits
    │                     X──────────────────────── response ◄─────│  (2) response lost
    │                                                              │
    │──── request ─────────────────────────────────────────────────►  (3) still working
    │                                                  (GC pause, queue, slow disk)
    │
    │   timeout T expires. The client sees the SAME thing in all three cases:
    │   silence. It cannot tell "not done" from "done" from "not done yet".
    ▼
  Outcomes of any RPC:  SUCCESS  |  FAILURE (explicit error)  |  UNKNOWN (timeout)
```

The UNKNOWN outcome is the heart of the subject. Its consequences:

- **Retries are only safe for idempotent operations** (§10). "Charge card $80" retried after case
  (2) charges twice.
- **Timeouts are guesses, not facts.** A timeout converts "slow" into "failed" by decree. Choosing
  the timeout is choosing a false-positive rate (`29-failure-detection-phi-accrual.md`).
- **The server may act after the client gave up.** Case (3) means work can complete *after* the
  caller decided it failed and moved on, so the system must tolerate late, stale actors
  (fencing tokens: `03-consensus-raft-and-distributed-locking.md` §9.2).

### 1.3 The eight fallacies of distributed computing

Formulated at Sun Microsystems (the first seven usually attributed to Peter Deutsch, 1994; the
eighth added by James Gosling). Each is an assumption that holds on one machine and fails over a
network.

| # | Fallacy | What actually happens | Design response | Where in this repo |
|---|---|---|---|---|
| 1 | The network is reliable | Packets drop, links flap, switches fail, partitions happen | Timeouts, retries with idempotence, replication | §3, §10; `33-resilience-patterns-circuit-breakers.md` |
| 2 | Latency is zero | Same AZ ≈ 0.1–0.5 ms RTT, cross-region 60–150 ms; tails far worse than medians | Batch, avoid chatty protocols, budget RTTs per request | `17-networking-protocols-and-communication.md` |
| 3 | Bandwidth is infinite | Replication, rebalancing and backfill saturate links and starve foreground traffic | Throttle recovery traffic, compress, move computation to data | `10-sharding-and-consistent-hashing.md` §6 |
| 4 | The network is secure | Anything on the path can read, drop, replay or forge | mTLS, authentication, replay protection | `17-networking-protocols-and-communication.md` §9 |
| 5 | Topology doesn't change | Nodes are replaced, IPs move, autoscaling adds and removes members | Service discovery, membership protocols, never hardcode peers | `29-failure-detection-phi-accrual.md` |
| 6 | There is one administrator | Several teams, cloud providers and config systems change things independently | Versioned config, staged rollouts, explicit ownership | `35-reliability-math-slos-and-error-budgets.md` |
| 7 | Transport cost is zero | Serialization CPU, cross-AZ and egress fees, connection setup | Measure per-request cost; locality-aware routing | `17-networking-protocols-and-communication.md` |
| 8 | The network is homogeneous | Mixed hardware, kernels, MTUs, library versions, protocol versions | Version negotiation, backward-compatible schemas | `../databases/02-data-storage-formats-and-encoding.md` |

A ninth, unofficial fallacy worth adding: **"a slow node is a working node"** — slowness is the most
common failure and the hardest to detect (§5).

### 1.4 Why partial failure is the core problem

Everything hard in the track reduces to partial failure plus the lack of a global view:

```
  Partial failure  +  can't distinguish slow from dead  +  no global clock
        │                        │                               │
        ▼                        ▼                               ▼
  replication            failure detection               ordering / causality
  (copies survive)       (guess who is dead: ch 29)      (clocks, HLC: databases/19 §3)
        │                        │                               │
        └──────────────┬─────────┴───────────────┬───────────────┘
                       ▼                         ▼
            agreement despite failures    "what may a reader see?"
            (consensus: ch 03, FLP §6)    (consistency models: §9, ch 04)
                       │                         │
                       └────────────┬────────────┘
                                    ▼
                     trade-offs under partition and latency
                              (CAP §7, PACELC §8)
```

---

## 2. Timing Models — Synchronous, Asynchronous, Partially Synchronous

> **In plain words.** Before proving a protocol works, you must say what you assume about time:
> do messages arrive within a known deadline, eventually but with no deadline, or within a
> deadline that only holds "most of the time"? The answer decides what is even possible.
>
> **Real-world example.** Raft never returns a wrong answer however slow the network is (safety
> assumes no timing), but it only makes progress when messages arrive faster than the election
> timeout (liveness assumes partial synchrony). During a network storm it stalls; it does not lie.

### 2.1 The three models

| Model | Assumption | What it lets you do | Realistic? |
|---|---|---|---|
| **Synchronous** | Known bounds on message delay `Δ`, relative process speed, and clock drift `ρ` | Timeouts are *exact* failure detectors: no reply within `2Δ` means crashed. Consensus is solvable with any number of crash faults (in f+1 rounds) | Only in special hardware (avionics buses, some real-time systems). Not on shared networks, VMs, or anything with GC |
| **Asynchronous** | No bounds at all. Messages eventually arrive (if links are reliable), processes eventually take steps | Protocols proven here are correct under any delays. But deterministic consensus is impossible with even one crash (FLP, §6) | Pessimistic but honest: the model to prove **safety** in |
| **Partially synchronous** (Dwork, Lynch, Stockmeyer 1988) | Either bounds exist but are unknown, or known bounds hold only after an unknown **Global Stabilization Time (GST)** | Consensus is solvable with n ≥ 2f+1 (crash) or n ≥ 3f+1 (Byzantine). Safety never depends on timing; liveness holds after GST | The working model of the industry: Paxos, Raft, Zab, PBFT |

```
  message delay
      ▲
      │ ▲       ▲▲                            asynchronous: unbounded
      │ █   ▲   ██  ▲                         spikes at any time, forever
      │ █ ▲ █ ▲ ██ ▲█
      │─█─█─█─█─██─██──────── Δ ──────────────────────────────────────────
      │ █ █ █ █ ██ ██ ▄ ▄ ▄ ▄ ▄ ▄ ▄ ▄ ▄ ▄      partially synchronous:
      │ █ █ █ █ ██ ██ █ █ █ █ █ █ █ █ █ █      chaos, then after GST
      └───────────────┼──────────────────► time      every message within Δ
                     GST (unknown to the protocol)
```

### 2.2 The design rule this produces

**Safety in the asynchronous model, liveness in the partially synchronous model.**

- **Safety** ("nothing bad ever happens": two leaders never commit conflicting entries, a read never
  returns a value that was never written) must hold under arbitrary delays, because you cannot rule
  delays out.
- **Liveness** ("something good eventually happens": a request eventually commits) is allowed to
  depend on the network behaving for long enough.

Consensus protocols implement this with **timeouts that adapt**: if the unknown bound is larger than
the current timeout, timeouts grow (Paxos implementations back off; Raft uses randomized election
timeouts so candidates stop colliding) until they exceed the real delay. The protocol never needs
to know `Δ`; it only needs it to exist eventually.

### 2.3 Timing assumptions hide in "asynchronous" systems

Many systems that claim not to depend on timing do, through the back door:

| Mechanism | Hidden timing assumption | What breaks if violated |
|---|---|---|
| Leader lease / lock lease | Clock drift between holder and grantor is bounded; the holder is not paused longer than the lease | Two nodes act as leader; a paused lock holder writes after expiry (use fencing tokens) |
| Raft lease reads (`03-consensus-raft-and-distributed-locking.md` §7) | Bounded clock drift across the election timeout | Stale reads from a deposed leader |
| Spanner commit wait | TrueTime uncertainty `ε` is a true bound | External consistency lost |
| Last-writer-wins by wall-clock timestamp | Clocks are close | A node with a fast clock silently wins every conflict |
| Session and TTL expiry | The process notices time passing | A 30 s GC pause outlives a 10 s session |

When reviewing a design, ask "which step assumes a bound on delay, pause or clock skew?" Every
such step is a place where a stop-the-world pause or a clock step turns into a correctness bug
rather than a slowdown.

---

## 3. Network Models — Loss, Reordering, Duplication, Partitions

> **In plain words.** The network can lose, delay, reorder and duplicate messages, and it can cut
> nodes off from each other in odd shapes. Protocols state which of these they tolerate and build
> reliable channels out of unreliable ones with retransmission and sequence numbers.
>
> **Real-world example.** A mobile app sends "add item" over HTTPS; the connection drops, the app
> reconnects and resends. TCP guaranteed ordering and no duplicates *within* each connection, but
> across the two connections the server sees the request twice.

### 3.1 Link abstractions

The standard textbook ladder (Cachin, Guerraoui, Rodrigues):

| Link type | Guarantee | Built from |
|---|---|---|
| **Fair-loss link** | A message sent infinitely often is delivered infinitely often; finite duplication; no invented messages | The raw network (UDP-like) |
| **Stubborn link** | Every message sent is delivered infinitely often (if the receiver is correct) | Fair-loss + retransmit forever |
| **Perfect (reliable) link** | Every message sent to a correct receiver is delivered **exactly once**; no invented messages | Stubborn + sequence numbers + receiver-side de-duplication |
| **FIFO perfect link** | Perfect + delivered in send order | Perfect + per-sender ordering |

TCP is roughly a FIFO perfect link **per connection, while the connection lives**. It gives no
guarantee across reconnects, across different connections from the same client, or across
proxies and load balancers that retry on your behalf. An application-level retry after a
reconnect is a new message as far as TCP knows. That is why "exactly-once" at the application
layer always needs application-level identifiers (§10).

### 3.2 Message faults

| Fault | Cause in practice | Consequence if unhandled | Standard defense |
|---|---|---|---|
| **Loss** | Congestion drops, full buffers, NIC or switch faults, crashed receiver | Operation silently missing | Ack + retransmit; timeouts |
| **Delay** | Queueing, retransmission, GC pauses, cross-region paths | A message arrives after its sender's view is stale (old leader, expired lease) | Epochs/terms on every message; reject stale ones |
| **Reordering** | Multiple paths, multiple connections, retries racing originals | "Delete" applied before "create", config v2 before v1 | Sequence numbers, version checks, per-key ordering |
| **Duplication** | Retries after a lost ack, at-least-once queues, proxy retries | Double charges, double-counted events | Idempotent operations, de-duplication by request ID |
| **Corruption** | Faulty NIC, memory, disk (below TCP's weak 16-bit checksum) | Wrong data accepted as valid | End-to-end checksums (CRC32C) at the application or storage layer |

### 3.3 Partition shapes

A "network partition" is rarely a clean cut into two halves.

```
  Clean partition            Partial partition             Asymmetric (one-way)
  ───────────────            ─────────────────             ────────────────────
   A ── B    C ── D           A ────── B                    A ─────────► B
   │    │    │    │           │        │                    A ◄────X──── B
   └────┘    └────┘           │        │
      side 1   side 2         C ───X── (A↔C broken,         A hears nothing from B,
                              B↔C fine)                    B hears everything from A
                              B sees everyone; A and C
                              each think the other is dead
```

- **Partial partitions** break the assumption that "reachable" is transitive. A node that can see
  both sides can become a flapping leader or a tie-breaker that confuses everyone (the Cloudflare
  case in §14.3).
- **Asymmetric partitions** break "if I can send, I can receive". A leader that can send heartbeats
  but not receive acks will keep followers from electing a replacement while committing nothing.
  Raft's pre-vote and check-quorum extensions exist largely for these shapes.
- **Slow is a partition.** A node paused for 30 s is, to everyone else, partitioned for 30 s. It is
  also a partition that heals by itself, with the paused node unaware time has passed.

### 3.4 The Two Generals problem: why "both sides know" is impossible

Two generals must agree to attack at the same time, communicating by messengers who may be
captured. General A sends "attack at dawn". If the messenger may be lost, A must wait for an
acknowledgement; but B does not know whether the acknowledgement arrived, so B needs an
acknowledgement of the acknowledgement, and so on. **No finite number of messages over a lossy
channel makes both sides certain the other will act.** This holds with no crashes at all.

Practical meaning: you cannot build "both the order service and the payment service definitely
committed, or neither did" out of messages alone. You either accept a window of uncertainty and
repair it (retries, reconciliation, sagas), or you introduce a coordinator whose decision is final
and durable (2PC, which *blocks* when the coordinator is unreachable). Both are covered in
`06-distributed-transactions-sagas-outbox-idempotency.md`.

---

## 4. Failure Models and Quorum Sizes — Crash-Stop to Byzantine

> **In plain words.** How badly can a broken node behave? It can stop, stop and come back, drop
> messages, be late, or behave arbitrarily. The worse the behavior you must tolerate, the more
> nodes you need per failure: f+1 copies to not lose data, 2f+1 nodes to agree despite crashes,
> 3f+1 to agree despite liars.
>
> **Real-world example.** A 5-node etcd cluster tolerates 2 crashed members. A 4-node cluster still
> tolerates only 1, because the majority of 4 is 3. Adding the fourth node bought nothing except a
> bigger quorum and one more machine that can fail.

### 4.1 The hierarchy

Each model includes all the behaviors of the one above it, so a protocol that tolerates a lower
row also tolerates every higher row.

```
  EASIER TO TOLERATE
        │
        ▼  Crash-stop (fail-stop if others can reliably detect it)
        │    halts and never returns; no wrong messages
        ▼  Crash-recovery
        │    halts, restarts; keeps only what was on stable storage
        ▼  Omission (send / receive)
        │    running, but silently drops some messages
        ▼  Timing / performance (only meaningful in synchronous models)
        │    responds, but outside the promised time bound
        ▼  Byzantine (arbitrary)
        │    anything: lies, sends conflicting messages to different peers,
        │    corrupts state; includes bugs, bit flips and malicious nodes
        ▼
  HARDER TO TOLERATE
```

| Model | Typical real cause | What the protocol must add | Example systems designed for it |
|---|---|---|---|
| Crash-stop | Hardware death, decommissioning, instance termination | Replication; detect and replace | Textbook model; chain replication papers |
| Crash-recovery | Process crash + restart, OOM kill, reboot, deploy | Durable state (fsync the log, the vote, the term) before acting on it; catch-up on rejoin | Raft, Paxos, Zab, Kafka, PostgreSQL |
| Omission | Full buffers, flaky NIC, firewall rules, one-way partitions | Retransmission, acks, timeouts | Every TCP-based system implicitly |
| Timing | GC pauses, CPU starvation, disk stalls | Leases with fencing; avoid relying on bounds | Anything with leases |
| Byzantine | Malicious operators, compromised nodes, silent data corruption, nondeterministic bugs | Signatures, n ≥ 3f+1, voting on results | PBFT, Tendermint, HotStuff; blockchains |

**Crash-recovery is the realistic default** and has a sharp edge: a node that loses state it
promised to keep ("amnesia") is no longer a crash-recovery node. A Raft node that forgets its
`votedFor` after a reboot can vote twice in one term and help elect two leaders. Disks that
acknowledge `fsync` without persisting, `fsync` errors that are ignored and then retried as if
successful, and restoring a node from an old snapshot all turn "crash" into a mild form of
Byzantine behavior. Research on consensus recovery (Alagappan et al., FAST 2018) showed that
several production systems mishandle exactly these storage faults.

**Non-malicious Byzantine faults are handled without BFT protocols.** Inside one organization,
the Byzantine faults you actually meet are bit flips, corrupted pages and buggy nodes, not
adversaries. The practical defense is end-to-end checksums (Kafka record batches, HDFS blocks,
page checksums in databases), validation, and quarantining the node, not 3f+1 replication.

### 4.2 Why f+1, 2f+1, 3f+1

Two properties drive every quorum size:

- **Liveness:** with `f` nodes silent, the rest must still form a quorum: `q ≤ n − f`.
- **Safety (intersection):** any two quorums must share enough nodes that information from one
  decision reaches the next. Two sets of size `q` drawn from `n` share at least `2q − n` nodes.

```
  f+1  — enough COPIES to survive f crashes (durability only)
         One surviving copy is enough to recover the data, IF something else
         decides who is alive and which copy is current.

  2f+1 — enough nodes to AGREE despite f crashes (majority quorums)
         q = f+1, n = 2f+1:   q ≤ n − f          (2f+1 − f = f+1)   ✓ live
                              2q − n = 1         at least 1 shared node  ✓ safe
         Any two majorities overlap, so a new leader always meets at least
         one node that saw the last decision. Two disjoint halves cannot
         both decide: no split brain.

  3f+1 — enough nodes to AGREE despite f LIARS (Byzantine quorums)
         q = 2f+1, n = 3f+1:  q ≤ n − f          (3f+1 − f = 2f+1)  ✓ live
                              2q − n = f+1       shared nodes ≥ f+1
         Up to f of the shared nodes may be lying, so f+1 guarantees at least
         ONE honest node carries the truth from one quorum to the next.
         With n = 3f, the overlap would be only f nodes, all possibly liars.
```

A runnable check of those bounds (not an implementation of anything, just the arithmetic):

```python
from itertools import combinations

def min_overlap(n, q1, q2):
    """Smallest possible intersection of a q1-node set and a q2-node set out of n."""
    return max(0, q1 + q2 - n)

# Sanity-check the formula by brute force for n = 5, majority quorums of 3.
worst = min(len(set(a) & set(b))
            for a in combinations(range(5), 3) for b in combinations(range(5), 3))
assert worst == min_overlap(5, 3, 3) == 1

def tolerates(n, f, model):
    q = n - f                                 # biggest quorum still reachable with f down
    need = 1 if model == "crash" else f + 1   # Byzantine: overlap must hold an honest node
    return min_overlap(n, q, q) >= need

for f in (1, 2, 3):
    crash = min(n for n in range(1, 20) if tolerates(n, f, "crash"))
    byz = min(n for n in range(1, 20) if tolerates(n, f, "byzantine"))
    print(f"f={f}: crash needs n={crash}, Byzantine needs n={byz}")
# f=1: crash needs n=3, Byzantine needs n=4
# f=2: crash needs n=5, Byzantine needs n=7
# f=3: crash needs n=7, Byzantine needs n=10
```

**Where f+1 is enough.** Data replicas alone need only f+1 copies when the *decision* about which
copies are current is made elsewhere by a consensus service. This is the primary-backup family:

- **Kafka**: a partition with replication factor 3 and `min.insync.replicas=2` keeps accepting
  `acks=all` writes with one replica down, because the in-sync replica set (ISR) is maintained by
  the controller, which itself runs a majority-quorum protocol (KRaft, formerly ZooKeeper).
- **Chain replication** and **Vertical Paxos**: f+1 replicas for data, plus a separate
  2f+1-node configuration master.
- **The trap:** f+1 without an external arbiter is two nodes that each decide alone during a
  partition. Two-node "HA pairs" with automatic failover and no witness are split-brain machines.

### 4.3 What each timing model allows

Combining §2 and §4 gives the classic solvability table for consensus:

| Timing model | Crash faults | Byzantine faults (no signatures) |
|---|---|---|
| Synchronous | Solvable for any f < n, in f+1 rounds | n ≥ 3f+1 (Lamport, Shostak, Pease 1982) |
| Partially synchronous | n ≥ 2f+1 (DLS 1988) | n ≥ 3f+1 (DLS 1988; signatures do not lower it) |
| Asynchronous, deterministic | Impossible for f ≥ 1 (FLP 1985) | Impossible |
| Asynchronous, randomized | n ≥ 2f+1, terminates with probability 1 (Ben-Or 1983) | n ≥ 3f+1 (Bracha 1987) |

In the synchronous model with unforgeable signatures, Byzantine *broadcast* can tolerate any
number of traitors (LSP's signed-messages algorithm); that relaxation does not carry over to
partial synchrony.

### 4.4 Sizing in practice: independent failures and failure domains

`f` counts **independent** failures. Correlated failures (an AZ outage, a bad deploy to every node,
a shared power feed) count as one event that takes out several nodes at once, so place replicas so
that one domain failure costs at most `f` nodes.

| Deployment | Survives | Does not survive |
|---|---|---|
| 3 nodes, 1 AZ | 1 node | The AZ |
| 3 nodes, 2 AZs (2 + 1) | 1 node; the AZ holding 1 node | The AZ holding 2 nodes (majority lost) |
| 3 nodes, 3 AZs | Any 1 node or any 1 AZ | 2 AZs |
| 5 nodes, 3 AZs (2 + 2 + 1) | Any 2 nodes; any 1 AZ | 1 AZ + 1 more node elsewhere |
| 4 nodes, 2 AZs (2 + 2) | 1 node | Either AZ (2 of 4 is not a majority) |

**Two-AZ deployments cannot survive losing either AZ with majority quorums.** That is why managed
consensus services span three zones, and why a two-datacenter setup needs a tie-breaker (a witness
node or an arbiter) in a third location.

Amazon Aurora is the canonical example of sizing for correlated failure: six copies of each
storage segment, two per AZ across three AZs, with a write quorum of 4 and a read quorum of 3
(Verbitski et al., SIGMOD 2017). Losing a whole AZ (2 copies) leaves 4 for writes; losing an AZ
plus one more copy still leaves 3 for reads and repair.

**Even `n` buys nothing for crash tolerance.** n = 4 tolerates 1 failure, the same as n = 3, with a
larger quorum (3 instead of 2) and therefore a slower commit path. Use odd sizes; 3 or 5 for
consensus groups, rarely 7.

---

## 5. Gray Failure — Alive to the Detector, Dead to the User

> **In plain words.** The most common failure is not a clean crash but a component that is partly
> working: it answers health checks but times out on real requests, or is fast for small requests
> and stuck for large ones. Because the monitoring says "healthy", nothing fails over.
>
> **Real-world example.** A database replica's disk starts taking 3 s per `fsync`. Its heartbeat
> thread runs in memory and answers in 1 ms, so the cluster keeps it in rotation. Every write it
> participates in waits 3 s.

Huang et al. ("Gray Failure: The Achilles' Heel of Cloud-Scale Systems", HotOS 2017) define gray
failure through **differential observability**: the failure detector's view of a component
differs from the view of the applications that use it.

```
                       ┌────────────────────────────┐
   failure detector ──►│  heartbeat thread: OK      │──► "healthy"
   (probes)            │                            │
                       │  request path:             │
   applications ──────►│   slow disk / deadlocked   │──► timeouts, errors
   (real work)         │   pool / dropped large pkts│
                       └────────────────────────────┘
        the two observers disagree; the system acts on the wrong one
```

| Symptom | Typical cause | Why simple health checks miss it |
|---|---|---|
| p99 latency ×10 on one node | Failing disk, noisy neighbor, CPU throttling | Heartbeats are small, cached and prioritized |
| Some requests fail, others succeed | NIC dropping large packets, MTU mismatch, one bad path in an ECMP fabric | Probes are small and take one path |
| Node accepts connections, never responds | Deadlocked worker pool; accept thread fine | TCP connect checks only the accept path |
| Replication lag grows on one replica | Slow apply thread, long-running query holding back replay | Replica is "up"; lag is not part of the check |
| Leader heartbeats reach followers, client writes time out | Asymmetric partition; overloaded leader | Followers see the leader as alive, so no election |

**Defenses** (detail in `29-failure-detection-phi-accrual.md`, "The Gray Failure Problem" in §7):

- Probe the path users take (a readiness check that exercises the real dependency), not a
  separate heartbeat path.
- Use the clients as the detector: outlier ejection on error rate and latency relative to peers.
- Monitor gray-failure proxies directly: `fsync` latency, replication lag, queue depth.
- Prefer designs where a slow member does not slow everyone: quorum operations wait for the
  fastest `q` responses, not all `n`, which is one of the underrated benefits of majority quorums
  over "write to all replicas".

---

## 6. FLP Impossibility — What It Forbids and What It Does Not

> **In plain words.** In a network with no timing guarantees, no deterministic protocol can promise
> that a group will always *finish* agreeing if even one member might silently crash. It does not
> say agreement is dangerous or usually slow; it says there is always some unlucky schedule of
> message delays that keeps the group undecided forever.
>
> **Real-world example.** An etcd cluster on a badly congested network keeps holding elections:
> each candidate times out before collecting votes, and no leader emerges for a while. etcd never
> elects two leaders in one term (safety holds); it just stops making progress until the network
> calms down (liveness lost). That is FLP's shape in production.

### 6.1 The statement

Fischer, Lynch and Paterson (JACM, 1985): in an **asynchronous** message-passing system with
**reliable links**, there is **no deterministic** protocol that solves **consensus** if even **one**
process may fail by **crashing**.

Consensus here means every correct process decides a value such that:

- **Agreement:** no two processes decide differently.
- **Validity:** the decided value was proposed by some process.
- **Termination:** every correct process eventually decides.

FLP says you cannot guarantee all three. Every real protocol keeps agreement and validity always,
and gives up **guaranteed** termination.

### 6.2 The intuition

Call a system state **bivalent** if both decisions (0 and 1) are still reachable, and **univalent**
once only one is. The proof shows:

1. Some initial state is bivalent (otherwise the decision would be fixed by the inputs alone, and a
   crashed process whose input mattered would block everyone).
2. From any bivalent state, for any pending message `m`, the adversary can find a way to deliver
   the other messages so that the system is still bivalent after `m` is finally delivered.

The key step: a process that would tip the decision might be the one that crashed, or might just be
slow, and in an asynchronous model nobody can tell which. If the others wait for it, it may be dead
(no termination). If they proceed without it, it may be alive and slow, and the decision may still
depend on it. By always delaying the one "deciding" message, an adversarial scheduler keeps the
system bivalent forever. No crash has to actually happen; the *possibility* of one is enough.

### 6.3 What FLP does and does not forbid

| FLP forbids | FLP does **not** forbid |
|---|---|
| A deterministic consensus protocol that **always** terminates in a purely asynchronous system with one possible crash | Protocols that are **always safe** and terminate whenever the network behaves (Paxos, Raft: partial synchrony, §2) |
| | **Randomized** protocols that terminate with probability 1 (Ben-Or 1983) |
| | Termination given an **unreliable failure detector** of class ◇S / ◇W (Chandra & Toueg 1996; see `29-failure-detection-phi-accrual.md` §6) |
| | Consensus in a **synchronous** system (any number of crash faults) |
| | Problems **weaker than consensus**: a linearizable read/write register (the ABD algorithm), CRDT convergence, eventual consistency, reliable broadcast. These are solvable in the asynchronous model with a majority of correct processes |
| | Consensus being fast and reliable **in practice**: FLP is about the existence of a bad schedule, not its likelihood |

### 6.4 Why engineers care: the consensus-equivalent problems

FLP applies to every problem that is as hard as consensus. Recognizing them tells you which
features *will* stall (not break) under bad network conditions:

| Problem | Why it is consensus in disguise |
|---|---|
| Leader election (at most one leader per term) | Nodes must agree on who leads |
| Linearizable compare-and-set, unique constraint, "reserve the last unit" | A CAS object can implement consensus: every node CASes its proposal into an empty register, and the winner is the decision |
| Total-order (atomic) broadcast / replicated log | Equivalent to consensus (Chandra & Toueg 1996) |
| Distributed lock with mutual exclusion | Agreeing who holds the lock |
| Non-blocking atomic commit across shards | Agreeing commit/abort despite a crashed participant |
| Membership / configuration changes | Agreeing on the current member set |

Note the asymmetry in the table and in §6.3: a **linearizable register** (read and write only) does
not need consensus, but **compare-and-set** does (Herlihy's consensus hierarchy: read/write
registers have consensus number 1, CAS has consensus number ∞). This is the precise reason
"make the counter linearizable" is cheap relative to "make the username unique".

**Design consequence.** Put consensus-equivalent operations behind a component that is built to
stall safely (etcd, ZooKeeper, a Raft/Paxos-backed database), give them timeouts and backoff, and
keep them off paths that must stay available during partitions.

---

## 7. CAP Stated Precisely

> **In plain words.** If the network splits a replicated system into two sides that can't talk, and
> a client writes on one side and then reads on the other, the system has two choices: answer with
> possibly old data, or refuse to answer. It cannot give the up-to-date answer, because the data
> can't cross the split. That is all CAP says, and it only applies while the split lasts.
>
> **Real-world example.** The sneaker shop runs two warehouses with their own stock databases,
> syncing over a VPN. The VPN drops. A customer on the east side buys the last pair; a customer on
> the west side asks "in stock?". The west database can say "yes" (available, maybe wrong) or
> "can't check right now" (consistent, unavailable).

### 7.1 The theorem as proved

Brewer stated the conjecture in 2000; Gilbert and Lynch proved it in 2002 ("Brewer's Conjecture and
the Feasibility of Consistent, Available, Partition-Tolerant Web Services"). In their formalization:

- **Consistency (C)** means **linearizability** (atomic consistency) of a **single read/write
  register**: every operation appears to take effect at one instant between its invocation and its
  response, in an order consistent with real time.
- **Availability (A)** means **every request received by a non-failing node must eventually
  result in a response** (not an error, not a redirect to another node). There is no time bound in
  the asynchronous version; a node that hangs forever is unavailable.
- **Partition tolerance (P)** means the system keeps its guarantees even when the network may
  **lose arbitrarily many messages** between nodes.

**Theorem.** In an asynchronous network where messages may be lost, it is impossible to implement a
read/write register that guarantees both availability and atomic consistency in all executions,
including those in which messages are lost.

**Proof sketch.**

```
     ┌────────── partition: no messages cross ───────────┐
     │                                                    │
   Node G1  (holds v0)                              Node G2 (holds v0)
     ▲                                                    ▲
     │ 1. client writes v1 to G1                          │
     │    A requires G1 to reply "ok" without G2          │
     │                                                    │
     │                       2. later, a client reads at G2
     │                          A requires G2 to reply; it has only v0
     │                          C requires v1 (the write already completed)
     └──────────────────  contradiction  ─────────────────┘
```

Gilbert and Lynch also analyze a partially synchronous model and show a weaker, time-bounded
consistency ("t-connected") is achievable; the core impossibility is unchanged.

### 7.2 The honest reading

1. **P is not a choice.** Partitions are something the network does to you. The real statement is:
   *when a partition happens*, each operation either gives up C (answers with possibly stale or
   divergent data) or gives up A (some nodes refuse or wait). "CA" describes a system that is
   either not distributed or whose behavior under partition is undefined.
2. **The choice is per operation and per moment,** not per product. A system can refuse
   writes on the minority side and serve stale reads there; can be CP for `ConsistentRead=true`
   requests and AP for the rest; can choose A for the cart and C for payment.
3. **The partition decision is a timeout.** Brewer (2012, "CAP Twelve Years Later") framed it
   this way: a node waiting for a reply eventually has to decide whether to proceed without it.
   Proceeding picks A; continuing to wait or erroring picks C. A long delay and a partition are the
   same thing from the inside.
4. **Outside partitions, CAP says nothing.** Latency, throughput and failure-free behavior are
   PACELC's territory (§8).

### 7.3 Common misreadings

| Misreading | Why it is wrong |
|---|---|
| "Pick any two of C, A, P" | P is not optional in a networked system. The only real choice is what to give up *during* a partition |
| "CAP consistency = ACID consistency" | CAP-C is linearizability of one register. ACID-C is "transactions preserve application invariants", which is the application's job |
| "CAP availability = high uptime / 99.99%" | CAP-A is a per-request property of *every non-failed node*, including nodes in the minority. A CP system can have excellent uptime; a system marketed as AP may be CAP-unavailable at quorum settings (Cassandra at `QUORUM` returns errors on the minority side) |
| "MongoDB is CP, Cassandra is AP" | Labels describe a default configuration, not the software. Both change class with read and write concern / consistency level |
| "Every system is either CP or AP" | Many are neither in the formal sense. PostgreSQL with async read replicas: replica reads are not linearizable (not C), and the side without the primary cannot accept writes (not A). Kleppmann ("A Critique of the CAP Theorem", 2015) makes this case in detail |
| "CAP applies to transactions and multi-object operations" | The theorem is about one register. Its lesson generalizes (anything at least as strong as linearizability is unavailable under partition), but the statement does not |
| "AP systems are always faster" | Latency is PACELC's EL/EC axis, a separate choice. An AP system can be slow; a CP system with a nearby leader can be fast |
| "Partitions are rare, so CAP rarely matters" | GC pauses, overloaded nodes, asymmetric links and misconfigured firewalls all behave like partitions to the protocol. The decision happens on every timeout |

### 7.4 What can stay available under partition?

CAP rules out linearizability with availability. A sharper question is: *what is the strongest
guarantee that remains available?* Results by Mahajan, Alvisi and Dahlin (2011) and Bailis et al.
("Highly Available Transactions", VLDB 2014) sort the common models:

| Availability class | Models (single-object and transactional) | Meaning |
|---|---|---|
| **Totally available** (any replica can serve any client) | Monotonic reads, monotonic writes, writes-follow-reads, read committed, read uncommitted | Survives any partition with no client routing constraints |
| **Sticky available** (a client must keep talking to the same replica(s)) | Read-your-writes, PRAM, causal consistency | Available as long as the client stays on its side of the partition with its replica |
| **Unavailable** (some partitions force refusal) | Sequential consistency, linearizability, snapshot isolation, serializability, strict serializability | Requires coordination that a partition can block |

Mahajan et al. show that **real-time causal consistency** is the strongest model achievable in an
always-available, convergent system. In practice: **causal consistency is the ceiling for designs
that must never refuse service.** Anything stronger (sequential, linearizable, a uniqueness
check) must be able to say "not now".

---

## 8. PACELC — Latency Versus Consistency When Nothing Is Broken

> **In plain words.** Partitions are occasional; latency is every request. Even with a perfect
> network, keeping replicas exactly in sync means waiting for them. So a replicated system is always
> choosing: answer fast from whatever is nearby, or wait until the copies agree.
>
> **Real-world example.** A user in Singapore reads their order history from a store whose leader is
> in Virginia (about 220 ms RTT, illustrative). Reading the local replica takes 5 ms but may miss an
> order placed a second ago; reading from the leader takes 220 ms and is current.

### 8.1 The statement

Daniel Abadi ("Consistency Tradeoffs in Modern Distributed Database System Design", IEEE Computer,
2012):

> **If** there is a **P**artition, how does the system trade off **A**vailability and
> **C**onsistency; **E**lse (normal operation), how does it trade off **L**atency and
> **C**onsistency?

Written as `PA/EL`, `PC/EC`, `PA/EC`, `PC/EL`. Precision notes:

- **PACELC is a classification framework, not a theorem.** The latency side does have formal
  backing: lower bounds show that linearizable and sequentially consistent operations cannot
  complete faster than a function of network delay (Lipton & Sandberg 1988; Attiya & Welch 1994).
  The EL/EC choice is a direct consequence of replication, and it exists whether or not partitions
  ever occur.
- **The E side comes from where you put the synchronization point.** Synchronous replication or
  quorum reads = pay RTTs, get consistency (EC). Asynchronous replication or reading any replica =
  skip RTTs, risk staleness (EL).
- **The classification is per configuration and per operation,** exactly like CAP. The table below
  states the configuration assumed.

### 8.2 Classifying real systems

| System | Configuration assumed | P: A or C? | E: L or C? | Why |
|---|---|---|---|---|
| **Google Spanner** | Default read-write transactions and strong reads | PC | EC | Paxos group per split; a minority side cannot commit. Commit wait on TrueTime uncertainty makes transactions externally consistent. Bounded-staleness / exact-staleness reads are an explicit EL option |
| **CockroachDB** | Default (serializable, leaseholder reads) | PC | EC | Raft per range; ranges that lose quorum become unavailable. `AS OF SYSTEM TIME` follower reads are an EL opt-in |
| **YugabyteDB** | Default | PC | EC | Raft per tablet; follower reads with bounded staleness are opt-in (EL) |
| **Amazon DynamoDB** (one region) | Default (eventually consistent reads) | Writes C; eventually consistent reads A | EL for default reads; EC with `ConsistentRead=true` | Each partition has a leader replica that takes writes and strongly consistent reads; eventually consistent reads may hit any replica |
| **DynamoDB global tables** | Default multi-region (asynchronous) | PA | EL | Each region accepts writes locally; replication is asynchronous; conflicts resolved last-writer-wins. AWS has since added a multi-Region strong-consistency mode, which moves that configuration toward PC/EC |
| **Apache Cassandra** | `ONE` / `LOCAL_ONE` | PA | EL | Any replica answers; convergence via hinted handoff, read repair, anti-entropy |
| **Cassandra** | `QUORUM` reads and writes | Neither cleanly | EC-ish | Minority side returns errors (not A); last-writer-wins by timestamp means it is still not linearizable (not C) |
| **Cassandra** | Lightweight transactions (`IF ...`, `SERIAL`) | PC | EC | Paxos per partition key; several round trips per operation |
| **MongoDB** | Replica set, `w: "majority"` (the default since 5.0), reads from primary | PC | EC | Primary on the minority side steps down. For strictly linearizable reads use `readConcern: "linearizable"`. Abadi (2012) classified MongoDB as PA/EC under the older defaults |
| **MongoDB** | Reads from secondaries (`secondaryPreferred`) | PA for reads | EL | Secondaries lag and may serve stale data |
| **PostgreSQL + async streaming replicas** | Writes to primary, reads from replicas | Writes: PC (only the primary writes). Replica reads: PA | EL | Commits don't wait for replicas; replica reads are stale; failover can lose acknowledged commits (RPO > 0) |
| **PostgreSQL + synchronous standby** | `synchronous_commit = on` or `remote_apply`, `synchronous_standby_names` set | PC (commits block if the sync standby is unreachable) | EC for durability; replica reads EC only with `remote_apply` | The commit waits for the standby's flush (`on`) or apply (`remote_apply`) |
| **etcd** | Default (linearizable reads) | PC | EC | Raft; reads confirmed via ReadIndex. `--consistency=s` (serializable) reads are EL and may be stale |
| **ZooKeeper** | Default | PC for writes | EL for reads | Writes go through Zab and are linearizable; reads are served locally by the connected server and may be stale. `sync()` before a read narrows the gap |
| **Apache Kafka** | `acks=all`, `min.insync.replicas=2`, unclean leader election off | PC | EC | Producers get errors when the ISR shrinks below the minimum. With `acks=1` or unclean election on, it becomes PA/EL and can lose acknowledged records |
| **Redis** (primary + async replicas, Sentinel or Cluster) | Default | PA | EL | Asynchronous replication; the Redis Cluster docs state it does not guarantee strong consistency and can lose acknowledged writes on failover. `WAIT` narrows but does not close the window |
| **Azure Cosmos DB** | Strong vs Session (the default) | Strong: PC; Session/Eventual: PA | Strong: EC; Session: EL | Five account-level levels: strong, bounded staleness, session, consistent prefix, eventual |
| **Riak KV** | Default | PA | EL | Dynamo design: sloppy quorums and hinted handoff |

**Reading the table.** The PC/EC systems pay at least one quorum RTT on every write and use a
leader or lease for current reads; in exchange, the programmer rarely sees anomalies. The PA/EL
systems answer from the nearest replica and push anomalies into the application (stale reads,
conflicting writes, lost updates with last-writer-wins). The interesting rows are the ones where
the *same* product appears twice: the knob belongs to the operation.

**PC/EL and PA/EC exist but are rarer.** PA/EC: consistent in normal operation, but degrades to
serving stale or divergent data when partitioned (Abadi's example: MongoDB under its older
defaults). PC/EL: low latency normally with weaker reads, but refuses operations under partition
(Abadi's example: Yahoo PNUTS, which let users read possibly stale data locally but mastered each
record in one region).

---

## 9. Consistency Models at a Glance — Linearizability to Eventual

> **In plain words.** A consistency model is a contract about what a reader may see after other
> clients have written. Stronger contracts are easier to program against and cost coordination;
> weaker ones are cheaper and push complexity into the application. This section is the one-page
> map; `04-replication-and-consistency.md` has the anomaly walkthroughs, the session-guarantee
> implementations and the quorum math.
>
> **Real-world example.** In the sneaker shop, "stock for the last pair" needs linearizability,
> "my cart shows what I just added" needs read-your-writes, and "number of likes on a product"
> only needs eventual consistency.

### 9.1 The spectrum (single object, non-transactional)

```
                           Linearizable            ← real-time order, one copy illusion
                                │                    [unavailable under partition]
                           Sequential              ← one total order, per-client order kept,
                                │                    real time ignored   [unavailable]
                             Causal                ← cause before effect for everyone;
                        ┌───────┴────────┐           concurrent writes may differ  [sticky]
                        │                │
                   PRAM / FIFO     Writes-follow-reads
                   [sticky]          [total]
               ┌───────┼──────────┐
               │       │          │
          Read-your  Monotonic  Monotonic
          -writes    reads      writes
          [sticky]   [total]    [total]

   Eventual consistency sits apart: it is a LIVENESS promise (replicas converge
   once writes stop), not a safety rule about what a read may return.
   [total]  = totally available   [sticky] = sticky available (§7.4)
   Arrows point from stronger to weaker: a stronger model implies all below it.
```

Two composition facts worth memorizing: **PRAM = read-your-writes + monotonic reads + monotonic
writes**, and **causal = PRAM + writes-follow-reads** (Brzeziński, Sobaniec, Wawrzyniak 2003–2004).
The four session guarantees (Terry et al. 1994) are therefore the building blocks of causal
consistency, scoped to one client session.

### 9.2 One-line definitions

- **Linearizable** (Herlihy & Wing 1990): each operation appears to take effect atomically at one
  instant between its invocation and its response; if A completes before B starts, B sees A.
- **Sequential** (Lamport 1979): all clients see the same single order of operations, and each
  client's own operations appear in its program order, but that order may disagree with real time.
- **Causal** (Ahamad et al. 1995): if one write could have influenced another (same client, or a
  read in between), every client sees them in that order; concurrent writes may be seen in
  different orders. **Causal+** adds convergence (COPS, Lloyd et al. 2011).
- **PRAM / FIFO** (Lipton & Sandberg 1988): writes from any one client are seen by everyone in the
  order that client issued them; writes from different clients may interleave differently for
  different observers.
- **Read-your-writes:** a client's read reflects all of that client's earlier writes.
- **Monotonic reads:** once a client has seen a value, it never later sees an older one.
- **Monotonic writes:** a client's writes are applied everywhere in the order it issued them.
- **Writes-follow-reads:** a write issued after reading value X is ordered after the write that
  produced X, everywhere.
- **Bounded staleness:** reads may lag, but by at most `T` seconds or `K` versions. It bounds
  recency, not order, so it is incomparable with causal consistency.
- **Eventual:** if no new writes arrive, all replicas eventually return the same value. Nothing is
  promised about reads in the meantime.

### 9.3 Summary table

| Model | Anomaly it still allows (one line) | Typical mechanism | Example systems / settings | Depth |
|---|---|---|---|---|
| Linearizable | None for single objects; not multi-object atomicity | Consensus log, leader + lease or ReadIndex, CAS | etcd default reads, Spanner, CockroachDB per key, DynamoDB `ConsistentRead` on one item, MongoDB `linearizable` read concern | `04-replication-and-consistency.md`; `03-consensus-raft-and-distributed-locking.md` §7 |
| Sequential | A read that starts after a write completed may still miss it (stale but globally ordered) | Total order broadcast with local reads | ZooKeeper reads (plus per-session FIFO) | `04-replication-and-consistency.md` |
| Causal (+) | Two observers see two *unrelated* concurrent writes in opposite orders | Dependency tracking: vector clocks, HLC, `afterClusterTime` | MongoDB causally consistent sessions (with majority concerns), COPS-style stores | `04-replication-and-consistency.md`; `../databases/19-distributed-databases-deep-dive.md` §3 |
| PRAM / FIFO | A reply is visible before the post it replies to (different writers) | Per-writer sequence numbers | Per-publisher ordered streams (ordering keys) | `04-replication-and-consistency.md` |
| Read-your-writes | Other users may not see your write yet | Route to leader after writes; LSN / version token checked by the replica | PostgreSQL LSN tokens, Cosmos DB session level | `04-replication-and-consistency.md` |
| Monotonic reads | Values may be stale, but never go backwards | Sticky replica, or "highest version seen" token | Session-pinned replicas | `04-replication-and-consistency.md` |
| Bounded staleness | Stale up to `T` / `K`, and possibly reordered | Closed timestamps, lag-aware routing | Cosmos DB bounded staleness, Spanner bounded-staleness reads, CockroachDB follower reads | `04-replication-and-consistency.md` |
| Eventual | Anything before convergence: own write missing, time going backwards, lost updates under last-writer-wins | Async replication, anti-entropy, read repair | Cassandra `ONE`, DynamoDB default reads, DNS, CDN caches | `04-replication-and-consistency.md` |

### 9.4 Three facts people get wrong

- **Linearizability is composable; that does not make it transactional.** Herlihy and Wing showed
  linearizability is a *local* property: if every object is linearizable, the whole history is
  (sequential consistency is not local). But two linearizable objects do not give you an atomic
  update of both; that is what transactions add.
- **Quorum overlap is not linearizability.** `R + W > N` guarantees that a read quorum intersects
  the last *successful* write quorum. Without extra steps (read repair completed before returning,
  or the ABD write-back phase), a read concurrent with a write can return the new value while a
  later read returns the old one. With **sloppy quorums** and hinted handoff (the Dynamo design),
  the write may sit on nodes outside the key's home replicas, and even the overlap guarantee is
  gone. Worked examples: `04-replication-and-consistency.md`.
- **A cache in front of a strongly consistent database is a weakly consistent system.** The
  guarantee belongs to the whole read path (`08-caching-strategies-and-patterns.md`).

### 9.5 Bridge to transactions: strict serializability

The models above constrain operations on single objects. Transactions group operations on many
objects. The two families meet at the top:

```
                     Strict serializability
                     (= serializability + real-time order across transactions)
                      /                               \
          Serializability                         Linearizability
          (multi-object, SOME serial order,       (single object, real-time order,
           real time ignored)                      no multi-object atomicity)
```

- **Serializable** alone permits a read-only transaction to be ordered *before* a write that had
  already committed in real time, i.e., a stale read that is still "serializable".
- **Strict serializable** (Spanner calls it external consistency) forbids that: if T1 commits before
  T2 starts, T2 observes T1. CockroachDB documents that it provides serializability plus
  single-key linearizability, not full strict serializability; transactions on disjoint keys can
  exhibit the "causal reverse" anomaly.

Isolation levels, their anomalies and implementations are in
`../databases/05-transactions-and-concurrency.md` (§2 anomalies, §3 isolation levels); this track
does not re-derive them.

---

## 10. Delivery Semantics and Idempotence — Definitions

> **In plain words.** When a message or request might be lost, you choose between "maybe never"
> and "maybe twice". "Exactly once" is not a delivery guarantee the network can give; it is an
> *effect* you build from "maybe twice" plus operations that are safe to repeat.

Definitions only; mechanisms (idempotency keys, de-duplication stores, the outbox, sagas) are in
`06-distributed-transactions-sagas-outbox-idempotency.md`, and Kafka's transactional exactly-once
is in `07-kafka-and-event-streaming.md` and `22-stream-processing-flink-watermarks-eos.md`.

- **At-most-once.** Send once and never retry. A message is processed zero or one times; losses are
  possible, duplicates are not. Acceptable for telemetry samples and cache invalidation hints where
  a miss is cheap.
- **At-least-once.** Retry until an acknowledgement arrives. A message is processed one or more
  times; duplicates are possible, losses are not (while the sender survives). This is the default of
  almost every queue and every client with retries, because of the UNKNOWN outcome in §1.2.
- **Exactly-once (effect).** The *effect* happens once even though the message may be delivered
  more than once: **exactly-once = at-least-once delivery + idempotent (or de-duplicated)
  processing**, or at-least-once plus an atomic commit of "output + consumed position" in one
  system (Kafka transactions, Flink checkpoints with transactional sinks). It is never a property of
  the network alone, and it ends at the boundary of the system that enforces it: a side effect
  outside that boundary (an email, a card charge) needs its own idempotency.
- **Idempotent operation.** Applying it twice leaves the same state as applying it once:
  `set status = SHIPPED` and `DELETE id = 7` are idempotent; `balance += 80` and `INSERT` without a
  unique key are not. A non-idempotent operation becomes idempotent by attaching a unique
  **idempotency key** and recording which keys have been applied, atomically with the effect.

---

## 11. Decision Guide — Which Guarantee Does This Feature Need?

> **In plain words.** Don't pick one consistency level for the whole application. For each feature,
> ask what goes wrong if a reader sees stale data or two writers race, and buy exactly the guarantee
> that prevents it. Most features need something cheap; a few need the expensive thing.
>
> **Real-world example.** One checkout flow in the sneaker shop touches six features with four
> different minimum guarantees (table below).

### 11.1 The questions, in order

```
  For one operation of one feature:

  Q1. Is there an INVARIANT that stale or concurrent data could break?
      (stock ≥ 0, balance ≥ 0, one account per email, coupon used once)
        │
        ├── YES ─► Q1a. Can the invariant be split into local budgets or made
        │                commutative (reserve 10 units per region, append-only ledger)?
        │                 ├── YES ─► coordinate only when a budget runs out
        │                 └── NO  ─► LINEARIZABLE conditional write / serializable
        │                            transaction on the authoritative copy.
        │                            Under partition: REJECT or QUEUE, never guess.
        │
        └── NO ──► Q2. Does ONE USER notice their own history going wrong?
                         (my edit vanished, my order went back to "processing")
                          ├── YES ─► SESSION GUARANTEES: read-your-writes + monotonic reads
                          │           (token-based, not sticky routing; see ch 04)
                          └── NO ──► Q3. Do users see CAUSE AND EFFECT from others?
                                            (reply without its post, ACL change then post)
                                             ├── YES ─► CAUSAL
                                             └── NO  ─► EVENTUAL (+ convergence rule:
                                                         CRDT merge, not wall-clock LWW)

  Then: Q4. What is the latency budget? If the guarantee's RTTs don't fit,
            go back to Q1a and renegotiate the invariant with the business
            (overbooking, "accept then cancel") rather than silently weakening it.
```

### 11.2 Feature table (SMB product examples)

| Feature | What goes wrong if too weak | Minimum guarantee | Typical mechanism | Behavior under partition |
|---|---|---|---|---|
| **Shopping cart** | "I added it and it's gone"; removed item reappears | Read-your-writes + monotonic reads; convergent merge | Session token routing; cart as a set with add/remove semantics (OR-set) or single-leader per cart | Accept; merge after heal |
| **Account balance / store credit / wallet** | Double spend; negative balance | Linearizable (strictly serializable) debit | Conditional update `WHERE balance >= :amt` on the leader, or append to a ledger with a serializable check; idempotency key per payment | Reject debits; balance display may be stale with a "pending" label |
| **Likes / views / follower counts** | Count off by a few for a while | Eventual, convergent counter; read-your-writes for the user's own "liked" state | Sharded or CRDT counters, async aggregation (`../solutions/distributed-counter-design.md`) | Accept everywhere |
| **Inventory: display "12 in stock"** | Shows 12 when there are 11 | Eventual or bounded staleness | Cached or replica read | Accept (stale) |
| **Inventory: reserve the last unit** | Two customers buy one item | Linearizable conditional decrement, or a reservation with expiry | `UPDATE stock SET n = n - 1 WHERE sku = ? AND n > 0` on the leader; or per-region quotas | Reject or queue checkout |
| **Social feed / timeline** | Reply shown before the post; own post missing | Causal ideally; in practice eventual + read-your-writes for own posts + monotonic reads | Fan-out on write with the author's own write merged client-side (`../solutions/instagram-feed-design.md`) | Accept; feed may be incomplete |
| **Profile and settings edits** | Old avatar after saving; setting flips back and forth | Read-your-writes + monotonic reads | Read from leader for N seconds after write, or version token | Accept on the leader's side |
| **Order status page** | "Shipped" then "Processing" on refresh | Monotonic reads (plus read-your-writes for the buyer) | Version token or read from leader | Show last known status |
| **Unique username / email; single-use coupon; idempotency keys** | Two accounts with one email; coupon used twice | Linearizable (consensus-equivalent, §6.4) | Unique constraint on the authoritative store; conditional put | Reject sign-up / redemption |
| **Permission revocation, password change, account lock** | Revoked user still acts; old password still works on some replica | Causal at minimum (revocation must precede later actions); linearizable check on sensitive paths | Check against the leader or a version-stamped token; short-lived credentials | Fail closed on sensitive actions |
| **Rate limiter counters** | Limit exceeded by a margin | Eventual with bounded error | Local counters with periodic sync | Accept; enforce locally |
| **Search index, recommendations, analytics** | Results a few seconds behind | Eventual | CDC into the index or warehouse | Accept; catch up |
| **Job scheduling / "only one worker runs this"** | Job runs twice at the same time | Linearizable lease + fencing token on the side effect | etcd/ZooKeeper lease, or `SELECT ... FOR UPDATE SKIP LOCKED` (`../solutions/job-scheduler-postgres-deep-dive.md`) | Stall rather than double-run |

The same principle, worked through in depth with likes, the last unit and money movement:
`../databases/12-replication-and-distributed-storage.md` §1.5 and §11.

### 11.3 Rules of thumb

- **Split reads that decide from reads that display.** A product page may show a stale stock count;
  the "Buy" button must not decide from it.
- **Session guarantees fix most user complaints; they fix no invariant.** "My own edit vanished" is
  a session problem. "Two people got the last ticket" is an invariant problem, and no session
  mechanism helps because the conflict is between different users.
- **Prefer making the invariant commutative over coordinating.** Append-only ledgers, per-region
  stock budgets and CRDT counters let most writes proceed without a quorum.
- **Every "eventual" choice needs a convergence rule.** Wall-clock last-writer-wins silently
  discards concurrent writes; say which write wins and why.
- **Price the weaker choice honestly.** Eventual consistency moves cost from write-time RTTs into
  reconciliation jobs, de-duplication and support tickets, forever.

---

## 12. Production pitfalls / war stories

1. **Retrying a non-idempotent call after a timeout.** The UNKNOWN outcome (§1.2) retried as if it
   were FAILURE produces double charges, duplicate orders and double-sent emails. Every retry path,
   including those in proxies, SDKs and service meshes that retry for you, must be idempotent or
   keyed (`33-resilience-patterns-circuit-breakers.md`, ch 06).
2. **Two-node "HA" with automatic failover and no witness.** f+1 nodes without an external arbiter
   cannot distinguish "peer dead" from "link dead"; during a partition both promote themselves.
   Use three voters or an arbiter in a third failure domain (§4.4).
3. **Asynchronous replication + automatic failover = acknowledged-write loss.** The promoted replica
   lacks whatever was in flight. Quantify it: RPO ≈ replica lag × write rate. The GitHub 2018
   incident (§14.1) is this pitfall at scale.
4. **Read on a replica, decide, write on the primary.** The decision uses stale (and, on PostgreSQL
   replicas, possibly differently ordered) data. Make the write conditional on the state it depends
   on, in the same transaction on the authoritative copy
   (`../databases/12-replication-and-distributed-storage.md` §9.8).
5. **Treating `R + W > N` as "strongly consistent".** Concurrent reads can go backwards; failed
   writes can later win via read repair; last-writer-wins drops concurrent writes; sloppy quorums
   void the overlap entirely (§9.4, ch 04).
6. **Wall-clock last-writer-wins.** A node whose clock runs fast wins every conflict until it is
   corrected; the losing writes vanish without an error. Jepsen's early Cassandra analysis
   demonstrated lost acknowledged writes from exactly this combination of concurrent updates and
   timestamp ordering.
7. **Sticky sessions as a consistency guarantee.** Pinning a user to a replica gives monotonic reads
   until a failover, rebalance, pool recycle or second device. Nothing errors; the user just sees
   time go backwards. Put the guarantee in a version token, not in the routing table.
8. **Leases without fencing.** A lock holder paused past its lease (GC, VM migration, swap) wakes up
   and writes as if it still holds the lock. Leases assume bounded pauses (§2.3); fencing tokens
   checked by the storage are what make them safe (`03-consensus-raft-and-distributed-locking.md` §9.2).
9. **Lying or ignored `fsync`.** Crash-recovery protocols assume that acknowledged state survives a
   restart. Disabling fsync for speed, write caches without power-loss protection, or retrying after
   an `fsync` error converts crashes into amnesia and consensus into split brain (§4.1).
10. **Even-sized or two-zone quorums.** Four voters tolerate one failure, like three; two zones
    cannot survive losing either one with majority quorums (§4.4).
11. **Health checks that miss gray failure.** TCP connect or a heartbeat thread says "healthy" while
    the request path is deadlocked or the disk takes seconds per write (§5).
12. **A cache that undoes the database's guarantee.** A linearizable store behind a TTL cache
    serves stale reads to everyone; read-your-writes must be designed across the cache too
    (`08-caching-strategies-and-patterns.md`).

---

## 13. Interview questions

**Q1. What makes a system "distributed", and why is it hard?**
Independent failure plus communication only by messages. The hard part is partial failure with
uncertainty: a timeout does not tell you whether the other side did the work, and there is no
global clock or global view to check against.

**Q2. Explain synchronous, asynchronous and partially synchronous models. Which one do real
consensus protocols assume?**
Synchronous: known delay bounds; timeouts are exact. Asynchronous: no bounds. Partially synchronous:
bounds exist but are unknown or hold only after an unknown GST. Paxos and Raft are safe in the
asynchronous model and live in the partially synchronous model.

**Q3. Why does a majority-quorum system need 2f+1 nodes to tolerate f crashes, and a BFT system
3f+1?**
Quorums must be reachable with f nodes down (`q ≤ n − f`) and any two quorums must intersect. For
crashes, one common node is enough: `2(n − f) − n ≥ 1` gives n ≥ 2f+1. For Byzantine nodes the
intersection must contain an honest node despite f liars: `2(n − f) − n ≥ f + 1` gives n ≥ 3f+1.

**Q4. Kafka keeps writing with 2 of 3 replicas. Isn't that f+1, not 2f+1?**
Yes, for the data. It is safe because the decision about which replicas are in sync is made by a
separate majority-quorum controller (KRaft). f+1 is enough for durability when an external
consensus service arbitrates membership; it is not enough to agree on its own.

**Q5. State FLP. Does it mean consensus is impossible in practice?**
No deterministic protocol guarantees termination of consensus in an asynchronous system if one
process may crash. It does not forbid safe protocols that terminate when the network behaves
(Raft, Paxos), randomized protocols, or protocols using eventually accurate failure detectors. In
practice it means consensus can stall during bad network periods but need never be wrong.

**Q6. State CAP precisely.**
In an asynchronous network that can lose messages, no read/write register implementation can
guarantee both linearizability and that every request to a non-failed node eventually gets a
non-error response. So, during a partition, each operation must give up linearizability or
availability. P is not a choice.

**Q7. Is PostgreSQL with async read replicas CP or AP?**
Neither, formally. Replica reads are not linearizable (not C), and the side of a partition without
the primary cannot write (not A). In PACELC terms: writes PC, replica reads PA/EL. That is why
labeling systems is less useful than stating each operation's guarantee and partition behavior.

**Q8. What does PACELC add to CAP?**
The trade-off that exists without partitions: replicating consistently costs latency (waiting for
a quorum or the leader), so every system also chooses between latency and consistency in normal
operation. Spanner is PC/EC; Cassandra at `ONE` is PA/EL.

**Q9. Linearizable versus sequential versus serializable versus strict serializable?**
Linearizable: single-object, real-time order. Sequential: single-object, one agreed order that
respects each client's program order but not real time. Serializable: multi-object transactions
equivalent to some serial order, real time ignored. Strict serializable: serializable plus real
time, i.e., the transactional analogue of linearizability.

**Q10. Does `R + W > N` give linearizability?**
No. It gives overlap with the last successful write quorum. Concurrent reads can see new-then-old,
failed partial writes can resurface, last-writer-wins drops concurrent writes, and sloppy quorums
remove the overlap. Linearizable registers need an extra write-back phase (ABD); CAS needs
consensus.

**Q11. What is the strongest consistency you can keep while staying available under partition?**
Causal consistency (real-time causal, per Mahajan et al.), and it requires clients to stay with
their replica (sticky availability). Monotonic reads, monotonic writes and writes-follow-reads are
available without that constraint.

**Q12. "Exactly-once delivery" — possible?**
Not as a network property. Exactly-once *effect* is at-least-once delivery plus idempotent or
de-duplicated processing, or an atomic commit of output and input position within one system. It
does not extend to side effects outside that system.

**Q13. A user saves their profile and sees the old one after reload. Diagnose and fix.**
A read-your-writes violation: the read hit a lagging replica (or a cache). Fix with a version/LSN
token returned by the write and required by subsequent reads (replica waits or redirects to the
leader), and invalidate or bypass the cache for that user's reads. Sticky routing is a partial fix
that fails on failover.

**Q14. Which operations in an e-commerce checkout need consensus-level guarantees?**
Reserving the last unit, debiting a balance, enforcing a unique order per idempotency key, and
single-use coupons: each is a conditional update on shared state (CAS), which is consensus-hard.
Cart contents, view counts and search indexing are fine with session or eventual guarantees.

**Q15. What is gray failure and how do you detect it?**
Partial failure where the failure detector and the applications disagree (differential
observability): heartbeats fine, real requests failing or slow. Detect with probes on the real
request path, client-side outlier ejection and direct metrics (fsync latency, replication lag).

---

## 14. Real-world cases — incidents with numbers

All cases below are public postmortems, vendor announcements or Jepsen analyses. Details are
limited to what those sources state; follow the references for full accounts.

**Quick index:** async replication + failover under a short partition → 14.1 · a partial network
failure amplified by recovery traffic → 14.2 · partial partition confuses a consensus cluster →
14.3 · an eventually consistent API made strong → 14.4 · consistency depends on configuration →
14.5 · pointers to related cases in other chapters → 14.6

### 14.1 GitHub, October 21, 2018: 43 seconds of partition, 24 hours of degradation

- **Setup.** MySQL clusters managed by Orchestrator, with primaries on the US East Coast and
  replicas on the West Coast; replication across regions was asynchronous (an EL choice).
- **What happened.** Connectivity between the East Coast network hub and the primary East Coast
  data center was lost for **43 seconds** during maintenance. Orchestrator promoted West Coast
  replicas to primaries.
- **Why it hurt.** Writes accepted by the East Coast primaries shortly before the partition had not
  replicated west, and after promotion the West Coast primaries accepted new writes. The two sides
  now held different histories: in CAP terms, the failover chose availability for writes, and the
  asynchronous replication meant it came with divergent data. GitHub operated in a degraded state
  for **24 hours and 11 minutes** while restoring and reconciling data.
- **Lesson.** EL (asynchronous replication) plus automatic cross-region promotion equals PA
  behavior with data divergence, whatever the architecture diagram says. Decide in advance whether
  failover may lose acknowledged writes, and gate cross-region promotion on that decision.
  (Failure-detection angle: `29-failure-detection-phi-accrual.md` §10.5.)

### 14.2 AWS, April 21, 2011: EBS re-mirroring storm in US-East

- **Setup.** Amazon EBS replicates each volume between storage nodes within an Availability Zone,
  over a primary network and a lower-capacity secondary network.
- **What happened.** During a network change in one AZ, traffic was shifted incorrectly onto the
  lower-capacity network, which could not carry it. Many EBS nodes lost contact with their replicas
  at the same moment. When connectivity returned, they all tried to re-mirror data to new replicas
  at once, exhausting free capacity; nodes that could not find space kept searching. AWS's summary
  reports that at the peak about **13% of the volumes** in the affected AZ were stuck.
- **Lesson.** Partial failure is rarely a clean crash, and a system's *recovery* actions (re-replicate
  everything that looks under-replicated) can be the largest load it ever sees. Correlated failures
  break "f independent failures" sizing (§4.4), and recovery traffic needs throttling and back-off
  (`34-adaptive-load-control-and-backpressure.md`).

### 14.3 Cloudflare, November 2020: "A Byzantine failure in the real world"

- **Setup.** Cloudflare's control plane used an etcd (Raft) cluster, and a database high-availability
  manager that relied on it.
- **What happened.** A network switch failed partially, so that some nodes could reach each other
  while others could not: a partial partition (§3.3). The etcd cluster could not keep a stable
  leader as nodes with different views of the network triggered repeated elections, and the
  dependent database management layer reacted with failovers. The API and dashboard were degraded
  for several hours; the postmortem is Cloudflare's blog post of the title above.
- **Lesson.** Formally these were omission faults, not lying nodes, but a partial partition gives
  different nodes contradictory views of who is alive, which is why the post reached for the word
  "Byzantine". Consensus stayed safe (FLP's shape: liveness lost, safety kept), yet everything that
  treated "consensus is unavailable" as "fail over now" amplified the incident.

### 14.4 Amazon S3, December 2020: from eventual to strong read-after-write

- **Before.** S3 offered read-after-write consistency for new objects but only eventual
  consistency for overwrites and deletes, and object listings could lag behind writes. Data
  platforms built extra layers to compensate: Hadoop's S3Guard and Amazon EMR's "consistent view"
  both tracked S3 metadata in DynamoDB so that jobs would not miss freshly written files.
- **Change.** In December 2020 AWS announced strong read-after-write consistency for all S3 GET,
  PUT and LIST operations, at no additional cost and with no change in performance, according to the
  announcement.
- **Lesson.** Weak consistency is not free for users of an API: every client pays for it in
  compensating machinery. When a platform can afford the coordination internally, pushing the
  guarantee down removes whole categories of client-side systems.

### 14.5 Jepsen on MongoDB: the guarantee is in the configuration

- **2015 ("MongoDB stale reads").** Jepsen showed that MongoDB 2.6.7 replica sets could return
  stale reads and dirty reads (data from a primary that was later rolled back), even with majority
  write concern, because reads did not coordinate with the majority. MongoDB later added
  `readConcern: "majority"` and (in 3.4) `readConcern: "linearizable"`.
- **2018 (MongoDB 3.6.4).** Jepsen evaluated causally consistent sessions and found that the causal
  guarantees held only when both reads and writes used majority concerns; with weaker read or
  write concerns, sessions could observe causal violations (for example, non-monotonic reads)
  during partitions. MongoDB's documentation reflects this requirement.
- **Lesson.** A consistency model is a property of a configuration, not of a product name. When
  someone says "MongoDB is consistent", ask: which write concern, which read concern, which read
  preference, and was it tested under partitions (§8.2)?

### 14.6 Related cases elsewhere in this repo

- **Jepsen on Amazon RDS for PostgreSQL (2025): Long Fork across primary and replicas** — replicas
  can order concurrent commits differently from the primary:
  `../databases/12-replication-and-distributed-storage.md` §9.8.
- **Jepsen on etcd 3.4.3 (2020): key-value operations strict serializable, locks are leases** —
  `03-consensus-raft-and-distributed-locking.md` §17.7.

---

## Key Takeaways

1. **Partial failure with uncertainty is the defining problem.** Every remote call can end in
   UNKNOWN; design every retry path for it.
2. **State your model.** Safety in the asynchronous model, liveness in the partially synchronous
   model; find and fence every hidden timing assumption (leases, LWW clocks, TTLs).
3. **Quorum sizes come from intersection plus liveness.** f+1 copies for durability with an
   external arbiter, 2f+1 to agree despite crashes, 3f+1 despite liars. Count failure domains, not
   machines; use odd sizes across three zones.
4. **Gray failure is the common case.** Detect with the request path and with clients, not with a
   separate heartbeat.
5. **FLP costs liveness, never safety.** Consensus can stall under bad networks; it need never be
   wrong. Know which features are consensus in disguise (CAS, uniqueness, leader election, locks).
6. **CAP is narrow and precise.** One register, linearizability, availability of every non-failed
   node, only during partitions. P is not a choice; the choice is per operation.
7. **PACELC is where most of the cost lives.** Every replicated read and write chooses latency or
   consistency on a normal day. Classify operations, not products.
8. **Consistency models are a menu, not a ladder you must climb.** Causal is the ceiling for
   always-available designs; session guarantees fix most user-visible anomalies; only invariants
   need linearizability. Depth: `04-replication-and-consistency.md`.
9. **Exactly-once is an effect, not a delivery.** At-least-once plus idempotence, inside a boundary
   you control.
10. **Choose per feature.** Separate reads that decide from reads that display, make invariants
    commutative where possible, and coordinate only where an invariant demands it.

---

## Cross-References

### Within distributed-systems/
- **`04-replication-and-consistency.md`**: Replication strategies and the full consistency spectrum
  in depth — anomaly walkthroughs per level, session-guarantee implementations (LSN and version
  tokens, sticky routing), quorum math (`R + W > N`, sloppy quorums, hinted handoff, ABD). §9 of
  this chapter is its summary.
- **`03-consensus-raft-and-distributed-locking.md`**: Raft internals, linearizable reads (§7:
  ReadIndex, leases), fencing tokens (§9.2), lock services. The practical answer to FLP.
- **`06-distributed-transactions-sagas-outbox-idempotency.md`**: 2PC, sagas, the transactional
  outbox, idempotency keys and de-duplication in depth (§10 of this chapter defines the terms).
- **`07-kafka-and-event-streaming.md`**: ISR replication (f+1 data replicas with a quorum
  controller), producer acknowledgements, transactional exactly-once.
- **`08-caching-strategies-and-patterns.md`**: How caches weaken the consistency of the store
  behind them.
- **`10-sharding-and-consistent-hashing.md`**: Partitioning, replica placement across failure
  domains, quorum operations per partition (§8).
- **`17-networking-protocols-and-communication.md`**: TCP guarantees and their limits, timeouts,
  TLS.
- **`22-stream-processing-flink-watermarks-eos.md`**: Exactly-once processing via checkpoints and
  transactional sinks.
- **`29-failure-detection-phi-accrual.md`**: Detecting crashes versus slowness, Chandra–Toueg
  failure detector classes (§6), gray failure (§7).
- **`33-resilience-patterns-circuit-breakers.md`**: Retries, timeouts and their safety under the
  UNKNOWN outcome.
- **`34-adaptive-load-control-and-backpressure.md`**: Throttling recovery traffic and overload
  that looks like partition.
- **`35-reliability-math-slos-and-error-budgets.md`**: Availability math, correlated failures.
- **`36-multi-region-active-active-and-geo-replication.md`**: Multi-region topologies, conflict
  resolution and CRDTs in operation, region evacuation: PACELC's EL side at global scale.

### From databases/
- **`../databases/12-replication-and-distributed-storage.md`**: CAP and PACELC in the storage
  context (§1.2–§1.4), invariants before labels (§1.5), leaderless quorums and "what W + R > N does
  not buy you" (§2.3), consistency models with real-system notes (§9), Long Fork on read replicas
  (§9.8), design playbook (§11).
- **`../databases/05-transactions-and-concurrency.md`**: Isolation levels and anomalies (§2, §3),
  distributed transactions (§6). The transactional side of §9.5.
- **`../databases/19-distributed-databases-deep-dive.md`**: Time, clocks and ordering — Lamport
  clocks, vector clocks, HLC, TrueTime (§3); conflict resolution and CRDTs (§7).
- **`../databases/16-failure-detection-and-leader-election.md`**: Failure models (§2), leader
  election, leases and fencing.
- **`../databases/14-write-ahead-log-internals.md`**: Durability and fsync, the ground truth
  behind the crash-recovery model.

### From solutions/
- **`../solutions/distributed-counter-design.md`**: Eventually consistent counters (the "likes" row
  of §11.2).
- **`../solutions/key-value-store-design.md`**: Dynamo-style quorums, hinted handoff and tunable
  consistency in a full design.
- **`../solutions/instagram-feed-design.md`**: Feed consistency trade-offs (the "feed" row).
- **`../solutions/job-scheduler-postgres-deep-dive.md`**: Exactly-one-worker execution with
  database locks.
- **`../solutions/ibkr-trading-platform-design.md`**: Balances and orders where linearizability and
  idempotence are mandatory.

---

## References

1. Fischer, M. J., Lynch, N. A., & Paterson, M. S. (1985). *Impossibility of Distributed Consensus
   with One Faulty Process.* Journal of the ACM, 32(2).
2. Dwork, C., Lynch, N., & Stockmeyer, L. (1988). *Consensus in the Presence of Partial Synchrony.*
   Journal of the ACM, 35(2).
3. Lamport, L., Shostak, R., & Pease, M. (1982). *The Byzantine Generals Problem.* ACM TOPLAS, 4(3).
4. Ben-Or, M. (1983). *Another Advantage of Free Choice: Completely Asynchronous Agreement
   Protocols.* PODC.
5. Chandra, T. D., & Toueg, S. (1996). *Unreliable Failure Detectors for Reliable Distributed
   Systems.* Journal of the ACM, 43(2).
6. Gilbert, S., & Lynch, N. (2002). *Brewer's Conjecture and the Feasibility of Consistent,
   Available, Partition-Tolerant Web Services.* ACM SIGACT News, 33(2).
7. Brewer, E. (2012). *CAP Twelve Years Later: How the "Rules" Have Changed.* IEEE Computer, 45(2).
8. Abadi, D. (2012). *Consistency Tradeoffs in Modern Distributed Database System Design.* IEEE
   Computer, 45(2).
9. Kleppmann, M. (2015). *A Critique of the CAP Theorem.* arXiv:1509.05393.
10. Herlihy, M., & Wing, J. (1990). *Linearizability: A Correctness Condition for Concurrent
    Objects.* ACM TOPLAS, 12(3).
11. Herlihy, M. (1991). *Wait-Free Synchronization.* ACM TOPLAS, 13(1).
12. Lamport, L. (1979). *How to Make a Multiprocessor Computer That Correctly Executes Multiprocess
    Programs.* IEEE Transactions on Computers, C-28(9).
13. Ahamad, M., Neiger, G., Burns, J., Kohli, P., & Hutto, P. (1995). *Causal Memory: Definitions,
    Implementation, and Programming.* Distributed Computing, 9(1).
14. Terry, D. B., Demers, A. J., Petersen, K., Spreitzer, M. J., Theimer, M. M., & Welch, B. B.
    (1994). *Session Guarantees for Weakly Consistent Replicated Data.* PDIS.
15. Mahajan, P., Alvisi, L., & Dahlin, M. (2011). *Consistency, Availability, and Convergence.*
    UT Austin Technical Report TR-11-22.
16. Bailis, P., Davidson, A., Fekete, A., Ghodsi, A., Hellerstein, J. M., & Stoica, I. (2014).
    *Highly Available Transactions: Virtues and Limitations.* VLDB.
17. Lloyd, W., Freedman, M. J., Kaminsky, M., & Andersen, D. G. (2011). *Don't Settle for Eventual:
    Scalable Causal Consistency for Wide-Area Storage with COPS.* SOSP.
18. Attiya, H., Bar-Noy, A., & Dolev, D. (1995). *Sharing Memory Robustly in Message-Passing
    Systems.* Journal of the ACM, 42(1).
19. Huang, P., Guo, C., Zhou, L., Lorch, J. R., Dang, Y., Chintalapati, M., & Yao, R. (2017).
    *Gray Failure: The Achilles' Heel of Cloud-Scale Systems.* HotOS.
20. Verbitski, A., et al. (2017). *Amazon Aurora: Design Considerations for High Throughput
    Cloud-Native Relational Databases.* SIGMOD.
21. Cachin, C., Guerraoui, R., & Rodrigues, L. (2011). *Introduction to Reliable and Secure
    Distributed Programming* (2nd ed.). Springer.
22. Alagappan, R., Ganesan, A., Lee, E., Albarghouthi, A., Chidambaram, V., Arpaci-Dusseau, A. C., &
    Arpaci-Dusseau, R. H. (2018). *Protocol-Aware Recovery for Consensus-Based Storage.* FAST.
23. Kleppmann, M. (2017). *Designing Data-Intensive Applications.* O'Reilly. Chapters 8 and 9.
24. Jepsen analyses (Kingsbury, K.): *MongoDB stale reads* (2015); *MongoDB 3.6.4* (2018);
    *etcd 3.4.3* (2020); *Amazon RDS for PostgreSQL 17.4* (2025). https://jepsen.io/analyses
25. GitHub (2018). *October 21 post-incident analysis.* GitHub Blog.
26. Amazon Web Services (2011). *Summary of the Amazon EC2 and Amazon RDS Service Disruption in the
    US East Region.*
27. Cloudflare (2020). *A Byzantine failure in the real world.* Cloudflare Blog.
28. Amazon Web Services (2020). *Amazon S3 now delivers strong read-after-write consistency.*
