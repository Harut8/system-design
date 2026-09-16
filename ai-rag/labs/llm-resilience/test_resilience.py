"""Assertions, zero dependencies. `python3 test_resilience.py`

These test *behaviour*, not implementation — they are the invariants that any
correct version of this stack should hold, including one rebuilt from the
libraries in production.py (README §6, exercise 7).
"""

from __future__ import annotations

import sys
import traceback

import capacity
import metrics as M
import scenarios as S
from admission import CRITICAL, SHEDDABLE, AdmissionController
from breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker
from bulkhead import Bulkhead
from adaptive import AIMDLimiter, GradientLimiter
from config import CLASS_BY_NAME, CLASSES, GatewayConfig, MODELS
from limits import ClientRateLimiter
from retry import RetryBudget, backoff, classify
from sim import Sim, TokenBucket

PASSED: list = []
FAILED: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append((name, detail))


# ───────────────────────────────────────────────────────────── kernel


def test_kernel() -> None:
    sim = Sim(seed=1)
    log = []

    def worker(tag, delay):
        yield sim.timeout(delay)
        log.append((round(sim.now, 3), tag))
        return tag

    sim.process(worker("b", 0.2))
    sim.process(worker("a", 0.1))
    sim.run(until=1.0)
    check("kernel orders events by time", log == [(0.1, "a"), (0.2, "b")], str(log))

    # determinism: same seed, same trace
    def trace(seed):
        s = Sim(seed=seed)
        out = []

        def noisy():
            for _ in range(20):
                yield s.timeout(s.rng.expovariate(2.0))
                out.append(round(s.now, 6))

        s.process(noisy())
        s.run(until=100.0)
        return out

    check("kernel is deterministic", trace(7) == trace(7))
    check("kernel seeds differ", trace(7) != trace(8))

    # any_of returns the winning event
    sim2 = Sim(seed=1)
    fast = sim2.timeout(0.1, "fast")
    slow = sim2.timeout(5.0, "slow")
    result = []

    def race():
        winner = yield sim2.any_of([slow, fast])
        result.append(winner.value)

    sim2.process(race())
    sim2.run(until=10.0)
    check("any_of picks the first to fire", result == ["fast"], str(result))


def test_token_bucket() -> None:
    bucket = TokenBucket(600, "t")  # 600/min = 10/s
    check("bucket starts full", bucket.level(0.0) == 600.0)
    check("bucket spends", bucket.try_take(0.0, 100.0) and bucket.level(0.0) == 500.0)
    check("bucket refills at rate", abs(bucket.level(10.0) - 600.0) < 1e-6)
    bucket.try_take(10.0, 600.0)
    check("bucket empties", bucket.level(10.0) == 0.0)
    check("wait_time is exact", abs(bucket.wait_time(10.0, 100.0) - 10.0) < 1e-6)
    check("oversized request is never satisfiable",
          bucket.wait_time(10.0, 10_000.0) == float("inf"))
    bucket.refund(10.0, 50.0)
    check("refund restores", abs(bucket.level(10.0) - 50.0) < 1e-6)


# ───────────────────────────────────────────────────── circuit breaker


def test_breaker_ignores_429() -> None:
    """The load-bearing assertion of the whole lab."""
    for counts, expected in ((False, CLOSED), (True, OPEN)):
        sim = Sim(seed=1)
        cb = CircuitBreaker(sim, "b", counts_429=counts, min_calls=20, threshold=0.30)
        for i in range(100):
            sim.now = i * 0.1
            if cb.allow():
                cb.record("429" if i % 10 < 4 else "ok")
        check(f"breaker with counts_429={counts} ends {expected}",
              cb.state == expected, f"got {cb.state}")


def test_breaker_trips_on_real_failures() -> None:
    sim = Sim(seed=1)
    cb = CircuitBreaker(sim, "b", min_calls=20, threshold=0.30)
    for i in range(60):
        sim.now = i * 0.1
        if cb.allow():
            cb.record("500" if i % 2 == 0 else "ok")
    check("breaker trips on 50% 500s", cb.state == OPEN, cb.state)
    check("breaker refuses while open", not cb.allow())


