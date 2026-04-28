# 04 — Batch vs Stateless Workload Observability

> Hardware metrics tell you the GPU is at 90% SM utilization. They don't tell you whether your training run is on track to converge tonight or your p99 inference latency is about to breach SLO. That's what application-level metrics do.

DCGM (doc 02) and K8s (doc 03) gave you the *substrate*. This doc covers the *workload* — and specifically how the same hardware looks completely different through the lens of three workload classes.

---

## 1. Workload Taxonomy

Every GPU workload in production falls into one of three classes:

| Class | Lifecycle | SLO type | Example |
|-------|-----------|----------|---------|
| **Batch (training)** | Long-running, fixed work, eventually-completes | Throughput, time-to-completion, $/training-run | Pretraining a 70B model, fine-tuning a vision model |
| **Stateless (inference)** | Continuous, request-driven, must respond now | Latency percentiles, throughput at latency, error rate | LLM API, image generation endpoint |
| **Interactive (notebook)** | User-driven bursts, idle gaps | Loose — "GPU available when I need it" | Jupyter, RStudio with GPU |

The same H100 burns differently in each:

- **Training**: 90%+ SM activity continuously, 24/7 power at TDP, NVLink saturated during AllReduce
- **Inference**: bursty SM activity (5–60% mean depending on load), HBM heavy (KV cache), low NVLink
- **Notebook**: ~5% mean utilization, mostly idle, occasional spikes

Your metrics, dashboards, and alerts must be different for each. A "GPU at 5%" alert that's correct for training is wrong for notebooks (expected) and wrong for inference (might mean the service is down, not under-utilized).

---

## 2. SLOs and Backing Metrics

Per-class SLO templates — these are the contract between platform team and workload owner:

### 2.1 Training SLOs

| SLO | Backing metric |
|-----|----------------|
| Job completes within X hours | `train_step_seconds` × planned steps |
| Throughput ≥ N samples/sec | `train_samples_per_second` |
| GPU utilization (SM_active) ≥ 70% | `DCGM_FI_PROF_SM_ACTIVE` |
| Checkpoint cadence within 1.5× target | `train_checkpoint_seconds_total` rate |
| No straggler ≥10% slower than median rank | `train_step_seconds` per rank (variance) |

### 2.2 Inference SLOs

| SLO | Backing metric |
|-----|----------------|
| p99 latency < 500ms | `inference_request_duration_seconds_bucket` |
| TTFT (LLM) < 200ms | `vllm:time_to_first_token_seconds` |
| Inter-token latency < 50ms | `vllm:inter_token_latency_seconds` |
| Throughput ≥ N RPS at SLO | composite rate × percentile |
| Error rate < 0.1% | `inference_requests_failed_total` / `_total` |
| KV cache hit rate > 25% | `vllm:prefix_cache_hits` / `vllm:prefix_cache_queries` |

### 2.3 Interactive SLOs

| SLO | Backing metric |
|-----|----------------|
| Notebook starts within 30s | `notebook_startup_duration_seconds` |
| Idle reclaim within policy | `notebook_idle_seconds` since last kernel activity |

The SLO menu changes the alert routing (doc 10): training SLO breaches go to the model owners, inference SLO breaches page the service owners, hardware SLO breaches go to platform.

---

## 3. Training Job Observability

### 3.1 Step time and throughput

The two foundational metrics, emitted by the training loop:

```python
# In your training step (PyTorch example)
from prometheus_client import Histogram, Counter

step_duration = Histogram(
    'train_step_seconds',
    'Time per training step',
    buckets=[0.1, 0.2, 0.5, 1, 2, 5, 10, 30, 60],
    labelnames=['job_id', 'rank', 'phase'],
)
samples_processed = Counter(
    'train_samples_total',
    'Total samples processed',
    labelnames=['job_id', 'rank'],
)

with step_duration.labels(job_id=JOB, rank=RANK, phase='train').time():
    loss = model(batch).backward()
    optimizer.step()
samples_processed.labels(job_id=JOB, rank=RANK).inc(BATCH_SIZE)
```

Then derived:

```promql
# Samples/sec across the job
sum by (job_id) (rate(train_samples_total[5m]))

# p50 step time per rank
histogram_quantile(0.5,
  sum by (job_id, rank, le) (rate(train_step_seconds_bucket[5m]))
)

# GPU efficiency = real work fraction
avg by (job_id) (DCGM_FI_PROF_SM_ACTIVE
  * on(pod, namespace) group_left(job_id) train_pod_metadata)
```

The classic trio you put on every training dashboard:

1. **Throughput line**: samples/sec over time. Look for steady, no decay.
2. **GPU SM_active line**: should be flat ~85–95% for steady-state training.
3. **Step time histogram heatmap**: `histogram_quantile(_, ...)` — outliers reveal data-loader stalls, checkpoint pauses, GC.

