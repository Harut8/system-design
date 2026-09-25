# SRE & Observability — Labs

A learn-by-doing task sheet for the 43 chapters in this folder. The chapters are the theory. This sheet makes you build the stack, break it on purpose, and measure what happens. Nearly every task runs on one laptop with one docker-compose stack.
- **Closed book first.** Try each task before rereading the chapter. Reread only when you get stuck.
- **Predict before you run.** Write the number you expect, then measure it.
- **Write results down.** Every task ends in a number, an output or a file.
- **Spaced review.** Redo each checkpoint from memory 1 day, 1 week and 1 month later.

## How to use this sheet
- Do Setup once. Then work in chapter order 00 → 18. Chapters 19–42 can be done in any order after that; each one names the earlier labs it builds on.
- Tick `- [ ]` boxes as you finish. Core tasks are the minimum. Stretch tasks are for a second pass.
- Keep `lab-notebook.md` next to the compose file. For each task record: date, prediction, measured result, the gap between them, and one sentence on why.
- Spacing: answer the checkpoint questions closed book when you finish a chapter. Answer them again after 1 day, 1 week and 1 month. Any question you miss goes back into the next review.
- Do not fix the demo service between chapters unless a task says to. Several later labs rely on a flaw an earlier lab found.
- Related practice elsewhere: Kubernetes mechanics in [`../k8s-learn/`](../k8s-learn/README.md); GPU telemetry in [`../gpu-observability/tasks.md`](../gpu-observability/tasks.md). This sheet links to them and does not repeat their tasks.

## Setup

| Environment | Cost | Used by |
|---|---|---|
| Laptop + Docker (≥ 8 GB RAM free, 4 cores), the `obs-lab` compose below | free | almost every chapter |
| Compose profiles `kafka`, `db`, `chaos`, `load` (same file) | free | 05, 16, 23–25, 29, 36, 38 |
| `kind` cluster + Helm (Istio or Linkerd, Chaos Mesh, Falco) | free | 22, 24.3, 27.3, 38.2 |
| Linux host or Linux VM (Lima, multipass) with `perf` / `bpftrace` | free | 09.5, 24.4 |
| Ollama with a small model (`qwen2.5:0.5b`) | free | 26 |
| Python 3.12 + `duckdb`, `numpy` on the host | free | 16, 30, 35, 39 |
| Any SaaS vendor trial or managed cloud Prometheus | paid, optional | 37.4, 39 (optional comparisons only) |

**Layout.** Create this tree once. Every lab refers to these paths.

```
obs-lab/
  compose.yaml        otelcol.yaml     tempo.yaml     alertmanager.yml   blackbox.yml
  prometheus/prometheus.yml            prometheus/rules/*.yml
  grafana/provisioning/datasources/ds.yaml   grafana/provisioning/dashboards/files.yaml   grafana/dashboards/
  app/Dockerfile  app/main.py  app/worker.py         k6/load.js
```

**`compose.yaml`** (image versions are early-2025 releases that work together; change them one at a time):

```yaml
name: obs-lab
x-app: &app
  build: ./app
  cap_add: [SYS_PTRACE]                     # py-spy (ch09, ch42)
  environment: &appenv
    OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
    OTEL_TRACES_EXPORTER: otlp
    OTEL_METRICS_EXPORTER: otlp
    OTEL_LOGS_EXPORTER: otlp
    OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED: "true"
    OTEL_SEMCONV_STABILITY_OPT_IN: http     # stable HTTP metric/attr names
    OTEL_METRIC_EXPORT_INTERVAL: "10000"
    PYROSCOPE_URL: http://pyroscope:4040
    REDIS_URL: redis://redis:6379/0
    INVENTORY_URL: http://inventory:8001
services:
  shop:      { <<: *app, ports: ["8000:8000"], environment: { <<: *appenv, OTEL_SERVICE_NAME: shop, PORT: "8000" } }
  inventory: { <<: *app, ports: ["8001:8001"], environment: { <<: *appenv, OTEL_SERVICE_NAME: inventory, PORT: "8001" } }
  worker:    { <<: *app, command: ["opentelemetry-instrument", "python", "worker.py"],
               environment: { <<: *appenv, OTEL_SERVICE_NAME: worker } }
  redis:     { image: "redis:7.4" }
  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.115.0
    command: ["--config=/etc/otelcol/config.yaml"]
    volumes: ["./otelcol.yaml:/etc/otelcol/config.yaml:ro"]
    ports: ["4317:4317", "4318:4318", "8888:8888", "55679:55679"]
  prometheus:
    image: prom/prometheus:v3.1.0
    command: [--config.file=/etc/prometheus/prometheus.yml, --storage.tsdb.path=/prometheus,
              --web.enable-lifecycle, --web.enable-admin-api, --web.enable-remote-write-receiver,
              --web.enable-otlp-receiver, "--enable-feature=exemplar-storage,native-histograms"]
    volumes: ["./prometheus:/etc/prometheus:ro", "prom-data:/prometheus"]
    ports: ["9090:9090"]
  alertmanager:
    image: prom/alertmanager:v0.28.0
    command: [--config.file=/etc/am/alertmanager.yml]
    volumes: ["./alertmanager.yml:/etc/am/alertmanager.yml:ro"]
    ports: ["9093:9093"]
  alert-sink: { image: "mendhak/http-https-echo:latest" }   # webhook receiver; read it with `docker compose logs alert-sink`
  blackbox:
    image: prom/blackbox-exporter:v0.25.0
    command: [--config.file=/etc/bb/blackbox.yml]
    volumes: ["./blackbox.yml:/etc/bb/blackbox.yml:ro"]
  loki:      { image: "grafana/loki:3.3.2", command: ["-config.file=/etc/loki/local-config.yaml"], ports: ["3100:3100"] }
  tempo:
    image: grafana/tempo:2.7.0
    user: "0"
    command: ["-config.file=/etc/tempo.yaml"]
    volumes: ["./tempo.yaml:/etc/tempo.yaml:ro"]
    ports: ["3200:3200"]
  pyroscope: { image: "grafana/pyroscope:1.10.0", ports: ["4040:4040"] }
  grafana:
    image: grafana/grafana:11.5.0
    environment: { GF_AUTH_ANONYMOUS_ENABLED: "true", GF_AUTH_ANONYMOUS_ORG_ROLE: Admin, GF_AUTH_DISABLE_LOGIN_FORM: "true" }
    volumes: ["./grafana/provisioning:/etc/grafana/provisioning:ro", "./grafana/dashboards:/var/lib/grafana/dashboards:ro"]
    ports: ["3000:3000"]
  # ---- optional profiles: docker compose --profile kafka --profile db up -d
  kafka:
    image: apache/kafka:3.9.0
    profiles: [kafka]
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_LISTENERS: PLAINTEXT://:9092,CONTROLLER://:9093
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
  postgres:  { image: "postgres:17", profiles: [db], environment: { POSTGRES_PASSWORD: lab },
               command: ["postgres", "-c", "shared_preload_libraries=pg_stat_statements,auto_explain"] }
  toxiproxy: { image: "ghcr.io/shopify/toxiproxy:2.9.0", profiles: [chaos], ports: ["8474:8474"] }
  k6:
    image: grafana/k6:0.56.0
    profiles: [load]
    volumes: ["./k6:/scripts:ro"]
    environment: { K6_PROMETHEUS_RW_SERVER_URL: http://prometheus:9090/api/v1/write }
volumes: { prom-data: {} }
```

**`otelcol.yaml`**. The app sends to the collector. The collector sends traces to Tempo and logs to Loki over OTLP, and it exposes metrics for Prometheus to scrape.

```yaml
receivers:
  otlp: { protocols: { grpc: { endpoint: 0.0.0.0:4317 }, http: { endpoint: 0.0.0.0:4318 } } }
processors:
  memory_limiter: { check_interval: 1s, limit_mib: 400 }
  batch: {}
exporters:
  otlp/tempo:   { endpoint: tempo:4317, tls: { insecure: true } }
  otlphttp/loki: { endpoint: http://loki:3100/otlp }
  prometheus:   { endpoint: 0.0.0.0:8889, enable_open_metrics: true }   # open_metrics => exemplars
  debug:        { verbosity: basic }
extensions:
  health_check: { endpoint: 0.0.0.0:13133 }
  zpages:       { endpoint: 0.0.0.0:55679 }
service:
  extensions: [health_check, zpages]
  telemetry: { metrics: { address: 0.0.0.0:8888 } }
  pipelines:
    traces:  { receivers: [otlp], processors: [memory_limiter, batch], exporters: [otlp/tempo] }
    metrics: { receivers: [otlp], processors: [memory_limiter, batch], exporters: [prometheus] }
    logs:    { receivers: [otlp], processors: [memory_limiter, batch], exporters: [otlphttp/loki] }
```

**`prometheus/prometheus.yml`**:

```yaml
global: { scrape_interval: 15s, evaluation_interval: 15s }
rule_files: [/etc/prometheus/rules/*.yml]
alerting: { alertmanagers: [{ static_configs: [{ targets: [alertmanager:9093] }] }] }
scrape_configs:
  - job_name: otel-apps              # app metrics re-exposed by the collector
    honor_labels: true               # keep job=<service.name> from the exporter (see 04 §4.3)
    static_configs: [{ targets: [otel-collector:8889] }]
  - job_name: stack                  # the observability stack observing itself (ch28)
    static_configs: [{ targets: [otel-collector:8888, prometheus:9090, loki:3100, tempo:3200,
                                 pyroscope:4040, alertmanager:9093, grafana:3000] }]
```

**`tempo.yaml`** (single binary, local storage, metrics-generator on):

```yaml
stream_over_http_enabled: true
server: { http_listen_port: 3200 }
distributor: { receivers: { otlp: { protocols: { grpc: { endpoint: 0.0.0.0:4317 }, http: { endpoint: 0.0.0.0:4318 } } } } }
storage: { trace: { backend: local, wal: { path: /var/tempo/wal }, local: { path: /var/tempo/blocks } } }
metrics_generator:
  storage: { path: /var/tempo/gen-wal, remote_write: [{ url: "http://prometheus:9090/api/v1/write", send_exemplars: true }] }
overrides: { defaults: { metrics_generator: { processors: [service-graphs, span-metrics] } } }
```

**`alertmanager.yml`** and **`blackbox.yml`**:

```yaml
# alertmanager.yml
route: { receiver: sink, group_by: [alertname, service], group_wait: 30s, group_interval: 5m, repeat_interval: 4h }
receivers: [{ name: sink, webhook_configs: [{ url: "http://alert-sink:8080/" }] }]
---
# blackbox.yml
modules:
  http_2xx: { prober: http, timeout: 5s }
```

**Grafana provisioning.** `ds.yaml` sets fixed datasource UIDs so the signals link to each other. `files.yaml` loads every JSON file in `grafana/dashboards/`:

```yaml
# grafana/provisioning/datasources/ds.yaml
apiVersion: 1
datasources:
  - { name: Prometheus, uid: prom, type: prometheus, url: "http://prometheus:9090", isDefault: true,
      jsonData: { exemplarTraceIdDestinations: [{ name: trace_id, datasourceUid: tempo }] } }
  - { name: Loki, uid: loki, type: loki, url: "http://loki:3100" }
  - { name: Tempo, uid: tempo, type: tempo, url: "http://tempo:3200",
      jsonData: { serviceMap: { datasourceUid: prom }, tracesToLogsV2: { datasourceUid: loki, filterByTraceID: true } } }
  - { name: Pyroscope, uid: pyro, type: grafana-pyroscope-datasource, url: "http://pyroscope:4040" }
---
# grafana/provisioning/dashboards/files.yaml (separate file)
apiVersion: 1
providers: [{ name: lab, type: file, options: { path: /var/lib/grafana/dashboards } }]
```

**The demo service.** One image runs as `shop`, `inventory` and `worker`. Auto-instrumentation comes from `opentelemetry-instrument`. You write the handlers. The skeleton below gives only the parts the labs depend on.

```dockerfile
# app/Dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" httpx redis prometheus-client pyroscope-io py-spy \
      opentelemetry-distro opentelemetry-exporter-otlp && opentelemetry-bootstrap -a install
COPY . .
CMD ["sh", "-c", "opentelemetry-instrument uvicorn main:app --host 0.0.0.0 --port $PORT"]
```

```python
# app/main.py (skeleton). Labs add to it.
import asyncio, logging, os, random, time
import httpx, pyroscope, redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
pyroscope.configure(application_name=os.environ["OTEL_SERVICE_NAME"], server_address=os.environ["PYROSCOPE_URL"])
app, log = FastAPI(), logging.getLogger("app")
FAULTS = {"error_rate": 0.0, "latency_ms": 0, "latency_rate": 0.0, "cpu_ms": 0}

@app.post("/admin/faults")              # the fault-injection knob every lab uses
async def set_faults(f: dict) -> dict: FAULTS.update(f); return FAULTS

def burn_cpu(ms: int) -> None: ...      # busy loop for `ms` ms; keep the name, ch09 hunts for it
async def inject() -> None: ...         # apply latency_rate/latency_ms, cpu_ms, error_rate (raise HTTPException(500))

@app.get("/checkout")                   # shop: await inject(); GET {INVENTORY_URL}/inventory/{sku}; LPUSH a job to Redis "jobs"
async def checkout(user: int = 0) -> dict: ...
@app.get("/inventory/{sku}")            # inventory: await inject(); return stock
async def inventory(sku: str) -> dict: ...
@app.get("/healthz")
async def healthz() -> dict: return {"ok": True}
# app/worker.py: loop { BRPOP jobs; sleep 20–80 ms; log "processed" }
```

**Load generator** `k6/load.js`. Run it with `docker compose --profile load run --rm -e RPS=20 -e DUR=10m k6 run /scripts/load.js`:

```js
import http from 'k6/http';
import { check } from 'k6';
export const options = { scenarios: { steady: { executor: 'constant-arrival-rate', rate: Number(__ENV.RPS || 20),
  timeUnit: '1s', duration: __ENV.DUR || '10m', preAllocatedVUs: 50, maxVUs: 500 } } };
export default function () {
  const r = http.get(`${__ENV.TARGET || 'http://shop:8000'}/checkout?user=${Math.floor(Math.random() * 1000)}`);
  check(r, { 'status 200': (x) => x.status === 200 });
}
```

