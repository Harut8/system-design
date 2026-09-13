## System Design Task: API Gateway & Rate Limiter

### Problem Statement

Design an **API Gateway with built-in rate limiting** — the single entry point
that every external client, mobile app, partner integration, and internal
microservice traverses before reaching any backend service.

Today, twelve backend teams each expose their own HTTP endpoints directly to the
internet, each wrote their own authentication middleware, each handles rate
limiting differently (or not at all), and one botnet attack against the least-
protected service took down the shared database for every other service. A
partner integrated against a service that renamed its URL in a deploy and broke
silently for two days because there was no contract layer between "what the
client calls" and "where the request goes." Nobody can answer "what is the P99
latency of our public API" because every service logs in a different format.

The gateway fixes this by becoming the **single enforcement layer** where
authentication, rate limiting, request routing, protocol translation, and
observability live. Clients hit one stable surface — consistent URLs, consistent
auth, consistent error format — and the gateway decides which backend handles
each request, enforces per-client rate limits and quotas, rejects malformed or
unauthorized traffic at the edge before it reaches business logic, and emits the
telemetry needed to answer "which client is sending this traffic, how much, and
what does the latency look like" in one dashboard.

This is infrastructure that every API consumer depends on. It sits directly in
the request hot path of every external call the platform serves, so its own
availability, latency overhead, and correctness of rate-limiting decisions are
first-class requirements, not afterthoughts. Assume it will handle everything
from mobile app traffic with unpredictable bursts, to partner integrations with
contracted SLAs, to internal service-to-service calls that should bypass some
(but not all) external-facing policies.

---

### Functional Requirements

1. **Request Routing**

   * Route incoming requests to appropriate backend services based on URL path,
     HTTP method, headers, and query parameters.
   * Support versioned APIs (`/v1/users`, `/v2/users`) routing to different
     backend service versions concurrently.
   * Weighted traffic splitting for canary deployments (e.g., 5% to v2, 95%
     to v1).
   * Path rewriting: the public URL path and the backend path may differ — the
     gateway translates without the backend knowing.
   * Service discovery integration: backend targets resolve dynamically (from a
     service registry or DNS), not from a static config that requires a deploy
     to change.

2. **Authentication & Authorization**

   * Validate API keys, JWT tokens, and OAuth2 bearer tokens at the gateway
     before requests reach backends.
   * Enforce per-endpoint authorization policies: "this API key may call
     `GET /v1/orders` but not `DELETE /v1/orders`."
   * Support both external clients (API key / OAuth) and internal service-to-
     service calls (mTLS, service identity tokens).
   * Token introspection caching: do not call the identity provider on every
     request — cache validated tokens with a bounded TTL that balances
     freshness vs. load on the IdP.

3. **Rate Limiting**

   * Per-client rate limits by API key or authenticated identity (requests/sec,
     requests/min, requests/day).
   * Per-endpoint rate limits independent of client identity (protect specific
     expensive endpoints).
   * Global rate limits per backend service (the gateway must never send more
     traffic than a backend can absorb, regardless of how many clients are
     sending).
   * Tiered rate limits by subscription plan (free: 100 req/min, pro:
     1,000 req/min, enterprise: 10,000 req/min).
   * Burst tolerance: a client at 80 req/min with a 100/min limit should be
     allowed a brief spike to 120/min without immediate rejection, via token-
     bucket or leaky-bucket semantics.
   * Rate-limit headers on every response: `X-RateLimit-Limit`,
     `X-RateLimit-Remaining`, `X-RateLimit-Reset`, per the IETF
     `RateLimit` header draft.
   * `429 Too Many Requests` response with a `Retry-After` header when a
     client exceeds its limit.

4. **Request/Response Transformation**

   * Protocol translation: accept REST from external clients while
     communicating gRPC to internal services (and vice versa).
   * Header injection: add tracing headers (`X-Request-ID`, `X-Correlation-ID`),
     tenant context, and authenticated identity to every forwarded request.
   * Response shaping: strip internal-only fields from responses before they
     reach external clients.
   * Request validation: reject requests that violate the published API schema
     (wrong content-type, missing required fields) at the gateway, saving
     backend resources.

5. **Resilience**

   * Circuit breaker per backend service: stop forwarding traffic to a service
     returning persistent errors, return a cached or degraded response instead.
   * Timeout enforcement per route: the gateway imposes a maximum response time
     for each backend, returning `504 Gateway Timeout` rather than letting
     clients hang.
   * Retry with exponential backoff for idempotent requests (GET, PUT with
     idempotency key) on transient 5xx from backends.
   * Bulkhead isolation: a slow or failing backend must not exhaust the
     gateway's connection pool and starve other, healthy backends.

6. **Observability**

   * Access logs for every request: client identity, endpoint, status code,
     latency, request/response size, rate-limit decision.
   * Distributed tracing: generate a trace ID at the edge and propagate it
     through every backend hop.
   * Metrics: request rate, error rate, latency histograms (P50/P95/P99),
     rate-limit rejection rate, circuit-breaker state — per client, per
     endpoint, per backend.
   * Real-time anomaly detection: spike in 4xx/5xx from a single client or
     endpoint triggers an alert.

7. **Admin & Configuration**

   * Route configuration changeable without gateway redeploy (hot reload from
     a config store, control plane API, or GitOps push).
   * API key and rate-limit plan management via an admin API.
   * IP allowlist/blocklist per route or globally.
   * Configuration versioning and instant rollback.

