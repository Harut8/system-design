# Appendix B — DCGM Field-ID Cheat Sheet

> Every `DCGM_FI_*` field you'll touch, grouped by family, with one-line meanings, units, types, and which chapter explains it. Use this as a lookup when reading code or alert YAML — not as a learning resource (start with Doc 02 for that).

The full DCGM API exposes ~250 fields. The ones below are **the production-relevant subset** — what dcgm-exporter ConfigMaps usually emit, with the trade-offs noted.

**Type column legend:**
- `NVML` — driver-backed counter, always available, cheap to poll, no exclusivity
- `PROF` — profiling subsystem, requires the profiling lock (Doc 00 §6)
- `HEALTH` — DCGM health-check field, emitted by `dcgmHealthSet` watch groups
- `LIFECYCLE` — derived from DCGM events, not raw counters

---

## B.1 GPU Activity (the headline numbers)

| Field | ID | Type | Unit | Meaning | Chapter |
|-------|----|----|------|---------|---------|
| `DCGM_FI_DEV_GPU_UTIL` | 203 | NVML | % | "GPU was busy" — 1 active warp = 100%. Misleading; keep for legacy compat only. | Doc 00 §3, Doc 02 §2.1 |
| `DCGM_FI_PROF_GR_ENGINE_ACTIVE` | 1001 | PROF | ratio 0–1 | Graphics/compute engine busy fraction. The honest version of GPU util. | Doc 02 §2.1 |
| `DCGM_FI_PROF_SM_ACTIVE` | 1002 | PROF | ratio 0–1 | **Fraction of SM cycles with ≥1 warp scheduled. The number to dashboard.** | Doc 00 §3, Doc 02 §2.1 |
| `DCGM_FI_PROF_SM_OCCUPANCY` | 1003 | PROF | ratio 0–1 | Avg fraction of warp slots filled when SM is active. Saturation signal. | Doc 02 §2.1 |
| `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | 1004 | PROF | ratio 0–1 | Tensor core utilization. The reason ML teams paid for H100s. | Doc 02 §2.1 |
| `DCGM_FI_PROF_PIPE_FP64_ACTIVE` | 1006 | PROF | ratio 0–1 | FP64 pipe activity (HPC workloads). | Doc 02 §2.1 |
| `DCGM_FI_PROF_PIPE_FP32_ACTIVE` | 1007 | PROF | ratio 0–1 | FP32 pipe (CUDA cores) activity. | Doc 02 §2.1 |
| `DCGM_FI_PROF_PIPE_FP16_ACTIVE` | 1008 | PROF | ratio 0–1 | FP16/BF16 pipe (non-tensor mixed-precision). | Doc 02 §2.1 |

---

## B.2 Memory & Bandwidth

| Field | ID | Type | Unit | Meaning | Chapter |
|-------|----|------|------|---------|---------|
| `DCGM_FI_DEV_FB_TOTAL` | 250 | NVML | MiB | HBM capacity (constant per GPU model). | Doc 02 §2.2 |
| `DCGM_FI_DEV_FB_FREE` | 251 | NVML | MiB | HBM free bytes. | Doc 02 §2.2 |
| `DCGM_FI_DEV_FB_USED` | 252 | NVML | MiB | HBM used bytes. | Doc 02 §2.2 |
| `DCGM_FI_DEV_FB_RESERVED` | 253 | NVML | MiB | Reserved (driver, page tables). | Doc 02 §2.2 |
| `DCGM_FI_PROF_DRAM_ACTIVE` | 1005 | PROF | ratio 0–1 | **HBM bandwidth utilization. Memory- vs compute-bound discriminator.** | Doc 00 §3, Doc 02 §2.2 |
| `DCGM_FI_PROF_PCIE_RX_BYTES` | 1011 | PROF | bytes/sec | PCIe receive throughput. | Doc 02 §2.2, Doc 06 §5 |
| `DCGM_FI_PROF_PCIE_TX_BYTES` | 1012 | PROF | bytes/sec | PCIe transmit throughput. | Doc 02 §2.2, Doc 06 §5 |
| `DCGM_FI_PROF_NVLINK_RX_BYTES` | 1009 | PROF | bytes/sec | NVLink receive bandwidth (per-link aggregate). | Doc 02 §2.2, Doc 06 §4 |
| `DCGM_FI_PROF_NVLINK_TX_BYTES` | 1010 | PROF | bytes/sec | NVLink transmit bandwidth (per-link aggregate). | Doc 02 §2.2, Doc 06 §4 |

> **Diagnostic patterns:**
> - `DRAM_ACTIVE` near 1.0, `SM_ACTIVE` low → HBM-bound (LLM decode, embedding lookup)
> - `PCIE_RX/TX` saturated → host↔device transfers starving the GPU
> - `NVLINK_RX/TX` near peak → tensor-parallel or AllReduce-heavy

---

## B.3 ECC, Retired Pages, Faults

| Field | ID | Type | Unit | Meaning | Chapter |
|-------|----|------|------|---------|---------|
| `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL` | 312 | NVML | count | Single-bit ECC, volatile total (since boot). | Doc 02 §2.3, Doc 07 §3 |
| `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL` | 313 | NVML | count | Double-bit ECC, volatile total. | Doc 07 §3 |
| `DCGM_FI_DEV_ECC_SBE_AGG_TOTAL` | 314 | NVML | count | Single-bit, aggregate (across reboots). | Doc 07 §3 |
| `DCGM_FI_DEV_ECC_DBE_AGG_TOTAL` | 315 | NVML | count | Double-bit, aggregate. | Doc 07 §3 |
| `DCGM_FI_DEV_RETIRED_SBE` | 391 | NVML | count | Pages retired due to SBE accumulation. | Doc 07 §3 |
| `DCGM_FI_DEV_RETIRED_DBE` | 392 | NVML | count | **Pages retired due to DBE — RMA candidate.** | Doc 07 §3 |
| `DCGM_FI_DEV_RETIRED_PENDING` | 393 | NVML | count | Pending page retirement (next reboot). | Doc 07 §3 |
| `DCGM_FI_DEV_ROW_REMAP_FAILURE` | 393+ | NVML | count | H100+: row remap failed — fatal, RMA. | Doc 07 §3 |
| `DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS` | 394 | NVML | count | H100+: uncorrectable rows that have been remapped. | Doc 07 §3 |
| `DCGM_FI_DEV_CORRECTABLE_REMAPPED_ROWS` | 395 | NVML | count | H100+: correctable rows remapped. | Doc 07 §3 |
| `DCGM_FI_DEV_XID_ERRORS` | 230 | NVML | code | Last XID error code observed (latched). | Doc 02 §2.3, Doc 07 §2 |

> **Lifecycle:** SBE rate spike → page retirement → DBE → RMA. **Alert on SBE *velocity* (rate), not absolute count** — old GPUs accumulate SBEs over years.

---

## B.4 Power, Thermal, Throttle

| Field | ID | Type | Unit | Meaning | Chapter |
|-------|----|------|------|---------|---------|
| `DCGM_FI_DEV_POWER_USAGE` | 155 | NVML | W | Instantaneous power draw. | Doc 02 §2.4, Doc 06 §6 |
| `DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION` | 157 | NVML | mJ (counter) | Energy counter. Use `rate()` for true average power. | Doc 02 §2.4 |
| `DCGM_FI_DEV_POWER_MGMT_LIMIT` | 159 | NVML | W | Configured power cap. | Doc 06 §6 |
| `DCGM_FI_DEV_GPU_TEMP` | 150 | NVML | °C | GPU die temperature. | Doc 02 §2.4 |
| `DCGM_FI_DEV_MEMORY_TEMP` | 156 | NVML | °C | **HBM temperature — separate from die, often the actual thermal limiter.** | Doc 02 §2.4, Doc 06 §6 |
| `DCGM_FI_DEV_GPU_MAX_OP_TEMP` | 151 | NVML | °C | Max operating temperature (constant). | Doc 02 §2.4 |
| `DCGM_FI_DEV_CLOCK_THROTTLE_REASONS` | 112 | NVML | bitfield | **Throttle reasons. The first thing to check when "GPU is slow."** | Doc 02 §2.4, Doc 06 §6 |
| `DCGM_FI_DEV_SM_CLOCK` | 100 | NVML | MHz | Current SM clock. | Doc 02 §2.4 |
| `DCGM_FI_DEV_MEM_CLOCK` | 101 | NVML | MHz | Current memory clock. | Doc 02 §2.4 |

### B.4.1 Throttle reasons bitfield decode

| Bit | Hex | Reason | Meaning |
|-----|-----|--------|---------|
| 0 | `0x01` | GPU_IDLE | GPU is idle, clocks lowered (normal) |
| 1 | `0x02` | APPLICATIONS_CLOCKS_SETTING | Application set custom clocks |
| 2 | `0x04` | SW_POWER_CAP | Software power cap (`nvidia-smi -pl`) |
| 3 | `0x08` | HW_SLOWDOWN | Hardware-enforced (catch-all) |
| 4 | `0x10` | SYNC_BOOST | Multi-GPU sync boost active |
| 5 | `0x20` | SW_THERMAL | Software thermal throttle |
| 6 | `0x40` | **HW_THERMAL** | **Hardware thermal throttle — investigate cooling** |
| 7 | `0x80` | **HW_POWER_BRAKESLOWDOWN** | **External power brake — PSU or PDU issue** |
| 8 | `0x100` | DISPLAY_CLOCK_SETTING | Display setting (irrelevant for compute) |

PromQL example:

```promql
# Decode HW thermal throttle
(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS & 0x40) > 0

