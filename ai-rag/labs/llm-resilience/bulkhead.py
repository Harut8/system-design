"""Per-class bulkheads: bounded concurrency plus a queue that drops honestly.

Why per class and not one shared pool (ch33 §4): `think` calls sit on an Opus
stream for 60+ seconds. Share one connection pool between `think` and `guard`
and a single slow Opus incident starves the safety classifier that every turn
depends on. Isolation is the point — each class gets its own slots, and one
class's bad day stays inside its own compartment.

The queue in front of each bulkhead is where most of the interesting behaviour
lives:

  * **Bounded.** An unbounded queue converts an overload into a latency
    catastrophe: requests sit for 40 seconds and are then served to a user who
    left (ch34 §7, bufferbloat). A bounded queue converts it into a fast, honest
    rejection.
  * **LIFO under load.** With FIFO, the request at the head is the one that has
    already waited longest and is therefore most likely to be dead on arrival.
    LIFO serves the freshest request and drops the stalest — worse fairness,
    dramatically better goodput during a burst (ch34 §7.3).
  * **CoDel.** Drop based on *sojourn time*, not depth. If the minimum time
    anything spent queued stays above `target` for a whole `interval`, the queue
    is standing rather than bursting, and we shed. A short burst passes through
    untouched; a persistent backlog gets trimmed (ch34 §7.2).
  * **Deadline-aware.** Whatever the policy says, a request whose deadline has
    already passed while queued is dropped before it consumes a slot.
"""

from __future__ import annotations

from typing import List, Optional

from sim import Event, Sim


class QueuedRequest:
    __slots__ = ("event", "enqueued_at", "deadline_at", "criticality")

    def __init__(self, event: Event, enqueued_at: float, deadline_at: float,
                 criticality: int) -> None:
        self.event = event
        self.enqueued_at = enqueued_at
        self.deadline_at = deadline_at
        self.criticality = criticality


class Bulkhead:
    """Semaphore + policy queue. `acquire()` yields True (granted) or a reason."""

    def __init__(
        self,
        sim: Sim,
        name: str,
        capacity: int,
        max_queue: int,
        policy: str = "lifo",
        codel_target_s: float = 0.05,
        codel_interval_s: float = 1.0,
    ) -> None:
        self.sim = sim
        self.name = name
        self.capacity = capacity
        self.max_queue = max_queue
        self.policy = policy
        self.in_use = 0
        self.queue: List[QueuedRequest] = []

        # CoDel state
        self.target = codel_target_s
        self.interval = codel_interval_s
        self._first_above_time = 0.0
        self._dropping = False
        self._drop_next = 0.0
        self._drop_count = 0

        # counters
        self.granted = 0
        self.rejected_full = 0
        self.rejected_codel = 0
        self.rejected_deadline = 0
        self.max_depth = 0
        self.wait_total = 0.0

    # ------------------------------------------------------------------ admit

    def acquire(self, deadline_at: float, criticality: int) -> Event:
        """Returns an Event whose value is True or a rejection reason string."""
        out = Event(self.sim)
        now = self.sim.now

        if self.in_use < self.capacity and not self.queue:
            self.in_use += 1
            self.granted += 1
            out.succeed(True)
            return out

        if len(self.queue) >= self.max_queue:
            self.rejected_full += 1
            out.succeed("queue-full")
            return out

        self.queue.append(QueuedRequest(out, now, deadline_at, criticality))
        self.max_depth = max(self.max_depth, len(self.queue))
        return out

    def release(self) -> None:
        self.in_use -= 1
        self._dispatch()

    # --------------------------------------------------------------- dispatch

    def _pick(self) -> Optional[QueuedRequest]:
        if not self.queue:
            return None
        if self.policy == "fifo":
            return self.queue.pop(0)
        return self.queue.pop()  # LIFO: newest, least likely to be stale

    def _dispatch(self) -> None:
        now = self.sim.now
        while self.in_use < self.capacity and self.queue:
            # 1. deadline sweep: anything already dead never gets a slot
            alive = []
            for item in self.queue:
                if item.deadline_at <= now:
                    self.rejected_deadline += 1
                    item.event.succeed("deadline-expired-in-queue")
                else:
                    alive.append(item)
            self.queue = alive
            if not self.queue:
                return

            item = self._pick()
            if item is None:
                return
            sojourn = now - item.enqueued_at

            # 2. CoDel: is the queue standing or bursting?
            if self._codel_should_drop(now, sojourn):
                self.rejected_codel += 1
                item.event.succeed("codel-drop")
                continue

            self.in_use += 1
            self.granted += 1
            self.wait_total += sojourn
            item.event.succeed(True)

    def _codel_should_drop(self, now: float, sojourn: float) -> bool:
        if sojourn < self.target:
            self._first_above_time = 0.0
            self._dropping = False
            return False

        if self._first_above_time == 0.0:
            self._first_above_time = now + self.interval
            return False

        if not self._dropping and now >= self._first_above_time:
            self._dropping = True
            self._drop_count = 1
            self._drop_next = now + self.interval
            return True

        if self._dropping and now >= self._drop_next:
            self._drop_count += 1
            # drop more aggressively the longer the standing queue persists
            self._drop_next = now + self.interval / (self._drop_count ** 0.5)
            return True

        return False

    @property
    def avg_wait(self) -> float:
        return self.wait_total / self.granted if self.granted else 0.0
