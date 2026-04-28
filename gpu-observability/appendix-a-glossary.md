# Appendix A — Glossary

> Plain-English definitions for every acronym and term used across the series. The chapters cross-reference here liberally — when you hit a term you don't recognize, search this file first.

Organized by family, not alphabetically. Each entry: *short definition*, *where it shows up*, *why it matters*.

---

## A.1 GPU Hardware

### SM (Streaming Multiprocessor)
One of the ~100 small independent processors on a GPU die. The H100 has 132. Each SM has its own warp schedulers, register file, L1 cache, CUDA cores, and tensor cores. **`SM_active` is the fraction of cycles where at least one warp was scheduled per SM** — the closest single-number proxy for "is the chip actually working." Doc 00 §2, Doc 02 §2.1.

### Warp
32 threads that execute in lockstep on one SM. The fundamental scheduling unit. A "warp slot" is a hardware register-file allocation; an SM has 64 max warp slots on H100. **`SM_OCCUPANCY` = warps active / max warps** — saturation signal. Doc 00 §2.

### Tensor core
Specialized matrix-multiply unit, 4 per SM on H100. Operates on 4×4 or 8×8 tiles in FP16/BF16/FP8/INT8. The reason H100/B200 are fast for ML — without tensor cores, you're using 1/8th of the silicon. **`PIPE_TENSOR_ACTIVE` = fraction of cycles tensor pipe is firing.** Doc 00 §2.

### CUDA core
The plain FP32 ALU on each SM. 128 per SM on H100. Used for non-tensor ops: element-wise, layer norm, softmax, ReLU. Most ML kernels touch CUDA cores even when tensor cores are doing the heavy lifting.

### HBM (High Bandwidth Memory)
The GPU's RAM. Stacks of DRAM glued to the side of the GPU die via silicon interposer. Capacity 40–192 GB depending on model. **Bandwidth is the killer spec, not capacity** — H100 SXM has 3.35 TB/s. For most ML workloads, bandwidth is the limiter (see *roofline*). Doc 00 §3, Doc 06 §3.

### NVLink
NVIDIA's GPU-to-GPU interconnect. ~900 GB/s aggregate per H100 (18 links × 50 GB/s). Used for tensor parallelism, AllReduce within a node. Different from PCIe (host↔GPU) and InfiniBand (node↔node). **NVLink errors and bandwidth saturation drive most "training got slow" tickets.** Doc 06 §4.

### NVSwitch
The on-board switch fabric inside an HGX/DGX 8-GPU node that connects all GPUs to all GPUs over NVLink. Without NVSwitch, you'd have point-to-point NVLink only. NVSwitch lets all 8 GPUs do AllReduce at full bandwidth.

### PCIe
The bus connecting the host CPU to the GPU. Gen4: 32 GB/s per direction; Gen5: 64 GB/s; Gen6: 128 GB/s. **Much slower than NVLink.** Host→GPU data copies (loading training batches, KV cache spill) traverse PCIe. PCIe link degradation (Gen5 falls back to Gen3) is a common silent failure. Doc 06 §5.

### TDP (Thermal Design Power)
The sustained power the GPU is rated to draw. H100 SXM: 700 W. B200: 1000 W. **GPUs throttle below their TDP if cooling can't keep up.** `power / TDP` is the headline efficiency metric for thermal/cooling investigations.

### GI / CI (GPU Instance / Compute Instance)
MIG sub-divisions. A GI is a hard partition of SMs + HBM (e.g., 1g.10gb = 1/7 of an A100). A CI is a sub-allocation of compute *within* a GI. Both appear as labels on DCGM metrics. Doc 00 §5, Doc 13 §3.

---

## A.2 Software & APIs

### NVML (NVIDIA Management Library)
`libnvidia-ml.so`. Driver-backed library exposing coarse counters: util%, memory used/free, temperature, power, ECC, XID, clocks. The library `nvidia-smi` calls. **Cannot expose SM-level activity** — that requires the profiling subsystem. Doc 01 §2.1.

### DCGM (Data Center GPU Manager)
NVIDIA's production telemetry layer above NVML. Adds the profiling subsystem (`DCGM_FI_PROF_*` fields), health checks, policy callbacks, group operations. Two forms: embedded (linked into your process) or standalone (`nv-hostengine` daemon). Doc 02.

### dcgm-exporter
The NVIDIA-maintained Prometheus exporter that polls DCGM and emits `/metrics`. Runs as a K8s DaemonSet. Configurable via ConfigMap (which field IDs to emit). **The thing every chapter assumes is running.** Doc 02.

