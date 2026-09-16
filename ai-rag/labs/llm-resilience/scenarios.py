"""Traffic generator and the five scenarios the report runs.

A *turn* is one user message. It fans out into a pipeline of LLM calls with
real dependencies between them, because the interesting failures are pipeline
failures: the safety classifier is cheap and mandatory, the rewrite is optional,
the answer is the product, and the title is nobody's problem.

    turn ──▶ guard ──▶ [rewrite] ──▶ retrieval ──▶ answer | think ──▶ [title]
             CRITICAL   SHEDDABLE_    (vector db,    CRITICAL_PLUS     SHEDDABLE
                        PLUS          not an LLM)    / CRITICAL

Degradation is expressed as *what the turn does when a call is refused*, which
is the only definition of "graceful" that survives contact with a product:

    guard shed      -> serve the turn, flag the conversation for async review
    rewrite shed    -> retrieve on the raw query (worse recall, still an answer)
    think shed      -> downgrade to the answer model (worse answer, still an answer)
    answer shed     -> the turn fails. This is the one we are protecting.
    title shed      -> enqueue for the Batch API; nobody notices

A turn counts as *complete* if the answer path produced something, *degraded* if
it did so with one or more rungs of the ladder active, and *failed* otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import metrics as M
from admission import TenantLimiter
from config import CLASS_BY_NAME, CLASSES, GatewayConfig
from gateway import Gateway, NaiveGateway
from provider import Provider
from sim import Sim


@dataclass
class RunResult:
    name: str
    gateway: object
    provider: Provider
    metrics: M.Metrics
    window_s: float
    turns_rps: float
    notes: Dict[str, float] = field(default_factory=dict)


class Driver:
    """Poisson arrivals of user turns, plus the per-turn pipeline."""

    def __init__(self, sim: Sim, gw, turns_rps: float, tenants: Optional[List[str]] = None,
                 tenant_weights: Optional[List[float]] = None) -> None:
        self.sim = sim
        self.gw = gw
        self.turns_rps = turns_rps
        self.tenants = tenants or ["default"]
        self.weights = tenant_weights or [1.0] * len(self.tenants)
        self.metrics = gw.metrics

    def run(self, until: float):
        return self.sim.process(self._arrivals(until))

    def _arrivals(self, until: float):
        sim = self.sim
        rng = sim.rng
        while sim.now < until:
            gap = rng.expovariate(self.turns_rps)
            yield sim.timeout(gap)
            if sim.now >= until:
                break
            tenant = rng.choices(self.tenants, weights=self.weights, k=1)[0]
            sim.process(self._turn(tenant))

    def _turn(self, tenant: str):
        sim = self.sim
        rng = sim.rng
        metrics = self.metrics
        metrics.turns_offered += 1
        degraded = False

        # 1. guard — mandatory, but has an async-review fallback
        guard = CLASS_BY_NAME["guard"]
        res = yield self.gw.call(guard, sim.now + guard.deadline_s, tenant)
        if res.outcome != M.OK:
            degraded = True  # flagged for review rather than blocked

        # 2. rewrite — optional
        rewrite = CLASS_BY_NAME["rewrite"]
        if rng.random() < rewrite.per_turn:
            res = yield self.gw.call(rewrite, sim.now + rewrite.deadline_s, tenant)
            if res.outcome != M.OK:
                degraded = True  # raw-query retrieval

        # 3. retrieval: a vector search, not an LLM call. Fast and not the
        #    bottleneck here, but it still costs the turn's deadline budget.
        yield sim.timeout(rng.lognormvariate(-3.0, 0.4))

        # 4. answer or think
        hard = rng.random() < CLASS_BY_NAME["think"].per_turn
        produced = False
        if hard:
            think = CLASS_BY_NAME["think"]
            res = yield self.gw.call(think, sim.now + think.deadline_s, tenant)
            if res.outcome == M.OK:
                produced = True
            else:
                degraded = True  # ladder rung L3: downgrade to the answer model

        if not produced:
            answer = CLASS_BY_NAME["answer"]
            res = yield self.gw.call(answer, sim.now + answer.deadline_s, tenant)
            produced = res.outcome == M.OK

        if produced:
            metrics.turns_complete += 1
            if degraded:
                metrics.turns_degraded += 1
        else:
            metrics.turns_failed += 1

        # 5. title — fire and forget, lowest criticality
        title = CLASS_BY_NAME["title"]
        if rng.random() < title.per_turn:
            sim.process(self._fire_and_forget(title, tenant))

    def _fire_and_forget(self, cls, tenant: str):
        yield self.gw.call(cls, self.sim.now + cls.deadline_s, tenant)


# --------------------------------------------------------------- scenarios


def run_sim(
    name: str,
    turns_rps: float,
    window_s: float = 60.0,
    cfg: Optional[GatewayConfig] = None,
    naive: bool = False,
    seed: int = 7,
    health=None,
    tenants: Optional[List[str]] = None,
    tenant_weights: Optional[List[float]] = None,
) -> RunResult:
    """One run. `health` is an optional fn(sim, provider) scheduling an incident."""
    sim = Sim(seed=seed)
    cfg = cfg or GatewayConfig()
    provider = Provider(sim)

    if naive:
        gw = NaiveGateway(sim, provider, cfg, window_s)
    else:
        tenant_limiter = None
        if tenants and len(tenants) > 1:
            fair = min(m.itpm for m in (c.model_cfg for c in CLASSES)) / len(tenants)
            tenant_limiter = TenantLimiter(sim, fair_share_tpm=fair)
        gw = Gateway(sim, provider, cfg, window_s, tenants=tenant_limiter)

    if health is not None:
        health(sim, provider)

    driver = Driver(sim, gw, turns_rps, tenants, tenant_weights)
    driver.run(window_s)
    # drain: let in-flight work finish so latency percentiles are not truncated
    sim.run(until=window_s + 180.0)

    return RunResult(name, gw, provider, gw.metrics, window_s, turns_rps)


def brownout(start: float, end: float, scale: float = 0.2, error_rate: float = 0.25,
             model: str = "sonnet"):
    """A provider-side incident: fleet shrinks, error rate spikes, 529s appear."""

    def install(sim: Sim, provider: Provider) -> None:
        sim.at(start, lambda: provider.set_health(model, server_scale=scale,
                                                  error_rate=error_rate))
        sim.at(end, lambda: provider.set_health(model, server_scale=1.0,
                                                error_rate=0.002))

    return install


def total_outage(start: float, end: float, model: str = "sonnet"):
    """The dependency is simply gone: every request errors."""

    def install(sim: Sim, provider: Provider) -> None:
        sim.at(start, lambda: provider.set_health(model, server_scale=0.05,
                                                  error_rate=1.0))
        sim.at(end, lambda: provider.set_health(model, server_scale=1.0,
                                                error_rate=0.002))

    return install


def capacity_step(at: float, scale: float, model: str = "sonnet"):
    """Capacity available to us changes (a noisy neighbour arrives or leaves)."""

    def install(sim: Sim, provider: Provider) -> None:
        sim.at(at, lambda: provider.set_health(model, server_scale=scale,
                                               error_rate=0.002))

    return install
