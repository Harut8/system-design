# 28 — asyncio internals: the coroutine, the loop, and the cancel

> **Tier 4, doc 28.** Prerequisites: [`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md)
> (code objects, `co_flags`, exception tables), [`20-eval-loop.md`](20-eval-loop.md)
> (frames, `_PyEval_EvalFrameDefault`, the eval breaker),
> [`09-syscalls-and-io.md`](09-syscalls-and-io.md) (blocking vs non-blocking,
> `epoll`/`kqueue`, level vs edge triggering), [`24-the-gil.md`](24-the-gil.md) §4–§7
> (check points, the convoy effect). Feeds into:
> [`29-async-patterns-and-pitfalls.md`](29-async-patterns-and-pitfalls.md),
> [`30-concurrency-correctness.md`](30-concurrency-correctness.md),
> [`46-production-python.md`](46-production-python.md).
>
> **THESIS: asyncio is not a concurrency primitive. It is a *scheduler written in
> Python* on top of two much older mechanisms — the generator's resumable frame and the
> kernel's readiness notification syscall — and every surprising thing about it follows
> from that sentence.** A coroutine is a generator with a flag set. `await` is
> `GET_AWAITABLE` + `SEND` + `YIELD_VALUE` + `RESUME`, four ordinary bytecodes.
> A `Task` is a ~120-line Python class that calls `coro.send(None)` in a loop. The event
> loop is a `deque`, a `heapq`, and one `select()` call per iteration. There is no magic
> anywhere in the stack — which is exactly why the failure modes (a blocked loop, a
> cancellation that never arrives, a `gather` that leaks a running task) are so
> unforgiving. **This document is about the mechanism. Doc 29 is about what to do with
> it.**
>
> **The one-line correction most people need:** one event loop is one thread. asyncio
> never gave you CPU parallelism, does not give it to you now, and on a free-threaded
> build the way you get it is *many loops in many threads* — not one loop going faster.
> See §18 and [`26-free-threading.md`](26-free-threading.md).

> **Verification provenance.** Unless marked otherwise, every class name, method name,
> constant, and code excerpt below was read from **this machine's stdlib**:
> `cpython-3.14.6-macos-aarch64-none/lib/python3.14/asyncio/*.py`, plus
> `include/python3.14/internal/pycore_*.h`, on an Apple M3 Pro running macOS.
> Disassembly was produced by `~/.local/bin/python3.14` (3.14.6, Clang 22.1.3) and is
> reproduced verbatim. Items marked *(verified)* were read from that tree or executed
> live during writing. Items marked *(sourced)* come from docs/PEPs/release notes and are
> cited in §23. **§19 lists, by name, the three claims I could not verify** — including
> one experiment that failed to reproduce a hazard the official docs warn about. Read
> that section; it is the honest part.
>
> **Version baseline:** Python 3.14.6 (current stable). 3.15 is in the release-candidate
> window at time of writing — the 3.15 docs build I fetched self-identified as
> `3.15.0b4`. asyncio's internals churn more than most of the stdlib; the *shapes* below
> are stable, the exact line numbers are not.

## Contents

1. [What asyncio actually is](#1-what-asyncio-actually-is)
2. [`async def` is one bit in `co_flags`](#2-async-def-is-one-bit-in-co_flags)
3. [`await`, disassembled](#3-await-disassembled)
4. [The frame that does not go away](#4-the-frame-that-does-not-go-away)
5. [The awaitable protocol underneath](#5-the-awaitable-protocol-underneath)
6. [Await depth costs on every suspend — measured](#6-await-depth-costs-on-every-suspend--measured)
7. [The event loop: one iteration, exactly](#7-the-event-loop-one-iteration-exactly)
8. [Handles, the ready deque, and the timer heap](#8-handles-the-ready-deque-and-the-timer-heap)
9. [The selector: kqueue, epoll, and the self-pipe](#9-the-selector-kqueue-epoll-and-the-self-pipe)
10. [The whole path: `await` down to the syscall](#10-the-whole-path-await-down-to-the-syscall)
11. [`Future`: a result slot with callbacks](#11-future-a-result-slot-with-callbacks)
12. [`Task`: the thing that drives a coroutine](#12-task-the-thing-that-drives-a-coroutine)
13. [Cancellation, the hardest part](#13-cancellation-the-hardest-part)
14. [Structured concurrency: `TaskGroup`, `ExceptionGroup`, `timeout`](#14-structured-concurrency-taskgroup-exceptiongroup-timeout)
15. [The eager task factory (3.12+)](#15-the-eager-task-factory-312)
16. [`asyncio.run`, `Runner`, and loop lifecycle](#16-asynciorun-runner-and-loop-lifecycle)
17. [Debug mode and slow-callback detection](#17-debug-mode-and-slow-callback-detection)
18. [uvloop, and the architectural reason it wins](#18-uvloop-and-the-architectural-reason-it-wins)
19. [asyncio vs threads vs free-threading](#19-asyncio-vs-threads-vs-free-threading)
20. [What I could not verify](#20-what-i-could-not-verify)
21. [Version deltas, 3.11 → 3.15](#21-version-deltas-311--315)
22. [Lab exercises](#22-lab-exercises)
23. [Question bank](#23-question-bank)
24. [Sources](#24-sources)

---

## 1. What asyncio actually is

Three layers, and people routinely confuse them:

| Layer | What it is | Where it lives |
|---|---|---|
| **The coroutine** | a language feature — a resumable function | `Objects/genobject.c`, `Python/bytecodes.c` |
| **The awaitable protocol** | a convention: `__await__` returns an iterator | `Lib/asyncio/futures.py`, PEP 492 |
| **The event loop** | a *library* that schedules coroutines against I/O readiness | `Lib/asyncio/base_events.py` |

Only the first is part of Python. The second is a two-method interface. The third is a
replaceable Python module — which is precisely why `uvloop` (§18), `trio`, and `anyio`
can exist at all, and why PEP 3156 spent most of its length specifying an *interface*
rather than an implementation.

The historical sequence matters because each layer arrived separately *(sourced)*:

| Year | Version | What landed |
|---|---|---|
| 2001 | 2.2 | PEP 255 generators — the resumable frame |
| 2005 | 2.5 | PEP 342 `send()`/`throw()` — generators become coroutines |
| 2009 | 3.3 | PEP 380 `yield from` — delegation, the direct ancestor of `await` |
| 2012 | 3.4 | **PEP 3156** — `asyncio` ("Tulip"), Guido van Rossum |
| 2015 | 3.5 | **PEP 492** — `async`/`await` syntax, Yury Selivanov |
| 2016 | 3.6 | PEP 525 async generators, PEP 530 async comprehensions |
| 2018 | 3.7 | PEP 567 `contextvars`; `asyncio.run()` |
| 2021 | 3.11 | **PEP 654** `ExceptionGroup`/`except*`; `TaskGroup`; `timeout()` |
| 2023 | 3.12 | eager task factory; `loop_factory` for `run()` |
| 2025 | 3.14 | free-threading support; per-thread task list; `python -m asyncio ps` |

**The load-bearing observation:** `await` was bolted onto a mechanism (generators)
designed for lazy iteration, ten years earlier, for a completely different purpose. That
is not a criticism — the reuse is elegant — but it explains why `StopIteration` shows up
in coroutine error messages, why `yield` inside `async def` means something entirely
different from `yield` inside `def`, and why the frame machinery in §4 looks the way it
does.

---

## 2. `async def` is one bit in `co_flags`

There is no "coroutine object type" in the way people imagine. The compiler sets a flag
on the code object, and the *function call* behaves differently as a result.

```python
>>> import inspect
>>> async def outer(): ...
>>> hex(outer.__code__.co_flags)
'0x83'
```

Decomposed *(verified, run on 3.14.6)*:

| Flag | Value | Set on `outer`? |
|---|---|---|
| `CO_OPTIMIZED` | `0x01` | ✓ |
| `CO_NEWLOCALS` | `0x02` | ✓ |
| `CO_GENERATOR` | `0x20` | ✗ |
| **`CO_COROUTINE`** | **`0x80`** | **✓** |
| `CO_ITERABLE_COROUTINE` | `0x100` | ✗ |
| `CO_ASYNC_GENERATOR` | `0x200` | ✗ |

Four states, one flag pair:

```
  CO_GENERATOR only            →  generator        (def with yield)
  CO_COROUTINE only            →  coroutine        (async def)
  CO_GENERATOR|CO_COROUTINE    →  CO_ITERABLE_COROUTINE via @types.coroutine
                                  (a generator that may be awaited — the 3.4-era
                                   compatibility bridge; asyncio no longer supports
                                   bare generator-based coroutines)
  CO_ASYNC_GENERATOR           →  async generator  (async def with yield, PEP 525)
```

`CO_ITERABLE_COROUTINE` is the interesting one: it is how `@types.coroutine` makes a
plain generator awaitable. It is *purely* a flag flip on an existing code object — the
decorator does not wrap anything. That is the strongest available evidence for the claim
in §1 that a coroutine is a generator wearing a different hat.

**What the flag changes.** At function-call time, `CO_COROUTINE` in `co_flags` makes the
compiler emit `RETURN_GENERATOR` as the *first* instruction of the function body (§3), so
calling the function builds an object and returns immediately instead of executing the
body. That is the entire mechanism behind "calling a coroutine function doesn't run it."

---

## 3. `await`, disassembled

This is the part everyone hand-waves. Here is the real thing.

```python
async def inner():
    return 1

async def outer():
    x = await inner()
    return x
```

`dis.dis(outer)` on **CPython 3.14.6** *(verified — reproduced verbatim, only the
`async with`/`async for` sections of the original test function trimmed)*:

```
   6            RETURN_GENERATOR
                POP_TOP
        L1:     RESUME                   0

   7            LOAD_GLOBAL              1 (inner + NULL)
                CALL                     0
                GET_AWAITABLE            0
                LOAD_CONST               0 (None)
        L2:     SEND                     3 (to L5)
        L3:     YIELD_VALUE              1
        L4:     RESUME                   3
                JUMP_BACKWARD_NO_INTERRUPT 5 (to L2)
        L5:     END_SEND
                STORE_FAST               0 (x)

  12            LOAD_FAST                0 (x)
                RETURN_VALUE

   7   L23:     CLEANUP_THROW
       L24:     JUMP_BACKWARD_NO_INTERRUPT 70 (to L5)

  --   L43:     CALL_INTRINSIC_1         3 (INTRINSIC_STOPITERATION_ERROR)
                RERAISE                  1
```

Instruction by instruction:

| Opcode | What it does |
|---|---|
| **`RETURN_GENERATOR`** | Allocates the coroutine object, **copies the current frame into it**, sets `owner = FRAME_OWNED_BY_GENERATOR`, pushes the object, returns to the caller. This is the whole of "calling a coroutine function doesn't run it." |
| `POP_TOP` | Discards the value sent into the first `send(None)` — always `None`. |
| **`RESUME 0`** | Function-entry check point. `oparg 0` = `RESUME_AT_FUNC_START`. |
| `GET_AWAITABLE 0` | Turns the operand into an iterator: returns it unchanged if it is a coroutine or a `CO_ITERABLE_COROUTINE` generator, otherwise calls `__await__`. The oparg encodes *why*: `0` = a plain `await`, `1` = after `__aenter__`, `2` = after `__aexit__` — used only for error messages *(sourced: `dis` docs)*. |
| **`SEND 3`** | Pushes the value into the awaited object and resumes it. Jump target `L5` on `StopIteration`. |
| **`YIELD_VALUE 1`** | Suspends *this* frame and hands the value up. `oparg 1` sets `gi_frame_state = FRAME_SUSPENDED_YIELD_FROM`. |
| **`RESUME 3`** | Resume after await. `oparg 3` = `RESUME_AFTER_AWAIT`. |
| `JUMP_BACKWARD_NO_INTERRUPT` | Loops back to `SEND`. **Deliberately not an eval-breaker check point** — see the box below. |
| `END_SEND` | Cleans the receiver off the stack, leaving the result. |
| `CLEANUP_THROW` | Where `coro.throw()` lands (§13). Re-raises anything that isn't `StopIteration`. |
| `CALL_INTRINSIC_1 INTRINSIC_STOPITERATION_ERROR` | Converts a `StopIteration` that escaped the coroutine body into a `RuntimeError`. PEP 479's rule, enforced in bytecode. |

> **The `SEND`/`YIELD_VALUE`/`RESUME` loop *is* `yield from`.** This is not analogous to
> PEP 380 delegation — it is literally the same three-instruction idiom the compiler
> emits for `yield from`, with `GET_AWAITABLE` in front to enforce awaitability. The
> `await` keyword is `yield from` with a type check.

> **A check-point detail that connects straight to [`24-the-gil.md`](24-the-gil.md) §4.**
> `RESUME` decomposes into `_LOAD_BYTECODE + _MAYBE_INSTRUMENT + _QUICKEN_RESUME +
> _CHECK_PERIODIC_IF_NOT_YIELD_FROM`, and that last micro-op reads *(verified, from
> `Python/bytecodes.c` on the `3.14` branch)*:
>
> ```c
> op(_CHECK_PERIODIC_IF_NOT_YIELD_FROM, (--)) {
>     if ((oparg & RESUME_OPARG_LOCATION_MASK) < RESUME_AFTER_YIELD_FROM) {
>         ...
>         if (_Py_atomic_load_uintptr_relaxed(&tstate->eval_breaker) & _PY_EVAL_EVENTS_MASK) {
>             int err = _Py_HandlePending(tstate);
> ```
>
> With `RESUME_AFTER_YIELD_FROM == 2` and `RESUME_AFTER_AWAIT == 3` *(verified, from
> `pycore_opcode_utils.h`)*, the test `3 < 2` is false: **resuming from an `await` does
> not check the eval breaker.** Neither does the `JUMP_BACKWARD_NO_INTERRUPT` that
> follows it. Both are correct — the outermost frame that *drove* the resume already
> passed a check point — and both mean that the GIL-yield / signal-delivery points inside
> an await chain are sparser than the raw instruction count suggests. Depth-N await
> chains do not add N check points.

---

## 4. The frame that does not go away

The single most important internal fact about coroutines, and the one that explains why
suspension is cheap:

**A coroutine's frame is embedded *inside* the coroutine object.** Not referenced by it —
embedded, as a struct member. From `Include/internal/pycore_interpframe_structs.h`
*(verified, read from this machine's headers)*:

```c
#define _PyGenObject_HEAD(prefix)                                           \
    PyObject_HEAD                                                           \
    PyObject *prefix##_weakreflist;                                         \
    PyObject *prefix##_name;                                                \
    PyObject *prefix##_qualname;                                            \
    _PyErr_StackItem prefix##_exc_state;                                    \
    PyObject *prefix##_origin_or_finalizer;                                 \
    char prefix##_hooks_inited;                                             \
    char prefix##_closed;                                                   \
    char prefix##_running_async;                                            \
    int8_t prefix##_frame_state;                                            \
    _PyInterpreterFrame prefix##_iframe;      /* ← the frame, inline */

struct _PyCoroObject { _PyGenObject_HEAD(cr) };
```

and the inverse lookup, which is pure pointer arithmetic *(verified,
`pycore_genobject.h`)*:

```c
static inline PyGenObject *_PyGen_GetGeneratorFromFrame(_PyInterpreterFrame *frame)
{
    assert(frame->owner == FRAME_OWNED_BY_GENERATOR);
    return (PyGenObject *)(((char *)frame) - offsetof(PyGenObject, gi_iframe));
}
```

Consequences that fall straight out:

1. **Suspension allocates nothing.** The frame is already off the C stack, in
   heap-allocated storage owned by the coroutine. Suspending is: bump `instr_ptr`, write
   `cr_frame_state`, unlink from `tstate->current_frame`, return. There is no
   "save the frame somewhere" step — there is nowhere else for it to be.
2. **`await` does not recurse into the C eval loop.** `SEND`'s fast path ends in
   `DISPATCH_INLINED(gen_frame)` *(verified, `bytecodes.c`)* — it links the awaited
   coroutine's embedded frame as a child of the current one and keeps going *in the same
   `_PyEval_EvalFrameDefault` invocation*. Deep await chains do not consume C stack.
   Compare a naive coroutine library built on real function calls, which would.
3. **The frame state is a tiny enum,** and it is what `cr_suspended`, `cr_running`, and
   `inspect.getcoroutinestate()` read *(verified, `pycore_frame.h`)*:

```c
FRAME_CREATED             = -3
FRAME_SUSPENDED           = -2      /* plain yield */
FRAME_SUSPENDED_YIELD_FROM = -1     /* suspended inside await / yield from */
FRAME_EXECUTING           =  0
FRAME_COMPLETED           =  1
FRAME_CLEARED             =  4
```

`YIELD_VALUE`'s oparg picks between the two suspended states with
`gen->gi_frame_state = FRAME_SUSPENDED + oparg` — which is why the header asserts
`FRAME_SUSPENDED_YIELD_FROM == FRAME_SUSPENDED + 1`. That is a genuine
`assert`-enforced layout dependency in CPython, not a coincidence.

The Python-visible surface *(verified, live on 3.14.6)*:

```python
>>> [a for a in dir(coro) if a.startswith('cr_')]
['cr_await', 'cr_code', 'cr_frame', 'cr_origin', 'cr_running', 'cr_suspended']
```

`cr_await` is the object this coroutine is currently blocked on — walking it gives you
the await chain, and it is exactly what `python -m asyncio pstree` (§21) renders.

> **Cross-ref.** [`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md)
> covers `co_stacksize` and localsplus; note that a coroutine object's size therefore
> scales with its function's stack and local requirements. Ten thousand suspended
> coroutines is ten thousand live frames' worth of localsplus, not ten thousand small
> objects. This is the real memory model of a high-connection-count async server, and
> [`16-object-memory-layout.md`](16-object-memory-layout.md)'s "count the objects"
> discipline applies directly.

