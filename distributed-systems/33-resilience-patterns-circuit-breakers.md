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

## 9. Interview Preparation — Resilience Patterns

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

## References

- Nygard, Michael T. *Release It! Design and Deploy Production-Ready Software*. Pragmatic Bookshelf, 2007 (2nd edition 2018). Origin of the circuit breaker pattern.
- Dean, Jeffrey, and Luiz Andre Barroso. "The Tail at Scale." *Communications of the ACM*, 2013. Hedged requests and tail-tolerant design.
- Google SRE Book, Chapter 22: "Addressing Cascading Failures." Retry budgets, load shedding, and deadline propagation.
- AWS Architecture Blog: "Exponential Backoff And Jitter." Analysis of jitter strategies with simulation data.
- Resilience4j documentation: https://resilience4j.readme.io/. Reference implementation for circuit breaker, retry, bulkhead, and rate limiter patterns.
- gRPC retry and hedging design: https://github.com/grpc/proposal/blob/master/A6-client-retries.md. Declarative retry policies with retry budgets.
