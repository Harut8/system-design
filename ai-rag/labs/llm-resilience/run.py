"""Run the lab and print the report. `python3 run.py [act ...]`, zero dependencies.

Each act makes one claim from `33-resilience-patterns-circuit-breakers.md` or
`34-adaptive-load-control-and-backpressure.md` observable on a concrete fixture:

    python3 run.py                  # everything
    python3 run.py capacity naive   # just those two
    python3 run.py --list           # the act list

Every number below is **rung 2 — simulated**: a consequence of the model in
provider.py and the fixture in config.py, both of which are invented (README
§7). They demonstrate mechanisms and orders of magnitude. They are not
measurements of any provider, and nothing here should be quoted as one.
"""

from __future__ import annotations

import sys
from typing import Dict, List

import capacity
import metrics as M
import scenarios as S
from config import (CLASS_BY_NAME, CLASSES, CRITICALITY_NAMES, FANOUT, MODELS,
                    GatewayConfig)

TARGET_TURNS_RPS = 1000.0


# ------------------------------------------------------------------ printing


def h1(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def h2(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def row(*cells, widths=None) -> None:
    widths = widths or []
    parts = []
    for i, cell in enumerate(cells):
        width = widths[i] if i < len(widths) else 12
        text = str(cell)
        parts.append(text.ljust(width) if i == 0 else text.rjust(width))
    print("  ".join(parts))


# --------------------------------------------------------------- act: capacity


def act_capacity() -> None:
    h1("1. The arithmetic: a 1,000 RPS app in front of a '100 RPS' API")

    total_rpm = sum(m.rpm for m in MODELS.values())
    print(f"""
The org's published ceilings add up to {total_rpm:,} requests/minute
= {total_rpm / 60:.0f} RPS. That is the number everyone quotes. It is almost
never the number that binds.

The app takes {TARGET_TURNS_RPS:,.0f} user turns/sec. Each turn fans out to
{FANOUT:.2f} LLM calls, so the offered LLM load is {TARGET_TURNS_RPS * FANOUT:,.0f} calls/sec
before a single retry.""")

    h2("Per class: which meter binds, and what it costs you")
    row("class", "model", "crit", "RPM", "ITPM", "OTPM", "binds", "quota", "eff",
        widths=[9, 7, 15, 7, 8, 8, 6, 8, 8])
    for binding in capacity.all_bindings():
        cls = binding.cls
        row(
            cls.name,
            cls.model,
            CRITICALITY_NAMES[cls.criticality],
            f"{binding.by_rpm:.0f}",
            f"{binding.by_itpm:.1f}",
            f"{binding.by_otpm:.1f}",
            binding.binds,
            f"{binding.limit_rps:.1f}/s",
            f"{binding.effective_rps:.1f}/s",
            widths=[9, 7, 15, 7, 8, 8, 6, 8, 8],
        )
    print("  (caps are req/s; `quota` = min of the three, `eff` = after the "
          "max_tokens concurrency cap)")

    h2("The max_tokens trap: a reservation you hold for the whole call")
    row("class", "max_tok", "actual", "conc cap", "conc need", "throttled?", "tax",
        widths=[9, 9, 9, 10, 11, 12, 7])
    for binding in capacity.all_bindings():
        cls = binding.cls
        row(
            cls.name,
            cls.max_tokens,
            f"{cls.out_tokens[0]:.0f}",
            f"{binding.concurrency_cap:.0f}",
            f"{binding.concurrency_needed:.0f}",
            "YES" if binding.concurrency_bound else "no",
            f"{binding.max_tokens_tax:.2f}x",
            widths=[9, 9, 9, 10, 11, 12, 7],
        )

    think = CLASS_BY_NAME["think"]
    tb = capacity.binding_for(think)
    print(f"""
The output-token meter charges `max_tokens` at admission and reconciles against
real output at completion. So `max_tokens` does *not* cap your sustained rate —
the reconciliation gives the slack back. What it caps is how many requests can
be in flight at once, because each one holds its full reservation for the whole
call:

    concurrency_cap = OTPM_bucket / max_tokens

That bites exactly where calls are long. `think` asks for {think.max_tokens} tokens and
writes about {think.out_tokens[0]:.0f}, on a call that runs ~{capacity.service_time(think):.0f}s. It can hold only
{tb.concurrency_cap:.0f} in flight when its own token quota would support {tb.concurrency_needed:.0f} — so it runs at
{tb.effective_rps:.2f} req/s instead of {tb.limit_rps:.2f}. Sizing `max_tokens` to the p97 of observed
output buys {tb.max_tokens_tax:.2f}x on that class, for one config line and no quality change.

Two corollaries worth internalising:

  * a 9-second answer stream has plenty of concurrency headroom; a 70-second
    reasoning call does not. The same `max_tokens` slack costs nothing in one
    place and a factor of two in the other.
  * if your client-side limiter reserves and forgets to reconcile (limits.py,
    one line), you throttle yourself to the `unreconciled` column instead —
    {capacity.binding_for(CLASS_BY_NAME['answer']).by_otpm_unreconciled:.1f} req/s on `answer` rather than {capacity.binding_for(CLASS_BY_NAME['answer']).by_otpm:.1f}.""")

    h2("Offered vs. quota at 1,000 turns/sec (ratio; >1.0 is over)")
    room = capacity.headroom(TARGET_TURNS_RPS)
    row("model", "RPM", "ITPM", "OTPM", "in-flight", widths=[10, 11, 11, 11, 11])
    for key, values in room.items():
        row(key, f"{values['rpm']:.1f}x", f"{values['itpm']:.1f}x",
            f"{values['otpm']:.1f}x", f"{values['conc']:.1f}x",
            widths=[10, 11, 11, 11, 11])

    sustainable = capacity.sustainable_turns_rps()
    gap = TARGET_TURNS_RPS / sustainable if sustainable else float("inf")
    print(f"""
Sustainable user-turn rate with this workload shape: {sustainable:.2f} turns/sec.
Target: {TARGET_TURNS_RPS:,.0f}. The gap is {gap:,.0f}x.

No retry policy closes a {gap:,.0f}x gap. No circuit breaker closes it. No adaptive
concurrency limit closes it. This is a capacity problem, and the honest
engineering response has two halves:

  (a) move the edge  — change the workload (levers below) or buy more quota;
  (b) fail well at the edge — which is what the rest of this lab is about.

Conflating the two is how teams end up with a retry storm and a bill.""")

    h2("Concurrency you must be able to hold (Little's Law)")
    print("Pool/bulkhead sizes come from this, not from a round number.\n")
    row("class", "svc time", "at cap", "concurrency", widths=[9, 12, 12, 14])
    for binding in capacity.all_bindings():
        cls = binding.cls
        service = capacity.service_time(cls)
        row(cls.name, f"{service:.2f}s", f"{binding.limit_rps:.1f}/s",
            f"{capacity.littles_law(cls, binding.limit_rps):.1f}",
            widths=[9, 12, 12, 14])

    h2("What each lever buys (sustainable user turns/sec)")
    for lever in capacity.levers():
        print(f"  {lever.name:<28} {lever.turns_rps:7.2f} turns/s "
              f"({lever.multiplier:4.2f}x)  {lever.detail}")
        print(f"  {'':<28} {'':>7}                cost: {lever.cost}")
    print("""
  Several of those buy exactly 1.00x. That is the most common wasted
  optimisation in LLM infrastructure: relieving a meter that was not the one
  binding. Halving your RAG context does nothing when the constraint is an
  in-flight reservation on a different model. Find the binding meter first.""")

    best = max(capacity.levers(), key=lambda lever: lever.turns_rps)
    remaining = TARGET_TURNS_RPS / best.turns_rps if best.turns_rps else float("inf")
    print(f"""
Even after every cheap lever, the gap is still {remaining:,.0f}x. Adding a 60%
whole-response cache leaves {capacity.response_cache_effect(TARGET_TURNS_RPS, 0.6):,.0f} turns/sec to serve, which still
needs roughly {capacity.tier_needed(capacity.response_cache_effect(TARGET_TURNS_RPS, 0.6)):,.0f}x the current quota. Write that number down and take
it to whoever owns the contract — that is the deliverable of this act.""")


# ------------------------------------------------------------- act: naive


def _outcome_table(result: S.RunResult, title: str) -> None:
    h2(title)
    metrics = result.metrics
    row("class", "offered/s", "good/s", "served", "p50", "p95", "shed", "err",
        widths=[9, 10, 9, 8, 8, 8, 8, 8])
    for cls in CLASSES:
        name = cls.name
        shed = (metrics.count(name, M.SHED) + metrics.count(name, M.QUEUE_DROP)
                + metrics.count(name, M.LOCAL_LIMIT) + metrics.count(name, M.BREAKER))
        err = metrics.count(name, M.EXHAUSTED) + metrics.count(name, M.TIMEOUT)
        row(
            name,
            f"{metrics.offered_rps(name):.1f}",
            f"{metrics.goodput(name):.1f}",
            f"{metrics.served_fraction(name) * 100:.0f}%",
            f"{metrics.pct(name, 0.50):.2f}s",
            f"{metrics.pct(name, 0.95):.2f}s",
            f"{shed}",
            f"{err}",
            widths=[9, 10, 9, 8, 8, 8, 8, 8],
        )
    totals = result.provider.totals()
    print(f"""
  turns: {metrics.turns_offered} offered, {metrics.turns_complete} answered """
          f"""({metrics.turn_completion() * 100:.1f}%), {metrics.turns_degraded} degraded, """
          f"""{metrics.turns_failed} failed
  provider: {int(totals['ok'])} ok, {int(totals['429'])} x429, {int(totals['529'])} x529, """
          f"""{int(totals['500'])} x500
  attempts: {sum(metrics.attempts.values())}, of which {sum(metrics.retries.values())} retries """
          f"""({metrics.retry_ratio() * 100:.1f}%)
  spend: ${metrics.cost_useful:.2f} useful + ${metrics.cost_wasted:.2f} wasted """
          f"""({metrics.retry_cost_share() * 100:.1f}% of spend bought nothing)""")


def act_naive() -> None:
    h1("2. No stack: three retries, one timeout, and hope")

    load = 20.0
    naive = S.run_sim("naive", turns_rps=load, naive=True)
    print(f"""
{load:.0f} user turns/sec — roughly {load * FANOUT:.0f} LLM calls/sec, against a fixture
sized for about {capacity.sustainable_turns_rps():.1f}. Every call site does what the default
HTTP client and a `for attempt in range(3)` do: retry everything three times,
exponential backoff with no jitter, no classification, no budget, one 30-second
timeout for a 12-token classifier and a 2,400-token reasoning stream alike.""")
    _outcome_table(naive, "Naive gateway")

    metrics = naive.metrics
    totals = naive.provider.totals()
    attempts = sum(metrics.attempts.values())
    offered = sum(metrics.offered.values())
    print(f"""
Three things to notice:

  * amplification — {offered} calls became {attempts} attempts
    ({attempts / offered if offered else 0:.2f}x). Under a 429 storm the client's answer to
    "you are over quota" is to go over quota harder. Chapter 33 §2.1.
  * the 429s are not failures — {int(totals['429'])} of the errors here are the provider
    telling us to slow down. Retrying them on a fixed schedule with no jitter
    re-synchronises every caller onto the same future second.
  * the timeout is wrong for every class — one number cannot serve a 0.3s
    classifier and a 60s stream. Chapter 33 §9.3.""")


def act_stack() -> None:
    h1("3. The full stack at the same load")

    load = 20.0
    result = S.run_sim("stack", turns_rps=load, cfg=GatewayConfig())
    _outcome_table(result, f"Composed gateway, {load:.0f} turns/sec")

    naive = S.run_sim("naive", turns_rps=load, naive=True)
    gw_good = result.metrics.all_goodput()
    naive_good = naive.metrics.all_goodput()
    print(f"""
Same offered load, same provider, same seed.

  goodput          naive {naive_good:7.1f} calls/s   stack {gw_good:7.1f} calls/s
  turn completion  naive {naive.metrics.turn_completion() * 100:6.1f}%        stack {result.metrics.turn_completion() * 100:6.1f}%
  attempts         naive {sum(naive.metrics.attempts.values()):7d}          stack {sum(result.metrics.attempts.values()):7d}
  wasted spend     naive ${naive.metrics.cost_wasted:7.2f}          stack ${result.metrics.cost_wasted:7.2f}

The stack does not serve more requests than the quota allows — nothing can. It
changes *which* requests get served and how the rest are refused: instantly,
cheaply, and starting with the ones nobody is waiting on.""")

    h2("Where the shedding landed, by criticality")
    metrics = result.metrics
    for cls in sorted(CLASSES, key=lambda c: c.criticality):
        shed = (metrics.count(cls.name, M.SHED) + metrics.count(cls.name, M.QUEUE_DROP)
                + metrics.count(cls.name, M.LOCAL_LIMIT))
        offered_n = metrics.offered[cls.name]
        pct = shed / offered_n * 100 if offered_n else 0.0
        bar = "#" * int(pct / 2)
        print(f"  {CRITICALITY_NAMES[cls.criticality]:<15} {cls.name:<9} "
              f"{pct:5.1f}% shed  {bar}")
    print("""
That ordering is the whole design: the product degrades from the outside in.
Titles stop first, then query rewriting, then the thinking tier, and the answer
path is defended to the last.""")


# -------------------------------------------------------- act: 429 vs 529


def act_breaker() -> None:
    h1("4. The 429/529 distinction, and the breaker flag that depends on it")

    print("""
A 429 means "you are over your quota" — the dependency is healthy and has told
you exactly how long to wait. A 529 means "our fleet is in trouble" — every
tenant sees it, and the wait is unbounded. They arrive as almost identical HTTP
responses and demand opposite reactions.

The consequence lands on one flag: whether 429s count toward the circuit
breaker's failure ratio.""")

    # A direct demonstration on the breaker itself rather than a full run:
    # in the composed stack the client-side limiter and the bulkheads keep the
    # issued rate under quota, so almost no 429 ever reaches the provider —
    # which is the point of having them. To see what the flag does, feed a
    # breaker the traffic it would see *without* those gates: a healthy
    # dependency that is answering 70% of calls and rate-limiting the other 30%.
    from breaker import CircuitBreaker
    from sim import Sim

    h2("Same call stream, one flag different")
    row("config", "state after 200 calls", "trips", "calls refused",
        widths=[24, 24, 8, 15])
    for label, flag in (("429 counts as failure", True), ("429 excluded (correct)", False)):
        clock = Sim(seed=3)
        cb = CircuitBreaker(clock, "demo", counts_429=flag)
        refused = 0
        for i in range(200):
            clock.now = i * 0.1
            if not cb.allow():
                refused += 1
                continue
            cb.record("429" if i % 10 < 3 else "ok")
        row(label, cb.state, cb.trips, refused, widths=[24, 24, 8, 15])

    print("""
When 429s count, the breaker opens on a working API and then refuses the
requests that *did* have quota — a self-inflicted outage layered on top of a
rate limit. Excluding them, the breaker stays closed and the client-side
limiter (limits.py) handles quota where it belongs.

The breaker's job is to detect a *broken* dependency. Now give it one.""")

    h2("A real provider brownout: fleet down to 10%, 55% error rate, t=20..50s")
    incident = S.brownout(20.0, 50.0, scale=0.1, error_rate=0.55, model="sonnet")
    with_breaker = S.run_sim("breaker-on", turns_rps=6.0,
                             cfg=GatewayConfig(breaker_enabled=True), health=incident)
    without = S.run_sim("breaker-off", turns_rps=6.0,
                        cfg=GatewayConfig(breaker_enabled=False), health=incident)

    row("config", "trips", "attempts", "p95 answer", "wasted $", "turns ok",
        widths=[16, 8, 10, 12, 10, 10])
    for label, result in (("breaker on", with_breaker), ("breaker off", without)):
        trips = sum(br.trips for br in result.gateway.breakers.values())
        row(
            label,
            trips,
            sum(result.metrics.attempts.values()),
            f"{result.metrics.pct('answer', 0.95):.2f}s",
            f"${result.metrics.cost_wasted:.2f}",
            f"{result.metrics.turn_completion() * 100:.1f}%",
            widths=[16, 8, 10, 12, 10, 10],
        )

    print("""
With the breaker, requests during the incident fail in microseconds against an
open circuit instead of occupying a thread for the full timeout, and the fleet
gets probed by one cheap request at a time rather than by the full arrival rate.
Without it, every caller discovers the outage independently, one timeout each.""")

    h2("Why 429s must not reach the breaker, in one line")
    print("""  breaker.record() returns early on status == "429" (breaker.py).
  That single early return is the difference between the two tables above.""")


# ------------------------------------------------------ act: retry budget


def act_retries() -> None:
    h1("5. Retry budget and jitter: bounding the amplification")

    incident = S.brownout(15.0, 45.0, scale=0.1, error_rate=0.55, model="sonnet")
    configs = [
        ("no budget, no jitter", GatewayConfig(retry_budget_ratio=100.0, jitter="none")),
        ("no budget, full jitter", GatewayConfig(retry_budget_ratio=100.0, jitter="full")),
        ("10% budget, full jitter", GatewayConfig(retry_budget_ratio=0.10, jitter="full")),
        ("no retries at all", GatewayConfig(retry_budget_ratio=0.0, jitter="full")),
    ]

    row("policy", "attempts", "retry%", "wasted $", "turns ok", "p95 answer",
        widths=[24, 10, 8, 10, 10, 11])
    for label, cfg in configs:
        result = S.run_sim(label, turns_rps=6.0, cfg=cfg, health=incident)
        metrics = result.metrics
        row(
            label,
            sum(metrics.attempts.values()),
            f"{metrics.retry_ratio() * 100:.1f}%",
            f"${metrics.cost_wasted:.2f}",
            f"{metrics.turn_completion() * 100:.1f}%",
            f"{metrics.pct('answer', 0.95):.2f}s",
            widths=[24, 10, 8, 10, 10, 11],
        )

    print("""
The budget is the mechanism that makes retries safe to enable at all. A
per-call-site attempt count multiplies load precisely when the dependency is
least able to absorb it; a budget expressed as a fraction of *successes*
self-cancels during a real outage, because there are no successes to fund it.

Note the last row. Sometimes "no retries" is competitive — retries pay off when
failures are independent (a single bad GPU node), and pay nothing when they are
correlated (the whole fleet is down). Measure which one you have before
tuning the backoff curve.""")

    h2("Why retries are not free on an LLM API")
    answer = CLASS_BY_NAME["answer"]
    model = answer.model_cfg
    per_retry = model.cost(answer.in_tokens[0], 0.0)
    print(f"""  A failed `answer` attempt re-processes {answer.in_tokens[0]:,.0f} input tokens:
  ${per_retry:.5f} per attempt, returning nothing. At {TARGET_TURNS_RPS:,.0f} turns/sec with a 5%
  retry rate that is ${per_retry * TARGET_TURNS_RPS * 0.05 * 86400:,.0f}/day of spend that bought zero answers.
  Alert on retry_cost / total_cost > 5%, not just on error rate.""")


# --------------------------------------------------- act: adaptive limits


def act_adaptive() -> None:
    h1("6. Adaptive concurrency vs. the number you typed in 2023")

    print("""
Capacity available to you on a shared API moves. At t=25s a noisy neighbour
arrives and the effective fleet drops to 35%; at t=45s it leaves. A fixed
concurrency limit is wrong in both directions.""")

    def two_steps(sim, provider):
        sim.at(25.0, lambda: provider.set_health("sonnet", 0.35, 0.002))
        sim.at(45.0, lambda: provider.set_health("sonnet", 1.0, 0.002))

    row("limiter", "goodput/s", "p95 answer", "timeouts", "final limit",
        widths=[12, 11, 12, 10, 12])
    for kind in ("fixed", "aimd", "gradient"):
        result = S.run_sim(kind, turns_rps=6.0,
                           cfg=GatewayConfig(adaptive=kind), health=two_steps)
        timeouts = sum(result.metrics.count(c.name, M.TIMEOUT) for c in CLASSES)
        limit = result.gateway.conc_limiters["answer"].value
        row(kind, f"{result.metrics.all_goodput():.1f}",
            f"{result.metrics.pct('answer', 0.95):.2f}s", timeouts, limit,
            widths=[12, 11, 12, 10, 12])

    print("""
The gradient controller is fed *time to first token*, not total duration. That
detail matters more than the choice of algorithm: total duration on an LLM call
is dominated by how many tokens the answer happened to need, so a controller fed
raw latency shrinks the limit whenever users ask harder questions. TTFT is the
part that actually reflects queueing.""")


# -------------------------------------------------------- act: degradation


def act_ladder() -> None:
    h1("7. The degradation ladder: what each rung buys")

    print("""
Load climbs from comfortable to 4x over. With the ladder enabled, each rung
trades a feature the user barely notices for the one they would.""")

    row("turns/s", "ladder", "turn ok%", "degraded%", "answer good/s", "title good/s",
        widths=[9, 9, 10, 11, 15, 14])
    for load in (5.0, 10.0, 20.0, 40.0):
        for enabled in (True, False):
            result = S.run_sim(
                f"ladder-{load}-{enabled}",
                turns_rps=load,
                cfg=GatewayConfig(degrade_enabled=enabled),
            )
            metrics = result.metrics
            degraded = (metrics.turns_degraded / metrics.turns_offered * 100
                        if metrics.turns_offered else 0.0)
            row(
                f"{load:.0f}" if enabled else "",
                "on" if enabled else "off",
                f"{metrics.turn_completion() * 100:.1f}%",
                f"{degraded:.1f}%",
                f"{metrics.goodput('answer'):.1f}",
                f"{metrics.goodput('title'):.1f}",
                widths=[9, 9, 10, 11, 15, 14],
            )

    print("""
Read the `title good/s` column against `turn ok%`. The ladder converts summariser
throughput into answered turns — it is not producing capacity, it is *reallocating*
it from the class nobody is waiting on to the class the product is.

Without the ladder every class competes equally for the same quota, so the
SHEDDABLE title calls consume input-token budget that the CRITICAL_PLUS answer
calls needed. That is the multi-tenant fairness problem (ch34 §9) turning up
*inside a single tenant*, between its own call classes.""")


def act_meters() -> None:
    h1("8. Client-side metering: the fleet-share problem")

    print("""
The quota is org-level. Your service is 12 pods. Two ways to divide it:

  split      each pod meters itself at limit/12. No coordination, no
             dependency — and wrong the moment load is uneven.
  shared     one Redis-backed bucket. 1-2ms per check, which is nothing
             against an 800ms LLM call, and it needs a documented fallback
             to `split` for when Redis is unreachable.""")

    row("mode", "429s seen", "local rejects", "goodput/s", "turns ok",
        widths=[24, 12, 15, 11, 10])
    for label, coordinated in (("per-instance split", False), ("coordinated (Redis)", True)):
        result = S.run_sim(label, turns_rps=6.0,
                           cfg=GatewayConfig(coordinated_limiter=coordinated))
        totals = result.provider.totals()
        rejects = sum(result.metrics.count(c.name, M.LOCAL_LIMIT) for c in CLASSES)
        row(label, int(totals["429"]), rejects, f"{result.metrics.all_goodput():.1f}",
            f"{result.metrics.turn_completion() * 100:.1f}%",
            widths=[24, 12, 15, 11, 10])

    no_limiter = S.run_sim("none", turns_rps=6.0,
                           cfg=GatewayConfig(client_limiter_enabled=False))
    print(f"""  {'no client limiter':<24} {int(no_limiter.provider.totals()['429']):>12} """
          f"""{0:>15} {no_limiter.metrics.all_goodput():>10.1f} """
          f"""{no_limiter.metrics.turn_completion() * 100:>9.1f}%""")

    print("""
Every 429 in the first column is a wasted round trip, a polluted error metric,
and a data point the circuit breaker has to be told to ignore. Metering locally
converts it into a zero-cost local decision — and, more importantly, into one
you can act on: a local reject knows the class and the criticality, so it can
degrade. A 429 from the provider knows neither.""")

    h2("Reservation and reconciliation")
    result = S.run_sim("recon", turns_rps=6.0, cfg=GatewayConfig())
    limiter = result.gateway.limiter
    for key in ("haiku", "sonnet", "opus"):
        observed = limiter.p95_observed_out(key)
        classes = [c for c in CLASSES if c.model == key]
        reserved = max(c.max_tokens for c in classes)
        if observed:
            print(f"  {key:<8} reserved up to {reserved:>5} max_tokens, "
                  f"p95 actual output {observed:>6.0f}  "
                  f"({reserved / observed:.1f}x over-reserved)")
    print(f"""
  {limiter.reconciled_tokens:,.0f} output tokens were reserved and handed back over the run.
  Until that reconciliation lands, those tokens are quota you cannot spend —
  which is why an OTPM meter without reconciliation throttles you to a fraction
  of your real allowance within a minute.""")


# ------------------------------------------------------------------ registry

ACTS = {
    "capacity": (act_capacity, "the arithmetic: 1,000 RPS app vs a '100 RPS' API"),
    "naive": (act_naive, "no patterns: retry storms and one global timeout"),
    "stack": (act_stack, "the composed stack at the same load"),
    "breaker": (act_breaker, "429 vs 529, and the flag that depends on it"),
    "retries": (act_retries, "retry budgets, jitter and what a retry costs"),
    "adaptive": (act_adaptive, "AIMD vs gradient vs a fixed limit"),
    "ladder": (act_ladder, "the degradation ladder, rung by rung"),
    "meters": (act_meters, "client-side metering, fleet shares, reconciliation"),
}

ORDER = ["capacity", "naive", "stack", "breaker", "retries", "adaptive", "ladder", "meters"]


def main(argv: List[str]) -> int:
    if "--list" in argv:
        for name in ORDER:
            print(f"  {name:<10} {ACTS[name][1]}")
        return 0
    wanted = [a for a in argv if not a.startswith("-")] or ORDER
    unknown = [a for a in wanted if a not in ACTS]
    if unknown:
        print(f"unknown act(s): {', '.join(unknown)}")
        print(f"available: {', '.join(ORDER)}")
        return 1
    for name in wanted:
        ACTS[name][0]()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
