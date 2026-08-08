# GPU Platform Tasks — the capacity engineering capstone

Do this after `device-plugin-tasks.md`. It is the endgame of Track B: five projects that
together are a miniature of what a frontier-lab capacity or Kubernetes-platform team actually
owns — telemetry ingestion, allocation accounting, planning, queueing, and placement.

Read alongside: `../kubernetes/09-kube-scheduler-internals.md`,
`../kubernetes/10-kubelet-internals.md`, `../kubernetes/21-resource-management-and-qos.md`,
and all of `../gpu-observability/` (start with its `tasks.md`).

> **The one idea: allocation is not utilization, and the gap between them is the entire job.**
>
> ```
>   requested          allocated           utilized
>   (pod spec)   ──▶   (scheduler bound)  ──▶  (SM actually busy)
>       8 GPUs             8 GPUs                 0.9 GPUs
>
>   ▲ capacity planning        ▲ scheduling         ▲ observability
>     lives here                 lives here            lives here
>
>   Stranding = allocated − utilized.  At fleet scale it is the largest
>   single line item anyone can move, and nobody can see it by default.
> ```
>
> Every project below exists to measure, expose, or close some part of that gap.

---

## Level 0 — A fleet you can break, without buying GPUs

You do not need real accelerators for projects 1–4. You need nodes that *claim* to have them.

1. **Fake GPU nodes.** Extended resources are advertised through the node status subresource,
   so you can invent them:

   ```bash
   kind create cluster --name gpulab
   kubectl proxy &
   curl --header "Content-Type: application/json-patch+json" \
     --request PATCH \
     --data '[{"op":"add","path":"/status/capacity/nvidia.com~1gpu","value":"8"}]' \
     http://localhost:8001/api/v1/nodes/gpulab-worker/status
   ```

   `nvidia.com/gpu` is now schedulable. Pods requesting it will bind. Nothing runs on a GPU,
   which is fine — the scheduler, the accounting and the queueing are all real.

2. **A fleet, not a node.** Install `kwok` and stand up 50–200 fake nodes across several
   invented "regions", "providers" and accelerator types (`nvidia.com/gpu`,
   `nvidia.com/mig-1g.10gb`, `google.com/tpu`). Heterogeneity is the point — homogeneous
   fleets hide every interesting problem.

