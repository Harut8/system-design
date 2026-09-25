# Chapter 37: Debugging Distributed Systems in Production — Finding Where the Time and the Errors Actually Go

An investigator's playbook for the question senior engineers actually get asked: "why did this
request take 8 seconds when every service says it answered in under 100 ms?" The chapter is method
first (how to run an investigation without guessing), then one section per signal (logs, metrics,
traces, queues, lag, GC, CPU, pools, locks, cascades, network, clocks), each with what the signal
is, how it lies to you, how to measure it with real commands, what the pattern looks like, and the
fix. It ends with worked investigations, public postmortems, sandbox experiments and a one-page
cheat sheet.

This chapter owns the *diagnostic reasoning*. Tooling internals live elsewhere and are linked, not
repeated: OpenTelemetry in [`../sre-observability/02-opentelemetry-deep-dive.md`](../sre-observability/02-opentelemetry-deep-dive.md),
trace storage and sampling in [`../sre-observability/08-traces-storage.md`](../sre-observability/08-traces-storage.md),
profilers in [`../sre-observability/09-profiling.md`](../sre-observability/09-profiling.md), and
incident command in [`../sre-observability/15-incident-response-and-postmortem.md`](../sre-observability/15-incident-response-and-postmortem.md).

Prerequisites: queueing and overload from [`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md),
retries, timeouts and hedging from [`33-resilience-patterns-circuit-breakers.md`](33-resilience-patterns-circuit-breakers.md),
and the failure model from [`00-primitives-and-system-models.md`](00-primitives-and-system-models.md).

---

## Table of Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Why production debugging is its own skill](#1-why-production-debugging-is-its-own-skill)
2. [The method — a repeatable investigation loop](#2-the-method--a-repeatable-investigation-loop)
3. [Latency budget decomposition — where the time hides](#3-latency-budget-decomposition--where-the-time-hides)
4. [Logs, metrics, traces — which tool answers which question](#4-logs-metrics-traces--which-tool-answers-which-question)
5. [Correlation IDs and context propagation](#5-correlation-ids-and-context-propagation)
6. [Distributed tracing as an investigation tool](#6-distributed-tracing-as-an-investigation-tool)
7. [Tail latency — percentiles, histograms, and fan-out](#7-tail-latency--percentiles-histograms-and-fan-out)
8. [Queueing — Little's Law and the time nobody measures](#8-queueing--littles-law-and-the-time-nobody-measures)
9. [Consumer lag — Kafka and other queues](#9-consumer-lag--kafka-and-other-queues)
10. [Replication lag — stale reads that look like bugs](#10-replication-lag--stale-reads-that-look-like-bugs)
11. [GC pauses and runtime stalls](#11-gc-pauses-and-runtime-stalls)
12. [CPU saturation, throttling, and steal](#12-cpu-saturation-throttling-and-steal)
13. [Connection pools](#13-connection-pools)
14. [Thread pools, worker pools, and event-loop blocking](#14-thread-pools-worker-pools-and-event-loop-blocking)
15. [Database contention — slow queries, wait events, N+1](#15-database-contention--slow-queries-wait-events-n1)
16. [Lock contention — row locks, lock queues, advisory locks, mutexes](#16-lock-contention--row-locks-lock-queues-advisory-locks-mutexes)
17. [Cascading failures — retry storms, timeout mismatch, metastability](#17-cascading-failures--retry-storms-timeout-mismatch-metastability)
18. [The network and the host — DNS, TLS, TCP, neighbours, clocks](#18-the-network-and-the-host--dns-tls-tcp-neighbours-clocks)
19. [Worked investigations](#19-worked-investigations)
20. [Production pitfalls](#20-production-pitfalls)
21. [Interview questions](#21-interview-questions)
22. [Real-world cases — public postmortems](#22-real-world-cases--public-postmortems)
23. [Sandbox experiments — run these yourself](#23-sandbox-experiments--run-these-yourself)
24. [Cheat sheet](#24-cheat-sheet)
25. [Key Takeaways](#25-key-takeaways)
26. [Cross-References](#26-cross-references)

---

## Start here — the whole chapter in plain words

**The problem.** A customer reports that checkout took 8 seconds. You open the dashboards: the API
gateway's p99 is 80 ms, the order service's p99 is 95 ms, the pricing service's p99 is 60 ms, and
Postgres says the average query takes 4 ms. Every graph is green. The customer is not lying, and
neither are the graphs. They are answering different questions. Debugging a distributed system in
production is mostly the skill of noticing *which question each number answers* and finding the
time (or the error) that falls between them.

The same gap exists for errors: the mobile app sees 2% failures, every server logs 0.1%. The
missing 1.9% are timeouts the server never knew about, 502s the load balancer generated on its
own, and responses that were computed successfully and then thrown away because the caller had
already given up.

**The running example.** This one request is used throughout the chapter. The numbers are
**illustrative**, chosen to be internally consistent, not measurements from a real system.

```
mobile app ──► edge LB ──► API gateway ──► order-service (pods A, B, ...) ──► Postgres
                                               │
                                               └──► pricing-service  (20 parallel calls)
```

Following one request ID through gateway logs, the trace and pool metrics decomposes the 8.0 s:

| # | Where the time went | Time | Why no service dashboard showed it |
|---|---|---|---|
| 1 | New connection from the phone: DNS + TCP + TLS over a mobile network | 0.25 s | Happens before any server starts a timer |
| 2 | Attempt 1 waits in pod A's worker queue; the gateway's per-try timeout (2.5 s) fires | 2.50 s | The handler timer never started before the gateway gave up. When a worker finally ran it, the handler recorded a *fast* 60 ms and the answer was discarded |
| 3 | Gateway retry backoff (with jitter) | 0.20 s | Internal to the gateway; its own latency metric is per upstream attempt |
| 4 | Attempt 2 waits in pod B's worker queue | 0.60 s | Queue time is outside the handler histogram |
| 5 | Pod B's handler waits for a free DB connection (pool of 10 is exhausted) | 3.90 s | It *is* in the handler histogram, but only ~0.3% of requests hit it, so it lives above p99. The "DB latency" metric starts after the connection is acquired |
| 6 | Fan-out to pricing: 20 parallel calls, the request waits for the slowest | 0.35 s | Pricing's p99 is 60 ms, but the max of 20 calls often lands in pricing's p99.9 |
| 7 | The SQL actually executing | 0.08 s | This is the "the database is fast" number |
| 8 | Serialization plus a GC pause while writing the response | 0.12 s | A GC pause shows up as "slow code" in whatever was running |
|   | **Total seen by the customer** | **8.00 s** | |

The 3.9 s pool wait is only 49% of the total. The rest is queueing, a timeout and a retry,
connection setup and fan-out: five different mechanisms, none of which is "a slow service".

**What the dashboards were actually saying.**

- **"p99 = 95 ms"** means 99% of requests were faster. It says nothing about the worst 1%, and this
  request was in the worst 0.3%. Look at p99.9 and at max, and read one slow request end to end.
- **"Handler latency"** starts when a worker picks the request up. Time spent in the kernel accept
  queue, the load balancer, or the worker queue is outside it (§3, §8).
- **Attempt 1 counted as a fast request.** Pod A really did answer in 60 ms, three seconds late,
  to a caller that had left. Retries turn one slow user request into several fast-looking server
  requests (§17).
- **"DB query 4 ms"** is execution time. Waiting for a connection or a lock is not execution (§13, §16).
- **Fan-out makes a rare tail common**: at 20 calls per request, `1 − 0.99^20 ≈ 18%` of requests
  wait for at least one call that is slower than the callee's p99 (§7).

**How you would have found it.** You would not find it from dashboards alone. You take the
request ID from the customer's support ticket or the client log (§5), pull every log line and the
trace for that ID (§4, §6), see two gateway attempts with an upstream-timeout flag, see a 3.9 s gap
before the first SQL span, and check the pool's pending-connections metric on pod B for that
minute (§13). Then you ask *why* the pool was exhausted, and the answer is usually another
section of this chapter: a slow query holding connections (§15), a lock (§16), or more pods than
the database can serve (§13).

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Latency | time from asking to getting an answer | time from ordering coffee to holding it |
| Percentile (p99) | the value 99% of requests are faster than | "99 of 100 customers got their coffee within 4 minutes" |
| Tail latency | the slowest few percent of requests | the customers whose order was lost behind the counter |
| Server time vs client time | what the server measured vs what the user waited | barista's "made it in 90 s" vs your 10 minutes in the queue |
| Queueing | waiting for a worker before any work starts | the line in front of the counter |
| Little's Law | people in line = arrival rate × time each spends | 2 customers/min × 5 min each = 10 in the shop |
| Utilization | fraction of time a resource is busy | how much of the hour the barista is making drinks |
| Saturation | work waiting because the resource is full | the length of the line |
| Correlation / request ID | one ID stamped on everything a request touches | an order number on the cup, the receipt and the ticket |
| Trace / span | a timeline of one request; each timed step is a span | a delivery tracking page with each hop and its time |
| Head vs tail sampling | decide to keep a trace at the start vs after it finished | keeping every 100th receipt vs keeping every receipt for a complaint |
| Connection pool | a fixed set of reusable connections to a dependency | a company's 10 pool cars; the 11th driver waits |
| Consumer lag | how far a reader is behind the newest message | unread emails piling up |
| Replication lag | how far a copy of the database is behind the original | a branch office with yesterday's price list |
| GC pause | the runtime freezes to clean up memory | the shop closes for five minutes to empty the bins |
| CPU throttling | the kernel stops your container because it used its CPU allowance | a metered parking spot that tows you when time runs out, even if the lot is empty |
| Lock contention | work waiting for another piece of work to release something | a single bathroom key at a petrol station |
| Retry storm | failures cause retries that cause more failures | everyone redialling a busy phone line at once |
| Metastable failure | the system stays broken after the trigger is gone | a traffic jam that persists long after the accident is cleared |
| Coordinated omission | a measuring tool that stops measuring during the slow part | a speed camera that only photographs cars when the road is clear |
| Blast radius | which users, regions, versions are affected | which floors of the building lost power |
| USE / RED | checklists: Utilization-Saturation-Errors for resources; Rate-Errors-Duration for services | a mechanic's inspection sheet |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| `λ` | arrival rate | 10 – 100,000 req/s | 150 req/s into one pod |
| `S` | service time: time a worker actually spends on one request | 1 ms – 1 s | 40 ms of handler work |
| `μ` | service rate of one worker `= 1/S` | — | 1 / 40 ms = 25 req/s |
| `c` | number of parallel servers: workers, threads, connections | 1 – 1,000 | 8 Gunicorn workers |
| `ρ` | utilization `= λ / (c·μ) = λ·S / c` | 0 – 1 | 150 × 0.04 / 8 = 0.75 |
| `L`, `Lq` | average number in the system / waiting in queue | — | 20 requests in flight |
| `W`, `Wq` | average time in the system / waiting in queue | — | 0.8 s total, 0.76 s queued |
| `L = λ·W` | Little's Law | — | 150 req/s × 0.8 s = 120 in flight |
| `T_client` | latency the caller observes | — | 8.0 s |
| `T_handler` | latency the server's handler histogram records | — | 60 ms |
| `pXX` | XXth percentile of a latency distribution | p50, p95, p99, p99.9 | p99 = 95 ms |
| `N` | fan-out width: parallel calls per request | 1 – 1,000 | 20 pricing calls |
| `p` | probability one call is "slow" (above some threshold) | 0.1% – 5% | 1% (above the callee's p99) |
| `1 − (1 − p)^N` | probability at least one of `N` calls is slow | — | 1 − 0.99^20 ≈ 18% |
| `A`, `R`, `k` | attempts per layer `= 1 + R` retries; `k` retrying layers | A = 2 – 4, k = 1 – 4 | 3 attempts × 3 layers = 27 calls |
| `lag_offsets` | log-end offset − committed offset, per partition | 0 – millions | 120,000 messages |
| `lag_seconds` | age of the oldest unprocessed message | 0 – hours | 60 s |
| `replay_lag` | time a replica is behind its primary | ms – minutes | 12 s during a batch job |
| `period`, `quota` | CFS bandwidth: CPU time allowed per period | 100 ms; limit × 100 ms | 2 CPUs → 200 ms per 100 ms |
| `nr_periods`, `nr_throttled` | periods observed / periods in which the cgroup was throttled | counters | 12,600 / 36,000 = 35% |
| `throttled_usec` | total time the cgroup was not allowed to run (cgroup v2) | counter, µs | 1,134 s in an hour |
| `pool_size` | max connections a client pool holds | 5 – 100 | HikariCP default 10 |
| `hold_time` | how long one request holds a connection | 1 – 100 ms | 25 ms |
| `RTT` | network round-trip time | 0.1 ms (same AZ) – 250 ms | 70 ms cross-region |
| `RTO_min` | Linux minimum TCP retransmission timeout | 200 ms | a lost segment costs ≥ 200 ms |
| initial SYN RTO | wait before re-sending a lost SYN | 1 s | lost SYNs add 1 s, then 3 s total |
| `θ` | clock offset between two hosts | µs – seconds | +300 ms on one node |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. Why production debugging is its own skill

> **In plain words.** On one machine, a debugger stops time and shows you everything. In a fleet,
> nothing stops, every machine has its own clock and its own partial view, and the thing that went
> wrong is usually the *interaction* between healthy parts. You debug with evidence collected in
> advance (logs, metrics, traces) and with experiments you can run on a live system without
> hurting it.
>
> **Real-world example.** A payments team spends two days tuning a query that the dashboard calls
> "slow" (p99 400 ms). The real problem was that 60% of each checkout's time was spent waiting for
> a database connection. Tuning the query helped a little because connections were held for less
> time; adding one span around pool checkout would have shown the answer in ten minutes.

### 1.1 What changes when the system is distributed

- **Partial failure is the normal state.** Something is always degraded somewhere. The question is
  never "is anything broken" but "is *this* broken thing the one hurting users".
- **Every measurement is local.** A server measures from when it saw the request to when it
  finished writing the response. The caller measures from when it decided to call to when it had
  the answer. The difference between them is network, queues, proxies, retries and connection
  setup, and nobody owns it.
- **There is no global clock.** Timestamps from two hosts can disagree by milliseconds (or seconds,
  on a broken node), so ordering events across machines by wall-clock time can show effects before
  causes (§18.5).
- **The system reacts to your observation and your fix.** Restarting a pod clears its queue and its
  evidence. Adding capacity during a retry storm can make it worse. Turning on debug logging can
  itself cause the latency you are chasing.
- **Most incidents are interactions, not bugs.** A retry policy that is correct, a pool size that
  is correct, and a deploy that is correct can together produce an outage (§17). The code review
  for each part passed.

### 1.2 The two questions this chapter answers

1. **Where did the time go?** Latency is conserved: every millisecond the user waited was spent
   *somewhere*. If the parts you measured add up to less than the whole, the difference is in a
   place you did not measure, and §3 lists those places.
2. **Where did the errors come from?** Errors are *not* conserved: they are created at one layer,
   transformed at others (a timeout becomes a 504 becomes a retry becomes a success), and hidden by
   sampling. You find them by comparing error rates at each layer for the same traffic.

---

## 2. The method — a repeatable investigation loop

> **In plain words.** Most wasted incident time comes from jumping to a favourite theory ("it's
> the database again"). A method keeps you honest: pin down exactly what is wrong, find what the
> affected requests have in common, check what changed, follow one bad request all the way
> through, check every resource on its path, and only then test theories, trying to prove each one
> *wrong*. Stop the bleeding before you understand it, and write down what you learned.
>
> **Real-world example.** "The site is slow" becomes "checkout p99 measured at the edge went from
> 400 ms to 8 s at 14:02 UTC, only for requests routed to eu-west-1b, only for app version 5.3,
> errors unchanged". That sentence already rules out most theories.

### 2.1 The loop

```
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │ 1. Define the symptom precisely (which SLI, which percentile, since when)    │
 │ 2. Scope the blast radius by dimension (region, AZ, version, tenant, route)  │
 │ 3. Ask what changed (deploys, config, flags, traffic, dependencies, data)    │
 │ 4. Follow ONE slow/failed request end to end; compare with a good one        │
 │ 5. Sweep resources with USE and services with RED along that path            │
 │ 6. Form hypotheses; for each, state what evidence would FALSIFY it; test     │
 │ 7. Mitigate before root cause (after grabbing perishable evidence)           │
 │ 8. Write it down; turn the finding into a panel, an alert, or a guardrail    │
 └───────────────────────────────▲──────────────────────────────┬───────────────┘
                                 └──── new evidence ◄───────────┘
```

**Step 1 — Define the symptom precisely.** Write one sentence with: the SLI (latency, errors,
freshness, correctness), the percentile, *where* it was measured (client, edge, service), the start
time in UTC, and whether it is still happening. "Users complain it's slow" is not a symptom.
"Edge p99 for `POST /checkout` rose from 400 ms to 8 s at 14:02 UTC and is still elevated; error
rate unchanged" is. Measure as close to the user as you can (edge logs, RUM, synthetic probes); a
server-side metric is a hypothesis about user experience, not a measurement of it.

**Step 2 — Scope the blast radius by dimension.** Break the bad metric down by every label you
have and look for the one that concentrates it:

| If the problem concentrates in… | Suspect first |
|---|---|
| one region / AZ | infrastructure: network, a zonal dependency, a node pool, a LB |
| one node or a few pods | host problems: noisy neighbour, throttling, bad disk, GC on that instance |
| one app version | the deploy |
| one tenant / customer | data shape: a hot key, a huge account, an unusual query plan |
| one endpoint | that code path or its specific dependency |
| one client version / user agent | a client change (retry policy, new call pattern) |
| everything, uniformly | a shared dependency (DB, cache, DNS, auth) or a traffic change |

```promql
# Which zone owns the slow requests? Compare p99 per zone.
histogram_quantile(0.99,
  sum by (le, zone) (rate(http_server_request_duration_seconds_bucket{route="/checkout"}[5m])))

# Which version owns the errors? Share of 5xx per version.
sum by (version) (rate(http_server_request_duration_seconds_count{route="/checkout", status=~"5.."}[5m]))
  / sum by (version) (rate(http_server_request_duration_seconds_count{route="/checkout"}[5m]))
```

A dimension you did not record cannot be sliced. That is the argument for putting zone, version
and a coarse tenant tier on request metrics, and high-cardinality attributes (tenant ID, user ID)
on traces and logs rather than on metrics (cardinality: [`../sre-observability/18-cardinality-and-cost.md`](../sre-observability/18-cardinality-and-cost.md)).

**Step 3 — Ask what changed.** Most incidents follow a change. List every change in the window
from one hour before the start until now: application deploys, config pushes, feature flags,
infrastructure changes (node pools, kernel, sidecar versions), dependency deploys (ask the other
teams, or read their deploy channel), certificate rotations, traffic changes (a marketing push, a
new client release, a batch job), data growth (a table crossed a size where the planner switched
plans), and time-based triggers (cron, TTL expiry, month end, DST).

```bash
kubectl rollout history deployment/order-service -n shop       # app deploys
helm history order-service -n shop                             # chart-level changes
kubectl get events -A --sort-by=.lastTimestamp | tail -50      # node, pod and scaling churn
git log --since="3 hours ago" --oneline -- deploy/ config/     # config-as-code changes
```

Dashboards with deploy annotations turn this step from an archaeology project into a glance.

**Step 4 — Follow one slow request end to end.** Aggregates tell you *that* something is slow;
one request tells you *where*. Get a request ID or trace ID for a request that was slow (from an
exemplar on the latency histogram, a trace search for `duration > 2s`, a support ticket, or the
edge access log), then pull everything for that ID from every service. Then do the same for a
fast request of the same kind and compare them side by side: differential diagnosis. The
difference between a 200 ms and an 8 s checkout is the investigation.

**Step 5 — Sweep resources with USE and services with RED.** For every resource on the slow
request's path (CPU, memory, disk, network, connection pools, thread pools, locks, queues), check
**U**tilization, **S**aturation (something waiting) and **E**rrors. For every service, check
**R**ate, **E**rrors, **D**uration. Saturation is the column people skip and the one that explains
most latency: a pool with 10/10 connections in use and 40 waiters is the answer; its utilization
of "100%" alone is not. (USE and RED in depth: [`../sre-observability/00-mental-models.md`](../sre-observability/00-mental-models.md) §4.)

**Step 6 — Form hypotheses and try to falsify them.** Write each hypothesis down with a
prediction that would prove it *wrong*, then test the cheapest one first.

| Hypothesis | Prediction if true | Falsified if |
|---|---|---|
| DB pool exhaustion on order-service | `pending` connections > 0 only on affected pods; acquire time ≈ the gap in the trace | pending is 0 during slow requests |
| GC pauses | pause timestamps in GC logs line up with the slow requests to within ms | slow requests occur with no pause in progress |
| CPU throttling | `nr_throttled` rising on affected pods; slowness correlates with bursts | throttled ratio ≈ 0 |
| A deploy | only the new version is slow; rollback fixes it | old version equally slow |
| Hot partition | lag / latency concentrates on one key or partition | spread evenly |

Two traps. First, in an incident *everything* moves: latency up, CPU up, errors up, queue depth
up. Most of those are effects. The signal that moved **first** is usually closest to the cause,
so line up time series at one-second resolution if you have it. Second, confirmation bias: once
you have a theory you will find supporting evidence for it. Only evidence that could have
disproved the theory counts.

**Step 7 — Mitigate before root cause.** Users do not need you to understand the problem; they
need it to stop. Roll back, drain the bad zone, disable the flag, shed load, kill the blocking
query, scale out (if the bottleneck scales). But mitigation destroys evidence (a restart empties the
queue and the heap), so spend 30–60 seconds grabbing perishable evidence first:

```bash
kubectl exec order-service-7d9f-abcde -- py-spy dump --pid 1 > dump-$(date +%s).txt  # Python stacks
kubectl exec order-service-7d9f-abcde -- jcmd 1 Thread.print > threads.txt           # JVM threads
curl -s localhost:6060/debug/pprof/goroutine?debug=1 > goroutines.txt                # Go goroutines
psql -c "COPY (SELECT now(), * FROM pg_stat_activity) TO STDOUT CSV HEADER" > activity.csv
kubectl logs order-service-7d9f-abcde --previous > previous.log                      # crashed container
```

(`py-spy` needs `SYS_PTRACE` in the container; details in
[`../sre-observability/42-python-observability.md`](../sre-observability/42-python-observability.md) §7.)

**Step 8 — Write it down.** Timeline, evidence (links to the exact queries and traces), what fooled
you, and the fix. The action item that pays back most often is a new panel or alert for the
signal you had to hunt for: pool pending, queue age, throttle ratio, lag in seconds. Postmortem
structure: [`../sre-observability/15-incident-response-and-postmortem.md`](../sre-observability/15-incident-response-and-postmortem.md) §11–§13.

### 2.2 The first 15 minutes

| Minute | Do | Output |
|---|---|---|
| 0–2 | Confirm the symptom at the edge (edge logs, RUM, synthetic check). Note start time in UTC | one-sentence symptom |
| 2–5 | Slice by zone, version, pod, route, tenant tier; is it one slice or everything? | blast radius |
| 5–7 | List changes in the last hour: deploys, flags, config, dependency deploys, traffic | suspect list |
| 7–9 | Get one slow request ID or trace; read its waterfall and its gateway access-log line(s) | where time goes for one request |
| 9–11 | Grab perishable evidence: stack dump on one bad pod, `pg_stat_activity` snapshot | saved files |
| 11–13 | Saturation sweep: CPU and throttle ratio, pool pending, queue depth / lag, DB wait events, LB response flags, retry rate | the saturated resource, if any |
| 13–15 | Pick a mitigation that is reversible (rollback, drain, flag off, shed); announce it | mitigation in flight |

Things *not* to do in the first 15 minutes: restart everything (evidence gone, and cold caches can
make it worse), raise every timeout (converts errors into slowness and holds more resources),
add retries (§17), or scale the database connection pool up (§13).

### 2.3 Latency or errors: same method, different conservation law

For latency, add up the parts: the missing time is somewhere (§3). For errors, compare rates layer
by layer for the same traffic:

```
client error rate  ≥  edge/LB error rate  ≥  service error rate  ≥  dependency error rate
       2.0%                 1.2%                    0.1%                   0.1%
         │                    │                       │
         │                    │                       └─ errors the app raised and logged
         │                    └─ + LB-generated 502/503/504 (no healthy upstream, upstream reset,
         │                         upstream timeout) that never reached application logs
         └─ + client-side timeouts, DNS/TLS failures, connection resets, requests that the
              server finished after the client gave up (nginx logs these as status 499)
```

Where the rate jumps between two adjacent layers is where the errors are created. The proxy's
response flags usually name the mechanism: in Envoy access logs `UT` is upstream request timeout,
`UF` upstream connection failure, `UO` upstream overflow (circuit breaker), `URX` retry limit
exceeded, `UH` no healthy upstream, `DC` downstream (client) closed the connection.

---

## 3. Latency budget decomposition — where the time hides

> **In plain words.** A request passes through many places before and after the code you wrote
> runs: the phone's DNS lookup, the TLS handshake, a load balancer, a queue in the kernel, a queue
> in your web server, a wait for a database connection, a garbage-collection pause, a retry. Your
> handler's timer only covers the middle. When the user's wait is much bigger than the handler's
> number, the difference is in one of these places, and each one has a way to measure it.
>
> **Real-world example.** A Django service shows 50 ms handler time while nginx logs 1.4 s
> `$request_time` for the same requests. `$upstream_response_time` is also 1.4 s, so the time is
> between nginx and Django's view: all Gunicorn workers were busy and requests sat in the listen
> backlog. `ss -ltn` shows Recv-Q at 900 on port 8000.

### 3.1 The timeline of one request

```
T_client (what the user feels) ─────────────────────────────────────────────────────────────►
│DNS│TCP│TLS│ send │ net │ LB queue │ LB→pod conn │ SYN/accept q │ worker q │ HANDLER │ write │ net │ parse │
                                                                            ▲         ▲
                                                             handler timer starts    ends
                                                             (T_handler, the "server time")

inside HANDLER:
│ middleware │ pool acquire │ lock wait │ query │ GC pause │ outbound call ─► (same timeline again, recursively) │

around everything:  per-try timeout ─► backoff ─► attempt 2 ─► ... (retries at every layer multiply this)
```

Written as an equation for one layer:

```
T_client = T_connect                                    (DNS + TCP + TLS, only for new connections)
         + Σ over attempts [ T_net + T_lb + T_queue + T_handler + T_write ]
         + Σ backoffs
         + T_client_processing                          (deserialization, client GC, rendering)

T_handler = T_middleware + T_pool_wait + T_lock_wait + T_cpu + T_gc + T_throttled + Σ T_downstream
```

The server's dashboard shows `T_handler` of each attempt as a separate sample. Everything else in
the first line is invisible to it.

### 3.2 Every place time hides, and how to measure it

| Hiding place | Typical size when it goes wrong | Who can see it | How to measure |
|---|---|---|---|
| DNS resolution | 5 s steps (resolver timeout), 10–100 ms of search-list misses | client only | `curl -w '%{time_namelookup}'`; CoreDNS latency histogram; §18.1 |
| TCP connect | 1 s / 3 s steps when SYNs are dropped | client, `ss` | `%{time_connect}`; `nstat` SYN retransmits; §18.3 |
| TLS handshake | 1–2 RTT + CPU; bad after deploys when every connection is new | client, proxy | `%{time_appconnect} − %{time_connect}`; §18.2 |
| Client-side pool / concurrency limit | unbounded | client only | pool pending metric in the *caller*; HTTP/1.1 browsers allow 6 connections per host |
| Load balancer / proxy queue | ms – seconds | proxy | Envoy `upstream_rq_pending_active`, `%DURATION%` vs `%UPSTREAM_SERVICE_TIME%`; nginx `$request_time − $upstream_response_time` |
| Kernel SYN and accept queue | ms – seconds, then drops | host | `ss -ltn` Recv-Q vs Send-Q on the listen socket; `nstat` `ListenOverflows` |
| Worker / thread queue | ms – seconds | server, if instrumented | queue-time header (`X-Request-Start`), executor queue size metrics; §8, §14 |
| Reading a slow client's request body | seconds on mobile | server | time from accept to "body complete"; buffering proxy in front |
| Pool acquire (DB, HTTP) | ms – pool timeout (often 30 s) | server, if instrumented | HikariCP `hikaricp_connections_acquire_seconds`, Go `DBStats.WaitDuration`; §13 |
| Lock wait | ms – minutes | DB, app profiler | `pg_stat_activity.wait_event_type = 'Lock'`; mutex profiles; §16 |
| GC / runtime pause | ms – seconds | runtime logs | GC logs, `jvm_gc_pause_seconds`, `go_gc_duration_seconds`; §11 |
| CPU throttling / run-queue wait | tens of ms per 100 ms period | cgroup, kernel | `cpu.stat` `nr_throttled`, PSI `cpu.pressure`; §12 |
| Downstream fan-out | the slowest of N | trace | critical path in the waterfall; §6, §7 |
| Writing the response to a slow client | seconds | server, proxy | proxy buffering; time-to-last-byte vs time-to-first-byte |
| Retries and backoff | multiples of the per-try timeout | caller, proxy | Envoy `upstream_rq_retry`; attempt number on spans and logs; §17 |
| Clock skew (measurement artefact) | negative or impossible gaps | none directly | `chronyc tracking`; §18.5 |

### 3.3 Measuring the gap with tools you already have

**From the client side**, `curl` splits a request into phases. Run it from inside the caller's
network namespace (the pod) to see what the caller sees:

```bash
curl -s -o /dev/null -w \
'dns=%{time_namelookup} connect=%{time_connect} tls=%{time_appconnect} ttfb=%{time_starttransfer} total=%{time_total}\n' \
https://orders.internal/healthz
# dns=0.004 connect=0.005 tls=0.019 ttfb=0.412 total=0.413
# The times are cumulative from the start: TLS took 0.019 − 0.005 = 14 ms; the server took
# ttfb − tls ≈ 393 ms to send its first byte.
```

**At a proxy**, compare total time with upstream time. In nginx:

```nginx
log_format timing '$remote_addr "$request" $status rt=$request_time '
                  'uct=$upstream_connect_time uht=$upstream_header_time urt=$upstream_response_time '
                  'rid=$request_id';
# rt  - urt  large → time spent with the client (slow upload/download) or before proxying
# urt - app handler time large → network, upstream accept/worker queue, or upstream retries
# uct large → connecting to the upstream (SYN drops, TLS to upstream, exhausted keepalive pool)
```

In Envoy, `%DURATION%` is the whole request, `%RESPONSE_DURATION%` is time to the first upstream
response byte, and `%RESP(X-ENVOY-UPSTREAM-SERVICE-TIME)%` is the upstream's time as measured by
the upstream Envoy (in a mesh). A large difference between the caller's sidecar and the callee's
sidecar is network or the callee's inbound queue.

**Queue time in the application** needs a timestamp from before the queue. The convention is a
header set by the first proxy and read by the app:

```nginx
proxy_set_header X-Request-Start "t=${msec}";   # seconds since epoch, millisecond resolution
```

```python
import time

def queue_time_seconds(headers) -> float | None:
    raw = headers.get("x-request-start", "")
    if not raw.startswith("t="):
        return None
    return max(0.0, time.time() - float(raw[2:]))   # cross-host: only as good as clock sync (§18.5)
```

Export it as a histogram next to the handler histogram. When handler p99 is flat and queue-time
p99 is climbing, you are saturated (§8).

**Inside the handler**, the gap between the parent span and its children is un-instrumented time
(§6.2). The cheapest high-value instrumentation is a span or histogram around pool checkout.

### 3.4 Worked decomposition: the running example with the evidence for each line

| Segment | Time | The evidence that shows it |
|---|---|---|
| Connection setup | 0.25 s | Client log / RUM resource timing: `connectStart→secureConnectionStart→requestStart` |
| Attempt 1 in pod A's queue | 2.50 s | Gateway log: attempt 1 status 504, flag `UT`, upstream time 2.5 s. Pod A access log for the same request ID shows it *started* 2.9 s after the gateway sent it, 0.4 s after the gateway had given up |
| Backoff | 0.20 s | Gateway log: attempt 2 start − attempt 1 end |
| Attempt 2 in pod B's queue | 0.60 s | `X-Request-Start` queue-time histogram on pod B; gap between the gateway's client span and pod B's server span |
| Pool wait | 3.90 s | Trace: 3.9 s gap between the server span start and the first DB span; `hikaricp_connections_pending` = 38 on pod B that minute |
| Pricing fan-out | 0.35 s | Trace: 20 parallel client spans, the slowest 350 ms (a throttled pricing pod) |
| SQL | 0.08 s | DB spans |
| Serialize + GC | 0.12 s | GC log on pod B: 90 ms young-gen pause at the matching timestamp |

The fix is not in any one team's code: the gateway's per-try timeout (2.5 s) was shorter than
order-service's pool timeout (30 s default), so the gateway abandoned requests that order-service
kept processing; order-service ran 64 worker threads against a 10-connection pool, so threads piled up waiting for connections and new requests piled up waiting for threads; and nothing
rejected requests that had already waited longer than the caller's deadline (§17.2, §19.1).

---
## 4. Logs, metrics, traces — which tool answers which question

> **In plain words.** Metrics are cheap counters and timers: great for "is something wrong, how
> much, since when, where". Traces are timelines of individual requests: great for "where did
> *this* request spend its time". Logs are detailed notes: great for "what exactly happened to
> this request and why". Each one lies in its own way, and the investigation usually moves from
> metrics to traces to logs.
>
> **Real-world example.** An alert says checkout errors are at 3% (metric). Slicing shows only the
> new version (metric). A failed trace shows the error is in the call to the tax service (trace).
> The tax service log for that request ID says `KeyError: 'region'` because the new client stopped
> sending a field (log).

### 4.1 Which signal for which question

| Question | Best first tool | Why |
|---|---|---|
| Is something wrong? How bad? Since when? | metrics | cheap, complete (not sampled), long retention |
| Which region / version / route / pod? | metrics with labels | slice and compare in seconds |
| Which tenant / user / payload shape? | traces or wide structured events | high cardinality is expensive in metrics |
| Where did this one request spend its time? | trace | shows the call tree, parallelism and gaps |
| What exactly happened, with what inputs, and which error? | logs, joined by request ID | full detail at the point of failure |
| Which code is using the CPU / allocating memory? | continuous profiles | [`../sre-observability/09-profiling.md`](../sre-observability/09-profiling.md) |
| What is the database doing right now? | the database's own views | §15, §16 |
| What is a stuck process doing right now? | a stack dump | `py-spy dump`, `jcmd Thread.print`, goroutine dump |

### 4.2 Logs

**What they are.** Discrete events with context. For debugging, only structured logs (one JSON
object per line) are worth the money: you can filter and aggregate on fields instead of regexes.

**What to log at boundaries.** Most useful logging happens where a request crosses a boundary:

- **Inbound, one "canonical" line per request at the end** (the pattern Stripe calls canonical log
  lines): route template (not raw path), status, duration, queue time, request ID, trace ID,
  caller identity, tenant, attempt number, bytes in/out, and the counters you accumulated
  (DB calls, DB time, pool wait, cache hits). One wide line answers most questions without joins.
- **Inbound, a start line for anything that can run long** (jobs, streaming responses, long
  polls). A request that hangs forever never writes its end line (see "survivorship" below).
- **Outbound, one line per attempt** for calls to other services: dependency, operation, attempt
  number, timeout used, duration, outcome, and pool wait.
- **State transitions**: circuit breaker opened, pool resized, consumer rebalanced, leader changed,
  config reloaded. These are rare and extremely valuable during an incident.

```json
{"ts":"2026-09-25T14:02:07.412Z","level":"info","msg":"request","service":"order-service",
 "route":"POST /checkout","status":200,"duration_ms":4450,"queue_ms":600,"pool_wait_ms":3903,
 "db_calls":7,"db_ms":81,"attempt":2,"request_id":"7c1e9a40d2","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736",
 "tenant_tier":"enterprise","zone":"eu-west-1b","version":"5.3.0","pod":"order-service-7d9f-q2x8k"}
```

**Levels and sampling.** Keep levels meaningful: `ERROR` means a human may need to act, `WARN`
means something degraded but was handled, `INFO` is the canonical line and state transitions,
`DEBUG` is off in production (or enabled per request via a header or baggage flag). At volume,
sample *successes*, never errors, and always keep slow requests:

```python
import random

def should_log(status: int, duration_ms: float, slow_ms: float = 1000, rate: float = 0.05) -> bool:
    return status >= 500 or duration_ms >= slow_ms or random.random() < rate
```

**How logs lie.**

- **Survivorship.** Most access logs are written when the response is sent. Requests that hang,
  or whose worker is killed on timeout (Gunicorn's `WORKER TIMEOUT`), may never be logged. An
  incident where the log volume *drops* is often worse than one where errors rise.
- **Sampling and drops.** Head-sampled or rate-limited logs silently lose the interesting lines.
  Async appenders configured to drop on a full buffer (a common default for non-blocking loggers)
  discard logs exactly when the system is overloaded.
- **Timestamps.** A log timestamp is when the line was *written* (often the end of the request),
  on *that host's* clock. Ordering lines from two hosts by timestamp can put effects before causes
  (§18.5).
- **Aggregation by the eye.** Grepping a log for "timeout" and finding 400 lines means nothing
  without the denominator. Convert to a rate.

**How to query.** Examples in LogQL (Loki):

```logql
# slow checkouts, with the field that explains them
{service="order-service"} | json | route="POST /checkout" and duration_ms > 2000
  | line_format "{{.request_id}} {{.duration_ms}} pool={{.pool_wait_ms}} queue={{.queue_ms}}"

```

More recipes: [`../sre-observability/appendix-c-query-recipe-book.md`](../sre-observability/appendix-c-query-recipe-book.md).

### 4.3 Metrics

**What they are.** Counters (monotonic totals), gauges (current values) and histograms (counts of
observations per bucket), aggregated in the process and scraped or pushed every 10–60 s.

**How metrics lie.**

- **Averages hide everything.** Mean latency is dominated by nothing in particular. Average CPU
  over a minute can be 40% while the process was 100% busy for 24 of those 60 seconds.
- **Windows smooth spikes.** `rate(x[5m])` spreads a 20 s stall across five minutes. A scrape
  interval of 30 s cannot show a 2 s event except as a tiny bump.
- **Gauges are samples.** A queue-depth gauge scraped every 15 s shows the depth at those instants,
  not the peak between them. Export a max-since-last-scrape or a histogram when peaks matter.
- **Pre-computed quantiles do not aggregate.** A summary's p99 from ten pods cannot be combined into
  a fleet p99 (§7.2).
- **Bucket bounds clip.** If the largest finite histogram bucket is 1 s, an 8 s request is counted
  as "more than 1 s". When a quantile falls in the `+Inf` bucket, Prometheus's `histogram_quantile`
  returns the upper bound of the highest finite bucket, so p99 is reported as 1 s while users wait 8.
- **Only completed requests are counted.** Most latency histograms are observed when the request
  finishes. Requests stuck in progress show up only as a gap in the rate. Track an in-flight gauge.
- **The dimension you need is missing.** If the metric has no `zone` label, the zonal problem is
  diluted into the fleet average.

**Useful PromQL for investigations.**

```promql
# p99 latency by route, from a histogram (correctly aggregated across pods)
histogram_quantile(0.99, sum by (le, route) (rate(http_server_request_duration_seconds_bucket[5m])))

# fraction of requests slower than 500 ms (no interpolation error; use this for SLOs)
1 - (sum(rate(http_server_request_duration_seconds_bucket{le="0.5"}[5m]))
     / sum(rate(http_server_request_duration_seconds_count[5m])))

# which pods are worst right now (look for outliers, not the average)
topk(5, histogram_quantile(0.99, sum by (le, pod) (rate(http_server_request_duration_seconds_bucket[1m]))))

# short windows during an incident: 1m rates at a 15 s scrape still have 4 samples
sum by (status) (rate(http_server_request_duration_seconds_count[1m]))
```

The metric names above follow the OpenTelemetry HTTP semantic conventions
(`http.server.request.duration`, exported to Prometheus as `http_server_request_duration_seconds`).
Your framework may use `http_request_duration_seconds` or something else; the shape is the same.

**Exemplars** attach a trace ID to a histogram observation, so a click on the p99 line of a
latency panel opens an actual slow trace. They are the fastest bridge from "p99 is up" to step 4
of the method (§2). Exemplars in storage and Grafana:
[`../sre-observability/08-traces-storage.md`](../sre-observability/08-traces-storage.md) §12.

### 4.4 Traces

**What they are.** A tree of timed spans for one request across services, joined by a trace ID
that travels with the request (§5). §6 covers reading them. Their main lies: sampling keeps the
wrong requests (§6.4), un-instrumented work appears as empty space (§6.2), and spans from
different hosts are placed on one timeline using clocks that may disagree (§18.5).

### 4.5 Putting them together

A typical investigation moves: **alert (metric) → slice (metric) → exemplar or trace search
(trace) → the span with the problem (trace) → the logs for that request ID in that service (log)
→ the resource metric that explains it (metric: pool pending, throttling, lag) → the stack or
database view that explains *that* (profile, `pg_stat_activity`)**. Each hop needs a shared key:
request ID or trace ID in logs, exemplars on histograms, and consistent `service`, `pod`, `zone`,
`version` labels on all three. Designing that correlation: [`../sre-observability/01-architecture-and-stack.md`](../sre-observability/01-architecture-and-stack.md)
and [`../sre-observability/34-schema-and-semantic-conventions-governance.md`](../sre-observability/34-schema-and-semantic-conventions-governance.md).

---

## 5. Correlation IDs and context propagation

> **In plain words.** Give every user request an ID at the front door and make every service,
> queue and background job carry it along and print it in every log line. Then "what happened to
> request X" is one search instead of an afternoon of matching timestamps. The standard way to
> carry it over HTTP and gRPC is the W3C `traceparent` header; over Kafka, a message header;
> into background jobs, a field in the job.
>
> **Real-world example.** A refund is issued twice. The refund API, the Kafka event and the worker
> that called the payment provider all log the same trace ID, and the worker log shows attempt 1
> timed out at the provider after the provider had already processed it, and attempt 2 succeeded.
> Without the ID, three teams would each report "our part worked".

### 5.1 Request ID vs trace ID

- A **request ID** is any unique ID for one inbound request, often generated by the edge proxy
  (`X-Request-ID`, nginx's `$request_id`). Easy to log everywhere; no structure.
- A **trace ID** is the 16-byte ID from W3C Trace Context that tracing systems use to assemble
  spans. If you run tracing, use the trace ID as the correlation ID in logs too, so that a log line
  links straight to a trace. Keep `X-Request-ID` only if clients or support tooling already use it,
  and log both.

Generate at the first hop you control. Accept an incoming ID from outside only if you trust the
source, and validate its format and length; otherwise attackers control a field in all your logs.

### 5.2 The W3C headers

```
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
             │  │                                │                │
             │  trace-id (16 bytes, 32 hex)       parent span id   flags (01 = sampled)
             version                              (8 bytes, 16 hex)
tracestate:  vendor-specific key=value pairs, e.g.  congo=t61rcWkgMzE
baggage:     tenant_tier=enterprise,feature_x=on   (application key-values carried to every hop)
```

- `traceparent` is what links spans. A service that receives it creates a child span and sends a
  new `traceparent` downstream with the same trace ID and its own span ID.
- The **sampled flag** tells downstream services whether the root decided to record this trace.
  Services that ignore it and make their own decision produce broken, partial traces.
- `baggage` carries values (tenant tier, experiment arm, a debug flag) to every downstream service
  so they can use them in logs and metrics. It is sent on every hop, including to third parties if
  you do not strip it at egress: never put PII or secrets in baggage, and keep it small.

### 5.3 Propagation across each kind of boundary

| Boundary | Where the context goes | Common way it breaks |
|---|---|---|
| HTTP | `traceparent`, `tracestate`, `baggage` headers | a proxy or API gateway that strips unknown headers; a hand-written HTTP client without instrumentation |
| gRPC | metadata (same keys, lowercase); deadline travels as `grpc-timeout` | custom interceptors that create a fresh context |
| Kafka | record headers (`traceparent` as a header) | producers that build records without headers; Kafka Connect or stream processors that drop headers |
| SQS / SNS / RabbitMQ | message attributes / AMQP headers | attribute limits; consumers that ignore them |
| Background jobs (Celery, Sidekiq, custom) | a field in the job payload or job headers | job enqueued from a context where the trace was already closed |
| Threads and executors | language context (Python `contextvars`, Java `Context`, Go `context.Context`) | thread pools that do not copy context |
| Database | SQL comment (sqlcommenter format) or `application_name` | the ORM strips comments; comments break prepared statement caching in some setups |
| Batch consumers | span *links* to each message's context, not a single parent | forcing one parent makes 499 of 500 messages lose their trace |

**Kafka, in Python with OpenTelemetry.** Auto-instrumentation libraries do this for most clients;
this is what they do under the hood:

```python
from opentelemetry import trace
from opentelemetry.propagate import inject, extract

tracer = trace.get_tracer("orders")

# producer: write the current context into the record headers
carrier: dict[str, str] = {}
inject(carrier)                                     # adds traceparent (+ tracestate, baggage)
producer.send("orders", value=payload,
              headers=[(k, v.encode()) for k, v in carrier.items()])

# consumer: continue the trace from the headers
for msg in consumer:
    ctx = extract({k: v.decode() for k, v in (msg.headers or [])})
    with tracer.start_as_current_span("orders process", context=ctx,
                                      kind=trace.SpanKind.CONSUMER) as span:
        span.set_attribute("messaging.kafka.offset", msg.offset)
        handle(msg)
```

The consumer span's parent is the producer span, so the gap between them in the waterfall is the
time the message spent in Kafka: that is consumer lag for this one message (§9). Async boundary
tracing in depth: [`../sre-observability/25-streaming-and-kafka-observability.md`](../sre-observability/25-streaming-and-kafka-observability.md) §8.

**Threads and background work in Python.** `contextvars` follow `await` automatically, and
`asyncio.to_thread` copies the context into the worker thread. `loop.run_in_executor` and a bare
`ThreadPoolExecutor.submit` do not, so the request ID disappears from every log line written in
the thread:

```python
import contextvars
from concurrent.futures import ThreadPoolExecutor

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
pool = ThreadPoolExecutor(max_workers=8)

def submit_with_context(fn, *args):
    ctx = contextvars.copy_context()           # snapshot request_id, OTel context, etc.
    return pool.submit(ctx.run, fn, *args)
```

For jobs that run later on another machine, serialize the context into the job at enqueue time
(`inject(job.headers)`) and `extract` it in the worker. If the job runs much later, use a span
link rather than a parent, so the original trace does not appear to last for hours.

**Database.** Tagging SQL with the caller's trace context lets you go from a row in
`pg_stat_activity` (a query holding a lock) back to the request that issued it:

```sql
SELECT * FROM orders WHERE id = $1 /*traceparent='00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01'*/
```

At minimum, set `application_name` per service (and ideally per pod) on every connection, so
`pg_stat_activity` tells you *who* is running the query.

### 5.4 How propagation lies

- **Broken chains look like missing services.** A trace that stops at the gateway does not mean
  the request never reached order-service; it may mean order-service started a new trace. Search
  logs by request ID as well as by trace ID.
- **Retries share a trace ID.** Good: all attempts appear in one trace. Make sure each attempt has
  its own span with an `attempt` attribute, or two attempts will look like one long call.
- **Fire-and-forget work outlives the trace.** Spans that end after the root span ended may be
  dropped by tail samplers that already made their decision (§6.4).
- **Header size limits.** Large `baggage` or `tracestate` values can push request headers over proxy
  limits (commonly 8–16 KB per header block) and turn into 431 or 400 errors.

---

## 6. Distributed tracing as an investigation tool

> **In plain words.** A trace waterfall shows each step of one request as a bar on a timeline.
> Reading it well means looking at what is *not* there: empty space inside a bar is time the code
> spent waiting for something nobody measured (a connection, a lock, a queue, the garbage
> collector). Bars stacked one after another that could have run at the same time are a design
> problem. And the slow request you need is often the one the sampler threw away.
>
> **Real-world example.** A trace shows `POST /checkout` at 4.45 s with its child spans adding up to
> 0.5 s. The 3.9 s of empty space sits before the first database span. Adding one span around
> "get connection from pool" turns the mystery into a label.

### 6.1 Reading a waterfall

The running example's attempt 2, as a waterfall (illustrative):

```
0 s        1 s        2 s        3 s        4 s        5 s
|----------|----------|----------|----------|----------|
gateway  POST /checkout (attempt 2, client span) ██████████████████████████  5.10 s
order-svc  POST /checkout (server span)               ░░░██████████████████  4.45 s
                                                      ▲
                     0.60 s gap: client span started, server span not yet (network + queue)
order-svc    (no child spans for 3.90 s)                 ···················   ← un-instrumented
order-svc    SELECT cart                                                 ▌   12 ms
order-svc    pricing.GetPrice ×20 (parallel)                              ▐█  max 350 ms
order-svc    INSERT order                                                   ▌ 30 ms
```

(The gateway span is 5.10 s because it includes the 0.6 s queue, 4.45 s server time, and a little
network. The customer's 8 s also includes attempt 1, the backoff and connection setup.)

Five things to read on every waterfall:

1. **Client span vs server span of the same call.** The gap between the caller's client span and
   the callee's server span (at the start and at the end) is network, proxy, and the callee's
   inbound queue. It is the best way to see queueing without instrumenting the queue.
2. **Self time.** A span's duration minus the union of its children. Large self time with low CPU
   is waiting: pool, lock, GC, throttling, synchronous I/O without a span.
3. **Staircase vs comb.** Identical child spans one after another (a staircase) are sequential
   calls, often N+1 queries or a loop over remote calls. Children that overlap (a comb) are
   parallel fan-out, where the slowest child sets the time.
4. **The critical path.** The chain of spans that actually determined the end time. Speeding up a
   span off the critical path changes nothing. Some tracing UIs compute it; otherwise walk
   backwards from the end of the root span to whichever child ended last.
5. **Retries and repeats.** Two spans for the same downstream call with the same parent, the first
   ending in an error or at exactly the timeout value, is a retry.

### 6.2 Finding what is in the gaps

Un-instrumented time is one of a short list of things. Rule them in or out:

| Gap looks like | Likely | Confirm with |
|---|---|---|
| before the first child span, on a busy service | pool acquire, worker queue | pool pending metric; queue-time histogram |
| before an outbound call, variable | connection setup: DNS, TCP, TLS | client connection metrics; `curl -w` from the pod |
| identical gap across all concurrent requests on one pod at the same instant | GC pause, event-loop block, CPU throttling | GC log; loop-lag metric; `cpu.stat` |
| gaps of ~100 ms sprinkled through CPU-heavy spans | CFS throttling at the 100 ms period | `nr_throttled` rising |
| gap between producer and consumer spans | message waiting in the queue | consumer lag in seconds (§9) |
| gap at the end of the server span | writing the response, serialization, slow client | span around serialization; proxy buffering |
| negative or overlapping impossible gaps | clock skew between hosts | compare to durations measured on one host (§18.5) |

Close the gaps you keep hitting by instrumenting them once: a span (or at least a histogram)
around pool checkout, lock acquisition, queue wait, and outbound connection setup. HTTP client
libraries usually expose hooks for connect and TLS timings. Details of OTel instrumentation:
[`../sre-observability/03-instrumentation.md`](../sre-observability/03-instrumentation.md).

### 6.3 Fan-out in traces

A comb of 20 parallel calls ends when the slowest one ends. When the root is slow because of
fan-out, you will see one child much longer than its siblings, and it will be a *different* child
(a different backend instance) in each slow trace. That pattern means the callee has a tail
problem (§7.4). If it is the *same* instance every time, you have one bad host (§18.4) and should
drain it.

### 6.4 Sampling: why the slow trace you need was thrown away

- **Head sampling** decides at the root span, before anything is known about the request: keep 1%.
  It is cheap and consistent across services (the decision rides in the `sampled` flag). But the
  requests you need are rare: if 0.3% of requests are slow and you keep 1%, you keep 0.003% of
  requests as slow traces, about 3 per 100,000 requests. During a short incident, that can be zero.
- **Tail sampling** buffers all spans of a trace and decides after it finishes: keep all errors,
  all traces slower than a threshold, and a small random baseline. It keeps exactly what you want,
  at the cost of a stateful tier: every span of a trace must reach the same collector instance
  (the OpenTelemetry Collector's load-balancing exporter routes by trace ID), and the collector
  must hold traces in memory until it decides.

```yaml
# OpenTelemetry Collector, tail_sampling processor
processors:
  tail_sampling:
    decision_wait: 30s          # must exceed your slowest traces you care about
    num_traces: 200000          # traces held in memory while waiting
    policies:
      - name: errors
        type: status_code
        status_code: {status_codes: [ERROR]}
      - name: slow
        type: latency
        latency: {threshold_ms: 1000}
      - name: baseline
        type: probabilistic
        probabilistic: {sampling_percentage: 1}
```

The trap specific to debugging slowness: `decision_wait` is how long the collector waits for spans
before deciding. If it is 5 s and your problem request takes 8 s, the decision is made on an
incomplete trace, and spans that arrive after the decision are handled separately or lost. The
slowest traces are exactly the ones most likely to be cut. Size `decision_wait` from your timeout
budget, not from your median.

Other ways the needed trace disappears: an overloaded collector drops spans (the pipeline is
saturated during the incident you are debugging), SDK export queues are full and drop spans, and
spans from services that make their own head-sampling decision do not match the root's.
Sampling in depth: [`../sre-observability/08-traces-storage.md`](../sre-observability/08-traces-storage.md) §8,
[`../sre-observability/02-opentelemetry-deep-dive.md`](../sre-observability/02-opentelemetry-deep-dive.md) §6,
and pipeline reliability in [`../sre-observability/28-telemetry-pipeline-reliability.md`](../sre-observability/28-telemetry-pipeline-reliability.md).

**Practical defaults for debuggability.** Tail-sample errors and slow traces at 100%; always keep
the trace ID in logs even for unsampled requests, so the logs for any request can be joined even
when its trace was not kept; and emit exemplars so every latency bucket points at a kept trace.

### 6.5 How traces lie

- **Missing spans look like fast services.** A service without instrumentation contributes its
  time to its caller's self time.
- **Span end is when the code said so.** A span ended before a streaming response finished, or
  started after pool acquisition, reports the wrong number with full confidence.
- **Async work is detached.** Work scheduled "after the response" is not on the critical path of
  that request but competes for the same resources as the next one.
- **Clocks.** Spans from different hosts are aligned with wall clocks. A child that appears to
  start before its parent is skew, not time travel. Durations within one host are reliable
  (monotonic clocks); positions across hosts are approximate. Some UIs (Jaeger, for example) adjust
  for skew heuristically, which can also hide a real gap.

---

## 7. Tail latency — percentiles, histograms, and fan-out

> **In plain words.** Averages describe nobody. What users feel is the slow end of the
> distribution: the p99 and p99.9. Percentiles cannot be averaged or added; they must be computed
> from histograms. Load tests can hide the tail by accident (coordinated omission). And when one
> request calls many backends, the backend's rare slow case becomes the request's common case.
>
> **Real-world example.** A search page queries 100 index shards. Each shard is slow (over 1 s) only
> 1% of the time. The page is slow `1 − 0.99^100 ≈ 63%` of the time. The shard team's dashboard
> is green; the page is broken.

### 7.1 Percentiles

- **p50** (median): what a typical request sees. **p95 / p99**: what one in 20 / one in 100
  requests sees, and what a user who makes 100 requests per session sees at least once. **p99.9
  and max**: where pool timeouts, GC, retries and lock waits live.
- A user session touches many requests (a page load can issue dozens of API calls), so a user's
  experience is closer to the service's p99 than to its p50.
- Report percentiles over windows long enough to hold enough samples: p99.9 over one minute at
  10 req/s is based on 600 samples, so "the slowest one" is the whole p99.9 estimate.

### 7.2 You cannot average percentiles

```
Pod A: 1,000 req/s, p99 = 100 ms          Pod B: 10 req/s, p99 = 2,000 ms
avg of p99s = 1,050 ms   ← describes neither pod nor the fleet
```

The fleet's p99 depends on the full distributions, which the two p99 numbers do not contain. The
same holds across time (the p99 of an hour is not the average of 60 per-minute p99s) and across
services (the p99 of A-then-B is not p99(A) + p99(B)).

**Histograms aggregate; summaries do not.** A Prometheus histogram exports counts per bucket
(`_bucket{le="0.25"}`), which you can sum across pods and over time and *then* compute a quantile
from. A summary computes quantiles in each process; you cannot combine them correctly.

```promql
# correct: sum buckets across pods, then take the quantile
histogram_quantile(0.99, sum by (le) (rate(http_server_request_duration_seconds_bucket[5m])))

# wrong: averaging pre-computed quantiles from a summary
avg(http_server_request_duration_seconds{quantile="0.99"})
```

Histogram caveats: the quantile is interpolated within a bucket, so accuracy depends on bucket
boundaries; put boundaries around your SLO thresholds (0.1, 0.25, 0.5, 1, 2.5, 5, 10 s), and make
sure the largest finite bucket is above your longest timeout, or the tail is clipped (§4.3).
Prometheus native histograms (and OTel exponential histograms) use automatically spaced buckets
and largely remove the boundary problem. Histograms vs summaries in more depth:
[`../sre-observability/00-mental-models.md`](../sre-observability/00-mental-models.md) §10.

### 7.3 Coordinated omission

A load generator that sends the next request only after the previous one returns (a closed loop)
stops sending while the server is stalled. It records the stall as *one* slow sample, when in
reality every user who would have arrived during the stall was also delayed.

Worked numbers (the §23 experiment reproduces them): 100 req/s scheduled for 100 s, 1 ms per
request, and one 2 s server freeze.

| | Samples during the freeze | Recorded p99 | Recorded p99.9 |
|---|---|---|---|
| Closed-loop tool (naive) | 1 | 1 ms | 1 ms |
| Open-loop (measured from scheduled send time) | 200 | ~1.1 s | ~1.9 s |

The naive tool reports a flawless p99.9 for a system that froze for 2 seconds. Use load generators
with a fixed arrival rate (wrk2 with `-R`, k6's `constant-arrival-rate` executor, vegeta with
`-rate`) and HdrHistogram-style recording. The same bias affects production clients with a fixed
concurrency (a batch job with 10 workers) and synthetic probes that run one request at a time: they
under-sample exactly the slow periods. Gil Tene's talk "How NOT to Measure Latency" is the
standard reference.

### 7.4 Fan-out amplification

If one call exceeds a latency threshold with probability `p`, and a request waits for all `N`
independent calls, the request exceeds it with probability `1 − (1 − p)^N`:

| Fan-out N | p = 1% (callee p99) | p = 0.1% (callee p99.9) |
|---|---|---|
| 1 | 1.0% | 0.1% |
| 10 | 9.6% | 1.0% |
| 20 | 18.2% | 2.0% |
| 50 | 39.5% | 4.9% |
| 100 | 63.4% | 9.5% |

Read backwards: for the request's p99 to stay under a threshold with `N = 100`, each callee needs
roughly `0.99^(1/100) ≈ 0.9999`, i.e. its **p99.99** must be under that threshold. Fan-out services
must be engineered for their dependencies' extreme tail, not their p99. The independence assumption
is optimistic: if the slow calls share a cause (a GC pause on a shared cache node), they are
correlated and the tail is worse for some requests and better for others.

Dean and Barroso's "The Tail at Scale" (CACM, 2013) is the canonical treatment. It includes the
100-server example above and reports that, in a Google benchmark reading 1,000 keys spread over 100
servers, sending a hedged request after a 10 ms delay cut the 99.9th-percentile latency from
1,800 ms to 74 ms while sending only about 2% more requests.

### 7.5 Tail-tolerant techniques, and what to check before using them

| Technique | What it does | Diagnostic check first |
|---|---|---|
| Hedged requests | after the p95 of the expected latency, send a second copy to another replica; use the first answer; cancel the other | is the tail per-replica (hedging helps) or shared (hedging doubles load on the cause)? Operation must be idempotent |
| Tied requests | send to two replicas, each told about the other; the first to *start* cancels the other | queueing at the replica is the main tail source |
| Micro-partitioning / rebalancing | many small partitions per server so load can move in small steps | tail concentrates on hot partitions |
| Latency-aware load balancing | least-outstanding-requests or power-of-two-choices instead of round robin | per-instance latency varies a lot |
| Outlier ejection | stop sending to an instance whose error or latency is much worse | one host is consistently the slow child (§6.3) |
| Good-enough results | return partial results after a deadline (search: 98 of 100 shards) | product accepts partial answers |
| Deadlines and cancellation | stop work nobody is waiting for | callers time out while callees keep working (§17.2) |

Hedging is powerful and dangerous: under overload every request crosses the hedge threshold, so
hedging doubles load exactly when capacity is shortest. Cap hedges with a budget (for example, at
most 5% of requests) and disable them when the callee reports saturation. Hedging and retry budgets:
[`33-resilience-patterns-circuit-breakers.md`](33-resilience-patterns-circuit-breakers.md) §2.6–§2.7;
load balancing choices: [`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md) §6.7;
queueing theory of the tail: 34 §2.6.