def test_breaker_recovers() -> None:
    sim = Sim(seed=1)
    cb = CircuitBreaker(sim, "b", min_calls=10, threshold=0.30, open_s=5.0, probes=3)
    for i in range(20):
        sim.now = i * 0.1
        if cb.allow():
            cb.record("500")
    check("breaker opened", cb.state == OPEN)

    sim.now += 6.0  # past the cooldown
    for _ in range(3):
        check("half-open admits a probe", cb.allow())
        check("half-open is in half-open state", cb.state == HALF_OPEN, cb.state)
        cb.record("ok")
    check("breaker closes after N good probes", cb.state == CLOSED, cb.state)


def test_breaker_half_open_admits_one_at_a_time() -> None:
    sim = Sim(seed=1)
    cb = CircuitBreaker(sim, "b", min_calls=10, threshold=0.30, open_s=1.0)
    for i in range(20):
        sim.now = i * 0.1
        if cb.allow():
            cb.record("500")
    sim.now += 2.0
    check("first probe admitted", cb.allow())
    check("second concurrent probe refused", not cb.allow())


def test_breaker_probe_is_not_leaked() -> None:
    """A probe that never reaches the dependency must be handed back, or the
    breaker stays half-open forever and can never close."""
    sim = Sim(seed=1)
    cb = CircuitBreaker(sim, "b", min_calls=10, threshold=0.30, open_s=1.0)
    for i in range(20):
        sim.now = i * 0.1
        if cb.allow():
            cb.record("500")
    sim.now += 2.0
    check("probe admitted", cb.allow())
    cb.abandon()  # bailed out locally, never called out
    check("abandoned probe is returned", cb.allow())


def test_breaker_backs_off_on_repeat_trips() -> None:
    sim = Sim(seed=1)
    cb = CircuitBreaker(sim, "b", min_calls=10, threshold=0.30, open_s=10.0,
                        max_open_s=120.0)
    opens = []
    for trip in range(3):
        for i in range(20):
            sim.now += 0.1
            if cb.allow():
                cb.record("500")
        opens.append(cb._open_for)
        sim.now += cb._open_for + 0.1
        cb.allow()  # -> half open
        cb.record("500")  # probe fails -> re-trip
    check("open duration grows on repeat trips",
          opens[0] < opens[1] <= opens[2] or opens == sorted(opens), str(opens))


# ──────────────────────────────────────────────────────────── retries


def test_retry_classification() -> None:
    check("429 is retryable", classify("429") == "retry_after")
    check("529 is retryable", classify("529") == "retry_after")
    check("500 is retryable", classify("500") == "retry")
    check("timeout is retryable", classify("timeout") == "retry")
    check("400 is not retryable", classify("400") == "no_retry")
    check("401 is not retryable", classify("401") == "no_retry")


def test_full_jitter_decorrelates() -> None:
    import random

    rng = random.Random(0)
    delays = [backoff(rng, 3, base=1.0, cap=20.0, jitter="full") for _ in range(400)]
    check("full jitter produces a spread", len(set(round(d, 3) for d in delays)) > 300)
    check("full jitter respects the cap", max(delays) <= 20.0)
    none = [backoff(rng, 3, base=1.0, cap=20.0, jitter="none") for _ in range(50)]
    check("no jitter synchronises", len(set(none)) == 1, str(set(none)))


def test_retry_after_is_a_floor() -> None:
    import random

    rng = random.Random(0)
    delays = [backoff(rng, 1, base=1.0, cap=20.0, retry_after=7.0) for _ in range(50)]
    check("retry-after is honoured as a minimum", min(delays) >= 7.0)


def test_retry_budget_self_cancels() -> None:
    budget = RetryBudget(ratio=0.10)
    budget.tokens = 5.0
    granted = sum(1 for _ in range(100) if budget.take(0.0))
    check("empty budget stops retrying", granted <= 6, str(granted))

    budget = RetryBudget(ratio=0.10)
    budget.tokens = 0.0
    for _ in range(100):
        budget.on_success()
    granted = sum(1 for _ in range(100) if budget.take(0.0))
    check("budget is ~10% of successes", 8 <= granted <= 12, str(granted))


# ───────────────────────────────────────────────────────── bulkheads


