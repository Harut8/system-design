# 25 — Streaming and Kafka Observability

> Async pipelines fail differently from synchronous ones. There's no failed HTTP response to count, no client waiting on the line, no obvious P99. The signals are *lag*, *throughput*, *partition skew*, *DLQ depth*, *exactly-once breakage*. Streaming observability is the discipline of making asynchronous systems debuggable as systems, not as black boxes.

This chapter is about Kafka, Kinesis, Pub/Sub, NATS, Pulsar, and the streaming consumer/producer patterns built on them. The principles generalize across engines.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [Why streaming is different](#2-why-different)
3. [The four streaming signals](#3-four-signals)
4. [Consumer lag: the dominant SLI](#4-consumer-lag)
5. [Producer signals: throughput, batch size, error rate](#5-producer-signals)
6. [Partition skew: when one partition wrecks the cluster](#6-partition-skew)
7. [Broker / cluster signals](#7-broker-signals)
8. [Tracing across async boundaries](#8-async-tracing)
9. [Exactly-once and idempotency observability](#9-exactly-once)
10. [Dead-letter queues and poison messages](#10-dlq)
11. [Schema registry and compatibility signals](#11-schema-registry)
12. [Stream-processing engines (Flink, KStreams, Spark)](#12-stream-processors)
13. [Per-engine specifics: Kafka, Kinesis, Pub/Sub, NATS, Pulsar](#13-per-engine)
14. [Streaming SLOs](#14-slos)
15. [Anti-patterns](#15-anti-patterns)
16. [Worked example: a Kafka consumer with full observability](#16-worked-example)
17. [Pitfalls](#17-pitfalls)
18. [Mental models](#18-mental-models)

---

## 1. Thesis

Three claims:

1. **Lag is the dominant streaming SLI.** Not request latency, not error rate. Lag — the distance between produced and consumed offsets — captures whether your pipeline is keeping up with reality.
2. **Async traces are real but require explicit instrumentation.** Without traceparent propagation through message headers, the trace breaks at every queue boundary. Debugging cross-service async flows becomes archaeology.
3. **Streaming SLOs use throughput-based SLIs, not request-based.** "99.9% of events processed within 60 seconds of production" — the freshness pattern from `doc 13 §2.1`.

If your team runs Kafka and the only dashboard is "broker CPU and disk," you're missing every operational signal that matters. This chapter is the right shape.

---

## 2. Why Streaming Is Different

Streaming flips the assumptions of request-response observability.

| Dimension | Request-response | Streaming |
|---|---|---|
| Failure visibility | Client sees the failure | Producer fires-and-forgets; consumer alone sees |
| Latency definition | Request arrival → response | Production → consumption (could be ms or hours) |
| Backpressure | Client retries / fails | Lag accumulates; consumer falls behind |
| Per-call SLI | Easy: success / fail | Harder: "is the pipeline keeping up?" |
| Tracing | Sync call propagation | Must propagate via message headers |
| Reordering | Rare | Common (per-partition ordering only) |
| Replay | Hard (state lost) | Easy (offset rewind) |

These differences shape every observability decision.

---

## 3. The Four Streaming Signals

| Signal | Source | Question |
|---|---|---|
| **Lag** | Consumer offset vs producer offset | Is the consumer keeping up? |
| **Throughput** | Messages / bytes per second | What's flowing? |
| **Error rate** | Consumer / producer errors | Is processing failing? |
| **State** | Broker / cluster health | Is the substrate healthy? |

Plus per-message *latency* derived from end-to-end traces (§8) and *DLQ depth* (§10) for failed messages.

---

## 4. Consumer Lag: The Dominant SLI

### 4.1 What it is

```
lag = producer_latest_offset - consumer_committed_offset
```

Per consumer group, per topic, per partition. Expressed in:
- **Messages** (e.g., 12,000 behind).
- **Bytes** (storage consumed by un-consumed messages).
- **Time** (e.g., consumer is 45 seconds behind real-time).

### 4.2 Time-based lag is the most useful

Time-based lag answers the SLO question directly:

```yaml
- name: events_processed_within_30s
  metric: kafka_consumer_lag_seconds < 30
  target: 0.999
```

Compute it as: `time_now - timestamp_of_committed_offset`. Most modern Kafka exporters do this for you (Burrow, kafka-lag-exporter).

### 4.3 The lag chart

The standard streaming dashboard:

```
Lag over time
                                     ▲
12K ┤                                 ╱│
    │                                ╱ │
    │                           ╱╲╱╲╱  │  consumer falling
8K  ┤                          ╱       │  behind
    │                       ╱╲╱
    │                    ╱╲╱
4K  ┤              ____╱
    │      ───────────────────────────  steady state ~ 200
0   ┴───────────────────────────────────────►
                                       time
```

Steady-state lag of N messages is normal (in-flight buffer). Sustained increase is the leading-indicator signal.

### 4.4 The "lag never recovers" failure mode

Consumer is sized for steady-state RPS. A spike pushes lag up; consumer can't catch up; lag grows unboundedly. The fix: scale consumer (more partitions / more consumer instances) or accept temporary backlog.

Alert pattern:

```promql
# Page if lag > 10× normal AND growing
(kafka_consumer_lag > 10 * avg_over_time(kafka_consumer_lag[7d]))
  and
(deriv(kafka_consumer_lag[10m]) > 0)
```

### 4.5 Lag exporters

Tools:
- **Burrow** (LinkedIn's classic; per-consumer-group lag analyzer).
- **kafka-lag-exporter** (Lightbend; clean Prometheus).
- **kminion** (CloudHut; modern alternative).
- **Datadog / Confluent integration** (managed).

All emit `kafka_consumergroup_lag_messages` and similar.

---

## 5. Producer Signals

The other half.

### 5.1 What to observe

```
kafka_producer_messages_sent_total{topic, ack_status}
kafka_producer_bytes_sent_total{topic}
kafka_producer_request_latency_ms_bucket{topic}
kafka_producer_record_errors_total{error_type}
kafka_producer_buffer_bytes_used / buffer_total   # saturation
kafka_producer_record_send_rate
kafka_producer_batch_size_avg
```

### 5.2 The buffer saturation signal

Producer buffers messages before sending in batches. If buffer fills (broker slow, network slow), producer either blocks or drops.

```
buffer_saturation > 0.8 → backpressure imminent
```

### 5.3 The "produce ack" SLO

```yaml
- name: producer_ack_under_50ms
  metric: kafka_producer_request_latency_ms_bucket{le="50"}
  target: 0.99
```

The producer's effective latency. Different from consumer-side latency.

### 5.4 The retry storm pattern

Failed produces retry. Retries amplify load. If retries are unbounded, broker pressure grows unboundedly.

Defense:
- Bounded retry attempts.
- Exponential backoff.
- Circuit breaker on the producer.
- Alert on retry rate.

```
kafka_producer_record_retries_total / kafka_producer_messages_sent_total
```

> 5% retry rate = brokerside problem; investigate.

---

## 6. Partition Skew

The most overlooked Kafka pathology.

### 6.1 The pathology

Kafka partitions are independent. A consumer group has one consumer per partition (max). If keys hash unevenly, one partition gets disproportionate traffic; one consumer is overloaded; lag accumulates *only on that partition*.

The aggregate "consumer group lag" looks fine if averaged. The per-partition view shows the truth.

### 6.2 The signal

```
sum by (partition) (kafka_consumer_lag_messages{topic="orders"})
```

Healthy: each partition has roughly equal lag. Skewed: one partition has 10-100× the lag of others.

### 6.3 The causes

- **Bad partition key.** If `customer_id` hashes unevenly (e.g., one whale customer dominates), that partition is hot.
- **Sequential keys.** A timestamp-based key sends consecutive messages to the same partition.
- **Cardinality of keys < partitions.** With 10 keys and 100 partitions, only 10 partitions ever get messages.

### 6.4 The fix

- Re-key with a higher-cardinality field.
- Re-partition (manual offset migration).
- Compound keys.
- For unbounded skew: consider per-key sticky consumers with backpressure.

The skew dashboard is mandatory:

```
Per-partition lag heatmap, top 20 partitions by lag
```

---

## 7. Broker / Cluster Signals

The substrate.

### 7.1 What to observe

```
kafka_broker_topic_partitions_under_replicated_total    # ISR shrinks
kafka_broker_topic_partitions_count
kafka_broker_request_handler_idle_pct                   # CPU at the broker
kafka_broker_network_request_rate
kafka_broker_log_size_bytes
kafka_broker_isr_shrinks_per_sec
kafka_broker_offline_partitions_count                   # !=0 is bad
```

Cluster-wide:
- Total messages/sec.
- Total bytes/sec.
- Number of partitions.
- Number of consumer groups.
- Controller leader count (should equal 1).

### 7.2 The under-replicated signal

`kafka_broker_topic_partitions_under_replicated_total > 0` = the cluster has lost replicas; durability degraded. Often precedes broker failure.

Alert immediately. This is a SEV-2 trigger.

### 7.3 The controller signal

Kafka has one controller across the cluster. Loss of controller → cluster fails to handle metadata changes. Alert on "controllers != 1."

### 7.4 The "ISR shrinking" pattern

ISR (In-Sync Replicas) shrinks when a replica falls behind the leader. Causes: slow disk, network, broker GC pause.

```
kafka_broker_isr_shrinks_per_sec > 0
```

Persistent ISR shrinkage = capacity / hardware issue.

---

## 8. Tracing Across Async Boundaries

The hardest streaming observability problem.

### 8.1 The problem

A request:
1. Synchronously processed by checkout service.
2. Publishes "order.created" to Kafka.
3. Consumed by inventory service (immediately).
4. Consumed by analytics service (5 minutes later).
5. Triggers notification service via downstream Kafka.

Without explicit propagation, the trace ends at step 2. Steps 3-5 are *new traces*, disconnected from the original. End-to-end debugging becomes impossible.

### 8.2 The solution: propagate via headers

```python
# Producer side
with tracer.start_as_current_span("publish.order.created") as span:
    headers = {}
    inject(headers)  # OTel injects traceparent into headers
    producer.send("order.created", value=msg, headers=list(headers.items()))

# Consumer side
def on_message(msg):
    headers = dict(msg.headers)
    ctx = extract(headers)
    with tracer.start_as_current_span("consume.order.created", context=ctx) as span:
        process(msg)
```

Now the consumer's span is a *child* of the producer's span, even across the queue. The trace shows the queue as a long edge.

### 8.3 The OTel messaging semantic conventions

OTel standardizes attribute names:

```
messaging.system           = "kafka"
messaging.destination.name = "order.created"
messaging.operation        = "publish" | "receive" | "process"
messaging.kafka.partition.number
messaging.kafka.message.offset
messaging.kafka.consumer.group
```

Use these. They're recognized by Tempo, Jaeger, Honeycomb, Datadog — and they enable per-topic / per-partition trace search.

### 8.4 The auto-instrumentation

OTel auto-instruments common Kafka clients:
- `confluent-kafka-python`, `kafka-python` (Python).
- `sarama`, `confluent-kafka-go` (Go).
- `kafka-clients` (Java).
- `kafkajs` (Node).

Drop in OTel; producer/consumer spans appear automatically. Header propagation handled.

### 8.5 The fan-out problem

One producer message → 5 consumers. One trace, 5 child spans? Or 5 separate traces with `links` to the original?

OTel's *trace links* model: each consumer's span has a `link` to the producer's span, but is a new trace root. Lets you query "all traces that originated from this trace" without one trace becoming massive.

Most teams in 2026 use linked traces (manageable) over giant traces (unmaintainable).

---

## 9. Exactly-Once and Idempotency Observability

The hardest semantic question.

### 9.1 The semantics

- **At-most-once:** message may be lost.
- **At-least-once:** message may be duplicated.
- **Exactly-once:** message processed exactly once.

Exactly-once is hard; usually requires transactional producers + idempotent consumers + careful broker config.

### 9.2 The signals

```
kafka_consumer_idempotent_duplicates_total          # consumer-detected dupes
kafka_producer_transactional_aborts_total
kafka_producer_transactional_commits_total
kafka_consumer_offset_commit_failures_total
```

For idempotent consumers (the more common pattern), instrument:

```
duplicates_total{topic}        # detected and skipped
processed_total{topic}
```

Ratio = how often duplicates flow. Useful for capacity (idempotency check overhead) and correctness.

### 9.3 The transactional-producer signal

Aborts/commits ratio: aborts > 1% means transactional logic is failing — investigate the data flow.

### 9.4 The offset-commit signal

If the consumer fails to commit offsets, on restart it reprocesses messages. Idempotent consumers handle this; non-idempotent ones double-process. Alert on `offset_commit_failures_total > 0`.

---

## 10. Dead-Letter Queues and Poison Messages

The failure-handling layer.

### 10.1 The DLQ pattern

Messages that fail processing N times are moved to a *dead-letter queue* for human review.

```
                      ┌──────┐
       in-topic   →  │ Cons │ →  out
                      └──┬───┘
                         │ on failure
                         ▼
                      ┌──────┐
                      │ DLQ  │  ← humans/auto-recovery
                      └──────┘
```

### 10.2 The signal

```
dlq_messages_total{source_topic}            # rate
dlq_depth{source_topic}                     # gauge: how many waiting
dlq_age_seconds_oldest{source_topic}        # how stale
```

### 10.3 The SLO

```yaml
- name: dlq_processed_within_24h
  metric: dlq_message_age_seconds < 86400
  target: 0.95
```

DLQ messages must be addressed; otherwise they accumulate as silent failures.

### 10.4 The "DLQ is bottomless" anti-pattern

Common: messages flow into DLQ; nobody monitors; nobody clears. After a year, millions of messages in the DLQ — and nobody knows what they mean.

Fix:
- DLQ depth alert.
- DLQ-age alert.
- Quarterly DLQ review.
- Auto-replay tooling for known-fixed errors.

---

## 11. Schema Registry and Compatibility Signals

For Kafka with structured data (Avro, Protobuf, JSON Schema), a schema registry gates compatibility.

### 11.1 The risks

- Producer publishes a schema change incompatible with consumers.
- Consumer fails to deserialize; either drops messages or crashes.

### 11.2 The signals

```
schema_registry_compatibility_check_failures_total
schema_registry_schema_count
deserialize_errors_total{topic}
```

### 11.3 The CI gate

Schema changes go through CI:
- New schema tested against last N consumer schemas for compatibility.
- Block merge if breaking change.

This is a release-engineering concern; observability surfaces escapes.

### 11.4 The deserialize-error surge

A surge in deserialize errors = breaking change shipped. Alert on it; rollback path needed.

---

## 12. Stream-Processing Engines

Beyond raw Kafka: Flink, Kafka Streams, Spark Streaming.

### 12.1 What's different

Stream processors maintain *state* (windowed aggregates, joins, materialized views). New observability concerns:

- **State size.** Memory + disk used by the stateful operator.
- **Checkpoint latency.** Time to persist state for fault tolerance.
- **Watermark progress.** Event-time progress; lag in event time vs processing time.
- **Operator-level metrics.** Per-operator throughput, latency, errors.

### 12.2 Per-engine

| Engine | Telemetry |
|---|---|
| **Flink** | Native metrics + Prometheus; very rich operator-level signals |
| **Kafka Streams** | JMX → Prometheus; via micrometer or kafka-streams-metrics |
| **Spark Streaming** | Spark UI metrics + Prometheus exporter |
| **Beam** | Runner-specific (Dataflow, Flink, Spark) |
| **Kinesis Analytics / Managed Flink** | CloudWatch native |

### 12.3 The watermark signal

Watermark = "we've seen all events up to this event-time." If watermark stalls, downstream windowed aggregates won't close. Major data freshness issue.

```
flink_operator_watermark_seconds_lag
```

Page on watermark stall.

---

## 13. Per-Engine Specifics

### 13.1 Apache Kafka

- JMX-based metrics; jmx_exporter for Prometheus.
- Burrow / kafka-lag-exporter / kminion for consumer-side.
- Cruise Control for cluster balancing + metrics.
- Apache Kafka 3.x with KRaft (no ZooKeeper) is the 2026 norm.

### 13.2 Confluent Cloud

- Managed; native metrics via Confluent Cloud Metrics API.
- Lag, throughput, errors all exposed.
- Less control; less ops burden.

### 13.3 Amazon Kinesis

- CloudWatch-native metrics.
- IteratorAge: equivalent of consumer lag (per shard).
- Per-shard records-in/out.
- The shard model differs from Kafka partitions but shapes the same observability.

### 13.4 GCP Pub/Sub

- StackDriver-native metrics.
- subscription/oldest_unacked_message_age (the lag equivalent).
- num_undelivered_messages.

### 13.5 NATS JetStream

- Built-in metrics endpoint.
- Stream / consumer state via API.
- Lighter than Kafka; less mature observability ecosystem.

### 13.6 Apache Pulsar

- Per-topic, per-subscription metrics.
- BookKeeper (storage) metrics.
- Pulsar Manager UI.
- Hierarchical topic structure (tenants / namespaces / topics) → tenant-aware observability natively.

---

## 14. Streaming SLOs

The right SLI shapes.

### 14.1 Freshness SLO

```yaml
- name: order_created_processed_within_30s
  metric: time_from_produce_to_consume_seconds_bucket{le="30"}
  target: 0.999
```

### 14.2 Throughput SLO

```yaml
- name: pipeline_throughput
  metric: messages_processed_per_sec
  target: > 1000  # static lower bound
```

### 14.3 Error / DLQ rate

```yaml
- name: dlq_rate
  metric: dlq_messages / total_messages
  target: 0.001  # < 0.1% of messages fail
```

### 14.4 Watermark progress (stream-processing)

```yaml
- name: watermark_freshness
  metric: watermark_lag_seconds < 60
  target: 0.999
```

The four SLO shapes cover the bulk of streaming-pipeline guarantees.

---

## 15. Anti-Patterns

1. **No lag SLO.** Pipeline degradation invisible until customers notice.
2. **Aggregated lag only.** Partition skew hidden.
3. **No header propagation.** Async traces broken.
4. **No DLQ monitoring.** Failures accumulate silently.
5. **No schema-compat CI.** Breaking changes deployed.
6. **Producer buffer saturation untracked.** Backpressure surprises.
7. **No per-partition view.** Skew unseen.
8. **No watermark monitoring (stream-processing).** Silent stall of windowed ops.
9. **No retry backoff.** Retry storms cascade.
10. **Idempotency duplicates uncounted.** Processing volume mysterious.
11. **No cluster-level signals.** Broker outages surprise.
12. **Tracing without OTel messaging conventions.** Custom attributes; lost interop.
13. **No cross-fanout linking.** Multi-consumer flows untraced.
14. **No DLQ replay tooling.** Failed messages permanently stuck.
15. **No throughput baselines.** Anomalies undetected.

---

## 16. Worked Example: A Kafka Consumer with Full Observability

Concrete and complete.

### 16.1 The consumer

`order-processor`. Consumes `order.created` topic; performs idempotent processing; publishes `order.processed`. Failures route to DLQ.

### 16.2 The instrumentation

OTel auto-instruments the consumer client. Manually instrument:

```python
@app.on_message("order.created")
def handle(msg):
    with tracer.start_as_current_span(
        "process.order.created",
        attributes={
            "messaging.system": "kafka",
            "messaging.destination.name": "order.created",
            "messaging.kafka.partition.number": msg.partition,
            "messaging.kafka.message.offset": msg.offset,
            "order.id": msg.value.id,
        }
    ) as span:
        if already_processed(msg.value.id):
            duplicates_total.inc()
            return
        try:
            result = process(msg.value)
            mark_processed(msg.value.id)
            producer.send("order.processed", result, traceparent=current_traceparent())
            messages_processed_total.inc()
        except Exception as e:
            span.record_exception(e)
            failures_total.inc()
            if msg.attempt_count >= 3:
                dlq.send(msg)
            else:
                raise  # retry
```

### 16.3 The metrics

```
kafka_consumer_lag_seconds{topic="order.created", group="order-processor"}
messages_processed_total{topic="order.created"}
duplicates_total
failures_total
dlq_messages_total
processing_duration_seconds_bucket
```

### 16.4 Dashboards

- Lag over time (per partition + aggregate).
- Throughput (messages/sec, bytes/sec).
- Error rate / DLQ rate.
- Processing latency p50/p95/p99.
- Partition skew heatmap.
- DLQ depth and age.

### 16.5 Alerts

- Lag > 60s for 5 min: page.
- DLQ depth growing > N/min: page.
- Processing latency p99 > 1s: ticket.
- Duplicate rate > 5%: ticket (idempotency degrading).
- Schema deserialize errors > 0: page (deploy regression).

### 16.6 SLOs

```yaml
- name: order_processed_within_30s
  target: 0.999
- name: order_dlq_rate
  target: 0.001
```

### 16.7 The result

When the pipeline degrades:
- Lag SLO burns → page.
- Trace shows the slow span (e.g., DB write was slow).
- Per-partition lag identifies a hot key.
- DLQ shows specific failure modes.
- End-to-end debugging in minutes, not hours.

---

## 17. Pitfalls

1. **Lag observed only in aggregate.** Skew invisible.
2. **No async tracing.** Cross-service flows opaque.
3. **No DLQ alerting.** Silent failure accumulation.
4. **Schema changes without CI.** Breakage in prod.
5. **No idempotency observability.** Duplicate-processing misattributed.
6. **Producer back-pressure unobserved.** Surprises.
7. **No watermark monitoring.** Stream stalls go quiet.
8. **No partition rebalancing visibility.** Hot partitions persist.
9. **No SLO on freshness.** Pipeline regression slow to detect.
10. **DLQ never reviewed.** Years-old failed messages.
11. **No producer retry-rate alert.** Storms cascade.
12. **No cluster-level observability.** Broker issues go undetected.
13. **No multi-consumer fan-out tracing.** Multi-consumer flows opaque.
14. **No replay tooling.** Failed-message recovery manual / impossible.
15. **No Confluent / Kinesis / Pub/Sub specifics.** Cloud-managed nuances missed.

---

## 18. Mental Models

> **Lag is the dominant streaming SLI. Time-based lag, expressed as a freshness SLO.**

> **Per-partition view always. Aggregate lag hides skew.**

> **OTel messaging conventions + header propagation = end-to-end async tracing.**

> **DLQ has SLOs too. Don't let it accumulate silently.**

> **Schema compatibility is a CI concern; observability surfaces escapes.**

> **Throughput, lag, error, state — the four signal classes.**

> **Idempotency makes at-least-once safe. Observability counts the duplicates.**

> **Watermarks for stream processing; the equivalent of lag for event time.**

> **Producer buffer saturation precedes backpressure; alert on it.**

> **Streaming and request-response are different. Reuse the SLO discipline; don't reuse the SLI shapes.**

Now go to `doc 26` (LLM and AI observability) — the 2026 frontier of observing models, tokens, and inference costs.