# Any "bad" throttle (not idle/sync_boost/display)
(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS & 0x1FC) > 0
```

---

## B.5 NVLink Health

| Field | ID | Type | Unit | Meaning | Chapter |
|-------|----|------|------|---------|---------|
| `DCGM_FI_DEV_NVLINK_REPLAY_ERROR_COUNT_TOTAL` | 449 | NVML | count | Replay errors (recoverable). | Doc 06 §4, Doc 07 §4 |
| `DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL` | 450 | NVML | count | Recovery errors (link bounced). | Doc 06 §4, Doc 07 §4 |
| `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL` | 451 | NVML | count | CRC flit errors (data corruption). | Doc 06 §4, Doc 07 §4 |
| `DCGM_FI_DEV_NVLINK_CRC_DATA_ERROR_COUNT_TOTAL` | 452 | NVML | count | CRC data errors. | Doc 07 §4 |
| `DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL` | 453 | NVML | MB | Aggregate NVLink BW used. | Doc 06 §4 |

> Per-link variants exist (`*_L0` through `*_L17` for H100). Often emitted only when a link goes hot — check Doc 02 §3 for ConfigMap pattern.

---

## B.6 PCIe Health

| Field | ID | Type | Unit | Meaning | Chapter |
|-------|----|------|------|---------|---------|
| `DCGM_FI_DEV_PCIE_REPLAY_COUNTER` | 202 | NVML | count | PCIe replay events (link errors). | Doc 06 §5 |
| `DCGM_FI_DEV_PCIE_LINK_GEN` | 226 | NVML | int | Current PCIe generation (4, 5, 6). | Doc 06 §5 |
| `DCGM_FI_DEV_PCIE_LINK_WIDTH` | 227 | NVML | int | Current PCIe lane width (x16 normal). | Doc 06 §5 |
| `DCGM_FI_DEV_PCIE_MAX_LINK_GEN` | 228 | NVML | int | Max supported gen (constant). | Doc 06 §5 |
| `DCGM_FI_DEV_PCIE_MAX_LINK_WIDTH` | 229 | NVML | int | Max supported width (constant). | Doc 06 §5 |

> Alert on `LINK_GEN < MAX_LINK_GEN` or `LINK_WIDTH < MAX_LINK_WIDTH` — silent degradation that halves your host↔device bandwidth.

---

## B.7 Encoder/Decoder (NVDEC/NVENC)

| Field | ID | Type | Unit | Meaning | Chapter |
|-------|----|------|------|---------|---------|
| `DCGM_FI_DEV_ENC_UTIL` | 206 | NVML | % | Hardware video encoder utilization. | — |
| `DCGM_FI_DEV_DEC_UTIL` | 207 | NVML | % | Hardware video decoder utilization. | — |

> Mostly relevant for video pipelines and Cloud Gaming. Skip for ML clusters.

---

## B.8 MIG (Multi-Instance GPU) Identity

| Field | ID | Type | Unit | Meaning | Chapter |
|-------|----|------|------|---------|---------|
| `DCGM_FI_DEV_MIG_MODE` | 75 | NVML | int | 0 = MIG disabled, 1 = enabled. | Doc 13 §3 |
| `DCGM_FI_DEV_MIG_MAX_SLICES` | 76 | NVML | int | Max GIs supported on this GPU. | Doc 13 §3 |
| `DCGM_FI_DEV_MIG_GI_INFO` | various | NVML | metadata | Per-GI metadata: GI ID, profile, SM count, mem size. | Doc 13 §3 |

> When MIG is enabled, **most fields above are emitted per-GI**, with extra labels `GPU_I_ID`, `GPU_I_PROFILE` (e.g., `1g.10gb`). Cardinality multiplier × #GIs.

---

## B.9 Health & Lifecycle (DCGM-derived)

| Field | ID | Type | Meaning | Chapter |
|-------|----|------|---------|---------|
| `DCGM_FI_DEV_GPU_HEALTH` | 230+ | HEALTH | Bitfield: PCIE_OK, NVLINK_OK, PMU_OK, MCU_OK, MEM_OK, SM_OK, INFOROM_OK, THERMAL_OK, POWER_OK | Doc 07 §5 |
| `gpu_lifecycle_state` (custom) | — | LIFECYCLE | Custom-emitted: `healthy`, `degraded`, `pre_rma`, `in_rma`. | Doc 07 §6 |
| `gpu_xid_total` (custom) | — | LIFECYCLE | Custom counter: XID events per code. | Doc 07 §2 |

> The `gpu_lifecycle_state` and `gpu_xid_total` are **not** built into DCGM — they're emitted by a sidecar or a parser script that watches dmesg + DCGM events. See Doc 07 §6.2 for the implementation.

---

## B.10 Recommended Production ConfigMap

A working baseline that emits **everything in this cheat sheet** with reasonable cardinality:

```csv
# dcgm-exporter ConfigMap — production baseline
# Format: <fieldId>, <metricName>, <description>

