# Lab: LLM resilience (`../../distributed-systems/33-…` + `34-…`)

A chat product taking **1,000 user turns/sec** in front of an LLM API whose
published ceilings add up to **~100 RPS** — plus token limits, which turn out to
matter far more than the request limit.

Every turn fans out to 3.11 model calls with different shapes and different
stakes: a safety classifier, a query rewrite, a streamed answer, an
extended-thinking escalation, and a conversation title. They do not fail the
same way and must not be defended the same way. That mix is the whole point:
**resilience for LLM calls is a routing and shedding problem across
heterogeneous call classes**, not a retry policy.

The lab is in two halves, and the split is the argument:

1. **The arithmetic** (`capacity.py`, Act 1) — division, no simulation. How big
   the gap actually is, which of the three meters binds, and what each
   architectural lever buys. Resilience patterns do not appear.
2. **The mechanisms** (everything else) — a discrete-event simulation of the
   full ch33/ch34 defence stack, so each pattern's behaviour under overload and
   under a provider brownout is observable rather than asserted.

Conflating those two is the mistake the lab is built to prevent. No retry
policy closes a 144× capacity gap. What the stack decides is **who gets served
and how everyone else is refused**.

```
python3 run.py                 # the whole report, eight acts, ~3s, zero deps
python3 run.py --list          # act names
python3 run.py capacity        # just the arithmetic
python3 run.py naive stack     # the before/after
python3 test_resilience.py     # assertions, zero deps
```

`production.py` is the same stack written with real libraries instead of from
scratch — see §6.

---

## Contents

