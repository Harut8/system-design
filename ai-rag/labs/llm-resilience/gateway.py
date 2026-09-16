"""The composed defence stack, in the order the gates have to run.

This file is the answer to "how do these patterns collaborate?". Each gate is
cheap relative to the one after it, and each one exists to stop a *different*
class of request from reaching the expensive part. Reorder them and the stack
stops working — the ordering notes below say why for each pair.

    turn arrives
        │
        ├─▶ ① ADMISSION  (admission.py)          ~0 ms, no I/O
        │     criticality ladder · deadline feasibility · per-tenant share
        │     WHY FIRST: this is the only gate that can say no without spending
        │     anything. A shed here costs one dict lookup; a shed three gates
        │     later has already consumed a queue slot and a quota reservation.
        │
        ├─▶ ② BULKHEAD   (bulkhead.py)           per-class slots + bounded queue
        │     LIFO · CoDel · deadline sweep
        │     WHY HERE: bounds how much of the *gateway* one class can occupy,
        │     so a 90-second Opus stall cannot starve the safety classifier.
        │     Must precede the quota gate: otherwise a stalled class holds
        │     reservations it cannot use.
        │
        ├─▶ ③ CONCURRENCY (adaptive.py)          per-class, latency-driven
        │     AIMD or Netflix gradient, fed time-to-first-token
        │     WHY HERE: bulkheads bound us by a *static* class budget; this
        │     bounds us by what the provider currently looks able to absorb,
        │     which moves minute to minute on a shared API.
        │
        ├─▶ ④ BREAKER    (breaker.py)            per model+class
        │     WHY BEFORE THE QUOTA GATE: when the circuit is open we want to
        │     fail in microseconds and *not* consume quota we could spend on a
        │     model that still works.
        │
        ├─▶ ⑤ QUOTA      (limits.py)             RPM · ITPM · OTPM reservation
        │     WHY LAST BEFORE THE CALL: this is the gate that must reflect
        │     reality at the instant of the call. Reserve max_tokens, call,
        │     reconcile against the real output.
        │
        ├─▶ ⑥ ATTEMPT LOOP (retry.py)            timeouts · classify · backoff
        │     per-operation timeouts → classify → budget → jittered backoff
        │     Feeds ④ (breaker) and ③ (limiter) on every outcome. This is the
        │     feedback edge: without it the two controllers above are blind.
        │
        └─▶ ⑦ FALLBACK                           degrade, don't fabricate

Two edges are easy to miss:

  * **⑥ → ④ excludes 429.** The attempt loop reports every outcome to the
    breaker *except* rate limiting. That single exclusion is what keeps the
    breaker from opening during ordinary quota exhaustion (Act 4).
  * **⑤ ↔ ⑥ reconciliation.** The quota gate can only be accurate if the
    attempt loop hands back the unused `max_tokens` after every call, including
    failed ones. Skip it and the client throttles itself to a fraction of its
    real allowance within a minute.
"""

from __future__ import annotations

from typing import Dict, Optional

import capacity
import metrics as M
from adaptive import make_limiter
from admission import ADMIT, AdmissionController, TenantLimiter
from breaker import CircuitBreaker
from bulkhead import Bulkhead
from config import CLASS_BY_NAME, MODELS, CallClass, GatewayConfig
from limits import ClientRateLimiter
from provider import Provider, Request
from retry import RetryBudget, cost_of_retry, decide
from sim import Resource, Sim


class CallResult:
    __slots__ = ("outcome", "latency", "ttft", "attempts", "detail")

    def __init__(self, outcome: str, latency: float = 0.0, ttft: float = 0.0,
                 attempts: int = 0, detail: str = "") -> None:
        self.outcome = outcome
        self.latency = latency
        self.ttft = ttft
        self.attempts = attempts
        self.detail = detail


