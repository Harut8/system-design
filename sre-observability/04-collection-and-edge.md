# 04 — Collection & Edge Processing

> The thinnest, most operationally critical layer in the entire stack. Above it is application code your platform team doesn't own. Below it is storage cost that, once written, you cannot un-pay. Everything you do here — batch, redact, sample, drop — is leverage; everything you forget here is either a $50k/month invoice surprise, a PII incident, or a 3 a.m. page about a dropped trace nobody can replay.

This chapter sits between [03 — Instrumentation](./03-instrumentation.md) and [05 — Transport & Buffering](./05-transport-and-buffering.md). Producers (SDKs, exporters, eBPF probes) hand off here; backends (TSDB, log store, span store) receive from here. If you redesign one layer of your observability stack per quarter, this is the one with the highest ROI per engineer-week.

---

## 1. Why a Collection Layer At All

You can technically point an OpenTelemetry SDK directly at a vendor endpoint and call it a day. Don't. The collection layer exists because of four invariants no application process can satisfy on its own:

1. **The producer cannot enforce fleet-wide policy.** Sampling, redaction, cardinality budgets, tenant tagging — these are platform decisions. Asking every service to implement them in code is N×M coupling and means the redaction bug ships in 80 services.
2. **The producer cannot survive backend outages.** When Tempo is down for 40 minutes, you do not want an `OTLP send failed` to either (a) crash the producer, (b) consume all its memory, or (c) silently drop. You want a buffer somewhere, owned by someone whose pager goes off when it fills.
3. **The producer cannot do post-hoc decisions.** Tail sampling needs *all spans of a trace assembled in one place*. The producer only sees its own spans. The decision must move to a stateful midpoint.
4. **The producer should not know the backend.** Switching from Tempo to Honeycomb or from Loki to ClickHouse should be a collector config change, not a fleet redeploy.

This is the same argument that gave us reverse proxies in front of web servers in 1998. It generalizes.

```
┌──────────────────────────────────────────────────────────────────────────┐
│              WORKLOAD PROCESSES   (your developers' code)                │
│        SDK / exporter / eBPF / file appender — 1000s of producers        │
└──────────────────────────────┬───────────────────────────────────────────┘
                  OTLP / Prom expo / syslog / file
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  AGENT TIER  (per-node DaemonSet or sidecar)                             │
│    - Host enrichment (k8s metadata, hostname, region)                    │
│    - File tailing (containerd, journald, /var/log)                       │
│    - Local batching, retry, OTLP/Prom push to gateway                    │
│    - SHOULD be small, dumb, and CPU-cheap                                │
└──────────────────────────────┬───────────────────────────────────────────┘
                   OTLP/gRPC (compressed, batched)
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  GATEWAY TIER  (centralized, typically Deployment+HPA)                   │
│    - Tail sampling (stateful)                                            │
│    - PII redaction, attribute denylist                                   │
│    - Cardinality drop / hash / bucket                                    │
│    - Persistent queue (WAL on disk) for backend outages                  │
│    - Vendor fan-out: same data → Mimir + Loki + Tempo + Kafka            │
└─────────┬──────────────┬──────────────┬──────────────────┬───────────────┘
          ▼              ▼              ▼                  ▼
       Mimir/         Loki/         Tempo/             Kafka
       VM/M3         ClickHouse    ClickHouse         (transport — 05)
```

This **agent + gateway** two-tier is the production default. It is not the only valid topology, but every alternative ("just an agent", "just a gateway", "direct to vendor") is a deliberate downgrade you should be able to defend in a design review.

### 1.1 Push vs pull

The metric pipeline has a religious split here; logs and traces are uncontroversial (push only).

| Dimension | Pull (Prometheus scrape) | Push (OTLP, statsd, remote_write) |
|---|---|---|
| Discovery | Server-side (k8s SD, EC2 SD, Consul) | Client knows endpoint |
| Auth | Server walks the fleet, has fleet creds | Each producer must hold creds |
| Durability across producer restart | Fine — server retries the next interval | Producer needs durable buffer |
| Short-lived workloads (CronJob, Lambda) | Bad fit — process exits before scrape | Natural fit — pushgateway / OTLP push |
| Cardinality control | Centralized at server (`metric_relabel_configs`) | Must be done at producer or in-collector |
| "Did the target answer?" signal | Free (`up{}` series) | Must be inferred from arrival pattern |
| Federation across networks | Server must reach every target | Producers reach one endpoint |

The pragmatic reading: **pull for stable, long-lived services on a flat network you control; push for everything else.** Real platforms run both. The OTel Collector embraces this by exposing `prometheus` as a *receiver* (it scrapes targets, like a Prometheus would) and `prometheusremotewrite` as both an exporter and a receiver. You can stitch any topology.

### 1.2 Agent vs gateway responsibilities

The split is not arbitrary. Use this as the default and have a reason when you deviate.

