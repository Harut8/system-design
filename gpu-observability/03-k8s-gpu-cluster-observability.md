# 03 — Kubernetes GPU Cluster Observability

> Everything DCGM cannot tell you. The cluster's view of GPUs: who allocated what, what got rejected, where the topology is, what the scheduler is doing, and how the autoscaler responds.

DCGM (doc 02) is hardware-side. This doc is the Kubernetes-side: how the *scheduler* sees GPUs, what `kube-state-metrics` and the device plugin emit, and how to correlate hardware telemetry with pod identity.

---

## 1. The Three Views of "a GPU"

A single H100 is simultaneously:

| View | Source | Identifier |
|------|--------|------------|
| **Hardware** | DCGM, NVML | `gpu_uuid` (e.g., `GPU-34319582-d595-...`) |
| **Linux device** | kernel, container runtime | `/dev/nvidia0` (PCI BDF: `0000:1a:00.0`) |
| **Kubernetes resource** | device plugin → API server | `nvidia.com/gpu: 1` (an opaque integer count) |

Bridging these is the platform team's job. The mapping:

- Device plugin reports IDs (UUIDs) per node via gRPC.
- Kubelet exposes the binding via `pod-resources` socket: which pod got which UUID.
- DCGM-exporter calls that socket and tags metrics with `pod`, `namespace`, `container`.
- kube-state-metrics emits `kube_pod_container_resource_requests{resource="nvidia_com_gpu"}` from the API server's view.

If those three sources ever disagree (KSM says 8 GPUs requested, DCGM only has 6 with pod labels), you have a real bug. We'll write the alert in §10.

---

## 2. Device Plugin Telemetry

The NVIDIA device plugin (`k8s-device-plugin`, deployed as DaemonSet) advertises GPUs to kubelet via the Device Plugin API. It exposes:

### 2.1 Node-level capacity / allocatable / allocated

```promql
# Total GPUs the node has
kube_node_status_capacity{resource="nvidia_com_gpu"}

# Allocatable (capacity minus unhealthy GPUs)
kube_node_status_allocatable{resource="nvidia_com_gpu"}

# Currently allocated to pods
kube_node_status_allocatable{resource="nvidia_com_gpu"}
  -
sum by (node) (
  kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
  * on(pod, namespace) group_left()
  kube_pod_info
)
```

The capacity vs allocatable gap is the key health signal: when an unhealthy GPU is detected (XID 79, repeated DBE), kubelet decreases `allocatable` *but not* `capacity`. So:

```promql
# Unhealthy GPUs (capacity - allocatable, per node)
kube_node_status_capacity{resource="nvidia_com_gpu"}
  - kube_node_status_allocatable{resource="nvidia_com_gpu"}
```

This is the cleanest "is hardware quietly degrading" signal you can write.

### 2.2 Per-pod GPU requests

```promql
# Total GPUs requested by pod
sum by (namespace, pod) (
  kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
)

# Pods that request GPUs but aren't running yet
kube_pod_status_phase{phase="Pending"}
  * on(pod, namespace) group_left()
  (sum by (pod, namespace) (
    kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
  ) > 0)
```

The second one is your "GPU-pending" panel — the pods waiting for GPU capacity, which feeds the autoscaler triggers in §7.

### 2.3 Device plugin own metrics

The plugin itself emits a few counters at `:9408/metrics` (off by default, enable via `--metrics`):

- `nvidia_gpu_devices_total` — devices it has registered with kubelet
- `nvidia_gpu_devices_unhealthy` — devices it reported unhealthy
- Re-registration counts on plugin restarts

Worth scraping if you've seen device plugin instability; otherwise low priority.

---

## 3. kube-state-metrics: GPU-relevant series

kube-state-metrics has no GPU-specific extensions — but it's the API-server-truth source for the resource requests/limits, which is what *should* match what's actually scheduled. Useful series:

| Metric | Use |
|--------|-----|
| `kube_pod_container_resource_requests{resource="nvidia_com_gpu"}` | What was asked for |
| `kube_pod_container_resource_limits{resource="nvidia_com_gpu"}` | What was capped at |
| `kube_pod_status_phase` | Pending / Running / Failed |
| `kube_pod_status_reason` | OOMKilled, NodeLost, Evicted |
| `kube_node_status_condition` | Ready, MemoryPressure, DiskPressure, PIDPressure |
| `kube_node_status_capacity{resource="nvidia_com_gpu"}` | Total GPUs per node |
| `kube_resourcequota{resource="requests.nvidia.com/gpu"}` | Namespace quota |
| `kube_pod_owner` | Walk up to Job/Deployment for grouping |

