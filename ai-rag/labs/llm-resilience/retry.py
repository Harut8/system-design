"""Retry classification, backoff, and the budget that stops a retry storm.

Three ideas, in order of how much damage their absence does:

1. **A budget, not a count.** `max_attempts=3` on every call site multiplies
   your load by 3 exactly when the dependency is least able to take it — ch33
   §2.1's retry amplification. A budget caps *aggregate* retries as a fraction
   of *successes*: when things are healthy there are plenty of successes and
   retries are free; when everything is failing the budget empties and retries
   stop, which is precisely when you want them to. 10% is the usual setting.

2. **Classification.** 429 always retries (it *will* work if you wait) but
   respects `retry-after`. 500 retries once. 529 retries with a long base delay.
   400/401 never retry. And a streamed response that died at 85% is not retried
   at all — you already paid for 6,000 input tokens and you would pay again for
   the last 15% of the output.

3. **Full jitter.** `sleep(base * 2**n)` synchronises every client that failed
   in the same second into a thundering herd on the same future second. AWS's
   full-jitter form — `uniform(0, min(cap, base * 2**n))` — is one line and
   removes the correlation entirely (ch33 §2.4).

`cost_of_retry` is here because on an LLM API a retry is not free compute the
way a retried REST call is: it re-prefills the whole prompt. Retry spend as a
share of total spend is a first-class metric, not an afterthought.
"""

from __future__ import annotations

from dataclasses import dataclass

# classification outcomes
RETRY = "retry"
RETRY_AFTER = "retry_after"
NO_RETRY = "no_retry"

_MAX_ATTEMPTS_BY_STATUS = {
    "429": 3,   # will succeed if you wait; bounded by deadline and budget
    "529": 2,   # provider-wide; long backoff, few attempts
    "500": 2,   # one genuine retry
    "timeout": 2,
    "400": 1,
    "401": 1,
}


def classify(status: str) -> str:
    if status in ("400", "401", "403", "404"):
        return NO_RETRY
    if status in ("429", "529"):
        return RETRY_AFTER
    if status in ("500", "502", "503", "timeout", "connect"):
        return RETRY
    return NO_RETRY


def attempts_allowed(status: str, class_max: int) -> int:
    return min(class_max, _MAX_ATTEMPTS_BY_STATUS.get(status, 1))


def backoff(
    rng,
    attempt: int,
    base: float,
    cap: float,
    retry_after: float = 0.0,
    jitter: str = "full",
) -> float:
    """Seconds to wait before attempt `attempt` (1-indexed retries).

    `retry_after` is a floor, not a replacement: the provider tells you the
    minimum, exponential backoff decides how much more to add when you keep
    hitting it.
    """
    window = min(cap, base * (2 ** max(0, attempt - 1)))
    if jitter == "none":
        delay = window
    elif jitter == "equal":
        delay = window / 2.0 + rng.random() * window / 2.0
    else:  # full
        delay = rng.random() * window
    return max(retry_after, delay)


class RetryBudget:
    """Token bucket over successes. Empty budget == no retries, by design."""

    def __init__(self, ratio: float = 0.10, min_per_second: float = 1.0,
                 window_s: float = 10.0) -> None:
        self.ratio = ratio
        self.min_per_second = min_per_second
        self.window_s = window_s
        self.tokens = 10.0
        self.capacity = 200.0
        self._updated = 0.0
        self.granted = 0
        self.denied = 0

    def on_success(self) -> None:
        self.tokens = min(self.capacity, self.tokens + self.ratio)

    def tick(self, now: float) -> None:
        """A small floor so a fully-dead dependency still gets probed."""
        if now > self._updated:
            self.tokens = min(
                self.capacity, self.tokens + (now - self._updated) * self.min_per_second * self.ratio
            )
            self._updated = now

    def take(self, now: float) -> bool:
        self.tick(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            self.granted += 1
            return True
        self.denied += 1
        return False

    @property
    def usage(self) -> float:
        total = self.granted + self.denied
        return self.granted / total if total else 0.0


@dataclass
class RetryDecision:
    retry: bool
    delay: float = 0.0
    reason: str = ""


def decide(
    *,
    rng,
    status: str,
    attempt: int,
    class_max_attempts: int,
    retry_after: float,
    time_left: float,
    expected_call_s: float,
    budget: RetryBudget,
    now: float,
    cfg,
    stream_fraction: float = 0.0,
) -> RetryDecision:
    """The whole retry policy in one place, in the order the checks matter."""
    kind = classify(status)
    if kind == NO_RETRY:
        return RetryDecision(False, reason="not-retryable")

    if stream_fraction >= cfg.retry_streaming_past:
        # ch33 §9.3: you already have most of the answer. Use it.
        return RetryDecision(False, reason="stream-mostly-delivered")

    if attempt >= attempts_allowed(status, class_max_attempts):
        return RetryDecision(False, reason="attempts-exhausted")

    delay = backoff(
        rng,
        attempt,
        base=cfg.retry_base_delay if status != "529" else cfg.retry_base_delay * 5,
        cap=cfg.retry_max_delay,
        retry_after=retry_after if cfg.honour_retry_after else 0.0,
        jitter=cfg.jitter,
    )

    # A retry that cannot finish before the deadline is pure waste: it costs
    # the provider a full prefill and returns an answer nobody will read.
    if delay + expected_call_s > time_left:
        return RetryDecision(False, reason="deadline")

    if not budget.take(now):
        return RetryDecision(False, reason="budget")

    return RetryDecision(True, delay=delay, reason=f"retry-{status}")


def cost_of_retry(call_class, model) -> float:
    """USD burned by one failed attempt: the input tokens are re-processed."""
    return model.cost(call_class.in_tokens[0], 0.0)
