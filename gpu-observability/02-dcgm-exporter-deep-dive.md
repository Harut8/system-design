# 02 — DCGM Exporter Deep Dive

> Everything below the `:9400/metrics` endpoint. What field IDs to emit, why some are 1.x and some are 1.0xx, how the profiling lock will eat your dashboard if you don't plan for it, and what `kubectl apply` patterns survive in production.

This doc assumes you've read doc 01 and have a basic dcgm-exporter DaemonSet running. If `curl <pod>:9400/metrics` doesn't return text yet, stop and finish that first.

---

## 1. DCGM Architecture (the parts that matter to ops)

```
       ┌─────────────────────────────────────────────────────┐
       │  Application / dcgmi / dcgm-exporter                │
       │         │                                           │
       │         │  DCGM client API (libdcgm.so)             │
       │         ▼                                           │
       │  ┌──────────────────────────────────────────┐       │
       │  │  Embedded host engine OR  ───TCP:5555──▶ │       │
       │  │                          standalone     │       │
       │  │                          nv-hostengine  │       │
       │  └──────────────┬───────────────────────────┘       │
       │                 │                                   │
       │                 ▼                                   │
       │  ┌──────────────────────────────┐                   │
       │  │ Field collection subsystem    │                  │
       │  │  ├── NVML fields (DCGM_FI_DEV_*) — always avail │  │
       │  │  └── Profiling fields (DCGM_FI_PROF_*) ─┐       │
       │  └────────────────────────────────────────┼───────┘ │
       │                                           │         │
       │  ┌────────────────────────────────────────▼──────┐  │
       │  │ NVIDIA Profiling Subsystem (CUPTI / hardware │   │
       │  │ counters). EXCLUSIVE: one client at a time   │   │
       │  │ pre-H100. H100/H200/B200 support multi-       │   │
       │  │ client read of selected counters via MPS.     │   │
       │  └───────────────────────────────────────────────┘  │
       └─────────────────────────────────────────────────────┘
```

The two-tier collection model is the thing to internalize:

- **NVML-backed fields** (`DCGM_FI_DEV_*`): always available, cheap to poll, no exclusivity. Temperature, power, util%, ECC counts, XIDs, clock speeds, framebuffer.
- **Profiling fields** (`DCGM_FI_PROF_*`): require exclusive access to NVIDIA's profiling subsystem. SM activity, tensor pipe, NVLink TX/RX bytes, DRAM active. **One client at a time on pre-H100.** This is the source of every "DCGM stopped reporting" page you'll get.

### 1.1 Embedded vs standalone host engine

| Mode | When to use | Failure mode |
|------|-------------|--------------|
| **Embedded** | K8s DaemonSet, default for `nvcr.io/nvidia/k8s/dcgm-exporter` images | Exporter crash = host engine gone; pod restart re-initializes (~1s) |
| **Standalone** (`nv-hostengine` daemon) | DGX system images, bare-metal where multiple tools share DCGM, or where you want exporter restarts to not reset the engine | Version skew between client lib and daemon → silent field drops |

DGX systems ship with a system-wide `nv-hostengine` running. Connecting the exporter standalone (`-r <host>:5555`) lets you restart the exporter without losing the profiling subscription. On generic K8s, embedded is simpler — let the DaemonSet own the lifecycle.

The library version rule: **`libdcgm.so` (in the exporter image) ≥ `nv-hostengine` version**. The client knows about more fields than the server, never the other way.

---

## 2. Field IDs You Actually Want

DCGM defines ~250 field IDs. You don't emit all of them; cardinality (doc 08) will eat you. The recommended baseline emits ~30, broken into seven families.

### 2.1 GPU activity (the headline numbers)