### 3.2 The checkpoint sawtooth

A pattern that surprises people: GPU utilization shows a *sawtooth*. The teeth are checkpoints.

```
SM_active%
   │
100 ┤ ▁██▁██▁██▁     ▁██▁██▁██▁     ▁██▁██▁
 75 ┤ ███████████   ███████████   ███████████
 50 ┤ ███████████   ███████████   ███████████
 25 ┤ ███████████   ███████████   ███████████
  0 ┤            ▔▔▔            ▔▔▔
    └──────────────────────────────────────── time
              ↑               ↑
           checkpoint     checkpoint
```

During checkpoint serialization (PyTorch `torch.save`, DeepSpeed, FSDP shard saves), the GPU is idle while CPU+disk do work. For a 70B model, that's tens of seconds. If checkpoints happen every 100 steps, the GPU loses 5–15% wall-clock time to them.

Detection:

```promql
# Spot checkpoint dips (correlate with checkpoint events from app metric)
DCGM_FI_PROF_SM_ACTIVE < 0.1
  and on(pod) increase(train_checkpoint_seconds_total[1m]) > 0
```

Mitigation isn't observability's job, but the metric you want:

```promql
# Checkpoint time as % of training time (should be <2%)
sum(rate(train_checkpoint_seconds_total[1h]))
  / sum(rate(train_step_seconds_sum[1h])) * 100
```

The fix is async checkpointing (FSDP `--save-async`, DeepSpeed `--zero-stage 3 --aio`) which keeps the GPU running while serialization happens on a separate stream.

### 3.3 Stragglers — the AllReduce killer

In synchronous distributed training, every step ends with an AllReduce: all N GPUs exchange gradients before the next step. Bulk-synchronous: the *slowest* GPU dictates the step time. One GPU at 80% the speed of the others = the whole job runs at 80%.

Per the upstream NCCL/research literature: a single straggler in a 1k-GPU job idles 999 healthy GPUs at every barrier.

Detection — variance across ranks:

```promql
# Straggler signal: max(step_time) / median(step_time) per job
max by (job_id) (
  histogram_quantile(0.5, sum by (job_id, rank, le) (rate(train_step_seconds_bucket[5m])))
)
/
quantile by (job_id) (0.5,
  histogram_quantile(0.5, sum by (job_id, rank, le) (rate(train_step_seconds_bucket[5m])))
)
> 1.10  # alert when worst rank is >10% slower than median
```

Once you've identified the slow rank, cross-reference with `DCGM_FI_DEV_CLOCK_THROTTLE_REASONS` and `DCGM_FI_DEV_GPU_TEMP` for that rank's GPU UUID. Most stragglers are:

1. Thermal throttle (one node has bad airflow)
2. Power throttle (PSU degrading)
3. NVLink errors (one link in the fabric is degraded)
4. PCIe link width drop (`DCGM_FI_DEV_PCIE_LINK_GEN/_WIDTH`)
5. Noisy neighbor on the same host (rare on dedicated training boxes)

Doc 15 expands the per-rank workflow.

### 3.4 Pipeline bubbles

In pipeline parallelism (DeepSpeed pipe, Megatron-LM PP), microbatches flow through stages. The "bubble" is the idle time at pipeline ends:

```
time →
GPU0: [F1][F2][F3][F4]                  [B4][B3][B2][B1]
GPU1:    [F1][F2][F3][F4]            [B4][B3][B2][B1]
GPU2:       [F1][F2][F3][F4]      [B4][B3][B2][B1]
GPU3:          [F1][F2][F3][F4][B4][B3][B2][B1]
       ▲▲▲▲                              ▲▲▲▲▲▲
        bubble (warmup)                  bubble (cooldown)
```

Bubble fraction = `(N_stages - 1) / (N_microbatches + N_stages - 1)`. For a 4-stage pipeline with 8 microbatches, that's 3/11 = 27% idle time on average.

Observability: instrument per-stage forward/backward and emit:

```promql
# Bubble time per stage
sum by (job_id, stage) (rate(train_pipeline_idle_seconds_total[5m]))
  / sum by (job_id, stage) (rate(train_pipeline_busy_seconds_total[5m]))
```

If observed bubble fraction exceeds the theoretical, you have additional stalls (data-loader, comm overhead between stages).

### 3.5 The training health "is this run going to converge" panel

What you put on the screen for an SRE who just got paged:

1. Loss curve (from app metrics)
2. Gradient norm (sanity — exploding or collapsing?)
3. Step time p50 over time (regressing?)
4. Samples/sec (declining?)
5. SM_active per rank (stragglers visible here)
6. ECC error rate per GPU UUID (silent corruption risk)
7. NVLink recovery error rate per link (degrading fabric)
8. Checkpoint cadence (failures cause big roll-backs)

