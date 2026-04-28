# 08 — Prometheus Metrics Design & Cardinality Management

> Cardinality is the resource you'll run out of. Not CPU, not disk, not network. *Active series.* This doc is the budget, the labels you should and shouldn't keep, the recording rules that move you from a 10M-series cluster to a 1M-series cluster, and the remote_write tuning to stop your WAL from filling.

If you skipped to this doc because Prometheus is OOMing, the answer is in §3 (drop labels) and §4 (recording rules). Apply both, in that order.

---

## 1. The Cardinality Math, Annotated

Base GPU telemetry, no labels:

```
1000 nodes × 8 GPUs × 30 base DCGM fields = 240,000 series
```

That's manageable. Add the standard scrape labels (`Hostname`, `gpu`, `UUID`, `device`, `modelName`, `container`, `namespace`, `pod`):

```
240,000 base series × workload-identity factor ≈ 1.5–3M series
```

The factor depends on pod churn, MIG slices, and how aggressive your relabeling is. A 3M series Prometheus is a single replica on a beefy node (64GB+ RAM). A 10M series Prometheus needs Mimir/Cortex/Thanos.

Failure modes you'll meet first:

| Series count | What breaks |
|--------------|-------------|
| ~2M | Single Prometheus replica fits on a 32GB node |
| ~5M | Need 64GB+, longer query latencies |
| ~10M | Single replica struggles; consider sharding or LTS |
| ~20M | Sharding mandatory; large queries time out |
| ~50M+ | You need Mimir with proper limits |

---

## 2. Label Taxonomy for GPU Metrics

Every label is a multiplier. Categorize them before adding:

### 2.1 Stable, low-cardinality (always keep)

| Label | Cardinality | Purpose |
|-------|-------------|---------|
| `Hostname` / `node` | ~1k | Locate the machine |
| `gpu` | 0..7 typically | GPU index on node |
| `UUID` | 1 per GPU | Stable identifier across reboots |
| `modelName` | ~5 | Model SKU (H100, A100, etc.) |
| `gpu_product` | ~5 | Same idea via k8s label |

Total contribution: a small constant multiplier.

### 2.2 Medium cardinality (keep with care)

| Label | Cardinality | Risk |
|-------|-------------|------|
| `namespace` | ~50 | Manageable; teams add namespaces over time |
| `GPU_I_ID` (MIG GI) | up to 7 per GPU | Multiplies × 7 on MIG-enabled clusters |
| `GPU_I_PROFILE` | ~5 (1g, 2g, 3g, 4g, 7g) | Constant |
| `container` | depends on pod design | OK if pods have 1-2 containers |

### 2.3 High cardinality (do not keep on raw scrapes)

| Label | Cardinality | Why bad |
|-------|-------------|---------|
| `pod` | thousands, churns | Recreated on every restart, deploy, scale |
| `replica_set` (older form) | hundreds, churns | Same as pod |
| `job_id` (Slurm/training jobs) | thousands per day | Each job creates new series |
| `request_id` / `trace_id` | ~∞ | Cardinality bomb, never |
| `user` (notebooks) | grows unboundedly | Don't put in metrics |

**The rule**: pod-level identity belongs in *recording rules* with bounded cardinality (rolled up by namespace, owner_kind, or workload class), not raw scrape labels. The exception is if you actively need per-pod drilldown — then keep it but accept the cost.

### 2.4 The MIG cardinality multiplier

A 1000-GPU cluster fully partitioned to 1g.10gb slices:

```
1000 × 7 GIs = 7000 logical GPUs
× 30 fields  = 210,000 base series
× labels      = 1.5–3M
```

7× the non-MIG case. Two mitigations:

1. **Drop GI labels from physical-GPU-only metrics** (temperature, power, ECC totals — these are per-physical-GPU regardless of MIG):

```yaml
metric_relabel_configs:
  - source_labels: [__name__, GPU_I_ID]
    regex: "(DCGM_FI_DEV_GPU_TEMP|DCGM_FI_DEV_POWER_USAGE|DCGM_FI_DEV_ECC_.*);.+"
    action: drop
```