| Field | ID | Type | What it really measures |
|-------|-----|------|-------------------------|
| `DCGM_FI_DEV_GPU_UTIL` | 203 | NVML | "GPU was busy" — 1 active warp = 100%. Misleading. Keep for backwards-compat dashboards. |
| `DCGM_FI_PROF_GR_ENGINE_ACTIVE` | 1001 | PROF | Graphics/compute engine busy fraction. The non-misleading version of GPU util. |
| `DCGM_FI_PROF_SM_ACTIVE` | 1002 | PROF | Fraction of SM cycles with ≥1 warp scheduled. **The number you put on dashboards.** |
| `DCGM_FI_PROF_SM_OCCUPANCY` | 1003 | PROF | Avg fraction of warp slots filled when SM is active. Saturation signal. |
| `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | 1004 | PROF | Tensor core utilization. The reason ML teams paid for H100s. |
| `DCGM_FI_PROF_PIPE_FP64_ACTIVE` | 1006 | PROF | FP64 pipe — HPC workloads. |
| `DCGM_FI_PROF_PIPE_FP32_ACTIVE` | 1007 | PROF | FP32 pipe. |
| `DCGM_FI_PROF_PIPE_FP16_ACTIVE` | 1008 | PROF | FP16/BF16 pipe — non-tensor mixed-precision. |

The "real" utilization triangle is **SM_ACTIVE × SM_OCCUPANCY × TENSOR_ACTIVE**. A workload at 95/30/5 is using SMs all the time but with shallow occupancy and barely any tensor cores — bad. 95/85/85 means you're cooking the silicon. Doc 5 builds a single "GPU efficiency" derived metric on top of these three.

### 2.2 Memory & bandwidth

| Field | ID | What |
|-------|-----|------|
| `DCGM_FI_DEV_FB_FREE` / `_USED` | 251/252 | Framebuffer (HBM) bytes |
| `DCGM_FI_DEV_FB_TOTAL` | 250 | HBM capacity (constant per model — drop or keep for join sanity) |
| `DCGM_FI_PROF_DRAM_ACTIVE` | 1005 | HBM bandwidth utilization. Memory-bound vs compute-bound discriminator. |
| `DCGM_FI_PROF_PCIE_RX_BYTES` | 1011 | PCIe receive throughput |
| `DCGM_FI_PROF_PCIE_TX_BYTES` | 1012 | PCIe transmit throughput |
| `DCGM_FI_PROF_NVLINK_RX_BYTES` | 1009 | NVLink receive (per link) |
| `DCGM_FI_PROF_NVLINK_TX_BYTES` | 1010 | NVLink transmit (per link) |

`DRAM_ACTIVE` near 1.0 with `SM_ACTIVE` low → you are HBM-bound (decoding LLM, large embedding lookups). `PCIE_RX/TX` saturating → host↔device data starving the GPU (covered in doc 6).

### 2.3 ECC, retired pages, faults

| Field | ID | What |
|-------|-----|------|
| `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL` | 312 | Single-bit ECC, volatile total |
| `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL` | 313 | Double-bit ECC, volatile total |
| `DCGM_FI_DEV_ECC_SBE_AGG_TOTAL` | 314 | Single-bit, aggregate (across reboots) |
| `DCGM_FI_DEV_ECC_DBE_AGG_TOTAL` | 315 | Double-bit, aggregate |
| `DCGM_FI_DEV_RETIRED_SBE` | 391 | Pages retired due to SBE |
| `DCGM_FI_DEV_RETIRED_DBE` | 392 | Pages retired due to DBE — RMA candidate |
| `DCGM_FI_DEV_RETIRED_PENDING` | 393 | Pending page retirement (next reboot) |
| `DCGM_FI_DEV_ROW_REMAP_FAILURE` | 393+ (H100+) | Row remap failed — fatal |
| `DCGM_FI_DEV_XID_ERRORS` | 230 | Last XID error code observed |

ECC lifecycle (doc 07 covers this end-to-end): SBE rate spike → page retirement → DBE → RMA. You want a **rate** alert on SBE *velocity*, not absolute count. Old GPUs accumulate SBEs over years and that's fine.

### 2.4 Power, thermal, throttle

| Field | ID | What |
|-------|-----|------|
| `DCGM_FI_DEV_POWER_USAGE` | 155 | Watts |
| `DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION` | 157 | Joules counter (use `rate()` for power) |
| `DCGM_FI_DEV_GPU_TEMP` | 150 | GPU die temp °C |
| `DCGM_FI_DEV_MEMORY_TEMP` | 156 | HBM temp °C — separate from die temp, often the actual limiter |
| `DCGM_FI_DEV_GPU_MAX_OP_TEMP` | 151 | Max op temp (constant) |
| `DCGM_FI_DEV_CLOCK_THROTTLE_REASONS` | 112 | Bitfield: idle, thermal, power, sync_boost, sw_thermal, hw_thermal, hw_power_brakeslowdown, display_clock_setting |
| `DCGM_FI_DEV_SM_CLOCK` | 100 | Current SM clock MHz |
| `DCGM_FI_DEV_MEM_CLOCK` | 101 | Current memory clock MHz |

The throttle reasons bitfield is the most useful debugging field on this list. "GPU is slow" → check the throttle reasons before anything else. dcgm-exporter exposes it as a single label-less integer; you decode it with PromQL bitwise ops or by emitting it as separate fields via `DCGM_FI_DEV_THERMAL_VIOLATION` etc.

### 2.5 Decode/encode (NVENC, NVDEC, NVJPG, OFA)

| Field | ID | What |
|-------|-----|------|
| `DCGM_FI_DEV_ENC_UTIL` | 206 | NVENC utilization % |
| `DCGM_FI_DEV_DEC_UTIL` | 207 | NVDEC utilization % |
| `DCGM_FI_PROF_NVDEC_*_ACTIVE` | 1013–1018 | Per-engine NVDEC active fraction |
| `DCGM_FI_PROF_NVJPG_*_ACTIVE` | 1019+ | NVJPG (JPEG decode) |
| `DCGM_FI_PROF_NVOFA_*_ACTIVE` | (varies) | Optical flow accelerator |

Mostly relevant for video transcoding workloads, multimodal training data prep, and anything that uses the dedicated decode engines (Triton with DALI). Skip unless that's you.

### 2.6 NVLink health (per-link)

| Field | ID | What |
|-------|-----|------|
| `DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL` | 449 | Aggregate NVLink BW |
| `DCGM_FI_DEV_NVLINK_REPLAY_ERROR_COUNT_TOTAL` | 421 | Replay errors (transient) |
| `DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL` | 422 | Recovery errors (link reset) |
| `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL` | 419 | Per-flit CRC errors (data corruption) |
| `DCGM_FI_DEV_NVLINK_CRC_DATA_ERROR_COUNT_TOTAL` | 420 | Data CRC errors |

A rising recovery-error rate on a specific link is the leading indicator of NVLink degradation, which you'll see before AllReduce performance regressions show up in training jobs (doc 15).

### 2.7 MIG-specific fields

When MIG is enabled, the same metrics emit per-GPU-Instance (GI) and per-Compute-Instance (CI) with extra labels (§5).

---

## 3. Field Groups, Field Sets, and the ConfigMap

DCGM groups fields into named **field groups** for efficient subscription. dcgm-exporter ships with three predefined collectors:

- `default-counters.csv` — minimal, NVML-only, no profiling lock taken. Safe everywhere.
- `dcp-metrics-included.csv` — includes profiling fields. Takes the profiling lock.
- `dcgm-exporter-mig-counters.csv` — variant with MIG-aware label emission.

The CSV format:

```csv
# Format: <field-id-or-DCGM_FI_*-name>, <prometheus-type>, <help-text>
DCGM_FI_DEV_GPU_TEMP,         gauge,   GPU temperature (in C).
DCGM_FI_DEV_POWER_USAGE,      gauge,   Power draw (in W).
DCGM_FI_PROF_GR_ENGINE_ACTIVE,gauge,   Graphics engine activity ratio.
DCGM_FI_PROF_SM_ACTIVE,       gauge,   SM active ratio.
DCGM_FI_PROF_SM_OCCUPANCY,    gauge,   SM occupancy ratio.
DCGM_FI_PROF_PIPE_TENSOR_ACTIVE, gauge, Tensor pipe active ratio.
DCGM_FI_PROF_DRAM_ACTIVE,     gauge,   HBM bandwidth utilization.
DCGM_FI_DEV_FB_USED,          gauge,   Framebuffer used (MiB).
DCGM_FI_DEV_FB_FREE,          gauge,   Framebuffer free (MiB).
DCGM_FI_DEV_PCIE_RX_THROUGHPUT, gauge, PCIe RX (KB/s).
DCGM_FI_DEV_PCIE_TX_THROUGHPUT, gauge, PCIe TX (KB/s).
DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL, counter, NVLink BW (bytes).
DCGM_FI_DEV_NVLINK_REPLAY_ERROR_COUNT_TOTAL, counter, NVLink replay errs.
DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL, counter, NVLink recovery errs.
DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL, counter, NVLink CRC flit errs.
DCGM_FI_DEV_ECC_SBE_VOL_TOTAL, counter, SBE ECC vol total.
DCGM_FI_DEV_ECC_DBE_VOL_TOTAL, counter, DBE ECC vol total.
DCGM_FI_DEV_RETIRED_DBE,      counter, Pages retired due to DBE.
DCGM_FI_DEV_RETIRED_PENDING,  gauge,   Pending retirements.
DCGM_FI_DEV_XID_ERRORS,       gauge,   Last XID error.
DCGM_FI_DEV_CLOCK_THROTTLE_REASONS, gauge, Throttle reasons bitfield.
DCGM_FI_DEV_GPU_MAX_OP_TEMP,  gauge,   Max op temp.
DCGM_FI_DEV_MEMORY_TEMP,      gauge,   HBM temp.
DCGM_FI_DEV_SM_CLOCK,         gauge,   SM clock (MHz).
DCGM_FI_DEV_MEM_CLOCK,        gauge,   Mem clock (MHz).
DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION, counter, Energy (mJ).
```

That's ~26 fields. With 8 GPUs/node × 1k nodes you sit at ~210k base series. After the standard pod/namespace labels (next section), expect 2–3M.

Mounted as ConfigMap:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: dcgm-exporter-metrics
  namespace: gpu-operator
data:
  dcgm-exporter-default.csv: |
    # ... CSV above ...
```

