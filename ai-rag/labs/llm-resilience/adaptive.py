"""Adaptive concurrency limits: AIMD and Netflix's gradient (ch34 §6).

A fixed concurrency limit is a guess about someone else's capacity, and it is
wrong twice: too low when the provider is healthy (you leave throughput on the
floor) and too high when it isn't (you push a struggling fleet further under).
On a shared, multi-tenant LLM API the real capacity available to *you* moves
minute to minute as other tenants come and go, so the guess is wrong more often
than it's right.

Both controllers here infer capacity from latency, which is the only signal the
provider gives you for free on every single response.

AIMD (Jacobson's TCP rule, applied to concurrency):
    success:  limit += 1/limit      (additive, slow)
    failure:  limit *= 0.5          (multiplicative, immediate)
  Conservative and stable; the sawtooth means it spends most of its time
  somewhat below the true ceiling.

Gradient (Netflix concurrency-limits):
    gradient  = rtt_noload / rtt_actual        (<1 means queueing somewhere)
    new_limit = limit * gradient + sqrt(limit)  (the sqrt term is headroom)
    limit     = 0.8 * limit + 0.2 * new_limit   (EWMA; the raw signal is noisy)
  Tracks a moving ceiling far better than AIMD, at the cost of needing a decent
  `rtt_noload` estimate — here a rolling minimum, which is what Netflix uses.

A wrinkle specific to LLM calls: the "latency" of a request is dominated by how
many tokens it decoded, not by how loaded the provider is. Feeding raw duration
into a gradient controller makes it shrink the limit whenever users happen to
ask for longer answers. Both controllers below are therefore fed **time to
first token** for streaming classes, which is the part that actually reflects
queueing. That choice is the difference between a controller that works and one
that oscillates.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Deque


class FixedLimiter:
    """The baseline: a number someone typed into a config file in 2023."""

    kind = "fixed"

    def __init__(self, limit: int = 40) -> None:
        self.limit = float(limit)
        self.min_limit = float(limit)
        self.max_limit = float(limit)

    def on_success(self, rtt: float, in_flight: int) -> None:
        pass

    def on_failure(self, status: str, in_flight: int) -> None:
        pass

    @property
    def value(self) -> int:
        return int(self.limit)


class AIMDLimiter:
    kind = "aimd"

    def __init__(self, initial: int = 40, min_limit: int = 4, max_limit: int = 600,
                 backoff: float = 0.5) -> None:
        self.limit = float(initial)
        self.min_limit = float(min_limit)
        self.max_limit = float(max_limit)
        self.backoff = backoff

    def on_success(self, rtt: float, in_flight: int) -> None:
        # Only grow when we are actually using the limit we have; otherwise the
        # limit drifts up during idle periods and overshoots on the next burst.
        if in_flight >= self.limit * 0.8:
            self.limit = min(self.max_limit, self.limit + 1.0 / self.limit)

    def on_failure(self, status: str, in_flight: int) -> None:
        if status == "429":
            # Quota exhaustion is not congestion. Back off, but gently — the
            # client-side limiter (limits.py) is the right tool for quota.
            self.limit = max(self.min_limit, self.limit * 0.9)
            return
        self.limit = max(self.min_limit, self.limit * self.backoff)

    @property
    def value(self) -> int:
        return int(self.limit)


class GradientLimiter:
    kind = "gradient"

    def __init__(self, initial: int = 40, min_limit: int = 4, max_limit: int = 2000,
                 smoothing: float = 0.2, window: int = 24) -> None:
        self.limit = float(initial)
        self.min_limit = float(min_limit)
        self.max_limit = float(max_limit)
        self.smoothing = smoothing
        self.window = window
        self._window: list = []
        self.rtt_noload = float("inf")
        self.last_gradient = 1.0
        self.updates = 0

    def on_success(self, rtt: float, in_flight: int) -> None:
        if rtt <= 0:
            return
        self._window.append(rtt)
        if len(self._window) < self.window:
            return

        # Compare the *minimum* of this window against the long-run minimum —
        # not the mean. A mean-vs-min gradient is below 1.0 even on a perfectly
        # idle dependency, because latency is noisy, so the limit decays to the
        # floor and throttles you for no reason. Min-vs-min is ~1.0 when idle
        # and drops only when queueing actually raises the floor.
        sample = min(self._window)
        self._window.clear()

        if sample < self.rtt_noload or math.isinf(self.rtt_noload):
            self.rtt_noload = sample
        else:
            # Slow upward decay: a permanently slower provider eventually
            # becomes the new normal instead of pinning the limit at the floor.
            self.rtt_noload += (sample - self.rtt_noload) * 0.05

        gradient = max(0.5, min(1.0, self.rtt_noload / sample))
        self.last_gradient = gradient
        self.updates += 1

        # Never shrink a limit we are not actually using: an idle pool tells you
        # nothing about the dependency's ceiling.
        if gradient < 1.0 and in_flight < self.limit * 0.5:
            return

        target = self.limit * gradient + math.sqrt(self.limit)
        self.limit = max(
            self.min_limit,
            min(self.max_limit, (1 - self.smoothing) * self.limit + self.smoothing * target),
        )

    def on_failure(self, status: str, in_flight: int) -> None:
        if status == "429":
            self.limit = max(self.min_limit, self.limit * 0.9)
            return
        self.limit = max(self.min_limit, self.limit * 0.5)

    @property
    def value(self) -> int:
        return int(self.limit)


def make_limiter(kind: str, initial: int) -> object:
    if kind == "aimd":
        return AIMDLimiter(initial=initial)
    if kind == "gradient":
        return GradientLimiter(initial=initial)
    return FixedLimiter(limit=initial)
