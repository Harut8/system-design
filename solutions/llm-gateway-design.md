# Multi-Provider LLM Gateway: Design Document

> Solution to [`tasks/llm-gateway.md`](../tasks/llm-gateway.md).

### Prerequisites and Learning Resources

Before or alongside this document, study these deep-dive chapters from the curriculum:

| Topic | Resource | Why |
|-------|----------|-----|
| Mental models | [`ai-rag/00-mental-models.md`](../ai-rag/00-mental-models.md) | How LLMs fit into the generation pipeline — context for why a gateway matters |
| LangChain architecture | [`ai-rag/20-langchain-architecture-and-internals.md`](../ai-rag/20-langchain-architecture-and-internals.md) | How frameworks abstract providers — the client-side of what this gateway serves |
| Agent orchestration | [`ai-rag/22-agent-orchestration-patterns.md`](../ai-rag/22-agent-orchestration-patterns.md) | Agents are the primary consumer of the gateway — understand their calling patterns |
| Deployment and compute | [`ai-rag/appendix-e-deployment-and-compute.md`](../ai-rag/appendix-e-deployment-and-compute.md) | Self-hosted model serving, GPU allocation, inference optimization — relevant to local model routing |

---

## Table of Contents

1. [Requirements Clarification](#1-requirements-clarification)
2. [Architecture Overview](#2-architecture-overview)
3. [Unified API Design](#3-unified-api-design)
4. [Provider Adapter Layer](#4-provider-adapter-layer)
5. [Model Capabilities Registry](#5-model-capabilities-registry)
6. [Routing Engine](#6-routing-engine)
7. [Streaming Architecture](#7-streaming-architecture)
8. [Retry and Fallback](#8-retry-and-fallback)
9. [Rate Limiting](#9-rate-limiting)
10. [Caching](#10-caching)
11. [Cost Engine](#11-cost-engine)
12. [Observability](#12-observability)
13. [Security](#13-security)
14. [Data Models](#14-data-models)
15. [Deployment](#15-deployment)
16. [Failure Scenarios](#16-failure-scenarios)
17. [Trade-offs](#17-trade-offs)
18. [Evolution Path](#18-evolution-path)
19. [Capacity Estimates](#19-capacity-estimates)
20. [Exercises](#20-exercises)

---

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|---|---|---|
| Scope | Does the gateway ever generate tokens itself (self-hosted models)? | Yes — self-hosted vLLM/TGI clusters are one more provider behind an adapter, not a special case |
| Consistency | Must rate limits be perfectly exact across nodes? | No — we accept **bounded overshoot** (≤2% of limit) in exchange for not putting a synchronous quorum call on the request hot path |
| Consistency | Must cost records be exactly-once? | Yes, non-negotiable — billing gaps or double-counts erode trust in the whole platform. Mechanism in §11 |
| Latency | Is the 50ms P99 overhead inclusive of network to the provider? | No — it's gateway-added overhead only: routing decision, policy checks, telemetry emission. Provider RTT and generation time are separate and dominate total latency |
| Availability | What happens if literally every provider is down? | Fail fast with a structured `503 all_providers_unavailable` error within one provider-timeout window — never hang past that |
| Streaming | Can a caller cancel mid-stream? | Yes — cancellation propagates to the upstream provider connection within one flush interval (~50ms), so the company isn't billed for tokens nobody reads |
| Caching | Is semantic cache on by default? | No — opt-in per route, because a false-positive cache hit is a correctness bug, not just a performance one. Exact-match cache is opt-out (default on) |
| Security | Does PII scrubbing block the request or just the log? | Configurable per tenant. Default: scrub logs only, forward the original request to the provider (the provider needs the real content to answer). Tenants with stricter policy can enable pre-send redaction, accepting quality loss |
| Multi-tenancy | Do tenants share provider API keys? | Yes, by default — the gateway holds a small pool of provider keys and multiplexes tenants behind them via internal accounting. Tenants requiring dedicated keys/quotas (e.g. enterprise customers with contracted capacity) get a dedicated key pool |
| Ops | Team size operating this? | 5 engineers. This bounds operational complexity as hard as the throughput numbers do |

### Key Assumptions

1. **Read-through, not a data store.** The gateway holds no long-term conversation state; each request is self-contained (full message history sent by the caller), except for server-side prompt caching which is a provider-native optimization, not gateway state.
2. **Providers fail independently and often.** OpenAI, Anthropic, and Google each have multiple-times-per-quarter degraded periods. Designing as if providers are reliable is the single most common mistake in this space.
3. **Cost and latency are both first-order.** A gateway that only optimizes for uptime and ignores the fact that GPT-4o costs 10x a Haiku-class model for many tasks is not solving the actual business problem.
4. **Streaming is the majority case**, not the exception — most interactive traffic streams; batch/offline traffic does not. Both must be first-class.
5. **Providers change under us.** Model deprecations, silent behavior changes, and pricing updates happen without a gateway code change — the design must treat provider metadata as data, not constants.
6. **Tail latency matters more than median for interactive traffic**, and throughput/cost matters more for batch traffic — the routing engine must know which class a request belongs to.

### What We Are Explicitly Not Building (v1)

- Not a prompt-management or prompt-versioning system (that's a separate service that calls the gateway).
- Not an evaluation/quality-scoring platform, though we emit the telemetry an eval system would consume.
- Not a fine-tuning or training orchestration layer.
- Not a full agent framework — the gateway serves single-turn and multi-turn chat/completion calls and tool-call round trips; multi-step agent orchestration lives in the caller.

---

## 2. Architecture Overview

### Component Map

```
                                   ┌─────────────────────────────────────────┐
                                   │              Config Service              │
                                   │  (model registry, routing rules, keys)   │
                                   │        etcd / Consul, versioned          │
                                   └───────────────┬───────────────────────┘
                                                    │ watch (push, <1s propagation)
                                                    ▼
 ┌──────────┐     ┌──────────────────────────────────────────────────────────────┐
 │  Caller   │────▶│                      Gateway Node (stateless)                 │
 │ (service) │     │                                                                │
 └──────────┘     │  ┌────────────┐  ┌───────────┐  ┌────────────┐  ┌───────────┐ │
      ▲            │  │   Ingress   │─▶│  AuthN /  │─▶│  Request   │─▶│  Router   │ │
      │            │  │  (HTTP/2,   │  │  AuthZ /   │  │ Normalizer │  │  Engine   │ │
      │            │  │   gRPC)     │  │  Policy    │  │            │  │           │ │
      │            │  └────────────┘  └───────────┘  └────────────┘  └─────┬─────┘ │
      │            │                                                        │       │
      │            │  ┌──────────────┐  ┌────────────┐  ┌───────────┐      │       │
      │            │  │  Rate Limiter │◀─│   Cache    │◀─┤  Cost      │◀────┘       │
      │            │  │  (local L1 +   │  │  (L1 local  │  │  Pre-Check │            │
      │            │  │   Redis L2)    │  │  + Redis L2)│  │  (budget)  │            │
      │            │  └──────┬───────┘  └────────────┘  └───────────┘            │
      │            │         │                                                    │
      │            │         ▼                                                    │
      │            │  ┌─────────────────────────────────────────────────────┐    │
      │            │  │              Provider Adapter Layer                    │    │
      │            │  │  ┌────────┐ ┌───────────┐ ┌────────┐ ┌────────────┐ │    │
      │            │  │  │ OpenAI  │ │ Anthropic │ │ Gemini  │ │  Bedrock /  │ │    │
      │            │  │  │ Adapter │ │  Adapter  │ │ Adapter │ │Azure/Self-  │ │    │
      │            │  │  └───┬────┘ └─────┬─────┘ └───┬────┘ │hosted Adapt.│ │    │
      │            │  │      │            │            │       └──────┬─────┘ │    │
      │            │  │      ▼            ▼            ▼               ▼       │    │
      │            │  │  Circuit Breaker per (provider, region, model)        │    │
      │            │  └─────────────────────────────────────────────────────┘    │
      │            │         │                                                    │
      │            │         ▼                                                    │
      │            │  ┌────────────┐  ┌────────────┐                            │
      │            │  │ Response    │─▶│  Telemetry  │──▶ OTel Collector          │
      │            │  │ Normalizer  │  │  Emitter    │──▶ Cost Ledger (Kafka)      │
      │            │  └────────────┘  └────────────┘                            │
      └────────────┴──────────────────────────────────────────────────────────────┘
                                                    │
                          ┌─────────────────────────┼─────────────────────────┐
                          ▼                          ▼                         ▼
                  ┌──────────────┐         ┌──────────────────┐     ┌──────────────────┐
                  │ OpenAI /      │         │ Anthropic /       │     │ Google / AWS      │
                  │ Azure OpenAI  │         │ Bedrock Anthropic │     │ Bedrock / Self-    │
                  └──────────────┘         └──────────────────┘     │ hosted vLLM cluster │
                                                                     └──────────────────┘
```

### Control Plane vs. Data Plane

| Plane | Components | Characteristics |
|---|---|---|
| **Data plane** | Gateway nodes: ingress, router, adapters, rate limiter (hot path), cache lookup | Stateless, horizontally scaled, every millisecond counts, must survive a config-service outage by running on last-known-good config |
| **Control plane** | Config service (model registry, routing rules, tenant budgets), key vault, cost ledger consumer, admin API | Can tolerate seconds of staleness; changes here propagate to data plane asynchronously, never synchronously on the request path |

**Why this split matters**: the single biggest reliability bug in gateway designs is making the hot path depend synchronously on a control-plane call (e.g., "fetch tenant budget from a database on every request"). Every control-plane fact the router needs — pricing, capabilities, routing rules, rate-limit config — is **pushed** to gateway nodes and cached in-memory, refreshed via a watch/long-poll, never fetched inline.

### Request Path, Step by Step

```
1.  Ingress accepts HTTP/2 (or gRPC) connection, TLS-terminated at LB.
2.  AuthN: validate caller's service identity (mTLS client cert or bearer JWT).
3.  AuthZ + Policy: is this tenant allowed to call this model/capability?
                     data residency check, PII policy lookup.
4.  Request Normalizer: parse unified request schema, validate, assign trace_id.
5.  Cost Pre-Check: does this tenant have budget remaining? (soft/hard limit)
6.  Cache Lookup: exact-match, then (if enabled) semantic — return early on hit.
7.  Rate Limiter: local token-bucket check (fast path), async-reconciled
    against Redis for cross-node fairness.
8.  Router: resolve model/equivalence-class -> ranked candidate list of
    (provider, model, region) using capability registry + routing rules +
    live health from circuit breakers.
9.  Adapter: translate unified request -> provider wire format, issue call
    (streaming or unary), through provider connection pool.
10. On failure: retry per provider policy, or fall to next candidate
    (Router loop back to step 9 with next candidate) per §8.
11. Response Normalizer: translate provider response -> unified schema.
12. Telemetry Emitter: emit trace spans, metrics, and a cost/usage record
    (durably, exactly-once) to the cost ledger.
13. Return to caller (unary or streamed).
```

Steps 5–7 are ordered cheapest-check-first: a cache hit or a budget rejection
should never pay the cost of a rate-limiter round trip, and neither should
pay for a router computation.

---

## 3. Unified API Design

### Design Goals

* One schema in, one schema out, regardless of destination provider.
* Every field that providers disagree on (system prompts, tool schemas, stop
  reasons) gets **one canonical representation**; adapters do the translation
  in both directions.
* An escape hatch (`provider_options`) for features not yet normalized, so
  callers are never blocked by the gateway's abstraction lagging a provider's
  feature release.

### Unified Request Schema

```jsonc
POST /v1/chat/completions
{
  "model": "tier-1-reasoning",          // equivalence class OR concrete model id
  "messages": [
    { "role": "system", "content": "You are a careful financial analyst." },
    { "role": "user", "content": [
        { "type": "text", "text": "Summarize this filing." },
        { "type": "image", "source": { "type": "url", "url": "https://…/10k.png" } }
    ]},
    { "role": "assistant", "content": "…prior turn…", "tool_calls": [ /* … */ ] },
    { "role": "tool", "tool_call_id": "call_abc123", "content": "…tool result…" }
  ],
  "tools": [
    {
      "name": "get_stock_price",
      "description": "Fetch the current stock price for a ticker.",
      "parameters": {
        "type": "object",
        "properties": { "ticker": { "type": "string" } },
        "required": ["ticker"]
      }
    }
  ],
  "tool_choice": "auto",                 // auto | none | required | {"name": "..."}
  "response_format": { "type": "json_schema", "schema": { /* … */ } },
  "max_output_tokens": 2048,
  "temperature": 0.3,
  "top_p": 1.0,
  "stop_sequences": ["\n\nEND"],
  "stream": true,

  "routing": {
    "strategy": "quality_tier",          // overrides default policy for this call
    "quality_tier": "tier-1",
    "fallback_allowed": true,
    "data_residency": "eu-only"
  },
  "cache": { "mode": "exact", "ttl_s": 3600 },
  "metadata": {
    "tenant": "team-risk-analytics",
    "project": "10k-summarizer",
    "environment": "prod",
    "idempotency_key": "req-9f3a7c2e"
  },
  "provider_options": {                  // escape hatch, passed through verbatim
    "anthropic": { "thinking": { "type": "enabled", "budget_tokens": 4000 } }
  }
}
```

### Unified Response Schema

```jsonc
{
  "id": "req_8f3a7c2e19d4",
  "model_requested": "tier-1-reasoning",
  "model_served": "claude-opus-4-20250514",
  "provider": "anthropic",
  "region": "us-east-1",
  "created": 1735500000,
  "content": [
    { "type": "text", "text": "The filing shows revenue growth of 12% YoY…" }
  ],
  "tool_calls": [
    { "id": "call_xyz789", "name": "get_stock_price", "arguments": { "ticker": "ACME" } }
  ],
  "stop_reason": "tool_use",             // stop | tool_use | length | content_filter | error
  "usage": {
    "input_tokens": 1423,
    "output_tokens": 312,
    "cached_input_tokens": 900,
    "total_tokens": 1735
  },
  "cost": {
    "input_usd": 0.00427,
    "output_usd": 0.00468,
    "cache_savings_usd": 0.00243,
    "total_usd": 0.00895
  },
  "latency": {
    "gateway_overhead_ms": 8,
    "ttft_ms": 340,
    "total_ms": 2210
  },
  "routing": {
    "candidates_tried": ["anthropic:claude-opus-4"],
    "fallback_used": false
  },
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

### Canonical Stop Reason Mapping

| Canonical | OpenAI | Anthropic | Gemini | Bedrock (varies) |
|---|---|---|---|---|
| `stop` | `stop` | `end_turn` | `STOP` | `end_turn` / `COMPLETE` |
| `length` | `length` | `max_tokens` | `MAX_TOKENS` | `max_tokens` |
| `tool_use` | `tool_calls` | `tool_use` | `n/a (function_call present)` | model-specific |
| `content_filter` | `content_filter` | model refusal text (heuristic) | `SAFETY` | model-specific |
| `error` | any 5xx / malformed | any 5xx / malformed | any 5xx / malformed | any 5xx / malformed |

This table is itself part of the adapter contract (§4) — every adapter must
implement this mapping and it is unit-tested against recorded fixtures per
provider so a provider's silent wire-format change is caught by CI, not by a
caller's parser breaking in production.

### System Prompt Normalization

| Provider | Native representation |
|---|---|
| OpenAI (Chat Completions) | A `{"role": "system", ...}` message, first in the array |
| OpenAI (Responses API) | Top-level `instructions` field |
| Anthropic | Top-level `system` string/array parameter, **not** a message |
| Google Gemini | Top-level `systemInstruction` object |
| Bedrock (model-dependent) | Varies per underlying model family |

The unified schema keeps the system prompt as a `role: "system"` message for
caller ergonomics (matches the most common convention), and each adapter
extracts and relocates it to the provider's native slot during translation.

### Tool Calling Normalization

```
Unified tool definition          →  Provider-native encoding
─────────────────────────────────────────────────────────────
{ name, description,             →  OpenAI:    {"type":"function","function":{...}}
  parameters: JSONSchema }       →  Anthropic: {"name","description","input_schema"}
                                  →  Gemini:    {"function_declarations":[{...}]}

Unified tool_choice              →  Provider-native encoding
─────────────────────────────────────────────────────────────
"auto"                           →  OpenAI: "auto"     Anthropic: {"type":"auto"}
"none"                           →  OpenAI: "none"      Anthropic: {"type":"none"}(no-tool prompt injection if unsupported)
"required"                       →  OpenAI: "required"  Anthropic: {"type":"any"}
{"name": "get_price"}            →  OpenAI: {"type":"function","function":{"name":"get_price"}}
                                     Anthropic: {"type":"tool","name":"get_price"}
                                     Gemini: tool_config.function_calling_config
```

**Parallel tool calls**: OpenAI and Anthropic both support the model
returning multiple tool calls in one turn; the unified response always uses a
`tool_calls: []` array (even for a single call) so callers write one code
path. Providers that only support a single tool call per turn (some
self-hosted models) are marked in the capability registry as
`parallel_tool_calls: false`, and the router avoids sending
multi-tool-eligible prompts to them when `tool_choice` implies multiple calls
are likely — this is a soft routing signal, not a hard block, since we cannot
know in advance how many calls a model will choose to emit.

### Multimodal Input Handling

Content blocks are typed (`text`, `image`, `document`, `audio`) with a
`source` that is either an inline base64 payload or a URL. The **Request
Normalizer** validates the requested model supports each content type present
(from the capability registry) *before* routing — a request containing an
image sent against a text-only model equivalence class is rejected at step 4
of the request path with a `400 unsupported_content_type`, not silently
stripped. Silent downgrade (dropping the image and answering as if it wasn't
there) is explicitly rejected as a design choice: it produces a plausible but
wrong answer, which is worse than an explicit error.

---

## 4. Provider Adapter Layer

### Adapter Interface

Every provider is implemented against one interface. New providers are added
by implementing this interface and registering the implementation — no
changes to router, rate limiter, or telemetry code.

```go
// Package adapter defines the contract every provider integration implements.
package adapter

type Provider interface {
    // Name returns the stable provider identifier, e.g. "anthropic".
    Name() string

    // Complete issues a non-streaming request and returns a unified response.
    Complete(ctx context.Context, req *UnifiedRequest) (*UnifiedResponse, error)

    // Stream issues a streaming request; events are pushed onto the returned channel
    // as they arrive, already translated to unified StreamEvent shape.
    // The channel is closed when the stream ends (success, error, or ctx cancellation).
    Stream(ctx context.Context, req *UnifiedRequest) (<-chan StreamEvent, error)

    // HealthCheck performs a cheap, low-cost call (or reads recent request outcomes)
    // to report current provider health, used by the circuit breaker's half-open probe.
    HealthCheck(ctx context.Context) HealthStatus

    // Capabilities returns this adapter's declared support matrix, cross-checked
    // against the central registry at startup (drift = alert, not silent divergence).
    Capabilities() ProviderCapabilities

    // ClassifyError maps a raw provider error (HTTP status, SDK error type) into
    // the gateway's canonical error taxonomy — this is what retry/circuit-breaker
    // logic and rate limiting act on, so it must be precise per provider.
    ClassifyError(err error) ErrorClass
}

type ErrorClass int

const (
    ErrClassTransient    ErrorClass = iota // network blip, 502/503/504 — retry same provider
    ErrClassRateLimited                    // 429 / throttling — retry with backoff, or fallback
    ErrClassAuth                           // bad/expired credential — do not retry, page on-call
    ErrClassInvalidRequest                 // 400-class, caller error — do not retry, surface to caller
    ErrClassContentPolicy                  // provider refused on safety grounds — do not retry same model
    ErrClassOverloaded                     // provider explicitly signals capacity exhaustion — fallback immediately
    ErrClassTimeout                        // gateway-side timeout waiting on provider — retry per policy
    ErrClassUnknown                        // unrecognized — conservative: treat as transient, alert if frequent
)
```

### Example: Anthropic Adapter (request translation excerpt)

```go
func (a *AnthropicAdapter) toWireFormat(req *adapter.UnifiedRequest) (*anthropicRequest, error) {
    wire := &anthropicRequest{
        Model:       a.resolveModelID(req.Model),
        MaxTokens:   req.MaxOutputTokens,
        Temperature: req.Temperature,
        Stream:      req.Stream,
    }

    // Anthropic wants system prompt hoisted out of the messages array.
    var msgs []anthropicMessage
    for _, m := range req.Messages {
        if m.Role == "system" {
            wire.System = append(wire.System, anthropicSystemBlock{Type: "text", Text: m.TextContent()})
            continue
        }
        msgs = append(msgs, translateMessage(m))
    }
    wire.Messages = msgs

    // Tool schema translation.
    for _, t := range req.Tools {
        wire.Tools = append(wire.Tools, anthropicTool{
            Name:        t.Name,
            Description: t.Description,
            InputSchema: t.Parameters, // Anthropic's input_schema == our JSONSchema verbatim
        })
    }
    wire.ToolChoice = translateToolChoice(req.ToolChoice)

    // Passthrough escape hatch, merged last so it can override defaults deliberately.
    if opts, ok := req.ProviderOptions["anthropic"]; ok {
        mergeRaw(wire, opts)
    }
    return wire, nil
}
```

### Provider Differences Matrix

| Concern | OpenAI | Anthropic | Google Gemini | AWS Bedrock | Azure OpenAI | Self-hosted (vLLM) |
|---|---|---|---|---|---|---|
| Auth | API key (Bearer) | API key (`x-api-key`) | API key or OAuth (ADC) | SigV4 | API key + resource endpoint | mTLS / internal token |
| System prompt | message w/ role `system` (or `instructions`) | top-level `system` param | top-level `systemInstruction` | model-family dependent | same as OpenAI | usually a chat-template convention |
| Streaming protocol | SSE, `data: {...}` chunks, `[DONE]` sentinel | SSE, typed `event:` lines (`message_start`, `content_block_delta`, …) | SSE or chunked JSON per model | varies (EventStream binary framing) | SSE (OpenAI-compatible) | SSE (usually OpenAI-compatible) |
| Tool call format | `tool_calls[]` with JSON-encoded args string | `content` blocks of `type: tool_use` with structured `input` | `functionCall` parts | model-family dependent | same as OpenAI | usually OpenAI-compatible |
| Model addressing | flat model string (`gpt-4o`) | flat model string (`claude-opus-4-...`) | flat model string | ARN or model-id + inference profile | **deployment name**, indirect from model | endpoint + model tag |
| Rate limit signal | `429` + `Retry-After` header | `429` + `retry-after` header | `429` (RESOURCE_EXHAUSTED) | `ThrottlingException` | `429` + `Retry-After` | varies, often just connection refusal at capacity |
| Prompt caching | automatic (no opt-in) for some models | explicit `cache_control` breakpoints | implicit context caching (separate API) | model-dependent | same as OpenAI | KV-cache reuse, gateway-managed (§10) |
| Batch discount | Batch API, ~50% off, async | Message Batches API, ~50% off, async | Batch API | model-dependent | same as OpenAI | N/A (self-hosted, no per-token billing) |

### Handling Azure OpenAI's Deployment Indirection

Azure OpenAI does not let you address a model by name — you address a
*deployment*, which an Azure admin has pre-provisioned to point at a specific
model version, region, and capacity (TPM) allocation. The adapter maintains a
`model → deployment` map per Azure resource, refreshed from config, so the
rest of the gateway still reasons in terms of models:

```yaml
providers:
  azure_openai:
    resources:
      - endpoint: "https://acme-eastus.openai.azure.com"
        api_key_ref: "vault://azure/eastus/key"
        deployments:
          gpt-4o:            { deployment_name: "gpt4o-prod-eastus", tpm_quota: 450000 }
          gpt-4o-mini:        { deployment_name: "gpt4o-mini-prod-eastus", tpm_quota: 900000 }
      - endpoint: "https://acme-westeu.openai.azure.com"
        api_key_ref: "vault://azure/westeu/key"
        deployments:
          gpt-4o:            { deployment_name: "gpt4o-prod-westeu", tpm_quota: 300000 }
```

This also gives geographic affinity for free: the router can pick the
West-EU deployment for an EU-residency-constrained tenant without any special
casing beyond "filter candidates by region."

### Version Management

* Each adapter declares the **wire-protocol versions** it supports (e.g.,
  Anthropic API version header `2023-06-01` vs newer). Bumping a provider API
  version is a config change (`api_version: "2023-06-01"` in the provider
  config), not a code change, unless the new version changes the response
  shape enough to need new translation logic.
* Model *identifiers* (e.g. `claude-opus-4-20250514`) are registry data, not
  code — see §5.
* Adapters are versioned independently and can be canaried: a new adapter
  version can run behind a feature flag, receiving shadow traffic (§6)
  before being promoted to serve real fallback candidates.

### Health Checking

Each adapter's `HealthCheck` is called by a background prober every 5s (not
per-request) and additionally derives health passively from the rolling
error rate of real traffic. Both feed the circuit breaker (§8):

```
health_status = f(
    active_probe_result,           // synthetic low-cost call, e.g. 1-token completion
    rolling_error_rate_60s,        // from real traffic
    rolling_p99_latency_60s,       // from real traffic
    provider_status_page_signal    // optional: scrape/poll provider status API
)
```

Weighting real traffic more heavily than the synthetic probe avoids the
classic failure mode where a synthetic health check succeeds (low load, cheap
request) while real traffic is failing (provider is degraded specifically
under load or for larger requests).

---

## 5. Model Capabilities Registry

### Purpose

The registry is the single source of truth the router, cost engine, and
request validator all consult. It must be **read in the hot path** at
sub-millisecond cost, which rules out a database round-trip per request — it
is held fully in-memory on every gateway node and updated via push.

### Data Model

```sql
CREATE TABLE model_capabilities (
    provider            TEXT NOT NULL,          -- 'openai' | 'anthropic' | 'google' | 'bedrock' | 'azure_openai' | 'self_hosted'
    model_id            TEXT NOT NULL,           -- provider-native id, e.g. 'claude-opus-4-20250514'
    display_name        TEXT NOT NULL,
    region               TEXT NOT NULL DEFAULT 'global',
    context_window_in    INTEGER NOT NULL,        -- max input tokens
    context_window_out   INTEGER NOT NULL,        -- max output tokens
    supports_vision      BOOLEAN NOT NULL DEFAULT FALSE,
    supports_tools        BOOLEAN NOT NULL DEFAULT FALSE,
    supports_parallel_tools BOOLEAN NOT NULL DEFAULT FALSE,
    supports_json_mode    BOOLEAN NOT NULL DEFAULT FALSE,
    supports_streaming    BOOLEAN NOT NULL DEFAULT TRUE,
    supports_prompt_cache  BOOLEAN NOT NULL DEFAULT FALSE,
    modalities            TEXT[] NOT NULL DEFAULT '{text}', -- text, image, audio, document
    knowledge_cutoff       DATE,
    price_input_per_1m     NUMERIC(10,4) NOT NULL,  -- USD per 1M input tokens
    price_output_per_1m    NUMERIC(10,4) NOT NULL,
    price_cached_input_per_1m NUMERIC(10,4),
    price_batch_discount_pct   NUMERIC(5,2) DEFAULT 0,
    price_effective_from   TIMESTAMPTZ NOT NULL,
    quality_tier            TEXT NOT NULL,          -- 'tier-1' | 'tier-2' | 'tier-3' (coarse quality bucket)
    equivalence_classes      TEXT[],                 -- e.g. {'tier-1-reasoning','general-chat'}
    status                   TEXT NOT NULL DEFAULT 'active', -- active | deprecated | sunset
    deprecation_announced_at  TIMESTAMPTZ,
    sunset_at                  TIMESTAMPTZ,
    replacement_model_id        TEXT,
    registered_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, model_id, region)
);

CREATE INDEX idx_capabilities_equivalence ON model_capabilities USING GIN (equivalence_classes);
CREATE INDEX idx_capabilities_status ON model_capabilities (status) WHERE status = 'active';
```

### In-Memory Representation on Gateway Nodes

```python
@dataclass(frozen=True)
class ModelCapability:
    provider: str
    model_id: str
    region: str
    context_window_in: int
    context_window_out: int
    supports_vision: bool
    supports_tools: bool
    supports_parallel_tools: bool
    supports_json_mode: bool
    supports_streaming: bool
    supports_prompt_cache: bool
    modalities: frozenset[str]
    price_input_per_1m: Decimal
    price_output_per_1m: Decimal
    price_cached_input_per_1m: Decimal | None
    quality_tier: str
    equivalence_classes: frozenset[str]
    status: Literal["active", "deprecated", "sunset"]
    replacement_model_id: str | None

class CapabilityRegistry:
    """Immutable snapshot, swapped atomically on config push. Readers never lock."""
    def __init__(self, models: list[ModelCapability]):
        self._by_key = {(m.provider, m.model_id, m.region): m for m in models}
        self._by_equivalence: dict[str, list[ModelCapability]] = defaultdict(list)
        for m in models:
            if m.status != "active":
                continue
            for cls in m.equivalence_classes:
                self._by_equivalence[cls].append(m)
        for cls in self._by_equivalence:
            self._by_equivalence[cls].sort(key=lambda m: m.price_input_per_1m)

    def resolve(self, model_or_class: str) -> list[ModelCapability]:
        if candidates := self._by_equivalence.get(model_or_class):
            return candidates
        exact = [m for k, m in self._by_key.items() if k[1] == model_or_class and m.status == "active"]
        return exact

    def get(self, provider: str, model_id: str, region: str = "global") -> ModelCapability | None:
        return self._by_key.get((provider, model_id, region))
```

The registry object is **replaced wholesale**, never mutated in place —
config pushes build a new immutable snapshot and swap a pointer, so concurrent
readers on the hot path never see a torn/partial update and never take a
lock.

### Manual Registration vs. Auto-Discovery

| Mode | Mechanism | Used for |
|---|---|---|
| **Manual** | PR to `models.yaml`, reviewed, applied via config service, propagated by push | Primary mechanism — pricing and capability claims need a human to read the provider's release notes and verify, not just detect a new string |
| **Auto-discovery** | Background job polls each provider's `/models` (or Bedrock's `ListFoundationModels`) every 30 min, diffs against the registry | **Detection only** — a newly-seen model is *not* auto-activated. It's inserted with `status: 'candidate'`, invisible to routing, and raises a low-priority alert: "new model detected, capabilities unknown, needs manual registration" |
| **Drift detection** | Same job compares registered capability claims (e.g. `supports_vision: true`) against a canary probe result | Mismatch raises a page — this is how you catch a provider silently changing behavior for an existing model id |

```yaml
# models.yaml (excerpt) — the source of truth manual registration edits
- provider: anthropic
  model_id: claude-opus-4-20250514
  display_name: "Claude Opus 4"
  region: global
  context_window_in: 200000
  context_window_out: 32000
  supports_vision: true
  supports_tools: true
  supports_parallel_tools: true
  supports_prompt_cache: true
  modalities: [text, image]
  price_input_per_1m: 15.00
  price_output_per_1m: 75.00
  price_cached_input_per_1m: 1.50
  price_effective_from: "2025-05-14T00:00:00Z"
  quality_tier: tier-1
  equivalence_classes: [tier-1-reasoning, vision-capable]
  status: active
```

### Deprecation Handling

```
status: active
    │  provider announces deprecation
    ▼
status: deprecated  (deprecation_announced_at set, replacement_model_id set)
    │  router: still routable, but every response includes a
    │  `deprecation_warning` field; alert fires to owning teams weekly
    │  cost dashboard surfaces "spend on deprecated models" as its own metric
    ▼  sunset_at reached
status: sunset  (no longer routable — requests fall through to replacement
                 via equivalence class, or fail with `model_sunset` if the
                 caller pinned the concrete model id with no equivalence class)
```

Pinning a **concrete model id** (not an equivalence class) is supported for
callers who need reproducibility (e.g. eval harnesses), but the gateway
proactively warns them as sunset approaches — pinning is an explicit
trade-off of stability over auto-migration, made visible, not silent.

---

## 6. Routing Engine

### Routing Strategies

| Strategy | Selection logic | Typical use |
|---|---|---|
| `least_cost` | Rank active candidates in the equivalence class by `price_input_per_1m * est_input + price_output_per_1m * est_output`, pick cheapest healthy one | Batch/offline summarization, non-latency-sensitive |
| `lowest_latency` | Rank by rolling P50 TTFT over the last 5 min per (provider, model, region) | Interactive chat |
| `quality_tier` | Filter to a named tier (`tier-1`, `tier-2`), then apply a secondary strategy within the tier | Default for most product traffic — pick the tier, let the gateway pick the cheapest/fastest within it |
| `weighted_round_robin` | Static or dynamically-adjusted weights per candidate, e.g. 70/30 split across two providers for negotiated-capacity reasons | Spreading load to respect per-provider committed-use contracts |
| `content_based` | Route on request shape: images present → vision-capable only; tool-heavy → tool-parallel-capable only; huge context → largest-context-window candidate | Automatic, applied as a **pre-filter** before any other strategy runs |

### Routing Decision Pipeline

```
                     ┌─────────────────────┐
 UnifiedRequest ────▶│ 1. Resolve model/    │  registry.resolve("tier-1-reasoning")
                      │    equivalence class │  -> [claude-opus-4, o1, gemini-2.5-pro, ...]
                      └──────────┬──────────┘
                                 ▼
                      ┌─────────────────────┐
                      │ 2. Content-based      │  drop candidates that can't satisfy
                      │    hard filter        │  modalities / tool / context-window needs
                      └──────────┬──────────┘
                                 ▼
                      ┌─────────────────────┐
                      │ 3. Policy filter      │  drop candidates outside tenant's allowed
                      │                       │  data-residency / compliance region
                      └──────────┬──────────┘
                                 ▼
                      ┌─────────────────────┐
                      │ 4. Health filter      │  drop candidates whose circuit breaker
                      │                       │  is OPEN
                      └──────────┬──────────┘
                                 ▼
                      ┌─────────────────────┐
                      │ 5. Strategy ranking   │  apply requested/default strategy
                      │                       │  (least_cost / lowest_latency / etc.)
                      └──────────┬──────────┘
                                 ▼
                      ┌─────────────────────┐
                      │ 6. Rate/quota check   │  does the top candidate have TPM/RPM
                      │                       │  headroom right now? if not, next candidate
                      └──────────┬──────────┘
                                 ▼
                       ranked candidate list  ──▶  adapter attempts in order (§8)
```

### Routing Rules DSL

Operators express routing policy declaratively; changes deploy through the
config service without a binary redeploy.

```yaml
routing_rules:
  - name: "eu-tenant-residency"
    match:
      tenant_tags: ["region:eu"]
    action:
      filter: { region_in: ["eu-west-1", "westeu"] }

  - name: "vision-content"
    match:
      request_contains: ["image", "document"]
    action:
      filter: { supports_vision: true }

  - name: "batch-jobs-cost-optimize"
    match:
      metadata.environment: "batch"
    action:
      strategy: least_cost
      fallback_allowed: true
      max_candidates_tried: 4

  - name: "default-interactive"
    match: { default: true }
    action:
      strategy: quality_tier
      quality_tier: tier-1
      secondary_strategy: lowest_latency
      fallback_allowed: true
      max_candidates_tried: 2
```

Rules are evaluated top-to-bottom, first match wins per action category
(filters accumulate, strategy is single-select) — this mirrors firewall-rule
semantics operators already understand, deliberately avoiding a bespoke
evaluation model.

### Model Equivalence Classes

The core abstraction that lets callers depend on a **capability tier**, not a
vendor:

| Equivalence class | Example members (rotates over time) | Selection criteria |
|---|---|---|
| `tier-1-reasoning` | `o1`, `claude-opus-4`, `gemini-2.5-pro` | Highest quality tier, used for complex multi-step reasoning, code generation |
| `tier-2-balanced` | `gpt-4o`, `claude-sonnet-4`, `gemini-2.5-flash` | Best cost/quality trade-off, default for most product traffic |
| `tier-3-fast-cheap` | `gpt-4o-mini`, `claude-haiku-4`, `gemini-2.5-flash-lite` | High-volume, low-complexity: classification, extraction, short chat turns |
| `vision-capable` | subset of the above with `supports_vision: true` | Any request with image/document content |
| `long-context` | models with `context_window_in >= 200_000` | Large-document analysis |

Membership is registry data (§5's `equivalence_classes` array), curated by
the platform team as new models are evaluated for quality — **not**
automatically inferred from a benchmark score, because benchmark-vs-real-task
quality correlation is weak enough that this must stay a human judgment call
with periodic review.

### A/B Testing and Shadow Traffic

```python
class TrafficSplitter:
    """Applied after routing resolves a primary candidate, before the adapter call."""

    def maybe_shadow(self, req: UnifiedRequest, primary: Candidate) -> None:
        experiment = self.experiments.get(req.metadata.tenant)
        if not experiment or not experiment.is_active():
            return
        if random.random() >= experiment.shadow_sample_rate:
            return
        # Fire-and-forget: mirror the exact request to the candidate model,
        # discard the response (log it for comparison), never affect the
        # caller's latency or the response they receive.
        asyncio.create_task(
            self._run_shadow(req, experiment.candidate_model, primary_response_id=req.id)
        )

    def maybe_ab_assign(self, req: UnifiedRequest) -> str | None:
        experiment = self.ab_experiments.get(req.metadata.tenant)
        if not experiment or not experiment.is_active():
            return None
        bucket = stable_hash(req.metadata.get("user_id", req.id)) % 100
        return experiment.variant_b_model if bucket < experiment.split_pct else None
```

* **Shadow traffic** never affects what the caller receives; it exists purely
  to compare a candidate model's cost/latency/output against the live model
  on real traffic before trusting it in the routing table.
* **A/B assignment** *does* affect what the caller receives, bucketed by a
  stable hash of user/session id so a given user gets a consistent variant
  across a session — required for any product-facing quality comparison to
  be meaningful.
* Both emit the same telemetry schema (§12) with an `experiment_id` and
  `variant` tag, so comparison is a query, not a special pipeline.

---

## 7. Streaming Architecture

### SSE Proxy Design

The gateway does not buffer a provider's full response before relaying it —
it relays chunks as they arrive, applying the unified-schema transform
per-chunk, which is why the response normalizer must be able to operate on
partial/incremental state, not just a complete response object.

```
Client                    Gateway                          Provider
  │                          │                                 │
  │──POST /v1/chat (stream)─▶│                                 │
  │                          │──POST (provider format)────────▶│
  │                          │                                 │
  │                          │◀──SSE: message_start────────────│
  │◀─SSE: {type:"start",…}──│  (translate + relay immediately) │
  │                          │◀──SSE: content_block_delta──────│
  │◀─SSE: {type:"delta",…}──│                                  │
  │                          │◀──SSE: content_block_delta──────│
  │◀─SSE: {type:"delta",…}──│      ... (repeats) ...           │
  │                          │◀──SSE: message_stop──────────────│
  │◀─SSE: {type:"done",      │  (final chunk carries usage,     │
  │        usage, cost}─────│   gateway computes cost here)    │
  │                          │                                 │
```

### Unified Stream Event Schema

```jsonc
// event: delta
{ "type": "delta", "index": 0, "content": { "type": "text", "text": "The filing" } }

// event: tool_call_delta (incremental tool-call argument streaming)
{ "type": "tool_call_delta", "id": "call_xyz", "name": "get_stock_price", "arguments_delta": "{\"ticker\":\"AC" } }

// event: done (terminal — always sent exactly once, even on error)
{ "type": "done", "stop_reason": "stop", "usage": {...}, "cost": {...}, "latency": {...} }

// event: error (terminal, in place of done — no further deltas will follow)
{ "type": "error", "error_class": "provider_overloaded", "message": "…", "partial_content_delivered": true }
```

Every stream terminates in exactly one of `done` or `error` — callers never
have to guess whether a dropped connection means "finished" or "failed
partway."

### Backpressure

A slow client (e.g. mobile connection) reading a fast provider stream must
not let the gateway accumulate an unbounded buffer of un-flushed provider
chunks in memory across thousands of concurrent streams.

```go
func (s *streamRelay) pump(ctx context.Context, providerCh <-chan adapter.StreamEvent, clientW http.ResponseWriter) error {
    flusher := clientW.(http.Flusher)
    // Bounded buffer: if the client can't keep up, we stop reading from the
    // provider rather than growing memory unboundedly. This applies natural
    // backpressure to the provider connection itself (TCP window shrinks).
    const maxBufferedEvents = 64

    buffered := 0
    for {
        select {
        case ev, ok := <-providerCh:
            if !ok {
                return nil // provider stream closed cleanly
            }
            if buffered >= maxBufferedEvents {
                if err := s.flushWithTimeout(clientW, flusher, 2*time.Second); err != nil {
                    // Client genuinely can't keep up or has gone away — abort
                    // upstream to stop paying for tokens nobody will read.
                    s.cancelUpstream()
                    return fmt.Errorf("client backpressure timeout: %w", err)
                }
                buffered = 0
            }
            writeSSE(clientW, ev)
            buffered++
        case <-ctx.Done():
            s.cancelUpstream()
            return ctx.Err()
        }
    }
}
```

Key properties:

* The provider connection is cancelled (not just abandoned) the moment the
  client disconnects or fails to keep up past a bounded timeout — this is
  what stops the company being billed for output tokens nobody reads.
* Backpressure is applied by **ceasing to read** from the provider channel,
  which propagates naturally: most provider SDKs' HTTP client will stop
  reading response bytes, TCP flow control kicks in, and (for providers that
  bill per token *generated*, not per token *sent*) this bounds cost exposure
  from a stalled client too.

### Partial Response Handling

If the provider stream fails after N tokens have already been relayed to the
client, the gateway **cannot** silently retry — the client has already
rendered partial output and a retry would either duplicate it or require the
client to discard and re-render, which the gateway cannot decide on the
client's behalf.

Policy:

1. Emit a terminal `error` event with `partial_content_delivered: true` and
   the `stop_reason` set to `error`.
2. The usage/cost record still reflects only the tokens actually generated
   and relayed (§11) — the caller is not billed for a phantom completion.
3. The caller's contract (documented, not silently assumed): on
   `partial_content_delivered: true`, the caller decides whether to discard
   and retry the whole request (new request, gateway treats it as unrelated)
   or to attempt a continuation (send the partial content back as an
   assistant turn and ask the model to continue) — gateway supports both,
   picks neither.

### Stream Multiplexing (Race Mode)

For latency-critical paths, the router can issue the same request to two
candidate models in parallel and stream back whichever produces a first
token fastest, cancelling the loser:

```python
async def race_stream(req: UnifiedRequest, candidates: list[Candidate]) -> AsyncIterator[StreamEvent]:
    tasks = {asyncio.create_task(adapters[c.provider].stream(req)): c for c in candidates[:2]}
    winner = None
    try:
        for coro in asyncio.as_completed(tasks):
            first_chunk_stream = await coro
            winner = first_chunk_stream
            break
        async for event in winner:
            yield event
    finally:
        # Cancel every non-winning task's underlying provider connection —
        # this is real money if left running (we'd be paying for a full
        # generation on the losing provider for zero product value).
        for t in tasks:
            if t.done() and t.result() is not winner:
                continue
            t.cancel()
```

Race mode is expensive (2x the provider cost for the "warm-up" until a
winner is picked, though the loser is cancelled quickly) and is reserved for
routes explicitly configured to accept that cost for a latency win — never a
default.

### Timeout Management for Long-Running Streams

| Timeout | Value | Behavior on trip |
|---|---|---|
| Connect timeout (gateway → provider) | 3s | Immediate fallback to next candidate |
| Time-to-first-token timeout | 15s (interactive) / 60s (batch) | Cancel, fallback to next candidate, no partial content yet so this is a clean retry |
| Inter-chunk timeout (provider goes silent mid-stream) | 30s | Terminate as `error`, `partial_content_delivered: true` — **not** retried automatically, since content has already been relayed |
| Total stream duration cap | 10 min | Hard cutoff, terminate as `error` with `stop_reason: length_timeout` — protects against a runaway generation loop tying up a connection indefinitely |
| Client idle/disconnect detection | TCP write failure or 5s missed heartbeat | Cancel upstream provider call immediately |

---

## 8. Retry and Fallback

### Per-Provider Retry Policy

```yaml
retry_policies:
  openai:
    retryable_error_classes: [transient, timeout, overloaded]
    max_attempts: 3
    backoff: { type: exponential, base_ms: 200, multiplier: 2.0, jitter: full, max_ms: 4000 }
    respect_retry_after_header: true

  anthropic:
    retryable_error_classes: [transient, timeout, overloaded]
    max_attempts: 3
    backoff: { type: exponential, base_ms: 250, multiplier: 2.0, jitter: full, max_ms: 5000 }
    respect_retry_after_header: true

  bedrock:
    retryable_error_classes: [transient, timeout]     # ThrottlingException routed to fallback, not same-provider retry
    max_attempts: 2
    backoff: { type: exponential, base_ms: 300, multiplier: 3.0, jitter: full, max_ms: 6000 }

  self_hosted:
    retryable_error_classes: [transient, timeout]
    max_attempts: 2
    backoff: { type: exponential, base_ms: 50, multiplier: 2.0, jitter: full, max_ms: 500 }  # same-DC, fail fast
```

Never retry `auth`, `invalid_request`, or `content_policy` errors — these are
not transient, and retrying them just multiplies cost for a guaranteed second
failure. `rate_limited` and `overloaded` are retried at most once same-provider
(respecting `Retry-After` if present) before falling to the next candidate —
waiting out a provider's rate limit is rarely worth the latency budget when a
same-tier alternative exists.

### Fallback Chain Execution

```python
async def execute_with_fallback(req: UnifiedRequest, candidates: list[Candidate]) -> UnifiedResponse:
    errors: list[AttemptError] = []
    max_candidates = req.routing.max_candidates_tried or 3

    for candidate in candidates[:max_candidates]:
        if breaker.state(candidate.key) == BreakerState.OPEN:
            errors.append(AttemptError(candidate, "circuit_open", retried=False))
            continue

        policy = retry_policies[candidate.provider]
        for attempt in range(policy.max_attempts):
            try:
                resp = await adapters[candidate.provider].complete(req, timeout=policy.timeout)
                breaker.record_success(candidate.key)
                resp.routing.candidates_tried = [c.key for c in errors] + [candidate.key]
                resp.routing.fallback_used = len(errors) > 0
                return resp
            except ProviderError as e:
                error_class = adapters[candidate.provider].classify_error(e)
                breaker.record_failure(candidate.key, error_class)
                errors.append(AttemptError(candidate, error_class, attempt))
                if error_class not in policy.retryable_error_classes:
                    break  # do not retry same candidate, move to next
                await backoff_sleep(policy.backoff, attempt)

    raise AllProvidersUnavailable(errors)  # -> structured 503 to caller, §16
```

Note the fallback chain only moves to the *next candidate* after either
exhausting same-provider retries or hitting a non-retryable error class —
this bounds worst-case latency to `sum(per-candidate timeout * attempts)`
across at most `max_candidates_tried` candidates, which is itself bounded and
configurable per route (interactive routes set this low; batch routes can
afford more).

### Idempotency

Two distinct idempotency problems:

1. **Gateway-level retry must not double-bill.** Solved by computing the
   cost/usage record only from the attempt that actually succeeded — failed
   attempts that returned zero tokens generate no cost record; failed
   attempts that partially generated tokens before failing (rare, mid-stream
   failure) generate a cost record for exactly the tokens actually produced,
   tagged `attempt_outcome: failed_partial`.
2. **Caller-level retry (the caller retries the whole HTTP request) must not
   double-execute.** The caller supplies `metadata.idempotency_key`; the
   gateway caches the **outcome** (not just in-flight dedup) of a given key
   for a configurable window (default 10 min), so a caller retry within that
   window returns the original result without re-calling any provider:

```python
async def handle_request(req: UnifiedRequest) -> UnifiedResponse:
    if key := req.metadata.get("idempotency_key"):
        if cached := await idempotency_store.get(tenant=req.metadata.tenant, key=key):
            cached.idempotent_replay = True
            return cached
        async with idempotency_store.lock(tenant=req.metadata.tenant, key=key, ttl_s=120):
            # second concurrent caller with the same key blocks here rather
            # than firing a second provider call
            resp = await route_and_execute(req)
            await idempotency_store.put(tenant=req.metadata.tenant, key=key, response=resp, ttl_s=600)
            return resp
    return await route_and_execute(req)
```

### Circuit Breaker State Machine

One breaker per `(provider, region, model)` — deliberately fine-grained, so
one struggling model on a provider doesn't trip the breaker for every other
model on the same provider.

```
                    failure_rate(60s window) > 50%
                    AND sample_count >= 20
        ┌──────────┐ ───────────────────────────────▶ ┌──────────┐
        │  CLOSED   │                                    │   OPEN    │
        │ (serving) │ ◀─────────────────────────────── │ (rejecting)│
        └──────────┘   half-open probe fails             └─────┬────┘
             ▲                                                    │
             │                                          cool-down │ timer
             │                                          expires   │ (30s,
             │                                          exponential│ up to 5min)
             │              half-open probe succeeds               ▼
             │         ┌───────────────────────────┐   ┌──────────────┐
             └────────│  N consecutive successes    │◀──│  HALF_OPEN    │
                       │  on limited trial traffic   │   │ (1 in 20 reqs │
                       └───────────────────────────┘   │  allowed through)│
                                                          └──────────────┘
```

```go
type BreakerState int
const (
    Closed BreakerState = iota
    Open
    HalfOpen
)

type CircuitBreaker struct {
    mu              sync.RWMutex
    state           BreakerState
    openedAt        time.Time
    cooldown        time.Duration // starts at 30s, doubles per repeated trip, capped at 5min
    window          *slidingWindow // 60s rolling error/success counts
    halfOpenSuccess int
}

func (b *CircuitBreaker) Allow() bool {
    b.mu.RLock(); defer b.mu.RUnlock()
    switch b.state {
    case Closed:
        return true
    case Open:
        return time.Since(b.openedAt) > b.cooldown // caller transitions to HalfOpen externally on next Allow
    case HalfOpen:
        return rand.Float64() < 0.05 // 1-in-20 trial traffic
    }
    return false
}

func (b *CircuitBreaker) RecordResult(success bool) {
    b.mu.Lock(); defer b.mu.Unlock()
    b.window.record(success)
    switch b.state {
    case Closed:
        if b.window.failureRate() > 0.5 && b.window.sampleCount() >= 20 {
            b.state = Open
            b.openedAt = time.Now()
        }
    case Open:
        if time.Since(b.openedAt) > b.cooldown {
            b.state = HalfOpen
            b.halfOpenSuccess = 0
        }
    case HalfOpen:
        if success {
            b.halfOpenSuccess++
            if b.halfOpenSuccess >= 5 {
                b.state = Closed
                b.cooldown = 30 * time.Second // reset backoff on full recovery
            }
        } else {
            b.state = Open
            b.openedAt = time.Now()
            b.cooldown = min(b.cooldown*2, 5*time.Minute)
        }
    }
}
```

### Degraded Mode

When every candidate for a requested capability has an OPEN breaker (or
exhausts `max_candidates_tried`), the gateway must **fail fast**, not hang:

```jsonc
HTTP 503
{
  "error": {
    "code": "all_providers_unavailable",
    "message": "No healthy provider available for equivalence class 'tier-1-reasoning' in region 'eu-west-1'.",
    "candidates_tried": [
      { "provider": "anthropic", "model": "claude-opus-4", "error_class": "circuit_open" },
      { "provider": "openai", "model": "o1", "error_class": "rate_limited", "retry_after_s": 12 }
    ],
    "retry_after_s": 12,
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
  }
}
```

The caller gets a `retry_after_s` computed from the soonest circuit-breaker
cooldown or `Retry-After` among tried candidates — actionable, not just "try
again sometime." Total time-to-this-error is bounded by the sum of
per-candidate timeouts (§7's timeout table), so degraded mode is detected in
seconds, never minutes.

---

## 9. Rate Limiting

### Hierarchy

```
Global (per provider account/API key)
   │  "we, the company, must never get banned by OpenAI"
   ▼
Provider quota pool (per provider, per region)
   │  mirrors the actual TPM/RPM Azure/OpenAI/etc. sold us
   ▼
Tenant budget (per team/project)
   │  "team-risk-analytics gets 40% of the tier-1 pool"
   ▼
User/session (optional, per end-user within a tenant)
   │  "no single end-user can consume a whole tenant's budget"
```

Each level is enforced independently; a request must clear **all** levels
that apply to it. The check order is cheapest-first: local in-memory token
bucket (tenant level, sub-microsecond) before any cross-node-consistent check
(global provider quota, which needs Redis).

### Token Bucket + Sliding Window Hybrid

Pure token bucket is fast but allows a burst at the boundary of two windows
to exceed the intended rate 2x. Pure sliding window is accurate but more
expensive to maintain at high QPS. The gateway uses token bucket for the
**hot-path, per-node-local check** (cheap, allows configured burst
deliberately) and a sliding window for the **cross-node reconciliation**
(accurate, catches sustained overshoot a single node's local bucket can't
see).

```python
class HybridLimiter:
    """
    Fast path: local token bucket, refilled continuously, checked with zero
    network calls. Allows short bursts up to `burst_capacity`.

    Slow path (async, off critical path): every 500ms, reconcile local
    consumption against a Redis-backed sliding window shared across all
    gateway nodes. If the *global* sliding-window count has exceeded the
    tenant's limit even though this node's local bucket had headroom, the
    local bucket's refill rate is throttled down until the window clears.
    """
    def __init__(self, rate_per_s: float, burst_capacity: float):
        self.tokens = burst_capacity
        self.rate = rate_per_s
        self.burst_capacity = burst_capacity
        self.last_refill = time.monotonic()
        self.throttle_factor = 1.0  # adjusted by reconciliation

    def try_consume(self, n: float) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst_capacity, self.tokens + elapsed * self.rate * self.throttle_factor)
        self.last_refill = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    async def reconcile(self, redis, tenant_key: str, limit: float, window_s: int = 60):
        global_count = await redis.execute_command(
            "CL.THROTTLE", tenant_key, limit, limit, window_s  # Redis Cell / sliding-window log
        )
        if global_count.exceeded:
            self.throttle_factor = 0.5   # halve local refill until it clears
        else:
            self.throttle_factor = min(1.0, self.throttle_factor + 0.1)  # ease back up
```

**Consistency trade-off, stated explicitly**: a tenant can burst up to
`(num_gateway_nodes × local_burst_capacity)` above the nominal limit for up
to one reconciliation interval (500ms) before global throttling kicks in.
For a 50-node fleet with a 5% local burst allowance, worst-case overshoot is
bounded at roughly 2% of the tenant's steady-state rate for well under a
second — an acceptable trade against putting a synchronous Redis round-trip
on every single request.

### Token-Based, Not Just Request-Based

Request-count limiting alone is nearly meaningless for LLM traffic — one
request can be 50 tokens or 190,000 tokens. Every limit tier tracks **both**:

```yaml
tenant_limits:
  team-risk-analytics:
    tier1_reasoning:
      rpm: 500
      tpm_input: 800000
      tpm_output: 150000
      burst_multiplier: 1.15    # local bucket allows 15% burst above steady rate
    tier3_fast_cheap:
      rpm: 5000
      tpm_input: 4000000
      tpm_output: 800000
      burst_multiplier: 1.30
```

Because exact output token count isn't known until generation completes, the
gateway uses **quota reservation**: at admission, reserve `estimated_output
= min(max_output_tokens, historical_p90_for_this_route)` tokens against the
budget; on completion, true-up the reservation to the actual usage (refund
the delta if the completion was shorter, charge more if truncation logic
allowed it to run longer than estimated — bounded by `max_output_tokens`
which is always a hard ceiling).

```python
async def admit_and_reserve(req: UnifiedRequest, tenant_limiter: HybridLimiter) -> Reservation:
    est_input = count_tokens(req.messages)  # exact, tokenizer-based, cheap
    est_output = min(req.max_output_tokens, historical_p90(req.model, req.metadata.tenant))
    if not tenant_limiter.try_consume_tokens(input=est_input, output=est_output):
        raise RateLimitExceeded(retry_after_s=tenant_limiter.estimated_wait())
    return Reservation(input=est_input, output_reserved=est_output)

async def true_up(reservation: Reservation, actual_output_tokens: int, tenant_limiter: HybridLimiter):
    delta = actual_output_tokens - reservation.output_reserved
    if delta < 0:
        tenant_limiter.refund_tokens(output=-delta)
    else:
        tenant_limiter.force_consume_tokens(output=delta)  # allowed to go slightly negative on the bucket
```

This is especially important for **streaming** requests, which can hold a
reservation open for minutes — the reservation is what prevents 500 slow
concurrent streams from each individually looking "cheap" at admission time
while collectively exhausting the tenant's real budget.

### Fair-Share Enforcement

At the **provider quota pool** level (shared across tenants hitting the same
upstream key), a simple FIFO would let one bursty tenant starve others. The
gateway uses weighted fair queuing keyed by tenant, so that when the pool is
saturated, each tenant's requests are throttled proportional to their
configured share, not first-come-first-served:

```
pool_capacity_tpm = 2,000,000  (OpenAI tier-5 account TPM limit)
tenant shares (config):  team-a: 40%, team-b: 35%, team-c: 25%
if aggregate demand > pool_capacity:
    each tenant's effective ceiling = share * pool_capacity_tpm
    (unused capacity from an idle tenant IS redistributed proportionally
     to active tenants — shares are a floor guarantee under contention,
     not a hard partition when there's slack)
```

---

## 10. Caching

### Exact-Match Cache

Key design must capture **everything** that affects output determinism:

```python
def exact_cache_key(req: UnifiedRequest) -> str:
    material = {
        "model": req.resolved_model_id,       # concrete model, post-routing — NOT the equivalence class
        "messages": req.messages,
        "tools": req.tools,
        "tool_choice": req.tool_choice,
        "response_format": req.response_format,
        "temperature": req.temperature,
        "top_p": req.top_p,
        "max_output_tokens": req.max_output_tokens,
        "stop_sequences": req.stop_sequences,
    }
    return "exact:" + hashlib.sha256(canonical_json(material).encode()).hexdigest()
```

Deliberately **excluded** from the key: `metadata` (tenant/trace/idempotency
fields don't affect output), `routing` hints (affect *which* model is
chosen, but the resolved model id is already in the key), `cache` directives
themselves. Deliberately **included**: the resolved concrete model id, so a
cache entry from `claude-opus-4-20250514` is never served for a request that
would have routed to `claude-opus-4-20250620` after a routing table update —
correctness over hit rate.

`temperature > 0` requests are cached anyway by default (many callers accept
the staleness trade for cost), but a route can set `cache.mode: "bypass"` or
require `temperature == 0` for cache eligibility if determinism matters more
than hit rate for that use case.

### Semantic Cache

Opt-in, embedding-similarity-based reuse for **near**-duplicate prompts —
different wording, same intent:

```
1. On cache miss (exact), compute an embedding of the normalized prompt
   (cheap, small embedding model, ~5ms).
2. ANN search (e.g. HNSW index in a vector store) against embeddings of
   recently-cached responses for the same model + route, filtered to
   entries still within TTL.
3. If best match similarity >= threshold (route-configurable, default 0.96
   cosine similarity — deliberately high, tuned toward precision over
   recall), return the cached response, tagged `cache_hit: semantic`.
4. Otherwise, proceed to the provider call; cache both the exact key and
   the embedding of the new entry on completion.
```

```python
class SemanticCache:
    def __init__(self, threshold: float = 0.96):
        self.threshold = threshold

    async def lookup(self, req: UnifiedRequest) -> UnifiedResponse | None:
        if not req.cache.semantic_enabled:
            return None
        embedding = await self.embedder.embed(normalize_prompt(req))
        candidates = await self.vector_store.search(
            embedding, filter={"model": req.resolved_model_id, "route": req.route_name}, top_k=3
        )
        if candidates and candidates[0].score >= self.threshold:
            resp = candidates[0].payload
            resp.cache_hit = "semantic"
            resp.cache_similarity = candidates[0].score
            return resp
        return None
```

**Why the threshold is high and the feature is opt-in**: a false-positive
semantic hit returns a *wrong but plausible-looking* answer for a materially
different question — a correctness bug disguised as a performance win. Routes
that enable it are ones where the product owner has explicitly evaluated
that trade-off (e.g., an FAQ-answering bot where near-duplicate questions
truly share an answer), not a blanket default.

### KV / Provider-Native Prompt Cache Management

Distinct from the gateway's own response cache: several providers offer
**server-side prompt caching** — the *provider* caches the KV-attention-state
for a repeated prefix (e.g., a long system prompt or few-shot examples),
charging a steep discount on cache-hit input tokens (§11).

The gateway's job here is not to implement caching itself but to **maximize
the provider's cache hit rate**:

* Structure requests so the stable, reusable portion (system prompt,
  tool definitions, few-shot examples) is a consistent prefix, byte-identical
  across calls — the adapter layer normalizes ordering so semantically
  identical requests produce byte-identical provider payloads.
* For Anthropic, insert explicit `cache_control` breakpoints after the stable
  prefix; for OpenAI, rely on automatic caching but ensure prefix stability;
  for self-hosted vLLM, this maps to the gateway's own **KV-cache-aware
  routing** — sending repeat requests with the same prefix to the *same*
  backend replica (session/prefix affinity) so the local KV cache is warm.

```yaml
# self-hosted vLLM: prefix-affinity routing config
self_hosted:
  kv_cache_affinity:
    enabled: true
    affinity_key: "hash(system_prompt + tools)"   # NOT full message history
    sticky_ttl_s: 600
    fallback_on_affinity_miss: "least_loaded_replica"
```

### Cache Invalidation

| Trigger | Action |
|---|---|
| TTL expiry (route-configured, default 1h exact / 15min semantic) | Passive — entry simply stops being returned |
| Manual purge | Admin API: purge by exact key, by tenant, or by model-id prefix |
| Model deprecation → sunset | All cache entries keyed to a sunset model id are purged proactively — never silently served for content from a model that no longer exists |
| Cache poisoning suspected (§16) | Purge by route + time-range, immediate |

### Savings Tracking

Every cache hit still emits a cost record (§11) computed as **what it would
have cost** minus **what it actually cost** (near-zero for exact-match,
embedding-lookup cost for semantic) — this is what makes "$14,200 saved this
month by caching" a real, queryable number rather than an assumption.

---

## 11. Cost Engine

### Real-Time Cost Computation

```python
def compute_cost(model: ModelCapability, usage: Usage) -> CostRecord:
    regular_input = usage.input_tokens - usage.cached_input_tokens
    input_cost = Decimal(regular_input) / 1_000_000 * model.price_input_per_1m
    cached_cost = (
        Decimal(usage.cached_input_tokens) / 1_000_000 * model.price_cached_input_per_1m
        if model.price_cached_input_per_1m is not None else Decimal(0)
    )
    output_cost = Decimal(usage.output_tokens) / 1_000_000 * model.price_output_per_1m

    would_have_cost_uncached = Decimal(usage.input_tokens) / 1_000_000 * model.price_input_per_1m
    cache_savings = would_have_cost_uncached - (input_cost + cached_cost)

    return CostRecord(
        input_usd=input_cost,
        cached_input_usd=cached_cost,
        output_usd=output_cost,
        cache_savings_usd=max(cache_savings, Decimal(0)),
        total_usd=input_cost + cached_cost + output_cost,
        priced_at=model.price_effective_from,   # pin the price version used, for audit
    )
```

Pricing is always resolved from the **capability registry snapshot active at
request time**, and the resolved `price_effective_from` is stored on the cost
record itself — so a later price change never retroactively alters a
historical bill, and "what did this cost when we made the call" is always
answerable exactly.

### Exactly-Once Cost Records

```
                         ┌──────────────────┐
 Adapter returns success │  Telemetry        │
 (or partial-on-error) ─▶│  Emitter builds    │
                         │  CostRecord         │
                         └─────────┬──────────┘
                                    │  write with idempotency key =
                                    │  (trace_id, attempt_id) — same
                                    │  key used for caller-level
                                    │  idempotency (§8)
                                    ▼
                         ┌──────────────────┐
                         │  Kafka topic        │  partitioned by tenant,
                         │  cost-ledger-events  │  producer uses idempotent
                         │  (durable, replicated)│  producer config (no dupes
                         └─────────┬──────────┘  even under producer retry)
                                    ▼
                         ┌──────────────────┐
                         │  Ledger consumer     │  upserts into cost_records
                         │  (exactly-once via     │  table keyed on the same
                         │  Kafka transactional    │  (trace_id, attempt_id) —
                         │  offset commit)         │  duplicate delivery is a
                         └─────────┬──────────┘  no-op upsert, not a double-add
                                    ▼
                         ┌──────────────────┐
                         │  Budget aggregator   │  materialized rolling sums
                         │  (per tenant/project/  │  per billing period, feeds
                         │  hour, updated async)   │  §9's admission checks
                         └──────────────────┘
```

The write to Kafka happens **before** the response is returned to the caller
for the streaming-done / non-streaming-response case, using a short
synchronous produce-ack (single-digit ms, same-DC Kafka) — this is the one
place a small amount of latency is spent deliberately, because "the response
left the gateway with no durable cost record" is the exact gap we must not
allow. If the produce itself fails, the request still succeeds for the
caller (never fail a completed generation over a billing-pipeline hiccup) but
raises a page — an outbox-style local durable queue on the gateway node
covers the produce-unavailable case as a last resort, drained on Kafka
recovery.

### Budget Enforcement

```sql
CREATE TABLE tenant_budgets (
    tenant            TEXT NOT NULL,
    period            TEXT NOT NULL,          -- 'monthly' | 'daily'
    period_start      DATE NOT NULL,
    soft_limit_usd    NUMERIC(12,2) NOT NULL,  -- alert only
    hard_limit_usd    NUMERIC(12,2) NOT NULL,  -- request rejection
    spent_usd         NUMERIC(12,2) NOT NULL DEFAULT 0,
    updated_at        TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant, period, period_start)
);
```

* **Soft limit** (default 80% of hard limit): fires a Slack/PagerDuty alert
  to the tenant's owning team; requests continue to be served.
* **Hard limit**: new requests from that tenant are rejected with `402
  budget_exceeded` (already-in-flight requests are allowed to complete —
  killing a mid-generation request over budget is a worse experience than a
  brief, bounded overshoot). Overshoot is bounded by
  `(in-flight requests at trip time) × (max cost per request)`, both known
  quantities.
* Enforcement reads from the same async-aggregated budget counters the rate
  limiter uses (§9) — not a synchronous DB read per request — accepting the
  same bounded-staleness trade-off (seconds, not a hard real-time guarantee)
  in exchange for hot-path performance.

### Cost Allocation and Attribution

Every request's cost record carries `tenant`, `project`, `environment`, and
optionally `feature`/`user_id` tags from `metadata` — the aggregation layer
supports arbitrary group-by across these for chargeback reporting:

```sql
SELECT project, model_served, sum(total_usd) AS spend, sum(output_tokens) AS tokens
FROM cost_records
WHERE tenant = 'team-risk-analytics' AND created_at >= date_trunc('month', now())
GROUP BY project, model_served
ORDER BY spend DESC;
```

### Cost Optimization Recommendations

A periodic (daily) batch job over the cost ledger surfaces actionable
recommendations, not just totals:

* Requests to `tier-1` models whose response length/complexity historically
  resembles `tier-3` traffic — candidate for a routing-rule downgrade.
* Routes with low prompt-cache hit rate despite a stable prefix — candidate
  for `cache_control` breakpoint tuning.
* Tenants below their soft limit every period for 3+ periods — candidate for
  budget right-sizing (frees pool capacity for others).

### Prompt Caching ROI

```sql
SELECT tenant,
       sum(cache_savings_usd) AS realized_savings,
       sum(total_usd) AS actual_spend,
       sum(cache_savings_usd) / nullif(sum(total_usd) + sum(cache_savings_usd), 0) AS savings_pct
FROM cost_records
WHERE created_at >= now() - interval '30 days'
GROUP BY tenant
ORDER BY realized_savings DESC;
```

This closes the loop the task requirements call out explicitly: caching
savings are **measured from real cost records**, never assumed from a
theoretical discount rate.

---

## 12. Observability

### OpenTelemetry Integration

```
Gateway span tree per request:

llm_gateway.request  (root span, trace_id = caller-visible id)
 ├─ llm_gateway.authn_authz
 ├─ llm_gateway.cache_lookup
 ├─ llm_gateway.rate_limit_check
 ├─ llm_gateway.routing_decision
 │    attributes: strategy, candidates_considered, candidates_filtered_reason
 ├─ llm_gateway.provider_call  (attempt 1)
 │    attributes: provider, model, region, http_status
 │    ├─ (child, cross-process via W3C traceparent header, if provider
 │    │   supports header passthrough / self-hosted internal call)
 ├─ llm_gateway.provider_call  (attempt 2, fallback)
 └─ llm_gateway.response_normalize
```

Every span carries `tenant`, `project`, `model_requested`, `model_served` as
attributes, so trace queries can be scoped ("show me every slow trace for
team-risk-analytics against claude-opus-4 in the last hour") without a
separate correlation step.

### Custom Metrics

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `gateway_request_duration_ms` | Histogram | provider, model, tenant, streamed | End-to-end latency |
| `gateway_overhead_ms` | Histogram | route | Gateway-added latency, isolated from provider time — the SLO metric from the task doc |
| `gateway_ttft_ms` | Histogram | provider, model | Time to first token, streaming only |
| `gateway_tokens_per_sec` | Gauge | provider, model | Inter-token generation rate, streaming |
| `gateway_cost_usd_total` | Counter | tenant, project, provider, model | Cumulative spend |
| `gateway_cache_hit_ratio` | Gauge | route, cache_type (exact/semantic) | Cache effectiveness |
| `gateway_circuit_breaker_state` | Gauge (0/1/2) | provider, model, region | Health at a glance |
| `gateway_rate_limit_rejections_total` | Counter | tenant, limit_tier | Budget/quota pressure |
| `gateway_fallback_used_total` | Counter | from_provider, to_provider | How often the safety net actually fires |
| `gateway_error_total` | Counter | error_class, provider | Classified error rate |

### Error Classification and Alerting

| Error class | Alert threshold | Severity |
|---|---|---|
| `auth` | Any occurrence | Page immediately — credential issue, likely affects all traffic to that provider |
| `provider_5xx` / `overloaded` | >10% of requests to one (provider, model) over 5 min | Page — likely provider incident, verify against their status page |
| `rate_limited` | >5% sustained over 10 min for one tenant | Ticket, not page — usually a tenant traffic pattern issue, notify tenant owner |
| `circuit_open` | Any breaker open >5 min | Page — fallback capacity may be thinning |
| `all_providers_unavailable` | Any occurrence | Page immediately — worst-case failure mode |
| `budget_exceeded` | N/A (expected behavior) | Notify tenant owner via Slack, not an on-call page |
| `gateway_internal` (bug in gateway itself, not provider) | Any occurrence | Page — this is our bug |

### Dashboards

1. **Fleet health** — circuit breaker states, error rates, and P50/P99
   latency per (provider, model), refreshed every 10s.
2. **Cost** — real-time spend rate, budget burn-down per tenant, cache
   savings, deprecated-model spend.
3. **Per-tenant** — the "is my traffic slow/failing/expensive" single-pane
   view called out in the task requirements: latency histogram, error
   breakdown, and cost rollup on one screen scoped to that tenant's traces.
4. **Routing effectiveness** — fallback rate, shadow/A-B experiment
   comparison, equivalence-class distribution over time.

---

## 13. Security

### API Key Management

* Provider credentials live in a secrets vault (HashiCorp Vault / AWS
  Secrets Manager), referenced from config by `vault://` URI, never
  hardcoded or caller-visible.
* Gateway nodes fetch credentials at startup and on rotation-triggered
  refresh (vault lease renewal), held in memory only, never written to disk
  or logs.
* Rotation is zero-downtime: the vault issues a new credential with overlap
  time before the old one is revoked; the adapter layer picks up the new
  credential via the same push mechanism as config (§2), and in-flight
  requests using the old credential complete normally within the overlap
  window.
* Tenants **never** hold or see provider API keys — this is precisely the
  problem statement's "thirty teams each hold their own keys" that the
  gateway exists to eliminate.

### PII Handling

Two independently configurable policies, deliberately not conflated:

```yaml
tenant_security_policy:
  team-risk-analytics:
    pii_redaction:
      scope: "logs_only"       # logs_only | pre_send | none
      detectors: [email, phone, ssn, credit_card, name_ner]
      action: "mask"           # mask | drop_field | reject_request
    logging:
      retain_full_content: false
      retention_days: 30
      audit_metadata_only: true
```

* `logs_only` (default): the original request is sent to the provider
  unmodified (the provider needs real content to answer correctly); what
  hits the gateway's own logs/traces is redacted first.
* `pre_send`: redaction happens **before** the provider ever sees the
  content — for tenants under a compliance regime that prohibits sending
  raw PII to a third party at all. Explicitly documented as a quality
  trade-off: a redacted prompt may produce a worse answer.
* PII detection runs as a pipeline stage (regex + lightweight NER model) in
  the Request Normalizer, cheap enough to stay under the latency budget
  (~2-3ms for typical message sizes), applied to both request and response
  before either is persisted to logs.

### Request/Response Logging Policy

| Tenant tier | Full content logged? | Retention | Access |
|---|---|---|---|
| Default | No — metadata + redacted preview only | 30 days | Platform team, on-call |
| Audit-required (e.g. regulated finance workflows) | Yes, full content, encrypted at rest | Per tenant's compliance requirement (default 1yr) | Restricted ACL, access itself audited |
| PII-strict | No — not even redacted preview, hash only | 30 days | Platform team, on-call |

### Audit Trail

```sql
CREATE TABLE audit_log (
    trace_id        TEXT NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL,
    caller_identity TEXT NOT NULL,     -- service identity from mTLS/JWT, not end-user
    tenant          TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model_served    TEXT NOT NULL,
    action          TEXT NOT NULL,     -- 'request_served' | 'request_rejected' | 'config_changed' | 'key_rotated'
    reject_reason   TEXT,
    cost_usd        NUMERIC(12,6),
    content_logged  BOOLEAN NOT NULL,  -- was full content retained per policy?
    PRIMARY KEY (trace_id, timestamp)
) PARTITION BY RANGE (timestamp);
```

Retained independently of the (optional) content log — the audit trail
always exists even for PII-strict tenants where content itself is never
retained, satisfying "who sent what metadata to which provider, when, and
what it cost" without requiring full content retention.

### Data Residency

Routing's policy filter (§6, step 3) is the enforcement point: a tenant
tagged `region:eu` never has an EU-restricted candidate list resolve to a
US-only provider region, full stop — this is a hard filter, not a preference,
and a request that cannot be satisfied within the allowed regions fails
explicitly (`400 no_compliant_provider`) rather than silently routing
out-of-region.

---

## 14. Data Models

### Core Schemas

```sql
-- Provider account/credential configuration
CREATE TABLE provider_configs (
    provider          TEXT NOT NULL,
    account_id        TEXT NOT NULL,        -- e.g. Azure resource, AWS account, OpenAI org
    region            TEXT NOT NULL,
    credential_ref     TEXT NOT NULL,        -- vault:// URI, never the raw secret
    tpm_quota          INTEGER NOT NULL,      -- upstream-negotiated quota, mirrors provider account limit
    rpm_quota          INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    PRIMARY KEY (provider, account_id, region)
);

-- Routing rules (versioned, so a rollback is a version pointer flip)
CREATE TABLE routing_rules (
    rule_id       UUID PRIMARY KEY,
    version        INTEGER NOT NULL,
    name           TEXT NOT NULL,
    match_spec     JSONB NOT NULL,
    action_spec    JSONB NOT NULL,
    priority        INTEGER NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Request log (sampled full detail + 100% metadata)
CREATE TABLE request_log (
    trace_id           TEXT NOT NULL,
    tenant              TEXT NOT NULL,
    project              TEXT,
    model_requested       TEXT NOT NULL,
    model_served           TEXT NOT NULL,
    provider                TEXT NOT NULL,
    region                    TEXT NOT NULL,
    status_code               INTEGER NOT NULL,
    error_class                TEXT,
    stop_reason                 TEXT,
    input_tokens                 INTEGER,
    output_tokens                 INTEGER,
    cached_input_tokens             INTEGER,
    gateway_overhead_ms               INTEGER,
    ttft_ms                             INTEGER,
    total_latency_ms                     INTEGER,
    fallback_used                         BOOLEAN NOT NULL DEFAULT FALSE,
    cache_hit                             TEXT,      -- null | 'exact' | 'semantic'
    created_at                            TIMESTAMPTZ NOT NULL
) PARTITION BY RANGE (created_at);

-- Cost record — the exactly-once billing artifact
CREATE TABLE cost_records (
    trace_id          TEXT NOT NULL,
    attempt_id         TEXT NOT NULL,      -- disambiguates retries within a trace
    tenant              TEXT NOT NULL,
    project              TEXT,
    environment           TEXT,
    provider               TEXT NOT NULL,
    model_served             TEXT NOT NULL,
    input_usd                 NUMERIC(12,6) NOT NULL,
    cached_input_usd            NUMERIC(12,6) NOT NULL DEFAULT 0,
    output_usd                    NUMERIC(12,6) NOT NULL,
    cache_savings_usd                NUMERIC(12,6) NOT NULL DEFAULT 0,
    total_usd                          NUMERIC(12,6) NOT NULL,
    priced_at                            TIMESTAMPTZ NOT NULL,
    created_at                            TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (trace_id, attempt_id)
);

-- Rate limit bucket state (Redis-backed in practice; SQL shape shown for clarity)
CREATE TABLE rate_limit_buckets (
    scope_type     TEXT NOT NULL,   -- 'global' | 'provider' | 'tenant' | 'user'
    scope_key       TEXT NOT NULL,
    window_start     TIMESTAMPTZ NOT NULL,
    window_s          INTEGER NOT NULL,
    request_count      INTEGER NOT NULL DEFAULT 0,
    input_tokens         BIGINT NOT NULL DEFAULT 0,
    output_tokens          BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (scope_type, scope_key, window_start)
);
```

---

## 15. Deployment

### Multi-Region Topology

```
                       ┌────────────────────┐
                       │   Global Anycast/GSLB │
                       └──────────┬──────────┘
              ┌───────────────────┼───────────────────┐
              ▼                    ▼                    ▼
     ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
     │  us-east-1        │  │  eu-west-1        │  │  ap-southeast-1  │
     │  Gateway cluster    │  │  Gateway cluster    │  │  Gateway cluster  │
     │  (N nodes, stateless)│  │  (N nodes, stateless)│  │  (N nodes, stateless)│
     └────────┬────────┘  └────────┬────────┘  └────────┬────────┘
              │                    │                    │
              └───────────┬────────┴────────┬───────────┘
                           ▼                  ▼
                  ┌──────────────┐  ┌──────────────────┐
                  │ Config Service │  │ Redis (rate limit, │
                  │ (etcd, raft,    │  │ cache) — regional   │
                  │ 5 nodes, multi-  │  │ clusters, NOT one    │
                  │ region members)  │  │ global instance       │
                  └──────────────┘  └──────────────────┘
                           │
                           ▼
                  ┌──────────────┐
                  │ Cost Ledger     │  Kafka, regional producers,
                  │ (Kafka + PG)     │  aggregated centrally async
                  └──────────────┘
```

* Gateway nodes are region-local and stateless — a region losing its Redis
  or falling out of the config-service quorum degrades to **last-known-good
  config, local-only rate limiting** (§16) rather than failing outright.
* Requests are routed to the caller's nearest healthy region by the GSLB;
  cross-region provider calls happen only when a region's local provider
  presence can't serve a requested capability (rare, and accepted as a
  latency cost for that edge case, e.g. an EU region needing an
  APAC-exclusive self-hosted model).

### Connection Pooling to Providers

```yaml
provider_connection_pools:
  openai:
    max_idle_connections: 200
    max_connections_per_host: 500
    idle_timeout_s: 90
    http2: true               # multiplexes many logical requests over few TCP conns
  anthropic:
    max_idle_connections: 200
    max_connections_per_host: 500
    idle_timeout_s: 90
    http2: true
  self_hosted:
    max_idle_connections: 1000  # same-DC, cheap to hold more
    max_connections_per_host: 2000
    idle_timeout_s: 300
```

Pre-warmed pools (a background keepalive prevents idle connections from
being torn down right before a traffic spike) are what keep the "connect
timeout" bucket in §7's timeout table rare rather than routine — a cold TLS
handshake to a provider can itself cost 50-150ms, which would blow the
overhead budget on its own if paid per request.

### Zero-Downtime Config Updates

1. Operator (or CI, for automated pricing updates) submits a new config
   version to the config service.
2. Config service validates schema, runs a **dry-run diff** against current
   routing decisions for a sample of recent real traffic (catches "this
   rule change would silently stop routing to model X" before it ships).
3. New version is pushed to gateway nodes via long-poll/watch; each node
   builds the new immutable snapshot (§5) in the background and atomically
   swaps the pointer — in-flight requests finish against whichever snapshot
   they started with, no request ever sees a torn read.
4. Rollout is staged (canary region first, 5 min bake, then fleet-wide) with
   automatic rollback if error rate or fallback rate spikes post-push.
5. Every version is retained, so rollback is "point at the previous version
   id," not "reconstruct the previous config" — a few-second operation.

### Graceful Degradation

| Dependency lost | Gateway behavior |
|---|---|
| Config service unreachable | Continue serving on last-known-good in-memory snapshot; alert; refuse *new* model/rule registrations until recovered |
| Redis (rate limit reconciliation) unreachable | Fall back to pure local token-bucket enforcement per node (no cross-node reconciliation) — slightly less accurate fairness, never a hard outage |
| Redis (cache) unreachable | Cache lookups skip silently (treated as miss), requests proceed to providers — degraded cost/latency, not degraded correctness |
| Kafka (cost ledger) unreachable | Local durable outbox buffers cost records; requests still succeed; budget enforcement falls back to last-known aggregate + a conservative safety margin until the ledger catches up |
| One region's provider connectivity degraded | Circuit breakers open per (provider, region); traffic in that region falls back to cross-region candidates per routing policy, at a latency cost, before failing |

---

## 16. Failure Scenarios

### Scenario 1: Full Provider Outage (e.g., OpenAI down for 40 minutes)

```
t+0s     Health probes + real traffic error rate spike for openai:* candidates
t+0-10s  Circuit breakers for every (openai, model, region) trip to OPEN
         (independently, per model — this happens within one 60s window
          reaching the 50%-failure/20-sample threshold, typically <10s
          under real load)
t+10s    Router's health filter (§6 step 4) drops all openai candidates from
         every equivalence class; traffic to tier-1/tier-2/tier-3 classes
         that had openai as a candidate now resolves entirely to
         anthropic/google/bedrock candidates automatically — no operator
         action required
t+10s    Alert fires: "circuit_open sustained >5min risk" pre-alert, plus
         immediate fallback-rate spike alert
t+30s    Dashboard shows fallback_used_total spiking — on-call confirms via
         OpenAI's status page, no gateway-side action needed, just visibility
t+40min  OpenAI recovers; half-open probes (1-in-20 trial traffic) start
         succeeding; breaker closes after 5 consecutive successes; traffic
         gradually shifts back per normal routing weights
```
Caller impact: zero code changes, zero manual intervention, transparent
fallback for callers whose equivalence class had a healthy alternative.
Callers who had **pinned** a concrete `openai:gpt-4o` model id with
`fallback_allowed: false` see `503 all_providers_unavailable` — a documented,
explicit trade-off of pinning (§5).

### Scenario 2: Silent Degradation (provider returns 200 OK but slow/garbled)

The dangerous case — no error to trigger the breaker's failure-rate signal.
Mitigations:

* Latency-based breaker trip: independent from the error-rate trigger, a
  sustained P99 latency >3x the 7-day rolling baseline for a (provider,
  model) also trips to OPEN, even with 100% success-by-HTTP-status.
* Output-shape sanity checks in the Response Normalizer: empty content with
  `stop_reason: stop` (should have produced tokens), or a JSON-mode request
  returning unparseable JSON, are classified as `error_class: malformed_response`
  and **do** count against the failure-rate breaker even though the HTTP
  status was 200 — this is what actually catches "the provider is silently
  broken."

### Scenario 3: Rate Limit Exhaustion Mid-Stream

A tenant's TPM budget is consumed by other concurrent requests while a
long-running stream is already in flight (admitted under §9's reservation).
Policy: **never kill an in-flight generation over a limit that was satisfied
at admission time** — the reservation already accounted for it. New requests
from that tenant are rejected (`429`) until the reservation frees up on
completion/true-up. This bounds worst-case overshoot to
`(concurrent in-flight reservations at trip time)`, a known, monitored
quantity (`gateway_inflight_reserved_tokens` gauge).

### Scenario 4: Cost Budget Exceeded Mid-Request-Burst

Hard limit trips (§11). In-flight requests complete (same principle as
above). New requests get `402 budget_exceeded` with the current
period's reset time. Tenant owner is paged via the alert configured in
`tenant_security_policy`/budget config — not a gateway on-call page, this is
expected, self-service-resolvable behavior (raise budget, or wait for
period reset).

### Scenario 5: Cache Poisoning (a bad response gets cached and repeatedly served)

Root causes considered: (a) a provider returned a malformed/wrong response
that still looked like a valid 200, or (b) a semantic-cache false-positive
match served an actually-different question's answer.

Mitigations:
* Response Normalizer's sanity checks (Scenario 2) run **before** the cache
  write, not just before the breaker signal — a malformed response is never
  cached in the first place.
* Semantic cache threshold is intentionally conservative (0.96) and opt-in
  per route, minimizing blast radius.
* Manual purge API (§10) for the "we shipped a bad cache entry, purge by
  route + time-range" incident-response path — designed to be a single
  command, not a data migration.

### Scenario 6: Credential Rotation During Live Traffic

Vault issues new credential with a 5-minute overlap before revoking the old
one. Adapter layer picks up the new credential via the same config-push
mechanism (§15) within its normal propagation window (<1s typical, bounded
at 5s worst-case by watch/long-poll retry settings) — well inside the
overlap window, so no in-flight request ever sees an auth failure due to
rotation timing under normal operation. If a node fails to pick up the
rotation before overlap expires (e.g., that node was partitioned from the
config service), its requests to that provider start failing with
`error_class: auth`, which pages immediately (§12) — the alert path is the
safety net for the edge case, not the primary mechanism.

---

## 17. Trade-offs

| Decision | What we chose | What we gave up | Why |
|---|---|---|---|
| Thin proxy vs. smart gateway | Smart gateway (routing, caching, cost logic in the gateway) | Simplicity, lower gateway-added latency ceiling | The whole point is centralizing decisions that were previously duplicated per-team; a thin proxy would just move the API-key problem without solving the routing/cost/reliability problem |
| Per-request routing vs. session affinity | Per-request by default, opt-in affinity for KV-cache-sensitive self-hosted routes | Some prompt-cache hit-rate loss for providers without native caching, if a session bounces between models | Session affinity everywhere would concentrate load unevenly and complicate the stateless-node deployment model; the affinity we do use (§10) is scoped narrowly to where it earns its cost |
| Centralized gateway vs. sidecar-per-service | Centralized, horizontally-scaled cluster | Every caller has a network hop to a shared service instead of a local sidecar | Centralization is what makes fleet-wide rate limiting, circuit breaking, and cost aggregation coherent — a sidecar-per-service model would need its own cross-instance coordination layer to get the same guarantees, which is strictly more complex, not less |
| Bounded rate-limit consistency vs. strict global consistency | Bounded overshoot (~2%), local-first enforcement | Perfect global accuracy | A synchronous cross-node call on every request would blow the 50ms P99 overhead budget; the overshoot bound is small and monitored |
| Exact-match cache on by default, semantic cache opt-in | Correctness-conservative default | Some missed cost savings from near-duplicate prompts on routes that never opt in | A false-positive exact-match hit is structurally impossible (it's a hash of exact content); a false-positive semantic hit is a genuine correctness risk that needs a human judgment call per route |
| Pinning concrete model ids is allowed | Reproducibility for callers who need it (eval harnesses) | Those callers don't get automatic fallback/migration | Explicit, visible trade-off beats a gateway that silently overrides what a caller asked for |
| Provider health from real traffic + synthetic probes, weighted toward real traffic | Slower to detect issues that only manifest at very low request volume | Faster, more accurate detection of load-dependent degradation (the common case) | The synthetic-probe-only design was explicitly ruled out (§4) as prone to false negatives under exactly the conditions (load-dependent degradation) that matter most |

---

## 18. Evolution Path

| Version | Scope | What's added | What's deliberately deferred |
|---|---|---|---|
| **v1** | Proxy + routing | Unified API, provider adapters (3-4 providers), capability registry (manual only), basic least-cost/lowest-latency routing, retry + circuit breaker, streaming proxy | No caching, no semantic routing, coarse per-tenant rate limits (RPM only, no TPM), cost tracking is batch/offline not real-time |
| **v2** | Caching + cost | Exact-match cache, real-time cost engine with exactly-once ledger, token-based rate limiting with reservation, budget soft/hard limits, full observability (OTel + dashboards) | No semantic cache, no A/B/shadow traffic, equivalence classes still hand-curated with no drift detection |
| **v3** | Semantic routing + optimization | Semantic cache (opt-in), model equivalence classes with auto-discovery + drift alerts, shadow traffic and A/B experiment framework, prompt-cache-aware request shaping (§10's provider-native cache optimization), cost optimization recommendations | No automatic model selection — routing strategies are still operator-configured, not learned |
| **v4** | Auto-model-selection | A learned/heuristic policy that picks the cheapest model *predicted* to meet a per-route quality bar for a given request (using shadow-traffic-collected quality signals from v3 as training/eval data), automatic equivalence-class membership proposals (still human-approved before activation), predictive autoscaling of provider connection pools from traffic forecasts | Fully autonomous, unreviewed model promotion remains out of scope indefinitely — quality regressions from an under-tested model are exactly the failure mode the registry's manual-registration gate (§5) exists to prevent, and v4 does not remove that gate, it only feeds better data into the human decision |

Each version is independently shippable and each version's absence is a
documented, acceptable state — v1 alone, run for months, is a legitimate
stopping point for a team that doesn't yet need caching or fine-grained
budgets.

---

## 19. Capacity Estimates

**Throughput.** 50k req/s sustained, 120k req/s peak (given). With an average
of 1,800 tokens/request combined (in+out), sustained token throughput is
~90M tokens/s peak-adjacent at 120k req/s — but the binding constraint in
practice is **upstream provider TPM quotas**, not gateway compute; the
gateway's own compute cost per request (routing decision, normalization,
telemetry) is dominated by JSON (de)serialization and is on the order of
0.3-0.8ms of CPU time — at 120k req/s that's roughly 40-95 CPU-seconds of
work per wall-clock second, so ~50-100 gateway-node vCPUs cover routing logic
alone; provisioning rounds up to **~40 nodes × 4 vCPU** for headroom plus
connection-pool/TLS overhead, independently scaled from any provider-side
constraint.

**Streaming connections.** 200k concurrent streams at peak. Each held
connection is cheap (a goroutine/task + a bounded buffer, §7's 64-event cap
at ~1-2KB/event ≈ ~100KB worst-case per stream) — memory budget ≈ 200k × 150KB
≈ 30GB across the fleet, comfortably under 1GB/node across 40 nodes.

**Rate limiter state.** 500 tenants × ~10 limit tiers each × a handful of
scope levels ≈ low tens of thousands of active bucket keys — trivially small
for Redis, sub-millisecond lookups.

**Cost ledger.** At 120k req/s peak, cost records at ~200 bytes each ≈ 24MB/s
into Kafka at peak, ~2TB/day at sustained rate if peak held continuously
(it isn't) — well within a modest Kafka cluster's throughput, and the
Postgres-backed aggregate tables are rollups, not per-request storage, so
long-term storage growth is bounded by partitioned request-log retention
policy (default 30-90 days raw, indefinite for cost aggregates).

**Cache.** Exact-match cache sized for a working set of recent distinct
requests; assuming a 20% cache-eligible hit rate at 50k req/s sustained and a
1-hour TTL, working set ≈ 50k req/s × 0.2 × 3600s ≈ 36M entries; at ~4KB
average cached response size, ≈ 144GB — sized to a dedicated Redis/cache
cluster, not colocated with rate-limit state.

---

## 20. Exercises

1. **Extend the routing DSL** to support cost-quality Pareto routing: given a
   per-route "quality floor" and "cost ceiling," select the cheapest
   candidate that historically meets the quality floor (using shadow-traffic
   eval scores from §18's v3). Define the data model for quality scores and
   how staleness of that data affects the routing decision's trust level.

2. **Design the auto-discovery drift detector in full.** Given a registered
   capability claim (`supports_json_mode: true`) and a canary probe result,
   specify the exact probe request, how you distinguish "provider changed
   behavior" from "our probe was flaky," and what the alert payload contains
   so an on-call engineer can act on it in under two minutes.

3. **Work the cross-region failover latency arithmetic.** A tenant pinned to
   `eu-west-1` loses all in-region provider connectivity. Walk the full path
   to a cross-region fallback: config lookup, breaker state propagation
   (is breaker state regional or global?), added RTT, and the P99 latency a
   caller in that region should now expect. State every assumption.

4. **Design idempotency for tool calls specifically.** §8 covers request-level
   idempotency, but a tool call itself (e.g., "charge this credit card") may
   not be idempotent on the caller's side even if the gateway's request
   handling is. What can the gateway's API expose to help callers build safe
   tool execution on top of a system that may, in rare failure modes, return
   the same tool call twice?

5. **Design the dry-run diff tool** referenced in §15's zero-downtime config
   update flow. Given a proposed routing-rule change and a sample of the last
   hour's real traffic, produce a report of exactly which requests would now
   route differently, and flag any that would newly fail a hard filter
   (residency, capability). What sample size and time window give you
   statistical confidence without making every config change wait an hour?

6. **Semantic cache poisoning, adversarially.** Assume a malicious or
   careless caller can submit requests. Can they engineer a semantic-cache
   entry that gets served to an unrelated tenant's genuinely different
   question? Walk the exact conditions required, and redesign the cache key
   /isolation boundary (§10) to make cross-tenant cache bleed structurally
   impossible, not just unlikely.

7. **Budget reservation vs. streaming duration.** §9's quota reservation
   estimates output tokens at admission. For a stream that runs far longer
   than its `historical_p90` estimate (a legitimate long agentic tool-call
   loop, not abuse), design the mid-stream true-up mechanism: at what
   intervals do you re-check budget against actual tokens generated so far,
   and what happens to the stream if the tenant's real budget is exhausted
   at token 40,000 of a still-running generation?

8. **Design Variant: single-region, 10 engineers → 1 engineer, 500 req/s.**
   A much smaller deployment: one provider primary + one fallback, no
   semantic cache, no multi-region. What collapses out of this design
   entirely, what stays because it's cheap insurance even at small scale,
   and at what request-rate or team-size threshold does each dropped
   component need to come back? Justify each answer against a concrete
   number, not a feeling.