And referenced via `--collectors=/etc/dcgm-exporter/dcgm-exporter-default.csv` in the container args.

---

## 4. The Profiling Lock — your #1 production hazard

NVIDIA's profiling subsystem (the one CUPTI hooks into) exposes hardware counters. Before H100, **only one process at a time** can subscribe. This means:

- If a user runs `nsys profile` or `ncu` (Nsight Compute) on a node, your dcgm-exporter loses access to all `DCGM_FI_PROF_*` fields. They go silent (no scrape error — just no data).
- If you accidentally deploy two dcgm-exporters on the same node (different ConfigMaps, A/B test), one wins the lock, the other emits NaNs.
- Vendor monitoring agents (Datadog GPU integration, NewRelic NVIDIA integration) that take the profiling lock will silently steal it from your exporter.

H100 and later ("Hopper", "Blackwell") support concurrent profiling clients for a *subset* of counters via the new "MPS-shared" profiling mode, but you should not assume this — write your alerts as if exclusivity holds.

### 4.1 Detection

```promql
# Profiling fields silently stopped
absent_over_time(DCGM_FI_PROF_SM_ACTIVE[5m])
   and on(instance) up{job="dcgm-exporter"} == 1
```

If `up{}` is 1 (exporter alive) and the prof field is missing, something else has the lock.

