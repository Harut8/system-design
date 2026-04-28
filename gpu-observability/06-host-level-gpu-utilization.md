# 06 — Host-Level GPU Utilization

> The fleet view (doc 05) tells you which teams waste GPUs. The host view tells you *why a specific GPU on a specific node is slow right now*. Different audiences, different metrics.

This is the SRE drilldown level: when an inference service breaches p99 or a training rank lags, you arrive on the host dashboard for that node and these are the metrics you read in order.

---

## 1. The 8 Per-Host Metrics That Diagnose 90% of Slowness

When a node-scoped GPU issue is reported, walk this list top to bottom. Each panel either resolves the case or escalates to the next.

| # | Signal | Tells you |
|---|--------|-----------|
| 1 | `DCGM_FI_DEV_CLOCK_THROTTLE_REASONS` | Is the GPU actively being clocked down? |
| 2 | `DCGM_FI_DEV_GPU_TEMP`, `DCGM_FI_DEV_MEMORY_TEMP` | Thermal — die vs HBM separately |
| 3 | `DCGM_FI_DEV_POWER_USAGE` vs TDP | Power-limited? |
| 4 | `DCGM_FI_DEV_PCIE_LINK_GEN`, `DCGM_FI_DEV_PCIE_LINK_WIDTH` | PCIe link degraded? |
| 5 | `DCGM_FI_PROF_PCIE_RX_BYTES`, `_TX_BYTES` | PCIe saturated? |
| 6 | NVLink replay/recovery/CRC error rates | NVLink degraded? |
| 7 | NUMA affinity (CPU socket vs GPU) | Wrong NUMA placement? |
| 8 | Pinned host memory, IOMMU pressure | Host bottleneck? |

The order is deliberate — fast-to-check, fast-to-resolve first. Only get to NUMA after the obvious thermal/power/link issues are ruled out.

---

## 2. Per-GPU, Per-Node Metric Hygiene

A standard HGX node has 8 GPUs. You want all 8 individually visible:

```promql
# Per-GPU temperature on a single node
DCGM_FI_DEV_GPU_TEMP{Hostname="gpu-014"}
```

Returns 8 series, one per `gpu` label (0..7). The L3 (host) dashboard is grids of 8 panels, each with all 8 lines plus a max-line for "worst GPU on this node."

Cardinality budget: 8 GPUs × 30 metrics × 1 node = 240 series. Times nodes for the cluster-wide view. Stays comfortable up to 1k nodes (240k series), which is well within Prometheus single-replica limits.

---

## 3. PCIe — The Most-Missed Bottleneck

PCIe Gen4 x16 = 32 GB/s theoretical per direction, ~28 GB/s practical. PCIe Gen5 x16 = 64 GB/s theoretical. H100 sits on PCIe Gen5; H200/B200 also Gen5.

Two failure modes you must monitor:

### 3.1 Link width / generation degradation

Sometimes a link trains down to Gen3 or x8 due to a marginal connector or thermal event. The GPU is fine, but bandwidth is permanently halved until reboot.

```promql
# Alert: PCIe link below expected
DCGM_FI_DEV_PCIE_LINK_GEN < 5  # or < 4 for older HW
DCGM_FI_DEV_PCIE_LINK_WIDTH < 16
```

This is one of the cheapest, most-impactful alerts you can write. We've seen multi-percent training throughput regressions from a single Gen3 GPU in an 8-GPU node (the slow GPU drags AllReduce).

### 3.2 PCIe saturation

When data-loader or H2D copies max out PCIe, GPU starves:

```promql
# PCIe link utilization (Gen5 x16 = 64 GB/s peak)
rate(DCGM_FI_PROF_PCIE_RX_BYTES[1m]) / (64 * 1024^3)
rate(DCGM_FI_PROF_PCIE_TX_BYTES[1m]) / (64 * 1024^3)

# Combined when both directions are active
(rate(DCGM_FI_PROF_PCIE_RX_BYTES[1m]) + rate(DCGM_FI_PROF_PCIE_TX_BYTES[1m]))
/ (64 * 1024^3 * 2)  # full-duplex peak
```

The asymmetric-contention reality (per NVIDIA forums): bidirectional H2D+D2H reduces each direction's bandwidth by ~25–55% even on Gen4. A workload that's I/O-bound during training data loading often sees this exact pattern. Don't trust the per-direction max; watch the *bidirectional* rate.

### 3.3 The signature

If `SM_ACTIVE` is low *and* `PCIE_RX_BYTES` is at 80%+ of peak, you're PCIe-starved. The fix is workload-side (larger batches to amortize H2D, prefetch, GPUDirect Storage if available). The metric just tells you it's the bottleneck.

