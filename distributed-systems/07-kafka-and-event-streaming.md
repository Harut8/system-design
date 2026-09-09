# Kafka and Event Streaming: A Complete Interview-Ready Deep Dive

A production-grade reference covering Apache Kafka's architecture, internals, and operational characteristics for senior engineers who need to confidently deploy Kafka in system design interview answers. Covers the distributed commit log model, producer and consumer internals, partition key design (the single most important Kafka decision), exactly-once semantics, stream processing, performance tuning, capacity planning, failure modes, and the concrete patterns where Kafka appears in real system design problems -- recommendation pipelines, feature stores, CDC, real-time search, and event sourcing.

Prerequisites: familiarity with distributed system fundamentals from `00-primitives-and-system-models.md` and replication models from `04-replication-and-consistency.md`.

---

## Table of Contents

1. [Mental Models -- Kafka Is Not a Message Queue](#1-mental-models--kafka-is-not-a-message-queue)
2. [Core Architecture](#2-core-architecture)
3. [Producers](#3-producers)
4. [Consumers](#4-consumers)
5. [Partition Key Design -- The Most Important Decision](#5-partition-key-design--the-most-important-decision)
6. [Exactly-Once Semantics](#6-exactly-once-semantics)
7. [Kafka Streams and ksqlDB](#7-kafka-streams-and-ksqldb)
8. [Kafka Connect](#8-kafka-connect)
9. [Performance Tuning and Internals](#9-performance-tuning-and-internals)
10. [Common Interview Patterns](#10-common-interview-patterns)
11. [Capacity Planning](#11-capacity-planning)
12. [Failure Modes and Operational Concerns](#12-failure-modes-and-operational-concerns)
13. [Kafka vs Alternatives -- Decision Matrix](#13-kafka-vs-alternatives--decision-matrix)

---

## 1. Mental Models -- Kafka Is Not a Message Queue

### 1.1 The Distributed Commit Log

The single most important mental model for Kafka: **it is a distributed, partitioned, replicated commit log**. Not a message queue. Not a pub/sub system. A commit log.

A traditional message queue (RabbitMQ, SQS) is built around the concept of a message being consumed and then disappearing. Once a consumer acknowledges a message, the broker deletes it. This is a destructive read. Kafka's model is fundamentally different: messages are appended to an immutable, ordered log and remain there until a configurable retention period expires (or forever, with compacted topics). Consumers read from the log by maintaining a position -- an **offset** -- and can rewind, skip ahead, or re-read data at will.

```
TRADITIONAL MESSAGE QUEUE vs KAFKA:

Message Queue (RabbitMQ/SQS):
  Producer --> [ msg3 | msg2 | msg1 ] --> Consumer
                                          (msg1 consumed, deleted from queue)
              [ msg3 | msg2 ]         --> Consumer
                                          (msg2 consumed, deleted from queue)

  - Messages are ephemeral
  - Once consumed, they are gone
  - One message goes to one consumer (competing consumers)

Kafka (Distributed Commit Log):
  Producer --> [ msg1 | msg2 | msg3 | msg4 | msg5 | ... ]
                  ^                     ^          ^
                  |                     |          |
              Consumer A            Consumer B   Consumer C
              (offset=0)            (offset=3)   (offset=4)

  - Messages are durable and immutable
  - Multiple consumers read independently at their own pace
  - Consumers can rewind and re-read
  - Data persists for the configured retention period
```

This distinction matters enormously in system design interviews. When an interviewer hears "we'll put Kafka here," they expect you to understand that Kafka gives you:

- **Replay capability**: A new consumer can start from the beginning and process all historical data. This is why Kafka is the backbone of event sourcing and feature pipelines.
- **Multiple independent consumers**: Five different services can each read every event independently, at their own pace, without affecting each other. This is fan-out without the broker duplicating messages.
- **Ordering guarantees within a partition**: Messages with the same key go to the same partition and are read in order. This is how you get causal ordering for a user's events.
- **Backpressure tolerance**: A slow consumer does not block producers or other consumers. It simply falls behind, and its lag is a measurable, monitorable quantity.

### 1.2 The Key Insight: Append-Only, Immutable, Ordered

Kafka partitions have three properties that together explain most of its behavior:

1. **Append-only**: New messages are always written to the end. There is no random insertion, no update-in-place, no reordering.
2. **Immutable**: Once written, a message cannot be changed or deleted (until retention expires or compaction removes older versions of the same key).
3. **Ordered within a partition**: Messages in a single partition have a strict total order, defined by their offset (a monotonically increasing 64-bit integer).

These three properties are what make Kafka fast (sequential I/O), scalable (partitions are independent), and useful for event-driven architectures (consumers see events in the order they happened, per partition).

### 1.3 When to Reach for Kafka in an Interview

Kafka is the right answer when you need one or more of: durable event storage with replay, high-throughput ordered event delivery, decoupling producers and consumers at different speeds, fan-out to multiple independent consumer groups, or a real-time data pipeline between systems. It is the wrong answer when you need: low-latency request-reply (use gRPC), simple task distribution to workers (use SQS or RabbitMQ), small-scale pub/sub (use Redis Pub/Sub), or when your system has fewer than a few thousand events per second and durability does not matter.

---

## 2. Core Architecture

### 2.1 Brokers, Topics, Partitions

A Kafka **cluster** is a set of servers called **brokers**. Each broker holds some subset of the data. The unit of data organization is the **topic** -- a named feed of messages (think: "user-clicks", "order-events", "page-views"). Each topic is divided into one or more **partitions**, and each partition is an ordered, immutable sequence of records.

```
KAFKA CLUSTER TOPOLOGY:

Cluster (3 brokers, topic "user-events" with 6 partitions, RF=3)

  Broker 0                Broker 1                Broker 2
  ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
  │ P0 (leader)      │   │ P0 (follower)    │   │ P0 (follower)    │
  │ P1 (follower)    │   │ P1 (leader)      │   │ P1 (follower)    │
  │ P2 (follower)    │   │ P2 (follower)    │   │ P2 (leader)      │
  │ P3 (leader)      │   │ P3 (follower)    │   │ P3 (follower)    │
  │ P4 (follower)    │   │ P4 (leader)      │   │ P4 (follower)    │
  │ P5 (follower)    │   │ P5 (follower)    │   │ P5 (leader)      │
  └──────────────────┘   └──────────────────┘   └──────────────────┘

  Each partition has exactly one leader and (RF-1) followers.
  All reads and writes go to the leader.
  Followers replicate the leader's log for fault tolerance.
```

Key rules:
- A partition lives on exactly one broker (as leader) and is replicated to (replication factor - 1) other brokers as followers.
- All client reads and writes go to the partition leader. (Kafka 2.4+ allows follower reads for latency-sensitive geo-distributed consumers, but this is not the default.)
- The **replication factor** (RF) determines how many copies of each partition exist. RF=3 means the cluster survives two broker failures for any partition.

### 2.2 Physical Layout: Segments, Logs, and Indexes

On disk, each partition is stored as a directory containing a series of **segments**. A segment is a chunk of the partition's log, and it consists of three files:

```
PHYSICAL LAYOUT ON A BROKER:

/data/kafka-logs/
└── user-events-0/              # Topic "user-events", partition 0
    ├── 00000000000000000000.log        # Segment file (messages from offset 0)
    ├── 00000000000000000000.index      # Offset index (offset → file position)
    ├── 00000000000000000000.timeindex  # Time index (timestamp → offset)
    ├── 00000000000052428800.log        # Next segment (from offset 52428800)
    ├── 00000000000052428800.index
    ├── 00000000000052428800.timeindex
    ├── 00000000000104857600.log        # And so on...
    ├── 00000000000104857600.index
    ├── 00000000000104857600.timeindex
    └── leader-epoch-checkpoint
```

- **`.log` file**: Contains the actual message data, written sequentially. This is the append-only log. Each message is stored with its offset, timestamp, key, value, and headers.
- **`.index` file**: A sparse index mapping offsets to physical byte positions in the `.log` file. Kafka does not index every message -- it indexes every N bytes (configurable via `log.index.interval.bytes`, default 4096). To find offset X, Kafka binary-searches the `.index` to find the nearest entry before X, then scans forward in the `.log`.
- **`.timeindex` file**: Maps timestamps to offsets, enabling time-based lookups ("give me all messages from the last hour").

Segments roll over when they exceed `log.segment.bytes` (default 1 GB) or when the oldest message in the segment exceeds `log.roll.ms`. Retention (`log.retention.hours`, `log.retention.bytes`) is applied at the segment level -- Kafka deletes entire segments, never individual messages within a segment.

### 2.3 ZooKeeper vs KRaft

Historically, Kafka depended on **ZooKeeper** for cluster metadata management: broker registration, controller election, topic configuration, partition leadership, and ACLs. ZooKeeper was Kafka's single external dependency and, for many operators, its most painful operational burden.

Starting with Kafka 3.3 (production-ready) and fully replacing ZooKeeper in Kafka 4.0, **KRaft** (Kafka Raft) moves all metadata management into Kafka itself. A subset of brokers are designated as **controllers**, and they use a Raft-based consensus protocol to maintain a metadata log.

```
ZOOKEEPER MODE vs KRAFT MODE:

ZooKeeper mode (pre-4.0):
  ┌─────────────┐
  │  ZooKeeper   │  <-- Separate cluster (3-5 nodes)
  │  Ensemble    │      Stores: broker list, topic config,
  └──────┬──────┘      partition leaders, ACLs, consumer offsets (legacy)
         │
  ┌──────┴──────┐
  │ Kafka Brokers│  <-- One broker elected as "controller" via ZK
  │ (N nodes)    │      Controller manages partition reassignment
  └─────────────┘

KRaft mode (3.3+):
  ┌──────────────────────────────────────────┐
  │               Kafka Cluster               │
  │                                           │
  │  Controller nodes (3 or 5):               │
  │    Raft-based consensus for metadata      │
  │    One active controller, rest are voters  │
  │                                           │
  │  Broker nodes (N):                        │
  │    Fetch metadata from controllers        │
  │    Handle produce/consume requests        │
  │                                           │
  │  (A node can be both controller + broker   │
  │   in smaller clusters)                    │
  └──────────────────────────────────────────┘

Benefits of KRaft:
  - No external dependency (simpler operations)
  - Faster controller failover (seconds, not minutes)
  - Better scalability (millions of partitions vs ZK's ~200K limit)
  - Single security model (no ZK ACLs to manage separately)
```

**Interview tip**: If you mention Kafka in a design, you can note "using KRaft mode, so no ZooKeeper dependency." This signals awareness of modern Kafka and removes a common follow-up about operational complexity.

---

## 3. Producers

### 3.1 Producer Architecture

A Kafka producer serializes a record (key + value + headers), determines the target partition, batches records destined for the same partition, optionally compresses the batch, and sends it to the partition leader.

```
PRODUCER INTERNALS:

  Application Thread                  Sender Thread (background)
  ┌─────────────────┐                ┌─────────────────────────┐
  │ producer.send()  │                │                         │
  │   │              │                │  Drain batches per node │
  │   ├─ serialize   │                │   │                     │
  │   ├─ partition   │  ──batches──>  │   ├─ compress           │
  │   └─ append to   │  (per-partition│   ├─ create request     │
  │      accumulator │   batches)     │   └─ send to broker     │
  └─────────────────┘                │                         │
                                      │  Handle responses:      │
                                      │   ├─ success → callback │
                                      │   └─ failure → retry    │
                                      └─────────────────────────┘

  Key configs:
    batch.size       = 16384 (bytes per batch, default 16 KB)
    linger.ms        = 0     (how long to wait for more records)
    buffer.memory    = 33554432 (total memory for unsent batches)
    max.in.flight.requests.per.connection = 5
```

### 3.2 Partitioning Strategies

When producing a message, Kafka must decide which partition receives it. The strategy depends on whether a key is provided:

**Key-based hashing (default when key is present)**: `partition = murmur2(key) % num_partitions`. All messages with the same key go to the same partition, guaranteeing ordering for that key. This is the most common and most important strategy.

**Round-robin (legacy default when key is null)**: Distributes messages evenly across partitions. No ordering guarantee. In older Kafka versions, null-key messages were round-robined per message.

**Sticky partitioning (default since Kafka 2.4 for null keys)**: Instead of round-robining each message, the producer "sticks" to one partition for the duration of a batch, then switches. This dramatically improves batching efficiency for null-key messages because an entire batch goes to one partition instead of being scattered.

**Custom partitioner**: You implement the `Partitioner` interface for domain-specific routing.

```python
# Python producer with explicit key-based partitioning
from confluent_kafka import Producer

conf = {
    'bootstrap.servers': 'kafka-1:9092,kafka-2:9092,kafka-3:9092',
    'acks': 'all',
    'enable.idempotence': True,
    'linger.ms': 10,
    'batch.size': 65536,
    'compression.type': 'lz4',
}

producer = Producer(conf)

def delivery_callback(err, msg):
    if err:
        print(f'Delivery failed: {err}')
    else:
        print(f'Delivered to {msg.topic()}[{msg.partition()}] @ offset {msg.offset()}')

# Key-based: all events for user_123 go to the same partition, in order
producer.produce(
    topic='user-events',
    key='user_123',           # Determines partition via murmur2 hash
    value='{"action": "click", "item": "product_456"}',
    callback=delivery_callback
)

producer.flush()              # Block until all messages are delivered
```

### 3.3 Batching: The linger.ms and batch.size Tradeoff

Kafka producers batch records destined for the same partition to amortize network overhead. Two parameters control batching:

- **`batch.size`** (bytes): Maximum size of a batch. When the accumulated records for a partition reach this size, the batch is sent immediately.
- **`linger.ms`** (milliseconds): How long to wait for additional records before sending a non-full batch. Default is 0, meaning "send immediately."

```
BATCHING TRADEOFF:

  linger.ms=0, batch.size=16384 (defaults):
    Record 1 arrives → sent immediately (tiny batch, high overhead)
    Record 2 arrives → sent immediately
    Record 3 arrives → sent immediately
    Network: 3 requests, 3 round-trips, poor compression ratio

  linger.ms=20, batch.size=65536:
    Record 1 arrives → wait...
    Record 2 arrives → wait... (batching)
    Record 3 arrives → wait... (batching)
    ... (20ms elapses)
    → All 3 records sent in one batch
    Network: 1 request, 1 round-trip, good compression ratio

  GENERAL GUIDANCE:
    Low latency required:  linger.ms=0-5,   batch.size=16384
    Balanced:              linger.ms=10-20,  batch.size=65536
    High throughput:       linger.ms=50-100, batch.size=131072-262144
```

The tradeoff is latency vs throughput. Higher `linger.ms` adds latency to each message (up to `linger.ms` milliseconds) but allows more messages to accumulate into batches, improving throughput and compression ratio.

### 3.4 Compression

Kafka supports four compression codecs, applied at the batch level:

| Codec | CPU Cost | Compression Ratio | Speed | Best For |
|-------|----------|-------------------|-------|----------|
| none | 0 | 1:1 | N/A | Low-CPU environments |
| snappy | Low | ~1.5-2x | Fast | General purpose, default choice |
| lz4 | Low | ~2-3x | Very fast | High throughput, recommended |
| zstd | Medium | ~3-5x | Medium | Best ratio, bandwidth-constrained |

**Interview recommendation**: "We'd use lz4 compression -- it gives 2-3x compression with minimal CPU overhead, which reduces network bandwidth and disk usage at the cost of negligible latency." This is the right default for most system design scenarios.

### 3.5 Acknowledgments (acks) and Durability

The `acks` setting controls how many replicas must acknowledge a write before the producer considers it successful:

```
ACKS MODES:

acks=0 (fire and forget):
  Producer ──send──> Leader broker
  Producer does NOT wait for any acknowledgment.
  Maximum throughput, risk of data loss.
  Use case: metrics, logs where some loss is acceptable.

acks=1 (leader acknowledgment):
  Producer ──send──> Leader broker
  Leader writes to its local log.
  Leader ──ack──> Producer
  If leader crashes BEFORE followers replicate, data is LOST.
  Use case: moderate durability, good throughput.

acks=all (a.k.a. acks=-1, full ISR acknowledgment):
  Producer ──send──> Leader broker
  Leader writes to its local log.
  Leader waits for ALL in-sync replicas (ISR) to replicate.
  Leader ──ack──> Producer
  Data survives any single broker failure (with RF >= 3, min.insync.replicas=2).
  Use case: financial transactions, event sourcing, anything you cannot lose.

  CRITICAL: acks=all with min.insync.replicas=1 is equivalent to acks=1.
  Always pair acks=all with min.insync.replicas=2 (and RF=3).
```

### 3.6 Idempotent Producers

Without idempotence, a network failure after the broker writes but before the ack reaches the producer causes the producer to retry, creating a **duplicate** message. Idempotent producers solve this.

When `enable.idempotence=true`, the producer is assigned a **Producer ID (PID)** and attaches a monotonically increasing **sequence number** to each message per partition. The broker tracks the latest sequence number for each PID-partition pair and rejects duplicates.

```
IDEMPOTENT PRODUCER DEDUP:

  Producer (PID=5)                         Broker (Partition 3)
  ─────────────────                        ────────────────────
  send(seq=0)  ──────────────────────────> receives seq=0, writes, acks
  send(seq=1)  ──────────────────────────> receives seq=1, writes to log
               <─── ack LOST (network) ──  ack sent but not delivered
  retry(seq=1) ──────────────────────────> sees seq=1 already written
               <─── ack (dedup) ──────────  returns success without writing again

  Result: exactly one copy of seq=1 in the log.
```

Enabling idempotence requires `max.in.flight.requests.per.connection <= 5` (default is 5, so no change needed) and `acks=all`. Since Kafka 3.0, idempotence is enabled by default.

---

## 4. Consumers

### 4.1 Consumer Groups and Partition Assignment

Consumers read from topics by joining a **consumer group**. Within a group, each partition is assigned to exactly one consumer. This is how Kafka achieves parallel consumption while maintaining per-partition ordering.

```
CONSUMER GROUP MECHANICS:

Topic "user-events" with 6 partitions, Consumer Group "analytics-pipeline":

  Scenario A: 3 consumers (balanced)
  ┌──────────────────────────────────────┐
  │  Consumer 0: reads P0, P1            │
  │  Consumer 1: reads P2, P3            │
  │  Consumer 2: reads P4, P5            │
  └──────────────────────────────────────┘

  Scenario B: 6 consumers (maximum parallelism)
  ┌──────────────────────────────────────┐
  │  Consumer 0: reads P0                │
  │  Consumer 1: reads P1                │
  │  Consumer 2: reads P2                │
  │  Consumer 3: reads P3                │
  │  Consumer 4: reads P4                │
  │  Consumer 5: reads P5                │
  └──────────────────────────────────────┘

  Scenario C: 8 consumers (2 idle -- waste!)
  ┌──────────────────────────────────────┐
  │  Consumer 0: reads P0                │
  │  Consumer 1: reads P1                │
  │  Consumer 2: reads P2                │
  │  Consumer 3: reads P3                │
  │  Consumer 4: reads P4                │
  │  Consumer 5: reads P5                │
  │  Consumer 6: IDLE (no partition)     │
  │  Consumer 7: IDLE (no partition)     │
  └──────────────────────────────────────┘

  RULE: Max useful consumers in a group = number of partitions.
  More consumers than partitions means wasted instances.

  Multiple consumer groups are INDEPENDENT:
  ┌──────────────────────────────────────┐
  │  Group "analytics":  reads ALL msgs  │
  │  Group "search":     reads ALL msgs  │
  │  Group "billing":    reads ALL msgs  │
  └──────────────────────────────────────┘
  Each group maintains its own offsets.
```

### 4.2 Partition Assignment Strategies

When consumers join or leave a group, a **rebalance** occurs and partitions are reassigned. The assignment strategy determines the algorithm:

**Range Assignor** (default): Assigns partitions to consumers in order. Consumer 0 gets the first N/M partitions, consumer 1 gets the next N/M, etc. Can cause slight imbalance if partitions don't divide evenly.

**Round-Robin Assignor**: Distributes partitions round-robin across consumers. More balanced than range, but does not consider which consumer previously owned a partition.

**Sticky Assignor**: Like round-robin, but tries to preserve previous assignments during rebalance. This minimizes partition movement, reducing the cost of rebalancing (state that a consumer built for a partition does not need to be rebuilt).

**Cooperative Sticky Assignor** (recommended): Like sticky, but uses **incremental cooperative rebalancing** -- only the partitions that need to move are revoked, while all other partitions continue being consumed. This is a major operational improvement over eager rebalancing.

### 4.3 Offset Management

Each consumer tracks its position in each partition via an **offset** -- the index of the next message to read. Offsets are stored in an internal Kafka topic called `__consumer_offsets`.

```python
# Manual offset commit for exactly-once processing
from confluent_kafka import Consumer, TopicPartition

conf = {
    'bootstrap.servers': 'kafka-1:9092,kafka-2:9092,kafka-3:9092',
    'group.id': 'order-processing-group',
    'auto.offset.reset': 'earliest',    # Start from beginning if no committed offset
    'enable.auto.commit': False,         # MANUAL commit -- critical for reliability
}

consumer = Consumer(conf)
consumer.subscribe(['order-events'])

try:
    while True:
        msg = consumer.poll(timeout=1.0)
        if msg is None:
            continue
        if msg.error():
            handle_error(msg.error())
            continue

        # Process the message
        order = deserialize(msg.value())
        result = process_order(order)

        if result.success:
            # Commit AFTER successful processing (at-least-once guarantee)
            consumer.commit(message=msg, asynchronous=False)
        else:
            # Do NOT commit -- message will be redelivered on next poll
            log_processing_failure(order, result.error)

finally:
    consumer.close()   # Triggers final offset commit and clean group leave
```

**Auto commit vs manual commit**:

| Mode | Config | Guarantee | Risk |
|------|--------|-----------|------|
| Auto commit | `enable.auto.commit=true` | At-most-once (can lose messages) | Offset committed before processing finishes. If consumer crashes mid-processing, message is lost. |
| Manual commit (sync) | `enable.auto.commit=false`, call `commit()` after processing | At-least-once (can duplicate) | If consumer crashes after processing but before commit, message is reprocessed on restart. |
| Manual commit + idempotent processing | Same + dedup in your application | Effectively exactly-once | Requires application-level idempotency |

**Interview default**: Always say "manual commit with at-least-once delivery and idempotent consumers." Auto commit is almost never acceptable for any system where correctness matters.

### 4.4 Rebalancing: Eager vs Cooperative

When a consumer joins, leaves, or crashes, the group must rebalance -- reassign partitions to the remaining consumers. This is one of Kafka's most operationally painful aspects.

```
EAGER REBALANCE (legacy, pre-2.4):

  Before rebalance:
    C0: [P0, P1]    C1: [P2, P3]    C2: [P4, P5]

  C2 crashes. Rebalance triggered:

  Step 1: ALL consumers REVOKE ALL partitions.
          C0: []              C1: []              C2: (dead)
          *** ALL CONSUMPTION STOPS ***

  Step 2: Group coordinator reassigns:
          C0: [P0, P1, P4]   C1: [P2, P3, P5]

  Problem: Even C0's P0 and P1, which didn't move, were
  revoked and re-assigned. Total pause: 5-30 seconds.
  During this window, ZERO messages are consumed.

COOPERATIVE (INCREMENTAL) REBALANCE (2.4+):

  Before rebalance:
    C0: [P0, P1]    C1: [P2, P3]    C2: [P4, P5]

  C2 crashes. Rebalance triggered:

  Step 1: Only P4 and P5 (from dead C2) need reassignment.
          C0: [P0, P1] (continues consuming!)
          C1: [P2, P3] (continues consuming!)

  Step 2: P4 and P5 assigned to C0 and C1:
          C0: [P0, P1, P4]   C1: [P2, P3, P5]

  Benefit: C0 and C1 never stopped consuming their
  existing partitions. Only the orphaned partitions paused.
```

**Always use cooperative rebalancing** (`partition.assignment.strategy=org.apache.kafka.clients.consumer.CooperativeStickyAssignor`). The eager rebalance protocol causes unnecessary consumption pauses, especially in large consumer groups.

### 4.5 Consumer Lag

**Consumer lag** is the difference between the latest offset (log-end offset) and the consumer's committed offset. It is the single most important Kafka operational metric.

```
CONSUMER LAG:

  Partition log:
  [ msg0 | msg1 | msg2 | msg3 | msg4 | msg5 | msg6 | msg7 | msg8 ]
                                  ^                            ^
                                  |                            |
                          committed offset (4)           log-end offset (8)

  Consumer lag = 8 - 4 = 4 messages

  Lag is healthy at a small, stable number (consumer is slightly behind).
  Lag that grows continuously means consumption rate < production rate.
  This WILL lead to data loss when lag exceeds the retention window.
```

Monitoring consumer lag (see Section 12 for PromQL queries) is non-negotiable. If lag exceeds `retention.ms` worth of data, the oldest unconsumed messages are deleted before the consumer reaches them -- **silent data loss**.

---

## 5. Partition Key Design -- The Most Important Decision

### 5.1 Why This Is the Most Important Kafka Decision

In a system design interview, the partition key is often the difference between a design that works and one that falls apart under load. The partition key determines:

1. **Which messages are ordered** relative to each other (same partition = ordered).
2. **Which consumer processes which messages** (one partition per consumer in a group).
3. **Whether load is balanced** across partitions (skewed keys = hot partitions).
4. **Maximum parallelism** for consumption (cannot have more consumers than partitions).

Get the partition key wrong, and you get hot partitions (one partition handling 80% of traffic while others are idle), ordering violations (events for the same entity processed out of order), or inadequate parallelism.

### 5.2 Key Design Principles

**Principle 1: Same key = same partition = guaranteed ordering.** If you need events for entity X processed in order, use X's identifier as the partition key.

**Principle 2: Key cardinality should be much higher than partition count.** If you have 64 partitions and 50 unique keys, some partitions will be empty. If you have 64 partitions and 10 million unique keys, the hash distributes well.

**Principle 3: Key frequency should be roughly uniform.** If 40% of your events have key="celebrity_user_123", that single partition handles 40% of the topic's throughput. This is a hot partition.

### 5.3 Concrete Examples

```
PARTITION KEY DESIGN EXAMPLES:

1. USER ACTIVITY STREAM (for a recommendation system)
   Key: user_id
   Why: All events for a user go to the same partition → in-order processing.
         A recommendation model needs to see click→view→purchase in order.
   Risk: Celebrity users generate disproportionate events.
   Mitigation: Salted key for very hot users (user_id + shard_suffix),
               accepting out-of-order for those users, or dedicated partition.

2. ORDER PROCESSING
   Key: order_id
   Why: All state transitions for an order (created→paid→shipped→delivered)
        must be processed in order by the same consumer.
   Risk: Low risk of hot partitions (orders are roughly equal in event volume).

3. IOT DEVICE TELEMETRY
   Key: device_id
   Why: Time-series data for a device should be in order.
   Risk: If some devices report 1000x more frequently, their partition is hot.
   Mitigation: Compound key (device_id + time_bucket) if exact ordering
               per-device is not required.

4. MULTI-TENANT SAAS EVENTS
   Key: tenant_id
   Why: Isolation -- all events for a tenant processed together.
   Risk: One large tenant dominates a partition.
   Mitigation: For large tenants, use (tenant_id + sub_shard) as key.
               Small tenants use tenant_id directly.

5. ITEM ENGAGEMENT AGGREGATION (e.g., counting likes)
   Key: item_id
   Why: All likes for an item go to the same partition, enabling
        accurate per-item counting without distributed aggregation.
   Risk: Viral items create hot partitions.
   Mitigation: Pre-aggregate in-memory with tumbling windows,
               or use compound key with periodic flush.

6. SEARCH INDEX UPDATES
   Key: document_id
   Why: If a document is updated three times, the consumer must see
        those updates in order to apply them correctly.
   Risk: Typically fine -- document update frequency is usually uniform.
```

### 5.4 Handling Hot Partitions

When a single key dominates throughput, you have several options:

1. **Salted keys**: Append a random suffix (e.g., `user_123_shard_0` through `user_123_shard_7`) to spread a hot key across multiple partitions. You lose strict ordering but gain throughput. The consumer must merge and re-sort if ordering matters.

2. **Compound keys**: Use `(entity_id, time_bucket)` or `(entity_id, sequence % N)` to distribute events for a single entity across partitions while preserving ordering within each sub-shard.

3. **Separate topic**: Route known hot entities to a dedicated topic with its own partitioning scheme and consumer group, sized for that entity's throughput.

4. **Accept the skew**: If the hot partition's throughput is within a single consumer's capacity, the skew is acceptable. Monitor it and plan to mitigate if the entity grows.

### 5.5 The Partition Count Trap

You **cannot reduce** the number of partitions in a topic without recreating it (and migrating all data and consumers). Adding partitions changes the key-to-partition mapping, breaking ordering guarantees for existing keys. Therefore:

- **Over-provision partitions** at topic creation. Start with more than you think you need.
- **Formula for minimum partitions**: `max(target_throughput / per_partition_throughput, target_consumer_parallelism)`. For example, if you need 100 MB/s throughput and each partition handles ~10 MB/s, you need at least 10 partitions.
- **Common defaults**: 6-12 partitions for moderate topics, 30-100 for high-throughput topics, up to 500+ for extreme scale.
- **Diminishing returns**: Each partition adds memory overhead on the broker (~10 KB for leadership metadata) and increases rebalance time. More than a few thousand partitions per broker degrades performance.

---

## 6. Exactly-Once Semantics

### 6.1 The "Exactly-Once" Confusion

Exactly-once semantics (EOS) is one of the most misunderstood concepts in distributed systems, and Kafka interviews are where it comes up most often. The precise mental model is:

**"Exactly-once" in Kafka means "at-least-once delivery + idempotent processing, composed in a way that the end-to-end effect is as if each message was processed exactly once."**

Kafka does not magically guarantee that your application code runs exactly once. What it guarantees is that a message is written to the output topic and the consumer offset is committed **atomically** -- either both happen or neither does.

### 6.2 The Three Pillars of Kafka EOS

```
EXACTLY-ONCE SEMANTICS -- THREE PILLARS:

1. IDEMPOTENT PRODUCER (within a single partition)
   enable.idempotence=true
   → Broker deduplicates retries using PID + sequence number
   → Guarantees: no duplicate writes from producer retries

2. TRANSACTIONAL API (across partitions and topics)
   transactional.id=<unique-id>
   → Producer groups writes to multiple partitions + offset commits
     into an atomic transaction
   → All writes visible together, or none are

3. CONSUMER read_committed (transactional isolation)
   isolation.level=read_committed
   → Consumer only sees messages from committed transactions
   → Uncommitted (aborted) messages are skipped

TOGETHER:
  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
  │  Idempotent   │    │ Transactional │    │    Consumer   │
  │  Producer     │ +  │ API           │ +  │ read_committed│
  │  (no dups on  │    │ (atomic       │    │ (skip aborted │
  │   retry)      │    │  multi-write) │    │  transactions)│
  └──────────────┘    └──────────────┘    └──────────────┘
         │                    │                     │
         └────────────────────┴─────────────────────┘
                              │
                   End-to-end exactly-once
                   (consume → process → produce)
```

### 6.3 Transactional Producer Pattern

```python
from confluent_kafka import Producer

conf = {
    'bootstrap.servers': 'kafka-1:9092,kafka-2:9092,kafka-3:9092',
    'transactional.id': 'order-processor-instance-0',  # Must be stable across restarts
    'acks': 'all',
    'enable.idempotence': True,
}

producer = Producer(conf)
producer.init_transactions()

try:
    producer.begin_transaction()

    # Consume from input topic (offsets committed as part of this transaction)
    # Process the message
    # Produce to output topic
    producer.produce('processed-orders', key=order_id, value=result)

    # Commit consumer offsets AND output messages atomically
    producer.send_offsets_to_transaction(
        consumer.position(consumer.assignment()),
        consumer.consumer_group_metadata()
    )

    producer.commit_transaction()

except Exception as e:
    producer.abort_transaction()
    raise
```

### 6.4 When You Actually Need Exactly-Once

EOS adds latency (transactions have commit overhead) and operational complexity (fencing, transaction timeouts, abort handling). Most systems do not need it.

```
DECISION FRAMEWORK: DO YOU NEED EXACTLY-ONCE?

  Question 1: Can your consumer handle duplicates idempotently?
    YES → Use at-least-once. (Most common answer.)
          Example: Upserting into a database by primary key.
                   Second upsert overwrites with same data. Harmless.

    NO  → Continue to Question 2.

  Question 2: Is the processing a pure consume-transform-produce pipeline?
    YES → Use Kafka Transactions (EOS).
          Example: Kafka Streams application joining two topics
                   and writing to a third.

    NO  → Continue to Question 3.

  Question 3: Does processing involve an external system (DB, API)?
    YES → You CANNOT use Kafka EOS alone.
          Kafka transactions only span Kafka.
          You need application-level idempotency (dedup table, idempotency keys).
          Example: Consume from Kafka, write to PostgreSQL.
                   Use: at-least-once + transactional outbox + dedup.

COMMON INTERVIEW ANSWER:
  "For this pipeline, we'd use at-least-once delivery with idempotent
   consumers. The consumer upserts into the database using the event's
   unique ID as the primary key, so duplicates are harmlessly overwritten.
   We'd only reach for Kafka's exactly-once transactions if we had a pure
   Kafka-to-Kafka stream processing pipeline, like in Kafka Streams."
```

---

## 7. Kafka Streams and ksqlDB

### 7.1 Kafka Streams: Lightweight Stream Processing

Kafka Streams is a client library (not a cluster) for building stream processing applications. Unlike Flink or Spark Streaming, it runs as a regular JVM application -- no separate infrastructure to deploy and operate.

```
KAFKA STREAMS vs FLINK -- POSITIONING:

  Kafka Streams:
    - Library, not a framework (runs in your app's JVM)
    - No separate cluster to manage
    - Exactly-once via Kafka transactions
    - Scales by running more instances (consumer group model)
    - Best for: Kafka-to-Kafka transformations, lightweight aggregations
    - State stored locally in RocksDB, backed by changelog topics

  Flink:
    - Distributed stream processing cluster
    - Separate infrastructure (JobManager, TaskManagers)
    - Can read from Kafka, Kinesis, files, sockets, etc.
    - Complex event processing, large-scale ML pipelines
    - Best for: Multi-source joins, complex windowing, high-scale aggregation

  IN AN INTERVIEW:
    Small-to-medium stream processing → Kafka Streams
    Complex, multi-source, high-scale → Flink
```

### 7.2 KStream vs KTable

The two fundamental abstractions in Kafka Streams:

```
KSTREAM vs KTABLE:

KStream (event stream):
  Each record is an independent event.
  Key "alice" appears multiple times -- each is a separate event.

  Time →  (alice, click)  (bob, view)  (alice, purchase)  (alice, click)
           event 1         event 2      event 3            event 4

  All four records exist. Nothing is overwritten.
  Analogy: an INSERT-only audit log.

KTable (changelog stream):
  Each record is an UPDATE for its key.
  Key "alice" appears multiple times -- later values replace earlier ones.

  Time →  (alice, {balance: 100})  →  (alice, {balance: 75})  →  (alice, {balance: 120})

  Current state: alice → {balance: 120}
  Only the latest value per key is materialized.
  Analogy: a database table. Each key is a row, updated in place.

BACKED BY THE SAME TOPIC:
  A KTable is a KStream interpreted differently.
  The underlying Kafka topic is the same append-only log.
  The KTable abstraction maintains a local materialized view
  (in RocksDB) of the latest value per key.
```

### 7.3 Windowed Aggregations

Kafka Streams supports three window types for aggregating events over time:

```
WINDOW TYPES:

1. TUMBLING WINDOW (fixed, non-overlapping):
   |----5min----|----5min----|----5min----|
   Events in each window are aggregated independently.
   Example: Count clicks per item per 5-minute window.

2. HOPPING WINDOW (fixed, overlapping):
   |----5min----|
        |----5min----|
             |----5min----|
   Advance = 1min, Size = 5min
   Each event can belong to multiple windows.
   Example: Rolling 5-minute average, updated every minute.

3. SESSION WINDOW (dynamic, gap-based):
   |-events--gap--events-gap----events--|
   |  session 1  |   | session 2      |
   A session closes when no events arrive for the "inactivity gap."
   Example: User browsing session = events with < 30min gap.

4. SLIDING WINDOW (Kafka Streams 2.7+):
   Continuous window that fires on every record change.
   Used in joins: "join events within 10 minutes of each other."
```

### 7.4 State Stores and Changelog Topics

Kafka Streams maintains local state (for aggregations, joins, KTables) in **RocksDB** instances on each stream processing instance. This state is backed by **changelog topics** -- internal Kafka topics that record every state change.

If a stream processor instance crashes, its partitions are reassigned to another instance, which rebuilds the local state by replaying the changelog topic. This is how Kafka Streams achieves fault-tolerant stateful processing without an external database.

```
STATE STORE ARCHITECTURE:

  Stream Processor Instance 0:
  ┌─────────────────────────────────┐
  │ Input: user-events (P0, P1)     │
  │                                  │
  │ Local RocksDB state store:       │
  │   user_123 → {click_count: 47}  │
  │   user_456 → {click_count: 12}  │
  │                                  │
  │ Backed by changelog topic:       │
  │   app-id-store-changelog (P0,P1) │
  └─────────────────────────────────┘

  If Instance 0 crashes:
  Instance 1 takes over P0, P1.
  Replays changelog topic to rebuild RocksDB.
  Resumes processing from last committed offset.

  Standby replicas (num.standby.replicas=1) can pre-warm
  state on other instances, reducing failover time.
```

### 7.5 ksqlDB

ksqlDB is a SQL interface on top of Kafka Streams. It allows creating stream processing pipelines with SQL syntax:

```sql
-- Create a stream from a Kafka topic
CREATE STREAM user_clicks (
    user_id VARCHAR KEY,
    item_id VARCHAR,
    click_time TIMESTAMP
) WITH (
    KAFKA_TOPIC='user-clicks',
    VALUE_FORMAT='JSON'
);

-- Real-time aggregation: clicks per item in 5-minute tumbling windows
CREATE TABLE item_click_counts AS
    SELECT item_id,
           COUNT(*) AS click_count,
           WINDOWSTART AS window_start,
           WINDOWEND AS window_end
    FROM user_clicks
    WINDOW TUMBLING (SIZE 5 MINUTES)
    GROUP BY item_id
    EMIT CHANGES;

-- Materialized view queryable via pull queries
SELECT item_id, click_count
FROM item_click_counts
WHERE item_id = 'product_456';
```

ksqlDB is useful in interviews as a quick explanation for "how do we compute real-time aggregations from Kafka." It is not a replacement for Flink at scale, but for moderate aggregations (counts, sums, windowed joins), it eliminates the need for a separate stream processing cluster.

---

## 8. Kafka Connect

### 8.1 Source and Sink Connectors

Kafka Connect is a framework for moving data between Kafka and external systems without writing custom code. It runs as a distributed cluster of **workers** that execute **connectors** (plugins).

```
KAFKA CONNECT ARCHITECTURE:

  Source Connectors (external → Kafka):
  ┌─────────┐    ┌──────────────┐    ┌───────────────┐
  │ Postgres │───>│ Debezium CDC │───>│ Kafka topic   │
  │ MySQL    │───>│ JDBC Source  │───>│ "db.orders"   │
  │ MongoDB  │───>│ Mongo Source │───>│ "mongo.users"  │
  │ Files    │───>│ File Source  │───>│ "log-events"   │
  └─────────┘    └──────────────┘    └───────────────┘

  Sink Connectors (Kafka → external):
  ┌───────────────┐    ┌──────────────┐    ┌─────────────┐
  │ Kafka topic   │───>│ ES Sink      │───>│ Elasticsearch│
  │ "enriched-    │───>│ S3 Sink      │───>│ S3 (data     │
  │  events"      │───>│ JDBC Sink    │───>│  lake)       │
  │               │───>│ Redis Sink   │───>│ Redis cache  │
  └───────────────┘    └──────────────┘    └─────────────┘
```

### 8.2 Change Data Capture with Debezium

**Debezium** is the most important Kafka Connect source connector for system design interviews. It reads a database's transaction log (WAL in Postgres, binlog in MySQL) and publishes each row-level change as a Kafka event.

```
DEBEZIUM CDC FLOW:

  PostgreSQL                   Debezium                    Kafka
  ┌──────────┐                ┌──────────┐               ┌──────────┐
  │ INSERT   │  WAL stream    │ Reads    │  Produces     │ Topic:   │
  │ UPDATE   │ ──────────────>│ WAL via  │ ─────────────>│ dbserver │
  │ DELETE   │  (logical      │ logical  │  (JSON/Avro   │ .public  │
  │          │  replication)  │ repl.    │   events)     │ .orders  │
  └──────────┘                └──────────┘               └──────────┘

  Each event contains:
  {
    "before": { "id": 42, "status": "pending", "total": 99.99 },
    "after":  { "id": 42, "status": "shipped", "total": 99.99 },
    "op": "u",           // c=create, u=update, d=delete, r=read(snapshot)
    "ts_ms": 1694352000000,
    "source": {
      "db": "orders_db", "table": "orders",
      "lsn": 234567890,  // WAL position
      "txId": 12345
    }
  }
```

**Why CDC matters in interviews**: It is the standard answer for "how do we keep our cache/search index/data warehouse in sync with the source database." Instead of dual-writing (which has consistency problems), you write to the database and let Debezium capture the change. This guarantees that the downstream system eventually sees every change, in order.

### 8.3 Schema Registry

The **Confluent Schema Registry** stores and enforces schemas (Avro, Protobuf, JSON Schema) for Kafka topics. It assigns a numeric ID to each schema version, and producers/consumers embed only the schema ID in the message rather than the full schema.

```
SCHEMA REGISTRY FLOW:

  Producer:
    1. Serialize record with schema v1
    2. Register schema v1 with registry → gets ID=1
    3. Write [magic_byte | schema_id=1 | data] to Kafka

  Consumer:
    1. Read message, extract schema_id=1
    2. Fetch schema v1 from registry (cached locally)
    3. Deserialize using the schema

  COMPATIBILITY MODES:
    BACKWARD:  New schema can read data written with previous schema.
               Safe for consumer upgrades. (Default, recommended.)
    FORWARD:   Previous schema can read data written with new schema.
               Safe for producer upgrades.
    FULL:      Both backward and forward compatible.
    NONE:      No compatibility checking. Dangerous.
```

---

## 9. Performance Tuning and Internals

### 9.1 Why Kafka Is Fast: Sequential I/O and the Page Cache

Kafka achieves high throughput despite writing everything to disk because it exploits two OS-level optimizations:

**Sequential I/O**: Kafka always appends to the end of a log file. Sequential writes to a modern SSD achieve 300-600 MB/s; sequential writes to spinning disk achieve 50-100 MB/s. Random writes achieve 0.1-1 MB/s. Kafka's append-only design turns disk I/O from a bottleneck into an advantage.

**OS page cache**: Kafka deliberately does not maintain its own in-memory cache. Instead, it relies on the Linux page cache. When Kafka writes a message to the log file, the OS caches that page in memory. When a consumer reads the message shortly after (the common case -- tailing the log), the read is served from the page cache with no disk I/O at all.

```
KAFKA I/O PATH (no zero-copy):

  Producer write:
    Network → Kafka broker JVM → OS page cache → Disk (async)
                                      ↓
  Consumer read (recent data):        ↓
    OS page cache → Kafka broker JVM → Network
    (no disk I/O -- data is still in page cache!)

  Consumer read (old data beyond page cache):
    Disk → OS page cache → Kafka broker JVM → Network

KEY INSIGHT:
  Kafka JVM heap is typically 4-6 GB regardless of data volume.
  The REST of the machine's RAM is used as page cache.
  A 64 GB RAM machine with 6 GB JVM heap has ~58 GB page cache.
  That caches the most recent ~58 GB of data across all partitions.
```

### 9.2 Zero-Copy (sendfile)

Kafka uses the Linux `sendfile()` system call to transfer data from the page cache directly to the network socket, bypassing the user-space JVM entirely.

```
TRADITIONAL DATA TRANSFER (4 copies, 4 context switches):

  Disk → Kernel read buffer → User space buffer → Kernel socket buffer → NIC
         (copy 1)              (copy 2)            (copy 3)             (copy 4)

ZERO-COPY WITH sendfile() (2 copies, 2 context switches):

  Disk → Kernel read buffer ──────────────────────────────> NIC
         (copy 1)            (DMA from page cache to NIC)   (copy 2)

  Bypasses user space entirely.
  CPU never touches the data.
  Reduces CPU usage by 50-70% for consumer-fetch workloads.
```

This is why Kafka consumers can read at sustained 100+ MB/s per broker with minimal CPU impact: the data flows from disk (or page cache) directly to the network card without passing through the JVM.

### 9.3 Partition Count Selection

The formula for choosing partition count:

```
PARTITION COUNT FORMULA:

  P = max(T_p / t_p, T_c / t_c)

  Where:
    T_p = target producer throughput (e.g., 200 MB/s)
    t_p = throughput achievable per partition by a producer (~30-50 MB/s)
    T_c = target consumer throughput (e.g., 100 MB/s)
    t_c = throughput achievable per partition by a consumer (~50-100 MB/s)

  Example:
    T_p = 200 MB/s,  t_p = 40 MB/s  → need 5 partitions for producers
    T_c = 200 MB/s,  t_c = 50 MB/s  → need 4 partitions for consumers

    P = max(5, 4) = 5 partitions minimum

  But also consider:
    - Target consumer parallelism (want 12 consumer instances? Need >= 12 partitions)
    - Over-provision by 2-3x for growth
    - Final answer for this example: 12-24 partitions

PARTITION COUNT GUIDELINES:
  < 10 partitions:   Small topic, low throughput
  10-50 partitions:  Medium topic, typical production use
  50-200 partitions: High-throughput topic
  200+ partitions:   Extreme scale (log aggregation, clickstream)

  UPPER LIMIT per broker: ~4,000 partitions (leader + follower)
  UPPER LIMIT per cluster: ~200,000 (ZooKeeper) or millions (KRaft)
```

### 9.4 Replication Factor Tradeoffs

```
REPLICATION FACTOR TRADEOFFS:

  RF=1:  No redundancy. Broker failure = data loss and partition unavailability.
         Use: dev/test only. NEVER in production for data you care about.

  RF=2:  Survives 1 broker failure. But with min.insync.replicas=2,
         a single replica failure makes the partition read-only.
         Rarely used in practice.

  RF=3:  The standard production setting. Survives 1 broker failure
         with min.insync.replicas=2 (the remaining 2 replicas keep
         the partition writable). Survives 2 broker failures for reads.
         Use: almost everything in production.

  RF=4+: Diminishing returns. 3x replication is already beyond what
         most systems need. Higher RF increases write latency (acks=all
         waits for more replicas) and storage cost.

STORAGE IMPACT:
  100 GB of data at RF=3 requires 300 GB of total disk across the cluster.
  This is a real cost at scale -- 10 TB of data means 30 TB of disk.
```

---

## 10. Common Interview Patterns

### 10.1 Event Sourcing Backbone

Kafka as the single source of truth for all state changes. Instead of storing current state in a database, you store the sequence of events that produced the current state.

```
EVENT SOURCING WITH KAFKA:

  Command → Validate → Append Event → Update Materialized View

  Topic: "order-events" (retention: forever, or log-compacted)
  ┌────────────────────────────────────────────────────┐
  │ {order_id: 42, type: "created",  items: [...]}     │ offset 0
  │ {order_id: 42, type: "paid",     amount: 99.99}    │ offset 1
  │ {order_id: 42, type: "shipped",  tracking: "..."}  │ offset 2
  │ {order_id: 42, type: "delivered", signed_by: "..."}│ offset 3
  └────────────────────────────────────────────────────┘

  Materialized View (rebuilt by replaying events):
    orders table: {id: 42, status: "delivered", amount: 99.99, ...}

  Benefits:
    - Complete audit trail (every state change recorded)
    - Can rebuild any view by replaying events
    - Can add new consumers (new views) at any time
    - Time-travel debugging: replay events up to a point in time

  Kafka requirement: log compaction or infinite retention
    log.cleanup.policy=compact (keeps latest value per key)
    OR retention.ms=-1 (keep forever, disk cost scales linearly)
```

### 10.2 Real-Time Feature Pipeline

This is the most common Kafka pattern in ML system design interviews.

```
REAL-TIME FEATURE PIPELINE:

  User action → Kafka → Stream processor → Feature store → Model serving

  ┌──────────┐    ┌────────────────┐    ┌────────────┐    ┌─────────────┐
  │ App      │    │ Kafka topic    │    │ Flink /    │    │ Feature     │
  │ server   │───>│ "user-actions" │───>│ Kafka      │───>│ Store       │
  │ (click,  │    │                │    │ Streams    │    │ (Redis /    │
  │  view,   │    │ key: user_id   │    │            │    │  DynamoDB)  │
  │  search) │    └────────────────┘    │ Computes:  │    └──────┬──────┘
  └──────────┘                          │ - click    │           │
                                        │   rate     │    ┌──────┴──────┐
                                        │ - session  │    │ Model       │
                                        │   length   │    │ Serving     │
                                        │ - search   │    │ (get feats  │
                                        │   entropy  │    │  for user)  │
                                        └────────────┘    └─────────────┘

  WHY KAFKA:
    - Decouples app servers from feature computation
    - Multiple consumer groups compute different features independently
    - Replay capability: retrain features by re-reading historical events
    - Handles burst traffic (app servers are not blocked by slow feature computation)
```

### 10.3 CDC for Cache Invalidation

```
CDC CACHE INVALIDATION:

  Write path:
    App → PostgreSQL (source of truth)

  Sync path:
    PostgreSQL WAL → Debezium → Kafka → Cache Invalidator → Redis

  ┌──────┐    ┌──────────┐    ┌──────────┐    ┌───────────┐    ┌───────┐
  │ App  │───>│ Postgres │    │ Debezium │    │ Kafka     │    │ Redis │
  │      │    │          │───>│ (CDC)    │───>│ topic:    │───>│ cache │
  │      │    │          │    │          │    │ db.public │    │       │
  │      │───────────────────────────────────────────────────>│ (read)│
  └──────┘    └──────────┘    └──────────┘    └───────────┘    └───────┘

  WHY NOT DUAL-WRITE:
    Without CDC: App writes to Postgres AND invalidates Redis.
    Problem: if the Redis invalidation fails (network issue, Redis down),
    the cache is stale INDEFINITELY. Or worse, the app crashes between
    the DB write and the cache invalidation -- now they are inconsistent.

    With CDC: App writes ONLY to Postgres. Debezium captures the WAL
    change and publishes it to Kafka. A consumer invalidates Redis.
    Postgres is the single source of truth. Redis is eventually consistent.
    If the consumer falls behind, lag is visible and alerts fire.
```

### 10.4 Fan-Out for Notifications

```
NOTIFICATION FAN-OUT:

  Event source → Kafka → Multiple notification channels

  Topic: "user-notifications" (key: user_id)

  ┌──────────────┐    ┌──────────────────────────────────────┐
  │ Order Service │    │  Consumer Group "push":              │
  │ "order        │    │    → Send push notification          │
  │  shipped"     │    ├──────────────────────────────────────┤
  │               │───>│  Consumer Group "email":             │
  │               │    │    → Send email via SES/SendGrid     │
  │               │    ├──────────────────────────────────────┤
  │               │    │  Consumer Group "sms":               │
  │               │    │    → Send SMS via Twilio              │
  │               │    ├──────────────────────────────────────┤
  │               │    │  Consumer Group "in-app":            │
  │               │    │    → Write to notifications DB        │
  │               │    ├──────────────────────────────────────┤
  │               │    │  Consumer Group "analytics":         │
  │               │    │    → Track notification delivery     │
  └──────────────┘    └──────────────────────────────────────┘

  Each consumer group processes ALL events independently.
  Email can be slow without blocking push notifications.
  Adding a new channel (e.g., Slack) = adding a new consumer group.
  No change to the producer or existing consumers.
```

### 10.5 Log Aggregation

```
LOG AGGREGATION:

  Application servers → Kafka → Storage / Search

  ┌────────┐                                    ┌──────────────┐
  │ App 1  │──┐                            ┌───>│ Elasticsearch│
  │ App 2  │──┤    ┌──────────────────┐    │    │ (search/     │
  │ App 3  │──┼───>│ Kafka topic:     │────┤    │  alerting)   │
  │  ...   │──┤    │ "application-    │    │    └──────────────┘
  │ App N  │──┘    │  logs"           │    │    ┌──────────────┐
  └────────┘       └──────────────────┘    ├───>│ S3 / HDFS    │
                                           │    │ (long-term   │
  Benefits over direct shipping:           │    │  archival)   │
  - Decouples apps from log storage        │    └──────────────┘
  - Handles burst (apps not blocked)       │    ┌──────────────┐
  - Multiple sinks from one stream         └───>│ Real-time    │
  - Replay for reindexing                       │ dashboards   │
  - Backpressure absorbed by Kafka              └──────────────┘
```

### 10.6 Dead Letter Queues

```
DEAD LETTER QUEUE PATTERN:

  Main topic → Consumer → Success → commit offset
                  │
                  └──→ Processing failure (3 retries exhausted)
                        │
                        ├──→ Produce to DLQ topic
                        └──→ Commit offset on main topic (move forward)

  Topic: "order-events"      Topic: "order-events.DLQ"
  ┌────────────────┐         ┌────────────────────────┐
  │ msg1 (ok)      │         │ msg3 (failed 3x):      │
  │ msg2 (ok)      │         │   original_msg + error  │
  │ msg3 (FAILED)  │──DLQ──> │   + stack_trace         │
  │ msg4 (ok)      │         │   + failure_timestamp    │
  │ msg5 (ok)      │         │   + retry_count          │
  └────────────────┘         └────────────────────────┘

  DLQ consumers:
    - Manual review dashboard
    - Automated retry with exponential backoff
    - Alerting (DLQ depth > threshold → page on-call)

  CRITICAL: Always commit the offset for the failed message
  after writing to the DLQ. Otherwise the consumer is stuck
  forever on the poison message (a "poison pill").
```

---

## 11. Capacity Planning

### 11.1 Back-of-Envelope Math

This is the kind of calculation interviewers expect you to walk through.

```
SCENARIO:
  E-commerce platform, 50,000 events/sec peak,
  average event size 500 bytes, retention 7 days.

STEP 1: RAW THROUGHPUT
  Events:     50,000 events/sec
  Event size: 500 bytes (0.5 KB)
  Raw rate:   50,000 * 500 = 25 MB/sec = 25 MBps

STEP 2: WITH REPLICATION (RF=3)
  Write throughput at broker level: 25 MBps * 3 = 75 MBps
  (Each message is written to 3 brokers)

STEP 3: STORAGE
  Daily volume:  25 MB/s * 86,400 s/day = 2,160 GB/day = ~2.1 TB/day
  With RF=3:     2.1 TB * 3 = 6.3 TB/day across the cluster
  7-day retention: 6.3 * 7 = 44.1 TB total cluster storage
  With compression (lz4, ~2.5x): 44.1 / 2.5 = ~17.6 TB compressed

STEP 4: PARTITION COUNT
  Target throughput: 25 MB/sec
  Per-partition throughput: ~10 MB/sec (conservative estimate)
  Minimum partitions for throughput: 25 / 10 = 3 partitions

  But we also want parallelism for consumers.
  If we need 12 consumer instances: >= 12 partitions.

  Over-provision for growth (2x): 24 partitions.
  Final: 24 partitions for the main topic.

STEP 5: BROKER COUNT
  Each broker can handle ~200 MB/sec aggregate throughput (NVMe SSDs).
  With 75 MBps write load + ~25 MBps consumer read load = ~100 MBps.
  Minimum brokers: 100 / 200 = 1 broker (for throughput alone).

  But for fault tolerance with RF=3: minimum 3 brokers.
  For headroom and partition distribution: 5-6 brokers.

  Storage per broker: 17.6 TB / 6 = ~3 TB per broker.
  Each broker needs ~3 TB NVMe + 64 GB RAM (for page cache).

SUMMARY:
  ┌──────────────────────────────────────────────┐
  │ Cluster sizing for 50K events/sec @ 500B     │
  │                                               │
  │ Brokers:         6 (m5.4xlarge or similar)    │
  │ Partitions:      24 (main topic)              │
  │ Replication:     RF=3, min.insync.replicas=2  │
  │ Compression:     lz4                          │
  │ Storage/broker:  ~3 TB NVMe                   │
  │ RAM/broker:      64 GB (6 GB heap + page cache)│
  │ Retention:       7 days                        │
  │ Total storage:   ~18 TB (compressed)           │
  └──────────────────────────────────────────────┘
```

### 11.2 Retention Sizing Quick Reference

```
RETENTION SIZING:

  Daily data = events_per_sec * avg_event_bytes * 86400

  ┌─────────────┬────────────┬──────────────┬─────────────────┐
  │ Events/sec  │ Avg size   │ Daily (raw)  │ 7-day RF=3 lz4  │
  ├─────────────┼────────────┼──────────────┼─────────────────┤
  │ 1,000       │ 500 B      │ 43 GB        │ 362 GB          │
  │ 10,000      │ 500 B      │ 432 GB       │ 3.6 TB          │
  │ 50,000      │ 500 B      │ 2.1 TB       │ 17.6 TB         │
  │ 100,000     │ 1 KB       │ 8.6 TB       │ 72 TB           │
  │ 1,000,000   │ 200 B      │ 17.3 TB      │ 145 TB          │
  └─────────────┴────────────┴──────────────┴─────────────────┘

  Formula for 7-day retention with RF=3 and lz4 compression:
    total_storage = events_per_sec * avg_bytes * 86400 * 7 * 3 / 2.5
```

---

## 12. Failure Modes and Operational Concerns

### 12.1 Broker Failure and Partition Leader Election

When a broker hosting a partition leader fails, the controller detects the failure (via heartbeat timeout) and elects a new leader from the partition's **in-sync replica set (ISR)**.

```
LEADER ELECTION ON BROKER FAILURE:

  Before failure:
    Partition 5: Leader=Broker2, ISR=[Broker2, Broker0, Broker1]

  Broker2 crashes:

  Step 1: Controller detects Broker2 is down (missed heartbeats).
  Step 2: Controller removes Broker2 from ISR for all its partitions.
          ISR=[Broker0, Broker1]
  Step 3: Controller elects new leader from remaining ISR.
          New leader = Broker0 (first in ISR order).
  Step 4: Controller updates metadata. Clients refresh.
  Step 5: Producers and consumers reconnect to Broker0 for Partition 5.

  Failover time: typically 1-5 seconds with KRaft.
  (With ZooKeeper: 5-30 seconds in pathological cases.)

  During failover, produce requests for this partition return errors.
  Well-configured producers (retries > 0, retry.backoff.ms) handle this
  transparently -- the client retries until the new leader is available.
```

### 12.2 ISR Shrink and Unclean Leader Election

```
ISR SHRINK:

  Partition 5: Leader=Broker0, ISR=[Broker0, Broker1, Broker2]

  Broker2's replication falls behind (network partition, slow disk):
    Broker2's replica is more than replica.lag.time.max.ms behind.

  ISR shrinks: ISR=[Broker0, Broker1]
  With min.insync.replicas=2: partition is still writable.

  If Broker1 ALSO falls behind: ISR=[Broker0]
  With min.insync.replicas=2: partition becomes READ-ONLY.
  Producers receive NotEnoughReplicasException.

  This is correct behavior! Better to reject writes than lose them.

UNCLEAN LEADER ELECTION (unclean.leader.election.enable):

  Scenario: Partition leader fails, and ALL ISR members are also down.
  Only out-of-sync replicas remain.

  unclean.leader.election.enable=false (DEFAULT, recommended):
    Partition is UNAVAILABLE until an ISR member recovers.
    Zero data loss. Availability sacrificed for consistency.

  unclean.leader.election.enable=true:
    An out-of-sync replica becomes leader.
    Messages that the old leader had but this replica missed are LOST.
    Availability preserved at the cost of data loss.

  INTERVIEW ANSWER: "We'd keep unclean leader election disabled.
  Losing availability for one partition is better than losing data.
  This is the AP vs CP tradeoff in CAP -- for event data, we
  choose consistency."
```

### 12.3 Consumer Rebalance Storms

A rebalance storm occurs when consumer group membership changes rapidly, triggering repeated rebalances before any single rebalance completes.

```
REBALANCE STORM:

  t=0   Consumer C3 crashes. Rebalance starts.
  t=5s  During rebalance, C2's heartbeat is delayed (GC pause).
        Group coordinator thinks C2 is dead. Triggers NEW rebalance.
  t=10s Rebalance completes, assigns partitions. C2 rejoins.
        Group coordinator triggers ANOTHER rebalance.
  t=15s During rebalance, C1 exceeds max.poll.interval.ms.
        Kicked out. ANOTHER rebalance.

  Result: continuous rebalancing, zero consumption, all consumers thrashing.

PREVENTION:
  1. Use cooperative rebalancing (reduces scope of each rebalance)
  2. Tune heartbeat and session timeouts:
     session.timeout.ms=30000     (how long before coordinator considers consumer dead)
     heartbeat.interval.ms=10000  (must be < session.timeout.ms / 3)
     max.poll.interval.ms=300000  (how long between poll() calls before being kicked)
  3. Ensure consumer processing time < max.poll.interval.ms
  4. Pin consumer group membership with static group membership:
     group.instance.id=<stable-id>  (consumer gets a stable identity,
     coordinator waits session.timeout.ms before reassigning its partitions)
```

### 12.4 Key Monitoring Metrics

```
ESSENTIAL KAFKA METRICS AND PROMQL:

# Consumer lag (THE most important metric)
# Alert if lag is growing or exceeds retention threshold
kafka_consumergroup_lag{
    group="order-processing",
    topic="order-events"
}

# Under-replicated partitions (ISR < RF)
# Alert: ANY under-replicated partitions for more than 5 minutes
kafka_server_replicamanager_underreplicatedpartitions > 0

# Active controller count (should be exactly 1)
kafka_controller_kafkacontroller_activecontrollercount != 1

# Request latency (produce and fetch)
kafka_network_requestmetrics_totaltimems{
    request="Produce",
    quantile="0.99"
}

# Bytes in/out per broker
rate(kafka_server_brokertopicmetrics_bytesinpersec[5m])
rate(kafka_server_brokertopicmetrics_bytesoutpersec[5m])

# ISR shrink rate (leading indicator of replication issues)
rate(kafka_server_replicamanager_isrshrinkspersec[5m]) > 0

# Offline partitions (partitions with no leader -- immediate alert!)
kafka_controller_kafkacontroller_offlinepartitionscount > 0

# Log flush latency (disk issues)
kafka_log_logflushrateandrequestmetrics_logflushtimems{
    quantile="0.99"
}

ALERT THRESHOLDS (reasonable starting points):
  ┌──────────────────────────────────┬────────────────────────┐
  │ Metric                           │ Alert threshold        │
  ├──────────────────────────────────┼────────────────────────┤
  │ Consumer lag (messages)          │ > 100,000 for 10 min   │
  │ Under-replicated partitions      │ > 0 for 5 min          │
  │ Offline partitions               │ > 0 (immediate)        │
  │ Active controllers               │ != 1 (immediate)       │
  │ ISR shrink rate                  │ > 0 for 5 min          │
  │ Produce latency p99              │ > 500ms                │
  │ Consumer group rebalances/hour   │ > 5                    │
  │ Disk usage per broker            │ > 75%                  │
  └──────────────────────────────────┴────────────────────────┘
```

### 12.5 Split Brain (Pre-KRaft)

In ZooKeeper mode, a network partition between the ZooKeeper ensemble and some brokers could cause a split brain: the controller believes certain brokers are dead and elects new partition leaders, while the old leaders are still alive and serving clients who haven't refreshed metadata. This was one of the most dangerous Kafka failure modes.

KRaft mitigates this by integrating consensus into Kafka itself. The controller quorum uses Raft, which has well-defined leader election and epoch fencing. A leader from an old epoch cannot make progress once a new leader is elected, because its writes will be rejected by the quorum.

---

## 13. Kafka vs Alternatives -- Decision Matrix

### 13.1 Comparison Table

```
┌──────────────┬────────────┬────────────┬────────────┬────────────┬────────────┐
│              │ Kafka      │ RabbitMQ   │ AWS SQS    │ Pulsar     │ Redis      │
│              │            │            │            │            │ Streams    │
├──────────────┼────────────┼────────────┼────────────┼────────────┼────────────┤
│ Model        │ Distributed│ Message    │ Managed    │ Distributed│ In-memory  │
│              │ commit log │ broker     │ queue      │ log +      │ append-only│
│              │            │ (AMQP)     │            │ segments   │ log        │
├──────────────┼────────────┼────────────┼────────────┼────────────┼────────────┤
│ Ordering     │ Per-       │ Per-queue  │ Best-      │ Per-       │ Per-stream │
│              │ partition  │ (FIFO)     │ effort     │ partition  │            │
│              │            │            │ (FIFO opt) │            │            │
├──────────────┼────────────┼────────────┼────────────┼────────────┼────────────┤
│ Throughput   │ Millions   │ Tens of    │ Thousands  │ Millions   │ Hundreds   │
│ (msgs/sec)   │ per sec    │ thousands  │ per sec    │ per sec    │ of thous.  │
├──────────────┼────────────┼────────────┼────────────┼────────────┼────────────┤
│ Replay       │ Yes        │ No (msgs   │ No (msgs   │ Yes        │ Yes (but   │
│              │ (offset    │ deleted    │ deleted    │ (cursor    │ limited by │
│              │ rewind)    │ on ack)    │ on ack)    │ rewind)    │ memory)    │
├──────────────┼────────────┼────────────┼────────────┼────────────┼────────────┤
│ Retention    │ Time or    │ Until      │ 14 days    │ Time or    │ Capped by  │
│              │ size-based │ consumed   │ max        │ size-based │ memory or  │
│              │ (or forever│            │            │ + tiered   │ maxlen     │
│              │ compacted) │            │            │ storage    │            │
├──────────────┼────────────┼────────────┼────────────┼────────────┼────────────┤
│ Consumer     │ Pull       │ Push       │ Pull       │ Push+Pull  │ Pull       │
│ model        │            │            │ (long-poll)│            │ (XREAD)    │
├──────────────┼────────────┼────────────┼────────────┼────────────┼────────────┤
│ Exactly-once │ Yes (trans-│ No (at-    │ No (at-    │ Yes (trans-│ No         │
│              │ actional)  │ least-once)│ least-once)│ actional)  │            │
├──────────────┼────────────┼────────────┼────────────┼────────────┼────────────┤
│ Ops          │ High       │ Medium     │ Zero       │ High       │ Low        │
│ complexity   │ (KRaft     │            │ (managed)  │ (ZK + BK)  │            │
│              │ helps)     │            │            │            │            │
├──────────────┼────────────┼────────────┼────────────┼────────────┼────────────┤
│ Best for     │ Event      │ Task       │ Simple     │ Multi-     │ Lightweight│
│              │ streaming, │ queues,    │ async      │ tenant     │ event log, │
│              │ data       │ RPC-style  │ decoupling,│ streaming, │ real-time  │
│              │ pipelines, │ work       │ serverless │ geo-       │ features   │
│              │ event      │ distribution│ triggers  │ replication│ (low vol.) │
│              │ sourcing   │            │            │            │            │
└──────────────┴────────────┴────────────┴────────────┴────────────┴────────────┘
```

### 13.2 Decision Flowchart

```
WHEN TO USE WHAT:

  Need replay / event sourcing / multiple independent consumers?
    YES → Kafka or Pulsar
    NO  → Continue

  Need simple task queue (produce, consume, done)?
    YES → SQS (if on AWS) or RabbitMQ
    NO  → Continue

  Need > 100K msgs/sec sustained throughput?
    YES → Kafka
    NO  → Continue

  Need geo-replicated multi-tenant streaming?
    YES → Pulsar (native multi-tenancy + geo-replication)
         or Kafka (with MirrorMaker 2 / Confluent Replicator)
    NO  → Continue

  Need lightweight, low-latency pub/sub for < 10K msgs/sec?
    YES → Redis Streams or Redis Pub/Sub
    NO  → Continue

  Fully managed, minimal ops, simple use case?
    YES → SQS / SNS
    NO  → Kafka (the default for anything non-trivial)

INTERVIEW SHORTCUT:
  "We'd use Kafka here because we need [replay / fan-out / ordering /
  high throughput / event sourcing]. If this were a simple task queue
  where messages are consumed and discarded, we'd use SQS instead."
```

### 13.3 Kafka vs Pulsar: The Deeper Comparison

Pulsar is the closest alternative to Kafka for streaming workloads. The key architectural difference is **separation of compute (brokers) and storage (BookKeeper)**:

```
KAFKA vs PULSAR ARCHITECTURE:

Kafka:
  Brokers store data on local disks.
  Rebalancing partitions = copying data between brokers.
  Scaling up = add broker, then rebalance (data movement).

Pulsar:
  Brokers are stateless. Data is in BookKeeper (distributed log store).
  Rebalancing = reassign ownership (no data movement).
  Scaling up = add broker, immediate load pickup.
  Tiered storage: old data can move to S3 automatically.

Pulsar advantages:
  - Faster scaling (no data rebalancing)
  - Native multi-tenancy (namespaces with quotas)
  - Native geo-replication
  - Tiered storage (hot data in BK, cold data in S3)

Kafka advantages:
  - Larger ecosystem (connectors, tools, community)
  - Simpler architecture (fewer moving parts without BK)
  - Better tooling and monitoring
  - More engineers know it (hiring, onboarding)
  - KRaft removes ZK dependency; Pulsar still needs ZK + BK
```

---

## Appendix A: Quick-Reference Configuration Cheat Sheet

```
PRODUCER CONFIGURATION (production defaults):

  acks=all                                    # Wait for all ISR replicas
  enable.idempotence=true                     # Deduplicate retries
  max.in.flight.requests.per.connection=5     # Required for idempotence
  retries=2147483647                          # Retry indefinitely (bounded by delivery.timeout.ms)
  delivery.timeout.ms=120000                  # 2 minutes total delivery timeout
  linger.ms=20                                # Batch for 20ms
  batch.size=65536                            # 64 KB batches
  compression.type=lz4                        # Good ratio, low CPU
  buffer.memory=67108864                      # 64 MB send buffer

CONSUMER CONFIGURATION (production defaults):

  enable.auto.commit=false                    # Manual commits only
  auto.offset.reset=earliest                  # Start from beginning if no offset
  isolation.level=read_committed              # If using transactions
  max.poll.records=500                        # Records per poll()
  max.poll.interval.ms=300000                 # 5 min max between polls
  session.timeout.ms=30000                    # 30 sec session timeout
  heartbeat.interval.ms=10000                 # 10 sec heartbeat
  partition.assignment.strategy=              # Cooperative sticky
    org.apache.kafka.clients.consumer.CooperativeStickyAssignor

BROKER CONFIGURATION (production defaults):

  num.partitions=6                            # Default for auto-created topics
  default.replication.factor=3                # RF for auto-created topics
  min.insync.replicas=2                       # Minimum ISR for acks=all
  unclean.leader.election.enable=false        # Never elect out-of-sync replica
  log.retention.hours=168                     # 7 days retention
  log.segment.bytes=1073741824                # 1 GB segment size
  log.retention.check.interval.ms=300000      # Check every 5 minutes
  num.io.threads=8                            # I/O threads (cores)
  num.network.threads=3                       # Network threads
  replica.lag.time.max.ms=30000               # 30 sec before ISR removal
```

## Appendix B: Interview Answer Template

When Kafka appears in your system design answer, use this structure:

```
1. STATE THE NEED:
   "We need [replay / fan-out / ordering / decoupling / high throughput],
    so we'll use Kafka as our event backbone."

2. DEFINE THE TOPIC AND KEY:
   "The topic is [name], keyed by [field] because we need ordering
    per [entity]. This gives us [N] partitions for parallelism."

3. ACKNOWLEDGE DELIVERY SEMANTICS:
   "Producers use acks=all with idempotence for durability.
    Consumers use manual offset commits for at-least-once delivery,
    with idempotent writes to [downstream system] for dedup."

4. ADDRESS FAILURE:
   "With RF=3 and min.insync.replicas=2, we survive a single broker
    failure. Consumer lag is our primary alert -- if it exceeds
    [threshold], we scale the consumer group."

5. SIZE IT:
   "At [N] events/sec averaging [M] bytes, that's [X] MB/s.
    With RF=3 and lz4, we need [Y] TB for [Z] days retention
    across [W] brokers."
```

This template covers the five things an interviewer wants to hear: why Kafka (not just "because it's popular"), how you'd key it (ordering and hot partition awareness), what guarantees you're providing (delivery semantics), how it handles failure (replication and monitoring), and that you can size it (capacity planning).

---

*Last updated: 2025. Cross-references: `04-replication-and-consistency.md` (replication models), `33-resilience-patterns-circuit-breakers.md` (consumer resilience), `34-adaptive-load-control-and-backpressure.md` (backpressure in streaming pipelines).*
