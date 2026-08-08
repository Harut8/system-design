# GPU Observability — Tasks

Practice for the 22 documents in this folder. Theory tells you what
`DCGM_FI_DEV_GPU_UTIL` means; only doing this tells you what it means *when it lies to you*.

Each task names the document it belongs to. Do the task, then re-skim the document — the
second read is a different document.

> **The one idea: a GPU utilization number is a claim about a definition, not about hardware.**
>
> ```
>   "the fleet is 90% utilized"     DCGM_FI_DEV_GPU_UTIL   — any kernel resident
>   "the fleet is 30% utilized"     DCGM_FI_PROF_SM_ACTIVE — any warp active per SM
>   "the fleet is  4% utilized"     DCGM_FI_PROF_SM_OCCUPANCY — warps ÷ max warps
>   "the fleet is 35% utilized"     power draw ÷ TDP
>
>   All four are correct. All four are measured on the same idle-ish fleet.
>   Whoever picks the metric picks the answer.
> ```
>
> Every task below is ultimately about being the person who can say which number to use, and
> why — that judgement is the expensive half of this field and the cheap half is a Helm chart.

---

## Setup — three environments

| Environment | Cost | Use for |
|---|---|---|
| **Simulated fleet** — `kind` + `kwok`, fake `nvidia.com/gpu` resources, synthetic exporter | free | Tasks about scale, cardinality, aggregation, dashboards, alerting |
| **One real GPU** — Lambda / RunPod / Vast.ai, hourly | ~$1–3/hr | Tasks about metric semantics, profiling, real failure modes |
| **Multi-GPU node** — 4–8 GPUs, a few hours total | ~$10–30/hr | NVLink, topology, collective ops, MIG |

Rent the real hardware in short focused sessions with the tasks written out in advance.
Budget roughly $150 across the whole track. Setup instructions for the simulated fleet are in
`../k8s-learn/gpu-platform-tasks.md` Level 0.

---

## 00 — Mental models

1. On a real GPU, run a workload deliberately shaped to be **memory-bandwidth bound** (a large
   elementwise op) and another that is **compute bound** (a big matmul). Record all four
   utilization proxies for each. Write down the four numbers side by side. This single table
   is the foundation of everything else here.
2. Construct the pathological case: a workload reading **>90% on `GPU_UTIL` and <10% on
   `SM_OCCUPANCY`**. A tiny kernel in a tight loop will do it. Keep the code — it is the most
   useful demo you own for explaining GPU metrics to anyone.
3. Write a one-paragraph answer to *"what is our GPU utilization?"* that is honest, is not
   evasive, and fits in a Slack message to a VP. Iterate until both properties hold.

## 01 — Architecture and stack

4. Draw the full path for one metric — NVML → DCGM host engine → `dcgm-exporter` → Prometheus →
   Grafana — and annotate each hop with its sampling interval and its failure mode.
5. Identify every place in that path where a value can be **stale but not obviously missing**.
   Staleness that renders as zero is the most dangerous class of observability bug, because it
   makes an idle fleet look busy or a busy fleet look idle, and nothing alerts.

## 02 — DCGM exporter deep dive

6. Run `dcgm-exporter` against a real GPU. Diff its default metric set against
   `appendix-b-field-ids.md`. Which profiling fields are absent, and why? (Profiling fields
   need elevated access and can conflict with an active profiler.)
7. Customize the field set via CSV config. Measure the exporter's own CPU cost at 1s, 10s and
   30s scrape intervals. Telemetry that costs 5% of the node it measures is a bad trade, and
   you should be able to state the number.
8. Deploy it as a DaemonSet and **join GPU metrics to pod identity**. Do it twice: once with
   the built-in Kubernetes mapping, once by consuming the kubelet pod-resources API yourself.
   Understanding the second is what lets you debug the first.
9. Break the join: start a pod, let it bind a GPU, kill the exporter, restart it. Are the
   pod labels still correct? What about after a node reboot?

## 03 — Kubernetes GPU cluster observability

