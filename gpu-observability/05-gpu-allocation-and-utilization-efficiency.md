# 05 — GPU Allocation & Utilization Efficiency

> The single dashboard your CFO will ask for. The math that turns "we have 1000 GPUs" into "we are wasting $4M/year and here's exactly which team is responsible."

Industry baseline as of 2026: average GPU utilization on Kubernetes sits at **30–40%**; some surveys put the cluster-wide average as low as **5%**. The difference between you and the cost-effective version of you is the metrics in this doc.

---

## 1. Two Different Numbers, Two Different Problems

GPUs are wasted in *two distinct ways*. Don't conflate them.

| Concept | Definition | Lever to fix |
|---------|-----------|--------------|
| **Allocation efficiency** | (GPUs allocated to pods) / (GPUs allocatable in cluster) | Bin-packing, scheduler, autoscaler tuning |
| **Utilization efficiency** | (Real GPU work) / (GPU time allocated to pods) | Workload tuning, batch sizing, MIG vs whole-GPU |

A cluster at 95% allocation and 20% utilization is *fully booked but barely working* — the workloads themselves aren't using their GPUs. Bin-packing won't help; you need workload owners to pack denser or scale down.

A cluster at 50% allocation and 90% utilization is *under-booked but the booked GPUs are cooking* — the scheduler isn't filling nodes well, or quota isn't being claimed. Workload tuning won't help; capacity policy will.

The first chart you build is the **2D heatmap** of allocation × utilization across teams, namespaces, and clusters. Outliers in each quadrant get a different intervention.

---

## 2. Allocation Efficiency Metrics

### 2.1 Cluster-wide

```promql
# Cluster GPU allocation %
sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"})
/
sum(kube_node_status_allocatable{resource="nvidia_com_gpu"})
```

### 2.2 Per node

```promql
# Per-node allocation
sum by (node) (
  kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
  * on(pod, namespace) group_left(node)
  kube_pod_info
)
/
sum by (node) (kube_node_status_allocatable{resource="nvidia_com_gpu"})
```

Sort descending. The bottom of this list is your bin-packing problem: nodes with 1 of 8 GPUs allocated, 7 idle, the cluster paying for the whole node.

### 2.3 Per team / namespace

```promql
sum by (namespace) (
  kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
)
/
on() group_left() sum(kube_node_status_allocatable{resource="nvidia_com_gpu"})
```

This is the "share of cluster allocated" view. Sum to 1.0 across all namespaces.

### 2.4 Quota usage

```promql
# Namespace using its quota
sum by (namespace) (
  kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
)
/
sum by (namespace) (
  kube_resourcequota{resource="requests.nvidia.com/gpu", type="hard"}
)
```

Teams sitting at 100% are blocked; teams sitting at 30% are over-quota'd and can release capacity. Both are conversations the platform team should have.

---

## 3. Utilization Efficiency Metrics

The hard one. There's no single number; you have to pick a definition and stick to it.

### 3.1 SM Active Ratio (the workhorse)

```promql
# Per pod
avg_over_time(DCGM_FI_PROF_SM_ACTIVE[5m])

# Per namespace
avg by (namespace) (
  avg_over_time(DCGM_FI_PROF_SM_ACTIVE[5m])
  * on(pod, namespace) group_left() kube_pod_info
)

# Cluster-wide
avg(avg_over_time(DCGM_FI_PROF_SM_ACTIVE[5m]))
```

This is your default "GPU efficiency" number. Healthy ranges by workload class (doc 04):

| Class | Healthy SM_active | Concerning |
|-------|------------------|------------|
| Training (steady-state) | 80–95% | < 60% |
| Inference (under load) | 30–80% (bursty) | < 10% sustained |
| Notebooks | < 20% expected | > 30% sustained means user has a real workload, fine |

### 3.2 The Three-Component Score

A more honest signal — averages all three layers:

```promql
gpu_efficiency = (
  DCGM_FI_PROF_SM_ACTIVE         # are SMs busy?
  * DCGM_FI_PROF_SM_OCCUPANCY    # are they full?
  * (DCGM_FI_PROF_PIPE_TENSOR_ACTIVE > 0.05)  # using tensor cores?
)
```

Encoded as a recording rule:

```yaml
- record: gpu:efficiency:5m
  expr: |
    (
      avg_over_time(DCGM_FI_PROF_SM_ACTIVE[5m])
      * avg_over_time(DCGM_FI_PROF_SM_OCCUPANCY[5m])
    )
    * (avg_over_time(DCGM_FI_PROF_PIPE_TENSOR_ACTIVE[5m]) > bool 0.05)
```

A workload at 95% SM_active with 25% occupancy and zero tensor activity scores ~5% — accurately reflecting "you're using the GPU but not the silicon."

### 3.3 Memory bandwidth utilization

For LLM inference and embedding workloads, you're memory-bound. SM_active is misleading because the SMs are stalled waiting on HBM:

```promql
DCGM_FI_PROF_DRAM_ACTIVE
```

If `DRAM_ACTIVE > 0.7` and `SM_ACTIVE < 0.5`, your workload is HBM-bound. There's no further GPU efficiency gain available without changing the algorithm (quantization, paged attention, FlashAttention).

### 3.4 The "GPU Waste" formula

Hours of GPU allocated but not utilized:

```promql
# Per team, hourly: GPU-hours wasted
sum by (team) (
  (1 - avg_over_time(DCGM_FI_PROF_SM_ACTIVE[1h]))
  * on(pod, namespace) group_left(label_team)
  kube_pod_labels
) * 1  # 1 hour
```

In dollars (assuming an internal transfer rate `$/gpu-hour`):

```yaml
- record: gpu:waste_dollars:1h
  expr: |
    sum by (team) (
      (1 - avg_over_time(DCGM_FI_PROF_SM_ACTIVE[1h]))
      * on(pod, namespace) group_left(label_team)
      kube_pod_labels{label_team!=""}
    ) * 4.20  # $/H100/hour internal rate
```

This is the row in your "GPU Waste Scoreboard" panel that gets emailed to engineering leadership weekly. The first time you ship it, expect surprise.

---

## 4. Fragmentation

Fragmentation = nodes that *cannot accept new GPU pods* despite having free GPUs, because something else (CPU, memory, NIC count) blocks them.

### 4.1 Detection

```promql
# Nodes with free GPUs but no schedulable capacity
(
  kube_node_status_allocatable{resource="nvidia_com_gpu"}
  - sum by (node) (
      kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
      * on(pod, namespace) group_left(node) kube_pod_info
  )
) > 0
  and on(node)
(
  kube_node_status_allocatable{resource="memory"}
  - sum by (node) (
      kube_pod_container_resource_requests{resource="memory"}
      * on(pod, namespace) group_left(node) kube_pod_info
  )
) < 32 * 1024 * 1024 * 1024  # less than 32GB free RAM
```

Translation: this node has free GPUs but < 32GB RAM left, so a typical 1-GPU pod with 64GB RAM request can't land. Common when a CPU-heavy workload squats next to an under-utilized GPU.

### 4.2 Fragmentation score

```promql
# Cluster fragmentation: GPUs free but unusable
gpu_fragmentation = (
  sum(unschedulable_gpus_per_node)  # custom metric or computed above
  / sum(kube_node_status_allocatable{resource="nvidia_com_gpu"})
)
```

Healthy clusters with bin-packing scheduling: < 5%. Without bin-packing on heterogeneous workloads: 15–30%. NVIDIA's KAI Scheduler and Volcano with bin-packing report 90% GPU occupancy as achievable; this is your target.

---

## 5. MIG Slice Utilization

When MIG is enabled, "GPU efficiency" needs per-GI breakdown:

```promql
# Per-MIG-slice utilization
avg by (Hostname, gpu, GPU_I_ID) (
  DCGM_FI_PROF_SM_ACTIVE
)

# Slices that are allocated but idle
avg_over_time(DCGM_FI_PROF_SM_ACTIVE{GPU_I_ID!=""}[15m]) < 0.1
```

The right-sizing question MIG forces: are 1g.10gb slices the right shape? If most workloads run at < 50% of a 1g slice, you should be using the smallest profile MIG offers. If they peg 1g, you should consolidate to 2g.

The driving query for a quarterly MIG resize:

```promql
# Distribution of slice utilization
histogram_quantile(0.5, # also 0.95, 0.99
  sum by (le) (
    rate(DCGM_FI_PROF_SM_ACTIVE_bucket{GPU_I_ID!=""}[1h])
  )
)
```

If p50 < 30% on 1g.10gb, your slices are too big. Repartition.