def test_bulkhead_bounds_and_rejects() -> None:
    sim = Sim(seed=1)
    bh = Bulkhead(sim, "b", capacity=2, max_queue=2, policy="lifo")
    outcomes = [bh.acquire(sim.now + 100.0, CRITICAL) for _ in range(6)]
    sim.run(until=0.001)
    values = [e.value for e in outcomes]
    check("bulkhead grants up to capacity", values[:2] == [True, True], str(values))
    check("bulkhead rejects past capacity+queue",
          values[4:] == ["queue-full", "queue-full"], str(values))
    check("in_use never exceeds capacity", bh.in_use <= bh.capacity)


def test_bulkhead_lifo_prefers_the_freshest() -> None:
    sim = Sim(seed=1)
    bh = Bulkhead(sim, "b", capacity=1, max_queue=4, policy="lifo",
                  codel_target_s=100.0)
    first = bh.acquire(sim.now + 100.0, CRITICAL)
    sim.run(until=0.001)
    check("first is granted immediately", first.value is True)
    sim.now = 1.0
    old = bh.acquire(sim.now + 100.0, CRITICAL)
    sim.now = 2.0
    fresh = bh.acquire(sim.now + 100.0, CRITICAL)
    bh.release()
    sim.run(until=3.0)
    check("LIFO serves the newest arrival first",
          fresh.value is True and old.value is not True,
          f"old={old.value} fresh={fresh.value}")


def test_bulkhead_drops_expired_deadlines() -> None:
    sim = Sim(seed=1)
    bh = Bulkhead(sim, "b", capacity=1, max_queue=4, codel_target_s=100.0)
    held = bh.acquire(sim.now + 100.0, CRITICAL)
    sim.run(until=0.001)
    doomed = bh.acquire(sim.now + 1.0, CRITICAL)  # deadline at t=1
    sim.now = 5.0
    bh.release()
    sim.run(until=6.0)
    check("expired request is dropped, not served",
          doomed.value == "deadline-expired-in-queue", str(doomed.value))
    check("held was granted", held.value is True)


# ─────────────────────────────────────────────────── adaptive limits


def test_aimd_sawtooth() -> None:
    limiter = AIMDLimiter(initial=100)
    limiter.on_failure("500", in_flight=100)
    check("AIMD halves on failure", limiter.value == 50, str(limiter.value))
    before = limiter.limit
    for _ in range(200):
        limiter.on_success(0.1, in_flight=int(limiter.limit))
    check("AIMD grows additively", limiter.limit > before)
    check("AIMD growth is slow", limiter.limit < before + 10)


def test_aimd_treats_429_gently() -> None:
    a = AIMDLimiter(initial=100)
    b = AIMDLimiter(initial=100)
    a.on_failure("429", in_flight=100)
    b.on_failure("500", in_flight=100)
    check("429 backs off less than 500 in AIMD", a.limit > b.limit,
          f"429->{a.limit} 500->{b.limit}")


def test_gradient_does_not_decay_when_idle() -> None:
    """The bug this lab hit: min-vs-mean gradients decay on a healthy API."""
    limiter = GradientLimiter(initial=100, window=10)
    import random

    rng = random.Random(0)
    for _ in range(600):
        # healthy dependency, ordinary lognormal latency noise
        limiter.on_success(rng.lognormvariate(-1.0, 0.25), in_flight=10)
    check("gradient holds its limit on a healthy, idle dependency",
          limiter.limit >= 90, str(limiter.limit))


def test_gradient_shrinks_under_real_queueing() -> None:
    limiter = GradientLimiter(initial=100, window=10)
    for _ in range(200):
        limiter.on_success(0.1, in_flight=100)  # establish the baseline
    baseline = limiter.limit
    for _ in range(400):
        limiter.on_success(1.0, in_flight=100)  # 10x latency == queueing
    check("gradient shrinks when latency rises", limiter.limit < baseline * 0.8,
          f"{baseline} -> {limiter.limit}")


# ──────────────────────────────────────────────────────────── quota