10. On the simulated fleet, scale to 200 nodes × 8 GPUs and compute your Prometheus series
    count. Then actually run it and measure ingestion rate and memory. Most GPU observability
    designs die at exactly this step.
11. Build the four-level aggregation — GPU → node → namespace/team → fleet — as recording
    rules. Verify each level sums correctly, including when GPUs are unallocated or nodes are
    NotReady.

## 04 — Batch vs stateless workloads

12. Instrument a batch training job and an inference server on the same fleet. Their
    utilization signatures should look nothing alike. Write down which metrics matter for
    each, and why a single dashboard serves neither well.
13. Define a **goodput** metric for the inference workload — useful work, not raw throughput —
    and show a case where throughput rises while goodput falls.

## 05 — Allocation and utilization efficiency

14. Compute **stranded GPU-hours** across your fleet: allocated minus utilized, by team.
    Requires the allocation controller from `../k8s-learn/gpu-platform-tasks.md` project 2.
15. Establish **per-config baselines**. Sample real occupancy per workload shape and define
    what "healthy" is for each. Without this a utilization dashboard produces arguments
    rather than decisions.
16. Find the three largest stranding sources on your simulated fleet and write the remediation
    for each: rightsizing, reclamation, or scheduling change.

## 06 — Host-level GPU utilization

17. Correlate GPU utilization with host CPU, memory bandwidth and disk I/O. Construct a case
    where the **GPU is starved by the data loader** — a very common real cause of low
    utilization, and one that GPU metrics alone will never reveal.
18. Add PCIe and NVLink throughput to the picture. Show a workload that is bottlenecked on
    interconnect rather than compute.

## 07 — Hardware health and failure detection

19. Enumerate the XID errors worth paging on versus logging. Not all XIDs are equal, and
    paging on all of them trains the team to ignore the pager.
20. Simulate a **degraded, not dead** GPU: thermal throttling, an ECC error rate climbing,
    NVLink running at reduced width. Degraded hardware is far more expensive than failed
    hardware, because failed hardware gets replaced and degraded hardware silently halves a
    training run.
21. Write the automated response: cordon, drain, label the node, notify. Decide what happens
    to a 12-hour training job that is 11 hours in.

## 08 — Prometheus metrics design and cardinality

22. Before adding any label, compute the series it creates. Then add `pod`, `namespace`,
    `team`, `job_id`, `model_name` one at a time and measure actual growth. `job_id` and
    `model_name` are traps — find out why by hand rather than being told.
23. Design the retention and downsampling tiers: raw for hours, 5m for weeks, 1h for years.
    Capacity planning needs years, and raw data for years is unaffordable.
24. Cost the whole thing out in $/month at 1000 GPUs. Compare against the cost of the GPUs.
    Telemetry above ~1% of fleet cost needs a justification.

## 09 — Grafana dashboards

25. Build three dashboards for three audiences — researcher, platform lead, finance — from
    the same data. If one dashboard is trying to serve all three, it serves none.
26. Give every panel a written interpretation: what does "good" look like, and what action
    does "bad" imply? A panel without an action is decoration.

## 10 — Alerting strategy

27. Write alerts for: fleet-wide utilization collapse, a single node stranding capacity, XID
    error rate, thermal throttling, and exporter absence. **The last one matters most** — a
    dead exporter looks exactly like a healthy idle fleet.
28. Set thresholds using your §05 baselines, not round numbers. Then run a week against the
    simulated fleet and count false positives. Tune until on-call would trust it.
29. Define SLOs on the *telemetry pipeline itself*: freshness, completeness, gap detection.
    "Real ownership of completeness, latency SLOs and gap detection" is a direct quote from
    the Anthropic Capacity Engineering posting.

## 11 — Profiling integration

30. Profile a real workload with Nsight Systems and correlate what you see with the DCGM
    metrics for the same window. Where does the fleet-level view mislead you about what the
    kernel is doing?
31. Establish where continuous fleet metrics stop being useful and targeted profiling has to
    start. Knowing that boundary is what keeps a capacity team from chasing ghosts.