---

### Non-Functional Requirements

1. **Scale**

   * Sustained: 100,000 requests/sec across all clients and routes.
   * Peak: 300,000 requests/sec during traffic spikes.
   * Concurrent connections: 500,000 open TCP connections at peak.
   * Support at least 10,000 registered API keys and 200+ backend routes.

2. **Latency**

   * Gateway-added overhead (routing decision + auth validation + rate-limit
     check + header injection, excluding backend processing time): **P50 ≤ 2 ms,
     P99 ≤ 10 ms**.
   * Rate-limit decision latency: **≤ 1 ms** P99 for the common case (local
     counter, no cross-node sync needed).
   * Configuration reload: new routes or rate-limit changes effective within
     **5 seconds** of commit.

3. **Availability**

   * Gateway data plane: **99.99%** (≈52 minutes/year downtime).
   * A single backend service failure must **never** take down the gateway or
     affect routing to other services.
   * The rate limiter must function in a degraded mode if its backing store
     (Redis, etc.) is unavailable — fail open or closed per policy, not crash.

4. **Correctness / Consistency**

   * Rate-limit counters must be accurate within a bounded tolerance across
     gateway instances: no client should exceed their configured limit by more
     than the burst allowance through clock skew or replication lag.
   * State the consistency model and tolerable overshoot explicitly (e.g.,
     "≤ 5% overshoot during a 1-second window under split-brain").
   * Authentication decisions must be strongly consistent: a revoked token
     must stop working within the cache TTL, and the TTL must be configurable.

5. **Security**

   * TLS termination at the gateway, mTLS to backends.
   * No plaintext secrets in config files; API keys and signing secrets stored
     in a vault.
   * DDoS mitigation: the gateway itself must survive a volumetric attack that
     exceeds normal traffic by 10x without losing availability for legitimate
     clients.

6. **Operability**

   * Adding a new backend service or API version must not require a gateway
     binary redeploy — config-driven only.
   * Every SLO defined above must be independently measurable from the
     gateway's own telemetry.
   * A single engineer on-call at 3 a.m. must be able to diagnose "why are
     requests to `/v2/orders` returning 502" from the gateway's dashboards
     alone, without SSHing into backend pods.

---

### Constraints and Assumptions

* You do not control backend service availability or response times — design
  for their failure, don't assume it away.
* Clients range from well-behaved mobile apps to aggressive scrapers to partner
  integrations with contracted rate limits — the design must handle all three
  without penalizing the well-behaved.
* Backend services use a mix of protocols (HTTP/1.1, HTTP/2, gRPC) and
  deployment models (Kubernetes pods, serverless functions, legacy VMs).
* The rate limiter must work correctly in a multi-instance, multi-region
  deployment without requiring a strongly-consistent distributed store on the
  request hot path.
* Assume this system will be operated by a platform team of 3-5 engineers
  and must remain debuggable by someone who is not its author.
* The system must work behind existing cloud load balancers (ALB/NLB) — it is
  not the L4 load balancer itself but the L7 application gateway behind one.

---

### What You Should Deliver

1. **Requirement clarification & explicit assumptions.**
2. **Architecture that evolves with scale** — show how the gateway grows from
   a single-node setup for a small API (1K req/sec) to a multi-region,
   multi-instance fleet (100K+ req/sec). Do not start with the full-blown
   version — start with what a 3-person team ships in a sprint, then show
   each tier of complexity.
3. **Rate-limiting design** in depth:
   * Algorithms (token bucket vs. sliding window log vs. sliding window counter
     vs. leaky bucket) — pick one or a combination and justify.
   * Distributed rate limiting across multiple gateway instances — how counters
     stay consistent.
   * Hierarchical limits (global → service → plan → client → endpoint) and
     which applies first.
   * What happens when the rate-limit store is unavailable.
4. **Request routing and service discovery** — how routes are configured, matched,
   and updated without downtime.
5. **Authentication pipeline** — the sequence of checks, caching strategy, and
   what happens on identity-provider failure.
6. **Resilience patterns** — circuit breaker states, timeout budgets, retry
   policy, and bulkhead sizing.
7. **Data model** — the schemas for route config, API keys, rate-limit plans,
   and access logs.
8. **Observability design** — what you trace, measure, and alert on.
9. **Capacity estimates** with arithmetic shown.
10. **Failure walkthroughs** for at least: a backend going fully down, a
    rate-limit store outage, a DDoS attack, a configuration rollout that
    breaks routing, and a client exceeding limits across multiple gateway
    instances simultaneously.
11. **Trade-offs** explicitly called out — what you deliberately did not build
    and what breaks if someone needs it.
12. **Evolution path** from the minimal viable version to the full distributed
    system.

---

### Expectations

* **Do the arithmetic.** Connection pool sizes, rate-limit counter memory,
  Redis throughput for counters, and bandwidth estimates should appear as
  numbers with a derivation, not adjectives.
* **Name concrete mechanisms** — token bucket vs. sliding window, circuit
  breaker states and transitions, consistent hashing for rate-limit sharding,
  Lua scripts for atomic Redis operations — and say what each buys you and
  what it costs.
* **Be precise about guarantees.** "Rate limits are enforced" needs a
  mechanism and a tolerance, not a promise.
* **Show the failure walkthrough.** For each failure class, state exactly what
  the calling client observes and what it should do about it.
* Prefer a design a small platform team can actually operate over one that
  needs its own on-call rotation to understand.
