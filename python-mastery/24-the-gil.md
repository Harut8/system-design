# 24 — The GIL: from the cache line to the eval loop

> **Tier 4, doc 24.** Prerequisites: `01-memory-hierarchy-and-caches.md` (cache lines,
> MESI, false sharing), `02-atomics-and-memory-models.md` (CAS, barriers, x86-TSO),
> `06-processes-threads-scheduling.md` (context switches, futexes),
> `15-refcounting-and-ownership.md` (borrowed vs owned refs).
>
> This is the vertical-slice document of the whole folder. It starts at the intercore
> bus and ends at `sys.setswitchinterval`, because **the GIL is not a design choice
> anyone would make today — it is the shadow cast by reference counting onto a cache-
> coherent multiprocessor.** Understand that sentence physically and every other GIL
> question answers itself.
>
> **Version baseline:** Python 3.14 (latest stable). Where behaviour changed across
> versions the version is named inline. Field names and code shapes in `ceval_gil.c`
> churn between releases — read this for the *mechanism*, then confirm against the
> source tree you actually ship.

## Contents

1. [The physical layer: why refcounting hates multicore](#1-the-physical-layer-why-refcounting-hates-multicore)
2. [What the GIL actually is](#2-what-the-gil-actually-is)
3. [A short history: the old GIL, the GIL battle, and Pitrou's rewrite](#3-a-short-history-the-old-gil-the-gil-battle-and-pitrous-rewrite)
4. [The eval loop: where the GIL is actually dropped](#4-the-eval-loop-where-the-gil-is-actually-dropped)
5. [What releases the GIL — and what doesn't](#5-what-releases-the-gil--and-what-doesnt)
6. [OS interaction: mutexes, condvars, futexes, and the scheduler](#6-os-interaction-mutexes-condvars-futexes-and-the-scheduler)
7. [The convoy effect, measured](#7-the-convoy-effect-measured)
8. [Signals, Ctrl-C, and fork](#8-signals-ctrl-c-and-fork)
9. [What the GIL does and does not guarantee](#9-what-the-gil-does-and-does-not-guarantee)
10. [Sub-interpreters: one GIL each (PEP 684 / PEP 734)](#10-sub-interpreters-one-gil-each-pep-684--pep-734)
11. [The Gilectomy: Larry Hastings' seven-core lesson](#11-the-gilectomy-larry-hastings-seven-core-lesson)
12. [What Sam Gross did differently (PEP 703)](#12-what-sam-gross-did-differently-pep-703)
13. [Free-threading's new cost model](#13-free-threadings-new-cost-model)
14. [C extensions under free-threading](#14-c-extensions-under-free-threading)
15. [Choosing a concurrency model](#15-choosing-a-concurrency-model)
16. [Diagnosing GIL problems in production](#16-diagnosing-gil-problems-in-production)
17. [The GIL elsewhere: other implementations](#17-the-gil-elsewhere-other-implementations)
18. [Lab exercises](#18-lab-exercises)
19. [Question bank](#19-question-bank)
20. [Sources](#20-sources)

---

## 1. The physical layer: why refcounting hates multicore

Start below Python. Below C. At the coherence protocol.

Every `PyObject` begins with a reference count. Every time Python touches an object —
loading a name, passing an argument, returning a value, iterating — that counter is
incremented and later decremented. In a hot loop this is *the* most frequently written
memory location in the process.

Now put two cores on it.

```
        Core 0                                    Core 1
     ┌──────────┐                              ┌──────────┐
     │  L1D     │                              │  L1D     │
     │ [None's  │                              │ [None's  │
     │  refcnt] │ ◀──── cache line, 64 bytes ──▶│  refcnt] │
     └────┬─────┘                              └─────┬────┘
          │                                          │
          └──────────────┬───────────────────────────┘
                         ▼
            ┌──────────────────────────┐
            │  coherence fabric (MESI) │   ← every write here is a
            │  L3 / interconnect       │     cross-core transaction
            └──────────────────────────┘
```

Under MESI, a cache line can be **M**odified, **E**xclusive, **S**hared, or **I**nvalid.
To *write* a line, a core must hold it in M or E state — which means invalidating every
other core's copy. So:

- Core 0 does `Py_INCREF(None)` → acquires the line exclusively, invalidates Core 1.
- Core 1 does `Py_INCREF(None)` → **request-for-ownership**, stalls until the line
  migrates across the interconnect, invalidates Core 0.
- Repeat, millions of times per second.

This is **cache line ping-ponging**. An L1 hit is ~4 cycles. A line bounced from another
core's L1 is ~40–100+ cycles, and across sockets on a NUMA machine, worse. The counter
is small; the *coherence traffic* is not.

Three separate costs stack here, and staff-level answers distinguish them:

| Cost | What it is | Roughly |
|---|---|---|
| **Atomicity** | `lock xadd` vs plain `add` — pipeline serialization, store-buffer drain | ~20–50 cycles even *uncontended* |
| **Coherence** | The line migrating between cores | ~40–300 cycles depending on topology |
| **Contention** | N cores serializing on one line — throughput scales as 1/N or worse | unbounded |

And crucially: **this happens even when the objects are logically read-only.** Two
threads merely *reading* `None`, `True`, small integers, a shared module dict, or a class
object still write to those refcounts. A pure-read workload generates pure-write
coherence traffic. That is the central perversity of reference counting on multicore, and
it is why "just make refcounts atomic" is not a solution — it is the thing that killed
the first serious attempt (§11).

> **Connect it downward:** this is the same phenomenon as false sharing
> (`01-memory-hierarchy-and-caches.md`), except it isn't false. It's *true* sharing —
> the threads genuinely contend for the same word. Padding cannot save you. Only *not
> writing* can.

**The same fact bites you in a completely different place.** `os.fork()` gives you
copy-on-write pages, and everyone expects the child to share the parent's memory for
free. It doesn't, because touching an object writes its refcount, which dirties the page,
which copies it. A "read-only" traversal of a large preloaded data structure in a forked
worker will steadily copy the entire structure. Refcount write-amplification defeats CoW
for exactly the reason it defeats multicore scaling: **there is no such thing as reading
a Python object.** (Covered in depth in `27-multiprocessing-and-subinterpreters.md`;
immortalization, §12.1, is the mitigation in both cases.)

---

## 2. What the GIL actually is

Given the above, the original CPython answer (1992, single-core era) is the cheapest
possible one: **allow only one thread to execute bytecode at a time, and then refcount
updates need no atomics at all.** Plain non-atomic `add` instructions. No coherence
traffic. No memory barriers. Single-threaded performance is optimal, and the entire
C-API becomes trivially thread-safe for free.

The GIL is therefore not a lock protecting a data structure. It is a **license to be
non-atomic everywhere else.**

Concretely, in CPython it is a small struct (see `Python/ceval_gil.c`, historically
`ceval_gil.h`) holding roughly:

| Field | Purpose |
|---|---|
| `locked` | atomic flag: is the GIL held? |
| `switch_number` | monotonically increasing counter, bumped on every handoff — used to detect "did a switch actually happen?" |
| `last_holder` | the `PyThreadState*` that most recently held it, so a waking thread can tell whether it was handed off to someone else |
| `mutex` | pthread mutex guarding the above |
| `cond` | pthread condition variable that waiters block on |
| `interval` | the switch interval (default **5 ms**) |

Plus, per-interpreter, the request flag:

| Field | Purpose |
|---|---|
| `gil_drop_request` | atomic flag: "somebody wants the GIL, please yield" |
| `eval_breaker` | the combined signal the eval loop checks |

**Scope note.** Since Python 3.12 (PEP 684) "the GIL" is not necessarily process-global:
each interpreter can own one. The struct above lives in `_PyRuntimeState` for the main
interpreter and in `PyInterpreterState` for interpreters created with their own GIL. In
the common single-interpreter process the distinction is invisible; §10 is where it
matters.

---

## 3. A short history: the old GIL, the GIL battle, and Pitrou's rewrite

The design in §2 is the *new* GIL — Antoine Pitrou's rewrite, landed in **Python 3.2
(2011)**. You need the old one to understand why the new one looks like that, and why
`switch_number` exists at all.

### The old GIL: a bytecode counter

Before 3.2, the holder decremented a **tick counter** and dropped the GIL every N
bytecode instructions. N defaulted to 100 and was tunable with `sys.setcheckinterval()`
(deprecated in 3.2, removed in **3.9**). Drop, signal the condvar, immediately try to
reacquire.

The failure is obvious once you look at it from the OS's point of view:

```
Thread A: drop_gil()  →  cond_signal  →  take_gil()   ← A is already running,
                                 │                       so it gets there first
                                 ▼
Thread B:  ...wakes up in the kernel, is placed on the run queue,
           eventually scheduled, checks `locked` → still taken → sleeps again
```

Signalling a condvar does not transfer the CPU. Thread A is *already on a core*; Thread B
has to be woken, queued, and scheduled — microseconds later. A reacquires uncontested. B
burns a full wake/sleep cycle for nothing, hundreds of thousands of times a second.

Dave Beazley's 2009–2010 talks made this concrete and famous. On a dual-core machine, a
CPU-bound workload of roughly 24.6 s sequentially took ~45 s split across two threads on
one core — and got **worse**, not better, when a second core was available (~68 s in his
measurements). Two cores made threaded Python nearly 3× slower than one thread. The
reason: with two cores both threads genuinely run at once, so both are constantly
fighting over the GIL instead of one merely waiting. This is the **GIL battle** (or "GIL
thrashing"): a convoy of futile wakeups burning system time.

Two second-order problems compounded it:

- **The counter measured the wrong thing.** 100 bytecodes is not a unit of time. 100 ×
  `LOAD_FAST` is nanoseconds; 100 × a `BINARY_OP` on 10,000-digit ints is seconds. The
  switch rate varied by orders of magnitude with workload.
- **Priorities were inverted.** An I/O thread that woke up had to wait for the CPU
  thread's tick counter, and then win a race against it.

### What Pitrou changed

The new GIL replaces "drop every N ticks" with "**yield when asked**":

1. A waiter blocks on the condvar with a **timeout** (`interval`, default 5 ms) instead
   of spinning on a counter.
2. On timeout, it checks `switch_number`. If the GIL changed hands while it waited,
   fine — the system is making progress, wait again. If it did **not**, the holder is
   monopolizing, so the waiter sets `gil_drop_request`.
3. The holder sees `gil_drop_request` at its next check point and yields.

This kills the battle: no drop happens unless someone actually wants the GIL, and the
timeout is real time rather than instruction count. `switch_number` exists purely to
answer "did anything happen while I was asleep?" — without it, every timeout would force
a drop even in a system that was already switching healthily.

What Pitrou **did not** add is fairness. There is still no handoff guarantee: the thread
that forced the drop is not guaranteed to be the one that gets the GIL. That omission is
the direct cause of §7.

> **The transferable lesson:** the old GIL optimized the wrong variable (switch
> *frequency*) using the wrong unit (bytecodes) and assumed signalling implies handoff.
> All three assumptions are ones you can make yourself in any lock design. The fix was
> not a better counter — it was changing *who initiates* the drop.

---

## 4. The eval loop: where the GIL is actually dropped

Here is the mechanism, and it is more elegant — and more fragile — than most people
expect. The GIL is **not** preemptively taken away by the OS. CPython is cooperatively
scheduled at the bytecode level.

### The handoff protocol

```
Thread A (holds GIL, running bytecode)      Thread B (wants GIL)
────────────────────────────────────        ─────────────────────────────
                                            take_gil():
                                              pthread_mutex_lock(&mutex)
                                              while (locked):
                                                ┌ pthread_cond_timedwait(
  ...executing bytecode...                      │   &cond, &mutex,
  (checks eval_breaker at                       │   interval /* 5ms */)
   designated check points —                    │
   one predictable branch)                      └ TIMED OUT, and
                                                  switch_number unchanged
                                                    ↓
                                             set gil_drop_request = 1   ──┐
                                                                          │
  eval_breaker is now set  ◀───────────────────────────────────────────────┘
    ↓
  eval_frame_handle_pending():
    if (gil_drop_request):
      _PyThreadState_Swap(NULL)
      drop_gil()   ─────────────────────────▶ cond_signal wakes B
      take_gil()   ← A now queues up            B sets locked, takes GIL,
                     behind B                   bumps switch_number
      _PyThreadState_Swap(tstate)
```

The code in `eval_frame_handle_pending()` is essentially (this is the ~3.10 shape; see
below for what changed):

```c
/* GIL drop request */
if (_Py_atomic_load_relaxed(&ceval2->gil_drop_request)) {
    /* Give another thread a chance */
    if (_PyThreadState_Swap(&runtime->gilstate, NULL) != tstate) {
        Py_FatalError("tstate mix-up");
    }
    drop_gil(ceval, ceval2, tstate);

    /* Other threads may run now */

    take_gil(tstate);

    if (_PyThreadState_Swap(&runtime->gilstate, tstate) != NULL) {
        Py_FatalError("orphan tstate");
    }
}
```

### Where the check actually happens — and where it doesn't

The folklore is "between every bytecode." That was roughly true historically; it is not
true now, and the difference matters.

Since 3.12–3.13 the eval breaker is a **per-thread-state bitfield**
(`tstate->eval_breaker`) packing several independent requests:

| Bit | Meaning |
|---|---|
| `_PY_GIL_DROP_REQUEST_BIT` | another thread wants the GIL |
| `_PY_SIGNALS_PENDING_BIT` | a signal arrived (§8) |
| `_PY_CALLS_TO_DO_BIT` | `Py_AddPendingCall` work queued |
| `_PY_ASYNC_EXCEPTION_BIT` | `PyThreadState_SetAsyncExc` |
| `_PY_GC_SCHEDULED_BIT` | cycle collection is due |

One atomic load tests all of them at once. But it is only tested at **designated check
points**, in practice:

- **`RESUME`** — emitted at function entry and after every `await`/`yield` resumption.
- **`JUMP_BACKWARD`** — every loop back-edge.
- instrumented variants of the above, plus a few explicit sites in the runtime.

Look at what the compiler actually emits:

```python
>>> import dis
>>> def f():
...     x = 0
...     for i in range(3):
...         x += 1
...     return x
>>> dis.dis(f)
  RESUME                   0        ← check point (function entry)
  LOAD_CONST               1 (0)
  STORE_FAST               0 (x)
  LOAD_GLOBAL              1 (range + NULL)
  LOAD_CONST               2 (3)
  CALL                     1
  GET_ITER
L1:
  FOR_ITER                 8 (to L2)
  STORE_FAST               1 (i)
  LOAD_FAST                0 (x)
  LOAD_CONST               3 (1)
  BINARY_OP               13 (+=)
  STORE_FAST               0 (x)
  JUMP_BACKWARD           10 (to L1)   ← check point (loop back-edge)
  ...
```

Two check points in the whole function. **Straight-line bytecode between them is
uninterruptible by the GIL machinery.** In practice every Python loop and every Python
call passes through one, so a thread executing Python code always yields eventually. The
consequence lives in what happens when it *isn't* executing Python code.

### Four consequences that follow directly

**1. The switch interval is a *timeout*, not a quantum.** A thread does not "get 5 ms of
CPU". Rather: a *waiting* thread waits 5 ms before it even asks. Then the holder yields
at its next check point. Total latency to switch = 5 ms + time to reach a check point +
scheduling latency (§6).

**2. A thread that never reaches a check point never yields.** This is why a single
long-running C call that holds the GIL freezes the entire interpreter — including signal
handling, including `Ctrl-C`. `re` on a pathological pattern, a huge `int`→`str`
conversion, `math.factorial` of something silly, a non-GIL-releasing extension: all of
them can make your process unresponsive with 15 idle cores available. There is no
preemption to save you. This is the single most common way a production Python process
goes fully dark.

**3. The check is deliberately, aggressively cheap.** One relaxed atomic load and a
well-predicted not-taken branch — a few cycles, mostly hidden by the out-of-order engine
(`00-cpu-execution-model.md`). The design pushes all cost onto the rare path, which is
also why the check points were *narrowed* over time: fewer sites, each cheaper, with no
loss of practical responsiveness.

**4. `_PyThreadState_Swap(NULL)` before dropping is not bookkeeping — it is the
invariant.** "Holding the GIL" and "having a current thread state" must be the same
thing. If they desync you get the `Py_FatalError("tstate mix-up")` you can see in the
source. This is why C extensions must use `Py_BEGIN_ALLOW_THREADS` /
`Py_END_ALLOW_THREADS` (which expand to exactly this save/restore dance) rather than
touching the GIL by hand.

### Tuning it

```python
import sys
sys.getswitchinterval()        # 0.005
sys.setswitchinterval(0.0001)  # seconds, float
```

Three things people get wrong about this API:

- It is a **global** setting for the interpreter, not per-thread. There is no way to say
  "this thread is latency-sensitive."
- It is a *floor on how long a waiter tolerates monopolization*, not a scheduling
  quantum. Lowering it does not give more CPU to anyone; it shortens the tax in §7.
- Lowering it too far is actively harmful (§6, §7). The optimum is workload-specific and
  must be measured, not reasoned about.

---

## 5. What releases the GIL — and what doesn't

§4 explains yielding under duress. The far more important case in real programs is
**voluntary release**: code that gives up the GIL because it is about to do something
that doesn't need the interpreter. This is the entire reason threaded Python is useful
for I/O, and the entire reason "just use threads for CPU work" fails.

The C-level idiom:

```c
Py_BEGIN_ALLOW_THREADS      /* expands to: { PyThreadState *_save = PyEval_SaveThread(); */
    result = some_blocking_or_expensive_c_call(...);   /* NO Python API in here. None. */
Py_END_ALLOW_THREADS        /* PyEval_RestoreThread(_save); } */
```

Between those macros the thread holds no GIL and has no thread state. Touching *any*
`PyObject` — including an innocent-looking `Py_DECREF` — is undefined behaviour, and the
crash usually lands somewhere unrelated much later.

### The table

| Releases the GIL | Notes |
|---|---|
| Blocking I/O: socket, file, `os.read`/`write`, `select`/`epoll` | the reason `threading` works for I/O at all |
| `time.sleep()` | including `sleep(0)`, which is a yield hint |
| `threading.Lock.acquire()`, `Condition.wait()`, `Queue.get()` | so lock contention is *not* GIL contention |
| `subprocess` / `os.waitpid` | |
| `hashlib` digests over ~2 KB | below the threshold it keeps the GIL — the release itself costs more than the hash |
| `zlib` / `bz2` / `lzma` compress & decompress on large buffers | |
| Most NumPy/SciPy array ops, BLAS calls | but **not** `dtype=object` arrays, and not tiny arrays where overhead dominates |
| `re` matching | **no** — see below |
| Well-written extensions: `lxml`, `Pillow`, `cryptography`, `orjson` (partly) | check each one; it is not automatic |

| Does **not** release the GIL | Consequence |
|---|---|
| Any pure-Python code | obviously — that's what the GIL protects |
| `re.match` / `re.search` on a pathological pattern | catastrophic backtracking freezes the whole process |
| Very large `int` ↔ `str` conversions | 3.11+ caps this by default (`sys.set_int_max_str_digits`) — that limit is partly a DoS fix and partly a GIL-freeze fix |
| Big `sorted()` / `list.sort()` with a Python key | key calls re-enter the eval loop, so it *does* yield; with a C-level comparison it may not |
| `json.dumps` of a huge structure (C accelerator) | one long GIL-holding call |
| Extensions that simply never call the macros | the default state of naive C/Cython code |

### The practical rule

> **A thread is useful in CPython exactly to the extent that it spends its time with the
> GIL released.** That is the whole model. Everything else — the convoy effect,
> `ThreadPoolExecutor` sizing, why `multiprocessing` exists — is downstream of that one
> sentence.

Two corollaries worth internalizing:

- **`ThreadPoolExecutor` for CPU-bound work in the GIL build is not a small
  inefficiency — it is negative value.** You pay context switches, cache thrash, and the
  §7 tax to get strictly less throughput than a `for` loop. Reach for
  `ProcessPoolExecutor`, a GIL-releasing extension, or a free-threaded build.
- **In Cython, `with nogil:` is how you say `Py_BEGIN_ALLOW_THREADS`**, and the compiler
  will refuse to let you touch Python objects inside it. That compile-time check is the
  single best ergonomic argument for Cython over hand-written C here.

---

## 6. OS interaction: mutexes, condvars, futexes, and the scheduler

Follow `pthread_cond_timedwait` down one more layer.

On Linux, a pthread mutex is a **futex** (fast userspace mutex). Uncontended, locking is
a single CAS in userspace — no syscall, ~20 cycles. Contended, the thread calls
`futex(FUTEX_WAIT)`, which parks it in the kernel and removes it from the run queue.
Waking is `futex(FUTEX_WAKE)`.

So a GIL handoff, in the contended case, costs:

```
  drop_gil → cond_signal → futex(FUTEX_WAKE)          ~ syscall, 100s of ns
    ↓
  kernel marks Thread B runnable, places on run queue
    ↓
  EEVDF/CFS decides when B actually runs               ~ scheduling latency, µs–ms
    ↓
  context switch: save/restore registers,              ~ 1–5 µs direct
    switch page tables (or not, same process),
    TLB and cache warm-up on the new core              ~ 10s of µs *indirect*
    ↓
  Thread B resumes in take_gil()
```

Three things staff-level engineers should take from this:

**The indirect cost dominates.** The direct register save/restore is small. The expensive
part is the cold cache and TLB on the new core — the thread's working set is somewhere
else now. This is why setting the switch interval very low backfires (see the table
in §7: below ~10 µs, throughput *drops*).

**The OS scheduler is not cooperating with you.** It knows nothing about the GIL. It may
place the woken thread on a different core, a different NUMA node, or an SMT sibling of a
busy core. It may not run it immediately at all. CPython requests a wakeup; the kernel
decides. Under cgroup CPU quota (`06-processes-threads-scheduling.md`), it may be
throttled entirely — which is a classic source of "GIL contention" that is actually
container throttling. **Check `cpu.stat`'s `nr_throttled` before you blame the GIL.**

**There is no fairness guarantee.** The condvar wait queue plus the drop-request
mechanism admits starvation: a thread can request the GIL, have the holder yield, and
then lose the reacquisition race to a third thread — or even to the thread that just
dropped it. This is a known unfairness in the design, and it is the seed of §7.

---

## 7. The convoy effect, measured

This is the most important practical GIL pathology, and Dave Beazley's presentation of it
(2009–2010, and [bpo-7946](https://bugs.python.org/issue7946)) is the canonical treatment.
Note that this is a pathology of the **new** GIL — Pitrou's fix for the GIL battle (§3)
created it.

**The setup:** one I/O-bound thread (a socket server) plus some CPU-bound threads.

**The trap:** an I/O-bound thread has an *extremely* short GIL residency — it wakes,
does a few bytecodes, hits a socket call, and releases the GIL. That is exactly the
behaviour you want to reward. Instead:

1. I/O thread completes its `recv`. It calls `take_gil()`.
2. The CPU-bound thread holds the GIL and is happily running bytecode.
3. The I/O thread must wait the **full switch interval (5 ms)** before it is even allowed
   to *set* `gil_drop_request`.
4. Only then does the CPU thread yield — and the I/O thread might still lose the race.
5. The I/O thread runs for microseconds, blocks on I/O again, and the whole cycle repeats.

The latency-sensitive thread is penalized precisely *because* it is well-behaved. A
5 ms tax on every single I/O completion.

Measured throughput of an echo-style server (requests/sec), varying switch interval and
CPU-bound thread count:

| Switch interval (s) | 0 CPU threads | 1 CPU thread | 2 CPU threads | 4 CPU threads |
|---|---|---|---|---|
| 0.1 | 30,000 | 5 | 2 | 0 |
| 0.01 | 30,000 | 50 | 30 | 15 |
| **0.005 (default)** | **30,000** | **100** | **50** | **30** |
| 0.001 | 30,000 | 500 | 280 | 200 |
| 0.0001 | 30,000 | 3,200 | 1,700 | 1,000 |
| 0.00001 | 30,000 | 11,000 | 5,500 | 2,800 |
| 0.000001 | 30,000 | 10,000 | 4,500 | 2,500 |

Read that first data column against the second. **30,000 → 100 RPS from adding one
CPU-bound thread.** A 300× collapse. Not a 2× slowdown from sharing a core — a
*three-orders-of-magnitude* collapse, caused entirely by the switch-interval tax.

Then read the last row: pushing the interval to 1 µs makes things *worse* than 10 µs,
because now context-switch cost (§6) dominates. There is an optimum, it is workload-
specific, and it is roughly 100–500× smaller than the default.

**Why the default is still 5 ms.** Because the trade is real in the other direction: a
small interval taxes *throughput* workloads with switch overhead, and the vast majority
of Python processes are not mixed I/O + CPU. 5 ms is a defensible default for the common
case and a terrible one for yours. That is a tuning parameter doing its job, not a bug.

**What to do about it in production:**

- **Don't mix CPU-bound and latency-sensitive I/O threads in one interpreter.** This is
  the real fix. Separate processes, or push CPU work into a GIL-releasing extension.
- `sys.setswitchinterval(0.0001)` is a legitimate mitigation for I/O-latency-sensitive
  services — measure it, don't cargo-cult it, and re-measure after any dependency bump.
- **Recognize the signature:** p99 latency quantized near multiples of 5 ms, with low
  overall CPU utilisation, is a GIL convoy fingerprint. If your latency histogram has a
  suspicious cliff at 5 ms, this is your first hypothesis. §16 is how you confirm it.
- **asyncio is not immune.** The event loop is one thread, so coroutines never fight each
  other for the GIL — but the moment you add a `ThreadPoolExecutor` for "blocking" work
  that turns out to be CPU-bound, the loop thread becomes the I/O-bound victim in exactly
  the scenario above, and your whole service's tail latency quantizes to the switch
  interval.

---

## 8. Signals, Ctrl-C, and fork

Two adjacent behaviours that people file under "weird Python threading bugs" and are
really just §4 and §5 seen from a different angle.

### Signals

A POSIX signal can be delivered to any thread, but Python's signal handlers are Python
functions — they need the GIL and a thread state. So CPython does this:

1. The real C signal handler (`signal_handler` in `Modules/signalmodule.c`) does almost
   nothing: it records which signal arrived in a flag array and sets
   `_PY_SIGNALS_PENDING_BIT` on the eval breaker. It is async-signal-safe by being
   trivial.
2. **Only the main thread** of the main interpreter runs the Python-level handler, at its
   next eval-breaker check point.

Three consequences:

- **`Ctrl-C` requires the main thread to be executing Python bytecode.** If it is blocked
  in a C call that holds the GIL (§5's second table), nothing happens — the flag is set
  and never read. This is the "Ctrl-C does nothing" experience, and it is why `Ctrl-C`
  works fine while the main thread is in `time.sleep()` (GIL released, interruptible)
  but not while it is in a runaway regex.
- **`KeyboardInterrupt` lands wherever the main thread happens to be**, which is
  effectively a random line. Code that must be interrupt-safe cannot assume a clean point.
- **Worker threads cannot be interrupted this way at all.** There is no "cancel this
  thread" in Python. `PyThreadState_SetAsyncExc` exists (it sets
  `_PY_ASYNC_EXCEPTION_BIT`) and is what `ctypes`-based "kill thread" recipes use, but it
  suffers the same limitation — the target must reach a check point — and it can leave
  locks held and `finally` blocks unrun. Do not build on it. Use a cooperative
  cancellation flag, or a process.

`signal.set_wakeup_fd()` is the escape hatch that lets an event loop learn about a signal
via its selector rather than via the eval breaker; it is how asyncio handles signals.

### fork

`os.fork()` copies only the calling thread. Every mutex held by any *other* thread at the
moment of the fork stays locked forever in the child, with no owner to release it.

The GIL itself is handled — `PyOS_AfterFork_Child()` reinitializes it and the runtime's
own locks, and re-registers the child's thread state as the main thread. What is *not*
handled is every other lock in your process: `logging`'s handler locks, an allocator's
internal locks inside a C library, a connection pool's mutex, `random`'s state lock. The
child deadlocks the first time it touches one.

This is why:

- **Python 3.12 added a `DeprecationWarning`** when `os.fork()` is called in a process
  with multiple threads. Take it seriously; it is a real bug class, not lint.
- **Python 3.14 changed `multiprocessing`'s default start method on Linux from `fork` to
  `forkserver`.** macOS moved to `spawn` back in 3.8. If you have code that silently
  relied on inheriting state through `fork`, 3.14 breaks it — and that break is the point.
- `os.register_at_fork()` exists for libraries that must reinitialize state; use it rather
  than hoping.

The connection to §1: even when fork *works*, refcount write-amplification erodes the CoW
saving that motivated using it.

---

## 9. What the GIL does and does not guarantee

### It does guarantee

- One thread executes bytecode at a time.
- A single bytecode instruction implemented entirely in C, that does not call back into
  Python, completes without another Python thread interleaving.
- Reference counts do not get corrupted.
- Interpreter-internal structures stay consistent.

### It does not guarantee

**Your multi-bytecode operation is atomic.** The universal example:

```python
x += 1        # LOAD_FAST / LOAD_CONST / BINARY_OP / STORE_FAST
```

Four instructions, three yield opportunities. Two threads doing this a million times each
will lose updates. This is the difference between a *data race* (memory corruption at the
hardware level — the GIL does prevent this) and a *race condition* (logical interleaving
— the GIL does nothing).

**Anything calling back into Python is atomic.** `d[k] = v` on a plain dict with a
`str` key is effectively atomic. On a dict whose key has a Python-level `__hash__`, it
is not — the interpreter re-enters the eval loop and can yield mid-operation. The same
applies to `__eq__`, `__index__`, `__del__`, and any `@property`.

**Your `__del__` runs at a predictable time or on a predictable thread.** A refcount can
hit zero on any thread, so a finalizer can execute on a thread that has never heard of
that object, holding locks that thread didn't expect to interact with. This is a real
deadlock source; see `22-garbage-collection.md`.

### The atomicity table — and the trap

| Operation | GIL build | Free-threaded build |
|---|---|---|
| `lst.append(x)` | atomic | atomic (per-object lock) |
| `d[k] = v` (builtin key type) | atomic | atomic (per-object lock) |
| `x += 1` (int) | **not atomic** | **not atomic** |
| `d[k] += 1` | **not atomic** | **not atomic** |
| `lst[i] = lst[j]` | atomic | atomic |
| `if k not in d: d[k] = v` | **not atomic** | **not atomic** |
| `obj.attr += 1` | **not atomic** | **not atomic** |
| `lst.sort()` with a Python `key=` | **not atomic** | **not atomic** |

> **The trap this table exists to spring:** notice the free-threaded column is *the same*.
> People expect removing the GIL to break their code. Mostly it doesn't — because the
> things that were atomic were atomic due to *C-level indivisibility*, which per-object
> locking preserves, and the things that weren't atomic were already broken. Free-
> threading doesn't create new race conditions in Python code so much as it **raises the
> probability of ones you already had** from "once a month in prod" to "immediately".
>
> The genuinely new hazards live in **C extensions** (§14), not in Python-level code.

**Never reason from this table in application code.** It documents an implementation
detail, not a language guarantee, and it has shifted before (dict internals changed in
3.6, 3.11 and 3.14). Use a `Lock`, or `itertools.count()`, or an immutable
accumulate-then-merge pattern. The table is for *reading other people's code* and for
diagnosing incidents — not for writing new lock-free code.

---

## 10. Sub-interpreters: one GIL each (PEP 684 / PEP 734)

There is a third answer to "the GIL limits me to one core", between `multiprocessing` and
free-threading, and it is easy to miss because it took a decade to become usable.

**PEP 684 (Python 3.12)** made the GIL **per-interpreter**. Everything in §2's struct
moved from process-global to `PyInterpreterState` for interpreters created with
`PyInterpreterConfig.gil = PyInterpreterConfig_OWN_GIL`. **PEP 734 (Python 3.14)** put a
supported Python API on top: the `concurrent.interpreters` module.

```python
from concurrent import interpreters

interp = interpreters.create()
interp.exec("import math; print(math.factorial(20))")
```

### Why this is not just "threads with extra steps"

Each interpreter has its own module state, its own `sys.modules`, its own builtins, its
own GC, its own allocator arenas — and, critically, **its own object graph**. Two
interpreters never share a `PyObject`, which is precisely why they can run bytecode
simultaneously on different cores without any of §1's coherence problem. The isolation
*is* the mechanism.

The cost model sits neatly between the other two:

| | Threads (GIL build) | Sub-interpreters | Processes |
|---|---|---|---|
| Parallel bytecode | ✗ | ✓ | ✓ |
| Startup | ~50 µs | ~ms (re-imports modules) | ~10–100 ms (spawn) |
| Memory per unit | ~8 MB stack | interpreter + its own copy of every imported module | full process |
| Sharing objects | free | **impossible** | impossible |
| Sharing data | free | buffers (zero-copy) + pickled values via `Queue` | pickle / `shared_memory` |
| Crash blast radius | whole process | whole process | one process |
| C extension support | universal | **requires multi-phase init (PEP 489) + `Py_mod_multiple_interpreters`** | universal |

### The two traps

**1. Memory does not amortize the way you expect.** Each interpreter imports its own
copy of every module. Ten interpreters each importing NumPy and Pandas is close to ten
times the module-level memory of one — you avoided the *process* overhead, not the
*import* overhead. Immortal statics and some interned strings are shared; your third-party
dependency tree is not.

**2. The extension gate is the real blocker, and it's the same shape as free-threading's.**
An extension must use multi-phase initialization and declare
`Py_mod_multiple_interpreters` support, or importing it in a sub-interpreter raises
`ImportError`. Compare this with §14's `Py_mod_gil`: both projects ended up needing the
ecosystem to explicitly declare "I hold no process-global mutable state." That is not a
coincidence — it is the same latent bug being surfaced by two different mechanisms.

Where sub-interpreters win over processes: no serialization boundary for buffers, much
faster startup, and a single process to deploy and observe. Where they lose: no fault
isolation, a much smaller compatible-extension universe, and a memory profile that
surprises people. Full treatment in `27-multiprocessing-and-subinterpreters.md`.

---

## 11. The Gilectomy: Larry Hastings' seven-core lesson

Larry Hastings' Gilectomy (2015–2018) is the most instructive failure in CPython's
history, and every engineer who says "just remove the GIL" should be made to read it.

### Attempt 1: make refcounts atomic

The obvious approach. Replace `++obj->ob_refcnt` with an atomic increment.

**Result: roughly a 30% slowdown — and it got worse with more threads.**

Exactly §1, playing out in production code. The atomic RMW instructions destroyed cache
consistency and flooded the intercore bus with coherence traffic. The GIL wasn't just
protecting refcounts; its absence turned every refcount into a cross-core transaction.

Note the shape of the failure: *the slowdown scaled the wrong way.* Adding cores made it
worse. That is the signature of a coherence problem rather than a lock-contention problem
— and telling those two apart from a flame graph is a genuine staff-level skill. (Lock
contention shows up as time parked in futex waits; coherence shows up as stalled cycles
and rising `mem_load_l3_miss_retired` / `HITM` counters with *no* corresponding wait time.)

### Attempt 2: buffered reference counting

Hastings borrowed **buffered reference counting** from GC research: don't update the
refcount at all. Append the operation to a **thread-local log** and reconcile later. No
shared writes → no coherence traffic.

`Py_INCREF` became:

```c
static inline void Py_INCREF(PyObject *o)
{
    PyRefLog *rl = PyThread_get_key_value(PyRefLogTLSKey);
    if (PyRefPad_IsFull(rl)) {
        PyRefLog_Rotate(rl);
    }
    PyRefPad_Write(rl->incref, o);
}
```

Two details worth pausing on:

- `Py_DECREF` uses a **separate** log (`rl->decref`). Increments and decrements must be
  replayed respecting order, or an object could be freed while still referenced. Ordering
  is not optional even when the counting is deferred.
- The logs are thread-local (`PyThread_get_key_value`) *specifically* to dodge contention.
  The first version had a shared log; contention on the log itself forced segregation by
  thread and by operation.

Compare the cost of that `Py_INCREF` with the original. The original was one non-atomic
`add` on a line already in L1. The replacement is a TLS lookup, a bounds check, a
predictable branch, and a store to a log that will later be walked again. **The fast path
got several times more expensive so that the slow path could get cheaper** — and in
CPython the fast path runs billions of times.

### The result

Hastings' October 2016 measurement: the buffered-refcount build reached **performance
parity with stock CPython — while using about seven CPU cores to match stock CPython on
one.**

That number is the entire lesson. Not "it was 30% slower". **Seven cores to break even.**

### Why it ultimately didn't ship

By 2018 the direction under consideration was replacing refcounting with a tracing
garbage collector and breaking the C API — an enormous ecosystem cost. It didn't happen.

**The three transferable lessons:**

1. **You cannot bolt thread-safety onto a design whose hot path assumes single-threaded
   mutation.** The refcount is not an implementation detail; it is the architecture.
2. **Deferring work does not delete it.** Buffered refcounting moved the cost from
   coherence traffic to log processing, memory pressure, and reconciliation. It traded
   one bottleneck for another.
3. **"Works, but needs 7× the hardware" is a failure.** Systems work that doesn't respect
   the single-threaded baseline gets rejected — correctly.

---

## 12. What Sam Gross did differently (PEP 703)

The accepted approach (Sam Gross, `nogil` → PEP 703, accepted Oct 2023) succeeded because
it attacked the problem from **five directions simultaneously**, rather than trying to
make one mechanism carry it.

The structural insight: *there is no single replacement for the GIL, because objects are
not all alike.* Sort objects by how they're actually used, and each class gets a different
— cheaper — treatment. Tier 1 pays nothing, tier 2 pays a branch, tier 3 pays at GC time,
and only what's left pays for a lock.

### 12.1 Immortalization — the "don't count at all" tier

The hottest refcounts belong to objects that never die: `None`, `True`, `False`, small
ints, interned strings, statically allocated type objects. PEP 703 marks these by setting
the local refcount field to a sentinel:

```
ob_ref_local == UINT32_MAX   →   immortal
```

`Py_INCREF` and `Py_DECREF` become **no-ops** for them. Per the PEP: *"This avoids
contention on the reference count fields of these objects when multiple threads access
them concurrently."*

You can see this from Python, on any 3.12+ build:

```python
>>> import sys
>>> sys.getrefcount(None)
4294967295          # UINT32_MAX — not a leak, an immortal marker
```

This is the single highest-leverage change. Go back to §1: the pathological case was N
cores ping-ponging `None`'s refcount. Immortalization deletes that case entirely — those
lines can now sit in **Shared** MESI state on every core forever, read-only, never
invalidated. It also fixes the fork/CoW problem from §1's coda, which is why it landed
in the GIL build too.

(Related but distinct from **PEP 683**, adopted in 3.12, which introduced immortal
objects for the GIL build; PEP 703 uses a different bit representation so it composes
with biased and deferred refcounting. Note the cost side: immortality is a *branch* in
`Py_DECREF` that every object now pays, which is a small but real regression in the GIL
build — accepted because the CoW and contention wins dominate.)

### 12.2 Biased reference counting — the "count locally" tier

Borrowed from Swift. Split the count in two:

| Field | Written by | How |
|---|---|---|
| `ob_ref_local` | the **owning** thread only | plain non-atomic instructions |
| `ob_ref_shared` | any other thread | atomic instructions |
| `ob_tid` | — | the owning thread's id, for the fast-path check |

The fast path — object created and used by one thread, which is the overwhelming majority
of objects — is `if (ob_tid == current_thread_id)` then a **non-atomic** increment.
Same instruction as the GIL build. Zero coherence traffic.

This answers the natural objection ("doesn't checking ownership cost a field and a
branch?") — yes, and it is worth it: one predictable, perfectly-predicted branch and an
L1 hit on a line you were about to touch anyway, versus a cross-core `lock xadd`. The
branch predictor makes the check nearly free; nothing makes the atomic free.

When a second thread touches the object, it *escapes*: it is marked shared, and both
counts are eventually merged. **Escape is one-way and permanent for that object.** An
object that gets handed to another thread once is on the slow path for the rest of its
life — which is why §13's "sharing wall" is about object *graphs*, not about momentary
contention.

### 12.3 Deferred reference counting — the "count later" tier

For objects that are read constantly from many threads but rarely die — top-level
functions, modules, classes, heap types — even biased refcounting escapes to the shared
path because they're genuinely cross-thread. Deferred refcounting skips refcount updates
for these when the reference lives only on the interpreter stack, and reconciles during
GC by scanning stacks.

The trade: it removes the hottest remaining atomics, but it makes the cycle collector's
job mandatory rather than opportunistic — an object with deferred references cannot be
freed by refcount alone. That is one reason §12.5 exists.

### 12.4 Per-object locking, QSBR, and mimalloc

Container mutations take a **per-object lock**. Reads mostly don't. Sam Gross estimated
this "automatic fine-grained locking" at about **1.5% overhead**.

Lock-free reads raise an immediate question: if a reader is walking a dict's key table
with no lock, what stops a concurrent writer from resizing and freeing that table out
from under it? The answer is **QSBR** (Quiescent State Based Reclamation, borrowed from
FreeBSD, in `Python/qsbr.c`). Memory that a lock-free reader might still be pointing at
isn't freed immediately — it goes on a deferred list (`_PyMem_FreeDelayed`) and is
reclaimed only once every thread has passed through a quiescent state, proving no reader
can still hold a pointer to it. It is the same idea as RCU in the Linux kernel. The cost
is bounded memory latency: freed memory stays allocated a little longer.

This is where **mimalloc** earns its place: it isn't just "a thread-safe allocator" (as an
LWN commenter correctly noted, any serious allocator is). It was chosen because its
internal structure — size-segregated heaps and pages — lets the GC **traverse all objects
without a global registry**, and lets lock-free readers find an object's metadata safely.
The allocator choice is load-bearing for the *GC design*, not just for allocation speed.

### 12.5 Stop-the-world for cycle collection

The cycle collector needs stable refcounts. Under the GIL it got that for free. Without
it, PEP 703 pauses all Python-executing threads. Per the PEP: **two stop-the-world pauses
per collection** — one to find cyclic trash, one after finalizers to confirm what's still
unreachable — with threads **resumed before** finalizers and `tp_clear` run, specifically
to avoid introducing deadlocks that don't exist under the GIL.

So: free-threaded Python has real stop-the-world GC pauses that the GIL build does not.
That is a genuine, new, workload-dependent latency cost, and it scales with thread count
(every thread must reach a safepoint before the pause can begin — the same "slowest
thread sets the pace" problem every STW runtime has). See `22-garbage-collection.md`.

### 12.6 The design is almost entirely borrowed — and that's the point

Sam Gross's own list of provenance:

| Component | Taken from |
|---|---|
| Biased reference counting | Swift |
| mimalloc | Koka / Lean |
| Internal lock design | WebKit ([locking in WebKit](https://webkit.org/blog/6161/locking-in-webkit/)) |
| QSBR (`Python/qsbr.c`) | FreeBSD |
| Interpreter (register-accumulator model, fast calls) | V8 Ignition, LuaJIT |
| Stop-the-world implementation | Go's runtime |

The Gilectomy tried to invent a mechanism. PEP 703 assembled proven ones. That contrast
is the meta-lesson of this whole document.

---

## 13. Free-threading's new cost model

### Where it stands (verify against your interpreter — this moves)

- **Officially supported since 3.14** (PEP 779 phase II), experimental in 3.13. Still
  **not the default build**. "GIL off by default" is phase III, with no committed date.
- **Single-threaded overhead, per the official docs:** on pyperformance, from about
  **1% on macOS aarch64 to 8% on x86-64 Linux**. Note the platform spread — it reflects
  how much the weaker-ordered ARM memory model and different cache topology change the
  cost of the same code.
- **Ignore benchmark numbers from 2024.** 3.13's free-threaded build ran with the
  adaptive specializing interpreter largely disabled (specialization wasn't thread-safe
  yet), which dominated its reported overhead. 3.14 re-enabled thread-safe specialization.
  Any figure you find that predates that describes a build nobody should benchmark now.
- Check at runtime with `sys._is_gil_enabled()` (3.13+). Free-threaded builds carry the
  `t` ABI tag (`cp314t`) and define `Py_GIL_DISABLED`:

```python
import sys, sysconfig
sys._is_gil_enabled()                          # False on a free-threaded build...
sysconfig.get_config_var("Py_GIL_DISABLED")    # ...but this tells you which BUILD you're on
```

  Those two can disagree — a free-threaded build can be *running with the GIL on*, because
  of `-X gil=1` / `PYTHON_GIL=1`, or because an extension re-enabled it at import (§14).
  When someone reports "free-threading didn't help", this is the first thing to check.

### The cost model actually changed shape

| | GIL build | Free-threaded build |
|---|---|---|
| Refcount update (own thread) | non-atomic | non-atomic (biased fast path) |
| Refcount update (shared object) | non-atomic | **atomic + coherence traffic** |
| `None`/small ints/interned strs | non-atomic write | **free (immortal)** |
| Container mutation | free (GIL) | **per-object lock (~1.5%)** |
| Cycle GC | no STW pause | **2 STW pauses per collection** |
| Object header | 16 bytes | **larger** (see `16-object-memory-layout.md` §2) |
| Scaling limit | 1 core of bytecode | **shared-object refcount contention** |

**The new scaling wall is object sharing.** Not the GIL — *sharing*. A workload where
threads work on disjoint object graphs scales beautifully. A workload where all threads
hammer one shared dict or one shared instance will hit refcount escape, shared-count
atomics, coherence ping-pong, and per-object lock contention — and may scale barely
better than under the GIL, while paying the single-threaded tax.

This is the most important practical takeaway in the document: **removing the GIL moved
the bottleneck from "one lock" to "the memory system", and the memory system's rules are
the ones in `01` and `02`.** You are now writing code whose performance is governed by
cache coherence. That is why Tier 0 comes first in this roadmap.

### The design rules that follow

Once sharing is the bottleneck, the optimizations are the ones you'd apply in any
shared-memory language:

- **Partition the data, not the work.** Give each thread its own slice of the object graph
  and merge at the end. This keeps biased refcounting on its fast path.
- **Share immutable, share once.** Objects read by all threads should be immortal or
  effectively so — module-level constants, frozen dataclasses created before the threads
  start. What kills you is the *shared mutable dict* every worker writes to.
- **A shared counter is a shared cache line.** The Python-level fix is per-thread counters
  summed at the end — literally §1's lab exercise, in Python.
- **Measure with thread count, not just wall time.** The diagnostic signature of a
  coherence problem is that adding threads makes per-thread throughput fall
  super-linearly. If 8 threads give you 2× of 1 thread, you have a sharing problem, not a
  "Python is slow" problem.

---

## 14. C extensions under free-threading

This is where the genuinely new breakage lives, and it deserves its own section because
almost every real free-threading migration failure is here rather than in Python code.

### The opt-in gate

```c
static PyModuleDef_Slot module_slots[] = {
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},   /* "I am free-threading safe" */
    {0, NULL}
};
```

An extension without this slot causes CPython to **re-enable the GIL at runtime when it
is imported** (unless `PYTHON_GIL=0` forces it off). It works, it warns, and it silently
erases the entire benefit of running a free-threaded build. `PyUnstable_Module_SetGIL()`
is the equivalent for single-phase-init modules.

**This is a performance cliff you must monitor for.** One transitive dependency updating
into a non-declaring build, and your carefully migrated service quietly reverts to
GIL-build behaviour with free-threading's single-threaded tax still applied — the worst
of both. Assert on `sys._is_gil_enabled()` at startup in production.

### What actually breaks

- **The GIL was your module's mutex, and you didn't know it.** Any module-level mutable
  C state — a cache, a counter, a lazily-initialized static, an interned-string table —
  was protected for free. It no longer is. This is the dominant bug class.
- **Borrowed references become significantly more dangerous.** `PyList_GetItem` returns a
  borrowed reference; under free-threading the list can be mutated concurrently and the
  object freed before you use it. Prefer the strong-reference APIs: `PyList_GetItemRef`,
  `PyDict_GetItemRef`, `PyObject_GetOptionalAttr`. Most of these were added in 3.13
  precisely for this.
- **"Atomic because it's one C call" no longer implies "atomic across two C calls."**
  `PyDict_Contains` followed by `PyDict_SetItem` is a race; it always was, but the GIL
  made the window zero.

### Critical sections

The supported tool for "I need this object stable across several operations":

```c
Py_BEGIN_CRITICAL_SECTION(obj);
    /* obj's per-object lock is held */
Py_END_CRITICAL_SECTION();

Py_BEGIN_CRITICAL_SECTION2(a, b);   /* two objects, deadlock-ordered for you */
Py_END_CRITICAL_SECTION2();
```

These compile to no-ops in the GIL build, so one source tree serves both.

**The subtlety that catches people:** a critical section is *not* a plain mutex. If the
thread suspends inside it — blocking on another lock, or being stopped for a
stop-the-world GC pause — the critical section is **released and reacquired**. That is
deliberate: it makes deadlock structurally impossible, which is why the API can be
applied mechanically across the interpreter. But it means **your invariants can be broken
across any suspension point inside the section**. Critical sections give you atomicity
against other threads doing ordinary work; they do not give you a transaction.

---

## 15. Choosing a concurrency model

The decision most teams actually need, with the GIL as one input among several.

| | Best for | Parallel CPU | Isolation | Data sharing | Main cost |
|---|---|---|---|---|---|
| **asyncio** | many concurrent I/O ops, high connection counts | ✗ | none | free | one blocking call stalls everything; async ecosystem lock-in |
| **Threads (GIL build)** | blocking I/O, GIL-releasing native calls | ✗ | none | free | §7 convoy; ~8 MB stack each |
| **Threads (free-threaded)** | CPU work over shared data structures | ✓ | none | free | 1–8% single-thread tax; sharing wall (§13); extension support |
| **Sub-interpreters** | CPU work, isolated, low startup | ✓ | partial | buffers + pickle | per-interpreter imports; extension support (§10) |
| **Processes** | CPU work, untrusted or crash-prone code | ✓ | full | pickle / `shared_memory` | startup, memory, serialization |
| **Native extension w/ `nogil`** | numeric / bulk data work | ✓ | none | free | you have to write it |

### How to actually decide

1. **Measure first.** Is the process CPU-bound or I/O-bound? `py-spy top` for a minute
   answers this and is the step people skip. Most "GIL problems" are neither.
2. **If it's I/O-bound:** threads or asyncio. The GIL is not your problem; §7 might be.
3. **If it's CPU-bound and the hot loop is numeric:** push it into NumPy / a Rust or C
   extension that releases the GIL. This is almost always the highest return per hour of
   effort, and it works on every Python you'll ever deploy to.
4. **If it's CPU-bound, pure Python, and embarrassingly parallel:** processes. Boring,
   universally supported, isolates crashes.
5. **If it's CPU-bound, pure Python, and needs a large shared working set** (the case
   where pickling dominates): this is exactly what free-threading is for. Check your
   extension dependencies first, then measure the single-threaded regression, then measure
   scaling.

> Note the ordering: **free-threading is fifth, not first.** It is the right answer for a
> real and previously unserved case — parallel CPU work over a shared object graph too
> large to copy — and the wrong answer for most workloads that merely *feel* GIL-bound.

---

## 16. Diagnosing GIL problems in production

§7 tells you the fingerprint. This is how you confirm it rather than guess.

**`py-spy` is the first tool, always.** It attaches to a running process without
modifying it.

```bash
py-spy top --pid 1234              # live view; the %GIL column is the whole answer
py-spy top --pid 1234 --gil        # only sample threads that currently hold the GIL
py-spy dump --pid 1234             # stack of every thread, right now
py-spy record --pid 1234 -o p.svg --idle   # flamegraph including waiting threads
```

Read it like this:

| Observation | Diagnosis |
|---|---|
| One thread at ~100% GIL, others near 0 | classic GIL saturation — you are CPU-bound in Python |
| Total GIL% high, spread across threads | GIL-bound; free-threading or processes will help |
| Total GIL% low, wall time high | **not the GIL** — you're blocked on I/O, a lock, or the scheduler |
| Every thread parked in the same C call | a native library serializing internally |
| Process CPU capped well below the limit | check cgroup throttling (§6) before anything else |

**`gil_load`** (Chris Billington) measures GIL wait/held fractions from inside the process
and gives you a number to alert on, rather than an impression.

**Confirm the convoy effect specifically** by perturbing the variable and watching the
outcome — this is the cheapest decisive experiment in the whole document:

```python
import sys
sys.setswitchinterval(0.0001)     # 50× smaller
```

If p99 latency drops sharply, you had a convoy. If nothing changes, stop blaming the GIL
and go look at §6's scheduler and cgroup causes. A hypothesis that survives a 50×
perturbation of its supposed cause was never the explanation.

**Other signals worth wiring up:**

- Latency histograms with enough resolution to see quantization at 5 ms. If your
  histogram buckets are `[1ms, 10ms, 100ms]` you cannot see this at all — that's a
  monitoring bug that hides a class of production bug.
- `sys._is_gil_enabled()` asserted at startup on free-threaded deployments (§14).
- `perf stat -e cache-misses,mem_load_l3_miss_retired.remote_hitm` when you suspect §1's
  coherence problem rather than lock contention. Coherence problems show *stalls without
  waits*; lock contention shows *waits*.
- `PYTHONFAULTHANDLER=1` plus `faulthandler.dump_traceback_later()` to catch the §4
  consequence-2 case where the process goes fully dark and even `py-spy` can't tell you
  much beyond "it's in C."

---

## 17. The GIL elsewhere: other implementations

Useful because it isolates the variable: implementations without refcounting mostly don't
have this problem, which is the strongest available evidence for §1's central claim.

| Implementation | GIL? | Why |
|---|---|---|
| **CPython** | yes (optional since 3.13) | reference counting |
| **PyPy** | yes | also refcounts at the RPython level; its STM branch (2014–2016) reached working parity at ~2× single-thread overhead and was abandoned for lack of funding and complexity — a second data point for §11's lesson |
| **Jython** | **no** | JVM GC, no refcounts; true threading, and it inherits Java's memory model |
| **IronPython** | **no** | .NET GC, same reasoning |
| **GraalPy** | **no** for pure Python | JVM/Truffle GC; falls back to a GIL-like lock when running native extensions through its C API emulation |
| **MicroPython** | yes | for entirely different reasons (simplicity on microcontrollers) |

The pattern is exact: **tracing GC → no GIL; reference counting → GIL.** Jython and
IronPython have been GIL-free for two decades and nobody used them for that, which is its
own lesson — the C extension ecosystem is a stronger constraint than parallelism.

---

## 18. Lab exercises

Do these. Reading this document leaves you at rung 3 (see the ladder in `README.md`).

1. **Feel the coherence cost.** In C: N threads incrementing (a) thread-local counters,
   (b) separate counters on the same cache line, (c) one shared atomic counter. Plot
   throughput vs N. You should see roughly flat, collapsed, and collapsed-worse.
   *This is the Gilectomy's first attempt, in 30 lines.*

2. **Reproduce the convoy effect.** A socket echo server thread + a `while True: pass`
   thread. Measure RPS. Then sweep `sys.setswitchinterval()` from `0.1` to `0.000001`
   and reproduce the table in §7 on your own hardware. Explain why the smallest value
   is not the best.

3. **Prove `x += 1` isn't atomic.** Two threads, one shared counter, one million
   increments each. Print the result. Then `dis` the function and point at the exact
   instruction boundary where the interleaving happens. Re-run on a free-threaded build
   and observe that it fails *faster*.

4. **Freeze the interpreter.** Write a C extension (or use a pathological regex) that
   holds the GIL for 30 seconds. Confirm `Ctrl-C` does nothing and other threads make no
   progress. Then wrap it in `Py_BEGIN_ALLOW_THREADS` and confirm the difference.
   *Then* explain, from §8, why `Ctrl-C` worked during `time.sleep(30)` but not here.

5. **Find the check points.** Write a function with a long straight-line body and no
   loops or calls, and one with a tight loop. `dis` both, locate `RESUME` and
   `JUMP_BACKWARD`, and predict which one a second thread can interrupt. Verify.

6. **The five-way comparison.** One CPU-bound workload; implement with threads on a GIL
   build, threads on a free-threaded build, `multiprocessing`, `concurrent.interpreters`,
   and a `nogil` native extension. Build a table: wall time, total CPU, RSS, startup cost,
   lines of code. *This artifact is the single best interview asset in Tier 4.*

7. **Find the sharing wall.** Take exercise 6's free-threaded version and make the threads
   share one large dict instead of working on disjoint data. Measure the scaling
   difference. You have just measured §13's new bottleneck.

8. **Break the GIL-as-mutex assumption.** Write a C extension with a module-level cache
   and no locking. Confirm it is correct under the GIL, then run it on a free-threaded
   build under load and corrupt it. Fix it with `Py_BEGIN_CRITICAL_SECTION`. *This is the
   migration risk in §14, reproduced in miniature.*

9. **Diagnose someone else's process.** Take any real service you run, attach `py-spy top`
   for 60 seconds, and write down: GIL%, the top holder, and whether it is CPU-bound,
   I/O-bound, or throttled. Most engineers have never done this once.

---

## 19. Question bank

Staff-level. If you can't answer from your own model, the section to reread is noted.

1. Why is atomic reference counting slower than a global lock for single-threaded code? *(§1)*
2. Two threads only *read* a shared object. Why does this generate write traffic on the memory bus? *(§1)*
3. Why does `os.fork()` fail to deliver the copy-on-write savings people expect from it? *(§1, §8)*
4. What was wrong with the pre-3.2 GIL, and why didn't `sys.setcheckinterval` fix it? *(§3)*
5. What is `switch_number` for? What breaks if you remove it? *(§3)*
6. Is the switch interval a quantum or a timeout? What is the actual end-to-end latency to a thread switch? *(§4, §6)*
7. Between which bytecodes can a thread switch happen — and where can it *not*? *(§4)*
8. Why can't `Ctrl-C` interrupt a long-running C extension call, but it can interrupt `time.sleep(60)`? *(§5, §8)*
9. Name three stdlib operations that release the GIL and three that don't. What's the rule? *(§5)*
10. Explain the convoy effect end to end, and predict the p99 latency signature it produces. *(§7)*
11. Pitrou's new GIL fixed the GIL battle and created the convoy effect. What was the trade? *(§3, §7)*
12. Your service's p99 is 5.2 ms and CPU is at 30%. What is your first hypothesis, and what single experiment confirms or kills it? *(§7, §16)*
13. Why did Larry Hastings' Gilectomy need seven cores to match stock CPython on one? *(§11)*
14. What is buffered reference counting, and why did it need *two* separate logs? *(§11)*
15. From a flame graph, how do you distinguish lock contention from a cache-coherence problem? *(§11, §16)*
16. How does biased reference counting make the common case non-atomic, and what happens when an object escapes? *(§12.2)*
17. Why is immortalizing `None` more valuable than any other single optimization in PEP 703? *(§12.1, §1)*
18. What is QSBR for, and what would break without it? *(§12.4)*
19. Why was mimalloc chosen — and why is "it's thread-safe" the wrong answer? *(§12.4)*
20. Free-threaded Python introduces stop-the-world GC pauses the GIL build doesn't have. Why is that unavoidable? *(§12.5)*
21. Your service is 8% slower on the free-threaded build and doesn't scale past 2 threads. Diagnose. *(§13)*
22. `sys._is_gil_enabled()` returns `True` on a `cp314t` build. Give three explanations. *(§13, §14)*
23. Which is more likely to break under free-threading: your Python code or your C extensions? Why? *(§9, §14)*
24. Why is `Py_BEGIN_CRITICAL_SECTION` not equivalent to holding a mutex? *(§14)*
25. Is `d[k] += 1` atomic on a free-threaded build? Justify from bytecode. *(§9)*
26. When would you choose sub-interpreters over processes, and over free-threaded threads? *(§10, §15)*
27. Sub-interpreters and free-threading both require extensions to opt in, with different slots. Why did two independent projects arrive at the same requirement? *(§10, §14)*
28. Jython has had no GIL since 2001. Why did that not settle the question? *(§17)*

---

## 20. Sources

**Primary**
- [PEP 703 — Making the Global Interpreter Lock Optional in CPython](https://peps.python.org/pep-0703/) (Sam Gross) — the specification. Read §Reference Counting and §Garbage Collection in full.
- [PEP 779 — Criteria for supported status for free-threaded Python](https://peps.python.org/pep-0779/) — the phase model.
- [PEP 684 — A per-interpreter GIL](https://peps.python.org/pep-0684/) and [PEP 734 — Multiple interpreters in the stdlib](https://peps.python.org/pep-0734/).
- [PEP 683 — Immortal objects](https://peps.python.org/pep-0683/).
- [Python support for free threading (official HOWTO)](https://docs.python.org/3/howto/free-threading-python.html) and the [C API HOWTO](https://docs.python.org/3/howto/free-threading-extensions.html) — authoritative on current limitations, overhead numbers, and the extension contract.
- CPython sources: `Python/ceval_gil.c`, `Python/ceval.c`, `Python/bytecodes.c` (grep for `CHECK_EVAL_BREAKER`), `Include/object.h`, `Python/qsbr.c`, `Python/critical_section.c`, `Modules/signalmodule.c`.

**The old GIL and the rewrite**
- [Dave Beazley — Understanding the Python GIL](http://www.dabeaz.com/GIL/) and [Inside the GIL](https://speakerdeck.com/dabeaz/inside-the-python-gil) — the origin of both the GIL-battle and convoy-effect analyses. Watch these before reading anything else on the topic.
- [bpo-7946 — Convoy effect with I/O bound threads and New GIL](https://bugs.python.org/issue7946) — still open. Read the whole thread.
- Antoine Pitrou's `python-dev` thread on the new GIL (2009) — the design discussion, including the fairness question he explicitly declined to solve.

**The Gilectomy**
- [Victor Stinner — Free Threading internals: reference counting](https://vstinner.github.io/free-threading-reference-counting.html) — has the actual buffered-refcount source.
- [LWN — A Gilectomy update (2018)](https://lwn.net/Articles/754577/)
- [LWN — Progress on the Gilectomy (2017)](https://lwn.net/Articles/723514/)
- [LWN — Gilectomy (2016)](https://lwn.net/Articles/689548/)
- Larry Hastings, *The Gilectomy* / *How's It Going?* — PyCon 2016/2017, EuroPython. Watch the talks; the graphs land better than prose.

**The GIL itself**
- [Python behind the scenes #13: the GIL and its effects on Python multithreading](https://tenthousandmeters.com/blog/python-behind-the-scenes-13-the-gil-and-its-effects-on-python-multithreading/) — the best free deep-dive; source of the §7 measurements.

**Free-threading design**
- [LWN — A viable solution for Python concurrency (2021)](https://lwn.net/Articles/872869/) — including Sam Gross's own comments on provenance and overhead.
- [Locking in WebKit](https://webkit.org/blog/6161/locking-in-webkit/) — where the internal lock design came from.
- [py-free-threading.github.io](https://py-free-threading.github.io/) — the community porting guide; the practical companion to §14.

**Tooling**
- [py-spy](https://github.com/benfred/py-spy) — sampling profiler with a `%GIL` column.
- [gil_load](https://github.com/chrisjbillington/gil_load) — quantifies GIL held/wait fractions.

**Foundations (Tier 0, if any of §1 was unclear)**
- Ulrich Drepper, *What Every Programmer Should Know About Memory*
- Paul McKenney, *Memory Barriers: a Hardware View for Software Hackers*
- Herlihy & Shavit, *The Art of Multiprocessor Programming*, 2e — ch. 7 (spin locks & contention)

---

*Next: `25-threads-and-synchronization.md` (what to actually build on top of this), then
`26-free-threading.md` (the migration in depth), then
`27-multiprocessing-and-subinterpreters.md` (the alternative in §10 and §15).*
