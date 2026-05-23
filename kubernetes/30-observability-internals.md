# Observability Internals: cAdvisor, Prometheus, OpenTelemetry, Loki, Tempo

Every byte of telemetry that comes out of a Kubernetes cluster originates from a small set of places: a cgroup file the kubelet reads via cAdvisor, an in-memory counter in the apiserver, a watch-event the kube-state-metrics process turns into a Prometheus gauge, a structured log line containerd wrote to `/var/log/pods`, an audit record the apiserver emitted before answering a request, or an OTLP span that an application SDK sent to an OpenTelemetry Collector. There is no observability magic. Each metric, each log line, each trace span, each event, each profile sample has a single producer, a single transport, and a single store. The job of a staff engineer is to know all of them, because when the cluster goes blind — when `kubectl top` says `error: metrics not available yet`, when Prometheus stops scraping, when Loki rejects writes with `entry too far behind`, when the trace UI shows nothing for a service that is clearly serving traffic — the only useful debugging tool is *knowing exactly where the signal was supposed to come from*.

This chapter is the staff-engineer's map of the observability stack as it exists on Kubernetes in 2026. We start with the three pillars (metrics, logs, traces) plus the three Kubernetes-specific signals (events, audit, profiles), and we resolve each to a concrete producer in the cluster. We then walk the metrics pipeline bottom-up: **cAdvisor** living inside the kubelet binary reading cgroup v2 files; the **kubelet `/metrics/*` endpoints** every monitoring system scrapes; **metrics-server** as the aggregated API that serves `kubectl top` and HPA from a 60-second in-memory ring buffer; **kube-state-metrics** watching every object and turning the API into Prometheus exposition; and the per-component control-plane metrics (apiserver, scheduler, controller-manager, etcd, kubelet, kube-proxy) that you must scrape if you want to debug anything at scale. We dig into **prometheus-operator** with its ServiceMonitor / PodMonitor / Probe / PrometheusRule CRDs, the canonical **kube-prometheus-stack** install, Prometheus's single-instance scaling limits (~10M active series), and the horizontal options: **Thanos**, **Cortex/Mimir**, **VictoriaMetrics**. We cover **OpenTelemetry**: the Collector pipeline (receivers → processors → exporters), the OpenTelemetryCollector and Instrumentation CRs from the OTel Operator, and the OTLP protocol. We tour **logs** end-to-end: CRI's `/var/log/pods/...` JSON format, the `/var/log/containers` symlinks, Fluent Bit / Fluentd / Vector as DaemonSet shippers, Loki / Elasticsearch / ClickHouse as backends. We map the **trace** path from SDK to OTel Collector to Tempo / Jaeger. We close with cardinality (the silent killer), eBPF-based observability (Pixie, Hubble, Tetragon, Parca), control-plane SLOs, multi-cluster patterns, cost observability, and twenty-plus pitfalls drawn from real production incidents.

This chapter sits adjacent to [ch 10 (kubelet)](10-kubelet-internals.md), where cAdvisor physically lives, [ch 24 (API aggregation)](24-api-aggregation-and-extension-apiservers.md), which is the mechanism metrics-server uses, [ch 22 (autoscaling)](22-autoscaling.md), which is the primary *consumer* of metrics-server, and [ch 05 (apiserver)](05-kube-apiserver-internals.md) and [ch 06 (admission)](06-admission-control-deep-dive.md), where most of the control-plane metrics worth alerting on originate. If you only remember one sentence from this chapter: **a Kubernetes signal is a tuple (producer, transport, store, query layer); every alert you write is a query against the store, every dashboard is a join across multiple stores, and the only operational question that matters is whether you can name each tuple end-to-end for the signal you're staring at.**

---

## Table of Contents

