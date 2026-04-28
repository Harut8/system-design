# 09 — Grafana Dashboards: Design & Implementation

> Five dashboards. Different audiences, different time windows, different metrics. The trap is to build one dashboard that tries to serve everyone — it ends up serving no one.

This doc lays out the L1–L5 dashboard hierarchy, with the queries, panels, and template variables for each. Built as Grafonnet (Jsonnet) snippets so they're version-controlled. The full JSON exports are too long to inline; the queries are the load-bearing part.

---

## 1. The L1–L5 Hierarchy

Each level zooms in. The right pattern is *click-down*: a panel on L1 deep-links to the L2 view filtered to the cluster you clicked on, etc.

| Level | Audience | Time window | Update freq |
|-------|----------|-------------|-------------|
| **L1 — Fleet** | VP Eng / leadership | 7d / 30d | Daily |
| **L2 — Cluster** | Platform team / infra on-call | 6h / 24h | Live |
| **L3 — Node** | SRE / hardware investigation | 1h / 6h | Live |
| **L4 — Job/Pod** | Workload owner / ML engineer | Job duration | Live |
| **L5 — Single GPU** | Hardware deep dive / RMA decision | 24h / 7d | Live |

### 1.1 Template variables, hierarchical

```jsonnet
local vars = [
  // L2 cluster selector — drives everything
  template.new('cluster', 'Prometheus',
    'label_values(up{job="dcgm-exporter"}, cluster)',
    label='Cluster',
    multi=false,
    includeAll=false,
  ),
  // L3 node selector — depends on cluster
  template.new('node', 'Prometheus',
    'label_values(DCGM_FI_DEV_GPU_TEMP{cluster="$cluster"}, Hostname)',
    label='Node',
    multi=true,
    includeAll=true,
  ),
  // L4 namespace selector
  template.new('namespace', 'Prometheus',
    'label_values(DCGM_FI_DEV_GPU_TEMP{cluster="$cluster", pod!=""}, namespace)',
    label='Namespace',
    multi=true,
    includeAll=true,
  ),
  // L5 GPU UUID
  template.new('gpu_uuid', 'Prometheus',
    'label_values(DCGM_FI_DEV_GPU_TEMP{cluster="$cluster", Hostname=~"$node"}, UUID)',
    label='GPU UUID',
  ),
];
```

The chain `cluster → node → namespace → gpu_uuid` lets a viewer drill from fleet down to a single GPU without ever leaving Grafana.

---

## 2. L1 — Fleet Overview

The dashboard leadership opens once a week. Optimize for *signal over fidelity*: 7-day averages, no live data, big numbers.

### 2.1 Big stat panels (top row)

| Panel | Query |
|-------|-------|
| **Total GPUs in fleet** | `count(DCGM_FI_DEV_GPU_TEMP)` |
| **Mean SM_active (7d)** | `avg_over_time(cluster:gpu_sm_active:avg1m[7d])` |
| **Mean Tensor active (7d)** | `avg_over_time(cluster:gpu_tensor_active:avg1m[7d])` |
| **Allocation efficiency** | `avg_over_time(cluster:gpu_allocation:ratio[7d])` |
| **Wasted GPU-hours (7d)** | `sum(team:gpu_waste_hours:1m) * 7 * 24` |
| **Wasted $ (7d)** | `sum(team:gpu_waste_dollars:1m) * 7 * 24` |

### 2.2 Team scoreboard (the panel that matters)

Sorted descending by waste:

```promql
# Per-team table: requested, mean util, wasted $/week
topk(20,
  sum by (label_team) (
    sum_over_time(team:gpu_waste_dollars:1m[7d])
  )
)
```

Display as a Grafana table panel with color cells (heatmap on waste $ column).

### 2.3 Per-cluster utilization heatmap

```promql
# Heatmap: cluster × hour-of-week, value = mean SM_active
avg by (cluster) (
  avg_over_time(cluster:gpu_sm_active:avg1m[1h])
)
```

