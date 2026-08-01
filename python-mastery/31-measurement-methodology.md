# 31 — Measurement methodology: how to know whether you actually made it faster

> **Tier 5, doc 31.** Prerequisites: [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
> (cache residency, the P/E cluster split, lab 8's "prove your laptop is a bad benchmark
> host"), [`20-eval-loop.md`](20-eval-loop.md) (PEP 659 adaptive specialization — why the
> *first* iterations of any loop are systematically different from the rest),
> [`22-garbage-collection.md`](22-garbage-collection.md) (why "GC off" is a semantic change,
> not a noise reduction). Feeds into: [`32-profiling.md`](32-profiling.md),
> [`33-optimizing-python.md`](33-optimizing-python.md), [`34-going-native.md`](34-going-native.md),
> [`35-memory-optimization.md`](35-memory-optimization.md), and every
> before/after claim in [`26-free-threading.md`](26-free-threading.md).
>
> **THESIS: a bad benchmark is worse than no benchmark.** No benchmark leaves you
> uncertain, and uncertainty makes you cautious. A bad benchmark makes you *confident* —
> and then you ship the wrong thing, delete the code that was actually fast, and defend
> the decision in code review with a number. Every other document in Tier 5 produces
> numbers. This one is about whether those numbers mean anything. **The single most
> important output of this document is one figure: your machine's noise floor. Any effect
> smaller than that figure is not an effect; it is your laptop.**

> **Measurement provenance.** Every number below was produced on the machine this repo
> lives on: **Apple M3 Pro (5 P-cores + 6 E-cores), macOS 26.5.2 (build 25F84, Darwin
> 25.5.0), arm64, 18 GB unified memory**, 128-byte cache lines, 16 KB pages, using
> **CPython 3.14.6** (`~/.local/bin/python3.14`), the **3.14.6 free-threading build**
> (`python3.14t`) where marked, and **pyperf 2.10.0**. Numbers marked *(measured)* came
> out of a live process during the writing of this document.
>
> **An honest and load-bearing disclosure:** the machine was **not quiet** while this
> document was written. Another workload — Tier 0 lab binaries from a concurrent session —
> was consuming 100–800% CPU for much of the session, with `load1` oscillating between
> roughly 3 and 12. I could not evict it. Rather than pretend, I recorded `load1` alongside
> the measurements, ran a *controlled* background-load sweep (§4.3) to calibrate what that
> costs, and interleaved every A/B comparison so that drift hits both arms equally (§7.6).
> **This is the normal condition of a developer laptop, and a methodology that only works
> on a quiet machine is not a methodology.** Where a number is contaminated, it says so.

## Contents

1. [Why this document precedes 32–35](#1-why-this-document-precedes-3235)
2. [The instrument: what a clock on this machine can resolve](#2-the-instrument-what-a-clock-on-this-machine-can-resolve)
3. [The hostile laptop, measured](#3-the-hostile-laptop-measured)
4. [The noise catalogue, mechanism by mechanism](#4-the-noise-catalogue-mechanism-by-mechanism)
5. [The noise floor of this machine — the mandatory experiment](#5-the-noise-floor-of-this-machine--the-mandatory-experiment)
6. [`timeit` and its traps](#6-timeit-and-its-traps)
7. [`pyperf`, properly](#7-pyperf-properly)
8. [Statistics that matter](#8-statistics-that-matter)
9. [Microbenchmark lies](#9-microbenchmark-lies)
10. [The experiment that fooled me](#10-the-experiment-that-fooled-me)
11. [Measuring in production](#11-measuring-in-production)
12. [The decision framework](#12-the-decision-framework)
13. [House rules — the one-page checklist](#13-house-rules--the-one-page-checklist)
14. [Lab exercises](#14-lab-exercises)
15. [Question bank](#15-question-bank)
16. [Sources](#16-sources)

---

## 1. Why this document precedes 32–35

README §16 lists "optimizing before measuring, and measuring badly" as trap #4, and puts
doc 31 before docs 32–35 deliberately. Here is the argument in full, because it is not
the obvious one.

The obvious argument is *"measure first, don't guess."* Everyone agrees with that and it
changes nothing, because everyone believes they already measure. The real argument is
about **asymmetry of harm**:

| State | What you do | Cost of being wrong |
|---|---|---|
| No data | Argue from a model, stay nervous, ship carefully | Bounded. You know you don't know. |
| **Bad data** | Argue from a number, stop thinking, ship confidently | **Unbounded.** You have manufactured false certainty and attached evidence to it. |
| Good data | Argue from a number *with an interval* | The interval tells you when to stop. |

A benchmark is an **instrument**, and an uncalibrated instrument does not produce
"approximately right" answers — it produces answers whose error you cannot bound. The
worked example in §10 is mine: I ran an experiment, got a clean, monotone, plausible
**7% effect** from a variable that the program never reads, wrote down a mechanism for it,
and then discovered by randomizing the run order that the effect was **zero**. I had a
number, a story, and a mechanism, and all three were wrong. That took about four minutes
to produce and would have survived any code review.

Three concrete failure shapes this document exists to prevent, all of which are
*measured* below:

1. **You measure the machine instead of the code.** §7.6: `pyperf compare_to` on two
   **byte-identical** benchmarks reported *"1.24x faster"* on this machine *(measured)*.
   Nothing changed. The scheduler did.
2. **You measure the compiler instead of the code.** §9.2: `timeit('1+1')`,
   `timeit('2**20')`, `timeit("'a'*100")` and `timeit('None')` all return
   **2.50–2.51 ns/loop** *(measured)* — because the compiler folded all four to a single
   `LOAD_CONST`. You benchmarked `timeit`'s loop.
3. **You measure a world that doesn't exist.** §9.1: the same summation loop costs
   **10.9 ns/element** on 1,000 items and **63.4 ns/element** on 8,000,000 *(measured)* —
   a **5.8×** difference from working-set size alone. Your microbenchmark's array fits in
   L1. Production's does not. See [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §1.

Notice that all three are *systematic* errors, not random ones. Running the bad benchmark
more times makes it *more* confident and no less wrong. That is why "just run it a lot"
is not a methodology, and why this document spends as much time on experiment *design* as
on statistics.

---

## 2. The instrument: what a clock on this machine can resolve

Before noise, before statistics: **what is the smallest interval you can even see?**
If you don't know your instrument's resolution you cannot know whether "3 ns faster" is a
result or a rounding artifact.

*(measured)*, CPython 3.14.6 on this machine:

```python
>>> import time
>>> time.get_clock_info('perf_counter')
namespace(implementation='mach_absolute_time()', monotonic=True,
          adjustable=False, resolution=4.166666666666666e-08)
>>> time.get_clock_info('process_time')
namespace(implementation='clock_gettime(CLOCK_PROCESS_CPUTIME_ID)',
          monotonic=True, adjustable=False, resolution=1e-06)
```

**`perf_counter` resolution is 41.667 ns** — a 24 MHz `mach_absolute_time` timebase, not
a nanosecond clock despite the `_ns` suffix. Confirmed empirically by taking 20,000
back-to-back readings and histogramming the non-zero deltas *(measured)*:

| Observed delta (ns) | Count |
|---|---|
| 41 | 2,071 |
| **42** | **9,581** |
| 83 | 6,043 |
| 84 | 2,066 |
| 125 | 23 |
| 209 | 1 |

Every delta is a multiple of ~41.7 ns. There is no such thing as a 10 ns measurement on
this machine. Three consequences:

1. **The median cost of a back-to-back `perf_counter_ns()` pair is 42 ns** *(measured)* —
   i.e. one tick. Timing anything that takes less than ~1 µs directly means the
   *instrument* is a double-digit percentage of the *measurement*. This is precisely why
   `timeit` and `pyperf` put an inner loop between the two clock reads: you amortize the
   41.7 ns quantum over `N` iterations.
2. **`process_time` is 24× coarser (1 µs)** but measures CPU time rather than wall time,
   so it is immune to the scheduler taking your core away. `python -m timeit -p` uses it.
   It is the right instrument when you want "how much CPU did this cost" and the wrong one
   when you want "how long did the user wait".
3. **Reported resolution is a floor, not a promise.** The 209 ns outlier in that table is a
   preemption inside a two-instruction window. Even the clock has a tail.

> **Rule.** If a single timed region is shorter than ~100 × your clock resolution
> (here: ~4 µs), you must batch it in an inner loop and divide — and then you must worry
> about what the inner loop itself does to the branch predictor and the caches (§9.3).
> `pyperf`'s default calibration targets **100 ms per raw value** for exactly this reason.

---

## 3. The hostile laptop, measured

[`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §2 asserts that
this machine is "a hostile benchmarking environment" and hands the proof to this document.
Here is the proof.

### 3.1 The topology, and the one number that matters

```console
$ sysctl -n machdep.cpu.brand_string hw.ncpu hw.pagesize hw.cachelinesize
Apple M3 Pro
11
16384
128
$ sysctl -a | grep -E 'hw.perflevel[01].(name|physicalcpu|logicalcpu|l1dcachesize|l2cachesize)'
hw.perflevel0.name:            Performance
hw.perflevel0.physicalcpu:     5
hw.perflevel0.logicalcpu:      5          ← equal to physicalcpu
hw.perflevel0.l1dcachesize:    131072     ← 128 KB
hw.perflevel0.l2cachesize:     16777216   ← 16 MB, shared by 5 P-cores
hw.perflevel1.name:            Efficiency
hw.perflevel1.physicalcpu:     6
hw.perflevel1.logicalcpu:      6          ← equal to physicalcpu
hw.perflevel1.l1dcachesize:    65536      ← 64 KB
hw.perflevel1.l2cachesize:     4194304    ← 4 MB, shared by 6 E-cores
```

**`logicalcpu == physicalcpu` on both clusters: there is no SMT / hyperthreading on Apple
Silicon.** That deletes an entire class of benchmark noise that dominates x86 methodology
guides — the sibling-thread problem, where your benchmark's throughput depends on what an
unrelated process is doing on the *other* half of your physical core. On an x86 server you
must either disable SMT or pin to sibling-free cores; here you get that for free. It is
the one thing this machine makes *easier*, and it is worth saying explicitly so you don't
copy an x86 checklist item that does nothing.

Everything else about the topology makes benchmarking harder.

### 3.2 The cluster tax, measured

The benchmark used throughout this document is deliberately boring — a pure-integer loop
with no allocation, no I/O, no dict growth, so that anything that moves is the machine and
not the workload:

```python
def workload(n):
    t = 0
    for i in range(n):
        t = (t + i * 3) % 1000003
    return t
```

Run in a fresh process, 3 warmup timings discarded, then `min` of 5 timings, `n = 200_000`.
Reported as **nanoseconds per loop iteration**. Same binary, same code, 40–80 fresh
processes, only the macOS QoS clamp differs *(measured)*:

| Placement | min | median | max | min vs default |
|---|---|---|---|---|
| default (P-cores) | **24.97** | 25.39 | 26.42 | 1.00× |
| `taskpolicy -c background` (E-cores) | **79.61** | 125.02 | 203.85 | **3.19×** |

**A benchmark that migrates from the P cluster to the E cluster gets 3.2× slower.** Not
3.2% — 3.2×. That is larger than almost any optimization you will ever measure. And the
E-core run is not merely slower, it is *noisier*: coefficient of variation **28.4%** vs
**1.03%** on P-cores *(measured)*, because the six E-cores share a 4 MB L2 with each other
and with whatever else the system decided to park there.

The mechanism, stated precisely: the E-core is a narrower, lower-clocked core with **half
the L1d (64 KB vs 128 KB)** and **a quarter of the L2 (4 MB vs 16 MB)**, and that L2 is
shared six ways instead of five. So a migration changes, simultaneously, the issue width,
the clock, the cache capacity, and the cache *contention*. Nothing about your code changed.

### 3.3 What macOS lets you control — and what it does not

This is where an honest document has to disappoint you.

**macOS has no `taskset`.** There is no supported user-space API to bind a thread to a
specific CPU. The `thread_policy_set` / `THREAD_AFFINITY_POLICY` interface exists in the
Mach headers and is documented as a *hint about which threads should share a cache* — and
on Apple Silicon it is **not implemented at all**; the kernel ignores affinity tags. You
cannot do the Linux thing.

What you *can* do is set a **QoS clamp**, which biases the scheduler toward one cluster.
Measured sweep, 20 fresh processes per case *(measured — note a sustained-load test was
running concurrently, so absolute numbers are inflated; the ratios are the result)*:

| Command prefix | min | median | max | min vs default |
|---|---|---|---|---|
| *(none)* | 27.11 | 30.23 | 31.30 | 1.00× |
| `taskpolicy -c utility` | 30.73 | 61.15 | 175.60 | 1.13× |
| `taskpolicy -c background` | 95.62 | 202.80 | 565.34 | **3.53×** |
| `taskpolicy -c maintenance` | 90.32 | 134.80 | 387.77 | **3.33×** |
| `nice -n 20` | 30.28 | 31.17 | 32.18 | 1.12× |

Read the last row carefully. **`nice` is not a QoS clamp.** Renicing to +20 barely moved
the median (1.03×) — it changes priority *within* a cluster, not cluster selection.
Engineers who "isolate" a benchmark with `nice` on macOS have done approximately nothing.
`taskpolicy -c background` and `-c maintenance` genuinely do relocate you, and
`-c utility` produces a *mixed* population (min close to default, median 2× worse) —
which is the worst of both worlds for a benchmark and a useful demonstration that a
"clamp" is a scheduling hint, not a pin.

**So the honest summary of what you control on this machine:**

| Noise source | Linux | macOS / this machine |
|---|---|---|
| CPU pinning | `taskset -c 3` | ❌ **not available** |
| Core isolation from the OS | `isolcpus=`, `nohz_full=`, `rcu_nocbs=` | ❌ not available |
| Disable turbo / fix frequency | `cpupower frequency-set`, `intel_pstate/no_turbo` | ❌ not exposed |
| Force one cluster | (n/a — homogeneous) | ⚠️ QoS clamp only, and only *downward* (you can force yourself onto E-cores; you cannot force yourself onto P-cores) |
| Disable ASLR | `setarch -R`, `randomize_va_space=0` | ❌ no supported mechanism |
| Fix the hash seed | `PYTHONHASHSEED=n` | ✅ same |
| Drop the page cache | `echo 3 > /proc/sys/vm/drop_caches` | ⚠️ `purge(8)`, coarse and privileged |
| Automated tuning check | `pyperf system tune` | ❌ *(see below)* |
| PMU counters | `perf stat`, `perf record` | ⚠️ `xctrace`/Instruments only |

```console
$ python -m pyperf system
WARNING: no operation available for your platform
```

*(measured)* — **`pyperf system` is Linux-only.** Every tuning knob it manages (CPU
isolation, `intel_pstate` turbo, IRQ affinity, ASLR check, `perf_event_max_sample_rate`)
has no macOS analogue. `pyperf` will still nag you to run it in every warning message.
Ignore that specific line; the rest of its warnings are real.

**And there is no `perf(1)`.** For PMU counters — cache misses, branch mispredictions,
IPC — macOS gives you `xctrace` (the Instruments CLI) and the Instruments GUI. Both are
substantially more limited on Apple Silicon than `perf` on Linux: no arbitrary event
selection, no per-process counter multiplexing you can script cleanly, and a workflow
built around GUI traces rather than a number on stdout. [`32-profiling.md`](32-profiling.md)
covers what you can actually get. For anything that needs a hardware counter to settle an
argument, **use a Linux box.** That is not a cop-out; it is the correct engineering answer,
and it is what the CPython performance team does.

> **Carry this forward.** [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
> lab 8 asks you to "prove your laptop is a bad benchmark host" and record the spread.
> §3.2 and §5 are that number: **a 3.2× cluster tax, and a 5.8%–185% run-to-run spread
> depending on how you aggregate.** Every effect claimed in docs 32–35 must clear it.

---

## 4. The noise catalogue, mechanism by mechanism

Each subsection: the mechanism, then what it costs *here*, measured.

### 4.1 CPU migration between clusters — the dominant noise source on this machine

The mechanism is §3.2. What makes it *noise* rather than a fixed cost is that macOS's
scheduler decides placement dynamically, and its policy for a short-lived process is to
start it cheaply and promote it if it proves to be sustained CPU-hungry. A benchmark
process that lives for 50 ms may spend a meaningful fraction of that on an E-core before
anything promotes it.

You can see this directly in the shape of the distribution. Two runs of the *same*
benchmark, 80 fresh processes each, differing only in what each process reports
*(measured)*:

| Aggregation inside each process | min | median | mean | p90 | max | CV | max/min |
|---|---|---|---|---|---|---|---|
| **min of 5** timings (3 warmups) | 24.97 | 25.39 | 25.41 | 25.74 | 26.42 | **1.03%** | **1.06×** |
| **1 single** timing (3 warmups) | 25.61 | 27.59 | **33.85** | 50.48 | 73.09 | **34.05%** | **2.85×** |

That second row is the honest picture of "I timed it once." The **mean (33.85) is 23%
above the median (27.59)**, the p90 is nearly double the min, and the max is 2.85× the min.

Now compare that 2.85× tail against the E-core ratio of **3.19×** from §3.2. They are the
same number. **The slow tail of a naive single-shot benchmark on this machine is the
scheduler putting your process on an efficiency core.** That is a mechanism, not a
hand-wave, and it is the single most useful diagnostic fact in this document: if your
benchmark distribution is bimodal with a ~3× second mode, you are looking at cluster
placement.

### 4.2 DVFS and thermal throttling

Two distinct effects that get conflated:

- **DVFS (dynamic voltage & frequency scaling)** operates on millisecond timescales. The
  core ramps up when it sees sustained work. Your first timed iteration runs at a lower
  clock than your hundredth. This is *why warmups exist*, and it is a different mechanism
  from PEP 659 warmup (§4.8) even though both make early samples slow.
- **Thermal throttling** operates on tens-of-seconds-to-minutes timescales and moves the
  *sustained* clock down. A benchmark suite that takes 20 minutes measures a different
  machine at minute 18 than at minute 2.

Measured: 10 spinner processes saturating the machine, sampling the benchmark continuously
for ~4.5 minutes, bucketed by elapsed time *(measured)*:

<!--THERMAL-->

Apple Silicon laptops throttle far less aggressively than x86 laptops, so the effect here
is mild compared to what you would see on a thin-and-light x86 machine — but "mild" is
relative to a noise floor of ~1%, and any monotone drift over the life of a benchmark
suite is a **confounder with run order**, which is the exact failure mode of §10.

**The mitigation is not "wait for it to cool."** It is **interleaving**: never run all of
variant A then all of variant B. Alternate, and randomize the alternation (§7.6). Drift
then hits both arms equally and cancels in the ratio.

### 4.3 Background load — the controlled dose-response

Everyone knows background load matters. Almost nobody knows *how much*, or that `min`
does not save you from it. Controlled sweep: K spinner processes running the same integer
loop, 20 fresh benchmark processes measured per K *(measured)*:

| Spinners | `load1` | min | median | mean | p90 | max | CV |
|---|---|---|---|---|---|---|---|
| 0 | 3.04 | 23.98 | 24.44 | 24.54 | 25.20 | 25.67 | 1.8% |
| 1 | 2.95 | 24.48 | 24.84 | 24.92 | 25.29 | 25.65 | 1.2% |
| 2 | 2.88 | 25.10 | 25.51 | 25.51 | 25.76 | 26.09 | 1.0% |
| 4 | 3.13 | 25.46 | 26.02 | 26.16 | 26.79 | 27.96 | 2.3% |
| 8 | 3.52 | 26.17 | 28.19 | 27.90 | 28.62 | 28.83 | 2.9% |
| 11 | 4.76 | 26.10 | 28.39 | 28.81 | 29.26 | 40.08 | 9.6% |

Three things to extract:

1. **The median moves +16%** (24.44 → 28.39) purely from other processes existing. That is
   larger than most optimizations you will ever ship.
2. **`min` moves too — +8.9%** (23.98 → 26.10). This is the important one. The folk theorem
   is "take the minimum, it filters out interference." That is true for *transient*
   interference (one context switch during one sample) and **false for sustained
   contention**, because under sustained contention *every* sample is degraded — there is
   no clean sample to be the minimum of. `min` protects you from spikes, not from a busy
   machine.
3. **The variance signature changes shape** before the mean does. CV *drops* from 1.8% to
   1.0% at 1–2 spinners (the machine settles into a steady contended state) and then climbs
   to 9.6% at 11. Variance alone is not a reliable "am I contended?" detector.

**Practical rule:** record `os.getloadavg()[0]` with every benchmark result and refuse to
compare across runs whose load differed materially. `pyperf` does this on Linux
automatically (it records `runnable_threads` from `/proc/loadavg`); on macOS you must add
it yourself.

### 4.4 ASLR, and what it does to alignment

Address Space Layout Randomization moves the base of the executable, the shared libraries,
the heap and the stack on every execution. It is a security feature and it is on by
default; there is no supported way to disable it on macOS.

Why a benchmarker cares: **code and data alignment affects performance through the
hardware, not the language.** Specifically —

- **Instruction-cache and branch-predictor aliasing.** Branch predictor tables and the
  µop/loop buffers are indexed by bits of the instruction address. Move a hot loop by 64
  bytes and two branches that previously used different predictor entries may now collide,
  or stop colliding. This is the mechanism behind the classic "adding a `printf` I never
  call made it 8% faster."
- **Cache set index.** L1 is virtually indexed; L2 and the SLC are physically indexed. The
  set index bits above the page offset therefore change when the mapping changes, which
  redistributes your working set across sets (see the conflict-miss discussion in
  [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §4).

What ASLR actually looks like here, six consecutive fresh interpreters *(measured)*:

```console
$ for i in $(seq 6); do python3.14 -c 'import sys; x=object(); print(hex(id(x)), hex(id(sys)))'; done
0x10646c5d0 0x10648d710
0x105a705d0 0x105a91710
0x1056d45d0 0x1056f53c0 …
0x10386c5d0 0x10388d710
0x101c705d0 0x101c91710
0x103e705d0 0x103e91710
```

Look at the **low 12 bits: they never change** (`…5d0`, `…710`). Only the high bits move,
over a range of roughly 72 MB in this sample. That tells you something precise and
non-obvious: **macOS randomizes at page granularity or coarser, so intra-page alignment is
preserved run to run.** With 16 KB pages and 128-byte lines, the L1 set index (which lives
below the page offset for a virtually-indexed L1) is *stable*; the physically-indexed L2/SLC
set index is not. So on this machine ASLR perturbs L2/SLC placement and the higher branch-
predictor index bits, but not L1 line offset or intra-page layout.

**I could not isolate an ASLR-attributable effect on this machine.** To do that properly
you need to run with ASLR off and on and compare distributions, and macOS does not let you
turn it off. On Linux you would use `setarch -R` (or `pyperf`'s check that
`/proc/sys/kernel/randomize_va_space` is `2`) and could answer the question directly.
**Stated plainly: this is a noise source I can describe mechanically and cannot quantify
here.** See §10 for what happened when I tried to quantify a closely related one.

### 4.5 Environment size, link order, and measurement *bias*

This is the subject of the single most important paper in this document's bibliography:
Mytkowicz, Diwan, Hauswirth & Sweeney, **"Producing Wrong Data Without Doing Anything
Obviously Wrong!"** (ASPLOS 2009). Its finding, in one sentence: **changing the size of a
UNIX environment variable — which the program never reads — or the link order of otherwise
identical object files, shifts benchmark results by enough to reverse the conclusion of a
compiler-optimization study.** The effect they measured was comparable in size to the
`-O2` vs `-O3` difference they were trying to evaluate.

The mechanism: the environment block sits above the initial stack pointer. Grow it and
every stack frame in the process shifts by that many bytes, changing stack-object alignment
relative to cache lines and pages. Link order changes function addresses, changing
i-cache and branch-predictor index bits (§4.4).

The paper's contribution is not the specific effect — it is the concept of **measurement
bias**: an error that is *systematic* rather than random, so it does not shrink when you
take more samples, and *invisible*, because nothing about the experiment looks wrong. Their
proposed defenses are (a) **randomize the experimental setup** across many configurations,
and (b) use **causal analysis** to check that your explanation actually holds.

I tried to reproduce it here. **The result is §10, and it is the most instructive
experiment in this document, because the first version of it produced a confident, clean,
completely fake 7% effect.**

### 4.6 Page-cache state and cold start

A benchmark that touches the filesystem — imports a module, reads a fixture, opens a
sqlite file — measures a *different system* on its first run than on its tenth, because
the page cache is cold the first time. First-run import costs on this machine are
dominated by reading and unmarshalling `.pyc` files off disk; second-run costs are memory
copies.

Three rules:

- **Never compare a cold-cache run to a warm-cache run.** Warm up by running the whole
  benchmark once and discarding it, *including its imports*.
- **If you are benchmarking startup or import time, the page cache is part of what you're
  measuring** — and you must decide which case you care about. Container cold start is a
  cold-cache event. A long-lived worker's steady state is not.
- On macOS you cannot cheaply drop the page cache (`purge(8)` is coarse and privileged).
  If you need reproducible cold-cache measurements, use a Linux box with
  `echo 3 > /proc/sys/vm/drop_caches`.

### 4.7 Hyperthreading — not applicable here, and why that matters elsewhere

Covered in §3.1: `logicalcpu == physicalcpu`, so there is no SMT on this machine and no
sibling-thread interference. Recorded here explicitly because **most benchmarking
checklists you will find online are x86 checklists**, and roughly a third of their advice
("disable SMT in the BIOS", "pin to even-numbered CPUs to avoid siblings", "watch out for
the sibling running the kernel's softirq work") is *inapplicable here and essential on
your production servers*. If your laptop is Apple Silicon and your fleet is x86, your
laptop is missing a noise source that production has. That asymmetry is a reason to
distrust laptop results about threading in particular — see
[`26-free-threading.md`](26-free-threading.md).

### 4.8 Interpreter-level noise

Everything above is hardware and OS. CPython adds three of its own.

#### (a) Hash randomization — the biggest single interpreter effect I measured

Since Python 3.3, `str` and `bytes` hashes are salted with a per-process random seed
(defence against algorithmic-complexity DoS). The salt changes the **probe sequence** of
every string-keyed dict, which changes the number of collisions, which changes how many
cache lines a lookup touches.

Benchmark: build a 30,000-entry `str`-keyed dict, look up every third key, report
ns/lookup as the best of 9 in-process repetitions. 30 fresh processes per condition
*(measured)*:

| Condition | min | median | mean | max | max/min |
|---|---|---|---|---|---|
| `PYTHONHASHSEED=0` (fixed) | 51.32 | 52.48 | 52.83 | 61.25 | 1.19× |
| `PYTHONHASHSEED=random` (default) | 49.98 | 53.71 | 55.46 | 75.63 | **1.51×** |

Then, per-seed: 12 distinct fixed seeds, best-of-5 processes each *(measured)*:

```
seed= 0  51.93     seed= 4  52.59     seed= 8  55.04
seed= 1  51.78     seed= 5  52.86     seed= 9  54.47
seed= 2  58.67     seed= 6  58.19     seed=10  64.58   ← worst
seed= 3  48.55 ←   seed= 7  50.41     seed=11  52.29     best
```

**A 33% spread across seeds, on identical code.** Because that is exactly the kind of
finding that turns out to be noise, I replicated it properly: seeds 3 and 10 only,
**interleaved**, 25 fresh processes each *(measured)*:

| Seed | min | p25 | median | mean | max |
|---|---|---|---|---|---|
| 3 | 47.94 | 49.67 | 50.11 | 50.05 | **50.94** |
| 10 | **62.45** | 65.78 | 66.13 | 66.02 | 67.24 |

**The two distributions are completely disjoint** — seed 3's *worst* run (50.94) is faster
than seed 10's *best* (62.45) — and the ratio of medians is **1.32×**. This is not noise.
It is a real, reproducible 32% performance difference caused by nothing but the hash seed,
and it dwarfs almost anything you will optimize.

Victor Stinner hit exactly this while stabilizing CPython's benchmark suite, and his
conclusion is the one to adopt: **do not fix the seed.** Fixing it makes your benchmark
reproducible and *unrepresentative* — you have optimized for one arbitrary point in a
distribution your users are sampled from uniformly. Instead, **let the seed vary and
average over many processes**, which is precisely why `pyperf` spawns 20 worker processes
by default rather than doing 20 repetitions in one. Stinner's own worked example is a
cautionary tale about cherry-picking: measuring seeds 1–5 made his patch look 3% faster;
measuring only seeds 1–3 reversed the sign.

> **The general principle, and it generalizes far beyond hashing:** when a nuisance
> parameter varies in production, *sample it* — don't pin it. Pinning converts random
> error into bias.

#### (b) GC timing

`gc.get_threshold()` on this build is **`(2000, 10, 10)`** *(measured)* — note the gen-0
threshold is 2000, not the 700 that most references quote. Collections therefore fire at
allocation-count boundaries that depend on **how much garbage your setup code left behind**,
which means the *phase* of the GC relative to your timed region is essentially arbitrary
across runs. A benchmark short enough to sometimes-contain and sometimes-not-contain a
gen-2 collection is bimodal for that reason alone.

`timeit` "fixes" this by disabling the GC entirely, which is a much bigger problem than the
one it solves. §6.1 measures it: **2.47×**.

#### (c) PEP 659 specialization warmup

[`20-eval-loop.md`](20-eval-loop.md) covers the adaptive specializing interpreter. Its
consequence for measurement: **a code object's bytecode literally changes as it runs.**
Freshly compiled, then after 200 calls *(measured, `dis(..., adaptive=True)`)*:

```
cold:  RESUME        FOR_ITER        BINARY_OP              BINARY_OP           JUMP_BACKWARD
warm:  RESUME_CHECK  FOR_ITER_LIST   BINARY_OP_MULTIPLY_INT BINARY_OP_ADD_INT   JUMP_BACKWARD_NO_JIT
```

Five instructions rewrote themselves. The generic `BINARY_OP` became two type-specialized
forms that skip the whole `nb_add`/`nb_multiply` dispatch; `FOR_ITER` became a list-specific
iterator; `RESUME` became the cheap `RESUME_CHECK`.

The measured cost of *not* warming up, isolated by compiling 3,000 fresh copies of the same
function and timing call #k of each *(measured, 8-element loop, timer overhead subtracted)*:

| Call # | 0 | 1 | 2 | 3 | 4 | … | 15 |
|---|---|---|---|---|---|---|---|
| median ns/call | **250** | 167 | 208 | 167 | 208 | … | 167 |

**Call 0 is 1.20× slower than steady state.** With a longer loop (256 elements) the ratio
collapses to **1.047×**, because specialization completes *inside the first call* — the
loop body executes enough times to trip the counters before the call returns.

Two consequences:

1. **Warmups are mandatory**, and "how many" depends on your workload's shape, not on a
   magic number. A function called once with a long loop needs ~0 warmups; a function
   called 10,000 times with a 3-element loop needs several.
2. **Warming up too much is also a lie** if what you care about is a code path that runs
   cold in production — request handlers on a freshly deployed pod, a CLI that runs once.
   The steady-state number is not the number your users experience. Decide which you're
   measuring and say so.

> **A build-specific note worth checking on your own interpreter.** `pyperf metadata`
> revealed that this `python3.14` was configured with `--with-tail-call-interp`,
> `--enable-experimental-jit=yes-off`, `--enable-optimizations` (PGO), `--with-lto` and
> `--with-mimalloc` *(measured)*. Tail-call dispatch, PGO and LTO all change the eval
> loop's code layout and therefore its branch-prediction behaviour; the JIT is built but
> disabled. **Two "Python 3.14.6" interpreters can differ by more than most patches you
> will benchmark.** Always capture the build configuration with the result — this is one
> of `pyperf`'s quietly most valuable features.

---

## 5. The noise floor of this machine — the mandatory experiment

This is the experiment every reader must run on their own hardware before trusting any
number in docs 32–35.

**Protocol.** Fix the code. Run it in **N fresh processes**. Report the distribution, not a
number. Nothing varies except the machine.

80 fresh processes, `min` of 5 timings after 3 warmups, quiet-ish window *(measured)*:

```
  n = 80        min = 24.968    p25 = 25.237    median = 25.393
  mean = 25.414 p75 = 25.542    p90 = 25.740    p99 = 26.185
  max = 26.417  sd = 0.262      CV = 1.03%      max/min = 1.058  (spread 5.8%)
```

```
    ns/iter │ 80 fresh processes, min-of-5 each  (LOW-noise aggregation)
    ────────┼──────────────────────────────────────────────────────────────
     24.97  │ █████████████████                                        4
     25.03  │ █████████████████                                        4
     25.09  │ █████████████                                            3
     25.15  │ █████████                                                2
     25.21  │ ████████████████████████████████████████████████████    12   ← mode
     25.27  │ ███████████████████████████████████                      8
     25.33  │ ██████████████████████████                               6
     25.39  │ ███████████████████████████████████████                  9   ← median 25.39
     25.45  │ ███████████████████████████████████████████             10   ← mean 25.41
     25.51  │ █████████                                                2
     25.57  │ █████████████████                                        4
     25.63  │ ██████████████████████████                               6
     25.69  │ █████████████████                                        4
     25.75  │ █████████                                                2
     25.81  │ ████                                                     1
     25.87  │                                                          0
     25.93  │ ████                                                     1
     25.99  │                                                          0
     26.06  │                                                          0
     26.12  │ ████                                                     1
     26.18  │                                                          0
     26.24  │                                                          0
     26.30  │                                                          0
     26.36  │ ████                                                     1   ← max 26.42
```

Now the **same 80 processes' worth of work**, aggregated the way people actually do it —
one timing per process, no `min` filter *(measured)*:

```
  n = 80        min = 25.613    p25 = 26.605    median = 27.588
  mean = 33.848 p75 = 37.779    p90 = 50.485    p99 = 70.436
  max = 73.085  sd = 11.524     CV = 34.05%     max/min = 2.85  (spread 185%)
```

```
    ns/iter │ 80 fresh processes, ONE timing each  (the naive aggregation)
    ────────┼──────────────────────────────────────────────────────────────
     25.6   │ ████████████████████████████████████████████████████    40   ← mode ≈ min
     27.6   │ ████████████                                             9   ← median 27.59
     29.6   │ █████                                                    4
     31.5   │ █████                                                    4
     33.5   │                                                          0   ← MEAN 33.85 lands HERE,
     35.5   │ ███                                                      2      in an EMPTY BIN.
     37.5   │ █████                                                    4      No run was ever
     39.5   │                                                          0      "average".
     41.4   │ █                                                        1
     43.4   │ ████                                                     3
     45.4   │ █                                                        1
     47.4   │ ███                                                      2
     49.3   │ ████                                                     3   ← p90 50.5
     51.3   │                                                          0
     53.3   │                                                          0
     55.3   │ █                                                        1
     57.3   │                                                          0
     59.2   │ ████                                                     3
     61.2   │ █                                                        1
     63.2   │                                                          0
     65.2   │                                                          0        ↓ E-core tail:
     67.2   │                                                          0        73.1/25.6 = 2.85×
     69.1   │                                                          0        vs measured
     71.1   │ █                                                        1        E/P ratio 3.19×
```

**Read the second histogram. It is the whole argument of §8 in one picture.**

- The distribution is **not normal**. It is a sharp mode at the physical floor with a long
  right tail. That is the universal shape of a latency distribution: there is a hard lower
  bound (the work genuinely takes that long) and no upper bound (anything can interrupt you).
- **The mean, 33.85, falls in an empty bin.** No single run produced a value near it. A
  "mean ± std dev" summary of this data — 33.8 ± 11.5 — describes a symmetric distribution
  that does not exist, and its ±1σ interval (22.3 … 45.4) includes values *below the
  physical minimum*.
- The tail is not random. It's §4.1: the scheduler on E-cores.

### The two numbers to carry forward

| Aggregation | Noise floor (max/min spread) | Usable effect threshold |
|---|---|---|
| 80 processes, min-of-5 each, quiet machine | **5.8%** | don't believe anything under **~6%** |
| 80 processes, one timing each | **185%** | don't believe anything, full stop |

**On this machine, with careful aggregation, ~6% is the smallest difference I can
distinguish from noise using a naive best-of comparison.** With interleaving and a
bootstrap CI (§7.6, §8.5) I got that down to roughly **±1.3%** on a 40-pair comparison —
but that required a *paired, randomized, interleaved* design, not more samples.

**Write your machine's number down and put it at the top of every benchmark file you
write.** If doc 33 claims a 3% win, and your floor is 6%, you have not measured a 3% win.

---

## 6. `timeit` and its traps

`timeit` is in the standard library, is the first thing everyone reaches for, and has four
behaviours that surprise people. Two are documented; all four are consequential.

### 6.1 It disables the garbage collector — and that is a semantic change

Straight from `Lib/timeit.py` *(measured — `inspect.getsource(timeit.Timer.timeit)`)*:

```python
def timeit(self, number=default_number):
    it = itertools.repeat(None, number)
    gcold = gc.isenabled()
    gc.disable()                       # ← here
    try:
        timing = self.inner(it, self.timer)
    finally:
        if gcold:
            gc.enable()
    return timing
```

The documented rationale is *"to make independent timings more comparable"* — and for a
pure-arithmetic snippet it is harmless. For anything that allocates, it is not noise
reduction; **it is running a different program.**

Measured. Workload: build a two-node reference cycle per iteration — objects that
refcounting alone can *never* free, so only the cycle collector reclaims them
([`22-garbage-collection.md`](22-garbage-collection.md)). 200,000 iterations, 7 repeats
*(measured)*:

| Harness | min ns/op | median ns/op |
|---|---|---|
| `timeit` default (GC disabled) | 79.25 | — |
| hand loop, `gc.disable()` | 80.26 | 81.61 |
| hand loop, `gc.enable()` | **199.02** | **201.87** |

**GC-on is 2.47× slower than GC-off** *(measured)*. `timeit` reports the 80, your
production process experiences the 200. And note the second-order harm: with GC disabled,
the loop *also* has different memory behaviour — the heap grows monotonically, so
allocation is bump-pointer-ish and cache-resident in a way it never is in a program that
actually collects.

**Rules:**
- If your snippet allocates, `timeit`'s default number is not a production number.
- Re-enable the GC explicitly when you care: put `import gc; gc.enable()` in the setup and
  understand that `timeit` will disable it again — so hand-roll the loop, as above, or use
  `pyperf`, which does **not** disable the GC.
- Conversely, if you *want* to isolate non-GC cost — e.g. attributing a regression — the
  GC-off number is the right instrument. Just label it.

### 6.2 `number` vs `repeat` — they measure different things

```console
$ python -m timeit -n 1000 -r 7 "some_expr"
```

- **`number` (`-n`)** = iterations of the inner loop *inside one timed region*. It exists
  to amortize the 41.7 ns clock quantum (§2). Increasing it reduces *instrument* error.
- **`repeat` (`-r`, default 5)** = how many times that whole timed region is repeated. It
  exists to sample *machine* noise. Increasing it gives you a distribution.

Conflating them is the classic error. `-n 10000000 -r 1` gives you one exquisitely precise
measurement of one moment on one core — high precision, unknown accuracy. `-n 1 -r 1000`
gives you a thousand measurements each dominated by clock overhead. You need both large
enough, and `timeit` will pick `number` for you (`autorange`, targeting ≥ 0.2 s) if you
omit `-n`.

And the deeper problem: **`repeat` samples noise *within one process*.** It cannot sample
across-process variation — hash seed, ASLR, initial core placement — because there is only
one process. §5 shows that across-process variation is the dominant term here. This is the
single strongest argument for `pyperf` (§7).

### 6.3 `min` — why it was recommended, and why that is contested

The `timeit` documentation's own note:

> *"It's tempting to calculate mean and standard deviation from the result vector and
> report these. However, this is not very useful. In a typical case, the lowest value gives
> a lower bound for how fast your machine can run the given code snippet; higher values in
> the result vector are typically not caused by variability in Python's speed, but by other
> processes interfering with your timing accuracy."*

**The case for `min`:** noise is one-sided. Nothing makes code run faster than physics
allows, so every deviation is additive interference. The minimum is therefore the best
estimator of "the intrinsic cost", and it is robust to spikes.

**The case against `min`, which the CPython performance community now largely accepts:**

1. **`min` is an extreme order statistic, and extreme order statistics are high-variance.**
   Measured, and this is the number that convinced me: in §8.6's experiment — 20
   *byte-identical* "variants", 9 runs each, randomized order — the spread of the
   per-variant **medians** was **1.6%**, while the spread of the per-variant **minima** was
   **17.2%** *(measured)*. `min` looks stable and is not. It is one lucky sample away from
   moving.
2. **`min` is not robust to sustained contention** — §4.3 measured `min` moving +8.9% under
   background load. It filters spikes, not pressure.
3. **`min` is unrepresentative when the nuisance parameter is real.** Hash seed (§4.8a)
   varies in production. Taking the minimum over seeds reports the performance your users
   get on their luckiest day. Stinner's argument: users run with ASLR on and a random hash
   seed, so the *average over those* is the honest estimate.
4. **`min` cannot give you a confidence interval** in any straightforward way, so it cannot
   tell you when to stop.

**What I actually recommend, and what this document does:** report **the whole
distribution** — min, median, mean, p90, max, and a histogram. Use the **median** as the
headline (robust, has a well-behaved bootstrap CI), report the **min** alongside as the
physical floor, and report the **mean** only when you are aggregating throughput (where it
is the right statistic, because total time *is* a sum). Never report mean ± σ alone for
latency (§8.2).

### 6.4 A `timeit` one-liner tells you almost nothing about production

The deepest trap, and it is not about `timeit`'s API at all. A one-liner benchmark differs
from production in every dimension that matters:

| Dimension | `timeit` one-liner | Production |
|---|---|---|
| Working set | tiny, L1-resident | large, DRAM-resident (§9.1: **5.8×**) |
| Branch history | one path, perfectly trained (§9.3) | polymorphic, mispredicted |
| Specialization | fully warm after ~1 call (§4.8c) | frequently cold |
| GC | **off** (§6.1: **2.47×**) | on, and pressured by everything else |
| Allocator state | steady, no fragmentation | fragmented, arenas pinned ([`16`](16-object-memory-layout.md) §5) |
| Type diversity | one type; inline caches never miss | many types; caches deoptimize |
| Instruction cache | one loop, fits trivially | megabytes of framework code |
| Concurrency | single thread | GIL contention / free-threading ([`24`](24-the-gil.md) §5) |

`timeit` is a **fine instrument for one question**: *given two ways of writing the same
tiny thing, which one executes fewer/cheaper bytecodes?* That is a real question and doc 33
asks it often. It is not the same question as *"will my service get faster?"*, and §12
exists to keep those apart.

---

## 7. `pyperf`, properly

`pyperf` (Victor Stinner, extracted from the work of stabilizing CPython's own benchmark
suite) is the correct default tool for Python microbenchmarks. Understanding *why* it is
built the way it is teaches most of this document.

### 7.1 The core design decision: fresh processes

`pyperf` runs your benchmark in **20 separate worker processes** by default, taking 3
values from each, after 1 warmup — 60 values total. It does not do 60 repetitions in one
process.

That is the entire point. From §4.8a and §4.4: hash seed and ASLR layout are **fixed for
the lifetime of a process**. Sixty repetitions in one process give you sixty samples of
*one* configuration; twenty processes give you twenty samples across the configuration
space your users actually occupy. §5's tables show the difference: within-process spread
was 1.06× on the low-noise aggregation, while across-process spread on a *nuisance
parameter I did control* (hash seed) was **1.32×**.

**Averaging across processes converts a bias into a sampled random variable.** That is the
single most important idea in `pyperf`'s design.

### 7.2 Calibration, warmups, and the knobs

| Option | Default (CPython) | What it controls |
|---|---|---|
| `-p/--processes` | **20** | worker processes — samples the per-process nuisance parameters |
| `-n/--values` | **3** | values per process |
| `-w/--warmups` | **1** | discarded values per process (DVFS + PEP 659, §4.2/§4.8c) |
| `-l/--loops` | *calibrated* | inner-loop iterations per value |
| `--min-time` | **100 ms** | calibration target for one raw value |
| `--rigorous` | ×2 processes → **40 procs / 120 values** | more samples across configurations |
| `--fast` | 10 procs × 2 values = 20 values | rough answers quickly |

Note that `--rigorous` multiplies **processes**, not values-per-process. `pyperf`'s authors
agree with §7.1: the marginal sample is worth more in a new process than in an old one.
(With a JIT — PyPy — the defaults invert to 6 processes × 10 values × 10 warmups, because
JIT warmup dominates and is per-process. As CPython's own JIT matures
[`21-tier2-and-jit.md`](21-tier2-and-jit.md), expect this trade-off to shift for CPython too.)

**Calibration** is the automatic solution to §6.2: `pyperf` grows `loops` until one raw
value takes ≥ 100 ms, so the 41.7 ns clock quantum (§2) contributes < 1 part in 2 million.

### 7.3 Reading the output — a real run

```console
$ python -m pyperf timeit --name loop -s "<the §3.2 workload>" "workload(20000)" -o loop.json
.....................
WARNING: the benchmark result may be unstable
* Not enough samples to get a stable result (95% certainly of less than 1% variation)
loop: Mean +- std dev: 502 us +- 26 us
```

*(measured)*. And the full statistics:

```console
$ python -m pyperf stats loop.json
Total duration: 11.2 sec
Number of calibration run: 1
Number of run with values: 20
Number of warmup per run: 1
Number of value per run: 3
Loop iterations per value: 256
Total number of values: 60

Minimum:         484 us
Median +- MAD:   496 us +- 6 us
Mean +- std dev: 502 us +- 26 us
Maximum:         639 us

  0th percentile: 484 us (-4% of the mean) -- minimum
  5th percentile: 487 us (-3% of the mean)
 25th percentile: 492 us (-2% of the mean) -- Q1
 50th percentile: 496 us (-1% of the mean) -- median
 75th percentile: 503 us (+0% of the mean) -- Q3
 95th percentile: 518 us (+3% of the mean)
100th percentile: 639 us (+27% of the mean) -- maximum

Number of outlier (out of 474 us..521 us): 2
```

**How to read this in ten seconds:**

1. **Compare median and mean.** 496 vs 502 — mean above median means right skew, i.e.
   there is a tail. Here it's small (1.2%) but present.
2. **Compare MAD and σ.** MAD 6 µs, σ 26 µs. σ is **4.3× MAD**. For a normal distribution
   σ ≈ 1.48 × MAD. A ratio this far off is a hard signal that the distribution is
   **not normal and σ is being driven by outliers**, not by the bulk.
3. **Look at the max.** 639 µs, +27% of the mean, while p95 is +3%. **The top 5% of runs
   contains a 24-percentage-point cliff.** That is a discrete event — a migration, a
   preemption — not continuous variation.
4. **Read the warning literally.** "Not enough samples to get a stable result (95%
   certainly of less than 1% variation)" means: *`pyperf` cannot certify a 1% effect from
   this data.* It is telling you your resolution.

`pyperf hist` renders it *(measured)*:

```
481 us:  4 ###########
488 us: 24 ####################################################################
496 us: 16 #############################################
503 us: 10 ############################
510 us:  2 ######
518 us:  2 ######
525 us:  0 |
   … eleven empty bins …
629 us:  1 ###
636 us:  1 ###
```

Same shape as §5: sharp mode near the floor, eleven empty bins, then two isolated far
outliers. **Always render the histogram.** A summary line would have hidden that gap
entirely, and the gap is the most informative feature in the data.

### 7.4 `pyperf metadata` — capture the world, not just the number

```console
$ python -m pyperf metadata loop.json
- cpu_count: 11
- platform: macOS-26.5.2-arm64-arm-64bit-Mach-O
- python_version: 3.14.6 (64-bit)
- python_compiler: Clang 22.1.3
- python_config_args: … '--with-tail-call-interp' '--with-mimalloc'
    '--enable-optimizations' '--enable-experimental-jit=yes-off' '--with-lto' …
- timer: mach_absolute_time(), resolution: 41.7 ns
- loops: 256
```

*(measured, trimmed)*. This is the feature people skip and shouldn't. A benchmark result
without the build configuration is not reproducible — see the §4.8 note on what those
config flags do. On Linux `pyperf` additionally records CPU affinity, CPU frequency
governor, ASLR state and `runnable_threads`; **none of those are available on macOS**,
which is another concrete cost of benchmarking here.

### 7.5 `pyperf compare_to`, and its limit

`compare_to` runs a **Student's two-sample, two-tailed t-test at α = 0.95** and prints
`Not significant!` when it can't reject the null.

Here is what it produced on this machine comparing **two runs of byte-identical code**
*(measured)*:

```console
$ python -m pyperf compare_to A.json A2.json
Mean +- std dev: [A] 624 us +- 138 us -> [A2] 502 us +- 25 us: 1.24x faster
```

**1.24× faster than itself.** Nothing changed. The `A` run happened during a load spike
(note its σ: 138 µs vs 25 µs) and the t-test happily declared a significant difference,
because *there was one* — between two different machine states, not two different programs.

Then the "real" comparison, run sequentially right after *(measured)*:

```console
$ python -m pyperf compare_to A.json C.json
Mean +- std dev: [A] 624 us +- 138 us -> [C] 400 us +- 20 us: 1.56x faster
```

1.56×, of which we now know **~1.24× is noise**. The honest residual is ~1.26×.

**The lesson is not that `pyperf` is broken.** Its statistics are correct. The
*experimental design* was broken: `pyperf` runs one benchmark to completion and then the
other, so **run order is perfectly confounded with machine state.** No statistical test can
repair a confounded design. This is the same failure as §10, and the fix is the same.

### 7.6 The fix: interleaved, randomized, paired comparison

Run A and C **alternating**, with the order of each pair randomized, then bootstrap a
confidence interval on the ratio of medians. 40 pairs, fresh processes throughout
*(measured)*:

| Variant | n | min | median | mean | max | CV |
|---|---|---|---|---|---|---|
| A — `t = (t + i*3) % M`, `range(n)` | 40 | 24.02 | 24.689 | 24.881 | 27.29 | 3.3% |
| C — `t = (t + i) % M`, `range(0, 3n, 3)` | 40 | 19.35 | 19.833 | 19.980 | 21.79 | 3.1% |

```
speedup (median A / median C) = 1.2448×    95% bootstrap CI [1.2337, 1.2572]

NULL CONTROL — A's first half vs A's second half:
    ratio = 1.0128×                        95% bootstrap CI [1.0014, 1.0384]
NULL CONTROL — A resampled against itself:
                                           95% bootstrap CI [0.9923, 1.0079]
```

Compare with the sequential `pyperf` answer of 1.56×. **The interleaved answer, 1.2448×
[1.2337, 1.2572], is the honest one**, and the sequential design overstated the effect by
about 25 percentage points.

**Read the null controls, because they are the part people omit.** Resampling A against
itself gives [0.9923, 1.0079] — that is the CI width the method can achieve, ±0.8%. But
comparing A's *first half* to A's *second half* gives **1.0128× with a CI that excludes
1.0** — there is a real 1.3% drift over the course of the experiment. **My own null
control failed at the 1.3% level.** So the honest resolution of this design on this machine
is not ±0.8%; it is **~±1.3%**, and I should not claim to resolve anything smaller.

> **This is the methodological core of the whole document.** Always run a null control —
> compare something to itself, using the exact pipeline you used for the real comparison.
> If the null control does not come back at 1.0, the difference is your method's error bar,
> and no claim smaller than it is admissible.

---

## 8. Statistics that matter

### 8.1 Distributions, not means

Both histograms in §5 make the point, but state it as a rule: **a benchmark result is a
distribution.** Reporting a single number discards the shape, and the shape is where the
information is (the eleven empty bins in §7.3; the bimodality in §5).

Minimum acceptable report for any benchmark claim: **n, min, median, mean, p90, max, and a
histogram.** If you can't fit the histogram, report `median` and `max/min`.

### 8.2 Why the mean is the wrong summary for latency

Three independent reasons, all visible in §5's second histogram:

1. **Latency distributions are bounded below and unbounded above.** The mean of a
   right-skewed distribution sits between the mode and the tail, describing neither. §5:
   mean = 33.85 landed in an **empty bin**.
2. **The mean is not robust.** One 200 ms GC pause in 1,000 samples of 1 ms moves the mean
   by 20%. It moves the median by nothing. If you're summarizing "typical", you want the
   robust statistic; if you're summarizing "total cost", you want the mean — decide which.
3. **`mean ± σ` implies a symmetric distribution.** §5's data: 33.8 ± 11.5 has a lower
   bound of 22.3, which is **below the physical minimum of 25.6**. The summary asserts
   something impossible.

**When the mean *is* right:** throughput and capacity planning. If you want "how many CPU
seconds will 10⁹ requests consume", the answer is genuinely `10⁹ × mean`, because total
time is a sum and sums are governed by means. Reach for the mean when you are adding up,
the median when you are describing.

**And note `pyperf` reports `Median ± MAD` alongside `Mean ± std dev` precisely so you can
compare them** (§7.3 step 2). The ratio σ/MAD is a free normality check.

### 8.3 Percentiles and their sampling error

Percentiles are the right currency for latency SLOs, and they are much noisier than people
assume.

The rule of thumb: **to estimate the p-th percentile you need on the order of `10/(1-p)`
samples for a stable answer** — ~100 for p90, ~1,000 for p99, ~10,000 for p99.9. With 60
samples (`pyperf`'s default), *your p99 is essentially the maximum*: it is determined by
one observation. In §7.3's run, "100th percentile: 639 µs" and "95th percentile: 518 µs"
are computed from 60 values — the p95 rests on about 3 observations and the max on 1.

Two corollaries:

- **Never compare p99s from small samples.** The difference between two p99s estimated from
  60 samples each is almost pure noise.
- **Percentiles do not average and do not add.** The mean of your ten shards' p99s is not
  the fleet p99. Aggregate the *distributions* (t-digest, HDR histogram), not the
  percentiles. This is one of the most common real production-metrics bugs.

### 8.4 Confidence intervals — and why bootstrap is the right default

A point estimate without an interval is not a measurement. But the textbook CI formula
(`mean ± t·σ/√n`) assumes normality, and §5 and §7.3 show your data is not normal.

**Use the bootstrap.** It makes no distributional assumption: resample your measurements
with replacement, recompute the statistic (median, ratio of medians, whatever you actually
report), repeat 10,000–20,000 times, and take the 2.5th and 97.5th percentiles of the
resulting distribution. That's the entire method, and it's ~10 lines:

```python
def bootstrap_ratio(a, b, n=20000, stat=statistics.median):
    """95% CI for stat(a)/stat(b) with no normality assumption."""
    rs = sorted(stat([random.choice(a) for _ in a]) /
                stat([random.choice(b) for _ in b]) for _ in range(n))
    return rs[int(0.025 * n)], rs[int(0.975 * n)]
```

This is what produced §7.6's `[1.2337, 1.2572]`. It works for the ratio of medians, which
has no closed-form CI, and that is exactly the statistic you want to report for an
A/B speedup.

**Independence caveat, stated because it is routinely violated:** the bootstrap assumes
your samples are independent. Consecutive samples *within one process* are not — they share
a hash seed, an address layout, a thermal state and probably a core. Bootstrap across
**processes**, not across in-process repetitions, or you will produce an interval far
narrower than the truth.

### 8.5 Effect size vs statistical significance

They are different questions and conflating them is the most common statistics error in
performance work.

- **Significance** asks: *is this difference distinguishable from zero?* With enough
  samples, **any** non-zero difference becomes significant. A 0.4% regression measured over
  10,000 runs is highly significant and completely irrelevant.
- **Effect size** asks: *is this difference big enough to care about?* That is an
  engineering judgement, and it must be made **before** you look at the data.

§7.5 gives the inverse failure too: `compare_to` declared a **significant** 1.24× on
identical code. Significance testing answers "could this be chance?" and is silent on
"could this be a confound?"

**Practice:** write down, before running anything, the smallest effect that would change
your decision (your **MDE** — minimum detectable effect). Then check your noise floor (§5)
and null control (§7.6) can resolve it. If they can't, either improve the design or admit
the experiment cannot answer the question. Doing that *afterwards* is how p-hacking
happens.

### 8.6 The multiple-comparisons trap — measured

You try 20 variants of a hot function and pick the fastest. Even if all 20 are identical,
the fastest of 20 noisy estimates will look faster than average. That is not a hypothetical:

**Experiment: 20 "variants" that are byte-identical. 9 fresh-process runs each, order fully
randomized within each round.** *(measured)*

```
grand median                  = 31.074 ns/op
best  'variant' (#19) median  = 30.827   (-0.79% vs grand median)
worst 'variant' (# 2) median  = 31.310   (+0.76% vs grand median)
best-vs-worst apparent speedup = 1.0157×      TRUE effect: exactly 1.0000×
Welch t (worst vs best) = 1.15, df = 13.4     (|t| > 2.1 would read as "p < 0.05")

spread of per-variant MINIMA  = 17.22%
```

Two findings, and the second is the surprising one:

1. **With proper randomization and interleaving, the trap was largely defused.** The best
   "variant" looked only 1.6% faster than the worst, and Welch's t was 1.15 — not
   significant. Randomization is genuinely protective. *(Had I run all 9 runs of variant 0,
   then all 9 of variant 1, and so on, §7.5 shows what would have happened.)*
2. **But if I had used `min` as my statistic, I would have declared a 17.2% winner among
   twenty copies of the same code.** That is §6.3's argument in one number: medians spread
   1.6%, minima spread **17.2%** — over 10× worse — because `min` is an extreme order
   statistic that one lucky sample can move.

**Defences, in order of value:**

1. **Randomize and interleave** (§7.6). Cheapest and most effective.
2. **Use a robust central statistic** (median), not an extreme one (min).
3. **Hold out.** Pick the winner on one set of runs, then *re-measure only the winner
   against only the baseline* with a fresh, larger experiment. If the effect doesn't
   survive replication, it wasn't there. This is what I did for the hash-seed result
   (§4.8a) — and it survived, which is why I believe that one.
4. **Adjust for multiplicity** if you insist on p-values (Bonferroni: divide α by the number
   of comparisons; with 20 variants your 0.05 becomes 0.0025). In practice, replication
   beats correction.

### 8.7 "It's 5% faster" needs a distribution, not two numbers

Assemble everything above into a rule for the sentence you are actually going to write in a
PR description.

A claim of the form *"X is 5% faster"* is admissible only if you can produce:

| Requirement | Why | Section |
|---|---|---|
| n, and n is across **processes** | within-process samples share nuisance parameters | §7.1, §8.4 |
| the noise floor of the host | 5% means nothing if the floor is 6% | §5 |
| a **null control** at ≈1.0 | proves the pipeline can detect "no change" | §7.6 |
| **interleaved, randomized** order | run order confounds with machine state | §7.6, §10 |
| a **confidence interval** on the ratio | "5%" without a CI is a point estimate of a random variable | §8.4 |
| min *and* median *and* max | shape carries the diagnosis | §8.1 |
| the build configuration | two "3.14.6"s can differ (§4.8) | §7.4 |
| whether GC was on | up to 2.47× | §6.1 |
| the working-set size vs production | up to 5.8× | §9.1 |

If you can't produce those, the honest sentence is *"it looks faster; I haven't measured
it well enough to say by how much."* That sentence has never once damaged anyone's
credibility, and the alternative regularly does.

---

## 9. Microbenchmark lies

A microbenchmark is a model of your program. All models are wrong; these are the specific
ways.

### 9.1 Unrealistic cache residency

The single biggest lie, and it connects straight back to
[`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §1.

Same code — a Python `for` loop summing a shuffled list of ints — at different sizes
*(measured)*:

| n | approx. bytes touched | ns/element | vs the 1K result |
|---|---|---|---|
| 1,000 | 0.04 MB | **10.92** | 1.00× |
| 8,000 | 0.29 MB | 9.58 | 0.88× |
| 64,000 | 2.30 MB | 11.05 | 1.01× |
| 512,000 | 18.4 MB | 14.77 | 1.35× |
| 2,000,000 | 72 MB | 53.57 | **4.91×** |
| 8,000,000 | 288 MB | 63.38 | **5.81×** |

**Nothing changed except how much data there is.** Below ~2 MB the whole thing lives in the
P-cluster's 16 MB L2 and the ints are cache-resident; above it, every pointer dereference
is a DRAM round trip and the prefetcher can't help because the addresses aren't known until
the pointers load ([`01`](01-memory-hierarchy-and-caches.md) §7).

Note the step is *not* at the L1 boundary and *not* sharp: 64,000 elements is already
2.3 MB, well past L1, and still runs at 11 ns because the L2 is 16 MB. The cliff arrives
between 512 K and 2 M elements. **Predicting where your own cliff is requires knowing your
cache sizes** — which is why doc 01 comes first.

**The practical failure:** you benchmark a "fast dict lookup" on 1,000 keys, ship it, and
production has 10 million keys and gets no faster, because in production the cost is one
DRAM access and your change optimized instruction count.

**Rule: your microbenchmark's working set must be the same order of magnitude as
production's.** If you don't know production's, that's the first thing to measure.

### 9.2 Dead-code elimination and constant folding

CPython's compiler folds constants and its optimizer drops provably-dead code. So does
every JIT and every C compiler at `-O2`. Measured *(measured)*:

| `timeit` statement | ns/loop |
|---|---|
| `1+1` | **2.50** |
| `2**20` | **2.51** |
| `'a'*100` | **2.51** |
| `None` | **2.51** |
| `x+y` (globals) | 6.83 |
| `[i for i in range(100)]` | 694.95 |

The first four are **the same number**, and it is `timeit`'s empty-loop cost. Why:

```console
>>> dis.dis(compile("2**20", "<s>", "eval"))
  RESUME 0
  LOAD_CONST 1048576      ← the exponentiation happened at COMPILE time
  RETURN_VALUE
```

`'a'*100` is likewise folded to a 100-character constant. You did not measure
exponentiation or string repetition; you measured `LOAD_CONST`. Note `x+y` at 6.83 ns —
that one wasn't folded (globals can change), and the ~4.3 ns gap over the baseline is the
actual cost of two `LOAD_GLOBAL`s and a specialized `BINARY_OP_ADD_INT`.

**Defences:**
- **Look at the disassembly.** `dis.dis(compile(stmt, '<s>', 'eval'))` takes five seconds
  and would have caught all four.
- **Make inputs opaque** — read them from a variable the compiler can't fold, ideally one
  whose value depends on something external.
- **Consume the result.** Accumulate it and return it, so nothing is provably dead.
- **Sanity-check the magnitude.** Anything that comes back at exactly your empty-loop cost
  was eliminated. Always measure your empty loop and know that number.

This gets far more dangerous in [`34-going-native.md`](34-going-native.md), where a C or
Rust compiler at `-O2` will delete your entire benchmark kernel if its result is unused —
and unlike CPython, it will do so silently and completely.

### 9.3 Branch predictors trained by repetition

Running the same branch a million times with the same outcome trains the predictor to ~100%
accuracy. In production that branch is polymorphic and mispredicts, at ~15–20 cycles a pop
([`01`](01-memory-hierarchy-and-caches.md) §1).

The Python-level version of this is **inline-cache specialization** (§4.8c). A benchmark
that calls `f(x)` a million times with `x` always an `int` gets `BINARY_OP_ADD_INT` and
never deoptimizes. Production calls it with ints, floats and `Decimal`s, the inline cache
thrashes, and you run the generic path. **A microbenchmark systematically over-reports the
benefit of specialization-friendly code**, and the gap is invisible unless you deliberately
vary types.

**Defence:** feed a *realistic distribution* of inputs, not one input. If production is 70%
int / 25% float / 5% other, make the benchmark 70/25/5. This one change routinely halves an
apparent speedup.

### 9.4 No allocator or GC pressure

§6.1 measured `timeit` deleting the GC's cost entirely (2.47×). But even with the GC on,
a microbenchmark's allocator is in an unrealistically good state:

- **No fragmentation.** pymalloc arenas are fresh; in production they're pinned by
  scattered survivors ([`16-object-memory-layout.md`](16-object-memory-layout.md) §5).
- **No competing allocation** from other subsystems interleaving in the size classes you're
  using, so free lists are always warm.
- **GC generation contents are unrealistic.** The cycle collector's cost is proportional to
  the number of *tracked objects it must traverse*
  ([`22-garbage-collection.md`](22-garbage-collection.md)) — and a benchmark process has a
  few thousand, while your service has millions. The same code with the same allocation
  rate costs dramatically more GC time in a big heap.

**Defence:** for anything allocation-sensitive, benchmark **inside a realistically-sized
heap**. Allocate a few million live objects in setup, then measure. The number will be
worse and it will be true.

### 9.5 The absent world

Finally, the things a microbenchmark structurally cannot contain: the network, the
database, the other 200 endpoints competing for i-cache, the GIL contention from your
request handler's neighbours ([`24-the-gil.md`](24-the-gil.md) §5), the container's cgroup
CPU quota, the noisy-neighbour VM. These do not make your microbenchmark wrong; they make
it **incomplete in a direction you must reason about explicitly** (§11, §12).

---

## 10. The experiment that fooled me

A worked example, in full, because [`16-object-memory-layout.md`](16-object-memory-layout.md)
§8 established the house standard: report the inconclusive experiment rather than invent a
story.

**Hypothesis.** Mytkowicz et al. (§4.5) showed that changing the size of an unread
environment variable shifts benchmark results by several percent, via stack alignment.
Reproduce it here.

**Experiment 1.** Set `BENCH_PAD` to a string of *k* bytes, for
k ∈ {0, 1, 7, 15, 63, 127, 1024, 4096, 16384}. Run the §3.2 benchmark, 12 fresh processes
per value of *k*, in that order. *(measured)*

| pad | min | median | mean | max |
|---|---|---|---|---|
| 0 B | 25.840 | **26.483** | 26.490 | 26.987 |
| 1 B | 25.903 | 26.251 | 26.245 | 26.565 |
| 7 B | 25.503 | 26.207 | 26.056 | 26.408 |
| 15 B | 25.314 | 25.645 | 25.700 | 26.122 |
| 63 B | 24.572 | 26.096 | 26.023 | 26.910 |
| 127 B | 24.096 | **24.615** | 24.594 | 25.297 |
| 1024 B | 24.294 | 24.579 | 24.624 | 25.027 |
| 4096 B | 24.211 | 24.617 | 24.668 | 25.089 |
| 16384 B | 24.306 | 24.538 | 24.631 | 25.096 |

Look at that. A clean, **monotone, ~7% step** somewhere between 63 and 127 bytes of
padding, stable across four subsequent sizes, visible in the min, the median, the mean and
the max alike. I had a story ready: *the environment block crosses a 128-byte cache-line
boundary at that point, shifting every stack frame's alignment relative to this machine's
128-byte lines.* The number was right, the mechanism was plausible, the boundary was
exactly where this machine's cache line is.

**It was entirely false.**

**Experiment 2 — same thing, interleaved, with the order of the five pad values randomized
in every round, 16 rounds.** *(measured)*

| pad | min | median | mean | max | median vs pad=0 |
|---|---|---|---|---|---|
| 0 B | 24.676 | 25.028 | 25.093 | 25.650 | 1.0000 |
| 15 B | 24.516 | 25.072 | 25.102 | 25.717 | 1.0018 |
| 127 B | 24.587 | 25.021 | 25.503 | 32.335 | **0.9997** |
| 1024 B | 24.537 | 25.036 | 25.014 | 25.688 | 1.0003 |
| 16384 B | 24.712 | 25.257 | 25.255 | 26.082 | 1.0092 |

**The effect is gone.** Every condition is within 0.9% of every other, i.e. inside the
noise floor from §5.

**What actually happened.** Experiment 1 ran the pad sizes *in ascending order*, one after
another, over about 100 seconds — and during those 100 seconds the machine's background
load was falling (the concurrent workload described in the provenance block was winding
down). **Run order was perfectly confounded with machine state.** The "128-byte cache line
boundary" was the moment the other process finished. My hypothesis correctly predicted the
boundary location by coincidence, which made the fake result *more* convincing, not less.

**Four lessons, all of which cost me time to learn here:**

1. **A monotone, mechanism-shaped result is not evidence of a mechanism.** Confounders are
   frequently monotone — thermal drift, cache warming, and background load all are.
2. **Having a plausible mechanism in advance made me *less* skeptical.** This is
   confirmation bias with a systems-engineering accent, and it is the specific failure mode
   of people who are good at systems.
3. **Randomization is not a statistical nicety. It is the load-bearing part of the design.**
   No amount of extra samples in experiment 1 would have found the error; more samples
   would have *tightened the CI around the wrong answer*.
4. **What I can and cannot conclude:** I have **not** reproduced the Mytkowicz effect on
   this machine, and I have **not** shown it is absent. This design measures environment
   size, but ASLR (§4.4) re-randomizes the layout on every execution anyway — so each
   process is already sampling a random alignment, and any fixed alignment effect is
   averaged away by the very thing I couldn't disable. On Linux, with `setarch -R` to pin
   the layout, the question is directly answerable. **Here it is not, and I am recording
   that as an open item rather than a result.**

> This section is the reason doc 31 exists. The failure took four minutes to produce, the
> result was clean and plausible, and I would have published it if I hadn't run the control.

---

## 11. Measuring in production

Everything above measures a **change**. Production measures a **system**. They are
different activities with different tools, and the most expensive mistakes come from
substituting one for the other.

### 11.1 The distinction, precisely

| | Measuring a *change* | Measuring a *system* |
|---|---|---|
| Question | did this diff make this code faster? | where does the time go, and what should we fix? |
| Environment | controlled; everything else held fixed | uncontrolled; everything varies |
| Instrument | microbenchmark, `pyperf`, A/B | RED/USE metrics, distributed traces, sampling profilers |
| Statistic | ratio of medians + CI | full latency distribution over time, per-dependency |
| Failure mode | confounding (§7.5, §10) | **measuring the wrong layer** |
| Doc | this one | [`32-profiling.md`](32-profiling.md), [`46-production-python.md`](46-production-python.md) |

A microbenchmark can tell you a function got 2× faster. Only production can tell you the
function was 0.4% of your latency budget.

### 11.2 A/B tests and canaries

- **Canary:** deploy the change to a small fraction of hosts and compare against the rest.
  Cheap and honest about the *deployment*, but time-confounded exactly like §7.5 — the
  canary and the baseline experience different traffic at different moments. Mitigate by
  running long enough to cover a full traffic cycle (day/night, weekday/weekend), and by
  **flip-flopping**: swap which hosts are canary halfway through. That is §7.6's
  interleaving, at fleet scale.
- **A/B (request-level):** route a random fraction of *requests* to each arm. Randomization
  is per-request, so time confounding largely cancels — this is the strongest design
  available and it is the production analogue of §7.6.
- **The traps:**
  - **Randomization unit ≠ analysis unit.** If you randomize by user but analyse by request,
    heavy users dominate and your variance estimate is wrong (you need clustered errors).
  - **Cache warming.** The arm that gets traffic first warms the caches for both. Route
    both arms from the start.
  - **Peeking.** Checking the dashboard repeatedly and stopping when it looks good is
    p-hacking with a nicer UI. Fix the duration in advance, or use a sequential test
    designed for continuous monitoring.
  - **Novelty/reallocation effects.** A new pod starts with a cold page cache, cold JIT
    state, cold connection pools. The first minutes are not representative.

### 11.3 Why your local optimization can't move a downstream-dominated p99

This is README §Tier-5's exit question, and the answer is arithmetic.

Suppose a request costs `L = L_local + L_dep`, where `L_dep` is a downstream call. Your
optimization halves `L_local`.

```
    Latency budget of one request, drawn to scale
    ────────────────────────────────────────────────────────────────────
    p50:   [ local 8ms ][ dep 12ms ]                          total 20ms
                └── halve this ──▶ 16ms total, a 20% win.  VISIBLE.

    p99:   [ local 9ms ][ dep ................. 191ms ]      total 200ms
                └── halve this ──▶ 195.5ms total, a 2.3% win.  INVISIBLE.
    ────────────────────────────────────────────────────────────────────
    The p99 request is not a slow version of the p50 request.
    It is a DIFFERENT request, in which a different component dominates.
```

**The tail is usually a different mechanism, not more of the same.** p99 is where retries,
connection-pool exhaustion, GC pauses, GIL convoys ([`24-the-gil.md`](24-the-gil.md) §5),
cgroup throttling and downstream tail latency live — none of which scale with your local
CPU cost.

Three practical consequences:

1. **Decompose before you optimize.** You need per-component latency (distributed tracing,
   or at minimum a span timer around each external call) *broken out by percentile*.
   Averages of components do not decompose a p99.
2. **Amdahl applies to the percentile, not to the mean.** Compute your local share *at the
   percentile you care about*. It is usually far smaller than at the median.
3. **If the tail is downstream, the levers are different**: hedged requests, deadlines and
   budgets, load-shedding, concurrency limits, caching, or fixing the dependency. None of
   those are Tier-5 optimizations, and reaching for `33-optimizing-python.md` here is
   category error.

### 11.4 The metrics your benchmark can't see

Instrument for these, because they explain "prod disagrees with my benchmark" more often
than anything else:

- **GC pause time and count** (`gc.callbacks` → histogram of pause durations).
- **RSS and page faults** — a change that's CPU-faster and memory-hungrier can be a net loss
  ([`35-memory-optimization.md`](35-memory-optimization.md)).
- **cgroup CPU throttling** (`/sys/fs/cgroup/cpu.stat` `nr_throttled`, `throttled_usec`).
  Low CPU utilization plus high latency is throttling until proven otherwise.
- **GIL wait time** — via `sys.setswitchinterval` sensitivity or a sampling profiler that
  distinguishes on-CPU from waiting-for-GIL.
- **Queue depth / concurrency**, which converts a small latency change into a large one via
  queueing theory. At 80% utilization, a 10% service-time improvement is a ~30% latency
  improvement. **Utilization is a multiplier on your effect size**, and it means the same
  code change is worth wildly different amounts depending on load.

---

## 12. The decision framework

The question is not "should I benchmark?" It is **"what is the cheapest experiment that can
change my decision?"**

```
                    ┌──────────────────────────────────────────┐
                    │  What decision am I trying to make?      │
                    └────────────────────┬─────────────────────┘
                                         ▼
              ┌──────────────────────────────────────────────────────┐
              │  Do I know which code is hot, from PRODUCTION data?   │
              └───────┬──────────────────────────────────┬───────────┘
                   NO │                                   │ YES
                      ▼                                   ▼
        ┌─────────────────────────────┐     ┌─────────────────────────────────────┐
        │ STOP. Do not benchmark.     │     │ Is the hot thing algorithmic        │
        │ Profile production first.   │     │ (complexity / data volume)?         │
        │ → 32-profiling.md           │     └────┬──────────────────────┬─────────┘
        │ 90% of "optimizations" die  │      YES │                   NO │
        │ here, correctly.            │          ▼                      ▼
        └─────────────────────────────┘   ┌────────────────┐   ┌───────────────────────┐
                                          │ Fix the        │   │ Is the candidate      │
                                          │ algorithm.     │   │ change LOCAL — one    │
                                          │ No benchmark   │   │ function, same        │
                                          │ needed to      │   │ inputs, same data     │
                                          │ prefer O(n)    │   │ shapes?               │
                                          │ over O(n²).    │   └───┬───────────────┬───┘
                                          │ Benchmark      │   YES │            NO │
                                          │ only to size   │       ▼               ▼
                                          │ the constant.  │  ┌─────────┐   ┌─────────────┐
                                          └────────────────┘  │ MICRO-  │   │ MACRO-      │
                                                              │ BENCH   │   │ BENCH       │
                                                              │ legit   │   │ required    │
                                                              └────┬────┘   └──────┬──────┘
                                                                   ▼               ▼
                    ┌──────────────────────────────────────────────────────────────────┐
                    │  GATES — a result that fails any of these is inadmissible        │
                    │                                                                  │
                    │  [ ] effect  >  noise floor (§5)          — else: unmeasurable   │
                    │  [ ] null control ≈ 1.0 (§7.6)            — else: broken pipeline│
                    │  [ ] interleaved + randomized (§7.6,§10)  — else: confounded     │
                    │  [ ] working set ≈ production (§9.1)      — else: wrong world    │
                    │  [ ] input distribution realistic (§9.3)  — else: fake spec.     │
                    │  [ ] GC state matches production (§6.1)   — else: 2.5× wrong     │
                    │  [ ] disassembly shows the work exists (§9.2) — else: folded away│
                    │  [ ] CI on the ratio reported (§8.4)      — else: not a result   │
                    └───────────────────────────────┬──────────────────────────────────┘
                                                    ▼
                    ┌──────────────────────────────────────────────────────────────────┐
                    │  Does the change touch: allocation volume · GC pressure ·        │
                    │  RSS · thread interaction · I/O · anything cross-service?        │
                    └──────────┬────────────────────────────────────────┬──────────────┘
                            NO │                                    YES │
                               ▼                                        ▼
                  ┌────────────────────────┐            ┌───────────────────────────────┐
                  │ Ship it behind a flag. │            │ PRODUCTION DATA REQUIRED.     │
                  │ Confirm in prod        │            │ Canary or request-level A/B.  │
                  │ metrics anyway.        │            │ A microbenchmark CANNOT       │
                  └────────────────────────┘            │ answer this. §11.             │
                                                        └───────────────────────────────┘
```

### 12.1 When a microbenchmark is legitimate

All of these must hold:

- The change is **local** — one function or expression, unchanged inputs and outputs.
- The **cost model is CPU-and-cache only** — no allocation change, no I/O, no threading.
- You have **production evidence that this code is hot** (else the answer doesn't matter).
- The **working set and input distribution** can be made production-like (§9.1, §9.3).
- The expected effect is **comfortably above your noise floor** (§5).

Typical legitimate uses: comparing two ways to spell the same loop; measuring
attribute-lookup hoisting; sizing the constant factor of a data structure choice; verifying
that a specialization actually fires. That is most of
[`33-optimizing-python.md`](33-optimizing-python.md).

### 12.2 When you need a macrobenchmark

When the change crosses a boundary a microbenchmark cannot model:

- **Allocation volume or object lifetime changes** → GC and allocator behaviour change
  non-locally ([`22`](22-garbage-collection.md), [`16`](16-object-memory-layout.md)).
- **Data structure layout changes** → cache behaviour depends on the whole heap
  ([`01`](01-memory-hierarchy-and-caches.md)).
- **Threading, GIL or free-threading changes** → contention is a property of the whole
  program ([`24`](24-the-gil.md), [`26-free-threading.md`](26-free-threading.md)).
- **Startup / import path changes** → page cache and import graph, not steady state.
- **Native extension boundaries** → GIL release, buffer copies, batching effects
  ([`34-going-native.md`](34-going-native.md)).

A macrobenchmark means: run the **real workload** (a replayed request trace is ideal), in
a **realistically-sized process**, for long enough to reach steady state, and measure the
**whole distribution**. `pyperformance` is the canonical Python example; your own service's
load test is better because it has your data shapes.

### 12.3 When only production will do

- The effect is smaller than a macrobenchmark can resolve, but valuable at scale (a 1% CPU
  saving across a large fleet is worth real money, and no benchmark of yours resolves 1%).
- The metric is a **tail** (§11.3) — tails are made of rare events that a load test won't
  faithfully reproduce.
- The change interacts with **real traffic shape**: cache hit rates, key skew, request-size
  distribution, retry storms.
- The change is a **capacity** question — throughput at a given latency SLO under real
  concurrency, where queueing amplifies everything (§11.4).

**And the corollary that saves the most time:** if the honest answer is "only production
can tell us", then **build the flag and the metric first**, and skip the elaborate local
benchmark entirely. Days get burned producing microbenchmarks for questions that were never
microbenchmark-shaped.

---

## 13. House rules — the one-page checklist

Pin this next to your benchmark files.

**Before you measure**
1. Write down the **decision** this measurement will change. If none, stop.
2. Write down the **minimum effect** that would change it (your MDE).
3. Know your **noise floor** (§5). If MDE < floor, redesign or don't measure.
4. Know your **clock resolution** and empty-loop cost (§2, §9.2).

**Designing the experiment**
5. **Fresh processes**, always. Never conclude from in-process repetitions alone (§7.1).
6. **Interleave and randomize** the arms. Never all-of-A-then-all-of-B (§7.6, §10).
7. **Run a null control** — A vs A, through the identical pipeline (§7.6).
8. Match **working-set size** (§9.1), **input distribution** (§9.3), **GC state** (§6.1),
   and **heap size** (§9.4) to production.
9. Do **not** pin `PYTHONHASHSEED`; sample it (§4.8a).
10. `dis` the thing you're benchmarking. Confirm the work exists (§9.2).

**Running**
11. Record `load1`, wall-clock time and build config with every result (§4.3, §7.4).
12. Warm up — enough for DVFS and PEP 659, not so much that you've erased the cold path
    you care about (§4.2, §4.8c).
13. Don't do anything else on the machine. Close the browser. Yes, really (§4.3).

**Reporting**
14. Report **n, min, median, mean, p90, max**, and a histogram (§8.1).
15. Report a **bootstrap CI on the ratio**, resampled across processes (§8.4).
16. Report the **null control's** result next to the real one (§7.6).
17. State the **effect size**, not just significance (§8.5).
18. If you tried k variants, say so and **replicate the winner** (§8.6).
19. Label anything you could not control. This document's §4.4 and §10 are the model.

**The single question to ask any benchmark, including your own:**
> *"What would this experiment have shown if the change did nothing?"*
> If you can't answer, you haven't run the null control, and you don't have a result.

---

## 14. Lab exercises

Reading this leaves you at rung 3 (README §14) — you can now *explain* measurement noise
fluently, which is exactly the trap that rung describes. These move you to rung 4, and
labs 4, 6 and 8 are the rung-5 ones, because they end in "here is where my model stopped."

**1 — Establish your noise floor. (Do this before any other lab in this folder.)**
Reproduce §5: one fixed workload, 80 fresh processes, both aggregations (min-of-5 and
single-shot). Plot both histograms. Write the two spread numbers on a sticky note.
*Proves: you know the smallest effect your hardware can resolve, and it is bigger than you
expected.* Every later doc's claims must clear it.

**2 — Find your cluster tax.** Repeat lab 1 under `taskpolicy -c background`, `-c utility`,
`-c maintenance` and `nice -n 20`. Reproduce §3.3's table on your machine. Then take the
single-shot histogram from lab 1 and check whether its slow tail sits at the ratio you
measured for the E-cores. *Proves: you can attribute a distribution's tail to a specific
hardware mechanism, which is the difference between "noisy" and a diagnosis.* On a Linux
box, do the analogous thing with `taskset` and note how much easier it is.

**3 — Break `timeit` four ways.** (a) Time an allocation-heavy cyclic workload with `timeit`
and with a hand loop with the GC enabled; reproduce §6.1's 2.47×. (b) Time `2**20` and
disassemble it. (c) Sweep `-n` from 1 to 10⁷ at fixed `-r` and plot ns/op — find where the
41.7 ns clock quantum stops dominating. (d) Sweep `-r` at fixed `-n` and show that the
*spread* doesn't shrink, because `repeat` samples within one process.
*Proves: you understand what `timeit` measures and what it silently changes.*

**4 — Run the null control, and fail it.** Take any two *byte-identical* benchmark files.
Compare them with (a) `pyperf compare_to` run sequentially and (b) an interleaved,
randomized harness with a bootstrap CI. Reproduce §7.5's "1.24× faster than itself" if you
can — you may need to start a build in another window to induce the load shift.
*Proves: the design, not the statistics, is what makes a comparison valid.* **Rung 5:
report the CI width your own pipeline achieves on a null comparison. That number, not the
textbook's, is your resolution.**

**5 — Measure the hash-seed effect.** Reproduce §4.8a: a string-keyed dict benchmark across
12 fixed seeds, then *replicate* the best and worst with interleaved runs. Decide, from your
own data, whether Stinner is right that you should sample rather than pin.
*Proves: a nuisance parameter can dwarf your optimization, and replication is how you tell a
real effect from a lucky draw.*

**6 — Reproduce the measurement-bias failure (§10).** Sweep an environment-variable size and
run the conditions **in ascending order**. Find an effect. Write down the mechanism you
believe. *Then* re-run interleaved and randomized. **Rung 5: whatever the outcome, write one
paragraph on what you can and cannot conclude, and why ASLR makes the fixed-alignment
version of this question unanswerable on macOS.** If you have a Linux box, redo it under
`setarch -R` and answer it properly.

**7 — Watch a microbenchmark lie about cache residency.** Reproduce §9.1's sweep on your own
machine and locate *your* cliff. Predict it from `sysctl hw.perflevel*.l2cachesize` before
running. Then construct the trap deliberately: two implementations where the small-n winner
is the large-n loser. *Proves: §9.1 is not an edge case, it is the default failure of every
microbenchmark that doesn't specify its working set.*

**8 — The multiple-comparisons trap, both ways.** Take 20 byte-identical copies of a
benchmark. Run them (a) sequentially, all runs of copy 0, then copy 1, …, and (b)
interleaved with randomized order. Report, for each, the apparent best-vs-worst speedup
using **median** and using **min**. Reproduce §8.6's finding that minima spread ~10× more
than medians. **Rung 5: state the false-discovery rate you'd expect if you screened 20 real
candidate optimizations with design (a), and what you'd do instead.**

---

## 15. Question bank

Staff-level. Each names the section to reread if you can't answer from your own model.

1. Your microbenchmark says the change is 40% faster; production says no change. Give five
   distinct mechanisms. *(§9 entire, §11.3)*
2. Why is a bad benchmark worse than no benchmark? Answer in terms of asymmetry of harm,
   not in terms of accuracy. *(§1)*
3. What is your machine's clock resolution, and why does that force an inner loop? What does
   the inner loop then do to the branch predictor? *(§2, §9.3)*
4. `taskset` doesn't exist on macOS. What *can* you control, what can't you, and what would
   a Linux box give you? *(§3.3)*
5. A benchmark's histogram is bimodal, with the second mode at ~3× the first. On this
   hardware, what is your first hypothesis and how do you test it? *(§4.1, §3.2)*
6. Why does taking the `min` not protect you from background load, when it does protect you
   from a one-off context switch? *(§4.3)*
7. `timeit` disables the GC. Name the situation where that makes it report a number 2.5×
   better than reality, and the situation where disabling the GC is the *correct* choice.
   *(§6.1)*
8. Distinguish `number` from `repeat`. Which one addresses instrument error, which addresses
   machine noise, and what does *neither* address? *(§6.2, §7.1)*
9. Give the strongest argument for reporting `min`, then the two measurements in this
   document that undermine it. *(§6.3, §4.3, §8.6)*
10. Why does `pyperf` spawn 20 processes instead of doing 20 repetitions in one? Name two
    per-process nuisance parameters it is sampling. *(§7.1, §4.8a, §4.4)*
11. `pyperf compare_to` reported "1.24x faster" comparing a benchmark to itself. Its
    statistics were correct. What was wrong, and what is the fix? *(§7.5, §7.6)*
12. What is a null control, and what does it mean when yours comes back at 1.013 with a CI
    excluding 1.0? *(§7.6)*
13. Why is `mean ± std dev` an actively misleading summary of a latency distribution? Give
    the case where the mean *is* the right statistic. *(§8.2)*
14. You have 60 samples. Why is your p99 meaningless, and roughly how many would you need?
    Why can't you average p99s across shards? *(§8.3)*
15. Distinguish effect size from statistical significance, and give one failure in each
    direction. *(§8.5)*
16. You benchmarked 20 variants and the best is 8% faster. What do you do before believing
    it? *(§8.6)*
17. Why do `timeit('1+1')` and `timeit('2**20')` return the same number, and what would you
    check in five seconds to catch it? *(§9.2)*
18. Your dict benchmark is 3× faster than production's. Give the cache-hierarchy
    explanation, with the working-set sizes at which the cliff appears. *(§9.1, doc 01 §1)*
19. Why does a microbenchmark systematically *over*-report the benefit of code that PEP 659
    can specialize? *(§9.3, §4.8c)*
20. p99 is dominated by a downstream dependency. Prove arithmetically that halving your
    local CPU cost can't help, and name three levers that can. *(§11.3)*
21. When is a microbenchmark legitimate, when do you need a macrobenchmark, and when will
    only production do? Give the boundary condition for each. *(§12)*
22. Someone shows you "5% faster" with two numbers. List what you ask for before believing
    it. *(§8.7)*

---

## 16. Sources

**Primary — the papers and docs this document is built on**

- **Mytkowicz, Diwan, Hauswirth & Sweeney, ["Producing Wrong Data Without Doing Anything Obviously Wrong!"](https://users.cs.northwestern.edu/~robby/courses/322-2013-spring/mytkowicz-wrong-data.pdf)**, ASPLOS 2009 🆓 — **the canonical paper on measurement bias.** Shows that UNIX environment size and object link order shift results enough to reverse a compiler-optimization study, and introduces *setup randomization* and *causal analysis* as the defences. **Verdict: read it in full, once, this week.** It is the intellectual foundation of §4.5, §8.6 and §10, and it will permanently change how you read other people's benchmarks.
- **[pyperf documentation](https://pyperf.readthedocs.io/)** (Victor Stinner) 🆓 — in particular [Analyze benchmark results](https://pyperf.readthedocs.io/en/latest/analyze.html) (the min-vs-average and median-vs-MAD discussions, with the primary-source links), [Runner CLI](https://pyperf.readthedocs.io/en/latest/runner.html) (the defaults quoted in §7.2) and [Tune the system for benchmarks](https://pyperf.readthedocs.io/en/latest/system.html). **Verdict: essential and short. Read `analyze.html` before you next report a benchmark.** Note the system-tuning page is Linux-only in practice.
- **[`timeit` — docs.python.org](https://docs.python.org/3/library/timeit.html)** 🆓 — read the `Timer.repeat` note on `min` (quoted in §6.3) and then read `Lib/timeit.py` itself; the `gc.disable()` is six lines in. **Verdict: read the source, not just the docs.** The docs don't foreground the GC behaviour and it's the biggest trap.

**Benchmark stability — Victor Stinner's series**

- **[My journey to stable benchmark, part 1 (system tuning)](https://vstinner.github.io/journey-to-stable-benchmark-system.html)**, **[part 2 (PGO / dead code)](https://vstinner.github.io/journey-to-stable-benchmark-deadcode.html)**, **[part 3 (average)](https://vstinner.github.io/journey-to-stable-benchmark-average.html)** 🆓 — the practitioner's account of making CPython's own benchmarks trustworthy. Part 3 is the source of §4.8a's "sample the hash seed, don't pin it" argument, including the cherry-picking example where measuring seeds 1–3 instead of 1–5 reverses the sign of the result. **Verdict: part 3 is required; parts 1–2 are the Linux tuning playbook you'll want when you get access to a real benchmark host.**
- **[Visualize the system noise using perf and CPU isolation](https://vstinner.github.io/perf-visualize-system-noise-with-cpu-isolation.html)** 🆓 — what §5's histograms look like when you *can* isolate cores. **Verdict: read it to see what you're missing on macOS.**

**Statistics for performance work**

- **Georges, Buytaert & Eeckhout, ["Statistically Rigorous Java Performance Evaluation"](http://buytaert.net/statistically-rigorous-java-performance-evaluation)**, OOPSLA 2007 🆓 — the paper that established confidence intervals rather than point estimates as the standard in managed-runtime benchmarking. The JVM specifics don't transfer; the methodology transfers completely. **Verdict: read §3–4 for the CI machinery; skip the JVM-specific sections.**
- **Kevin Modzelewski, ["Benchmarking: minimum vs average"](http://blog.kevmod.com/2016/06/benchmarking-minimum-vs-average/)** (2016) 🆓 — the clearest short statement of the anti-`min` case, from the Pyston side. **Verdict: ten minutes, and it settles §6.3.**
- **[pyperf issue #1 — "Use a better measure than average and standard deviation"](https://github.com/psf/pyperf/issues/1)** 🆓 — the actual argument, in public, between practitioners. **Verdict: skim for the disagreements; it's more honest than any tutorial.**

**Methodology and hardware**

- **Denis Bakhvalov, [*Performance Analysis and Tuning on Modern CPUs*, 2e](https://easyperf.net/)** (2024) 🆓 — **chapters 2–3 are the direct complement to this document**: ch. 2 on performance-measurement methodology and noise, ch. 3 on the CPU-level mechanisms (alignment, predictors, caches) that make §4.4 and §9.3 true. **Verdict: read ch. 2 alongside this doc and ch. 3 alongside [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md).**
- **Brendan Gregg, [*Systems Performance*, 2e](https://www.brendangregg.com/systems-performance-2nd-edition-book.html)** — ch. 2 (Methodology) is the source of the USE and RED methods and of the "measure the system, not the change" framing in §11. Also [Gregg's "Broken Linux Performance Tools"](https://www.brendangregg.com/blog/2016-07-16/broken-linux-performance-tools-scale14x.html) 🆓 for the general lesson that the tool is often the bug. **Verdict: ch. 2 is required reading before [`32-profiling.md`](32-profiling.md).**
- **Gil Tene, ["How NOT to Measure Latency"](https://www.youtube.com/watch?v=lJ8ydIuPFeU)** 🆓 — coordinated omission, and why your load generator is lying about your tail. **Verdict: watch it before you trust any p99 you did not compute yourself.** Directly relevant to §8.3 and §11.

**Tools used in this document**

- `pyperf` 2.10.0 — `timeit`, `stats`, `hist`, `metadata`, `compare_to`. Install with `uv pip install pyperf`.
- `taskpolicy(8)`, `sysctl(8)`, `time.get_clock_info` — the macOS-side instruments, such as they are (§3.3).
- `xctrace` / Instruments — the only PMU access on this platform, covered in [`32-profiling.md`](32-profiling.md).

**Sibling docs**

- [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §1–§4, §11 lab 8 — the cache sizes that determine §9.1's cliff, and the lab that hands this document its noise floor.
- [`20-eval-loop.md`](20-eval-loop.md) — PEP 659 specialization; the mechanism behind §4.8c's warmup curve.
- [`22-garbage-collection.md`](22-garbage-collection.md) — what `timeit` turns off in §6.1, and why heap size changes the answer in §9.4.
- [`24-the-gil.md`](24-the-gil.md) §5 — the convoy effect: a latency pathology no microbenchmark can see.
- [`26-free-threading.md`](26-free-threading.md) — where §7.6's paired design becomes mandatory, because the single-threaded overhead you're trying to measure is close to this machine's noise floor. *(Preview, measured here: this integer loop ran **1.072× slower** on `python3.14t` than on `python3.14`, medians over 30 interleaved fresh processes each — versus the ~1% pyperformance geomean the official HOWTO quotes for macOS aarch64. One microbenchmark is not a geomean; that is the whole point of doc 26 having its own measurements.)*
- [`32-profiling.md`](32-profiling.md) — the next document: what to do once you know your numbers mean something.

---

*Next: [`32-profiling.md`](32-profiling.md) — you now know whether a number is real. Profiling
is how you find out **which** number to chase, and it has its own systematic lies:
instrumentation overhead that changes what it measures, sampling bias, and the fact that
"60% of time in `_PyEval_EvalFrameDefault`" tells you almost nothing.*
