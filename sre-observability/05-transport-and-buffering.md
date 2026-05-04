# 05 — Transport & Buffering

> Between the collector (chapter 04) and the storage tier (chapters 06–09) sits a queue. Not because it's fashionable, but because the moment you have more than one storage backend, more than one tenant, or more than one region, the alternative — direct push to storage — becomes the worst kind of fragile: it works in staging and bursts at 03:14 on a Sunday. This chapter is the queue layer: when to add it, what shape, what it costs, and what fails.

If you have a single storage backend, a single team, a single region, and < 5k events/sec, **skip the queue and re-read this in twelve months**. §11 has the rubric. Everyone else: read on.

---

## 1. Why a Queue at All

The collector tier already batches, retries, and buffers on disk. The storage tier already has its own ingest API, with its own backpressure and retry semantics. Why insert a third system between them?

### 1.1 The "direct-to-storage" anti-pattern

```
                                      ┌─────────────┐
              ┌──────────────────────▶│  Mimir      │
              │                       └─────────────┘
              │                       ┌─────────────┐
   collector ─┼──────────────────────▶│  Loki       │
              │                       └─────────────┘
              │                       ┌─────────────┐
              └──────────────────────▶│  Tempo      │
                                      └─────────────┘
```

This works at small scale. It looks elegant. It has three production failure modes that the queue removes:

1. **Storage outage propagates to producers.** If Mimir's distributor is at 100% rejection (say, ingester rolled over and lost its ring), the collector's `remote_write` queue fills up, the collector's memory grows, and once the disk-backed buffer fills (5 GB? 50 GB?), the collector either drops new data or back-pressures into the application via gRPC flow control. *Your production pods now have a memory regression because Loki is sad.* This is the canonical "telemetry took down the service" outage.
2. **Replay is impossible.** Storage rejected 30 minutes of data because of a bad relabel rule? It's gone. With a queue and a retention window, you fix the consumer, reset the offset, and replay.
3. **Fan-out is N×M.** Each collector has to know about every backend. Adding a new storage backend (an S3 archive, a ClickHouse lakehouse, a vendor for trial) means redeploying every collector.

### 1.2 What the queue buys you

| Property | Without queue | With queue |
|---|---|---|
| Storage outage tolerance | Producer-side memory growth, eventually drop | Brokers absorb hours of data; consumer catches up |
| Replay after schema/relabel fix | Impossible | Reset consumer group offset |
| Fan-out (one log → Loki + ClickHouse + S3) | N writes from collector, N retry policies | Single write, N consumer groups |
| Schema evolution safety | Collector knows N backend schemas | Producer writes one schema; consumers translate |
| Multi-tenant isolation | Per-collector, fragile | Tenant-aware partitioning + broker quotas |
| Ingest cost smoothing during traffic spike | Storage must be sized for peak | Storage sized for *p95*, queue absorbs peak |
| Cross-region routing | Collector → cross-region storage (latency, $$) | Collector → local broker → MirrorMaker to remote |

The queue is the *decoupling boundary*. Everything upstream of it is the *event production* problem; everything downstream is the *event consumption* problem. Once they're separated, you can change either side without involving the other.

### 1.3 The tipping point

You don't always need it. The break-even is roughly:

```
Add a queue when ANY of:
  - You have >1 storage backend for the same signal stream
  - You have >1 region producing telemetry for >1 region storing it
  - Sustained ingest >50k events/sec (any signal)
  - You serve >3 internal tenants and need per-tenant isolation
  - You need cold-tier replay (lakehouse pattern, §7)
  - Your storage tier has had a >15 min outage in the last 90 days
                                          ^ telemetry of telemetry
```

Below those thresholds, a single OTel Collector gateway with a sized disk-backed exporter queue is simpler and cheaper. The queue introduces three new failure modes (broker disk full, consumer lag, partition skew) that are not free.

> **Mental model.** The queue exists so that the producer's tail latency does not depend on the storage tier's tail availability. Decoupling = the producer commits to "I delivered to the broker"; the broker commits to "I'll hold this until a consumer takes it."

---

## 2. Kafka Deep Dive for Telemetry

Kafka is the default transport for self-hosted observability stacks of meaningful scale. It's not the only choice (§3) but it's the one you'll find most often in production, and the one whose mental model the others borrow from.

### 2.1 Topic, partition, consumer group — the telemetry mapping

```
   Producers                   Broker cluster                    Consumers
   (collectors)                 (3 brokers)                      (storage tier)

   ┌──────────┐           ┌─────────────────────┐          ┌──────────────────┐
   │ otel-col │──────────▶│  topic: otlp-traces │─────────▶│ Tempo distributor│
   │   gw-1   │           │   partitions: 0..47 │          │  group: tempo    │
   └──────────┘           │   replication: 3    │          └──────────────────┘
                          │                     │          ┌──────────────────┐
   ┌──────────┐           │                     │─────────▶│ S3 sink (Connect)│
   │ otel-col │──────────▶│                     │          │  group: s3-cold  │
   │   gw-2   │           └─────────────────────┘          └──────────────────┘
   └──────────┘
```

A few definitions that matter in this context:

- **Topic**: the named log. For telemetry, you nearly always want at least one topic per signal type — `otlp-traces`, `otlp-metrics`, `otlp-logs`, plus optionally `otlp-logs-audit` for differently-retained classes. **Do not** mix signals in one topic; their throughputs, retention, and consumer rates differ by orders of magnitude.
- **Partition**: the unit of parallelism. A partition is consumed by exactly one consumer in a group at a time. Order is preserved within a partition, never across partitions.
- **Consumer group**: a set of consumers that *share* the work of consuming a topic. Each partition is owned by one member of the group. Add a second group to fan-out the same data to a second destination.
- **Offset**: per-partition, per-group cursor. Replay = "set the offset back."

### 2.2 Partitioning key — the most consequential choice

The partition is computed as `hash(key) mod num_partitions`. The key choice decides:

1. Which records go to the same partition (and so are *ordered* relative to one another).
2. How evenly load is spread (skew kills you).
3. Whether downstream stateful consumers can do their job.

| Signal | Bad key | Good key | Why |
|---|---|---|---|
| Traces | `service.name` | `trace_id` | Tail sampling assembles a trace; all spans of one trace must land in the same partition so one consumer (the tail sampler / Tempo distributor with the trace ID load-balancing pattern) sees them all. Keying by service splits a single trace across partitions; you'll never assemble it. |
| Metrics | `metric_name` | `tenant_id` (+ `service` for sub-spreading) | Metric names are extremely skewed (one metric is 90% of volume). Tenant ID is the real isolation boundary. |
| Logs | none (round-robin) | `tenant_id` | Same logic as metrics; per-tenant ordering is usually all you need, and it lets you apply per-tenant quotas at the consumer. |
| Logs (very high volume) | `tenant_id` only | `tenant_id + service.name` | If one tenant dominates, hash on (tenant, service) so a hot tenant spreads across partitions. |

