"""A ~200-line discrete-event kernel. Zero dependencies, fully deterministic.

Everything in this lab runs on simulated time. No sleeps, no threads, no clock
reads — `Sim.now` is the only clock, and two runs with the same seed produce
byte-identical output. That is the whole reason the numbers in the report are
worth printing: they are *reproducible consequences of a stated model*, not
measurements of anything real (README §7).

The API is a small subset of SimPy's, reimplemented here so the lab has no
install step:

    sim = Sim(seed=1)

    def worker(sim, sem):
        req = sem.request()
        yield req                      # wait for a slot
        yield sim.timeout(0.25)        # do work for 250ms
        sem.release()
        return "done"

    p = sim.process(worker(sim, sem))  # start a coroutine
    sim.run(until=60.0)                # advance the clock
    p.value                            # -> "done"

Three primitives:

  Event     something that will happen. `yield` it to wait for it.
  Process   a generator driven by the kernel; is itself an Event, so one
            process can `yield` another and read its return value.
  Resource  a counting semaphore with a FIFO waiter queue and a *mutable*
            capacity — adaptive concurrency limits (adaptive.py) resize it
            while requests are in flight.

Plus `TokenBucket`, which is here rather than in limits.py because both sides
of the wire need it: the provider meters you with one (provider.py) and the
client meters itself with three (limits.py).
"""

from __future__ import annotations

import heapq
import itertools
import random
from typing import Any, Callable, Generator, Iterable, List, Optional


class Event:
    """Something that fires once, at a known simulated time, with a value."""

    __slots__ = ("sim", "_callbacks", "triggered", "value")

    def __init__(self, sim: "Sim") -> None:
        self.sim = sim
        self._callbacks: List[Callable[["Event"], None]] = []
        self.triggered = False
        self.value: Any = None

    def add_callback(self, fn: Callable[["Event"], None]) -> None:
        if self.triggered:
            # Already fired: re-dispatch through the kernel so callback order
            # stays a function of scheduling order, never of call order.
            self.sim.at(0.0, lambda: fn(self))
        else:
            self._callbacks.append(fn)

    def succeed(self, value: Any = None) -> "Event":
        if not self.triggered:
            self.sim.at(0.0, lambda: self._fire(value))
        return self

    def _fire(self, value: Any) -> None:
        if self.triggered:
            return
        self.triggered = True
        self.value = value
        callbacks, self._callbacks = self._callbacks, []
        for fn in callbacks:
            fn(self)


class Process(Event):
    """A generator the kernel drives. Yields Events; returns a value."""

    __slots__ = ("_gen",)

    def __init__(self, sim: "Sim", gen: Generator[Event, Any, Any]) -> None:
        super().__init__(sim)
        self._gen = gen
        sim.at(0.0, lambda: self._resume(None))

    def _resume(self, value: Any) -> None:
        try:
            event = self._gen.send(value)
        except StopIteration as stop:
            self._fire(getattr(stop, "value", None))
            return
        event.add_callback(lambda e: self._resume(e.value))


class Sim:
    """The clock and the event heap."""

    def __init__(self, seed: int = 0) -> None:
        self.now = 0.0
        self._heap: List[tuple] = []
        self._seq = itertools.count()
        self.rng = random.Random(seed)

    def at(self, delay: float, fn: Callable[[], None]) -> None:
        heapq.heappush(self._heap, (self.now + delay, next(self._seq), fn))

    def timeout(self, delay: float, value: Any = None) -> Event:
        event = Event(self)
        self.at(delay, lambda: event._fire(value))
        return event

    def event(self) -> Event:
        return Event(self)

    def process(self, gen: Generator[Event, Any, Any]) -> Process:
        return Process(self, gen)

    def any_of(self, events: Iterable[Event]) -> Event:
        """Fires with the *winning event* as its value. Ties break by seq."""
        out = Event(self)
        winner: List[Optional[Event]] = [None]

        def done(ev: Event) -> None:
            if winner[0] is None:
                winner[0] = ev
                out.succeed(ev)

        for event in events:
            event.add_callback(done)
        return out

    def run(self, until: float) -> None:
        heap = self._heap
        while heap and heap[0][0] <= until:
            when, _, fn = heapq.heappop(heap)
            self.now = when
            fn()
        self.now = max(self.now, until)


class Resource:
    """Counting semaphore with FIFO waiters and resizable capacity.

    `set_capacity` is what makes adaptive concurrency limits (ch34 §6) work:
    the limiter raises and lowers the ceiling underneath in-flight requests,
    and shrinking never preempts — it just stops granting until drained.
    """

    def __init__(self, sim: Sim, capacity: int) -> None:
        self.sim = sim
        self._capacity = int(capacity)
        self.in_use = 0
        self.waiters: List[Event] = []

    @property
    def capacity(self) -> int:
        return self._capacity

    def set_capacity(self, capacity: int) -> None:
        self._capacity = max(1, int(capacity))
        self._grant()

    def request(self) -> Event:
        event = Event(self.sim)
        self.waiters.append(event)
        self._grant()
        return event

    def cancel(self, event: Event) -> bool:
        """Abandon a pending request (queue timeout). True if it was pending."""
        if event in self.waiters:
            self.waiters.remove(event)
            return True
        return False

    def release(self) -> None:
        self.in_use -= 1
        self._grant()

    def _grant(self) -> None:
        while self.waiters and self.in_use < self._capacity:
            self.in_use += 1
            self.waiters.pop(0).succeed(True)


class TokenBucket:
    """Continuously-refilled bucket sized in units-per-minute.

    Capacity equals the per-minute limit and refill is `limit/60` per second,
    which is the shape LLM APIs document for RPM/ITPM/OTPM meters: you can
    spend a full minute's allowance instantly, then you are metered.
    """

    def __init__(self, limit_per_minute: float, name: str = "") -> None:
        self.name = name
        self.limit = float(limit_per_minute)
        self.rate = self.limit / 60.0
        self.tokens = self.limit
        self._updated = 0.0

    def _refill(self, now: float) -> None:
        if now > self._updated:
            self.tokens = min(self.limit, self.tokens + (now - self._updated) * self.rate)
            self._updated = now

    def level(self, now: float) -> float:
        self._refill(now)
        return self.tokens

    def try_take(self, now: float, amount: float) -> bool:
        self._refill(now)
        if amount > self.limit:
            return False  # single request exceeds the whole limit; never satisfiable
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def wait_time(self, now: float, amount: float) -> float:
        """Seconds until `amount` is available. `inf` if never satisfiable."""
        self._refill(now)
        if amount > self.limit:
            return float("inf")
        if self.tokens >= amount:
            return 0.0
        return (amount - self.tokens) / self.rate

    def refund(self, now: float, amount: float) -> None:
        self._refill(now)
        self.tokens = min(self.limit, self.tokens + amount)

    def utilization(self, now: float) -> float:
        """0.0 = full bucket (idle), 1.0 = empty bucket (saturated)."""
        return 1.0 - (self.level(now) / self.limit if self.limit else 0.0)
