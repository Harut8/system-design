# ai-rag — Roadmap & Reading Map

Practice and new theory live here (`ai-rag/`). Most of the *supporting* theory already exists
elsewhere in this repo — vector index internals, OpenTelemetry, metrics storage, cost
attribution, async Python, measurement methodology. This file is the bridge: what to build,
in what order, and which existing document to read alongside each thing.

> **The rung-3 trap applies here too.** Reading about rerankers produces confident-sounding
> knowledge that collapses under one follow-up question. Every chapter in this folder exists
> to unblock a project in §4. If you are reading something that isn't unblocking a build,
> stop and go build.

---

## 1. Scope

The surface layer of RAG — wire an embedding model to a vector store, stuff results into a
prompt — is a two-week ramp and is not what this folder is about. The hard and durable part is
everything underneath:

| Surface layer | What this folder covers |
|---|---|
| "It returns relevant answers" | Golden sets, recall@k regression gates, faithfulness scoring |
| "It works" | OTEL traces per retrieval / rerank / generation / tool call |
| "It costs something" | Per-request, per-tenant, per-model token attribution |
| A framework call | Hybrid retrieval, reranking, index tuning, latency budgets |

The organising principle: **an LLM pipeline is a data system.** Correctness, measurability and
unit cost are engineering properties of it, not afterthoughts — and they are where the
difficulty actually lives.

---

## 2. What changed since 2023

RAG practice from 2023–24 is stale in specific, closable ways:

| 2023 practice | 2026 practice | Where it's covered |
|---|---|---|
| Eyeball the outputs | Golden sets, recall@k / MRR / nDCG, LLM-as-judge with calibration, CI regression gates | `08`, `09` |
| Fixed-size chunks | Semantic, contextual, late chunking; parent-document retrieval | `02` |
| Cosine similarity only | Hybrid BM25 + dense with RRF, cross-encoder reranking | `04` |
| Query goes straight to the index | Rewriting, decomposition, multi-query, HyDE | `05` |
| Stuff everything in the prompt | Context budgeting, compaction, citation, memory | `06` |
| Single-shot retrieve→generate | Agentic multi-hop retrieval, tool calling | `13`, `14` |
| No instrumentation | OTEL GenAI semantic conventions, span per LLM call, trace↔eval linkage | `10` |
| Cost noticed on the invoice | Per-request, per-tenant, per-model token accounting and budgets | `11` |
| "It's slow" | Prompt caching, batching, model routing, streaming, fallback | `12` |

Long context also changed the calculus — sometimes the answer is *less* RAG, and knowing when
is part of the job (`06`).

---

## 3. Chapter plan

Written as they're needed, not up front. Each one is a build-log with theory attached, which
keeps every claim honest by construction.

