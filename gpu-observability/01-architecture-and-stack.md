# 01 — Architecture & Stack Overview

> Where every byte of GPU telemetry comes from, where it goes, and what breaks at each hop.

This document is the map. Subsequent docs zoom into individual layers (DCGM, Prometheus, dashboards, alerting, etc.); here we wire them together end-to-end and call out the load-bearing decisions a platform team has to make before writing any YAML.

---

## 1. End-to-End Signal Flow

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              GPU NODE (HGX 8x H100)                      │
│                                                                          │
│   ┌─────────────┐   PCIe   ┌───────────┐   in-proc   ┌────────────┐      │
│   │ GPU silicon │ ───────▶ │  NVML     │ ──────────▶ │ DCGM       │      │
│   │ (HW perf    │  MMIO/   │ (libnvidia│   shmem +   │ host engine│      │
│   │  counters)  │  driver  │ -ml.so)   │   IPC       │ (nv-hosteng│      │
│   └─────────────┘          └───────────┘             │  ine)      │      │
│                                                      └─────┬──────┘      │
│                                                            │ libdcgm.so  │
│                                                            ▼             │
│                                                   ┌──────────────────┐   │
│                                                   │ dcgm-exporter    │   │
│                                                   │ (DaemonSet pod)  │   │
│                                                   │ HTTP :9400       │   │
│                                                   └────────┬─────────┘   │
└────────────────────────────────────────────────────────────┼─────────────┘
                                                             │ /metrics
                                                             │ (pull)
┌────────────────────────────────────────────────────────────▼─────────────┐
│                       PROMETHEUS (per cluster, HA pair)                  │
│                                                                          │
│   scrape (15s) ──▶ relabel ──▶ TSDB head (2h) ──▶ blocks (2h)            │
│                                       │                                  │
│                                       └──▶ remote_write ──┐              │
│                                                           │              │
│   recording rules (1m, 5m rollups) ◀──────────────────────┤              │
└───────────────────────────────────────────────────────────┼──────────────┘
                                                            ▼
                                            ┌────────────────────────────┐
                                            │  Long-term TSDB             │
                                            │  Mimir / Thanos / Cortex    │
                                            │  (object storage backed)    │
                                            └─────┬──────────────────┬────┘
                                                  │ PromQL           │ rules
                                                  ▼                  ▼
                                       ┌──────────────────┐   ┌─────────────┐
                                       │     Grafana      │   │ Alertmanager│
                                       │  L1..L5 dashbds  │   │ → PagerDuty │
                                       └──────────────────┘   └─────────────┘
```

The five hops to memorize:

| Hop | Latency | What can break | Symptom |
|-----|---------|----------------|---------|
| GPU → NVML | µs (driver call) | Driver crash, GPU fell off bus (XID 79) | NVML returns `NVML_ERROR_GPU_IS_LOST` |
| NVML → DCGM | shared mem | Profiling lock held by another tool (Nsight, vendor agent) | `DCGM_FI_PROF_*` series go stale |
| DCGM → exporter | libdcgm IPC | Version skew, hostengine OOM | Exporter `/metrics` returns 500 |
| Exporter → Prometheus | network 15s | Network partition, scrape timeout | `up{job="dcgm-exporter"} == 0` |
| Prometheus → Mimir/Thanos | remote_write, async | Backpressure, WAL fills | `prometheus_remote_storage_samples_pending` rises |

Each layer has a *responsibility boundary* you'll see again and again in the alerting doc (10): hardware health is on-call infra, exporter health is platform, dashboards/SLOs are workload owners.

---

## 2. Layer Responsibilities

### 2.1 GPU silicon → NVML

NVML (`libnvidia-ml.so`) is the in-kernel/driver-backed library that exposes coarse counters: utilization (busy %), memory used/free, temperature, power, ECC counts, XID errors, clock speeds. It's what `nvidia-smi` calls under the hood.

NVML's "GPU utilization" is famously misleading. It reports the *fraction of time at least one kernel was running on the GPU during the last sample window* — not how much of the SMs were active. A kernel using 1 SM out of 132 reports 100% utilization. This is the single biggest reason you cannot stop at NVML for production observability (we cover this in doc 5).

### 2.2 NVML → DCGM

DCGM (Data Center GPU Manager) sits one layer up. It bundles:

- **nv-hostengine** — a daemon (or embedded library) that polls NVML *and* opens NVIDIA's profiling subsystem (the same one Nsight uses) to collect the `DCGM_FI_PROF_*` family.
- **libdcgm.so** — the client library that exporters/agents link against to subscribe to field groups.
- **CLI** (`dcgmi`) and **Python bindings** for diagnostics.

The profiling counters are the ones that matter:

| Field | Meaning | Why you want it |
|-------|---------|-----------------|
| `DCGM_FI_PROF_SM_ACTIVE` | Fraction of cycles ≥1 warp was assigned to an SM | Real "is the GPU doing work" signal |
| `DCGM_FI_PROF_SM_OCCUPANCY` | Avg fraction of warp slots filled | Tells you if you're saturating SM resources |
| `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | Tensor core busy fraction | Are you actually using the silicon you paid for |
| `DCGM_FI_PROF_DRAM_ACTIVE` | HBM bandwidth utilization | Memory-bound vs compute-bound discrimination |
| `DCGM_FI_PROF_PCIE_TX/RX_BYTES` | PCIe traffic per direction | H2D/D2H bottleneck detection |
| `DCGM_FI_PROF_NVLINK_TX/RX_BYTES` | NVLink traffic per link | Collective comm health |