Composite query that answers "show me all pods consuming GPU and what they belong to":

```promql
sum by (namespace, owner_kind, owner_name) (
  kube_pod_container_resource_requests{resource="nvidia_com_gpu"} > 0
  * on(pod, namespace) group_left(owner_kind, owner_name)
  kube_pod_owner
)
```

---

## 4. Node-Level Topology: NVLink, PCIe, NUMA

Topology determines whether a multi-GPU job runs at full NVLink speed (~600 GB/s on H100 NVLink-4) or limps over PCIe (~64 GB/s on Gen5 x16). The scheduler doesn't know about this by default.

### 4.1 `nvidia-smi topo -m`

```
        GPU0  GPU1  GPU2  GPU3  GPU4  GPU5  GPU6  GPU7  CPU Affinity  NUMA Affinity
GPU0     X    NV18  NV18  NV18  NV18  NV18  NV18  NV18   0-47           0
GPU1   NV18    X    NV18  NV18  NV18  NV18  NV18  NV18   0-47           0
GPU2   NV18  NV18    X    NV18  NV18  NV18  NV18  NV18   0-47           0
GPU3   NV18  NV18  NV18    X    NV18  NV18  NV18  NV18   0-47           0
GPU4   NV18  NV18  NV18  NV18    X    NV18  NV18  NV18  48-95           1
GPU5   NV18  NV18  NV18  NV18  NV18    X    NV18  NV18  48-95           1
GPU6   NV18  NV18  NV18  NV18  NV18  NV18    X    NV18  48-95           1
GPU7   NV18  NV18  NV18  NV18  NV18  NV18  NV18    X   48-95           1
```

Legend (memorize these):

| Code | Meaning | Bandwidth ballpark |
|------|---------|-------------------|
| `X` | Self | — |
| `NV#` | NVLink with N bonded links | 25 GB/s × N per direction |
| `PIX` | Single PCIe switch (PXB) | 16–64 GB/s |
| `PXB` | Multiple PCIe bridges, same NUMA | 16 GB/s |
| `PHB` | PCIe host bridge (CPU root complex) | 16 GB/s |
| `NODE` | Crosses NUMA inside same socket | slower, NUMA penalty |
| `SYS` | Crosses NUMA across QPI/UPI sockets | worst |

Above is an HGX H100 8-GPU baseboard: every pair has NV18 (18 NVLinks bonded, ~900 GB/s). Compare to a non-NVLink box: any GPU pair shows `SYS` and your AllReduce takes the long way through CPU memory.

### 4.2 Exporting topology

The standard pattern is a small DaemonSet sidecar (or init job) that runs `nvidia-smi topo -mp` (the parseable form), parses the matrix, and emits Prometheus metrics:

```promql
# Synthetic metric, emitted by topo-exporter
gpu_pair_link_type{
   node="gpu-014",
   src_gpu="0",
   dst_gpu="1",
   link_type="NV18"
} 1

gpu_numa_affinity{node="gpu-014", gpu="0", numa_node="0"} 1
```

These are slow-moving (rarely change without a reboot) so emit once per minute, not per scrape.

### 4.3 Topology Manager and Topology-aware scheduling

The vanilla scheduler doesn't read this. Two options:

- **Kubelet Topology Manager** (`single-numa-node` policy): rejects pods that can't be placed with all their devices (GPU + NIC) on the same NUMA node. Coarse — it only enforces, doesn't optimize across nodes.
- **Custom scheduler / Volcano / Kueue / NVIDIA DRA driver**: knows the NVLink fabric, prefers placements where requested GPUs are NVLink-connected. Required for serious distributed training.

Observability for this:

```promql
# Requests for >1 GPU that landed on non-NVLink-connected GPUs (bad)
count by (namespace, pod) (
  pod_gpu_assignment{node=~".+"}  # custom: emitted by topology-aware scheduler
) > 1
  unless on(node, pod, namespace)
  pod_gpus_nvlink_connected{} == 1
```

If you don't run a topology-aware scheduler, this query is useful as a *retroactive* check: count multi-GPU pods, then for each one cross-reference `gpu_pair_link_type` to see if their GPUs are NVLink-connected. The answers will surprise you.

---

## 5. Pod-to-GPU Binding Visibility

The single most-used query in any GPU dashboard:

