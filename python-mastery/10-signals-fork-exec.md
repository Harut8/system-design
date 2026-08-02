# 10 — Signals, fork, and exec: three ways a process changes shape

> **Tier 1, doc 10.** Prerequisites: [`06-processes-threads-scheduling.md`](06-processes-threads-scheduling.md)
> (process vs thread, context switches), [`07-virtual-memory.md`](07-virtual-memory.md)
> (copy-on-write, page tables). Reads well next to
> [`24-the-gil.md`](24-the-gil.md) §4 (the eval breaker) and §8 (signals/fork), because
> Python's signal design *is* an eval-breaker design. Feeds into:
> [`25-threads-and-synchronization.md`](25-threads-and-synchronization.md),
> [`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md),
> [`28-asyncio-internals.md`](28-asyncio-internals.md) §self-pipe,
> [`46-production-python.md`](46-production-python.md) (graceful shutdown).
>
> **THESIS: signals, `fork()`, and `exec()` are the three places where the kernel
> reaches into a running process and changes it out from under the code that is
> executing. Two of the three are reentrancy disasters and one is clean — and the one
> that is clean is clean *precisely because it destroys the address space.*** A signal
> handler reenters your program between two arbitrary machine instructions. `fork()`
> reenters your address space with every thread but one deleted, and every lock those
> threads held frozen in whatever state it was in. `exec()` is safe because it keeps
> almost nothing. Every Python-level rule in this document — handlers deferred to the
> eval loop, handlers restricted to the main thread, file descriptors non-inheritable by
> default, `multiprocessing` moved off `fork` in 3.14 — is the same move made four times:
> **convert an asynchronous reentrancy problem into a synchronous one, or refuse to play.**

> **Measurement provenance.** Numbers labelled *(measured)* were produced on the machine
> this repo lives on: **Apple M3 Pro, macOS 25.5 (Darwin 25.5.0), arm64, 128-byte cache
> lines, 16 KB pages, 11 cores (5 P + 6 E)**, using **CPython 3.14.6**
> (`~/.local/bin/python3.14`) and the **3.14.6 free-threading build**
> (`~/.local/bin/python3.14t`). C source is quoted from the **`3.14` branch of
> github.com/python/cpython** as of Aug 2026 — downloaded and read, with line numbers,
> not recalled. **This is a macOS box**, which matters more in this document than in any
> other in the folder: `pidfd`, `signalfd`, and `/proc` do not exist here, and macOS has
> its own fork hazard (§8.3) that Linux does not. Linux-only facts are cited to
> `man7.org` and flagged **not measured here** in place.

## Contents

1. [The one problem all three share](#1-the-one-problem-all-three-share)
2. [Signal delivery: what the kernel does](#2-signal-delivery-what-the-kernel-does)
3. [What Python does instead: the deferred handler](#3-what-python-does-instead-the-deferred-handler)
4. [The deferral you can measure: which C calls are signal-blind](#4-the-deferral-you-can-measure-which-c-calls-are-signal-blind)
5. [Async-signal-safety: the real rule, and Python's sidestep](#5-async-signal-safety-the-real-rule-and-pythons-sidestep)
6. [PEP 475: the EINTR retry loop you no longer write](#6-pep-475-the-eintr-retry-loop-you-no-longer-write)
7. [Signals and threads](#7-signals-and-threads)
8. [`fork()`: what the child gets](#8-fork-what-the-child-gets)
9. [The classic deadlock, reproduced](#9-the-classic-deadlock-reproduced)
10. [What Python did about it: the 3.12 warning and the 3.14 default](#10-what-python-did-about-it-the-312-warning-and-the-314-default)
11. [`exec()`: the clean one](#11-exec-the-clean-one)
12. [File descriptors across fork and exec](#12-file-descriptors-across-fork-and-exec)
13. [Zombies, orphans, and reaping](#13-zombies-orphans-and-reaping)
14. [Process groups, sessions, and who gets your Ctrl-C](#14-process-groups-sessions-and-who-gets-your-ctrl-c)
15. [Graceful shutdown, assembled](#15-graceful-shutdown-assembled)
16. [House rules](#16-house-rules)
17. [You can answer this](#17-you-can-answer-this)
18. [Sources](#18-sources)

---

## 1. The one problem all three share

Write the three primitives as transformations on a process and the symmetry is immediate:

| Primitive | Address space | Threads | File descriptors | Control flow |
|---|---|---|---|---|
| **signal** | unchanged | one thread diverted | unchanged | **arbitrary instruction becomes a call site** |
| **`fork()`** | **duplicated (COW)** | **all but the caller deleted** | duplicated | continues in two processes |
| **`exec()`** | **destroyed and replaced** | all but the caller deleted | **kept** (unless `CLOEXEC`) | restarts at a new entry point |

The reentrancy hazard is in the bolded cells.

**A signal makes every instruction boundary a potential function call.** Your handler can
begin executing while the interrupted code is halfway through updating a data structure.
If the handler touches that structure, it observes a torn invariant. This is not a race
in the concurrency sense — there is one thread — it is *reentrancy*, and it needs no
second core to bite you.

**`fork()` makes every lock held by another thread permanently unavailable.** The child
gets a byte-identical copy of the parent's memory, which includes the parent's mutexes in
whatever state they were in at the instant of the call. It does *not* get the other
threads. A mutex whose owner does not exist in the child is a mutex that will never be
unlocked. §9 measures this: **26 of 40 forked children wedged forever on a lock nobody
held** *(measured)*.

**`exec()` is safe because it keeps almost nothing.** No heap, no locks, no half-built
objects. This is why the POSIX rule for a forked child in a threaded program is not "be
careful" but "call only async-signal-safe functions until you `exec`" — the child is
expected to be a launcher, not a program. `fork()` + `exec()` is safe; `fork()` alone,
in a threaded process, is a gamble whose odds §9 puts a number on.

Everything below is a consequence of these three rows.

---

## 2. Signal delivery: what the kernel does

Before Python, the mechanism. A signal has three states:

1. **Generated** — something (`kill(2)`, the terminal driver, the kernel itself for
   `SIGSEGV`/`SIGPIPE`, a timer for `SIGALRM`) marks the signal pending on the target
   task.
2. **Pending** — recorded as a bit in the target's pending set. *Standard signals do not
   queue*: if `SIGTERM` is generated three times before delivery, the target sees one
   `SIGTERM`. (Real-time signals, `SIGRTMIN`..`SIGRTMAX`, do queue. Python exposes them
   as plain integers and gives you no queueing help.)
3. **Delivered** — on the next transition from kernel mode back to user mode, the kernel
   checks the pending set against the blocked set (the *signal mask*) and, if a signal is
   deliverable, arranges for the handler to run.

That third step is the interesting one. The kernel does not "call" your handler the way a
library call works. It **rewrites the user-space stack** so that returning to user mode
lands in the handler, with a synthetic frame that returns to a trampoline which invokes
`sigreturn(2)` to restore the original context. During the handler the signal being
handled is added to the mask (so a second one does not nest), plus whatever
`sa_mask` requested.

Three consequences that people routinely get wrong:

- **Delivery is not instantaneous.** It happens at the next kernel→user transition of a
  thread that has the signal unblocked. A thread spinning in a tight compute loop with no
  syscalls still gets there — the timer interrupt forces the transition — but a thread
  blocked in an uninterruptible kernel state does not.
- **A blocking syscall in progress is aborted, not resumed** — unless the handler was
  installed with `SA_RESTART`, in which case the kernel restarts a subset of syscalls.
  Without it the syscall returns `-1` with `errno == EINTR`. This is the entire subject
  of §6.
- **Delivery targets a *thread*, not a process.** For a process-directed signal the
  kernel picks any thread that does not have it blocked. This is the root of §7.

CPython installs its handlers through `PyOS_setsig()`, and the flags it chooses are the
whole story:

```c
/* Python/pylifecycle.c
 *
 * All of the code in this function must only use async-signal-safe functions,
 * listed at `man 7 signal-safety` [...]
 */
PyOS_sighandler_t
PyOS_setsig(int sig, PyOS_sighandler_t handler)
{
#ifdef HAVE_SIGACTION
    struct sigaction context, ocontext;
    context.sa_handler = handler;
    sigemptyset(&context.sa_mask);
    /* Using SA_ONSTACK is friendlier to other C/C++/Golang-VM code that
     * extension module or embedding code may use where tiny thread stacks
     * are used.  https://bugs.python.org/issue43390 */
    context.sa_flags = SA_ONSTACK;
    if (sigaction(sig, &context, &ocontext) == -1)
        return SIG_ERR;
    return ocontext.sa_handler;
#else
    PyOS_sighandler_t oldhandler;
    oldhandler = signal(sig, handler);
#ifdef HAVE_SIGINTERRUPT
    siginterrupt(sig, 1);
#endif
    return oldhandler;
#endif
}
```

`sa_flags = SA_ONSTACK` — and **conspicuously not `SA_RESTART`**. In the fallback path,
`siginterrupt(sig, 1)` explicitly *disables* restarting. CPython deliberately wants its
syscalls to fail with `EINTR` so that it can regain control, run the Python-level
handler, and decide for itself whether to retry. That decision is PEP 475 and it is the
hinge the rest of this design hangs from.

---

## 3. What Python does instead: the deferred handler

Here is the C function that actually runs when a signal arrives in a CPython process.
Read it and notice what is *not* there:

```c
/* Modules/signalmodule.c:349 (3.14) */
static void
signal_handler(int sig_num)
{
    int save_errno = errno;

    trip_signal(sig_num);

#ifndef HAVE_SIGACTION
#ifdef SIGCHLD
    /* To avoid infinite recursion, this signal remains
       reset until explicit re-instated. [...] */
    if (sig_num != SIGCHLD)
#endif
    /* If the handler was not set up with sigaction, reinstall it. [...] */
    PyOS_setsig(sig_num, signal_handler);
#endif

    /* Issue #10311: asynchronously executing signal handlers should not
       mutate errno under the feet of unsuspecting C code. */
    errno = save_errno;
    /* ... Windows SetEvent for SIGINT ... */
}
```

**No Python code runs here.** No `PyObject` is touched, no allocation happens, the GIL is
not acquired. The handler saves `errno`, calls `trip_signal()`, restores `errno`, and
returns. The `errno` save/restore is the tell: this function knows it is running at an
arbitrary point inside unsuspecting C code and refuses to disturb anything.

### 3.1 `trip_signal`: three stores and a write

```c
/* Modules/signalmodule.c:274 (3.14) */
static void
trip_signal(int sig_num)
{
    _Py_atomic_store_int(&Handlers[sig_num].tripped, 1);

    /* Set is_tripped after setting .tripped, as it gets
       cleared in PyErr_CheckSignals() before .tripped. */
    _Py_atomic_store_int(&is_tripped, 1);

    _PyEval_SignalReceived();

    /* And then write to the wakeup fd *after* setting all the globals and
       doing the _PyEval_SignalReceived. We used to write to the wakeup fd
       and then set the flag, but this allowed the following sequence of events
       (especially on windows, where trip_signal may run in a new thread):

       - main thread blocks on select([wakeup.fd], ...)
       - signal arrives
       - trip_signal writes to the wakeup fd
       - the main thread wakes up
       - the main thread checks the signal flags, sees that they're unset
       - the main thread empties the wakeup fd
       - the main thread goes back to sleep
       - trip_signal sets the flags to request the Python-level signal handler
         be run
       - the main thread doesn't notice, because it's asleep

       See bpo-30038 for more details.
    */
    int fd = wakeup.fd;
    if (fd != INVALID_FD) { /* ... write one byte == sig_num ... */ }
}
```

Three things, in a mandated order:

1. `Handlers[sig_num].tripped = 1` — a per-signal flag, one slot per signal number.
2. `is_tripped = 1` — a global "some signal arrived" flag, checked first on the fast path
   so that the common case costs one relaxed load.
3. `_PyEval_SignalReceived()` — poke the interpreter.
4. Optionally, one byte down the wakeup fd (§7.3).

That embedded comment is worth reading twice. It documents a **real store-ordering bug**
(bpo-30038) in exactly the shape [`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md)
teaches: the flag write and the wakeup write are two independent stores, and a reader can
observe them in the wrong order and go back to sleep forever. The fix was not a lock, it
was *store order* — set the flags first, wake second, so the waiter that is woken always
finds the flags already set. This is a message-passing (MP) litmus test in production
code.

