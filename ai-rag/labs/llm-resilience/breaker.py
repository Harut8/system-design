"""Circuit breaker, with the one modification LLM APIs demand.

The state machine is ordinary (ch33 §3.2): CLOSED -> OPEN on a failure ratio
over a sliding window -> HALF_OPEN after a cooldown -> CLOSED on N successful
probes, back to OPEN on any probe failure, with the cooldown doubling each
consecutive trip so a genuinely dead dependency isn't probed every 20 seconds
forever.

The modification is `counts_429`. A 429 is not a failure of the dependency; it
is the dependency working correctly and telling you to slow down. Feed 429s
into the failure ratio and the breaker opens during *normal* operation of a
system that is simply over its quota — which blocks the requests that had quota
too. Act 4 in the report runs it both ways.

Two further LLM-specific choices baked into the defaults (config.py):

  * a 60s window, not 10s — LLM incidents last minutes, and a short window makes
    the breaker oscillate around a recovering fleet;
  * a 30% threshold, not 5% — the baseline error rate on these APIs is higher
    than on an internal RPC, and a model rollout can spike it briefly.

Probes should be cheap. `probe_is_cheap` records the intent; the gateway routes
half-open probes to a short prompt on the cheapest tier rather than replaying a
6,000-token production request.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Tuple

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        sim,
        name: str,
        threshold: float = 0.30,
        window_s: float = 60.0,
        min_calls: int = 20,
        open_s: float = 20.0,
        probes: int = 3,
        counts_429: bool = False,
        max_open_s: float = 120.0,
    ) -> None:
        self.sim = sim
        self.name = name
        self.threshold = threshold
        self.window_s = window_s
        self.min_calls = min_calls
        self.base_open_s = open_s
        self.max_open_s = max_open_s
        self.probes_required = probes
        self.counts_429 = counts_429

        self.state = CLOSED
        self._window: Deque[Tuple[float, bool]] = deque()
        self._opened_at = 0.0
        self._open_for = open_s
        self._consecutive_trips = 0
        self._probes_in_flight = 0
        self._probes_ok = 0

        self.trips = 0
        self.rejected = 0
        self.probe_is_cheap = True

    # ------------------------------------------------------------------ admit

    def allow(self) -> bool:
        now = self.sim.now
        if self.state == OPEN:
            if now - self._opened_at >= self._open_for:
                self.state = HALF_OPEN
                self._probes_in_flight = 0
                self._probes_ok = 0
            else:
                self.rejected += 1
                return False
        if self.state == HALF_OPEN:
            # Let exactly one probe run at a time. A half-open breaker that
            # admits the full arrival rate re-kills the recovering dependency.
            if self._probes_in_flight >= 1:
                self.rejected += 1
                return False
            self._probes_in_flight += 1
            return True
        return True

    def abandon(self) -> None:
        """Give back a half-open probe slot that never reached the dependency.

        Every `allow()` that returns True while HALF_OPEN consumes the single
        probe slot. If the caller then bails out for an unrelated reason — quota
        exhausted locally, deadline already gone — and never calls `record()`,
        the slot leaks and the breaker stays half-open forever, rejecting
        everything. A breaker that can never close is worse than no breaker.
        """
        if self.state == HALF_OPEN and self._probes_in_flight > 0:
            self._probes_in_flight -= 1

    # ----------------------------------------------------------------- record

    def record(self, status: str) -> None:
        """`status` is a provider status string: 'ok', '429', '529', '500',
        'timeout', '400'."""
        if status == "429" and not self.counts_429:
            return  # backpressure, not failure — the whole point
        if status == "400":
            return  # our bug, not theirs
        failed = status != "ok"
        if self.state == HALF_OPEN:
            self._probes_in_flight = max(0, self._probes_in_flight - 1)
            if failed:
                self._trip()
            else:
                self._probes_ok += 1
                if self._probes_ok >= self.probes_required:
                    self._close()
            return

        self._window.append((self.sim.now, failed))
        self._evict()
        if len(self._window) >= self.min_calls:
            failures = sum(1 for _, f in self._window if f)
            if failures / len(self._window) >= self.threshold:
                self._trip()

    def _evict(self) -> None:
        cutoff = self.sim.now - self.window_s
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def _trip(self) -> None:
        self.state = OPEN
        self._opened_at = self.sim.now
        self._consecutive_trips += 1
        self._open_for = min(
            self.max_open_s, self.base_open_s * (2 ** (self._consecutive_trips - 1))
        )
        self._window.clear()
        self.trips += 1

    def _close(self) -> None:
        self.state = CLOSED
        self._consecutive_trips = 0
        self._open_for = self.base_open_s
        self._window.clear()

    # ------------------------------------------------------------------ view

    def failure_ratio(self) -> float:
        self._evict()
        if not self._window:
            return 0.0
        return sum(1 for _, f in self._window if f) / len(self._window)