**Smoke test.** `docker compose up -d --build`, then run 2 minutes of load. In Grafana (http://localhost:3000) Explore you should see: `http_server_request_duration_seconds_count{job="shop"}` in Prometheus, `{service_name="shop"}` in Loki, `{ resource.service.name = "shop" }` in Tempo, and a `shop` flame graph in Pyroscope.

Conventions used below:
- `REQ` means `http_server_request_duration_seconds`, the stable HTTP semconv histogram. Its labels are `http_route`, `http_request_method` and `http_response_status_code`.
- A fault is set with `curl -XPOST localhost:8000/admin/faults -H 'content-type: application/json' -d '{"error_rate":0.05}'`. Use port 8001 for inventory. Reset with all zeros.
- Collector self-metrics are written with a `_total` suffix. Some versions differ, so check the real names with `curl -s localhost:8888/metrics | grep otelcol_`.

---
## 00 — Mental Models  ([chapter](00-mental-models.md))
**Time:** ~3 h · **Needs:** core stack, k6

- [ ] **00.1 Fill the USE × RED matrix** *(Level: Core)*
  - **Goal:** Turn §4.3 into queries you can run.
  - **Do:** Make a table with rows `shop`, `inventory`, `worker`, `redis`, `otel-collector` and columns Rate / Errors / Duration / Utilization / Saturation / Errors(resource). Write one PromQL or LogQL query per cell, or write "no signal".
  - **Predict:** How many cells will say "no signal"? Which component has the fewest signals?
  - **Verify:** Every query returns data under load. The empty cells become instrumentation work in 03.1. For CPU throttling as a saturation signal, see [`../k8s-learn/resources-tasks.md`](../k8s-learn/resources-tasks.md).
- [ ] **00.2 Serial composition, predicted and measured** *(Level: Core)*
  - **Goal:** Check the §15.2 formula against real traffic.
  - **Do:** Set `error_rate` 0.01 on inventory and 0.005 on shop. Run 10 min at 30 RPS. Measure the end-to-end ratio `sum(rate(REQ_count{job="shop",http_response_status_code=~"5.."}[10m])) / sum(rate(REQ_count{job="shop"}[10m]))`.
  - **Predict:** Compute 1 − (0.99 × 0.995) before you run.
  - **Verify:** The measured ratio is within ±0.3 pp of the prediction. If it is not, find the retry or error-mapping behaviour that explains the gap.
- [ ] **00.3 Mean vs percentiles, and percentiles don't compose** *(Level: Core)*
  - **Goal:** Watch §16.1 and §16.5 happen.
  - **Do:** Set `latency_rate` 0.02 and `latency_ms` 2000 on shop. Compare the mean (`rate(REQ_sum[5m]) / rate(REQ_count[5m])`), p50 and p99. Then run `docker compose up -d --scale shop=2` with the port mapping removed, and compare `avg(histogram_quantile(0.99, sum by (le, instance) (rate(REQ_bucket{job="shop"}[5m]))))` with `histogram_quantile(0.99, sum by (le) (rate(REQ_bucket{job="shop"}[5m])))`.
  - **Predict:** Which of mean, p50 and p99 moves by more than 10×? Is the average of per-instance p99s above or below the global p99?
  - **Verify:** Record all five numbers. p50 barely moves. The mean rises by about 40 ms (0.02 × 2 s). p99 lands inside the bucket that holds 2 s; it is an interpolated value, not exactly 2 s.
- [ ] **00.4 Little's Law on live traffic** *(Level: Core)*
  - **Goal:** Use §16.3 as a way to check your instruments against each other.
  - **Do:** Add an `UpDownCounter` `shop.inflight` around the handler. At 50 RPS with `latency_ms` 200 on every request, compute L = λ × W from PromQL.
  - **Predict:** L ≈ 50 × 0.2 = 10, plus the base latency.
  - **Verify:** `avg_over_time(shop_inflight[5m])` is within 15 % of λW. If it is not, one of the two instruments is wrong. Find which one.
- [ ] **00.5 The M/M/1 knee** *(Level: Stretch)*
  - **Goal:** Reproduce the §16.4 curve.
  - **Do:** Write a sync `def` endpoint that calls `burn_cpu(50)`. Run a single uvicorn worker with `--limit-concurrency 1`, so μ ≈ 20/s. Run k6 constant-arrival-rate at 10, 16, 18 and 19 RPS, 3 min each.
  - **Predict:** W = 1/(μ − λ) at each rate: 100, 250, 500 and 1000 ms.
  - **Verify:** Plot measured p50 against predicted. Both curves turn sharply upward above 80 % utilization.

**Checkpoint (closed book):**
1. What does USE measure, and what does RED measure? Why is this a matrix, not a choice between them?
2. Three services in series, each 99.9 % available. What is the combined availability, and roughly how much downtime is that per 30 days?
3. Why can't you average per-pod p99 values to get the fleet p99?
<details><summary>Answers</summary>

1. USE (Utilization, Saturation, Errors) describes resources. RED (Rate, Errors, Duration) describes request-serving services. Every service consumes resources, so you need both views for each component.
2. 0.999³ ≈ 99.7 %, which is about 2.2 h per 30 days.
3. Quantiles are not linear. The fleet p99 depends on the full merged distribution. Aggregate the bucket counts (`sum by (le)`) first, then compute the quantile.
</details>

---

## 01 — Architecture and Stack  ([chapter](01-architecture-and-stack.md))
**Time:** ~2.5 h · **Needs:** core stack

- [ ] **01.1 Walk the hops with counters** *(Level: Core)*
  - **Goal:** Make §1.1 concrete. For each hop there should be a counter that proves data passed through it.
  - **Do:** For one span, list each hop (SDK → collector receiver → exporter → Tempo distributor → ingester → query) with its port. Next to each hop, write the metric that proves passage, e.g. `otelcol_receiver_accepted_spans_total`, `otelcol_exporter_sent_spans_total`, `tempo_distributor_spans_received_total`. Do the same for one log line and one metric sample.
  - **Verify:** Under load, `rate()` of every metric on the path is non-zero, and the rates agree within 5 %.
- [ ] **01.2 Failure per hop** *(Level: Core)*
  - **Goal:** Fill in the §9 failure table from experiments, not from memory.
  - **Do:** Under 20 RPS, `docker compose stop` one backend at a time (tempo, loki, prometheus, otel-collector) for 3 min, then start it again.
  - **Predict:** For each one: is data buffered, retried, or lost? Does the app notice? Does a gap show in Grafana after recovery?
  - **Verify:** A 4-row table with columns symptom, `otelcol_exporter_queue_size`, `otelcol_exporter_send_failed_*_total`, and "gap after recovery (s)". Stopping the collector itself should lose data at the SDK. Confirm this from the SDK's export error logs.
- [ ] **01.3 Compute the lab's budget and scale it** *(Level: Core)*
  - **Goal:** Apply §6.5 to real numbers.
  - **Do:** Measure samples/s (`rate(prometheus_tsdb_head_samples_appended_total[5m])`), active series (`prometheus_tsdb_head_series`), log bytes/s (`rate(loki_distributor_bytes_received_total[5m])`) and spans/s at 20 RPS. Extrapolate linearly to 300 services at 500 RPS each.
  - **Verify:** A one-row budget per signal. State which of the three §11 reference sizes your extrapolation falls into.
- [ ] **01.4 Decision log for the lab** *(Level: Stretch)*
  - **Goal:** Explain it: answer the §12 questions for a real-sounding org.
  - **Do:** Write one page, "Decision log for a 40-service company", to a new platform hire. Each §12 question gets a one-line answer and a number from 01.3.
  - **Verify:** A peer can reconstruct your topology (§4, agent + gateway or not) from the page alone.

**Checkpoint (closed book):**
1. Name the layers from instrumentation to consumption, in order.
2. Why run both a node-local agent and a gateway tier instead of one?
3. Which hop is the most dangerous to lose without noticing, and why?
<details><summary>Answers</summary>

1. Instrumentation → node agent → gateway/collector → transport/buffer → per-signal storage (metrics, logs, traces, profiles) → query layer → consumption (dashboards, alerts).
2. The agent handles local concerns: host metadata, cheap filtering, and a local buffer. The gateway handles central, stateful work: tail sampling, redaction policy, routing, and credentials. Keeping them apart lets each scale and fail on its own.
3. The collector or gateway hop. It sits between the SDK and the backends, so when it drops data the dashboards simply look quiet. Nothing errors, and nobody notices unless the platform watches itself (chapter 28).
</details>

---

## 02 — OpenTelemetry Deep Dive  ([chapter](02-opentelemetry-deep-dive.md))
**Time:** ~3 h · **Needs:** core stack

- [ ] **02.1 Hand-craft a `traceparent`** *(Level: Core)*
  - **Goal:** Learn the §4.1 wire format and the §6.3 flag bit.
  - **Do:** `curl -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' localhost:8000/checkout`. Then send the same request with flags `00` and a new trace id.
  - **Predict:** Which of the two traces appears in Tempo, given the default `parentbased_always_on` sampler?
  - **Verify:** `curl localhost:3200/api/traces/4bf92f3577b34da6a3ce929d0e0e4736` returns spans from shop and inventory. The trace sent with flags `00` returns 404.
- [ ] **02.2 Baggage is not an attribute** *(Level: Core)*
  - **Goal:** Separate §3.4 baggage from span attributes.
  - **Do:** Send `baggage: tenant=acme`. In inventory, read it with `opentelemetry.baggage.get_baggage("tenant")` and copy it onto the current span.
  - **Predict:** Before you add the copy, does `{ span.tenant = "acme" }` match anything?
  - **Verify:** It matches 0 traces before the change and all of them after.
- [ ] **02.3 Delta vs cumulative temporality** *(Level: Core)*
  - **Goal:** See the §7.3 landmine in the raw data.
  - **Do:** Add a `debug` exporter with `verbosity: detailed` to the metrics pipeline. Run shop once with `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta` and once without. Grep the collector logs for `AggregationTemporality` and the counter values.
  - **Predict:** What happens to the value of a counter between exports in each mode? What would `rate()` compute if delta points were stored as a Prometheus counter?
  - **Verify:** Paste three consecutive data points from each mode into your notebook. Delta values reset every interval; cumulative values keep growing.
- [ ] **02.4 A View that removes series** *(Level: Core)*
  - **Goal:** Reshape the stream at the SDK, as in §7.4.
  - **Do:** Configure the MeterProvider programmatically (see 42 §3.2) with a View on `http.server.request.duration` that keeps only `http.route`, `http.request.method` and `http.response.status_code`, and sets 6 explicit buckets.
  - **Predict:** Series count before and after, from buckets × label combinations.
  - **Verify:** `count(REQ_bucket{job="shop"})` matches your prediction.
- [ ] **02.5 Head sampling keeps traces whole** *(Level: Stretch)*
  - **Goal:** Check that §6.1 ratio sampling drops whole traces, not individual spans.
  - **Do:** Set `OTEL_TRACES_SAMPLER=parentbased_traceidratio` and `OTEL_TRACES_SAMPLER_ARG=0.1` on shop only. Run 10 min.
  - **Predict:** Spans/s at the collector compared with 100 % sampling. Will inventory spans ever appear without their shop parent?
  - **Verify:** `rate(otelcol_receiver_accepted_spans_total[5m])` drops to about 10 %. `{ resource.service.name = "inventory" }` returns no root-less traces.

**Checkpoint (closed book):**
1. What are the four fields of `traceparent`, and what does the last one control?
2. Why is delta temporality dangerous when the backend is Prometheus?
3. What is the difference between Resource and InstrumentationScope?
<details><summary>Answers</summary>

1. version, trace-id (16 bytes), parent span-id (8 bytes), and trace-flags. Bit 0 of the flags is "sampled", and parent-based samplers downstream follow it.
2. Prometheus expects cumulative counters and handles resets itself. If delta points are stored as a counter, every interval looks like a reset, so `rate()` and `increase()` return garbage. Delta has to be converted to cumulative, which is stateful and has to happen in one place.
3. Resource says who emitted the data: service.name, host, k8s pod. It is the same for the whole process. Scope says which library or instrumentation produced it: name and version.
</details>

---

## 03 — Instrumentation  ([chapter](03-instrumentation.md))
**Time:** ~4 h · **Needs:** core stack

- [ ] **03.1 Minimum viable instrumentation for the worker** *(Level: Core)*
  - **Goal:** Apply §2.2 to a queue consumer, which auto-instrumentation leaves mostly dark.
  - **Do:** Add a counter `jobs.processed{outcome}`, a histogram `job.duration`, and a gauge for queue depth read from Redis `LLEN jobs`. Fill in the worker's cells in the 00.1 matrix.
  - **Verify:** All three appear in Prometheus. Stopping the worker makes the queue-depth gauge grow linearly at the enqueue rate.
- [ ] **03.2 Find the gap: propagate through the Redis queue** *(Level: Core)*
  - **Goal:** Reproduce the §5.4 async break, then fix it.
  - **Do:** Look at a `/checkout` trace in Tempo. Then put `propagate.inject(carrier)` into the job payload in shop, and in the worker `ctx = propagate.extract(carrier)` followed by `tracer.start_as_current_span("process job", context=ctx, kind=SpanKind.CONSUMER)`.
  - **Predict:** Before the fix, how many traces does one checkout produce? After the fix, what does the time between the LPUSH span and the worker span mean?
  - **Verify:** Before the fix, the worker spans are separate root traces. After it, the waterfall shows one trace with a visible gap. That gap equals queue wait: stop the worker for 30 s and the gap grows to about 30 s.
- [ ] **03.3 Exemplars end to end** *(Level: Core)*
  - **Goal:** Link a metric to a trace in one click (§3.3, §3.5).
  - **Do:** Put a latency panel in Grafana on `histogram_quantile(0.99, sum by (le) (rate(REQ_bucket{job="shop"}[5m])))` and turn on Exemplars. Inject `latency_rate` 0.05.
  - **Verify:** Exemplar diamonds appear on the slow band. Clicking one opens a Tempo trace longer than `latency_ms`. If there are no exemplars, check `docker run --rm --network obs-lab_default curlimages/curl -s -H 'Accept: application/openmetrics-text' http://otel-collector:8889/metrics | grep '# {'`.
- [ ] **03.4 Logs carry trace context** *(Level: Core)*
  - **Goal:** Enforce the §4.1 structured-log contract.
  - **Do:** Log one `checkout done` line per request with `order_id` and `user` as `extra=`. Find a trace id in Tempo, then query `{service_name="shop"} | trace_id="<id>"` in Loki.
  - **Verify:** You get exactly one line per service involved in that trace. Grafana's "Logs for this span" button works.
- [ ] **03.5 Tail-based log sampling** *(Level: Stretch)*
  - **Goal:** Measure what §4.5 saves.
  - **Do:** Emit 20 DEBUG lines per request into a per-request buffer held in a contextvar. Flush the buffer only if the request fails. Compare with logging everything at DEBUG.
  - **Predict:** Byte reduction at a 1 % error rate.
  - **Verify:** `sum(rate(loki_distributor_bytes_received_total[5m]))` for both modes. The ratio should be close to your prediction, and failed requests still have all their DEBUG lines.
- [ ] **03.6 PII test at the source** *(Level: Stretch)*
  - **Goal:** Test the §14.1 allowlist and §14.2 hash-and-bucket rules.
  - **Do:** Write a pytest that runs `/checkout` in-process with an `InMemorySpanExporter` and a log capture handler. It fails if any attribute or log line matches an email regex, or if the raw `user` value appears.
  - **Verify:** The test fails on the current code. It passes after you replace `user` with `hash(user) % 64`.

**Checkpoint (closed book):**
1. Why is a histogram preferred over a summary for request latency?
2. Where does trace context get lost in a queue-based handoff, and what restores it?
3. What makes a log "structured" in the sense chapter 03 requires?
<details><summary>Answers</summary>

1. Histogram buckets can be summed across instances and quantiles computed afterward, and bucket boundaries can support SLO thresholds. A summary's quantiles are computed per process and cannot be aggregated.
2. At the enqueue/dequeue boundary. The in-process `Context` does not travel with the message. Inject `traceparent` into the message payload or headers and extract it in the consumer, as a parent or a span link.
3. Machine-parseable key/value fields (usually JSON) with a stable schema: timestamp, level, service, trace_id, span_id, and event-specific fields. It must not be free text with values interpolated into the message.
</details>

---

## 04 — Collection and Edge  ([chapter](04-collection-and-edge.md))
**Time:** ~4 h · **Needs:** core stack

- [ ] **04.1 Head vs tail sampling: lose the slow trace, then keep it** *(Level: Core)*
  - **Goal:** Show that head sampling cannot keep outliers and tail sampling can (§5.3).
  - **Do:** Set inventory `latency_rate` 0.01 and `latency_ms` 1500. First run the SDK at `parentbased_traceidratio` 0.05. Then switch the SDK back to always-on and add a `tail_sampling` processor with policies `status_code: ERROR`, `latency: threshold_ms: 1000`, and `probabilistic: 5`. Run 10 min at 30 RPS each time. Count slow traces with the Tempo search API (`/api/search?q={ duration > 1s }&limit=1000`, with start and end) piped to `jq '.traces | length'`.
  - **Predict:** The share of the ~180 slow requests you will find in each mode.
  - **Verify:** About 5 % with head sampling, about 100 % with tail sampling. Spans/s stored stays close to 5 % + 1 % in both modes.
- [ ] **04.2 Break tail sampling with a short `decision_wait`** *(Level: Core)*
  - **Goal:** Hit the §5.1 assembly-window problem.
  - **Do:** Set `decision_wait: 1s` while inventory sleeps 3 s on 1 % of requests.
  - **Predict:** Will the latency policy keep the 3 s traces?
  - **Verify:** Most slow traces are now missing or fragmented. `otelcol_processor_tail_sampling_sampling_late_span_age` (or the equivalent in your version) shows late spans. Set `decision_wait` just above your p99.9 and the traces come back.
- [ ] **04.3 `relabel_configs` vs `metric_relabel_configs`, and `honor_labels`** *(Level: Core)*
  - **Goal:** Learn which phase can see which labels (§4.2, §4.3).
  - **Do:** Try to drop `http_request_method` with `relabel_configs`, then with `metric_relabel_configs` using `action: labeldrop`. Then remove `honor_labels: true`.
  - **Predict:** Which attempt changes the series? What does removing `honor_labels` do to the `job` label?
  - **Verify:** Only `metric_relabel_configs` works. Without `honor_labels`, series gain `exported_job="shop"` and `job="otel-apps"`, which breaks every `job="shop"` query in this sheet.
- [ ] **04.4 Redact at the edge** *(Level: Core)*
  - **Goal:** Enforce §6.2 in the gateway, not in each service.
  - **Do:** Make shop put `enduser.id` and a fake `http.request.header.authorization` on its spans. Add a `transform` processor with `set(attributes["enduser.id"], SHA256(attributes["enduser.id"]))` and `delete_key(attributes, "http.request.header.authorization")` in `trace_statements`.
  - **Verify:** `{ span.http.request.header.authorization != nil }` returns 0 traces. `enduser.id` values are 64-hex.
- [ ] **04.5 Persistent queue vs in-memory queue** *(Level: Stretch)*
  - **Goal:** Measure the §7.2 durability trade-off.
  - **Do:** Stop Tempo. Send exactly 3000 requests with k6 (`iterations`). Restart the collector. Start Tempo again. Count spans stored. Repeat with a `file_storage` extension and `sending_queue: { storage: file_storage }` on `otlp/tempo`. Mount a writable volume for it.
  - **Predict:** Spans lost in each run.
  - **Verify:** All spans queued at the time of the restart are lost with the memory queue and none are lost with the file queue. Record both counts.

**Checkpoint (closed book):**
1. Why can't you put `batch` (or split traces across collectors) before `tail_sampling` without a `loadbalancing` exporter?
2. What does `honor_labels: true` do, and when is it correct?
3. Name the processors that belong in every collector pipeline, in the right order.
<details><summary>Answers</summary>

1. The tail sampler needs every span of a trace in one process before it decides. If spans of one trace land on different collectors, each one sees a fragment and decides differently. The `loadbalancing` exporter routes by trace id to keep them together.
2. It keeps the target's own `job` and `instance` labels instead of overwriting them with the scrape config's. It is correct for exporters that re-expose other sources' metrics, such as the collector or a Pushgateway.
3. `memory_limiter` first. Then enrichment and filtering (k8sattributes/resource, filter/transform/redaction, tail_sampling for traces). Then `batch` last, before the exporters.
</details>

---

## 05 — Transport and Buffering  ([chapter](05-transport-and-buffering.md))
**Time:** ~3.5 h · **Needs:** `--profile kafka`

- [ ] **05.1 Put Kafka between agent and gateway** *(Level: Core)*
  - **Goal:** Build the §10 topology in miniature.
  - **Do:** Add a second collector, `otel-gateway`. The agent exports with `kafka: { brokers: [kafka:9092], topic: otlp_spans, encoding: otlp_proto, partition_traces_by_id: true }`. The gateway has a `kafka` receiver on the same topic with `group_id: otel-gateway` and exports to Tempo.
  - **Verify:** Traces still arrive. `kafka-consumer-groups.sh --bootstrap-server kafka:9092 --describe --group otel-gateway` (under `/opt/kafka/bin/`) shows lag near 0.
- [ ] **05.2 Slow consumer blows the retention window** *(Level: Core)*
  - **Goal:** Reproduce §9.2.
  - **Do:** `kafka-configs.sh --bootstrap-server kafka:9092 --alter --entity-type topics --entity-name otlp_spans --add-config retention.ms=60000,segment.ms=10000`. Stop the gateway for 4 min under load, then start it.
  - **Predict:** How much of the 4-minute window will reach Tempo?
  - **Verify:** Tempo has a gap of roughly (4 min − 1 min retention) and the gateway logs an offset reset. Write down the lesson as retention ≥ max expected outage × safety factor.
- [ ] **05.3 Compression codecs on real OTLP** *(Level: Core)*
  - **Goal:** Put numbers on §2.4.
  - **Do:** Create 4 topics and run the agent with `producer: { compression: none | gzip | lz4 | zstd }`, 5 min each at the same RPS. Measure each topic's size with `kafka-log-dirs.sh --describe --bootstrap-server kafka:9092 --topic-list <t>`.
  - **Predict:** The ratio order and the rough size of zstd's ratio.
  - **Verify:** A table of bytes per codec, plus agent CPU from `docker stats --no-stream`.
- [ ] **05.4 Partition key and consumer parallelism** *(Level: Stretch)*
  - **Goal:** See why §2.2 calls the key "the most consequential choice".
  - **Do:** Recreate the topic with 6 partitions and run 2 gateways with tail sampling. Compare `partition_traces_by_id: true` with `false`.
  - **Predict:** What fraction of slow traces the latency policy keeps in each mode.
  - **Verify:** With `false`, spans of one trace split across gateways and some traces are stored as fragments. Count traces with a missing root: `{ } | count() > 0` shows them as partial in Tempo's UI.
- [ ] **05.5 Design note: no queue** *(Level: Core)*
  - **Goal:** Explain it (§11).
  - **Do:** Write 5 sentences to a team lead proposing Kafka for a 15-service shop: why they don't need it yet, and which measured signal (from 01.3) would change your mind.
  - **Verify:** The note names a threshold number and a metric.

**Checkpoint (closed book):**
1. What does a durable queue buy a telemetry pipeline that a collector's memory queue does not?
2. Why partition trace data by trace id?
3. What decides the retention you need on a telemetry topic?
<details><summary>Answers</summary>

1. It absorbs backend outages and bursts for hours instead of seconds. It decouples producers from storage. Several consumers (hot store and lakehouse) can read the same stream, and data can be replayed.
2. So every span of a trace lands on one partition, and therefore on one consumer, which stateful processing like tail sampling or service graphs needs.
3. The longest consumer outage you must survive (including time to detect and fix it) plus the backfill time, with margin. Size it with bytes/s × retention.
</details>

---

## 06 — Metrics Storage  ([chapter](06-metrics-storage.md))
**Time:** ~3.5 h · **Needs:** core stack

- [ ] **06.1 Open the TSDB** *(Level: Core)*
  - **Goal:** See the §2 on-disk layout.
  - **Do:** `docker compose exec prometheus promtool tsdb list -r /prometheus` and `... promtool tsdb analyze /prometheus`. Also `ls /prometheus/wal /prometheus/chunks_head`.
  - **Verify:** Record the block count, the head series, and the top 5 label pairs by series count. Explain each directory in one line.
- [ ] **06.2 Bytes per sample: counter vs noisy gauge** *(Level: Core)*
  - **Goal:** Test the §3.3 figure of ~1.37 B/sample.
  - **Do:** Expose two `prometheus_client` metrics from shop on a side port: a steadily increasing counter and a `random.random()` gauge, each with 500 label values. Wait for a compacted block (≥ 2 h), then read NUM SAMPLES and SIZE from `promtool tsdb list`, or use `prometheus_tsdb_compaction_chunk_size_bytes_sum / prometheus_tsdb_compaction_chunk_samples_sum`.
  - **Predict:** Which compresses worse, and by how much?
  - **Verify:** Counters come close to ~1–2 B/sample. Random float gauges are several times larger, because XOR has almost nothing to exploit.
- [ ] **06.3 RAM per active series** *(Level: Core)*
  - **Goal:** Measure the §5.2 figure of ~3 KiB/series.
  - **Do:** Serve 100k, 300k and 1M synthetic series (a 10-line `prometheus_client` script, or avalanche) from a scrape target. After each step settles, read `process_resident_memory_bytes{job="stack",instance="prometheus:9090"}` and `prometheus_tsdb_head_series`.
  - **Predict:** RSS at 1M series.
  - **Verify:** Plot RSS against series and compute the slope in bytes/series.
- [ ] **06.4 Crash and WAL replay** *(Level: Core)*
  - **Goal:** See §2.3 and §2.4.
  - **Do:** At 1M series, `docker kill -s KILL obs-lab-prometheus-1`, then `docker compose start prometheus`. Time how long until `/-/ready` returns 200.
  - **Predict:** Replay time, and whether scraped data from before the kill survives.
  - **Verify:** The log line reporting WAL replay duration, and the gap in a graph, which should equal the downtime only.
- [ ] **06.5 Classic vs native histogram** *(Level: Stretch)*
  - **Goal:** Measure §14.4 cardinality collapse and §14.5 accuracy.
  - **Do:** Add a View with `ExponentialBucketHistogramAggregation()` to one histogram. Send it with a second collector pipeline, `otlphttp/prom: { endpoint: "http://prometheus:9090/api/v1/otlp" }`. Feed it a known lognormal latency.
  - **Predict:** Series per label set (classic: buckets + 2; native: 1), and which p99 is closer to the true value.
  - **Verify:** `histogram_quantile(0.99, sum(rate(<native>[5m])))` against the exact value from `numpy.percentile` on the generated samples.
- [ ] **06.6 Same series, other engine** *(Level: Stretch)*
  - **Goal:** Check the §9.2 claim.
  - **Do:** Add `victoriametrics/victoria-metrics` as a second `remote_write` target at the 1M-series step.
  - **Verify:** Bytes/series for VM compared with Prometheus, from both processes' RSS.

**Checkpoint (closed book):**
1. What makes Gorilla/XOR compression effective, and what kind of data defeats it?
2. Roughly how much RAM does 1M active series cost in Prometheus?
3. What is in the head block, and what protects it from a crash?
<details><summary>Answers</summary>

1. Delta-of-delta timestamps are almost always 0 at a fixed scrape interval. Consecutive values XOR to mostly-zero bits when they change slowly. Random or high-entropy floats and jittery timestamps defeat it.
2. About 3 KiB per series, so ~3 GiB for the head. In production, with queries and remote_write, expect 4–6 GiB.
3. The last ~2 h of samples, held in memory (with mmapped chunks). The WAL, which is replayed on restart.
</details>

---

## 07 — Logs Storage  ([chapter](07-logs-storage.md))
**Time:** ~3.5 h · **Needs:** core stack; ClickHouse for the stretch task

- [ ] **07.1 The stream-cardinality knife edge** *(Level: Core)*
  - **Goal:** Reproduce §4.5.
  - **Do:** Push to Loki directly: `POST /loki/api/v1/push` with one stream per request, `{app="bad", req="<uuid>"}`, from a Python loop at 200 lines/s. Compare with one stream `{app="good"}` that puts the uuid in the line.
  - **Predict:** Which limit you hit first, and the error message.
  - **Verify:** `loki_ingester_memory_streams` climbs until pushes are rejected with a stream-limit error (`loki_discarded_samples_total{reason=~".*stream.*"}`). The good stream never hits a limit.
- [ ] **07.2 Measure your compression ratio** *(Level: Core)*
  - **Goal:** Put a number on §8 for your own log lines.
  - **Do:** Run 30 min of load. `curl -XPOST localhost:3100/flush`. Compare `loki_distributor_bytes_received_total` with `docker compose exec loki du -sb /loki/chunks`.
  - **Predict:** A ratio from the §8.2 tricks.
  - **Verify:** Record the ratio. Repeat with a random 32-byte token appended to each line and explain the change (§8.3).
- [ ] **07.3 Label vs structured metadata vs line filter** *(Level: Core)*
  - **Goal:** Learn what Loki indexes and what it scans (§4.1, §4.6).
  - **Do:** Time and read "bytes processed" in the query inspector for three queries over 1 h: `{service_name="shop"} |= "order_id=123"`, `{service_name="shop"} | trace_id="<id>"`, and `{service_name=~".+"} |= "order_id=123"`.
  - **Verify:** A table of bytes processed per query. Say which query prunes by stream and which scans every stream.
- [ ] **07.4 Same logs, columnar engine** *(Level: Stretch)*
  - **Goal:** Compare the §5 archetype.
  - **Do:** Load 5M synthetic lines into `clickhouse/clickhouse-server` with `ORDER BY (service, ts)` and a `bloom_filter` skipping index on `user_id`. Run "errors for user 42 in the last hour" there and as LogQL.
  - **Predict:** Which reads fewer bytes.
  - **Verify:** `read_bytes` from `system.query_log` against Loki's bytes processed.
- [ ] **07.5 Pick a log store** *(Level: Core)*
  - **Goal:** Explain it (§12, §14).
  - **Do:** Rework the §12 1 TB/day cost example with your measured ratio from 07.2. Write a 5-sentence recommendation (ES, Loki or ClickHouse) to a CTO whose top query is "find this customer's requests".
  - **Verify:** The note cites your ratio and the §14 decision-tree branch.

**Checkpoint (closed book):**
1. Why does a high-cardinality label hurt Loki far more than Elasticsearch?
2. What is structured metadata in Loki, and when should a field go there instead of a label?
3. Name two reasons log lines compress 10× or more.
<details><summary>Answers</summary>

1. Loki keeps one chunk stream per label set. Unbounded labels create millions of tiny streams, which blows up the index, ingester memory and chunk count. ES indexes terms inside documents, so high-cardinality values are its normal case, though at high storage cost.
2. Per-line key/values stored with the line but not part of the stream identity. Use it for high-cardinality fields you still want to filter on, like trace_id or user_id.
3. Repeated templates and keys within a stream, repeated timestamp prefixes, and chunked general-purpose compressors (gzip/snappy/zstd) working on sorted, similar lines.
</details>

---

## 08 — Traces Storage  ([chapter](08-traces-storage.md))
**Time:** ~3 h · **Needs:** core stack

- [ ] **08.1 Lookup by id vs search by attribute** *(Level: Core)*
  - **Goal:** Feel the §2 trade-off in Tempo's design (§3.3, §3.4).
  - **Do:** Time `curl localhost:3200/api/traces/<id>` and a search `q={ span.http.route = "/checkout" && duration > 1s }` over 1 h and over 24 h.
  - **Predict:** Which query grows with the time range, and why.
  - **Verify:** Record latencies. The id lookup stays flat. Search grows with the number of blocks scanned.
- [ ] **08.2 Span metrics are computed on sampled data** *(Level: Core)*
  - **Goal:** Show the §8.4 trap.
  - **Do:** With the metrics-generator on, compare `sum(rate(traces_spanmetrics_calls_total{service="shop"}[5m]))` with `sum(rate(REQ_count{job="shop"}[5m]))`. Do it at 100 % sampling, then with SDK head sampling at 10 %.
  - **Predict:** The ratio in each case.
  - **Verify:** 1.0 at 100 % sampling and about 0.1 at 10 %. Write down where span metrics must be generated (before sampling) to be correct.
- [ ] **08.3 Span explosion from an auto-instrumented loop** *(Level: Core)*
  - **Goal:** Reproduce §14.6.
  - **Do:** Add an endpoint that does 500 Redis `GET`s in a loop. Measure spans per trace and `rate(tempo_distributor_bytes_received_total[5m])`. Then switch to a pipeline (`r.pipeline()`) or wrap the loop in one manual span.
  - **Predict:** Bytes per trace before and after.
  - **Verify:** Spans per trace go from about 500 to 2 or 3. Bytes drop in proportion.
- [ ] **08.4 Clock skew and negative spans** *(Level: Stretch)*
  - **Goal:** See §14.2.
  - **Do:** Install `libfaketime` in the image and run inventory with `LD_PRELOAD=/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1 FAKETIME="-0.5"`. The path is different on arm64.
  - **Predict:** How the waterfall renders the child span.
  - **Verify:** A screenshot where the child starts before its parent. Note how Tempo/Grafana shows it.
- [ ] **08.5 Size the tail-sampling buffer** *(Level: Stretch)*
  - **Goal:** Apply the §13.3 formula (and 04 §5.4).
  - **Do:** Compute memory = spans/s × decision_wait × bytes/span (bytes/span from `tempo_distributor_bytes_received_total / tempo_distributor_spans_received_total`). Measure collector RSS at 20 and 80 RPS with `decision_wait: 10s`.
  - **Verify:** Measured RSS delta is within 2× of the prediction. Explain the overhead factor.

**Checkpoint (closed book):**
1. Why can Tempo store traces so cheaply, and what does it give up for that?
2. How do you get correct RED metrics when traces are sampled?
3. Two common causes of broken or orphaned traces?
<details><summary>Answers</summary>

1. It keeps no attribute index. Traces sit in object storage as columnar blocks, found by trace id through bloom filters. Attribute search has to scan block columns, so it is slower and the cost grows with the time range.
2. Generate span metrics (or app metrics) before sampling, either in the SDK or in the collector's spanmetrics connector ahead of the sampler. Metrics computed from sampled spans must be rescaled, and they are biased under tail sampling.
3. A service that doesn't propagate context (missing instrumentation, async boundary, proxy stripping headers), and spans of one trace split across tail samplers or regions. Clock skew and dropped parents are two more.
</details>

---

## 09 — Profiling  ([chapter](09-profiling.md))
**Time:** ~3 h · **Needs:** core stack; Linux host for 09.5

- [ ] **09.1 Find the hot function** *(Level: Core)*
  - **Goal:** Get from a latency symptom to a line of code (§11.1, §11.2).
  - **Do:** Ask a partner, or a script choosing at random, to set `cpu_ms` on one service without telling you which. Starting from the latency panel only, find the service, then the function, in Pyroscope.
  - **Predict:** Write your guess for the function's self-time % before you open the flame graph.
  - **Verify:** `burn_cpu` is the widest self-time frame. Record how many minutes it took you.
- [ ] **09.2 Diff flame graph for a "fix"** *(Level: Core)*
  - **Goal:** Use §11.3 and §12.3.
  - **Do:** Replace a naive hot path (e.g. `json.dumps` in a loop, or a regex compiled per call) with the efficient version. Compare the two time ranges in Pyroscope's diff view.
  - **Verify:** The diff shows the removed frame in the "decreased" colour. p99 and CPU drop by an amount you record.
- [ ] **09.3 On-CPU vs off-CPU** *(Level: Core)*
  - **Goal:** See the §11.6 blind spot.
  - **Do:** Add a sync endpoint that calls `time.sleep(0.3)`. Under load, run `docker compose exec shop py-spy record -o /tmp/p.svg --pid 1 --duration 30`, then again with `--idle`. Use `ps` to find the uvicorn PID if it isn't 1.
  - **Predict:** Does the sleeping function show up in the default Pyroscope CPU profile?
  - **Verify:** It is absent from the CPU views and dominant with `--idle`.
- [ ] **09.4 Profiler overhead** *(Level: Core)*
  - **Goal:** Measure §13.1 and §1.3 rather than trusting them.
  - **Do:** At 50 RPS, compare CPU (`docker stats`) and p99 with profiling off, at the default rate (100 Hz), and at `sample_rate=1000` in `pyroscope.configure`.
  - **Predict:** The overhead % at each setting.
  - **Verify:** A 3-row table. Decide whether 100 Hz passes the §13.5 "do you save more than you spend" test.
- [ ] **09.5 Frame pointers and broken stacks** *(Level: Stretch)*
  - **Goal:** Reproduce §16.1 with a system-wide profiler.
  - **Do:** On Linux, build a small recursive C program twice: `-O2 -fomit-frame-pointer` and `-O2 -fno-omit-frame-pointer`. `perf record -F 99 -g ./a.out`, then `perf report --stdio`. Retry the first build with `--call-graph dwarf`.
  - **Verify:** The median stack depth for each run. The first build gives truncated stacks until DWARF unwinding is used. For GPU-side profiling, see [`../gpu-observability/tasks.md`](../gpu-observability/tasks.md) (chapter 11 tasks).

**Checkpoint (closed book):**
1. Why do profilers sample at 99 Hz and not 100 Hz?
2. What does a CPU flame graph not show that often explains latency?
3. Why does symbolization often fail in containers?
<details><summary>Answers</summary>

1. To avoid sampling in lockstep with periodic activity (timers, 100 Hz schedulers), which would bias the samples.
2. Off-CPU time: waiting on I/O, locks, sleeps, the GIL. You need off-CPU/wall-clock profiles or traces for that.
3. Debug symbols are stripped from the image, the profiler can't see the container's filesystem or build IDs, or JIT/interpreted frames need a runtime-specific unwinder.
</details>

---

## 10 — Query Layer  ([chapter](10-query-layer.md))
**Time:** ~4 h · **Needs:** core stack. Keep [appendix C](appendix-c-query-recipe-book.md) open.

- [ ] **10.1 `rate` vs `irate` vs `increase`, and counter resets** *(Level: Core)*
  - **Goal:** See when each one misleads (§2.3).
  - **Do:** Graph `rate(REQ_count{job="shop"}[5m])`, `irate(...[5m])` and `increase(...[5m])` during a bursty k6 run (ramping-arrival-rate). Then `docker compose restart shop` in the middle of the run.
  - **Predict:** Will `increase` return whole numbers? Will the restart cause a dip or a spike?
  - **Verify:** `increase` returns non-integers because of extrapolation. `rate` handles the reset with no negative dip. `irate` is noisy. Screenshot all three together.
- [ ] **10.2 `histogram_quantile` pitfalls** *(Level: Core)*
  - **Goal:** Make each §2.5 mistake on purpose.
  - **Do:** Set the latency View buckets to `[0.1, 0.5, 1, 5]` and inject exactly 600 ms on 5 % of requests. Run: (a) `histogram_quantile(0.99, rate(REQ_bucket[5m]))` without `sum by (le)`; (b) `sum by (job)` with `le` left out; (c) the correct `histogram_quantile(0.99, sum by (le) (rate(REQ_bucket{job="shop"}[5m])))`; (d) the same query with 0.5 instead of 0.99; almost all requests sit in the first bucket.
  - **Predict:** The p99 that (c) reports when the true value is 0.6 s, and the p50 that (d) reports when the true median is a few ms.
  - **Verify:** (a) returns one series per label set, (b) returns empty/NaN, and (c) returns about 0.9 s (0.5 + 0.8 × 0.5, from linear interpolation inside the 0.5–1 s bucket), and (d) returns about 0.05 s (interpolated across 0–0.1 s). Record both errors and write the rule: put a bucket boundary at the SLO threshold.
- [ ] **10.3 The subquery gotcha** *(Level: Core)*
  - **Goal:** Understand §2.7.
  - **Do:** Compare `max_over_time(rate(REQ_count[5m])[1h:])` with `max_over_time(rate(REQ_count[5m])[1h:1m])` and with a recording rule plus `max_over_time(rule[1h])`.
  - **Verify:** Record the values and `prometheus_engine_query_duration_seconds` for each. Explain which step the default subquery used.
- [ ] **10.4 LogQL: find the error spike** *(Level: Core)*
  - **Goal:** Turn logs into a time series (§3.3, §3.4).
  - **Do:** At a time only a partner knows, set `error_rate` 0.1 on inventory for 4 min. Find it using only Loki: `sum by (service_name) (count_over_time({service_name=~"shop|inventory"} | severity_text="ERROR" [1m]))`, then `topk(3, sum by (exception_type) (count_over_time({service_name="inventory"} | severity_text="ERROR" [5m])))`. Check field names in the log details pane first.
  - **Verify:** Your spike start and end are within 1 min of the real window. Rewrite the query with the line filter after a `| json` parser and compare bytes processed (§3.6).
- [ ] **10.5 TraceQL structural query** *(Level: Core)*
  - **Goal:** Use §4.3 to find a pattern you cannot get from metrics.
  - **Do:** `{ resource.service.name = "shop" && span.http.route = "/checkout" } >> { resource.service.name = "inventory" && status = error }`. Then `{ resource.service.name = "shop" } | count() > 5`.
  - **Verify:** The first query's count matches injected inventory errors that reached checkout, within the sampling ratio.
- [ ] **10.6 A query of death and its limit** *(Level: Stretch)*
  - **Goal:** See §6.1.
  - **Do:** Run `count by (__name__) ({__name__=~".+"})` over 7 d with a 15 s step. Restart Prometheus with `--query.max-samples=100000` and run it again.
  - **Verify:** Record the query time and Prometheus RSS peak before the limit, and the error message after.

**Checkpoint (closed book):**
1. Why does `increase()` return 4.2 for a counter that only moves in whole numbers?
2. What two things must the inner aggregation of `histogram_quantile` keep, and what happens if you drop `le`?
3. In LogQL, what is the cheapest thing to put first, and why?
<details><summary>Answers</summary>

1. It extrapolates the observed slope to the edges of the range window, because samples rarely fall exactly on the window boundaries.
2. `le` plus the grouping labels you want in the output. Without `le` the bucket structure is gone and the function returns nothing.
3. The stream selector with narrow label matchers, then line filters (`|=`). The index prunes streams, and line filters are much faster than parsing (`| json`) every line.
</details>

---
## 11 — Dashboards  ([chapter](11-dashboards.md))
**Time:** ~3 h · **Needs:** core stack

- [ ] **11.1 A RED dashboard as code** *(Level: Core)*
  - **Goal:** Build §3.2 and §3.5 as a provisioned JSON file, not by clicking.
  - **Do:** Write `grafana/dashboards/red.json` with a `service` variable (`label_values(REQ_count, job)`) and three panels: rate, error ratio, and p50/p90/p99. Commit it. Restart Grafana with its volume deleted.
  - **Verify:** The dashboard comes back without any clicks. Switching `service` between shop and inventory updates all three panels. Lint it with `dashboard-linter lint grafana/dashboards/red.json` (from `go install github.com/grafana/dashboard-linter@latest`) and fix every finding.
- [ ] **11.2 The five-second test, timed** *(Level: Core)*
  - **Goal:** Measure §6.1 on a real person.
  - **Do:** A partner injects one of {inventory errors, shop latency, worker stopped} while you look away. Open your dashboard and say which component is broken, while they time you. Rearrange the layout by the §6.2 pyramid, then repeat with a different fault.
  - **Predict:** Your time before and after the rearrangement.
  - **Verify:** Two times recorded. The target is under 5 s for the top row.
- [ ] **11.3 From symptom to log line in three clicks** *(Level: Core)*
  - **Goal:** Test the §7 correlation chain.
  - **Do:** Starting from a latency spike: click an exemplar, go to the Tempo trace, press "Logs for this span", and land on the Loki line.
  - **Verify:** Count the clicks (≤ 3) and the seconds. Fix any broken link in `ds.yaml` (`tracesToLogsV2`, `exemplarTraceIdDestinations`).
- [ ] **11.4 Deploy annotations answer "what changed?"** *(Level: Core)*
  - **Goal:** Apply §9.3.
  - **Do:** Write `deploy.sh`. It changes a fault setting (your fake "release") and posts `curl -XPOST localhost:3000/api/annotations -H 'content-type: application/json' -d '{"text":"shop v2","tags":["deploy","shop"]}'`. Add an annotation query on the `deploy` tag to the dashboard.
  - **Verify:** Every change shows as a vertical line, and the latency step lines up with it.
- [ ] **11.5 USE for non-physical resources** *(Level: Stretch)*
  - **Goal:** Apply §4.4 to a Python service.
  - **Do:** Add gauges for event-loop lag (a coroutine that sleeps 100 ms and records the overshoot), the Redis connection pool in use, and the AnyIO thread-pool tokens in use. Build a USE row from them.
  - **Verify:** A blocking `time.sleep` in an async handler (see 42.3) shows up as event-loop lag before p99 moves.

**Checkpoint (closed book):**
1. What three panels make a RED row, and why should duration be a histogram?
2. What does the "five-second test" check?
3. Why should dashboards be provisioned from files and not built in the UI?
<details><summary>Answers</summary>

1. Request rate, error ratio, and latency percentiles. A histogram can be aggregated across instances, and its buckets match SLO thresholds.
2. Whether someone who has never seen the dashboard can tell within 5 seconds that something is wrong, and roughly where.
3. Review, history, reproducibility, and disaster recovery. A UI-only dashboard disappears with the Grafana database and drifts without review (see 36.2).
</details>

---

## 12 — Alerting  ([chapter](12-alerting.md))
**Time:** ~3.5 h · **Needs:** core stack

- [ ] **12.1 Unit-test an alert rule** *(Level: Core)*
  - **Goal:** Treat alerts as code (§10.2) and learn that `for:` is dwell time (§3.1).
  - **Do:** Write `ShopHighErrorRate` (error ratio > 5 %, `for: 5m`) in `prometheus/rules/shop.yml`. Write `tests.yml` with `input_series` of `0+6x20` errors and `0+54x20` successes, asserting no alert at 4m and an alert at 10m. Run `docker run --rm -v $PWD/prometheus:/p -w /p --entrypoint promtool prom/prometheus:v3.1.0 test rules tests.yml`.
  - **Predict:** The first evaluation time at which the alert fires.
  - **Verify:** The test passes. Change the threshold to 11 % and it fails with a readable diff.
- [ ] **12.2 Multi-window multi-burn-rate, hand-written** *(Level: Core)*
  - **Goal:** Build §5.4 and check the §5.5 math against a live system.
  - **Do:** Copy the §5.4 recording and alert rules, changed to use `REQ_count` for shop with a 99.9 % SLO. Inject `error_rate` 0.02 (burn rate 20) and time until `ShopFastBurn` fires. Then repeat with 0.005 (burn rate 5).
  - **Predict:** Time to fire at burn 20. The 1 h window must average ≥ 14.4, so about 0.72 × 60 ≈ 43 min, plus `for`. Which alert, if any, fires at burn 5?
  - **Verify:** Measured time-to-fire is within ±3 min of your prediction. At burn 5 nothing pages, since the slow burn threshold is 6. Write down how much budget burns unnoticed per day at that rate.
- [ ] **12.3 Routing, grouping, inhibition** *(Level: Core)*
  - **Goal:** Control notification volume (§7).
  - **Do:** Add a route for `severity=page` and an inhibit rule where `ShopDown` suppresses `ShopHighLatency` on the same `service`. Test routing offline: `docker run --rm -v $PWD:/c --entrypoint amtool prom/alertmanager:v0.28.0 config routes test --config.file=/c/alertmanager.yml severity=page service=shop`. Then fire 10 alert instances with different `instance` labels at the same moment, e.g. a loop of `amtool alert add ShopHighLatency service=shop instance=i$n --alertmanager.url=http://alertmanager:9093` run on the compose network.
  - **Predict:** How many webhook POSTs reach `alert-sink` with `group_by: [alertname, service]`, and how many with `[alertname, instance]`?
  - **Verify:** Count POSTs in `docker compose logs alert-sink`. The counts should be 1 and 10. With shop down, the latency alert is inhibited (grey in the Alertmanager UI).
- [ ] **12.4 Absence is not zero** *(Level: Stretch)*
  - **Goal:** Catch the "silent failure" anti-pattern (§13.7).
  - **Do:** Write `up{job="otel-apps"} == 0` and `absent(up{job="otel-apps"})`. Remove the scrape job entirely and reload (`curl -XPOST localhost:9090/-/reload`).
  - **Predict:** Which alert fires?
  - **Verify:** Only `absent` fires. Then add `absent_over_time(REQ_count{job="shop"}[10m])` for the case where shop stops exporting but the collector keeps running.

**Checkpoint (closed book):**
1. For a 99.9 % SLO over 30 days, what burn rate consumes 2 % of the budget in 1 hour, and why pair the long window with a short one?
2. What is the difference between inhibition and silencing?
3. Why should page alerts be on symptoms and not causes?
<details><summary>Answers</summary>

1. 14.4 (0.02 × 720 h / 1 h). The short window (5 m) makes the alert reset quickly once the burn stops, instead of firing for the rest of the hour.
2. Inhibition is an automatic rule: while alert A fires, suppress matching alert B. A silence is a manual, time-limited mute set by a person, for example during maintenance.
3. Symptoms (errors and latency users see) always matter and are few. Causes (CPU high, pod restarted) are many, often harmless, and miss unknown failure modes. Cause signals belong on dashboards or tickets.
</details>

---

## 13 — SLO Engineering  ([chapter](13-slo-engineering.md))
**Time:** ~3.5 h · **Needs:** core stack, Sloth (`ghcr.io/slok/sloth`)

- [ ] **13.1 Generate the SLO with Sloth and diff it** *(Level: Core)*
  - **Goal:** Use an SLO compiler (§7) and compare it with 12.2.
  - **Do:** Write `slo.yml` (`version: "prometheus/v1"`, service `shop`, objective 99.5, `sli.events.error_query` and `total_query` on `REQ_count` with `[{{.window}}]`, plus `alerting.page_alert` and `ticket_alert`). Run `docker run --rm -v $PWD:/d ghcr.io/slok/sloth generate -i /d/slo.yml -o /d/prometheus/rules/slo-shop.yml`.
  - **Predict:** How many recording rules and alerts it generates.
  - **Verify:** Load the rules, then query `slo:sli_error:ratio_rate5m{sloth_service="shop"}` and `slo:period_error_budget_remaining:ratio`. List every difference from your hand-written 12.2 rules (windows, factors, `for`).
- [ ] **13.2 A latency SLI that needs a bucket boundary** *(Level: Core)*
  - **Goal:** Learn why §2.3 says "the threshold hides the work".
  - **Do:** Define good events as `REQ_bucket{le="0.3"}`. Check whether `0.3` exists in your buckets. If it doesn't, add it with a View (02.4) and redeploy.
  - **Predict:** What the SLI query returns before the boundary exists.
  - **Verify:** An empty result before and a sensible ratio after. Write down the rule: SLO thresholds are bucket boundaries.
- [ ] **13.3 Spend budget on purpose** *(Level: Core)*
  - **Goal:** Practise the §5.1 budget calculus.
  - **Do:** Use a 1-day SLO window for the lab. With your measured RPS, compute how many minutes of 20 % errors use up 25 % of a 99.5 % daily budget. Inject exactly that.
  - **Predict:** The minute count. (Budget = 0.005 × daily requests; each minute burns 0.2 × RPS × 60.)
  - **Verify:** Your remaining-budget query, `1 - (sum(increase(errors[1d])) / (0.005 * sum(increase(total[1d]))))` written with the real metric names, falls by 25 % ± 3 pp. Sloth's `slo:period_error_budget_remaining:ratio` uses its own 30-day period, so it moves much less.
- [ ] **13.4 Dependency math sets the inventory SLO** *(Level: Core)*
  - **Goal:** Apply §11.1 and §11.4.
  - **Do:** Measure 1 h availability for shop's own logic and for inventory separately. Compute the inventory SLO needed for shop to meet 99.5 %, given shop's own error share.
  - **Verify:** A short table: required inventory SLO, current measured value, and pass/fail.
- [ ] **13.5 Error-budget policy** *(Level: Stretch)*
  - **Goal:** Explain it (§12).
  - **Do:** Write a one-page policy for the checkout team, addressed to its product manager, with the four budget bands from §12 and who decides at each.
  - **Verify:** Each band names an action with a trigger number.

**Checkpoint (closed book):**
1. Write the formula for error budget and for burn rate.
2. Why do many teams use 28-day windows instead of 30?
3. What is the difference between a journey SLO and a service SLO, and which should page?
<details><summary>Answers</summary>

1. Budget = (1 − SLO) × total events in the window. Burn rate = observed error ratio / (1 − SLO). A burn rate of 1 uses exactly the whole budget by the end of the window.
2. Four whole weeks, so every window has the same number of each weekday. This removes weekly seasonality from comparisons.
3. A journey SLO measures what the user experiences end to end (checkout succeeds). A service SLO measures one component. Journey SLOs should page; service SLOs guide ownership and budgets.
</details>

---

## 14 — On-Call  ([chapter](14-on-call.md))
**Time:** ~2.5 h · **Needs:** core stack, alerts from 12–13

- [ ] **14.1 Runbook-as-code gate** *(Level: Core)*
  - **Goal:** Enforce §6.3 "no runbook = no alert" in CI.
  - **Do:** For each `severity: page` rule, write `runbooks/<alertname>.md` to the §6.2 standard and add `annotations.runbook_url`. Write `check_runbooks.py`: parse `prometheus/rules/*.yml` and exit 1 if a page rule has no `runbook_url` or its file is missing.
  - **Verify:** The script passes. Delete one runbook and it fails, naming the alert.
- [ ] **14.2 Page response drill** *(Level: Core)*
  - **Goal:** Run the §8 loop against a clock.
  - **Do:** A partner draws one of five faults from a hat and injects it without warning. You respond using only dashboards and runbooks. Log ack time, the diagnosis, time to mitigate, and the runbook step where you got stuck.
  - **Verify:** A 5-row table after five drills. Fix every runbook that failed and redo that fault.
- [ ] **14.3 Rotation math** *(Level: Core)*
  - **Goal:** Apply §2.2 and §9.1.
  - **Do:** From `docker compose logs alert-sink` over your lab week, compute pages per shift. Design a primary/secondary weekly rotation for 6 engineers. Compute each engineer's on-call weeks per quarter, and the §9.1 health metrics (pages/shift, out-of-hours pages, % actionable).
  - **Verify:** A table that meets the chapter's floor of ≤ 2 pages per shift. If it doesn't, list the alerts to delete (12.3).
- [ ] **14.4 Handoff note** *(Level: Stretch)*
  - **Goal:** Explain it (§5.1).
  - **Do:** Write the §5.1 handoff document for your drill week to the incoming on-call.
  - **Verify:** A reader can name open issues and "things to watch" without asking you.

**Checkpoint (closed book):**
1. Why have a secondary on-call?
2. Name the four on-call health metrics.
3. What makes a runbook usable at 3 AM?
<details><summary>Answers</summary>

1. Escalation when the primary misses a page, a second pair of hands on large incidents, and someone who can step up to IC. With a single tier, one person carries the whole load.
2. Pages per shift, out-of-hours pages, % of pages that needed action, and time-to-ack/resolve. Some teams also track weeks since a clean shift.
3. It opens directly from the alert, starts with impact and first checks, gives copy-paste queries and commands, states rollback and escalation paths, and shows when it was last verified.
</details>

---

## 15 — Incident Response and Postmortem  ([chapter](15-incident-response-and-postmortem.md))
**Time:** ~4 h · **Needs:** core stack, 2–3 people (or timebox the roles yourself)

- [ ] **15.1 A timed drill with roles** *(Level: Core)*
  - **Goal:** Practise §4 roles and the §5 first 60 minutes.
  - **Do:** Assign IC, Ops and Scribe. Inject a compound fault: inventory `latency_ms` 800 at 30 %, plus the worker stopped. The scribe records UTC timestamps for every observation and decision in a shared doc.
  - **Verify:** A timeline with time to detect, time to declare, time to mitigate and time to resolve. The IC never ran a command.
- [ ] **15.2 Mitigate before you understand** *(Level: Core)*
  - **Goal:** Test §6.3, "have we tried rollback yet?"
  - **Do:** Run `deploy.sh` (11.4) to ship a bad "v2" (`error_rate` 0.3). Measure time to roll back (restore faults) and, separately, time to find the cause.
  - **Predict:** How much user impact (error-budget %, from 13.3) is avoided by rolling back first.
  - **Verify:** Both times recorded. Budget spent with an immediate rollback compared with root-cause-first.
- [ ] **15.3 A blameless postmortem from the template** *(Level: Core)*
  - **Goal:** Write the §12 artifact from real data.
  - **Do:** Use the §12 template for 15.1: summary, severity, impact in numbers, timeline, what went well, what went wrong, where we got lucky, contributing factors (§14.2, no "root cause"), and action items.
  - **Verify:** A peer reviews it against the §17 anti-patterns and finds none. Every action item meets §13.1: owner, date, and a type from §13.3.
- [ ] **15.4 Status-page updates** *(Level: Stretch)*
  - **Goal:** Explain it (§7.1, §7.4).
  - **Do:** Write the customer updates for T+5, T+20 and resolution, with no time estimates.
  - **Verify:** Each update is under 60 words and says what users see and when the next update will come.

**Checkpoint (closed book):**
1. Why must the IC not also do hands-on debugging?
2. Why does the chapter reject "five whys", and what replaces it?
3. What goes in "what we got lucky on", and why is it the most valuable section?
<details><summary>Answers</summary>

1. Coordination, communication and decisions need full attention. An IC who is debugging loses the overall picture and nobody runs the incident.
2. It forces a single linear causal chain and stops at whoever is closest to the event. The replacement is a set of contributing factors across technology, process and organisation.
3. The things that limited the damage by chance. They show latent risks that went unpunished this time, so they are the cheapest place to prevent the next incident.
</details>

---

## 16 — Capacity Planning  ([chapter](16-capacity-planning.md))
**Time:** ~4 h · **Needs:** core stack, k6, Python + numpy

- [ ] **16.1 Little's Law predicts the ceiling** *(Level: Core)*
  - **Goal:** Use §6.1 to predict a hard limit before a load test finds it.
  - **Do:** Add a sync `def /slow` that does `time.sleep(0.1)`. FastAPI runs sync handlers in AnyIO's thread pool, which has 40 tokens by default. Run ramping-arrival-rate from 100 to 600 RPS over 10 min against one uvicorn worker.
  - **Predict:** Maximum throughput = concurrency / W = 40 / 0.1 s = 400 RPS. p99 should break sharply past that.
  - **Verify:** The knee in p99 and the throughput plateau fall within 10 % of 400. Change the limiter to 80 tokens, predict again, and re-measure.
- [ ] **16.2 Headroom from a load test** *(Level: Core)*
  - **Goal:** Apply §3 and the §3.3 30/50/70 rule.
  - **Do:** Define capacity as the highest RPS where p99 < 300 ms and errors < 0.5 %. Current load = 20 RPS. Compute headroom for CPU, the thread pool and Redis connections separately (§5.2, bottlenecks move).
  - **Verify:** A per-resource headroom table with a 30/50/70 band for each resource.
- [ ] **16.3 Closed vs open loop hides the tail** *(Level: Core)*
  - **Goal:** See §7.2 and coordinated omission.
  - **Do:** Run the same 5-min test twice: `constant-vus` with 40 VUs, then `constant-arrival-rate` at the throughput the VU run reached. Inject `latency_rate` 0.05 / 1000 ms.
  - **Predict:** Which run reports the higher p99?
  - **Verify:** The open-loop run's p99 is higher, because the closed loop stops sending while it waits. Record both numbers.
- [ ] **16.4 Forecast and the capacity-plan artifact** *(Level: Core)*
  - **Goal:** Produce the §9.1 document from a model.
  - **Do:** Generate 12 months of daily peak RPS with numpy: 20 × 1.06^month, weekly seasonality, noise. Fit linear and compound models (§4.1, §4.4). Find the month when headroom (from 16.2) crosses 30 %. Fill in the §9.1 template.
  - **Verify:** The plan names a date, the resource that runs out first, the provisioning lead time (§8.5), and the forecast envelope (§4.5).
- [ ] **16.5 M/M/c check** *(Level: Stretch)*
  - **Goal:** Test §6.3.
  - **Do:** Run 1, 2 and 4 uvicorn workers with a CPU-bound 20 ms handler. Compare measured p50 wait with the Erlang C prediction (15 lines of Python).
  - **Verify:** Measured and predicted curves plotted together. For GPU capacity, see [`../gpu-observability/tasks.md`](../gpu-observability/tasks.md) (chapter 12 tasks).

**Checkpoint (closed book):**
1. State Little's Law and give one use in capacity planning.
2. Why plan on headroom rather than utilization?
3. Why does a closed-loop load test understate tail latency?
<details><summary>Answers</summary>

1. L = λW. Given measured latency and a concurrency limit (threads, pool size), max throughput = L_max / W. The same relationship also sizes pools and queues.
2. Headroom is measured against the capacity where the SLO still holds, per resource, so it already includes nonlinearity and the bottleneck. 60 % CPU can already be past the latency knee.
3. Virtual users wait for each response before sending the next request. When the system slows, the offered load drops, so the slow periods are sampled less (coordinated omission).
</details>

---

## 17 — Production Readiness Reviews  ([chapter](17-production-readiness-reviews.md))
**Time:** ~2.5 h · **Needs:** results from 03–16

- [ ] **17.1 Run the PRR on the demo service** *(Level: Core)*
  - **Goal:** Apply the §4 checklist and §5.1 scoring honestly.
  - **Do:** Score shop against every item in §4.1–§4.10. Mark each item blocking or non-blocking (§5.2) and attach evidence: a query, file or screenshot.
  - **Verify:** A scored checklist with a list of blockers. Every "yes" has evidence a reviewer can open.
- [ ] **17.2 PRR-as-code** *(Level: Core)*
  - **Goal:** Automate the §10.1 checks.
  - **Do:** Write `prr_check.py`. It uses the Prometheus API to check that RED metrics exist for the service, that a page alert with a runbook exists (reuse 14.1), and that an SLO rule exists. It uses the Grafana API (`/api/search?query=<service>`) to check for a dashboard. It checks a `/healthz` probe. It prints a score out of 10.
  - **Verify:** It scores `worker` lower than `shop`. Fix one gap and watch the score change.
- [ ] **17.3 Mid-life PRR triggers** *(Level: Stretch)*
  - **Goal:** Explain it (§6.1, §7).
  - **Do:** Write the trigger list for your team (traffic ×3, new dependency, and so on). Write a deprecation checklist for replacing the Redis queue with Kafka.
  - **Verify:** Each trigger is detectable by a query or a CI rule.

**Checkpoint (closed book):**
1. What is a PRR, and what is it not?
2. Why run PRRs after launch too?
3. What can be automated in a PRR, and what still needs a human?
<details><summary>Answers</summary>

1. A structured review of whether a service can be operated reliably: observability, SLOs, on-call, capacity, rollback, DR and security. It is not a gate for code quality or architecture taste, and it should not be adversarial.
2. Services drift. Traffic grows, dependencies change, owners leave. Mid-life triggers and ongoing scorecards catch the decay.
3. Presence checks (metrics, alerts, runbooks, SLOs, dashboards, probes) can be automated. Judgement cannot: are the SLOs meaningful, is the failure-mode analysis complete, is the rollback actually tested.
</details>

---

## 18 — Cardinality and Cost  ([chapter](18-cardinality-and-cost.md))
**Time:** ~3.5 h · **Needs:** core stack

- [ ] **18.1 Cause a cardinality explosion and measure it** *(Level: Core)*
  - **Goal:** Verify the §3.1 product rule and the RAM cost from 06.3.
  - **Do:** Add a counter `checkout.orders{user_id, route, status}` and a histogram `checkout.value{user_id}` with 12 bucket boundaries. Run k6 with N = 100, 1 000 and 10 000 distinct users, 15 min each.
  - **Predict:** Series = N × routes × statuses for the counter, plus N × (12 + 1 `+Inf` + `_sum` + `_count`) = 15N for the histogram. Predict RSS from 3 KiB/series.
  - **Verify:** `prometheus_tsdb_head_series`, `process_resident_memory_bytes` and `scrape_duration_seconds{job="otel-apps"}` at each step, within 20 % of your predictions.
- [ ] **18.2 Detect it like an on-call would** *(Level: Core)*
  - **Goal:** Use the §6.2 and §6.3 queries.
  - **Do:** Run `topk(10, count by (__name__) ({__name__=~".+"}))`, `count(count by (user_id) (checkout_orders_total))` and `curl -s localhost:9090/api/v1/status/tsdb | jq '.data.seriesCountByLabelValuePair[:5]'`.
  - **Verify:** All three name the same culprit. Save the queries as a "cardinality" dashboard row.
- [ ] **18.3 Defend in order of preference** *(Level: Core)*
  - **Goal:** Compare the §7 defenses by their effect.
  - **Do:** (a) Drop `user_id` in the collector (`transform` metric statements: `delete_key(attributes, "user_id")`). (b) Hash-and-bucket it into 16 buckets in the SDK (§7.4). (c) Set `sample_limit: 2000` on the scrape job.
  - **Predict:** What (c) does to the whole target when the limit is exceeded.
  - **Verify:** Series count after (a) and (b). With (c), `up{job="otel-apps"}` becomes 0: the entire scrape is rejected. Record that as the reason `sample_limit` is a fuse, not a filter.
- [ ] **18.4 CI cardinality check** *(Level: Core)*
  - **Goal:** Implement §5.4 so the explosion never reaches production.
  - **Do:** Write a pytest that runs 1 000 synthetic requests in-process with an `InMemoryMetricReader`, counts data points per metric, and fails over a budget file (`budgets.yaml`: metric → max series).
  - **Verify:** It fails on the 18.1 change and passes after 18.3(b).
- [ ] **18.5 "Is this label worth it?"** *(Level: Stretch)*
  - **Goal:** Explain it (§14).
  - **Do:** A product team asks for `customer_tier` and `customer_id` on checkout latency. Reply in 5 sentences with the cost in series and dollars, using your 18.1 slope and a $/series-month rate you state.
  - **Verify:** The reply offers the alternatives: exemplars, logs, and 35.3's lakehouse join. For GPU label design, see [`../gpu-observability/tasks.md`](../gpu-observability/tasks.md) (chapter 08 tasks).

**Checkpoint (closed book):**
1. A histogram with 12 bucket boundaries, 3 routes, 5 status codes and 20 pods. How many series?
2. Why is `sample_limit` dangerous as a cardinality defense?
3. Where should a `user_id` dimension live instead of a metric label?
<details><summary>Answers</summary>

1. (12 + 1 + 2) × 3 × 5 × 20 = 4 500: 12 finite buckets, `+Inf`, `_sum` and `_count`, for each of 300 label combinations.
2. Going over the limit fails the whole scrape. You lose every metric from that target, including the SLO metrics, not just the bad series.
3. As a span/log attribute or an exemplar, or in a wide-event/lakehouse store where high cardinality is cheap to query per event.
</details>

---

## 19 — Multi-Tenancy  ([chapter](19-multi-tenancy.md))
**Time:** ~3 h · **Needs:** core stack; Loki with `auth_enabled: true`

- [ ] **19.1 Tenants in Loki** *(Level: Core)*
  - **Goal:** Check §5.2 isolation.
  - **Do:** Mount a copy of Loki's `local-config.yaml` with `auth_enabled: true`. Push lines with `X-Scope-OrgID: team-a` and `team-b`. Query each tenant's lines with the other tenant's header.
  - **Predict:** What a query with no header returns.
  - **Verify:** Cross-tenant queries return 0 lines. A query with no header returns an error ("no org id").
- [ ] **19.2 Route tenants in the collector** *(Level: Core)*
  - **Goal:** Implement §6.3.
  - **Do:** Set `OTEL_RESOURCE_ATTRIBUTES=tenant=team-a` on shop and `team-b` on inventory. Use the `routing` connector keyed on `resource.attributes["tenant"]` to send to two `otlphttp` exporters with `headers: { X-Scope-OrgID: team-a }` and `{ X-Scope-OrgID: team-b }`.
  - **Verify:** Each tenant sees only its own service's logs. A service without a tenant attribute lands in a `default` pipeline, not in either tenant.
- [ ] **19.3 Quotas and the noisy neighbour** *(Level: Core)*
  - **Goal:** See §7.5 and §10 happen.
  - **Do:** Add Loki runtime overrides (`runtime_config.file`) with `team-b: { ingestion_rate_mb: 1, ingestion_burst_size_mb: 2 }`. Flood team-b at about 5 MB/s from a Python pusher while team-a runs normally.
  - **Predict:** team-b's accepted rate, and whether team-a's p99 push latency changes.
  - **Verify:** `sum by (tenant) (rate(loki_discarded_bytes_total{reason="rate_limited"}[1m]))` is non-zero only for team-b. team-a's ingest rate is flat.
- [ ] **19.4 Per-tenant series limits in Mimir** *(Level: Stretch)*
  - **Goal:** Apply §5.1.
  - **Do:** Run Mimir monolithic from the official "Play with Grafana Mimir" tutorial compose. Set `max_global_series_per_user: 5000` for `team-b` in runtime config. `remote_write` the 18.1 explosion with `headers: { X-Scope-OrgID: team-b }`.
  - **Verify:** `cortex_discarded_samples_total{reason="per_user_series_limit",user="team-b"}` increases and the other tenants are unaffected. For GPU tenancy, see [`../gpu-observability/tasks.md`](../gpu-observability/tasks.md) (chapter 13 tasks).

**Checkpoint (closed book):**
1. Logical vs physical isolation: one advantage of each.
2. Where should the tenant id be set, and who should be trusted to set it?
3. What happens to a tenant's writes when they exceed their ingestion rate limit?
<details><summary>Answers</summary>

1. Logical isolation (shared cluster, tenant header) is cheap and efficient. Physical isolation (separate clusters) gives the strongest blast-radius and compliance separation.
2. At a trusted write path, the gateway or collector, derived from authenticated identity. It should not be a header any client can set freely.
3. They are rejected (HTTP 429). Clients retry or drop depending on their queues, and the backend's discarded-samples metrics count the rejections by reason.
</details>

---

## 20 — AIOps and Frontier  ([chapter](20-aiops-and-frontier.md))
**Time:** ~3 h · **Needs:** core stack; an LLM is optional (local Ollama is free)

- [ ] **20.1 Statistical anomaly detection in PromQL** *(Level: Core)*
  - **Goal:** Build the §3.3 recipes and see where they fail (§3.4).
  - **Do:** Record `job:req:rate5m = sum by (job) (rate(REQ_count[5m]))`. Alert on `abs(job:req:rate5m - avg_over_time(job:req:rate5m[1h])) / stddev_over_time(job:req:rate5m[1h]) > 3`. Run 3 h of k6 with a slow ramp plus two injected events: a 40 % traffic drop and a 3× spike.
  - **Predict:** Which events fire the z-score alert, and whether the slow ramp causes false positives.
  - **Verify:** A table of events: detected (y/n) and detection delay. In Prometheus 3, `holt_winters` is renamed `double_exponential_smoothing` and needs `--enable-feature=promql-experimental-functions`.
- [ ] **20.2 Precision and recall** *(Level: Core)*
  - **Goal:** Evaluate the detector as §13.1 does.
  - **Do:** Inject 10 labelled incidents over a day (script the timestamps). Compute precision and recall of the z-score rule and of a static threshold.
  - **Verify:** A 2×2 confusion table per detector. Pick one and justify the choice in one sentence.
- [ ] **20.3 LLM postmortem draft, fact-checked** *(Level: Stretch)*
  - **Goal:** Measure the §8.3 validation problem.
  - **Do:** Give the 15.1 raw timeline and chat log to an LLM (local `qwen2.5` via Ollama works) and ask for a postmortem draft. Tag every factual claim in the draft as supported or unsupported by the input.
  - **Verify:** A count of unsupported claims. Write the review rule you would require before any LLM draft is published.

**Checkpoint (closed book):**
1. Why do z-score detectors produce false positives on seasonal traffic?
2. Which AIOps capability does the chapter recommend adopting first?
3. What metric tells you an anomaly detector is paying off?
<details><summary>Answers</summary>

1. The baseline window doesn't model daily or weekly patterns, so normal peaks and troughs look like 3σ deviations. Offset comparisons (`offset 1w`) or seasonal models are needed.
2. Alert grouping and noise reduction (and forecasting) before autonomous diagnosis or remediation. It is low risk and the value is easy to measure.
3. Precision and recall on labelled incidents, time-to-detect gained over existing alerts, and pages that led to action. Anomaly counts alone say nothing.
</details>

---

## 21 — Frontend, RUM and Mobile  ([chapter](21-frontend-rum-and-mobile.md))
**Time:** ~3.5 h · **Needs:** core stack, a browser, Node.js 20+

- [ ] **21.1 Capture Web Vitals** *(Level: Core)*
  - **Goal:** Measure §4 as users see it.
  - **Do:** Serve a static checkout page from shop (`/static`). Load `web-vitals` (`import {onLCP, onINP, onCLS} from 'https://unpkg.com/web-vitals@4?module'`) and send each metric with `navigator.sendBeacon('/rum', JSON.stringify(m))`. The `/rum` handler records a histogram per metric name. Load the page 30 times without throttling, then 30 times with DevTools "Slow 4G" and 4× CPU throttling.
  - **Predict:** The p75 LCP in each mode.
  - **Verify:** `histogram_quantile(0.75, sum by (le) (rate(rum_lcp_seconds_bucket[30m])))` for both modes, and whether each passes the 2.5 s "good" threshold.
- [ ] **21.2 Browser span to backend trace, and the CORS gotcha** *(Level: Core)*
  - **Goal:** Link RUM to backend traces (§12.1, §12.2).
  - **Do:** Bundle `@opentelemetry/sdk-trace-web` with the fetch instrumentation (`propagateTraceHeaderCorsUrls: [/localhost:8001/]`). Export to the collector at `http://localhost:4318/v1/traces`. Call inventory directly on port 8001, which is a different origin.
  - **Predict:** What the browser does with the preflight before you allow `traceparent`.
  - **Verify:** The console shows a CORS preflight failure. Add FastAPI `CORSMiddleware(allow_headers=["traceparent","tracestate"])` and collector `http: { cors: { allowed_origins: ["http://localhost:8000"] } }`. You then get one trace from the browser span to inventory.
- [ ] **21.3 Sticky session sampling** *(Level: Core)*
  - **Goal:** Implement §11.2.
  - **Do:** Decide sampling once per session (`sessionStorage`, 20 %) and apply it to both vitals and spans.
  - **Verify:** Across 50 sessions, each session is either fully present or fully absent, and the sampled share is about 20 %.
- [ ] **21.4 Beacon loss on unload** *(Level: Stretch)*
  - **Goal:** Measure the §6.3 unload problem.
  - **Do:** Fire a beacon on `visibilitychange` using (a) `fetch`, (b) `fetch(..., {keepalive: true})` and (c) `sendBeacon`. Close the tab immediately, 20 times per method.
  - **Verify:** A delivery-rate table per method from the server's received count.

**Checkpoint (closed book):**
1. Name the three Core Web Vitals and their "good" thresholds.
2. Why must the backend allow the `traceparent` header in CORS for RUM→trace linking?
3. Why sample per session and not per event?
<details><summary>Answers</summary>

1. LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1, all at the 75th percentile.
2. A custom header on a cross-origin request triggers a preflight. If `Access-Control-Allow-Headers` doesn't list `traceparent`, the browser blocks the request or the header, and the link is lost.
3. So a sampled session is complete (vitals, errors, spans and replay together) and per-session analysis is possible. Per-event sampling leaves every session with gaps.
</details>

---
## 22 — Service Mesh Observability  ([chapter](22-service-mesh-observability.md))
**Time:** ~4 h · **Needs:** kind, Helm, `istioctl` (or Linkerd). Cluster basics are in [`../k8s-learn/service-networking-tasks.md`](../k8s-learn/service-networking-tasks.md).

- [ ] **22.1 Mesh RED vs app RED** *(Level: Core)*
  - **Goal:** Find out who owns which number (§8.1, §8.2).
  - **Do:** `kind create cluster`, then `kind load docker-image obs-lab-shop obs-lab-inventory`. Run `istioctl install --set profile=demo -y` and `kubectl label ns default istio-injection=enabled`. Deploy shop and inventory (the same image, with its OTel env) and a k6 Job. Compare `sum(rate(istio_requests_total{destination_workload="inventory",response_code=~"5.."}[5m]))` with the app's own 5xx rate. Then give the inventory Deployment a failing readiness probe for 1 min.
  - **Predict:** Where the two error counts differ: sidecar-generated 503s, and retries.
  - **Verify:** A table of both counts for normal load, injected app errors and the unready backend. Explain each difference using the `response_flags` label.
- [ ] **22.2 The mesh can't propagate context for you** *(Level: Core)*
  - **Goal:** Prove §9.2.
  - **Do:** Configure an OpenTelemetry tracing provider in the mesh (meshConfig `extensionProviders`, then a `Telemetry` resource). Turn off the app's propagation (`OTEL_PROPAGATORS=none`) and look at the traces. Then turn it back on.
  - **Predict:** How many traces one checkout produces with app propagation off.
  - **Verify:** With propagation off, each hop's sidecar spans form a separate trace. With it on, there is one trace containing both sidecar and app spans.
- [ ] **22.3 Access-log volume and error-only filtering** *(Level: Core)*
  - **Goal:** Put numbers on §10.2 and §10.3.
  - **Do:** Turn on Envoy access logs with a `telemetry.istio.io/v1` `Telemetry` resource. Measure bytes per request from `kubectl logs` for 5 min, then add `filter: { expression: "response.code >= 400" }`.
  - **Predict:** Daily bytes at 10 000 RPS with and without the filter.
  - **Verify:** Both extrapolations and the measured reduction ratio.
- [ ] **22.4 Sidecar overhead** *(Level: Stretch)*
  - **Goal:** Measure §4.1.
  - **Do:** Run the same k6 load against inventory with and without injection. Record p50, p99 and per-pod memory.
  - **Verify:** A table of the added latency and memory. Optional: repeat with Cilium + Hubble (`hubble observe --namespace default`) for the §4.2 comparison.

**Checkpoint (closed book):**
1. What does a mesh give you for free, and what can it never give you?
2. Why do mesh and app error rates disagree?
3. Why must the app still forward trace headers in a mesh?
<details><summary>Answers</summary>

1. Free: per-edge RED metrics, access logs, and hop-level spans for every workload. Never: business-level signals, in-process work, async/queue boundaries, or the correlation of an inbound request to its outbound calls inside the app.
2. The sidecar sees failures the app never sees (connection refused, no healthy upstream, retries, timeouts it enforces). The app sees errors the mesh may record as successful.
3. The sidecar cannot tell which outbound call belongs to which inbound request. Only the app can copy the context across that boundary.
</details>

---

## 23 — Database Observability  ([chapter](23-database-observability.md))
**Time:** ~3.5 h · **Needs:** `--profile db` (Postgres 17)

- [ ] **23.1 Top-N queries from `pg_stat_statements`** *(Level: Core)*
  - **Goal:** Use §4, the highest-leverage source.
  - **Do:** `CREATE EXTENSION pg_stat_statements;`, then `pgbench -U postgres -i -s 20`, then `pgbench -U postgres -c 8 -T 300`. In parallel, loop a bad query: `SELECT * FROM pgbench_accounts WHERE abalance = 1234;`. Then run `SELECT queryid, calls, round(mean_exec_time::numeric,2) mean_ms, round(total_exec_time::numeric) total_ms, left(query,60) FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 5;`.
  - **Predict:** Which query tops `total_exec_time`, and which tops `mean_exec_time`.
  - **Verify:** The bad query tops mean time. The pgbench update may top total time. Explain why you need both orderings (§4.4).
- [ ] **23.2 Capture the plan, fix, and prove it** *(Level: Core)*
  - **Goal:** Apply §3.4 and §5.
  - **Do:** `ALTER SYSTEM SET auto_explain.log_min_duration = '100ms'; SELECT pg_reload_conf();`. Find the Seq Scan plan in `docker compose logs postgres`. `CREATE INDEX ON pgbench_accounts(abalance);` then `SELECT pg_stat_statements_reset();` and rerun.
  - **Verify:** The plan changes to an Index or Bitmap Scan, and the bad query's `mean_exec_time` falls by the factor you record.
- [ ] **23.3 Lock waits live** *(Level: Core)*
  - **Goal:** See §8.
  - **Do:** In session A: `BEGIN; UPDATE pgbench_accounts SET abalance = 0 WHERE aid = 1;` and leave it open. In session B, run the same update. In session C: `SELECT pid, wait_event_type, wait_event, pg_blocking_pids(pid), left(query,40) FROM pg_stat_activity WHERE wait_event_type = 'Lock';`.
  - **Verify:** B shows `Lock/transactionid`, blocked by A's pid. Add `postgres-exporter` (`quay.io/prometheuscommunity/postgres-exporter`) and graph `pg_locks_count` during the hold.
- [ ] **23.4 Pool saturation, predicted** *(Level: Core)*
  - **Goal:** Apply §6.2 and §6.3 using Little's Law (16.1).
  - **Do:** Add `/db` to inventory, backed by `psycopg_pool.AsyncConnectionPool(max_size=5)` and a query taking about 20 ms. Export `pool.get_stats()` (`requests_waiting`, `requests_wait_ms`) as metrics. Ramp from 50 to 400 RPS.
  - **Predict:** The pool ceiling = 5 / 0.02 s = 250 RPS. Above it, wait time grows and DB CPU stays flat.
  - **Verify:** In the traces, the gap between the handler span start and the DB span start grows past 250 RPS. Pool wait p99 and DB-side `mean_exec_time` are recorded side by side (§13.2, "DB is fine, app is slow").
- [ ] **23.5 The IN-list cardinality trap** *(Level: Stretch)*
  - **Goal:** See §4.3.
  - **Do:** Generate `SELECT ... WHERE aid IN (...)` with list lengths 1..500. Count `SELECT count(*) FROM pg_stat_statements;` before and after.
  - **Predict:** How many entries Postgres 17 creates.
  - **Verify:** About 500 new entries. Explain what that does to a per-queryid exporter (`pg_stat_statements.max` eviction, series churn).

**Checkpoint (closed book):**
1. Why sort `pg_stat_statements` by total time, not only mean time?
2. How do you tell "DB is slow" apart from "pool is exhausted"?
3. What does `pg_blocking_pids()` return?
<details><summary>Answers</summary>

1. Total time = calls × mean. It shows where the database spends its time overall. A fast query called a million times can matter more than a rare slow one.
2. Compare DB-side execution time with app-side time. If exec time is flat while app latency and pool-wait metrics grow (or the span gap before the DB call grows), the pool is the bottleneck.
3. The array of process IDs that hold the locks the given backend is waiting for.
</details>

---

## 24 — Network Observability  ([chapter](24-network-observability.md))
**Time:** ~3 h · **Needs:** core stack, `nicolaka/netshoot`; kind for 24.3; Linux for 24.4

- [ ] **24.1 Packet loss becomes tail latency** *(Level: Core)*
  - **Goal:** Link §3.1 retransmits to p99.
  - **Do:** `docker run --rm --net container:obs-lab-inventory-1 --cap-add NET_ADMIN nicolaka/netshoot tc qdisc add dev eth0 root netem loss 2%`. Measure shop→inventory client p99 (`http_client_request_duration_seconds`) and `nstat -az TcpRetransSegs` (in the same netshoot namespace) before and after. Remove it with `tc qdisc del dev eth0 root`.
  - **Predict:** Which percentile moves, and by roughly how much. The minimum RTO is about 200 ms.
  - **Verify:** p50 barely changes. p99 jumps by about 200 ms or more, and retransmits rise. Record the numbers.
- [ ] **24.2 CLOSE_WAIT leak** *(Level: Core)*
  - **Goal:** Reproduce §4.2.
  - **Do:** Write a 15-line Python TCP server that accepts connections and never calls `close()`. Hit it with a client that connects and closes 1 000 times.
  - **Predict:** Which side accumulates sockets, and in which state.
  - **Verify:** `ss -tan state close-wait | wc -l` on the server climbs to about 1 000 and stays there. Write the alert you would use: node_exporter's `--collector.tcpstat` provides `node_tcp_connection_states{state="close_wait"}`.
- [ ] **24.3 DNS as a failure surface** *(Level: Core)*
  - **Goal:** Build the §8.4 DNS SLO. The `ndots` tax itself is [`../k8s-learn/service-networking-tasks.md`](../k8s-learn/service-networking-tasks.md) Task 3.2; do that first.
  - **Do:** On kind, graph `sum(rate(coredns_dns_requests_total[1m]))`, `sum by (rcode) (rate(coredns_dns_responses_total[1m]))` and the p99 of `coredns_dns_request_duration_seconds_bucket`. Scale CoreDNS to 0 for 60 s under load.
  - **Predict:** What the app reports: which error, and after how long.
  - **Verify:** Record the app error type and the time to first error. Write a DNS SLI of success ratio plus p99, with a target.
- [ ] **24.4 Retransmits by process with eBPF** *(Level: Stretch)*
  - **Goal:** Use §7 without instrumenting anything.
  - **Do:** During 24.1, on a Linux host, run `sudo bpftrace -e 'kprobe:tcp_retransmit_skb { @[comm] = count(); }'`.
  - **Verify:** The retransmits are attributed to the right process name, and the count matches `nstat` within 10 %.

**Checkpoint (closed book):**
1. Why does 1–2 % packet loss hurt p99 far more than p50?
2. What does a growing CLOSE_WAIT count tell you, and whose bug is it?
3. What are the signs that DNS is the cause of an incident?
<details><summary>Answers</summary>

1. Most requests are unaffected. The few that lose a segment wait for a retransmit timeout (minimum ~200 ms) or fast retransmit, which adds a large delay only to the tail.
2. The peer closed the connection but the local application never called close(). It is a leak in the local app.
3. Resolution timeouts or SERVFAIL/NXDOMAIN spikes, errors like "temporary failure in name resolution", latency added in multiples of the resolver timeout, and failures across many services at once.
</details>

---

## 25 — Streaming and Kafka Observability  ([chapter](25-streaming-and-kafka-observability.md))
**Time:** ~4 h · **Needs:** `--profile kafka`, `confluent-kafka` in the app image

- [ ] **25.1 `traceparent` through Kafka headers** *(Level: Core)*
  - **Goal:** Build §8.2 and read the gap in the waterfall.
  - **Do:** shop produces `orders` with `propagate.inject(carrier)` and `headers=list(carrier.items())`. A consumer service extracts the context from `msg.headers()` and starts a `process orders` span of kind CONSUMER with the §8.3 messaging attributes (`messaging.system`, `messaging.destination.name`, `messaging.operation.type`). Build it once as a parent/child relationship and once with a span link.
  - **Predict:** The waterfall shape for each, and what the time between the publish span and the process span means.
  - **Verify:** With parent/child you see one trace with a gap equal to queue time. Pause the consumer for 20 s and the gap becomes about 20 s. With links you see two traces joined by the link. Write when each is right (§8.5, fan-out).
- [ ] **25.2 Offset lag vs time lag** *(Level: Core)*
  - **Goal:** Learn why §4.2 prefers time lag.
  - **Do:** Run `danielqsj/kafka-exporter --kafka.server=kafka:9092`. In the consumer, export `now - msg.timestamp()[1]/1000` as a gauge. Stop the consumer for 2 min at 50 msg/s, then again at 5 msg/s.
  - **Predict:** Offset lag and time lag in each run.
  - **Verify:** Offset lag differs by 10× between the runs (about 6 000 vs 600). Time lag is about 120 s in both. Write which one you would put an SLO on.
- [ ] **25.3 Partition skew** *(Level: Core)*
  - **Goal:** Reproduce §6.1 and §6.2.
  - **Do:** Create `orders` with 6 partitions. Key 80 % of messages by one customer id. Run 3 consumers.
  - **Verify:** `sum by (partition) (kafka_consumergroup_lag)` shows one hot partition while total throughput looks fine. Rekey (customer + order) and re-measure.
- [ ] **25.4 Poison message and DLQ** *(Level: Core)*
  - **Goal:** Contrast §4.4 with §10.1.
  - **Do:** Produce one non-JSON message. The first consumer version retries forever. The second sends the message to `orders.dlq` after 3 tries and increments `dlq_messages_total`.
  - **Predict:** The lag curve for each version.
  - **Verify:** v1 shows lag on one partition growing without bound. v2 shows lag recovering, one DLQ message, and an alert on `increase(dlq_messages_total[5m]) > 0`.
- [ ] **25.5 Under-replicated partitions** *(Level: Stretch)*
  - **Goal:** See §7.2 and §7.4.
  - **Do:** Run a 3-broker KRaft cluster and a topic with RF=3 and `min.insync.replicas=2`. Kill one broker, then two, while producing with `acks=all`.
  - **Verify:** `kafka-topics.sh --describe --under-replicated-partitions` output after one kill. After two, produce errors with `NOT_ENOUGH_REPLICAS`.

**Checkpoint (closed book):**
1. Why is time-based lag a better SLI than offset lag?
2. Parent/child or link for a consumer span, and when to use each?
3. What does a lag curve that "never recovers" usually mean?
<details><summary>Answers</summary>

1. It is in user-facing units (how stale the data is) and does not depend on message rate. A 10 000-message lag can mean 1 second or 1 hour.
2. Parent/child for one-message-one-process, where queue time is part of the request. Links for batches, fan-out and fan-in, where one span relates to many producers.
3. Consumers are slower than producers even at full speed (capacity), or a consumer is stuck (poison message, rebalance loop, one hot partition).
</details>

---

## 26 — LLM and AI Observability  ([chapter](26-llm-and-ai-observability.md))
**Time:** ~3.5 h · **Needs:** core stack, Ollama (`ollama pull qwen2.5:0.5b`)

- [ ] **26.1 TTFT vs total latency** *(Level: Core)*
  - **Goal:** Measure §5.2.
  - **Do:** Add an `/ask` endpoint to shop that streams from `POST http://host.docker.internal:11434/api/chat` (`"stream": true`). On Linux, add `extra_hosts: ["host.docker.internal:host-gateway"]` to shop. Record histograms of time-to-first-token, total time, and output tokens/s (from the final chunk's `eval_count` / `eval_duration`). Run 50 requests asking for about 50 tokens and 50 asking for about 500.
  - **Predict:** Which percentile, TTFT or total, depends on output length.
  - **Verify:** TTFT p50 is about the same for both groups. Total time scales roughly with output tokens.
- [ ] **26.2 GenAI spans with semantic conventions** *(Level: Core)*
  - **Goal:** Apply §9.2.
  - **Do:** Wrap the call in a span named `chat qwen2.5:0.5b` with `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`. Newer semconv uses `gen_ai.provider.name` in place of `gen_ai.system`; pin one.
  - **Verify:** `{ span.gen_ai.usage.output_tokens > 300 }` finds exactly the long answers.
- [ ] **26.3 Cost per request and the growing-context trap** *(Level: Core)*
  - **Goal:** Use §4.2 and §4.4.
  - **Do:** Record `llm_cost_usd_total` from token counts and a price table you define. Simulate 10-turn conversations that resend the full history each turn.
  - **Predict:** Input tokens on turn 10 compared with turn 1.
  - **Verify:** A per-turn plot of input tokens, growing roughly linearly. A cost/session number. One mitigation (summarise history, prompt caching §13.1) and its measured saving.
- [ ] **26.4 Retrieval eval as CI** *(Level: Stretch)*
  - **Goal:** Build §6.2 and §10.2.
  - **Do:** Build a 20-document corpus, 20 golden questions with their expected document id, and a tiny embedding retriever (any local embedding model, or BM25 with `rank_bm25`). Compute hit@3 in pytest and fail below 0.8. Swap in a worse chunking strategy.
  - **Verify:** The test fails on the worse chunking with a hit@3 number in the output. For inference-server metrics (vLLM, Triton), see [`../gpu-observability/tasks.md`](../gpu-observability/tasks.md) (chapter 14 tasks).

**Checkpoint (closed book):**
1. Why is TTFT a separate SLI from total latency?
2. Why do tokens per session grow in chat apps, and why does it matter for cost?
3. Name two quality signals you can collect without human labels.
<details><summary>Answers</summary>

1. With streaming, users perceive responsiveness at the first token. Total time mostly depends on output length, which is a product choice and not a fault.
2. Each turn resends the conversation history as input, so input tokens grow with turn number and cost grows faster than turn count.
3. Retrieval hit rate / recall on a golden set, LLM-as-judge faithfulness scores, "I don't know" rate, refusal rate, user thumbs and regenerations, and tool-call error rate.
</details>

---

## 27 — Security Observability  ([chapter](27-security-observability.md))
**Time:** ~3.5 h · **Needs:** core stack; kind for 27.3

- [ ] **27.1 A tamper-evident audit log** *(Level: Core)*
  - **Goal:** Build §3.2 to §3.4.
  - **Do:** Every call to `/admin/faults` emits a JSON audit event (who, what, when, where, outcome) to a separate logger routed to its own Loki stream. Each record carries `prev_hash` and `hash = sha256(prev_hash + canonical_json(record))`. Write `verify_chain.py`, which pulls the stream with `curl -G localhost:3100/loki/api/v1/query_range` (with `--data-urlencode` for the query) and checks the chain.
  - **Verify:** The verifier passes. Edit one record in an exported copy and it reports the exact index where the chain breaks.
- [ ] **27.2 Brute-force detection in LogQL** *(Level: Core)*
  - **Goal:** Apply the §4.2 signals.
  - **Do:** Add a fake `/login` that emits `auth.outcome` and `source.ip`. Run k6 with 30 wrong passwords per minute from one "IP" (an `X-Forwarded-For` header) plus normal traffic. Write `sum by (source_ip) (count_over_time({service_name="shop"} | auth_outcome="failure" [5m])) > 20` and make it a Grafana alert.
  - **Verify:** The alert fires within 5 min for the attacking IP only.
- [ ] **27.3 Runtime detection with Falco** *(Level: Core)*
  - **Goal:** See §7.1 to §7.4.
  - **Do:** On kind: `helm install falco falcosecurity/falco -n falco --create-namespace --set driver.kind=modern_ebpf --set tty=true`. Run `kubectl exec -it <shop-pod> -- sh`.
  - **Verify:** `kubectl logs -n falco -l app.kubernetes.io/name=falco` shows the "Terminal shell in container" rule firing. Record the delay from exec to log line.
- [ ] **27.4 Detection-as-code with Sigma** *(Level: Stretch)*
  - **Goal:** Build §12.3.
  - **Do:** Write the 27.2 rule as Sigma YAML. Run `pip install sigma-cli`, then `sigma plugin install loki`, then `sigma convert -t loki --without-pipeline rule.yml`.
  - **Verify:** The generated LogQL returns the same result as your hand-written query. Map it to a MITRE ATT&CK technique id (§10).

**Checkpoint (closed book):**
1. What are the "5 W's" an audit record must answer?
2. How does a hash chain make an audit log tamper-evident, and what does it not prevent?
3. Which security signals should page SRE on-call, and which should go to the SOC?
<details><summary>Answers</summary>

1. Who (actor), what (action and resource), when, where (source/system), and why or outcome (result, reason).
2. Each record commits to the hash of the previous one, so editing or deleting a record breaks every later link. It does not stop an attacker who can rewrite the whole chain, unless an anchor hash is stored elsewhere (WORM storage, a periodic signed checkpoint).
3. SRE gets availability-impacting signals (WAF blocking legitimate traffic, DDoS saturating capacity). The SOC gets threat signals (brute force, privilege escalation, anomalous access), through its own response path.
</details>

---

## 28 — Telemetry Pipeline Reliability  ([chapter](28-telemetry-pipeline-reliability.md))
**Time:** ~3.5 h · **Needs:** core stack

- [ ] **28.1 Canary and verifier** *(Level: Core)*
  - **Goal:** Build §8.1 and §8.2.
  - **Do:** Write `canary.py`, which every 30 s emits a gauge `canary_tick_timestamp_seconds`, a log line containing a unique id, and a span `canary.tick` carrying the same id. Write `verifier.py`, which queries Prometheus, Loki and Tempo for the newest id and exports `canary_signal_freshness_seconds{signal}` and `canary_queryable{signal,outcome}`. Stop Loki.
  - **Predict:** How long until the verifier reports the logs signal as unqueryable.
  - **Verify:** It goes red within 60 s. The other signals stay green.
- [ ] **28.2 Silent loss** *(Level: Core)*
  - **Goal:** Reproduce §16 and write the loss-ratio SLI (§4.1).
  - **Do:** Lower `memory_limiter` to `limit_mib: 60` and burst k6 to 300 RPS.
  - **Predict:** What the RED dashboard shows (it looks healthy, with less traffic).
  - **Verify:** `otelcol_receiver_refused_spans_total` rises. Write `1 - sum(rate(otelcol_exporter_sent_spans_total[5m])) / sum(rate(otelcol_receiver_accepted_spans_total[5m]) + rate(otelcol_receiver_refused_spans_total[5m]))` as a loss ratio with an alert at 1 %.
- [ ] **28.3 An independent observation path** *(Level: Core)*
  - **Goal:** Build the §3.3 "tiny Prometheus".
  - **Do:** Add `prometheus-meta` (its own config) that scrapes only the stack components and the verifier, with its own Alertmanager route. Add an always-firing `Watchdog` alert (`vector(1)`) on the main Prometheus, and alert on its absence from the meta side. Then `docker compose stop prometheus`.
  - **Verify:** The meta stack pages within 2 min, while the main stack is silent.
- [ ] **28.4 Queue-depth alert before loss** *(Level: Stretch)*
  - **Goal:** Apply §6.3.
  - **Do:** Alert on `otelcol_exporter_queue_size / otelcol_exporter_queue_capacity > 0.8`. Stop Tempo under load.
  - **Predict:** Time from stop until the queue is full: capacity (in batches) / batches per second.
  - **Verify:** The alert fires before `otelcol_exporter_enqueue_failed_spans_total` starts rising. Record the lead time.

**Checkpoint (closed book):**
1. What is the "dogfooding paradox"?
2. Name the four core SLIs for a telemetry pipeline.
3. Why is a canary a better signal than the pipeline's own metrics?
<details><summary>Answers</summary>

1. If the observability platform monitors itself through the same pipeline, then when it breaks, the alerts about it break too. You need an independent path.
2. Ingestion availability (accepted/total), freshness (write-to-queryable latency), completeness/loss ratio, and query availability/latency. Retention is sometimes added.
3. It tests the whole path end to end (write, store, query) from the user's side, so it catches failures that component metrics can't express, such as wrong routing or a broken query path.
</details>

---

## 29 — Synthetic Monitoring  ([chapter](29-synthetic-monitoring.md))
**Time:** ~3 h · **Needs:** core stack, blackbox exporter, k6

- [ ] **29.1 The `/healthz` trap** *(Level: Core)*
  - **Goal:** Show §4.4 with two probes.
  - **Do:** Add a blackbox module `checkout_ok: { prober: http, http: { fail_if_body_not_matches_regexp: ['"order_id"'] } }`. Add a scrape job with `metrics_path: /probe`, targets `http://shop:8000/healthz` and `http://shop:8000/checkout`, and the standard relabel (`__address__ → __param_target → instance`, `__address__ = blackbox:9115`). Stop inventory.
  - **Predict:** What `probe_success` shows for each target.
  - **Verify:** `/healthz` stays 1 while `/checkout` goes to 0. Write down which probe belongs in a synthetic SLO.
- [ ] **29.2 A journey check from k6** *(Level: Core)*
  - **Goal:** Build a §6 multi-step journey and measure flakiness (§6.4).
  - **Do:** Write a k6 script: browse, then add to cart, then check out, with `check()` on each step. Run it every minute for 1 h with `--out experimental-prometheus-rw`.
  - **Verify:** `k6_checks_rate` per step in Grafana. Count the failures that happened while the real error rate was 0; that is your flake rate.
- [ ] **29.3 "Any" vs "all" probe alerting** *(Level: Core)*
  - **Goal:** Compare the §8.3 policies.
  - **Do:** Run 3 blackbox instances as "regions". Route one through toxiproxy with a `timeout` toxic toggled on and off at random. Alert A fires if any region fails for 2 m. Alert B fires if ≥ 2 regions fail for 2 m.
  - **Predict:** Pages from each over 1 h.
  - **Verify:** A pages for the single broken probe path. B stays quiet until you break shop itself.
- [ ] **29.4 Synthetic deploy gate** *(Level: Stretch)*
  - **Goal:** Build the §12.2 post-deploy check.
  - **Do:** Add `thresholds: { checks: ['rate>0.99'], http_req_duration: ['p(95)<300'] }` to the journey script. Run it after `deploy.sh`. A failure means rollback.
  - **Verify:** A bad deploy makes k6 exit with code 99 and triggers the rollback. A good deploy exits 0.

**Checkpoint (closed book):**
1. What does synthetic monitoring catch that RUM cannot, and the other way round?
2. Why is `/healthz` a poor synthetic target?
3. Why alert on "≥ 2 of 3 regions failing" rather than "any region failing"?
<details><summary>Answers</summary>

1. Synthetic checks catch outages when there is no traffic (night, new features) and give a stable baseline. RUM catches real-user diversity: devices, networks, geography, and paths you didn't script.
2. It checks that the process is alive, not that the user journey works. Dependencies, data and business logic can all fail while it returns 200.
3. A single probe location has its own network and vantage-point failures. Requiring agreement removes those false pages but still catches real outages.
</details>

---

## 30 — Error Tracking  ([chapter](30-error-tracking.md))
**Time:** ~3 h · **Needs:** core stack, Python; GlitchTip (Sentry-compatible, self-hosted) is optional

- [ ] **30.1 Build a fingerprinter** *(Level: Core)*
  - **Goal:** Implement the §3.3 grouping algorithm.
  - **Do:** Generate 1 000 exceptions from 3 real bugs, with messages that include ids and amounts (`"order 8231 failed: amount 12.40"`). Group by (a) raw message, (b) exception type, and (c) a fingerprint: type plus normalised in-app frames (module:function, no line numbers), hashed.
  - **Predict:** The group count for each method.
  - **Verify:** About 1 000, 2–3 and exactly 3. Add a fourth bug that raises the same type from a different function and check that (c) separates it.
- [ ] **30.2 New-in-release detection** *(Level: Core)*
  - **Goal:** Apply §4.3 and §4.5.
  - **Do:** Tag events with `service.version`. "Deploy" v2, which adds a new bug and fixes an old one. Compute fingerprints seen in v2 but never in v1, and those seen in v1 but not in v2.
  - **Verify:** The new bug is flagged within one minute of v2 traffic and the fixed bug is listed as resolved. Compute a crash-free-requests rate per version.
- [ ] **30.3 Error to trace and back** *(Level: Core)*
  - **Goal:** Build the §8.1 and §8.4 links.
  - **Do:** In the exception handler, call `span.record_exception(e)` and `span.set_status(Status(StatusCode.ERROR))`, and log the fingerprint with the trace id.
  - **Verify:** TraceQL `{ status = error && event.exception.type = "ValueError" }` (Tempo 2.5+) finds the trace, and the Loki line for that trace carries the fingerprint.
- [ ] **30.4 Rate-limit an error storm** *(Level: Stretch)*
  - **Goal:** Implement §9.3 without losing rare errors (§7.4).
  - **Do:** Put a token bucket per fingerprint (10/min) in front of the error sink. Send 10 000 errors from one bug and 3 from a rare one.
  - **Verify:** About 10 events kept for the storm and all 3 rare ones kept, with a `dropped_total{fingerprint}` counter showing the rest.

**Checkpoint (closed book):**
1. Why can't error tracking group by message text?
2. What does "new in this release" need that plain logs lack?
3. Why does the chapter say you usually shouldn't sample errors?
<details><summary>Answers</summary>

1. Messages contain variable data (ids, values, timestamps), so one bug produces thousands of distinct strings. Grouping needs a normalised fingerprint such as type plus stack frames.
2. Stable fingerprints, a release tag on every event, and a first-seen/last-seen index per fingerprint across releases.
3. Errors are rare compared with requests, and low-volume errors are the most valuable. Rate-limit per fingerprint during storms instead of sampling uniformly.
</details>

---

## 31 — FinOps for Observability  ([chapter](31-finops-for-observability.md))
**Time:** ~2.5 h · **Needs:** core stack, a spreadsheet or Python

- [ ] **31.1 Unit economics of the lab** *(Level: Core)*
  - **Goal:** Build the §5.1 cost-per-request model from measurements.
  - **Do:** Over 1 h at a fixed RPS, measure per-signal CPU-seconds (`docker stats`) and bytes stored (Prometheus block sizes, `du` on Loki chunks and Tempo blocks). Price them with public on-demand compute and object-storage list prices, and state which ones you used.
  - **Verify:** A table of cost per 1 M requests per signal, and which signal dominates.
- [ ] **31.2 Showback by team** *(Level: Core)*
  - **Goal:** Build the §3 allocation.
  - **Do:** Add a `team` resource attribute to each service. Resource attributes land on `target_info` by default, so copy `team` onto the data points with a `transform` processor (`set(attributes["team"], resource.attributes["team"])` in `metric_statements`). Query `sum by (team) (count by (team, __name__) ({team!=""}))` for series, and bytes per `service_name` from Loki (`sum by (service_name) (bytes_over_time({service_name=~".+"}[1h]))`).
  - **Verify:** A per-team monthly showback table at your 31.1 rates.
- [ ] **31.3 Find what nobody uses** *(Level: Core)*
  - **Goal:** Run the §9 deletion routine.
  - **Do:** Run `mimirtool analyze grafana --address=http://grafana:3000`, then `mimirtool analyze rule-file prometheus/rules/*.yml`, then `mimirtool analyze prometheus --address=http://prometheus:9090` (all from the `grafana/mimirtool` image on the compose network).
  - **Predict:** The share of series never used by a dashboard or rule.
  - **Verify:** The `prometheus-metrics.json` output lists used and unused metrics. Draft the §9.2 deletion PR with the series saved.
- [ ] **31.4 The 10× stress test** *(Level: Stretch)*
  - **Goal:** Apply §6.3.
  - **Do:** Model 10× traffic and 2× services in Python. Which signal's cost grows superlinearly? Consider cardinality from new services, and trace volume at a fixed sampling rate.
  - **Verify:** A one-paragraph forecast naming the first budget breach and the lever (§8) that fixes it.

**Checkpoint (closed book):**
1. What are the four cost questions?
2. Showback vs chargeback: when does each fit?
3. Name three self-hosted cost levers in order of typical impact.
<details><summary>Answers</summary>

1. What are we spending? Who is spending it? Is it worth it (unit economics)? What will it be (forecast)?
2. Showback shows teams their costs with no billing. Use it first, to build awareness. Chargeback bills their budgets. Use it once allocation is trusted and teams have levers to act on it.
3. Cardinality and volume reduction (drop unused series and labels, sample traces, filter logs), retention and tiering, then compute right-sizing and commitments.
</details>

---
## 32 — Compliance and Privacy  ([chapter](32-compliance-and-privacy.md))
**Time:** ~3 h · **Needs:** core stack

- [ ] **32.1 PII audit of your own telemetry** *(Level: Core)*
  - **Goal:** Run the §3.2 and §3.3 audit on the logs you already have.
  - **Do:** Pull the last hour from Loki: `curl -sG localhost:3100/loki/api/v1/query_range --data-urlencode 'query={service_name=~".+"}' --data-urlencode limit=5000 --data-urlencode since=1h | jq -r '.data.result[].values[][1]'`. Pipe it to `pii_scan.py`, which uses regexes for email, IPv4 and card-like numbers (Luhn-checked). Do the same for span attributes with a Tempo search.
  - **Predict:** How many PII classes you will find in a lab you built yourself.
  - **Verify:** Findings per service and field. Every finding becomes a §3.4 "this should not exist" ticket.
- [ ] **32.2 Redaction processor with a unit test** *(Level: Core)*
  - **Goal:** Build §4.2 and §4.5.
  - **Do:** Add a contrib `redaction` processor to the traces pipeline with `allow_all_keys: true` and `blocked_values: ["4[0-9]{12}(?:[0-9]{3})?", "[\\w.+-]+@[\\w-]+\\.[\\w.]+"]`. Write a test that posts a span with a card number and an email to `:4318/v1/traces`, sends it through a `file` exporter, and asserts that neither value appears.
  - **Verify:** The test passes. Removing the processor makes it fail.
- [ ] **32.3 Right to erasure by pseudonymization** *(Level: Core)*
  - **Goal:** Implement the §6.3 architecture.
  - **Do:** Replace `user` with `HMAC(key_user, user_id)`, where each user has a random key stored in a small SQLite table. To "erase" a user, delete their key.
  - **Verify:** Before deletion you can map telemetry back to the user with the key. After deletion you cannot, and no telemetry had to be rewritten. Write the §6.4 residual risk (linkability through other fields) in one sentence.
- [ ] **32.4 Data classification table** *(Level: Stretch)*
  - **Goal:** Explain it (§3.1, §11.1).
  - **Do:** Classify every attribute and log field the demo emits (public / internal / confidential / restricted). Give each a retention and an access tier (§5.1, §5.2). Address it to an auditor.
  - **Verify:** Every field from 32.1 appears in the table with a justification.

**Checkpoint (closed book):**
1. Why redact at the source rather than in the backend?
2. How does pseudonymization make erasure possible without rewriting logs?
3. Why is "encryption at rest" not access control?
<details><summary>Answers</summary>

1. Once PII leaves the process it is copied into queues, backups, vendors and caches. Each copy has to be found and erased, and some copies are immutable.
2. Telemetry stores only a keyed hash. Deleting the per-user key breaks the link, so the remaining data is effectively anonymous.
3. Anyone with query access to the decrypted store still reads plaintext. Encryption protects media and transport. Who may read what is controlled separately, by RBAC and tenancy.
</details>

---

## 33 — Federated Multi-Region  ([chapter](33-federated-multi-region.md))
**Time:** ~3.5 h · **Needs:** core stack + Thanos (`quay.io/thanos/thanos:v0.37.2`)

- [ ] **33.1 Two regions, one query** *(Level: Core)*
  - **Goal:** Build §5.3 federated query.
  - **Do:** Run `prometheus-eu` and `prometheus-us` with `external_labels: { region: eu | us }`, each with a `thanos sidecar --prometheus.url=... --tsdb.path=/prometheus --grpc-address=0.0.0.0:10901` sharing its volume. Add `thanos query --endpoint=sidecar-eu:10901 --endpoint=sidecar-us:10901` and point Grafana at it. Then stop `sidecar-us`.
  - **Predict:** What `sum by (region) (up)` returns with one region down, with partial response allowed and with it denied.
  - **Verify:** You get partial data with a warning in one case and an error in the other. Write which you want for dashboards and which for SLO alerts (§10).
- [ ] **33.2 The traffic-weighting gotcha** *(Level: Core)*
  - **Goal:** Compute §8.3 by hand and in PromQL.
  - **Do:** Region eu carries 90 % of traffic at 99.9 % success. Region us carries 10 % at 95 %. Compute global availability as `avg(region ratios)` and as `sum(errors)/sum(total)`. Reproduce it with two shop instances and the fault knob.
  - **Predict:** Both numbers before querying: 97.45 % and 99.41 %.
  - **Verify:** The PromQL results match. Write which one the user experiences.
- [ ] **33.3 Recording rules at the spoke** *(Level: Core)*
  - **Goal:** Measure the §5.6 saving.
  - **Do:** Federate raw series from eu to a hub with `/federate?match[]={job="otel-apps"}`. Then federate only `job:req:rate5m` and `job:req_errors:rate5m`, computed at the spoke.
  - **Verify:** `scrape_samples_scraped{job="federate-eu"}` before and after, and the reduction factor.
- [ ] **33.4 WAN latency on global queries** *(Level: Stretch)*
  - **Goal:** See §9.1.
  - **Do:** Put toxiproxy with 150 ms latency between Thanos Query and `sidecar-us`. Time a 24 h range query with the region selector on "all" and on "eu".
  - **Verify:** Record both times, and the query-frontend or caching fix you would apply (§9.4).

**Checkpoint (closed book):**
1. Name the three multi-region architectures.
2. Why do averaged regional availabilities mislead?
3. What should keep working in a region when the global hub is down?
<details><summary>Answers</summary>

1. Independent regions with federated query; per-region ingest into a central global store (hub and spoke); and a single distributed store spanning regions.
2. Averaging gives each region equal weight regardless of traffic. The user-weighted ratio is sum(errors)/sum(total).
3. Local ingest, local dashboards, and local alerting and paging. Each region should detect and page on its own problems without the hub.
</details>

---

## 34 — Schema and Semantic-Conventions Governance  ([chapter](34-schema-and-semantic-conventions-governance.md))
**Time:** ~2.5 h · **Needs:** core stack, Python; OTel Weaver (`otel/weaver`) optional

- [ ] **34.1 An attribute registry** *(Level: Core)*
  - **Goal:** Build §3.2.
  - **Do:** Write `registry/attributes.yaml` for every custom attribute in the demo (`order.id`, `tenant`, `job.outcome` …), with type, stability, cardinality class (§5.1) and PII class (§6.1). Optionally, lay it out in Weaver's registry format and run `weaver registry check -r registry/`.
  - **Verify:** Every custom attribute emitted by the demo (find them with a TraceQL or Loki search) is in the registry, or shows up as a violation.
- [ ] **34.2 Contract test** *(Level: Core)*
  - **Goal:** Build the §9.2 producer test.
  - **Do:** Write a pytest that runs `/checkout` with an `InMemorySpanExporter`. It asserts that every span attribute key is either an OTel semconv name or in the registry, and that it matches the §4.1 naming regex (`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`). Then rename `order.id` to `orderId`.
  - **Verify:** The test fails and names the offending key and span.
- [ ] **34.3 Breaking-change blast radius** *(Level: Stretch)*
  - **Goal:** Put a number on the §8.1 "what counts as breaking" rule.
  - **Do:** Plan a rename of `checkout_orders_total` to `shop_orders_total`. Find every consumer: `grep` the dashboards JSON and the rules, and use `mimirtool analyze` output (31.3). Dual-emit both names for a deprecation window (§7.3).
  - **Verify:** A list of affected dashboards and alerts with a count. A migration PR that makes the old name unused before it is dropped.

**Checkpoint (closed book):**
1. What is the difference between a semconv attribute and a registry attribute?
2. Give two examples of breaking changes and one non-breaking change.
3. Where should naming and cardinality rules be enforced?
<details><summary>Answers</summary>

1. Semconv attributes are standardised by OTel across vendors and languages. A registry attribute is your organisation's own custom attribute, with owner, type and classifications.
2. Breaking: renaming a metric or attribute, changing a unit or type, removing a label. Non-breaking: adding a new optional attribute with bounded cardinality.
3. In CI, with contract tests and lint against the registry, and again at the collector as a safety net. Code review alone doesn't enforce them.
</details>

---

## 35 — Telemetry Lakehouse  ([chapter](35-telemetry-lakehouse.md))
**Time:** ~3 h · **Needs:** core stack, Python + `duckdb`

- [ ] **35.1 Tee spans to Parquet** *(Level: Core)*
  - **Goal:** Build the §4 tee pattern.
  - **Do:** Add a `file` exporter to the traces pipeline (`path: /data/spans.jsonl`, on a writable host mount). With DuckDB, `read_json_auto` it, unnest `resourceSpans → scopeSpans → spans`, and `COPY ... TO 'lake/spans' (FORMAT parquet, PARTITION_BY (dt, service))` (§5.3).
  - **Verify:** p99 duration by route from DuckDB matches the Tempo/Prometheus answer for the same hour within a few percent.
- [ ] **35.2 The small-files problem** *(Level: Core)*
  - **Goal:** Measure §5.4.
  - **Do:** Write the same 1 M spans as 10 000 small Parquet files and as 10 compacted ones. Time `SELECT service, count(*), quantile_cont(duration_ms, 0.99) FROM read_parquet('…/**/*.parquet') GROUP BY 1`.
  - **Predict:** The slowdown factor.
  - **Verify:** Record both times, and write the compaction interval you would choose.
- [ ] **35.3 The join metrics can't do** *(Level: Core)*
  - **Goal:** Use §9.
  - **Do:** Create `customers(user_id, tier)` in DuckDB with 1 000 users in 3 tiers. Join it to spans on the hashed `user` attribute. Compute p99 checkout latency per tier.
  - **Verify:** A per-tier table. Explain why the same answer in Prometheus would need a `user_id` label (18.1).
- [ ] **35.4 Hot vs lake cost for one year** *(Level: Stretch)*
  - **Goal:** Apply §10.2 and §10.3.
  - **Do:** Using your measured bytes/day in Tempo and in Parquet, price 1-year retention for hot storage and for object storage plus a query engine, at the list prices from 31.1.
  - **Verify:** A break-even retention in days. For GPU telemetry in a lakehouse, see [`../gpu-observability/tasks.md`](../gpu-observability/tasks.md) (chapter 17 tasks).

**Checkpoint (closed book):**
1. Why keep a hot store and a lakehouse, rather than one of them?
2. What causes the small-files problem, and how is it fixed?
3. Name two questions only the lakehouse can answer cheaply.
<details><summary>Answers</summary>

1. The hot store gives low-latency, recent, operational queries. The lake gives cheap long retention, SQL joins with business data and ad-hoc analytics. Their costs and query shapes differ.
2. Streaming writers flush often, which creates many tiny files. Each file costs an open and a metadata read, and compresses poorly. Periodic compaction into large files fixes it (Iceberg/Delta do this).
3. Latency per customer tier or contract (a join with warehouse data), year-over-year capacity trends, forensic lookups across months, and SLO compliance reports over long windows.
</details>

---

## 36 — DR for the Observability Stack  ([chapter](36-dr-for-observability-stack.md))
**Time:** ~3 h · **Needs:** core stack

- [ ] **36.1 Restore Prometheus from a snapshot** *(Level: Core)*
  - **Goal:** Run the §5.4 runbook and measure RPO and RTO.
  - **Do:** Record a fingerprint query: `sum(increase(REQ_count{job="shop"}[1h]))` evaluated at a fixed past timestamp. Run `curl -XPOST localhost:9090/api/v1/admin/tsdb/snapshot` and `docker compose cp prometheus:/prometheus/snapshots/<name> ./backup/`. Keep load running 10 more min. Then `docker compose rm -sf prometheus && docker volume rm obs-lab_prom-data`. Restore with `docker run --rm -v obs-lab_prom-data:/prometheus -v $PWD/backup/<name>:/snap alpine sh -c 'cp -a /snap/. /prometheus/ && chown -R 65534:65534 /prometheus'`, then `docker compose up -d prometheus`.
  - **Predict:** RPO (the data gap) and RTO (time to answer queries again).
  - **Verify:** The fingerprint query returns the same value. RPO equals the time from snapshot to failure, and RTO is timed. Compare both with the §3.2 targets for SLI metrics.
- [ ] **36.2 Grafana from Git only** *(Level: Core)*
  - **Goal:** Test §5.3 "config in Git".
  - **Do:** Build one dashboard in the UI and not in a file. Then delete Grafana's container and data, and bring it back up.
  - **Predict:** What survives.
  - **Verify:** Provisioned datasources and dashboards come back in < 2 min. The UI-only dashboard is gone. Add it to Git.
- [ ] **36.3 Backfill metrics after an outage** *(Level: Core)*
  - **Goal:** Fill a gap as §8.4 describes.
  - **Do:** Export the missing window from another source (for example the Kafka path from 05.1, or a CSV you produce) as OpenMetrics. Run `promtool tsdb create-blocks-from openmetrics data.om /prometheus` inside the container, then restart.
  - **Verify:** The gap in the 36.1 graph is filled. Note the out-of-order limitation.
- [ ] **36.4 Blind during incident** *(Level: Stretch)*
  - **Goal:** Run the §4.4 simulation as a game day (§9).
  - **Do:** While a partner injects an app fault, stop Prometheus and Grafana. Diagnose using only the 28.3 meta stack, Loki and Tempo.
  - **Verify:** Time to diagnose compared with 14.2. The runbook gaps found are written down (§9.3).

**Checkpoint (closed book):**
1. Why does the observability stack need a tighter DR plan than many of the services it watches?
2. Which telemetry needs the lowest RPO, and why?
3. What does a Prometheus snapshot contain, and what is lost between snapshots?
<details><summary>Answers</summary>

1. It is needed most exactly when other things fail. A platform outage during an incident leaves everyone blind.
2. Audit logs (zero loss, compliance) and SLI metrics (minutes), because SLO and budget decisions depend on them. Profiles and traces can tolerate more loss.
3. Hard links to the persisted blocks, plus the head flushed into a block by default. Everything written after the snapshot is lost unless it can be replayed from a durable buffer or remote_write copy.
</details>

---

## 37 — Vendor Migration Patterns  ([chapter](37-vendor-migration-patterns.md))
**Time:** ~3 h · **Needs:** core stack + VictoriaMetrics as the "new vendor"

- [ ] **37.1 Dual-write with reconciliation** *(Level: Core)*
  - **Goal:** Build §4.1 and §4.2.
  - **Do:** Add `remote_write` from Prometheus ("old") to VictoriaMetrics ("new"). Write `reconcile.py`, which runs 15 key queries (RED, SLO, saturation) against both `/api/v1/query` endpoints and reports % difference per query.
  - **Predict:** Which queries drift, and why (§4.3: extrapolation, staleness, step alignment).
  - **Verify:** A table with per-query diff and a pass/fail tolerance you set. Explain every failure.
- [ ] **37.2 Migration inventory** *(Level: Core)*
  - **Goal:** Build the §7.1 inventory and apply "delete instead of migrate" (§7.3).
  - **Do:** List every dashboard, alert, recording rule and runbook link using the 31.3 output. Classify each as migrate, delete or rewrite.
  - **Verify:** The count per class, and the share you avoid migrating.
- [ ] **37.3 Alert parity during a fault** *(Level: Core)*
  - **Goal:** Prove §5.4 before cutover.
  - **Do:** Point `vmalert` at VictoriaMetrics with the same rule files. Inject the 12.2 fault.
  - **Verify:** A firing timeline from both systems side by side. Firing times agree within one evaluation interval, or you have a written explanation.
- [ ] **37.4 Cutover and rollback plan** *(Level: Stretch)*
  - **Goal:** Explain it (§9).
  - **Do:** Write the cutover runbook for a real vendor move (optionally against a SaaS trial): freeze window, go/no-go criteria from 37.1 and 37.3, rollback trigger, and decommission criteria (§10.1).
  - **Verify:** Every go/no-go criterion is a measurable query.

**Checkpoint (closed book):**
1. Why migrate reads (dashboards, alerts) before writes?
2. Why won't dual-written numbers match exactly?
3. When is "delete instead of migrate" the right call?
<details><summary>Answers</summary>

1. With dual-write running, you can validate the new system's answers against the old one before users depend on it, and roll back just by switching reads.
2. Different engines extrapolate, handle staleness and align steps differently, ingestion can be partial, and data arrives at different times. Set tolerances, not equality.
3. When an asset hasn't been viewed or hasn't fired in months, duplicates another one, or has no owner. Migration is a cheap moment to prune.
</details>

---

## 38 — Continuous Verification and Chaos  ([chapter](38-continuous-verification.md))
**Time:** ~4 h · **Needs:** `--profile chaos`; kind + Chaos Mesh for 38.2

- [ ] **38.1 A hypothesis-driven experiment** *(Level: Core)*
  - **Goal:** Use the §3.1 form.
  - **Do:** Point `REDIS_URL` at toxiproxy (`curl -XPOST localhost:8474/proxies -d '{"name":"redis","listen":"0.0.0.0:26379","upstream":"redis:6379"}'`). Write the hypothesis: "with +200 ms on Redis, checkout p99 < 500 ms and errors < 0.5 %". Add `curl -XPOST localhost:8474/proxies/redis/toxics -d '{"type":"latency","attributes":{"latency":200,"jitter":50}}'` for 10 min.
  - **Predict:** Accept or reject, before you run.
  - **Verify:** PromQL for both conditions over the experiment window. Record the verdict and the budget cost (13.3).
- [ ] **38.2 Game day with Chaos Mesh** *(Level: Core)*
  - **Goal:** Run §6.1 on Kubernetes.
  - **Do:** `helm install chaos-mesh chaos-mesh/chaos-mesh -n chaos-mesh --create-namespace --set chaosDaemon.runtime=containerd --set chaosDaemon.socketPath=/run/containerd/containerd.sock`. Apply a `NetworkChaos` (`action: delay`, `latency: 200ms`, selector `app: inventory`, `duration: 10m`), then a `PodChaos` (`action: pod-kill`).
  - **Verify:** Which alerts fired, how long each took, and whether the runbook worked. Record this in the §10.1 cycle format. Probe and restart mechanics are in [`../k8s-learn/pod-tasks.md`](../k8s-learn/pod-tasks.md).
- [ ] **38.3 Automated kill switch** *(Level: Core)*
  - **Goal:** Build §5.4.
  - **Do:** Write `abort.py`, which polls the error-ratio query every 5 s and deletes the toxic (`DELETE /proxies/redis/toxics/latency_downstream`) when the ratio exceeds 2 %. Add a `timeout` toxic that pushes errors past the threshold.
  - **Predict:** Time from breach to abort.
  - **Verify:** Measured abort latency is under 30 s, including Prometheus scrape and rate-window delay. Explain each component of the delay.
- [ ] **38.4 Canary verdict** *(Level: Stretch)*
  - **Goal:** Build §8.3 and §8.4.
  - **Do:** Run `shop-v1` and `shop-v2` (a different `service.version`, and v2 slightly slower) behind nginx with `weight=9` and `weight=1`. Compare latency distributions per version with a Mann-Whitney U test on 5 minutes of span durations from Tempo, or on bucket data.
  - **Verify:** The script returns FAIL for a 50 ms regression and PASS for an identical build.

**Checkpoint (closed book):**
1. What makes a chaos experiment an experiment and not just breaking things?
2. What must be in place before running chaos in production?
3. Why is every deploy a chaos experiment?
<details><summary>Answers</summary>

1. A written, falsifiable hypothesis about steady-state metrics, a bounded blast radius, abort criteria, and a recorded verdict.
2. SLOs and dashboards that show steady state, working alerts, a kill switch, a small initial blast radius, and agreement from the owners and on-call.
3. It changes the system under real traffic. Deployment markers and canary analysis turn it into a controlled comparison with an automatic rollback signal.
</details>

---

## 39 — Build vs Buy  ([chapter](39-build-vs-buy-framework.md))
**Time:** ~2 h · **Needs:** a spreadsheet or Python, results from 31.1

- [ ] **39.1 A 3-year TCO model** *(Level: Core)*
  - **Goal:** Build the §2 model including the "less obvious" costs (§2.3).
  - **Do:** Model self-hosted (infra from 31.1 scaled, plus FTEs, on-call and upgrades) against vendor (list price per host, GB and span, with overage). Use a 200-engineer org with 40 % yearly growth. Find the inflection point (§3).
  - **Verify:** A chart of both curves and the crossover in months. A sensitivity check: which input moves the crossover most?
- [ ] **39.2 Vendor evaluation rubric** *(Level: Stretch)*
  - **Goal:** Apply §8.1 and §8.4.
  - **Do:** Score two options (self-hosted LGTM vs one SaaS) on the §8.1 dimensions with weights that add to 100. Include exit cost in engineer-weeks.
  - **Verify:** A scored table. Changing the top weight by ±10 does not flip the result, or you say that it does.
- [ ] **39.3 Decision record** *(Level: Core)*
  - **Goal:** Explain it (§7.2, §7.4).
  - **Do:** Write an ADR (context, options, decision, consequences) to the VP of Engineering. Include the 12-month forecast trigger that would reopen the decision.
  - **Verify:** The trigger is a number you can measure (spend, series, GB/day) with a date for the next review.

**Checkpoint (closed book):**
1. Name three "less obvious" costs of self-hosting.
2. What is the "we'll save money" trap?
3. What does the hybrid pattern look like?
<details><summary>Answers</summary>

1. On-call for the platform, upgrades and migrations, the hiring and retention of specialists, and the opportunity cost of engineers not building product.
2. Comparing the vendor bill with infrastructure cost alone, and ignoring people, the reliability work, and the months of lagging features during the build.
3. Buy the non-strategic or high-toil parts (for example RUM or error tracking), self-host the high-volume, cost-sensitive parts (for example metrics and logs), and unify them in one query and visualisation layer.
</details>

---

## 40 — IDP and Golden Paths  ([chapter](40-idp-and-golden-paths.md))
**Time:** ~2.5 h · **Needs:** Python (`cookiecutter`), core stack

- [ ] **40.1 Catalog entries with observability annotations** *(Level: Core)*
  - **Goal:** Build §4.1 and §4.2.
  - **Do:** Write a Backstage `catalog-info.yaml` for shop, inventory and worker, with `metadata.annotations` for the Grafana dashboard selector, the PagerDuty/Opsgenie integration, and `backstage.io/techdocs-ref`, plus `spec.owner` and `spec.dependsOn`.
  - **Verify:** A script checks that every service has an owner, a dashboard link that resolves (via Grafana `/api/search`) and a runbook path that exists.
- [ ] **40.2 A golden-path template** *(Level: Core)*
  - **Goal:** Build §7.1 to §7.4 and time it.
  - **Do:** Make a cookiecutter template that generates a FastAPI service with OTel env, a Sloth SLO file (13.1), a RED dashboard JSON (11.1) and a runbook skeleton (14.1). Generate `payments` and add it to the compose file.
  - **Predict:** Minutes from `cookiecutter` to seeing `payments` on the RED dashboard with a loaded SLO.
  - **Verify:** The measured time. `prr_check.py` (17.2) scores the new service ≥ 8/10 with no manual work.
- [ ] **40.3 Scorecard** *(Level: Stretch)*
  - **Goal:** Build §5.1 to §5.4.
  - **Do:** Extend `prr_check.py` into a table across all services (§5.4 cross-team scoreboard) and output Markdown.
  - **Verify:** The scoreboard ranks services, and each failing check links to a fix.

**Checkpoint (closed book):**
1. What is a golden path, and what is the "deviation tax"?
2. Why is the service catalog called the substrate?
3. How does an IDP act as a PRR engine?
<details><summary>Answers</summary>

1. The supported, paved way to build and run a service, with observability built in. The deviation tax is the extra work teams take on themselves when they leave that path.
2. Ownership, dependencies and metadata live there, and scorecards, paging, dashboards and PRR all key off it.
3. Scorecards check PRR items continuously and automatically. Templates meet most items from day one, and the remaining ones show up as visible, owned gaps.
</details>

---

## 41 — Brownfield Integration  ([chapter](41-brownfield-integration.md))
**Time:** ~2.5 h · **Needs:** core stack

- [ ] **41.1 Ingest an "acquired" stack without touching it** *(Level: Core)*
  - **Goal:** Build the §7 unified-but-not-merged pattern.
  - **Do:** Write a legacy service that emits StatsD (`statsd` Python lib, to UDP 8125) and RFC 5424 syslog. Add `statsd` and `syslog` receivers to the collector, and a `resource` processor that sets `source=acquired`.
  - **Verify:** Its metrics are in Prometheus and its logs in Loki, all labelled `source="acquired"`, with no change to the legacy code. Record one gap you cannot fix this way, such as no trace context.
- [ ] **41.2 One dashboard over two backends** *(Level: Core)*
  - **Goal:** Build §6.2 and decide the §6.5 single SLO source.
  - **Do:** Build a Grafana dashboard that uses the `-- Mixed --` datasource to show Prometheus (your stack) and VictoriaMetrics (37.1, standing in for the acquired team's stack) side by side.
  - **Verify:** The journey SLO panel reads from exactly one source, and you have written down why.
- [ ] **41.3 90-day plan** *(Level: Stretch)*
  - **Goal:** Explain it (§4.2, §9).
  - **Do:** Write the §4.2 90-day plan for the acquisition, with a "do not consolidate" list and reasons from §9.1 to §9.4.
  - **Verify:** Every item has an owner, a date and an exit criterion.

**Checkpoint (closed book):**
1. What is the first thing to do in an acquisition's observability integration?
2. When is it right not to consolidate?
3. What does "unified but not merged" mean?
<details><summary>Answers</summary>

1. Inventory: tools, contracts, data flows, alerts, on-call and owners, before changing anything.
2. Specialty tools with no equivalent, compliance-required tools, very high migration cost for little gain, or when the team's skill is concentrated in their stack.
3. Keep the separate backends, but unify at the top: shared conventions and labels, a federated query and visualisation layer, and single SLO definitions.
</details>

---

## 42 — Python Observability  ([chapter](42-python-observability.md))
**Time:** ~5 h · **Needs:** core stack; gunicorn in the app image

- [ ] **42.1 Gunicorn metrics that lie** *(Level: Core)*
  - **Goal:** Reproduce §1.1 and fix it with §5.2.
  - **Do:** Run shop as `gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app` with a `prometheus_client` counter served on `/metrics`. Scrape it 10 times in a row while k6 runs.
  - **Predict:** Is the counter monotonic across scrapes?
  - **Verify:** The values jump around (one worker per scrape), and `rate()` shows false resets. Set `PROMETHEUS_MULTIPROC_DIR`, use `MultiProcessCollector`, and add the `child_exit` hook with `mark_process_dead`. After that the counter is monotonic and equals k6's request count.
- [ ] **42.2 SDK init before fork** *(Level: Core)*
  - **Goal:** Measure §9.1 rule 1 instead of assuming it.
  - **Do:** Initialize the OTel SDK (BatchSpanProcessor + gRPC exporter) at import time, and run gunicorn with `--preload -w 4`. Send exactly 2 000 requests. Count spans stored in Tempo for that window. Then move the init into a `post_fork` hook and repeat.
  - **Predict:** Spans stored in each case.
  - **Verify:** Both counts plus any fork or gRPC warnings in the logs. Write down what your SDK version's at-fork handling does and does not save.
- [ ] **42.3 Blocking inside async** *(Level: Core)*
  - **Goal:** See §10.1 and §10.2.
  - **Do:** Add `async def /bad` that calls `time.sleep(0.2)`. Run 5 RPS against it and 50 RPS against `/checkout`. Measure `/checkout` p99 and event-loop lag (11.5). Change it to `await asyncio.to_thread(time.sleep, 0.2)`.
  - **Predict:** `/checkout` p99 before and after.
  - **Verify:** Before, `/checkout` p99 is at least 200 ms and loop lag is high. After, both are back to baseline. Record the numbers.
- [ ] **42.4 Context lost in a thread pool** *(Level: Core)*
  - **Goal:** Hit §6.2.
  - **Do:** In a handler, `loop.run_in_executor(pool, work)`, where `work` starts a span. Then wrap it: `ctx = contextvars.copy_context(); loop.run_in_executor(pool, ctx.run, work)`.
  - **Predict:** Where `work`'s span appears before the fix.
  - **Verify:** Before, it is a separate root trace. After, it is a child of the handler span.
- [ ] **42.5 SIGTERM, the grace-period cliff, and SIGKILL** *(Level: Core)*
  - **Goal:** Measure §18.2, §18.3 and §18.5. Pod-side shutdown mechanics are in [`../k8s-learn/pod-tasks.md`](../k8s-learn/pod-tasks.md).
  - **Do:** Set `OTEL_BSP_SCHEDULE_DELAY=30000` so spans sit in the batch queue. Send exactly 200 requests before each of three stops: (a) `docker stop shop`, a normal SIGTERM; (b) `docker compose pause otel-collector` then `docker stop -t 3 shop`, so the flush hangs on the exporter; (c) `docker kill shop`, which sends SIGKILL. Unpause the collector after (b). Count stored spans for each run with a Tempo search.
  - **Predict:** Spans lost in (a), (b) and (c).
  - **Verify:** (a) loses about 0, because the SDK's exit-time `shutdown()` or your lifespan hook flushes the queue. (b) loses about 200: the export timeout (10 s default) is longer than the 3 s grace, so the process is SIGKILLed mid-flush. (c) loses about 200, and no code can save it. Fix (b) by setting the grace period ≥ drain + flush budget and `OTEL_EXPORTER_OTLP_TIMEOUT` below it, then re-run (b).
- [ ] **42.6 Observability tests in CI** *(Level: Stretch)*
  - **Goal:** Build §15.2 to §15.4.
  - **Do:** Write tests for the span tree shape of `/checkout` (`InMemorySpanExporter`), a log assertion that `trace_id` is on every line, and a cardinality budget (reuse 18.4). Optionally use `memray run` to find a deliberate leak (§7.3).
  - **Verify:** Each test fails when you break the corresponding code.

**Checkpoint (closed book):**
1. Why do `prometheus_client` counters under Gunicorn prefork give wrong values without multiprocess mode?
2. What breaks when the OTel SDK is initialized before `fork()`, and what is the fix?
3. Why does `time.sleep` in an `async def` handler slow unrelated endpoints?
<details><summary>Answers</summary>

1. Each worker process has its own counter. A scrape reaches one worker, so values jump between workers' totals and look like resets.
2. Background threads (the batch export thread) and network channels (gRPC) don't survive fork correctly in the children. Re-initialize the SDK per worker in `post_fork`, or avoid `--preload`.
3. It blocks the single event-loop thread, so every other coroutine on that worker waits for the sleep to finish.
</details>

---

## Appendices
The appendices are reference material and have no labs of their own. Use them this way:
- [Appendix A — Glossary](appendix-a-glossary.md): pick 10 random terms each week and define them closed book. Check against the glossary.
- [Appendix B — Reference architectures](appendix-b-reference-architectures.md): after the capstones, place your lab on B.1–B.3 and list the three components you would add first to reach the next size (B.4).
- [Appendix C — Query recipe book](appendix-c-query-recipe-book.md): write each C.5 use-case query yourself, then compare with the book. Check every C.6 antipattern against your own rules and dashboards.

---

## Capstone projects

### Capstone 1 — Observable checkout, from zero to three-click diagnosis (2–3 days)
**Chapters:** 02–04, 06–11, 42.
- **Spec:** Starting from an empty repo, rebuild the demo (shop, inventory, worker over Redis, plus a Kafka consumer) with full OTel: RED metrics with exemplars, structured logs with trace ids, propagation across HTTP, Redis and Kafka, continuous profiling, tail sampling at a gateway, and a provisioned RED/USE dashboard set.
- **Acceptance:** A partner injects 5 faults you don't know about: an error burst, tail latency, a CPU hot function, queue backlog, and a poison message. For each, you get from the alert or dashboard to the causal trace, log line or flame-graph frame in ≤ 3 clicks. Tail sampling keeps 100 % of error and slow traces and ≤ 10 % of the rest. `pytest` includes span-shape, log-context and cardinality-budget tests.
- **What to measure:** Time to diagnose for each fault, stored spans/s against received spans/s, bytes/request per signal, and profiler CPU overhead.

### Capstone 2 — SLO-driven operations for one quarter, compressed (2 days)
**Chapters:** 12–17, 20, 38.
- **Spec:** Sloth-generated SLOs for two journeys, MWMBR paging with runbooks gated in CI, an error-budget policy, a PRR score, and a run of 3 chaos experiments and 2 unannounced drills. Each ends in a postmortem written from the §12 template.
- **Acceptance:** Every page links to a runbook. Every drill has a timeline and a postmortem with owned action items. Each chaos hypothesis has a recorded verdict. The PRR score improves by ≥ 3 points from start to end.
- **What to measure:** Predicted vs actual time-to-fire for each burn-rate alert, alert precision (pages that led to action / all pages), budget consumed per experiment, time to detect and time to mitigate per drill, and 16.1-style capacity headroom before and after fixes.

### Capstone 3 — A multi-tenant platform that watches itself (2–3 days)
**Chapters:** 18, 19, 28, 31, 32, 36.
- **Spec:** Loki (and optionally Mimir) with 3 tenants routed by the collector, per-tenant quotas, edge redaction, canary and verifier, a meta-Prometheus with a Watchdog, a showback dashboard, and a Prometheus snapshot/restore runbook.
- **Acceptance:** A noisy tenant is throttled while the others keep their ingest SLO. A PII probe span never reaches storage. Stopping any single backend is detected by the independent path within 2 min. A full Prometheus loss is restored with RPO ≤ your snapshot interval.
- **What to measure:** Per-tenant accepted vs discarded bytes, the pipeline loss ratio under a burst, canary freshness p99, RTO and RPO for the restore drill, and cost per tenant per month at stated prices.

### Capstone 4 — Async and AI pipeline with lakehouse analytics (2–3 days)
**Chapters:** 05, 25, 26, 30, 35, 42.
- **Spec:** Orders flow shop → Kafka → an enrichment consumer that calls a local LLM (Ollama) → Postgres. Tracing covers the whole path, with the queue gap visible. There is a DLQ, time-based lag, error fingerprinting per release, and span data teed to Parquet and queried with DuckDB.
- **Acceptance:** One trace spans HTTP → Kafka → LLM call → DB with GenAI and messaging semconv attributes. A poison message goes to the DLQ and raises an alert. A v2 release with a new bug is flagged as "new in release". DuckDB answers "p99 enrichment latency by customer tier and model" for the last 24 h.
- **What to measure:** Queue-time share of end-to-end latency, time lag during a 2-minute consumer outage, TTFT and tokens/s p50/p99, LLM cost per order, and the DuckDB query time on compacted vs uncompacted Parquet.
