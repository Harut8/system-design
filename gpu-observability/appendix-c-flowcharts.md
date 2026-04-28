# Appendix C — Troubleshooting Flowcharts

> Decision trees for the five most common GPU-observability scenarios. When an alert fires at 3 AM, the on-call follows a flowchart, not a textbook. Print these and stick them on the wall.

Each flowchart starts from a **symptom** the on-call sees on a dashboard or in a page. Each branch leads to a **resolution** (fix, escalation, RMA) or to a deeper sub-flow in another chapter.

These are written in Mermaid. They render in GitHub, GitLab, most Markdown previewers, and Grafana 10+ markdown panels.

---

## C.1 "GPU Is Slow" — Throttle / Performance Drop

The most common page. SM_active or step-time degraded but no hard failure.

```mermaid
flowchart TD
    Start([Alert: GPU slow / step time regressed]) --> Q1{DCGM_FI_DEV_CLOCK_THROTTLE_REASONS != 0?}
    Q1 -->|No| Q2{Tensor active dropped?}
    Q1 -->|Yes| Decode[Decode throttle bitfield]

    Decode --> T1{HW_THERMAL bit?}
    Decode --> T2{HW_POWER_BRAKESLOWDOWN?}
    Decode --> T3{SW_POWER_CAP?}
    Decode --> T4{SW_THERMAL?}

    T1 -->|Yes| Therm[Check fan curves, ambient temp, HBM_TEMP > 85°C?<br/>→ Doc 06 §6, Doc 07 §5]
    T2 -->|Yes| Power[Check rack PDU, PSU health<br/>→ Doc 06 §6.3]
    T3 -->|Yes| Cap[Check nvidia-smi -pl, K8s power policy<br/>→ Doc 06 §6.2]
    T4 -->|Yes| SwTherm[Driver-side thermal — reseat, check thermal paste<br/>→ Doc 07 §5]

    Q2 -->|Yes| Soft[Software regression: dtype change?<br/>kernel fusion broken?<br/>→ Doc 11 §3]
    Q2 -->|No| Q3{PCIe LINK_GEN < MAX_LINK_GEN?}

    Q3 -->|Yes| Pcie[PCIe degraded — reseat or RMA<br/>→ Doc 06 §5]
    Q3 -->|No| Q4{NVLink errors rising?}

    Q4 -->|Yes| Nvlink[NVLink recovery loop — likely cable/connector<br/>→ Doc 06 §4, Doc 07 §4]
    Q4 -->|No| Q5{DRAM_active near 1.0 + SM_active normal?}

    Q5 -->|Yes| MemBound[Workload is HBM-bandwidth-bound — expected<br/>→ Doc 00 §3]
    Q5 -->|No| Profile[Capture nsys profile<br/>→ Doc 11 §4.1]
```

**Common resolutions:**
- HW thermal → cooling investigation, check ambient °C, fan duty cycle
- HW power → rack-level PSU/PDU, often correlates with neighboring nodes
- PCIe degraded → reseat card, escalate to hardware team
- NVLink errors → cable/connector reseat; if persistent, RMA the GPU

> **First-look PromQL** (paste into Grafana Explore for a node):
>
> ```promql
> # All throttle reasons broken out for a node
> DCGM_FI_DEV_CLOCK_THROTTLE_REASONS{Hostname="$node"}
> DCGM_FI_DEV_GPU_TEMP{Hostname="$node"}
> DCGM_FI_DEV_MEMORY_TEMP{Hostname="$node"}
> DCGM_FI_DEV_POWER_USAGE{Hostname="$node"}
> ```

---

## C.2 Training Straggler — One Rank Slower Than Others

A distributed training job has step time variance. Some ranks at 0.5s, one at 2.0s.