Time on X, cluster on Y, color by utilization. A glance shows which clusters are full vs empty and when.

### 2.4 Hardware health summary

| Panel | Query |
|-------|-------|
| GPUs in RMA queue | `count(gpu_lifecycle_state{state="in_rma"})` |
| DBE events (7d) | `sum(increase(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL[7d]))` |
| XID 79 events (7d) | `sum(increase(gpu_xid_total{code="79"}[7d]))` |
| Mean fleet temp | `avg(DCGM_FI_DEV_GPU_TEMP)` |

---

## 3. L2 — Cluster View

The platform team's primary screen. One per cluster (template-variable driven).

### 3.1 Allocation row

```promql
# Cluster allocation %
sum(kube_pod_container_resource_requests{cluster="$cluster", resource="nvidia_com_gpu"})
/
sum(kube_node_status_allocatable{cluster="$cluster", resource="nvidia_com_gpu"})

# Pending GPU pods
sum(kube_pod_status_phase{cluster="$cluster", phase="Pending"}
  * on(pod, namespace) group_left()
  (sum by (pod, namespace) (
    kube_pod_container_resource_requests{cluster="$cluster", resource="nvidia_com_gpu"}
  ) > 0))

# Capacity vs allocated over time
sum(kube_node_status_allocatable{cluster="$cluster", resource="nvidia_com_gpu"})
sum(kube_pod_container_resource_requests{cluster="$cluster", resource="nvidia_com_gpu"})
```

### 3.2 Utilization row

```promql
# Distribution of SM_active across all GPUs in cluster
histogram_quantile(0.5, sum by (le) (rate(DCGM_FI_PROF_SM_ACTIVE_bucket{cluster="$cluster"}[5m])))
histogram_quantile(0.95, ...)
histogram_quantile(0.99, ...)
```

Plot p50/p95/p99 lines. Healthy training-heavy cluster: p50 around 80%, p95 near 95%. Healthy inference cluster: p50 lower, p95 spiky.

### 3.3 Health row

| Panel | Query |
|-------|-------|
| ECC errors (5m rate) | `sum(rate(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL{cluster="$cluster"}[5m]))` |
| Throttling GPUs | `count((DCGM_FI_DEV_CLOCK_THROTTLE_REASONS{cluster="$cluster"} & 0xC0) > 0)` |
| NVLink errors (5m rate) | `sum(rate(DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL{cluster="$cluster"}[5m]))` |
| Exporter availability | `avg(up{cluster="$cluster", job="dcgm-exporter"})` |

### 3.4 Workload class breakdown

```promql
# Average SM_active by workload class
avg by (label_workload_class) (
  DCGM_FI_PROF_SM_ACTIVE{cluster="$cluster"}
  * on(pod, namespace) group_left(label_workload_class)
  kube_pod_labels{cluster="$cluster"}
)
```

Three lines: training, inference, notebook. The shape comparison alone is informative.

---

## 4. L3 — Node View

When a node is suspected. The drilldown screen for the §10 in doc 06 (slow-GPU runbook).

### 4.1 Node-level panel grid

For node `$node`:

```promql
# All 8 GPUs' temperatures
DCGM_FI_DEV_GPU_TEMP{Hostname="$node"}

# All 8 GPUs' HBM temperatures
DCGM_FI_DEV_MEMORY_TEMP{Hostname="$node"}

# All 8 GPUs' power draw
DCGM_FI_DEV_POWER_USAGE{Hostname="$node"}

# All 8 GPUs' SM_active
DCGM_FI_PROF_SM_ACTIVE{Hostname="$node"}
```

Lay out as a 2×2 grid, each panel showing 8 lines.

### 4.2 NVLink heatmap

```promql
# Per-GPU NVLink TX bandwidth
sum by (gpu) (rate(DCGM_FI_PROF_NVLINK_TX_BYTES{Hostname="$node"}[1m]))

# NVLink errors per GPU
sum by (gpu) (rate(DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL{Hostname="$node"}[5m]))
```