**Critical constraint: profiling lock.** The profiling subsystem is single-client per GPU. If Nsight Systems, a vendor monitoring agent, or a second DCGM instance grabs it, dcgm-exporter loses access and its `_PROF_*` series silently go stale. Doc 02 covers detection.

### 2.3 DCGM → dcgm-exporter

`dcgm-exporter` is NVIDIA's official Go binary. Two deployment modes:

- **Embedded host engine** — exporter loads `libdcgm.so` and runs nv-hostengine in-process. Simplest, recommended for K8s DaemonSet.
- **Standalone host engine** — exporter connects via TCP (`DCGM_HE_PORT_NUMBER`, default 5555) to an external `nv-hostengine` daemon. Used when other tools (DGX system stack, cluster diag tools) already own the host engine.

Version skew between the exporter's bundled `libdcgm.so` and an external host engine is a real failure mode: keep `client_version >= host_engine_version`, never the other way.

The exporter exposes Prometheus text format on `:9400/metrics`. Field selection is via a CSV ConfigMap (the `DCGM_EXPORTER_CONFIG` file) — this is your cardinality lever (doc 08).

### 2.4 Exporter → Prometheus

**Pull, not push, for telemetry.** Prometheus scrapes `:9400` every 15s by default. We deliberately don't switch to push (Pushgateway, OTel collector ingest) for steady-state node telemetry because:

1. Pull gives you `up{}` — a free liveness signal per scrape target. Push hides "the agent crashed" as "the metric stopped updating," indistinguishable from "the metric is legitimately constant."
2. Pull lets Prometheus apply per-target rate limits, scrape timeouts, and honor relabeling deterministically.
3. Pull discovery via Kubernetes service discovery follows pod lifecycle for free; push requires the agent to learn its own identity.

The exception is **batch jobs** (one-shot training runs that finish in 10s) where pull may miss the window. For those: Pushgateway *only*, scoped to job-completion metrics. Never push hardware counters.

Per-scrape volume sanity check: 100 metrics × 8 GPUs × 1 sample = 800 samples/scrape. At 15s scrape interval that's ~53 samples/sec/node. A 1k-node cluster: ~53k samples/sec into the local Prometheus. That fits a single Prometheus replica. The problem (next section) is *cardinality*, not throughput.

### 2.5 Prometheus → long-term store

Local Prometheus retains 15 days by default. For a fleet you want:

- Months of history for capacity planning (doc 12)
- Cross-cluster query for fleet dashboards (doc 09 L1)
- HA so a single Prometheus crash doesn't lose alerts

Three viable choices in 2026 — covered in §5 below.

---

## 3. Design Decisions Worth Defending

### 3.1 Push vs pull (revisited)

Use **pull** for steady-state hardware telemetry; **push** only for ephemeral job-scoped metrics (training step time, job completion). This split shows up later in dashboards: hardware panels query DCGM series; per-job panels join DCGM by `gpu_uuid` against pushed job metadata.

### 3.2 Cardinality budget

The math you write on the whiteboard before deploying anything:

```
series ≈ (# GPUs) × (# metrics emitted) × (label combinations)
```

Concrete: 1,000 nodes × 8 GPUs × 60 base DCGM metrics = **1.4M active series, before any pod labels**. Multiply by:

- **MIG**: 1 H100 → up to 7 GPU Instances (GIs), each with N Compute Instances (CIs). On a fully-MIG-partitioned cluster you can hit `gpu_uuid × gi_id × ci_id` ~ 50× explosion.
- **Pod labels**: pod churn (training jobs that restart, inference deployments rolling) creates new series that live in the head block for the full 2-hour compaction window. A 1k-GPU cluster with active churn can sit on several GB of head-block memory.
- **Hardware mix**: the day someone adds MI300X (40 ECC memory blocks per GPU as separate series) or a different vendor your budget evaporates.

The rule (developed in doc 08): **stable, low-cardinality labels in metrics; pod/job/user identity stays in logs**, joined at query time by `gpu_uuid` + `node`. Workload-identity labels are added only via *recording rules* with bounded cardinality, not raw scrapes.

### 3.3 Retention tiers

A standard 3-tier setup that survives at scale:

| Tier | Resolution | Retention | Backing store | Used by |
|------|-----------|-----------|---------------|---------|
| Hot | 15s raw | 2–6h | Prometheus head | Live alerting, drilldown |
| Warm | 15s raw | 15d | Prometheus on-disk + remote_write | Recent dashboards, postmortems |
| Cold | 5m / 1h rollups | 13mo+ | Object storage (S3/GCS) via Mimir/Thanos | Capacity planning, trends |

Recording rules generate the rollups *inside Prometheus* before remote_write, so the long-term store ingests pre-aggregated data — a 20× write reduction.

### 3.4 Scrape interval

15s for DCGM is the right default. Going to 5s doubles cardinality cost without giving you more signal — DCGM's profiling counters are themselves sampled at ~1s internal intervals and you're rarely making decisions on sub-15s data. Going to 60s saves money but loses sensitivity for transient throttle events and short-lived training-step regressions. **Don't tune scrape interval to save cardinality** — fix labels instead.

### 3.5 HA model for Prometheus

Two replicas scraping the same targets, both remote-writing to the long-term store. Alertmanager dedups alerts. Don't try to share a TSDB between replicas — it doesn't work, and the long-term store handles dedup on the read path (Mimir/Thanos both do this natively).

---

## 4. Reference Architecture (production deployment)

```
                ┌─────────────────────────────────────────────────────────┐
                │                K8s GPU CLUSTER  (1 of N)                │
                │                                                         │
                │  ┌─────────────────────────────────────────────────┐    │
                │  │  GPU NODE  (DaemonSet pods, 1 per node)         │    │
                │  │  • dcgm-exporter        :9400                   │    │
                │  │  • node-exporter        :9100  (host metrics)   │    │
                │  │  • nvidia-device-plugin (allocatable GPUs)      │    │
                │  │  • cAdvisor (kubelet)   :10250 (container/cgroup│    │
                │  │                                metrics, GPU per │    │
                │  │                                pod via DRA)     │    │
                │  └─────────────────────────────────────────────────┘    │
                │                       ▲                                 │
                │                       │ scrape 15s                      │
                │  ┌────────────────────┴────────────────────────────┐    │
                │  │  Prometheus HA pair  (replica A, replica B)     │    │
                │  │  - kube-state-metrics, dcgm, node, cAdvisor,    │    │
                │  │    device plugin                                │    │
                │  │  - recording rules: gpu:sm_active:rate1m, etc.  │    │
                │  │  - remote_write → Mimir                         │    │
                │  └────────────────────┬────────────────────────────┘    │
                └───────────────────────┼─────────────────────────────────┘
                                        │ remote_write (Snappy + protobuf)
                                        ▼
                ┌────────────────────────────────────────────────────────┐
                │  GLOBAL Mimir/Thanos  (multi-tenant, HA, regional)     │
                │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌──────────┐   │
                │  │ Distrib │─▶│ Ingester│─▶│ Storage │─▶│ Querier  │   │
                │  │  (auth, │  │ (in-mem │  │ Gateway │  │ + Query  │   │
                │  │  shard) │  │  + WAL) │  │ (S3/GCS)│  │ Frontend │   │
                │  └─────────┘  └─────────┘  └─────────┘  └────┬─────┘   │
                │                                              │ PromQL  │
                └──────────────────────────────────────────────┼─────────┘
                                                               ▼
                                                     ┌──────────────────┐
                                                     │ Grafana (multi-  │
                                                     │ org, RBAC by team│
                                                     │  / namespace)    │
                                                     └──────────────────┘
                                                     ┌──────────────────┐
                                                     │ Alertmanager     │
                                                     │ → PagerDuty      │
                                                     │ → Slack          │
                                                     │ → ServiceNow     │
                                                     └──────────────────┘
```

