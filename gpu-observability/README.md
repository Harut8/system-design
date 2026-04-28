# GPU Observability — Deep Dive Series

Production-grade GPU observability at the scale of large ML infrastructure platforms (Uber, Meta, Google).
This series covers every layer: hardware counters → DCGM → Prometheus → dashboards → alerting → capacity planning.

Designed as a **textbook with deep dives**: foundational mental models, layer-by-layer chapters, an end-to-end incident walkthrough that ties them together, and reference appendices for daily lookups.

---

## Why This Matters

GPU clusters are expensive, opaque, and fail in ways CPU infrastructure does not.
A single A100 costs ~$10 k. A 1000-GPU cluster running at 40 % utilization burns $6 M/year of waste.
Thermal throttling, NVLink degradation, MIG misconfiguration, and CUDA memory leaks are invisible
without deep telemetry. This series builds the full observability stack.

---

## Reading Paths

Different readers want different things. Pick a path:

| Path | Audience | Read in this order |
|------|----------|--------------------|
| **Newcomer** | First time on a GPU platform | 00 → 01 → 02 → 03 → 09 → Appendix A |
| **Platform engineer building from scratch** | Implementing the stack | 01 → 02 → 03 → 08 → 09 → 10 → 07 → Appendix B |
| **SRE / on-call** | Owning the pager | 00 → 06 → 07 → 10 → 16 → Appendix C |
| **ML engineer wiring jobs into the stack** | Instrumenting workloads | 00 → 04 → 11 → 14 → 15 |
| **Capacity / FinOps** | Cost & efficiency | 05 → 12 → 13 |
| **Incident debugger** | Studying real failures | 16 → walk back into referenced chapters as needed |

Read **Doc 00 first** regardless of path — every later chapter assumes the mental models in it.

---

## Callout Conventions

Chapters use a small set of blockquote callouts. Skim these on a re-read:

- `> **Mental model:**` — a way of thinking the chapter is trying to install
- `> **Pitfall:**` — a footgun to avoid (we hit it; you don't have to)
- `> **Diagnostic patterns:**` — quick "if you see X, suspect Y" tables
- `> **First-look PromQL:**` — copy-paste queries to start an investigation

---

## Document Map

### 0. Mental Models — How GPUs Actually Work
**`00-mental-models.md`**

Foundational chapter. Six mental models (SM/warp/tensor cores, the GPU-util lie, HBM as the limiter, MIG/MPS/time-slicing, the profiling lock, cardinality tax) that the rest of the series assumes. Read first.

---

### 1. Architecture & Stack Overview
**`01-architecture-and-stack.md`**

- End-to-end signal flow: GPU hardware → NVML/DCGM → node-exporter sidecar → Prometheus scrape → Thanos/Cortex → Grafana / alertmanager
- Component responsibilities and failure modes at each layer
- Design decisions: push vs pull, cardinality budget, retention tiers
- Reference architecture diagram (text-based)
- Multi-cluster federation topology

---

### 2. DCGM Exporter — Deep Dive
**`02-dcgm-exporter-deep-dive.md`**

- DCGM architecture: host engine, embedded mode, standalone daemon
- Field groups vs metric groups — which counters to enable and why
- DCP (Data Collection Policy): profiling metrics vs non-profiling (SM occupancy needs profiling mode)
- Key counter families:
  - SM activity, SM occupancy, tensor core utilization
  - PCIe bandwidth (rx/tx), NVLink bandwidth
  - Memory utilization, framebuffer free/used
  - ECC errors (single-bit correctable vs double-bit uncorrectable)
  - XID errors and their taxonomy
  - Power draw, thermal readings, clock throttle reasons
  - NVDEC/NVENC utilization
- Kubernetes DaemonSet deployment pattern
- `dcgm-exporter` ConfigMap for custom field IDs
- Profiling mode conflicts: only one process can hold profiling lock — implications for multi-tenant nodes
- GPU Instance (MIG) metrics: per-GI, per-CI cardinality explosion
- Health checks and watchdog restart behavior

---

### 3. Kubernetes GPU Cluster Observability
**`03-k8s-gpu-cluster-observability.md`**

- kube-state-metrics extensions for GPU resources (`nvidia.com/gpu` resource requests/limits)
- GPU device plugin telemetry: allocatable vs allocated vs available
- Node-level GPU topology: NVLink domains, PCIe switches, NUMA affinity
- Pod-to-GPU binding visibility: which pod owns which GPU index
- GPU sharing modes: time-slicing, MPS, MIG — observability differences for each
- Scheduler-level metrics: pending GPU pods, queue depth, scheduling latency
- `nvidia-smi` topo output parsing for rack-level topology maps
- GPU node taints, labels, and capacity tracking across node pools
- Cluster autoscaler integration: GPU node scale-up/down events
- OOM and device health eviction events from kubelet

---

### 4. Batch vs Stateless Workload Observability
**`04-batch-vs-stateless-workloads.md`**

- Taxonomy: training jobs (batch), inference services (stateless), Jupyter notebooks (interactive)
- Per-workload-type SLOs and what metrics back them
- Training jobs:
  - Step time, samples/sec, GPU throughput over job lifetime
  - Stragglers: one slow GPU tanks the whole AllReduce — detecting rank divergence
  - Checkpoint I/O impact on GPU utilization (the "sawtooth" pattern)
  - Bubble time in pipeline parallelism
- Inference services:
  - Request latency broken down: queue → H2D copy → kernel → D2H copy → response
  - Batch size distribution and dynamic batching efficiency (TensorRT, Triton metrics)
  - Throughput vs latency trade-off curves per model
  - KV cache hit rate (vLLM / PagedAttention metrics)
  - Token generation rate (tokens/sec) and time-to-first-token
- Interactive/Jupyter:
  - Idle GPU detection: GPU allocated but utilization < 5% for > N minutes
  - Session lifecycle tracking

---

### 5. GPU Allocation & Utilization Efficiency
**`05-gpu-allocation-and-utilization-efficiency.md`**

- The utilization gap: requested GPUs vs actually used compute
- Allocation efficiency metrics:
  - `requested_gpu / allocatable_gpu` per node, namespace, team
  - Fragmentation: nodes with GPUs that cannot be scheduled due to CPU/memory mismatch
- Utilization efficiency metrics:
  - SM active ratio (actual work / time GPU was allocated)
  - Tensor core utilization ratio
  - Memory bandwidth utilization vs theoretical peak
- "GPU waste" dashboard: cost-weighted idle GPU hours by team/project
- Bin-packing efficiency: how well the scheduler fills nodes
- MIG slice utilization: per-instance SM and memory utilization
- Chargeback model: how to attribute GPU cost to teams using these metrics
- Quota and limit-range enforcement tracking

---

### 6. Host-Level GPU Utilization
**`06-host-level-gpu-utilization.md`**

- Per-GPU per-node metrics: all 8 GPUs on an HGX node independently tracked
- CPU ↔ GPU data transfer bottlenecks: PCIe bandwidth saturation detection
- NUMA pinning correctness: is the CPU socket local to the GPU being used?
- NVLink fabric health:
  - NVLink bandwidth utilization (per-link, per-direction)
  - NVLink error counters: replay errors, recovery errors, CRC
- Infiniband / RoCE network metrics paired with GPU compute:
  - Detecting network-starved collective operations
- Host memory pressure during GPU workloads (pinned memory, IOMMU)
- GPU topology-aware affinity scoring
- Thermal and power at the host level:
  - Per-GPU power draw vs TDP
  - GPU junction temperature, memory temperature
  - Clock throttle reasons: power limit, thermal, idle, sync boost

---

### 7. Hardware Health & Failure Detection
**`07-hardware-health-and-failure-detection.md`**

- XID error taxonomy (Nvidia XID codes 1–94+):
  - XID 79: GPU has fallen off the bus (fatal)
  - XID 63/64: ECC page retirement
  - XID 45: Preemptive cleanup / application error
  - XID 13: Graphics Engine Exception
- ECC error lifecycle: correctable SBE → uncorrectable DBE → page retirement → RMA
- DCGM health checks: `dcgmHealthSet`, watch groups, policy callbacks
- Proactive vs reactive failure handling:
  - Alert on first SBE spike before DBE occurs
  - Correlate ECC errors with specific workloads
- GPU RMA workflow observability: tracking GPUs through replacement cycle
- Predictive failure signals: rising temperature trend, increasing throttle frequency
- Dead GPU detection: exporter drop-out vs GPU crash
- NVML return code monitoring in application logs
- DCGM field `DCGM_FI_DEV_RETIRED_DBE` page retirement alerting

---

### 8. Prometheus Metrics Design & Cardinality Management
**`08-prometheus-metrics-design-and-cardinality.md`**

- Label taxonomy for GPU metrics:
  - `gpu_index`, `gpu_uuid`, `node`, `pod`, `namespace`, `container`, `gpu_model`
- Cardinality budget: large cluster × 8 GPUs × 100 metrics × label combinations
- MIG cardinality explosion: `gpu_uuid` × `gi_id` × `ci_id`
- High-cardinality solutions: recording rules, pre-aggregation, exemplars
- Critical recording rules for GPU dashboards (PromQL examples)
- Relabeling strategies in scrape configs to drop unnecessary labels
- Thanos/Cortex/Mimir: sharding and query federation for multi-cluster
- Remote write tuning: batch size, parallelism, compression for high-volume GPU telemetry
- Retention strategy: raw 15s scrape → 5m rollup → 1h rollup

---

### 9. Grafana Dashboards — Design & Implementation
**`09-grafana-dashboards.md`**

- Dashboard hierarchy:
  - L1: Fleet overview (all clusters, all GPUs)
  - L2: Cluster view (per-cluster utilization, health, allocation)
  - L3: Node view (per-host GPU breakdown, NVLink, PCIe)
  - L4: Job/pod view (per-training-run or per-inference-deployment)
  - L5: GPU detail (single GPU all counters over time)
- Key panels and PromQL for each level
- Fleet utilization heatmap (GPU index × node × time)
- Allocation efficiency scoreboard by team/namespace
- ECC error timeline with annotation overlays
- Throttle reason breakdown (stacked bar)
- NVLink bandwidth per-link directional view
- Inference latency percentile breakdown (p50/p95/p99)
- Training job efficiency: GPU utilization per step with checkpoint markers
- Template variables: `cluster`, `node`, `gpu_uuid`, `namespace`, `pod`
- Dashboard-as-code: Grafonnet / Jsonnet patterns

---

### 10. Alerting Strategy
**`10-alerting-strategy.md`**

- Alert tiers: page-worthy vs ticket vs informational
- Hardware alerts:
  - DBE ECC error → immediate page (likely GPU failure)
  - SBE ECC rate spike → warning ticket
  - XID 79 (GPU fell off bus) → critical page
  - Temperature > 85°C sustained → warning
  - Power limit throttle sustained → warning
- Utilization alerts:
  - GPU allocated but SM utilization < 5% for 30m → idle GPU ticket
  - Cluster GPU allocation > 95% → capacity warning
  - NVLink error rate increasing → investigation
- Inference SLO alerts:
  - p99 latency > threshold → page
  - Token throughput drop > 20% → warning
- Training straggler alert: one rank's utilization drops while others stay high
- Alert routing: hardware → infra on-call, utilization → ML platform team, SLO → owning service team
- Runbook links embedded in alert annotations
- Inhibition rules: don't fire utilization alerts when node is being drained

---

### 11. Profiling Integration — Beyond Metrics
**`11-profiling-integration.md`**

- When metrics aren't enough: Nsight Systems, Nsight Compute integration
- CUPTI (CUDA Profiling Tools Interface) — what it exposes
- Roofline model analysis: is the kernel compute-bound or memory-bound?
- Profiling in production: sampling vs always-on overhead
- Connecting profiling traces to metric anomalies (correlation by time + GPU UUID)
- PyTorch Profiler + TensorBoard integration for training jobs
- Triton server trace integration for inference
- Continuous profiling approaches: Pyroscope GPU support, NVIDIA Nsight perf SDK

---

### 12. Capacity Planning & Cost Optimization
**`12-capacity-planning-and-cost-optimization.md`**

- GPU demand forecasting from utilization trends
- Workload classification: bursty vs steady-state → sizing implications
- Preemption and priority queues: visibility into preemption rates
- Spot/preemptible GPU instance observability (cloud-specific)
- Right-sizing: from SM utilization data to optimal GPU model selection
- MIG partitioning strategy driven by actual workload profiles
- GPU sharing (time-slicing, MPS) — when metrics say it's worth it
- Cross-cluster bin-packing: moving workloads between clusters based on utilization
- Showback and chargeback dashboards: $/GPU-hour by team, project, model
- Long-term capacity trend: weeks of historical utilization → headroom analysis

---

### 13. Multi-Tenant GPU Observability
**`13-multi-tenant-gpu-observability.md`**

- Isolation levels: full GPU, MIG slice, MPS shared context, time-sliced
- Per-tenant metrics scoping: namespace-based, label-based
- Noisy neighbor detection: one tenant's memory bandwidth starving another
- Quota enforcement visibility: who is over/under quota
- Per-tenant cost attribution with GPU UUID tracking
- Privacy considerations: hiding cross-tenant GPU telemetry
- RBAC on Grafana dashboards: teams see only their namespaces
- Tenant SLO tracking and breach reporting

---

### 14. LLM Inference Observability — Special Topic
**`14-llm-inference-observability.md`**

- Metrics unique to LLM serving (vLLM, TGI, Triton + TensorRT-LLM)
- KV cache utilization and eviction rate
- Prefill vs decode phase GPU utilization split
- Time-to-first-token (TTFT) and inter-token latency (ITL)
- Request queue depth and scheduling latency
- Continuous batching efficiency: batch size distribution over time
- Tensor parallelism efficiency: how balanced is load across TP ranks?
- Speculative decoding acceptance rate (if used)
- LoRA adapter loading/unloading overhead tracking
- GPU memory breakdown: model weights, KV cache, activations, fragmentation
- Connecting LLM metrics to DCGM hardware counters (tensor core utilization during decode)

---

### 15. Distributed Training Observability — Special Topic
**`15-distributed-training-observability.md`**

- Collective communication profiling: NCCL ops (AllReduce, AllGather, Reduce-Scatter)
- NCCL debug metrics: `NCCL_DEBUG=INFO` log parsing pipeline
- Detecting AllReduce stragglers via per-rank step time variance
- Pipeline parallelism bubble detection (idle GPU time between microbatches)
- Tensor parallelism load imbalance
- Gradient norm tracking as a training health signal
- Checkpoint throughput: I/O bandwidth during save, GPU idle during checkpoint
- Failure recovery observability: job restart latency, checkpoint age at failure
- RDMA / InfiniBand metrics during large AllReduce operations
- Integration with PyTorch Distributed (torch.distributed) tracing hooks

---

### 16. Incident Walkthrough — Capstone
**`16-incident-walkthrough.md`**

End-to-end trace of one realistic incident (a 7B fine-tune slowdown caused by NVLink CRC errors) through every chapter of the book: dashboards → cross-layer joins → throttle decode → forensic L5 view → XID → profile capture → RMA decision → postmortem. The synthesis exercise. Read after 01–14.

---

## Appendices

### Appendix A — Glossary
**`appendix-a-glossary.md`**

Plain-English definitions for every acronym and term used across the series, organized by family (hardware, software, errors, workloads, LLM, distributed, observability, K8s, cost). The lookup table when chapters reference SM, GI, TTFT, AllReduce, etc.

### Appendix B — DCGM Field-ID Cheat Sheet
**`appendix-b-field-ids.md`**

Every `DCGM_FI_*` field-ID grouped by family with units, types, meanings, throttle-bitfield decode table, and a copy-paste production ConfigMap. The reference for reading alert YAML and ConfigMap definitions.

### Appendix C — Troubleshooting Flowcharts
**`appendix-c-flowcharts.md`**

Mermaid decision trees for the seven most common ops scenarios: GPU slow, training straggler, inference p99 spike, cardinality bomb, ECC RMA decision, DCGM stopped reporting, idle-GPU hunt. Each flowchart is paired with first-look PromQL. Print them and link from alert annotations.

---

## Implementation Order (Recommended)

| Phase | Documents | Goal |
|-------|-----------|------|
| 0 | 00, Appendix A | Internalize the mental models and the vocabulary |
| 1 | 01, 02, 03 | Get DCGM → Prometheus → basic dashboards working |
| 2 | 05, 06, 08, Appendix B | Utilization efficiency, cardinality, field-ID reference |
| 3 | 07, 10, Appendix C | Hardware health alerting + runbook flowcharts |
| 4 | 04, 09 | Workload-specific dashboards (batch vs stateless) |
| 5 | 11, 12 | Profiling integration and capacity planning |
| 6 | 13, 14, 15 | Multi-tenancy, LLM, and distributed training |
| 7 | 16 | Synthesis: walk a real incident through every layer |

---

## Key Tools & Technologies

| Layer | Tool |
|-------|------|
| GPU metrics source | DCGM, NVML, nvidia-smi |
| K8s integration | dcgm-exporter DaemonSet, device plugin, kube-state-metrics |
| Metrics store | Prometheus + Thanos / Cortex / Mimir |
| Dashboards | Grafana (Jsonnet/Grafonnet) |
| Alerting | Alertmanager + PagerDuty |
| Profiling | Nsight Systems, PyTorch Profiler, Pyroscope |
| Cost attribution | Custom recording rules + Grafana reporting |
| LLM serving | vLLM, NVIDIA Triton Inference Server |
| Training | PyTorch DDP/FSDP, DeepSpeed, Megatron-LM |
