# AI Observability + Evaluation Platform: Design Document

> Solution to [`tasks/ai-observability-evaluation.md`](../tasks/ai-observability-evaluation.md).

### Prerequisites and Learning Resources

Before or alongside this document, study these deep-dive chapters from the curriculum:

| Section in this doc | Curriculum chapter | What it covers |
|--------------------|--------------------|----------------|
| §7–8 Offline/Online Eval | [`ai-rag/08-evaluation-methodology.md`](../ai-rag/08-evaluation-methodology.md) | Retrieval metrics (recall@k, MRR, NDCG), end-to-end RAG metrics (faithfulness, relevance), golden dataset design, LLM-as-judge methodology |
| §4 Agent Trace Model | [`ai-rag/22-agent-orchestration-patterns.md`](../ai-rag/22-agent-orchestration-patterns.md) | Agent execution patterns (ReAct, plan-and-execute, multi-agent) — the workflows whose traces this platform captures |
| §9 LLM-as-Judge | [`ai-rag/08-evaluation-methodology.md`](../ai-rag/08-evaluation-methodology.md) | Judge prompt design, calibration, inter-judge agreement — the evaluation theory behind §9 |
| §3 Distributed Tracing | [`ai-rag/20-langchain-architecture-and-internals.md`](../ai-rag/20-langchain-architecture-and-internals.md) | LangChain's callback system and tracing — the instrumentation points this platform hooks into |
| §5 Prompt Versioning | [`ai-rag/21-langgraph-deep-dive.md`](../ai-rag/21-langgraph-deep-dive.md) | LangGraph state management — prompts are versioned alongside graph definitions |
| General context | [`ai-rag/00-mental-models.md`](../ai-rag/00-mental-models.md) | The represent→retrieve→generate pipeline — understanding what you're observing and evaluating |

---

This document designs the platform end to end: how a span gets from an application's LLM call into a queryable trace, how a prompt earns a version and a deployment history, how a batch of golden examples becomes a CI gate, how a canary rollout is judged safe or unsafe automatically, and how every one of those judgments stays traceable back to the evidence that produced it. Sections are written to be read in order — later sections lean on data models and mechanisms introduced earlier (the trace model in §3-4 is the foundation everything else in §6-11 is built on) — but each section is also self-contained enough to serve as a reference once the whole system is understood.

## Table of Contents