---

## 4. NVLink — Per-Link, Per-Direction

NVLink fabric on an HGX H100 baseboard: every pair of GPUs connected via 18 NVLink-4 links bonded (`NV18` in `nvidia-smi topo -m`), giving ~900 GB/s per direction per pair.

### 4.1 Bandwidth utilization

```promql
# Per-link NVLink RX bandwidth utilization
sum by (Hostname, gpu, nvlink) (
  rate(DCGM_FI_PROF_NVLINK_RX_BYTES[1m])
) / (25 * 1024^3)  # NVLink-4 = 25 GB/s/link
```

Healthy AllReduce during distributed training: 70–95% utilization on every link, all GPUs symmetric. Asymmetric utilization (one link at 90%, others at 30%) means NCCL didn't pick a balanced ring/tree — usually a topology mismatch.

### 4.2 Error counters — leading indicator of degradation

These are the most underrated metrics in the stack:

| Counter | Meaning |
|---------|---------|
| `DCGM_FI_DEV_NVLINK_REPLAY_ERROR_COUNT_TOTAL` | Transient packet replay (link recovered) |
| `DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL` | Link reset event |
| `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL` | Per-flit CRC error (data corruption) |
| `DCGM_FI_DEV_NVLINK_CRC_DATA_ERROR_COUNT_TOTAL` | Data CRC error |

```promql
# Recovery error rate per link
rate(DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL[5m])
```

A healthy fabric: zero recovery errors for days/weeks. A degraded fabric: increasing rate, often on one specific link first. This is the *predictive* signal — degraded links cause AllReduce slowdowns hours/days before they fail.

Rule of thumb: > 1 recovery/hour on a single link → ticket; > 10/hour → page; > 100/hour → that link is dying.

### 4.3 Cross-cutting NVLink dashboard panel

The "fabric heatmap" — a matrix of all GPU pairs on a node × bandwidth or error rate. Reveals fabric-wide vs link-specific issues:

```promql
# Per-pair bandwidth (synthetic, requires topology metadata)
nvlink_pair_bandwidth_bytes_per_sec{
  Hostname="gpu-014",
  src_gpu="0",
  dst_gpu="1"
}
```

Doc 09 has the dashboard JSON.

---

## 5. NUMA Affinity Correctness

On a dual-socket HGX node, GPUs split between NUMA nodes:

```
Socket 0 (NUMA 0): CPUs 0-47    GPUs 0-3
Socket 1 (NUMA 1): CPUs 48-95   GPUs 4-7
```

A process on CPU 5 talking to GPU 6 traverses the QPI/UPI link → significant latency, halved bandwidth. NCCL pinning fixes this; misconfigured pods don't.

### 5.1 What to check

Per pod:

```bash
# Inside the pod
cat /proc/self/status | grep ^Cpus_allowed_list
nvidia-smi topo -m
```

Cross-reference: which CPUs is the process allowed on, and which NUMA node owns the GPU? If they don't match, you have a misconfiguration.

### 5.2 Synthetic metric

A small init container or sidecar can emit:

```promql
# 1 if mismatch, 0 if correct
gpu_numa_misalignment{
  Hostname="gpu-014",
  pod="train-0",
  gpu="6"
} 1
```

Then a cluster-wide alert:

```promql
sum(gpu_numa_misalignment) > 0
```

Most clusters have nonzero misalignment. The fix is the kubelet's Topology Manager (`policy: single-numa-node`) or pod-level `topologyManagerPolicy` plus correct CPU manager static policy.

### 5.3 The cost of getting it wrong

For LLM inference workloads with heavy host↔device traffic (KV cache resharding, embedding lookups), NUMA mis-pinning costs 10–30% of throughput. For pure-compute training that lives entirely in HBM, the cost is closer to 1–5%. Either way, free performance for fixing config.

---

## 6. Network — InfiniBand / RoCE Fabric

The third interconnect tier (after PCIe within node, NVLink between GPUs on a node): network *between* nodes for multi-node training.

NCCL prefers RDMA over TCP for collective ops. The standard fabric on production GPU clusters is InfiniBand (NDR 400 Gb/s) or RoCEv2 over Ethernet (Spectrum-X). NCCL drives traffic at near-line-rate during AllReduce.

### 6.1 Metrics to scrape

From `node-exporter` with `--collector.infiniband`:

