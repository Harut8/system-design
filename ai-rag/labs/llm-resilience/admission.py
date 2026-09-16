"""Admission control and the degradation ladder — deciding what *not* to send.

This is the pattern that actually closes a capacity gap, and the only one in
the stack that makes the system faster by doing less. Everything downstream
(bulkheads, breakers, retries) manages requests that are already inside; this
decides which requests get inside at all.

Three gates, applied in order:

1. **Criticality shedding** (ch34 §4.3). Pressure is read from the client-side
   token buckets — not CPU, because on an LLM gateway the CPU is idle while the
   quota is the thing that's exhausted. As pressure rises, whole classes stop
   being admitted, cheapest-to-lose first:

       pressure >= 0.80   shed SHEDDABLE            (title/summary)
       pressure >= 0.88   + SHEDDABLE_PLUS          (query rewrite)
       pressure >= 0.95   + CRITICAL                (think -> downgrade)
       CRITICAL_PLUS                                 never shed here

   Note what that ladder means in product terms: under load the app stops
   naming conversations, then stops rewriting queries, then stops routing
   anything to the thinking model — and only then does it start telling users
   no. Each rung is a feature the user barely notices, bought at the price of
   the one thing they would.

2. **Deadline feasibility** (ch34 §3.3). If the predicted queue wait plus the
   expected call duration already exceeds what the caller can use, reject now.
   Accepting it burns a full prefill to produce an answer nobody reads — the
   LLM version of "work that is already dead".

3. **Per-tenant fairness** (ch34 §9). One tenant's batch job must not consume
   the org quota that every other tenant's interactive traffic needs. A
   per-tenant token bucket sized at a multiple of the fair share allows bursts
   while bounding sustained monopolisation.

`Degrader` is the ladder as a queryable object so the report can show which rung
is active and what it bought.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from config import CRITICAL, CRITICAL_PLUS, SHEDDABLE, SHEDDABLE_PLUS
from sim import Sim, TokenBucket

ADMIT = "admit"
SHED_CRITICALITY = "shed-criticality"
SHED_DEADLINE = "shed-deadline"
SHED_TENANT = "shed-tenant"

# pressure -> lowest criticality still admitted
LADDER = (
    (0.95, CRITICAL_PLUS),
    (0.88, CRITICAL),
    (0.80, SHEDDABLE_PLUS),
    (0.00, SHEDDABLE),
)

LADDER_RUNGS = (
    (0.00, "L0 full service"),
    (0.80, "L1 stop titling conversations"),
    (0.88, "L2 + skip query rewrite, retrieve on the raw query"),
    (0.95, "L3 + route 'think' turns down to the answer model"),
    (1.01, "L4 + honest 'try again in a moment' on the answer path"),
)


@dataclass
class Decision:
    verdict: str
    reason: str = ""
    degrade_to: str = ""


class TenantLimiter:
    """Per-tenant bucket over *input tokens*, the resource that actually binds."""

    def __init__(self, sim: Sim, fair_share_tpm: float, burst_multiple: float = 3.0) -> None:
        self.sim = sim
        self.fair_share = fair_share_tpm
        self.burst_multiple = burst_multiple
        self.buckets: Dict[str, TokenBucket] = {}
        self.shed: Dict[str, int] = {}

    def allow(self, tenant: str, tokens: float) -> bool:
        bucket = self.buckets.get(tenant)
        if bucket is None:
            bucket = TokenBucket(self.fair_share * self.burst_multiple, tenant)
            bucket.rate = self.fair_share / 60.0  # burst big, sustained rate fair
            self.buckets[tenant] = bucket
        if bucket.try_take(self.sim.now, tokens):
            return True
        self.shed[tenant] = self.shed.get(tenant, 0) + 1
        return False


class AdmissionController:
    def __init__(self, sim: Sim, limiter, cfg, tenant_limiter: TenantLimiter | None = None) -> None:
        self.sim = sim
        self.limiter = limiter
        self.cfg = cfg
        self.tenants = tenant_limiter
        self.shed_counts: Dict[str, int] = {}
        self.pressure_samples: list = []

    def pressure(self, model: str) -> float:
        return self.limiter.pressure(model)

    def floor_for(self, pressure: float) -> int:
        for threshold, criticality in LADDER:
            if pressure >= threshold:
                return criticality
        return SHEDDABLE

    def rung(self, pressure: float) -> str:
        label = LADDER_RUNGS[0][1]
        for threshold, name in LADDER_RUNGS:
            if pressure >= threshold:
                label = name
        return label

    def admit(self, call_class, deadline_left: float, expected_s: float,
              tenant: str = "default") -> Decision:
        if not self.cfg.admission_enabled:
            return Decision(ADMIT)

        pressure = self.pressure(call_class.model)
        self.pressure_samples.append(pressure)

        if self.cfg.degrade_enabled:
            floor = self.floor_for(pressure)
            if call_class.criticality > floor:
                self._bump(f"{call_class.name}:criticality")
                return Decision(SHED_CRITICALITY,
                                reason=f"pressure={pressure:.2f}",
                                degrade_to=call_class.fallback)

        if self.cfg.deadline_admission and expected_s > deadline_left:
            self._bump(f"{call_class.name}:deadline")
            return Decision(SHED_DEADLINE,
                            reason=f"need={expected_s:.1f}s left={deadline_left:.1f}s",
                            degrade_to=call_class.fallback)

        if self.tenants is not None and not self.tenants.allow(tenant, call_class.in_tokens[0]):
            self._bump(f"{call_class.name}:tenant")
            return Decision(SHED_TENANT, reason=tenant, degrade_to=call_class.fallback)

        return Decision(ADMIT)

    def _bump(self, key: str) -> None:
        self.shed_counts[key] = self.shed_counts.get(key, 0) + 1

    @property
    def mean_pressure(self) -> float:
        if not self.pressure_samples:
            return 0.0
        return sum(self.pressure_samples) / len(self.pressure_samples)