### 4.2 Mitigation: cooperative pause/resume

For nodes where developers need Nsight access:

```bash
# Before profiling
dcgmi profile --pause -i <gpu-index>

# After profiling
dcgmi profile --resume -i <gpu-index>
```

Wrap this in a Slurm prolog/epilog or a Kubernetes admission hook tied to a `nsight-access` label. The Ohio Supercomputer Center documents this pattern with `--gres=nsight` in Slurm; production K8s shops typically use an init container that calls `dcgmi profile --pause` for jobs annotated `gpu.nvidia.com/profiling-access=true`.

### 4.3 Mitigation: separate non-profiling exporter

For ultra-locked-down environments, run *two* exporters per node:

- **`dcgm-exporter-stable`** — NVML-only fields, never takes the lock, always available
- **`dcgm-exporter-prof`** — profiling fields, may temporarily lose data

The stable exporter is what alerts run against (hardware health, ECC, throttle); the prof exporter feeds dashboards. This costs cardinality but eliminates "all GPU metrics gone" scenarios.

---

## 5. MIG: Cardinality Explosion in 50 Lines of Config

MIG (Multi-Instance GPU) partitions one physical GPU into up to 7 GPU Instances (GIs); each GI can be subdivided into Compute Instances (CIs). On H100, common profiles:

| Profile | GIs | Memory |
|---------|-----|--------|
| 1g.10gb | 7 | 10 GB each |
| 2g.20gb | 3 | 20 GB each |
| 3g.40gb | 2 | 40 GB each |
| 4g.40gb | 1 + 1g | 40 GB |
| 7g.80gb | 1 (whole GPU) | 80 GB |

When MIG is on, dcgm-exporter emits fields per GI/CI with extra labels:

```
DCGM_FI_PROF_SM_ACTIVE{
   gpu="0",
   UUID="GPU-34319582-...",
   GPU_I_ID="3",
   GPU_I_PROFILE="1g.10gb",
   device="nvidia0",
   modelName="NVIDIA H100 80GB HBM3",
   Hostname="gpu-node-014",
   ...
} 0.42
```

### 5.1 The math

A 1k-node cluster with 8 H100s/node, all in 1g.10gb mode, gives you `1000 × 8 × 7 = 56,000 GIs`. With 30 fields per GI, that's **1.68M raw series** *before* pod/namespace labels. Add labels and you're at 5–10M easily.

Mitigation in the relabel config (full version in doc 08):

```yaml
metric_relabel_configs:
  # Drop GI-level series for fields that only matter at the physical GPU level
  - source_labels: [__name__, GPU_I_ID]
    regex: "DCGM_FI_DEV_GPU_TEMP|DCGM_FI_DEV_POWER_USAGE;.+"
    action: drop
  # Keep GI labels only on the PROF fields where they're meaningful
```

The principle: **temperature and power are physical-GPU properties, not per-GI.** Don't emit them per GI. Only `_PROF_SM_ACTIVE`, `_PROF_DRAM_ACTIVE`, `_PROF_PIPE_*`, and `_DEV_FB_*` need GI-level breakdown.

### 5.2 MIG profile changes

When MIG profile changes (operator reconfigures nodes), labels change. Old series go stale and live in the head block for 2 hours. A cluster-wide MIG repartitioning of 1000 nodes can briefly add millions of series. Schedule reconfigurations during off-hours and watch `prometheus_tsdb_head_series` during the window.

---

## 6. Kubernetes DaemonSet

The canonical deployment via the GPU Operator's Helm chart (recommended) installs a DaemonSet that:

- Runs on every node tainted `nvidia.com/gpu` (or labelled equivalently)
- Mounts the device plugin's pod-resources socket so it can label metrics with `pod`, `namespace`, `container`
- Mounts the ConfigMap with the field CSV
- Runs as `privileged: false` but with `SYS_ADMIN` cap (needed for profiling subscription)

### 6.1 Minimal DaemonSet (for reference; prefer the GPU Operator)

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: dcgm-exporter
  namespace: gpu-operator
