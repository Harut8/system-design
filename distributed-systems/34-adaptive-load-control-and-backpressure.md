# Adaptive Load Control & Backpressure: A Staff-Engineer Deep Dive

A comprehensive, production-grade reference covering the full spectrum of overload defense in distributed systems: queuing theory foundations, admission control, load shedding strategies, backpressure propagation, adaptive concurrency limits (AIMD, Netflix gradient), queue collapse and bufferbloat, rate limiting algorithms, fairness and multi-tenant isolation, graceful degradation, and recovery from metastable failures. Every mechanism is grounded in real-world implementations from Google SRE, Netflix, Envoy, Linkerd, Kafka, and Flink.

Prerequisites: familiarity with reliability patterns from `33-resilience-patterns-circuit-breakers.md` and networking fundamentals from `17-tcp-internals-and-congestion-control.md`.

---

## Table of Contents

1. [The Overload Problem](#1-the-overload-problem)
2. [Queuing Theory for Engineers](#2-queuing-theory-for-engineers)
3. [Admission Control](#3-admission-control)
4. [Load Shedding](#4-load-shedding)
5. [Backpressure Mechanisms](#5-backpressure-mechanisms)
6. [Adaptive Concurrency Limits](#6-adaptive-concurrency-limits)
7. [Queue Collapse and Bufferbloat](#7-queue-collapse-and-bufferbloat)
8. [Rate Limiting](#8-rate-limiting)
9. [Fairness and Multi-Tenant Isolation](#9-fairness-and-multi-tenant-isolation)
10. [Graceful Degradation Under Load](#10-graceful-degradation-under-load)
11. [Recovery from Overload and Metastability](#11-recovery-from-overload-and-metastability)
12. [Production Design Tradeoff Matrix](#12-production-design-tradeoff-matrix)

---

## 1. The Overload Problem

### 1.1 Why Overload Is the Most Dangerous Failure Mode

A crashed node is simple -- it stops, a health check catches it, traffic reroutes. An overloaded node is far worse. It stays alive enough to accept connections but too slow to complete them. Health checks pass. Load balancers keep sending traffic. The node churns through work but finishes none of it. It consumes resources -- CPU, memory, file descriptors, database connections -- without producing output. It is a black hole that looks like a healthy server.

This is why overload causes more large-scale outages than hardware failure. A single overloaded service can trigger a cascade that brings down an entire fleet. Google's SRE book documents this pattern explicitly: the majority of their most severe incidents involved overload, not crashes.

### 1.2 The Non-Linear Relationship Between Utilization and Latency

The most dangerous misconception in capacity planning is that latency scales linearly with load. It does not. Queuing theory proves that latency stays nearly flat until a critical utilization threshold, then explodes toward infinity.

```
THE HOCKEY STICK CURVE — Utilization vs. Response Time:

  Response
  Time (ms)
     │
 500 │                                                          *
     │                                                        *
 400 │                                                      *
     │                                                    *
 300 │                                                  *
     │                                               *
 200 │                                           *
     │                                       *
 100 │                                  *
     │              * * * * * * * *
  50 │  * * * * * *
     │
     └──────────────────────────────────────────────────────────
     0%   10%  20%  30%  40%  50%  60%  70%  80%  90%  95% 100%

                         Utilization (ρ)

  KEY INSIGHT:
    - From 0-60% utilization: latency is nearly flat
    - From 60-80%: latency starts climbing noticeably
    - From 80-90%: latency doubles or triples
    - From 90-95%: latency increases 5-10x
    - Above 95%: latency approaches infinity

  THIS IS NOT LINEAR. It is a hyperbolic function: W = 1/(μ - λ)
  As λ → μ (arrival rate approaches service rate), W → ∞
```

### 1.3 Little's Law

Little's Law is the foundational equation linking three quantities in any stable queuing system:

```
LITTLE'S LAW:

  L = λ * W

  Where:
    L = average number of items in the system (queue + being served)
    λ = average arrival rate (requests per second)
    W = average time each item spends in the system (latency)

  This is universal. It holds for:
    - HTTP request queues
    - Database connection pools
    - Kafka consumer groups
    - Thread pool work queues
    - Network packet buffers

  Example:
    A service handles 1000 req/s (λ) with 200ms average latency (W).
    L = 1000 * 0.2 = 200 requests in flight at any moment.

    If latency doubles to 400ms (overload begins):
    L = 1000 * 0.4 = 400 requests in flight.

    Those 400 requests consume memory, connections, threads.
    This increased resource consumption makes latency worse,
    which increases L further. This is the death spiral.
```

### 1.4 The Death Spiral

The overload death spiral is a positive feedback loop. Each stage makes the next stage worse, and the system cannot recover without external intervention.

```
THE DEATH SPIRAL:

  ┌─────────────────────────────┐
  │  1. Traffic spike arrives   │
  └──────────────┬──────────────┘
                 ▼
  ┌─────────────────────────────┐
  │  2. Utilization exceeds     │
  │     ~80%, latency rises     │
  └──────────────┬──────────────┘
                 ▼
  ┌─────────────────────────────┐
  │  3. Clients timeout,        │
  │     receive errors          │
  └──────────────┬──────────────┘
                 ▼
  ┌─────────────────────────────┐
  │  4. Clients retry           │◄──────────────────────────┐
  │     (often immediately)     │                           │
  └──────────────┬──────────────┘                           │
                 ▼                                          │
  ┌─────────────────────────────┐                           │
  │  5. Retries ADD to the      │     POSITIVE FEEDBACK     │
  │     original load           │     LOOP — each cycle     │
  │     (retry amplification)   │     generates MORE        │
  └──────────────┬──────────────┘     retries than the      │
                 ▼                     previous one          │
  ┌─────────────────────────────┐                           │
  │  6. Server even more        │                           │
  │     overloaded, latency     ├───────────────────────────┘
  │     increases further       │
  └──────────────┬──────────────┘
                 ▼
  ┌─────────────────────────────┐
  │  7. Upstream services       │
  │     timeout, cascade        │
  │     failure propagates      │
  └─────────────────────────────┘
```

### 1.5 Retry Amplification

Retry amplification is often misunderstood. A single client retrying 3 times generates at most 4 attempts (1 original + 3 retries) per logical request. This is linear, not exponential.

The real danger is **multi-layer retry amplification**, where retries compound across service layers:

```
SINGLE-LAYER RETRY (LINEAR):

  Client sends 1000 req/s, each with up to 3 retries.
  If all fail: 1000 * 4 = 4000 total attempts.
  Amplification factor: 4x (linear in retry count).

MULTI-LAYER RETRY (MULTIPLICATIVE):

  Client → Service A → Service B → Database

  Suppose the database is slow and all requests fail:

  Database receives request from Service B.
    Service B retries 3 times → 4 attempts per request.
  Service A sees Service B fail.
    Service A retries 3 times → 4 attempts per request.
  Client sees Service A fail.
    Client retries 3 times → 4 attempts per request.

  Total attempts at the database per logical client request:
    4 * 4 * 4 = 64 attempts

  For 1000 original req/s:
    1000 * 64 = 64,000 req/s at the database

  THIS is the exponential amplification that causes cascading failure.
  It is multiplicative across layers, not within a single layer.

  DEFENSE:
    - Retry budgets: allow at most 10% of traffic to be retries
    - Retry at only one layer (usually closest to the origin)
    - Exponential backoff with jitter at every layer
    - Propagate deadlines so expired requests are never retried
```

Real incidents follow this pattern with striking regularity. Amazon's 2012 ELB outage was caused by a modest traffic increase that triggered a retry storm across internal services, amplifying load until the entire region's control plane was overwhelmed. Google has documented similar cascading failures in their SRE book: a single overloaded backend caused its callers to queue up requests, which caused their callers to queue, propagating failure through five service layers in under a minute.

---

## 2. Queuing Theory for Engineers

### 2.1 The M/M/1 Queue Model

The simplest useful queuing model is M/M/1: Markovian (Poisson) arrivals, Markovian (exponential) service times, one server. While no production system matches this exactly, the model captures the fundamental non-linear behavior of queuing systems.

```
M/M/1 QUEUE:

  Arrivals (λ)    ┌─────────────────────────┐    Departures (μ)
  ────────────►   │  Queue    │   Server    │   ──────────────►
                  │  ○ ○ ○ ○  │  [  ●  ]    │
                  └─────────────────────────┘

  Key variables:
    λ  = arrival rate (requests per second)
    μ  = service rate (max throughput when fully busy)
    ρ  = λ/μ  (utilization, must be < 1 for stability)

  KEY FORMULAS:

    Utilization:              ρ = λ / μ

    Avg items in system:      L = ρ / (1 - ρ)

    Avg time in system:       W = 1 / (μ - λ)
                              W = (1/μ) / (1 - ρ)

    Avg items in queue only:  Lq = ρ² / (1 - ρ)

    Avg wait in queue only:   Wq = ρ / (μ - λ)
                              Wq = ρ * (1/μ) / (1 - ρ)

    Response time:            R = service_time + wait_time
                              R = (1/μ) + Wq
                              R = 1 / (μ - λ)

  CRITICAL INSIGHT:
    As ρ → 1:  W → ∞,  L → ∞
    The system becomes unstable when arrival rate meets service rate.
```

### 2.2 Utilization vs. Response Time

This table makes the non-linearity concrete. Assume a server with a 10ms service time (μ = 100 req/s):

```
  ┌──────────────┬──────────────┬────────────────┬───────────────────┐
  │ Utilization  │ Arrival Rate │ Avg Response   │ Response Time     │
  │ (ρ)         │ (λ req/s)    │ Time (ms)      │ Multiplier        │
  ├──────────────┼──────────────┼────────────────┼───────────────────┤
  │    10%       │     10       │     11.1       │      1.1x         │
  │    30%       │     30       │     14.3       │      1.4x         │
  │    50%       │     50       │     20.0       │      2.0x         │
  │    70%       │     70       │     33.3       │      3.3x         │
  │    80%       │     80       │     50.0       │      5.0x         │
  │    90%       │     90       │    100.0       │     10.0x         │
  │    95%       │     95       │    200.0       │     20.0x         │
  │    99%       │     99       │   1000.0       │    100.0x         │
  └──────────────┴──────────────┴────────────────┴───────────────────┘

  At 50% utilization, response time is 2x service time. Reasonable.
  At 90% utilization, response time is 10x service time. Danger.
  At 99% utilization, response time is 100x service time. Outage.
```

### 2.3 Multi-Server Queues (M/M/c)

Adding servers (c > 1) helps by allowing multiple requests to be processed in parallel, but it does not eliminate the hockey stick -- it shifts the curve rightward.

```
M/M/c QUEUE (c servers):

  Arrivals (λ)    ┌─────────────────────────────────┐
  ────────────►   │  Queue    │  Server 1  [●]      │
                  │  ○ ○ ○    │  Server 2  [●]      │
                  │           │  Server 3  [●]      │
                  │           │  ...                 │
                  │           │  Server c  [●]      │
                  └─────────────────────────────────┘

  System utilization: ρ = λ / (c * μ)

  This ρ represents the fraction of time each server is busy on average.

  With c=4 servers, μ=100 req/s per server, λ=360 req/s:
    ρ = 360 / (4 * 100) = 0.9
    Each server is busy 90% of the time on average.

  The benefit of multiple servers is that queueing delay is much lower
  than a single server at the same per-server utilization:
    M/M/1 at ρ=0.9:   Wq = 90ms   (with 10ms service time)
    M/M/4 at ρ=0.9:   Wq ≈ 6ms    (Erlang C formula)

  But at ρ → 1, even M/M/c queues blow up:
    M/M/4 at ρ=0.99:  Wq ≈ 240ms
    Adding servers delays the hockey stick, it does not prevent it.
```

### 2.4 Beyond M/M/1: Realistic Queuing Models

The M/M/1 and M/M/c models assume Poisson arrivals and exponential service times. Real backend systems violate both assumptions.

**Why M/M/1 is insufficient for production analysis:**

```
REAL-WORLD VIOLATIONS OF M/M/1 ASSUMPTIONS:

  Assumption: Poisson arrivals (memoryless, uniformly random)
  Reality:
    - Bursty traffic (flash sales, breaking news, cron job storms)
    - Correlated arrivals (batch operations, fan-out from upstream)
    - Diurnal patterns with sharp transitions

  Assumption: Exponential service times (memoryless)
  Reality:
    - Bimodal: cache hit (1ms) vs. cache miss (50ms)
    - Heavy-tailed: most requests 5ms, some take 500ms (GC pauses,
      lock contention, cold storage reads)
    - Variable by endpoint: GET /health (0.1ms) vs. POST /report (30s)

  Consequence: M/M/1 UNDERESTIMATES queueing delay when service
  times are highly variable, because a single slow request blocks
  the server while fast requests pile up behind it.
```

**The M/G/1 model** generalizes to arbitrary service-time distributions, keeping Poisson arrivals. The key result is the Pollaczek-Khinchine formula:

```
M/G/1 QUEUE (general service times):

  Average number in queue:

    Lq = (ρ² + λ² * Var[S]) / (2 * (1 - ρ))

  Where:
    ρ = λ * E[S]        (utilization)
    E[S]                 (mean service time)
    Var[S]               (variance of service time)

  KEY INSIGHT: queue length depends on the VARIANCE of service time,
  not just the mean. Two systems with identical average service times
  but different variances will have very different queue depths.
```

### 2.5 Kingman's Formula and the Coefficient of Variation

Kingman's approximation provides an intuitive formula for waiting time in a G/G/1 queue (general arrivals, general service times, one server):

```
KINGMAN'S APPROXIMATION:

  Wq ≈ (ρ / (1 - ρ)) * ((Ca² + Cs²) / 2) * E[S]

  Where:
    ρ     = utilization (λ * E[S])
    Ca    = coefficient of variation of interarrival times
            Ca = σ_arrival / μ_arrival
    Cs    = coefficient of variation of service times
            Cs = σ_service / μ_service
    E[S]  = mean service time

  The coefficient of variation (CV) measures relative variability:
    CV = standard_deviation / mean

  For exponential distributions: CV = 1 (the M/M/1 baseline)
  For deterministic (constant):  CV = 0 (best case)
  For heavy-tailed:              CV > 1 (worse than exponential)

  EXAMPLE — Same utilization, different variability:

  System A (uniform requests):
    ρ = 0.8, Ca = 1.0, Cs = 0.3, E[S] = 10ms
    Wq ≈ (0.8/0.2) * ((1.0 + 0.09) / 2) * 10 = 4 * 0.545 * 10 = 21.8ms

  System B (mixed request types):
    ρ = 0.8, Ca = 1.0, Cs = 2.0, E[S] = 10ms
    Wq ≈ (0.8/0.2) * ((1.0 + 4.0) / 2) * 10 = 4 * 2.5 * 10 = 100ms

  Same utilization. Same average service time.
  System B has 4.6x higher queueing delay because of high service-time variance.

  THIS is why a single slow request type can destroy the latency
  of an entire service, even at moderate utilization.
```

### 2.6 Tail Latency

Average latency is a misleading metric. The tail (p95, p99, p99.9) reveals how the system behaves for the worst-affected requests, which are often the ones that matter most (e.g., a checkout completing at p99.9).

```
TAIL LATENCY IN DISTRIBUTED SYSTEMS:

  For a single service at 1ms p50, 10ms p99:
    99% of requests are fast, 1% are slow.

  For a fan-out to 100 backend shards (e.g., search):
    P(all 100 fast) = 0.99^100 = 0.366
    P(at least one slow) = 1 - 0.366 = 63.4%

    63% of user-facing requests hit the tail of at least one backend.
    The user-visible p50 is dominated by backend p99.

  AMPLIFICATION TABLE:

  ┌───────────────────┬──────────┬──────────┬──────────┐
  │ Fan-out width     │ p99 hit  │ p99.9    │ p99.99   │
  │                   │ rate     │ hit rate │ hit rate │
  ├───────────────────┼──────────┼──────────┼──────────┤
  │ 1 (no fan-out)    │   1%     │   0.1%   │  0.01%   │
  │ 10                │   9.6%   │   1.0%   │  0.1%    │
  │ 50                │  39.5%   │   4.9%   │  0.5%    │
  │ 100               │  63.4%   │   9.5%   │  1.0%    │
  │ 1000              │  99.996% │  63.2%   │  9.5%    │
  └───────────────────┴──────────┴──────────┴──────────┘

  DEFENSES:
    - Hedged requests: send duplicate request to second replica after
      a delay (e.g., after p95 latency). Use the first response.
      Caveat: hedging increases total load — use sparingly.
    - Tail-tolerant design: return partial results within a deadline
      rather than waiting for the slowest shard.
    - Request coalescing: deduplicate identical in-flight requests.
    - Background probing: pre-warm cold replicas.
```

### 2.7 Queue Depth Limits and Head-of-Line Blocking

Unbounded queues are a reliability hazard. A bounded queue forces a decision -- reject or drop -- but an unbounded queue silently accumulates items whose deadlines have already expired, wasting resources processing requests that will time out before the response reaches the client.

Head-of-line (HOL) blocking occurs when a slow or stuck request at the front of a FIFO queue blocks all subsequent requests. This is why the HTTP/2 multiplexing model exists at the protocol level, and why service-level queues should use priority or deadline scheduling rather than strict FIFO.

### 2.8 Queuing Networks

Production systems are not single queues. They are networks of queues where the output of one queue feeds the input of the next:

```
QUEUING NETWORK — Typical request path:

  ┌─────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
  │ LB Queue│───►│ App Queue│───►│ DB Pool  │───►│ Disk I/O │
  │  (NIC)  │    │ (threads)│    │ (conns)  │    │  Queue   │
  └─────────┘    └──────────┘    └──────────┘    └──────────┘

  Total latency = sum of queueing delays at each stage.

  A queue at every layer can make the system look healthy temporarily:
    - LB queue depth: 0 (draining fast to app)
    - App queue depth: 0 (draining fast to DB)
    - DB pool queue: 200 (bottleneck here)
    - Individual queue metrics say "fine" for 3 of 4 stages.
    - Total latency is unacceptable.

  MULTI-RESOURCE CONTENTION:
    A single request may hold multiple resources simultaneously:
      - 1 thread
      - 1 database connection
      - N bytes of memory for response buffering
      - File descriptors for downstream connections

    Resource limits are not interchangeable:
      - A CPU limit does not protect database connections.
      - A connection pool limit does not protect memory.
      - Reaching ANY single resource limit causes queueing.

  This is why per-resource monitoring is essential.
  Aggregate metrics ("CPU is fine") can hide resource-specific bottlenecks.
```

---

## 3. Admission Control

### 3.1 What Is Admission Control

Admission control is the decision of whether work should enter a constrained system in the first place. It is distinct from both rate limiting and load shedding, though all three reject requests:

```
ADMISSION CONTROL vs. RATE LIMITING vs. LOAD SHEDDING:

  ┌──────────────────┬────────────────────────────────────────────────┐
  │ Mechanism        │ Decision basis                                 │
  ├──────────────────┼────────────────────────────────────────────────┤
  │ Rate Limiting    │ "How many requests per second from this        │
  │                  │  source?" Policy-based, pre-configured.        │
  ├──────────────────┼────────────────────────────────────────────────┤
  │ Admission Control│ "Can the system handle this specific request   │
  │                  │  right now?" Capacity-aware, real-time.        │
  ├──────────────────┼────────────────────────────────────────────────┤
  │ Load Shedding    │ "The system is already overloaded. Which       │
  │                  │  requests should we drop?" Reactive.           │
  └──────────────────┴────────────────────────────────────────────────┘

  Rate limiting prevents abuse.
  Admission control prevents overload.
  Load shedding recovers from overload.

  REQUEST LIFECYCLE:

                    ┌──────────────────┐
   Request ───────► │  Rate Limiter    │ ── rejected (429) ──►
   arrives          │  (per-source     │
                    │   policy)        │
                    └────────┬─────────┘
                             │ passed
                             ▼
                    ┌──────────────────┐
                    │  Admission       │ ── rejected (503) ──►
                    │  Controller      │
                    │  (capacity-aware)│
                    └────────┬─────────┘
                             │ admitted
                             ▼
                    ┌──────────────────┐
                    │  Bounded Queue   │ ── dropped (queue full) ──►
                    └────────┬─────────┘
                             │ dequeued
                             ▼
                         Workers
```

### 3.2 Capacity-Based Admission

The admission controller checks whether the system has the resources to serve this request before accepting it:

```
CAPACITY-BASED ADMISSION:

  Check before accepting:
    ✓ In-flight requests < max_concurrency?
    ✓ Thread pool has available threads?
    ✓ Database connection pool has available connections?
    ✓ Memory usage below threshold?
    ✓ CPU utilization below threshold?

  If ANY resource is exhausted → reject immediately with 503.

  Example:
    A service receives a request that needs a database connection.
    All 50 connections are occupied.
    The request's deadline expires in 200ms.
    Average connection hold time is 500ms.

    WITHOUT admission control:
      Request enters the connection-pool queue.
      Waits 500ms for a connection.
      Deadline has expired.
      Processes anyway (wasted work).

    WITH admission control:
      Checks: connection pool exhausted, deadline too short to wait.
      Rejects immediately with 503.
      Client can retry on another instance (or fail fast to user).

  This is fundamentally different from rate limiting:
    Rate limiting: "You sent too many requests."
    Admission control: "The system cannot serve this request right now."
```

### 3.3 Deadline-Aware Admission

Every request in a distributed system should carry a deadline -- the absolute time by which the response must arrive to be useful. Deadline-aware admission rejects requests that cannot possibly complete in time:

```
DEADLINE-AWARE ADMISSION:

  remaining_budget = request.deadline - now()
  estimated_processing_time = current_p95_latency

  if remaining_budget < estimated_processing_time:
    reject(503, "insufficient deadline budget")

  Example:
    Client sets 2s timeout on the full request chain:
      Client → Gateway → Service A → Service B → Database

    By the time the request reaches Service B:
      Total elapsed: 1.8s
      Remaining budget: 0.2s
      Service B p95 latency: 0.5s

    Service B rejects immediately.
    Without this, Service B would process for 0.5s,
    but Service A already timed out at the 2s mark.
    The response is delivered to nobody.

  DEADLINE PROPAGATION:
    gRPC propagates deadlines natively via the grpc-timeout header.
    HTTP services must propagate them explicitly (e.g., X-Request-Deadline).
    Each hop subtracts its own processing overhead from the remaining budget.
```

### 3.4 Per-Tenant Admission

In multitenant systems, admission control prevents one tenant from consuming all the capacity:

```
PER-TENANT ADMISSION:

  Total capacity: 1000 concurrent requests
  Tenant allocation:
    Tenant A (enterprise): reserved 400, burst to 600
    Tenant B (startup):    reserved 200, burst to 300
    Tenant C (free):       reserved  50, burst to 100
    Shared pool:           remaining capacity

  When Tenant A sends a burst of 2000 requests:
    First 600 admitted (up to burst limit)
    Remaining 1400 rejected with 429
    Tenants B and C unaffected — their reserved capacity is protected

  WITHOUT per-tenant admission:
    Tenant A consumes all 1000 concurrent slots
    Tenants B and C starve completely
    "Noisy neighbor" problem
```

### 3.5 Dependency-Aware Admission

Advanced admission controllers consider the state of downstream dependencies:

```
DEPENDENCY-AWARE ADMISSION:

  Service A depends on Service B and Database C.

  Admission check:
    if service_b.circuit_breaker == OPEN:
      reject requests that require Service B
      allow requests that only need Database C
    if database_c.connection_pool_utilization > 90%:
      reject expensive queries (analytics, reports)
      allow cheap queries (key-value lookups)

  This prevents accepted requests from failing downstream,
  which would waste resources and amplify load on struggling
  dependencies.
```

---

## 4. Load Shedding

### 4.1 The Counterintuitive Truth

Load shedding means intentionally dropping requests to protect the system. This seems wasteful, but the math is clear: under overload, a server that tries to process everything completes nothing. Rejecting 10% of requests lets the other 90% complete successfully. Without shedding, goodput (successfully completed requests) collapses to zero while the server burns CPU on work it will never finish.

```
GOODPUT VS. LOAD — With and Without Load Shedding:

  Goodput
  (successful
   req/s)
     │
     │                        ┌──── With load shedding
 900 │         * * * * * * * *──────────────────────
     │       *                ──── Shedding excess, protecting core capacity
 700 │     *
     │    *
 500 │   *
     │  *      Without shedding ────┐
 300 │ *                             * *
     │*                                  *
 100 │                                     *  *
   0 │─────────────────────────────────────────* * * *──
     └──────────────────────────────────────────────────
     0   200  400  600  800 1000 1200 1400 1600 1800

                    Offered Load (req/s)
                    Server capacity: 1000 req/s

  Without shedding: goodput peaks then DROPS as overload grows.
    Reason: server spends all CPU on queuing overhead, context switching,
    GC pressure, and processing requests that will timeout anyway.

  With shedding: goodput plateaus at capacity.
    Excess requests are rejected immediately (cheap).
    Resources are preserved for requests that will complete.
```

### 4.2 Load Shedding Strategies

**Random rejection.** The simplest approach: when utilization exceeds a threshold, reject each incoming request with a probability proportional to the overload factor. Easy to implement, but unfair -- it treats all requests equally regardless of importance.

**Priority-based shedding.** Classify requests into priority tiers and shed low-priority work first. Google's internal systems define four criticality levels: `CRITICAL_PLUS` (never shed -- these keep the system alive), `CRITICAL` (user-facing requests), `SHEDDABLE_PLUS` (important but deferrable), and `SHEDDABLE` (background work like analytics, pre-fetching). Under increasing load, the system sheds `SHEDDABLE` first, then `SHEDDABLE_PLUS`, then `CRITICAL`, and only touches `CRITICAL_PLUS` if the alternative is total failure.

**Client-based shedding.** Protect paying customers by rejecting free-tier traffic first. This requires request metadata (API key, account tier) to be available at the shedding layer.

**Cost-based shedding.** An expensive analytics query that will consume 30 seconds of CPU should be shed before a lightweight key-value lookup. This requires the shedding layer to estimate request cost, which can be done by endpoint, by request size, or by historical latency profiles.

**Deadline-based shedding.** Every request carries a deadline (explicit timeout). If a request has been sitting in a queue longer than its remaining deadline allows for processing, drop it immediately. The client has already timed out; processing this request wastes resources and produces a response nobody is waiting for.

**CoDel (Controlled Delay).** Originally designed for network packet queues, CoDel tracks the sojourn time of each request -- how long it has spent in the queue. If sojourn time exceeds a target (e.g., 5ms) for longer than an interval (e.g., 100ms), the algorithm begins dropping requests. The drop rate follows an inverse-square-root schedule, increasing gradually rather than oscillating between no-drop and heavy-drop states.

**Important caveat about CoDel in service queues:** CoDel was designed for network queue management where "dropping" a packet triggers TCP retransmission -- a well-defined recovery mechanism. Applying CoDel to application work queues requires careful adaptation. Dropping an HTTP request, a database write, and a network packet have very different consequences. A dropped packet is retransmitted by TCP. A dropped HTTP request returns a 503 to the user. A dropped database write may lose data. The general principle -- control queue delay rather than merely queue length -- is highly valuable. But the implementation must account for application-level semantics of "dropping" work.

### 4.3 Google's Approach to Handling Overload

Google's "Handling Overload" chapter from the SRE book describes a multi-layered defense:

```
GOOGLE'S OVERLOAD DEFENSE LAYERS:

  Layer 1: Per-Client Quotas
  ┌─────────────────────────────────────────────────────────────────┐
  │  Each client (service) has a configured QPS quota.              │
  │  Requests exceeding quota are rejected before any processing.   │
  │  Provisioned per-client, not globally, to prevent one           │
  │  misbehaving service from starving others.                      │
  └─────────────────────────────────────────────────────────────────┘

  Layer 2: CPU-Based Rejection
  ┌─────────────────────────────────────────────────────────────────┐
  │  When backend CPU utilization exceeds a threshold (e.g., 80%),  │
  │  the server begins rejecting requests probabilistically.        │
  │  rejection_probability = (requests - threshold) / (requests+1)  │
  │  This is independent of client identity -- pure self-protection.│
  └─────────────────────────────────────────────────────────────────┘

  Layer 3: Criticality-Based Progressive Shedding
  ┌─────────────────────────────────────────────────────────────────┐
  │  Request criticality is propagated in RPC metadata:             │
  │                                                                  │
  │    CRITICAL_PLUS  → never shed (system liveness)                │
  │    CRITICAL       → shed only under extreme overload            │
  │    SHEDDABLE_PLUS → shed early under moderate overload          │
  │    SHEDDABLE      → shed first (background, async, analytics)   │
  │                                                                  │
  │  Under increasing load:                                          │
  │    85% CPU → shed SHEDDABLE                                     │
  │    90% CPU → shed SHEDDABLE + SHEDDABLE_PLUS                    │
  │    95% CPU → shed all except CRITICAL_PLUS                      │
  └─────────────────────────────────────────────────────────────────┘
```

### 4.4 Where and How to Shed

**At the edge (load balancer / API gateway).** This is cheapest -- the request never reaches the backend. Envoy, NGINX, and cloud load balancers all support rate limiting and connection limits. The tradeoff: edge shedding cannot make priority decisions without parsing request metadata, which adds latency and complexity to the edge.

**At the service.** The service knows request priority, estimated cost, and its own resource utilization. Service-level shedding is more precise but more expensive -- the request has already consumed network bandwidth and connection resources.

**Response codes.** Use HTTP 503 (Service Unavailable) for general overload, HTTP 429 (Too Many Requests) for rate-limit violations. Always include a `Retry-After` header with a value that includes jitter -- without it, clients will retry immediately and synchronize into a thundering herd.

### 4.5 Load-Shedding Observability

Shedding without observability is flying blind. Track these metrics to prove the system is shedding correctly:

```
LOAD SHEDDING METRICS:

  ┌──────────────────────────┬──────────────────────────────────────┐
  │ Metric                   │ Why it matters                       │
  ├──────────────────────────┼──────────────────────────────────────┤
  │ shed_rate                │ Requests rejected per second.        │
  │                          │ Should rise under overload,          │
  │                          │ drop when load subsides.             │
  ├──────────────────────────┼──────────────────────────────────────┤
  │ shed_rate_by_priority    │ Are we shedding the right requests?  │
  │                          │ CRITICAL traffic shed = escalation.  │
  ├──────────────────────────┼──────────────────────────────────────┤
  │ goodput                  │ Successfully completed req/s.        │
  │                          │ Must stay near capacity under         │
  │                          │ shedding; if it drops, shedding      │
  │                          │ is too aggressive or too late.        │
  ├──────────────────────────┼──────────────────────────────────────┤
  │ shed_latency             │ Time spent deciding to reject.       │
  │                          │ Must be near-zero (<1ms).            │
  │                          │ If high, the shedding logic itself   │
  │                          │ is contributing to overload.          │
  ├──────────────────────────┼──────────────────────────────────────┤
  │ wasted_work              │ Requests processed to completion     │
  │                          │ but whose response was never read     │
  │                          │ (client already timed out).           │
  │                          │ Deadline-based shedding should        │
  │                          │ drive this toward zero.               │
  └──────────────────────────┴──────────────────────────────────────┘
```

---

## 5. Backpressure Mechanisms

### 5.1 Why Backpressure Is Essential

Backpressure is the mechanism by which a downstream system signals upstream systems to slow down. Without backpressure, a fast producer overwhelms a slow consumer, and the gap manifests as unbounded queue growth, memory exhaustion, or dropped data. Backpressure makes the entire pipeline operate at the speed of its slowest component rather than letting fast components flood the slow ones.

```
END-TO-END BACKPRESSURE FLOW:

  ┌────────┐    ┌────────────┐    ┌─────────┐    ┌──────────┐    ┌────────┐
  │ Client │───►│ API Gateway│───►│ Service │───►│ Worker   │───►│Database│
  └────────┘    └────────────┘    └─────────┘    └──────────┘    └────────┘

  Without backpressure:
    Client sends at 10K req/s → Gateway passes all → Service queues grow
    → Worker overwhelmed → Database connection pool exhausted → crash

  With end-to-end backpressure:
    Database signals Worker: "slow down" (connection pool high-water mark)
         │
         ▼
    Worker signals Service: "slow down" (rejects or returns 429)
         │
         ▼
    Service signals Gateway: "slow down" (health check degrades)
         │
         ▼
    Gateway signals Client: "slow down" (429 with Retry-After)
         │
         ▼
    Client backs off (exponential backoff + jitter)

  CRITICAL RULE:
    Backpressure must be present at EVERY layer.
    If any intermediate layer lacks backpressure, it becomes
    an unbounded buffer that will eventually exhaust memory.
```

### 5.2 Backpressure Semantics

There are four distinct responses when a system is under pressure. Each has different semantics and appropriate use cases:

```
BACKPRESSURE RESPONSE TYPES:

  ┌──────────────┬─────────────────────────────────────────────────┐
  │ Response     │ Behavior                                         │
  ├──────────────┼─────────────────────────────────────────────────┤
  │ Block        │ Producer waits until consumer is ready.          │
  │              │ No data loss. Can cause producer-side stalls.    │
  │              │ Example: TCP flow control (window = 0).          │
  ├──────────────┼─────────────────────────────────────────────────┤
  │ Reject       │ Return error immediately. Producer decides       │
  │              │ whether to retry. No data loss if producer       │
  │              │ buffers. Example: HTTP 429, gRPC RESOURCE_       │
  │              │ EXHAUSTED.                                       │
  ├──────────────┼─────────────────────────────────────────────────┤
  │ Buffer       │ Accept into bounded buffer for later processing. │
  │              │ Adds latency but absorbs short bursts.           │
  │              │ Dangerous if buffer is unbounded.                │
  │              │ Example: Kafka topic (durable), thread pool      │
  │              │ work queue (volatile).                           │
  ├──────────────┼─────────────────────────────────────────────────┤
  │ Drop         │ Silently discard. Fastest, lowest resource use.  │
  │              │ Data loss. Only acceptable for idempotent,       │
  │              │ non-critical work (metrics, logs, samples).      │
  │              │ Example: UDP under load, StatsD, sampling.       │
  └──────────────┴─────────────────────────────────────────────────┘

  A durable queue (Kafka, SQS) preserves work but does NOT increase
  processing capacity. If consumers are slower than producers, the
  queue grows without bound. Durability buys time; it does not solve
  the throughput mismatch.
```

### 5.3 Backpressure Strategies

**Explicit signaling.** The most direct form. HTTP 429 with `Retry-After` tells the client exactly when to retry. gRPC uses the `RESOURCE_EXHAUSTED` status code with optional retry metadata. These signals require the upstream to respect them -- a client that ignores 429 responses and retries immediately defeats the mechanism.

**Protocol-level flow control.** TCP has built-in backpressure via the receive window -- when the receiver's buffer fills, the window shrinks to zero, pausing the sender. HTTP/2 adds stream-level flow control with WINDOW_UPDATE frames, allowing per-stream throttling without affecting other streams on the same connection. gRPC inherits HTTP/2 flow control and adds its own application-level flow control for streaming RPCs.

**Reactive Streams (demand signaling).** The Reactive Streams specification (standardized in Java 9's `java.util.concurrent.Flow`) uses a demand-pull model: the subscriber tells the publisher exactly how many items it is ready to receive. The publisher must not emit more items than requested. This inverts the traditional push model and makes backpressure the default behavior rather than an afterthought.

**Queue depth monitoring.** The consumer monitors its internal queue depth and signals "stop" when the queue exceeds a high-water mark, resuming when it drops below a low-water mark. This hysteresis (two thresholds, not one) prevents oscillation between full-speed and full-stop.

**Credit-based flow control.** The consumer grants a fixed number of credits (permits) to the producer. Each sent item consumes one credit. When credits reach zero, the producer must wait for the consumer to grant more. AMQP 1.0 uses this model natively.

### 5.4 End-to-End Overload Propagation

The most dangerous overload scenarios involve propagation across multiple service boundaries. Understanding the full request path is essential:

```
END-TO-END OVERLOAD PROPAGATION:

  Client → API Gateway → Service A → Service B → Database

  Suppose the database becomes slow (disk I/O saturation):

  Step 1: Database query latency: 10ms → 500ms
  Step 2: Service B holds DB connections longer.
          Connection pool saturates (50/50 connections busy).
          New requests queue for connections.
  Step 3: Service B response time: 15ms → 800ms
          Service A's outbound connection pool to B fills up.
          Service A's threads block waiting for Service B.
  Step 4: Service A response time: 20ms → 1200ms
          API Gateway sees slow responses, keeps sending traffic.
          Gateway's connection pool to A fills up.
  Step 5: Client requests start timing out (2s deadline).
          Clients retry. Retry traffic hits the gateway.
  Step 6: Total load at the database: original + retries from 3 layers.

  TIME TO TOTAL FAILURE: typically 30-120 seconds.

  THE MISSING QUESTION:
    Where should the system STOP accepting work?
    How should the overload signal propagate BACK to the caller?

  SOLUTIONS:

  1. Per-hop deadline propagation:
     Client sets deadline: 2s
     Gateway subtracts processing: passes 1.9s to Service A
     Service A subtracts processing: passes 1.7s to Service B
     Service B checks: 1.5s remaining, DB p95 is 500ms → proceed
     If remaining budget < estimated time → reject immediately

  2. Cancellation propagation:
     Client cancels after 2s timeout.
     Gateway must propagate cancellation to Service A.
     Service A must propagate to Service B.
     Service B must abort the database query.
     WITHOUT cancellation propagation, the database continues
     processing a query whose result nobody will read.

  3. Fan-out amplification awareness:
     If Service A fans out to 20 shards of Service B:
       1 client request → 20 Service B requests
       Each Service B request retries 3x → 80 requests
       Client retry → another 80 requests
     A 1000 req/s client load becomes 160,000 req/s at Service B.

  4. Connection-pool saturation as a signal:
     When Service A's connection pool to B is >80% full,
     Service A should begin rejecting NEW requests (admission control)
     rather than queuing them behind an already-saturated dependency.
```

### 5.5 Backpressure Across Async Boundaries

When a synchronous request becomes a durable background job, the backpressure semantics change:

```
SYNC-TO-ASYNC BOUNDARY:

  Client ──HTTP──► API ──enqueue──► Queue ──dequeue──► Worker ──► DB

  The API responds 202 Accepted immediately.
  The client thinks the request succeeded.
  But the worker may be hours behind.

  PROBLEMS:
    - Client has no visibility into actual processing capacity.
    - Queue depth is the only backpressure signal.
    - A "fast" API accepting 10K enqueues/s with workers
      processing 100/s creates a backlog that grows forever.

  SOLUTIONS:
    - Expose queue depth to the API layer.
      If queue depth > threshold, reject new enqueues (429).
    - Expose estimated processing time to the client.
      "Your job is #5000 in queue, estimated completion: 2 hours."
    - Apply admission control at the enqueue boundary:
      reject if the queue will not drain before the job's SLA.
```

### 5.6 Backpressure in Streaming Systems

**Kafka.** Kafka does not have built-in backpressure from broker to producer -- producers can overwhelm a broker. Backpressure manifests as consumer lag: the gap between the log head and the consumer's committed offset. Operators monitor lag as the primary overload signal. On the consumer side, `max.poll.records` limits how many records a poll returns, and consumers can call `pause()` on partitions to stop fetching until processing catches up.

**Flink.** Flink's backpressure mechanism is entirely implicit. Operators exchange data through network buffers allocated from a fixed pool. When a downstream operator is slow, its input buffers fill up. This prevents the upstream operator from writing output, causing its output buffers to fill, propagating the pressure all the way back to the source. Flink exposes backpressure metrics per operator, and sustained backpressure on a specific operator pinpoints the bottleneck.

---

## 6. Adaptive Concurrency Limits

### 6.1 Why Fixed Limits Fail

Every service has some concurrency limit: the maximum number of requests it can process simultaneously before performance degrades. The problem is that this limit is not a constant. It varies with request mix (cheap vs. expensive), downstream latency, GC pauses, contention, and a dozen other factors. A fixed limit set too high allows overload; set too low, it wastes capacity. The solution is to make the limit adaptive -- adjusting automatically based on observed system behavior.

### 6.2 Closed-Loop Control Theory

Adaptive concurrency limits are instances of a closed-loop feedback control system. Understanding the underlying control theory prevents common implementation mistakes:

```
THE CONTROL LOOP:

                ┌──────────────────────────────┐
                │                              │
                ▼                              │
  Incoming ──► Controller ──► Service ──► Metrics
  traffic      │              │              │
               │              ▼              │
               └──── Adjust limit ◄──────────┘

  The controller observes signals:
    - Queue depth
    - Request latency (RTT)
    - Error rate
    - In-flight request count
    - CPU / memory pressure

  It adjusts the allowed concurrency limit.

  CONTROL-LOOP PROPERTIES:

  ┌──────────────────────┬──────────────────────────────────────────┐
  │ Property             │ Why it matters                            │
  ├──────────────────────┼──────────────────────────────────────────┤
  │ Feedback delay       │ Time between adjustment and observed      │
  │                      │ effect. Longer delay = harder to control. │
  │                      │ Network RTT + queue drain time.           │
  ├──────────────────────┼──────────────────────────────────────────┤
  │ Measurement noise    │ Latency spikes from GC, network jitter,  │
  │                      │ cold caches. Raw signals are too noisy    │
  │                      │ for direct control. Requires smoothing.   │
  ├──────────────────────┼──────────────────────────────────────────┤
  │ Oscillation          │ Controller overreacts: drops limit too    │
  │                      │ low → load drops → latency improves →    │
  │                      │ raises limit too high → overloads again.  │
  │                      │ The system never reaches steady state.    │
  ├──────────────────────┼──────────────────────────────────────────┤
  │ Overshoot            │ Controller raises limit faster than the   │
  │                      │ system can absorb. Causes transient       │
  │                      │ overload during ramp-up.                  │
  ├──────────────────────┼──────────────────────────────────────────┤
  │ Undershoot           │ Controller is too conservative. Keeps     │
  │                      │ limit low long after overload subsides.   │
  │                      │ Wastes capacity.                          │
  ├──────────────────────┼──────────────────────────────────────────┤
  │ Stability            │ The controller must converge to a steady  │
  │                      │ limit under constant load rather than     │
  │                      │ oscillating indefinitely.                 │
  └──────────────────────┴──────────────────────────────────────────┘

  DESIGN TRADEOFFS:

  Aggressive controller (reacts quickly):
    + Responds fast to overload (protects service)
    - Overreacts to transient spikes (GC pauses, cold starts)
    - Oscillates if feedback delay is significant

  Conservative controller (reacts slowly):
    + Stable, smooth limit changes
    + Tolerates measurement noise
    - Slow to protect under sudden load spikes
    - May allow overload during the detection window

  Both AIMD and the Netflix gradient algorithm are specific
  answers to this tradeoff. AIMD is conservative (slow increase,
  fast decrease). The gradient algorithm uses latency ratio as
  a continuous signal rather than binary success/failure.
```

### 6.3 AIMD (Additive Increase, Multiplicative Decrease)

AIMD originates from TCP congestion control (Jacobson 1988) and applies directly to service concurrency management. The algorithm is simple: on each successful request, increase the concurrency limit by a small constant (additive increase). On each failure -- timeout, error, or latency spike -- cut the limit in half (multiplicative decrease).

```
AIMD SAWTOOTH PATTERN:

  Concurrency
  Limit
     │
  60 │         *
     │        * *
  50 │       *   *
     │      *     *             *
  40 │     *       *           * *
     │    *         *         *   *
  30 │   *           *       *     *
     │  *             *     *       *
  20 │ *               *   *         *
     │*                 * *           *
  10 │                   *             * ...
     │
     └──────────────────────────────────────────
                        Time

  AIMD RULES:
    - Success: limit = limit + (1 / limit)    [additive increase]
    - Failure: limit = limit * 0.5             [multiplicative decrease]

  Properties:
    - Converges to optimal concurrency over time
    - Reacts quickly to overload (halving)
    - Recovers slowly (prevents oscillation)
    - The sawtooth pattern is expected and healthy
    - Multiple independent clients converge to fair sharing
```

### 6.4 Netflix Concurrency Limits (Gradient Algorithm)

Netflix's open-source `concurrency-limits` library uses a more sophisticated approach based on the TCP Vegas congestion control algorithm. Instead of reacting only to failures, it continuously measures latency as a signal for congestion.

```
NETFLIX GRADIENT ALGORITHM:

  Core idea: measure the relationship between current RTT and
  the minimum observed RTT (approximation of no-load latency).

  gradient = RTT_noload / RTT_actual

  Interpretation:
    gradient ≈ 1.0  → system is not congested, latency matches baseline
                       → increase concurrency limit
    gradient < 1.0  → system is congested, latency elevated
                       → decrease concurrency limit
    gradient > 1.0  → impossible in theory, but noise/measurement
                       jitter can cause this → treat as 1.0

  Limit update:
    new_limit = current_limit * gradient + queue_size

  Where queue_size is a configurable sqrt(current_limit) buffer
  that allows some queuing headroom.

  RTT_noload estimation:
    - Maintained as an exponentially decaying minimum
    - Periodically reset to avoid stale baselines
    - Reset window: every 1000 requests or 60 seconds
    - This prevents the algorithm from being fooled by a
      permanently elevated baseline (e.g., after a deploy
      that increased real service time)

  SMOOTHING:
    The raw gradient is noisy. Netflix applies exponential
    smoothing to the limit:
      smoothed_limit = 0.8 * smoothed_limit + 0.2 * new_limit

  EXAMPLE:
    Service time baseline:  10ms (RTT_noload)
    Current RTT:            15ms (RTT_actual)
    gradient = 10/15 = 0.67
    Current limit: 100
    new_limit = 100 * 0.67 + sqrt(100) = 67 + 10 = 77
    → Concurrency limit decreased from 100 to 77
```

Netflix applies this algorithm on both the client side (limiting outbound request concurrency to a backend) and the server side (limiting inbound request acceptance). The library integrates with gRPC interceptors, Servlet filters, and Envoy external authorization.

### 6.5 Token Bucket vs. Leaky Bucket

These are distinct flow-shaping algorithms often confused with each other:

```
TOKEN BUCKET:                          LEAKY BUCKET:

  Tokens added at rate r               Requests processed at rate r
  Bucket holds max b tokens            Bucket holds max b requests

  ┌──────────────┐                     ┌──────────────┐
  │  ○ ○ ○ ○ ○   │ ← tokens           │  ● ● ● ● ●   │ ← requests
  │  ○ ○ ○       │   added at          │  ● ● ●       │   arrive
  │              │   fixed rate        │              │   bursty
  └──────┬───────┘                     └──────┬───────┘
         │                                    │
    request arrives,                     requests drain
    consumes 1 token                     at fixed rate
    (if available)                       (smoothed output)
         │                                    │
         ▼                                    ▼
    ALLOWS BURSTS                        SMOOTHS BURSTS
    up to bucket size                    output is constant

  Token bucket:
    - Permits bursts (if tokens accumulated during idle)
    - Commonly used for API rate limiting
    - Allows "credit" for quiet periods
    - Used by: Linux tc, Envoy, NGINX

  Leaky bucket:
    - Enforces constant output rate
    - No burst credit -- excess is dropped or queued
    - Used when downstream truly cannot handle bursts
    - Used by: network traffic shaping, ATM networks
```

### 6.6 Integration with Circuit Breakers

Adaptive concurrency limits and circuit breakers are complementary, not redundant. The concurrency limit controls how many requests are allowed in flight to a dependency. The circuit breaker decides whether to allow any requests at all. A natural integration: when the concurrency limit drops below a minimum viable threshold (e.g., 3), the circuit breaker opens, routing traffic to a fallback or returning errors immediately. When the limit recovers above the threshold, the circuit breaker enters half-open and begins probing.

---

## 7. Queue Collapse and Bufferbloat

### 7.1 What Is Queue Collapse?

Queue collapse occurs when every item in a queue has expired by the time it reaches the head. The server processes items, but every response arrives after the client's deadline. Goodput drops to zero while the server remains at 100% utilization -- the worst possible state. It is doing maximum work with zero value.

```
QUEUE COLLAPSE:

  Time ──────────────────────────────────────►

  Queue state at T=0:    [A][B][C][D][E][F][G][H][I][J]
  Each request has a 2-second timeout.

  Server processes 1 request per second.

  T=0:  Process A (arrived at T=-9). Client timed out 7 seconds ago. WASTED.
  T=1:  Process B (arrived at T=-8). Client timed out 6 seconds ago. WASTED.
  T=2:  Process C (arrived at T=-7). Client timed out 5 seconds ago. WASTED.
  ...
  T=9:  Process J (arrived at T=0).  Client timed out. WASTED.

  Meanwhile, new requests K, L, M, N arrive and join the back of the queue.
  They will ALSO expire before reaching the head.

  Result: 100% CPU utilization, 0% goodput. Indefinitely.
```

### 7.2 CoDel for Service Queues

CoDel (Controlled Delay), designed by Kathleen Nichols and Van Jacobson for network routers, translates to service request queues with adaptation.

```
CoDel ALGORITHM FLOW:

  ┌──────────────────────────────────────────────────────────────┐
  │  REQUEST ARRIVES                                              │
  │    record enqueue_time = now()                                │
  └──────────────────────────┬───────────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  REQUEST DEQUEUED FOR PROCESSING                              │
  │    sojourn_time = now() - enqueue_time                        │
  └──────────────────────────┬───────────────────────────────────┘
                             │
                             ▼
                   ┌─────────────────────┐
                   │ sojourn_time        │
                   │ > TARGET (5ms)?     │
                   └────┬──────────┬─────┘
                    YES │          │ NO
                        ▼          ▼
              ┌─────────────┐  ┌──────────────────────┐
              │ Has this     │  │ Reset dropping state  │
              │ been true    │  │ (queue is healthy)    │
              │ for > INTERVAL│  └──────────────────────┘
              │ (100ms)?     │
              └───┬─────┬───┘
               YES│     │NO
                  ▼     ▼
          ┌──────────┐ ┌──────────┐
          │ DROP the  │ │ PROCESS  │
          │ request   │ │ normally │
          │ (signal   │ └──────────┘
          │ overload) │
          └──────────┘

  Drop schedule (after entering dropping state):
    Drop at intervals of: INTERVAL / sqrt(drop_count)

    drop_count=1 → drop every 100ms
    drop_count=4 → drop every  50ms
    drop_count=9 → drop every  33ms

    This inverse-square-root schedule increases drop rate
    gradually, converging on the minimum drop rate needed
    to keep sojourn times below TARGET.
```

### 7.3 LIFO vs. FIFO Under Overload

Under normal operation, FIFO is fair: first come, first served. Under overload, FIFO is catastrophic -- the freshest requests (most likely to still have deadline remaining) wait behind the oldest requests (most likely already expired). This is why some systems switch to LIFO (stack) processing under overload: process the most recent request first, because it has the most remaining deadline budget.

The tradeoff: LIFO is inherently unfair (early requests starve), so it should only activate during detected overload, not as the default scheduling policy. Google's SRE documentation describes this as a "controlled unfairness" -- under overload, unfair-but-completing is strictly better than fair-but-completing-nothing.

### 7.4 Bounded Queues

Every queue in a production system must have a maximum depth. Unbounded queues are a reliability hazard because they convert a latency problem into a memory problem. When a queue hits its bound:
- **Drop newest (tail drop):** simplest, used by most network queues
- **Drop oldest (head drop):** better under overload -- discards the most-expired items
- **Drop random:** CoDel-like behavior without tracking sojourn times

Request coalescing is another technique: if multiple requests in the queue are for the same key or resource, merge them into a single request. This is particularly effective for cache-miss storms where hundreds of requests queue up for the same cold cache key.

---

## 8. Rate Limiting

### 8.1 Token Bucket Implementation

The token bucket is the most common rate limiting algorithm in production. A bucket holds tokens; tokens are added at a fixed rate `r` up to a maximum burst size `b`. Each request consumes one token. If the bucket is empty, the request is rejected.

```
TOKEN BUCKET STATE MACHINE:

  State: { tokens: float, last_refill: timestamp }

  On request arrival:
    1. elapsed = now() - last_refill
    2. tokens = min(tokens + elapsed * rate, burst_size)
    3. last_refill = now()
    4. if tokens >= 1.0:
         tokens -= 1.0
         ALLOW
       else:
         REJECT (429 Too Many Requests)

  Example: rate = 100/sec, burst = 200
    After 2 seconds idle: tokens = min(0 + 2*100, 200) = 200
    Next 200 requests: allowed instantly (burst)
    Request 201: rejected until next refill
```

### 8.2 Sliding Window Algorithms

**Fixed window.** Divide time into fixed windows (e.g., 1-minute intervals). Count requests per window. Simple but has an edge problem: 100 requests at 0:59 and 100 at 1:01 pass a 100/minute limit even though 200 requests arrived within 2 seconds.

**Sliding log.** Store the timestamp of every request. Count requests in the trailing window. Accurate but expensive: O(n) storage where n is the number of requests in the window.

**Sliding window counter.** Hybrid approach: maintain counts for the current and previous fixed window. Weight the previous window's count by the fraction of overlap with the current sliding window. This approximates the sliding log with O(1) storage.

```
SLIDING WINDOW COUNTER:

  Window size: 60 seconds
  Limit: 100 requests per window
  Current time: 1:45 (45 seconds into the current window)

  Previous window (1:00-1:59): 80 requests
  Current window (2:00-2:59):  30 requests so far

  Overlap of previous window: (60 - 45) / 60 = 0.25

  Weighted count = 30 + (80 * 0.25) = 30 + 20 = 50
  50 < 100 → ALLOW
```

### 8.3 Distributed Rate Limiting

Single-node rate limiting is straightforward. Distributed rate limiting -- enforcing a global limit across multiple service instances -- is hard. The two dominant approaches:

**Centralized counter (Redis).** All instances increment a shared counter in Redis. Atomicity is achieved with Lua scripts or `MULTI/EXEC` transactions. This is the approach used by most API gateways (Kong, Envoy).

```
REDIS TOKEN BUCKET (Lua script):

  -- KEYS[1] = rate limit key
  -- ARGV[1] = max tokens (burst)
  -- ARGV[2] = refill rate (tokens/sec)
  -- ARGV[3] = current timestamp (seconds, float)
  -- ARGV[4] = tokens to consume (usually 1)

  local key = KEYS[1]
  local max_tokens = tonumber(ARGV[1])
  local refill_rate = tonumber(ARGV[2])
  local now = tonumber(ARGV[3])
  local requested = tonumber(ARGV[4])

  local bucket = redis.call('hmget', key, 'tokens', 'last_refill')
  local tokens = tonumber(bucket[1]) or max_tokens
  local last_refill = tonumber(bucket[2]) or now

  local elapsed = math.max(0, now - last_refill)
  tokens = math.min(max_tokens, tokens + elapsed * refill_rate)

  local allowed = tokens >= requested
  if allowed then
    tokens = tokens - requested
  end

  redis.call('hmset', key, 'tokens', tokens, 'last_refill', now)
  redis.call('expire', key, math.ceil(max_tokens / refill_rate) * 2)

  return allowed and 1 or 0
```

The limitation of centralized rate limiting: every request requires a round trip to Redis, adding 0.5-2ms of latency. Under high throughput, Redis itself becomes the bottleneck.

**Local rate limiting with synchronization.** Each instance maintains a local token bucket initialized with `global_limit / num_instances`. Periodically (every 1-10 seconds), instances synchronize unused tokens through a coordination service. This reduces Redis round trips but allows short bursts above the global limit during synchronization gaps. Envoy uses this approach via its rate limit service.

### 8.4 Rate Limit Headers

Standard response headers communicate rate limit state to clients:

```
HTTP/1.1 429 Too Many Requests
X-RateLimit-Limit: 1000           ← max requests per window
X-RateLimit-Remaining: 0          ← requests remaining in current window
X-RateLimit-Reset: 1625000000     ← Unix timestamp when window resets
Retry-After: 30                   ← seconds to wait before retrying
```

The IETF draft `RateLimit` header (draft-ietf-httpapi-ratelimit-headers) standardizes these as `RateLimit-Limit`, `RateLimit-Remaining`, and `RateLimit-Reset`. Production APIs should support both the `X-` prefixed and standardized variants during the transition period.

---

## 9. Fairness and Multi-Tenant Isolation

### 9.1 Priority vs. Fairness

Priority-based shedding answers "who should be served first?" Fairness answers "how do we prevent one participant from monopolizing the system?" These are complementary but distinct problems:

```
PRIORITY WITHOUT FAIRNESS:

  Shared Service (capacity: 1000 req/s)

  Tenant A (same priority as B and C) sends 5000 req/s
  Tenant B sends 100 req/s
  Tenant C sends 100 req/s

  With priority-based shedding only:
    All tenants are the same priority, so shedding is random.
    Tenant A gets ~962 req/s (5000/5200 * 1000)
    Tenant B gets ~19 req/s  (100/5200 * 1000)
    Tenant C gets ~19 req/s  (100/5200 * 1000)

    Tenant A's burst steals 80% of B and C's capacity.
    B and C did nothing wrong.

  With fairness (per-tenant isolation):
    Each tenant gets up to 333 req/s (1000/3 fair share).
    Tenant A gets 333 req/s (capped at fair share)
    Tenant B gets 100 req/s (under their share, served fully)
    Tenant C gets 100 req/s (under their share, served fully)
    Remaining 467 req/s can go to Tenant A (if no reserved capacity).

  FAIRNESS PROTECTS WELL-BEHAVED TENANTS FROM NOISY NEIGHBORS.
```

### 9.2 Weighted Fair Queuing (WFQ)

WFQ assigns each tenant (or priority class) a weight. The scheduler serves requests proportional to their weights, preventing any single tenant from monopolizing the system:

```
WEIGHTED FAIR QUEUING:

  Tenant A: weight 5  (enterprise SLA)
  Tenant B: weight 3  (business tier)
  Tenant C: weight 1  (free tier)

  Total weight: 9
  Total capacity: 900 req/s

  Fair shares:
    Tenant A: 500 req/s (5/9 * 900)
    Tenant B: 300 req/s (3/9 * 900)
    Tenant C: 100 req/s (1/9 * 900)

  If Tenant A sends only 200 req/s:
    Surplus 300 req/s redistributed proportionally to B and C.

  Work-conserving: no capacity sits idle while requests wait.
```

### 9.3 Resource Isolation Mechanisms

```
ISOLATION STRATEGIES:

  ┌──────────────────────────┬─────────────────────────────────────┐
  │ Mechanism                │ Behavior                             │
  ├──────────────────────────┼─────────────────────────────────────┤
  │ Per-tenant concurrency   │ Each tenant limited to N concurrent  │
  │ limits                   │ requests. Simple. May waste capacity │
  │                          │ if a tenant is idle.                 │
  ├──────────────────────────┼─────────────────────────────────────┤
  │ Reserved capacity        │ Each tenant guaranteed a minimum.    │
  │                          │ Excess capacity shared. Prevents     │
  │                          │ starvation.                          │
  ├──────────────────────────┼─────────────────────────────────────┤
  │ Hierarchical rate limits │ Global limit → per-tenant limit →   │
  │                          │ per-endpoint limit. Cascading        │
  │                          │ enforcement.                         │
  ├──────────────────────────┼─────────────────────────────────────┤
  │ Separate thread pools    │ Each tenant gets its own thread pool.│
  │                          │ Strong isolation but wastes threads   │
  │                          │ when tenants are idle.               │
  ├──────────────────────────┼─────────────────────────────────────┤
  │ Separate queues          │ Each tenant's requests queue         │
  │                          │ independently. Workers round-robin   │
  │                          │ across queues (or weighted).         │
  ├──────────────────────────┼─────────────────────────────────────┤
  │ Physical isolation       │ Separate instances per tenant.       │
  │ (shard-per-tenant)       │ Strongest isolation but highest      │
  │                          │ cost. Used for largest/most critical │
  │                          │ tenants.                             │
  └──────────────────────────┴─────────────────────────────────────┘
```

### 9.4 Priority Inversion and Starvation

Two failure modes to guard against in multi-tenant systems:

**Priority inversion** occurs when a high-priority request depends on a resource held by a low-priority request:

```
PRIORITY INVERSION:

  High-priority Tenant A request needs a database connection.
  All connections are held by low-priority Tenant C batch jobs.
  Tenant C jobs are being preempted by medium-priority Tenant B.

  Result: Tenant A (highest priority) waits for Tenant C (lowest).
  Tenant C cannot complete because Tenant B keeps preempting it.

  SOLUTION: priority inheritance — temporarily boost Tenant C's
  priority so it can release the resource Tenant A needs.
```

**Starvation** occurs when low-priority work never runs because high-priority work is always present:

```
STARVATION:

  Tenant A sends continuous high-priority traffic.
  Tenant C's low-priority requests never reach the head of the queue.

  SOLUTION: aging — increase the effective priority of waiting requests
  over time. After 30 seconds in the queue, a low-priority request
  is promoted to medium. After 60 seconds, to high.
  This guarantees eventual service for all priorities.
```

---

## 10. Graceful Degradation Under Load

### 10.1 The Degradation Ladder

Graceful degradation means progressively reducing functionality to protect core features. Define explicit tiers of degradation, ordered from least impactful to most impactful:

```
THE DEGRADATION LADDER:

  Load Level    Action                              User Impact
  ──────────    ──────────────────────────────────  ─────────────────────
  Normal        Full feature set                    None

  Level 1       Disable non-critical background     None visible.
  (75% CPU)     work: analytics events,             Background analytics
                log enrichment, pre-fetching        delayed.

  Level 2       Reduce response fidelity:           Slightly less
  (85% CPU)     fewer recommendations (20→5),       personalized.
                smaller search result pages          Pagination affected.
                (100→25), skip spell-check

  Level 3       Switch from real-time to cached:    Data may be 5-30
  (90% CPU)     serve stale data from cache,        minutes stale.
                disable real-time aggregations      Stale badge shown.

  Level 4       Disable entire non-critical         Features visibly
  (95% CPU)     features: reviews, comments,        missing. Core
                related products, social feeds      transaction path
                                                    still works.

  Level 5       Static fallback page or             Major degradation.
  (99% CPU)     maintenance mode. Only health       Users see limited
                checks and critical auth paths.     functionality.
```

### 10.2 Implementation Mechanisms

**Feature flags.** Each degradation level maps to a set of feature flags. A central configuration service (LaunchDarkly, Unleash, or a simple etcd key) flips flags based on load signals. The flags must be evaluated locally (cached) rather than fetched per-request, or the feature flag system itself becomes a bottleneck under load.

**Response fidelity reduction.** Instead of binary on/off, reduce the cost of features that remain active. A recommendation engine can return 5 results instead of 20. A search index can skip expensive re-ranking. An analytics pipeline can sample at 10% instead of 100%. These reductions are often invisible to users but dramatically reduce backend load.

**Stale-while-revalidate.** Serve cached responses immediately while asynchronously refreshing the cache. Under normal load, the cache is refreshed within milliseconds and the user gets fresh data. Under overload, stale responses are served for minutes or hours, but the service never blocks waiting for a backend that is too slow to respond.

---

## 11. Recovery from Overload and Metastability

### 11.1 The Recovery Problem

A system can remain overloaded even after the original traffic spike disappears. This is the most counterintuitive aspect of overload: removing the initial cause does not automatically restore normal operation.

```
WHY OVERLOAD PERSISTS AFTER THE SPIKE:

  ┌─────────────────────────────────┐
  │  1. Traffic spike arrives       │
  │     (external event, marketing  │
  │     campaign, cron storm)       │
  └──────────────┬──────────────────┘
                 ▼
  ┌─────────────────────────────────┐
  │  2. Queue grows, latency rises  │
  └──────────────┬──────────────────┘
                 ▼
  ┌─────────────────────────────────┐
  │  3. Clients timeout and retry   │
  └──────────────┬──────────────────┘
                 ▼
  ┌─────────────────────────────────┐
  │  4. Retry traffic sustains the  │◄────┐
  │     overload independently of   │     │
  │     the original spike          │     │
  └──────────────┬──────────────────┘     │
                 ▼                        │
  ┌─────────────────────────────────┐     │
  │  5. Original spike ends.        │     │
  │     But retry backlog remains.  │     │
  │     Queue is still full.        │     │
  │     New retries keep arriving.  ├─────┘
  └─────────────────────────────────┘

  THE KEY PRINCIPLE:
    The system must reduce offered load BELOW sustainable capacity
    long enough for the backlog to drain.
    Simply adding capacity or waiting for the spike to end
    may not be enough.
```

### 11.2 Metastable Failures

A metastable failure is a self-sustaining failure state that persists even after the triggering event resolves. The system has two stable states: normal operation and overloaded. A sufficiently large perturbation pushes it from normal to overloaded, and it stays overloaded indefinitely without intervention.

```
METASTABLE FAILURE STATES:

  State diagram:

  Normal operation ◄──── recovery threshold ────► Overloaded
        │                                              │
        │ (small perturbations                         │ (self-sustaining
        │  absorbed)                                   │  via retries,
        │                                              │  queue buildup,
        └──────────────────────────────────────────────┘  cache misses)

  Common metastable triggers:
    - Retry storms (most common)
    - Cache invalidation cascade (thundering herd on cold cache)
    - Connection pool exhaustion with queued waiters
    - Lock contention under high concurrency
    - GC pressure from excessive object allocation

  Each trigger has a SUSTAINING FEEDBACK LOOP:
    Retries → more load → more failures → more retries
    Cache misses → more DB load → slower DB → more timeouts → more retries
    GC pauses → slower responses → more in-flight requests → more memory → more GC
```

### 11.3 Recovery Strategies

```
RECOVERY MECHANISMS:

  1. RETRY SUPPRESSION DURING RECOVERY
     When the system detects it is recovering from overload:
       - Reject ALL retries (identified by retry headers/metadata)
       - Accept only original requests
       - Gradually re-enable retries as backlog drains

  2. GRADUAL TRAFFIC RAMP-UP
     Do not instantly restore full traffic after an outage:
       - Start at 10% of normal traffic
       - Increase by 10% every 30 seconds
       - Monitor latency at each step
       - If latency spikes, hold or reduce
     Load balancers call this "slow start."

  3. QUEUE DRAINING
     When recovering, the existing queue is mostly expired work:
       - Flush the entire queue (all items past deadline)
       - Process only newly arriving requests
       - This immediately restores goodput from 0% to near-capacity

  4. CACHE WARMING
     After an outage, caches are cold:
       - Stale-while-revalidate prevents a thundering herd
       - Pre-warm critical cache keys before accepting traffic
       - Limit the cache-miss rate (e.g., max 10 concurrent misses
         per key using singleflight/request coalescing)

  5. AVOIDING SYNCHRONIZED RECOVERY
     If all clients retry at the same time after an outage:
       - Jitter on Retry-After headers
       - Random delay before reconnection
       - Staggered health check intervals
     Without this, recovery itself triggers a new overload.

  6. ADMISSION CONTROL DURING RECOVERY
     Tighter admission thresholds during recovery than normal:
       - Normal: reject above 90% utilization
       - Recovery: reject above 60% utilization
       - Gradually relax to normal thresholds
     This prevents the system from immediately falling back
     into overload during recovery.
```

### 11.4 Overload Testing

A system that has never been tested under sustained overload has unknown behavior under sustained overload. Testing must be systematic:

```
OVERLOAD TESTING SCENARIOS:

  ┌────────────────────────────┬──────────────────────────────────────┐
  │ Scenario                   │ What it validates                     │
  ├────────────────────────────┼──────────────────────────────────────┤
  │ Sustained overload         │ Does goodput stay at capacity?        │
  │ (2x capacity for 10 min)  │ Does the system shed correctly?       │
  │                            │ Does it recover when load drops?      │
  ├────────────────────────────┼──────────────────────────────────────┤
  │ Sharp spike                │ Does the system shed before the       │
  │ (0→10x in 1 second)       │ queue fills? Is the spike absorbed?   │
  ├────────────────────────────┼──────────────────────────────────────┤
  │ Slow ramp                  │ At what utilization does latency      │
  │ (gradual increase to 2x)  │ become unacceptable? Does the         │
  │                            │ adaptive limit engage?                │
  ├────────────────────────────┼──────────────────────────────────────┤
  │ Dependency failure         │ Does the service shed requests to     │
  │ (DB goes slow or down)    │ the failed dependency while serving   │
  │                            │ requests to healthy dependencies?     │
  ├────────────────────────────┼──────────────────────────────────────┤
  │ Recovery after overload    │ Does the system recover within        │
  │ (spike then drop to 0.5x) │ seconds, or does it stay wedged?      │
  │                            │ Is there a metastable failure?        │
  ├────────────────────────────┼──────────────────────────────────────┤
  │ Multi-tenant overload      │ Does one tenant's burst affect        │
  │ (one tenant 10x, others   │ other tenants? Is fairness enforced? │
  │ normal)                    │                                       │
  └────────────────────────────┴──────────────────────────────────────┘

  CONTROL-PLANE PROTECTION:
    During overload testing, verify that these still work:
      - Health checks (must respond even under 10x load)
      - Monitoring and metrics emission
      - Configuration updates and feature flag changes
      - Admin endpoints (circuit breaker controls, drain)
    If the control plane is overwhelmed by data-plane overload,
    operators cannot observe or fix the problem.
```

---

## 12. Production Design Tradeoff Matrix

```
┌─────────────────────┬───────────────┬──────────────┬──────────────────────┬───────────────────────────┐
│ Mechanism           │ Latency       │ Complexity   │ Failure Mode         │ Used By                   │
│                     │ Impact        │              │                      │                           │
├─────────────────────┼───────────────┼──────────────┼──────────────────────┼───────────────────────────┤
│ Admission Control   │ None (fast    │ Medium       │ Over-rejection during│ Google (per-client        │
│                     │ rejection)    │              │ transient conditions │ quotas), Envoy            │
├─────────────────────┼───────────────┼──────────────┼──────────────────────┼───────────────────────────┤
│ Load Shedding       │ None (fast    │ Low-Medium   │ Over-shedding drops  │ Google (criticality),     │
│ (priority-based)    │ rejection)    │              │ valid requests       │ Envoy, AWS ALB            │
├─────────────────────┼───────────────┼──────────────┼──────────────────────┼───────────────────────────┤
│ Backpressure        │ Adds latency  │ Medium       │ Deadlocks if chain   │ Flink, Kafka, Reactive    │
│ (end-to-end)        │ (intentional) │              │ forms a cycle        │ Streams, TCP/HTTP2        │
├─────────────────────┼───────────────┼──────────────┼──────────────────────┼───────────────────────────┤
│ AIMD Concurrency    │ Low           │ Low          │ Slow convergence     │ TCP, Netflix concurrency  │
│ Limits              │               │              │ after misestimate    │ limits library            │
├─────────────────────┼───────────────┼──────────────┼──────────────────────┼───────────────────────────┤
│ Gradient Concurrency│ Low           │ Medium       │ Baseline RTT drift   │ Netflix, Envoy            │
│ (Vegas-based)       │               │              │ causes miscalibration│ ext_authz integration     │
├─────────────────────┼───────────────┼──────────────┼──────────────────────┼───────────────────────────┤
│ Token Bucket        │ None          │ Low          │ Redis SPOF in        │ NGINX, Kong, Envoy,       │
│ Rate Limiting       │               │              │ distributed mode     │ Cloudflare, AWS API GW    │
├─────────────────────┼───────────────┼──────────────┼──────────────────────┼───────────────────────────┤
│ CoDel Queue Mgmt    │ Reduces tail  │ Medium       │ Aggressive drops     │ Linux kernel (fq_codel),  │
│                     │ latency       │              │ during short bursts  │ Envoy, application queues │
├─────────────────────┼───────────────┼──────────────┼──────────────────────┼───────────────────────────┤
│ Fair Queuing        │ Low           │ Medium-High  │ Weight misconfigura- │ Envoy, gRPC, custom       │
│ (WFQ, per-tenant)   │               │              │ tion starves tenants │ middleware                │
├─────────────────────┼───────────────┼──────────────┼──────────────────────┼───────────────────────────┤
│ Circuit Breaker     │ None (fast    │ Low          │ Premature opening    │ Hystrix, resilience4j,    │
│                     │ fail)         │              │ blocks valid traffic │ Linkerd, Istio            │
├─────────────────────┼───────────────┼──────────────┼──────────────────────┼───────────────────────────┤
│ Graceful            │ Variable      │ High         │ Feature flag         │ Netflix (Zuul),           │
│ Degradation         │ (depends on   │              │ misconfiguration     │ Facebook, Google          │
│                     │ shed features)│              │ removes critical     │ (GFE/Maglev)             │
│                     │               │              │ functionality        │                           │
├─────────────────────┼───────────────┼──────────────┼──────────────────────┼───────────────────────────┤
│ Deadline            │ Reduces waste │ Medium       │ Overly aggressive    │ gRPC deadline             │
│ Propagation         │               │              │ deadlines reject     │ propagation, Google       │
│                     │               │              │ slow-but-valid work  │ internal services         │
└─────────────────────┴───────────────┴──────────────┴──────────────────────┴───────────────────────────┘
```

### Choosing the Right Combination

No single mechanism is sufficient. Production systems layer multiple controls:

```
DEFENSE-IN-DEPTH LAYERING (recommended for production):

  Layer 1: Rate Limiting at Edge
    ├── Global rate limits per client/API key (token bucket)
    ├── Connection limits per source IP
    └── Request size limits

  Layer 2: Admission Control
    ├── Capacity-based admission (CPU, connections, memory)
    ├── Deadline-aware admission (reject expired requests)
    ├── Per-tenant admission (noisy-neighbor protection)
    └── Dependency-aware admission (downstream health)

  Layer 3: Adaptive Concurrency Limits (client-side)
    ├── AIMD or gradient-based limit per downstream dependency
    ├── Circuit breaker integration
    └── Retry budgets (max 10% of requests are retries)

  Layer 4: Load Shedding at Service
    ├── CPU-based rejection threshold
    ├── Priority/criticality-based progressive shedding
    └── Deadline-based request expiry

  Layer 5: Queue Management
    ├── Bounded queue depths
    ├── CoDel or deadline-aware scheduling
    └── LIFO fallback under detected overload

  Layer 6: Fairness and Isolation
    ├── Per-tenant concurrency limits
    ├── Weighted fair queuing
    └── Reserved capacity guarantees

  Layer 7: Graceful Degradation
    ├── Feature flags tied to load signals
    ├── Response fidelity reduction
    └── Stale-while-revalidate caching

  Layer 8: Backpressure Propagation
    ├── HTTP/2 and gRPC flow control
    ├── Retry-After headers on all 429/503 responses
    ├── Deadline and cancellation propagation
    └── Health check degradation signals to load balancers
```

The key principle is defense in depth: each layer catches what the previous layer missed. Rate limiting at the edge prevents bulk abuse. Admission control prevents the service from accepting work it cannot complete. Concurrency limits protect individual service-to-service paths. Load shedding protects the server itself. Queue management prevents resource waste. Fairness prevents noisy neighbors. Degradation preserves core functionality. Backpressure propagates signals upstream so the entire system adapts, rather than one component absorbing all the pain.

The most dangerous configuration is having only one layer of defense. If your only protection is a rate limit at the API gateway, then a single internal service generating excessive retries will bypass it entirely and cascade through the backend. If your only protection is a circuit breaker, then slow responses (not failures) will slip through because the circuit breaker only counts errors, not latency. Staff-level engineering means understanding that each mechanism has blind spots, and layering them so the gaps do not align.

---

> **Further reading:** Google SRE Book, Chapter 21 "Handling Overload"; Netflix Technology Blog, "Performance Under Load" (2018); Kathleen Nichols & Van Jacobson, "Controlling Queue Delay" (CoDel, ACM Queue 2012); TCP Congestion Avoidance (Jacobson, 1988); Amazon Builders' Library, "Using load shedding to avoid overload"; Bronson et al., "Metastable Failures in Distributed Systems" (OSDI 2022); Kingman, "The single server queue in heavy traffic" (1961).