### 4.3 PCIe row

```promql
# PCIe link gen / width — alert if degraded
DCGM_FI_DEV_PCIE_LINK_GEN{Hostname="$node"}
DCGM_FI_DEV_PCIE_LINK_WIDTH{Hostname="$node"}

# PCIe bandwidth
sum by (gpu) (rate(DCGM_FI_PROF_PCIE_RX_BYTES{Hostname="$node"}[1m]))
sum by (gpu) (rate(DCGM_FI_PROF_PCIE_TX_BYTES{Hostname="$node"}[1m]))
```

### 4.4 Throttle stacked bar

```promql
# Decode throttle bitfield into individual reasons
(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS{Hostname="$node"} & 0x40) > 0   # HW thermal
(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS{Hostname="$node"} & 0x80) > 0   # HW power brake
(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS{Hostname="$node"} & 0x20) > 0   # SW thermal
(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS{Hostname="$node"} & 0x04) > 0   # SW power cap
```

Stacked bar over time per GPU.

### 4.5 Pod assignment table

```promql
# Which pods own which GPUs on this node
DCGM_FI_DEV_GPU_TEMP{Hostname="$node", pod!=""}
```

Display as table with columns: gpu, UUID, namespace, pod, container.

---

## 5. L4 — Job/Pod View

For workload owners. Per training run or per inference deployment.

### 5.1 Training job dashboard

Variables: `job_id`, `cluster`.

```promql
# Step time per rank
histogram_quantile(0.5,
  sum by (rank, le) (rate(train_step_seconds_bucket{job_id="$job_id"}[1m]))
)

# Samples/sec (total across ranks)
sum(rate(train_samples_total{job_id="$job_id"}[5m]))

# SM_active per rank (alongside step time)
avg by (rank) (
  DCGM_FI_PROF_SM_ACTIVE
  * on(pod, namespace) group_left(rank)
  kube_pod_labels{label_job_id="$job_id"}
)

# Loss
train_loss{job_id="$job_id"}

# Gradient norm
train_grad_norm{job_id="$job_id"}

# Checkpoint markers (from event metric)
train_checkpoint_seconds_total{job_id="$job_id"}
```

Annotate the loss panel with checkpoint markers (Grafana annotation queries).

### 5.2 Inference deployment dashboard

Variables: `service`, `namespace`.

```promql
# Request rate
sum(rate(inference_requests_total{service="$service"}[1m]))

# Latency percentiles (TTFT for LLM)
histogram_quantile(0.5, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket{service="$service"}[5m])))
histogram_quantile(0.95, ...)
histogram_quantile(0.99, ...)

# Throughput at SLO (compose RPS with p99 latency check)
sum(rate(inference_requests_total{service="$service"}[1m]))
unless on()
(histogram_quantile(0.99,
   sum by (le) (rate(inference_request_duration_seconds_bucket{service="$service"}[5m]))
) > 0.5)

# vLLM-specific
vllm:kv_cache_usage_perc{service="$service"}
rate(vllm:prefix_cache_hits[5m]) / rate(vllm:prefix_cache_queries[5m])
vllm:num_requests_running{service="$service"}
vllm:num_requests_waiting{service="$service"}

# Underlying GPU utilization
avg(DCGM_FI_PROF_SM_ACTIVE
    * on(pod, namespace) group_left() kube_pod_labels{label_app="$service"})
```

Annotate the latency panel with deploy events (rollout markers from k8s events).

---

## 6. L5 — Single GPU Detail

Used when an RMA decision is on the table, or for forensic investigation.

Variables: `gpu_uuid`.