### CUPTI (CUDA Profiling Tools Interface)
The C API behind Nsight Systems, Nsight Compute, and continuous-profiling tools. Lets you register callbacks on kernel launches, memory copies, and access hardware counters. **The primitive everyone builds on for production profiling.** Doc 11 §6.

### NVTX (NVIDIA Tools Extension)
Annotation API for marking ranges in your code (`nvtx.range("forward")`). Nsight Systems renders these as colored bands on the timeline. **Without NVTX, profiling traces are a sea of unnamed kernels.** Doc 11 §4.2.

### NCCL (NVIDIA Collective Communications Library)
The library that implements distributed-training collectives (AllReduce, AllGather, Reduce-Scatter, Broadcast) over NVLink + InfiniBand. Used by PyTorch DDP/FSDP, DeepSpeed, Megatron. **NCCL_DEBUG=INFO is the primary debugging surface.** Doc 11 §8, Doc 15.

### MIG (Multi-Instance GPU)
Hardware-enforced GPU partitioning, A100/H100/B200 only. Splits one GPU into up to 7 isolated instances with their own SMs and HBM. **Strong tenant isolation; per-GI metrics in DCGM.** Doc 00 §5, Doc 13.

### MPS (Multi-Process Service)
Soft GPU sharing via a per-node daemon that multiplexes CUDA contexts onto one GPU. No hardware enforcement — noisy neighbors possible. Doc 00 §5, Doc 13 §4.

### Time-slicing
NVIDIA GPU Operator config that advertises N "GPUs" per physical GPU to the K8s scheduler. Pods get full-GPU access, time-multiplexed by the driver. **Even softer than MPS; observability sees one physical GPU only.** Doc 00 §5.

---

## A.3 Errors, Health, RMA

### XID error
NVIDIA's error code taxonomy (1 to ~140). Each code maps to a specific class of failure: XID 79 (GPU fell off bus), XID 63 (ECC page retirement), XID 13 (graphics engine exception). Logged to dmesg by the driver and exposed via `DCGM_FI_DEV_XID_ERRORS`. Doc 07 §2.

### ECC SBE / DBE (Single-Bit / Double-Bit Error)
Memory bit errors detected by HBM ECC. SBE: corrected by ECC, GPU continues. DBE: uncorrectable, page is retired, kernel may crash. **Lifecycle: SBE rate climbs → page retirement → DBE → RMA.** Doc 07 §3.

### Page retirement
When a HBM page accumulates too many SBEs (or one DBE), the driver retires it from use. The page is permanently unavailable until a reboot. `DCGM_FI_DEV_RETIRED_DBE > 0` is an RMA candidate. Doc 07 §3.

### Row remap
H100+ feature: a redundant HBM row is swapped in for a failing one. Hides ECC errors that would have caused page retirement on A100. **`DCGM_FI_DEV_ROW_REMAP_FAILURE > 0` is fatal — out of spare rows.** Doc 07 §3.

### Throttle reasons
Bitfield in `DCGM_FI_DEV_CLOCK_THROTTLE_REASONS`. Bits encode why the GPU is running below max clock: idle, thermal, power, sync_boost, sw_thermal, hw_thermal, hw_power_brakeslowdown, display_clock_setting. **The first thing to check when "GPU is slow."** Doc 02 §2.4, Doc 06 §6.

### RMA (Return Merchandise Authorization)
The vendor process for replacing a failed GPU. Triggered by DBE accumulation, row remap exhaustion, sustained XID 79, or persistent NVLink/PCIe errors. Doc 07 §6.

---

## A.4 Workload Types

### Training (batch)
Long-running job (hours to weeks) that consumes a fixed dataset to update model weights. Throughput-oriented. Step time, samples/sec, GPU SM_active are the headline metrics. Doc 04 §2.

### Inference (stateless)
Long-running service that responds to requests with model predictions. Latency-oriented. p50/p95/p99 latency, RPS, TTFT (for LLMs) are headline. Doc 04 §3.

### Interactive (Jupyter, notebooks)
Human-in-the-loop, idle most of the time. **Most common source of GPU waste** — allocated all day, used 5 minutes. Doc 04 §4, Doc 05 §3.

### Distributed training
Training across multiple GPUs/nodes. Adds collective ops (AllReduce, etc.), straggler detection, pipeline bubble, AllReduce time per rank. Doc 15.

