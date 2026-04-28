# 15 — Distributed Training Observability — Special Topic

> A 1000-GPU training run is a distributed system that tolerates exactly zero stragglers. One slow GPU drags 999 healthy ones to its speed at every AllReduce barrier. This doc covers the metrics that surface stragglers, communication health, and recovery — the signals that decide whether a 3-week pretraining run finishes or melts down at week 2.

The economics: a 1000-H100 run costs ~$200/hour just for compute. A 10% slowdown for 1 day is $480 of waste per percentage point. Detecting stragglers is high-leverage observability.

---

## 1. The Distributed Training Stack

Training jobs at scale combine multiple parallelism strategies. Each has its own observability surface:

| Parallelism | What's distributed | Comm pattern | Bottleneck signal |
|-------------|-------------------|--------------|-------------------|
| **Data parallel (DP)** | Data batches | AllReduce on gradients | NVLink + IB during AllReduce |
| **Tensor parallel (TP)** | Layer matmul splits | AllReduce + AllGather per layer | NVLink (intra-node) |
| **Pipeline parallel (PP)** | Layer ranges | Send/Recv between stages | Stage timing imbalance → bubble |
| **FSDP** | Param shards | Gather params on demand, ReduceScatter grads | Comm overlap with compute |
| **Context parallel (CP)** | Sequence dim | AllGather of activations | NVLink |
| **Expert parallel (EP)** | MoE experts | All-to-all routing | IB (cross-node MoE) |