```mermaid
flowchart TD
    Start([Alert: per-rank step time variance > 30%]) --> Q1{Identify slow rank from train_step_seconds metric}
    Q1 --> Q2{Slow rank's SM_active dropped?}

    Q2 -->|Yes| Q3{Throttle reasons set?}
    Q2 -->|No| Q4{NCCL AllReduce time on slow rank > others?}

    Q3 -->|Yes| Goto1[→ Flowchart C.1 GPU slow]
    Q3 -->|No| Q5{Checkpoint just happened?}

    Q5 -->|Yes| Ckpt[Checkpoint sawtooth — expected<br/>→ Doc 04 §2.4]
    Q5 -->|No| Q6{Data loader stall?<br/>iowait on host high?}
    Q6 -->|Yes| DataLoad[CPU/disk bottleneck on this node<br/>→ Doc 06 §7]
    Q6 -->|No| ProfRank[nsys profile on slow rank<br/>→ Doc 11 §4.3]

    Q4 -->|Yes| Q7{NVLink errors on slow rank?}
    Q4 -->|No| Q8{InfiniBand counters?}

    Q7 -->|Yes| Nvlink[NVLink degraded — RMA candidate<br/>→ Doc 07 §4]
    Q7 -->|No| Q9{Same node has other slow ranks?}

    Q9 -->|Yes| Node[Node-level issue — drain & reschedule<br/>→ Doc 06 §1]
    Q9 -->|No| GpuLevel[Single-GPU fault — capture profile<br/>→ Doc 11 §4]

    Q8 -->|RX/TX low| IB[InfiniBand/RoCE issue<br/>→ Doc 06 §4.5, Doc 15]
    Q8 -->|Errors high| IBerr[IB link errors — fabric team<br/>→ Doc 15]
```

**Common resolutions:**
- Single-GPU thermal/power → drain pod, reschedule, RMA if recurring
- NVLink/IB error → fabric-team handoff
- Data loader stall → check `iowait`, dataset shard distribution
- Checkpoint sawtooth → not a real problem; tune checkpoint frequency

> **First-look PromQL:**
>
> ```promql
> # Per-rank step time, p50 and p99
> histogram_quantile(0.99, sum by (rank, le) (rate(train_step_seconds_bucket{job_id="$job_id"}[1m])))
>
> # Per-rank SM_active to confirm slow rank's GPU is actually running
> avg by (rank) (
>   DCGM_FI_PROF_SM_ACTIVE
>   * on(pod, namespace) group_left(rank) kube_pod_labels{label_job_id="$job_id"}
> )
> ```

---

## C.3 Inference p99 Spike — Latency SLO Burning

Inference service p99 latency exceeds threshold; error budget burning.

```mermaid
flowchart TD
    Start([Alert: p99 latency > SLO]) --> Q1{Spike sudden or gradual?}

    Q1 -->|Sudden| Q2{Recent deploy?}
    Q1 -->|Gradual| Q3{KV cache util climbing?}

    Q2 -->|Yes| Deploy[Compare latency before/after deploy timestamp<br/>roll back?<br/>→ Doc 09 annotations]
    Q2 -->|No| Q4{Underlying GPU SM_active or DRAM_active anomaly?}

    Q4 -->|SM low| Idle[GPU idle while requests queue —<br/>scheduler bug or batch coalescing failure<br/>→ Doc 14 §5]
    Q4 -->|DRAM saturated| Bw[Bandwidth-bound — expected for decode<br/>scale horizontally]
    Q4 -->|Looks normal| Q5{Tail latency only, not throughput?}

    Q3 -->|Yes| Kv[KV cache pressure — scale horizontally<br/>or reduce max_model_len<br/>→ Doc 14 §4]
    Q3 -->|No| Q6{Queue depth growing?}

    Q6 -->|Yes| Q7{RPS climbed?}
    Q6 -->|No| Q4

    Q7 -->|Yes| Capacity[Capacity issue — autoscale<br/>→ Doc 12]
    Q7 -->|No| Slow[Per-request slowdown — check vLLM<br/>continuous batching config<br/>→ Doc 14 §5]

    Q5 -->|Yes| Q8{Tail correlates with eviction events?}
    Q5 -->|No| Q4

    Q8 -->|Yes| Pre[Preemption or KV eviction storm<br/>→ Doc 14 §4]
    Q8 -->|No| Profile[Capture exemplar trace from spike bucket<br/>→ Doc 11 §9]
```

**Common resolutions:**
- Deploy regression → rollback, then bisect
- KV cache pressure → horizontal scale, or shrink max context
- Scheduler/batch failure → check `vllm:num_requests_waiting` vs `vllm:num_requests_running`
- Underlying hardware → drop into Flowchart C.1 / C.5

> **First-look PromQL:**
>
> ```promql
> # p99 vs p50 — is it tail or whole distribution?
> histogram_quantile(0.99, sum by (le) (rate(inference_request_duration_seconds_bucket{service="$svc"}[5m])))
> histogram_quantile(0.50, sum by (le) (rate(inference_request_duration_seconds_bucket{service="$svc"}[5m])))
>
> # vLLM internals
> vllm:num_requests_waiting{service="$svc"}
> vllm:kv_cache_usage_perc{service="$svc"}
> rate(vllm:request_preemption_total{service="$svc"}[5m])
> ```

---

## C.4 Cardinality Bomb — Prometheus Memory Climbing