Doc 09 builds this panel as the L4 job view.

---

## 4. Stateless (Inference) Observability

The latency-dominated workload class. Every metric is a percentile.

### 4.1 The latency breakdown

A request flowing through an inference server (Triton, vLLM, TGI) has identifiable phases:

```
┌──────────────────────────────────────────────────────────────┐
│ TOTAL request duration                                       │
│                                                              │
│ ┌──────┐┌──────┐┌────────┐┌────────────────┐┌──────┐┌──────┐ │
│ │queue ││H2D   ││ kernel ││ kernel (decode)││ D2H  ││resp  │ │
│ │      ││copy  ││(prefill)│ (per token loop)│ copy ││      │ │
│ └──────┘└──────┘└────────┘└────────────────┘└──────┘└──────┘ │
└──────────────────────────────────────────────────────────────┘
```

Instrument each phase:

| Phase | Metric |
|-------|--------|
| Queue wait | `inference_queue_duration_seconds` |
| H2D copy | derive from PCIe RX bytes during request |
| Compute (prefill / decode for LLM) | `vllm:request_prefill_time_seconds`, `vllm:request_decode_time_seconds` |
| D2H copy | derive from PCIe TX |
| Total | `inference_request_duration_seconds` |

The dashboard answer to "why is p99 high?" is which phase dominates:

```promql
# Stacked histogram: contribution of each phase
sum by (phase) (
  rate(inference_phase_duration_seconds_sum[5m])
)
/
sum by (phase) (
  rate(inference_phase_duration_seconds_count[5m])
)
```

If queue dominates: under-provisioned (autoscale up).
If compute dominates and SM_active is low: bad batching, model not GPU-saturating.
If H2D dominates: input size or PCIe contention.

### 4.2 Dynamic batching efficiency (Triton)

Triton groups requests into batches dynamically. The efficiency metric:

```promql
# Average batch size = inference count / execution count
rate(nv_inference_request_success[5m])
/
rate(nv_inference_count[5m])
```

That's the actual batch size achieved. Compare to your configured `max_batch_size`:

| Achieved / max | Diagnosis |
|----------------|-----------|
| > 0.8 | Saturated — autoscale up |
| 0.3 – 0.8 | Healthy steady-state |
| < 0.3 | Under-utilized — batch wait time too high or low traffic |

The Triton config `max_queue_delay_microseconds` is the trade-off knob: higher → bigger batches (better GPU util) but worse latency. The dashboard shows the consequence; tuning happens by hand.

### 4.3 LLM-specific (vLLM, TGI)

vLLM exposes a rich Prometheus metric set on `:8000/metrics`:

| Metric | What |
|--------|------|
| `vllm:num_requests_running` | Active requests in batch |
| `vllm:num_requests_waiting` | Queue depth |
| `vllm:kv_cache_usage_perc` | Fraction of KV cache used (0–1) |
| `vllm:time_to_first_token_seconds` | TTFT histogram |
| `vllm:inter_token_latency_seconds` | ITL histogram |
| `vllm:request_prefill_time_seconds` | Prefill phase duration |
| `vllm:request_decode_time_seconds` | Decode phase duration |
| `vllm:prefix_cache_queries` | Total cache lookups |
| `vllm:prefix_cache_hits` | Cache hits |
| `vllm:prompt_tokens_total` | Input tokens processed |
| `vllm:generation_tokens_total` | Output tokens generated |

Key derived signals:

```promql
# Prefix cache hit rate — drives prefill cost
rate(vllm:prefix_cache_hits[5m]) / rate(vllm:prefix_cache_queries[5m])

# Tokens/sec generation throughput
rate(vllm:generation_tokens_total[5m])

# KV cache pressure — close to 1 means evictions and recomputation
max(vllm:kv_cache_usage_perc)
```

Doc 14 is dedicated to LLM serving observability.

### 4.4 Throughput vs latency curves

Inference performance is a curve, not a number. As load increases:

```
latency
  │
  │                              ┌─── cliff
  │                            ┌─┘
  │                         ┌──┘
  │                  ┌──────┘
  │  ──────────┬──────                ◀ knee at load X
  │
  └─────────────────────── throughput
```

The throughput-at-latency-SLO is your real capacity. If your SLO is p99 < 500ms and you can serve 800 RPS at that latency, that's your number. Don't report "max throughput" without the latency it was measured at.

Operationalize by emitting:

```promql
# Throughput at p99 latency SLO
sum(rate(inference_requests_total[5m]))
  unless on()
  histogram_quantile(0.99,
    sum by (le) (rate(inference_request_duration_seconds_bucket[5m])
  ) > 0.5
```