### 3.2 The handoff: one bit in the eval breaker

```c
/* Python/ceval_gil.c:663 (3.14) */
void
_PyEval_SignalReceived(void)
{
    _Py_set_eval_breaker_bit(_PyRuntime.main_tstate, _PY_SIGNALS_PENDING_BIT);
}
```

One bit, set on **`_PyRuntime.main_tstate`** — the main thread's state, explicitly, no
matter which thread the C handler happened to run on. The eval breaker is the same
mechanism the GIL drop request uses; see [`24-the-gil.md`](24-the-gil.md) §4. The
interpreter checks it at the ~22 instruction categories that consult the breaker (calls,
loop back-edges, `RESUME`) — the same 22 enumerated in
[`30-concurrency-correctness.md`](30-concurrency-correctness.md) §4. When the bit is set:

```c
/* Python/ceval_gil.c:824 (3.14) */
static int
handle_signals(PyThreadState *tstate)
{
    assert(_PyThreadState_CheckConsistency(tstate));
    _Py_unset_eval_breaker_bit(tstate, _PY_SIGNALS_PENDING_BIT);
    if (!_Py_ThreadCanHandleSignals(tstate->interp)) {
        return 0;
    }
    if (_PyErr_CheckSignalsTstate(tstate) < 0) {
        /* On failure, re-schedule a call to handle_signals(). */
        _Py_set_eval_breaker_bit(tstate, _PY_SIGNALS_PENDING_BIT);
        return -1;
    }
    return 0;
}
```

and `_PyErr_CheckSignalsTstate` is where your Python function is finally called:

```c
/* Modules/signalmodule.c:1801 (3.14), abridged */
int
_PyErr_CheckSignalsTstate(PyThreadState *tstate)
{
    _Py_CHECK_EMSCRIPTEN_SIGNALS();
    if (!_Py_atomic_load_int(&is_tripped)) {
        return 0;                       /* fast path: one atomic load */
    }
    /*
     * The is_tripped variable is meant to speed up the calls to
     * PyErr_CheckSignals [...] This variable is set to 1 when a signal arrives
     * and it is set to 0 here, when we know some signals arrived. This way
     * we can run the registered handlers with no signals blocked.
     *
     * NOTE: with this approach we can have a situation where is_tripped is
     *       1 but we have no more signals to handle [...] This won't do us any
     *       harm (except we're gonna spent some cycles for nothing).
     */
    _Py_atomic_store_int(&is_tripped, 0);

    for (int i = 1; i < Py_NSIG; i++) {
        if (!_Py_atomic_load_int_relaxed(&Handlers[i].tripped)) continue;
        _Py_atomic_store_int_relaxed(&Handlers[i].tripped, 0);
        PyObject *func = get_handler(i);
        /* [...] bpo-43406: the handler may have been replaced by another
           thread since the signal arrived; if it's now SIG_DFL/SIG_IGN we
           must NOT raise() — PyErr_SetInterrupt() only *simulates* a signal
           and must never kill the process. Write an unraisable instead. */
        result = _PyObject_Call(tstate, func, arglist, NULL);
        if (!result) {
            /* On error, re-schedule a call to _PyErr_CheckSignalsTstate() */
            _Py_atomic_store_int(&is_tripped, 1);
            return -1;
        }
    }
    return 0;
}
```

So the full path of a Ctrl-C is:

```
terminal driver sends SIGINT to the foreground process group
        ↓ kernel marks pending, delivers at next kernel→user transition
C signal_handler runs  →  trip_signal()  →  three atomic stores + eval-breaker bit
        ↓ ... an unbounded amount of time passes ...
eval loop reaches an instruction that checks the breaker
        ↓
handle_signals() → _PyErr_CheckSignalsTstate() → calls your Python handler
        ↓
default SIGINT handler raises KeyboardInterrupt at the current bytecode
```

**The gap marked "an unbounded amount of time" is the entire practical content of this
design**, and §4 measures it.

### 3.3 Main thread of the main interpreter — enforced twice

```c
/* Include/internal/pycore_pystate.h:83 (3.14) */
/* Only handle signals on the main thread of the main interpreter. */
static inline int
_Py_ThreadCanHandleSignals(PyInterpreterState *interp)
{
    return (_Py_IsMainThread() && _Py_IsMainInterpreter(interp));
}
```

The same predicate gates two different things:

- **Running** a handler — `handle_signals()` returns immediately for any other thread.
- **Installing** a handler — `signal.signal()` fails outright:

```c
/* Modules/signalmodule.c:~506 (3.14) */
    if (!_Py_ThreadCanHandleSignals(tstate->interp)) {
        _PyErr_SetString(tstate, PyExc_ValueError,
                         "signal only works in main thread "
                         "of the main interpreter");
        return NULL;
    }
```

