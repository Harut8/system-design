# 00 — Mental Models: How GPUs Actually Work (for Observability People)

> Before you can monitor a GPU, you need a working model of what's inside one. This chapter is the prerequisite all the others assume. If you already know what an SM is, why HBM bandwidth matters more than capacity, why "GPU util = 100%" is a lie, and why one client at a time can read profiling counters — skip ahead. Otherwise, read this twice.

This chapter is the **rosetta stone** for the rest of the series. The DCGM field IDs, the cardinality math, the alert thresholds — none of them make sense until you have a picture of where the numbers come from.

We're not teaching CUDA programming here. We're teaching *the smallest amount of GPU architecture you need to read a dashboard and not be lied to*.

---

## 1. Reading This Chapter

Six mental models, each one a question:

1. **What is a GPU made of?** — SMs, warps, tensor cores, HBM
2. **What does "the GPU is busy" actually mean?** — the utilization lie
3. **Why is HBM the limiter, not compute?** — the roofline intuition
4. **What is MIG, MPS, time-slicing — and why do they look different in metrics?**
5. **Why can only one client read profiling counters?** — the profiling lock
6. **Why does cardinality matter more than throughput?** — the Prometheus tax

Each model unlocks several chapters. Read them in order; the later ones depend on the earlier.

---

## 2. Model 1 — A GPU Is a Grid of Tiny Independent Processors

Forget "the GPU." There is no "the GPU" in a useful sense. There is a chip with **~100 streaming multiprocessors (SMs)**, each one a small independent processor with its own scheduler, registers, L1 cache, and execution units. The H100 has 132 SMs. The A100 has 108. The B200 has 148 (across two dies).

```
   ┌──────────────── H100 die ────────────────┐
   │  SM   SM   SM   SM   SM   SM   SM   SM   │
   │  SM   SM   SM   SM   SM   SM   SM   SM   │     132 SMs total
   │  SM   SM   SM   SM   SM   SM   SM   SM   │
   │  ...  (16 rows × ~8 SMs = 132)            │
   │                                           │
   │   ┌────── L2 cache (50 MB) ──────────┐    │
   │   └────────────────────────────────────┘  │
   │                                           │
   │   ┌────── HBM3 (80 GB, 3.35 TB/s) ──┐     │
   │   └─────────────────────────────────┘     │
   └───────────────────────────────────────────┘
```

Each SM contains:

| Unit | Count per SM (H100) | What it does |
|------|---------------------|--------------|
| **CUDA cores** (FP32 ALUs) | 128 | Plain floating-point math |
| **Tensor cores** (4th gen) | 4 | Matrix multiplication (FP16/BF16/FP8/INT8) — the *whole point* of an ML GPU |
| **FP64 cores** | 64 | Double-precision (HPC, not ML) |
| **Warp schedulers** | 4 | Pick which warp gets to run next cycle |
| **Register file** | 256 KB | Per-SM register storage |
| **Shared memory / L1** | 256 KB | Programmable scratchpad + L1 cache |

A **warp** is 32 threads that execute in lockstep. Schedulers issue one warp instruction per cycle per scheduler — so up to 4 warps can be making progress on a single SM at once.

**Why this matters for observability:** when you see `SM_active = 0.5`, that doesn't mean "half the SMs are doing work." It means "during half the sampled cycles, *at least one* warp was scheduled on each SM." A single warp on a 132-SM chip is enough to drive `SM_active` near 100%. You need `SM_OCCUPANCY` (how many warp slots are filled) and `PIPE_TENSOR_ACTIVE` (how often tensor cores fire) to know whether the silicon is actually being used.

> **Mental model:** A GPU is 132 small CPUs that can only run identical-looking programs. It's *wide*, not *fast*. Performance comes from feeding all 132 of them at once.

---

## 3. Model 2 — "GPU Util = 100%" Is a Lie

NVML (the library `nvidia-smi` uses) reports `utilization.gpu` as **the percentage of time during the last 1-second sample where at least one kernel was running**. That's it. It is **not** "what fraction of the chip was active."

A worked example:

```
Workload A: launch one kernel that uses 1 SM out of 132,
            keep it running for 1 second.
   → nvidia-smi says GPU util = 100%
   → SM_ACTIVE = ~0.008
   → silicon is 99.2% idle

Workload B: launch a kernel using all 132 SMs at full occupancy
            for 1 ms, idle for 999 ms.
   → nvidia-smi says GPU util = 100% (1ms in last sample)
   → SM_ACTIVE = 0.001
   → silicon is essentially idle, but the metric lies

Workload C: tight CUDA-graph training step using all SMs at
            85% occupancy continuously.
   → nvidia-smi says GPU util = 100%
   → SM_ACTIVE = 0.95, SM_OCCUPANCY = 0.85
   → silicon is actually being used
```

