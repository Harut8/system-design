# Resilience Patterns: Circuit Breakers, Bulkhead Thread Pools, Retries, and Fallbacks

A production-grade reference covering the defensive patterns that keep distributed systems alive when dependencies fail. Covers retry safety and amplification math, circuit breaker state machines and their own failure modes, bulkhead isolation at every level, timeout layering, deadline propagation, and the correct composition of all patterns into a full defense stack. Written for Staff+ engineers who build, debug, and operate production microservice architectures.

Prerequisites: familiarity with distributed system failure models from `00-primitives-and-system-models.md` and failure detection from `29-failure-detection-phi-accrual.md`.

---

## Table of Contents

1. [Why Resilience Patterns Exist](#1-why-resilience-patterns-exist)
2. [Retry Patterns — The Deceptively Dangerous Pattern](#2-retry-patterns--the-deceptively-dangerous-pattern)
3. [Circuit Breaker Pattern — Deep Dive](#3-circuit-breaker-pattern--deep-dive)
4. [Bulkhead Pattern](#4-bulkhead-pattern)
5. [Timeout Patterns](#5-timeout-patterns)
6. [Combining Patterns — The Full Defense Stack](#6-combining-patterns--the-full-defense-stack)
   - [6.4 Pattern Comparison — When to Use What](#64-pattern-comparison--when-to-use-what) *(includes Bulkhead vs. Rate Limiter, Little's Law math)*
7. [Testing Resilience Patterns](#7-testing-resilience-patterns)
8. [Production Tradeoff Matrix](#8-production-tradeoff-matrix)
9. [Real-World Resilience — Production Case Studies](#9-real-world-resilience--production-case-studies)
   - [9.1 Payment Processing (Stripe/Adyen)](#91-payment-processing-service-stripeadyen-integration)
   - [9.2 Background Job Processing (Queue Workers)](#92-background-job-processing-queue-workers)
   - [9.3 LLM API Calls (OpenAI/Anthropic)](#93-llm-api-calls-openai--anthropic-integration)
   - [9.4 E-Commerce Product Page (Aggregation)](#94-e-commerce-product-page-aggregation-service)
   - [9.5 Microservice-to-Database](#95-microservice-to-database-internal-dependency)
   - [9.6 Third-Party Webhook Delivery](#96-third-party-webhook-delivery-outbound)
   - [9.7 How to Derive Numbers for YOUR System](#97-how-to-derive-numbers-for-your-system)
10. [Interview Preparation](#10-interview-preparation--resilience-patterns)
11. [Sandbox Experiments — Run These Yourself](#11-sandbox-experiments--run-these-yourself)

---

## 1. Why Resilience Patterns Exist

### 1.1 Partial Failure Is the Norm

In a monolith, a function call either succeeds or throws an exception. In a distributed system, a call can succeed, fail, succeed on the server but fail to deliver the response, hang indefinitely, succeed slowly enough to be useless, or succeed on the first try and then fail on retries that the client should never have sent. Partial failure -- where some components work while others do not -- is not an exceptional condition. It is the steady state.

A system with 20 microservice dependencies, each at 99.9% availability, has a per-request probability of encountering at least one dependency failure of roughly `1 - 0.999^20 = ~2%`. At 10,000 requests per second, that is 200 requests per second touching a degraded path. Resilience patterns exist because partial failure is not something you can prevent; it is something you must survive.

### 1.2 The Cascade Failure Problem

The critical failure mode in microservice architectures is not a single service crashing. It is a single slow or failing service pulling down every service that depends on it, which in turn pulls down every service that depends on them. This is a cascade failure, and it follows a predictable anatomy.

```
ANATOMY OF A CASCADE FAILURE (with timing):

t=0s    Database connection pool on Service C saturates (slow queries)
        C's response latency rises from 50ms to 8 seconds

t=2s    Service B, which calls C with a 10s timeout, starts accumulating
        blocked threads. B's thread pool (200 threads) begins filling.

t=15s   B's thread pool is exhausted. B can no longer accept ANY requests,
        including requests that don't touch C at all.
        B starts returning 503 to all callers.

t=16s   Service A, which calls B, begins timing out. A's thread pool
        starts filling with requests blocked on B.

t=30s   A's thread pool is exhausted. A returns 503 to the load balancer.
        The load balancer routes traffic to A's other instances.

t=31s   The other instances of A absorb the redirected traffic, doubling
        their load. They begin exhausting their own thread pools.

t=45s   ALL instances of A are down. The entire product is unavailable.

t=45s   Root cause: one slow query in Service C's database.

TIMELINE:

  0s         15s         30s         45s
  |           |           |           |
  C slow   B down      A down     Total outage
  =====================================>
  [  One slow dependency destroys everything  ]
```

The critical insight is at `t=15s`: Service B dies not because it crashed, but because all of its threads are blocked waiting for a dependency that will never respond quickly. Every thread in B is alive, consuming memory and CPU, doing nothing useful. This is **resource exhaustion through dependency coupling**, and it is the single most common cause of cascade failures in microservice architectures.

### 1.3 Why Timeouts Alone Are Not Enough

Timeouts prevent threads from blocking forever, but they do not prevent the damage that occurs during the timeout window. If Service C's latency is 8 seconds and B's timeout is 10 seconds, B's threads are still blocked for 8 seconds each. With a 200-thread pool and 500 requests per second arriving, B exhausts its threads in under half a second: `200 threads / 500 rps = 0.4 seconds` of incoming requests fills the pool while existing threads wait 8 seconds each.

Timeouts are necessary but not sufficient. They must be combined with patterns that limit how many resources a single dependency can consume (bulkheads), stop sending requests to a known-broken dependency (circuit breakers), intelligently retry when retries will help (retries with budgets), and gracefully degrade when a dependency is unavailable (fallbacks).

---

## 2. Retry Patterns -- The Deceptively Dangerous Pattern

### The Simple Explanation

A retry is exactly what it sounds like: if a request fails, try again. You do this every day — if a webpage doesn't load, you hit refresh. The problem is that computers do this at scale. If one person refreshes a page, that's fine. If 10,000 servers all "refresh" at the same time against a struggling backend, you've just turned a sick patient into a dead one by piling more weight on them.

Think of it like a restaurant. A waiter goes to the kitchen and the order gets lost. Sending it again makes sense — it was a one-off mistake. But if the kitchen is on fire and the waiter keeps re-submitting the same order every 10 seconds, they're not helping. They're blocking the doorway and adding papers to a kitchen that's literally burning. That's what retries do to a failing service at scale.

**The core tension**: retries fix transient glitches but amplify sustained failures. The entire section below is about keeping the first behavior and preventing the second.

Retries are the single most common resilience pattern and, simultaneously, the single most common cause of making outages worse. Every production outage postmortem collection at scale -- Google, Amazon, Meta -- contains incidents where retries turned a partial failure into a total failure.

### 2.1 Retry Storms and Retry Amplification

The math of retry amplification is the most important thing to internalize about retries. Consider a simple three-tier architecture:

```
RETRY AMPLIFICATION IN A 3-TIER SYSTEM:

  Service A (retries 3x) --> Service B (retries 3x) --> Service C (fails)

  One user request to A causes:
    A sends request to B.        B sends request to C.       C fails.
    B retries to C.              C fails again.
    B retries to C.              C fails again.
    B retries to C.              C fails again.
    A sees B fail (after B's 3 retries).
    A retries to B.              B retries 3x to C.          3 more failures.
    A retries to B.              B retries 3x to C.          3 more failures.
    A retries to B.              B retries 3x to C.          3 more failures.

  Total requests hitting C: 3 x 3 = 9 retries per original request.

GENERALIZED FORMULA:

  For N layers of services, each retrying R times:

    Total requests at the bottom = R^N

  With R=3 and N=5 layers:  3^5 = 243 requests per user request.
  With R=3 and N=7 layers:  3^7 = 2,187 requests per user request.

AMPLIFICATION TREE (3 layers, 3 retries each):

  User request
  └── A try 1
  │   └── B try 1
  │   │   └── C try 1  [FAIL]
  │   │   └── C try 2  [FAIL]
  │   │   └── C try 3  [FAIL]
  │   └── B try 2
  │   │   └── C try 1  [FAIL]
  │   │   └── C try 2  [FAIL]
  │   │   └── C try 3  [FAIL]
  │   └── B try 3
  │       └── C try 1  [FAIL]
  │       └── C try 2  [FAIL]
  │       └── C try 3  [FAIL]
  └── A try 2
  │   └── [same 9 calls to C]
  └── A try 3
      └── [same 9 calls to C]

  Total C invocations: 3^3 = 27
```

When Service C is struggling under load, the worst possible thing you can do is multiply the load it receives by 27. Yet this is exactly what naive retry policies do. Retries turn a partial outage into a total outage by ensuring that a struggling service receives more traffic precisely when it can handle the least.

### 2.2 Safe Retry Design

Safe retries require answering five questions before any retry is sent: Is the operation idempotent? Has the retry budget been exceeded? Has enough time elapsed since the last attempt? Is there enough remaining deadline to make a retry worthwhile? Is the failure actually retryable?

#### 2.2.1 Idempotency: The Prerequisite for Retries

A retry is only safe if the operation is idempotent -- executing it twice produces the same result as executing it once. `GET /user/123` is naturally idempotent. `POST /charges` is not: retrying it might charge the customer twice.

The standard solution is **idempotency keys**: a UUID generated client-side and sent with each request. The server stores the idempotency key alongside the result of the first execution. On a retry carrying the same key, the server returns the stored result without re-executing the operation.

```
IDEMPOTENCY KEY IMPLEMENTATION:

  Client                            Server
    |                                  |
    |  POST /charges                   |
    |  Idempotency-Key: 550e8400...    |
    |  Amount: $49.99                  |
    |  ─────────────────────────────>  |
    |                                  | 1. Check key store: not found
    |                                  | 2. BEGIN transaction
    |                                  | 3. INSERT into idempotency_keys
    |                                  |    (key, status=processing)
    |                                  | 4. Execute charge logic
    |                                  | 5. UPDATE idempotency_keys
    |                                  |    (key, status=complete, response=...)
    |                                  | 6. COMMIT transaction
    |  <─────────────────────────────  |
    |  201 Created                     |
    |                                  |
    |  (network drops response)        |
    |                                  |
    |  POST /charges (retry)           |
    |  Idempotency-Key: 550e8400...    |
    |  Amount: $49.99                  |
    |  ─────────────────────────────>  |
    |                                  | 1. Check key store: FOUND
    |                                  | 2. Return stored response
    |  <─────────────────────────────  |
    |  201 Created (same response)     |

  CRITICAL IMPLEMENTATION DETAILS:
  ─────────────────────────────────────────────────────────────────
  - The key insertion and the operation MUST be in the same transaction.
    Otherwise a crash between inserting the key and executing the operation
    leaves a "phantom" key that blocks all future retries permanently.

  - Keys should expire (e.g., 24-48 hours). Without expiration, the key
    store grows without bound.

  - A "processing" status handles concurrent retries: if the key exists
    with status=processing, return 409 Conflict, not the result.

  - The key MUST be generated by the client, not the server.
    A server-generated key defeats the purpose: the client has no way
    to identify retries of the same logical operation.
```

#### 2.2.2 Retry Budgets

Per-request retry limits (e.g., "retry 3 times") are a blunt instrument. They do not account for the global impact of retries on the downstream service. Google's SRE practices use a **retry budget**: limit total retries to a percentage of total request volume.

```
RETRY BUDGET (Google SRE approach):

  Rule: retries may not exceed 10% of total outgoing requests.

  Example:
    Service B sends 1,000 rps to Service C.
    Budget = 1,000 * 0.10 = 100 retry requests per second.

  When C is healthy (0.1% failure rate):
    Failed requests: 1 per second. Retries used: ~1/sec. Budget: fine.

  When C is degraded (20% failure rate):
    Failed requests: 200 per second. Retries available: 100 per second.
    Half the failed requests get retried. Total load on C: 1,100 rps.
    This is a 10% increase — survivable for C.

  Without retry budget (3 retries per request, 20% failure rate):
    Failed requests: 200 per second. Retries: 200 * 3 = 600 per second.
    Total load on C: 1,600 rps — a 60% increase.
    C was already struggling at 1,000 rps. 1,600 kills it.

  IMPLEMENTATION:
    Use a token bucket. Initialize with budget_size tokens.
    Refill at rate = primary_request_rate * budget_percentage.
    Each retry consumes one token. No token → no retry → fail fast.

  CRITICAL RULE:
    Successful requests SHOULD refill the token bucket.
    This provides natural backpressure: when the downstream recovers,
    the budget refills organically through successful requests.
```

#### 2.2.3 Retry Context Propagation

In a multi-tier system, the remaining retry budget and request deadline must be propagated downstream. If Service A has 500ms remaining on its deadline, it is pointless for Service B to retry with 300ms timeouts -- there is not enough time for even one retry to complete and still leave time for A to process the result.

gRPC handles this natively through deadline propagation: the remaining time is passed in the `grpc-timeout` header, and each service in the chain can see how much time remains. HTTP services must implement this manually, typically through a custom header like `X-Request-Deadline` carrying a Unix timestamp.

### The Math You Need to Internalize

```
WHY RETRY MATH MATTERS — INTUITIVE WALKTHROUGH:

  Imagine you're a teacher grading papers. A student submits an essay,
  but your printer jams. The student resubmits. Fine — 1 extra copy.
  That's a retry.

  Now imagine 3 teachers in a chain: Teacher A gives work to Teacher B,
  who gives it to Teacher C. Each teacher resends 3 times if they don't
  get a response.

  Teacher C's printer jams (the root failure).

  Teacher B sends to C, no response. B retries 3 times = 3 copies at C.
  Teacher A sends to B, no response (B was busy retrying).
  A retries 3 times to B. Each time, B retries 3 times to C.

  Total copies at C's broken printer: 3 × 3 = 9.
  Add A's own 3 attempts routing through B: 3 × 3 × 1 = 9 per A attempt.
  A tries 3 times: 3 × 9 = 27 total.

  GENERAL FORMULA:  R^N  (R = retries per layer, N = number of layers)

  This is EXPONENTIAL growth. It's the same math as compound interest,
  but working against you:

    2 layers, 3 retries:  3^2 =     9x amplification
    3 layers, 3 retries:  3^3 =    27x amplification
    5 layers, 3 retries:  3^5 =   243x amplification
    7 layers, 3 retries:  3^7 = 2,187x amplification

  A service already struggling under 1,000 rps now receives 243,000 rps.
  That's not recovery — that's a DDoS attack from your own infrastructure.

  THE FIX — RETRY BUDGETS (percentage-based, not count-based):

  Instead of "each request can retry 3 times," the rule is:
  "total retries across ALL requests cannot exceed 10% of normal traffic."

  At 1,000 rps normal traffic:
    Budget = 100 retries/second total, shared across all callers.

  At 20% failure rate (200 failures/second):
    Only 100 get retried. Total load = 1,100 rps (+10%).
    Compare with per-request 3x retries: 1,000 + 600 = 1,600 rps (+60%).

  The budget prevents retries from becoming the dominant source of load.

  BUDGET MATH:
    budget_tokens = normal_rps × budget_percentage
    refill_rate   = successful_requests_per_second (natural backpressure)

  Token bucket implementation: start with budget_tokens.
  Each retry costs 1 token. No tokens → no retry → fail fast.
  Successful requests refill tokens, so when the downstream recovers,
  the budget organically refills.
```

### 2.3 Exponential Backoff

Linear backoff (wait 1s, 2s, 3s, 4s) is dangerous because it does not create enough spacing between retries when the downstream is recovering. Exponential backoff is the baseline:

```
EXPONENTIAL BACKOFF FORMULA:

  delay = min(base * 2^attempt, maxDelay)

  Parameters:
    base     = initial delay (e.g., 100ms)
    attempt  = retry attempt number (0-indexed)
    maxDelay = cap to prevent absurd waits (e.g., 30s)

  Example with base=100ms, maxDelay=30s:
    Attempt 0:  min(100ms * 2^0, 30s) = 100ms
    Attempt 1:  min(100ms * 2^1, 30s) = 200ms
    Attempt 2:  min(100ms * 2^2, 30s) = 400ms
    Attempt 3:  min(100ms * 2^3, 30s) = 800ms
    Attempt 4:  min(100ms * 2^4, 30s) = 1,600ms
    Attempt 5:  min(100ms * 2^5, 30s) = 3,200ms
    ...
    Attempt 8:  min(100ms * 2^8, 30s) = 25,600ms
    Attempt 9:  min(100ms * 2^9, 30s) = 30,000ms (capped)
```

### 2.4 Jitter -- Critically Important

Exponential backoff without jitter creates **thundering herds**. If 1,000 requests fail at `t=0`, all 1,000 retry at `t=100ms`, all fail again, all retry at `t=200ms`, and so on. The downstream service sees periodic spikes of exactly 1,000 requests. Jitter randomizes retry timing so that retries spread across the delay window instead of arriving in synchronized bursts.

```
JITTER STRATEGIES:

  Let cap = maxDelay, base = initial delay, attempt = retry number.

  ┌────────────────────┬──────────────────────────────────────────────────────┐
  │  Strategy          │  Formula                                            │
  ├────────────────────┼──────────────────────────────────────────────────────┤
  │  No Jitter         │  sleep = min(cap, base * 2^attempt)                 │
  │  (NEVER USE THIS)  │                                                     │
  ├────────────────────┼──────────────────────────────────────────────────────┤
  │  Full Jitter       │  sleep = random(0, min(cap, base * 2^attempt))      │
  │                    │                                                     │
  │                    │  Maximizes spread. Sleep can be as low as 0, which  │
  │                    │  means some retries fire immediately. Generally the │
  │                    │  best default choice per AWS analysis.              │
  ├────────────────────┼──────────────────────────────────────────────────────┤
  │  Equal Jitter      │  temp = min(cap, base * 2^attempt)                  │
  │                    │  sleep = temp/2 + random(0, temp/2)                 │
  │                    │                                                     │
  │                    │  Guarantees a minimum sleep of temp/2 (the non-     │
  │                    │  random component). Useful when you want spread but │
  │                    │  cannot tolerate near-zero sleep values.            │
  ├────────────────────┼──────────────────────────────────────────────────────┤
  │  Decorrelated      │  sleep = min(cap, random(base, prev_sleep * 3))     │
  │  Jitter            │                                                     │
  │                    │  Each retry's delay depends on the PREVIOUS delay,  │
  │                    │  not the attempt number. Creates decorrelated retry │
  │                    │  timing across clients even if they started at the  │
  │                    │  same time. Slightly more aggressive — tends toward │
  │                    │  longer sleeps than full jitter.                    │
  └────────────────────┴──────────────────────────────────────────────────────┘

  COMPARISON (AWS analysis — "Exponential Backoff And Jitter" blog):
  ─────────────────────────────────────────────────────────────────────
  Scenario: 100 clients contending for a single resource, measured by
  total work completed and total number of calls made.

  │ Strategy       │ Total Calls │ Completion Time │ Recommendation      │
  ├────────────────┼─────────────┼─────────────────┼─────────────────────┤
  │ No Jitter      │ Highest     │ Highest         │ Never               │
  │ Full Jitter    │ Lowest      │ Lowest          │ Default choice      │
  │ Equal Jitter   │ Moderate    │ Moderate        │ When min delay      │
  │                │             │                 │ matters             │
  │ Decorrelated   │ Low         │ Low             │ Stateful alt to     │
  │                │             │                 │ full jitter         │

  Full jitter wins because it maximizes the spread of retry timing,
  giving the downstream the most even distribution of load across time.
```

### 2.5 Retry Classification: Which Errors Are Retryable?

Retrying a non-retryable error wastes resources and can cause harm. Retrying a `400 Bad Request` will fail forever because the request itself is invalid. Retrying a `POST /charge` that returned a network timeout might double-charge the customer.

```
HTTP STATUS CODE RETRY CLASSIFICATION:

  RETRYABLE (transient server/infrastructure errors):
  ┌─────────┬────────────────────────────────────────────────────────────┐
  │  429    │  Too Many Requests — respect Retry-After header           │
  │  500    │  Internal Server Error — generic, possibly transient      │
  │  502    │  Bad Gateway — upstream crashed, may recover               │
  │  503    │  Service Unavailable — overloaded or in maintenance       │
  │  504    │  Gateway Timeout — upstream slow, may recover              │
  └─────────┴────────────────────────────────────────────────────────────┘

  NOT RETRYABLE (client errors or permanent conditions):
  ┌─────────┬────────────────────────────────────────────────────────────┐
  │  400    │  Bad Request — fix the payload, not the retry count       │
  │  401    │  Unauthorized — retry will fail until credentials refresh │
  │  403    │  Forbidden — permission issue, not transient               │
  │  404    │  Not Found — resource does not exist                       │
  │  409    │  Conflict — application-level conflict, needs resolution   │
  │  422    │  Unprocessable Entity — semantic error in request          │
  └─────────┴────────────────────────────────────────────────────────────┘

  SPECIAL CASE — 401 Unauthorized:
    Retryable ONLY if you refresh credentials between retries (e.g.,
    rotating an expired OAuth token). Retrying with the same expired
    token is pointless.

gRPC STATUS CODE RETRY CLASSIFICATION:

  RETRYABLE:
  ┌──────────────────┬───────────────────────────────────────────────────┐
  │  UNAVAILABLE     │  Transient. The gRPC retry policy default.       │
  │  RESOURCE_       │  Server out of resources. May recover.            │
  │  EXHAUSTED       │                                                   │
  │  ABORTED         │  Transaction conflict. Can retry with new txn.   │
  └──────────────────┴───────────────────────────────────────────────────┘

  NOT RETRYABLE:
  ┌──────────────────┬───────────────────────────────────────────────────┐
  │  INVALID_        │  Client sent bad data. Will always fail.          │
  │  ARGUMENT        │                                                   │
  │  NOT_FOUND       │  Resource missing. Won't appear on retry.         │
  │  ALREADY_EXISTS  │  Duplicate creation. Retry makes it worse.        │
  │  PERMISSION_     │  AuthZ failure. Retry won't help.                 │
  │  DENIED          │                                                   │
  │  UNAUTHENTICATED │  AuthN failure (unless token refresh occurs).     │
  └──────────────────┴───────────────────────────────────────────────────┘

  AMBIGUOUS:
  ┌──────────────────┬───────────────────────────────────────────────────┐
  │  INTERNAL        │  Bug on server. May or may not be transient.      │
  │                  │  Often NOT retryable in practice.                  │
  │  DEADLINE_       │  Timeout. Retryable only if the operation was     │
  │  EXCEEDED        │  idempotent and there is remaining budget.        │
  └──────────────────┴───────────────────────────────────────────────────┘

  TIMEOUT SUBTLETY — connect timeout vs read timeout:
  ─────────────────────────────────────────────────────────────────────
  Connect timeout: the TCP handshake did not complete. The server never
  saw the request. SAFE to retry even non-idempotent operations — the
  server never executed anything.

  Read timeout: the TCP connection was established, the request was sent,
  but no response arrived in time. The server MAY have executed the
  request. NOT SAFE to retry non-idempotent operations without an
  idempotency key.
```

### 2.6 Hedged Requests

Hedged requests are a technique from Google's "The Tail at Scale" paper (Dean & Barroso, 2013). Instead of waiting for a single request to timeout and then retrying, you send a second copy of the request to a different backend after a short delay (e.g., the p95 latency). The first response wins; the slower request is cancelled.

```
HEDGED REQUESTS:

  t=0ms    Client sends request to Backend A.
  t=10ms   No response yet (p95 is 8ms — this request is in the tail).
           Client sends the SAME request to Backend B.
  t=12ms   Backend B responds. Client uses this response.
           Client cancels the in-flight request to Backend A.

  WHEN HEDGING HELPS:
    - Tail latency is caused by per-request variance (GC pauses, queue
      depth, disk seek), not systemic overload
    - Backends are stateless or idempotent
    - You have multiple replicas to spread the hedge across
    - The hedge delay is at the p95 or higher (not p50 — that doubles load)

  WHEN HEDGING MAKES THINGS WORSE:
    - The downstream is overloaded (you just doubled the load)
    - The operation is non-idempotent (double execution risk)
    - All replicas share a bottleneck (same database, same disk)
    - The hedge fires too early (below p90), doubling baseline load

  CANCELLATION IS CRITICAL:
    If you don't cancel the redundant in-flight request, every hedged
    request doubles the downstream load permanently. gRPC cancellation
    propagation handles this; HTTP requires cooperative cancellation
    (e.g., client drops the connection and server checks for broken pipe).
```

---

## 3. Circuit Breaker Pattern -- Deep Dive

### The Simple Explanation

A circuit breaker is the "stop calling them, they're clearly not answering" pattern.

Imagine you're calling a friend's phone. First call — no answer. Second call — no answer. Third call — no answer. At this point, a reasonable person stops calling and tries again in 30 minutes. An unreasonable person calls 500 more times in the next minute. A circuit breaker makes your service behave like the reasonable person.

The name comes from your home's electrical panel. When a circuit draws too much current (a short circuit), the breaker trips and cuts the power. This prevents the wiring from catching fire. You fix the problem, then flip the breaker back on. Software circuit breakers work the same way: when a dependency is failing too often, the breaker "trips" and stops all requests to it. After a cooldown period, it lets a few test requests through. If they succeed, the breaker closes and normal traffic resumes.

**The key insight**: a circuit breaker is NOT about giving up. It's about giving the failing service breathing room to recover. If a restaurant kitchen is overwhelmed, the best thing the host can do is stop seating new tables for 15 minutes. The kitchen catches up, and then you resume seating. Without that pause, the kitchen never recovers.

**Circuit breaker vs. timeout**: A timeout says "I'll wait 3 seconds for you to answer, then give up on THIS request." A circuit breaker says "You've failed 50 times in a row — I'm not going to ask you ANYTHING for the next 30 seconds." Timeouts are per-request. Circuit breakers are per-dependency, across all requests.

**Circuit breaker vs. retry**: Retries say "that failed, let me try again." Circuit breakers say "that's been failing so much, I'm not even going to try." They work together: retries handle transient blips, and the circuit breaker kicks in when retries prove the problem isn't transient.

### 3.1 Origin and Purpose

The circuit breaker pattern, introduced by Michael Nygard in "Release It!" (2007), is modeled on electrical circuit breakers. When a downstream service is failing, the circuit breaker stops sending requests to it, allowing the downstream to recover and preventing the caller from wasting resources on requests that will fail.

### 3.2 State Machine

The circuit breaker is a three-state machine:

```
CIRCUIT BREAKER STATE MACHINE:

  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │   ┌──────────┐    failure threshold    ┌──────────┐                │
  │   │          │      exceeded           │          │                │
  │   │  CLOSED  │ ───────────────────────> │   OPEN   │                │
  │   │          │                          │          │                │
  │   │ (normal  │                          │ (reject  │                │
  │   │  traffic │                          │  all     │                │
  │   │  flows)  │                          │  calls)  │                │
  │   │          │                          │          │                │
  │   └──────────┘                          └────┬─────┘                │
  │        ^                                     │                      │
  │        │                                     │ wait duration        │
  │        │                                     │ expires              │
  │        │                                     ▼                      │
  │        │        probe succeeds         ┌──────────┐                │
  │        └────────────────────────────── │ HALF-OPEN│                │
  │                                        │          │                │
  │                 probe fails            │ (limited │                │
  │          ┌────────────────────────────  │  probes  │                │
  │          │                             │  sent)   │                │
  │          │                             └──────────┘                │
  │          ▼                                                          │
  │   Back to OPEN                                                      │
  │   (reset wait timer)                                                │
  │                                                                     │
  └─────────────────────────────────────────────────────────────────────┘

  STATE DESCRIPTIONS:
  ─────────────────────────────────────────────────────────────────────
  CLOSED:    Normal operation. Requests flow through. Failures are
             counted. When the failure rate or count exceeds the
             configured threshold, transition to OPEN.

  OPEN:      All requests are immediately rejected (fail fast) without
             being sent to the downstream. A timer runs. When the timer
             expires, transition to HALF-OPEN.

  HALF-OPEN: A limited number of probe requests are allowed through.
             If the probes succeed, the downstream is assumed healthy:
             transition to CLOSED. If any probe fails, transition
             back to OPEN and reset the wait timer.
```

### 3.3 Implementation Details That Matter

#### 3.3.1 Sliding Window Types

The mechanism for tracking failure rates determines how quickly the breaker responds to changes.

```
COUNT-BASED SLIDING WINDOW:

  Ring buffer of the last N calls (e.g., N=100).
  Each call outcome (success/failure/slow) overwrites the oldest entry.
  Failure rate = failures_in_buffer / N.
  Trip when failure_rate > threshold (e.g., 50%).

  Pros: Simple. Deterministic. Predictable memory usage.
  Cons: At low traffic, the window spans a long wall-clock time.
        A burst of failures from 30 minutes ago still counts.

TIME-BASED SLIDING WINDOW:

  Circular array of T time buckets (e.g., 10 buckets of 1 second each
  = 10-second window). Each bucket tracks call counts and failure counts.
  Failure rate = sum(failures) / sum(total_calls) over all buckets.

  Pros: Reflects recent behavior regardless of traffic volume.
  Cons: At very low traffic (1 rps), a single failure = 100% failure
        rate. Needs a minimum-calls threshold.

THE MINIMUM-CALLS PROBLEM:
─────────────────────────────────────────────────────────────────────
  If the circuit breaker trips on failure_rate > 50% and you've only
  had 2 calls in the window (1 success, 1 failure = 50%), you trip
  the breaker on what could be normal variance.

  Solution: require a minimum number of calls before evaluating the
  failure rate. Resilience4j defaults to minimumNumberOfCalls=100.
  With fewer calls in the window, the breaker stays CLOSED regardless
  of failure rate.
```

#### 3.3.2 Half-Open Probing

The half-open state determines how the circuit breaker detects recovery. The probe strategy matters.

```
HALF-OPEN PROBE STRATEGIES:

  SINGLE PROBE:
    Let exactly one request through. If it succeeds, close the breaker.
    Problem: one successful request is weak evidence. The downstream
    might succeed sporadically while still mostly failing.

  N PROBES (e.g., 10):
    Let N requests through. Require success_rate > threshold to close.
    Problem: if the downstream is still failing, you just sent it N
    requests that will fail. On a recently recovered service, N
    simultaneous probes from many circuit breaker instances become
    a burst of traffic.

  PERCENTAGE RAMP:
    Start at 5% traffic, then 10%, 25%, 50%, 100%.
    If failure rate stays below threshold at each level, advance.
    If any level exceeds the threshold, return to OPEN.
    This is traffic-based canary recovery.
    Problem: complex to implement. Most libraries don't support this.
    Used in production at large scale (Envoy's outlier detection
    with success_rate ejection and recovery).
```

### 3.4 Safety Concerns with Circuit Breakers

Circuit breakers are protective patterns that can themselves cause outages. Understanding their failure modes is essential.

#### 3.4.1 The Circuit Breaker That Makes Things Worse

```
FAILURE MODE: Overly aggressive tripping

  Scenario: Service C has a 2% error rate (within SLO).
  Circuit breaker configured with failure_threshold=1%,
  sliding_window_size=50.

  Result: The breaker trips during NORMAL operation. 100% of requests
  to C are blocked, even though 98% of them would have succeeded.

  The circuit breaker turned a 2% degradation into a 100% outage.

  Prevention:
  - Set thresholds well above the baseline error rate.
  - Use the slow-call-rate threshold (Resilience4j):
    trip on latency degradation, not just errors.
  - Monitor the gap between error rate and breaker threshold.
    Alert if they converge.
```

#### 3.4.2 Synchronized Circuit Breakers

```
FAILURE MODE: Synchronized trip and recovery

  Scenario: 50 instances of Service B each have a circuit breaker to
  Service C. A brief spike causes all 50 breakers to trip at t=0.

  All 50 breakers have wait_duration=30s.

  At t=30s, all 50 breakers enter HALF-OPEN simultaneously.
  Each allows 1 probe request through.
  Service C, which just recovered, receives 50 simultaneous probe
  requests. If C was barely handling normal load, this probe burst
  may cause C to fail again, tripping all breakers back to OPEN.

  This creates an oscillation:
    t=0s    All OPEN (trip)
    t=30s   All HALF-OPEN (probe burst fails, C overloaded)
    t=30s   All OPEN again (retrip)
    t=60s   All HALF-OPEN (probe burst fails again)
    ...repeats indefinitely...

  The circuit breaker has created a sustained outage that prevents
  recovery. C can only recover if the probe load is small enough
  for it to handle, but synchronized breakers guarantee the probe
  load is large.

  SOLUTIONS:
  ─────────────────────────────────────────────────────────────────
  1. Jittered wait duration:
     wait_duration = base_wait + random(0, base_wait * 0.5)
     This spreads HALF-OPEN transitions across time.

  2. Per-instance circuit breakers (already the default):
     Each instance tracks its own failure counts, so they may trip
     at slightly different times naturally.

  3. Randomized probe count:
     In HALF-OPEN, each instance independently decides whether to
     send a probe with probability p (e.g., p=0.1). On average,
     only 10% of instances probe simultaneously.

  4. Centralized circuit breaker state (advanced):
     A single coordinated breaker (via shared state in Redis or
     etcd) that allows exactly 1 probe globally. Adds a dependency
     on the state store. Used rarely.
```

#### 3.4.3 Circuit Breakers Hiding Real Failures

```
FAILURE MODE: Silent degradation

  Scenario: The circuit breaker to Service C is OPEN. The fallback
  returns cached data. Users see stale but functional responses.
  Nobody notices that C has been down for 4 hours because:
    - No error rate alarm: the breaker suppresses errors
    - No latency alarm: responses are fast (from cache)
    - No traffic alarm on C: the breaker blocks all traffic

  C's database ran out of disk space 4 hours ago. Nobody has
  investigated because the breaker is "handling it."

  PREVENTION:
  ─────────────────────────────────────────────────────────────────
  ALWAYS alert on circuit breaker state changes.

  Required alerts:
  1. Circuit breaker OPENED (severity: warning → page if sustained)
  2. Circuit breaker has been OPEN for > X minutes (severity: page)
  3. Circuit breaker is oscillating OPEN/CLOSED (severity: warning)
  4. Fallback activation rate exceeds Y% (severity: warning)

  The circuit breaker buys you TIME to investigate, not PERMISSION
  to ignore the failure.
```

### 3.5 What to Do When the Circuit Is Open — Fallback Strategies

A fallback is the "plan B" pattern. When the primary path fails, what do you show the user?

Think of it like a restaurant menu. The chef's special (fresh tuna) isn't available tonight. Your options, from best to worst: (1) serve yesterday's tuna that was refrigerated (cached/stale data — edible but not as fresh), (2) serve the rest of the meal without the tuna (degraded response — incomplete but functional), (3) take the order and promise to deliver the tuna tomorrow (queue for later), (4) serve a generic fish that's always in stock (static defaults), (5) tell the customer "sorry, no tuna tonight" (fail fast — honest error).

The choice depends on the domain. Stale stock prices are dangerous. Stale profile photos are fine. A missing recommendations widget is acceptable. A missing checkout button is not. **Every fallback decision is a domain decision, not a technical one.**

```
FALLBACK HIERARCHY (from most to least desirable):

  ┌──────────────────────────────────────────────────────────────────┐
  │  1. CACHED / STALE DATA                                         │
  │     Return the last known good response from a local cache.     │
  │     Include a freshness indicator (e.g., "Data as of 10m ago"). │
  │     Risk: stale data may be dangerously wrong (stale prices,    │
  │     stale inventory counts, stale permissions).                 │
  ├──────────────────────────────────────────────────────────────────┤
  │  2. DEGRADED RESPONSE                                           │
  │     Omit the data from the failed dependency. Return the rest.  │
  │     Example: product page without reviews, dashboard without    │
  │     the recommendations widget.                                 │
  │     Risk: users may not realize the response is incomplete.     │
  ├──────────────────────────────────────────────────────────────────┤
  │  3. QUEUE FOR LATER                                             │
  │     Accept the request and queue it for async processing when   │
  │     the dependency recovers. Acknowledge with "accepted for     │
  │     processing."                                                │
  │     Risk: unbounded queue growth if the dependency is down for  │
  │     a long time. Users may not understand async fulfillment.    │
  ├──────────────────────────────────────────────────────────────────┤
  │  4. STATIC DEFAULTS                                             │
  │     Return hardcoded default values.                            │
  │     Example: feature flags service down → use last-deployed     │
  │     defaults baked into the binary.                             │
  │     Risk: defaults may be incorrect or outdated.                │
  ├──────────────────────────────────────────────────────────────────┤
  │  5. FAIL FAST                                                   │
  │     Return a clear error immediately. The user sees an error,   │
  │     but the system does not degrade further. Include a machine- │
  │     readable error code (not just 500) so the caller's retry    │
  │     logic can classify it correctly.                            │
  │     Risk: user-visible errors. But honest errors are better     │
  │     than silent corruption.                                     │
  └──────────────────────────────────────────────────────────────────┘

  CRITICAL RULE: EVERY FALLBACK MUST HAVE AN ALERT.
  ─────────────────────────────────────────────────────────────────
  Fallbacks hide failures. If you use a fallback, you MUST alert
  on fallback activation so someone investigates the root cause.
  A fallback without an alert is a time bomb: it masks failures
  until the fallback itself breaks, and then you have two problems.
```

### 3.6 Production Implementations

```
IMPLEMENTATION LANDSCAPE:

  ┌──────────────────┬──────────────────────────────────────────────────┐
  │  Library         │  Notes                                           │
  ├──────────────────┼──────────────────────────────────────────────────┤
  │  Resilience4j    │  Java. Modern successor to Hystrix. Functional  │
  │  (Java)          │  composition. Supports circuit breaker, retry,   │
  │                  │  bulkhead, rate limiter, time limiter. The       │
  │                  │  current standard for JVM services.              │
  ├──────────────────┼──────────────────────────────────────────────────┤
  │  Hystrix         │  Java. Netflix. DEPRECATED since 2018. Do not   │
  │  (Java)          │  use in new projects. Deprecated because the    │
  │                  │  thread-pool-per-dependency model doesn't scale  │
  │                  │  to hundreds of dependencies, and the reactive   │
  │                  │  paradigm (Resilience4j, Envoy) is preferred.    │
  ├──────────────────┼──────────────────────────────────────────────────┤
  │  Polly           │  .NET. Policy-based resilience. Supports all    │
  │  (.NET)          │  patterns including advanced pipeline chaining.  │
  │                  │  v8+ integrates with .NET DI natively.           │
  ├──────────────────┼──────────────────────────────────────────────────┤
  │  gRPC built-in   │  gRPC service config supports declarative retry │
  │  retry policies  │  policies with maxAttempts, retryableStatusCodes │
  │                  │  backoffMultiplier, and hedging policies. No     │
  │                  │  circuit breaker — combine with service mesh.    │
  ├──────────────────┼──────────────────────────────────────────────────┤
  │  Envoy / Istio   │  Service mesh sidecar. Outlier detection (a     │
  │  Outlier         │  form of per-endpoint circuit breaking), retry   │
  │  Detection       │  policies, and timeout configuration at the     │
  │                  │  infrastructure level. No code changes needed.   │
  ├──────────────────┼──────────────────────────────────────────────────┤
  │  Go:             │  sony/gobreaker (circuit breaker),              │
  │  Community       │  cenkalti/backoff (retry with backoff),         │
  │                  │  hashicorp/go-retryablehttp (HTTP retry client). │
  └──────────────────┴──────────────────────────────────────────────────┘
```

---

## 4. Bulkhead Pattern

### The Simple Explanation

A bulkhead is the "don't put all your eggs in one basket" pattern applied to server resources.

Imagine you live in an apartment building with one shared water pipe for all 20 apartments. If apartment 5 has a burst pipe, the water pressure drops for everyone — apartments that have no problem at all suddenly can't take a shower. Now imagine each apartment has its own isolated water supply. Apartment 5's burst pipe is apartment 5's problem. Everyone else is fine.

That's a bulkhead. Instead of all your dependencies sharing one thread pool / connection pool / resource pool, you give each dependency its own isolated pool. When the payments service slows down and hogs all its allocated threads, the user service and the search service keep running perfectly because they have their own threads that payments can't touch.

**Bulkhead vs. Rate Limiter — The Most Commonly Confused Pair**

These two patterns look similar (both "limit stuff") but solve completely different problems:

```
BULKHEAD vs. RATE LIMITER — SIDE BY SIDE:

  ┌─────────────────────────┬──────────────────────────────────────────┐
  │  BULKHEAD               │  RATE LIMITER                            │
  ├─────────────────────────┼──────────────────────────────────────────┤
  │  WHAT it limits:        │  WHAT it limits:                         │
  │  Concurrent requests    │  Request RATE (requests per second)      │
  │  (how many at once)     │  (how many over time)                    │
  ├─────────────────────────┼──────────────────────────────────────────┤
  │  WHO it protects:       │  WHO it protects:                        │
  │  The CALLER (yourself)  │  The CALLEE (the downstream service)     │
  │  from a slow dependency │  from being overwhelmed by callers       │
  │  consuming all your     │                                          │
  │  resources              │                                          │
  ├─────────────────────────┼──────────────────────────────────────────┤
  │  WHERE it lives:        │  WHERE it lives:                         │
  │  Client-side (in the    │  Server-side (or gateway/proxy, the      │
  │  service making calls)  │  service receiving calls)                │
  ├─────────────────────────┼──────────────────────────────────────────┤
  │  WHEN it activates:     │  WHEN it activates:                      │
  │  When concurrency hits  │  When request rate exceeds the limit,    │
  │  the cap (pool full),   │  regardless of concurrency               │
  │  regardless of rate     │                                          │
  ├─────────────────────────┼──────────────────────────────────────────┤
  │  Analogy:               │  Analogy:                                │
  │  A hotel has 80 rooms.  │  A nightclub lets in 10 people per       │
  │  When they're full,     │  minute. Even if the club is half empty, │
  │  "no vacancy" — no      │  you wait in line if 10 already entered  │
  │  matter how fast people │  this minute. Even if you're a VIP.      │
  │  are checking out.      │                                          │
  ├─────────────────────────┼──────────────────────────────────────────┤
  │  Math:                  │  Math:                                   │
  │  concurrency =          │  rate = requests / time_window           │
  │  requests_in_flight     │                                          │
  │  (Little's Law:         │  Token bucket: refill at R tokens/sec,   │
  │  L = λ × W, where      │  bucket capacity B.                      │
  │  L = concurrency,       │  Each request costs 1 token.             │
  │  λ = arrival rate,      │  Sustained rate ≤ R.                     │
  │  W = avg response time) │  Burst up to B.                          │
  │                         │                                          │
  │  If λ=100rps, W=50ms:  │  If R=100/sec, B=150:                    │
  │  L = 100 × 0.05 = 5    │  Sustains 100 rps.                       │
  │  concurrent requests    │  Allows burst of 150 at once.            │
  │  (normal)               │                                          │
  │                         │                                          │
  │  If W degrades to 2s:   │  If traffic spikes to 500 rps:           │
  │  L = 100 × 2 = 200     │  150 burst served, rest rejected at      │
  │  concurrent requests    │  rate exceeding 100/sec.                 │
  │  (pool overflow →       │                                          │
  │  bulkhead protects)     │                                          │
  └─────────────────────────┴──────────────────────────────────────────┘

  THE PRACTICAL DIFFERENCE IN ONE SENTENCE:
  ─────────────────────────────────────────────────────────────────────
  A rate limiter says: "You can only send 100 requests per second."
  A bulkhead says: "You can only have 10 requests in-flight to me
  at the same time."

  A service with 1ms response time can handle 10,000 rps through a
  bulkhead of 10 threads (each thread serves 1000 req/sec).

  The same service with a 2-second response time can only handle
  5 rps through the same 10-thread bulkhead (each thread is busy
  for 2 seconds).

  The bulkhead didn't change — the slow dependency filled it up.
  That's the point: the bulkhead limits the DAMAGE of slowness,
  not the rate of traffic.

  YOU OFTEN NEED BOTH:
  ─────────────────────────────────────────────────────────────────────
  Rate limiter (server-side): protects the downstream from too many
  requests per second from all callers.

  Bulkhead (client-side): protects the caller from one slow downstream
  consuming all threads/connections.

  They're complementary, not alternatives.
```

### 4.1 Origin and Principle

The bulkhead pattern borrows from ship hull design. Ships are divided into watertight compartments so that a breach in one compartment does not flood the entire vessel. In software, a bulkhead isolates failures so that one failing dependency cannot consume all resources and bring down unrelated functionality.

### 4.2 Why Bulkheads Are Needed

Without bulkheads, all dependencies share a single resource pool (thread pool, connection pool, etc.). When one dependency slows down, it consumes an outsized share of the pool, starving all other dependencies.

```
THE PROBLEM: SHARED THREAD POOL EXHAUSTION

  Service B has a single thread pool of 200 threads.
  It calls three dependencies: C (fast), D (fast), E (slow today).

  Normal state:
    C: 50 threads active (avg 10ms response) → low occupancy
    D: 50 threads active (avg 15ms response) → low occupancy
    E: 50 threads active (avg 20ms response) → low occupancy
    Free: 50 threads

  E becomes slow (2 second response time):
    C: 50 threads active → 50 threads (unchanged)
    D: 50 threads active → 50 threads (unchanged)
    E: threads accumulate... 50 → 100 → 150 → 200 threads
    Free: 0 threads

    ALL incoming requests block, including requests to C and D, which
    are working perfectly. Requests to C and D, which would succeed in
    10-15ms, are queued behind 200 threads waiting 2 seconds for E.

  WITH BULKHEAD ISOLATION:

  ┌─────────────────────────────────────────────────────────────────────┐
  │  Service B Thread Pools (isolated per dependency)                  │
  │                                                                     │
  │  ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐   │
  │  │  Pool for C      │ │  Pool for D      │ │  Pool for E      │   │
  │  │  (80 threads)    │ │  (80 threads)    │ │  (80 threads)    │   │
  │  │  Active: 50      │ │  Active: 50      │ │  Active: 80 FULL│   │
  │  │  Free: 30        │ │  Free: 30        │ │  Free: 0         │   │
  │  └──────────────────┘ └──────────────────┘ └──────────────────┘   │
  │                                                                     │
  │  E is slow → Pool E is full → requests to E fail fast.             │
  │  Pools C and D are unaffected. Requests to C and D continue at     │
  │  full speed.                                                        │
  └─────────────────────────────────────────────────────────────────────┘
```

### 4.3 Isolation Mechanisms

```
BULKHEAD ISOLATION TYPES:

  ┌───────────────────┬─────────────────────────────────────────────────┐
  │  Mechanism        │  Details                                        │
  ├───────────────────┼─────────────────────────────────────────────────┤
  │  Thread Pool      │  Separate OS/virtual thread pool per dependency.│
  │  Isolation        │  Requests to each dependency run on their own   │
  │                   │  pool. When the pool is full, new requests fail │
  │                   │  immediately (or are queued up to a limit).     │
  │                   │                                                 │
  │                   │  Pros: Strong isolation. Timeout enforcement.   │
  │                   │        Can cancel stuck threads.                │
  │                   │  Cons: Thread overhead (stack memory, context   │
  │                   │        switches). Doesn't scale to hundreds    │
  │                   │        of dependencies.                         │
  ├───────────────────┼─────────────────────────────────────────────────┤
  │  Semaphore        │  A counting semaphore limiting concurrent calls │
  │  Isolation        │  to each dependency. No separate thread pool —  │
  │                   │  requests execute on the caller's thread.       │
  │                   │                                                 │
  │                   │  Pros: Lower overhead. Scales to many deps.    │
  │                   │  Cons: Cannot timeout stuck calls (the calling │
  │                   │        thread is blocked). Requires the caller │
  │                   │        to handle timeouts independently.        │
  ├───────────────────┼─────────────────────────────────────────────────┤
  │  Connection Pool  │  Separate HTTP/gRPC connection pools per        │
  │  Isolation        │  dependency. Limits how many TCP connections    │
  │                   │  can be consumed by any single downstream.      │
  │                   │                                                 │
  │                   │  Pros: Natural fit for HTTP clients. Built into │
  │                   │        most HTTP libraries (max_connections_    │
  │                   │        per_host). Low overhead.                 │
  │                   │  Cons: Limits concurrency, not execution time. │
  │                   │        A slow dependency still blocks its       │
  │                   │        connections for the full timeout.         │
  ├───────────────────┼─────────────────────────────────────────────────┤
  │  Process-Level    │  Run each dependency client in a separate       │
  │  Isolation        │  process. Complete memory and resource           │
  │                   │  isolation. The sidecar proxy pattern (Envoy).  │
  │                   │                                                 │
  │                   │  Pros: Strongest isolation. A crash in one      │
  │                   │        dependency client cannot corrupt another. │
  │                   │  Cons: IPC overhead. Operational complexity.    │
  ├───────────────────┼─────────────────────────────────────────────────┤
  │  Infrastructure   │  Kubernetes resource limits (CPU, memory),      │
  │  (K8s/cgroups)    │  cgroups v2 resource constraints. Isolate at   │
  │                   │  the container or pod level.                    │
  │                   │                                                 │
  │                   │  Pros: Enforced by the kernel. Cannot be        │
  │                   │        bypassed by application bugs.            │
  │                   │  Cons: Coarse-grained. Limits are per-container │
  │                   │        not per-dependency within a container.   │
  └───────────────────┴─────────────────────────────────────────────────┘
```

### 4.4 Sizing Bulkheads

```
SIZING GUIDELINES:

  Too small: artificial bottleneck. The dependency is healthy but
  requests are rejected because the bulkhead is too narrow.

  Too large: no isolation. The bulkhead allows enough concurrency
  to consume the entire shared resource anyway.

  FORMULA FOR THREAD POOL SIZING:

    pool_size = target_rps * average_latency_seconds * safety_margin

    Example: dependency at 100 rps with 50ms average latency:
      pool_size = 100 * 0.05 * 2.0 = 10 threads (with 2x safety)

    Example: dependency at 100 rps that degrades to 2s latency:
      If the pool is sized for normal conditions: 10 threads.
      At 2s latency: pool fills in 10 / 100 = 0.1 seconds.
      Remaining requests fail fast — which is exactly the point.

  THE KEY INSIGHT:
    Size the bulkhead for NORMAL conditions, not degraded conditions.
    The purpose of the bulkhead is to limit the blast radius during
    degradation, not to absorb degraded traffic.
```

---

## 5. Timeout Patterns

### The Simple Explanation

A timeout is the "I'm not waiting forever" pattern. It's the simplest resilience pattern and the only one that is truly non-negotiable — every single network call must have one.

Imagine you order food at a restaurant. If no food arrives after 45 minutes, you leave. That's a timeout. Without it, you sit there indefinitely — maybe the kitchen lost your order, maybe the chef quit, maybe the building is on fire. You don't know and you don't care. You have better things to do than wait forever.

In software, a thread waiting for a response is a thread that can't serve anyone else. Without a timeout, that thread is stuck forever. Multiply by hundreds of requests and your entire service freezes — not because something crashed, but because everything is patiently waiting for a response that will never come.

**Timeout vs. circuit breaker**: A timeout protects a single request ("I won't wait more than 3 seconds for you"). A circuit breaker protects against a pattern of failures ("you've been timing out all day, I'm done calling you"). Timeouts are the input that feeds the circuit breaker — repeated timeouts are what cause the breaker to trip.

**The math of why timeouts matter**:

```
TIMEOUT MATH — THREAD POOL EXHAUSTION:

  Your server has 200 threads and receives 500 requests/second.

  Normal case (responses in 50ms):
    Threads in use = 500 rps × 0.05s = 25 threads
    175 threads idle. Everything is fine.

  Dependency hangs (no timeout set):
    Threads accumulate: 500 new threads needed per second.
    After 0.4 seconds: all 200 threads are blocked.
    Your service is dead. No crash, no error — just frozen.

  Dependency hangs (timeout = 3 seconds):
    Threads in use = 500 rps × 3s = 1,500 threads needed.
    You only have 200. Pool exhausts in 200/500 = 0.4 seconds.
    Still dead — but at least threads get released after 3s
    instead of never. Combine with a bulkhead to survive.

  Dependency hangs (timeout = 500ms, with bulkhead of 50 threads):
    Only 50 threads can be consumed by the slow dependency.
    Other 150 threads serve other work normally.
    Each stuck thread freed after 500ms.
    Throughput to slow dependency: 50 / 0.5s = 100 rps (degraded
    but alive). Everything else: unaffected.

  THE LESSON: timeout × arrival_rate = threads consumed.
  This is Little's Law: L = λ × W
    L = concurrent requests (threads in use)
    λ = arrival rate (requests per second)
    W = average wait time (which timeout caps)
```

### 5.1 The Three Timeouts

Most engineers configure "a timeout" without recognizing that there are three distinct timeouts, each serving a different purpose.

```
THREE TIMEOUT TYPES:

  ┌───────────────┬──────────────────────────────────────────────────────┐
  │  Timeout      │  What It Bounds                                     │
  ├───────────────┼──────────────────────────────────────────────────────┤
  │  Connect      │  Time to establish a TCP connection (SYN → SYN-ACK).│
  │  Timeout      │  Bounds: network latency, server backlog queue.     │
  │               │  Typical: 1-5 seconds.                              │
  │               │  If this fires, the server never saw your request.  │
  ├───────────────┼──────────────────────────────────────────────────────┤
  │  Read         │  Time to receive the first byte (or complete        │
  │  Timeout      │  response) after the request was sent.              │
  │               │  Bounds: server processing time + response transfer.│
  │               │  Typical: depends on operation (100ms to 30s).      │
  │               │  If this fires, the server MAY have processed the   │
  │               │  request. You don't know.                           │
  ├───────────────┼──────────────────────────────────────────────────────┤
  │  Write        │  Time to send the request body to the server.       │
  │  Timeout      │  Bounds: client-side network throughput + send       │
  │               │  buffer space.                                       │
  │               │  Typical: 5-30 seconds for large payloads.          │
  │               │  Rarely configured separately, but matters for      │
  │               │  large uploads or slow networks.                    │
  └───────────────┴──────────────────────────────────────────────────────┘

  THE DANGER OF NO TIMEOUT:
  ─────────────────────────────────────────────────────────────────────
  A missing timeout means a thread can block FOREVER. In production,
  "forever" is until the thread pool is exhausted, or until the
  container is OOM-killed 4 hours later, or until an operator
  notices and restarts the service manually.

  Every outgoing network call MUST have a timeout. No exceptions.

  THE DANGER OF TOO-SHORT TIMEOUT:
  ─────────────────────────────────────────────────────────────────────
  Setting read_timeout=100ms on a dependency whose p99 is 80ms means
  1% of requests timeout under normal conditions, even when the
  dependency is perfectly healthy. This creates:
    - Unnecessary retries (increasing downstream load)
    - Unnecessary circuit breaker trips
    - Spurious error rate that consumes error budget

  Rule of thumb: set timeouts at 2-3x the p99 latency of the
  dependency. Monitor and adjust based on observed latency distribution.
```

### 5.2 Deadline Propagation

In a microservice chain, each service subtracts its own processing time from the remaining deadline before forwarding the request. Without deadline propagation, each service applies its own independent timeout, which can result in total end-to-end latency far exceeding the user's tolerance.

```
DEADLINE PROPAGATION:

  User → Gateway → Service A → Service B → Service C
  Total deadline: 500ms

  WITHOUT deadline propagation:
    Gateway timeout to A: 500ms
    A's timeout to B: 500ms
    B's timeout to C: 500ms
    Worst case: 500 + 500 + 500 = 1,500ms end-to-end.
    The user gave up after 500ms. The remaining 1,000ms is wasted work.

  WITH deadline propagation:
    Gateway sends deadline to A: "you have 500ms"
    A processes for 50ms, forwards to B: "you have 450ms"
    B processes for 30ms, forwards to C: "you have 420ms"
    C processes for 100ms, responds to B: used 100ms of 420ms
    Total: 50 + 30 + 100 + transit = well under 500ms

    If C is slow and uses 420ms, B gets the response with 0ms left
    on the deadline. B should NOT process the response — it's already
    too late. B returns DEADLINE_EXCEEDED upstream.

  gRPC DEADLINE PROPAGATION:
    gRPC propagates deadlines automatically via the grpc-timeout header.
    Every service in the chain sees the remaining time. Libraries will
    automatically cancel the RPC when the deadline is reached, both
    on the client side and (with proper server interceptors) on the
    server side.

  HTTP DEADLINE PROPAGATION:
    No standard mechanism. Common approaches:
    - X-Request-Deadline header (Unix timestamp in milliseconds)
    - X-Request-Timeout header (remaining milliseconds)
    Both require manual implementation in middleware.
```

### 5.3 Adaptive Timeouts

Static timeouts become stale as the system evolves. Adaptive timeouts adjust dynamically based on observed latency.

```
ADAPTIVE TIMEOUT APPROACH:

  Continuously track the p99 (or p999) latency of each dependency.
  Set timeout = observed_p99 * multiplier (e.g., 2x).

  Implementation:
  - Maintain a sliding window of response times (e.g., HdrHistogram).
  - Every N seconds, recalculate p99 and update the timeout.
  - Apply a floor (minimum timeout, e.g., 100ms) and a ceiling
    (maximum timeout, e.g., 30s) to prevent runaway values.

  RISKS:
  - If the dependency gradually slows down over weeks, the adaptive
    timeout gradually increases, masking the degradation.
  - If the dependency has bimodal latency (fast for reads, slow for
    writes), a single adaptive timeout is wrong for one mode.
  - A latency spike in the observation window can temporarily set
    the timeout too high, delaying failure detection.

  MITIGATION: combine adaptive timeouts with alerting on timeout
  value changes. If the adaptive timeout drifts above a static
  threshold, investigate.
```

---

## 6. Combining Patterns -- The Full Defense Stack

### The Simple Explanation

Each resilience pattern solves one specific problem. Using them individually is like wearing only a helmet on a motorcycle — it protects your head, but the rest of you is exposed. The full defense stack combines all patterns into layered protection where each pattern covers the gaps the others leave.

Think of it like airport security. There's a specific order and each layer serves a purpose:

1. **Timeout** (the boarding gate deadline) — "The flight leaves at 3pm. Nothing after this point matters." Sets the absolute outer boundary.
2. **Bulkhead** (separate security lanes) — "Business class and economy have separate lines so one slow lane doesn't block the other." Isolates resources per dependency.
3. **Circuit breaker** (the security alert system) — "Terminal B is shut down due to a threat. Don't send anyone there." Stops sending requests to a known-broken dependency.
4. **Retry** (the re-check) — "Your bag triggered the scanner. Run it through once more." Handles transient one-off failures.
5. **Fallback** (the backup plan) — "Your flight is cancelled. Here's a hotel voucher." Provides a degraded-but-functional response when everything else fails.

The order is not arbitrary. Retries must happen inside the circuit breaker (so the breaker sees accurate health data). The timeout must wrap everything (so nothing runs past the user's patience). The bulkhead must be outside the circuit breaker (so even checking the breaker doesn't consume unbounded resources).

### 6.1 The Correct Layering Order

The resilience patterns must be composed in a specific order. Getting the order wrong creates subtle failure modes.

```
CORRECT LAYERING (outermost to innermost):

  ┌─────────────────────────────────────────────────────────────────────┐
  │  INCOMING REQUEST                                                   │
  │  │                                                                  │
  │  ▼                                                                  │
  │  ┌─────────────┐                                                    │
  │  │  TIMEOUT     │  Outermost. Sets the absolute deadline for the   │
  │  │  (Deadline)  │  entire operation. If everything inside takes     │
  │  │             │  too long, abort.                                  │
  │  └──────┬──────┘                                                    │
  │         ▼                                                           │
  │  ┌─────────────┐                                                    │
  │  │  BULKHEAD   │  Limits concurrency to this dependency. If the    │
  │  │  (Semaphore │  bulkhead is full, fail fast without consuming     │
  │  │  or Pool)   │  further resources.                                │
  │  └──────┬──────┘                                                    │
  │         ▼                                                           │
  │  ┌──────────────┐                                                   │
  │  │  CIRCUIT     │  Checks if the circuit is OPEN. If OPEN, go      │
  │  │  BREAKER     │  directly to FALLBACK without attempting the      │
  │  │              │  call. If CLOSED/HALF-OPEN, proceed.              │
  │  └──────┬───────┘                                                   │
  │         ▼                                                           │
  │  ┌─────────────┐                                                    │
  │  │  RETRY       │  If the call fails and the error is retryable,   │
  │  │  (with       │  retry with backoff + jitter, subject to retry   │
  │  │  backoff +   │  budget and remaining deadline.                   │
  │  │  jitter)     │                                                   │
  │  └──────┬───────┘                                                   │
  │         ▼                                                           │
  │  ┌─────────────┐                                                    │
  │  │  ACTUAL      │  The real network call to the dependency.         │
  │  │  CALL        │  (with per-call connect + read timeout)           │
  │  └──────┬───────┘                                                   │
  │         │                                                           │
  │         ▼                                                           │
  │  SUCCESS or FAILURE                                                 │
  │         │                                                           │
  │         ▼ (on final failure)                                        │
  │  ┌─────────────┐                                                    │
  │  │  FALLBACK   │  Return cached data, degraded response, or a      │
  │  │             │  meaningful error.                                  │
  │  └─────────────┘                                                    │
  │                                                                     │
  └─────────────────────────────────────────────────────────────────────┘
```

### 6.2 Why the Order Matters

```
ANTI-PATTERN: Retries OUTSIDE the circuit breaker

  ┌─────────────────┐
  │  RETRY (3x)     │
  │  └── CIRCUIT    │
  │      BREAKER    │
  │      └── CALL   │
  └─────────────────┘

  Problem: the circuit breaker opens after the first failed attempt.
  The retry policy retries 2 more times. Both retries hit the OPEN
  circuit breaker and fail immediately. The circuit breaker recorded
  3 failures (the original + the retries hitting the open breaker).
  The failure count is inflated by retries, making the breaker think
  the downstream is worse than it actually is.

  CORRECT: Retries INSIDE the circuit breaker

  ┌─────────────────┐
  │  CIRCUIT         │
  │  BREAKER         │
  │  └── RETRY (3x) │
  │      └── CALL   │
  └─────────────────┘

  The circuit breaker sees the outcome of the retry sequence as one
  logical call: either eventually-succeeded or finally-failed. The
  failure count reflects actual dependency health, not retry noise.

ANTI-PATTERN: Retries without a timeout

  Without a total timeout, retries with exponential backoff can run
  indefinitely: 100ms + 200ms + 400ms + 800ms + ... = the thread
  is blocked for minutes, consuming resources for a request the user
  abandoned seconds ago.

  ALWAYS wrap retries in a deadline. Abort the retry sequence when
  the deadline is reached, regardless of remaining retry attempts.

ANTI-PATTERN: Circuit breaker without monitoring

  A circuit breaker with no alerting on state changes is a silent
  failure absorber. The system appears healthy (no errors reaching
  users thanks to fallbacks) while the dependency is down and the
  fallback is serving increasingly stale data. By the time someone
  notices, the downstream problem may have cascaded in other ways.
```

### 6.3 Full Request Flow

```
FULL DEFENSE STACK — REQUEST FLOW:

  t=0ms   Request arrives at Service B for operation requiring C.

  t=0ms   TIMEOUT layer starts a 2000ms deadline timer.

  t=0ms   BULKHEAD check: 45/80 threads active for C. Semaphore acquired.

  t=0ms   CIRCUIT BREAKER check: state=CLOSED. Proceed.

  t=1ms   RETRY layer: attempt 1.
          Send request to C with connect_timeout=1s, read_timeout=500ms.

  t=480ms C responds with HTTP 503.
          RETRY layer classifies 503 as retryable.
          Check retry budget: 8/100 tokens used. Budget available.
          Check remaining deadline: 2000 - 480 = 1520ms remaining.
          Calculate backoff: min(100 * 2^0, 30000) = 100ms.
          Apply full jitter: random(0, 100) = 67ms.
          Sleep 67ms.

  t=547ms RETRY layer: attempt 2.
          Send request to C.

  t=590ms C responds with HTTP 200. Success.

  t=590ms CIRCUIT BREAKER records success.
          BULKHEAD releases semaphore (46/80 → 45/80).
          TIMEOUT layer cancelled (1410ms remaining, unused).

  t=590ms Response returned to caller.

  ─────────────────────────────────────────────────────────────────

  ALTERNATIVE FLOW — all retries fail:

  t=0ms   Same setup. Deadline=2000ms. Bulkhead acquired.
          Circuit breaker: CLOSED.

  t=1ms   Attempt 1 → 503 after 450ms.
  t=518ms Attempt 2 (after 67ms jitter) → 503 after 400ms.
  t=1050ms Attempt 3 (after 132ms jitter) → timeout after 500ms.
  t=1550ms All retries exhausted. Remaining deadline: 450ms.

  t=1550ms CIRCUIT BREAKER records failure.
           Failure count: 47/100 in window. Threshold 50%. Not tripped.
           BULKHEAD releases semaphore.

  t=1550ms FALLBACK: return cached data from local store.
           Response header: X-Fallback: true, X-Data-Age: 300s.

  t=1551ms Response returned to caller with degraded data.
```

### 6.4 Pattern Comparison — When to Use What

The patterns are easily confused because they all "protect against failures." This section clarifies exactly what each one does, what it does NOT do, and when to reach for it.

```
EVERY PATTERN IN ONE SENTENCE:

  Timeout:         "I won't wait forever for you."
  Retry:           "That failed, let me try once more."
  Circuit Breaker: "You've been failing too much, I'll stop asking."
  Bulkhead:        "Your slowness won't consume all my resources."
  Rate Limiter:    "I'll only accept N requests per second."
  Fallback:        "You failed, here's a plan B."
  Hedged Request:  "I'll ask two servers; first answer wins."
  Deadline:        "The user is waiting 500ms total, pass that clock
                    down the chain."
```

```
WHICH PATTERN SOLVES WHICH PROBLEM?

  ┌──────────────────────────────┬─────┬───────┬─────────┬────────┬──────┬────────┬───────┬────────┐
  │  Problem                     │ TO  │ Retry │ Circuit │ Bulk-  │ Rate │ Fall-  │ Hedge │ Dead-  │
  │                              │     │       │ Breaker │ head   │ Limit│ back   │       │ line   │
  ├──────────────────────────────┼─────┼───────┼─────────┼────────┼──────┼────────┼───────┼────────┤
  │ Dependency hangs forever     │ ✓   │       │         │        │      │        │       │        │
  │ Transient one-off failure    │     │ ✓     │         │        │      │        │       │        │
  │ Dependency down for minutes  │     │       │ ✓       │        │      │        │       │        │
  │ Slow dep exhausts threads    │     │       │         │ ✓      │      │        │       │        │
  │ Too many callers overwhelm   │     │       │         │        │ ✓    │        │       │        │
  │ Need a degraded response     │     │       │         │        │      │ ✓      │       │        │
  │ Tail latency (p99 spikes)    │     │       │         │        │      │        │ ✓     │        │
  │ Wasted work past user's wait │     │       │         │        │      │        │       │ ✓      │
  └──────────────────────────────┴─────┴───────┴─────────┴────────┴──────┴────────┴───────┴────────┘

  TO = Timeout

  NOTICE: each pattern targets exactly one failure mode. No single
  pattern covers everything. That's why they compose into a stack.
```

```
THE MOST COMMONLY CONFUSED PAIRS:

  ┌────────────────────────────────────────────────────────────────────┐
  │  CIRCUIT BREAKER vs. RATE LIMITER                                 │
  ├────────────────────────────────────────────────────────────────────┤
  │                                                                    │
  │  Both "block requests." Different reasons, different triggers.    │
  │                                                                    │
  │  Circuit breaker triggers on: FAILURE RATE of the downstream.     │
  │  Rate limiter triggers on: REQUEST RATE of the upstream.          │
  │                                                                    │
  │  Circuit breaker: "Backend is sick → stop calling it."            │
  │  Rate limiter: "Too many callers → reject excess."                │
  │                                                                    │
  │  A circuit breaker can be CLOSED (allowing traffic) while the     │
  │  rate limiter is rejecting requests (too much traffic).            │
  │  A circuit breaker can be OPEN (blocking traffic) while the       │
  │  rate limiter would happily allow it (traffic is within limits).   │
  │                                                                    │
  │  They solve opposite problems:                                    │
  │  CB: "the server can't handle ANY load right now"                 │
  │  RL: "the server can handle SOME load, just not THIS much"       │
  └────────────────────────────────────────────────────────────────────┘

  ┌────────────────────────────────────────────────────────────────────┐
  │  BULKHEAD vs. CIRCUIT BREAKER                                     │
  ├────────────────────────────────────────────────────────────────────┤
  │                                                                    │
  │  Both "prevent a bad dependency from killing you." Different how. │
  │                                                                    │
  │  Bulkhead: limits HOW MANY RESOURCES the dependency can consume.  │
  │  Circuit breaker: limits WHETHER requests go to the dependency.   │
  │                                                                    │
  │  Bulkhead with 50 threads: "You can use 50 of my threads.        │
  │  If all 50 are busy, new requests to you fail fast.               │
  │  But I'll keep trying — maybe some will succeed."                 │
  │                                                                    │
  │  Circuit breaker: "You've failed 50% of the time.                 │
  │  I'm not sending ANY requests for the next 30 seconds.            │
  │  Not even one."                                                    │
  │                                                                    │
  │  A slow dependency (2s response, not failing) will fill the       │
  │  bulkhead but NOT trip the circuit breaker. The bulkhead limits   │
  │  concurrency; the breaker needs actual failures.                   │
  │                                                                    │
  │  You want both: the bulkhead contains the blast radius while      │
  │  the breaker decides whether to bother at all.                    │
  └────────────────────────────────────────────────────────────────────┘

  ┌────────────────────────────────────────────────────────────────────┐
  │  TIMEOUT vs. DEADLINE PROPAGATION                                 │
  ├────────────────────────────────────────────────────────────────────┤
  │                                                                    │
  │  A timeout is LOCAL: "I'll wait 500ms for Service B."             │
  │  A deadline is GLOBAL: "The user is waiting 500ms total.          │
  │  Service A used 50ms. Service B, you have 450ms left.             │
  │  Service C, after B uses 30ms, you have 420ms left."              │
  │                                                                    │
  │  Without deadline propagation, each service sets its own          │
  │  independent timeout. Worst case: 500 + 500 + 500 = 1,500ms.     │
  │  The user left after 500ms. The remaining 1,000ms of work is      │
  │  pure waste — the response has nowhere to go.                     │
  │                                                                    │
  │  Deadlines prevent wasted work across the entire call chain.      │
  │  Timeouts prevent wasted work on a single hop.                    │
  └────────────────────────────────────────────────────────────────────┘

  ┌────────────────────────────────────────────────────────────────────┐
  │  RETRY vs. HEDGED REQUEST                                         │
  ├────────────────────────────────────────────────────────────────────┤
  │                                                                    │
  │  Retry: "That failed. Wait, then try the same thing again."       │
  │  Hedge: "That's taking too long. Try a DIFFERENT server NOW."     │
  │                                                                    │
  │  Retry is sequential: fail → wait → try again → wait → try again. │
  │  Hedge is parallel: send request A, then after p95 latency, send │
  │  the same request to server B. First response wins, cancel other. │
  │                                                                    │
  │  Retry adds latency (the backoff delays).                         │
  │  Hedge adds cost (you're running 2 requests in parallel).         │
  │                                                                    │
  │  Retry helps when the failure is transient (network blip).        │
  │  Hedge helps when the slowness is per-server (GC pause, hot       │
  │  shard). If all servers share a bottleneck, hedging doubles the   │
  │  load on that bottleneck.                                          │
  └────────────────────────────────────────────────────────────────────┘
```

```
LITTLE'S LAW — THE MATH BEHIND IT ALL:

  Almost every resilience pattern's behavior can be predicted using
  one equation from queueing theory:

    L = λ × W

    L = number of requests in the system (concurrency / threads in use)
    λ = arrival rate (requests per second)
    W = average time each request spends in the system (latency)

  THIS ONE EQUATION TELLS YOU:

  1. WHY SLOW IS WORSE THAN DOWN:
     If a dependency goes DOWN (instant 503): W = 1ms.
     L = 1000 rps × 0.001s = 1 thread. Negligible.

     If a dependency goes SLOW (hangs for 10s): W = 10s.
     L = 1000 rps × 10s = 10,000 threads needed. You have 200. Dead.

     Down services release resources instantly. Slow services hold them.

  2. HOW TO SIZE A BULKHEAD:
     pool_size ≥ λ × W_normal × safety_margin
     = 100 rps × 0.05s × 2 = 10 threads

     When W degrades to 2s:
     threads_needed = 100 × 2 = 200 (but bulkhead caps at 10)
     Excess requests fail fast. That's the protection.

  3. HOW TO SET TIMEOUTS:
     You want L (concurrency) to stay below your thread pool size.
     L = λ × W, so W_max = pool_size / λ
     = 200 threads / 1000 rps = 200ms

     Any timeout above 200ms risks pool exhaustion at 1000 rps.
     This is why timeout = 2-3x p99 is a rule of thumb, not a law.
     The real constraint is: timeout × rps < available_threads.

  4. WHY CIRCUIT BREAKERS HELP RECOVERY:
     When the breaker opens: λ to the dependency drops to 0.
     L = 0 × W = 0. The dependency has zero load.
     It can drain its queues, close stuck connections, recover.

     When the breaker allows probes: λ = a few requests.
     L = a_few × W. Manageable. If W returns to normal, close breaker.
```

---

## 7. Testing Resilience Patterns

### 7.1 Chaos Engineering

Resilience patterns that have never been tested under real failure conditions are resilience theater. They provide a false sense of security. Chaos engineering deliberately injects failures to verify that patterns work as intended.

```
TESTING CHECKLIST FOR RESILIENCE PATTERNS:

  CIRCUIT BREAKER:
  ┌─────────────────────────────────────────────────────────────────────┐
  │  [ ] Inject failures at exactly the threshold rate. Verify the     │
  │      breaker trips. Verify it does NOT trip at threshold - 1%.     │
  │  [ ] Verify HALF-OPEN probing sends exactly the configured number  │
  │      of probes (not more).                                         │
  │  [ ] Verify the breaker closes after probes succeed.               │
  │  [ ] Verify the fallback activates when the breaker is OPEN.       │
  │  [ ] Verify alerts fire on state transitions.                      │
  │  [ ] Inject failures across multiple instances simultaneously.     │
  │      Verify no thundering herd on HALF-OPEN recovery.              │
  │  [ ] Measure the time between dependency recovery and breaker      │
  │      closing. This is the "recovery lag" — it should be bounded.   │
  └─────────────────────────────────────────────────────────────────────┘

  RETRIES:
  ┌─────────────────────────────────────────────────────────────────────┐
  │  [ ] Under sustained 100% failure, measure total retry load on     │
  │      downstream. Verify it does not exceed budget (e.g., +10%).    │
  │  [ ] Verify non-retryable errors (400, 403) are NOT retried.       │
  │  [ ] Verify idempotency keys prevent duplicate execution on retry. │
  │  [ ] Verify retry delay includes jitter (not synchronized bursts). │
  │  [ ] Verify retries stop when the deadline is exhausted, even if   │
  │      retry attempts remain.                                        │
  │  [ ] In multi-tier setup: verify total amplification under failure │
  │      matches expected R^N. If it exceeds expectations, retry       │
  │      budgets are not propagating correctly.                        │
  └─────────────────────────────────────────────────────────────────────┘

  BULKHEADS:
  ┌─────────────────────────────────────────────────────────────────────┐
  │  [ ] Slow one dependency to 10x normal latency. Verify other       │
  │      dependencies continue at normal latency and error rate.       │
  │  [ ] Fill the bulkhead to capacity. Verify new requests fail fast  │
  │      (not queued indefinitely).                                    │
  │  [ ] After the slow dependency recovers, verify the bulkhead       │
  │      drains and normal traffic resumes within seconds.             │
  └─────────────────────────────────────────────────────────────────────┘

  TIMEOUTS:
  ┌─────────────────────────────────────────────────────────────────────┐
  │  [ ] Inject latency at 2x the configured timeout. Verify the      │
  │      timeout fires and the thread is released.                     │
  │  [ ] Verify deadline propagation: with a 500ms total deadline      │
  │      and 3 services in the chain, verify the deepest service       │
  │      sees the correct remaining deadline (not the full 500ms).     │
  │  [ ] Verify that no code path has a missing timeout. Static        │
  │      analysis tools or integration tests with a test proxy         │
  │      (Toxiproxy) that adds 60s delay can surface missing timeouts. │
  └─────────────────────────────────────────────────────────────────────┘
```

### 7.2 Tools

```
CHAOS AND FAILURE INJECTION TOOLS:

  ┌──────────────────┬──────────────────────────────────────────────────┐
  │  Tool            │  Purpose                                         │
  ├──────────────────┼──────────────────────────────────────────────────┤
  │  Toxiproxy       │  TCP proxy that injects latency, bandwidth      │
  │  (Shopify)       │  limits, connection resets, and timeouts between │
  │                  │  services. Ideal for integration/load tests.     │
  ├──────────────────┼──────────────────────────────────────────────────┤
  │  Chaos Monkey    │  Netflix. Randomly terminates EC2 instances in   │
  │  (Netflix)       │  production to verify instance-level resilience. │
  ├──────────────────┼──────────────────────────────────────────────────┤
  │  Litmus          │  Kubernetes-native chaos engineering framework.  │
  │  (LitmusChaos)   │  Pod kill, network partition, disk fill, CPU    │
  │                  │  stress experiments with CRDs.                   │
  ├──────────────────┼──────────────────────────────────────────────────┤
  │  Chaos Mesh      │  Kubernetes-native. Supports network chaos       │
  │  (PingCAP)       │  (partition, delay, loss), I/O chaos, and time  │
  │                  │  skew injection.                                 │
  ├──────────────────┼──────────────────────────────────────────────────┤
  │  Gremlin         │  Commercial chaos engineering platform with a    │
  │                  │  managed control plane. Supports infrastructure  │
  │                  │  and application-level attacks.                   │
  └──────────────────┴──────────────────────────────────────────────────┘
```

### 7.3 Load Testing with Failure Injection

The most revealing test is a load test with simultaneous failure injection. Run the system at production-equivalent load, then inject a dependency failure and observe:

1. Does the error rate stay contained (only the failing dependency's calls fail, not everything)?
2. Does latency stay bounded (requests that don't touch the failing dependency are unaffected)?
3. Does the retry load stay within budget (downstream sees at most 10% additional traffic)?
4. Do circuit breakers trip at the expected threshold (not too early, not too late)?
5. Do fallbacks activate correctly (returning degraded but not incorrect data)?
6. After the failure is removed, does the system fully recover within a bounded time?

If any of these answers is "no," the resilience patterns have a bug. The most common bugs discovered through this testing: missing timeouts on one code path, retry budgets not shared across threads, circuit breaker thresholds set lower than the normal error rate, and fallbacks that throw their own exceptions (turning a dependency failure into a service crash).

---

## 8. Production Tradeoff Matrix

```
┌──────────────────┬───────────────────────┬─────────────────────┬──────────────────────────┬────────────────────────────┐
│  Pattern         │  Protects Against     │  Cost               │  Failure Modes of        │  When NOT to Use           │
│                  │                       │                     │  the Pattern Itself      │                            │
├──────────────────┼───────────────────────┼─────────────────────┼──────────────────────────┼────────────────────────────┤
│  Retry           │  Transient failures,  │  Increased load on  │  Retry storms amplify    │  Non-idempotent ops        │
│  (with backoff   │  brief network        │  downstream (up to  │  outages. Retries without│  without idempotency       │
│  + jitter)       │  glitches, single-    │  budget %). Latency │  jitter cause thundering │  keys. When downstream     │
│                  │  request failures     │  increase for        │  herds. Retrying non-    │  is persistently down      │
│                  │                       │  retried requests   │  retryable errors wastes │  (use circuit breaker).    │
│                  │                       │                     │  resources forever.      │                            │
├──────────────────┼───────────────────────┼─────────────────────┼──────────────────────────┼────────────────────────────┤
│  Circuit         │  Cascade failures,    │  Rejected requests  │  Trips too aggressively: │  Dependencies with no      │
│  Breaker         │  resource exhaustion  │  during OPEN state. │  blocks healthy traffic. │  fallback (if the call is  │
│                  │  from slow/failing    │  Complexity of       │  Synchronized breakers:  │  mandatory and cannot be   │
│                  │  dependencies         │  configuration and  │  thundering herd on      │  degraded). Fire-and-      │
│                  │                       │  monitoring.        │  recovery. Hides failures│  forget calls.             │
│                  │                       │                     │  if monitoring is absent.│                            │
├──────────────────┼───────────────────────┼─────────────────────┼──────────────────────────┼────────────────────────────┤
│  Bulkhead        │  Resource exhaustion  │  Reduced total      │  Undersized: artificial  │  Services with only one    │
│                  │  from one dependency  │  throughput (fixed   │  bottleneck under normal │  dependency. When the      │
│                  │  consuming all        │  resources divided  │  load. Oversized: no     │  service is I/O-bound,     │
│                  │  threads/connections  │  across pools).     │  actual isolation. Thread│  not thread-bound          │
│                  │                       │  Memory overhead    │  pool version has stack  │  (use async I/O instead).  │
│                  │                       │  for thread pools.  │  memory overhead per pool│                            │
├──────────────────┼───────────────────────┼─────────────────────┼──────────────────────────┼────────────────────────────┤
│  Timeout         │  Indefinite thread    │  False timeouts     │  Too short: premature    │  Never. Every network      │
│                  │  blocking, resource   │  under normal       │  failures on healthy     │  call MUST have a timeout. │
│                  │  exhaustion from      │  variance. Lost     │  services. Too long:     │  The question is not       │
│                  │  unresponsive         │  requests that were │  threads blocked nearly  │  whether, but what value.  │
│                  │  dependencies         │  "almost done"      │  as long as no timeout.  │                            │
│                  │                       │                     │  Missing on one path:    │                            │
│                  │                       │                     │  negates all protection. │                            │
├──────────────────┼───────────────────────┼─────────────────────┼──────────────────────────┼────────────────────────────┤
│  Fallback        │  User-visible errors  │  Stale/incomplete   │  Masks real failures if  │  Operations where partial/ │
│                  │  when dependencies    │  data served to     │  no alerting. Fallback   │  stale data is dangerous   │
│                  │  fail. Allows         │  users. Complexity  │  itself can fail (e.g.,  │  (financial transactions,  │
│                  │  graceful degradation │  of maintaining      │  cache is also down),    │  medication dosing,        │
│                  │                       │  fallback logic.    │  causing a cascading     │  safety-critical systems). │
│                  │                       │                     │  exception.              │                            │
├──────────────────┼───────────────────────┼─────────────────────┼──────────────────────────┼────────────────────────────┤
│  Hedged          │  Tail latency         │  Doubles (or more)  │  Doubles downstream load │  Non-idempotent ops.       │
│  Requests        │  (p99/p999). Single-  │  downstream load    │  if cancellation fails.  │  Downstream under load     │
│                  │  request slowness     │  in the worst case. │  Doubles cost in metered │  (hedging makes it worse). │
│                  │  from per-request     │  Must cancel losing │  environments (cloud     │  All replicas share a      │
│                  │  variance (GC, queue) │  requests promptly. │  egress, API rate caps). │  bottleneck (same DB).     │
├──────────────────┼───────────────────────┼─────────────────────┼──────────────────────────┼────────────────────────────┤
│  Deadline        │  Wasted work past     │  Complexity of      │  Clock skew between      │  Never skip in multi-hop   │
│  Propagation     │  the user's patience. │  propagating and    │  services causes early   │  architectures. Single-    │
│                  │  Downstream services  │  honoring deadlines │  or late deadline expiry.│  hop calls can use simple  │
│                  │  working on requests  │  across all hops.   │  Missing propagation on  │  timeouts instead.         │
│                  │  nobody is waiting    │  Requires           │  one hop voids the       │                            │
│                  │  for.                 │  middleware changes. │  protection entirely.    │                            │
└──────────────────┴───────────────────────┴─────────────────────┴──────────────────────────┴────────────────────────────┘
```

---

## Key Takeaways

1. **Retries are weapons.** They solve transient failures but amplify sustained failures. Safe retries require idempotency, budgets, backoff with jitter, retryable-error classification, and deadline awareness. Without all five, retries make outages worse.

2. **Circuit breakers have their own failure modes.** A circuit breaker that trips too aggressively creates outages. Synchronized breakers create thundering herds on recovery. Breakers without alerting hide failures. The breaker itself is a system that must be monitored, tuned, and tested.

3. **Bulkheads prevent the universal failure mode** of one slow dependency consuming all shared resources. Size them for normal operation, not degraded operation -- the point is to limit blast radius, not absorb degraded traffic.

4. **Timeouts are non-negotiable.** Every network call needs three timeouts (connect, read, write). Deadlines must propagate across service boundaries. A single missing timeout on a single code path can bring down the entire service.

5. **The patterns compose in a specific order.** Timeout wraps bulkhead wraps circuit breaker wraps retry wraps the actual call. Fallback catches the final failure. Retries inside the circuit breaker, not outside. This is not arbitrary -- the wrong order creates the failure modes each pattern was designed to prevent.

6. **Untested resilience is worse than no resilience** because it creates false confidence. Chaos engineering, load testing with failure injection, and explicit verification of every pattern's behavior under failure are required, not optional.

---

## 9. Real-World Resilience — Production Case Studies

Theory tells you what a circuit breaker is. This section shows you how to configure one for a payment processor versus a recommendation engine, why the numbers differ, and how to derive them from your own system's data instead of copying defaults from a blog post.

Every number below comes from a calculation, not a guess. The general approach: measure your system's actual behavior (latency percentiles, error rates, traffic patterns), apply Little's Law and basic probability, and derive the configuration from those measurements.

---

### 9.1 Payment Processing Service (Stripe/Adyen Integration)

A checkout service that charges customers via an external payment gateway. This is the hardest case because the operation is non-idempotent, financially consequential, and latency-sensitive (users are waiting at checkout).

```
SYSTEM PROFILE:
  Traffic:           200 charges/second peak (Black Friday: 800/sec)
  Gateway p50:       120ms
  Gateway p95:       350ms
  Gateway p99:       800ms
  Gateway error rate: 0.3% baseline (429s during rate limit spikes)
  Gateway SLA:       99.95% monthly
  Your SLA to users: checkout completes within 3 seconds

─────────────────────────────────────────────────────────────────────

STEP 1: TIMEOUT CALCULATION

  WHY NOT JUST "SET IT TO 5 SECONDS":
    Your SLA is 3 seconds total. The checkout flow is:
      validate cart (20ms) → reserve inventory (50ms) → charge (??ms)
      → create order (30ms) → send confirmation (async, not counted)

    Budget for charging: 3000 - 20 - 50 - 30 = 2900ms max.
    But you want headroom for variance: 2900 × 0.7 = ~2000ms.

  Connect timeout: 2 seconds.
    Gateway is external (cross-internet). DNS + TCP + TLS handshake
    to Stripe's edge can take 200-500ms normally. 2s covers cold
    connections and mild network congestion. If the gateway isn't
    reachable in 2 seconds, it's likely down — fail fast.

  Read timeout: 2 seconds.
    Gateway p99 = 800ms. Setting timeout at 2.5× p99 = 2000ms.
    This means ~0.1% of requests timeout under NORMAL conditions
    (only those beyond p99.9). That's acceptable: ~0.2 timeouts/sec
    at 200 rps. Those get retried.

    WHY NOT 1 SECOND (closer to p99):
    At 1s timeout, ~1% of healthy requests timeout = 2 rps of false
    timeouts. At 3 retries each, that's 6 unnecessary retries/sec on
    a system that's working perfectly. You're punishing the gateway
    for normal variance.

    WHY NOT 5 SECONDS:
    If the gateway hangs, each thread is blocked for 5s.
    At 200 rps: Little's Law says L = 200 × 5 = 1,000 threads needed.
    If you have 100 threads in the bulkhead → pool exhausts in 0.5s.
    Even with the bulkhead, 5s timeout means each thread is wasted
    for 5 seconds. At 2s timeout, threads recycle 2.5× faster.

─────────────────────────────────────────────────────────────────────

STEP 2: RETRY CONFIGURATION

  Idempotency key: MANDATORY. Generated client-side (UUID v4).
    Sent as Idempotency-Key header. Stored server-side for 48 hours.
    WITHOUT THIS, a retry on a read timeout = potential double charge.
    This is the first thing you implement, before any retry logic.

  Which errors to retry:
    ✓ Connect timeout     — gateway never saw the request. Safe.
    ✓ HTTP 429            — rate limited. Respect Retry-After header.
    ✓ HTTP 502, 503, 504  — gateway infra issue. Transient.
    ✗ HTTP 400            — your payload is wrong. Fix code, not retry.
    ✗ HTTP 402            — card declined. Retrying won't unblock the card.
    ✗ HTTP 404            — endpoint doesn't exist. Never recovers.
    ✗ Read timeout        — ONLY retry with idempotency key.
                            Without the key, you don't know if the charge
                            went through. Retrying = potential double charge.

  Max retries: 2 (total 3 attempts).
    WHY 2 AND NOT 5:
    Each retry takes time. With exponential backoff:
      Attempt 1: immediate (0ms wait)
      Attempt 2: ~200ms wait (100ms base × 2^1 × jitter)
      Attempt 3: ~500ms wait (100ms base × 2^2 × jitter)
    Total worst case: 2000ms (attempt 1) + 200ms + 2000ms (attempt 2)
    + 500ms + 2000ms (attempt 3) = 6,700ms.
    But your total deadline is 2,900ms. After attempt 1 takes 2000ms,
    you have 900ms remaining. That's enough for ONE retry (200ms wait +
    max 700ms of the call). A third retry wouldn't fit.

    PRACTICAL RULE: max_retries = floor(remaining_deadline / (timeout +
    max_backoff)) after the first attempt.

  Retry budget: 10% of traffic = 20 retries/sec at 200 rps.
    At 0.3% baseline error rate: 0.6 failures/sec. Budget is ample.
    At a 30% spike: 60 failures/sec. Only 20 get retried. Load on
    gateway: 200 + 20 = 220 rps (+10%). Survivable.
    Without budget: 60 × 2 retries = 120 extra rps. Load: 320 rps
    (+60%). Might push gateway into deeper failure.

  Backoff: base=100ms, exponential with full jitter.
    delay = random(0, min(100 × 2^attempt, 2000))
    Full jitter chosen because Stripe's rate limiter recovers quickly
    (sub-second). You want retries spread across time, not bunched.

─────────────────────────────────────────────────────────────────────

STEP 3: CIRCUIT BREAKER CONFIGURATION

  Sliding window: TIME-BASED, 10 seconds, 1-second buckets.
    WHY TIME-BASED, NOT COUNT-BASED:
    At 200 rps, a count window of 100 calls = 0.5 seconds of traffic.
    Too reactive — a 100ms network blip trips the breaker. A 10-second
    window means transient blips are diluted by the surrounding healthy
    traffic, but sustained failures (gateway down for 5+ seconds)
    trip the breaker before too much damage is done.

  Failure threshold: 40%.
    WHY 40%:
    Baseline error rate is 0.3%. Setting threshold at 40% means:
    - Normal 0.3%: nowhere near tripping. Safe margin: 133× baseline.
    - Gateway rate-limiting (5-10% errors): still below threshold.
      Rate limiting is self-correcting; tripping the breaker would
      block ALL charges, which is worse than the 10% that are failing.
    - Gateway partially down (40%+ errors): breaker trips. At this
      point, more than 1 in 3 charges is failing. Users are seeing
      errors. Continuing to send traffic isn't helping.

    WHY NOT 10% ("catch failures early"):
    Stripe occasionally rate-limits during traffic spikes. A 10%
    threshold would trip during normal Black Friday traffic when the
    rate limiter kicks in for 30 seconds. You'd block ALL charges
    during your highest-revenue hour.

  Minimum calls in window: 50.
    At 200 rps, 50 calls accumulate in 0.25 seconds. This prevents
    tripping on 2 failures out of 3 calls during the first 15ms
    after a deploy when traffic is ramping up.

  Wait duration (OPEN → HALF-OPEN): 15 seconds, with ±5s jitter.
    WHY 15 SECONDS:
    Stripe's typical outage recovery is 10-60 seconds. 15 seconds
    gives the gateway time to recover without making users wait
    too long. Jitter prevents 20 instances from all probing at t=15.

  HALF-OPEN probes: 5 requests.
    Require 4/5 success to close. One success is weak evidence.
    Five requests with 80% success threshold gives confidence the
    gateway is actually healthy, not just sporadically responding.

  FALLBACK when OPEN:
    NOT cached data. NOT a default response. For payments, the only
    safe fallback is:
      1. Return a clear error: "Payment could not be processed.
         Please try again in a moment."
      2. Preserve the cart and idempotency key so the retry uses
         the same key (preventing double charge on eventual success).
      3. Optionally queue the charge for async processing with
         explicit user consent ("We'll charge you when the system
         recovers and email your confirmation").

    NEVER silently succeed without actually charging. NEVER return
    cached payment results. Financial operations have no safe
    "degraded" mode — they either succeed or they don't.

─────────────────────────────────────────────────────────────────────

STEP 4: BULKHEAD CONFIGURATION

  Isolation type: Semaphore (not thread pool).
    WHY: The payment service is async (Netty/Spring WebFlux/Node.js).
    Thread pools don't apply. Semaphore limits concurrent in-flight
    requests to the gateway.

  Semaphore size: 40 permits.
    CALCULATION (Little's Law):
      Normal: L = λ × W = 200 rps × 0.12s (p50) = 24 concurrent.
      Add safety margin: 24 × 1.7 = ~40 permits.

    WHAT HAPPENS WHEN GATEWAY SLOWS TO 2 SECONDS:
      L = 200 × 2.0 = 400 concurrent needed. Semaphore caps at 40.
      Only 40 requests in-flight at once. The other 160 rps fail fast
      with "service unavailable." That's 80% of charges failing, but
      the OTHER endpoints (cart, inventory, search) are unaffected.
      Without the bulkhead, all 200 rps pile up on the gateway,
      consuming ALL server resources, and cart/search/everything dies.

    AT BLACK FRIDAY (800 rps):
      Normal concurrency: 800 × 0.12 = 96. Semaphore of 40 is too
      small! Scale to: 800 × 0.12 × 1.7 = ~160 permits.
      → This is why bulkhead size should be configurable per
        environment, not hardcoded. Use an env var or config service.
```

---

### 9.2 Background Job Processing (Queue Workers)

A worker service that consumes jobs from a queue (SQS, RabbitMQ, Kafka) and calls external APIs to process them. Examples: sending emails via SendGrid, generating PDFs via a rendering service, processing webhook deliveries.

```
SYSTEM PROFILE:
  Queue depth:       5,000 jobs average, 50,000 during spikes
  Worker instances:  10 workers, each processing 20 concurrent jobs
  Total throughput:  200 jobs/second
  External API p99:  500ms (email), 2s (PDF), 300ms (webhook)
  Job SLA:           process within 5 minutes of enqueue

─────────────────────────────────────────────────────────────────────

WHY QUEUES CHANGE THE RESILIENCE CALCULUS:

  Synchronous (HTTP API):
    User is waiting → latency matters → timeout must be tight.
    If you fail, the user sees an error immediately.

  Asynchronous (queue worker):
    Nobody is waiting in real-time → latency is less critical.
    If you fail, the job goes back on the queue and retries later.
    The QUEUE ITSELF is a natural retry mechanism with built-in backoff.

  This changes your resilience configuration significantly:

  1. TIMEOUTS CAN BE LONGER (user isn't waiting):
     Email API timeout: 10 seconds (vs. 2s for a synchronous call).
     PDF generation timeout: 30 seconds (PDFs are legitimately slow).
     Webhook delivery timeout: 5 seconds.

     CALCULATION: your constraint is the job SLA (5 min), not user
     patience. With 3 retry attempts and 30s timeout each:
     worst case per job = 30 + 30 + 30 + backoff = ~2 minutes.
     Well within the 5-minute SLA.

  2. RETRY STRATEGY IS DIFFERENT:
     Synchronous: retry immediately with exponential backoff (user
     is waiting, every millisecond counts).

     Queue worker: let the job fail, return it to the queue, and let
     the queue's visibility timeout handle the retry delay.

     SQS visibility timeout = backoff:
       Attempt 1: immediate processing
       Attempt 2: visibility timeout 30 seconds (job re-appears)
       Attempt 3: visibility timeout 2 minutes
       Attempt 4: visibility timeout 10 minutes
       After max attempts: move to Dead Letter Queue (DLQ)

     WHY THIS IS BETTER THAN IN-PROCESS RETRIES:
     - If the worker crashes mid-retry, the job isn't lost (still
       on the queue). In-process retries die with the process.
     - The queue distributes retries across all workers. A slow
       worker doesn't hold the job hostage.
     - Backoff is per-job, managed by the queue, not per-worker.

  3. CIRCUIT BREAKER MATTERS EVEN MORE:
     Without a breaker, 200 jobs/sec hitting a dead email API means
     200 timeouts/sec × 10s timeout = 2,000 concurrent stuck workers.
     The queue backs up to 50,000, then 100,000, then the queue itself
     hits limits (SQS: 120,000 in-flight, RabbitMQ: memory alarm).

     With a breaker (trips at 50% failure rate):
       Email API goes down → within 5 seconds, breaker opens.
       Jobs that need email are immediately returned to the queue
       with a short visibility timeout (30s). No timeout waiting.
       Workers are free to process non-email jobs.
       Every 30 seconds, a probe checks if email API is back.

  4. BULKHEAD IS PER-JOB-TYPE, NOT PER-DEPENDENCY:
     If your worker processes emails, PDFs, and webhooks:
       Email pool:   8 concurrent (fast, high volume)
       PDF pool:     4 concurrent (slow, lower volume)
       Webhook pool: 8 concurrent (fast, high volume)

     CALCULATION:
       Email:   100 jobs/sec × 0.5s (p99) = 50 concurrent at p99.
                But each worker handles 20 total → cap at 8 per type.
       PDF:     20 jobs/sec × 2s (p99) = 40 concurrent at p99.
                Cap at 4 per worker. PDFs are slow — don't let them
                starve email and webhook processing.
       Webhook: 80 jobs/sec × 0.3s (p99) = 24 concurrent at p99.
                Cap at 8 per worker.

     WHY THIS MATTERS:
       If the PDF service hangs, only 4 threads are blocked.
       Email and webhook processing continues at full speed.
       Without per-type bulkheads: 20 slow PDF jobs block all 20
       worker threads → no emails, no webhooks, everything stops.

─────────────────────────────────────────────────────────────────────

DEAD LETTER QUEUE (DLQ) — THE ULTIMATE FALLBACK:

  After max retry attempts, the job moves to a DLQ.
  A DLQ is NOT a trash can. It is an alerting and investigation tool.

  REQUIRED SETUP:
  1. Alert when DLQ depth > 0 (warning) and > 100 (page).
  2. DLQ consumer that logs the job payload, failure reason, and
     attempt count for investigation.
  3. A replay mechanism: after fixing the root cause, replay DLQ
     jobs back to the main queue with one command.
  4. DLQ retention: 14 days. After that, jobs are gone.

  DLQ MATH:
    Normal DLQ rate: <0.01% of jobs (1 in 10,000).
    If DLQ rate exceeds 1%: something is systematically broken.
    If DLQ rate exceeds 10%: the external API has been down for
    longer than your retry policy covers. Investigate immediately.
```

---

### 9.3 LLM API Calls (OpenAI / Anthropic Integration)

A service that calls an LLM API for features like summarization, content moderation, or chat. LLM APIs have unique resilience challenges: high latency variance, token-based rate limits, streaming responses, and cost per call.

```
SYSTEM PROFILE:
  Traffic:           50 rps (content moderation on user posts)
  API p50:           800ms (depends on prompt length and model)
  API p95:           3 seconds
  API p99:           8 seconds (long prompts, complex reasoning)
  API rate limit:    1,000 requests/minute (org-level, shared)
  API cost:          $0.003 per request average
  Error rate:        0.5% (mostly 429 rate limits, occasional 500s)

─────────────────────────────────────────────────────────────────────

WHY LLM APIS ARE DIFFERENT FROM TYPICAL REST APIS:

  1. LATENCY IS BIMODAL:
     Short prompts (content moderation): 200-500ms.
     Long prompts (summarization): 2-10 seconds.
     A single timeout doesn't work. You need per-operation timeouts.

  2. RATE LIMITS ARE ORG-LEVEL, NOT PER-INSTANCE:
     If you have 10 instances each doing 50 rps, total = 500 rps.
     Rate limit is 1,000 rpm (≈17 rps). You're already 30× over
     the per-minute limit. Rate limiting is not an edge case — it's
     your normal operating condition.

  3. RETRIES ARE EXPENSIVE:
     A typical REST API retry costs microseconds of compute.
     An LLM API retry costs $0.003 (input tokens re-processed).
     At 50 rps with a 5% retry rate: 2.5 retries/sec × $0.003
     = $0.0075/sec = $648/day in wasted spend.
     Retries need cost awareness, not just availability awareness.

  4. STREAMING CHANGES TIMEOUT SEMANTICS:
     Non-streaming: wait for complete response. Timeout = total.
     Streaming: first token arrives in 200ms, then tokens stream
     for 5 seconds. A 3-second timeout kills a valid streaming
     response at 60% completion. You need:
       - Time to first token timeout: 5 seconds
       - Inter-token timeout: 2 seconds (if no token for 2s, abort)
       - Total response timeout: 30 seconds (absolute cap)

─────────────────────────────────────────────────────────────────────

STEP 1: TIMEOUT — PER OPERATION TYPE

  Content moderation (short prompt, fast response):
    Connect timeout: 3 seconds
    Time to first token: 5 seconds
    Total timeout: 10 seconds
    Reasoning: p99 is ~3s. 10s = 3× p99, catches all normal responses
    and only times out on genuine hangs.

  Summarization (long prompt, slow response):
    Connect timeout: 3 seconds
    Time to first token: 15 seconds
    Total timeout: 60 seconds
    Reasoning: long prompts take 5-15s to start generating.
    60s total covers a 4,000-token response at ~50 tokens/second.

  THE MISTAKE EVERYONE MAKES:
    Setting timeout = 10s for all LLM calls. Summarization times out
    on every long document. The team raises the timeout to 60s. Now
    content moderation (which should fail fast in 10s) blocks threads
    for 60s when the API hangs. Per-operation timeouts are mandatory.

─────────────────────────────────────────────────────────────────────

STEP 2: RETRY — COST-AWARE

  Which errors to retry:
    ✓ HTTP 429 (rate limited): ALWAYS. Respect Retry-After header.
       OpenAI returns Retry-After in seconds. Wait that long.
       If no Retry-After: exponential backoff starting at 1 second.
    ✓ HTTP 500 (internal error): yes, but max 1 retry.
       LLM APIs have genuine transient 500s (GPU allocation fails).
    ✓ HTTP 503 (overloaded): yes, with longer backoff (5s base).
    ✗ HTTP 400 (bad request): your prompt is malformed. Fix it.
    ✗ HTTP 401 (unauthorized): API key invalid. No retry helps.
    ✗ Timeout on streaming response at 80%+ completion:
       DO NOT retry. You already have most of the response.
       Use what you have or return partial results.
       Retrying re-processes ALL input tokens = double the cost
       for the last 20% of output.

  Max retries: 1 for 500s, 2 for 429s.
    WHY DIFFERENT:
    429 means "slow down, try later" — it WILL work if you wait.
    500 means "something broke" — a second try might work, a third
    probably won't and costs $0.009 total.

  Backoff for 429s: start at Retry-After value (or 1s), exponential.
    delay = max(retry_after_header, base × 2^attempt)
    Base = 1 second (not 100ms — LLM rate limits recover in seconds,
    not milliseconds, and you're sharing the limit with other teams).

  COST GUARDRAIL:
    Track retry spend as a percentage of total LLM spend.
    Alert if retry_cost / total_cost > 5%.
    This catches: a bug that causes infinite retries of the same
    failing prompt, a model version that returns 500 on specific
    inputs (retrying the same input forever), and retry storms
    from multiple instances hitting rate limits simultaneously.

─────────────────────────────────────────────────────────────────────

STEP 3: CIRCUIT BREAKER — WITH RATE LIMIT AWARENESS

  THE CRITICAL DISTINCTION:
    HTTP 429 (rate limit) is NOT a failure of the dependency.
    It is the dependency telling you to slow down. It is working
    correctly. DO NOT count 429s toward the circuit breaker failure
    rate. If you do, rate limiting trips your breaker, which blocks
    ALL requests, including the ones that would have succeeded if
    you'd just waited 1 second.

  Failure threshold: 30% (counting only 500s and timeouts, NOT 429s).
  Sliding window: 60 seconds (LLM APIs have longer recovery cycles).
  Minimum calls: 20.

    WHY 30%:
    LLM APIs have higher baseline error rates than typical REST APIs
    (GPU allocation failures, model loading delays). 30% means:
    - Baseline 0.5% errors: 60× safety margin.
    - Brief spike of 10% errors (model deployment): no trip.
    - Sustained 30%+ errors: API is genuinely down. Trip.

    WHY 60-SECOND WINDOW:
    LLM API outages tend to last minutes, not seconds (GPU cluster
    issues, model deployment rollbacks). A 10-second window would
    cause the breaker to oscillate: trip, wait 30s, probe succeeds
    (the API recovered briefly), close, trip again 5 seconds later.
    A 60-second window smooths this out.

  HALF-OPEN: 3 probes over 30 seconds (1 probe every 10s).
    LLM APIs are expensive — don't probe aggressively.
    Probes should use a CHEAP request (short prompt, fast model)
    not a production workload. A content moderation check on a
    10-word test input costs $0.0001 vs. $0.003 for a real request.

  FALLBACK when OPEN:
    Content moderation: queue posts for later moderation. Show to
    users with a "pending review" flag. No auto-approve — that
    defeats the purpose of moderation.

    Summarization: return a truncated version (first 3 paragraphs)
    with "Full summary temporarily unavailable."

    Chat: return "I'm temporarily unavailable. Please try again
    in a moment." NEVER fabricate a response without the LLM.

─────────────────────────────────────────────────────────────────────

STEP 4: RATE LIMITING — CLIENT-SIDE (YOU ARE THE CALLER)

  THE UNIQUE PROBLEM: shared org-level rate limits.
  If your limit is 1,000 rpm and you have 10 instances:
    Each instance gets 1,000 / 10 = 100 rpm = ~1.7 rps.
    But your traffic is 50 rps. That's 30× over the per-instance share.

  SOLUTION: client-side rate limiter using a token bucket.

    Token bucket (per instance):
      Rate: 100 tokens/minute (your fair share)
      Burst: 20 tokens (handle short spikes without hitting the limit)

    When bucket is empty: queue the request and wait for a token,
    up to a max queue wait of 5 seconds. If still no token after 5s,
    fail the request (the system is trying to use the LLM faster
    than the rate limit allows — this is a capacity problem, not
    a resilience problem).

  WHY NOT JUST RELY ON THE API'S 429 RESPONSE:
    Every 429 wastes a network round trip (100-300ms) and counts
    against your error metrics. Client-side rate limiting prevents
    the request from ever leaving your service. It's faster (no
    network hop), cheaper (no API call), and doesn't pollute your
    error rate metrics.

  COORDINATION ACROSS INSTANCES (advanced):
    For precise rate limiting across 10 instances, use a Redis-backed
    token bucket (shared state). Each instance checks Redis before
    calling the API.

    Redis overhead: 1-2ms per check. Acceptable for LLM calls
    that take 800ms+ anyway.

    If Redis is down: fall back to per-instance rate limiting
    at (org_limit / instance_count). Less precise but functional.
```

---

### 9.4 E-Commerce Product Page (Aggregation Service)

A product page that aggregates data from 6 microservices in a single user-facing request. This is the canonical case for bulkheads and degraded responses.

```
SYSTEM PROFILE:
  User SLA:          page loads in 800ms
  Traffic:           2,000 rps peak

  Dependencies and their profiles:
  ┌────────────────────┬────────┬────────┬────────┬──────────────────┐
  │  Service           │  p50   │  p99   │  Rate  │  Critical?       │
  ├────────────────────┼────────┼────────┼────────┼──────────────────┤
  │  Catalog           │  15ms  │  80ms  │  0.01% │  YES — no page   │
  │  Pricing           │  20ms  │  100ms │  0.05% │  YES — no buy    │
  │  Inventory         │  10ms  │  50ms  │  0.02% │  PARTIAL — show  │
  │                    │        │        │        │  "check in store" │
  │  Reviews           │  50ms  │  200ms │  0.1%  │  NO — omit       │
  │  Recommendations   │  80ms  │  300ms │  0.3%  │  NO — omit       │
  │  User profile      │  10ms  │  40ms  │  0.01% │  PARTIAL — show  │
  │  (personalization) │        │        │        │  generic page     │
  └────────────────────┴────────┴────────┴────────┴──────────────────┘

─────────────────────────────────────────────────────────────────────

TIMEOUT BUDGET (800ms total SLA):

  The aggregation service calls dependencies in TWO PHASES:

  Phase 1 — Critical (sequential, must succeed):
    Catalog → Pricing → Inventory
    Sequential worst case: 80 + 100 + 50 = 230ms (all at p99).
    Budget: 400ms for all three, with 200ms per-call timeout.

  Phase 2 — Non-critical (parallel, best-effort):
    Reviews + Recommendations + User Profile (all in parallel)
    Budget: 800 - 400 (phase 1) - 50 (own processing) = 350ms.
    Per-call timeout: 350ms. Slowest parallel call determines phase
    duration. Reviews p99 = 200ms, Recs p99 = 300ms, both fit.

  WHY NOT CALL EVERYTHING IN PARALLEL:
    You could — and many systems do. But pricing often depends on
    catalog data (product ID → price lookup). And inventory depends
    on pricing tier (wholesale vs. retail SKU). These dependencies
    force sequential calls in Phase 1.

    The key insight: parallelize what you can, sequence what you must,
    and set a timeout for each phase that fits within the total budget.

─────────────────────────────────────────────────────────────────────

BULKHEAD SIZING (per dependency):

  Total thread pool for the aggregation service: 400 threads.
  At 2,000 rps with 50ms average processing: L = 2000 × 0.05 = 100
  threads normally active. 400 gives 4× headroom.

  PER-DEPENDENCY SEMAPHORES:

  Catalog:         50 permits
    L = 2000 × 0.015 (p50) = 30. Safety: 30 × 1.7 = 50.

  Pricing:         60 permits
    L = 2000 × 0.020 = 40. Safety: 40 × 1.5 = 60.

  Inventory:       40 permits
    L = 2000 × 0.010 = 20. Safety: 20 × 2.0 = 40.

  Reviews:         30 permits
    L = 2000 × 0.050 = 100. BUT reviews are non-critical.
    Cap at 30 intentionally. If reviews are slow, let them fail.
    Don't allocate 100 permits to a non-critical dependency.

  Recommendations:  25 permits
    Same logic. Non-critical → small bulkhead.

  User Profile:    30 permits
    L = 2000 × 0.010 = 20. Safety: 20 × 1.5 = 30.

  TOTAL PERMITS: 50 + 60 + 40 + 30 + 25 + 30 = 235.
    Less than the 400 thread pool. This is correct. Bulkheads limit
    concurrency PER dependency. Even if all bulkheads are full
    simultaneously, total is 235 — the thread pool survives.

    THE MATH GUARANTEES SURVIVAL:
    Sum of all bulkhead limits < total thread pool size.
    This is the fundamental constraint. Violate it and bulkheads
    provide no real isolation.

─────────────────────────────────────────────────────────────────────

CIRCUIT BREAKER CONFIGURATION (per dependency):

  Critical dependencies (Catalog, Pricing):
    Failure threshold:     60%
    Sliding window:        10 seconds
    Minimum calls:         100
    Wait duration:         10 seconds ± 3s jitter

    WHY 60% (high threshold):
    Tripping the breaker on Catalog or Pricing means the ENTIRE
    product page fails (no fallback for these). A false trip is
    catastrophic. Only trip when the service is genuinely unusable.

  Non-critical dependencies (Reviews, Recommendations):
    Failure threshold:     25%
    Sliding window:        10 seconds
    Minimum calls:         50
    Wait duration:         30 seconds ± 10s jitter

    WHY 25% (low threshold):
    Tripping the breaker on Reviews just removes the reviews widget.
    The page still works. Trip aggressively — failing fast on a non-
    critical dependency is better than spending 350ms timing out on
    every request and slowing the page for everyone.

  NOTICE THE ASYMMETRY:
    Critical dependencies: high threshold (avoid false trips).
    Non-critical dependencies: low threshold (trip fast, save latency).
    This is the opposite of what most teams implement by default.

─────────────────────────────────────────────────────────────────────

FALLBACK STRATEGY (domain-specific):

  Catalog down:        ERROR. Cannot render the product page without
                       product data. Return 503 with "Product
                       temporarily unavailable."

  Pricing down:        ERROR or STALE with extreme caution.
                       Option A: show "Price unavailable" with
                       "Add to cart to see price." Safe but hurts
                       conversion.
                       Option B: show cached price with "Price as of
                       Xm ago" and a 5-minute max staleness.
                       After 5 minutes stale, switch to Option A.
                       NEVER serve a stale price older than 5 minutes
                       — prices change for flash sales, price drops,
                       and competitive matching.

  Inventory down:      Show "Check availability in store" or "Usually
                       ships in 1-2 days." Allow add-to-cart — validate
                       inventory at checkout (where it's checked again
                       anyway).

  Reviews down:        Hide the reviews section. Show star rating from
                       cache if available (changes slowly, stale is OK).

  Recommendations down: Hide the "You might also like" section.
                        Show static "Popular products" from a daily
                        cache instead.

  User Profile down:   Show generic page (no personalization).
                        "Hi there" instead of "Hi Harut."
```

---

### 9.5 Microservice-to-Database (Internal Dependency)

Your service's own database isn't usually thought of as a "dependency that needs a circuit breaker," but it's the most critical one. When the database is slow, your service is slow. When it's down, your service is down.

```
SYSTEM PROFILE:
  Database:          PostgreSQL, primary + 2 read replicas
  Connection pool:   20 connections (HikariCP / pgBouncer)
  Query p50:         2ms
  Query p99:         25ms
  Slow query spike:  queries degrade to 500ms during vacuum or
                     lock contention

─────────────────────────────────────────────────────────────────────

CONNECTION POOL = BUILT-IN BULKHEAD:

  The connection pool IS a bulkhead. It limits concurrent database
  calls to pool_size. When all connections are busy, new requests
  wait in the pool's queue (or fail fast if the queue is full).

  POOL SIZING (HikariCP formula):
    connections = (core_count × 2) + effective_spindle_count
    For a 4-core server with SSD: (4 × 2) + 1 = 9 connections.
    Round up to 10-15 for safety.

    WHY SO FEW (common surprise):
    PostgreSQL connections are expensive. Each connection is a full
    OS process (~10MB memory). 100 connections = 1GB memory on the
    database server. Connection overhead causes more harm than the
    parallelism helps.

    At 2ms p50 query time: 10 connections serve 10/0.002 = 5,000
    queries/second. More than enough for most services.

    When queries degrade to 500ms: 10 connections serve 10/0.5 = 20
    queries/second. Traffic above 20 qps either waits in the pool
    queue or fails. This is the bulkhead protecting you — without
    the pool limit, 1,000 connections would open and kill the DB.

  POOL TIMEOUTS:
    Connection acquisition timeout: 1 second.
      If no connection is available from the pool in 1 second,
      fail the request. Don't queue indefinitely — the pool is full
      because the database is slow, and waiting longer just delays
      the inevitable timeout.

    Connection max lifetime: 30 minutes.
      Prevents stale connections (TCP half-open, network changed,
      DB failover). HikariCP rotates connections transparently.

    Idle timeout: 10 minutes.
      Release connections that haven't been used. Returns memory
      to the database.

─────────────────────────────────────────────────────────────────────

CIRCUIT BREAKER ON THE DATABASE — YES OR NO?

  CONTROVERSIAL OPINION: usually NO for your primary database.

  WHY NOT:
    If your database is down, your service is down. There's no
    meaningful fallback. A circuit breaker that says "database is
    down, returning fallback" — returning WHAT? Your service's data
    IS the database.

  WHEN YES:
    - Read replicas: if a replica is slow, route reads to another
      replica or the primary. The circuit breaker per replica enables
      this routing.
    - Caching layer: if you have a Redis cache in front of the DB,
      a circuit breaker can switch reads to cache-only mode when
      the DB is struggling.
    - Separate read/write paths: circuit breaker on the write path
      can queue writes while the DB is briefly unavailable, if you
      can tolerate eventual consistency.

  WHAT YOU SHOULD USE INSTEAD:
    Statement timeout:     5 seconds per query.
      Set at the connection level: SET statement_timeout = '5s'.
      Prevents any single query from running for 10 minutes and
      blocking other queries behind it.

    Connection pool timeout: 1 second (as above).
      Prevents thread pile-up when the pool is exhausted.

    Health check query:    SELECT 1 every 30 seconds.
      Detects dead connections before a real query hits them.
      HikariCP does this automatically.

    Slow query alerting:   Alert when p99 > 100ms.
      Catch degradation before it becomes an outage.
```

---

### 9.6 Third-Party Webhook Delivery (Outbound)

Your service sends webhooks to customer-configured endpoints. Each customer's endpoint is a different dependency with unknown reliability. This is the hardest resilience problem because you control nothing about the destination.

```
SYSTEM PROFILE:
  Customers:         5,000 webhook endpoints
  Events:            500 events/second total
  Delivery SLA:      "best effort, at-least-once, within 1 hour"
  Customer endpoints: latency ranges from 50ms to 30 seconds
                      availability ranges from 99.9% to 60%

─────────────────────────────────────────────────────────────────────

WHY THIS IS THE HARDEST CASE:

  Every other example has a KNOWN dependency with MEASURABLE behavior.
  Webhook endpoints are UNKNOWN, UNCONTROLLED, and WILDLY VARIABLE.

  Customer A's endpoint: responds in 100ms, 99.99% uptime.
  Customer B's endpoint: responds in 15 seconds, goes down for
    hours at a time, returns HTML error pages instead of proper
    status codes, has TLS certificates that expire every 3 months.

  You need PER-CUSTOMER resilience, not global configuration.

─────────────────────────────────────────────────────────────────────

RETRY STRATEGY — DECAYING EXPONENTIAL:

  Unlike payment processing (retry in milliseconds) or LLM APIs
  (retry in seconds), webhooks use LONG backoff because:
  1. Customer endpoints may be down for hours (deploy, maintenance).
  2. At-least-once, within 1 hour is the SLA — not "within 5 seconds."
  3. Aggressive retries to a dead endpoint waste YOUR resources.

  RETRY SCHEDULE:
    Attempt 1:   immediate
    Attempt 2:   after 10 seconds
    Attempt 3:   after 1 minute
    Attempt 4:   after 5 minutes
    Attempt 5:   after 15 minutes
    Attempt 6:   after 30 minutes
    Attempt 7:   after 1 hour
    Attempt 8:   after 4 hours
    After 8 attempts: disable the webhook endpoint, notify customer.

  WHY THESE SPECIFIC INTERVALS:
    10s and 1m:   catch transient failures (deploy restart, network
                  blip). Most transient issues resolve in under 1 minute.
    5m and 15m:   catch short maintenance windows.
    30m and 1h:   catch extended outages while staying within SLA.
    4h:           one final attempt before giving up.

  EACH INTERVAL INCLUDES JITTER:
    actual_delay = scheduled_delay × random(0.8, 1.2)
    With 5,000 customers, even small synchronization causes bursts.

─────────────────────────────────────────────────────────────────────

PER-CUSTOMER CIRCUIT BREAKER:

  You cannot have ONE circuit breaker for "all webhook deliveries."
  Customer A's broken endpoint would trip the breaker and block
  deliveries to Customer B, who is perfectly healthy.

  Configuration (per customer):
    Failure threshold: 80% (customer endpoints are unreliable;
                       don't trip on 30% error rates — that's
                       "normal" for some customers).
    Sliding window:    10 minutes (longer than typical — customer
                       endpoints recover slowly).
    Minimum calls:     5 (some customers get 1 event/day; don't
                       trip on 1 failure out of 2 calls).
    Wait duration:     5 minutes (probe every 5 minutes; customer
                       endpoints aren't worth probing every 15 seconds).

  MEMORY CONSIDERATION:
    5,000 customers × 1 circuit breaker each = 5,000 circuit breakers.
    Each breaker stores: state (3 bytes), counters (~100 bytes),
    timestamps (~24 bytes) = ~127 bytes × 5,000 = 620KB total.
    Negligible memory. Scale concern is configuration management,
    not memory.

─────────────────────────────────────────────────────────────────────

BULKHEAD — PER-CUSTOMER CONCURRENCY LIMIT:

  Total delivery workers: 100 concurrent.
  Per-customer limit:     3 concurrent deliveries.

  WHY 3:
    Customer B's endpoint takes 15 seconds to respond.
    Without per-customer limits, B's deliveries consume:
    events_for_B × 15s = potentially dozens of workers.
    With a limit of 3: maximum 3 workers blocked by B.
    The other 97 workers serve the remaining 4,999 customers.

    If B has 50 pending events: 3 are in-flight, 47 are queued.
    They'll be delivered eventually, at B's slow pace, without
    affecting anyone else.

─────────────────────────────────────────────────────────────────────

TIMEOUT — GENEROUS BUT BOUNDED:

  Connect timeout: 5 seconds.
    Customer endpoints might be on slow infrastructure, behind
    multiple proxies, or in distant regions. 5s is generous.

  Read timeout: 15 seconds.
    Some endpoints do heavy processing on receive (validate,
    acknowledge, log). 15s accommodates slow but functional endpoints.

  WHY NOT 30 OR 60 SECONDS:
    A 30s timeout × 3 concurrent workers per customer = each slow
    customer can block 3 workers for 30s each. At 100 total workers,
    if 34 customers are slow simultaneously: 34 × 3 = 102 workers
    blocked. Pool exhausted. Timeout of 15s halves the exposure:
    same 34 customers block 3 workers for 15s each — workers recycle
    2× faster, effective throughput is 2× higher.
```

---

### 9.7 How to Derive Numbers for YOUR System

Every number in the examples above was calculated, not guessed. Here's the general process:

```
STEP-BY-STEP: DERIVING YOUR RESILIENCE CONFIGURATION

  1. MEASURE YOUR DEPENDENCIES (before configuring anything):

     For each dependency, collect:
     ┌───────────────────────────────────────────────────────────────┐
     │  Metric              │  How to get it                        │
     ├───────────────────────────────────────────────────────────────┤
     │  p50 latency         │  Application metrics (Prometheus,     │
     │  p95 latency         │  Datadog, etc.) — histogram of        │
     │  p99 latency         │  response times over 7 days           │
     │  p999 latency        │                                       │
     ├───────────────────────────────────────────────────────────────┤
     │  Error rate (%)      │  Count of 5xx / total requests.       │
     │  Error types         │  Breakdown: timeout vs. 500 vs. 429.  │
     │                      │  Each type may need different handling.│
     ├───────────────────────────────────────────────────────────────┤
     │  Traffic (rps)       │  Requests per second at p50 and peak. │
     │  Peak traffic        │  Your resilience must work at peak,   │
     │                      │  not at average.                      │
     ├───────────────────────────────────────────────────────────────┤
     │  Dependency recovery │  How long does this dependency take   │
     │  time                │  to recover from failure? 5 seconds?  │
     │                      │  5 minutes? This sets your circuit    │
     │                      │  breaker wait duration.               │
     └───────────────────────────────────────────────────────────────┘

  2. CALCULATE TIMEOUT (from latency data):
     timeout = p99 × multiplier

     multiplier choices:
       2× p99: aggressive. ~0.5% of healthy requests timeout.
               Use for non-critical, latency-sensitive calls.
       3× p99: balanced. ~0.1% of healthy requests timeout.
               Default choice for most dependencies.
       5× p99: conservative. Almost never false-timeouts.
               Use for critical ops where a false timeout is expensive.

     SANITY CHECK: timeout × peak_rps < thread_pool_size.
     If not, either lower the timeout or increase the pool.

  3. CALCULATE BULKHEAD SIZE (from Little's Law):
     permits = peak_rps × p50_latency × safety_margin

     safety_margin choices:
       1.5×: tight. May hit the limit during p99 latency spikes.
       2.0×: balanced. Handles normal variance.
       3.0×: generous. Use for critical dependencies.

     SANITY CHECK: sum(all_bulkheads) < total_thread_pool.

  4. CALCULATE CIRCUIT BREAKER THRESHOLD:
     threshold = max(baseline_error_rate × 10, 25%)

     The 10× rule: your threshold should be at least 10× above
     baseline. This prevents tripping on normal variance while
     still catching genuine failures.

     Floor of 25%: even with a 0.01% baseline, don't set the
     threshold below 25%. Too sensitive.

     Ceiling of 80%: above 80%, the dependency is so broken that
     you're losing most requests anyway. The breaker adds limited
     value but may still prevent resource exhaustion.

  5. CALCULATE RETRY BUDGET:
     budget_rps = peak_rps × 0.10

     10% is the Google SRE default. Adjust:
       5%  for fragile dependencies (barely handling current load).
       15% for robust dependencies (significant headroom).

  6. CALCULATE BACKOFF BASE:
     base_delay = dependency_recovery_time / max_retries / 3

     If the dependency typically recovers in 30 seconds and you have
     3 retries: base = 30 / 3 / 3 = 3.3 seconds.
     This spaces retries across the recovery window.

     For fast-recovering dependencies (< 1 second): base = 100ms.
     For slow-recovering dependencies (> 1 minute): use queue-based
     retry (SQS, Kafka) instead of in-process backoff.

  7. VALIDATE WITH LOAD TESTING:
     All calculations above are estimates. Validate by:
     a. Running at peak traffic.
     b. Injecting the failure mode each pattern targets.
     c. Measuring: does the pattern behave as calculated?
     d. Adjusting numbers based on observed behavior.

     THE MOST COMMON ADJUSTMENTS AFTER TESTING:
     - Bulkhead too small: increased from 1.5× to 2.5× safety.
     - Circuit breaker too sensitive: raised threshold from 30% to 50%.
     - Timeout too short: raised from 2× p99 to 3× p99.
     - Retry backoff too aggressive: base from 100ms to 500ms.
```

---

## 10. Interview Preparation — Resilience Patterns

Questions designed to test real-world judgment at the mid-to-staff engineer level. For each question, think through the answer before reading the guidance. The best answers demonstrate tradeoff reasoning, not pattern memorization.

---

### Conceptual and Design Questions

**Q1: Your service calls three downstream dependencies. One of them starts responding in 8 seconds instead of 50ms. You have no resilience patterns in place. Walk me through exactly what happens to your service and how the failure propagates.**

What the interviewer wants: The full cascade story — thread pool exhaustion, requests to healthy dependencies getting blocked, upstream callers timing out, load balancer redistribution, and total outage from a single slow dependency. Mention specific numbers: thread pool size, arrival rate, how quickly threads exhaust. Show you understand that slow is worse than down because slow holds resources while down releases them immediately.

---

**Q2: You're adding retry logic to a payment service that calls a third-party payment processor. What are the five things you need to get right before a single retry is safe to send?**

What the interviewer wants: (1) Idempotency keys — the operation is a charge, retrying without one double-charges the customer. (2) Retry budget — not per-request retry count, but a global budget as a percentage of traffic. (3) Backoff with jitter — exponential backoff alone creates thundering herds. (4) Deadline awareness — no point retrying if the user's request deadline has already passed. (5) Error classification — a 400 should never be retried; a connect timeout is safe to retry even without an idempotency key because the server never received the request. The strong answer also distinguishes connect timeout (safe) from read timeout (unsafe without idempotency key).

---

**Q3: Explain retry amplification. Your architecture has 5 layers of services, each retrying 3 times. How many requests hit the bottom service per user request? How do you prevent this?**

What the interviewer wants: 3^5 = 243 requests per single user request. Prevention: (1) Only retry at the edge — intermediate services should not retry, or should use a shared retry budget propagated via headers. (2) Retry budgets (Google SRE approach) — limit retries to 10% of total traffic, not per-request counts. (3) Deadline propagation — if there's no time left for a retry to be useful, don't send it. The staff-level answer notes that retry budgets must be enforced at each layer independently, and that gRPC propagates deadlines natively while HTTP requires custom headers.

---

**Q4: Your circuit breaker is configured with a 50% failure threshold on a 100-call sliding window. Your dependency has a normal 2% error rate but occasionally spikes to 5% for a few seconds. Should you change the configuration? What are the risks of setting the threshold too low vs. too high?**

What the interviewer wants: The threshold is fine at 50% — a 5% spike is well below it. Risks of too low: the breaker trips during normal variance, turning a 2% degradation into a 100% outage (the breaker itself causes the outage). Risks of too high: the breaker never trips, and you get no protection — the service exhausts threads waiting for a failing dependency. The strong answer mentions the minimum-calls threshold: with a time-based window at low traffic, 1 failure out of 2 calls is 50%, which would trip the breaker on noise. You need a minimum call count (e.g., 100) before evaluating the failure rate.

---

**Q5: You have 50 instances of Service B, all with circuit breakers to Service C. Service C goes down, all 50 breakers open, and C recovers 30 seconds later. What happens next, and what can go wrong?**

What the interviewer wants: All 50 breakers enter HALF-OPEN simultaneously (identical wait durations). Each sends a probe request. C receives 50 simultaneous probes. If C was barely recovering, this probe burst overwhelms it, all probes fail, all breakers reopen. This creates an oscillation cycle that prevents C from ever recovering. Solutions: (1) Jittered wait duration so breakers enter HALF-OPEN at different times. (2) Randomized probe probability so only a fraction of instances probe at each interval. (3) Percentage ramp recovery instead of binary OPEN/CLOSED. The staff answer identifies this as a coordination problem and connects it to the broader theme of avoiding synchronized behavior in distributed systems.

---

**Q6: Why must retries be placed inside the circuit breaker, not outside? What specific failure mode occurs if you get the order wrong?**

What the interviewer wants: If retries wrap the circuit breaker (outside), the first attempt fails and the breaker opens. The retry policy sends 2 more attempts, both hit the open breaker and fail immediately. The breaker now records 3 failures (original + 2 retries against the open breaker), inflating the failure count. The breaker thinks the downstream is 3x worse than reality. Correct order: circuit breaker wraps retry. The breaker sees the outcome of the full retry sequence as one logical call — either eventually-succeeded or finally-failed. This gives the breaker accurate health data.

---

**Q7: Walk me through the correct layering order of timeout, bulkhead, circuit breaker, retry, and fallback. Why this specific order and not another?**

What the interviewer wants: Outermost to innermost: Timeout → Bulkhead → Circuit Breaker → Retry → Actual Call → Fallback. Reasoning: (1) Timeout is outermost because it's the absolute deadline — nothing inside should exceed it. (2) Bulkhead is next because you want to limit concurrency before checking the breaker — if the bulkhead is full, fail fast without even asking the breaker. (3) Circuit breaker checks dependency health before attempting any call or retry. (4) Retry is innermost (closest to the call) because retries should only happen when the breaker allows them, within the bulkhead's concurrency limit, and within the overall deadline. (5) Fallback catches the final failure after all retry attempts are exhausted.

---

### Scenario-Based Questions

**Q8: Your e-commerce product page aggregates data from 4 services: catalog, pricing, reviews, and recommendations. The reviews service goes down. What do you do? What if the pricing service goes down instead?**

What the interviewer wants: Reviews down → serve the product page without reviews (degraded response). This is safe because reviews are not critical to completing a purchase. Pricing down → this is a different story. Serving stale prices is dangerous — a stale price that's too low creates a financial loss; a stale price that's too high creates customer trust issues. The fallback strategy must be domain-aware. Possible approaches: show "price unavailable, add to cart to see price," use last-known price with a freshness indicator and a short TTL cache, or fail the product page entirely if the price is stale beyond a threshold. The staff answer connects this to the fallback hierarchy and explains that not all degraded responses are equal — the domain determines which fallbacks are safe.

---

**Q9: You're running a load test and notice that when you inject a dependency failure, your retry budget is being exhausted in seconds, but the circuit breaker hasn't tripped yet. What's wrong and how do you fix it?**

What the interviewer wants: The circuit breaker's sliding window is likely count-based and too large (e.g., 1000 calls) or the failure threshold is too high. The retry budget depletes at the traffic rate, but the circuit breaker needs N failures in its window before tripping. If the window is large, it takes a long time to accumulate enough failures. Meanwhile, every failed request burns a retry token. Fix: (1) Switch to a time-based sliding window so the breaker evaluates recent behavior regardless of traffic volume. (2) Lower the sliding window size. (3) Ensure the circuit breaker evaluates fast enough to trip before the retry budget is fully consumed. The deeper answer notes that these two patterns need to be tuned together — the breaker should trip fast enough to preserve the retry budget for when the downstream actually recovers.

---

**Q10: Your team proposes adding hedged requests to reduce p99 latency for your search service. The search service calls a single Elasticsearch cluster. Should you do it? Why or why not?**

What the interviewer wants: No. Hedging helps when tail latency comes from per-request variance (GC pauses, queue depth on a specific node) and you can route the hedge to a different backend. If all replicas share the same Elasticsearch cluster, the hedge hits the same bottleneck. Worse, if the cluster is under load, hedging doubles the load, making p99 worse for everyone. Hedging only works when: (1) backends are independent (no shared bottleneck), (2) the operation is idempotent, (3) you can cancel the losing request, and (4) the hedge fires at p95+ (not earlier, or you permanently double load). The right answer for this scenario: investigate why p99 is high on Elasticsearch — it's likely a query optimization or cluster sizing issue, not something hedging can solve.

---

**Q11: You're on-call and get paged. Your service's error rate is near zero, latency is normal, and all dashboards are green. But a colleague mentions that one downstream dependency has been down for 6 hours. What happened?**

What the interviewer wants: The circuit breaker opened and the fallback is serving cached/degraded data. All user-facing metrics look healthy because the breaker suppresses errors and the fallback responds fast. This is the "silent degradation" failure mode. The dependency has been down for 6 hours but nobody noticed because: no error rate alarm (breaker suppresses errors), no latency alarm (fallback is fast), no traffic alarm on the dependency (breaker blocks all traffic). The fix: always alert on circuit breaker state changes, alert when a breaker has been OPEN for more than X minutes, alert on fallback activation rate. The breaker buys time to investigate, not permission to ignore.

---

**Q12: Your service has a 500ms SLA. It calls Service A (p99: 50ms) and Service B (p99: 200ms) sequentially. You set both call timeouts to 500ms. What's wrong with this?**

What the interviewer wants: Without deadline propagation, worst case is 500ms + 500ms = 1000ms, which violates the 500ms SLA. The correct approach: propagate a deadline. Service A gets the full 500ms deadline but should return in ~50ms. After A responds (say 60ms used), Service B gets 440ms remaining. If A is slow and takes 300ms, B only gets 200ms — which might not be enough for B's normal p99, so you may need to fail fast or use a fallback for B. The staff answer also notes that the timeout for each call should be set relative to that call's p99 (2-3x p99), but the overall deadline is the governing constraint. You need both: per-call timeouts AND a propagated deadline.

---

**Q13: You join a team that has circuit breakers on all downstream calls but no chaos testing. They've never seen a breaker trip in production. Should you be concerned?**

What the interviewer wants: Yes, very concerned. Either (1) the thresholds are set so high the breakers will never trip, meaning you have zero protection, (2) the downstream services happen to be reliable enough that the breakers haven't been needed yet — but you have no confidence they'll work correctly when they are needed, or (3) the breakers have a bug and would fail to trip. Untested resilience is worse than no resilience because it creates false confidence. The action items: run chaos experiments to deliberately trip the breakers, verify the fallbacks activate, verify alerts fire, measure recovery lag, and check the configuration against actual baseline error rates.

---

**Q14: Design the resilience strategy for a service that processes financial transactions. The service must call a fraud detection service before approving any transaction. The fraud service has a p99 of 200ms and occasionally has 30-second outages. What patterns do you use and what are your fallback options?**

What the interviewer wants: This is a trick question about fallbacks. The standard fallback hierarchy (cache, degrade, default) is dangerous here. You cannot skip fraud detection — approving a transaction without fraud checking exposes the business to fraud losses. You cannot use cached fraud decisions — they don't apply to new transactions. Your options are limited: (1) Queue the transaction and process it when fraud service recovers (latency hit but correct), (2) Fail the transaction with a clear error to the user ("please try again in a moment"), (3) Apply a simplified local fraud rule (amount threshold, velocity check) as a degraded-but-not-absent check. The staff answer recognizes that some dependencies are mandatory and cannot be gracefully degraded. Circuit breakers still help (fail fast instead of waiting 30 seconds), but the fallback must match the domain's safety requirements.

---

**Q15: You're reviewing a PR that adds retries with exponential backoff but no jitter to a high-throughput service (10,000 rps). The author says jitter is an unnecessary optimization. Convince them otherwise with a concrete scenario.**

What the interviewer wants: At 10,000 rps, even a 100ms outage causes 1,000 requests to fail simultaneously. Without jitter, all 1,000 retry at exactly t=100ms (first backoff). All 1,000 fail again. All retry at exactly t=200ms. The downstream sees perfectly synchronized spikes of 1,000 requests every backoff interval, on top of the 10,000 rps of new traffic. With full jitter, those 1,000 retries spread uniformly across the [0, 100ms] window, adding roughly 10 extra requests per millisecond — which the downstream can absorb. The difference between "1,000 simultaneous retries" and "10 extra rps spread across 100ms" is the difference between deepening the outage and transparently recovering from it. This isn't an optimization — at high throughput, it's a correctness requirement.

---

### Architecture and Tradeoff Questions

**Q16: When would you implement resilience patterns in application code (Resilience4j, Polly) vs. in a service mesh (Envoy/Istio)? What are the tradeoffs?**

What the interviewer wants: Service mesh advantages: no code changes, consistent policy across all services, language-agnostic, centrally managed configuration, operational team can tune without developer involvement. Service mesh disadvantages: limited to L7 patterns (retry, timeout, circuit breaking via outlier detection), cannot implement application-aware fallbacks (cache, degraded response), adds network hop latency through the sidecar, harder to debug (failure happens in the sidecar, not in the application). Application library advantages: full control, can implement domain-specific fallbacks, can integrate with application state (caches, queues), more granular per-operation configuration. Application library disadvantages: requires code changes, inconsistent implementation across teams/languages, each team must understand and tune correctly. The staff answer: use the service mesh for baseline protection (timeouts, retries, outlier detection) across all services, and add application-level patterns (circuit breakers with fallbacks, domain-aware retry logic) where the domain requires it. Defense in depth, not either/or.

---

**Q17: Your company is adopting microservices. The architect proposes adding circuit breakers, bulkheads, retries, timeouts, fallbacks, hedged requests, and deadline propagation to every service from day one. What do you say?**

What the interviewer wants: Push back. This is over-engineering. Start with the patterns that prevent the most common and most dangerous failure modes: (1) Timeouts on every outgoing call — non-negotiable from day one. (2) Retries with backoff, jitter, and budget on idempotent calls. (3) Circuit breakers on critical dependencies with proper alerting. Add bulkheads when you have services with multiple dependencies and have observed or can model the shared-resource-exhaustion problem. Add hedged requests only when you have measured tail latency issues with independent backends. Add deadline propagation when you have request chains deeper than 2 hops. The principle: add patterns when you have evidence (traffic data, failure data, or architectural analysis) that the failure mode they address is a real risk. Resilience patterns have costs — complexity, operational overhead, configuration surface area — and every one is a system that can itself fail.

---

**Q18: You have a service with 200 dependencies (large fanout aggregation service). Thread-pool-per-dependency bulkheads are impractical. What do you do?**

What the interviewer wants: 200 thread pools is too many — the memory overhead (stack per thread) and context-switch overhead would be significant. Options: (1) Semaphore-based bulkheads — a counting semaphore per dependency limits concurrency without dedicated threads. Low overhead, scales to hundreds of dependencies. Downside: cannot force-timeout stuck calls since the caller's thread is blocked. (2) Connection pool isolation — limit max connections per dependency in the HTTP client. Built into most HTTP libraries. (3) Async/non-blocking I/O — if the service uses an event loop model (Node.js, Netty, Go goroutines), thread exhaustion is not the failure mode. Instead, limit in-flight requests per dependency with a semaphore. (4) Group dependencies into tiers by criticality — critical dependencies (5-10) get thread-pool bulkheads; non-critical dependencies (190) get semaphore bulkheads or connection pool limits. The staff answer recognizes that the isolation mechanism should match the execution model and the dependency count.

---

**Q19: A junior engineer asks: "If circuit breakers stop sending requests to a failing service, how does the service ever recover? Aren't we making it worse by cutting off traffic?" How do you explain why circuit breakers help recovery, not hurt it?**

What the interviewer wants: When a service is failing due to overload, sending more requests makes it worse. The service needs breathing room to recover — drain its queues, close hung connections, finish processing stuck requests, possibly restart. The circuit breaker gives it that breathing room by cutting off the traffic that's piling up. The HALF-OPEN state then sends a small number of probe requests to test if the service has recovered. If probes succeed, traffic gradually resumes. Without the circuit breaker, the failing service receives the same (or amplified, due to retries) traffic, never gets a chance to recover, and the failure either persists indefinitely or cascades to callers. The analogy: a circuit breaker in your house trips to prevent the wiring from catching fire. It doesn't "make it worse" by cutting power — it prevents the damaging condition from continuing. You fix the problem, then flip the breaker back.

---

**Q20: You're designing a resilience strategy for a globally distributed service. Requests from US users hit the US region, EU users hit the EU region. Each region has its own set of dependencies. Should circuit breakers be per-region, per-instance, global, or something else?**

What the interviewer wants: Per-instance circuit breakers (the default) are the starting point. Each instance tracks its own failure rates based on the traffic it sends. This naturally handles per-region behavior because US instances only call US dependencies and EU instances only call EU dependencies. A global circuit breaker (shared state across instances/regions) is usually wrong because: (1) a failure in one region shouldn't trip the breaker for another region, (2) the shared state store becomes a single point of failure, (3) network latency to the state store adds to every request. Per-region is implicit when you have per-instance breakers and region-local dependencies. The one case for centralized state: when you need to coordinate recovery (avoid the synchronized half-open thundering herd across many instances). Even then, jittered wait durations usually solve this without centralized coordination. The staff answer: start per-instance, and only add coordination when you have evidence of the specific failure mode (synchronized recovery) that coordination solves.

---

### Quick-Fire Judgment Calls

These are rapid-fire questions where the interviewer wants a clear recommendation with a one-sentence justification:

**Q21:** Your dependency's p99 is 150ms. What do you set the read timeout to?
→ 300-450ms (2-3x p99). Monitor and adjust. Too short causes false timeouts on healthy services; too long defeats the purpose.

**Q22:** Retry count: 3 retries per request, or 10% retry budget?
→ 10% retry budget. Per-request count doesn't account for global impact. At scale, per-request retries with amplification across layers can multiply load by orders of magnitude.

**Q23:** Circuit breaker trips. Do you page someone?
→ Warning immediately, page if OPEN for more than N minutes (tune N based on the dependency's typical recovery time). A breaker that stays open means the fallback is covering a real failure nobody is investigating.

**Q24:** Full jitter or equal jitter?
→ Full jitter as the default. Equal jitter only when your system cannot tolerate near-zero sleep values (e.g., a retry that fires at 0ms delay effectively becomes an immediate retry, which you might want to avoid for rate-limited APIs).

**Q25:** Your team wants to add retries to a `DELETE /resource/{id}` endpoint. Is this safe?
→ Yes, if the endpoint is idempotent (returns 200 or 204 whether the resource existed or was already deleted). Most REST APIs implement DELETE as idempotent. Verify the implementation — a DELETE that has side effects (cascading deletes, event emission) on every call is not truly idempotent.

**Q26:** You're choosing between Resilience4j in your Java service and Envoy sidecar outlier detection. You need fallbacks that return cached data from a local Redis. Which do you pick?
→ Resilience4j. Envoy can do timeouts, retries, and outlier-based ejection, but it cannot execute application-level fallback logic. The fallback needs to call Redis and construct a domain-specific response — that's application code, not infrastructure policy.

**Q27:** A dependency has been returning 503 for 2 minutes. Your circuit breaker is OPEN. A user's request needs data from that dependency and there is no fallback. What do you return?
→ Fail fast with a clear, machine-readable error (e.g., 503 with a specific error code and a Retry-After header). Do not hang, do not return empty data pretending it's valid, do not retry into a known-broken dependency. Honest, fast errors are better than slow lies.

**Q28:** You notice your circuit breaker is oscillating between OPEN and CLOSED every 30 seconds. What's happening?
→ The dependency is partially recovering during the OPEN window (no traffic gives it breathing room), passes the half-open probes, breaker closes, traffic resumes, dependency fails again under load, breaker reopens. This is the "barely surviving" pattern. The fix: the dependency needs to be scaled up, its root cause fixed, or traffic to it needs to be load-shed at a higher level. The circuit breaker is correctly reflecting the dependency's inability to handle the current load.

---

*Use these questions for self-assessment: if you can answer each with specific numbers, concrete failure scenarios, and clear tradeoff reasoning — not just pattern names — you're operating at the level where you can design and debug resilience strategies in production systems.*

---

## 11. Sandbox Experiments — Run These Yourself

Everything above is assertion until you have watched it happen. This section is a
ladder of experiments, from a single LLM call with an error rate to a composed
defence stack under a provider brownout. Each one gives you a setup, a
**prediction you derive before running it**, and the numbers the run actually
produces.

**Where to run them.** The companion sandbox lives at
`../ai-rag/labs/llm-resilience/simulator.html` — open it directly in a browser,
no server and no dependencies. Steps 1–10 of its **Learn** tab correspond to
experiments 1–7 below; the **Sandbox** tab is experiment 8. The same model runs
headless in Python (`run.py`) if you would rather script the sweeps.

> **What these numbers are.** They are *simulated*, from an invented fixture: a
> chat product calling an LLM API with a plausible-but-fictional latency and
> rate-limit profile. They demonstrate **mechanisms and orders of magnitude**,
> and every one of them is reproducible from a seed. They are not measurements
> of any provider, and none of them should be quoted as one. The *arithmetic* in
> each "Predict" block, however, is exact — it is the part worth carrying to your
> own system.

---

### 11.1 Experiment 1 — The raw failure rate passes straight through

The simplest possible case, and the one everything else is measured against.

```
SETUP
  10 requests/sec for 30 seconds       = 300 requests
  provider returns 500 for 10% of calls
  no retry, no breaker, 3s timeout
```

**Predict.** With no retry, your failure rate *is* their failure rate. Expect
300 × 0.10 = **30 failures**, with a standard deviation of
√(300 × 0.1 × 0.9) = 5.2 — so anything from 20 to 40 is unremarkable.

**Observe.** 300 requests, **38 failures (87% success)**, 300 attempts, 15.5
seconds of wall-clock burned on requests that returned nothing.

**Why it matters.** This is the number a retry has to beat. Note the second-order
point: 38 is 1.5 standard deviations above 30, which is ordinary. If you A/B two
resilience configurations on 300 requests and see a 20% difference, you have
measured noise. Sample-size discipline is part of resilience work.

---

### 11.2 Experiment 2 — Retry converts failures into load, on a known curve

```
SETUP
  same 300 requests, same 10% error rate
  retry up to n attempts, full jitter
```

**Predict.** Retries follow a geometric series. With per-attempt failure
probability `p` and a cap of `n` attempts:

```
  attempts per request  =  1 + p + p² + … + p^(n-1)  =  (1 − pⁿ) / (1 − p)
  residual failure rate =  pⁿ
```

At p = 0.1, n = 3: 1 + 0.1 + 0.01 = **1.11 attempts per request**, so 300
requests should cost **333 attempts**, and 0.1³ = **0.1% of requests still fail**
(0.3 of them).

**Observe.** Mean over 30 seeds: **333.57 attempts, 0.43 failures.** The closed
form is not an approximation — it is what the system does.

The same two formulas across the whole error-rate range:

```
  p      amplification            residual failure rate
         predicted  observed      predicted   observed
  0.05     1.052     1.051          0.0001     0.0003
  0.10     1.110     1.118          0.0010     0.0013
  0.20     1.240     1.252          0.0080     0.0080
  0.30     1.390     1.388          0.0270     0.0240
  0.50     1.750     1.735          0.1250     0.1120
  0.80     2.440     2.430          0.5120     0.4827
```

**Why it matters.** Two things fall out of that table that you can use in a
design review tomorrow:

1. **Amplification is bounded by `1/(1−p)`**, not by your attempt cap. At p = 0.1
   three attempts cost 11% extra load, not 200%. People argue about attempt
   caps as though they were the lever; at low error rates they are nearly free.
2. **The cap stops mattering once p is large.** At p = 0.8, three attempts still
   leave 48% of requests failing while nearly tripling your load. The lever that
   works at low p is worthless at high p — which is exactly the regime an
   incident puts you in.

**Vary it.** Push `p` to 0.5 and watch amplification hit 1.75× while a ninth of
requests still fail. That is the moment retries stop being a fix.

---

### 11.3 Experiment 3 — Independent vs. correlated failures

Experiment 2's arithmetic assumes each attempt is an independent coin flip. Break
that assumption and the whole calculation inverts.

```
SETUP
  5 requests/sec for 40 seconds
  provider returns 500 for EVERY call between t=10s and t=25s
  retry up to n attempts
```

**Predict.** During the outage, `p = 1`, so `pⁿ = 1` — every retry fails too.
Attempts rise linearly with the cap and rescue nothing. Any success you see comes
from requests whose backoff happened to carry them past t=25s, which is a
function of your backoff schedule, not of retrying.

**Observe.**

```
  attempt cap   success   attempts   amplification   wall-clock wasted
      n=1         63%        200         1.00×             30s
      n=2         64%        275         1.38×             76s
      n=3         68%        346         1.73×            127s
      n=5         76%        474         2.37×            271s
```

**Why it matters.** Going from 1 to 5 attempts bought 13 percentage points of
success and cost **9× the wasted time** and 2.4× the load — and the 13 points
came from waiting out the outage, not from retrying. You could have bought the
same thing with one retry and a longer backoff, at a fraction of the load.

This is §2.1's retry amplification with numbers on it. **Retries are an
availability tool when failures are independent (one bad node, one unlucky
request) and a load-amplification tool when they are correlated (a bad deploy, a
saturated dependency, a regional outage).** You do not get to choose which kind
you have; you only get to choose whether your retry policy notices.

**Vary it.** Set the cap to 5 and the base delay to 4s. Success climbs further —
because you are now simply waiting out the outage. That is a *timeout* strategy
wearing a retry costume, and it is much cheaper to implement as one.

---

### 11.4 Experiment 4 — Timeouts, and what a circuit breaker actually saves

The most expensive failure is not an error. It is a dependency that accepts your
connection and then says nothing.

```
SETUP
  4 requests/sec for 40 seconds
  provider latency ×20 between t=10s and t=30s (stalls, does not error)
  3-second timeout
  circuit breaker: trip at 50% failures, 10s window, stay open 3s, 2 probes to close
```

**Predict.** 20 seconds of stall × 4 req/s = **80 requests**, each burning the
full 3s timeout = **240 seconds of connection-time held**, all of it producing
nothing. A breaker should collapse most of that to near-zero, at the cost of
also refusing some requests that would have squeaked through.

**Observe.**

```
                    success   refused instantly   trips   wall-clock wasted
  breaker OFF         49%            0              0          243s
  breaker ON          39%           70              5           84s
```

**Why it matters.** The predicted 240s and the observed 243s agree, which tells
you the model is doing what you think. Now read the trade honestly:

- The breaker cut wasted connection-time by **65%** (243s → 84s). In a real
  service that is thread-pool, connection-pool and file-descriptor pressure that
  no longer propagates to the rest of your system. This is §1.2's cascade,
  prevented.
- It also cost **10 percentage points of success**. An open circuit refuses
  requests indiscriminately, including ones the degraded dependency would have
  served.

**A circuit breaker trades a little availability for a large reduction in
blast radius.** That is a good trade when the alternative is exhausting a shared
resource, and a bad one when the protected path has no fallback and the
dependency is only *partly* sick. If you cannot afford the 10 points, the fix is
to give that path a fallback (§3.5), not to delete the breaker.

**Vary it.** Set `stay open for` to 20s and watch success collapse further — the
circuit is now open long after the stall ended at t=30s. This is why fixed reset
timeouts must be shorter than your typical incident, and why exponential
open-duration backoff (which compounds 3s → 6s → 12s → 24s across a single
brownout) should be opt-in rather than the default.

---

### 11.5 Experiment 5 — 429 is not a failure

One checkbox, and it is the highest value-per-line change in this chapter.

```
SETUP
  6 requests/sec at a provider that allows 4/sec
  provider is otherwise HEALTHY — it answers every call it has quota for
  it returns 429 with a retry-after header for the rest
  circuit breaker on, trip at 30% failures
  the only variable: does a 429 count toward the failure ratio?
```

**Predict.** You are 50% over quota, so roughly a third of calls get a 429. A
third exceeds the 30% trip threshold — so if 429s count, the breaker will open
**on a healthy API**, and then refuse the requests that did have quota.

**Observe.**

```
                      success   refused by breaker   trips   429s seen
  counts 429 = true     47%            86              2        35
  counts 429 = false    75%             0              0       164
```

**Why it matters.** The flag costs **28 percentage points of availability**, and
it costs them against a dependency that was working correctly the whole time. The
mechanism is worth stating precisely because it is counter-intuitive: counting
429s makes the breaker open, which *reduces* the request rate, which is why the
429 count falls from 164 to 35 — the metric that triggered the breaker improves
*because* the breaker is hurting you. Every dashboard looks better; the product
is worse.

The fix is one early return:

```python
def record(self, status):
    if status == 429:        # backpressure, not failure
        return
    ...
```

A circuit breaker exists to detect a **broken** dependency. One that is rate
limiting you is working perfectly and has told you exactly how long to wait.
Quota belongs to a rate limiter (§9.3 step 4); the breaker should never see it.

**Vary it.** Untick the box, then raise your request rate to 12/sec. Success
falls, but the breaker still never trips — because being over quota is not a
dependency failure no matter how far over you are.

---

### 11.6 Experiment 6 — Jitter, measured

```
SETUP
  40 clients, all calling the same provider
  provider fails for 2 seconds (t=5s to t=7s) — a blip, not an outage
  retry with a 2-second base delay
  the only variable: jitter strategy
```

**Predict.** Every client fails inside the same 2-second window and schedules its
retry 2 seconds later. With no jitter all 40 retries land in the same instant.
Equal jitter spreads them over half the window; full jitter over all of it.

**Observe.** Peak attempts in a single 0.25-second bucket:

```
  jitter = none    80 attempts   at t=7.00s
  jitter = equal   59 attempts   at t=9.00s
  jitter = full    52 attempts   at t=8.00s
```

**Why it matters.** Same total load (920 / 954 / 968 attempts — within noise),
completely different *shape*. The no-jitter run delivers a **80-attempt spike**
at a single instant, aimed at a provider that has just demonstrated it is unwell.
Full jitter delivers 52 — a 35% lower peak — for one line of code:

```python
delay = random.uniform(0, min(cap, base * 2 ** attempt))
```

Note that the spike lands at exactly t=7.00s: base delay 2s after the failures
began at t=5s. Synchronisation is not probabilistic, it is arithmetic. Any two
clients that fail in the same second and share a backoff schedule *will* retry in
the same second.

**Vary it.** Raise clients to 60 with jitter off and watch the peak scale
linearly. This is how a brief blip becomes a sustained outage: the spike causes
the next failure, which schedules the next spike.

---

### 11.7 Experiment 7 — A retry budget self-cancels

```
SETUP
  3 requests/sec for 36 seconds
  provider completely down from t=8s to t=28s (a 20-second outage)
  retry up to 3 attempts, full jitter
  the variable: retry budget, as a fraction of successes
```

**Predict.** A budget is refilled by *successes*. During a total outage there are
none, so the budget should drain and retries should stop — automatically, without
anyone changing a config.

**Observe.**

```
  budget      attempts   amplification   gave up early   success
  0 (off)       224          2.07×             0           53%
  0.05          119          1.10×            55           44%
  0.10          120          1.11×            55           44%
  0.25          124          1.15×            54           44%
  1.00          143          1.32×            44           44%
```

**Why it matters.** The budget cut load by **47%** (224 → 120 attempts) at a cost
of 9 points of success — and those 9 points, as Experiment 3 showed, came from
waiting out the outage rather than from retrying. Notice that the exact budget
value barely matters (0.05, 0.10 and 0.25 are within noise of each other): what
matters is that a bound exists at all.

This is the mechanism that makes retries safe to enable by default. A per-call-
site attempt count multiplies load precisely when the dependency can least absorb
it. A budget expressed as a fraction of successes is self-limiting in exactly the
regime where retries cannot help — and stays fully available for the blip they
*are* for.

Envoy implements this natively (`retry_budget`); gRPC has it in the service
config; in Python it is about thirty lines.

---

### 11.8 Experiment 8 — Composing the stack, and the trade nobody mentions

Switch to the **Sandbox** tab. The workload is now a realistic product: every
user turn fans out to 3.11 model calls across three tiers — a safety classifier,
a query rewrite, a streamed answer, an occasional reasoning escalation, a title.
They have different criticalities and different fallbacks.

```
SETUP
  10 user turns/sec (about 31 LLM calls/sec)
  provider incident on the answer tier: fleet at 10%, error rate 55%, t=20s–45s
  compare three configurations on the same seed
```

**Predict.** The defended stack should waste less and generate fewer 529s. Its
effect on *turn completion* is less obvious — think about it before running.

**Observe.**

```
  configuration                        turns answered   wasted spend   529s generated
  naive: 3 retries, one global timeout       60%            $6.20            141
  full defence stack                         28%            $1.95              0
  full stack + a fallback for `answer`       65%            $1.02              0
```

**Why it matters.** Read the first two rows and the stack looks like a
regression. It is not, and the reason is the most important thing in this
chapter.

The breaker correctly detects a broken dependency and refuses traffic in
microseconds. But `answer` is the one call class with `fallback: none` — so every
refusal is a failed turn. The naive client instead waits out the full timeout on
every call and collects whatever the degraded fleet still manages to serve. It
buys availability with **3× the wasted spend and 141 provider-wide 529s** — and
those 529s are not yours alone. You have converted your incident into every
tenant's incident, including your own other call classes.

The third row is the resolution. Give the critical path somewhere to fall back to
— a cache, a cheaper model, a partial answer — and the defended stack beats the
naive one on **every** axis simultaneously. The lesson is not "breakers cost
availability". It is:

> A circuit breaker on a critical path with no fallback trades availability for
> blast-radius containment. If you cannot afford that trade, the answer is to
> build the fallback, not to remove the breaker.

**Vary it.** Reorder the stack. Drag `Retries` above `Client Rate Limiter` and
every attempt now takes a fresh quota reservation. Drag it above `Circuit
Breaker` and you retry into an open circuit. The list is not decoration — each
pattern wraps the ones below it, so the order *is* the semantics.

---

### 11.9 A checklist you can take to a design review

Each row is a number you should be able to produce for your own system, with the
experiment that teaches you how to get it.

| Question | How to answer it | Exp. |
|---|---|---|
| What is our dependency's failure rate, and is it independent or correlated? | Look at whether failures cluster in time. Clustered means retries will not help. | 1, 3 |
| How much load will our retry policy add at that failure rate? | `(1 − pⁿ)/(1 − p)`. Compute it before arguing about attempt caps. | 2 |
| What is our residual failure rate after retries? | `pⁿ`. If it is not low enough, the answer is a fallback, not more attempts. | 2 |
| How much connection-time does a stalled dependency hold? | `arrival_rate × stall_duration × timeout`. This is the cascade budget. | 4 |
| Does our breaker count 429s? | Read the code. This one line is worth ~28 points of availability. | 5 |
| Do we jitter? What is our peak retry concurrency after a blip? | `clients` in one bucket at `t_fail + base`. | 6 |
| What bounds our total retry load during a full outage? | If the answer is "the attempt cap", you have no bound. | 7 |
| Which of our call paths have no fallback? | Those are the ones where a breaker costs availability. Fix the fallback. | 8 |

If you can fill that table in with real numbers for your own dependencies, you
are past pattern-name fluency and into the design work this chapter is about.

---

## References

- Nygard, Michael T. *Release It! Design and Deploy Production-Ready Software*. Pragmatic Bookshelf, 2007 (2nd edition 2018). Origin of the circuit breaker pattern.
- Dean, Jeffrey, and Luiz Andre Barroso. "The Tail at Scale." *Communications of the ACM*, 2013. Hedged requests and tail-tolerant design.
- Google SRE Book, Chapter 22: "Addressing Cascading Failures." Retry budgets, load shedding, and deadline propagation.
- AWS Architecture Blog: "Exponential Backoff And Jitter." Analysis of jitter strategies with simulation data.
- Resilience4j documentation: https://resilience4j.readme.io/. Reference implementation for circuit breaker, retry, bulkhead, and rate limiter patterns.
- gRPC retry and hedging design: https://github.com/grpc/proposal/blob/master/A6-client-retries.md. Declarative retry policies with retry budgets.