---

## 6. Bin-Packing Efficiency

The scheduler-side mirror of fragmentation. NVIDIA's Volcano integration achieved 90% GPU occupancy via bin-packing; vanilla Kubernetes scheduler defaults to *spreading* (anti bin-packing) which is wrong for GPU.

### 6.1 What to enable

In `KubeSchedulerConfiguration`:

```yaml
profiles:
  - schedulerName: gpu-bin-packer
    plugins:
      score:
        enabled:
          - name: NodeResourcesFit
        disabled:
          - name: NodeResourcesBalancedAllocation
    pluginConfig:
      - name: NodeResourcesFit
        args:
          scoringStrategy:
            type: MostAllocated   # the bin-pack toggle
            resources:
              - name: nvidia.com/gpu
                weight: 10
              - name: cpu
                weight: 1
              - name: memory
                weight: 1
```

`MostAllocated` scores nodes higher when more allocated → packs into the busiest viable node first. The opposite (`LeastAllocated`) is the default for most clusters.

### 6.2 Measuring bin-packing quality

```promql
# Distribution of node occupancy — should be heavily-weighted at 100% or 0%, not 50%
histogram_quantile(0.5,
  sum by (le) (
    rate(node_gpu_allocation_percent_bucket[1h])
  )
)
```

A bimodal distribution (lots of fully-packed nodes, lots of empty nodes) is what you want — empty nodes can scale down. A unimodal distribution at 50% is the worst case: every node half-full, none drainable.

---

## 7. The Chargeback Model

Three patterns; pick one and commit:

| Model | Mechanism | Pros | Cons |
|-------|-----------|------|------|
| **Allocation-based** | Bill per `requested_gpu × time` | Simple, predictable | Penalizes idle requests but doesn't reward efficient use |
| **Utilization-based** | Bill per `SM_active × time` | Rewards actual work | Hard to budget; encourages gaming (run busywork to look efficient) |
| **Hybrid (recommended)** | Bill `requested × time` with a *waste credit* refund for util > X% | Predictable + rewards efficiency | More plumbing |

The hybrid recording rules:

```yaml
groups:
  - name: gpu-chargeback
    interval: 1h
    rules:
      - record: chargeback:base:1h
        expr: |
          sum by (team) (
            kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
            * on(pod, namespace) group_left(label_team) kube_pod_labels
          )
          * 4.20  # $/H100/h

      - record: chargeback:efficiency_credit:1h
        expr: |
          sum by (team) (
            (avg_over_time(DCGM_FI_PROF_SM_ACTIVE[1h]) > 0.7)
            * on(pod, namespace) group_left(label_team) kube_pod_labels
          )
          * 0.50  # $0.50 credit per efficient GPU-hour

      - record: chargeback:net:1h
        expr: chargeback:base:1h - chargeback:efficiency_credit:1h
```

This rewards teams that pack their workloads densely without forcing them to gamble on capacity.

### 7.1 Showback vs chargeback

| Phase | Goal | Risk |
|-------|------|------|
| **Showback** (months 1–6) | Surface the numbers, no real $ moves | None — informational |
| **Chargeback** (after) | Real budget transfers | Teams game it; need solid baselines |

Most companies sit in showback for >6 months because moving to chargeback without a calibrated baseline causes legitimate workloads to look bad and creates internal political fallout.

---

## 8. The Efficiency Scoreboard Dashboard

The L1 fleet-overview panel. Doc 09 details the layout; the data model:

| Column | Source | Why it matters |
|--------|--------|----------------|
| Team | `kube_pod_labels.team` | Accountability |
| GPUs requested | `sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"})` | What they hold |
| Mean SM_active | `avg(DCGM_FI_PROF_SM_ACTIVE)` over team's pods | Are they using it |
| Mean Tensor active | `avg(DCGM_FI_PROF_PIPE_TENSOR_ACTIVE)` | Are they using the silicon they paid for |
| GPU-hours wasted (1d) | derived as in §3.4 | Translation to $ |
| Wasted $ (1d) | × $/h | What leadership reads |
| Trend (1d vs 7d) | comparison | Is it improving? |

Sort descending by `wasted $`. The top of this list is where to focus.

---

## 9. Workload-Specific Right-Sizing

The deeper decisions this dashboard enables (most happen quarterly, not in real-time):

### 9.1 GPU model selection

