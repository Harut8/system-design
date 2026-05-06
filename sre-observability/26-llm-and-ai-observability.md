# 26 — LLM and AI Observability

> Observability for AI workloads breaks every assumption of traditional observability. The output of the system isn't a status code or a number — it's *text*. The cost isn't bytes or CPU — it's *tokens*. The failure mode isn't an exception — it's a *plausible wrong answer*. By 2026, every observability platform must integrate LLM-specific signals or it's missing the most expensive workload in the org.

This chapter is about LLM-application and ML-model observability — token accounting, drift, eval harnesses, hallucination signals, prompt traces, vector DB observability, and the cost-control discipline that the other 25 chapters' patterns must extend to cover.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [Why LLM observability is different](#2-why-different)
3. [The LLM signal taxonomy](#3-llm-signals)
4. [Token accounting (the dominant cost driver)](#4-token-accounting)
5. [Latency and TTFT in LLM apps](#5-latency-ttft)
6. [Quality signals: evals, ground truth, human feedback](#6-quality)
7. [Hallucination and faithfulness](#7-hallucination)
8. [Prompt and completion logging](#8-prompt-logging)
9. [Tool-use and agent traces](#9-agent-traces)
10. [Vector DB observability (RAG retrieval)](#10-vector-db)
11. [Drift: model, data, concept](#11-drift)
12. [PII in prompts and completions](#12-pii)
13. [Caching, batching, and cost optimization](#13-caching)
14. [Per-vendor specifics: OpenAI, Anthropic, self-hosted](#14-vendor)
15. [LLM SLOs](#15-slos)
16. [Anti-patterns](#16-anti-patterns)
17. [Worked example: a RAG application with full observability](#17-worked-example)
18. [Pitfalls](#18-pitfalls)
19. [Mental models](#19-mental-models)

---

## 1. Thesis

Three claims:

1. **Cost is the first-order observability concern.** A naive LLM application can spend $10k/day where a comparable non-LLM app spends $10. Token accounting per request, per user, per feature is non-negotiable.
2. **Quality is observable but only with labeled data.** "Was that answer correct?" requires ground truth. Build an eval harness from day one; treat it as a CI dependency.
3. **The trace is the unit of LLM debugging.** A single user turn = one trace = potentially many model calls + retrievals + tool uses + retries. Without proper tracing, you debug by guessing.

If your team is shipping LLM features and your only observability is "the API call succeeded," you'll discover at month 2 that you're spending $250k/month on the wrong things and can't measure quality regressions. This chapter is the instrumentation discipline.

---

## 2. Why LLM Observability Is Different

| Dimension | Traditional | LLM |
|---|---|---|
| Output type | Structured | Natural language |
| Cost driver | CPU, memory | Tokens (input + output) |
| Latency | Fixed-ish per endpoint | Variable; depends on prompt + completion length |
| Errors | HTTP 5xx | "Plausible but wrong"; refusals; hallucinations |
| Quality | Schema validity | Semantic correctness; grounded-ness |
| Reproducibility | Deterministic | Probabilistic (temperature > 0) |
| Latency floor | Network + compute | TTFT + tokens-per-second |
| Failure modes | Exception | Silent wrong answer |

Each of these reshapes the instrumentation.

---

## 3. The LLM Signal Taxonomy

| Signal | What it measures |
|---|---|
| **Token usage** | Input tokens, output tokens, by model, by prompt template, by user |
| **Latency** | TTFT (time-to-first-token), total latency, tokens-per-second |
| **Cost** | Tokens × per-token rate; aggregated by tenant / feature / user |
| **Quality** | Eval scores (against ground truth); user feedback; grounded-ness |
| **Errors** | Retries, refusals, content-policy blocks, exceptions |
| **Drift** | Distribution shift in inputs, outputs, embeddings |
| **Tool / agent metadata** | Tools called, retrieval counts, hop counts |

Each is necessary; together they answer "is this LLM feature working, and at what cost?"

---

## 4. Token Accounting

The cost layer.

### 4.1 The metrics

```
llm_tokens_input_total{model, feature, tenant}
llm_tokens_output_total{model, feature, tenant}
llm_tokens_cached_total{model, feature, tenant}     # if using prompt caching
llm_cost_dollars_total{model, feature, tenant}
```

Per request, capture token counts. Sum to get aggregate spend.

### 4.2 The cost calculation

```
cost = (input_tokens × price_per_input_token)
     + (output_tokens × price_per_output_token)
     - (cached_input_tokens × price_per_cached_token discount)
```

Output tokens are typically 3-5× more expensive than input. Caching is typically 10× cheaper than fresh input. **The cost difference between a cached and uncached prompt is enormous; instrument cache hits explicitly.**

### 4.3 The chargeback dashboard

```
LLM cost by feature (last 30d)
─────────────────────────────────
chatbot               $42,000
summarization          $8,000
RAG search             $6,500
auto-tagging           $1,200
─────────────────────────────────
total                 $57,700

Top contributors:
  - chatbot: 1.8M sessions × ~9000 tokens = 16B tokens
  - summarization: 200K docs × ~5000 tokens = 1B tokens
```

Per-feature, per-tenant attribution. Without this, the AI bill is opaque and grows unchecked.

### 4.4 The "growth in tokens per session" signal

A subtle regression: a feature drifts to longer conversations / longer context windows. Tokens per session creep up; cost grows; nobody notices.

```
llm_tokens_per_session_p95{feature}
```

Tracked over time; alert on regressions.

### 4.5 Prompt caching observability

Modern LLM APIs support prompt caching (Anthropic's prompt caching, OpenAI's structured caching). Hits are dramatically cheaper.

```
llm_cache_hit_rate_total{model}
```

Target: > 70% for repeated-prompt workloads. Lower indicates caching is misconfigured (cache breakpoints wrong, prefix not stable).

---

## 5. Latency and TTFT in LLM Apps

### 5.1 What's different

LLM responses arrive as a stream. Two latency metrics matter:

- **TTFT (time to first token).** How long before the user sees something.
- **Total latency.** Time to complete generation.
- **Tokens per second (throughput).** During the streaming portion.

### 5.2 The metrics

```
llm_ttft_seconds_bucket{model, feature}
llm_total_latency_seconds_bucket{model, feature}
llm_tokens_per_second{model, feature}
```

TTFT is the user-perceived responsiveness; total latency dominates the cost (longer = more tokens = more $).

### 5.3 The streaming UX

For streaming responses:
- **TTFT < 1s** for chat-like features.
- **Tokens/sec > 30** for readable streaming.

These are user-experience SLIs.

### 5.4 The retry impact

LLM calls retry: rate limits, transient errors, tool-use timeouts. Retries multiply latency.

```
llm_retries_total{reason}
llm_attempts_per_request_bucket
```

Multi-retry latency is the user pain; instrument it.

### 5.5 The model-router cascade

A common pattern: try fast/cheap model first; fall back to slower/more-expensive on failure or quality threshold.

```
llm_router_decisions_total{from_model, to_model, reason}
```

Visibility into which models are getting hit, when, and why.

---

## 6. Quality Signals: Evals, Ground Truth, Human Feedback

The hardest part.

### 6.1 The eval harness

A test suite of (input, expected_output) pairs that runs continuously:
- On every model upgrade.
- On every prompt change.
- On a schedule for the live model.

Outputs:
- **Pass rate** (binary correctness on test cases).
- **Score** (graded — 0-1 or 0-5 — for tasks without single right answer).
- **Latency / cost** for the eval.

```
eval_pass_rate{eval_suite, model_version}
eval_avg_score{eval_suite, model_version}
```

### 6.2 The eval-as-CI pattern

```
PR proposes prompt change
   ↓
CI runs eval suite (200-2000 cases)
   ↓
Compare to baseline:
   - Pass rate degraded?
   - Score declined?
   - Cost increased?
   ↓
Block merge if regression > threshold.
```

This is the LLM equivalent of unit tests. Skipping it = shipping regressions.

### 6.3 Tools

| Tool | Strength |
|---|---|
| **OpenAI Evals** | Well-known; integration with OpenAI |
| **Promptfoo** | Open-source; YAML-based; dev-friendly |
| **LangSmith** | Tracing + evals; LangChain ecosystem |
| **Helicone** | Tracing + cost + evals (open-source) |
| **Braintrust** | Eval-focused; rich UI |
| **Weights & Biases** | ML-tracking-style; evals + experiments |

### 6.4 Human feedback

For tasks without programmatic ground truth:
- Thumbs up / thumbs down on responses (in-product).
- Manual review queue (sample 1% of responses; rate them).

```
user_feedback_positive_total{feature}
user_feedback_negative_total{feature}
```

A drop in positive rate = quality regression. Alert on it.

### 6.5 The LLM-as-judge pattern

For graded scoring without humans: use a stronger model to judge a weaker model's outputs.

```python
score = judge_model.evaluate(
    input=user_query,
    output=weak_model_response,
    rubric="..."
)
```

Caveats: the judge has biases; LLM-as-judge correlates with but isn't identical to human judgment. Calibrate periodically with human samples.

---

## 7. Hallucination and Faithfulness

The hardest LLM-specific failure mode.

### 7.1 What hallucination looks like

The model generates plausible but factually wrong content. Particularly bad in:
- Customer-facing answers.
- Citation tasks (made-up sources).
- Code generation (made-up APIs).

### 7.2 Detection signals

For RAG (retrieval-augmented generation):
- **Faithfulness:** does the answer rely *only* on retrieved context?
- **Citation correctness:** are cited sources real?
- **Coverage:** does the answer cite *any* retrieved context?

```
rag_faithfulness_score{feature}            # 0-1, computed by judge
rag_citation_correctness_rate{feature}
rag_uncited_answer_rate{feature}            # answers without citations
```

Tools: RAGAS, TruLens, custom judge prompts.

### 7.3 Confidence signals

Some models emit log-probabilities or expressed confidence. Useful for:
- Routing low-confidence cases to human review.
- Surfacing uncertainty to the user.

```
llm_completion_logprob_avg{feature}
```

Lower aggregate logprob = model is less certain.

### 7.4 The "I don't know" rate

Well-instructed models say "I don't know" when appropriate. The rate is a quality signal:
- Too low: the model is overconfident (hallucinating).
- Too high: the model is underutilizing context.

Instrument explicitly.

---

## 8. Prompt and Completion Logging

Capture the inputs and outputs for debugging.

### 8.1 What to log

Per LLM call:
- The full prompt (rendered).
- The completion.
- The model and version.
- All parameters (temperature, max_tokens, etc.).
- Tokens (input, output, cached).
- Latency and TTFT.
- Cost.
- User / tenant / feature.
- Eval / quality scores if available.
- Trace context.

### 8.2 The volume / cost trade-off

Full prompt + completion logging is *expensive*. A typical LLM app generates 10x the log volume of a non-LLM app. Strategies:

- Sample (e.g., 100% of errors, 100% of low-confidence, 5% of normal).
- Truncate long completions.
- Compress (text compresses well; zstd at the agent).
- Redact PII before storing.

### 8.3 The replay value

Logged prompts + completions enable:
- Debugging quality regressions ("this answer was wrong; let me see the prompt").
- Building eval cases ("real-world failures become test fixtures").
- Auditing for compliance ("what did we tell users about X?").

The investment pays for itself.

### 8.4 The PII concern

Prompts contain user data. Completions may contain user data. Both are now *stored* — the data-protection regime applies.

Defenses:
- Redact at the source (PII detection regex / NER).
- Encrypt at rest.
- Per-tenant retention.
- Audit query access.

---

## 9. Tool-Use and Agent Traces

The trace structure for agentic systems.

### 9.1 The trace shape

```
User turn (root span)
  ├── retrieval_call (RAG fetch)
  │     └── vector_db_search
  ├── llm_call_1 (initial response)
  ├── tool_call: search_web
  ├── llm_call_2 (refine with web result)
  ├── tool_call: calculate
  ├── llm_call_3 (synthesize)
  └── llm_call_4 (final response)
```

A single user turn = many spans. The trace reveals the agent's reasoning path.

### 9.2 The OTel GenAI semantic conventions

OTel's GenAI conventions (stabilizing through 2025-2026) define attributes:

```
gen_ai.system           = "openai" | "anthropic" | "self-hosted"
gen_ai.request.model    = "gpt-4-turbo" | "claude-opus-4-7"
gen_ai.usage.input_tokens
gen_ai.usage.output_tokens
gen_ai.response.id
gen_ai.response.finish_reasons
```

Use these. Tools (Tempo, Honeycomb, Datadog) recognize them.

### 9.3 Tool-call tracing

Each tool call is its own span:

```
gen_ai.tool.name
gen_ai.tool.args
gen_ai.tool.result.summary    (truncated)
```

Lets you debug "why did the agent call this tool with these args?"

### 9.4 Agent loops

A pathological agent gets stuck in a loop (call same tool repeatedly with same args). Detection:

```
agent_iterations_per_turn_bucket
agent_repeated_tool_calls_total
```

Alert on agent loops; cap iteration count; raise to user when exceeded.

---

## 10. Vector DB Observability (RAG Retrieval)

The data substrate for RAG.

### 10.1 The metrics

```
vector_search_latency_seconds_bucket
vector_search_results_count
vector_index_size_bytes
embedding_generation_latency_seconds
embedding_dimension                          # configuration sanity
```

### 10.2 The recall signal

Retrieval quality: did the search return the *right* documents?

For a labeled eval set:
```
recall_at_k = (relevant docs in top-k) / (relevant docs total)
```

Track per query class; regressions indicate index drift, embedding model change, or query degradation.

### 10.3 The freshness signal

For dynamic content (e.g., user-generated docs added in real time):

```
embedding_index_age_seconds                  # how stale
embedding_indexing_lag_seconds                # how far behind
```

Stale indices return outdated results. Alert.

### 10.4 Per-engine

| Vector DB | Notes |
|---|---|
| **Pinecone** | Managed; metrics via Pinecone-Cloud |
| **pgvector / Postgres** | Standard Postgres metrics + index stats |
| **Weaviate** | Native Prom endpoint |
| **Milvus / Zilliz** | Native metrics |
| **Qdrant** | Native metrics |
| **Elastic / OpenSearch with vector** | Standard ES metrics + hybrid-search-specific |

### 10.5 The hybrid-search story

Most production RAG combines vector + keyword search. Per-strategy metrics tell you which contributes most:

```
hybrid_search_strategy_total{strategy="vector"}
hybrid_search_strategy_total{strategy="keyword"}
hybrid_search_strategy_total{strategy="combined"}
```

Tuning the blend matters; observability shows which side helps.

---

## 11. Drift: Model, Data, Concept

The slow degradation problem.

### 11.1 Model drift

The vendor updates the model. Same prompt produces different outputs.

```
model_version{api}
model_response_distribution_shift
```

Alert on version changes. Re-run evals on update.

### 11.2 Data drift (input distribution shift)

User behavior changes. Inputs are different from training/calibration data. Quality silently degrades.

```
input_token_count_distribution
input_topic_distribution           (semantic clustering of inputs)
```

Compare current to historical. Alert on significant shift.

### 11.3 Concept drift

The underlying truth changes (regulations, prices, names). Static knowledge in the model becomes wrong.

This is hard to detect automatically. Best signal: human-feedback negative-rate increase on classes of question.

### 11.4 Embedding drift

For RAG: re-embedding old documents with a new model gives different vectors. Old documents become unsearchable.

Don't change embedding models lightly. When you must, fully re-embed and version explicitly.

---

## 12. PII in Prompts and Completions

The data-protection layer.

### 12.1 What's in scope

- User-provided text in prompts (PII volunteered).
- Retrieved documents in RAG context (PII present in the corpus).
- Completions (PII the model surfaces).
- Logs of all the above.
- Embeddings (which can leak content).

### 12.2 Defenses

- **Redact at the source.** PII detection (regex + NER models) before storing or sending.
- **Per-tenant boundaries.** No cross-tenant data leak via shared model context.
- **Encryption at rest and in transit.**
- **Audit logs of LLM calls.** Who queried what, when.
- **Right-to-erasure for embeddings + logs.** GDPR.
- **Restrict logging in regulated tenants** (HIPAA, PCI: aggressive redaction or full opt-out).

### 12.3 The "zero-retention" provider option

Some LLM vendors offer zero-retention modes: data isn't retained, used for training, or logged. Use these for regulated workloads.

### 12.4 The training-data-leak concern

Prompts sent to a vendor *might* be used for training (vendor-dependent; check the contract). Sensitive data → use zero-retention or self-hosted.

---

## 13. Caching, Batching, and Cost Optimization

The 2026 cost-control toolkit.

### 13.1 Prompt caching

Models like Anthropic Claude and OpenAI GPT-4o support prompt caching: stable prefixes are cached at the model side; subsequent requests with the same prefix pay 10× less.

```
llm_cache_hit_rate{model}
llm_cost_savings_from_cache_total
```

Effective when system prompts or RAG contexts are reused across requests.

### 13.2 Batching

For non-realtime workloads, batch APIs (OpenAI Batch, Anthropic Batch, etc.) offer 50% cost discounts with 24-hour SLA.

```
batch_jobs_pending
batch_completion_latency_hours
batch_cost_savings_total
```

### 13.3 Model routing

Route easy queries to cheap models; hard queries to expensive ones. Quality maintained; cost reduced.

```
router_decisions{difficulty, model_chosen}
router_savings_dollars_total
```

### 13.4 Output length control

Output tokens are the dominant cost. `max_tokens` parameter, instructions for brevity, post-processing truncation.

```
completion_length_bucket
truncated_responses_total
```

Track; tune.

---

## 14. Per-Vendor Specifics

### 14.1 OpenAI

- Headers expose `x-ratelimit-*` for rate-limit observability.
- `response.usage` has token counts.
- Recommended: OpenTelemetry's OpenAI instrumentation library.

### 14.2 Anthropic Claude

- Rate-limit headers similar.
- Prompt caching with explicit cache breakpoints.
- Response includes detailed token breakdown (cached vs uncached).
- Anthropic OTel instrumentation library.

### 14.3 Self-hosted (Llama, Mistral, etc.)

- vLLM, Triton, TGI all expose Prom-compatible metrics.
- GPU metrics matter (DCGM exporter; see `gpu-observability/` sister folder).
- Model load time, prefill latency, KV-cache utilization.

### 14.4 Inference frameworks

- **vLLM:** continuous batching; KV-cache stats.
- **TGI** (Text Generation Inference, Hugging Face): rich metrics endpoint.
- **Triton:** NVIDIA's; metrics via Prometheus.
- **llama.cpp:** simpler; basic metrics.

### 14.5 Aggregator platforms

- **LangSmith:** LangChain ecosystem; tracing + evals.
- **Langfuse:** open-source LLM observability platform.
- **Helicone:** open-source proxy + dashboards.
- **Phoenix (Arize):** ML observability with LLM additions.
- **Datadog LLM Observability:** vendor product.
- **OpenLLMetry / Traceloop:** OTel-compatible LLM instrumentation.

The 2026 trend: OTel GenAI conventions becoming the unifying layer, with tools layering on top.

---

## 15. LLM SLOs

The shapes that work.

### 15.1 Quality SLO

```yaml
- name: chatbot_eval_pass_rate
  metric: eval_pass_rate{suite="production"}
  target: 0.85
```

### 15.2 Latency SLO

```yaml
- name: chatbot_ttft_under_2s
  metric: llm_ttft_seconds_bucket{le="2.0"}
  target: 0.99
```

### 15.3 Cost SLO

```yaml
- name: cost_per_session
  metric: avg(llm_cost_per_session{feature="chatbot"})
  target: < 0.05  # under $0.05 per session
```

### 15.4 User-feedback SLO

```yaml
- name: positive_feedback_rate
  metric: user_feedback_positive_total / user_feedback_total
  target: 0.85
```

### 15.5 Error SLO

```yaml
- name: llm_request_success
  metric: llm_calls_success_total / llm_calls_total
  target: 0.999
```

The five shapes cover most LLM-feature SLOs. Plus per-feature drift / freshness / hallucination SLOs as appropriate.

---

## 16. Anti-Patterns

1. **No token accounting.** Cost surprises; finance reactive.
2. **No quality eval.** Regressions ship.
3. **No prompt logging.** Debugging by guesswork.
4. **No RAG faithfulness signal.** Hallucination invisible.
5. **No tool-call tracing.** Agent loops invisible.
6. **No cache hit-rate metric.** Wasted spend.
7. **No drift detection.** Slow quality decay.
8. **PII unhandled.** Compliance violation.
9. **No per-feature attribution.** Cost blame impossible.
10. **No model-router observability.** Cascade decisions opaque.
11. **No vector-db latency SLO.** Retrieval problems invisible.
12. **No version label on model.** Vendor updates surprise.
13. **No batch / streaming differentiation.** TTFT confused with total latency.
14. **No human-feedback pipeline.** Quality unmeasurable for non-grading tasks.
15. **Trace-ignorant LLM clients.** Context lost across calls.

---

## 17. Worked Example: A RAG Application With Full Observability

Concrete and complete.

### 17.1 The application

`docs-chatbot`. Customer support chatbot. Retrieval-augmented:
1. User asks question.
2. Embed the question.
3. Search vector DB (pgvector) for relevant docs.
4. Construct prompt: system + retrieved docs + user question.
5. Call Claude Opus 4.7 with prompt caching on the system prompt.
6. Stream response to user.
7. After response, log; collect feedback.

### 17.2 Trace structure

```
chatbot.turn (root)
  ├── embed_question (latency: 50ms, model: text-embedding-3-large)
  ├── vector_search (latency: 80ms, results: 5)
  ├── llm_call (model: claude-opus-4-7)
  │     ├── ttft: 600ms
  │     ├── total: 4.2s
  │     ├── input_tokens: 8200 (cached: 7800)
  │     ├── output_tokens: 220
  │     └── cost: $0.012
  └── log_and_feedback_setup
```

### 17.3 Metrics

```
chatbot_turns_total
chatbot_ttft_seconds_bucket
chatbot_total_latency_seconds_bucket
chatbot_tokens_input_total{cached="true"}
chatbot_tokens_input_total{cached="false"}
chatbot_tokens_output_total
chatbot_cost_dollars_total{tenant}
chatbot_retrieval_relevant_count_bucket
chatbot_user_feedback_positive_total
chatbot_user_feedback_negative_total
chatbot_eval_pass_rate{suite="production"}
chatbot_faithfulness_score
chatbot_hallucination_indicator_total
```

### 17.4 Eval suite

- 200 fixed test questions, with known-good answers from human curators.
- Run nightly + on every prompt-change PR.
- Metric: pass rate (judge model rates each).
- Block merges that drop pass rate by > 2%.

### 17.5 Dashboards

- TTFT and total latency p50/p95/p99.
- Token usage trend; cost per session.
- Cache hit rate.
- Quality: eval pass rate, faithfulness score, user feedback.
- Drift: input topic distribution; embedding-index age.
- Errors: LLM call failures, vector-search failures, retries.

### 17.6 Alerts

- Eval pass rate < 80%: page (quality regression).
- TTFT p95 > 3s: page.
- Cache hit rate < 60%: ticket (caching broken).
- Cost per session > $0.10: ticket (cost regression).
- Faithfulness score < 0.7: ticket.
- Embedding index age > 24h: ticket.

### 17.7 SLOs

```yaml
- name: chatbot_quality
  metric: eval_pass_rate
  target: 0.85
- name: chatbot_ttft
  metric: ttft_under_2s
  target: 0.99
- name: chatbot_cost
  metric: avg_cost_per_session_under_0.05
  target: 0.95
- name: chatbot_user_satisfaction
  metric: positive_feedback_rate
  target: 0.85
```

### 17.8 The result

When something regresses:
- Trace shows where time / cost went.
- Eval pass-rate drop catches quality regressions in CI before deploy.
- Faithfulness signal catches hallucination regressions in production.
- Cost dashboard caught a 3× spike when caching broke (system prompt was reorged; cache breakpoint moved).
- Drift detection caught a topic shift when a new product launched and confused the bot.

LLM observability isn't optional. It's the platform-team product for the LLM era.

---

## 18. Pitfalls

1. **No token accounting.** $$$ surprises.
2. **No eval harness.** Quality drift undetected.
3. **No faithfulness measurement.** Hallucination ships.
4. **No prompt logging.** Debugging impossible.
5. **No tool-call tracing.** Agent failures opaque.
6. **No cache observability.** Caching breaks invisibly.
7. **PII in logs.** Compliance violation.
8. **Per-feature attribution missing.** Cost-center confusion.
9. **No drift signals.** Slow decay.
10. **Vendor lock-in via proprietary instrumentation.** OTel GenAI is the standard.
11. **Mean latency for streaming.** TTFT and total are different.
12. **No human-feedback loop.** Quality measurable only by judge models.
13. **Eval suite too small.** Not statistically meaningful.
14. **No model-version label.** Vendor updates invisible.
15. **No vector-search latency SLO.** Retrieval bottlenecks invisible.

---

## 19. Mental Models

> **Cost is the first observability concern. Token accounting per request, per feature, per tenant.**

> **Quality is observable but only with labeled data. Build the eval suite from day one.**

> **The trace is the unit of LLM debugging. One user turn = one trace = many spans.**

> **OTel GenAI conventions are the substrate. Use them.**

> **Cache hit rate is a 10× cost lever. Instrument explicitly.**

> **Faithfulness, hallucination, citation correctness — RAG-specific signals.**

> **Drift is slow decay. Detect with eval cadence, input distribution, user feedback.**

> **PII is in prompts and completions. Redact at source; audit access.**

> **Model versioning + alerting on update is non-negotiable.**

> **Streaming UX uses TTFT, not total latency.**

Now go to `doc 27` (security observability) — the overlap of SRE telemetry and SOC telemetry.