1. [How the patterns collaborate](#1-how-the-patterns-collaborate)
2. [The request flow, gate by gate](#2-the-request-flow-gate-by-gate)
3. [The workload](#3-the-workload)
4. [What the report shows](#4-what-the-report-shows)
5. [The files](#5-the-files)
6. [Doing this for real: libraries](#6-doing-this-for-real-libraries)
7. [What this lab is not](#7-what-this-lab-is-not)
8. [Exercises](#8-exercises)

---

## 1. How the patterns collaborate

Each pattern is usually taught alone, which makes them look like a menu. They
are not a menu. They are a **funnel**, and each stage exists to stop a
*different* class of request from reaching the expensive part — in increasing
order of what it costs to say no.

| Stage | Pattern | Stops | Cost of a "no" here |
|---|---|---|---|
| ① | Admission control | work that is not worth doing *right now* | one dict lookup |
| ② | Bulkhead + queue | work that would crowd out another class | one queue slot |
| ③ | Adaptive concurrency | work the provider currently can't absorb | one semaphore wait |
| ④ | Circuit breaker | work aimed at a dependency that is broken | microseconds |
| ⑤ | Client-side quota | work that would come back as a 429 | zero network |
| ⑥ | Timeouts + retries | work that failed but might succeed | a full prefill, in dollars |
| ⑦ | Fallback | nothing — it decides what the user gets instead | product quality |

Read the right-hand column downwards. **The ordering is the design.** A request
shed at ① costs nothing; the same request shed at ⑥ has already consumed a
queue slot, a concurrency slot, a quota reservation, and a prompt prefill you
paid for. Every gate you move earlier is money.

### The edges between them

The stages are not independent — they feed each other, and three of those edges
are where the real engineering is:

```
  ⑥ attempt loop ──(outcome, minus 429s)──▶ ④ breaker
  ⑥ attempt loop ──(time to first token)──▶ ③ adaptive limiter
  ⑥ attempt loop ──(unused max_tokens)────▶ ⑤ quota reservation
  ⑤ quota pressure ───────────────────────▶ ① admission ladder
```

**⑥ → ④, minus 429s.** The attempt loop reports every outcome to the breaker
*except* rate limiting. A 429 is the dependency working correctly and telling
you to slow down; a 529 is its fleet in trouble. They arrive as nearly
identical HTTP responses and demand opposite reactions. Count 429s and the
breaker opens on a healthy API, then refuses the requests that *did* have
quota — a self-inflicted outage layered on a rate limit. In the report, the
same call stream with that one flag flipped ends `open` with 180 of 200 calls
refused, versus `closed` with zero.

That exclusion is one early return in `breaker.py`. It is the highest
value-per-line in the lab.

**⑥ → ③, time to first token, not total duration.** Total duration on an LLM
call is dominated by how many tokens the answer needed. A latency-driven
concurrency controller fed raw duration shrinks the limit whenever users ask
harder questions. TTFT is the part that actually reflects queueing. Related and
equally load-bearing: the controller must be scoped **per call class**. Pool a
0.2s classifier and a 9s answer stream behind one controller and the
classifier's latency becomes the no-load baseline, every answer call reads as
congestion, and the limit decays to the floor — which then *causes* queueing,
which it reads as more congestion. (Both of those started as bugs in this lab.)

**⑥ → ⑤, reconciliation.** The output-token meter is charged `max_tokens` at
admission and reconciled against real output at completion. If the attempt loop
does not hand the slack back, the client throttles itself to a fraction of the
quota it is paying for — on the `answer` class, 8.1 req/s instead of 32.1.

**⑤ → ①, pressure.** The admission ladder needs a saturation signal, and on an
LLM gateway the usual one is wrong: your CPU is idle while your token budget is
exhausted, so a CPU-based shedder never fires. Pressure comes from quota
headroom and in-flight concurrency instead.

### What each pattern cannot do

Just as important as the edges, because this is where teams reach for the wrong
tool:

- A **circuit breaker** cannot help with overload. It detects a *broken*
  dependency. A dependency that is rate-limiting you is not broken.
- **Retries** cannot create capacity. They help when failures are independent
  (one bad node) and are pure cost when they are correlated (the fleet is
  down). In the report's brownout, "no retries at all" is competitive.
- **Bulkheads** cannot shed. They bound and queue; something still has to
  decide what to drop.
- **Adaptive concurrency** cannot respect priority. It knows how much, never
  which.
- **Admission control** is the only pattern that closes the loop between
  "we are over capacity" and "so this specific feature stops" — which is why it
  is also the only one with no library (§6).

---

## 2. The request flow, gate by gate

One user turn, end to end. Left column is the pipeline; right column is what
each LLM call goes through inside `Gateway.call`.

```
 USER TURN                          EACH CALL ENTERS THE STACK
 ─────────                          ──────────────────────────
                                     │
 guard ──────────────────────────────┤  ① ADMISSION            admission.py
 CRITICAL, haiku, 420→12 tok         │     criticality vs pressure
 fail ⇒ serve + flag for review      │     deadline feasible?
      │                              │     tenant within fair share?
      ▼                              │        └─ no ⇒ ⑦ fallback (free)
 rewrite ────────────────────────────┤
 SHEDDABLE_PLUS, haiku, 900→70       │  ② BULKHEAD             bulkhead.py
 fail ⇒ retrieve on the raw query    │     per-class slots, bounded queue
      │                              │     LIFO · CoDel · deadline sweep
      ▼                              │        └─ drop ⇒ ⑦ fallback
 retrieval (vector DB, not an LLM)   │
      │                              │  ③ CONCURRENCY          adaptive.py
      ▼                              │     per-class AIMD / gradient on TTFT
 ┌─ hard turn? ─┐                    │
 │              │                    │  ④ BREAKER              breaker.py
 ▼              ▼                    │     open ⇒ ⑦ fallback, microseconds
 think        answer ────────────────┤     half-open ⇒ one cheap probe
 CRITICAL     CRITICAL_PLUS          │
 opus         sonnet, streamed       │  ⑤ QUOTA                limits.py
 6.8k→2.4k    6.2k→520               │     RPM · ITPM · OTPM buckets
 fail ⇒       fail ⇒ THE TURN        │     reserve max_tokens ──┐
 downgrade    FAILS                  │        └─ empty ⇒ ⑦     │
      │         │                    │                          │
      └────┬────┘                    │  ⑥ ATTEMPT LOOP retry.py │
           ▼                         │     ttft / inter-token /  │
 title (fire and forget) ────────────┤     total timeouts        │
 SHEDDABLE, haiku, 1.6k→28           │     classify → budget →   │
 fail ⇒ enqueue for the Batch API    │     jittered backoff      │
                                     │        │                  │
                                     │        ├──▶ ④ breaker (minus 429s)
                                     │        ├──▶ ③ limiter (TTFT)
                                     │        └──▶ ⑤ reconcile ◀─┘
                                     │
                                     │  ⑦ FALLBACK
                                     │     degrade — never fabricate
```

The degradation ladder read as product behaviour, which is the only reading
that matters:

```
 pressure   rung   what stops                        what the user sees
 ────────   ────   ──────────────────────────────    ──────────────────────
   < 0.80   L0     nothing                           full service
   ≥ 0.80   L1     conversation titles               untitled chats
   ≥ 0.88   L2     + query rewriting                 slightly worse retrieval
   ≥ 0.95   L3     + the extended-thinking tier       shallower hard answers
   at cap   L4     + the answer path itself          "try again in a moment"
```

The product degrades from the outside in. In the report at 20 turns/sec, `title`
sheds 48% and `think` 16% while `guard` and `answer` shed 0% — that ordering,
not the absolute numbers, is the result.

> **Caveat the report makes visible:** CRITICAL_PLUS means "never shed *by
> policy*", not "always served". At 40 turns/sec `answer` still completes only
> 8% of offered calls — it is not being shed, it simply does not fit. Shedding
> reallocates capacity; it does not create any. And shedding a class only helps
> if it contends for the *same* meter: dropping Haiku titles buys nothing when
> the constraint is an in-flight reservation on Opus.

---

## 3. The workload

Five call classes, one product (`config.py`):

| class | model | criticality | in→out | `max_tokens` | deadline | on failure |
|---|---|---|---|---|---|---|
| `guard` | Haiku | CRITICAL | 420→12 | 64 | 2.5s | serve, flag for review |
| `rewrite` | Haiku | SHEDDABLE_PLUS | 900→70 | 256 | 2.5s | retrieve on raw query |
| `answer` | Sonnet | CRITICAL_PLUS | 6200→520 | 2048 | 40s | **the turn fails** |
| `think` | Opus | CRITICAL | 6800→2400 | 8192 | 180s | downgrade to `answer` |
| `title` | Haiku | SHEDDABLE | 1600→28 | 64 | 300s | enqueue for Batch API |

Fan-out is 3.11 calls per turn, so 1,000 turns/sec is 3,110 LLM calls/sec
before a single retry.

### The finding that surprised me

Capacity is metered on three axes (RPM, input tokens/min, output tokens/min),
and **the binding one is almost never RPM**. On this fixture the aggregate RPM
ceiling says 107 req/s; the `answer` class actually tops out at 21.5, and
`think` at 0.56.

The `think` number is the interesting one. The output meter charges
`max_tokens` at admission and reconciles later, so `max_tokens` does *not* cap
your sustained rate — the reconciliation returns the slack. What it caps is how
many requests can be **in flight at once**, because each holds its full
reservation for the duration of the call:

```
concurrency_cap = OTPM_bucket / max_tokens
```

`think` asks for 8,192 tokens, writes ~2,400, and runs for ~70 seconds. It can
hold 39 calls in flight when its own token quota would support 69 — so it runs
at 0.56 req/s instead of 0.98. Sizing `max_tokens` to the p97 of observed output
buys 1.77× on that class, for one config line and no quality change.

The corollary generalises: the same `max_tokens` slack is free on a 9-second
answer stream and costs a factor of two on a 70-second reasoning call. Long
calls are where sloppy `max_tokens` becomes a throughput bug.

---

## 4. What the report shows

| act | claim |
|---|---|
| `capacity` | 1,000 turns/s vs a 6.94 turns/s fixture — a 144× gap; which meter binds per class; Little's Law pool sizes; what each lever buys |
| `naive` | `for attempt in range(3)` + one global timeout: 1.57× amplification, 88% of spend bought nothing, 20% of turns answered |
| `stack` | same load, same seed, the composed stack: 30% of turns answered, 3.1% retry rate, $3.83 wasted instead of $64.32, shedding ordered by criticality |
| `breaker` | the 429 flag, isolated: `open` + 180/200 refused vs `closed` + 0. Then a real brownout, where the breaker cuts p95 from 31.5s to 14.5s and waste from $5.30 to $1.04 |
| `retries` | budget and jitter under a brownout; why "no retries at all" is sometimes competitive; what a retry costs in dollars on a 6,200-token prompt |
| `adaptive` | fixed vs AIMD vs gradient against a capacity step change |
| `ladder` | the degradation ladder at 5/10/20/40 turns/s |
| `meters` | per-instance split vs coordinated quota; reservation vs reconciliation |

Three levers in Act 1 buy exactly **1.00×**. That is deliberate and it is the
act's second lesson: relieving a meter that was not the one binding is the most
common wasted optimisation in LLM infrastructure. Halving your RAG context does
nothing when the constraint is an in-flight reservation on a different model.

---

## 5. The files

```
sim.py          ~200-line deterministic discrete-event kernel (Event/Process/
                Resource/TokenBucket). No threads, no sleeps, no wall clock.
config.py       the fixture: models, meters, prices, five call classes,
                criticality, per-operation timeouts, all gateway knobs
provider.py     the API: three token buckets per model, M/M/c-shaped queueing,
                429 with retry-after, 529 on fleet overload, 500s, TTFT events

capacity.py     ── the arithmetic. No simulation. Read this one first.

admission.py    ① criticality ladder, deadline feasibility, per-tenant share
bulkhead.py     ② per-class slots, bounded queue, LIFO, CoDel, deadline sweep
adaptive.py     ③ fixed / AIMD / Netflix gradient concurrency controllers
breaker.py      ④ circuit breaker, with the 429 exclusion and probe accounting
limits.py       ⑤ client-side RPM/ITPM/OTPM with reservation + reconciliation
retry.py        ⑥ classification, full jitter, retry budget, cost of a retry
gateway.py      the composition — the flow diagram in §2 lives at the top
metrics.py      goodput, shed-by-criticality, retry spend
scenarios.py    traffic generator, turn pipeline, brownout/outage/step injectors
run.py          the eight acts
test_resilience.py   assertions

production.py            the same stack with real libraries (§6)
requirements-production.txt
```

Zero dependencies for everything except `production.py`, which is a reference
and is not executed.

---

## 6. Doing this for real: libraries

**None of the modules above should ship.** They exist to make mechanisms
visible. `production.py` maps each one to the library that already does it:

| hand-rolled here | what you actually use |
|---|---|
| `retry.py` | the SDK's own `max_retries` + `httpx.Timeout`; `tenacity` for policy it can't express |
| `breaker.py` | `pybreaker` (sync) / `purgatory-circuitbreaker` (async) |
| `limits.py` | `aiolimiter` in-process; `limits` + Redis for a fleet |
| `bulkhead.py` | `asyncio.Semaphore` / `anyio.CapacityLimiter` — genuinely enough |
| `adaptive.py` | Envoy's `adaptive_concurrency` filter, at the sidecar |
| routing + fallback | LiteLLM's `Router` (TPM/RPM-aware, cooldowns, fallbacks) |
| `admission.py` | **nothing. This one stays yours.** |

Three things worth knowing before you write any of it:

1. **Read what the SDK already does.** The Anthropic and OpenAI clients retry
   429/5xx with exponential backoff and honour `retry-after`, defaulting to
   `max_retries=2`. Wrapping that in `tenacity` without setting `max_retries=0`
   gives you 3 × 3 = 9 attempts. Pick one layer to own retries.
2. **Retry budgets have no Python library.** Envoy has `retry_budget`, gRPC has
   it in the service config; in Python you write the 30 lines. They are worth
   writing — an attempt count multiplies load exactly when the dependency can
   least take it, whereas a budget expressed as a fraction of *successes*
   self-cancels during a real outage.
3. **Admission control has no library, and that is correct.** A criticality
   ladder encodes a product judgement — which features your users will forgive
   you for dropping, and in what order. No library can know that. The mechanics
   are 20 lines; the ordering is the whole design.

`production.py` also covers the levers that are not resilience patterns at all
and usually matter more: prompt caching, the Batch API for every SHEDDABLE
class, `max_tokens` sizing, model routing, response caching — and the three
metrics that stay honest while you are shedding on purpose (goodput,
shed-by-criticality, retry spend), because availability and error rate both
*improve* under load shedding while the product gets worse.

---

## 7. What this lab is not

**Every number in `config.py` and `provider.py` is invented.** The rate limits
are shaped like a published API tier but are not one. The token counts are
shaped like a RAG chat app but are not measured from one. The service-time
model (`base + prefill + decode`, stretched by an M/M/c-ish queueing factor) is
a plausible shape, not a model of anyone's infrastructure.

So:

- **Rung 1 — arithmetic.** Act 1 is division. Given the fixture, it is exactly
  right; given your fixture, re-run it. `capacity.py` is the file to port.
- **Rung 2 — simulated.** Acts 2–8 are consequences of a stated model. They
  demonstrate *mechanisms* and orders of magnitude. Nothing here is a
  measurement of any provider and none of it should be quoted as one.
- **Rung 0 — unrun.** `production.py` has no network access here and is not
  covered by the tests. The shapes come from each library's documented API;
  pin your versions and check the signatures.

Deliberately out of scope: token-level streaming mechanics, prompt caching's
effect on rate-limit accounting (assumed, parameterised as
`cache_read_itpm_weight`, and *not* verified against any provider's docs),
multi-region failover, cost optimisation beyond the retry-waste line, and
anything about answer quality. This lab measures *whether you got an answer*,
never whether it was a good one — for that, see `../golden-set/`.

Two simplifications worth naming because they would change conclusions:
arrivals are Poisson (real chat traffic is burstier, which makes shedding
matter more), and there is exactly one tenant class in most scenarios (the
per-tenant limiter exists in `admission.py` but only one act exercises it).

---

## 8. Exercises

1. **Port Act 1 to your own numbers.** Replace `MODELS` and `CLASSES` in
   `config.py` with your tier and your measured token counts, then run
   `python3 run.py capacity`. The output is the deliverable: which meter binds,
   what each lever buys, and how many times your current quota you would need.
   Take that last number to whoever owns the contract.
2. **Find your `think`.** Compute `OTPM_bucket / max_tokens` for every call
   class and compare it against `rate × service_time`. Any class where the
   first is smaller is being throttled by a `max_tokens` value someone picked
   as a round number.
3. **Flip `breaker_counts_429`** in `GatewayConfig` and re-run `run.py
   breaker`. Then go and check what your production breaker does with a 429.
4. **Break the reconciliation.** Delete the `limiter.reconcile(...)` call in
   `gateway.py` and re-run `run.py meters`. Compare the goodput drop against
   the `unreconciled` column in Act 1.
5. **Scope the adaptive limiter back to per-model** (key `conc_limiters` by
   `cls.model` instead of `cls.name`) and watch the limit decay to the floor.
   This was a real bug in this lab before it was a lesson.
6. **Write your ladder.** List your product's features in the order you would
   drop them, and say what the user sees at each rung. If you cannot fill in
   the right-hand column, you do not have a degradation strategy — you have a
   timeout.
7. **Rebuild the stack from §6's libraries** and check it against
   `test_resilience.py`'s invariants. The assertions are about behaviour, not
   implementation, so they should hold for `pybreaker` and `tenacity` too.