# Activity
1001, DCGM_FI_PROF_GR_ENGINE_ACTIVE, gauge
1002, DCGM_FI_PROF_SM_ACTIVE, gauge
1003, DCGM_FI_PROF_SM_OCCUPANCY, gauge
1004, DCGM_FI_PROF_PIPE_TENSOR_ACTIVE, gauge
1005, DCGM_FI_PROF_DRAM_ACTIVE, gauge
1007, DCGM_FI_PROF_PIPE_FP32_ACTIVE, gauge
1008, DCGM_FI_PROF_PIPE_FP16_ACTIVE, gauge

# Memory & bandwidth
250, DCGM_FI_DEV_FB_TOTAL, gauge
251, DCGM_FI_DEV_FB_FREE, gauge
252, DCGM_FI_DEV_FB_USED, gauge
1009, DCGM_FI_PROF_NVLINK_RX_BYTES, counter
1010, DCGM_FI_PROF_NVLINK_TX_BYTES, counter
1011, DCGM_FI_PROF_PCIE_RX_BYTES, counter
1012, DCGM_FI_PROF_PCIE_TX_BYTES, counter

# Thermal & power
150, DCGM_FI_DEV_GPU_TEMP, gauge
156, DCGM_FI_DEV_MEMORY_TEMP, gauge
155, DCGM_FI_DEV_POWER_USAGE, gauge
157, DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION, counter
112, DCGM_FI_DEV_CLOCK_THROTTLE_REASONS, gauge
100, DCGM_FI_DEV_SM_CLOCK, gauge
101, DCGM_FI_DEV_MEM_CLOCK, gauge