All three look identical to NVML. Only the profiling counters distinguish them.

**Why this is the most important number on every dashboard:** people see `GPU util 100%` and think "we're full, buy more GPUs." Then they buy more, those run at the same 100% (5% real), and the bill keeps growing. Doc 05 covers this in detail; the *framing* is here:

| Metric | Source | What it actually says |
|--------|--------|----------------------|
| `DCGM_FI_DEV_GPU_UTIL` | NVML | "≥1 kernel was running" — lies about real usage |
| `DCGM_FI_PROF_GR_ENGINE_ACTIVE` | profiling | "graphics/compute engine was busy" — better |
| `DCGM_FI_PROF_SM_ACTIVE` | profiling | "≥1 warp was scheduled per SM" — *the* number |
| `DCGM_FI_PROF_SM_OCCUPANCY` | profiling | "warp slots actually filled" — saturation |
| `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | profiling | "tensor cores firing" — was the H100 worth its price? |

> **Pitfall:** if your dashboards still show `DCGM_FI_DEV_GPU_UTIL` as the primary utilization, your team is making capacity decisions on a metric that goes to 100% with 1% real load. Migrate.

---

## 4. Model 3 — HBM Bandwidth Is the Real Limiter

A modern GPU has so much compute that *most kernels are memory-bound, not compute-bound.* The limiter isn't FLOPs — it's how fast you can pull bytes from HBM into the SMs.

```
H100 numbers (the ones to memorize):
   FP16 tensor peak:  989  TFLOP/s   ← compute headroom
   HBM3 bandwidth:    3.35 TB/s       ← memory headroom
   Ratio:             ~295 FLOP/byte  ← arithmetic intensity threshold
```

A kernel's **arithmetic intensity** (AI) is the number of FLOPs it does per byte read/written from HBM. If AI > 295, compute is the limiter; if AI < 295, memory is the limiter.

Most ML kernels:

| Kernel | Approx AI | Bound by |
|--------|-----------|----------|
| Large GEMM (matmul, K=4096) | ~4096 | Compute (tensor cores) |
| LLM attention (decode phase) | ~10 | Memory |
| Layer norm | ~10 | Memory |
| Softmax | ~5 | Memory |
| Element-wise ops (add, ReLU) | ~1 | Memory |
| Embedding lookup | <<1 | Memory |

This is why an LLM doing token-by-token generation (decode) shows `SM_active=0.6`, `tensor_active=0.05`, `DRAM_active=0.95`. The chip isn't underused — it's *bandwidth-pinned*. Buying a bigger GPU helps only if it has more HBM bandwidth.

> **Mental model:** Tensor cores can do work faster than HBM can deliver bytes. For most workloads, you're paying for compute that's waiting on memory. The interesting question on a dashboard isn't "is SM active?" — it's "is SM active *and* is DRAM active *and* is tensor active, simultaneously?"

This intuition is why Doc 02's "utilization triangle" (SM_active × SM_occupancy × tensor_active) and Doc 11's roofline analysis are the load-bearing diagnostic patterns.

---

## 5. Model 4 — MIG, MPS, Time-Slicing Look Different in Metrics

A single GPU can be sliced for multiple tenants in three different ways, and each has different observability properties:

```
┌──────────────────────────────────────────────────────────────┐
│  MIG (Multi-Instance GPU)  — H100, A100, B200                │
│  Hard partition. The GPU exposes itself as N independent     │
│  "GPU instances," each with its own slice of SMs and HBM.    │
│  DCGM emits per-GI metrics. Cardinality: GPU × GI × CI.      │
│                                                              │
│  Tenant A          Tenant B          Tenant C                │
│  ┌──────┐          ┌──────┐          ┌──────────┐            │
│  │ 2 SMs│          │ 2 SMs│          │  3 SMs   │            │
│  │ 10GB │          │ 10GB │          │  20 GB   │            │
│  └──────┘          └──────┘          └──────────┘            │
│  Strong isolation. No noisy-neighbor on bandwidth.           │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│  MPS (Multi-Process Service)                                 │
│  Soft partition. Multiple CUDA contexts share one GPU,       │
│  scheduled by the MPS daemon. No hard SM boundaries.         │
│  Observability: per-process attribution requires CUPTI hooks │
│  or the "MPS active thread percentage" hint.                 │
│                                                              │
│  All processes contend for SMs and HBM bandwidth.            │
│  Noisy neighbors are a real failure mode.                    │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│  Time-slicing (NVIDIA GPU Operator config)                   │
│  Even softer. The K8s device plugin advertises N "GPUs" per  │
│  physical GPU. Pods get full GPU access, but only one runs   │
│  at a time per slice. No hardware enforcement.               │
│  Observability: pod-to-GPU-instance mapping is the same      │
│  physical GPU; metrics are per-physical-GPU only.            │
└──────────────────────────────────────────────────────────────┘
```

**Why this matters for observability:**

| Aspect | MIG | MPS | Time-slicing |
|--------|-----|-----|--------------|
| Per-tenant SM_active | Yes (per GI) | No (CUPTI required) | No |
| Noisy neighbor possible | No | Yes | Yes |
| Cardinality multiplier | × (#GIs × #CIs) | ×1 (CUPTI tags) | ×1 |
| `nvidia.com/gpu` resource value | one per GI | one per physical | N per physical |

Doc 03 covers how K8s sees these. Doc 13 covers tenant-level dashboards. The point here: the *observability shape* of a multi-tenant GPU depends on *how* it's shared. You cannot debug an MPS-noisy-neighbor with MIG dashboards.

---

## 6. Model 5 — The Profiling Lock (Why DCGM Goes Silent)

NVIDIA's **profiling subsystem** (the thing behind `DCGM_FI_PROF_*` and Nsight Compute) has a hard rule on pre-H100 GPUs: **only one client can hold it at a time**. If dcgm-exporter is reading SM_active, and you launch `ncu`, one of them gets nothing.

```
   Profiling subsystem (one slot)
            │
   ┌────────┴────────┐
   │                 │
   ├─ Slot taken ──▶ dcgm-exporter (DaemonSet, always running)
   │                 │
   │                 └─ emits DCGM_FI_PROF_SM_ACTIVE etc.
   │
   └─ ncu launched by user ──▶  ✗  fails or kicks dcgm out