What's *not* shown but matters:

- **Service discovery** — Prometheus uses `kubernetes_sd_configs` to discover dcgm-exporter pods via the `dcgm-exporter` ServiceMonitor (Prom Operator) or a static role=endpoints config.
- **TLS / mTLS** — exporter `:9400` should be cluster-internal only. Remote_write to Mimir uses bearer token + TLS.
- **Object storage lifecycle** — S3/GCS bucket policies handle the cold tier rollover, not Prometheus. Compactor runs as a separate Mimir component.

---

## 5. Multi-Cluster Federation Topology

For organizations running >1 GPU cluster (anyone past one region), you have three viable patterns. The right one depends on whether you need *global query* or just *centralized storage*.

### 5.1 Hierarchical federation (cheap, limited)

```
  cluster-A Prom ─┐
  cluster-B Prom ─┼─▶  global Prom (federate /federate, scrape rollups only)
  cluster-C Prom ─┘
```

Each leaf Prometheus exposes recording-rule rollups. A central Prom scrapes those rollups via the `/federate` endpoint. Used by Flipkart at 80M series (Oct 2025 InfoQ writeup) — and it works for pre-aggregated data.

- **Pros**: pure Prometheus, zero new infra. No object storage needed.
- **Cons**: you cannot run high-cardinality drilldown queries globally — only the rollups exist centrally. PromQL `sum by (gpu_uuid)` over the fleet is not possible unless `gpu_uuid` survives the rollup, which defeats the purpose.
- **When**: <5 clusters, all metrics queryable centrally are pre-aggregated, raw data lives in regional dashboards.

### 5.2 Thanos sidecar (medium ops, full fidelity)

```
  cluster-A: [Prom + Thanos sidecar] ──▶ S3 ◀── Thanos store gateway ◀── Querier
  cluster-B: [Prom + Thanos sidecar] ──▶ S3 ◀──┘                          ▲
  cluster-C: [Prom + Thanos sidecar] ──▶ S3 ◀──┘                          │
                                                                          Grafana
```

Each Prometheus runs a Thanos sidecar that uploads its 2h blocks to a shared bucket. A central querier fans out PromQL across sidecars (recent data) and store gateways (historical).

- **Pros**: full fidelity globally, no remote_write throughput pain (uploads are async block uploads, not per-sample), cheapest object-storage-backed option.
- **Cons**: query latency is unbounded by the slowest sidecar; cluster-local Prometheus *is* the durability story for the last 2h (if it dies, you lose ≤2h).
- **When**: you already have Prometheus everywhere and want long-term storage with minimal change.

### 5.3 Mimir / Cortex remote_write (heavy ops, tenant isolation)

```
  cluster-A Prom ──remote_write──┐
  cluster-B Prom ──remote_write──┼──▶ Mimir distributors ──▶ ingesters ──▶ S3
  cluster-C Prom ──remote_write──┘                                        ▲
                                                                          │
                                                          Grafana ──▶ Mimir querier
```

Every sample streams via remote_write to a central Mimir cluster. As of **Mimir 3.0 (Nov 2025)**, the architecture is decoupled via Kafka between distributors and ingesters, so write/query paths scale independently.

- **Pros**: real multi-tenancy with hard isolation (per-tenant rate limits, query limits, cardinality limits), best for orgs that bill per-team. Cortex has effectively stopped at 1.18.x and is in maintenance — Mimir is the answer.
- **Cons**: you run a Kafka cluster + ~6 microservices. Operationally heaviest. Network egress costs for remote_write across regions are real money at GPU scale.
- **When**: >10 clusters, multi-tenant SaaS-style internal platform, or you need per-team cardinality enforcement.

### 5.4 Decision matrix

