# 29 — Async Patterns and Pitfalls

> **Provenance.** Everything measured here ran in this session on an **Apple M3 Pro**
> (5P + 6E, 11 logical CPUs), macOS 25.5.0, **CPython 3.14.6**, with **trio 0.33.0**,
> **anyio 4.14.2**, and **uvloop 0.22.1** installed into a throwaway venv. The machine was
> **not quiet** — `load1` ran 2.2–2.9 throughout. One measurement in §10.2 came back
> *negative* (an impossible result) and is reported as below the noise floor rather than
> as a finding. CPython source quotations are from the local 3.14.6 install via
> `inspect.getsource`, and from the `3.14` branch of `python/cpython`.
>
> [`28-asyncio-internals.md`](28-asyncio-internals.md) is the mechanism: `co_flags`,
> `GET_AWAITABLE`/`SEND`, the loop iteration, the timer heap, `Task.__step`. **This
> document is the same machinery seen from production.** It does not re-explain how a
> `Task` works; it explains what happens to your service when you use one wrongly.

---

## Contents

1. [The five failure modes](#1-the-five-failure-modes)
2. [Backpressure: what an unbounded queue really costs](#2-backpressure-what-an-unbounded-queue-really-costs)
3. [Task lifetime: when a fire-and-forget task actually dies](#3-task-lifetime-when-a-fire-and-forget-task-actually-dies)
4. [Structured concurrency: `gather` leaves orphans, `TaskGroup` does not](#4-structured-concurrency-gather-leaves-orphans-taskgroup-does-not)
5. [Cancellation is edge-triggered, and that is the whole problem](#5-cancellation-is-edge-triggered-and-that-is-the-whole-problem)
6. [`shield`: protecting work you have already given up on](#6-shield-protecting-work-you-have-already-given-up-on)
7. [`uncancel`, `cancelling()`, and the TaskGroup contract](#7-uncancel-cancelling-and-the-taskgroup-contract)
8. [Timeouts, deadlines, and propagation](#8-timeouts-deadlines-and-propagation)
9. [Blocking the loop: the only bug that matters](#9-blocking-the-loop-the-only-bug-that-matters)
10. [A loop-lag monitor you can run in production](#10-a-loop-lag-monitor-you-can-run-in-production)
11. [Sync ↔ async bridges that do not deadlock](#11-sync--async-bridges-that-do-not-deadlock)
12. [Executors: `to_thread`, `run_in_executor`, and sizing](#12-executors-to_thread-run_in_executor-and-sizing)
13. [`contextvars`: propagation, isolation, and cost](#13-contextvars-propagation-isolation-and-cost)
14. [Graceful shutdown](#14-graceful-shutdown)
15. [anyio and trio: the choices they made differently](#15-anyio-and-trio-the-choices-they-made-differently)
16. [A production checklist](#16-a-production-checklist)
17. [What I could not verify](#17-what-i-could-not-verify)
18. [Lab exercises](#18-lab-exercises)
19. [Question bank](#19-question-bank)
20. [Sources](#20-sources)

---

## 1. The five failure modes

Nearly every asyncio production incident is one of five things. This document is
organized around them, and each one is measured rather than asserted.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 1. THE LOOP IS BLOCKED                                          §9, §10  │
│    One synchronous call. p50 stays healthy, p99 tracks the call length.  │
│    Signature: latency histogram grows a second mode.                     │
├──────────────────────────────────────────────────────────────────────────┤
│ 2. NO BACKPRESSURE                                                  §2   │
│    Producer outruns consumer into an unbounded queue.                    │
│    Signature: RSS climbs, latency climbs, throughput does NOT.           │
├──────────────────────────────────────────────────────────────────────────┤
│ 3. ORPHANED / LEAKED TASKS                                     §3, §4    │
│    Work that outlives the request, or vanishes mid-flight.               │
│    Signature: "Task was destroyed but it is pending!", or ghost writes.  │
├──────────────────────────────────────────────────────────────────────────┤
│ 4. CANCELLATION THAT DOESN'T                                   §5–§8     │
│    A timeout fires, the caller returns, the work keeps going.            │
│    Signature: load that doesn't drop when you shed traffic.              │
├──────────────────────────────────────────────────────────────────────────┤
│ 5. A BRIDGE THAT DEADLOCKS                                         §11   │
│    Sync code waiting on the loop from inside the loop.                   │
│    Signature: total freeze, zero CPU.                                    │
└──────────────────────────────────────────────────────────────────────────┘
```

The unifying observation, and the reason this document exists separately from doc 28:
**every one of these is invisible in a unit test and obvious in a latency histogram.**
They are operational failures, and they need operational instrumentation, not more
`assert`s.

---

## 2. Backpressure: what an unbounded queue really costs

The single most common architectural mistake in async Python is
`asyncio.Queue()` with no `maxsize`. Here is what it buys you.

Setup: a producer and a consumer, 30,000 messages of 512 bytes, consumer deliberately
slower than the producer. Only `maxsize` changes.

| `maxsize` | peak depth | p50 latency | p99 latency | max latency | wall |
|---|---|---|---|---|---|
| **0 (unbounded)** | **29,960** | **573.6 ms** | **1126.6 ms** | 1137.8 ms | 1.15 s |
| 100,000 | 29,960 | 573.2 ms | 1125.5 ms | 1136.9 ms | 1.15 s |
| 1,000 | 1,000 | 36.6 ms | 37.8 ms | 37.9 ms | 1.10 s |
| 100 | 100 | 3.7 ms | 3.9 ms | 4.3 ms | 1.10 s |
| 10 | 10 | 0.4 ms | 0.4 ms | 0.6 ms | 1.10 s |
| 1 | 1 | 0.1 ms | 0.1 ms | 0.1 ms | 1.10 s |

**Read the wall column first: it is the same in every row (1.10–1.15 s).** The unbounded
queue is not faster. It moves exactly as many messages in exactly as much time. What it
does is convert a *throughput mismatch* into *latency and memory*, and then hide it.

### 2.1 This is Little's Law, and it is exact

L = λW. Queue depth equals arrival rate times waiting time, so W = L/λ. Throughput here
is 30,000 / 1.10 s ≈ **27,273 items/s**. Predicting p50 latency from depth alone:

| depth | predicted W = L/λ | measured p50 | error |
|---|---|---|---|
| 1,000 | 36.7 ms | 36.6 ms | 0.3% |
| 100 | 3.67 ms | 3.7 ms | 0.8% |
| 10 | 0.367 ms | 0.4 ms | 9% |
| 29,960 | 1,098 ms | (p99) 1,127 ms | 2.6% |

The law holds to within a few percent across three orders of magnitude. **Your queue
depth *is* your latency**, divided by a throughput you do not control. There is nothing
to tune here and no cleverness available — the only lever is the depth.

### 2.2 What the bound actually does

A bounded queue makes `await q.put(...)` block when full. That blocking propagates
backwards:

```
   client ──► handler ──► await q.put()  ◄── blocks here when full
                                │
                                ▼
                       consumer at capacity

   The handler stops accepting. Connections queue in the kernel.
   The kernel accept queue fills. New connections are REFUSED.
   The load balancer sees the refusal and sheds to another instance.
```

That chain is the point. **Backpressure is how a system tells its caller "no."** An
unbounded queue removes every rung of that ladder and replaces it with an OOM kill at an
unpredictable time, after an unpredictable amount of latency-degraded service.

### 2.3 The rule

> **Every queue gets a `maxsize`. Every one, including the ones you think are internal.**
> Choose the bound from your latency budget, not your memory budget: pick the p99 latency
> you are willing to serve, multiply by throughput, and that is your depth.

For 27,273 items/s and a 50 ms budget: depth ≈ 1,363. Round to 1,000 and you have the
row that measured 37.8 ms p99.

Related patterns, in increasing sophistication:

```python
# 1. Bound it. (Almost always sufficient.)
q = asyncio.Queue(maxsize=1000)

# 2. Shed load instead of blocking, when latency matters more than completeness.
try:
    q.put_nowait(item)
except asyncio.QueueFull:
    metrics.increment("dropped")        # and ALARM on this

# 3. Bound concurrency rather than buffering, when there is no natural queue.
sem = asyncio.Semaphore(50)
async def handle(x):
    async with sem:
        await do_work(x)
```

Pattern 3 deserves emphasis because it is the one people miss: **`asyncio.gather` over
10,000 items launches 10,000 concurrent tasks.** There is no implicit limit. A
`Semaphore` (or a `TaskGroup` fed by a bounded queue) is what stops you from opening
10,000 sockets at once.

---

## 3. Task lifetime: when a fire-and-forget task actually dies

The asyncio docs warn:

> Save a reference to the result of `create_task()`, to avoid a task disappearing
> mid-execution.

This warning is real, widely repeated, and **almost always stated too strongly.** I could
not reproduce it in the obvious way: 2,000 fire-and-forget tasks, `gc.collect()` after
every single `create_task`, and **2,000 of 2,000 completed**. Zero lost.

So when does it actually bite? The precise experiment — drop our reference, force a
collection, and ask whether the task survived:

| what the task is doing | survives? |
|---|---|
| just created (queued in `loop._ready`) | **ALIVE** |
| suspended in `asyncio.sleep()` (timer in `loop._scheduled`) | **ALIVE** |
| waiting on an `Event` **we** still hold a reference to | **ALIVE** |
| waiting on an `Event` **only the task itself** references | **COLLECTED** |
| awaiting a bare `Future` nothing else references | **COLLECTED** |
| awaiting `gather()` of orphan futures | **COLLECTED** |
| eager task factory + orphan future | **COLLECTED** |

And in each collected case CPython emitted, via the loop exception handler:

```
Task was destroyed but it is pending!
task: <Task pending name='Task-14' coro=<...w() done, defined at leak2.py:48>
      wait_for=<Future pending cb=[Task.task_wakeup()]>>
```

### 3.1 The actual rule

The event loop **does not** hold a strong reference to your tasks. `asyncio.all_tasks()`
is backed by a weak registry (in 3.14 the implementation lives in the C `_asyncio`
module). What keeps a task alive is the thing that will *wake* it:

```
   task in loop._ready       → Handle holds task.__step        → ALIVE
   task sleeping             → TimerHandle in _scheduled       → ALIVE
   task awaiting a Future    → Future._callbacks holds it      → ALIVE
                               …but only if the FUTURE is itself reachable
```

Inspecting the referrers of a sleeping task confirms the chain:

```
referrers of a sleeping task: {'coroutine': 1, 'Future': 1, 'builtin_function_or_method': 1}
len(loop._scheduled)=1  len(loop._ready)=0
```

> **The rule, stated correctly:** a pending task is kept alive *transitively, by whatever
> will resume it*. It becomes garbage exactly when **the thing it is waiting for is
> itself unreachable** — at which point the task could never have been resumed anyway,
> so collecting it is arguably correct behaviour rather than a bug.

### 3.2 Why you should still keep the reference

Three reasons that survive the above:

1. **You cannot easily audit reachability.** "Is the future that will resolve this task
   reachable?" is not a question you want to answer during a code review. The idiom is
   cheap; the analysis is not.
2. **You need the handle anyway** — to cancel it at shutdown (§14), to observe its
   exception, to await it.
3. **Unobserved exceptions vanish.** A fire-and-forget task that raises logs
   "Task exception was never retrieved" only when it is *collected*, which may be much
   later or never.

The standard idiom, which also solves the leak:

```python
_background: set[asyncio.Task] = set()

def spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _background.add(t)
    t.add_done_callback(_background.discard)   # self-cleaning: no unbounded growth
    return t
```

The `add_done_callback(discard)` is the part people omit, and without it the set *is*
your leak — a slow one that looks like a memory leak in the application rather than a
task-management bug.

**Better still: do not fire and forget.** §4's `TaskGroup` gives you a scope that owns
the task, which removes the entire question.

---

## 4. Structured concurrency: `gather` leaves orphans, `TaskGroup` does not

One child fails. What happens to its siblings? Measured, with a sibling that logs whether
it was cancelled or ran to completion:

| construct | sibling's fate | what the caller sees |
|---|---|---|
| `gather(..., return_exceptions=False)` | **RAN TO COMPLETION** | `ValueError: boom` |
| `gather(..., return_exceptions=True)` | **RAN TO COMPLETION** | exceptions in the result list |
| `asyncio.TaskGroup()` | **cancelled** | `ExceptionGroup` with 1 exception |

`gather` does not cancel siblings on failure. It raises the first exception to *you*
while everything else keeps running, unsupervised, with nobody holding a reference.

### 4.1 The orphan, measured

A background task ticking every 20 ms, alongside a task that fails after 10 ms:

```
background task had done 0 ticks when gather() raised;
0.3s later it had done 10. It was never cancelled.
```

The caller got its exception at t≈10 ms and moved on — believing the operation had
failed and stopped. The background work continued for another 200 ms, writing to whatever
it writes to. **In a request handler, that is work attributed to a request that has
already returned 500 to the client**, holding a DB connection, consuming quota, and
mutating state.

Under `TaskGroup`, the same sibling is cancelled at the moment of failure.

### 4.2 The exception-handling difference

`TaskGroup` raises an `ExceptionGroup`, which is not a drop-in for what `gather` raised:

```python
# gather: first exception wins, others are discarded
try:
    await asyncio.gather(a(), b(), c())
except ValueError:
    ...

# TaskGroup: ALL failures arrive, and you must use except*
try:
    async with asyncio.TaskGroup() as tg:
        tg.create_task(a()); tg.create_task(b()); tg.create_task(c())
except* ValueError as eg:
    for e in eg.exceptions:        # possibly several
        ...
except* ConnectionError as eg:
    ...
```

This is a real migration cost and the honest reason `gather` persists in codebases. It is
still worth paying: the `ExceptionGroup` is not noise, it is the information `gather` was
throwing away.

### 4.3 When `gather` is still right

`gather` is correct when the operations are genuinely independent and you want *all*
results regardless of individual failures — a fan-out to several caches where any subset
is useful:

```python
results = await asyncio.gather(*(fetch(u) for u in urls), return_exceptions=True)
ok = [r for r in results if not isinstance(r, BaseException)]
```

Note what you have accepted: if the caller of *this* function is cancelled, these tasks
are still not supervised. Wrap the whole thing in a `TaskGroup` or a timeout scope that
owns them.

**Default to `TaskGroup`. Reach for `gather` deliberately, with `return_exceptions=True`,
when partial success is meaningful.**

---

## 5. Cancellation is edge-triggered, and that is the whole problem

This is the deepest semantic issue in asyncio, and the clearest place where trio chose
differently (§15).

Ask: after a task has been cancelled and has caught `CancelledError`, do its *subsequent*
awaits also raise? Measured — catch the first `CancelledError`, then hit three more
checkpoints:

**asyncio:**
```
caught #1
checkpoint 0: passed
checkpoint 1: passed
checkpoint 2: passed
```

**trio:**
```
caught #1
checkpoint 0: Cancelled AGAIN
checkpoint 1: Cancelled AGAIN
checkpoint 2: Cancelled AGAIN
```

asyncio's cancellation is **edge-triggered**: `Task.cancel()` arranges for
`CancelledError` to be thrown in *once*. If the coroutine catches it and does not
re-raise, the cancellation is simply gone. Verified end-to-end:

```
caught #1
second await COMPLETED (cancel did not stick)
task returned 'swallowed'  -> cancellation was SWALLOWED
```

trio's is **level-triggered**: a cancel scope stays cancelled, and *every* checkpoint
inside it raises until control leaves the scope. You cannot escape by catching.

### 5.1 Why this matters operationally

Every `except Exception:` in your codebase is a potential cancellation swallower — and in
Python 3.8+ `CancelledError` inherits from `BaseException`, precisely so that bare
`except Exception` does *not* catch it. But these still do:

```python
try:
    await something()
except BaseException:          # catches CancelledError
    log.exception("oops")      # ...and does not re-raise
    return fallback            # cancellation silently defeated

try:
    await something()
finally:
    await cleanup()            # if cleanup() blocks, cancellation is DELAYED
                               # if cleanup() raises, cancellation is REPLACED
```

The second one is subtler and more common. A `finally` block that awaits is running
*during* cancellation; it gets one free pass through the checkpoint (edge-triggered), so
a slow cleanup delays shutdown unboundedly. §14 measures exactly this.

### 5.2 The discipline

```python
try:
    await work()
except asyncio.CancelledError:
    await _fast_cleanup()      # bounded! no network calls, no locks
    raise                      # ALWAYS re-raise
except Exception:
    ...                        # ordinary errors here
```

Three rules that follow directly from edge-triggering:

1. **Always re-raise `CancelledError`.** If you catch it, you own the obligation.
2. **Never `await` something slow in a cancellation handler or `finally`.** You have one
   free checkpoint; spend it on a `put_nowait`, not an HTTP call.
3. **Never catch `BaseException` around an `await`** unless you re-raise unconditionally.

---

## 6. `shield`: protecting work you have already given up on

`asyncio.shield(fut)` returns an awaitable that, when cancelled, does **not** cancel
`fut`. Measured, with a 200 ms "critical" operation and a caller cancelled at 50 ms:

| | inner task's fate | caller's fate |
|---|---|---|
| **no shield** | `critical CANCELLED` | `CancelledError` |
| **`shield`** | `caller done; inner.done()=False` → later `critical COMPLETED`, result available | `CancelledError` |

So the shield works exactly as documented — and the documentation's implication is the
trap:

> **`shield` does not protect the caller. It protects the callee, and abandons it.**

After the shield is cancelled, your coroutine is gone. The shielded task runs on with
nobody awaiting it, nobody holding a reference (§3), and nobody to observe its exception.
You have deliberately created an orphan.

### 6.1 The only correct shape

`shield` is right when the operation *must* complete for correctness even though this
caller no longer cares — committing a transaction, releasing a remote lease, flushing an
audit record. And in every one of those cases you must **keep the task and have someone
else own it**:

```python
_critical: set[asyncio.Task] = set()

async def commit_and_reply(txn):
    t = asyncio.create_task(txn.commit())
    _critical.add(t); t.add_done_callback(_critical.discard)
    try:
        return await asyncio.shield(t)
    except asyncio.CancelledError:
        # We are going away; the commit is NOT. It is owned by _critical
        # and will be drained at shutdown (§14).
        raise
```

Without that set, a shielded commit that outlives its caller is exactly the "waiting on
a Future only the task references" row of §3 — collectible mid-flight.

**If you find `shield` in a code review and there is no external owner for the shielded
task, it is a bug.**

---

## 7. `uncancel`, `cancelling()`, and the TaskGroup contract

Python 3.11 added cancellation *bookkeeping* to make `TaskGroup` and `asyncio.timeout`
composable. Measured:

```
cancelling() = 1
after uncancel(): 0
result: 'recovered'
```

`Task.cancel()` increments a counter; `Task.uncancel()` decrements it. `cancelling()`
reads it. The counter exists to answer a question that arises the moment scopes nest:

> A `TaskGroup` cancelled its children because one failed. An inner `asyncio.timeout`
> also fired. When the inner scope catches `CancelledError`, was it *its* cancellation or
> the group's?

Without the counter, an inner timeout would swallow the group's cancellation and the
group would hang. `asyncio.timeout` calls `uncancel()` when it converts `CancelledError`
into `TimeoutError`; if the count does not return to zero, the cancellation belonged to
someone else and must keep propagating.

### 7.1 What this means for you

**You almost certainly should not call `uncancel()`.** It exists for framework code —
`TaskGroup`, `timeout`, and third-party scope implementations. Calling it in application
code means claiming a cancellation that may not have been yours, which reintroduces the
hang it was designed to prevent.

What you *should* do is respect the invariant when writing a scope-like abstraction:

```python
# If you catch CancelledError and convert it to something else,
# you MUST uncancel exactly once, and only if the count says it was yours.
if task.uncancel() == 0 and self._timed_out:
    raise TimeoutError from None
raise            # not ours -- keep propagating
```

---

## 8. Timeouts, deadlines, and propagation

### 8.1 Precision

`asyncio.timeout()` overshoot, 20 trials per target:

| target | median over | max over |
|---|---|---|
| 1 ms | **0.203 ms** | 0.237 ms |
| 5 ms | 0.706 ms | 0.752 ms |
| 20 ms | 1.116 ms | 1.214 ms |
| 100 ms | 1.205 ms | 1.307 ms |

Overshoot converges to about **1.2 ms** and does not scale with the target. That is the
loop's wakeup granularity, and it matches the ~1.07 ms idle lag floor measured
independently in §10. trio's `move_on_after` is statistically identical (0.212 / 0.708 /
1.136 / 1.194 ms) — this is a platform property, not a library one.

**Consequence:** a 1 ms timeout has 20% error. Timeouts below ~10 ms are not meaningful on
this platform; if you need them, you need a different mechanism (and probably not Python).

### 8.2 A timeout cancels the *await*, not the *work*

The critical misunderstanding. Measured, with `shield` in the middle:

```
caller timed out
task state: done=True cancelled=False
```

The caller timed out at 50 ms. The task **completed normally** afterwards. A timeout is a
cancellation request delivered to whatever you are awaiting; if that thing is shielded,
detached, or catches and swallows (§5), the timeout affects only your control flow.

This is the mechanism behind the most confusing production symptom in async services:
**you shed load, request rate drops, and backend load does not.** Every timed-out request
left its work running.

### 8.3 `timeout` vs `wait_for`

```python
# 3.11+: the scope form. Preferred.
async with asyncio.timeout(5):
    await step_one()
    await step_two()          # the budget covers BOTH

# older: per-await, and it wraps the coroutine in a Task
result = await asyncio.wait_for(step_one(), 5)
```

`asyncio.timeout` is a scope with correct `uncancel` bookkeeping (§7), covers multiple
awaits under one budget, and can be rescheduled while running
(`cm.reschedule(new_deadline)`). Prefer it.

### 8.4 Deadline propagation

Timeouts do not compose by nesting durations. Three services each with a "5 second
timeout" produce a 15-second worst case. Propagate an **absolute deadline** instead:

```python
DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("deadline", default=None)

async def call_downstream(req):
    dl = DEADLINE.get()
    remaining = None if dl is None else dl - asyncio.get_running_loop().time()
    if remaining is not None and remaining <= 0:
        raise TimeoutError("deadline already exceeded")
    async with asyncio.timeout(remaining):          # None = no timeout
        return await http.post(..., headers={"X-Deadline-Ms": str(int(remaining*1000))})
```

Use `loop.time()` (monotonic) for the deadline, never `time.time()` — see
[`30-concurrency-correctness.md`](30-concurrency-correctness.md) §14, which measures why
`time.time()` is `adjustable=True` and can move backwards. `contextvars` is the right
carrier because it propagates into tasks automatically (§13).

---

## 9. Blocking the loop: the only bug that matters

If you fix one class of async bug, fix this one. From
[`30-concurrency-correctness.md`](30-concurrency-correctness.md) §13, measured — a ticker
wanting to run every 1 ms while a 300 ms synchronous call runs:

| scheduler | p50 | p99 | max |
|---|---|---|---|
| **asyncio** (blocking call in a coroutine) | 1.18 ms | **306.07 ms** | 306.1 ms |
| **threads** (same work, separate thread) | 2.74 ms | **7.58 ms** | 7.6 ms |

**40× difference in tail latency.** Cooperative scheduling cannot preempt: the loop
regains control only when your coroutine awaits. A blocking call in one handler degrades
*every concurrent request on that process*, which is why the symptom is so confusing —
the slow endpoint is often not the one that broke.

### 9.1 The signature

**p50 stays healthy; p99 tracks the length of the blocking call.** A latency histogram
grows a second mode at the blocking duration. If your p99 is suspiciously close to a
round number (100 ms, 250 ms, 1 s), look for a synchronous call of that length.

### 9.2 The usual culprits

| looks async | actually blocks |
|---|---|
| `requests.get(...)` | always — use `httpx.AsyncClient` / `aiohttp` |
| `time.sleep(x)` | always — use `await asyncio.sleep(x)` |
| `psycopg2`, `pymysql`, most DB drivers | always — use `asyncpg`, `aiomysql`, or `to_thread` |
| `open(...).read()` | yes, on a slow disk or NFS |
| `json.loads(huge)` | yes — CPU-bound, ~100 ms for tens of MB |
| `re.match` with a pathological pattern | yes — catastrophic backtracking |
| `logging` to a slow handler | yes — syslog over TCP, blocking file writes |
| `socket.getaddrinfo` (DNS) | yes — and `loop.getaddrinfo` uses a thread by default |
| bcrypt / scrypt / argon2 | **by design** — always `to_thread` these |
| a big `for` loop over a large list | yes, if it never awaits |

### 9.3 Built-in detection: `slow_callback_duration`

```python
loop.set_debug(True)
loop.slow_callback_duration = 0.1     # seconds
```

Measured: with the threshold at 0.1 s, a 0.05 s block produced **no** warning and a 0.20 s
block produced **1**, naming the offending handle. It works, and it has two problems for
production use: debug mode is off by default, and it adds tracing overhead to every
callback. That is what §10 is for.

---

## 10. A loop-lag monitor you can run in production

The loop cannot measure itself while blocked. The trick is to measure the *error* of a
timer: ask to be woken in `interval` seconds and see how late you actually are. That
lateness **is** the block.

```python
class LoopLagMonitor:
    """Samples event-loop lag; reports when it exceeds a threshold."""

    def __init__(self, interval=0.05, threshold=0.1):
        self.interval, self.threshold = interval, threshold
        self.samples, self.events = [], []
        self._task, self._stop = None, False

    async def _run(self):
        loop = asyncio.get_running_loop()
        expected = loop.time() + self.interval
        while not self._stop:
            await asyncio.sleep(self.interval)
            now = loop.time()
            lag = now - expected              # <-- how late the loop was
            self.samples.append(lag * 1000)
            if lag > self.threshold:
                stacks = [
                    f"{t.get_name()}@{t.get_stack(limit=1)[0].f_code.co_name}"
                    for t in asyncio.all_tasks(loop)
                    if t is not asyncio.current_task() and t.get_stack(limit=1)
                ]
                self.events.append((lag * 1000, stacks[:3]))
            expected = now + self.interval

    def start(self):
        self._task = asyncio.create_task(self._run())
        return self
```

### 10.1 Detection quality, measured

Sampling every 20 ms, threshold 50 ms:

| scenario | lag p50 | lag p99 | breaches detected |
|---|---|---|---|
| baseline, no blocking | 1.07 ms | 1.17 ms | **0** (0 injected) |
| blocks of 200 / 300 / 120 ms | 1.12 ms | 302.26 ms | **3** (3 injected) |

Reported lags of **192.3 / 302.3 / 119.7 ms** against injected blocks of 200 / 300 /
120 ms. Every block caught, no false positives, and the magnitude is accurate to within a
sampling interval.

Note the **1.07 ms idle floor** — that is `asyncio.sleep`'s granularity, the same number
as §8.1's timeout overshoot. Set your threshold well above it; anything under ~5 ms is
measuring the platform, not your code.

### 10.2 Overhead

First measurement came back at **−7.3%** — the monitored run was *faster*, which is
causally impossible and therefore noise. Re-measured properly with A/B/B/A alternation,
8 runs per arm, 1 ms sampling (20× more aggressive than you would ever deploy):

```
monitor OFF: median 1506.7 ns/coro   (min 1494, max 1513)
monitor ON : median 1515.2 ns/coro   (min 1493, max 1588)
ratio = 1.006  (+0.6%)
run-to-run spread: off 1.01x, on 1.06x
-> effect is BELOW the noise floor
```

**The honest claim is not "0.6% overhead" but "not measurable against a ~6% noise
floor."** At a realistic 50 ms sampling interval it is 50× less work than that. Run it in
production.

See [`31-measurement-methodology.md`](31-measurement-methodology.md) for why the first
number came out negative and why one-shot benchmarks of small effects are worthless.

### 10.3 What to do with the signal

Lag is a **service-level indicator**, not a debug print. Export `p99(lag)` to your metrics
system and alert on it. When it breaches, you want a stack — the monitor above collects
pending task frames, which tells you what the loop is *carrying*; for the culprit itself
(already returned by the time you sample) use `py-spy dump` against the process, or turn
on `slow_callback_duration` temporarily (§9.3).

---

## 11. Sync ↔ async bridges that do not deadlock

Four situations, three of which people get wrong.

### 11.1 Calling async from sync, no loop running

```python
asyncio.run(main())          # correct: creates a loop, runs, closes it
```

### 11.2 Calling async from sync *while a loop is already running in this thread*

Measured — both of the obvious attempts fail loudly, which is good:

```
asyncio.run(...)          → RuntimeError: asyncio.run() cannot be called from a running event loop
loop.run_until_complete() → RuntimeError: This event loop is already running
```

There is no supported way to do this. If you are here, the calling function must become
`async`, or the sync work must move to a thread. (`nest_asyncio` monkey-patches around it
and breaks the invariants everything else depends on. Do not.)

### 11.3 The real deadlock: blocking on a future from the loop thread

This one does **not** raise. It hangs:

```python
fut = asyncio.run_coroutine_threadsafe(work(), loop)
fut.result(timeout=0.5)      # called FROM the loop thread
```

Measured: `TIMEOUT (deadlocked: loop cannot run the coro)`. The loop thread is blocked
inside `fut.result()`, so it cannot run the coroutine that would resolve the future. Zero
CPU, total freeze — failure mode 5 from §1. Without the timeout it hangs forever.

### 11.4 The correct bridge: from a *different* thread

```python
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()

fut = asyncio.run_coroutine_threadsafe(work(), loop)   # thread-safe
value = fut.result(timeout=2.0)                        # blocks THIS thread, not the loop
```

Measured: returns `42` in **51.6 ms** for a coroutine that sleeps 50 ms. Correct and
cheap.

The rules:

| from | to | use |
|---|---|---|
| sync, no loop | async | `asyncio.run()` |
| **another thread** | async | `asyncio.run_coroutine_threadsafe(coro, loop)` → `.result()` |
| another thread | just schedule, no result | `loop.call_soon_threadsafe(fn)` |
| async | blocking sync | `await asyncio.to_thread(fn)` (§12) |
| async | async, other loop | there is no such thing — use a queue |

**`call_soon_threadsafe` and `run_coroutine_threadsafe` are the only two loop methods
that are safe to call from another thread.** Everything else on the loop object assumes
you are on the loop thread.

---

## 12. Executors: `to_thread`, `run_in_executor`, and sizing

Measured costs per call, 2,000 calls of a no-op:

| call | cost | vs direct await |
|---|---|---|
| `asyncio.to_thread(noop)` | **22,343 ns** | 14× |
| `loop.run_in_executor(None, noop)` | **12,050 ns** | 7.6× |
| `await coro` directly | 1,594 ns | 1× |
| `create_task` + `gather` | 1,613 ns | 1.01× |

### 12.1 Why `to_thread` costs 1.85× `run_in_executor`

The source explains it exactly:

```python
async def to_thread(func, /, *args, **kwargs):
    loop = events.get_running_loop()
    ctx = contextvars.copy_context()                       # <-- this
    func_call = functools.partial(ctx.run, func, *args, **kwargs)
    return await loop.run_in_executor(None, func_call)
```

`to_thread` copies the current `Context` so your `contextvars` are visible in the thread,
and pays a `partial` plus `ctx.run` per call. That is usually what you want — request IDs
and deadlines (§8.4) should cross into the thread — and it is why it is not the cheaper
one.

**Neither is free. ~12–22 µs per call means offloading anything shorter than about
100 µs of work is a net loss.** Batch instead: hand the executor 1,000 items, not 1,000
calls.

### 12.2 The default executor's size, and the container trap

Measured on this 11-CPU machine: `ThreadPoolExecutor max_workers=15`. The formula, from
`concurrent/futures/thread.py`:

```python
# We use process_cpu_count + 4 for both types of tasks.
# But we limit it to 32 to avoid consuming surprisingly large resource
max_workers = min(32, (os.process_cpu_count() or 1) + 4)
```

Note it is **`os.process_cpu_count()`**, not `os.cpu_count()` — it respects CPU affinity,
which matters in containers where the two differ. Two consequences:

1. **The default pool is shared by every `to_thread` call in your process**, including
   `loop.getaddrinfo`'s DNS lookups. 15 slots is not many. If you offload 15 slow blocking
   calls, DNS resolution stalls behind them and the symptom looks like a network problem.
2. **For blocking I/O, 15 is usually too small**; for CPU-bound work, anything above the
   core count is pointless under the GIL.

Set your own, per purpose:

```python
db_pool = concurrent.futures.ThreadPoolExecutor(max_workers=50, thread_name_prefix="db")
...
await loop.run_in_executor(db_pool, blocking_query, sql)
```

Separate pools give you isolation (a saturated DB pool cannot starve DNS) and
attribution (thread names show up in `py-spy`).

### 12.3 Processes

`ProcessPoolExecutor` via `run_in_executor` is the answer for CPU-bound work, and its cost
is dominated by pickling, not by the pool. See
[`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md).
The asyncio-specific caveat: the default executor is *never* a process pool, and
`asyncio.to_thread` has no process equivalent.

---

## 13. `contextvars`: propagation, isolation, and cost

The semantics, measured:

| | value seen |
|---|---|
| parent sets `req-A`, then `create_task(child)` — what does child see? | **`req-A`** |
| child sets `req-CHILD` — what does parent see afterwards? | **`req-A`** (unchanged) |
| parent `await`s a plain coroutine that sets `req-INLINE` — parent sees? | **`req-INLINE`** |

Two rules follow:

1. **A `Task` gets a *copy* of the context at creation time.** Writes inside the task do
   not escape. This is what makes contextvars correct for request-scoped data under
   concurrency — unlike a global, and unlike `threading.local` under asyncio, where all
   tasks on one loop share the same thread.
2. **A bare `await` does *not* create a new context.** Awaiting a coroutine directly runs
   it in *your* context, so its writes are visible to you. The boundary is the `Task`,
   not the `await`.

That second rule is the surprising one and a genuine footgun: whether a callee can mutate
your context depends on whether someone wrapped it in a task, which is an implementation
detail of the callee.

### 13.1 Cost

Lookup, 1,000,000 iterations:

| access | cost |
|---|---|
| `ContextVar.get()` | **41.87 ns** |
| `threading.local` attribute | 36.24 ns |
| `dict['x']` | 20.59 ns |
| local variable | 16.27 ns |

`ContextVar.get()` is ~2× a dict lookup and ~2.6× a local. Cheap enough for
request-scoped metadata; too expensive for an inner loop — hoist it to a local.

Task creation cost vs. how many contextvars are set:

| contextvars set | ns/task |
|---|---|
| 0 | 1,728 |
| 10 | 1,665 |
| 100 | 1,785 |

**Flat.** Copying a context is O(1), not O(n) — `Context` is an immutable HAMT with
structural sharing, so `copy_context()` copies a pointer, not the mapping. You can put as
much in the context as you like without making task creation more expensive.

### 13.2 The pattern

```python
REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id")

async def handle(request):
    REQUEST_ID.set(request.headers["x-request-id"])
    await do_everything()        # every task spawned below inherits it

class ContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = REQUEST_ID.get("-")   # default avoids LookupError
        return True
```

Always use `.get(default)` in logging paths — a bare `.get()` raises `LookupError` outside
a request, and an exception inside a log filter is its own kind of bad day.

---

## 14. Graceful shutdown

A correct shutdown drains work with a **bound**, then forces the rest. Measured, 10
workers where half have 10 ms of cleanup and half have 500 ms, drained with a 200 ms
window:

```
{'clean': 5, 'forced': 5, 'wall_ms': 201.2}
```

Exactly what the design predicts: the fast half finished, the slow half did not, and the
whole thing took 201 ms rather than 500 ms. **The bound is the feature.** An unbounded
drain means a deploy can hang forever on one stuck task.

### 14.1 The full shape

```python
async def main():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)      # NOT signal.signal()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(serve(stop))
        await stop.wait()
        # leaving the TaskGroup cancels and awaits every child

async def shutdown(timeout=10.0):
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        log.error("forcing %d tasks that did not drain in %.1fs", len(pending), timeout)
        for t in pending:
            log.error("  stuck: %s", t.get_coro())
    return len(done), len(pending)
```

Four details that matter:

1. **`loop.add_signal_handler`, not `signal.signal`.** The latter runs your handler in the
   C signal context and hands it to the loop only at the next check point; the former
   integrates with the loop's self-pipe (doc 28 §9). On Windows it is unavailable — use
   `signal.signal` plus `call_soon_threadsafe`.
2. **Log what didn't drain.** `t.get_coro()` names the stuck coroutine. Without this you
   are guessing after every deploy.
3. **Cancel in the right order.** Stop *accepting* first, then drain in-flight. Cancelling
   the acceptor and the handlers simultaneously loses requests that were mid-flight.
4. **`asyncio.Queue.shutdown()`** (3.13+, present in 3.14 along with
   `asyncio.QueueShutDown`) unblocks every waiting producer and consumer — the missing
   primitive that used to force sentinel-per-consumer dances.

### 14.2 The interaction with §5

Cleanup that awaits during cancellation gets exactly one free checkpoint before the next
cancellation lands. If your `finally` blocks do real I/O, your drain window is a
suggestion. Keep cancellation cleanup bounded and synchronous where you can.

---

## 15. anyio and trio: the choices they made differently

Verified by running the same three questions against both runtimes.

| question | asyncio | trio |
|---|---|---|
| child fails — sibling's fate? | `gather`: **runs on**; `TaskGroup`: cancelled | nursery: **cancelled** (only option) |
| can a task be spawned with no supervisor? | **yes** — `create_task` | **no** — tasks require a nursery |
| orphan possible after a failure? | **yes** — 0 ticks at failure, **10** after 0.3 s | **no** — 0 ticks, **0** after 0.3 s |
| cancellation model | **edge**-triggered (§5) | **level**-triggered |
| after cancel, do later checkpoints raise? | **no** (0/3 raised) | **yes** (3/3 raised) |
| timeout precision | 0.20 / 0.71 / 1.12 / 1.21 ms | 0.21 / 0.71 / 1.14 / 1.19 ms |

### 15.1 What trio got right

**Structured concurrency is not optional.** There is no `trio.create_task()`; a task
needs a nursery, the nursery block cannot exit until its children are done, and if one
child fails the rest are cancelled. §4's orphan is not a bug you can write.

**Cancellation is level-triggered**, so it cannot be swallowed. You may catch
`trio.Cancelled` without re-raising, but the very next checkpoint raises again — control
*will* leave the cancelled scope. asyncio's edge trigger lets a task escape permanently
(§5, measured).

**Cancel scopes are first-class objects.** Timeouts are just cancel scopes with a
deadline, so nesting and rescheduling compose by construction rather than by
`uncancel()` bookkeeping (§7).

### 15.2 What asyncio has

The ecosystem. Every database driver, HTTP client, and framework targets asyncio.
`TaskGroup` (3.11) and `asyncio.timeout` (3.11) are direct ports of trio's nursery and
cancel scope — **the design argument was won; only the cancellation model differs.**

### 15.3 anyio: the pragmatic answer

anyio implements trio's structural API *on top of* asyncio. The identical program ran on
both backends:

```
backend=asyncio  result=[0, 1, 2, 3, 4]  (72.6ms)
backend=trio     result=[0, 1, 2, 3, 4]  (52.6ms)
```

Use anyio when you are writing a **library** — it lets your users pick the runtime and
gives you trio's structure without abandoning the asyncio ecosystem. For an
**application** already on asyncio, `TaskGroup` plus `asyncio.timeout` plus the discipline
in §5 gets you most of the way, and is one fewer dependency.

> **Caveat on those timings.** They are single runs of a 50 ms workload dominated by
> `sleep`, on a loaded machine. They say the two backends work; they say nothing about
> relative performance, and I make no such claim.

---

## 16. A production checklist

**Backpressure**
- [ ] Every `asyncio.Queue` has a `maxsize`, chosen from a latency budget (§2.3).
- [ ] Every unbounded fan-out (`gather` over a list, a loop of `create_task`) is bounded by a `Semaphore` or a `TaskGroup` fed from a bounded queue.
- [ ] Queue-full is a metric with an alert, not a silent block.

**Task lifetime**
- [ ] No bare `create_task(...)` whose result is discarded; every spawn is either awaited, held in a set with `add_done_callback(discard)`, or owned by a `TaskGroup` (§3.2).
- [ ] `TaskGroup` is the default; `gather` is used deliberately with `return_exceptions=True` (§4.3).
- [ ] Every `shield` has an external owner for the shielded task (§6.1).

**Cancellation**
- [ ] Every `except asyncio.CancelledError` re-raises (§5.2).
- [ ] No `except BaseException` around an `await` without an unconditional re-raise.
- [ ] No slow `await` inside `finally` or a cancellation handler.
- [ ] `uncancel()` appears only in scope-like framework code, if at all (§7.1).

**Timeouts**
- [ ] `asyncio.timeout` scopes, not per-call `wait_for` (§8.3).
- [ ] Deadlines propagate as absolute monotonic times, not durations (§8.4).
- [ ] No timeout below ~10 ms is treated as meaningful (§8.1).
- [ ] Somebody has asked "when this times out, does the work actually stop?" (§8.2).

**The loop**
- [ ] Loop lag is exported as a metric with an alert (§10.3).
- [ ] No blocking call in a coroutine — audited against §9.2's table.
- [ ] Password hashing, large `json.loads`, and DNS are on explicit executors (§12.2).
- [ ] `slow_callback_duration` can be enabled without a deploy.

**Bridges & shutdown**
- [ ] Only `call_soon_threadsafe` / `run_coroutine_threadsafe` cross thread boundaries (§11.4).
- [ ] No `.result()` on a concurrent future from the loop thread (§11.3).
- [ ] Shutdown drains with a **bounded** wait and logs what did not drain (§14.1).
- [ ] Signals go through `loop.add_signal_handler`.

---

## 17. What I could not verify

1. **The `gather` orphan's blast radius.** §4.1 measures that the sibling keeps running.
   I did **not** measure what that costs in a real service (held connections, duplicated
   writes) — that claim is reasoning, not data.

2. **§15's anyio backend timings** are single runs of a sleep-dominated workload on a
   loaded machine (`load1` 2.2–2.9). They demonstrate that both backends run; they are
   **not** a performance comparison and I make no such claim.

3. **uvloop.** Installed (0.22.1) but **not benchmarked here** — doc 28 §18 covers it, and
   I did not want to publish a second, weaker set of numbers for the same thing.

4. **The loop-lag monitor at production sampling rates.** §10.2 measured 1 ms sampling
   and found the effect below noise. I did **not** measure 50 ms sampling; the claim that
   it is "50× less work" is arithmetic from the sampling interval, not a measurement.

5. **§9.2's culprit table** is field knowledge plus the obvious source reading, not a
   per-row measurement. The rows I did measure are `time.sleep` (§9's 306 ms) and
   password-hashing-style CPU work (by construction). Treat the rest as a list to check,
   not a list of verified facts.

6. **Windows behaviour.** Everything here ran on macOS with the selector loop. Signal
   handling (§14.1), `add_signal_handler` availability, and timer granularity (§8.1)
   differ on Windows with `ProactorEventLoop`. I tested none of it.

7. **Whether `trio` truly forbids swallowing `Cancelled`.** My first attempt to
   demonstrate a `RuntimeError` on a swallowed `Cancelled` **failed** — the cancel scope
   absorbed it and execution continued normally. What I *can* show is the level-triggered
   behaviour (3/3 later checkpoints re-raised), which means a swallow cannot let you
   escape the scope. The stronger claim "trio raises if you swallow" is one I could not
   reproduce and do not make.

8. **The 1.07 ms lag floor and 1.2 ms timeout overshoot** are this machine, this OS, this
   loop. They are almost certainly different on Linux with `epoll` and on uvloop, neither
   of which I measured.

---

## 18. Lab exercises

1. **Find your queue depth.** Take a real producer/consumer in your codebase, measure
   throughput, and compute the `maxsize` that yields your target p99 via Little's Law
   (§2.1). Set it. Measure whether the prediction held.

2. **Reproduce the orphan (§4.1)** in your own service: make one child of a `gather` fail
   and log from the sibling afterwards. Then convert to `TaskGroup` and confirm the log
   stops.

3. **Audit every `create_task`.** Grep for it. For each, answer: who holds the reference,
   who observes the exception, and who cancels it at shutdown? Fix the ones with no
   answer.

4. **Build the swallow detector.** Write an `ast` check that flags
   `except (BaseException | asyncio.CancelledError)` blocks with no `raise` on every path,
   and any `await` inside a `finally`. Run it against a real codebase and report the
   count. (See [`42-runtime-code-manipulation.md`](42-runtime-code-manipulation.md).)

5. **Deploy the lag monitor (§10)** to a real service. Export p99 lag. Wait a week. What
   did it catch, and was it something you already knew about?

6. **Measure your `to_thread` break-even.** §12 found ~12–22 µs of overhead. Find the work
   duration at which offloading becomes a win for *your* workload, and check whether the
   calls you currently offload are above it.

7. **Prove the deadline propagates.** Build a three-service chain with a `contextvars`
   deadline (§8.4) and verify the total worst case equals the outermost budget rather than
   the sum.

8. **Port one module to anyio** and run its tests on both backends. Report what broke —
   that list is your codebase's dependence on asyncio-specific cancellation semantics.

9. **Settle §17.7.** Find the construction (if any) in trio 0.33 where swallowing a
   `Cancelled` raises rather than being absorbed. Read `trio/_core/_run.py`'s cancel-scope
   exit logic to decide whether the claim is true at all.

10. **Break the bridge.** Reproduce §11.3's deadlock, then attach `py-spy dump` to the
    frozen process and confirm you can identify it from the stacks alone.

---

## 19. Question bank

**Backpressure**
1. An unbounded queue and a 1,000-deep queue moved the same messages in the same wall time. What did the unbounded one actually cost, and how would you have predicted the number?
2. Derive the right `maxsize` from a p99 latency budget. What do you need to measure first?
3. `await asyncio.gather(*(fetch(u) for u in urls))` over 10,000 URLs. What is wrong, and what are two different fixes?

**Tasks and structure**
4. Is "always keep a reference to your task" true? State the precise rule for when a pending task is collected, and what keeps it alive otherwise.
5. A child of `gather` raises. Describe the state of its siblings one second later. Now answer for `TaskGroup`.
6. Why does `TaskGroup` raise `ExceptionGroup` when `gather` raised a plain exception? What information did `gather` discard?
7. What does `asyncio.shield` protect, and what does it abandon? Why is a shield with no external task owner a bug?

**Cancellation**
8. Is asyncio's cancellation edge- or level-triggered? Design an experiment to tell, and state what each outcome looks like.
9. Show three ways ordinary-looking code silently defeats a cancellation.
10. What are `Task.cancelling()` and `Task.uncancel()` for? What breaks without them, and why should application code leave them alone?
11. A timeout fires, your handler returns 504, and backend load does not drop. Explain.

**Operations**
12. Give the production signature of a blocked event loop in terms of p50 and p99, and explain the shape.
13. How do you measure event-loop lag from inside the loop, given the loop cannot run while blocked?
14. Why is `asyncio.to_thread` about 1.85× the cost of `run_in_executor`, and when do you want the more expensive one?
15. Your process has 11 CPUs. How big is the default executor, what formula produced it, and why does the answer differ inside a container?
16. Name the only two event-loop methods safe to call from another thread. Describe the deadlock you get from breaking that rule.
17. A `Task` gets a copy of the context; a bare `await` does not. Why is that asymmetry a footgun?
18. Design a graceful shutdown. Where does the bound go, and what do you log?

**Comparison**
19. trio has no `create_task`. What class of bug does that eliminate, and what does it cost?
20. asyncio and trio cancellation differ in one word. Which, and what does it let an asyncio task do that a trio task cannot?
21. When would you choose anyio over plain asyncio? When would you not?

---

## 20. Sources

**CPython source and docs**
- [`asyncio.to_thread`](https://github.com/python/cpython/blob/3.14/Lib/asyncio/threads.py) — nine lines; explains §12.1's cost difference entirely.
- [`concurrent/futures/thread.py`](https://github.com/python/cpython/blob/3.14/Lib/concurrent/futures/thread.py) — `min(32, (os.process_cpu_count() or 1) + 4)`. *Verdict: read the comment above it; the reasoning is honest about being a guess.*
- [`asyncio.taskgroups`](https://github.com/python/cpython/blob/3.14/Lib/asyncio/taskgroups.py) and [`asyncio.timeouts`](https://github.com/python/cpython/blob/3.14/Lib/asyncio/timeouts.py) — the `uncancel()` bookkeeping of §7, in about 200 lines total. *Verdict: the single best thing to read to understand asyncio cancellation.*
- [Developing with asyncio](https://docs.python.org/3/library/asyncio-dev.html) — debug mode, `slow_callback_duration`. *Verdict: short, and most of §9 is in it.*
- [`asyncio.Queue.shutdown`](https://docs.python.org/3/library/asyncio-queue.html) — 3.13+, the primitive §14.1 wanted for years.

**Structured concurrency**
- Nathaniel J. Smith, [*Notes on structured concurrency, or: Go statement considered harmful*](https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/) (2018). *Verdict: the essay that produced `TaskGroup`. If you read one thing from this list, read this. It is the argument §4 measures.*
- Nathaniel J. Smith, [*Timeouts and cancellation for humans*](https://vorpus.org/blog/timeouts-and-cancellation-for-humans/) (2018). *Verdict: the case for level-triggered cancellation and cancel scopes; §5's contrast is this essay's thesis.*
- [PEP 654 — Exception Groups and `except*`](https://peps.python.org/pep-0654/) — why §4.2 looks the way it does.
- [trio documentation: cancellation and timeouts](https://trio.readthedocs.io/en/stable/reference-core.html#cancellation-and-timeouts) — the normative description of level-triggered scopes.
- [anyio documentation](https://anyio.readthedocs.io/) — the portable API of §15.3.

**Tools** (versions resolved this session)
- `trio` 0.33.0, `anyio` 4.14.2, `uvloop` 0.22.1 — installed and used for §15.
- [`py-spy`](https://pypi.org/project/py-spy/) 0.4.2 — the only practical way to see inside a frozen loop (§11.3, §10.3).
- [`aiomonitor`](https://pypi.org/project/aiomonitor/) 0.7.1 — a REPL into a running loop; useful, last released 2024-11-11. **Not tested here.**

**Sibling docs**
- [`28-asyncio-internals.md`](28-asyncio-internals.md) — the mechanism behind every pattern here; §13 (cancellation), §14 (TaskGroup), §17 (debug mode), §18 (uvloop).
- [`30-concurrency-correctness.md`](30-concurrency-correctness.md) §13 — the 306 ms vs 7.6 ms measurement §9 rests on; §14 — why deadlines must be monotonic.
- [`31-measurement-methodology.md`](31-measurement-methodology.md) — read before believing §10.2, including the negative number I threw away.
- [`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md) — where CPU-bound work actually goes.

---

*Next: [`30-concurrency-correctness.md`](30-concurrency-correctness.md) — the same
territory without the event loop: what "atomic" means in Python, why the classic race
cannot be reproduced on a modern GIL build, and the taxonomy of everything that can go
wrong when two things run at once.*