```

H100/B200 relaxed this for many counters via a multi-client mode, but you cannot assume it. Code defensively.

**Operational consequence:** every chapter that touches profiling has to deal with this:

- Doc 02 §4 covers the lock and the `dcgmi profile --pause/--resume` workflow
- Doc 11 §11 wraps it for Nsight captures
- Doc 13 covers per-tenant profiling visibility constraints

> **Pitfall:** the most common "DCGM stopped reporting" page is someone running Nsight Compute on a node and not pausing dcgm-exporter first. The metric goes flat, alerts fire, the on-call wakes up. Build the pause/resume into your tooling.

---

## 7. Model 6 — Cardinality Is a Tax You Pay Forever

In Prometheus, **every unique label combination is a separate time series.** Every series costs ~3 KB resident memory plus ~1 byte/sample/15s indefinitely. GPU labels multiply fast:

```
Base GPU metric:
   DCGM_FI_PROF_SM_ACTIVE
     {cluster, node, gpu_uuid, gpu_index}

   1000 nodes × 8 GPUs = 8000 series. Fine.

Add MIG:
   {cluster, node, gpu_uuid, gpu_index, GI_ID, CI_ID}
   8000 GPUs × 7 GI × 1 CI = 56000 series. Still OK.

Add pod (the trap):
   {cluster, node, gpu_uuid, ..., pod, namespace, container}
   56000 × turnover (pods restart) = 1M+ stale series. Pain.

Add request_id (end of times):
   ∞
```

Doc 08 is dedicated to this. The model to internalize *now*: **labels you'd want to graph by are not always labels you should ingest.** If you only ever filter by `pod` on the L4 dashboard (one pod at a time), you don't need pod-labelled series at the L1/L2 level — you can join them at query time via `kube_pod_labels`.

> **Mental model:** every label is a tax you pay forever. Add labels that are *constant per series for the life of the series*. Avoid labels that *churn* (pod, request, trace). Use `group_left` joins to attach churning labels at query time.

The single rule: **the series count grows with the product of label cardinalities. Each new label is multiplicative.**

---

## 8. Putting It Together: The "Healthy GPU" Mental Picture

When you open a node-level dashboard, this is the picture you should be checking against:

```
   ┌───────────────────────────────────────────────────────────┐
   │ Healthy GPU (training-heavy):                             │
   │   SM_active        ≈ 0.85–0.95                            │
   │   SM_occupancy     ≈ 0.50–0.85                            │
   │   tensor_active    ≈ 0.30–0.80   (workload-dependent)     │
   │   DRAM_active      ≈ 0.40–0.80                            │
   │   power            ≈ 90–100% of TDP                       │
   │   temp             ≈ 70–80°C                              │
   │   throttle reasons = 0                                    │
   │   ECC SBE rate     ≈ 0/hour                               │
   │   XID errors       = 0                                    │
   └───────────────────────────────────────────────────────────┘

   ┌───────────────────────────────────────────────────────────┐
   │ Healthy GPU (LLM decode-heavy inference):                 │
   │   SM_active        ≈ 0.60–0.80                            │
   │   tensor_active    ≈ 0.05–0.20  (decode is bandwidth-bound)│
   │   DRAM_active      ≈ 0.85–0.99  ← the saturating signal   │
   │   power            ≈ 70–85% of TDP                        │
   │   temp             ≈ 60–75°C                              │
   └───────────────────────────────────────────────────────────┘

   ┌───────────────────────────────────────────────────────────┐
   │ Sick GPU symptoms (each chapter teaches one):             │
   │   SM_active flatlines at 0.0   → exporter dropped, or XID │
   │   SM_active high, tensor zero  → wrong dtype / no fusion   │
   │   DRAM_active 0, SM_active high→ probably synthetic test  │
   │   SBE rate climbing             → memory degrading        │
   │   throttle bits != 0            → cooling, power, or PCIe │
   │   power capped, util high       → power-limit regime      │
   └───────────────────────────────────────────────────────────┘
