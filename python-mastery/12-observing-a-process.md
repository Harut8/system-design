# 12 — Observing a process

> **Tier 1, doc 12.** Prerequisites: [`06-processes-threads-scheduling.md`](06-processes-threads-scheduling.md)
> (you must know what a runqueue and a context switch are before a scheduler metric means
> anything), [`07-virtual-memory.md`](07-virtual-memory.md) §RSS/PSS,
> [`09-syscalls-and-io.md`](09-syscalls-and-io.md) (the trap path is what `strace` and
> `perf trace` are instrumenting), [`10-signals-fork-exec.md`](10-signals-fork-exec.md)
> (`ptrace` stops are signal stops). Feeds into:
> [`31-measurement-methodology.md`](31-measurement-methodology.md),
> [`32-profiling.md`](32-profiling.md) (which is this document turned inward — Python's
> own profilers rather than the kernel's), `13-cpython-source-map.md`.
>
> **THESIS: every observability tool is a bet about where the truth lives, and the bet is
> priced.** Counters are free and tell you almost nothing. Sampling is cheap and tells you
> where CPU time went — but only CPU time, and only for stacks it can walk. Tracing tells
> you everything and can cost you 62×. Introspection tells you what *Python* is doing and
> requires the runtime's cooperation. **The dominant failure in production performance work
> is not picking the wrong tool; it is not knowing which of the four you are holding**, and
> therefore not knowing what the output structurally cannot contain.
>
> The second thesis, specific to us: **a general-purpose process observer cannot see
> Python.** `perf`, `sample`, `spindump`, and every native profiler in existence sees the
> interpreter's C stack, not your program. §7 shows this concretely, measured, and shows
> the surprising shape it has taken on CPython 3.14.

> **Provenance.** The subject of this document is *the Linux observability stack* —
> `perf_event_open`, the PMU, ftrace, kprobes/uprobes, eBPF, `ptrace`, and `/proc` — plus
> the Python-specific layer built on top of it. **This machine does not run Linux.** All
> Linux material is therefore researched from primary sources — the man-pages project, the
> kernel's own `Documentation/` tree, the CPython docs and PEP 768, and Brendan Gregg's
> published work — and is attributed inline. **No researched number is presented as though
> I measured it.**
>
> What *was* measured here ran on an **Apple M3 Pro** (5P + 6E, 11 logical CPUs, 128-byte
> cache lines, 16 KiB pages), **Darwin 25.5.0 / macOS 26.5.2 (build 25F84)**, arm64, with
> **CPython 3.14.6** (a `uv`-provided build, Clang 22.1.3, `WITH_DTRACE=0`, built
> *without* `-fno-omit-frame-pointer`). Those measurements are confined to §7 (what a
> native sampler sees when it profiles CPython), §15 (PEP 768 remote introspection), and
> §16 (the Darwin toolchain). They are marked *(measured)*. Two of them are **negative
> results** — a `PermissionError` and a refusal — and are reported as such, because on
> this platform the refusal *is* the finding.
>
> This document does not repeat [`32-profiling.md`](32-profiling.md). Doc 32 covers
> Python's *own* profilers (`cProfile`, `sys.monitoring`, and the distortion they
> introduce). **This document is about observing a process from outside it**, usually
> without its cooperation, often in production, frequently while it is on fire.

---

## Contents

