# 16 — Incident Walkthrough: Tracing a Real Slowdown End-to-End

> The capstone. We take one realistic incident — a 7B-parameter LLM fine-tuning job suddenly running 30% slower — and trace it through every layer of the stack: hardware silicon → DCGM → Prometheus → Kubernetes → Grafana → alerting → RMA. Every step references the doc that explains the layer. By the end you should be able to debug an arbitrary GPU incident by walking the same path.

This chapter is **the synthesis exercise**. Don't read it before Doc 02 and Doc 07 — the references won't make sense. Read it after you've gone through 01–15 once.

---

## 1. The Incident: What Was Reported

```
Slack #ml-platform — 14:32 UTC, Tue 2026-04-21

@oncall — our 7B LoRA fine-tune (job-id: ft-llama7b-2026-04-21-001)
started a checkpoint 18 min ago and never recovered. Step time
was 0.45s steady; now it's 0.70s and climbing. 8 H100 nodes,
64 GPUs, FSDP. Nothing changed on our side. Help?
```

That's all the user gave us. Step time went from 0.45s to 0.70s, ~55% regression. No deploy, no config change.

We're going to walk through what the on-call sees, in order, building the diagnosis from the metrics they have.

---

## 2. The First Look — L4 Job Dashboard (Doc 09 §5)

The on-call opens the L4 training dashboard scoped to `job_id="ft-llama7b-2026-04-21-001"`.

```
   ┌─────────────────────────────────────────────────────────┐
   │  Step time per rank (p50, last 1h)                      │
   │                                                         │
   │  0.85s ┤                                       ╭─       │
   │  0.70s ┤                                ╭─────╯         │
   │  0.55s ┤              ╭────────────────╯                │
   │  0.45s ┤──────────────╯                                 │
   │        └──────────────┬──────┬──────┬──────┬──────      │
   │       13:30        14:00  14:15  14:30                  │
   │                                                         │
   │  ◀── checkpoint marker (annotation)                     │
   └─────────────────────────────────────────────────────────┘
```

**What we see:**
- Step time was 0.45s → jumped to ~0.55s after the 14:14 checkpoint annotation → kept climbing → now 0.70s.
- This is the first clue: **the regression coincided with a checkpoint, but kept getting worse afterwards** — not the checkpoint sawtooth that Doc 04 §2.4 describes.

Next panel down: per-rank step time variance.

```
   ┌─────────────────────────────────────────────────────────┐
   │  histogram_quantile(0.99) by rank vs (0.50) by rank     │
   │                                                         │
   │  rank 0..62 ── tight cluster, p99 ≈ p50 + 5%            │
   │  rank 47    ── p99 ≈ 2× p50                             │
   └─────────────────────────────────────────────────────────┘
```

**Rank 47 is the straggler.** Doc 04 §2.3 + Doc 15 §3 cover this pattern. With FSDP, one slow rank tanks every AllReduce, which is why *all* ranks' step time degraded — they're all blocking on the slowest rank's grad-reduce.

We've narrowed scope. The flowchart we follow now is **Appendix C.2 (training straggler)**.

---

## 3. Identify the Slow Rank's GPU — Cross-Layer Join (Doc 03)

The on-call needs the GPU UUID for rank 47. The path:

```
   PyTorch rank ─→ pod label ─→ kube_pod_labels ─→ DCGM metric label
```

PromQL (the join pattern from Doc 03 §6):

```promql
# Find the GPU UUID assigned to rank 47
DCGM_FI_DEV_GPU_TEMP{Hostname!=""}
* on(pod, namespace) group_left(rank, label_job_id)
  kube_pod_labels{label_job_id="ft-llama7b-2026-04-21-001", rank="47"}
```

Returns:

```
{Hostname="hgx-h100-node-12", UUID="GPU-3a4f...", gpu="3", pod="ft-llama7b-...-rank-47", rank="47"}
```

**Suspect GPU:** node `hgx-h100-node-12`, GPU index 3, UUID `GPU-3a4f...`. Now we drop to L3 (node view).

---

## 4. L3 — Node Dashboard for `hgx-h100-node-12` (Doc 09 §4)

Open the node dashboard. All 8 GPUs visible side-by-side.

```
   Per-GPU SM_active (last 30m):
   GPU 0..2, 4..7  ──  ~0.92 steady
   GPU 3           ──  was 0.92, dropped to 0.65 at 14:14, still 0.65

   Per-GPU GPU_TEMP:
   GPU 0..2, 4..7  ──  74°C
   GPU 3           ──  68°C  ← cooler than peers (ran less, but suspicious)

   Per-GPU MEMORY_TEMP:
   GPU 0..2, 4..7  ──  82°C
   GPU 3           ──  79°C

   Per-GPU POWER_USAGE:
   GPU 0..2, 4..7  ──  ~680 W
   GPU 3           ──  ~480 W  ← significantly less
```

