"""The arithmetic. No simulation — just division, done before you write code.

Almost every question people bring to a resilience review of an LLM gateway is
actually a capacity question wearing a costume. "How many retries should we
configure?" is usually "we are 40x over our quota and hoping." So this module
answers, for the fixture in config.py:

  * which of the three meters binds for each call class, and by how much;
  * how many requests a class can actually sustain, versus the RPM number
    everybody quotes;
  * how much concurrency that implies (Little's Law), which is the number your
    bulkheads and connection pools should be sized from;
  * what the offered load is after fan-out, and how big the gap is;
  * what each architectural lever buys, in user turns per second.

The headline result for this fixture: the "100 RPS API" is a ~1.6 RPS API on the
answer path, because the output-token meter is charged `max_tokens` and the
answer class asks for 2048 while writing ~520.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Optional

from config import CLASSES, MODELS, CallClass


@dataclass
class Binding:
    """Per-class ceilings. Note the two different OTPM numbers.

    The output-token meter is charged `max_tokens` at admission and reconciled
    against real output at completion. That means `max_tokens` does **not** cap
    your sustained rate — reconciliation returns the difference, so sustained
    throughput is governed by *actual* output. What `max_tokens` caps is how
    many requests can be in flight at once, because each one holds its full
    reservation for the duration of the call:

        concurrency_cap = OTPM_bucket_capacity / max_tokens

    Both matter, and which one bites depends on the call. A 9-second answer
    stream has plenty of concurrency headroom; a 70-second thinking call does
    not. `by_otpm_unreconciled` is the pessimistic case for a provider (or a
    client-side limiter) that reserves and never gives the slack back — see
    limits.py, where forgetting to reconcile is a one-line bug.
    """

    cls: CallClass
    by_rpm: float
    by_itpm: float
    by_otpm: float  # sustained, assuming reconciliation
    by_otpm_unreconciled: float  # if nobody hands the slack back
    concurrency_cap: float  # simultaneous in-flight, from max_tokens reservation

    @property
    def limit_rps(self) -> float:
        return min(self.by_rpm, self.by_itpm, self.by_otpm)

    @property
    def binds(self) -> str:
        options = {"RPM": self.by_rpm, "ITPM": self.by_itpm, "OTPM": self.by_otpm}
        return min(options, key=lambda k: options[k])

    @property
    def concurrency_needed(self) -> float:
        return self.limit_rps * service_time(self.cls)

    @property
    def concurrency_bound(self) -> bool:
        """True when `max_tokens` throttles this class below its own quota."""
        return self.concurrency_cap < self.concurrency_needed

    @property
    def effective_rps(self) -> float:
        """Rate actually achievable once the concurrency cap is applied."""
        service = service_time(self.cls)
        return min(self.limit_rps, self.concurrency_cap / service if service else 1e9)

    @property
    def max_tokens_tax(self) -> float:
        """Throughput multiplier from sizing `max_tokens` to observed output."""
        tight = float(int(self.cls.out_tokens[0] + 2 * self.cls.out_tokens[1]))
        model = self.cls.model_cfg
        service = service_time(self.cls)
        better = min(self.limit_rps, (model.otpm / tight) / service if service else 1e9)
        return better / self.effective_rps if self.effective_rps else 0.0


def binding_for(cls: CallClass) -> Binding:
    model = cls.model_cfg
    return Binding(
        cls=cls,
        by_rpm=model.rpm / 60.0,
        by_itpm=(model.itpm / 60.0) / cls.in_tokens[0],
        by_otpm=(model.otpm / 60.0) / cls.out_tokens[0],
        by_otpm_unreconciled=(model.otpm / 60.0) / float(cls.max_tokens),
        concurrency_cap=model.otpm / float(cls.max_tokens),
    )


def all_bindings(classes: Optional[Iterable[CallClass]] = None) -> List[Binding]:
    return [binding_for(c) for c in (classes or CLASSES)]


def service_time(cls: CallClass) -> float:
    model = cls.model_cfg
    return (
        model.base_latency_s
        + cls.in_tokens[0] / model.prefill_tok_s
        + cls.out_tokens[0] / model.decode_tok_s
    )


def littles_law(cls: CallClass, rps: float) -> float:
    """Concurrency = arrival rate x time in system. Size pools from this."""
    return rps * service_time(cls)


def demand(turns_rps: float, classes: Optional[Iterable[CallClass]] = None) -> Dict[str, float]:
    return {c.name: turns_rps * c.per_turn for c in (classes or CLASSES)}


def model_demand(turns_rps: float,
                 classes: Optional[Iterable[CallClass]] = None) -> Dict[str, Dict[str, float]]:
    """Per-model offered RPM / ITPM / OTPM at a given turn rate.

    OTPM is counted on *actual* output because the reservation is reconciled.
    `conc` is the simultaneous in-flight demand, which is what the unreconciled
    `max_tokens` reservation actually constrains.
    """
    out = {k: {"rpm": 0.0, "itpm": 0.0, "otpm": 0.0, "conc": 0.0, "conc_cap": 0.0}
           for k in MODELS}
    for cls in (classes or CLASSES):
        rps = turns_rps * cls.per_turn
        row = out[cls.model]
        row["rpm"] += rps * 60.0
        row["itpm"] += rps * 60.0 * cls.in_tokens[0]
        row["otpm"] += rps * 60.0 * cls.out_tokens[0]
        row["conc"] += rps * service_time(cls) * float(cls.max_tokens)
    for key, model in MODELS.items():
        out[key]["conc_cap"] = float(model.otpm)
    return out


def headroom(turns_rps: float,
             classes: Optional[Iterable[CallClass]] = None) -> Dict[str, Dict[str, float]]:
    """Offered / limit, per model per meter. >1.0 means over quota."""
    offered = model_demand(turns_rps, classes)
    return {
        key: {
            "rpm": offered[key]["rpm"] / model.rpm,
            "itpm": offered[key]["itpm"] / model.itpm,
            "otpm": offered[key]["otpm"] / model.otpm,
            "conc": offered[key]["conc"] / model.otpm,
        }
        for key, model in MODELS.items()
    }


def worst_meter(turns_rps: float, classes: Optional[Iterable[CallClass]] = None) -> float:
    return max(max(row.values()) for row in headroom(turns_rps, classes).values())


def sustainable_turns_rps(classes: Optional[Iterable[CallClass]] = None) -> float:
    """Largest user-turn rate at which no meter on any model exceeds 1.0."""
    classes = list(classes or CLASSES)
    lo, hi = 0.0, 100_000.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if worst_meter(mid, classes) > 1.0:
            hi = mid
        else:
            lo = mid
    return lo


def tier_needed(turns_rps: float, classes: Optional[Iterable[CallClass]] = None) -> float:
    """Multiple of the current quota required to serve `turns_rps` outright."""
    return worst_meter(turns_rps, classes)


# ------------------------------------------------------------------- levers


@dataclass
class Lever:
    name: str
    detail: str
    turns_rps: float
    multiplier: float
    cost: str


def _edit(classes: List[CallClass], name: str, **kwargs) -> List[CallClass]:
    return [replace(c, **kwargs) if c.name == name else c for c in classes]


def _drop(classes: List[CallClass], name: str) -> List[CallClass]:
    return [c for c in classes if c.name != name]


def levers() -> List[Lever]:
    """What each change buys, in sustainable user turns per second.

    Every entry is a change to the *workload*, not to the resilience stack.
    That is the point: the stack decides how gracefully you behave at the edge
    of capacity. Only these move the edge.
    """
    base_classes = list(CLASSES)
    base = sustainable_turns_rps(base_classes)
    out = [Lever("baseline", "config.py as written", base, 1.0, "—")]

    def add(name: str, detail: str, classes: List[CallClass], cost: str) -> None:
        value = sustainable_turns_rps(classes)
        out.append(Lever(name, detail, value, value / base if base else 0.0, cost))

    answer = next(c for c in base_classes if c.name == "answer")
    think = next(c for c in base_classes if c.name == "think")
    tight_answer = int(answer.out_tokens[0] + 2 * answer.out_tokens[1])
    tight_think = int(think.out_tokens[0] + 2 * think.out_tokens[1])

    tight = _edit(_edit(base_classes, "answer", max_tokens=tight_answer),
                  "think", max_tokens=tight_think)
    add("max_tokens -> p97 of observed",
        f"answer {answer.max_tokens}->{tight_answer}, think {think.max_tokens}->{tight_think}",
        tight,
        "free; truncation risk if the real tail is fatter than you measured")

    add("title -> Batch API",
        "move the SHEDDABLE summariser off the interactive quota entirely",
        _drop(base_classes, "title"),
        "50% cheaper per token; titles land minutes late")

    add("drop the rewrite hop",
        "retrieve on the raw query plus BM25 instead of an LLM rewrite",
        _drop(base_classes, "rewrite"),
        "recall loss on multi-hop questions; needs a golden set to price")

    add("halve the RAG context",
        "answer input 6200 -> 3100 tokens by reranking to top-k=5",
        _edit(base_classes, "answer",
              in_tokens=(answer.in_tokens[0] / 2, answer.in_tokens[1] / 2)),
        "recall@k drops; measure, do not assume")

    add("route 'answer' to Haiku",
        "downgrade the default answer tier, escalate only on low confidence",
        _edit(base_classes, "answer", model="haiku"),
        "quality regression on hard turns")

    combined = _drop(base_classes, "title")
    combined = _edit(combined, "answer", max_tokens=tight_answer,
                     in_tokens=(answer.in_tokens[0] / 2, answer.in_tokens[1] / 2))
    combined = _edit(combined, "think", max_tokens=tight_think)
    add("all three cheap levers",
        "tight max_tokens + batched titles + halved context",
        combined,
        "the realistic engineering answer, before buying quota")

    return out


def response_cache_effect(turns_rps: float, hit_rate: float) -> float:
    """A whole-response cache removes turns before any call is made."""
    return turns_rps * (1.0 - hit_rate)