| Metric | Use |
|--------|-----|
| `node_infiniband_port_data_received_bytes_total` | RX bytes per port |
| `node_infiniband_port_data_transmitted_bytes_total` | TX bytes per port |
| `node_infiniband_port_excessive_buffer_overrun_errors_total` | Buffer overruns |
| `node_infiniband_port_link_downed_total` | Link bounces |
| `node_infiniband_port_local_link_integrity_errors_total` | Local errors |
| `node_infiniband_port_packet_seq_err_total` | Packet sequence errors |
| `node_infiniband_port_symbol_error_total` | Symbol errors |

Healthy IB fabric during AllReduce should show:

- TX ≈ RX (collective is symmetric)
- Each port near link rate (e.g., 50 GB/s for NDR x4)
- Zero or near-zero error counters

### 6.2 Detection: network-starved collectives

A telling signature of network bottleneck:

```promql
# AllReduce is slow AND IB is saturated AND NVLink is idle
DCGM_FI_PROF_NVLINK_TX_BYTES < 10e9   # NVLink mostly idle
  and on(Hostname)
rate(node_infiniband_port_data_transmitted_bytes_total[1m]) > 40 * 1024^3  # IB saturated
```

Translation: the cross-node AllReduce is bottlenecked on network, not compute. Mitigations are sharding/topology decisions (rail-optimized fabric, hierarchical AllReduce), not hardware.

### 6.3 Network errors that page

| Error | Severity |
|-------|----------|
| `link_downed_total` increasing | Page — fabric is bouncing |
| `excessive_buffer_overrun` | Page — congestion control failing |
| `symbol_error` rate increasing | Ticket — cable/transceiver degrading |
| `packet_seq_err` | Ticket — usually transient |

---

## 7. Host Memory and IOMMU

The most-overlooked layer. A misconfigured pinned-memory setup throttles all H2D copies:

### 7.1 Pinned (page-locked) memory

CUDA `cudaMallocHost`/PyTorch `pin_memory=True` allocates host memory the kernel won't page out, enabling DMA without staging. If pinned-memory allocation fails (limit hit, fragmentation), kernels fall back to staged copies → 2–3× slower H2D.

```promql
# Host memory pressure
node_memory_MemFree_bytes / node_memory_MemTotal_bytes < 0.05
```

Plus container-level limits: `kube_pod_container_resource_requests{resource="memory"}` should leave headroom for pinned allocation.

### 7.2 Transparent HugePages (THP)

Counterintuitive: THP can hurt GPU workloads because the kernel's defrag interrupts blocking pinned-memory allocation. Production GPU nodes typically run with `transparent_hugepage=madvise` or `=never`.

Detection from node-exporter:

```promql
node_vmstat_thp_collapse_alloc  # rate of THP collapses
node_vmstat_thp_split_pmd       # PMD splits
```

High rates correlate with H2D latency spikes.

### 7.3 IOMMU pass-through vs translation

Default IOMMU mode adds DMA address translation overhead (~5–15% on H2D). For GPU clusters, set `iommu=pt` (passthrough) on kernel cmdline.

This isn't a real-time metric — it's a config-correctness check via node-exporter `node_kernel_cmdline`:

```promql
# All nodes have iommu=pt set
absent(node_kernel_cmdline{cmdline=~".*iommu=pt.*"})
```

If the alert fires for any node, the fleet-wide IOMMU policy isn't applied uniformly.

---

## 8. Thermal and Power — Per-Host Detail

Doc 07 covers ECC and hardware health alerts comprehensively. The host-view subset:

### 8.1 Per-GPU power

```promql
# Power draw vs TDP per GPU on a node
DCGM_FI_DEV_POWER_USAGE{Hostname="gpu-014"}
/ DCGM_FI_DEV_POWER_DEFAULT_LIMIT  # TDP for this model
```

H100 SXM TDP: 700W. H200 SXM: 700W. B200: 1000W (configurable). At sustained > 95% of TDP under load, you're power-throttling territory.

### 8.2 Junction vs memory temperature

GPU "temperature" is multiple sensors. The two that matter:

| Sensor | Field | Throttle threshold |
|--------|-------|-------------------|
| GPU die (junction) | `DCGM_FI_DEV_GPU_TEMP` | ~83–87°C depending on model |
| HBM (memory) | `DCGM_FI_DEV_MEMORY_TEMP` | ~95°C HBM3, 85°C HBM3e |

HBM is often the *real* limiter. A GPU showing die temp at 70°C might be HBM-throttling at 95°C. Always plot both.

### 8.3 Throttle reason decode

The throttle bitfield (`DCGM_FI_DEV_CLOCK_THROTTLE_REASONS`):