---

## 5. The awaitable protocol underneath

Strip asyncio away entirely. The protocol is: **`__await__` returns an iterator; the
driver calls `send()` on it; each `yield` hands a value to the driver; `StopIteration`
carries the return value.** That is all.

Driven by hand, no event loop *(verified, executed live)*:

```python
class Awaitable:
    def __await__(self):
        r = yield "PAYLOAD"          # hand PAYLOAD to whoever is driving us
        return r * 2                 # what `await` evaluates to

async def demo():
    v = await Awaitable()
    return v + 1

c = demo()
c.send(None)          # → 'PAYLOAD'          (runs to the yield)
c.cr_await            # → <generator Awaitable.__await__>
c.cr_suspended        # → True
c.send(10)            # → raises StopIteration(21)
```

Three things to internalize from those five lines:

- **`send(None)` starts it; `send(v)` resumes it with a value.** `Task` uses exactly
  this and nothing more (§12).
- **The return value travels in an exception.** `StopIteration.value` is the coroutine's
  result. If your coroutine body lets a `StopIteration` escape for any other reason, the
  interpreter cannot tell the difference — hence PEP 479 and the
  `INTRINSIC_STOPITERATION_ERROR` in §3.
- **The yielded value is a message to the scheduler.** asyncio's convention is that a
  coroutine may only ever yield a `Future` (or bare `None`). Yield anything else and
  `Task.__step` raises `RuntimeError: Task got bad yield: ...` *(verified, in
  `tasks.py`)*. The protocol is generic; asyncio's *use* of it is not.

The three driver-side methods:

| Method | Effect at the suspension point |
|---|---|
| `coro.send(v)` | Resume; `await` evaluates to `v`. |
| `coro.throw(exc)` | Resume by **raising `exc` at the await expression**. This is the entire cancellation mechanism (§13). Lands on `CLEANUP_THROW` in the delegating frame. |
| `coro.close()` | Throws `GeneratorExit`; runs `finally` blocks; refuses to let the coroutine yield again. |

Verified live: `throw(CancelledError())` propagates out of the `await`, and `close()`
runs the coroutine's `finally` block *(verified)*.

**`Future.__await__` is the whole asyncio-side protocol, and it is six lines**
*(verified, `Lib/asyncio/futures.py`)*:

```python
def __await__(self):
    if not self.done():
        self._asyncio_future_blocking = True
        yield self                       # ← tells Task to wait for completion
    if not self.done():
        raise RuntimeError("await wasn't used with future")
    return self.result()                 # may raise

__iter__ = __await__                     # compatible with `yield from`
```

**A Future yields *itself*.** That is the message. `_asyncio_future_blocking` is a
three-valued flag doing double duty *(verified, from its own comment)*: its *presence*
marks a class as Future-compatible for duck-typing, and its *value* lets `Task.__step`
distinguish `await fut` (correct — the flag is `True`) from `yield fut` (wrong — still
`False`, and you get the `yield was used instead of yield from` error).

---

## 6. Await depth costs on every suspend — measured

The `SEND`/`YIELD_VALUE` loop in §3 has a consequence people miss: **an await chain is
re-walked on every suspension and every resumption.** Going out, each frame in the chain
executes one `YIELD_VALUE`. Coming back in, each executes one `SEND`.

Measured with `sys.monitoring`, counting `PY_RESUME` and `PY_YIELD` events across one
suspension at varying await depth — a single instant run, no benchmarking
*(verified, measured on 3.14.6)*:

```python
class Once:
    def __await__(self):
        yield              # suspends exactly once

async def d0(): await Once()
async def d1(): await d0()
async def d2(): await d1()
async def d3(): await d2()
```

| await chain depth | `PY_RESUME` | `PY_YIELD` |
|---|---|---|
| 1 | 3 | 3 |
| 2 | 4 | 4 |
| 3 | 5 | 5 |
| 4 | 6 | 6 |

Exactly +1 resume and +1 yield per level of nesting, per suspension. (The constant offset
of 2 is the `Task`'s own coroutine plus the harness.)

**Why this matters in production.** A framework that layers eight coroutine wrappers
between your handler and the socket — middleware, tracing, retry, auth, serialization —
pays 8 frame transitions on the way out and 8 on the way back **for every single
suspension**, and a request that awaits 20 times pays it 20 times. This is the mechanism
behind "our async framework has a lot of overhead"; it is not mysterious and it is not
the event loop. Flatten the chain, or accept the cost knowingly.

It is also why `await` on an already-completed `Future` is *not* free: it still runs
`GET_AWAITABLE`, `SEND`, and `END_SEND`. It just doesn't suspend. (§15's eager task
factory attacks exactly this.)

---

## 7. The event loop: one iteration, exactly

`BaseEventLoop.run_forever` is eleven lines *(verified,
`Lib/asyncio/base_events.py`)*:

```python
def run_forever(self):
    self._run_forever_setup()
    try:
        while True:
            self._run_once()
            if self._stopping:
                break
    finally:
        self._run_forever_cleanup()
```

Everything is in `_run_once`. Here is one iteration, in the order it actually happens:

```
┌────────────────────────────────────────────────────────────────────────────────┐
│  BaseEventLoop._run_once()   —  ONE ITERATION                                  │
└────────────────────────────────────────────────────────────────────────────────┘

 ① PRUNE THE TIMER HEAP
    if len(_scheduled) > 100  and  cancelled/total > 0.5:
         rebuild the list, drop cancelled, heapq.heapify()      ← O(n), amortized
    else:
         pop cancelled entries off the HEAD only                ← O(log n) each
    ────────────────────────────────────────────────────────────────────────────
    why: TimerHandle.cancel() only sets a flag. heapq cannot delete from the
    middle. Without this, a workload that schedules and cancels timeouts (i.e.
    every real server) grows _scheduled without bound.
                                    │
                                    ▼
 ② DECIDE THE SELECT TIMEOUT
    if _ready or _stopping:            timeout = 0        ← non-blocking poll
    elif _scheduled:                   timeout = _scheduled[0]._when - time()
                                       clamped to [0, MAXIMUM_SELECT_TIMEOUT]
                                                          ← 24*3600 seconds
    else:                              timeout = None     ← block indefinitely
                                    │
                                    ▼
 ③ THE ONLY BLOCKING CALL IN THE ENTIRE PROCESS
    event_list = self._selector.select(timeout)
    ──────────────────────────────────────────────────────  kqueue() on macOS
    This is where the process sleeps. Every microsecond spent               ↑
    NOT here is a microsecond of latency added to every pending I/O.    doc 09
                                    │
                                    ▼
 ④ TRANSLATE READINESS → CALLBACKS
    self._process_events(event_list)
    for each (key, mask):  reader/writer Handle  →  _ready.append(handle)
    (a cancelled reader is unregistered from the selector here, lazily)
                                    │
                                    ▼
 ⑤ EXPIRE TIMERS
    end_time = time() + _clock_resolution        ← fire slightly EARLY, never late
    while _scheduled and _scheduled[0]._when < end_time:
        heappop  →  _ready.append(handle)
                                    │
                                    ▼
 ⑥ RUN CALLBACKS — the only place callbacks are ever called
    ntodo = len(_ready)               ← SNAPSHOT. This is the fairness mechanism.
    for i in range(ntodo):
        handle = _ready.popleft()
        if handle._cancelled: continue
        handle._run()                 ← runs ONE callback to completion,
                                        i.e. drives ONE Task through ONE
                                        step of its coroutine
    ──────────────────────────────────────────────────────────────────────────
    Callbacks appended DURING this loop are NOT run this iteration. They wait
    for the next poll. That is why `await asyncio.sleep(0)` costs exactly one
    full iteration, including one select(0) syscall.
                                    │
                                    ▼
                        back to ①, forever
```