| Doc | Topic | Status |
|---|---|---|
| [`00-mental-models.md`](00-mental-models.md) | The retrieval→generation pipeline as a data system; where correctness actually lives | **written** — labs unrun |
| [`01-embeddings-and-representation.md`](01-embeddings-and-representation.md) | Model choice, dimensionality, domain adaptation, multilingual, drift | **written** — labs unrun |
| [`02-chunking-and-document-processing.md`](02-chunking-and-document-processing.md) | Parsing, chunk strategies, contextual/late chunking, parent-doc | **written** — labs unrun |
| `03-indexing-and-vector-stores.md` | HNSW parameters in anger, quantization, filtered search, pgvector vs dedicated | planned |
| `04-retrieval-hybrid-and-reranking.md` | BM25 + dense, RRF fusion, cross-encoder rerankers, latency budget | planned |
| `05-query-understanding.md` | Rewriting, decomposition, multi-query, HyDE, routing | planned |
| `06-context-engineering.md` | Window budgeting, compaction, citation, memory, long-context tradeoffs | planned |
| `07-generation-and-structured-output.md` | Structured outputs, schema validation, retries, determinism | planned |
| `08-evaluation-methodology.md` | Golden sets, recall@k / MRR / nDCG, faithfulness, LLM-as-judge calibration, significance | planned |
| `09-eval-infrastructure-and-ci.md` | Eval as a pipeline: datasets, versioning, regression gates, dashboards | planned |
| `10-llm-observability-and-tracing.md` | OTEL GenAI semconv, span design, trace↔eval linkage, sampling | planned |
| `11-token-accounting-and-cost.md` | Per-request/tenant/model attribution, budget enforcement, unit economics | planned |
| `12-serving-latency-and-caching.md` | Streaming, prompt caching, batching, model routing, fallback, timeouts | planned |
| `13-agents-and-tool-calling.md` | Tool schemas, multi-hop retrieval, planning, idempotency, failure handling | planned |
| `14-agent-evaluation.md` | Trajectory eval, task success, tool-call correctness, cost per resolved task | planned |
| `15-ingestion-pipelines-and-freshness.md` | Parsing at volume, dedup, incremental index updates, backfill, staleness SLOs | planned |
| `16-multi-tenancy-and-isolation.md` | Per-tenant indexes, noisy neighbours, quota, data isolation | planned |
| `17-safety-guardrails-and-prompt-injection.md` | Injection defence, output filtering, PII, tool-call authorization | planned |
| `18-failure-modes-and-incident-walkthrough.md` | End-to-end: a retrieval regression found, diagnosed, fixed | planned |
| `19-build-vs-buy.md` | Langfuse / LangSmith / Braintrust / Arize / Helicone landscape and when to build | planned |
| `appendix-a-glossary.md` | | planned |
| `appendix-b-metric-definitions.md` | Every eval and cost metric, with its exact formula | planned |
| `appendix-c-eval-recipe-book.md` | Copy-pasteable eval setups | planned |

---

## 4. Cross-reference map — theory already in this repo

**Do not rewrite any of this here.** Read it in place, then write only the AI-specific delta.
Roughly 60% of the depth this roadmap needs already exists in other folders.

### Retrieval & indexing → `../databases/`

| Need | Read |
|---|---|
| HNSW internals — graph construction, `M` / `efConstruction` / `efSearch`, recall-vs-latency | `../databases/11-hnsw-vector-search-internals.md` |
| Vector search broadly — IVF, PQ, quantization, ANN tradeoffs | `../databases/11-vector-search-internals.md` |
| Index structures generally — B-tree, inverted, bitmap; why an index is a tradeoff not a win | `../databases/06-indexing-internals.md` |
| Scans, selectivity, access-method choice — the theory under hybrid retrieval | `../databases/03-access-methods-and-table-scans.md` |
| Columnar formats & encoding — for eval datasets and trace storage in Parquet | `../databases/02-data-storage-formats-and-encoding.md` |
| Query engine internals — pushdown, joins; needed for filtered vector search | `../databases/04-query-engine-internals.md` |
| DuckDB / chDB in-process OLAP — the right tool for eval-result and cost analytics | `../databases/21-in-process-olap-duckdb-chdb.md` |
| OLAP fundamentals — for the cost/usage aggregation layer | `../databases/08-olap-databases.md` |
| SQL performance — for the query layer over eval and cost data | `../databases/15-sql-performance-deep-dive.md` |
| Storage engine + WAL — if a project persists its own index | `../databases/01-storage-engine-fundamentals.md`, `../databases/14-write-ahead-log-internals.md` |
| Replication — when the index shards | `../databases/12-replication-and-distributed-storage.md` |

### Observability & tracing → `../sre-observability/`

