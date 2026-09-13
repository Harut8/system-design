# API Gateway & Rate Limiter: Design Document

> Solution to [`tasks/api-gateway-rate-limiter.md`](../tasks/api-gateway-rate-limiter.md).

### Prerequisites and Learning Resources

Before or alongside this document, study these deep-dive chapters from the curriculum:

| Topic | Resource | Why |
|-------|----------|-----|
| Caching strategies | [`distributed-systems/08-caching-strategies-and-patterns.md`](../distributed-systems/08-caching-strategies-and-patterns.md) | Token caching, response caching, rate-limit counter stores |
| Consistent hashing | [`distributed-systems/10-sharding-and-consistent-hashing.md`](../distributed-systems/10-sharding-and-consistent-hashing.md) | Distributing rate-limit counters across nodes |
| Resilience patterns | [`distributed-systems/33-resilience-patterns-circuit-breakers.md`](../distributed-systems/33-resilience-patterns-circuit-breakers.md) | Circuit breakers, bulkheads, retries — the gateway's core defense |
| Load control | [`distributed-systems/34-adaptive-load-control-and-backpressure.md`](../distributed-systems/34-adaptive-load-control-and-backpressure.md) | Backpressure, load shedding, adaptive concurrency limits |
| Networking protocols | [`distributed-systems/17-networking-protocols-and-communication.md`](../distributed-systems/17-networking-protocols-and-communication.md) | HTTP/2, gRPC, TLS — the transport layer the gateway manages |
| SLOs and error budgets | [`distributed-systems/35-reliability-math-slos-and-error-budgets.md`](../distributed-systems/35-reliability-math-slos-and-error-budgets.md) | How to define and measure gateway availability |

---

## Table of Contents

