# 07 — Hardware Health & Failure Detection

> When a GPU dies, your training run dies. When 1 of 1000 GPUs is *dying* (not yet dead), your job slows for hours before you notice. This doc covers the difference: detecting impending failure early enough to drain the workload before it crashes.

The two failure scopes:

- **Hard failures** — GPU is gone. XID 79, driver crash, node down. Immediate page; pod evict; node drain; RMA.
- **Soft failures** — GPU is degrading. SBE rate climbing, NVLink retries, thermal throttling. Ticket; investigate; correlate with workload before pulling.

Both need monitoring. Soft failures are where the value is.

---

## 1. The Three Hardware Health Sources

| Source | What it sees | Latency |
|--------|-------------|---------|
| **DCGM fields** (`DCGM_FI_DEV_*`) | Per-GPU counters: ECC, XID last code, temps, throttle | 15s scrape |
| **DCGM Health API** (`dcgmHealthSet` / `dcgmHealthCheck`) | Pre-defined watch groups with severity levels (PCIe, NVLink, ECC, Power, Thermal) | Polled or callback |
| **Kernel `dmesg` / NVRM logs** | Driver-level XID events with full context | Continuous |

DCGM fields are the easiest to scrape and dashboard. The Health API gives you DCGM's own classification ("PCIe link width has decreased — Warning"). The kernel log is the source of truth for transient XIDs that DCGM may sample around.

A complete pipeline scrapes all three:

1. Prometheus scrapes dcgm-exporter → counters, last-XID
2. A small DaemonSet exposes `dmesg | grep NVRM` → emits per-event metrics with the full XID code as a label
3. (Optional) DCGM Health API integration emits classified Warning/Critical events

The third source is the one most teams miss. A burst of XIDs between two scrape windows can be invisible to dcgm-exporter — `DCGM_FI_DEV_XID_ERRORS` only holds the last code.

---

## 2. ECC Error Lifecycle

The most important hardware story to internalize. ECC errors progress through stages, and the right alert hits a different stage:

```
healthy ──▶ rising SBE rate ──▶ DBE ──▶ page retirement ──▶ row remap ──▶ RMA
            ↑                  ↑                                 ↑
            warning ticket     critical page                     replace
```

| Stage | Field | Severity |
|-------|-------|----------|
| Single-bit error (SBE) | `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL` | Info — normal background |
| Rising SBE rate | `rate(...)[1h]` | Warning — investigate |
| Double-bit error (DBE) | `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL` | Critical — data corrupted |
| Page retired (SBE-driven) | `DCGM_FI_DEV_RETIRED_SBE` | Warning |
| Page retired (DBE-driven) | `DCGM_FI_DEV_RETIRED_DBE` | Critical |
| Row remap pending (H100+) | `DCGM_FI_DEV_ROW_REMAP_PENDING` | Warning |
| Row remap failed | `DCGM_FI_DEV_ROW_REMAP_FAILURE` | Fatal — RMA |

### 2.1 SBE rate alerting (the leading indicator)

A healthy H100 sees ~0–10 SBEs/day across all HBM. A degrading cell can hit 100+/hour. Detect the *velocity*, not the absolute count:

```yaml
- alert: GPU_SBE_RateHigh
  expr: |
    rate(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL[1h]) > 10 / 3600   # > 10 per hour
  for: 30m
  labels: { severity: warning, team: gpu-platform }
  annotations:
    summary: "GPU {{ $labels.gpu }} on {{ $labels.Hostname }} SBE rate climbing"
    runbook: https://wiki/gpu-sbe-rate
```

The `for: 30m` clause prevents flap on transient cosmic-ray hits.

### 2.2 DBE — page immediately

```yaml
- alert: GPU_DBE_Detected
  expr: |
    increase(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL[5m]) > 0
  for: 0s
  labels: { severity: critical, team: gpu-platform }
  annotations:
    summary: "Double-bit ECC on GPU {{ $labels.gpu }} ({{ $labels.Hostname }})"
    description: |
      Uncorrectable memory error. Workload data may be corrupted.
      1. Drain pods from this GPU
      2. Restart the affected pod
      3. If recurring within 24h, RMA
    runbook: https://wiki/gpu-dbe
```

### 2.3 Page retirement watermarks

