# Chapter 29: Failure Detection in Distributed Systems — Phi Accrual, Heartbeating, and Timeout Tuning

## Table of Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [The Fundamental Problem of Failure Detection](#1-the-fundamental-problem-of-failure-detection)
2. [Heartbeat-Based Detection](#2-heartbeat-based-detection)
3. [Timeout Tuning — The Art and Science](#3-timeout-tuning--the-art-and-science)
4. [The Phi Accrual Failure Detector — Deep Dive](#4-the-phi-φ-accrual-failure-detector--deep-dive)
5. [Advanced Failure Detection Patterns](#5-advanced-failure-detection-patterns)
6. [Failure Detector Properties — Chandra-Toueg Classification](#6-failure-detector-properties--chandra-toueg-classification)
7. [Production Pitfalls and War Stories](#7-production-pitfalls-and-war-stories)
8. [Design Patterns and Recommendations](#8-design-patterns-and-recommendations)
9. [Interview Questions — Failure Detection](#9-interview-questions--failure-detection)
10. [Real-world cases — incidents with numbers](#10-real-world-cases--incidents-with-numbers)

---

## Start here — the whole chapter in plain words

**The problem.** In a cluster, machines have to decide whether their peers are still alive. The only
evidence is messages: "I'm alive" pings (heartbeats) that arrive, or don't. A missing message could
mean the peer crashed, or just that it is slow, paused, or cut off by the network, and you cannot
tell which from the outside. Declare death too fast and you kick out healthy machines; too slow and
you keep sending work to a dead one. This chapter is about making that call well.

**A real-world example.** A ride-hailing dispatch service runs on 12 nodes. Each node owns the live
sessions of about 5,000 of 60,000 online drivers. Every node sends a heartbeat to its peers once
per second. (Numbers are illustrative.)

- **Fixed short timeout (1.5 s).** A node hits a 2 s JVM garbage-collection pause a few times a day.
  Each time, peers see 2+ s of silence and declare it dead. Its 5,000 driver sessions move to the
  other 11 nodes (about 9% more load each). The node wakes up, rejoins, and the sessions move back.
  Riders see "searching for driver" hiccups several times a day, and nothing actually broke.
- **Fixed long timeout (30 s).** No false alarms, but when a node really crashes, its 5,000 drivers
  get no ride offers for 30 s.
- **Phi accrual detector (§4).** Instead of one fixed number, each node learns what "normal" looks
  like for each peer (average gap 1 s, how much it varies) and outputs a suspicion score φ. With
  Akka-style defaults (threshold 8, 3 s grace for pauses, 100 ms minimum spread), a 2 s GC pause
  (3 s of silence) scores φ ≈ 0, while a real crash crosses φ = 8 after about 4.5 s of silence.
- **SWIM-style indirect probes and suspicion (§5).** Before convicting, ask 3 other nodes to ping
  the suspect. If any of them gets an answer, the problem was one network path, not the node.
- **Fencing with epochs (§8).** If the "dead" node was only paused and wakes up still thinking it
  owns its drivers, its writes carry an old epoch number and the database rejects them.
- **Health checks (§8).** A node whose heartbeat thread is fine but whose request threads are
  deadlocked still looks alive to heartbeats. A `/readyz` check that does real work catches it.

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Heartbeat | a small "I'm alive" message sent on a schedule | a roommate texting "home safe" every night |
| Timeout | how long to wait before assuming the worst | "if they haven't called by 10 pm, start worrying" |
| False positive | declaring a healthy node dead | calling the police because a friend's phone battery died |
| False negative | missing a real crash | not noticing the fridge stopped working until the milk goes bad |
| Phi (φ) accrual | a suspicion score that rises the longer the silence lasts, scaled to how unusual that silence is | a parent's worry that grows each minute a usually-punctual teenager is late |
| Threshold | the φ value at which you act | "after 2 hours late, I call around" |
| Inter-arrival time | the gap between two heartbeats | the time between a bus and the next one |
| Sliding window | the last W gaps, used to learn what is normal | remembering the last 1,000 bus gaps, forgetting older ones |
| Gossip | nodes pass news to a few random peers, who pass it on | office rumors spreading |
| SWIM / indirect probe | if a node doesn't answer me, ask others to try | "can you call her? She isn't picking up for me" |
| Suspicion + incarnation | a "maybe dead" state the node can refute by saying "I'm alive, version 2" | a missing-person report cancelled when the person calls in |
| Gray failure | partly broken: alive for heartbeats, broken for real work | a shop with the lights on but nobody at the till |
| Fencing token / epoch | a number that grows with each new owner; old owners' actions get rejected | changing the locks after a new tenant moves in |
| GC pause | the program freezes to clean memory | a cashier stopping to count the drawer |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| `T_hb` | heartbeat interval: how often "I'm alive" is sent | 100 ms – 10 s | 1 s |
| `k` | timeout multiplier for a fixed timeout | 3 – 10 | 3 missed beats → dead |
| `T_timeout` | fixed timeout `= k × T_hb` | 1 – 40 s | 3 × 1 s = 3 s |
| `N` | number of nodes in the cluster | 3 – 10,000 | 12 dispatch nodes |
| `W` | sliding window size (number of stored gaps) | 100 – 1000 | Cassandra and Akka: 1000 |
| `x_i` | one observed gap between heartbeats | ≈ `T_hb` | 1003 ms |
| `μ` | mean (average) gap in the window | ≈ `T_hb` | 1000 ms |
| `σ`, `σ²` | standard deviation / variance of gaps (how jittery) | 5 – 200 ms | 50 ms |
| `t_now`, `t_last` | current time; arrival time of the last heartbeat | — | 12:00:05.2 and 12:00:04.0 |
| `Δt` | silence so far `= t_now − t_last` | — | 1.2 s |
| `z` | how many σ above the mean the silence is: `(Δt − μ)/σ` | — | (1200 − 1000)/50 = 4 |
| `F(t)` | chance a live node's heartbeat arrives within `t` (CDF) | 0 – 1 | F(1100 ms) ≈ 0.977 |
| `P_later(Δt)` | chance a live node's heartbeat would be even later `= 1 − F(Δt)` | — | 0.023 at 1100 ms |
| `φ` (phi) | suspicion `= −log10(P_later)`; φ = 1, 2, 3, 8 ↔ 10%, 1%, 0.1%, 10⁻⁸ | 0 – ∞ | φ = 4.5 at Δt = 1200 ms (μ 1000, σ 50) |
| `phi_convict_threshold` | Cassandra's φ threshold for marking a node DOWN | 8 (10–12 on cloud) | exponential model: 8 ↔ ≈ 18.4 × μ of silence |
| Akka `threshold` | Akka's φ threshold for "unreachable" | 8 | — |
| `acceptable-heartbeat-pause` | Akka grace added to the mean | 3 s | mean 1 s + 3 s = 4 s |
| `min-std-deviation` | Akka floor on σ | 100 ms | observed σ 5 ms → use 100 ms |
| `SRTT`, `RTTVAR`, `RTO` | TCP-style smoothed average, smoothed deviation, timeout | — | RTO = SRTT + 4 × RTTVAR |
| `α`, `β` | smoothing weights for SRTT / RTTVAR | 1/8, 1/4 | new SRTT = 7/8 old + 1/8 sample |
| `G` | clock granularity in the RTO formula | 1 – 10 ms | — |
| `T_min` | hard floor on any computed timeout | 2 × `T_hb` | 2 s |
| `jitter_fraction` | random spread added to heartbeat times | 0.1 – 0.5 | 1 s + up to 0.3 s |
| SWIM period `T`, `k` helpers | probe period; number of nodes asked for an indirect ping | 1 s; 3 | ping-req via 3 nodes |
| incarnation `i` | a node's own version counter used to refute suspicion | grows by 1 | suspect(B, 4) beaten by alive(B, 5) |
| suspicion timeout | how long a suspect has to refute before it is declared dead | a few seconds, scaled by `log N` | — |
| `f` | number of faulty (possibly lying) nodes tolerated | 1 – 2 | need f + 1 independent suspicions |
| `P, ◇P, S, ◇S, W, ◇W` | Chandra–Toueg failure detector classes (§6) | — | ◇ = "eventually" |
| epoch / fencing token | number that increases with each new leader or lock holder | grows by 1 | writes with epoch 5 rejected after epoch 6 |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. The Fundamental Problem of Failure Detection

> **In plain words.** From outside, a crashed machine and a very slow machine look the same: both stay silent. So every failure detector guesses, and sometimes guesses wrong. Wrongly calling a healthy node dead is often worse than being a bit slow to notice a real crash, because the reaction (moving its work) adds load everywhere.
>
> **Real-world example.** A bank's ledger database has a primary and a standby. A 10 s network blip makes the standby think the primary died; it promotes itself, and for a few minutes two machines think they are the primary. Waiting 30 s would have avoided the failover but meant 30 s of downtime on a real crash.

### Why Perfect Detection Is Impossible

Failure detection in distributed systems is not an engineering problem that better hardware or faster networks can solve. It is a theoretical impossibility in asynchronous systems. The FLP impossibility result (Fischer, Lynch, and Paterson, 1985) proves that no deterministic algorithm can achieve consensus in an asynchronous system if even a single process can crash. At the heart of FLP lies a deeper truth: in an asynchronous network, there is no way to distinguish a crashed process from an extremely slow one. A message that has not arrived might be delayed by an arbitrarily congested network, stuck behind a GC pause on the sender, or never coming because the sender's power supply failed. The observer cannot tell.

This means every failure detector operates in a regime of uncertainty. It must eventually make a decision -- declare a node alive or dead -- based on incomplete information. The decision will sometimes be wrong, and the system must be designed to tolerate those mistakes.

### Crash Detection vs Slowness Detection

These are fundamentally different problems that production systems often conflate:

**Crash detection** asks: has the remote process permanently stopped executing? The process has either segfaulted, lost power, or been OOM-killed. It will not recover without external intervention. The correct response is to reassign its work immediately.

**Slowness detection** asks: is the remote process still making progress, but too slowly to meet SLAs? The process might be thrashing on a degraded disk, fighting for CPU time against a noisy neighbor, or stuck in a long GC cycle. The correct response might be to shed load, not to fail over -- because failing over to an equally loaded node accomplishes nothing.

Most failure detectors treat both cases identically: no heartbeat arrived within the timeout, therefore the node is suspect. This conflation causes a specific class of cascading failures where a system under load starts evicting nodes precisely when it can least afford to lose capacity.

### False Positives vs False Negatives

| Error Type | Definition | Consequence |
|:---|:---|:---|
| **False Positive** | Declaring a healthy node dead | Unnecessary failover, split-brain risk, thundering-herd rebalancing, wasted capacity |
| **False Negative** | Failing to detect a crashed node | Requests routed to a dead node, timeout-driven latency spikes, stale reads, unavailability |

The asymmetry of costs matters. In most production systems, false positives are more dangerous than false negatives. A false negative means a few requests fail until the next detection cycle catches the crash -- typically seconds. A false positive can trigger a cascade: the system evicts a healthy node, redistributes its load to remaining nodes, which become overloaded and slow, which triggers more false positives, which evicts more nodes, until the entire cluster collapses.

### The Fundamental Tradeoff: Detection Speed vs Accuracy

```
            FAST DETECTION                          ACCURATE DETECTION
            (Short Timeouts)                        (Long Timeouts)
                 │                                        │
                 │  Catches crashes quickly                │  Rarely triggers false alarms
                 │  High false positive rate               │  Slow to detect actual failures
                 │  Risk of cascading evictions            │  Longer unavailability windows
                 │                                        │
                 └──────────────┬──────────────────────────┘
                                │
                        ENGINEERING GOAL:
                  Minimize detection time SUBJECT TO
                  an acceptable false positive rate
```

There is no configuration that achieves both instant detection and zero false positives. Every system makes a tradeoff, explicitly or by accident. The phi accrual detector makes this tradeoff explicit and tunable -- which is its primary contribution.

### Real-World Consequences of Getting It Wrong

**Split-brain from aggressive detection.** A network partition cuts a five-node Raft cluster into the leader plus one follower on one side and three followers on the other. If the three followers detect the leader as failed before the partition heals, they elect a new leader. If the old leader has not yet noticed the partition (asymmetric reachability), two nodes now believe they are leader. The old leader cannot commit anything (it reaches only 2 of 5 nodes, not a majority), and Raft's term numbers prevent permanent divergence, but clients talking to the old leader see their writes hang and then fail, and any reads it serves locally without a lease or read-index check can be stale.

**Cascading failure from false positives.** A Cassandra cluster under heavy compaction load experiences heartbeat delays. Gossip marks nodes as down, triggering streaming of data to remaining nodes, which increases their load, which delays their heartbeats, which triggers more evictions. The cluster death-spirals from a self-inflicted wound.

**Unnecessary failover cost.** In a primary-standby database setup, falsely detecting the primary as dead triggers a failover. The standby promotes itself, the old primary comes back, and now there is a split-brain window. Even if fencing prevents data corruption, the failover itself causes minutes of downtime, connection resets, and cache invalidation.

---

## 2. Heartbeat-Based Detection

> **In plain words.** Each node regularly says "I'm alive". If a peer stops hearing it for long enough, it gets suspicious. You can send the message directly to everyone (simple, but expensive with many nodes) or pass it around by gossip. Spread the send times out so all nodes do not talk at the same instant.
>
> **Real-world example.** 500 nodes heartbeating everyone every second is 500 × 499 = 249,500 messages per second. With gossip, each node talks to one random peer per second, about 500 gossip exchanges per second in total, and news still reaches everyone within a handful of rounds.

### Fixed-Interval Heartbeat Protocols

The simplest failure detector sends periodic heartbeat messages at a fixed interval and declares a node dead if no heartbeat arrives within a timeout period. Despite its simplicity, this is the foundation of most production systems.

```
Node A (Monitored)                    Node B (Monitor)
    │                                      │
    │───── heartbeat (seq=1) ─────────────>│  t=0ms
    │                                      │
    │───── heartbeat (seq=2) ─────────────>│  t=1000ms
    │                                      │
    │           (network delay)            │
    │───── heartbeat (seq=3) ──────...     │  t=2000ms
    │                          ...────────>│  t=2300ms  (300ms jitter)
    │                                      │
    │      X  CRASH  X                     │
    │                                      │  t=3000ms: expected heartbeat
    │                                      │  t=4000ms: expected heartbeat
    │                                      │  t=5000ms: TIMEOUT (3 missed = dead)
    │                                      │
```

The protocol has three knobs: heartbeat interval (`T_hb`), timeout multiplier (`k`), and the resulting timeout (`T_timeout = k * T_hb`). Common values: `T_hb = 1s`, `k = 3`, giving `T_timeout = 3s`. The multiplier `k` must be large enough to absorb normal jitter but small enough to detect failures promptly.

### Push vs Pull Heartbeat Models

**Push model (heartbeat).** The monitored node actively sends periodic messages to monitors. This is what Cassandra, Akka, and most gossip-based systems use. Advantages: the monitored node controls timing; no extra round-trip latency. Disadvantages: the monitor cannot distinguish a crashed node from a network partition that blocks only the heartbeat direction.

**Pull model (ping/ack).** The monitoring node sends a probe and expects a response. This is what SWIM uses and what TCP keepalives implement. Advantages: measures actual round-trip reachability; the response can carry payload (load metrics, epoch). Disadvantages: adds the probe's network latency to the detection window; the monitor must schedule probes for all nodes it watches.

**Hybrid model.** Systems like etcd and ZooKeeper use a session-based model: the client sends periodic pings to the leader, and the leader tracks session liveness. The leader simultaneously heartbeats followers via AppendEntries RPCs in Raft (or similar in Zab). This separates client liveness from cluster membership.

### Direct Heartbeating vs Gossip-Disseminated Heartbeats

**Direct heartbeating** means every node sends heartbeats directly to every other node (or to a designated monitor). Message complexity is $O(N^2)$ per interval for all-to-all, or $O(N)$ if heartbeats go to a central coordinator.

**Gossip-disseminated heartbeats** piggyback liveness information on gossip protocol messages. Each node maintains a heartbeat counter that it increments periodically. During gossip exchanges, nodes share their view of every other node's heartbeat counter. If node A sees that node C's heartbeat counter has not advanced in `T_timeout`, it suspects C. This reduces per-node message overhead to $O(1)$ gossip exchanges per interval (each exchange carries $O(N)$ state), achieving $O(\log N)$ dissemination time with high probability.

Cassandra uses gossip-disseminated heartbeats in production: each node's heartbeat state has a *generation* (set when the process starts, so it changes on restart) and a *version* that the node increments about once per second and gossips. Other nodes update their view of that state during gossip rounds and feed the arrival times of fresh heartbeat updates into the phi accrual detector.

### Heartbeat Message Design

A production heartbeat message should carry more than just "I am alive":

```
HeartbeatMessage {
    node_id:          UUID        // Unique identity of the sending node
    epoch:            uint64      // Monotonically increasing restart counter
    sequence:         uint64      // Monotonic per-epoch sequence number
    timestamp_ms:     int64       // Sender's wall-clock time (informational only)
    load_average:     float32     // CPU load for load-aware routing
    available_capacity: float32   // Remaining capacity (connections, memory, disk)
    cluster_version:  uint64      // Schema/config version for detecting stale nodes
    ack_sequence:     uint64      // Last received sequence from the peer (bidirectional)
}
```

The **epoch** field is critical: it distinguishes a node that crashed and restarted (new epoch) from one that was merely slow (same epoch). Without it, a restarted node's first heartbeat might be interpreted as proof that the old instance is still alive, masking the crash entirely. ZooKeeper's session model uses epochs (session IDs with creation timestamps) for exactly this purpose.

The **sequence number** enables detection of reordered or duplicated heartbeats. If sequence 47 arrives after sequence 49, the receiver knows message 47 is stale and should not reset its timeout based on it.

### The Thundering-Herd Problem

If all nodes in a cluster wake up simultaneously and send heartbeats at the same wall-clock instant, the network experiences a burst of $N$ messages every `T_hb` seconds. In a 1000-node cluster with 1-second heartbeats, this creates a synchronized burst of 1000 packets per second, concentrated into milliseconds, causing switch buffer overflow and packet drops -- which ironically causes the heartbeats to fail, triggering false suspicions.

### Staggered Heartbeat Scheduling

The solution is to jitter heartbeat timing:

```
next_heartbeat_time = last_heartbeat_time + T_hb + random(0, T_hb * jitter_fraction)
```

Where `jitter_fraction` is typically 0.1 to 0.5. Gossip systems such as Cassandra also pick a random peer each round, which spreads load across the cluster. etcd's Raft implementation randomizes election timeouts between `[T_election, 2 * T_election)` for a related reason: so followers do not all start elections at the same instant.

An alternative is **phase-based staggering**: assign each node a fixed offset based on its node ID:

```
offset = hash(node_id) % T_hb
next_heartbeat_time = floor(now / T_hb) * T_hb + offset
```

This deterministically spreads heartbeats across the interval without randomness, which makes timing more predictable for debugging.

---

## 3. Timeout Tuning — The Art and Science

> **In plain words.** A single fixed timeout is either too short on a bad day or too long on a good day. Better: learn the normal gap and its jitter, and set the timeout from them (like TCP does). But never let it get tighter than a safe floor.
>
> **Real-world example.** Heartbeats arrive every 1000 ms ± 1 ms on a quiet network, so a learned timeout might shrink to 1005 ms. The next harmless 50 ms delay then evicts a healthy node. A floor of 2 s prevents that.

### Why Static Timeouts Fail in Production

A static timeout of 5 seconds might be perfect for a lightly loaded cluster on a dedicated network. That same timeout becomes a source of cascading false positives when:

- **GC pauses** on the JVM can freeze a process for 200ms to 30 seconds (G1 mixed collections, ZGC allocation stalls, full GC under heap pressure). During the pause, no heartbeats are sent or processed.
- **Network congestion** from a backup job, a burst of cross-rack traffic, or a switch firmware bug can spike latency from sub-millisecond to hundreds of milliseconds.
- **Disk I/O stalls** when the OS flushes dirty pages or a compaction storm saturates disk bandwidth can block any thread that tries to write (including the heartbeat thread if it logs to disk).
- **CPU starvation** from a noisy neighbor on shared infrastructure, or from the application itself during a CPU-intensive operation (compaction, index building, checkpointing).

Each of these events produces a temporary spike in heartbeat inter-arrival times. A static timeout cannot adapt; it either tolerates these spikes (by being long enough to cover the worst case, at the cost of slow detection) or it does not (and fires false positives).

### Adaptive Timeout Based on RTT Distributions

The key insight is that heartbeat inter-arrival times follow a distribution that can be estimated online. Instead of a fixed timeout, the system maintains a running estimate of the expected inter-arrival time and its variance, then sets the timeout as a function of both.

### Jacobson/Karels Algorithm: TCP-Style RTT Estimation

The most widely deployed adaptive timeout algorithm comes from TCP (RFC 6298). It tracks a smoothed round-trip time (SRTT) and a round-trip time variation (RTTVAR), then computes a retransmission timeout (RTO):

**Initialization** (on first measurement $R$):

$$SRTT = R$$
$$RTTVAR = R / 2$$
$$RTO = SRTT + \max(G, 4 \cdot RTTVAR)$$

**Subsequent measurements** (new sample $R'$):

$$RTTVAR = (1 - \beta) \cdot RTTVAR + \beta \cdot |SRTT - R'|$$
$$SRTT = (1 - \alpha) \cdot SRTT + \alpha \cdot R'$$
$$RTO = SRTT + \max(G, 4 \cdot RTTVAR)$$

Where $\alpha = 1/8$, $\beta = 1/4$, and $G$ is the clock granularity.

**Worked example.** Suppose a heartbeat system starts with inter-arrival times of 1000ms:

```
Step 1: First measurement R = 1000ms
  SRTT    = 1000
  RTTVAR  = 500
  RTO     = 1000 + 4*500 = 3000ms

Step 2: R' = 1050ms (slight delay)
  RTTVAR  = 0.75*500 + 0.25*|1000 - 1050| = 375 + 12.5 = 387.5
  SRTT    = 0.875*1000 + 0.125*1050 = 875 + 131.25 = 1006.25
  RTO     = 1006.25 + 4*387.5 = 2556.25ms

Step 3: R' = 1800ms (GC pause on sender)
  RTTVAR  = 0.75*387.5 + 0.25*|1006.25 - 1800| = 290.6 + 198.4 = 489.0
  SRTT    = 0.875*1006.25 + 0.125*1800 = 880.5 + 225 = 1105.5
  RTO     = 1105.5 + 4*489 = 3061.5ms

Step 4: R' = 1010ms (back to normal)
  RTTVAR  = 0.75*489 + 0.25*|1105.5 - 1010| = 366.75 + 23.9 = 390.6
  SRTT    = 0.875*1105.5 + 0.125*1010 = 967.3 + 126.3 = 1093.6
  RTO     = 1093.6 + 4*390.6 = 2656.0ms
```

Notice how the algorithm adapts: after the 1800ms spike, the RTO increases to absorb similar future spikes. As measurements return to normal, the RTO gradually decreases but retains memory of the variance.

### Sliding Window Approaches

The Jacobson/Karels algorithm uses exponential smoothing, which gives exponentially decaying weight to older samples. An alternative is to maintain an explicit sliding window of the last $W$ inter-arrival times and compute statistics directly:

```
window = circular_buffer(capacity=W)  // e.g., W = 1000

on_heartbeat_received():
    interval = now - last_arrival_time
    window.push(interval)
    last_arrival_time = now

compute_timeout():
    mean = window.mean()
    stddev = window.stddev()
    return mean + k * stddev          // k = 3 or 4 for safety margin
```

The sliding window approach has two advantages over exponential smoothing: (1) you can compute arbitrary statistics (median, percentiles, distribution shape) that EWMA cannot, and (2) old outliers are explicitly evicted after $W$ samples rather than exponentially decayed. The phi accrual detector uses exactly this approach.

The disadvantage is memory: storing 1000 samples per monitored node costs $O(N \cdot W)$ memory. For a 500-node cluster monitoring all peers with $W = 1000$ and 8-byte timestamps, that is $500 \times 1000 \times 8 = 4\text{MB}$ -- negligible for modern systems.

### The Danger of Tuning Too Aggressively

Adaptive timeouts can be too adaptive. If the algorithm tracks a period of unusually stable, low-jitter heartbeats and tightens the timeout aggressively, it becomes hypersensitive to the next normal variation:

```
Steady state: intervals = [1000, 1001, 999, 1002, 1000, 998, ...]
  mean = 1000, stddev ≈ 1.3
  timeout = 1000 + 4*1.3 ≈ 1005ms    <-- DANGEROUSLY TIGHT

Next interval: 1050ms (normal jitter from a context switch)
  Result: FALSE POSITIVE
```

Production systems guard against this with a minimum timeout floor:

```
timeout = max(T_min, mean + k * stddev)
```

Where `T_min` is a hard floor (e.g., 2x the heartbeat interval). Akka enforces a floor on the standard deviation (`min-std-deviation`, default 100 ms) and adds a fixed grace period (`acceptable-heartbeat-pause`). etcd's defaults keep the election timeout at 10x the heartbeat interval (100 ms heartbeat, 1000 ms election timeout).

### Production Tuning Heuristics

| System | Heartbeat Interval | Default Timeout / Detector | Tuning Notes |
|:---|:---|:---|:---|
| **Cassandra** | Gossip round: 1s | Phi accrual (exponential model), `phi_convict_threshold` = 8 | Commonly raised to 10–12 on cloud/VM deployments |
| **Akka Cluster** | 1s | Phi accrual (normal model), threshold = 8, plus 3s `acceptable-heartbeat-pause` | Akka docs suggest 12 on cloud platforms such as EC2 |
| **etcd** | 100ms heartbeat | 1000ms election timeout (10x) | Increase both for high-latency networks |
| **ZooKeeper** | `tickTime` (2000ms in the sample config) | Session timeout, bounded to 2–20 x `tickTime` | Session timeout must exceed the worst GC pause |
| **Consul** | Probe: 1s (LAN), 5s (WAN); gossip: 200ms (LAN), 500ms (WAN) | SWIM + Lifeguard with suspicion | Tunable per datacenter |
| **Kubernetes** | kubelet status/lease: 10s | `node-monitor-grace-period` 40s (50s in newer releases) | `--node-status-update-frequency` |

---

## 4. The Phi (φ) Accrual Failure Detector — Deep Dive

> **In plain words.** Instead of "alive or dead", the detector gives a suspicion score φ. The score asks: how unusual is this silence for this node? φ = 1 means a live node would be this late 10% of the time, φ = 2 means 1%, φ = 3 means 0.1%, φ = 8 means one in 100 million. Each application picks its own threshold.
>
> **Real-world example.** Heartbeats normally arrive every 1000 ms with a spread of 50 ms. After 1050 ms of silence φ ≈ 0.8 (nothing unusual). After 1200 ms φ ≈ 4.5. After 1300 ms φ ≈ 9, over the common threshold of 8.

### Origin and Motivation

The phi accrual failure detector was introduced by Naohiro Hayashibara, Xavier Defago, Rami Yared, and Takuya Katayama in their 2004 paper *"The φ Accrual Failure Detector."* The core motivation was dissatisfaction with binary failure detectors: traditional detectors output a boolean (alive or dead) at each query, forcing the detector designer to embed a fixed threshold. Different applications on the same system might want different thresholds -- a leader election protocol needs high confidence before triggering failover, while a load balancer can afford to be more aggressive.

### Core Insight: Continuous Suspicion Level

Instead of outputting a binary decision, the phi accrual detector outputs a continuous **suspicion level** $\varphi$ (phi). The value of $\varphi$ represents the confidence that the monitored node has crashed, expressed on a logarithmic scale. The application then compares $\varphi$ against its own threshold to make the binary decision.

This decouples the detection mechanism (statistical modeling of heartbeat arrivals) from the detection policy (threshold selection per use case).

```
Traditional Detector:          Phi Accrual Detector:

  Input: heartbeats              Input: heartbeats
    │                               │
    ▼                               ▼
  ┌─────────┐                   ┌────────────────┐
  │ Compare │                   │ Compute φ from │
  │ against │                   │ arrival time   │
  │ fixed   │                   │ distribution   │
  │ timeout │                   └───────┬────────┘
  └────┬────┘                           │
       │                               ▼
       ▼                        φ = 0.5, 1.2, 3.7, 8.1, ...
  ALIVE / DEAD                         │
                                       ▼
                              Application applies threshold:
                              if φ > 8: suspect node
                              if φ > 12: declare dead
```

### How It Works Step by Step

**Step 1: Maintain a sliding window of inter-arrival times.**

Each time a heartbeat arrives, compute the interval since the previous heartbeat and store it in a bounded sliding window of size $W$ (Cassandra uses $W = 1000$).

```
arrivals = [t1, t2, t3, ..., tn]
intervals = [t2-t1, t3-t2, t4-t3, ..., tn-t(n-1)]
window = last W intervals
```

**Step 2: Compute the mean and variance of the distribution.**

From the sliding window of intervals:

$$\mu = \frac{1}{W} \sum_{i=1}^{W} x_i$$

$$\sigma^2 = \frac{1}{W} \sum_{i=1}^{W} (x_i - \mu)^2$$

**Step 3: Model the distribution as normal.**

The detector assumes inter-arrival times follow a normal distribution $\mathcal{N}(\mu, \sigma^2)$. The cumulative distribution function (CDF) is:

$$F(t) = \frac{1}{2}\left[1 + \text{erf}\left(\frac{t - \mu}{\sigma\sqrt{2}}\right)\right]$$

**Step 4: Compute phi.**

Let $t_{\text{now}}$ be the current time and $t_{\text{last}}$ be the time the last heartbeat arrived. The elapsed time since the last heartbeat is $\Delta t = t_{\text{now}} - t_{\text{last}}$.

The probability that the next heartbeat of a *live* node would have arrived by now is $F(\Delta t)$. The probability that a live node's heartbeat would arrive even *later* than now is $P_{\text{later}}(\Delta t) = 1 - F(\Delta t)$ (the survival function, or tail). The smaller this tail, the harder it is to explain the silence with normal delay.

Phi is defined as:

$$\varphi = -\log_{10}\big(P_{\text{later}}(\Delta t)\big) = -\log_{10}(1 - F(\Delta t))$$

Equivalently:

$$\varphi = -\log_{10}\left(\frac{1}{2}\left[1 - \text{erf}\left(\frac{\Delta t - \mu}{\sigma\sqrt{2}}\right)\right]\right)$$

### Interpreting Phi Values

The logarithmic scale means phi maps directly to $P_{\text{later}} = 10^{-\varphi}$: the chance that a live node, behaving as the model predicts, would be this late. That is the chance of being wrong if you declare the node dead right now (the paper calls it the mistake likelihood):

| $\varphi$ Value | $P(\text{false positive})$ | Interpretation |
|:---|:---|:---|
| 1 | 10% | Very weak suspicion. One in ten declarations would be wrong. |
| 2 | 1% | Moderate suspicion. |
| 3 | 0.1% | Strong suspicion. |
| 4 | 0.01% | Very strong suspicion. |
| 8 | $10^{-8}$ (1 in 100 million) | Cassandra/Akka default. Extremely confident. |
| 12 | $10^{-12}$ | Often suggested for cloud environments with noisier networks. |

In practice, $\varphi = 8$ means: "if heartbeat delays truly followed the fitted model, a live node would be this late only 1 time in 100,000,000." Real delays have heavier tails than the model (GC pauses, congestion), so the true false-suspicion rate is much higher than $10^{-8}$. Treat phi as a well-calibrated *scale* for picking thresholds, not a literal guarantee.

### Phi Computation Visualized

```
              Normal Distribution of Inter-Arrival Times
              μ = 1000ms, σ = 50ms

                           ┌─── μ = 1000ms
                           │
         ▲                 │
         │            ▓▓▓▓▓▓▓▓▓▓▓▓
         │         ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
         │       ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
  P(x)   │     ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
         │   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
         │  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
         │▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
         └──────┬─────────────────┬──────────────────── time
              850ms             1150ms
                                        │
                                        │  Δt = 1200ms
                                        │  (elapsed since last heartbeat)
                                        ▼
                                  ┌─────────┐
                                  │ φ ≈ 4.5 │  P_later ≈ 0.003% (z = 4)
                                  └─────────┘

         If Δt = 1500ms → φ ≈ 23    (z = 10; node is almost certainly dead)
         If Δt = 1300ms → φ ≈ 9.0   (z = 6; crosses a threshold of 8)
         If Δt = 1050ms → φ ≈ 0.8   (z = 1; normal variation, node is fine)
```

### Why a Normal Distribution — and When That Breaks

The original paper assumes inter-arrival times are normally distributed. This is a reasonable approximation when network jitter is the dominant source of variance: many small independent perturbations (routing decisions, switch buffer delays, scheduling jitter) sum to produce approximately Gaussian behavior by the Central Limit Theorem.

The assumption breaks in several important cases:

**Bimodal distributions from GC pauses.** JVM-based systems (Cassandra, Kafka, Elasticsearch) exhibit a bimodal distribution of inter-arrival times: most arrivals cluster tightly around `T_hb`, but occasional GC pauses create a second mode at `T_hb + T_gc`. A normal distribution underestimates the probability of the GC mode, causing phi to spike higher than warranted during GC pauses. GC pauses are a common cause of false positives in JVM clusters.

```
Bimodal Distribution (GC-affected system):

  ▲
  │  ▓▓▓▓▓▓                     GC mode
  │  ▓▓▓▓▓▓▓▓                    ▓▓
  │  ▓▓▓▓▓▓▓▓▓▓                  ▓▓▓
  │  ▓▓▓▓▓▓▓▓▓▓▓▓                ▓▓▓▓
  │  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓             ▓▓▓▓▓
  └──────────────────────────────────────── time
     900  1000  1100        1400  1500  1600
              ↑                    ↑
        Normal heartbeats    After GC pause
```

**Long-tailed distributions.** Network congestion events, disk I/O stalls, and container throttling produce heavy-tailed distributions where extreme delays are more likely than a Gaussian predicts. Here the normal approximation overestimates phi during tails: it suspects too early, which means more false positives (and fewer false negatives).

Implementations work around the normal model in different ways. **Cassandra** does not use the normal model at all: it uses an **exponential** model, which needs only the mean $\mu$ of the window. Then $P_{\text{later}}(\Delta t) = e^{-\Delta t/\mu}$ and $\varphi = \Delta t / (\mu \ln 10) \approx 0.434\,\Delta t/\mu$. This grows linearly with silence and is far more tolerant of occasional long gaps. **Akka** keeps the normal model but adds a fixed grace period (`acceptable-heartbeat-pause`) to the mean and a floor on the standard deviation. Operators with known GC pressure raise the threshold (10–12).

**Worked comparison** (python-checked). Heartbeats every 1 s, silence $\Delta t$ since the last one:

| Model | Parameters | Silence needed to reach $\varphi = 8$ |
|:---|:---|:---|
| Normal (paper) | $\mu$ = 1000 ms, $\sigma$ = 50 ms | ≈ 1.28 s ($z \approx 5.61$) |
| Akka (normal + grace) | mean = 1 s + 3 s pause = 4 s, $\sigma$ = 100 ms floor | ≈ 4.55 s |
| Cassandra (exponential) | $\mu$ = 1 s | $8 \ln 10 \approx$ 18.4 s |

### How Cassandra Implements the Phi Accrual Detector

Cassandra's implementation lives in `org.apache.cassandra.gms.FailureDetector`:

1. Each node bumps its heartbeat version about once per second and gossips it (one gossip round per second to a random peer).
2. When a newer heartbeat for node X is learned, the `FailureDetector` records the arrival time in a bounded `ArrivalWindow` (1000 samples).
3. On query ("is node X alive?"), it computes phi from the window's **mean only** (exponential model, see above) against the elapsed time since the last arrival.
4. If $\varphi > \text{phi\_convict\_threshold}$ (default 8), the node is convicted and marked DOWN in the gossip state.
5. The convict threshold is configurable in `cassandra.yaml` via `phi_convict_threshold`.

Key implementation details: intervals longer than a maximum (the `cassandra.fd_max_interval_ms` system property, by default about 2x the gossip interval) are not added to the window, so one extreme outlier (e.g., a 30-second partition) does not distort the mean. Separately, if the *local* node itself was paused for longer than `cassandra.max_local_pause_in_ms` (default 5 s, e.g. its own GC), it skips convicting peers for that round, because the silence was probably its own fault.

### How Akka Implements It

Akka Cluster's `PhiAccrualFailureDetector` is configured with:

- `threshold`: phi value above which a node is considered unreachable (default 8)
- `max-sample-size`: sliding window capacity (default 1000)
- `min-std-deviation`: floor on standard deviation to prevent over-sensitivity (default 100ms)
- `acceptable-heartbeat-pause`: additional grace period added to the expected interval (default 3s, critical for GC-heavy systems)
- `heartbeat-interval`: how often heartbeats are sent (default 1s); also used as the first-heartbeat estimate before real samples exist

In Akka's formula the effective mean is $\mu + \text{acceptable-heartbeat-pause}$ and the effective deviation is $\max(\sigma, \text{min-std-deviation})$. It evaluates the normal tail with a fast logistic approximation instead of `erf`.

The `min-std-deviation` floor is Akka's solution to the over-sensitivity problem described in Section 3. Even if observed variance drops to near zero, the detector never tightens below `min-std-deviation`, preventing false positives from unrealistically tight confidence intervals.

### Bootstrapping: The Cold Start Problem

When a node first joins the cluster or first contacts a new peer, there are no samples in the arrival window. The phi calculation requires at least mean and variance estimates. Approaches:

1. **Seed with synthetic samples.** Akka seeds the window with two synthetic intervals, $T_{hb} - T_{hb}/4$ and $T_{hb} + T_{hb}/4$, which gives a starting mean $\mu_0 = T_{hb}$ and standard deviation $\sigma_0 = T_{hb}/4$ (1000 ms and 250 ms with defaults). Real samples then dominate as the window fills.
2. **Seed with a conservative initial interval.** Cassandra puts an initial interval (by default about 2x the gossip interval) into a new arrival window, so a fresh peer starts with a lenient mean.
3. **Use a fixed timeout until sufficient samples accumulate.** Some custom implementations ignore phi until a few tens of samples exist and use a plain timeout meanwhile.

---

## 5. Advanced Failure Detection Patterns

> **In plain words.** If a node does not answer me, maybe only my path to it is broken. SWIM asks a few other nodes to try before raising the alarm, and even then first marks the node "suspect" so it can answer "I'm alive" before it is thrown out. Lifeguard adds: if I am the slow one, I should be slower to accuse others.
>
> **Real-world example.** Node A's ping to B times out after 500 ms. A asks C, D and E to ping B; E gets an answer, so B stays in. If none answer, B is marked suspect for a few seconds; B sees this and gossips "alive, incarnation 5", which overrides "suspect, incarnation 4".

### SWIM Protocol Failure Detection

SWIM (Scalable Weakly-consistent Infection-style Process Group Membership) takes a fundamentally different approach from heartbeat-based detection. Instead of each node monitoring every other node via continuous heartbeats, SWIM uses randomized probing:

```
SWIM Probe Cycle (node A, period T):

  1. A randomly selects target B
  2. A sends ping to B
  3. If B responds with ack → B is alive, done

  4. If B does NOT respond within timeout:
     A selects k random nodes {C, D, E}
     A sends ping-req(B) to {C, D, E}
     C, D, E each ping B directly
     If any of them gets an ack from B → B is alive
     
  5. If NO indirect ack arrives → A suspects B

     ┌───┐    ping     ┌───┐
     │ A │────────────>│ B │  (no response)
     └─┬─┘             └───┘
       │                 ▲
       │ ping-req(B)     │ ping
       │    ┌───┐        │
       ├───>│ C │────────┘
       │    └───┘
       │    ┌───┐
       ├───>│ D │────────────> B  (no response)
       │    └───┘
       │    ┌───┐
       └───>│ E │────────────> B  (ack!)
            └───┘
       
       Result: B is alive (reached via E)
```

SWIM achieves $O(1)$ expected message load per node per period (each node sends one probe per period, plus at most $k$ ping-reqs), with failure detection spread across the cluster. Because every live node picks a random target each period, the expected time until *some* node first probes a crashed member is constant: about $e/(e-1) \approx 1.58$ protocol periods for large $N$, independent of cluster size. Spreading the news to everyone by gossip (infection-style dissemination) then takes $O(\log N)$ periods. Picking targets in a shuffled round-robin order also bounds the worst case: every member is probed by a given node within $2N-1$ periods.

### Suspicion Subprotocol with Incarnation Numbers

SWIM's suspicion mechanism prevents premature conviction:

1. When node A suspects node B, it does not immediately declare B dead. Instead, it disseminates a **suspect(B, incarnation=i)** message via gossip.
2. If B is actually alive and learns of its own suspicion, it increments its **incarnation number** to $i+1$ and disseminates an **alive(B, incarnation=i+1)** message. Messages with higher incarnation numbers override lower ones.
3. If B does not refute the suspicion within a configurable timeout (`suspicion-timeout`), nodes that received the suspect message transition B to **confirmed dead** and disseminate a **confirm(B)** message.

The incarnation number is the key mechanism: it allows a healthy-but-temporarily-unreachable node to "come back from the dead" by proving it is alive with a higher incarnation number. Without incarnation numbers, a transient network partition would permanently mark a node as dead.

### Lifeguard Extensions

The Lifeguard paper (Dadgar, Phillips, and Currey at HashiCorp, 2018) identified a systematic problem with SWIM: under network stress or high load, the false positive rate increases precisely when the cluster is least able to handle unnecessary evictions. Lifeguard introduces three extensions:

1. **Local Health Multiplier (LHM).** Each node tracks its own responsiveness. If a node is slow to respond to incoming pings (because it is overloaded), it increases a local health multiplier that extends its own suspicion and probe timeouts. A node that knows it is slow gives itself and others more grace.

2. **Dynamic suspicion timeout.** Instead of a fixed suspicion timeout, Lifeguard scales the timeout with the number of independent confirmations: more nodes that independently suspect the same target increase confidence, so the timeout decreases. Conversely, a single suspicion with no corroboration gets a long timeout.

3. **Buddy system.** When a node pings a member that it currently suspects, it tells that member about the suspicion inside the ping itself. The suspected member learns right away (instead of waiting for gossip to reach it) and can refute by bumping its incarnation number.

Consul (through HashiCorp's `memberlist` library) uses Lifeguard in production. HashiCorp's experiments reported a large reduction in false positives compared to vanilla SWIM under the same stress conditions.

### Two-Phase Detection

Many production systems implement a two-stage pipeline:

```
Phase 1: SUSPECT                    Phase 2: CONFIRM
  Single observer detects               Multiple independent observers
  heartbeat timeout                     corroborate suspicion
      │                                       │
      ▼                                       ▼
  Mark node as SUSPECT               Mark node as DOWN
  Continue routing to it              Stop routing, trigger failover
  (with reduced weight)               Reassign partitions/ranges
  Wait for corroboration
```

This pattern is used in:
- **Consul**: SWIM suspicion period before conviction.
- **Kubernetes**: a node is marked `NotReady`/`Unknown` after `node-monitor-grace-period`, and pods are then evicted only after their toleration for the `not-ready`/`unreachable` taints runs out (300 seconds by default; older versions used `--pod-eviction-timeout`, also 5 minutes).
- **MongoDB**: replica set members heartbeat each other every 2 seconds; only after the primary has been unreachable for `electionTimeoutMillis` (10 seconds by default) does a secondary call an election, and winning it requires votes from a majority.

### Byzantine Failure Detection Challenges

Byzantine failure detection -- where a node may actively lie about its liveness or the liveness of others -- requires fundamentally different approaches:

- **Mutual suspicion.** In a Byzantine setting, a malicious node could falsely report others as dead to trigger unnecessary failover. Quorum-based corroboration (requiring $f+1$ independent suspicions before conviction) prevents a single Byzantine node from evicting honest nodes.
- **Authenticated heartbeats.** Heartbeat messages must be signed to prevent forgery. A Byzantine node could forge heartbeats from a crashed node to mask the crash.
- **Accountability.** Systems like PeerReview (Haeberlen et al., 2007) maintain tamper-evident logs that allow retrospective detection of Byzantine behavior, even if real-time detection is impossible.

---

## 6. Failure Detector Properties — Chandra-Toueg Classification

> **In plain words.** Theory grades failure detectors on two things: does it eventually catch every crash (completeness), and does it avoid accusing healthy nodes (accuracy)? The key result: to reach agreement you do not need a perfect detector, only one that is eventually right about at least one healthy node.
>
> **Real-world example.** Raft's election timeout makes mistakes during network trouble (extra elections), but once the network is stable it stops falsely suspecting the leader, and that is enough for the cluster to keep agreeing on the log.

Chandra and Toueg (1996) formalized failure detectors as distributed oracles that provide (possibly incorrect) hints about which processes have crashed. They defined two orthogonal properties:

### Completeness

- **Strong Completeness.** Eventually, every process that crashes is permanently suspected by every correct process.
- **Weak Completeness.** Eventually, every process that crashes is permanently suspected by at least one correct process.

Weak completeness can be transformed into strong completeness by gossiping suspicions: if any correct process suspects a crashed node, it tells everyone.

### Accuracy

- **Strong Accuracy.** No correct process is ever suspected. (Impossible in asynchronous systems without synchrony assumptions.)
- **Weak Accuracy.** At least one correct process is never suspected by any correct process.
- **Eventually Strong Accuracy.** After some unknown time $T$, no correct process is ever suspected. (False positives are allowed initially but must stop eventually.)
- **Eventually Weak Accuracy.** After some unknown time $T$, at least one correct process is never suspected.

### The Key Failure Detector Classes

| Class | Completeness | Accuracy | Symbol |
|:---|:---|:---|:---|
| Perfect | Strong | Strong | $\mathcal{P}$ |
| Eventually Perfect | Strong | Eventually Strong | $\Diamond\mathcal{P}$ |
| Strong | Strong | Weak | $\mathcal{S}$ |
| Eventually Strong | Strong | Eventually Weak | $\Diamond\mathcal{S}$ |
| Weak | Weak | Weak | $\mathcal{W}$ |
| Eventually Weak | Weak | Eventually Weak | $\Diamond\mathcal{W}$ |

### The Minimum Needed for Consensus

Chandra and Toueg showed that **$\Diamond\mathcal{W}$ (eventually weak) is sufficient to solve consensus** in an asynchronous system with crash failures and reliable channels, as long as a majority of processes are correct. Chandra, Hadzilacos, and Toueg (1996) then proved it is also the **weakest** such class. This result is profound:

- You do not need a perfect failure detector. You do not even need one that is always right. You only need one that, after some point in the execution, permanently trusts at least one correct process.
- $\Diamond\mathcal{S}$ (eventually strong) is sufficient and more practical: after some point, no correct process is falsely suspected, and all crashed processes are suspected. Most practical failure detectors target $\Diamond\mathcal{S}$.

### How These Properties Map to Real Systems

**Raft's election timeout** implements a $\Diamond\mathcal{S}$ detector. After GST (network stabilization), the timeout correctly identifies leader crashes and does not falsely suspect the leader. Before GST, false positives cause unnecessary elections, but safety is preserved by term numbers.

**Cassandra's phi accrual detector** aims to behave like $\Diamond\mathcal{P}$ in practice by using a high threshold. With $\varphi = 8$, the *model* says a live node would be this late with probability $10^{-8}$ -- the real rate is higher because real delays have heavy tails, which is why operators still see occasional false DOWN marks. The detector also achieves strong completeness: a crashed node's heartbeat counter stops advancing, causing phi to grow without bound at all observers.

**SWIM with Lifeguard** provides strong completeness (the random probe cycle ensures every crashed node is eventually probed and suspected by everyone) and eventual strong accuracy (the suspicion/incarnation mechanism and Lifeguard extensions eliminate false positives once the network stabilizes).

---

## 7. Production Pitfalls and War Stories

> **In plain words.** Most false alarms come from the node or network being slow, not dead: garbage-collection pauses, CPU starvation, clock jumps, one-way network problems. And some real failures are invisible to heartbeats, because the heartbeat thread still works while everything else is broken.
>
> **Real-world example.** A JVM node pauses 6 s for garbage collection a few times a day. With a 3 s timeout, each pause becomes a "crash", its data is re-streamed to other nodes, and their extra load causes more pauses.

### GC Pauses and False Failure Detection

This is the single most common source of false positives in JVM-based distributed systems. A G1 GC mixed collection or a CMS fallback to full GC can pause all application threads for 200ms to 30+ seconds. During this pause:

1. The paused node stops sending heartbeats.
2. The paused node stops processing incoming heartbeats and pings.
3. Other nodes' failure detectors time out and declare the node dead.
4. The paused node wakes up, finds itself evicted from the cluster, and attempts to rejoin.
5. Rejoining triggers data streaming/rebalancing, which increases load on remaining nodes, which increases their GC pressure, which can trigger their pauses.

Mitigations: (a) Tune GC to minimize worst-case pause time (use ZGC or Shenandoah for sub-millisecond pauses, or G1 with `-XX:MaxGCPauseMillis` set well below the heartbeat timeout). (b) Increase the phi threshold or timeout to accommodate expected GC pauses. (c) Use a dedicated heartbeat thread pinned to a CPU core that is excluded from GC stop-the-world pauses (possible with some JVM configurations but fragile). (d) Move to a runtime with short or no GC pauses for the critical path (ScyllaDB, a C++ reimplementation of Cassandra, cites GC-free operation as a benefit; Go's collector, used by etcd, typically stops the world for well under 1ms).

### Network Partitions vs Process Failures

From the perspective of a failure detector on node A, these two scenarios are indistinguishable:

- **Node B has crashed.** B's process is gone. No heartbeats will ever come.
- **The network between A and B is partitioned.** B is alive and functioning, serving clients on its side of the partition.

The correct response to each is radically different. For a crash, reassign B's work. For a partition, do not -- B is still serving traffic, and reassigning its work creates duplicate ownership.

This is why systems like ZooKeeper use session-based detection: the client must actively maintain its session with the leader. If the leader cannot reach the client, the session expires, and the client's ephemeral nodes are deleted. But the client is also expected to monitor its own session: if it realizes it has lost contact with the leader, it must stop acting on its locks and leases before the session timeout expires, implementing a form of cooperative self-fencing.

### CPU Starvation and Heartbeat Delays

On shared infrastructure (VMs, containers), CPU starvation from noisy neighbors or cgroup throttling can delay heartbeat threads. Unlike GC pauses (which are all-or-nothing), CPU starvation causes progressive degradation: heartbeats become increasingly delayed but never fully stop. This is particularly insidious because:

- Phi values rise slowly, hovering near the threshold without clearly crossing it.
- The node appears "flaky" -- sometimes responsive, sometimes not.
- The failure detector oscillates between suspect and alive, causing upstream routing instability.

Mitigation: dedicate CPU cores to critical system threads using `isolcpus` or cgroup CPU pinning. Monitor heartbeat latency as a first-class metric. Use `SCHED_FIFO` real-time scheduling for the heartbeat thread (Linux only, requires `CAP_SYS_NICE`).

### Clock Skew and Timeout Calculations

Failure detectors that use wall-clock timestamps for inter-arrival time calculation are vulnerable to clock adjustments. If `ntpd` or `chrony` steps the clock forward by 500ms, the current gap since the last heartbeat instantly appears 500ms longer than it really is, which can push phi over the threshold and trigger a false positive. If the clock steps backward, the next interval appears shorter (even negative), corrupting the window statistics and making the detector too tight later.

Mitigation: use monotonic clocks (`CLOCK_MONOTONIC` on Linux, `System.nanoTime()` on JVM) for all interval measurements. Monotonic clocks are immune to NTP adjustments, leap seconds, and daylight saving time changes. Every modern failure detector implementation uses monotonic time internally, but custom implementations frequently make this mistake.

### The Gray Failure Problem

Gray failures (Huang et al., 2017) are partial failures that are harder to detect than total crashes:

- A disk develops bad sectors: reads from certain ranges fail, but the process is still alive and responding to heartbeats.
- A NIC drops 5% of packets: most heartbeats arrive, but application traffic is severely degraded.
- A process deadlocks one of its worker threads: heartbeats (from a separate thread) continue, but requests time out.
- Memory corruption causes incorrect responses: the node is alive but producing wrong answers.

Heartbeat-based failure detectors miss all of these. The node is "alive" -- its heartbeat thread is running -- but it is not functioning correctly. This is why production systems implement multi-layer detection (Section 8).

### Asymmetric Network Failures

A can reach B, but B cannot reach A. This creates a paradox for failure detection:

- A's failure detector considers B alive (it receives B's heartbeats).
- B's failure detector considers A dead (it does not receive A's heartbeats).
- If B is the leader, it may step down, causing an unnecessary election.
- If A is the leader, B cannot receive its log entries, falling behind.

SWIM's indirect ping mechanism partially addresses this: if B cannot directly reach A, it asks C to relay the probe. But if the asymmetry is at the network layer (e.g., a misconfigured firewall rule), indirect probes through C may also fail if C is on A's side of the asymmetry.

---

## 8. Design Patterns and Recommendations

> **In plain words.** Use several layers of checks, because each catches different problems. Give leader detection a longer, safer timeout than everything else. And assume the detector will sometimes be wrong: make sure a node wrongly declared dead cannot do damage when it wakes up (fencing).
>
> **Real-world example.** A payments service uses TCP keepalives, a 1 s heartbeat with phi threshold 8, a /readyz check every 5 s, and an external probe every minute. The old leader with epoch 5 wakes after a pause and tries to write; the database has already seen epoch 6 and rejects it.

### Multi-Layer Detection

No single detection mechanism catches all failure modes. Production systems should layer multiple independent detectors:

```
Layer 1: Network-Level Detection
  - TCP keepalives (OS-level, detects connection drops)
  - Switch-level link failure notifications (LLDP/BFD)
  - Detection time: milliseconds to seconds

Layer 2: Process-Level Heartbeats
  - Application heartbeat protocol (phi accrual, SWIM)
  - Detects process crashes and severe unresponsiveness
  - Detection time: seconds

Layer 3: Application-Level Health Checks
  - HTTP health endpoints (/healthz, /readyz)
  - Tests actual functionality: database queries, downstream dependencies
  - Detects gray failures, deadlocks, resource exhaustion
  - Detection time: seconds to tens of seconds

Layer 4: External Observation
  - Monitoring system alerts (Prometheus, Datadog)
  - Synthetic probes from external vantage points
  - Detects datacenter-level failures invisible from inside
  - Detection time: minutes
```

Each layer catches failures the others miss. Network detection catches link failures before the heartbeat timeout. Heartbeats catch process crashes that TCP keepalives (with their multi-hour default timeouts) miss. Application health checks catch gray failures that heartbeats miss. External monitoring catches datacenter-level failures that internal detectors, by definition, cannot observe.

### Circuit Breaker Integration

Failure detection and circuit breakers operate at different layers but should share information:

```
Failure Detector (φ accrual)              Circuit Breaker
  Monitors: liveness                       Monitors: request success rate
  Granularity: per-node                    Granularity: per-endpoint
  Response: eviction/failover              Response: fast-fail requests

  Integration: φ value feeds into circuit breaker as a signal
  If φ > low_threshold: circuit breaker increases caution (tighter error budget)
  If φ > high_threshold: circuit breaker opens immediately (don't wait for errors)
```

This allows the circuit breaker to preemptively open before requests start failing, based on the statistical evidence from the failure detector that the node is becoming unresponsive.

### Leader Election Stability vs Detection Speed

In leader-based consensus systems (Raft, Multi-Paxos, ZAB), the failure detection timeout directly controls leader election frequency. A short timeout enables fast failover but causes "election storms" -- frequent unnecessary elections where the leader was merely slow, not dead.

The design principle is: **the failure detection timeout for the leader should be significantly longer than for non-leader nodes.** The cost of a false positive on the leader (election, brief unavailability, client reconnection) is much higher than the cost of a false positive on a follower (reduced read capacity but no availability impact).

etcd implements this with separate timeouts: the heartbeat interval (100ms) determines how often the leader pings followers, and the election timeout (10x heartbeat = 1s minimum) determines how long followers wait before suspecting the leader. The election timeout is randomized between `[10, 20]` heartbeat intervals to prevent simultaneous elections.

### Handling "I Think I'm Dead" — Fencing Tokens and Epochs

A node that has been declared dead by the failure detector may not know it is dead. It may still hold locks, still be writing to storage, still responding to clients. This is the **zombie node** problem.

The solution is **fencing**: every action that requires liveness must present a token that proves the actor has not been superseded:

1. **Epoch-based fencing.** Every leadership change increments a global epoch number. Storage systems reject writes with a stale epoch. If the old leader (epoch 5) tries to write after a new leader (epoch 6) has been elected, the storage layer rejects the write.

2. **Lease-based fencing.** The leader holds a time-limited lease. Before the lease expires, the leader must renew it. If the leader is partitioned and cannot renew, the lease expires, and the leader must stop acting as leader before the expiration time. This requires the leader's clock to run at least as fast as the clock of the node granting the lease -- a weaker assumption than clock synchronization.

3. **Fencing tokens.** A monotonically increasing token is issued with each lock acquisition. The storage layer tracks the highest token it has seen and rejects operations with lower tokens. Martin Kleppmann describes this pattern extensively in *Designing Data-Intensive Applications*.

```
Leader A (epoch=5)               Storage              Leader B (epoch=6)
     │                              │                       │
     │ write(key, val, epoch=5)     │                       │
     │─────────────────────────────>│                       │
     │                              │  (epoch 5 accepted)   │
     │                              │                       │
     │   ... network partition ...  │                       │
     │                              │                       │
     │                              │ write(key, val2, epoch=6)
     │                              │<──────────────────────│
     │                              │  (epoch 6 accepted)   │
     │                              │                       │
     │ write(key, val3, epoch=5)    │                       │
     │─────────────────────────────>│                       │
     │         REJECTED             │  (epoch 5 < 6, stale) │
     │<─────────────────────────────│                       │
```

### Summary of Recommendations

| Concern | Recommendation |
|:---|:---|
| **Default detector** | Phi accrual with threshold 8 for same-datacenter, 12 for cross-DC or cloud |
| **GC-heavy systems** | Increase phi threshold; set minimum variance floor; consider ZGC/Shenandoah |
| **Large clusters (>100 nodes)** | Gossip-disseminated heartbeats or SWIM; avoid $O(N^2)$ direct heartbeats |
| **Leader election** | Use longer timeout for leader detection than for follower detection |
| **Gray failures** | Supplement heartbeats with application-level health checks |
| **Clock handling** | Always use monotonic clocks for interval measurement |
| **Zombie prevention** | Implement fencing tokens or epoch-based rejection at the storage layer |
| **Bootstrapping** | Seed the arrival window with the configured heartbeat interval; use fixed timeout until sufficient samples |
| **Thundering herd** | Jitter heartbeat scheduling by 10-50% of the interval |
| **Partial failures** | Multi-layer detection: network + process heartbeat + application health check + external monitoring |

---

## 9. Interview Questions — Failure Detection

> **In plain words.** Interviewers want to see that you know a missing heartbeat is ambiguous, that
> every timeout trades speed against false alarms, and that you design for the wrong call (fencing,
> suspicion states) instead of pretending the detector is perfect.
>
> **Real-world example.** "Our 12-node dispatch cluster evicts a node a few times a day and it is
> always back within 3 seconds. What do you change?" A strong answer: check GC logs, switch from a
> 1.5 s fixed timeout to phi accrual with a pause allowance, add a suspicion step, and fence writes.

### Conceptual questions

**Q1. Why can't a failure detector be perfect?**
*Sections: §1, §6.*
In an asynchronous network there is no upper bound on message delay or process pause. A crashed
node and a node that is merely very slow (GC, congestion, partition) produce the same observation:
silence. Any finite timeout will sometimes convict a slow node (false positive); an infinite one
never detects crashes. FLP shows consensus is impossible deterministically in this model;
Chandra–Toueg show that an *unreliable* detector that is eventually accurate (◇W / ◇S) is enough
for consensus with a correct majority.

**Q2. Explain the phi accrual detector and what φ = 8 means.**
*Sections: §4.*
Keep a window of recent heartbeat gaps; fit a distribution (normal in the paper, exponential in
Cassandra). With `Δt` the silence since the last heartbeat, compute `P_later(Δt)`, the chance a
live node would be this late, and output `φ = −log10(P_later)`. φ = 1, 2, 3 mean 10%, 1%, 0.1%;
φ = 8 means 10⁻⁸ under the model. The application picks the threshold. Real delays are
heavier-tailed than the model, so the true false-suspicion rate is higher.

**Q3. Why is a threshold on φ better than a fixed timeout?**
*Sections: §3, §4.*
It adapts to each peer and each network: a peer with jittery gaps (σ = 200 ms) automatically gets
a longer effective timeout than one with steady gaps. It also separates mechanism from policy: a
load balancer can act at φ = 3, a leader election at φ = 10, both from the same detector.

**Q4. Why do Akka and Cassandra modify the textbook formula?**
*Sections: §3, §4.*
With a normal model and a steady network, σ shrinks to a few ms and a 50 ms hiccup produces a
huge φ. Akka adds `acceptable-heartbeat-pause` (3 s) to the mean and floors σ at 100 ms. Cassandra
uses an exponential model (`φ ≈ 0.434 × Δt/μ`), which grows linearly and tolerates long gaps, and
drops overly long intervals from the window.

**Q5. How does SWIM differ from all-to-all heartbeating?**
*Sections: §2, §5.*
All-to-all costs O(N²) messages per interval (500 nodes ≈ 249,500). SWIM has each node ping one
random peer per period, with `k` indirect pings through helpers on failure: O(1) expected load per
node. First detection takes about e/(e−1) ≈ 1.58 periods on average regardless of N; gossip then
spreads it in O(log N) periods. Suspicion plus incarnation numbers let a live node refute.

**Q6. What is a gray failure and why do heartbeats miss it?**
*Sections: §7, §8.*
The node is partly broken: heartbeat thread fine, request path broken (deadlocked pool, bad disk,
5% packet loss). Heartbeats only prove the heartbeat thread runs. You need application-level
health checks, outlier detection on real request errors/latency, and client-side signals.

**Q7. A node was declared dead but was only paused. What stops it from corrupting data?**
*Sections: §8.*
Fencing. Every new owner gets a higher epoch/token; storage rejects writes with a lower one.
Leases add self-fencing: the old owner stops acting when its lease expires, assuming bounded clock
drift.

### System design prompts

**Prompt A. Design membership and failure detection for a 2,000-node cache cluster across 3 availability zones.**

1. *Requirements:* detect crashes within ~5 s, false evictions below about one per day cluster-wide,
   no O(N²) traffic.
2. *Dissemination:* SWIM-style probing (1 s period, 3 indirect helpers) plus piggybacked gossip;
   membership changes reach all nodes in O(log N) periods.
3. *Suspicion:* suspect state with a timeout that scales with log N and shrinks as independent
   suspicions arrive (Lifeguard); incarnation numbers for refutation.
4. *Local health:* a node that is itself slow widens its own timeouts (Lifeguard LHM) so an
   overloaded node does not accuse everyone else.
5. *Cross-zone:* a longer probe timeout for cross-zone targets; do not evict a whole zone at once —
   cap evictions per minute (for example, at most 5% of nodes) and alert a human beyond that.
6. *Action:* on confirmed failure, reassign key ranges; the new owner gets a new epoch.
7. *Gray failures:* clients report per-node error rates; outlier ejection removes nodes with 5x the
   median error rate even if they answer pings.

*What interviewers listen for:* message complexity; the suspect/confirm split; a cap on mass
eviction; fencing; recognizing that the detector's own node can be the sick one.

**Prompt B. Choose leader failure detection for a Raft-based config store serving 50,000 clients.**

1. Heartbeat 100 ms, election timeout randomized in [1000, 2000) ms (10x–20x), longer across regions.
2. Measure p99.9 RTT and GC/fsync pauses first; the election timeout must be well above them.
3. Pre-vote and check-quorum so a partitioned node cannot disrupt the cluster, and a leader that
   loses its majority steps down.
4. Client sessions/leases use a separate, longer timeout (for example 10 s) than leader election,
   because losing a client session deletes its locks.
5. Fencing tokens on every lock handed to clients.

*What interviewers listen for:* separate timeouts for separate decisions; randomization against
split votes; quorum instead of a single observer; no correctness dependence on timing.

### Rapid-fire

| Question | Strong answer | Section |
|---|---|---|
| Formula for φ? | `φ = −log10(P_later(t_now − t_last))`, `P_later = 1 − F` | §4 |
| φ = 1 / 2 / 3 mean? | 10% / 1% / 0.1% chance a live node would be this late (under the model) | §4 |
| Cassandra default `phi_convict_threshold`? | 8; often raised to 10–12 on cloud | §3, §4 |
| Cassandra's model? | Exponential: `φ = Δt / (μ ln 10)`; φ = 8 ↔ ≈ 18.4 μ of silence | §4 |
| Akka defaults? | threshold 8, window 1000, min σ 100 ms, pause 3 s, heartbeat 1 s | §4 |
| Normal model, μ = 1000 ms, σ = 50 ms, Δt = 1200 ms? | z = 4 → P_later ≈ 3.2×10⁻⁵ → φ ≈ 4.5 | §4 |
| Why use a monotonic clock? | NTP steps make wall-clock gaps jump, causing false positives | §7 |
| TCP RTO formula? | `SRTT + max(G, 4·RTTVAR)`, α = 1/8, β = 1/4 | §3 |
| SWIM message load per node? | O(1) expected per period | §5 |
| How does a SWIM node refute suspicion? | Gossip `alive` with a higher incarnation number | §5 |
| Weakest detector for consensus? | ◇W (equivalent to ◇S), with a correct majority | §6 |
| Kubernetes default before pod eviction? | node grace period, then 300 s taint toleration | §5 |

### Debugging prompts

**D1.** *Symptoms:* nodes flap DOWN/UP every few hours, each episode lasts 2–6 s, always the same
JVM service, CPU and network look normal. *Diagnosis:* stop-the-world GC pauses longer than the
detector tolerates. Check GC logs for pauses that line up with the DOWN events. *Fix:* reduce
pause times (heap sizing, ZGC/Shenandoah), and add a pause allowance or raise the threshold.

**D2.** *Symptoms:* a new custom detector works for weeks on a quiet network, then after a deploy
every tiny hiccup causes evictions; logs show σ ≈ 2 ms. *Diagnosis:* σ collapsed on a very steady
network, so a 60 ms delay is z = 30. *Fix:* floor σ (100 ms) and floor the timeout.

**D3.** *Symptoms:* at exactly 03:00 all nodes suspect all others at once, then recover. *Diagnosis:*
wall-clock step from NTP; intervals were measured with `System.currentTimeMillis()`. *Fix:*
monotonic clock.

**D4.** *Symptoms:* 10% of checkout requests time out, but the membership view shows all nodes
healthy. *Diagnosis:* gray failure on one of 10 nodes (heartbeat thread fine, worker pool
deadlocked). *Fix:* readiness checks that exercise the real path; outlier ejection on request errors.

**D5.** *Symptoms:* one overloaded node reports many *other* nodes as suspect. *Diagnosis:* the
accuser is the sick one: it processes acks too slowly. *Fix:* Lifeguard-style local health
multiplier; require corroboration before conviction.

### Common mistakes

- Treating φ = 8 as a literal "one in 100 million" guarantee; real delays have heavy tails.
- Saying the paper's normal model is what Cassandra uses (Cassandra uses an exponential model).
- One timeout for everything: leader election, client sessions, and load-balancer ejection need
  different thresholds.
- Forgetting that the detector's *action* (rebalancing) adds load and can cause more false positives.
- Measuring gaps with wall-clock time.
- No fencing: assuming a node declared dead has actually stopped.
- Relying only on heartbeats and missing gray failures.

---

## 10. Real-world cases — incidents with numbers

These are **composite scenarios** built from failure modes this chapter describes; numbers are
illustrative but internally consistent. Case 10.5 is based on a public postmortem.

**Quick index:** flapping nodes after GC → 10.1 · evictions from tiny hiccups → 10.2 · everyone
suspects everyone at one instant → 10.3 · errors but "all nodes healthy" → 10.4 · short partition
causes a long outage → 10.5 · Kubernetes node NotReady under CPU load → 10.6

### 10.1 Chat presence service: fixed timeout versus GC pauses

- **Setup.** A chat app's presence service runs on 40 JVM nodes holding 2 million WebSocket
  connections (50,000 per node). Heartbeat 1 s, fixed timeout 3 s.
- **Symptom.** About 120 evictions per day, each followed by the node rejoining within 10 s. Every
  eviction forces 50,000 clients to reconnect, and users see "offline" flicker.
- **Measurement/Diagnosis.** GC logs show 4–8 s full-GC pauses, about 3 per node per day
  (40 × 3 = 120), matching the evictions one to one. No crash was found in any of them.
- **Fix.** Heap tuning plus a low-pause collector brought the worst pause below 500 ms. The detector
  moved to phi accrual (threshold 8, 3 s pause allowance, 100 ms σ floor), so a real crash is
  convicted after about 4.5 s of silence instead of 3 s. False evictions fell from about 120 per
  day to about 1 per week.
- **Lesson.** Look at what the detector is catching before tuning it. A 1.5 s slower detection of
  real crashes bought a large drop in self-inflicted outages.

### 10.2 IoT telemetry: the detector that learned to be too strict

- **Setup.** A telemetry ingest cluster of 16 nodes on a dedicated, very quiet network. A homegrown
  phi detector with the normal model and no σ floor.
- **Symptom.** After weeks of calm, nodes start getting evicted by routine 50–100 ms hiccups
  (a log rotation, a kernel scheduling delay).
- **Measurement/Diagnosis.** The window shows μ = 1000 ms, σ = 2 ms. A 1060 ms gap is
  z = 30 → φ ≈ 197, far over the threshold of 8.
- **Fix.** Floor σ at 100 ms: the same 1060 ms gap is now z = 0.6 → φ ≈ 0.56. Also add
  `T_min` = 2 s. Evictions from hiccups dropped to zero.
- **Lesson.** Adaptive detectors can adapt themselves into hypersensitivity. Always floor σ and the
  timeout.

### 10.3 Payments cluster: the 03:00 clock step

- **Setup.** A payments service with 30 nodes measures heartbeat gaps with the wall clock.
  μ = 1000 ms, σ = 100 ms.
- **Symptom.** At 03:00 the time daemon steps the clock forward by 2 s. Within one second, every
  node suspects most peers; 8 nodes are evicted and payment authorizations fail for 40 s while
  ownership moves around.
- **Measurement/Diagnosis.** A heartbeat received 0.5 s earlier now looks 2.5 s old: z = 15 →
  φ ≈ 50. The eviction times match the clock-step log line.
- **Fix.** Measure intervals with a monotonic clock (`System.nanoTime()` / `CLOCK_MONOTONIC`) and
  configure the time daemon to slew instead of step. Zero repeat events in the following quarter.
- **Lesson.** Durations need monotonic time; wall-clock time is for showing dates to people.

### 10.4 E-commerce checkout: alive for heartbeats, dead for customers

- **Setup.** Checkout runs on 10 nodes behind a load balancer; SWIM-based membership; the load
  balancer's health check is a TCP connect.
- **Symptom.** 10% of checkouts time out after 5 s. Membership shows 10/10 healthy.
- **Measurement/Diagnosis.** One node's worker pool (200 threads) is deadlocked on a lock; its
  heartbeat and accept threads are fine. 100% of its requests time out, and it receives 1/10 of
  traffic.
- **Fix.** A `/readyz` check that runs a real lightweight checkout path every 5 s (fail after
  2 misses) and outlier ejection when a node's error rate is 5x the median. The bad node is removed
  within about 10 s; the error rate drops from 10% to 0.1% (normal baseline).
- **Lesson.** Heartbeats prove that the heartbeat thread is alive, nothing more. Check the path
  customers use.

### 10.5 Public postmortem: GitHub, October 21, 2018

- **Setup.** GitHub ran MySQL clusters managed by Orchestrator, which detects failed primaries and
  promotes replicas, across East Coast and West Coast data centers.
- **Symptom.** Network connectivity between the US East Coast network hub and the primary East
  Coast data center was lost for **43 seconds**. Orchestrator promoted West Coast replicas to
  primary during that window.
- **Measurement/Diagnosis.** When connectivity returned, both sides had accepted writes the other
  did not have, and application traffic now went across the country to the new primaries. GitHub
  ran in a degraded state for **24 hours and 11 minutes** while it restored and reconciled data.
- **Fix.** Among the follow-ups GitHub described: changing the Orchestrator configuration so
  primaries are not promoted across regional boundaries.
- **Lesson.** The detector was "right" that the primary was unreachable, but a 43 s partition
  triggered an action whose cost was a full day. Match the failover action and its blast radius to
  the detector's confidence; make cross-region promotion a deliberate decision.

### 10.6 Video platform: Kubernetes node NotReady under CPU starvation

- **Setup.** A video transcoding cluster on Kubernetes. The kubelet updates node status every 10 s;
  the node is marked NotReady after a 40 s grace period; pods are evicted after a further 300 s.
  No CPU is reserved for system daemons.
- **Symptom.** During upload peaks, nodes flip to NotReady; some stay long enough that 30
  transcoding pods are evicted and restarted, losing up to 5 minutes of work each.
- **Measurement/Diagnosis.** Transcoder pods use 100% of node CPU; the kubelet misses status
  updates for 45–60 s (more than 4 intervals), crossing the 40 s grace period. The node never
  crashed.
- **Fix.** Reserve CPU for system daemons (`system-reserved`/`kube-reserved`, e.g. 1 core per
  node) and set CPU limits on transcoder pods. Longest kubelet status gap fell from 60 s to under
  12 s; NotReady events went from about 20 per week to 0.
- **Lesson.** A starving heartbeat sender is the classic false positive. Give the component that
  proves liveness its own guaranteed resources.

---

## References

1. Hayashibara, N., Defago, X., Yared, R., & Katayama, T. (2004). *The φ Accrual Failure Detector.* IEEE Symposium on Reliable Distributed Systems.
2. Chandra, T. D., & Toueg, S. (1996). *Unreliable Failure Detectors for Reliable Distributed Systems.* Journal of the ACM, 43(2), 225-267.
3. Fischer, M. J., Lynch, N. A., & Paterson, M. S. (1985). *Impossibility of Distributed Consensus with One Faulty Process.* Journal of the ACM, 32(2), 374-382.
4. Das, A., Gupta, I., & Motivala, A. (2002). *SWIM: Scalable Weakly-consistent Infection-style Process Group Membership Protocol.* IEEE DSN.
5. Dadgar, A., Phillips, J., & Currey, J. (2018). *Lifeguard: Local Health Awareness for More Accurate Failure Detection.* HashiCorp (IEEE DSN Workshops).
6. Huang, P., Guo, C., Zhou, L., Lorch, J. R., Dang, Y., Chintalapati, M., & Yao, R. (2017). *Gray Failure: The Achilles' Heel of Cloud-Scale Systems.* HotOS.
7. Jacobson, V. (1988). *Congestion Avoidance and Control.* ACM SIGCOMM.
8. Kleppmann, M. (2017). *Designing Data-Intensive Applications.* O'Reilly Media. Chapter 8: The Trouble with Distributed Systems.
9. Chandra, T. D., Hadzilacos, V., & Toueg, S. (1996). *The Weakest Failure Detector for Solving Consensus.* Journal of the ACM, 43(4), 685-722.
10. Paxson, V., Allman, M., Chu, J., & Sargent, M. (2011). *RFC 6298: Computing TCP's Retransmission Timer.* IETF.