| Need | Read |
|---|---|
| **LLM & AI observability — the single most on-target document in the repo** | `../sre-observability/26-llm-and-ai-observability.md` |
| OpenTelemetry deep dive — the substrate for all LLM tracing | `../sre-observability/02-opentelemetry-deep-dive.md` |
| Instrumentation practice | `../sre-observability/03-instrumentation.md` |
| Trace storage — where spans go and what that costs | `../sre-observability/08-traces-storage.md` |
| Metrics storage — for token/latency/quality time series | `../sre-observability/06-metrics-storage.md` |
| Query layer — serving the dashboards over all of it | `../sre-observability/10-query-layer.md` |
| Semantic conventions governance — directly applicable to GenAI semconv | `../sre-observability/34-schema-and-semantic-conventions-governance.md` |
| Cardinality & cost — per-tenant/per-model labels explode fast | `../sre-observability/18-cardinality-and-cost.md` |
| FinOps for observability — the cost-attribution pattern, reused for tokens | `../sre-observability/31-finops-for-observability.md` |
| Telemetry lakehouse — where eval + trace + cost data lands for analysis | `../sre-observability/35-telemetry-lakehouse.md` |
| Pipeline reliability — the ingestion side must not lose data | `../sre-observability/28-telemetry-pipeline-reliability.md` |
| Python observability | `../sre-observability/42-python-observability.md` |
| Alerting & SLOs — for retrieval-quality SLOs, which almost nobody defines | `../sre-observability/12-alerting.md`, `../sre-observability/13-slo-engineering.md` |
| Multi-tenancy in telemetry | `../sre-observability/19-multi-tenancy.md` |

### Inference & GPU economics → `../gpu-observability/`

Relevant only for self-hosted inference; skip for API-based work.

| Need | Read |
|---|---|
| LLM inference observability — TTFT, ITL, batching, queueing | `../gpu-observability/14-llm-inference-observability.md` |
| Capacity planning & cost optimization | `../gpu-observability/12-capacity-planning-and-cost-optimization.md` |
| Allocation vs utilization semantics | `../gpu-observability/05-gpu-allocation-and-utilization-efficiency.md` |
| Prometheus metric design & cardinality | `../gpu-observability/08-prometheus-metrics-design-and-cardinality.md` |
| Telemetry lakehouse + SQL analytics | `../gpu-observability/17-telemetry-lakehouse-and-sql-analytics.md` |
| Multi-tenant GPU observability | `../gpu-observability/13-multi-tenant-gpu-observability.md` |

### Engineering craft → `../python-mastery/`

| Need | Read |
|---|---|
| **Measurement methodology — the rigour that makes eval numbers defensible** | `../python-mastery/31-measurement-methodology.md` |
| Testing strategy — evals are tests; this is how they're structured | `../python-mastery/43-testing-strategy.md` |
| asyncio internals — every LLM pipeline is I/O-bound and concurrent | `../python-mastery/28-asyncio-internals.md` |
| Async patterns and pitfalls — bounded concurrency, backpressure, cancellation | `../python-mastery/29-async-patterns-and-pitfalls.md` |
| Concurrency correctness | `../python-mastery/30-concurrency-correctness.md` |
| Profiling — when the pipeline is slow and it isn't the model | `../python-mastery/32-profiling.md` |

`31-measurement-methodology.md` is the highest-leverage file in the repo for this track. Eval
credibility *is* measurement methodology: an accuracy number you cannot explain the derivation
of is worth less than no number at all.

### Distribution & deployment

| Need | Read |
|---|---|
| Distributed systems foundations — for a sharded/replicated index | `../distributed-systems/README.md` |
| Failure detection & leader election | `../databases/16-failure-detection-and-leader-election.md` |
| Working reference implementations | `../databases/failure_detection_phi_accrual.py`, `../databases/failure_detection_gossip.py` |
| Scale-tiered service patterns to imitate | `../implementation/distributed-counter/`, `../implementation/fastapi-rbac/` |
| Deployment, when it's time | `../k8s-learn/README.md` (Track A only — do not detour into Track C) |

---

## 5. Project ladder

Build-driven. Each project defines what to read next; nothing is read speculatively.

### P0 — Eval harness *(do this first)*

