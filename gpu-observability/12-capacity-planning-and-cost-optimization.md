# 12 — Capacity Planning & Cost Optimization

> The hardest fleet decision: how many GPUs to buy, what models, where to put them, and when. Your observability stack already has the data — this doc connects it to procurement, sizing, and the chargeback model that gives the data to leadership.

The 2026 macro context matters: GPU supply is constrained, spot pricing is unreliable, and reserved capacity needs months of lead time. Capacity planning failures show up as either *stranded spend* (unused reservations) or *lost throughput* (couldn't get GPUs when needed). Both are observability-derived.

---

## 1. The Three Capacity-Planning Questions

Every quarter, you answer three questions:

1. **Are we using what we have?** (utilization vs allocation, doc 5 — but trended over months)
2. **Is demand growing or shrinking?** (forecasting from historical demand metrics)
3. **What model mix should the next batch be?** (workload profiles → SKU recommendations)

Each question is backed by specific Prometheus rollup queries.

---

## 2. Demand Forecasting

The metric you forecast is `requested_gpus_over_time`. If you forecast `allocatable` you're forecasting your own purchases (chicken-and-egg).

```promql
# Daily peak GPU requests, smoothed
max_over_time(
  sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"})[1d:1m]
)
```

Stored at 1h rollup, you have months of history. Forecast methods:

| Method | When | Accuracy |
|--------|------|----------|
| **Linear regression on weekly peaks** | Steady, growing org | ±15% |
| **Seasonal ARIMA** | Detectable weekly/monthly cycles | ±10% |
| **Prophet / forecast libraries** | Complex seasonality | ±10% |
| **Engineering planning input** | Major model launch coming | bounded by plan |

Most orgs do linear-regression-with-engineering-override: the model says "+30% in 6 months," eng team confirms a major training run starts in month 4 (so the curve should bend up sooner), procurement gets the adjusted number.

The output: **target capacity by quarter**, broken by GPU model.

### 2.1 Demand vs supply chart

The single most useful capacity-planning chart:

```
GPUs
  │
  │            ┌─── reserved capacity ceiling
  │           ╱
  │          ╱  ◀──── on-demand burst region
  │   ┌────/
  │   │
  │  ─┘    ←── actual peak demand (90th percentile of rolling daily peak)
  │
  │     ←── steady-state demand (mean)
  └──────────────────────────────── time
```

Two lines: rolling daily peak (90th percentile of last 30d), and reserved capacity ceiling. The gap is what spot/on-demand absorbs. When the demand line crosses the ceiling, you're running on (expensive) on-demand or queuing pods.

---

## 3. Right-Sizing: Workload Profile → SKU

For each major workload class, the historical profile drives the SKU selection.

### 3.1 Profile a workload class

```promql
# 30-day p95 SM_active for training jobs
quantile_over_time(0.95,
  avg by (label_workload_class) (
    DCGM_FI_PROF_SM_ACTIVE
    * on(pod, namespace) group_left(label_workload_class) kube_pod_labels
  )[30d:1h]
)

# Tensor activity p95 — are tensor cores being used?
quantile_over_time(0.95,
  avg by (label_workload_class) (DCGM_FI_PROF_PIPE_TENSOR_ACTIVE * ...)[30d:1h]
)

# HBM peak usage
max_over_time(
  max by (label_workload_class) (DCGM_FI_DEV_FB_USED * ...)[30d:1h]
)
```

### 3.2 The recommendation matrix

| Workload p95 SM_active | Tensor p95 | HBM peak | Recommendation |
|-----------------------|-----------|----------|----------------|
| > 80% | > 50% | > 60GB | H100/H200 (compute-saturated) |
| > 80% | > 50% | < 30GB | MIG 3g/4g slice (still compute, less memory) |
| > 60% | > 30% | > 60GB | H100/A100 |
| < 50% | any | > 30GB | Memory-bound — could run on lower-tier GPU |
| < 30% | any | < 16GB | L40S or smaller |
| Bursty | any | varies | Inference-class, MIG slicing |

These translate to procurement specs. The driving question: "we're buying 200 more GPUs next quarter — what mix?"

### 3.3 The opposite question: should we *return* GPUs?

Long-running workloads holding GPUs at < 20% util for months should consolidate to fewer, smaller GPUs. The metric:

```promql
# Workloads that have averaged < 20% SM_active over 30d
avg_over_time(
  avg by (namespace, label_workload_class) (
    DCGM_FI_PROF_SM_ACTIVE * ...
  )[30d:1h]
) < 0.20
```

The output of this query is a quarterly conversation with workload owners.

---

## 4. Workload Classification: Bursty vs Steady-State

Different patterns warrant different capacity strategies.

### 4.1 Steady-state workloads

Inference services running 24/7. Demand has a daily cycle but a stable floor.

- **Capacity model**: reserve capacity to cover floor + burst headroom
- **Provisioning**: reserved instances or owned hardware
- **Observability**: 95th percentile of rolling 1h demand

### 4.2 Bursty workloads

Training jobs that consume 1000 GPUs for 3 weeks then 0 for a month.

- **Capacity model**: pooled, with priority queuing
- **Provisioning**: shared pool, possibly with spot for opportunistic capacity
- **Observability**: queue depth + scheduling latency, doc 03

### 4.3 The classifier query

```promql
# Coefficient of variation of demand by namespace
stddev_over_time(sum by (namespace) (
  kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
)[7d:1h])
/
avg_over_time(sum by (namespace) (
  kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
)[7d:1h])
```

CoV < 0.1 → steady-state, reserve capacity.
CoV > 1.0 → bursty, queue.
CoV in between → hybrid pool.

---

## 5. Spot / Preemptible Instance Observability

Spot instances are 60–90% cheaper but evictable. Your observability needs to surface:

| Metric | Purpose |
|--------|---------|
| `spot_eviction_total{instance_type, az}` | How often is each spot pool evicting |
| `spot_lifetime_seconds_bucket` | Distribution of how long spot instances last |
| `workload_preemption_total{namespace}` | How often workloads got killed |
| `spot_savings_dollars_total` | Cost saved vs on-demand |

```promql
# Average spot lifetime by AZ — informs whether to use that AZ
histogram_quantile(0.5,
  sum by (az, le) (rate(spot_lifetime_seconds_bucket[24h]))
)

# Eviction rate per pool
rate(spot_eviction_total[24h])
```

If average spot lifetime is < 1 hour in a particular AZ, that capacity is not usable for training jobs (too short to make progress). Steer training to a more stable AZ; reserve spot for inference auto-scaling burst.

The shift in 2026: spot pricing has become volatile and instances are often unavailable at peak. Don't plan for spot as primary capacity. Use reserved as the floor; spot only opportunistically.

---

## 6. MIG Strategy Driven by Data

Doc 5 §9.2 introduced the MIG sizing question. Make this an automated quarterly review:

```promql
# Distribution of HBM usage on whole GPUs (non-MIG'd)
histogram_quantile(0.5, sum by (le) (
  rate(DCGM_FI_DEV_FB_USED_bucket{GPU_I_ID=""}[7d])
))
histogram_quantile(0.95, sum by (le) (...))
```

If on whole GPUs, p95 HBM use is 30GB out of 80GB, MIG those GPUs into 3g.40gb slices. Observed utilization profiles drive the partitioning decision, not gut feel.

### 6.1 The MIG sizing decision tree

```
For each workload class:
  if peak HBM < 10GB and peak SM_active < 30%:
    → 1g.10gb slice
  if peak HBM < 20GB and peak SM_active < 60%:
    → 2g.20gb slice
  if peak HBM < 40GB and peak SM_active < 80%:
    → 3g.40gb slice
  else:
    → whole GPU
```

The output is a per-workload-class MIG profile. Apply via the GPU operator's MIG strategy.

### 6.2 Cross-workload MIG conflicts

A node has one MIG configuration at a time. If two workload classes need different profiles, you split nodes into pools, OR you choose the more permissive profile and accept some inefficiency.

The metric to watch:

```promql
# Pods unable to schedule because of MIG profile mismatch
count(
  kube_pod_status_phase{phase="Pending"}
  * on(pod, namespace) group_left(label_required_mig_profile) kube_pod_labels
)
```

If this is consistently > 0 in a particular MIG profile dimension, dedicate a node pool.

---

## 7. Cross-Cluster Bin-Packing

When you have multiple GPU clusters, work flows where capacity is. Observe and steer:

```promql
# Cross-cluster utilization comparison
avg by (cluster) (cluster:gpu_sm_active:avg1m)

# Cluster with most pending pods (i.e., least free)
sum by (cluster) (
  kube_pod_status_phase{phase="Pending"}
  * on(pod, namespace) (sum by (pod, namespace) (
      kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
    ) > 0)
)
```

Operationalize as: a cluster-router that, on submission, picks the cluster with the most slack and schedules there. Doc 13 covers multi-tenant policy.

---

## 8. Showback and Chargeback Reports

Doc 5 §7 introduced the model. The capacity-planning view rolls up monthly:

```promql
# Per-team monthly GPU spend
sum by (label_team) (
  sum_over_time(team:gpu_chargeback_dollars[30d])
)

# Per-team monthly waste
sum by (label_team) (
  sum_over_time(team:gpu_waste_dollars[30d])
)

# Efficient teams (waste < 20% of spend)
sum by (label_team) (sum_over_time(team:gpu_waste_dollars[30d]))
/
sum by (label_team) (sum_over_time(team:gpu_chargeback_dollars[30d]))
< 0.2
```

The dashboard sent to leadership monthly:

| Team | Monthly GPU $ | Wasted $ | Waste % | Trend (Mo/Mo) |
|------|--------------|----------|---------|---------------|
| llm-platform | $1.2M | $80k | 6.7% | -2% |
| recsys | $400k | $200k | 50% | +5% |
| research | $200k | $150k | 75% | +20% |

The "research" line in this synthetic example is the conversation: research teams have low utilization by design (experimentation), but should still trend toward less waste. The data starts the conversation.

---

## 9. Long-Term Capacity Trend Analysis

Two charts that drive the annual budget cycle:

### 9.1 Demand-supply gap

```promql
# Daily peak demand vs allocatable
max_over_time(
  sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"})[1d:5m]
)
sum(kube_node_status_allocatable{resource="nvidia_com_gpu"})
```

Plot as two lines over 12+ months. The gap is your headroom (or deficit when demand exceeded supply).

### 9.2 Efficiency trend

```promql
# Cluster-wide mean SM_active over time
avg_over_time(cluster:gpu_sm_active:avg1m[7d])
```

Over a year, plotted weekly. If trending up: bin-packing and idle reclaim are working. If flat or down: investigation needed.

---

## 10. Preemption and Priority Queues

For shared pools, priority preemption is real. The visibility you need:

```promql
# Preemption rate — how often is high-priority work kicking out lower-priority
rate(kube_pod_status_terminated_reason{reason="Preempted"}[1h])

# Per-team preemption — research jobs preempted by training jobs?
sum by (preempted_team) (rate(kube_pod_status_terminated_reason{reason="Preempted"}[1h]))
```

GFS-style preemption-aware schedulers (per recent research) reduce eviction rate by ~33% by anticipating preemption. Without that, you need observability to surface the cost of preemption to teams whose work gets killed.

---

## 11. RMA and Hardware Lifecycle Capacity

Capacity planning must account for hardware coming and going:

```promql
# GPUs currently in RMA (unavailable capacity)
count(gpu_lifecycle_state{state="in_rma"})

# Annualized RMA rate by SKU
sum by (gpu_product) (
  increase(gpu_lifecycle_transitions_total{from="in_service", to="in_rma"}[365d])
)
```

For an H100 fleet with a 1% annual RMA rate, you need 1% buffer capacity to absorb hardware out of service. Plan procurement to maintain effective capacity, not nameplate capacity.

---

## 12. The Annual Procurement Memo (the document this dashboard produces)

Once a year, the data above produces a memo with:

1. **Current state**: fleet size, mean utilization, mean allocation, RMA rate
2. **Demand forecast**: projected GPU-hours over next 4 quarters with confidence interval
3. **Mix recommendation**: # H100, # H200, # B200, # L40S based on workload profiles
4. **Reservation strategy**: what fraction reserved 1y, 3y, on-demand, spot
5. **Capacity buffer**: % over forecast for safety + RMA pool
6. **Risks**: spot unreliability, supply chain, model mix uncertainty

Every number in this memo cites a specific recording rule from this doc. If a number doesn't have a citation, it's a guess and should be flagged.

---

## 13. Acceptance Checklist

- [ ] 1h rollups of demand exist for ≥ 90 days
- [ ] Per-team monthly cost rollups in Prometheus or LTS
- [ ] Workload classification (steady vs bursty) for top 10 namespaces
- [ ] Quarterly MIG sizing review query is automated
- [ ] Spot eviction and lifetime metrics scraped (if using spot)
- [ ] Cross-cluster utilization comparison dashboard exists
- [ ] Procurement memo template references metric queries directly
- [ ] RMA lifecycle metrics tracked

---

## 14. Forward Pointers

- **Doc 13**: per-tenant capacity, isolation, and quota observability
- **Doc 14**: LLM serving — KV cache sizing as a capacity driver
- **Doc 15**: training — checkpoint storage as a capacity dimension

---

## Sources

- [Compute Exchange — Reserved vs On-Demand GPU in 2026](https://compute.exchange/blogs/reserved-vs.-on-demand-gpu-in-2026)
- [Spheron — GPU Shortage 2026](https://www.spheron.network/blog/gpu-shortage-2026/)
- [arXiv — GFS: Preemption-aware Scheduling Framework for GPU Clusters](https://arxiv.org/pdf/2509.11134)
- [Introl — Spot Instances and Preemptible GPUs](https://introl.com/blog/spot-instances-preemptible-gpus-ai-cost-savings)
- [NStarX — GPU Capacity Planning & Cost Control](https://nstarxinc.com/blog/gpu-capacity-planning-cost-control-avoiding-stranded-spend-and-failed-reservations/)
- [Mavvrik — On-prem GPU chargeback strategies](https://www.mavvrik.ai/on-premises-gpu-chargeback-strategies-challenges-and-kubernetes/)
- [Introl — AI Infrastructure Capacity Planning Forecasting](https://introl.com/blog/ai-infrastructure-capacity-planning-forecasting-gpu-2025-2030)