**The trace_id rule is non-negotiable.** It pairs with the *loadbalancing exporter* pattern in the OTel Collector (chapter 04, §6.4): the collector gateway routes spans to a downstream collector based on `trace_id`, so tail sampling sees the whole trace. Kafka partitioning by `trace_id` extends that guarantee across the durability boundary. Partition by anything else and your tail sampler is broken; you just don't know yet.

> **Pitfall.** Default OTLP Kafka exporter behavior is to round-robin partition without a key. For traces this silently breaks tail sampling. Always set `partition_traces_by_id: true` (OTel Collector kafkaexporter ≥ v0.85).

### 2.3 Retention: time vs size, and tiered storage

```
log.retention.hours = 72            # 3 days, fine for hot replay
log.retention.bytes = -1            # disabled (use time only)
                                    # OR cap per-partition disk usage
log.segment.bytes   = 1073741824    # 1 GiB segments
log.cleanup.policy  = delete         # not 'compact' — telemetry is append-only
```

Retention rules of thumb for telemetry:

| Use case | Retention | Why |
|---|---|---|
| "Bridge a storage outage" | 24–72h | Long enough for a Sunday-night incident to be diagnosed and replayed Monday morning |
| "Replay a schema change" | 7 days | One full release cycle |
| "Backfill a new index" | 14–30 days | Need to rebuild a downstream index from scratch |
| "Cold archive" | months–years | Use **tiered storage**, not local disk |

**Tiered storage** (Confluent Cloud, Apache Kafka 3.6+ with `remote.log.storage.system.enable=true`, Redpanda Tiered Storage) keeps hot segments on broker SSD and offloads cold segments to S3/GCS. For a telemetry topic with 30-day retention, the local broker disk holds maybe 6–12 hours; the rest is on object storage at ~$0.023/GB-mo. This is the only economical way to keep a multi-week replay window for a high-volume topic.

Without tiered storage, a 10 MB/s topic with 30-day retention is `10 MB/s × 86400 × 30 × 3 (RF) = ~75 TB` on broker SSD. With tiered storage, the brokers hold a few hundred GB; S3 holds the rest.

### 2.4 Compression: zstd, lz4, snappy

| Codec | Ratio (OTLP protobuf) | CPU cost producer | CPU cost consumer | Use when |
|---|---|---|---|---|
| `none` | 1.0× | 0 | 0 | Don't. You'll regret the network bill. |
| `snappy` | 2.5–3× | low | low | Legacy, default-ish. Outclassed by lz4. |
| `lz4` | 3–4× | low | low | Hot path, latency-sensitive |
| `zstd` (level 3) | 5–7× | medium | medium | High-volume topics where bandwidth or storage > CPU |
| `zstd` (level 9+) | 7–10× | high | medium | Cold-tier-bound topics (tiered storage), batch consumers |

For OTLP protobuf payloads, **zstd level 3** is the right default for high-volume topics; it cuts 60% off both broker disk and inter-broker replication bandwidth at modest producer CPU cost. For latency-sensitive paths (real-time alerting derived from traces), `lz4` keeps end-to-end latency lower.

Compression should happen at the *producer*, end-to-end. Configure `compression.type=zstd` on the topic so producers that didn't set it get coerced.

### 2.5 Durability: replication factor, acks, ISR

The classic telemetry setting:

```
# topic config
replication.factor      = 3
min.insync.replicas     = 2

# producer config
acks                    = all
enable.idempotence      = true
max.in.flight.requests.per.connection = 5
retries                 = 2147483647   # effectively infinite
delivery.timeout.ms     = 120000
```

This is the "lose no data on a single broker failure, tolerate one broker out for maintenance" configuration. Trade-offs:

- `acks=all` + `min.insync.replicas=2` means a write succeeds only when 2 of 3 replicas have it. If two brokers are down, *producers block*. This is correct for billing/audit logs, often overkill for raw telemetry.
- `acks=1` (leader only) doubles producer throughput, but a leader failure between ack and replication loses up to a few seconds of data. **Acceptable for metrics and traces** in most stacks; almost never for audit logs.
- `acks=0` (fire and forget) is for traffic where loss is fine and latency must be sub-millisecond. Don't use this for telemetry.

A defensible default per signal:

| Signal | acks | min.insync.replicas | Rationale |
|---|---|---|---|
| Metrics (sampled) | `1` | 1 | Loss of a few seconds is invisible in 15s scrapes |
| Traces | `1` | 1 | Loss is acceptable; tail sampling drops 95% anyway |
| Logs (app/debug) | `1` | 1 | Best-effort; volume is high |
| Logs (audit/security) | `all` | 2 | Compliance demands no loss |
| Billing events | `all` | 2 | Self-explanatory |