## 12 — Capacity planning and cost optimization

32. Build a $/GPU-hour model across two invented providers with **different billing
    structures** — one on-demand per-second, one with reservations and committed-use
    discounts. Normalizing dissimilar billing models is a named preferred qualification and is
    much harder than it looks.
33. Forecast 90 days of demand from allocation history. State your assumptions and put error
    bars on it.
34. Answer, with data: **is fleet growth causal or merely correlated with the business
    driver?** This is the question that separates a capacity engineer from a reporting layer.

## 13 — Multi-tenant GPU observability

35. Enforce per-team isolation of telemetry: each team sees its own data, platform sees all.
    Do it in the query layer, not by copying data.
36. Build a chargeback report: GPU-hours × rate, by team, by month, reconciled against a
    provider invoice. Reconciliation always finds a discrepancy on the first attempt — find
    yours and explain it.

## 14 — LLM inference observability

37. Serve a model on vLLM. Instrument **TTFT, ITL, throughput, goodput, queue depth, batch
    size, KV-cache hit rate** alongside the GPU metrics.
38. Sweep batch size and plot latency against throughput. Locate the knee. This curve is the
    core economic fact of inference serving, and being able to produce it on demand is a
    genuine differentiator.
39. Derive **$/1M tokens** for your deployment from GPU-hour cost and measured throughput.
    Compare against published API pricing and find the crossover point where self-hosting
    wins. Ties directly to `ai-rag/` P2–P3.

## 15 — Distributed training observability

40. Instrument a multi-GPU job. Measure the collective-communication fraction of step time.
41. Construct a **straggler**: one slow rank holding up an all-reduce. Show how it appears in
    per-GPU metrics — the signature is subtle and easy to misread as general low utilization.

## 16 — Incident walkthrough

42. Work the incident in the document, but stop at each decision point and commit to an answer
    before reading on.
43. Then write your own from an incident you construct on the simulated fleet: symptom,
    hypotheses, what you measured, what was wrong, what you changed. Build-logs in this format
    are the most credible public artifact in this whole folder.

## 17 — Telemetry lakehouse and SQL analytics

44. Export GPU telemetry to Parquet. Query with DuckDB. Prometheus is wrong for the questions
    capacity planning asks — "GPU-hours by team by month over two years" is an OLAP query, not
    a time-series one.
45. Build the star schema: fact table of GPU-seconds, dimensions for node, team, workload,
    accelerator type, provider.
46. Answer these three in SQL: *utilization by team by month, trending?* · *which workloads
    consumed the most stranded capacity last quarter?* · *what would rightsizing the worst ten
    have saved?* Those are the questions leadership actually asks.

---

## Suggested order

**Weeks 1–2 — semantics.** Tasks 1–3, 6–9. One real GPU session. Nothing else here is
trustworthy until the metric semantics are in your hands rather than in your notes.

**Weeks 3–4 — scale and cost.** Tasks 10–11, 22–24. Simulated fleet.

**Weeks 5–6 — the gap.** Tasks 14–16, 25–26. Needs the allocation controller from
`../k8s-learn/gpu-platform-tasks.md` project 2.

**Weeks 7–8 — reliability.** Tasks 19–21, 27–29.

**Weeks 9–10 — economics.** Tasks 32–34, 36, 44–46. The highest-value block for capacity work.

**Weeks 11–12 — inference.** Tasks 37–39. Bridges into `../ai-rag/`.

Tasks 17–18, 30–31, 40–43 as time allows; they deepen rather than unlock.

## What to publish

Task 2 (the `GPU_UTIL` vs `SM_OCCUPANCY` contradiction, with code), task 38 (the batch-size
knee), and task 43 (your own incident write-up). Those three are defensible, non-obvious, and
demonstrate judgement rather than tool familiarity.

Label the simulated work as simulated. "Benchmarked on a 200-node simulated fleet" is accurate
and costs you nothing; implying a real fleet costs you the whole interview.