| You have… | Pick |
|-----------|------|
| <5 clusters, pre-aggregation acceptable | Federation |
| Existing Prometheus everywhere, want LTS | Thanos |
| Multi-tenant platform, per-team quotas | Mimir |
| Already on Cortex 1.18.x | Plan a Mimir migration this fiscal year |

---

## 6. Where Each Concern Lives

A quick lookup of which document owns each design concern, so the reader knows where to go for depth:

| Concern | Doc |
|---------|-----|
| DCGM internals, field groups, profiling lock | 02 |
| K8s integration: device plugin, kube-state, MIG | 03 |
| Workload-specific signals (training, inference) | 04 |
| Allocation vs utilization efficiency | 05 |
| Per-host topology, NVLink, NUMA, IB | 06 |
| Hardware health, XID, ECC, RMA | 07 |
| Cardinality, recording rules, retention | 08 |
| Dashboards (L1–L5), Grafonnet | 09 |
| Alerting tiers, routing, runbooks | 10 |
| Profiling integration (Nsight, CUPTI, Pyroscope) | 11 |
| Capacity planning, chargeback | 12 |
| Multi-tenancy, RBAC, isolation | 13 |
| LLM inference (vLLM, TGI, KV cache) | 14 |
| Distributed training (NCCL, RDMA) | 15 |

---

## 7. Common Failure Modes (forward references)

The scenarios that recur across docs and that the architecture needs to handle by construction:

1. **Profiling lock contention** — Nsight or vendor agent steals it, `_PROF_*` series go silent. Detection: `absent_over_time(DCGM_FI_PROF_SM_ACTIVE[5m])` (doc 02, doc 10).
2. **Pod churn cardinality bomb** — bad CD pipeline restarts inference pods every minute, head block balloons, Prometheus OOMs. Mitigation: drop pod label or use recording rules (doc 08).
3. **GPU fell off bus (XID 79)** — exporter still up, but DCGM returns errors for that GPU; the series simply stops. Detection: `up{} == 1 AND absent(DCGM_FI_DEV_GPU_TEMP{gpu="N"})` (doc 07).
4. **Exporter crashed but pod restarted** — `up{}` shows healthy, but counters reset. Detect with `resets()` on monotonic counters or with restart count from kube-state-metrics (doc 03).
5. **Remote_write backlog** — Prometheus WAL fills, scrape throttling kicks in, you lose hot data. Watch `prometheus_remote_storage_samples_pending` and `prometheus_wal_corruptions_total` (doc 08).
6. **Cardinality from a new GPU model** — first MI300X or B200 node added under a NVIDIA-only label scheme. Catch via `prometheus_tsdb_head_series` trend alert (doc 08).

---

## 8. What You Should Have After Phase 1

After implementing docs 01 + 02 + 03 (Phase 1 in the README), you should have:

- A DaemonSet running dcgm-exporter on every GPU node with a curated field list
- Prometheus HA pair scraping every cluster
- `up{job="dcgm-exporter"}` and `DCGM_FI_DEV_GPU_TEMP` queryable in Grafana
- A working `/federate` or `remote_write` path to a global store (whichever you picked in §5)
- The L2 cluster dashboard from doc 09 rendering for at least one cluster

That's the floor. Everything else — efficiency, alerting, dashboards, capacity — builds on this floor. If any of those four bullets is missing, *do not skip ahead*; the upstream docs assume them.

---

## Sources

- [NVIDIA DCGM-Exporter docs](https://docs.nvidia.com/datacenter/dcgm/latest/gpu-telemetry/dcgm-exporter.html)
- [NVIDIA/dcgm-exporter (GitHub)](https://github.com/NVIDIA/dcgm-exporter)
- [DCGM Feature Overview (profiling fields)](https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/feature-overview.html)
- [Last9 — 10,000 GPUs, One TSDB: Cardinality at GPU Scale](https://last9.io/blog/10-000-gpus-one-tsdb-cardinality-at-gpu-scale/)
- [Grafana — Mimir vs Thanos discussion](https://github.com/grafana/mimir/discussions/3380)
- [Flipkart — Hierarchical Federation at 80M series (InfoQ, Oct 2025)](https://www.infoq.com/news/2025/10/flipkart-prometheus-80million/)
- [Arthur Chiao — GPU Performance: Utilization vs Saturation](https://arthurchiao.art/blog/understanding-gpu-performance/)