If a workload's `SM_active` rarely exceeds 30%, you don't need an H100. Options:

| Observed efficiency | Suggestion |
|---------------------|-----------|
| H100 at < 20% sustained | Move to A100 or smaller, save 2–3× |
| H100 at < 50% with no tensor activity | Move to L40S (cheaper, no NVLink) |
| H100 at > 80%, tensor at > 50% | Keep H100 |
| H100 at > 90%, FP64 active | This is HPC, keep H100 or upgrade to B200 |

### 9.2 MIG vs whole GPU

Per workload, the chart you want is the *peak* SM_active and HBM_used:

| Peak SM_active | Peak HBM | Recommendation |
|----------------|----------|----------------|
| < 30% | < 10GB | 1g.10gb MIG slice |
| < 60% | < 20GB | 2g.20gb |
| < 80% | < 40GB | 3g.40gb or 4g.40gb |
| ≥ 80% | ≥ 40GB | Whole GPU |

### 9.3 Sharing mode decision

If utilization is bursty (inference) with low correlation across tenants:

- **MPS** if processes are trusted and homogeneous
- **MIG** if multi-tenant or untrusted
- **Time-slicing** only for dev/notebook workloads

---

## 10. Enforcement: Limit-Range and Quota

Quotas are enforced by API server; observability tells you who is breaching:

```promql
# Quota breach attempts (denied admissions)
increase(apiserver_admission_controller_admission_duration_seconds_count{
  name="ResourceQuota",
  rejected="true"
}[1h])

# Per-namespace quota usage trend
sum by (namespace) (kube_pod_container_resource_requests{resource="nvidia_com_gpu"})
/
sum by (namespace) (kube_resourcequota{resource="requests.nvidia.com/gpu", type="hard"})
```

The pattern: alert at 90%, page at 100%, route to the namespace owner so platform team isn't the bottleneck.

---

## 11. Targets to Aim For

Realistic targets for an org running these dashboards seriously for 6+ months:

| Metric | Floor | Good | Excellent |
|--------|-------|------|-----------|
| Cluster GPU allocation | > 70% | > 85% | > 95% |
| Mean SM_active across allocated GPUs | > 30% | > 50% | > 70% |
| Tensor core utilization | > 10% | > 30% | > 50% |
| GPU fragmentation | < 20% | < 10% | < 5% |
| Idle notebook GPU-hours / total | < 10% | < 5% | < 2% |
| Stragglers in training jobs (>10% slow) | < 5% of jobs | < 1% | 0 |

Most orgs start in the "floor" column. Getting to "good" is mostly bin-packing + idle reclaim. Getting to "excellent" requires workload owners to care.

---

## 12. Forward Pointers

- **Doc 06**: per-host efficiency drilldown (NUMA, NVLink, PCIe topology contributions)
- **Doc 08**: the recording rules backing this dashboard (cardinality budget)
- **Doc 09**: the actual Grafana JSON for the scoreboard
- **Doc 12**: capacity planning uses this as the historical base
- **Doc 13**: per-tenant cost attribution

---

## Sources

- [NVIDIA — Practical Tips for Preventing GPU Fragmentation in Volcano](https://developer.nvidia.com/blog/practical-tips-for-preventing-gpu-fragmentation-for-volcano-scheduler/)
- [Cast AI — 2026 Kubernetes Resource Optimization Report](https://cast.ai/blog/2026-state-of-kubernetes-resource-optimization-cpu-at-8-memory-at-20-and-getting-worse/)
- [Spheron — Kubernetes GPU Orchestration in 2026: DRA, KAI, Grove](https://www.spheron.network/blog/kubernetes-gpu-orchestration-2026/)
- [CIO — How Kubernetes is solving the GPU utilization crisis](https://www.cio.com/article/4152554/how-kubernetes-is-finally-solving-the-gpu-utilization-crisis-to-save-your-ai-budget.html)
- [Mavvrik — On-prem GPU chargeback strategies](https://www.mavvrik.ai/on-premises-gpu-chargeback-strategies-challenges-and-kubernetes/)
- [Kubernetes — Resource Bin Packing](https://kubernetes.io/docs/concepts/scheduling-eviction/resource-bin-packing/)
- [The Register — Datadog GPU efficiency (April 2026)](https://www.theregister.com/2026/04/23/datadog_digs_down_into_gpu/)