A modern frontier-model run uses all six (Megatron's "5D + EP"). Each parallelism dimension is a potential straggler axis.

---

## 2. NCCL — the Library That Carries the Comm

NCCL (NVIDIA Collective Communications Library) implements the collective ops. Production observability lives in three places:

1. **NCCL_DEBUG=INFO logs** — verbose per-op timing
2. **`/proc/<pid>/fd/`** + NCCL communicator state — deep diagnostics
3. **Custom instrumentation in the framework** — emit per-rank timing to Prometheus

### 2.1 Capturing NCCL timing

```bash
NCCL_DEBUG=INFO
NCCL_DEBUG_FILE=/var/log/nccl-rank-%h-%p.log
NCCL_DEBUG_SUBSYS=COLL,P2P,NET,INIT
```

Output lines like:

```
NCCL INFO ## AllReduce dur 4.23ms count 1048576 datatype float16
```

A small log-parser sidecar (one per pod) emits:

```promql
# Histogram of AllReduce duration per rank
nccl_collective_duration_seconds_bucket{
  rank="3",
  op="AllReduce",
  size_bucket="1MB"
}
```

Cross-rank variance on this metric is the straggler signal.

### 2.2 NCCL 2.27+ resilient collectives

NCCL 2.27 added the ability to recover from a single-link failure during a collective without aborting the whole job. The metric:

```promql
nccl_link_recovery_total
```

Each non-zero increment is a fabric event recovered from. Trending up = fabric degrading.

---

## 3. Stragglers — the Killer

### 3.1 Detection by step-time variance

The straightforward approach (also doc 04 §3.3):

```promql
# Slowest rank vs median
max by (job_id) (
  histogram_quantile(0.5, sum by (job_id, rank, le) (rate(train_step_seconds_bucket[5m])))
)
/
quantile by (job_id) (0.5,
  histogram_quantile(0.5, sum by (job_id, rank, le) (rate(train_step_seconds_bucket[5m])))
)
> 1.10
```

If the slowest rank is > 10% slower than the median, you have a straggler.

### 3.2 Persistent vs transient

A persistent straggler (same rank slow every step) is hardware. A transient straggler (different rank slow each step) is data-loader, network jitter, or shared resource contention.

```promql
# Persistent: count of consecutive 5m windows where rank N was slowest
count_over_time(
  (
    histogram_quantile(0.5, sum by (rank, le) (rate(train_step_seconds_bucket[5m])))
    == on(job_id) max by (job_id) (
        histogram_quantile(0.5, sum by (job_id, rank, le) (rate(train_step_seconds_bucket[5m])))
      )
  )[1h:5m]
) > 6   # slowest in 6+ of last 12 windows → persistent
```

Persistent stragglers route to the platform team (drain the GPU, RMA workflow). Transient ones to the workload team (data pipeline, etc.).

### 3.3 The expensive part of a straggler

Per the StragglAR research and NCCL benchmarks, in synchronous training:

- Step time = max(per-rank compute time) + AllReduce time
- AllReduce is dominated by the slowest rank reaching the barrier
- A 10% slow rank → 10% slowdown of *every* step

For a 1000-rank, 100-day pretraining run: a 10% slowdown for the entire run is 10 lost days = ~$48,000 (conservative). A persistent straggler not detected for 1 day costs $480.

The detection alert in doc 10 §5 fires at 10m of `for:` — not 30m. Cheap to be wrong (false positive: page on-call who looks once); expensive to be right too late.

---

## 4. Pipeline Bubble Visibility

For pipeline parallelism (Megatron-PP, DeepSpeed pipe), the bubble is unavoidable:

```
   t=0    t=1    t=2    t=3
PP0 [F0]  [F1]  [F2]  [F3]  ...  [B3]  [B2]  [B1]  [B0]
PP1       [F0]  [F1]  [F2]      [B3]  [B2]  [B1]  [B0]
PP2             [F0]  [F1]      [B3]  [B2]  [B1]  [B0]
PP3                   [F0]      [B3]  [B2]  [B1]  [B0]
    ▲▲▲                                                ▲▲▲
    bubble                                             bubble
```

Theoretical bubble fraction = `(N_stages - 1) / (N_microbatches + N_stages - 1)`.

Instrumentation:

```python
# In each pipeline stage's training loop
with prometheus.Histogram('pipeline_stage_seconds', ...) \
        .labels(stage=STAGE_ID, phase='forward').time():
    pipeline.forward()
```

Then:

```promql
# Bubble = 1 - (busy time / wall time) per stage
1 - (sum by (job_id, stage) (rate(pipeline_stage_seconds_sum[5m]))
   / sum by (job_id, stage) (rate(train_step_seconds_sum[5m])))
```

Compare observed to theoretical. Excess is *additional* idle from data-loader stalls, network, or interleaving issues.

### 4.1 1F1B vs interleaved schedules

Megatron's interleaved 1F1B reduces bubble. The metric to verify it's working:

```promql
# Number of microbatches per step
job_id_micro_batches_per_step{job_id="..."}
```

If you upgraded to interleaved scheduling and the bubble fraction didn't drop, the schedule isn't actually engaged.

---

## 5. Tensor Parallelism Load Imbalance

TP requires balanced work across ranks. Imbalance shows up as:

```promql
# Variance in SM_active across TP ranks within the same pod
stddev by (pod) (
  DCGM_FI_PROF_SM_ACTIVE
  * on(pod, namespace) group_left() kube_pod_labels{label_workload="tp-training"}
) > 0.05
```

If variance > 5%, one TP rank is consistently faster/slower. Causes:

- Asymmetric layer sharding (uneven head counts in MHA)
- One GPU thermal-throttled
- NVLink degradation on one of the GPUs (doc 06 §4)

The fix is symmetry: either repartition the model or replace the slow GPU.

---

## 6. Gradient Norm — a Health Signal

Not a perf metric — a *correctness* metric. Exploding gradients ruin training:

```python
total_norm = torch.norm(torch.stack([
    p.grad.norm(2) for p in model.parameters() if p.grad is not None
]))
prom_grad_norm.set(total_norm.item())
```

```promql
# Plot gradient norm over time
train_grad_norm{job_id="..."}

# Alert: gradient norm exploded (10× moving average)
train_grad_norm > 10 * avg_over_time(train_grad_norm[1h])
```

If gradient norm explodes, the next step's loss likely diverges. Catching this early = saving the run.

Also: gradient norm collapsing to near-zero → training stalled (vanishing gradients, learning rate decayed too far). Different alert, same panel.

---

## 7. Checkpoint I/O

Checkpoint serialization is the second-biggest non-compute bottleneck after AllReduce.

### 7.1 Time spent

```promql
# Total checkpoint time / total step time
sum(rate(train_checkpoint_seconds_total[1h]))
/
sum(rate(train_step_seconds_sum[1h])) > 0.05   # > 5% of training time
```

If checkpoints are > 5% of training time, switch to async checkpointing (FSDP `--save-async`) or sharded checkpoint (DeepSpeed ZeRO-3).

### 7.2 Storage backend health

```promql
# Storage write throughput during checkpoint
rate(checkpoint_bytes_written_total[1m])

# Underlying disk/object-store latency
filesystem_write_latency_seconds  # or s3 PUT latency
```

A common finding: the bottleneck isn't GPU side, it's the shared filesystem (NFS, Lustre) or object store. Different team, different fix.

### 7.3 Checkpoint frequency

Too frequent = wasted GPU time. Too infrequent = larger work loss on failure.

```
optimal interval ≈ sqrt(2 × failure_MTBF × checkpoint_cost)
```

For a 1000-GPU run with MTBF 4h and checkpoint cost 60s: ~5 min between checkpoints. The metric:

```promql
# Average time between checkpoints
1 / rate(train_checkpoint_total[1h])
```

---

## 8. Failure Recovery

When a node fails mid-run, recovery time = restart latency + checkpoint reload + scale-up wait. Each is observable.

```promql
# Job restart count
sum by (job_id) (changes(train_run_id[1h]))

# Time between failure and recovery (last failure to first new step)
job_recovery_seconds{job_id="..."}

# Checkpoint age at failure (how much progress was lost)
job_checkpoint_age_at_failure_seconds{job_id="..."}
```

If checkpoint age at failure averages 2 hours, you're losing ~2 GPU-hours per node failure × N nodes per failure event. Multiply by failures per week → annual cost of insufficient checkpoint frequency.

---

## 9. RDMA / InfiniBand During AllReduce

For multi-node training, the IB fabric carries every gradient AllReduce. Doc 06 covered IB monitoring; the distributed-training-specific signal:

```promql
# IB throughput during a known AllReduce window
rate(node_infiniband_port_data_transmitted_bytes_total[1m])
  * on(Hostname) group_left() (
      rate(nccl_collective_duration_seconds_count{op="AllReduce"}[1m]) > 0
  )
```

Healthy AllReduce: each port at 80–95% of NDR line rate (~50 GB/s on 4-rail H100 nodes), all ranks symmetric.

Pathological:
- IB at 30% during AllReduce → topology problem (rail not used) or CPU-side bottleneck
- Asymmetric per-rank IB → one node's NIC degraded; investigate

---

## 10. PyTorch Distributed Tracing Hooks

PyTorch 2.x exposes hooks into `torch.distributed` collective calls:

```python
import torch.distributed as dist

# Wrap NCCL timer
def hook(state, bucket):
    start = time.time()
    fut = dist.all_reduce(bucket.buffer(), async_op=True).get_future()
    def cb(f):
        prom_allreduce_duration.observe(time.time() - start)
        return f
    return fut.then(cb)
model.register_comm_hook(state, hook)
```

This emits per-AllReduce timings to Prometheus from the framework, without parsing NCCL logs. Latency overhead < 1µs per call.

---

## 11. The "Is This Run Going to Finish On Time?" Panel

The composite panel for the run owner — what the on-call ML engineer reads at 3 AM:

| Panel | Indicator |
|-------|-----------|
| Loss curve | Curving down? Catastrophic spike? |
| Gradient norm | In healthy range? |
| Step time p50 over time | Stable or regressing? |
| Step time variance across ranks | Stragglers? |
| ETA to completion | Live calculation |
| Checkpoints in last hour | Frequency healthy? |
| ECC error rate (any GPU in job) | Hardware degrading mid-run? |
| NVLink recovery rate | Fabric degrading? |

ETA calculation:

```promql
# Steps remaining × current step time
(planned_steps - current_step) *
histogram_quantile(0.5, sum by (le) (rate(train_step_seconds_bucket[5m])))
```

Plotted alongside the planned end time, the ETA panel is the one a workload owner stares at.

---

## 12. Distributed Training Alert Suite

Beyond doc 10:

```yaml
- alert: Train_Straggler_Persistent
  expr: |
    count_over_time(
      (max by (job_id, rank) (rate(train_step_seconds_bucket[5m]))
       == on(job_id) max by (job_id) (rate(train_step_seconds_bucket[5m])))
    [30m:5m]) > 4
  for: 5m
  labels: { severity: warning }

- alert: Train_GradientExplosion
  expr: train_grad_norm > 10 * avg_over_time(train_grad_norm[1h])
  for: 0s
  labels: { severity: critical }

- alert: Train_Bubble_Excess
  expr: |
    1 - (sum by (job_id, stage) (rate(pipeline_stage_seconds_sum[5m]))
       / sum by (job_id, stage) (rate(train_step_seconds_sum[5m])))
    > 1.5 * job_id_theoretical_bubble
  for: 30m
  labels: { severity: warning }

- alert: Train_NoCheckpoints
  expr: |
    time() - max(train_checkpoint_timestamp_seconds) > 30 * 60   # 30 min since last
  for: 5m
  labels: { severity: warning }

- alert: Train_RankFailed
  expr: |
    count by (job_id) (
      kube_pod_status_phase{phase="Running"}
      * on(pod, namespace) group_left(job_id) kube_pod_labels
    ) < expected_rank_count
  for: 1m
  labels: { severity: critical }
```

---

## 13. Parallelism-Specific Dashboards

The L4 training dashboard (doc 09 §5.1) becomes a tabbed view:

- **DP tab**: AllReduce timing per rank, gradient sync latency
- **TP tab**: Per-rank SM_active variance, NVLink usage
- **PP tab**: Per-stage forward/backward time, bubble fraction
- **FSDP tab**: Communication overlap, gather/reduce-scatter timing
- **EP tab** (MoE): All-to-all routing latency, expert load distribution

Each tab is a curated view, but template-variable driven so the same dashboard works for any job.

---

## 14. Acceptance Checklist

- [ ] Per-rank `train_step_seconds` emitted from training framework
- [ ] NCCL log parser sidecar deployed for all distributed jobs
- [ ] Per-rank straggler alert defined and tested
- [ ] Gradient norm metric emitted with explosion alert
- [ ] Checkpoint timing metrics emitted
- [ ] Pipeline bubble fraction calculated for PP jobs
- [ ] IB throughput correlated to AllReduce windows
- [ ] L4 training dashboard with parallelism tabs
- [ ] ETA panel implemented for in-flight runs

---

## 15. Final Forward Pointer

This is the last document. The other 14 documents covered the substrate; this one covered the workload class with the highest stakes. Together they are the production observability stack.

Next steps for a team that has implemented this:
1. Run for a quarter
2. Triage the alerts that fired and either delete (false positive) or improve runbooks
3. Re-baseline cardinality and capacity targets
4. Quarterly review of efficiency targets (doc 5 §11)
5. Iterate

The data is the asset. The dashboards and alerts are how you turn it into action.

---

## Sources

- [NCCL — NVIDIA documentation](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/index.html)
- [NCCL 2.27 resilient training (NVIDIA blog)](https://developer.nvidia.com/blog/enabling-fast-inference-and-resilient-training-with-nccl-2-27/)
- [arXiv:2505.23523 — Accelerating AllReduce with a Persistent Straggler](https://arxiv.org/html/2505.23523v1)
- [Google Cloud — Stragglers in AI: automated detection](https://cloud.google.com/blog/products/compute/stragglers-in-ai-a-guide-to-automated-straggler-detection)
- [PyTorch FSDP](https://docs.pytorch.org/docs/stable/fsdp.html)
- [Megatron-FSDP / 5D parallelism](https://docs.nvidia.com/megatron-core/developer-guide/latest/api-guide/custom_fsdp.html)
- [PyTorch — torch.distributed comm hooks](https://docs.pytorch.org/docs/stable/distributed.html)
- [Nebius — Building reliable clusters for distributed AI](https://nebius.com/blog/posts/how-we-build-reliable-clusters)