Confirmed *(measured)* — calling `signal.signal()` from a worker thread on 3.14.6:

```
ValueError: signal only works in main thread of the main interpreter
```

This is also why signals are useless as a subinterpreter notification channel: a
subinterpreter is not the main interpreter, so it can neither install nor run handlers.
See [`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md).

---

## 4. The deferral you can measure: which C calls are signal-blind

The interpreter only checks the eval breaker *between* bytecode instructions. A single
bytecode that spends two seconds inside C code is two seconds during which no Python
handler can run — unless that C code checks for itself.

The experiment: install a `SIGALRM` handler that records `time.perf_counter()`, arm an
interval timer for 50 ms, then start a long C-level call. If the handler timestamp lands
~50 ms after arming, the call was interruptible. If it lands at the *end* of the call,
the call was signal-blind for its whole duration.

*(measured, CPython 3.14.6, M3 Pro)*

| C-level call | call duration | handler ran late by | verdict |
|---|---:|---:|---|
| pure-Python `while` loop | — | **3.9 ms** | interruptible (back-edge checks the breaker) |
| `math.factorial(400_000)` | 1.48 s | **2.7 ms** | **interruptible** |
| `str(7**2_000_000)` | 0.31 s | **5.0 ms** | **interruptible** |
| `re.match(r'(a+)+$', 'a'*29+'b')` | **14.91 s** | **3.3 ms** | **interruptible** |
| `time.sleep(2)` | 2.01 s | 4.6 ms | interruptible (EINTR + PEP 475) |
| `sorted(4_000_000 floats)` | 0.58 s | **506 ms** | **signal-blind** |
| `zlib.compress(12 MB, level 9)` | 0.17 s | **121 ms** | **signal-blind** |

The pattern is not "C is uninterruptible". It is: **a C loop is interruptible exactly
when someone put a `PyErr_CheckSignals()` in it.** Grepping the 3.14 sources for
`CheckSignals` call sites tells you precisely who bothered:

| File | `CheckSignals` sites | What it buys |
|---|---:|---|
| `Objects/longobject.c` | 1 macro, 4 uses (L2113, L3304, L3857, L3909) | big-int arithmetic and int↔str conversion |
| `Modules/_sre/sre_lib.h` | 1 macro | regex backtracking |
| `Python/bltinmodule.c` | 1 | `input()` |
| `Objects/listobject.c` | **0** | — |
| `Modules/zlibmodule.c` | **0** | — |
| `Objects/bytesobject.c`, `unicodeobject.c`, `dictobject.c`, `Modules/mathmodule.c` | **0** | — |

The two macros that saved you 14.9 seconds of un-Ctrl-C-able regex:

```c
/* Objects/longobject.c:114 */
#define SIGCHECK(PyTryBlock)                    \
    do {                                        \
        if (PyErr_CheckSignals()) PyTryBlock    \
    } while(0)
```

```c
/* Modules/_sre/sre_lib.h:550 */
#define _MAYBE_CHECK_SIGNALS                                       \
    do {                                                           \
        if ((0 == (++sigcount & 0xfff)) && PyErr_CheckSignals()) { \
            RETURN_ERROR(SRE_ERROR_INTERRUPTED);                   \
        }                                                          \
    } while (0)
```

`0xfff` — the regex engine checks every 4096 backtracking steps. That is why a
catastrophic-backtracking regex, the single most notorious way to hang a Python service,
is nonetheless **Ctrl-C-able**, and why `sorted()` on a big list is not. Note also that
`math.factorial` has *no* signal check of its own; it is interruptible only because it is
built out of `longobject.c` multiplications that do.

`mathmodule.c` having zero sites is the sharper lesson: **interruptibility is not a
property of "the C layer", it is a property of each individual loop, decided by whoever
wrote it.** There is no systematic rule and no way to tell from the Python side except by
measuring.

### 4.1 What this means operationally

- **"Ctrl-C doesn't work" is usually not a bug in your signal handling.** It usually
  means the main thread is inside one long C call with no `PyErr_CheckSignals()`. Look
  for `sort`, compression, `pickle`, NumPy/BLAS kernels, a database driver's C
  extension, or a native library holding the GIL.
- **A second Ctrl-C often does work** — because the *default* `SIGINT` disposition is
  restored in some paths, or because the call finished. Do not read that as "the first
  one was lost".
- **A C extension that releases the GIL for 10 seconds is 10 seconds of no signal
  handling on the main thread**, even though other Python threads run fine. The eval
  breaker bit is set on `main_tstate` and only the main thread will act on it. See
  [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md) §GIL release.
- **If you write a long-running C loop, call `PyErr_CheckSignals()` periodically** and
  bail out on a nonzero return. Every 4096 iterations is a proven-reasonable cadence.

---

## 5. Async-signal-safety: the real rule, and Python's sidestep

The POSIX rule is narrow and absolute. From `signal-safety(7)`:

> An *async-signal-safe* function is one that can be safely called from within a signal
> handler. Many functions are *not* async-signal-safe. In particular, nonreentrant
> functions are generally unsafe to call from a signal handler.
>
> The kinds of issues that render a function unsafe can be quickly understood when one
> considers the implementation of the *stdio* library, all of whose functions are not
> async-signal-safe. [...] Suppose that the main program is in the middle of a call to a
> *stdio* function such as `printf(3)` where the buffer and associated variables have
> been partially updated. If, at that moment, the program is interrupted by a signal
> handler that also calls `printf(3)`, then the second call to `printf(3)` will operate
> on inconsistent data, with unpredictable results.

The rule is not "don't do slow things". It is **"don't touch shared mutable state that
the interrupted code might be halfway through mutating."** `malloc()` is unsafe because
it has a global free list. `printf()` is unsafe because it has a global buffer.
`localtime()` is unsafe because it returns a pointer to a static. The safe list is short:
`write()`, `_exit()`, `signal()`, `sigaction()`, `kill()`, and a few dozen others, mostly
raw syscalls with no userspace state.

### 5.1 Python's move

**CPython's C handler obeys the rule strictly** (§3: atomic stores, one `write()`,
`errno` restored) **and then breaks the rule's spirit somewhere it is safe to break it.**
Your Python handler is not run from the signal context at all — it is run later, from the
eval loop, on the main thread, with the GIL held and a consistent interpreter state.

So the guarantee Python actually gives you is not async-signal-safety. It is:

> **Your handler runs between two bytecode instructions on the main thread.**

Call that *async-bytecode-safety*. It is much stronger than the C rule in one way (you
can allocate, log, take locks, raise exceptions — all forbidden in a real handler) and
strictly weaker in another (**it is not prompt**, per §4). The trade is deliberate and it
is the right one, but it leaves a residue of bugs that people misattribute.

### 5.2 The reentrancy bugs that survive

**Between bytecodes is still "in the middle" of most things you care about.** These are
real, and none of them are fixed by Python's deferral:

**(a) The acquire/try window.** The canonical one:

```python
lock.acquire()          # ← KeyboardInterrupt can be raised HERE
try:                    #   (between the CALL and the SETUP of the try block)
    ...
finally:
    lock.release()      # never runs; the lock leaks forever
```

The `with` statement closes this window because the bytecode compiler emits
`BEFORE_WITH` such that the exception table entry covering the release is already
installed when the lock is acquired. **Use `with`. It is not a style preference here, it
is a correctness fix.** (The exception-table mechanics are in
[`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md) §zero-cost
exceptions.)

**(b) `KeyboardInterrupt` inside a `finally:` block.** A `finally` handler is ordinary
bytecode. A second Ctrl-C during cleanup aborts the cleanup. Production shutdown paths
that must complete should mask `SIGINT` for their duration
(`signal.pthread_sigmask(SIG_BLOCK, {SIGINT})`) rather than hope.

**(c) Locks in Python signal handlers.** The `signal` docs say this outright:

> **Warning.** Synchronization primitives such as `threading.Lock` should not be used
> within signal handlers. Doing so can lead to unexpected deadlocks.

The handler runs *on the main thread*, so if the main thread already holds that lock, you
have self-deadlock with no second thread involved. This is exactly the reentrancy hazard
the C rule warns about, resurfacing one layer up.

**(d) Handlers that do real work.** A handler that logs, flushes, or writes to a socket
runs on the main thread and *blocks the main thread*. The idiomatic Python signal handler
sets a flag or pushes to a queue and returns. Everything else belongs in the main loop.