3. **Synthetic DCGM.** Write an exporter that emits *real* DCGM metric names with plausible
   fake values, per fake GPU. Field names and IDs are in
   `../gpu-observability/appendix-b-field-ids.md`. At minimum:

   | Metric | Meaning |
   |---|---|
   | `DCGM_FI_DEV_GPU_UTIL` | fraction of time ≥1 kernel was resident — **not** occupancy |
   | `DCGM_FI_PROF_SM_ACTIVE` | fraction of time ≥1 warp was active on an SM, averaged over SMs |
   | `DCGM_FI_PROF_SM_OCCUPANCY` | resident warps ÷ max warps |
   | `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | tensor-core pipe utilization |
   | `DCGM_FI_DEV_FB_USED` / `_FREE` | framebuffer memory |
   | `DCGM_FI_DEV_POWER_USAGE` | watts — the honest utilization proxy |
   | `DCGM_FI_DEV_XID_ERRORS` | hardware faults |

   Give your generator distinct workload *shapes*: a training job (high SM_ACTIVE, high power,
   steady), an inference server (bursty, low occupancy, high GPU_UTIL), an idle-but-allocated
   notebook (GPU_UTIL near zero, memory held). Those three shapes are what the rest of the
   projects have to tell apart.

4. **Real hardware, once.** Rent one GPU for a few hours (Lambda, RunPod, Vast.ai) at the end
   of project 1 and validate that your synthetic metrics match reality in name, unit and
   cardinality. A day of real hardware is worth a month of assumptions, and it's the
   difference between "I simulated this" and "I verified it against a real device."

---

## Project 1 — DCGM → Prometheus

**Build:** the telemetry substrate everything else reads from.

### Levels

1. Deploy `dcgm-exporter` as a DaemonSet (or your synthetic exporter on the fake fleet).
   Scrape it with Prometheus. Confirm the metric names against
   `../gpu-observability/appendix-b-field-ids.md`.
2. **Join GPU metrics to Kubernetes identity.** Raw DCGM tells you GPU 3 on node 7 is busy. It
   does not tell you *whose* job that is. Attach pod, namespace, and team labels — via the
   exporter's `kubernetes-mapping` or your own relabeling from the kubelet pod-resources API.
   **This join is the single most valuable thing in the project**; without it there is no
   attribution and therefore no capacity engineering.
3. Design the metric schema deliberately. Which labels are worth their cardinality? Compute
   the series count: `GPUs × metrics × label combinations`. Read
   `../gpu-observability/08-prometheus-metrics-design-and-cardinality.md` and
   `../sre-observability/18-cardinality-and-cost.md` *before* you pick labels, not after.
4. Recording rules for the aggregates you'll reuse: per-node, per-namespace, per-team,
   per-accelerator-type utilization.
5. Break it: kill the exporter mid-scrape, restart a node, delete a pod mid-job. Which metrics
   go stale versus disappear? Staleness that reads as zero utilization will silently corrupt
   every downstream number.

### Acceptance

- [ ] Any GPU-second is attributable to a pod, namespace and team
- [ ] You can state your total series count and defend each label
- [ ] A written note on what happens to each metric when the exporter dies

Read: `../gpu-observability/02-dcgm-exporter-deep-dive.md`, `03-k8s-gpu-cluster-observability.md`,
`08-prometheus-metrics-design-and-cardinality.md`.

---

## Project 2 — GPU Capacity & Utilization Dashboard

**Build:** the allocation-versus-utilization gap, made visible.

### Levels

1. Write a controller (client-go, per `controller-tasks.md`) that maintains **allocation**
   state: for every node, GPUs capacity / allocatable / requested / bound. This comes from the
   API server, not from DCGM — they are different sources of truth and conflating them is the
   classic error.
2. Join allocation against utilization from project 1. Produce, per namespace and per team:
   **allocated GPU-hours**, **utilized GPU-hours**, and **stranded GPU-hours** (the
   difference).
3. Define *utilized* precisely and defend it. Is a GPU at `GPU_UTIL=100%, SM_OCCUPANCY=4%`
   utilized? This is the DCGM semantics trap — the same fleet reads as 90% or 5% utilized
   depending on which metric you trust. Pick a definition, write down why, and expose both.
4. Grafana dashboards for three audiences, because the same data must answer three questions:

   | Audience | Question | Panel |
   |---|---|---|
   | Researcher | "Is my job using the GPU I asked for?" | Per-job occupancy over time |
   | Platform lead | "Where is capacity stranded?" | Stranding heatmap by team × accelerator |
   | Finance | "What did that cost?" | GPU-hours × rate, by team, by month |

5. Add a per-config **baseline**: what does "good" occupancy look like for *this* workload
   shape? A 40% number is excellent for one shape and terrible for another, and a dashboard
   without baselines just generates arguments.

### Acceptance

- [ ] Stranded GPU-hours per team, computed and defensible
- [ ] Both utilization definitions exposed, with a written rationale for the headline one
- [ ] Three dashboards, each answering exactly one question

Read: `../gpu-observability/05-gpu-allocation-and-utilization-efficiency.md`,
`06-host-level-gpu-utilization.md`, `09-grafana-dashboards.md`, `13-multi-tenant-gpu-observability.md`.

---

## Project 3 — Kubernetes GPU Capacity Planner

**Build:** a CRD-driven planning and allocation surface. This is the closest project to the
Anthropic Capacity Engineering job description.

### Levels

1. Design the API before writing code. Sketch a `GPUAllocation` (or `CapacityBudget`) CRD:

   ```yaml
   spec:
     team: research-training
     accelerator: nvidia.com/gpu
     guaranteed: 64        # reserved, always available
     burst: 128            # may borrow from the shared pool
     region: us-central1
   status:
     allocated: 64
     utilized: 41
     stranded: 23
     borrowedFrom: [inference-pool]
   ```

   Spend real time on this. `operator-tasks.md` covers the mechanics; the hard part is the
   schema, and schema contracts are named explicitly in the job description.
2. Build the operator with `controller-runtime`. Reconcile: read allocations, read live
   utilization, compute status, expose it.
3. **Enforcement.** A budget nobody enforces is a spreadsheet. Add a validating admission
   webhook (see `../kubernetes/06-admission-control-deep-dive.md`) that rejects pods exceeding
   a team's guaranteed+burst. Then decide what happens to a team that is *under*-utilizing —
   reclaim, warn, or nothing — and defend the choice.
4. **Rightsizing recommendations.** For each workload, compare requested GPUs against p50/p95
   observed occupancy and emit a recommendation. Handle the obvious objection: a job that
   needs 8 GPUs for ten minutes of a two-hour run still needs 8 GPUs.
5. **Cost attribution.** Attach a rate per accelerator type per region. Produce team-level
   spend. Then make it multi-provider: two invented providers with *different billing models*
   (one on-demand per-second, one with committed-use discounts and reservations). Normalizing
   these is explicitly a preferred qualification, and it is much harder than it sounds.
6. **Forecasting.** Given 90 days of allocation history plus a growth signal, project demand
   and produce a supply plan. Then answer the real question: is the growth *causal* or merely
   correlated with a business driver?

### Acceptance

- [ ] A CRD you would defend in a design review, with documented schema contracts
- [ ] Enforcement that actually blocks something
- [ ] Team-level cost, normalized across two dissimilar billing models
- [ ] A 90-day forecast with stated assumptions and error bars

Read: `../gpu-observability/12-capacity-planning-and-cost-optimization.md`,
`17-telemetry-lakehouse-and-sql-analytics.md`, `../sre-observability/31-finops-for-observability.md`.

---

## Project 4 — GPU Job Queue

**Build:** admission and queueing for scarce accelerators. Default Kubernetes has no queue —
unschedulable pods simply sit Pending, which is not a policy.

### Levels

1. Reproduce the failure first. Submit 20 jobs each requesting 8 GPUs to a 16-GPU fleet.
   Observe: no ordering, no fairness, no visibility into position. Write down what's wrong.
2. **Gang scheduling.** A distributed training job needs all 8 pods or none — 7 running pods
   holding GPUs while waiting for the 8th is deadlock, and two such jobs deadlock each other
   permanently. Implement all-or-nothing admission using a `Permit` plugin with waiting pods,
   or a job-level workload abstraction that only admits when the whole gang fits.
3. **Queue policy.** Priority classes, per-team fairness, ageing so low-priority work isn't
   starved forever, and backfill so a small job can use a hole while a large one waits. Each
   is a distinct mechanism; implement them separately.
4. **Preemption and borrowing.** A team may borrow idle GPUs from the shared pool, but must
   surrender them when the owner returns. Decide the eviction contract: grace period,
   checkpoint signal, or hard kill — and what that means for a 12-hour training run.
5. Compare against **Kueue**: `ClusterQueue`, `LocalQueue`, `ResourceFlavor`, `Workload`,
   cohorts and borrowing. Read its source. Then write up where your design differs and why.
   Being able to critique Kueue's model is a strong interview signal; Kueue is named in the
   Anthropic Kubernetes Platform posting.

### Acceptance

- [ ] Gang admission with a demonstrated deadlock-free property
- [ ] Queue position and estimated wait, visible to the submitter
- [ ] Preemption that respects a documented eviction contract
- [ ] A written comparison against Kueue

Read: `../kubernetes/09-kube-scheduler-internals.md` (queueing and preemption),
`../gpu-observability/04-batch-vs-stateless-workloads.md`.

---

## Project 5 — Build Your Own Kubernetes GPU Scheduler

**Build:** a scheduler-framework plugin that places GPU work better than the default. The
hardest project here, and the one that maps to the $405–485k Kubernetes Platform role.

### Levels

1. Learn the extension points cold — you will use six of them:

   | Point | Your use |
   |---|---|
   | `QueueSort` | Ordering by priority and age |
   | `PreFilter` | Compute the gang's total demand once |
   | `Filter` | Reject nodes lacking the right accelerator or topology |
   | `Score` | Bin-packing, topology affinity, fragmentation avoidance |
   | `Reserve` / `Permit` | Gang admission and rollback |
   | `Bind` | Only if you need custom binding |

2. Run it as a **second scheduler** alongside the default (`schedulerName` in the pod spec).
   Never replace the default while learning — you will lock yourself out of your own cluster.
3. **Bin-packing versus spreading.** The default spreads; GPUs usually want packing, so that
   whole nodes stay free for the next 8-GPU job. Implement a `Score` plugin that packs, then
   measure fragmentation: what fraction of free GPUs are unusable because they're scattered
   one-per-node? That number is the entire justification for the plugin.
4. **Topology awareness.** Model NVLink domains and PCIe topology on your fake nodes. Two GPUs
   on the same NVLink island are worth far more to a training job than two across a PCIe root
   complex. Score for it. This is what "topology-aware placement" and "collective networking
   such as NCCL" mean in the posting.
5. **Utilization-aware placement.** Feed project 1's live occupancy into `Score` — prefer nodes
   whose resident jobs are under-utilizing. Then confront the feedback loop: scheduling
   decisions change utilization, which changes future scheduling. Damp it, or watch it
   oscillate.
6. **Prove it.** Replay a realistic job trace through both the default scheduler and yours.
   Report: GPU-hours stranded, fragmentation, mean queue wait, p95 time-to-start, gang
   deadlocks. **A scheduler without a benchmark is an opinion.**

### Acceptance

- [ ] Plugin running as a second scheduler, handling real pods
- [ ] Measured improvement over default on a replayed trace, with the trace published
- [ ] Topology-aware scoring with a written model of the hardware
- [ ] An honest list of the cases where yours is *worse* than the default

Read: `../kubernetes/09-kube-scheduler-internals.md` in full,
`../kubernetes/34-custom-schedulers-and-scheduler-framework.md`,
`../kubernetes/10-kubelet-internals.md` (device manager).

---

## Sequence and effort

| # | Project | Effort | Depends on |
|---|---|---|---|
| 0 | Fake fleet + synthetic DCGM | 1 week | — |
| 1 | DCGM → Prometheus | 1–2 weeks | 0 |
| 2 | Capacity & utilization dashboard | 2 weeks | 1, `controller-tasks.md` |
| 3 | Capacity planner | 3–4 weeks | 2, `operator-tasks.md`, `admission-tasks.md` |
| 4 | GPU job queue | 3–4 weeks | 3 |
| 5 | Custom GPU scheduler | 4–6 weeks | 4, `scheduling-framework-tasks.md` |

**~4 months at 8–10h/week.** If you only do two, do **1 and 3** — telemetry plus attribution
is the Capacity Engineering role, and it's reachable without scheduler-framework depth.
Projects 4 and 5 are the Kubernetes Platform role.

## Publishing

Each project is a build-log: what you built, what the numbers did, what surprised you, where
your design is worse than the incumbent. The last item is what makes the rest believable.

Project 5's benchmark is the single most credible artifact on this page — a measured comparison
against the default scheduler on a published trace is evidence almost nobody outside a large
infrastructure team can produce.

Label honestly: these run on a simulated fleet. "I built a GPU scheduler plugin and benchmarked
it against the default on a 200-node simulated fleet" is accurate, impressive, and cannot be
punctured. "I built GPU scheduling infrastructure" cannot survive a follow-up question.
