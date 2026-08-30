## System Design Task: Multi-Provider LLM Gateway

### Problem Statement

Design an **LLM Gateway** — a single internal service that every product team,
agent, and batch job in the company calls instead of talking to OpenAI,
Anthropic, Google, AWS Bedrock, Azure OpenAI, or self-hosted vLLM clusters
directly.

Today, thirty product teams each hold their own provider API keys, each wrote
their own retry logic, each got paged separately when OpenAI had an incident,
and nobody can answer "how much are we spending on Claude this month" without
grepping through a dozen billing dashboards. One team's traffic spike against a
shared Azure OpenAI deployment silently rate-limited a completely unrelated
team. A provider deprecated a model with six weeks' notice and four teams found
out from a 404 in production.

The gateway fixes this by becoming the **one place** where provider diversity,
failure, cost, and quota live. Callers send one request format and get one
response format, regardless of which provider or model actually served it. The
gateway decides — based on cost, latency, quality tier, and live health — which
provider to call, retries and falls back when a provider misbehaves, enforces
per-team budgets and rate limits, and emits the telemetry that lets you answer
"what did request `req_8f3a` cost, which model served it, and how long did the
first token take" in one query.

This is infrastructure that hundreds of services will depend on. It sits
directly in the hot path of every LLM call the company makes, so its own
availability and latency overhead are first-class requirements, not
afterthoughts. Assume it will carry the traffic of everything from a
low-latency chat product to overnight batch summarization jobs to autonomous
agents holding open streaming sessions for minutes at a time.

---

### Functional Requirements

1. **Provider Abstraction**

   * A single unified request schema (messages, system prompt, tools,
     generation parameters) that every caller uses, regardless of destination
     provider.
   * A single unified response schema (content blocks, tool calls, usage,
     stop reason) that every caller receives back, regardless of which
     provider actually generated it.
   * Provider-specific feature mapping: translate the unified schema into each
     provider's native wire format and back — e.g., Anthropic's top-level
     `system` parameter vs. OpenAI's `system` role message vs. Gemini's
     `systemInstruction`; Bedrock's per-model request bodies; Azure OpenAI's
     deployment-name-as-model-id indirection.
   * Tool / function calling normalization: one tool-definition format in, one
     tool-call format out, even though every provider encodes tool schemas,
     parallel tool calls, and forced tool choice differently.
   * Multimodal input normalization (images, PDFs, audio where supported) into
     one content-block format, with graceful, explicit rejection when the
     selected provider/model cannot support what was sent rather than a
     silent downgrade.
   * Passthrough / escape hatch: allow a caller to attach provider-specific
     parameters that bypass normalization for features not yet abstracted,
     without breaking portability of the rest of the request.

2. **Model Capabilities Registry**

   * A capability matrix per (provider, model, version) covering: context
     window (input/output), vision support, tool-use support, JSON /
     structured-output mode, streaming support, max output tokens, supported
     modalities, and knowledge cutoff.
   * Pricing per model: input tokens, output tokens, cached-input tokens,
     batch-API discount rate, and effective date (pricing changes over time
     and old cost records must still resolve against the price in effect when
     the call was made).
   * Deprecation and sunset metadata: announced deprecation date, hard
     shutoff date, and the recommended replacement model.
   * Support both **manual registration** (an operator adds a new model via
     config/PR) and a path to **auto-discovery** (periodically polling
     provider `/models` endpoints to detect new models and flag drift between
     registered and actual capabilities).
   * The registry must be queryable by the routing engine in the hot path
     with sub-millisecond lookup — it cannot be a network call per request.

3. **Routing Engine**

   * Named routing strategies: least-cost, lowest-observed-latency,
     highest-quality-tier, weighted round-robin, and content-based (e.g.,
     route requests containing images only to vision-capable models).
   * **Model equivalence classes**: logical names like `tier-1-reasoning` or
     `fast-cheap-chat` that map to a ranked list of concrete models across
     providers (e.g., `o1`, `claude-opus-4`, `gemini-2.5-pro`), so callers
     depend on a capability tier, not a specific vendor's model string.
   * Geographic / data-residency affinity: some tenants must only be routed to
     providers/regions that satisfy their compliance constraints.
   * A rules DSL or config format that lets routing policy change without a
     code deploy.
   * Support for shadow traffic (mirror a fraction of live requests to a
     candidate model without affecting the response the caller receives) and
     A/B assignment (split traffic between two models and report comparative
     quality/cost/latency).