---
## 8. Queueing — Little's Law and the time nobody measures

> **In plain words.** Whenever work arrives faster than it can be done, even briefly, it waits in
> a line. The line is invisible to "how long did the work take" metrics, because the timer starts
> when the work starts. As a resource gets busier the line grows slowly, then suddenly: going from
> 80% to 95% busy multiplies waiting time several times. Little's Law lets you compute the hidden
> waiting time from two numbers you usually have.
>
> **Real-world example.** A pod handles 150 req/s. The gateway reports an average of 120 requests
> in flight to it. By Little's Law each request spends 120 / 150 = 0.8 s from the gateway's point
> of view, but the handler histogram says 45 ms. About 0.75 s per request is spent waiting before
> the handler starts.

### 8.1 Little's Law, used as a diagnostic

`L = λ·W`: the average number of items in a system equals the arrival rate times the average time
each item spends in it. It holds for any stable system regardless of distributions, which makes it
a consistency check between metrics:

| You know | You compute | Example |
|---|---|---|
| in-flight `L` and rate `λ` | true time in system `W = L/λ` | 120 in flight / 150 req/s = 0.8 s, vs 45 ms handler → 0.75 s hidden queueing |
| rate and hold time | resources busy on average `L = λ·W` | 400 req/s × 25 ms connection hold = 10 connections busy on average |
| lag and throughput | time to drain | 120,000 messages behind, draining at (2,000 − 1,500) msg/s = 240 s |
| concurrency limit and latency | max throughput `λ = L/W` | 64 threads / 0.5 s per request = 128 req/s, no matter how many CPUs |