2. **Keep GI labels only on metrics where they're meaningful** (`_PROF_SM_ACTIVE`, `_PROF_DRAM_ACTIVE`, `_DEV_FB_USED`, etc.).

---

## 3. Relabeling Strategies (the three knobs)

Three places to apply relabeling, in increasing cost:

### 3.1 At the exporter (cheapest)

The dcgm-exporter ConfigMap (doc 02) already controls *which fields* are emitted. Don't emit fields you won't query — every field is N series across the fleet.

### 3.2 At the scrape (cheap)

In Prometheus `scrape_configs`, `metric_relabel_configs` runs after scrape, before storage. Drops here save TSDB write cost:

```yaml
- job_name: dcgm-exporter
  kubernetes_sd_configs: [...]
  metric_relabel_configs:
    # Drop pod label from non-workload metrics (saves churn cost)
    - source_labels: [__name__]
      regex: "DCGM_FI_DEV_GPU_TEMP|DCGM_FI_DEV_POWER_USAGE|DCGM_FI_DEV_FAN_SPEED"
      action: replace
      target_label: pod
      replacement: ""

    # Drop modelName from per-pod fields (it's redundant — derive from node label)
    - action: labeldrop
      regex: "modelName|driver_version"

    # Drop GI labels from physical-only fields
    - source_labels: [__name__, GPU_I_ID]
      regex: "(DCGM_FI_DEV_(GPU_TEMP|POWER_USAGE|ECC_.+|FAN_SPEED|CLOCK_.+));.+"
      action: drop
```

### 3.3 At the recording rule (next section)

---

## 4. Recording Rules — The Lever That Saves the Day

Recording rules pre-compute aggregations. They:
1. Reduce query-time work for dashboards
2. Provide stable, low-cardinality series for remote_write
3. Enable hierarchical federation (doc 01 §5.1)

### 4.1 The standard GPU rule set

```yaml
groups:
  - name: gpu-aggregations
    interval: 1m
    rules:
      # Per-namespace utilization
      - record: namespace:gpu_sm_active:avg1m
        expr: |
          avg by (namespace) (
            DCGM_FI_PROF_SM_ACTIVE
            * on(pod, namespace) group_left() kube_pod_info
          )

      # Per-team (label-derived)
      - record: team:gpu_sm_active:avg1m
        expr: |
          avg by (label_team) (
            DCGM_FI_PROF_SM_ACTIVE
            * on(pod, namespace) group_left(label_team) kube_pod_labels
          )

      # Cluster-level
      - record: cluster:gpu_sm_active:avg1m
        expr: avg(DCGM_FI_PROF_SM_ACTIVE)

      # Tensor utilization roll-up
      - record: namespace:gpu_tensor_active:avg1m
        expr: |
          avg by (namespace) (
            DCGM_FI_PROF_PIPE_TENSOR_ACTIVE
            * on(pod, namespace) group_left() kube_pod_info
          )

      # Allocation efficiency
      - record: cluster:gpu_allocation:ratio
        expr: |
          sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"})
          /
          sum(kube_node_status_allocatable{resource="nvidia_com_gpu"})

      # Wasted GPU-hours (5m window, scaled to per-hour)
      - record: team:gpu_waste_hours:1m
        expr: |
          sum by (label_team) (
            (1 - avg_over_time(DCGM_FI_PROF_SM_ACTIVE[5m]))
            * on(pod, namespace) group_left(label_team)
            kube_pod_labels{label_team!=""}
          ) / 60

  - name: gpu-health
    interval: 1m
    rules:
      - record: cluster:gpu_dbe_rate:5m
        expr: sum(rate(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL[5m]))

      - record: cluster:gpu_xid_critical_rate:5m
        expr: |
          sum by (Hostname) (
            increase(gpu_xid_total{code=~"45|48|62|64|79|95"}[5m])
          )

  - name: gpu-rollups-for-lts
    interval: 5m
    rules:
      # 5m rollup of SM_active for long-term retention
      - record: gpu_sm_active:avg5m
        expr: avg_over_time(DCGM_FI_PROF_SM_ACTIVE[5m])

  - name: gpu-rollups-1h
    interval: 1h
    rules:
      - record: gpu_sm_active:avg1h
        expr: avg_over_time(DCGM_FI_PROF_SM_ACTIVE[1h])
```