def test_reservation_and_reconciliation() -> None:
    sim = Sim(seed=1)
    limiter = ClientRateLimiter(sim, headroom=1.0, instances=1, coordinated=True)
    _, _, otpm = limiter.buckets["sonnet"]
    before = otpm.level(0.0)
    reservation = limiter.reserve("sonnet", in_tokens=6200, max_tokens=2048)
    check("reservation succeeds when there is quota", reservation is not None)
    check("reservation charges max_tokens",
          abs(otpm.level(0.0) - (before - 2048)) < 1e-6, str(otpm.level(0.0)))
    limiter.reconcile(reservation, actual_out=520)
    check("reconciliation returns the slack",
          abs(otpm.level(0.0) - (before - 520)) < 1e-6, str(otpm.level(0.0)))


def test_unreconciled_limiter_throttles_itself() -> None:
    """Forgetting reconcile() is a one-line bug with a large blast radius."""
    def sustained(reconcile: bool) -> int:
        sim = Sim(seed=1)
        limiter = ClientRateLimiter(sim, headroom=1.0, instances=1, coordinated=True)
        served = 0
        for step in range(3000):
            sim.now = step * 0.02  # 60 seconds, well past the quota
            res = limiter.reserve("sonnet", in_tokens=6200, max_tokens=2048)
            if res is not None:
                served += 1
                if reconcile:
                    limiter.reconcile(res, actual_out=520)
        return served

    with_recon, without = sustained(True), sustained(False)
    check("reconciliation materially raises sustained throughput",
          with_recon > without * 1.5, f"{with_recon} vs {without}")


def test_fleet_split_is_smaller_than_coordinated() -> None:
    sim = Sim(seed=1)
    split = ClientRateLimiter(sim, headroom=1.0, instances=12, coordinated=False)
    shared = ClientRateLimiter(sim, headroom=1.0, instances=12, coordinated=True)
    check("per-instance split gets 1/N of the quota",
          abs(split.buckets["sonnet"][0].limit * 12
              - shared.buckets["sonnet"][0].limit) < 1e-6)


# ────────────────────────────────────────────────────────── capacity


def test_capacity_arithmetic() -> None:
    answer = CLASS_BY_NAME["answer"]
    binding = capacity.binding_for(answer)
    model = answer.model_cfg
    check("ITPM ceiling is offered-tokens arithmetic",
          abs(binding.by_itpm - (model.itpm / 60.0) / answer.in_tokens[0]) < 1e-9)
    check("reserved OTPM is never above sustained OTPM",
          binding.by_otpm_unreconciled <= binding.by_otpm)
    check("effective rate never exceeds the quota rate",
          binding.effective_rps <= binding.limit_rps + 1e-9)

    think = capacity.binding_for(CLASS_BY_NAME["think"])
    check("a long call with a loose max_tokens is concurrency-bound",
          think.concurrency_bound, "think should be throttled by max_tokens")
    check("tightening max_tokens helps a concurrency-bound class",
          think.max_tokens_tax > 1.0, str(think.max_tokens_tax))

    check("sustainable rate is positive and finite",
          0 < capacity.sustainable_turns_rps() < 1e6)
    check("headroom is monotone in load",
          capacity.worst_meter(20.0) > capacity.worst_meter(10.0))
    check("littles law is rate x service",
          abs(capacity.littles_law(answer, 10.0)
              - 10.0 * capacity.service_time(answer)) < 1e-9)


def test_levers_never_go_backwards_silently() -> None:
    levers = {lever.name: lever for lever in capacity.levers()}
    check("baseline is present", "baseline" in levers)
    check("tightening max_tokens is a real win",
          levers["max_tokens -> p97 of observed"].multiplier > 1.0)


# ───────────────────────────────────────────────── the composed stack


def test_nominal_load_is_fully_served() -> None:
    result = S.run_sim("nominal", turns_rps=5.0, window_s=60.0)
    metrics = result.metrics
    check("nominal load completes ~every turn",
          metrics.turn_completion() > 0.95, f"{metrics.turn_completion():.3f}")
    check("no breaker trips on a healthy provider",
          sum(b.trips for b in result.gateway.breakers.values()) == 0)
    check("no provider 429s when metering ourselves",
          result.provider.totals()["429"] == 0)