A golden query set over a real corpus, with recall@k, MRR, nDCG, and a faithfulness check.
Versioned datasets, results in DuckDB, a regression gate that fails CI when retrieval quality
drops.

*Why first:* every later project needs it to prove anything at all. Without it, each
subsequent change is a guess. It is also the part of the stack most people skip, which is why
most RAG systems cannot answer "did that change help?"

Reads: `../python-mastery/31-measurement-methodology.md`, `../python-mastery/43-testing-strategy.md`,
`../databases/21-in-process-olap-duckdb-chdb.md`.

### P1 — Retrieval service

Hybrid BM25 + dense with RRF fusion and a cross-encoder reranker, behind an API. Every change
benchmarked against P0. Keep a written log of what moved the numbers and what didn't — the
negative results are the valuable half.

Reads: `../databases/11-hnsw-vector-search-internals.md`, `../databases/06-indexing-internals.md`,
`../databases/03-access-methods-and-table-scans.md`.

### P2 — Traced and costed

OTEL instrumentation of P1: span per retrieval / rerank / generation / tool call, GenAI
semantic conventions, token counts as metrics, per-tenant and per-model cost attribution, a
dashboard showing quality and cost on the same screen.

*Why it matters:* quality and cost are a single tradeoff surface, and almost no RAG system
exposes both. This is the layer that turns a demo into something operable.

Reads: `../sre-observability/26-llm-and-ai-observability.md`, `../sre-observability/02-opentelemetry-deep-dive.md`,
`../sre-observability/31-finops-for-observability.md`, `../sre-observability/18-cardinality-and-cost.md`.

### P3 — Agentic retrieval

One agent with real tool calls, multi-hop retrieval, retries, structured failure handling —
plus a trajectory eval suite measuring task success, tool-call correctness, and cost per
resolved task. Keep it small and make it *reliable*, not impressive.

Reads: `../python-mastery/29-async-patterns-and-pitfalls.md`, `../python-mastery/30-concurrency-correctness.md`.

### P4 — Flagship: retrieval & eval data platform

P0–P3 assembled into a substrate other engineers could build on: multi-tenant ingestion →
incremental indexing → serving → measurement → cost attribution. Sharded and replicated once
single-node works.

*Why one flagship rather than five small projects:* only a flagship survives follow-up
questions. Small projects demonstrate that you can follow a tutorial; a system with tenants,
failure modes and a cost model demonstrates that you can design one.

Reads: the distributed and storage rows in §4, pulled in as each becomes load-bearing.

---

## 6. Rung ledger

Each artifact sits on exactly one rung, and each rung gets its own verb. Blurring them is the
only thing here that would make a claim dishonest.

| Artifact | Rung | How to describe it |
|---|---|---|
| P0–P4 code | **2 — implemented** | "A project I built." No caveat needed. |
| Numbers produced *by* P0 | **1 — measured** | Always state the dataset, its size, and what counted as a hit. |
| Docs in this folder | **3 — studied**, unless written as build-logs | Build-logs — "here's what I built and what the numbers did" — are rung 2 by construction |

Any accuracy or latency figure you quote must come with a one-sentence account of how it was
measured. If that sentence doesn't exist, cut the number.

---

## 7. Deliberately out of scope

Named so they don't creep in:

- **Fine-tuning, LoRA, RLHF** — a different job family. Learn the *decision boundary* (when
  fine-tuning beats retrieval) in `01`; skip the practice.
- **Training infrastructure, distributed training** — requires cluster access this roadmap
  assumes you don't have.
- **CUDA, kernels, quantization implementation** — adjacent-sounding, entirely different skill
  set.
- **Model architecture and research** — not the job this folder trains for.
- **`../kubernetes/` beyond deployment basics** — 46 files at ~130KB each, the most expensive
  directory in the repo and the least load-bearing here. If it becomes relevant, enter via
  `../k8s-learn/controller-tasks.md` and `operator-tasks.md` only.
