"""What to count when the system is shedding on purpose.

The trap in an overload-defence system is that the obvious dashboard lies.
Availability looks *better* under load shedding, because the requests you never
admitted never had a chance to fail. Error rate goes down as the product gets
worse. So this module is built around the three numbers that don't lie:

  goodput       successful, *useful-to-a-user* completions per second. The only
                number that measures the product. If shedding is working,
                goodput holds flat while offered load climbs; if goodput falls
                while shedding rises, the shedding itself is the problem.
  shed-by-class where the pain landed. CRITICAL_PLUS shedding is an escalation;
                SHEDDABLE shedding is the system working as designed.
  retry-spend   dollars burned on attempts that returned nothing. On an LLM API
                a retry re-processes the entire prompt, so this is a real line
                item, not a rounding error (ch33 §9.3 step 2).

Latency is kept as raw samples and percentiles are computed exactly — the
sample counts here are small enough that an approximate histogram would only
add error for no benefit.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

# terminal outcomes
OK = "ok"
OK_FALLBACK = "ok_fallback"
OK_DEGRADED = "ok_degraded"
SHED = "shed"
BREAKER = "breaker_open"
EXHAUSTED = "exhausted"
TIMEOUT = "timeout"
QUEUE_DROP = "queue_drop"
LOCAL_LIMIT = "local_rate_limit"

USEFUL = (OK, OK_DEGRADED)


class Metrics:
    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self.outcomes: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.latency: Dict[str, List[float]] = defaultdict(list)
        self.ttft: Dict[str, List[float]] = defaultdict(list)
        self.attempts: Dict[str, int] = defaultdict(int)
        self.retries: Dict[str, int] = defaultdict(int)
        self.retry_reasons: Dict[str, int] = defaultdict(int)
        self.no_retry_reasons: Dict[str, int] = defaultdict(int)
        self.offered: Dict[str, int] = defaultdict(int)
        self.cost_useful = 0.0
        self.cost_wasted = 0.0
        self.turns_offered = 0
        self.turns_complete = 0
        self.turns_degraded = 0
        self.turns_failed = 0

    # ---------------------------------------------------------------- record

    def offer(self, cls: str) -> None:
        self.offered[cls] += 1

    def record(self, cls: str, outcome: str, latency: float = 0.0,
               ttft: float = 0.0) -> None:
        self.outcomes[cls][outcome] += 1
        if outcome in USEFUL or outcome == OK_FALLBACK:
            if latency:
                self.latency[cls].append(latency)
            if ttft:
                self.ttft[cls].append(ttft)

    def attempt(self, cls: str, retry: bool = False, reason: str = "") -> None:
        self.attempts[cls] += 1
        if retry:
            self.retries[cls] += 1
            if reason:
                self.retry_reasons[reason] += 1

    def no_retry(self, reason: str) -> None:
        self.no_retry_reasons[reason] += 1

    # ------------------------------------------------------------------ view

    def count(self, cls: str, outcome: str) -> int:
        return self.outcomes[cls].get(outcome, 0)

    def total(self, cls: str) -> int:
        return sum(self.outcomes[cls].values())

    def useful(self, cls: str) -> int:
        return sum(self.outcomes[cls].get(o, 0) for o in USEFUL)

    def goodput(self, cls: str) -> float:
        return self.useful(cls) / self.window_s

    def offered_rps(self, cls: str) -> float:
        return self.offered[cls] / self.window_s

    def served_fraction(self, cls: str) -> float:
        offered = self.offered[cls]
        return self.useful(cls) / offered if offered else 0.0

    def all_goodput(self) -> float:
        return sum(self.useful(c) for c in self.outcomes) / self.window_s

    def all_offered_rps(self) -> float:
        return sum(self.offered.values()) / self.window_s

    def pct(self, cls: str, q: float, which: str = "latency") -> float:
        samples = sorted(self.latency[cls] if which == "latency" else self.ttft[cls])
        if not samples:
            return 0.0
        idx = min(len(samples) - 1, int(q * len(samples)))
        return samples[idx]

    def retry_ratio(self) -> float:
        attempts = sum(self.attempts.values())
        return sum(self.retries.values()) / attempts if attempts else 0.0

    def retry_cost_share(self) -> float:
        total = self.cost_useful + self.cost_wasted
        return self.cost_wasted / total if total else 0.0

    def turn_completion(self) -> float:
        return self.turns_complete / self.turns_offered if self.turns_offered else 0.0