| Concern | Agent (per-node) | Gateway (fleet) |
|---|---|---|
| Reading host signals (`/proc`, `/sys`, `journald`) | Yes | No |
| Tailing container/file logs | Yes | No |
| K8s metadata enrichment (`pod`, `namespace`, `node`) | Yes (RBAC scoped to the node) | Sometimes (if agent skipped it) |
| Batching, compression | Yes (small batches) | Yes (large batches before fan-out) |
| In-memory retry / queueing | Yes (small) | Yes (large, often with file-backed WAL) |
| Tail sampling | **No** (single agent only sees one node's spans) | **Yes** — must be here |
| PII redaction | Sometimes | Always |
| Cardinality drops | Sometimes | Always |
| Tenant tagging / multi-tenant routing | No | Yes |
| Vendor fan-out (same data → 2+ backends) | No | Yes |
| Auth secrets to backends (API keys) | No | Yes (one place to rotate) |
| Talks directly to backend store | Avoid | Yes |

Why the agent never holds backend creds: every node holding a vendor API key is N×N rotation. Every node having a network path to the vendor is your egress firewall surface area. Push to a gateway over mTLS inside the cluster, push to vendor from one place.

Why tail sampling cannot live at the agent: spans of a single trace are emitted by *multiple processes* on *multiple nodes*. An agent only sees the spans from its node. Tail sampling needs the whole trace co-located in memory; that is the gateway's job.

---

## 2. OpenTelemetry Collector — Deep Dive

The OpenTelemetry Collector (`otelcol`) is the de facto standard for both agent and gateway today. It supersedes the Jaeger Agent, the Zipkin collector, the OpenCensus service, and is on track to replace Fluent Bit and Vector for the OTLP-shaped subset of the log pipeline (it hasn't yet for reasons we'll cover in §3).

### 2.1 The architecture in one diagram

```
                  ┌──────────────────────────────────────────────┐
                  │              otelcol process                 │
                  │                                              │
   OTLP/gRPC ───▶ │ ┌──────────┐   ┌────────────┐   ┌─────────┐ │ ─▶ Mimir
   OTLP/HTTP ───▶ │ │ Receivers│──▶│ Processors │──▶│Exporters│ │ ─▶ Tempo
   Prom scrape ─▶ │ └──────────┘   └────────────┘   └─────────┘ │ ─▶ Loki
   filelog ──────▶│      ▲              ▲               │       │ ─▶ Kafka
   syslog ───────▶│      │              │               │       │
                  │      └── Pipelines ─┴───────────────┘       │
                  │       (one per signal type, or split fan)   │
                  │                                              │
                  │  Extensions (cross-cutting):                 │
                  │   health_check, pprof, zpages,               │
                  │   memory_ballast, file_storage, oauth2client │
                  └──────────────────────────────────────────────┘
```

Three first-class concepts:

- **Receiver** — reads data from somewhere (network listener, file tailer, scrape).
- **Processor** — transforms data in-flight (batch, filter, transform, redact, sample).
- **Exporter** — writes data somewhere (OTLP push, remote_write, Loki push, Kafka).
- **Pipeline** — an ordered chain receiver → processors → exporter, scoped to a signal type (`traces`, `metrics`, `logs`).
- **Extension** — out-of-band capability that isn't in the data path (health endpoint, profiling, persistent queue storage).

A pipeline is a list, not a graph. To do fan-out (same data → 2 places), you list two exporters in one pipeline. To do fork (same data → different processing → different places), you define two pipelines that share a receiver.

### 2.2 Configuration model

Everything is YAML. The shape is rigid:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 1800
    spike_limit_mib: 400
  batch:
    send_batch_size: 8192
    timeout: 5s

exporters:
  otlphttp/tempo:
    endpoint: https://tempo-distributor.tempo.svc:4318
  prometheusremotewrite:
    endpoint: https://mimir-distributor.mimir.svc/api/v1/push

extensions:
  health_check:
    endpoint: 0.0.0.0:13133
  file_storage:
    directory: /var/lib/otelcol/queue
    timeout: 1s

service:
  extensions: [health_check, file_storage]
  pipelines:
    traces:
      receivers:  [otlp]
      processors: [memory_limiter, batch]
      exporters:  [otlphttp/tempo]
    metrics:
      receivers:  [otlp]
      processors: [memory_limiter, batch]
      exporters:  [prometheusremotewrite]
```

The naming convention is `type` or `type/instance`. You can have multiple `otlp` receivers (`otlp/internal`, `otlp/external`) listening on different ports with different configs. Same for processors and exporters.

**Order in the `processors:` list under a pipeline matters.** The collector pushes batches through that list in order. `memory_limiter` must come *first* (so it can refuse load when the heap is full); `batch` should come last among the buffering processors.

### 2.3 Core, contrib, custom builds

`otelcol` ships in three flavors:

| Distribution | Components | Use it when |
|---|---|---|
| **Core** (`otelcol`) | OTLP receivers/exporters, batch, memory_limiter, attributes, resource | Educational; rarely shipped to prod |
| **Contrib** (`otelcol-contrib`) | Hundreds of components: `tail_sampling`, `k8sattributes`, `transform`, `loadbalancing`, `prometheusremotewrite`, `lokiexporter`, `clickhouse`, `loki`, `filelog` | The default for production gateways and most agents |
| **Custom** (built with `ocb` / OpenTelemetry Collector Builder) | Whatever subset you specify | When you want a small binary, or you've forked a component |

Contrib is what you run in production *unless* the binary size or attack surface matters (FedRAMP environments, embedded edge boxes). Then build a custom one with `ocb`:

```yaml
# builder-config.yaml — fed to `ocb --config builder-config.yaml`
dist:
  name: otelcol-prod
  description: Custom collector for $COMPANY
  output_path: ./build
  version: 0.115.0

extensions:
  - gomod: github.com/open-telemetry/opentelemetry-collector-contrib/extension/healthcheckextension v0.115.0
  - gomod: github.com/open-telemetry/opentelemetry-collector-contrib/extension/storage/filestorage v0.115.0

receivers:
  - gomod: go.opentelemetry.io/collector/receiver/otlpreceiver v0.115.0

processors:
  - gomod: go.opentelemetry.io/collector/processor/batchprocessor v0.115.0
  - gomod: go.opentelemetry.io/collector/processor/memorylimiterprocessor v0.115.0
  - gomod: github.com/open-telemetry/opentelemetry-collector-contrib/processor/tailsamplingprocessor v0.115.0
  - gomod: github.com/open-telemetry/opentelemetry-collector-contrib/processor/k8sattributesprocessor v0.115.0
  - gomod: github.com/open-telemetry/opentelemetry-collector-contrib/processor/transformprocessor v0.115.0

exporters:
  - gomod: github.com/open-telemetry/opentelemetry-collector-contrib/exporter/prometheusremotewriteexporter v0.115.0
  - gomod: github.com/open-telemetry/opentelemetry-collector-contrib/exporter/lokiexporter v0.115.0
  - gomod: go.opentelemetry.io/collector/exporter/otlpexporter v0.115.0
  - gomod: github.com/open-telemetry/opentelemetry-collector-contrib/exporter/loadbalancingexporter v0.115.0
```

The builder produces a single Go binary. Pin component versions; don't track `latest`.

### 2.4 The four processors you cannot live without

#### `memory_limiter` — the only thing standing between you and OOMKill

The collector does not *have* memory backpressure to its receivers by default. If receive rate > export rate and there's no `memory_limiter`, the heap grows until the kernel reaps the pod, the WAL on disk corrupts, and on restart you've lost in-flight batches.

```yaml
processors:
  memory_limiter:
    check_interval: 1s        # how often to check heap usage
    limit_mib: 1800           # soft cap — start refusing
    spike_limit_mib: 400      # hard cap = limit + spike → start GC
```

When the soft cap is hit, the limiter starts returning errors back through receivers. OTLP/gRPC receivers translate this to `RESOURCE_EXHAUSTED`, which OTel SDKs respect with backoff. **Place it first in every pipeline.** Place it after a `batch` and you've already used the memory you were trying to protect.

Watch `otelcol_processor_refused_*` and `otelcol_process_runtime_total_alloc_bytes` to know it's working.

#### `batch` — the throughput multiplier

Without `batch`, every span/sample/log line traverses the export call path individually. With it, you group up to N items or T seconds:

```yaml
processors:
  batch:
    send_batch_size: 8192        # target size
    send_batch_max_size: 16384   # never exceed this
    timeout: 5s                  # max delay before flushing partial batch
```

Rules of thumb:

- For traces: `send_batch_size: 512–8192`, `timeout: 1–5s`.
- For metrics: `send_batch_size: 8192`, `timeout: 10s` (metrics tolerate more delay).
- For logs: `send_batch_size: 1000–10000`, `timeout: 1–5s`.

`batch` is conventionally placed *after* `memory_limiter` and *after* sampling/redaction processors but *before* the exporter. The mistake we'll cover in §11 is putting it before `tail_sampling`, which then has to materialize the entire batch in memory while it waits for the trace assembly window.

#### Retry & queue (in the exporter, not a processor)

Each exporter has a `sending_queue` and a `retry_on_failure` block. They are easy to mis-tune.

```yaml
exporters:
  otlphttp/tempo:
    endpoint: https://tempo-distributor:4318
    sending_queue:
      enabled: true
      num_consumers: 10           # parallelism out
      queue_size: 5000            # in-memory items
      storage: file_storage       # ← named extension; persistent queue
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 5m        # after this, drop
    timeout: 30s
```

Without `storage:`, the queue is in memory only — if the pod restarts, the queue is gone. With `storage: file_storage` and the matching extension, the queue is a WAL on disk and survives restarts:

```yaml
extensions:
  file_storage:
    directory: /var/lib/otelcol/queue
    timeout: 1s
    compaction:
      directory: /var/lib/otelcol/queue
      on_start: true
      on_rebound: true
      rebound_needed_threshold_mib: 10
      rebound_trigger_threshold_mib: 20
```

This is the single most important durability knob in the gateway. Without it, a 30-minute Mimir outage during a deploy of the collector = data loss. With it, the collector buffers to disk for as long as the disk lasts.

The cost: write amplification (every batch is now persisted before being sent). For metrics, the cost is fine. For high-volume logs, you may run out of disk before you run out of patience — size your PVC accordingly. **Provision 4–6× your peak send rate × max retention** and have a circuit breaker that paginates if the queue depth crosses 80%.

#### `attributes` and `resource` — different things, often confused

- `attributes` processor: modifies *span attributes / log attributes / metric data point attributes* (the per-event K/V pairs).
- `resource` processor: modifies the *resource* (the entity producing the data — the service, host, k8s pod). One resource is shared by many events from the same producer.

If you need to add `cluster=us-east-1` to every span produced by every workload, that's a `resource` mutation, not an `attributes` mutation. Putting `cluster` in attributes wastes bytes and breaks resource-based aggregation in stores like Tempo.

```yaml
processors:
  resource:
    attributes:
      - key: deployment.environment
        value: prod
        action: upsert
      - key: cluster
        value: us-east-1
        action: insert
  attributes:
    actions:
      - key: http.user_agent           # noisy, drop it
        action: delete
      - key: db.statement              # PII risk; hash
        action: hash
```

### 2.5 Telemetry of telemetry

The collector emits its own metrics on `:8888/metrics` (configurable). The ones you must alert on:

| Metric | Meaning | Alert when |
|---|---|---|
| `otelcol_receiver_accepted_spans` (and `_metric_points`, `_log_records`) | Inbound throughput per receiver/transport | drops or spikes |
| `otelcol_receiver_refused_*` | Inbound rejected (e.g., memory_limiter pushing back) | sustained >0 |
| `otelcol_processor_dropped_spans` | Items dropped by processors (sampler dropping intentionally is fine; others are bugs) | by-processor breakdown |
| `otelcol_exporter_sent_*` | Outbound throughput | sudden drop |
| `otelcol_exporter_send_failed_*` | Failed sends (after retry exhausted) | sustained >0 |
| `otelcol_exporter_queue_size` | In-flight items in sending queue | > 80% of `queue_size` |
| `otelcol_exporter_queue_capacity` | Configured queue capacity | constant; for ratios |
| `otelcol_processor_batch_batch_size_trigger_send` | Batches sent because they hit max size (good) | — |
| `otelcol_processor_batch_timeout_trigger_send` | Batches sent because timeout fired (= you're not full) | sustained → reduce batch size |
| `otelcol_process_runtime_heap_alloc_bytes` | Heap | climbing toward limit_mib |
| `otelcol_process_uptime` | Restarts | unexpected drops |

The collector that monitors itself is itself observable. Scrape `:8888` from your normal Prometheus, build a dedicated dashboard for it, and treat the gateway tier as a first-class service with its own SLOs (see chapter 13). "Our metrics are slow today" almost always traces back to gateway saturation or a downed exporter.

### 2.6 Common collector gotchas

A focused list (the §11 grand-list at the end of the chapter has more):

1. **Receivers do not back-pressure SDKs by default.** Without `memory_limiter`, you OOM. With it, the SDK retries and you keep your data.
2. **`batch` after `tail_sampling`** is fine. **`batch` before `tail_sampling`** ruins everything: the sampler now needs to keep the whole batch in memory for `decision_wait`, multiplying memory by batch size.
3. **`k8sattributes` needs RBAC.** It calls the k8s API to look up pod metadata. Without `pods/list` and `pods/watch`, it logs warnings and emits no labels. The fields you expected on every span are silently absent; queries break.
4. **OTLP/HTTP and OTLP/gRPC are not interchangeable for receiving compressed payloads.** Some SDKs send `gzip` over HTTP/JSON, some send `gzip` over HTTP/protobuf. Pin the receiver to known protocols and reject the rest at the network edge.
5. **`prometheusremotewrite` exporter encodes timestamps as ms.** OTLP carries them as ns. If you don't put a `prometheus` translator in the pipeline (or use `prometheusremotewrite` directly which does it for you), you can lose sub-ms resolution. For Mimir at 15s scrape this is invisible; for sub-second high-frequency metrics, it matters.
6. **`extensions:` block is a separate top-level field.** If you put `file_storage` under `processors:` it silently won't load. The collector logs `unknown component file_storage` at startup; people miss this when looking at `kubectl logs` for the running pod (the message scrolled off).

---

## 3. Logs: Fluent Bit, Vector, and Why Logstash Doesn't Show Up Anymore

The OTel Collector now has a `filelog` receiver that can replace much of what Fluent Bit and Vector do. It is rapidly catching up but is not yet the operational default for the log path because:

- It uses more memory than Fluent Bit (Go GC vs C).
- Multiline parsing was a late addition and is less battle-tested.
- The Loki and OpenSearch exporters are in `contrib` and have rougher edges than the Vector or Fluent Bit equivalents.

So today, the real production patterns are:

- **Fluent Bit** as the agent (DaemonSet, tiny, C, predictable).
- **Vector** as either agent or gateway when you need richer transformation logic (VRL).
- **OTel Collector** for traces and metrics; sometimes also logs if your shop is OTLP-end-to-end.
- **Logstash** is dead for new builds. It is heavy (JVM), eager to consume memory, and its plugin ecosystem is largely subsumed by Vector and OTel. You'll see it in old ELK installs; don't add it to a green-field stack.

### 3.1 Fluent Bit deep dive

**What it is:** a 1-3 MB resident, C-based agent. CRI/JSON tail parsing, k8s metadata enrichment, ~80 input plugins, ~50 output plugins, Lua filters. SIMD-accelerated parsers. Streams in records, not files.

**The data model:** `(timestamp, tag, record)`. The `tag` is a routing key (think Kafka topic). Inputs assign tags; filters and outputs match on tag globs.

**Pipeline:** `INPUT → PARSER → FILTER → BUFFER → OUTPUT`. The buffer is per-output and on-disk if you enable it.

A representative DaemonSet config:

```ini
[SERVICE]
    Flush               1
    Daemon              Off
    Log_Level           info
    HTTP_Server         On
    HTTP_Listen         0.0.0.0
    HTTP_Port           2020
    storage.path        /var/log/flb-storage/
    storage.sync        normal
    storage.checksum    off
    storage.backlog.mem_limit 64M

[INPUT]
    Name                tail
    Path                /var/log/containers/*.log
    Path_Key            filename
    Parser              cri
    Tag                 kube.<namespace>.<pod_name>.<container_name>
    Tag_Regex           (?<pod_name>[^_]+)_(?<namespace>[^_]+)_(?<container_name>.+)-
    Read_from_Head      true
    Refresh_Interval    5
    Rotate_Wait         30
    Skip_Long_Lines     On
    DB                  /var/log/flb-tail.db
    DB.Sync             Normal
    Mem_Buf_Limit       50MB
    storage.type        filesystem
    multiline.parser    cri,docker

[FILTER]
    Name                kubernetes
    Match               kube.*
    Kube_URL            https://kubernetes.default.svc:443
    Merge_Log           On
    Keep_Log            Off
    K8S-Logging.Parser  On
    K8S-Logging.Exclude On
    Annotations         Off
    Labels              On
    Buffer_Size         32k

[FILTER]
    Name                modify
    Match               *
    Remove              kubernetes_labels.pod-template-hash
    Remove              kubernetes_labels.controller-revision-hash

[OUTPUT]
    Name                loki
    Match               kube.*
    Host                loki-distributor.loki.svc
    Port                3100
    Labels              cluster=us-east-1, $kubernetes['namespace_name'], $kubernetes['container_name']
    Auto_Kubernetes_Labels Off
    Line_Format         json
    storage.total_limit_size 5G
    Retry_Limit         5
```

Notes on the non-obvious bits:

- **`Read_from_Head true`**. Without this, after a Fluent Bit restart, it tails from the *end* of files. Anything written between the crash and the restart is silently dropped. This is one of the most common quietly-losing-logs misconfigs.
- **`DB`**. Fluent Bit uses an SQLite file to remember offsets per file. Mount this on a persistent volume or it forgets after restart and either re-reads everything (`Read_from_Head true`) or skips everything (default).
- **`storage.type filesystem`**. Without it, the buffer is memory-only. With it, when Loki is down, the buffer drains to `/var/log/flb-storage/` and survives restarts.
- **`Mem_Buf_Limit`**. When the in-memory buffer hits this, the input *pauses* (this is Fluent Bit's backpressure). With `storage.type filesystem` *and* `storage.backlog.mem_limit` set, the agent keeps reading and chunks overflow to disk.
- **`multiline.parser cri,docker`**. CRI splits long log lines at 16K. Without a multiline parser, a single Java stack trace becomes 30 separate "log lines" with garbled JSON. The `cri` parser reassembles them by looking at the partial-tag flag.
- **`kubernetes` filter**. This is what enriches with `namespace_name`, `pod_name`, `container_name`, `labels`. It calls the kube-apiserver. RBAC: `pods/list`, `pods/watch`. Without RBAC, you get unenriched records and no error in the user-facing logs.

### 3.2 Vector deep dive

**What it is:** a Rust-based, single-binary log/metric/trace shipper, with its own DSL ("VRL" — Vector Remap Language). Higher memory footprint than Fluent Bit (~50–200MB working set for a moderately-loaded gateway) but vastly more expressive transformation.

**The data model:** events of three types — `Log`, `Metric`, `Trace`. Every event is a typed record; VRL is statically checked against the schema.

**Pipeline:** `sources → transforms → sinks`, declared in TOML or YAML. Components are composable like a graph (one source can feed many transforms, transforms can fan out into different sinks).

```toml
[sources.k8s_logs]
  type = "kubernetes_logs"
  glob_minimum_cooldown_ms = 100
  use_apiserver_cache = true

[transforms.parse_json]
  type   = "remap"
  inputs = ["k8s_logs"]
  source = '''
    # decode the message field as JSON if it parses
    structured, err = parse_json(.message)
    if err == null {
      . = merge(., structured)
    }
    # drop fields we never query on
    del(.kubernetes.pod_uid)
    del(.kubernetes.pod_ips)

    # redact obvious PII patterns
    .message = redact(.message, filters: ["us_social_security_number"])

    # bucket high-cardinality user IDs into a hash
    if exists(.user_id) {
      .user_id_hash = sha2(string!(.user_id), variant: "SHA-256")
      del(.user_id)
    }
  '''

[transforms.route_by_severity]
  type   = "route"
  inputs = ["parse_json"]
  route.errors = '.level == "error" || .level == "fatal"'
  route.audit  = '.event_type == "audit"'

[sinks.loki_app]
  type   = "loki"
  inputs = ["route_by_severity._unmatched"]
  endpoint = "https://loki-distributor.loki.svc:3100"
  encoding.codec = "json"
  labels.cluster = "us-east-1"
  labels.namespace = "{{ kubernetes.pod_namespace }}"
  labels.app = "{{ kubernetes.pod_labels.app }}"
  buffer.type = "disk"
  buffer.max_size = 5_000_000_000

[sinks.loki_errors]
  type   = "loki"
  inputs = ["route_by_severity.errors"]
  endpoint = "https://loki-distributor.loki.svc:3100"
  labels.tier = "errors"
  labels.namespace = "{{ kubernetes.pod_namespace }}"
  encoding.codec = "json"

[sinks.s3_audit]
  type   = "aws_s3"
  inputs = ["route_by_severity.audit"]
  bucket = "company-audit-logs"
  key_prefix = "year=%Y/month=%m/day=%d/"
  compression = "zstd"
  encoding.codec = "ndjson"
  buffer.type = "disk"
  buffer.max_size = 50_000_000_000
```

VRL is the differentiator. Anything Fluent Bit does in a Lua filter is one line in VRL, type-checked, with bench-marked CPU cost. Multi-sink fan-out with per-sink filtering is native. The cost is operational footprint and config complexity.

### 3.3 The comparison table

| Dimension | Fluent Bit | Vector | OTel Collector (filelog) |
|---|---|---|---|
| Language / runtime | C | Rust | Go |
| Resident memory (idle) | 1–10 MB | 30–80 MB | 50–150 MB |
| CPU per MB log/sec ingested (rough) | best | ~1.5× FB | ~2× FB |
| K8s metadata enrichment | First-class | First-class | Yes (`k8sattributes`) |
| Multiline (Java stack, Python traceback) | Good (CRI/Docker built-in parsers) | Good | OK; rougher edges |
| Transformation language | Lua (limited) | VRL (rich, typed) | OTTL (rich, typed) |
| Hot reload of config | Yes (since 2.0) | Yes | Yes (`reloader`) |
| Disk buffer / WAL | Yes (`storage.type filesystem`) | Yes (`buffer.type disk`) | Yes (`file_storage` extension) |
| Output plugins | ~50 | ~40 | huge in `contrib` |
| Native OTLP support | OTLP output exists, marginal | OTLP source/sink (improving) | Native |
| Best as | Per-node agent | Gateway, any tier | Traces/metrics agent and gateway, increasingly logs too |

**When to pick which:**

- **Fleet-wide logs at <10K nodes, want minimum CPU per node:** Fluent Bit DaemonSet.
- **Need rich per-event transformation, multi-sink routing, schema-as-code:** Vector at the gateway tier (often after a Fluent Bit agent, sometimes Vector all the way down).
- **You're already running OTel Collector for traces/metrics and want one binary:** OTel Collector with `filelog` receiver, accepting that you'll do more tuning.
- **You need an event-streaming / ETL hybrid (e.g., logs → Kafka → ClickHouse with derived metrics):** Vector. It handles all three signals natively and the routing primitive is unbeatable.

---

## 4. Prometheus Scrape Model — In Detail

The OTel Collector can scrape too (its `prometheus` receiver embeds a fairly faithful Prometheus scrape engine), but most platforms still run Prometheus itself for the scrape tier and remote_write into the longer-term store. The semantics below are Prometheus's; understand them precisely.

### 4.1 Service discovery

Prometheus does not maintain a static target list in production. SD plugins fetch targets from a source on a poll:

| SD | Source | Use it for |
|---|---|---|
| `kubernetes_sd_configs` | Kube API: pods, endpoints, services, nodes, ingresses | Anything in k8s |
| `ec2_sd_configs` | AWS EC2 DescribeInstances | EC2 fleets outside k8s |
| `consul_sd_configs` | Consul catalog | HashiCorp / Nomad shops |
| `file_sd_configs` | A JSON file on disk | Hand-rolled or scripted target lists |
| `dns_sd_configs` | SRV records | Service mesh-adjacent |
| `static_configs` | Hardcoded list | Lab and bootstrap |

Each SD emits a list of `__address__` plus a bag of "meta" labels (e.g., `__meta_kubernetes_pod_label_app`). Those meta labels are not stored — they are inputs to the relabeling pipeline.

### 4.2 `relabel_configs` vs `metric_relabel_configs`

These two run at *different stages* and confusing them causes more wasted hours than any other Prometheus topic.

```
SD discovers target ──▶ relabel_configs ──▶ scrape /metrics ──▶ metric_relabel_configs ──▶ TSDB
                       (filter targets,                       (filter samples,
                        rewrite labels)                        rewrite labels)
```

- `relabel_configs` runs **once per target per SD refresh**. It decides *whether to scrape this target at all*, and *what labels to attach* (e.g., turn `__meta_kubernetes_pod_label_app` into `app`).
- `metric_relabel_configs` runs **once per sample per scrape**. It decides *whether to keep the sample* and *whether to rewrite its labels*.

The first is cheap (rare, small input set); the second is expensive (every sample, hot path).

If you want to **drop a target**, do it in `relabel_configs`. Doing it in `metric_relabel_configs` still pays the scrape cost.

If you want to **drop a high-cardinality label from a metric**, you must use `metric_relabel_configs` (the label doesn't exist until you've scraped).

```yaml
scrape_configs:
  - job_name: 'kubernetes-pods'
    kubernetes_sd_configs:
      - role: pod
    relabel_configs:
      # Only scrape pods with annotation prometheus.io/scrape=true
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
        action: keep
        regex: "true"
      # Override port with annotation if present
      - source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
        action: replace
        regex: ([^:]+)(?::\d+)?;(\d+)
        replacement: $1:$2
        target_label: __address__
      # Promote pod label `app` to a Prom label
      - source_labels: [__meta_kubernetes_pod_label_app]
        target_label: app
      - source_labels: [__meta_kubernetes_pod_name]
        target_label: pod
      - source_labels: [__meta_kubernetes_namespace]
        target_label: namespace
    metric_relabel_configs:
      # Drop the customer_id label everywhere — kills cardinality
      - action: labeldrop
        regex: customer_id
      # Drop a metric we know is broken / too noisy
      - source_labels: [__name__]
        regex: 'go_memstats_alloc_bytes_total'
        action: drop
      # Bucket request_id into "exists / not exists" if anything still emits it
      - source_labels: [request_id]
        regex: '.+'
        target_label: request_id
        replacement: 'present'
```

### 4.3 `honor_labels`

A subtle and dangerous flag. By default, when a scraped metric has a label that conflicts with the target's labels (e.g., the metric carries `instance="foo"` but the target's `instance` is `bar`), Prometheus *renames* the metric's label to `exported_instance`. With `honor_labels: true`, the metric's label wins.

You almost always want the default (`honor_labels: false`) **except** when scraping the Pushgateway or another aggregator that's emitting metrics on behalf of other producers. In that case the metric's labels are the source of truth and you must set `honor_labels: true`.

### 4.4 Federation, remote_write, and Agent mode — pick one

Three mechanisms get metrics out of one Prometheus into another store:

| Mechanism | Direction | What it sends | Use it for |
|---|---|---|---|
| **Federation** (`/federate`) | Pull (the upstream pulls) | A subset of series matched by label selectors, every scrape interval | Hierarchical Prom-of-Proms; small-fanout aggregation |
| **`remote_write`** | Push | Every sample, in real time, after relabel | Long-term storage (Mimir, Cortex, VM, Thanos Receiver) |
| **Agent mode** (`--enable-feature=agent`) | Push (forward only) | Every sample, no local TSDB at all | Edge clusters that should forward but never query locally |

Federation is the legacy of the pre-remote_write era. It scales poorly (the upstream's scrape interval × selector cost) and is brittle (a long pull blocks the next one). Use it only for narrow rollups or for compatibility with old hierarchies. New deployments should use `remote_write` or Agent mode.

Agent mode runs Prometheus with no query, no local storage, no compactor — only scrape and remote_write. Memory drops by 10×. You get back the `up{}` and metadata machinery that's awkward to replicate from a pure OTel scrape pipeline.

```yaml
# prometheus --enable-feature=agent --config.file=agent.yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'app'
    kubernetes_sd_configs: [...]

remote_write:
  - url: https://mimir-distributor.mimir.svc/api/v1/push
    queue_config:
      capacity: 20000
      max_samples_per_send: 5000
      max_shards: 200
      min_shards: 50
      batch_send_deadline: 5s
```

For deeper remote_write tuning (queue sizing, shard math, WAL compression), see the GPU observability `08-prometheus-metrics-design-and-cardinality.md` chapter §7 — same mechanism, same knobs.

### 4.5 Pushgateway — anti-pattern unless you know why

Pushgateway exists to let short-lived jobs (cron, batch, CI) push their metrics so Prometheus can later scrape them. It is widely misused. The official docs are explicit: don't use it as a general push proxy for service metrics.

Reasons:
- Pushgateway is a **stateful intermediary**. Series live there until manually deleted. A misconfigured deploy can leave dead series visible forever.
- It cannot be sharded. One instance is the bottleneck for everyone using it.
- It has no per-tenant quotas, no series limits.

The right use: a finite, well-defined set of CronJobs/CI builds that emit a known small set of metrics with stable labels. The push-and-forget pattern; you delete the group when the job is removed. Anything else — use OTLP push to the OTel Collector.

---

## 5. Tail Sampling for Traces

Head sampling decides at trace start (or at the first span) whether to keep a trace. The decision is encoded in the `trace_id` so all participants stay consistent. It is cheap and stateless. It is also blind: at decision time, no one knows whether this trace will end up being the one production failure in a million.

You want both:

```
HEAD SAMPLING                            TAIL SAMPLING
(per-process, decisive at start)         (per-collector, decisive after assembly)

Pros: cheap, simple, stateless           Pros: keeps the rare error trace
      consistent across services         Catches actual P99 latency
      no extra infra                     Composes policies (status, latency, attr)

Cons: blind to outcome                   Cons: stateful, memory-hungry
      tail of the latency dist           ~30s decision window delay
      drops the rare error               Requires a load balancer in front
```

Real systems run head sampling at a low base rate (1%, configured in the SDK) for cost protection, and then tail sampling on top to over-keep the interesting tail.

### 5.1 The assembly window problem

A trace with N spans across M services arrives at the collector tier as a stream of OTLP messages, *not* as one bundled trace. Spans of the same trace can arrive seconds apart (the leaf was a 5s DB query; the root span finishes after that). The collector must:

1. **Hold spans by `trace_id` in memory.**
2. **Wait long enough for "all" spans to arrive.** This is `decision_wait`; the standard value is 30s.
3. **Apply policies to the assembled trace** and keep / drop.

You cannot escape the wait. A trace whose root span exceeds `decision_wait` will be sampled with an incomplete view (later spans arrive, are checked against the already-made decision, and if "drop" was decided, are dropped — even if those late spans had an error).

Set `decision_wait` to longer than P99.9 of trace duration, and tag traces longer than that with a "long" attribute at the SDK so downstream systems know they may be missing tail spans.

### 5.2 The stateful collector pitfall — `loadbalancing` exporter

If your gateway tier has 4 collector replicas, span A of trace X may land on replica 1 and span B on replica 3. Each sees a fragment; neither can assemble the trace. Tail sampling silently degrades to "sample by individual span" — meaningless.

**Solution: a two-tier gateway.** First tier is a pool of stateless collectors whose only job is to route by `trace_id` to a stable second-tier replica. Second tier is the stateful tail-samplers.

```yaml
# Tier 1 — stateless router
exporters:
  loadbalancing:
    routing_key: traceID                          # ← critical
    protocol:
      otlp:
        timeout: 1s
        tls:
          insecure: true
    resolver:
      dns:
        hostname: otelcol-tail-sampler-headless.observability.svc
        port: 4317
        interval: 5s
        timeout: 1s

service:
  pipelines:
    traces:
      receivers:  [otlp]
      processors: [memory_limiter, batch]
      exporters:  [loadbalancing]
```

The `loadbalancing` exporter hashes `traceID` to the replica list. As long as the resolver returns the same set of replicas, span A and span B for trace X land on the same replica, and tail sampling works. Use a **headless service** so DNS returns all replica IPs.

Tier 2 (the `otelcol-tail-sampler` Deployment behind the headless service) runs the `tail_sampling` processor.

### 5.3 The `tail_sampling` processor

```yaml
processors:
  tail_sampling:
    decision_wait: 30s
    num_traces: 100000              # max in-memory traces
    expected_new_traces_per_sec: 5000
    policies:
      - name: errors
        type: status_code
        status_code: { status_codes: [ERROR] }

      - name: slow
        type: latency
        latency: { threshold_ms: 1000 }

      - name: rare-service
        type: string_attribute
        string_attribute:
          key: service.name
          values: ["fraud-engine", "payments"]

      - name: tagged-debug
        type: boolean_attribute
        boolean_attribute: { key: app.debug, value: true }

      - name: rate-limit-by-service
        type: composite
        composite:
          max_total_spans_per_second: 1000
          policy_order: [errors, slow, baseline]
          composite_sub_policy:
            - name: errors
              type: status_code
              status_code: { status_codes: [ERROR] }
            - name: slow
              type: latency
              latency: { threshold_ms: 500 }
            - name: baseline
              type: probabilistic
              probabilistic: { sampling_percentage: 1 }

      - name: baseline
        type: probabilistic
        probabilistic: { sampling_percentage: 1 }
```

Policy types you will actually use:

| Type | Decision basis | When to reach for it |
|---|---|---|
| `status_code` | OTel span status (`ERROR`, `OK`, `UNSET`) | Always — the cheapest "interesting trace" filter |
| `latency` | Trace duration vs threshold | Catch the long tail |
| `string_attribute` / `numeric_attribute` / `boolean_attribute` | Match on attr value | Keep traces from a specific tenant, debug-flagged, or rare service |
| `rate_limiting` | Cap total spans/sec kept (regardless of policy) | Cost protection backstop |
| `probabilistic` | Random N% | Baseline, must be present |
| `and` | AND of sub-policies | "Errors AND from prod AND in checkout" |
| `composite` | Try sub-policies in order with a global span/sec cap | The realistic production policy |

The OR is implicit: if **any** policy says keep, the trace is kept.

### 5.4 The memory math

Memory per trace ≈ sum of span sizes. A typical span in OTLP protobuf is ~1–4 KB; a typical microservice trace has ~30 spans. Call it 100 KB/trace.

```
peak memory ≈ num_traces × avg_trace_size
            = 100,000   × 100 KB
            = 10 GB
```

Plus protocol overhead, GC slack, etc. — budget 1.5–2× this.

**The tuning loop:**
1. Set `num_traces` and `expected_new_traces_per_sec` based on observed traffic plus 30% headroom.
2. Watch `otelcol_processor_tail_sampling_sampling_decision_latency` and `otelcol_processor_tail_sampling_global_count_traces_on_decisions`.
3. If memory climbs near `memory_limiter`, reduce `num_traces` first (you'll start dropping the oldest-but-still-undecided traces) before reducing `decision_wait`.
4. For deeper SLO-driven sampling policy (per-tenant budgets, per-service caps), see chapter 13.

### 5.5 Why you cannot batch before tail-sampling

`batch` flushes when full or when timeout fires. If `batch` is upstream of `tail_sampling` and `decision_wait` is 30s, the batch has to either:

- Sit and wait for `tail_sampling` (defeating the purpose of batching, and the spans for one trace are now spread across many small unfinished batches), or
- Be flushed and then tail_sampling has to keep them in memory anyway (doubling the memory cost).

**Always: `tail_sampling` first, then `batch`, then exporter.**

---

## 6. Edge-Side Cardinality and PII Control

The collection layer is the last place you can intervene cheaply. Once a high-cardinality label is in the TSDB, removing it costs you a re-write of every block that contains it. Once PII is in the log store, you owe a deletion campaign and possibly a notification.

### 6.1 Cardinality-killing transformations

For metrics, the OTel processors are `attributes`, `transform`, `filter`, and `metricstransform`. The Prometheus equivalent is `metric_relabel_configs`. Use them to:

- **Drop a label entirely.** `customer_id` should never be on a metric.
- **Hash a label.** When you need to differentiate customers in a metric but can't store the raw ID. SHA-256 truncated to 64 bits, encoded base32. Same customer → same hash, but no PII linkage.
- **Bucket a label.** Bucket `latency_bucket` by quantile range, `customer_tier` by enterprise/standard/free.
- **Drop a metric outright** if it's never queried.

```yaml
processors:
  transform/cardinality:
    metric_statements:
      - context: datapoint
        statements:
          # Drop the high-cardinality user_id from rate metrics
          - delete_key(attributes, "user_id") where metric.name == "http_requests_total"
          # Hash session_id
          - set(attributes["session_hash"], SHA256(attributes["session_id"]))
              where metric.name == "session_active"
          - delete_key(attributes, "session_id") where metric.name == "session_active"
          # Bucket request size into orders of magnitude
          - set(attributes["size_bucket"],
                Concat(["10^", string(Int(Log10(attributes["request_bytes"])))], ""))
              where metric.name == "http_request_size_bytes"
  filter/drop_metrics:
    metrics:
      metric:
        - 'name == "go_memstats_*_objects"'   # nobody queries this
```

For **traces and logs**, cardinality is less of a concern (the store doesn't index everything by default), but PII matters more.

### 6.2 PII redaction

```yaml
processors:
  attributes/pii:
    actions:
      - key: http.request.body                  # never log request bodies
        action: delete
      - key: db.statement
        action: hash
      - key: user.email
        action: delete
      - key: http.url
        action: update
        from_attribute: http.url
        # Strip query string
        # Use a `transform` processor for regex; this is illustrative
  transform/redact_log_body:
    log_statements:
      - context: log
        statements:
          # Mask credit card numbers in body
          - replace_pattern(body, "\\b(?:\\d[ -]*?){13,16}\\b", "[REDACTED-CARD]")
          # Mask SSNs
          - replace_pattern(body, "\\b\\d{3}-\\d{2}-\\d{4}\\b", "[REDACTED-SSN]")
          # Mask email addresses except domain
          - replace_pattern(body, "([a-zA-Z0-9._%+-]+)@", "[REDACTED]@")
```

**Why redact at the agent/gateway, not the store:**

1. **Once it's at the store, it's been on disk and in memory across multiple machines.** Even if you delete it, you have a hot replica, an object-store block, a backup snapshot, an LTS replica. Deleting one is not deleting all.
2. **Most stores don't support partial-record redaction.** You can delete a log line; you cannot delete the credit card from inside a log line and leave the rest.
3. **It violates the "if you don't need it, don't store it" rule.** Storage is the wrong place to filter — the further left you push redaction, the smaller your blast radius.
4. **Compliance scope**. Auditors map PII to the systems that touch it. Every system the data crosses unredacted is in scope. Pinning redaction to the agent contracts the scope to that one tier.

The hard part of redaction is the long tail: a custom application puts SSNs in a log message, your regex doesn't match the format, you've shipped them. Two countermeasures:

1. **Schema-as-code for log fields** (OTel semantic conventions plus your own internal extension). Reject events that violate.
2. **A canary tester**. A scheduled CI job emits a known fake-PII event into staging logs; an automated check searches for it in the store 5 minutes later. If it's there, the redaction is broken.

---

## 7. Backpressure, Retry, and Durability

The single most important property of the collection layer: **never let telemetry block the application**. The Sentry SDK pioneered this principle in the early 2010s ("if Sentry is down, your app continues"); every modern observability stack inherits it.

The chain of buffers:

```
Producer (SDK)
   ├─ in-memory queue (small, e.g. 2048 spans)
   ├─ retry with exponential backoff
   └─ on overflow → drop oldest (loudly logged metric)
        │
        ▼
Agent (collector or Fluent Bit)
   ├─ in-memory queue
   ├─ disk buffer (file_storage / storage.type filesystem)
   └─ on overflow → drop or backpressure to producer
        │
        ▼
Gateway
   ├─ in-memory queue
   ├─ persistent queue (file_storage extension)
   ├─ retry with exponential backoff
   └─ on persistent failure → drop with metric
        │
        ▼
Transport (Kafka — chapter 05)
   └─ Durable for backend outage of arbitrary length
        │
        ▼
Backend store
```

Each tier should be sized so the next-shorter-tier outage is absorbed. Specifically:

| Tier outage absorbed | Required buffer somewhere |
|---|---|
| Brief gateway hiccup (<60s) | Agent in-memory queue |
| Gateway restart (rolling deploy) | Agent in-memory + small disk |
| Backend (Mimir/Tempo) outage of N minutes | Gateway disk WAL ≥ N × send rate |
| Backend outage > 1h, multi-tenant | Kafka transport tier (chapter 05) |
| Cross-region backend loss | Same, replicated |

### 7.1 The "drop on overflow vs block producer" tradeoff

You configure each level to either:

- **Drop on overflow.** When the buffer is full, discard new data. Loud metric (`refused_*`), no producer impact.
- **Block producer.** When the buffer is full, refuse new data — the producer's send call returns an error, and the SDK's behavior dictates retry / drop.

Drop on overflow is the default and the right default for application latency. Block-producer is correct for *high-stakes* signals (audit logs, billing events) where missing one is worse than slowing the application. Most platforms split:

- Default pipeline: drop on overflow, large file-backed queue.
- Audit pipeline: block producer (or even synchronous write to Kafka), separate quotas, separate dashboards.

The choice is made per-pipeline. Pretending one knob fits everything is the error.

### 7.2 The persistent queue gotcha

`file_storage` survives restarts. It does **not** survive node loss. If the collector pod's PVC is on local SSD (`emptyDir` or `hostPath`), node failure = queue loss.

Two patterns:

1. **Local SSD + Kafka tier downstream.** The collector's file_storage absorbs short outages; Kafka absorbs long ones. You accept that a node loss might lose <30s of buffered data.
2. **Network-attached durable storage** (EBS, PD, Azure Disk). Survives node loss, but write latency is higher and a slow disk now affects collector throughput.

In practice, (1) is the default for trace/log data. (2) is rare and only for compliance-driven deployments where the collector is the durability tier and there is no Kafka.

---

## 8. Operational Concerns

### 8.1 Sizing collectors

Rule of thumb (based on contrib OTel Collector v0.115, x86_64, no exotic processors):

| Signal | Throughput per vCPU | Memory per 10K events/sec |
|---|---|---|
| Traces (OTLP, no tail sampling) | ~30K spans/sec | ~256 MB |
| Traces (with tail sampling, `num_traces=100K`) | ~10K spans/sec | ~10 GB (the trace cache) |
| Metrics (OTLP, batch only) | ~50K data points/sec | ~256 MB |
| Metrics (with `transform`/`filter`/k8sattributes) | ~25K data points/sec | ~512 MB |
| Logs (OTLP, batch only) | ~40K records/sec | ~256 MB |
| Logs (with redaction) | ~15K records/sec | ~512 MB |

These are starting points; benchmark your config in staging before sizing prod.

For Fluent Bit at the agent: ~25-50 MB/sec/core ingestion, 10-50 MB resident memory. For Vector: ~10-30 MB/sec/core, 50-200 MB resident.

### 8.2 HPA on collectors

CPU-based HPA works for the stateless tier (the `loadbalancing` router). For the stateful tail-sampling tier, scaling out invalidates routing — half the trace_ids re-hash to new replicas, and you lose assembly for ~`decision_wait`. So:

- **Stateless tier:** HPA on CPU + queue depth (`otelcol_exporter_queue_size`).
- **Stateful tier:** vertical scaling (bigger pods) over horizontal. If you must scale horizontally, do it during a maintenance window or accept ~30s of degraded sampling.

For HPA on queue depth (a metric, not CPU), use `kube-prometheus-stack`'s `prometheus-adapter` or KEDA:

```yaml
# KEDA ScaledObject
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: otelcol-gateway-hpa
spec:
  scaleTargetRef:
    name: otelcol-gateway
  minReplicaCount: 4
  maxReplicaCount: 32
  triggers:
    - type: prometheus
      metadata:
        serverAddress: http://prometheus.observability.svc:9090
        threshold: '0.7'
        query: |
          max(otelcol_exporter_queue_size / otelcol_exporter_queue_capacity)
```

### 8.3 Rolling upgrades without losing data

The collector's graceful shutdown:
1. Stop accepting new connections (drop from Service endpoints).
2. Drain receivers (allow open OTLP streams to finish).
3. Drain processors (flush batch, finalize tail sampling decisions for traces in window).
4. Drain exporters (flush sending queue).
5. Exit.

Configure:

```yaml
service:
  telemetry:
    logs: { level: info }
  extensions: [health_check, file_storage]
```

```bash
# In the pod spec
terminationGracePeriodSeconds: 60
preStop:
  exec:
    command: ["/bin/sh", "-c", "sleep 10 && /otelcol --shutdown"]
```

The `preStop` sleep gives the kube-proxy time to remove the pod from Service endpoints (the lame-duck period) before SIGTERM hits the process.

For the tail-sampling tier, also consider **a `decision_wait`-aligned drain**. A pod terminating mid-window means in-flight traces never reach a decision. With `file_storage`, the in-flight cache survives a restart — but only if you've enabled it for the tail sampler, which is uncommon.

### 8.4 Multi-region collection topology

Three patterns, in order of preference:

1. **Region-local agent + region-local gateway, cross-region replication via Kafka or via storage layer.** Each region's collectors talk only to their region's storage; chapter 05 / chapter 06 handle cross-region. Lowest WAN cost; survives region isolation.
2. **Region-local agent, central gateway in one region.** WAN-heavy; central gateway is a SPOF. Use only when your storage tier is also single-region.
3. **One global gateway, agents push directly across regions.** Don't.

### 8.5 Secrets management

OTLP exporters often need API keys (Honeycomb, Datadog, Lightstep). The OTel Collector reads them via env vars or files; never in plain config:

```yaml
exporters:
  otlphttp/honeycomb:
    endpoint: https://api.honeycomb.io
    headers:
      x-honeycomb-team: ${env:HONEYCOMB_API_KEY}
```

Mount the secret as a file or env var via your normal k8s secret mechanism (or external-secrets-operator with Vault/AWS Secrets Manager). Rotate via deployment restart.

For mTLS to internal backends (Mimir, Tempo, Loki), use cert-manager + reload triggered by the file_storage extension's filewatch. Hot-reload on cert rotation is supported and worth setting up — restart-on-rotate is a worse user experience for the platform team.

---

## 9. A Worked Example — Production Gateway Config

Tier-2 OTel Collector gateway (post-`loadbalancing` router) doing: OTLP ingest, batching, redaction, tail sampling, cardinality drops, and fan-out to Mimir + Tempo + Loki, with persistent queue and `memory_limiter` correctly placed.

```yaml
# otelcol-gateway-tier2.yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
        max_recv_msg_size_mib: 32
      http:
        endpoint: 0.0.0.0:4318

processors:
  # 1. ALWAYS FIRST. Refuse work when the heap is over the soft cap.
  memory_limiter:
    check_interval: 1s
    limit_mib: 7000          # pod has 8Gi, leave 1Gi headroom for GC
    spike_limit_mib: 1000

  # 2. Enrich with cluster + region + tenant — resource not attributes.
  resource/enrich:
    attributes:
      - { key: deployment.environment, value: prod, action: upsert }
      - { key: cluster, value: us-east-1, action: insert }

  # 3. Look up k8s metadata for the source pod (RBAC: pods/list, pods/watch).
  k8sattributes:
    auth_type: serviceAccount
    passthrough: false
    extract:
      metadata: [k8s.namespace.name, k8s.pod.name, k8s.node.name]
      labels:
        - { tag_name: k8s.app, key: app, from: pod }
        - { tag_name: k8s.team, key: team, from: pod }
    pod_association:
      - sources:
          - { from: resource_attribute, name: k8s.pod.uid }
      - sources:
          - { from: connection }

  # 4. Redact PII from spans and logs. (Metrics pipeline doesn't use this one.)
  attributes/redact:
    actions:
      - { key: http.request.body, action: delete }
      - { key: http.user_agent, action: delete }
      - { key: db.statement, action: hash }
      - { key: user.email, action: delete }
      - { key: http.target, action: update, from_attribute: http.target }   # see transform below

  transform/redact_body:
    log_statements:
      - context: log
        statements:
          - replace_pattern(body, "\\b(?:\\d[ -]*?){13,16}\\b", "[REDACTED-CARD]")
          - replace_pattern(body, "\\b\\d{3}-\\d{2}-\\d{4}\\b", "[REDACTED-SSN]")

  # 5. Drop high-cardinality metric labels before remote_write.
  transform/cardinality:
    metric_statements:
      - context: datapoint
        statements:
          - delete_key(attributes, "user_id")
          - delete_key(attributes, "session_id")
          - delete_key(attributes, "request_id")
          - delete_key(attributes, "trace_id")
          # Bucket http.target into route templates if it leaked raw paths
          - set(attributes["http.route"], attributes["http.target"])
              where attributes["http.route"] == nil

  # 6. Tail sampling. Comes BEFORE batch.
  tail_sampling:
    decision_wait: 30s
    num_traces: 100000
    expected_new_traces_per_sec: 5000
    policies:
      - { name: errors,  type: status_code, status_code: { status_codes: [ERROR] } }
      - { name: slow,    type: latency,     latency: { threshold_ms: 1000 } }
      - { name: payments, type: string_attribute,
          string_attribute: { key: service.name, values: [payments, fraud-engine] } }
      - { name: baseline, type: probabilistic, probabilistic: { sampling_percentage: 1 } }

  # 7. Batch is LAST. Large batches → fewer exporter RPCs → lower CPU.
  batch:
    send_batch_size: 8192
    send_batch_max_size: 16384
    timeout: 5s

exporters:
  prometheusremotewrite/mimir:
    endpoint: https://mimir-distributor.mimir.svc/api/v1/push
    headers:
      X-Scope-OrgID: ${env:TENANT_ID}
    sending_queue:
      enabled: true
      num_consumers: 10
      queue_size: 10000
      storage: file_storage
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 5m
    timeout: 30s
    resource_to_telemetry_conversion:
      enabled: true     # promote resource attrs to metric labels (controlled by our transform)

  otlp/tempo:
    endpoint: tempo-distributor.tempo.svc:4317
    tls: { insecure: true }
    sending_queue:
      enabled: true
      num_consumers: 10
      queue_size: 5000
      storage: file_storage
    retry_on_failure: { enabled: true, max_elapsed_time: 5m }

  loki:
    endpoint: https://loki-distributor.loki.svc:3100/loki/api/v1/push
    headers:
      X-Scope-OrgID: ${env:TENANT_ID}
    default_labels_enabled:
      exporter: false
      job: true
    sending_queue:
      enabled: true
      num_consumers: 10
      queue_size: 5000
      storage: file_storage
    retry_on_failure: { enabled: true, max_elapsed_time: 5m }

extensions:
  health_check:
    endpoint: 0.0.0.0:13133
  pprof:
    endpoint: 0.0.0.0:1777
  zpages:
    endpoint: 0.0.0.0:55679
  file_storage:
    directory: /var/lib/otelcol/queue
    timeout: 5s
    compaction:
      directory: /var/lib/otelcol/queue
      on_start: true
      on_rebound: true
      rebound_needed_threshold_mib: 100
      rebound_trigger_threshold_mib: 200

service:
  extensions: [health_check, pprof, zpages, file_storage]
  telemetry:
    logs: { level: info }
    metrics:
      level: detailed
      address: 0.0.0.0:8888
  pipelines:
    traces:
      receivers:  [otlp]
      processors: [memory_limiter, resource/enrich, k8sattributes,
                   attributes/redact, tail_sampling, batch]
      exporters:  [otlp/tempo]

    metrics:
      receivers:  [otlp]
      processors: [memory_limiter, resource/enrich, k8sattributes,
                   transform/cardinality, batch]
      exporters:  [prometheusremotewrite/mimir]

    logs:
      receivers:  [otlp]
      processors: [memory_limiter, resource/enrich, k8sattributes,
                   transform/redact_body, batch]
      exporters:  [loki]
```

Annotations on what's load-bearing:

- `memory_limiter` is first in every pipeline. The collector cannot OOM into a corrupted file_storage WAL.
- `tail_sampling` is in the **traces** pipeline only and comes before `batch`. Memory is dominated by the trace cache, sized via `num_traces`.
- `transform/cardinality` is in the **metrics** pipeline only. Without it, `user_id` leaks from a misbehaving service and Mimir's per-tenant series limits kick in 6 hours later.
- `redact` processors run before `batch` so the redacted form is what's sent. Redacting after batch would mean the batch buffer holds raw PII for up to `timeout`.
- All exporters use `sending_queue` with `storage: file_storage`. PVC is sized for ~30 minutes of peak send rate; beyond that we depend on chapter 05's Kafka tier.
- `retry_on_failure.max_elapsed_time: 5m` means we give up on a sample after 5 minutes of retries. Tune up if downstream backups span longer.
- `prometheusremotewrite.headers.X-Scope-OrgID` carries the tenant ID for Mimir multi-tenancy (chapter 19).

---

## 10. Common Pitfalls Specific to the Collection Layer

In rough order of "frequency we see it in design reviews".

1. **`batch` placed before `tail_sampling`.** The sampler waits 30s; the batch sits in memory for those 30s. Memory blows up. **Fix:** `tail_sampling` first, `batch` second.
2. **`memory_limiter` placed after `batch` (or omitted).** Heap climbs uncapped during traffic spikes; pod is OOMKilled mid-flush; persistent queue can corrupt. **Fix:** `memory_limiter` is *always* first.
3. **Tail-sampling tier behind a vanilla Service (round-robin LB).** Spans for one trace land on different replicas, no replica has the whole trace, sampling silently degrades. **Fix:** `loadbalancing` exporter with `routing_key: traceID` in front.
4. **`k8sattributes` processor without RBAC.** Pod metadata silently absent; downstream queries that filter on `k8s.namespace` return nothing. **Fix:** `pods/list`, `pods/watch` on a dedicated ServiceAccount; verify with `kubectl auth can-i`.
5. **Fluent Bit `tail` without `Read_from_Head: true`.** After restart, agent skips everything written during the gap. No alert; logs just disappear. **Fix:** `Read_from_Head true` plus a persistent DB file at `DB`.
6. **No persistent queue on the gateway.** A 10-minute backend incident during a deploy = 10 minutes of telemetry lost. **Fix:** `file_storage` extension on a sized PVC; size for the longest backend MTTR you've seen.
7. **`relabel_configs` confused with `metric_relabel_configs`.** Relabeling samples in `relabel_configs` (which only runs at SD time) does nothing. Filtering targets in `metric_relabel_configs` runs *after* the scrape — too late. **Fix:** target-level concerns in `relabel_configs`; sample-level in `metric_relabel_configs`.
8. **High-cardinality labels added at the producer, drop attempted at the TSDB.** Wrong layer — by then you've paid the ingest cost. **Fix:** drop at the collector with `transform` / `attributes` / `metric_relabel_configs`; consider rejecting unknown labels at the gateway via a denylist.
9. **Pushgateway used as a general-purpose push proxy.** Series persist forever, no quotas, single point of failure. **Fix:** OTel Collector OTLP push for service metrics; reserve Pushgateway for short-lived batch jobs only.
10. **OTLP exporter without `compression: gzip` or `compression: zstd` set.** Default is no compression; you pay 4-10× egress. **Fix:** explicitly set compression on every exporter; verify in `otelcol_exporter_sent_bytes`.
11. **Two collectors on the same node both subscribing to the same source.** Common with side-by-side migration (Fluent Bit + OTel Collector both tailing `/var/log/containers/*.log`). Logs ingested twice, charged twice. **Fix:** one tailer per file class; the other should be downstream of it (OTel Collector's `filelog` can read from Fluent Bit's forward output, etc.).
12. **No `terminationGracePeriodSeconds` tuned.** Pod gets SIGTERM, drains take longer than the grace, kubelet sends SIGKILL, in-flight batches die. **Fix:** `terminationGracePeriodSeconds: 60` plus a `preStop` sleep ≥ 10s for the lame-duck period.
13. **`honor_labels: true` set on a normal scrape** (because someone copied a Pushgateway example). Targets' own labels (`instance`, `job`) get clobbered by metric labels of the same name; queries that depend on `instance=<host>` go sideways. **Fix:** default to `honor_labels: false`; turn it on only for true federation/aggregation sources.

---

## 11. Glossary / Mental Model Summary

| Term | Precise meaning in this chapter |
|---|---|
| **Agent** | Per-node collector process. Reads host signals, tails files, pushes to gateway. |
| **Gateway** | Centralized collector tier. Stateful processing, vendor fan-out, durability. |
| **Receiver** | Component that ingests data from somewhere into the collector. |
| **Processor** | In-flight transformation between receiver and exporter. |
| **Exporter** | Component that writes to a backend / next hop. |
| **Pipeline** | Ordered chain `receivers → processors → exporter` for one signal type. |
| **Extension** | Out-of-band component (health, pprof, file_storage). Not in the data path. |
| **Memory limiter** | Backpressure processor that refuses receives when heap is full. |
| **Persistent queue** | Disk-backed exporter queue (`file_storage`) that survives restarts. |
| **Head sampling** | Per-process decision at trace start; cheap, blind. |
| **Tail sampling** | Decision after trace assembly, in a stateful collector tier. |
| **Decision wait** | Window the tail sampler holds spans before deciding. Must exceed P99.9 trace duration. |
| **Loadbalancing exporter** | Routes by `traceID` to keep all spans of a trace on one downstream replica. |
| **Relabel configs** | Run at SD time; decide *whether* to scrape a target and *what* labels it has. |
| **Metric relabel configs** | Run per sample after scrape; decide *whether* to keep a sample and rewrite its labels. |
| **Honor labels** | When true, scraped metric's own labels win conflicts with target's labels. Used for Pushgateway-class targets only. |
| **Agent mode (Prometheus)** | `--enable-feature=agent`: scrape + remote_write only, no local TSDB. |
| **Pushgateway** | Stateful intermediary for short-lived job metrics. Anti-pattern for general use. |
| **Resource (OTel)** | The producing entity (service/host/pod). One per process. |
| **Attributes (OTel)** | Per-event K/V pairs (per span/log/data point). |

The single mental model to take away: **the collection layer is the only point in the stack that has full observability semantics, low latency, and the ability to refuse work**. Everything upstream is application code (you can't refuse it). Everything downstream is storage cost (you can't unwrite it). Decisions made here — what to keep, what to drop, what to redact, when to backpressure — define both the cost and the correctness of the rest of the pipeline.

---

**TL;DR.** Run an agent + gateway two-tier with OTel Collector everywhere it makes sense, Fluent Bit at the agent for cheap log shipping, Vector wherever you need richer transformation, and Prometheus in `--enable-feature=agent` mode for pull-based metric collection. Always place `memory_limiter` first and `batch` last in every pipeline; place `tail_sampling` before `batch` and put a `loadbalancing` exporter in front of any tail-sampling tier; redact PII at the gateway before exporters; drop high-cardinality labels here, not at the TSDB; configure the `file_storage` extension on every exporter for backend-outage durability and back it with Kafka (chapter 05) for outages longer than your disk; size `decision_wait` to exceed P99.9 trace duration; treat the collector itself as a first-class service with its own SLOs, dashboards, and pages — because once this layer fails, everything downstream is asymptotically less useful.