spec:
  selector:
    matchLabels: { app: dcgm-exporter }
  template:
    metadata:
      labels: { app: dcgm-exporter }
    spec:
      nodeSelector:
        nvidia.com/gpu.present: "true"
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      hostNetwork: false        # use ClusterIP service + ServiceMonitor
      hostPID: true             # required to read pod-resources via /proc
      containers:
        - name: dcgm-exporter
          image: nvcr.io/nvidia/k8s/dcgm-exporter:3.3.7-3.5.0-ubuntu22.04
          args:
            - "--collectors=/etc/dcgm-exporter/default.csv"
            - "--kubernetes=true"             # enable pod label association
            - "--kubernetes-gpu-id-type=uid"  # use device UUID, not /dev/nvidiaN
          ports:
            - name: metrics
              containerPort: 9400
          securityContext:
            runAsNonRoot: false
            runAsUser: 0
            capabilities:
              add: ["SYS_ADMIN"]
          volumeMounts:
            - name: pod-resources
              mountPath: /var/lib/kubelet/pod-resources
            - name: collectors
              mountPath: /etc/dcgm-exporter
          readinessProbe:
            httpGet: { path: /health, port: 9400 }
            periodSeconds: 5
          livenessProbe:
            httpGet: { path: /health, port: 9400 }
            periodSeconds: 30
            failureThreshold: 3
      volumes:
        - name: pod-resources
          hostPath: { path: /var/lib/kubelet/pod-resources }
        - name: collectors
          configMap: { name: dcgm-exporter-metrics }
```

### 6.2 Pod label association

`--kubernetes=true` makes dcgm-exporter call the kubelet's `pod-resources` gRPC API (Unix socket at `/var/lib/kubelet/pod-resources/kubelet.sock`) to learn which pod owns which GPU UUID. The result: every metric gets `pod`, `namespace`, `container` labels for the workload using that GPU.

This is also the **cardinality time bomb** (doc 08). Pod churn means new label values; if the same GPU is assigned to a different pod every 30s during a deployment rollout, you accumulate hundreds of thousands of series in 5 minutes.

### 6.3 ServiceMonitor for the Prometheus Operator

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: dcgm-exporter
  namespace: gpu-operator
  labels: { release: kube-prometheus-stack }
spec:
  selector:
    matchLabels: { app: dcgm-exporter }
  endpoints:
    - port: metrics
      interval: 15s
      scrapeTimeout: 10s
      metricRelabelings:
        # Drop the modelName label from per-pod metrics — it duplicates a node label
        - sourceLabels: [__name__]
          regex: ".+"
          targetLabel: __tmp_keep
          replacement: "1"
        - action: labeldrop
          regex: "modelName"
```

---

## 7. Health, Watchdog, and Restart Behavior

dcgm-exporter exposes `/health` (HTTP 200 if libdcgm is responsive) and `/metrics`. The container will:

1. Initialize libdcgm on startup. If NVML is unhealthy (driver mismatch, no GPUs visible), the exporter exits with a non-zero code → DaemonSet restarts.
2. On a per-scrape basis, if the host engine returns errors for a specific field, that field is omitted from the response (no metric emitted) rather than emitting a zero. **Important for alerting**: don't write `metric == 0` alerts; write `absent()` ones.
3. If `nv-hostengine` (embedded) crashes, the whole exporter typically segfaults or exits cleanly; the pod restart restores it. There is no in-process recovery.

Symptoms of a sick exporter pod:

| Symptom | Likely cause |
|---------|--------------|
| `/health` 200 but `/metrics` empty list | DCGM hostengine just crashed; next health check will fail too |
| All `_PROF_*` fields gone, `_DEV_*` present | Profiling lock contention (§4) |
| One GPU's series gone, others fine | XID 79 (GPU fell off bus); cluster has hardware fault |
| All metrics gone for a single node | Container OOM, driver kpanic, or kubelet evicting; check `kubectl describe pod` |

DCGM also has its own **health check API** (`dcgmHealthSet`, `dcgmHealthCheck`) that lets the host engine self-report watch-group violations (PCIe link width drop, ECC threshold, NVLink link down). Most exporter deployments don't surface these via the Prometheus endpoint — they're emitted to the DCGM event log. Doc 7 covers piping that log into the metrics pipeline.

---

## 8. Scrape Sanity Checklist

Before declaring victory, verify:

```bash
# 1. /metrics returns text
curl -s pod-ip:9400/metrics | head -20

# 2. Profiling fields are present (not just NVML)
curl -s pod-ip:9400/metrics | grep -c DCGM_FI_PROF_
# expect: 7+ if you're using the dcp-metrics CSV

# 3. Pod labels are populated where a GPU is allocated
curl -s pod-ip:9400/metrics | grep 'pod="' | head -5

# 4. MIG labels appear (if MIG enabled)
curl -s pod-ip:9400/metrics | grep GPU_I_ID | head -5

# 5. No NaN / +Inf values
curl -s pod-ip:9400/metrics | grep -E '(NaN|\+Inf|-Inf)'
# expect: empty
```

In Prometheus:

```promql
# Are all expected nodes scraped?
count(up{job="dcgm-exporter"} == 1)

# Are profiling fields flowing for all GPUs?
count by (Hostname, gpu) (DCGM_FI_PROF_SM_ACTIVE) == on() group_left()
  count by (Hostname, gpu) (DCGM_FI_DEV_GPU_TEMP)

# Cardinality estimate for this exporter
count({job="dcgm-exporter"})
```

If that last query returns >5M for a 1k-GPU cluster, go straight to doc 08 before deploying the rest of the stack.

---

## 9. Common Gotchas

1. **`--kubernetes-gpu-id-type=device-name` produces unstable labels.** Use `=uid` so the GPU UUID is the canonical identifier; pod restarts then reuse the same series.
2. **Don't scrape on `hostNetwork: true` unless you have to.** The default ClusterIP + ServiceMonitor pattern works and avoids port collisions on multi-tenant nodes.
3. **DCGM 3.x and older drivers**. Some `DCGM_FI_PROF_*` fields require driver R535+. Older drivers silently omit them — don't waste hours debugging "why is `PIPE_TENSOR_ACTIVE` empty" without checking `nvidia-smi --version`.
4. **The exporter's image tag includes the DCGM version *and* the CUDA version**: `3.3.7-3.5.0-ubuntu22.04` means dcgm-exporter 3.3.7, DCGM 3.5.0, Ubuntu 22.04 base. Match the DCGM major version to whatever's in your driver bundle.
5. **B200 / Blackwell field IDs.** As of Q1 2026, several new Blackwell-only fields (HBM3e error counters, transformer engine stats) appear under the `DCGM_FI_DEV_*` numeric range 600–699. Check the DCGM release notes for your exact driver before assuming they exist.
6. **`hostPID: true` is required** for the pod-resources lookup to work; without it, all metrics emit without `pod=` labels and you lose workload attribution.

---

## 10. Output: what the rest of the stack consumes

After this doc you should have:

- A DaemonSet running on every GPU node, exporting ~30 fields per GPU
- Profiling fields visible (or a documented decision to skip them)
- Pod/namespace/container labels populated for allocated GPUs
- A baseline series count and growth-rate measurement for capacity planning
- An alert (`absent_over_time(DCGM_FI_PROF_SM_ACTIVE[5m])`) that fires if the profiling lock is stolen

Doc 03 builds on this: kube-state-metrics extensions, device plugin telemetry, scheduler-level signals, and the complete K8s GPU-aware observability picture.

---

## Sources

- [DCGM Field Identifiers — official reference](https://docs.nvidia.com/datacenter/dcgm/latest/dcgm-api/dcgm-api-field-ids.html)
- [DCGM Profiling docs](https://docs.nvidia.com/datacenter/dcgm/latest/dcgm-api/dcgm-api-profiling.html)
- [DCGM Feature Overview](https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/feature-overview.html)
- [NVIDIA/dcgm-exporter — GitHub](https://github.com/NVIDIA/dcgm-exporter)
- [DCGM Exporter — NVIDIA Cloud Native docs](https://docs.nvidia.com/datacenter/cloud-native/gpu-telemetry/latest/dcgm-exporter.html)
- [XID Errors r590 (Dec 2025)](https://docs.nvidia.com/deploy/pdf/XID_Errors.pdf)
- [Ohio SC — Nsight/DCGM conflict](https://www.osc.edu/resources/technical_support/known_issues/nsight_gpu_profiler_not_working_due_to_dcgm_conflict)
- [GKE — Collect and view DCGM metrics](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/dcgm-metrics)
- [Modal — GPU Health practical guide](https://modal.com/docs/guide/gpu-health)