```python
# The only signal handler shape that is always correct
_shutdown = threading.Event()

def _on_term(signum, frame):
    _shutdown.set()          # Event.set() is safe here; it does not block

signal.signal(signal.SIGTERM, _on_term)
signal.signal(signal.SIGINT, _on_term)
```

> **`Event.set()` in a handler is fine, `Lock.acquire()` is not** — the difference is
> that `set()` never waits on a lock the main thread might already hold in a way that
> can't progress. If you want a rule that never needs thought: *set a flag, write to a
> pre-created pipe, or `raise`. Nothing else.*

---

## 6. PEP 475: the EINTR retry loop you no longer write

Before Python 3.5, this was correct code:

```python
while True:
    try:
        data = sock.recv(4096)
        break
    except InterruptedError:      # OSError with errno == EINTR
        continue
```

Every blocking call in the program needed that wrapper, because CPython installs handlers
without `SA_RESTART` (§2), so any signal — including one from an unrelated library's
`SIGALRM`, or a `SIGWINCH` from resizing a terminal — aborted the syscall.

[PEP 475](https://peps.python.org/pep-0475/) (Natali & Stinner, Python 3.5) moved the
retry into the stdlib:

> This PEP proposes to handle EINTR and retries at the lowest level, i.e. in the wrappers
> provided by the stdlib (as opposed to higher-level libraries and applications).
>
> Specifically, when a system call fails with `EINTR`, its Python wrapper must call the
> given signal handler (using `PyErr_CheckSignals()`). If the signal handler raises an
> exception, the Python wrapper bails out and fails with the exception.
>
> **If the signal handler returns successfully, the Python wrapper retries the system
> call automatically. If the system call involves a timeout parameter, the timeout is
> recomputed.**

The three-clause spec is the whole design and each clause matters:

1. **Run the Python handler first.** This is what makes Ctrl-C work: `SIGINT`'s default
   handler raises `KeyboardInterrupt`, so the retry never happens.
2. **Retry only if the handler returned normally.** An exception propagates — the syscall
   is abandoned.
3. **Recompute the timeout.** Without this, a signal storm would extend a
   `sock.settimeout(5)` indefinitely, one interruption at a time.

Clause 3 has a consequence people find surprising, so here it is *(measured, 3.14.6)*:

```
time.sleep(1.0), SIGALRM handler fires at 0.3 s   →  returned after 1.0031 s
```

**The interrupted sleep still sleeps the full second.** It does not return early, and it
does not sleep 1.3 s. The remaining 0.7 s is recomputed and re-issued. Pre-3.5, that same
program returned at 0.3 s with `InterruptedError`.

`InterruptedError` still exists, and can still be raised — from `os.read()`/`os.write()`
on a *non-blocking* fd, from third-party C extensions that do their own syscalls, and
from `signal.sigtimedwait()`. But in modern Python, code that catches `InterruptedError`
around a stdlib blocking call is almost always dead code left from a 2.7 port.

**Functions covered** include `os.read`/`write`/`open`/`wait*`, `time.sleep`,
`select`/`poll`/`epoll`/`kqueue`, socket operations (`accept`, `connect`, `recv*`,
`send*`), `signal.sigtimedwait`, `os.fsync`, `fcntl.flock`, and `threading.Lock.acquire`
with a timeout. The exceptions are `os.close()` and `os.dup2()`, which PEP 475
deliberately does *not* retry: on Linux the fd is already closed when `EINTR` is
reported, so retrying would close somebody else's fd.

---

## 7. Signals and threads

### 7.1 The kernel picks a thread; Python picks a different one

For a process-directed signal (`kill(pid, ...)`, terminal Ctrl-C), the kernel delivers to
**any thread that has the signal unblocked**. Which one is unspecified. So the C
`signal_handler` in §3 may run on *any* Python thread — or on a thread Python has never
heard of, created by a C extension or a linked Go/Rust runtime.

This is exactly why `trip_signal` calls
`_Py_set_eval_breaker_bit(_PyRuntime.main_tstate, ...)` rather than poking the current
thread: **wherever the C handler lands, the work is routed to the main thread.** The docs
state the resulting contract:

> Python signal handlers are always executed in the main Python thread of the main
> interpreter, even if the signal was received in another thread. This means that signals
> can't be used as a means of inter-thread communication. You can use the synchronization
> primitives from the `threading` module instead.
>
> Besides, only the main thread of the main interpreter is allowed to set a new signal
> handler.

### 7.2 The consequences

- **Signals are not inter-thread communication.** Use `threading.Event`, a `queue.Queue`,
  or a `Condition`.
- **A worker thread cannot install a handler** (`ValueError`, §3.3, measured).
- **If your main thread is blocked in something signal-blind (§4), no handler runs**,
  even though eleven other threads are happily running Python. This is the single most
  common "SIGTERM is ignored" incident shape: a main thread that did
  `some_worker_pool.join()` and is parked inside a C-level wait, while the pool's threads
  keep going.
- **`threading.Thread.join()` and `Lock.acquire()` on the main thread *are*
  interruptible** on POSIX — CPython passes `intr_flag` down so the wait returns on
  `EINTR` and checks signals. This is why a Ctrl-C at a `join()` works. Do not rely on
  the equivalent inside a C extension's own wait primitive.

### 7.3 The three ways to make signal handling deterministic

**(a) `signal.set_wakeup_fd(fd)` — the self-pipe trick, built in.** `trip_signal` writes
one byte (the signal number) to a pre-created fd. Your event loop selects on that fd
alongside its sockets, so a signal reliably wakes the loop, and the byte tells you which
signal. Because the write happens in `trip_signal` — in the *real* signal handler — the
wakeup is prompt even when the interpreter is stuck (§4).

```python
r, w = socket.socketpair()
w.setblocking(False)
signal.set_wakeup_fd(w.fileno(), warn_on_full_buffer=False)
# now `r` becomes readable on every signal; select() on it in your loop
```

Note the buffer-full handling in `trip_signal`: if the write fails, CPython schedules a
warning through `_PyEval_AddPendingCall`, with an admission in the comment —
`/* _PyEval_AddPendingCall() isn't signal-safe, but we still use it for this exceptional
case. */`. Pass `warn_on_full_buffer=False` in production; a full pipe means the loop is
already behind, and the extra warning does not help.

**This is what asyncio uses.** `loop.add_signal_handler()` (Unix only) installs a C-level
handler plus `set_wakeup_fd`, and dispatches your callback as a normal loop callback —
which is why an asyncio signal callback is *not* subject to the reentrancy rules of §5.2:
it runs from the loop, not from a handler. See
[`28-asyncio-internals.md`](28-asyncio-internals.md) §self-pipe.

**(b) `pthread_sigmask` + a dedicated signal thread.** The classic POSIX-server pattern.
Block the signals everywhere, then have one thread synchronously accept them:

```python
signals = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
signal.pthread_sigmask(signal.SIG_BLOCK, signals)   # inherited by threads created after

def signal_thread():
    while True:
        sig = signal.sigwait(signals)     # blocks; no handler, no deferral
        handle(sig)

threading.Thread(target=signal_thread, daemon=True).start()
```

`sigwait()` does not run a handler at all — it dequeues a pending signal synchronously.
No reentrancy, no eval-breaker latency, no main-thread dependence. The cost: the mask is
inherited by threads created *after* the call, so this must run before you start
anything, and it interacts badly with libraries that install their own handlers.

**(c) Linux-only: `signalfd`.** Turns signals into readable file descriptors, which
composes perfectly with `epoll`. **Python has no stdlib wrapper** — you need `ctypes` or
a third-party module. Same story for `pidfd_open(2)` and `pidfd_send_signal(2)`
(exposed as `os.pidfd_open` / `signal.pidfd_send_signal`, both Linux-only): confirmed
absent on this macOS box *(measured — `hasattr(os,'pidfd_open')` → `False`)*. If your
production target is Linux, `pidfd` is the correct way to signal a child without the
PID-reuse race; just do not write it into cross-platform code without a fallback.

---

## 8. `fork()`: what the child gets

### 8.1 The inheritance table

From `fork(2)`, the child is an exact duplicate **except**:

| Category | In the child |
|---|---|
| Memory | **copy** (copy-on-write; see [`07-virtual-memory.md`](07-virtual-memory.md)) |
| Open file descriptors | **copies, sharing the same open file descriptions** — same offset, same status flags |
| PID / PPID | new PID; PPID = parent's PID |
| **Threads** | **only the calling thread exists** |
| Mutexes, condvars, other pthreads objects | **replicated in whatever state they were in** |
| Pending signals | **cleared** (empty set) |
| Signal dispositions & mask | inherited |
| Timers (`setitimer`, POSIX timers) | **not inherited** |
| Memory locks (`mlock`) | not inherited |
| `getrusage`/`times` counters | reset to zero |
| Semaphore adjustments, record locks | not inherited |

Two rows carry all the weight, and the man page states the consequence explicitly:

> The child process is created with a single thread—the one that called `fork()`. The
> entire virtual address space of the parent is replicated in the child, **including the
> states of mutexes, condition variables, and other pthreads objects**; the use of
> `pthread_atfork(3)` may be helpful for dealing with problems that this can cause.
>
> **After a `fork()` in a multithreaded program, the child can safely call only
> async-signal-safe functions (see `signal-safety(7)`) until such time as it calls
> `execve(2)`.**

Read that last sentence as the load-bearing one. The POSIX-blessed use of `fork()` in a
threaded program is: **fork, then immediately exec.** Anything else is outside the
standard, and §9 shows what "outside the standard" costs in practice.

Note also the *pending signals cleared* row: a `SIGTERM` that arrived but had not yet been
handled in the parent does **not** exist in the child. And the *timers not inherited* row:
a child that inherited your `setitimer`-based watchdog does not, in fact, have one.

### 8.2 What CPython does around the call

CPython wraps every fork in three functions, all in `Modules/posixmodule.c`:

```c
/* Modules/posixmodule.c:663 (3.14) */
void
PyOS_BeforeFork(void)
{
    PyInterpreterState *interp = _PyInterpreterState_GET();
    run_at_forkers(interp->before_forkers, 1);

    _PyImport_AcquireLock(interp);
    _PyEval_StopTheWorldAll(&_PyRuntime);
    HEAD_LOCK(&_PyRuntime);
}

void
PyOS_AfterFork_Parent(void)
{
    HEAD_UNLOCK(&_PyRuntime);
    _PyEval_StartTheWorldAll(&_PyRuntime);

    PyInterpreterState *interp = _PyInterpreterState_GET();
    _PyImport_ReleaseLock(interp);
    run_at_forkers(interp->after_forkers_parent, 0);
}

void
PyOS_AfterFork_Child(void)
{
    /* re-creates runtime->interpreters.mutex (HEAD_UNLOCK) */
    status = _PyRuntimeState_ReInitThreads(runtime);
    /* ... */
#ifdef Py_GIL_DISABLED
    _Py_brc_after_fork(tstate->interp);      /* biased refcounting queues */
    _Py_qsbr_after_fork((_PyThreadStateImpl *)tstate);
#endif
    _PyInterpreterState_ReinitRunningMain(tstate);
    status = _PyEval_ReInitThreads(tstate);
    /* ... reset remote-debug state, asyncio task lists, etc. ... */
}
```

Notice what CPython protects: **its own** locks. The import lock is taken before the fork
and released after, in both processes, because a child that inherits a held import lock
can never import again. `_PyRuntimeState_ReInitThreads` recreates the runtime mutexes.
On free-threaded builds the biased-refcount queues and the QSBR state are reinitialised
(see [`26-free-threading.md`](26-free-threading.md) and
[`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md) §QSBR).

`os.register_at_fork(before=..., after_in_parent=..., after_in_child=...)` hooks the same
lists — `before` handlers run in **reverse** registration order (`run_at_forkers(lst, 1)`),
the two `after` lists in forward order. That is the standard `pthread_atfork` convention:
acquire in reverse, release in order.

**CPython does not, and cannot, fix your locks.** A `threading.Lock` you created is a
`PyMutex` in your heap. CPython has no registry of them and no way to know which were
held. That is §9.

### 8.3 macOS makes it worse

Since macOS 10.13, the Objective-C runtime actively **aborts** a process that forked and
then touches Objective-C without `exec`ing, with the famously shouty message:

```
objc[NNNN]: +[NSObject initialize] may have been in progress in another thread when fork() was called.
objc[NNNN]: +[__NSCFConstantString initialize] may have been in progress in another thread
            when fork() was called. We cannot safely call it or ignore it in the fork()
            child process. Crashing instead.
```

or the `__THE_PROCESS_HAS_FORKED_AND_YOU_CANNOT_USE_THIS_COREFOUNDATION_FUNCTIONALITY___YOU_MUST_EXEC__`
symbol in a crash trace. Any Python process that has imported something touching
CoreFoundation — TLS via Security.framework, `urllib` with system certs, anything
matplotlib-adjacent, anything Metal-adjacent — is a candidate. The
`OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` environment variable that circulates on Stack
Overflow **turns off the detector, not the bug**; it converts a deterministic crash into
an intermittent one. This is why `multiprocessing` has defaulted to `spawn` on macOS
since Python 3.8 — and the CPython source says so in as many words
(`Lib/multiprocessing/context.py`):

```python
    # bpo-33725: running arbitrary code after fork() is no longer reliable
    # on macOS since macOS 10.14 (Mojave). Use spawn by default instead.
    # gh-84559: We changed everyones default to a thread safeish one in 3.14.
    if reduction.HAVE_SEND_HANDLE and sys.platform != 'darwin':
        _default_context = DefaultContext(_concrete_contexts['forkserver'])
    else:
        _default_context = DefaultContext(_concrete_contexts['spawn'])
```

---

## 9. The classic deadlock, reproduced

The textbook description of this bug is "a thread might hold a lock when you fork". The
useful question is *how often*, and the answer is much worse than folklore suggests.

The setup is deliberately ordinary — a background thread that holds a lock across an I/O
wait, which is what every connection pool, logger, and metrics client in your dependency
tree does:

```python
import os, threading, time, signal

lock = threading.Lock()

def hog():
    while True:
        with lock:
            time.sleep(0.002)      # GIL released here, so fork can land inside
        time.sleep(0.002)

threading.Thread(target=hog, daemon=True).start()
time.sleep(0.05)

bad, N = 0, 40
for _ in range(N):
    pid = os.fork()
    if pid == 0:
        signal.alarm(2)            # SIGALRM's default action kills us even if wedged
        with lock:                 # the child is single-threaded. Nobody holds this lock.
            os._exit(0)            # ...and yet:
    _, st = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(st) != 0:
        bad += 1
    time.sleep(0.001)
print(f"wedged children: {bad}/{N}")
```

*(measured, CPython 3.14.6, M3 Pro)*

| Build | Children permanently wedged |
|---|---:|
| 3.14.6, GIL build | **24/40, 26/40** (two runs) |
| 3.14.6, free-threaded | **22/40** |

**Roughly 60% of forks produce a child that hangs forever on a lock that, in the child,
nobody holds.** The child is single-threaded. There is exactly one thread and it is
waiting for a mutex whose owner was never copied into this process. Only `SIGALRM` — a
signal whose *default* action is to kill, so it needs no interpreter cooperation — gets
the process out.

Three things to take from this:

**(a) The rate is not "rare".** The window is `time.sleep(0.002)` out of a 4 ms cycle —
about 50% duty. Real code has smaller windows, but real code also has *many more locks*:
the allocator's, `logging`'s module-level lock, the import lock, every connection pool,
every `functools.lru_cache` on a free-threaded build. A production process forks into a
lock-holding window far more often than the "unlucky timing" framing suggests.

**(b) Free-threading does not save you** — 22/40 *(measured)*. `PyOS_BeforeFork` calls
`_PyEval_StopTheWorldAll()`, which sounds like it should help: all threads are paused
before the fork. But stop-the-world pauses threads *at safepoints*, and a thread parked in
`time.sleep()` while holding a Python-level lock is at a perfectly good safepoint. It is
stopped, it is not holding the GIL, and it is still holding your lock. **Stopping the
world freezes the threads; it does not release what they own.** On the GIL build the same
thing happens for the same reason: the forking thread holds the GIL, which tells you only
that no other thread is executing bytecode — not that no other thread holds a mutex.

**(c) `signal.alarm` was the only way out.** If the child had needed a Python-level
handler to escape, it could not have run one — the main thread was blocked in a lock
acquire and, even when that acquire is interruptible, the handler would have needed to
do something. `SIGKILL`/`SIGALRM` default actions bypass the interpreter entirely. When
you write a watchdog for a forked child, **do not route it through a Python handler.**

> **The reproduction is a Tier-9 capstone in the manifest** ("Reproduce, then fix, a
> `fork()`-in-threaded-process deadlock", docs 06/10/27). The fix, for the record, is not
> `register_at_fork` heroics — it is `forkserver` or `spawn`, §10.

---

## 10. What Python did about it: the 3.12 warning and the 3.14 default

CPython's response arrived in two stages, both driven by
[gh-84559](https://github.com/python/cpython/issues/84559) ("multiprocessing's default
posix start method of `'fork'` is broken").

### 10.1 Python 3.12: tell the user

`os.fork()`, `os.forkpty()` and `multiprocessing`'s fork path now count threads and warn:

```c
/* Modules/posixmodule.c:8020 (3.14) */
// This MUST only be called from the parent process after
// PyOS_AfterFork_Parent().
static void
warn_about_fork_with_threads(const char* name, const Py_ssize_t num_os_threads)
{
    // It's not safe to issue the warning while the world is stopped, because
    // other threads might be holding locks that we need, which would deadlock.
    assert(!_PyRuntime.stoptheworld.world_stopped);
    /* ... count via threading._active + threading._limbo ... */
    if (num_python_threads > 1) {
        PyErr_WarnFormat(
                PyExc_DeprecationWarning, 1,
                "This process (pid=%d) is multi-threaded, "
                "use of %s() may lead to deadlocks in the child.",
                getpid(), name);
        PyErr_Clear();
    }
}
```

Seen live *(measured, 3.14.6, `-W always`)*:

```
<string>:12: DeprecationWarning: This process (pid=11077) is multi-threaded,
             use of fork() may lead to deadlocks in the child.
```

Two details worth noticing. The comment on the `assert` is the same lesson as §9(b) in
miniature: **CPython cannot even emit a warning while the world is stopped, because
warning machinery takes locks.** And the count is best-effort — it reads
`threading._active` and `threading._limbo` without holding
`threading._active_limbo_lock`, so a thread created by a C extension that never
registered with `threading` is invisible. **Absence of the warning is not evidence of
safety.**

### 10.2 Python 3.14: change the default

From the 3.14 release notes:

> On Unix platforms other than macOS, **'forkserver' is now the default start method**
> (replacing 'fork'). This change does not affect Windows or macOS, where 'spawn' remains
> the default start method.
>
> If the threading incompatible *fork* method is required, you must explicitly request it
> via a context from `get_context()` (preferred) or change the default via
> `set_start_method()`. (Contributed by Gregory P. Smith in gh-84559.)

And the `multiprocessing` docs on `fork`:

> **Changed in version 3.14:** This is no longer the default start method on any platform.

*(measured, this box)*:

```
sys.platform = darwin | default start method = spawn
available: ['spawn', 'fork', 'forkserver']
```

### 10.3 The three start methods, compared

| | `fork` | `forkserver` | `spawn` |
|---|---|---|---|
| Mechanism | `fork()` the live process | fork a **clean, single-threaded server** at first use; it forks the workers | `exec()` a brand-new interpreter |
| Startup cost | lowest | low (server pays interpreter startup once) | highest (full interpreter + re-import per worker) |
| Inherits globals/state | **everything** | only what the server had at its creation | nothing; args are pickled |
| Inherits fds | all inheritable ones | few | few |
| Thread-safe | **no** | **yes** | **yes** |
| COW memory sharing | yes (until refcounts touch the pages) | partial | none |
| Default in 3.14 | never | Unix ≠ macOS | Windows, macOS |

**`forkserver` is the interesting compromise** and it is worth understanding *why* it
works. At first use, `multiprocessing` forks once — from a process state chosen to be as
simple as possible — into a dedicated server. That server is single-threaded by
construction, so *it* can fork safely forever after. Your main process may grow to fifty
threads; the forkserver does not care, because it is not the one forking.

The migration cost is real, and it is the cost of losing implicit inheritance:

- Anything the workers relied on being present in globals must now be picklable and
  passed, or established in an initializer.
- Module-level side effects re-run (`forkserver` re-imports the module that defines the
  target).
- A `if __name__ == "__main__":` guard becomes mandatory, exactly as it always was on
  Windows.
- Objects that cannot be pickled — open sockets, live DB connections, file handles,
  `logging` handlers with sockets — must be created inside the worker.

If you truly need `fork` (a large read-only dataset you want to COW-share, and no
threads), ask for it explicitly and pair it with `gc.freeze()` before forking to stop the
cycle collector from writing to every page. That interaction is measured in
[`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md) (the fork tax) and
[`22-garbage-collection.md`](22-garbage-collection.md) §`gc.freeze`.

---

## 11. `exec()`: the clean one

`execve(2)` replaces the process image. Same PID, same PPID, new everything else. It is
the only one of the three primitives with no reentrancy hazard, because there is nothing
left to reenter.

### 11.1 What survives

| Survives `exec()` | Does not survive |
|---|---|
| PID, PPID, PGID, SID | the entire address space (heap, stack, globals) |
| open file descriptors **without `FD_CLOEXEC`** | fds **with** `FD_CLOEXEC` |
| **signal mask** (blocked set) | **handled** dispositions → reset to `SIG_DFL` |
| **`SIG_IGN`** dispositions | installed handler functions (there is no code to run) |
| current working directory, umask, root dir | threads other than the caller |
| uid/gid (unless setuid), resource limits | timers (`alarm` cancelled), pending signals kept |
| controlling terminal, process group membership | memory locks, mappings |

The signal row is the one that bites, and it is worth seeing rather than trusting. Set one
signal to a handler and another to `SIG_IGN`, then exec a fresh interpreter and ask it
*(measured, 3.14.6)*:

```
SIGUSR1 (was handled)  in child -> SIG_DFL
SIGUSR2 (was SIG_IGN)  in child -> SIG_IGN
```

**Ignored stays ignored; handled becomes default.** The asymmetry is logical — a handler
is a function pointer into an address space that no longer exists, whereas `SIG_IGN` is
just a policy — but it is a classic source of production mysteries:

- A parent that did `signal.signal(SIGPIPE, SIG_IGN)` (extremely common in networking
  code) hands every child an ignored `SIGPIPE`. Shell scripts and CLI tools that expect
  to die quietly on a broken pipe instead see `write()` fail with `EPIPE` and print
  errors, or loop.
- Same for `SIGCHLD` set to `SIG_IGN`, which additionally changes reaping semantics
  (§13).
- **The signal mask survives too.** A process that blocked `SIGTERM` and forgot to unblock
  before exec produces a child that cannot be terminated normally.

`subprocess` handles the common cases for you: `restore_signals=True` (the default)
resets `SIGPIPE`, `SIGXFZ` and `SIGXFSZ` to `SIG_DFL` in the child before exec. It does
**not** restore an arbitrary disposition you changed, and it does not reset the mask.
If you change dispositions in a server, reset them explicitly in the child.

### 11.2 `subprocess` does not always fork

Modern `subprocess` prefers `posix_spawn()` when it can prove errors are reported
correctly, because `posix_spawn` can use `vfork()` and skip the page-table duplication
entirely. From `Lib/subprocess.py`:

```python
    Prefer an implementation which can use vfork() in some cases for best
    performance.
    """
    if _mswindows or not hasattr(os, 'posix_spawn'):
        return False
    if ((_env := os.environ.get('_PYTHON_SUBPROCESS_USE_POSIX_SPAWN')) in ('0', '1')):
        return bool(int(_env))
    if sys.platform in ('darwin', 'sunos5'):
        # posix_spawn() is a syscall on both macOS and Solaris,
        # and properly reports errors
        return True
    # ...
        if sys.platform == 'linux' and libc == 'glibc' and version >= (2, 24):
            # glibc 2.24 has a new Linux posix_spawn implementation using vfork
            # which properly reports errors to the parent process.
            return True
    # By default, assume that posix_spawn() does not properly report errors.
    return False
```

This matters for two reasons. First, **`subprocess.run()` in a threaded process is safe**
in a way `os.fork()` is not — it forks and immediately execs, which is exactly the POSIX
blessing from §8.1, and on macOS/modern-glibc it may not even fork. Second, **it explains
why `preexec_fn` is documented as unsafe**: `preexec_fn` runs arbitrary Python between
fork and exec — that is, in the one window POSIX says must contain only async-signal-safe
calls — and it disables the `posix_spawn` fast path. Use the dedicated parameters
instead:

| Instead of `preexec_fn=` | Use |
|---|---|
| `lambda: os.setsid()` | `start_new_session=True` |
| `lambda: os.setpgid(0, 0)` | `process_group=0` (3.11+) |
| `lambda: os.setuid(n)` / `setgid` | `user=`, `group=`, `extra_groups=` (3.9+) |
| `lambda: os.umask(m)` | `umask=` (3.9+) |
| closing fds | `close_fds=True` (default), `pass_fds=` |

*(Verified present on 3.14.6: `preexec_fn`, `close_fds`, `restore_signals`,
`start_new_session`, `pass_fds`, `user`, `group`, `process_group`.)*

---

## 12. File descriptors across fork and exec

### 12.1 What the child shares

`fork()` gives the child *copies of the fd numbers* pointing at *the same open file
descriptions*. Same offset, same status flags. Two consequences:

- **Two processes writing to the same inherited fd interleave at the same offset.** This
  is why forked workers appending to one logfile produce interleaved-but-not-corrupted
  lines for small writes (a single `write()` under `PIPE_BUF` is atomic) and torn lines
  for big ones.
- **An fd is only really closed when the last copy closes.** A child holding an inherited
  socket keeps the connection open after the parent closes it. The classic symptom: a
  server restarts, fails to bind, and `lsof` shows the port held by a long-dead worker's
  grandchild.

### 12.2 PEP 446 made fds non-inheritable by default

Before Python 3.4, every fd Python created was inheritable, so every `exec` leaked
whatever happened to be open. [PEP 446](https://peps.python.org/pep-0446/) flipped the
default and, importantly, made the creation **atomic** where the OS allows it:

> In a multi-threaded application, an inheritable file descriptor may be created just
> before a new program is spawned, before the file descriptor is made non-inheritable. In
> this case, the file descriptor is leaked to the child process. This race condition could
> be avoided if the file descriptor is created directly non-inheritable.

That is why the implementation uses `O_CLOEXEC`, `SOCK_CLOEXEC`, `F_DUPFD_CLOEXEC` and
friends rather than an `open()` followed by an `fcntl()` — the two-step version has a
window in which another thread can `exec`.

*(measured, 3.14.6)*:

```
open()           inheritable=False
socket()         inheritable=False
os.pipe() read   inheritable=False
stdin            inheritable=True
stdout           inheritable=True
```

Standard streams stay inheritable, because that is what "standard" means. Everything else
you create is closed on exec unless you say otherwise via `os.set_inheritable(fd, True)`
or `subprocess(pass_fds=...)`.

**`FD_CLOEXEC` is an exec property, not a fork property.** A forked child that never
execs inherits *everything*, `CLOEXEC` or not. This is another reason `spawn`/`forkserver`
are cleaner than `fork`: they exec, so the flag actually does its job.

---

## 13. Zombies, orphans, and reaping

A terminated process is not gone. The kernel keeps its exit status until the parent
collects it. Until then it is a **zombie**: no memory, no threads, just a PID and a
status word.

*(measured, 3.14.6, macOS)* — fork a child that exits immediately, don't wait, then ask
`ps`:

```
  PID STAT COMM
14368 Z    <defunct>
```

`waitpid()` collects it and the PID disappears:

```
waitstatus_to_exitcode: 3
after wait, ps knows it?: gone
```

The failure mode is **PID exhaustion**: a long-lived parent that forks and never waits
accumulates zombies until the process table fills, at which point every `fork()` in the
system fails with `EAGAIN`. A zombie costs almost no memory, which is exactly why it goes
unnoticed until it is a full outage.

The rules:

- **`subprocess.Popen` reaps for you** — but only when you call `wait()`, `poll()`,
  `communicate()`, or let the object be garbage-collected (which triggers a warning and a
  best-effort reap). `Popen` objects you keep in a list and never touch are zombie
  factories.
- **`os.waitpid(-1, os.WNOHANG)`** in a loop is the manual reaper. Loop until it raises
  `ChildProcessError` or returns `(0, 0)`.
- **`signal.SIGCHLD` set to `SIG_IGN`** tells the kernel not to create zombies at all
  — but it also makes `wait()` fail, so you cannot then collect exit statuses. It is a
  reasonable choice for fire-and-forget children and a terrible one if you care whether
  they succeeded. (`SIGCHLD`'s *default* disposition is already "ignore" in the sense of
  taking no action — *(measured: `signal.getsignal(SIGCHLD)` → `0`, i.e. `SIG_DFL`)* —
  but that default still creates zombies. Explicitly setting `SIG_IGN` is what changes
  the reaping semantics. They are not the same thing.)
- **Reaping from a `SIGCHLD` handler must loop.** Standard signals do not queue (§2), so
  three children exiting simultaneously may produce **one** `SIGCHLD`. A handler that
  reaps exactly one child leaks the other two. Always drain:

```python
def _reap(signum, frame):
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        _record_exit(pid, os.waitstatus_to_exitcode(status))
```

- **Orphans** are the mirror image: the parent dies first, and the child is re-parented
  to `init`/`launchd` (PID 1), which reaps it. Orphans are not a leak — but they *are* a
  supervision failure: your worker is now running with no supervisor, and nothing will
  restart or stop it. In containers, PID 1 is often your app rather than a real init, and
  **an app that does not reap is how a container accumulates zombies**. Use `--init`, or
  `tini`, or reap explicitly.

`os.waitstatus_to_exitcode(status)` (3.9+) is the right way to interpret the status word:
it returns the exit code for a normal exit and the **negated signal number** for a killed
process — `-9` for `SIGKILL`, `-15` for `SIGTERM`. This is the same convention
`subprocess.returncode` uses, and it is how you distinguish "the OOM killer got it"
(`-9`) from "it exited with an error" (`1`). See
[`07-virtual-memory.md`](07-virtual-memory.md) for the OOM-killer side of that story.

---

## 14. Process groups, sessions, and who gets your Ctrl-C

Three nested identifiers, and almost every "the child ignored my SIGTERM" incident is a
misunderstanding of them.

```
session (SID)  ─ one controlling terminal, one session leader
   └── process group (PGID) ─ the unit of job control; signal target for the terminal
          └── process (PID)
```

*(measured, this shell)*: `os.getpid()=14360`, `os.getpgrp()=14354`, `os.getsid(0)=14354`
— the process is in a group led by the shell's job, in a session led by the same.

**Ctrl-C sends `SIGINT` to every process in the terminal's foreground process group**,
not just to the process you launched. Ctrl-\ sends `SIGQUIT`, Ctrl-Z sends `SIGTSTP`, the
same way. This is why:

- A Python script that spawns children with `subprocess` and then catches
  `KeyboardInterrupt` to "clean up" often finds the children **already dead** — they got
  the same Ctrl-C, in parallel, before the parent's handler ran.
- Conversely, a child started with `start_new_session=True` is in a *different* session,
  so it gets **no** Ctrl-C. Handy for daemons; a trap when you assumed the terminal would
  clean up.
- `SIGHUP` is delivered to the foreground group when the terminal disappears. `nohup` and
  `setsid` exist to escape that.

The controls Python gives you:

| Goal | Mechanism |
|---|---|
| Child in its own **session** (fully detached: no controlling tty, no Ctrl-C) | `subprocess.Popen(..., start_new_session=True)` (= `setsid()`) |
| Child in its own **process group**, same session | `Popen(..., process_group=0)` (3.11+) |
| Signal a whole group | `os.killpg(pgid, sig)`, or `os.kill(-pgid, sig)` |
| Find a child's group | `os.getpgid(pid)` |

**The "kill the whole tree" recipe.** A child that spawns grandchildren cannot be cleaned
up by killing the child — the grandchildren are re-parented and keep running. Put the
child in its own process group at creation and signal the group:

```python
p = subprocess.Popen(cmd, start_new_session=True)   # new session ⇒ new process group
try:
    p.wait(timeout=30)
except subprocess.TimeoutExpired:
    os.killpg(os.getpgid(p.pid), signal.SIGTERM)    # the whole tree
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        p.wait()
```

Two caveats. `start_new_session=True` means the child no longer receives Ctrl-C from your
terminal, which is usually what you want for a managed subprocess but is surprising
interactively. And there is a **PID-reuse race** between reading `getpgid(p.pid)` and
`killpg` — negligible in practice on a machine with a large PID space, and eliminable on
Linux with `pidfd` (§7.3), which does not exist on macOS.

---

## 15. Graceful shutdown, assembled

Everything in this document converges on one production shape. A container orchestrator
sends `SIGTERM`, waits (Kubernetes: `terminationGracePeriodSeconds`, default 30 s), then
sends `SIGKILL`. You cannot handle `SIGKILL`. So the whole game is: **notice `SIGTERM`
promptly, stop accepting work, finish what's in flight, exit before the deadline.**

```python
import os, signal, socket, sys, threading

_shutdown = threading.Event()

def _on_signal(signum, frame):
    # Rule from §5.2: set a flag, nothing else.
    _shutdown.set()

def main():
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # §7.3(a): guarantee the loop wakes even if it is parked in select().
    r, w = socket.socketpair()
    w.setblocking(False)
    signal.set_wakeup_fd(w.fileno(), warn_on_full_buffer=False)

    while not _shutdown.is_set():
        serve_one_batch()            # must return periodically; see §4

    stop_accepting_new_work()
    # §5.2(b): don't let a second Ctrl-C abort the cleanup.
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    drain_in_flight(deadline_seconds=20)
    reap_children()                  # §13
    sys.exit(0)
```

The failure modes this shape is defending against, each traced to its section:

| Symptom | Cause | Section |
|---|---|---|
| `SIGTERM` "ignored", process `SIGKILL`ed at the deadline | main thread inside a signal-blind C call | §4 |
| Handler never runs though the app is clearly alive | main thread blocked; workers running | §7.2 |
| Handler runs but the process still hangs | handler did real work / took a lock | §5.2 |
| Shutdown aborted halfway | second `SIGINT` during cleanup | §5.2(b) |
| Child processes survive the parent | not in the parent's process group | §14 |
| Port still bound after restart | fd inherited by a surviving descendant | §12.1 |
| Zombies accumulate in the container | PID 1 does not reap | §13 |
| Worker hangs forever right after startup | forked from a threaded parent | §9 |

**Under asyncio, use `loop.add_signal_handler()` instead of `signal.signal()`** — it
routes through `set_wakeup_fd` and dispatches your callback as a normal loop callback, so
none of §5.2's reentrancy rules apply and the shutdown can be `async`. It is Unix-only.
See [`29-async-patterns-and-pitfalls.md`](29-async-patterns-and-pitfalls.md) §graceful
shutdown for the task-cancellation half of the problem.

---

## 16. House rules

**Signals**

1. **A signal handler sets a flag and returns.** Nothing else. Not logging, not locks,
   not I/O.
2. Use `with`, never bare `acquire()`/`try:` — it closes the interrupt window (§5.2a).
3. If you need prompt, deterministic signal handling, use `set_wakeup_fd` (event loops) or
   `pthread_sigmask` + `sigwait` in a dedicated thread (threaded servers). Do not rely on
   handler latency.
4. Mask `SIGINT`/`SIGTERM` around cleanup that must complete.
5. In a long C loop, call `PyErr_CheckSignals()` every few thousand iterations.
6. Never use signals to talk between Python threads. Use `threading` primitives.

**fork**

7. **Do not call `os.fork()` in a process that has threads** — including threads you did
   not start (a logging handler's, a gRPC channel's, an SDK's telemetry uploader's).
8. If you must fork, `exec` immediately, or use `subprocess`, which does it for you.
9. Take the 3.14 default. `forkserver` on Linux, `spawn` on macOS/Windows. Reach for
   `fork` only with a measured COW-sharing reason, and pair it with `gc.freeze()`.
10. The absence of a `DeprecationWarning` proves nothing (§10.1).
11. `register_at_fork` is for *your* module's state. It cannot make someone else's
    library fork-safe.

**exec / processes**

12. Assume `SIG_IGN` and the signal mask leak into every child. Reset them explicitly if
    the child's behaviour depends on them.
13. Never `preexec_fn`. Use the dedicated `subprocess` parameters.
14. Put managed subprocesses in their own process group and kill the group, not the PID.
15. Reap in a loop, always; `SIGCHLD` does not queue.
16. Interpret exit status with `os.waitstatus_to_exitcode()` — `-9` and `1` mean very
    different things.

---

## 17. You can answer this

- Walk a Ctrl-C from the terminal driver to `KeyboardInterrupt`. Name every intermediate
  step and say where an unbounded delay can be introduced.
- Why can't a worker thread call `signal.signal()`? Name the C predicate that stops it and
  the *two* different things it gates.
- Your service ignores `SIGTERM` roughly one deploy in twenty and gets `SIGKILL`ed.
  Give three distinct explanations and the diagnostic that separates them.
- `sorted()` is not interruptible but a catastrophically backtracking regex is. Explain,
  from the C sources, why — and predict which of `zlib.compress`, `str(huge_int)`,
  `math.factorial` fall on which side.
- Precisely: what does the child of `fork()` inherit, and what are the three things it
  does not? Which of the omissions causes deadlocks?
- A forked child hangs on `lock.acquire()` while being the only thread in the process.
  Explain, and say why free-threading's stop-the-world at fork does not prevent it.
- Why did `multiprocessing` change its default start method in 3.14, and what breaks in
  existing code when it does?
- What survives `exec()`: file descriptors, signal handlers, signal masks, ignored
  signals? Which asymmetry causes production bugs, and give a concrete one.
- What is a zombie, what does it cost, and what are the two ways to prevent
  accumulation? Why must a `SIGCHLD` reaper loop?
- Ctrl-C kills your subprocess before your `KeyboardInterrupt` handler runs. Why? Now
  make the subprocess *not* receive it, and say what you gave up.
- Why is `preexec_fn` unsafe, and what does using it cost you in `subprocess`
  performance?
- PEP 475: a `time.sleep(1.0)` is interrupted at 0.3 s by a handler that returns
  normally. When does it return, and what would pre-3.5 Python have done?

---

## 18. Sources

**Primary — CPython 3.14 branch** (read, with line numbers, Aug 2026):

- `Modules/signalmodule.c` — `trip_signal` (L274), `signal_handler` (L349),
  `signal.signal` main-thread check (L~506), `_PyErr_CheckSignalsTstate` (L1801)
- `Python/ceval_gil.c` — `_PyEval_SignalReceived` (L663), `handle_signals` (L824)
- `Include/internal/pycore_pystate.h` — `_Py_ThreadCanHandleSignals` (L83)
- `Python/pylifecycle.c` — `PyOS_setsig` (`SA_ONSTACK`, no `SA_RESTART`)
- `Modules/posixmodule.c` — `run_at_forkers` (L626), `PyOS_BeforeFork` (L663),
  `PyOS_AfterFork_Parent` (L674), `PyOS_AfterFork_Child` (L702),
  `warn_about_fork_with_threads` (L8020, message at L8072)
- `Objects/longobject.c` — `SIGCHECK` macro (L114), uses at L2113/3304/3857/3909
- `Modules/_sre/sre_lib.h` — `_MAYBE_CHECK_SIGNALS` (L550), the `0xfff` cadence
- `Lib/multiprocessing/context.py` — default-context selection (L332–340)
- `Lib/subprocess.py` — `_use_posix_spawn()` (L712–745)

**PEPs**

- [PEP 475 — Retry system calls failing with EINTR](https://peps.python.org/pep-0475/)
  (Natali & Stinner, 3.5)
- [PEP 446 — Make newly created file descriptors non-inheritable](https://peps.python.org/pep-0446/)
  (Stinner, 3.4)

**Issues & docs**

- [gh-84559](https://github.com/python/cpython/issues/84559) — "multiprocessing's default
  posix start method of `'fork'` is broken: change to `'forkserver' || 'spawn'`"
  (Gregory P. Smith); shipped in 3.14
- [`signal` — Set handlers for asynchronous events](https://docs.python.org/3/library/signal.html)
  — "Signals and threads"
- [`multiprocessing` — Contexts and start methods](https://docs.python.org/3/library/multiprocessing.html)
- [What's New in Python 3.14 — multiprocessing](https://docs.python.org/3/whatsnew/3.14.html)

**POSIX / kernel**

- [`fork(2)`](https://man7.org/linux/man-pages/man2/fork.2.html) — the inheritance list
  and the single-thread rule
- [`signal-safety(7)`](https://man7.org/linux/man-pages/man7/signal-safety.7.html) — the
  async-signal-safe function list and the `stdio` explanation

**macOS fork safety**

- Barry Warsaw, [*How macOS Broke Python*](https://www.wefearchange.org/2018/11/forkmacos.rst)
  — the 10.13 Objective-C fork check and its consequences for `multiprocessing`
- Ruby's parallel encounter with the same change:
  [bugs.ruby-lang.org #14009](https://bugs.ruby-lang.org/issues/14009)

**Books** (see [BOOKS.md](BOOKS.md) for verdicts)

- Kerrisk, *The Linux Programming Interface* — ch. 20–22 (signals), 24–28 (process
  creation), 34 (process groups & sessions). The reference for this entire document.
- Stevens & Rago, *Advanced Programming in the UNIX Environment* 3e — ch. 8, 10, 9.
- OSTEP ch. 5 — the cleanest short treatment of `fork`/`exec`/`wait`.

---

*Next in Tier 1: [`11-ipc-and-shared-memory.md`](11-ipc-and-shared-memory.md) — pipes,
UNIX sockets, FD passing, futexes, and what `multiprocessing.shared_memory` actually
costs.*