1. [Requirements Clarification](#1-requirements-clarification)
2. [Architecture Overview](#2-architecture-overview)
3. [Distributed Tracing](#3-distributed-tracing)
4. [Agent Trace Model](#4-agent-trace-model)
5. [Prompt Versioning](#5-prompt-versioning)
6. [Dataset Management](#6-dataset-management)
7. [Offline Evaluation Engine](#7-offline-evaluation-engine)
8. [Online Evaluation](#8-online-evaluation)
9. [LLM-as-Judge](#9-llm-as-judge)
10. [Human Feedback System](#10-human-feedback-system)
11. [Regression Detection](#11-regression-detection)
12. [Storage Architecture](#12-storage-architecture)
13. [Data Models](#13-data-models)
14. [API Design](#14-api-design)
15. [Scaling](#15-scaling)
16. [Failure Modes](#16-failure-modes)
17. [Cost Model](#17-cost-model)
18. [Trade-offs](#18-trade-offs)
19. [Evolution Path](#19-evolution-path)
20. [Exercises](#20-exercises)

---

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|---|---|---|
| Scope | Tracing only, or tracing + eval + feedback as one system? | One system — eval needs traces as raw material, tracing needs eval to be actionable. Components are independently adoptable. |
| Tenancy | Single company, many teams, or multi-customer SaaS? | Internal platform, 80+ teams, each a tenant with hard data isolation. |
| Protocol | Build a new tracing protocol or extend OpenTelemetry? | Extend OTel: custom span kinds via semantic-convention attributes, standard OTLP wire format, so existing OTel tooling still works. |
| Determinism | Can eval runs be guaranteed reproducible? | No — LLM outputs vary even at temperature 0 for many providers. Pin every *input* (dataset, prompt, model, judge versions) and treat output variance as a measured, reported quantity, not eliminated. |
| Judge trust | Is LLM-judge output treated as ground truth? | No — treated as a calibrated signal with a measured agreement rate against human labels, re-calibrated whenever the judge changes. |
| Data sensitivity | Do all tenants want full-content logging? | No — per-tenant retention and redaction policy; some want maximal retention for eval, others need PII scrubbed within a window. |
| Real-time requirement | Must online eval block the user response? | No — always async, off the request's critical path. Only ingestion (queuing the span) sits in the critical path, and even that must degrade to local buffering rather than block. |
| Rollback authority | Who can trigger a prompt rollback? | Both human (one-click in UI) and automated (regression detector, if a tenant opts into auto-rollback for a given deployment). |
| Cost ownership | Who pays for LLM-judge calls? | Charged back to the tenant that owns the agent being evaluated, same as their model-call costs — visible in the same cost dashboard. |
| Language support | Which SDK languages are required at launch? | Python and TypeScript/JS at launch (covers the large majority of LLM app code); wire protocol designed so a Go/Java SDK can follow without a protocol change. |
| Ownership of "correctness" | Who decides a rubric/grading criteria is right? | The team that owns the agent, not the platform team — the platform provides the mechanism (datasets, judges, calibration) and enforces process rigor (versioning, second-review), not domain judgment. |
| Cross-region deployment | Is the platform itself deployed to one region or multiple? | Deployed per-region, mirroring where tenants' own applications run, since trace ingestion latency (§2.4 hot path) would suffer from a cross-continent hop; control-plane data (prompt registry, datasets) replicates across regions for tenants operating multi-region, with region as an explicit dimension in `environment` alongside dev/staging/prod. |
| PII handling default | Is full-content logging the default? | No — default policy is `redact_pii` (see §3.3.1) for all tenants; a tenant must explicitly opt into `log_full` for eval/training purposes, never the reverse. |
| Judge vendor lock-in | Must judges use the same provider as the app being evaluated? | No — judge model and application model are independently selectable; this is deliberate, since using the same model to both generate and grade an answer is a known source of self-preference bias in LLM-judge literature. |
| Existing OTel users | What happens to a team already emitting standard OTel traces to another backend? | They dual-export (standard OTel collectors support multiple exporters natively) — this platform is additive, not a forced migration off existing APM tooling for non-LLM spans. |

### Key Assumptions

1. **Traces are the substrate.** Every other capability (datasets, eval, feedback, regression detection) either consumes traces or produces data attached back to a trace. Get the trace model right first.
2. **Prompts are versioned artifacts, not code.** They must be editable and deployable by non-engineers (PMs, prompt engineers) without a code deploy, while still being diffable and revertible like code.
3. **Judges are software with a bug rate.** Every judge configuration has a calibration score; consumers see it; a judge below a trust threshold is flagged, not silently trusted.
4. **Sampling is a first-class dial, not an afterthought.** At target scale, 100%-everything is not economically viable (judge cost dominates); every layer (tracing, online eval) has an explicit, tunable sampling rate.
5. **Multi-tenant isolation is structural, not a filter.** Tenant data (traces, datasets, prompts) is partitioned at the storage layer, not merely filtered at query time, to prevent both accidental leakage and noisy-neighbor performance impact.
6. **The platform team is 6–8 engineers.** Every design choice is evaluated against "can this be operated and debugged by a small team," not just "is this theoretically optimal."

### 1.1 Explicit Non-Goals

Stated up front, since a design document that never says what it isn't tends to accumulate silent scope creep in review:

* Not a general-purpose APM/infra-monitoring replacement — non-LLM service spans ride the same pipe (§3.1) but the platform's value-add (prompt versioning, eval, judges) is specifically about LLM/agent behavior, not e.g. database query performance monitoring in its own right.
* Not a synchronous guardrail/content-filter enforcement layer (§18.1) — it observes and scores, it does not block a response before the user sees it.
* Not a training or fine-tuning platform — it produces clean, consented, attributable *export* pipelines (§10.6) that a separate ML training system consumes; it does not run training jobs itself.
* Not a prompt-authoring IDE or a no-code agent builder — the Prompt Registry versions and deploys whatever content it's given; a rich authoring UI is a plausible client of its API, not a component this document designs.

---

## 2. Architecture Overview

### 2.1 Component Map

```
                                    ┌─────────────────────────────────────────┐
                                    │         Instrumented Applications         │
                                    │  (agents, RAG pipelines, chat services)   │
                                    └───────────────┬───────────────────────────┘
                                                     │  SDK (Python / TS)
                                                     │  spans, prompt reads, feedback events
                                                     ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│                                   EDGE / INGRESS                                    │
│  ┌───────────────────┐   ┌────────────────────┐   ┌────────────────────────────┐   │
│  │  Trace Collector    │   │  Prompt Registry    │   │  Feedback Collector API    │   │
│  │  (OTLP gRPC/HTTP)   │   │  Read Cache (edge)  │   │                             │   │
│  └─────────┬───────────┘   └──────────┬──────────┘   └──────────────┬─────────────┘   │
└────────────┼─────────────────────────┼───────────────────────────┼─────────────────┘
             │                          │                            │
             ▼                          ▼                            ▼
   ┌───────────────────┐     ┌──────────────────────┐     ┌───────────────────────┐
   │  Ingest Queue       │     │  Prompt Registry Svc   │     │  Feedback Service       │
   │  (Kafka)            │     │  (control plane)       │     │                         │
   └─────────┬───────────┘     └──────────┬─────────────┘     └───────────┬─────────────┘
             │                            │                               │
             ▼                            ▼                               ▼
   ┌───────────────────┐       ┌────────────────────┐          ┌──────────────────────┐
   │ Span Processor      │       │ Postgres (versions,│          │ Postgres (feedback,   │
   │ (enrich, sample,    │       │ deployments, diffs)│          │ annotation queues)    │
   │ redact, roll up)    │       └────────────────────┘          └───────────┬──────────┘
   └─────────┬───────────┘                                                  │
             │                                                              ▼
   ┌─────────┴────────────────────────────┐                     ┌──────────────────────┐
   ▼                                       ▼                     │ Dataset Service        │
┌────────────────┐               ┌──────────────────┐            │ (production sampling,  │
│ Trace Store      │               │ Payload Store     │            │ annotation workflow)   │
│ (ClickHouse)     │◄─────────────►│ (S3 / blob)        │            └───────────┬───────────┘
│ structured spans │   pointer     │ large I/O blobs    │                        │
└────────┬─────────┘               └────────────────────┘                        ▼
         │                                                              ┌──────────────────────┐
         │                                                              │ Offline Eval Engine    │
         ▼                                                              │ (batch runner,         │
┌──────────────────┐      ┌──────────────────────┐      ┌─────────────►│  metric computation)   │
│ Online Eval        │      │ Regression Detector    │      │             └───────────┬───────────┘
│ Service (sampler +  │─────►│ (stats tests, alerts, │      │                         │
│ async scorer)       │      │  auto-rollback)        │      │                         ▼
└─────────┬───────────┘      └──────────┬─────────────┘      │              ┌──────────────────────┐
          │                             │                     │              │ LLM Judge Service      │
          ▼                             ▼                     │◄─────────────┤ (versioned judges,     │
┌──────────────────┐      ┌──────────────────────┐            │              │  calibration, cost mgmt)│
│ Metrics TSDB        │      │ Alert Manager          │            │              └──────────────────────┘
│ (Prometheus/M3)     │      │ (Slack/PD/webhook)     │            │
└─────────┬───────────┘      └────────────────────────┘            │
          │                                                         │
          ▼                                                         │
┌──────────────────────────────────────────────────────────────────┴────┐
│                          Dashboard Service (query fan-out)              │
│              per agent / prompt version / model / team views            │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.2 Control Plane vs. Data Plane

| Plane | Components | Consistency | Availability target |
|---|---|---|---|
| **Data plane** | Trace Collector, Ingest Queue, Span Processor, Trace Store, Payload Store, Online Eval sampler | High throughput, eventually consistent, durability-over-latency | 99.95% |
| **Control plane** | Prompt Registry, Dataset Service, Offline Eval Engine, Judge Service config, Regression Detector rules, Alert rules | Strongly consistent reads-after-write, low volume, high correctness | 99.99% for Prompt Registry read path specifically (hot path dependency) |

The split matters because the two planes have opposite dominant failure modes: the data plane fails by falling behind under load (solved with buffering, backpressure, sampling); the control plane fails by being *wrong* (a stale prompt version served to production is a correctness bug, not a performance one), so it is designed for strong consistency at low volume rather than throughput.

### 2.3 Request-Time vs. Async Paths

Two distinct data flows share the platform:

1. **Synchronous, request-time path** — an application calls `sdk.get_prompt("support-triage", env="prod")` before making an LLM call. This must be fast (P99 ≤ 10 ms) and resilient (cached locally, degrades to last-known-good on registry outage). This is the *only* platform call allowed to sit in an application's critical path.
2. **Asynchronous, best-effort path** — everything else: span emission, feedback events, online eval sampling and scoring, dashboard aggregation. All of it is fire-and-forget from the application's perspective, buffered locally by the SDK, and shipped in the background. A total platform outage on this path degrades observability, never the product.

This asymmetry — one hot synchronous read path, everything else async — is the single most important architectural decision in the system, because it's what lets a platform outage never become a customer-facing outage for the 80+ dependent teams.

### 2.4 Capacity Estimates

```
INGESTION
  Sustained spans/sec:            50,000       (NFR target)
  Peak spans/sec:                 150,000      (NFR target)
  Avg span row size (ClickHouse): ~700 bytes (structured attrs, payload refs only)
  Sustained ingest bytes/sec:     50,000 * 700B  ≈ 35 MB/s
  Peak ingest bytes/sec:          150,000 * 700B ≈ 105 MB/s
  Daily span volume (sustained):  50,000 * 86,400 ≈ 4.32B spans/day
  Daily structured storage:       4.32B * 700B ≈ 3.0 TB/day  → ~1.1 PB/year raw
    (before TTL tiering/deletion — see §12.1 for the retention curve that
     brings effective steady-state storage down substantially)

PAYLOADS (externalized, §3.3)
  Estimate: ~40% of spans carry an externalized payload (llm.call, tool.execute,
  retrieval.query — not agent.step/chain.run bookkeeping spans)
  Avg externalized payload: ~3 KB after content-dedup
  4.32B spans/day * 40% * 3 KB ≈ 5.2 TB/day raw → deduped (shared retrieved-doc
  content, repeated system prompts) reduces this by an estimated 25-35% in
  practice, consistent with the 500 TB/year NFR target once tiering is applied.

PROMPT REGISTRY (hot path)
  Reads: every LLM call resolves a prompt → at 50,000 spans/sec sustained and
  an estimated 1 llm.call span per ~3 total spans, that's ~16,000 registry
  reads/sec sustained — served entirely from edge cache (§5.6), not hitting
  Postgres; Postgres only sees writes (new versions/deployments, low volume,
  <10/sec even at 80+ tenants actively iterating) and cache-miss refills.

OFFLINE EVAL
  10,000 examples / 30 min SLA → ≥22 concurrent workers (derived in §7.2),
  provisioned at 40 for headroom.

ONLINE EVAL (single large tenant, illustrative)
  100,000 req/sec platform peak; a given agent might carry 5,000 req/sec of that.
  Cheap checks at 100% sampling: 5,000 checks/sec, ~5-20ms each → a small,
  horizontally-scaled worker pool (CPU-bound, no external calls) handles this
  easily — roughly 10-15 workers at ~99% utilization budget.
  Judge checks at 2% sampling: 100 judge calls/sec for that one agent —
  provisioned against the judge model's provider TPM/RPM quota, not raw
  platform compute; this is the actual bottleneck (see §9.5, §17).
```

### 2.5 End-to-End Sequence Flows

**Trace ingestion, from application call to queryable trace:**

```
Application         SDK              Collector        Kafka      Span Processor   ClickHouse
    │                 │                   │              │              │              │
    │  llm call made  │                   │              │              │              │
    │────────────────►│                   │              │              │              │
    │                 │ span buffered     │              │              │              │
    │                 │ locally (async)   │              │              │              │
    │  response       │                   │              │              │              │
    │◄────────────────│                   │              │              │              │
    │                 │ batch flush       │              │              │              │
    │                 │ (OTLP)            │              │              │              │
    │                 │──────────────────►│              │              │              │
    │                 │                   │ validate,    │              │              │
    │                 │                   │ ack (fast)   │              │              │
    │                 │                   │─────────────►│              │              │
    │                 │                   │              │  consume,    │              │
    │                 │                   │              │  enrich,     │              │
    │                 │                   │              │  tail-sample,│              │
    │                 │                   │              │  rollup      │              │
    │                 │                   │              │─────────────►│              │
    │                 │                   │              │              │ batched      │
    │                 │                   │              │              │ insert       │
    │                 │                   │              │              │─────────────►│
    │                 │                   │              │              │              │ queryable
```

Note the application only ever waits on the local SDK buffer write, never on the network hop to the Collector — that hop, and everything after it, is fully decoupled from the request path per §2.3.

**Prompt deploy and rollback:**

```
Editor UI       Prompt Registry Svc     Postgres        Redis pub/sub     Edge caches (N nodes)
   │                    │                   │                 │                   │
   │ save template      │                   │                 │                   │
   │───────────────────►│ compute content   │                 │                   │
   │                    │ hash, upsert      │                 │                   │
   │                    │──────────────────►│                 │                   │
   │ deploy to prod      │                   │                 │                   │
   │───────────────────►│ INSERT deployment; │                 │                   │
   │                    │ flip active ptr    │                 │                   │
   │                    │ (1 transaction)    │                 │                   │
   │                    │──────────────────►│                 │                   │
   │                    │ publish invalidate │                 │                   │
   │                    │───────────────────────────────────►│                   │
   │                    │                   │                 │ push to all nodes │
   │                    │                   │                 │──────────────────►│
   │                    │                   │                 │                   │ refill on
   │                    │                   │                 │                   │ next read
   │  ... regression     │                   │                 │                   │
   │  detected 20 min    │                   │                 │                   │
   │  later ...          │                   │                 │                   │
   │ rollback             │                   │                 │                   │
   │───────────────────►│ INSERT new deployment│                 │                   │
   │                    │ (points at prev    │                 │                   │
   │                    │ version_id),       │                 │                   │
   │                    │ flip ptr back      │                 │                   │
   │                    │──────────────────►│                 │                   │
   │                    │ publish invalidate │                 │                   │
   │                    │───────────────────────────────────►│──────────────────►│
```

Both deploy and rollback are the *same* mechanism (append a `PromptDeployment` row, flip the active pointer, invalidate caches) — rollback is not a special-cased code path, which is exactly what makes it safe to trigger automatically from the Regression Detector (§8.5, §11.5) without a separate, less-tested "emergency" code path.

### 2.6 Observing the Observability Platform Itself

A platform whose entire purpose is telling other teams whether their system is healthy has an obvious credibility problem if it cannot answer the same question about itself. The platform dogfoods its own SDK and pipeline for its own control-plane services:

* Every platform service (Collector, Span Processor, Prompt Registry, Eval Scheduler, Judge Service, Regression Detector) emits **standard OTel traces** (not LLM-specific spans — these are ordinary service-to-service spans) into a **separate, platform-owned tenant** of the same Trace Store, so a platform engineer debugging "why is ingestion lagging" uses the exact same trace UI a product team uses to debug their agent.
* Platform-internal SLOs (ingest lag, registry read latency, judge queue depth, eval-run completion time) are tracked as ordinary metrics in the same Metrics TSDB and alerted through the same Alert Manager — there is no separate, bespoke platform-monitoring stack to keep in sync with the product-facing one.
* The **judge calibration re-run** (§9.3, §16.3) and the **span roll-up invariant check** (§4.3: `sum(leaf llm.call costs) == root.rolled_up_cost_usd`) are themselves scheduled jobs whose pass/fail history is a platform-health metric, tracked and alerted on exactly like a tenant's quality metric would be — the platform treats its own correctness properties as first-class monitored signals, not one-time assertions checked only in a test suite.
* This matters operationally for the "operable by 6-8 engineers" NFR (§1): a platform team that has to context-switch to a completely different toolchain to debug their own system, versus using the product they run, both slows incident response and means platform-observability bugs are far more likely to go unnoticed than product-observability bugs would be.

---

## 3. Distributed Tracing

### 3.1 Why Extend OpenTelemetry Rather Than Build a New Protocol

OTel already solves context propagation, sampling, exporters, and has SDKs in every major language. Building a parallel protocol means every team gets two instrumentation libraries to reason about, and loses interoperability with tools they already use (Datadog, Honeycomb, Jaeger). Instead:

* Standard OTLP (gRPC/HTTP) is the wire protocol.
* LLM-specific data rides as span **attributes** following a defined semantic-convention prefix (`llm.*`, `agent.*`, `retrieval.*`, `tool.*`) rather than inventing new span types at the protocol level — a span is still a span; a `llm.call` "span kind" is really `span.kind = "llm.call"` set as an attribute plus a validated attribute schema for that kind.
* Any OTel-instrumented service already emitting standard HTTP/DB spans plugs into the same trace with zero extra work; only the LLM-specific parts need the platform SDK.

### 3.2 Span Kind Schemas

```yaml
# llm.call span attributes
span.kind: "llm.call"
llm.provider: "anthropic"
llm.model: "claude-sonnet-4-5"
llm.prompt_version_id: "pv_8f2a1c"   # resolved prompt version, if from registry
llm.input_messages_ref: "blob://payloads/8f2a1c9d.json"   # pointer, not inline
llm.output_ref: "blob://payloads/9c3b2e1a.json"
llm.input_tokens: 1834
llm.output_tokens: 412
llm.cached_input_tokens: 1200
llm.cost_usd: 0.0182
llm.temperature: 0.3
llm.max_tokens: 1024
llm.stop_reason: "end_turn"
llm.latency_ttft_ms: 340
llm.latency_total_ms: 2100

# tool.execute span attributes
span.kind: "tool.execute"
tool.name: "search_knowledge_base"
tool.args_ref: "blob://payloads/..."
tool.result_ref: "blob://payloads/..."
tool.success: true
tool.duration_ms: 180
tool.error_type: null

# retrieval.query span attributes
span.kind: "retrieval.query"
retrieval.index: "support-kb-v3"
retrieval.query_text_ref: "blob://payloads/..."
retrieval.top_k: 8
retrieval.results_ref: "blob://payloads/..."
retrieval.scores: [0.91, 0.88, 0.81, 0.77, 0.74, 0.70, 0.65, 0.61]

# agent.step span attributes
span.kind: "agent.step"
agent.step_index: 3
agent.step_type: "act"       # think | act | observe
agent.state_before_ref: "blob://payloads/..."
agent.state_after_ref: "blob://payloads/..."

# chain.run span attributes
span.kind: "chain.run"
chain.name: "support-triage-pipeline"
chain.version: "3"
```

### 3.3 Payload Externalization

Structured attributes (model name, token counts, cost) stay inline on the span row in the columnar store — they're small, fixed-shape, and queried constantly. Large free-form content (full message arrays, retrieved documents, tool results) is written to blob storage and referenced by pointer:

```
threshold: any single attribute value > 4 KB → externalize
storage:   S3, key = sha256(content) for automatic dedup
           (two spans with identical retrieved-doc content share one blob)
trace row: stores only the blob key + a content-type + byte size
```

This keeps the hot trace-query path (list spans, filter by attribute, aggregate) fast — a ClickHouse row stays under ~1 KB even for a rich `llm.call` span — while full content is one indexed lookup away when a human opens a trace in the UI.

### 3.3.1 PII Redaction and Retention Policy Enforcement

Since payload content is exactly where PII lives (user messages, retrieved documents, tool arguments), redaction is enforced at the same externalization boundary rather than bolted on separately:

```python
class RedactionPolicy:
    tenant_id: str
    mode: str                 # "log_full" | "redact_pii" | "hash_only" | "no_persist"
    pii_categories: list[str] # ["email", "phone", "ssn", "credit_card", "name"] — configurable
    retention_days: int       # payload deletion window, independent of trace-metadata retention

def externalize_payload(content: str, policy: RedactionPolicy) -> PayloadRef:
    if policy.mode == "no_persist":
        return PayloadRef(blob_key=None, note="not persisted per tenant policy")
    if policy.mode == "redact_pii":
        content = pii_scrubber.redact(content, categories=policy.pii_categories)
    elif policy.mode == "hash_only":
        return PayloadRef(blob_key=None, content_hash=sha256(content))
    key = f"{policy.tenant_id}/{sha256(content)}"
    blob_store.put(key, content, expires_in_days=policy.retention_days)
    return PayloadRef(blob_key=key)
```

* `redact_pii` runs a combination of regex (structured PII — emails, phone numbers, SSNs, card numbers) and an NER model (unstructured PII — names, addresses) before the payload is written to blob storage — redaction happens **before persistence**, not as a post-hoc scrub, so unredacted content is never at rest even transiently.
* This is explicitly the "logged" side of the log-vs-send distinction the task calls out: the content sent *to* the model provider is whatever the application sent (the platform doesn't intercept or alter outbound model calls); redaction governs only what the platform itself persists for observability/eval purposes.
* A scheduled job re-sweeps existing blobs against current policy whenever a tenant tightens their `pii_categories` or `retention_days` — policy changes apply retroactively to already-stored payloads within one sweep cycle (default: daily), not only to new writes going forward.
* `retention_days` for payloads is tracked **independently** from the trace metadata TTL (§12.1) — a common compliance pattern is "keep the fact that a call happened, its cost, and its latency indefinitely for billing/audit, but delete the actual message content after 30 days," which this split makes directly expressible.

### 3.4 Context Propagation Across Async Boundaries

The hard case: a request enters an API gateway, gets pushed onto a queue, is picked up by a worker pool minutes later, and that worker fans out three tool calls to separate services. All of it must be one trace.

* **W3C Trace Context** (`traceparent` header) is the propagation format for synchronous hops (HTTP/gRPC).
* For queue-mediated hops (the request sits on Kafka/SQS before a worker picks it up), the trace context is serialized into the message envelope (a `trace_context` field alongside the payload), not inferred from wall-clock adjacency — this is what makes "queued for 4 minutes, then processed" show up as one trace with an accurate gap, rather than two disconnected traces.
* For fan-out (parallel tool calls), each child span carries the *same* parent span ID (the `agent.step` or `agent.run` span that dispatched them) so the UI can render them as siblings under one parent even though they execute concurrently on different workers.
* The SDK maintains an async-safe context var (`contextvars` in Python, `AsyncLocalStorage` in Node) so context propagates correctly through `async`/`await` chains without manual passing in the common case.

### 3.5 Sampling Strategies

| Strategy | When applied | Mechanism |
|---|---|---|
| **Head-based** | At trace start, before any work happens | SDK decides per-trace at creation: `sample = hash(trace_id) < rate`. Cheapest, but can't account for how the trace turns out (e.g., can't preferentially keep error traces). |
| **Tail-based** | After the full trace completes | Collector buffers spans for a trace until it completes (or times out), then decides: always keep errors, always keep traces above a latency/cost outlier threshold, sample the rest at the configured rate. Requires holding spans in memory/local buffer until the trace closes — bounded by a max trace duration timeout (default 10 min) after which the collector force-flushes what it has. |
| **Cost-aware** | Per span, at ingestion | Spans representing expensive LLM calls (large token counts) are sampled at a higher rate than cheap ones — an expensive mistake is more worth capturing fully than a cheap one, and cost outliers are exactly what teams want visibility into. |

### 3.5.1 OTel Semantic Convention Alignment

To keep the "extend, don't replace" commitment from §3.1 concrete, the table below shows how the platform's custom attributes sit alongside (never in conflict with) standard OTel semantic conventions a span might already carry from generic instrumentation (HTTP client spans, DB spans upstream/downstream of an LLM call):

| Standard OTel attribute | Still present/used as-is? | Platform-specific addition |
|---|---|---|
| `span.kind` (OTel's own `SpanKind` enum: `CLIENT`, `SERVER`, `INTERNAL`, ...) | Yes, set per OTel's normal rules (an `llm.call` span is `SpanKind.CLIENT`, since it's an outbound call to a provider) | `llm.call` etc. carried as a *separate* attribute (not overloading `SpanKind`), since OTel's `SpanKind` enum has no LLM-aware values and extending it would break generic OTel tooling that expects the standard enum |
| `http.status_code`, `http.method` | Yes, when the LLM call rides over HTTP (nearly always) | Complements, doesn't replace — `llm.stop_reason` captures the *model's* outcome (e.g., `max_tokens`), distinct from the *transport's* outcome (`http.status_code = 200`) |
| `service.name`, `service.version` | Yes, standard resource attributes identifying the emitting service | `agent_id`, `release_version` are platform-level concepts one layer above "which microservice" — an agent can span multiple services |
| `trace_id`, `span_id`, `parent_span_id` | Yes, unchanged — this is the whole point of building on OTel rather than replacing it | None — full reuse |
| Baggage / `traceparent` propagation | Yes, standard W3C Trace Context, per §3.4 | Extended baggage entries carry `tenant_id` and `agent_id` across service hops so downstream, non-LLM-aware services (a plain HTTP microservice a tool call hits) still tag their own ordinary spans with the right tenant for isolation purposes, without needing the platform SDK themselves |

Any existing OTel collector/exporter a team already runs (to Datadog, Honeycomb, Jaeger) continues to receive the same spans unmodified — the platform-specific attributes are simply additional key-value pairs those tools don't understand and safely ignore, which is what "extend, don't replace" means in practice, not just in principle.

Default policy actually deployed: **tail-based, 100% for errors and P99 latency/cost outliers, configurable base rate (default 10%) for everything else**, all overridable per tenant. This is the standard pattern from production tracing systems (Honeycomb, Datadog APM) applied here because the same rationale holds: the traces you'd sample away with head-based random sampling are disproportionately the ones you'd actually want when debugging a complaint.

```python
# Span Processor tail-sampling decision (simplified)
def should_keep(trace: BufferedTrace) -> bool:
    if trace.has_error:
        return True
    if trace.total_cost_usd > tenant_config.cost_outlier_threshold:
        return True
    if trace.total_latency_ms > tenant_config.latency_p99_threshold:
        return True
    return stable_hash(trace.trace_id) < tenant_config.base_sample_rate
```

### 3.6 Trace Storage: Columnar Store for Analytics

Spans land in **ClickHouse**, chosen over a document store (Mongo) or a general OLTP database (Postgres) because trace queries are overwhelmingly analytical: "P99 latency for `llm.call` spans on model X in the last 24h, grouped by prompt version." ClickHouse's columnar layout means a query touching 3 of 40 columns only reads those 3 columns off disk, and its native support for array/map columns fits span attributes without an EAV anti-pattern.

```sql
CREATE TABLE spans (
    trace_id        UUID,
    span_id         UUID,
    parent_span_id  Nullable(UUID),
    tenant_id       LowCardinality(String),
    span_kind       LowCardinality(String),   -- llm.call, tool.execute, ...
    span_name       String,
    start_time      DateTime64(3),
    end_time        DateTime64(3),
    duration_ms     UInt32,
    status          Enum8('ok' = 0, 'error' = 1, 'unset' = 2),
    -- LLM-specific, nullable for non-llm spans
    llm_provider        LowCardinality(Nullable(String)),
    llm_model            LowCardinality(Nullable(String)),
    llm_prompt_version_id Nullable(String),
    llm_input_tokens      Nullable(UInt32),
    llm_output_tokens     Nullable(UInt32),
    llm_cost_usd          Nullable(Float64),
    -- generic extension bag for less-common attributes
    attributes      Map(String, String),
    payload_refs    Map(String, String),      -- attribute name -> blob key
    agent_id        LowCardinality(String),
    user_id         Nullable(String),
    session_id      Nullable(String),
    environment     LowCardinality(String),
    release_version String
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(start_time)
ORDER BY (tenant_id, agent_id, start_time, trace_id)
TTL start_time + INTERVAL 90 DAY TO VOLUME 'cold',
    start_time + INTERVAL 400 DAY DELETE;
```

Partitioning by day + ordering by `(tenant_id, agent_id, start_time)` means the two most common query shapes — "this tenant's traffic in a time window" and "this specific agent's traffic" — both hit a small number of partitions/granules instead of a full scan.

### 3.7 SDK Instrumentation

**Python — automatic instrumentation via decorator/context manager:**

```python
from platform_sdk import trace, llm_call, tool

@trace.agent_run(agent_id="support-triage")
def handle_ticket(ticket: Ticket) -> Response:
    with trace.step(step_type="think"):
        plan = llm_call(
            prompt=sdk.get_prompt("support-triage-classifier", env="prod"),
            variables={"ticket_text": ticket.body},
        )   # emits an llm.call span automatically, attributes filled from the
            # provider response (tokens, cost, latency, stop_reason)

    with trace.step(step_type="act"):
        if plan.action == "search_kb":
            result = search_knowledge_base(ticket.body)   # @tool-decorated below
        ...
    return synthesize_response(plan, result)

@tool(name="search_knowledge_base")
def search_knowledge_base(query: str) -> list[Document]:
    # body of the tool; the decorator emits the tool.execute span,
    # capturing args/result/duration/success automatically
    return kb_client.search(query, top_k=8)
```

**TypeScript — equivalent shape:**

```typescript
import { traceAgentRun, traceStep, llmCall, tool } from "@platform/observability-sdk";

export const handleTicket = traceAgentRun({ agentId: "support-triage" }, async (ticket) => {
  const plan = await traceStep({ stepType: "think" }, () =>
    llmCall({
      prompt: await sdk.getPrompt("support-triage-classifier", { env: "prod" }),
      variables: { ticketText: ticket.body },
    })
  );

  const result = await traceStep({ stepType: "act" }, () =>
    plan.action === "search_kb" ? searchKnowledgeBase(ticket.body) : null
  );

  return synthesizeResponse(plan, result);
});

const searchKnowledgeBase = tool({ name: "search_knowledge_base" }, async (query: string) => {
  return kbClient.search(query, { topK: 8 });
});
```

Both SDKs propagate trace context via the language's native async-local mechanism (`contextvars` / `AsyncLocalStorage`, §3.4), so nested `llm_call`/`tool` invocations automatically attach as children of the enclosing `trace.step` without the caller manually threading a context object through every function signature — this is what makes "automatic instrumentation" actually low-friction to adopt, versus a manual-span-only API that most teams would under-instrument in practice.

### 3.8 Manual Instrumentation Escape Hatch

For call shapes the decorators can't capture (e.g., a call spread across a callback-based framework), a manual API is available:

```python
span = trace.start_span(kind="llm.call", name="fallback-classifier-call")
try:
    result = call_model(...)
    span.set_attributes({"llm.output_tokens": result.usage.output_tokens, ...})
finally:
    span.end()
```

Manual spans use the same attribute schema (§3.2) and are validated against it at ingestion — the Span Processor rejects (and counts, alertable) spans claiming `span.kind = "llm.call"` that are missing required attributes for that kind, catching instrumentation bugs early rather than silently admitting malformed data that would break downstream rollups.

---

## 4. Agent Trace Model

### 4.1 Hierarchical Span Model

```
AgentRun (run_id = "run_9f3e...")
 ├─ Step 0 (think)
 │   └─ LLMCall  (plan next action)
 ├─ Step 1 (act)
 │   ├─ ToolCall  (search_knowledge_base)          ─┐
 │   └─ ToolCall  (fetch_account_details)           ├─ parallel, same parent
 ├─ Step 1 (observe)                                ─┘
 │   └─ (merged tool results attached to state)
 ├─ Step 2 (think)
 │   └─ LLMCall  (decide: delegate to billing-agent)
 ├─ Step 2 (act) — delegation
 │   └─ AgentRun (sub_run_id = "run_a21c...", agent = "billing-specialist")
 │        ├─ Step 0 (think) → LLMCall
 │        ├─ Step 1 (act)   → ToolCall (refund_lookup)
 │        └─ Step 2 (act)   → final answer
 └─ Step 3 (respond) — final answer synthesized from sub-agent result
```

Every node above is a span; `AgentRun` and `Step` are themselves spans (kind `chain.run`-family / `agent.step`), not just a UI grouping construct — this is what lets "how many steps did runs of this agent average last week" be a straight SQL aggregate rather than an application-layer computation over reconstructed trees.

### 4.2 State Transitions

```
        ┌────────┐
   ─────►  idle   │
        └───┬────┘
            │ start
            ▼
        ┌────────┐      max_steps/timeout/budget exceeded
   ┌────► thinking├─────────────────────────────────┐
   │    └───┬────┘                                   │
   │        │ plan produced                           ▼
   │        ▼                                    ┌──────────┐
   │    ┌────────┐    tool/final-answer chosen    │  error   │
   │    │ acting ├────────┐                        └──────────┘
   │    └────────┘        │
   │                       ▼
   │                  ┌──────────┐
   │                  │observing │
   │                  └────┬─────┘
   │                       │ result fed back
   │        needs another step         final answer reached
   └───────────────────────┘                       │
                                                     ▼
                                                ┌────────┐
                                                │  done   │
                                                └────────┘
```

Each transition emits (or updates) the `agent.step` span with `agent.step_type` and closes the previous span. `done`, `error`, `max_steps_exceeded`, `timeout`, `cancelled`, and `budget_exceeded` are recorded as **distinct terminal states** on the `AgentRun` span (`agent.terminal_state` attribute) — this is what makes "how often do agents hit max-steps vs. genuinely finish" a queryable health metric instead of an anecdote.

### 4.3 Token and Cost Attribution / Roll-up

Roll-up is computed at write time by the Span Processor, not read time, so dashboard queries don't have to recursively walk a trace tree:

```python
def rollup_costs(trace: RawTrace) -> None:
    """Runs once per completed trace in the Span Processor before it's written."""
    for run_span in trace.agent_run_spans_bottom_up():   # leaf sub-agents first
        child_llm_cost = sum(s.llm_cost_usd for s in run_span.child_llm_calls())
        child_run_cost = sum(s.rolled_up_cost_usd for s in run_span.child_agent_runs())
        run_span.rolled_up_cost_usd = child_llm_cost + child_run_cost
        run_span.rolled_up_input_tokens = sum(
            s.llm_input_tokens for s in run_span.child_llm_calls()
        ) + sum(s.rolled_up_input_tokens for s in run_span.child_agent_runs())
        # same pattern for output_tokens
```

A top-level `AgentRun`'s `rolled_up_cost_usd` is therefore always exactly the sum of every LLM call anywhere beneath it, including inside delegated sub-agents — verified by an invariant check in the Span Processor's test suite (`sum(leaf llm.call costs) == root.rolled_up_cost_usd`, checked on a sample of writes continuously in production as a data-quality canary).

### 4.4 Visualization as a DAG

The trace UI renders two views from the same underlying span tree:

1. **Timeline/waterfall** — spans laid out on a time axis, showing overlap (parallel tool calls visibly concurrent) and gaps (queue wait time visible as whitespace).
2. **Graph/DAG** — nodes = spans, edges = parent→child and delegation relationships, laid out top-to-bottom; multi-agent delegation edges are visually distinguished (different edge style) from plain step-to-step sequencing so a reviewer can immediately see "this run called out to another agent" versus "this run just took another step."

Both views are generated client-side from one API response (`GET /v1/traces/{trace_id}`) that returns the full span tree with payload pointers resolved lazily on click, not eagerly — a 500-span agent run trace must not force-fetch 500 payload blobs to render the timeline.

### 4.5 Sequence Flow: Multi-Agent Delegation and Cost Roll-up

```
Supervisor Agent (Team A)        Model Gateway        Sub-Agent Runtime (Team B's billing-specialist)
       │                              │                            │
       │ think: decide to delegate    │                            │
       │─────────────────────────────►│                            │
       │◄─────────────────────────────│ (llm.call span #1, $0.02)  │
       │                              │                            │
       │ act: invoke sub-agent as a "tool"                          │
       │─────────────────────────────────────────────────────────►│
       │   (new AgentRun span opened, parent_span_id = supervisor's│
       │    current agent.step span — delegation edge recorded)    │
       │                              │                            │ think
       │                              │◄───────────────────────────│
       │                              │────────────────────────────► (llm.call span #2, $0.01)
       │                              │                            │ act: refund_lookup tool
       │                              │                            │────► (tool.execute span, $0)
       │                              │                            │ respond (final answer)
       │◄─────────────────────────────────────────────────────────│ AgentRun (Team B) closes,
       │   structured result returned as delegation "tool" result   │ rolled_up_cost_usd = $0.01
       │ observe: sub-agent result merged into state                │
       │ respond: synthesize final answer                           │
       │─────────────────────────────►│                            │
       │◄─────────────────────────────│ (llm.call span #3, $0.015) │
       │                                                            │
   AgentRun (Team A) closes.
   rolled_up_cost_usd = $0.02 (span #1) + $0.015 (span #3)
                       + $0.01 (Team B sub-run's rolled_up_cost_usd)
                       = $0.045
```

The supervisor's roll-up transparently includes the sub-agent's cost without needing to know anything about *how* Team B's agent is built internally — it only consumes Team B's `AgentRun.rolled_up_cost_usd`, which is exactly the interface boundary used to answer Exercise 7 in §20.

### 4.6 Common Query Patterns Against the Agent Trace Model

These are the queries the trace model (§3.6, §4.1-4.3) is explicitly shaped to answer cheaply — each is a straightforward aggregate over the flat `spans` table, not a recursive tree-walk, because rollups are computed at write time (§4.3):

```sql
-- "How often do agents hit max-steps vs. genuinely finish?" (terminal state health)
SELECT agent_id, agent_terminal_state, count() AS runs
FROM spans
WHERE span_kind = 'agent.run' AND start_time > now() - INTERVAL 7 DAY
GROUP BY agent_id, agent_terminal_state
ORDER BY agent_id, runs DESC;

-- "Show every step where the model's plan didn't match the action it took"
SELECT trace_id, agent_step_index, attributes['plan_action'] AS planned, attributes['taken_action'] AS actual
FROM spans
WHERE span_kind = 'agent.step'
  AND attributes['plan_action'] != attributes['taken_action']
  AND start_time > now() - INTERVAL 1 DAY;

-- "Total cost by agent, broken down by direct calls vs. sub-agent delegation"
SELECT
    agent_id,
    sumIf(llm_cost_usd, span_kind = 'llm.call') AS direct_llm_cost,
    sumIf(rolled_up_cost_usd, span_kind = 'agent.run' AND parent_span_id IS NOT NULL) AS delegated_cost
FROM spans
WHERE start_time > now() - INTERVAL 1 DAY
GROUP BY agent_id;

-- "P99 steps-per-run for a given agent, last 24h" (a proxy for runaway-loop risk)
SELECT agent_id, quantile(0.99)(agent_step_index) AS p99_steps
FROM spans
WHERE span_kind = 'agent.step' AND start_time > now() - INTERVAL 1 DAY
GROUP BY agent_id;
```

---

## 5. Prompt Versioning

### 5.1 Prompt as Code, Modeled as Data

A prompt version bundles everything needed to reproduce a call:

```python
@dataclass
class PromptVersion:
    id: str                      # content-addressed: "pv_" + sha256(content)[:12]
    prompt_name: str              # "support-triage-classifier"
    template: str                 # Jinja2/Mustache template
    variables_schema: dict        # JSON Schema for template variables
    model: str                    # "claude-sonnet-4-5"
    model_config: dict            # temperature, max_tokens, top_p, etc.
    few_shot_examples: list[dict] # bundled examples, versioned with the prompt
    created_by: str
    created_at: datetime
    parent_version_id: str | None # previous version this was edited from
    commit_message: str
```

### 5.2 Content-Addressable Storage

```python
def compute_version_id(template, variables_schema, model, model_config, few_shot) -> str:
    canonical = json.dumps({
        "template": template,
        "variables_schema": variables_schema,
        "model": model,
        "model_config": model_config,
        "few_shot_examples": few_shot,
    }, sort_keys=True)
    return "pv_" + hashlib.sha256(canonical.encode()).hexdigest()[:16]
```

Saving content identical to an existing version resolves to that version's ID instead of minting a duplicate — two editors independently reverting to the same wording don't fork history, and a CI job that re-registers an unchanged prompt on every deploy doesn't spam the version list.

### 5.3 Storage Model

```sql
CREATE TABLE prompt_versions (
    id              TEXT PRIMARY KEY,     -- content-addressed pv_...
    prompt_name     TEXT NOT NULL,
    template        TEXT NOT NULL,
    variables_schema JSONB NOT NULL,
    model           TEXT NOT NULL,
    model_config    JSONB NOT NULL,
    few_shot_examples JSONB NOT NULL DEFAULT '[]',
    parent_version_id TEXT REFERENCES prompt_versions(id),
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    commit_message  TEXT,
    tenant_id       TEXT NOT NULL
);

CREATE TABLE prompt_deployments (
    id              BIGSERIAL PRIMARY KEY,
    prompt_name     TEXT NOT NULL,
    environment     TEXT NOT NULL,        -- dev | staging | prod
    version_id      TEXT NOT NULL REFERENCES prompt_versions(id),
    traffic_pct     NUMERIC NOT NULL DEFAULT 100,   -- for canary splits
    deployed_by     TEXT NOT NULL,
    deployed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    rolled_back_from BIGINT REFERENCES prompt_deployments(id),
    tenant_id       TEXT NOT NULL,
    UNIQUE (prompt_name, environment, version_id)
);

-- current-live view: the row(s) with is_active = true per (prompt_name, environment)
CREATE TABLE prompt_deployment_active (
    prompt_name     TEXT NOT NULL,
    environment     TEXT NOT NULL,
    deployment_id   BIGINT NOT NULL REFERENCES prompt_deployments(id),
    tenant_id       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, prompt_name, environment, deployment_id)
);
```

Deployments are append-only, like versions; "current state" is a small pointer table (`prompt_deployment_active`) that's cheap to read and cheap to update atomically, so a deploy/rollback is one transaction touching a handful of rows, never a rewrite of history.

### 5.4 Diff View

```python
def diff_prompt_versions(v1: PromptVersion, v2: PromptVersion) -> PromptDiff:
    return PromptDiff(
        template_diff=unified_diff(v1.template.splitlines(), v2.template.splitlines()),
        config_diff={
            k: (v1.model_config.get(k), v2.model_config.get(k))
            for k in set(v1.model_config) | set(v2.model_config)
            if v1.model_config.get(k) != v2.model_config.get(k)
        },
        model_changed=(v1.model != v2.model),
        few_shot_diff=diff_examples(v1.few_shot_examples, v2.few_shot_examples),
    )
```

The UI renders `template_diff` as a standard side-by-side text diff and `config_diff` as a small changed-fields table — surfacing "temperature 0.3 → 0.7" is often the actual root cause of a quality regression, and burying it inside an unreadable full-object diff is a common real-world failure of naive prompt-versioning tools.

### 5.5 Deployment, Rollback, and Runtime Resolution

```
Deploy flow:
  1. Editor saves template/config change  → new PromptVersion (content-addressed)
  2. Editor (or CI) creates PromptDeployment(env="prod", version_id=new, traffic_pct=100)
  3. Registry flips prompt_deployment_active pointer atomically
  4. Edge caches (see 5.6) invalidated via pub/sub, refreshed within ~2s

Rollback flow:
  1. Operator clicks "rollback" on prod for prompt X
  2. New PromptDeployment row created pointing back to the previous version_id,
     with rolled_back_from = <the bad deployment's id>
     (rollback is itself a new deployment event — audit trail preserved, nothing is deleted)
  3. Same pointer-flip + cache-invalidation path as a normal deploy
```

Runtime resolution supports both modes required by 6.6 of the task:

```python
# Resolve "whatever's live in prod" — normal runtime use
prompt = sdk.get_prompt("support-triage-classifier", env="prod")

# Pin to an exact version — reproducible eval / debugging
prompt = sdk.get_prompt_version("pv_8f2a1c9d3e4f5678")
```

### 5.6 Hot-Path Read Performance (P99 ≤ 10 ms)

The Prompt Registry's Postgres primary is never queried per LLM call. Instead:

* Every gateway/worker node runs a **local edge cache** (in-process LRU, keyed by `(prompt_name, environment)`) populated on first miss and refreshed via a **pub/sub invalidation** (Redis pub/sub or a lightweight long-poll) the instant a deploy/rollback flips the active pointer.
* On registry unavailability, the edge cache serves the **last-known-good** version indefinitely (with a staleness metric emitted) rather than failing the caller's LLM call — a registry outage degrades to "you can't deploy new prompts," never to "you can't call your agent."
* Canary traffic splits (`traffic_pct < 100`) are resolved client-side in the SDK using a stable hash of a per-request key (user ID or session ID) against the deployed split, so the same user consistently lands in the same cohort for the duration of a canary — required for clean canary metric comparison (8.4).

### 5.7 SDK Usage Example

```python
# Normal runtime use — resolves whatever's live in prod, cached at the edge
prompt = sdk.get_prompt("support-triage-classifier", env="prod")
response = model_client.call(
    model=prompt.model,
    messages=prompt.render(variables={"ticket_text": ticket.body}),
    **prompt.model_config,
)

# Reproducible eval / debugging — pin to an exact version regardless of what's
# currently deployed
prompt = sdk.get_prompt_version("pv_8f2a1c9d3e4f5678")

# Registering a new version programmatically (e.g., from a CI job reading a
# prompt file out of the repo)
new_version = sdk.register_prompt_version(
    name="support-triage-classifier",
    template=open("prompts/support_triage.jinja").read(),
    variables_schema={"type": "object", "properties": {"ticket_text": {"type": "string"}}},
    model="claude-sonnet-4-5",
    model_config={"temperature": 0.2, "max_tokens": 512},
    commit_message="Tighten classification categories per support team feedback",
)
sdk.deploy(name="support-triage-classifier", env="prod", version_id=new_version.id, traffic_pct=10)
```

### 5.8 Prompt Registry as the Reproducibility Backbone

Every downstream system that claims reproducibility (offline eval runs, §7; canary comparisons, §8; regression evidence, §11) does so by storing a `prompt_version_id`, never a resolved template string — the version ID is the join key that lets an `EvalRun` from six months ago be traced back to the *exact* template, model config, and few-shot examples used, even after the prompt has since been edited dozens of times. This is the same content-addressing discipline applied to datasets (§6) and judge configs (§9.1), deliberately kept consistent across all three so a single mental model ("everything the platform reasons about is a pinned, immutable version") applies everywhere rather than each subsystem inventing its own versioning story.

---

## 6. Dataset Management

### 6.1 Dataset Tiers

| Tier | Source | Trust level | Used for |
|---|---|---|---|
| **Golden** | Hand-curated by domain experts, reviewed | High | Release gating, CI eval gates |
| **Silver** | Production-sampled + lightly reviewed, or weak-labeled | Medium | Broader regression coverage, judge calibration candidates |
| **Production-sampled** | Raw traces, filtered but unlabeled | Unlabeled until annotated | Candidate pool for promotion; online eval baseline |

### 6.2 Schema

```sql
CREATE TABLE datasets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    tier            TEXT NOT NULL CHECK (tier IN ('golden','silver','production_sampled')),
    tenant_id       TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    description     TEXT
);

CREATE TABLE dataset_versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id      UUID NOT NULL REFERENCES datasets(id),
    version_number  INT NOT NULL,
    example_count   INT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      TEXT NOT NULL,
    change_summary  TEXT,
    UNIQUE(dataset_id, version_number)
);

CREATE TABLE dataset_examples (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_version_id UUID NOT NULL REFERENCES dataset_versions(id),
    input             JSONB NOT NULL,
    expected_output   JSONB,             -- nullable: reference-free tasks use grading_criteria instead
    grading_criteria  TEXT,              -- rubric text for reference-free eval
    metadata          JSONB NOT NULL DEFAULT '{}',   -- tags, difficulty, source_trace_id
    split             TEXT NOT NULL DEFAULT 'eval' CHECK (split IN ('train','eval','test')),
    source_trace_id   UUID,              -- provenance if pulled from production
    added_by          TEXT NOT NULL,
    added_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

A `dataset_version` is immutable once created; edits create a new version referencing a new set of `dataset_examples` rows (examples themselves are also immutable — an "edit" is really "remove reference, add a new example row"). This mirrors the prompt-versioning philosophy deliberately: every artifact an eval run depends on is content-addressed or version-pinned, so an `EvalRun` record fully determines what was run without ambiguity.

### 6.3 Production Data Collection Pipeline

```python
# Runs continuously as a Span Processor consumer / scheduled job
class ProductionSamplingRule:
    agent_id: str
    filter: str          # e.g. "user_feedback.rating <= 2 OR judge_score.faithfulness < 0.6"
    target_dataset: str  # "support-triage-candidates"
    sample_rate: float   # for the random-sample component, independent of the filter hits

def collect_candidates(rule: ProductionSamplingRule, window: TimeRange):
    traces = trace_store.query(agent_id=rule.agent_id, filter=rule.filter, window=window)
    for t in traces:
        dataset_service.add_candidate_example(
            dataset=rule.target_dataset,
            input=t.input_ref,
            metadata={"source_trace_id": t.trace_id, "collected_reason": rule.filter},
            status="pending_review",     # requires human promotion before entering golden/silver
        )
```

Candidates never enter a golden dataset automatically — they land in a `pending_review` queue that feeds directly into the annotation workflow (6.4). This is a deliberate gate: without it, a systematically-biased judge or a spam feedback wave could poison the very datasets used to catch regressions.

### 6.4 Annotation Workflow Integration

Dataset example promotion from `pending_review` uses the same annotation queue infrastructure as human feedback labeling (detailed in §10.2) — an annotator is shown the candidate's input/output, asked to confirm/edit the expected output or rubric, and their decision either promotes the example (with their edits) into the target dataset version or discards it. Routing (round-robin vs. skill-based), completion tracking, and second-review requirements for golden-tier promotions are identical to the general annotation queue mechanism, not a separate implementation.

### 6.5 Worked Example: Building a Golden Set from Production

```python
# 1. Define a sampling rule targeting low-confidence and low-rated traces
rule = ProductionSamplingRule(
    agent_id="support-triage",
    filter="user_feedback.rating <= 2 OR judge_score.faithfulness < 0.6",
    target_dataset="support-triage-candidates",
    sample_rate=0.05,   # plus a 5% random sample, to catch failure modes no
                         # filter anticipated
)
dataset_service.register_sampling_rule(rule)

# 2. Nightly job pulls matching traces into "pending_review"
#    (implementation in §6.3)

# 3. Annotators work the queue (§10.2); each task shows:
#    - the original input, retrieved context, and model output
#    - the triggering signal (which filter matched, or "random sample")
#    - a form to confirm/edit expected_output or write a grading_criteria rubric

# 4. Weekly promotion job: examples with 2 concurring annotator results and
#    no unresolved adjudication move into a new dataset_version of the
#    "support-triage-golden" dataset
promoted = dataset_service.promote_reviewed_candidates(
    source="support-triage-candidates",
    target="support-triage-golden",
    require_second_review=True,
)
print(f"Promoted {promoted.count} examples into version {promoted.new_version_number}")
```

### 6.6 Dataset Export / Import

```
GET  /v1/datasets/{id}/versions/{v}/export?format=jsonl
     → streams the version's examples as JSONL for offline analysis,
       notebook use, or handoff to a fine-tuning pipeline (subject to the
       separate consent gate described in §10.6 if destined for training)

POST /v1/datasets/{id}/versions/import
     → bulk-create a new version from an uploaded JSONL/CSV, used for
       teams migrating an existing hand-built eval set into the platform
```

---

## 7. Offline Evaluation Engine

### 7.1 Pipeline Architecture

```
                    ┌─────────────────┐
   EvalRun request  │  Eval Scheduler  │
   (dataset_version, │  (queues jobs,   │
    prompt_version,  │   tracks progress)│
    model, metrics)  └────────┬─────────┘
                               │  fan out, N examples
                               ▼
                    ┌─────────────────────┐
                    │  Worker Pool (K8s Job,│      each worker:
                    │  horizontally scaled) │  1. render prompt w/ example.input
                    └────────┬─────────────┘      2. call model → output
                               │                    3. run each configured metric
                               ▼                    4. write EvalResult row
                    ┌─────────────────────┐
                    │   EvalResult store    │
                    │   (Postgres + S3 for  │
                    │    full transcripts)  │
                    └────────┬─────────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  Comparison / Report  │
                    │  Generator (stats,    │
                    │  significance tests)  │
                    └─────────────────────┘
```

Each example is an independent unit of work — the worker pool scales horizontally with zero coordination between workers beyond pulling from a shared job queue, and a crashed worker's in-flight examples are simply re-queued (idempotent: re-running example N just overwrites its `EvalResult` row, keyed by `(eval_run_id, example_id)`).

### 7.2 Throughput Arithmetic (30-minute SLA for 10,000 examples)

```
Budget: 30 min = 1800 s for 10,000 examples
Per-example wall time (generation + metrics): ~4s average
  (1 model call ~2.5s + 1-3 metric computations ~1.5s, some parallel)

Required parallelism = 10,000 examples * 4s / 1800s ≈ 22 concurrent workers minimum

Provisioned: 40 workers (headroom for slower examples, retries, judge-metric latency spikes)
Each worker rate-limited to respect the target model's TPM/RPM quota —
  coordinate via the same rate limiter the production Model Gateway uses,
  so a large eval run cannot itself trip the account's provider rate limit
  and start throttling production traffic sharing that quota.
```

### 7.3 Metric Types

| Metric | Type | Computation | Cost |
|---|---|---|---|
| Exact / fuzzy match | Reference-based | String equality / edit distance vs. `expected_output` | Free, instant |
| BLEU / ROUGE | Reference-based | N-gram overlap vs. reference | Free, instant |
| Embedding similarity | Reference-based | Cosine similarity of embeddings of output vs. reference | Cheap (1 embedding call) |
| Faithfulness | Reference-free (RAG) | LLM judge: is every claim in the output supported by retrieved context? | LLM judge call |
| Relevance | Reference-free | LLM judge: does output address the input? | LLM judge call |
| Toxicity / safety | Reference-free | Classifier model or LLM judge against policy categories | Cheap classifier or judge call |
| Latency | Operational | Measured directly from the eval call | Free |
| Cost | Operational | Computed from token counts × pricing table | Free |
| Custom | Either | Tenant-registered Python function, arbitrary logic | Varies |

```python
class Metric(Protocol):
    name: str
    def compute(self, example: DatasetExample, output: ModelOutput) -> MetricResult: ...

class ExactMatch(Metric):
    name = "exact_match"
    def compute(self, example, output):
        return MetricResult(score=float(output.text.strip() == example.expected_output.strip()))

class FaithfulnessJudge(Metric):
    name = "faithfulness"
    def __init__(self, judge_config: JudgeConfig): ...
    def compute(self, example, output):
        return judge_service.score(
            judge_config=self.judge_config,
            context=example.metadata["retrieved_context"],
            response=output.text,
        )
```

### 7.4 Comparison Reports

```python
def compare_eval_runs(run_a: EvalRun, run_b: EvalRun) -> ComparisonReport:
    report = ComparisonReport(run_a=run_a.id, run_b=run_b.id)
    for metric_name in run_a.metrics:
        scores_a = [r.metric_scores[metric_name] for r in run_a.results]
        scores_b = [r.metric_scores[metric_name] for r in run_b.results]
        stat, p_value = mannwhitneyu(scores_a, scores_b, alternative="two-sided")
        report.add(
            metric=metric_name,
            mean_a=mean(scores_a), mean_b=mean(scores_b),
            delta=mean(scores_b) - mean(scores_a),
            p_value=p_value,
            significant=p_value < 0.05,
        )
    return report
```

Reports never present a bare delta without significance — a comparison UI that says "faithfulness 0.91 → 0.89" without a p-value invites exactly the false-alarm/false-confidence pattern this platform exists to prevent.

### 7.5 CI/CD Integration (Eval Gates)

```yaml
# .ci/eval-gate.yaml
eval_gate:
  dataset: "support-triage-golden-v12"
  prompt_under_test: "${GIT_BRANCH_PROMPT_VERSION}"
  compare_against: "prod"          # resolves to whatever's currently deployed
  metrics:
    - name: faithfulness
      min_mean: 0.85
      max_regression_vs_baseline: 0.03   # fail if worse than baseline by more than this, with p<0.05
    - name: exact_match
      min_mean: 0.70
    - name: cost_usd
      max_mean: 0.02
  on_fail: block_merge
```

```
CI job:
  1. Register candidate PromptVersion from the branch's prompt file
  2. Trigger EvalRun(dataset=golden-v12, prompt=candidate, model=<same as prod>)
  3. Trigger EvalRun(dataset=golden-v12, prompt=<currently deployed prod version>, model=<same>)
     (skip if a recent cached baseline run against the same dataset version exists — avoid
      re-spending on an unchanged baseline for every PR)
  4. Compare, apply eval_gate.yaml thresholds
  5. Exit non-zero + post comparison report as a PR comment if any gate fails
```

### 7.6 Sequence Flow: CI Eval Gate

```
Developer     CI Pipeline        Eval Scheduler       Worker Pool       Comparison Generator
    │              │                    │                   │                    │
    │ open PR       │                    │                   │                    │
    │──────────────►│                    │                   │                    │
    │              │ register candidate  │                   │                    │
    │              │ PromptVersion       │                   │                    │
    │              │───────────────────────────────────────────────────────────────► (Prompt Registry, §5)
    │              │ submit EvalRun(A)   │                   │                    │
    │              │ candidate vs golden │                   │                    │
    │              │────────────────────►│ fan out 10k jobs  │                    │
    │              │                    │──────────────────►│ generate+score     │
    │              │                    │◄──────────────────│ (parallel, §7.2)   │
    │              │ (reuse cached       │                   │                    │
    │              │  baseline run if    │                   │                    │
    │              │  recent one exists) │                   │                    │
    │              │ GET comparison       │                   │                    │
    │              │───────────────────────────────────────────────────────────►│
    │              │◄───────────────────────────────────────────────────────────│
    │              │ apply eval_gate.yaml thresholds                             │
    │  PR comment   │ pass → allow merge; fail → block, attach report            │
    │◄──────────────│                   │                   │                    │
```

### 7.6.1 Custom Metric Registration

Teams register domain-specific metrics through the same `Metric` interface used internally (§7.3):

```python
from platform_sdk.eval import Metric, MetricResult, register_metric

class RefundAmountAccuracy(Metric):
    """Domain-specific: does the agent's stated refund amount match policy
    for this ticket's product tier, independent of wording?"""
    name = "refund_amount_accuracy"

    def compute(self, example: DatasetExample, output: ModelOutput) -> MetricResult:
        stated_amount = extract_dollar_amount(output.text)
        correct_amount = policy_lookup(example.metadata["product_tier"])
        return MetricResult(
            score=float(stated_amount == correct_amount),
            details={"stated": stated_amount, "expected": correct_amount},
        )

register_metric(RefundAmountAccuracy(), tenant_id="support-team")
```

Custom metrics run through the exact same worker pool, comparison-report, and CI-gate machinery as built-in metrics (§7.1, §7.4, §7.5) — there is no second-class "custom metric" execution path, which is what lets a team's eval gate mix a built-in `faithfulness` judge score with a hand-written business-logic check in one `eval_gate.yaml` (§7.5) without special-casing either.

### 7.7 Worked Numeric Example: Interpreting a Comparison Report

```
Metric          Mean (prod, A)   Mean (candidate, B)   Delta    p-value   Verdict
faithfulness    0.91             0.87                  -0.04    0.003     REGRESSION (blocks merge:
                                                                            exceeds max_regression_vs_baseline=0.03)
exact_match     0.72             0.74                   0.02    0.21      no significant change
cost_usd        0.018            0.011                 -0.007   <0.001    significant improvement
                                                                            (not gated, informational)
latency_p50_ms  1100             950                    -150    0.04      significant improvement
```

Even though cost and latency both improved (the candidate uses a cheaper/faster model config), the CI gate still blocks the merge on the faithfulness regression — a worked illustration of why every tracked metric needs its own threshold rather than a single blended "quality score" that a cost win could mask a quality loss inside.

---

## 8. Online Evaluation

### 8.1 Sampling Architecture

```python
class OnlineEvalConfig:
    agent_id: str
    cheap_checks_sample_rate: float = 1.0     # schema, PII, format — nearly free, sample everything
    judge_checks_sample_rate: float = 0.02    # expensive — 2% default
    checks: list[CheckConfig]

# Sampling decision made by the Span Processor as a trace completes
def maybe_enqueue_online_eval(trace: CompletedTrace, config: OnlineEvalConfig):
    if stable_hash(trace.trace_id) < config.cheap_checks_sample_rate:
        cheap_check_queue.publish(trace.trace_id)
    if stable_hash(trace.trace_id, salt="judge") < config.judge_checks_sample_rate:
        judge_check_queue.publish(trace.trace_id)
```

Two separate queues with separate consumer pools because cheap checks (schema validation, regex/PII scan, banned-content match) run in milliseconds and can afford near-100% sampling, while judge checks are rate-limited by judge-model quota and cost — mixing them in one queue would let a burst of judge work starve latency-sensitive cheap checks, or force cheap checks down to judge-affordable sampling rates unnecessarily.

### 8.2 Async Evaluation Pipeline

```
Trace completes → Span Processor → (sampling decision) → Eval Queue
                                                                │
                                                                ▼
                                                     Online Eval Worker
                                                     1. fetch trace + payloads
                                                     2. run configured checks
                                                     3. write EvalResult, keyed to trace_id
                                                     4. emit metric point to TSDB
                                                     5. if score below threshold: emit event
                                                        to Regression Detector / Alert Manager
```

Never touches the request path — the worker consumes from a queue populated *after* the trace already completed and was returned to the user. Attaching a score after the fact (P99 ≤ 60s per the NFRs) is acceptable because online eval's purpose is fleet-level quality monitoring and canary comparison, not per-request gating (that's what synchronous guardrails in the application layer are for, out of scope here).

```python
class OnlineEvalWorker:
    """Consumes from judge_check_queue; one instance per pool replica."""

    def run(self):
        for trace_id in self.queue.consume():
            try:
                trace = trace_store.fetch(trace_id, hydrate_payloads=True)
                config = online_eval_config_cache.get(trace.agent_id)
                results = []
                for check in config.checks:
                    result = check.run(trace)          # each check independent;
                    results.append(result)               # one slow judge check
                                                           # doesn't block others
                eval_result_store.write(trace_id, results)
                metrics_tsdb.emit_batch([
                    MetricPoint(name=r.metric_name, value=r.score,
                                agent_id=trace.agent_id,
                                prompt_version_id=trace.llm_prompt_version_id,
                                tenant_id=trace.tenant_id, ts=now())
                    for r in results
                ])
                for r in results:
                    if r.score < config.threshold_for(r.metric_name):
                        regression_detector.notify(trace_id, r)   # §11 evaluates
                                                                    # whether this is
                                                                    # noise or signal
            except JudgeServiceUnavailable:
                self.queue.nack_and_requeue(trace_id, backoff=exponential)
            except TraceNotFound:
                self.queue.ack(trace_id)   # trace expired/redacted before we got
                                            # to it — not retryable, log and move on
```

The `except JudgeServiceUnavailable` path matters operationally: online eval is explicitly allowed to fall behind or temporarily stop during a judge-provider outage (§16.3-adjacent scenario) without that outage cascading into ingestion or dashboard availability — the queue absorbs backlog and drains once the judge dependency recovers, another instance of the async-path degradation posture from §2.3.

### 8.3 Cheap vs. Expensive Checks

| Check | Type | Latency | Sampling |
|---|---|---|---|
| JSON schema / output format validation | Cheap | ~5 ms | 100% |
| PII leakage regex/NER scan | Cheap | ~20 ms | 100% |
| Banned-content keyword/embedding match | Cheap | ~15 ms | 100% |
| Latency/cost threshold check | Cheap | ~1 ms (already in span) | 100% |
| LLM-judge relevance/faithfulness score | Expensive | 1–4 s | 1–5% (tunable) |
| Embedding-similarity drift check | Medium | ~50 ms | 10–20% |

### 8.4 Canary Evaluation

```
Deploy prompt version B to 10% of "support-triage" prod traffic (control = version A, 90%)
  → SDK's stable-hash traffic split (5.6) routes requests
  → Every trace tagged with prompt_version_id = A or B
  → Online Eval scores both cohorts identically (same checks, same sample rate)
  → Canary Comparison job runs every 15 min:
      - pulls last N hours of EvalResults for cohort A and cohort B
      - runs the same significance-tested comparison as offline (§7.4),
        but on a rolling window instead of a fixed dataset
      - requires a minimum sample size per cohort (configurable, default 200)
        before declaring any verdict — too few samples → "inconclusive," not "pass"
```

### 8.5 Automated Promotion / Rollback

```
Canary verdict logic (evaluated every 15 min while a canary is active):

  if any tracked metric regressed with p < 0.01 AND effect size > tenant's min_effect_size:
      → AUTO ROLLBACK (if tenant opted in) — revert prod pointer to version A,
        create incident record with the triggering comparison attached
  elif all tracked metrics non-regressed (p >= 0.05 for "no difference" OR improved)
       AND minimum sample size reached AND minimum canary duration elapsed (default 2h):
      → ELIGIBLE FOR PROMOTION — surfaced to a human for one-click promote to 100%,
        or auto-promoted if tenant opted into full automation
  else:
      → CONTINUE CANARY (inconclusive, keep collecting)
```

Auto-rollback is opt-in per tenant, never a platform-wide default — some teams want a human in the loop for any prod prompt change regardless of what the stats say, and the platform should not override that.

### 8.6 Sequence Flow: Canary Deployment End-to-End

```
Operator      Prompt Registry     SDK (traffic split)    Online Eval        Canary Comparison Job     Regression Detector
   │                │                     │                   │                       │                        │
   │ deploy v2 @10%  │                     │                   │                       │                        │
   │───────────────►│ (§5.4/5.8 flow)     │                   │                       │                        │
   │                │────────────────────►│ hash(user_id)      │                       │                        │
   │                │                     │ < 0.10 → v2, else A│                       │                        │
   │                │                     │  (per request)     │                       │                        │
   │                │                     │  tag trace with     │                       │                        │
   │                │                     │  prompt_version_id  │                       │                        │
   │                │                     │────────────────────►│ sample + score        │                        │
   │                │                     │                    │ (cheap 100%,           │                        │
   │                │                     │                    │  judge 2%, §8.1-8.3)   │                        │
   │                │                     │                    │──────────────────────►│ every 15 min:          │
   │                │                     │                    │                       │ pull cohort A vs B      │
   │                │                     │                    │                       │ results, run sig. test  │
   │                │                     │                    │                       │─────────────────────────►│
   │                │                     │                    │                       │                        │ verdict:
   │                │                     │                    │                       │                        │ REGRESSION,
   │                │                     │                    │                       │                        │ PROMOTE-ELIGIBLE,
   │                │                     │                    │                       │                        │ or CONTINUE
   │  page/alert if  │                     │                    │                       │                        │
   │  REGRESSION      │◄──────────────────────────────────────────────────────────────────────────────────────│
   │◄────────────────│                     │                    │                       │                        │
   │  (or auto-       │ rollback flow      │                    │                       │                        │
   │   rollback if    │ (§5.8) if opted-in │                    │                       │                        │
   │   opted in)       │                     │                    │                       │                        │
```

### 8.7 Worked Numeric Example: Canary Sample-Size Timeline

```
Agent traffic: 5,000 req/day, canary split 10% → ~500 canary requests/day
Cheap checks: 100% of canary sample → 500 cheap-check results/day
Judge checks: 20% of canary sample (boosted above the platform default 2%
  specifically during an active canary, since canary decisions matter more
  than steady-state monitoring) → 100 judge-scored results/day

Minimum sample size for the tracked judge metric (power analysis for a
medium effect size, alpha=0.01 per §11.6): ~180 per cohort

Time to reach minimum sample size: 180 / 100 per day ≈ 1.8 days minimum,
  plus the configured minimum canary duration floor (2h) — in practice the
  sample-size requirement dominates for this traffic level, illustrating
  why Exercise 4 (§20) asks for a design that shortens this for low-volume
  agents.
```

---

## 9. LLM-as-Judge

### 9.1 Judge Configuration as a Versioned Prompt

```python
@dataclass
class JudgeConfig:
    id: str                    # content-addressed, like PromptVersion
    name: str                  # "faithfulness-judge-v3"
    judge_model: str           # "claude-haiku-4-5" — deliberately allowed to differ from the app's model
    rubric_template: str       # the judge's own prompt template
    criteria: list[str]        # ["relevance", "faithfulness", "helpfulness", "safety"]
    scoring_scale: str         # "binary" | "1-5" | "0-1_continuous"
    output_schema: dict        # forces structured judge output (JSON mode / tool-call forced)
    calibration_score: float | None   # kappa vs. human labels, updated on recalibration
    calibrated_at: datetime | None
```

```
Example rubric_template (abbreviated):

  You are evaluating whether a support agent's RESPONSE is faithful to the
  provided CONTEXT. A response is faithful if every factual claim in it is
  directly supported by the context. It is not faithful if it states
  anything not present in or contradicted by the context, even if true in
  general.

  CONTEXT: {{ context }}
  RESPONSE: {{ response }}

  Score faithfulness on a 1-5 scale:
  1 = response contains claims directly contradicted by context
  3 = response contains claims not supported by context but not contradicted
  5 = every claim in response is directly supported by context

  Output as JSON: {"score": <int 1-5>, "unsupported_claims": [<string>, ...], "reasoning": "<string>"}
```

### 9.2 Multi-Criteria Scoring: Single-Pass vs. Per-Criterion

| Approach | Cost | Consistency risk |
|---|---|---|
| **Single judge call, multiple criteria in one prompt/schema** | 1 call per example (cheap) | Criteria can leak/influence each other (e.g., a judge primed to think about safety may downgrade relevance scores it wouldn't otherwise) |
| **Separate judge call per criterion** | N calls per example (N× cost) | Each score is independent and more reliable, but N× the latency and spend |

Default: **single-pass for online/high-volume sampling** (cost-bound), **per-criterion for offline golden-set gating** (accuracy-bound, lower volume). This mirrors the judge-model-selection trade-off in 9.4 — cheap-and-fast where volume is high, expensive-and-accurate where the decision matters most (a release gate).

**Example single-pass multi-criteria judge output**, using the `output_schema` from a `JudgeConfig` that scores four dimensions in one call:

```json
{
  "relevance": {"score": 5, "reasoning": "Directly answers the user's refund-window question."},
  "faithfulness": {"score": 2, "reasoning": "States a 30-day window; retrieved context specifies 14 days for this product tier.", "unsupported_claims": ["30-day refund window"]},
  "helpfulness": {"score": 4, "reasoning": "Clear, actionable, but the factual error undermines it."},
  "safety": {"score": 5, "reasoning": "No policy violations."}
}
```

This is the kind of disagreement across criteria — high relevance and helpfulness, low faithfulness — that a single blended score would hide, and it's exactly the shape of finding that should route the example into the production-sampling candidate pool (§6.3, filter: `judge_score.faithfulness < 0.6`) for human review and, if confirmed, a golden-set addition or a prompt fix.

### 9.3 Judge Calibration

```python
def calibrate_judge(judge_config: JudgeConfig, calibration_set: Dataset) -> float:
    """calibration_set: examples with a human-assigned score for the same criterion."""
    human_scores, judge_scores = [], []
    for example in calibration_set.examples:
        human_scores.append(example.metadata["human_label"])
        judge_scores.append(judge_service.score(judge_config, example).score)
    kappa = cohen_kappa_score(bucketize(human_scores), bucketize(judge_scores))
    judge_config.calibration_score = kappa
    judge_config.calibrated_at = now()
    return kappa
```

* Calibration re-runs whenever `judge_config` changes (new version_id) — a judge is never trusted "by default" after an edit, it re-earns its calibration score.
* Judges with kappa below a configurable trust threshold (default 0.4, "fair agreement" per the standard Landis & Koch scale) are flagged in the UI with a visible warning wherever their scores are shown, and cannot be used as a CI eval gate criterion until recalibrated above threshold.
* Calibration sets are themselves golden-tier datasets, maintained with the same rigor as any other golden dataset (§6.1) — the calibration is only as good as the human labels it's checked against.

### 9.4 Judge Model Selection

| Use case | Judge model class | Rationale |
|---|---|---|
| Online sampling (1-5% of high-volume prod traffic) | Cheap/fast (e.g., a small/distilled model) | Cost must scale sub-linearly with traffic; volume is high enough that noise averages out across many samples |
| Offline CI gate (golden dataset, every PR) | Expensive/high-accuracy (e.g., a frontier model) | Low volume (hundreds of examples), decision has real consequence (blocks a merge), accuracy matters more than per-call cost |
| Canary comparison | Matches the online sampling judge | Must be consistent across the control/canary cohorts being compared — switching judges mid-comparison invalidates the comparison |

### 9.4.1 Judge Prompt Anti-Patterns

Judge rubrics are prompts, and prompts are easy to write badly in ways that specifically undermine a judge's usefulness. The platform's judge-authoring UI surfaces linting warnings for the most common failures observed in practice:

| Anti-pattern | Why it breaks calibration |
|---|---|
| Vague scoring anchors ("rate the quality 1-10") with no description of what each number means | Different judge model calls (and different human labelers) anchor the scale differently — kappa against human labels stays low no matter how good the underlying model is, because the *rubric*, not the model, is the source of noise |
| Asking the judge to score criteria that trade off against each other in one unstructured free-text response instead of a forced schema | Output parsing becomes unreliable, and criteria bleed into each other (§9.2) worse than in a schema-forced multi-criteria call |
| Using the same model and near-identical framing as the application's own generation prompt | Self-preference bias — a model judging its own family's output style tends to score it more favorably than an independent judge would, inflating scores without inflating actual quality |
| No explicit handling of "not applicable" | Forces a score on criteria that don't apply to a given example (e.g., scoring "faithfulness to context" for a query with no retrieval step), corrupting the aggregate mean with meaningless data points |
| Rubric written once and never revisited after calibration drift is detected (§16.3) | Calibration is a point-in-time measurement; a rubric with no owner and no re-calibration cadence silently becomes untrustworthy exactly when a team has come to rely on it most |

### 6.7 PII and Compliance in Datasets

Dataset examples inherit the source trace's redaction policy (§3.3.1) at the moment they're sampled into a `pending_review` candidate (§6.3) — a production trace redacted per its tenant's policy does not get "un-redacted" by virtue of being promoted into a golden dataset, since golden datasets are frequently the most widely-shared, longest-retained artifact in the whole platform (used across many eval runs, potentially referenced in fine-tuning exports per §10.6). A tenant wanting fuller content in their eval datasets than their production redaction policy allows must explicitly author synthetic or manually-reviewed examples rather than relying on production sampling to bypass their own retention policy — the dataset pipeline is not a backdoor around the redaction policy that governs the exact same content elsewhere in the platform.

### 9.5 Cost Management

* **Response caching**: identical `(judge_config_id, input_hash)` pairs are cached — the same production trace scored by two overlapping eval runs (e.g., a nightly regression scan and an ad-hoc investigation) is judged once, not twice.
* **Batching**: judge calls for an offline eval run are submitted through the provider's batch API where available (typically ~50% cheaper, with a latency trade-off acceptable for offline/async workloads).
* **Hard budget ceilings**: every `EvalRun` and every tenant's online-eval judge spend has a daily cap; hitting it pauses further judge calls (with an alert), rather than continuing to spend — the same posture as the LLM Gateway's per-tenant budget enforcement in a related design.
* **Sampling before judging, not after**: the sampling decision (§8.1) happens before a judge is invoked, never "judge everything then discard most results" — an easy naive-implementation mistake that pays full judge cost for the discarded majority.

### 9.6 Judge Versioning and Judge Regression

Judge configs are versioned exactly like production prompts (content-addressed, immutable, deployable). A `JudgeConfig` change is itself subject to the Regression Detector: if a new judge version starts scoring the *same held-out calibration set* meaningfully differently than the previous version, that's flagged before the new judge is allowed to become the "active" judge for any gate — a judge silently drifting is a known real-world failure mode (provider swaps the underlying model version under a fixed model string) and must be caught the same way a production prompt regression is caught.

### 9.7 Batched Judging for Cost Efficiency

```python
class JudgeService:
    def score_batch(self, judge_config: JudgeConfig, items: list[JudgeInput]) -> list[JudgeResult]:
        # 1. Dedup: identical (judge_config.id, content_hash) pairs resolved from cache
        cached, uncached = self._partition_by_cache(judge_config, items)

        # 2. For offline/async workloads, submit uncached items through the
        #    provider's batch API (typically ~50% cheaper, minutes-to-hours
        #    turnaround — acceptable for offline eval, not for online sampling)
        if self.mode == "offline":
            batch_job = provider_client.batch.submit(
                requests=[self._build_request(judge_config, i) for i in uncached]
            )
            results = provider_client.batch.wait(batch_job.id)
        else:
            # online: synchronous calls through the standard Model Gateway,
            # rate-limited against the judge model's live quota
            results = [self._call_sync(judge_config, i) for i in uncached]

        self._write_cache(judge_config, uncached, results)
        return merge_in_order(cached, results, items)
```

### 9.8 Judge Cost Comparison Table (Illustrative)

| Judge model class | Cost / call (relative) | Typical calibration kappa achieved | Recommended use |
|---|---|---|---|
| Small/distilled model | 1x (baseline) | 0.35 - 0.50 | High-volume online sampling where cost dominates and moderate agreement suffices given large sample sizes |
| Mid-tier model | ~5x | 0.50 - 0.65 | Canary comparisons, silver-tier dataset scoring |
| Frontier model | ~20x | 0.65 - 0.80+ | Golden-set CI gates, judge calibration-set labeling assistance (never a replacement for human labels on the calibration set itself) |

A judge below the tenant-configured trust threshold (default kappa 0.4, §9.3) is barred from CI-gating use regardless of which model class produced it — the class-to-kappa mapping above is illustrative and must be re-measured per task/domain, not assumed from the model tier alone.

---

## 10. Human Feedback System

### 10.1 Feedback Collection SDK

```typescript
// TypeScript SDK — inline widget usage
import { Feedback } from "@platform/observability-sdk";

<Feedback
  traceId={currentTraceId}
  type="thumbs"                       // thumbs | likert-5 | free-text | multi-dimension
  onSubmit={(value) => feedbackClient.submit({ traceId: currentTraceId, value })}
/>
```

```python
# Server-side / backend submission (e.g., internal reviewer tool, or a support agent
# correcting a bot's answer)
feedback_client.submit(
    trace_id="tr_9f3e2a",
    feedback_type="correction",
    value={"corrected_output": "The refund window is 30 days, not 14."},
    submitted_by="agent_jsmith",
    role="internal_reviewer",
)
```

Every feedback submission carries `trace_id` (mandatory — feedback with no traceable origin is not accepted, since it can't be attributed back to a prompt version or attached to a dataset), `role` (end_user | internal_reviewer | domain_expert — used for weighting during aggregation), and a `feedback_type` discriminator.

### 10.2 Annotation Queue Management

```sql
CREATE TABLE annotation_tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type     TEXT NOT NULL,     -- 'production_sample' | 'calibration_set' | 'dataset_review'
    source_ref      UUID NOT NULL,     -- trace_id or dataset_example_id
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','assigned','completed','needs_adjudication')),
    assigned_to     TEXT,
    assignment_strategy TEXT NOT NULL DEFAULT 'round_robin', -- or 'skill_based'
    requires_second_review BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id       TEXT NOT NULL
);

CREATE TABLE annotation_results (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID NOT NULL REFERENCES annotation_tasks(id),
    annotator       TEXT NOT NULL,
    label           JSONB NOT NULL,
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`requires_second_review = true` (default for anything targeting a golden dataset) means a task isn't `completed` until two independent `annotation_results` rows exist; a disagreement between them flips status to `needs_adjudication` and routes to a third, more senior annotator rather than auto-resolving.

### 10.3 Inter-Annotator Agreement

```python
def compute_agreement(task_ids: list[str]) -> AgreementReport:
    results_by_task = group_results(task_ids)   # {task_id: [annotation_results]}
    if all(len(r) == 2 for r in results_by_task.values()):
        labels_a = [r[0].label for r in results_by_task.values()]
        labels_b = [r[1].label for r in results_by_task.values()]
        kappa = cohen_kappa_score(labels_a, labels_b)
    else:
        # 3+ annotators per task somewhere in the set
        kappa = fleiss_kappa(build_rating_matrix(results_by_task))
    return AgreementReport(kappa=kappa, n_tasks=len(results_by_task))
```

Surfaced per dataset/task type in the annotation dashboard; a kappa below 0.4 on a given labeling task is a signal the rubric itself is ambiguous and needs rewriting before more labels are collected against it — cheaper to fix the instructions than to keep producing noisy labels.

**Worked example**: three annotators label 100 examples as pass/fail on "is this response helpful." Fleiss' kappa is computed from the agreement matrix:

```python
# ratings: 100 rows x 2 categories (pass, fail), each cell = count of annotators
# who chose that category for that example (3 annotators per example)
ratings = build_rating_matrix(annotation_results)   # shape (100, 2)

def fleiss_kappa(ratings: np.ndarray) -> float:
    n, k = ratings.shape
    N = ratings.sum(axis=1)[0]          # raters per item, assumed constant
    p_j = ratings.sum(axis=0) / (n * N) # category marginal proportions
    P_i = ((ratings ** 2).sum(axis=1) - N) / (N * (N - 1))
    P_bar = P_i.mean()
    P_e = (p_j ** 2).sum()
    return (P_bar - P_e) / (1 - P_e)

kappa = fleiss_kappa(ratings)   # e.g., 0.62 → "substantial agreement" (Landis & Koch)
```

A result of 0.62 clears the platform's default 0.4 trust threshold, so this task's labels are eligible for golden-set promotion without adjudication overhead; a result below 0.4 would instead trigger the rubric-rewrite workflow described above before any more of these labels are trusted.

### 10.4 Feedback Aggregation

A single trace can accumulate an end-user thumbs-down, an internal reviewer's 4/5 rating, and an automated judge score of 0.72 — these are not collapsed into one number silently. The aggregation layer stores all three, computes a **weighted composite** (weights configurable per tenant, e.g., domain-expert label > internal reviewer > end-user > judge) for use in dashboards/alerting defaults, but the disaggregated view is always one click away, because "the aggregate says fine but the domain expert said no" is exactly the kind of disagreement a team needs visible, not averaged away.

### 10.5 Feedback → Dataset Pipeline

Identical mechanism to production sampling (§6.3): feedback below a configurable threshold, or tagged with a specific failure category, auto-creates a `pending_review` dataset candidate referencing the originating trace, which flows into the same annotation-promotion workflow.

### 10.6 Feedback → Fine-Tuning Pipeline

```
High-confidence corrected examples (feedback_type = "correction", reviewed and
confirmed by a second annotator, tenant has consented to training-data use)
        │
        ▼
  Training Data Export Job (separate service, separate compliance boundary
  from the eval/observability data store — this data flow is legally
  distinct: it becomes model training data, not just an eval artifact)
        │
        ▼
  Fine-tune / reward-model training dataset (owned by the ML training team,
  the observability platform's responsibility ends at a clean, consented,
  attributable export)
```

This pipeline is explicitly gated behind a **separate consent flag** per tenant/data source — feedback collected under an eval/observability data-processing agreement does not automatically become training data; that requires its own opt-in, because the compliance obligations differ (training data typically has stricter, harder-to-reverse retention/consent requirements than eval telemetry).

### 10.7 Sequence Flow: End-User Feedback to Golden Dataset

```
End User      Product UI      Feedback API      Feedback Store      Sampling Rule Engine     Annotation Queue     Dataset Service
   │               │                │                  │                     │                       │                   │
   │ thumbs down    │                │                  │                     │                       │                   │
   │───────────────►│ submit         │                  │                     │                       │                   │
   │               │───────────────►│ validate trace_id,│                     │                       │                   │
   │               │                │ rate-limit per     │                     │                       │                   │
   │               │                │ (user, trace)       │                     │                       │                   │
   │               │                │────────────────────►│                     │                       │                   │
   │               │                │                    │ (nightly) matches   │                       │                   │
   │               │                │                    │ "rating <= 2" rule  │                       │                   │
   │               │                │                    │────────────────────►│ create pending_review │                   │
   │               │                │                    │                     │ candidate + task       │                   │
   │               │                │                    │                     │───────────────────────►│                   │
   │               │                │                    │                     │                       │ assigned to       │
   │               │                │                    │                     │                       │ annotator          │
   │               │                │                    │                     │                       │ (round-robin)      │
   │               │                │                    │                     │                       │ 2 concurring labels│
   │               │                │                    │                     │                       │──────────────────►│ promote into
   │               │                │                    │                     │                       │                   │ golden dataset
   │               │                │                    │                     │                     │                   │ new version
```

### 10.8 Multi-Dimension Feedback Form Example

```typescript
<Feedback
  traceId={currentTraceId}
  type="multi-dimension"
  dimensions={[
    { key: "accuracy", label: "Was this accurate?", scale: "likert-5" },
    { key: "tone", label: "Was the tone appropriate?", scale: "likert-5" },
    { key: "correction", label: "What should it have said?", type: "free-text", optional: true },
  ]}
  onSubmit={(value) => feedbackClient.submit({
    traceId: currentTraceId,
    feedbackType: "multi_dimension",
    value,
    role: "end_user",
  })}
/>
```

Multi-dimension feedback stores each dimension as a separate scored field within `feedback_entries.value` (JSONB), so a downstream consumer can query "traces rated poorly on tone but well on accuracy" as an independent slice — collapsing this into a single average at collection time would destroy exactly the information a team most needs when a single blended thumbs-down could mean two very different underlying problems.

---

## 11. Regression Detection

### 11.1 Metric Time Series

Every online-eval score and every offline `EvalRun` aggregate is written as a point in the Metrics TSDB (`metric_name, agent_id, prompt_version_id, model, tenant_id, timestamp, value`), which is what makes both point-in-time comparisons (11.2) and trend analysis (11.3) queries over the same underlying data rather than two separate systems.

### 11.2 Statistical Tests

| Test | Used for | Why |
|---|---|---|
| Two-sample t-test | Metrics with approximately normal score distributions and reasonably large samples (e.g., latency in ms) | Standard, well-understood, fast |
| Mann-Whitney U | Non-normal / ordinal metrics (Likert scores, 1-5 judge scores) | Doesn't assume normality, robust to outliers |
| Bootstrap confidence interval | Metrics with no clean parametric form (composite scores, ratios) | Makes no distributional assumption at all, works for any statistic |

```python
def bootstrap_ci(sample: list[float], stat_fn=np.mean, n_boot=10000, alpha=0.05):
    boot_stats = [stat_fn(np.random.choice(sample, len(sample), replace=True)) for _ in range(n_boot)]
    lo, hi = np.percentile(boot_stats, [100*alpha/2, 100*(1-alpha/2)])
    return stat_fn(sample), (lo, hi)
```

### 11.3 Change-Point Detection

Two-point before/after comparisons miss gradual drift (a judge or model quietly degrading over weeks with no single sharp before/after boundary to compare across). A change-point detector (PELT or Bayesian online change-point detection, both standard, off-the-shelf algorithms) runs over each tracked metric's daily time series and flags a detected shift for review even with no specific deploy event to compare against:

```python
import ruptures as rpt

def detect_change_points(series: list[float]) -> list[int]:
    algo = rpt.Pelt(model="rbf").fit(np.array(series))
    return algo.predict(pen=10)   # indices of detected change points
```

### 11.4 Alert Rules

```yaml
alert_rules:
  - name: faithfulness-absolute-floor
    metric: faithfulness
    agent_id: support-triage
    condition: absolute_threshold
    threshold: 0.80
    direction: below
    severity: page

  - name: latency-relative-degradation
    metric: latency_p99_ms
    agent_id: support-triage
    condition: relative_change
    window: 7d
    threshold_pct: 20
    direction: increase
    severity: ticket

  - name: csat-trend
    metric: user_thumbs_rating
    agent_id: support-triage
    condition: trend
    consecutive_periods: 3
    period: 1d
    direction: declining
    severity: ticket
```

### 11.5 Automated Rollback Triggers

Covered mechanically in §8.5 (canary rollback). The Regression Detector is the component that evaluates the statistical verdict feeding that decision — it is the same engine whether the trigger is a canary comparison or a scheduled full-fleet regression scan; the difference is only which two cohorts (canary vs. control, or this week vs. last week) are being compared.

### 11.5.1 Regression Detector Worker Architecture

```
                ┌──────────────────────────┐
   scheduled    │  Regression Detector       │      reads: Metrics TSDB (time series),
   (cron, per   │  Scheduler                 │             Postgres (alert_rules, active canaries)
   rule cadence)│                            │
                └──────────┬─────────────────┘
                           │ dispatch evaluation jobs
                           ▼
                ┌──────────────────────────┐
                │  Stats Worker Pool          │──► §11.2 tests, §11.3 change-point detection,
                │  (stateless, horizontally   │    §11.6 multiple-comparison correction
                │   scaled)                   │
                └──────────┬─────────────────┘
                           │ verdict
                           ▼
                ┌──────────────────────────┐
                │  Verdict → Action Router    │
                └──────┬───────────┬─────────┘
                       │           │
              REGRESSION       PROMOTE-ELIGIBLE / no-op
                       │           │
                       ▼           ▼
              ┌────────────┐  ┌────────────────┐
              │ Alert       │  │ (dashboard      │
              │ Manager     │  │  verdict update │
              │ (§11.9)     │  │  only)          │
              └──────┬──────┘  └────────────────┘
                     │
          tenant opted   tenant opted
          into auto-      OUT of auto-
          rollback?        rollback?
                │                │
                ▼                ▼
        Prompt Registry     Human notified,
        rollback (§5.8)     awaits manual action
```

The Scheduler runs each `alert_rule` and each active canary's comparison on its own configured cadence (e.g., absolute-threshold rules checked every few minutes, canary comparisons every 15 min per §8.4, full-fleet change-point scans daily) rather than a single global tick — this bounds worst-case detection latency per rule type without forcing every rule to pay the cost of the most expensive analysis (change-point detection) on the tightest cadence.

### 11.6 A/B Test Analysis Rigor

* **Minimum sample size** enforced before any verdict (power-analysis-derived per metric's expected effect size and variance, not an arbitrary round number).
* **Multiple-comparison correction** (Benjamini-Hochberg FDR control) applied whenever a canary is evaluated against more than one tracked metric simultaneously — evaluating 10 metrics at p<0.05 each has a much higher than 5% chance of a false positive on *some* metric by chance alone, and an alerting system that doesn't correct for this trains its users to distrust it.
* **Practical vs. statistical significance** both reported — a p<0.001 result on a 0.2% latency change is statistically real and practically irrelevant; the report shows effect size alongside p-value so a human isn't misled by significance alone.

### 11.7 False-Positive Management

* **Alert deduplication**: multiple rules firing off the same underlying regression (e.g., faithfulness drop triggers both an absolute-threshold and a trend alert) collapse into one notification referencing both rule matches, not two pages.
* **Snooze/false-positive feedback loop**: an operator marking an alert as a false positive is captured and used to adjust that rule's threshold sensitivity (or flagged for the rule's owner to revisit) rather than silently discarded — an alerting system with no feedback loop degrades into "the thing everyone ignores" within a few months, which is worse than no alerting at all.

### 11.8 Dashboards

Dashboards are the read side of everything this document has described so far — they issue queries against the Metrics TSDB (aggregate trend widgets), the Trace Store (drill-down and search), and the control-plane Postgres (deploy markers, alert history), fanned out by the Dashboard Service and composed into one view per scope:

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Agent: support-triage         Env: prod        Window: last 24h          │
├──────────────────────────────────────────────────────────────────────────┤
│  Volume            Latency P50/P99        Error Rate        Cost/day      │
│  ▂▃▅▇█▇▅▃▂          420ms / 2100ms         0.3%              $284         │
├──────────────────────────────────────────────────────────────────────────┤
│  Quality Trend (faithfulness, relevance, CSAT) ── with deploy markers ──  │
│  0.95 ┤                                    ▼ deploy pv_8f2a (14:32)       │
│  0.90 ┤────────────╮                      ╭────                          │
│  0.85 ┤             ╰──────────╮         ╱                               │
│  0.80 ┤                         ╰───────╯   ← regression window flagged  │
│       └───────────────────────────────────────────────────► time         │
├──────────────────────────────────────────────────────────────────────────┤
│  Active Canary: pv_9c31 @ 10%  |  Verdict: CONTINUE (n=340/500 required)  │
├──────────────────────────────────────────────────────────────────────────┤
│  Open Alerts (2)                                                          │
│  ⚠ faithfulness-absolute-floor  triggered 09:14  [view evidence]         │
│  ⚠ latency-relative-degradation triggered 11:02  [view evidence]         │
└──────────────────────────────────────────────────────────────────────────┘
```

Every widget supports **drill-down**: clicking the quality-trend dip opens the exact set of traces contributing to that window's aggregate score, pre-filtered — satisfying the task's explicit requirement that "an aggregate metric without a path to its raw evidence is not acceptable." Deploy markers are pulled directly from `prompt_deployments` rows in the same time range, which is why the quality-trend regression and the 14:32 deploy visually line up without any manual correlation step.

### 11.9 Alert Rule Authoring and Notification Routing

```yaml
# authored by an end-user team through the dashboard UI, not just platform operators
notification_channels:
  - type: slack
    target: "#support-triage-alerts"
  - type: pagerduty
    target: "support-triage-oncall"
    severity_filter: [page]     # only page-severity alerts wake someone up at 3am;
                                 # ticket-severity alerts go to Slack only
```

Alert Manager deduplicates by `(rule_name, agent_id, metric)` within a configurable dedup window (default 1h) — a metric flapping across a threshold repeatedly produces one open alert with an updated evidence timeline, not a new page every time it crosses back and forth, directly addressing the false-positive/alert-fatigue concern from §11.7 at the delivery layer as well as the detection layer.

---

## 12. Storage Architecture

| Store | Technology | Holds | Why this choice |
|---|---|---|---|
| Trace Store | ClickHouse | Structured span rows | Columnar, fast analytical aggregation, native array/map columns for attributes, TTL-driven tiering built in |
| Payload Store | S3 (or equivalent blob store) | Large message/tool/retrieval content, content-addressed | Cheap at scale, natural fit for immutable content-addressed blobs, lifecycle policies for tiered storage/expiry |
| Control-plane metadata | PostgreSQL | Prompt versions/deployments, datasets, eval run metadata, feedback, annotation queues, alert rules | Strong consistency, relational integrity (foreign keys between versions/deployments/runs matter), moderate volume |
| Eval artifacts | S3 | Full eval-run transcripts, judge reasoning text | Same rationale as payload store — large, immutable, infrequently random-accessed |
| Metrics time series | Prometheus-compatible TSDB (e.g., Thanos/M3/Mimir) | Aggregated metric points for dashboards, alerting, regression detection | Purpose-built for high-cardinality-bounded time series with efficient range queries and native alerting integration |
| Ingest buffer | Kafka | Raw spans between collector and processor; also queue mediator for online eval and annotation tasks | Durable, replayable, decouples ingestion rate from processing rate, natural backpressure point |
| Edge cache | Redis (or in-process + pub/sub) | Prompt registry hot reads | Sub-millisecond reads, pub/sub invalidation fits the "flip a pointer, notify everyone" deploy model |

### 12.1 Retention Policies

```
Trace Store (ClickHouse):
  hot (SSD):    0-90 days,  full row detail
  cold (object storage tier via ClickHouse TTL TO VOLUME): 90-400 days, same schema, slower query
  deleted: > 400 days (configurable per tenant compliance requirement, some go to 30 days,
                        some golden-eval-linked traces are retained indefinitely by explicit tag)

Payload Store (S3):
  standard: 0-30 days
  infrequent access: 30-180 days
  glacier/deep-archive or delete: > 180 days, per-tenant policy
  PII-flagged payloads: redacted or deleted per tenant policy, independent of the above,
    checked by a scheduled redaction job against tenant PII-retention config

Metrics TSDB: downsampled after 30 days (raw → 5-min → 1-hour rollups), kept 2 years
  (dashboards showing year-over-year trend need the rollups, not raw points)
```

### 12.2 Data Flow Between Stores

```
                     ┌─────────────┐
     writes          │   Kafka     │  writes (queue mediator)
   ┌──────────────────┤  (ingest,   ├──────────────────┐
   │                  │   feedback, │                   │
   │                  │   eval jobs)│                   │
   ▼                  └─────────────┘                   ▼
┌─────────────┐                              ┌────────────────────┐
│ ClickHouse    │◄─── query (dashboards, ────►│ Postgres             │
│ (traces,      │      trace search,          │ (prompts, datasets,  │
│  span rollups)│      eval result join)       │  eval runs, feedback,│
└──────┬────────┘                              │  annotation queues)  │
       │  pointer                                       │  pointer
       ▼                                                 ▼
┌─────────────┐                              ┌────────────────────┐
│ S3            │◄─────────────────────────────┤ (eval transcripts,  │
│ (payloads)    │                              │  judge reasoning)   │
└─────────────┘                              └────────────────────┘

       ClickHouse aggregate rollups also flow, on a schedule, into:
                              ▼
                     ┌─────────────────┐
                     │ Metrics TSDB      │  (5-min rollups of key metrics,
                     │                   │   feeds dashboards + alerting +
                     └─────────────────┘   regression detector time series)
```

No store is a single point of query failure for every use case: trace search degrades gracefully if the TSDB is down (dashboards show "stale" but the Trace Store answers direct trace lookups), and the reverse holds too — this redundancy of *purpose*, not data, is what keeps a single component outage from taking down the whole platform's read path.

### 12.3 Backup and Disaster Recovery

| Store | Backup mechanism | RPO | RTO |
|---|---|---|---|
| ClickHouse | Object-storage-backed replicas (ClickHouse Keeper + replicated MergeTree) across 3 AZs | ~0 (synchronous replication within a region) | Minutes (automatic replica promotion) |
| Postgres (control plane) | Continuous WAL archiving + daily snapshot, multi-AZ standby | < 1 min | < 5 min (standby promotion) |
| Kafka | Replication factor 3 across AZs | ~0 for acknowledged writes | Minutes |
| S3 payloads | Native multi-AZ durability (11 nines), versioning enabled | ~0 | N/A (no restore needed under normal AZ loss) |

The control-plane Postgres is the one store where an RTO of minutes actually matters operationally, since it's on the hot path for prompt deploys (though not for prompt *reads*, which are edge-cached per §5.6) — everything else can tolerate a longer recovery window because reads degrade to cache/replay rather than hard-failing.

---

## 13. Data Models

```sql
-- Trace / Span: see full ClickHouse schema in §3.6

-- PromptVersion / PromptDeployment: see full schema in §5.3

-- Dataset / DatasetVersion / DatasetExample: see full schema in §6.2

CREATE TABLE eval_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_version_id  UUID NOT NULL REFERENCES dataset_versions(id),
    prompt_version_id   TEXT NOT NULL REFERENCES prompt_versions(id),
    model               TEXT NOT NULL,
    judge_config_ids    TEXT[] NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running','completed','failed','cancelled')),
    metrics_requested   TEXT[] NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,
    triggered_by        TEXT NOT NULL,     -- user, or "ci:<pipeline_run_id>"
    tenant_id           TEXT NOT NULL
);

CREATE TABLE eval_results (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    eval_run_id     UUID NOT NULL REFERENCES eval_runs(id),
    dataset_example_id UUID NOT NULL REFERENCES dataset_examples(id),
    output_ref      TEXT NOT NULL,          -- S3 pointer to full model output
    metric_scores   JSONB NOT NULL,         -- {"faithfulness": 4, "exact_match": 1.0, ...}
    latency_ms      INT,
    cost_usd        NUMERIC,
    UNIQUE(eval_run_id, dataset_example_id)
);

CREATE TABLE judge_configs (
    id                  TEXT PRIMARY KEY,   -- content-addressed
    name                TEXT NOT NULL,
    judge_model         TEXT NOT NULL,
    rubric_template     TEXT NOT NULL,
    criteria            TEXT[] NOT NULL,
    scoring_scale       TEXT NOT NULL,
    output_schema       JSONB NOT NULL,
    calibration_score   NUMERIC,
    calibrated_at       TIMESTAMPTZ,
    tenant_id           TEXT NOT NULL
);

CREATE TABLE feedback_entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id        UUID NOT NULL,
    feedback_type   TEXT NOT NULL,   -- thumbs | likert | free_text | correction | multi_dimension
    value           JSONB NOT NULL,
    submitted_by    TEXT,
    role            TEXT NOT NULL,   -- end_user | internal_reviewer | domain_expert
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id       TEXT NOT NULL
);

CREATE TABLE alerts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_name       TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    metric          TEXT NOT NULL,
    triggered_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    severity        TEXT NOT NULL,
    evidence        JSONB NOT NULL,   -- comparison stats, p-value, sample sizes, trace_ids of examples
    status          TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','acknowledged','resolved','false_positive')),
    tenant_id       TEXT NOT NULL
);

-- Supporting entities referenced elsewhere in this document but not yet
-- given a full schema:

CREATE TABLE redaction_policies (
    tenant_id       TEXT PRIMARY KEY,
    mode            TEXT NOT NULL DEFAULT 'redact_pii'
                        CHECK (mode IN ('log_full','redact_pii','hash_only','no_persist')),
    pii_categories  TEXT[] NOT NULL DEFAULT ARRAY['email','phone','ssn','credit_card','name'],
    retention_days  INT NOT NULL DEFAULT 30,
    updated_by      TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE cost_ledger (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL,
    category        TEXT NOT NULL,   -- 'model_call' | 'judge_call' | 'annotation' | 'storage'
    agent_id        TEXT,
    trace_id        UUID,            -- nullable: storage/annotation costs aren't trace-scoped
    eval_run_id     UUID,
    amount_usd      NUMERIC NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    period          DATE NOT NULL    -- billing-period bucket, for fast monthly rollups
);
-- Indexed on (tenant_id, period) and (tenant_id, agent_id, period) — this is
-- the table the cost dashboards (Req 10) and budget-ceiling enforcement
-- (§9.5, §17) both read from; judge and annotation spend land here through
-- the same path as model-call cost so "is this team's traffic slow, failing,
-- or expensive" (task's framing) is answerable from one ledger, not three.

-- Materialized view backing the dashboard's pre-aggregated widgets (§15.5) —
-- illustrative ClickHouse definition, refreshed incrementally on span insert:
CREATE MATERIALIZED VIEW span_hourly_rollup
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMMDD(hour)
ORDER BY (tenant_id, agent_id, hour)
AS SELECT
    tenant_id,
    agent_id,
    toStartOfHour(start_time) AS hour,
    countState() AS request_count,
    avgState(duration_ms) AS avg_latency,
    quantileState(0.99)(duration_ms) AS p99_latency,
    sumState(llm_cost_usd) AS total_cost,
    countStateIf(status = 'error') AS error_count
FROM spans
GROUP BY tenant_id, agent_id, hour;
```

### 13.1 Entity Relationship Summary

```
Tenant ──< Agent ──< AgentRun (trace) ──< Span (llm.call | tool.execute | retrieval.query | agent.step)
                          │
                          ├──► FeedbackEntry (by trace_id)
                          └──► EvalResult (by source_trace_id, if promoted into a dataset)

PromptVersion ──< PromptDeployment >── Environment
     │
     └── referenced by: Span.llm_prompt_version_id, EvalRun.prompt_version_id

Dataset ──< DatasetVersion ──< DatasetExample
     │                              │
     └── referenced by: EvalRun.dataset_version_id       └── referenced by: EvalResult.dataset_example_id

JudgeConfig ──< calibration runs (AnnotationTask against a calibration Dataset)
     │
     └── referenced by: EvalRun.judge_config_ids, EvalResult.metric_scores (score provenance)

AlertRule ──< Alert (triggered instances) ──► evidence references Span/EvalResult/trace_ids
```

Every arrow in this diagram is a foreign-key-backed reference to an **immutable, versioned** record (§5.8's "everything the platform reasons about is a pinned version" principle) — the one deliberate exception is `Tenant`/`Agent`, which are mutable configuration entities, not versioned artifacts, since they represent an organizational identity rather than a point-in-time snapshot of behavior.

### 13.2 Schema Evolution Strategy

Two very different evolution problems live side by side in this data model, and they're handled differently on purpose:

* **The `spans` table's `attributes` and `payload_refs` columns are `Map(String, String)`** (§3.6) specifically so that adding a new span attribute (a new field on `llm.call`, or an entirely new span kind) never requires a ClickHouse schema migration — new attribute keys just start appearing in the map. The cost of this flexibility is that attribute-level type safety and required-field validation happen in application code (the Span Processor's per-span-kind schema validator, §3.8), not in the database schema itself — a deliberate trade of DB-enforced correctness for zero-downtime schema growth, appropriate given how frequently new LLM providers and features (new tool-call formats, new modalities) show up.
* **The `prompt_versions`, `dataset_examples`, `judge_configs`, and `eval_results` tables use explicit typed columns**, not a generic attributes map, because these are the tables reproducibility guarantees (§5.8, §1) depend on — a loosely-typed, ever-growing schema for the *inputs to an eval run* would undermine the exact guarantee those tables exist to provide. Changes to these schemas go through standard migrations (Postgres `ALTER TABLE`), versioned and applied with the same rigor as any other production database change, precisely because they're rare relative to span-attribute growth and correctness matters more than agility for this data.

This split — flexible schema-on-read for high-volume, fast-evolving telemetry; strict schema-on-write for low-volume, reproducibility-critical control-plane data — mirrors the ClickHouse-vs-Postgres store split in §12 at the column level, not just the table level.

---

## 14. API Design

```
# Trace ingestion (OTLP-compatible)
POST /v1/traces                          — OTLP gRPC/HTTP span batch ingestion

# Trace query
GET  /v1/traces/{trace_id}                — full hydrated span tree
GET  /v1/traces?agent_id=&filter=&window= — search/filter, paginated

# Prompt Registry
POST /v1/prompts/{name}/versions          — create new version (content-addressed)
GET  /v1/prompts/{name}/versions/{id}     — fetch a specific version
GET  /v1/prompts/{name}?env=prod          — resolve currently-deployed version (hot path)
POST /v1/prompts/{name}/deployments       — deploy a version to an environment (supports traffic_pct)
POST /v1/prompts/{name}/rollback          — roll environment back to previous deployment
GET  /v1/prompts/{name}/diff?from=&to=    — diff view between two versions

# Datasets
POST /v1/datasets                         — create dataset
POST /v1/datasets/{id}/versions           — create new version (add/remove/edit examples)
GET  /v1/datasets/{id}/versions/{v}       — fetch example set for a version

# Offline Eval
POST /v1/eval-runs                        — submit {dataset_version_id, prompt_version_id, model, metrics}
GET  /v1/eval-runs/{id}                   — status + results summary
GET  /v1/eval-runs/compare?a=&b=          — comparison report with significance tests

# Online Eval / Canary
POST /v1/online-eval-configs              — configure sampling rates + checks per agent
GET  /v1/canary/{prompt_name}/status      — live canary comparison verdict

# LLM Judge
POST /v1/judges                           — register/version a judge config
POST /v1/judges/{id}/calibrate            — run calibration against a labeled set
GET  /v1/judges/{id}/calibration          — current calibration score + history

# Human Feedback
POST /v1/feedback                         — submit feedback tied to a trace_id
GET  /v1/annotation-tasks?assigned_to=    — an annotator's queue
POST /v1/annotation-tasks/{id}/results    — submit a label

# Dashboards / Alerts
GET  /v1/metrics?agent_id=&metric=&window=&granularity=
POST /v1/alert-rules
GET  /v1/alerts?status=open
```

Every write endpoint requires `tenant_id` resolution from the caller's auth context (never a client-supplied field) — tenant isolation is enforced at the API gateway/auth layer before a request reaches any service, consistent with the "structural, not filtered" isolation principle in §1.

### 14.1 Example Request/Response Payloads

```
POST /v1/eval-runs
{
  "dataset_version_id": "dv_4471",
  "prompt_version_id": "pv_8f2a1c9d3e4f5678",
  "model": "claude-sonnet-4-5",
  "metrics": ["exact_match", "faithfulness", "cost_usd", "latency_ms"],
  "judge_config_ids": ["jc_faith_v3"]
}

→ 202 Accepted
{
  "id": "er_a91f2c",
  "status": "running",
  "example_count": 10000,
  "estimated_completion": "2026-08-30T14:52:00Z"
}
```

```
GET /v1/eval-runs/compare?a=er_a91f2c&b=er_prod_baseline_0912

→ 200 OK
{
  "run_a": "er_a91f2c",
  "run_b": "er_prod_baseline_0912",
  "comparisons": [
    {"metric": "faithfulness", "mean_a": 0.87, "mean_b": 0.91, "delta": -0.04,
     "p_value": 0.003, "significant": true, "test": "mann_whitney_u"},
    {"metric": "exact_match", "mean_a": 0.74, "mean_b": 0.72, "delta": 0.02,
     "p_value": 0.21, "significant": false, "test": "mann_whitney_u"}
  ]
}
```

```
POST /v1/feedback
{
  "trace_id": "tr_9f3e2a",
  "feedback_type": "thumbs",
  "value": {"rating": "down"},
  "role": "end_user"
}

→ 201 Created  { "id": "fb_11ac3d" }
```

### 14.2 Auth and Rate Limiting on the API Layer

Every endpoint sits behind the same API gateway used platform-wide: service-identity auth (mTLS or signed JWT) resolves `tenant_id` and scopes, and per-tenant rate limits apply independently per endpoint class (ingestion endpoints rate-limited far more permissively than, say, `POST /v1/eval-runs`, which is expensive to fan out and therefore quota-limited per tenant to prevent one team's CI pipeline from starving the shared eval worker pool — the same noisy-neighbor concern as trace ingestion, applied to compute-heavy control-plane operations).

---

## 15. Scaling

### 15.1 Trace Ingestion at 150,000 spans/sec Peak

```
Collector tier: stateless OTLP receivers behind a load balancer,
  horizontally scaled on CPU (protobuf decode + basic validation).
  ~150k spans/sec / ~5k spans/sec per collector instance ≈ 30 instances at peak.

Kafka: partitioned by tenant_id (bounds any one tenant's burst to its own
  partitions' consumer lag, protecting other tenants) — target ~50 partitions,
  each handling up to ~3-4k spans/sec comfortably.

Span Processor: consumer group scaled to Kafka partition count, does
  enrichment (rollups, redaction, tail-sampling buffering) — CPU/memory bound
  on the tail-sampling buffer (bounded by max trace duration timeout, §3.5).

ClickHouse: sized for ~150k rows/sec sustained insert — batched inserts
  (Span Processor batches ~5k rows or 1s, whichever first) since ClickHouse
  strongly prefers large batched inserts over many small ones.
```

### 15.2 Eval Pipeline Parallelism

Covered with concrete arithmetic in §7.2. General scaling lever: eval workers are stateless and horizontally scaled per `EvalRun`, bounded only by the target model's provider rate limit (coordinated through the shared Model Gateway rate limiter) — adding platform compute beyond that ceiling doesn't increase throughput, so the scaling strategy is "scale workers up to the provider-quota ceiling, then queue."

### 15.3 Sampling to Control Cost at Scale

The single biggest lever against both storage and judge-spend growth is sampling, applied at three independent points: head/tail trace sampling (§3.5), online-eval cheap-vs-judge sampling (§8.1/8.3), and dataset production-sampling filters (§6.3) — each tunable per tenant, so a cost-sensitive high-volume tenant and a low-volume compliance-heavy tenant coexist without either dictating the platform's aggregate cost profile.

### 15.4 Tiered Storage

Hot/cold/deleted tiers (§12.1) are the mechanism that lets 500 TB/year of raw data stay affordable — the overwhelming majority of trace volume is queried only in its first 1-2 weeks (active debugging, recent canary analysis); moving anything older to cheaper object storage tiers with the same query interface (ClickHouse's `TTL ... TO VOLUME`) keeps the affordability curve sane without a separate "cold trace" system operators have to remember exists.

### 15.5 Dashboard Query Scaling

Dashboard aggregate queries (P99 ≤ 2s NFR) are served from pre-aggregated materialized views in ClickHouse (`AggregatingMergeTree` rolling up per-agent, per-hour latency/cost/volume), not raw span scans — a live "last 24h P99 latency" widget queries ~24 pre-aggregated rows, not millions of raw spans. Materialized views are refreshed incrementally as new spans land (ClickHouse's native materialized view mechanism, triggered on insert), so aggregate freshness lags raw ingestion by seconds, not minutes, and the 2-second query SLA is a property of the pre-aggregation, not of query-time computation over raw data.

### 15.6 Judge Service Scaling

The Judge Service itself is stateless and scales horizontally like any other worker pool; the actual constraint is external — the judge model provider's TPM/RPM quota (§2.4, §17). Scaling strategy is therefore: provision enough Judge Service workers to saturate the provider quota under peak concurrent demand (offline eval runs + online sampling + canary-boosted sampling all competing for the same quota), and apply the same per-tenant fair-share token-bucket rate limiting used elsewhere in the platform (mirroring the Model Gateway's pattern) so one tenant's large offline eval run cannot starve another tenant's online canary judge calls sharing the same provider account.

### 15.7 Feedback and Annotation Scaling

Feedback ingestion (§10.1) is low-volume relative to trace ingestion (bounded by human interaction rate, not machine request rate) and scales trivially on the same Postgres-backed service pattern as the rest of the control plane. Annotation *throughput*, however, is bounded by human annotator headcount, not compute — the platform's job here is queue efficiency (routing, avoiding idle annotators, avoiding duplicate work) rather than horizontal scaling, which is why §10.2's queue design optimizes for assignment strategy correctness over raw throughput.

### 15.8 Capacity Summary Table

A consolidated view of the numbers derived throughout this document, for quick reference against the NFRs in the task:

| Dimension | Target (NFR) | Design headroom / mechanism |
|---|---|---|
| Sustained span ingestion | 50,000/sec | ~30 collector instances at ~5k spans/sec each (§15.1); Kafka partitioned to ~50 partitions, ~1k/sec each comfortably |
| Peak span ingestion | 150,000/sec | Same tier autoscales; SDK local buffering (§2.3) absorbs any residual burst above provisioned capacity without blocking callers |
| Trace query P99 | ≤ 300 ms | ClickHouse `ORDER BY (tenant_id, agent_id, start_time)` keeps common queries within a few partitions (§3.6) |
| Dashboard aggregate P99 | ≤ 2 s | Pre-aggregated materialized views, not raw scans (§15.5) |
| Prompt registry read P99 | ≤ 10 ms | Edge cache, zero network hop in the common case (§5.6) |
| Offline eval throughput | 10,000 examples / 30 min | ≥22 workers required, 40 provisioned (§7.2), bounded ultimately by provider rate limit, not platform compute |
| Online eval judge latency | ≤ 60 s P99 | Fully async, queue-mediated, decoupled from ingestion (§8.2) |
| Trace ingestion availability | 99.95% | Degrades to local SDK buffering, never blocks caller (§2.3, §16.1) |
| Prompt registry read availability | 99.99% | Edge-cache-served, control-plane outage doesn't propagate to reads (§16.5) |
| Storage growth | ~500 TB/year effective | Tiered retention + payload dedup bring raw ~1 PB/year (§2.4) down toward the NFR target |

This table is deliberately the last word on scaling rather than the first, because every number in it is derived from a specific mechanism described earlier — a capacity table with no design behind it is just a wish list.

---

## 16. Failure Modes

### 16.1 Trace Ingestion Overload / Loss

**Symptom**: Collector queue backs up, Kafka consumer lag grows, spans arrive late or get dropped at the SDK's local buffer limit.

**Application-visible impact**: none — the SDK's local buffer absorbs backpressure and drops oldest-first once full, emitting a `dropped_spans_total` counter locally; the instrumented application's own request path is never blocked (§2.3 design principle).

**Platform-visible impact**: Collector emits `ingest_lag_seconds` and `spans_dropped_total`; an internal alert (not customer-facing) fires if lag exceeds a threshold, prompting horizontal scale-out of the collector/processor tier.

**Mitigation**: Kafka gives ~7 days of replay buffer at default retention, so a processor-side outage (not a collector/SDK-side drop) is recoverable by replaying once processors are healthy again — genuine data loss only occurs if the SDK's local buffer overflows before ever reaching Kafka, which is sized and alerted on independently.

### 16.2 Eval Pipeline Failure Mid-Run

**Symptom**: A worker crashes partway through a 10,000-example `EvalRun`.

**Mitigation**: Examples are idempotent, individually-keyed units of work (§7.1) — the Eval Scheduler tracks per-example completion and re-queues anything not completed within a timeout. A full-run failure (e.g., the target model provider goes down entirely) marks the run `failed` with partial results preserved and queryable, rather than discarding completed work; a re-triggered run only re-processes examples that don't already have a result for that exact `(eval_run_id, example_id)` pair.

### 16.3 Judge Model Silently Degrading

**Symptom**: A provider swaps the model behind a fixed model string (a known real-world occurrence), and judge scores drift without any error being raised — the judge still returns confidently-formatted scores, just less accurate ones.

**Mitigation**: This is exactly why calibration (§9.3) is re-run and monitored continuously, not just at judge creation — a scheduled recalibration job runs the active judge against its calibration set on a regular cadence (e.g., weekly) even with no config change, and a calibration-score drop between runs is itself a regression-detector-tracked metric (§11), alerting the platform team (not the judge's individual consumers, since a provider-side model swap affects every judge using that model, not just one tenant's).

### 16.4 Flood of Low-Quality / Spam Feedback

**Symptom**: A bot, a coordinated user action, or a UI bug generates a burst of thumbs-down (or thumbs-up) feedback with no real signal behind it.

**Mitigation**: Feedback aggregation (§10.4) already weights end-user feedback lower than reviewer/expert feedback by default, limiting a spam burst's influence on the composite score used for alerting. Additionally, rate limiting per `(user_id, trace_id)` (one feedback submission per user per trace, idempotent on resubmission) and an anomaly check on feedback *volume* itself (a sudden spike in submissions from a narrow set of identities) routes to manual review rather than silently feeding the burst into the feedback→dataset pipeline (§10.5) — the same "candidates require human promotion" gate that protects dataset quality against a biased judge (§6.3) protects it against spam feedback too.

### 16.5 Prompt Registry Outage

**Symptom**: The Prompt Registry control-plane service (Postgres + registry API) becomes fully unavailable.

**Application-visible impact**: None for existing traffic — every application node is already serving prompt reads from its local edge cache (§5.6), which continues serving last-known-good versions indefinitely. The *only* visible impact is that new prompt deploys/rollbacks cannot happen until the registry recovers — a real but bounded degradation, explicitly the trade-off accepted in exchange for the 99.99% hot-path *read* availability target (the registry API's own availability can be lower than that, because reads don't depend on it staying up).

**Platform-visible impact**: Deploy/rollback requests fail fast with a clear error (never silently queued and lost); an internal alert fires immediately given the registry's role as a single control point for an emergency kill-switch scenario (Exercise 2, §20) — this is the one outage class where the platform team's own incident response time matters disproportionately, since a tenant needing an emergency prompt change during this window has no fallback path.

**Mitigation**: Multi-AZ Postgres standby with automatic promotion (§12.3) bounds the outage window; the edge-cache fallback bounds the blast radius during that window to "can't deploy," never "can't serve."

### 16.6 Storage Node / AZ Loss

**Symptom**: A ClickHouse shard, Kafka broker, or an entire AZ becomes unavailable.

**Mitigation**: ClickHouse replicated MergeTree (§12.3) and Kafka's replication factor 3 mean an AZ loss is absorbed by automatic replica promotion with no data loss for already-acknowledged writes; in-flight writes at the moment of failure are retried by the producer (Span Processor / Collector) against the next healthy replica, using the same at-least-once delivery semantics already required for span durability — an AZ loss degrades capacity temporarily (fewer healthy replicas serving reads/writes until the AZ recovers or is replaced) but does not create a correctness gap, only a capacity one, which autoscaling and the remaining AZs' headroom are provisioned to absorb.

### 16.7 Cost Explosion / Runaway Judge Loop

**Symptom**: A misconfigured agent enters a retry loop that keeps generating traces, each of which triggers online-eval judge scoring; or a tenant fat-fingers an online-eval sample rate from 2% to 100%; or an offline eval run is accidentally triggered repeatedly by a mis-wired CI job on every commit instead of only on merge to a release branch.

**Mitigation**: This is precisely why hard budget ceilings exist at multiple independent layers (§9.5, §17) rather than as a single top-level control: the per-tenant daily judge-spend cap trips regardless of *why* volume spiked, pausing further judge calls once the cap is hit and alerting the tenant — the request-time cost is "some traffic goes unscored today," never "an uncapped bill." Complementing the hard cap, the cost ledger (§13) feeds a **cost anomaly check** (a specific instance of the change-point detection machinery in §11.3, applied to the `cost_usd` metric series instead of a quality metric) that pages the platform team on an unexplained spend-rate spike even before the daily cap would otherwise trip — catching the problem in the first hour of a runaway loop rather than only at the point the cap silently starts dropping work.

**What the tenant observes**: a dashboard banner ("judge spend cap reached for today, N% of traffic unscored") and an alert, not a bill they discover at the end of the month — cost incidents get the same "fail loud and visible immediately" treatment as a quality regression, consistent with the platform's general posture that surprises are worse than degraded-but-visible service.

---

## 17. Cost Model

### 17.1 What Drives Spend

| Driver | Scales with | Primary lever |
|---|---|---|
| Trace storage | Ingestion rate × retention × payload size | Sampling (§3.5), payload externalization + dedup (§3.3), tiered retention (§12.1) |
| LLM judge calls | Online sample rate × traffic + offline eval frequency × dataset size | Sampling (§8.1), caching identical judge calls (§9.5), cheap-model-for-volume / expensive-model-for-gates split (§9.4) |
| Human annotation | Annotation queue volume | Second-review only where required (golden tier), routing efficiency, promoting only filtered candidates rather than raw production sample |
| Compute (workers, ClickHouse, Kafka) | Ingestion + eval throughput | Horizontal autoscaling bound to actual load, not fixed peak provisioning |

### 17.2 Illustrative Numbers at Target Scale

```
Trace storage: 500 TB/year raw → after payload dedup (~30% reduction from
  shared retrieved-doc content) and tiered storage (cold tier ~5x cheaper
  than hot), effective blended cost roughly comparable to ~150-200 TB
  hot-tier-equivalent pricing.

Judge spend: at 2% online sampling of 100k req/s peak (much lower at
  sustained 50k req/s average) ≈ 1,000 judge calls/sec sustained-average
  scenario would be enormous — in practice tuned per-agent, most agents
  sampled far below the ceiling; illustrative single-agent example:
  10M requests/day × 2% sample × 1 judge call × ~$0.002/call (cheap judge
  model) ≈ $400/day for that agent's online eval judge spend — visible
  per-agent in the cost dashboard (Req 10) so a tenant can tune their own
  sample rate against their own budget.

Offline eval (CI gate): 10,000-example golden set × 1 judge criterion ×
  ~$0.01/call (higher-accuracy judge model for gating) = $100 per full
  gate run; run once per merge to a release branch, not per commit, to
  bound this.
```

### 17.2.1 Monthly Cost Breakdown (Illustrative, Platform-Wide at Target Scale)

| Category | Monthly estimate | Notes |
|---|---|---|
| ClickHouse compute + hot storage | $18,000 - $25,000 | Sized for 50k spans/sec sustained ingest + dashboard query load |
| S3 (payloads, eval artifacts, cold trace tier) | $4,000 - $7,000 | Dominated by payload volume before dedup savings |
| Kafka | $3,000 - $5,000 | Ingest buffer + queue mediator for eval/feedback/online-eval jobs |
| Postgres (control plane) | $1,500 - $2,500 | Low volume relative to trace/eval data; sized for durability, not throughput |
| Metrics TSDB | $2,000 - $3,000 | Downsampled rollups, 2-year retention |
| LLM judge spend (aggregate, online + offline, all tenants) | $15,000 - $40,000 | Highest-variance line item — directly a function of aggregate sample rates tenants choose; the single largest lever in §17.3 |
| Human annotation (contracted annotator time) | $8,000 - $15,000 | Scales with golden-set growth rate and calibration-set maintenance cadence, not with traffic |
| Eval/online-eval compute (workers) | $3,000 - $5,000 | Bursty — offline CI gates and canary-boosted sampling periods |

Judge spend and annotation cost are the two categories platform operators actively manage per-tenant (via budget ceilings, §9.5) because, unlike storage/compute, they scale with a *choice* (sample rate, golden-set ambition) rather than with raw traffic alone — which is exactly why they're also the two categories charged back to tenants rather than absorbed as flat platform overhead.

### 17.3 Optimization Strategies Summary

1. Sample aggressively by default, let teams opt up, not down, from a conservative baseline.
2. Cache everything content-addressable (payloads, judge calls on identical input).
3. Use the cheapest model that clears the calibration bar for each use case (online monitoring) and reserve expensive judges for low-volume, high-stakes gates.
4. Tier storage aggressively; almost all query volume is against recent data.
5. Batch judge calls through provider batch APIs for offline/async workloads.
6. Hard per-tenant budget ceilings everywhere spend can be triggered by traffic volume the platform doesn't control (online eval, canary evaluation).

### 17.4 Per-Tenant Cost Visibility

Every optimization lever above is only actionable if a tenant can actually see what's driving their own spend — the cost ledger (§13) backs a per-tenant cost dashboard mirroring the quality dashboard in §11.8:

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Team: support-eng      Agent: support-triage      Period: Aug 2026       │
├──────────────────────────────────────────────────────────────────────────┤
│  Model calls (generation)         $6,840    ████████████████████░░  82%  │
│  Judge calls (online, 2% sample)  $1,120    ████░░░░░░░░░░░░░░░░░░  13%  │
│  Judge calls (offline CI gates)     $310    █░░░░░░░░░░░░░░░░░░░░░░   4%  │
│  Storage (this agent's share)        $90    ░░░░░░░░░░░░░░░░░░░░░░   1%  │
├──────────────────────────────────────────────────────────────────────────┤
│  Total: $8,360   |   Budget: $10,000   |   Forecast (EOM): $9,180        │
│  Judge spend trending +18% week-over-week — driven by a canary raising   │
│  judge sampling from 2% → 20% on Aug 24 (see: active canary, §8.4)       │
└──────────────────────────────────────────────────────────────────────────┘
```

Surfacing *why* a cost trend moved (here, explicitly attributing the judge-spend increase to an active canary's temporarily boosted sampling rate, §8.7) rather than just showing the trend line is what turns this from a bill a tenant discovers after the fact into a dashboard that lets them make an informed trade-off in the moment — the same "aggregate metric needs a path to its evidence" principle from §11.8 applied to cost instead of quality.

---

## 18. Trade-offs

| Decision | Chosen approach | Alternative | Why |
|---|---|---|---|
| Full tracing vs. sampling | Tail-based sampling, 100% for errors/outliers, tunable base rate otherwise | Trace everything, always | 100%-always is not economically viable at target scale, and the traces sampling would drop are exactly the ones most valuable for debugging — tail-based captures the valuable minority cheaply |
| Real-time vs. batch eval | Both, serving different needs: online async sampling for fleet monitoring, offline batch for release gating | Pick one | Neither alone suffices — offline can't catch live drift, online can't give a clean pre-deploy gate against a fixed dataset |
| LLM-judge vs. human eval | LLM judge for volume, calibrated against a human-labeled set; human eval reserved for calibration sets and disputed/high-stakes cases | Human-only, or judge-only | Human-only doesn't scale to production traffic volume; judge-only has no ground truth to trust without calibration — the two are complementary, not competing |
| Centralized vs. embedded evaluation | Centralized service (Judge Service, Eval Engine) called by all tenants, not a library each team embeds and runs independently | Embedded per-team eval libraries | Centralizing means calibration, cost controls, and judge versioning are consistent and auditable platform-wide; an embedded model would let every team reinvent (and mis-calibrate) their own judge, recreating the exact fragmentation problem in the task's motivation |
| OTel extension vs. new protocol | Extend OTel semantic conventions | Bespoke LLM tracing protocol | Interoperability with existing tooling and instrumentation outweighs the flexibility of a purpose-built protocol |
| Auto-rollback default | Opt-in per tenant | Platform-wide automatic rollback on any regression | Some teams require a human decision on any prod prompt change regardless of statistical confidence; forcing automation removes that agency |
| Single-pass vs. per-criterion judging | Single-pass for high-volume online, per-criterion for offline gates | One approach everywhere | Cost and consistency pull in opposite directions; using different modes for different volume/stakes profiles gets both, at the cost of maintaining two code paths through the Judge Service |
| Write-time vs. read-time cost roll-up | Write-time (Span Processor computes roll-ups once, on ingest) | Read-time (recursive query at dashboard load) | Read-time is simpler to implement but makes every trace-tree view pay a recursive-query cost proportional to run size; write-time roll-up trades ingest-path complexity for flat, cheap reads — the right trade given reads (dashboards, debugging) vastly outnumber writes |
| Immutable, append-only versioning everywhere (prompts, datasets, judges) | Chosen uniformly | Mutable "latest" records with a separate audit log | A single mental model across every versioned entity means every subsystem's reproducibility story is the same story, at the storage cost of never deleting a version — accepted because the volume of prompt/dataset/judge versions is orders of magnitude lower than trace volume, so the storage cost is negligible relative to the reproducibility guarantee it buys |

### 18.1 What This Design Deliberately Does Not Build

* **No built-in guardrail/content-filtering enforcement layer** — online eval *observes and scores* production traffic, including safety/toxicity checks, but does not sit inline blocking a response before it reaches a user. A team needing synchronous pre-response filtering needs a separate, purpose-built guardrail service in their own request path; conflating "observe and alert" with "block in real time" would reintroduce the exact hot-path latency risk §2.3 is designed to avoid.
* **No cross-tenant benchmark leaderboard** — golden datasets and eval results are tenant-scoped by design (§1 assumption on structural isolation); a platform-wide "which team's agent scores highest" view was deliberately excluded, since it would create an incentive to game eval metrics rather than treat them as an honest regression signal.
* **No automatic prompt optimization (prompt rewriting)** — the platform tells you *that* a metric regressed and *what evidence* supports that, but does not attempt to auto-rewrite a prompt to fix it; that capability is explicitly deferred to the v4 evolution stage (§19) as a distinct, higher-risk capability that needs its own trust-building period once the measurement layer beneath it is proven reliable.

### 18.2 Sensitivity to Changed Constraints

The choices above are optimized for the stated assumptions (§1: 80+ internal tenants, 6-8 person platform team, mixed compliance postures). It's worth being explicit about what would change if those assumptions didn't hold, since a design that silently bakes in unstated assumptions is harder to evolve later:

* **If this became an external multi-customer SaaS product** rather than an internal platform, the "structural, not filtered" tenant isolation principle (§1) would need to extend further — likely full data-plane isolation (separate ClickHouse clusters or at minimum separate storage volumes per customer, not just a `tenant_id` column) to meet the stronger security guarantees external customers typically require, and the cost-chargeback mechanism (§17) would need to become customer-facing billing rather than internal chargeback, with the accuracy and auditability bar that implies.
* **If the platform team were 2 engineers instead of 6-8**, the v1→v4 evolution path (§19) would need to slow down further and the auto-rollback/auto-promotion automation in v4 would likely need to stay permanently opt-in rather than becoming a natural default even for mature tenants — small teams can't absorb the incident-response burden of automation they don't fully trust yet, and building that trust takes calendar time regardless of headcount.
* **If judge-model costs dropped by an order of magnitude** (a real possibility given the pace of model pricing changes), the cost-driven trade-offs in §9.4 and §17 would shift meaningfully — online sampling rates could rise toward 100% for more tenants, narrowing the gap between "cheap high-volume judge" and "expensive low-volume judge" to the point where maintaining two separate judge-model tiers might no longer be worth the operational complexity, simplifying §9.4's table to a single default judge for most use cases.
* **If regulatory requirements tightened** (e.g., a jurisdiction mandating human review of every automated quality decision affecting a customer-facing response), the online-eval-to-auto-rollback path (§8.5) would need a mandatory human-approval gate inserted regardless of tenant opt-in preference — the current opt-in model assumes tenants are free to choose their own risk tolerance, which a regulatory floor could override for specific data categories or geographies.

---

## 19. Evolution Path

| Version | Scope | Rationale |
|---|---|---|
| **v1** | Tracing (OTel-compatible ingestion, trace store, basic dashboard) + Prompt Versioning (registry, deploy, rollback) | Delivers immediate value with the smallest surface: teams get visibility and safe prompt iteration without any eval investment yet. Independently adoptable per the task's incremental-adoption constraint. |
| **v2** | + Dataset Management + Offline Evaluation Engine (batch eval, metrics, CI gates) | Once teams have traces, production-sampled datasets become possible; offline eval turns "did this prompt change help" from a guess into a gated, measured decision. |
| **v3** | + Online Evaluation + LLM-as-Judge (calibrated, versioned) + Regression Detection + Human Feedback | Closes the loop from "we can measure a batch" to "we're continuously monitoring production and get alerted on drift," with judge trust established via calibration rather than assumed. |
| **v4** | Automated optimization loop: regression-triggered auto-rollback fully wired, feedback→fine-tuning pipeline live, canary promotion automation, judge self-recalibration on a schedule with drift alerting | The system closes the loop end-to-end — a quality regression is caught, attributed, and (where a tenant opts in) reverted without a human in the critical path, while every automated decision remains auditable back to the evidence that triggered it. |

Each version is a strict superset — a v1 adopter's tracing and prompt data becomes the raw material v2's datasets and v3's online eval consume, so no team is forced to re-instrument when the platform grows around them.

### 19.1 What Ships in Each Version, Concretely

**v1 — Tracing + Prompt Versioning**
* Components: Trace Collector, Span Processor (enrichment + head-based sampling only — tail-based buffering deferred, it's an optimization not a correctness requirement), Trace Store, Prompt Registry (versions + deployments + rollback, no A/B traffic split yet — that needs the canary machinery from v3).
* Explicitly deferred: dataset management, any form of eval, judge, feedback collection.
* Success criterion for graduating to v2: at least a handful of pilot teams have real production trace volume and have used rollback at least once in anger — proof the core mechanism is trustworthy before eval is built on top of it.

**v2 — + Dataset Management + Offline Evaluation**
* Adds: Dataset Service (all three tiers, but production-sampling pipelines can launch with manual CSV import only — the automated sampling-rule engine of §6.3 is a v2.1 refinement once teams have datasets to seed it from), Offline Eval Engine, CI gate integration.
* Explicitly deferred: LLM-as-judge (offline metrics launch with reference-based metrics only — exact match, embedding similarity — deferring the judge-trust problem until v3 gives it a dedicated calibration mechanism rather than shipping an uncalibrated judge early).
* Success criterion: at least one team has blocked a real merge on a real eval-gate regression — proof the gate has teeth, not just a dashboard nobody reads.

**v3 — + Online Evaluation + LLM-as-Judge + Regression Detection + Human Feedback**
* Adds: tail-based sampling now justified (online eval needs the error/outlier-preserving trace selection it provides), Judge Service with calibration from day one (never launched uncalibrated, per §9.3's re-earn-trust-on-every-change principle), Regression Detector with alerting (auto-rollback still off by default — opt-in only, per §18), Feedback collection SDK and annotation queues.
* This is the version where the platform's core promise — "catch a regression before a human notices" — first becomes true end to end; it's also the largest single jump in operational surface area, which is why it's sequenced after v1/v2 have already proven the foundational mechanisms under real load.
* Success criterion: a canary comparison has correctly flagged a real regression before 100% rollout, with the evidence trail (§8.4-8.5) good enough that the team trusted the flag without re-deriving the analysis by hand.

**v4 — Automated Optimization Loop**
* Adds: auto-rollback fully wired for tenants who opt in, feedback→fine-tuning export pipeline (with its separate consent gate, §10.6) live, canary auto-promotion, scheduled judge self-recalibration with drift alerting (§16.3) running continuously rather than on manual trigger.
* Explicitly still deferred (see §18.1): automatic prompt rewriting/optimization — v4 closes the *measurement and reaction* loop (detect, alert, revert), not a *generation* loop (auto-fix the prompt itself), which remains a distinct, higher-risk capability layered on top only once v4's measurement layer has an established trust record.
* Success criterion: an auto-rollback has fired correctly in production with no human in the loop, and the postmortem confirms the reverted version really was worse — the bar for this version isn't "the automation exists," it's "the automation was trusted enough to be turned on, and it was right."

---

## 20. Exercises

1. **Trace model extension.** Design the span schema and rollup logic for a new span kind, `human_handoff` — an agent run that escalates to a human agent mid-conversation. What terminal state does the `AgentRun` get, and how does cost/token attribution change when part of the "run" involves no LLM calls at all?

2. **Prompt registry consistency.** The registry's edge cache (§5.6) can serve a stale prompt version for a few seconds after a deploy due to pub/sub propagation delay. Design a mechanism for a tenant that needs a deploy to be *immediately* consistent everywhere (e.g., a compliance-mandated emergency prompt kill-switch) without abandoning the cached-read performance model for normal deploys.

3. **Judge calibration set drift.** A judge's calibration score looks stable over months, but the *underlying production traffic distribution* has shifted (new categories of user queries the calibration set never covered). Design a mechanism to detect that a calibration set has gone stale relative to current production traffic, independent of the judge's measured kappa staying flat.

4. **Canary statistics under low traffic.** A low-volume agent (500 requests/day) wants canary evaluation, but the minimum-sample-size requirement (§8.4) means a canary would take weeks to reach a verdict at a 10% traffic split. Propose a design change (sequential testing, adaptive traffic allocation, or otherwise) that shortens time-to-verdict for low-volume agents without inflating false-positive rate.

5. **Multi-tenant judge cost fairness.** Design the budget-enforcement mechanism (§9.5, §17) so that one tenant's misconfigured 100%-sampling online judge cannot exhaust a shared judge-model provider quota that other tenants' judge calls also depend on — mirroring the noisy-neighbor problem from the NFRs, but for judge calls specifically rather than trace ingestion.

6. **Regression detector false-positive tuning.** Using the false-positive feedback loop described in §11.7, design the actual algorithm that adjusts a rule's sensitivity based on accumulated "marked as false positive" history — how many false-positive marks, over what window, should widen a threshold, and how do you avoid a rule being tuned into uselessness by a team that habitually dismisses real regressions?

7. **Cross-agent delegation cost attribution edge case.** Two teams each own an agent; Team A's agent delegates to Team B's agent as a sub-agent call (§4.1, §11 multi-agent orchestration). Design the cost/budget attribution so Team A's run-level budget enforcement (task NFR: max-step/budget ceilings) correctly accounts for cost incurred inside Team B's sub-agent, without requiring Team A's budget enforcer to have direct visibility into Team B's internal prompt/model configuration.

8. **Reproducibility audit.** Given an `EvalRun` record from six months ago, design the exact procedure (and note where it necessarily fails) for reconstructing "what would this eval have produced if run today" versus "what did it actually produce then" — accounting for prompt version pinning, dataset version pinning, judge version pinning, and the provider-side model non-determinism the task explicitly says cannot be eliminated.

9. **Self-preference bias detection.** §9.4.1 flags that a judge sharing a model family with the application it evaluates risks inflating scores through self-preference bias. Design a concrete detection mechanism: given historical calibration data, how would you measure whether a specific `(judge_model, application_model)` pairing shows this bias, and what should the platform do automatically when it's detected — block the pairing, just warn, or something else? Justify your choice against the cost/accuracy trade-offs in §9.4.

10. **Redaction policy change under active litigation hold.** A tenant tightens their `redaction_policies` row mid-quarter (§3.3.1), which triggers the retroactive resweep job — but a subset of their traces are under a legal hold requiring the *original*, unredacted content to be preserved for discovery, in direct tension with the new stricter policy. Design how `redaction_policies` and the resweep job need to change to support a hold-exception without weakening the redaction guarantee for every other trace the tenant owns.
</content>
