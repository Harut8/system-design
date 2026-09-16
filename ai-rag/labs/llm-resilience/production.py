"""The same stack, assembled from libraries instead of written from scratch.

Every module in this lab exists to make one mechanism *visible*. None of them
should ship. In production each one is a well-maintained library — usually one
you already have — and the engineering work is composition and configuration,
not implementation.

    hand-rolled here          what you actually use
    ──────────────────────    ─────────────────────────────────────────────────
    retry.py                  the SDK's own `max_retries`; tenacity or backoff
                              for anything the SDK won't do
    breaker.py                pybreaker (sync) / purgatory (async)
    limits.py                 aiolimiter (in-process) or `limits` + Redis (fleet)
    bulkhead.py               asyncio.Semaphore / anyio.CapacityLimiter
    adaptive.py               Envoy's adaptive_concurrency filter, at the mesh
    admission.py             — no library; this one is genuinely yours, because
                              only you know what your criticality ladder is
    provider.py               the real API
    sim.py                    production traffic, or a load generator

The only piece with no off-the-shelf answer is admission control, and that is
not an accident: shedding correctly requires knowing which of your features the
user will forgive you for dropping. No library can know that.

**Status of this file: rung 0 — unrun.** It has no network access in this
environment and is not exercised by `test_resilience.py`. The shapes are
written from each library's documented API, but pin your versions and check the
signatures before copying: `pybreaker`'s `exclude` predicate form, LiteLLM's
router kwargs, and the `limits` async storage API have all moved between
releases. Treat this as a map of the ecosystem, not as tested code.

    pip install -r requirements-production.txt
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

log = logging.getLogger("llm-gateway")


# ═══════════════════════════════════════════════════════════════════════════
# 0. START HERE: most of retry.py is already in the SDK
# ═══════════════════════════════════════════════════════════════════════════
#
# Before adding tenacity, read what the official client already does. The
# Anthropic and OpenAI SDKs both retry connection errors, 408, 409, 429 and 5xx
# with exponential backoff, honour the `retry-after` header, and default to
# `max_retries=2`. A large fraction of hand-written LLM retry wrappers are
# re-implementing this, badly, on top of it — and then getting 3 x 3 = 9
# attempts because the outer loop doesn't know about the inner one.
#
#   RULE: pick one layer to own retries. If you wrap the SDK in tenacity, set
#   `max_retries=0` on the client. If you keep the SDK's retries, don't wrap.

try:
    import anthropic
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover - this file is a reference, not a test
    anthropic = None
    AsyncAnthropic = None


def build_client(timeout_s: float = 30.0, max_retries: int = 2):
    """The 80% solution: correct timeouts and the SDK's own retry policy.

    `httpx.Timeout` is the important part. A single scalar timeout cannot serve
    both a 300ms classifier and a 90-second reasoning stream, and `connect` in
    particular should be short and separate — a TCP connect that takes 8 seconds
    is never going to produce a useful answer.

    Note the wall-clock trap: the SDK retries timeouts, so the worst case is
    `timeout x (max_retries + 1)`. If you have a caller-facing deadline, that
    product is the number that has to fit inside it, not `timeout`.
    """
    import httpx

    return AsyncAnthropic(
        timeout=httpx.Timeout(timeout_s, connect=2.0, read=timeout_s, write=10.0),
        max_retries=max_retries,
    )


# Per-operation overrides without building a second client. This is the
# library equivalent of config.py's `Timeouts` per call class.
def per_call_client(client, *, timeout_s: float, max_retries: int):
    return client.with_options(timeout=timeout_s, max_retries=max_retries)


# ═══════════════════════════════════════════════════════════════════════════
# 1. RETRIES — tenacity, when you need policy the SDK doesn't have
# ═══════════════════════════════════════════════════════════════════════════
#
# Reach for tenacity when you need something the SDK's retry cannot express:
# a retry *budget*, a deadline-aware stop condition, different policies per
# call class, or "do not retry a stream that is 80% delivered".
#
# `wait_exponential_jitter` is the one to use — it is full-jitter, which is
# what stops every client that failed in the same second from retrying in the
# same later second (ch33 §2.4).

try:
    from tenacity import (AsyncRetrying, RetryCallState, retry_if_exception,
                          stop_after_attempt, stop_after_delay,
                          wait_exponential_jitter)
except ImportError:  # pragma: no cover
    AsyncRetrying = None


def is_retryable(exc: BaseException) -> bool:
    """Classification (retry.py's `classify`), using the SDK's typed errors.

    Use the exception classes, never string-matching on the message. Note that
    `RateLimitError` is retryable but should be handled by the rate limiter,
    not by piling on more attempts — see §2.
    """
    if anthropic is None:
        return False
    if isinstance(exc, anthropic.RateLimitError):  # 429
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500  # includes 529 overloaded_error
    return isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError))


def retry_after_seconds(exc: BaseException, default: float = 1.0) -> float:
    """Honour the provider's own advice. It knows when the bucket refills."""
    response = getattr(exc, "response", None)
    if response is not None:
        header = response.headers.get("retry-after")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
    return default


async def call_with_retry(fn: Callable[[], Awaitable[Any]], *, attempts: int = 3,
                          deadline_s: float = 30.0) -> Any:
    """tenacity equivalent of retry.py's `decide`.

    `stop_after_delay` is doing real work here: it is the deadline check. A
    retry that cannot finish before the caller gives up costs a full prefill
    and returns an answer nobody reads.
    """
    async for attempt in AsyncRetrying(
        retry=retry_if_exception(is_retryable),
        wait=wait_exponential_jitter(initial=1.0, max=20.0),  # full jitter
        stop=(stop_after_attempt(attempts) | stop_after_delay(deadline_s)),
        reraise=True,
    ):
        with attempt:
            return await fn()


# The `backoff` library is the other common choice; the decorator form is
# terser but harder to make deadline-aware:
#
#   import backoff
#   @backoff.on_exception(backoff.expo, anthropic.RateLimitError,
#                         jitter=backoff.full_jitter, max_tries=3, max_time=30)
#   async def summarise(text: str): ...


# ── retry budget ───────────────────────────────────────────────────────────
# No mainstream Python library ships one, which is why retry.py has ~30 lines
# of it. It is the single highest-value thing in that file: a per-call-site
# attempt count multiplies load exactly when the dependency can least take it,
# whereas a budget expressed as a fraction of *successes* self-cancels during a
# real outage. Envoy has it natively (`retry_budget`), gRPC has it in the
# service config; in Python you write the ~30 lines.


class RetryBudget:
    """Port of retry.py's budget. Keep this; it has no library equivalent."""

    def __init__(self, ratio: float = 0.10, initial: float = 10.0,
                 cap: float = 200.0) -> None:
        self.ratio, self.tokens, self.cap = ratio, initial, cap

    def on_success(self) -> None:
        self.tokens = min(self.cap, self.tokens + self.ratio)

    def take(self) -> bool:
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


# ═══════════════════════════════════════════════════════════════════════════
# 2. CLIENT-SIDE RATE LIMITING — aiolimiter in-process, `limits` for a fleet
# ═══════════════════════════════════════════════════════════════════════════
#
# Remember there are three meters, not one, and that the output meter is
# charged `max_tokens` and reconciled later (limits.py). Most libraries model a
# single scalar rate, so you run three limiters and gate on all three.

try:
    from aiolimiter import AsyncLimiter
except ImportError:  # pragma: no cover
    AsyncLimiter = None


class InProcessQuota:
    """Single-pod metering. `AsyncLimiter(max_rate, time_period)` is a leaky
    bucket; `acquire(n)` takes n units, which is what makes it usable for
    token meters and not just request counts.

    The `max_tokens` reservation is the part no library does for you: you have
    to take the reservation before the call and hand the slack back after, or
    your own limiter throttles you to a fraction of your real allowance.
    """

    def __init__(self, rpm: int, itpm: int, otpm: int, headroom: float = 0.9) -> None:
        self.requests = AsyncLimiter(rpm * headroom, 60)
        self.input_tokens = AsyncLimiter(itpm * headroom, 60)
        self.output_tokens = AsyncLimiter(otpm * headroom, 60)

    async def reserve(self, in_tokens: int, max_tokens: int) -> None:
        await self.requests.acquire(1)
        await self.input_tokens.acquire(in_tokens)
        await self.output_tokens.acquire(max_tokens)

    def reconcile(self, max_tokens: int, actual_out: int) -> None:
        """Give back what you reserved and did not use.

        aiolimiter has no public 'return tokens' call, so in practice you
        either (a) track the debt yourself and skip that many future
        acquisitions, or (b) use a bucket you control. This asymmetry is the
        main reason limits.py exists in this lab at all.
        """
        slack = max(0, max_tokens - actual_out)
        log.debug("reconcile: returning %d output tokens", slack)


# ── fleet-wide, Redis-backed ───────────────────────────────────────────────
# The quota is org-level; your service is N pods. Splitting the limit N ways is
# simple and wrong whenever load is uneven. A shared bucket costs 1-2ms per
# check, which is nothing next to an 800ms LLM call.
#
#   from limits import parse
#   from limits.aio.storage import RedisStorage
#   from limits.aio.strategies import MovingWindowRateLimiter
#
#   storage = RedisStorage("async+redis://localhost:6379")
#   limiter = MovingWindowRateLimiter(storage)
#   item = parse("4000/minute")
#   if not await limiter.hit(item, "org:acme", "model:sonnet", cost=1):
#       raise Quota Exhausted
#
# `cost=` is what lets one limiter meter tokens instead of requests.
#
# MANDATORY: a documented fallback for when Redis is unreachable. Falling open
# means every pod floods the provider; falling closed means an outage caused by
# your own cache. Fall back to the per-pod split (limit / N) — degraded but
# bounded, which is the same trade limits.py makes in `degrade_to_local()`.


# ═══════════════════════════════════════════════════════════════════════════
# 3. CIRCUIT BREAKER — pybreaker, with the 429 exclusion wired in
# ═══════════════════════════════════════════════════════════════════════════
#
# pybreaker gives you the state machine, listeners, and a Redis-backed shared
# state so a fleet agrees the circuit is open. What it cannot know is that a
# 429 is not a failure — that is the `exclude` argument, and it is the single
# most important line in this section (Act 4 in the report).

try:
    import pybreaker
except ImportError:  # pragma: no cover
    pybreaker = None


def build_breaker(name: str):
    """One breaker per (model, call class), not one per process.

    A sick Opus must not open the circuit on Haiku, and a poisoned prompt in
    one class must not disable the others.

    Thresholds are LLM-shaped, per ch33 §9.3: these APIs have a higher baseline
    error rate than an internal RPC, and their incidents last minutes rather
    than seconds, so a tight threshold on a short window oscillates.
    """
    return pybreaker.CircuitBreaker(
        name=name,
        fail_max=20,          # consecutive failures before opening
        reset_timeout=30,     # seconds open before probing
        # ── the load-bearing line ──────────────────────────────────────────
        # 429 is the dependency working correctly and telling you to slow down.
        # Count it and the breaker opens on a healthy API, then refuses the
        # requests that did have quota.
        exclude=[lambda exc: isinstance(exc, anthropic.RateLimitError)],
        listeners=[],         # add a listener that emits a state-change metric
    )


# For asyncio, `purgatory-circuitbreaker` is the async-native equivalent and
# supports a Redis store:
#
#   from purgatory import AsyncCircuitBreakerFactory
#   breakers = AsyncCircuitBreakerFactory(default_threshold=20, default_ttl=30)
#   async with await breakers.get_breaker("sonnet:answer"):
#       ...
#
# Whichever you pick, half-open probes should use a *cheap* request — a 10-word
# prompt on the smallest model — not a replay of a 6,000-token production call.


# ═══════════════════════════════════════════════════════════════════════════
# 4. BULKHEADS — asyncio.Semaphore is genuinely enough
# ═══════════════════════════════════════════════════════════════════════════
#
# There is no bulkhead library for Python and there does not need to be. What
# matters is not the primitive but three decisions the primitive doesn't make
# for you: one semaphore *per call class* (not one global), a bounded wait, and
# what you do when the wait expires.

class Bulkhead:
    """Per-class concurrency isolation. ~15 lines, and the 15 lines are fine.

    The `timeout` is the part people leave out. `async with semaphore` with no
    timeout is an unbounded queue with extra steps: requests pile up, are served
    after the user has left, and the system does maximum work for zero goodput
    (ch34 §7).
    """

    def __init__(self, limit: int, queue_timeout_s: float) -> None:
        self._sem = asyncio.Semaphore(limit)
        self._timeout = queue_timeout_s

    async def run(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._timeout)
        except asyncio.TimeoutError:
            raise OverloadedError("bulkhead queue timeout") from None
        try:
            return await fn()
        finally:
            self._sem.release()


class OverloadedError(Exception):
    """Distinct from a dependency failure. Do not feed this to the breaker."""


# `anyio.CapacityLimiter` is the same idea and works on trio as well:
#
#   limiter = anyio.CapacityLimiter(40)
#   async with limiter:
#       ...
#
# LIFO queueing and CoDel (bulkhead.py) have no Python library. If you want
# them, they live at the proxy — Envoy's `buffer` and `adaptive_concurrency`
# filters, or Linkerd — not in your application code.


# ═══════════════════════════════════════════════════════════════════════════
# 5. ADAPTIVE CONCURRENCY — push this to the mesh
# ═══════════════════════════════════════════════════════════════════════════
#
# Netflix's concurrency-limits is a JVM library with no maintained Python port.
# For Python services the practical answer is to run it in the sidecar:
#
#   Envoy: adaptive_concurrency filter (gradient controller, minRTT sampling)
#   Linkerd / Istio: circuit-breaking + outlier detection at the mesh layer
#
# If you do implement it in-process (adaptive.py), the two things that decide
# whether it works at all are not the algorithm:
#
#   1. Scope it per call class. A controller that sees both a 0.2s classifier
#      and a 9s answer stream takes the classifier's latency as its no-load
#      baseline, reads every answer call as congestion, and decays to the
#      floor — which then causes real queueing, which it reads as more
#      congestion.
#   2. Feed it time-to-first-token, not total duration. Total duration on an
#      LLM call is dominated by how many tokens the answer needed. A controller
#      fed raw latency shrinks the limit whenever users ask harder questions.


# ═══════════════════════════════════════════════════════════════════════════
# 6. ROUTING, FALLBACK AND MULTI-PROVIDER — LiteLLM
# ═══════════════════════════════════════════════════════════════════════════
#
# If you want most of §1-§5 as configuration rather than code, LiteLLM's Router
# is the closest thing to a batteries-included answer: it does TPM/RPM-aware
# routing across deployments, per-deployment cooldowns after repeated failures
# (a circuit breaker by another name), retries, and model fallbacks.
#
#   from litellm import Router
#   router = Router(
#       model_list=[
#           {"model_name": "answer",
#            "litellm_params": {"model": "anthropic/claude-sonnet-5"},
#            "tpm": 1_000_000, "rpm": 2_000},
#           {"model_name": "answer",
#            "litellm_params": {"model": "bedrock/anthropic.claude-sonnet-5"},
#            "tpm": 1_000_000, "rpm": 2_000},
#       ],
#       routing_strategy="usage-based-routing-v2",
#       fallbacks=[{"answer": ["answer-cheap"]}],
#       num_retries=2,
#       allowed_fails=3,
#       cooldown_time=30,
#       redis_host=os.getenv("REDIS_HOST"),   # shared state across pods
#   )
#
# Check the kwargs against your installed version — this surface moves.
#
# What it buys: routing, fallback, cooldowns, shared TPM/RPM state, one
# interface across providers.
# What it does not buy: your criticality ladder, your deadline propagation,
# your per-class timeouts, your reconciliation of `max_tokens`. Those stay
# yours, and they are the ones that decide whether the product degrades well.


# ═══════════════════════════════════════════════════════════════════════════
# 7. ADMISSION CONTROL — no library, and that is correct
# ═══════════════════════════════════════════════════════════════════════════
#
# This is the one part of admission.py you should keep writing yourself. A
# criticality ladder encodes a product judgement — which features your users
# will forgive you for dropping, and in what order — and no library can know
# that. The mechanics are trivial; the ordering is the whole design.

CRITICAL_PLUS, CRITICAL, SHEDDABLE_PLUS, SHEDDABLE = 0, 1, 2, 3


@dataclass
class AdmissionPolicy:
    """Pressure-driven shedding. ~20 lines and the most valuable 20 in the file.

    `pressure` should come from the resource that actually binds — on an LLM
    gateway that is quota headroom or in-flight concurrency, *not* CPU. Your
    gateway's CPU is idle while its token budget is exhausted, so a CPU-based
    shedder will never fire.
    """

    thresholds: Dict[float, int] = None  # pressure -> lowest criticality admitted

    def __post_init__(self) -> None:
        self.thresholds = self.thresholds or {
            0.95: CRITICAL_PLUS,
            0.88: CRITICAL,
            0.80: SHEDDABLE_PLUS,
            0.00: SHEDDABLE,
        }

    def admit(self, criticality: int, pressure: float) -> bool:
        for threshold in sorted(self.thresholds, reverse=True):
            if pressure >= threshold:
                return criticality <= self.thresholds[threshold]
        return True


# Deadline propagation is the other piece with no library: pass the caller's
# remaining budget down every hop and refuse work that cannot finish inside it.
# In gRPC this is built in; over HTTP you carry it yourself in a header and
# turn it into the per-call timeout.


# ═══════════════════════════════════════════════════════════════════════════
# 8. THE THINGS THAT ARE NOT RESILIENCE PATTERNS AT ALL
# ═══════════════════════════════════════════════════════════════════════════
#
# Act 1 of the report exists to make this point: the levers that actually move
# your capacity edge are workload changes, and most of them are provider
# features rather than client patterns.
#
#   Prompt caching      cache reads are ~0.1x input cost. Put the stable prefix
#                       first and the volatile part last; verify with
#                       `usage.cache_read_input_tokens` — a zero there across
#                       repeated calls means a silent invalidator (a timestamp,
#                       an unsorted dict) in the prefix.
#
#   Batch API           50% cheaper and, more importantly, *off the interactive
#                       quota entirely*. Every SHEDDABLE class in your fixture
#                       — titles, summaries, backfills, evals — belongs here.
#                       This is usually the single biggest win available.
#
#   max_tokens          size it to the p97 of observed output. It is charged
#                       against the output-token meter at admission and
#                       reconciled later, so slack does not cost sustained
#                       throughput — it costs *concurrency*, which is what
#                       binds on long reasoning calls (Act 1).
#
#   Model routing       run the cheap tier by default and escalate on a
#                       confidence signal, rather than running the expensive
#                       tier and degrading under load.
#
#   Response caching    the only lever that removes a call instead of shrinking
#                       it. Semantic-cache hit rates are workload-specific;
#                       measure yours, do not adopt a number from a blog post.


# ═══════════════════════════════════════════════════════════════════════════
# 9. OBSERVABILITY — the three metrics that don't lie under shedding
# ═══════════════════════════════════════════════════════════════════════════
#
# Availability *improves* under load shedding, because the requests you never
# admitted never had a chance to fail. Error rate goes down as the product gets
# worse. Instrument the numbers that stay honest (metrics.py):

try:
    from opentelemetry import metrics as otel_metrics
except ImportError:  # pragma: no cover
    otel_metrics = None


def register_metrics(meter) -> Dict[str, Any]:
    return {
        # successful, *useful-to-a-user* completions per second. If shedding is
        # working this holds flat while offered load climbs. If it falls while
        # shed_rate rises, the shedding itself is the problem.
        "goodput": meter.create_counter("llm.goodput", unit="1"),
        # where the pain landed. CRITICAL_PLUS shedding is an escalation;
        # SHEDDABLE shedding is the system working as designed.
        "shed": meter.create_counter("llm.shed", unit="1"),
        # dollars burned on attempts that returned nothing. On an LLM API a
        # retry re-processes the whole prompt, so alert on
        # retry_cost / total_cost > 5%, not just on error rate.
        "retry_cost": meter.create_counter("llm.retry_cost_usd", unit="USD"),
        # breaker state transitions, tagged by model and call class
        "breaker_state": meter.create_up_down_counter("llm.breaker_open"),
        # headroom, straight from the response headers
        "quota_remaining": meter.create_observable_gauge("llm.quota_remaining"),
    }


# The provider hands you headroom on every single response. Read it — it is the
# cheapest telemetry you will ever get, and it is the input a client-side
# limiter should be correcting against:
#
#   response = await client.messages.with_raw_response.create(...)
#   headers = response.headers
#   headers["anthropic-ratelimit-requests-remaining"]
#   headers["anthropic-ratelimit-input-tokens-remaining"]
#   headers["anthropic-ratelimit-output-tokens-remaining"]
#   headers["anthropic-ratelimit-tokens-reset"]
#   message = response.parse()


# ═══════════════════════════════════════════════════════════════════════════
# 10. PUTTING IT TOGETHER — the same seven gates, ~60 lines
# ═══════════════════════════════════════════════════════════════════════════


class ProductionGateway:
    """gateway.py's stack, in library form. Same order, same reasons.

    Compare against the diagram at the top of gateway.py: the ordering is
    identical, because the ordering is the design. Each gate is cheaper than
    the one after it, and each exists to stop a different class of request from
    reaching the expensive part.
    """

    def __init__(self, client, quotas: Dict[str, InProcessQuota],
                 bulkheads: Dict[str, Bulkhead],
                 breakers: Dict[str, Any],
                 policy: AdmissionPolicy) -> None:
        self.client = client
        self.quotas = quotas
        self.bulkheads = bulkheads
        self.breakers = breakers
        self.policy = policy
        self.budget = RetryBudget()

    async def call(self, spec, *, deadline_s: float, pressure: float) -> Any:
        # ① admission — the only gate that can say no for free
        if not self.policy.admit(spec.criticality, pressure):
            return await self.fallback(spec, reason="shed")

        # ② bulkhead — bounded concurrency + bounded wait, per class
        async def guarded() -> Any:
            breaker = self.breakers[spec.name]
            quota = self.quotas[spec.model]

            # ④ breaker before ⑤ quota: when the circuit is open, fail in
            #    microseconds and don't spend quota we could use elsewhere
            @breaker
            async def attempt() -> Any:
                # ⑤ quota: reserve max_tokens, call, reconcile
                await quota.reserve(spec.estimated_in_tokens, spec.max_tokens)
                started = time.monotonic()
                try:
                    message = await per_call_client(
                        self.client, timeout_s=spec.timeout_s, max_retries=0
                    ).messages.create(
                        model=spec.model_id,
                        max_tokens=spec.max_tokens,
                        messages=spec.messages,
                    )
                finally:
                    elapsed = time.monotonic() - started
                    log.debug("%s took %.2fs", spec.name, elapsed)
                quota.reconcile(spec.max_tokens, message.usage.output_tokens)
                self.budget.on_success()
                return message

            # ⑥ attempt loop — deadline-aware, budget-gated
            async def with_budget() -> Any:
                try:
                    return await attempt()
                except Exception as exc:
                    if is_retryable(exc) and self.budget.take():
                        delay = max(retry_after_seconds(exc),
                                    random.uniform(0, 4.0))  # full jitter
                        if delay < deadline_s:
                            await asyncio.sleep(delay)
                            return await attempt()
                    raise

            return await with_budget()

        try:
            return await self.bulkheads[spec.name].run(guarded)
        except (OverloadedError, Exception):
            # ⑦ fallback — degrade, never fabricate
            return await self.fallback(spec, reason="failed")

    async def fallback(self, spec, *, reason: str) -> Any:
        """Degrade along the ladder. The one thing never to do is invent an
        answer: a chat product that hallucinates under load has converted an
        availability incident into a correctness incident."""
        if spec.fallback == "raw_query":
            return spec.raw_query            # skip the rewrite, retrieve as-is
        if spec.fallback == "downgrade_to_answer":
            return await self.call(spec.cheaper_variant, deadline_s=10.0, pressure=0.0)
        if spec.fallback == "defer_to_batch":
            return await enqueue_batch(spec)  # the Batch API, later, half price
        if spec.fallback == "queue_for_review":
            return {"flagged": True}          # serve, review asynchronously
        raise OverloadedError(reason)         # honest "try again in a moment"


async def enqueue_batch(spec) -> Any:  # pragma: no cover - reference only
    """Deferred work belongs on the Batch API, not on the interactive quota."""
    raise NotImplementedError("wire this to client.messages.batches.create")