class Gateway:
    def __init__(self, sim: Sim, provider: Provider, cfg: GatewayConfig,
                 window_s: float, tenants: Optional[TenantLimiter] = None) -> None:
        self.sim = sim
        self.provider = provider
        self.cfg = cfg
        self.metrics = M.Metrics(window_s)

        self.limiter = ClientRateLimiter(
            sim,
            headroom=cfg.limiter_headroom,
            instances=cfg.instances,
            coordinated=cfg.coordinated_limiter,
            cache_read_itpm_weight=cfg.cache_read_itpm_weight,
        )
        self.admission = AdmissionController(sim, self.limiter, cfg, tenants)

        # ② per-class bulkheads, sized by how long a call of that class holds a
        #    slot (Little's Law: slots = target_rps x seconds_held).
        self.bulkheads: Dict[str, Bulkhead] = {}
        for cls in CLASS_BY_NAME.values():
            hold = self._expected_duration(cls)
            slots = max(4, int(self._class_target_rps(cls) * hold * 1.5))
            self.bulkheads[cls.name] = Bulkhead(
                sim,
                cls.name,
                capacity=slots,
                max_queue=max(8, slots * 2),
                policy=cfg.queue_policy,
                codel_target_s=cfg.codel_target_s,
                codel_interval_s=cfg.codel_interval_s,
            )

        # ③ adaptive concurrency, scoped **per class, not per model**.
        #
        #    A latency-driven controller infers "the dependency is queueing"
        #    from a rise above the no-load baseline. Pool a 0.2s classifier and
        #    a 9s answer stream behind one controller and the baseline becomes
        #    the classifier's, so every single answer call reads as congestion
        #    and the limit decays to the floor — which then *causes* queueing,
        #    which the controller reads as more congestion. The signal is only
        #    meaningful across calls with the same shape.
        #
        #    The initial value comes from Little's Law at the quota-implied
        #    rate (concurrency = rate x service time), not from a round number.
        initial: Dict[str, int] = {}
        for cls in CLASS_BY_NAME.values():
            need = self._class_target_rps(cls) * self._expected_duration(cls)
            initial[cls.name] = max(cfg.adaptive_initial, int(need * 1.3) + 1)
        self.conc_limiters = {k: make_limiter(cfg.adaptive, v) for k, v in initial.items()}
        self.conc_slots = {k: Resource(sim, v) for k, v in initial.items()}
        self.conc_initial = initial

        # ④ one breaker per (model, class): a sick Opus must not open the
        #    circuit on Haiku, and a poisoned prompt in one class must not
        #    disable the others.
        self.breakers: Dict[str, CircuitBreaker] = {}
        for cls in CLASS_BY_NAME.values():
            self.breakers[cls.name] = CircuitBreaker(
                sim,
                f"{cls.model}:{cls.name}",
                threshold=cfg.breaker_threshold,
                window_s=cfg.breaker_window_s,
                min_calls=cfg.breaker_min_calls,
                open_s=cfg.breaker_open_s,
                probes=cfg.breaker_probes,
                counts_429=cfg.breaker_counts_429,
            )

        self.budget = RetryBudget(ratio=cfg.retry_budget_ratio)
        self.local_limit_waits = 0
        self.deadline_losses = 0

    # ------------------------------------------------------------- estimates

    def _expected_duration(self, cls: CallClass) -> float:
        model = cls.model_cfg
        return (
            model.base_latency_s
            + cls.in_tokens[0] / model.prefill_tok_s
            + cls.out_tokens[0] / model.decode_tok_s
        )

    def _class_target_rps(self, cls: CallClass) -> float:
        """This class's share of the model's quota, in requests/sec.

        Uses capacity.py's *reconciled* ceiling (sustained rate is governed by
        actual output tokens, with `max_tokens` capping concurrency) rather than
        the pessimistic reserved figure. Sizing pools off the reserved number
        throttles you to a fraction of the quota you are paying for.
        """
        siblings = [c for c in CLASS_BY_NAME.values() if c.model == cls.model]
        demand = sum(c.per_turn for c in siblings) or 1.0
        share = cls.per_turn / demand
        return share * capacity.binding_for(cls).effective_rps

    # ------------------------------------------------------------------- call

    def call(self, cls: CallClass, deadline_at: float, tenant: str = "default"):
        """A Process yielding a CallResult. This is the whole stack."""
        return self.sim.process(self._call(cls, deadline_at, tenant))

    def _call(self, cls: CallClass, deadline_at: float, tenant: str):
        sim = self.sim
        rng = sim.rng
        cfg = self.cfg
        started = sim.now
        self.metrics.offer(cls.name)

        expected = self._expected_duration(cls)

        # ---------------------------------------------------- ① admission
        decision = self.admission.admit(
            cls, deadline_left=deadline_at - sim.now, expected_s=expected, tenant=tenant
        )
        if decision.verdict != ADMIT:
            self.metrics.record(cls.name, M.SHED)
            return CallResult(M.SHED, detail=decision.verdict)

        # ---------------------------------------------------- ② bulkhead
        bulkhead = self.bulkheads[cls.name]
        if cfg.bulkhead_enabled:
            granted = yield bulkhead.acquire(deadline_at, cls.criticality)
            if granted is not True:
                self.metrics.record(cls.name, M.QUEUE_DROP)
                return CallResult(M.QUEUE_DROP, detail=str(granted))
        try:
            # ------------------------------------------------ ③ concurrency
            slots = self.conc_slots[cls.name]
            slot = slots.request()
            yield slot
            try:
                result = yield sim.process(
                    self._attempts(cls, deadline_at, started, expected)
                )
                return result
            finally:
                slots.release()
        finally:
            if cfg.bulkhead_enabled:
                bulkhead.release()

    def _attempts(self, cls: CallClass, deadline_at: float, started: float,
                  expected: float):
        sim = self.sim
        rng = sim.rng
        cfg = self.cfg
        model = cls.model_cfg
        breaker = self.breakers[cls.name]
        limiter = self.limiter
        attempt = 0
        last_status = ""

        while True:
            attempt += 1
            time_left = deadline_at - sim.now
            if time_left <= 0:
                self.metrics.record(cls.name, M.TIMEOUT)
                return CallResult(M.TIMEOUT, attempts=attempt - 1, detail="deadline")

            # -------------------------------------------------- ④ breaker
            if cfg.breaker_enabled and not breaker.allow():
                self.metrics.record(cls.name, M.BREAKER)
                return CallResult(M.BREAKER, attempts=attempt - 1, detail=breaker.state)

            # -------------------------------------------------- ⑤ quota
            in_tokens = max(50.0, rng.gauss(*cls.in_tokens))
            out_tokens = max(1.0, min(float(cls.max_tokens), rng.gauss(*cls.out_tokens)))
            cached_in = in_tokens * cls.cacheable_prefix
            reservation = None
            if cfg.client_limiter_enabled:
                wait = limiter.wait_time(cls.model, in_tokens, cls.max_tokens, cached_in)
                if wait > 0:
                    # Queue briefly for quota — but only briefly. Waiting longer
                    # than max_limiter_wait means demand exceeds the limit, which
                    # is a capacity problem no amount of waiting fixes.
                    if wait > min(cfg.max_limiter_wait_s, time_left):
                        breaker.abandon()  # never called out; don't eat the probe
                        self.local_limit_waits += 1
                        self.metrics.record(cls.name, M.LOCAL_LIMIT)
                        return CallResult(M.LOCAL_LIMIT, attempts=attempt - 1,
                                          detail="quota")
                    yield sim.timeout(wait)
                reservation = limiter.reserve(cls.model, in_tokens, cls.max_tokens, cached_in)
                if reservation is None:
                    breaker.abandon()
                    self.local_limit_waits += 1
                    self.metrics.record(cls.name, M.LOCAL_LIMIT)
                    return CallResult(M.LOCAL_LIMIT, attempts=attempt - 1, detail="race")

            # -------------------------------------------------- ⑥ attempt
            self.metrics.attempt(cls.name, retry=attempt > 1, reason=last_status)
            request = Request(
                model=cls.model,
                in_tokens=in_tokens,
                out_tokens=out_tokens,
                max_tokens=cls.max_tokens,
                cached_in_tokens=cached_in,
                streaming=cls.streaming,
            )
            ttft_event = sim.event()
            call = self.provider.call(request, ttft_event)

            t0 = sim.now
            # Three separate timeouts, per ch33 §9.3 step 1. A single number
            # cannot serve a 12-token classifier and a 2,400-token stream.
            #
            # `deadline_limited` records *whose fault* a timeout was. If we ran
            # out of caller deadline before the operation timeout fired, the
            # dependency did nothing wrong — we queued too long on our own side.
            # Feeding that back to the breaker and the concurrency limiter makes
            # both of them shrink in response to their own queueing, which is a
            # self-reinforcing collapse. It has to be classified separately.
            op_ttft = cls.timeouts.ttft
            ttft_budget = min(op_ttft, deadline_at - sim.now)
            deadline_limited = ttft_budget < op_ttft
            first = yield sim.any_of([ttft_event, call, sim.timeout(max(0.01, ttft_budget))])

            status = ""
            response = None
            observed_ttft = 0.0
            stream_fraction = 0.0

            if first is call:
                response = call.value
                status = response.status
                observed_ttft = response.ttft
                deadline_limited = False
            elif first is ttft_event:
                observed_ttft = sim.now - t0
                deadline_limited = False
                # `total` is measured from the start of the attempt, not from
                # first token — otherwise the two budgets silently add up.
                op_total = cls.timeouts.total - observed_ttft
                total_budget = min(op_total, deadline_at - sim.now)
                deadline_limited = total_budget < op_total
                done = yield sim.any_of([call, sim.timeout(max(0.01, total_budget))])
                if done is call:
                    response = call.value
                    status = response.status
                    deadline_limited = False
                else:
                    status = "timeout"
                    expected_decode = max(0.01, cls.out_tokens[0] / model.decode_tok_s)
                    stream_fraction = min(1.0, (sim.now - t0 - observed_ttft) / expected_decode)
            else:
                status = "timeout"  # never got a first token

            # -------------------------------------------- reconcile + feed back
            actual_out = response.out_tokens if (response and response.ok) else 0.0
            if reservation is not None:
                limiter.reconcile(reservation, actual_out)

            limiter_ctl = self.conc_limiters[cls.name]
            slots = self.conc_slots[cls.name]
            if deadline_limited:
                # our queueing, not their failure — tell neither controller
                self.deadline_losses += 1
                if cfg.breaker_enabled:
                    breaker.abandon()
            else:
                if cfg.breaker_enabled:
                    breaker.record(status)  # 429 filtered inside; see breaker.py
                if status == "ok":
                    limiter_ctl.on_success(max(0.001, observed_ttft), slots.in_use)
                else:
                    limiter_ctl.on_failure(status, slots.in_use)
                slots.set_capacity(limiter_ctl.value)

            if status == "ok" and response is not None:
                self.budget.on_success()
                self.metrics.cost_useful += model.cost(
                    response.in_tokens, response.out_tokens, response.cached_in_tokens
                )
                latency = sim.now - started
                self.metrics.record(cls.name, M.OK, latency=latency, ttft=observed_ttft)
                return CallResult(M.OK, latency=latency, ttft=observed_ttft,
                                  attempts=attempt)

            # a failed attempt still cost a prefill
            self.metrics.cost_wasted += cost_of_retry(cls, model)
            last_status = status

            verdict = decide(
                rng=rng,
                status=status,
                attempt=attempt,
                class_max_attempts=cls.max_attempts,
                retry_after=response.retry_after if response else 0.0,
                time_left=deadline_at - sim.now,
                expected_call_s=expected,
                budget=self.budget,
                now=sim.now,
                cfg=cfg,
                stream_fraction=stream_fraction,
            )
            if not verdict.retry:
                self.metrics.no_retry(verdict.reason)
                outcome = M.TIMEOUT if status == "timeout" else M.EXHAUSTED
                self.metrics.record(cls.name, outcome)
                return CallResult(outcome, attempts=attempt, detail=status)

            yield sim.timeout(verdict.delay)