```

If you can read those three boxes and *predict what a healthy training cluster's dashboards look like*, you're ready for the rest of the series.

---

## 9. Glossary Anchors

These are the terms that recur. Full definitions in Appendix A.

| Term | One-liner |
|------|-----------|
| **SM** | Streaming Multiprocessor — one of ~100 mini-processors on the chip |
| **Warp** | 32 threads that execute together in lockstep |
| **Tensor core** | Specialized matrix-multiply unit; the reason H100/B200 are fast for ML |
| **HBM** | High-Bandwidth Memory — the GPU's RAM (40–192 GB depending on model) |
| **NVLink** | NVIDIA's GPU-to-GPU interconnect; ~900 GB/s on H100 |
| **MIG** | Multi-Instance GPU — hard partition of one GPU into N independent instances |
| **GI / CI** | GPU Instance / Compute Instance (MIG sub-divisions) |
| **MPS** | Multi-Process Service — soft sharing of one GPU across processes |
| **NVML** | NVIDIA Management Library — coarse counters via the driver |
| **DCGM** | Data Center GPU Manager — the production telemetry layer above NVML |
| **CUPTI** | CUDA Profiling Tools Interface — the API behind Nsight |
| **XID** | NVIDIA error code (1–140+); see Doc 07 |
| **ECC SBE/DBE** | Single-Bit / Double-Bit Error in HBM ECC |
| **TTFT / ITL** | Time-To-First-Token / Inter-Token Latency (LLM serving) |
| **AllReduce** | Distributed-training collective op; sum gradients across ranks |

---

## 10. What You Should Now Be Able to Do

You're ready for Doc 01 onward when you can:

- [ ] Explain why `nvidia-smi` showing 100% util doesn't mean the GPU is full
- [ ] Name the three numbers in the "utilization triangle"
- [ ] Predict whether an LLM decode kernel is compute- or memory-bound, and why
- [ ] Distinguish MIG, MPS, and time-slicing by what they look like in metrics
- [ ] Explain why running Nsight on a node can blackhole DCGM
- [ ] Spot the cardinality trap when someone proposes "add pod label to every metric"

If any of these still feel hand-wavy, re-read the relevant section before Doc 01 — the rest of the series is built on these.

---

## 11. Forward Pointers

- **Doc 01** — the architecture stack assumes you understand the NVML/DCGM split (Models 1, 5)
- **Doc 02** — DCGM field IDs are organized by Models 1–3
- **Doc 05** — the utilization gap formalizes Model 2
- **Doc 08** — cardinality budget formalizes Model 6
- **Doc 11** — roofline analysis formalizes Model 3
- **Doc 13** — multi-tenancy is Model 4 in production
- **Appendix A** — full glossary
- **Appendix B** — DCGM field-ID cheat sheet

---

## Sources

- [NVIDIA H100 Architecture Whitepaper](https://resources.nvidia.com/en-us-tensor-core/gtc22-whitepaper-hopper)
- [NVIDIA — How to use the GPU Utilization metric](https://developer.nvidia.com/blog/understanding-gpu-utilization-with-dcgm/)
- [Modal — GPU Glossary](https://modal.com/gpu-glossary)
- [Horace He — Making Deep Learning Go Brrrr](https://horace.io/brrr_intro.html)
- [Stas Bekman — ML Engineering: GPU Architecture](https://github.com/stas00/ml-engineering)