```yaml
- alert: GPU_PageRetirement_Approaching_RMA
  expr: |
    DCGM_FI_DEV_RETIRED_DBE > 32
  for: 1m
  labels: { severity: warning }
  annotations:
    summary: "GPU {{ $labels.gpu }} retired {{ $value }} pages — half of RMA threshold"
```

NVIDIA's RMA threshold is typically 64 retired DBE pages. At 32, you have hours to days before forced replacement; plan it during a maintenance window.

---

## 3. XID Error Catalog (the production-relevant subset)

Per the NVIDIA XID Errors r590 (Dec 2025) catalog and field reports. The codes you'll actually see:

| XID | Name | Severity | Action |
|-----|------|----------|--------|
| **13** | Graphics Engine Exception | Critical | App bug or HW. Run `compute-sanitizer`; check for kernel race conditions. |
| **31** | GPU Page Table Fault | Critical | Use-after-free, bad pointer in CUDA code. Application-side. |
| **43** | GPU stopped processing | Warning | Stalled kernel. Possible thermal/power issue if intermittent. |
| **45** | Preemptive GPU removal | Fatal | Driver gave up. **Drain node, reboot, RMA if recurs.** |
| **48** | Double-bit ECC | Fatal | Memory cell degradation. **RMA if > 1/week.** |
| **56** | Display Engine error | Warning | Rare on data-center GPUs (no display). Usually noise. |
| **62** | Internal microcontroller halt | Fatal | Firmware (PMU/GSP/SEC2) crashed. Try driver+VBIOS update; RMA if persists. |
| **63** | ECC page retirement (event) | Warning | Page retired due to repeated SBE. Track count via `_RETIRED_SBE`. |
| **64** | ECC/Power error | Critical | Voltage droop causing bit flips. Check PSU; swap GPU between hosts to isolate. |
| **68** | NVDEC/NVENC error | Warning | Codec issue, often workload-dependent. |
| **74** | NVLink error | Warning | One link degrading. `nvidia-smi nvlink -s` for details; reseat bridges; RMA if multi-bridge. |
| **79** | Fallen off bus | Fatal | GPU disappeared from PCIe. **Reboot mandatory; RMA if persists.** |
| **92** | High single-bit ECC error rate | Warning | Same alert as §2.1 but driver-level. |
| **94** | Contained ECC (single-bit) | Info | Normal background; few per month expected. |
| **95** | Uncontained ECC error | Critical | DBE escaped containment, may have corrupted other processes. **Restart all workloads; investigate.** |

### 3.1 Emitting XIDs as Prometheus events

`dcgm-exporter` emits `DCGM_FI_DEV_XID_ERRORS` as a gauge that holds the *last* XID code. This loses bursts. The pattern:

```promql
# Spotting XID 79 anywhere
DCGM_FI_DEV_XID_ERRORS == 79
```

Better: a sidecar that tails `journalctl -k | grep NVRM` and emits per-XID counters:

```promql
# Cumulative XID counter
gpu_xid_total{gpu="0", code="79", Hostname="gpu-014"}
```

That's a discrete event you can `increase()` over windows. We'll show the sidecar in §6.

### 3.2 XID 79 is the "GPU is gone" alert

```yaml
- alert: GPU_FellOffBus
  expr: |
    increase(gpu_xid_total{code="79"}[1m]) > 0
    or
    DCGM_FI_DEV_XID_ERRORS == 79
  for: 0s
  labels: { severity: critical, team: gpu-platform }
  annotations:
    summary: "GPU {{ $labels.gpu }} on {{ $labels.Hostname }} fell off bus (XID 79)"
    description: |
      The GPU is no longer responding. Pod will fail.
      1. Drain node
      2. Reboot
      3. If GPU does not return after reboot, RMA
```

---

## 4. DCGM Health API Integration

DCGM ships its own classified health system. Subscribe to watch groups:

```c
// In a sidecar or DCGM-aware agent
dcgmHandle_t handle;
dcgmGroupHandle_t group;
dcgmConnect(&handle, "localhost", 5555);
dcgmGroupCreate(handle, DCGM_GROUP_DEFAULT, "all-gpus", &group);

dcgmHealthSystems_t watches =
    DCGM_HEALTH_WATCH_PCIE      |
    DCGM_HEALTH_WATCH_NVLINK    |
    DCGM_HEALTH_WATCH_PMU       |   // power management unit
    DCGM_HEALTH_WATCH_MCU       |   // memory controller
    DCGM_HEALTH_WATCH_MEM       |   // memory ECC
    DCGM_HEALTH_WATCH_SM        |   // SM-related
    DCGM_HEALTH_WATCH_INFOROM   |   // GPU's stored info
    DCGM_HEALTH_WATCH_THERMAL   |
    DCGM_HEALTH_WATCH_POWER;

dcgmHealthSet(handle, group, watches);

// Periodically poll
while (true) {
    dcgmHealthResponse_v4 response;
    dcgmHealthCheck(handle, group, &response);
    for (int i = 0; i < response.incidentCount; i++) {
        // Each incident has: gpuId, system, health, errorString, code
        emit_prometheus_metric(response.incidents[i]);
    }
    sleep(30);
}
```

The output classifies issues as `DCGM_HEALTH_RESULT_PASS`, `_WARN`, or `_FAIL` per system per GPU. Emit as:

```promql
dcgm_health_result{gpu="0", Hostname="gpu-014", system="NVLINK"} 0  # 0=PASS, 1=WARN, 2=FAIL
```

Then alert on the integer:

```yaml
- alert: DCGMHealth_Fail
  expr: dcgm_health_result == 2
  labels: { severity: critical }
```

This is the cleanest "DCGM itself thinks something is broken" signal in the stack.

---

## 5. Detecting "Dead GPU" vs "Exporter Crash"

A pernicious failure mode: `DCGM_FI_DEV_GPU_TEMP{gpu="3"}` stops being scraped. Two possible causes, very different responses:

| Symptom | Cause | Action |
|---------|-------|--------|
| All GPUs on a node go silent | Exporter pod crashed | Restart pod |
| One specific GPU goes silent | XID 79 / GPU dead | Drain node, RMA |
| Profiling fields silent, NVML fields present | Profiling lock contention | Doc 02 §4 |
| All metrics from the cluster go silent | Prometheus scrape broken | Check service discovery |

The differentiating query:

```promql
# An expected GPU's metrics stopped
absent_over_time(DCGM_FI_DEV_GPU_TEMP{gpu="3", Hostname="gpu-014"}[5m])
  and on(Hostname)
  count(DCGM_FI_DEV_GPU_TEMP{Hostname="gpu-014"}) > 0   # other GPUs still report
```

Translation: GPU 3 specifically is gone, but the exporter on this node is still talking to other GPUs. → XID 79 or hardware failure. Page.

```promql
# Exporter is gone entirely from a node
absent(up{job="dcgm-exporter", Hostname="gpu-014"} == 1)
```

→ Exporter or pod-level issue. Restart, then escalate if it keeps failing.

---

## 6. The dmesg/NVRM Sidecar

Pattern: a tiny DaemonSet that tails kernel log lines and emits Prometheus metrics. NVIDIA's own `nvidia-dcgm-exporter` doesn't do this; you wire it up.

```yaml
# Sidecar (or DaemonSet) that tails kernel log
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: nvrm-log-exporter
spec:
  template:
    spec:
      hostNetwork: true
      hostPID: true
      containers:
        - name: tailer
          image: nvrm-log-exporter:1.0
          securityContext:
            privileged: true   # needs to read /var/log/messages
          volumeMounts:
            - name: kmsg
              mountPath: /dev/kmsg
              readOnly: true
            - name: messages
              mountPath: /var/log
              readOnly: true
      volumes:
        - name: kmsg
          hostPath: { path: /dev/kmsg }
        - name: messages
          hostPath: { path: /var/log }
```

The exporter regex-matches lines like:

```
NVRM: Xid (PCI:0000:1a:00.0): 79, pid='<unknown>', name=<unknown>, GPU has fallen off the bus.
```

And emits:

```
gpu_xid_total{Hostname="gpu-014", gpu="0", code="79"} 1
gpu_xid_seconds{Hostname="gpu-014", gpu="0", code="79"} 1714294823
```

Now you can `rate(gpu_xid_total[5m])` and `histogram_quantile()` over events instead of relying on the last-value gauge.

---

## 7. RMA Workflow Observability

The lifecycle of a failing GPU through procurement:

```
detected ──▶ ticket ──▶ drained ──▶ vendor RMA ──▶ replacement ──▶ re-imaged ──▶ in service
   │           │            │           │              │              │              │
   alert     auto-create    cordon     ship         receive          burn-in        repool
              ticket        node                     unit            48h             label
```

Each transition is a metric (or annotation):

```promql
gpu_lifecycle_state{
   serial="123456",
   state=~"detected|in_rma|burn_in|in_service|retired"
}
```

Counts you want on a quarterly review:

```promql
# RMA rate by GPU model
sum by (gpu_product) (
  increase(gpu_lifecycle_transitions_total{from="in_service", to="in_rma"}[30d])
)
```

H100 RMA rate baseline: ~0.5–1.5%/year. If you're seeing > 3%/year on a particular SKU, fleet quality is off — escalate to vendor.

---

## 8. Predictive Failure Signals

Three combinations that historically precede hard failures by 30 min – 24 h:

### 8.1 Rising temperature trend + rising throttle

```promql
# Temperature climbing while throttle reasons accumulate
deriv(DCGM_FI_DEV_GPU_TEMP[1h]) > 0.01   # rising > 0.01 °C/sec
  and on(Hostname, gpu)
(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS & 0x40) > 0   # HW thermal throttle
```

Cooling is failing. Drain workload, investigate fans/thermal paste/airflow.

### 8.2 SBE rate climbing + retired pages climbing + DBE absent

```promql
rate(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL[1h]) > 10/3600
  and
deriv(DCGM_FI_DEV_RETIRED_SBE[6h]) > 0
  and absent(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL > 0 offset 1h)
```

Memory cells are degrading. DBE is coming. Drain proactively.

### 8.3 NVLink recovery rate climbing on a single link

```promql
rate(DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL[1h]) > 0
  and
deriv(DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL[12h]) > 0
```

NVLink fabric is degrading on a specific link. Distribute training jobs across other links until the GPU is replaced.

---

## 9. NVML Return Codes in Application Logs

Don't forget: applications themselves see hardware health. PyTorch CUDA OOM, NCCL errors, NVML errors all surface in logs. Pipe them into the metrics pipeline:

```python
# In your training framework
try:
    output = model(batch)
except RuntimeError as e:
    if "CUDA" in str(e):
        gpu_app_error_total.labels(error_type="cuda", message=str(e)[:50]).inc()
        raise
```

```promql
# Application-side CUDA errors per pod
rate(gpu_app_error_total{error_type="cuda"}[5m])
```

A burst of "CUDA error: an illegal memory access" across multiple pods on the same node is hardware. A burst on one pod across nodes is application bug.

---

## 10. Acceptance Checklist

Phase 3 ready when:

- [ ] DBE alert fires within 5m of a synthetic injection (use `dcgmi diag -r`)
- [ ] XID 79 alert wired and tested via `nvrm-log-exporter`
- [ ] SBE rate alert calibrated (no false positives over a 1-week baseline)
- [ ] DCGM Health API exporter (or equivalent classification) running
- [ ] dmesg/NVRM sidecar deployed and emitting `gpu_xid_total`
- [ ] Page retirement watermark alerts at 32 and 64
- [ ] NVLink recovery error trend alert defined
- [ ] Predictive temperature trend alert defined
- [ ] Runbooks linked from every alert annotation

---

## 11. Forward Pointers

- **Doc 10**: full alert routing — page vs ticket vs informational by team
- **Doc 12**: RMA rate analysis for capacity planning
- **Doc 15**: NVLink degradation correlation with training stragglers

---

## Sources

- [NVIDIA XID Errors r590 (Dec 2025 PDF)](https://docs.nvidia.com/deploy/pdf/XID_Errors.pdf)
- [NVIDIA DCGM Health Monitor API](https://docs.nvidia.com/datacenter/dcgm/2.4/dcgm-api/dcgm-api-health-mon.html)
- [Abhik Sarkar — Complete NVIDIA XID Error Field Guide](https://www.abhik.ai/articles/nvidia-xid-errors)
- [Modal — GPU Health (production guide)](https://modal.com/docs/guide/gpu-health)
- [NVIDIA — DCGM Diagnostic Tool (`dcgmi diag`)](https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/dcgm-diagnostics.html)
- [NVIDIA — GPU Debug Guidelines](https://docs.nvidia.com/deploy/gpu-debug-guidelines/index.html)
