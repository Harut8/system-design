# 30 — Concurrency Correctness

> **Provenance.** Everything measured here was run in this session on an **Apple M3 Pro**
> (5 performance + 6 efficiency cores, 11 logical, 128-byte cache lines, UMA), macOS
> 25.5.0 (Darwin), Apple clang 21.0.0. Interpreters: **CPython 3.14.6** (GIL build),
> **CPython 3.14.6 free-threading build**, plus **3.9.25 / 3.11.9 / 3.12.3 / 3.13.5** for
> the historical comparison. The machine was **not quiet** — `load1` sat between 2.2 and
> 2.6 for the whole session. Where that mattered I say so, and one result in §9 is a
> noise artifact I caught and corrected rather than published. Every number below is
> reproducible from the scripts described in §20.
>
> CPython source quotations are from the **`3.14` branch** of `python/cpython`, fetched
> during this session: `Python/bytecodes.c` (5,549 lines), `Python/lock.c` (638),
> `Include/internal/pycore_lock.h` (236), `Modules/_threadmodule.c`.

---

## Contents

1. [The thesis: correctness is a property of the schedule](#1-the-thesis-correctness-is-a-property-of-the-schedule)
2. [Data race vs race condition — the distinction that organizes everything](#2-data-race-vs-race-condition--the-distinction-that-organizes-everything)
3. [The centerpiece: one bug, five interpreters](#3-the-centerpiece-one-bug-five-interpreters)
4. [Where CPython is actually allowed to switch threads](#4-where-cpython-is-actually-allowed-to-switch-threads)
5. [The atomicity table, measured on both builds](#5-the-atomicity-table-measured-on-both-builds)
6. [Check-then-act, and why every compound operation is a bug](#6-check-then-act-and-why-every-compound-operation-is-a-bug)
7. [Python has no memory model](#7-python-has-no-memory-model)
8. [Liveness: deadlock, self-deadlock, livelock, starvation](#8-liveness-deadlock-self-deadlock-livelock-starvation)
9. [Lock convoying, measured](#9-lock-convoying-measured)
10. [Starvation is a GIL artifact — the experiment that proves it](#10-starvation-is-a-gil-artifact--the-experiment-that-proves-it)
11. [Priority inversion, and why Python mostly can't see it](#11-priority-inversion-and-why-python-mostly-cant-see-it)
12. [Lock ordering as an enforceable discipline](#12-lock-ordering-as-an-enforceable-discipline)
13. [Cooperative vs preemptive scheduling](#13-cooperative-vs-preemptive-scheduling)
14. [Clocks: the correctness bug hiding in your timeouts](#14-clocks-the-correctness-bug-hiding-in-your-timeouts)
15. [Cache thrashing, false sharing, NUMA — correctness-adjacent](#15-cache-thrashing-false-sharing-numa--correctness-adjacent)
16. [Progress guarantees: wait-free, lock-free, obstruction-free](#16-progress-guarantees-wait-free-lock-free-obstruction-free)
17. [Transactional memory, and why it is not on your menu](#17-transactional-memory-and-why-it-is-not-on-your-menu)
18. [WebAssembly: the platform where blocking is illegal](#18-webassembly-the-platform-where-blocking-is-illegal)
19. [Work-stealing: what Python does not have](#19-work-stealing-what-python-does-not-have)
20. [Testing and fuzzing concurrent code](#20-testing-and-fuzzing-concurrent-code)
21. [A review checklist](#21-a-review-checklist)
22. [What I could not verify](#22-what-i-could-not-verify)
23. [Lab exercises](#23-lab-exercises)
24. [Question bank](#24-question-bank)
25. [Sources](#25-sources)

---

## 1. The thesis: correctness is a property of the schedule

A sequential program has one execution. A concurrent program has an enormous set of
possible executions — one per legal interleaving — and it is correct only if **every**
member of that set produces an acceptable result.

This has an uncomfortable consequence that the rest of this document is an elaboration
of: **testing samples the set; it does not cover it.** You ran your test 10,000 times
and it passed. There are more than 10,000 schedules. The ones you didn't sample are
exactly the ones that will run in production at 3 a.m., because production has different
core counts, different contention, different timing, and vastly more trials.

The second, sharper consequence is the one most Python engineers have never internalized:

> **The set of legal schedules is not a fixed property of your program. It is a property
> of the interpreter you are running on, and it has changed twice in Python's history —
> once quietly in 3.10, and once loudly with free-threading.**

Section 3 demonstrates this with a single unchanged program that loses 42.72% of its
updates on 3.9, loses **nothing at all** on 3.11 through 3.14, and then loses 57.36% on
free-threaded 3.14. The bug was there the whole time. Only the schedule set moved.

That is why "we tested it and it was fine" is not evidence of thread safety, and why a
codebase validated on 3.12 can be full of latent races that free-threading will expose
all at once.

```
        the program                    the schedule set                the outcome
    ┌──────────────────┐          ┌────────────────────────┐        ┌──────────────┐
    │  s.v += 1        │  ────►   │  which interleavings   │  ───►  │  correct?    │
    │  (never changes) │          │  the runtime permits   │        │  (varies!)   │
    └──────────────────┘          └────────────────────────┘        └──────────────┘
                                       ▲            ▲
                                       │            │
                          interpreter version    core count,
                          GIL vs free-threaded   contention, load
```

**Cross-references.** This document is the *correctness* half of Tier 4. The mechanism
half lives elsewhere and is not repeated here: the GIL's implementation and the convoy
effect are [`24-the-gil.md`](24-the-gil.md); free-threading's cost and sharing wall are
[`26-free-threading.md`](26-free-threading.md); hardware memory ordering and atomics are
[`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md); lock-free
algorithms and reclamation are [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md);
cache lines and false sharing are [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md);
the asyncio machinery is [`28-asyncio-internals.md`](28-asyncio-internals.md).

---

## 2. Data race vs race condition — the distinction that organizes everything

These two terms are used interchangeably in casual conversation and they mean different
things. Getting them apart is the single highest-leverage conceptual move in this
document, because **Python's two builds sit in different boxes.**

### 2.1 Definitions

**Data race** — a *memory-model* concept. Two threads access the same memory location,
at least one access is a write, the accesses are not ordered by any
happens-before relation, and at least one is non-atomic. In C11/C++11 a data race is
**undefined behaviour**: the compiler and hardware may do literally anything, including
tearing the value, inventing loads, or optimizing your loop away entirely. See
[`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md) §14 for the SC-DRF
bargain this comes from.

**Race condition** — a *logic* concept. The correctness of the result depends on the
relative timing of operations. No undefined behaviour is required. A race condition can
exist in a program with perfect locking, perfect atomics, and no data races anywhere —
if the *granularity* of the locking doesn't match the *granularity* of the invariant.

They are independent axes:

| | **no data race** | **data race** |
|---|---|---|
| **no race condition** | Correct program. | Impossible to rely on — UB can break anything. |
| **race condition** | `if k not in d: d[k] = v` under the GIL. Every operation atomic; the *pair* is not. **This is where almost all Python bugs live.** | `x++` in C from two threads with no synchronization. Both problems at once. |

### 2.2 Why pure Python code cannot have a data race

This is a strong claim, so let me state it precisely.

**In pure Python, on either CPython build, you cannot construct a C11-style data race.**
Every Python-level memory access goes through the interpreter, which either (a) holds the
GIL, serializing everything, or (b) on the free-threaded build, uses atomic operations
and per-object locks that establish the necessary ordering. PEP 703 makes memory safety
a hard requirement: a racy Python program produces a *wrong answer*, never a torn pointer
or a segfault.

Verified above: the free-threaded build lost 82.64% of increments to `d['k'] += 1` (§5)
— it lost updates, but the dict remained a valid dict, and 320,000 concurrent
`list.append` calls produced a list of exactly length 320,000 with no corruption.

Three caveats, all of which matter:

1. **C extensions are exempt.** A native extension that touches shared state without the
   GIL and without its own synchronization has a genuine data race with genuine
   undefined behaviour. This is the single largest hazard in the free-threading
   migration — see [`26-free-threading.md`](26-free-threading.md) §6 and
   [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md).
2. **`ctypes` / `mmap` / shared memory are exempt.** Once you are writing into a raw
   buffer, you are writing C, and C's rules apply.
3. **"No data race" is not "no bug."** It only means the failure mode is a wrong value
   rather than undefined behaviour. Wrong values are still outages.

The payoff of this framing: **for pure Python, you can stop worrying about memory
ordering and spend 100% of your attention on race conditions** — on the atomicity of
*compound operations*. That is what §5 and §6 are about.

---

## 3. The centerpiece: one bug, five interpreters

Here is the entire program. It is the canonical thread-safety example from every
textbook and every interview.

```python
class Box:
    __slots__ = ('v',)

T, N = 8, 200_000

def trial():
    s = Box(); s.v = 0
    b = threading.Barrier(T)
    def w():
        b.wait()
        for _ in range(N):
            s.v += 1          # <-- the bug
    ths = [threading.Thread(target=w) for _ in range(T)]
    for t in ths: t.start()
    for t in ths: t.join()
    return s.v                # want 1,600,000
```

Eight threads, 200,000 increments each, 1,600,000 expected. Worst of three runs, same
source file, six interpreters:

| interpreter | GIL | lost updates | loss | wall |
|---|---|---|---|---|
| **3.9.25** | yes | **683,567** | **42.72%** | 0.055 s |
| 3.11.9 | yes | 0 | 0.00% | 0.029 s |
| 3.12.3 | yes | 0 | 0.00% | 0.031 s |
| 3.13.5 | yes | 0 | 0.00% | 0.031 s |
| 3.14.6 | yes | 0 | 0.00% | 0.024 s |
| **3.14.6t** | **no** | **917,723** | **57.36%** | 0.088 s |

Read that table again. The textbook is right on 3.9. The textbook is **wrong on 3.11
through 3.14**, in the sense that the failure it predicts does not occur — not "rarely
occurs," but did not occur in three runs of 1.6 million opportunities each. And then the
textbook becomes right again, more emphatically than ever, on the free-threaded build.

Three conclusions, each of which costs people money:

1. **You cannot demonstrate this classic bug on a modern GIL build.** If you have been
   teaching it with a live demo, your demo has been silently passing since 2021.
2. **A test suite that "proves" thread safety on 3.11–3.14 proves nothing.** The absence
   of the failure is an artifact of the interpreter's switch-point policy, not a
   property of your code.
3. **Free-threading does not introduce new bugs. It reveals old ones**, at a rate high
   enough that they stop looking like flakes. 57.36% is not a heisenbug; it is a
   deterministic-looking failure.

Section 4 explains exactly why 3.11–3.14 come out clean, and it is not because anyone
made `+=` atomic.

---

## 4. Where CPython is actually allowed to switch threads

### 4.1 The measurement that pointed at the answer

First: are the threads even interleaving? If they run to completion one at a time, of
course nothing is lost. Probe — 4 threads each appending their own id to a shared list
(`list.append` is atomic, §5), then count the transitions in the recorded sequence. Each
transition is a real preemption inside the hot loop.

| `sys.setswitchinterval` | entries | observed switches | mean run length | wall |
|---|---|---|---|---|
| 5 ms (default) | 400,000 | 3 | 133,333 ops | 0.005 s |
| 100 µs | 400,000 | 36 | 11,111 ops | 0.005 s |
| 1 µs | 400,000 | **1,233** | 324 ops | 0.017 s |

So at a 1 µs switch interval the interpreter switched threads 1,233 times during the
loop. Preemption is happening constantly. And yet:

| variant | lost |
|---|---|
| `s.v += 1` | **0.00%** |
| `tmp = s.v` / `tmp = tmp + 1` / `s.v = tmp` (three separate statements) | **0.00%** |
| `s.v = add1(s.v)` (a function call between read and write) | **73.58%** |

Splitting the increment into three statements does not break it. Inserting a *function
call* breaks it immediately and catastrophically. That is the entire clue.

### 4.2 The bytecode

```
# s.v += 1                          # s.v = add1(s.v)
LOAD_FAST_BORROW  0 (s)             LOAD_GLOBAL       3 (add1 + NULL)
COPY              1                 LOAD_FAST_BORROW  0 (s)
LOAD_ATTR         2 (v)   ◄─┐       LOAD_ATTR         4 (v)     ◄─┐
LOAD_SMALL_INT    1         │       CALL              1           │  ← add1's RESUME
BINARY_OP        13 (+=)    │ no    LOAD_FAST_BORROW  0 (s)       │    runs HERE
SWAP              2         │ check STORE_ATTR        2 (v)     ◄─┘
STORE_ATTR        1 (v)   ◄─┘       JUMP_BACKWARD    31
JUMP_BACKWARD    30
```

In the left column there is nothing between `LOAD_ATTR` and `STORE_ATTR` that can yield.
In the right column, `CALL` enters `add1`, whose first instruction is `RESUME` — and
`RESUME` is a thread-switch point. The vulnerable window now contains one.

### 4.3 The source: 22 instructions, and only 22

CPython's eval loop does not check for pending work between arbitrary bytecodes. It
checks only where `_CHECK_PERIODIC` is compiled in. From `Python/bytecodes.c` (3.14):

```c
op(_CHECK_PERIODIC, (--)) {
    _Py_CHECK_EMSCRIPTEN_SIGNALS_PERIODICALLY();
    QSBR_QUIESCENT_STATE(tstate);
    if (_Py_atomic_load_uintptr_relaxed(&tstate->eval_breaker) & _PY_EVAL_EVENTS_MASK) {
        int err = _Py_HandlePending(tstate);
        ERROR_IF(err != 0);
    }
}
```

Parsing the whole file and mapping every `_CHECK_PERIODIC` use to its enclosing
instruction gives **exactly 22** instructions, in three families:

| family | instructions |
|---|---|
| **Loop back edges** | `JUMP_BACKWARD`, `JUMP_BACKWARD_JIT`, `JUMP_BACKWARD_NO_JIT`, `INSTRUMENTED_JUMP_BACKWARD` |
| **Function entry / resumption** | `RESUME`, `INSTRUMENTED_RESUME` |
| **Calls into non-Python code** | `CALL`, `CALL_FUNCTION_EX`, `CALL_KW_NON_PY`, `CALL_NON_PY_GENERAL`, `CALL_BUILTIN_CLASS`, `CALL_BUILTIN_FAST`, `CALL_BUILTIN_FAST_WITH_KEYWORDS`, `CALL_BUILTIN_O`, `CALL_STR_1`, `CALL_TUPLE_1`, `CALL_METHOD_DESCRIPTOR_FAST`, `CALL_METHOD_DESCRIPTOR_FAST_WITH_KEYWORDS`, `CALL_METHOD_DESCRIPTOR_NOARGS`, `CALL_METHOD_DESCRIPTOR_O`, `INSTRUMENTED_CALL`, `INSTRUMENTED_CALL_FUNCTION_EX` |

That is the complete list of places a GIL-build CPython 3.14 thread can be preempted by
another Python thread. **Everything between two consecutive check points is atomic with
respect to other Python threads.**

Two refinements worth knowing:

- `RESUME` uses `_CHECK_PERIODIC_IF_NOT_YIELD_FROM`, which **skips** the check when
  resuming from a `yield from` / `await` delegation
  (`oparg & RESUME_OPARG_LOCATION_MASK) < RESUME_AFTER_YIELD_FROM`). Relevant to
  [`28-asyncio-internals.md`](28-asyncio-internals.md) §3.
- The specialized `RESUME_CHECK` does *not* check the breaker; it `DEOPT_IF`s when the
  breaker differs from the instrumentation version, bouncing back to the generic
  `RESUME`. The fast path is a compare-and-deopt, not a branch into pending work.

### 4.4 The history — and the irony

This was deliberate. **[PR #18334](https://github.com/python/cpython/pull/18334),
"bpo-29988: Only check evalbreaker after calls and on backwards egdes"** *(sic)*, by
**Mark Shannon**, merged **2021-03-24** (commit `4958f5d`), first released in **Python
3.10**. Its stated rationale:

> Makes sure that `__exit__` or `__aexit__` is called in (async) `with` statements, by
> not handling interrupts during set up of the with block.
>
> We want to make sure that interrupts are always handled eventually, and ideally that
> they are handled promptly. Checking `eval_breaker` on backward edges ensures that they
> are always handled eventually. Checking after every explicit call ensures that they are
> handled promptly in most cases.

The change was made to fix a **`Ctrl-C`-vs-context-manager** bug. Making a large class of
Python-level races unobservable was a *side effect nobody was aiming for* — and it is the
reason the 3.11–3.14 column of §3's table is full of zeros.

> **The trap.** It is tempting to conclude "so `+=` is atomic now, on GIL builds."
> **Do not.** It is not a language guarantee, it is not documented, it is an emergent
> property of an optimization, and it evaporates the moment (a) a check point lands in
> your window — which any function call, any C call, any loop back edge introduces, or
> (b) you run free-threaded. The correct model is not "`+=` is safe"; it is "**I have no
> idea where the check points are in this expression, so I will lock.**"

### 4.5 Why free-threading changes everything

On the free-threaded build there is no GIL to hold, so "atomic between check points"
stops being true. Threads run *simultaneously* on different cores. The window between
`LOAD_ATTR` and `STORE_ATTR` is now a genuine window in wall-clock time on another core,
not a window in the interpreter's switch schedule. Hence 57.36%.

---

## 5. The atomicity table, measured on both builds

Eight threads × 40,000 operations each, 320,000 expected. Shortfall = lost updates.

| operation | GIL build | free-threaded | why |
|---|---|---|---|
| `obj.attr = obj.attr + 1` | ✅ 0.00% | ❌ **74.82%** | read and write are separate bytecodes |
| `obj.attr += 1` | ✅ 0.00% | ❌ **67.20%** | same; `+=` is not one operation |
| `lst[0] += 1` | ✅ 0.00% | ❌ **80.49%** | `BINARY_OP` + `STORE_SUBSCR` |
| `d['k'] += 1` | ✅ 0.00% | ❌ **82.64%** | same |
| `lst.append(x)` | ✅ 0.00% | ✅ **0.00%** | one C call, internally locked |
| `d[unique_key] = v` | ✅ 0.00% | ✅ **0.00%** | one C call, internally locked |
| `set.add(len(set))` | ✅ 0.00% | ❌ **1.07%** | the `len()` read is a separate operation |
| `dict.setdefault(k, v)` | ✅ | ✅ | one C call — the atomic primitive |
| `obj.attr += 1` under `Lock` | ✅ 0.00% | ✅ **0.00%** | correct on both, always |

### 5.1 How to read this table

The GIL column is **not** a list of things that are safe. It is a list of things whose
races are currently unobservable because of §4. The only rows that are genuinely,
portably, permanently atomic are the ones that are **a single call into C on a single
object**:

- `list.append`, `list.pop`, `list.extend`
- `dict.__setitem__`, `dict.__getitem__`, `dict.setdefault`, `dict.pop`
- `set.add`, `set.discard`
- `deque.append`, `deque.popleft` — the classic safe queue primitives
- `Queue.put`, `Queue.get` — explicitly synchronized

And even these are atomic **individually**. Two of them in sequence is not atomic. That
is §6.

### 5.2 The 1.07% row is the interesting one

`set.add(len(s))` lost only 1.07% where the others lost 67–82%. Both `len()` and `.add()`
are individually atomic C calls; the race is only in the gap between them, and that gap
is narrow — one bytecode. Compare with `d['k'] += 1`'s wide gap (load, add, store) at
82.64%.

**This is the most dangerous shape of bug in the table.** A 1% failure rate under an
8-thread stress test is a bug that passes code review, passes CI, passes staging, and
then corrupts one in a hundred records in production. The 82% bugs get caught. The 1%
bugs ship.

---

## 6. Check-then-act, and why every compound operation is a bug

The dominant race-condition shape in real Python code:

```python
if key not in cache:          # CHECK
    cache[key] = expensive()  # ACT
```

Both lines are atomic. The *pair* is not. Measured on the free-threaded build with 8
threads × 40,000 iterations, instrumented to detect when a key appeared between the check
and the act: **23 detected clobbers**. Under the GIL: **0** — for exactly the §4 reason,
and note that `expensive()` being a function call would put a check point right in the
middle and change that zero.

The generic form:

```
    ┌──────────────────────────────────────────────────────────┐
    │  Thread A: CHECK  ──────────────────►  ACT               │
    │                          ▲                               │
    │                          │  Thread B does the whole      │
    │                          │  thing here. A's decision is  │
    │                          │  now stale, and A overwrites. │
    └──────────────────────────────────────────────────────────┘
```

### 6.1 The family

Every one of these is the same bug wearing a different hat:

| pattern | code | fix |
|---|---|---|
| check-then-act | `if k not in d: d[k] = v` | `d.setdefault(k, v)` |
| read-modify-write | `d[k] += 1` | `Counter` under a lock, or `Lock` |
| test-and-set | `if not self.started: self.start()` | `Lock`, or `functools.cache` |
| lazy init | `if self._x is None: self._x = f()` | double-checked locking **with** a lock, or module-level init |
| get-then-remove | `if k in d: del d[k]` | `d.pop(k, None)` |
| size-then-index | `if lst: x = lst[0]` | `try: x = lst[0] except IndexError:` |
| copy-then-iterate | `for x in d:` while another thread writes | `list(d.items())` under a lock |

The unifying rule: **if your invariant spans more than one operation, your lock must span
more than one operation.** There is no atomic primitive that saves you, because the
problem is not the primitives — it's the gap between them.

### 6.2 The idiom that is actually correct

```python
# Preferred: use the atomic primitive when one exists.
value = cache.setdefault(key, sentinel)

# When none exists: lock the invariant, not the operation.
with self._lock:
    if key not in self._cache:
        self._cache[key] = self._compute(key)   # NB: holds the lock across compute
```

That second form has a real cost — it serializes `_compute`. The standard fix is
double-checked locking, which in Python is safe (no memory-model subtleties, per §7) but
must still be written carefully:

```python
def get(self, key):
    try:
        return self._cache[key]          # fast path, no lock
    except KeyError:
        pass
    value = self._compute(key)           # computed outside the lock
    # setdefault makes the race benign: first writer wins, everyone
    # returns the same object, duplicates are discarded.
    return self._cache.setdefault(key, value)
```

This *may compute `value` more than once* under contention, and that is a deliberate
trade: it exchanges a correctness problem for a small amount of duplicated work. Make
that trade consciously and only when `_compute` is pure and cheap-ish. If `_compute` has
side effects, you must hold the lock.

---

## 7. Python has no memory model

[`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md) §16 establishes
this; the correctness consequences belong here.

**The Python language specification does not define a memory model.** There is no
document that tells you which reorderings are permitted, what `happens-before` means at
the Python level, or what a racy program is guaranteed to observe. C11 has one. Java has
one (JSR-133). Go has one. Python has the CPython implementation's behaviour, and that
behaviour changed in 3.10 (§4) and changes again per-build (§3).

What you may rely on, in practice, on CPython:

| assumption | safe? | notes |
|---|---|---|
| A single C-level operation on one object is atomic | ✅ | The `list.append` family, both builds |
| Values are never torn | ✅ | PEP 703 guarantees memory safety |
| Objects never become invalid under you | ✅ | Refcounting + per-object locks |
| Two operations in sequence are atomic | ❌ | §6 |
| `x += 1` is atomic | ❌ | §3, §5 — accidentally true on 3.10–3.14 GIL builds only |
| Writes become visible to other threads promptly | ⚠️ | Unspecified. In practice yes, via the lock/atomic machinery. Do not build on it. |
| A lock establishes ordering for everything it protects | ✅ | Acquire/release semantics in `PyMutex` |

The practical rule, which is not a cop-out but the actual engineering answer:

> **Use locks and queues. Do not attempt lock-free reasoning at the Python level.**
> You cannot write `memory_order_acquire` in Python. You have no fences. The one
> synchronization vocabulary you have is `threading`'s, and it is sufficient. Reserve
> the lock-free reasoning of [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md)
> for the C extension layer, where the C11 model actually applies.

---

## 8. Liveness: deadlock, self-deadlock, livelock, starvation

Safety says "nothing bad happens." Liveness says "something good eventually happens."
Everything up to here has been safety. These four are the liveness failures, and they
have completely different signatures in production.

### 8.1 Deadlock — lock-order inversion

Two locks, two threads, opposite acquisition orders:

```python
def t1():
    with a:
        with b: pass       # A then B
def t2():
    with b:
        with a: pass       # B then A   <-- inversion
```

Measured: **5 out of 5 trials deadlocked**, every one within the 2.02 s observation
window, on 1,000,000 iterations each. With both threads acquiring in the same order, the
same workload completed in **0.059 s** with zero hangs.

Deadlock requires all four **Coffman conditions** simultaneously:

| condition | meaning | how to break it |
|---|---|---|
| Mutual exclusion | The resource can't be shared | Use immutable data / copies |
| Hold and wait | Hold one while requesting another | Acquire all at once, or none |
| No preemption | Can't forcibly take a lock back | Use timeouts (`acquire(timeout=)`) |
| Circular wait | A cycle in the wait-for graph | **Global lock ordering — §12** |

In practice you break **circular wait**, because it is the only one you can enforce
mechanically and cheaply.

**Production signature:** threads stop making progress and *stay* stopped. CPU goes to
zero. The process is alive and unresponsive. Requests pile up until the connection pool
exhausts. This is the easy one to diagnose — see §20.4.

### 8.2 Self-deadlock — `Lock` is not re-entrant

```python
l = threading.Lock()
l.acquire()
l.acquire(timeout=0.2)   # -> False. Without the timeout: hangs forever.
```

Measured: second `acquire` returns `False` with a timeout; `RLock` in the same position
returns `True`.

This is the bug you write when a method that takes the lock calls another method that
takes the same lock — usually after an innocent refactor. `RLock` fixes it and costs
you something real: it maintains an owner and a recursion count, and it **hides the
design problem**. A method that re-enters its own lock usually means the lock's scope
is unclear. Prefer restructuring so that the public method takes the lock and calls a
private `_locked` variant that assumes it.

```python
def update(self, k, v):
    with self._lock:
        self._update_locked(k, v)

def _update_locked(self, k, v):     # documented precondition: caller holds _lock
    ...
```

### 8.3 Livelock — running hard, achieving nothing

The polite-backoff pattern: take your lock, try for the other, and if you can't get it,
release everything and retry so as not to deadlock.

```python
mine.acquire()
if theirs.acquire(blocking=False):
    ...                  # progress
else:
    retries += 1         # "be polite": back off and retry
mine.release()
```

Two threads doing this in opposite orders can synchronize into lockstep, each releasing
exactly when the other is about to grab. Measured over 1.0 s of two threads at 100% CPU:

| | thread 0 | thread 1 |
|---|---|---|
| **useful work completed** | **0** | **0** |
| retries burned | 125,274 | 125,275 |

**Goodput: 0 / 250,549 attempts = 0.0000%.** Note the retry counts: 125,274 and 125,275
— perfect lockstep.

> **Methodology note, reported honestly.** My first attempt at this demo did *not*
> livelock: it completed 544,741 and 566,465 units of work. The GIL made true lockstep
> unlikely — one thread would usually win and proceed. I forced the pathological schedule
> with a `Barrier`, and only then did goodput go to zero. **The barrier is doing the work
> that a real scheduler does occasionally and unpredictably.** So treat this as a
> demonstration of the *shape* of livelock, not evidence that it is common in Python.
> The honest claim is: livelock in Python requires a coincidence that the GIL makes rare
> and that free-threading makes less rare.

**Production signature:** the exact opposite of deadlock. **CPU pegged at 100%,
throughput at zero.** Every thread is running. Nothing is blocked. `py-spy dump` shows
threads in different places each time you sample. A deadlock detector that looks for
blocked threads finds nothing — which is why livelock is much harder to diagnose than
deadlock, despite being rarer.

### 8.4 Starvation — a thread that never gets a turn

One shared lock, 8 threads, 200,000 total acquisitions. Perfectly fair would be 25,000
each. Two runs each build:

**GIL build:**
```
  [68878, 0, 0, 0, 45183, 0, 0, 85939]      starved: 5/8
  [95952, 0, 0, 0, 51271, 0, 0, 52777]      starved: 5/8
```

**Free-threaded build:**
```
  [25024, 24812, 25047, 23640, 26415, 24512, 25330, 25220]   starved: 0/8
  [26547, 25816, 25440, 25885, 22966, 24532, 23409, 25405]   starved: 0/8
```

Five of eight threads got **zero** acquisitions on the GIL build, reproducibly. The
free-threaded build was fair to within ±7% of ideal.

This result is counterintuitive enough that it needs its own section — see §10, where
the cause turns out not to be the lock at all.

---

## 9. Lock convoying, measured

A **convoy** forms when the cost of *transferring* a lock exceeds the work done inside
it. Threads queue up, each pays a park/unpark round trip, and aggregate throughput falls
as you add threads.

Throughput (ops/sec) of a lock-protected critical section, **median of 5 passes**:

**GIL build, ~1 µs critical section:**

| threads | median ops/s | vs 1 thread |
|---|---|---|
| 1 | 1,475,429 | 1.00× |
| 2 | 1,477,957 | 1.00× |
| 4 | 1,479,107 | 1.00× |
| 8 | 1,469,813 | 1.00× |
| 16 | 1,438,922 | 0.98× |

**Free-threaded build, ~1 µs critical section:**

| threads | median ops/s | vs 1 thread |
|---|---|---|
| 1 | 1,355,843 | 1.00× |
| 2 | 989,088 | **0.73×** |
| 4 | 641,941 | **0.47×** |
| 8 | 630,111 | **0.46×** |
| 16 | 497,574 | **0.37×** |

And the dependence on critical-section length (free-threaded, normalized to 1 thread):

| threads | cs ≈ 1 µs | cs ≈ 5 µs | cs ≈ 20 µs |
|---|---|---|---|
| 2 | 0.71× | 0.74× | 0.81× |
| 4 | 0.45× | 0.68× | 0.81× |
| 8 | 0.45× | 0.65× | 0.81× |
| 16 | **0.36×** | 0.68× | 0.80× |

**The shorter the critical section, the worse contention hurts** — because the fixed
handoff cost is a larger fraction of the total. This is the counterintuitive tuning
result: making your critical section *shorter* does not always help; below a threshold,
the lock overhead dominates and you should instead make it *less frequent* (batching) or
*less shared* (sharding).

The GIL build is flat because the GIL has already serialized everything — adding threads
cannot add contention that isn't already there. This is the same phenomenon as
[`24-the-gil.md`](24-the-gil.md) §7's convoy, seen from the other side: under the GIL
your `Lock` is nearly free because it is nearly never contended (§10); under
free-threading it becomes the bottleneck it always logically was.

> **Noise correction.** My first pass at this table, single-run, showed the GIL build
> dropping to **0.51× at 8 threads and 0.37× at 5 µs** — which would have been a
> dramatic and completely false finding. `load1` was ~2.6 during the session. Re-running
> with medians of 5 passes showed run-to-run spread of only 1.01–1.04× and a flat curve.
> **A single measurement of a contended benchmark on a loaded machine is worthless.** See
> [`31-measurement-methodology.md`](31-measurement-methodology.md).

---

## 10. Starvation is a GIL artifact — the experiment that proves it

§8.4 showed 5 of 8 threads getting zero lock acquisitions on the GIL build and perfect
fairness on the free-threaded build. The obvious explanation — "the free-threaded build
has a fairer lock" — is **wrong**, and the source proves it.

### 10.1 Both builds use the same lock

`Modules/_threadmodule.c` (3.14), the `_thread.lock` object:

```c
typedef struct {
    PyObject_HEAD
    PyMutex lock;
} lockobject;
```

with `_PyMutex_LockTimed` / `_PyMutex_TryUnlock` / `PyMutex_IsLocked` doing the work.
`threading.Lock` **is** `PyMutex` on both builds. Same code, same fairness policy.

### 10.2 And `PyMutex` is explicitly fair

`Python/lock.c`:

```c
// If a thread waits on a lock for longer than TIME_TO_BE_FAIR_NS (1 ms), then
// the unlocking thread directly hands off ownership of the lock. This avoids
// starvation.
static const PyTime_t TIME_TO_BE_FAIR_NS = 1000*1000;

// Spin for a bit before parking the thread. This is only enabled for
// `--disable-gil` builds because it is unlikely to be helpful if the GIL is
// enabled.
#if Py_GIL_DISABLED
static const int MAX_SPIN_COUNT = 40;
#else
static const int MAX_SPIN_COUNT = 0;
#endif
```

and the handoff itself:

```c
int should_be_fair = now > entry->time_to_be_fair;
entry->handed_off = should_be_fair;
if (should_be_fair) {
    v |= _Py_LOCKED;          // hand ownership directly to the waiter
}
```

This is **eventual fairness** in the WebKit `WTF::Lock` / ParkingLot style: barge freely
for throughput, but if any waiter has been parked longer than 1 ms, hand the lock
straight to it. It cannot starve a waiter for more than ~1 ms.

### 10.3 So why did 5 threads get zero?

**Because under the GIL nobody ever becomes a waiter.** A thread holding the GIL acquires
the lock, does the (empty) critical section, and releases it — all within its 5 ms GIL
slice, with the lock uncontended every time. The fast path never parks anyone.
`time_to_be_fair` is never consulted because there is no `mutex_entry`. The starvation is
in the **GIL's** scheduling, not the lock's.

Prediction: widen the critical section until it spans a GIL switch, and fairness must
return. Measured, 8 threads:

| critical section | GIL build | free-threaded |
|---|---|---|
| ~0 µs (empty) | **7/8 starved**, spread 40,000× | 0/8 starved, spread 1.2× |
| ~12 µs | 0/8 starved, spread 6.1× | 0/8 starved, spread 1.1× |
| ~120 µs | 0/8 starved, spread 1.3× | 0/8 starved, spread 1.0× |
| ~1.2 ms | 0/8 starved, spread 1.0× | 0/8 starved, spread 1.0× |
| ~10 ms (> switch interval) | 0/8 starved, spread 1.0× | 0/8 starved, spread 1.0× |

Confirmed exactly. With an empty critical section **one thread took all 40,000
acquisitions** (spread 40,000×). At ~12 µs, starvation is gone. The free-threaded build
is fair at every width, because there the lock is genuinely contended and `PyMutex`'s
handoff does its job.

**The lesson generalizes beyond this experiment.** On a GIL build, a lock that
*looks* fine in a microbenchmark may be hiding total starvation, because your benchmark's
critical section is too short to ever contend. The same code under free-threading, or
under a real workload with I/O inside the lock, behaves completely differently. This is
one more instance of §1's thesis.

---

## 11. Priority inversion, and why Python mostly can't see it

**Priority inversion:** a high-priority thread waits on a lock held by a low-priority
thread, which is itself preempted by a medium-priority thread that has nothing to do
with the lock. The high-priority thread is now effectively running at the medium
thread's priority. The famous case is the **Mars Pathfinder** (1997), whose repeated
resets in flight were traced to exactly this and fixed by enabling priority inheritance
on a mutex — remotely, from Earth.

The classic remedies:

| remedy | mechanism |
|---|---|
| **Priority inheritance** | The lock holder temporarily inherits the highest priority among its waiters |
| **Priority ceiling** | The lock has a fixed priority; anyone holding it runs at that priority |
| **Avoid priorities** | Most server software does this — one priority, no inversion |

### 11.1 Python's position

**The Python standard library provides no way to set thread priority.** There is no
`thread.set_priority`, no `nice` for threads, no scheduling-policy control in
`threading`. You can reach `pthread_setschedparam` through `ctypes`, and on macOS you can
influence QoS classes, but nothing in the documented API exposes it.

The consequence is mostly protective: **classic priority inversion is largely out of
reach in pure Python because you cannot create the priority differences that cause it.**
All your Python threads run at the same OS priority by default.

Where it can still reach you:

- **Across processes.** `multiprocessing` workers *can* be `nice`d, and they contend for
  real OS resources. Inversion is possible here.
- **Through C extensions** that create their own threads with explicit priorities.
- **Through the OS scheduler on asymmetric cores.** This machine has 5 performance and 6
  efficiency cores. macOS may schedule a lock-holding thread onto an E-core while a
  waiter sits on a P-core — a hardware analogue of priority inversion. I did not attempt
  to measure this (see §22).
- **In containers**, via cgroup CPU quota. A throttled cgroup can leave a lock holder
  descheduled for tens of milliseconds. This is the realistic modern version of the
  problem and it is a *container configuration* bug, not a Python one.

---

## 12. Lock ordering as an enforceable discipline

Breaking circular wait is the only Coffman remedy you can enforce mechanically. The rule:
**define a total order over all locks, and require that every thread acquires them in
that order.**

### 12.1 Assign every lock a rank

```python
import threading

class RankedLock:
    """A lock that refuses to be acquired out of order.

    Ranks must be acquired in strictly increasing order within a thread.
    Violations raise immediately -- at the moment of the mistake, on the
    thread that made it, with both locks named -- instead of deadlocking
    at 3 a.m. under load.
    """
    _local = threading.local()

    def __init__(self, rank: int, name: str):
        self._lock = threading.Lock()
        self.rank = rank
        self.name = name

    def __enter__(self):
        held = getattr(self._local, 'held', None)
        if held is None:
            held = self._local.held = []
        if held and held[-1].rank >= self.rank:
            raise RuntimeError(
                f"lock order violation: holding {held[-1].name}"
                f"(rank {held[-1].rank}) while acquiring {self.name}"
                f"(rank {self.rank}); ranks must strictly increase"
            )
        self._lock.acquire()
        held.append(self)
        return self

    def __exit__(self, *exc):
        self._local.held.pop()
        self._lock.release()
```

This converts a probabilistic, load-dependent, hard-to-reproduce deadlock into a
deterministic exception that fires on the *first* violating code path, in unit tests, on
one thread, with no concurrency required at all. That last point is the important one:
**you can detect lock-order violations without ever running the threads concurrently.**

### 12.2 When you cannot order

Sometimes the order is data-dependent — transferring between two accounts, where the
locks are per-account:

```python
def transfer(a, b, amount):
    first, second = (a, b) if id(a) < id(b) else (b, a)   # total order by identity
    with first.lock, second.lock:
        ...
```

Ordering by `id()` works but is not stable across processes; order by a persistent key
(account id) when one exists.

### 12.3 Timeouts as a backstop, not a solution

```python
if not lock.acquire(timeout=5.0):
    raise ResourceBusy(...)      # release everything, back off, retry
```

This breaks *no preemption* and guarantees you notice. It does **not** guarantee
progress — retrying in lockstep is §8.3's livelock. If you use timeouts as a deadlock
backstop, add **randomized** backoff, and alarm on the timeout: a fired lock timeout is
always a bug report, never a normal condition.

---

## 13. Cooperative vs preemptive scheduling

These are the two ways a scheduler can take control away from running code, and they
have opposite correctness properties.

| | **preemptive** (threads) | **cooperative** (asyncio) |
|---|---|---|
| Who decides to switch | The runtime/OS, at any check point | The code, at `await` |
| Switch points | The 22 instructions of §4, plus any blocking syscall | Exactly the `await`s you wrote |
| Can you reason about atomicity? | Barely — check points are invisible | **Yes — the switch points are in the source** |
| Failure mode | Races everywhere | One blocking call stalls everything |
| Latency under a CPU hog | Bounded by the timeslice | **Unbounded** |

### 13.1 The correctness win of cooperative scheduling

Under asyncio, a coroutine runs uninterrupted between `await`s. So the check-then-act of
§6 becomes *safe*, as long as there is no `await` in the window:

```python
if key not in d:      # CHECK
    d[key] = value    # ACT -- no await between: indivisible
```

Measured: 8 concurrent tasks × 50,000 iterations, **50,000 keys, zero clobbers**, by
construction.

Insert one `await` into the window and it breaks immediately:

```python
if key not in d:              # CHECK
    await asyncio.sleep(0)    # <-- suspension point inside the window
    d[key] = value            # ACT
```

Measured: 8 tasks × 1,000 iterations → **7,000 detected clobbers**.

> **The rule this gives you, which is genuinely valuable:** in async code, `await` is the
> only place a race can occur. Auditing async code for races is *tractable* — grep for
> `await` inside your critical sections. Auditing threaded code for races is not, because
> the switch points are invisible (§4). This is the strongest correctness argument for
> asyncio and it is rarely stated.
>
> The corollary is a trap: **adding an `await` to an existing function is a
> potentially-breaking change** to every caller's atomicity assumptions. Turning a sync
> helper into an async one is not a refactor; it is a concurrency change.

### 13.2 The liveness loss

Cooperative scheduling cannot preempt. One synchronous call blocks the entire loop.
Measured — a ticker asking to run every 1 ms, while another coroutine makes a 300 ms
synchronous call:

| scheduler | p50 | p99 | max |
|---|---|---|---|
| **asyncio** (300 ms sync call in a coroutine) | 1.18 ms | **306.07 ms** | **306.1 ms** |
| **threads** (300 ms CPU-bound work in a thread) | 2.74 ms | **7.58 ms** | 7.6 ms |

**A 40× difference in tail latency.** The threaded version's 7.58 ms p99 is roughly the
GIL switch interval (5 ms) plus scheduling noise — exactly what preemption buys you. The
asyncio version's p99 *is* the length of the blocking call, because nothing can take
control back.

This is the single most common asyncio production incident, and its signature is
distinctive: **p50 stays healthy while p99 tracks the duration of whatever blocking call
you accidentally introduced.** See [`28-asyncio-internals.md`](28-asyncio-internals.md)
§17 for `asyncio`'s debug-mode slow-callback detection, which exists precisely to catch
this.

---

## 14. Clocks: the correctness bug hiding in your timeouts

Timeouts, rate limits, retry backoff, cache expiry, and lease renewal are all concurrency
control. All of them are wrong if they use the wrong clock.

`time.get_clock_info()` on this machine:

| clock | monotonic | adjustable | resolution | implementation |
|---|---|---|---|---|
| `time.time` | **False** | **True** | 1e-06 | `clock_gettime(CLOCK_REALTIME)` |
| `time.monotonic` | True | False | 4e-08 | `mach_absolute_time()` |
| `time.perf_counter` | True | False | 4e-08 | `mach_absolute_time()` |
| `time.process_time` | True | False | 1e-06 | `clock_gettime(CLOCK_PROCESS_CPUTIME_ID)` |
| `time.thread_time` | True | False | 4e-08 | `clock_gettime(CLOCK_THREAD_CPUTIME_ID)` |

**`time.time()` is `adjustable=True` and `monotonic=False`.** NTP can step it. A VM can
resume with a corrected clock. A leap second can be smeared into it. An admin can set it.
It can go **backwards**.

```python
# WRONG -- can wait forever, or return instantly, if the wall clock moves
deadline = time.time() + 30
while time.time() < deadline:
    ...

# RIGHT
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    ...
```

The failure is rare and catastrophic: if the clock jumps backwards by an hour, a 30-second
timeout becomes a 60-minute hang, on every thread waiting on that deadline, simultaneously.

Measured resolution and cost (smallest observed nonzero delta over 200,000 samples; cost
over 500,000 calls):

| clock | observed resolution | cost per call |
|---|---|---|
| `time.time` | 715.3 ns | 37.7 ns |
| `time.monotonic` | 82.9 ns | 40.1 ns |
| `time.perf_counter` | 82.9 ns | 43.0 ns |
| `time.monotonic_ns` | — | 47.8 ns |
| `time.perf_counter_ns` | — | 41.7 ns |

Two things worth noticing. `time.time()`'s *observed* resolution is **8.6× coarser** than
`monotonic`'s despite both being cheap — do not use it to measure short durations. And
all of them cost ~40 ns, which is 2–3× a dict lookup: **clock calls inside a hot loop are
a real cost**, and a common accidental one in retry/backoff code.

### 14.1 Clock drift between machines

Everything above is one machine. Across machines the situation is worse, and it is a
distributed-systems problem rather than a Python one, but two rules belong in any
concurrency review:

- **Never compare timestamps taken on different machines to order events.** Clock skew
  between well-synchronized NTP hosts is typically single-digit milliseconds and is
  occasionally *much* worse. Use logical clocks (Lamport, vector) or a sequencer.
- **Never use a wall-clock timestamp as a lease deadline** without accounting for skew.
  The classic failure is two nodes both believing they hold the lease.

The stdlib gives you `time.monotonic()` per process; it is meaningless across processes
and undefined across reboots.

---

## 15. Cache thrashing, false sharing, NUMA — correctness-adjacent

These are performance failures, not correctness failures — a false-sharing bug computes
the right answer slowly. They earn a place here because **they are the reason correct
concurrent code fails to scale**, and because engineers routinely misdiagnose them as
lock contention and "fix" them by removing locks, which *does* create correctness bugs.

[`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) covers the
mechanism in full (§3 cache lines, §5 MESI, §6 false sharing and this machine's 128-byte
lines, §9 NUMA). The correctness-relevant summary:

**False sharing** — two threads write to different variables that happen to share a cache
line. No logical sharing, full coherence traffic. The line ping-pongs; both threads slow
down dramatically.

**True sharing** — two threads write the same variable. Same traffic, but here it is
inherent to the algorithm, and the fix is algorithmic (sharding, per-thread accumulation)
not layout.

**What this means for Python specifically:** at the Python level you have essentially no
control over object layout, so you cannot deliberately pad against false sharing. What
you *can* do is avoid the pattern that guarantees true sharing — a single shared counter
or dict updated by every thread. [`26-free-threading.md`](26-free-threading.md) §7 measured
this as the **sharing wall**: a shared dict scaled at **0.32×** under free-threading,
i.e. slower than the GIL build. The fix is per-thread state merged at the end:

```python
# Instead of one shared counter:
results = [collections.Counter() for _ in range(nthreads)]   # per-thread
# ... each thread touches only results[my_index] ...
total = sum(results, collections.Counter())                  # merge once
```

**NUMA.** This machine is UMA — a single Apple-silicon package with unified memory, so I
could not measure NUMA effects here at all. On a multi-socket server, memory has an
affinity, and a thread accessing another socket's memory pays roughly 1.5–2× the latency.
For CPython the relevant consequence is that **`fork()`-based multiprocessing plus
refcounting is a NUMA worst case**: children touch shared pages, refcount writes dirty
them, copy-on-write fires, and the copies land on whichever node faulted them.
`gc.freeze()` before forking is the mitigation — see
[`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md) and
[`22-garbage-collection.md`](22-garbage-collection.md).

---

## 16. Progress guarantees: wait-free, lock-free, obstruction-free

[`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md) §1 defines these
precisely and §8 measures that lock-free is usually *slower* than a lock. What belongs
here is the correctness framing: these are **liveness guarantees**, and they are exactly
the guarantees §8's failures violate.

| guarantee | promise | rules out |
|---|---|---|
| **Wait-free** | Every thread finishes in a bounded number of its own steps | deadlock, livelock, **starvation** |
| **Lock-free** | *Some* thread always makes progress | deadlock, livelock — **not starvation** |
| **Obstruction-free** | A thread running alone finishes | deadlock — not livelock, not starvation |
| **Blocking** (a mutex) | Nothing | nothing |

Read the last row carefully. **A `threading.Lock` offers no progress guarantee at all.**
It can deadlock (§8.1), and §8.4 measured it starving 5 of 8 threads. `PyMutex`'s 1 ms
fairness handoff (§10.2) is a *practical* mitigation, not a formal bound.

The reason we use blocking locks anyway is §9 plus doc 03 §8: they are faster, vastly
simpler, and correct-by-construction in a way lock-free code is not. **Choose a lock, and
buy your liveness with discipline (§12) rather than with algorithms.**

The one place the distinction bites in Python: `queue.Queue` is blocking and can deadlock
your shutdown if a producer dies while consumers wait. `queue.SimpleQueue` is
"reentrant-safe" but equally blocking. Neither is lock-free. Always shut down with
sentinels and timeouts.

### 16.1 What wait-freedom actually costs: the helping mechanism

Lock-free is achievable with a CAS retry loop. **Wait-freedom is not**, because a retry
loop has no bound — that is exactly the gap between the two. Every wait-free algorithm
therefore needs a way for a thread that *would* have retried forever to instead be
carried across the line by somebody else. That mechanism is called **helping**:

> Before completing its own operation, a thread must first check whether other threads
> have pending operations and, if so, **complete them on their behalf.**

This is the whole idea, and it explains every property wait-free algorithms have:

```
   LOCK-FREE (Treiber stack)             WAIT-FREE (helping)
   ─────────────────────────             ───────────────────────────────
   loop:                                 1. announce my operation in a
     read head                              shared "state array"
     build new node                     2. scan the array for pending
     CAS(head, old, new)                   operations older than mine
     if failed: goto loop  ◄── unbounded 3. complete THOSE first
                                        4. complete (or discover someone
   Thread A can lose every race            already completed) mine
   forever. Some thread always
   progresses. A might never.            No thread can be lapped, because
                                         everyone else is obliged to finish
                                         its work before finishing their own.
```

**Michael–Scott → Kogan–Petrank.** The canonical worked example. Michael & Scott's queue
(doc 03 §6) is lock-free. Kogan & Petrank (*Wait-Free Queues With Multiple Enqueuers and
Dequeuers*, PPoPP 2011) make it wait-free by adding exactly the machinery above: each
operation gets a monotonically increasing **age-based priority**, is published in a
**state array**, and younger operations are required to help older ones first. The
underlying algorithm is unchanged. The wait-freedom is entirely in the bookkeeping.

And the bookkeeping is the cost. Every operation now pays an announce, a scan of an
array proportional to the thread count, and possible duplicate work. This is why
wait-free structures are rare in production: **you pay the worst case on every
operation to bound the worst case.** Doc 03 §8 already measured lock-free losing to a
plain mutex; wait-free is a further step in the same direction.

### 16.2 Herlihy's consensus hierarchy: why CAS is special

Maurice Herlihy's *Wait-Free Synchronization* (ACM TOPLAS, January 1991) is the paper
that made this a science rather than a collection of tricks. Its result:

> Assign each synchronization primitive a **consensus number** — the maximum number of
> threads for which that primitive can solve wait-free consensus. A primitive with
> consensus number *n* can build a wait-free implementation of **any** object for *n*
> threads, and cannot do it for *n+1*.

| primitive | consensus number |
|---|---|
| atomic read/write registers | **1** |
| test-and-set, swap, fetch-and-add, plain queue, plain stack | **2** |
| *n*-register assignment | 2*n* − 2 |
| **compare-and-swap**, **LL/SC** | **∞** |

Two consequences that are worth carrying around permanently:

1. **You cannot build a wait-free anything for two threads out of plain loads and
   stores.** Not with cleverness, not with more variables — it is an impossibility
   proof, not a difficulty claim. This is why every lock-free structure in doc 03 bottoms
   out in a CAS.
2. **CAS is universal.** Consensus number ∞ means CAS plus registers can implement any
   object wait-free for any number of threads. Herlihy proved this constructively with
   the **universal construction**: represent the object as a linked list of operations,
   have threads CAS their operation onto the list, and let the list order define the
   linearization. It is universal and it is far too slow to use directly — its value is
   the proof that no *stronger* primitive is needed, which is why hardware vendors ship
   CAS and stop.

This is also the theoretical reason §17's transactional memory was attractive: TM
promises composable atomicity without making you build the helping machinery by hand.

### 16.3 Two flavours of wait-free

The literature distinguishes bounds that matter when you read papers:

- **Wait-free (bounded)** — a bound exists as a function of the thread count *n*.
  Kogan–Petrank is O(*n*) per operation.
- **Wait-free (population-oblivious)** — the bound does not depend on *n* at all. This is
  the strong form, and it is rare. An atomic `fetch_add` on hardware with a native
  instruction is population-oblivious: one instruction, always, whatever else is running.

### 16.4 CPython's hottest operation is wait-free — verified

Here is where this stops being theory. `Py_INCREF` runs billions of times in any Python
program. From `Include/refcount.h` (3.14), free-threaded build:

```c
static inline Py_ALWAYS_INLINE void Py_INCREF(PyObject *op)
{
#if defined(Py_GIL_DISABLED)
    uint32_t local = _Py_atomic_load_uint32_relaxed(&op->ob_ref_local);
    uint32_t new_local = local + 1;
    if (new_local == 0) {
        _Py_INCREF_IMMORTAL_STAT_INC();
        // local is equal to _Py_IMMORTAL_REFCNT_LOCAL: do nothing
        return;                                              // ── path 1
    }
    if (_Py_IsOwnedByCurrentThread(op)) {
        _Py_atomic_store_uint32_relaxed(&op->ob_ref_local, new_local);   // ── path 2
    }
    else {
        _Py_atomic_add_ssize(&op->ob_ref_shared, (1 << _Py_REF_SHARED_SHIFT));  // ── 3
    }
```

Classify each path:

| path | when | operations | guarantee |
|---|---|---|---|
| **1. Immortal** | `None`, `True`, small ints, interned strings — `ob_ref_local` is `UINT32_MAX`, so `+1` wraps to 0 | relaxed load, add, compare, return | **wait-free, population-oblivious** |
| **2. Owned by this thread** | biased reference counting's fast path | relaxed load, relaxed store — **no atomic RMW at all** | **wait-free, population-oblivious** |
| **3. Shared** | another thread owns the object | one `fetch_add` | **wait-free** on this machine (LSE `ldadd`); see below |

**There is no retry loop anywhere in it.** No CAS, no compare-exchange, no `goto`. That
is not an accident — it is the central design achievement of PEP 703, and it is why
free-threading has a single-digit-percent single-thread tax
([`26-free-threading.md`](26-free-threading.md) §3 measured +8.1% here) instead of the
~30% the Gilectomy paid for naive atomic refcounts ([`24-the-gil.md`](24-the-gil.md) §11).

Path 3 carries a hardware caveat that ties directly to
[`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md) §9 and §11. A
`fetch_add` is wait-free **only if the hardware has a native fetch-add instruction**:

- **This machine** advertises `FEAT_LSE` (verified via `sysctl`, §17), so `fetch_add`
  compiles to a single `ldadd` — one instruction, bounded, **wait-free**.
- **x86-64** has `lock xadd` — likewise wait-free.
- **Pre-LSE ARMv8** has only LL/SC, so `fetch_add` becomes an
  `ldxr`/`add`/`stxr`/retry loop — **lock-free but *not* wait-free**, because the
  exclusive monitor can be stolen arbitrarily many times.

So the same C source is wait-free on this laptop and merely lock-free on a 2015 ARM
server. That is the sharpest illustration in this folder of why progress guarantees are
properties of the *compiled program on specific hardware*, not of the source text — and
it is doc 02 §11's "why `fetch_add` beats a CAS loop" result restated as a liveness
property rather than a performance one.

### 16.5 What is wait-free at the Python level

Nothing you write. There is no CAS in the Python language, `threading.Lock` is blocking
(§16's table), and every container operation ultimately takes a lock on the free-threaded
build. **You cannot write a wait-free algorithm in pure Python**, and the runtime
pieces that *are* wait-free — the refcount paths above, QSBR's read side (doc 03 §11) —
are below the level you can reach.

The practical value of this section is therefore diagnostic rather than constructive:

- When someone proposes "let's make this lock-free for latency," §16's table plus doc 03
  §8 tells you it will likely be **slower** and will not fix starvation unless it is
  *wait*-free, which it almost certainly won't be.
- When you need a genuine latency bound — audio, control loops, a signal handler — the
  answer in Python is not a clever data structure. It is to move that work out of Python
  entirely, into a C or Rust extension where the primitives exist.

---

## 17. Transactional memory, and why it is not on your menu

**Transactional memory (TM)** lets you mark a block as atomic and have the system detect
conflicts and roll back — composable atomicity without lock ordering. It is the cleanest
theoretical answer to §12.

**Hardware TM (HTM)** shipped and largely retreated:

- **Intel TSX/RTM** (Haswell, 2013) was disabled by microcode on several generations for
  correctness errata and then Spectre-class side channels; broadly unavailable now.
- **IBM POWER8+** and **z Systems** have working HTM; z/OS uses it in production.
- **ARM TME** (Transactional Memory Extension) is an *optional* Armv9 feature.

**On this machine it does not exist.** Verified two ways:

```
$ sysctl hw.optional.arm.FEAT_TME
sysctl: unknown oid 'hw.optional.arm.FEAT_TME'

$ sysctl -a | grep -ci FEAT_TME
0
```

The M3 Pro advertises **79** `hw.optional.arm.*` features — including `FEAT_LSE`, the
large-system atomics that make CAS cheap here (see
[`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md) §9) — and TME is not
among them. Clang for `arm64-apple-darwin25.5.0` does not expose `__tstart`/`__tcommit`.

**Software TM (STM)** exists in Python's world: **PyPy-STM** (Armin Rigo, ~2012–2015)
replaced the GIL with software transactions. It worked, demonstrated real parallelism, and
was abandoned — the overhead (2–5× reported by the project) and the difficulty of
handling I/O and non-transactional side effects inside transactions made it impractical.
Its intellectual descendant is visible in PEP 703's *avoidance* of TM in favour of biased
reference counting and per-object locks (see [`24-the-gil.md`](24-the-gil.md) §12).

**Practical takeaway:** TM is not an option for Python correctness work today, on this
hardware or any hardware you are likely to deploy on. The composability problem it was
designed to solve remains, and §12's lock ordering remains the answer.

---

## 18. WebAssembly: the platform where blocking is illegal

There is a line in the `_CHECK_PERIODIC` source quoted in §4.3 that I passed over:

```c
op(_CHECK_PERIODIC, (--)) {
    _Py_CHECK_EMSCRIPTEN_SIGNALS_PERIODICALLY();     // <-- this
    QSBR_QUIESCENT_STATE(tstate);
    ...
```

It is there because **WebAssembly has no signals**, and it opens onto a platform whose
concurrency model is the strictest constraint CPython runs under — and the one place
where §16's wait-freedom stops being academic.

### 18.1 Why CPython polls for Ctrl-C on WASM

On a POSIX system, `Ctrl-C` arrives as `SIGINT`; the handler sets a flag and the eval
breaker notices. WASM has no signal delivery at all, so there is nothing to interrupt the
interpreter. CPython's answer is to **poll a shared memory location** at the same check
points that handle everything else. From `Include/internal/pycore_emscripten_signal.h`
(3.14, 30 lines in full):

```c
#if defined(__EMSCRIPTEN__)
void _Py_CheckEmscriptenSignals(void);
void _Py_CheckEmscriptenSignalsPeriodically(void);
#define _Py_CHECK_EMSCRIPTEN_SIGNALS()             _Py_CheckEmscriptenSignals()
#define _Py_CHECK_EMSCRIPTEN_SIGNALS_PERIODICALLY() _Py_CheckEmscriptenSignalsPeriodically()
extern int Py_EMSCRIPTEN_SIGNAL_HANDLING;
extern int _Py_emscripten_signal_clock;
#else
#define _Py_CHECK_EMSCRIPTEN_SIGNALS()
#define _Py_CHECK_EMSCRIPTEN_SIGNALS_PERIODICALLY()
#endif
```

On every other platform both macros expand to **nothing** — which is why the line costs
you zero on this laptop. And the polling is itself rate-limited by a countdown, visible
in `RESUME_CHECK` (`bytecodes.c`, §4.3):

```c
inst(RESUME_CHECK, (--)) {
#if defined(__EMSCRIPTEN__)
    DEOPT_IF(_Py_emscripten_signal_clock == 0);
    _Py_emscripten_signal_clock -= Py_EMSCRIPTEN_SIGNAL_HANDLING;
#endif
```

When the clock runs out, the specialized instruction **deoptimizes** back to the generic
`RESUME`, which does the real check. Interrupt handling is thereby folded into the
adaptive interpreter's existing deopt machinery rather than costing a branch in the hot
path (see [`20-eval-loop.md`](20-eval-loop.md)).

In Pyodide the browser side of this is `pyodide.setInterruptBuffer()`: you allocate a
`SharedArrayBuffer`, hand it to the Pyodide worker, and the main thread writes a `2`
(SIGINT) into it. The worker's interpreter polls it at the §4.3 check points. **Ctrl-C in
a Python REPL in your browser is a shared-memory poll**, and it is the same 22
instructions doing the work.

### 18.2 The platform, dated

| target | what it is | PEP 11 tier |
|---|---|---|
| `wasm32-emscripten` | Python in the browser / Node.js — Pyodide, PyScript, JupyterLite | **Tier 3** (as of 3.14) |
| `wasm32-wasi` | WASM on the server, in a POSIX-like capability sandbox | **Tier 2** |

Tier 3 for Emscripten was approved by the Steering Council on **2024-10-25** and is
documented by **[PEP 776](https://peps.python.org/pep-0776/), "Emscripten Support"**
(Hood Chatham, Informational, Active, created 2025-03-18, Python-Version 3.14).

PEP 776 is blunt about the platform's limits:

> Emscripten is a POSIX platform. However, there are POSIX APIs that exist but always
> fail when called and POSIX APIs that don't exist at all. In particular, there are
> problems with networking APIs and blocking I/O, and there is **no support for
> `fork()`**.

### 18.3 The threading model, and the restriction that changes everything

WASM threads are **Web Workers** plus a **`SharedArrayBuffer`** plus the `Atomics.*`
operations. There is no `fork`, no `clone`, no thread that shares a normal address space
— shared memory is an explicitly allocated buffer, and everything else is copied via
`postMessage`.

Then comes the rule that makes WASM unlike every other platform in this document:

> **`Atomics.wait()` is forbidden on the browser's main thread.**

Blocking the main thread freezes the page, so the platform simply refuses. The
consequences cascade:

- **You cannot implement a mutex on the main thread.** Not slowly, not badly — at all.
  A mutex needs a way to wait, and the only blocking wait primitive is unavailable.
- **Therefore blocking synchronization is not an option there, and §16's progress
  guarantees stop being a design preference and become a hard requirement.** On the main
  thread your choices are: spin (burning the UI thread, usually unacceptable),
  `Atomics.waitAsync` (non-blocking, but it makes the operation asynchronous and
  restructures your code), or a genuinely **lock-free/wait-free** algorithm.
- This is the clearest real-world answer to "when would I ever need lock-free code?" that
  a Python engineer is likely to meet. It is not high-frequency trading. It is the
  browser tab.

Worker threads *may* call `Atomics.wait`, so a normal mutex works there. The asymmetry —
one thread in the process that physically cannot block, all others fine — has no analogue
in POSIX and breaks the assumption every threading library makes.

### 18.4 What this means for Python specifically

**Pyodide ships without threads at all.** PEP 776, on why:

> Enabling threading requires websites to be served with special security headers that
> indicate acceptance of the possibility of Spectre-style information leakage. These
> headers are a usability hazard for users who are not intimately familiar with the web
> platform.
>
> If an executable is linked with both threading and a dynamic loader, Emscripten prints
> a warning that using dynamic loading and pthreads together is experimental. It may
> cause performance problems or crashes.
>
> **Because of these limitations, Pyodide standardizes a no-pthreads build of Python.**

The "special security headers" are cross-origin isolation — `Cross-Origin-Opener-Policy:
same-origin` and `Cross-Origin-Embedder-Policy: require-corp`. Without both, the browser
refuses to hand out a `SharedArrayBuffer`, and threading silently isn't available. **This
is a concurrency correctness property controlled by your HTTP response headers**, which
is a sentence worth sitting with. It is a post-Spectre mitigation: shared memory plus a
high-resolution timer is a cache side channel, so the platform makes you opt in.

You can detect the situation at runtime:

```python
import sys
info = sys._emscripten_info          # provisional; Emscripten only, 3.11+
info.pthreads         # True if built with Emscripten pthreads support
info.shared_memory    # True if built with shared memory support
info.runtime          # e.g. the browser user agent, or 'Node.js v14.18.2'
```

So the practical concurrency model for Python in the browser is:

| model | works in Pyodide? |
|---|---|
| `threading` with real parallelism | ❌ — no pthreads in the standard build |
| `multiprocessing` | ❌ — no `fork()` |
| `asyncio` | ✅ — and it maps naturally onto the JS event loop |
| Multiple Workers, each with its own interpreter | ✅ — but they share nothing; message-passing only |
| Free-threading (PEP 703) | ❌ — needs threads to be meaningful |

**The browser forces the actor model on you**, and the correctness consequence is
entirely positive: no shared mutable state means none of §5, §6, or §8 can happen. You
trade every race in this document for serialization costs at the `postMessage` boundary.

### 18.5 Why this section belongs in a correctness document

Three transferable lessons, none of which require you to ever ship WASM:

1. **"Cooperative scheduling" has a strictest case, and it is instructive.** §13 measured
   asyncio's p99 blowing out to 306 ms because one call wouldn't yield. On the WASM main
   thread that failure mode is not a bug you might introduce — it is *enforced by the
   platform*, which removed the ability to block precisely because the failure was
   otherwise inevitable. Sometimes the fix for a liveness problem is to make the
   dangerous operation impossible.
2. **Progress guarantees become mandatory when blocking is unavailable.** §16 argued you
   should almost never write lock-free code. WASM's main thread is the exception that
   proves the rule and shows what the exception looks like: not "we want more
   throughput," but "the primitive does not exist."
3. **Concurrency capability can be a deployment property.** Two identical Python programs,
   one served with COOP/COEP and one without, have different concurrency models. That is
   the same lesson as §1 — the schedule set is not a property of your source — arriving
   from a completely different direction.

---

## 19. Work-stealing: what Python does not have

**Work-stealing** gives each worker its own deque, pushing and popping from its own end
(cheap, uncontended) and *stealing* from the other end of a victim's deque when idle. It
is how Java's `ForkJoinPool`, Go's runtime, Rust's Rayon, and Intel TBB scale.
It reduces contention on the shared queue from O(tasks) to O(steals).

**`concurrent.futures.ThreadPoolExecutor` does not do this.** Verified by reading
`concurrent/futures/thread.py` in 3.14:

```python
self._work_queue = queue.SimpleQueue()      # ONE queue, shared by all workers
```

and the workers all do `work_queue.get(block=True)` from it. Searching the module source:
`'steal'` → **not present**; `'deque'` → **not present**. `ProcessPoolExecutor` likewise
uses a single shared work queue.

`queue.SimpleQueue` is implemented in C (`_queue`, a builtin module), so the enqueue and
dequeue are single atomic C calls (§5) and it is not a correctness problem. It is a
**scalability** one: every worker contends on the same queue head.

Why Python gets away with it, and when it stops getting away with it:

- Under the **GIL**, contention on the work queue is irrelevant — the GIL is the
  bottleneck, and a work-stealing deque would optimize something that isn't the problem.
- Under **free-threading**, this becomes a real limit, and it is exactly §9's convoy: a
  shared queue with a short critical section is the worst case. If you are dispatching
  many small tasks to many threads on a free-threaded build, expect the pool itself to be
  your ceiling.

If you need work-stealing today, the options are: shard the work yourself into
per-thread queues with a simple steal protocol; move the parallel section into a native
extension that has its own pool (NumPy/Rayon/TBB); or use `multiprocessing` with
per-worker chunking. For correctness purposes the important note is that **a
hand-rolled work-stealing deque is a lock-free data structure** — the Chase–Lev deque is
the standard one — and inherits every hazard in
[`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md), including ABA and
reclamation. Do not write one in Python; you cannot, and you would not want to.

---

## 20. Testing and fuzzing concurrent code

The bad news from §1: tests sample the schedule space. The good news: you can bias the
sampling toward the dangerous regions, and you can make failures much more likely than
production will.

### 19.1 Fuzz the scheduler with `sys.setswitchinterval`

This is the highest-value, lowest-effort technique available to a Python engineer, and
almost nobody uses it. §4.1 measured the effect: switching from the 5 ms default to 1 µs
raised the observed preemption count in a hot loop from **3 to 1,233** — a **411×
increase in scheduling pressure** for one line of code.

```python
# conftest.py
import sys, pytest, random

@pytest.fixture
def chaotic_scheduler():
    old = sys.getswitchinterval()
    sys.setswitchinterval(random.choice([1e-6, 1e-5, 1e-4]))
    try:
        yield
    finally:
        sys.setswitchinterval(old)
```

Caveats, both real:

- It costs wall time — the 1 µs setting took 0.017 s where 5 ms took 0.005 s (3.4×).
- **It cannot find the races §4 makes unreachable.** No switch interval will break
  `s.v += 1` on a GIL build, because the problem is *where* the check points are, not
  how often they fire. That is what §20.2 is for.

### 19.2 Run the suite on the free-threaded build

Given §3, this is now the most effective race detector available for Python code. The
same test that cannot fail on 3.14 fails 57% of the time on 3.14t.

```bash
uv python install 3.14t
uv run --python 3.14t pytest -x
```

Treat a suite that passes on the GIL build and fails on the free-threaded build as
**correct behaviour from the test suite**: it found a real bug that was always there.
The migration guides frame free-threading as risky; from a correctness standpoint it is
the best thing to happen to Python thread-safety testing in a decade.

### 19.3 Repeat, stress, and run tests concurrently

- **`pytest-run-parallel`** (0.9.1, 2026-06-03) — runs each test in multiple threads
  simultaneously. Built for exactly the free-threading audit above.
- **`pytest-repeat`** / `--count` — re-run flaky candidates many times.
- **`pytest-timeout`** (2.4.0) — essential. A deadlocked test otherwise hangs CI forever.
  Set a global timeout; a test that hits it is a liveness bug, not a slow test.
- **Hypothesis stateful testing** (`RuleBasedStateMachine`, 6.165.0) — generates
  operation *sequences* against a model. It does not generate *interleavings*, so it
  finds §6-style compound-operation bugs in a single-threaded model but not schedule
  bugs. See [`43-testing-strategy.md`](43-testing-strategy.md) §7, which measured it
  shrinking a buggy cache to a 3-step repro in 0.09 s.

### 19.4 Diagnosing a live hang, with the stdlib only

`faulthandler` needs no dependencies and works on a wedged process:

```python
import faulthandler
faulthandler.dump_traceback_later(60, exit=True)   # dump all threads, then die
```

Run against §8.1's deadlock, this printed the exact line each thread was stuck on:

```
Timeout (0:00:01)!
Thread 0x00000001700cb000 [worker-B] (most recent call first):
  File "dl_diag.py", line 14 in t2
  ...
Thread 0x000000016f0bf000 [worker-A] (most recent call first):
  File "dl_diag.py", line 9 in t1
```

Thread A stuck at line 9, thread B at line 14 — read the two lines, see the inversion,
done. **Put `faulthandler.dump_traceback_later()` in every long-running service**, or at
minimum `faulthandler.enable()` plus a `SIGQUIT` handler.

For a process you cannot modify, **`py-spy`** (0.4.2) attaches without cooperation:

```bash
py-spy dump --pid 12345          # one snapshot of every thread
py-spy top  --pid 12345          # live view
```

The distinguishing test between §8's failures, which is worth memorizing:

| symptom | CPU | stacks change between samples? | diagnosis |
|---|---|---|---|
| No progress | **~0%** | No | **Deadlock** |
| No progress | **~100%** | Yes | **Livelock** |
| Slow progress | ~100% | Yes | **Contention / convoy** |
| Some threads never progress | any | Yes | **Starvation** |

### 19.5 ThreadSanitizer

TSan is the real tool for detecting data races — but per §2.2, at the **C level**, which
means it is for extension authors and CPython contributors, not for pure-Python code.
Verified available on this machine (Apple clang 21.0.0 compiles `-fsanitize=thread`
successfully), but note that **the stock CPython here is not a TSan build** —
`sysconfig` shows `-O3` with no sanitizer flags. Using it means building CPython with
`--with-thread-sanitizer` (and, for free-threading work, `--disable-gil`), then building
your extension against it. CPython's own CI runs a free-threaded TSan job; its suppression
file (`Tools/tsan/suppressions_free_threading.txt`) is a good map of the runtime's known
benign races.

### 19.6 What none of this gives you

No tool listed above *proves* the absence of races. For that you need model checking —
exhaustively enumerating interleavings of a small model (TLA+, SPIN) — which applies to
your *design*, not your Python source. For a genuinely subtle protocol (a lease
algorithm, a lock-free structure, a distributed handoff), specify it in TLA+ and check
it there; then implement the checked design straightforwardly and test the
implementation with everything above.

---

## 21. A review checklist

Concrete things to look for in a concurrency code review, in rough order of how often
they are the actual bug.

**Shared mutable state**
- [ ] Every piece of state touched by more than one thread is either immutable, thread-local, owned by exactly one thread, or protected by a lock.
- [ ] The lock protects an **invariant**, not an operation. Ask: "what must be true when no lock is held?"
- [ ] No `+=`, `-=`, `|=` or any read-modify-write on shared state outside a lock (§5).
- [ ] No check-then-act pair outside a lock; an atomic primitive is used where one exists (§6).

**Locks**
- [ ] Every lock has a documented rank, and acquisition order is globally consistent (§12).
- [ ] No lock is held across a blocking call, an I/O operation, or a callback into user code.
- [ ] No `Lock` is acquired re-entrantly (§8.2); `RLock` use is deliberate, not accidental.
- [ ] Critical sections are not so short that handoff dominates (§9) nor so long that they serialize the program.
- [ ] `acquire(timeout=)` is used as an alarm, and firing it raises/logs rather than silently retrying (§12.3).

**Liveness**
- [ ] Shutdown is explicit: sentinels, `Event`s, and joins with timeouts. No daemon thread is assumed to just die.
- [ ] Every retry loop has randomized backoff and a bound (§8.3).
- [ ] Queues are bounded, so a slow consumer applies backpressure instead of exhausting memory.

**Time**
- [ ] Every timeout, deadline, and backoff uses `time.monotonic()`, never `time.time()` (§14).
- [ ] No cross-machine timestamp comparison is used to order events.

**Async**
- [ ] No blocking call in a coroutine — no `requests`, no `time.sleep`, no sync DB driver, no big CPU loop (§13.2).
- [ ] Every check-then-act in async code has been checked for an `await` in the window (§13.1).
- [ ] Adding an `await` to an existing function was reviewed as a concurrency change, not a refactor.

**Testing**
- [ ] The suite runs on the free-threaded build in CI (§20.2).
- [ ] There is a global test timeout (§20.3).
- [ ] The service can dump all thread stacks on demand (§20.4).

---

## 22. What I could not verify

Stated explicitly, in the style this folder requires.

1. **Priority inversion on asymmetric cores.** §11 speculates that macOS may schedule a
   lock holder onto an E-core while a waiter occupies a P-core. **I did not measure
   this.** It is plausible and consistent with the hardware, but I have no data and no
   API from Python to control core affinity on macOS. Treat it as a hypothesis.

2. **Livelock frequency in real Python programs.** §8.3 produced a genuine zero-goodput
   livelock, but only by forcing lockstep with a `Barrier`. I have **no evidence** about
   how often livelock occurs naturally in Python, and my one unforced attempt failed to
   reproduce it. The mechanism is demonstrated; the incidence is unknown.

3. **The exact 3.10 boundary.** §3 shows 3.9 racing and 3.11 not racing, and §4.4 dates
   PR #18334 to 3.10. **I did not test 3.10 itself** — no 3.10 interpreter was installed
   and I did not add one. The attribution to that PR is inference from the merge date,
   the PR's stated content, and the observed 3.9/3.11 boundary, not from a bisect.

4. **Whether the 3.11–3.14 zeros are truly zero.** Three runs of 1.6 M increments each
   found no loss. That bounds the per-opportunity failure rate at roughly < 2×10⁻⁷, not
   at zero. A rarer schedule may exist. The claim "cannot be demonstrated" is
   well-supported; "cannot happen" is not, and I do not make it.

5. **`FEAT_TME` absence.** Verified by `sysctl` (oid absent, 0 matches among 79
   advertised ARM features) and by clang's intrinsics. I did **not** attempt to execute a
   `TSTART` instruction to confirm it faults.

6. **NUMA effects (§15).** Not measurable on this UMA machine. The 1.5–2× remote-access
   figure is standard published guidance for multi-socket x86 servers, not something I
   measured.

7. **Cross-machine clock skew figures (§14.1).** "Single-digit milliseconds for
   well-synchronized NTP" is general field knowledge; I measured only this machine's
   local clocks.

8. **PyPy-STM's 2–5× overhead (§17).** Taken from the project's own historical
   statements. PyPy-STM is long dead and I did not run it.

9. **Machine noise.** `load1` was 2.2–2.6 throughout. §9 documents one case where this
   produced a false result that single-run measurement would have published. Other
   timings here inherit the same risk; the ones I re-ran with medians are labelled.

10. **Everything in §18 is read, not run.** I did **not** build CPython for
    `wasm32-emscripten`, did not run Pyodide, and did not execute a single line of Python
    in a browser during this session. The WASM section is sourced entirely from CPython's
    headers and `bytecodes.c`, PEP 776's text, and the platform documentation for
    `Atomics.wait`. The claims about what Pyodide ships and what the main thread forbids
    are quotations from primary sources, not observations. In particular I have **no
    measurement** of what the Emscripten signal-poll costs on a real WASM build.

11. **Herlihy's consensus numbers (§16.2)** are quoted from the 1991 result, not
    re-derived. The impossibility proofs are load-bearing for the section's argument and
    I have taken them on the paper's authority.

12. **Kogan–Petrank (§16.1)** — I described the algorithm's structure from the paper's
    abstract and secondary descriptions. I did **not** implement or benchmark it, and I
    make no claim about its constant factors beyond the general "you pay the worst case
    on every operation" argument, which follows from the design rather than from data.

13. **The wait-freedom classification of `Py_INCREF` (§16.4)** is my analysis of the
    source, not a citation — PEP 703 does not use the term. The reasoning (no retry
    loop; `fetch_add` compiles to `ldadd` under `FEAT_LSE`, which I verified is present)
    is sound, but I did not disassemble the actual free-threaded binary to confirm the
    instruction selection. That is lab exercise 11.

---

## 23. Lab exercises

1. **Reproduce the generational table (§3).** Install 3.9 and 3.10 via
   `uv python install`, run the increment program on 3.9, 3.10, 3.11, 3.14, 3.14t, and
   settle open question §22.3: does 3.10 behave like 3.9 or like 3.11? Bisect CPython if
   it doesn't match the PR date.

2. **Find the check points yourself.** Write a program with a shared counter and a
   deliberately-placed function call. Move the call one bytecode at a time (using
   `dis` to confirm), and find the exact position at which loss appears. Compare with the
   22-instruction list in §4.3.

3. **Break the "atomic" primitives.** `list.append` is atomic. Build a program in which a
   sequence of two atomic list operations still corrupts your invariant. Then fix it with
   a lock and confirm the fix on the free-threaded build.

4. **Implement `RankedLock` (§12.1) and deploy it.** Add it to a real codebase of yours,
   assign ranks, and run the existing test suite. Anything that raises is a latent
   deadlock. Report how many you found.

5. **Measure your own convoy curve (§9).** Take a real lock from your codebase, measure
   throughput vs thread count vs critical-section length on both builds, and find the
   critical-section length at which contention stops mattering. Use medians of ≥5 passes.

6. **Reproduce the starvation result and explain it (§10).** Then predict, before
   measuring, what happens if you replace `threading.Lock` with `threading.Semaphore(1)`,
   and check.

7. **Build the async race auditor.** Write a tool using `ast` that finds every `await`
   lexically inside an `if x not in y:` / `y[x] = ...` window in an async codebase. Run
   it against a real project. (See [`42-runtime-code-manipulation.md`](42-runtime-code-manipulation.md).)

8. **Blocking-call detector.** Using §13.2's technique, build a monitor coroutine that
   samples loop lag and logs a stack dump whenever lag exceeds a threshold. Compare with
   asyncio's built-in debug mode.

9. **Run your suite free-threaded (§20.2).** Report: how many tests fail, and for each,
   is it a real bug or a test-harness assumption?

10. **Implement helping (§16.1).** Write a wait-free counter in C using an announce
    array: each thread publishes its pending increment, and every thread completes all
    pending announcements it finds before returning. Benchmark it against (a) a plain
    `fetch_add` and (b) a mutex, at 1/2/4/8 threads. You should find it loses badly to
    both. Explain why anyone would still want it.

11. **Settle §22.13 by disassembly.** Build or obtain a free-threaded CPython, find
    `Py_INCREF`'s three paths in the disassembly of a hot function, and confirm that
    path 3 compiles to a single `ldadd` (LSE) with no `ldxr`/`stxr` retry loop. Then
    cross-compile the same source for a pre-LSE ARMv8 target and confirm the retry loop
    appears — turning a wait-free operation into a merely lock-free one.

12. **Find the consensus number of `PyMutex`.** Given §16.2's hierarchy, argue what
    consensus number a blocking mutex has and why that is the wrong question. (Hint:
    consensus numbers classify *wait-free* solvability. What does a mutex assume about
    the scheduler?)

13. **Poll like Emscripten (§18.1).** Implement Ctrl-C over a shared buffer in ordinary
    CPython: spawn a thread that watches a `multiprocessing.Value`, and have the worker
    check it at loop back-edges. Measure the polling overhead at several check
    frequencies, and compare against the countdown-plus-deopt design in `RESUME_CHECK`.
    Why does CPython rate-limit the poll instead of checking every time?

14. **Verify the browser's asymmetry.** In a cross-origin-isolated page, call
    `Atomics.wait` on the main thread and in a Worker on the same `SharedArrayBuffer`.
    Confirm the main thread throws and the Worker blocks. Then remove the COOP/COEP
    headers and confirm `SharedArrayBuffer` disappears entirely — the §18.4 point that
    your concurrency model is set by HTTP headers.

---

## 24. Question bank

Staff-level questions this document should let you answer cold.

**Taxonomy**
1. Define data race and race condition. Give a program with one and not the other, in both directions.
2. Why can pure Python code not have a data race? Name three exceptions.
3. What is the practical consequence of Python having no memory model? What may you rely on anyway?

**The interpreter**
4. Is `x += 1` atomic in Python? Give the complete answer — including which versions, which builds, and why.
5. Name the three families of bytecode instruction at which a GIL-build CPython thread can be preempted. Roughly how many instructions is that?
6. Why does inserting a function call between a read and a write turn a safe increment into a 73%-loss race?
7. Which CPython change made this class of race unobservable, what was it actually trying to fix, and in which version did it land?
8. `list.append` is atomic. Why? Is it atomic on the free-threaded build? Why?

**Liveness**
9. State the four Coffman conditions. Which one do you break in practice, and why that one?
10. Distinguish deadlock, livelock, and starvation by their production signatures — CPU, progress, and what `py-spy` shows.
11. `threading.Lock` starved 5 of 8 threads on a GIL build and was perfectly fair on the free-threaded build, using the same `PyMutex` code. Explain.
12. What is `TIME_TO_BE_FAIR_NS`, what problem does it solve, and why did it not help in the case above?
13. Why does making a critical section *shorter* sometimes reduce throughput?

**Scheduling**
14. Give the strongest correctness argument for asyncio over threads, and the strongest liveness argument against it. Support both with numbers.
15. Why is turning a sync function async a breaking change even if every caller is updated?
16. Why is classic priority inversion hard to produce in pure Python? Name two ways it can still reach you.

**Practice**
17. Design a lock-ordering scheme for a system with per-account locks and a global audit-log lock. How do you enforce it in CI?
18. You inherit a service that deadlocks once a week in production and never in staging. What do you add to the process, and what do you look at first?
19. Your team wants to adopt free-threading. What do you do first, and what do you expect to happen to your test suite?
20. Why is `sys.setswitchinterval(1e-6)` a useful test fixture, and what class of bug will it never find?
21. When would you reach for TLA+ instead of more tests?

**Progress guarantees**
22. Lock-free is achievable with a CAS retry loop. Why is wait-freedom not? What mechanism closes the gap, and what does it cost?
23. Michael–Scott's queue is lock-free; Kogan–Petrank's is wait-free. What did they add, and what does every operation now pay?
24. What is a consensus number? Give the numbers for atomic registers, `fetch_add`, and CAS. Why does the last one mean hardware vendors stopped adding primitives?
25. Classify the three paths of the free-threaded `Py_INCREF` by progress guarantee. Why is there no retry loop, and what would one have cost?
26. The same `fetch_add` is wait-free on an Apple M3 and only lock-free on a 2015 ARM server. Explain, and say what that implies about reasoning from source code.
27. Can you write a wait-free algorithm in pure Python? Justify your answer, and say what you would do instead if you needed a hard latency bound.

**WebAssembly**
28. Why does `_CHECK_PERIODIC` contain a call to an Emscripten signal check? What does that macro expand to on Linux?
29. `Atomics.wait` is forbidden on the browser main thread. Name three consequences for anyone porting a threaded library to WASM.
30. Why does Pyodide ship a build of Python with no pthreads? Give both reasons from PEP 776.
31. How can two byte-identical deployments of the same Python program have different concurrency models? (§18.4)
32. Which of `threading`, `multiprocessing`, and `asyncio` work in the browser, and why? What concurrency model does that leave you with, and which bugs in this document does it eliminate outright?

---

## 25. Sources

**CPython source (3.14 branch, read this session)**
- [`Python/bytecodes.c`](https://github.com/python/cpython/blob/3.14/Python/bytecodes.c) — `_CHECK_PERIODIC`, `_CHECK_PERIODIC_IF_NOT_YIELD_FROM`, `RESUME`, `JUMP_BACKWARD`. *Verdict: the authoritative answer to "where can a thread switch?". Parse it yourself rather than trusting any blog, including this one.*
- [`Python/lock.c`](https://github.com/python/cpython/blob/3.14/Python/lock.c) — `TIME_TO_BE_FAIR_NS`, `MAX_SPIN_COUNT`, `mutex_unpark`. *Verdict: 638 lines and worth reading end to end; it is the clearest lock implementation in the tree.*
- [`Modules/_threadmodule.c`](https://github.com/python/cpython/blob/3.14/Modules/_threadmodule.c) — proves `threading.Lock` is `PyMutex`.
- `Include/internal/pycore_lock.h` — `PyMutex` layout and the `_Py_LOCKED`/`_Py_HAS_PARKED` bits.

**The change that made this document necessary**
- [PR #18334 — "bpo-29988: Only check evalbreaker after calls and on backwards egdes"](https://github.com/python/cpython/pull/18334), Mark Shannon, merged 2021-03-24, commit `4958f5d`, released in 3.10. *Verdict: read the description. Three sentences that changed the observable concurrency semantics of the language, in service of an unrelated bug.*
- [bpo-29988](https://bugs.python.org/issue29988) — the original `with`-statement/`Ctrl-C` issue.

**Wait-freedom** (§16)
- Maurice Herlihy, *Wait-Free Synchronization*, ACM TOPLAS 13(1), January 1991 — consensus numbers, the impossibility results, and the universal construction. *Verdict: one of the few genuinely essential papers in the field. Read §§1–4 even if you skip the proofs; the consensus hierarchy is the reason your hardware has CAS and not something else.*
- Alex Kogan & Erez Petrank, *Wait-Free Queues With Multiple Enqueuers and Dequeuers*, PPoPP 2011 — the age-based helping scheme built on Michael–Scott. *Verdict: the best worked example of what wait-freedom costs in practice. Read it directly after doc 03 §6.*
- Kogan & Petrank, *A Methodology for Creating Fast Wait-Free Data Structures*, PPoPP 2012 — the fast-path/slow-path technique that makes helping affordable.
- [`Include/refcount.h`](https://github.com/python/cpython/blob/3.14/Include/refcount.h) — the three-path `Py_INCREF` of §16.4. *Verdict: read the free-threaded branch and note the complete absence of a retry loop. That is PEP 703's central trick in twelve lines.*

**WebAssembly** (§18)
- [PEP 776 — Emscripten Support](https://peps.python.org/pep-0776/), Hood Chatham, Informational/Active, created 2025-03-18, Python-Version 3.14. *Verdict: the authoritative statement of what does and does not work, including the no-pthreads decision. Short and worth reading end to end.*
- `Include/internal/pycore_emscripten_signal.h` (3.14) — 30 lines; the whole signal-polling interface.
- [Explainer: Allowing `Atomics.wait` on the main thread](https://github.com/WebAssembly/shared-everything-threads/blob/main/proposals/shared-everything-threads/WaitOnMainThread.md) — the WebAssembly CG's own account of the restriction and the pressure to relax it. *Verdict: read this before assuming the §18.3 rule is permanent.*
- [MDN — `Atomics.wait`](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Atomics/wait) and [`Atomics.waitAsync`](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Atomics/waitAsync) — the normative behaviour and the non-blocking alternative.
- [Pyodide — Interrupting execution](https://pyodide.org/en/stable/usage/keyboard-interrupts.html) — `setInterruptBuffer` in practice, the browser end of §18.1.
- [web.dev — Using WebAssembly threads from C, C++ and Rust](https://web.dev/articles/webassembly-threads) — Workers, `SharedArrayBuffer`, and the cross-origin-isolation requirement.

**Background reading** (see [BOOKS.md](BOOKS.md) for verdicts and sequencing)
- Herlihy & Shavit, *The Art of Multiprocessor Programming*, 2nd ed. — progress guarantees (§16), the formal treatment of everything in §8.
- Anderson & Dahlin, *Operating Systems: Principles and Practice* — Coffman conditions, scheduling.
- Kleppmann, *Designing Data-Intensive Applications*, ch. 8 — the clock material of §14.1 at distributed-systems scale.
- Butenhof, *Programming with POSIX Threads* — still the best treatment of lock ordering and priority inversion.

**Tools** (versions resolved against PyPI on 2026-08-02)
- [`pytest-run-parallel`](https://pypi.org/project/pytest-run-parallel/) 0.9.1 — runs each test in N threads. *Verdict: the right tool for a free-threading audit.*
- [`py-spy`](https://pypi.org/project/py-spy/) 0.4.2 — attach to a wedged process without cooperation. *Verdict: install it on every production host before you need it.*
- [`pytest-timeout`](https://pypi.org/project/pytest-timeout/) 2.4.0 — non-optional for concurrent test suites.
- [`hypothesis`](https://pypi.org/project/hypothesis/) 6.165.0 — stateful testing; see [`43-testing-strategy.md`](43-testing-strategy.md).
- `faulthandler` (stdlib) — §20.4. *Verdict: zero-dependency, works on a hung process, criminally underused.*
- CPython's `Tools/tsan/suppressions_free_threading.txt` — a map of the runtime's known benign races.

**Sibling docs**
- [`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md) §14, §16 — SC-DRF, and Python's absent memory model.
- [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md) §1, §8 — progress guarantees; lock-free measured slower than a lock.
- [`24-the-gil.md`](24-the-gil.md) §4, §7 — the eval loop's release points and the GIL convoy.
- [`26-free-threading.md`](26-free-threading.md) §5, §6, §7 — race amplification, the C-extension hazard, the sharing wall.
- [`28-asyncio-internals.md`](28-asyncio-internals.md) §13, §17 — cancellation, slow-callback detection.
- [`31-measurement-methodology.md`](31-measurement-methodology.md) — read before believing §9 or §13, including my numbers.

---

*Next: [`31-measurement-methodology.md`](31-measurement-methodology.md) — how to measure
any of this without fooling yourself, which §9's noise correction should have already
convinced you is the hard part.*