### Tensor parallelism (TP)
Split a single layer's compute across multiple GPUs. Inter-GPU bandwidth-heavy (NVLink). Used in LLM serving and training of >7B models.

### Pipeline parallelism (PP)
Split layers across GPUs in a pipeline. Each GPU runs different layers on different microbatches. **"Bubble time" is the unavoidable idle at pipeline ends.** Doc 04 §2.4, Doc 15.

### Data parallelism (DP)
Each GPU runs the full model on different data. Gradients summed via AllReduce. Simplest form of distributed training.

### FSDP (Fully Sharded Data Parallel)
PyTorch implementation that shards parameters, gradients, and optimizer state across GPUs (instead of replicating). Reduces memory pressure for large models. Adds `gather_params` / `reduce_scatter` ops to NCCL.

---

## A.5 LLM Serving Terms

### TTFT (Time To First Token)
From request arrival to the first generated token emitted. Dominated by the **prefill phase** (compute-bound matmul on the prompt). User-facing latency for chat. Doc 14 §2.

### ITL (Inter-Token Latency)
Time between consecutive output tokens during generation. Dominated by the **decode phase** (memory-bandwidth-bound). User-facing "smoothness" of streaming output. Doc 14 §2.

### Prefill phase
The forward pass over the input prompt that fills the KV cache. **Compute-bound** (large matmuls, tensor cores firing). Doc 14 §3.

### Decode phase
Token-by-token generation after prefill. **Memory-bandwidth-bound** (small matmuls, KV cache reads dominate). Doc 14 §3.

### KV cache
The stored key/value tensors from prior tokens, kept in HBM so the model doesn't re-compute them per output token. **Often 30–70% of GPU memory in inference.** Doc 14 §4.

### Continuous batching
LLM serving technique where new requests join an in-flight batch on each token step (instead of waiting for the batch to finish). vLLM, TGI, Triton support it. **Major latency improvement; requires careful KV cache management.** Doc 14 §5.

### Speculative decoding
Run a small "draft" model to predict N tokens; verify with the big model. If accepted, save (N-1) decode steps. **Acceptance rate is a key metric.** Doc 14 §7.

### LoRA (Low-Rank Adaptation)
Fine-tuning method that adds small low-rank matrices to a frozen base model. Many adapters can hot-swap onto one base — common in inference. **Adapter load/unload overhead is a metric to track.** Doc 14 §8.

### vLLM, TGI, Triton, TensorRT-LLM
LLM serving engines. vLLM (UC Berkeley): open, popular, PagedAttention. TGI (HuggingFace): production-focused. Triton (NVIDIA): general inference server, hosts TensorRT-LLM as a backend. TensorRT-LLM: NVIDIA's optimized LLM runtime, kernel-fused.

---

## A.6 Distributed Training Ops

### AllReduce
Collective op that sums values across all ranks and broadcasts the sum back. Used for gradient sync in DP/FSDP. **Bandwidth-bound on NVLink/IB. Stragglers slow the whole AllReduce.** Doc 15 §2.

### AllGather
Each rank gathers all other ranks' values. Used in FSDP for parameter unsharding before forward.

### Reduce-Scatter
Sum across ranks, then scatter the result. Used in FSDP for gradient sharding.

### Broadcast
One rank sends to all others. Used for initial weight distribution.

### Straggler
A single rank that runs slower than others, blocking the collective. **Per-rank step-time variance is the detection signal.** Doc 04 §2.3, Doc 15 §3.

### Pipeline bubble
Idle GPU time at the start (warmup) and end (cooldown) of pipeline-parallel training. Inevitable but minimizable with smaller microbatches. Doc 04 §2.4.

---

## A.7 Observability Stack

### Prometheus
Pull-based metrics database. Scrapes targets every N seconds, stores time series locally, queries via PromQL. **The default for GPU metrics.** Doc 01 §3, Doc 08.

### Recording rule
Pre-computed PromQL expression evaluated periodically, results stored as a new metric. Used to **collapse high-cardinality queries** before dashboard time. Doc 08 §6.

### Cardinality
Number of unique label-combination time series. **The dominant cost driver in Prometheus.** Doc 00 §7, Doc 08.

### Exemplar
A trace ID attached to a histogram bucket sample. Lets a Grafana panel link a percentile point to a specific trace in Tempo/Jaeger. Doc 09 §7, Doc 11 §9.

### Thanos / Cortex / Mimir
Long-term-storage layers above Prometheus, for multi-cluster federation and >2-week retention. Doc 08 §10.

