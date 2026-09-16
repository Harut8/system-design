"""Client-side metering: know your own quota before you spend it.

Relying on the provider's 429 to tell you you're over quota is the default, and
it's bad on four counts (ch33 §9.3 step 4): every 429 costs a round trip, it
pollutes your error-rate metrics, it feeds your circuit breaker garbage, and it
consumes a slot in the provider's request pipeline that a well-behaved tenant
could have used. Metering yourself turns a 300ms network rejection into a 0ms
local one.

Three buckets per model, mirroring the provider's, run at `headroom` (default
0.9) of the real limits so that ordinary jitter doesn't push you over.

**The reservation problem.** You must charge the output-token bucket *before*
the call, but you do not know how many output tokens the model will produce
until it's done. So: reserve `max_tokens`, then `reconcile()` the difference on
completion. This is the same thing the provider does, and it means the client's
view of its own headroom is accurate only if `max_tokens` is close to reality.
`observed_p95` tracks actual output per class so the gateway can report how much
headroom a tighter `max_tokens` would buy (capacity.py acts on this).

**The fleet problem.** The quota is org-level; your service is N pods. Two
options, both implemented here:

  coordinated=False  each pod meters at limit/N. Simple, no dependency, and
                     wrong whenever load is uneven — pod 7 throttles itself at
                     1/12th of the quota while pods 1-6 sit idle.
  coordinated=True   one shared bucket (a Redis INCR, ~1-2ms — irrelevant next
                     to an 800ms LLM call). Accurate. Needs a fallback to the
                     split-limit mode when the coordinator is unreachable, which
                     `degrade_to_local()` models.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from config import MODELS
from sim import Sim, TokenBucket


class Reservation:
    __slots__ = ("model", "in_tokens", "max_tokens", "at")

    def __init__(self, model: str, in_tokens: float, max_tokens: int, at: float) -> None:
        self.model = model
        self.in_tokens = in_tokens
        self.max_tokens = max_tokens
        self.at = at


class ClientRateLimiter:
    def __init__(
        self,
        sim: Sim,
        headroom: float = 0.90,
        instances: int = 1,
        coordinated: bool = True,
        cache_read_itpm_weight: float = 1.0,
    ) -> None:
        self.sim = sim
        self.headroom = headroom
        self.instances = max(1, instances)
        self.coordinated = coordinated
        self.cache_read_itpm_weight = cache_read_itpm_weight
        self.share = 1.0 if coordinated else 1.0 / self.instances

        self.buckets: Dict[str, Tuple[TokenBucket, TokenBucket, TokenBucket]] = {}
        for key, model in MODELS.items():
            scale = headroom * self.share
            self.buckets[key] = (
                TokenBucket(model.rpm * scale, f"{key}.rpm"),
                TokenBucket(model.itpm * scale, f"{key}.itpm"),
                TokenBucket(model.otpm * scale, f"{key}.otpm"),
            )

        self.observed_out: Dict[str, list] = {}
        self.denied: Dict[str, int] = {}
        self.reconciled_tokens = 0.0
        self.coordinator_up = True

    # ------------------------------------------------------------------ admit

    def wait_time(self, model: str, in_tokens: float, max_tokens: int,
                  cached_in: float = 0.0) -> float:
        now = self.sim.now
        rpm, itpm, otpm = self.buckets[model]
        billed_in = (in_tokens - cached_in) + cached_in * self.cache_read_itpm_weight
        return max(
            rpm.wait_time(now, 1.0),
            itpm.wait_time(now, billed_in),
            otpm.wait_time(now, float(max_tokens)),
        )

    def reserve(self, model: str, in_tokens: float, max_tokens: int,
                cached_in: float = 0.0) -> Optional[Reservation]:
        now = self.sim.now
        rpm, itpm, otpm = self.buckets[model]
        billed_in = (in_tokens - cached_in) + cached_in * self.cache_read_itpm_weight
        if (
            rpm.wait_time(now, 1.0) > 0
            or itpm.wait_time(now, billed_in) > 0
            or otpm.wait_time(now, float(max_tokens)) > 0
        ):
            self.denied[model] = self.denied.get(model, 0) + 1
            return None
        rpm.try_take(now, 1.0)
        itpm.try_take(now, billed_in)
        otpm.try_take(now, float(max_tokens))
        return Reservation(model, billed_in, max_tokens, now)

    def reconcile(self, res: Reservation, actual_out: float) -> None:
        """Hand back the output tokens we reserved but did not use."""
        _, _, otpm = self.buckets[res.model]
        slack = max(0.0, float(res.max_tokens) - actual_out)
        if slack:
            otpm.refund(self.sim.now, slack)
            self.reconciled_tokens += slack
        self.observed_out.setdefault(res.model, []).append(actual_out)

    def refund_all(self, res: Reservation) -> None:
        """The call never reached the provider (breaker open, local timeout)."""
        rpm, itpm, otpm = self.buckets[res.model]
        now = self.sim.now
        rpm.refund(now, 1.0)
        itpm.refund(now, res.in_tokens)
        otpm.refund(now, float(res.max_tokens))

    # ------------------------------------------------------------------- view

    def utilization(self, model: str) -> Dict[str, float]:
        now = self.sim.now
        rpm, itpm, otpm = self.buckets[model]
        return {
            "rpm": rpm.utilization(now),
            "itpm": itpm.utilization(now),
            "otpm": otpm.utilization(now),
        }

    def pressure(self, model: str) -> float:
        """Worst-case utilisation across the three meters, 0..1."""
        return max(self.utilization(model).values())

    def degrade_to_local(self) -> None:
        """Coordinator unreachable: fall back to limit/N per instance."""
        self.coordinator_up = False
        self.share = 1.0 / self.instances
        for key, model in MODELS.items():
            scale = self.headroom * self.share
            self.buckets[key] = (
                TokenBucket(model.rpm * scale, f"{key}.rpm"),
                TokenBucket(model.itpm * scale, f"{key}.itpm"),
                TokenBucket(model.otpm * scale, f"{key}.otpm"),
            )

    def p95_observed_out(self, model: str) -> float:
        samples = sorted(self.observed_out.get(model, []))
        if not samples:
            return 0.0
        return samples[min(len(samples) - 1, int(0.95 * len(samples)))]
