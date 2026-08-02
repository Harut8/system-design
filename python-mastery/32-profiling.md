# 32 — Profiling: the instrument changes the measurement

> **Tier 5, doc 32.** Prerequisites: [`31-measurement-methodology.md`](31-measurement-methodology.md)
> (you must know your noise floor before a profile means anything),
> [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §10,
> [`20-eval-loop.md`](20-eval-loop.md). Feeds into:
> [`33-optimizing-python.md`](33-optimizing-python.md),
> [`35-memory-optimization.md`](35-memory-optimization.md),
> [`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md).
>
> **THESIS: a deterministic profiler does not measure your program — it measures your
> program plus itself, and it does not add that overhead evenly.** On this machine
> cProfile slows a call-heavy function by **5.09×** and a loop-heavy function by
> **1.00×**. Two functions whose true cost ratio is 1.67× are reported at 8.52×. The
> profiler does not merely inflate the numbers; **it reorders them**. Every "optimize
> the top of the profile" instinct is therefore a bet that your instrument didn't
> choose that top for you.

> **Measurement provenance.** All numbers *(measured)* were produced on the machine this
> repo lives on: **Apple M3 Pro, macOS, arm64, CPython 3.14.6**, 128-byte cache lines,
> 16 KB pages, 5 P-cores + 6 E-cores. Per
> [`31-measurement-methodology.md`](31-measurement-methodology.md), that heterogeneity is
> a hazard: figures below are best-of-3 to suppress cluster migration, and ratios within
> a single run are more trustworthy than absolute values across runs.

## Contents

1. [The distortion, measured](#1-the-distortion-measured)
2. [Why deterministic profilers lie in a specific direction](#2-why-deterministic-profilers-lie-in-a-specific-direction)
3. [Sampling profilers, and what they trade away](#3-sampling-profilers-and-what-they-trade-away)
4. [`sys.monitoring` vs `sys.setprofile` — measured](#4-sysmonitoring-vs-syssetprofile--measured)
5. [The tool inventory](#5-the-tool-inventory)
6. ["60% in `_PyEval_EvalFrameDefault`" and other useless answers](#6-60-in-_pyevalevalframedefault-and-other-useless-answers)
7. [Memory profiling is a different problem](#7-memory-profiling-is-a-different-problem)
8. [Profiling what isn't CPU: I/O, locks, and off-CPU time](#8-profiling-what-isnt-cpu-io-locks-and-off-cpu-time)
9. [Profiling in production](#9-profiling-in-production)
10. [The workflow that actually finds things](#10-the-workflow-that-actually-finds-things)
11. [Lab exercises](#11-lab-exercises)
12. [Question bank](#12-question-bank)
13. [Sources](#13-sources)

---

## 1. The distortion, measured

Two functions. Identical total work — increment an integer two million times. One does it
through 2,000,000 function calls; the other through 2,000 iterations of a 1,000-iteration
inner loop.

```python
def leaf(x):
    return x + 1

def many_small():                    # 2,000,000 calls
    t = 0
    for i in range(N):
        t = leaf(t)
    return t

def few_large():                     # 2,000 calls, 1000 units each
    t = 0
    for _ in range(N // 1000):
        for i in range(1000):
            t = t + 1
    return t
```

Result on this machine *(measured)*:

| | `many_small` | `few_large` | ratio |
|---|---|---|---|
| **Unprofiled (truth)** | 37.6 ms | 22.5 ms | **1.67×** |
| **Under cProfile** | 191.6 ms | 22.5 ms | **8.52×** |
| **Slowdown from profiling** | **5.09×** | **1.00×** | — |

**Distortion factor: 5.09×.**

Read the third column, not the first two. Everyone knows a profiler adds overhead; the
assumption is that it adds it *uniformly*, so the ranking survives even if the absolute
numbers don't. **It does not.** `few_large` was not slowed at all — its cost is inside a
loop the profiler never sees. `many_small` was slowed fivefold because the profiler fires
on every call and return.

If you profiled this program and optimized the top entry, you would spend your day on
`many_small` believing it was 8.5× the cost of `few_large`, when it was 1.67×.

Here is what cProfile actually reports *(measured)*:

```
         2000003 function calls in 0.211 seconds

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.124    0.124    0.189    0.189 prof_distort.py:13(many_small)
  2000000    0.064    0.000    0.064    0.000 prof_distort.py:10(leaf)
        1    0.022    0.022    0.022    0.022 prof_distort.py:20(few_large)
```

`few_large` sits at the bottom at 0.022 s — and that 0.022 s is *correct*, it matches the
unprofiled 22.5 ms almost exactly. The bug is not that `few_large` was mismeasured. It is
that **everything else was inflated around it.**

---

## 2. Why deterministic profilers lie in a specific direction

`cProfile` (the C implementation, `_lsprof`) hooks the interpreter's call and return
events. Per event it must: read a high-resolution clock, find or create the record for
this code object, update timers, and maintain a call stack.

That is a fixed cost of roughly **60–80 ns per call event** on this machine, inferred
from the 5.09× slowdown across 2M calls *(derived, not directly instrumented — see
labs)*. Compare it to the thing being measured:

```
  A trivial Python function call:        ~15-20 ns
  cProfile's bookkeeping for that call:  ~60-80 ns
                                         ────────────
  You are measuring the ruler.
```

The bias therefore has a precise shape:

| Code shape | Distortion |
|---|---|
| Many small function calls | **Massively over-reported** |
| Recursive code | Over-reported (every level is an event) |
| Tight loops, no calls | **Not distorted at all** |
| Comprehensions (3.12+ inlined) | Not distorted — no frame per iteration |
| C functions called from Python | Over-reported (call event, but no Python body) |
| Time inside one long C call (NumPy, `re`) | **Under-reported relative to everything else** |

The last row is the dangerous one in real code. If your program spends 70% of its time
inside one `numpy.dot` and 30% in Python glue with a million calls, cProfile will inflate
the glue and leave the `dot` untouched — and tell you to optimize the glue.

> **The one-line rule.** Deterministic profilers are *relatively* accurate only between
> pieces of code with **similar call granularity**. Comparing a call-heavy function to a
> loop-heavy one via cProfile is not a valid measurement, and there is no flag that fixes
> it.

`profile` (the pure-Python one) is worse by roughly an order of magnitude and exists only
for extension and portability reasons. Never use it for real work.

---

## 3. Sampling profilers, and what they trade away

A sampling profiler interrupts periodically and records the stack. Its overhead is
**proportional to sampling rate, not to program structure** — which removes the §2 bias
entirely.

```
  DETERMINISTIC                     SAMPLING
  ─────────────                     ────────
  every call/return is an event     wall-clock interrupts at fixed Hz
  cost ∝ number of calls            cost ∝ sample rate
  exact call counts                 no call counts at all
  distorts by code shape ✗          unbiased by code shape ✓
  in-process, needs code change     can attach to a running process ✓
  ~5x slowdown here (measured)      ~1-5% typical
  sees every call, however rare     misses anything rarer than the rate
```

**What you give up:** exact call counts, and any function whose total time is below the
sampling resolution. A function called twice for 50 µs will simply not appear. Sampling
answers *"where does the time go"*, never *"how many times was this called"*.

**py-spy** is the standard choice — it reads the target process's memory from outside, so
it needs no instrumentation and can attach to production.

> **macOS limitation, measured.** py-spy 0.4.1 on this machine refuses to run without
> root, even in `py-spy record -- <cmd>` mode which spawns the child itself:
>
> ```
> $ py-spy record -o out.svg -d 4 -- python3.14 spin.py
> This program requires root on OSX.
> ```
>
> This is macOS's `task_for_pid` restriction under System Integrity Protection, not a
> py-spy bug — reading another process's memory is privileged. **On Linux it works
> unprivileged for your own processes.** I did not run it under `sudo` while writing this
> document, so **every py-spy claim below is documented behaviour, not measured here.**
> Lab 3 has you run it with `sudo` and check.

---

## 4. `sys.monitoring` vs `sys.setprofile` — measured

CPython has two instrumentation APIs. The legacy `sys.setprofile`/`sys.settrace` fire a
Python callback on every event. **PEP 669's `sys.monitoring`** (3.12+) lets a tool
register per-event callbacks that the interpreter can enable *per code object* and
disable dynamically, so uninstrumented code pays nothing.

Same workload (2M calls), same event (function start), measured *(measured)*:

| Instrumentation | Time | Overhead |
|---|---|---|
| baseline | 39.8 ms | 1.00× |
| `sys.monitoring` (PEP 669) | 105.8 ms | **2.66×** |
| `sys.setprofile` | 226.2 ms | **5.68×** |

**PEP 669 is 2.1× cheaper than the legacy API for the same information.** That is the
measured justification for the new API, and it is why modern profilers, coverage tools and
debuggers are migrating to it.

The bigger win isn't in this table: `sys.monitoring` supports `DISABLE`, letting a
callback say *"never call me for this location again."* A coverage tool can mark each line
once and then run at nearly full speed — impossible with `setprofile`, where every event
costs forever. See [`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md).

---

## 5. The tool inventory

| Tool | Kind | Overhead | Attach to running? | Use it for |
|---|---|---|---|---|
| `cProfile` | deterministic | **5×+, biased** | no | call counts; small scripts; never for ranking |
| `profile` | deterministic, pure-Python | ~50× | no | nothing |
| `sys.monitoring` | event API | 2.66× *(measured)* | no | building your own tooling |
| **py-spy** | sampling | ~1–5% | **yes** | **first reach for a live process** |
| **Scalene** | sampling + memory + native split | low–moderate | no | separating Python vs native vs GPU time |
| **memray** | allocation tracking | moderate–high | partial | **memory**, allocation attribution |
| `tracemalloc` | allocation tracking (stdlib) | high | no | leak attribution when you can't add deps |
| `austin` | sampling | low | yes | alternative to py-spy |
| Instruments / `xctrace` | native sampling | low | yes | **macOS native/C-level time** |
| `perf` | native sampling | low | yes | **Linux only** — not available here |

Two of these are installed on this machine *(verified)*: `py-spy 0.4.1` and `scalene`.
`memray` and `austin` are not globally installed.

**The default workflow is py-spy first, cProfile almost never.** cProfile's legitimate
uses are narrow: you want exact call counts, or you're profiling a short deterministic
script where 5× doesn't matter and you'll interpret the output knowing §2.

---

## 6. "60% in `_PyEval_EvalFrameDefault`" and other useless answers

A native profiler on a CPython process will report most time in
`_PyEval_EvalFrameDefault`. This is the interpreter's dispatch loop
([`20-eval-loop.md`](20-eval-loop.md)) — *all* Python code runs inside it. Learning that
60% of your time is there tells you only that your program is written in Python.

This is the **abstraction-mismatch** problem: a C-level profiler sees C frames, and your
Python call stack is data structures *inside* one of them, not stack frames the profiler
understands.

Three ways out:

1. **Use a Python-aware profiler** (py-spy, Scalene) that walks CPython's frame chain and
   reconstructs Python-level stacks.
2. **Use a native profiler with a CPython unwinder.** `perf` on Linux can do this with the
   right support; note that PEP 768's work in 3.14 also improved external-debugger
   attachment.
3. **Profile at both levels and intersect.** Python-level says *which of your functions*;
   native says whether the time is interpreter dispatch, allocator, GC, or a library.

The same trap in a different costume: a profile showing all time in `dict.__getitem__`,
`list.append`, or `str.join`. Those are C builtins; the profiler attributes the *caller's*
work to them. The question is never "why is `dict.__getitem__` slow" — it isn't — but
"why am I calling it 40 million times."

---

## 7. Memory profiling is a different problem

CPU profiling asks *where does time go*. Memory profiling has **three different
questions**, and using the wrong tool for the wrong one is the most common mistake in
[`35-memory-optimization.md`](35-memory-optimization.md):

| Question | Right instrument | Wrong instrument |
|---|---|---|
| How much memory does the process use? | **RSS** (`resource.getrusage`, `ps`) | `sys.getsizeof` |
| Which code allocated it? | **memray**, `tracemalloc` | RSS |
| Why isn't it being freed? | `gc` module, object graph | allocation trackers |
| How big is this one object? | `sys.getsizeof` + deep sizer | RSS |

[`16-object-memory-layout.md`](16-object-memory-layout.md) §11 documents `getsizeof`'s
limits, and §9 of that doc records a case where using it produced a **20× wrong answer**
about `__slots__` — a mistake caught only by switching to RSS.

**`ru_maxrss` is a high-water mark, not current usage.** It never goes down. Measuring
"after" by reading it post-`del` will report the peak and tell you nothing:

```python
# WRONG — ru_maxrss never decreases, so the second reading is the first peak
base = rss(); a = [Plain() for _ in range(N)]; p = rss() - base
del a
b = [Slotted() for _ in range(N)]; s = rss() - p - base   # reports ~0
```

The fix is one process per variant. That's a real error I made while writing doc 16, and
it's in [`31-measurement-methodology.md`](31-measurement-methodology.md)'s spirit: the
instrument's semantics *are* part of the experiment design.

Also note `tracemalloc` sees only Python-level allocations through the pymalloc domains —
memory allocated by a C extension via raw `malloc` (a NumPy buffer, a compression context)
is **invisible** to it. If RSS and `tracemalloc` disagree by hundreds of megabytes, that
gap is usually native allocation, not a bug in your accounting.

---

## 8. Profiling what isn't CPU: I/O, locks, and off-CPU time

Every profiler above answers "where is the CPU busy". Most production latency problems are
about where the program is **not** busy — blocked on a socket, a lock, a disk, or the GIL.

**On-CPU vs off-CPU** is the fundamental split (Brendan Gregg's framing). A service at
5% CPU with terrible p99 has an off-CPU problem, and a CPU profile of it is nearly empty
where the answer lives.

What to reach for:

- **Blocked on the GIL**: the signature is p99 quantized near multiples of the 5 ms switch
  interval, at low CPU. See [`24-the-gil.md`](24-the-gil.md) §5 — the convoy effect. py-spy's
  `--idle` flag includes threads not currently running, which is how you see this at all.
- **Blocked in `asyncio`**: a coroutine calling something synchronous stalls the whole
  loop. `asyncio`'s debug mode logs callbacks exceeding a threshold; see
  [`29-async-patterns-and-pitfalls.md`](29-async-patterns-and-pitfalls.md).
- **Blocked on locks**: no good stdlib answer. On Linux, eBPF (`offcputime`,
  `bpftrace`) is the right tool — see [`12-observing-a-process.md`](12-observing-a-process.md).
  **Not available on macOS**; use Instruments' System Trace.
- **Blocked on I/O**: usually visible in application-level metrics long before a profiler.

---

## 9. Profiling in production

The reason py-spy matters more than cProfile: **you can point it at a process that is
already misbehaving**, without a restart, a code change, or a deploy.

```bash
py-spy dump --pid 12345          # one stack snapshot per thread, instantly
py-spy top  --pid 12345          # live top-style view
py-spy record --pid 12345 -d 60 -o prod.svg   # flamegraph over a minute
```

*(Documented usage — see §3's caveat: unverified on this machine, needs `sudo` on macOS.)*

**Continuous profiling** — sampling every process at a low rate, always, and storing the
results — is now standard practice (Parca, Pyroscope, Datadog, Cloud Profiler). It changes
the economics: instead of reproducing a problem under a profiler, you go and look at what
the process was doing when it happened. Your `../sre-observability/09-profiling.md` covers
the platform side.

**The p99 caveat from [`31-measurement-methodology.md`](31-measurement-methodology.md) §11
applies here.** A profile aggregated over a minute shows you the *mean* program. If your
problem is a p99 that happens 1% of the time, it contributes 1% of your samples and is
invisible under the bulk. Profile the slow requests specifically, or not at all.

---

## 10. The workflow that actually finds things

Ordered. Skipping steps is how people spend a week optimizing the wrong function.

1. **Establish the noise floor first.** If you can't measure a 20% change reliably
   ([`31-measurement-methodology.md`](31-measurement-methodology.md) §5), you cannot
   evaluate any fix you make. Do this before opening a profiler.
2. **Confirm it's CPU-bound at all.** Check CPU utilisation. If it's low, go to §8 —
   a CPU profile will not contain your answer.
3. **Sample first, with py-spy.** Unbiased, no code change, works on the real workload.
   Get a flamegraph. Look for *width*, not depth.
4. **Form a hypothesis and name a number.** "Serialization is >30% of request time." A
   profile without a hypothesis produces a reading exercise, not a decision.
5. **Only now consider cProfile** — and only for exact call counts, interpreting via §2.
6. **Check whether it's memory, not CPU** (§7). Allocation pressure shows up as CPU time
   spread thinly across everything, plus GC — which
   [`22-garbage-collection.md`](22-garbage-collection.md) §11 notes also costs you a cold
   cache afterwards.
7. **Fix one thing. Re-measure against the noise floor.** Not against your memory of the
   old number.
8. **Verify in production.** [`31`](31-measurement-methodology.md) §11 — local wins
   routinely fail to materialize, and the reasons are legitimate.

---

## 11. Lab exercises

Reading this leaves you at rung 3 (README §14).

**1 — Reproduce the distortion.** Rebuild §1's experiment. Confirm the profiled ratio
diverges from the true ratio on your machine, and report the distortion factor. Then vary
the granularity — 10 calls of 200,000 units, 200,000 calls of 10 units — and plot
distortion against calls-per-unit-work. *Proves §2's bias has a shape you can predict.*

**2 — Derive cProfile's per-event cost.** From the slowdown and the call count, compute
nanoseconds of overhead per call event. Compare it to the cost of an empty Python function
call measured directly. *Proves you are measuring the ruler.*

**3 — Run py-spy with `sudo` and settle §3.** Profile the same workload with py-spy and
with cProfile. Does py-spy rank the two functions correctly where cProfile did not?
*Proves sampling removes the granularity bias — the claim this doc could not verify.*

**4 — Make cProfile give catastrophically wrong advice.** Write a program where the real
hot spot is one long NumPy call and the decoy is a million trivial Python calls. Confirm
cProfile points at the decoy. Then confirm py-spy doesn't. *This is the lab that changes
how you work.*

**5 — Measure `sys.monitoring`'s `DISABLE`.** Build a toy coverage tool twice: once with
`setprofile`, once with `sys.monitoring` returning `DISABLE` after first hit. Measure both.
*Proves §4's claim that the real win isn't the 2.1×.*

**6 — Find the tracemalloc blind spot.** Allocate a large NumPy array. Compare
`tracemalloc`'s report against RSS. Explain the gap. *Proves §7's native-allocation point,
and is exactly the confusion that produces "phantom" memory in production.*

**7 — Profile something off-CPU.** Write a service that is slow because of a lock or a
blocking call inside an event loop, at low CPU. Confirm a CPU profile is nearly empty
where the problem is. Then find it with `py-spy --idle`. *Proves §8.*

**8 — The p99 invisibility test.** Build a workload where 1% of requests are 100× slower.
Profile the aggregate. Confirm the slow path is invisible. Then profile only the slow
requests. *Proves §9's caveat, which is the most common production profiling error.*

---

## 12. Question bank

1. cProfile says function A costs 8× function B. What must you know before believing it? *(§1, §2)*
2. Why does cProfile slow a call-heavy function 5× and a loop-heavy one not at all? *(§2)*
3. Under what condition are two cProfile numbers safely comparable? *(§2)*
4. Your program spends 70% of its time in one NumPy call. What will cProfile tell you to optimize, and why is it wrong? *(§2, §6)*
5. What does a sampling profiler fundamentally *not* know? *(§3)*
6. Why does py-spy need root on macOS but not on Linux? *(§3)*
7. `sys.monitoring` is 2.66× overhead vs `setprofile`'s 5.68×. Why is that not the main advantage? *(§4)*
8. A native profile shows 60% in `_PyEval_EvalFrameDefault`. What have you learned? *(§6)*
9. A profile shows most time in `dict.__getitem__`. What is the actual question to ask? *(§6)*
10. Name the four distinct memory questions and the right instrument for each. *(§7)*
11. Why does reading `ru_maxrss` after a `del` report the wrong thing? *(§7)*
12. `tracemalloc` reports 400 MB; RSS says 2 GB. Give the most likely explanation. *(§7)*
13. Your service is at 5% CPU with a terrible p99. Why is a CPU profile nearly useless? *(§8)*
14. What does p99 latency quantized near 5 ms multiples suggest? *(§8, [`24`](24-the-gil.md) §5)*
15. Why can a 60-second aggregate profile hide the exact problem you're chasing? *(§9)*

---

## 13. Sources

**Primary**
- [`cProfile` / `profile` docs](https://docs.python.org/3/library/profile.html) — read the "Limitations" section specifically; it admits the calibration problem §2 measures.
- [PEP 669 — Low Impact Monitoring for CPython](https://peps.python.org/pep-0669/) — read this; it explains the design §4 measures.
- [`sys.monitoring` docs](https://docs.python.org/3/library/sys.monitoring.html) — reference.
- [`tracemalloc` docs](https://docs.python.org/3/library/tracemalloc.html) — reference; note the domain limitation in §7.

**Tools**
- [py-spy](https://github.com/benfred/py-spy) — read the README end to end; it's short and it's the tool you'll use most.
- [memray](https://bloomberg.github.io/memray/) — the best Python memory profiler. Read the docs before doing any memory work.
- [Scalene](https://github.com/plasma-umass/scalene) — read the paper's abstract at minimum; the Python-vs-native time split is its distinguishing feature.
- [austin](https://github.com/P403n1x87/austin) — alternative sampler; reference.

**Methodology**
- Brendan Gregg, [*Systems Performance*, 2e](https://www.brendangregg.com/systems-performance-2nd-edition-book.html) — ch. 6, and his writing on [off-CPU analysis](https://www.brendangregg.com/offcpuanalysis.html) and [flamegraphs](https://www.brendangregg.com/flamegraphs.html). **Read the off-CPU material** — §8 is a summary of it.
- Denis Bakhvalov, [*Performance Analysis and Tuning on Modern CPUs*, 2e](https://easyperf.net/) — free; ch. 5–6 on native profiling.
- Mytkowicz et al., *Producing Wrong Data Without Doing Anything Obviously Wrong!* (ASPLOS 2009) — the measurement-bias paper. Cited by [`31`](31-measurement-methodology.md); it applies to profilers as much as benchmarks.

**Sibling docs**
- [`31-measurement-methodology.md`](31-measurement-methodology.md) — do not use this doc without it.
- [`20-eval-loop.md`](20-eval-loop.md) — why §6's answer is useless.
- [`16-object-memory-layout.md`](16-object-memory-layout.md) §11 — `getsizeof`'s limits.
- [`22-garbage-collection.md`](22-garbage-collection.md) §11 — GC's cache cost, invisible to CPU profiles.
- `../sre-observability/09-profiling.md` — continuous profiling as a platform.

---

*Next: [`33-optimizing-python.md`](33-optimizing-python.md) — you can now find the hot spot
and trust the finding. What you do about it is ordered by effect size, and "rewrite it in
C" is fifth on that list, not first.*