Five things a staff-level answer gets right about that diagram:

**1. The selector is polled on *every* iteration, even when there is ready work.** With
`_ready` non-empty the timeout is `0`, so it is a non-blocking poll rather than a sleep —
but it is still a syscall. A loop under heavy CPU-ish load makes one `kevent`/`epoll_wait`
per iteration for nothing. This is a real, if usually small, floor cost, and one of the
things `uvloop` shaves (§18).

**2. `handle._run()` runs to completion. There is no preemption.** The loop cannot
interrupt a callback. A callback that blocks — `time.sleep`, `requests.get`, a
`json.dumps` of 200 MB, a `bcrypt` round — stops the entire loop, including timer
expiry, including I/O dispatch, including other tasks' cancellations. **This is the
single most common asyncio production failure**, and §17 is how you detect it.

**3. The `ntodo` snapshot is deliberate.** Without it, a task that reschedules itself
via `call_soon` in a tight loop would starve I/O forever — the ready deque would never
drain. With it, the loop guarantees a selector poll between rounds. The cost is that
`sleep(0)` is a full iteration, not a cheap yield.

**4. Timers fire *early*, within one clock tick.** `end_time = self.time() +
self._clock_resolution`, and on this machine `_clock_resolution` is
`4.1667e-08` seconds (`mach_absolute_time()`) *(verified)*. The design chose "never
late by a rounding error" over "never early". Anything that computes deadlines by
comparing `loop.time()` to a timer's `_when` needs to know that.

**5. `loop.time()` is `time.monotonic()`** *(verified)* — not wall clock. Timers are
immune to NTP steps and DST. `call_at` takes loop time, not epoch time, and mixing the
two is a classic bug.

---

## 8. Handles, the ready deque, and the timer heap

Two data structures hold everything the loop will ever do:

```python
self._ready     = collections.deque()   # callbacks to run ASAP, FIFO
self._scheduled = []                    # heapq of TimerHandle, ordered by _when
```

`call_soon` is four lines of real work *(verified)*:

```python
def _call_soon(self, callback, args, context):
    handle = events.Handle(callback, args, self, context)
    self._ready.append(handle)
    return handle
```

`call_at` is the heap version:

```python
timer = events.TimerHandle(when, callback, args, self, context)
heapq.heappush(self._scheduled, timer)
timer._scheduled = True
```

and `call_later(delay, ...)` is literally `call_at(self.time() + delay, ...)`
*(verified)* — which means **`call_later` resolves the deadline at scheduling time**.
Two `call_later(1.0, ...)` calls made 300 ms apart fire 300 ms apart, not together.

### `Handle` — the unit of work

```python
class Handle:
    __slots__ = ('_callback', '_args', '_cancelled', '_loop',
                 '_source_traceback', '_repr', '__weakref__', '_context')
```

*(verified.)* Note `__slots__` — [`16-object-memory-layout.md`](16-object-memory-layout.md)
§9's optimization, applied where it matters: a busy loop allocates a `Handle` per
callback per iteration.

`Handle._run` is where context propagation happens:

```python
def _run(self):
    try:
        self._context.run(self._callback, *self._args)
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:
        ... self._loop.call_exception_handler({...})
```

Three mechanisms in six lines:

- **`self._context.run(...)`** — every callback executes inside a `contextvars.Context`
  captured at scheduling time (PEP 567). This is why a `ContextVar` set inside a task is
  invisible to its parent, and why `Task` copies the context in `__init__`
  (`self._context = contextvars.copy_context()`) *(verified)*.
- **`SystemExit` and `KeyboardInterrupt` are re-raised**, escaping `_run_once` and
  therefore `run_forever`. Every other exception is swallowed into the loop's exception
  handler. That asymmetry is deliberate and is why a bug in a bare `call_soon` callback
  logs rather than crashes.