| Bit | Reason | Severity |
|-----|--------|----------|
| 0x01 | GPU idle | Informational |
| 0x02 | Application clock setting | User-configured |
| 0x04 | SW power cap | Throttled by user-set power cap |
| 0x08 | HW slowdown (thermal) | **Critical** — alert |
| 0x10 | Sync boost | OK |
| 0x20 | SW thermal slowdown | Warning |
| 0x40 | HW thermal slowdown | **Critical** — page |
| 0x80 | HW power brake slowdown | **Critical** — page |
| 0x100 | Display clock setting | N/A on data center GPUs |

Decode in PromQL with bitwise ops (Prometheus 2.41+):

```promql
# HW thermal slowdown observed
(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS & 0x40) > 0
```

Or emit pre-decoded fields from your relabel config.

---

## 9. Topology-Aware Affinity Scoring

For multi-GPU pods, a quality score for the placement:

```
score = sum over (gpu_pair_in_pod) of:
  +10 if NV18 (full NVLink)
  +5 if NV6 (partial NVLink)
  -2 if PIX (PCIe switch)
  -10 if SYS (cross-NUMA)
```

Computed by the topology exporter (doc 03 §4) and emitted as `pod_gpu_topology_score`. The dashboard panel ranks pods by score; bad placements bubble to the top for re-scheduling.

---

## 10. The "Slow GPU on This Node" Runbook

The exact sequence when paged:

```
1. open L3 host dashboard for this node
2. check throttle reasons panel
   - HW thermal? → check fan curves, ambient, PSU; ticket facilities
   - SW power cap? → check power policy; revert
   - none? → continue
3. check temperature panel
   - HBM > 95°C? → workload too aggressive; throttle batch size or schedule less hot
   - die > 87°C? → cooling issue
4. check PCIe link gen/width
   - downgraded? → reboot the node; if recurs, RMA
5. check NVLink errors
   - rising recovery rate on one link? → drain workload, run nvlink diagnostics
6. check NUMA affinity panel
   - misalignment? → fix kubelet topology manager
7. check IB error counters
   - link bouncing? → escalate to fabric team
8. last: check workload-side metrics (data loader stalls, etc.)
```

This runbook lives next to the dashboard in Grafana annotations. Doc 10 covers alert routing so the right person sees the right runbook.

---

## 11. Acceptance Checklist

You're ready for Phase 3 when:

- [ ] Per-GPU panels render for every node (8 GPUs visible)
- [ ] PCIe link gen/width alert defined and quiet (no Gen3 surprises)
- [ ] NVLink error rate panels show flat-zero baseline
- [ ] NUMA misalignment metric exists (even if it's nonzero today — at least it's measured)
- [ ] InfiniBand or RoCE port metrics scraped from node-exporter
- [ ] Throttle reasons decoded into individual panels
- [ ] HBM temp plotted separately from die temp
- [ ] Host memory pressure correlated to H2D latency in at least one dashboard

---

## 12. Forward Pointers

- **Doc 07**: ECC, XID, RMA workflow — hardware health alerting end-to-end
- **Doc 09**: L3 host dashboard JSON
- **Doc 10**: Throttle/thermal alert routing
- **Doc 15**: NVLink/IB metrics joined to per-rank training step time

---

## Sources

- [NVIDIA — NVLink overview](https://en.wikipedia.org/wiki/NVLink)
- [NVIDIA Forums — Asymmetric PCIe bandwidth (H2D drops 56%)](https://forums.developer.nvidia.com/t/asymmetric-pcie-bandwidth-in-bidirectional-transfers-h2d-drops-56-while-d2h-maintains-performance/352186)
- [NVIDIA Forums — Bandwidth contention of concurrent H2D & D2H](https://forums.developer.nvidia.com/t/bandwidth-contention-of-concurrent-h2d-d2h-memory-copy/250140)
- [Chaim Rand — NUMA Awareness in High-Performance Deep Learning](https://chaimrand.medium.com/the-crucial-role-of-numa-awareness-in-high-performance-deep-learning-99ae3e8eb49a)
- [AMD GPUOpen — Affinity, placement, and order](https://gpuopen.com/learn/amd-lab-notes/amd-lab-notes-affinity-affinity_part1/)
- [NVIDIA — AI Fabric Resiliency](https://developer.nvidia.com/blog/ai-fabric-resiliency-and-why-network-convergence-matters/)
- [Nebius — Building reliable clusters for distributed AI](https://nebius.com/blog/posts/how-we-build-reliable-clusters)
- [DEV — How PCIe, NVLink, and NUMA affect GPU scheduling](https://dev.to/daya-shankar/how-pcie-nvlink-and-numa-topology-affect-gpu-scheduling-outcomes-l52)