Prometheus head series count or memory ballooning. Scrape latency growing.

```mermaid
flowchart TD
    Start([Alert: prometheus_tsdb_head_series climbing]) --> Q1{Recent metric/label addition?}

    Q1 -->|Yes — known change| Rollback[Rollback the change OR add relabeling drop<br/>→ Doc 08 §4]
    Q1 -->|No| Q2{Find top offending metric}

    Q2 --> Topk[topk by metric name:<br/>topk 10 count by name __name__ of group by name __name__]
    Topk --> Q3{Identify high-card label}

    Q3 --> Q4{Label is necessary for any dashboard?}

    Q4 -->|No| Drop[metric_relabel_configs drop label<br/>→ Doc 08 §4]
    Q4 -->|Yes for filter only| Move[Move label to recording rule<br/>or kube_pod_labels join<br/>→ Doc 08 §6]
    Q4 -->|Yes everywhere| Q5{Can we hash/bucket the label values?}

    Q5 -->|Yes| Bucket[Replace UUID with first-N-chars hash<br/>or bucket continuous values]
    Q5 -->|No| Recording[Pre-aggregate via recording rule<br/>drop raw metric<br/>→ Doc 08 §6]

    Drop --> Verify[Verify with prometheus_tsdb_head_series<br/>going down within 2× block period]
    Move --> Verify
    Bucket --> Verify
    Recording --> Verify
```

**Common offenders to check first:**

```promql
# Top metrics by series count
topk(20, count by (__name__) ({__name__=~".+"}))

# Top labels for a specific metric
topk(20, count by (pod) (DCGM_FI_PROF_SM_ACTIVE))
topk(20, count by (gpu_uuid) (DCGM_FI_PROF_SM_ACTIVE))
```

**Common resolutions:**
- Pod label on long-running metrics → drop, rejoin via `kube_pod_labels`
- MIG GI/CI exploded → emit per-GI only on MIG-enabled nodes
- Per-request label leaked into a metric → drop label entirely
- Stale series (pod churn) → reduce `--storage.tsdb.retention.time`

---

## C.5 ECC Errors Climbing — RMA Decision

ECC SBE rate has climbed on a specific GPU; might be approaching RMA territory.

```mermaid
flowchart TD
    Start([Alert: SBE rate spike on GPU UUID]) --> Q1{DBE count > 0?}

    Q1 -->|Yes| Imm[**RMA immediately.**<br/>Drain workloads, mark unhealthy<br/>→ Doc 07 §6]
    Q1 -->|No| Q2{Row remap failure count > 0?}

    Q2 -->|Yes — H100+ only| Imm
    Q2 -->|No| Q3{RETIRED_DBE > 0?}

    Q3 -->|Yes| Imm
    Q3 -->|No| Q4{RETIRED_SBE rate climbing over 24h?}

    Q4 -->|Yes| Pre[Mark pre_rma. Schedule drain in maintenance window.<br/>→ Doc 07 §6.2]
    Q4 -->|No| Q5{SBE clustered to specific HBM page?}

    Q5 -->|Yes — single page| Watch[Page will retire on its own; monitor<br/>→ Doc 07 §3]
    Q5 -->|No — distributed| Q6{XID 63 64 in dmesg?}

    Q6 -->|Yes| Pre
    Q6 -->|No| Norm[Background SBE rate is normal for old GPUs<br/>compare to fleet baseline<br/>→ Doc 07 §3.2]

    Imm --> Drain[1. Cordon node<br/>2. Drain pods using this GPU<br/>3. Mark gpu_lifecycle_state=in_rma<br/>4. Open RMA ticket]
    Pre --> Sched[1. Mark gpu_lifecycle_state=pre_rma<br/>2. Avoid scheduling new long jobs<br/>3. Drain at next maintenance window]
```

**RMA decision rule of thumb:**
- `RETIRED_DBE > 0` → RMA, no exceptions
- `ROW_REMAP_FAILURE > 0` (H100+) → RMA, no exceptions
- `RETIRED_SBE rate > 1/hour` for 24h → pre-RMA queue
- Persistent XID 63/64 → pre-RMA queue
- Old GPU with steady SBE rate but no retirement → monitor

> **First-look PromQL:**
>
> ```promql
> # SBE rate per-GPU, last 24h
> rate(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL[24h])
>
> # Page retirement
> DCGM_FI_DEV_RETIRED_SBE
> DCGM_FI_DEV_RETIRED_DBE
> DCGM_FI_DEV_RETIRED_PENDING
>
> # Row remap (H100+)
> DCGM_FI_DEV_ROW_REMAP_FAILURE
> DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS
> ```