# Health: ECC, retirement
312, DCGM_FI_DEV_ECC_SBE_VOL_TOTAL, counter
313, DCGM_FI_DEV_ECC_DBE_VOL_TOTAL, counter
314, DCGM_FI_DEV_ECC_SBE_AGG_TOTAL, counter
315, DCGM_FI_DEV_ECC_DBE_AGG_TOTAL, counter
391, DCGM_FI_DEV_RETIRED_SBE, counter
392, DCGM_FI_DEV_RETIRED_DBE, counter
393, DCGM_FI_DEV_RETIRED_PENDING, counter
230, DCGM_FI_DEV_XID_ERRORS, gauge

# NVLink health
449, DCGM_FI_DEV_NVLINK_REPLAY_ERROR_COUNT_TOTAL, counter
450, DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL, counter
451, DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL, counter

# PCIe health
202, DCGM_FI_DEV_PCIE_REPLAY_COUNTER, counter
226, DCGM_FI_DEV_PCIE_LINK_GEN, gauge
227, DCGM_FI_DEV_PCIE_LINK_WIDTH, gauge
```

That's **~30 fields**, well within sustainable cardinality (Doc 08).

---

## B.11 Non-DCGM GPU Metrics (Adjacent)

Useful sources outside DCGM that you'll find yourself joining against:

| Metric source | Typical name | What it gives you |
|---------------|--------------|-------------------|
| kube-state-metrics | `kube_pod_container_resource_requests{resource="nvidia_com_gpu"}` | Pod-to-GPU-count requests |
| kube-state-metrics | `kube_node_status_allocatable{resource="nvidia_com_gpu"}` | Per-node GPU capacity |
| kube-state-metrics | `kube_pod_labels{...}` | Join key for pod metadata |
| node-exporter | `node_filesystem_avail_bytes` | Local-disk pressure (NCCL spill) |
| nv-fabricmanager exporter | `nvfabricmanager_*` | NVSwitch fabric health |
| ib_exporter | `infiniband_*` | InfiniBand RDMA counters |
| vLLM exporter | `vllm:*` | LLM inference (KV cache, queues, TTFT) |
| Triton exporter | `nv_inference_*` | Triton serving (queue, compute, throughput) |

---

## B.12 Fields You Almost Certainly Don't Need

Drop these from the exporter unless you have a specific reason — they add cardinality without value:

| Field | Why skip |
|-------|----------|
| `DCGM_FI_DEV_GPU_UTIL` (203) | Lies; use `_PROF_SM_ACTIVE` instead. Keep only for legacy dashboards. |
| `DCGM_FI_DEV_FB_TOTAL` (250) | Constant per GPU model; emit once at boot, not every scrape. |
| `DCGM_FI_DEV_GPU_MAX_OP_TEMP` (151) | Constant. |
| `DCGM_FI_DEV_PCIE_MAX_LINK_GEN` (228) | Constant. |
| `DCGM_FI_DEV_ENC_UTIL` (206), `_DEC_UTIL` (207) | Video; not for ML clusters. |
| Per-link NVLink (`*_L0`–`*_L17`) | High cardinality; emit only on suspect nodes via separate ConfigMap. |
| `DCGM_FI_PROF_PIPE_FP64_ACTIVE` (1006) | Only relevant to HPC; drop on ML clusters. |

---

## Sources

- [NVIDIA DCGM API Reference — Field IDs](https://docs.nvidia.com/datacenter/dcgm/latest/dcgm-api/group__dcgmFieldIdentifiers.html)
- [dcgm-exporter — default counters](https://github.com/NVIDIA/dcgm-exporter/blob/main/etc/default-counters.csv)
- [NVIDIA — Profiling Metrics in DCGM](https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/feature-overview.html#profiling)
- [NVIDIA Throttle Reasons — NVML reference](https://docs.nvidia.com/deploy/nvml-api/group__nvmlClocksThrottleReasons.html)