1. [Requirements Clarification](#1-requirements-clarification)
2. [Architecture by Scale (1K → 100K+ RPS)](#2-architecture-by-scale-1k--100k-rps)
3. [Rate-Limiting Design](#3-rate-limiting-design)
4. [Request Routing & Service Discovery](#4-request-routing--service-discovery)
5. [Authentication Pipeline](#5-authentication-pipeline)
6. [Request/Response Transformation](#6-requestresponse-transformation)
7. [Resilience Patterns](#7-resilience-patterns)
8. [Observability](#8-observability)
9. [Security](#9-security)
10. [Data Models](#10-data-models)
11. [Capacity Estimates](#11-capacity-estimates)
12. [Failure Walkthroughs](#12-failure-walkthroughs)
13. [Trade-offs](#13-trade-offs)
14. [Evolution Path](#14-evolution-path)

---

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|---|---|---|
| Scope | Does the gateway terminate WebSocket connections? | Yes — it upgrades HTTP to WS and proxies frames, applying auth on the initial handshake and rate limiting on message count/byte volume |
| Consistency | Must rate limits be exactly enforced across all gateway nodes? | No — we accept **≤5% overshoot** during a single window under split-brain conditions. Exact enforcement would require a synchronous distributed counter on every request, adding 2-5ms P99 |
| Consistency | Can a revoked API key still work briefly? | Yes — up to the token-cache TTL (default 30s, configurable down to 0 for enterprise keys). Tradeoff: IdP load vs. revocation latency |
| Latency | Is the 10ms P99 budget for auth + routing + rate-limit combined? | Yes — everything the gateway adds before proxying to the backend. Auth cache hit + local rate-limit check + route match must all fit |
| Availability | What happens if the rate-limit store (Redis) is unavailable? | **Fail open** by default: requests pass without rate-limit enforcement. Configurable per-plan: enterprise plans may fail closed to avoid billing overages |
| Protocols | Must the gateway support gRPC natively? | Yes — both as ingress (gRPC clients) and as egress (gRPC backends behind REST endpoints) |
| DDoS | Does the gateway handle L3/L4 DDoS? | No — that's the upstream LB / cloud provider's responsibility. The gateway handles L7 abuse (credential stuffing, API scraping, application-layer floods) |
| Multi-region | Active-active or active-passive? | Active-active with geo-routing at the DNS/LB layer. Rate-limit counters are per-region by default, with optional cross-region sync for global limits |

### Key Assumptions

1. **The gateway is L7 infrastructure, not L4.** It sits behind a cloud load balancer (ALB/NLB) that handles TCP connection management and basic health checks.
2. **Backends fail independently and often.** A gateway that assumes healthy backends is worse than no gateway at all — it hides failures instead of isolating them.
3. **Rate limiting is a spectrum, not a binary.** Different clients (free-tier scrapers vs. enterprise partners with SLAs) need different enforcement semantics, granularity, and consequences for exceeding limits.
4. **Configuration is code.** Route definitions, rate-limit plans, and auth policies are versioned artifacts, not live-edited database rows — they go through code review and can be rolled back.
5. **Observability is the product.** A gateway nobody can debug at 3 a.m. is a liability, not infrastructure. Every decision the gateway makes (route match, rate-limit check, circuit-breaker trip) must be traceable.

### What We Are Explicitly Not Building (v1)

- Not a full API management platform with developer portal, SDK generation, or usage billing. Those consume the gateway's telemetry but live separately.
- Not a WAF (Web Application Firewall) — though the gateway does basic request validation and IP blocking, deep packet inspection and OWASP rule evaluation belong in a dedicated WAF layer.
- Not a service mesh sidecar. The gateway is an edge/ingress component; east-west service-to-service traffic may use a mesh (Envoy, Linkerd) that the gateway routes into, not replaces.
- Not a CDN. Static asset caching and edge delivery are handled by CloudFront / Cloudflare in front of the gateway.

---

## 2. Architecture by Scale (1K → 100K+ RPS)

> **Start simple, scale when needed.** Each tier builds on the previous one. Don't over-engineer early.

### Architecture Comparison Matrix

| Scale | RPS | Clients | Backends | Architecture | Team | Monthly Infra Cost |
|-------|-----|---------|----------|--------------|------|--------------------|
| **1K RPS** | 1,000 | 100 | 5 | Single-node Nginx/Envoy + local Redis | 1-2 | $200-500 |
| **10K RPS** | 10,000 | 1,000 | 20 | Multi-node gateway + Redis Cluster | 3-4 | $2-5K |
| **50K RPS** | 50,000 | 5,000 | 50 | Gateway fleet + distributed rate-limit + control plane | 4-6 | $15-30K |
| **100K+ RPS** | 100,000+ | 10,000+ | 100+ | Multi-region fleet + global rate-limit sync + edge PoPs | 6-10 | $50-100K |

---

### 2.1 Tier 1: 1K RPS (Early Product / Small API)

**Scale Profile:**
```
Registered API keys:    100
Requests/sec:           ~1,000 (peak ~3,000)
Backend services:       5
Concurrent connections: ~5,000
Rate-limit decisions:   ~1,000/sec
```

**Architecture: Single-Node Gateway**

```
                        ┌──────────────┐
    Clients ──────────▶ │   Nginx /    │ ──────▶ Backend A (users)
     (TLS)              │   OpenResty  │ ──────▶ Backend B (orders)
                        │              │ ──────▶ Backend C (payments)
                        │  + Lua rate  │
                        │    limiter   │
                        │  + local     │
                        │    Redis     │
                        └──────────────┘
```

**Key Decisions:**

| Decision | Choice | Why |
|----------|--------|-----|
| Gateway runtime | **Nginx + OpenResty (Lua)** or **Envoy** | Battle-tested, high single-node throughput, Lua scripting for custom logic |
| Rate-limit store | **Local Redis** on the same host | Sub-millisecond latency, no network hop, single instance handles 100K+ ops/sec |
| Rate-limit algorithm | **Token bucket** per API key | Simple, allows bursts naturally, one Redis key per client |
| Auth | **API key lookup** in Redis | Hash the key, look up the plan and permissions — fast enough at this scale |
| Config | **Nginx config files + reload** | `nginx -s reload` for zero-downtime config changes |
| TLS | **Let's Encrypt + certbot** | Free, automated renewal |

**Rate-Limit Implementation (Lua + Redis):**

```lua
-- Token bucket in Redis via Lua script (atomic)
local key = "rl:" .. api_key
local now = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])        -- tokens/sec
local capacity = tonumber(ARGV[3])    -- max burst
local requested = tonumber(ARGV[4])   -- tokens to consume (usually 1)

local bucket = redis.call("HMGET", key, "tokens", "last_refill")
local tokens = tonumber(bucket[1]) or capacity
local last_refill = tonumber(bucket[2]) or now

-- Refill tokens based on elapsed time
local elapsed = math.max(0, now - last_refill)
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
end

redis.call("HMSET", key, "tokens", tokens, "last_refill", now)
redis.call("EXPIRE", key, math.ceil(capacity / rate) * 2)

return { allowed, tokens }  -- {1=allowed/0=rejected, remaining tokens}
```

**Why This Works at 1K RPS:**

- Single Nginx worker handles 10K+ RPS — we have 10x headroom.
- Local Redis: ~0.1ms per rate-limit check, no network hop.
- Total gateway overhead: **< 1ms P99**.
- One engineer can operate this. Config is a single nginx.conf + a Lua file.

**What Breaks at 5K+ RPS:** Single node is a SPOF. Redis on the same host means a host failure loses both gateway and rate-limit state. Config changes require SSH access.

---

### 2.2 Tier 2: 10K RPS (Growing Product)

**Scale Profile:**
```
Registered API keys:    1,000
Requests/sec:           ~10,000 (peak ~30,000)
Backend services:       20
Concurrent connections: ~50,000
Rate-limit decisions:   ~10,000/sec
```

**Architecture: Multi-Node Gateway + Redis Cluster**

```
                   ┌─────────────────────────────────────────────────┐
                   │              Cloud Load Balancer (ALB)           │
                   └──────────┬──────────────┬──────────────┬────────┘
                              │              │              │
                        ┌─────▼────┐   ┌─────▼────┐  ┌─────▼────┐
                        │ Gateway  │   │ Gateway  │  │ Gateway  │
                        │ Node 1   │   │ Node 2   │  │ Node 3   │
                        │ (Envoy / │   │ (Envoy / │  │ (Envoy / │
                        │  custom) │   │  custom) │  │  custom) │
                        └────┬─────┘   └────┬─────┘  └────┬─────┘
                             │              │              │
                        ┌────▼──────────────▼──────────────▼─────┐
                        │         Redis Cluster (3 nodes)         │
                        │   rate-limit counters + token cache     │
                        └────────────────────────────────────────┘
                             │
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
     Backend services  (discovered via Consul / K8s DNS)
```

**What Changes from Tier 1:**

| Component | Tier 1 | Tier 2 | Why |
|-----------|--------|--------|-----|
| Gateway nodes | 1 | 3+ behind ALB | Eliminate SPOF, horizontal scale |
| Rate-limit store | Local Redis | **Redis Cluster** (3 nodes) | Shared state across gateway instances |
| Service discovery | Static upstream config | **Consul / K8s DNS** | Backends scale independently; no config change per backend deploy |
| Config management | Config files + SSH | **Control plane API** or GitOps (Consul KV / ConfigMap) | No SSH, auditable changes |
| Auth cache | Local Redis | **Redis Cluster** (shared) | Consistent token revocation across nodes |
| Health checks | None | **Active health checks** per backend | Detect failures before clients do |

**Rate-Limit Algorithm Upgrade: Sliding Window Counter**

Token bucket works for single-client limits, but at this scale we need hierarchical limits. We use **sliding window counters** — a hybrid of fixed-window and sliding-window-log that uses O(1) memory:

```
Window: [t0, t0+60s]
Current window count: 42 requests
Previous window count: 78 requests
Elapsed fraction of current window: 0.3 (18 seconds into 60-second window)

Weighted count = prev * (1 - 0.3) + current = 78 * 0.7 + 42 = 96.6
Limit: 100/min → allow (96.6 < 100)
```

Redis implementation — two keys per (client, window):

```
rl:{api_key}:{window_start}       → count (integer)
rl:{api_key}:{window_start - 60}  → count (integer, previous window)
```

Single `MULTI`/`EXEC` or Lua script increments the current-window counter and reads both to compute the weighted count. TTL = 2 × window size.

**Why Sliding Window Counter Over Token Bucket:**

| Algorithm | Memory | Burst Control | Accuracy | Complexity |
|-----------|--------|---------------|----------|------------|
| Token bucket | 2 fields/key | Natural burst | Approximate | Low |
| Fixed window | 1 counter/key | Boundary burst (2x) | Exact within window | Lowest |
| Sliding window log | O(n) per client | Exact | Exact | High memory |
| **Sliding window counter** | **2 counters/key** | **Smoothed boundary** | **~99.7% accurate** | **Low** |

Sliding window counter gives us near-exact rate limiting with O(1) memory — the right tradeoff for a growing product.

**Rate-Limit Hierarchy:**

```
Global gateway limit          → protects the gateway fleet itself
  └── Per-backend limit       → protects each backend service
        └── Per-plan limit    → enforces subscription tiers
              └── Per-key limit → enforces individual client limits
                    └── Per-endpoint limit → protects expensive operations
```

Evaluation order: check from **top to bottom**, reject on the **first violation**. Each level has its own window and counter.

**Config Example (YAML):**

```yaml
rate_limits:
  global:
    requests_per_second: 50000    # gateway fleet capacity

  backends:
    orders-service:
      requests_per_second: 5000   # what this backend can absorb

  plans:
    free:
      requests_per_minute: 100
      requests_per_day: 10000
      burst_multiplier: 1.2       # 20% burst allowed
    pro:
      requests_per_minute: 1000
      requests_per_day: 100000
      burst_multiplier: 1.5
    enterprise:
      requests_per_minute: 10000
      requests_per_day: 1000000
      burst_multiplier: 2.0

  endpoints:
    "POST /v1/payments":
      requests_per_minute: 50     # per-key limit on expensive endpoint
    "GET /v1/search":
      requests_per_minute: 200    # search is expensive
```

---

### 2.3 Tier 3: 50K RPS (Scale-Up)

**Scale Profile:**
```
Registered API keys:    5,000
Requests/sec:           ~50,000 (peak ~120,000)
Backend services:       50
Concurrent connections: ~250,000
Rate-limit decisions:   ~50,000/sec
```

**Architecture: Gateway Fleet + Distributed Rate-Limit + Control Plane**

```
 ┌─────────────────────────────────────────────────────────────────┐
 │                        Control Plane                             │
 │  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
 │  │  Admin API   │  │  Config DB   │  │  Push-based Config     │ │
 │  │ (route CRUD, │  │  (Postgres)  │  │  Distributor (etcd     │ │
 │  │  key mgmt,   │  │              │  │  watch / xDS / gRPC    │ │
 │  │  plan mgmt)  │  │              │  │  push to gateways)     │ │
 │  └─────────────┘  └──────────────┘  └────────────────────────┘ │
 └──────────────────────────┬──────────────────────────────────────┘
                             │ config push (<1s propagation)
 ┌───────────────────────────▼─────────────────────────────────────┐
 │                    Data Plane (Gateway Fleet)                    │
 │                                                                  │
 │  ┌──────────┐  ┌──────────┐  ┌──────────┐       ┌──────────┐  │
 │  │ Gateway  │  │ Gateway  │  │ Gateway  │  ...  │ Gateway  │  │
 │  │ Pod 1    │  │ Pod 2    │  │ Pod 3    │       │ Pod N    │  │
 │  │          │  │          │  │          │       │          │  │
 │  │ ┌──────┐ │  │ ┌──────┐ │  │ ┌──────┐ │       │ ┌──────┐ │  │
 │  │ │Local │ │  │ │Local │ │  │ │Local │ │       │ │Local │ │  │
 │  │ │Cache │ │  │ │Cache │ │  │ │Cache │ │       │ │Cache │ │  │
 │  │ └──────┘ │  │ └──────┘ │  │ └──────┘ │       │ └──────┘ │  │
 │  └────┬─────┘  └────┬─────┘  └────┬─────┘       └────┬─────┘  │
 │       │              │              │                   │        │
 │  ┌────▼──────────────▼──────────────▼───────────────────▼─────┐ │
 │  │              Redis Cluster (6 nodes, 3 primary + 3 replica) │ │
 │  │              Rate-limit counters + token cache               │ │
 │  └──────────────────────────────────────────────────────────────┘│
 └──────────────────────────────────────────────────────────────────┘
                             │
     ┌───────────────────────┼───────────────────────┐
     ▼                       ▼                       ▼
  Backend cluster A     Backend cluster B      Backend cluster C
  (via K8s service)     (via Consul)           (via DNS)
```

**What Changes from Tier 2:**

| Component | Tier 2 | Tier 3 | Why |
|-----------|--------|--------|-----|
| Control plane | Config files / KV store | **Dedicated Admin API + config push** | Config changes are an API call, not a commit. Audit trail, RBAC, instant rollback |
| Gateway fleet | 3 nodes | **10-20 pods**, auto-scaled by HPA on CPU and connection count | Handle 50K+ RPS with headroom |
| Rate-limit counters | Redis Cluster, every check hits Redis | **Two-tier: local counter + periodic Redis sync** | Cut Redis traffic by 80%, reduce P99 latency |
| Auth | Redis token cache | **Local LRU cache (10K entries) + Redis fallback** | Avoid Redis round-trip for hot tokens |
| Circuit breakers | Basic timeout | **Per-backend circuit breaker with half-open probing** | Faster failure isolation, controlled recovery |
| Observability | Access logs + basic metrics | **OpenTelemetry traces + Prometheus metrics + structured logs** | Correlate a single request across auth → route → backend → response |

**Two-Tier Rate Limiting (Local + Redis Sync):**

At 50K RPS, every rate-limit check hitting Redis means 50K Redis ops/sec just for rate limits. Instead:

```
┌─────────────────────────────────────────────────┐
│                 Gateway Node                     │
│                                                   │
│  Request → Local counter check (in-memory)       │
│                │                                  │
│         ┌──────▼──────┐                          │
│         │ Under local │── Yes → Allow            │
│         │  threshold? │                          │
│         └──────┬──────┘                          │
│                │ No (near limit)                  │
│         ┌──────▼──────┐                          │
│         │ Check Redis │── Under → Allow          │
│         │  (global)   │                          │
│         └──────┬──────┘                          │
│                │ Over                             │
│                └──── Reject (429)                 │
│                                                   │
│  Background: sync local counters to Redis every  │
│  100ms or every 50 requests, whichever comes     │
│  first                                            │
└─────────────────────────────────────────────────┘
```

**How it works:**

1. Each gateway node maintains an in-memory counter per (client, window).
2. For a client with a 1,000 req/min limit across 10 gateway nodes, each node gets an **allocation** of 100 req/min.
3. Under the local allocation: allow immediately (0ms latency).
4. Near/over the local allocation: check Redis for the global count (1ms latency).
5. Every 100ms, each node flushes its local deltas to Redis via `INCRBY`.

**Accuracy:** Worst case, each node overshoots by its allocation increment (50 requests if syncing every 50) × N nodes = 500 request overshoot on a 1,000/min limit = 50% overshoot. Too high.

**Fix: Adaptive allocation.** When a client is at 80%+ of their limit on any node, that node switches to **Redis-direct mode** (every request checks Redis) for that client. Only low-volume clients stay in local-only mode.

```python
def check_rate_limit(api_key: str, limit: RateLimit) -> bool:
    local = local_counters.get(api_key, 0)
    utilization = local / (limit.per_minute / num_nodes)

    if utilization < 0.8:
        # Fast path: local check only
        local_counters[api_key] = local + 1
        return True
    else:
        # Near limit: authoritative Redis check
        global_count = redis.eval(SLIDING_WINDOW_SCRIPT, api_key)
        return global_count < limit.per_minute
```

Result: **< 5% overshoot** with **80% of requests never touching Redis**.

---

### 2.4 Tier 4: 100K+ RPS (Multi-Region)

**Scale Profile:**
```
Registered API keys:    10,000+
Requests/sec:           ~100,000 (peak ~300,000)
Backend services:       100+
Concurrent connections: ~500,000
Regions:                3 (US-East, EU-West, AP-Southeast)
Rate-limit decisions:   ~100,000/sec
```

**Architecture: Multi-Region Fleet + Edge PoPs**

```
                    ┌──────────────────────┐
                    │    Global DNS         │
                    │  (Route53 / latency   │
                    │   -based routing)     │
                    └──────────┬────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                     ▼
   ┌──────────────┐    ┌──────────────┐     ┌──────────────┐
   │  US-East     │    │  EU-West     │     │  AP-South    │
   │  Region      │    │  Region      │     │  Region      │
   │              │    │              │     │              │
   │ ┌──────────┐ │    │ ┌──────────┐ │     │ ┌──────────┐ │
   │ │ Gateway  │ │    │ │ Gateway  │ │     │ │ Gateway  │ │
   │ │ Fleet    │ │    │ │ Fleet    │ │     │ │ Fleet    │ │
   │ │ (20 pods)│ │    │ │ (15 pods)│ │     │ │ (10 pods)│ │
   │ └────┬─────┘ │    │ └────┬─────┘ │     │ └────┬─────┘ │
   │      │       │    │      │       │     │      │       │
   │ ┌────▼─────┐ │    │ ┌────▼─────┐ │     │ ┌────▼─────┐ │
   │ │ Redis    │ │    │ │ Redis    │ │     │ │ Redis    │ │
   │ │ Cluster  │ │    │ │ Cluster  │ │     │ │ Cluster  │ │
   │ └────┬─────┘ │    │ └────┬─────┘ │     │ └────┬─────┘ │
   └──────┼───────┘    └──────┼───────┘     └──────┼───────┘
          │                   │                     │
          └─────── Cross-region sync ───────────────┘
                  (async, every 1-5 seconds)
```

**What Changes from Tier 3:**

| Component | Tier 3 | Tier 4 | Why |
|-----------|--------|--------|-----|
| Deployment | Single region | **3 active regions** behind geo-DNS | Latency (<50ms to nearest PoP), compliance (EU data stays in EU), blast-radius isolation |
| Rate-limit counters | Single Redis Cluster | **Per-region Redis + async cross-region sync** | A cross-region Redis call adds 50-200ms — unacceptable on the hot path |
| Config distribution | etcd watch / gRPC push | **Global control plane + per-region replicas** | Config changes propagate to all regions within 5s |
| Circuit breakers | Per-backend | **Per-(backend, region)** | A backend degraded in US-East may be fine in EU-West |
| DDoS defense | Basic rate limiting | **Edge-level IP reputation + adaptive throttling** | Volumetric attacks get dropped before they reach the gateway fleet |

**Global Rate Limiting Across Regions:**

For a client with a global 10,000 req/min limit, split across 3 regions:

**Option A: Static allocation.** Assign 40% / 30% / 30% to US / EU / AP based on historical traffic distribution. Simple, but a traffic shift (e.g., EU marketing campaign) causes rejections in EU while US allocation goes unused.

**Option B: Async sync with borrowing (chosen).** Each region gets an initial allocation (10K / 3 ≈ 3,333/min). Every 1-5 seconds, regions publish their used counts to a lightweight global aggregator (a separate Redis instance or a small coordination service). When a region's local allocation is exhausted, it **borrows** from regions with surplus.

```
Region allocations (10,000/min global):
  US-East:  4,000/min (base) — currently using 3,800
  EU-West:  3,000/min (base) — currently using 1,200
  AP-South: 3,000/min (base) — currently using 2,500

EU has surplus: 3,000 - 1,200 = 1,800 unused
US is near limit: request 500 from EU
→ EU allocation becomes 2,500, US becomes 4,500

Sync latency: 1-5 seconds
Overshoot risk during sync gap: ≤ 5% (500 requests on a 10K/min limit)
```

**Multi-Region Auth:**

Token validation cache is per-region (no cross-region cache sharing — latency). Token revocation is pushed to all regions via the control plane's event bus (Kafka / SNS):

```
User revokes API key → Control plane writes to DB
                     → Publishes revocation event to all regions
                     → Each region's gateway invalidates its local cache
                     → Worst-case propagation: ~5 seconds
```

---

## 3. Rate-Limiting Design

### Algorithm Deep Dive

We use **sliding window counter** as the primary algorithm, with **token bucket** for burst-tolerant endpoints:

#### Sliding Window Counter

```
Time: |----prev window (60s)----||----current window (60s)----|
                                       ^ now (40% into current)

Rate = prev_count × (1 - 0.4) + current_count
     = prev_count × 0.6 + current_count
```

**Redis Implementation (Lua script, atomic):**

```lua
local key_prefix = KEYS[1]              -- "rl:{client_id}"
local now = tonumber(ARGV[1])           -- current timestamp (ms)
local window = tonumber(ARGV[2])        -- window size (ms)
local limit = tonumber(ARGV[3])         -- max requests per window

local current_window = math.floor(now / window) * window
local previous_window = current_window - window

local current_key = key_prefix .. ":" .. current_window
local previous_key = key_prefix .. ":" .. previous_window

local current_count = tonumber(redis.call("GET", current_key) or "0")
local previous_count = tonumber(redis.call("GET", previous_key) or "0")

local elapsed_ratio = (now - current_window) / window
local weighted_count = previous_count * (1 - elapsed_ratio) + current_count

if weighted_count >= limit then
    return {0, math.ceil(limit - weighted_count), 0}  -- rejected
end

redis.call("INCR", current_key)
redis.call("PEXPIRE", current_key, window * 2)

return {1, math.ceil(limit - weighted_count - 1), current_window + window - now}
-- {allowed, remaining, reset_ms}
```

**Memory per client:** 2 Redis keys × ~50 bytes = 100 bytes per (client, window). For 10K clients × 5 window types (sec, min, hour, day, endpoint) = 50K keys = ~5MB. Negligible.

#### Token Bucket (for burst-tolerant endpoints)

Used where we want to allow bursts while enforcing an average rate. Each bucket has:

- **capacity** (max tokens / max burst size)
- **refill_rate** (tokens added per second)

```
Client: api_key_123
Plan: Pro (1,000 req/min → 16.67/sec refill, capacity: 50 burst)

State: {tokens: 50, last_refill: 1694000000.000}
Request arrives at 1694000000.500
  elapsed = 0.5s → refill 8.33 tokens → tokens = min(50, 50 + 8.33) = 50
  consume 1 → tokens = 49 → allow

50 requests arrive in 100ms burst:
  all 50 consumed → tokens = 0 → next request rejected until refill
  At 16.67/sec, 1 token available in ~60ms
```

### Rate-Limit Store Failure Handling

```
Redis healthy?
  │
  ├── Yes → Normal operation (sliding window counter)
  │
  └── No → Degraded mode
              │
              ├── Plan = free → FAIL OPEN (allow, but local-only counter)
              │                  Risk: free-tier abuse during outage
              │
              ├── Plan = pro  → FAIL OPEN with stricter local limit
              │                  (50% of normal limit, local counter only)
              │
              └── Plan = enterprise → FAIL CLOSED (reject with 503)
                                      Enterprise SLAs include billing
                                      guarantees; allowing unmetered
                                      traffic violates the contract
```

---

## 4. Request Routing & Service Discovery

### Route Matching

Routes are stored as a prefix tree (trie) for O(path_length) matching:

```
/v1
  /users             → user-service (GET, POST)
  /users/:id         → user-service (GET, PUT, DELETE)
  /orders            → order-service (GET, POST)
  /orders/:id        → order-service (GET, PUT)
  /payments          → payment-service (POST)
  /search            → search-service (GET)
/v2
  /users             → user-service-v2 (GET, POST)  [canary: 10%]
  /users/:id         → user-service-v2 (GET, PUT, DELETE)
```

### Route Configuration Schema

```yaml
routes:
  - match:
      path_prefix: "/v1/users"
      methods: ["GET", "POST"]
      headers:
        x-api-version: "2024-01"    # optional header match
    route:
      backend: user-service
      timeout: 5s
      retry:
        attempts: 2
        on: ["5xx", "connect-failure"]
      rate_limit_override:
        per_key_per_minute: 500
    transforms:
      request:
        rewrite_path: "/api/users"  # backend sees /api/users
        add_headers:
          x-gateway-region: "us-east-1"
      response:
        remove_headers: ["x-internal-trace"]

  - match:
      path_prefix: "/v2/users"
      methods: ["GET", "POST"]
    route:
      traffic_split:
        - backend: user-service-v2
          weight: 10
        - backend: user-service
          weight: 90
      timeout: 5s
```

### Service Discovery Integration

```
Route config says: backend = "order-service"
                         │
         ┌───────────────┼───────────────┐
         ▼               ▼               ▼
    K8s DNS         Consul lookup    Static IP list
  order-service.    order-service.   (legacy VMs)
  default.svc.     service.consul
  cluster.local

Gateway resolves backend to a set of healthy endpoints,
picks one via least-connections or round-robin.
```

Endpoint health is maintained via **active health checks** (periodic HTTP pings) combined with **passive health checks** (tracking 5xx rates from real traffic). An endpoint is ejected from the pool after N consecutive failures and re-admitted after passing M health checks.

---

## 5. Authentication Pipeline

```
Request arrives
    │
    ▼
┌──────────────────────────┐
│ 1. Extract credentials   │
│    (API key from header,  │
│     JWT from Bearer,      │
│     mTLS client cert)     │
└───────────┬──────────────┘
            │
            ▼
┌──────────────────────────┐
│ 2. Local LRU cache check │  ← hit rate: ~95% at steady state
│    (10K entries, 30s TTL) │
└─────┬────────────┬───────┘
      │ miss       │ hit
      ▼            ▼
┌─────────────┐   ┌─────────────────────┐
│ 3. Redis    │   │ Return cached        │
│    cache    │   │ identity + plan      │
│    check    │   └─────────────────────┘
└──┬──────┬───┘
   │miss  │hit
   ▼      ▼
┌─────────────┐  ┌──────────────────────┐
│ 4. IdP call │  │ Cache in LRU + Redis │
│ (Postgres / │  │ Return identity      │
│  Auth0 /    │  └──────────────────────┘
│  Keycloak)  │
└──────┬──────┘
       │
       ▼
┌──────────────────────────┐
│ 5. Cache result          │
│    LRU: 30s TTL          │
│    Redis: 60s TTL        │
│    (revoked keys get     │
│     negative-cached      │
│     for 5min to prevent  │
│     IdP spam from        │
│     attackers)           │
└──────────────────────────┘
```

**Token Revocation Propagation:**

When an API key is revoked:
1. Control plane writes revocation to the config DB.
2. Publishes a `key_revoked` event to all gateway nodes.
3. Each node removes the key from its local LRU cache.
4. Redis entry is deleted or replaced with a negative-cache entry.
5. Worst case: a request on a node that missed the event uses the stale LRU cache entry for up to 30s.

For enterprise keys where instant revocation is required: set `cache_ttl: 0` on the key's config, forcing every request to check Redis (not the IdP — Redis is fast enough).

---

## 6. Request/Response Transformation

### Protocol Translation (REST ↔ gRPC)

```
External client           Gateway                  Backend
    │                        │                        │
    │  POST /v1/orders       │                        │
    │  Content-Type: json    │                        │
    │  {"item": "abc"}       │                        │
    │ ────────────────────▶  │                        │
    │                        │  gRPC CreateOrder()    │
    │                        │  OrderRequest{         │
    │                        │    item: "abc"         │
    │                        │  }                     │
    │                        │ ────────────────────▶  │
    │                        │                        │
    │                        │  ◀──── OrderResponse{  │
    │                        │    id: "ord_123"       │
    │                        │  }                     │
    │                        │                        │
    │  ◀──── 201 Created     │                        │
    │  {"id": "ord_123"}     │                        │
```

The mapping between REST and gRPC is defined per-route via **proto-to-JSON transcoding rules** (similar to gRPC-Gateway's approach). The gateway compiles `.proto` files at config-load time and uses them to translate bidirectionally.

### Header Injection

Every proxied request gets:

```
X-Request-ID: req_a1b2c3d4           # unique per request (UUID v7 for time-ordering)
X-Correlation-ID: corr_xyz           # from client or generated (spans multiple requests)
X-Client-ID: client_42               # authenticated identity
X-Client-Plan: pro                   # subscription tier
X-Gateway-Region: us-east-1          # which region handled this
X-Gateway-Start: 1694000000123       # gateway receipt timestamp (ms)
```

---

## 7. Resilience Patterns

### Circuit Breaker (Per-Backend)

State machine:

```
                  N consecutive failures
        ┌─────────── (threshold: 5) ──────────────┐
        │                                          ▼
    ┌───────┐                               ┌──────────┐
    │ CLOSED │                               │   OPEN   │
    │(normal)│                               │(rejecting│
    │        │                               │  traffic)│
    └───────┘                               └────┬─────┘
        ▲                                        │
        │    probe succeeds                      │ after timeout
        │    (threshold: 3)                      │ (default: 30s)
        │                                        ▼
        │                                  ┌───────────┐
        └──────────────────────────────────│ HALF-OPEN │
                                           │ (probe 1  │
                                           │  req at   │
                                           │  a time)  │
                                           └───────────┘
```

| State | Behavior | Transition |
|-------|----------|------------|
| **CLOSED** | All requests forwarded | → OPEN after 5 failures in 10s window |
| **OPEN** | All requests get `503` immediately (no backend call) | → HALF-OPEN after 30s |
| **HALF-OPEN** | 1 probe request forwarded per second | → CLOSED after 3 successes; → OPEN on any failure |

**What clients see during OPEN state:**

```json
{
  "error": "service_unavailable",
  "message": "The orders service is temporarily unavailable",
  "retry_after": 30,
  "request_id": "req_a1b2c3d4"
}
```

### Timeout Budget

Every request has a **total timeout budget** that is partitioned:

```
Client timeout: 10s (set by client)
Gateway enforces: min(client_timeout, route_max_timeout)

Route config: timeout = 5s, retries = 2
  Attempt 1: 3s timeout (leave room for retry)
  Attempt 2: 2s timeout (remaining budget)
  Total: ≤ 5s
```

### Bulkhead Isolation

Each backend gets a dedicated connection pool:

```
Gateway connection pools:
  user-service:    max 200 connections
  order-service:   max 150 connections
  payment-service: max 100 connections
  search-service:  max 300 connections
  overflow pool:   0 (no sharing — prevents cascade)
```

If `order-service` is slow and its 150 connections are all occupied, new requests to `order-service` get `503` immediately — but `user-service` is unaffected because it has its own isolated pool.

---

## 8. Observability

### Metrics (Prometheus)

```
# Request rate by client, route, status
gateway_requests_total{client_id, route, method, status_code, backend}

# Latency histograms (gateway overhead only, excluding backend time)
gateway_overhead_seconds{route, quantile="0.5|0.95|0.99"}

# Backend latency (time spent waiting for backend response)
gateway_backend_duration_seconds{backend, quantile="0.5|0.95|0.99"}

# Rate-limit decisions
gateway_rate_limit_decisions_total{client_id, plan, decision="allow|reject"}
gateway_rate_limit_utilization{client_id, plan}  # gauge: 0.0 to 1.0

# Circuit breaker state
gateway_circuit_breaker_state{backend}  # 0=closed, 1=half-open, 2=open
gateway_circuit_breaker_transitions_total{backend, from, to}

# Connection pools
gateway_backend_connections_active{backend}
gateway_backend_connections_max{backend}

# Auth
gateway_auth_cache_hit_total{cache_layer="lru|redis"}
gateway_auth_cache_miss_total{cache_layer="lru|redis"}
gateway_auth_latency_seconds{method="api_key|jwt|mtls"}
```

### Distributed Tracing (OpenTelemetry)

Every request gets a trace:

```
Trace: req_a1b2c3d4
├── gateway.ingress          (0ms)   TLS termination, parse
├── gateway.auth             (0.2ms) API key lookup (LRU hit)
├── gateway.rate_limit       (0.1ms) sliding window check (local)
├── gateway.route            (0.05ms) trie match → order-service
├── gateway.transform        (0.1ms) path rewrite, header inject
├── gateway.proxy            (45ms)  forward to order-service
│   └── order-service.handle (44ms)  backend processing
└── gateway.response         (0.2ms) response transform, log
Total gateway overhead: 0.65ms
Total request time: 45.65ms
```

### Alerting Rules

| Alert | Condition | Severity |
|-------|-----------|----------|
| High error rate | 5xx rate > 5% for 2min on any backend | P1 |
| Gateway latency | P99 overhead > 20ms for 5min | P2 |
| Rate-limit store down | Redis health check fails for 30s | P1 |
| Circuit breaker open | Any backend circuit breaker in OPEN state | P2 |
| Connection pool exhausted | Any backend pool at >90% for 1min | P2 |
| Auth cache miss spike | LRU miss rate > 30% (possible cache poisoning) | P3 |

---

## 9. Security

### TLS Architecture

```
Client ──── TLS 1.3 ────▶ Gateway ──── mTLS ────▶ Backend
             (public cert)              (internal CA)
```

- Gateway terminates public TLS (certificates from Let's Encrypt or ACM).
- Gateway-to-backend uses mTLS with an internal CA (Vault PKI or cert-manager).
- Gateway's private keys are stored in the process memory or HSM, never on disk in plaintext.

### API Key Storage

```
Client sends: X-API-Key: sk_live_a1b2c3d4e5f6

Gateway:
  1. SHA-256 hash the key: hash = sha256("sk_live_a1b2c3d4e5f6")
  2. Look up hash in cache/DB
  3. Never store or log the raw key — only the hash and a prefix (sk_live_a1b2***)
```

### DDoS Defense (L7)

```
Request arrives
    │
    ▼
┌────────────────────────────┐
│ IP reputation check        │  ← shared blocklist, updated every 10s
│ (in-memory bloom filter)   │
└──────────┬─────────────────┘
           │ not blocked
           ▼
┌────────────────────────────┐
│ Connection rate limiter    │  ← per-IP: max 50 new connections/sec
│ (per-IP, sliding window)  │
└──────────┬─────────────────┘
           │ under limit
           ▼
┌────────────────────────────┐
│ Request rate limiter       │  ← per-IP (unauthenticated): 100 req/min
│ (pre-auth, per-IP)        │
└──────────┬─────────────────┘
           │ under limit
           ▼
    Normal auth + rate-limit pipeline
```

Unauthenticated requests hit the per-IP rate limiter before the auth pipeline runs. This prevents credential-stuffing attacks from consuming auth-cache and IdP resources.

---

## 10. Data Models

### API Key

```sql
CREATE TABLE api_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash        BYTEA NOT NULL UNIQUE,      -- SHA-256 of the raw key
    key_prefix      VARCHAR(12) NOT NULL,        -- "sk_live_a1b2" for display
    client_id       UUID NOT NULL REFERENCES clients(id),
    plan_id         UUID NOT NULL REFERENCES rate_limit_plans(id),
    name            VARCHAR(255),                -- human-readable label
    scopes          TEXT[] NOT NULL DEFAULT '{}', -- ["orders:read", "orders:write"]
    ip_allowlist    INET[],                      -- optional IP restrictions
    is_active       BOOLEAN NOT NULL DEFAULT true,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at    TIMESTAMPTZ
);

CREATE INDEX idx_api_keys_key_hash ON api_keys(key_hash);
CREATE INDEX idx_api_keys_client_id ON api_keys(client_id);
```

### Rate-Limit Plan

```sql
CREATE TABLE rate_limit_plans (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(50) NOT NULL UNIQUE,  -- "free", "pro", "enterprise"
    requests_per_second INTEGER,
    requests_per_minute INTEGER NOT NULL,
    requests_per_hour   INTEGER,
    requests_per_day    INTEGER,
    burst_multiplier    NUMERIC(3,2) NOT NULL DEFAULT 1.0,
    concurrent_limit    INTEGER,                      -- max in-flight requests
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Route Configuration

```sql
CREATE TABLE routes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    path_pattern    VARCHAR(500) NOT NULL,       -- "/v1/users/:id"
    methods         TEXT[] NOT NULL,             -- ["GET", "PUT"]
    backend         VARCHAR(255) NOT NULL,       -- "user-service"
    version         INTEGER NOT NULL DEFAULT 1,  -- for optimistic concurrency
    timeout_ms      INTEGER NOT NULL DEFAULT 5000,
    retry_attempts  SMALLINT NOT NULL DEFAULT 0,
    retry_on        TEXT[] DEFAULT '{"5xx", "connect-failure"}',
    rate_limit_override JSONB,                   -- per-endpoint override
    transforms      JSONB,                       -- request/response transforms
    traffic_split   JSONB,                       -- canary weights
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_routes_path_methods
    ON routes(path_pattern, methods) WHERE is_active = true;
```

### Access Log (append-only, partitioned)

```sql
CREATE TABLE access_logs (
    id              UUID DEFAULT gen_random_uuid(),
    timestamp       TIMESTAMPTZ NOT NULL,
    request_id      VARCHAR(36) NOT NULL,
    client_id       UUID,
    api_key_prefix  VARCHAR(12),
    method          VARCHAR(10) NOT NULL,
    path            VARCHAR(2000) NOT NULL,
    status_code     SMALLINT NOT NULL,
    latency_ms      INTEGER NOT NULL,
    backend         VARCHAR(255),
    backend_latency_ms INTEGER,
    request_bytes   INTEGER,
    response_bytes  INTEGER,
    rate_limited    BOOLEAN NOT NULL DEFAULT false,
    client_ip       INET,
    user_agent      VARCHAR(500),
    region          VARCHAR(20),
    gateway_node    VARCHAR(100)
) PARTITION BY RANGE (timestamp);

-- Daily partitions, retained 90 days
```

At 100K RPS, this is ~8.6B rows/day. In production, access logs go to a streaming pipeline (Kafka → ClickHouse/S3), not directly to Postgres. The schema above shows the logical shape; the physical storage is a columnar OLAP store.

---

## 11. Capacity Estimates

### Tier 3 (50K RPS) — Detailed Arithmetic

**Gateway Nodes:**

```
Target: 50,000 RPS sustained, 120,000 peak
Per-node capacity (Envoy on 4-core, 8GB): ~10,000 RPS
Nodes needed at peak: 120,000 / 10,000 = 12
With 50% headroom for rolling deploys: 18 nodes
HPA range: 8 (off-peak) → 20 (peak)
```

**Redis Cluster for Rate Limiting:**

```
Rate-limit checks/sec: 50,000 (1 per request)
  - 80% resolved locally: 10,000 Redis ops/sec
  - Background sync: 50,000 / 50 (batch) = 1,000 INCRBY ops/sec
  Total Redis ops: ~11,000/sec

Auth token cache:
  - 5% cache miss (hit rate 95%): 2,500 Redis reads/sec
  - Cache writes: ~500/sec (new tokens)

Total Redis throughput: ~14,000 ops/sec
Single Redis node handles: ~100,000 ops/sec
→ 3-node Redis Cluster (1 primary + 2 replicas) with 7x headroom
```

**Rate-Limit Counter Memory:**

```
Active clients in a window: ~5,000
Windows per client: 5 (sec, min, hour, day, endpoint)
Keys: 5,000 × 5 = 25,000
Bytes per key: ~100 (key name + counter + TTL metadata)
Total: 25,000 × 100 = 2.5 MB
→ Negligible; Redis memory dominated by token cache
```

**Token Cache Memory:**

```
Active tokens: 10,000 keys × ~1KB (JWT + permissions) = 10 MB
→ Fits in a single Redis node with room to spare
```

**Access Log Volume:**

```
50,000 RPS × ~500 bytes per log entry = 25 MB/sec = 2.16 TB/day
→ Kafka topic, 3x replication = 6.5 TB/day raw
→ ClickHouse with LZ4 compression (~4x): ~1.6 TB/day stored
→ 90-day retention: ~144 TB
```

**Network Bandwidth:**

```
Avg request size: 2 KB (headers + small JSON body)
Avg response size: 5 KB
Ingress: 50,000 × 2 KB = 100 MB/sec = 800 Mbps
Egress: 50,000 × 5 KB = 250 MB/sec = 2 Gbps
Per node (18 nodes): ~45 Mbps in + ~110 Mbps out
→ Well within 10 Gbps NIC capacity
```

---

## 12. Failure Walkthroughs

### Scenario 1: Backend Goes Fully Down

```
Timeline:
  T+0s:    order-service starts returning 503 on all requests
  T+0-3s:  Gateway forwards requests normally, clients see 503
  T+3s:    5 consecutive failures → circuit breaker OPENS for order-service
  T+3s+:   All requests to /v1/orders get instant 503 from gateway
            (no backend call, saves resources)
            Response: {"error": "service_unavailable", "retry_after": 30}
  T+33s:   Circuit breaker → HALF-OPEN, probes 1 request
  T+33s:   Probe fails → back to OPEN for another 30s
  T+63s:   Probe succeeds → tries 3 more probes
  T+66s:   3 probes succeed → CLOSED, traffic resumes

Client experience:
  - First 3 seconds: backend errors pass through (503)
  - After 3 seconds: fast 503 from gateway (no backend wait)
  - Other backends completely unaffected (bulkhead isolation)
```

### Scenario 2: Rate-Limit Store (Redis) Outage

```
Timeline:
  T+0s:    Redis Cluster becomes unreachable (network partition)
  T+0s:    Health check fails, gateway enters degraded mode

  Degraded behavior (per plan):
    free/pro:       Fail open — requests pass with local-only counters
                    Risk: ~5min of unenforced limits
    enterprise:     Fail closed — 503 with "rate limit service unavailable"

  T+0-5min: Local counters provide approximate enforcement
            (per-node limits = global limit / num_nodes)
  T+5min:   Redis recovers, counters sync, normal operation resumes

  Alert: P1 page to on-call — "rate limit store unhealthy"
```

### Scenario 3: DDoS Attack (L7 Flood)

```
Attack: 500K req/sec from 10,000 IPs, all hitting /v1/search

Timeline:
  T+0s:    Traffic spike detected by anomaly detector
  T+1s:    Per-IP rate limiter kicks in:
           - Each IP limited to 100 req/min (unauthenticated)
           - 10,000 IPs × 100/min = ~16,700 RPS pass through
  T+1s:    Legitimate traffic: ~5,000 RPS (normal for /v1/search)
           Attack traffic through: ~11,700 RPS (post IP-limit)
  T+2s:    /v1/search endpoint rate limit (5,000 RPS global) triggers:
           - 5,000 RPS pass, rest get 429
  T+5s:    Auto-scaling adds gateway nodes for TLS termination capacity
  T+10s:   Ops reviews anomaly alert, adds attacker IP ranges to blocklist
  T+15s:   Blocklist propagated, attack traffic dropped at edge

  Impact: /v1/search degraded for ~15 seconds.
          All other endpoints unaffected throughout (bulkhead isolation).
```

### Scenario 4: Bad Config Rollout

```
A config change accidentally routes /v1/orders to the wrong backend.

Timeline:
  T+0s:    Config push accepted by control plane
  T+1s:    Config distributed to all gateway nodes
  T+1s:    /v1/orders requests start hitting wrong backend → 404 or 500
  T+1s:    Error rate spike alert fires (5xx > 5% on /v1/orders for 10s)
  T+30s:   On-call reviews, triggers config rollback via admin API
  T+31s:   Previous config version pushed to all nodes
  T+32s:   Normal routing restored

  Mitigation: Config changes support canary rollout (push to 1 node first,
  validate for 60s, then roll to fleet). Enabled for production routes,
  optional for staging.
```

### Scenario 5: Client Exceeding Limits Across Multiple Nodes

```
Client api_key_abc has a 1,000 req/min limit.
They send 5,000 req/sec distributed across all gateway nodes.

With 18 gateway nodes, each sees ~278 req/sec from this client.

Local allocation per node: 1,000 / 18 ≈ 55 req/min

  T+0s:    Each node allows up to 55 requests locally (fast path)
  T+1s:    Total allowed: 18 × 55 = 990 (under limit, correct)
  T+1s:    Node utilization > 80% → all nodes switch to Redis-direct
  T+1s+:   Every request for this client checks Redis (global counter)
  T+1.1s:  Global counter hits 1,000 → all subsequent requests rejected

  Total overshoot: ≤ 55 requests (one node's local batch)
  = 5.5% overshoot — within our ≤5% tolerance

  Client sees: 429 with Retry-After header on request #1,001-1,055
```

---

## 13. Trade-offs

| Decision | What We Chose | What We Gave Up | When It Hurts |
|----------|---------------|-----------------|---------------|
| Rate-limit accuracy | ≤5% overshoot (local + Redis) | Exact enforcement | Billing-critical use cases where 5% matters — mitigated by enterprise fail-closed mode |
| Auth cache TTL (30s) | Low IdP load | Instant revocation | Security incidents where a compromised key must die immediately — mitigated by per-key configurable TTL |
| Fail-open on Redis outage | Availability over enforcement | Rate limits during outage | Abuse during outage window — mitigated by local-only counters and short outage window |
| Sliding window counter | O(1) memory, ~99.7% accuracy | 100% exact rate counts | Edge cases at window boundaries — acceptable for all practical use cases |
| Per-region rate-limit counters | Low latency (no cross-region hop) | Global consistency | Client abusing per-region limits by geo-distributing — mitigated by async cross-region sync |
| Config-driven routing | No redeploy for route changes | Compile-time route validation | Config errors reach production — mitigated by canary rollout and instant rollback |
| Single gateway fleet (not per-backend) | Operational simplicity | Blast-radius isolation | Gateway bug affects all backends — mitigated by canary deploys and fast rollback |

### What We Deliberately Did Not Build

1. **Response caching.** The gateway does not cache backend responses. Adding this is a Tier 4+ enhancement that requires cache-key design per endpoint, invalidation hooks from backends, and careful handling of personalized responses. Most teams should put a CDN in front of the gateway for static/semi-static content instead.

2. **Request queuing / admission control.** When a backend is overloaded, we circuit-break and reject — we don't queue requests hoping the backend recovers. Queuing adds unbounded latency and memory pressure. If you need smoothing, use an async queue behind the backend, not in the gateway.

3. **GraphQL support.** GraphQL queries are arbitrarily complex, making rate limiting (per-query-cost) a separate design problem. Tier 4+ if needed; most teams put a dedicated GraphQL gateway (Apollo Router, etc.) behind the API gateway for GraphQL traffic.

4. **Mutual authentication between clients.** The gateway authenticates clients against itself (API key, JWT, OAuth). It does not act as a broker for client-to-client trust. That's an authorization service problem.

---

## 14. Evolution Path

```
Tier 1 (Week 1-2)           Tier 2 (Month 1-2)
┌──────────────────┐        ┌──────────────────────────┐
│ Single Nginx +   │        │ Multi-node + Redis       │
│ local Redis      │        │ Cluster + Consul         │
│                  │        │                          │
│ • Basic routing  │───────▶│ • Sliding window limiter │
│ • API key auth   │        │ • Health checks          │
│ • Token bucket   │        │ • Circuit breakers       │
│ • Access logs    │        │ • Structured logging     │
└──────────────────┘        └──────────────────────────┘
                                       │
                                       ▼
Tier 3 (Month 3-6)          Tier 4 (Month 6-12)
┌──────────────────────────┐ ┌──────────────────────────┐
│ Gateway fleet +           │ │ Multi-region +            │
│ control plane             │ │ edge PoPs                 │
│                          │ │                          │
│ • Admin API              │ │ • Geo-distributed fleet  │
│ • Two-tier rate limit    │ │ • Cross-region sync      │
│ • Config push (<5s)      │ │ • Edge IP reputation     │
│ • OTel tracing           │─▶│ • Response caching       │
│ • Canary config rollout  │ │ • Auto-scaling (HPA)     │
│ • JWT + OAuth support    │ │ • GraphQL awareness      │
│ • gRPC transcoding       │ │ • Developer portal       │
└──────────────────────────┘ └──────────────────────────┘
```

### Migration Checkpoints

| Checkpoint | Trigger to Move | Risk of Moving Too Early |
|------------|----------------|-------------------------|
| Tier 1 → 2 | Single node at >60% CPU sustained; need HA | Over-engineering for <1K RPS; Redis Cluster ops overhead for a 2-person team |
| Tier 2 → 3 | >10 backend services; config changes need to be self-service; rate-limit accuracy matters for billing | Control plane is its own service to maintain; 3-person team may not have capacity |
| Tier 3 → 4 | Latency SLA requires regional presence; compliance requires data residency; traffic >50K RPS sustained | Multi-region doubles operational complexity; cross-region sync adds consistency challenges |

---

## Exercises

1. **Implement a sliding window counter** in Redis (Lua script) that supports hierarchical limits (global + per-plan + per-key). Write a test showing the boundary behavior when a window rolls over.

2. **Design a circuit breaker** with CLOSED/OPEN/HALF-OPEN states. Implement it as a Python class with configurable thresholds. Add a test that simulates a backend going down, the breaker opening, and then the backend recovering.

3. **Sketch the route-matching trie** data structure. Given routes `/v1/users`, `/v1/users/:id`, `/v1/users/:id/orders`, and `/v2/users`, show how the trie is structured and how a request to `/v1/users/42/orders` is matched in O(path_length) time.

4. **Calculate the Redis memory** needed for rate-limit counters serving 50K clients with 3 window types (minute, hour, day) each. Include overhead for key metadata, TTLs, and hash-table overhead.

5. **Walk through a multi-region rate-limit scenario**: a client with a 10K req/min global limit sends 6K/min from US-East and 5K/min from EU-West. Show how the async sync detects the overage and which requests get rejected, with a timeline.

6. **Design the canary config rollout** mechanism: a new route is pushed to 1 gateway node, validated for 60 seconds, then rolled to the fleet. What metrics does the canary check? What causes an automatic rollback?