### Alertmanager
The component that takes Prometheus alert firing events and routes them to PagerDuty/Slack/email. Doc 10.

### Grafonnet
Jsonnet library for generating Grafana dashboard JSON programmatically. The dashboard-as-code path. Doc 09 §8.

### Pyroscope
Continuous-profiling backend. The 2025 GPU support uses CUPTI + eBPF to collect kernel-time flamegraphs with <1% overhead. Doc 11 §6.

---

## A.8 Cluster & Scheduler

### Device plugin (NVIDIA k8s-device-plugin)
The K8s component that advertises `nvidia.com/gpu` as a schedulable resource and binds GPU access into containers. Doc 03 §2.

### `nvidia.com/gpu`
The K8s extended resource string for GPUs. Pods request `resources.limits.nvidia.com/gpu: 1`. With MIG, becomes `nvidia.com/mig-1g.10gb` etc.

### kube-state-metrics
Exporter that turns K8s API state into Prometheus metrics: `kube_pod_status_phase`, `kube_node_status_allocatable`, `kube_pod_container_resource_requests`. Doc 03 §1.

### NodeFeatureDiscovery (NFD)
Labels nodes with hardware capabilities (GPU model, NVLink topology, MIG support). Doc 03 §3.

### GPU Operator
NVIDIA's umbrella Helm chart that installs driver, container toolkit, device plugin, dcgm-exporter, NFD. **The "one Helm chart" path for K8s GPU.** Doc 01 §4.

---

## A.9 Cost & Capacity Terms

### Allocation efficiency
`requested_GPU / allocatable_GPU` — what fraction of available GPUs are claimed by pods. Doc 05 §1.

### Utilization efficiency
`SM_active / 1.0` averaged over time — what fraction of allocated GPU compute is actually used. Doc 05 §2.

### GPU waste
`(allocated_GPU_hours) × (1 - utilization_efficiency) × $/GPU-hour`. The dollar number that drives executive interest. Doc 05 §5, Doc 12 §6.

### Chargeback / showback
Billing GPU usage back to teams (chargeback) or showing it without billing (showback). Requires per-team labels on every metric. Doc 12 §7, Doc 13 §5.

### Bin-packing efficiency
How well the scheduler fits pods onto available GPUs. Fragmentation = unused GPUs that can't be claimed because of CPU/memory mismatch. Doc 05 §4.

---

## A.10 Acronyms — Quick Lookup

| Acronym | Expansion |
|---------|-----------|
| AI | Arithmetic Intensity (FLOP/byte) |
| BF16 | Brain Float 16 (mixed-precision) |
| CI | Compute Instance (MIG sub-division) |
| CUDA | Compute Unified Device Architecture |
| CUPTI | CUDA Profiling Tools Interface |
| DBE | Double-Bit Error |
| DCGM | Data Center GPU Manager |
| DDP | Distributed Data Parallel |
| DP | Data Parallelism |
| ECC | Error-Correcting Code |
| FB | Frame Buffer (HBM) |
| FP4/8/16/32/64 | Floating point precisions |
| FSDP | Fully Sharded Data Parallel |
| GEMM | General Matrix Multiply |
| GI | GPU Instance (MIG sub-division) |
| HBM | High Bandwidth Memory |
| ITL | Inter-Token Latency |
| KV | Key/Value (cache) |
| LoRA | Low-Rank Adaptation |
| MIG | Multi-Instance GPU |
| MPS | Multi-Process Service |
| NCCL | NVIDIA Collective Communications Library |
| NFD | NodeFeatureDiscovery |
| NVLink | NVIDIA's GPU-GPU interconnect |
| NVML | NVIDIA Management Library |
| NVTX | NVIDIA Tools Extension |
| PCIe | Peripheral Component Interconnect Express |
| PP | Pipeline Parallelism |
| RMA | Return Merchandise Authorization |
| SBE | Single-Bit Error |
| SM | Streaming Multiprocessor |
| TDP | Thermal Design Power |
| TP | Tensor Parallelism |
| TTFT | Time To First Token |
| XID | NVIDIA driver error code |

---

## Sources

- [NVIDIA H100 Whitepaper](https://resources.nvidia.com/en-us-tensor-core/gtc22-whitepaper-hopper)
- [NVIDIA DCGM User Guide](https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/index.html)
- [NVIDIA NCCL Docs](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/index.html)
- [Modal — GPU Glossary](https://modal.com/gpu-glossary)
- [Stas Bekman — ML Engineering](https://github.com/stas00/ml-engineering)