4. **Streaming**

   * Proxy Server-Sent-Events (or provider-native streaming protocol) from
     the selected provider back to the caller with the unified response
     schema applied incrementally, not just at the end.
   * Backpressure handling: a slow client must not cause the gateway to
     buffer an unbounded amount of an in-flight provider stream in memory.
   * Partial-response handling: define what the caller receives if the
     provider stream fails or times out after some tokens have already been
     sent — the caller has already committed to a partial answer and cannot
     be silently retried without duplicating output.
   * Stream multiplexing: support fan-out use cases (e.g., one logical
     request that the gateway internally races against two models and
     streams back the first to respond) without leaking the losing stream's
     resources.

5. **Retry and Fallback**

   * Per-provider retry policies: which HTTP/error codes are retryable,
     backoff shape, and max attempts — these differ meaningfully between
     providers (e.g., a 429 from OpenAI vs. a Bedrock throttling exception
     carry different semantics and recommended backoff).
   * Fallback chains: if the primary model/provider fails or is unhealthy,
     automatically retry against a defined next choice, respecting the
     equivalence class where fidelity matters.
   * Circuit breakers per (provider, region, model) so that a struggling
     backend is stopped from receiving new traffic before it fails every
     in-flight request, and is re-probed automatically.
   * Idempotency: retries must not double-charge a tenant's budget or
     double-execute a caller-visible side effect (e.g., a tool call that
     itself is not idempotent).
   * A defined **degraded mode**: what the gateway does when *all* providers
     for a requested capability are unavailable — fail fast with a
     structured error, not hang.

6. **Rate Limiting**

   * Per-tenant token budgets (input + output tokens per time window), not
     just request counts.
   * Per-model RPM (requests/min) and TPM (tokens/min) limits that mirror
     each provider's actual account-level quotas.
   * Global, gateway-wide quota management per provider account/API key, so
     the gateway itself never causes the company to get rate-limited or
     banned upstream.
   * Burst allowance (token-bucket style) so a tenant with a bursty workload
     isn't punished for average-case shaping.
   * Fair-share enforcement: one noisy tenant must not be able to starve
     another tenant sharing the same upstream provider quota.

7. **Observability**

   * Per-request distributed tracing: one trace ID per logical request,
     spanning gateway ingress, routing decision, provider call(s), retries,
     and response, correlatable end-to-end.
   * Token usage tracking (input, output, cached) recorded per request, per
     tenant, per model.
   * Latency histograms broken out by phase: routing overhead, time-to-first-
     token (TTFT), inter-token latency, total request time — per provider and
     per model.
   * Error classification: distinguish caller error (bad request), provider
     error (5xx, timeout), rate-limit error, policy rejection (budget/PII),
     and gateway-internal error, each independently alertable.
   * Cost attribution surfaced in the same telemetry stream that latency and
     errors live in, so a single dashboard answers "is this team's traffic
     slow, failing, or expensive."

8. **Cost Management**

   * Real-time cost computation per request from the capabilities registry's
     pricing table, available before the response leaves the gateway.
   * Budget alerts: soft-limit warnings and hard-limit enforcement per
     team/project, configurable per billing period.
   * Cost allocation: every request tagged with team/project/environment so
     spend can be rolled up and charged back.
   * Prompt-caching savings tracking: when a provider's native prompt cache
     is used, record the discount actually realized vs. the non-cached cost,
     so caching ROI is measurable, not assumed.

9. **Caching**

   * Exact-match response cache for identical (model, messages, parameters)
     requests, with a defined TTL and explicit cache-bypass option per
     request.
   * Semantic cache: similarity-based reuse of cached responses for
     near-duplicate prompts (embedding similarity above a threshold), with a
     defined policy for how "close enough" is decided and how staleness risk
     is bounded.
   * Cache key design that correctly accounts for everything that affects the
     output (model version, system prompt, tools, temperature, etc.) so a
     cache hit never silently returns output for a materially different
     request.
   * Cache invalidation: manual purge by key/prefix, and automatic
     invalidation when a model version backing a cached response is
     deprecated.

10. **Security**

    * Provider API key management: keys stored in a secrets vault, never in
      caller-visible config, rotatable without downtime.
    * PII scrubbing: optional pre-flight redaction of caller-supplied content
      before it is logged (not necessarily before it's sent to the provider —
      define the distinction explicitly).
    * Request/response logging policy: configurable per tenant (some tenants'
      traffic may be legally prohibited from full-content logging; others
      require it for audit).
    * Audit trail: who (service identity) sent what (metadata, not
      necessarily full content) to which provider, when, and what it cost —
      retained and queryable for compliance review.

---

### Non-Functional Requirements

1. **Scale**

   * Sustained: 50,000 requests/sec across all tenants and providers.
   * Peak: 120,000 requests/sec.
   * Token throughput: sustained 2B input tokens/hour, 400M output
     tokens/hour across the fleet.
   * Support at least 500 distinct tenants (teams/projects) and 50+
     registered (provider, model) combinations concurrently.
   * Streaming sessions: up to 200,000 concurrent open streams at peak
     (chat UIs, long-running agents).

