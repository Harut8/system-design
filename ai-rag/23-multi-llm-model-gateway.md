# 23 — multi-LLM integration and model gateway architecture

> **Prerequisites:** [`20-langchain-architecture-and-internals.md`](20-langchain-architecture-and-internals.md)
> (the `Runnable` protocol solves provider-abstraction at the *client library* layer — one process,
> one language; this chapter solves the same abstraction problem at the *platform* layer — many
> applications, many languages, many teams, one network hop — and the two are complementary, not
> competing, solutions to the same underlying problem of "N applications need M providers"),
> [`22-agent-orchestration-patterns.md`](22-agent-orchestration-patterns.md) (§13's platform-ownership
> argument — one interception point every agent's tool call routes through, so a policy change touches
> one file instead of every agent — is the identical argument this chapter makes about model calls
> instead of tool calls; read that section first so the gateway's central-enforcement design in §1
> here reads as an instance of a pattern you've already justified once, not a new one to independently
> defend), [`../sre-observability/26-llm-and-ai-observability.md`](../sre-observability/26-llm-and-ai-observability.md)
> (the OTEL GenAI semantic conventions, span design, and token/cost metrics that chapter builds for a
> single call site are what §9 here wires into a gateway sitting in front of every call site in an
> organization — the instrumentation is identical, only the blast radius differs),
> [`../distributed-systems/README.md`](../distributed-systems/README.md) (the gateway is, structurally,
> a stateless reverse proxy with a circuit breaker and a rate limiter in front of an unreliable
> upstream — every mechanism in §5 and §6 here is a named pattern from that folder applied to LLM
> providers specifically rather than a novel invention).
>
> **Feeds into:** `11-token-accounting-and-cost.md` (planned — §7's per-tenant cost attribution here
> is the concrete mechanism that chapter will generalize into full unit-economics modeling),
> `12-serving-latency-and-caching.md` (planned — §10 and §11's streaming and caching material here are
> the gateway-specific instance of that chapter's broader serving-latency treatment),
> `16-multi-tenancy-and-isolation.md` (planned — §6 and §8's per-tenant rate limiting and RBAC here are
> the model-access-control slice of that chapter's general multi-tenancy problem),
> `19-build-vs-buy.md` (planned — §15's LiteLLM/Portkey/Helicone comparison here is a direct input to
> that chapter's build-vs-buy framework, applied to exactly one category of tool).
>
> **THESIS:** A model gateway is not middleware for convenience — it is the platform primitive that
> converts an **N × M integration problem** (N applications, M providers, each pair hand-wired) into an
> **N + M problem** (each application integrates once with the gateway, each provider is onboarded once
> behind an adapter). Every capability people associate with a gateway — routing, fallback, rate
> limiting, cost control, observability, RBAC — is a *consequence* of having a single, central
> interception point for every model call in an organization, not an independent feature you bolt on.
> The hard engineering is not calling three different provider SDKs; any junior engineer can write
> three adapters in an afternoon. The hard engineering is making the gateway **disappear as a source of
> latency, single-point-of-failure risk, and semantic drift** while it sits in the critical path of
> every request — which means the gateway itself must be more reliable, better observed, and more
> carefully capacity-planned than any single provider it fronts, or you have built a fragile chokepoint
> and called it infrastructure.
>
> A second, quieter thesis threads through the whole chapter: the LLM gateway's hardest problems are
> **not** LLM-specific. Circuit breakers, sliding-window rate limiters, RBAC, blue/green deploys — all
> of it is fifteen-year-old distributed-systems practice. What *is* LLM-specific, and what actually
> separates a senior candidate from someone who has only read a gateway's README, is the small set of
> places where LLM semantics break assumptions those old patterns were built on: rate limiting a
> resource (tokens) you can't measure until *after* you've spent it, retrying calls whose "success"
> means "got *a* plausible response" rather than "got *the* response," and caching outputs that are
> non-deterministic by construction. §5, §6, and §11 are where the real interview signal lives.

---

## Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Why a model gateway — the N×M problem](#1-why-a-model-gateway--the-nm-problem)
2. [Gateway architecture and the request lifecycle](#2-gateway-architecture-and-the-request-lifecycle)
3. [The provider abstraction layer](#3-the-provider-abstraction-layer)
4. [Model routing](#4-model-routing)
5. [Fallback and resilience](#5-fallback-and-resilience)
6. [Rate limiting and quota management](#6-rate-limiting-and-quota-management)
7. [Cost management](#7-cost-management)
8. [Authentication and authorization](#8-authentication-and-authorization)
9. [Observability](#9-observability)
10. [Streaming through the gateway](#10-streaming-through-the-gateway)
11. [Caching](#11-caching)
12. [Configuration management](#12-configuration-management)
13. [The SDK side — client experience](#13-the-sdk-side--client-experience)
14. [Production deployment](#14-production-deployment)
15. [Comparison with existing solutions — build vs buy](#15-comparison-with-existing-solutions--build-vs-buy)
16. [Interview questions](#16-interview-questions)
17. [Lab exercises](#17-lab-exercises)
18. [Real-world cases — incidents with numbers](#18-real-world-cases--incidents-with-numbers)

---

## Start here — the whole chapter in plain words

**The problem.** A company starts using AI models in many apps. Each team picks its own provider
(OpenAI, Anthropic, Azure, Bedrock…) and writes its own code for keys, retries, limits and logging.
Soon nobody knows who spends how much, one provider outage takes down some apps but not others, and
switching models means changing code in every app. A **model gateway** is one shared service that
sits between all apps and all providers, so these jobs are done once, in one place.

**A real-world example.** A mid-size online retailer has 6 apps that use LLMs (support bot, product
description writer, search helper, fraud notes, HR bot, internal coding assistant) and 4 providers.
The numbers below are illustrative.

1. **Without a gateway.** Up to 6 × 4 = 24 separate integrations. The support bot's provider has a
   45-minute outage and the bot is down for all 45 minutes, because only that team's code knows about
   one provider. Finance asks "who spent the $38,000 on AI last month?" and it takes a week of
   spreadsheets to answer. One app has a bug that loops, sends 2 million tokens a minute, and
   causes 429 "too many requests" errors for every other app sharing the same provider account.
2. **Put a gateway in front.** Now 6 + 4 = 10 integrations: each app connects to the gateway once,
   and each provider is connected to the gateway once.
3. **Routing (§4).** Apps ask for `model="default"` or `model="fast"`. The gateway decides which real
   model answers. Short, easy questions go to a cheaper model.
4. **Fallback (§5).** When the support bot's main provider fails, a circuit breaker notices after 5
   failures and sends traffic to a backup provider. Users see answers again within seconds.
5. **Rate limits (§6).** Each app has a token budget per minute, so the looping app is stopped at
   its own limit instead of hurting the other 5.
6. **Cost tracking (§7).** Every call is recorded with its team, model, tokens and cost. "Who spent
   the $38,000?" becomes one query.
7. **Keys and permissions (§8).** Provider keys live only in the gateway. A leaked key is rotated in
   one place, not searched for in 6 codebases.
8. **The catch (§1.3, §14).** If the gateway goes down, all 6 apps go down. So it must run as many
   copies, with shared state, careful health checks and safe deploys.

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Model gateway | one service every app calls instead of calling providers directly | a building's reception desk: visitors don't wander to offices on their own |
| Provider | a company or server that runs the model (OpenAI, Anthropic, self-hosted vLLM) | a supplier |
| Adapter | code that translates the gateway's common format into one provider's format | a travel plug adapter |
| Canonical format | the one request/response shape used inside the gateway | a company's standard order form, whatever the supplier |
| Alias | a nickname like "default" that points to a real model in config | a speed-dial button: you change the number, not the button |
| Routing | choosing which real model answers a request | a dispatcher assigning a taxi |
| Fallback chain | an ordered list of backups to try if the first one fails | plan A, plan B, plan C |
| Circuit breaker | stops calling a provider that keeps failing, and tests it again later | a fuse that trips, then gets reset carefully |
| Rate limit / quota | the most tokens or requests a team may use per minute or month | a data cap on a phone plan |
| Token bucket | a limiter that refills at a steady rate and allows short bursts | a jar of coins refilled every second; spend them, then wait |
| Reserve-then-settle | block an estimated amount up front, then correct to the real amount | a hotel holding $200 on your card, then charging the actual bill |
| Prompt caching (provider side) | the provider reuses work for a repeated start of the prompt and charges less | a copy shop that already has your first 50 pages ready |
| Semantic cache | reusing an answer for a question that looks similar | answering from memory because the question "sounds like" one you heard |
| TTFT | time until the first word of the answer arrives | how long until the waiter brings the first dish |
| RBAC | rules for which team may use which model | key cards that open only some doors |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| `N`, `M` | number of applications, number of providers | 5 – 100 apps; 2 – 6 providers | 6 apps × 4 providers = 24 links without a gateway, 6 + 4 = 10 with one |
| TPM / RPM | tokens per minute / requests per minute a provider or tenant may use | set per contract or per tenant | a team capped at 100,000 TPM |
| 429 | HTTP status "too many requests" (you hit a rate limit) | — | the provider refuses because the org's shared TPM is used up |
| 5xx / 400 | server-side error (retry or fall back) / bad request (don't retry) | — | a 503 goes to the backup; a malformed request does not |
| `input_tokens`, `output_tokens` | tokens sent to the model / tokens it generated | 100 – 100,000 / 50 – 4,000 | a 3,000-token prompt and a 400-token answer |
| `cached_input_tokens` | input tokens served from the provider's prompt cache (cheaper) | 0 – 90% of input | 5,000 of a 6,000-token prompt reused |
| `reasoning_tokens` | hidden "thinking" tokens on reasoning models, billed as output | 0 – many thousands | a hard math question uses 3,000 invisible tokens |
| `max_tokens` | the most output tokens the model may generate | 256 – 8,192 | `max_tokens=1,000` caps a long answer |
| `temperature` | how random the output is; 0 = most repeatable | 0 – 1 | 0 for extraction, 0.7 for creative text |
| `input_per_1k`, `output_per_1k`, `cached_input_per_1k` | price per 1,000 tokens of each kind (the code's dated examples) | fractions of a cent | $0.0025 per 1K = $2.50 per million |
| `weight`, `canary_pct` | share of traffic sent to one target / to the new canary model | 0 – 1 | `canary_pct=0.05` → 5% of sessions try the new model |
| p50 / p95 / p99 | latency that 50% / 95% / 99% of requests are faster than | ms to s | p95 = 3 s → 1 request in 20 is slower than 3 s |
| TTFT | time to first token of a streamed answer | 0.3 – 2 s | first word shows after 0.6 s |
| `latency_slo_ms` | the latency target the router tries to meet | 1,000 – 5,000 ms | pick the cheapest model with p50 under 2,000 ms |
| `error_rate_ceiling` | error rate above which a target counts as unhealthy | 0.10 | 12 errors in 100 calls → skipped |
| `window_seconds` | how far back the health tracker looks | 60 s | only the last minute of calls counts |
| `failure_threshold` | failures in a row before the circuit breaker opens | 5 | 5 timeouts → stop calling that model |
| `recovery_timeout_s` | how long the breaker stays open before a test call | 30 s | after 30 s, one trial request is allowed |
| `half_open_max_calls` | trial calls allowed while testing recovery | 1 | one request checks whether the provider is back |
| `connect_timeout_s`, `first_token_timeout_s`, `total_timeout_s`, `per_fallback_hop_timeout_s` | time limits for connecting, first token, the whole call, and each backup step | 5 s, 10 s, 60 s, 15 s | a stuck provider is abandoned after 10 s with no first token |
| `max_attempts`, `base_delay_s`, `max_delay_s` | retry count and wait times (doubling each try, plus jitter) | 3, 0.5 s, 8 s | waits 0.5 s, then 1 s, then gives up |
| `capacity`, `refill_rate_per_s` | token bucket size and how fast it refills | e.g. 100,000 and 1,667/s | 1,667 × 60 ≈ 100,000 tokens per minute |
| `delta` | actual tokens minus reserved tokens at settlement | usually negative | reserved 4,000, used 3,400 → `delta = -600`, 600 refunded |
| `monthly_limit_usd`, `soft_alert_threshold` | a team's monthly budget and the share that triggers an alert | $5,000, 0.8 | alert at $4,000 spent |
| `similarity_threshold` | how similar two questions must be to share a semantic-cache answer | 0.97 (conservative) | 0.98 → reuse, 0.95 → call the model |
| TTL | how long a cache entry or credential is kept before refresh | seconds to hours | `refresh_interval_s=300` re-reads keys every 5 min |
| `fallback_depth` | which step of the backup list served the request (0 = primary) | 0 – 2 | 1 → the first backup answered |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. Why a model gateway — the N×M problem

> **In plain words.** Without a gateway, every app talks to every AI provider on its own, and each team rebuilds the same retries, keys and logging slightly differently. A gateway is one shared front door: apps talk only to it, and it talks to the providers.
>
> **Real-world example.** 6 apps and 4 providers can mean up to 6 × 4 = 24 separate integrations. With a gateway it is 6 + 4 = 10: each app connects once, each provider is connected once. The price: one more network hop, and one service that must never go down.

### 1.1 The integration explosion

Picture an organization eighteen months into its LLM adoption, with no central abstraction. The
recommendation team calls OpenAI directly from a Python service. The support-bot team calls Anthropic
directly from a Node service, because Claude tested better on their eval set. The internal-tools team
is on Azure OpenAI, because that's what procurement already had a contract for. A new fraud-detection
prototype wants Bedrock, because the data cannot leave the AWS account boundary for compliance reasons.
Four teams, four providers, four independent integrations — each one reimplementing, at different
quality levels, the same list of concerns:

- A retry loop with backoff for rate limits and transient 5xxs.
- An API key, stored *somewhere*, rotated on *someone's* schedule.
- A streaming handler for SSE, hand-rolled against that one provider's event format.
- Token counting, using that provider's tokenizer, for cost estimation and context-window management.
- Error handling for that provider's specific error taxonomy (is a 429 retryable? is this 400 a bad
  request or a content-policy rejection? every provider encodes this differently).
- Logging — usually inconsistent, usually missing the fields you'll want six months later when a cost
  spike needs explaining.

This is the **N × M problem**: N applications, each independently wired to some subset of M providers,
produces up to N × M bespoke integrations, each with its own retry logic, its own auth handling, its
own logging gaps, and its own subtly different understanding of what "the request failed" means. It is
the identical shape of problem microservices architectures solved with an API gateway and service mesh
in the 2010s, and message buses solved before that: point-to-point integration between every producer
and every consumer scales quadratically in integration *count*, and — worse — quadratically in
*inconsistency*, because nothing forces the four teams' retry policies, logging schemas, or auth models
to agree with each other.

A model gateway is the answer applied to this specific domain: a single service every application
talks to over one stable interface, which itself holds exactly one adapter per provider. The
integration count becomes **N + M** — N applications integrate once, with the gateway; M providers are
onboarded once, behind an adapter, by the platform team. Adding a fifth application does not require
touching any provider code. Adding a fifth provider does not require touching any application code.
This is the same argument `22`'s §13 makes for a policy engine sitting between every agent and every
tool call: centralize the interception point, and a change to policy (or, here, to provider surface)
becomes a one-file change instead of an N-file change.

### 1.2 What the gateway is a platform primitive *for*

Once every model call in the organization flows through one chokepoint, a list of capabilities becomes
possible that are difficult or impossible to retrofit onto N independent integrations after the fact:

- **Provider abstraction.** Applications code against one canonical request/response shape; the
  gateway translates to and from each provider's wire format. Switching a model, or a provider, behind
  an alias does not require an application deploy (§12).
- **Central authentication.** Provider API keys live in exactly one place, rotated on one schedule, by
  one team, with one audit log of who used which credential when (§8).
- **Rate limiting and quota.** Per-tenant, per-team, and per-model limits are enforced against a single
  source of truth instead of trusted to N independent, unenforceable client-side promises (§6).
- **Cost control.** Every dollar spent on inference flows through one measurement point, enabling
  real per-tenant attribution and hard budget enforcement that no individual application can bypass
  by simply not implementing its own tracking (§7).
- **Logging and observability.** One schema, one trace format, one dashboard, covering every model
  call in the organization — not four inconsistent, partially-instrumented ones (§9).
- **Model routing and fallback.** Requests can be routed by cost, latency, or capability, and can fail
  over to a secondary provider transparently — logic that is architecturally awkward to duplicate
  correctly in N client codebases, and is trivial to get right once, centrally (§4, §5).
- **Security and policy enforcement.** PII redaction, prompt-injection screening, content filtering,
  and RBAC over which team may call which model — enforced at the one place no application traffic can
  route around (§8, and `17-safety-guardrails-and-prompt-injection.md`, planned).
- **Version and model lifecycle management.** Deprecating a model version, rolling out a new one behind
  a canary, or pinning a specific snapshot for a regulated workload — all a configuration change at the
  gateway, not a coordinated multi-team migration (§12).

None of these is really an independent "feature." Every one of them is what you get for free, or
nearly free, once you have paid the actual architectural cost — building the one chokepoint — a single
time.

### 1.3 The cost side of the ledger

None of this is free. A gateway adds a network hop to every single model call in the organization, and
it becomes a single point of failure by construction — if it is down, every application that depends
on it for inference is down, even if every underlying provider is healthy. It is a new service that
needs its own on-call rotation, its own capacity planning, its own SLOs, tighter than any individual
provider's, because it is now *on the critical path in front of* every provider instead of behind just
one application. Section 14 is entirely about not letting that tradeoff bite you: a stateless,
horizontally scaled, well-observed gateway with aggressive timeouts and a fast failure path is a net
win; a gateway built as an afterthought, un-load-tested, single-instance, is a worse outcome than the
N×M mess it replaced. The rest of this chapter is the engineering required to land on the right side of
that line.

---

## 2. Gateway architecture and the request lifecycle

> **In plain words.** A request passes through a short line of checkpoints: who are you, which model should serve this, are you allowed, are you over your limit, then translate and send. Each checkpoint does one job.
>
> **Real-world example.** The support bot sends "model=default". The router turns that into a concrete model, the policy check confirms the support team may use it, the rate limiter sees they have used 40% of this minute's token budget, and the adapter converts the request into that provider's format.

### 2.1 The full pipeline

```
Application
   │  (typed SDK call: gateway.chat(messages, model="default", tenant="team-x"))
   ▼
AI SDK (client library)
   │  serializes to canonical wire format, attaches auth, may retry once locally
   ▼
Gateway API (ingress: HTTP/gRPC, auth middleware, request validation)
   │
   ▼
Router
   │  resolves alias → concrete model, applies routing policy (§4)
   ▼
Policy Engine
   │  RBAC check, content policy, PII screen, budget check (§7, §8)
   ▼
Rate Limiter
   │  per-tenant / per-model quota check, pre-flight token estimate (§6)
   ▼
Provider Adapter
   │  translates canonical request → provider wire format (§3)
   ▼
LLM Provider
   │  (OpenAI / Anthropic / Azure / Vertex / Bedrock / self-hosted)
   ▼
[response flows back up the same stack, normalized at the Adapter boundary,
 metered at the Rate Limiter / Cost Tracker, logged at every layer, and
 returned through the Gateway API to the SDK]
```

Each stage exists to answer exactly one question, and a well-designed gateway keeps them as separable
components — separable enough to unit-test independently and reorder or bypass for specific request
classes — rather than one monolithic handler function:

| Stage | Question it answers | Failure mode if missing |
|---|---|---|
| AI SDK | How does application code express intent without hand-building HTTP? | Every app hand-rolls HTTP, retries, and error handling (§1) |
| Gateway API | Is this request well-formed and who is making it? | No consistent auth or input validation boundary |
| Router | Which concrete model/provider actually serves this? | Model choice hardcoded in every application (§12) |
| Policy Engine | Is this tenant *allowed* to make this call? | No RBAC, no budget enforcement, no content policy (§7, §8) |
| Rate Limiter | Has this tenant exceeded their quota? | One noisy tenant starves every other tenant's provider quota |
| Provider Adapter | How do I speak this specific provider's wire protocol? | Router and policy code littered with per-provider branches (§3) |

### 2.2 Request and response normalization — the canonical message format

The single most consequential design decision in the entire gateway is the **canonical internal
message format** — the shape every component upstream of the Provider Adapter operates on, regardless
of which provider eventually serves the request. Get this wrong and every other component (router,
policy engine, cost tracker, logger) ends up provider-aware, which defeats the entire point of having
adapters.

```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    name: str | None = None  # for multi-agent / named-participant transcripts


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass
class ChatRequest:
    """The canonical internal request. Every provider adapter's job is to
    translate exactly this shape into its own wire format — nothing upstream
    of the adapter should ever branch on provider identity."""
    messages: list[Message]
    model: str                       # a gateway ALIAS, resolved by the router (§4)
    tenant_id: str
    max_tokens: int | None = None
    temperature: float = 1.0
    tools: list[ToolSpec] = field(default_factory=list)
    tool_choice: Literal["auto", "required", "none"] | str = "auto"
    response_format: dict[str, Any] | None = None   # JSON schema for structured output
    stream: bool = False
    stop_sequences: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)  # trace id, feature flag, etc.


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0     # provider-side prompt cache hits (§11)
    reasoning_tokens: int = 0        # o-series / extended-thinking tokens, reported separately, billed at the output rate


@dataclass
class ChatResponse:
    """The canonical internal response, normalized regardless of provider."""
    message: Message
    model: str                        # the CONCRETE provider model actually used
    provider: str                     # "openai" | "anthropic" | "azure" | "vertex" | "bedrock" | ...
    usage: Usage
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter", "error"]
    latency_ms: float
    request_id: str                   # gateway-generated, for tracing (§9)
    provider_request_id: str | None = None  # upstream request id, for provider-side support tickets
```

Two design choices here carry the rest of the chapter:

1. **`model` in the request is an alias, resolved later by the router** (§4, §12) — never a
   provider-specific model string chosen by the caller. This is what makes changing the underlying
   model a config change instead of an application redeploy.
2. **`Usage` is a first-class, normalized type** with fields for cached and reasoning tokens, because
   those categories are billed differently by every provider that has them, and cost calculation (§7)
   is impossible to get right if the canonical type only has "tokens."

### 2.3 Where normalization actually happens

Normalization is the Provider Adapter's entire job, in both directions: canonical `ChatRequest` →
provider wire format on the way in, provider wire response → canonical `ChatResponse` on the way out.
Everything between the Gateway API and the Adapter — router, policy engine, rate limiter — operates
exclusively on canonical types and must never import a provider SDK. This is the architectural
discipline that keeps N+1 provider onboarding from touching router or policy code; violate it once
(one `if provider == "anthropic"` branch leaking into the router "just this once") and the abstraction
starts eroding immediately, the same way a single `isinstance` check on a concrete `Runnable` subclass
would defeat LangChain's protocol in `20`.

---

## 3. The provider abstraction layer

> **In plain words.** Each provider speaks a slightly different dialect. An adapter is a translator for one provider, so the rest of the gateway only ever sees one common format.
>
> **Real-world example.** OpenAI puts the system prompt inside the message list; Anthropic wants it as a separate field. The Anthropic adapter moves it. The app never knows, and adding a fifth provider means writing one new adapter file, not changing 6 apps.

### 3.1 The common interface

Every major provider exposes broadly the same three capabilities — chat completion, embeddings, and
(increasingly) native structured output — but with meaningfully different wire formats, different
streaming event shapes, different tool-calling conventions, and different tokenizers. The adapter's job
is to hide every one of those differences behind one `Protocol`.

```python
from __future__ import annotations
from typing import AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class ProviderAdapter(Protocol):
    """Every provider integration implements exactly this surface. Nothing
    outside this file, and the concrete adapter modules, should ever import
    a provider SDK directly."""

    name: str  # "openai", "anthropic", "azure_openai", "vertex", "bedrock", "vllm"

    async def chat(self, request: ChatRequest) -> ChatResponse: ...

    async def chat_stream(self, request: ChatRequest) -> AsyncIterator["StreamEvent"]: ...

    async def embed(self, texts: list[str], model: str) -> list[list[float]]: ...

    def count_tokens(self, messages: list[Message], model: str) -> int:
        """Provider-native token counting, used for pre-flight rate limiting (§6)."""
        ...

    async def health_check(self) -> "HealthStatus": ...
```

`Protocol` rather than an `ABC` is a deliberate choice: adapters are frequently thin wrappers around a
provider's own SDK client, and `Protocol`'s structural typing means an adapter class does not need to
inherit from a gateway base class at all — it only needs to have the right methods. This matters in
practice because provider SDKs update independently of the gateway codebase, and a structural interface
survives that churn better than a nominal one requiring every adapter to subclass a shared base whose
constructor might not have anticipated a new provider's client init signature. (An `ABC` is the more
conventional choice if you want to *enforce* the interface at class-definition time rather than at
call time via `runtime_checkable`; either is defensible — the point is that some interface is enforced
uniformly, not which mechanism enforces it.)

### 3.2 A concrete adapter — OpenAI

```python
import time
import uuid
from openai import AsyncOpenAI, APIStatusError, APITimeoutError


class OpenAIAdapter:
    name = "openai"

    def __init__(self, api_key: str, base_url: str | None = None, timeout: float = 30.0):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    async def chat(self, request: ChatRequest) -> ChatResponse:
        start = time.monotonic()
        payload = self._to_wire(request)
        try:
            resp = await self._client.chat.completions.create(**payload)
        except APITimeoutError as e:
            raise ProviderTimeoutError(provider=self.name) from e
        except APIStatusError as e:
            raise self._map_error(e) from e
        return self._from_wire(resp, request, latency_ms=(time.monotonic() - start) * 1000)

    def _to_wire(self, request: ChatRequest) -> dict:
        messages = []
        for m in request.messages:
            if m.tool_results:
                for tr in m.tool_results:
                    messages.append({
                        "role": "tool", "tool_call_id": tr.tool_call_id, "content": tr.content,
                    })
            else:
                entry: dict = {"role": m.role.value, "content": m.content}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                        for tc in m.tool_calls
                    ]
                messages.append(entry)

        payload: dict = {
            "model": request.model,          # already resolved to a concrete OpenAI model by the router
            "messages": messages,
            "max_tokens": request.max_tokens,   # o-series reasoning models take max_completion_tokens instead
            "temperature": request.temperature,
            "stream": request.stream,
        }
        if request.tools:
            payload["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in request.tools
            ]
            # OpenAI-specific tool_choice quirk: "required" forces a call, "auto" and "none" pass through
            payload["tool_choice"] = request.tool_choice
        if request.response_format:
            payload["response_format"] = {"type": "json_schema", "json_schema": request.response_format}
        return payload

    def _from_wire(self, resp, request: ChatRequest, latency_ms: float) -> ChatResponse:
        choice = resp.choices[0]
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments))
            for tc in (choice.message.tool_calls or [])
        ]
        return ChatResponse(
            message=Message(role=Role.ASSISTANT, content=choice.message.content, tool_calls=tool_calls),
            model=resp.model,
            provider=self.name,
            usage=Usage(
                input_tokens=resp.usage.prompt_tokens,
                output_tokens=resp.usage.completion_tokens,
                # prompt_tokens_details is an object (or None), not a dict
                cached_input_tokens=getattr(getattr(resp.usage, "prompt_tokens_details", None),
                                            "cached_tokens", 0) or 0,
            ),
            finish_reason=self._map_finish_reason(choice.finish_reason),
            latency_ms=latency_ms,
            request_id=str(uuid.uuid4()),
            provider_request_id=resp.id,
        )

    def _map_finish_reason(self, reason: str) -> str:
        return {"stop": "stop", "length": "length", "tool_calls": "tool_calls",
                "content_filter": "content_filter"}.get(reason, "stop")

    def _map_error(self, e: APIStatusError) -> Exception:
        if e.status_code == 429:
            return RateLimitedError(provider=self.name, retry_after=e.response.headers.get("retry-after"))
        if e.status_code >= 500:
            return ProviderUnavailableError(provider=self.name)
        return ProviderBadRequestError(provider=self.name, detail=str(e))

    def count_tokens(self, messages: list[Message], model: str) -> int:
        import tiktoken
        enc = tiktoken.encoding_for_model(model)
        # per-message overhead per OpenAI's own counting guidance; not exact for every model
        # revision, which is exactly why §6 treats this as an ESTIMATE, not ground truth.
        total = 0
        for m in messages:
            total += 4 + len(enc.encode(m.content or ""))
        return total + 2
```

### 3.3 A concrete adapter — Anthropic

The Anthropic adapter is instructive precisely because it diverges from OpenAI in ways that would leak
into every caller if the gateway didn't absorb them: **system prompts are a top-level parameter, not a
message with `role: system`**; tool results are user-role content blocks with a `tool_result` type,
not a separate `role: tool`; and usage accounting reports cache reads and cache writes as distinct
fields rather than OpenAI's single "cached tokens" figure.

```python
from anthropic import AsyncAnthropic


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, api_key: str, timeout: float = 30.0):
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout)

    async def chat(self, request: ChatRequest) -> ChatResponse:
        start = time.monotonic()
        system, messages = self._to_wire(request)
        try:
            resp = await self._client.messages.create(
                model=request.model,
                system=system,
                messages=messages,
                max_tokens=request.max_tokens or 4096,   # Anthropic requires max_tokens; OpenAI does not
                temperature=request.temperature,
                tools=self._tools_wire(request.tools) if request.tools else NOT_GIVEN,
            )
        except anthropic.APIStatusError as e:
            raise self._map_error(e) from e
        return self._from_wire(resp, latency_ms=(time.monotonic() - start) * 1000)

    def _to_wire(self, request: ChatRequest) -> tuple[str, list[dict]]:
        # Anthropic hoists system content OUT of the messages array entirely —
        # the single largest structural divergence from OpenAI's message shape.
        system_parts = [m.content for m in request.messages if m.role == Role.SYSTEM and m.content]
        system = "\n\n".join(system_parts)

        messages = []
        for m in request.messages:
            if m.role == Role.SYSTEM:
                continue
            if m.tool_results:
                content = [
                    {"type": "tool_result", "tool_use_id": tr.tool_call_id,
                     "content": tr.content, "is_error": tr.is_error}
                    for tr in m.tool_results
                ]
                messages.append({"role": "user", "content": content})
            elif m.tool_calls:
                blocks = [{"type": "text", "text": m.content}] if m.content else []
                blocks += [{"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                           for tc in m.tool_calls]
                messages.append({"role": "assistant", "content": blocks})
            else:
                messages.append({"role": m.role.value, "content": m.content})
        return system, messages

    def _from_wire(self, resp, latency_ms: float) -> ChatResponse:
        text_blocks = [b.text for b in resp.content if b.type == "text"]
        tool_calls = [ToolCall(id=b.id, name=b.name, arguments=b.input)
                      for b in resp.content if b.type == "tool_use"]
        return ChatResponse(
            message=Message(role=Role.ASSISTANT, content="".join(text_blocks) or None, tool_calls=tool_calls),
            model=resp.model,
            provider=self.name,
            usage=Usage(
                # Anthropic's input_tokens EXCLUDES cache reads and cache writes; OpenAI's
                # prompt_tokens INCLUDES cached tokens. The canonical input_tokens is the
                # total (OpenAI-style), so add them back here — otherwise §7.1's
                # `input_tokens - cached_input_tokens` subtracts the cached part twice.
                # (Cache writes are billed at a premium over base input; not modeled here.)
                input_tokens=(resp.usage.input_tokens
                              + (getattr(resp.usage, "cache_read_input_tokens", 0) or 0)
                              + (getattr(resp.usage, "cache_creation_input_tokens", 0) or 0)),
                output_tokens=resp.usage.output_tokens,
                cached_input_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            ),
            finish_reason={"end_turn": "stop", "max_tokens": "length",
                           "tool_use": "tool_calls"}.get(resp.stop_reason, "stop"),
            latency_ms=latency_ms,
            request_id=str(uuid.uuid4()),
            provider_request_id=resp.id,
        )
```

### 3.4 Azure OpenAI, Vertex AI, Bedrock, and self-hosted — what actually differs

| Provider | What the adapter has to absorb |
|---|---|
| **Azure OpenAI** | Same wire shape as OpenAI, but the endpoint is per-deployment (a customer-chosen deployment name, not a model name), auth is an Azure AD token or API key against a resource-scoped URL, and model *availability* is region- and quota-allocated rather than globally uniform — the adapter's `model` resolution has to map alias → (deployment name, region), not alias → model string. |
| **Google Vertex AI** | Auth is a GCP service-account credential exchanged for a short-lived OAuth token, not a static API key — the adapter needs a token-refresh background task. Gemini's content-blocks format is closer to Anthropic's than OpenAI's (parts, not messages-with-string-content), and safety-filter rejections come back as a distinct `finish_reason` that must be mapped to the canonical `content_filter` value. |
| **AWS Bedrock** | Not one API — Bedrock is a routing layer over heterogeneous model families (Anthropic, Meta, Cohere, Amazon Titan/Nova), each with a *different* request body shape even though they share one HTTP surface (`InvokeModel` / `Converse`). Prefer the newer `Converse` API specifically because it normalizes tool-calling and message format across model families — using per-model `InvokeModel` bodies means the gateway's Bedrock adapter re-derives its own internal mini-abstraction-layer, one abstraction too many. Auth is SigV4 request signing, not a bearer token. |
| **Self-hosted (vLLM / TGI)** | Usually the easiest adapter to write, because both serve an OpenAI-compatible `/v1/chat/completions` endpoint — the OpenAI adapter often works unmodified with a different `base_url`. The real work is elsewhere: health checks need to distinguish "pod is up" from "model weights are loaded and warm" (a basic liveness check can pass before a fresh replica serves at production latency, so readiness should be gated on a real test generation), and there is no vendor SLA — the gateway's circuit breaker (§5) is the *only* thing standing between a degraded self-hosted replica and every caller. |

### 3.5 Handling provider-specific feature divergence

Three categories of divergence recur across every adapter and deserve a named strategy each, rather
than ad hoc handling per provider:

**Tool calling.** Argument encoding differs (OpenAI's function arguments arrive as a JSON *string* that
must be parsed; Anthropic's `tool_use.input` arrives as an already-parsed object). Parallel tool calls
are supported by some providers and not others, and "force exactly one specific tool" is expressed
differently (OpenAI: `tool_choice={"type": "function", "function": {"name": ...}}`; Anthropic:
`tool_choice={"type": "tool", "name": ...}`). The canonical `ToolCall` type in §2.2 always holds an
already-parsed `dict`, which forces every adapter to do the string-to-object normalization at the
boundary rather than pushing that inconsistency upstream.

**Streaming.** Event granularity differs sharply: OpenAI streams token deltas as a flat sequence of
chunks; Anthropic's stream is a structured sequence of `message_start` / `content_block_start` /
`content_block_delta` / `content_block_stop` / `message_stop` events, because a single response can
interleave multiple content block types (text, then a tool call, then more text). The canonical
`StreamEvent` type (§10) has to be expressive enough to represent both without losing information —
typically a small closed set: `TextDelta`, `ToolCallDelta`, `UsageUpdate`, `Done`.

**Token counting.** Every provider's tokenizer is different (OpenAI's `tiktoken`, Anthropic's own
tokenizer accessible only via a `count_tokens` API call, Google's SentencePiece-based counting), and
none of them is guaranteed stable across model versions. This is why §6 treats pre-flight token counts
as an *estimate* used for rate-limiting headroom, never as the billed truth — the billed truth is
always the `usage` block the provider returns after the call completes.

---

## 4. Model routing

> **In plain words.** Routing decides which real model answers a request. It can be a fixed table, or it can pick based on price, speed, or how hard the question looks.
>
> **Real-world example.** An HR bot gets 10,000 questions a day. Short ones like "How many vacation days do I have?" go to a cheap, fast model; long policy comparisons go to a stronger, pricier one. A canary sends 5% of users to a new model to compare quality before switching everyone.

### 4.1 Static routing

The simplest and most common case: a routing table, checked at request time, mapping an alias and
optional selector to a concrete `(provider, model)` pair.

```python
from dataclasses import dataclass


@dataclass
class RouteTarget:
    provider: str
    model: str
    weight: float = 1.0          # for weighted / canary splits


@dataclass
class RoutingRule:
    alias: str                                    # what the application asked for
    targets: list[RouteTarget]                    # one or more, for weighted routing
    tenant_override: dict[str, list[RouteTarget]] | None = None  # per-tenant pin


class StaticRouter:
    def __init__(self, rules: dict[str, RoutingRule]):
        self._rules = rules

    def resolve(self, alias: str, tenant_id: str) -> RouteTarget:
        rule = self._rules.get(alias)
        if rule is None:
            raise UnknownModelAliasError(alias)

        targets = (rule.tenant_override or {}).get(tenant_id, rule.targets)
        if len(targets) == 1:
            return targets[0]
        return self._weighted_choice(targets)

    def _weighted_choice(self, targets: list[RouteTarget]) -> RouteTarget:
        import random
        total = sum(t.weight for t in targets)
        r = random.uniform(0, total)
        upto = 0.0
        for t in targets:
            upto += t.weight
            if upto >= r:
                return t
        return targets[-1]
```

Config, not code, drives this (the model names here and in §12.1 are dated examples; several of these
snapshots have since been superseded or retired, so use whatever your providers currently offer):

```yaml
routes:
  default:
    targets:
      - {provider: openai, model: gpt-4o, weight: 1.0}
  fast:
    targets:
      - {provider: anthropic, model: claude-3-5-haiku-20241022, weight: 1.0}
  reasoning:
    targets:
      - {provider: openai, model: o3, weight: 1.0}
    tenant_override:
      team-finance:
        - {provider: azure_openai, model: gpt-4o-finance-deploy, weight: 1.0}  # data-residency requirement
```

Static routing is the right default for most traffic: it is predictable, cheap to reason about, and
trivially auditable ("which model did tenant X's traffic actually hit last Tuesday" is one config diff
away). Reach for the dynamic strategies below only when static routing's predictability is costing you
something measurable — money, latency, or quality — that a rule can't capture.

### 4.2 Dynamic routing: cost-based and latency-based

Dynamic routing consults live signal — a rolling window of observed cost, latency, and error rate per
`(provider, model)` — instead of a static weight.

```python
from collections import deque
import time


class ProviderHealthTracker:
    """Rolling window of recent latency/error/cost samples per route target,
    feeding both dynamic routing decisions and circuit-breaker state (§5)."""

    def __init__(self, window_seconds: float = 60.0):
        self._window = window_seconds
        self._samples: dict[str, deque[tuple[float, float, bool]]] = {}  # key -> (ts, latency_ms, error)

    def record(self, key: str, latency_ms: float, error: bool) -> None:
        now = time.monotonic()
        buf = self._samples.setdefault(key, deque())
        buf.append((now, latency_ms, error))
        while buf and now - buf[0][0] > self._window:
            buf.popleft()

    def p50_latency(self, key: str) -> float | None:
        buf = self._samples.get(key)
        if not buf:
            return None
        sorted_lat = sorted(l for _, l, _ in buf)
        return sorted_lat[len(sorted_lat) // 2]

    def error_rate(self, key: str) -> float:
        buf = self._samples.get(key)
        if not buf:
            return 0.0
        errors = sum(1 for _, _, e in buf if e)
        return errors / len(buf)


class CostLatencyRouter:
    """Picks the cheapest healthy target that meets a latency SLO, falling
    back to the fastest healthy target if none meets it."""

    def __init__(self, targets: list[RouteTarget], costs: dict[str, float],  # $/1K tokens, per target key
                 health: ProviderHealthTracker, latency_slo_ms: float,
                 error_rate_ceiling: float = 0.10):
        self._targets = targets
        self._costs = costs
        self._health = health
        self._slo = latency_slo_ms
        self._ceiling = error_rate_ceiling

    def resolve(self) -> RouteTarget:
        healthy = [t for t in self._targets
                   if self._health.error_rate(self._key(t)) < self._ceiling]
        if not healthy:
            raise AllProvidersDegradedError()

        within_slo = [t for t in healthy
                      if (p50 := self._health.p50_latency(self._key(t))) is None
                      or p50 <= self._slo]
        pool = within_slo or healthy
        return min(pool, key=lambda t: self._costs.get(self._key(t), float("inf")))

    def _key(self, t: RouteTarget) -> str:
        return f"{t.provider}:{t.model}"
```

### 4.3 Content-based routing

Route by *what the request is asking for*, not just by tenant or config — cheap, fast models for
simple queries, capable and expensive models reserved for requests that actually need them. The
routing signal can be as crude as input length, or as involved as a small, fast classifier (even
another, cheaper LLM call) scoring query complexity.

```python
class ComplexityRouter:
    def __init__(self, cheap: RouteTarget, capable: RouteTarget,
                 complexity_classifier: "ComplexityClassifier"):
        self._cheap = cheap
        self._capable = capable
        self._classifier = complexity_classifier

    async def resolve(self, request: ChatRequest) -> RouteTarget:
        # Cheap heuristics first — avoid paying for a classifier call on the
        # common case. Escalate to the model-based classifier only when the
        # heuristics are ambiguous.
        last_user_msg = next((m.content for m in reversed(request.messages)
                               if m.role == Role.USER and m.content), "")
        if len(last_user_msg) < 200 and not request.tools:
            return self._cheap
        if len(last_user_msg) > 2000 or len(request.tools) > 3:
            return self._capable

        score = await self._classifier.score(last_user_msg)   # 0.0 (trivial) .. 1.0 (hard)
        return self._capable if score > 0.5 else self._cheap
```

The engineering judgment call here is the classifier's own cost and latency: a model-based complexity
classifier that costs 20% of what the capable model would have cost, run on every request, erodes the
routing's entire economic argument. In practice, teams that do this well use a cheap, self-hosted, or
distilled classifier — sub-50ms, sub-cent — never the same tier of model they're trying to route away
from.

### 4.4 A/B testing and canary routing

Structurally identical to §4.1's weighted static routing, but the weights are a deliberately temporary
configuration tied to an experiment, not a steady-state traffic split — and, critically, the routing
decision and the outcome measurement have to share a join key.

```python
class CanaryRouter:
    def __init__(self, stable: RouteTarget, canary: RouteTarget, canary_pct: float):
        self._stable = stable
        self._canary = canary
        self._canary_pct = canary_pct

    def resolve(self, request: ChatRequest) -> tuple[RouteTarget, str]:
        # Deterministic bucketing by a stable identifier (tenant or session id),
        # not per-request randomness — otherwise the same user/session flips
        # between canary and stable mid-conversation, which corrupts both the
        # user experience and any A/B analysis that assumes assignment stability.
        bucket_key = request.metadata.get("session_id") or request.tenant_id
        h = int(hashlib.sha256(bucket_key.encode()).hexdigest(), 16)
        is_canary = (h % 10_000) / 10_000 < self._canary_pct
        variant = "canary" if is_canary else "stable"
        return (self._canary if is_canary else self._stable), variant
```

The `variant` label returned alongside the target is not optional — it has to be attached to every
downstream log line, cost record, and eval sample for that request, or the canary's entire purpose
(measuring whether it performs better) is unfulfillable. This is the same join-key discipline `22`'s
§14 trajectory-eval labs depend on: a measurement is only as good as the label that lets you group by
it later.

### 4.5 Routing table design principles

A production routing table earns its complexity only when each layer answers a question the layer
below it cannot: tenant overrides exist for compliance and contractual reasons (data residency, a
customer paying for a specific model SLA); capability-based routing exists because not every model
supports every feature (tool calling, vision, extended context) and a request that needs one must not
land on a target that lacks it; cost/latency dynamic routing exists to exploit multi-provider price and
performance variance that changes weekly. Collapsing all of this into one router function that
branches on everything at once is how routing tables become unmaintainable — keep tenant overrides,
capability filtering, and dynamic health-based selection as composable, independently testable stages
applied in a fixed order, not one function with accumulating `if` statements.

---

## 5. Fallback and resilience

> **In plain words.** Providers fail. The gateway keeps a backup list, stops calling a provider that keeps failing (a circuit breaker), and gives up quickly instead of waiting forever. Retrying is only safe for some errors.
>
> **Real-world example.** The primary provider starts timing out at 14:00. After 5 failures in a row the breaker opens and requests go straight to the backup, so users see answers again within seconds instead of 60-second hangs. A malformed request (HTTP 400) is not retried on the backup, because it would fail there too.

### 5.1 Provider chains

Every routing decision in §4 should resolve not to one target but to an ordered chain — primary,
secondary, tertiary — because "the model I wanted is down" must never mean "the request fails," except
for callers that have explicitly opted out of fallback (some regulated or evaluation workloads need
byte-for-byte reproducibility from one named model, and silently falling back would violate that).

```python
@dataclass
class ProviderChain:
    targets: list[RouteTarget]   # ordered: primary, secondary, tertiary...
    allow_fallback: bool = True


class ChainExecutor:
    def __init__(self, adapters: dict[str, ProviderAdapter],
                 breakers: dict[str, "CircuitBreaker"]):
        self._adapters = adapters
        self._breakers = breakers

    async def execute(self, chain: ProviderChain, request: ChatRequest) -> ChatResponse:
        last_error: Exception | None = None
        targets = chain.targets if chain.allow_fallback else chain.targets[:1]

        for i, target in enumerate(targets):
            key = f"{target.provider}:{target.model}"
            breaker = self._breakers[key]
            if breaker.is_open():
                last_error = ProviderUnavailableError(provider=target.provider, reason="circuit_open")
                continue

            adapter = self._adapters[target.provider]
            req = replace(request, model=target.model)
            try:
                response = await breaker.call(adapter.chat, req)
                response.message.metadata = {"fallback_depth": i}  # observability: which chain link served this
                return response
            except (RateLimitedError, ProviderUnavailableError, ProviderTimeoutError) as e:
                last_error = e
                continue
            except ProviderBadRequestError:
                raise   # a 400 will fail identically on every other provider — don't waste the fallback budget

        raise AllProvidersFailedError(chain=chain, last_error=last_error)
```

The `except ProviderBadRequestError: raise` line is the detail most naive fallback implementations get
wrong: falling back only makes sense for failure classes where a *different provider* might plausibly
succeed. A malformed request, a content-policy rejection on genuinely disallowed content, or a
context-window overflow will fail identically on the secondary provider — burning the fallback chain on
those wastes latency and obscures the real error behind a generic "all providers failed."

### 5.2 The circuit breaker

```python
import time
from enum import Enum


class BreakerState(Enum):
    CLOSED = "closed"       # normal operation
    OPEN = "open"           # failing fast, not calling the provider
    HALF_OPEN = "half_open"  # trial request to check recovery


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout_s: float = 30.0,
                 half_open_max_calls: int = 1):
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_s
        self._half_open_max = half_open_max_calls
        self._state = BreakerState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._half_open_calls = 0

    def is_open(self) -> bool:
        if self._state == BreakerState.OPEN:
            if time.monotonic() - self._opened_at >= self._recovery_timeout:
                self._state = BreakerState.HALF_OPEN
                self._half_open_calls = 0
                return False
            return True
        return False

    async def call(self, fn, *args, **kwargs):
        if self._state == BreakerState.HALF_OPEN and self._half_open_calls >= self._half_open_max:
            raise ProviderUnavailableError(reason="half_open_capacity_exhausted")

        if self._state == BreakerState.HALF_OPEN:
            self._half_open_calls += 1

        try:
            result = await fn(*args, **kwargs)
        except Exception:
            self._on_failure()
            raise
        else:
            self._on_success()
            return result

    def _on_failure(self) -> None:
        self._failure_count += 1
        if self._state == BreakerState.HALF_OPEN:
            self._state = BreakerState.OPEN
            self._opened_at = time.monotonic()
        elif self._failure_count >= self._failure_threshold:
            self._state = BreakerState.OPEN
            self._opened_at = time.monotonic()

    def _on_success(self) -> None:
        self._failure_count = 0
        if self._state == BreakerState.HALF_OPEN:
            self._state = BreakerState.CLOSED
```

One breaker instance per `(provider, model)` pair, not one per provider — a provider having an outage
on one model does not imply every model behind that provider is unhealthy (Azure region-level capacity
issues in particular are frequently model-deployment-specific).

### 5.3 Degraded versus down — why the distinction matters

A binary healthy/unhealthy health check throws away the signal that actually drives good fallback
decisions. A provider can be:

- **Down** — connection refused, 5xx on every call, or every call timing out. Trip the breaker
  immediately; fall back on the next request without hesitation.
- **Degraded** — elevated latency (p50 tripled) but still succeeding, or an elevated-but-nonzero error
  rate (occasional 429s under load, not sustained). This is the harder case: tripping the breaker
  immediately abandons a provider that might still be the best available option if every fallback is
  *also* degraded; never tripping it means every caller absorbs the degradation.

The pragmatic middle ground most production gateways converge on: a **latency-aware breaker** that
trips not only on hard failures but also on a rolling p95 exceeding N× the provider's normal baseline
for a sustained window (using the same `ProviderHealthTracker` from §4.2), combined with routing (§4.2)
continuing to prefer the degraded provider over an even-worse fallback rather than a hard cutover. In
other words: circuit breaking answers "should I stop calling this at all," and dynamic routing answers
"given several imperfect options, which do I prefer right now" — treat them as two different decisions
informed by the same health signal, not one decision.

### 5.4 Timeout management

Every layer needs its own timeout, tuned to that layer's actual job, not one blanket timeout applied
uniformly:

```python
@dataclass
class TimeoutPolicy:
    connect_timeout_s: float = 5.0
    first_token_timeout_s: float = 10.0   # TTFT budget — catches a provider that accepted
                                           # the request but is silently stuck
    total_timeout_s: float = 60.0         # hard ceiling for non-streaming / total stream duration
    per_fallback_hop_timeout_s: float = 15.0  # budget per chain link, so 3 hops don't sum to 3x total
```

The `per_fallback_hop_timeout_s` detail matters because naive fallback implementations apply the same
generous `total_timeout_s` to every hop in the chain, so a caller waiting on a 3-provider fallback
chain where each hop times out slowly can wait 3× the advertised total latency SLO before finally
getting an `AllProvidersFailedError`. Budget the *whole chain* to the caller's SLO, and divide it across
hops, rather than budgeting each hop independently.

### 5.5 Retry with exponential backoff

```python
import asyncio
import random


async def retry_with_backoff(fn, *args, max_attempts: int = 3,
                              base_delay_s: float = 0.5, max_delay_s: float = 8.0,
                              retryable: tuple[type[Exception], ...] = (RateLimitedError, ProviderTimeoutError),
                              **kwargs):
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except retryable as e:
            last_exc = e
            if attempt == max_attempts - 1:
                break
            retry_after = getattr(e, "retry_after", None)
            delay = float(retry_after) if retry_after else min(base_delay_s * (2 ** attempt), max_delay_s)
            delay += random.uniform(0, delay * 0.1)   # jitter, to avoid thundering-herd retries in lockstep
            await asyncio.sleep(delay)
    raise last_exc
```

Respecting a provider's `Retry-After` header when present, and adding a small jitter on top so that
many clients don't retry in lockstep, is the detail that separates a backoff implementation that plays well with a provider's own
load-shedding from one that fights it.

### 5.6 The idempotency problem

Retrying an LLM call is not like retrying a database write. A database retry either succeeds
identically or is safely rejected by a uniqueness constraint; an LLM retry, at any `temperature > 0`,
returns a **different, non-deterministic response** even for the exact same input, and even at
`temperature = 0` most providers do not guarantee bit-identical output across calls (sampling
implementation details, hardware non-determinism in batched inference, and periodic silent model-version
updates behind a stable model string all break the guarantee in practice). This creates two distinct
problems a gateway has to solve deliberately, not accidentally:

1. **Double-billing on ambiguous failures.** If a request times out at the network layer but the
   provider actually completed and billed it, a naive retry pays twice for one logical request. There
   is no universal fix — few providers expose a client-supplied idempotency key for chat completions
   the way payment APIs do — so the pragmatic mitigation is a **short-TTL request-hash dedup cache** at
   the gateway (§11.2's exact-match cache serves double duty here): if an identical request (same
   messages, same model, same tenant) is retried within a short window, serve the cached response
   instead of re-issuing the call, accepting the small risk of serving a stale response to a
   legitimately-identical-but-independent request.
2. **Tool-call side effects on retry.** If the assistant's turn included a tool call with a real side
   effect (charged a customer, sent an email) and the retry is of the *next* turn after a transient
   failure, replaying the tool call is a correctness bug independent of the gateway — this is `22`'s
   §8 checkpoint-and-resume problem, and the gateway's job is narrower: never silently retry a request
   whose `messages` array already contains a `tool_result`, without the caller's explicit
   acknowledgment that doing so is safe for that specific tool.

The interview-relevant framing: idempotency at a model gateway is not "make retries safe," full stop —
it's "know precisely which class of retry is safe (a clean network-level failure before any tokens were
generated) versus which class requires the caller's tool-execution semantics to weigh in," and design
the retry boundary accordingly rather than blanket-retrying everything that looks like an error.

---

## 6. Rate limiting and quota management

> **In plain words.** Rate limits stop one team from using up everyone's shared capacity. For LLMs you limit tokens, not just requests, because one request can be 50 tokens or 50,000.
>
> **Real-world example.** Team A has 100,000 tokens per minute. A request with a 3,000-token prompt and `max_tokens=1,000` reserves 4,000 tokens up front. The answer uses only 400, so 600 are returned to the budget when the call finishes.

### 6.1 The dimensions that need independent limits

A gateway serving multiple tenants needs limits that compose across at least three axes
simultaneously — per-tenant, per-application, and per-model — because a limit on only one axis lets a
single noisy caller starve every other caller sharing the same underlying provider quota, which is
exactly the failure mode a shared gateway exists to prevent.

```python
@dataclass
class QuotaKey:
    tenant_id: str
    application_id: str | None = None
    model: str | None = None

    def scopes(self) -> list[str]:
        """All limit scopes this request needs to check, from narrowest to widest."""
        scopes = [f"tenant:{self.tenant_id}"]
        if self.application_id:
            scopes.append(f"tenant:{self.tenant_id}:app:{self.application_id}")
        if self.model:
            scopes.append(f"tenant:{self.tenant_id}:model:{self.model}")
        return scopes
```

### 6.2 Token-based versus request-based limiting

Request-count limiting ("100 requests/minute") is trivial to enforce but nearly meaningless for LLM
cost and capacity — one request can be five tokens or fifty thousand. Production gateways limit on
**tokens per unit time**, matching the actual constrained resource (provider TPM/RPM quotas, and your
own budget), and typically enforce request-count limits only as a secondary guard against abuse
patterns (thundering-herd retries, a misconfigured client in a tight loop) that token limiting alone
doesn't catch cheaply.

### 6.3 Sliding window versus token bucket

```python
import time
from collections import deque


class SlidingWindowCounter:
    """Precise, but O(events in window) memory — fine at gateway scale for a
    per-scope counter, since the events are compact (timestamp, token count)."""

    def __init__(self, window_s: float, limit: int):
        self._window_s = window_s
        self._limit = limit
        self._events: deque[tuple[float, int]] = deque()

    def try_consume(self, amount: int) -> bool:
        now = time.monotonic()
        while self._events and now - self._events[0][0] > self._window_s:
            self._events.popleft()
        used = sum(a for _, a in self._events)
        if used + amount > self._limit:
            return False
        self._events.append((now, amount))
        return True

    def remaining(self) -> int:
        now = time.monotonic()
        while self._events and now - self._events[0][0] > self._window_s:
            self._events.popleft()
        return max(0, self._limit - sum(a for _, a in self._events))


class TokenBucket:
    """O(1) memory, allows controlled bursting up to the bucket capacity —
    generally the better fit for a distributed gateway backed by Redis,
    since the whole state is two numbers (tokens, last_refill), trivial to
    store and atomically update with a Lua script across gateway replicas."""

    def __init__(self, capacity: float, refill_rate_per_s: float):
        self._capacity = capacity
        self._refill_rate = refill_rate_per_s
        self._tokens = capacity
        self._last_refill = time.monotonic()

    def try_consume(self, amount: float) -> bool:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now
        if self._tokens < amount:
            return False
        self._tokens -= amount
        return True
```

Sliding window gives an exact answer at the cost of storing every event; a token bucket approximates
with O(1) state and, unlike a fixed window, doesn't have the classic edge-of-window burst artifact (two
full-limit bursts landing back-to-back across a window boundary). At gateway scale, across many
replicas, the token bucket's small, atomically-updatable state is usually the deciding factor — it maps
directly onto a Redis `INCRBYFLOAT` + TTL pattern or a single Lua script, whereas a sliding window's
per-event log needs either a sorted set per scope or a downsample, both meaningfully more expensive
per request at high QPS.

### 6.4 The pre-flight estimation problem

The genuinely LLM-specific wrinkle: **you don't know the true token cost of a request until the
response finishes**, but you must decide whether to *admit* the request before it starts. Waiting until
after the call to enforce the limit means an over-quota tenant's request still consumed real provider
capacity and cost real money before being rejected — rate limiting after the fact is not rate limiting,
it's post-hoc accounting.

The standard solution is a two-phase reservation:

```python
class TokenQuotaEnforcer:
    def __init__(self, buckets: dict[str, TokenBucket], adapters: dict[str, ProviderAdapter]):
        self._buckets = buckets
        self._adapters = adapters

    async def admit(self, request: ChatRequest, quota_key: QuotaKey) -> "Reservation":
        adapter = self._adapters[self._provider_for(request.model)]
        estimated_input = adapter.count_tokens(request.messages, request.model)
        # output is unknowable in advance — reserve against a conservative
        # ceiling (max_tokens if the caller set one, else a per-model default
        # p95 completion length learned from historical usage) rather than 0.
        estimated_output_ceiling = request.max_tokens or self._default_output_estimate(request.model)
        estimated_total = estimated_input + estimated_output_ceiling

        for scope in quota_key.scopes():
            bucket = self._buckets[scope]
            if not bucket.try_consume(estimated_total):
                raise QuotaExceededError(scope=scope, requested=estimated_total)

        return Reservation(quota_key=quota_key, reserved_amount=estimated_total)

    async def settle(self, reservation: "Reservation", actual_usage: Usage) -> None:
        actual_total = actual_usage.input_tokens + actual_usage.output_tokens
        delta = actual_total - reservation.reserved_amount
        # Refund the gap if we over-reserved (the common case — actual
        # completions are usually shorter than the ceiling); if we somehow
        # under-reserved, debit the difference so the NEXT request in this
        # window sees an accurate remaining balance rather than a rolling
        # under-count that compounds across requests.
        for scope in reservation.quota_key.scopes():
            bucket = self._buckets[scope]
            bucket._tokens = max(0.0, min(bucket._capacity, bucket._tokens - delta))
```

This reserve-then-settle pattern is exactly the same shape as a database connection pool checking out
before use and returning the true cost on release, or a cloud provider's "reserve capacity, bill
actual" instance model — the general pattern is well understood; the LLM-specific part is that the
reservation ceiling has to come from somewhere sensible (`max_tokens` if set, else a learned p95 by
model) or every tenant's effective quota becomes far more conservative than their real usage, because
every request reserves against a worst case that almost never happens.

### 6.5 Enforcing limits at the model-family level

One more wrinkle worth naming explicitly: provider-side quotas (OpenAI's org-level TPM, Azure's
per-deployment TPM) are usually allocated *per model*, not per gateway tenant — so the gateway's
per-tenant limiter and the provider's own upstream limiter are two independent constraints that both
have to hold simultaneously. A tenant well within their gateway-assigned quota can still get a 429 from
the provider if the *aggregate* traffic across all tenants routed to that model exceeds the provider's
allocation. The fix is a **provider-quota-aware admission check** layered on top of per-tenant limiting:
a shared bucket per `(provider, model)` representing the org's actual upstream allocation, checked in
addition to (not instead of) the per-tenant bucket. Getting only one of the two right is a common
production bug: teams that implement thorough per-tenant limiting but never model the shared upstream
ceiling are surprised when 429s appear despite every individual tenant reporting "well under quota."

---

## 7. Cost management

> **In plain words.** Every call costs money based on tokens in and tokens out. The gateway computes the cost of each call, records who made it, and can warn or block a team that goes over budget.
>
> **Real-world example.** With illustrative prices of $3 per million input tokens and $15 per million output tokens, a call with 2,000 input and 500 output tokens costs 2,000 × $3/1M + 500 × $15/1M = $0.006 + $0.0075 = $0.0135. At 80% of a $5,000 monthly budget ($4,000) the team gets an alert; at 100% the gateway switches them to a cheaper model.

### 7.1 Per-request cost calculation

```python
@dataclass
class ModelPricing:
    input_per_1k: float
    output_per_1k: float
    cached_input_per_1k: float = 0.0   # typically 10%–50% of input_per_1k, varies by provider (§11.4)


# ILLUSTRATIVE, DATED prices (list prices for these snapshots around late 2024).
# Do not copy them: load current prices from the vendors' pricing pages as config (§12).
PRICING: dict[str, ModelPricing] = {
    "openai:gpt-4o": ModelPricing(input_per_1k=0.0025, output_per_1k=0.010, cached_input_per_1k=0.00125),
    "anthropic:claude-3-5-sonnet-20241022": ModelPricing(input_per_1k=0.003, output_per_1k=0.015,
                                                          cached_input_per_1k=0.0003),
    # ... every (provider, model) the gateway routes to, kept current with vendor pricing pages
}


def calculate_cost(provider: str, model: str, usage: Usage) -> float:
    pricing = PRICING[f"{provider}:{model}"]
    billable_input = usage.input_tokens - usage.cached_input_tokens
    return (
        billable_input / 1000 * pricing.input_per_1k
        + usage.cached_input_tokens / 1000 * pricing.cached_input_per_1k
        + usage.output_tokens / 1000 * pricing.output_per_1k
    )
```

Note the `PRICING` table is itself a liability that needs an owner: provider pricing changes without
warning (new model releases, higher prices above a context-length threshold on some models, promo
pricing), and a stale table silently misattributes cost. Treat it as versioned, alerted-on
configuration (§12), not a constant baked into code.

### 7.2 Per-tenant and per-team attribution

```python
@dataclass
class CostRecord:
    tenant_id: str
    application_id: str
    team: str
    model: str
    provider: str
    cost_usd: float
    usage: Usage
    timestamp: float
    request_id: str
    variant: str | None = None   # canary/stable tag from §4.4, joins cost to experiment analysis


class CostLedger:
    """Every request writes exactly one record here, at the point usage is
    known (after the call, or after stream completion — see §10.3). This is
    the single source of truth every budget check, dashboard, and finance
    export reads from — never a per-application self-reported estimate."""

    async def record(self, record: CostRecord) -> None:
        await self._store.insert(record)               # e.g. append to a ClickHouse / BigQuery table
        await self._realtime_agg.increment(              # e.g. Redis, for §7.3's fast budget check
            key=f"cost:{record.tenant_id}:{_day_bucket(record.timestamp)}",
            amount=record.cost_usd,
        )
```

Attribution granularity should go at least to `tenant → application → team`, because "team X is
spending too much" is only actionable if you can immediately answer "on which application, calling
which model" without a follow-up investigation — the whole point of centralizing cost measurement is
losing the ability to say "not my problem, ask the app team," and the ledger should make that specific
answer a query, not an investigation.

### 7.3 Budget enforcement — hard limits and soft alerts

```python
@dataclass
class Budget:
    tenant_id: str
    monthly_limit_usd: float
    soft_alert_threshold: float = 0.8    # fraction of limit
    hard_limit_action: Literal["block", "downgrade_model", "alert_only"] = "block"


class BudgetEnforcer:
    def __init__(self, budgets: dict[str, Budget], ledger: CostLedger, alerter: "Alerter"):
        self._budgets = budgets
        self._ledger = ledger
        self._alerter = alerter

    async def check(self, tenant_id: str) -> "BudgetDecision":
        budget = self._budgets.get(tenant_id)
        if budget is None:
            return BudgetDecision(allow=True)

        spent = await self._ledger.month_to_date(tenant_id)
        fraction = spent / budget.monthly_limit_usd

        if fraction >= budget.soft_alert_threshold and not await self._already_alerted(tenant_id):
            await self._alerter.notify(tenant_id, fraction, budget)

        if fraction >= 1.0:
            if budget.hard_limit_action == "block":
                return BudgetDecision(allow=False, reason="monthly_budget_exhausted")
            if budget.hard_limit_action == "downgrade_model":
                return BudgetDecision(allow=True, forced_route="cheap")   # forces §4.3's cheap tier
        return BudgetDecision(allow=True)
```

`downgrade_model` as a hard-limit action, rather than a flat block, is worth calling out explicitly in
an interview: a platform team's incentive is rarely "stop the customer-facing feature from working the
moment a budget is hit" — it's "keep the feature degraded-but-functional while someone gets paged to
raise the budget or investigate the spend." Which action is appropriate is a product decision the
gateway should expose as configuration, not one the gateway should hardcode.

### 7.4 Token accounting nuances — input, output, cached, reasoning

Four token categories now need independent accounting on modern provider APIs, and conflating any of
them into "total tokens" produces wrong cost and wrong capacity-planning numbers:

- **Input tokens** — priced lowest, generally.
- **Output tokens** — typically 3-5x the input price (4x and 5x for the two dated examples in §7.1), because generation is autoregressive and cannot
  be batched across the sequence dimension the way prompt processing can.
- **Cached input tokens** — provider-side prompt caching (Anthropic's explicit cache-control blocks,
  OpenAI's automatic prefix caching) reprices repeated prefix content at a steep discount (roughly
  50% to 90% off depending on provider and model; Anthropic also charges a premium for cache writes), and a gateway that doesn't track this separately will show cost *increases* after
  enabling caching optimizations that are actually saving money, because the discount silently
  vanishes into an undifferentiated "input tokens" bucket.
- **Reasoning tokens** — o-series and extended-thinking models bill internal reasoning tokens at the
  output-token rate, and they do not appear (or appear only summarized) in the response content — a
  cost dashboard that estimates output from visible-content length will systematically undercount
  spend on these models. Check each provider's usage shape: OpenAI reports `reasoning_tokens` as a
  breakdown *already included* in `completion_tokens`, so adding it on top double-counts.

### 7.5 Cost-optimized routing and unit economics

Once cost is measured accurately per request, it becomes a routing input (§4.2's `CostLatencyRouter`)
and a product-economics input simultaneously — the same number that feeds "route this to the cheaper
provider" also feeds "what does this feature cost per active user per month," which is the number that
actually determines whether an LLM-powered feature is a viable product line. A senior platform
engineer's job includes being able to produce that second number on demand, not just the first.

---

## 8. Authentication and authorization

> **In plain words.** Provider keys live in one locked place that only the gateway can read. Each team gets its own gateway key, and a policy says which models each team may use.
>
> **Real-world example.** A leaked provider key used to mean checking 12 apps for copies. Now it is one rotation in the secrets manager. The finance team may use the model approved for financial data; the marketing team's request for it is refused before it uses any quota.

### 8.1 API key management

Provider credentials belong in a secrets manager (Vault, AWS Secrets Manager, GCP Secret Manager) —
never in gateway config files, environment variables committed anywhere, or application code — fetched
by the gateway at startup and on a rotation schedule, never by any application directly. This is the
concrete mechanism behind §1.2's "central authentication" claim: once every provider key lives in
exactly one service's secrets access, rotating a compromised key is a one-service operation instead of
an audit of every application that might have a copy.

```python
class CredentialStore:
    def __init__(self, secrets_client: "SecretsClient", refresh_interval_s: float = 300.0):
        self._client = secrets_client
        self._cache: dict[str, tuple[str, float]] = {}   # provider -> (key, fetched_at)
        self._refresh_interval = refresh_interval_s

    async def get(self, provider: str) -> str:
        cached = self._cache.get(provider)
        if cached and time.monotonic() - cached[1] < self._refresh_interval:
            return cached[0]
        key = await self._client.get_secret(f"llm-gateway/providers/{provider}/api-key")
        self._cache[provider] = (key, time.monotonic())
        return key
```

### 8.2 Per-tenant credentials and the multi-tenancy decision

Two legitimate models, and the choice is a compliance and blast-radius decision, not a technical one:

- **Shared gateway credentials, gateway-enforced tenant isolation** — the gateway holds one API key per
  provider, shared across all tenants, and tenant boundaries exist only in the gateway's own
  policy/quota layer. Simpler operationally; a compromised gateway key affects every tenant equally.
- **Per-tenant BYO credentials** — a tenant supplies their own provider API key (common for enterprise
  customers with existing provider contracts, or where a tenant's usage must appear on *their own*
  provider billing account for cost-allocation or compliance reasons). The gateway still centralizes
  routing, observability, and policy, but proxies through the tenant's own credential rather than a
  shared one — meaning per-tenant rate limits become largely advisory, since the real limiting
  authority is now the tenant's own provider account.

### 8.3 RBAC — which teams can use which models

```python
@dataclass
class ModelAccessPolicy:
    tenant_id: str
    allowed_model_aliases: set[str] | Literal["*"]
    denied_model_aliases: set[str] = field(default_factory=set)   # explicit deny wins over "*"


class AccessController:
    def __init__(self, policies: dict[str, ModelAccessPolicy]):
        self._policies = policies

    def check(self, tenant_id: str, alias: str) -> None:
        policy = self._policies.get(tenant_id)
        if policy is None:
            raise UnauthorizedError(tenant_id, alias, reason="no_policy_configured")
        if alias in policy.denied_model_aliases:
            raise UnauthorizedError(tenant_id, alias, reason="explicitly_denied")
        if policy.allowed_model_aliases != "*" and alias not in policy.allowed_model_aliases:
            raise UnauthorizedError(tenant_id, alias, reason="not_in_allowlist")
```

Model access control matters for reasons beyond cost: a model with a data-processing agreement
covering regulated data (health, finance) may be contractually approved for one team's workload and not
another's; a frontier reasoning model may be gated behind an internal approval process for cost reasons
independent of any individual request's budget. RBAC here is policy enforcement (§1.2), and belongs in
the Policy Engine stage of §2.1's pipeline — checked before the rate limiter, so a denied request never
consumes quota.

### 8.4 Secrets rotation without downtime

Rotation has to be a zero-downtime operation: fetch and validate the new credential, hold both old and
new as valid for an overlap window, cut traffic to the new credential, then revoke the old one — never
a hard swap that risks a window where the cached credential in `CredentialStore` is invalid and every
in-flight adapter call starts failing simultaneously. This is standard secrets-rotation practice, not
LLM-specific, but it is worth stating explicitly because a gateway's blast radius on a botched rotation
is every provider call in the organization at once.

---

## 9. Observability

> **In plain words.** Every call gets a log line with who, which model, how long, how many tokens, what it cost, and whether a backup was used. The prompt text itself is not logged unless a team opts in, because it may contain personal data.
>
> **Real-world example.** Latency jumps at 10:15. The dashboard shows time-to-first-token for one provider went from 600 ms to 4 s, while the 400-error rate is flat. So it is a provider problem, not an app bug, and the on-call engineer knows which one within a minute.

### 9.1 What to log, and the PII line

Every request through the gateway should produce a structured log record and an OTEL span with, at
minimum: request ID, tenant/application/team, model alias and resolved concrete model, provider,
latency (broken into TTFT and total, §9.2), token usage by category (§7.4), cost, finish reason, and
fallback depth (did this succeed on the primary or a fallback hop, §5.1) — none of which is PII and all
of which is safe to retain indefinitely for cost and reliability analysis.

**Prompt and completion content is a different, deliberate decision, not a default.** Full content
logging is invaluable for debugging quality regressions and disputes, but it means the gateway becomes
a repository of every prompt and completion in the organization, including whatever PII, customer data,
or regulated content users put in them. The defensible default: log content only when a tenant has
opted in, store it separately from the metrics/cost pipeline with its own retention and access-control
policy, and redact or hash obvious PII patterns (emails, SSNs, card numbers) before storage even when
opted in, rather than trusting every calling application to have already sanitized its inputs.

```python
@dataclass
class RequestLogRecord:
    request_id: str
    tenant_id: str
    application_id: str
    model_alias: str
    resolved_model: str
    provider: str
    latency_ms: float
    ttft_ms: float | None
    usage: Usage
    cost_usd: float
    finish_reason: str
    fallback_depth: int
    variant: str | None
    error: str | None
    # deliberately absent by default: prompt content, completion content —
    # see the opt-in content-logging pipeline for tenants that need it.
```

### 9.2 Latency: TTFT and total

Time-to-first-token and total latency are different SLOs measuring different user experiences and need
independent alerting thresholds — a chat UI cares intensely about TTFT (perceived responsiveness) and
tolerates a longer total latency for a long streamed answer; a batch summarization job cares about
total latency and not at all about TTFT. Reporting only one of the two, or an average that blends them,
hides regressions specific to either dimension.

### 9.3 Integration with Langfuse / LangSmith and OTEL GenAI semconv

The gateway should emit **both**: OTEL spans following the GenAI semantic conventions
(`gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, etc. — see
`../sre-observability/26-llm-and-ai-observability.md` for the full convention and why it exists) into
the org's general tracing backend for cross-system correlation (a slow gateway call showing up in the
same trace as the upstream API request that triggered it), and a dedicated LLM-observability
platform (Langfuse, LangSmith, Helicone) for the LLM-specific views those OTEL backends don't render
well — prompt/completion diffing across versions, eval score correlation, and per-prompt-template cost
breakdowns. Treat them as complementary, not redundant: one instrumentation call site emitting to both.

```python
from opentelemetry import trace

tracer = trace.get_tracer("llm-gateway")

async def instrumented_chat(request: ChatRequest, executor: ChainExecutor, chain: ProviderChain):
    with tracer.start_as_current_span("gen_ai.chat") as span:
        span.set_attribute("gen_ai.request.model", request.model)
        span.set_attribute("gen_ai.system", chain.targets[0].provider)
        span.set_attribute("tenant.id", request.tenant_id)
        try:
            response = await executor.execute(chain, request)
            span.set_attribute("gen_ai.response.model", response.model)
            span.set_attribute("gen_ai.usage.input_tokens", response.usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", response.usage.output_tokens)
            span.set_attribute("gen_ai.fallback_depth", response.message.metadata.get("fallback_depth", 0))
            return response
        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            raise
```

Two notes on the attribute names: `gen_ai.fallback_depth` and `tenant.id` are this gateway's own
custom attributes, not part of the OTEL convention; and the GenAI conventions are still evolving (newer
versions rename `gen_ai.system` to `gen_ai.provider.name`), so pin the semconv version you emit.

### 9.4 Error rate per provider, and the metric that actually predicts incidents

Track error rate segmented by `(provider, model, error_class)` — a rising 429 rate is a capacity/quota
signal demanding a routing or quota change; a rising 5xx rate is a provider incident demanding circuit
breaker attention; a rising 400 rate is almost always an application bug (malformed requests, a
context-window overflow from an unbounded conversation history) and paging on-call for it is the wrong
response. Collapsing all three into one "error rate" metric produces alerts that fire for the wrong
reason and get ignored.

---

## 10. Streaming through the gateway

> **In plain words.** Streaming sends the answer word by word so users see text right away. The gateway must pass each piece on immediately and still count tokens and cost at the very end.
>
> **Real-world example.** A user asks a question in a mobile app. The first words arrive after 0.5 s instead of waiting 8 s for the whole answer. If the user closes the app after 3 s, the tokens already generated were still billed, so the gateway still records their cost.

### 10.1 SSE end-to-end

The gateway has to be a transparent conduit for server-sent events without buffering the entire
response — buffering defeats the entire purpose of the caller having asked for a stream in the first
place (perceived latency, TTFT).

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest):
    if not request.stream:
        response = await gateway.chat(request)
        return response

    async def event_stream():
        async for event in gateway.chat_stream(request):
            yield f"data: {json.dumps(dataclasses.asdict(event))}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

### 10.2 Back-pressure and token-by-token forwarding

The gateway's stream consumer (reading from the provider) and stream producer (writing to the
application) run at different, independently-varying rates — a slow client connection (mobile network,
a UI rendering each token) must not cause the gateway to buffer unboundedly waiting for the client to
catch up, and a provider streaming faster than the client can consume needs the gateway's own
`async for` loop over the provider stream to naturally apply back-pressure by not pulling the next chunk
until the current one is flushed to the client — which `StreamingResponse`'s async generator model gives
for free, provided the gateway does not eagerly buffer chunks into a list before yielding them (an easy
mistake when someone "helpfully" accumulates the full response for logging *before* yielding, which
silently converts a stream into a buffer-then-forward pattern with all of streaming's latency cost and
none of its TTFT benefit).

### 10.3 Streaming complicates rate limiting and cost — the settlement problem

Both §6's admission control and §7's cost ledger assume usage is known at the point of enforcement.
Streaming breaks that assumption structurally: **total token usage is only known when the stream
completes**, which can be tens of seconds after admission. The gateway therefore has to run the
reserve-then-settle pattern from §6.4 across the entire stream lifetime, not around a single call:

```python
async def stream_with_accounting(request: ChatRequest, adapter: ProviderAdapter,
                                  reservation: Reservation, enforcer: TokenQuotaEnforcer,
                                  ledger: CostLedger) -> AsyncIterator[StreamEvent]:
    usage_so_far = Usage(input_tokens=0, output_tokens=0)
    try:
        async for event in adapter.chat_stream(request):
            if isinstance(event, UsageUpdate):
                usage_so_far = event.usage   # providers typically send a final usage event at stream end
            yield event
    finally:
        # Settlement happens even on a client-disconnected or errored stream —
        # tokens already generated by the provider were already billed by the
        # provider regardless of whether the client stayed connected to receive them.
        await enforcer.settle(reservation, usage_so_far)
        await ledger.record(CostRecord(..., usage=usage_so_far,
                                        cost_usd=calculate_cost(adapter.name, request.model, usage_so_far)))
```

The `finally` block settling on disconnect is the detail that catches teams off guard in production: a
client that drops mid-stream (a mobile app backgrounded, a browser tab closed) does not un-bill the
tokens the provider already generated before the disconnect — the gateway must settle against
whatever partial usage the provider reports, not skip settlement because "the response was never
delivered."

---

## 11. Caching

> **In plain words.** A cache stores answers so a repeated question doesn't cost another model call. Exact-match caching is safe; "similar question" caching can return the wrong answer and must be used with care.
>
> **Real-world example.** A classification endpoint sees the same 2,000 product titles again and again at temperature 0; an exact cache serves repeats in 5 ms for $0. But "cancel my subscription" and "how do I NOT cancel my subscription" look similar to an embedding model and must never share an answer.

### 11.1 Exact-match caching

The simplest and safest cache: hash the full canonical request (messages, model, temperature, tools —
everything that affects the output distribution) and cache the response keyed on that hash, with a
bounded TTL.

```python
import hashlib


def request_cache_key(request: ChatRequest) -> str:
    canonical = json.dumps({
        "messages": [dataclasses.asdict(m) for m in request.messages],
        "model": request.model,
        "temperature": request.temperature,
        "tools": [dataclasses.asdict(t) for t in request.tools],
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()
```

Exact-match caching is safe by construction for `temperature = 0`, deterministic-tool-only workloads
(classification, extraction, structured parsing) where the whole point is that identical input should
produce identical output — and actively wrong for anything creative, conversational, or intentionally
varied, where a cached response defeats the purpose of calling the model at all.

### 11.2 Semantic caching — and why it is dangerous by default

Semantic caching embeds the incoming query, searches a cache of previously embedded queries for a
near-neighbor above a similarity threshold, and serves that cached response if found — the idea being
that "what's the capital of France" and "France's capital city?" should hit the same cache entry. This
is genuinely useful for high-volume, narrow-domain FAQ-style traffic, and genuinely dangerous as a
default gateway feature, for a reason worth stating plainly in an interview: **embedding similarity is
not semantic equivalence**. "Cancel my subscription" and "how do I NOT cancel my subscription" embed
close together (same topic, similar vocabulary, opposite intent), and a similarity threshold loose
enough to catch real paraphrases will also catch pairs like this, serving a confidently wrong cached
answer with no signal to the caller that anything went wrong. Semantic caching belongs behind an
explicit opt-in per use case, with a similarity threshold tuned and evaluated against that specific
use case's query distribution — never a blanket gateway-wide default.

```python
class SemanticCache:
    def __init__(self, embedder: "EmbeddingClient", vector_index: "VectorIndex",
                 similarity_threshold: float = 0.97):   # deliberately conservative default
        self._embedder = embedder
        self._index = vector_index
        self._threshold = similarity_threshold

    async def lookup(self, query: str) -> ChatResponse | None:
        vec = await self._embedder.embed(query)
        match = await self._index.search(vec, top_k=1)
        if match and match.score >= self._threshold:
            return match.cached_response
        return None
```

### 11.3 Cache invalidation

Model or prompt-template version bumps must invalidate every cache entry produced under the old
version — the cache key has to include a model/template version component, not just the query content,
or a prompt engineering fix silently fails to take effect for every cached-hit query until the TTL
naturally expires. This is the same staleness-vs-correctness tradeoff every cache faces, applied here
with an unusually sharp edge: a stale LLM cache entry doesn't just serve outdated data, it can serve an
answer generated under a prompt version the team believes no longer exists.

### 11.4 Provider-side prompt caching

Distinct from the gateway's own response cache: providers now cache the model's internal KV-state for a
repeated prompt *prefix* server-side, cutting cost and latency on the cached portion without changing
the output at all (the model still runs generation fresh — only the prefix's attention computation is
reused). Anthropic requires explicit `cache_control` breakpoints in the request; OpenAI's prefix caching
is automatic above a length threshold. The gateway's job here is narrower than for its own cache: put
the stable, repeated part of a prompt (system instructions, few-shot examples, a large retrieved
context reused across turns) *first* and consistently byte-identical across calls, and expose per-model
cache-hit-rate metrics (§7.4's cached-token accounting) so teams can see whether their prompt structure
is actually earning the discount.

---

## 12. Configuration management

> **In plain words.** Apps ask for a nickname like "default" or "fast", not a real model name. The platform team decides in config which real, dated model each nickname points to.
>
> **Real-world example.** "default" points to a pinned snapshot. To upgrade, the team runs the eval suite on the new model, then changes one line of config. All 30 apps that call "default" move over without a single app deploy.

### 12.1 Model aliases and version pinning

Every routing decision in §4 resolves an **alias**, never a raw provider model string, specifically so
that changing what "default" or "fast" points to is a config push, not an application deploy across
every consumer of that alias.

```yaml
aliases:
  default:
    concrete: {provider: openai, model: gpt-4o-2024-11-20}   # pinned snapshot, not the floating "gpt-4o"
    pin_policy: explicit_snapshot   # vs "latest" — explicit is the safer default for anything customer-facing
  fast:
    concrete: {provider: anthropic, model: claude-3-5-haiku-20241022}
  reasoning:
    concrete: {provider: openai, model: o3-2025-04-16}
```

Pinning to an explicit dated snapshot rather than a floating alias like `gpt-4o` is the deliberate,
boring choice for anything with an eval suite or a customer-facing behavior contract: providers do
silently update floating aliases to newer underlying weights, and an eval-passing prompt today can
regress tomorrow with zero code change and zero deploy on your side. Floating aliases are appropriate
only for exploratory or internal-tooling traffic where "always get whatever's newest" is the actual
desired behavior.

### 12.2 Environment-based config and feature flags

```python
@dataclass
class GatewayConfig:
    environment: Literal["dev", "staging", "prod"]
    aliases: dict[str, RoutingRule]
    feature_flags: dict[str, bool] = field(default_factory=dict)

    def resolve_alias(self, alias: str, tenant_id: str, flags: "FlagClient") -> RoutingRule:
        rule = self.aliases[alias]
        if flags.is_enabled("route-canary-model-v2", tenant_id):
            return self._canary_variant(rule)
        return rule
```

Feature-flagging the routing table itself (rather than only flagging application-level behavior) is
what makes canary routing (§4.4) an operational lever pulled by a platform on-call engineer without a
gateway redeploy — the same architectural benefit as any other feature-flagged config, applied to model
choice specifically.

### 12.3 Changing models without changing application code

This is §1's central promised payoff made concrete: an application calls `gateway.chat(model="default",
...)`. The platform team updates the `default` alias's `concrete` target in config, deploys the config
(not the application), and every caller of `"default"` is now on the new model — with the eval suite
(§08's methodology, run against the new concrete target before the alias flip) as the actual gate, not
the code review of an application PR nobody on the platform team would even see.

---

## 13. The SDK side — client experience

> **In plain words.** The SDK is the small library apps use to call the gateway. It should be easy to use and should only retry the connection to the gateway; the gateway handles provider failures.
>
> **Real-world example.** A developer writes `client.chat(messages)` and gets a typed answer back. If both the SDK and the gateway retried 3 times at every layer, one failing request could turn into dozens of provider calls; keeping the SDK's retries small prevents that.

### 13.1 A typed client wrapping the gateway

```python
import httpx
from typing import AsyncIterator


class GatewayClient:
    def __init__(self, base_url: str, api_key: str, tenant_id: str,
                 timeout: float = 30.0, max_retries: int = 2):
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout,
                                        headers={"Authorization": f"Bearer {api_key}",
                                                 "X-Tenant-Id": tenant_id})
        self._max_retries = max_retries

    async def chat(self, messages: list[Message], model: str = "default", **kwargs) -> ChatResponse:
        payload = {"messages": [dataclasses.asdict(m) for m in messages], "model": model, **kwargs}
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._http.post("/v1/chat/completions", json=payload)
                resp.raise_for_status()
                return _parse_response(resp.json())
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < self._max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise GatewayClientError.from_response(e.response) from e

    async def chat_stream(self, messages: list[Message], model: str = "default", **kwargs) -> AsyncIterator[StreamEvent]:
        payload = {"messages": [dataclasses.asdict(m) for m in messages], "model": model, "stream": True, **kwargs}
        async with self._http.stream("POST", "/v1/chat/completions", json=payload) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    yield _parse_stream_event(json.loads(line[6:]))

    async def structured(self, messages: list[Message], schema: type["BaseModel"], model: str = "default") -> "BaseModel":
        response = await self.chat(messages, model=model,
                                    response_format={"type": "json_schema", "schema": schema.model_json_schema()})
        return schema.model_validate_json(response.message.content)
```

### 13.2 Retry logic — SDK versus gateway, and why both exist

This division of labor is a common source of confusion worth being precise about in an interview. The
**gateway** retries and falls back *across providers* (§5) because it is the only layer with visibility
into which providers exist and which are healthy — an application has no business knowing there even
are three fallback providers behind "default." The **SDK** retries only the *transport hop to the
gateway itself* — a dropped connection, a gateway-side 503 during a deploy — a narrower, client-local
concern that has nothing to do with LLM provider health. Retrying the same failure at both layers
independently (an SDK doing 3 retries, each hitting a gateway that itself does a 3-provider fallback
chain with its own retries) multiplies worst-case latency combinatorially; keep the SDK's retry budget
small and scoped to gateway connectivity only, and trust the gateway to own provider-level resilience
entirely.

### 13.3 Streaming helpers and structured-output ergonomics

A good SDK hides the SSE parsing and event-type dispatch behind an iterator of typed events (as in
`chat_stream` above), and hides structured-output JSON-schema plumbing behind a call that takes a
Pydantic model and returns a validated instance (`structured`, above) — the developer-experience bar is
"a developer who has never read this chapter can call `.chat()` or `.structured()` and get a
correctly-typed result," with every provider-abstraction, retry, and normalization concern from §2-§11
invisible unless something goes wrong, at which point the error type (`GatewayClientError` subtypes
matching §3's adapter error taxonomy) should be specific enough to act on without reading gateway logs.

---

## 14. Production deployment

> **In plain words.** The gateway must be more reliable than any provider behind it, because if it is down, everything is down. So it runs as many identical copies with shared state, careful health checks and safe deploys.
>
> **Real-world example.** 20 gateway copies sit behind a load balancer. Rate-limit counters and circuit-breaker state live in Redis, so if one copy sees a provider fail, all 20 stop calling it. A bad release goes to the "green" copies first and is rolled back before most traffic hits it.

### 14.1 Stateless design and horizontal scaling

The gateway process itself must hold no per-request state that survives past that request's response —
circuit breaker state, rate-limit buckets, and the cost ledger's real-time aggregates all live in a
shared store (Redis, typically) rather than in-process memory, specifically so that any gateway replica
can serve any request and replicas can be added or removed under a standard load balancer without
sticky sessions. A circuit breaker's state kept in-process instead of shared means each replica
independently, slowly, re-discovers that a provider is down — multiplying the blast-radius window by
however many replicas exist before the whole fleet's breakers converge.

```python
class RedisCircuitBreaker:
    """Same state machine as §5.2's CircuitBreaker, backed by shared state so
    every gateway replica observes the same breaker transitions."""

    def __init__(self, redis: "Redis", key: str, failure_threshold: int, recovery_timeout_s: float):
        self._redis = redis
        self._key = key
        self._threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_s

    async def is_open(self) -> bool:
        state = await self._redis.hgetall(self._key)
        if state.get("status") != "open":
            return False
        opened_at = float(state["opened_at"])
        if time.monotonic() - opened_at >= self._recovery_timeout:
            await self._redis.hset(self._key, "status", "half_open")
            return False
        return True

    async def record_failure(self) -> None:
        # atomic increment + conditional open, via a Lua script in production
        # to avoid a race between the read-count / compare / write-open steps
        # across concurrent replicas incrementing the same key simultaneously.
        count = await self._redis.hincrby(self._key, "failures", 1)
        if count >= self._threshold:
            await self._redis.hset(self._key, mapping={"status": "open", "opened_at": time.monotonic()})
```

### 14.2 Load balancing and health checks

Standard L7 load balancing (round-robin or least-connections) across stateless gateway replicas, with a
health check endpoint that distinguishes "process is up" from "process can actually serve" — the
gateway's own `/healthz` should verify its dependency chain (Redis reachable, secrets store reachable),
not just return 200 unconditionally, or a load balancer will keep routing traffic to a replica that is
technically running but functionally unable to check quota or fetch credentials.

### 14.3 Blue/green deployment and multi-region

Blue/green deploys matter more for a gateway than for a typical stateless API, because the gateway sits
in front of every LLM-dependent feature in the organization — a bad gateway deploy is not one team's
incident, it's every team's incident simultaneously. Multi-region deployment additionally has to account
for provider latency geography (routing EU traffic to an EU-region Azure OpenAI deployment both for
latency and for data-residency compliance, per §8.2's tenant-credential discussion) and for the shared
Redis state in §14.1 needing either regional partitioning (accepting slightly looser global rate-limit
accuracy) or a cross-region-consistent store (accepting higher latency on every quota check) — there is
no free option here, and which tradeoff to take is a decision that should be made explicitly, with the
cost stated, rather than defaulted into by whatever the first regional deployment happened to do.

---

## 15. Comparison with existing solutions — build vs buy

> **In plain words.** You don't have to build a gateway from scratch. Open-source and commercial tools exist; the question is whether they fit your rules on data, routing and cost tracking.
>
> **Real-world example.** A 200-person startup with no strict data-residency rules can run an open-source proxy in a day. A bank that must keep prompts inside one region and bill 40 internal teams separately may keep the open-source adapters but build its own policy and cost ledger.

| Solution | What it is | Where it's strong | Where it falls short of a custom platform gateway |
|---|---|---|---|
| **LiteLLM** | Open-source Python library / proxy server providing a unified OpenAI-compatible interface across 100+ providers | Broadest provider coverage available anywhere, drop-in OpenAI SDK compatibility, active community, self-hostable proxy mode with built-in rate limiting and budgets | Policy/RBAC and cost-attribution features are less mature than a purpose-built internal platform's; deep customization (bespoke content-based routing, org-specific compliance rules) means forking or extending, at which point you own a fork of someone else's abstraction rather than your own |
| **Portkey** | Commercial gateway-as-a-service, with a hosted control plane, extensive caching/routing/observability features (its core gateway is also available as open source) | Polished UI, fast to adopt, strong out-of-box observability and guardrails | Data governance implications of a third party proxying every prompt when using the hosted service (even with content-logging opt-outs, request metadata transits their infrastructure); vendor lock-in on routing/config surface; ongoing platform pricing on top of underlying model cost |
| **Helicone** | Primarily an observability/logging layer for LLM calls, with lighter gateway features (caching, rate limiting) added over time | Best-in-class request/response logging and cost dashboards with minimal integration effort (often a base-URL swap) | Not a full gateway — routing, fallback, and RBAC are thinner than LiteLLM's or Portkey's; better thought of as complementary to a gateway than a replacement for one |
| **Custom gateway** (this chapter) | Purpose-built internal service | Exact fit to internal policy, compliance, and cost-attribution requirements; no third-party in the request path; full control over the routing/fallback logic that matters most to your specific traffic mix | Ongoing engineering and on-call cost (§1.3); reinventing genuinely solved problems (provider adapters, SSE parsing) unless scoped carefully |

**The actual decision framework**, not "which is best" in the abstract: build when compliance or
data-residency constraints make routing prompt content through any third party's infrastructure a
non-starter, when the organization's routing/policy requirements are specific enough that an
off-the-shelf tool's extension points don't reach them, or when the LLM gateway is itself close to the
company's core product (an AI infrastructure company has no business buying this). Buy — or start
with an open-source proxy like LiteLLM and extend it — when the organization's needs are close to the
common case (multi-provider access, basic routing, standard observability) and the engineering time
saved outweighs the lock-in and customization ceiling, which is the common case for most organizations
whose product is not the gateway itself. A frequent, pragmatic middle path: adopt LiteLLM's provider
adapters and proxy runtime (§3's hardest, most churn-prone code, already solved and maintained upstream)
while building the policy engine, cost ledger, and RBAC layer in-house, where the organization's actual
differentiated requirements live.

---

## 16. Interview questions

> **In plain words.** For each question, first say the idea in one plain sentence, then give one number, then name one trade-off. The "Weak" and "Strong" answers below show what interviewers listen for.
>
> **Real-world example.** "Why a gateway?" → "One front door for all model calls: 6 apps and 4 providers become 10 integrations instead of 24. The cost is an extra hop and a new single point of failure, so the gateway must be more reliable than any provider."

**1. Why build a model gateway instead of letting each application call providers directly?**
*Weak:* "So we don't repeat code." *Strong:* names the N×M-to-N+M reduction in §1.1, and lists which
downstream capabilities (auth, rate limiting, cost control, observability, RBAC) are consequences of
having one interception point rather than independent features — and is honest about the cost side
(§1.3): a gateway is a new SPOF (single point of failure) and a new latency hop, and its own reliability bar must exceed any
single provider's.

**2. Walk through the full request lifecycle, start to finish.** *Weak:* "the app calls the gateway,
which calls the LLM." *Strong:* the full pipeline in §2.1 — SDK, Gateway API, Router, Policy Engine,
Rate Limiter, Provider Adapter — naming what question each stage answers and what breaks if that stage
is missing (§2.1's table), plus which parts operate on canonical types only versus provider-specific
wire formats (§2.3).

**3. Why is `Protocol`/structural typing a defensible choice for the provider adapter interface,
versus a shared abstract base class?** *Weak:* "they're basically the same." *Strong:* names that
adapters are often thin SDK wrappers whose constructor shape varies by provider, that structural typing
tolerates that variance without forcing a common `__init__` contract, and concedes the real tradeoff —
an ABC enforces the interface earlier, at class-definition time, which some teams prefer (§3.1).

**4. Anthropic and OpenAI structure tool results differently. What has to change in the canonical
message format, or does anything?** *Weak:* doesn't know either provider's actual shape. *Strong:*
describes Anthropic's system-prompt hoisting and tool_result-as-user-content-block versus OpenAI's
separate `role: tool` messages (§3.3), and explains why the canonical `Message` type in §2.2 needs a
`tool_results` field general enough that either adapter can losslessly translate to and from it.

**5. How would you route a request to the cheapest provider that meets a latency SLO?** *Weak:*
"always use the cheapest model." *Strong:* describes filtering to providers under an error-rate
ceiling first, then within that healthy set to those meeting the SLO by p50/p95, then minimizing cost
only within that filtered pool (§4.2) — cost-mindless-of-health optimization routes traffic straight
into a degraded provider.

**6. What's the difference between a circuit breaker tripping and a routing decision preferring one
provider over another?** *Weak:* treats them as the same mechanism. *Strong:* circuit breaking answers
a binary "stop calling this entirely, for now" based on sustained hard-failure or latency-SLA breach;
dynamic routing answers "which of several imperfect, still-open options do I prefer right now" — two
different decisions consuming the same health signal (§5.3).

**7. Why can't you retry an LLM call the way you'd retry a database write?** *Weak:* "LLMs are just
slower." *Strong:* names non-determinism (even at temperature 0, in practice) meaning a retry is not
guaranteed to return the same answer, the double-billing risk on ambiguous network-level failures with
no idempotency-key mechanism on most chat completion APIs, and the sharper problem of retrying a turn
whose prior assistant message already executed a tool call with a real side effect (§5.6).

**8. Why can't you rate-limit tokens the same way you rate-limit requests?** *Weak:* doesn't see a
difference. *Strong:* true token cost is unknown until the response completes, so admission has to
happen against an *estimate* (input token count plus an output ceiling), settled against actual usage
after the fact — the reserve-then-settle pattern in §6.4 — and names why a naive "check after the call"
approach isn't rate limiting at all, since the cost was already incurred.

**9. Token bucket or sliding window for a distributed rate limiter — which do you pick and why?**
*Weak:* picks one without justification. *Strong:* sliding window is exact but stores every event;
token bucket is O(1) state, tolerates controlled bursting, and maps cleanly onto an atomic Redis
operation across replicas — names the actual deciding factor for a *distributed* limiter specifically
being the state size and atomic-update cost, not "one is just better" (§6.3).

**10. A tenant is well within their assigned quota but still getting 429s from the provider. What's
going on?** *Weak:* "the quota system is buggy." *Strong:* identifies the missing shared
`(provider, model)`-level ceiling representing the org's actual upstream provider allocation, distinct
from and in addition to per-tenant limiting — aggregate traffic across all tenants can exceed the
provider's real quota even when every individual tenant is compliant (§6.5).

**11. How do you calculate the true cost of a request when the provider offers prompt caching?**
*Weak:* multiplies total tokens by one input rate. *Strong:* separates billable (non-cached) input,
cached input at its discounted rate, and output at its own (usually higher) rate — and flags reasoning
tokens as a fourth, often-invisible-in-content category billed at the output rate (§7.1, §7.4).

**12. A team says the gateway's cost dashboard shows higher spend after they enabled prompt caching.
What's the likely bug?** *Weak:* "caching must not be working." *Strong:* the cost calculation is
likely summing cached and non-cached input tokens at the same rate, so the actual discount is being
computed correctly by the provider but erased in the gateway's own cost math — the fix is separating
`cached_input_tokens` from `input_tokens` in the `Usage` type and pricing table, not debugging the
cache itself (§7.4).

**13. When would you choose "block" versus "downgrade model" as a hard-budget-limit action?**
*Weak:* "block is safer." *Strong:* names that this is a product decision about whether a
customer-facing feature should go fully dark versus degrade in quality when a tenant's budget is
exhausted, and that the platform's job is exposing this as configuration per use case rather than
picking one behavior for the whole org (§7.3).

**14. Why is per-request content logging (prompts and completions) not simply "on by default" the
way latency and cost metrics are?** *Weak:* "it uses too much storage." *Strong:* names that
content logging turns the gateway into an aggregation point for every PII and regulated-data string in
every prompt across the org, and that the correct default is opt-in, separately access-controlled
storage with its own retention policy — versus metrics/cost fields, which carry no such risk and
should be on unconditionally (§9.1).

**15. Why should a gateway emit both OTEL GenAI-semconv spans and Langfuse/LangSmith traces, instead
of picking one?** *Weak:* "redundancy is good." *Strong:* OTEL spans buy cross-system trace
correlation with the rest of the org's tracing infrastructure (a slow gateway call inside the same
trace as the request that triggered it); the LLM-specific platform buys prompt/completion diffing and
eval-score correlation that general tracing backends don't render — different questions, same
instrumentation call site (§9.3).

**16. How does streaming complicate rate limiting and cost accounting, specifically?** *Weak:*
"streaming is just slower to bill." *Strong:* total usage is only known when the stream completes
(often tens of seconds after admission), so the reserve-then-settle pattern has to span the entire
stream lifetime, and settlement must still occur on a client-disconnected or errored stream, because
the provider already billed whatever tokens it generated regardless of delivery (§10.3).

**17. Why is semantic caching dangerous as a default gateway feature?** *Weak:* "it might return the
wrong answer sometimes." *Strong:* names the actual mechanism — embedding similarity captures topical
closeness, not semantic equivalence or intent, so near-opposite queries ("cancel my subscription" vs.
"how do I not cancel") can embed close enough to collide at a loose threshold, serving a confidently
wrong cached answer with no signal anything went wrong — and that it should be opt-in per use case with
a tuned threshold, never a blanket default (§11.2).

**18. Why must the cache key for exact-match caching include a model/template version, not just the
query text?** *Weak:* doesn't see the issue. *Strong:* a prompt-template or model-version bump has to
invalidate every cache entry produced under the old version, or a prompt fix silently doesn't take
effect for cached-hit queries until the TTL naturally expires — the cache key needs a version
component precisely to make invalidation automatic rather than a manual flush someone has to remember
(§11.3).

**19. Why should the SDK's retry logic be narrower in scope than the gateway's?** *Weak:* "so there's
less code." *Strong:* the gateway owns provider-level resilience (fallback across a chain it alone has
visibility into, §5); the SDK should retry only the transport hop to the gateway itself — retrying the
same underlying failure independently at both layers multiplies worst-case latency combinatorially
instead of adding real resilience (§13.2).

**20. Why does the gateway need to be *more* reliable than any single provider it fronts, not just
"as reliable"?** *Weak:* "high availability is always good." *Strong:* every application depending on
the gateway is down if the gateway is down, even when every underlying provider is healthy — the
gateway sits in series with, not in parallel to, provider availability, so its own uptime is a ceiling
on the effective uptime of every feature behind it, which is exactly why §14 treats stateless design,
shared-state circuit breakers, and blue/green deploys as non-optional rather than nice-to-haves.

**21. System design: design a model gateway for 100+ internal teams, multiple cloud providers, and a
compliance requirement that some teams' data must never leave a specific cloud region.**
*Weak:* draws one box labeled "gateway" between "apps" and "LLMs" and lists feature names. *Strong*
walks the interviewer through: the canonical message format and adapter layer (§2, §3) so the
region-constrained teams' Azure-in-region deployment and everyone else's OpenAI/Anthropic traffic share
one interface; per-tenant routing overrides (§4.1's `tenant_override`) pinning the constrained teams to
their compliant target with no code path that could route around it; RBAC (§8.3) enforced before rate
limiting so denied requests never touch quota; a shared-state design (§14.1) so the deployment
horizontally scales without per-replica drift in breaker or quota state; multi-region deployment
(§14.3) with the state-consistency tradeoff named explicitly rather than assumed away; and a
cost-ledger (§7.2) granular enough that 100+ teams' finance questions are answerable by query, not
investigation. A strong answer also volunteers the failure mode this design is *not* solving —
if the gateway itself is deployed in a region a compliance boundary excludes, the boundary is violated
regardless of adapter-layer correctness, so the gateway's own deployment topology, not just its routing
logic, has to respect the constraint.

**22. What's the single biggest way a home-grown gateway can become worse than the N×M mess it
replaced?** *Weak:* "if it has bugs." *Strong:* if it is built and deployed as an afterthought — single
instance, unobserved, un-load-tested — it becomes a lower-reliability, higher-latency chokepoint that
every application now depends on, which is a strictly worse failure mode than N independent
integrations, because a single provider's outage used to take down only that provider's callers, and
now a gateway outage takes down everyone regardless of which providers are actually healthy (§1.3,
§14).

---

## 17. Lab exercises

**Lab 1 — Canonical message format and two real provider adapters.**
*Goal:* prove §2.2's canonical `ChatRequest`/`ChatResponse` types are actually provider-neutral, not
just neutral on paper.
*Steps:* implement the `ChatRequest`/`ChatResponse` dataclasses from §2.2, then build real adapters for
OpenAI and Anthropic (§3.2, §3.3) against live API keys. Send the identical canonical request —
including at least one tool definition and one multi-turn tool-call/tool-result exchange — through
both adapters and confirm both produce a valid canonical `ChatResponse` with correctly normalized
`tool_calls` and `usage`.
*Artifact:* the two adapter implementations and a side-by-side response diff for the same canonical
input.
*Success criterion:* zero provider-specific branching anywhere outside the two adapter files.
*Time:* ~1 day plus API cost.
*Unblocks:* every later lab, all of which assume this abstraction layer exists.

**Lab 2 — A router with static, tenant-override, and content-based rules.**
*Goal:* build §4's router as a composable pipeline, not one function with accumulating `if` statements.
*Steps:* implement `StaticRouter` (§4.1) with at least three aliases, a tenant override for one alias,
and `ComplexityRouter` (§4.3) selecting between a cheap and capable model by input length. Write test
cases proving: an unoverridden tenant gets the default target, an overridden tenant always gets their
pinned target regardless of complexity signal, and a long/tool-heavy request always escalates to the
capable tier.
*Artifact:* the router implementation and its test suite.
*Success criterion:* all three routing behaviors independently testable without mocking the other two.
*Time:* ~half a day.
*Unblocks:* Lab 3.

**Lab 3 — Fallback chain with a real circuit breaker, tested against induced failures.**
*Goal:* prove §5's `ChainExecutor` and `CircuitBreaker` actually fail over, not just in theory.
*Steps:* wire a three-target `ProviderChain` (use a mock "flaky" adapter for two of the three, and
your real Lab 1 adapter as the working one). Induce a sustained failure on the primary until its
breaker trips, confirm the executor falls to the secondary automatically, then stop the induced failure
and confirm the breaker's half-open trial eventually recovers the primary. Separately, confirm a
`ProviderBadRequestError` (a deliberately malformed request) does NOT trigger fallback to the next hop.
*Artifact:* the chain executor, breaker, and a log of the induced-failure/recovery cycle plus the
bad-request non-fallback case.
*Success criterion:* automatic failover, automatic recovery, and correct non-fallback on a
non-retryable error class, all demonstrated from logs, not asserted from code review.
*Time:* ~1 day.
*Unblocks:* Lab 6.

**Lab 4 — Token-bucket rate limiter with reserve-then-settle admission.**
*Goal:* make §6.4's pre-flight estimation problem a working implementation, not a paragraph.
*Steps:* implement `TokenBucket` (§6.3) and `TokenQuotaEnforcer` (§6.4) with per-tenant and
per-(provider,model) scopes (§6.5). Drive concurrent requests from two simulated tenants against a
shared per-model bucket sized to force contention, and confirm one tenant's traffic cannot starve the
other below their configured floor. Separately, confirm settlement correctly refunds an
over-reservation when the actual completion is shorter than the reserved ceiling.
*Artifact:* the enforcer implementation and a concurrency test demonstrating both the starvation
protection and the settlement refund.
*Success criterion:* neither tenant is ever denied below their configured floor even under the other
tenant's sustained max-rate traffic.
*Time:* ~1 day.
*Unblocks:* Lab 5.

**Lab 5 — Per-tenant cost ledger with hard-budget enforcement.**
*Goal:* turn §7's cost calculation and budget enforcement into a real, queryable ledger.
*Steps:* implement `calculate_cost` (§7.1) against a real pricing table for your Lab 1 providers, wire
it to a `CostLedger` (§7.2) backed by a real datastore (SQLite is fine for the lab), and implement
`BudgetEnforcer` (§7.3) with both a soft-alert threshold and a hard "downgrade_model" action. Run
enough traffic through one tenant to cross both thresholds and confirm the soft alert fires once (not
repeatedly) and the hard limit correctly forces the cheap route rather than blocking outright.
*Artifact:* the ledger schema, enforcer, and a demonstrated soft-alert-then-hard-downgrade sequence
from real recorded cost data.
*Success criterion:* a query against the ledger answers "how much did tenant X spend on model Y
yesterday" correctly, and the downgrade actually changes which target the router resolves to.
*Time:* ~1 day.
*Unblocks:* Lab 8.

**Lab 6 — Streaming with correct settlement on disconnect.**
*Goal:* prove §10.3's hardest claim — that settlement must happen even when the client disconnects
mid-stream.
*Steps:* implement `stream_with_accounting` (§10.3) against a real streaming adapter. Start a stream,
forcibly kill the client connection partway through (before `[DONE]`), and confirm the `finally` block
still settles the reservation and writes a cost record using whatever partial usage the provider
reported — not zero, and not skipped entirely.
*Artifact:* the streaming accounting wrapper and a log showing a mid-stream disconnect followed by a
correct non-zero settlement.
*Success criterion:* the cost ledger has an accurate record for the disconnected request, matching
what the provider actually billed.
*Time:* ~half a day.
*Unblocks:* Lab 7.

**Lab 7 — Exact-match cache with version-aware invalidation.**
*Goal:* prove §11.3's invalidation claim, not just build a cache that returns fast on a repeat query.
*Steps:* implement the exact-match cache (§11.1) with a cache key that includes a
prompt-template-version field. Cache a response under version 1, bump the version, and confirm the
next identical query is a cache MISS (correctly recomputed) rather than serving the stale version-1
response. Then repeat the same query twice under the new version and confirm the second is a HIT.
*Artifact:* the cache implementation and a log showing the version-bump-triggers-miss and
same-version-hits sequences.
*Success criterion:* zero stale responses served across a version bump, verified from logs.
*Time:* ~half a day.
*Unblocks:* none directly, but required before any production caching claim in this repo's later
serving-latency chapter.

**Lab 8 — End-to-end FastAPI gateway with full instrumentation.**
*Goal:* assemble every prior lab's component into one running gateway service, matching §2.1's full
pipeline, with real OTEL spans.
*Steps:* wire the Lab 1 adapters, Lab 2 router, Lab 3 fallback/breaker, Lab 4 rate limiter, and Lab 5
cost ledger behind the FastAPI app from §10.1, with the OTEL instrumentation from §9.3 on every
request. Run a mixed load test (a script issuing concurrent requests across at least two simulated
tenants, with one tenant deliberately exceeding quota) and pull the resulting traces into a local
Jaeger or console exporter to confirm span attributes match §9.3's schema.
*Artifact:* the assembled gateway service, the load test script, and a captured trace showing correct
`gen_ai.*` attributes including fallback depth and token usage.
*Success criterion:* a single trace for a fallback-triggering request shows the primary failure, the
breaker state change, and the successful secondary call as one coherent span tree.
*Time:* ~1.5 days.
*Unblocks:* `11-token-accounting-and-cost.md` and `12-serving-latency-and-caching.md`'s planned labs,
which assume a working gateway to instrument further.

---

## 18. Real-world cases — incidents with numbers

> **In plain words.** Each case is a kind of problem teams hit when running a model gateway: what users or finance saw, why it happened, the numbers, and the fix.
>
> **Real-world example.** A team is "under quota" but gets 429s → Case 2. Everyone waits a minute when a provider hangs → Case 3.

These are **composite scenarios** built from failure modes this chapter describes; numbers are
illustrative but internally consistent. They are not any specific company's postmortem. Prices are
illustrative round numbers, not current list prices.

**Quick index:** caching turned on but cost dashboard doesn't move → Case 1; 429s while every tenant
is under quota → Case 2; 60-second hangs during a provider brownout → Case 3; a small outage becomes a
retry storm → Case 4; semantic cache gives opposite answers → Case 5; ledger total lower than the
provider invoice → Case 6; JSON extraction breaks with no deploy → Case 7.

### Case 1 — Prompt caching "saves nothing"

**Setup.** A support bot sends 50,000 requests a day. Each has 6,000 input tokens, of which 5,000 are
the same system prompt and help-center text, and 300 output tokens. Illustrative prices: $3 per
million input tokens, $0.30 per million cached input tokens, $15 per million output tokens.

**Symptom.** The team turns on provider prompt caching. The provider reports a 90% cache hit rate on
the 5,000-token prefix, but the gateway's cost dashboard still shows $1,125 a day.

**Measurement.** Before caching: 300M input tokens × $3/M = $900, plus 15M output tokens × $15/M =
$225, total $1,125/day. With caching, 50,000 × 0.9 × 5,000 = 225M tokens are cached. Real cost:
75M uncached × $3/M = $225, plus 225M cached × $0.30/M = $67.50, plus $225 output = $517.50/day. The
gateway's `Usage` had no `cached_input_tokens` field, so it priced all 300M input tokens at the full
rate.

**Fix.** Add `cached_input_tokens` to `Usage` and `cached_input_per_1k` to the pricing table (§2.2,
§7.1), and normalize each provider's usage fields so "input" means the same thing everywhere (§3.3).
Dashboard drops from $1,125 to $517.50/day, a 54% saving that was real all along.

**Lesson.** Cost math must track every token category the provider bills differently (§7.4).

### Case 2 — 429s while every tenant is "under quota"

**Setup.** 12 internal tenants share one model deployment with an upstream allocation of 2,000,000
TPM. The platform team gave each tenant 250,000 TPM in the gateway, 3,000,000 in total, assuming they
would never all be busy at once.

**Symptom.** At the Monday 9:00 peak, apps see provider 429s. Every tenant's gateway dashboard says
they are at about 70% of their quota.

**Diagnosis.** 12 × 250,000 × 0.70 = 2,100,000 TPM of demand against 2,000,000 TPM upstream. About
100,000 / 2,100,000 ≈ 4.8% of token volume is refused by the provider. The gateway only checked
per-tenant buckets, never the shared upstream one (§6.5).

**Fix.** Add a shared `(provider, model)` bucket sized to the real allocation, checked in addition to
the per-tenant bucket, and route overflow to a second deployment. Provider 429s drop to near zero;
the requests that are still over capacity get a clear gateway error that names the scope that is full.

**Lesson.** Per-tenant limits that add up to more than the upstream allocation are a promise the
gateway can't keep. Model the shared ceiling explicitly.

### Case 3 — Everyone waits 60 seconds when a provider hangs

**Setup.** A chat product with a 20 s latency target. Fallback chain of 3 providers. Each hop used
the same `total_timeout_s = 60` and there was no first-token timeout.

**Symptom.** During a primary-provider brownout, the provider accepts requests but sends nothing. p99
latency goes from 6 s to 64 s. Users give up before the backup ever answers.

**Diagnosis.** Each request waits the full 60 s on the stuck primary before the chain moves to the
backup, which then takes about 4 s: 60 + 4 = 64 s. The circuit breaker only counts completed
failures, so it trips slowly because each failure takes a minute to happen.

**Fix.** Add `first_token_timeout_s = 10` and give the whole chain one budget divided across hops
(§5.4). Worst case to reach the backup is now 10 s, so brownout p99 is about 10 + 4 = 14 s, inside
the 20 s target. The faster failures also trip the breaker sooner, so most requests skip the
primary entirely.

**Lesson.** A provider that hangs is worse than one that fails fast. Time out on the first token, and
budget the whole chain, not each hop.

### Case 4 — A small outage becomes a retry storm

**Setup.** The SDK makes up to 3 attempts. The gateway tries up to 3 providers, and retries each one up
to 3 times on 429 or timeout.

**Symptom.** A short rate-limit event at the primary provider turns into a 10-minute outage across
all providers, with provider traffic many times higher than normal.

**Diagnosis.** One logical request can become 3 (SDK) × 3 (hops) × 3 (retries per hop) = 27 provider
calls. At 1,000 logical requests per second, that is up to 27,000 calls per second hitting providers
that are already overloaded. The retries themselves keep the 429s going.

**Fix.** The SDK retries only the connection to the gateway, not provider errors the gateway already
handled (§13.2). The gateway allows 2 attempts per hop and respects `Retry-After`. Worst case drops
from 27 to 1 × 3 × 2 = 6 calls per logical request, and the event ends when the provider recovers.

**Lesson.** Retries multiply across layers. Decide which single layer owns provider retries.

### Case 5 — The semantic cache answers the opposite question

**Setup.** A subscription service puts a semantic cache in front of its support bot with
`similarity_threshold = 0.97`. Hit rate is 12%. To save more money, someone lowers it to 0.90 and the
hit rate rises to 31%.

**Symptom.** A user asks "how do I keep my subscription and not cancel it?" and gets step-by-step
cancellation instructions.

**Measurement.** Reviewers sample 500 cache hits at 0.90: 20 (4%) are answers to a different
question. At 0.97, the same review finds 2 in 500 (0.4%).

**Fix.** Restore 0.97, allow the semantic cache only for FAQ-style intents that were evaluated, and
exclude account actions (cancel, refund, delete) entirely (§11.2). Hit rate goes back to 12%.

**Lesson.** "Similar embedding" is not "same meaning". Tune the threshold on real queries and measure
wrong answers, not just hit rate.

### Case 6 — The cost ledger is 13% lower than the invoice

**Setup.** A mobile assistant streams 100,000 answers a day. Each has about 2,000 input tokens and 800
output tokens when completed. Illustrative prices: $2.50/M input, $10/M output. 18% of users close
the app mid-answer, after about 400 output tokens on average.

**Symptom.** The monthly provider invoice is higher than the gateway ledger by a steady 13%.

**Diagnosis.** The gateway recorded cost only after `[DONE]`. A completed stream costs 2,000 ×
$2.50/M + 800 × $10/M = $0.013; a disconnected one still costs 2,000 × $2.50/M + 400 × $10/M = $0.009.
Real daily cost: 82,000 × $0.013 + 18,000 × $0.009 = $1,066 + $162 = $1,228. The ledger saw only the
$1,066, a gap of $162 / $1,228 ≈ 13.2%.

**Fix.** Settle and record in a `finally` block using whatever usage the provider reported (§10.3),
and cancel the upstream request when the client disconnects so the provider stops generating. The
ledger now matches the invoice within rounding.

**Lesson.** Tokens generated are billed whether or not anyone reads them. Settle on every exit path.

### Case 7 — JSON extraction breaks with no deploy

**Setup.** An invoice-extraction service makes 200,000 calls a day through the alias `extract`, which
pointed to a floating model name rather than a dated snapshot.

**Symptom.** One morning, the share of responses that fail JSON validation rises from 0.5% to 4%, with
no code or config change on the team's side.

**Measurement.** Failures go from 200,000 × 0.005 = 1,000 to 200,000 × 0.04 = 8,000 a day. Traces show
the same alias and prompt, but the `gen_ai.response.model` value changed overnight: the provider had
moved the floating name to newer weights.

**Fix.** Pin `extract` to a dated snapshot (§12.1), run the eval suite before any alias change, and
alert when the response model differs from the pinned one. Failures return to about 1,000 a day.

**Lesson.** Floating model names are fine for experiments, not for anything with a quality contract.
