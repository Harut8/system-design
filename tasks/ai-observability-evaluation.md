## System Design Task: AI Observability + Evaluation Platform

### Problem Statement

Design a **unified AI Observability and Evaluation platform** — the system
every team building LLM-powered features (chatbots, RAG pipelines, coding
agents, autonomous multi-step agents) plugs into to answer three questions
they currently cannot answer at all: **"what actually happened on this
request,"** **"is it any good,"** and **"did the last change make it worse."**

Today, a typical mid-size org with 20 teams shipping LLM features looks like
this: half the teams have no tracing beyond `print()` statements and CloudWatch
logs that don't correlate a user's report ("the bot gave a wrong answer") back
to the exact prompt, retrieved documents, and model response that produced it.
Prompts live as Python f-strings scattered across the codebase, edited via
`git blame`-hostile diffs, with no record of which prompt version was live
when a given response was generated. Nobody can answer "did the prompt change
we shipped Tuesday make answers worse" without manually sampling transcripts
in a spreadsheet. Quality regressions are discovered by angry Slack messages
from support, days after a bad deploy, not by an automated check that ran
before the deploy went to 100% of traffic. Every team that wants an
"LLM-as-judge" re-implements it from scratch, with no calibration against
human judgment, so nobody trusts the scores it produces. Human feedback
(thumbs up/down) is collected in a dozen incompatible formats and never makes
it back into a dataset anyone can retrain or eval against.

The platform's job is to make **tracing, versioning, and evaluation a
byproduct of normal development**, not a separate project a team has to staff.
An agent call should be traced automatically, with zero code beyond an SDK
import. A prompt edit should create a new version, not overwrite history. A
prompt deployed to 5% of traffic should be automatically compared against the
control on the metrics the team cares about, and a statistically significant
quality drop should page someone — or trigger an automatic rollback — before a
human notices.

This is fundamentally an **observability problem with a stats and ML layer on
top**: the ingestion, storage, and query architecture look like a distributed
tracing / metrics system (think OpenTelemetry, Datadog, Honeycomb); the
evaluation layer looks like an experimentation platform crossed with an ML
model-evaluation harness. The two are inseparable in practice because eval
needs traces as its raw material (both to build datasets from production and
to attribute quality scores back to a specific prompt version, model, and
trace), and tracing is only actionable once you can say whether what it
recorded was *good*.

Assume adoption by **80+ internal teams** within 18 months, spanning
low-latency customer-facing chat, high-volume batch classification, and
long-running autonomous agents that make hundreds of LLM calls per run. Some
teams will process regulated data and require strict retention/redaction
policies; others want to dump everything into a shared eval dataset in the
open. The platform must serve both without forcing one team's compliance
posture onto another's iteration speed.

---

### Functional Requirements

1. **Distributed Tracing**

   * OpenTelemetry-compatible ingestion: accept standard OTel traces/spans so
     existing instrumentation and third-party SDKs interoperate, while adding
     LLM-specific span kinds as OTel semantic-convention extensions rather
     than a parallel, incompatible protocol.
   * Custom span types with typed attributes: `llm.call` (model, provider,
     prompt version, input/output messages, token counts, cost, latency,
     temperature and other sampling params, stop reason), `tool.execute`
     (tool name, arguments, result, duration, success/failure),
     `retrieval.query` (query text, retriever/index name, documents
     returned, scores, top-k), `agent.step` (step index, step type — think/
     act/observe —, state before/after), `chain.run` (named pipeline
     execution wrapping child spans).
   * Full trace context propagation across process boundaries and async
     execution: a single logical request that fans out across services (a
     gateway, a worker pool, an async tool-execution queue) must produce one
     connected trace, not fragments that have to be manually stitched
     together.
   * Automatic and manual instrumentation: an SDK decorator/context-manager
     that wraps a function to emit a span automatically, plus a manual API
     for teams whose call shape the decorator can't capture.
   * Trace-level metadata: user/session ID, tenant/team, environment
     (dev/staging/prod), release/deploy version, and arbitrary tags —
     queryable as first-class filters, not buried in a JSON blob.
   * Payload handling: large inputs/outputs (long documents, images, full
     retrieval result sets) stored separately from the trace's structured
     metadata so hot-path trace queries stay fast, with a defined size
     threshold before payloads move to blob storage.