- **`self = None` at the end** (in the source, one line below) — an explicit refcycle
  break. You will see this idiom five times in `tasks.py`; it is
  [`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md) §6's "unpublish
  before you release" applied to Python-level frames.

### `TimerHandle` — and the cancellation leak it creates

```python
class TimerHandle(Handle):
    __slots__ = ['_scheduled', '_when']
    def __lt__(self, other): return self._when < other._when
```

`__lt__` exists purely so `heapq` can order them *(verified)*.

**`TimerHandle.cancel()` cannot remove the entry from the heap.** `heapq` has no delete.
So cancel sets `_cancelled = True`, drops `_callback` and `_args` (so the closure is
released), and bumps the loop's `_timer_cancelled_count`. The entry stays. Phase ① of
`_run_once` is the garbage collector for that, with the constants
*(verified, `base_events.py`)*:

```python
_MIN_SCHEDULED_TIMER_HANDLES = 100
_MIN_CANCELLED_TIMER_HANDLES_FRACTION = 0.5
```

— rebuild the heap only when there are more than 100 timers *and* more than half of them
are cancelled. Below either threshold it only pops dead entries off the head.

This is a real, load-bearing heuristic. Every `asyncio.timeout()` and `wait_for` that
*doesn't* fire leaves a cancelled `TimerHandle` behind. A server doing 10k req/s with a
timeout per request creates 10k dead heap entries per second; the 50% rule is what keeps
`_scheduled` bounded. When someone reports "asyncio memory grows under load with lots of
short timeouts", this is the first structure to look at.

### `call_soon_threadsafe` — the only thread-safe method

```python
def call_soon_threadsafe(self, callback, *args, context=None):
    handle = events._ThreadSafeHandle(callback, args, self, context)
    self._ready.append(handle)
    self._write_to_self()          # ← wake the selector
    return handle
```

*(verified.)* Two differences from `call_soon`: a `_ThreadSafeHandle` (an `RLock`-guarded
subclass, added for free-threading — §19), and `_write_to_self()`, which is §9.

**Everything else on the loop is thread-unsafe.** `call_soon`, `create_task`,
`Future.set_result` — all of them assume they are being called from the loop's thread.
In debug mode `_check_thread()` enforces it; in production mode it does not, and the
resulting corruption is silent. `call_soon_threadsafe` and
`run_coroutine_threadsafe` are the entire supported cross-thread surface.

---

## 9. The selector: kqueue, epoll, and the self-pipe

asyncio does not talk to the kernel directly. It uses the `selectors` module, which picks
the best available mechanism at import time.

On this machine *(verified)*:

```python
>>> selectors.DefaultSelector
<class 'selectors.KqueueSelector'>
>>> type(asyncio.new_event_loop()._selector).__name__
'KqueueSelector'
```

| Platform | `DefaultSelector` | Underlying syscall |
|---|---|---|
| Linux | `EpollSelector` | `epoll_wait` |
| macOS / BSD | `KqueueSelector` | `kevent` |
| Windows (default loop) | — | IOCP, via `ProactorEventLoop` (a *completion*, not readiness, model) |
| fallback | `SelectSelector` | `select` — O(n) per call, FD_SETSIZE-limited |

> **Readiness vs completion.** `epoll`/`kqueue` tell you *"this fd will not block now"*;
> you then perform the `recv` yourself. IOCP and `io_uring` tell you *"the read you asked
> for has finished, here is the data"*. `BaseSelectorEventLoop` is built on the first
> model, `ProactorEventLoop` on the second — which is why Windows asyncio is a separate
> loop class rather than a different selector. See
> [`09-syscalls-and-io.md`](09-syscalls-and-io.md); it is also why there is still no
> `io_uring` event loop in the stdlib.

**Level-triggered, not edge-triggered.** `selectors` registers in level-triggered mode.
That is the forgiving choice: a partial read leaves the fd readable, so the loop will
call you again next iteration. Edge-triggered would require draining to `EAGAIN` on every
callback and would turn a missed byte into a permanently stalled connection. Doc 09
covers the trade; asyncio's answer is "correctness over syscall count."

Registration is the `_add_reader`/`_add_writer` pair *(verified,
`selector_events.py`)*: one `SelectorKey` per fd, whose `data` is the tuple
`(reader_handle, writer_handle)`, and readiness is dispatched by
`_process_events` mapping `EVENT_READ`/`EVENT_WRITE` back onto those handles.

### The self-pipe

Look at a brand-new loop *(verified, live)*:

```python
>>> l = asyncio.new_event_loop()
>>> l._internal_fds
1
>>> dict(l._selector.get_map())
{4: SelectorKey(fileobj=4, fd=4, events=1,
                data=(<Handle BaseSelectorEventLoop._read_from_self()>, None))}
```

A loop with zero user sockets is already watching one fd. `_make_self_pipe` creates a
`socket.socketpair()` and registers the read end *(verified)*. It exists to solve one
problem: **the loop is asleep inside `select(timeout)`, and something outside that
`select` needs it to wake up now.**

Three callers rely on it:

- `call_soon_threadsafe` — another thread queued work.
- `add_signal_handler` — the C-level signal handler cannot run Python; it writes a byte.
  This is `signal.set_wakeup_fd()`, and it is the reason asyncio handles `SIGINT` while
  blocked in `select` at all. ([`24-the-gil.md`](24-the-gil.md) §8 has the eval-breaker
  version of the same problem.)
- `Runner._on_sigint` — after cancelling the main task, it calls
  `self._loop.call_soon_threadsafe(lambda: None)` with the comment *"wakeup loop if it is
  blocked by select() with long timeout"* *(verified, `runners.py`)*.

The self-pipe is the classic UNIX solution to "unify signals and threads with the
readiness model" — you convert the out-of-band event into an fd becoming readable,
because that is the only thing your event loop knows how to wait for. It is worth
recognizing on sight; every event-driven system has one.

---

## 10. The whole path: `await` down to the syscall

Here is the complete round trip for `data = await reader.read(100)`, from Python
statement to `kevent` and back. This is the diagram to be able to draw from memory.

```
  YOUR COROUTINE                    TASK                 EVENT LOOP           KERNEL
  ══════════════                    ════                 ══════════           ══════

  data = await reader.read(100)
        │
        │ GET_AWAITABLE
        │ SEND 3          ──────────────────────────────────────┐
        │                                                       │  (frames pushed
        │                        ...inside the stream layer:    │   inline — no C
        │                        fut = loop.create_future()     │   recursion, §4)
        │                        loop._add_reader(fd, cb) ──────┼──▶ selector.register(
        │                                                       │       fd, EVENT_READ)
        │                        await fut                      │
        │                          → Future.__await__:          │
        │                              _asyncio_future_blocking = True
        │                              yield self  ─────────────┘
        │ YIELD_VALUE 1  ◀── the Future travels UP the whole await chain,
        │                    one YIELD_VALUE per level (§6)
        ▼
   ┌────────────────────────────────────────────┐
   │ Task.__step_run_and_handle_result:         │
   │   result = coro.send(None)  → the Future   │
   │   blocking = result._asyncio_future_blocking│
   │   result._asyncio_future_blocking = False  │  ← consume the flag
   │   result.add_done_callback(self.__wakeup)  │  ← ★ the reattachment
   │   self._fut_waiter = result                │  ← ★ the cancel handle
   │   return  ← the callback ENDS. The Task is now off the ready queue          │
   └────────────────────────────────────────────┘   entirely. Nothing references
        │                                            it except the Future's
        │                                            callback list.
        ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │ _run_once():  _ready is empty  →  timeout = next timer or None           │
   │               self._selector.select(timeout)  ─────────────────────────▶ │ kevent()
   │                                                                          │  ...
   │               ◀───────────────────────────────  fd 7 is READABLE ────────│  sleeps
   │               _process_events → _ready.append(reader_handle)             │
   │               handle._run() → the stream's callback:                     │
   │                    data = sock.recv(...)        ────────────────────────▶│ recv()
   │                    fut.set_result(data)                                  │
   │                      └─▶ Future.__schedule_callbacks():                  │
   │                            loop.call_soon(Task.__wakeup, fut)            │
   └──────────────────────────────────────────────────────────────────────────┘
        │
        │   NEXT iteration of _run_once (the ntodo snapshot, §7 ⑥)
        ▼
   ┌────────────────────────────────────────────┐
   │ Task.__wakeup(future):                     │
   │   future.result()   → raises? → __step(exc)│
   │                     → ok?     → __step()   │
   │      └─▶ coro.send(None) ──────────────────┼──┐
   └────────────────────────────────────────────┘  │
        │                                          │  SEND re-walks the chain
        ▼                                          │  DOWN, one level per frame
  RESUME 3  ◀────────────────────────────────────────┘
  END_SEND
  STORE_FAST data          ← your line continues, on the same frame, with the
                             same locals, in the same thread it started in.
```

**Read the two starred lines together — they are the crux of the whole design.**

```python
result.add_done_callback(self.__wakeup, context=self._context)
self._fut_waiter = result
```

`add_done_callback` is *how the task gets resumed*. `_fut_waiter` is *how the task gets
cancelled* (§13) — it is the only handle the Task keeps on what it is currently blocked
on. Every asyncio behaviour worth understanding is one of those two edges being followed.

And note what is **not** in the diagram: any timer, any polling of the coroutine, any
thread. Between `yield self` and `__wakeup`, the coroutine costs exactly one entry in one
Future's callback list. That is why 100k idle connections is cheap and 100k *active* ones
is not.

---

## 11. `Future`: a result slot with callbacks

`asyncio.Future` is smaller than people expect: a state, a result-or-exception, a list of
callbacks, and a loop reference.

```python
_state    = _PENDING          # → _CANCELLED | _FINISHED, one-way
_result   = None
_exception = None
_loop     = None
_callbacks = []               # [(callback, context), ...]
```

The state machine has exactly three states and no way back:

```
                    set_result(v)  ─────▶  FINISHED (result)
                   /
    PENDING ──────┼── set_exception(e) ─▶  FINISHED (exception)
                   \
                    cancel()       ─────▶  CANCELLED
```

`set_result` and `set_exception` raise `InvalidStateError` if the future is not
`PENDING` *(verified)*. `cancel()` on a non-pending future returns `False` rather than
raising. That asymmetry — "setting twice is a bug, cancelling twice is not" — shows up
throughout the cancellation design.

Every transition ends in `__schedule_callbacks` *(verified)*:

```python
def __schedule_callbacks(self):
    callbacks = self._callbacks[:]
    if not callbacks: return
    self._callbacks[:] = []
    for callback, ctx in callbacks:
        self._loop.call_soon(callback, self, context=ctx)
```

**Callbacks are never called synchronously.** They are always routed through
`call_soon`, i.e. deferred to the next iteration's phase ⑥. This is a deliberate
re-entrancy guarantee: `fut.set_result(x)` cannot run arbitrary user code inside your
current callback and corrupt your invariants. It is also why "set a result" and "the
awaiting task resumes" are separated by at least one loop iteration.

### "Task exception was never retrieved"

The warning everyone has seen, mechanized *(verified, `futures.py`)*:

```python
__log_traceback = False

def set_exception(self, exception):
    ...
    self.__log_traceback = True      # armed

def result(self):
    self.__log_traceback = False     # disarmed
def exception(self):
    self.__log_traceback = False     # disarmed
def cancel(self, msg=None):
    self.__log_traceback = False     # disarmed

def __del__(self):
    if not self.__log_traceback:
        return
    self._loop.call_exception_handler({
        'message': f'{self.__class__.__name__} exception was never retrieved',
        'exception': self._exception, 'future': self})
```

A one-bit flag, armed by `set_exception` and disarmed by anything that observes the
result, checked in `__del__`. Reproduced live *(verified)*:

```
Task exception was never retrieved
future: <Task finished name='Task-2' coro=<boom()> exception=ValueError('orphaned')>
```

Three consequences:

- **It fires at collection time, not failure time.** The traceback in the log points at
  the coroutine, but the *timing* is whenever the GC got round to it — possibly much
  later, possibly at interpreter shutdown, possibly (with a reference cycle) never. If
  you have ever seen this message appear out of order in your logs, that's why.
- **It is routed through `loop.call_exception_handler`, not `warnings`.** So
  `-W error` will not turn it into a failure, and a custom exception handler
  (`loop.set_exception_handler`) can capture it — which is what you want in production.
  Wiring that to your error tracker is the highest-value five lines in an async service.
- **It is the *only* signal that a fire-and-forget task died.** `asyncio.create_task(f())`
  with the result discarded has no other failure path. This single fact is most of why
  `TaskGroup` exists (§14).

`Future.set_exception` also refuses `StopIteration` outright, replacing it with a
`RuntimeError` *(verified)* — the same PEP 479 hazard as §3, in the library layer.

---

## 12. `Task`: the thing that drives a coroutine

**A `Future` is a value that will arrive. A `Task` is a `Future` that arranges its own
arrival by repeatedly calling `send()` on a coroutine.** That is the whole difference.

```python
class Task(futures._PyFuture):
    def __init__(self, coro, *, loop=None, name=None, context=None, eager_start=False):
        super().__init__(loop=loop)
        self._num_cancels_requested = 0
        self._must_cancel = False
        self._fut_waiter = None
        self._coro = coro
        self._context = context or contextvars.copy_context()
        if eager_start and self._loop.is_running():
            self.__eager_start()
        else:
            self._loop.call_soon(self.__step, context=self._context)
            _py_register_task(self)
```

*(verified.)* Note the last three lines: **creating a task schedules exactly one
callback.** `create_task` does not run anything (unless the eager factory is installed —
§15).

### `__step` — the engine

`Task.__step` splits into a wrapper and a body *(verified)*. The wrapper handles the
current-task bookkeeping:

```python
def __step(self, exc=None):
    if self.done(): raise exceptions.InvalidStateError(...)
    if self._must_cancel:
        if not isinstance(exc, exceptions.CancelledError):
            exc = self._make_cancelled_error()
        self._must_cancel = False
    self._fut_waiter = None
    _py_enter_task(self._loop, self)          # ← what current_task() reads
    try:
        self.__step_run_and_handle_result(exc)
    finally:
        _py_leave_task(self._loop, self)
        self = None
```

and the body is one `send`/`throw` plus an exhaustive classification of what came back:

```python
try:
    result = coro.send(None)  if exc is None  else  coro.throw(exc)
except StopIteration as exc:
    super().set_result(exc.value)                   # coroutine returned
except exceptions.CancelledError as exc:
    self._cancelled_exc = exc
    super().cancel()                                # coroutine accepted cancellation
except (KeyboardInterrupt, SystemExit) as exc:
    super().set_exception(exc); raise               # ← re-raised into the LOOP
except BaseException as exc:
    super().set_exception(exc)                      # coroutine raised
else:
    blocking = getattr(result, '_asyncio_future_blocking', None)
    if blocking is not None:      ... # a Future: subscribe (§10)
    elif result is None:          self._loop.call_soon(self.__step)   # bare yield
    else:                         RuntimeError('Task got bad yield: ...')
```

Everything about task semantics is visible in that block:

| Coroutine did | Task does |
|---|---|
| `return v` (`StopIteration(v)`) | `set_result(v)` |
| raise `CancelledError` | `Future.cancel()` — task state becomes CANCELLED, not FAILED |
| raise `KeyboardInterrupt`/`SystemExit` | set it **and re-raise into the event loop** — these two escape `run_forever` |
| raise anything else | `set_exception(exc)` |
| `yield <Future>` (via `await`) | subscribe `__wakeup`, store `_fut_waiter` |
| `yield None` (bare) | `call_soon(__step)` — this is `asyncio.sleep(0)` |
| `yield <anything else>` | `RuntimeError` |

Three details worth pausing on:

**`asyncio.sleep(0)` has a dedicated fast path.** *(verified, `tasks.py`)*:

```python
def __sleep0():
    """Skip one event loop run cycle. ... uses a bare 'yield' expression
    (which Task.__step knows how to handle) instead of creating a Future object."""
    yield

async def sleep(delay, result=None):
    if delay <= 0:
        await __sleep0()
        return result
    ...
    future = loop.create_future()
    h = loop.call_later(delay, futures._set_result_unless_cancelled, future, result)
    try:    return await future
    finally: h.cancel()
```

`sleep(0)` allocates no Future and touches no timer heap — it is the cheapest possible
"give the loop one turn". `sleep(n>0)` allocates a Future *and* a `TimerHandle`, and note
the `finally: h.cancel()` — every timed-out-early sleep leaves a cancelled `TimerHandle`
for §8's heap-pruning heuristic to clean up.

**The loop keeps only *weak* references to tasks** *(verified)*:

```python
_scheduled_tasks = weakref.WeakSet()
_eager_tasks = set()
```

Hence the documented hazard: *"Save a reference to tasks passed to this function... The
event loop only keeps weak references to tasks. A task that isn't referenced elsewhere
may get garbage collected at any time, even before it's done."* *(sourced,
`asyncio.shield` docstring, verbatim from the source)*. §20 reports my failed attempt to
reproduce this.

**`__wakeup` deliberately calls `__step()` with no arguments** on success, and the
comment says why *(verified)*: passing a value would make the eval loop use `send(value)`
instead of `__next__()`, "which is slower for futures that return non-generator iterators
from their `__iter__`." A micro-optimization documented in the source — worth reading as
evidence of how carefully this hot path has been tuned.

---

## 13. Cancellation, the hardest part

Everything before this section is mechanism you can reason about locally. Cancellation is
where asyncio gets genuinely hard, and the reason is structural: **cancellation is
implemented as an exception, delivered at a suspension point, into code that may or may
not be prepared for it, and which may legitimately refuse it.**

### 13.1 `CancelledError` is a `BaseException`

```python
>>> asyncio.CancelledError.__mro__
(<class 'asyncio.exceptions.CancelledError'>, <class 'BaseException'>, <class 'object'>)
```

*(verified.)* It moved out of `Exception` in Python 3.8, for one reason: so that
`except Exception:` does not swallow it. Which makes this a bug:

```python
try:
    await do_work()
except Exception:            # fine — will NOT catch CancelledError
    log.exception("failed")
```

and this a much worse one:

```python
try:
    await do_work()
except BaseException:        # catches CancelledError
    ...                      # and if you don't re-raise, the cancellation is LOST
```

**A swallowed `CancelledError` is a hung shutdown.** The canonical rule: catch it only to
clean up, and always re-raise.

### 13.2 What `Task.cancel()` actually does

```python
def cancel(self, msg=None):
    self._log_traceback = False
    if self.done(): return False
    self._num_cancels_requested += 1
    if self._fut_waiter is not None:
        if self._fut_waiter.cancel(msg=msg):
            return True
    self._must_cancel = True
    self._cancel_message = msg
    return True
```

*(verified.)* Two branches:

- **Task is suspended on a Future** → cancel *that* Future. The Future transitions to
  CANCELLED, schedules its callbacks, and on the next iteration `__wakeup` calls
  `future.result()`, which raises `CancelledError`, which `__step(exc)` throws into the
  coroutine at its await point.
- **Task is not currently suspended** (it is queued to run, or running right now) → set
  `_must_cancel`, and the *next* `__step` converts it into a thrown `CancelledError`.

**`cancel()` returns immediately and guarantees nothing.** Its own docstring says so:
*"Unlike Future.cancel, this does not guarantee that the task will be cancelled: the
exception might be caught and acted upon, delaying cancellation of the task or preventing
cancellation completely."* *(verified.)* You must `await` the task to learn what
happened.

Note also the commented-out block sitting in the shipped source:

```python
# These two lines are controversial.  See discussion starting at
# https://github.com/python/cpython/pull/31394#issuecomment-1053545331
# if self._num_cancels_requested > 1:
#     return False
```

*(verified.)* Whether repeated `cancel()` calls should be no-ops is *still* an open
design argument, preserved in comments, in the middle of the most-used cancellation
function in the language. That is an honest signal about how settled this area is.

### 13.3 Delivery happens only at await points — demonstrated

```python
async def spin():
    try:
        n = 0
        for _ in range(300_000):      # pure Python, no awaits
            n += 1
        return "spin completed despite cancel()"
    except asyncio.CancelledError:
        return "cancelled"

t = asyncio.ensure_future(spin())
await asyncio.sleep(0)                # let it start
t.cancel()                            # cancel while it is running
await t
```

Result *(verified, run live)*: **`spin completed despite cancel()`**.

The loop cannot interrupt a running callback (§7, point 2). `cancel()` set
`_must_cancel`; by the time the next `__step` ran, the coroutine had already returned.
This is the same non-preemption that makes a blocking call fatal, seen from the
cancellation side.

> **The rule to carry:** *cancellation latency is bounded by your longest stretch of
> code between two `await`s.* Not by the timeout you configured. If a request handler has
> a 200 ms CPU-bound stretch, no timeout shorter than 200 ms is achievable, and
> `asyncio.timeout(0.05)` will silently take 200 ms.

### 13.4 `shield` — and why it is a trap

```python
res = await asyncio.shield(something())
```

`shield` creates an *outer* Future and links it to the inner one with done-callbacks
*(verified, `tasks.py`)*. Cancelling the outer future does not touch the inner task: the
`_outer_done_callback` merely detaches `_inner_done_callback` and attaches
`_log_on_exception` instead.

Read that last part carefully. **After a shield is cancelled, the inner task keeps
running with nobody waiting for it**, and the only thing left listening is a logger. It
is not a way to "protect" work; it is a way to *detach* work, and detached work in an
async service is exactly what §11's never-retrieved warning is for. Reach for it only
when you genuinely mean "this must complete even if my caller gives up", and keep a
reference to the inner task so you can await it during shutdown.

### 13.5 The cancelling/uncancel counters (3.11+)

The subtle problem `TaskGroup` and `timeout` created: **a timeout wants to cancel a task,
observe the cancellation, and then *not* propagate it** — converting it into
`TimeoutError`. But if the task was *also* cancelled from outside at the same moment,
swallowing the `CancelledError` loses a real cancellation.

The fix is a counter, not a flag *(verified, `tasks.py`)*:

```python
def cancelling(self):
    return self._num_cancels_requested

def uncancel(self):
    if self._num_cancels_requested > 0:
        self._num_cancels_requested -= 1
        if self._num_cancels_requested == 0:
            self._must_cancel = False
    return self._num_cancels_requested
```

The protocol: whoever calls `cancel()` is responsible for calling `uncancel()` if they
decide to handle it. If `uncancel()` returns 0, no outstanding cancellation remains and
it is safe to suppress. If it returns > 0, somebody else still wants this task dead —
propagate.

Verified live *(verified)*:

```
after 2 cancels:                    t.cancelling() == 2
inside the child's except block:    current_task().cancelling() == 2
```

`asyncio.timeout.__aexit__` is the canonical consumer *(verified, `timeouts.py`)*:

```python
if self._state is _State.EXPIRING:
    self._state = _State.EXPIRED
    if self._task.uncancel() <= self._cancelling and exc_type is not None:
        if issubclass(exc_type, exceptions.CancelledError):
            raise TimeoutError from exc_val
```

It snapshots `self._cancelling = self._task.cancelling()` on `__aenter__` and compares
against it on exit. **The comparison is against the entry snapshot, not against zero** —
because the task may already have had pending cancellations when the block was entered.
That single line is the difference between a timeout that composes correctly with an
outer `TaskGroup` and one that eats your shutdown signal.

Verified end to end *(verified, run live)*:

```
async with asyncio.timeout(0.01):
    await asyncio.sleep(1)
# → TimeoutError, __cause__ = CancelledError
```

The `CancelledError` is preserved as `__cause__`. It was converted, not discarded.

### 13.6 Why this is genuinely the hardest part

Collect the properties: cancellation is (a) an exception, so it interacts with every
`try`/`finally` and every `except` in the call stack; (b) delivered only at await points,
so its latency is unbounded by anything you configure; (c) *refusable*, by design; (d)
counted rather than flagged, because it must compose; (e) delivered to *one* task, so
propagating it to children is entirely the application's job unless a `TaskGroup` is
doing it for you; and (f) it can arrive **inside your cleanup code** — an `await` in a
`finally` block can itself be cancelled, which is why "async cleanup is not guaranteed to
complete" is a true statement about asyncio.

Nothing about that is fixable by being careful at a call site. It is fixable by
structure, which is §14.

---

## 14. Structured concurrency: `TaskGroup`, `ExceptionGroup`, `timeout`

### 14.1 What `gather` gets wrong

`asyncio.gather` predates all of this and has three failure modes. The first, measured
live *(verified)*:

```python
async def boom():      raise ValueError("boom")
async def survivor():  await asyncio.sleep(0.02); log("survivor finished")

try:
    await asyncio.gather(boom(), survivor())
except ValueError as e:
    log(f"gather raised {e!r}")
await asyncio.sleep(0.05)
```

Output:

```
["gather raised ValueError('boom')", 'survivor finished']
```

**`gather` re-raised the first exception and left the sibling running.** Control returned
to your `except` block while `survivor` was still executing — and had you not slept
afterwards, it would have been an orphan. That is failure mode 1: *no cancellation of
siblings*.

Failure mode 2: *only the first exception survives.* If both children raise, you see one
and the other is silently dropped (or produces a never-retrieved warning).

Failure mode 3: *`return_exceptions=True` inverts the problem* — nothing raises, and it
becomes your job to scan the result list for exception instances, which nobody
consistently does.

### 14.2 `TaskGroup` (3.11+)

Same scenario, same siblings *(verified, run live)*:

```python
try:
    async with asyncio.TaskGroup() as tg:
        tg.create_task(boom())
        tg.create_task(survivor())
except* ValueError as eg:
    log(f"except* caught {type(eg).__name__} {eg.exceptions}")
```

Output:

```
['survivor cancelled', "except* caught ExceptionGroup (ValueError('boom'),)"]
```

The sibling was **cancelled**, and the failure arrived as an `ExceptionGroup`. Both
defects fixed.

The mechanism, from `taskgroups.py` *(verified)*:

- `create_task` registers `self._on_task_done` as a done-callback on every child and adds
  it to `self._tasks`.
- `_on_task_done` collects the exception into `self._errors`, and if the group is not
  already aborting, calls `self._abort()` (which cancels every unfinished child) **and
  cancels the parent task**, setting `_parent_cancel_requested = True`. Cancelling the
  parent is what interrupts a parent that is sitting on some *other* `await` inside the
  `async with` body.
- `_aexit` loops on an internal `_on_completed_fut` until `self._tasks` is empty — the
  group cannot exit while any child is alive. This is the structural guarantee.
- On the way out, if `_parent_cancel_requested`, it calls `self._parent_task.uncancel()`
  and suppresses the `CancelledError` when the count reaches zero — §13.5's protocol,
  used exactly as designed.
- Finally: `raise BaseExceptionGroup('unhandled errors in a TaskGroup', self._errors)`.

Two subtleties in the shipped code worth knowing:

```python
if self._errors:
    if self._parent_task.cancelling():
        self._parent_task.uncancel()
        self._parent_task.cancel()
```

*(verified.)* An uncancel-then-recancel, whose comment explains it: *"If the parent task
is being cancelled from the outside of the taskgroup, un-cancel and re-cancel the parent
task, which will keep the cancel count stable."* Preserving the *count* across the
group's own bookkeeping is the entire reason `cancelling()` is public.

```python
self._tasks.add(task)
task.add_done_callback(self._on_task_done)
```

with the comment *"Always schedule the done callback even if the task is already done
(e.g. if the coro was able to complete eagerly), otherwise if the task completes with an
exception then it will cancel the current task too early. gh-128550, gh-128588"*
*(verified)*. That is the eager task factory (§15) colliding with `TaskGroup` — a real
bug, fixed after 3.12 shipped. Composition of these features is not free.

### 14.3 `ExceptionGroup` and `except*` (PEP 654)

`TaskGroup` *needs* PEP 654 and could not have shipped without it. Concurrent children
produce a *set* of failures, and Python's exception model had no way to represent one.

Practical rules:

| | |
|---|---|
| `except*` matching | runs **every** matching clause, splitting the group by type; `except` runs at most one |
| `ExceptionGroup` vs `BaseExceptionGroup` | `ExceptionGroup` may only contain `Exception` subclasses; `TaskGroup` raises `BaseExceptionGroup`, whose constructor returns an `ExceptionGroup` when all members are `Exception`s — which is why the demo above shows `ExceptionGroup` |
| single child | still wrapped in a group. `except ValueError:` around a `TaskGroup` **will not fire** — the most common migration bug |
| `KeyboardInterrupt`/`SystemExit` | tracked separately as `_base_error` and re-raised *bare*, not wrapped, so `Ctrl-C` still behaves |

### 14.4 `asyncio.timeout()` (3.11+)

A context manager rather than a wrapper, which is the whole point: it applies to a
*block*, composes with `TaskGroup`, and (unlike `wait_for`) does not need to own a task.

Mechanism *(verified, `timeouts.py`)*: `__aenter__` snapshots `self._cancelling =
task.cancelling()` and schedules `loop.call_at(when, self._on_timeout)`. `_on_timeout`
calls `self._task.cancel()` and flips state to `EXPIRING`. `__aexit__` performs §13.5's
uncancel dance and converts to `TimeoutError`.

`reschedule(when)` lets you move the deadline mid-flight — the correct primitive for
"deadline for the whole operation, extended on each byte received", which people
otherwise implement with a chain of `wait_for`s.

Note `asyncio.TimeoutError is TimeoutError` *(verified)* — as of 3.11 it is an alias for
the builtin, so `except TimeoutError:` is now correct and portable.

**3.15 adds `TaskGroup.cancel()`** *(sourced, 3.15 whatsnew, gh-127214, John Belmonte)*
for early termination of a group when its goal has been met — previously this required
raising a sentinel exception inside the group and suppressing it on the way out.

---

## 15. The eager task factory (3.12+)

The observation: most coroutines don't actually suspend. A cache hit, a buffered read, a
validation step — they run to completion on the first `send`. But `create_task` always
pays: allocate a Task, allocate a `Handle`, append to `_ready`, wait for the next
iteration, then run.

The eager task factory removes that round trip *(verified, `tasks.py`)*:

```python
def create_eager_task_factory(custom_task_constructor):
    def factory(loop, coro, *, eager_start=True, **kwargs):
        return custom_task_constructor(coro, loop=loop, eager_start=eager_start, **kwargs)
    return factory

eager_task_factory = create_eager_task_factory(Task)
```

and in `Task.__init__`, `eager_start and self._loop.is_running()` routes to
`__eager_start`, which runs the coroutine's first step **synchronously, inside
`create_task`**:

```python
def __eager_start(self):
    prev_task = _py_swap_current_task(self._loop, self)
    try:
        _py_register_eager_task(self)
        try:
            self._context.run(self.__step_run_and_handle_result, None)
        finally:
            _py_unregister_eager_task(self)
    finally:
        curtask = _py_swap_current_task(self._loop, prev_task)
        if self.done():
            self._coro = None
        else:
            _py_register_task(self)     # ← "graduates" to the normal path
```

Enable it with:

```python
loop.set_task_factory(asyncio.eager_task_factory)
```

**What changes, and it is not only performance:**

| | Lazy (default) | Eager |
|---|---|---|
| When the first step runs | next loop iteration | **inside `create_task()`** |
| `current_task()` during that step | the new task | the new task (swapped in and out) |
| If it completes without suspending | Task allocated, scheduled, run | Task allocated, run, **never scheduled** |
| Task registry | `_scheduled_tasks` (WeakSet) | `_eager_tasks` (a plain `set`) until it suspends |
| **Ordering semantics** | strictly "later" | **the body runs before `create_task` returns** |

That last row is a semantic change, not an optimization. Code that assumed
`create_task(f())` would not touch shared state until the caller next awaited is now
wrong. So is code that relies on tasks starting in creation order relative to other
`call_soon` work.

The reported win is **"2x to 5x faster" for some use cases** *(sourced, 3.12 whatsnew,
gh-102853 / gh-104140 / gh-104138, Jacob Bower & Itamar Oren)*. I did not measure it
here — see §20.

**It is opt-in, and it should stay that way in a library.** Setting a task factory is a
loop-global decision; a library that sets it changes semantics for the whole application.
Set it in your application entry point, or not at all. And recall §14.2: eager start
already produced one real `TaskGroup` bug (gh-128550/gh-128588). Adopt it deliberately,
after reading your own code for creation-order assumptions.

---

## 16. `asyncio.run`, `Runner`, and loop lifecycle

`asyncio.run` is a thin wrapper around `Runner` *(verified, `runners.py`)*:

```python
def run(main, *, debug=None, loop_factory=None):
    if events._get_running_loop() is not None:
        raise RuntimeError("asyncio.run() cannot be called from a running event loop")
    with Runner(debug=debug, loop_factory=loop_factory) as runner:
        return runner.run(main)
```

`Runner.run` does four things beyond `run_until_complete` *(verified)*:

1. Wraps non-coroutine awaitables in a coroutine.
2. Creates the task with the runner's copied `contextvars.Context`.
3. **Installs a `SIGINT` handler** — but only if this is the main thread *and* the current
   handler is still `signal.default_int_handler`. First `Ctrl-C` cancels the main task
   (plus a `call_soon_threadsafe` to wake the selector, §9); second raises
   `KeyboardInterrupt` immediately. On the way out, if the interrupt count is > 0 and
   `task.uncancel() == 0`, the `CancelledError` is converted back into
   `KeyboardInterrupt` — §13.5 again.
4. Restores the previous handler.

`Runner.close` (and therefore the end of every `asyncio.run`) performs shutdown in a
fixed order *(verified)*:

```
_cancel_all_tasks(loop)              # cancel every remaining task, then
                                     #   gather(*to_cancel, return_exceptions=True)
                                     #   and report anything that raised
loop.run_until_complete(loop.shutdown_asyncgens())
loop.run_until_complete(loop.shutdown_default_executor(timeout=THREAD_JOIN_TIMEOUT))
loop.close()
```

with `THREAD_JOIN_TIMEOUT = 300` *(verified, `constants.py`)* — the documented "five
minutes to shut the executor down, then warn."

**`shutdown_asyncgens` is the part people don't know exists.** An async generator
suspended at a `yield` has a pending `finally` that can only be run by awaiting
`aclose()`. PEP 525 solved this with `sys.set_asyncgen_hooks(firstiter=..., finalizer=...)`,
and `_run_forever_setup` installs asyncio's *(verified)*:

```python
self._old_agen_hooks = sys.get_asyncgen_hooks()
sys.set_asyncgen_hooks(firstiter=self._asyncgen_firstiter_hook,
                       finalizer=self._asyncgen_finalizer_hook)
```

`firstiter` registers the generator in a `WeakSet`; `finalizer` schedules
`agen.aclose()` via `call_soon_threadsafe`, because finalization can happen on the GC's
whim in any thread. This is the machinery that makes `async with` inside an async
generator survivable. If you have ever seen `an asynchronous generator was garbage
collected without being closed`, you were outside it.

### Loop lifecycle rules that follow

- **`asyncio.run` always creates a *new* loop and always closes it.** It is not
  re-entrant and not resumable. Calling it twice in a process is legal but discards all
  loop-scoped state.
- **Use `Runner` when you need to interleave async and blocking phases** on one loop —
  it is the supported replacement for the old `loop = get_event_loop(); ...` pattern.
- **`asyncio.get_event_loop()` now raises `RuntimeError` if there is no running loop**
  *(3.14, sourced, gh-126353)* rather than silently creating one. Code that relied on the
  implicit creation is broken on 3.14 — deliberately.
- **The event loop *policy* system is deprecated and slated for removal in 3.16**
  *(sourced, 3.14 whatsnew, gh-127949)*: `AbstractEventLoopPolicy`,
  `DefaultEventLoopPolicy`, `get_event_loop_policy`, `set_event_loop_policy`, and the two
  Windows policy classes. The replacement is
  `asyncio.run(main(), loop_factory=...)` / `Runner(loop_factory=...)`. **Any code doing
  `asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())` is on a removal path** —
  §18 has the modern form.

---

## 17. Debug mode and slow-callback detection

Turn it on with `asyncio.run(main(), debug=True)`, `loop.set_debug(True)`,
`PYTHONASYNCIODEBUG=1`, or `-X dev`.

What it actually enables, from the source *(verified)*:

| Check | Where |
|---|---|
| **Slow-callback warning** | `_run_once` phase ⑥ times every `handle._run()` and logs if `dt >= loop.slow_callback_duration` (default **0.1** s) |
| **Thread affinity** | `_check_thread()` on `call_soon` / `call_at` / `call_soon_threadsafe` — raises if called off the loop thread |
| **Callback type check** | `_check_callback()` rejects a coroutine passed where a callable was expected |
| **Source tracebacks** | every `Handle` and `Future` captures `DEBUG_STACK_DEPTH = 10` stack frames at creation, so the warning can say *where it was created* |
| **Coroutine origin tracking** | `sys.set_coroutine_origin_tracking_depth` — makes "coroutine was never awaited" name the creation site |

Reproduced live *(verified)*:

```python
async def m2():
    time.sleep(0.25)          # blocks the loop inside one callback