1.  [The Three Pillars Plus Three](#1-the-three-pillars-plus-three)
2.  [Where Every Signal Comes From](#2-where-every-signal-comes-from)
3.  [cAdvisor: The Container Advisor Inside the Kubelet](#3-cadvisor-the-container-advisor-inside-the-kubelet)
4.  [The Kubelet `/metrics/*` Endpoints](#4-the-kubelet-metrics-endpoints)
5.  [`/stats/summary` and the Resource Metrics API](#5-statssummary-and-the-resource-metrics-api)
6.  [metrics-server: The Aggregated Metrics API](#6-metrics-server-the-aggregated-metrics-api)
7.  [kube-state-metrics: One Watch per Kind, Cardinality per Object](#7-kube-state-metrics-one-watch-per-kind-cardinality-per-object)
8.  [KSM Custom Resource Metrics](#8-ksm-custom-resource-metrics)
9.  [Control-Plane Metrics: apiserver](#9-control-plane-metrics-apiserver)
10. [Control-Plane Metrics: scheduler](#10-control-plane-metrics-scheduler)
11. [Control-Plane Metrics: controller-manager](#11-control-plane-metrics-controller-manager)
12. [Control-Plane Metrics: etcd](#12-control-plane-metrics-etcd)
13. [Data-Plane Metrics: kubelet and kube-proxy](#13-data-plane-metrics-kubelet-and-kube-proxy)
14. [The Prometheus Ecosystem](#14-the-prometheus-ecosystem)
15. [prometheus-operator and the kube-prometheus-stack](#15-prometheus-operator-and-the-kube-prometheus-stack)
16. [ServiceMonitor, PodMonitor, Probe, PrometheusRule](#16-servicemonitor-podmonitor-probe-prometheusrule)
17. [Service Discovery for Scrapes](#17-service-discovery-for-scrapes)
18. [Push vs Pull, and Where the Pushgateway Belongs](#18-push-vs-pull-and-where-the-pushgateway-belongs)
19. [Recording Rules and Alert Rules](#19-recording-rules-and-alert-rules)
20. [The Four Golden Signals per Component](#20-the-four-golden-signals-per-component)
21. [Scaling Prometheus: Sharding, Thanos, Mimir, VictoriaMetrics](#21-scaling-prometheus-sharding-thanos-mimir-victoriametrics)
22. [OpenTelemetry: One Pipeline for Three Signals](#22-opentelemetry-one-pipeline-for-three-signals)
23. [The OpenTelemetry Operator: Collector and Instrumentation CRs](#23-the-opentelemetry-operator-collector-and-instrumentation-crs)
24. [OTLP: gRPC and HTTP](#24-otlp-grpc-and-http)
25. [Logs: From stdout to a Query](#25-logs-from-stdout-to-a-query)
26. [Log Shippers: Fluent Bit, Fluentd, Vector](#26-log-shippers-fluent-bit-fluentd-vector)
27. [Log Aggregators: Loki, Elasticsearch, ClickHouse](#27-log-aggregators-loki-elasticsearch-clickhouse)
28. [Traces: SDK to Backend](#28-traces-sdk-to-backend)
29. [Continuous Profiling: Parca, Pyroscope, Polar Signals](#29-continuous-profiling-parca-pyroscope-polar-signals)
30. [Kubernetes Events as a Telemetry Signal](#30-kubernetes-events-as-a-telemetry-signal)
31. [Audit Logs: The Forensic Stream](#31-audit-logs-the-forensic-stream)
32. [The "What's Slow" Debugging Tree](#32-the-whats-slow-debugging-tree)
33. [Cardinality: The Silent Killer](#33-cardinality-the-silent-killer)
34. [Per-Pod Resource Accounting at Scale, PSI](#34-per-pod-resource-accounting-at-scale-psi)
35. [eBPF Observability: Pixie, Hubble, Tetragon, Parca](#35-ebpf-observability-pixie-hubble-tetragon-parca)
36. [Control-Plane SLOs and SLIs](#36-control-plane-slos-and-slis)
37. [Dashboards and Drift](#37-dashboards-and-drift)
38. [Cost Observability: Kubecost, OpenCost](#38-cost-observability-kubecost-opencost)
39. [Multi-Cluster Observability](#39-multi-cluster-observability)
40. [Pitfalls](#40-pitfalls)
41. [TL;DR](#41-tldr)

---

## 1. The Three Pillars Plus Three

The conventional framing of observability lists three pillars: **metrics** (numeric time series), **logs** (timestamped text or structured records), and **traces** (causally linked spans across services). On Kubernetes the picture is richer. There are at least three more first-class signals: **events** (the apiserver's own `core/v1/Event` object stream — short-lived narrative facts like "FailedScheduling" or "BackOff"), **audit** (a structured stream of every apiserver request, kept for forensics and compliance), and **profiles** (CPU/heap/lock samples emitted by `pprof` endpoints on every Go component and increasingly by eBPF agents for arbitrary processes).

```
              ┌──────────────────────────────────────────────────────────┐
              │   SIX SIGNALS A K8s CLUSTER PRODUCES                     │
              └──────────────────────────────────────────────────────────┘

  METRICS                LOGS                  TRACES
  ───────                ────                  ──────
  numeric, regular       timestamped lines     causal spans across
  samples, low           or JSON records       services with a single
  cardinality if         per process /         shared trace-id;
  you behave             container             OTLP/W3C traceparent

      ▲                       ▲                       ▲
      │ scrape /metrics       │ stdout/stderr →       │ SDK in app →
      │ every 15-60s          │ /var/log/pods         │ OTLP → Collector
      │                       │ → shipper             │ → backend
      │                       │
   Prometheus,             Loki, Elastic,         Tempo, Jaeger,
   Mimir, VM,              ClickHouse,            Zipkin, Honeycomb
   Thanos                  Splunk

  EVENTS                 AUDIT                 PROFILES
  ──────                 ─────                 ────────
  apiserver-stored       structured JSON of    /debug/pprof on every
  narrative facts;       every apiserver       Go binary; eBPF-based
  short TTL (~1h)        request, classed by   continuous profiling
                         stage + level         (Parca, Pyroscope)

      ▲                       ▲                       ▲
      │ watch core/v1/Event   │ apiserver writes      │ pprof scrape
      │ → exporter            │ to file/webhook       │ or eBPF sampler

   event-exporter →       SIEM (Splunk,         Parca, Pyroscope,
   Slack / Loki /         Elastic, Datadog),    Polar Signals
   OTLP                   Loki, BigQuery
```

The first thing a staff engineer does when inheriting a cluster is enumerate which of these six signals are actually being produced and stored. A cluster with metrics but no events is debuggable but mysterious. A cluster with logs but no traces still ships, but cross-service debugging is reduced to grep. A cluster without an audit log is undebuggable for security incidents — when somebody asks "who deleted that namespace?" you have no answer.

The rest of the chapter resolves each of those six signals to (producer, transport, store, query). Once you have that map, every operational question becomes "which row of the map is wrong?"

---

## 2. Where Every Signal Comes From

The Kubernetes-native source for each signal is small and finite. Memorize this table. Every later section deepens one row.

| Signal | Producer in K8s | Transport | Store | Query layer |
|---|---|---|---|---|
| Container CPU/mem/net/fs metrics | cAdvisor inside kubelet, reading cgroup v2 / network ns counters | HTTP scrape on `:10250/metrics/cadvisor` | Prometheus / Mimir / VM | PromQL |
| Per-pod resource metrics for HPA | kubelet `/metrics/resource` (the "summary API in Prometheus shape") | Aggregated API `metrics.k8s.io` | metrics-server memory (last 1 sample, ~60s) | `kubectl top`, HPA, VPA |
| Node-level CPU/mem/disk | node-exporter DaemonSet, reading `/proc`, `/sys` | HTTP scrape `:9100/metrics` | Prometheus | PromQL |
| Object-state metrics (pod phase, deployment replicas, …) | kube-state-metrics Deployment, watching apiserver | HTTP scrape `:8080/metrics` | Prometheus | PromQL |
| Control-plane metrics | apiserver, scheduler, controller-manager, etcd, kubelet, kube-proxy expose `/metrics` | HTTP scrape on each component's secure port | Prometheus | PromQL |
| Container logs | App writes stdout/stderr → containerd → CRI log file `/var/log/pods/...` | DaemonSet shipper (Fluent Bit / Vector) | Loki / Elastic / ClickHouse / Splunk | LogQL / Lucene / SQL |
| System logs (kubelet, containerd journal) | systemd-journald | journalctl scrape or Vector journald source | same | same |
| Distributed traces | Instrumented app SDK (OTel) | OTLP/gRPC or OTLP/HTTP → OTel Collector | Tempo / Jaeger / Honeycomb | TraceQL / Jaeger UI |
| Events | core/v1/Event objects (etcd-backed, 1h TTL) | kubernetes-event-exporter watches them | Loki / OTLP / Slack / PagerDuty | LogQL / etc. |
| Audit | apiserver audit backend (log/webhook) | webhook → collector, or file → shipper | SIEM + Loki | Lucene / LogQL / SQL |
| Profiles | `/debug/pprof/{profile,heap,goroutine,block,mutex}` on every Go component; eBPF samplers for arbitrary processes | scrape by Parca/Pyroscope/Grafana Agent | Parca DB / Pyroscope / S3 | flamegraph UI |

Two structural observations matter. First, **every metric is pulled, not pushed** (except OpenTelemetry's OTLP and the Prometheus Pushgateway corner case — see §18). Second, **every signal has a fan-in component** between the producer and the store: scrapers for metrics, DaemonSet shippers for logs, the OTel Collector for traces. The fan-in is where you do filtering, sampling, batching, and retries, and it is also where most production outages occur (a wedged Fluent Bit fills `/var/log/pods`; a misconfigured OTel Collector drops 100% of spans). Treat the fan-in as a tier-1 component.

---

## 3. cAdvisor: The Container Advisor Inside the Kubelet

`cAdvisor` is the source of every per-container resource metric in Kubernetes. It is a project ([google/cadvisor on GitHub](https://github.com/google/cadvisor)) that originally ran as its own DaemonSet but has, since Kubernetes 1.7, been **vendored into the kubelet binary itself** (`vendor/github.com/google/cadvisor` in `kubernetes/kubernetes`). When the kubelet starts, it spawns the cAdvisor manager as an in-process goroutine. There is no separate process, no separate container, no separate port — cAdvisor's HTTP endpoints are served by the kubelet's own server on port 10250.

```
        ┌────────────────────────────────────────────────────────────────┐
        │  NODE                                                          │
        │                                                                │
        │    ┌──────────────────────────────────────────────────────┐   │
        │    │  kubelet process (single binary)                     │   │
        │    │                                                       │   │
        │    │  ┌─────────────────────────────────────────────┐    │   │
        │    │  │  cAdvisor (in-process goroutine)             │    │   │
        │    │  │                                              │    │   │
        │    │  │  Housekeeping loop (every 1s by default):    │    │   │
        │    │  │    for cgroup in /sys/fs/cgroup/...:         │    │   │
        │    │  │      read cpu.stat, memory.current,           │    │   │
        │    │  │           memory.events, io.stat,             │    │   │
        │    │  │           network counters via netns          │    │   │
        │    │  │      store sample in ring buffer (default 60s)│    │   │
        │    │  └─────────────────────────────────────────────┘    │   │
        │    │                                                       │   │
        │    │  HTTP server on :10250 (TLS, requires auth):         │   │
        │    │    /metrics                  (kubelet runtime)        │   │
        │    │    /metrics/cadvisor         (CONTAINERS)             │   │
        │    │    /metrics/resource         (resource metrics API)   │   │
        │    │    /metrics/probes           (probe success/failure)  │   │
        │    │    /stats/summary            (JSON, hierarchical)     │   │
        │    │    /pods                     (running pods)           │   │
        │    │    /spec, /healthz, /pprof   (debug)                  │   │
        │    └──────────────────────────────────────────────────────┘   │
        │                                                                │
        │    ┌──────────────────────────────────────────────────────┐   │
        │    │  Linux kernel: cgroup-v2 hierarchy                   │   │
        │    │   /sys/fs/cgroup/kubepods.slice/                     │   │
        │    │     kubepods-burstable.slice/                        │   │
        │    │       kubepods-burstable-pod<uid>.slice/             │   │
        │    │         cri-containerd-<container-id>.scope/         │   │
        │    │           cpu.stat, memory.current, io.stat, ...     │   │
        │    └──────────────────────────────────────────────────────┘   │
        └────────────────────────────────────────────────────────────────┘
```

### What cAdvisor reads

For every running container, cAdvisor reads:

- **CPU**: `cpu.stat` (usage_usec, user_usec, system_usec, throttled_usec, nr_throttled, nr_periods) from cgroup v2. CPU pressure from `cpu.pressure`.
- **Memory**: `memory.current` (bytes in use), `memory.peak`, `memory.events` (oom, oom_kill), `memory.stat` (anon, file, kernel_stack, slab, sock), `memory.pressure`.
- **Block I/O**: `io.stat` (rbytes, wbytes, rios, wios per device), `io.pressure`.
- **Network**: counters in `/proc/<pid>/net/dev` from any process in the pod's network namespace, or via netlink for the pod sandbox.
- **Filesystem**: per-container writable layer usage from the CRI (containerd reports it from the snapshotter).

Each sample becomes a Prometheus metric named `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`, `container_network_receive_bytes_total`, etc., labelled with `pod`, `namespace`, `container`, `image`, `id` (cgroup path).

### The endpoints

The kubelet exposes cAdvisor's data on three Prometheus-shaped endpoints and one JSON endpoint:

- `:10250/metrics/cadvisor` — per-container metrics, **full** set including network, FS, throttling, OOM, hundreds of series per container. This is what Prometheus scrapes for "what is each container doing?"
- `:10250/metrics/resource` — a **minimal**, stable set used by `metrics-server` to compute pod-level CPU and memory. Just `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`, `node_cpu_usage_seconds_total`, `node_memory_working_set_bytes`. This is the "Resource Metrics API" in Prometheus shape.
- `:10250/metrics/probes` — probe success/failure counters from the kubelet's probe manager.
- `:10250/stats/summary` — JSON hierarchical summary of node + pods + containers, used by some legacy consumers and by Heapster's descendants.

The kubelet also exposes its own runtime metrics on `:10250/metrics` — these are *not* cAdvisor metrics; they describe the kubelet itself (see §13).

### Why this matters

A staff engineer who knows that cAdvisor lives inside the kubelet never wastes time looking for "the cAdvisor pod." When `container_cpu_usage_seconds_total` stops moving, the kubelet is broken; when only `container_network_*` stops, you have a netns enumeration issue (often a CNI bug). When the kubelet OOMs on a busy node, the entire metrics stream for that node disappears — including `metrics-server`'s scrape, so HPA goes blind too. The blast radius of "kubelet crashed" extends well beyond the workloads on that node.

cAdvisor itself has known scalability cliffs on huge nodes. Reading every cgroup file every second on a node with thousands of containers becomes expensive (§34). PSI-based signals (cgroup-v2 pressure) reduce this cost dramatically — instead of polling every container, you sample the node-level pressure files.

---

## 4. The Kubelet `/metrics/*` Endpoints

Distinct from cAdvisor, the kubelet exposes its **own** runtime metrics on `/metrics`. These describe how the kubelet is doing, not what the containers are doing. They are essential for debugging pod startup latency, PLEG stalls, eviction storms, and probe failures.

```
   kubelet :10250 endpoints (HTTPS, requires `nodes/metrics` or `nodes/proxy` RBAC):

   ┌──────────────────────────────────────────────────────────────────────┐
   │  Endpoint                Purpose                                     │
   ├──────────────────────────────────────────────────────────────────────┤
   │  /metrics                Kubelet RUNTIME metrics (this section)      │
   │                            kubelet_pleg_*, kubelet_runtime_*,        │
   │                            kubelet_volume_stats_*, kubelet_pod_*     │
   ├──────────────────────────────────────────────────────────────────────┤
   │  /metrics/cadvisor       Per-CONTAINER metrics from cAdvisor         │
   │                            container_cpu_*, container_memory_*,      │
   │                            container_network_*, container_fs_*       │
   ├──────────────────────────────────────────────────────────────────────┤
   │  /metrics/resource       The "Resource Metrics API" in Prom shape.   │
   │                          Tiny stable set scraped by metrics-server.  │
   │                            container_cpu_usage_seconds_total         │
   │                            container_memory_working_set_bytes        │
   │                            node_cpu_usage_seconds_total              │
   │                            node_memory_working_set_bytes             │
   ├──────────────────────────────────────────────────────────────────────┤
   │  /metrics/probes         Probe results: liveness, readiness, startup │
   │                            prober_probe_total{probe_type, result}    │
   ├──────────────────────────────────────────────────────────────────────┤
   │  /stats/summary          JSON, hierarchical: node + pods + containers│
   │                          Used by legacy tools and metrics-server     │
   │                          fall-back (when /metrics/resource missing)  │
   ├──────────────────────────────────────────────────────────────────────┤
   │  /pods                   List of pods this kubelet thinks it's       │
   │                          running (NOT from apiserver — from local    │
   │                          state). Useful for diagnosing drift.        │
   ├──────────────────────────────────────────────────────────────────────┤
   │  /healthz, /pprof, /spec Standard Go binary endpoints                │
   └──────────────────────────────────────────────────────────────────────┘
```

### The runtime metrics that matter

| Metric | What it tells you | Alert if |
|---|---|---|
| `kubelet_pleg_relist_duration_seconds` (histogram) | How long the PLEG takes to enumerate containers via CRI | p99 > 3s — PLEG is wedged, pods will be marked NotReady |
| `kubelet_pleg_relist_interval_seconds` | Time between successive relists; >1s means slow | p99 > 5s |
| `kubelet_pod_start_duration_seconds` | Time from pod creation to Running | p99 > 60s for non-image-pulling pods |
| `kubelet_pod_worker_duration_seconds{operation_type}` | Per-pod-worker operation latency | p99 > 30s on `create` |
| `kubelet_runtime_operations_duration_seconds{operation_type}` | CRI gRPC call latency (RunPodSandbox, PullImage, …) | p99 > 5s on `RunPodSandbox` |
| `kubelet_runtime_operations_errors_total` | CRI gRPC error counter | rate > 0 sustained |
| `kubelet_volume_stats_used_bytes` / `_capacity_bytes` | Per-PVC usage; PVCs with `kubernetes.io/volume-stats=true` annotation | usage/capacity > 0.9 |
| `kubelet_evictions{eviction_signal}` | Eviction events by signal (memory.available, …) | rate > 0 sustained |
| `kubelet_image_pull_duration_seconds` | Time spent pulling images | p99 > 5min |
| `prober_probe_total{probe_type,result}` | From /metrics/probes; count of probe runs | success ratio < 0.95 |

PLEG (the Pod Lifecycle Event Generator — see [ch 10](10-kubelet-internals.md)) is the kubelet's heartbeat to the CRI. A wedged PLEG is the single most common "node is dead but not really" symptom. Always graph `kubelet_pleg_relist_duration_seconds` and alert on the p99.

---

## 5. `/stats/summary` and the Resource Metrics API

Before Prometheus existed and before Kubernetes had a metrics API, the kubelet served container statistics as a JSON document on `/stats/summary`. The format is hierarchical:

```
{
  "node": {
    "nodeName": "node-2",
    "cpu":    {"time": "...", "usageNanoCores": 1532000000, "usageCoreNanoSeconds": ...},
    "memory": {"availableBytes": ..., "usageBytes": ..., "workingSetBytes": ..., "rssBytes": ...},
    "network": {...},
    "fs": {...},
    "runtime": {"imageFs": {...}},
    "rlimit": {...}
  },
  "pods": [
    {
      "podRef": {"name": "nginx-abc", "namespace": "default", "uid": "..."},
      "cpu": {...}, "memory": {...},
      "containers": [
        {"name": "nginx", "cpu": {...}, "memory": {...}, "rootfs": {...}, "logs": {...}}
      ],
      "volumes": [
        {"name": "data", "pvcRef": {...}, "usedBytes": ..., "capacityBytes": ...}
      ]
    }
  ]
}
```

The `Resource Metrics API` in Kubernetes is a **higher-level abstraction** over the same data. There are two paths to it:

1. **`/metrics/resource`** on the kubelet — Prometheus exposition format, the modern path, four metrics only (node/container × cpu/memory_working_set). This is what metrics-server scrapes since v0.5.
2. **`/stats/summary`** — JSON, used by metrics-server only as a fallback (and by some non-Prometheus consumers).

The `metrics.k8s.io` aggregated API (served by metrics-server) lifts those numbers to cluster-level queries:

- `GET /apis/metrics.k8s.io/v1beta1/nodes` — per-node CPU + memory
- `GET /apis/metrics.k8s.io/v1beta1/namespaces/<ns>/pods` — per-pod CPU + memory
- `GET /apis/metrics.k8s.io/v1beta1/nodes/<node>` — one node
- `GET /apis/metrics.k8s.io/v1beta1/namespaces/<ns>/pods/<pod>` — one pod

The output of `kubectl top pod` is exactly an HTTP GET to the second URL.

```
$ kubectl get --raw /apis/metrics.k8s.io/v1beta1/namespaces/default/pods/nginx-abc | jq
{
  "kind": "PodMetrics",
  "metadata": {"name": "nginx-abc", "namespace": "default", ...},
  "timestamp": "2026-05-23T12:00:00Z",
  "window": "30s",
  "containers": [
    {
      "name": "nginx",
      "usage": {"cpu": "5m", "memory": "12Mi"}
    }
  ]
}
```

The `window` field is the **time between the two samples metrics-server used to compute the rate**. Critically, metrics-server stores only the last *one* sample plus the previous one — it has no historical data. For history, you need Prometheus to scrape the same `/metrics/resource` endpoint with retention.

---

## 6. metrics-server: The Aggregated Metrics API

`metrics-server` is a small (~50 MB RSS at idle) Deployment that registers itself as the backend for the `metrics.k8s.io` APIService. It is the canonical example of [API aggregation](24-api-aggregation-and-extension-apiservers.md): the apiserver proxies requests for `*.metrics.k8s.io` to the metrics-server pod transparently.

### Architecture

```
                              metrics-server architecture
                              ───────────────────────────

  ┌──────────────────────────────────────────────────────────────────────────┐
  │                                                                          │
  │     ┌─────────────────────────────────────────────────────────────┐     │
  │     │  HPA controller (kube-controller-manager)                    │     │
  │     │  every 15s: GET metrics.k8s.io/.../pods                      │     │
  │     └────────────────────────────┬────────────────────────────────┘     │
  │                                  │                                       │
  │     ┌────────────────────────────▼────────────────────────────────┐     │
  │     │  kubectl top                                                 │     │
  │     │  GET metrics.k8s.io/v1beta1/nodes                            │     │
  │     └────────────────────────────┬────────────────────────────────┘     │
  │                                  │                                       │
  │     ┌────────────────────────────▼────────────────────────────────┐     │
  │     │  kube-apiserver                                              │     │
  │     │   APIService registry: metrics.k8s.io → metrics-server svc   │     │
  │     │   Aggregation proxy:                                         │     │
  │     │     - extracts user from request                             │     │
  │     │     - calls into metrics-server with X-Remote-User           │     │
  │     │     - returns response unchanged                             │     │
  │     └────────────────────────────┬────────────────────────────────┘     │
  │                                  │ HTTPS                                 │
  │                                  ▼                                       │
  │     ┌────────────────────────────────────────────────────────────┐     │
  │     │  metrics-server Deployment (1–2 replicas, HA-able)          │     │
  │     │                                                              │     │
  │     │  Internal:                                                   │     │
  │     │    Scraper goroutine (per node):                             │     │
  │     │      every 60s (--metric-resolution):                        │     │
  │     │        GET https://<kubeletIP>:10250/metrics/resource        │     │
  │     │        parse Prometheus exposition                           │     │
  │     │        compute rate over (previous sample, this sample)      │     │
  │     │        store {nodeName, podRef, containerName, cpu, mem,     │     │
  │     │               window} in memory                              │     │
  │     │                                                              │     │
  │     │    REST API:                                                 │     │
  │     │      /apis/metrics.k8s.io/v1beta1/{nodes,pods}                │     │
  │     │      reads from the in-memory map, returns                   │     │
  │     │                                                              │     │
  │     │  Storage: a single map of last sample. NOT a database.        │     │
  │     │           If metrics-server crashes, history is gone.          │     │
  │     └────────────────────────────────────────────────────────────┘     │
  │                                  │  scrape every 60s                     │
  │                                  ▼                                       │
  │     ┌──────────────────────────────────────────────────────────┐       │
  │     │  Every kubelet exposes /metrics/resource                  │       │
  │     │  Auth: client cert (metrics-server's SA token, via TLS    │       │
  │     │        bootstrap, mounted via projected service account)  │       │
  │     └──────────────────────────────────────────────────────────┘       │
  └──────────────────────────────────────────────────────────────────────────┘
```

### Key properties

- **Stateless**, **lightweight**, **HA-able**. Two replicas with a `topologySpreadConstraint` is the production pattern.
- **In-memory only**. No etcd, no DB, no PVC. Restart = clean slate. `kubectl top` returns `error: metrics not available yet` for the first 60–120s after a restart.
- **Default scrape interval: 60s** (`--metric-resolution`). HPA reads at 15s by default — so HPA will get the same sample multiple times. The metrics-server scrape interval is the **floor** on HPA reactivity.
- **One sample retained per container** plus the previous one (used for rate calculation). No history.
- **CPU is computed as a rate over the window**; memory is reported as `working_set_bytes` (anonymous + file-backed, minus reclaimable). Working set is the **OOM signal** — when it exceeds memory.max, the cgroup is OOM-killed.

### Common failure modes

1. **APIService not Available.** `kubectl get apiservices | grep metrics` shows `v1beta1.metrics.k8s.io  False (FailedDiscoveryCheck)`. Cause: metrics-server pod not running, or the apiserver can't reach it (NetworkPolicy, missing CA cert). HPA fails immediately.
2. **TLS verification failures on kubelet scrape.** metrics-server by default verifies kubelet certs against the cluster CA. On bare-metal kubeadm clusters the kubelet serving cert is self-signed unless `serverTLSBootstrap: true` is set. Workaround: `--kubelet-insecure-tls` (acceptable in homelabs, never in prod).
3. **High-pod-count clusters running out of memory.** metrics-server stores all samples in memory; on a 5000-node, 100k-pod cluster you need to size it accordingly (typical: 500 Mi requests).
4. **Metric "not yet available" for new pods.** Pods need to be alive for at least one scrape interval (60s) to appear. Brand-new pods cannot be HPA-targeted by CPU until the first scrape completes.

### What it is *not*

metrics-server is **not Prometheus**. It does not answer arbitrary queries. It does not store history. It does not expose recording rules. It exists solely to feed the `metrics.k8s.io` API for HPA, VPA, kubectl top, and the scheduler's resource-based preemption logic. If you want history of pod CPU usage, you scrape `/metrics/cadvisor` (or `/metrics/resource`) into Prometheus separately. The two paths share the kubelet endpoint but are otherwise independent.

Source: [`kubernetes-sigs/metrics-server`](https://github.com/kubernetes-sigs/metrics-server). Particularly `pkg/scraper/client/resource/resource.go` for the Prom-format parser and `pkg/storage/storage.go` for the in-memory store.

---

## 7. kube-state-metrics: One Watch per Kind, Cardinality per Object

`kube-state-metrics` (KSM) is the **object-state** half of Kubernetes monitoring. cAdvisor tells you what containers are doing on the node; KSM tells you what the apiserver thinks about every object in the cluster. The two are orthogonal: a pod with `status.phase=Failed` because the image pull failed has zero container metrics (no container ever ran) and one `kube_pod_status_phase{phase="Failed"} = 1` from KSM.

### Architecture

```
                              kube-state-metrics
                              ──────────────────

   ┌────────────────────────────────────────────────────────────────────┐
   │  KSM Deployment (default 1 replica; sharded for big clusters)      │
   │                                                                    │
   │  For every supported kind (Pod, Deployment, Service, PV, PVC,      │
   │  Node, Job, CronJob, HPA, …):                                       │
   │    SharedInformer over (kind, "" namespace)                         │
   │       → InMemoryStore                                               │
   │       → Build metric families on each event                         │
   │                                                                    │
   │  HTTP server on :8080/metrics:                                      │
   │    For each kind, for each object, emit metric family               │
   │      kube_pod_info{namespace,pod,host_ip,pod_ip,uid,node,...}        │
   │      kube_pod_status_phase{namespace,pod,phase}=0|1                 │
   │      kube_pod_container_status_restarts_total{...}                  │
   │      kube_deployment_status_replicas{namespace,deployment}          │
   │      kube_node_status_condition{node,condition,status}              │
   │      kube_persistentvolume_status_phase{phase, persistentvolume}    │
   │      kube_service_info{service, cluster_ip, type, ...}              │
   │      kube_hpa_status_current_replicas{...}                          │
   │    ... (hundreds of metric families across all kinds)               │
   │                                                                    │
   │  Telemetry on :8081/metrics (KSM's own runtime metrics).            │
   └────────────────────────────────────────────────────────────────────┘
                                    │ watch
                                    ▼
                            kube-apiserver
```

KSM does **not** scrape anything. It is a pure transformer: watch the apiserver, project each object into a set of Prometheus metrics. The latency from "object created" to "metric scrapable" is the sum of the apiserver watch latency (typically <100ms) plus Prometheus scrape interval (typically 30s).

### Sharding

A single KSM instance keeps every object in memory. On a cluster with 200k pods + 50k services + 30k secrets, that is ~10 GB of RSS and a `/metrics` response of >100 MB. Prometheus scraping such a target is its own scaling problem.

KSM supports **horizontal sharding** out of the box: each replica handles a slice of the cluster by hashing object UIDs. Configured as:

```yaml
args:
  - --shard=$(POD_ORDINAL)
  - --total-shards=8
```

Deploy as a StatefulSet so each replica has a stable ordinal. Combined with a PodMonitor selector, Prometheus scrapes each shard separately and combines results via PromQL.

For per-shard distribution, KSM uses an FNV-1a hash over the object UID and takes `hash mod total-shards`. The same object always lands on the same shard, so metrics don't flap during rebalance — but adding or removing shards still re-shards everything, so plan capacity carefully.

Source: [`kubernetes/kube-state-metrics`](https://github.com/kubernetes/kube-state-metrics).

### The metrics that matter

A handful of KSM metrics underpin most "is the cluster healthy?" dashboards:

| Metric | Purpose |
|---|---|
| `kube_pod_status_phase{phase="Pending"\|"Running"\|"Failed"\|"Succeeded"\|"Unknown"}` | Pod count by phase |
| `kube_pod_container_status_waiting_reason{reason}` | Why containers are stuck (`ImagePullBackOff`, `CrashLoopBackOff`, `CreateContainerConfigError`) |
| `kube_pod_container_status_restarts_total` | Crash loop detector |
| `kube_pod_container_resource_requests{resource}` / `_limits` | Cluster allocation totals |
| `kube_node_status_condition{condition="Ready"\|"MemoryPressure"\|"DiskPressure"\|"NetworkUnavailable", status="true"\|"false"\|"unknown"}` | Node health summary |
| `kube_deployment_status_replicas` / `_available` / `_unavailable` | Rollout state |
| `kube_replicaset_status_ready_replicas` | Per-RS readiness |
| `kube_persistentvolume_status_phase{phase}` | PV state |
| `kube_hpa_status_current_replicas` / `_desired_replicas` | HPA effect |
| `kube_job_status_failed` | Failed jobs |

These metrics are **gauges** (snapshots), not counters, with one important exception: `kube_pod_container_status_restarts_total` is a counter. Treat the rest as instantaneous truth.

---

## 8. KSM Custom Resource Metrics

KSM's most underused feature is **custom resource state metrics** — a declarative way to expose Prometheus metrics for arbitrary CRDs without writing Go code.

You feed KSM a YAML configuration that names the GVR (group/version/resource) and JSONPath-style expressions for each metric. KSM walks every CR matching the spec and produces metrics with the names and labels you defined.

```yaml
# custom-resource-state-config.yaml
kind: CustomResourceStateMetrics
spec:
  resources:
    - groupVersionKind:
        group: pkg.crossplane.io
        version: v1
        kind: Provider
      labelsFromPath:
        provider: [metadata, name]
      metrics:
        - name: crossplane_provider_installed
          help: "1 if the Provider is Installed"
          each:
            type: StateSet
            stateSet:
              labelName: status
              path: [status, conditions]
              list: ["Healthy", "Installed"]
              valueFrom: [status]
        - name: crossplane_provider_revision_image_pull_policy
          help: "Image pull policy of the active revision"
          each:
            type: Info
            info:
              labelsFromPath:
                image: [spec, package]
                pull_policy: [spec, packagePullPolicy]
```

Mount this ConfigMap and pass `--custom-resource-state-config-file=/etc/ksm/config.yaml`. KSM now watches `providers.pkg.crossplane.io` and emits `crossplane_provider_installed{provider, status} 1`.

This is the production-friendly way to expose operator state to Prometheus without modifying the operator binary. It is how teams expose ArgoCD application sync status, Crossplane composition health, Strimzi Kafka cluster state, and dozens of other operator-managed resources.

---

## 9. Control-Plane Metrics: apiserver

The apiserver is the busiest control-plane component and the one whose metrics most often predict outage. Every apiserver exposes a Prometheus `/metrics` endpoint on its secure port (typically 6443). Scraping it requires a ServiceAccount with the `system:monitoring` ClusterRole (or `view` + a non-resource URL grant for `/metrics`).

### The metrics that matter

```promql
# 1. REQUEST LATENCY (the canonical "is the apiserver slow?" signal)
apiserver_request_duration_seconds_bucket{verb, resource, subresource, scope, group, version}
  # Histogram. Use histogram_quantile.
  # Buckets are configurable; default goes up to 60s.

# 2. INFLIGHT REQUESTS
apiserver_current_inflight_requests{request_kind="mutating"|"readOnly"}
  # If sustained near the configured max, APF is throttling.

# 3. APF (API Priority and Fairness) — request flow control
apiserver_flowcontrol_dispatched_requests_total{flow_schema, priority_level}
apiserver_flowcontrol_rejected_requests_total{flow_schema, priority_level, reason}
apiserver_flowcontrol_current_inqueue_requests{flow_schema, priority_level}
apiserver_flowcontrol_request_concurrency_in_use{priority_level}
apiserver_flowcontrol_request_concurrency_limit{priority_level}

# 4. STORAGE — apiserver → etcd
etcd_request_duration_seconds_bucket{operation, type}
  # Histogram of GET/PUT/DELETE/RANGE/TXN latency to etcd
apiserver_storage_objects{resource}
  # Object count per kind. CRITICAL: the dominant cost driver.

# 5. ADMISSION
apiserver_admission_controller_admission_duration_seconds_bucket{name, type, operation}
apiserver_admission_webhook_admission_duration_seconds_bucket{name, type, operation}
apiserver_admission_webhook_rejection_count{name, type, operation, error_type, rejection_code}

# 6. WATCH
apiserver_longrunning_requests{verb, resource, group, version, scope}
  # Active LIST and WATCH streams. The number should be < max-mutating-requests-inflight.
apiserver_registered_watchers{group, kind}

# 7. AUTH
apiserver_request_total{verb, code, ...}  # any 401, 403 means auth failure
authentication_attempts{result="success"|"failure"|"error"}
authorization_attempts_total{result}
```

### Canonical alerts

```yaml
# APIServerErrorBudgetBurn — Google SRE multiwindow multi-burn-rate
- alert: APIServerErrorBudgetBurn
  expr: |
    (
      sum(rate(apiserver_request_total{code=~"5..", verb!~"WATCH|CONNECT"}[5m]))
      /
      sum(rate(apiserver_request_total{verb!~"WATCH|CONNECT"}[5m]))
    ) > (14.4 * 0.01)  # 14.4 = burn for 99% SLO over 30d
  for: 2m
  labels: {severity: critical}

# APIServerHighLatency
- alert: APIServerHighLatency
  expr: |
    histogram_quantile(0.99,
      sum by (le, verb, resource) (
        rate(apiserver_request_duration_seconds_bucket{verb!~"WATCH|CONNECT", subresource!="log"}[5m])
      )
    ) > 1
  for: 10m
  annotations:
    summary: "p99 apiserver latency for {{$labels.verb}} {{$labels.resource}} > 1s"

# APIServerAPFThrottling
- alert: APIServerAPFThrottling
  expr: |
    sum by (priority_level) (
      rate(apiserver_flowcontrol_rejected_requests_total[5m])
    ) > 0
  for: 5m

# APIServerAdmissionWebhookSlow
- alert: APIServerAdmissionWebhookSlow
  expr: |
    histogram_quantile(0.99,
      sum by (le, name) (rate(apiserver_admission_webhook_admission_duration_seconds_bucket[5m]))
    ) > 1
  for: 10m
```

### Quantile vs histogram pitfall

The `apiserver_request_duration_seconds` series is a **histogram**, not a summary. You query it with `histogram_quantile`, not by reading a `_sum` / `_count` ratio. Many production rules erroneously do:

```promql
# WRONG — this is the mean, not a quantile
sum(rate(apiserver_request_duration_seconds_sum[5m]))
/ sum(rate(apiserver_request_duration_seconds_count[5m]))
```

That's the *average*, which hides long tails. The correct quantile:

```promql
# RIGHT
histogram_quantile(0.99,
  sum by (le) (rate(apiserver_request_duration_seconds_bucket[5m]))
)
```

Always aggregate by `le` (and any other dimensions you care about) **before** calling `histogram_quantile`; the function operates per-bucket.

---

## 10. Control-Plane Metrics: scheduler

The scheduler exposes `/metrics` on its secure port (default 10259). Its metrics are the only window into "why are pods pending?" beyond the apiserver's event stream.

```promql
# Scheduling attempt counters
scheduler_scheduling_attempts{result="scheduled"|"unschedulable"|"error"}
  # If 'unschedulable' is climbing, you have insufficient capacity, taints,
  # or affinity constraints that nobody satisfies.

# Pending pods by queue
scheduler_pending_pods{queue="active"|"backoff"|"unschedulable"|"gated"}
  # 'unschedulable' is the most important — these pods have been rejected
  # at least once and are waiting for a cluster change.

# End-to-end scheduling latency (histogram)
scheduler_pod_scheduling_duration_seconds_bucket{attempts}
  # 'attempts' label is the cumulative number of scheduling attempts before
  # this pod was finally scheduled. attempts="1+" is normal, "16+" is a sign
  # of a scheduling fight (anti-affinity vs spread, for example).

# Per-extension-point latency
scheduler_framework_extension_point_duration_seconds_bucket{extension_point, profile, plugin}
  # Where time is spent: Filter, Score, Reserve, PreBind, Bind, ...

# Preemption
scheduler_preemption_attempts_total
scheduler_preemption_victims  # Histogram of victim count per preemption

# Volume binding
scheduler_volume_scheduling_duration_seconds_bucket{operation}

# Goroutine and queue depth
scheduler_pending_pods{queue}
scheduler_scheduler_cache_size{type="nodes"|"pods"|"assumed_pods"}
```

### Canonical alerts

```yaml
- alert: SchedulerPendingPodsHigh
  expr: scheduler_pending_pods{queue="unschedulable"} > 50
  for: 15m
  annotations:
    summary: "{{$value}} pods unschedulable for 15+ minutes"

- alert: SchedulerSlow
  expr: |
    histogram_quantile(0.99,
      sum by (le) (rate(scheduler_pod_scheduling_duration_seconds_bucket[5m]))
    ) > 5
  for: 10m
```

The scheduler's most useful debugging signal is `scheduler_framework_extension_point_duration_seconds_bucket` broken down by plugin. If `NodeResourcesFit` is fast but `InterPodAffinity` is slow, you have a topology-spread fight or a quadratic anti-affinity rule.

---

## 11. Control-Plane Metrics: controller-manager

The kube-controller-manager exposes `/metrics` on 10257. Its metrics are dominated by **workqueue** metrics — every built-in controller (Deployment, ReplicaSet, EndpointSlice, GarbageCollector, Job, Node, ServiceAccount, …) runs a workqueue, and each emits the same metric family per workqueue.

```promql
# Workqueue depth (current items waiting)
workqueue_depth{name}
  # name = "deployment", "replicaset", "endpoint_slice", "garbage_collector_attempt_to_delete", ...

# Workqueue add rate
workqueue_adds_total{name}

# Time items spend in the queue before being processed
workqueue_queue_duration_seconds_bucket{name}

# Time to actually do the work after pickup
workqueue_work_duration_seconds_bucket{name}

# Retries (item was requeued with rate limit)
workqueue_retries_total{name}

# Items that have been continuously requeued for too long
workqueue_unfinished_work_seconds{name}

# Leader election
leader_election_master_status{name}  # 1 if this replica is leader, 0 otherwise

# Garbage collector
garbage_collector_attempt_to_delete_queue_latency  # legacy name; see workqueue_*

# Node controller
node_collector_zone_size{zone}            # nodes per failure-zone
node_collector_unhealthy_nodes_in_zone{zone}  # unhealthy node count
```

### Canonical alerts

```yaml
- alert: ControllerManagerWorkqueueBackedUp
  expr: workqueue_depth > 100
  for: 10m
  annotations:
    summary: "Workqueue {{$labels.name}} in kube-controller-manager has {{$value}} pending items"

- alert: ControllerManagerNoLeader
  expr: max by (name) (leader_election_master_status) == 0
  for: 5m
  annotations:
    summary: "{{$labels.name}} has no leader (split-brain or no replica running)"
```

Workqueue metrics are *the* signal for controller health. If `workqueue_depth{name="deployment"}` is growing unbounded, the deployment controller is overwhelmed — usually because the apiserver is slow, etcd is slow, or someone created a million ReplicaSets.

---

## 12. Control-Plane Metrics: etcd

etcd is the most critical component to monitor and the most commonly *unmonitored* in homegrown clusters. Every etcd member exposes `/metrics` on its client port (default 2379). On managed K8s (EKS, GKE, AKS), the etcd metrics are not user-accessible — you depend on the provider's dashboards.

### The metrics that matter

```promql
# DISK — the dominant performance signal
etcd_disk_wal_fsync_duration_seconds_bucket
  # WAL fsync = every committed write blocks here.
  # p99 > 25 ms is the canonical "etcd is unhealthy" threshold (SIG-scalability).

etcd_disk_backend_commit_duration_seconds_bucket
  # bbolt commit = persisting a transaction to disk.
  # p99 > 25 ms = ditto, plus you're at risk of leader churn.

# NETWORK
etcd_network_peer_round_trip_time_seconds_bucket
  # Inter-member RTT. >50ms p99 = the cluster is geographically split too far;
  # leader elections will happen frequently.

etcd_network_peer_sent_failures_total
etcd_network_peer_received_failures_total

# CONSENSUS / RAFT
etcd_server_leader_changes_seen_total
  # rate > 0 sustained = leadership is unstable (almost always disk or network).

etcd_server_proposals_committed_total
etcd_server_proposals_pending
etcd_server_proposals_failed_total
etcd_server_proposals_applied_total

# STORAGE SIZE
etcd_mvcc_db_total_size_in_bytes
etcd_mvcc_db_total_size_in_use_in_bytes
  # The gap between these = bytes reclaimable by defrag. > 50% gap = run defrag.

etcd_server_quota_backend_bytes
  # The configured quota. Default 2 GiB; 8 GiB for big clusters.

# WATCHERS
etcd_debugging_mvcc_watcher_total
  # Per-cluster watch count. Each apiserver replica + every controller is a watcher.

# COMPACTION
etcd_debugging_mvcc_db_compaction_pause_duration_milliseconds_bucket
etcd_debugging_mvcc_db_compaction_total_duration_milliseconds_bucket

# CLIENT REQUEST LATENCY (from etcd's side, not the apiserver's)
etcd_server_proposals_committed_total
grpc_server_handled_total
grpc_server_handling_seconds_bucket
```

### Canonical alerts

```yaml
- alert: EtcdHighFsyncLatency
  expr: |
    histogram_quantile(0.99,
      rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m])
    ) > 0.025
  for: 10m
  annotations:
    summary: "etcd WAL fsync p99 > 25 ms — disk is too slow or contended"
    runbook: "Move etcd to local NVMe, separate from container storage"

- alert: EtcdHighBackendCommitLatency
  expr: |
    histogram_quantile(0.99,
      rate(etcd_disk_backend_commit_duration_seconds_bucket[5m])
    ) > 0.025
  for: 10m

- alert: EtcdMemberDown
  expr: max(up{job="etcd"}) by (instance) == 0
  for: 1m

- alert: EtcdHighLeaderChanges
  expr: rate(etcd_server_leader_changes_seen_total[10m]) > 0.5
  for: 10m

- alert: EtcdDBSizeApproachingQuota
  expr: etcd_mvcc_db_total_size_in_bytes / etcd_server_quota_backend_bytes > 0.8
  for: 30m
  annotations:
    summary: "etcd DB at {{$value | humanizePercentage}} of quota — defrag soon"
```

If you scrape one component in a cluster, scrape etcd. Most cluster outages start as etcd outages, and the warning signs (fsync slowdown, leader churn) are visible 30–60 minutes before the apiserver starts failing.

---

## 13. Data-Plane Metrics: kubelet and kube-proxy

We covered the kubelet runtime metrics in §4. kube-proxy exposes its own `/metrics` on port 10249.

### kube-proxy metrics

```promql
# Sync latency (iptables / IPVS / nftables rule application)
kubeproxy_sync_proxy_rules_duration_seconds_bucket
  # p99 > 30s = rule reconciliation is lagging; new Services / Endpoints are stale.

# Network programming latency (time from EndpointSlice update to rule applied)
kubeproxy_network_programming_duration_seconds_bucket
  # This is the user-visible "how long until my new pod is in service?" metric.

# Sync count
kubeproxy_sync_proxy_rules_total

# Errors
kubeproxy_sync_proxy_rules_iptables_restore_failures_total
```

In a 5000-Service cluster, `kubeproxy_sync_proxy_rules_duration_seconds` is one of the most important signals — iptables-mode kube-proxy has O(N) reconcile time, and at large N the p99 can exceed minutes. IPVS, nftables (Kubernetes 1.31+), and eBPF (Cilium kube-proxy replacement) all reduce this dramatically. See [ch 14 (services and kube-proxy)](14-services-and-kube-proxy.md).

---

## 14. The Prometheus Ecosystem

Prometheus is the de facto metrics store for Kubernetes. The ecosystem has four core pieces:

```
       ┌────────────────────────────────────────────────────────────────┐
       │  PROMETHEUS ECOSYSTEM                                          │
       └────────────────────────────────────────────────────────────────┘

       ┌──────────┐    ┌────────────┐    ┌─────────────┐    ┌──────────┐
       │ Targets  │    │ Prometheus │    │  Storage    │    │  Query   │
       │ (apps,   │───▶│  server    │───▶│  (TSDB on   │───▶│ (PromQL, │
       │  kubelet,│    │  scrape +  │    │   local FS  │    │  HTTP    │
       │  KSM,    │    │  rule eval │    │   or remote │    │  API)    │
       │  etcd, …)│    │            │    │   write)    │    │          │
       └──────────┘    └─────┬──────┘    └─────────────┘    └────┬─────┘
                             │                                    │
                             │  alerts                            │ Grafana
                             ▼                                    ▼ dashboards
                       ┌──────────────┐                     ┌──────────┐
                       │ Alertmanager │                     │ Grafana  │
                       │ (dedup,      │────▶ PagerDuty,    │          │
                       │  routing,    │      Slack,        └──────────┘
                       │  silencing)  │      OpsGenie, …
                       └──────────────┘
```

- **Prometheus server**: pulls metrics via HTTP, stores them in a custom append-only TSDB, evaluates alert and recording rules, serves PromQL queries.
- **Alertmanager**: receives raw alert state from Prometheus, deduplicates across replicas, groups by labels, applies inhibition rules, silences, and delivers to PagerDuty / Slack / OpsGenie / generic webhooks.
- **Pushgateway**: an optional intermediate target for jobs that don't live long enough to be scraped (cron jobs, batch shutdown notifications). Discouraged for anything else.
- **Exporters**: stand-alone processes that translate non-Prometheus data sources into Prometheus exposition format. node-exporter, mysqld-exporter, blackbox-exporter (probe HTTP/TCP/ICMP), windows-exporter, etc.

### The TSDB

Prometheus's TSDB is a write-once-per-scrape, gorilla-compressed time-series store. Each series is identified by the set of `{metric_name, label_key=label_value, ...}` — a unique combination of labels creates a new series. The fundamental capacity constraint is **active series count**: roughly 10 million on a single Prometheus instance with modern hardware before query and ingestion latency become problematic. See §21 for scaling.

A point on disk is ~1–2 bytes after compression (gorilla XOR encoding does great work on slowly-changing values). A million series at a 15-second scrape interval is ~4 GB/day after compression — manageable. But the *labels* are stored in a separate inverted index, and that's what blows up. See §33 on cardinality.

---

## 15. prometheus-operator and the kube-prometheus-stack

`prometheus-operator` ([prometheus-operator/prometheus-operator](https://github.com/prometheus-operator/prometheus-operator)) turns Prometheus, Alertmanager, and the various exporters into Kubernetes-native resources. Instead of writing a `prometheus.yml` config file and managing it through ConfigMaps, you write **ServiceMonitor**, **PodMonitor**, **Probe**, **AlertmanagerConfig**, **PrometheusRule**, and **Prometheus** custom resources. The operator watches them and generates the equivalent Prometheus config, mounting it into the Prometheus pods.

```yaml
apiVersion: monitoring.coreos.com/v1
kind: Prometheus
metadata:
  name: main
  namespace: monitoring
spec:
  replicas: 2
  retention: 15d
  retentionSize: 200GB
  storage:
    volumeClaimTemplate:
      spec:
        accessModes: [ReadWriteOnce]
        storageClassName: gp3
        resources: {requests: {storage: 250Gi}}
  resources:
    requests: {cpu: 1, memory: 4Gi}
    limits:   {memory: 8Gi}
  serviceMonitorSelector: {}        # watch all SMs cluster-wide
  podMonitorSelector: {}
  probeSelector: {}
  ruleSelector: {}
  serviceAccountName: prometheus
  externalLabels:
    cluster: prod-us-east-1
    replica: $(POD_NAME)
  # Thanos sidecar for long-term storage
  thanos:
    image: quay.io/thanos/thanos:v0.36.0
    objectStorageConfig:
      key: thanos.yaml
      name: thanos-objstore
```

### kube-prometheus and kube-prometheus-stack

There are two related Helm-distributed bundles:

1. **kube-prometheus** ([prometheus-operator/kube-prometheus](https://github.com/prometheus-operator/kube-prometheus)) — the jsonnet-based reference install. Defines Prometheus, Alertmanager, Grafana, node-exporter, kube-state-metrics, blackbox-exporter, and a comprehensive set of dashboards and alert rules curated by the prometheus-operator maintainers. Distributed as jsonnet, with a generated YAML manifest.
2. **kube-prometheus-stack** — the Helm chart from `prometheus-community/helm-charts`. Same content as kube-prometheus, packaged as Helm. The most common production install.

The stack ships with:

- Prometheus (replicated, optionally with Thanos sidecar)
- Alertmanager (replicated)
- Grafana (preloaded with dashboards: K8s API Server, Controller Manager, Scheduler, kubelet, etcd, node-exporter, K8s cluster overview)
- node-exporter DaemonSet
- kube-state-metrics Deployment
- A curated set of `PrometheusRule` resources implementing the SIG-instrumentation alerts (`KubeAPIServerLatency`, `KubeletDown`, `KubeStateMetricsListErrors`, …)
- ServiceMonitors for every control-plane component (when accessible)

For 95% of clusters, install kube-prometheus-stack as your starting point and customize from there.

---

## 16. ServiceMonitor, PodMonitor, Probe, PrometheusRule

These are the daily-driver CRDs once prometheus-operator is installed.

### ServiceMonitor

A ServiceMonitor selects Kubernetes Services by label and scrapes their backing Endpoints (i.e., the pods behind the Service).

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: my-app
  namespace: monitoring
  labels: {release: kube-prometheus-stack}  # so Prometheus picks it up
spec:
  selector:
    matchLabels: {app: my-app}
  namespaceSelector:
    matchNames: [default, staging, prod]
  endpoints:
    - port: metrics                  # named port on the Service
      path: /metrics
      interval: 30s
      scrapeTimeout: 10s
      scheme: https
      tlsConfig:
        caFile:   /etc/prometheus/secrets/my-app-ca/ca.crt
        certFile: /etc/prometheus/secrets/my-app-cert/tls.crt
        keyFile:  /etc/prometheus/secrets/my-app-cert/tls.key
      relabelings:
        # Add a "cluster" label
        - targetLabel: cluster
          replacement: prod-us-east-1
      metricRelabelings:
        # Drop a high-cardinality metric
        - action: drop
          sourceLabels: [__name__]
          regex: my_app_request_id_total
```

The operator translates this into a Prometheus `kubernetes_sd_configs` scrape with a `role: endpoints` selector. Each Service's backing pods become scrape targets, one per (pod, port) tuple.

### PodMonitor

Use a PodMonitor when there's no Service (e.g., a job pod) or when you want to scrape pods directly without going through the Service abstraction.

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: training-jobs
  namespace: monitoring
  labels: {release: kube-prometheus-stack}
spec:
  selector:
    matchLabels: {job-type: training}
  namespaceSelector:
    matchNames: [ml-platform]
  podMetricsEndpoints:
    - port: metrics
      interval: 15s
```

### Probe

A Probe runs the **blackbox-exporter** against a list of URLs or hosts, useful for synthetic monitoring.

```yaml
apiVersion: monitoring.coreos.com/v1
kind: Probe
metadata:
  name: external-deps
  namespace: monitoring
spec:
  jobName: external-deps
  prober:
    url: blackbox-exporter:9115
  module: http_2xx
  targets:
    staticConfig:
      static:
        - https://api.stripe.com/v1
        - https://oauth2.googleapis.com/token
        - https://kubernetes.default.svc/healthz
      labels:
        env: prod
```

### PrometheusRule

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: my-app
  namespace: monitoring
  labels: {release: kube-prometheus-stack}
spec:
  groups:
    - name: my-app.rules
      interval: 30s
      rules:
        # Recording rule — precompute expensive query
        - record: my_app:request_rate:1m
          expr: |
            sum by (route, code) (
              rate(my_app_http_requests_total[1m])
            )

        # Alert rule — fires when the condition holds for 'for' duration
        - alert: MyAppHighErrorRate
          expr: |
            (
              sum(rate(my_app_http_requests_total{code=~"5.."}[5m]))
              /
              sum(rate(my_app_http_requests_total[5m]))
            ) > 0.01
          for: 10m
          labels:
            severity: page
            team: platform
          annotations:
            summary: "my-app error rate > 1% for 10 minutes"
            description: |
              5xx ratio is {{$value | humanizePercentage}} for the last 5m.
              Dashboard: https://grafana/d/my-app
              Runbook: https://wiki/runbooks/my-app-errors
```

### AlertmanagerConfig

```yaml
apiVersion: monitoring.coreos.com/v1alpha1
kind: AlertmanagerConfig
metadata:
  name: platform-team
  namespace: monitoring
spec:
  route:
    receiver: platform-default
    groupBy: [alertname, cluster]
    routes:
      - matchers: [{name: severity, value: page}]
        receiver: pagerduty-platform
      - matchers: [{name: severity, value: ticket}]
        receiver: jira-platform
  receivers:
    - name: platform-default
      slackConfigs:
        - apiURL: {key: url, name: slack-webhook}
          channel: "#platform-alerts"
    - name: pagerduty-platform
      pagerdutyConfigs:
        - routingKey: {key: key, name: pagerduty-platform-routing-key}
    - name: jira-platform
      webhookConfigs:
        - url: https://alertmanager-jira-bot/notify
```

---

## 17. Service Discovery for Scrapes

Prometheus needs to know what to scrape. The Kubernetes service discovery (`kubernetes_sd_configs`) talks to the apiserver and enumerates targets dynamically. There are six roles:

| `role` | Yields one target per | Common use |
|---|---|---|
| `pod` | Each container port on each pod | PodMonitor; direct pod scrape |
| `service` | Each named port on each Service | rare — usually you want endpoints |
| `endpoints` | Each (port, pod) pair behind a Service | ServiceMonitor default |
| `endpointslice` | Each (port, address) in each EndpointSlice | ServiceMonitor in newer setups; scales better than endpoints |
| `ingress` | Each Ingress rule | blackbox monitoring of Ingress URLs |
| `node` | Each Node | kubelet, node-exporter |

The operator-generated config typically looks like this (for a ServiceMonitor):

```yaml
- job_name: serviceMonitor/monitoring/my-app/0
  kubernetes_sd_configs:
    - role: endpointslice
      namespaces:
        names: [default, staging, prod]
  scheme: https
  tls_config:
    ca_file: /etc/prometheus/secrets/my-app-ca/ca.crt
  relabel_configs:
    # Keep only endpoints from Services matching the ServiceMonitor selector
    - source_labels: [__meta_kubernetes_service_label_app]
      regex: my-app
      action: keep
    - source_labels: [__meta_kubernetes_endpointslice_port_name]
      regex: metrics
      action: keep
    # Populate target labels
    - source_labels: [__meta_kubernetes_namespace]
      target_label: namespace
    - source_labels: [__meta_kubernetes_pod_name]
      target_label: pod
    - source_labels: [__meta_kubernetes_service_name]
      target_label: service
```

The `__meta_*` labels are *discovery-time* metadata; you select on them in `relabel_configs` and either keep, drop, or rename them. After relabeling, the labels become permanent on every series scraped from that target — which is also why a careless relabel can blow up cardinality (see §33).

### EndpointSlice over Endpoints

Newer prometheus-operator (>=0.61) defaults to `role: endpointslice` instead of `role: endpoints`. The reason: a Service with 1000 endpoints produces one Endpoints object with 1000 entries (each watch update carries the full list), but with EndpointSlice the list is sharded across many EndpointSlice objects (each with ~100 entries), so a single endpoint change only sends one small slice update. At cluster scale (thousands of Services × hundreds of endpoints), this is the difference between Prometheus discovery working and not.

---

## 18. Push vs Pull, and Where the Pushgateway Belongs

Prometheus is pull-based. Every Prometheus user has at some point asked "but my batch job is dead in 5 seconds, how do I scrape it?" The answer is the **Pushgateway** — an intermediate process that accepts pushed metrics over HTTP and exposes them on `/metrics` for Prometheus to scrape.

```
   ┌────────────────────┐
   │  Cron job runs     │
   │  computes value    │
   │  POSTs to PGW      │
   │  exits             │
   └─────────┬──────────┘
             │
             ▼
   ┌────────────────────┐
   │  Pushgateway       │
   │  stores last value │   ◀── scraped by Prometheus every 30s
   └────────────────────┘
```

The Pushgateway is **only for batch jobs that don't live long enough to be scraped**. It is explicitly not for:

- Service metrics (those should be scraped directly).
- Per-instance metrics (the Pushgateway by design deduplicates pushed series by labels — bad for high-churn data).
- Long-running batch (just expose `/metrics` and have Prometheus scrape it).

The Pushgateway holds metrics forever unless you delete them, which means **dead jobs continue to expose stale metrics**. You either explicitly DELETE the metric at the end of the job, or you scope each push with a `pushgateway_TTL` annotation (newer versions support TTL).

OpenTelemetry, in contrast, is push-based by design. Most observability platforms (Datadog, New Relic, Honeycomb) also use push. The trade-off:

- **Pull** (Prometheus): the scraper knows the schedule, can apply rate limits, naturally detects "target gone." But discovery is required, and firewall traversal can be painful.
- **Push** (OTLP): clients send when they have data; works through firewalls; natural fit for ephemeral workloads. But requires server-side rate-limiting and back-pressure mechanisms.

In practice, most production clusters run both: Prometheus pulls long-lived services, OpenTelemetry pushes traces and ephemeral metrics, and the two converge in Grafana for visualization.

---

## 19. Recording Rules and Alert Rules

A **recording rule** precomputes a PromQL expression and stores the result as a new metric. A **alert rule** fires when a condition has been true for a stated duration.

### Recording rules

Use them when:
- A query is too expensive to run on every dashboard refresh (e.g., a 30-day percentile).
- Multiple alerts share the same subexpression.
- You want to publish stable, queryable derived metrics to other teams.

```yaml
- record: node_namespace_pod_container:container_cpu_usage_seconds_total:sum_irate
  expr: |
    sum by (cluster, namespace, pod, container) (
      irate(container_cpu_usage_seconds_total{job="kubelet", metrics_path="/metrics/cadvisor", image!=""}[5m])
    ) * on (cluster, namespace, pod) group_left (node)
    topk by (cluster, namespace, pod) (1,
      max by (cluster, namespace, pod, node) (kube_pod_info{node!=""})
    )

- record: cluster:apiserver_request_duration_seconds:99th_quantile_by_verb
  expr: |
    histogram_quantile(0.99,
      sum by (le, verb, cluster) (
        rate(apiserver_request_duration_seconds_bucket{verb!~"WATCH|CONNECT"}[5m])
      )
    )
```

The naming convention is `level1_level2_level3:metric_name:operation` where the prefix lists the labels you're aggregating *down to* — `node_namespace_pod_container` means the result has those four labels, plus the metric's own. This is a strong convention; follow it.

### Alert rules

```yaml
# A "ratio with a for-duration" alert — the canonical correct pattern
- alert: APIServerHighErrorRatio
  expr: |
    (
      sum(rate(apiserver_request_total{code=~"5..", verb!~"WATCH|CONNECT"}[5m]))
      /
      sum(rate(apiserver_request_total{verb!~"WATCH|CONNECT"}[5m]))
    ) > 0.05
  for: 5m
  labels: {severity: critical}
  annotations:
    summary: "apiserver 5xx error ratio > 5% for 5 minutes"
    description: "{{$value | humanizePercentage}} of apiserver requests returning 5xx"
    dashboard: "https://grafana/d/k8s-apiserver"
    runbook:   "https://wiki/runbooks/apiserver-errors"
```

Three principles:

1. **Alert on ratios, not absolutes.** "5xx > 100/sec" is wrong when your cluster traffic varies 1000×; "5xx ratio > 1%" is right.
2. **Always use `for:`.** Without it, single-evaluation glitches page you.
3. **Always include a runbook link.** The 3am on-call is a different person from the alert author.

The "four golden signals" pattern (next section) is the framework for choosing what to alert on.

---

## 20. The Four Golden Signals per Component

Google's SRE book defines four golden signals: **latency, traffic, errors, saturation**. For each Kubernetes control-plane component, here are the canonical metrics for each:

| Component | Latency | Traffic | Errors | Saturation |
|---|---|---|---|---|
| **apiserver** | `apiserver_request_duration_seconds` p99 | `rate(apiserver_request_total[5m])` | 5xx rate, `apiserver_admission_webhook_rejection_count`, `authentication_attempts{result="failure"}` | `apiserver_current_inflight_requests`, APF queue depths |
| **etcd** | `etcd_disk_wal_fsync_duration_seconds` p99, `etcd_disk_backend_commit_duration_seconds` p99 | `rate(etcd_server_proposals_committed_total[5m])` | `etcd_server_proposals_failed_total`, peer connection failures | `etcd_mvcc_db_total_size_in_bytes / quota`, pending proposals |
| **scheduler** | `scheduler_pod_scheduling_duration_seconds` p99 | `rate(scheduler_scheduling_attempts[5m])` | `scheduler_scheduling_attempts{result="error"}` | `scheduler_pending_pods{queue="unschedulable"}` |
| **controller-manager** | `workqueue_queue_duration_seconds` p99 per controller | `workqueue_adds_total` per controller | per-controller error counters | `workqueue_depth`, `workqueue_unfinished_work_seconds` |
| **kubelet** | `kubelet_pod_start_duration_seconds` p99 | `rate(kubelet_runtime_operations_total[5m])` | `kubelet_runtime_operations_errors_total`, prober failure rate | `kubelet_pleg_relist_duration_seconds` p99, eviction rate |
| **kube-proxy** | `kubeproxy_network_programming_duration_seconds` p99 | `rate(kubeproxy_sync_proxy_rules_total[5m])` | `kubeproxy_sync_proxy_rules_iptables_restore_failures_total` | `kubeproxy_sync_proxy_rules_duration_seconds` p99 (high = behind) |
| **CoreDNS** | `coredns_dns_request_duration_seconds` p99 | `rate(coredns_dns_requests_total[5m])` | `rate(coredns_dns_responses_total{rcode!="NOERROR"}[5m])` | concurrent in-flight queries |

Build one Grafana row per component, four panels per row (one per signal). That is your "is the cluster healthy?" dashboard.

---

## 21. Scaling Prometheus: Sharding, Thanos, Mimir, VictoriaMetrics

A single Prometheus instance ingests reliably up to ~10 million active series on modern hardware (~50 GB RAM, NVMe, 16 cores). Beyond that, you hit one of three walls: ingestion CPU, query memory, or local disk I/O. The solutions split along two axes: how to handle *more series* and how to handle *long retention*.

### Single-instance HA: the replica pattern

The simplest "HA" is two identical Prometheus replicas with the same scrape configs. They scrape every target twice (slight offset), each stores independently, and they emit alerts independently. Alertmanager deduplicates alerts. This handles single-replica crashes but not "more data than fits in one replica."

Important: `kube-prometheus-stack` defaults to 2 replicas with identical configs. The `replica` external label is automatically added by the operator. Queries against either replica return the same answer (within a scrape interval).

### Functional sharding

Split scrape targets across multiple Prometheus instances by *function*: one Prometheus for the apiserver tier, one for the node tier, one for each application team. Each instance is independently sized, independently HA. Queries that span multiple shards require federation or a global query layer.

### Hashmod sharding

For homogeneous targets (e.g., 10000 pods of the same app), use Prometheus's built-in `hashmod` relabel to split:

```yaml
relabel_configs:
  - source_labels: [__address__]
    modulus: 4
    target_label: __tmp_hash
    action: hashmod
  - source_labels: [__tmp_hash]
    regex: ^0$           # replica 0 keeps hash==0; replica 1 keeps ^1$; etc.
    action: keep
```

### Thanos

[Thanos](https://github.com/thanos-io/thanos) is the most-deployed long-term-storage solution. Architecture:

```
       ┌──────────────────────────────────────────────────────────────┐
       │  Per-cluster:                                                │
       │                                                              │
       │   ┌────────────────────────────────────────────────────┐    │
       │   │  Prometheus replica 0 ──┐                           │    │
       │   │  + thanos-sidecar ──────┼──> uploads 2h blocks       │    │
       │   │                          │    to object store (S3,    │    │
       │   │  Prometheus replica 1 ──┤    GCS, Azure Blob, ...)    │    │
       │   │  + thanos-sidecar ──────┘                            │    │
       │   └────────────────────────────────────────────────────┘    │
       │                                                              │
       │   thanos-sidecar also serves a StoreAPI for the last 2h     │
       │   of recent data (queried directly from Prometheus's TSDB)  │
       │                                                              │
       └──────────────────────────────────────────────────────────────┘
                          │                              │
                          ▼                              ▼
                   ┌──────────────┐             ┌──────────────────┐
                   │ Object store │             │  Across clusters:│
                   │ (S3 / GCS /  │             │  Thanos Querier  │
                   │  Blob)       │             │  fans out queries│
                   │              │             │  to every Store  │
                   │ 2h blocks    │             │  (sidecar + Store│
                   │ + indexes    │◀────────────│  Gateway).       │
                   └──────────────┘             │                  │
                          ▲                     │  Optional:        │
                          │                     │  - Compactor      │
                   ┌──────┴───────┐             │    (downsamples)  │
                   │ Thanos Store │             │  - Ruler          │
                   │ Gateway      │             │    (recording+   │
                   │ (serves      │             │    alert rules)  │
                   │  historical  │             │  - Receive       │
                   │  data via    │             │    (push)        │
                   │  StoreAPI)   │             └──────────────────┘
                   └──────────────┘
```

Key properties:

- **Object store is the source of truth** for long-term data. Prometheus only holds the last 2 hours.
- **Querier fans out** in parallel and deduplicates.
- **Compactor** runs in the background, merging 2h blocks into longer (8h, 2d, 14d) blocks and producing 5m/1h **downsamples** for fast long-range queries.
- **Sidecar pattern** (the default): Thanos sidecar runs next to Prometheus, uploads completed blocks, serves recent data via StoreAPI.
- **Receive pattern** (alternative): instead of Prometheus + sidecar, you push to thanos-receive (essentially Prometheus's TSDB exposed as a write target via the Prometheus remote-write protocol). Useful when target clusters can't run a full Prometheus.

### Cortex and Mimir

[Cortex](https://github.com/cortexproject/cortex) (Prometheus-community) and its commercial fork [Grafana Mimir](https://github.com/grafana/mimir) are *push-only* horizontally scalable Prometheus-compatible stores. Workloads write via remote-write; the system shards ingestion across distributors → ingesters → object storage. They are multi-tenant from the ground up (every series has a tenant ID).

```
   Prometheus ──remote_write──▶ distributor ──hash by series──▶ ingesters
                                                                    │
                                            (each ingester holds       ▼
                                             a chunk in RAM for      object store
                                             ~12h, then flushes)     (blocks)
```

Choose Mimir when you have many small Prometheuses pushing into one global store; choose Thanos when you have a few large Prometheuses you want to keep query-side.

### VictoriaMetrics

[VictoriaMetrics](https://github.com/VictoriaMetrics/VictoriaMetrics) is a single-binary alternative. It is significantly more compact (often 5–10× less disk than Prometheus for the same data) due to a different chunk layout and aggressive compression. It supports Prometheus's remote-write protocol, has its own MetricsQL (superset of PromQL), and scales to many millions of series per instance. The cluster variant (vmstorage / vminsert / vmselect) is the horizontal option.

Tradeoff: VictoriaMetrics is excellent for raw efficiency but somewhat less idiomatic if your team has deep PromQL muscle memory; some PromQL edge cases differ from upstream Prometheus.

### Choosing

| Scale | Choice |
|---|---|
| 1–5M series, single cluster, single team | Single Prometheus + 1 replica for HA |
| 5–10M series, want long retention | Prometheus + Thanos sidecar + S3 |
| Many clusters, central query | Thanos with sidecars + remote Store Gateway in one region |
| Many teams, multi-tenant, write-heavy | Mimir |
| Extreme efficiency, dense single-binary | VictoriaMetrics |

---

## 22. OpenTelemetry: One Pipeline for Three Signals

OpenTelemetry (OTel) is the CNCF-graduated specification for instrumenting applications and shipping telemetry. Its three pieces:

1. **Specification**: data model + semantic conventions (e.g., `http.method`, `http.status_code`, `k8s.pod.name`) shared across signals.
2. **SDKs**: per-language libraries (Go, Java, Python, .NET, Node, Ruby, Rust, etc.) that produce traces, metrics, and logs and ship them via OTLP.
3. **Collector**: a vendor-neutral process that ingests, processes, and exports telemetry.

The promise: **one SDK, one wire protocol, any backend**. You instrument your code with the OTel SDK; the Collector handles routing to Prometheus, Loki, Tempo, Jaeger, Honeycomb, Datadog, or any combination.

### The Collector pipeline

```
              ┌─────────────────────────────────────────────────────────────┐
              │   OTEL COLLECTOR (one process)                              │
              └─────────────────────────────────────────────────────────────┘

   ┌──────────┐     ┌──────────────┐     ┌────────────┐     ┌──────────┐
   │RECEIVERS │────▶│ PROCESSORS   │────▶│ EXPORTERS  │────▶│ BACKENDS │
   └──────────┘     └──────────────┘     └────────────┘     └──────────┘
        │                  │                    │
        │                  │                    │
   - otlp (gRPC+HTTP)  - batch              - otlphttp/grpc
   - prometheus        - memory_limiter     - prometheusremotewrite
   - jaeger            - resource           - loki
   - zipkin            - attributes         - tempo
   - filelog           - filter             - kafka
   - k8s_events        - probabilistic_     - logging (stdout for debug)
   - hostmetrics         sampler            - file
   - kubeletstats      - tail_sampling      - opensearch
   - prometheus          (trace-wide rules) - debug
     (scrape proxy)    - k8sattributes
                         (enriches every
                          signal with
                          pod, ns, node)
```

Every Collector configuration is essentially:

```yaml
receivers:
  otlp:
    protocols: {grpc: {endpoint: 0.0.0.0:4317}, http: {endpoint: 0.0.0.0:4318}}
  prometheus:
    config:
      scrape_configs:
        - job_name: 'self'
          static_configs: [{targets: ['localhost:8888']}]

processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 1500
  batch:
    timeout: 10s
    send_batch_size: 8192
  k8sattributes:
    auth_type: serviceAccount
    passthrough: false
    extract:
      metadata: [k8s.pod.name, k8s.namespace.name, k8s.node.name, k8s.pod.uid]
      labels:
        - tag_name: app
          key: app.kubernetes.io/name
          from: pod
  resource:
    attributes:
      - key: cluster
        value: prod-us-east-1
        action: insert

exporters:
  prometheusremotewrite:
    endpoint: http://mimir:9009/api/v1/push
  loki:
    endpoint: http://loki:3100/loki/api/v1/push
  otlp/tempo:
    endpoint: tempo:4317
    tls: {insecure: true}

service:
  pipelines:
    metrics:
      receivers: [otlp, prometheus]
      processors: [memory_limiter, k8sattributes, resource, batch]
      exporters: [prometheusremotewrite]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, k8sattributes, resource, batch]
      exporters: [loki]
    traces:
      receivers: [otlp]
      processors: [memory_limiter, k8sattributes, resource, batch]
      exporters: [otlp/tempo]
```

### Deployment patterns

```
   1. AGENT MODE (DaemonSet) — one Collector per node
      ┌────────────┐
      │  Node      │
      │            │
      │  ┌──────┐  │  apps in pods on this node send to localhost:4317
      │  │ OTel │◀─┼─────────────────────────────────────
      │  │ Coll │  │                                     │
      │  └──┬───┘  │                                     │
      └─────┼──────┘
            │ batched + enriched
            ▼
       Gateway / backend

   2. SIDECAR MODE — one Collector per pod (per app)
      Useful when app and Collector should share a netns or for strong isolation.
      Higher per-pod cost; less common.

   3. GATEWAY MODE (Deployment) — central Collector that aggregates from agents
      Big batching, big throughput. Often combined with agent mode:
        apps → agent (per node) → gateway (central) → backend
```

The most common production pattern is **agent + gateway**: a per-node DaemonSet handles per-node enrichment (k8s.pod.*, host metrics), and a central gateway Deployment handles fan-out to multiple backends and tail-based sampling for traces.

---

## 23. The OpenTelemetry Operator: Collector and Instrumentation CRs

The [OpenTelemetry Operator](https://github.com/open-telemetry/opentelemetry-operator) provides two CRDs that turn the Collector into a Kubernetes-native resource.

### OpenTelemetryCollector

```yaml
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: otel-agent
  namespace: monitoring
spec:
  mode: daemonset       # or "deployment", "sidecar", "statefulset"
  image: otel/opentelemetry-collector-contrib:0.108.0
  resources:
    requests: {cpu: 100m, memory: 256Mi}
    limits:   {memory: 512Mi}
  serviceAccount: otel-agent
  env:
    - name: K8S_NODE_NAME
      valueFrom: {fieldRef: {fieldPath: spec.nodeName}}
    - name: K8S_POD_IP
      valueFrom: {fieldRef: {fieldPath: status.podIP}}
  config:
    receivers:
      otlp: {protocols: {grpc: {endpoint: 0.0.0.0:4317}, http: {endpoint: 0.0.0.0:4318}}}
      kubeletstats:
        collection_interval: 30s
        auth_type: serviceAccount
        endpoint: "${env:K8S_NODE_NAME}:10250"
        insecure_skip_verify: false
        metric_groups: [node, pod, container, volume]
      hostmetrics:
        collection_interval: 30s
        scrapers: {cpu: {}, memory: {}, disk: {}, filesystem: {}, network: {}, load: {}}
      k8s_events:
        auth_type: serviceAccount
        namespaces: []   # all
    processors:
      memory_limiter: {check_interval: 1s, limit_mib: 400}
      batch: {timeout: 10s, send_batch_size: 8192}
      k8sattributes:
        passthrough: false
        extract:
          metadata: [k8s.pod.name, k8s.namespace.name, k8s.node.name, k8s.pod.uid]
    exporters:
      otlphttp/gateway:
        endpoint: http://otel-gateway.monitoring:4318
    service:
      pipelines:
        metrics: {receivers: [otlp, kubeletstats, hostmetrics], processors: [memory_limiter, k8sattributes, batch], exporters: [otlphttp/gateway]}
        traces:  {receivers: [otlp], processors: [memory_limiter, k8sattributes, batch], exporters: [otlphttp/gateway]}
        logs:    {receivers: [otlp, k8s_events], processors: [memory_limiter, k8sattributes, batch], exporters: [otlphttp/gateway]}
```

The operator handles RBAC (creates a ClusterRole for the relevant resources), volume mounts (kubelet TLS cert), and rolling updates on config changes.

### Instrumentation: auto-inject SDKs

The killer CRD is `Instrumentation`. The operator runs a mutating admission webhook that, when a pod has the annotation `instrumentation.opentelemetry.io/inject-{language}: "true"`, injects an init container that copies the OTel SDK into the pod and modifies the main container's env vars (or LD_PRELOAD, or `-javaagent`, etc.) so the app picks up the SDK transparently.

```yaml
apiVersion: opentelemetry.io/v1alpha1
kind: Instrumentation
metadata:
  name: default
  namespace: my-app
spec:
  exporter:
    endpoint: http://otel-agent.monitoring:4318
  propagators: [tracecontext, baggage, b3]
  sampler:
    type: parentbased_traceidratio
    argument: "0.1"   # 10% sampling
  java:
    image: ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-java:latest
  nodejs:
    image: ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-nodejs:latest
  python:
    image: ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-python:latest
  dotnet:
    image: ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-dotnet:latest
  go:
    image: ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-go:latest
    # Go auto-instrumentation uses eBPF uprobes — no code changes, but
    # requires CAP_SYS_PTRACE and is more experimental than the SDK approach.
```

To opt in a workload:

```yaml
spec:
  template:
    metadata:
      annotations:
        instrumentation.opentelemetry.io/inject-java: "true"
```

The injected SDK auto-instruments the language's standard HTTP/gRPC libraries, common database drivers, popular frameworks (Spring Boot, Express, Flask, ASP.NET), and emits OTLP spans to the configured endpoint. For Java and .NET this is essentially free — no source change required. For Go and Rust the auto-instrumentation story is weaker, and most teams add SDK calls manually.

### Why OTel won

Before OTel there were OpenTracing, OpenCensus, vendor-specific agents (Datadog, Dynatrace, New Relic), and Jaeger-/Zipkin-native clients. Every backend required its own SDK. OTel unified them all behind a single API + SDK + protocol. The result: vendors compete on backend quality, not on lock-in via instrumentation. As of 2024, OTel is the second-most-active CNCF project after Kubernetes itself, and every major observability vendor has shifted to ingesting OTLP natively.

---

## 24. OTLP: gRPC and HTTP

OTLP (OpenTelemetry Protocol) is the wire protocol. It is defined in `.proto` files in the [opentelemetry-proto](https://github.com/open-telemetry/opentelemetry-proto) repo. There are two transports:

- **OTLP/gRPC** (port 4317): binary, protobuf, bidirectional streaming-capable, the default for collector-to-collector and for SDKs with native gRPC support.
- **OTLP/HTTP** (port 4318): one HTTP POST per export, body is either protobuf (`application/x-protobuf`) or JSON (`application/json`). Better firewall traversal, easier to debug with curl, and the default for many SDKs (especially browser/edge).

```bash
# Send a trace span over OTLP/HTTP with curl
curl -X POST http://otel-collector:4318/v1/traces \
  -H 'Content-Type: application/json' \
  -d '{
    "resourceSpans": [{
      "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "my-app"}}]},
      "scopeSpans": [{
        "spans": [{
          "traceId":"5b8aa5a2d2c872e8321cf37308d69df2",
          "spanId":"051581bf3cb55c13",
          "name":"checkout",
          "startTimeUnixNano":"1716459600000000000",
          "endTimeUnixNano":"1716459600100000000",
          "kind":1
        }]
      }]
    }]
  }'
```

The three top-level messages are `ExportTraceServiceRequest`, `ExportMetricsServiceRequest`, `ExportLogsServiceRequest`, all sharing a `Resource` (the entity emitting telemetry — usually a service + pod + node) and a `Scope` (the library/SDK that produced the data).

---

## 25. Logs: From stdout to a Query

The Kubernetes logging contract is the simplest of the three pillars: **applications write to stdout/stderr**. Everything else is downstream.

```
                                Log path on a node
                                ──────────────────

   ┌───────────────────────────────────────────────────────────────────────┐
   │ NODE                                                                  │
   │                                                                       │
   │   Container PID 1 (app)                                              │
   │   stdout: "2026-05-23T12:00:00.123 INFO request completed"           │
   │     │                                                                 │
   │     │ FD 1 is a pipe to containerd-shim                              │
   │     ▼                                                                 │
   │   containerd-shim                                                    │
   │     │ wraps each line in CRI log format:                             │
   │     │  {timestamp} {stream} {tag} {log}\n                            │
   │     ▼                                                                 │
   │   /var/log/pods/<namespace>_<pod>_<uid>/<container>/0.log            │
   │                                                                       │
   │   Symlinked from:                                                    │
   │   /var/log/containers/<pod>_<namespace>_<container>-<id>.log          │
   │     -> /var/log/pods/.../0.log                                       │
   │                                                                       │
   │   Rotation (kubelet, not logrotate):                                 │
   │     containerLogMaxSize:  10Mi (default)                             │
   │     containerLogMaxFiles: 5    (default)                             │
   │                                                                       │
   │   When 0.log hits maxSize, kubelet rotates: 0.log → 0.log.1 → ...    │
   │                                                                       │
   │   ┌──────────────────────────────────────┐                          │
   │   │  DaemonSet log shipper                │                          │
   │   │  (Fluent Bit / Vector / Fluentd)      │                          │
   │   │                                       │                          │
   │   │  tails /var/log/containers/*.log      │                          │
   │   │  parses CRI JSON wrapper              │                          │
   │   │  enriches with pod / namespace        │                          │
   │   │    metadata (from kubelet API or      │                          │
   │   │    apiserver watch)                   │                          │
   │   │  applies user-defined parsers         │                          │
   │   │    (e.g., parse JSON log message)     │                          │
   │   │  ships to backend over HTTP/gRPC      │                          │
   │   └────────────────┬─────────────────────┘                          │
   │                    │                                                 │
   └────────────────────┼─────────────────────────────────────────────────┘
                        │
                        ▼
            ┌──────────────────────┐
            │  Loki / Elastic /    │
            │  ClickHouse / Splunk │
            │  (log aggregator)    │
            └──────────────────────┘
```

### The CRI log format

Every line is JSON, written one-per-line:

```json
{"log":"2026-05-23T12:00:00.123 INFO request completed\n","stream":"stdout","time":"2026-05-23T12:00:00.124456789Z"}
```

(Some CRI implementations use a text format instead: `<timestamp> <stream> <tag> <log>`. The kubelet/CRI-O combo and containerd default to the text format `2026-05-23T12:00:00.124456789Z stdout F request completed`, where `F` means "full line" and `P` means "partial, continued in next line".)

### Log rotation

Critically, container log rotation is **the kubelet's job**, configured via the kubelet config file:

```yaml
# kubelet config
containerLogMaxSize: 50Mi
containerLogMaxFiles: 10
```

`logrotate` should NOT touch `/var/log/containers` or `/var/log/pods` — doing so confuses the kubelet, and the shipper will read partial lines or duplicate lines after rotation.

### Why stdout matters

The "stdout/stderr only" rule has several consequences:

1. **No log files inside the container.** If your app writes to `/var/log/app/app.log` inside the container, that file lives in the container's writable layer (an overlayfs upperdir), is invisible from the host, and is deleted with the container. Use stdout.
2. **No log rotation inside the container.** Same reason.
3. **Multi-line logs are the parser's problem.** A Java stacktrace spans many lines; each becomes a separate CRI log entry. The shipper must reassemble them via configurable multi-line parsers.
4. **High write rates are the shipper's problem.** A pod logging 10 MB/s saturates the disk where `/var/log` lives. The shipper must drop or back-pressure. Best practice: set `containerLogMaxSize` to a value that bounds total log disk usage = `nodes × pods/node × maxSize × maxFiles`.

---

## 26. Log Shippers: Fluent Bit, Fluentd, Vector

The three production-grade DaemonSet shippers:

| Shipper | Language | Memory at idle | Best for |
|---|---|---|---|
| **Fluent Bit** | C | ~10–30 MB | Low-overhead default; CNCF graduated |
| **Fluentd** | Ruby + C plugins | ~100–300 MB | Rich plugin ecosystem; legacy installs |
| **Vector** | Rust | ~30–80 MB | Modern, expressive transform language (VRL), excellent observability of itself |

### Fluent Bit DaemonSet sketch

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata: {name: fluent-bit, namespace: logging}
spec:
  selector: {matchLabels: {app: fluent-bit}}
  template:
    spec:
      serviceAccountName: fluent-bit
      tolerations: [{operator: Exists}]
      hostNetwork: false
      containers:
        - name: fluent-bit
          image: cr.fluentbit.io/fluent/fluent-bit:3.0
          resources: {requests: {cpu: 50m, memory: 100Mi}, limits: {memory: 200Mi}}
          volumeMounts:
            - {name: varlog, mountPath: /var/log, readOnly: true}
            - {name: dockercontainers, mountPath: /var/lib/docker/containers, readOnly: true}
            - {name: config, mountPath: /fluent-bit/etc}
      volumes:
        - {name: varlog, hostPath: {path: /var/log}}
        - {name: dockercontainers, hostPath: {path: /var/lib/docker/containers}}
        - {name: config, configMap: {name: fluent-bit-config}}
```

```ini
# fluent-bit.conf (in the ConfigMap)
[SERVICE]
    Flush     1
    Daemon    Off
    Log_Level info
    HTTP_Server  On
    HTTP_Port    2020      # exposes Fluent Bit's own /metrics for Prometheus

[INPUT]
    Name              tail
    Path              /var/log/containers/*.log
    Parser            cri
    Tag               kube.*
    Refresh_Interval  10
    Mem_Buf_Limit     50MB
    Skip_Long_Lines   On

[FILTER]
    Name                kubernetes
    Match               kube.*
    Kube_URL            https://kubernetes.default.svc:443
    Kube_CA_File        /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
    Kube_Token_File     /var/run/secrets/kubernetes.io/serviceaccount/token
    Merge_Log           On
    K8S-Logging.Parser  On
    K8S-Logging.Exclude On

[OUTPUT]
    Name             loki
    Match            kube.*
    Host             loki.logging.svc.cluster.local
    Port             3100
    Labels           job=fluent-bit,namespace=$kubernetes['namespace_name'],pod=$kubernetes['pod_name'],container=$kubernetes['container_name']
    Auto_Kubernetes_Labels  Off
```

The `kubernetes` filter is the workhorse: it calls the kubelet (or apiserver) to enrich every log line with pod, namespace, container, image, labels, and annotations. Without it, your log entries have only the filename's information.

### Vector

Vector ([vectordotdev/vector](https://github.com/vectordotdev/vector)) uses a directed-graph configuration in TOML/YAML where each node is a source, transform, or sink, and the transform language (VRL) is purpose-built for structured data manipulation. Vector is also a metrics+logs+traces pipeline, so it can replace some uses of the OTel Collector.

```yaml
sources:
  kubernetes:
    type: kubernetes_logs
    auto_partial_merge: true

transforms:
  parse_json:
    type: remap
    inputs: [kubernetes]
    source: |
      .parsed = parse_json(.message) ?? null
      if exists(.parsed.level) { .level = .parsed.level }

  drop_health:
    type: filter
    inputs: [parse_json]
    condition: '!(string!(.kubernetes.pod_name) starts_with "healthz")'

sinks:
  loki:
    type: loki
    inputs: [drop_health]
    endpoint: http://loki:3100
    labels:
      namespace: "{{ kubernetes.namespace_name }}"
      app: "{{ kubernetes.labels.\"app.kubernetes.io/name\" }}"
      level: "{{ level }}"
    encoding: {codec: json}
```

### Picking

For new installs in 2026, the dominant choices are:

- **Fluent Bit** if you want the smallest footprint and the most-deployed option.
- **Vector** if you want richer transformations or unified logs+metrics+traces shipping.
- **Fluentd** only if you're inheriting an existing Fluentd install with custom plugins.

OpenTelemetry Collector's `filelog` receiver is also viable, especially if you're already running OTel for traces — fewer components.

---

## 27. Log Aggregators: Loki, Elasticsearch, ClickHouse

The backend is where the model differs sharply:

### Loki

[Loki](https://github.com/grafana/loki) takes the Prometheus approach to logs: **index labels, not content**. A log line is stored as compressed chunks; the index only knows which chunks contain logs for a given label set. Queries (LogQL) start by selecting label sets, then filter line content with `|=`, `|~`, `!=`, etc. — content filtering is a full scan of the matched chunks, but cheap because chunks are compressed and S3-backed.

```logql
# All error logs in the prod namespace for the past hour
{namespace="prod"} |= "ERROR"

# 5xx rate per service over the last 5m, computed from logs
sum by (app) (rate({namespace="prod"} | json | status >= 500 [5m]))
```

Loki's model:

- **Cheap ingest** (~10–50× cheaper than Elasticsearch for the same data).
- **Cheap storage** (S3 / GCS).
- **Slower full-text search** than Elasticsearch (must read chunks).
- **Same operational model as Prometheus** (labels = cardinality matters).

The architecture is microservice (Distributor → Ingester → Object Store + Index Gateway → Querier), runnable as a monolith for small installs.

### Elasticsearch

The classic full-text log store. Inverted indices over every word in every log line. Brilliant for exploratory keyword search; expensive in storage (~5–10× the raw size) and memory (the index lives mostly in RAM). Tooling: Kibana for queries, Elastic ECK operator for K8s deployment, Filebeat / Fluent Bit as shippers.

### ClickHouse and OpenObserve

Modern columnar-store-based log aggregators. ClickHouse is a general-purpose columnar database; you store logs as a wide table and query with SQL. OpenObserve is a turnkey package over a similar idea.

Tradeoffs: SQL is more familiar than LogQL/Lucene; query performance for ad-hoc analytics is excellent; full-text search needs additional indexing (the recently-added `Inverted` and `Full-text` index types in ClickHouse).

### Splunk and Datadog

Commercial. Splunk for on-prem enterprises with budget; Datadog for cloud-native teams that want logs + metrics + traces in one UI. Both have first-class Kubernetes integrations.

---

## 28. Traces: SDK to Backend

A distributed trace is a tree of **spans**. Each span has a unique `span_id`, a parent `parent_span_id`, a shared `trace_id` across the entire trace, start/end timestamps, and a set of attributes. The W3C `traceparent` header propagates the (trace_id, span_id) across HTTP calls:

```
traceparent: 00-{32 hex chars trace_id}-{16 hex chars span_id}-{flags}
```

```
                                Trace path
                                ──────────

    ┌───────────┐                                                ┌──────────┐
    │ frontend  │  HTTP /checkout                                │  backend │
    │ (Go app)  │  ────────────────────────────────────────────▶ │  (Java)  │
    │ OTel SDK  │     headers:                                   │  OTel    │
    │           │       traceparent: 00-abc-001-01               │  SDK     │
    └─────┬─────┘                                                └────┬─────┘
          │ span "POST /checkout" (parent=null, id=001)              │
          │ otel.SDK exports via OTLP/gRPC                           │
          │                                                          │
          ▼                                                          ▼
    ┌──────────────────────────────────────────────────────────────────┐
    │  OTel Collector (DaemonSet on node)                              │
    │    receives spans, batches, enriches with k8s.* attributes        │
    │    optionally tail-samples (keep all error traces, 1% of normal) │
    └──────────────────────────────────┬───────────────────────────────┘
                                       │ OTLP/gRPC
                                       ▼
                                ┌────────────────┐
                                │  Tempo / Jaeger│
                                │  Honeycomb     │
                                │  Datadog       │
                                └────────────────┘
```

### Tempo

[Tempo](https://github.com/grafana/tempo) is Grafana's trace backend, modeled on Loki — cheap object storage (S3/GCS), label-indexed by trace_id, query via TraceQL. Designed for **keep 100% of traces, query by trace_id**. The "find a trace by attribute" use case requires `traceql` (since v2.0) and a small in-memory metadata index.

Tempo's "find a slow trace" works via a sister system, the **metrics-generator** — Tempo consumes spans and produces span-derived metrics (`tempo_spanmetrics_calls_total`, `tempo_spanmetrics_latency_*`) which Prometheus scrapes. You find latency outliers via PromQL, then look up the trace by ID in Tempo.

### Jaeger

[Jaeger](https://www.jaegertracing.io/) is the older CNCF trace backend. Indexed traces — supports rich search by service name, operation, tag — but more expensive per trace stored. Often deployed in front of a backing store (Cassandra, Elasticsearch).

### Sampling

In any production trace pipeline, you sample. Common strategies:

- **Head-based sampling**: the SDK decides at trace-start time (random fraction). Cheap; simple; sometimes drops the interesting trace.
- **Tail-based sampling**: the Collector buffers all spans of a trace for a few seconds, then decides based on the full trace (keep all error traces, keep slow traces, sample the rest at 1%). Requires the Collector's `tail_sampling` processor and per-trace state.

Sampling rate is a knob: 100% is correct but expensive; 1% gives you statistical visibility; tail-based at 1% with "keep all errors and slow traces" is the production sweet spot.

---

## 29. Continuous Profiling: Parca, Pyroscope, Polar Signals

The newest pillar. **Continuous profiling** runs a low-overhead sampler against every process and stores flamegraphs over time, so you can answer "what was service X doing 6 hours ago when CPU spiked?"

Two technical approaches:

### Application-instrumented (pprof / JFR)

Every Go binary ships `/debug/pprof/{profile,heap,goroutine,block,mutex}` endpoints. Java has Java Flight Recorder (JFR). Python has py-spy. Pyroscope and Parca both support pulling these and aggregating into a database.

### eBPF-based system-wide

[Parca](https://github.com/parca-dev/parca) and [Polar Signals](https://www.polarsignals.com/) use eBPF to sample CPU stacks across the entire system without any application instrumentation. The eBPF probe walks the stack at each timer tick, captures (kernel-stack + user-stack + pid + tid), and ships to a backend. The backend resolves symbols (via DWARF debug info from binaries on the node), aggregates into flamegraphs, and stores. The agent is a per-node DaemonSet.

```
   ┌─────────────────────────────────────────────────────────────────┐
   │ NODE                                                            │
   │                                                                 │
   │   eBPF program attached to perf_event:                          │
   │     every 1/99 second, capture stack trace of running pid       │
   │                                                                 │
   │   User-space agent (parca-agent / pyroscope-eBPF):              │
   │     reads BPF map, resolves symbols via DWARF                   │
   │     groups by (pid → cgroup → pod → service)                    │
   │     ships flamegraph deltas to backend over HTTP/2              │
   └─────────────────────────────────────────────────────────────────┘
```

The output is a flamegraph per service per minute, queryable in a UI. Storage cost is low (deltas compress well); query cost is moderate; instrumentation cost is essentially zero (a few percent CPU on the node).

This is the killer app for "the service got slow at 03:17 last Tuesday and nobody knows why" — you replay the flamegraph from that minute and see exactly which function was hot.

---

## 30. Kubernetes Events as a Telemetry Signal

Kubernetes events (`core/v1/Event`) are short-lived narrative facts. The kubelet emits "Pulling image", "Started container", "FailedScheduling", "Killing", "BackOff". Controllers emit "ScalingReplicaSet", "SuccessfulCreate". The apiserver stores events in etcd, but with a default TTL of **1 hour** (configurable via `--event-ttl`).

```
$ kubectl get events --sort-by=.metadata.creationTimestamp
LAST SEEN  TYPE     REASON       OBJECT              MESSAGE
2m         Normal   Scheduled    pod/nginx-abc       Successfully assigned default/nginx-abc to node-2
2m         Normal   Pulling      pod/nginx-abc       Pulling image "nginx:1.27"
1m         Warning  Failed       pod/nginx-abc       Failed to pull image "nginx:1.27": ErrImagePull
30s        Normal   BackOff      pod/nginx-abc       Back-off pulling image "nginx:1.27"
```

For most operational debugging, the event log is more informative than the metric stream — but only if you ship it before TTL.

### Shipping events

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: event-exporter, namespace: monitoring}
spec:
  replicas: 1
  template:
    spec:
      serviceAccountName: event-exporter
      containers:
        - name: event-exporter
          image: ghcr.io/resmoio/kubernetes-event-exporter:v1.7
          args: [-conf=/data/config.yaml]
          volumeMounts: [{name: cfg, mountPath: /data}]
      volumes:
        - name: cfg
          configMap: {name: event-exporter-cfg}
```

```yaml
# event-exporter-cfg
config.yaml: |
  logLevel: info
  logFormat: json
  route:
    routes:
      - match:
          - receiver: loki
        drop:
          - reason: "Pulled"
          - reason: "Created"
      - match:
          - receiver: slack
            type: "Warning"
  receivers:
    - name: loki
      loki:
        streamLabels: {source: event-exporter}
        url: "http://loki.logging:3100/loki/api/v1/push"
    - name: slack
      slack:
        token: xoxb-...
        channel: "#k8s-alerts"
        message: "{{ .Type }} {{ .Reason }} on {{ .InvolvedObject.Kind }}/{{ .InvolvedObject.Name }}: {{ .Message }}"
```

The OpenTelemetry Collector's `k8s_events` receiver is an alternative — it ingests events into the OTLP logs pipeline.

Without an exporter, the event "ImagePullBackOff at 03:17" is gone by 04:17, and your post-mortem has no record of it.

---

## 31. Audit Logs: The Forensic Stream

Recapping from [ch 05](05-kube-apiserver-internals.md): the apiserver can be configured to emit a structured JSON record for every request, classified by `level`:

- `None` — do not log
- `Metadata` — request metadata only (who, what, when, response code)
- `Request` — adds the request body
- `RequestResponse` — adds both bodies (large!)

```yaml
# audit-policy.yaml
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages: [RequestReceived]
rules:
  # Don't log "get" of leases (huge volume)
  - level: None
    resources: [{group: coordination.k8s.io, resources: [leases]}]
    verbs: [get, list, watch]
  # Log secret access at Metadata only (don't capture the value!)
  - level: Metadata
    resources: [{group: "", resources: [secrets, configmaps]}]
  # Default: Metadata for everything
  - level: Metadata
    omitStages: [RequestReceived]
```

```yaml
# kube-apiserver flags
--audit-policy-file=/etc/kubernetes/audit-policy.yaml
--audit-log-path=/var/log/kubernetes/audit.log
--audit-log-maxage=30
--audit-log-maxbackup=10
--audit-log-maxsize=100
# OR webhook:
--audit-webhook-config-file=/etc/kubernetes/audit-webhook.yaml
```

### Shipping audit

The audit log is JSON, one event per line, written to `--audit-log-path`. The standard pattern:

1. Write to file on the control-plane node.
2. DaemonSet shipper (Fluent Bit / Vector) ships to two places:
   - **A SIEM** (Splunk, Elastic SIEM, Datadog Cloud SIEM, Falco-side) for security analytics.
   - **A queryable store** (Loki, BigQuery, ClickHouse) for engineering forensics ("who deleted that namespace?").

### Audit log volume

A busy cluster produces 100s of MB of audit per hour, dominated by `watch` traffic and leader-election leases. The audit policy is the rate limiter; tune aggressively. A common production policy is `None` for `get/list/watch` of leases and pods/status, `Metadata` for everything else, and `Request` only for security-sensitive verbs (create/update/delete of RBAC objects).

If you exceed the sink's throughput, the apiserver will block on the audit emit (it's synchronous by default for the file backend). Always set `--audit-log-batch-max-size` and use the webhook backend with a buffered sink.

---

## 32. The "What's Slow" Debugging Tree

Production incident, 3am, "my service is slow." The decision tree:

```
                                "my service is slow"
                                         │
                ┌────────────────────────┼────────────────────────┐
                │                        │                        │
                ▼                        ▼                        ▼
        Is the service running?    Is the service        Is the cluster healthy?
        ───────────────────────    receiving traffic?    ───────────────────────
        $ kubectl get pod          ────────────────────  $ kubectl get nodes
        $ kubectl logs --tail=50   $ kubectl get        $ kubectl top nodes
        $ kubectl describe pod        endpointslice     $ check Prometheus
                │                  $ kubectl exec       up{job="apiserver"}
                ▼                       curl localhost:N
        Pod NotReady?
        ───────────
        kubectl describe pod    -- check Conditions, probe failures
        kubectl get events      -- pull/scheduling/crash reasons
        kubectl logs --previous -- last terminated container's logs
                │
                ▼
        Pod Ready, app slow?
        ────────────────────
        kubectl top pod              -- CPU/mem usage NOW
        Prometheus query:
          rate(container_cpu_usage_seconds_total{pod=~"X"}[5m])
          container_memory_working_set_bytes{pod=~"X"} / container_spec_memory_limit_bytes
          rate(container_cpu_cfs_throttled_seconds_total{pod=~"X"}[5m])  -- CPU THROTTLING
                │
                ├─ throttled? → CPU limit too low or other co-tenants hot
                ├─ memory near limit? → OOM imminent; check workingset trend
                ├─ neither? → app-level slowness, not resource
                │
                ▼
        App-level: probe metrics + app metrics + traces
        ─────────────────────────────────────────────
        prober_probe_total{result="failed", probe_type=...}      -- failing probes
        Prometheus query against app's own /metrics
        Tempo / Jaeger: find a slow trace, look at child spans
                │
                ▼
        Downstream slow?
        ────────────────
        Check downstream service: DB, Redis, another K8s service
        Check etcd:   etcd_disk_wal_fsync_duration_seconds (if it's the apiserver path)
        Check apiserver:  histogram_quantile(0.99, apiserver_request_duration_seconds)
        Check kube-proxy: kubeproxy_sync_proxy_rules_duration_seconds
                │
                ▼
        You might be the bottleneck
        ───────────────────────────
        Is apiserver_current_inflight_requests at the limit?
        Is APF rejecting your requests? apiserver_flowcontrol_rejected_requests_total
        Are you holding a long-lived watch that's wedged?
```

The most underused debugging command is `kubectl describe pod`. It shows events, probe results, the last termination reason, container statuses, and conditions — most "why is this pod broken?" questions are answered by it.

The second most underused: `kubectl logs --previous` to see the logs of the *last terminated container*. If a container is crash-looping, the current pod's logs are empty (it just started); the previous container's logs have the actual crash reason.

The least-known: `kubectl get --raw /api/v1/nodes/<node>/proxy/metrics/cadvisor | grep <pod>` — bypass everything and read straight from the kubelet.

---

## 33. Cardinality: The Silent Killer

Every unique combination of `{metric_name, label1=value1, label2=value2, ...}` is a separate **series** in Prometheus. The storage cost per series is small; the cost of *millions of series* is catastrophic.

Cardinality multiplies. A metric with three labels — `service` (200 values), `pod` (10000 values), `endpoint` (50 values) — has 200 × 10000 × 50 = 100M potential series. Even if only a fraction are present at any moment, the active series count balloons.

### The pod-name trap

`kube_pod_info{pod=...}` has one series per pod. Restart a Deployment with 100 replicas, and you've created 100 new series (the old ones expire after stale time, but they remain in the index until the next compaction). Over a week, a cluster with healthy churn easily accumulates 100K+ `kube_pod_*` series.

### Real-world cardinality explosions

| Metric (real-world example) | Disaster |
|---|---|
| `http_requests_total{path=...}` where `path` includes the full URL (with query strings) | every unique request URL is a series; a request fuzzer creates millions |
| `db_query_duration_seconds{query=...}` where `query` is the full SQL | every SQL statement is a series; ORMs that vary parameter order multiply by N! |
| `kafka_consumer_lag{partition=..., offset=...}` with offset as a label | every offset is a series — billions |
| `kube_pod_init_container_status_last_terminated_reason{reason=..., container=...}` | reason has dozens of values; multiplied by N containers across the cluster |
| A label "request_id" or "trace_id" | unbounded; one series per request |

### Defenses

1. **Drop or relabel high-cardinality labels at scrape time** via `metric_relabelings`:

```yaml
metricRelabelings:
  - sourceLabels: [__name__, reason]
    regex: "kube_pod_init_container_status_last_terminated_reason;(OOMKilled|Error|ContainerCannotRun)"
    action: keep
  # or, drop the metric entirely:
  - sourceLabels: [__name__]
    regex: "kube_pod_init_container_status_last_terminated_reason"
    action: drop
```

2. **Use recording rules to aggregate down** before storing:

```yaml
- record: cluster:requests:rate1m_by_service_code
  expr: sum by (service, code) (rate(http_requests_total[1m]))
# now you can drop the raw metric and keep only the aggregate
```

3. **Bucket the high-cardinality dimension**:

```python
# instead of:
counter.labels(path=request.path).inc()
# do:
counter.labels(path=normalize_path(request.path)).inc()
# where normalize_path turns /users/42 into /users/:id
```

4. **Set a hard cap**. Prometheus has `--query.max-samples`, `--storage.tsdb.head-chunks-write-queue-size`, and per-target `sample_limit` and `series_limit` (in `scrape_config`). Setting `sample_limit: 100000` aborts scrapes that exceed it — the scrape fails, but the rest of the cluster's metrics are unaffected.

5. **Alert on cardinality**:

```yaml
- alert: PrometheusHighCardinalityMetric
  expr: |
    topk(5, count by (__name__) ({__name__=~".+"})) > 1000000
  for: 1h
```

### `kube_pod_status_phase` is fine, `kube_pod_init_container_status_last_terminated_reason × reasons × containers × pods` is not

The KSM developers carefully picked label sets to keep cardinality bounded. `kube_pod_status_phase` has 5 phases × N pods = bounded by pod count. But `kube_pod_init_container_status_last_terminated_reason` multiplies pod count × init container count × reason count. On a 50000-pod cluster with 2 init containers/pod and 10 reasons, you're at 1M series for that *single metric*. Drop it unless you need it.

---

## 34. Per-Pod Resource Accounting at Scale, PSI

cAdvisor's housekeeping loop reads every cgroup file every second. On a node with 2000 containers, that is 2000 × ~15 files = 30000 file reads per second, plus parsing, plus the network-namespace enumeration for network metrics. On modern hardware this is fine; on very dense nodes (Karpenter packing 500-pod nodes) it starts to dominate node CPU.

### PSI: Pressure Stall Information

Cgroup-v2 exposes **PSI** (Pressure Stall Information) on three resources: `cpu`, `memory`, `io`. Each file (`/sys/fs/cgroup/.../cpu.pressure`) contains rolling averages over 10s, 60s, 300s of "fraction of time at least one task was stalled waiting for this resource."

```
$ cat /sys/fs/cgroup/kubepods.slice/cpu.pressure
some avg10=2.31 avg60=1.50 avg300=0.72 total=51234567
full avg10=0.00 avg60=0.00 avg300=0.00 total=0
```

- `some` = at least one task stalled
- `full` = all tasks stalled (only meaningful for memory/io)

PSI is a **system-level signal** rather than per-container, but it answers the operational question "is something on this node starved?" cheaply — one file read per resource per node, not per container.

For per-container PSI, cgroup-v2 also exposes `cpu.pressure`, `memory.pressure`, `io.pressure` *per cgroup* (i.e., per pod and per container). cAdvisor recently added PSI as labels on `container_pressure_*` metrics. The cost: still per-container, but a single file read rather than parsing five files.

On a dense node with 5000 containers, switching some signals from cAdvisor's housekeeping to PSI sampling reduces overhead by ~5×.

### Node-Problem-Detector

The [node-problem-detector](https://github.com/kubernetes/node-problem-detector) DaemonSet watches for kernel-level issues (CPU stalls, NTP drift, OOM kills, oom-kill journal messages, kernel oopses) and reports them as `NodeCondition`s or events. It complements PSI: PSI tells you "the cgroup is starved", NPD tells you "the kernel logged an OOM at 13:42:01."

---

## 35. eBPF Observability: Pixie, Hubble, Tetragon, Parca

eBPF (extended Berkeley Packet Filter) lets you run sandboxed programs in the kernel attached to hooks (syscalls, network packets, function entries). Multiple observability tools use eBPF to extract data with no instrumentation.

| Tool | Signal | What it does |
|---|---|---|
| **[Pixie](https://github.com/pixie-io/pixie)** | Application protocol observability (HTTP, gRPC, MySQL, Postgres, Redis, Kafka, DNS) | Attaches uprobes to OpenSSL and standard sockets; reconstructs L7 protocols at the wire level; produces per-request latency and traces without code changes. |
| **[Cilium Hubble](https://github.com/cilium/hubble)** | Network flows and policy verdicts | eBPF programs at TC and socket layer log every packet (or sampled packets) with full L3/L4/L7 metadata, pod identity, and policy decision. |
| **[Tetragon](https://github.com/cilium/tetragon)** | Security: syscalls, file access, network connects, process exec | eBPF kprobes on security-sensitive functions; allow/deny via in-kernel policy; rich audit stream. |
| **[Parca](https://github.com/parca-dev/parca)** | CPU profiles | eBPF perf_event sampler — captures stack traces at 99 Hz; aggregates flamegraphs. |
| **[KubeArmor](https://github.com/kubearmor/KubeArmor)** | Runtime security policy | LSM hooks + eBPF; enforces file/process/network rules at the kernel level. |
| **[Inspektor Gadget](https://github.com/inspektor-gadget/inspektor-gadget)** | Ad-hoc kernel introspection | A toolkit of eBPF programs ("gadgets") for things like trace-tcp, trace-exec, trace-mount; CLI-driven. |

The unifying story: **observability without instrumentation**. You don't need to modify the application; you don't need to deploy SDKs. The kernel sees everything, eBPF gives you safe access to it, and the agent ships the data.

The tradeoff: kernel version sensitivity (some hooks need 5.10+, some 6.x), CO-RE (Compile Once, Run Everywhere) makes this mostly tolerable but not always. eBPF observability is the *default* in 2026 for any cluster using Cilium as CNI; bolt-on for clusters that don't.

---

## 36. Control-Plane SLOs and SLIs

[SIG-Scalability](https://github.com/kubernetes/community/tree/master/sig-scalability) publishes the K8s scalability SLOs — the targets the project itself tests against:

| SLI | SLO |
|---|---|
| API call latency: GET / LIST | p99 ≤ 1s for resource access |
| API call latency: mutating | p99 ≤ 1s for mutating calls |
| API call latency: very large lists | p99 scales with list size |
| Pod startup latency (stateless) | p99 ≤ 5s from creation to Running |
| Pod startup latency (stateful) | p99 ≤ 5s + volume mount time |
| In-cluster network programming latency | p99 ≤ 10s from EndpointSlice update to rule applied |
| DNS programming latency | p99 ≤ 5s from Service creation to DNS resolves |

These are the floor. Real production clusters often hit them at 5000 nodes / 150000 pods with default settings. Beyond that, you tune (see [ch 35](35-performance-scaling-and-tuning.md)).

Encode them as PrometheusRule alerts:

```yaml
- alert: KubeAPIServerLatency
  expr: |
    histogram_quantile(0.99,
      sum by (le, verb, resource) (
        rate(apiserver_request_duration_seconds_bucket{
          verb!~"WATCH|CONNECT|PROXY",
          subresource!~"proxy|log|exec|portforward|attach"
        }[5m])
      )
    ) > 1
  for: 10m

- alert: KubeletPodStartupLatency
  expr: |
    histogram_quantile(0.99,
      sum by (le) (rate(kubelet_pod_start_duration_seconds_bucket[5m]))
    ) > 60
  for: 15m

- alert: ServicePropagationLatency
  expr: |
    histogram_quantile(0.99,
      sum by (le) (rate(kubeproxy_network_programming_duration_seconds_bucket[5m]))
    ) > 10
  for: 10m
```

These three alerts catch ~80% of "the cluster feels slow" before users notice.

---

## 37. Dashboards and Drift

The canonical Grafana dashboards every cluster should have:

1. **Kubernetes / Compute Resources / Cluster** (kube-prometheus): cluster-wide CPU, memory, pods, namespaces.
2. **Kubernetes / Compute Resources / Namespace (Pods)**: per-namespace breakdown.
3. **Kubernetes / Compute Resources / Pod**: drill into a single pod.
4. **Kubernetes / Compute Resources / Workload**: deployment / sts / ds view.
5. **Kubernetes / Networking / Cluster**: rx/tx by namespace + pod.
6. **Kubernetes / Networking / Pod**: per-pod network drill.
7. **Kubernetes / API Server**: golden signals for apiserver.
8. **Kubernetes / Controller Manager**: workqueue depths, leader election.
9. **Kubernetes / Scheduler**: pending pods, scheduling latency.
10. **Kubernetes / Kubelet**: per-node PLEG, runtime ops, evictions.
11. **etcd**: fsync, commit, peer RTT, DB size.
12. **Node Exporter Full**: per-node system metrics.
13. **CoreDNS**: query rate, errors, latency.

All thirteen ship with kube-prometheus-stack. They are uniformly excellent and uniformly customized away over time.

**Drift management**: dashboards-as-code. Store dashboards in Git as JSON, render them via Grafonnet or the Grafana Operator's `GrafanaDashboard` CRD, never edit in the UI:

```yaml
apiVersion: grafana.integreatly.org/v1beta1
kind: GrafanaDashboard
metadata: {name: my-app, namespace: monitoring}
spec:
  instanceSelector: {matchLabels: {dashboards: grafana}}
  json: |
    { "title": "my-app overview", ... }
```

This makes dashboards reviewable in PRs and reproducible across clusters.

---

## 38. Cost Observability: Kubecost, OpenCost

Per-tenant cost attribution requires joining:

- Node cost (cloud bill per instance type per hour)
- Pod resource share (requests or actual usage of the node)
- Label-based tenancy (namespace, label `tenant=`, label `cost-center=`)
- PV cost (size + tier)
- Network egress (cross-AZ + cross-region + internet)

[OpenCost](https://github.com/opencost/opencost) (CNCF) is the open-source reference implementation; [Kubecost](https://www.kubecost.com/) is the commercial product (now overlapping heavily with OpenCost since the OpenCost donation). The architecture:

```
   ┌────────────────────────────────────────────────────────────────┐
   │  OpenCost Deployment                                           │
   │                                                                │
   │  Prometheus client → reads container_cpu_usage,                │
   │                       container_memory_working_set,            │
   │                       kube_pod_container_resource_requests,    │
   │                       kube_node_status_capacity,               │
   │                       kube_persistentvolumeclaim_*             │
   │                                                                │
   │  Cloud pricing client → fetches per-instance-type prices       │
   │                          (EC2, GCE, AKS) or static CSV          │
   │                                                                │
   │  Allocator:                                                    │
   │    for each pod:                                               │
   │      share = pod_requests / node_capacity                      │
   │      cost = node_hourly_cost × share × hours                    │
   │    sum by (namespace, label, deployment, ...) → cost            │
   │                                                                │
   │  Exposes /metrics and a UI                                     │
   └────────────────────────────────────────────────────────────────┘
```

Output: `kubecost_cluster_cost_total`, `kubecost_namespace_cost_total{namespace, cost-center, ...}`. Query in PromQL, dashboard in Grafana, set budgets per tenant, alert on overruns.

Label propagation discipline matters: every pod that should be attributable must have the tenancy labels (`team=`, `cost-center=`, `app=`). Enforce via Kyverno or VAP at admission time — pod without `cost-center=` → reject.

Cloud-native alternatives: AWS Cost Explorer with Cost Allocation Tags, GCP Cost Manager with labels, Azure Cost Management. All have Kubernetes-specific dashboards but require the same label discipline.

---

## 39. Multi-Cluster Observability

Once you have >1 cluster, you have an observability federation problem. Three patterns:

### Hub-and-spoke metrics

```
   cluster-A ──remote_write──┐
   cluster-B ──remote_write──┼──▶  Mimir / Thanos Receive / VictoriaMetrics cluster
   cluster-C ──remote_write──┘     (single source of truth; multi-tenant by cluster ID)
                                              │
                                              ▼
                                          Grafana
```

Each cluster runs its own Prometheus (for local scraping) and remote-writes to a central store. Queries always go through the central store.

### Federated queries

```
   cluster-A: Prometheus + Thanos Sidecar ──┐
   cluster-B: Prometheus + Thanos Sidecar ──┼──▶  Thanos Querier (central)
   cluster-C: Prometheus + Thanos Sidecar ──┘            │
                                                          ▼
                                                       Grafana
```

Each cluster keeps its data locally; the central Querier fans out across all sidecars at query time. Higher query latency, lower ingest infrastructure cost.

### Logs and traces

For logs: Loki is naturally multi-tenant; you tag with a `cluster=` label or use the X-Scope-OrgID tenancy.

For traces: Tempo and Jaeger both support multi-tenant ingestion. Per-cluster OTel Collectors → central gateway → Tempo with a tenant ID per cluster.

### The OTel Collector hub pattern

A single OTel Collector deployment (perhaps the same one fronting Mimir, Loki, and Tempo) is the natural fan-in for all signals across all clusters. Each cluster runs an agent that ships OTLP to the central gateway; the gateway sharder routes by tenant.

---

## 40. Pitfalls

Drawn from real production incidents. Some of these you will hit; the rest you should design against.

1. **No metrics-server installed.** `kubectl top` fails; HPA target metrics resolution fails; VPA recommendations stall. Symptom: HPA shows `<unknown>` for current CPU. Fix: install metrics-server (it is not a default in vanilla Kubernetes; managed clusters ship it).
2. **kube-state-metrics unsharded on a giant cluster.** A single KSM pod with 500k objects OOMs at ~12 GB RSS, and the `/metrics` response exceeds Prometheus scrape timeout. Shard via the `--shard` / `--total-shards` flags; deploy as StatefulSet.
3. **Two Prometheus replicas confused as "HA storage."** Two replicas scraping the same targets store *two copies* of the same data; they are not redundant storage. If you need durable cross-replica retention, you need Thanos / Mimir / VictoriaMetrics in front.
4. **Fluentd OOMing on log spikes.** Ruby GC plus a backlog plus heavy parsing patterns = OOM. Vector handles this better (Rust + back-pressure). Mitigations: set `Mem_Buf_Limit`, use `filesystem` buffer instead of memory, set sensible per-instance throughput caps.
5. **Cardinality explosion from one careless label.** Adding `traceID` or `requestID` or full `URL` as a Prometheus label kills the TSDB. The damage often does not show up at scrape time — it shows up at query time when Prometheus runs out of memory. Catch via `topk` queries on `count by (__name__)({__name__=~".+"})`.
6. **Not scraping etcd.** Managed clusters often don't expose etcd; self-managed ones often forget. You lose the most predictive signal for cluster health. If etcd metrics aren't accessible, at minimum get them out-of-band (e.g., the cloud provider's dashboards).
7. **`apiserver_request_duration_seconds` "quantile" confusion.** Using `apiserver_request_duration_seconds{quantile="0.99"}` works on systems exposing it as a *summary* (some still do), but `apiserver_request_duration_seconds_bucket` is the histogram on modern apiservers; query with `histogram_quantile`. Mixing the two gives wrong results.
8. **Alert rules tied to absolute thresholds.** "Error count > 100/min" works for one cluster size and breaks for all others. Use ratios.
9. **100% trace sampling in production.** A million spans/second blows up the trace backend's storage and bandwidth. Use head sampling at 1–10% with tail-based exceptions for errors.
10. **No log retention policy.** Loki / Elastic / ClickHouse keep logs forever by default. Storage runs out; new writes fail. Configure retention (Loki: `table_manager.retention_period`; Elastic: ILM policies).
11. **Wrong Loki index granularity.** Loki's index labels are *what you can query by* — if you didn't label `app=` you can't search by app cheaply. Conversely, putting `pod=` as a Loki stream label creates a stream per pod, and Loki struggles with high-cardinality streams. Pick labels at the *service level*, not the pod level; filter to a pod via line-content matching.
12. **`kube_pod_info` series budget under-counted.** Every pod restart creates a series; over a week of healthy churn, a 10k-pod cluster easily has 100k+ `kube_pod_info` series. Plan TSDB head size accordingly.
13. **PodMonitor / ServiceMonitor with missing `namespaceSelector`.** By default, prometheus-operator scopes to the operator's own namespace. To scrape across namespaces, the Prometheus CR must allow it (`serviceMonitorNamespaceSelector: {}`) and the SM must specify which namespaces it covers.
14. **ServiceMonitor `relabelings` dropping critical labels.** A `replace` with the wrong regex erases `namespace` or `pod`, making downstream alerts unattributable. Always test relabelings with `promtool`.
15. **Events lost without exporter.** Default event TTL is 1 hour. If you don't have `kubernetes-event-exporter` or the OTel `k8s_events` receiver, every post-mortem older than an hour is missing the most informative signal.
16. **Audit log volume overwhelming the sink.** Audit logs can run to 10 GB/hour on a busy 100-node cluster. If your sink is rate-limited (e.g., Splunk HEC), the apiserver blocks. Use the webhook backend with a buffered backend, or downsample with a stricter audit policy.
17. **CPU throttling alarms set wrong.** `container_cpu_cfs_throttled_seconds_total / container_cpu_cfs_periods_total > 0` shows throttling, but real workloads almost always show some. Alert on a high *ratio* sustained, not on "any throttling."
18. **HPA reading metrics-server during its 60-second blackout.** Restart metrics-server, all HPAs see "metric unavailable" for 60–120s and refuse to scale. Mitigation: 2-replica metrics-server with a PDB.
19. **Prometheus disk filling up.** Default retention is 15 days, but at 5M series scraped every 15s that's 250 GB. Either size up the PVC, lower retention, or remote-write to Thanos/Mimir.
20. **Recording rules circular dependency.** Two recording rules each depending on the other → Prometheus refuses to evaluate one (the second one in the group sees an empty input). Promtool can check this; CI should run promtool over your PrometheusRules.
21. **OTel Collector default queue size too small.** The `batch` processor with default settings tolerates ~30s of backend downtime. If your trace backend has an outage, the Collector starts dropping. Set `sending_queue.queue_size` aggressively on the exporter, plus a persistent queue (`file_storage` extension) for durability.
22. **Missing `k8sattributes` processor in the OTel pipeline.** Spans show up with no pod/namespace/node metadata, so you can't search by them. The processor needs RBAC for pods/list,watch — easy to forget on a fresh install.
23. **Loki stream-label drift causing "out of order" rejections.** If two shippers attach slightly different label sets to the same stream, Loki sees them as different streams; timestamps interleave; one stream's writes get rejected as "entry too far behind."
24. **eBPF agents requiring host PID/network/IPC namespace + privileged.** Pixie, Tetragon, Parca all need substantial host access. Confirm your Pod Security Standards allow them in the relevant namespace; consider a dedicated `monitoring-privileged` namespace.
25. **Profiling agent eating CPU on small nodes.** Continuous profiling at 99 Hz × thousands of processes ≠ free. On t3.small nodes, Parca-agent can be 10% of the node. Tune sampling rate, exclude system pods.
26. **Grafana dashboard variables querying high-cardinality labels.** A `pod` variable populated by `label_values(kube_pod_info, pod)` runs a huge query on every dashboard load. Use recording rules or scope by namespace.
27. **Multiple Prometheuses scraping the same target with no de-duplication.** Different external labels per Prometheus means the same metric becomes two series; alert evaluations fire twice (Alertmanager handles this, but only if external labels are consistent). Always set distinct `external_labels` per replica + cluster.
28. **Dashboard drift from in-UI edits.** Day 1: clean install of kube-prometheus-stack. Day 200: every dashboard hand-edited by 30 engineers; no one knows what's the source of truth. Switch to dashboards-as-code (Grafonnet, Grafana Operator's `GrafanaDashboard` CRD).
29. **HA Prometheus + sidecar Thanos with wrong `external_labels`.** Both replicas must share the same `cluster` label and have distinct `replica` labels. Otherwise Thanos refuses to dedupe, query results are doubled.

---

## 41. TL;DR

Kubernetes observability is the deliberate composition of six signals (metrics, logs, traces, events, audit, profiles) sourced from a small, finite set of producers: **cAdvisor inside the kubelet** for container resource metrics, **kubelet `/metrics/*` endpoints** for runtime and probe metrics, **metrics-server** as the aggregated `metrics.k8s.io` API that fuels HPA and `kubectl top` from a 60-second in-memory ring buffer, **kube-state-metrics** as the apiserver-watching projector that turns every object into a Prometheus gauge, and the **control-plane `/metrics` endpoints** (apiserver, scheduler, controller-manager, etcd, kubelet, kube-proxy) that you cannot debug a cluster without. **Prometheus** scrapes them all, **prometheus-operator** turns scrape configs into ServiceMonitor / PodMonitor / Probe / PrometheusRule CRDs, and the **kube-prometheus-stack** Helm chart packages the whole thing with Alertmanager, Grafana, and curated dashboards. Beyond ~10M active series, you scale horizontally with **Thanos** (object-store + sidecar + querier + compactor + ruler), **Mimir** (multi-tenant remote-write), or **VictoriaMetrics** (efficient single-binary alternative). **OpenTelemetry** provides the unified SDK + Collector + OTLP protocol for the future-direction signals (traces, increasingly metrics and logs); the **OpenTelemetry Operator** turns the Collector into a Kubernetes CR and offers an `Instrumentation` CR that auto-injects language SDKs into your pods. **Logs** flow from container stdout via containerd into `/var/log/pods/...` CRI files, tailed by a DaemonSet shipper (**Fluent Bit**, **Fluentd**, **Vector**), enriched with pod metadata, and stored in **Loki** (label-indexed, cheap), **Elasticsearch** (full-text, expensive), or **ClickHouse** (columnar, SQL). **Traces** flow from instrumented apps via OTLP into the Collector and on to **Tempo** (cheap, trace-id-indexed) or **Jaeger** (richly searchable). **Events** are short-lived narrative facts you must ship before their 1-hour TTL via `kubernetes-event-exporter` or the OTel `k8s_events` receiver. **Audit logs** describe every apiserver request and must be split between a SIEM and a queryable engineering store. **Profiles** come from `/debug/pprof` and increasingly from **eBPF-based** continuous profilers (**Parca**, **Pyroscope**) that need no application instrumentation. The two perennial enemies are **cardinality** (the silent killer — every unique label combination is a series; a careless `request_id` label can sink your TSDB) and **fan-in failure** (a wedged shipper, OOMing kube-state-metrics, throttled audit sink, dropped traces). Apply the **four golden signals** (latency, traffic, errors, saturation) per component to build your dashboards and alerts; alert on **ratios with `for:` durations**, not absolutes; treat **etcd as tier-zero** to monitor (fsync latency, leader changes, DB size vs quota); always include **runbook links** in alert annotations; and remember that the most useful debugging command remains `kubectl describe pod` followed by `kubectl logs --previous`. Past one cluster, you federate via **Thanos**, **Mimir**, **Loki multi-tenancy**, or a hub-and-spoke **OTel Collector**. Past a budget, you add **Kubecost / OpenCost** with disciplined label propagation. Past a few thousand series per cluster, you measure cardinality continuously and drop or aggregate aggressively. The single sentence to keep: *every signal is a tuple (producer, transport, store, query layer); you cannot operate a cluster you cannot name the tuple for*.