2. **Agent-Specific Spans**

   * A hierarchical trace model that renders an agent run as a navigable
     tree/DAG: `AgentRun` → `Step`(s) → child `LLMCall` / `ToolCall` /
     `RetrievalQuery` spans, preserving causal and temporal order even when
     steps execute concurrently (parallel tool calls).
   * Explicit representation of the **think → act → observe** loop per step:
     the model's reasoning/plan for the step, the action taken (tool call or
     final answer), and the observation fed back into the next step —
     queryable independently (e.g., "show me every step where the model's
     plan didn't match the action it took").
   * **Tool call trees**: when a tool call itself triggers nested LLM calls or
     further tool calls (a tool that is itself a mini-agent), the nesting must
     be preserved and visualizable, not flattened.
   * **Multi-agent delegation graphs**: when a supervisor agent delegates to
     specialist sub-agents, the trace must show the delegation edges (which
     agent invoked which, with what sub-task) and allow cost/latency/error
     roll-up from every leaf sub-agent back to the top-level run.
   * Per-step and per-run **token and cost attribution**: total tokens/cost
     for a run must equal the sum of its LLM call spans, computed and
     queryable without a full trace scan at read time.
   * Run-level terminal state: success, max-steps-exceeded, timeout,
     error, cancelled — each independently queryable/alertable, distinct from
     "the HTTP request succeeded."

3. **Prompt Versioning**

   * A **prompt registry** where every save creates an immutable version:
     template text, variables/schema, model + generation config (temperature,
     max tokens, etc.), and any few-shot examples bundled as one versioned
     unit — never edited in place.
   * **Content-addressable storage**: identical prompt content (even if
     "saved" separately by two people) resolves to the same version, so
     history isn't polluted with no-op duplicate versions.
   * **Diff view** between any two versions of a prompt: template text diff,
     plus a structured diff of config fields (model changed, temperature
     changed, a few-shot example added/removed).
   * **Deployment tracking**: which prompt version is live in which
     environment (dev/staging/prod) and, within prod, what traffic split
     across versions (for canary/A-B rollouts) — a query over any trace must
     be able to say "which prompt version produced this."
   * **Rollback**: reverting an environment's deployed version must be a
     single action that takes effect without a code deploy, with the previous
     state recorded (who rolled back, from which version, to which, why).
   * Prompt version pinning: a caller can pin to an exact version (for
     reproducibility, e.g. in an offline eval) or resolve to "whatever's
     currently deployed to prod" (for normal runtime use) — both must be
     supported by the same registry read path.

4. **Dataset Management**

   * Multiple dataset tiers: **golden** (hand-curated, high-trust, used to
     gate releases), **silver** (larger, semi-automatically curated or
     weakly-labeled), and **production-sampled** (raw traces pulled from live
     traffic, unlabeled until annotated).
   * A defined schema per dataset example: input, expected output or
     grading criteria (not always exact-match — may be a rubric), metadata
     (source trace ID if derived from production, tags, difficulty), and
     provenance (who/what added it, when).
   * **Dataset versioning**: adding/removing/editing examples creates a new
     dataset version; an eval run must record and pin the exact dataset
     version it ran against, for reproducibility.
   * **Splits**: train/eval/test partitions where relevant (e.g., examples
     later used to fine-tune a judge or a reward model must not leak into the
     eval set used to score that judge).
   * **Production data collection pipelines**: continuously or on-demand
     sample real traces into a "candidate examples" pool based on filters
     (low user rating, judge flagged low score, random sample, specific
     error type) for human review before promotion into a golden set.
   * **Annotation workflow integration**: examples routed to human annotators
     for labeling (expected output, quality rating, free-text correction),
     with queue management and completion tracking (see Human Feedback).

5. **Offline Evaluation**

   * **Batch evaluation pipelines**: given a dataset version, a prompt
     version (or agent version), and a model, run every example through the
     pipeline and score every result — parallelized, resumable if
     interrupted partway.
   * Multiple built-in metric types: exact/fuzzy match, reference-based
     text similarity (BLEU/ROUGE, embedding cosine similarity), **faithfulness**
     (is the answer grounded in the provided context, for RAG), **relevance**
     (does the answer address the question), **toxicity/safety**
     classification, and **latency/cost** as first-class metrics alongside
     quality — plus a pluggable interface for teams to register custom
     metric functions.
   * **Comparison reports** across prompt/model versions: run the same
     dataset against version A and version B, report per-metric deltas with
     statistical significance, not just two raw numbers side by side.
   * **CI/CD integration**: an eval run invocable from a CI pipeline that can
     **gate a deploy** — fail the build if a defined metric regresses beyond
     a threshold versus the currently-deployed version's last eval run.
   * Support both **reference-based** eval (dataset has a known-correct
     answer) and **reference-free** eval (only a rubric/criteria exists, no
     single correct answer — most agent and open-ended generation tasks).

6. **Online Evaluation**

   * **Configurable sampling**: evaluate X% of live production traffic per
     agent/route/tenant, tunable independently per surface (e.g., 100% of a
     new low-volume agent, 1% of a high-volume mature one).
   * **Async evaluation pipeline**: scoring happens out of the request's
     critical path — the user is never blocked waiting for a judge call — and
     results are attached back to the originating trace once available.
   * **Automated checks on production traffic**: cheap, synchronous-capable
     checks (schema/format validation, PII leakage, banned-content match,
     latency/cost thresholds) distinguished from expensive async checks
     (LLM-judge scoring), so the cheap ones can run at or near 100% sampling.
   * **Canary evaluation for prompt deployments**: when a new prompt version
     is deployed to X% of traffic, automatically compute the same online
     metrics for canary vs. control cohorts and produce a live comparison,
     not just two independent dashboards a human has to eyeball.
   * **Automated promotion/rollback triggers** driven by the canary
     comparison, gated by statistical confidence, not a raw threshold on a
     tiny sample.

7. **LLM-as-Judge**

   * **Configurable judge prompts**: a judge is itself a versioned prompt
     (rubric, scoring scale, examples) — not hardcoded — so judges are
     iterated on and reviewed with the same rigor as production prompts.
   * **Multi-criteria scoring**: a single judge call can score multiple
     independent dimensions in one pass (e.g., relevance, faithfulness,
     helpfulness, safety) with a defined output schema, or run as separate
     specialized judge calls per criterion — support both, and state the
     cost/consistency trade-off between them.
   * **Judge calibration**: measure agreement between judge scores and human
     labels on a held-out calibration set (Cohen's kappa or equivalent);
     surface this agreement score per judge/criterion so consumers know how
     much to trust it, and re-run calibration whenever the judge prompt or
     judge model changes.
   * **Inter-judge agreement**: when multiple judge configurations (different
     prompts, different models) score the same data, measure and surface
     their agreement with each other, to catch a judge that's an outlier.
   * **Judge model selection**: support cheap/fast judges for high-volume
     online sampling and expensive/high-accuracy judges for offline gating,
     with an explicit cost-vs-accuracy trade-off documented per judge config.
   * **Cost management**: batching, caching of identical judge calls, and
     a hard budget ceiling per eval run/day so judge spend cannot silently
     balloon (a naive design re-scores identical (prompt, response) pairs
     repeatedly across overlapping eval runs).
   * **Judge versioning**: judge prompt/model changes are versioned exactly
     like production prompts, so a historical eval score can be attributed to
     the exact judge version that produced it, and judge regressions are
     themselves detectable.

8. **Human Feedback**

   * A **feedback collection SDK** embeddable in product UIs: inline
     thumbs up/down, Likert-scale ratings, free-text corrections, and
     structured multi-dimension forms — attributable back to the exact
     trace/run/message that was rated.
   * **Annotation queue management** for internal reviewers: assign
     examples to annotators (round-robin, skill-based, or targeted), track
     completion, support re-review, and prevent the same annotator from
     single-handedly deciding a golden-set label without a second opinion
     where policy requires it.
   * **Inter-annotator agreement** measurement (Cohen's kappa for two
     annotators, Fleiss' kappa for more) surfaced per dataset/task, with a
     defined process for resolving disagreement (adjudication by a third
     annotator, majority vote, or escalation).
   * **Feedback aggregation**: multiple feedback signals on the same
     trace (end-user thumbs down + internal annotator's 4/5 rating + an
     auto-judge score) reconciled into a queryable summary, without silently
     discarding disagreement.
   * **Feedback → dataset pipeline**: feedback above/below a threshold (or
     flagged for a specific failure mode) automatically becomes a candidate
     example for the production-sampled dataset tier.
   * **Feedback → fine-tuning pipeline**: a defined, auditable path for
     high-confidence corrected examples to flow into training data for a
     future fine-tune or reward model — with consent/compliance gating,
     since this data flow has different legal implications than eval.

9. **Regression Detection**

   * **Statistical tests** on eval metric distributions between two versions
     (current vs. candidate, or before/after a deploy): two-sample t-test or
     Mann-Whitney U depending on metric distribution shape, plus bootstrap
     confidence intervals for metrics without a clean parametric form.
   * **Change-point detection** on metric time series (not just two-point
     comparisons) to catch a gradual quality drift that no single before/
     after comparison would flag.
   * **Alert rules**: absolute threshold ("faithfulness < 0.8"), relative
     degradation ("latency P99 up more than 20% week-over-week"), and trend-
     based ("3 consecutive days of declining CSAT") — each independently
     configurable per metric/agent/team.
   * **Automated rollback triggers**: a canary deployment whose online
     metrics regress beyond a defined, statistically-confident threshold
     triggers automatic traffic reversion to the previous prompt version,
     with the triggering evidence attached to the incident record.
   * **A/B test analysis**: proper experiment analysis (not just "version A's
     average is higher") — sample size/power considerations, multiple-
     comparison correction when many metrics are tested simultaneously, and
     clear reporting of practical vs. statistical significance.
   * **False-positive management**: alert suppression/deduplication so a
     single regression doesn't fan out into dozens of redundant pages, and a
     feedback loop for marking an alert as a false positive that tunes future
     sensitivity.

10. **Dashboards and Alerting**

    * Real-time dashboards scoped per agent, per prompt version, per model,
      and per team, each showing volume, latency, cost, error rate, and
      quality-score trends on a common time axis.
    * **Cost tracking** broken down by team/agent/model/prompt-version, with
      budget-vs-actual and forecast-to-end-of-period.
    * **Quality score trends**: online eval and human feedback scores plotted
      over time with deploy markers overlaid, so a quality change is visually
      correlated with the change that caused it.
    * **Alert rules** authorable by end users (not just platform operators)
      through the same UI, with configurable notification channels
      (Slack/PagerDuty/email/webhook) and severity tiers.
    * Drill-down from any dashboard widget straight to the underlying traces
      that produced the aggregate number — an aggregate metric without a path
      to its raw evidence is not acceptable.

---

### Non-Functional Requirements

1. **Scale**

   * Trace ingestion: sustained **50,000 spans/sec**, peak **150,000
     spans/sec** platform-wide.
   * 80+ tenants, 2,000+ distinct agent/prompt combinations in active use.
   * Average traced run: 3–10 spans; long-tail agent runs: hundreds to
     low-thousands of spans over minutes to hours.
   * Offline eval throughput: a single eval run must be able to score a
     10,000-example dataset within **30 minutes** end to end (generation +
     scoring), parallelized across workers.
   * Online eval: capable of sampling and scoring up to **5% of a
     100,000-req/sec production stream** without materially impacting
     ingestion latency.
   * Storage: assume **500 TB/year** of raw trace/payload data at target
     scale before compression/tiering.

2. **Latency**

   * Trace ingestion (SDK emits span → durably queued): **P99 ≤ 200 ms**,
     and must never block the instrumented application's own request path.
   * Trace query (fetch a single trace by ID, fully hydrated): **P99 ≤
     300 ms**.
   * Dashboard aggregate queries (e.g., last-24h latency histogram for one
     agent): **P99 ≤ 2 s** for pre-aggregated windows.
   * Online eval "cheap check" (schema/PII/format validation): **P99 ≤
     500 ms** from trace completion to result attached.
   * Online eval "judge check" (async, not blocking the user): result
     attached within **P99 ≤ 60 s** of trace completion.
   * Prompt registry read (resolve "current prod version" for a given
     prompt): **P99 ≤ 10 ms**, since this sits in front of every LLM call at
     runtime.

3. **Availability**

   * Trace ingestion path: **99.95%** — a platform outage must not be able to
     take down any team's own product (the SDK must degrade to local
     buffering/drop, never to blocking or crashing the caller).
   * Prompt registry read path: **99.99%**, since it is in the hot path of
     every instrumented LLM call; a registry outage with no cached fallback
     would take down every dependent product simultaneously.
   * Eval and dashboard services: **99.9%** — best-effort, may degrade before
     ingestion does.

4. **Durability and Consistency**

   * No silent trace loss: if a span cannot be durably persisted, that must
     be counted and alertable, never dropped without a trace (pun intended)
     of the drop itself.
   * Prompt version history is immutable and append-only — a published
     version's content can never change retroactively; only new versions or
     deployment pointers change.
   * Eval results must be reproducible: an eval run record must pin dataset
     version, prompt version, model version, and judge version precisely
     enough that re-running it against the same pins produces a comparable
     result (allowing for LLM non-determinism at a fixed temperature/seed
     where the provider supports it).

5. **Operability and Cost**

   * Sampling and retention must be independently configurable per tenant so
     a high-volume, cost-sensitive team and a low-volume, compliance-heavy
     team can coexist without one dictating the other's cost profile.
   * LLM-judge spend must be bounded and forecastable — a runaway eval loop
     or misconfigured 100%-sampling online judge must not be able to produce
     a surprise five-figure bill overnight.
   * The system must be operable by a platform team of **6–8 engineers** at
     target scale.

---

### Constraints and Assumptions

* You do not control the instrumented applications' languages/frameworks —
  assume SDKs are needed for at least Python and TypeScript/JavaScript, and
  design the wire protocol so other languages can implement a compatible SDK
  later.
* LLM providers are non-deterministic even at temperature 0 for some models;
  eval reproducibility must be defined honestly against this constraint, not
  assumed away.
* Some tenants are subject to data-retention and redaction requirements
  (PII must not persist in traces beyond a defined window, or must be
  redacted before storage); others want maximal retention for eval/training
  purposes. Both must be policy, not code, differences.
* LLM-as-judge is not free and not perfectly accurate — the design must treat
  judge scores as a signal with a measured error rate, not ground truth, and
  must make that error rate visible to consumers of the score.
* Assume the platform is adopted incrementally — some teams will onboard
  only tracing initially and add eval later, or vice versa — the components
  should be independently valuable, not an all-or-nothing bundle.

---

### What You Should Deliver

1. Requirement clarification and explicit assumptions.
2. High-level architecture: every major component (Trace Collector, Span
   Processor, Trace Store, Prompt Registry, Dataset Service, Offline Eval
   Engine, Online Eval Service, LLM Judge Service, Human Feedback Service,
   Regression Detector, Dashboard Service, Alert Manager) and how data flows
   between them.
3. The trace/span data model, including how agent-specific spans (think/act/
   observe, tool trees, multi-agent delegation) are represented and queried.
4. Prompt versioning design: storage model, diff mechanism, deployment/
   rollback flow, and how a running application resolves "which version do I
   use right now."
5. Dataset management design: schema, versioning, production-sampling
   pipelines, annotation workflow integration.
6. Offline evaluation engine design: pipeline architecture, metric
   computation, comparison reports, CI/CD gating.
7. Online evaluation design: sampling architecture, async scoring pipeline,
   canary comparison, automated promotion/rollback.
8. LLM-as-judge design: judge prompt/config model, multi-criteria scoring,
   calibration methodology, cost controls.
9. Human feedback design: collection SDK, annotation queue, agreement
   metrics, and the pipeline from feedback into datasets and training data.
10. Regression detection design: statistical methodology, alerting rules,
    automated rollback mechanics, false-positive management.
11. Storage architecture with concrete technology choices and justification
    (why this store for traces vs. metadata vs. artifacts vs. time series).
12. Core data models/schemas for Trace, Span, PromptVersion, Dataset,
    DatasetExample, EvalRun, EvalResult, JudgeConfig, FeedbackEntry, Alert.
13. Key APIs: trace ingestion, prompt CRUD, dataset CRUD, eval submission/
    results, feedback collection, dashboard queries.
14. Capacity estimates with arithmetic shown, matched against the numbers
    above.
15. Failure-mode walkthroughs for at least: trace ingestion overload/loss,
    an eval pipeline failure mid-run, a judge model silently degrading
    (still returns scores, but they've drifted from human judgment), and a
    flood of low-quality/spam human feedback.
16. Cost model: what drives spend (storage, judge calls, human annotation)
    and concrete strategies to control each.
17. Explicit trade-offs: full tracing vs. sampling, real-time vs. batch eval,
    LLM-judge vs. human eval, centralized vs. embedded evaluation logic.
18. An evolution path from a minimal v1 to the full platform.

---

### Expectations

* **Do the arithmetic.** Ingestion throughput, storage growth, eval
  parallelism, and judge cost should appear as derived numbers, not
  adjectives.
* **Treat judge scores as measured, not assumed, signal.** Any design that
  uses an LLM judge without describing how its accuracy is calibrated and
  monitored is incomplete.
* **Be precise about reproducibility.** State exactly what is and is not
  guaranteed to be reproducible about a given eval run, and why.
* **Show the failure walkthrough.** For each failure class, state what the
  instrumented application observes, what the platform team observes, and
  what the automated/human response is.
* Prefer a design that makes the **common case** (a team just wants traces
  and a prompt registry) trivially easy to adopt, while the **advanced case**
  (full online eval with automated rollback) is available without forcing
  every team to pay its complexity cost upfront.
</content>