asyncio.run(m2(), debug=True)
```

```
Executing <Task finished name='Task-5' coro=<m2() ...> created at .../runners.py:110>
took 0.255 seconds
```

**This is the single most valuable diagnostic asyncio ships**, because it detects the
failure mode from §7 point 2 directly rather than by inference. The signature in
production without it is: rising p99 with flat CPU, latency that correlates across
*unrelated* endpoints (they share the loop), and a profiler that shows the blocking call
as a small fraction of total time (it is — it just serializes everything else).

Two production notes:

- **Debug mode is not free.** Capturing a 10-frame traceback per Handle per callback is
  real work. Enable it in staging and in load tests; think hard before production.
- **`slow_callback_duration` is tunable and 0.1 s is enormous** for a latency-sensitive
  service. Setting `loop.slow_callback_duration = 0.005` in a canary will find things
  the default never reports. The timing code in phase ⑥ is only compiled into the
  `self._debug` branch, so you must have debug mode on for it to apply at all.

For finding *where* a stuck loop is stuck, 3.14 added external introspection:
`python -m asyncio ps PID` and `python -m asyncio pstree PID` *(sourced, 3.14 whatsnew,
gh-91048)*, plus in-process `asyncio.capture_call_graph()` /
`asyncio.print_call_graph()` *(verified — both present in `asyncio.__all__` on this
build)*. These walk the `cr_await` chain from §4 and the `future_add_to_awaited_by`
edges that `Task.__step` and `TaskGroup.create_task` maintain. This is the first time
CPython has shipped a way to answer "what is my async service blocked on?" from outside
the process.

---

## 18. uvloop, and the architectural reason it wins

`uvloop` is a drop-in replacement for `BaseEventLoop`, written in **Cython** on top of
**libuv** — the same event loop Node.js uses. The README's claim is *"uvloop makes
asyncio 2-4x faster"* *(sourced, uvloop README, verbatim)*.

The modern way to use it *(sourced, uvloop README)*:

```python
import uvloop
uvloop.run(main())                                   # preferred, uvloop ≥ 0.18
# or, explicitly:
with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
    runner.run(main())
