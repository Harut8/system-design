# 11 — Profiling Integration: Beyond Metrics

> Metrics tell you `SM_active = 0.45`. They cannot tell you which CUDA kernel is the bottleneck, whether the kernel is compute-bound or memory-bound, or why a specific AllReduce took 80ms. Profiling does.

This doc bridges the metrics world (Prometheus, DCGM) and the profiling world (Nsight, CUPTI, PyTorch Profiler). It's about *when* to reach for each tool, *how* to integrate them with continuous monitoring, and how to keep the overhead low enough to run in production.

---

## 1. When Metrics Aren't Enough

Examples that metrics flag but cannot explain:

| Symptom (from metrics) | Question only profiling answers |
|------------------------|---------------------------------|
| `SM_active=0.6` but expected 0.95 | Which kernel left SMs idle? |
| `DRAM_active=0.9` but `tensor_active=0.05` | Are we paying tensor cost on memory-bound kernels? |
| Step time regressed 20% after upgrade | Which kernel got slower? |
| One rank's AllReduce takes 2× the others | Is it network, NCCL, or compute? |
| TTFT high on vLLM | Is prefill compute-bound or KV-cache-IO-bound? |
| Sporadic 10ms latency spikes | Memory allocator pause? CUDA context sync? |

The metrics dashboard tells you *something is wrong* and *roughly where*. Profiling tells you *what to fix*.

---

## 2. The Tool Stack

| Tool | Layer | Best for | Production-safe? |
|------|-------|----------|------------------|
| **CUPTI** | API | Programmatic counter access; building exporters | Yes (the underlying primitive) |
| **Nsight Systems** (`nsys`) | Trace | Timeline view: kernels, comms, CPU, GPU, NCCL, NVTX | Yes, with sampling |
| **Nsight Compute** (`ncu`) | Per-kernel | Kernel-level counters, roofline, source-level metrics | No — too heavy |
| **PyTorch Profiler** | Framework | PyTorch ops + CUDA + Stack traces; TensorBoard view | Yes, with schedule |
| **Pyroscope (eBPF + CUPTI)** | Continuous | Always-on flamegraphs of GPU kernel time | Yes — production-designed |
| **NCCL_DEBUG=INFO logs** | Framework | NCCL collective timing, ring/tree topology | Yes (log only; parse offline) |

The split: **Nsight Systems for one-off investigation**; **PyTorch Profiler for development**; **Pyroscope/CUPTI for continuous production**.

---

## 3. Roofline Analysis — the diagnostic question

Before any kernel-level profiling, the roofline model classifies the kernel:

```
performance (FLOP/s)
   │
   │   peak (compute-bound region)
   │  /─────────────────────────
   │ /
   │/  arithmetic intensity threshold
   │
   │
   └───────────────────── arithmetic intensity (FLOP/byte)
   memory-bound region
```

A kernel's *arithmetic intensity* (FLOPs per byte transferred from HBM) determines whether it's bound by compute or memory. Above the threshold, more FLOPs = more time. Below, more bytes = more time.

H100 peak FP16 (tensor): ~990 TFLOP/s. HBM3 BW: ~3.35 TB/s. Threshold: ~295 FLOP/byte.

So:
- **GEMM with K=4096**: AI ~4096 → compute-bound → tensor cores should be busy
- **Layer norm**: AI ~10 → memory-bound → tensor cores idle is OK
- **Softmax**: AI ~5 → memory-bound

When a kernel that should be compute-bound shows low tensor activity, you have a software bug (wrong dtype, no fused kernel). The metric system can spot this:

```promql
# Suspicious: high SM_active but low tensor_active for an LLM workload
DCGM_FI_PROF_SM_ACTIVE > 0.7
  and DCGM_FI_PROF_PIPE_TENSOR_ACTIVE < 0.05
  and on(pod, namespace) group_left() kube_pod_labels{label_workload_type="llm-inference"}
```

---

## 4. Nsight Systems — the timeline tool

`nsys profile` produces a `.qdrep` (or `.nsys-rep`) trace file showing:

- CPU thread activity
- CUDA API calls
- GPU kernel timeline per stream
- Memory copies (H2D, D2H, P2P)
- NCCL collective phases
- NVTX ranges (your application annotations)

### 4.1 Capturing in production

```bash
nsys profile \
  --output=/captures/prod-$(hostname)-$(date +%s) \
  --trace=cuda,nvtx,osrt,nccl \
  --duration=60 \
  --sample=cpu \
  --capture-range=nvtx \
  --capture-range-end=stop \
  --force-overwrite=true \
  --stop-on-exit=true \
  -- ./your_application
```