**What we see:** GPU 3 is doing less work than its peers, despite being on the same FSDP shard pattern (which means the workload is symmetric — they should be identical).

Cooler + less power + lower SM_active + same workload = **GPU 3 is throttled**.

Now we go to the throttle reason panel.

---

## 5. Throttle Reason Decode (Appendix B §B.4)

L3 dashboard panel: `DCGM_FI_DEV_CLOCK_THROTTLE_REASONS{Hostname="hgx-h100-node-12", gpu="3"}`.

Value: `0x42` (decimal 66).

Decoding the bitfield (Appendix B §B.4.1):

```
0x42 = 0x40 (HW_THERMAL) | 0x02 (APPLICATIONS_CLOCKS_SETTING)

Wait — 0x40 is HW_THERMAL?
But GPU temp is 68°C and HBM temp is 79°C — not hot.
```

**This is the puzzle.** HW thermal throttle is firing, but the GPU isn't actually hot. We have two layers of evidence in conflict.

The on-call moves to the L5 dashboard for this single GPU's UUID — the forensic view.

---

## 6. L5 — Single GPU, Last 24h (Doc 09 §6)

Time-series comparison, last 24 hours, GPU `3a4f...`:

```
   GPU_TEMP        ── 68°C  flat
   MEMORY_TEMP     ── 79°C  flat
   POWER_USAGE     ── dropped from 680→480 W at 14:14
   SM_CLOCK        ── dropped from 1980 MHz → 1410 MHz at 14:14
   THROTTLE_REASONS── 0 → 0x42 at 14:14
   PCIE_LINK_GEN   ── 5 (max) — fine
   NVLINK_REPLAY_ERR ─ ────  flat zero
   NVLINK_RECOVERY ── 0 → 23 → 41 → 67 (climbing!) starting 14:11
```

**Found it.** NVLink recovery errors started 3 minutes *before* the throttle and step-time regression. The throttle reason and the user-visible slowdown are downstream effects.

Cross-reference: Doc 06 §4 (NVLink errors) and Doc 07 §4 (NVLink as failure precursor).

> **Pitfall avoided:** the on-call could have stopped at "HW_THERMAL throttle" and gone hunting for cooling problems. The temperature data and the NVLink data pointed elsewhere. Always read multiple metrics before forming a hypothesis.

The mechanism: NVLink CRC errors at the physical layer trigger driver-level recovery. The H100's safety logic reduces clocks to keep the link stable while it retries. That looks like a "thermal" throttle reason because the driver multiplexes `HW_SLOWDOWN` causes onto the same bit on some firmware versions.

---

## 7. Hardware-Health Cross-Check (Doc 07)

Pull the XID counter for this GPU:

```promql
gpu_xid_total{UUID="GPU-3a4f..."}
```

Result: XID 74 (NVLink error) appeared 12 times in the last 30 minutes.

XID 74 — Doc 07 §2:

> **XID 74 — NVLink Error.** Driver detected a recoverable NVLink fault. If recurring, the link is degrading. Watch for escalation to XID 79 (GPU fell off bus).

We also pull ECC and remap:

```
DCGM_FI_DEV_ECC_DBE_VOL_TOTAL  ── 0
DCGM_FI_DEV_RETIRED_DBE        ── 0
DCGM_FI_DEV_ROW_REMAP_FAILURE  ── 0
DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL ── 0 → 8 → 19 → 34
```

**The picture:** NVLink CRC errors at the silicon level → driver recovers but logs XID 74 → driver throttles to keep the link stable → user sees slow step time. No ECC, no DBE, no RMA-blocking signal *yet*. But the trend is bad.

This is the moment to follow **Appendix C.2 → "NVLink degraded — RMA candidate"** path.

---

## 8. Capture a Profile Before Anything Else (Doc 11 §4.3)

Standard incident playbook: capture an Nsight trace before any mitigation, in case the user asks "what kernel was the AllReduce stuck on?"

The on-call uses the alert-triggered capture pattern from Doc 11 §4.3:

```bash
# Alert-triggered capture script
kubectl exec -n ml-platform ft-llama7b-rank-47 -- \
  dcgmi profile --pause -i 3   # release lock first (Doc 02 §4)

kubectl exec -n ml-platform ft-llama7b-rank-47 -- \
  nsys profile \
    --output=/captures/incident-2026-04-21-rank47.nsys-rep \
    --trace=cuda,nvtx,nccl \
    --duration=60 \
    -- /proc/1/exe

aws s3 cp /captures/incident-2026-04-21-rank47.nsys-rep \
  s3://gpu-profiles/incidents/

kubectl exec -n ml-platform ft-llama7b-rank-47 -- \
  dcgmi profile --resume -i 3
```