```promql
# Which pod is using which GPU UUID right now?
DCGM_FI_DEV_GPU_TEMP{pod!=""}
```

The `pod!=""` filter is shorthand for "GPUs currently allocated to a workload." DCGM-exporter populates `pod`, `namespace`, `container` from the kubelet's `pod-resources` socket (doc 02 §6.2).

But this only works for **whole-GPU and MIG allocations**. For:

- **Time-sliced GPU**: the device plugin advertises N "slots" per GPU, so multiple pods land on the same UUID. DCGM-exporter sees only one pod label per metric — the rest are invisible. *Time-slicing is the observability dark mode.*
- **MPS shared GPU**: same problem, multiple pods share one CUDA context, dcgm-exporter sees one.

If you've enabled time-slicing/MPS, you must instrument at the *workload* level (per-process metrics, doc 14) because the platform-level metrics conflate tenants.

### 5.1 Job-level views (doc 4 expands)

Roll up pod assignments to logical groupings:

```promql
# GPUs currently held by training jobs
count(
  DCGM_FI_DEV_GPU_TEMP{pod!=""}
  * on(pod, namespace) group_left(owner_kind)
  kube_pod_owner{owner_kind="Job"}
)

# GPUs held by inference services
count(
  DCGM_FI_DEV_GPU_TEMP{pod!=""}
  * on(pod, namespace) group_left(owner_kind)
  kube_pod_owner{owner_kind="ReplicaSet"}
)
```

---

## 6. GPU Sharing Modes — Observability Differences

Three sharing modes, three observability stories:

### 6.1 Time-slicing

The device plugin reports N×real GPUs as allocatable (e.g., 8 physical → 32 advertised at slice=4). Pods land arbitrarily on slots. The kernel-level GPU context-switches between them.

**What you see:** the GPU's DCGM metrics are aggregate across all sharing pods. You cannot tell which pod's kernel ran when.
**What you can't:** per-tenant GPU utilization, per-tenant memory bandwidth, per-tenant SM activity. The blast radius of a noisy neighbor is invisible at this layer.
**Workaround:** application-level metrics from each tenant (PyTorch profiler, vLLM metrics) joined to platform metrics by `pod` label and time.

### 6.2 MPS (Multi-Process Service)

A single CUDA context is multiplexed; multiple processes' kernels run concurrently on different SMs. Higher throughput than time-slicing because there's no context switch.

**What you see:** same problem — DCGM sees one pod label, aggregate across processes. With H100+ multi-instance profiling you can sometimes get per-context counters, but it's brittle.
**Memory isolation: none.** A bug in one tenant can OOM the other.

### 6.3 MIG

Hardware-level partitioning. Each GI is its own mini-GPU with its own SMs, L2 slice, and HBM partition. Independent ECC. Independent contexts.

**What you see:** *full DCGM observability per partition.* This is why MIG is the production-standard sharing mode for inference: utilization metrics are per-tenant, ECC is per-tenant, memory pressure is per-tenant. Cardinality cost is real (doc 02 §5) but the operational model is sane.

| Sharing mode | Per-tenant DCGM | Memory isolation | Fault isolation | Use case |
|--------------|------------------|------------------|-----------------|----------|
| Time-slicing | ❌ | ❌ | ❌ | Dev/test, low-stakes notebooks |
| MPS | ❌ | ❌ | partial | HPC, trusted multi-process apps |
| MIG | ✅ | ✅ | ✅ | Multi-tenant inference |

---

## 7. Scheduler-Level Metrics

When a pod requesting `nvidia.com/gpu: 1` can't be scheduled, the chain is:

```
  PodSpec → kube-scheduler → no node satisfies → Pending + scheduling event
                                                 → cluster-autoscaler triggers
                                                 → cloud provider provisions node
                                                 → device plugin registers GPUs
                                                 → kubelet allocatable updates
                                                 → scheduler retries
```

Each hop is observable.

### 7.1 Pending GPU pods

```promql
# How many GPU pods are pending right now
sum(
  kube_pod_status_phase{phase="Pending"}
  * on(pod, namespace) group_left()
  (sum by (pod, namespace) (
    kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
  ) > 0)
)

# How long has each been pending
time() - kube_pod_created
  * on(pod, namespace) group_left()
  kube_pod_status_phase{phase="Pending"} == 1
```

### 7.2 Scheduler attempts

The scheduler emits its own metrics at `kube-scheduler:10259/metrics`:

| Metric | Use |
|--------|-----|
| `scheduler_pending_pods` | Pods in scheduling queue (active/backoff/unschedulable) |
| `scheduler_pod_scheduling_attempts` | Retries before success |
| `scheduler_pod_scheduling_duration_seconds` | End-to-end scheduling latency |
| `scheduler_unschedulable_pods` | Pods the scheduler gave up on |

```promql
# p95 scheduling latency for GPU pods (requires custom labelling on scheduler)
histogram_quantile(0.95,
  rate(scheduler_pod_scheduling_duration_seconds_bucket[5m])
)
```

### 7.3 Cluster autoscaler

Cluster Autoscaler (CA) emits at `:8085/metrics`:

| Metric | Use |
|--------|-----|
| `cluster_autoscaler_unschedulable_pods_count` | Trigger for scale-up |
| `cluster_autoscaler_nodes_count{state="ready"}` | Current node count by state |
| `cluster_autoscaler_scaled_up_nodes_total` | Cumulative scale-up events |
| `cluster_autoscaler_scaled_down_nodes_total` | Cumulative scale-down events |
| `cluster_autoscaler_failed_scale_ups_total` | Failures (out of quota, AZ exhausted) |

Scale-up latency expectation per the upstream Cluster Autoscaler docs:

| Cluster size | Avg | Worst case |
|--------------|-----|-----------|
| <100 nodes | ~5s | 30s |
| 100–1000 nodes | ~15s | 60s |

Add cloud provider provisioning time (60–300s for a GPU instance) and the wall-clock latency for "pod requests GPU → pod gets GPU" lands at 1–6 min on cold scale-up.

For Karpenter (the increasingly-preferred replacement) the equivalent metrics live under `karpenter_*` and provisioning is sub-minute when the node template is warm.

### 7.4 GPU node scale-up failures

Three common modes worth dedicated alerts:

```promql
# Out of quota — won't self-heal, page someone
increase(cluster_autoscaler_failed_scale_ups_total{reason="quotaExceeded"}[15m]) > 0

# AZ exhausted — usually transient, ticket
increase(cluster_autoscaler_failed_scale_ups_total{reason="zoneCapacity"}[15m]) > 0

# Pod has been pending > 10m, autoscaler has tried > 3 times
(time() - kube_pod_created
  * on(pod, namespace) group_left()
  kube_pod_status_phase{phase="Pending"}) > 600
  and on(pod, namespace) (
    kube_pod_container_resource_requests{resource="nvidia_com_gpu"} > 0
  )
```

---

## 8. Node Taints, Labels, and Pool Tracking

GPU nodes are typically tainted `nvidia.com/gpu=present:NoSchedule` (only pods with matching toleration land there) and labelled with several attributes the scheduler uses for placement:

| Label | Provider | Purpose |
|-------|----------|---------|
| `nvidia.com/gpu.present=true` | GPU operator | Yes, this node has a GPU |
| `nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3` | GPU operator | Model |
| `nvidia.com/gpu.count=8` | GPU operator | How many on this node |
| `nvidia.com/gpu.memory=81559` | GPU operator | HBM in MiB |
| `nvidia.com/mig.config=...` | GPU operator | MIG profile applied |
| `node.kubernetes.io/instance-type=p5.48xlarge` | cloud provider | EC2/GCE instance type |
| `topology.kubernetes.io/zone=us-east-1a` | cloud provider | AZ |

Pool/capacity tracking dashboard query:

```promql
# Capacity by GPU model and AZ
sum by (gpu_product, zone) (
  kube_node_labels{label_nvidia_com_gpu_product!=""}
  * on(node) group_left(label_nvidia_com_gpu_count)
  kube_node_labels
)
```

(That query needs `kube_node_labels` with relabeling enabled in KSM to expose the labels as series — covered in the kube-prometheus-stack defaults but worth confirming.)

---

## 9. OOM, Eviction, and Device Health Events

GPU pods get killed in three distinct ways and you need to tell them apart:

| Cause | Signal | Detection |
|-------|--------|-----------|
| **CPU/memory OOM** | OOMKilled, container_last_seen reset | `kube_pod_container_status_terminated_reason{reason="OOMKilled"}` |
| **GPU OOM (CUDA OOM)** | container exits non-zero, no kernel OOM kill | App-level metric / log; not visible to k8s |
| **Device unhealthy eviction** | kubelet evicts pod when device plugin marks GPU unhealthy | `kube_pod_status_reason{reason=~"NodeLost|DeviceFailed"}` + simultaneous `kube_node_status_allocatable` drop |

