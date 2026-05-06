# 20 — AIOps and the Frontier

> The 2026 frontier of observability. Anomaly detection, alert grouping, LLM-assisted incident response, automated postmortem authoring, agentic on-call assistance. The hype is enormous; the production utility is real but narrower than vendors claim. This chapter is the staff-engineer view: what works, what doesn't, what to deploy now, what to defer.

This chapter assumes everything before it. AIOps is *additive* — it amplifies an already-healthy stack but cannot rescue a broken one. Deploy AIOps before the foundation is solid and you'll automate the wrong things faster.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [The AIOps taxonomy](#2-taxonomy)
3. [Statistical anomaly detection (the boring, valuable part)](#3-statistical)
4. [ML-based anomaly detection: where it works, where it doesn't](#4-ml)
5. [Alert grouping and noise reduction](#5-alert-grouping)
6. [Forecasting: capacity and SLO budget](#6-forecasting)
7. [LLM-assisted incident response](#7-llm-ir)
8. [LLM-generated runbooks and postmortems](#8-llm-runbooks)
9. [Agentic on-call: claims vs reality](#9-agentic)
10. [Causal inference and root-cause analysis](#10-causal)
11. [Toolchain landscape (2026)](#11-tools)
12. [The "AIOps adoption order"](#12-adoption-order)
13. [Evaluation: how to know it's working](#13-evaluation)
14. [Risks and failure modes](#14-risks)
15. [Anti-patterns](#15-anti-patterns)
16. [Worked example: anomaly detection rollout at scale](#16-worked-example)
17. [Pitfalls](#17-pitfalls)
18. [Mental models](#18-mental-models)

---

## 1. Thesis

Three claims that distinguish reality from vendor pitch:

1. **AIOps amplifies; it does not replace.** A well-tuned multi-window multi-burn-rate alert beats almost every ML anomaly detector in precision and recall. Use AIOps for problems static rules can't express — high-cardinality anomaly hunting, alert grouping at scale, runbook generation, postmortem assistance.
2. **Statistical methods (boring) outperform ML (sexy) for most use cases.** Holt-Winters, MAD, exponential smoothing, seasonal decomposition. These are 50-year-old techniques that work, are explainable, and don't drift. Reach for ML only when statistics fail.
3. **LLM assistance is the highest-leverage AIOps frontier in 2026.** Not detection — synthesis. LLMs that draft postmortems from incident timelines, summarize alerts into one English sentence, suggest runbook steps from logs. These are *reading* tasks, where LLMs are strongest.

If your team is buying an AIOps platform to "reduce alert volume," ask first whether they've done alert hygiene (`doc 12 §9`). 80% of the time, the vendor's value is replicable with one quarterly cleanup PR.

---

## 2. The AIOps Taxonomy

Six categories. Each has different maturity, ROI, and risk.

| Category | Description | Maturity | ROI |
|---|---|---|---|
| **Anomaly detection** | "this metric looks weird" alarms | Mature (statistical), emerging (ML) | Medium |
| **Alert grouping** | Cluster related alerts into incidents | Mature | High |
| **Forecasting** | Predict future capacity / SLO burn | Mature | High |
| **Causal inference** | "X caused Y" reasoning | Emerging | Medium |
| **LLM-assisted IR** | English summaries, suggestions | Emerging-to-mature | High |
| **LLM-generated artifacts** | Postmortems, runbooks, dashboards | Emerging | Medium-high |

The "high ROI" categories are alert grouping, forecasting, and LLM assistance. Start there.

---

## 3. Statistical Anomaly Detection

The boring, durable techniques. **Almost every "we need ML" problem is solved by these.**

### 3.1 The methods

| Method | What it does | When |
|---|---|---|
| **Threshold** | Alert if `value > X` | Static, predictable bounds |
| **Z-score** | Alert if `value > μ + 3σ` over window | Roughly Gaussian distributions |
| **MAD** (Median Absolute Deviation) | Like Z-score but median-based | Outlier-robust; good for skewed distributions |
| **Holt-Winters** | Forecast value; alert on residual | Seasonal series (daily, weekly cycles) |
| **STL decomposition** | Decompose into trend + season + noise; alert on noise | Strong seasonality |
| **EWMA** (Exponentially Weighted Moving Average) | Weighted recent values; alert on deviation | Slowly-shifting baselines |

### 3.2 What they catch

A typical service:
- Diurnal traffic pattern (Holt-Winters models it).
- Weekly cycle (Holt-Winters or STL captures it).
- Slow drift in error rate (EWMA captures it).
- Outlier spikes (MAD catches them).

These methods have been in production for decades. They are explainable (you can show the math to a PM), they don't drift in surprising ways (no model retraining), and they're cheap to compute.

### 3.3 The PromQL recipes

```promql
# Z-score over 1h window
abs(metric - avg_over_time(metric[1h])) / stddev_over_time(metric[1h]) > 3

# Compare to last week (anomaly if today differs significantly from a week ago)
abs(rate(metric[5m]) - rate(metric[5m] offset 1w)) > threshold
```

Most "smart" anomaly alerting can be expressed in PromQL. For more:

```promql
# Holt-Winters approximation: Prometheus' holt_winters() function
holt_winters(rate(http_requests_total[5m])[1h:5m], 0.5, 0.5)
```

### 3.4 Where statistics fail

Three cases where you genuinely need more:

1. **High dimensionality.** "Something is anomalous somewhere in 10K metrics." A separate threshold per metric isn't tractable. ML clustering helps.
2. **Multivariate anomalies.** "CPU + queue + latency together" — none individually anomalous, but the *combination* is. Multivariate detection.
3. **Slow regime shifts.** A service quietly degrades over weeks. Static thresholds drift; smarter models track the baseline.

These are the legitimate ML-anomaly use cases.

---

## 4. ML-Based Anomaly Detection

Where it works, where it disappoints.

### 4.1 The methods

| Method | Pros | Cons |
|---|---|---|
| **Isolation Forest** | Fast; works on high-dim | Doesn't capture time |
| **LSTM autoencoders** | Captures temporal patterns | Training cost, drift, opaque |
| **Prophet** (Meta) | Easy seasonal forecasting | Slow on many series |
| **Vendor-internal models** | Black-box but tuned | Vendor lock-in; can't debug |

### 4.2 The tuning trap

ML models need:
- Training data (clean baseline).
- Hyperparameter tuning.
- Drift detection.
- Retraining pipeline.
- Per-service tuning (one model rarely covers all services).

For 200 services, the ops cost of ML detection often *exceeds* the value. Vendors hide this; their product runs the model fleet for you.

### 4.3 The precision-recall reality

In published evaluations of ML anomaly detection on Prometheus data:
- Precision: 30–70% (lots of false positives).
- Recall: 60–90%.

Versus burn-rate alerts:
- Precision: 80–95%.
- Recall: 90–99%.

ML detection is usually *worse* than a well-tuned burn-rate alert for the same problem. Use ML for problems where burn-rate doesn't apply (the high-dim / multivariate cases).

### 4.4 When ML is worth it

- **High-cardinality anomalies.** Detecting which of 100K customer cohorts has degraded.
- **Cross-signal anomalies.** Latency + queue + CPU as a vector.
- **Pattern recognition** in logs (semantic clustering of error messages).

For these, the ML investment pays off. For "alert me when latency is weird" — burn-rate is better.

---

## 5. Alert Grouping and Noise Reduction

The most under-appreciated AIOps category.

### 5.1 The problem

A single underlying issue (an upstream service degrades) produces 30 alerts (every consumer of that service). The on-call doesn't need 30 pages — they need *one* with rich context.

### 5.2 The solutions

#### Rule-based grouping

Already built into Alertmanager (`doc 12 §7.2`). `group_by` collapses related alerts into one notification. Inhibition rules suppress downstream alerts when an upstream is firing.

This is non-ML and works. Most of the value is here. Configure inhibition rules; you're 80% done.

#### ML-based correlation

For very large stacks (1000+ alerts/day):
- Cluster alerts by topology (same service, same dependency, same time window).
- Identify "alert storms" automatically.
- Group into a single incident.

Tools: BigPanda, PagerDuty's alert grouping, Datadog's incident management. They use a mix of rules and ML (graph algorithms over service topology).

The 2026 state: rule-based for most teams; ML-based for the 1% with very high alert volume.

### 5.3 Noise filtering

Beyond grouping: detect alerts that are likely false positives:
- Same alert fires N times in a quarter without action → likely noise.
- Alert fires only on deploys → tune.
- Alert fires only at midnight (cron job blip) → silence on schedule.

These can be automated detections; some platforms surface them. The action — deletion, retuning — remains manual.

---

## 6. Forecasting

The "predict the future" category.

### 6.1 SLO budget forecasting

Given current burn rate trend, predict when the budget will exhaust.

```
Current 28-day burn: 1.2× normal
Days remaining at this rate: 19 days
Trend: increasing 5% per day

Forecast: budget exhausted in ~9 days if trend continues.
```

This is more useful than a current-state burn rate — it gives the team time to act.

### 6.2 Capacity forecasting

`doc 16` covered the model. AIOps applies it:
- Per-service projection of resource use.
- Anomaly detection on the projection (is growth accelerating beyond baseline?).
- Auto-procurement triggers in some platforms.

### 6.3 Latency forecasting

"P99 latency is trending up; will it cross SLO threshold?" Forecast on histogram quantiles; alert if forecast crosses. The "predict the alert before it fires" pattern.

### 6.4 The "leading indicator" pattern

The general AIOps pattern: turn a *future* prediction into a *present* signal. Predict a future SLO breach → alert today. Predict a future capacity wall → procure today. Predict a future incident → page now.

---

## 7. LLM-Assisted Incident Response

The 2026 high-leverage category.

### 7.1 What LLMs do well in IR

- **Summarize alerts.** "These 12 alerts together suggest a Kafka consumer lag in the orders pipeline."
- **Surface relevant context.** Pull recent deploys, related runbooks, similar incidents.
- **Draft status-page updates.** "Customers are experiencing slow checkout; team is investigating."
- **Suggest first actions.** "Last similar incident was resolved by rolling back service-X. Recent deploy of service-X happened 12 minutes ago. Consider rollback."
- **Translate technical to executive.** Take a technical timeline and produce a 3-bullet exec summary.

### 7.2 The architecture

```
Alert / incident channel → LLM API → tools (queries, runbooks, deploys) → response

Context window:
  - Current alerts (formatted)
  - Recent dashboards (snapshots / queries)
  - Runbooks (text)
  - Recent deploys (markdown)
  - Similar past incidents (vectorized + retrieved)
  - Service catalog (yaml)
```

The LLM is given tools to query observability data, retrieve runbooks, search past incidents. It assembles a synthesis.

### 7.3 What LLMs do badly in IR

- **Authoritative diagnosis.** LLMs hallucinate causes that sound plausible. Treat suggestions as hypotheses, not conclusions.
- **Novel failure modes.** If the failure isn't represented in past data, the LLM has nothing to suggest.
- **Real-time decision-making.** Latency of LLM responses (seconds-to-minutes) doesn't fit a hot incident.
- **Critical reasoning under pressure.** The LLM is unflustered (good) but lacks situational judgment (bad).

The pattern that works: LLM as *fast-typing junior* — drafts summaries and suggestions; the human IC validates and acts.

### 7.4 The Pagerduty / incident.io / Coralogix integrations

In 2026, every major incident-management platform has LLM features:
- Alert summarization.
- Past-incident retrieval.
- Status-page draft generation.
- Postmortem assistance.

Quality varies; pilot before committing. The good ones are useful productivity tools; the bad ones add noise.

---

## 8. LLM-Generated Runbooks and Postmortems

Synthesis tasks LLMs are well-suited for.

### 8.1 Postmortem drafting

Given:
- The incident timeline (Slack channel transcript).
- The metrics dashboards during the incident.
- Recent deploys and config changes.
- Past similar incidents.

The LLM drafts a postmortem (template from `doc 15 §12`):
- Summary.
- Impact.
- Timeline (extracted and chronological).
- Suggested contributing factors.
- Suggested action items.

The human IC reviews, corrects, expands. A 4-hour postmortem becomes 1 hour.

### 8.2 Runbook drafting

For new services or new alerts: the LLM drafts a runbook from:
- The alert expression.
- The service architecture.
- Past incident patterns.
- Org's runbook template.

The human reviews. Catches issues that would be caught only after the first real incident otherwise.

### 8.3 The validation problem

LLM-generated artifacts can be subtly wrong. Checks:
- Have a human review every artifact.
- Run runbooks through a validation step (e.g., "does each command exist?").
- Test in non-production first.

The pattern: LLM as drafter, human as editor. Same as junior-engineer-with-senior-review.

### 8.4 The training data trap

If the LLM is trained on *your* historical incidents, it learns your biases too. If postmortems historically blamed individuals, the LLM might draft blamefully. If they were terse, the LLM stays terse. Curate training data deliberately.

---

## 9. Agentic On-Call: Claims vs Reality

The vendor pitch: "AI agents that handle pages autonomously." The 2026 reality: severely limited.

### 9.1 What works

- **Acknowledging pages and updating status.** Trivial; safe; useful.
- **Running pre-approved playbooks.** "If alert X fires, run command Y." This is *automation*, not AI; it's been done for years.
- **Suggesting next actions to a human.** As LLM-IR (§7).
- **Writing summary emails.** Synthesis.

### 9.2 What doesn't work

- **Autonomous mitigation of novel issues.** The agent doesn't know what's safe to do.
- **Decision-making under ambiguity.** Should we roll back? Activate kill switch? Page another team? The agent can't reliably judge.
- **Handling cascading or evolving incidents.** Conditions change; the agent's mental model lags.

### 9.3 The "supervised agent" pattern

The approach that's emerging:

1. Agent receives alerts + context.
2. Agent proposes actions.
3. Human approves with one-click.
4. Agent executes; reports.

Net: human stays in the loop on every action; agent does the *typing* and *querying*. 30-50% time savings on routine pages; full responsibility kept.

### 9.4 The "fully autonomous" trap

For highly-routine actions (e.g., restarting a single pod that's stuck), full autonomy is reasonable. For anything with broader blast radius — code rollbacks, traffic shifts, tenant-affecting changes — keep humans in the loop. The cost of the agent making one bad call is much higher than its accumulated savings.

---

## 10. Causal Inference and Root-Cause Analysis

The "explain why" frontier.

### 10.1 The promise

Given an incident, automatically:
- Identify which service is the actual cause vs symptoms.
- Trace the dependency chain.
- Cite supporting evidence.

### 10.2 The methods

- **Service graph + temporal correlation.** Which service degraded first? That's the root.
- **Causal graphs.** Bayesian networks over service dependencies; query for likely cause given symptoms.
- **LLM over telemetry.** Prompt with timeline + topology; ask for hypothesis.

### 10.3 The reality check

Causal inference is hard in distributed systems. Multiple contributing factors (`doc 15 §14`); correlation isn't causation; the "first-degraded" service may itself be a symptom of an upstream issue not in the graph.

Tools claim "automated RCA" — pilot carefully. Most produce hypotheses, not conclusions. Treat as such.

### 10.4 What works in 2026

- **Service-graph-based "this dependency degraded first" hints.** Correct often enough to be useful.
- **LLM hypothesis generation.** Useful as one input to a human's reasoning.
- **Topology-aware alert grouping** (mentioned in §5). Strong precursor to RCA.

What doesn't work: "the system tells us the root cause." Don't pay vendor premiums for this claim; verify their evidence.

---

## 11. Toolchain Landscape (2026)

The AIOps tool space is fragmented. Categories:

| Category | Examples |
|---|---|
| **APM with anomaly detection** | Datadog Watchdog, New Relic AI, Dynatrace Davis |
| **Alert correlation / grouping** | BigPanda, Moogsoft, PagerDuty Event Intelligence |
| **Incident response with LLM** | incident.io AI, FireHydrant Signal, PagerDuty AIOps |
| **Forecasting** | Anodot, Vantage, custom Prometheus + Prophet |
| **LLM postmortem assistance** | Jeli (PagerDuty), Howie, custom (OpenAI/Anthropic-based) |
| **Causal RCA** | Causely, Lumigo, Rookout |
| **Open-source AIOps frameworks** | Robusta, Komodor, Sentry alerts |

### 11.1 Build vs buy

For most orgs, *buy* the AIOps layer. The ML / LLM ops complexity is high; vendors have invested years.

For very large orgs (1000+ engineers, custom data shapes), *build* selectively — typically:
- Custom Prophet / Holt-Winters pipelines on internal data.
- Custom RAG-based postmortem helpers (using internal LLM API + vector DB of past incidents).

The hybrid: buy the platform, build the integrations.

### 11.2 The vendor evaluation rubric

When evaluating an AIOps vendor:

1. **Show me the math.** What model, what data, what tuning? Black-box "magic" is a red flag.
2. **Precision and recall on our data.** Pilot with 30 days of historical incidents.
3. **False-positive rate.** How many alerts will it generate per day? If > 10× current, it's noise.
4. **Drift handling.** What happens when our service mix changes?
5. **Explainability.** When it triggers, can it explain why?
6. **Integration.** Does it work with our pipeline (OTel, Prom, Mimir, etc.)?
7. **Cost over 3 years.** Per-host pricing scales surprisingly fast.

---

## 12. The AIOps Adoption Order

A staged plan, prioritized by ROI.

```
Phase 1: foundation (no AIOps)
  - SLOs in place
  - Multi-window multi-burn-rate alerts
  - Alert hygiene (4-question audit)
  - Inhibition rules for cascade suppression

Phase 2: cheap statistical (no ML)
  - Holt-Winters / EWMA / MAD on anomaly-prone metrics
  - Forecasting on capacity and SLO budget
  - Dashboard sparklines with anomaly bands

Phase 3: alert grouping
  - Topology-aware grouping
  - ML-based correlation if alert volume > 100/day after Phase 1+2

Phase 4: LLM assistance
  - LLM summarization of alerts
  - LLM-assisted postmortem drafts
  - LLM-suggested first actions during IR

Phase 5: ML anomaly detection (selective)
  - High-cardinality use cases
  - Multivariate / cross-signal cases
  - Pattern recognition in log clusters

Phase 6: agentic / causal (experimental)
  - Supervised agents for routine actions
  - Causal RCA hints
  - Avoid full autonomy until proven safe
```

**The order matters.** Skipping to Phase 4-6 without Phase 1-3 is the typical failure mode — buying ML to fix alert noise that's actually a hygiene problem.

---

## 13. Evaluation: How to Know It's Working

AIOps without measurement is theater.

### 13.1 The metrics

| Metric | What good looks like |
|---|---|
| **False positive rate** | < 20% (anomaly alerts) |
| **Detection lead time vs static** | AIOps detects N seconds earlier than static rule |
| **Alerts grouped per incident** | After grouping: ≥ 1 alert per incident, not 30 |
| **MTTM with vs without LLM assist** | Reduction in MTTM after LLM assist enabled |
| **Postmortem authoring time** | Reduction with LLM assist |
| **Engineer satisfaction** | Survey: "is the AI helpful?" |

### 13.2 A/B-style evaluation

Where possible, run AIOps signals in *shadow mode* for 30-90 days:
- AIOps alerts go to a separate channel (no paging).
- Compare to ground-truth incidents.
- Tune until precision and recall match the static-rule baseline before activating paging.

This is the only honest way to evaluate. Vendor demos are not.

### 13.3 The "is this paying off?" review

Quarterly review: did the AIOps investment pay back?
- Cost of the platform.
- Engineer time saved (in concrete activities).
- Incident-rate or MTTM change attributable to AIOps.

If the math doesn't pencil, decommission. AIOps is *expensive*; it must earn its keep like any other tool.

---

## 14. Risks and Failure Modes

What can go wrong.

### 14.1 Alert generation explosion

Symptom: AIOps adds 100 new alerts; on-call drowns.
Fix: shadow mode; tune thresholds; integrate with the four-question audit.

### 14.2 Hallucinated diagnoses

Symptom: LLM suggests "root cause: Kafka consumer lag" — confidently — when actual cause was DB. On-call wastes 20 minutes.
Fix: train on-call to treat suggestions as hypotheses; never act on LLM diagnosis without verification.

### 14.3 Drift

Symptom: ML model trained 6 months ago no longer matches current traffic; precision drops silently.
Fix: drift detection; periodic retraining; alerts on model precision regression.

### 14.4 Vendor lock-in

Symptom: 3 years of incident data and trained models are vendor-proprietary. Switching costs 6 months.
Fix: own your training data; use vendors with open data export.

### 14.5 Paging on patterns nobody understands

Symptom: ML model pages on "an anomaly"; nobody can articulate what.
Fix: prefer explainable methods; don't deploy black-box pagers.

### 14.6 Automation cascade

Symptom: agent restarts a pod; that triggers a deploy; deploy fails; agent restarts again; loop.
Fix: rate limits on agent actions; circuit breakers; human-in-the-loop for any cascade.

### 14.7 LLM cost overrun

Symptom: every alert sends a 100K-token context to GPT-4; bill is $30k/month.
Fix: cost monitoring per-call; scope context aggressively; cheaper models where possible.

---

## 15. Anti-Patterns

1. **Buying AIOps to fix alert hygiene.** The cheaper fix is the four-question audit.
2. **Deploying ML detection in parallel with static rules.** Two alert paths; alerts double.
3. **No shadow-mode evaluation.** Paging on AIOps without precision/recall data.
4. **Black-box detectors.** Can't explain to on-call why it fired.
5. **No drift handling.** ML model degrades silently.
6. **Full autonomy on safety-critical actions.** Agent rolls back the wrong service.
7. **Vendor lock-in via proprietary models.** Can't migrate.
8. **LLM treated as authoritative.** Hallucinated diagnoses acted on.
9. **No cost monitoring on LLM calls.** Bill explodes.
10. **Skipping phases 1-3.** Phase 4-6 deployed before foundation.
11. **Quarterly review skipped.** AIOps cost grows; benefit unclear.
12. **Bot grouping replaces inhibition rules.** Both implemented; conflict.
13. **Agent training on biased data.** Old patterns reinforce.
14. **No on-call training on AIOps tools.** Engineers ignore suggestions; tools unused.
15. **Hyped vendor deals.** Buying the futuristic feature; delivering the ordinary.

---

## 16. Worked Example: Anomaly Detection Rollout at Scale

A real-shape rollout.

### 16.1 Starting state

- 200 services
- Burn-rate alerts in place; quarterly hygiene cycle running
- 50 paging alerts/week aggregate; 80% actionable
- 3 false-page incidents in the last quarter

### 16.2 Hypothesis

"ML anomaly detection will catch issues burn-rate misses (e.g., latency anomalies that don't burn the SLO, or rare error types)."

### 16.3 Pilot

- Selected 5 high-traffic services.
- Deployed Datadog Watchdog (or equivalent) in shadow mode.
- 60-day observation.

### 16.4 Findings

- 142 anomaly alerts triggered.
- Compared with manual ground truth (incidents in tracker):
  - 18 incidents in the period.
  - Anomaly detector caught 12 (recall: 67%).
  - False positives: 124 of 130 non-incident triggers (precision: 4%).
  - 4 incidents caught by burn-rate alerts that anomaly missed.
  - 3 incidents caught by anomaly that burn-rate missed (detection lead time: 10 minutes).

### 16.5 Decision

- Anomaly detection has poor precision but earlier detection on some classes.
- Decision: keep anomaly detection in shadow mode (informational dashboard); do not promote to paging.
- Use the 3 missed-by-burn-rate examples to design new SLI / burn-rate rules covering those cases.

### 16.6 Outcome

- Burn-rate rules updated; 2 of the 3 miss-classes now covered.
- Anomaly detection retained as a monitoring aid, not a paging system.
- Cost of vendor: $45k/year; benefit: 2 better burn-rate rules. Math doesn't pencil; canceled at renewal.

This is the typical real-world AIOps experience. Net positive learning, but the headline product wasn't the value.

---

## 17. Pitfalls

1. **AIOps before foundation.** Phase-skipping causes regression.
2. **No shadow mode.** Promoting unproven detectors to paging.
3. **Black-box trust.** Acting on suggestions without verification.
4. **No drift handling.** Models silently degrade.
5. **Vendor lock-in.** Years of training data trapped.
6. **No A/B evaluation.** Can't tell if AIOps helps.
7. **Full agent autonomy.** Cascade failures.
8. **LLM cost surprise.** Inflated bills.
9. **Hallucinated diagnoses.** On-call misled.
10. **Skipping the math.** "It just works" is a red flag.
11. **No periodic reevaluation.** Tools accumulate; rarely culled.
12. **Believing vendor case studies.** Trial on your data.
13. **Replacing rather than augmenting humans.** Agent authority too high.
14. **No fallback when AIOps fails.** Reliability single-point-of-failure.
15. **Treating AIOps as the strategy.** It's a tool; reliability is the strategy.

---

## 18. Mental Models

> **AIOps amplifies; it does not replace.** A bad foundation gets faster-bad.

> **Statistics first. ML where statistics fail.** Holt-Winters and EWMA cover most "smart alerting" use cases.

> **Rule-based alert grouping covers 80%. ML covers the rest.**

> **LLM is a fast-typing junior.** Drafts, summaries, hypotheses. Humans validate and act.

> **Shadow-mode every detector.** Promote to paging only with measured precision and recall.

> **Explainability is non-negotiable.** Black-box alerts erode on-call trust.

> **Phase 1-3 before phase 4-6.** Skipping leads to bad rollouts.

> **Quarterly evaluation. Cancel what doesn't pay.** AIOps tools earn their keep or go.

> **Agentic autonomy = blast radius × frequency × consequence.** Stay supervised until proven safe.

> **Reliability is the strategy. AIOps is one tool.**

This is the last chapter of the original roadmap. The next chapters (`doc 21+`) cover the topics the original roadmap missed — frontend / RUM / mobile, service mesh, database observability, network, streaming, LLM ops, security, telemetry-pipeline reliability, synthetic, error tracking — and then the enterprise-pattern chapters that follow.
