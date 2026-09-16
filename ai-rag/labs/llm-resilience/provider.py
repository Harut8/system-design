"""The other side of the wire: a metered, queueing, occasionally-sick LLM API.

Two independent failure surfaces, because real providers have two and the
client must tell them apart (ch33 §9.3 step 3):

  429 rate_limit_error   You asked for more than your allowance. The provider
                         is healthy. It even tells you how long to wait
                         (`retry-after`). Counting this as a dependency failure
                         is how you trip a breaker on a working API.

  529 overloaded_error   The provider's *fleet* is in trouble — every tenant
                         sees it, the wait is unbounded, and hammering it is
                         how a brownout becomes an outage.

  500 api_error          Genuinely transient. Worth exactly one retry.

Metering runs on three buckets per model (RPM / ITPM / OTPM). The OTPM charge
is the request's `max_tokens`, reserved at admission and reconciled against the
real output at completion — which is why a generous `max_tokens` silently costs
you throughput even when the model writes three sentences.

Service time is `base + prefill + decode`, with prefill and decode slowed by a
queueing factor once the server pool saturates (an M/M/c-flavoured stand-in for
ch34 §2 — a shape, not a model of anyone's real infrastructure).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import MODELS, Model
from sim import Event, Sim, TokenBucket

OK = "ok"
RATE_LIMITED = "429"
OVERLOADED = "529"
SERVER_ERROR = "500"
BAD_REQUEST = "400"


@dataclass
class Response:
    status: str
    ttft: float = 0.0  # seconds until first token (streaming) or full body
    duration: float = 0.0  # seconds until last token
    in_tokens: float = 0.0
    out_tokens: float = 0.0
    cached_in_tokens: float = 0.0
    retry_after: float = 0.0
    limit_hit: str = ""  # "rpm" | "itpm" | "otpm"
    headers: Dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == OK


@dataclass
class Request:
    model: str
    in_tokens: float
    out_tokens: float  # ground truth; the client does not know this up front
    max_tokens: int
    cached_in_tokens: float = 0.0
    streaming: bool = False


class ModelEndpoint:
    """One model's meters, queue and server pool."""

    def __init__(self, sim: Sim, model: Model) -> None:
        self.sim = sim
        self.model = model
        self.rpm = TokenBucket(model.rpm, f"{model.key}.rpm")
        self.itpm = TokenBucket(model.itpm, f"{model.key}.itpm")
        self.otpm = TokenBucket(model.otpm, f"{model.key}.otpm")
        self.servers = model.servers
        self.in_flight = 0
        self.queue_depth = 0
        # health knobs the scenarios twist
        self.server_scale = 1.0  # 1.0 healthy; 0.25 = lost 75% of the fleet
        self.error_rate = 0.002
        self.overload_queue_ratio = 3.0  # queue > ratio * servers -> 529
        # counters
        self.stats: Dict[str, int] = {}
        self.tokens_in = 0.0
        self.tokens_out = 0.0
        self.cached_in = 0.0
        self.cost_usd = 0.0

    # ------------------------------------------------------------ accounting

    def _bump(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    @property
    def effective_servers(self) -> float:
        return max(1.0, self.servers * self.server_scale)

    def headers(self) -> Dict[str, float]:
        """The `anthropic-ratelimit-*`-shaped feedback a real response carries."""
        now = self.sim.now
        return {
            "requests-remaining": self.rpm.level(now),
            "input-tokens-remaining": self.itpm.level(now),
            "output-tokens-remaining": self.otpm.level(now),
        }

    # ---------------------------------------------------------------- metering

    def _meter(self, req: Request) -> Optional[Response]:
        """Charge the three buckets. Returns a 429 Response if any is empty.

        Note the asymmetry: input is charged as *actual* input tokens, output is
        charged as `max_tokens`. That asymmetry is the single biggest lever on
        effective throughput for long-`max_tokens` classes.
        """
        now = self.sim.now
        billed_in = req.in_tokens  # cached prefix still meters at full weight here
        checks = (
            ("rpm", self.rpm, 1.0),
            ("itpm", self.itpm, billed_in),
            ("otpm", self.otpm, float(req.max_tokens)),
        )
        for name, bucket, amount in checks:
            wait = bucket.wait_time(now, amount)
            if wait > 0.0:
                self._bump(f"429.{name}")
                return Response(
                    status=RATE_LIMITED,
                    retry_after=min(60.0, wait) if math.isfinite(wait) else 60.0,
                    limit_hit=name,
                    headers=self.headers(),
                )
        self.rpm.try_take(now, 1.0)
        self.itpm.try_take(now, billed_in)
        self.otpm.try_take(now, float(req.max_tokens))
        return None

    # ----------------------------------------------------------------- service

    def _service_time(self, req: Request) -> tuple:
        """(ttft, total). Degrades with queueing, per ch34 §2.2."""
        model = self.model
        rho = min(0.985, max(0.0, (self.in_flight + self.queue_depth) / self.effective_servers))
        # M/M/c, not M/M/1. With a large server pool the delay probability stays
        # near zero until utilisation is high and then turns a corner hard — the
        # `1/(1-rho)` single-server curve would have us queueing at rho=0.4,
        # which is not how a provider sized for your quota behaves (ch34 §2.3).
        stretch = min(30.0, 1.0 + (rho ** 8) / (1.0 - rho))

        jitter = self.sim.rng.lognormvariate(0.0, 0.25)
        prefill = req.in_tokens / model.prefill_tok_s
        decode = req.out_tokens / model.decode_tok_s
        ttft = (model.base_latency_s + prefill) * stretch * jitter
        total = ttft + decode * min(stretch, 4.0) * jitter
        return ttft, total

    def call(self, req: Request, ttft_event: Optional[Event] = None) -> Event:
        """Start a request. Returns a Process whose value is a Response.

        `ttft_event` fires when the first token would reach the client, so the
        caller can enforce a time-to-first-token timeout separately from a total
        timeout — the distinction that makes streaming timeouts workable.
        """
        return self.sim.process(self._call(req, ttft_event))

    def _call(self, req: Request, ttft_event: Optional[Event] = None):
        rng = self.sim.rng

        # 1. metering happens before any work is done: a 429 costs one RTT.
        limited = self._meter(req)
        if limited is not None:
            yield self.sim.timeout(0.02 + rng.random() * 0.03)
            return limited

        # 2. fleet-level overload. Checked against queue depth, not your quota.
        if self.queue_depth > self.overload_queue_ratio * self.effective_servers:
            self._bump("529")
            self._refund(req)
            yield self.sim.timeout(0.03 + rng.random() * 0.05)
            return Response(
                status=OVERLOADED,
                retry_after=5.0 + rng.random() * 5.0,
                headers=self.headers(),
            )

        self.queue_depth += 1
        # 3. queue for a server slot
        wait = 0.0
        while self.in_flight >= self.effective_servers:
            wait += 0.05
            yield self.sim.timeout(0.05)
            if wait > 30.0:
                break
        self.queue_depth -= 1
        self.in_flight += 1

        ttft, total = self._service_time(req)
        yield self.sim.timeout(ttft)
        if ttft_event is not None:
            ttft_event.succeed(True)
        yield self.sim.timeout(max(0.0, total - ttft))
        self.in_flight -= 1

        # 4. genuine transient errors
        if rng.random() < self.error_rate:
            self._bump("500")
            self._reconcile(req, 0.0)
            return Response(status=SERVER_ERROR, ttft=ttft, duration=total,
                            headers=self.headers())

        self._bump("ok")
        self._reconcile(req, req.out_tokens)
        self.tokens_in += req.in_tokens
        self.tokens_out += req.out_tokens
        self.cached_in += req.cached_in_tokens
        self.cost_usd += self.model.cost(req.in_tokens, req.out_tokens, req.cached_in_tokens)
        return Response(
            status=OK,
            ttft=ttft,
            duration=total,
            in_tokens=req.in_tokens,
            out_tokens=req.out_tokens,
            cached_in_tokens=req.cached_in_tokens,
            headers=self.headers(),
        )

    def _refund(self, req: Request) -> None:
        now = self.sim.now
        self.rpm.refund(now, 1.0)
        self.itpm.refund(now, req.in_tokens)
        self.otpm.refund(now, float(req.max_tokens))

    def _reconcile(self, req: Request, actual_out: float) -> None:
        """Give back the difference between reserved `max_tokens` and reality."""
        slack = max(0.0, float(req.max_tokens) - actual_out)
        if slack:
            self.otpm.refund(self.sim.now, slack)


class Provider:
    """All model endpoints for one organisation's API key."""

    def __init__(self, sim: Sim) -> None:
        self.sim = sim
        self.endpoints = {key: ModelEndpoint(sim, model) for key, model in MODELS.items()}

    def call(self, req: Request, ttft_event: Optional[Event] = None) -> Event:
        return self.endpoints[req.model].call(req, ttft_event)

    def set_health(self, model: str, server_scale: float = 1.0, error_rate: float = 0.002) -> None:
        endpoint = self.endpoints[model]
        endpoint.server_scale = server_scale
        endpoint.error_rate = error_rate

    def totals(self) -> Dict[str, float]:
        out: Dict[str, float] = {"ok": 0, "429": 0, "529": 0, "500": 0,
                                 "tokens_in": 0.0, "tokens_out": 0.0, "cost_usd": 0.0}
        for endpoint in self.endpoints.values():
            out["ok"] += endpoint.stats.get("ok", 0)
            out["429"] += sum(v for k, v in endpoint.stats.items() if k.startswith("429"))
            out["529"] += endpoint.stats.get("529", 0)
            out["500"] += endpoint.stats.get("500", 0)
            out["tokens_in"] += endpoint.tokens_in
            out["tokens_out"] += endpoint.tokens_out
            out["cost_usd"] += endpoint.cost_usd
        return out