`enable.idempotence=true` is free (since Kafka 3.0 it's the default) and prevents duplicate writes within a producer session. Always on.

### 2.6 Producer batching: linger.ms and batch.size

```
batch.size       = 1048576    # 1 MiB
linger.ms        = 50          # wait up to 50 ms to fill a batch
buffer.memory    = 67108864   # 64 MiB producer-side buffer
```

The producer accumulates records into a per-partition batch until either `batch.size` is full or `linger.ms` elapses. Larger batches → fewer requests → better throughput and better compression. The cost is end-to-end latency.

For telemetry, **50–100 ms `linger.ms` is fine** — by the time a span reaches the OTel collector, it's already been buffered; another 50 ms before going to Kafka is invisible. Crank `batch.size` to 1 MiB. The combination roughly doubles throughput on a busy collector vs Kafka defaults.

### 2.7 Consumer lag: the platform SLI

Consumer lag = current end-of-log offset − current consumer offset, per partition. Sum across partitions for a topic-group lag.

```
kafka_consumer_group_lag{
   group="tempo-ingester",
   topic="otlp-traces",
   partition="17"
}
```

Track three things:

1. **Absolute lag in records.** Useful for "how many spans are not in Tempo right now?"
2. **Lag in seconds** (estimate via rate): `lag_records / produce_rate_records_per_sec`. This is what humans want to see — "we're 90 seconds behind."
3. **Lag relative to retention.** If lag-in-time approaches retention-in-time, you're about to lose data. **This is the page-worthy alert**:

```
ALERT KafkaConsumerLagApproachingRetention
  expr: kafka_consumer_group_lag_seconds{group=~"tempo|loki|mimir.*"} 
        > on(topic) (kafka_topic_retention_seconds * 0.5)
  for: 10m
```

Half the retention window is a reasonable trip-wire — you have the other half as headroom to fix the consumer before any data is lost.

`kafka_lag_exporter`, `cruise-control`, or Burrow are the standard ways to expose lag as a Prometheus metric. Confluent's `kafka-consumer-groups.sh --describe` is the manual equivalent.

---

## 3. Cloud-Native Alternatives

If you don't want to run Kafka, the managed options divide into "Kafka API-compatible" and "everything else."

### 3.1 Comparison table

| System | Model | Ordering | Throughput unit | Retention | Replay | Notes |
|---|---|---|---|---|---|---|
| **Apache Kafka** (self-host) | Topic + partitions, pull | Per-partition | Partitions × broker | Time/size, tiered (3.6+) | Native (offsets) | The reference; you run it |
| **Confluent Cloud** | Same as above, managed | Per-partition | Same | Tiered to S3 included | Native | + Schema Registry, ksqlDB |
| **Redpanda** | Kafka API, no JVM, no ZK | Per-partition | Cores per broker | Time/size, tiered | Native | C++ thread-per-core; 2–4× lower p99 latency |
| **AWS Kinesis Data Streams** | Shards, pull (KCL) | Per-shard | Shard (1MB/s in, 2MB/s out) | 24h–365d | Native via shard iterator | On-demand or provisioned; no native consumer groups (KCL coordinates via DynamoDB) |
| **AWS Kinesis Firehose** | Stream → destination, push | None guaranteed | Auto | Ephemeral | None | Use when destination is S3/Redshift/OS and you don't need replay |
| **GCP Pub/Sub** | Topic + subscription, push or pull | Per ordering key (opt-in) | Auto | 7d default, up to 31d | Replay by snapshot/timestamp | Exactly-once delivery on pull subs, push tier separate |
| **Azure Event Hubs** | Hub + partitions, pull | Per-partition | Throughput Units / PUs | 1d–7d (Standard), up to 90d (Dedicated) | Native (offsets) | Kafka-API-compatible mode available |
| **NATS JetStream** | Stream + consumers, pull or push | Per-stream | File or memory | Time/size | Native | Lightweight; lower throughput than Kafka |
| **Pulsar** | Topic + subscriptions, pull | Per-subscription type | Brokers + bookies | Time/size, tiered native | Native | Decoupled storage (BookKeeper); good for multi-tenant |

### 3.2 Kinesis-specific gotchas

Kinesis Data Streams uses **shards** instead of partitions. Each shard does 1 MB/s in, 2 MB/s out, 1000 records/s in. Partition key hashes into shards exactly like Kafka. The KCL (Kinesis Client Library) coordinates consumers via a DynamoDB table that stores offsets and shard ownership.

Two operational surprises:
- **Shard count changes** require explicit `MergeShards` / `SplitShard` API calls. On-demand mode automates this but at higher cost.
- **No exact equivalent of Kafka's `min.insync.replicas`** — durability is "in 3 AZs synchronously" with no producer-visible knob.

**Kinesis Firehose** is a different product: a managed pipeline that buffers and writes to S3/Redshift/OpenSearch/Splunk. It's what you use when the destination is S3 and you don't need replay or multiple consumers. For an OTel → S3 cold path, Firehose is operationally simpler than running Kafka Connect; for OTel → Mimir + Loki + Tempo, Firehose can't fan out the right way and you want Streams.

### 3.3 Pub/Sub-specific gotchas

GCP Pub/Sub doesn't expose partitions. Ordering is **opt-in** via "ordering keys" — set an ordering key on a message, and messages with the same key are delivered in order to the same subscriber. **Without an ordering key, delivery is unordered.** For trace_id-based grouping, set the ordering key to `trace_id`.

Exactly-once delivery is available on **pull subscriptions** with `enableExactlyOnceDelivery=true`. It uses ack deadlines + dedup tracking server-side. The catch: it doesn't work on push subscriptions, and the consumer must explicitly ack to consume the next message in an ordering key (head-of-line blocking).

```yaml
# Pub/Sub subscription with ordering + exactly-once
subscription:
  name: otlp-traces-tempo
  topic: otlp-traces
  enable_message_ordering: true
  enable_exactly_once_delivery: true
  ack_deadline_seconds: 60
  retain_acked_messages: false
  message_retention_duration: 604800s  # 7 days
```

### 3.4 Redpanda

Redpanda is Kafka-wire-compatible (same client SDKs, same admin API) but reimplemented in C++ with thread-per-core, no JVM, no ZooKeeper, no KRaft (it uses Raft directly on each partition). The practical wins:

- **2–4× lower p99 produce/consume latency** in independent benchmarks (and matched in our experience for telemetry workloads).
- **No JVM tuning.** No GC pauses. No heap sizing arguments.
- **Single binary deploy.** Operationally simpler.

The trade-off is a smaller ecosystem (no Confluent Schema Registry integration native, though Apicurio works; no Confluent Connect catalog though Kafka Connect runs against it). For a telemetry pipeline, the ecosystem mismatch rarely bites — the protocols are the same.

---

## 4. Schema and Serialization on the Wire

### 4.1 OTLP protobuf — the de-facto telemetry wire format

OpenTelemetry defines protobuf schemas for traces, metrics, logs, and profiles. These are stable, versioned, and what every modern SDK and collector emits. **Send OTLP/protobuf over Kafka by default.** 

Concretely: the OTel Collector kafkaexporter writes OTLP-protobuf-encoded messages to Kafka by default. The OTel Collector kafkareceiver reads them. Producers and consumers agree on the schema by virtue of using the same OTel proto version.

```yaml
# OTel Collector kafkaexporter
exporters:
  kafka/traces:
    protocol_version: 2.0.0
    brokers: [kafka-1:9092, kafka-2:9092, kafka-3:9092]
    topic: otlp-traces
    encoding: otlp_proto         # default; alt: otlp_json, jaeger_proto, zipkin_json
    partition_traces_by_id: true # CRITICAL for tail sampling downstream
    producer:
      compression: zstd
      max_message_bytes: 10485760
      flush_max_messages: 1000
      required_acks: -1          # acks=all
    auth:
      sasl:
        mechanism: SCRAM-SHA-512
        username: ${env:KAFKA_USER}
        password: ${env:KAFKA_PASS}
      tls:
        ca_file: /etc/ssl/kafka-ca.pem
```

### 4.2 JSON vs protobuf vs Avro

| Format | Bytes (1k spans) | Encode/decode CPU | Schema evolution | When to use |
|---|---|---|---|---|
| OTLP protobuf | ~120 KB | low | additive only via field numbers | Default for traces, metrics, OTel logs |
| OTLP JSON | ~1.1 MB (10×) | medium | additive | Debugging, low-volume, edge cases (browser RUM) |
| Plain JSON logs | varies | medium | none enforced | Legacy log pipelines, Fluent Bit defaults |
| Avro | ~150 KB | medium | strict, schema-registry-mediated | Lakehouse pattern (Avro → Parquet path) |
| Arrow IPC | ~150 KB columnar | low (vectorized) | semi-strict | High-throughput analytics (Tempo's vParquet, OTel Arrow) |

The **10× protobuf-vs-JSON ratio** for traces is real and consistent. At 100k spans/sec, the difference is 12 MB/s vs 110 MB/s on the wire — and ~5 GB/hour vs 50 GB/hour on the broker disk. Use protobuf.

JSON is sometimes mandatory: browser RUM SDKs that can't ship a protobuf encoder, `application/x-ndjson`-based SaaS receivers, debugging. Convert at the collector edge — receive JSON, write protobuf to Kafka.

### 4.3 Schema registry patterns

When producers and consumers don't share a release cycle (e.g., a custom log format used by 80 services), a **schema registry** is the contract enforcement point:

- **Confluent Schema Registry** — the reference. Speaks Avro, Protobuf, JSON Schema. Stores schemas keyed by `subject` (typically `<topic>-value` and `<topic>-key`). Producers register schema → get an ID → prepend the 5-byte ID to the payload. Consumers read the ID, fetch the schema, decode.
- **Apicurio** — open-source alternative; same API.
- **AWS Glue Schema Registry** — Kinesis-flavored equivalent.

For OTel pipelines you usually don't need a schema registry on the OTLP topics — the OpenTelemetry proto schema is the contract, versioned by OTel release. You *do* need it for **custom log topics** where 50 services write 50 slightly different shapes and you want to catch schema drift at write time.

### 4.4 Schema evolution rules

The non-negotiables:

1. **Additive only.** New optional fields, with defaults. Never remove, rename, or change the type of an existing field.
2. **Field numbers (protobuf) / field names (JSON/Avro) are immutable.** Once a field is in production, it's in production forever.
3. **Old consumers must tolerate new fields** (default for OTLP protobuf — unknown fields are skipped).
4. **New consumers must tolerate old messages** (default with proper schema versioning + defaults).
5. **Deprecation** = mark the field deprecated, dual-write the new field, wait two full retention cycles, only then stop populating the old field.

Violating any of these causes either consumer crashes ("unexpected null in required field") or silent data loss ("consumer skipped messages it didn't recognize"). Both are bad; the silent one is worse.

### 4.5 Why Arrow / Parquet is showing up

Between transport and lakehouse, columnar batched formats are increasingly appearing:

- **OTel Arrow** (otel-arrow project) — encodes OTLP as Arrow IPC. ~3× compression vs OTLP protobuf, vectorized decode at the consumer. Wire-compatible OTLP, opt-in via `arrow_proto` encoding. For very high-volume pipelines this is meaningful.
- **Parquet sink** — Kafka Connect S3 + Parquet, or Iceberg sink. Cold-path consumers convert OTLP to Parquet for SQL on the lakehouse (chapter 06's "lakehouse" path).

You don't need either on day one. You will need them by the time your S3 tier costs more than your Kafka cluster.

---

## 5. Backpressure and Durability Semantics

### 5.1 Delivery semantics

| Semantic | What it guarantees | What it doesn't | Realistic? |
|---|---|---|---|
| At-most-once | No duplicates | Records may be lost | Trivial (ack before send) |
| At-least-once | No loss (modulo storage) | Duplicates possible | Default and right answer |
| Exactly-once | Both | (limited scope) | Within one Kafka transaction; not across systems |

**At-least-once + idempotent ingest at the consumer** is the production answer for all telemetry. The consumer must tolerate duplicates because Kafka's exactly-once is *intra-Kafka* (transactions across topics within one cluster); the moment a consumer writes to Mimir/Loki/Tempo, the guarantee is broken.

### 5.2 Idempotent ingest at the storage tier

How storage de-duplicates:

- **Mimir/Cortex** — sample dedup via `(series_id, timestamp_ms, value)`. Two writes with the same triple produce one stored sample. New value at the same timestamp = error (out-of-order).
- **Loki** — log line dedup via content hash within a chunk. Mostly idempotent.
- **Tempo** — span IDs are unique; double-writes to the same `(trace_id, span_id)` produce one stored span (object store overwrite).
- **ClickHouse** — `ReplacingMergeTree` or `INSERT … ON DUPLICATE KEY` patterns. Less automatic; requires schema design.

The takeaway: **never depend on exactly-once delivery**. Make your storage idempotent.

### 5.3 "Queue full" — failure modes at the producer

When the producer's buffer fills (Kafka client `buffer.memory` exhausted, or OTel exporter `sending_queue` full), four things can happen:

| Strategy | Effect | When it's right |
|---|---|---|
| **Block (default for Kafka client)** | Producer thread blocks; collector slows down; if collector is in-process with the app, the app slows down | Almost never for telemetry; this is the "telemetry takes down the service" path |
| **Drop newest** | Newest records are dropped; recent debug data lost | Default for OTel collector when queue is full — preserves history during a backend outage |
| **Drop oldest** | Oldest records dropped; recent data preserved | When recency matters more than completeness (alerting-driven metrics) |
| **Spill to disk** | Persistent buffer absorbs the burst | When you can take the disk hit and need durability through a multi-hour outage |

The OTel collector exporter helper has this as `sending_queue` config:

```yaml
exporters:
  kafka/traces:
    sending_queue:
      enabled: true
      num_consumers: 10
      queue_size: 10000           # in-memory items
      blocking: false             # do NOT block producer; drop on overflow
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 300s
    storage: file_storage/otc     # disk-backed queue; survives restart
```

The `storage:` reference points to a `file_storage` extension that persists the queue on disk — your collector tier's last line of defense before data loss when Kafka itself is down.

### 5.4 Disk-backed queues: collector vs broker

There are two durable buffers in this architecture:

```
┌─────────┐     ┌────────────┐         ┌────────┐         ┌─────────┐
│   App   │────▶│  Collector │────────▶│ Kafka  │────────▶│ Storage │
└─────────┘     │ disk queue │         │ broker │         └─────────┘
                │   (~GBs)   │         │  disk  │
                └────────────┘         │ (~TBs) │
                                       └────────┘
```

- **Collector disk queue** is for "Kafka is down for 5 minutes." Sized in GBs. Protects against transport-layer outage.
- **Broker disk** is for "the consumer is down for hours, or we want to replay." Sized in TBs (with tiered storage, effectively unlimited).

Both are required. Skip the collector disk queue and a broker outage propagates straight into your application's memory.

---

## 6. Multi-Tenant Transport

### 6.1 Topic-per-tenant vs single topic + tenant header

| Pattern | Pros | Cons | When |
|---|---|---|---|
| One topic per tenant per signal (`otlp-traces-tenant-acme`) | Strong isolation; per-tenant retention; per-tenant ACLs | Topic count blows up; partition count blows up (§8); meta-management cost | < 50 tenants, regulated tenants needing physical separation |
| Single topic + `tenant_id` header (`otlp-traces`) | Manageable topic count; centralized partition management | Noisy neighbor possible; per-tenant retention impossible at this layer | > 50 tenants; common case for SaaS observability platforms |
| Hybrid (per-tenant topic for "premium" tenants, shared topic for the rest) | Best of both | Two code paths to maintain | Tiered SaaS pricing |

For most platforms, the single-topic-with-`tenant_id`-attribute pattern is correct. Encode tenant_id as a Kafka record header (not the partition key — the key is `trace_id` for traces, etc.):

```
Kafka record:
  key:    <trace_id>             (binary)
  value:  <OTLP protobuf>
  headers:
    tenant_id: acme-corp
    signal:    traces
    schema_v:  1.3.0
```

Headers are cheap, tenant-routable at the consumer, and don't disturb partition assignment.

### 6.2 Per-tenant rate limits

At the **producer** (collector gateway): the OTel Collector has a `transformprocessor` + `filterprocessor` pattern, or use the dedicated tenant rate-limiter (memory_limiter is per-collector; for per-tenant you bring your own). At scale this is usually pushed to a sidecar or a custom processor.

At the **broker**: Kafka quotas. Per-principal:

```
# Limit a producer to 50 MB/s
kafka-configs.sh --bootstrap-server kafka-1:9092 \
  --alter --add-config 'producer_byte_rate=52428800' \
  --entity-type users --entity-name otel-gateway-acme

# Limit a consumer group (storage ingester) to 100 MB/s reads
kafka-configs.sh --bootstrap-server kafka-1:9092 \
  --alter --add-config 'consumer_byte_rate=104857600' \
  --entity-type users --entity-name tempo-ingester-acme
```

Quotas can also be set per-(user, client-id) for finer control. Once a quota is hit, the broker delays responses (it doesn't drop messages); the producer sees back-pressure as latency.

At the **consumer** (storage ingester): the storage tier enforces per-tenant ingest rate, max active series, max log volume, etc. This is the layer that finally protects the storage cluster — see chapter 19 for Mimir/Loki tenant limits.

### 6.3 Noisy neighbor isolation

The defense-in-depth stack:

1. **Producer-side rate limit per tenant** (collector gateway).
2. **Broker-side quota per producer principal** (defends Kafka itself).
3. **Partition spread per tenant** (don't let one tenant pin one partition; hash on `(tenant_id, sub_key)`).
4. **Consumer-side per-tenant fairness** (round-robin tenant batches at the storage ingester, or one consumer group per tenant for the largest tenants).
5. **Cardinality / volume budget at the storage tier** (chapter 19 — the hard ceiling).

A single noisy tenant typically eats one of layer 1–4 first; layer 5 is the last-resort circuit breaker.

---

## 7. Fan-Out Architectures

### 7.1 The lakehouse pattern

```
                                              ┌──────────────────┐
                                       ┌─────▶│ Mimir distributor│   "hot path"
                                       │      │ (15 days, fast)  │
                                       │      └──────────────────┘
                                       │
                                       │      ┌──────────────────┐
                  ┌─────────────┐      ├─────▶│ Tempo distributor│
   collector ────▶│ Kafka topic │──────┤      └──────────────────┘
                  │ otlp-traces │      │
                  └─────────────┘      │      ┌──────────────────┐
                                       ├─────▶│ Loki ingester    │
                                       │      └──────────────────┘
                                       │
                                       │      ┌──────────────────┐
                                       └─────▶│ S3 Parquet sink  │   "cold path"
                                              │ (Kafka Connect / │
                                              │ Iceberg)          │
                                              │ months–years     │
                                              └──────────────────┘
```

Each downstream is its own consumer group on the same topic. The hot path is fast, narrow, expensive per byte; the cold path is cheap per byte, slow to query (SQL on S3 via Trino, ClickHouse-on-S3, Athena), and gives you the long retention that the hot path can't afford.

The **replay use case**: a year later, a security incident requires querying logs from six months ago. Hot path doesn't have it; cold path does. Spin up a new consumer group on the cold-tier-backed topic (or a dedicated replay topic), feed it into a temporary Loki / ClickHouse cluster, run the query, tear down.

### 7.2 Don't dual-write from the producer

```
                           ┌────────┐
              ┌───────────▶│ Loki   │     ← bad
              │            └────────┘
   producer ──┤
              │            ┌────────┐
              └───────────▶│ S3     │     ← bad
                           └────────┘
```

The temptation: "let me have the collector write to both Loki and S3 directly, skip the queue." Why this is wrong:

1. **Two retry policies, two failure modes.** A Loki outage now requires the producer to handle both successful S3 writes and failed Loki writes — what does it do, retry only Loki? What if the Loki write succeeds three minutes later — do you have a duplicate?
2. **No replay.** S3 is an archive, not a replayable log. You can't reset Loki from S3 without writing custom replay machinery.
3. **The producer becomes the coordinator.** Any new destination requires producer changes.

Fan out at the queue. Always.

### 7.3 Kafka Connect / Iceberg sinks

For the cold path, the standard pattern is:

```yaml
# Kafka Connect S3 sink connector
name: s3-otlp-traces-cold
connector.class: io.confluent.connect.s3.S3SinkConnector
tasks.max: 8
topics: otlp-traces
s3.bucket.name: telemetry-cold-tier
s3.region: us-east-1
flush.size: 100000
rotate.interval.ms: 600000               # 10 minutes
storage.class: io.confluent.connect.s3.storage.S3Storage
format.class: io.confluent.connect.s3.format.parquet.ParquetFormat
partitioner.class: io.confluent.connect.storage.partitioner.TimeBasedPartitioner
path.format: 'year'=YYYY/'month'=MM/'day'=dd/'hour'=HH
locale: en-US
timezone: UTC
schema.compatibility: NONE
```

The output: hourly Parquet partitions in S3, queryable by Athena, Trino, ClickHouse-on-S3, or a Spark job. Iceberg sinks (Apache Iceberg + Kafka Connect Iceberg sink) add catalog-level schema evolution and ACID.

---

## 8. Operational Topics

### 8.1 Capacity planning a Kafka cluster for telemetry

**Throughput math (uncompressed → compressed → replicated):**

```
Ingest rate (bytes/sec from producers):  R_in
After compression (zstd 5×):              R_in / 5
Replication factor:                       3
Bytes written to broker disks total:      3 × R_in / 5  =  0.6 × R_in

Bytes-out (consumers + replication):
  Replication:                            (RF - 1) × R_in / 5  = 0.4 × R_in
  Consumers (3 fan-out):                  3 × R_in / 5         = 0.6 × R_in
  Total network out per cluster:          1.0 × R_in
```

Worked example: 50k spans/sec × 4 KB OTLP-proto each = 200 MB/s in. After zstd, 40 MB/s on the wire. Across 3 replicas, brokers absorb 120 MB/s of writes. Plus 80 MB/s replication + 120 MB/s consumer reads = 200 MB/s out. With 3 brokers, that's ~67 MB/s in, 67 MB/s out per broker — comfortable on a 10 Gbit NIC.

**Disk math:**

```
Hot retention: 24h × 40 MB/s × 3 (RF) = 10.4 TB total
                                        / 3 brokers = 3.5 TB/broker
Plus 30% headroom for compaction, log cleaner, segment rotation: ~4.5 TB/broker
```

Use NVMe SSD. Spinning disk is dead for telemetry; the IOPS pattern (small random reads from consumer rewind, segment compaction) destroys HDDs.

**Partition count math:**

```
Target throughput per partition: 5–10 MB/s sustained
Partitions per topic:            R_in / partition_throughput
```

For the 40 MB/s post-compression example, 8–16 partitions is plenty for `otlp-traces`. But you also need:

- Enough partitions for each consumer group to scale out — N_consumers ≤ N_partitions.
- Headroom for traffic growth — partition count is hard to reduce.

A common heuristic: **partitions = max(consumer_parallelism × 2, throughput / 5MBps)**. Round up to a power of 2 for clean rebalancing math. Don't over-provision: each partition has fixed overhead (file handles, metadata, replication threads).

**Per-broker partition limit:** keep `total_partitions_replicas / num_brokers ≤ ~4000`. Above that, controller failover takes minutes (because the new controller loads metadata for every partition). Modern Kafka with KRaft pushes this to ~100k but the per-broker resource overhead is still real.

### 8.2 Rolling upgrades

Kafka tolerates rolling broker upgrades because of replication. The procedure:

1. Disable broker auto-balancing during the upgrade (if using Cruise Control).
2. For each broker, in turn:
   - Drain leaders to other brokers (`kafka-leader-election.sh --election-type PREFERRED`).
   - Stop, upgrade binaries, start.
   - Wait for `under_replicated_partitions = 0`.
3. Bump `inter.broker.protocol.version` last, after all brokers are on the new version.
4. Bump `log.message.format.version` separately, after producers/consumers are confirmed compatible.

Upgrades that span major versions (2.x → 3.x, 3.x → KRaft) are not rolling — they're staged migrations with their own runbooks. Plan a maintenance window.

### 8.3 Cross-region replication

Three options for "telemetry in EU goes to brokers in EU but should also reach the US analytics store":

| Tool | Topology | Latency | Operational cost | Best for |
|---|---|---|---|---|
| **MirrorMaker 2** | Active-passive or active-active; runs on Connect | Async, seconds | Low; standard tooling | Most cases |
| **Confluent Replicator** | Same as MM2 + offset translation | Async | Confluent license | When buying Confluent already |
| **Confluent Cluster Linking** | Direct broker-to-broker, no Connect cluster | Lower; broker-level | Confluent only | High-volume, latency-sensitive |
| **Stretched cluster (multi-region brokers)** | One cluster across regions | Sync, slow producer | High (RF math gets ugly) | Avoid for telemetry |

For an OTel pipeline, MirrorMaker 2 with active-passive replication is the default. The "writer side" is the region where the collector lives; the "reader side" is the analytics region. Don't write to both sides for the same data — pick a primary, MM2 handles the rest.

### 8.4 Security

Mandatory in any multi-tenant or production cluster:

```
listeners=SASL_SSL://0.0.0.0:9093
security.inter.broker.protocol=SASL_SSL
sasl.mechanism.inter.broker.protocol=SCRAM-SHA-512
sasl.enabled.mechanisms=SCRAM-SHA-512
ssl.keystore.location=/etc/kafka/kafka.server.keystore.jks
ssl.truststore.location=/etc/kafka/kafka.server.truststore.jks
ssl.client.auth=required
authorizer.class.name=org.apache.kafka.metadata.authorizer.StandardAuthorizer
```

ACLs per tenant — producer principals can write only their own topics, consumer principals can read only their own consumer group:

```
kafka-acls.sh --bootstrap-server kafka:9093 \
  --add --allow-principal User:otel-gw-acme \
  --producer --topic otlp-traces

kafka-acls.sh --bootstrap-server kafka:9093 \
  --add --allow-principal User:tempo-acme \
  --consumer --topic otlp-traces --group tempo-acme
```

mTLS instead of SASL/SCRAM is also fine and slightly simpler at scale (cert rotation is solved; SCRAM password rotation is custom). Use whichever your existing PKI supports.

### 8.5 Meta-observability — observing the queue itself

The queue can fail silently. The metrics you must scrape from your Kafka cluster:

```
# Per broker
kafka_server_brokertopicmetrics_bytesin_total
kafka_server_brokertopicmetrics_bytesout_total
kafka_server_replicamanager_underreplicatedpartitions
kafka_server_replicamanager_isrshrinks_total
kafka_controller_kafkacontroller_activecontrollercount
kafka_log_log_logsize{topic, partition}
kafka_network_requestmetrics_localtimems  # queue handler latency

# Per topic
kafka_topic_partitions{topic}
kafka_topic_message_in_total{topic}

# Per consumer group (via lag exporter or Burrow)
kafka_consumer_group_lag{group, topic, partition}
kafka_consumer_group_lag_seconds{group, topic}
```

The four alerts that matter:

```
# 1. Cluster controller is stable
kafka_controller_active_count != 1 for 1m → page

# 2. No under-replicated partitions
kafka_under_replicated_partitions > 0 for 5m → ticket
kafka_under_replicated_partitions > 0 for 30m → page

# 3. Consumer lag is approaching retention loss
kafka_consumer_group_lag_seconds > (retention * 0.5) for 10m → page

# 4. Disk filling
node_filesystem_free_bytes{mountpoint=~"/var/lib/kafka.*"} 
   / node_filesystem_size_bytes < 0.20 → page
```

This is recursive — you're using your observability stack to observe the transport layer of your observability stack. **Always run the meta-monitoring on a different stack** (or at minimum a different Prometheus). If your transport is down, the alert about your transport being down also can't get through.

---

## 9. Failure Modes and Case Studies

### 9.1 Broker disk full → producer back-pressure → collector OOM

Sequence:

```
T+0     One broker's disk hits 100%. New writes to its log segments fail.
T+0     Replication to that broker stalls; topic enters "under-replicated" state.
T+30s   With acks=all and min.insync.replicas=2, leader rotation begins;
         partitions whose leaders were on the full broker re-elect. Until
         re-election completes (several seconds), producers retry → buffer
         memory grows.
T+2m    Producer buffer.memory exhausted. Kafka client sends "block" upstream
         to the OTel collector exporter helper.
T+2m    Collector sending_queue fills (because exporter is blocked).
T+5m    Collector memory grows; memory_limiter starts dropping batches.
         BUT: the OTel batchprocessor still buffers in-memory batches.
T+8m    Collector OOM-killed by Kubernetes.
T+8m    Application's OTLP exporter starts seeing connection refused.
         Application's own export queue grows. Application memory grows.
T+15m   In the worst case, application pods OOM. Production traffic affected.
```

The fix at every layer:
- **Broker**: alert at 70% disk, drain at 80%, expand before 90%. Always.
- **Producer**: `block.on.buffer.full=false` (Kafka client) or `blocking: false` (OTel sending_queue).
- **Collector**: hard memory ceiling (`memory_limiter` extension), drop batches, *never* propagate back-pressure to the app.

### 9.2 Slow consumer → lag → retention window blown → permanent data loss

Sequence:

```
T+0      Tempo distributor deploy regression: indexing path 3× slower.
T+30m    Consumer lag grows linearly. Lag exporter shows 30m lag.
T+1h     Lag = 1 hour.
T+24h    Lag = 24 hours. Equal to topic retention (24h).
T+24h+1m At broker, log.retention.hours triggers segment deletion.
         Consumer's next fetch returns "OFFSET_OUT_OF_RANGE."
         Consumer either resets to earliest (skipping ahead, losing
         everything between previous offset and earliest) or to latest
         (skipping ahead, losing everything between previous offset and now).
         Either way, **permanent data loss**.
```

Defense: the lag-vs-retention alert in §2.7. By the time lag reaches 50% of retention, you have hours to fix the consumer or expand retention.

### 9.3 Stuck consumer group → duplicate ingestion

Sequence:

```
T+0      Consumer C-1 is processing a batch from partition P. C-1 hangs
         (deadlock, GC pause, slow downstream call).
T+45s    No heartbeat. Broker considers C-1 dead. Triggers consumer group
         rebalance. Partition P assigned to C-2.
T+45s    C-2 starts processing from C-1's last committed offset.
T+45s+ε  C-1 wakes up, commits the batch it was processing, then dies.
         Records that C-1 already wrote to storage are now also being
         written by C-2 → duplicates.
```

This is why **idempotent ingest at the storage tier is non-negotiable**. Kafka exactly-once doesn't help here because the downstream write is outside the Kafka transaction.

### 9.4 Network partition between collector and broker region

Producer side decides what to do based on `delivery.timeout.ms`:
- If `delivery.timeout.ms` is short (default 2 minutes), records older than the timeout are dropped on the producer side.
- If long (we recommend 10+ minutes for telemetry), the producer keeps retrying and buffering. The disk-backed collector queue absorbs the burst.

The mistake: setting `delivery.timeout.ms=120000` (default) and `retries=2147483647` together. The retries don't matter once the timeout expires. Bump both, or accept short outages.

### 9.5 ZooKeeper-vs-KRaft transition gotchas

Kafka 4.0 (released 2025) drops ZooKeeper entirely; KRaft is the only metadata mode. If you're still on 2.x or 3.x with ZooKeeper:

- **Migration is one-way and requires downtime.** It's not a rolling change.
- **Controller quorum size matters.** KRaft uses Raft for metadata; you need 3 or 5 controllers (odd numbers, like any Raft cluster). Don't co-locate controllers with high-load brokers — controller latency affects every partition leader change.
- **Backup the metadata.** KRaft metadata is in `__cluster_metadata` topic. Snapshot it before major changes.

For a new cluster in 2026, start on KRaft. Don't even consider ZooKeeper.

---

## 10. Worked Example: Reference Topology

A reference topology for a mid-size SRE platform: 5 environments, 30 services, ~200k events/sec total telemetry.

### 10.1 Topology

```
   Application pods (with OTel SDK)
            │   OTLP/gRPC
            ▼
   ┌─────────────────────┐
   │ OTel Collector      │  DaemonSet per node — receives, batches, 
   │ Agent (per-node)    │  enriches with k8s metadata
   └─────────┬───────────┘
             │   OTLP/gRPC
             ▼
   ┌─────────────────────┐
   │ OTel Collector      │  Deployment, 6 replicas — tail sampling for 
   │ Gateway             │  traces, redaction, fan-out to Kafka
   └─────────┬───────────┘
             │   Kafka producer (zstd, acks=1, partition_traces_by_id=true)
             ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │ Kafka cluster (3 brokers, NVMe SSD, KRaft, RF=3, MISR=2)         │
   │                                                                   │
   │   topic: otlp-metrics    partitions: 16   key: tenant_id          │
   │   topic: otlp-traces     partitions: 32   key: trace_id           │
   │   topic: otlp-logs       partitions: 24   key: tenant_id          │
   │   topic: otlp-logs-audit partitions:  8   key: tenant_id (RF=3,   │
   │                                                       acks=all)   │
   │                                                                   │
   │   Tiered storage to S3 enabled; hot retention 24h on broker disk  │
   │   Cold retention 30d in S3                                        │
   └──┬─────────────────┬───────────────┬──────────────┬───────────────┘
      │                 │               │              │
      ▼                 ▼               ▼              ▼
   ┌────────┐      ┌────────┐      ┌────────┐    ┌──────────────┐
   │ Mimir  │      │ Tempo  │      │ Loki   │    │ Kafka Connect│
   │ distr  │      │ distr  │      │ ingstr │    │ S3 sink      │
   │ group: │      │ group: │      │ group: │    │ group:       │
   │  mimir │      │  tempo │      │  loki  │    │  s3-cold     │
   └────────┘      └────────┘      └────────┘    └──────────────┘
                                                        │
                                                        ▼ Parquet
                                                  ┌────────────┐
                                                  │ S3 + Iceberg│
                                                  │ Trino/Athena│
                                                  └────────────┘
```

### 10.2 Kafka topic configurations

```bash
# otlp-traces — partitioned by trace_id for tail-sampling assembly
kafka-topics.sh --bootstrap-server kafka-1:9092 --create \
  --topic otlp-traces \
  --partitions 32 \
  --replication-factor 3 \
  --config min.insync.replicas=1 \
  --config retention.ms=86400000 \
  --config compression.type=zstd \
  --config segment.bytes=1073741824 \
  --config remote.storage.enable=true

# otlp-logs-audit — durable, longer retention, stronger acks
kafka-topics.sh --bootstrap-server kafka-1:9092 --create \
  --topic otlp-logs-audit \
  --partitions 8 \
  --replication-factor 3 \
  --config min.insync.replicas=2 \
  --config retention.ms=2592000000 \
  --config compression.type=zstd \
  --config remote.storage.enable=true
```

### 10.3 OTel Collector kafkaexporter config

```yaml
exporters:
  kafka/traces:
    brokers: [kafka-1:9093, kafka-2:9093, kafka-3:9093]
    topic: otlp-traces
    encoding: otlp_proto
    partition_traces_by_id: true
    producer:
      compression: zstd
      max_message_bytes: 10485760
      flush_max_messages: 10000
      required_acks: 1
    sending_queue:
      enabled: true
      num_consumers: 10
      queue_size: 50000
      blocking: false
      storage: file_storage/otc
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 300s
    auth:
      sasl:
        mechanism: SCRAM-SHA-512
        username: ${env:KAFKA_USER}
        password: ${env:KAFKA_PASS}
      tls:
        ca_file: /etc/ssl/kafka-ca.pem

  kafka/metrics:
    brokers: [kafka-1:9093, kafka-2:9093, kafka-3:9093]
    topic: otlp-metrics
    encoding: otlp_proto
    partition_metrics_by_resource_attributes: [service.namespace, tenant.id]
    producer:
      compression: zstd
      required_acks: 1
    # ... same sending_queue / retry / auth as above

  kafka/logs:
    brokers: [kafka-1:9093, kafka-2:9093, kafka-3:9093]
    topic: otlp-logs
    encoding: otlp_proto
    producer:
      compression: zstd
      required_acks: 1
    # ... same sending_queue / retry / auth as above

extensions:
  file_storage/otc:
    directory: /var/lib/otelcol/queue
    timeout: 10s

service:
  extensions: [file_storage/otc]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, tail_sampling, batch]
      exporters: [kafka/traces]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [kafka/metrics]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, attributes/redact, batch]
      exporters: [kafka/logs]
```

### 10.4 Capacity estimate

For 200k events/sec at 4 KB/event = 800 MB/s ingest. After zstd compression (~5×), 160 MB/s on the wire. Across 3 brokers with RF=3, each broker handles:

- Inbound producer traffic: ~53 MB/s (assume even partition spread)
- Inbound replication: ~107 MB/s (from other 2 brokers)
- Outbound replication: ~107 MB/s
- Outbound consumer: ~160 MB/s (to 3 fan-out groups + 1 cold sink)

Total per broker: ~213 MB/s in, ~267 MB/s out. Comfortable on a 10 Gbit NIC; tight on 1 Gbit.

24h retention × 160 MB/s × 3 RF = 41.5 TB. Across 3 brokers = ~14 TB/broker (NVMe). With tiered storage offloading anything > 6h, ~3.5 TB/broker on local disk + S3 for the rest.

---

## 11. When NOT to Use a Queue

The decision rubric:

```
Do you have one storage backend per signal?              [yes / no]
Do you have one region of producers and consumers?       [yes / no]
Is sustained ingest below 5k events/sec per signal?      [yes / no]
Do you have ≤ 3 internal tenants?                        [yes / no]
Is your storage tier 99.9%+ available historically?      [yes / no]
Have you ever needed to replay telemetry?                [no  / yes]
```

If all answers are in the "no queue" column, **don't add a queue.** The collector's disk-backed `sending_queue` plus the storage tier's own retry logic is enough. You'll get:

- One fewer system to operate (Kafka is not free; budget 0.5 SRE FTE minimum).
- Lower end-to-end latency (no broker-side hop).
- Lower cost (no broker fleet).
- Simpler architecture diagrams.

Add the queue when **any one** of those answers flips. Don't add it speculatively. The classic mistake is "we'll need it eventually" — by the time you need it, you'll know, because the collector's queue is filling and the storage outages are visible. Until then, you're carrying complexity for a future you may never have.

The honest version: most stacks under ~2k engineers can run without a transport queue. Most stacks above that scale benefit from one. The middle is where judgment lives.

---

## 12. Common Pitfalls

1. **Partitioning traces by service name (or anything other than trace_id).** Tail sampling and trace assembly break silently — incomplete traces show up in Tempo, queries return DAGs with missing branches, no error logged anywhere. Always `partition_traces_by_id: true`.
2. **Using JSON encoding for high-volume traces.** 10× the bytes of OTLP protobuf. The bandwidth and disk hit is real and pointless. Reserve JSON for debugging or low-volume edges (browser RUM).
3. **Single consumer group for multiple signals.** "We have one group called `obs-ingester` consuming all topics." Lag on traces is now indistinguishable from lag on logs; partition assignment is balanced wrong; one slow signal stalls the others. One group per signal per destination.
4. **No DLQ.** A poison pill (corrupt protobuf, schema mismatch, single oversized message) makes the consumer crash-loop. Send rejected messages to a dead-letter topic, alert on its size, never block the main consumer.
5. **No schema registry on heterogeneous custom log streams.** 50 services emitting 50 slightly-different shapes with no contract. Three weeks later: "why are 12% of logs unparseable?" Use Apicurio or Confluent SR for any non-OTLP custom topic.
6. **Dual-writing from app to queue and storage simultaneously.** Either one-write-to-storage with no replay, or one-write-to-queue with fan-out — never both. The "we'll dual-write for safety during the migration" plan always fossilizes into permanent dual-write.
7. **`acks=0` for telemetry to "improve throughput."** It works until a broker hiccups and drops 30 seconds of data silently. Always at least `acks=1`. Use `acks=all` for audit and billing topics where compliance demands it.
8. **`linger.ms=0` (no batching) on a high-volume producer.** Quintuples the request rate, increases broker CPU 3×, hurts compression ratios. Default to 50 ms.
9. **Forgetting to set producer/consumer rate limits per tenant.** First time a tenant misbehaves (bad relabel rule emitting 100M new series), they take down the whole queue. Quotas are cheap, set them early.
10. **No alert on "consumer lag approaches retention."** The single most expensive missing alert in this layer. By the time data is *gone*, it's too late.
11. **Using a single topic for all signals with a "type" header.** Loses you per-signal retention, per-signal partitioning strategy, per-signal access control. Topic separation is the easiest organizational tool here.
12. **Cross-region stretched Kafka cluster (one cluster, brokers in two regions).** Replication is synchronous; producer p99 jumps to the cross-region RTT. Use two clusters + MirrorMaker 2 instead.

---

## 13. Glossary / Mental Model Summary

| Term | What it means here |
|---|---|
| **Topic** | The named, partitioned, durable log. One per signal type per logical purpose. |
| **Partition** | The unit of parallelism; ordered within, unordered across. Owned by exactly one consumer in a group at a time. |
| **Consumer group** | A set of consumers sharing the work of consuming a topic. Two groups on the same topic = fan-out. |
| **Offset** | Per-partition, per-group cursor. Reset = replay. |
| **Lag** | end_offset − consumer_offset. The platform's most important transport SLI. |
| **RF / ISR / MISR** | Replication factor / in-sync replicas / minimum in-sync replicas. The durability triad. |
| **acks** | What "the producer's write succeeded" means. `1` = leader has it; `all` = leader + MISR have it. |
| **Idempotent producer** | Within a producer session, no duplicates from retries. Free; always on. |
| **Tiered storage** | Hot segments on broker disk, cold segments offloaded to object storage. Required for multi-week retention at scale. |
| **DLQ** | Dead-letter queue. The topic where unprocessable messages go to die so they don't crash-loop the main consumer. |
| **Schema registry** | Out-of-band store for schemas referenced by ID in the message header. Enforces evolution rules at write time. |
| **Lakehouse / cold path** | The Kafka → S3 Parquet path that gives you SQL-on-telemetry at 1/100 the hot-path cost. |
| **Tail sampling partition rule** | All spans of a trace must land in the same partition. Hash the partition key on `trace_id`, no exceptions. |

The mental model in one paragraph: **the queue is the place where producers and consumers no longer need to know about each other.** Producers commit data to a durable log; consumers, at their own pace, read it and do whatever they want with it. Everything in this chapter is consequence: durability via replication, ordering via partitioning, isolation via quotas and topic separation, fan-out via consumer groups, replay via retained offsets, cost control via tiered storage. Get the partitioning key right (`trace_id` for traces, `tenant_id` for everything else), set the lag alert at 50% of retention, monitor the cluster on a separate stack, and the rest is tuning.

Chapter 06 picks up from the consumer side: how the Mimir distributor, ingester, and store-gateway turn the metrics topic into a queryable TSDB. Chapter 07 does the same for Loki on the logs topic. Chapter 08 covers Tempo's consumption pattern (and why the trace_id partitioning rule from this chapter is what makes it work). Chapter 19 covers the per-tenant quota story end-to-end across the producer, broker, and consumer layers.

---

## TL;DR

The transport layer is the durability and decoupling boundary between collection and storage. Add a queue (Kafka by default; Pub/Sub, Kinesis, Redpanda, Pulsar as managed alternatives) once you have more than one storage backend, more than one region, more than ~50k events/sec, or more than three tenants — below those thresholds, the collector's disk-backed queue plus storage retries is enough. Partition traces by `trace_id` (mandatory for tail sampling), partition metrics and logs by `tenant_id`, encode in OTLP protobuf with zstd compression, run with RF=3 / acks=1 for routine signals and acks=all for audit and billing, alert on consumer lag approaching half of retention, and fan out to multiple consumer groups (hot path: Mimir/Loki/Tempo; cold path: Kafka Connect → S3 Parquet) instead of dual-writing from the producer. The queue is what makes the storage outage at 03:14 on Sunday a paged-out lag alert instead of a service-impacting incident — and what lets you replay six months later when a security incident asks where the bytes went.