```

Not `uvloop.install()` and not `set_event_loop_policy` — see §16's deprecation.

### What it replaces

`uvloop` reimplements the *loop*, not the language. Coroutines, `await`, `Task`, `Future`,
and cancellation semantics are unchanged — it must be so, since your coroutines are
compiled bytecode either way. What it replaces is everything in §7–§10 below the Task:

| Layer | stdlib | uvloop |
|---|---|---|
| Loop iteration | Python `_run_once` | libuv's `uv_run`, in C |
| Readiness | `selectors` (Python wrapper over kqueue/epoll) | libuv's platform backends, in C |
| Ready queue / timer heap | `collections.deque` + `heapq`, Python objects | libuv's C structures |
| Transports & protocols | Python classes per read/write | Cython `cdef` classes calling libuv directly |
| `Future`/`Task` | `_asyncio` C accelerator (stdlib already) | same idea, uvloop's own |

### The architectural reason, stated properly

It is **not** "C is faster than Python". It is that the stdlib loop crosses the
Python/C boundary many times per I/O event, and uvloop crosses it about once.

Trace one received packet through §10 again and count the Python-level objects
constructed and Python-level calls made per event in the stdlib path: a `SelectorKey`
lookup, a `Handle`, a `deque` append, a `Handle._run`, a `Context.run`, a transport
method, a protocol method, a `Future.set_result`, a `__schedule_callbacks`, another
`Handle`, another `call_soon`, then `Task.__wakeup` → `Task.__step` → `coro.send`. In
uvloop, everything from the syscall down to "call the protocol's `data_received`" happens
inside compiled Cython with libuv's own structures, and the interpreter is entered once —
to run your callback.

Three corollaries that follow directly, and that separate a real answer from a recited one:

- **The speedup is largest for I/O-heavy, compute-light workloads** — an echo server,
  a proxy, a fan-out gateway. That is exactly the shape of the README's benchmark chart.
- **The speedup approaches zero as your per-request Python work grows.** If your handler
  spends 3 ms parsing JSON and doing business logic, replacing 40 µs of loop overhead
  with 10 µs is noise. Measure your loop overhead *before* adopting it, and read
  [`31-measurement-methodology.md`](31-measurement-methodology.md) first.
- **You inherit libuv's semantics and libuv's bugs**, and you lose the ability to read
  the loop's source in an incident. That is a real operational cost, not a rhetorical one.

**Availability as of this writing** *(verified, from the PyPI JSON API on 2026-08-02)*:
latest release **0.22.1**, uploaded 2025-10-16, 48 wheels — including
`cp314` **and `cp314t`** (free-threaded) builds for macOS, manylinux and musllinux. Note
the package's trove classifiers stop at 3.13 while the wheels ship 3.14/3.14t; the
classifiers are stale, the wheels are the truth. No release newer than 0.22.1 exists on
PyPI at this time.

**A closing detail with real explanatory weight:** `Lib/asyncio/constants.py` and
`Lib/asyncio/events.py` both carry the header *"Contains code from
https://github.com/MagicStack/uvloop"* with an MIT/Apache dual-license notice
*(verified)*. The influence runs both ways — parts of uvloop have been upstreamed into
the stdlib loop. Some of the historical 2–4× gap has been closed by CPython itself.

---

## 19. asyncio vs threads vs free-threading

### One loop is one thread

This is the sentence to be able to defend under follow-up questions:

> **asyncio gives you concurrency, not parallelism. One event loop runs on one thread,
> executes one callback at a time, and cannot use a second core. It never could.**

What it *does* give you is a cheaper unit of waiting. A thread blocked in `recv` costs a
stack (megabytes of address space), a kernel scheduling entity, and a GIL handoff on
every wakeup. A coroutine blocked on a Future costs one entry in a callback list. That is
the entire trade, and it is why asyncio wins at 100k connections and loses at 100k
CPU-bound tasks.

| | asyncio | Threads (GIL build) | Threads (free-threaded) |
|---|---|---|---|
| Parallel Python bytecode | ✗ | ✗ | ✓ |
| Cost per waiting unit | one callback entry | ~8 MB stack + kernel task | same as GIL build |
| Switch cost | a Python function call | context switch + GIL handoff |context switch |
| Switch points | **only at `await`** — visible in source | anywhere ([`24`](24-the-gil.md) §4) | anywhere |
| Blocking call | **stalls everything** | stalls one thread | stalls one thread |
| Races | fewer (switches are explicit) | many | many, more likely |

The switch-point row is asyncio's real correctness advantage and it is underrated: **the
set of points at which your state can change out from under you is exactly the set of
`await` expressions, and they are lexically visible.** That is a much stronger property
than anything `threading` offers, and it is why a lot of asyncio code correctly gets away
with no locks at all. It is also exactly why a `TOCTOU` bug across an `await` is such a
classic — see [`29-async-patterns-and-pitfalls.md`](29-async-patterns-and-pitfalls.md).

### The convoy interaction ([`24-the-gil.md`](24-the-gil.md) §7)

The most damaging asyncio/threads interaction is not inside the loop, it is at the
boundary. `loop.run_in_executor` / `asyncio.to_thread` hands work to a
`ThreadPoolExecutor`. If that work is genuinely blocking-I/O, fine. If it turns out to be
CPU-bound Python, **the loop thread becomes the well-behaved I/O thread in doc 24's
convoy scenario** — it wakes, does microseconds of work, and then waits a full switch
interval before it is even allowed to ask for the GIL back. Your entire service's tail
latency quantizes to `sys.getswitchinterval()`.

Signature: p99 with a cliff near 5 ms, low CPU utilization, and a `py-spy` profile
showing the loop thread mostly idle. Confirm by perturbing:
`sys.setswitchinterval(0.0001)`. Doc 24 §16 is the full procedure.

### Free-threading changes the *deployment* shape, not the loop

Python 3.14 gave asyncio **first-class free-threading support** *(sourced, 3.14 whatsnew,
gh-128002)*, described as enabling *"parallel execution of multiple event loops across
different threads, scaling linearly with the number of threads."* The new
`asyncio-threading` docs page states the model plainly *(sourced, verbatim)*:

> *"A single event loop on one core can handle many connections concurrently, but the
> Python code that runs to handle each one still executes serially. Once requests involve
> a non-trivial amount of per-request computation, that handling becomes the bottleneck,
> and a single core can no longer keep up. Combining asyncio with threads is most useful
> here: by running an event loop per thread, the handling of different requests can run
> in parallel across multiple CPU cores."*

So the free-threaded answer is **loop-per-thread**, not a faster loop. The stated rules
*(sourced, same page)*:

- Each thread gets its own loop; never share one across threads.
- Tasks and Futures created on one loop must not be awaited or manipulated from another.
- Cross-thread entry points are `asyncio.run_coroutine_threadsafe` and
  `loop.call_soon_threadsafe` — nothing else.
- `asyncio.Lock`, `asyncio.Event`, etc. are **not** cross-thread primitives. They protect
  against other tasks on the same loop, not other threads.

The internal changes that made this safe are visible in the source: `_ThreadSafeHandle`
with its `RLock` (§8), the per-thread `_current_tasks` mapping, and the replacement of
the global task registry with **a per-thread doubly-linked list for native tasks**, which
the release notes credit with a **10–20% improvement in standard benchmark results** and
reduced memory, *and* which is what makes cross-thread introspection (`pstree`) possible
*(sourced, 3.14 whatsnew, gh-107803, Kumar Aditya)*.

Compare this with the *N processes behind a load balancer* model that async Python has
used for a decade. Loop-per-thread wins on shared caches, warm connection pools, and one
process to observe; it loses on fault isolation and inherits every hazard in
[`26-free-threading.md`](26-free-threading.md) §13 — the sharing wall is still there, and
a shared dict between loops is still a coherence problem.

---

## 20. What I could not verify

Three items. Doc 16 §8 sets the precedent: report the failure, don't manufacture a
result.

**1. The "task disappeared mid-flight" hazard — could not reproduce.** The stdlib's own
docstring warns that *"the event loop only keeps weak references to tasks. A task that
isn't referenced elsewhere may get garbage collected at any time, even before it's
done."* The registry really is `weakref.WeakSet()` *(verified)*. But I tried twice to
reproduce collection of an unreferenced in-flight task and failed both times:

```python
t = asyncio.ensure_future(work())     # awaits an Event nobody else references
r = weakref.ref(t)
del t; gc.collect()
# → task alive after gc? True   ... work finished   ... task alive at end? False
```

In both attempts the awaited object's done-callback list held a bound method of the task
(`Task.__wakeup`), which kept it alive until completion — exactly the `_fut_waiter`
edge from §10. **I did not construct a case where the chain is genuinely broken.** The
warning may require a scenario I did not find (a custom awaitable that drops its
callbacks, a cancelled-then-resurrected future, or the C `_asyncio.Task` path
specifically). Treat "keep a reference to your tasks" as sound defensive practice with a
documented rationale — but I cannot show you the failure, and I am not going to invent
one. **This is an open item for a future revision.**

**2. The eager task factory's "2x to 5x" and uvloop's "2-4x" are quoted, not
measured.** Both come from the sources cited in §24 (3.12 release notes; uvloop README).
Measuring either properly means a load test, which this document deliberately does not
run. Assume both numbers describe their authors' best case, and read
[`31-measurement-methodology.md`](31-measurement-methodology.md) before quoting them at
anyone.

**3. Kumar Aditya's free-threading asyncio blog post is cited but unread.** The official
`asyncio-threading` docs page links it as the source for the "scaling linearly with the
number of threads" claim and for benchmark numbers. The host returned HTTP 429 during
this session and I did not retrieve it. **The only free-threading asyncio numbers in this
document are the 10–20% from the CPython release notes**, which I did read. If you want
the scaling curves, go to the blog directly — do not take a number from here that isn't
here.

Two smaller notes on precision:

- The 3.15 documentation build I fetched self-identified as **`3.15.0b4`**, not rc1.
  Anything I attribute to 3.15 below is from that build and could still change.
- The exact free-threaded behaviour of the per-thread native-task linked list — in
  particular whether it holds strong or weak references — I read only through its
  Python-visible effects (`all_tasks()` reporting) and the release notes, **not** from
  `Modules/_asynciomodule.c`. Do not quote me on the reference strength.

---

## 21. Version deltas, 3.11 → 3.15

All *(sourced)*, from the respective "What's New" documents.

**3.11** — the structured-concurrency release.
`TaskGroup` (Yury Selivanov, gh-90908) · `timeout()` / `timeout_at()` (Andrew Svetlov,
gh-90927) · `Runner` (gh-91218) · `Barrier` · `Task.cancelling()` / `Task.uncancel()`
("primarily intended for internal use, notably by `TaskGroup`") · `asyncio.TimeoutError`
becomes an alias for the builtin · PEP 654 `ExceptionGroup`/`except*` lands in the
language.

**3.12** — the performance release.
`eager_task_factory` / `create_eager_task_factory` (gh-102853, gh-104140, gh-104138) ·
`loop_factory` on `asyncio.run()` (gh-99388) · socket writes avoid a copy and use
`sendmsg()` where available (gh-91166) · C implementation of `current_task()` claimed
4×–6× (gh-100344) · `asyncio.iscoroutine()` now returns `False` for generators — legacy
generator-based coroutines are gone.

**3.13** — the correctness release.
`Queue.shutdown` + `QueueShutDown` (gh-104228) · `as_completed()` returns something that
is both an async iterator and a plain iterator, yielding the original task objects
(gh-77714) · **`TaskGroup` cancellation-collision fixes** (gh-116720): nested groups
could hang when both hit an exception simultaneously, because the inner group swallowed
the outer's cancellation; groups now preserve the cancellation count and may call the
parent's `cancel()` · `Server.close_clients()` / `abort_clients()` · child watchers
deprecated.

**3.14** — the free-threading and introspection release.
**First-class free-threading support** (gh-128002), loop-per-thread scaling ·
**per-thread doubly-linked list for native tasks**, 10–20% on standard benchmarks plus
lower memory (gh-107803) · `python -m asyncio ps` / `pstree`, `capture_call_graph()`,
`print_call_graph()` (gh-91048) · `create_task(**kwargs)` — `name` and `context` are no
longer special-cased · **`get_event_loop()` raises `RuntimeError` when no loop is
running** (gh-126353) · child-watcher classes removed · **the entire event-loop policy
system deprecated**, removal targeted at 3.16 (gh-127949) · `asyncio.iscoroutinefunction`
deprecated in favour of `inspect.iscoroutinefunction` (gh-122875) ·
`pdb.set_trace_async()` and a `$_asynctask` convenience variable.

**3.15 (beta at time of writing)** — `TaskGroup.cancel()` for early termination
(gh-127214, John Belmonte) · the policy and `iscoroutinefunction` deprecations continue
toward 3.16 removal · `python -m ... --async-aware` stack dumping mentioned in the
faulthandler/threading tooling notes.

**The direction of travel, stated once:** implicit global state (policies, the implicit
loop, child watchers) is being deleted; explicit structure (`Runner`, `loop_factory`,
`TaskGroup`, `timeout`) is replacing it; and the runtime is growing external
introspection because "what is my async service stuck on" was unanswerable for a decade.

---

## 22. Lab exercises

Reading this leaves you at rung 3 (README §14). **All of these are light — no load tests,
no benchmarks.** Each finishes instantly.

**1 — Disassemble your own `await`.** Write an `async def` containing one `await`, one
`async with`, and one `async for`. Run `dis.dis` on it. Find every `GET_AWAITABLE`,
`SEND`, `YIELD_VALUE`, `RESUME`, and `CLEANUP_THROW`, and account for the oparg on each
`RESUME`. Then check `co_flags & inspect.CO_COROUTINE`.
*Proves §2–§3, and inoculates you against "await is magic" forever.*

**2 — Drive a coroutine with no event loop.** Reproduce §5's `Awaitable` class and step
`demo()` by hand with `send`. Then repeat with `throw(CancelledError())` and with
`close()` on a coroutine that has a `finally`. Print `cr_await`, `cr_suspended`, and
`inspect.getcoroutinestate()` at each step.
*Proves §4–§5 — that `Task` has no powers you don't.*

**3 — Count the frame transitions.** Reproduce §6's `sys.monitoring` measurement: an
await chain of depth 1 through 5, one suspension each, counting `PY_RESUME` and
`PY_YIELD`. Confirm the +1-per-level result on your build. Then explain to yourself why
your web framework's middleware stack has a per-request cost.
*Proves §6. This is the cheapest genuinely surprising experiment in the document.*

**4 — Write the world's smallest event loop.** Under 60 lines: a `deque` of callbacks, a
`heapq` of timers, a `select` call, and a `Task` class whose `step` calls `coro.send`.
Make `await sleep(x)` work. Do **not** look at `base_events.py` while writing it; look
afterwards and diff your design against §7.
*Proves §7–§12. This is the single highest-value exercise here and the one that makes the
rest of asyncio stop being mysterious.*

**5 — Watch cancellation fail.** Reproduce §13.3: a task with a long pure-Python loop and
no awaits, cancelled while running. Confirm it completes. Then insert one
`await asyncio.sleep(0)` into the loop body and confirm it now cancels. Measure nothing —
just observe the two outcomes.
*Proves §13.3 and §7 point 2 in one shot.*

**6 — Break `gather`, then fix it with `TaskGroup`.** Reproduce §14.1: one child that
raises immediately, one that sleeps then prints. Show the sibling survives `gather` and
is cancelled by `TaskGroup`. Then write `except ValueError:` around the `TaskGroup` and
watch it *not* catch. Fix it with `except*`.
*Proves §14 — and the `except`-vs-`except*` step is the most common real migration bug.*

**7 — Find the blocking call.** Write a handler that does `time.sleep(0.25)` and run it
under `asyncio.run(..., debug=True)`. Read the warning. Then set
`loop.slow_callback_duration = 0.005` and find something in your *own* codebase that
trips it.
*Proves §17 — and step two usually finds a real bug.*

**8 — Inspect a live loop.** Start a program with a `TaskGroup` and some sleeping
children, then from another terminal run `python -m asyncio pstree <pid>`. Then do the
same in-process with `asyncio.print_call_graph()`. Map the output back onto the `cr_await`
chain from §4.
*Proves §4 and §17, and gives you a tool most engineers don't know exists.*

---

## 23. Question bank

Staff-level. The section to reread is noted.

1. `async def f(): ...` — what does the compiler actually change about `f`'s code object, and what does that change at call time? *(§2)*
2. Disassemble `x = await g()` and name every instruction. Which one suspends, and which one resumes? *(§3)*
3. `await` is described as "`yield from` with a type check." Defend or refute that from bytecode. *(§3)*
4. Where does a suspended coroutine's frame live, and why does that make deep await chains cheap in C stack but not in time? *(§4, §6)*
5. `Future.__await__` yields `self`. Why *itself*, and what does `_asyncio_future_blocking` distinguish? *(§5, §11)*
6. Your framework has 8 layers of middleware coroutines. What does each additional layer cost, and *per what*? *(§6)*
7. Walk one iteration of `_run_once` in order. Where exactly does the process sleep? *(§7)*
8. Why does the loop poll the selector even when there is already work in `_ready`? *(§7)*
9. Why is `await asyncio.sleep(0)` a full loop iteration rather than a cheap yield? What is the `ntodo` snapshot for? *(§7, §12)*
10. `TimerHandle.cancel()` cannot remove the timer from the heap. What does it do instead, and what stops `_scheduled` from growing without bound? *(§8)*
11. A brand-new event loop with no sockets is already watching one file descriptor. What is it, and name three things that would not work without it. *(§9)*
12. Trace `data = await reader.read(100)` from the Python statement to `kevent` and back. Name the two Task attributes that make resumption and cancellation possible. *(§10)*
13. Why are `Future` done-callbacks always routed through `call_soon` instead of being called directly? *(§11)*
14. Explain "Task exception was never retrieved" mechanically: which bit, set where, cleared where, checked where? Why does it sometimes appear long after the failure? *(§11)*
15. What does a `Task` add over a `Future`? Answer in terms of `__step`. *(§12)*
16. `Task.__step` catches `KeyboardInterrupt` and `SystemExit` differently from every other exception. How, and why? *(§12)*
17. Why is `CancelledError` a `BaseException`? Give a concrete bug that change prevented. *(§13.1)*
18. `Task.cancel()` returns `True`. What have you been promised? *(§13.2)*
19. You call `task.cancel()` and the task completes successfully anyway. Explain — and give the rule for bounding cancellation latency. *(§13.3)*
20. What does `shield` protect, and what happens to the inner task when the outer one is cancelled? *(§13.4)*
21. Why does cancellation need a *counter* (`cancelling()`/`uncancel()`) rather than a boolean? Give the scenario a boolean gets wrong. *(§13.5)*
22. `asyncio.timeout.__aexit__` compares `uncancel()` against a snapshot taken at `__aenter__`, not against zero. Why does that matter? *(§13.5)*
23. Name `gather`'s three failure modes and say which one `return_exceptions=True` fixes and which one it makes worse. *(§14.1)*
24. `except ValueError:` around a `TaskGroup` doesn't fire. Why, and what is the fix? *(§14.3)*
25. How does a `TaskGroup` interrupt a parent that is blocked on an unrelated `await` inside its body? *(§14.2)*
26. The eager task factory is a performance feature. Name the semantic change it makes and one real bug it caused. *(§15, §14.2)*
27. What does `asyncio.run` do on the way out, in order, and what is `shutdown_asyncgens` for? *(§16)*
28. The event loop policy system is being removed. What replaces it, and what is the modern way to install uvloop? *(§16, §18)*
29. Debug mode reports a callback took 0.25 s. What class of bug is that, and why is it invisible to an ordinary CPU profile? *(§17, [`32-profiling.md`](32-profiling.md))*
30. uvloop is "2-4x faster". Explain the architectural reason — without saying "it's written in C" — and name a workload where it would buy you nothing. *(§18)*
31. asyncio never gave you CPU parallelism. On a free-threaded 3.14 build, what does the parallel deployment shape look like, and which four rules must you follow? *(§19)*
32. Your async service's p99 has a cliff at 5 ms and CPU sits at 30%. First hypothesis, and the one-line experiment that confirms or kills it. *(§19, [`24-the-gil.md`](24-the-gil.md) §7, §16)*

---

## 24. Sources

**Primary — the source tree (read these, they are short)**
- [`Lib/asyncio/base_events.py`](https://github.com/python/cpython/blob/main/Lib/asyncio/base_events.py) — `run_forever`, **`_run_once`**, `call_soon`/`call_later`/`call_at`, the timer-pruning constants. *Verdict: `_run_once` is ~70 lines and is the single most valuable thing to read in this whole document. Start here.*
- [`Lib/asyncio/tasks.py`](https://github.com/python/cpython/blob/main/Lib/asyncio/tasks.py) — `Task.__step`, `__step_run_and_handle_result`, `__wakeup`, `cancel`/`uncancel`, `__eager_start`, `shield`, `gather`, `__sleep0`. *Verdict: read `__step_run_and_handle_result` in full; every Task semantic is in that one `try/except/else`.*
- [`Lib/asyncio/futures.py`](https://github.com/python/cpython/blob/main/Lib/asyncio/futures.py) — `Future.__await__`, `__schedule_callbacks`, the `__log_traceback` mechanism. *Verdict: 100 lines of real content, and `__await__` is six of them.*
- [`Lib/asyncio/taskgroups.py`](https://github.com/python/cpython/blob/main/Lib/asyncio/taskgroups.py) and [`timeouts.py`](https://github.com/python/cpython/blob/main/Lib/asyncio/timeouts.py) — *Verdict: the hardest code in asyncio and the best-commented. The comments explain cancellation-collision cases you will not think of yourself.*
- [`Lib/asyncio/selector_events.py`](https://github.com/python/cpython/blob/main/Lib/asyncio/selector_events.py), [`events.py`](https://github.com/python/cpython/blob/main/Lib/asyncio/events.py), [`runners.py`](https://github.com/python/cpython/blob/main/Lib/asyncio/runners.py) — `_add_reader`, `_process_events`, the self-pipe; `Handle`/`TimerHandle`; `Runner`. *Verdict: skim; read `_make_self_pipe` and `Handle._run` properly.*
- [`Python/bytecodes.c`](https://github.com/python/cpython/blob/main/Python/bytecodes.c) — `_SEND`, `YIELD_VALUE`, `GET_AWAITABLE`, `RESUME`, `_CHECK_PERIODIC_IF_NOT_YIELD_FROM`. *Verdict: authoritative for §3; grep by opcode name.*
- [`Include/internal/pycore_interpframe_structs.h`](https://github.com/python/cpython/blob/main/Include/internal/pycore_interpframe_structs.h), `pycore_genobject.h`, `pycore_frame.h`, `pycore_opcode_utils.h` — the embedded frame, `_PyGen_GetGeneratorFromFrame`, the frame-state enum, the `RESUME` oparg constants. *Verdict: §4 is unarguable once you have read `_PyGenObject_HEAD`.*
- [`InternalDocs/generators.md`](https://github.com/python/cpython/blob/main/InternalDocs/generators.md) — CPython's own explanation of `RETURN_GENERATOR`, `SEND`/`YIELD_VALUE` chaining, and `CLEANUP_THROW`. *Verdict: two pages, written by the people who wrote the code. Read it immediately after §3.*

**PEPs — in the order they built the thing**
- [PEP 3156 — Asynchronous IO Support Rebooted: the "asyncio" Module](https://peps.python.org/pep-3156/) (van Rossum, 2012). *Verdict: mostly of historical interest now, but §"Event Loop Interface" is still the specification third-party loops implement. Skim.*
- [PEP 492 — Coroutines with async and await syntax](https://peps.python.org/pep-0492/) (Selivanov, 2015). *Verdict: the primary source for §2–§5. The "Design Considerations" section explains why `await` is not `yield from` at the language level even though it is at the bytecode level.*
- [PEP 525 — Asynchronous Generators](https://peps.python.org/pep-0525/) (Selivanov, 2016). *Verdict: read the finalization section — it is the only clear explanation of `set_asyncgen_hooks` and §16's shutdown dance.*
- [PEP 530 — Asynchronous Comprehensions](https://peps.python.org/pep-0530/) (Selivanov, 2016). *Verdict: short; read only if `async for` inside a comprehension surprises you.*
- [PEP 654 — Exception Groups and except*](https://peps.python.org/pep-0654/) (Katriel, Selivanov, van Rossum, 2021). *Verdict: required for §14. The "Motivation" section is explicitly about `TaskGroup` — this PEP exists because structured concurrency needed it.*
- [PEP 567 — Context Variables](https://peps.python.org/pep-0567/). *Verdict: read if §8's `self._context.run(...)` was unfamiliar.*

**Official documentation**
- [asyncio — Asynchronous I/O](https://docs.python.org/3/library/asyncio.html) and [Coroutines and Tasks](https://docs.python.org/3/library/asyncio-task.html). *Verdict: the task page's cancellation notes are more precise than most blog posts; the rest is reference material.*
- [Developing with asyncio](https://docs.python.org/3/library/asyncio-dev.html). *Verdict: short, and the source for §17's debug-mode list. Everyone should read it once; almost nobody has.*
- [asyncio and free-threaded Python](https://docs.python.org/3/library/asyncio-threading.html) — **new in 3.14**. *Verdict: the authoritative statement of the loop-per-thread model in §19. Two pages. Read it before doing anything with asyncio on a free-threaded build.*
- [What's New in Python 3.13](https://docs.python.org/3.13/whatsnew/3.13.html) / [3.14](https://docs.python.org/3/whatsnew/3.14.html) / [3.15](https://docs.python.org/3.15/whatsnew/3.15.html) — the asyncio sections. *Verdict: the source for every dated claim in §21. The 3.13 `TaskGroup` cancellation-collision entry (gh-116720) is worth reading in full even if you never hit it.*
- [`dis` — Python Bytecode Instructions](https://docs.python.org/3/library/dis.html). *Verdict: authoritative for the `GET_AWAITABLE` oparg meanings in §3.*

**uvloop**
- [uvloop README](https://github.com/MagicStack/uvloop) — the "2-4x" claim, the benchmark chart, and the modern `uvloop.run()` usage. *Verdict: the usage section is current and the policy-based instructions elsewhere on the internet are not; trust this one.*
- Yury Selivanov, *uvloop: Blazing fast Python networking* — the design write-up, historically at `magic.io/blog/uvloop-blazing-fast-python-networking/`. **⚠️ That host failed DNS resolution during this session (`ENOTFOUND magic.io`) and I could not read it.** *Verdict: cited because it is the canonical design explanation and the README still links it; find a mirror or the archived copy before quoting it.*
- [libuv design overview](https://docs.libuv.org/en/v1.x/design.html). *Verdict: the right substitute for the above — read "The I/O loop" and compare it against §7's diagram.*

**Free-threading and asyncio**
- Kumar Aditya, *Scaling asyncio on Free-Threaded Python* (labs.quansight.org). **⚠️ HTTP 429 during this session; not read.** *Verdict: linked from the official docs as the source for the scaling claims. Go there for numbers — none of its numbers appear in this document.*
- [`26-free-threading.md`](26-free-threading.md) §13 — the sharing wall, which loop-per-thread does not exempt you from.

**Sibling docs**
- [`24-the-gil.md`](24-the-gil.md) §4, §7 — check points, and the convoy effect that `run_in_executor` walks you into.
- [`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md) — `co_flags`, exception tables, `co_stacksize`; §3 and §4 depend on it.
- [`20-eval-loop.md`](20-eval-loop.md) — frames, `DISPATCH_INLINED`, the eval breaker.
- [`09-syscalls-and-io.md`](09-syscalls-and-io.md) — `epoll`/`kqueue`/`io_uring`, level vs edge triggering; §9's foundations.
- [`31-measurement-methodology.md`](31-measurement-methodology.md) — read before believing any of §18's or §15's quoted speedups, including mine.

---

*Next: [`29-async-patterns-and-pitfalls.md`](29-async-patterns-and-pitfalls.md) — the same
machinery seen from production: backpressure and bounded queues, detecting a blocked loop
automatically, sync↔async bridges that don't deadlock, deadline propagation across
service boundaries, and where anyio and trio made different choices from the ones in §13
and §14.*