(Reads: total RPS, but blank when p99 > 500ms.)

---

## 5. Interactive Workloads (Jupyter, etc.)

Jupyter notebook GPUs are the easiest to detect as wasted, hardest to reclaim:

### 5.1 Idle detection

```promql
# GPU allocated but no real work for >30 min
avg_over_time(DCGM_FI_PROF_SM_ACTIVE[30m]) < 0.05
  and on(pod, namespace) group_left()
  kube_pod_labels{label_app="jupyterhub"}
```

This says: GPU has been < 5% SM active for 30 minutes, and the owning pod is a Jupyter notebook. Threshold tuning matters — a researcher running cells with thinking time *should* show low utilization between cells. Use 30+ min averaging windows, not 5 min.

The reclaim policy (typically enforced by JupyterHub culler):

| Idle time | Action |
|-----------|--------|
| 30 min | Kernel shutdown (preserves notebook state) |
| 2 h | Pod termination (loses GPU) |
| 24 h | Persistent volume cleanup |

Each transition needs a metric so users can see "you have 22 minutes before your kernel is shut down":

```promql
notebook_idle_seconds_remaining{user="alice"}
```

### 5.2 Cost attribution

Notebook GPU hours dominate "wasted GPU" budgets at most companies. Doc 12 covers chargeback; the notebook contribution is:

```promql
# Hours of GPU allocated to idle notebooks per team last week
sum by (team) (
  (DCGM_FI_PROF_SM_ACTIVE < 0.05)
  * on(pod, namespace) group_left(label_team)
  kube_pod_labels{label_app="jupyterhub"}
) * 3600
```

---

## 6. Common Patterns Across Classes

A few signals are universal:

| Signal | Training | Inference | Notebook |
|--------|----------|-----------|----------|
| GPU OOM | Job crashes | Request errors, possibly cascade | Kernel dies |
| NCCL timeout | Hang then crash | N/A (rare) | N/A |
| Driver hang | Hang | Pod restart loop | Kernel hang |
| Thermal throttle | Stragglers / slow | p99 latency rise | Cell takes longer |

All are visible in the hardware metrics from doc 02; the *consequence* is in the application metrics from this doc. Effective dashboards plot both side-by-side so on-call can see the cause.

---

## 7. The Cross-Layer Join

The pattern that recurs in every workload-aware query:

```promql
# Join hardware metric (DCGM) to workload identity (k8s)
DCGM_FI_PROF_SM_ACTIVE
  * on(pod, namespace) group_left(label_workload_class, label_team)
  kube_pod_labels
```

Then per-class views fall out:

```promql
# Average GPU utilization by workload class
avg by (label_workload_class) (
  DCGM_FI_PROF_SM_ACTIVE
  * on(pod, namespace) group_left(label_workload_class)
  kube_pod_labels
)
```

This gives you the dashboard that separates the steady 90% bar (training) from the bursty 30% (inference) from the flat 3% (notebooks) — and that single chart drives more capacity decisions than any other.

---

## 8. Forward Pointers

- **Doc 05** quantifies "GPU waste" using the per-class baselines from §2
- **Doc 09** builds the L4 job-view dashboard from these signals
- **Doc 10** routes alerts: training SLO → ML team, inference SLO → service team
- **Doc 14** dives into LLM serving (vLLM, TGI, Triton+TensorRT-LLM)
- **Doc 15** dives into distributed training stragglers and NCCL

---

## Sources

- [vLLM Production Metrics](https://docs.vllm.ai/en/stable/usage/metrics/)
- [vLLM v1 Metrics Design](https://docs.vllm.ai/en/v0.8.5/design/v1/metrics.html)
- [Triton Inference Server — Metrics](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/metrics.html)
- [Triton — Dynamic Batchers](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/batcher.html)
- [PyTorch Profiler](https://docs.pytorch.org/docs/stable/profiler.html)
- [NVIDIA — NCCL 2.27 resilient training (2024)](https://developer.nvidia.com/blog/enabling-fast-inference-and-resilient-training-with-nccl-2-27/)
- [Google Cloud — Stragglers in AI: automated detection](https://cloud.google.com/blog/products/compute/stragglers-in-ai-a-guide-to-automated-straggler-detection)
- [arxiv:2505.23523 — Accelerating AllReduce with a Persistent Straggler](https://arxiv.org/html/2505.23523v1)
- [Red Hat — Triage vLLM performance (March 2026)](https://developers.redhat.com/articles/2026/03/09/5-steps-triage-vllm-performance)
- [Glukhov — Monitor LLM inference in production (2026)](https://www.glukhov.org/observability/monitoring-llm-inference-prometheus-grafana/)