The last row explains a common confusion: when a dependency slows down, a fixed-size thread pool
caps throughput at `threads / latency`, so the service's CPU *drops* while its queue grows.
"CPU went down during the incident" is a strong hint that the bottleneck is a concurrency limit
(threads, connections, locks), not compute.

Get `L` from an in-flight gauge (Envoy `upstream_rq_active`, a middleware gauge, `http.server.active_requests`
in OTel conventions) and `λ` from the request counter over the same window. Compute
`W_hidden = L/λ − mean(T_handler)`. If it is large, the queue is before the handler.

### 8.2 Utilization and the knee

For intuition, a single-server queue with random arrivals and random service times (M/M/1) has
average time in system `T = S / (1 − ρ)`. With `S = 40 ms`:

| Utilization ρ | Time in system T | Of which waiting |
|---|---|---|
| 0.50 | 80 ms | 40 ms |
| 0.75 | 160 ms | 120 ms |
| 0.90 | 400 ms | 360 ms |
| 0.95 | 800 ms | 760 ms |
| 0.99 | 4,000 ms | 3,960 ms |

Real servers have many workers (M/M/c waits less than M/M/1 at the same ρ) and less random
service times, which moves the knee to the right but does not remove it. The practical lessons
for debugging:

- **The handler time stays flat while total time explodes.** In the §23 experiment, handler p50
  stays near 35 ms from ρ = 0.5 to ρ = 0.95 while end-to-end p50 goes from 40 ms to 150 ms.