```promql
# All time series for this GPU on one screen
DCGM_FI_DEV_GPU_TEMP{UUID="$gpu_uuid"}
DCGM_FI_DEV_MEMORY_TEMP{UUID="$gpu_uuid"}
DCGM_FI_DEV_POWER_USAGE{UUID="$gpu_uuid"}
DCGM_FI_PROF_SM_ACTIVE{UUID="$gpu_uuid"}
DCGM_FI_PROF_SM_OCCUPANCY{UUID="$gpu_uuid"}
DCGM_FI_PROF_PIPE_TENSOR_ACTIVE{UUID="$gpu_uuid"}
DCGM_FI_PROF_DRAM_ACTIVE{UUID="$gpu_uuid"}
DCGM_FI_DEV_FB_USED{UUID="$gpu_uuid"}
DCGM_FI_DEV_ECC_SBE_VOL_TOTAL{UUID="$gpu_uuid"}
DCGM_FI_DEV_ECC_DBE_VOL_TOTAL{UUID="$gpu_uuid"}
DCGM_FI_DEV_RETIRED_SBE{UUID="$gpu_uuid"}
DCGM_FI_DEV_RETIRED_DBE{UUID="$gpu_uuid"}
DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL{UUID="$gpu_uuid"}
DCGM_FI_DEV_PCIE_LINK_GEN{UUID="$gpu_uuid"}
DCGM_FI_DEV_PCIE_LINK_WIDTH{UUID="$gpu_uuid"}
DCGM_FI_DEV_CLOCK_THROTTLE_REASONS{UUID="$gpu_uuid"}
gpu_xid_total{UUID="$gpu_uuid"}
```

This is the dashboard used in the RMA decision: "show me the last 7 days for this GPU's serial." Annotate with workload assignment changes (pod transitions).

---

## 7. Annotation Strategy

Annotations layer events onto time-series. The ones that pay off:

| Annotation | Source | Useful on |
|------------|--------|-----------|
| Pod started/terminated | `kube_pod_status_phase` change | L3, L4 |
| MIG reconfiguration | `gpu_mig_config_changed` | L3, L5 |
| Driver/firmware update | manual or from Ansible | L1–L5 |
| Cluster autoscaler scale-up | `cluster_autoscaler_scaled_up_nodes_total` increase | L2 |
| Alert fired | Alertmanager webhook → Grafana | All |
| RMA / replacement | manual / lifecycle exporter | L5 |

In Grafana 10+, annotation queries support PromQL directly. Example:

```promql
# Annotation: a DBE happened on this GPU
increase(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL{UUID="$gpu_uuid"}[1m]) > 0
```

---

## 8. Dashboard-as-Code: Grafonnet Pattern

A small library of helpers eliminates copy-paste:

```jsonnet
// gpu-panels.libsonnet
local g = import 'g.libsonnet';

{
  utilizationPanel(title, query, format='percentunit')::
    g.panel.timeSeries.new(title)
    + g.panel.timeSeries.queryOptions.withTargets([
        g.query.prometheus.new('Prometheus', query)
      ])
    + g.panel.timeSeries.standardOptions.withUnit(format)
    + g.panel.timeSeries.standardOptions.withMin(0)
    + g.panel.timeSeries.standardOptions.withMax(1),

  thresholdPanel(title, query, warn, crit)::
    self.utilizationPanel(title, query)
    + g.panel.timeSeries.standardOptions.thresholds.withMode('absolute')
    + g.panel.timeSeries.standardOptions.thresholds.withSteps([
        g.panel.timeSeries.thresholdStep.withColor('green').withValue(0),
        g.panel.timeSeries.thresholdStep.withColor('orange').withValue(warn),
        g.panel.timeSeries.thresholdStep.withColor('red').withValue(crit),
      ]),

  ggridSinglePanel(title, query)::
    g.panel.statTimeline.new(title)
    + g.panel.statTimeline.queryOptions.withTargets([
        g.query.prometheus.new('Prometheus', query)
      ]),
}
```

Then a dashboard file becomes:

```jsonnet
local lib = import 'gpu-panels.libsonnet';
local g = import 'g.libsonnet';

g.dashboard.new('GPU L3 — Node View')
+ g.dashboard.withVariables([...])
+ g.dashboard.withPanels([
    lib.utilizationPanel('SM Active per GPU',
      'DCGM_FI_PROF_SM_ACTIVE{Hostname="$node"}'),
    lib.utilizationPanel('Tensor Active per GPU',
      'DCGM_FI_PROF_PIPE_TENSOR_ACTIVE{Hostname="$node"}'),
    lib.thresholdPanel('GPU Temp', 'DCGM_FI_DEV_GPU_TEMP{Hostname="$node"}',
      warn=80, crit=87),
    // ...
  ])
```

CI runs `jsonnet -J vendor dashboards/gpu-l3.jsonnet > dashboards/gpu-l3.json` and pushes to Grafana via the operator or API.

---

## 9. Color and Threshold Conventions

A tiny but important design rule: use the *same* thresholds across all dashboards.

| Metric | Green | Yellow | Red |
|--------|-------|--------|-----|
| GPU temp (°C) | <80 | 80–86 | >86 |
| HBM temp (°C) | <85 | 85–93 | >93 |
| Power vs TDP | <90% | 90–95% | >95% |
| SM_active (training) | >70% | 50–70% | <50% |
| SM_active (inference under load) | <60% | 60–80% | >80% (saturating) |
| Allocation | <80% | 80–95% | >95% |
| ECC SBE rate (per hour) | <1 | 1–10 | >10 |
| Pending GPU pods | 0 | 1–5 | >5 |

The same color appears on every dashboard for the same value — the eye learns it.

---

## 10. Performance: query budgets per dashboard

Dashboards refresh frequently. A panel that takes 8s to query is a bad panel. Budgets:

| Level | Per-panel target | Total dashboard target |
|-------|------------------|------------------------|
| L1 (long windows) | < 2s | < 10s on initial load |
| L2 (live) | < 500ms | < 3s |
| L3 (live) | < 500ms | < 3s |
| L4/L5 (focused) | < 1s | < 5s |