Open the trace in Nsight Systems — the AllReduce phase shows ~120ms of NCCL `recv` blocked, with the NCCL log fragment captured alongside:

```
NCCL INFO Connection from rank 46 -> 47 retry 3 (NVLink down)
NCCL INFO ## AllReduce dur 119.42ms (expected ~25ms)
```

NCCL was retrying because of NVLink instability between GPUs 2 and 3 on this node (GPU 3 talks to GPU 2 over NVLink for tensor-parallel partner). **The trace confirms the metric story.**

---

## 9. The Decision Tree — Now What? (Doc 07 §6, Appendix C.5)

Diagnosis is complete. The action depends on policy:

```
   ┌─────────────────────────────────────────────────────────┐
   │  Severity assessment for GPU 3 on hgx-h100-node-12     │
   │                                                         │
   │  Hard failure?      No — link recovers, GPU still alive │
   │  RMA-blocking?      Not yet — no DBE, no row remap fail │
   │  Workload-affecting? Yes — straggler degrading 64-GPU   │
   │                     job 30% across the board            │
   │                                                         │
   │  Decision: drain the pod, mark GPU pre_rma, schedule    │
   │  hardware investigation in next maintenance window.     │
   └─────────────────────────────────────────────────────────┘
```

The actions, in order:

1. **Restart the training job** (it's stuck and bleeding budget).
   - Save checkpoint, kill, relaunch with `--exclude-nodes=hgx-h100-node-12` (Doc 04 §2.5 — the restart pattern). FSDP will reshard onto the remaining 7 nodes × 8 GPUs = 56 GPUs (acceptable for a fine-tune).

2. **Mark the GPU as pre-RMA** in the lifecycle exporter (Doc 07 §6.2):
   ```bash
   curl -X POST gpu-lifecycle-exporter:8080/api/state \
     -d '{"uuid":"GPU-3a4f...", "state":"pre_rma", "reason":"NVLink CRC errors + XID 74"}'
   ```

3. **Cordon the node** so the scheduler stops placing GPU-3-hungry pods there:
   ```bash
   kubectl taint node hgx-h100-node-12 gpu-3-degraded=true:NoSchedule
   ```
   The other 7 GPUs on the node remain usable for non-tensor-parallel work.

4. **File the hardware ticket** with the data captured:
   - Time series (from Grafana, exported)
   - Nsight trace
   - DCGM `dcgmi diag -r 3` output
   - XID and NVLink counter snapshots

5. **Update the alerting rule** (Doc 10 §3) so future NVLink-CRC climb alerts page faster — this took 18 minutes for the user to notice. The alert that *should* have fired:
   ```yaml
   - alert: NVLinkCRCErrorsClimbing
     expr: |
       rate(DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL[5m]) > 0.01
     for: 2m
     annotations:
       summary: "NVLink CRC errors climbing on {{ $labels.UUID }}"
       runbook_url: "/docs/appendix-c-flowcharts#c1-gpu-is-slow"
   ```

---

## 10. Postmortem — What We Learned (Doc 10 §6)

### 10.1 What worked

- Per-rank step-time variance metric (Doc 04 §2.3) caught the straggler in one panel.
- Cross-layer join (Doc 03) mapped rank → pod → GPU UUID in one query.
- L1 → L4 → L3 → L5 dashboard hierarchy (Doc 09) followed naturally as scope narrowed.
- Profiling lock pause/resume tooling (Doc 02 §4) made the Nsight capture safe.
- Lifecycle exporter (Doc 07 §6) made "pre-RMA" a first-class state.

### 10.2 What didn't

- **No alert fired before the user reported.** NVLink CRC error rate climbed for 3 minutes before throttle kicked in; we didn't have an alert on it. → Add alert, route to platform on-call.
- **Throttle bitfield decoding wasn't on the L3 dashboard.** On-call had to remember `0x40 = HW_THERMAL`. → Add a "throttle reasons" decoder panel that breaks the bitfield into named lines (Doc 09 update).
- **Step-time histograms were missing rank label until last sprint.** Old dashboards would have shown "step time degraded" but not "rank 47 specifically." → Audit other metrics for missing rank/per-instance labels.

### 10.3 Why this incident happened

GPU NVLink connectors in HGX boards develop intermittent contact issues over time, especially in high-vibration environments or after rack moves. CRC errors are the early signal; XID 74 events follow; if untreated, eventually XID 79 (GPU falls off bus) — a hard failure mid-job. **The whole point of pre-RMA is to catch this before XID 79.**

### 10.4 Cross-references — what each chapter contributed

| Step | Doc | What it gave us |
|------|-----|-----------------|
| Open L4 dashboard | Doc 09 §5 | Per-rank panel layout |
| Identify straggler | Doc 04 §2.3 | The "p99 by rank" pattern |
| Map rank → GPU | Doc 03 §6 | The `kube_pod_labels` join |
| L3 node grid | Doc 09 §4 | 8-GPU side-by-side panel |
| Decode throttle | Appendix B §B.4 | Bitfield meanings |
| L5 single-GPU forensics | Doc 09 §6 | Multi-metric overlay |
| NVLink as precursor | Doc 06 §4, Doc 07 §4 | Trend interpretation |
| XID lookup | Doc 07 §2 | XID 74 meaning |
| Profile capture | Doc 11 §4.3 | Alert-triggered Nsight |
| Profiling lock | Doc 02 §4 | Pause/resume safety |
| RMA decision | Doc 07 §6, Appendix C.5 | When to RMA vs pre-RMA |
| Lifecycle state | Doc 07 §6.2 | `pre_rma` API |
| Alert improvement | Doc 10 §3 | NVLink-CRC alert rule |

If a step references a doc you haven't read, **that's the chapter to read next.**

---

## 11. Mental Models in This Walkthrough

This is where the Doc 00 mental models showed up:

| Model (Doc 00) | Where it appeared |
|----------------|-------------------|
| **GPU is wide, not fast** (§2) | All 132 SMs depend on inter-SM data; one bad NVLink degrades all of them |
| **GPU util is a lie** (§3) | We needed `SM_ACTIVE` per-GPU to spot the slow GPU; `nvidia-smi util` would have shown 100% on all 8 |
| **HBM is the limiter** (§3) | Confirmed via DRAM_active vs SM_active that we weren't memory-bound — the gap was elsewhere |
| **Profiling lock** (§6) | Had to pause DCGM before nsys |
| **Cardinality tax** (§7) | The `rank` label that helped us debug only works because it's bounded (0–63) |

**If you've internalized these five, you can debug a GPU incident you've never seen before.** That's the whole point of the chapter.

---

## 12. Practice Variants

To exercise the same flow on different shapes of incident, try mentally walking through these:

1. **Inference TTFT spike.** vLLM service p99 TTFT jumps 3×. Where do you start? (Hint: Doc 14 §2 + Appendix C.3.)
2. **Prometheus ate all the memory.** Series count climbed 5× overnight. (Hint: Appendix C.4.)
3. **Whole node drops out.** All 8 GPUs on `hgx-h100-node-22` go silent simultaneously. (Hint: probably exporter pod, but could be host kernel panic.)
4. **Allocation efficiency tanks.** Cluster has 30% pending GPU pods, 50% of GPUs idle. (Hint: Doc 05 + bin-packing in Doc 12.)
5. **MIG slice noisy neighbor.** Tenant A's GPU instance is fine, tenant B's on the same physical GPU sees high latency. (Hint: with MIG, this *shouldn't* happen — Doc 13 §3 explains.)

The answer to each is in some combination of the chapters and appendices. If you're stuck, walk the symptom backward through the layer stack from Doc 01 §1.

---

## 13. Acceptance Checklist

You're ready to take a GPU incident on-call shift when:

- [ ] You can read every metric on the L1–L5 dashboards (Doc 09) and explain what it means.
- [ ] You can decode `DCGM_FI_DEV_CLOCK_THROTTLE_REASONS` from memory.
- [ ] You can map rank → pod → GPU UUID with one PromQL query.
- [ ] You know the difference between SBE accumulation, DBE, page retirement, and row remap.
- [ ] You can identify whether a workload is HBM-bound or compute-bound from a dashboard.
- [ ] You can pause/resume the profiling lock and explain why.
- [ ] You know the RMA decision rules (Appendix C.5) cold.
- [ ] You've followed Appendix C flowcharts at least once on a real or simulated incident.

---

## 14. Forward Pointers

- **Appendix A** — terms used in this walkthrough
- **Appendix B** — the throttle bitfield, NVLink fields, XID error counter
- **Appendix C** — the flowcharts followed at each branching decision
- **Doc 10** — the alert that should have fired earlier
- **Doc 15** — distributed-training-specific straggler diagnosis (this incident lives there too)

---

## Sources

- [NVIDIA — XID error reference](https://docs.nvidia.com/deploy/xid-errors/index.html)
- [NVIDIA — NVLink troubleshooting](https://docs.nvidia.com/deploy/hardware-troubleshooting/index.html)
- [Meta — How we debug distributed-training slowness (2024)](https://engineering.fb.com/2024/03/12/data-infrastructure/training-large-language-models-at-scale-meta/)
- [Uber — Michelangelo platform GPU SRE practices](https://www.uber.com/blog/michelangelo-machine-learning-platform/)
- Internal docs 00–15 of this series