class NaiveGateway:
    """The stack everyone actually ships first, for contrast.

    No admission control, no bulkheads, no breaker, no client-side metering,
    a fixed concurrency ceiling, one timeout for every call shape, and three
    retries with plain exponential backoff on every error — including 429.
    It is not a strawman; it is the default configuration of most HTTP clients
    plus a `for attempt in range(3)`.
    """

    def __init__(self, sim: Sim, provider: Provider, cfg: GatewayConfig,
                 window_s: float) -> None:
        self.sim = sim
        self.provider = provider
        self.cfg = cfg
        self.metrics = M.Metrics(window_s)
        self.attempts_total = 0

    def call(self, cls: CallClass, deadline_at: float, tenant: str = "default"):
        return self.sim.process(self._call(cls, deadline_at))

    def _call(self, cls: CallClass, deadline_at: float):
        sim = self.sim
        rng = sim.rng
        model = cls.model_cfg
        started = sim.now
        self.metrics.offer(cls.name)

        for attempt in range(1, 4):  # the famous `range(3)`
            self.metrics.attempt(cls.name, retry=attempt > 1, reason="naive")
            self.attempts_total += 1
            in_tokens = max(50.0, rng.gauss(*cls.in_tokens))
            out_tokens = max(1.0, min(float(cls.max_tokens), rng.gauss(*cls.out_tokens)))
            request = Request(cls.model, in_tokens, out_tokens, cls.max_tokens,
                              0.0, cls.streaming)
            ttft_event = sim.event()
            call = self.provider.call(request, ttft_event)
            done = yield sim.any_of([call, sim.timeout(30.0)])  # one global timeout
            if done is not call:
                self.metrics.cost_wasted += model.cost(in_tokens, 0.0)
                continue
            response = call.value
            if response.ok:
                latency = sim.now - started
                self.metrics.cost_useful += model.cost(
                    response.in_tokens, response.out_tokens
                )
                self.metrics.record(cls.name, M.OK, latency=latency, ttft=response.ttft)
                return CallResult(M.OK, latency=latency, ttft=response.ttft,
                                  attempts=attempt)
            self.metrics.cost_wasted += model.cost(in_tokens, 0.0)
            # exponential backoff, no jitter, no budget, no classification
            yield sim.timeout(min(8.0, 0.5 * (2 ** (attempt - 1))))

        self.metrics.record(cls.name, M.EXHAUSTED)
        return CallResult(M.EXHAUSTED, attempts=3)
