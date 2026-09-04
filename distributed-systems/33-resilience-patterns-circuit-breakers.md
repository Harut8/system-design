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

## References

- Nygard, Michael T. *Release It! Design and Deploy Production-Ready Software*. Pragmatic Bookshelf, 2007 (2nd edition 2018). Origin of the circuit breaker pattern.
- Dean, Jeffrey, and Luiz Andre Barroso. "The Tail at Scale." *Communications of the ACM*, 2013. Hedged requests and tail-tolerant design.
- Google SRE Book, Chapter 22: "Addressing Cascading Failures." Retry budgets, load shedding, and deadline propagation.
- AWS Architecture Blog: "Exponential Backoff And Jitter." Analysis of jitter strategies with simulation data.
- Resilience4j documentation: https://resilience4j.readme.io/. Reference implementation for circuit breaker, retry, bulkhead, and rate limiter patterns.
- gRPC retry and hedging design: https://github.com/grpc/proposal/blob/master/A6-client-retries.md. Declarative retry policies with retry budgets.