**Important:** CUDA OOM is invisible to Kubernetes. Your training job hits `torch.cuda.OutOfMemoryError`, the process exits, the container restarts, k8s says "container exited with code 1." You must instrument at the application layer (`pytorch_cuda_oom_total`) and wire it into Prometheus to catch this distinctly.

```promql
# Frequent restarts on GPU pods (likely CUDA OOM if no k8s OOM signal)
increase(kube_pod_container_status_restarts_total[15m])
  * on(pod, namespace) group_left()
  (sum by (pod, namespace) (
    kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
  ) > 0)
  > 3
  unless on(pod, namespace)
  kube_pod_container_status_terminated_reason{reason="OOMKilled"}
```

Translation: GPU pod restarted >3 times in 15m, but it wasn't a host-memory OOM. Investigate as suspected CUDA OOM or driver issue.

---

## 10. The Cross-Source Sanity Alert

The single most valuable cross-cutting alert in the whole observability stack — it catches the silent failures where pieces disagree:

```promql
# K8s thinks N GPUs are allocated; DCGM has metrics for M; alert if they differ
abs(
  sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"})
  -
  count(DCGM_FI_DEV_GPU_TEMP{pod!=""})
) > 5
```

If this fires:
- DCGM-exporter pods are crashed on some nodes (run kubectl get ds dcgm-exporter)
- Some GPUs are scheduled to pods but DCGM has no data (XID 79?)
- Time-slicing is on and the count math is wrong (expected; suppress the alert in those namespaces)

---

## 11. Phase 1 Acceptance Checklist

You're done with Phase 1 (docs 01 + 02 + 03) when all of this is true:

- [ ] DaemonSet `dcgm-exporter` is Running on every node with `nvidia.com/gpu.present=true`
- [ ] `up{job="dcgm-exporter"}` is 1 for 100% of GPU nodes
- [ ] `count(DCGM_FI_PROF_SM_ACTIVE)` returns the expected GPU count
- [ ] Pod labels populated: `count(DCGM_FI_DEV_GPU_TEMP{pod!=""}) > 0`
- [ ] kube-state-metrics `kube_pod_container_resource_requests{resource="nvidia_com_gpu"}` returns
- [ ] Node labels present: `count(kube_node_labels{label_nvidia_com_gpu_product!=""})` > 0
- [ ] At least one Grafana panel renders showing live GPU temps and SM_active
- [ ] Pending-GPU-pod count is queryable
- [ ] Cluster autoscaler metrics scrape successfully
- [ ] Cross-source sanity alert defined (§10)

If any of those are false, do not start Phase 2 — the upstream docs assume them.

---

## 12. Forward References

- **Allocation efficiency vs utilization efficiency**: doc 05 builds on this to define "GPU waste" as cost-attributed
- **Per-job dashboards (training/inference)**: doc 04 uses `kube_pod_owner` joins shown in §3
- **NVLink topology metrics → distributed training health**: doc 15 uses §4 metrics to detect AllReduce stragglers
- **Multi-tenant RBAC on these metrics**: doc 13

---

## Sources

- [NVIDIA k8s-device-plugin (GitHub)](https://github.com/NVIDIA/k8s-device-plugin)
- [Kubernetes — Device Plugins](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/)
- [Kubernetes — Schedule GPUs](https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/)
- [Cluster Autoscaler FAQ — scale-up latency](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md)
- [NVIDIA — Time-Slicing GPUs in Kubernetes](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html)
- [Kubenatives — MIG vs Time-Slicing vs MPS](https://www.kubenatives.com/p/mig-vs-time-slicing-vs-mps-which)
- [NVIDIA DGX BasePOD — Validate NVLink topology](https://docs.nvidia.com/dgx-basepod/deployment-guide-dgx-basepod/latest/nvlink.html)
- [Kubernetes 1.36 GPU scheduling patterns (debugg.ai)](https://debugg.ai/resources/kubernetes-gpu-scheduling-2025-kueue-volcano-mig)
- [CNCF — Reclaiming underutilized GPUs (Jan 2026)](https://www.cncf.io/blog/2026/01/20/reclaiming-underutilized-gpus-in-kubernetes-using-scheduler-plugins/)
- [NVIDIA Tech Blog — Monitoring GPUs in K8s with DCGM](https://developer.nvidia.com/blog/monitoring-gpus-in-kubernetes-with-dcgm/)
