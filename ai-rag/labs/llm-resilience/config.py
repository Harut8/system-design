"""The fixture: one chat product, three model tiers, five call classes.

**Every number in this file is invented.** The rate limits are shaped like a
published API tier but are not one; the token counts are shaped like a RAG chat
app but are not measured from one. They exist so the arithmetic in capacity.py
and the simulation in scenarios.py have something concrete to chew on. Replace
them with your own before quoting any output of this lab (README §7).

The two facts that are *not* invented, and that drive most of the surprises:

  1. LLM capacity is metered on three axes at once — requests/min, input
     tokens/min, output tokens/min — and the binding one is usually not RPM.
  2. The output-token meter is charged against `max_tokens`, not against what
     the model actually generates, and reconciled afterwards. Your `max_tokens`
     is therefore a throughput setting, not just a safety valve.

Criticality follows Google's four-level scheme (`34-adaptive-load-control...`
§4.3): CRITICAL_PLUS never sheds, SHEDDABLE sheds first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

# ---------------------------------------------------------------- criticality

CRITICAL_PLUS = 0
CRITICAL = 1
SHEDDABLE_PLUS = 2
SHEDDABLE = 3

CRITICALITY_NAMES = {
    CRITICAL_PLUS: "CRITICAL_PLUS",
    CRITICAL: "CRITICAL",
    SHEDDABLE_PLUS: "SHEDDABLE_PLUS",
    SHEDDABLE: "SHEDDABLE",
}


# --------------------------------------------------------------------- models


@dataclass(frozen=True)
class Model:
    """A model tier and the meters the provider applies to it.

    `prefill_tok_s` and `decode_tok_s` are per-request throughput at low
    concurrency; the provider degrades them under queueing (provider.py).
    """

    key: str
    api_id: str
    rpm: int
    itpm: int
    otpm: int
    prefill_tok_s: float
    decode_tok_s: float
    base_latency_s: float
    price_in_per_mtok: float
    price_out_per_mtok: float
    servers: int  # concurrent slots before queueing starts

    def cost(self, in_tokens: float, out_tokens: float, cached_in: float = 0.0) -> float:
        """USD. Cache reads are billed at ~0.1x input; writes at ~1.25x."""
        fresh = max(0.0, in_tokens - cached_in)
        return (
            fresh * self.price_in_per_mtok
            + cached_in * self.price_in_per_mtok * 0.1
            + out_tokens * self.price_out_per_mtok
        ) / 1_000_000.0


MODELS = {
    "haiku": Model(
        key="haiku",
        api_id="claude-haiku-4-5",
        rpm=4_000,
        itpm=4_000_000,
        otpm=800_000,
        prefill_tok_s=12_000.0,
        decode_tok_s=110.0,
        base_latency_s=0.18,
        price_in_per_mtok=1.00,
        price_out_per_mtok=5.00,
        servers=400,
    ),
    "sonnet": Model(
        key="sonnet",
        api_id="claude-sonnet-5",
        rpm=2_000,
        itpm=8_000_000,
        otpm=1_000_000,
        prefill_tok_s=8_000.0,
        decode_tok_s=65.0,
        base_latency_s=0.32,
        price_in_per_mtok=3.00,
        price_out_per_mtok=15.00,
        servers=200,
    ),
    "opus": Model(
        key="opus",
        api_id="claude-opus-5",
        rpm=400,
        itpm=400_000,
        otpm=320_000,
        prefill_tok_s=5_000.0,
        decode_tok_s=35.0,
        base_latency_s=0.45,
        price_in_per_mtok=5.00,
        price_out_per_mtok=25.00,
        servers=80,
    ),
}


# ---------------------------------------------------------------- call classes


@dataclass(frozen=True)
class Timeouts:
    """Per-operation timeouts. A single global timeout cannot serve all five.

    `ttft` and `inter_token` only apply to streaming calls: a 3-second total
    timeout kills a healthy stream at 60% done, and a 60-second total timeout
    lets a hung classifier hold a slot for a minute. Both are ch33 §9.3's
    "mistake everyone makes".
    """

    connect: float
    ttft: float
    inter_token: float
    total: float


@dataclass(frozen=True)
class CallClass:
    name: str
    model: str
    criticality: int
    per_turn: float  # expected calls per user turn (fan-out)
    in_tokens: Tuple[float, float]  # (mean, stdev)
    out_tokens: Tuple[float, float]
    max_tokens: int  # what the OTPM meter actually charges
    streaming: bool
    deadline_s: float  # end-to-end budget before the result is worthless
    timeouts: Timeouts
    max_attempts: int
    fallback: str
    cacheable_prefix: float = 0.0  # fraction of input that is a stable prefix
    note: str = ""

    @property
    def model_cfg(self) -> Model:
        return MODELS[self.model]


CLASSES = [
    CallClass(
        name="guard",
        model="haiku",
        criticality=CRITICAL,
        per_turn=1.0,
        in_tokens=(420.0, 90.0),
        out_tokens=(12.0, 3.0),
        max_tokens=64,
        streaming=False,
        deadline_s=2.5,
        timeouts=Timeouts(connect=2.0, ttft=2.5, inter_token=0.0, total=4.0),
        max_attempts=2,
        fallback="queue_for_review",
        cacheable_prefix=0.75,
        note="safety/moderation classification on the inbound turn",
    ),
    CallClass(
        name="rewrite",
        model="haiku",
        criticality=SHEDDABLE_PLUS,
        per_turn=0.85,
        in_tokens=(900.0, 220.0),
        out_tokens=(70.0, 20.0),
        max_tokens=256,
        streaming=False,
        deadline_s=2.5,
        timeouts=Timeouts(connect=2.0, ttft=2.0, inter_token=0.0, total=3.0),
        max_attempts=1,
        fallback="raw_query",
        cacheable_prefix=0.55,
        note="query rewrite / decomposition ahead of retrieval",
    ),
    CallClass(
        name="answer",
        model="sonnet",
        criticality=CRITICAL_PLUS,
        per_turn=1.0,
        in_tokens=(6200.0, 1400.0),
        out_tokens=(520.0, 190.0),
        max_tokens=2048,
        streaming=True,
        deadline_s=40.0,
        timeouts=Timeouts(connect=2.0, ttft=8.0, inter_token=2.5, total=45.0),
        max_attempts=2,
        fallback="none",
        cacheable_prefix=0.30,
        note="grounded answer over retrieved context; streamed to the user",
    ),
    CallClass(
        name="think",
        model="opus",
        criticality=CRITICAL,
        per_turn=0.08,
        in_tokens=(6800.0, 1500.0),
        out_tokens=(2400.0, 900.0),
        max_tokens=8192,
        streaming=True,
        deadline_s=180.0,
        timeouts=Timeouts(connect=2.0, ttft=25.0, inter_token=4.0, total=200.0),
        max_attempts=1,
        fallback="downgrade_to_answer",
        cacheable_prefix=0.30,
        note="extended-thinking path for the ~8% of turns routed as hard",
    ),
    CallClass(
        name="title",
        model="haiku",
        criticality=SHEDDABLE,
        per_turn=0.18,
        in_tokens=(1600.0, 400.0),
        out_tokens=(28.0, 8.0),
        max_tokens=64,
        streaming=False,
        deadline_s=300.0,
        timeouts=Timeouts(connect=2.0, ttft=3.0, inter_token=0.0, total=6.0),
        max_attempts=1,
        fallback="defer_to_batch",
        cacheable_prefix=0.0,
        note="conversation title/summary; nobody is waiting on it",
    ),
]

CLASS_BY_NAME = {c.name: c for c in CLASSES}

FANOUT = sum(c.per_turn for c in CLASSES)


# ------------------------------------------------------------------- defaults


@dataclass
class GatewayConfig:
    """Knobs for the defence stack. Scenarios override individual fields."""

    # retries (ch33 §2)
    retry_budget_ratio: float = 0.10  # retries allowed as a fraction of successes
    retry_base_delay: float = 1.0  # seconds; LLM limits recover in seconds
    retry_max_delay: float = 20.0
    jitter: str = "full"  # "none" | "equal" | "full"
    honour_retry_after: bool = True
    retry_streaming_past: float = 0.80  # don't retry a stream >80% delivered

    # circuit breaker (ch33 §3)
    breaker_enabled: bool = True
    breaker_threshold: float = 0.30  # failure ratio over the window
    breaker_window_s: float = 60.0
    breaker_min_calls: int = 20
    breaker_open_s: float = 20.0
    breaker_probes: int = 3
    breaker_counts_429: bool = False  # the load-bearing flag; see Act 4

    # bulkheads (ch33 §4)
    bulkhead_enabled: bool = True
    queue_policy: str = "lifo"  # "fifo" | "lifo"
    codel_target_s: float = 0.05
    codel_interval_s: float = 1.0

    # client-side metering (ch34 §8)
    client_limiter_enabled: bool = True
    limiter_headroom: float = 0.90  # aim below the provider's ceiling
    max_limiter_wait_s: float = 2.0
    instances: int = 12  # fleet size sharing one org-level limit
    coordinated_limiter: bool = True  # shared (Redis-style) vs per-instance split

    # admission control + shedding (ch34 §3, §4)
    admission_enabled: bool = True
    deadline_admission: bool = True

    # adaptive concurrency (ch34 §6)
    adaptive: str = "gradient"  # "fixed" | "aimd" | "gradient"
    adaptive_initial: int = 40

    # degradation ladder (ch34 §10)
    degrade_enabled: bool = True

    # cost model
    cache_hit_rate: float = 0.0  # fraction of turns answered without any call
    cache_read_itpm_weight: float = 1.0  # see capacity.py §"the caching asterisk"
