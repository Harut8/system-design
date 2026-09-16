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
13. [Cloud-Native Ownership — App vs. Infra vs. Hybrid](#13-cloud-native-ownership--app-vs-infra-vs-hybrid)
14. [Interview Preparation — Adaptive Load Control & Backpressure](#14-interview-preparation--adaptive-load-control--backpressure)
15. [Sandbox Experiments — Run These Yourself](#15-sandbox-experiments--run-these-yourself)

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

## 13. Cloud-Native Ownership — App vs. Infra vs. Hybrid

The previous sections describe what mechanisms exist. This section answers the question production engineers actually fight about: **who owns each mechanism?** In a cloud-native Kubernetes environment with message queues, the boundary between application team and platform/infra team is precise, not blurry. Getting this wrong means either gaps (nobody owns the mechanism, it is not configured) or conflicts (both teams configure it differently, they interfere).

### 13.1 The Ownership Principle

```
THE LINE:

  If the decision requires DOMAIN KNOWLEDGE    → app team owns it.
  If the decision requires INFRASTRUCTURE KNOWLEDGE → infra/platform team owns it.
  If it requires BOTH → app team sets the POLICY, infra team ENFORCES it.

  EXAMPLES:

  "Which requests are sheddable?"
    → Requires domain knowledge (is this a checkout or an analytics query?)
    → APP owns it.

  "How many pods should run?"
    → Requires infrastructure knowledge (cluster capacity, node sizing)
    → INFRA owns it.

  "What is the rate limit per tenant?"
    → Requires domain knowledge (tier, SLA, revenue)
    → APP team defines the values.
    → INFRA team enforces them (Istio, NGINX, API gateway config).
    → HYBRID.
```

### 13.2 Full Ownership Map — Kubernetes + Message Queues

```
┌──────────────────────────────────────────────────────────────────────┐
│                     INFRA / PLATFORM TEAM OWNS                       │
│                                                                      │
│  KUBERNETES:                                                         │
│    HPA autoscaling rules (CPU/memory targets, min/max replicas)      │
│    Resource limits and requests (cpu, memory per container)          │
│    Pod disruption budgets                                            │
│    Network policies                                                  │
│    Ingress controller operations (NGINX, Istio gateway)             │
│    Node pool sizing and instance types                               │
│    Namespace quotas                                                  │
│                                                                      │
│  SERVICE MESH (Istio / Linkerd):                                     │
│    mTLS between services                                             │
│    Mesh-level retry policies (coarse: "retry 503s once")            │
│    Outlier detection (eject pods with >5% error rate)               │
│    Traffic shifting (canary, blue-green weight)                      │
│    Global rate limiting enforcement                                  │
│                                                                      │
│  MESSAGE INFRASTRUCTURE:                                             │
│    Kafka broker cluster operations (broker count, disk, replication) │
│    RabbitMQ cluster nodes, HA policies, mirroring                    │
│    Redis cluster / sentinel operations, memory limits               │
│    Topic/queue/exchange provisioning automation                      │
│    Retention policies, compaction                                    │
│    Schema registry infrastructure                                    │
│    Dead-letter queue infrastructure (DLQ exists, routing configured) │
│    Monitoring infrastructure (consumer lag dashboards)               │
│                                                                      │
│  DATABASE:                                                           │
│    PgBouncer / ProxySQL (connection pooler between app and DB)       │
│    max_connections tuning                                            │
│    Read replica provisioning and failover                            │
│    Backup, PITR, disaster recovery                                   │
│    Instance sizing (RDS instance class, disk IOPS)                   │
│                                                                      │
│  OBSERVABILITY:                                                      │
│    Prometheus / Datadog / Grafana infrastructure                     │
│    Log aggregation pipeline (Loki, ELK, Fluentd)                    │
│    Distributed tracing backend (Jaeger, Tempo)                       │
│    Alert routing infrastructure (PagerDuty, OpsGenie integration)   │
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│                     APP / BACKEND TEAM OWNS                          │
│                                                                      │
│  FASTAPI / APPLICATION CODE:                                         │
│    Concurrency limits (asyncio.Semaphore, --limit-concurrency)      │
│    Admission control logic (accept/reject based on capacity)        │
│    Deadline propagation (set and check X-Request-Deadline headers)  │
│    Priority classification (which requests are CRITICAL vs SHEDDABLE)│
│    Per-tenant fairness logic (weighted fair queuing in middleware)   │
│    Graceful degradation (feature flags, response fidelity reduction) │
│    Backpressure signaling (429 + Retry-After response semantics)    │
│    Connection pool sizing per pod (SQLAlchemy pool_size)            │
│    Query timeouts (SET statement_timeout in queries)                │
│    Application-level circuit breakers with domain-aware fallbacks   │
│    Request validation and input size limits                          │
│    Cost estimation per request type                                  │
│                                                                      │
│  HEALTH/READINESS ENDPOINT LOGIC:                                    │
│    What "healthy" means (infra hosts the probe, app defines it)     │
│    Readiness: "can I serve traffic?" (check DB pool, cache, deps)   │
│    Liveness: "is my process stuck?" (event loop responsive)         │
│    Startup: "have I finished initializing?" (cache warmed, etc.)    │
│                                                                      │
│  KAFKA CONSUMER CODE:                                                │
│    Consumer group design and partition assignment strategy            │
│    Partition key strategy (determines ordering and parallelism)      │
│    max.poll.records (batch size per poll — controls throughput)      │
│    Consumer pause() / resume() logic (application-level backpressure)│
│    Offset commit strategy (at-least-once vs exactly-once semantics) │
│    Deserialization, processing, and error handling                    │
│    Dead-letter topic HANDLING logic (what to do with failed msgs)   │
│    Idempotency in consumer processing                                │
│                                                                      │
│  RABBITMQ CONSUMER CODE:                                             │
│    prefetch_count (QoS) — THE most important backpressure knob      │
│    ack / nack / reject semantics per message type                   │
│    Queue declaration, binding, and routing key design                │
│    TTL per message or per queue (application SLA)                    │
│    Publisher confirms (producer-side reliability)                     │
│    Consumer concurrency (how many messages processed in parallel)    │
│    Dead-letter exchange routing logic                                 │
│                                                                      │
│  CELERY / REDIS-BACKED WORKERS:                                      │
│    Task routing (which queue for which task type)                    │
│    rate_limit per task class ("100/m" for email, unlimited for pay)  │
│    acks_late + reject_on_worker_lost (at-least-once delivery)       │
│    Task result backend choice (Redis, DB, or disabled)               │
│    Max queue length enforcement (LLEN check before enqueue)          │
│    Worker concurrency and prefork/eventlet/gevent selection          │
│    Task retry policy (max_retries, backoff, jitter)                  │
│    Task priority routing (separate queues for critical vs bulk)     │
│                                                                      │
│  OBSERVABILITY CONTENT:                                              │
│    What metrics to emit (latency histograms, error rates by type)   │
│    Alert thresholds (p99 > 200ms → warning, > 500ms → page)        │
│    SLO definitions (99.9% of requests < 300ms)                       │
│    Dashboard content (per-tenant latency, per-endpoint throughput)  │
│    Structured log content (trace IDs, tenant IDs, request cost)     │
└──────────────────────────────────────────────────────────────────────┘
```

### 13.3 Why the App MUST Own Admission Control, Priority, and Degradation

Infra can throttle traffic. Only the app knows what the traffic **means**.

```
WHAT INFRA SEES vs. WHAT THE APP KNOWS:

  ┌──────────────────────────┬────────────────────────────────────────┐
  │ Infra sees               │ App knows                              │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ POST /api/v1/orders      │ $50,000 enterprise order from Tenant A│
  │ (an HTTP request)        │ needs fraud check + inventory          │
  │                          │ reservation + payment auth.            │
  │                          │ Priority: CRITICAL. Cost: 500ms.       │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ CPU at 85%               │ 80% of CPU is processing SHEDDABLE    │
  │ (a resource metric)      │ analytics batch jobs. The CRITICAL     │
  │                          │ order path uses 5% CPU. Shedding       │
  │                          │ analytics frees 80% capacity.          │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ Kafka consumer lag:      │ 49,950 are notification emails (can    │
  │ 50,000 messages          │ wait 30 min). 50 are payment events   │
  │ (a queue depth)          │ that need processing in <5 seconds.    │
  │                          │ Scaling consumers helps emails but     │
  │                          │ the payment events need priority       │
  │                          │ routing to a dedicated consumer group. │
  ├──────────────────────────┼────────────────────────────────────────┤
  │ 429 from downstream      │ The 429 is from a recommendation      │
  │ (an HTTP error)          │ service. The product page can render   │
  │                          │ without recommendations. Serve a       │
  │                          │ degraded page. Do NOT fail the request.│
  ├──────────────────────────┼────────────────────────────────────────┤
  │ Database connections:    │ 40 connections held by a reporting      │
  │ 48/50 used               │ query that runs every hour. Killing    │
  │ (pool exhaustion)        │ the report frees 40 connections.       │
  │                          │ Infra would scale pods (making it      │
  │                          │ worse — more pods = more connections). │
  └──────────────────────────┴────────────────────────────────────────┘

  CONSEQUENCE:
    Infra-only autoscaling cannot make these decisions.
    An HPA that scales on CPU would ADD pods when analytics consume
    85% CPU, creating MORE database connections and making the DB
    problem WORSE. Only the app knows that shedding analytics
    (not adding pods) is the correct response.
```

### 13.4 The Hybrid Mechanisms — Who Sets Policy vs. Who Enforces

These are the mechanisms that cause the most team friction because both teams must participate:

```
HYBRID OWNERSHIP — POLICY vs. ENFORCEMENT:

  ┌──────────────────────┬─────────────────────┬────────────────────────┐
  │ Mechanism            │ App team sets        │ Infra team enforces    │
  ├──────────────────────┼─────────────────────┼────────────────────────┤
  │ Rate limiting        │ Rate values per      │ NGINX limit_req_zone,  │
  │                      │ tenant/tier/endpoint │ Istio EnvoyFilter,     │
  │                      │ (free=10, biz=100,   │ Kong rate-limiting     │
  │                      │ enterprise=1000 RPS) │ plugin                 │
  ├──────────────────────┼─────────────────────┼────────────────────────┤
  │ Connection pool      │ pool_size per pod    │ PgBouncer max_client   │
  │ sizing               │ based on query       │ _conn, PostgreSQL      │
  │                      │ profile and latency  │ max_connections.       │
  │                      │                      │ ALL THREE must agree.  │
  ├──────────────────────┼─────────────────────┼────────────────────────┤
  │ Autoscaling          │ Which metric to scale│ HPA configuration,     │
  │                      │ on (CPU? queue depth?│ KEDA ScaledObject,     │
  │                      │ custom metric?) and  │ cluster autoscaler     │
  │                      │ min/max replicas     │ node provisioning      │
  ├──────────────────────┼─────────────────────┼────────────────────────┤
  │ Resource limits      │ Memory/CPU needs per │ Kubernetes resource    │
  │                      │ pod based on load    │ limits/requests in     │
  │                      │ testing data         │ deployment manifests   │
  ├──────────────────────┼─────────────────────┼────────────────────────┤
  │ Health probes        │ Endpoint logic:      │ Probe configuration:   │
  │                      │ what checks to run,  │ path, port, interval,  │
  │                      │ what "unhealthy"     │ timeout, threshold     │
  │                      │ means for this       │ in pod spec            │
  │                      │ service              │                        │
  ├──────────────────────┼─────────────────────┼────────────────────────┤
  │ Kafka partitioning   │ Partition key        │ Initial partition      │
  │                      │ strategy, requested  │ count, broker resource │
  │                      │ partition count for  │ allocation,            │
  │                      │ throughput target     │ rebalancing operations │
  ├──────────────────────┼─────────────────────┼────────────────────────┤
  │ Alerting             │ Alert thresholds:    │ Alert routing:         │
  │                      │ p99 > 200ms = warn,  │ PagerDuty integration, │
  │                      │ error rate > 1% =    │ escalation policies,   │
  │                      │ page                 │ on-call schedules      │
  ├──────────────────────┼─────────────────────┼────────────────────────┤
  │ Retry policy         │ Which errors to      │ Istio/Envoy retry      │
  │ (mesh-level)         │ retry, retry budget, │ configuration in       │
  │                      │ idempotency          │ VirtualService or      │
  │                      │ requirements         │ DestinationRule        │
  └──────────────────────┴─────────────────────┴────────────────────────┘

  THE COORDINATION CONTRACT:

  When the app team sizes their connection pool:
    App:  pool_size=20, max_overflow=10 per pod → 30 max per pod
    Infra: 10 pods × 30 = 300 connections needed

  PgBouncer must accept ≥ 300.
  PostgreSQL max_connections must accept PgBouncer's pool.
  Plus headroom for:
    - Rolling deploys (11th pod briefly runs)
    - DBA connections (psql, migrations)
    - Monitoring (pg_stat_activity, Datadog)
    - Connection pooler overhead

  If ANY of these three numbers is wrong, the system breaks
  under load. This is a contract between three teams (app, platform, DBA).
```

### 13.5 Message Queue Backpressure — The App-Side Knobs That Matter

In Kafka, RabbitMQ, and Redis-backed queues, the infra team provisions the infrastructure. The app team controls how fast messages are consumed and what happens when the consumer falls behind. These knobs are the most important and most frequently misconfigured:

```
KAFKA — App-Side Backpressure Controls:

  1. max.poll.records (default: 500)
     How many records consumer.poll() returns per call.
     Lower = less memory per batch, more frequent commits.
     Higher = better throughput, risk of rebalance timeout.

     BACKPRESSURE EFFECT:
       If processing 500 records takes 30s but max.poll.interval.ms is 5min,
       the consumer is slow but alive. If it takes 6 minutes, the consumer
       is kicked from the group → rebalance → lag spikes.

  2. Consumer pause() and resume()
     The app can pause individual partitions when overwhelmed:

       # Pseudo-code for Kafka consumer backpressure
       while True:
           records = consumer.poll(timeout=1.0)
           if database_pool.active_connections > threshold:
               consumer.pause(assigned_partitions)
               # Stop fetching until DB pressure drops
           elif consumer.paused():
               consumer.resume(paused_partitions)
           process(records)

     This is APPLICATION-LEVEL backpressure that infra cannot provide.
     The broker does not know your database is overloaded.

  3. Partition key strategy (determines parallelism ceiling)
     If all messages have the same key → one partition → one consumer max.
     No amount of infra scaling helps. This is a design decision
     the app team makes at message production time.

RABBITMQ — App-Side Backpressure Controls:

  1. prefetch_count (QoS) — THE critical knob
     How many unacknowledged messages the broker sends to a consumer.

     prefetch_count = 1:
       Consumer processes one message at a time.
       Maximum backpressure. Minimum throughput.
       Broker holds all other messages until ack.
       SAFE for expensive, failure-prone processing.

     prefetch_count = 50:
       Consumer buffers 50 messages locally.
       Good throughput. But if consumer crashes,
       50 messages are redelivered (nack + requeue).
       Risk of duplicate processing without idempotency.

     prefetch_count = 0 (unlimited):
       Broker sends ALL messages as fast as possible.
       Consumer memory grows without bound.
       NO backpressure. The consumer will OOM.
       NEVER use 0 in production.

     SIZING RULE:
       prefetch_count ≈ consumer_throughput × processing_latency
       If consumer processes 100 msg/s at 50ms each:
         prefetch_count = 100 * 0.05 = 5
       Set it to 2-3x this for burst headroom: 10-15.

  2. ack / nack / reject semantics
     ack:    message processed successfully. Remove from queue.
     nack:   processing failed. Requeue (retry) or dead-letter.
     reject: permanently reject. Route to dead-letter exchange.

     APP DECIDES which errors are retryable (nack + requeue)
     vs. permanent (reject → DLX). Infra cannot make this decision.

     Example:
       JSON parse error → reject (retrying won't fix bad data)
       Database timeout  → nack + requeue (transient, retry may work)
       Business rule violation → reject + log for human review

REDIS / CELERY — App-Side Backpressure Controls:

  1. Queue length enforcement (Redis has NO built-in limit)
     Redis lists grow without bound. The APP must check before enqueue:

       queue_length = redis.llen("celery_queue")
       if queue_length > MAX_QUEUE_LENGTH:
           raise HTTPException(status_code=429, detail="Queue full")
       # else enqueue the task

     WITHOUT this check, Redis memory grows until:
       maxmemory-policy = noeviction → writes fail (producer errors)
       maxmemory-policy = allkeys-lru → QUEUE DATA IS SILENTLY EVICTED

  2. rate_limit per task class
     Celery supports per-task rate limits:

       @app.task(rate_limit="100/m")    # max 100 emails per minute
       def send_notification_email(user_id):
           ...

       @app.task(rate_limit=None)       # unlimited for critical path
       def process_payment(order_id):
           ...

     This is application-level traffic shaping that the infra
     team cannot configure (they don't know which tasks are
     rate-sensitive).

  3. Task priority via queue routing
     Instead of one queue, route by priority:

       CELERY_TASK_ROUTES = {
           "payments.*":       {"queue": "critical"},
           "notifications.*":  {"queue": "normal"},
           "analytics.*":      {"queue": "bulk"},
       }

     Run dedicated workers per queue with different concurrency:
       celery -A app worker -Q critical --concurrency=20
       celery -A app worker -Q normal  --concurrency=10
       celery -A app worker -Q bulk    --concurrency=2

     This is a BULKHEAD pattern implemented at the queue level.
     Critical payment processing is isolated from bulk analytics.
```

### 13.6 The Complete Cloud-Native Request Path — Who Owns What

```
                     INFRA OWNS                           APP OWNS
                     ──────────                           ────────
  Internet
      │
      ▼
  ┌──────────┐
  │ Cloud LB │  ← DDoS protection, TLS termination,
  │ (ALB/NLB)│    connection limits, health routing
  └────┬─────┘
       │
       ▼
  ┌──────────┐
  │ Ingress  │  ← TLS, routing rules,                  ← rate limit VALUES
  │ (NGINX / │    request size enforcement                per tenant/tier
  │  Istio)  │                                            (app defines,
  └────┬─────┘                                             infra enforces)
       │
       ▼
  ┌──────────┐
  │ K8s Pod  │  ← resource limits, HPA scaling,        ← readiness endpoint
  │          │    disruption budgets, scheduling          LOGIC (what checks)
  │ ┌──────┐ │
  │ │Uvicorn│ │  ← worker count (ops tunes based       ← --limit-concurrency
  │ │      │ │    on load testing data from app),         (app sizes to their
  │ │      │ │    --limit-max-requests (recycling)        workload profile)
  │ │ ┌────┤ │
  │ │ │Fast│ │                                          ← admission control
  │ │ │API │ │                                          ← deadline propagation
  │ │ │    │ │                                          ← priority routing
  │ │ │    │ │                                          ← per-tenant fairness
  │ │ │    │ │                                          ← graceful degradation
  │ │ │    │ │                                          ← 429 + Retry-After
  │ │ │    │ │                                          ← circuit breakers
  │ │ └────┘ │                                            with domain fallbacks
  │ └──────┘ │
  └────┬─────┘
       │
       ├──────────────────────────┐
       │                          │
       ▼                          ▼
  ┌──────────┐             ┌───────────┐
  │PgBouncer │  ← pooler   │ Redis     │  ← cluster ops,
  │          │    ops,      │           │    maxmemory
  │          │    failover  │           │
  └────┬─────┘             └─────┬─────┘
       │                         │
       ▼                         ▼
  ┌──────────┐             ┌───────────┐
  │PostgreSQL│  ← instance │ Kafka     │  ← broker ops,
  │  (RDS)   │    sizing,  │           │    partitions,
  │          │    backups   │           │    retention
  └──────────┘             └───────────┘

  APP OWNS for DB:                    APP OWNS for queues:
    pool_size per pod                   consumer group design
    query timeouts                      max.poll.records
    slow query optimization             prefetch_count (RabbitMQ)
    index design                        ack/nack/reject logic
    read vs write routing               pause()/resume()
                                        queue length enforcement
                                        task priority routing
```

### 13.7 Common Ownership Failures

These are the real-world incidents that happen when the boundary is wrong:

```
FAILURE 1: Infra auto-scales during DB connection saturation

  Symptom:  Database connection errors under load.
  Infra:    "CPU is high, HPA scaled from 5 to 15 pods."
  Result:   5 pods × 30 conn = 150 (OK) → 15 pods × 30 conn = 450 (exceeds max_connections=200)
  Cause:    HPA scaled on CPU without considering database connection budget.
  Fix:      App team must define max replicas based on DB connection headroom.
            HPA max replicas = floor(max_connections * 0.7 / pool_size_per_pod)
            With max_connections=200 and pool_size=30: max replicas = floor(140/30) = 4

FAILURE 2: Mesh-level retries amplify database overload

  Symptom:  Database is slow. Error rate spikes to 90%.
  Infra:    Istio VirtualService has "retries: attempts: 3" for all services.
  Result:   Each failed DB-bound request is retried 3x at the mesh level.
            The app also retries internally. Total: 3 × 3 = 9 attempts per request.
            Database receives 9x normal load during the slowdown.
  Cause:    Mesh-level retry is too broad. It doesn't know which errors are retryable.
  Fix:      Mesh retries should ONLY retry on 503 and connection resets.
            App-level retries handle business logic (idempotency, backoff).
            Never retry at both layers for the same failure.

FAILURE 3: Kafka consumer scales but throughput doesn't increase

  Symptom:  Consumer lag is 500,000 messages. KEDA scaled to 20 consumers.
            Lag is not decreasing.
  Infra:    "We added more consumers. Why isn't it faster?"
  Cause:    Topic has 4 partitions. Maximum parallelism is 4 consumers.
            16 of the 20 consumers are idle (no partitions assigned).
  Fix:      APP team should have requested partition count matching
            their throughput target at topic creation time.
            Partition count = target_throughput / per_consumer_throughput
            If each consumer handles 1000 msg/s and target is 20,000:
            partitions = 20,000 / 1,000 = 20

FAILURE 4: Redis maxmemory evicts queue data silently

  Symptom:  Celery tasks are disappearing. No errors in logs.
  Infra:    Redis maxmemory-policy is allkeys-lru. Memory at 100%.
  Result:   Redis evicts the oldest list entries (queued tasks) to make
            room for new ones. Tasks silently vanish.
  Cause:    App team did not enforce max queue length before enqueue.
            Infra team set eviction policy without understanding that
            Redis is being used as a queue (not just a cache).
  Fix:      App: enforce queue length (LLEN check before LPUSH).
            Infra: set maxmemory-policy to noeviction for Redis
            instances used as task queues. Writes fail loudly
            instead of silently evicting.

FAILURE 5: Health probe kills pods under legitimate load

  Symptom:  During traffic spikes, pods restart repeatedly.
  Infra:    Liveness probe: GET /health, timeout 3s, period 10s,
            failure threshold 3.
  Result:   Under heavy load, /health handler (which queries the DB)
            takes 4 seconds. Probe fails. After 3 failures (30 seconds),
            Kubernetes restarts the pod. Cold restart + cache warming
            takes 30 seconds. Pod absorbs less load. Other pods get
            more traffic. They also start failing health checks.
            Cascading pod restarts.
  Cause:    App team put DB checks in the liveness probe.
            Liveness should ONLY check "is the process stuck?"
            Readiness checks "can I serve traffic?"
  Fix:      Liveness: simple in-process check (return 200, no I/O).
            Readiness: check DB connection, cache, downstream deps.
            Failed readiness → stop traffic (don't restart the pod).
            Failed liveness → restart (process is deadlocked).
```

### 13.8 The Coordination Checklist

Before any production deployment, these numbers must be coordinated across teams:

```
COORDINATION CHECKLIST:

  Database Connections:
  ┌──────────────────────────────────────────────────────────────┐
  │ App:  pool_size × max_pods = ___  connections needed         │
  │ Infra: PgBouncer max_client_conn = ___                      │
  │ DBA:  max_connections = ___                                  │
  │                                                              │
  │ RULE: max_connections > PgBouncer pool > (pool_size × pods)  │
  │       with 30% headroom for deploys + admin + monitoring     │
  └──────────────────────────────────────────────────────────────┘

  Autoscaling:
  ┌──────────────────────────────────────────────────────────────┐
  │ App:  max_replicas limited by DB connections? ___            │
  │ App:  max_replicas limited by downstream rate limits? ___    │
  │ Infra: HPA min/max replicas = ___                           │
  │ Infra: scale-up stabilization window = ___                  │
  │ Infra: scale-down stabilization window = ___                │
  │                                                              │
  │ RULE: max_replicas ≤ min(DB budget, downstream budget,      │
  │       cluster capacity) / per_pod_resource_usage             │
  └──────────────────────────────────────────────────────────────┘

  Message Queues:
  ┌──────────────────────────────────────────────────────────────┐
  │ App:  expected throughput = ___ msg/s                        │
  │ App:  per-consumer throughput = ___ msg/s                    │
  │ Infra: partition count = ___ (must ≥ max consumer count)    │
  │ Infra: retention = ___ hours                                │
  │ Infra: broker disk = ___ GB (retention × throughput × msg)  │
  │                                                              │
  │ RULE: partitions ≥ target_throughput / per_consumer_rate    │
  │       with room for 2-3x traffic spikes                     │
  └──────────────────────────────────────────────────────────────┘

  Rate Limiting:
  ┌──────────────────────────────────────────────────────────────┐
  │ App:  actual sustainable throughput = ___ RPS                │
  │ App:  per-tenant limits = ___                               │
  │ Infra: ingress rate limit = ___ RPS                         │
  │                                                              │
  │ RULE: ingress_limit ≤ actual_throughput                     │
  │       (never rate-limit above what the service can handle)  │
  │       sum(per_tenant_limits) may exceed total capacity      │
  │       (statistical multiplexing — not all tenants peak      │
  │       simultaneously), but each individual limit must be    │
  │       ≤ total capacity                                      │
  └──────────────────────────────────────────────────────────────┘

  Health Probes:
  ┌──────────────────────────────────────────────────────────────┐
  │ App:  liveness checks = ___ (process health only, no I/O)   │
  │ App:  readiness checks = ___ (deps, pool, cache)            │
  │ Infra: liveness period/timeout/threshold = ___              │
  │ Infra: readiness period/timeout/threshold = ___             │
  │                                                              │
  │ RULE: liveness timeout > worst-case event loop delay        │
  │       readiness timeout > worst-case DB ping time           │
  │       Use startup probes for slow-initializing services     │
  └──────────────────────────────────────────────────────────────┘
```

### 13.9 The Math and Logic Behind Standard Practices

This section answers the "how exactly?" question for every major app-owned and hybrid mechanism. Not just what to do — the calculations, sizing formulas, and decision logic that determine the correct values in production.

---

#### 13.9.1 Graceful Degradation — Decision Logic and Implementation

Graceful degradation means **reducing response quality to preserve availability**. The app team owns this because only domain knowledge determines what to drop.

```
THE DEGRADATION LADDER — ordered by impact:

  Level 0: FULL FIDELITY (normal operation)
    Serve complete response with all features.
    Cost: 100% of resources per request.

  Level 1: SKIP OPTIONAL ENRICHMENTS
    Drop: recommendations, related items, personalization.
    Save: 30-50% latency (removes 1-3 downstream calls).
    User impact: page works, feels less personalized.
    Trigger: p99 > 2× baseline OR dependency circuit open.

  Level 2: SERVE STALE DATA
    Drop: real-time prices, inventory counts, live status.
    Serve: cached version (stale by 30s-5min).
    Save: 60-80% DB load (cache hit rate goes to ~100%).
    User impact: data may be slightly outdated.
    Trigger: DB connection pool > 80% OR query p99 > 500ms.

  Level 3: REDUCE PAYLOAD SIZE
    Drop: large lists (100 items → 10), images, preview content.
    Save: 70% bandwidth, 40% serialization CPU.
    User impact: list views show fewer items, must paginate.
    Trigger: memory > 80% OR serialization time > 50ms.

  Level 4: STATIC FALLBACK
    Drop: all dynamic content.
    Serve: pre-rendered static page from CDN/local cache.
    Save: ~100% of backend resources.
    User impact: page is functional but non-interactive.
    Trigger: error rate > 20% OR all circuit breakers open.

  Level 5: MAINTENANCE MODE
    Drop: all user-facing functionality.
    Serve: "We're working on it" page + status page link.
    Save: 100% of backend resources.
    User impact: service is effectively down.
    Trigger: manual decision OR cascading failure detected.
```

```
IMPLEMENTATION IN FASTAPI — The Degradation Middleware Pattern:

  import time
  from enum import IntEnum

  class DegradationLevel(IntEnum):
      FULL = 0
      SKIP_ENRICHMENTS = 1
      SERVE_STALE = 2
      REDUCE_PAYLOAD = 3
      STATIC_FALLBACK = 4

  # The degradation controller — reads system metrics and decides level
  class DegradationController:
      def __init__(self):
          self.current_level = DegradationLevel.FULL
          self._last_check = 0
          self._check_interval = 1.0  # re-evaluate every 1 second

      def get_level(self, metrics: SystemMetrics) -> DegradationLevel:
          now = time.monotonic()
          if now - self._last_check < self._check_interval:
              return self.current_level
          self._last_check = now

          # Evaluate conditions — most severe first
          if metrics.error_rate > 0.20:
              self.current_level = DegradationLevel.STATIC_FALLBACK
          elif metrics.memory_pct > 80 or metrics.serialization_p99 > 0.050:
              self.current_level = DegradationLevel.REDUCE_PAYLOAD
          elif metrics.db_pool_pct > 80 or metrics.db_query_p99 > 0.500:
              self.current_level = DegradationLevel.SERVE_STALE
          elif metrics.overall_p99 > 2 * metrics.baseline_p99:
              self.current_level = DegradationLevel.SKIP_ENRICHMENTS
          else:
              self.current_level = DegradationLevel.FULL

          return self.current_level

  # In the endpoint handler
  @app.get("/products/{product_id}")
  async def get_product(product_id: str, request: Request):
      level = degradation_controller.get_level(current_metrics)

      product = await get_product_from_db(product_id)  # always needed

      if level <= DegradationLevel.SKIP_ENRICHMENTS:
          product.recommendations = await get_recommendations(product_id)
          product.reviews = await get_reviews(product_id)

      if level <= DegradationLevel.SERVE_STALE:
          product.price = await get_live_price(product_id)
          product.inventory = await get_inventory(product_id)
      else:
          product.price = await get_cached_price(product_id)  # stale OK
          product.inventory = None  # omit rather than show wrong count

      if level >= DegradationLevel.REDUCE_PAYLOAD:
          product.related_items = product.related_items[:5]  # trim list

      return product

  KEY INSIGHT:
    The degradation controller evaluates SYSTEM metrics.
    The endpoint handler applies DOMAIN-SPECIFIC degradation.
    The controller says "we're at level 2."
    The handler knows "level 2 means serve stale prices but
    NEVER serve stale inventory counts for products with
    low stock" — that's domain knowledge.
```

```
QUANTIFYING THE SAVINGS — How to calculate degradation impact:

  EXAMPLE: Product page with 5 downstream calls:

    Call             Latency   CPU/req   Connections   Required?
    ──────────────   ───────   ───────   ───────────   ─────────
    product_db       10ms      2ms       1 DB conn     YES (core data)
    pricing_svc      30ms      1ms       1 HTTP conn   YES at L0-L1
    inventory_svc    20ms      1ms       1 HTTP conn   YES at L0-L1
    recommend_svc    80ms      5ms       1 HTTP conn   NO at L1+
    review_svc       50ms      3ms       1 HTTP conn   NO at L1+

  At Level 0 (full fidelity):
    Serial latency:   10 + 30 + 20 + 80 + 50 = 190ms
    Parallel latency:  max(10, 30, 20, 80, 50) = 80ms
    CPU per request:   2 + 1 + 1 + 5 + 3 = 12ms
    Connections used:  1 DB + 4 HTTP = 5

  At Level 1 (skip enrichments — drop recommend + reviews):
    Parallel latency:  max(10, 30, 20) = 30ms          → 62% faster
    CPU per request:   2 + 1 + 1 = 4ms                 → 67% less CPU
    Connections used:  1 DB + 2 HTTP = 3                → 40% fewer conns
    Capacity increase: 12ms / 4ms = 3× more RPS per core

  At Level 2 (serve stale — cached pricing + no inventory):
    Parallel latency:  max(10, 0.1) = 10ms              → 87% faster
    CPU per request:   2 + 0.01 = 2.01ms                → 83% less CPU
    Connections used:  1 DB + 0 HTTP = 1                 → 80% fewer conns
    Capacity increase: 12ms / 2ms = 6× more RPS per core

  TAKEAWAY:
    Level 1 degradation triples your effective capacity.
    Level 2 degradation gives you 6× capacity.
    Most services can handle traffic spikes by shedding
    optional enrichments, without any infrastructure changes.
    This is why the APP must own degradation — only the app
    knows which 80ms call is optional and which 10ms call is critical.
```

---

#### 13.9.2 Backpressure Signaling — The HTTP 429 + Retry-After Contract

The app decides WHEN to signal backpressure and WHAT information to include. The infra layer (NGINX, Istio) can enforce rate limits, but only the app can signal meaningful retry semantics.

```
THE 429 RESPONSE CONTRACT — What the server communicates:

  HTTP/1.1 429 Too Many Requests
  Retry-After: 5                          ← "try again in 5 seconds"
  X-RateLimit-Limit: 1000                 ← "your tier allows 1000 req/min"
  X-RateLimit-Remaining: 0               ← "you have 0 left in this window"
  X-RateLimit-Reset: 1694800800           ← "window resets at this Unix time"
  Content-Type: application/json

  {"error": "rate_limit_exceeded", "retry_after": 5}

  EACH HEADER'S PURPOSE:
    Retry-After:          Tells client WHEN to retry.
    X-RateLimit-Limit:    Tells client their budget.
    X-RateLimit-Remaining: Tells client how close they are.
    X-RateLimit-Reset:    Tells client when budget refills.

  WITHOUT these headers: the client guesses. Guessing = thundering herd.
```

```
CALCULATING Retry-After — The Math:

  GOAL: spread retries so they don't create a synchronized spike.

  SCENARIO: 1000 RPS service, capacity drops to 500 RPS.
    500 requests/s are rejected with 429.

  FIXED Retry-After: 5
    All 500 rejected requests retry at T+5.
    At T+5: 500 retries + 1000 new requests = 1500 RPS.
    Service is overloaded again. Another 1000 rejected.
    OSCILLATION: 500→1500→500→1500 RPS pattern.

  JITTERED Retry-After: uniform(2, 10)
    500 rejected clients spread retries over 8 seconds.
    Retry rate: 500 / 8 = 62.5 retries/s (mixed into normal traffic).
    At any given second: ~1000 new + ~63 retries = ~1063 RPS.
    Service overloaded by 63 RPS, not 500 RPS. Manageable.

  FORMULA for server-side jitter:
    base_delay = current_queue_depth / processing_rate
    jittered_delay = base_delay + random.uniform(0, base_delay)

  EXAMPLE:
    Queue depth: 200 requests.
    Processing rate: 100 req/s.
    Drain time (base_delay): 200 / 100 = 2 seconds.
    Retry-After: uniform(2, 4) seconds.

    This tells clients: "I'll be busy for ~2s, try again in 2-4s."
    The jitter ensures they don't all arrive at 2.0s.

  ADAPTIVE Retry-After based on load:
    if current_load < 0.7 * capacity:
        retry_after = 1  # light load, retry soon
    elif current_load < 0.9 * capacity:
        retry_after = random.uniform(2, 5)
    elif current_load < 1.0 * capacity:
        retry_after = random.uniform(5, 15)
    else:  # over capacity
        retry_after = random.uniform(10, 30)

  This creates BACKPRESSURE PROPORTIONAL TO LOAD:
    Light overload → retry in 1-5s (fast recovery).
    Heavy overload → retry in 10-30s (let system drain).
```

```
WHEN THE APP SHOULD RETURN 429 vs 503:

  429 Too Many Requests:
    Meaning: "You're sending too fast. Slow down."
    Client action: Respect Retry-After, then retry same request.
    Use when: Per-client or per-tenant rate limit exceeded.
    The REQUEST is valid. The RATE is too high.

  503 Service Unavailable:
    Meaning: "I can't handle this right now. Try later."
    Client action: Retry with exponential backoff.
    Use when: Server is globally overloaded (not client-specific).
    The SYSTEM is overloaded. Any client would get this.

  DECISION LOGIC:
    if request.tenant over their rate limit:
        return 429 + Retry-After  # client-specific problem
    elif system.at_capacity():
        return 503 + Retry-After  # system-wide problem
    elif request.deadline_expired():
        return 504 Gateway Timeout  # too late to process

  WHY IT MATTERS:
    A well-behaved client treats 429 and 503 differently:
    429 → slow down THIS client's request rate.
    503 → the whole service is down, switch to fallback/cache.
    Wrong status code = wrong client behavior = wrong recovery.
```

---

#### 13.9.3 Graceful Shutdown — The SIGTERM Sequence with Math

Graceful shutdown is the process of stopping a pod without dropping in-flight requests. The app owns the shutdown logic; Kubernetes controls the timeline.

```
THE KUBERNETES SHUTDOWN TIMELINE:

  T=0s    Kubernetes sends SIGTERM to the pod.
          Pod is SIMULTANEOUSLY removed from the Service endpoint list.
          But endpoint propagation is ASYNCHRONOUS — load balancers
          may not know for 1-5 seconds.

  T=0-5s  DANGER ZONE: new requests may still arrive because
          kube-proxy / Envoy / NGINX haven't updated their backends yet.
          The pod MUST still accept and process these requests.

  T=5s+   Endpoint propagation complete. No new traffic arrives.
          Pod is draining in-flight requests.

  T=Ns    terminationGracePeriodSeconds expires (default: 30s).
          Kubernetes sends SIGKILL. Process dies immediately.
          ANY in-flight requests are dropped.

  CRITICAL MATH:
    terminationGracePeriodSeconds must be >
      endpoint_propagation_delay + max(in_flight_request_duration)

    With Istio:   propagation ≈ 1-3s
    With NGINX:   propagation ≈ 1-5s
    With kube-proxy (iptables): propagation ≈ 1-10s

    If your slowest request takes 30s (e.g., a report generation):
      terminationGracePeriodSeconds = 10 + 30 + 5 = 45s (minimum)

    FORMULA:
      grace_period = propagation_delay + max_request_duration + safety_margin
      grace_period = 5 + max_request_duration + 5
```

```
IMPLEMENTATION IN FASTAPI — Proper SIGTERM Handling:

  import signal
  import asyncio
  from contextlib import asynccontextmanager

  shutdown_event = asyncio.Event()
  active_requests = 0  # atomic counter (use threading.Lock or asyncio-safe)

  @asynccontextmanager
  async def lifespan(app: FastAPI):
      # Startup
      loop = asyncio.get_event_loop()

      def handle_sigterm(sig, frame):
          shutdown_event.set()  # signal that we're draining

      signal.signal(signal.SIGTERM, handle_sigterm)
      yield
      # Shutdown — wait for in-flight requests to complete
      # (Uvicorn handles this, but custom async tasks need manual drain)

  app = FastAPI(lifespan=lifespan)

  @app.middleware("http")
  async def shutdown_middleware(request: Request, call_next):
      global active_requests

      if shutdown_event.is_set():
          # During shutdown: reject NEW requests with 503
          # so clients retry on other pods.
          return JSONResponse(
              status_code=503,
              headers={"Retry-After": "1", "Connection": "close"},
              content={"error": "shutting_down"}
          )

      active_requests += 1
      try:
          response = await call_next(request)
          return response
      finally:
          active_requests -= 1

  THE preStop HOOK — Adding delay for endpoint propagation:

    containers:
    - name: api
      lifecycle:
        preStop:
          exec:
            command: ["sh", "-c", "sleep 5"]
      # This 5-second sleep runs BEFORE SIGTERM reaches the app.
      # It gives kube-proxy / Envoy time to remove this pod
      # from the endpoint list BEFORE the app starts rejecting.

  COMPLETE TIMELINE WITH preStop:

    T=0s    Kubernetes starts shutdown.
    T=0s    preStop hook runs: sleep 5. Pod STILL serves traffic.
    T=0-5s  Endpoint propagation happens in parallel.
    T=5s    preStop finishes. SIGTERM sent to app.
    T=5s    App stops accepting NEW requests (503 for stragglers).
    T=5s+   App drains in-flight requests.
    T=30s   terminationGracePeriodSeconds expires. SIGKILL.

    GRACE PERIOD MATH:
      Total shutdown budget = terminationGracePeriodSeconds = 30s
      preStop sleep: 5s
      Remaining for app drain: 30 - 5 = 25s
      If max request duration > 25s, increase the grace period.
```

```
CONNECTION DRAINING MATH:

  SCENARIO: Pod handling 500 RPS, average latency 20ms.
    Concurrent requests at SIGTERM time:
      L = λ × W = 500 × 0.020 = 10 concurrent requests

    Drain time for 10 in-flight requests at 20ms each:
      If they all started just before SIGTERM: max(remaining_time) ≈ 20ms
      99th percentile drain time ≈ 100ms (some requests slower)

    WITH SLOW REQUESTS (5% take 5 seconds):
      Expected slow requests in flight: 500 × 0.05 × 5 = 125 concurrent
      Drain time: up to 5 seconds for the slowest one.

    WITH DATABASE TRANSACTIONS:
      A transaction holding a row lock must complete.
      If a transaction takes 2s and started at T-1s: needs 1s more.
      If a transaction takes 30s (batch job): needs up to 30s.
      This is why batch jobs should check the shutdown signal
      and commit partial progress:

        for batch in chunks(items, size=100):
            if shutdown_event.is_set():
                break  # commit what we have, let next pod continue
            process(batch)
            db.commit()
```

---

#### 13.9.4 Connection Pool Sizing — The Complete Formula

Connection pool sizing is a HYBRID responsibility: the app team sizes the pool based on workload; the infra/DBA team sizes the database and pooler based on aggregate demand.

```
THE THREE-LAYER CONNECTION BUDGET:

  Layer 1: APPLICATION (SQLAlchemy pool per pod)
  Layer 2: CONNECTION POOLER (PgBouncer between app and DB)
  Layer 3: DATABASE (PostgreSQL max_connections)

  Each layer has limits. The system breaks at the TIGHTEST limit.

  ┌─────────────────────────────────────────────────┐
  │  App Pod 1    App Pod 2    ...    App Pod N      │
  │  pool=20      pool=20             pool=20       │
  │  overflow=10  overflow=10         overflow=10   │
  │     │            │                    │          │
  │  30 max       30 max             30 max          │
  │  per pod      per pod            per pod         │
  └──────┬──────────┬──────────────────┬────────────┘
         │          │                  │
         ▼          ▼                  ▼
  ┌─────────────────────────────────────────────────┐
  │           PgBouncer (connection pooler)          │
  │  max_client_conn = 500   (accepts from apps)     │
  │  default_pool_size = 50  (holds to database)     │
  │                                                   │
  │  MULTIPLEXING: 500 app connections share 50 DB   │
  │  connections. PgBouncer queues when all 50 busy. │
  └────────────────────┬────────────────────────────┘
                       │
                       ▼
  ┌─────────────────────────────────────────────────┐
  │           PostgreSQL                              │
  │  max_connections = 100                            │
  │    50 for PgBouncer                               │
  │    10 for monitoring (Datadog, pg_stat)           │
  │    10 for migrations / DBA                        │
  │    5 for replication slots                         │
  │    25 reserved headroom                            │
  └─────────────────────────────────────────────────┘
```

```
APP-SIDE POOL SIZING FORMULA:

  INPUTS:
    RPS_per_pod    = requests per second this pod handles
    query_per_req  = average DB queries per request
    query_latency  = average query duration (seconds)
    headroom       = 1.5 (50% safety margin)

  FORMULA (from Little's Law):
    connections_needed = RPS_per_pod × query_per_req × query_latency × headroom

  EXAMPLE:
    RPS_per_pod   = 200
    query_per_req = 3
    query_latency = 0.005 (5ms)
    headroom      = 1.5

    connections_needed = 200 × 3 × 0.005 × 1.5 = 4.5 → round up to 5

    pool_size = 5  (persistent connections, always open)
    max_overflow = 5  (burst connections, created on demand, closed when idle)
    Total max per pod = 10

  BUT ADD VARIANCE:
    If 5% of queries take 200ms (slow queries, aggregations):
      slow_query_connections = RPS_per_pod × query_per_req × 0.05 × 0.200
                             = 200 × 3 × 0.05 × 0.200 = 6 connections
    These 6 connections are held 40× longer than the fast ones.
    They dominate pool usage even though they're 5% of queries.

    ADJUSTED:
      fast_query_conns = 200 × 3 × 0.95 × 0.005 = 2.85
      slow_query_conns = 200 × 3 × 0.05 × 0.200 = 6.0
      total = 2.85 + 6.0 = 8.85 → round to 10

    pool_size = 10, max_overflow = 5
    Total max per pod = 15

  THE SLOW QUERY TRAP:
    5% of queries consume 68% of connections (6 / 8.85).
    This is why "our average query is 5ms" does not mean
    "we need 5 connections." The TAIL matters.

  PRACTICAL SQLALCHEMY SETTINGS:
    pool_size=10         # baseline persistent connections
    max_overflow=5       # burst capacity
    pool_timeout=3       # fail fast if pool exhausted (not 30s default!)
    pool_recycle=3600    # recycle connections every hour
    pool_pre_ping=True   # detect stale connections before use
```

```
AGGREGATE BUDGET — The Cross-Team Coordination:

  GIVEN:
    10 pods × 15 connections max = 150 connections from app
    + 1 rolling-deploy pod × 15 = 15 connections (deploy surge)
    + 5 monitoring connections (Datadog, pg_stat_activity)
    + 5 DBA/migration connections
    + 3 replication slots
    ────────────────────────────
    Total: 178 connections needed

  WITHOUT PgBouncer:
    PostgreSQL max_connections = 178 + 20% headroom = 214
    Each PostgreSQL connection costs ~5-10MB of RAM.
    214 connections × 10MB = 2.14GB just for connection state.
    RDS db.r5.large (16GB RAM) loses 13% to connections alone.

  WITH PgBouncer (transaction mode):
    PgBouncer max_client_conn = 200 (accepts all app connections)
    PgBouncer default_pool_size = 25 (actual DB connections)
    PgBouncer reserve_pool_size = 5 (for slow transactions)

    PostgreSQL max_connections = 25 + 5 + 10 + 5 = 45
    45 connections × 10MB = 450MB. Much better.

    WHY THIS WORKS (the multiplexing math):
      In transaction pooling mode, PgBouncer assigns a DB connection
      only for the duration of a transaction (BEGIN...COMMIT).
      A query taking 5ms uses a DB connection for 5ms out of every
      request (which might span 20ms including HTTP parsing, etc.).
      Utilization ratio: 5ms / 20ms = 25%.
      150 app connections × 25% utilization = 37.5 concurrent DB conns.
      A pool of 25 + 5 reserve = 30 handles this with headroom.

  FAILURE CASE — What happens without coordination:
    App team sets pool_size=30 (generous, seems safe for one pod).
    Infra scales to 20 pods for Black Friday.
    20 × 30 = 600 connections to PostgreSQL.
    max_connections = 200.
    RESULT: connection errors at scale. 400 connections refused.
    At 2 AM. On Black Friday. With the CEO watching dashboards.
```

---

#### 13.9.5 Pod and Worker Sizing — CPU, Memory, and Concurrency Math

```
UVICORN WORKER COUNT — Why the Gunicorn formula doesn't apply:

  GUNICORN FORMULA (sync workers):
    workers = 2 × CPU_CORES + 1
    Reasoning: each sync worker blocks on I/O. 2× CPU means when
    half the workers are blocked on I/O, the other half use the CPU.

  UVICORN FORMULA (async workers):
    workers = CPU_CORES (start with 1:1)
    Reasoning: each async worker runs an event loop that handles
    THOUSANDS of concurrent connections. More workers add overhead:
      - Each worker has its own memory space (~50-200MB base)
      - Each worker has its own DB connection pool
      - Each worker has its own HTTP client pool
      - Context switching between workers costs CPU

  THE MATH:
    Assume: 4-core pod, handler is 2ms CPU + 8ms I/O wait.

    With 1 async worker:
      CPU capacity: 1000ms / 2ms = 500 RPS of CPU work per core.
      But only 1 core is used. Max CPU-limited RPS = 500.
      I/O concurrency: event loop handles thousands of awaits.
      Bottleneck: CPU at 500 RPS.

    With 4 async workers:
      CPU capacity: 4 × 500 = 2000 RPS.
      Each worker uses 1 core. All 4 cores utilized.
      Memory: 4 × 150MB base = 600MB before any request state.

    With 8 async workers (the "2n+1" mistake):
      CPU capacity still 2000 RPS (only 4 cores exist).
      Memory: 8 × 150MB = 1.2GB.
      DB connections: 8 × pool_size. Probably too many.
      OS context switching: non-trivial at 8 processes.
      WORSE performance than 4 workers due to overhead.

  EXCEPTIONS WHERE >1 WORKER PER CORE HELPS:
    - Handler has synchronous blocking I/O (file reads, legacy sync DB)
    - Handler calls CPU-bound code that doesn't release the GIL
      (NumPy releases GIL; pure Python does not)
    - In these cases: 2 × cores is appropriate (mitigates blocking)
```

```
MEMORY SIZING — How to calculate pod memory limits:

  FORMULA:
    memory_limit = base_memory
                 + (max_concurrent_requests × per_request_memory)
                 + connection_pools
                 + caches
                 + safety_margin

  COMPONENTS:
    base_memory:
      Python interpreter:          ~30MB
      FastAPI + dependencies:      ~50-150MB (depends on imports)
      Uvicorn per worker:          ~20MB
      Total base per worker:       ~100-200MB

    per_request_memory:
      Request object:              1-5KB
      Parsed JSON body:            0.1-100KB (depends on payload)
      ORM objects loaded:          5-500KB (depends on query)
      Response serialization:      1-100KB
      Asyncio coroutine state:     2-8KB
      TOTAL:                       ~10KB typical, ~500KB worst case

    connection_pools:
      SQLAlchemy pool (10 conns):  ~5MB
      httpx client pool:           ~2MB
      Redis client:                ~1MB
      Total:                       ~10MB

    caches:
      In-memory LRU cache:         configured (e.g., 100MB)

  EXAMPLE:
    1 worker, limit-concurrency=200, 10KB per request average:
      base:         150MB
      requests:     200 × 10KB = 2MB
      pools:        10MB
      caches:       50MB
      Total:        212MB → set limit to 300MB (40% headroom)

    4 workers, limit-concurrency=200 each, 50KB per request (ORM-heavy):
      base:         4 × 150MB = 600MB
      requests:     4 × 200 × 50KB = 40MB
      pools:        4 × 10MB = 40MB
      caches:       50MB (shared via Redis, not per-worker)
      Total:        730MB → set limit to 1024MB (1Gi)

  THE OOM TRAP:
    If you set memory limit WITHOUT concurrency limit:
      Under 10,000 RPS spike with 100ms latency:
        Concurrent requests: 10,000 × 0.1 = 1000
        Memory: 1000 × 50KB = 50MB for requests alone. OK.
      Under 10,000 RPS spike with 2s latency (downstream slow):
        Concurrent requests: 10,000 × 2 = 20,000
        Memory: 20,000 × 50KB = 1GB for requests alone. OOM.

    Concurrency limit MUST be set so that:
      max_concurrent × per_request_memory < memory_limit - base - pools
      
    In the 1Gi example:
      budget = 1024MB - 600MB - 40MB - 50MB = 334MB for requests
      max_concurrent = 334MB / 50KB ≈ 6,800 total across 4 workers
      Per worker: 6,800 / 4 = 1,700
      BUT: at 1,700 concurrent requests per worker, the event loop
      is scheduling 1,700 coroutines. Performance degrades well
      before that. A practical limit: 200-500 per worker.
```

```
CPU REQUESTS AND LIMITS — The Kubernetes budgeting:

  CPU REQUEST = guaranteed CPU (used for scheduling).
  CPU LIMIT = max CPU (throttled if exceeded).

  SIZING LOGIC:
    CPU request = sustained_cpu_usage during normal load
    CPU limit = peak_cpu_usage during spikes (or no limit)

  MEASUREMENT:
    Run load test at target RPS. Observe CPU:
      At 1000 RPS: CPU usage = 200m (0.2 cores)
      At 2000 RPS: CPU usage = 400m (0.4 cores)
      Linear relationship: 0.2m per RPS.

    For a target of 2000 RPS per pod:
      CPU request: 400m (what it needs normally)
      CPU limit: 800m (2× headroom for GC, serialization spikes)

  CPU LIMIT DEBATE — Set or not?
    WITH limit (e.g., 800m):
      Pod gets throttled (CFS quota) when it hits 800m.
      Latency spikes during throttling. Requests slow down.
      BUT: pod doesn't starve other pods on the node.

    WITHOUT limit (only request):
      Pod can burst to full node CPU if available.
      Great for bursty workloads. No throttling.
      BUT: can steal CPU from other pods (noisy neighbor).

    STANDARD PRACTICE:
      Set request = measured sustained usage.
      Set limit = 2-4× request OR omit for burst-tolerant workloads.
      NEVER set limit < request (Kubernetes rejects it).
      NEVER set request = 0 (pod gets scheduled on any node,
        may land on an overloaded node with no guaranteed CPU).
```

---

#### 13.9.6 Message Consumption Sizing — Prefetch, Batch, and Ack Math

```
RABBITMQ prefetch_count — The derivation:

  GOAL: keep the consumer busy without buffering too many unacked messages.

  THE PIPELINE MODEL:
    Consumer has a "pipeline" of messages in flight:
      - Some are being processed (active)
      - Some are in the local buffer (prefetched, waiting)

    If the buffer empties, the consumer IDLES waiting for the broker
    to deliver the next batch. This is wasted throughput.

    If the buffer is too full, crash = N messages redelivered.

  FORMULA:
    prefetch_count = throughput × round_trip_time × safety_factor

    WHERE:
      throughput = messages processed per second by this consumer
      round_trip_time = time for broker to deliver next batch
                        after consumer acks (network + broker latency)
      safety_factor = 2-3 (buffer for variance)

  EXAMPLE 1: Fast processing, local broker
    throughput = 500 msg/s
    round_trip_time = 2ms (same datacenter)
    prefetch = 500 × 0.002 × 3 = 3 → set prefetch_count = 5

    The consumer processes 1 message every 2ms.
    Broker delivers next batch in 2ms.
    3-5 prefetched messages ensure the consumer never idles.

  EXAMPLE 2: Slow processing, remote broker
    throughput = 10 msg/s (100ms per message)
    round_trip_time = 50ms (cross-region)
    prefetch = 10 × 0.050 × 3 = 1.5 → set prefetch_count = 2

    Slow consumer: only 2 messages buffered. If it crashes,
    only 2 messages are redelivered. Safe.

  EXAMPLE 3: Batch processing
    throughput = 1000 msg/s (but processes in batches of 100)
    round_trip_time = 5ms
    prefetch = 100 (match batch size)

    Set prefetch = batch size so the consumer always has a full
    batch ready. But understand: crash = 100 messages redelivered.

  ANTI-PATTERNS:
    prefetch_count = 0 (unlimited):
      Broker dumps entire queue to consumer.
      10,000 messages × 2KB each = 20MB of local buffer.
      Consumer crash = 10,000 messages redelivered.
      Other consumers get NOTHING (all messages are prefetched here).
      THIS DEFEATS FAIR DISPATCH across multiple consumers.

    prefetch_count = 10000 (too high):
      Same as unlimited in practice.
      Messages are "invisible" to other consumers.
      Load balancing across consumers breaks down.

    prefetch_count = 1 everywhere:
      Safe but slow. Consumer processes 1, waits for ack round-trip,
      gets next message. If round_trip = 5ms:
        Max throughput = 1000ms / (processing_time + 5ms)
        For 2ms processing: 1000 / 7 = 142 msg/s max
        vs. prefetch=10: ~500 msg/s (pipeline is full)
```

```
KAFKA max.poll.records AND BATCH SIZING:

  KAFKA CONSUMER LOOP:
    while True:
        records = consumer.poll(timeout_ms=1000)
        process(records)  # must complete before max.poll.interval.ms
        consumer.commit()

  CONSTRAINT:
    processing_time(records) < max.poll.interval.ms (default: 5 minutes)

  IF VIOLATED:
    Consumer is considered dead → removed from group → rebalance.
    All partitions reassigned. Processing of current batch is wasted.
    Rebalance assigns same partitions to another consumer → reprocessing.

  SIZING:
    max.poll.records = max.poll.interval.ms / processing_time_per_record

  EXAMPLE:
    max.poll.interval.ms = 300,000 (5 minutes)
    processing per record = 50ms
    max.poll.records = 300,000 / 50 = 6,000

    But leave 50% headroom for variance:
    max.poll.records = 3,000

  WITH SAFETY MARGIN:
    max.poll.records = floor(max.poll.interval.ms / (2 × p99_processing_time))

    If p99 processing is 200ms:
      max.poll.records = floor(300,000 / (2 × 200)) = 750

  PRACTICAL TUNING:
    Start with max.poll.records = 100.
    Measure: how long does processing 100 records take?
    If 100 records take 2 seconds → well within 5-minute budget.
    Increase to 500. 500 records take 10 seconds. Still fine.
    Increase to 1000. Monitor. Stop when p99 batch time > 50% of
    max.poll.interval.ms.
```

```
CELERY WORKER CONCURRENCY — Prefork vs Eventlet vs Gevent:

  PREFORK (default — multiprocessing):
    Each worker is a separate OS process.
    concurrency = N means N processes.
    Each process handles 1 task at a time (synchronous).
    Best for: CPU-bound tasks (image processing, ML inference).

    SIZING:
      concurrency = CPU_CORES (CPU-bound tasks)
      Memory: N × per_process_memory (Python interpreter + task data)

    EXAMPLE:
      4-core pod, 200MB per process, tasks are CPU-bound:
        concurrency = 4
        Memory: 4 × 200MB = 800MB

  EVENTLET / GEVENT (green threads — cooperative multitasking):
    Each worker is 1 OS process with N green threads.
    concurrency = N means N concurrent tasks (coroutines).
    Best for: I/O-bound tasks (API calls, email sending, DB queries).

    SIZING:
      concurrency = target_throughput × avg_task_duration

    EXAMPLE:
      Target: 100 tasks/s, each takes 500ms (mostly I/O wait):
        concurrency = 100 × 0.5 = 50 green threads
        Memory: 1 process + 50 × task_data (~5KB each) = 200MB + 250KB

  RATE LIMITING INTERACTION:
    rate_limit="100/m" on a task class means: globally across all workers,
    process at most 100 instances of this task per minute.

    If you have 10 workers with concurrency=5 each:
      Each worker gets ~10 of this task per minute.
      Each worker can process 10 tasks in (10 × 0.5s) = 5s.
      Workers are idle for 55 seconds per minute on this task class.
      Other task classes fill the remaining capacity.
```

---

#### 13.9.7 Health Probe Logic — What to Check and Why

```
THE THREE PROBES — Different questions, different answers:

  LIVENESS: "Is this process stuck?"
    Check:   Process responsive? Event loop running? No deadlock?
    Effect:  FAIL → Kubernetes RESTARTS the pod (kills it, starts new one).
    Danger:  False failure = unnecessary restart = dropped connections.

    WHAT TO CHECK:
      ✓ Return 200 immediately (proves event loop is responsive).
      ✓ Optionally: check that worker threads are alive.
      ✗ DO NOT: query the database.
      ✗ DO NOT: call external services.
      ✗ DO NOT: check disk space, queue depth, or error rate.
      ✗ DO NOT: include any I/O that can time out.

    WHY:
      If the database is down, your process is NOT stuck. It's
      healthy but cannot serve requests. That's READINESS, not LIVENESS.
      Restarting the pod won't fix the database.
      Restarting the pod WILL: drop in-flight requests, lose cache,
      cold-start penalty, potentially cascade (all pods restart).

    IMPLEMENTATION:
      @app.get("/healthz")
      async def liveness():
          return {"status": "alive"}
          # That's it. Nothing else. No I/O.

    TUNING:
      periodSeconds: 10       # check every 10 seconds
      timeoutSeconds: 5       # allow 5s response time (event loop under load)
      failureThreshold: 3     # 3 consecutive failures = restart
      Total time to restart: 10 × 3 = 30 seconds of unresponsiveness.

      IF this is too aggressive (restarts under heavy load):
        Increase timeoutSeconds to 10.
        Increase failureThreshold to 5.
        Total: 10 × 5 = 50 seconds before restart.


  READINESS: "Can this pod serve traffic?"
    Check:   Dependencies available? Pool healthy? Initialized?
    Effect:  FAIL → Kubernetes REMOVES pod from Service endpoints.
             No traffic routed to this pod. Pod stays running.
    Recover: When check passes again → pod re-added to endpoints.

    WHAT TO CHECK:
      ✓ Database connection pool: can acquire a connection in <100ms?
      ✓ Redis connection: PING responds?
      ✓ Critical downstream service: health endpoint responds?
      ✓ Cache warmed? (for services that need warm cache)
      ✓ Internal state: is the service past initialization?

    WHY EACH CHECK:
      DB pool check: if the pool is exhausted, this pod will time out
        on every request. Stop sending traffic until connections free up.
      Redis check: if cache is down and every request hits DB,
        this pod will overload the database. Stop traffic until cache returns.
      Downstream check: if a critical dependency is down, this pod
        will just proxy errors. Better to fail readiness and let the
        client hit a pod whose dependency IS up (different AZ, etc.).

    IMPLEMENTATION:
      @app.get("/ready")
      async def readiness():
          checks = {}

          # DB pool check — can we get a connection?
          try:
              async with async_session() as session:
                  await asyncio.wait_for(
                      session.execute(text("SELECT 1")),
                      timeout=0.1  # 100ms max
                  )
              checks["database"] = "ok"
          except Exception:
              checks["database"] = "failed"
              return JSONResponse(
                  status_code=503,
                  content={"status": "not_ready", "checks": checks}
              )

          # Redis check
          try:
              await asyncio.wait_for(redis.ping(), timeout=0.05)
              checks["redis"] = "ok"
          except Exception:
              checks["redis"] = "failed"
              return JSONResponse(status_code=503, content={...})

          return {"status": "ready", "checks": checks}

    TUNING:
      periodSeconds: 5         # check every 5s
      timeoutSeconds: 3        # allow 3s (includes DB + Redis ping)
      failureThreshold: 2      # 2 failures = remove from endpoints
      successThreshold: 2      # 2 successes = re-add to endpoints
      Time to remove: 5 × 2 = 10 seconds.
      Time to recover: 5 × 2 = 10 seconds after deps recover.


  STARTUP: "Has this pod finished initializing?"
    Check:   Same as readiness, but only during startup.
    Effect:  Until startup probe passes, liveness and readiness
             probes are NOT checked. Pod stays in "starting" state.
    Purpose: Prevent liveness probe from killing a slow-starting pod.

    WHEN TO USE:
      - Cache warming takes 30-60 seconds (loading product catalog).
      - ML model loading takes 15-30 seconds.
      - Database migration checks at startup.
      - Any initialization > liveness failureThreshold × periodSeconds.

    EXAMPLE:
      Cache warming takes 45 seconds.
      Liveness probe: period=10, threshold=3 → kills after 30 seconds.
      WITHOUT startup probe: pod is killed during cache warming!
      WITH startup probe: Kubernetes waits until startup succeeds.

    CONFIGURATION:
      startupProbe:
        httpGet:
          path: /ready  # same endpoint as readiness
          port: 8000
        periodSeconds: 5
        failureThreshold: 30    # 5 × 30 = 150s max startup time
        timeoutSeconds: 3

    AFTER startup probe passes:
      Liveness and readiness probes begin normal operation.
      Startup probe is never checked again for this pod.
```

---

#### 13.9.8 Rate Limiting — The App Defines, Infra Enforces Pattern

```
THE RATE LIMIT SIZING FORMULA:

  STEP 1: Determine actual service capacity (load test).
    Load test result: service sustains 5000 RPS at p99 < 200ms.

  STEP 2: Set global rate limit below capacity.
    Global limit = capacity × 0.8 = 4000 RPS
    (20% headroom for internal traffic, retries, bursts)

  STEP 3: Allocate per-tenant limits from the global budget.

    STATISTICAL MULTIPLEXING:
      Not all tenants peak simultaneously.
      Sum of individual limits can exceed total capacity.
      But each individual limit must be ≤ total capacity.

    SIZING PER TIER:
      Total capacity: 4000 RPS
      Enterprise tenants (5 tenants, each pays for guaranteed capacity):
        Per-tenant: 500 RPS → 5 × 500 = 2500 RPS committed
      Business tenants (50 tenants, shared pool):
        Per-tenant: 50 RPS → 50 × 50 = 2500 RPS theoretical max
      Free tenants (unlimited count, heavily limited):
        Per-tenant: 5 RPS

      Sum: 2500 + 2500 + (N × 5) for free tenants.
      If 200 free tenants: 2500 + 2500 + 1000 = 6000 RPS theoretical.
      But at 4000 capacity, statistical multiplexing assumes
      no more than ~60% of non-enterprise tenants peak simultaneously.

    IF MULTIPLEXING ASSUMPTION FAILS:
      (e.g., Black Friday, product launch, viral moment)
      Per-tenant limits still hold. Total arrivals > capacity.
      Free tier exhausts first (lowest limits).
      Business tier hits limits next.
      Enterprise tier is last to be affected.
      This is PRIORITY BY RATE LIMIT SIZING — no code needed.

  STEP 4: Convert to NGINX / Istio configuration.

    NGINX (app team provides values, infra team deploys config):
      # Per API key (extracted from header)
      limit_req_zone $http_x_api_key zone=per_key:10m rate=50r/s;

      # Global
      limit_req_zone $server_name zone=global:1m rate=4000r/s;

      location /api/ {
          limit_req zone=per_key burst=10 nodelay;
          limit_req zone=global burst=200 nodelay;
          proxy_pass http://backend;
      }

    THE burst PARAMETER:
      burst=10 means: allow 10 requests above the rate limit
      to be queued instead of rejected.
      nodelay: process burst immediately, don't delay.

      WITHOUT burst: exactly 50 req/s per key. Any microsecond
      burst above this is rejected. Too strict.
      WITH burst=10: allows bursts of 60 req/s for short periods.
      Tokens refill at 50/s.

  STEP 5: The app handles what the rate limiter can't.

    Rate limits at the gateway are COARSE:
      - Per API key, per IP, or global.
      - Cannot distinguish request types.
      - Cannot cost-weight requests.

    The APP adds FINE-GRAINED controls:
      - Expensive query counts as 10 units against the limit.
      - Read requests count as 1, writes as 5.
      - Admin endpoints exempt from rate limits.
      - Burst allowance varies by time of day.

    IMPLEMENTATION:
      class CostAwareRateLimiter:
          """Counts request cost, not just request count."""
          def __init__(self, redis, max_cost_per_minute: int):
              self.redis = redis
              self.max_cost = max_cost_per_minute

          async def check(self, tenant_id: str, cost: int) -> bool:
              key = f"rate:{tenant_id}:{minute_bucket()}"
              current = await self.redis.incrby(key, cost)
              if current == cost:  # first request this minute
                  await self.redis.expire(key, 120)
              return current <= self.max_cost

      # Usage in endpoint:
      COST_MAP = {
          "/products": 1,       # simple lookup
          "/search": 5,         # expensive query
          "/reports": 50,       # very expensive aggregation
      }

      cost = COST_MAP.get(request.url.path, 1)
      if not await rate_limiter.check(tenant_id, cost):
          raise HTTPException(429, headers={"Retry-After": "10"})
```

---

#### 13.9.9 The Envelope vs. Content Mental Model — Summary

```
THE COMPLETE MENTAL MODEL:

  INFRA controls the ENVELOPE:
  ─────────────────────────────
  │ How many pods run            │ → HPA min/max, cluster autoscaler
  │ How much CPU/memory each gets│ → resource requests/limits
  │ How many DB connections exist│ → max_connections, PgBouncer sizing
  │ How many partitions a topic  │ → Kafka broker config
  │ Network policies and TLS     │ → Istio, NetworkPolicy, cert-manager
  │                              │
  │ FORMULA: infra sizes the BOX.│
  │ The box is: N pods × M CPU  │
  │   × K connections × P       │
  │   partitions.               │
  │ The box does not know what   │
  │ is inside it.               │
  └──────────────────────────────┘

  APP controls the CONTENT:
  ─────────────────────────────
  │ Which requests to accept     │ → admission control middleware
  │ Which requests are sheddable │ → priority classification
  │ How to degrade gracefully    │ → degradation ladder (§13.9.1)
  │ How to signal backpressure   │ → 429 + Retry-After (§13.9.2)
  │ How to shut down safely      │ → SIGTERM handling (§13.9.3)
  │ How to consume safely        │ → prefetch, ack, batch (§13.9.6)
  │ Connection pool sizing       │ → Little's Law (§13.9.4)
  │                              │
  │ FORMULA: app fills the box   │
  │ with meaning. Same box of    │
  │ 10 pods can serve 100% of   │
  │ traffic at Level 2           │
  │ degradation, or crash at     │
  │ Level 0 under 3× load.      │
  │ The difference is APP logic. │
  └──────────────────────────────┘

  THE LINE:
  ─────────────────────────────────────────────────────
  │ Decision requires DOMAIN KNOWLEDGE?    → APP owns  │
  │ Decision requires INFRA KNOWLEDGE?     → INFRA owns│
  │ Decision requires BOTH?                → HYBRID     │
  │   App sets the POLICY (values, rules, thresholds)  │
  │   Infra ENFORCES it (config, tooling, automation)  │
  └─────────────────────────────────────────────────────┘

  LITMUS TEST for any new mechanism:
    "Could an infra engineer configure this correctly
     WITHOUT talking to the app team?"
    YES → infra owns it (TLS, node sizing, network policy).
    NO  → app owns it or it's hybrid.

    "Could the app team implement this WITHOUT any
     infrastructure changes?"
    YES → app owns it (admission control, degradation logic).
    NO  → infra owns it or it's hybrid.

    Both NO → hybrid. Requires a COORDINATION CONTRACT
    (documented, versioned, reviewed together).
```

```
THE COORDINATION ANTI-PATTERNS — What breaks without alignment:

  ANTI-PATTERN 1: "The app team doesn't know about PgBouncer"
    App sets pool_size=50. PgBouncer has default_pool_size=20.
    App connections pile up waiting for PgBouncer slots.
    App sees: "database is slow" (it's not — PgBouncer is the bottleneck).
    Fix: app team must know PgBouncer exists and its pool_size.

  ANTI-PATTERN 2: "Infra set HPA to scale on CPU without asking app"
    App has a background job that uses 80% CPU for batch processing.
    HPA sees 80% CPU → scales to 20 pods.
    Each new pod starts the batch job. CPU goes to 80% on every pod.
    HPA scales to max_replicas. Cluster overloaded.
    Fix: custom metric for scaling (queue depth, not CPU).
    App team MUST define the scaling metric.

  ANTI-PATTERN 3: "Infra configured Istio retries for all services"
    Retry policy: retryOn: 5xx, attempts: 3, perTryTimeout: 10s.
    App already has internal retries with backoff.
    Total retries: 3 (Istio) × 3 (app) = 9 per request.
    Under outage: 9× amplification on an already-failing service.
    Fix: agree on ONE retry layer. Either mesh-level OR app-level.
    Never both for the same error class.

  ANTI-PATTERN 4: "Nobody owns the queue length"
    Redis is used as a Celery broker.
    Infra: "Redis is running, our job is done."
    App: "We enqueue tasks, Celery picks them up."
    Nobody checks: what happens when the queue grows to 10M items?
    Redis OOMs. All queued tasks lost.
    Fix: APP owns queue length enforcement (LLEN check).
    INFRA owns maxmemory-policy=noeviction for queue Redis instances.
```

---

*The ownership boundary is a **contract**, not a wall. Both teams must understand the full picture. The infra team must understand why `pool_size=50` is wrong even though 50 < max_connections. The app team must understand why scaling to 20 pods will exhaust the connection budget. The coordination checklist (§13.8) is the artifact of this contract — fill it out together before every production deployment.*

---

## 14. Interview Preparation — Adaptive Load Control & Backpressure

Questions designed to test real-world judgment at the mid-to-staff engineer level. Every question is grounded in a concrete production scenario — FastAPI services, connection pools, Kubernetes pods, real numbers. For each question, think through the answer before reading the guidance. The best answers demonstrate that you can reason about overload quantitatively, not just name the patterns.

---

### Real-World Scenario Questions

**Q1: You have a FastAPI service with 1 Uvicorn worker (single process, async). It handles 1000 RPS with 5ms average latency under normal load. A marketing campaign doubles traffic to 2000 RPS. Walk me through exactly what happens inside the process and what the clients experience.**

What the interviewer wants: A single Uvicorn worker runs one Python event loop on one thread. At 1000 RPS with 5ms per request, the event loop can handle it because 1000 * 0.005 = 5 seconds of CPU work per second — but async means CPU is yielded during I/O waits, so effective utilization depends on how much is I/O-bound vs. CPU-bound. At 2000 RPS: if the handler is purely async I/O (database queries, HTTP calls), the event loop accepts all connections but doubles the number of concurrent coroutines. Memory grows. Each coroutine holds state (request object, response buffer). If downstream dependencies (DB, cache) can handle the extra load, latency stays reasonable. If they can't, await calls take longer, more coroutines pile up in the event loop, memory grows, and eventually the process either runs out of memory or the OS kills it (OOM). If ANY part of the handler is synchronous/CPU-bound (JSON serialization of large payloads, image processing, CPU-heavy validation), the event loop blocks. While one request occupies the CPU, all other coroutines are frozen. Latency spikes. The strong answer quantifies: with 1 worker, 1 CPU core, and handlers that are 2ms CPU + 3ms I/O wait, the CPU capacity is 1000ms / 2ms = 500 RPS of CPU work. At 2000 RPS, the CPU is 4x overloaded. Requests queue in the event loop. Latency goes from 5ms to hundreds of milliseconds within seconds.

---

**Q2: Same FastAPI service. You scale to 4 Uvicorn workers behind a single pod. Your database connection pool is set to `pool_size=5, max_overflow=10` per worker (using SQLAlchemy async). Under load, engineers report intermittent `TimeoutError: QueuePool limit reached`. Explain what is happening and how to fix it.**

What the interviewer wants: 4 workers × (5 + 10) = 60 maximum database connections from this single pod. If the database can handle 60 connections and the queries are fast, this works. The error means all 15 connections per worker are occupied and new requests are waiting for a connection. The pool's `pool_timeout` (default 30s in SQLAlchemy) expires before a connection becomes available. Root causes: (1) Queries are too slow — a slow query holds a connection for 500ms instead of 5ms, so 15 connections can only handle 30 RPS instead of 3000 RPS. (2) Connection leaks — a code path that acquires a connection but does not release it (missing `async with` or failed to close on exception). (3) N+1 query patterns — a single request acquires multiple connections or holds one connection for many sequential queries. (4) The pool is simply too small for the traffic. Fixes: (1) Set `pool_pre_ping=True` to detect stale connections. (2) Set `pool_recycle=3600` to prevent connections from going stale. (3) Add `pool_timeout=5` (not 30) so requests fail fast instead of queuing for 30 seconds behind exhausted connections. (4) Most importantly — diagnose WHY connections are held so long. A connection pool is not a queue; if you need to queue, add explicit admission control. The staff answer also notes: total connections across all pods must not exceed the database's `max_connections`. If you have 10 pods × 60 connections = 600, and PostgreSQL is set to `max_connections=100`, you've already exceeded the limit — connection creation itself will fail, and the pool_size settings are irrelevant.

---

**Q3: Your FastAPI service calls a third-party payment API with a 30-second timeout. Under load, the payment API slows to 25-second responses. Your service has 4 workers with 100 max concurrent connections each. What happens and how do you prevent it?**

What the interviewer wants: Little's Law: at 100 RPS with 25s response time, you need L = 100 × 25 = 2500 concurrent connections to the payment API. You only have 400 (4 × 100). After 4 seconds, all 400 connection slots are occupied. New requests queue waiting for a free connection. But the queue also grows at 100 RPS. Within 10 seconds you have thousands of waiting requests, each consuming memory (coroutine state, request objects). The 30-second timeout means each request waits up to 30 seconds before giving up, holding resources the entire time. Meanwhile, incoming user requests to YOUR service pile up because the event loop is saturated with coroutines waiting on the payment API. Your service becomes unresponsive to ALL endpoints, not just the payment endpoint. Prevention: (1) Set the payment API timeout to 5s, not 30s — if it hasn't responded in 5s, it won't respond usefully. (2) Use a semaphore to limit concurrent payment calls (e.g., 50). Reject additional payment requests immediately with 503. (3) Circuit breaker: if the payment API's p99 exceeds 5s for 30 seconds, stop calling it entirely. (4) Bulkhead: isolate payment-related endpoints so they cannot exhaust resources shared with other endpoints. The key insight: a slow dependency is more dangerous than a dead one, because slow holds resources while dead releases them immediately.

---

**Q4: You deploy a FastAPI service on Kubernetes with `resources.limits.memory: 512Mi` and `resources.limits.cpu: 500m`. The service handles JSON APIs with average response sizes of 2KB. Under a load test at 5000 RPS, pods keep getting OOMKilled. Explain why and how to fix it.**

What the interviewer wants: At 5000 RPS with 10ms average latency, Little's Law says L = 5000 × 0.01 = 50 concurrent requests. Each request consumes memory for: the request object (~1-5KB), the parsed JSON body, any ORM objects loaded from the database, the response serialization buffer, and the asyncio coroutine state (~2-8KB). Under normal conditions, 50 × 10KB = 500KB of concurrent request memory — trivial. But under overload: if latency increases to 500ms, L = 5000 × 0.5 = 2500 concurrent requests. Memory: 2500 × 10KB = 25MB just for request state. If requests involve database queries that load ORM objects averaging 50KB each: 2500 × 50KB = 125MB. If the response serialization buffers large payloads or if there are in-memory caches: you can easily hit 512MB. The OOMKill happens because: (1) No admission control — the service accepts all 5000 RPS regardless of capacity. (2) No concurrency limit — asyncio happily spawns 2500 coroutines. (3) No queue bound — requests pile up in the event loop without limit. Fix: (1) Add a concurrency limiter middleware (`asyncio.Semaphore(200)`) that rejects requests with 503 when 200 are already in flight. (2) Set bounded queue depth on the ASGI server (Uvicorn's `--limit-concurrency`). (3) Increase memory limits based on actual load testing data. (4) Add request size limits. The staff answer: the memory limit should be sized to the maximum concurrent requests you're willing to handle × per-request memory footprint, with 30% headroom. Don't set memory limits without knowing your concurrency limit.

---

**Q5: You have a FastAPI service behind an NGINX reverse proxy. NGINX is configured with `proxy_read_timeout 60s` and a `limit_req_zone` rate limiter at 1000 RPS with burst=200. Your FastAPI service can handle 800 RPS sustainably. Traffic is normally 500 RPS. A traffic spike hits 1500 RPS. Describe second-by-second what happens.**

What the interviewer wants: Second 1: 1500 RPS arrives at NGINX. Rate limiter allows 1000 RPS (configured limit) + 200 burst = 1200 through. 300 requests rejected with 503. Second 2: 1200 RPS reaches FastAPI. FastAPI can only process 800 RPS. 400 requests per second begin queuing in the ASGI server. Second 3: Queue grows by 400/s. After 5 seconds, 2000 requests are queued. Each queued request is waiting for processing. NGINX's 60s timeout means NGINX will wait 60 seconds for a response. Second 10: 4000 requests queued. Latency for new requests is now 4000/800 = 5 seconds of queue wait. Clients see 5-second response times. Second 30: 12,000 requests queued. Queue wait time: 15 seconds. Memory consumption is climbing. Clients at the front of the queue have already timed out (typical client timeout: 10-30s), but FastAPI is still processing their requests — wasted work. Second 60: NGINX starts timing out the oldest requests (60s proxy_read_timeout). But the queue is still 20,000+ deep. This is queue collapse. The problems: (1) NGINX rate limiter is set to 1000 RPS but FastAPI can only handle 800 — the rate limit should match actual backend capacity, not a round number. (2) 60s proxy_read_timeout is far too long — set it to 5-10s. (3) No queue bound on FastAPI — add `--limit-concurrency` to Uvicorn. (4) No deadline-based shedding — requests sitting in queue for >2s should be dropped. Fix the rate limiter to 800 RPS, set proxy_read_timeout to 10s, add `--limit-concurrency 100` to Uvicorn, and add middleware that drops requests older than 3 seconds.

---

**Q6: Your team runs a multi-tenant SaaS API on FastAPI. Three customers: Enterprise (Tenant A, 60% of revenue), Startup (Tenant B, 30%), Free tier (Tenant C, 10%). Total capacity: 1000 RPS. Tenant C discovers your API and starts scraping at 3000 RPS. Your monitoring shows p99 latency for Tenant A jumped from 50ms to 2 seconds. The CEO is calling. What happened, and what do you implement this week vs. this quarter?**

What the interviewer wants: What happened: Tenant C's 3000 RPS overwhelmed the shared service. Without per-tenant isolation, all tenants compete for the same worker threads, database connections, and CPU. Tenant C consumes most of the capacity. Tenant A's requests queue behind Tenant C's requests. p99 goes from 50ms to 2s because the queue depth is dominated by Tenant C's volume.

This week (emergency): (1) Rate limit Tenant C to 50 RPS at the API gateway (NGINX/Kong) using their API key. (2) Rate limit free tier globally to 100 RPS. (3) Add per-tenant rate limits: Tenant A = 600 RPS, Tenant B = 300 RPS, Free tier = 100 RPS. This can be done with NGINX `limit_req_zone` keyed on API key or a Redis-based rate limiter.

This quarter (proper fix): (1) Per-tenant concurrency limits in application middleware — not just RPS limits, but concurrent in-flight request limits per tenant. (2) Weighted fair queuing — Tenant A gets 60% of capacity, Tenant B gets 30%, Free tier gets 10%. (3) Separate database connection pools per tenant tier (or at least reserved connections for Tenant A). (4) Request costing — an expensive aggregation query from Tenant C should count as 10 "units" against their rate limit, not 1. (5) Tenant-aware load shedding — under overload, shed Free tier first, then Startup, never Enterprise. (6) Monitoring and alerting per tenant — p99 by tenant, not just aggregate.

The staff answer also notes: the business impact determines the engineering priority. Tenant A is 60% of revenue. A 2-second p99 for Tenant A is a revenue emergency. The short-term fix must be deployed within hours, not days. Per-tenant rate limiting at the gateway is the fastest path.

---

**Q7: You run a FastAPI service with a PostgreSQL database (RDS, db.r5.xlarge, max_connections=200). The service has 5 pods, each with `pool_size=20, max_overflow=20` (total: 5 × 40 = 200 connections, exactly matching max_connections). During a deploy (rolling update), a 6th pod starts before the 5th pod terminates. What happens?**

What the interviewer wants: During rolling deployment, there's a brief period with 6 pods running simultaneously. The 6th pod tries to create 20 connections (pool_size). But the database already has 200 connections from the existing 5 pods. Every `CREATE CONNECTION` from the 6th pod fails with `FATAL: too many connections`. The 6th pod's health check (which likely hits the database) fails. Kubernetes marks it as unhealthy and restarts it. The restart creates the same problem. You get a restart loop during every deploy.

But it's worse than that: the 5th pod (being terminated) receives SIGTERM and starts draining. It stops accepting new requests but its existing connections stay open until in-flight requests complete (graceful shutdown). If any request is slow, those connections are held for seconds. Meanwhile, the 6th pod is failing. If the 5th pod's graceful shutdown timeout (terminationGracePeriodSeconds) is long, the overlap window grows.

Fix: (1) Set `max_overflow=0` and `pool_size=15` per pod. 5 × 15 = 75, leaving headroom for rolling deploys (6 × 15 = 90, still under 200). (2) Or set PostgreSQL `max_connections=300` with headroom for deploys + monitoring connections + migration connections. (3) Use PgBouncer as a connection pooler between pods and PostgreSQL. PgBouncer multiplexes hundreds of application connections over a smaller number of actual database connections. (4) Configure Kubernetes `maxSurge=0, maxUnavailable=1` so the old pod terminates before the new pod starts (but this means brief downtime during deploys). The staff answer: never size your connection pool to exactly match the database's max_connections. Always leave 20-30% headroom for rolling deploys, monitoring tools (pg_stat_activity, DataDog agent), manual DBA connections, and migration scripts.

---

**Q8: Your FastAPI service processes webhook events from Stripe. Events arrive at ~50/s normally but spike to 5000/s during batch operations (e.g., subscription renewals at month end). Each webhook handler calls 3 internal services and takes 200ms. You're using Celery with Redis as the broker and 10 workers. At 5000 events/s, the Redis broker runs out of memory after 20 minutes. Explain the math and the fix.**

What the interviewer wants: Processing capacity: 10 Celery workers × (1000ms / 200ms) = 50 tasks/second. Arrival rate during spike: 5000/s. Backlog growth: 5000 - 50 = 4950 tasks/second accumulating in Redis. Each task message is ~2KB (JSON payload with event data). After 20 minutes (1200 seconds): 4950 × 1200 = 5,940,000 tasks queued. At 2KB each: 5.94M × 2KB ≈ 11.2GB. If Redis is provisioned with 8GB, it runs out of memory and starts evicting keys or crashes (depending on maxmemory-policy). When Redis crashes, all queued tasks are lost (Redis is not a durable queue by default, even with RDB/AOF, recovery is messy under OOM).

Fix: (1) Backpressure at the webhook endpoint — return 429 to Stripe when queue depth exceeds threshold. Stripe will retry with exponential backoff (their retry policy is well-documented). This is the correct answer because Stripe expects and handles 429s. (2) Scale Celery workers horizontally during spikes (KEDA autoscaler based on Redis queue length). (3) Set a max queue length — reject new tasks when the queue exceeds N items. (4) Use a durable queue (SQS, RabbitMQ with persistence) instead of Redis if at-least-once delivery matters. (5) Batch processing — instead of processing each webhook individually, batch them: dequeue 100 events, process them in a single database transaction. This can increase throughput 10-50x.

The staff answer: the fundamental problem is a 100x mismatch between arrival rate and processing rate. No amount of Redis tuning fixes a throughput mismatch. You either increase processing capacity (more workers, faster handlers, batching) or decrease arrival rate (backpressure, rate limiting at ingestion).

---

**Q9: You have a FastAPI service with an in-memory cache (Python dict) holding 100,000 product records (~500MB). The cache TTL is 5 minutes. Every 5 minutes, all 100,000 keys expire simultaneously. When the cache is empty, each cache miss triggers a database query. Describe the failure and the fix.**

What the interviewer wants: This is a thundering herd / cache stampede. At the 5-minute mark, all 100,000 keys expire simultaneously. The next requests for each key are cache misses. If the service handles 1000 RPS and each request needs 1-5 product records, that's 1000-5000 database queries per second instead of the normal ~10/s (misses on newly added products). The database connection pool saturates immediately. Query latency spikes. More requests pile up. The service becomes unresponsive for 30-60 seconds while the cache refills. This happens every 5 minutes like clockwork.

Fixes: (1) Staggered TTL — set TTL = 5min + random(0, 60s). Keys expire gradually over 60 seconds instead of all at once. (2) Background refresh — a background task refreshes the cache before TTL expires (`stale-while-revalidate`). The cache is never empty; users always get data (possibly stale by seconds). (3) Singleflight / request coalescing — if 100 requests arrive for the same cache key simultaneously, only 1 triggers the database query. The other 99 wait for that one query to complete and share the result. In Python: use `asyncio.Lock` per key, or a library like `cachetools` with locking. (4) Warm-up on startup — pre-populate the cache from database before accepting traffic. (5) Never use synchronous cache expiry for large caches. Either use staggered TTL or background refresh.

The staff answer: cache stampedes are a form of correlated failure — all misses arrive at the same instant. Any caching strategy that allows correlated expiry is a latent time bomb. The fix is decorrelation (staggered TTL) plus deduplication (singleflight).

---

**Q10: Your team's FastAPI service uses `httpx.AsyncClient` with default settings to call 5 internal microservices. Under load testing at 2000 RPS, you discover that request latency to downstream services jumps from 10ms to 500ms even though the downstream services are healthy and fast. `netstat` shows 50,000 connections in TIME_WAIT. Explain what's happening.**

What the interviewer wants: The default `httpx.AsyncClient` (without connection pooling or with a new client per request) creates a new TCP connection for each request. At 2000 RPS to 5 services = 10,000 connections/second being created and destroyed. Each closed connection enters TIME_WAIT for 60 seconds (Linux default). After 5 seconds: 50,000 sockets in TIME_WAIT. Each socket consumes a file descriptor and a port from the ephemeral port range (typically 32768-60999 = ~28,000 ports). At 50,000 TIME_WAIT sockets, you've exhausted the ephemeral port range. New connection attempts fail or get delayed waiting for ports to recycle. The 500ms latency is the kernel waiting for a port to become available.

Fix: (1) Use a single `httpx.AsyncClient` instance with connection pooling (the default pool size is 100 connections). Create it once at startup and reuse it across requests. This reuses TCP connections via HTTP keep-alive. (2) Set `limits=httpx.Limits(max_connections=200, max_keepalive_connections=100)` to size the pool appropriately. (3) If you must create many connections, tune the kernel: `net.ipv4.tcp_tw_reuse=1` allows reuse of TIME_WAIT sockets. (4) Use Unix domain sockets for same-host communication (eliminates TCP overhead entirely). The staff answer: this is a resource exhaustion problem disguised as a latency problem. The fix is not "tune timeouts" — it's "stop creating 10,000 connections per second." Connection pooling is not an optimization; for async HTTP clients at scale, it's a correctness requirement.

---

### Conceptual and Design Questions

**Q11: A junior engineer proposes adding a `asyncio.Queue(maxsize=10000)` as a work buffer in front of the database layer in your FastAPI service. They argue: "If the database is slow, we'll buffer requests in memory and process them when the database recovers." What's wrong with this approach?**

What the interviewer wants: Several problems: (1) A queue does not increase processing capacity. If the database can process 100 queries/s and you're receiving 500/s, the queue grows by 400 items/second. After 25 seconds the 10,000-item queue is full. You've delayed the problem by 25 seconds, not solved it. (2) Requests in the queue are still consuming HTTP connections. The client (browser, mobile app) is waiting for a response. After 5 seconds, the client times out and retries. Now you have the original request sitting uselessly in the queue AND a retry request arriving. (3) If the queue is in-memory and the process crashes or restarts, all 10,000 queued requests are lost. (4) The queue hides the overload signal. Without the queue, the service returns 503 immediately, the client retries on another instance, or the load balancer notices and stops sending traffic. With the queue, the service looks "healthy" (accepting requests, no errors) while actually falling further behind. (5) Memory: 10,000 requests × 10KB each = 100MB consumed just for buffering. The correct approach: reject requests you cannot serve immediately. Return 503 with Retry-After. Let the client decide whether to retry. A bounded queue of 50-100 items for absorbing microsecond bursts is fine; a queue of 10,000 is a memory-consuming lie about your capacity.

---

**Q12: Your FastAPI service processes requests that take between 5ms (cache hit) and 5 seconds (cold database query with aggregation). Average latency is 50ms. You've set `--limit-concurrency 200` on Uvicorn. Is this a good setting? What happens when 200 concurrent requests are all the slow 5-second type?**

What the interviewer wants: When all 200 slots are occupied by 5-second requests, the service is at 100% capacity but processing only 200/5 = 40 RPS — far below its normal throughput. New requests (including fast 5ms cache-hit requests) are rejected with 503 because the concurrency limit is reached. The problem: a uniform concurrency limit treats all requests equally. A 5ms cache-hit request and a 5-second aggregation query both consume one concurrency slot, but the aggregation holds it 1000x longer.

Better approaches: (1) Separate endpoints into fast and slow paths with independent concurrency limits. Fast path: `max_concurrency=180` for cache-hit endpoints. Slow path: `max_concurrency=20` for aggregation endpoints. (2) Cost-based admission: weight the concurrency cost by estimated processing time. A 5-second query costs 100 "units" against the limit while a 5ms query costs 1. (3) Deadline-based shedding: if a request's estimated processing time exceeds the remaining deadline, reject immediately. (4) Separate worker pools (bulkhead pattern): run slow queries in a separate pool of workers with its own connection pool and concurrency limit, so slow requests cannot starve fast ones.

The staff answer: `--limit-concurrency` is a blunt instrument. It prevents memory exhaustion but does not protect goodput for heterogeneous workloads. Production services need per-endpoint or per-cost-class concurrency control, not a single global number.

---

**Q13: You're designing a rate limiter for your public API. The PM wants "1000 requests per minute per API key." You implement a fixed-window counter in Redis. In production, a customer complains they're getting 429 errors even though they sent only 800 requests in the last minute. How is this possible?**

What the interviewer wants: The fixed-window boundary problem. Suppose the window boundary is at :00 seconds. The customer sent 600 requests between 12:00:30 and 12:00:59 (within window 12:00), then 800 requests between 12:01:00 and 12:01:30 (within window 12:01). From the customer's perspective, they sent 800 requests in the last 60 seconds (from 12:00:30 to 12:01:30). From the rate limiter's perspective, window 12:00 had 600 (passed), and window 12:01 starts at 800. But the REAL rate in the sliding 60-second window from 12:00:30 to 12:01:30 is 600 + 800 = 1400 — they're actually over the limit. The opposite can also happen: 999 requests at 12:00:59 and 999 at 12:01:00 = 1998 requests in 2 seconds, both windows say "under 1000."

Fix: use a sliding window counter (weighted overlap between current and previous window) or a sliding window log. The sliding window counter approximation is good enough for most APIs and uses O(1) storage. The staff answer: fixed-window rate limiting has a known 2x burst vulnerability at window boundaries. Any production rate limiter should use sliding windows unless the simplicity tradeoff is explicitly accepted.

---

**Q14: Your team operates a FastAPI service that fans out to 20 microservices to assemble a product page. Each downstream call has a 200ms timeout. The total SLA for the product page is 500ms. A staff engineer joins and says "your timeout math doesn't add up." What are they seeing?**

What the interviewer wants: If the 20 calls are sequential: worst case = 20 × 200ms = 4000ms. Even if most are fast, one slow dependency makes the total exceed 500ms. If the 20 calls are parallel (using `asyncio.gather`): worst case = max(200ms) = 200ms. But the p99 hit rate for 20 parallel calls matters — probability that at least one hits its 200ms timeout is 1 - (1 - p_timeout)^20. If each service has a 1% timeout rate, the chance of at least one timeout is 1 - 0.99^20 = 18.2%. So 18% of product page requests will take ~200ms just from one slow dependency.

The real problem: 500ms SLA minus 200ms downstream timeout leaves only 300ms for: receiving the request, parsing it, making 20 HTTP connections (TCP + TLS handshake if not pooled = 5-20ms each), processing 20 responses, serializing the result, and sending it back. If connections are not pooled, 20 × 10ms handshake = 200ms, leaving 100ms for everything else. With connection pooling and parallel calls: 500ms - 200ms timeout - 20ms overhead = 280ms margin. But you need the margin for the EXPECTED case, not the timeout case. Set downstream timeouts to 300ms (1.5x their p99), use structured concurrency to cancel all outstanding calls if the 500ms deadline is reached, and serve partial results (skip non-critical services like recommendations/reviews) if the critical services respond in time.

---

**Q15: You notice that your FastAPI service's memory usage grows linearly during a load test but never decreases, even after load drops to zero. The service eventually OOMKills after 6 hours of sustained load. There are no obvious memory leaks in your code. What are the most likely causes?**

What the interviewer wants: Common non-obvious memory growth patterns in Python/FastAPI: (1) SQLAlchemy session/identity map — if sessions are not properly closed, the identity map accumulates every loaded ORM object. With a scoped session tied to request lifecycle, a missing `await session.close()` in error paths leaks objects. (2) `httpx.AsyncClient` response bodies — if responses are not consumed (`await response.aread()`), the connection stays open and the buffer is held. (3) asyncio task references — if tasks are created with `asyncio.create_task()` but not awaited, their result objects accumulate. (4) Python's memory allocator (pymalloc) — Python returns memory to its own free lists but does not always return it to the OS. `gc.collect()` frees Python objects but pymalloc's arena allocator may retain the memory. Under sustained load, the high-water mark of allocated arenas only grows. (5) Logging handlers with in-memory buffers. (6) Global caches without eviction (a `dict` used as a cache without TTL or LRU bounds). (7) Circular references that the garbage collector processes but cannot free until a full collection cycle.

Diagnosis: use `tracemalloc` to snapshot memory allocations before and after load. Compare snapshots to find which allocation sites are growing. For pymalloc fragmentation, check `sys._debugmallocstats()`. For ORM leaks, check `len(session.identity_map)` over time.

The staff answer: in a long-running Python process, "no memory leak" does not mean "constant memory." Memory fragmentation and pymalloc's arena allocation policy mean that memory usage can grow monotonically even without leaks. Set memory limits with headroom, use worker recycling (`--limit-max-requests` in Uvicorn/Gunicorn), and monitor RSS over time.

---

### Architecture and Tradeoff Questions

**Q16: You're designing the overload protection strategy for a new FastAPI service that will serve 10,000 RPS. Walk through your design from the edge to the database, specifying exact mechanisms at each layer.**

What the interviewer wants: A complete defense-in-depth design with specific numbers:

Edge (NGINX / cloud LB): Rate limit at 12,000 RPS global (20% headroom). Per-IP limit at 100 RPS to prevent single-source floods. Connection limit at 5000 concurrent. Request body size limit at 1MB.

API Gateway / middleware: Per-API-key rate limit (tiered: free=10 RPS, business=100 RPS, enterprise=1000 RPS). Request validation and early rejection. Deadline header injection (X-Request-Deadline = now + 5s).

Application (FastAPI middleware): Concurrency limiter: `asyncio.Semaphore(500)` — reject with 503 when 500 requests are in-flight. Deadline-aware: middleware checks remaining deadline budget, rejects if <100ms remaining. Priority headers: extract tenant tier, pass to downstream calls. Request metrics: emit latency histogram per endpoint.

Database layer: Connection pool per pod: `pool_size=20, max_overflow=10`. Query timeout: `statement_timeout=3s` in PostgreSQL. Slow query detection: log queries >500ms. Read replica routing for read-heavy endpoints.

Queue layer (if applicable): Bounded queue: `maxsize=1000`. Dead-letter queue for failed items. Consumer concurrency limit.

The staff answer: the specific numbers matter less than the reasoning. Every number should be justified: "500 concurrent requests because each consumes ~1MB of memory, and our pod has 1GB available for request processing." "20 pool connections because our queries average 5ms, so 20 connections can handle 4000 QPS of database work." Numbers without reasoning are just configuration; numbers with reasoning are engineering.

---

**Q17: Your FastAPI service has been running fine at 2000 RPS for months. One morning, latency gradually increases from 10ms to 500ms over 30 minutes with no traffic change, no deploys, and no alerts from dependencies. By the time someone notices, the service is barely functional. What happened, and what observability would have caught it earlier?**

What the interviewer wants: This is a slow-onset degradation — the hardest kind to detect. Common causes: (1) Database table bloat — a table crossed a threshold where query planner switches from index scan to sequential scan. (2) Connection pool leak — a slow leak of 1 connection per hour. After 20 hours, the pool is half-exhausted. At 30 hours, connection wait times dominate latency. (3) Memory pressure — RSS growing due to fragmentation until the OS starts swapping. Swap I/O is 100-1000x slower than RAM. (4) Log volume — a verbose log statement generates 10GB of logs, filling the disk. Once disk is full, every fsync blocks. (5) Certificate expiry or TLS session cache exhaustion — TLS handshakes start failing, causing connection retries. (6) DNS resolution degradation — if DNS TTL expires and the DNS server is slow, every new connection adds 500ms for DNS lookup.

Observability that catches this: (1) p99 latency alerts (not just error rate — this scenario has zero errors). (2) Connection pool utilization alerts (>70% → warning, >90% → page). (3) Memory RSS trending alert (if RSS increases >10% over 1 hour, alert). (4) Database query latency per query type. (5) Disk I/O latency and disk space. (6) Event loop lag for async services (if the event loop is delayed >10ms, something is blocking it). The staff answer: error rate is a lagging indicator. Latency and resource utilization are leading indicators. Alert on gradient changes (rate of change), not just absolute thresholds.

---

**Q18: An engineer proposes: "Let's just auto-scale our FastAPI pods based on CPU. If CPU > 70%, add a pod. If CPU < 30%, remove one. That handles overload." What are the three most important things they're missing?**

What the interviewer wants: (1) Scaling delay — auto-scaling takes 1-5 minutes (detect metric, decide, provision pod, pull image, start process, health check passes, load balancer adds pod). During that window, the service is already overloaded. You need admission control for the window BEFORE new pods arrive. Auto-scaling is a capacity planning mechanism, not an overload defense. (2) Non-CPU bottlenecks — CPU at 50% doesn't mean the service is healthy. The database connection pool might be saturated. Memory might be at 95%. The downstream dependency might be slow. Scaling more pods that all share the same database just multiplies the number of connections hitting an already-stressed database, making it WORSE. (3) Thundering herd on scale-down — when load drops and pods are removed, their in-flight connections are terminated. If the load balancer doesn't drain gracefully, those requests fail. Clients retry. The retries hit the remaining (fewer) pods. The retry load triggers scale-up again. You get an oscillation loop.

Additional: (4) Cost — auto-scaling up is easy; auto-scaling DOWN is where the budget blows up. If the service scales to 20 pods during a 5-minute spike and takes 30 minutes to scale back down (stabilization window), you're paying for 20 pods for 35 minutes total. (5) Stateful resources don't scale horizontally — database connections, cache warm-up, in-memory session state. The staff answer: auto-scaling solves capacity planning. It does not solve overload defense, resource bottlenecks, or thundering herds. You need both auto-scaling AND admission control / load shedding / rate limiting.

---

### Quick-Fire Judgment Calls

**Q19:** Your FastAPI service connects to Redis for caching. Redis goes down. Do you return errors to all users or serve responses without cache?
→ Serve without cache (bypass Redis). Cache is an optimization, not a data source. Set the Redis call inside a try/except with a 50ms timeout. Log the miss. If Redis-less load overwhelms the database, activate graceful degradation (stale responses from a local in-memory fallback).

**Q20:** Your Uvicorn worker count: should it be `2 * CPU_CORES + 1` (the Gunicorn rule) for a FastAPI async service?
→ No. That formula is for sync workers (Gunicorn with sync workers, Flask). For async (Uvicorn), use 1 worker per CPU core as a starting point. Async workers handle concurrency via the event loop, not via multiple processes. More workers = more memory overhead and more database connections. Benchmark your specific workload.

**Q21:** Your service handles 1000 RPS. You add a rate limiter at 1000 RPS. Under exactly 1000 RPS of legitimate traffic, users start seeing 429 errors. Why?
→ Measurement granularity. If the rate limiter uses 1-second fixed windows, traffic isn't perfectly uniform. Requests arrive in bursts within each second. A 50ms burst of 80 requests exceeds the per-window rate. Fix: add burst headroom (rate=1000, burst=1200) or use a sliding window.

**Q22:** You need to choose between `Retry-After: 5` (fixed) and `Retry-After: <random 1-10>` (jittered) on your 429 responses. Which one?
→ Jittered. Fixed Retry-After causes all rejected clients to retry at exactly T+5s, creating a synchronized spike. Jittered spreads retries over 1-10 seconds. At 1000 rejected clients, fixed creates a 1000-request spike at T+5. Jittered creates ~100 RPS spread over 10 seconds.

**Q23:** Your async FastAPI handler does `await asyncio.sleep(0.001)` at the start to "yield control to the event loop." A colleague says this is cargo cult programming. Are they right?
→ Yes. `asyncio.sleep(0)` (or 0.001) yields to the event loop scheduler but adds 1ms of latency to EVERY request. At 1000 RPS, that's 1 second of cumulative delay per second of wall time. If the handler already has `await` calls (database, HTTP), those naturally yield. An explicit yield is only useful if the handler is CPU-bound for >10ms without any I/O and you need to prevent event loop starvation — and in that case, the handler should be offloaded to a thread pool with `loop.run_in_executor()`.

**Q24:** You're reviewing a PR that adds `asyncio.Semaphore(10)` to limit concurrent database queries. The service handles 1000 RPS and each query takes 5ms. Will this work?
→ No. 10 concurrent queries × (1000ms / 5ms) = 2000 queries/second capacity. At 1000 RPS with 1 query per request, the math works — but only if every request needs exactly 1 query of exactly 5ms. In practice, some requests need 3-5 queries, and some queries take 50ms. The semaphore will become the bottleneck and cause artificial queueing. Size it to the database connection pool size (e.g., 20-50), not an arbitrary number. The semaphore should protect the database from overload, not throttle the application unnecessarily.

**Q25:** Your FastAPI service runs on Kubernetes. The liveness probe hits `/health` every 10 seconds with a 3-second timeout. Under heavy load, the event loop is saturated and `/health` takes 5 seconds. What happens?
→ Kubernetes marks the pod as unhealthy and RESTARTS it. The restart kills all in-flight requests. When the pod restarts, it gets a cold cache, takes 30 seconds to warm up, and during warmup absorbs less load. Kubernetes restarts the pod again. You've turned a latency problem into a crash loop. Fix: (1) Separate the health endpoint from the main event loop (use a separate thread or a dedicated lightweight server on another port). (2) Increase the liveness probe timeout to 10s. (3) Use a readiness probe (not liveness) to stop traffic — readiness failures stop traffic but don't restart the pod. Liveness probes should only restart when the process is truly stuck (deadlocked), not when it's merely overloaded.

**Q26:** Your service writes access logs synchronously to disk for every request. Under 5000 RPS, each log write takes 0.1ms. Is this a problem?
→ Yes, eventually. 5000 × 0.1ms = 500ms of I/O per second — seems fine. But disk I/O has high variance. When the OS flushes dirty pages (every 5-30 seconds), `write()` latency spikes to 5-50ms. A 50ms disk stall blocks the event loop, freezing all 5000 concurrent requests. Fix: use async logging (write to an in-memory buffer, flush periodically in a background task) or write logs via a sidecar (send to stdout, let the container runtime handle buffering and shipping). Never let a synchronous disk write sit in the hot path of an async event loop.

**Q27:** Your team wants to add a circuit breaker to the database connection. You push back. Why?
→ A database is not a typical "dependency" for circuit breaker purposes. If the breaker opens, your service cannot serve ANY request that needs data — which is likely all of them. There's no meaningful fallback for "cannot reach the database." A circuit breaker makes sense for optional or replaceable dependencies (a recommendation service, a third-party API) where you have a fallback path. For the database, use connection pool timeouts (fail fast if no connection available in 3s), query timeouts (kill queries over 5s), and read replica failover. The circuit breaker pattern assumes "stop calling this dependency" is a valid state. For your primary datastore, it usually isn't.

**Q28:** Your async FastAPI service calls an external API using `httpx`. Under load, you see `httpx.PoolTimeout: timed out waiting for a connection from the pool`. The external API responds in 20ms. What's happening?
→ The `httpx.AsyncClient` has a default connection pool limit of 100 connections per host. At 200 RPS with 20ms per request: 200 × 0.02 = 4 concurrent connections needed — should be fine. But if YOU aren't reusing the client (creating a new `AsyncClient` per request), each instance has its own pool. If you ARE reusing it, check: (1) Are responses being fully consumed? Unconsumed responses hold connections open. (2) Is there a slow endpoint on the same host consuming all pool connections? (3) Are connections being leaked in error paths? Fix: `async with httpx.AsyncClient(limits=httpx.Limits(max_connections=200))` as a singleton, ensure `async with client.stream()` is used if streaming, add `timeout=httpx.Timeout(5.0, pool=2.0)` to fail fast on pool exhaustion rather than waiting.

---

*Use these questions for self-assessment: if you can trace the exact resource exhaustion path (threads, connections, memory, file descriptors, ports) through a specific failure scenario with real numbers, you understand overload defense at the level needed to design and debug production systems. Pattern names are necessary but insufficient — the difference between mid-level and staff-level is the ability to quantify what happens and predict when it breaks.*

---

## 15. Sandbox Experiments — Run These Yourself

Overload is the failure mode you cannot reason about from a diagram, because the
interesting behaviour is non-linear and arrives suddenly. This section is a ladder
of experiments that make the knee visible: from a single concurrency limit and
Little's Law, up to a product's capacity arithmetic and the exact moment a token
bucket stops protecting you.

**Where to run them.** The companion sandbox is at
`../ai-rag/labs/llm-resilience/simulator.html` — open it directly, no server and
no dependencies. Experiments 1–2 are steps 7 and 10 of its **Learn** tab; 3–7 use
the **Sandbox** tab. `capacity.py` in the same folder does the arithmetic with no
simulation at all, and that is the part to port to your own numbers first.

> **What these numbers are.** Simulated, from an invented fixture — a chat
> product in front of an LLM API with a plausible-but-fictional latency and
> rate-limit profile. They demonstrate **mechanisms and orders of magnitude**,
> reproducibly from a seed. They are not measurements of any provider. The
> *arithmetic* in each "Predict" block is exact, and that is what to carry to
> your own system.

---

### 15.1 Experiment 1 — Little's Law is not advice, it is a constraint

Before any pattern, the number that determines everything else.

```
SETUP
  N requests/sec, each holding a resource for L seconds
  a hard concurrency limit of C
  requests that cannot get a slot within 3s are rejected
```

**Predict.** Little's Law: concurrency required = `N × L`. If `C < N × L` the
system cannot keep up *no matter what else you do* — the queue grows until
something sheds. If `C > N × L` with headroom, queueing is negligible and latency
is just service time.

**Observe.**

```
  arrival   service   need      limit   success   p95 latency   rejected
  6 /s      0.6s      3.6        4       100%       0.72s           0
  12 /s     0.6s      7.2        4        61%       3.66s         141
  12 /s     0.6s      7.2        8       100%       0.73s           0
  12 /s     0.6s      7.2       16       100%       0.72s           0
  24 /s     0.6s     14.4       16       100%       0.73s           0
```

**Why it matters.** Rows 2 and 3 differ only in a concurrency limit of 4 versus 8,
against a requirement of 7.2. One is a 61%-success incident with 3.66s p95; the
other is invisible. No tuning, no backoff curve and no breaker rescues row 2 — it
is short of capacity by arithmetic.

Rows 4 and 5 make the complementary point: raising the limit from 8 to 16 changes
*nothing*, because 8 already exceeded the requirement. **Concurrency limits do not
improve a system that is not concurrency-bound.** Most "we raised the pool size
and it got better" stories are row 2 → row 3; most "we raised it and nothing
happened" are row 3 → row 4.

Size every pool — threads, connections, semaphores, provider concurrency — from
`N × L` with headroom, and re-derive it when `L` changes. An LLM call's `L` is
dominated by output tokens, so a prompt change silently moves your pool
requirement.

---

### 15.2 Experiment 2 — Shedding reallocates; it does not create

```
SETUP
  12 requests/sec, 0.6s each, concurrency limit 4  (need 7.2 — row 2 above)
  half the traffic is "important" (someone is waiting), half is "background"
  the variable: shed background work when the pool is >80% busy
```

**Predict.** Shedding cannot raise total throughput — the limit is still 4. What
changes is *which* requests get the slots, and how fast the rest are refused.

**Observe.**

```
                  important work      background work     p95 latency
  shedding OFF    106/172  ( 62%)     113/188  ( 60%)        3.66s
  shedding ON     171/171  (100%)      27/189  ( 14%)        0.97s
```

**Why it matters.** Total served is 219 in both rows — shedding produced zero
extra capacity, exactly as predicted. But:

- Important work went from **62% to 100%**. Every request someone was waiting for
  succeeded.
- p95 fell from **3.66s to 0.97s**, because the queue is no longer full of work
  that will be discarded on arrival.
- Background work absorbed the entire loss, which is the point.

This is §4.1's counterintuitive truth with numbers on it: the system got *better*
by doing *less*. Without shedding, both classes compete equally, so a request
nobody is waiting for occupies a slot ahead of one a user is staring at — and both
end up slow.

**The caveat that matters in real systems** (Experiment 7 returns to it): shedding
only helps if the shed work contends for the *same* resource.

---

### 15.3 Experiment 3 — Find the meter that actually binds

Switch to the **Sandbox** tab, which runs a realistic product: each user turn fans
out to 3.11 model calls across three tiers. Capacity is metered on three axes at
once — requests/min, input tokens/min, output tokens/min.

**Predict.** Compute each class's ceiling on all three axes and take the minimum.
For a class sending `I` input tokens and producing `O` output tokens:

```
  by RPM   =  RPM_limit / 60
  by ITPM  =  (ITPM_limit / 60) / I
  by OTPM  =  (OTPM_limit / 60) / O
```

**Observe.** Per-class ceilings, requests/sec:

```
  class     tier      by RPM   by ITPM   by OTPM   binds   effective
  guard     haiku      66.7     158.7    1111.1    RPM       66.67
  rewrite   haiku      66.7      74.1     190.5    RPM       66.67
  answer    sonnet     33.3      21.5      32.1    ITPM      21.51
  think     opus        6.7       1.0       2.2    ITPM       0.56
  title     haiku      66.7      41.7     476.2    ITPM      41.67
```

**Why it matters.** The published request limit says `answer` can do 33 req/s. It
can do **21.5** — and for `think`, the RPM limit says 6.7 while the real ceiling is
**0.56**, a factor of twelve. Anyone capacity-planning off the RPM number is wrong
by an order of magnitude on the classes that matter most.

Note `think`'s effective rate (0.56) is *below* even its ITPM ceiling (1.0). That
gap is a second constraint: the output meter is charged against `max_tokens` at
admission and reconciled afterwards, so `max_tokens` caps how many calls can be
**in flight simultaneously**:

```
  concurrency_cap = OTPM_bucket / max_tokens
```

`think` asks for 8192 tokens, writes about 2400, and runs for ~70 seconds. It can
hold 39 in flight where its own token quota would support 69. Sizing `max_tokens`
to the p97 of observed output buys 1.77× on that class — one config line, no
quality change. **On a long-running call, a generous `max_tokens` is a throughput
setting.**

Sustainable rate for the whole product: **6.94 turns/sec**. Write that number down
before tuning anything.

---

### 15.4 Experiment 4 — The knee, and what "over capacity" looks like

```
SETUP
  full defence stack, healthy provider
  sweep offered load from well under capacity to 6x over
```

**Predict.** Below capacity, latency is service time and everything completes.
Above it, §2.2's utilisation curve turns a corner: latency climbs, then goodput
plateaus while completion collapses.

**Observe.**

```
  offered      calls/s   turns answered   goodput   answer p95
   3 turns/s      9           100%          9.8/s     15.8s
   6 turns/s     19           100%         18.9/s     16.4s
  10 turns/s     31           100%         29.4/s     16.5s
  15 turns/s     47            56%         39.1/s     29.3s
  25 turns/s     78            20%         51.5/s     34.2s
  40 turns/s    124            11%         72.0/s     24.8s
```

**Why it matters.** Three readings, in increasing order of how much trouble they
will save you:

1. **The knee is sharp.** Between 10 and 15 turns/sec, completion falls from 100%
   to 56%. There is no gentle slope to watch for on a dashboard — you are fine,
   and then you are not. Provision against the knee, not the average.
2. **Goodput keeps rising while the product collapses.** At 40 turns/s the system
   completes *more calls per second* (72) than at 15 (39), while answering **11%
   of turns instead of 56%**. Calls-per-second is the metric that will be on your
   dashboard, and it says things are improving. Measure completed *user outcomes*,
   not completed requests.
3. **p95 is non-monotonic** — it peaks at 25 turns/s and then *falls* at 40.
   Latency improved because the system started refusing work earlier; the
   survivors are the fast ones. A latency graph alone cannot tell you whether you
   are healthy or shedding hard.

Availability and error rate both *improve* under load shedding, because requests
you never admitted never had a chance to fail. The three metrics that stay honest
are goodput of useful outcomes, shed-rate by criticality, and wasted spend.

---

### 15.5 Experiment 5 — A token bucket's burst is a fuse, and it has a length

The experiment people get wrong, because the effect is delayed.

```
SETUP
  6 turns/sec (comfortably inside capacity)
  at t=20s the provider cuts your quota to 10% of normal
  the variable: how long the cut lasts
```

**Predict.** This one needs arithmetic before you run it. A token bucket holds a
full minute's allowance as burst. After a cut, the bucket drains at
`demand − new_refill_rate`, so the delay before your first 429 is:

```
  drain_time  =  burst_capacity / (demand − new_refill_rate)
```

For this fixture — `answer` at 6 req/s × 6200 input tokens = 37,200 tokens/sec of
demand, against an ITPM limit cut to 800,000/min:

```
  quota   burst          refill      demand       empties after
   10%    800,000 tok    13,333/s    37,200/s        33.5s
    5%    400,000 tok     6,667/s    37,200/s        13.1s
    2%    160,000 tok     2,667/s    37,200/s         4.6s
```

So a **25-second incident at 10% should produce zero 429s** — the burst outlives
the incident.

**Observe.**

```
  incident            429s seen   turns answered
  25s @ quota 10%          0          100%
  70s @ quota 10%         18           67%
  25s @ quota  2%         37           34%
  70s @ quota  2%        162           32%
```

Zero, exactly as predicted, then 18 once the incident outlives the 33.5s fuse.

**Why it matters.** Three consequences that bite in production:

- **A short quota reduction is invisible.** Monitoring shows nothing; the bucket
  absorbed it. That is what burst is *for* — but it means absence of 429s is not
  evidence of absence of a quota problem.
- **The failure arrives late and then all at once**, 33 seconds after the actual
  event, with no proximate cause in your logs. Debugging that without the
  drain-time formula is miserable.
- **Burst hides your own bugs too.** A deploy that doubles prompt size looks fine
  for the first half-minute of every process lifetime.

**Vary it.** Raise load to 25 turns/sec and re-run the 25s @ 10% case. Demand
rises, the fuse shortens, and the same incident now produces 429s. Drain time is a
function of *your* demand, not just their limit.

---

### 15.6 Experiment 6 — Adaptive concurrency against a moving ceiling

```
SETUP
  12 turns/sec
  provider fleet drops to 20% between t=20s and t=45s
  the variable: fixed limit vs AIMD vs Netflix gradient
```

**Predict.** A fixed limit is a guess about someone else's capacity, and it is
wrong in both directions when that capacity moves. Feedback controllers should
shed earlier and generate less provider-side overload.

**Observe.**

```
  controller   turns answered   529s generated   wasted spend
  gradient          34%               30            $3.61
  AIMD              34%               30            $3.60
  fixed             34%               93            $4.80
```

**Why it matters.** Completion is identical across all three — the capacity is
gone, and no client-side controller conjures it back. The difference is entirely
in **what you do to the provider while it is down**: the fixed limit generates
**3× the 529s** and 33% more wasted spend, because it keeps pushing a
pre-configured number of concurrent requests at a fleet that can no longer absorb
them.

Those 529s are provider-wide. A fixed limit does not merely fail to help; it
extends the incident for every tenant, including your own other call classes.

Two implementation details decide whether a controller works at all, and neither
is the algorithm:

1. **Scope it per call class.** Pool a 0.2s classifier and a 9s answer stream
   behind one controller and the classifier's latency becomes the "no-load"
   baseline. Every answer call then reads as congestion, the limit decays to the
   floor, the floor causes real queueing, and the controller reads that as more
   congestion. It death-spirals on its own output.
2. **Feed it time-to-first-token, not total duration.** Total duration on an LLM
   call is dominated by how many tokens the answer needed. A controller fed raw
   latency shrinks the limit whenever users ask harder questions.

A third, if you implement the gradient yourself: the sample window must scale with
the limit. A fixed 24-sample window is fine at limit=100 and fatal at limit=4,
where those samples take a minute to accumulate and the controller sits at the
floor long after recovery.

---

### 15.7 Experiment 7 — Shedding only helps if it contends for the same resource

The result that looks like a bug and is not.

```
SETUP
  25 turns/sec (3.6x capacity), full stack, healthy provider
  the variable: criticality ladder on vs never shed
```

**Observe.**

```
  criticality       class      ladder ON   ladder OFF
  CRITICAL_PLUS     answer         17%         17%
  CRITICAL          guard          93%         83%
  CRITICAL          think          50%         53%
  SHEDDABLE_PLUS    rewrite        81%         61%
  SHEDDABLE         title         100%        100%
```

**Why it matters.** The ladder is supposed to shed `title` first and protect
`answer`. Instead `title` is served **100% either way** and `answer` sits at 17%
regardless.

This is correct, and the reason is the most useful thing in this section: `title`
runs on a tier with enormous headroom, and `answer` runs on one that is saturated.
**Shedding titles frees capacity that `answer` cannot use.** The ladder is not
broken; it is correctly declining to shed work that is not contending for
anything.

What the ladder *did* do is visible in the rows that share a tier: `rewrite` went
from 61% to 81% and `guard` from 83% to 93%, because those three classes share the
Haiku pool, and the ladder reordered *that* contention.

The generalisation:

> A criticality ladder must be scoped to the contended resource. Shedding
> low-priority work that uses a different pool, tier, shard or quota is a no-op
> dressed up as load management.

Before building a shedding policy, answer: *which* resource is exhausted, and
which classes actually compete for it? If your background jobs use a separate
connection pool, shedding them will not help your API latency — and you will spend
a sprint discovering that.

---

### 15.8 A checklist you can take to a capacity review

| Question | How to answer it | Exp. |
|---|---|---|
| What concurrency do we need? | `arrival_rate × service_time`, per resource. Re-derive when service time changes. | 1 |
| Are we concurrency-bound, or is raising the pool a no-op? | Compare pool size against `N × L`. Only row-2 systems benefit. | 1 |
| When we shed, what goes first — and does it use the same resource? | If it uses a different pool, shedding it buys nothing. | 2, 7 |
| Which meter actually binds each call path? | Compute all of them, take the minimum. It is rarely the request limit. | 3 |
| Where is our knee? | Sweep load until completion falls. Provision against that, not the mean. | 4 |
| Is our dashboard metric goodput-of-outcomes or requests-per-second? | The second rises while the product collapses. | 4 |
| How long is our rate-limiter burst — how late does a quota problem surface? | `burst / (demand − refill_rate)`. | 5 |
| Is our concurrency limit fixed? What does it do to the dependency mid-incident? | Fixed limits triple provider-side overload here. | 6 |
| Is our adaptive controller scoped per call class, and fed TTFT? | If it pools dissimilar calls, it decays to the floor. | 6 |

If you can produce those numbers for your own system, you can predict its overload
behaviour instead of discovering it.

---

> **Further reading:** Google SRE Book, Chapter 21 "Handling Overload"; Netflix Technology Blog, "Performance Under Load" (2018); Kathleen Nichols & Van Jacobson, "Controlling Queue Delay" (CoDel, ACM Queue 2012); TCP Congestion Avoidance (Jacobson, 1988); Amazon Builders' Library, "Using load shedding to avoid overload"; Bronson et al., "Metastable Failures in Distributed Systems" (OSDI 2022); Kingman, "The single server queue in heavy traffic" (1961).