### 4.2 The retention pipeline this enables

```
Raw 15s samples ──── 2h-15d ─── Prometheus
   │
   ├── 1m recording rules ──── 30d retention ─── Prometheus / LTS
   │
   ├── 5m recording rules ──── 90d retention ─── LTS
   │
   └── 1h recording rules ──── 13mo+ retention ── LTS (capacity planning)
```

Capacity planning queries (doc 12) target the 1h rollups. Daily dashboards target 5m. Live drilldown targets raw.

The math: a 1h rollup is 240× fewer samples than 15s. Storing the 1h rollup for 13 months costs less than storing raw for 15 days.

---

## 5. Detecting Cardinality Drift (the alerts you need)

You don't notice cardinality growth until something breaks. Pre-empt:

```yaml
- alert: PrometheusHeadSeriesGrowing
  expr: |
    deriv(prometheus_tsdb_head_series[6h]) > 1000   # > 1000 new series/sec sustained
  for: 30m
  labels: { severity: warning, team: observability }
  annotations:
    summary: "Prometheus head series growing fast"

- alert: PrometheusHighCardinalityMetric
  expr: |
    topk(5,
      count by (__name__) ({__name__=~".+"})
    ) > 100000
  for: 1h
  labels: { severity: warning }
  annotations:
    summary: "Metric {{ $labels.__name__ }} has {{ $value }} series"
```

The second alert is the canary for the day someone added a B200 with new labels, or someone stopped relabeling pod IDs out.

---

## 6. The "Top Cardinality Offenders" Investigation Query

When `prometheus_tsdb_head_series` is climbing, run:

```promql
# Top 20 metrics by series count
topk(20, count by (__name__) ({__name__=~".+"}))

# Top 10 label values driving series growth on a specific metric
topk(10, count by (pod) (DCGM_FI_PROF_SM_ACTIVE))
```

The Prometheus UI's `/api/v1/status/tsdb` endpoint exposes a more detailed breakdown — top metrics, top label names, top label values.

In Mimir/Thanos, the equivalent is `/api/v1/cardinality/labels` and `/api/v1/cardinality/values` (Mimir 2.x+).

---

## 7. Remote Write Tuning

For remote_write to Mimir/Thanos at GPU scale, the defaults are wrong. Tune toward:

```yaml
remote_write:
  - url: https://mimir/api/v1/push
    queue_config:
      capacity: 20000              # samples per shard buffer
      max_samples_per_send: 5000   # batch size
      max_shards: 500              # parallel shards
      min_shards: 100              # don't downscale aggressively
      batch_send_deadline: 5s      # max wait before sending partial batch
    write_relabel_configs:
      # Don't ship raw 15s data to LTS — only rollups + alert-relevant
      - source_labels: [__name__]
        regex: "DCGM_FI_PROF_.*"
        action: drop
      - source_labels: [__name__]
        regex: "(.+:avg5m|.+:avg1h|cluster:.+|namespace:.+|team:.+)"
        action: keep
```

The `write_relabel_configs` is the single most-impactful tuning: don't remote_write raw 15s DCGM samples. Ship the recording rule outputs only. Reduces remote_write volume by ~95% and proportionally reduces LTS storage.

### 7.1 Memory rule of thumb

```
remote_write memory ≈ shard_count × (capacity + max_samples_per_send) × series_size
                   ≈ 500 × (20k + 5k) × 100 bytes
                   ≈ 1.25 GB
```

If your Prometheus pod is OOM'ing, this is where to look — too many shards × too much capacity.

### 7.2 WAL compression

Always on:

```yaml
storage:
  tsdb:
    wal_compression: true
```

Snappy compression reduces WAL disk I/O ~2× with negligible CPU cost. There's no reason to disable.

---

## 8. Retention Tier Configuration

Prometheus local TSDB retention:

```yaml
# Hot tier: 6h-2d for live alerting + drilldown
storage:
  tsdb:
    retention.time: 2d
    retention.size: 50GB
    out_of_order_time_window: 5m
```

Mimir/Thanos retention via compactor:

```yaml
# Mimir compactor tiers
limits:
  compactor_blocks_retention_period: 13mo
  compactor_block_ranges:
    - 2h    # raw blocks (downsampled to 5m at 12h)
    - 12h
    - 24h

# Mimir downsampling
ruler:
  evaluation_interval: 1m

querier:
  query_store_after: 12h    # raw stays in ingester for 12h
```

The architecture in doc 01 §5 with Mimir 3.0's Kafka decoupling lets you tune writers and readers independently. With Thanos, downsampling happens in the compactor, so the query path automatically reaches for 5m rollups for queries spanning > 1d.

---

## 9. The Cardinality Cost of Debugging

A pattern that bites: an SRE adds `pod` and `container` labels temporarily for a debugging session, forgets to revert. Two weeks later Prometheus is OOM'ing.

Counter-pattern: tag your relabel configs with comments and run a quarterly review query:

```promql
# Series count per scrape job — anyone trending up is suspect
count by (job) ({__name__=~".+"})
```

Send the snapshot to a static asset (e.g., commit it to the repo) every quarter. Diff against last quarter's. Big deltas are deliberate or are bugs.

---

## 10. Federation / Sharding Strategies

If you've followed the rules and still need >10M series, you have three tactical options:

### 10.1 Vertical Prometheus split

Run multiple Prometheus instances, each scraping a *subset* of jobs:

```
prometheus-gpu-hardware  → dcgm-exporter only
prometheus-gpu-workloads → vllm, triton, training metrics
prometheus-k8s-state     → kube-state-metrics, node-exporter
```

Each instance is bounded; queries that need cross-instance use Mimir/Thanos.

### 10.2 Horizontal sharding (`hashmod`)

```yaml
relabel_configs:
  - source_labels: [__address__]
    modulus: 4
    target_label: __tmp_hash
    action: hashmod
  - source_labels: [__tmp_hash]
    regex: "^0$"  # this Prometheus shard takes hash 0 only
    action: keep
```

Run 4 Prometheus instances, each takes 1/4 of the targets. Quadruples capacity at 4× the ops cost.

### 10.3 Mimir from day one

The unspoken option: skip the Prometheus-on-its-own scaling exercise. Run small "agent-mode" Prometheus or Grafana Agent on each cluster, remote_write everything to Mimir, do all queries against Mimir. Highest ops floor but cleanest scale story.

---

## 11. Acceptance Checklist

You've finished cardinality plumbing when:

- [ ] `prometheus_tsdb_head_series` is stable for 1 week
- [ ] No metric has >100k series (or you've documented why)
- [ ] All standard recording rules from §4 are deployed
- [ ] Remote_write `write_relabel_configs` ships only rollups
- [ ] WAL compression on
- [ ] Cardinality drift alert defined and quiet
- [ ] LTS retention tiers defined: 15d raw, 90d 5m, 13mo 1h
- [ ] Quarterly cardinality review process documented

---

## 12. Forward Pointers

- **Doc 09**: dashboards query the recording rules from §4
- **Doc 10**: alerts query both raw and recording-rule series
- **Doc 12**: capacity planning lives entirely on the 1h rollups
- **Doc 13**: per-tenant cardinality limits in Mimir

---

## Sources

- [Prometheus — Remote Write Tuning](https://prometheus.io/docs/practices/remote_write/)
- [Last9 — How to scale Prometheus remote write](https://last9.io/blog/how-to-scale-prometheus-remote-write/)
- [Last9 — How to manage high cardinality metrics](https://last9.io/blog/how-to-manage-high-cardinality-metrics-in-prometheus/)
- [Grafana — High cardinality metrics in Prometheus and Kubernetes](https://grafana.com/blog/how-to-manage-high-cardinality-metrics-in-prometheus-and-kubernetes/)
- [VictoriaMetrics — Relabeling cookbook](https://docs.victoriametrics.com/victoriametrics/relabeling/)
- [Last9 — 10,000 GPUs, One TSDB](https://last9.io/blog/10-000-gpus-one-tsdb-cardinality-at-gpu-scale/)
- [Better Stack — Relabeling guide](https://betterstack.com/community/guides/monitoring/prometheus-relabeling/)