- **Average utilization misleads.** 60% average over a minute can be 100% for half the minute and
  20% for the other half; queues form during the 100% part. Look at utilization at the resolution
  of the latency you care about, or directly at saturation (queue length, pending, run queue).
- **Variability matters as much as load.** Bursty arrivals or a bimodal service time (most requests
  10 ms, a few 2 s) create queues at modest average utilization (Kingman's formula,
  [`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md) §2.5).
  One slow request type can make fast requests wait behind it: head-of-line blocking.

Full queueing treatment: [`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md) §1.2–§2.8.

### 8.3 Why "server time" excludes the queue, layer by layer

| Queue | Where it lives | How to see it |
|---|---|---|
| Kernel SYN queue and accept queue | listen socket | `ss -ltn` (Recv-Q = waiting to be accepted, Send-Q = backlog size); `nstat -az TcpExtListenOverflows` |
| Proxy pending queue | Envoy, HAProxy, nginx upstream | Envoy `upstream_rq_pending_active`, `upstream_rq_pending_overflow`; HAProxy `qcur` |
| Web server worker queue | Gunicorn (listen backlog), Tomcat (`acceptCount`), Jetty/Undertow queue | thread-pool queue-size metrics; queue-time header (§3.3) |
| Executor queue inside the app | `ThreadPoolExecutor`, Java executors | Micrometer `executor_queued_tasks`; your own gauge |
| Event loop ready queue | asyncio, Node.js | loop lag (§14.3) |
| Pool wait | DB / HTTP client pools | pending / acquire-time metrics (§13) |
| Broker queue | Kafka, SQS, RabbitMQ | lag in seconds, age of oldest message (§9) |

```bash
# Listen socket on port 8000: Recv-Q is connections waiting for the app to accept them
ss -ltn 'sport = :8000'
# State   Recv-Q  Send-Q  Local Address:Port
# LISTEN  913     2048    0.0.0.0:8000          ← 913 connections queued, backlog 2048
```

### 8.4 The fix

- **Reduce `S` or add servers** when the queue is from genuine load. Check that the bottleneck
  actually scales: more pods do not help if the queue is really for 10 database connections.
- **Bound every queue** and reject when full (a fast 503 or 429 is better than a slow timeout).
- **Drop work whose deadline has passed** before starting it: if the request waited 2.6 s and the
  caller's timeout was 2.5 s, processing it wastes capacity (attempt 1 in the running example).
  Propagate the deadline (gRPC does this with `grpc-timeout`) or pass it in a header, and check it
  when dequeuing.
- **Under overload, prefer newest-first or CoDel-style queues**, so the requests you serve are ones
  whose callers are still waiting ([`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md) §7).
- **Measure queue time permanently** with a histogram, and alert on it; it moves before handler
  time and before errors.

---

## 9. Consumer lag — Kafka and other queues

> **In plain words.** A consumer that reads a stream can fall behind. "Lag" is how far behind.
> Counting it in messages is easy but hard to interpret; counting it in seconds ("the oldest
> unprocessed event is 90 s old") is what users feel. Lag that grows on every partition means
> the consumers are too slow overall; lag that grows on one partition means something specific
> to that partition: a hot key, a poison message, or one sick consumer.
>
> **Real-world example.** Order confirmation emails arrive 20 minutes late. Lag is 0 on 11 of 12
> partitions. On partition 7, the committed offset has not moved for 20 minutes, and the consumer
> log repeats the same deserialization error for offset 4,418,201 every few seconds: a poison
> message the consumer retries forever.

### 9.1 What lag is and how it lies

`lag_offsets = log_end_offset − committed_offset`, per partition. How it misleads:

- **Offsets are not time.** 100,000 messages of lag is 1 s on a partition receiving 100,000 msg/s
  and a day on one receiving 1 msg/s. Summing offset lag across partitions of different rates
  produces a number with no meaning. Alert on **time lag**: the age of the oldest unconsumed
  message, or `lag_offsets / consume_rate` as an estimate (only valid when the rate is steady).
- **Committed is not processed.** Consumers commit periodically, so lag shows a sawtooth even when
  healthy. A consumer that commits *before* processing (auto-commit with asynchronous processing)
  can show zero lag while dropping work.
- **Zero lag can hide a delay upstream.** If the producer is an outbox relay or a batch job that is
  itself behind, events enter Kafka late and consumer lag looks perfect.
- **Metrics can vanish.** Some exporters stop reporting lag for a group with no active members, so
  the dashboard goes blank (or flat) exactly when every consumer is dead. Alert on absence.
- **Rebalances pause everyone.** During a rebalance the group stops consuming (fully, with the
  eager protocol; partially, with cooperative rebalancing). Lag climbs on all partitions and
  then recovers, which looks like a load spike.

### 9.2 How to measure it

```bash
# Per-partition lag, owner and host
kafka-consumer-groups.sh --bootstrap-server kafka:9092 --describe --group billing
# GROUP    TOPIC   PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG      CONSUMER-ID           HOST        CLIENT-ID
# billing  orders  6          9812001         9812044         43       consumer-billing-1-…  /10.0.1.17  consumer-billing-1
# billing  orders  7          4418201         4541377         123176   consumer-billing-2-…  /10.0.2.23  consumer-billing-2
# billing  orders  8          9803377         9803401         24       consumer-billing-3-…  /10.0.3.9   consumer-billing-3

# Group state (Stable, PreparingRebalance, CompletingRebalance, Empty, Dead) and coordinator
kafka-consumer-groups.sh --bootstrap-server kafka:9092 --describe --group billing --state

# Members and their assigned partitions
kafka-consumer-groups.sh --bootstrap-server kafka:9092 --describe --group billing --members --verbose
```

Run `--describe` twice, 30 s apart. A partition whose `CURRENT-OFFSET` did not move while
`LOG-END-OFFSET` did is stuck, whatever its lag number.

```promql
# offset lag per partition (kafka_exporter-style metric names)
sum by (topic, partition) (kafka_consumergroup_lag{consumergroup="billing"})

# is lag growing or draining? (positive = falling further behind)
deriv(sum(kafka_consumergroup_lag{consumergroup="billing"})[10m:30s])

# time lag, if you run a time-lag exporter (e.g. kafka-lag-exporter)
max by (topic) (kafka_consumergroup_group_lag_seconds{group="billing"})
```

Client-side, the Java consumer exposes `records-lag-max` (in the consumer fetch manager metrics);
it is the lag that consumer instance sees on its own partitions.

### 9.3 What the patterns look like

| Pattern | Most likely cause | Next check |
|---|---|---|
| All partitions growing roughly linearly | consumers slower than producers: more traffic, or the consumer's own dependency slowed | consume rate vs produce rate; the consumer's downstream latency |
| One partition growing, offset **not** advancing | poison message, or the consumer for it is hung | consumer logs for a repeated offset; a stack dump of that consumer |
| One partition growing, offset advancing slowly | hot key (skewed partitioning), or that consumer instance is on a sick host | produce rate per partition; per-host CPU/throttling |
| All partitions jump up together, then drain, repeatedly | rebalances | group state; consumer logs for "poll timeout has expired" or join/leave messages |
| Sawtooth of fixed period | commit interval or batch processing | normal if the peak is small |
| Lag fine, end-to-end latency bad | delay before the topic (producer batching, outbox relay, upstream job) | timestamp in the message vs time of production |

**Rebalance storms.** A consumer that takes longer than `max.poll.interval.ms` (default 5 minutes)
between `poll()` calls is removed from the group, triggering a rebalance; it then rejoins and
triggers another. With `max.poll.records` = 500 (default) and a downstream that has slowed to
1 s per record, one batch takes 500 s, longer than the limit. The Java client logs that the
consumer poll timeout has expired. Fixes: smaller `max.poll.records`, time-bounded processing per
batch, static membership (`group.instance.id`) so restarts do not rebalance, and the cooperative
sticky assignor (or the newer consumer group protocol from KIP-848) so a rebalance does not stop
the whole group.

**Poison messages.** Retrying one bad message forever stops a partition, because Kafka consumers
process a partition in order. Fix: bounded retries with backoff, then a dead-letter topic with the
original headers and the error; alert on DLQ rate.

**Hot partitions.** One key (a large tenant, a default value such as `null` or `"unknown"`) gets a
large share of traffic. Check produce rate per partition. Fixes are in the key design:
[`07-kafka-and-event-streaming.md`](07-kafka-and-event-streaming.md) §5; hot-key strategies in
[`10-sharding-and-consistent-hashing.md`](10-sharding-and-consistent-hashing.md).

The same reasoning applies to SQS (`ApproximateAgeOfOldestMessage` is the time lag), RabbitMQ
(queue depth plus consumer utilisation) and job queues (age of the oldest job). Consumer and
broker signals in depth: [`../sre-observability/25-streaming-and-kafka-observability.md`](../sre-observability/25-streaming-and-kafka-observability.md) §4–§10;
Kafka consumer internals: [`07-kafka-and-event-streaming.md`](07-kafka-and-event-streaming.md) §4 and §12.

---

## 10. Replication lag — stale reads that look like bugs

> **In plain words.** Many systems send writes to a primary database and reads to replicas that
> copy the primary with a delay. When that delay grows, users read old data: they change their
> email and the next page shows the old one. These reports arrive as "caching bugs" or "the save
> button doesn't work", and nobody looks at replication until someone graphs lag next to the
> complaints.
>
> **Real-world example.** Every night at 02:00 a batch job updates 40 million rows. Replica lag
> rises to 12 s for 20 minutes. During that window, users in Asia see their just-placed orders
> missing from "My orders" and place them again, creating duplicates.

### 10.1 Symptoms that point to replication lag

- Read-your-own-write failures: the write returned success, the next read does not show it.
- Values that go backwards: a counter or status seen as newer, then older (two reads hit two
  replicas with different lag).
- Uniqueness or idempotency checks that read from a replica miss a row that exists on the primary,
  so a duplicate is created.
- Problems that appear only under write-heavy periods, only for some users (those routed to one
  replica), and disappear on retry a few seconds later.

### 10.2 How to measure it, and how the measurement lies

```sql
-- On the Postgres primary: lag per replica, in bytes and in time
SELECT application_name, client_addr, state,
       pg_wal_lsn_diff(sent_lsn, replay_lsn) AS replay_lag_bytes,
       write_lag, flush_lag, replay_lag
FROM pg_stat_replication;

-- On a Postgres replica: time since the last replayed transaction
SELECT now() - pg_last_xact_replay_timestamp() AS apparent_lag;
```

- `now() − pg_last_xact_replay_timestamp()` on a replica *grows when the primary is idle*, because
  no new transactions arrive to replay. It over-reports lag at quiet times. Use a heartbeat table
  (the primary writes the current time every second; lag = now − the value visible on the replica)
  for a number that means what you want.
- `pg_stat_replication`'s `*_lag` columns show the recent measured lag and go to NULL a short time
  after a replica has caught up and there is no WAL activity.
- MySQL's `Seconds_Behind_Source` (`Seconds_Behind_Master` in older versions) is computed from the
  timestamp of the event being applied. It reads 0 when the applier is idle even if the replica has
  not yet received newer events, and jumps around with large transactions. Heartbeat tools
  (pt-heartbeat) are the reliable alternative.
- Lag averaged over replicas hides the one replica that is minutes behind. Alert on the max.

### 10.3 Why replicas fall behind

| Cause | What you see |
|---|---|
| Large write bursts (bulk updates, backfills, index builds) | byte lag climbs with the primary's WAL rate |
| Replica disk or CPU saturation | replica I/O wait high; apply rate below primary's WAL rate |
| Single-threaded apply | primary uses many cores, replica applies on one (MySQL without parallel replication) |
| Replay paused by long queries on the replica | Postgres: `max_standby_streaming_delay` lets replay wait for queries that conflict; lag grows while a report runs |
| Network between primary and replica | cross-region replicas lag by at least the RTT, more under bandwidth limits |
| Long transactions on the primary | changes become visible on the replica only when the transaction commits |

### 10.4 The fix

- **Read-your-writes where it matters.** After a user writes, route that user's reads to the
  primary for a few seconds, or carry a position token: return the primary's LSN after the write
  (`SELECT pg_current_wal_lsn()`), and have the reader use a replica only once
  `pg_last_wal_replay_lsn() >= token`, otherwise the primary.
- **Monotonic reads**: pin a session to one replica, so values do not go backwards.
- **Never check uniqueness or idempotency on a replica.** Use constraints on the primary.
- **Throttle bulk jobs** by replica lag (pause the backfill when lag exceeds a threshold).
- **Alert on max lag in seconds**, measured by heartbeat.

Consistency models behind these guarantees: [`04-replication-and-consistency.md`](04-replication-and-consistency.md);
cross-region lag: [`36-multi-region-active-active-and-geo-replication.md`](36-multi-region-active-active-and-geo-replication.md);
replica lag as a database signal: [`../sre-observability/23-database-observability.md`](../sre-observability/23-database-observability.md) §7.

---

## 11. GC pauses and runtime stalls

> **In plain words.** Languages with automatic memory management occasionally pause the program,
> or slow it down, to reclaim memory. A pause does not appear as its own step in any trace; it
> makes whatever was running look slow. Every request in that process slows down by the same
> amount at the same moment, which is the fingerprint.
>
> **Real-world example.** A JVM service's p99 jumps from 80 ms to 900 ms every few minutes, on one
> pod at a time. The GC log shows full collections of about 800 ms at exactly those timestamps:
> a new in-process cache had grown the live data to 90% of the maximum heap, so the collector ran
> almost continuously and kept falling back to full collections.

### 11.1 What pauses look like in each runtime

| Runtime | Where pauses come from | What to measure |
|---|---|---|
| JVM | stop-the-world phases of the collector (G1 young and mixed pauses, full GC); safepoints for other reasons (deoptimization, thread dumps, biased-lock revocation in older JDKs) | GC and safepoint logs; `jvm_gc_pause_seconds` (Micrometer) |
| Go | short stop-the-world phases (usually sub-millisecond); the bigger cost is **GC assist** (goroutines that allocate are made to help marking) and the collector's CPU use competing with requests | `GODEBUG=gctrace=1`; `go_gc_duration_seconds`; CPU profile share in `runtime.gcBgMarkWorker` |
| Python (CPython) | reference counting frees most objects immediately; the cyclic collector's full (generation 2) collections pause the interpreter and grow with the number of tracked objects | `gc.callbacks` timing; `gc.get_stats()` |
| Node.js | V8 scavenges and mark-compact | `--trace-gc`; `perf_hooks` GC entries |

Pauses that are not GC but look the same: the process being swapped or its memory being
reclaimed under cgroup pressure, CPU throttling (§12), a VM being paused or live-migrated, and
long synchronous work on an event loop (§14.3).

### 11.2 How to confirm

The test is timestamp alignment: slow requests on a pod should start before, and end just after,
a pause on that same pod, and the added latency should roughly equal the pause.

```bash
# JVM (JDK 9+ unified logging): GC and safepoint events with wall-clock and uptime stamps
java -Xlog:gc*,safepoint:file=/var/log/app/gc.log:time,uptime,level,tags:filecount=5,filesize=20m ...
# [2026-09-25T14:02:07.301+0000][12345.678s][info][gc] GC(118) Pause Young (Normal) (G1 Evacuation Pause) 1843M->612M(4096M) 91.234ms

jstat -gcutil <pid> 1000          # heap occupancy per generation and GC counts/times, every second
jcmd <pid> GC.heap_info

# Go: one line per GC cycle; the first and third "clock" numbers are the stop-the-world phases
GODEBUG=gctrace=1 ./server
# gc 42 @123.456s 4%: 0.061+5.2+0.047 ms clock, ...
```

```promql
# JVM: fraction of wall time spent in GC pauses, and the worst pause per pod
sum by (pod) (rate(jvm_gc_pause_seconds_sum[1m]))
max by (pod) (jvm_gc_pause_seconds_max)

# Go: worst stop-the-world pause observed (summary exported by client_golang)
max by (pod) (go_gc_duration_seconds{quantile="1"})
```

```python
# Python: log slow cyclic collections
import gc, logging, time

_t0 = 0.0
def _gc_timer(phase, info):
    global _t0
    if phase == "start":
        _t0 = time.perf_counter()
    elif (ms := (time.perf_counter() - _t0) * 1000) > 20:
        logging.warning("gc gen%d took %.0f ms (collected %d)", info["generation"], ms, info["collected"])

gc.callbacks.append(_gc_timer)
```

**What the pattern looks like.**

- Spikes that hit **all endpoints on one process at the same instant**, including trivial ones
  like a health check.
- A trace gap of the same length in every request that was in flight on that pod at that moment.
- **Per-pod, unsynchronised** timing: pod A spikes at 14:02:07, pod B at 14:03:41. If every pod
  spikes at the same second, look for a shared trigger instead (cron, cache expiry, §19.4).
- Heap graphs in a sawtooth whose peaks are close to the limit, or a staircase of rising floors
  (a leak, leading to ever more frequent collections).
- Health checks timing out during long pauses, which makes an orchestrator restart a healthy but
  paused process ([`29-failure-detection-phi-accrual.md`](29-failure-detection-phi-accrual.md) §10.1).

### 11.3 The fix

- **Reduce allocation** first: an allocation profile usually shows a few hot spots (serialization,
  logging, per-request buffers). Continuous profiling: [`../sre-observability/09-profiling.md`](../sre-observability/09-profiling.md).
- **Size the heap for the container**: leave headroom between the heap limit and the container's
  memory limit (thread stacks, direct buffers and native memory live outside the heap). In Go, set
  `GOMEMLIMIT` below the container limit so the collector works harder before the OOM killer acts.
- **Choose the collector for the latency goal**: G1 with a pause target for general services; ZGC
  (generational since JDK 21) or Shenandoah for very low pause targets, at some throughput cost.
- **Python**: reduce long-lived object churn, call `gc.freeze()` after start-up in pre-fork servers
  so start-up objects are excluded from collections, and tune `gc.set_threshold` only with
  measurements.
- **Move big caches out of the heap** (a local cache with millions of objects makes every full
  collection slower) or into a separate process.

---

## 12. CPU saturation, throttling, and steal

> **In plain words.** "CPU usage 40%" does not mean there is spare CPU. The number is an average
> over time and over cores; your container may have a hard CPU allowance it exhausts in short
> bursts, after which the kernel stops it until the next 100 ms window; a single thread may be
> maxing out one core; or the hypervisor may be giving your "CPU" to another VM.
>
> **Real-world example.** A Go service with a 2-CPU limit on a 32-core node shows 0.8 cores of
> average usage. Its p99 is 300 ms worse than on a node pool without limits. `cpu.stat` shows the
> container was throttled in 35% of 100 ms periods: the runtime ran 32 threads at once, burnt the
> 200 ms allowance in about 6 ms of wall time, and then waited.

### 12.1 Utilization vs saturation on a host

- **Utilization**: share of time CPUs were busy. **Saturation**: runnable threads waiting for a
  CPU. Latency comes from saturation.
- **Load average** on Linux counts runnable tasks *and* tasks in uninterruptible sleep (usually
  disk or NFS I/O). A high load average with idle CPUs is an I/O problem, not a CPU one.
- **Per-core view**: a single-threaded component (a Redis instance, a Python process holding the
  GIL, network interrupts delivered to one core) can be at 100% on one core while the host shows
  a few percent.

```bash
vmstat 1                 # r = runnable tasks (saturation if persistently > cores), b = blocked on I/O
mpstat -P ALL 1          # per-core %usr %sys %soft %steal: look for one hot core or high %soft
pidstat -u -t -p <pid> 1 # per-thread CPU: which thread is at 100%
cat /proc/pressure/cpu   # PSI: "some avg10=…" = share of time at least one task waited for CPU
```

### 12.2 CFS throttling in containers

A CPU **limit** is enforced with CFS bandwidth control: the cgroup gets `quota` of CPU time per
`period` (100 ms by default). A limit of 2 CPUs is 200 ms of CPU time per 100 ms of wall time,
*summed over all threads*. A multi-threaded process can spend that in a fraction of the period and
is then not scheduled at all until the period ends.

```
limit = 2 CPUs → quota 200 ms per 100 ms period; process runs 16 busy threads in a burst

period:  0 ms ──────── 12.5 ms ───────────────────────────────── 100 ms
         16 threads × 12.5 ms = 200 ms of CPU used
                            └──────── THROTTLED for 87.5 ms ────────┘
average usage over a minute: 0.8 CPUs (40% of the limit); requests arriving in the burst wait up to 87.5 ms
```

**How it lies.** Usage graphs are averages over 15–60 s, far longer than the 100 ms period, so a
container that is throttled in a third of all periods can show 40% of its limit. Node-level CPU
can be low at the same time. Nothing in application metrics says "throttled"; you see only
latency, often in multiples of the period.

**How to measure.**

```bash
# cgroup v2 (inside the container, or under the pod's cgroup on the node)
cat /sys/fs/cgroup/cpu.max        # "200000 100000" = 200 ms quota per 100 ms period; "max 100000" = no limit
cat /sys/fs/cgroup/cpu.stat
# usage_usec 98234512
# user_usec 81234000
# system_usec 17000512
# nr_periods 36000
# nr_throttled 12600               ← throttled in 35% of periods
# throttled_usec 1134000000        ← 1,134 s of wall time spent unable to run
cat /sys/fs/cgroup/cpu.pressure    # per-cgroup PSI

# cgroup v1 equivalent: /sys/fs/cgroup/cpu,cpuacct/cpu.stat with throttled_time in nanoseconds
```

```promql
# share of CFS periods in which each container was throttled (cAdvisor metrics)
sum by (namespace, pod, container) (rate(container_cpu_cfs_throttled_periods_total{container!=""}[5m]))
  / sum by (namespace, pod, container) (rate(container_cpu_cfs_periods_total{container!=""}[5m]))

# seconds of throttling per second (how much wall time the container could not run)
sum by (pod) (rate(container_cpu_cfs_throttled_seconds_total{container!=""}[5m]))
```

A throttled-period ratio above a few percent on a latency-sensitive service is worth fixing.

**The fix.**

- Remove CPU limits for latency-sensitive services and set requests accurately (the scheduler and
  CPU weights still share CPU fairly), or raise limits well above typical bursts. The trade-offs
  are in [`../kubernetes/21-resource-management-and-qos.md`](../kubernetes/21-resource-management-and-qos.md) §9, §10 and §27.
- Match the runtime's parallelism to the allowance: `GOMAXPROCS` (recent Go versions derive it from
  the cgroup limit; older ones need `automaxprocs` or an explicit setting), `-XX:ActiveProcessorCount`
  or container-aware JVM defaults, worker and thread-pool counts, and the thread counts of native
  libraries (BLAS, compression).
- For the most latency-critical pods, integer CPU requests with the static CPU manager give
  exclusive cores ([`../kubernetes/21-resource-management-and-qos.md`](../kubernetes/21-resource-management-and-qos.md) §11).
- Older kernels had a CFS bandwidth bug that throttled multi-core workloads well below their quota;
  it was fixed in Linux 5.4 and backported to some distribution kernels. If throttling looks
  impossible given usage, check the kernel version.

### 12.3 Steal time and noisy neighbours

On virtual machines, **steal** is time a vCPU was ready to run but the hypervisor ran something
else. It shows as `%st` in `top`, `%steal` in `mpstat`, and `node_cpu_seconds_total{mode="steal"}`
in node_exporter. A few percent is common on shared instances; sustained double digits means the
host is oversubscribed or a burstable instance has run out of CPU credits (on AWS, watch
`CPUCreditBalance` for T-family instances). The fix is a different instance type or host, not
application tuning. Other shared-resource neighbours (memory bandwidth, disk, network allowances)
are covered in §18.4.

---
## 13. Connection pools

> **In plain words.** Opening a database or HTTP connection is expensive, so services keep a small
> pool and lend connections to requests. When every connection is lent out, the next request waits.
> That wait is invisible to the database (which sees fast queries) and to query timers (which start
> after the connection is handed over). Pool exhaustion is the single most common answer to "every
> component is fast but the request is slow".
>
> **Real-world example.** A service makes an HTTP call to a fraud-check API *inside* a database
> transaction. When the fraud API slows from 50 ms to 2 s, each request holds its DB connection
> for 2 s instead of 60 ms. The pool of 10 serves 5 req/s instead of 160, and every other endpoint
> of the service, even ones that never call the fraud API, times out waiting for a connection.

### 13.1 How pools lie

- **The database says "fast".** `pg_stat_statements` reports execution time. Waiting for a
  connection happens in the client.
- **The DB span says "fast".** Most instrumentation starts the query span after checkout.
- **"Active 10/10" looks like healthy utilization.** Without the *pending* count you cannot tell a
  busy pool from a saturated one.
- **Pool timeouts turn into other errors.** A 30 s acquire timeout (the HikariCP and SQLAlchemy
  defaults) is longer than most callers wait, so the caller times out first and the pool error
  never appears in the caller's logs.
- **Leaks look like exhaustion.** A code path that does not return connections drains the pool
  slowly; it looks like load until you notice it never recovers at night.
- **Idle-in-transaction looks like load on the database.** Connections held by application code
  that is doing something else (an HTTP call, a sleep, a slow loop) show in Postgres as
  `idle in transaction`, holding locks and snapshots without doing work.

### 13.2 How to measure

| Pool | Saturation signal | Wait-time signal |
|---|---|---|
| HikariCP (Micrometer) | `hikaricp_connections_pending`, `hikaricp_connections_active` vs `hikaricp_connections_max` | `hikaricp_connections_acquire_seconds` (timer), `hikaricp_connections_timeout_total` |
| Go `database/sql` | `DBStats.InUse` vs `MaxOpenConnections` | `DBStats.WaitCount`, `DBStats.WaitDuration` (cumulative) |
| SQLAlchemy | `engine.pool.checkedout()` vs `pool_size + max_overflow` | time around checkout (below); `TimeoutError: QueuePool limit of size 5 overflow 10 reached` |
| PgBouncer | `SHOW POOLS;` → `cl_waiting` (clients waiting for a server connection) | `maxwait` / `maxwait_us` (oldest waiting client) |
| Python `requests` | "Connection pool is full, discarding connection" warnings (default `pool_maxsize=10` per host, non-blocking, so excess requests open throwaway connections) | connect time |
| httpx / aiohttp | pool timeout errors (httpx default 100 connections; aiohttp `limit=100`) | time to acquire |
| Apache HttpClient 4.x | default 2 connections per route, 20 total: a classic silent bottleneck | lease time |

```python
# SQLAlchemy: measure checkout wait separately from query time
import time
from sqlalchemy import create_engine, text
from prometheus_client import Histogram

POOL_WAIT = Histogram("db_pool_acquire_seconds", "time waiting for a DB connection")
engine = create_engine("postgresql+psycopg://app@db/shop", pool_size=10, max_overflow=0,
                       pool_timeout=2.0)       # fail fast: shorter than the caller's deadline

def fetch_cart(cart_id: int):
    t0 = time.perf_counter()
    with engine.connect() as conn:
        POOL_WAIT.observe(time.perf_counter() - t0)
        return conn.execute(text("SELECT * FROM cart_items WHERE cart_id = :id"), {"id": cart_id}).all()
```

```sql
-- Postgres: what are the connections actually doing?
SELECT application_name, state, count(*),
       max(now() - state_change) AS longest_in_state
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY 1, 2 ORDER BY 3 DESC;

-- Connections holding a transaction open while the app does something else
SELECT pid, application_name, now() - xact_start AS xact_age, left(query, 60) AS last_query
FROM pg_stat_activity
WHERE state = 'idle in transaction' AND now() - xact_start > interval '5 seconds'
ORDER BY xact_age DESC;
```

```text
-- PgBouncer admin console (psql -p 6432 pgbouncer)
SHOW POOLS;   -- per database/user: cl_active, cl_waiting, sv_active, sv_idle, maxwait
SHOW STATS;   -- per database: query and transaction counts and times
```

### 13.3 Pool sizing math

Use Little's Law on the *hold time* (from checkout to return, including any work done while
holding the connection):

```
connections busy on average  = λ × hold_time
                             = 400 req/s × 25 ms = 10
pool_size                    ≈ busy × (1.5 – 2) for bursts        → 15 – 20 per pod
total connections to the DB  = pool_size × pods (× surge during rollouts)
```

Then check the database side. With 40 pods × 20 = 800 connections, a rolling deploy with 25%
surge briefly has 50 pods × 20 = 1,000, which may exceed Postgres `max_connections`. More
connections are not more throughput: a database with 16 cores does its best work with a few dozen
concurrently *active* queries. HikariCP's guidance starts from roughly `cores × 2 + effective
spindles` active connections at the database; beyond that, extra connections mostly add lock and
CPU contention. Put a pooler (PgBouncer in transaction mode, RDS Proxy) between many pods and one
database, and size the pooler's server-side pool for the database.

**Do not "fix" pool exhaustion by enlarging the pool** until you know the hold time. If connections
are held for 2 s because of an HTTP call inside a transaction, a bigger pool just moves the queue
into the database and adds lock contention.

### 13.4 Connection storms after a deploy

New pods start with empty pools and open connections at once, each paying TCP, TLS and
authentication (SCRAM authentication and a new backend process per connection make this expensive
for Postgres). Symptoms: database CPU spikes during every rollout, connection errors or timeouts
on new pods for the first seconds, `max_connections` errors. Fixes: a pooler in front of the
database, a small minimum-idle setting with gradual growth, slower rollouts, jittered warm-up, and
readiness probes that wait until the pool is warm.

### 13.5 Fixes, in the order to try them

1. Stop holding connections across remote calls, sleeps or user think time; keep transactions short.
2. Fix the slow queries that inflate hold time (§15).
3. Set the acquire timeout below the caller's deadline, so waiters fail fast and visibly.
4. Size by Little's Law, cap total connections at the database, use a pooler.
5. Turn on leak detection (HikariCP `leakDetectionThreshold`; in SQLAlchemy, pool `checkout` / `checkin`
   events that flag long checkouts) and a server-side `idle_in_transaction_session_timeout`.

Connection pools from the network side: [`17-networking-protocols-and-communication.md`](17-networking-protocols-and-communication.md) §10;
pool observability: [`../sre-observability/23-database-observability.md`](../sre-observability/23-database-observability.md) §6.

---

## 14. Thread pools, worker pools, and event-loop blocking

> **In plain words.** Servers do work with a fixed number of workers: threads, processes, or one
> event loop that juggles many requests. When every worker is stuck waiting (for a connection, a
> lock, a slow dependency), new requests queue. When one piece of code blocks an event loop, every
> request on that loop stops at once.
>
> **Real-world example.** A FastAPI service's p99 jumps to 2 s at 15% CPU. `py-spy dump` shows the
> event loop thread inside `requests.get(...)`: someone added a synchronous HTTP call in an
> `async def` endpoint. While it waits, the loop serves nobody.

### 14.1 Thread and worker pool exhaustion

Pools stack. In the running example, 64 request threads were all waiting for 10 DB connections, so
the thread pool was also exhausted, so requests queued for threads (§8), so the gateway timed out
and retried. The pool that is *exhausted first* is the cause; the others are symptoms. Find it with
a stack dump, which shows what every worker is waiting on:

```bash
# JVM: count thread states and the frames they are stuck in
jcmd <pid> Thread.print > threads.txt
grep -A 2 '"http-nio' threads.txt | grep -E 'State|at ' | sort | uniq -c | sort -rn | head
#   64    java.lang.Thread.State: TIMED_WAITING (parking)
#   64    at com.zaxxer.hikari.pool.HikariPool.getConnection(...)      ← all waiting for the DB pool

# Go: goroutines grouped by identical stack, with counts
curl -s 'localhost:6060/debug/pprof/goroutine?debug=1' | grep -E '^[0-9]+ @' | sort -rn | head

# Python: every thread's stack, without stopping the process
py-spy dump --pid <pid>
```

Gunicorn sync workers handle one request each; when all are busy the listen backlog fills (§8.3),
and a worker exceeding `--timeout` is killed with `WORKER TIMEOUT` in the logs (and usually no
access log line for that request).

**Pool-induced deadlock.** A task running in pool P submits a sub-task to the same pool P and
waits for it. Under load every thread is a parent waiting for a child that cannot be scheduled.
Symptom: throughput drops to zero, CPU drops to zero, stack dumps show all threads waiting on
futures from the same executor. Fix: separate pools for parent and child work, or non-blocking
composition.

**Metrics.** Active threads vs max and queue size for each executor (`executor_active_threads`,
`executor_queued_tasks` in Micrometer; `tomcat_threads_busy_threads` vs `tomcat_threads_config_max_threads`).
Bulkheads (separate pools per dependency) keep one slow dependency from taking all the threads:
[`33-resilience-patterns-circuit-breakers.md`](33-resilience-patterns-circuit-breakers.md) §4.

### 14.2 The GIL and CPU-bound Python

In CPython (without the free-threaded build), one thread holds the Global Interpreter Lock at a
time. A CPU-heavy thread (JSON of a 20 MB payload, a pandas transformation) slows every other
thread in the process, including the ones that only do I/O. `py-spy top --pid <pid> --gil` or
`py-spy record --gil` shows which code holds the GIL. Fix: move CPU-bound work to processes, or
to a separate service. Details: [`../sre-observability/42-python-observability.md`](../sre-observability/42-python-observability.md) §10.

### 14.3 Event-loop blocking (asyncio, Node.js)

An event loop runs one callback at a time. Any synchronous call that takes 500 ms (a blocking HTTP
client, `time.sleep`, a synchronous database driver, heavy CPU work, synchronous DNS resolution,
reading a large file) delays every other request on that loop by 500 ms.

**The fingerprint:** all concurrent requests on one process slow by the same amount at the same
time, at low overall CPU (if the blocking call is I/O) or 100% of one core (if it is CPU).

**How to detect.**

```python
# Loop-lag monitor: how late does a timer fire? Export as a histogram in production.
import asyncio, time, logging

async def loop_lag_monitor(interval: float = 0.1, warn: float = 0.05):
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        lag = time.perf_counter() - t0 - interval
        if lag > warn:
            logging.warning("event loop lag %.0f ms", lag * 1000)
```

- asyncio debug mode (`PYTHONASYNCIODEBUG=1` or `asyncio.run(main(), debug=True)`) logs every
  callback slower than `loop.slow_callback_duration` (100 ms by default) with the task name. It
  has overhead; use it in staging or on one canary pod.
- `py-spy dump --pid <pid>` during a stall shows the loop thread inside the blocking call.
- Node.js: `perf_hooks.monitorEventLoopDelay()` gives a histogram of loop delay.

**Fixes.** Use async clients (`httpx.AsyncClient`, `asyncpg`, `aiohttp`); push unavoidable blocking
calls to threads with `await asyncio.to_thread(fn, ...)` (which copies context, §5.3); push CPU
work to a process pool. Know the implicit pools: `run_in_executor(None, ...)` uses a default
`ThreadPoolExecutor` with `min(32, os.cpu_count() + 4)` workers, and Starlette/FastAPI run plain
`def` endpoints in a thread pool limited to 40 threads by default, so a slow sync endpoint can
exhaust it and queue unrelated sync endpoints.

---

## 15. Database contention — slow queries, wait events, N+1

> **In plain words.** When the database is the bottleneck, ask it what its sessions are waiting
> for right now, which statements use the most total time, and how a specific slow statement is
> executed. Three views answer those: `pg_stat_activity`, `pg_stat_statements`, and `EXPLAIN
> (ANALYZE, BUFFERS)`.
>
> **Real-world example.** Checkout slows down after a release. `pg_stat_statements` shows a new
> statement with 4 million calls per hour, 0.4 ms mean, one row per call: an ORM change turned one
> query per order into one query per order line (N+1). Each query is fast; there are 40 of them per
> request.

### 15.1 What are sessions waiting for right now?

```sql
SELECT wait_event_type, wait_event, state, count(*)
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY 1, 2, 3
ORDER BY 4 DESC;
```

| `wait_event_type : wait_event` | Meaning | Look next at |
|---|---|---|
| (NULL) with `state = active` | running on CPU | CPU-heavy statements in `pg_stat_statements`; plans |
| `Lock : transactionid`, `Lock : tuple` | waiting for another transaction's row lock | §16 blocking tree; hot rows |
| `Lock : relation` | waiting for a table-level lock (often DDL or a lock queued behind DDL) | §16.2 |
| `LWLock : *` (e.g. `LockManager`, `BufferMapping`, `WALWrite`) | contention on internal shared structures | very high concurrency; too many connections; WAL write speed |
| `IO : DataFileRead` | reading pages from disk (cache miss) | missing index, working set larger than memory, cold cache after failover |
| `IO : WALSync` / `WALWrite` | commits waiting for WAL flush | disk latency; commit rate; `synchronous_commit` |
| `Client : ClientRead` with `state = idle in transaction` | the database waits for the *application* mid-transaction | app code between queries: remote calls, slow loops (§13.1) |
| `IPC : SyncRep` | commit waiting for a synchronous replica | replica or network latency |

Sample it repeatedly (every second for a minute) and count; a single snapshot can mislead.
Wait-event monitoring in depth: [`../sre-observability/23-database-observability.md`](../sre-observability/23-database-observability.md) §8.

### 15.2 Which statements cost the most?

```sql
SELECT queryid, calls,
       round(total_exec_time::numeric / 1000, 1)                       AS total_s,
       round(mean_exec_time::numeric, 2)                               AS mean_ms,
       round(stddev_exec_time::numeric, 2)                             AS stddev_ms,
       rows / nullif(calls, 0)                                         AS rows_per_call,
       round(100.0 * shared_blks_hit / nullif(shared_blks_hit + shared_blks_read, 0), 1) AS hit_pct,
       left(regexp_replace(query, '\s+', ' ', 'g'), 80)               AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 15;
```

How it lies: the counters are cumulative since the last reset, so a statement that became slow an
hour ago is diluted by weeks of history. Take two snapshots and subtract, or look at the rate in
your metrics system. The mean hides bimodal behaviour; `stddev_exec_time` and `max_exec_time` hint
at it. Execution time excludes time waiting for a connection in the client (§13) and network time.

Reading the result:

- **Top by total time** is where the database spends its effort; fixing the top three is usually
  most of the win.
- **Huge `calls`, small mean, 1 row per call** is N+1 (confirm in a trace: a staircase of identical
  spans).
- **Low `hit_pct`** means reads from disk: missing index or a working set bigger than memory.
- **High mean and high rows** may be a legitimate report query running on the primary at peak.

### 15.3 How is one statement executed?

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT ... ;          -- runs the statement for real
BEGIN; EXPLAIN (ANALYZE, BUFFERS) UPDATE ... ; ROLLBACK;   -- for writes, inside a rolled-back transaction
```

What to look for: estimated vs actual row counts that differ by orders of magnitude (stale
statistics; run `ANALYZE`), `Seq Scan` with a large `Rows Removed by Filter` (missing index), a
nested loop with a large `loops` count, `Buffers: shared read=` in the millions (disk), and sorts or
hashes spilling to disk (`external merge`). Capture plans of slow statements automatically with
`auto_explain` (`auto_explain.log_min_duration = '500ms'`; `log_analyze` adds overhead) and log slow
statements with `log_min_duration_statement`.

**Plan flips.** A query that was fast yesterday and slow today with no code change usually changed
plan: statistics were refreshed after a table crossed a size, or a prepared statement switched from
custom to a generic plan (Postgres considers a generic plan after five executions of a prepared
statement; `plan_cache_mode` controls this). Compare today's plan to a known-good one.

```sql
-- Tables read mostly by sequential scans: candidates for a missing index
SELECT relname, seq_scan, seq_tup_read, idx_scan, n_live_tup,
       seq_tup_read / nullif(seq_scan, 0) AS avg_rows_per_seq_scan
FROM pg_stat_user_tables
ORDER BY seq_tup_read DESC
LIMIT 10;
```

### 15.4 The fix

N+1: fetch in one query (join, `WHERE id = ANY($1)`, ORM eager loading such as SQLAlchemy
`selectinload`). Missing index: add it (`CREATE INDEX CONCURRENTLY` to avoid blocking writes).
Plan flip: refresh statistics, raise the statistics target for skewed columns, and as a last resort
pin behaviour. Expensive reads: move to a replica (with §10 in mind) or a cache. Deeper SQL
performance material: [`../databases/15-sql-performance-deep-dive.md`](../databases/15-sql-performance-deep-dive.md).

---

## 16. Lock contention — row locks, lock queues, advisory locks, mutexes

> **In plain words.** A lock lets one piece of work at a time touch something. If the work holding
> the lock is slow, or if everyone needs the same lock, everyone else waits in line. Lock waits
> look like "the database is slow" or "the service is slow" with idle CPUs. The key question is
> always: who holds the lock, and why are they holding it so long?
>
> **Real-world example.** A migration runs `ALTER TABLE orders ADD COLUMN ...`. It needs an
> exclusive lock, and waits behind a 10-minute analytics transaction. While it waits in line, every
> new query on `orders`, even plain reads, queues behind the migration. The site is down, and the
> migration itself has not done anything yet.

### 16.1 Who is blocking whom (Postgres)

`pg_blocking_pids(pid)` returns the sessions a given session is waiting for. This query prints the
blocking tree, roots first (the roots are what you kill or fix):

```sql
WITH RECURSIVE edges AS (
  SELECT pid AS waiter, unnest(pg_blocking_pids(pid)) AS blocker
  FROM pg_stat_activity
),
roots AS (
  SELECT DISTINCT blocker AS pid FROM edges
  WHERE blocker NOT IN (SELECT waiter FROM edges)
),
tree AS (
  SELECT pid, pid AS root, 0 AS depth, ARRAY[pid] AS path FROM roots
  UNION ALL
  SELECT e.waiter, t.root, t.depth + 1, t.path || e.waiter
  FROM tree t JOIN edges e ON e.blocker = t.pid
  WHERE e.waiter <> ALL (t.path)
)
SELECT repeat('  ', t.depth) || t.pid                  AS pid_tree,
       a.state,
       a.wait_event_type || ':' || a.wait_event         AS waiting_on,
       date_trunc('second', now() - a.xact_start)       AS xact_age,
       left(regexp_replace(a.query, '\s+', ' ', 'g'), 60) AS query
FROM tree t
JOIN pg_stat_activity a ON a.pid = t.pid
ORDER BY t.root, t.path;
```

Output from the §23 sandbox (a transaction holding a row lock, a migration queued behind it, and
two ordinary queries queued behind the migration):

```
 pid_tree | state  |   waiting_on    | xact_age |                 query
----------+--------+-----------------+----------+----------------------------------------------
 2565     | active | Timeout:PgSleep | 00:00:03 | begin; update accounts set balance = balance - 1 where id =
   2576   | active | Lock:relation   | 00:00:02 | alter table accounts add column note text;
     2582 | active | Lock:relation   | 00:00:01 | select count(*) from accounts;
     2583 | active | Lock:relation   | 00:00:01 | update accounts set balance = 0 where id = 1;
```

The `SELECT` does not conflict with the row lock held by 2565. It waits because it is queued
behind the `ALTER TABLE`'s `AccessExclusiveLock` request. That lock-queue effect is why DDL on a
busy table can take a site down while the DDL itself is blocked.

To end the incident: `SELECT pg_cancel_backend(pid)` cancels the current statement;
`pg_terminate_backend(pid)` ends the session (and rolls back its transaction). Cancel the root
blocker or the queued DDL, whichever is safer to lose.

### 16.2 Patterns and their causes

| Pattern | Cause | Fix |
|---|---|---|
| Many sessions `Lock:relation` behind one DDL | lock queue behind a migration waiting for a long transaction | `SET lock_timeout = '2s'` in migrations and retry; never run DDL while long transactions are open; `CREATE INDEX CONCURRENTLY` |
| Many sessions `Lock:transactionid` on one row | hot row: a counter, a balance, an inventory item every request updates | shard the counter into N rows and sum; append-only ledger plus aggregation; move work out of the transaction |
| Root blocker is `idle in transaction` | application holds a transaction open while doing something else | shorter transactions; `idle_in_transaction_session_timeout` |
| `deadlock detected` errors | two transactions lock the same rows in opposite orders | consistent lock order (sort IDs before locking); retry the victim |
| Queue-table workers blocking each other | `SELECT ... FOR UPDATE` on the same "next job" row | `FOR UPDATE SKIP LOCKED` |

A hot row's throughput is bounded by its lock hold time, independent of hardware: if every
transaction holds the row lock for 5 ms (including a network round trip from the app between the
`UPDATE` and the `COMMIT`), that row supports at most 200 updates per second.

**Make lock waits visible permanently.** `log_lock_waits = on` logs any wait longer than
`deadlock_timeout` (1 s by default), naming the holder and the queue:

```
LOG:  process 12345 still waiting for ShareLock on transaction 987654 after 1000.123 ms
DETAIL:  Process holding the lock: 12001. Wait queue: 12345, 12346, 12350.
```

`pg_stat_database.deadlocks` counts deadlocks per database.

### 16.3 Advisory locks

Application-defined locks (`pg_advisory_lock(key)`) appear in `pg_locks` with
`locktype = 'advisory'`. Two traps: session-level advisory locks survive the end of a transaction,
so a code path that forgets to unlock holds the lock until the connection closes, and with a
connection pool that can be forever; and PgBouncer in transaction-pooling mode hands each
transaction a possibly different server connection, so session-level advisory locks do not work
as intended there (use the transaction-level `pg_advisory_xact_lock`).

```sql
SELECT l.pid, l.objid, l.mode, l.granted, a.application_name, a.state, now() - a.state_change AS in_state
FROM pg_locks l JOIN pg_stat_activity a USING (pid)
WHERE l.locktype = 'advisory'
ORDER BY l.granted DESC, in_state DESC;
```

### 16.4 Application mutexes and lock convoys

In-process locks create the same queues without a database to ask. Tools: Go's mutex and block
profiles (`runtime.SetMutexProfileFraction`, `runtime.SetBlockProfileRate`, then
`/debug/pprof/mutex`), Java thread dumps (threads `BLOCKED` on the same monitor) and JFR lock
events, Python `py-spy dump` (many threads in `acquire`).

A **lock convoy** forms when a heavily used lock's holder is delayed (preempted, throttled, paused
by GC, or doing I/O while holding the lock): every other thread queues, and when the lock is
released it is handed over one thread at a time, each handover costing a context switch. Throughput
collapses far below what the work needs. Signature: high context-switch rate, low CPU, stack dumps
full of threads parked on the same lock. Fixes: never do I/O while holding a lock, shrink critical
sections, shard the lock (striped locks), or use lock-free or copy-on-write structures for read-heavy
data. Distributed locks (Redis, etcd, ZooKeeper) add lease expiry and fencing problems:
[`03-consensus-raft-and-distributed-locking.md`](03-consensus-raft-and-distributed-locking.md).
Lock internals: [`../databases/17-latches-and-locks-internals.md`](../databases/17-latches-and-locks-internals.md);
lock contention in SQL: [`../databases/15-sql-performance-deep-dive.md`](../databases/15-sql-performance-deep-dive.md) §7.

---

## 17. Cascading failures — retry storms, timeout mismatch, metastability

> **In plain words.** The worst incidents are made of healthy parts reacting to each other. A
> dependency slows for 30 seconds; callers time out and retry; the retries triple the load on the
> dependency; it stays slow because of the retries; and it keeps failing after the original cause
> is gone. Debugging these means finding the *loop*, not the component.
>
> **Real-world example.** An auth service has a 40-second GC-related brownout. It recovers, but
> login errors stay at 60% for 25 minutes. Every layer (mobile app, gateway, backend) retries 3
> times; the auth service now receives many times its normal load, all its capacity is spent on
> requests whose callers already gave up, and goodput stays near zero until the gateway sheds load.

### 17.1 Retry storms

With `A` attempts per layer and `k` retrying layers, one user request can become `A^k` calls to the
bottom layer: 3 attempts at 3 layers is 27. Retries are harmless when failures are rare and
independent, and multiply load exactly when the dependency is overloaded.

**How to see it.** Compare *attempts* with *unique requests*:

```promql
# Envoy: retries as a share of upstream requests, per upstream cluster
sum by (envoy_cluster_name) (rate(envoy_cluster_upstream_rq_retry[1m]))
  / sum by (envoy_cluster_name) (rate(envoy_cluster_upstream_rq_total[1m]))
```

Without proxy metrics, log the attempt number on every outbound call and every inbound request
(from a header), and graph requests by attempt number. A dependency whose incoming rate rises while
the user-facing rate is flat is being retried at.

Retry math, budgets and jitter: [`33-resilience-patterns-circuit-breakers.md`](33-resilience-patterns-circuit-breakers.md) §2.

### 17.2 Timeout mismatch

```
caller timeout 1 s   ─►  callee keeps working for 5 s (its own DB timeout)  ─►  result discarded
caller retries        ─►  callee now has 2 copies of the same work, then 3
```

When a caller's timeout is shorter than the time the callee may legitimately take, the callee does
work nobody will use, and the caller's retries add more. The running example had this shape:
gateway per-try timeout 2.5 s, order-service pool timeout 30 s.

**How to see it.** Server-side logs show requests completing successfully (status 200) after the
caller's timeout; nginx logs 499 (client closed request); for the same request, the caller's Envoy
logs `UT` (upstream timeout) and the callee's Envoy logs `DC` (downstream disconnected). In traces, the server span ends after the client span ended.

**Fix.** Timeouts should *decrease* down the call chain; propagate the deadline (gRPC deadlines, a
header with the absolute deadline) and have each service refuse to start work, or stop work, when
the remaining budget is too small. Set pool and queue timeouts below the request deadline.
Deadline propagation: [`33-resilience-patterns-circuit-breakers.md`](33-resilience-patterns-circuit-breakers.md) §5.2.

### 17.3 Metastable failures

A metastable failure (Bronson et al., HotOS 2021; Huang et al., OSDI 2022) is a state in which the
system stays overloaded after the trigger is removed, because of a *sustaining effect*: retries,
a cold cache that sends all reads to the database, queues full of requests whose callers left,
clients reconnecting in a synchronized wave, or a GC death spiral (more load, more allocation,
more GC, less throughput).

**The signature.** The trigger ends (the dependency is back, the traffic spike passed) but error
rate and latency do not recover. Throughput of *attempts* is high while **goodput** (requests
completed successfully within their deadline) is low. The queue's oldest-item age is larger than
the callers' timeouts, so everything served is already useless.

**How to break the loop.** Reduce offered load below capacity, not just back to normal: shed load
at the edge, turn off retries (or let retry budgets do it), drop queued requests older than their
deadline, pre-warm caches before sending traffic, then ramp traffic back up. Adding capacity helps
only if it arrives faster than the sustaining effect grows. In depth:
[`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md) §11.

### 17.4 Thundering herds

Many clients doing the same thing at the same moment:

- **Cache expiry stampede**: keys written at the same time with the same TTL expire together; every
  request misses and hits the database. Fix: TTL jitter, request coalescing (singleflight), serve
  stale while one request refreshes.
- **Reconnect storms**: after a failover or a deploy of a stateful service, every client reconnects
  at once (and pays TLS and authentication). Fix: jittered reconnect backoff.
- **Synchronized schedules**: cron at `:00` across a fleet, or clients that retry at exactly 1, 2,
  4, 8 s (exponential backoff without jitter). Latency spikes on a fixed period across all pods at
  the same second are the signature (§19.4).

### 17.5 Reading the timeline of a cascade

In a cascade, every graph turns red within a minute. Order the signals by *when they moved* at the
finest resolution you have: dependency latency, then caller's in-flight and pool pending, then
retry rate, then error rate, then CPU. The first mover is nearest the cause; later movers are the
amplifiers you need to break in the fix.

---

## 18. The network and the host — DNS, TLS, TCP, neighbours, clocks

> **In plain words.** Some latency comes from below the application: name lookups, connection
> setup, lost packets, a shared machine, and clocks that disagree. These leave distinctive marks:
> delays of exactly 5 s, 1 s or 3 s, or 200 ms; problems that follow one node; and timelines where
> effects come before causes.
>
> **Real-world example.** A service's latency histogram has small bumps at +1 s and +3 s. Those are
> the Linux retransmission times for a lost TCP SYN. `nstat` on the callee's nodes shows listen
> queue overflows: the backend's accept queue was full during bursts and the kernel dropped
> incoming SYNs.

### 18.1 DNS

- **The 5-second signature.** The glibc resolver's default timeout is 5 s. A lost UDP DNS packet
  (conntrack races on parallel A/AAAA queries in Kubernetes are a known cause) costs exactly 5 s.
  Latency clusters at `+5 s` point to DNS.
- **Search-list expansion.** Kubernetes pods default to `ndots:5`, so a name like `api.example.com`
  is first tried with each search suffix, producing several NXDOMAIN lookups before the real one.
  Use fully qualified names with a trailing dot, lower `ndots`, or a node-local DNS cache
  ([`../kubernetes/18-dns-and-coredns.md`](../kubernetes/18-dns-and-coredns.md) §3, §13, §17).
- **Caching in the client.** Some runtimes cache DNS answers for a long time (JVM
  `networkaddress.cache.ttl`), so after a failover clients keep connecting to the old address.
  Others do not cache at all and resolve on every connection.

```bash
time getent hosts orders.shop.svc.cluster.local     # resolution as the application sees it (uses resolv.conf)
dig +search +stats orders                           # shows the query time and which name matched
```

In CoreDNS, `coredns_dns_request_duration_seconds` and the forward plugin's metrics show resolver
latency; DNS observability: [`../sre-observability/24-network-observability.md`](../sre-observability/24-network-observability.md) §8.

### 18.2 TLS handshakes

A new TLS connection costs one round trip for TCP plus one (TLS 1.3) or two (TLS 1.2) for the
handshake, plus certificate signature work on the server. At 70 ms RTT across regions, a new
connection costs 140–210 ms before the first request byte. Symptoms: latency and CPU spikes on TLS
terminators after deploys, failovers or LB changes, when every connection is new; high latency
only on "first request" paths; clients that open a connection per request. Measure with
`curl -w` (`time_appconnect − time_connect`), proxy handshake counters, and the ratio of new
connections to requests. Fix: connection reuse (keep-alive, HTTP/2), session resumption, TLS 1.3,
ECDSA certificates (cheaper to sign than RSA), and in a service mesh, awareness that each hop adds
mTLS. TLS in depth: [`17-networking-protocols-and-communication.md`](17-networking-protocols-and-communication.md) §9.

### 18.3 TCP retransmits, SYN backlog, and resource limits

| Signature | Mechanism | Check |
|---|---|---|
| latency bumps at +200 ms, +600 ms | retransmission of a lost data segment (Linux minimum RTO 200 ms, doubling) | `nstat -az TcpRetransSegs`; `ss -ti dst <ip>` shows `retrans:` and `rto:` per connection |
| connect latency at +1 s, +3 s, +7 s | lost SYN or SYN-ACK: initial RTO 1 s, doubling | `nstat -az TcpExtTCPSynRetrans`; `TcpExtListenOverflows`, `TcpExtListenDrops` on the server |
| connection refused / reset under bursts | accept queue full | `ss -ltn` Recv-Q near Send-Q; raise the app's backlog and `net.core.somaxconn` (default 4096 since Linux 5.4, 128 before) |
| "Cannot assign requested address" | ephemeral port exhaustion to one destination (many short connections in TIME_WAIT) | `ss -tan state time-wait \| wc -l`; reuse connections |
| random drops on busy nodes | conntrack table full | `dmesg` "nf_conntrack: table full, dropping packet"; `conntrack -S` |
| drops at cloud instance limits | per-instance bandwidth, PPS or connection-tracking allowances | on AWS ENA: `ethtool -S eth0 \| grep allowance_exceeded` |

```bash
ss -s                                   # socket summary: established, time-wait, orphaned
ss -ti 'dst 10.0.3.7'                   # per-connection rtt, rto, cwnd, retrans counters
nstat -az | grep -E 'TcpRetransSegs|TcpExtTCPSynRetrans|ListenOverflows|ListenDrops'
```

node_exporter exposes the same counters (`node_netstat_Tcp_RetransSegs`,
`node_netstat_TcpExt_ListenOverflows`). A retransmit rate that rises only on nodes in one zone is a
network problem in that zone (§19.2). Network observability in depth:
[`../sre-observability/24-network-observability.md`](../sre-observability/24-network-observability.md) §3–§5;
TCP fundamentals: [`17-networking-protocols-and-communication.md`](17-networking-protocols-and-communication.md) §1 and §13.

### 18.4 Noisy neighbours

Services sharing a node share CPU caches, memory bandwidth, disk and network. The signature is
that the problem **follows the host, not the service**: several unrelated pods on one node are
slow, and the same service's pods on other nodes are fine.

```bash
kubectl get pods -A -o wide --field-selector spec.nodeName=ip-10-0-2-23   # who else lives here?
```

Check the node's CPU (steal, §12.3), disk (`iostat -x 1`: `await`, `%util`; cloud volume burst
credits such as AWS gp2 `BurstBalance`), and network allowances (§18.3). Mitigation is to drain
or cordon the node; the durable fix is requests and limits that reflect reality, separate node
pools for noisy workloads, or dedicated hosts.

### 18.5 Clock skew

Every cross-host timestamp comparison assumes synchronized clocks. When a host's clock is off by
`θ`, its log lines and span starts move by `θ` relative to everyone else's.

**How it lies.** Child spans that start before their parents; negative gaps; log lines from a
downstream service that appear before the upstream request was sent; queue-time headers (§3.3)
that are negative or impossibly large on one host; token validation failures (`exp`, `nbf`) or
"certificate not yet valid" errors on one node; distributed locks or leases that expire early on a
host whose clock runs fast.

**How to measure.**

```bash
chronyc tracking        # "System time" offset from NTP, "Last offset", leap status
timedatectl status      # "System clock synchronized: yes/no"
```

```promql
max by (instance) (abs(node_timex_offset_seconds))    # current offset per host
min by (instance) (node_timex_sync_status)            # 0 = not synchronized
```

**The fix.** Run chrony or NTP everywhere and alert on offset and on loss of sync. Measure
durations with monotonic clocks on one host (`time.perf_counter()`, `System.nanoTime()`, Go's
monotonic readings), never by subtracting wall-clock timestamps from two hosts. For ordering events
across hosts use causal metadata (trace parent links, Lamport or hybrid logical clocks), not wall
time: [`00-primitives-and-system-models.md`](00-primitives-and-system-models.md).

---
## 19. Worked investigations

> **In plain words.** Each case below follows the method in §2: symptom, evidence, root cause, fix.
> Read them as patterns: the same few mechanisms (queues, pools, locks, retries, pauses, skew)
> combine differently each time.

All cases in this section are **illustrative composites**: realistic mechanisms with invented,
internally consistent numbers. They are not reports of specific incidents. Public postmortems are
in §22.

**Quick index:** 8-second request, green dashboards → 19.1 · p99 up in one AZ only → 19.2 · lag
grows on one partition → 19.3 · latency spikes every 10 minutes → 19.4 · CPU at 40% but slow →
19.5 · outage right after a migration → 19.6 · errors persist after a dependency recovers → 19.7 ·
"the save button doesn't work" → 19.8 · async service slow at low CPU → 19.9

### 19.1 The 8-second checkout with green dashboards

- **Symptom.** About 0.3% of checkouts take 6–9 s at the edge since 14:00 UTC. Every service's p99
  panel is under 100 ms. Error rate is flat.
- **Evidence.** An edge access log line for a slow request gives a request ID. The gateway log has
  two upstream attempts: attempt 1 ended at 2.5 s with flag `UT`, attempt 2 took 5.1 s. The trace
  for attempt 2 shows 0.6 s between the gateway's client span and order-service's server span, then
  3.9 s with no child spans before the first SQL span. `hikaricp_connections_pending` on that pod
  peaks at 38 that minute; `pg_stat_activity` has 10 connections from the pod, 4 of them
  `idle in transaction` for 1–2 s each. Their `application_name` and SQL comments point to the
  `apply_promo` code path, which since the 13:55 deploy calls an external promotions API inside the
  transaction.
- **Root cause.** A deploy moved an external HTTP call inside a DB transaction. Connection hold
  time rose from 20 ms to over 1 s for promo checkouts, exhausting the 10-connection pool; 64 request
  threads blocked on the pool; requests queued for threads; the gateway's 2.5 s per-try timeout fired
  and retried, adding load.
- **Fix.** Mitigation: disable the promotions feature flag. Durable: move the external call outside
  the transaction; set the pool acquire timeout to 1 s (below the gateway timeout); make order-service
  reject requests whose `X-Request-Start` age exceeds the gateway timeout; add a pool-wait span; add
  panels for p99.9, pool pending, and queue time.
- **Lesson.** The time was spread across a queue, a retry, a pool, and fan-out. No single component
  was "slow", and the component that changed (the promotions call) did not appear in the slowest span.

### 19.2 p99 spike only in one availability zone

- **Symptom.** p99 for all services in `eu-west-1b` rose from 120 ms to 900 ms at 09:12. Other zones
  are normal. No deploys.
- **Evidence.** Slicing by zone isolates 1b; slicing by service shows *every* service in 1b affected,
  so it is not one application. The latency histogram has new bumps near +200 ms and +600 ms.
  `node_netstat_Tcp_RetransSegs` rate on 1b nodes is 40 times the other zones; `ss -ti` on a 1b pod
  shows `retrans:` counters climbing on connections to other zones as well as within the zone. The
  cloud provider's status page, 20 minutes later, reports packet loss in one zone.
- **Root cause.** Partial packet loss in the zone's network. Every lost segment costs at least the
  200 ms minimum RTO; a second loss on the same segment costs 400 ms more.
- **Fix.** Mitigation: shift traffic away from the zone (a zonal shift at the load balancer, scaling
  up the other zones). Durable: enough capacity in N−1 zones to absorb a zonal evacuation, and a
  runbook step that checks retransmits by zone.
- **Lesson.** "All services in one place" means infrastructure. Latency modes at fixed offsets
  (200 ms, 1 s, 3 s, 5 s) name the mechanism.

### 19.3 Consumer lag growing on one partition

- **Symptom.** Invoice emails for some customers arrive hours late. Total consumer lag is growing
  slowly.
- **Evidence.** `kafka-consumer-groups.sh --describe` twice, 60 s apart: partitions 0–5 and 8–11 have
  lag under 100 and advancing offsets. Partition 7's `CURRENT-OFFSET` is identical in both runs;
  its lag equals its produce rate multiplied by the time since 06:40. The consumer that owns it logs
  the same `SchemaException` for the same offset every 2 seconds. Other messages on partition 7 are
  stuck behind it because a partition is processed in order.
- **Root cause.** A producer released at 06:38 wrote one event with a field the consumer's schema
  cannot parse. The consumer retried it forever.
- **Fix.** Mitigation: publish the bad record to a dead-letter topic and advance past it (or deploy
  a consumer that tolerates the field). Durable: bounded retries then DLQ, schema compatibility
  checks in the producer's CI, alerts on per-partition *time* lag and on "offset unchanged for 5
  minutes while lag > 0".
- **Lesson.** Aggregate lag hid a stuck partition. Per-partition, offset-advancing checks find it
  in one minute.

### 19.4 Latency sawtooth every 10 minutes

- **Symptom.** p99 spikes from 150 ms to 1.4 s for about 30 s at :00, :10, :20 ... every hour.
- **Evidence.** The key question is whether all pods spike **at the same second** (a shared trigger)
  or at different times (a per-process cause such as GC). Here they all spike at the same second,
  aligned to the wall clock, which rules out GC (per-process heaps are not synchronized). Database
  queries per second triple during each spike; cache hit ratio drops from 97% to 40%. The cache
  client sets a fixed 600 s TTL on product data, and the cache is warmed by a job at deploy time, so
  all keys were written in the same minute and expire together.
- **Root cause.** Synchronized cache expiry causing a stampede to the database every 10 minutes.
- **Fix.** Add ±20% jitter to TTLs, coalesce concurrent misses for the same key (singleflight), and
  serve stale data while one request refreshes.
- **Lesson.** Periodic spikes: aligned to wall-clock boundaries → cron, TTLs, scheduled jobs,
  compaction schedules; aligned to process uptime and different per pod → GC, leaks, per-process
  timers.

### 19.5 CPU at 40% but requests are slow

- **Symptom.** After moving to a new node pool with larger nodes, a Go service's p99 rose from 80 ms
  to 350 ms. CPU usage is 0.8 cores against a 2-core limit.
- **Evidence.** Throttle ratio (`container_cpu_cfs_throttled_periods_total` / `container_cpu_cfs_periods_total`)
  is 35% on every pod; on the old pool it was 2%. `cpu.stat` inside a pod confirms `nr_throttled`
  rising. The new nodes have 32 cores instead of 8. The service runs on an older Go version, so
  `GOMAXPROCS` defaulted to 32, the number of host cores, and the runtime ran 32 threads in parallel
  bursts that spent the 200 ms quota in about 6 ms of each 100 ms period.
- **Root cause.** CFS quota with a runtime sized for the host instead of the container.
- **Fix.** Set `GOMAXPROCS` from the container limit (or upgrade to a container-aware Go version);
  for this latency-critical service, remove the CPU limit and set the request to observed usage at
  p95.
- **Lesson.** Average usage cannot show throttling. Check `nr_throttled` whenever latency and CPU
  disagree.

### 19.6 Site down right after a migration

- **Symptom.** At 11:02, during a deploy, every endpoint that touches `orders` times out; the
  database's active connection count goes to the maximum.
- **Evidence.** `pg_stat_activity` grouped by wait event: 480 sessions in `Lock:relation`. The
  blocking-tree query (§16.1) shows one root: a reporting transaction open for 14 minutes in
  `idle in transaction` holding an `AccessShareLock` on `orders`. Under it, the migration's
  `ALTER TABLE orders ADD COLUMN ...` waits for `AccessExclusiveLock`; under the migration, 479
  application queries wait in the lock queue.
- **Root cause.** A DDL statement queued behind a long transaction and blocked all later queries on
  the table. The migration had no `lock_timeout`.
- **Fix.** Mitigation: cancel the migration (`pg_cancel_backend`), which releases the queue at once;
  terminate the idle reporting session. Durable: `SET lock_timeout = '3s'` in every migration with
  retries; `idle_in_transaction_session_timeout` on the database; run reports on a replica.
- **Lesson.** The blocked statement (the DDL) is not the root; the root is the oldest transaction
  at the top of the blocking tree.

### 19.7 Errors persist after a dependency blip

- **Symptom.** The auth service was unhealthy for 40 s at 16:20. It has been healthy since 16:21
  by its own metrics, yet login errors at the edge stay at 55% until 16:47.
- **Evidence.** Requests arriving at auth are 6 times normal while edge login attempts are 1.3
  times normal; the retry share at the gateway is 70%. Auth's queue-time histogram shows p50 of 4 s,
  above the gateway's 2 s timeout, so almost every request auth completes has already been abandoned.
  Goodput (successful responses returned before the caller's deadline) is near zero while auth's own
  success rate is 99%.
- **Root cause.** Metastable failure. The trigger was brief; the sustaining effect was retries at
  three layers plus an unbounded queue of abandoned requests.
- **Fix.** Mitigation: shed 50% of login traffic at the edge for 2 minutes, disable gateway retries
  to auth, restart auth pods to drop their queues, then restore traffic gradually. Durable: retry
  budgets, deadline propagation with dequeue-time deadline checks, a bounded queue, and adaptive
  concurrency limits ([`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md) §6).
- **Lesson.** "The dependency is healthy" and "the system is healthy" are different claims. Measure
  goodput and queue age.

### 19.8 "The save button doesn't work"

- **Symptom.** Support tickets: users change their shipping address, click save, and the page shows
  the old address. It happens mostly between 02:00 and 02:30 UTC, and mostly for users in Asia.
- **Evidence.** The write API logs success for every report. The profile page reads from a replica.
  `pg_stat_replication.replay_lag` on the Asia-routed replica reaches 12–15 s during a nightly
  batch that updates 40 million rows; other hours it is under 100 ms.
- **Root cause.** Replica lag during a bulk job, plus reads routed to the replica immediately after
  a write.
- **Fix.** Read-your-writes: after a user writes, serve that user's reads from the primary for 30 s
  (or wait for the replica to reach the write's LSN). Throttle the batch job by replica lag.
- **Lesson.** Stale reads look like application bugs. Put replica lag on the same dashboard as
  "write succeeded but not visible" reports.

### 19.9 Async service slow at low CPU

- **Symptom.** A FastAPI service's p99 went from 60 ms to 2.1 s after a release. CPU is 15%. Every
  endpoint is affected, including `/healthz`, and pods fail readiness intermittently.
- **Evidence.** The loop-lag histogram (§14.3) shows p99 lag of 1.9 s. `py-spy dump` on a pod,
  taken three times, shows the event loop thread in `requests/sessions.py` each time, called from a
  new `async def` endpoint that fetches exchange rates from a third-party API with `requests.get`.
  That API has been responding in about 2 s since 10:00.
- **Root cause.** A synchronous HTTP call on the event loop. Each call froze every other request on
  the process for the duration of the third-party call.
- **Fix.** Use `httpx.AsyncClient` with a timeout, cache the exchange rates, and add a lint rule that
  forbids known blocking libraries in async code.
- **Lesson.** "Everything on one process slows together at low CPU" means a blocked event loop or
  a GIL holder. A stack dump settles it in seconds.

---
## 20. Production pitfalls

1. **Trusting p99 panels for rare, severe problems.** A problem affecting 0.3% of requests is
   invisible at p99. Keep p99.9 and max panels, and read individual slow requests.
2. **Measuring latency only in the handler.** Queue time, retries and connection setup never show
   up. Add a queue-time histogram and compare the caller's and the callee's view of the same call.
3. **Averaging percentiles** across pods, time windows or services (§7.2). Aggregate histogram
   buckets, then take the quantile.
4. **Head sampling at 1% as the only tracing.** The slow and failed traces you need are gone.
   Tail-sample errors and slow requests; set `decision_wait` above the slowest request you care about.
5. **Access logs written only at completion.** Hung and killed requests never appear. Log a start
   line for long operations and watch for drops in log volume.
6. **Raising timeouts during an incident.** Longer timeouts hold threads and connections longer and
   convert fast errors into slow ones. Fix the cause or shed load.
7. **Enlarging the connection pool to fix pool exhaustion** without measuring hold time. The queue
   moves into the database.
8. **Remote calls inside database transactions.** Connection hold time and lock hold time become
   the remote call's latency.
9. **Caller timeouts shorter than callee work, without deadline propagation.** The callee does
   abandoned work; retries multiply it.
10. **Retries at every layer.** `A^k` amplification during the exact moments the dependency is
    weakest. Retry at one layer, with a budget.
11. **CPU limits on latency-sensitive, multi-threaded services** with runtimes sized for the host.
    Check `nr_throttled` whenever latency and CPU usage disagree.
12. **Alerting on consumer lag in offsets summed across partitions.** Alert on time lag, per
    partition, and on offsets that stop advancing.
13. **Running DDL without `lock_timeout`** on busy tables.
14. **Restarting everything first.** Evidence gone, caches cold, and synchronized restarts can cause
    their own thundering herd. Grab stack dumps and database snapshots first.
15. **Treating the first red graph as the cause.** In a cascade everything turns red; order signals
    by when they moved.
16. **Observability that depends on the system being debugged** (dashboards behind the same
    service discovery, logs shipped through the saturated network). Keep an out-of-band path.

---

## 21. Interview questions

**Q1. A request took 8 seconds, but every service reports under 100 ms. How do you find where the
time went?**
The dashboards and the user are measuring different things. "Under 100 ms" is usually a p99 of
handler time per attempt: it excludes the top 1%, time before the handler starts, and retries.
I would get the request ID of a slow request, pull its trace and every service's logs for it, and
decompose the 8 s: connection setup (DNS, TCP, TLS), time in the load balancer and in each
service's accept and worker queues (the gap between the caller's client span and the callee's
server span), pool waits and lock waits (gaps inside server spans before child spans), GC pauses
and CPU throttling (gaps across all concurrent requests on one pod), fan-out (the slowest of N
children), and retries (repeated child spans, proxy flags like `UT`). Then I confirm each piece
with its own metric: pool pending, queue time, throttle ratio, GC log. Typically the answer is a
combination, for example a pool wait plus a timeout-and-retry plus queueing.

**Q2. Why can't you average p99 across instances? How do you get a fleet p99?**
A percentile is a property of the whole distribution; the p99s of parts do not contain enough
information to compute the p99 of the whole. Export histograms, sum the bucket counts across
instances, and compute the quantile from the summed histogram
(`histogram_quantile(0.99, sum by (le) (rate(x_bucket[5m])))`). Summaries cannot be aggregated.

**Q3. What is coordinated omission?**
A measuring client that waits for each response before sending the next stops sending during a
stall, so it records one slow sample instead of all the requests that would have arrived. A 2 s
freeze at 100 req/s shows up as one sample, and p99.9 looks perfect. Use open-loop load generators
with a fixed arrival rate and measure latency from the intended send time.

**Q4. A service fans out to 100 backends. Each backend's p99 is 10 ms. What is the service's p99?**
Much worse than 10 ms: the chance that at least one of 100 calls exceeds the backend's p99 is
`1 − 0.99^100 ≈ 63%`, so the service's *median* is already above the backend's p99. To keep the
service's p99 at 10 ms, each backend needs roughly p99.99 ≤ 10 ms. Mitigations: hedged or tied
requests with a budget, partial results after a deadline, and fixing per-backend tail causes.

**Q5. How do you use Little's Law while debugging?**
`L = λW` checks metrics against each other. With in-flight requests from the proxy and the request
rate, `W = L/λ` is the true time in the system; if it is much larger than handler time, requests are
queuing before the handler. It also sizes pools (`λ × hold_time`) and gives drain time for a backlog.

**Q6. Head sampling or tail sampling for debugging latency?**
Tail sampling, because the traces you need are rare and head sampling decides before knowing the
request is slow. Keep all errors and all traces above a latency threshold plus a small random
baseline. It needs all spans of a trace routed to one collector and a decision window longer than
the slowest traces. Always log the trace ID even for unsampled requests.

**Q7. CPU usage is 40% of the limit but latency is bad. What do you check?**
CFS throttling (`nr_throttled / nr_periods` in `cpu.stat`, or the cAdvisor throttled-periods
ratio): a multi-threaded process can exhaust its quota early in each 100 ms period. Then per-core
saturation (one hot thread or IRQ core), run queue and PSI, and steal time on VMs. Fix throttling by
removing or raising limits and sizing runtime parallelism (`GOMAXPROCS`, thread pools) to the quota.

**Q8. Kafka consumer lag is growing on one partition only. What are the likely causes?**
If the committed offset is not moving: a poison message being retried forever or a hung consumer;
check logs for a repeated offset and take a stack dump. If it moves slowly: a hot key sending more
traffic to that partition, or the consumer instance is on a bad host. Check produce rate per
partition and the consumer's host metrics. Fix with a DLQ, key redesign, or moving the consumer.

**Q9. Users report that saved changes sometimes do not appear. What do you suspect?**
Replication lag with reads served from replicas. Correlate reports with replica lag (measured with
a heartbeat), and check if they cluster around batch jobs. Fix with read-your-writes (primary reads
for a short window, or LSN tokens) and throttling bulk writes by lag.

**Q10. The DB connection pool is exhausted. Why not just make it bigger?**
Exhaustion means `λ × hold_time` exceeds the pool. If hold time is inflated (slow queries, remote
calls inside transactions, lock waits), a bigger pool moves the queue into the database, where more
concurrent queries add lock and CPU contention and total throughput can fall. Measure hold time,
fix what inflates it, set the acquire timeout below the request deadline, and cap total connections
with a pooler.

**Q11. The client sees 2% errors; the servers log 0.1%. Where are the rest?**
Errors created between them: client-side timeouts (the server finished later; nginx logs 499),
load-balancer-generated 502/503/504 that never reach the application, connection failures, DNS and
TLS errors, and requests dropped from full queues. Compare error rates layer by layer and read the
proxy's response flags.

**Q12. What is a metastable failure and how do you get out of one?**
The system stays overloaded after the trigger ends because a sustaining effect (retries, cold
cache, a queue of abandoned requests) keeps load above capacity. Signs: dependency healthy, goodput
near zero, queue age above caller timeouts. Recover by cutting load below capacity (shed, disable
retries, drop expired queued work), then ramp traffic back.

**Q13. What do you do in the first 15 minutes of a latency incident?**
Confirm the symptom at the edge with a start time; slice by zone, version, pod, route and tenant;
list recent changes; read one slow trace; grab stack dumps and a `pg_stat_activity` snapshot; sweep
saturation signals (throttling, pool pending, queue depth, lag, lock waits, retries); then choose a
reversible mitigation (rollback, drain, flag, shed) and announce it.

**Q14. Latency spikes every 10 minutes. How do you narrow it down?**
Check whether all instances spike at the same second. If yes, look for a shared, clock-aligned
trigger: cron jobs, TTL expiry, compaction, batch jobs, backups. If each instance spikes at a
different time, look at per-process causes: GC, heap growth, per-process timers, log rotation.

---

## 22. Real-world cases — public postmortems

Only publicly documented incidents and engineering write-ups are listed. Details are summarized
from the organizations' own reports; read the originals for the full timelines.

**22.1 Discord, "Why Discord is switching from Go to Rust" (2020).** Discord described a service
(Read States) with latency and CPU spikes roughly every two minutes. The cause was the Go runtime
forcing a garbage collection at least every two minutes, and each collection scanning a very large
in-memory LRU cache. Tuning the cache size traded one problem for another, and the team rewrote the
service in Rust. *Lesson for this chapter:* periodic spikes with a fixed period that matches a
runtime timer point to GC (§11); GC cost grows with the live heap you keep.

**22.2 Cloudflare, July 2, 2019.** A new WAF rule containing a regular expression that backtracked
catastrophically was deployed globally at once. CPU on the machines serving HTTP and HTTPS traffic
went to nearly 100% worldwide, and customers received 502 errors. *Lesson:* a single code path can
saturate CPU across a fleet; a CPU profile names it immediately (§12), and staged rollouts limit the
blast radius.

**22.3 Cloudflare, January 1, 2017 (leap second).** Cloudflare's RRDNS code computed a duration from
wall-clock time, got a negative value when the leap second was inserted, and code that assumed time
never goes backwards caused failures for some DNS resolutions. Go later added monotonic clock
readings to `time.Time` (Go 1.9). *Lesson:* measure durations with monotonic clocks; wall clocks
jump (§18.5).

**22.4 AWS Kinesis Data Streams, us-east-1, November 25, 2020.** Adding capacity to the Kinesis
front-end fleet made each front-end server exceed the maximum number of operating-system threads
allowed by its configuration, because each server kept a thread per other server in the fleet.
Front-end servers could not build their shard maps, and services that depend on Kinesis (AWS
reported impact on Cognito and CloudWatch, among others) were affected. *Lesson:* saturation is not
only CPU and memory; limits that scale with fleet size (threads, connections, file descriptors) are
resources to monitor with USE (§2, §14).

**22.5 Amazon DynamoDB, us-east-1, September 20, 2015.** After a brief network disruption, many
storage servers requested their partition membership from the metadata service at the same time.
Membership data had grown (partly due to global secondary indexes) so that responses took longer
than the storage servers' timeout; servers retried, keeping the metadata service overloaded and
unable to recover on its own. AWS resolved it by pausing requests to the metadata service and adding
capacity. *Lesson:* a textbook sustaining loop: timeout shorter than the (grown) work plus retries,
a thundering herd after a blip, and recovery by cutting load first (§17).

**22.6 GitHub, October 21, 2018.** A 43-second loss of connectivity between GitHub's US East Coast
network hub and its primary US East Coast data center led the database topology manager to promote
primaries in the US West Coast. When connectivity returned, the East Coast databases held writes
that had not been replicated west, and applications in the East were now writing to primaries across
the country. GitHub chose data integrity over speed and ran degraded for 24 hours and 11 minutes
while it restored and reconciled data. *Lesson:* replication lag and cross-region latency turn a
short network event into a long consistency problem; failover automation must account for them
(§10; [`36-multi-region-active-active-and-geo-replication.md`](36-multi-region-active-active-and-geo-replication.md)).

**22.7 Roblox, October 28–31, 2021.** A 73-hour outage traced to the Consul cluster that backed
service discovery. Roblox's postmortem (written with HashiCorp) identified contention in a newly
enabled Consul streaming feature under very high load, and a pathological performance issue in the
BoltDB storage layer's freelist. Diagnosis was slowed because monitoring and other tooling depended
on the same Consul cluster. *Lesson:* contention inside a shared dependency looks like everything
being slow at once (§16); keep an observability path that does not depend on the system being
debugged (§20).

Further reading on the phenomena rather than incidents: Dean and Barroso, "The Tail at Scale" (CACM
2013) for §7; Bronson et al., "Metastable Failures in Distributed Systems" (HotOS 2021) and Huang et
al., "Metastable Failures in the Wild" (OSDI 2022) for §17.3; Gil Tene, "How NOT to Measure
Latency" for §7.3; Brendan Gregg's USE method for §2.

---

## 23. Sandbox experiments — run these yourself

> **In plain words.** Short scripts that make each hidden delay visible on a laptop: queue time that
> handler metrics miss, a pool that makes a fast query slow, fan-out tails, a load tester that lies,
> a blocked event loop, a lock queue, and CPU throttling.

The Python experiments use only the standard library (Python 3.10+). The printed numbers are from
one run on a laptop; yours will differ slightly, but the shape will not. The Docker experiments
list the expected result rather than a captured one, except where noted.

### 23.1 Queue time is invisible to handler metrics (Little's Law)

Open-loop arrivals (a Poisson process) into a server with 4 workers and a mean service time of
50 ms, so capacity is 80 req/s. Takes about 2 minutes.

```python
# littles_law.py
import asyncio, random, time

C, S, DURATION = 4, 0.050, 30          # workers, mean service time (s), seconds of arrivals

def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]

async def run(rate):
    workers = asyncio.Semaphore(C)
    in_system, area, last = 0, 0.0, time.perf_counter()   # area = integral of L over time
    handler, total = [], []

    def tick(delta):
        nonlocal in_system, area, last
        now = time.perf_counter()
        area += in_system * (now - last)
        last, in_system = now, in_system + delta

    async def request():
        t_arrive = time.perf_counter(); tick(+1)
        async with workers:
            t_start = time.perf_counter()
            await asyncio.sleep(random.expovariate(1 / S))
            t_end = time.perf_counter()
        tick(-1)
        handler.append(t_end - t_start); total.append(t_end - t_arrive)

    t0, tasks = time.perf_counter(), []
    while time.perf_counter() - t0 < DURATION:
        tasks.append(asyncio.create_task(request()))
        await asyncio.sleep(random.expovariate(rate))
    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - t0
    lam, W, L = len(total) / elapsed, sum(total) / len(total), area / elapsed
    print(f"rho={rate * S / C:4.2f}  handler p50/p99={pct(handler, .5)*1e3:4.0f}/{pct(handler, .99)*1e3:4.0f} ms"
          f"  total p50/p99={pct(total, .5)*1e3:5.0f}/{pct(total, .99)*1e3:5.0f} ms"
          f"  L={L:5.1f}  lambda*W={lam * W:5.1f}")

async def main():
    random.seed(7)
    for rate in (40, 60, 72, 76):
        await run(rate)

asyncio.run(main())
```

```
rho=0.50  handler p50/p99=  35/ 233 ms  total p50/p99=   40/  236 ms  L=  2.3  lambda*W=  2.3
rho=0.75  handler p50/p99=  35/ 234 ms  total p50/p99=   54/  273 ms  L=  4.1  lambda*W=  4.1
rho=0.90  handler p50/p99=  36/ 234 ms  total p50/p99=  106/  445 ms  L=  9.7  lambda*W=  9.7
rho=0.95  handler p50/p99=  34/ 243 ms  total p50/p99=  152/  430 ms  L= 12.4  lambda*W= 12.4
```

**What to notice.** The handler histogram is the same at every load; the median time users see
nearly quadruples. `L` measured directly equals `λ·W`. Try `C = 1, S = 0.2` to see the knee sharper.

### 23.2 A fast query behind an exhausted pool

```python
# pool_wait.py -- 50 concurrent requests share a 5-connection pool; each query takes 100 ms.
import asyncio, time

POOL, QUERY, CONCURRENT, ACQUIRE_TIMEOUT = 5, 0.100, 50, 0.6

async def handler(pool, stats):
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(pool.acquire(), ACQUIRE_TIMEOUT)
    except asyncio.TimeoutError:
        stats["timeouts"] += 1
        return
    t1 = time.perf_counter()
    try:
        await asyncio.sleep(QUERY)                 # the "DB span" everyone looks at
    finally:
        pool.release()
    t2 = time.perf_counter()
    stats["db"].append(t2 - t1); stats["wait"].append(t1 - t0); stats["total"].append(t2 - t0)

async def main():
    pool = asyncio.Semaphore(POOL)
    stats = {"db": [], "wait": [], "total": [], "timeouts": 0}
    await asyncio.gather(*(handler(pool, stats) for _ in range(CONCURRENT)))
    for k in ("db", "wait", "total"):
        xs = sorted(stats[k])
        print(f"{k:5s}  n={len(xs):2d}  p50={xs[len(xs)//2]*1e3:4.0f} ms  max={xs[-1]*1e3:4.0f} ms")
    print(f"acquire timeouts: {stats['timeouts']} of {CONCURRENT}")

asyncio.run(main())
```

```
db     n=30  p50= 100 ms  max= 101 ms
wait   n=30  p50= 301 ms  max= 502 ms
total  n=30  p50= 402 ms  max= 603 ms
acquire timeouts: 20 of 50
```

**What to notice.** The query never takes more than 101 ms, yet 40% of requests fail and the rest
wait up to five times the query time. Predict before running: 5 connections × 100 ms serve 50
requests in 10 waves of 100 ms, so any request needing more than 0.6 s of waiting (waves 7–10, i.e.
20 requests) times out.

### 23.3 Fan-out amplification and hedging

```python
# fanout.py
import random

def one_call():   # 98% fast (~10 ms), 2% slow (200-1000 ms)
    return random.uniform(8, 12) if random.random() < 0.98 else random.uniform(200, 1000)

def pct(xs, q):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(q * len(xs)))]

def request(n, hedge_after=None):
    latencies, extra = [], 0
    for _ in range(n):
        first = one_call()
        if hedge_after is not None and first > hedge_after:
            extra += 1
            first = min(first, hedge_after + one_call())   # the backup races the original
        latencies.append(first)
    return max(latencies), extra

random.seed(1)
for n in (1, 10, 100):
    for hedge in (None, 15):
        runs = [request(n, hedge) for _ in range(20_000 // n + 2_000)]
        lat = [r[0] for r in runs]
        extra = sum(r[1] for r in runs) / (len(runs) * n)
        slow = sum(x > 100 for x in lat) / len(lat)
        label = "no hedge " if hedge is None else f"hedge@{hedge}ms"
        print(f"N={n:3d} {label}  p50={pct(lat, .5):4.0f}  p99={pct(lat, .99):4.0f} ms"
              f"  P(>100 ms)={slow:5.1%}  extra calls={extra:4.1%}")
```

```
N=  1 no hedge   p50=  10  p99= 647 ms  P(>100 ms)= 2.2%  extra calls=0.0%
N=  1 hedge@15ms  p50=  10  p99=  25 ms  P(>100 ms)= 0.0%  extra calls=2.1%
N= 10 no hedge   p50=  12  p99= 972 ms  P(>100 ms)=18.4%  extra calls=0.0%
N= 10 hedge@15ms  p50=  12  p99=  27 ms  P(>100 ms)= 0.2%  extra calls=1.9%
N=100 no hedge   p50= 733  p99= 995 ms  P(>100 ms)=86.7%  extra calls=0.0%
N=100 hedge@15ms  p50=  26  p99= 568 ms  P(>100 ms)= 4.0%  extra calls=2.0%
```

**What to notice.** `P(>100 ms)` matches `1 − 0.98^N` (2.0%, 18.3%, 86.7% in theory). At N = 100 the median
request is slow. Hedging costs 2% extra calls here because the backup is *independent* of the
original. Change `one_call` so that slowness is shared (for example, 2% of *time windows* are slow
for every call) and hedging stops helping: that is the check in §7.5.

### 23.4 Coordinated omission

```python
# coordinated_omission.py -- 1 ms per request, except a 2 s freeze at t = 50 s; 100 req/s for 100 s
STALL_START, STALL_LEN, SERVICE, INTERVAL, DURATION = 50.0, 2.0, 0.001, 0.010, 100.0

def finish_time(start):
    if STALL_START <= start < STALL_START + STALL_LEN:
        start = STALL_START + STALL_LEN
    return start + SERVICE

def pct(xs, q):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(q * len(xs)))]

closed, open_, free_at = [], [], 0.0
for i in range(int(DURATION / INTERVAL)):
    scheduled = i * INTERVAL
    sent = max(scheduled, free_at)         # a closed-loop client waits for the previous reply
    done = finish_time(sent)
    free_at = done
    closed.append(done - sent)             # what a naive tool records
    open_.append(done - scheduled)         # what a user arriving on schedule experienced

for name, xs in (("closed-loop (naive)", closed), ("open-loop (corrected)", open_)):
    print(f"{name:22s} p50={pct(xs, .5)*1e3:6.1f} ms  p99={pct(xs, .99)*1e3:7.1f} ms"
          f"  p99.9={pct(xs, .999)*1e3:7.1f} ms  max={max(xs)*1e3:6.0f} ms")
```

```
closed-loop (naive)    p50=   1.0 ms  p99=    1.0 ms  p99.9=    1.0 ms  max=  2001 ms
open-loop (corrected)  p50=   1.0 ms  p99= 1110.0 ms  p99.9= 1920.0 ms  max=  2001 ms
```

### 23.5 One blocking call stalls an event loop

```python
# loop_block.py
import asyncio, time

async def loop_lag_monitor(interval=0.05, report=0.02):
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        if (lag := time.perf_counter() - t0 - interval) > report:
            print(f"  [monitor] event loop lag {lag*1e3:.0f} ms")

async def healthy_request():
    t0 = time.perf_counter()
    await asyncio.sleep(0.01)                  # non-blocking I/O
    return time.perf_counter() - t0

async def bad_request():
    time.sleep(0.5)                            # stands in for requests.get(), a sync driver, big json.loads

async def main(blocking):
    mon = asyncio.create_task(loop_lag_monitor())
    await asyncio.sleep(0.1)
    tasks = [asyncio.create_task(healthy_request()) for _ in range(20)]
    if blocking:
        tasks.append(asyncio.create_task(bad_request()))
    res = [r for r in await asyncio.gather(*tasks) if r is not None]
    mon.cancel()
    print(f"blocking={blocking}: 20 healthy requests, max latency {max(res)*1e3:.0f} ms")

asyncio.run(main(False))
asyncio.run(main(True), debug=True)            # debug mode names the slow task
```

```
blocking=False: 20 healthy requests, max latency 10 ms
Executing <Task finished name='Task-47' coro=<bad_request() done, ...> took 0.500 seconds
  [monitor] event loop lag 454 ms
blocking=True: 20 healthy requests, max latency 503 ms
```

Replace `time.sleep(0.5)` with `await asyncio.to_thread(time.sleep, 0.5)` and the healthy requests
return to 10 ms.

### 23.6 A Postgres lock queue behind a migration

```yaml
# docker-compose.yml
services:
  pg:
    image: postgres:16
    environment: {POSTGRES_PASSWORD: pg}
    ports: ["5432:5432"]
    command: ["postgres", "-c", "log_lock_waits=on", "-c", "deadlock_timeout=1s"]
```

```bash
docker compose up -d
export PGHOST=localhost PGUSER=postgres PGPASSWORD=pg
psql -c "CREATE TABLE accounts(id int PRIMARY KEY, balance int);
         INSERT INTO accounts SELECT g, 100 FROM generate_series(1, 1000) g;"

# terminal 1: a transaction that holds a row lock (plus RowExclusiveLock on the table) for 60 s
psql -c "BEGIN; UPDATE accounts SET balance = balance - 1 WHERE id = 1; SELECT pg_sleep(60); COMMIT;"
# terminal 2: a migration; it needs AccessExclusiveLock and queues
psql -c "ALTER TABLE accounts ADD COLUMN note text;"
# terminal 3: an ordinary read; it does not conflict with terminal 1, but queues behind terminal 2
psql -c "SELECT count(*) FROM accounts;"
# terminal 4: run the blocking-tree query from §16.1, then the wait-event summary from §15.1
```

This experiment was run against PostgreSQL 16; the blocking tree it printed is the one shown in
§16.1. Then repeat terminal 2 with `SET lock_timeout = '2s';` in front of the `ALTER TABLE`: the
migration fails after 2 s, and the read in terminal 3 is no longer blocked. `docker compose logs pg`
shows the `still waiting for AccessExclusiveLock` lines from `log_lock_waits`.

### 23.7 CPU throttling you cannot see in usage graphs

```bash
docker run --rm --cpus=1 python:3.12-slim sh -c '
  cat /sys/fs/cgroup/cpu.max
  python - <<EOF
import multiprocessing as mp, time
def burn(_):
    end = time.time() + 10
    while time.time() < end: pass
with mp.Pool(4) as p: p.map(burn, range(4))
EOF
  cat /sys/fs/cgroup/cpu.stat'
```

Expected (not captured here): `cpu.max` prints `100000 100000`; after the run, `nr_throttled` is
close to `nr_periods` (four busy processes want 4 CPUs and get 1, so they exhaust the quota in
every period) and `throttled_usec` is many seconds. Run it again with `--cpus=4`: `nr_throttled`
stays near zero. This needs a host using cgroup v2; on cgroup v1 read
`/sys/fs/cgroup/cpu,cpuacct/cpu.stat` instead.

---

## 24. Cheat sheet

| Symptom | Likely causes | First query / command |
|---|---|---|
| Client slow, every service's p99 green | tail above p99; queueing before handlers; retries; pool waits; fan-out | one slow request ID → trace + gateway log; `histogram_quantile(0.999, …)` |
| Handler p99 flat, end-to-end rising | saturation: worker, accept or proxy queue | queue-time histogram; `ss -ltn 'sport = :PORT'`; Envoy `upstream_rq_pending_active`; `L/λ` vs handler time |
| Gap in a span before the first child | pool wait, lock wait, worker queue | `hikaricp_connections_pending`; `DBStats.WaitDuration`; `SHOW POOLS` (`cl_waiting`) |
| All requests on one pod slow at the same instant | GC pause, event-loop block, CPU throttling | GC log; loop-lag metric; `py-spy dump`; `cat /sys/fs/cgroup/cpu.stat` |
| Everything in one AZ slow | zonal network or dependency | p99 `by (zone)`; `nstat` retransmits by zone; provider status |
| Problem follows one node | noisy neighbour, bad host, steal | `kubectl get pods -A -o wide --field-selector spec.nodeName=…`; `mpstat` `%steal`; `iostat -x 1` |
| CPU usage moderate, latency bad | CFS throttling; one hot core; GIL | throttled-periods ratio; `mpstat -P ALL 1`; `py-spy top --gil` |
| CPU *dropped* during the incident | concurrency limit (threads, connections, locks) | stack dump: what are workers waiting on? |
| DB "fast", app DB calls slow | pool wait; network; N+1 | pool metrics; `pg_stat_statements` by `calls` |
| Many DB sessions waiting | locks, I/O, internal contention | `pg_stat_activity` grouped by `wait_event_type, wait_event` |
| `Lock:relation` pile-up | lock queue behind DDL / long transaction | blocking-tree query (§16.1); `pg_cancel_backend` |
| `idle in transaction` sessions | app holds transactions during remote calls | `pg_stat_activity WHERE state = 'idle in transaction'` |
| Query slower than yesterday | plan flip, stats, data growth | `EXPLAIN (ANALYZE, BUFFERS)`; `pg_stat_user_tables` seq scans |
| Lag growing on all partitions | consumers slower than producers | consume vs produce rate; consumer's downstream latency |
| Lag growing on one partition | poison message, hot key, sick consumer | `kafka-consumer-groups.sh --describe` twice; consumer logs |
| Lag jumps together, repeatedly | rebalances | `--describe --state`; "poll timeout has expired" in logs |
| Saved data not visible | replica lag | `pg_stat_replication.replay_lag`; heartbeat lag |
| Latency bumps at +200 ms / +1 s / +3 s / +5 s | TCP retransmit / SYN loss / DNS timeout | `nstat -az`; `ss -ti`; `dig +stats`; `curl -w` phases |
| Spikes after every deploy | connection storm, cold caches, TLS handshakes | new connections per second; DB connections; handshake counts |
| Periodic spikes, all pods same second | cron, TTL expiry, compaction, batch job | line up with schedules; cache hit ratio |
| Periodic spikes, pods at different times | GC, per-process timers, leaks | GC logs; heap graphs |
| Errors persist after dependency recovers | retry storm, metastable state | retries / total ratio; queue age vs caller timeout; goodput |
| Client errors ≫ server errors | client timeouts, LB-generated 5xx, resets | LB logs and response flags (`UT`, `UF`, `UO`, `DC`); nginx 499 |
| Child span before parent; negative gaps | clock skew | `chronyc tracking`; `node_timex_offset_seconds` |
| Needed slow trace missing | head sampling; `decision_wait` too short; dropped spans | tail-sampling config; collector drop metrics |

---

## 25. Key Takeaways

1. Latency is conserved: every millisecond the user waited was spent somewhere. When the measured
   parts add up to less than the whole, the missing time is in a place nobody measured.
2. "Every service reports under 100 ms" usually means "p99 of handler time per attempt". It
   excludes the tail, queues, connection setup and retries.
3. Follow one slow request end to end, and compare it with a fast one. Aggregates tell you *that*;
   one request tells you *where*.
4. Run the method: precise symptom, blast radius by dimension, what changed, one request, USE/RED,
   falsifiable hypotheses, mitigate first, write it down.
5. Saturation, not utilization, explains latency. Look for anything waiting: queues, pool pending,
   run queue, lock waiters, lag.
6. Little's Law turns in-flight and rate into true time in system, and exposes hidden queueing.
7. Percentiles come from histograms, never from averaging percentiles. Watch bucket bounds and
   coordinated omission.
8. Fan-out makes a callee's rare tail the caller's common case: `1 − (1 − p)^N`.
9. The slow trace you need is the one head sampling threw away. Tail-sample errors and slow traces.
10. Most "slow database" incidents are waits before or around the query: pool, locks, transactions
    held open during remote calls.
11. CPU at 40% can be throttled; check `nr_throttled`. CPU *dropping* during an incident points to a
    concurrency limit.
12. Cascades are loops. Find the sustaining effect (retries, queues of abandoned work, cold caches)
    and cut load below capacity to escape.
13. Fixed latency offsets are fingerprints: 200 ms retransmit, 1 s and 3 s SYN loss, 5 s DNS, 100 ms
    CFS period.
14. Never order events across hosts by wall clock without knowing the skew; measure durations with
    monotonic clocks.
15. Grab perishable evidence (stack dumps, `pg_stat_activity`) in the first minute, then mitigate.

---

## 26. Cross-References

### Within distributed-systems/
- **[`00-primitives-and-system-models.md`](00-primitives-and-system-models.md)**: failure models, partial failure, clocks and ordering behind §1 and §18.5.
- **[`03-consensus-raft-and-distributed-locking.md`](03-consensus-raft-and-distributed-locking.md)**: distributed locks, leases and fencing (§16.4).
- **[`04-replication-and-consistency.md`](04-replication-and-consistency.md)**: replication and read-your-writes guarantees behind §10.
- **[`06-distributed-transactions-sagas-outbox-idempotency.md`](06-distributed-transactions-sagas-outbox-idempotency.md)**: idempotency that makes retries and hedging safe (§7.5, §17); outbox relays as a hidden source of event delay (§9.1).
- **[`07-kafka-and-event-streaming.md`](07-kafka-and-event-streaming.md)**: consumer groups, rebalancing (§4), partition key design (§5), operational failure modes (§12).
- **[`10-sharding-and-consistent-hashing.md`](10-sharding-and-consistent-hashing.md)**: hot keys and hot partitions (§9.3).
- **[`17-networking-protocols-and-communication.md`](17-networking-protocols-and-communication.md)**: TCP (§1), DNS (§8), TLS (§9), connection pooling (§10), network debugging (§13).
- **[`29-failure-detection-phi-accrual.md`](29-failure-detection-phi-accrual.md)**: GC pauses and gray failures seen by failure detectors (§11).
- **[`33-resilience-patterns-circuit-breakers.md`](33-resilience-patterns-circuit-breakers.md)**: retry amplification and budgets (§2), hedged requests (§2.6), bulkheads (§4), timeouts and deadline propagation (§5).
- **[`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md)**: queueing theory (§1–§2), load shedding (§4), adaptive concurrency (§6), queue collapse and CoDel (§7), metastability (§11).
- **[`35-reliability-math-slos-and-error-budgets.md`](35-reliability-math-slos-and-error-budgets.md)**: SLIs and error budgets that define what "the symptom" in §2 is measured against.
- **[`36-multi-region-active-active-and-geo-replication.md`](36-multi-region-active-active-and-geo-replication.md)**: cross-region replication lag and failover (§10, §22.6).

### From sre-observability/
- **[`00-mental-models.md`](../sre-observability/00-mental-models.md)**: USE vs RED (§4), sampling (§6), histograms and exemplars (§10), latency math (§16).
- **[`02-opentelemetry-deep-dive.md`](../sre-observability/02-opentelemetry-deep-dive.md)**: context propagation (§4) and sampling (§6) internals.
- **[`03-instrumentation.md`](../sre-observability/03-instrumentation.md)**: adding the spans and metrics that close the gaps in §6.2.
- **[`08-traces-storage.md`](../sre-observability/08-traces-storage.md)**: tail sampling (§8), exemplars and correlation (§12).
- **[`09-profiling.md`](../sre-observability/09-profiling.md)**: CPU, allocation and lock profiles for §11, §12 and §16.
- **[`12-alerting.md`](../sre-observability/12-alerting.md)**: symptom-based alerting (§6) for the signals this chapter hunts for.
- **[`15-incident-response-and-postmortem.md`](../sre-observability/15-incident-response-and-postmortem.md)**: the first 60 minutes (§5), mitigation before understanding (§6), postmortems (§11–§13).
- **[`23-database-observability.md`](../sre-observability/23-database-observability.md)**: `pg_stat_statements` (§4), pool observability (§6), replica lag (§7), locks and wait events (§8).
- **[`24-network-observability.md`](../sre-observability/24-network-observability.md)**: TCP and kernel signals (§3–§5), DNS (§8).
- **[`25-streaming-and-kafka-observability.md`](../sre-observability/25-streaming-and-kafka-observability.md)**: consumer lag (§4), partition skew (§6), async tracing (§8), DLQs (§10).
- **[`28-telemetry-pipeline-reliability.md`](../sre-observability/28-telemetry-pipeline-reliability.md)**: why telemetry goes missing during incidents.
- **[`42-python-observability.md`](../sre-observability/42-python-observability.md)**: structlog and contextvars (§4), py-spy (§7), async and the GIL (§10).
- **[`appendix-c-query-recipe-book.md`](../sre-observability/appendix-c-query-recipe-book.md)**: more PromQL, LogQL and TraceQL recipes.

### From kubernetes/
- **[`18-dns-and-coredns.md`](../kubernetes/18-dns-and-coredns.md)**: `ndots:5` (§3), NodeLocal DNSCache (§13), the "DNS is slow" troubleshooting tree (§17).
- **[`21-resource-management-and-qos.md`](../kubernetes/21-resource-management-and-qos.md)**: CFS quota and throttling (§9), the case for no CPU limits (§10), static CPU manager (§11), `cpu.stat` timeline (§27).

### From databases/
- **[`15-sql-performance-deep-dive.md`](../databases/15-sql-performance-deep-dive.md)**: EXPLAIN analysis (§5), lock contention (§7), debugging workflows (§11).
- **[`17-latches-and-locks-internals.md`](../databases/17-latches-and-locks-internals.md)**: latch and lock internals, diagnosing latch contention (§16).