2. **Latency**

   * Gateway-added overhead (routing decision + provider selection +
     telemetry emission, excluding the provider's own generation time): **P50
     ≤ 10 ms, P99 ≤ 50 ms**.
   * Time-to-first-byte for a cache hit: **P99 ≤ 20 ms**.
   * Streaming: first gateway-relayed chunk must reach the client within **15
     ms** of receiving the first chunk from the provider.
   * Retry/fallback decision latency (detecting a failed attempt and issuing
     the next one) **≤ 100 ms** beyond the failed attempt's own timeout.

3. **Availability**

   * Gateway control plane and routing path: **99.99%** (≈52 minutes/year).
   * A single provider outage must **never** take down the gateway's overall
     availability for capabilities other providers can also serve — this is
     the entire point of the system and must be demonstrable, not assumed.
   * Configuration changes (new model registration, routing rule updates,
     rate limit changes) must roll out with **zero downtime** and be
     revertible within seconds.

4. **Correctness / Consistency**

   * Rate limit and budget enforcement must be consistent across all gateway
     instances — no tenant should be able to exceed budget by fanning
     requests across nodes faster than limit state converges. State the
     staleness bound you accept.
   * Cost records must be **exactly-once** per successfully completed
     request, including across retries and failovers — no double-billing, no
     silent gaps.

5. **Durability**

   * Every completed request's usage/cost record must be durably persisted
     before being considered final, survivable across a single node/AZ loss.
   * Audit logs retained per the tenant's compliance requirement (default 1
     year, extendable per tenant).

6. **Operability**

   * Onboarding a new provider or model must not require a redeploy of the
     gateway binary — config/registry driven.
   * Every SLO in this document must be independently measurable from the
     gateway's own telemetry.

---

### Constraints and Assumptions

* You do not control the upstream providers' availability, latency, or rate
  limit behavior — design for their failure, don't assume it away.
* Provider pricing and capabilities change over time (new models, deprecated
  models, price changes); the design must not hard-code these as constants.
* Some tenants have strict data-residency or PII constraints that restrict
  which providers/regions may process their requests.
* Callers include both interactive, latency-sensitive products and
  long-running batch/agentic workloads with very different tolerance for
  retries and queuing — the design should not force one class to pay for the
  other's requirements.
* Assume this system will be operated by a platform team of 4-6 engineers and
  must remain debuggable at 3 a.m. by someone who is not its author.

---

### What You Should Deliver

1. Requirement clarification and explicit assumptions.
2. High-level architecture: every major component, the request path through
   them, and the split between control plane and data plane.
3. The unified request/response schema, with concrete examples of how it maps
   to at least three different providers.
4. Provider adapter design and how a new provider is added.
5. The model capabilities registry's data model and how routing consumes it.
6. The routing engine: strategies, rules format, equivalence classes, and how
   a routing decision is actually made per request.
7. Streaming architecture end-to-end, including backpressure and failure
   handling.
8. Retry, fallback, and circuit-breaker design, with state machines.
9. Rate limiting and budget enforcement design, including the hierarchy
   (global → provider → tenant → user) and how limits stay consistent across
   nodes.
10. Caching design (exact and semantic) including cache key construction and
    invalidation.
11. Cost engine design: how cost is computed, attributed, and enforced in
    real time.
12. Observability: what you trace, what you measure, and what you'd alert on.
13. Security design: credential handling, PII policy, audit trail.
14. Capacity estimates with the arithmetic shown, not just the answer.
15. Failure walkthroughs for at least: a provider going fully down, a
    provider silently degrading (slow but 200 OK), rate-limit exhaustion
    mid-stream, and a budget being exceeded mid-request.
16. Trade-offs explicitly called out — what you deliberately did not build,
    and what breaks if a team needs it anyway.
17. An evolution path from a minimal viable version to the full system.

---

### Expectations

* **Do the arithmetic.** Throughput, token volume, connection pool sizes, and
  cache hit-rate assumptions should appear as numbers with a derivation, not
  adjectives.
* **Name concrete mechanisms** — token bucket vs. sliding window, circuit
  breaker states, exponential backoff with jitter, SSE vs. chunked transfer —
  and say what each buys you and what it costs.
* **Be precise about guarantees.** "Exactly-once billing" and "no double
  charging" need a mechanism, not a promise.
* **Show the failure walkthrough.** For each failure class, state exactly
  what the calling service observes and what it should do about it.
* Prefer a design a small platform team can actually operate over one that
  needs its own on-call rotation to understand.