Achieve this by:
1. Querying recording rules whenever possible (they're pre-computed)
2. Using `$__rate_interval` instead of hard-coded `[5m]` so range matches dashboard refresh
3. Caching: Grafana 10+ has query caching; enable for L1
4. `min_step` on long-range queries

The "I added a `pod` label to a fleet panel and now it's slow" bug happens once — make a CI lint that grep for high-card labels in L1/L2 dashboard JSON.

---

## 11. Layout: the home dashboard

A single landing page at `/d/gpu-home` with:

- One stat panel per cluster (mean SM_active 24h)
- Click → opens L2 for that cluster
- One row of "current alerts firing"
- Links to L1 (fleet), to RMA queue, to capacity planning dashboard

This is the URL you put in the team's bookmarks. Most clicks happen here.

---

## 12. Acceptance Checklist

- [ ] L1 fleet dashboard renders with team scoreboard sorted by waste $
- [ ] L2 cluster dashboard renders for each cluster, drilldown link to L3
- [ ] L3 node dashboard shows all 8 GPUs in grid
- [ ] L4 training & inference dashboards exist with appropriate queries
- [ ] L5 single-GPU dashboard works with UUID variable
- [ ] All dashboards generated from Jsonnet, committed to repo
- [ ] No L1/L2 panel uses pod or job_id labels (kept low-card)
- [ ] Annotations wired for pod transitions, alerts firing, deploys
- [ ] Threshold colors consistent across dashboards
- [ ] Per-dashboard query budgets met

---

## 13. A Complete Panel Example: Throttle Reason Decoder

The throttle reasons bitfield deserves its own dashboard panel. Here's the complete Grafonnet for the L3 view:

```jsonnet
local g = import 'g.libsonnet';

local throttleBits = [
  { name: 'GPU Idle',         hex: '0x01', color: 'gray'   },
  { name: 'App Clock Set',    hex: '0x02', color: 'blue'   },
  { name: 'SW Power Cap',     hex: '0x04', color: 'yellow' },
  { name: 'HW Slowdown',      hex: '0x08', color: 'orange' },
  { name: 'Sync Boost',       hex: '0x10', color: 'blue'   },
  { name: 'SW Thermal',       hex: '0x20', color: 'orange' },
  { name: 'HW Thermal',       hex: '0x40', color: 'red'    },
  { name: 'HW Power Brake',   hex: '0x80', color: 'red'    },
];

g.panel.timeSeries.new('Throttle Reasons (decoded) — $node')
+ g.panel.timeSeries.queryOptions.withTargets([
    g.query.prometheus.new('Prometheus',
      '(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS{Hostname="$node"} & %s) > 0' % bit.hex)
    + g.query.prometheus.withLegendFormat('GPU{{gpu}} — ' + bit.name)
    for bit in throttleBits
  ])
+ g.panel.timeSeries.fieldConfig.defaults.custom.withDrawStyle('bars')
+ g.panel.timeSeries.fieldConfig.defaults.custom.withStacking({mode: 'normal'})
```

The panel renders as a stacked bar over time. Idle GPUs show gray; thermally-throttled GPUs show red. **The "GPU is slow" runbook (Appendix C.1) starts on this panel.**

### 13.1 Health row, fully composed

```jsonnet
local lib = import 'gpu-panels.libsonnet';

local healthRow = g.row.new('Health — $cluster')
+ g.row.withPanels([
    lib.thresholdPanel('GPU Temp',
      'DCGM_FI_DEV_GPU_TEMP{cluster="$cluster"}',
      warn=80, crit=87),
    lib.thresholdPanel('HBM Temp',
      'DCGM_FI_DEV_MEMORY_TEMP{cluster="$cluster"}',
      warn=85, crit=93),
    lib.utilizationPanel('Power vs TDP',
      'DCGM_FI_DEV_POWER_USAGE{cluster="$cluster"} / on(UUID) DCGM_FI_DEV_POWER_MGMT_LIMIT'),
    g.panel.stat.new('GPUs Throttled (HW)')
    + g.panel.stat.queryOptions.withTargets([
        g.query.prometheus.new('Prometheus',
          'count((DCGM_FI_DEV_CLOCK_THROTTLE_REASONS{cluster="$cluster"} & 0xC0) > 0)')
      ]),
    g.panel.stat.new('SBE Rate (per hour)')
    + g.panel.stat.queryOptions.withTargets([
        g.query.prometheus.new('Prometheus',
          'sum(rate(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL{cluster="$cluster"}[1h])) * 3600')
      ]),
  ]);
```

Reusable across L2 (cluster), L3 (node) — same row, different `cluster`/`Hostname` filter.

---

## 14. Forward Pointers

- **Doc 10**: alerts that source the same metrics; correlate firing alerts as annotations
- **Doc 12**: capacity planning dashboard (uses 1h rollups exclusively)
- **Doc 13**: per-tenant Grafana orgs, RBAC
- **Appendix B**: full throttle bitfield reference for the decoder panel
- **Appendix C**: which dashboard each flowchart starts from

---

## Sources

- [Grafonnet — official docs](https://grafana.github.io/grafonnet/index.html)
- [Grafonnet — GitHub](https://github.com/grafana/grafonnet)
- [NVIDIA DCGM Dashboard for Kubernetes](https://grafana.com/grafana/dashboards/23382-nvidia-mig-dcgm/)
- [Glukhov — Monitor LLM inference with Prometheus & Grafana (2026)](https://www.glukhov.org/observability/monitoring-llm-inference-prometheus-grafana/)
- [Ceph — Managing Grafana Dashboards with Grafonnet](https://ceph.io/en/news/blog/2021/managing-grafana-dashboards-with-grafonnet/)