Overhead: 1–3% if you stay on `--trace=cuda,nvtx,nccl` and skip CUPTI counter modes.

### 4.2 NVTX annotations — the load-bearing pattern

Your application emits NVTX ranges to label phases:

```python
import torch.cuda.nvtx as nvtx

def train_step(batch):
    with nvtx.range("train.step"):
        with nvtx.range("forward"):
            output = model(batch)
        with nvtx.range("loss"):
            loss = criterion(output, batch.target)
        with nvtx.range("backward"):
            loss.backward()
        with nvtx.range("optimizer"):
            optimizer.step()
```

Then in Nsight Systems you see exactly which phase took 20ms longer this step. Without NVTX, you stare at a sea of unnamed kernels.

### 4.3 Triggered captures from alerts

The pattern: an alert (e.g., training step time regression) triggers a capture on the affected node:

```yaml
# Alertmanager webhook → script that runs nsys profile on the suspect pod
- name: nsys-capture-trigger
  webhook_configs:
    - url: http://capture-trigger:8080/nsys
      send_resolved: false
```

The capture trigger script:

```bash
# Receives webhook with alert labels including pod/namespace
kubectl exec -n $NAMESPACE $POD -- nsys profile \
  --output=/captures/$ALERTNAME-$(date +%s).nsys-rep \
  --duration=30 \
  -- /proc/1/exe  # attach to existing PID 1

# Upload to S3 for analysis
aws s3 cp /captures/* s3://gpu-profiles/triggered/
```

Now every regression alert ships with a timeline you can open. Doc 10 alerts can be paired with this for the cases where it makes sense.

---

## 5. PyTorch Profiler — for ML engineers

The standard pattern in training code:

```python
from torch.profiler import profile, schedule, tensorboard_trace_handler

prof = profile(
    activities=[
        torch.profiler.ProfilerActivity.CPU,
        torch.profiler.ProfilerActivity.CUDA,
    ],
    schedule=schedule(
        wait=1,        # skip first step
        warmup=1,      # warmup overhead-skewed step
        active=3,      # capture 3 steps
        repeat=2,      # 2 capture windows
    ),
    on_trace_ready=tensorboard_trace_handler('/tb-logs/'),
    record_shapes=True,
    with_stack=True,
)

with prof:
    for step in range(N):
        train_step()
        prof.step()
```

The schedule prevents capturing the entire run (which would be terabytes). You get 6 captured steps spaced through the run. TensorBoard renders the trace with kernel-level breakdown.

The output also feeds the PyTorch Profiler Recommendations: it parses kernel names and surfaces "DataLoader is the bottleneck" / "Optimizer.step() is sync-blocking" / "Try using channels-last memory format."

---

## 6. Continuous GPU Profiling (Pyroscope-style)

The 2025/2026 advance: always-on profiling with negligible overhead, surfacing flamegraphs of GPU kernel time the way CPU profilers surface CPU time.

### 6.1 The architecture

```
   App with CUDA kernels
       │
       ▼
   CUPTI callbacks ──▶ USDT probes ──▶ eBPF kernel-space ──▶ pyroscope agent ──▶ pyroscope server
                                       │
                                       └── kernel name, duration, calling thread, GPU UUID
```

The eBPF probe avoids the filesystem-IO overhead that traditional Nsight captures pay; data flows through ring buffers in the kernel. Reported overhead: < 1% on production training jobs.

### 6.2 What you get

Flamegraphs of kernel time. The X axis is "fraction of GPU time." The Y axis is the call stack down into the kernel. You can:

- Compare two time windows (before/after deploy)
- Filter by `gpu_uuid`, `pod`, `namespace`
- Find which kernel spent unexpected time

### 6.3 When to integrate

If your fleet runs >1000 GPUs continuously, this pays off. If you have 10 GPUs, just use Nsight on demand.

---

## 7. Triton Inference Server Tracing

Triton has built-in distributed tracing (OpenTelemetry compatible):

```bash
# Triton config
--trace-config triton,file=/traces/triton.json \
--trace-config rate=1000 \
--trace-config level=TIMESTAMPS \
--trace-config count=-1
```

Per request, Triton emits spans:

- HTTP/gRPC receive
- Schedule (queue → batch)
- Compute (forward in framework)
- HTTP/gRPC send

Combined with vLLM/TensorRT-LLM-specific spans, you can attribute p99 latency to the exact phase, the same as the inference latency breakdown in doc 04 §4.1 — except per-request, end-to-end.

Integration: pipe spans into Tempo/Jaeger and alongside your Prometheus metrics in Grafana, with `exemplars` linking percentile points to specific traces.

---

## 8. NCCL Tracing for Distributed Training

For collective ops, NCCL has its own logging:

```bash
NCCL_DEBUG=INFO
NCCL_DEBUG_SUBSYS=COLL,P2P,NET,INIT
```

Output is voluminous; parse offline. The signal you're looking for:

```
NCCL INFO Channel 03 [Send] 6[1] -> 7[1] [send] via NET/IB/0
NCCL INFO AllReduce: opCount 0x1234 sendbuff 0x... recvbuff 0x... count 4194304 datatype 7 op 0 root 0 comm 0x...
NCCL INFO ## AllReduce dur 4.23ms
```

For each AllReduce, you get the duration. Per rank, parse and emit Prometheus metrics:

```promql
nccl_allreduce_duration_seconds{rank="0", op_count="...", size_bytes="..."}
```

Then per-rank divergence is a query, and a straggler ticket auto-includes the slow ranks (doc 15 expands).

NCCL 2.27+ adds resilient collectives (handles single-link failure without aborting), surfaced via `NCCL_DEBUG`.

---

## 9. Linking Profiling Traces to Metric Anomalies

The pattern: an alert fires, an annotation lands on the dashboard, a profile capture lives at a known URL. The on-call clicks a link in the alert and lands in the trace.

Concrete via Grafana exemplars:

```yaml
# In Prometheus
exemplar_storage:
  enabled: true

# In histogram metric (instrumentation side)
inference_request_duration_seconds.labels(...).observe(duration, exemplar={"trace_id": trace_id})
```

In Grafana, dots appear on the histogram heatmap; clicking opens the trace in Tempo. Time spent there is *finding the kernel*, not *finding the request*.

---

## 10. Sampling Strategy for Production

The cost-benefit table for continuous profiling:

| Strategy | Overhead | Coverage | Use |
|----------|----------|----------|-----|
| Full Nsight on every run | 5–15% | 100% | Pre-prod load tests only |
| 1% sampling rate (1 of 100 requests traced) | <0.5% | sparse | Inference services |
| Time-windowed (5 min/hour) | < 1% avg | episodic | Training jobs, captures during rare events |
| Always-on Pyroscope | <1% | 100% sampled | Fleet-wide continuous |
| On-demand triggered by alert | 0% baseline | reactive | High-stakes investigations |

The practical answer: **always-on Pyroscope-style + on-demand Nsight from alerts**. Don't run heavy profilers continuously.

---

## 11. Beware: profiling lock conflicts

From doc 02 §4: NVIDIA's profiling subsystem allows one client at a time pre-H100. If you run Nsight while DCGM-exporter holds the profiling lock for `_PROF_*` fields, *one of them gets nothing*.

The pattern in production:

1. dcgm-exporter holds the lock continuously for `_PROF_SM_ACTIVE` etc.
2. When an SRE wants to run `nsys profile`, they must `dcgmi profile --pause -i <gpu>` first.
3. Resume after with `dcgmi profile --resume -i <gpu>`.

Wrap this in tooling. Doc 02 §4.2 shows the Slurm prolog/epilog pattern.

H100/B200 changed this: profiling is multi-client for many counters, and the conflict is rarer. But until your entire fleet is H100+, code defensively.

---

## 12. Acceptance Checklist

You're at a good profiling-integration baseline when:

- [ ] NVTX annotations exist in at least the primary training and inference codebases
- [ ] Nsight Systems can be launched on a target pod from a runbook in ≤2 commands
- [ ] PyTorch Profiler is wired into at least one training framework with reasonable schedule
- [ ] One continuous-profiling solution (Pyroscope or equivalent) is running on a fraction of the fleet
- [ ] Alerts from doc 10 can trigger profile capture (at least manually-triggerable)
- [ ] Triton trace export to a tracing backend (Tempo/Jaeger) is on
- [ ] NCCL_DEBUG=INFO log capture for distributed training jobs is automated
- [ ] Profiling lock conflicts are documented and the pause/resume is automated

---

## 13. Worked Example: Roofline From Production Metrics

Roofline analysis is usually a Nsight Compute thing, but you can produce a **first-pass roofline** from DCGM metrics alone. The recipe:

```python
# scripts/roofline-from-prom.py — daily report
import requests

PROM = "http://prometheus:9090/api/v1/query"

# H100 SXM peaks (memorize: tensor FP16 / HBM3)
PEAK_FP16_TFLOPS = 989
PEAK_HBM_TBPS    = 3.35
THRESHOLD_FLOP_PER_BYTE = PEAK_FP16_TFLOPS * 1e12 / (PEAK_HBM_TBPS * 1e12)

def query(q):
    return requests.get(PROM, params={"query": q}).json()["result"]

# Per-GPU averaged over last hour
sm_active     = query('avg_over_time(DCGM_FI_PROF_SM_ACTIVE[1h])')
tensor_active = query('avg_over_time(DCGM_FI_PROF_PIPE_TENSOR_ACTIVE[1h])')
dram_active   = query('avg_over_time(DCGM_FI_PROF_DRAM_ACTIVE[1h])')

for gpu in sm_active:
    uuid = gpu["metric"]["UUID"]
    sm  = float(gpu["value"][1])
    tc  = next(g["value"][1] for g in tensor_active if g["metric"]["UUID"] == uuid)
    bw  = next(g["value"][1] for g in dram_active   if g["metric"]["UUID"] == uuid)

    # Approximate FLOP rate: tensor_active * peak_tensor + (sm - tensor) * peak_fp32
    achieved_tflops = float(tc) * PEAK_FP16_TFLOPS
    achieved_bw_tbps = float(bw) * PEAK_HBM_TBPS
    arithmetic_intensity = (achieved_tflops * 1e12) / (achieved_bw_tbps * 1e12 + 1e-9)

    # Classify
    if arithmetic_intensity > THRESHOLD_FLOP_PER_BYTE:
        regime = "compute-bound (good if tensor_active high)"
    elif float(bw) > 0.7:
        regime = "memory-bound (HBM saturated)"
    else:
        regime = "underused (both SM and HBM idle)"

    print(f"{uuid[:8]} sm={sm:.2f} tc={tc:.2f} bw={bw:.2f} AI={arithmetic_intensity:.1f} → {regime}")
```

The output:

```
GPU-3a4f sm=0.92 tc=0.78 bw=0.55 AI=1402.0 → compute-bound (good if tensor_active high)
GPU-7b2c sm=0.88 tc=0.04 bw=0.91 AI=43.5  → memory-bound (HBM saturated)
GPU-9f01 sm=0.15 tc=0.01 bw=0.05 AI=66.1  → underused (both SM and HBM idle)
```

**What this tells you per GPU:**
- Compute-bound + high tensor → good, the H100 is being used
- Memory-bound → workload is decode/embedding/elementwise; bigger GPU buys nothing
- Underused → idle waste (Doc 05) or stalls (data loader, sync)

**This is not a substitute for Nsight Compute** for kernel-level decisions, but it's a continuous fleet-wide signal that catches "wrong dtype, no fusion" regressions across thousands of GPUs.

### 13.1 Pipe-to-Prometheus alert from regression

```yaml
- alert: GPUWorkloadRegressedToNonTensor
  expr: |
    avg_over_time(DCGM_FI_PROF_SM_ACTIVE[1h]) > 0.7
    and
    avg_over_time(DCGM_FI_PROF_PIPE_TENSOR_ACTIVE[1h]) < 0.05
    and on(pod, namespace) group_left()
        kube_pod_labels{label_workload_class="llm-inference"}
  for: 30m
  annotations:
    summary: "LLM workload {{ $labels.pod }} is using SMs but not tensor cores"
    runbook_url: "/docs/11-profiling-integration#3-roofline-analysis"
```

Catches the "someone shipped a build that fell back to FP32" regression in <1 hour.

---

## 14. Forward Pointers

- **Doc 14**: LLM-specific profiling — KV cache traces, prefill vs decode kernel breakdown
- **Doc 15**: NCCL trace parsing into Prometheus metrics for AllReduce time per-rank
- **Doc 16**: a real incident where the alert-triggered Nsight capture from §4.3 was the load-bearing tool
- **Appendix C.1**: the "GPU slow" flowchart that branches into profile capture

---

## Sources

- [NVIDIA CUPTI](https://developer.nvidia.com/cupti)
- [NVIDIA Nsight Systems](https://developer.nvidia.com/nsight-systems)
- [NVIDIA Nsight Compute Profiling Guide](https://docs.nvidia.com/nsight-compute/ProfilingGuide/index.html)
- [Polar Signals — Continuous NVIDIA CUDA Profiling In Production (Oct 2025)](https://www.polarsignals.com/blog/posts/2025/10/22/gpu-profiling)
- [PyTorch Profiler](https://docs.pytorch.org/docs/stable/profiler.html)
- [NVIDIA — NCCL 2.27 resilient training](https://developer.nvidia.com/blog/enabling-fast-inference-and-resilient-training-with-nccl-2-27/)
- [Modal — CUPTI glossary](https://modal.com/gpu-glossary/host-software/cupti)
- [eunomia — GPU Profiling Under the Hood](https://eunomia.dev/blog/2025/04/21/gpu-profiling-under-the-hood-an-implementation-focused-survey-of-modern-accelerator-tracing-tools/)