1. [The problem: a process will not tell you what it is doing](#1-the-problem-a-process-will-not-tell-you-what-it-is-doing)
2. [Four mechanisms, four prices](#2-four-mechanisms-four-prices)
3. [`/proc`: the free tier](#3-proc-the-free-tier)
4. [Counting: `perf stat` and the PMU](#4-counting-perf-stat-and-the-pmu)
5. [Sampling: `perf record` and the overflow interrupt](#5-sampling-perf-record-and-the-overflow-interrupt)
6. [The stack-walking problem: `fp` vs `dwarf` vs `lbr`](#6-the-stack-walking-problem-fp-vs-dwarf-vs-lbr)
7. [Why a native profiler cannot see Python — measured](#7-why-a-native-profiler-cannot-see-python--measured)
8. [Making `perf` see Python: the trampoline](#8-making-perf-see-python-the-trampoline)
9. [Tracing: tracepoints, kprobes, uprobes, USDT](#9-tracing-tracepoints-kprobes-uprobes-usdt)
10. [ftrace: the tracer that is already in your kernel](#10-ftrace-the-tracer-that-is-already-in-your-kernel)
11. [eBPF and `bpftrace`: aggregate in the kernel, not in your terminal](#11-ebpf-and-bpftrace-aggregate-in-the-kernel-not-in-your-terminal)
12. [`strace`: the 62× tool](#12-strace-the-62-tool)
13. [Flame graphs: what the axes mean and what they cannot show](#13-flame-graphs-what-the-axes-mean-and-what-they-cannot-show)
14. [On-CPU vs off-CPU: the time your profile does not contain](#14-on-cpu-vs-off-cpu-the-time-your-profile-does-not-contain)
15. [Observing CPython from outside: PEP 768 and the unwinder — measured](#15-observing-cpython-from-outside-pep-768-and-the-unwinder--measured)
16. [Darwin: what you actually have — measured](#16-darwin-what-you-actually-have--measured)
17. [Permissions: the wall you will actually hit](#17-permissions-the-wall-you-will-actually-hit)
18. [The USE method applied to one Python process](#18-the-use-method-applied-to-one-python-process)
19. [The RED method applied to one Python service](#19-the-red-method-applied-to-one-python-service)
20. [Is it cgroup throttling? A decision procedure](#20-is-it-cgroup-throttling-a-decision-procedure)
21. [The observation session, end to end](#21-the-observation-session-end-to-end)
22. [A review checklist](#22-a-review-checklist)
23. [What I could not verify](#23-what-i-could-not-verify)
24. [Lab exercises](#24-lab-exercises)
25. [Question bank](#25-question-bank)
26. [Sources](#26-sources)

---

## 1. The problem: a process will not tell you what it is doing

A running process is an opaque object. It holds a virtual address space you cannot read,
a set of thread contexts the scheduler owns, and a program counter that moves a billion
times a second. Nothing about it is designed for you to watch.

Everything in this document is a workaround for that. There are exactly four of them, and
knowing which one you are using is the entire skill:

```
                    the process
                         │
   ┌─────────────┬───────┴────────┬──────────────────┐
   │             │                │                  │
COUNTING      SAMPLING         TRACING          INTROSPECTION
   │             │                │                  │
"how many?"  "where, mostly?"  "what, exactly?"  "what does the
                                                  runtime think?"
   │             │                │                  │
/proc          perf record     ftrace/eBPF        PEP 768
perf stat      sample(1)       strace/uprobes     py-spy
PMU counters   py-spy          perf trace         sys.monitoring
   │             │                │                  │
~0%          0.1–2%           1%–6200%           varies
```

The prices in the bottom row are not decoration. They are the reason the tools exist
separately. A tool that could give you exact answers for free would have replaced the
other three.

### 1.1 The one question that orders everything

Before you open a tool, answer this:

> **Is the process on-CPU or off-CPU when the problem happens?**

If it is *on*-CPU — burning cycles — you want sampling, and the answer is a flame graph.
If it is *off*-CPU — blocked on a lock, a socket, a disk, or the runqueue — **a CPU
profile is structurally incapable of containing your answer**, and you need §14.

Most people reach for a CPU profiler first, get a flat and uninteresting profile, and
conclude the tool is bad. The tool is fine. It answered the question it was asked, which
was not their question. §14 exists because this mistake is nearly universal.

---

## 2. Four mechanisms, four prices

### 2.1 Counting

A counter is a number the kernel or the hardware increments as a side effect of work it
was doing anyway. Reading it is one syscall or one file read. The cost of *maintaining*
it was already paid.

Everything in `/proc/[pid]/stat`, `/proc/[pid]/status`, and `/proc/[pid]/io` is a counter.
So is every hardware PMU event, when used in counting mode (`perf stat`).

**What counting can tell you:** totals and rates. "This process has taken 4.1 million
minor faults." "It ran 2.3 instructions per cycle." "It was involuntarily descheduled
18,000 times in the last minute."

**What counting cannot tell you:** *where*. A counter has no stack.

### 2.2 Sampling

Interrupt the process at some frequency; each time, record where it was. After N samples,
the distribution of "where" approximates the distribution of time.

This is statistical. It has a sampling error, it can miss anything shorter than the
sample interval, and it can be systematically biased if the sample clock beats in lockstep
with the workload (which is why the conventional profile rate is **99 Hz, not 100 Hz** —
an off-round frequency avoids phase-locking with timers and loops that tick at round
intervals; see [`31-measurement-methodology.md`](31-measurement-methodology.md) for the
general principle, and Gregg's `perf` examples for the convention).

**What sampling can tell you:** where CPU time went, with stacks, at ~1% cost.

**What sampling cannot tell you:** anything about time the thread was *not* running, and
anything whose duration is short and whose frequency is low (rare-but-slow events vanish).

### 2.3 Tracing

Instrument a specific event so that *every* occurrence is recorded. Tracing is exact. It
is also unbounded: the cost is (per-event cost) × (event frequency), and you do not
control the second term.

The per-event cost spans four orders of magnitude depending on mechanism:

| Mechanism | How the event is caught | Approximate per-event cost |
|---|---|---|
| Static tracepoint (kernel) | a patched-in nop → call site | tens of ns |
| USDT (userspace static) | a `nop` the tracer rewrites | tens of ns when enabled, ~0 when not |
| Optimized kprobe (`CONFIG_OPTPROBES`) | jump instruction | ~100 ns |
| Plain kprobe / uprobe | `int3` breakpoint → trap → single-step | µs-ish (a full trap; cf. [`09`](09-syscalls-and-io.md) §2) |
| `ptrace` syscall stop (`strace`) | **two** stops per syscall, each a context switch to the tracer | tens of µs |

That last row is why `strace` is in a category of its own; see §12.

### 2.4 Introspection

Ask the runtime. This is the only mechanism that can produce *Python* answers — function
names, line numbers, task trees — because it is the only one that knows Python exists.

It comes in two flavours: **cooperative** (the process runs code on your behalf —
`sys.monitoring`, `sys.remote_exec`) and **non-cooperative** (you read the process's
memory and decode its data structures yourself — py-spy, `_remote_debugging`). §15 covers
both.

### 2.5 The rule

> **Start with counting (free), narrow with sampling (cheap), confirm with tracing
> (expensive, targeted), and explain with introspection.**

Every investigation in §21 follows that order. Going straight to tracing is the most
common way to turn a performance problem into an outage.

---

## 3. `/proc`: the free tier

On Linux, `/proc/[pid]/` is a filesystem view of the kernel's `task_struct`. It costs one
`read()` per question and it is the single most under-used tier of the stack. You can
answer a startling fraction of production questions without installing anything.

*(All field descriptions in this section are from the `proc_pid_*(5)` man pages, not
measured here — this machine has no procfs.)*

### 3.1 `/proc/[pid]/stat` — the counters

52 space-separated fields on one line. The ones that matter, by position
(per `proc_pid_stat(5)`):

| # | Field | Why you care |
|---|---|---|
| 3 | `state` | `R` running/runnable, `S` interruptible sleep, `D` **uninterruptible** sleep (usually disk), `Z` zombie, `T` stopped |
| 10 | `minflt` | minor faults — see [`07`](07-virtual-memory.md); growth here is allocation, not I/O |
| 12 | `majflt` | **major** faults — this is real disk I/O to satisfy memory access. Non-zero and growing means you are swapping or thrashing the page cache |
| 14 | `utime` | user-mode CPU, in clock ticks (divide by `sysconf(_SC_CLK_TCK)`) |
| 15 | `stime` | kernel-mode CPU, same units |
| 20 | `num_threads` | thread count — a Python process with 400 threads is a finding |

`utime`/`stime` are **cumulative counters**. To get a rate you must sample twice and
subtract. Every "CPU %" number you have ever seen is that subtraction.

The `D` state deserves emphasis: a process in `D` is not stopped, not sleeping normally,
and cannot be killed with `SIGKILL`. It is inside the kernel waiting for something that
does not check for signals — almost always block I/O. **A Python process stuck in `D` is
not a Python problem.** No Python-level tool will ever explain it.

### 3.2 `/proc/[pid]/status` — the same thing, readable, plus the good part

`proc_pid_status(5)` documents the human-readable rendering: `VmRSS`, `RssAnon`,
`RssFile`, `RssShmem`, `VmHWM` (peak RSS — a counter that never goes down, which is
exactly what you want when diagnosing an OOM kill that already happened).

The two fields worth memorising are at the bottom:

```
voluntary_ctxt_switches:     14831
nonvoluntary_ctxt_switches:  2914
```

- **Voluntary** — the thread blocked. It called something that slept: a lock, a read, a
  `sleep`. High and growing = the thread is I/O- or lock-bound.
- **Nonvoluntary** — the scheduler took the CPU away. High and growing = **CPU
  contention**. Something else wants the core.

That single pair distinguishes "my process is waiting" from "my process is being
preempted", which are opposite problems with opposite fixes, and it is free.

### 3.3 `/proc/[pid]/schedstat` — scheduler latency, for one process, for free

Three numbers (requires `CONFIG_SCHED_INFO`/`CONFIG_SCHEDSTATS`):

```
<time spent on the cpu, ns>  <time spent waiting on a runqueue, ns>  <timeslices run>
```

The middle number is **run-queue latency for this process**: nanoseconds it was runnable
but not running. This is the USE method's "saturation" metric for CPU (§18), scoped to
one process, available without a tracer, at zero cost.

If your service's p99 is bad and this number is climbing, you are not slow — you are
**queued**. Profiling the code will find nothing, because the code was not running.

### 3.4 `/proc/[pid]/smaps_rollup` and the RSS question

`proc_pid_smaps(5)` describes the per-mapping breakdown; `smaps_rollup` is the
pre-summed version and is dramatically cheaper to read on a process with thousands of
mappings (a Python process with many loaded extension modules is exactly that).

The field that resolves most arguments is **`Pss`** — proportional set size, where a page
shared by N processes counts as 1/N. For a `fork()`-based server (gunicorn, celery,
`multiprocessing`), the sum of RSS across workers wildly overcounts; the sum of PSS does
not. See [`07-virtual-memory.md`](07-virtual-memory.md) for the mechanism and
[`10-signals-fork-exec.md`](10-signals-fork-exec.md) for why copy-on-write pages stop
being shared the moment the GC touches a refcount.

### 3.5 `/proc/[pid]/io`, `/wchan`, `/task/`, `/stack`

- **`io`** — `rchar`/`wchar` (bytes through the syscall interface) vs
  `read_bytes`/`write_bytes` (bytes that actually hit the block layer). The gap between
  them is the page cache doing its job. If `rchar` is huge and `read_bytes` is ~0, you
  are reading from RAM and your "disk problem" is not a disk problem.
- **`wchan`** — the kernel symbol the process is currently sleeping in
  (`proc_pid_wchan(5)`). One word that often names the entire problem:
  `futex_wait_queue`, `io_schedule`, `poll_schedule_timeout`.
- **`task/[tid]/`** — every field above, per thread. **On a GIL-bound Python process this
  is the only per-thread view you get for free**, and it is how you discover that 15 of
  your 16 threads are asleep. (See [`24-the-gil.md`](24-the-gil.md).)
- **`stack`** — the kernel-side stack of the task (root only). Not the Python stack, not
  even the userspace stack. Useful precisely once: when a thread is wedged in `D` state
  and you need to know which kernel path it is wedged in.

### 3.6 Delay accounting: the number `/proc` alone won't give you

The kernel's delay-accounting subsystem (`Documentation/accounting/delay-accounting.rst`)
tracks, per task, the time spent waiting on: CPU runqueue, block I/O, swap-in, memory
reclaim, thrashing, and more, delivered over a netlink/taskstats interface (the kernel
ships a `getdelays` example tool). It is the most precise "where did the wall-clock go"
accounting available without a tracer, and almost nobody uses it because it is not a
file you can `cat`.

**Know it exists.** When someone asks "the process took 50 seconds and used 12 seconds of
CPU — where did the other 38 go?", delay accounting answers it directly, and so does §14.

---

## 4. Counting: `perf stat` and the PMU

The **PMU** (performance monitoring unit) is hardware: a small bank of counters in the
CPU that increment on microarchitectural events — cycles, instructions retired, cache
misses, branch mispredictions, TLB misses. `perf stat` programs them via
`perf_event_open(2)` and reads them back.

This is the layer where [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
becomes observable rather than theoretical.

### 4.1 What `perf_event_open` actually is

One syscall, one enormous struct (`perf_event_attr`), one file descriptor per event
(`perf_event_open(2)`). The `type` field selects the event source:

| `type` | Source |
|---|---|
| `PERF_TYPE_HARDWARE` | generalized PMU events (`cycles`, `instructions`, …) |
| `PERF_TYPE_SOFTWARE` | kernel counters (`context-switches`, `page-faults`, `cpu-migrations`, `task-clock`) |
| `PERF_TYPE_TRACEPOINT` | static kernel tracepoints |
| `PERF_TYPE_HW_CACHE` | the cache-event encoding (L1D/LLC/dTLB × read/write × access/miss) |
| `PERF_TYPE_RAW` | a raw vendor event code — everything the generalized set omits |
| `PERF_TYPE_BREAKPOINT` | hardware watchpoints |
| dynamic `kprobe`/`uprobe` | set `kprobe_func`/`uprobe_path` + `probe_offset` |

`PERF_TYPE_RAW` is the escape hatch and it matters more than it looks: the "generalized"
hardware events are a lowest-common-denominator abstraction, and the events you actually
need for a memory-bound diagnosis are usually vendor-specific raw codes. The man page
points at **libpfm4** for translating vendor manual names into the hex values `config`
expects.

### 4.2 The trap: multiplexing

**There are more events you want than counters the hardware has.** Four to eight general
counters is typical. Ask for twelve events and the kernel time-slices them across the
counters, then scales the results up.

`perf_event_open(2)` gives you the means to detect this:

- `PERF_FORMAT_TOTAL_TIME_ENABLED` → `time_enabled`
- `PERF_FORMAT_TOTAL_TIME_RUNNING` → `time_running`

> "This can be used to calculate estimated totals if the PMU is overcommitted and
> multiplexing is happening." — `perf_event_open(2)`

The scaling is `count * (time_enabled / time_running)`. When `time_running < time_enabled`,
**your counter values are extrapolations, not measurements.** `perf stat` prints the
percentage in its output; people read past it.

> **The rule:** if a `perf stat` line shows less than 100% enabled, the number is an
> estimate. On a workload with phases (and every real workload has phases), that estimate
> can be badly wrong, because the counter may have been scheduled in only during one
> phase. Ask for fewer events, or use `--repeat` and check the variance.

### 4.3 Event grouping

Events opened in a **group** (via the `group_fd` argument) are scheduled onto the PMU
together or not at all. This matters whenever you compute a *ratio*: IPC is
`instructions / cycles`, and if the two counters were multiplexed independently they were
measured over different slices of the program's execution. **A ratio of two
non-grouped multiplexed counters is meaningless.** Group them.

### 4.4 What to actually ask for, for a Python process

```bash
# researched syntax, per perf-stat(1); not run on this machine
perf stat -e cycles,instructions,cache-references,cache-misses,\
branch-instructions,branch-misses,page-faults,context-switches \
  --repeat 5 -- python workload.py
```

The interpretation, for CPython specifically:

- **IPC below ~1.0** on a pure-Python workload is normal and not a bug. The eval loop is
  a chain of dependent, hard-to-predict indirect branches
  (see [`20-eval-loop.md`](20-eval-loop.md)). Do not "fix" it.
- **Branch misses** are the eval loop's signature cost. On a computed-goto interpreter
  each opcode's dispatch is an indirect jump whose target depends on data. This is
  exactly what PEP 659 specialization and 3.14's tail-call interpreter attack.
- **Cache misses** dominating means you are chasing pointers, which in CPython means you
  are chasing `PyObject*`s. That is an object-layout problem
  ([`16-object-memory-layout.md`](16-object-memory-layout.md)), not an algorithm problem.
- **`page-faults` growing steadily** in steady state means the allocator is still asking
  the kernel for memory — arena churn ([`07`](07-virtual-memory.md), doc 21).

### 4.5 Top-down analysis

On Intel (IceLake and later for the unrestricted form), `perf stat --topdown` classifies
every cycle into four buckets, per `perf-stat(1)`:

- **Frontend bound** — cannot fetch/decode instructions fast enough
- **Backend bound** — computation or memory access is the bottleneck
- **Bad speculation** — branch mispredictions and similar
- **Retiring** — actually doing work

The man page attaches a caveat that is easy to skip and load-bearing:

> "The bottleneck is only the real bottleneck if the workload is actually bound by the CPU
> and not by something else."

Which is §14, again, arriving from a different direction. Top-down tells you how the CPU
spent the cycles it spent. It says nothing about the cycles the CPU did not spend on you.

The same page also notes top-down needs the *full* PMU and recommends disabling the NMI
watchdog, and that on older Intel parts it is per-core, needing `-a` and therefore root
or `perf_event_paranoid=-1` (§17).

---

## 5. Sampling: `perf record` and the overflow interrupt

Sampling is the same PMU, used differently. Instead of reading a counter at the end, you
tell the counter to **overflow** every N events, and on overflow the CPU raises an
interrupt in which the kernel records a sample.

From `perf_event_open(2)`:

> "A 'sampling' event is one that generates an overflow notification every N events,
> where N is given by `sample_period`. A sampling event has `sample_period > 0`. When an
> overflow occurs, requested data is recorded in the mmap buffer."

Two ways to specify the rate:

- **`sample_period`** — every N events exactly. Deterministic, but on an event whose rate
  varies (like `cycles` under frequency scaling) the *time* between samples varies.
- **`sample_freq`** + the `freq` flag — target N samples per second; the kernel adjusts
  the period dynamically to hit it. This is what `-F 99` sets.

`sample_type` is a bitmask of what to record per sample: `PERF_SAMPLE_IP` (instruction
pointer), `PERF_SAMPLE_TID`, `PERF_SAMPLE_TIME`, `PERF_SAMPLE_CPU`, and the one that
makes the whole thing useful — **`PERF_SAMPLE_CALLCHAIN`**, "records the callchain (stack
backtrace)".

Samples land in a ring buffer the tool `mmap`s (`-m/--mmap-pages`). If the tool cannot
drain it fast enough, **samples are lost** — and since Linux 6.0 you can count them
exactly via `PERF_FORMAT_LOST`. Silent sample loss is a real failure mode of high-frequency
profiling and it biases the profile toward whatever was running when the buffer had room.

### 5.1 The canonical invocation

```bash
# researched syntax, per perf-record(1) and Gregg's perf examples; not run here
perf record -F 99 -g -p $PID -- sleep 30      # one process, 30s, with stacks
perf record -F 99 -ag -- sleep 30              # whole system, with stacks
```

`-F 99` and not `-F 100`: see §2.2. `-g` enables call-graph recording for both kernel and
user space — and immediately raises the question §6 exists to answer.

### 5.2 A default worth knowing

Gregg's `perf` page shows the `perf record -vv` dump of `perf_event_attr` for a
software event, where `sample_freq` defaults to **4000**. That is a much higher rate than
the 99 Hz used for CPU profiling, because for event-based sampling (context switches,
faults) the kernel is adaptively targeting 4,000 samples/sec rather than capturing every
event. If you meant *every* event, `-c 1`.

This is a good instance of a general trap: **`perf` will silently give you a sample of
your events rather than all of them**, and the difference only shows up if you were about
to compute a total.

---

## 6. The stack-walking problem: `fp` vs `dwarf` vs `lbr`

A sample without a stack tells you which instruction was executing. That is nearly
useless: it will be inside `memcpy`, or inside the eval loop. You need the *path*.

Walking a stack at interrupt time, in the kernel, from arbitrary code, is hard. There are
three strategies, and they trade correctness against cost differently.

### 6.1 `--call-graph fp` — frame pointers

The compiler keeps a register (`rbp` on x86-64, `x29` on AArch64) pointing at the current
frame, and each frame stores the caller's. Walking is a pointer chase: cheap, fast,
completely reliable — **if every frame in the stack has one**.

Compilers omit frame pointers by default under optimization, because it frees a register
and saves the prologue/epilogue. For twenty years this made system-wide stack walking on
Linux impossible in practice: your application might have frame pointers, but `libc`
would not, and any stack passing through `libc` broke.

**This changed recently and it is worth knowing the dates.** Fedora accepted the
`-fno-omit-frame-pointer` change (having rejected it once before), becoming the first
distro to re-enable frame pointers; Ubuntu announced frame pointers by default in
**24.04 LTS**; Arch followed (Gregg, *The Return of the Frame Pointers*, March 2024).

The cost argument that won:

> "Last time I studied the performance gain from frame pointer omission in our production
> environment, it was usually less than one percent, and it was often so close to zero
> that it was difficult to measure." — Gregg, *BPF Performance Tools* p. 40, quoted in
> his Fedora change comment

**The general lesson, which transfers well beyond profiling:** a <1% steady-state cost
that makes a 500% win *findable* is not a cost. Systems that cannot be observed do not get
optimized.

### 6.2 `--call-graph dwarf` — copy the stack, unwind offline

No frame pointers? Then the unwind information lives in DWARF `.eh_frame` data, which is
a bytecode program you have to interpret. You cannot do that at interrupt time. So `perf`
copies a chunk of the user stack (8 KB by default) into every sample and unwinds later.

This works on unmodified binaries, and it is expensive twice: **huge `perf.data` files**
(kilobytes per sample instead of tens of bytes) and **slow post-processing**. It also
truncates: stacks deeper than the copied window are cut off. Deep Python stacks are
exactly the case that overflows it.

### 6.3 `--call-graph lbr` — ask the hardware

Intel's Last Branch Record keeps a hardware ring of recent branches; the CPU hands you a
call chain for free. Zero stack-walking cost, no compiler requirements.

The catch, per Gregg:

> "Note that LBR is usually limited in stack depth (either 8, 16, or 32 frames), so it may
> not be suitable for deep stacks or flame graph generation, as flame graphs need to walk
> to the common root for merging."

That last clause is the important one. **Flame graphs require complete stacks.** A
truncated stack cannot be merged with its siblings, so LBR-based profiles of deep code
produce a flame graph that is wrong in a way that looks right.

### 6.4 Choosing

| Situation | Use |
|---|---|
| Modern Fedora/Ubuntu/Arch, or you control the build | `fp` — cheapest, complete |
| Distro binaries without frame pointers, shallow stacks | `dwarf` |
| Shallow stacks, Intel, need near-zero overhead | `lbr` |
| **Deep Python stacks** | `fp`, and rebuild anything that lacks them |

---

## 7. Why a native profiler cannot see Python — measured

Everything above profiles *the interpreter*. Your Python program is data that the
interpreter is reading. A native profiler has no more visibility into it than a CPU
profile of `bash` has into your shell script.

Here is that stated concretely, on this machine.

**The target** — a spinning Python process, three Python frames deep:

```python
def leaf(n):
    t = 0
    for i in range(n):
        t += i * i
    return t

def middle(n):  return leaf(n)
def hot():
    while True:
        middle(20000)
```

**The observer** — macOS's `sample(1)`, the platform's native sampling profiler, at 1 ms
for 1 second against the live PID. Abridged, but the structure is untouched *(measured)*:

```
Call graph:
    843 Thread_4921888   DispatchQueue_1: com.apple.main-thread  (serial)
    + 843 start  (in dyld) + 6992
    +   843 main  (in python3.14) + 36
    +     843 pymain_main + 464
    +       843 Py_RunMain + 1136
    +         843 pymain_run_file + 72
    +           843 pymain_run_file_obj + 164
    +             843 _PyRun_AnyFileObject + 80
    +               843 _PyRun_SimpleFileObject + 256
    +                 843 pyrun_file + 164
    +                   843 run_mod + 292
    +                     843 PyEval_EvalCode + 160
    +                       843 _PyEval_Vector + 780
    +                         233 _TAIL_CALL_BINARY_OP_ADD_INT + 732,52,...
    +                         129 _TAIL_CALL_STORE_FAST + 168
    +                         ! 49 long_dealloc + 12,20,...
    +                         ! 40 _PyObject_Free + 28,100,...
    +                         ! 12 _PyObject_Free + 36
    +                         ! : 12 _tlv_get_addr  (in libdyld.dylib) + 20
    +                         96 _TAIL_CALL_FOR_ITER_RANGE + 152,72,...
    +                         79 _TAIL_CALL_BINARY_OP_MULTIPLY_INT + 128,220,...
    +                         59 _TAIL_CALL_STORE_FAST + 120
    +                         ! 18 _tlv_get_addr  (in libdyld.dylib) + 28,20,...
    +                         45 _PyEval_Vector + 780
    +                         ! 45 _TAIL_CALL_STORE_FAST + 76,112
    +                         45 _TAIL_CALL_BINARY_OP_ADD_INT + 568
    +                         ! 40 _PyObject_Malloc + 88,40,...
```

**843 samples of a program whose only interesting functions are `hot`, `middle`, and
`leaf`. None of the three appears anywhere.** The profiler is not broken; those names do
not exist as native code. `leaf` is a `PyCodeObject` and its "call stack" is a linked list
of `_PyInterpreterFrame`s in the interpreter's data segment — invisible to a stack walker
that only knows about machine frames.

### 7.1 The 3.14 twist: the wall has become a mosaic

The classic version of this complaint is *"my profile says 60% in
`_PyEval_EvalFrameDefault` and that tells me nothing"* (see
[`32-profiling.md`](32-profiling.md) §6). **On CPython 3.14 that specific symptom is
gone**, and its replacement is more interesting.

The leaf frames above are `_TAIL_CALL_BINARY_OP_ADD_INT`, `_TAIL_CALL_STORE_FAST`,
`_TAIL_CALL_FOR_ITER_RANGE`, `_TAIL_CALL_BINARY_OP_MULTIPLY_INT`. These are 3.14's
**tail-call interpreter**: each bytecode handler is a separate C function that tail-calls
the next one, so the native profiler now attributes samples **per opcode** rather than
lumping them into one giant `_PyEval_EvalFrameDefault`
(see [`20-eval-loop.md`](20-eval-loop.md) for the mechanism and
[`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md) for the opcodes).

That is a genuine improvement in resolution, and it changes what a native profile is good
for:

- ✅ **You can now read the bytecode mix off a native profile.** `BINARY_OP_ADD_INT` at
  233+45 samples and `BINARY_OP_MULTIPLY_INT` at 79 is a fair description of
  `t += i * i` — and the specialized `_INT` suffixes confirm PEP 659 specialization fired
  ([`20`](20-eval-loop.md) §PEP 659).
- ✅ **The non-opcode leaves are the real cost centres.** `long_dealloc` (49),
  `_PyObject_Free` (52 across two sites), `_PyObject_Malloc` (45) — that is the
  allocator, which is what "boxing every intermediate integer" looks like from the
  outside (see [`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md) and
  doc 21 on the allocator).
- ✅ **`_tlv_get_addr` in `libdyld.dylib` (30+ samples)** is Darwin's thread-local-storage
  accessor. TLS lookups are on the hot path because the interpreter reaches for the
  current thread state constantly. You would never have guessed that from Python.
- ❌ **You still cannot see which of your functions is slow.** Only one frame in the whole
  843 hints that a Python call even happened: the nested `_PyEval_Vector` at 45 samples.
  Three Python frames deep, and the profiler shows you *one* ambiguous marker.

> **The finding, stated plainly:** on CPython 3.14 a native profiler gives you an
> excellent view of *how the interpreter is spending cycles* and no view whatsoever of
> *what your program is doing*. Those are different questions. If you want the second
> one, you need §8 or §15.

*(Caveat, stated because the honesty rule requires it: this is macOS `sample(1)`, not
Linux `perf`. The mechanism — walk native frames, resolve symbols — is the same, and the
conclusion about Python invisibility is structural rather than platform-specific. But I
did not run `perf` here, and the exact frame attribution on a Linux build with different
compiler flags would differ in detail. See §23.)*

---

## 8. Making `perf` see Python: the trampoline

CPython ships a fix for exactly this problem on Linux
(`Doc/howto/perf_profiling.rst`). When enabled, the interpreter emits a **perf map** file
that maps synthesized code addresses to Python function names, and executes each Python
function through a small per-function **trampoline** so that the native stack contains a
frame `perf` can attribute.

Three ways to turn it on, in increasing precedence:

```bash
PYTHONPERFSUPPORT=1 perf record -F 9999 -g -o perf.data python my_script.py
perf record -F 9999 -g -o perf.data python -X perf my_script.py
```

```python
import sys
sys.activate_stack_trampoline("perf")
do_profiled_stuff()
sys.deactivate_stack_trampoline()
non_profiled_stuff()
```

The `sys` functions win over `-X`, which wins over the environment variable. The
programmatic form is the one you want in a long-lived service: **turn the trampoline on
for the 30 seconds you are profiling and off again**, rather than paying for it forever.

### 8.1 The frame-pointer dependency, again

The docs are explicit that this path assumes an interpreter built *with* frame pointers.
Without them:

> "you can still use the `perf` profiler, but the overhead will be a bit higher because
> Python needs to generate unwinding information for every Python function call on the
> fly. Additionally, `perf` will take more time to process the data because it will need
> to use the DWARF debugging information to unwind the stack and this is a slow process."

That mode is `PYTHON_PERF_JIT_SUPPORT=1` / `-X perf_jit`, and it comes with two sharp
edges the docs call out:

1. **It needs `perf` newer than v6.8** (the fix was backported to 6.7.2) — "Due to a bug
   in the `perf` tool". And: version strings lie. "some distros add some custom version
   numbers including a `-` character. This means that `perf 6.7-3` is not necessarily
   `perf 6.7.3`."
2. **You must run `perf inject` before `perf report`**, to fold the JIT information into
   `perf.data`:

```bash
perf record -F 9999 -g -k 1 --call-graph dwarf -o perf.data python -Xperf_jit my_script.py
perf inject -i perf.data --jit --output perf.jit.data
perf report -g -i perf.jit.data
```

Forget the `inject` step and you get a report full of unresolved addresses and no error
message telling you why.

### 8.2 DTrace/SystemTap static markers

CPython can be built `--with-dtrace`, adding static probe points
(`Doc/howto/instrumentation.rst`): `function__entry`, `function__return`, `line`,
`gc__start`, `gc__done`, `instance__new__start`, `import__find__load__start`, `audit`,
and others.

Two things to know:

- The markers are guarded. Every `PyDTrace_X()` call must be preceded by
  `PyDTrace_X_ENABLED()`, "so that Python can minimize performance impact when probing is
  disabled", and on builds without DTrace they compile to nothing.
- **Almost no distributed build enables it.** This machine's interpreter reports
  `WITH_DTRACE: 0` *(measured)*. Neither do most distro packages. If you plan to use
  these markers, you are planning to build CPython
  (`13-cpython-source-map.md`).

`function__entry`/`function__return` are *tracing*, not sampling — they fire on every
call. §2.3's cost model applies with full force. `gc__start`/`gc__done` are a different
matter: GC cycles are rare and expensive, which makes them the ideal tracing target
([`22-garbage-collection.md`](22-garbage-collection.md)).

---

## 9. Tracing: tracepoints, kprobes, uprobes, USDT

Four ways to make a specific event observable. They differ in who placed the probe point
and what it costs when it fires.

### 9.1 Static kernel tracepoints

Hardcoded by kernel developers at semantically meaningful places: `sched:sched_switch`,
`syscalls:sys_enter_read`, `block:block_rq_issue`. Implemented as a patched-in nop that
becomes a call when enabled, so **disabled cost is effectively zero**.

They are also the only probes with a **stable-ish interface**. A kprobe on an internal
function breaks when someone renames the function; a tracepoint is closer to a contract.

**Prefer tracepoints when one exists.**

### 9.2 kprobes — dynamic kernel instrumentation

From `Documentation/trace/kprobes.rst`:

> "When a kprobe is registered, Kprobes makes a copy of the probed instruction and
> replaces the first byte(s) of the probed instruction with a breakpoint instruction
> (e.g., int3 on i386 and x86_64). When a CPU hits the breakpoint instruction, a trap
> occurs, the CPU's registers are saved, and control passes to Kprobes… Next, Kprobes
> single-steps its copy of the probed instruction."

Note the design detail and *why* it is there: it single-steps a **copy**, not the original.

> "(It would be simpler to single-step the actual instruction in place, but then Kprobes
> would have to temporarily remove the breakpoint instruction. This would open a small
> time window when another CPU could sail right past the probepoint.)"

That parenthetical is a small masterclass in concurrent-systems reasoning, and it is the
same shape of argument as everything in
[`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md): the naive fix opens a
window, and on a multiprocessor a window is a bug.

**Jump optimization.** With `CONFIG_OPTPROBES=y`, "Kprobes tries to reduce probe-hit
overhead by using a jump instruction instead of a breakpoint instruction at each
probepoint" — trading a full trap for a jump. Controllable at runtime via
`debug.kprobes_optimization`. Note the documented behavioural difference: an optimized
kprobe's `pre_handler` can no longer redirect execution by modifying `regs->ip`.

### 9.3 uprobes — the same trick in userspace

`Documentation/trace/uprobetracer.rst` describes the userspace equivalent: a probe placed
at a file offset in a binary or library, which traps into the kernel when hit. Same
breakpoint mechanism, but now every hit is a **user→kernel→user round trip** (see
[`09-syscalls-and-io.md`](09-syscalls-and-io.md) §2 for what that costs).

For Python this is a tempting and usually bad idea. You *can* uprobe
`_PyEval_EvalFrameDefault`, or `PyObject_Malloc`. But the hit rate is millions per second,
and at a trap per hit you have built a program that measures itself into a coma.

**uprobes are for rare events in hot processes**, not hot events. Probe
`PyErr_SetObject`, not `PyObject_Malloc`.

### 9.4 USDT — static probes in userspace

User Statically-Defined Tracing: probe points compiled into the binary as nops that a
tracer rewrites when it attaches. Nearly free when disabled. CPython's `--with-dtrace`
markers (§8.2) are exactly this.

### 9.5 The summary you should carry

```
       placed by         cost when off     cost when on      stability
────────────────────────────────────────────────────────────────────────
tracepoint  kernel devs      ~0             tens of ns        good
USDT        app devs         ~0             tens of ns        good
kprobe      you              0              ~100ns–µs         none
uprobe      you              0              µs (trap)         none
```

---

## 10. ftrace: the tracer that is already in your kernel

ftrace is built into the kernel and driven entirely through a filesystem —
`/sys/kernel/tracing` (tracefs). No tools to install; it works on a box you are not
allowed to install anything on, which is more often than you would like.

The interface is `echo` and `cat` (`Documentation/trace/ftrace.rst`):

```bash
# researched syntax, per the kernel's ftrace.rst; not run on this machine
cd /sys/kernel/tracing
cat available_tracers                  # nop function function_graph wakeup_rt ...
echo function_graph > current_tracer
echo 'py_*' > set_ftrace_filter        # restrict, or you will trace the whole kernel
echo 1 > tracing_on
cat trace_pipe                          # consuming read
echo 0 > tracing_on
```

Three things worth internalising:

**`set_ftrace_filter` is not optional.** The `function` tracer traces *every* kernel
function. On a busy machine, unfiltered, the ring buffer wraps before you can read it and
the overhead is severe. `set_ftrace_notrace` is the inverse; the docs' own example
excludes `'*preempt*' '*lock*'` for exactly this reason.

**`function_graph` gives you durations, not just events** — entry and exit, so you get
nested call timing. It is the closest thing to a kernel-side flame graph you can get
without eBPF.

**The latency tracers are underrated.** `wakeup_rt` traces the highest-priority task
system-wide, from wakeup to running:

```
  <idle>-0  3dNs7   0us :   0:120:R + [003] 312:100:R kworker/3:1H
  <idle>-0  3dNs7   1us+: ttwu_do_activate <-try_to_wake_up
  <idle>-0  3d..3  15us : __schedule <-schedule
  <idle>-0  3d..3  15us :   0:120:R ==> [003] 312:100:R kworker/3:1H
```

"took just 15 microseconds from the time it woke up, to the time it ran" — that quantity
is **scheduler wakeup latency**, and it is the thing you are actually chasing when an
async Python service has good CPU numbers and bad p99
([`29-async-patterns-and-pitfalls.md`](29-async-patterns-and-pitfalls.md)).

---

## 11. eBPF and `bpftrace`: aggregate in the kernel, not in your terminal

eBPF is the answer to tracing's central problem. Every tracer before it shipped **events**
to userspace, so cost scaled with event count. eBPF lets you attach a verified,
JIT-compiled program to a probe point that runs **in kernel context** and writes into a
**map** — a histogram, a counter, a stack-count table. Only the summary crosses to
userspace.

This changes the cost model qualitatively. Tracing a million events per second and
printing them is impossible. Tracing a million events per second and incrementing a
histogram bucket is routine.

`bpftrace` is the high-level language over it. Its probe types
(`bpftrace(8)`) map onto §9: `kprobe`/`kretprobe`, `uprobe`/`uretprobe`, `tracepoint`,
`usdt`, `software`, `hardware`, plus `profile` (timed, per-CPU), `interval` (timed, once),
`watchpoint`, and `begin`/`end`.

### 11.1 Discovering what you can attach to

```bash
# researched syntax, per bpftrace(8); not run on this machine
bpftrace -l 'kprobe:*'
bpftrace -l 't:syscalls:*openat*'
bpftrace -l 'k:*socket*,tracepoint:syscalls:*tcp*'
bpftrace -lv 'enum cpu_usage_stat'
```

The `-p PID` flag is worth reading carefully, because its meaning changes by probe type:

> "Attach to the process with or filter actions by PID. If the process terminates,
> bpftrace will also terminate. When using USDT, uprobes, uretprobes, hardware, software,
> profile, interval, or watchpoint probes they will be attached to only this process. For
> all other probes, except begin/end, the pid will act like a predicate to filter out
> events not from that pid."

So for kprobes and tracepoints, `-p` **does not reduce the probe's cost** — the probe
still fires system-wide and your program discards non-matching events. On a busy host that
distinction is the difference between a diagnostic and an incident.

### 11.2 The one-liners that earn their keep on a Python process

```bash
# researched syntax; not run on this machine

# Which syscalls, how many? (the strace -c answer, at ~0 cost)
bpftrace -e 'tracepoint:raw_syscalls:sys_enter /pid == PID/ { @[args.id] = count(); }'

# Distribution of read() sizes — is the app doing 4-byte reads?
bpftrace -e 'tracepoint:syscalls:sys_exit_read /pid == PID/ { @ = hist(args.ret); }'

# How long is this process spending off-CPU, and in which kernel path?
bpftrace -e 'kprobe:finish_task_switch { @[kstack] = count(); }'

# Page faults by userspace stack (needs frame pointers, §6)
bpftrace -e 'software:page-faults:1 /pid == PID/ { @[ustack] = count(); }'
```

The last two produce **stack-keyed maps**, which is the raw material a flame graph is
folded from (§13). Note the frame-pointer dependency propagating all the way up here:
`ustack` on a Python built without frame pointers gives you garbage, and eBPF has no
DWARF-unwinding escape hatch the way `perf record --call-graph dwarf` does.

### 11.3 What eBPF will not do for you

It runs in the kernel and understands kernel data structures. **It does not understand
`PyObject`.** `ustack` gives you the same C-frame view §7 measured. Tools that show Python
stacks from eBPF do it by *also* reading interpreter state and decoding it — the §15
mechanism, wearing an eBPF hat.

---

## 12. `strace`: the 62× tool

`strace` uses `ptrace(2)` to attach to a process and stop it at every system call. It is
the most-reached-for tool in this document and the one whose cost is least understood.

### 12.1 Why it costs what it costs

Per `ptrace(2)`, a traced process stops **twice per syscall** — syscall-enter-stop and
syscall-exit-stop — each observed by the tracer via `waitpid(2)`:

> "syscall-stops happen very often (twice per system call), and performing
> `PTRACE_GETSIGINFO` for every syscall-stop may be somewhat expensive."

Each stop is a full context switch to the tracer process, work in the tracer, and a
context switch back. Compare that to the ~81 ns trap floor measured in
[`09-syscalls-and-io.md`](09-syscalls-and-io.md) §2 and the ratio is obvious before you
measure it.

Gregg measured it on a syscall-heavy `dd`:

| Run | Result |
|---|---|
| `dd` alone | 5.2 GB copied, **1.5 GB/s** |
| under `perf trace` | ~2.5× slower |
| under `strace -c` | 5.2 GB copied in 218.9 s, **23.9 MB/s** — **62× slower** |

> "With perf, the program ran 2.5x slower. But with strace, it ran 62x slower. That's
> likely to be a worst-case result: if syscalls are not so frequent, the difference
> between the tools will not be as great." — Gregg, *Linux perf Examples*

62× is not "some overhead". **It is a different program.** Any latency measurement taken
under `strace` is a measurement of `strace`.

### 12.2 The attach hazard nobody mentions

`ptrace(2)` documents two attach modes, and a design bug in the older one:

> "`PTRACE_ATTACH` sends `SIGSTOP` to this thread. If the tracer wants this `SIGSTOP` to
> have no effect, it needs to suppress it… The design bug here is that a ptrace attach and
> a concurrently delivered `SIGSTOP` may race and the concurrent `SIGSTOP` may be lost."

And:

> "Since attaching sends `SIGSTOP` and the tracer usually suppresses it, this may cause a
> stray `EINTR` return from the currently executing system call in the tracee."

Read that twice. **Attaching `strace` to a production process can cause a syscall in that
process to return `EINTR`.** If the application's retry logic is imperfect — and in
Python, if a C extension does not handle `EINTR`, or if code predates PEP 475 semantics —
attaching your diagnostic tool has just injected a fault. This connects directly to
[`10-signals-fork-exec.md`](10-signals-fork-exec.md); the signal machinery you learned
there is the machinery `strace` is standing on.

`PTRACE_SEIZE` is the modern replacement that does not send `SIGSTOP`.

### 12.3 When `strace` is nonetheless the right tool

It is unbeatable for **low-frequency, high-confusion** questions:

```bash
# researched syntax, per strace(1); not run on this machine
strace -f -e trace=openat,stat -p $PID     # which config file is it actually reading?
strace -f -e trace=connect -p $PID          # who is it talking to?
strace -c -p $PID                            # syscall count summary
strace -k -e trace=openat -p $PID            # with stack traces
```

"Why does import take 8 seconds" is an `openat` question, and `strace -f -e trace=openat`
answers it in one line where a profiler shows a smear across `importlib`.

**The rule:** `strace` for *what happened at all*, never for *how long it took*. For the
latter, `perf trace` (same information, ring-buffer delivery, no ptrace stops).

---

## 13. Flame graphs: what the axes mean and what they cannot show

A flame graph is a visualization of stack-keyed counts. Every sampling tool in this
document produces exactly that data structure; the flame graph is just how you look at it.

### 13.1 The axes

- **y-axis: stack depth.** Bottom = root, top = leaf. (Icicle graphs invert it. Gregg:
  "I don't have a strong opinion about this, do whichever you prefer! Preferably include
  a toggle.")
- **x-axis: NOT time.** It is the merged, **alphabetically sorted** set of samples. Width
  = fraction of samples containing that frame.

The x-axis point is the one everyone gets wrong, and Gregg's account of *why* it is
alphabetical is the clearest justification:

> "I switched to timed sampling (profiling) to solve the overhead problem, but since the
> function flow is no longer known (sampling has gaps) I ditched time on the x-axis and
> reordered samples to maximize frame merging. It worked, the final visualization was much
> more readable."

Sorting alphabetically maximizes merging, which is what turns a wall of samples into
readable plateaus. **You cannot read a sequence off a flame graph.** If you need
sequence, you want a **flame chart** — the Chrome DevTools variant, which does put time on
the x-axis, and correspondingly cannot merge.

### 13.2 Reading one

1. **Look for width, not height.** A tall narrow tower is deep recursion, not a problem. A
   wide plateau is where time went.
2. **Read the top edge.** The leaf frames are what was actually executing.
3. **The bottom is your entry points.** If one is unexpectedly wide, you have a caller
   problem, not a callee problem.

### 13.3 What a flame graph structurally cannot contain

- **Off-CPU time** — unless it is specifically an off-CPU flame graph (§14).
- **Sequence** — see above.
- **Anything with a truncated stack** — LBR-depth-limited or DWARF-window-truncated stacks
  cannot merge to a common root (§6.3), so they fragment into misleading slivers.
- **Python frames**, unless produced by a Python-aware tool (§7, §8, §15).

### 13.4 Differential flame graphs

Two profiles, one image, colored by delta: red = grew, blue = shrank. This is the correct
tool for "we deployed and it got slower" and it is dramatically better than staring at two
flame graphs side by side, because the human eye is bad at diffing area.

---

## 14. On-CPU vs off-CPU: the time your profile does not contain

This is the most important section in this document.

Gregg's demonstration is one command:

```
$ time tar cf archive.tar linux-4.15-rc2

real    0m50.798s
user    0m1.048s
sys     0m11.627s
```

> "tar took about one minute to run, but the time command shows it only spent 1.0 seconds
> of user-mode CPU time, and 11.6 seconds of kernel-mode CPU time, out of a total 50.8
> seconds of elapsed time. **We are missing 38.2 seconds!**"

A CPU profile of that `tar` would sample 12.7 seconds of a 50.8-second problem. It would
be a perfectly accurate profile and a completely useless one. **75% of the answer is in
the time the process was not running.**

Substitute your Python service. If `real` ≫ `user + sys`, no amount of CPU profiling will
help you, and every minute spent staring at a flame graph is wasted.

### 14.1 Getting off-CPU time

- **Free, coarse:** `time`, then `/proc/[pid]/status` voluntary vs nonvoluntary context
  switches (§3.2), then `/proc/[pid]/schedstat` field 2 for runqueue latency (§3.3).
- **Histogram:** the bcc tool `cpudist -O -p PID` measures off-CPU time distribution by
  instrumenting `finish_task_switch()` with a kprobe, taking a timestamp
  (`bpf_ktime_get_ns()`) and PID (`bpf_get_current_pid_tgid()`), storing buckets in an
  eBPF map. Requires Linux 4.4+.
- **`perf sched timehist`** (Linux 4.10+) gives per-event `wait time`, `sch delay`, and
  `run time` columns. Gregg notes it "costs more overhead to measure than the eBPF
  histogram summary" — it ships every event to userspace, the §11 problem.

### 14.2 Why the histogram is not enough

> "Measuring off-CPU times as a histogram is a little bit useful, but not a lot. What we
> really want to know is context – *why* are threads blocking and going off-CPU."

Hence **off-CPU flame graphs**: capture the *stack* at the moment the thread blocks, and
weight it by the blocked duration. Same visualization, different weight. Now the wide
plateaus are "where we wait" instead of "where we compute".

For a Python service, an off-CPU flame graph is where you finally see:

- the GIL — threads blocked in `take_gil` / futex waits ([`24-the-gil.md`](24-the-gil.md))
- the connection pool — threads blocked acquiring a semaphore
- the event loop — blocked in `kevent`/`epoll_wait`, which is *healthy* and must be
  filtered out or it will dominate everything
  ([`28-asyncio-internals.md`](28-asyncio-internals.md))

That last point is the practical trap: **an idle async server's off-CPU profile is 100%
`epoll_wait` and tells you nothing.** Off-CPU profiling requires you to know what
*legitimate* waiting looks like before the illegitimate kind is visible.

### 14.3 Off-wake and chain graphs

Off-CPU tells you where a thread blocked. It does not tell you *who unblocked it*, which
is the actual answer when the problem is a lock hold time in another thread.

Gregg's **wakeup profiling** captures the stack of the waker; **off-wake profiling**
merges the blocked stack with the waker stack into one flame graph — blocked stack
growing up, waker stack growing down, meeting in the middle. **Chain graphs** extend it
through multiple wakeup hops.

For a GIL-bound multithreaded Python program this is the definitive picture: it shows
which thread's work is making the other threads wait — the one question
[`24-the-gil.md`](24-the-gil.md) and [`26-free-threading.md`](26-free-threading.md) are
both ultimately about.

---

## 15. Observing CPython from outside: PEP 768 and the unwinder — measured

Everything so far sees C. To see Python from outside the process, something must decode
the interpreter's data structures. There are two ways, and 3.14 changed both.

### 15.1 The old way: read memory, guess offsets

py-spy, the established tool, "works by directly reading the memory of the python program
using the `process_vm_readv` system call on Linux, the `vm_read` call on OSX or the
`ReadProcessMemory` call on Windows" (py-spy README). It then walks
`PyInterpreterState` → `PyThreadState` → frames → `PyCodeObject` and reconstructs Python
stacks.

This is impressive and inherently fragile: it depends on struct layouts that are CPython
implementation details, so every release risks breaking it. It also reads memory that is
being mutated concurrently, so it can observe torn state. (py-spy pauses the process by
default and offers `--nonblocking` to skip that, trading accuracy for non-intrusiveness.)

Permissions bite here immediately:

> "py-spy needs `SYS_PTRACE` to be able to read process memory. **Kubernetes drops that
> capability by default**"

— fixed by adding `SYS_PTRACE` to the container's `securityContext.capabilities`, which
is a deployment change, which means you cannot do it during the incident. **Add it
before you need it.** (§17.)

### 15.2 The new way: PEP 768

Python 3.12 introduced a **debug offsets table** at the start of `PyRuntime`, so external
tools can locate runtime structures "regardless of ASLR or how Python was compiled". PEP
768 (3.14) extends it with a `_debugger_support` struct:

```c
struct _debugger_support {
    uint64_t eval_breaker;              // Location of the eval breaker flag
    uint64_t remote_debugger_support;   // Offset to our support structure
    uint64_t debugger_pending_call;     // Where to write the pending flag
    uint64_t debugger_script_path;      // Where to write the script path
    uint64_t debugger_script_path_size; // Size of the script path buffer
};
```

The protocol: an external tool writes a script path into the target's memory and sets the
pending flag plus the **eval breaker** bit. The interpreter notices at its next safe point
— the same eval-breaker check that handles signals and GIL drops, see
[`20-eval-loop.md`](20-eval-loop.md) and
[`10-signals-fork-exec.md`](10-signals-fork-exec.md) — and executes the script.

**This is why it is "zero-overhead"**: no new check is added to the hot path. It reuses a
check the interpreter was already performing. Compare `sys.settrace`, which makes every
Python call slower forever.

Exposed as `sys.remote_exec(pid, script_path)`, with consumers already in the stdlib:

```bash
python -m pdb -p 1234              # attach a debugger to a running process
python -m asyncio ps 1234          # flat table of running asyncio tasks
python -m asyncio pstree 1234      # the async call tree
```

`asyncio pstree` is the tool [`29-async-patterns-and-pitfalls.md`](29-async-patterns-and-pitfalls.md)
has been waiting for: "particularly useful for debugging long-running or stuck
asynchronous programs… quickly identify where a program is blocked, what tasks are
pending, and how coroutines are chained together."

**The safe-point caveat, from the 3.14 release notes, matters operationally:**

> "due to how the Python interpreter works attaching to a remote process that is blocked
> in a system call or waiting for I/O will only work once the next bytecode instruction is
> executed or when the process receives a signal."

So the tool is weakest exactly where you need it most. **A process wedged in a blocking
syscall cannot be introspected this way** — it will never reach a safe point. For that
case you are back to §3 (`/proc/[pid]/stack`, `wchan`, `D` state) and §12.

### 15.3 What 3.14 actually ships, and what it does not

*(measured on this machine, CPython 3.14.6)*

```
sys.remote_exec:              True
sys.activate_stack_trampoline: True
_remote_debugging exports:    RemoteUnwinder, FrameInfo, ThreadInfo,
                              TaskInfo, CoroInfo, AwaitedInfo,
                              PROCESS_VM_READV_SUPPORTED
```

`_remote_debugging.RemoteUnwinder` is the engine: a py-spy-style external unwinder, now
maintained *inside* CPython, so the struct offsets can never drift out of sync with the
interpreter. `python -m pdb -p` and `python -m asyncio pstree` are its clients.

**Note what is not here.** The high-frequency statistical sampling profiler built on this
foundation — *Tachyon*, `profiling.sampling` — is a **3.15** feature (Galindo and Kiss
Kollár, gh-135953 / gh-138122). On 3.14.6, `python -m profile.sample` does not exist
*(measured: `ModuleNotFoundError`)*. 3.14 ships the mechanism; 3.15 ships the profiler.

### 15.4 The Darwin wall — a negative result

Attempting to use `RemoteUnwinder` against a live Python process owned by the same user,
on this machine *(measured)*:

```
PermissionError: Cannot get task port for PID 25346 (kern_return_t: 5).
This typically requires running as root or having the
'com.apple.system-task-ports' entitlement.
```

`kern_return_t: 5` is `KERN_FAILURE` from `task_for_pid()`. On Darwin, reading another
process's memory requires a **Mach task port**, and macOS refuses to hand one out to an
unprivileged process even for a same-user target — a deliberate hardening decision, not a
CPython limitation.

**Consequence:** on macOS, PEP 768 introspection, py-spy, and every external unwinder
require `sudo` (or a signed binary carrying the entitlement). The feature is
cross-platform; the *ergonomics* are not. On Linux the equivalent gate is
`ptrace_scope`/`SYS_PTRACE` (§17), which is at least configurable per-container.

---

## 16. Darwin: what you actually have — measured

If you develop on a Mac and deploy on Linux — the overwhelmingly common case — you need
to know that **your local observability toolkit is not a subset of production's; it is a
different toolkit with different gates.**

*(All of this section is measured on this machine: macOS 26.5.2, arm64.)*

### 16.1 SIP takes DTrace away

macOS inherited DTrace from Solaris, and on paper it is superb. In practice:

```
$ csrutil status
System Integrity Protection status: enabled.

$ dtrace -n 'profile-99 { @[execname] = count(); }' -c "python3.14 -c '...'"
dtrace: system integrity protection is on, some features will not be available
dtrace: failed to initialize dtrace: DTrace requires additional privileges
```

That was DTrace **profiling a child process it launched itself, owned by the same user**,
and it still refused. With SIP enabled — the default, and disabling it is a bad idea —
DTrace is effectively unavailable. Neither is CPython's DTrace support present in
practice: this interpreter reports `WITH_DTRACE: 0`.

### 16.2 The tools that exist, and their gates

| Tool | Linux analogue | Works unprivileged here? |
|---|---|---|
| `sample(1)` | `perf record -g` | **Yes** *(measured — §7)* |
| `spindump(8)` | `perf sched` + off-CPU | No — needs root *(measured)* |
| `fs_usage(1)` | `strace -e trace=file` | No — needs root *(measured)* |
| `dtrace(1)` | bpftrace | No — SIP *(measured)* |
| `ktrace(1)` | ftrace / trace-cmd | No — needs root |
| `vmmap`, `heap`, `leaks`, `footprint` | `/proc/pid/smaps`, valgrind | Yes (same user) |
| `powermetrics` | RAPL / `turbostat` | No — needs root |
| `xctrace` (Instruments) | — | Yes, with a GUI-adjacent workflow |

**The honest summary: the one tool you can use freely on macOS is `sample`, and §7 showed
exactly how much Python it can see (none).** For Python-level observation on a Mac you use
Python-level tools — `cProfile`, `sys.monitoring`, and in 3.15+ `profiling.sampling` — or
you run `sudo`.

### 16.3 Why this matters beyond convenience

The tooling asymmetry produces a specific, recurring failure: **engineers develop a mental
model of their service's performance using tools that cannot see off-CPU time or kernel
behaviour, and then deploy into an environment where those are the dominant terms.** The
service is I/O-bound and lock-bound in production and CPU-bound on the laptop, because the
laptop has no network latency, no noisy neighbours, and no cgroup quota.

The mitigation is not "get a Linux laptop". It is: **do the on-CPU work locally, and do
the off-CPU work in an environment that resembles production**, with the §14 tools. The
methods (§18–§21) transfer; the tools do not.

---

## 17. Permissions: the wall you will actually hit

The tool you need will be blocked. Knowing the gate by name is what turns a two-day
outage into a two-minute `securityContext` edit.

### 17.1 `perf_event_paranoid`

`/proc/sys/kernel/perf_event_paranoid` controls unprivileged access to perf events. Per
the kernel's `admin-guide/sysctl/kernel.rst`:

| Value | Effect |
|---|---|
| `-1` | Allow (almost) all events by all users; ignore the mlock limit without `CAP_IPC_LOCK` |
| `>= 0` | Disallow raw and ftrace-function tracepoint access |
| `>= 1` | Disallow CPU event access |
| `>= 2` | Disallow kernel profiling |

**Default is 2.** Which means: out of the box, an unprivileged user can profile their own
processes' userspace, and nothing else. Most "perf doesn't work" reports are this.

Some distributions (notably Debian/Ubuntu derivatives) carry a patch adding a stricter
level that denies `perf_event_open` to unprivileged users entirely.

`perf_event_open(2)` notes the check for whether the interface exists at all: "The
official way of knowing if `perf_event_open()` support is enabled is checking for the
existence of the file `/proc/sys/kernel/perf_event_paranoid`."

### 17.2 `CAP_PERFMON` — use this, not `CAP_SYS_ADMIN`

> "`CAP_PERFMON` capability (since Linux 5.8) provides secure approach to performance
> monitoring and observability operations in a system according to the principal of least
> privilege… Accessing system performance monitoring and observability operations using
> `CAP_PERFMON` rather than the much more powerful `CAP_SYS_ADMIN` excludes chances to
> misuse credentials and makes operations more secure. `CAP_SYS_ADMIN` usage for secure
> system performance monitoring and observability is discouraged in favor of the
> `CAP_PERFMON` capability." — `perf_event_open(2)`

`CAP_SYS_ADMIN` is famously "root by another name". If someone proposes granting it to
enable profiling, `CAP_PERFMON` is the correct counter-proposal.

### 17.3 The container checklist

Write this down before the incident, not during it:

| Symptom | Gate | Fix |
|---|---|---|
| `perf` sees no kernel symbols | `perf_event_paranoid >= 2`, `kptr_restrict` | lower on the **host** (it is not namespaced) |
| py-spy / `pdb -p`: permission denied | `SYS_PTRACE` dropped (K8s default) | add `SYS_PTRACE` to `securityContext.capabilities` |
| `perf` shows addresses, not names | no symbols in the container image | ship a debug image, or `perf buildid-cache` |
| bpftrace: cannot attach | needs `CAP_BPF`+`CAP_PERFMON` (or privileged), and host kernel headers/BTF | run the tracer on the **host**, targeting the container's PID |
| `/sys/kernel/tracing` missing | tracefs not mounted in the container | trace from the host |
| macOS: cannot read target memory | Mach task port (§15.4) | `sudo`, or entitled binary |

**The structural point:** most kernel-level observability is a *host* capability, not a
container capability. The right architecture is usually a privileged tracing daemon on the
node that can target any container's PIDs, not per-container tracing permissions.

---

## 18. The USE method applied to one Python process

Gregg's USE method:

> "**For every resource, check utilization, saturation, and errors.**"
>
> - **resource**: all physical server functional components (CPUs, disks, busses, ...)
> - **utilization**: the average time that the resource was busy servicing work
> - **saturation**: the degree to which the resource has extra work which it can't
>   service, often queued
> - **errors**: the count of error events

It is designed for systems. Applied to a single Python process, with the process's
*software* resources treated as resources, it becomes a checklist you can run in five
minutes. Errors first — Gregg notes they are "usually quicker and easier to interpret".

| Resource | Utilization | Saturation | Errors |
|---|---|---|---|
| **CPU** | `utime+stime` delta ÷ wall (`/proc/pid/stat` 14,15) | `schedstat` field 2 (runqueue ns); `nonvoluntary_ctxt_switches` | — |
| **Memory** | `VmRSS`, `Pss` (`smaps_rollup`) | `majflt` growth; swap; cgroup `memory.events` | `oom_kill` in `memory.events` |
| **Disk I/O** | `/proc/pid/io` `read_bytes`/`write_bytes` | threads in `D` state; delay accounting blkio | — |
| **Network** | socket byte counters | `send-q`/`recv-q` depth (`ss -tin`) | retransmits, `ECONNRESET` |
| **The GIL** | % of wall time some thread holds it | threads blocked in `take_gil` (off-CPU, §14) | — |
| **Thread pool** | busy workers ÷ `max_workers` | queue depth | task rejections |
| **Connection pool** | checked-out ÷ pool size | waiters; `pool_timeout` waits | pool-timeout exceptions |
| **asyncio loop** | loop iteration time ÷ wall | callback queue depth; `slow callback` warnings | unhandled task exceptions |
| **File descriptors** | `ls /proc/pid/fd \| wc -l` ÷ `RLIMIT_NOFILE` | — | `EMFILE` |

### 18.1 The three rows that are Python-specific and matter most

**The GIL row is the one nobody instruments and everybody needs.** A Python process can
show 100% of one core, ~0% of the other ten, and a runqueue full of its own threads. CPU
"utilization" says the machine is idle; the GIL is at 100%. See
[`24-the-gil.md`](24-the-gil.md) for measurement approaches, and note that py-spy's
GIL-holding-thread filter exists precisely for this.

**Pool saturation is where p99 comes from.** A connection pool at 100% utilization with
waiters is Little's Law arriving on schedule — the same phenomenon measured in
[`29-async-patterns-and-pitfalls.md`](29-async-patterns-and-pitfalls.md), where a bounded
queue moved identical work in identical wall time while p99 went 1127 ms → 3.9 ms.

**The event-loop row catches the classic async bug**: one synchronous call inside a
coroutine. Loop utilization looks fine on average; the *saturation* metric (queue depth
during the stall) is where it shows up.

### 18.2 Gregg's caveat

> "systems can be suffering more than one performance problem, and so the first one you
> find may be *a* problem but not *the* problem."

USE finds bottlenecks. It does not rank them. Confirm with the §21 workflow before you
change anything.

---

## 19. The RED method applied to one Python service

USE covers resources. It does not cover *requests*. Tom Wilkie's RED method
(GrafanaCon EU 2018) exists for that gap:

> "The USE Method doesn't really apply to services; it applies to hardware, network disks,
> things like this. We really wanted a microservices-oriented monitoring philosophy, so we
> came up with the RED Method."

**For every service, monitor:**

- **Rate** — requests per second
- **Errors** — how many of those are failing
- **Duration** — the distribution of how long they take

> "The RED Method is a good proxy to how happy your customers will be."

Google's **Four Golden Signals** (Latency, Traffic, Errors, Saturation) is the same idea
plus saturation — which is USE's word, and the bridge between the two methods.

### 19.1 The three implementation errors, in Python services specifically

**1. Averaging the duration.** A mean latency is a number that describes no request. You
need a histogram; you need p50/p95/p99; and you need to know that
**percentiles do not average across instances** — you cannot mean three pods' p99s and get
the fleet p99. Aggregate the histogram buckets, then compute the percentile.

**2. Not counting the failures in the duration.** Errors are often *fast* (a validation
rejection returns in 2 ms). If your duration metric only covers successes, an incident
where 60% of requests fail instantly will make your latency dashboard look **better**.
Record duration for every request, labelled by outcome.

**3. Measuring at the wrong boundary.** A Python web app measuring latency inside the view
function misses time spent queued in front of the workers — which, under saturation, is
*all* of the latency. Measure at the outermost boundary you control, and separately record
the queue-wait time (gunicorn, uvicorn, and most ASGI servers can emit it).

### 19.2 How RED and USE compose

```
RED says:  "p99 tripled at 14:00."          ← the symptom, user-visible
USE says:  "GIL saturation, 40 threads      ← the cause, in one process
            blocked in take_gil"
§14 says:  "here is the off-wake graph      ← the mechanism, in one function
            showing which call holds it"
```

That is the whole discipline in three lines. RED tells you *that*; USE tells you *where*;
the tracing tools tell you *why*. Skipping a level is how investigations go sideways for
two days.

---

## 20. Is it cgroup throttling? A decision procedure

This deserves its own section because it is the single most common "the process is slow
and every profile looks fine" cause in containerized Python, and because it is
**diagnosable in one file read**.

The mechanism: cgroup v2's `cpu.max` gives a container a quota per period (default period
100 ms). When the container exhausts its quota, **every thread is descheduled until the
next period begins** — up to 100 ms of dead stop, invisible to any CPU profiler, because
during the stall your process is not running and therefore not being sampled.

```bash
# researched; cgroup v2 interface
cat /sys/fs/cgroup/cpu.stat
```

```
nr_periods 41283
nr_throttled 3129
throttled_usec 84213000
```

**The decision procedure:**

1. `nr_throttled` is 0 and stays 0 → not throttling. Move on.
2. `nr_throttled / nr_periods` is a few percent → you are being throttled sometimes.
   Correlate the timestamps against your p99 spikes before concluding anything.
3. `throttled_usec` growing by ~tens of ms per second → **this is your latency.** Stop
   profiling.

The counter-intuitive part, and the reason people miss it: **average CPU utilization can
be low while throttling is severe.** A container with a 0.5-CPU quota that does its work
in bursts will show 20% average utilization and be throttled hard during every burst. The
dashboard says "over-provisioned"; the users say "slow". Both are correct.

The fix is usually *not* more quota. It is fewer threads. A Python process that starts
`os.cpu_count()` workers inside a 0.5-CPU container has read the **host's** core count —
a classic bug that [`06-processes-threads-scheduling.md`](06-processes-threads-scheduling.md)
covers, and one reason `os.process_cpu_count()` exists.

Also read `cpu.pressure` (PSI): `some avg10` tells you what fraction of the last 10
seconds *some* task was stalled waiting for CPU, which is saturation in a single number.

---

## 21. The observation session, end to end

A worked procedure. The ordering is the point.

### Phase 0 — Before you touch a tool (2 minutes)

Write down: **what is the symptom, in user-visible terms, and when did it start?** If you
cannot state it, you will confirm whatever you already believe. And: **is this on-CPU or
off-CPU?** — `time`, or `utime+stime` vs wall clock from `/proc/pid/stat` (§14).

### Phase 1 — Free counters (2 minutes, zero risk)

```bash
cat /proc/$PID/status        # state, VmRSS, VmHWM, ctxt switches
cat /proc/$PID/schedstat     # runqueue latency  ← check this
cat /proc/$PID/io            # syscall bytes vs block-layer bytes
cat /sys/fs/cgroup/cpu.stat  # nr_throttled      ← and this
ls /proc/$PID/task | wc -l   # thread count
```

**Decision point.** `nr_throttled` growing → §20, stop. `schedstat[1]` large → you are
queued, not slow; the problem is capacity or thread count. `nonvoluntary` ≫ `voluntary`
→ CPU contention. `voluntary` ≫ `nonvoluntary` → you are blocking; go to Phase 3.

### Phase 2 — On-CPU, if the process is actually on-CPU (5 minutes, ~1%)

```bash
perf stat -e cycles,instructions,cache-misses,branch-misses -p $PID -- sleep 10
perf record -F 99 -g -p $PID -- sleep 30
perf report -g
```

With the trampoline on (§8), or with py-spy / `profiling.sampling` (§15) for Python-level
frames. **Check the `perf stat` multiplexing percentages** (§4.2) before believing any
counter.

### Phase 3 — Off-CPU, which is usually where the answer is (10 minutes, ~1%)

```bash
bpftrace -e 'kprobe:finish_task_switch { @[comm, kstack] = count(); }'
```

then an off-CPU flame graph, then off-wake if a lock is implicated (§14.3). Filter out
legitimate waiting (`epoll_wait`, `kevent`, idle pool threads) *first*, or it will
dominate.

### Phase 4 — Targeted tracing (only now, only narrow)

You should by this point have a specific hypothesis naming a specific function or syscall.
Trace *that*:

```bash
bpftrace -e 'tracepoint:syscalls:sys_enter_openat /pid == PID/ { @[str(args.filename)] = count(); }'
strace -f -e trace=connect -p $PID          # low-frequency events only
```

**Never open a tracer without a hypothesis.** That is how you get a 62× slowdown and a
gigabyte of output containing no answer.

### Phase 5 — Python-level introspection

```bash
python -m asyncio pstree $PID    # what are the tasks doing?
python -m pdb -p $PID            # what is this thread doing, exactly?
py-spy dump --pid $PID           # stacks for every thread
```

Remember §15.2: if the process is wedged in a blocking syscall, PEP 768 tools will **hang
waiting for a safe point that never arrives**. That hang is itself diagnostic — it tells
you the process is not executing bytecode, which sends you back to `wchan` and `D` state.

### Phase 6 — Write down what you found and what you ruled out

Including the negative results. "Not throttling; `nr_throttled` flat at 0 across the
incident window" is a real finding that saves the next person an hour.

---

## 22. A review checklist

When someone brings you a performance investigation, ask:

- [ ] **Which of the four mechanisms produced this data?** (§2) Do they know?
- [ ] **Is the problem on-CPU or off-CPU, and was that established before profiling?** (§14)
- [ ] **If it is a `perf stat` table — was any counter multiplexed?** (§4.2)
- [ ] **If it is a flame graph — could it walk the stacks?** Frame pointers? LBR
      truncation? (§6)
- [ ] **If it names Python functions — how?** Trampoline, py-spy, or PEP 768? If it does
      not name them, does the author know why? (§7)
- [ ] **If it is a duration — was it measured under `strace`?** (§12) That number is
      fiction.
- [ ] **If it is a percentile — was it averaged across instances?** (§19.1)
- [ ] **If it is a duration — does it include failed requests?** (§19.1)
- [ ] **Was `cpu.stat`'s `nr_throttled` checked?** (§20) It takes four seconds.
- [ ] **What was ruled out, and with what evidence?**

---

## 23. What I could not verify

Stated explicitly, because the alternative is implying I ran things I did not.

1. **No Linux measurements at all.** Every `perf`, `ftrace`, `bpftrace`, `strace`, eBPF,
   `/proc`, and cgroup output in this document is quoted from primary documentation or
   from Gregg's published measurements and is attributed inline. I ran none of it. The
   62× `strace` figure, the 2.5× `perf trace` figure, and the <1% frame-pointer figure are
   **Gregg's numbers on Gregg's hardware**, not mine.
2. **The §7 finding is macOS `sample(1)`, not Linux `perf`.** The structural conclusion —
   native profilers see interpreter frames, not Python frames — is platform-independent.
   The *specific* frame attribution (which `_TAIL_CALL_*` symbols appear and in what
   proportion) reflects this build: `uv`-provided CPython 3.14.6, Clang 22.1.3, arm64, no
   frame pointers. A Linux build with different flags would differ in detail. I did not
   verify that `perf` on Linux 3.14 shows the same per-opcode decomposition, though the
   tail-call interpreter is a build-configuration property rather than a platform one, so
   I expect it does.
3. **I did not quantify the perf trampoline's overhead.** The CPython docs say the
   non-frame-pointer JIT mode has "a bit higher" overhead without a number, and I have no
   Linux box to measure it on.
4. **I did not verify PEP 768 end-to-end.** §15.4's `PermissionError` means I confirmed
   the *gate*, not the *feature*. I did not run `pdb -p` or `asyncio pstree` successfully
   against a live process, and I did not test them under `sudo`.
5. **Delay accounting (§3.6) is described from the kernel docs only.** I have not used
   `getdelays` and cannot speak to its ergonomics.
6. **The `perf_event_paranoid` level table is from the kernel sysctl documentation.** I
   did not verify the behaviour of each level empirically, and I have deliberately not
   asserted precisely which distributions carry the extra stricter level, only that some
   do.
7. **The USE table's Python-specific rows (§18) are my synthesis**, not Gregg's. He
   defines the method for hardware resources; treating the GIL and a connection pool as
   "resources" is an extension. I believe it is a faithful one — he explicitly invites it
   ("It can be useful to consider some software resources as well, or software imposed
   limits") — but the specific rows are mine and are not authoritative.

---

## 24. Lab exercises

**Lab 1 — Prove the invisibility (30 min, works on any platform).**
Write a program with three nested functions, the innermost of which burns CPU. Profile it
with your platform's *native* sampler (`perf record -g` on Linux, `sample` on macOS).
Confirm none of your function names appear. Then enable the Python-aware path (`-X perf`
on Linux; py-spy or `sudo` on macOS) and confirm they do. **Write down the exact list of
frames each tool showed you.** This is §7 and it is the single most clarifying 30 minutes
in this document.

**Lab 2 — The missing time (20 min).**
Write a program that spends 1 second on CPU and 5 seconds blocked on `time.sleep`. Take a
CPU profile. Observe that it is a perfectly accurate profile of 1/6 of your program. Then
compute `utime+stime` vs wall from `/proc/self/stat` (or `resource.getrusage`) and
recover the missing 5 seconds. **This is §14 in miniature and you should be able to do it
from memory.**

**Lab 3 — Multiplexing (Linux, 20 min).**
Run `perf stat` with 3 events, then with 15. Compare the enabled percentages. Compute IPC
from the 15-event run and from the 3-event run and explain the difference. Then re-run the
15-event version with the two events grouped and show that the ratio stabilises. (§4.2–4.3)

**Lab 4 — The `strace` tax (Linux, 20 min).**
Time a syscall-heavy Python loop (e.g. 100k small `os.write`s to `/dev/null`) three ways:
alone, under `perf trace`, under `strace -c`. Reproduce the *shape* of Gregg's result. You
will not get 62× — the point is to find *your* number and never again quote a latency
measured under `strace`. (§12)

**Lab 5 — Off-CPU (Linux + bpftrace, 45 min).**
Write a program with two threads contending on a `threading.Lock`. Produce a CPU flame
graph (nearly empty — that is the finding) and then an off-CPU flame graph keyed on
`finish_task_switch` stacks. Then do the same for the GIL, with no explicit lock at all.
(§14)

**Lab 6 — Throttling (containers, 30 min).**
Run a CPU-bound Python workload in a container with `--cpus=0.5`. Watch `cpu.stat`'s
`nr_throttled` climb while a CPU profile shows nothing unusual and average utilization
looks modest. Then set the worker count from `os.process_cpu_count()` instead of
`os.cpu_count()` and watch the throttling drop. (§20)

**Lab 7 — Remote introspection (Linux, or macOS with sudo, 30 min).**
Start a long-running asyncio program with several tasks, one of them deliberately stuck.
Find it with `python -m asyncio pstree $PID`. Then make a task block in a synchronous
`time.sleep(300)` inside a coroutine and try again — observe the safe-point problem from
§15.2 first-hand. (§15)

**Lab 8 — Build the capstone (multi-day).**
The README's capstone: *write a sampling profiler that reads another process's frames.*
You now have every piece: `_remote_debugging.RemoteUnwinder` or the PEP 768 debug-offsets
protocol for the reading, §5 for the sampling discipline, §13 for folding stacks into a
flame graph, and §17 for the permissions you will hit on the first try. Start by reading
`RemoteUnwinder`'s implementation in `Modules/_remote_debugging_module.c`.

---

## 25. Question bank

Answer out loud. If you hedge, re-read the section.

1. A process takes 50 seconds of wall time and 12 seconds of CPU. You take a CPU flame
   graph. What fraction of the problem can that graph possibly contain, and how would you
   see the rest?
2. Why is the conventional profile frequency 99 Hz and not 100 Hz?
3. Your `perf stat` output shows `instructions` at 43.2% enabled. What does that mean, and
   is the IPC you computed from it trustworthy?
4. Name the three call-graph strategies `perf record` supports and give one workload where
   each is the wrong choice.
5. Frame pointers cost under 1% and were omitted by default for two decades. Give the
   argument that reversed that decision, and name two distributions that reversed it.
6. You profile a CPython 3.14 process with `perf` and the hot frames are named
   `_TAIL_CALL_STORE_FAST` and `_TAIL_CALL_BINARY_OP_ADD_INT`. What is that telling you,
   and what is it structurally unable to tell you?
7. Explain why `sys.remote_exec` is described as zero-overhead. What existing mechanism
   does it reuse, and what is the operational consequence of that reuse when the target is
   blocked in `read()`?
8. Why does `strace` cost ~62× on a syscall-heavy program while `perf trace` costs ~2.5×?
   Be specific about what happens per syscall.
9. Name the failure mode where attaching `strace` to a healthy production process can
   cause that process to malfunction.
10. A kprobe single-steps a *copy* of the probed instruction rather than the original.
    Why? What bug does the simpler design have?
11. What is the difference between `voluntary_ctxt_switches` and
    `nonvoluntary_ctxt_switches`, and what different action does each imply?
12. Which single file tells you a process's run-queue latency, and where in the USE method
    does that number belong?
13. Your service's average CPU utilization is 20% and its p99 is terrible. Give three
    distinct hypotheses and the one command that tests each.
14. Why can't you average p99 latencies across three pods to get the fleet p99? What do you
    aggregate instead?
15. Your latency dashboard *improved* during an incident where 60% of requests were
    failing. Explain.
16. `bpftrace -p PID -e 'kprobe:vfs_read {...}'` — does `-p` reduce the probe's cost?
    What about for `uprobe`?
17. Why does an eBPF `ustack` on a CPython process built without frame pointers give
    garbage, when `perf record --call-graph dwarf` would not?
18. Your container's Python process cannot be inspected by py-spy: "Permission Denied".
    Name the capability, why it is missing, and why the fix cannot be applied during the
    incident.
19. Why is `CAP_PERFMON` the correct answer when someone requests `CAP_SYS_ADMIN` for
    profiling?
20. An off-CPU flame graph of an idle async server is 100% `epoll_wait`. Is the tool
    broken? What must you do before the graph becomes useful?
21. What does an off-*wake* flame graph show that an off-CPU flame graph cannot, and why is
    that the definitive picture for a GIL problem?
22. `nr_throttled` is climbing but average CPU utilization is 20%. Explain how both are
    true, and give the fix that is *not* "raise the quota".
23. Why is the x-axis of a flame graph alphabetical? What visualization do you use when
    you need sequence, and what does it give up?
24. On macOS, why does `RemoteUnwinder` fail with `kern_return_t: 5` against a process you
    own, when the equivalent works on Linux for your own processes?
25. You must choose one number to page on for a Python web service. RED or USE? Which
    metric? Defend it.

---

## 26. Sources

**Primary — man pages (the specification)**

- [`perf_event_open(2)`](https://man7.org/linux/man-pages/man2/perf_event_open.2.html) —
  the whole PMU interface: `perf_event_attr`, event types, `sample_type`,
  `PERF_FORMAT_TOTAL_TIME_ENABLED`/`RUNNING`, the mmap ring-buffer layout, `CAP_PERFMON`.
  *Verdict: enormous and the only complete reference. Read the `sample_period`/`sample_freq`
  and `PERF_FORMAT_*` subsections in full — §4.2's multiplexing trap is entirely in there
  and almost nobody has read it. Grep the rest.*
- [`ptrace(2)`](https://man7.org/linux/man-pages/man2/ptrace.2.html) — attach/detach,
  syscall-stops, signal-delivery-stop, `PTRACE_SEIZE`.
  *Verdict: read "Attaching and detaching" and "Signal injection and suppression" before
  you ever attach a debugger to production. The admission that the `PTRACE_ATTACH`/
  `SIGSTOP` race is "a design bug" is unusually candid and is §12.2.*
- [`strace(1)`](https://man7.org/linux/man-pages/man1/strace.1.html) — *Verdict: the
  `-e trace=` expression syntax and `-k` (stack traces) are the parts worth learning;
  everything else you will look up.*
- [`proc_pid_stat(5)`](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html),
  [`proc_pid_status(5)`](https://man7.org/linux/man-pages/man5/proc_pid_status.5.html),
  [`proc_pid_smaps(5)`](https://man7.org/linux/man-pages/man5/proc_pid_smaps.5.html),
  [`proc_pid_wchan(5)`](https://man7.org/linux/man-pages/man5/proc_pid_wchan.5.html) —
  *Verdict: the split `proc_pid_*` pages are far more usable than the old monolithic
  `proc(5)`. The field-number table in `proc_pid_stat` is the one to bookmark; the
  ctxt-switch pair at the bottom of `proc_pid_status` is the highest-value two lines in
  all of procfs.*
- [`perf-record(1)`](https://man7.org/linux/man-pages/man1/perf-record.1.html) and
  [`perf-stat(1)`](https://man7.org/linux/man-pages/man1/perf-stat.1.html) — *Verdict: read
  `perf-stat`'s STAT REPORT section on top-down; its caveat that "the bottleneck is only
  the real bottleneck if the workload is actually bound by the CPU" is the most useful
  sentence in either page.*
- [`perf-trace(1)`](https://man7.org/linux/man-pages/man1/perf-trace.1.html) — *Verdict:
  short. Read it once so that you stop reaching for `strace` reflexively.*

**Kernel documentation**

- [`Documentation/trace/ftrace.rst`](https://www.kernel.org/doc/html/latest/trace/ftrace.html)
  — *Verdict: long, and the first third is all you need. The `set_ftrace_filter` /
  `set_ftrace_notrace` examples and the `wakeup_rt` latency-tracer walkthrough are the
  parts that pay off.*
- [`Documentation/trace/kprobes.rst`](https://www.kernel.org/doc/html/latest/trace/kprobes.html)
  — *Verdict: **read "How Does a Kprobe Work?" in full.** Four paragraphs, and the
  parenthetical explaining why it single-steps a copy is a better lesson in
  multiprocessor reasoning than most textbook chapters.*
- [`Documentation/trace/uprobetracer.rst`](https://www.kernel.org/doc/html/latest/trace/uprobetracer.html)
  — *Verdict: short reference for the raw tracefs interface. You will use bpftrace instead,
  but read this to understand what bpftrace is doing.*
- [`Documentation/admin-guide/perf-security.rst`](https://www.kernel.org/doc/html/latest/admin-guide/perf-security.html)
  — *Verdict: the authoritative treatment of `perf_event_paranoid` and `CAP_PERFMON`. Read
  it before arguing with a security team; it makes the least-privilege case for you.*
- [`Documentation/accounting/delay-accounting.rst`](https://www.kernel.org/doc/html/latest/accounting/delay-accounting.html)
  — *Verdict: the least-known high-value interface in the kernel. Read it for the list of
  what is tracked; the "where did the wall clock go" answer is right there.*

**Brendan Gregg — the methodology**

- [*The USE Method*](https://www.brendangregg.com/usemethod.html) — *Verdict: read the
  Summary and Strategy sections; the rest is per-OS checklists you will adapt anyway. The
  caveat that "the first one you find may be a problem but not the problem" is the part
  people skip and then regret.*
- [*Off-CPU Analysis*](https://www.brendangregg.com/offcpuanalysis.html) — *Verdict:
  **read this one in full.** The `tar` example (50.8 s wall, 12.7 s CPU) reframes
  performance work permanently, and the progression from off-CPU time → off-CPU stacks →
  off-CPU flame graphs is the correct order to learn it in.*
- [*Linux Wakeup and Off-Wake Profiling*](https://www.brendangregg.com/blog/2016-02-01/linux-wakeup-offwake-profiling.html)
  — *Verdict: the sequel to the above and the technique that finally makes lock contention
  visible end-to-end. Read after you have made one off-CPU flame graph yourself.*
- [*Flame Graphs*](https://www.brendangregg.com/flamegraphs.html) — *Verdict: read the
  Origin and Variations sections. Origin explains why the x-axis is alphabetical better
  than any tutorial; Variations tells you when you want a flame chart instead.*
- [*Linux perf Examples*](https://www.brendangregg.com/perf.html) — *Verdict: the reference
  you will actually keep open. The strace-vs-perf comparison (62× vs 2.5×) is §12's
  headline; the `perf record -vv` dump showing `sample_freq 4000` is the kind of detail no
  man page surfaces.*
- [*The Return of the Frame Pointers*](https://www.brendangregg.com/blog/2024-03-17/the-return-of-the-frame-pointers.html)
  (Mar 2024) — *Verdict: read it for the argument, not the news. The "<1% cost to make a
  500% win findable" framing transfers to every observability trade-off you will make.*
- *Systems Performance* 2e, ch. 5–6 and 13–15; *BPF Performance Tools* — the book-length
  versions. See [BOOKS.md](BOOKS.md).

**Distribution decisions**

- [Fedora: `-fno-omit-frame-pointer`](https://fedoraproject.org/wiki/Changes/fno-omit-frame-pointer)
  — *Verdict: read the Feedback section. It is a well-documented argument between
  performance engineering and micro-benchmarking, and the winning side is instructive.*
- [Ubuntu: frame pointers by default in 24.04 LTS](https://ubuntu.com/blog/ubuntu-performance-engineering-with-frame-pointers-by-default)

**Python**

- [PEP 768 — Safe external debugger interface](https://peps.python.org/pep-0768/)
  (Galindo Salgado, Wozniski) — *Verdict: **read the Specification in full.** The
  `_debugger_support` struct and the safe-point argument are the whole design, and
  understanding why it is zero-overhead teaches you more about the eval loop than most
  eval-loop documentation.*
- [Python support for the Linux `perf` profiler](https://docs.python.org/3/howto/perf_profiling.html)
  — *Verdict: short and entirely practical. The frame-pointer / `perf_jit` / `perf inject`
  section is the part that will save you an afternoon.*
- [Instrumenting CPython with DTrace and SystemTap](https://docs.python.org/3/howto/instrumentation.html)
  — *Verdict: read the marker list and the `_ENABLED()` guard convention, then check
  whether your interpreter actually has `--with-dtrace`. It probably does not.*
- [`sys.monitoring`](https://docs.python.org/3.14/library/sys.monitoring.html) — *Verdict:
  read the "Disabling events" section. Returning `DISABLE` from a callback is the
  mechanism that makes low-overhead debuggers possible and it is the direct in-process
  analogue of everything this document does from outside.*
- [What's New in Python 3.14](https://docs.python.org/3.14/whatsnew/3.14.html) — PEP 768,
  `pdb -p PID`, `asyncio ps`/`pstree`. *Verdict: the asyncio-introspection and pdb-remote
  entries are the operationally significant ones.*
- [What's New in Python 3.15](https://docs.python.org/3.15/whatsnew/3.15.html) — Tachyon /
  `profiling.sampling`. *Verdict: read to know what is coming; it is the tool §7's problem
  has been waiting for.*
- [py-spy](https://github.com/benfred/py-spy) — *Verdict: read the README's "how it works"
  and the Kubernetes `SYS_PTRACE` section. The latter is the single most useful paragraph
  in this document's §17.*

**Tools**

- [`bpftrace(8)` reference](https://github.com/bpftrace/bpftrace/blob/master/man/adoc/bpftrace.adoc)
  — *Verdict: read the probe-type list and the `-p` semantics paragraph. The `-p` caveat
  (that it does not reduce kprobe cost) is the kind of thing that turns a diagnostic into
  an incident.*
- [The RED Method](https://grafana.com/blog/2018/08/02/the-red-method-how-to-instrument-your-services/)
  (Wilkie, GrafanaCon EU 2018) — *Verdict: 10 minutes, and it gives you the vocabulary to
  argue with a dashboard. Read alongside the Four Golden Signals chapter of the Google SRE
  book.*