---

## C.6 DCGM Stopped Reporting — Profiling Lock or Exporter Failure

Suddenly `DCGM_FI_PROF_*` series go flat or `up{job="dcgm-exporter"}=0`.

```mermaid
flowchart TD
    Start([Alert: DCGM metrics flat or scrape failing]) --> Q1{up == 0 or stale?}

    Q1 -->|up == 0| Pod[Pod is down — kubectl get pod<br/>check restart count, OOM, image]
    Q1 -->|stale only| Q2{All fields stale or only PROF?}

    Q2 -->|All| Engine[Host engine crash — check logs<br/>kubectl logs on dcgm-exporter pod]
    Q2 -->|Only PROF| Lock[Profiling lock contention<br/>→ Doc 02 §4]

    Lock --> Q3{ncu / nsys running on this node?}
    Q3 -->|Yes| Pause[Run dcgmi profile --pause -i N<br/>or kill the conflicting tool<br/>→ Doc 02 §4.2]
    Q3 -->|No| Q4{vendor agent on host?}
    Q4 -->|Yes| Vendor[GPU vendor diagnostic agent took the lock<br/>coordinate with vendor team]
    Q4 -->|No| Restart[dcgm-exporter restart usually clears<br/>kubectl rollout restart]

    Pod --> Q5{Restart count > 5 in 1h?}
    Q5 -->|Yes| Logs[Check pod logs: libdcgm version mismatch<br/>nv-hostengine TCP issue<br/>→ Doc 02 §1.1]
    Q5 -->|No| Wait[Pod auto-recovers — verify metrics resume]
```

**Common resolutions:**
- Profiling lock → pause/kill conflicting tool, or `dcgmi profile --pause`
- Pod OOM → bump memory request on DaemonSet
- Version skew → align `libdcgm.so` (in image) with `nv-hostengine` version
- Host engine crash → restart pod; if recurring, check XID errors on the GPU (host engine crashes often follow GPU faults)

---

## C.7 Idle GPU Hunt — Where Is the Waste

Cluster utilization scoreboard shows wasted $; need to find the worst offenders.

```mermaid
flowchart TD
    Start([Goal: find idle GPUs eating budget]) --> Q1[Query: SM_active < 0.05 for > 30m AND pod != empty]

    Q1 --> List[Per-pod waste table — Doc 05 §5]
    List --> Q2{Workload class?}

    Q2 -->|Notebook / Jupyter| Nb[Auto-suspend after N min idle<br/>→ Doc 04 §4]
    Q2 -->|Inference idle| Inf[Scale to zero or right-size<br/>→ Doc 12 §4]
    Q2 -->|Training stuck| Stuck[Job hung — kill or alert owner<br/>→ Doc 04 §2.5]
    Q2 -->|Allocated, never started| Unsched[Init container or image pull stuck<br/>→ Doc 03 §5]
    Q2 -->|Allocated, finished, not torn down| Leaked[Job stop hook missed<br/>→ K8s pod-finalizer audit]

    Nb --> Pol[Policy: auto-cull after 2h idle<br/>→ Doc 13 §5]
    Inf --> Pol
    Stuck --> Pol
    Leaked --> Pol
```

> **First-look PromQL:**
>
> ```promql
> # Top 20 pods burning $ on idle GPUs (right_now snapshot)
> topk(20,
>   sum by (pod, namespace) (
>     (DCGM_FI_PROF_SM_ACTIVE < bool 0.05)
>     * on(pod, namespace) group_left() kube_pod_labels{label_workload_class!=""}
>   )
> )
> ```

---

## C.8 How to Use These Flowcharts

1. **Print or pin to runbooks.** The point of a flowchart is that you don't read it — you follow it.
2. **Link from alert annotations.** Each Doc 10 alert should `runbook_url` to the relevant flowchart.
3. **Pair with first-look PromQL.** Every flowchart includes a copy-paste block; the on-call should land in Grafana Explore with these queries within 30 seconds of being paged.
4. **Update after every incident.** If you encountered a branch the flowchart didn't cover, add it. Flowcharts are living documents.

---

## Sources

- [NVIDIA DCGM Diagnostics](https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/dcgm-diagnostics.html)
- [NVIDIA Throttle Reasons (NVML)](https://docs.nvidia.com/deploy/nvml-api/group__nvmlClocksThrottleReasons.html)
- [Mermaid — flowchart syntax](https://mermaid.js.org/syntax/flowchart.html)
- Doc 06, Doc 07, Doc 10, Doc 11 — the chapters these flowcharts source resolutions from