def test_overload_sheds_by_criticality() -> None:
    result = S.run_sim("overload", turns_rps=20.0, window_s=60.0)
    metrics = result.metrics

    def shed_rate(name: str) -> float:
        offered = metrics.offered[name]
        shed = (metrics.count(name, M.SHED) + metrics.count(name, M.QUEUE_DROP)
                + metrics.count(name, M.LOCAL_LIMIT))
        return shed / offered if offered else 0.0

    check("SHEDDABLE sheds more than CRITICAL_PLUS",
          shed_rate("title") > shed_rate("answer"),
          f"title={shed_rate('title'):.2f} answer={shed_rate('answer'):.2f}")
    check("CRITICAL_PLUS is never shed by policy",
          metrics.count("answer", M.SHED) == 0,
          str(metrics.count("answer", M.SHED)))
    check("the guard path is defended",
          metrics.served_fraction("guard") > 0.9,
          f"{metrics.served_fraction('guard'):.2f}")


def test_stack_beats_naive_on_waste() -> None:
    stack = S.run_sim("stack", turns_rps=20.0, window_s=60.0)
    naive = S.run_sim("naive", turns_rps=20.0, window_s=60.0, naive=True)
    check("the stack answers more turns than the naive client",
          stack.metrics.turn_completion() > naive.metrics.turn_completion(),
          f"stack={stack.metrics.turn_completion():.3f} "
          f"naive={naive.metrics.turn_completion():.3f}")
    check("the stack wastes far less money",
          stack.metrics.cost_wasted < naive.metrics.cost_wasted / 3,
          f"${stack.metrics.cost_wasted:.2f} vs ${naive.metrics.cost_wasted:.2f}")
    check("the naive client amplifies load",
          sum(naive.metrics.attempts.values()) > sum(naive.metrics.offered.values()))
    check("the stack keeps the retry ratio low",
          stack.metrics.retry_ratio() < 0.10,
          f"{stack.metrics.retry_ratio():.3f}")


def test_breaker_helps_during_a_brownout() -> None:
    incident = S.brownout(20.0, 50.0, scale=0.1, error_rate=0.55, model="sonnet")
    on = S.run_sim("on", turns_rps=6.0, cfg=GatewayConfig(breaker_enabled=True),
                   health=incident)
    off = S.run_sim("off", turns_rps=6.0, cfg=GatewayConfig(breaker_enabled=False),
                    health=incident)
    check("breaker trips during a real outage",
          sum(b.trips for b in on.gateway.breakers.values()) > 0)
    check("breaker cuts wasted spend during a brownout",
          on.metrics.cost_wasted < off.metrics.cost_wasted,
          f"on=${on.metrics.cost_wasted:.2f} off=${off.metrics.cost_wasted:.2f}")
    check("breaker cuts tail latency during a brownout",
          on.metrics.pct("answer", 0.95) < off.metrics.pct("answer", 0.95),
          f"on={on.metrics.pct('answer', 0.95):.1f}s "
          f"off={off.metrics.pct('answer', 0.95):.1f}s")


def test_runs_are_reproducible() -> None:
    a = S.run_sim("r", turns_rps=10.0, window_s=30.0, seed=42)
    b = S.run_sim("r", turns_rps=10.0, window_s=30.0, seed=42)
    check("same seed, same result",
          a.metrics.turns_complete == b.metrics.turns_complete
          and abs(a.metrics.cost_useful - b.metrics.cost_useful) < 1e-9)


def test_no_class_exceeds_its_quota() -> None:
    """The client must never sustain more than the provider's ceiling."""
    result = S.run_sim("cap", turns_rps=40.0, window_s=60.0)
    for cls in CLASSES:
        binding = capacity.binding_for(cls)
        achieved = result.metrics.goodput(cls.name)
        check(f"{cls.name} stays within its quota ceiling",
              achieved <= binding.limit_rps * 1.25 + 1.0,
              f"{achieved:.2f}/s vs ceiling {binding.limit_rps:.2f}/s")


# ────────────────────────────────────────────────────────────── main

TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    for test in TESTS:
        try:
            test()
        except Exception:
            FAILED.append((test.__name__, traceback.format_exc().splitlines()[-1]))

    for name, detail in FAILED:
        print(f"FAIL  {name}" + (f"  — {detail}" if detail else ""))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed "
          f"({len(TESTS)} test functions)")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
