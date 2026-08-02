# 06 — Processes, Threads, and Scheduling

> **Provenance.** Measurements in this document were produced in one session on an
> **Apple M3 Pro** (5 performance + 6 efficiency cores, 11 logical, no SMT, 128-byte
> cache lines, 16 KB pages), **Darwin 25.5.0** (`xnu-12377.121.10~1`, macOS 26.5.2),
> interpreter **CPython 3.14.6**, GIL build (`sys._is_gil_enabled()` → `True`),
> `sys.getswitchinterval()` → `0.005`. The machine was **not quiet**: `load1` ranged
> **1.84 – 3.65** across the session and is reported alongside every table. Every timing
> is the **median of ≥ 5 alternating passes** with min and max shown, because
> [`30-concurrency-correctness.md`](30-concurrency-correctness.md) §9 documents a case on
> this exact machine where a single pass produced a dramatic and completely false result.
> Scripts are described inline and reproduced in [Lab exercises](#lab-exercises).
>
> **Linux scheduler behaviour is taught here from primary sources, not measured.** The
> kernel-source constants, algorithm descriptions, syscall semantics, and cgroup
> interface details are quoted from `git.kernel.org` (mainline `kernel/sched/*`),
> `docs.kernel.org`, `man7.org` man-pages, and LWN, each attributed inline and listed in
> [Sources](#sources). One short note in
> [What I could not verify](#what-i-could-not-verify) covers the whole class.

---

## Contents

1. [Process, thread, task: three words for one kernel object](#1-process-thread-task-three-words-for-one-kernel-object)
2. [The cost of creating one, measured](#2-the-cost-of-creating-one-measured)
3. [What a context switch actually costs](#3-what-a-context-switch-actually-costs)
4. [Voluntary and involuntary switches: reading the counters](#4-voluntary-and-involuntary-switches-reading-the-counters)
5. [Timeslices, ticks, and preemption](#5-timeslices-ticks-and-preemption)
6. [CFS: the algorithm Linux ran for sixteen years](#6-cfs-the-algorithm-linux-ran-for-sixteen-years)
7. [EEVDF: what replaced it, and why](#7-eevdf-what-replaced-it-and-why)
8. [The real-time classes](#8-the-real-time-classes)
9. [Affinity](#9-affinity)
10. [cgroup v2 CPU control, and the throttling failure mode](#10-cgroup-v2-cpu-control-and-the-throttling-failure-mode)
11. [Asymmetric cores: when the scheduler picks a *kind* of core](#11-asymmetric-cores-when-the-scheduler-picks-a-kind-of-core)
12. [What all of this means for CPython](#12-what-all-of-this-means-for-cpython)
13. [Three clocks, three questions](#13-three-clocks-three-questions)
14. [A diagnostic playbook](#14-a-diagnostic-playbook)
15. [Lab exercises](#lab-exercises)
16. [Question bank](#question-bank)
17. [What I could not verify](#what-i-could-not-verify)
18. [Sources](#sources)

---

## 1. Process, thread, task: three words for one kernel object

The most useful thing to know about processes and threads on Linux is that **the kernel
does not have two concepts.** It has one — the *task*, represented by `struct task_struct`
— and "process" and "thread" are names for two points on a continuous dial of *how much
that task shares with its creator.*

That dial is `clone(2)`. `fork()` is `clone()` with almost no sharing flags;
`pthread_create()` is `clone()` with almost all of them. Everything in between is legal
and is what containers are built out of.

### 1.1 The dial, drawn

```
                      clone(2) flags = the sharing dial
   ┌──────────────────────────────────────────────────────────────────────────┐
   │                                                                          │
   │  fork()                    vfork()          pthread_create()             │
   │    │                          │                    │                     │
   │    ▼                          ▼                    ▼                     │
   │  ┌───────────────────────────────────────────────────────────────────┐   │
   │  │ CLONE_VM        address space      no  │  yes(+wait) │  YES       │   │
   │  │ CLONE_FS        cwd, umask, root   no  │  no         │  YES       │   │
   │  │ CLONE_FILES     fd table           no  │  no         │  YES       │   │
   │  │ CLONE_SIGHAND   signal handlers    no  │  no         │  YES       │   │
   │  │ CLONE_THREAD    thread group / TGID no  │  no        │  YES       │   │
   │  │ CLONE_SYSVSEM   SysV sem undo      no  │  no         │  YES       │   │
   │  │ CLONE_SETTLS    TLS register       no  │  no         │  YES       │   │
   │  └───────────────────────────────────────────────────────────────────┘   │
   │                                                                          │
   │  Everything the scheduler cares about is IDENTICAL in all three cases.    │
   │  A task_struct is a task_struct. It has a vruntime, a weight, an          │
   │  affinity mask, a policy, and a place on exactly one CPU's runqueue.      │
   └──────────────────────────────────────────────────────────────────────────┘

   The consequence, stated once and relied on for the rest of this document:

        THE SCHEDULER SCHEDULES THREADS, NOT PROCESSES.

   A 40-thread process is 40 entries competing on runqueues, not one.
   This is why `nice` on a process, cgroup quota on a container, and
   "one CPU" in a Kubernetes limit all mean something subtler than
   they look. See §10.
```

`clone(2)` also encodes the *dependencies* between these flags, which tell you where the
kernel's own abstraction boundaries are. From the man page's `ERRORS` section: specifying
`CLONE_SIGHAND` without `CLONE_VM` is `EINVAL` (since 2.6.0), and specifying
`CLONE_THREAD` without `CLONE_SIGHAND` is `EINVAL` (since 2.5.35). Read backwards, that
is the kernel telling you: *a thread group must share signal handlers, and shared signal
handlers only make sense with a shared address space.* Signal disposition is a property of
the address space, not of the schedulable entity — which is exactly why signal handling in
threaded programs is as awkward as it is (see [`10-signals-fork-exec.md`](10-signals-fork-exec.md)).

### 1.2 What "thread" buys you and what it costs

| Resource | Separate process | Thread in the same process |
|---|---|---|
| Address space / page tables | own | shared |
| File descriptor table | own (copy at fork) | shared |
| Signal handlers | own | shared |
| Signal *mask* | own | **own** (per-task) |
| cwd, umask, root | own | shared |
| Scheduling entity | one per process | **one per thread** |
| Affinity mask | per-task | **per-task, independently settable** |
| `getrusage(RUSAGE_SELF)` | per process | **summed across all threads** |
| Failure blast radius | isolated | shared — one segfault kills all |

The last two rows are the ones that cause bugs. The rusage row is measured in §4.2 and it
surprised me.

### 1.3 The layer underneath `pthread_create`

`pthread_create` is a portability shim, not a primitive. Underneath it on Linux is
`clone()` and a `task_struct`. Underneath it on the machine used for the measurements
below is a Mach **thread** inside a Mach **task** — the split is architectural rather than
cosmetic: a Mach *task* owns resources (an address space, a port namespace) and executes
nothing; a Mach *thread* is the schedulable register state and owns no resources. A
"process" on Darwin is a BSD-level `proc` structure glued to a Mach task.

Why mention it at all: because it explains the shape of the knobs available in §11. A
system that separates "the thing that holds resources" from "the thing that runs" will
naturally expose scheduling controls at the *thread* level, which is what makes the
per-thread QoS experiment in §11.2 possible.

---

## 2. The cost of creating one, measured

Before any scheduling theory, the price list. Same workload for every entry: create a
schedulable entity that does nothing, and wait for it to finish.

**Measured**, median of 5 alternating passes, `load1 = 2.97`:

| Mechanism | min | median | max | vs. a thread |
|---|---|---|---|---|
| `threading.Thread` start only | 23.4 µs | **24.8 µs** | 26.4 µs | 1.0× |
| `threading.Thread` start + join | 27.3 µs | 28.2 µs | 29.3 µs | 1.1× |
| `os.fork()` + `_exit` + `waitpid` | 759 µs | **806 µs** | 833 µs | **32.5×** |
| `mp.Process` (`fork`) | 1.23 ms | 1.27 ms | 1.50 ms | 51× |
| `subprocess.run(["/usr/bin/true"])` | 1.49 ms | 1.51 ms | 1.53 ms | 61× |
| `mp.Process` (`forkserver`, warm) | 4.73 ms | 4.89 ms | 5.80 ms | 197× |
| `subprocess.run([python, "-c", "pass"])` | 10.82 ms | 11.04 ms | 11.20 ms | 445× |
| `mp.Process` (`spawn`) | 24.94 ms | **25.19 ms** | 25.33 ms | **1016×** |

Four things fall out of this table.

**A thread is ~25 µs and a fork is ~0.8 ms — a 32× gap, not the 1000× gap folklore
implies.** `fork()` is genuinely cheap because it is copy-on-write: the kernel copies page
*tables*, not pages. The expensive part of "starting a process" is almost never `fork`; it
is everything after it.

**`spawn` costs 1000× a thread, and it is the default.** Reading CPython 3.14's
`multiprocessing/context.py` directly:

```python
    # bpo-33725: running arbitrary code after fork() is no longer reliable
    # on macOS since macOS 10.14 (Mojave). Use spawn by default instead.
    # gh-84559: We changed everyones default to a thread safe one in 3.14.
    if reduction.HAVE_SEND_HANDLE and sys.platform != 'darwin':
        _default_context = DefaultContext(_concrete_contexts['forkserver'])
    else:
        _default_context = DefaultContext(_concrete_contexts['spawn'])
```

So as of 3.14 the default is `forkserver` on Linux and `spawn` elsewhere — a deliberate
trade of ~4 ms (forkserver) or ~25 ms (spawn) per worker against the class of
fork-in-a-threaded-process deadlocks catalogued in
[`10-signals-fork-exec.md`](10-signals-fork-exec.md). The 20 ms difference between
`forkserver` and `spawn` in the table is precisely the cost of re-executing the
interpreter and re-importing `__main__` versus forking an already-initialized stub.

**Interpreter startup dominates process creation.** `subprocess` of `/usr/bin/true` is
1.51 ms; `subprocess` of `python -c pass` is 11.04 ms. **~9.5 ms of that is CPython
starting up**, and it is the single largest term in the whole table. Any architecture
that starts a Python process per unit of work has a 10 ms floor before your code runs.

**Practical rule.** If a work item takes less than ~1 ms, a process pool cannot pay for
itself no matter how parallel the work is; you need a *persistent* pool, and the pool
creation cost must be amortized over the process lifetime, not the task. This is the
quantitative version of the advice in
[`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md).

---

## 3. What a context switch actually costs

A context switch has two costs, and only one of them shows up in a microbenchmark.

### 3.1 The direct cost

The kernel must:

1. Enter the kernel (trap or interrupt), save the outgoing task's user register file.
2. Run the scheduler: pick the next task from the runqueue. On EEVDF this is an rbtree
   walk with an augmented-tree min-`vruntime` search (§7).
3. Switch the address space if the next task has a different one (`switch_mm`), then
   switch registers and the kernel stack (`switch_to`).
4. Restore the incoming task's registers and return to user mode.

That is a few hundred nanoseconds of pure work on modern hardware. It is not where the
money goes.

### 3.2 The indirect cost, which is larger

The incoming task finds:

- **A cold L1.** Both caches, data and instruction. On the machine here that is 128 KB
  L1d on a P-core; on an E-core it is 64 KB
  ([`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §2).
- **A polluted L2**, shared cluster-wide, evicted in proportion to how long the other task
  ran.
- **TLB entries that may or may not survive.** This used to be the dominant term: a naive
  address-space switch flushes the TLB, and every subsequent memory access takes a page
  walk. Modern architectures avoid it — x86 has PCID (process-context identifiers),
  AArch64 has ASIDs (address space identifiers) in `TTBR0_EL1`. TLB entries are tagged
  with the address space that created them, so a switch away and back does not
  necessarily invalidate anything.
- **A branch predictor and prefetcher trained on someone else's code.**

The kernel has its own number for how expensive this is. From mainline
`kernel/sched/fair.c`:

```c
__read_mostly unsigned int sysctl_sched_migration_cost	= 500000UL;
```

**500 µs.** That is the kernel's estimate of how long a task's cache footprint stays
valuable — used to decide whether a task is "cache hot" and therefore should *not* be
migrated to an idle CPU during load balancing. It is three orders of magnitude larger than
the direct switch cost, and it is the correct order of magnitude to keep in your head for
what a switch really costs when it moves you to a different core.

### 3.3 Measured: socketpair ping-pong

Two threads (then two processes) bounce one byte back and forth over a `socketpair`. One
round trip = two blocking waits = two wakeups = **two context switches**. The baseline
subtracts syscall cost: the same `send`+`recv` pair issued from a single thread, where the
data is already buffered and nothing ever blocks.

**Measured**, 20,000 round trips per pass, median of 5 alternating passes, `load1 = 2.22`:

| Case | min | median | max |
|---|---|---|---|
| syscall only, no switch (`send`+`recv`, same thread) | 593.0 ns | **622.5 ns** | 960.2 ns |
| 2 threads, same process | 5959.9 ns | **6013.8 ns** | 6058.8 ns |
| 2 processes (`fork`) | 6305.7 ns | **6383.8 ns** | 6556.6 ns |

Derived:

```
   thread switch pair  = 6013.8 - 622.5 = 5391.3 ns  ->  2696 ns per switch
   process switch pair = 6383.8 - 622.5 = 5761.3 ns  ->  2881 ns per switch
   process / thread round-trip ratio    = 1.062x
```

**The headline: switching between two processes cost 6.2% more than switching between two
threads.** Not 2×, not 10×. The folklore that "process context switches are far more
expensive than thread context switches" is a statement about TLB flushes on hardware that
no longer flushes. With ASID-tagged TLB entries the address-space switch is a register
write and a possible extra page-walk or two.

Two caveats that keep this honest:

- These processes share almost nothing and their working sets are tiny. A process switch
  between two 500 MB workloads *would* differ, because the indirect cost of §3.2 scales
  with footprint, not with the switch mechanism. The benchmark measures the *direct* cost
  faithfully and the *indirect* cost barely at all.
- 2.7 µs is high relative to the "sub-microsecond" figure often quoted for a bare
  `switch_to`. This measurement includes the full wake-up path — mark runnable, enqueue,
  possibly send an IPI to another core, run the scheduler, return — plus two Python method
  calls per side. It is the number that matters for "how much does an event-driven handoff
  cost me", which is the question people actually have.

**The rule this gives you:** an event-driven design that hands work between threads pays
roughly **2.7 µs per handoff on this machine**. If your work items are 5 µs, you have
built a system that spends a third of its life in the scheduler. Batch, or don't hand off.
This is the same arithmetic that makes short critical sections lose to long ones in
[`30-concurrency-correctness.md`](30-concurrency-correctness.md) §9.

---

## 4. Voluntary and involuntary switches: reading the counters

### 4.1 What the two counters are supposed to mean

`getrusage(2)` reports two switch counters, exposed in Python as
`resource.getrusage(resource.RUSAGE_SELF).ru_nvcsw` and `.ru_nivcsw`:

- **`ru_nvcsw` — voluntary.** The task gave up the CPU before it had to, because it
  blocked: a read that had no data, a lock that was held, a sleep.
- **`ru_nivcsw` — involuntary.** The task was *preempted*: it still wanted the CPU and the
  scheduler took it away, because its slice expired or something more urgent woke up.

The classical reading, which is Gregg's in *Systems Performance*: **a high involuntary
rate means CPU saturation** — you have more runnable work than CPUs, and tasks are being
kicked off mid-computation. A high voluntary rate means an I/O-bound or heavily
synchronizing workload. The ratio characterizes the workload without a profiler.

### 4.2 Measured workload signatures

Four workload shapes, each run for 1.0 s, counters sampled around it.
**Measured**, median of 5 alternating passes, `load1 = 2.16`:

| Workload | vol/s | invol/s | vol : invol | cpu/wall |
|---|---|---|---|---|
| CPU-bound (tight integer loop) | 0 | **62** | 0 : 1 | 1.000 |
| I/O-bound (1 ms `time.sleep` loop) | 0 | 794 | 0 : 1 | 0.011 |
| I/O-bound (`socketpair` round trips) | **337,126** | 263 | 1282 : 1 | 1.015 |
| mixed (5 ms burn / 5 ms sleep) | 0 | 121 | 0 : 1 | 0.447 |

Spread across passes: CPU-bound involuntary 34–146, sleep involuntary 790–799,
socketpair voluntary 331,920–356,621.

Three of the four rows behave exactly as the model predicts:

- The **CPU-bound** row is the cleanest number in this document. A single CPU-bound thread
  was preempted **62 times per second — once every ~16 ms.** Nothing else moved it. That
  is the OS preemption rate for an unloaded, un-niced compute thread, and §12.2 compares
  it against the GIL's rate.
- The **socketpair** row shows 337k voluntary switches per second against 263 involuntary
  — a 1282 : 1 ratio, and the arithmetic checks out against §3.3: 6.0 µs per round trip is
  ~166k round trips/s, two threads blocking on each, ~333k voluntary switches. The
  counters and the timings agree to within 1%.
- The **mixed** row shows `cpu/wall = 0.447`, correctly reporting a workload that is on
  the CPU 45% of the time.

### 4.3 The row that does not behave: `time.sleep`

**A loop of 1 ms sleeps produced 794 involuntary switches per second and zero voluntary
ones.** A sleeping thread is the textbook definition of a *voluntary* yield. The counter
says otherwise, and it says so with a `cpu/wall` ratio of 0.011 that proves the thread
really was asleep.

That is worth chasing, so I chased it. Six blocking primitives, 2000 operations each,
counters differenced around each. **Measured**, median of 5 alternating passes,
`load1 = 3.65`:

| Blocking primitive | `ru_nvcsw` | `ru_nivcsw` | vol/op | invol/op |
|---|---|---|---|---|
| `time.sleep(0.5 ms)` × N | 0 | 2006 | 0.00 | **1.00** |
| `Event.wait(0.5 ms)` (timed) × N | 0 | 2013 | 0.00 | **1.01** |
| `Event.wait()` (untimed, 2 threads) × N | 0 | 4007 | 0.00 | **2.00** |
| `socket.recv` (untimed, 2 threads) × N | **4000** | 4 | **2.00** | 0.00 |
| `socket.recv` with `settimeout` × N | 2000 | 2004 | 1.00 | 1.00 |
| contended `threading.Lock` (2 threads) × N | 0 | 14 | 0.00 | 0.01 |

My first hypothesis — "timed waits are booked differently from untimed ones" — is
**refuted by row 3**: an untimed `Event.wait()` is still booked involuntary. The pattern
that actually fits is about *which layer* the thread blocks in. Blocking in the BSD socket
layer (`recv` with no timeout) is counted voluntary. Blocking in the lower-level
synchronization primitives — the sleep/semaphore path, the futex-equivalent used by
`PyMutex`/`Condition`, and the `poll`-with-deadline path that `settimeout` forces
`socket.recv` onto — is counted involuntary. Row 5 is the clincher: **the same logical
operation flips from voluntary to involuntary purely because adding a timeout routes it
through `poll` instead of a blocking `recv`.**

I could not read the kernel's accounting site to confirm the exact mechanism, so the
mechanism goes in [What I could not verify](#what-i-could-not-verify). The *finding*,
however, is solid and it is the transferable lesson:

> **`ru_nivcsw` is not a portable measure of preemption.** It measures "switches the kernel
> chose to classify as involuntary", and that classification differs by which wait
> primitive you used. Before you build an alert on "involuntary switches per second means
> CPU saturation", validate the counter against a workload whose answer you already know
> — a pure spin loop should show a rate near your tick rate (62/s here), and a sleep loop
> should show something you can explain.

Row 6 is a free confirmation of a sibling result: **2000 acquisitions of a contended
`threading.Lock` produced 14 context switches total.** Under the GIL the lock is almost
never actually contended, because the GIL has already serialized the threads that would
contend for it. That is
[`30-concurrency-correctness.md`](30-concurrency-correctness.md) §10's "starvation is a
GIL artifact" result, arrived at from the OS counters instead of from the lock.

### 4.4 The signatures worth memorizing

| Signature | Diagnosis |
|---|---|
| high involuntary, `cpu/wall` ≈ 1.0, rate ≫ tick rate | CPU saturation: more runnable threads than CPUs |
| high involuntary, `cpu/wall` ≈ 1.0, rate ≈ tick rate | healthy CPU-bound work, no contention |
| high voluntary, `cpu/wall` ≪ 1.0 | I/O- or lock-bound; find the wait |
| high voluntary, `cpu/wall` ≈ 1.0 | you are paying for handoffs, not doing work (§3.3) |
| low both, `cpu/wall` ≪ 1.0, high latency | **you are being throttled** — see §10 |

That last row is the one this document exists to make answerable.

---

## 5. Timeslices, ticks, and preemption

### 5.1 Two different clocks

A preemptive scheduler needs a way to regain control from code that never blocks. There
are two mechanisms and they are frequently conflated.

**The tick** is a periodic timer interrupt. Its job is accounting: charge elapsed time to
the running task, update statistics, and check whether the running task should be
preempted. `kern.clockrate` on the machine here reports `hz = 100, tick = 10000` — a
10 ms statistics tick. Linux's `CONFIG_HZ` is typically 250 or 1000, and modern kernels run
largely tickless (`CONFIG_NO_HZ_FULL`) so that a CPU with one runnable task takes no
timer interrupts at all.

**The slice** is how long the scheduler intends to let a task run. It is *not* the tick,
and on a modern kernel it is not a fixed quantum either. The tick is only the coarsest
granularity at which slice expiry can be *noticed*; high-resolution timers (`HRTICK`) can
arm an interrupt at the exact slice boundary instead.

### 5.2 There is no fixed timeslice

This is the single most common stale belief about Linux scheduling. There has been no
fixed timeslice since the O(1) scheduler was replaced in 2.6.23. What exists in mainline
today is a *base* slice:

```c
/* kernel/sched/fair.c, mainline */
unsigned int sysctl_sched_base_slice			= 700000ULL;
static unsigned int normalized_sysctl_sched_base_slice	= 700000ULL;
```

**700 µs.** That is the default request size a task makes of the scheduler (§7.2), not a
guaranteed quantum. The actual time a task runs before being preempted depends on its
weight, the weights of everything else runnable, its lag, and its deadline.

The one place a genuine fixed quantum survives is `SCHED_RR`, whose quantum is retrievable
via `sched_rr_get_interval(2)` — and that is a real-time policy you almost certainly are
not using (§8).

### 5.3 The preemption points

A task loses the CPU at one of:

- **Slice expiry**, noticed at the tick or at an `HRTICK` timer.
- **A higher-priority wakeup.** A task in a higher scheduling class always preempts one in
  a lower class. Within the fair class, a woken task preempts the running one if the
  scheduler decides it should (`WAKEUP_PREEMPTION`, and under EEVDF the `PREEMPT_SHORT`
  feature, which lets a task with a shorter slice preempt one with a longer slice —
  visible in `kernel/sched/fair.c` as `if (sched_feat(PREEMPT_SHORT) && (pse->slice < se->slice))`).
- **A blocking syscall**, which is voluntary.
- **A `cond_resched()` point in the kernel**, or anywhere at all under
  `CONFIG_PREEMPT`/`PREEMPT_RT`.

For a Python program, only the first three are observable, and §12 shows that the GIL adds
a fourth that dominates all of them.

---

## 6. CFS: the algorithm Linux ran for sixteen years

You need CFS even though it has been superseded, because every scheduler tunable, every
cgroup file, and every piece of tribal knowledge in your organization is phrased in its
vocabulary.

### 6.1 The idea

From the kernel's own CFS documentation:

> 80% of CFS's design can be summed up in a single sentence: CFS basically models an
> "ideal, precise multi-tasking CPU" on real hardware. […] "Ideal multi-tasking CPU" is a
> (non-existent :-)) CPU that has 100% physical power and which can run each task at
> precise equal speed, in parallel, each at 1/nr_running speed.

Real hardware runs one task at a time per CPU, so CFS simulates the ideal machine with a
bookkeeping value:

> In CFS the virtual runtime is expressed and tracked via the per-task `p->se.vruntime`
> (nanosec-unit) value. […] CFS's task picking logic is based on this `p->se.vruntime`
> value and it is thus very simple: it always tries to run the task with the smallest
> `p->se.vruntime` value (i.e., the task which executed least so far).

`vruntime` is real elapsed runtime *scaled by the task's weight*. A heavy task's
`vruntime` advances slowly, so it stays leftmost longer and gets more CPU. That is the
entire mechanism.

### 6.2 Nice values are weights, and the weights are a real table

`nice` is not a priority in CFS; it is an index into a weight table. From mainline
`kernel/sched/core.c`:

```c
const int sched_prio_to_weight[40] = {
 /* -20 */     88761,     71755,     56483,     46273,     36291,
 /* -15 */     29154,     23254,     18705,     14949,     11916,
 /* -10 */      9548,      7620,      6100,      4904,      3906,
 /*  -5 */      3121,      2501,      1991,      1586,      1277,
 /*   0 */      1024,       820,       655,       526,       423,
 /*   5 */       335,       272,       215,       172,       137,
 /*  10 */       110,        87,        70,        56,        45,
 /*  15 */        36,        29,        23,        18,        15,
};
```

Read it properly and it answers the question people actually ask.

- **Nice 0 is weight 1024.** That is `NICE_0_LOAD`, the unit.
- **Each step is a factor of ~1.25.** 1024/820 = 1.249, 820/655 = 1.252, 88761/71755 =
  1.237. This is deliberate: the design goal is that **one nice level changes a task's CPU
  share by about 10% when two tasks compete** — because 1.25/(1.25+1) = 0.556, i.e. 55.6%
  vs 44.4%, a swing of ~10 percentage points. That property holds regardless of *where* on
  the scale you are, which is exactly what the old fixed-quantum schedulers got wrong.
- **The dynamic range is 88761/15 = 5917×.** A `nice -20` task competing with a `nice +19`
  task gets 99.98% of the CPU. `nice` is not a gentle hint at the extremes.
- Two tasks at nice 0 and nice 5: 1024 vs 335, i.e. 75.4% / 24.6%.

The kernel keeps a second table, `sched_prio_to_wmult[40]`, holding pre-computed `2^32/x`
inverses so the division in the `vruntime` update becomes a multiply — the comment in
`core.c` says exactly that. It is a nice reminder that this arithmetic runs on every
scheduler tick on every CPU.

### 6.3 The rbtree

> CFS maintains a time-ordered rbtree, where all runnable tasks are sorted by the
> `p->se.vruntime` key. CFS picks the "leftmost" task from this tree and sticks to it.

Plus `rq->cfs.min_vruntime`, a monotonically increasing floor used to place newly woken
tasks. That floor is what stops a task that slept for an hour from having a `vruntime` an
hour behind everyone else's and then monopolizing the CPU to "catch up". Newly woken tasks
get placed relative to `min_vruntime`, not at their stale value.

### 6.4 Where CFS ran out of road

CFS gives you one knob — `nice` — and that knob controls **how much CPU** a task gets. It
has no way to express **how soon** a task should get it. Those are different requirements:
an audio thread wants 2% of the CPU but wants it within 5 ms; a compiler wants 100% of the
CPU and does not care about latency at all. In CFS both are expressed by moving `nice`,
which is the wrong control, so you get a pile of heuristics: sleeper fairness bonuses,
wakeup preemption granularity, `sched_latency`/`min_granularity` tuning, and the
`sched_wakeup_granularity_ns` folklore. Every one of those is a patch over the missing
concept.

The proposed fix was a separate `latency_nice` attribute. What actually happened was more
interesting.

---

## 7. EEVDF: what replaced it, and why

### 7.1 The status, dated

The kernel's own EEVDF documentation:

> The "Earliest Eligible Virtual Deadline First" (EEVDF) was first introduced in a
> scientific publication in 1995. The Linux kernel began transitioning to EEVDF in version
> 6.6 (as a new option in 2024), moving away from the earlier Completely Fair Scheduler
> (CFS) in favor of a version of EEVDF proposed by Peter Zijlstra in 2023.

The algorithm is from Ion Stoica and Hussein Abdel-Wahab, 1995 — it is not new work, it is
a thirty-year-old paper finally applied. Zijlstra's implementation landed in the 6.6 cycle;
the remaining pieces (notably the `DELAY_DEQUEUE` / `DELAY_ZERO` scheduler features that
fix lag handling for tasks that block) are dated **2024-08-17, Peter Zijlstra**, per the
`kernel/sched/features.h` commit log, and are `true` by default in mainline today:

```c
/* kernel/sched/features.h, mainline */
SCHED_FEAT(PLACE_LAG, true)
SCHED_FEAT(PLACE_DEADLINE_INITIAL, true)
SCHED_FEAT(RUN_TO_PARITY, true)
SCHED_FEAT(PREEMPT_SHORT, true)
SCHED_FEAT(DELAY_DEQUEUE, true)
SCHED_FEAT(DELAY_ZERO, true)
```

The scheduler is still called "the fair class" and still lives in `kernel/sched/fair.c`;
`SCHED_OTHER` / `SCHED_NORMAL` is still the policy name. Nothing in userspace changed.
**"CFS" as a name for the thing your Linux box runs is now wrong, but "CFS" as a name for
the cgroup interface and the fair class is still what everyone says.**

### 7.2 The three concepts

Jonathan Corbet's LWN write-up of the original posting gives the cleanest statement of
lag:

> For each process, EEVDF calculates the difference between the time that process should
> have gotten and how much it actually got; that difference is called "lag". A process with
> a positive lag value has not received its fair share and should be scheduled sooner than
> one with a negative lag value.

And the kernel doc:

> EEVDF picks tasks with lag greater or equal to zero and calculates a virtual deadline
> (VD) for each, selecting the task with the earliest VD to execute next. It's important to
> note that this allows latency-sensitive tasks with shorter time slices to be prioritized,
> which helps with their responsiveness.

So there are three:

1. **Lag** — how much CPU you are owed. Positive means underserved.
2. **Eligibility** — you may only be picked if `lag >= 0`. This is the fairness guarantee,
   and it is *stronger* than CFS's: CFS picked the minimum `vruntime` and hoped;
   EEVDF *excludes* over-served tasks from consideration entirely.
3. **Virtual deadline** — `eligible_time + request_size`, where the request size is your
   slice. Among eligible tasks, earliest deadline wins.

### 7.3 The trick, which is worth understanding properly

Here is the part that makes EEVDF the right answer rather than merely a different one.
Corbet:

> When the scheduler is calculating the time slice for each process, it factors in that
> process's assigned latency-nice value; a process with a lower latency-nice setting (and,
> thus, tighter latency requirements) will get a shorter time slice. […] Remember that the
> virtual deadline is calculated by adding the time slice to the eligible time. That will
> cause processes with shorter time slices to have closer virtual deadlines and, as a
> result, to be executed first. […] **Note that the amount of CPU time given to any two
> processes (with the same nice value) will be the same, but the low-latency process will
> get it in a larger number of shorter slices.** No tricky scheduler heuristics are needed
> to get this result.

That is the whole design in one paragraph: **latency and throughput become independent
axes, and they fall out of one formula.** `nice` still controls *how much*. Slice length
controls *how soon*, because a shorter request produces an earlier deadline. A task
asking for less at a time is served sooner, and the total it receives is unchanged.

```
   EEVDF pick, drawn
   ═════════════════

   virtual time ──────────────────────────────────────────────────────►
                                 V (now)
                                 │
   task A  lag = +3 ms  ─────────┼────┐ slice 700 µs
   (eligible)                    │    │ deadline ──► [A]      ◄── earliest
                                 │    └────────────────────────    deadline
                                 │                                 among the
   task B  lag = +1 ms  ─────────┼─────────┐ slice 3 ms            ELIGIBLE
   (eligible)                    │         │ deadline ────► [B]    set: A wins
                                 │         └───────────────────
                                 │
   task C  lag = -2 ms  ────┐    │                       C is INELIGIBLE.
   (over-served)            └────┼──► becomes eligible   Not considered at
                                 │    once V advances    all, no matter how
                                 │    past its eligible  close its deadline.
                                 │    time
                                 │
   ┌─────────────────────────────┴──────────────────────────────────────────┐
   │ TWO GATES, IN ORDER:                                                    │
   │   1. eligibility (lag >= 0)  -> the FAIRNESS guarantee                  │
   │   2. earliest virtual deadline -> the LATENCY guarantee                 │
   │                                                                          │
   │ CFS had only one gate (min vruntime) and therefore only one guarantee.   │
   │ Everything CFS did about latency was a heuristic bolted onto that gate.  │
   └──────────────────────────────────────────────────────────────────────────┘
```

### 7.4 The parts that are still moving

The hard problem is **lag for sleeping tasks**. If a task can reset a negative lag by
sleeping briefly, then sleeping becomes an exploit: burn your share, nap, come back
eligible. The kernel doc:

> There are ongoing discussions on how to manage lag, especially for sleeping tasks; but at
> the time of writing EEVDF uses a "decaying" mechanism based on virtual run time (VRT).
> This prevents tasks from exploiting the system by sleeping briefly to reset their negative
> lag.

`DELAY_DEQUEUE` is the mechanical half of this: a task that blocks while it still has
negative lag is not immediately removed from the runqueue; it stays enqueued (and keeps
"paying off" its debt in virtual time) until its lag reaches zero. `PLACE_LAG` controls
whether a waking task's lag is preserved across the sleep at all.

**What this means for you as an application engineer:** almost nothing directly, and that
is the point. There is no EEVDF tunable you should be reaching for. What changed is that
the *reason* your latency-sensitive thread was being starved under CFS — that `nice` was
the only lever and it was the wrong lever — has a real answer now, in the kernel, with no
tuning. Distrust any runbook step that sets `sched_latency_ns` or
`sched_wakeup_granularity_ns`; those sysctls belong to a scheduler that no longer exists.

---

## 8. The real-time classes

Above the fair class sit the real-time policies. `sched(7)` describes the model:

> Conceptually, the scheduler maintains a list of runnable threads for each possible
> `sched_priority` value. In order to determine which thread runs next, the scheduler looks
> for the nonempty list with the highest static priority and selects the thread at the head
> of this list.

```
   Linux scheduling classes, highest first
   ═══════════════════════════════════════

   ┌────────────────────────────────────────────────────────────────────────┐
   │ stop_sched_class     kernel-internal (CPU hotplug, migration). Not      │
   │                      reachable from userspace. Preempts everything.     │
   ├────────────────────────────────────────────────────────────────────────┤
   │ SCHED_DEADLINE       (runtime, deadline, period) per task.              │
   │                      GEDF + CBS. Admission-controlled: the kernel       │
   │                      REFUSES the request if the set is not schedulable. │
   │                      Since Linux 3.14. sched_setattr(2) only.           │
   ├────────────────────────────────────────────────────────────────────────┤
   │ SCHED_FIFO           static priority 1..99, run until you block or      │
   │ SCHED_RR             yield (FIFO) / until your quantum expires (RR).    │
   │                      NO fairness. NO starvation protection except       │
   │                      RT throttling (below).                             │
   ├────────────────────────────────────────────────────────────────────────┤
   │ SCHED_OTHER (NORMAL) the fair class: EEVDF today, CFS before 6.6.       │
   │ SCHED_BATCH          fair, but never preempts on wakeup (throughput).   │
   │ SCHED_IDLE           fair, weight 3. Runs only when nothing else can.   │
   └────────────────────────────────────────────────────────────────────────┘

   A single runnable SCHED_FIFO task at priority 99 starves every
   SCHED_OTHER task on its CPU, indefinitely, by design.
```

### 8.1 FIFO vs RR

`sched(7)`:

> `SCHED_RR` is a simple enhancement of `SCHED_FIFO`. Everything described above for
> `SCHED_FIFO` also applies to `SCHED_RR`, except that each thread is allowed to run only
> for a maximum time quantum. If a `SCHED_RR` thread has been running for a time period
> equal to or longer than the time quantum, it will be put at the end of the list for its
> priority. A `SCHED_RR` thread that has been preempted by a higher priority thread and
> subsequently resumes execution as a running thread will complete the unexpired portion of
> its round-robin time quantum.

The quantum is retrievable with `sched_rr_get_interval(2)`. Note the last sentence: RR's
quantum is *conserved* across preemption, which is what makes it round-robin rather than
merely time-sliced.

### 8.2 SCHED_DEADLINE

`sched(7)`:

> Since Linux 3.14, Linux provides a deadline scheduling policy (`SCHED_DEADLINE`). This
> policy is currently implemented using GEDF (Global Earliest Deadline First) in conjunction
> with CBS (Constant Bandwidth Server). […] A sporadic task is one that has a sequence of
> jobs, where each job is activated at most once per period. Each job also has a *relative
> deadline*, before which it should finish execution.

You describe your task as a triple — *runtime*, *deadline*, *period* — and the kernel
either accepts it or refuses. That admission control is the entire value proposition:
`SCHED_DEADLINE` is the only Linux policy that will tell you *in advance* that what you
asked for is impossible, rather than degrading silently. CBS is the enforcement half: a
task that overruns its declared runtime is throttled until its next period, so one buggy
deadline task cannot take the system down.

### 8.3 RT throttling: the safety net you must know about

A `SCHED_FIFO` infinite loop is a kernel-level denial of service. `sched(7)`:

> `/proc/sys/kernel/sched_rt_period_us` — This file specifies a scheduling period that is
> equivalent to 100% CPU bandwidth. […] The default value in this file is 1,000,000 (1
> second).
>
> `/proc/sys/kernel/sched_rt_runtime_us` — The value in this file specifies how much of the
> "period" time can be used by all real-time and deadline scheduled processes on the system.
> […] The default value in this file is 950,000 (0.95 seconds), meaning that **5% of the CPU
> time is reserved for processes that don't run under a real-time or deadline scheduling
> policy.**

So by default your runaway RT thread gets 95% and leaves you 5% to log in and kill it.
`RLIMIT_RTTIME` is the per-process version: a ceiling on CPU time consumed in one
uninterrupted RT burst.

**The failure mode to recognize:** a service using `SCHED_FIFO` that mysteriously stalls
for 50 ms every second is being RT-throttled. It is consuming its 950 ms and then being
descheduled for the remaining 50 ms of the period. The signature is a *perfectly periodic*
stall, which is diagnostic — real contention is never that regular.

### 8.4 Why you should almost certainly not use these from Python

Setting `SCHED_FIFO` requires privilege (`CAP_SYS_NICE`, or an `RLIMIT_RTPRIO` allowance).
Then it requires you to be right about your worst-case execution time — because there is
no fairness backstop, only the 5% reservation. A Python thread cannot make that promise:
it can be interrupted by a GC pass (§12.5), a `malloc` that takes a page fault, or an
arena being returned to the OS.

There is also a purely mechanical obstacle worth documenting, because it surprises people
who go looking:

```console
$ python3.14 -c "import os; print(hasattr(os,'sched_setscheduler'), os.SCHED_FIFO)"
```

**On the machine used here, `os.SCHED_FIFO`, `os.SCHED_RR`, and `os.SCHED_OTHER` all
exist, and `os.sched_setscheduler` does not** *(measured)*. Also absent:
`sched_getscheduler`, `sched_setparam`, `sched_getparam`, `sched_rr_get_interval`,
`sched_setaffinity`, `sched_getaffinity`. Present: `sched_yield`,
`sched_get_priority_min`, `sched_get_priority_max` (which report 15..47 for all three
policies here). The lesson generalizes past this one platform: **`os.SCHED_*` constants
existing tells you nothing about whether the syscalls that consume them exist.** Probe with
`hasattr`, not with the constants.

---

## 9. Affinity

### 9.1 The interface

`sched_setaffinity(2)`:

> A thread's CPU affinity mask determines the set of CPUs on which it is eligible to run.
> […] Restricting a thread to run on a single CPU also avoids the performance cost caused
> by the cache invalidation that occurs when a thread ceases to execute on one CPU and then
> recommences execution on a different CPU.

Three properties matter and are easy to get wrong:

- **It is per-thread, not per-process.** "The affinity mask is a per-thread attribute that
  can be adjusted independently for each of the threads in a thread group." Pass a TID from
  `gettid(2)`; passing a PID sets only the main thread. A `taskset` on a PID sets the main
  thread's mask, which threads created *afterwards* inherit — and threads created *before*
  keep their old mask.
- **It is inherited across `fork()` and preserved across `execve()`.** Your affinity is
  sticky in ways you did not ask for.
- **The kernel silently narrows it.** "The set of CPUs on which the thread will actually
  run is the intersection of the set specified in the `mask` argument and the set of CPUs
  actually present on the system. The system may further restrict the set of CPUs on which
  the thread runs if the 'cpuset' mechanism is being used. **These restrictions on the
  actual set of CPUs on which the thread will run are silently imposed by the kernel.**"

That last one is the container trap: inside a cpuset-constrained container, your carefully
computed mask is intersected with the cpuset and you are never told.

### 9.2 What pinning buys, and what it costs

**Buys:** a warm L1/L2, no migration cost (§3.2's 500 µs of cache value), predictable NUMA
locality, and reproducible benchmarks.

**Costs:** you have taken load balancing away from a scheduler that is better at it than
you are. A pinned thread on a busy CPU will *not* migrate to an idle one. Pinning is
correct for a small number of long-lived, known-shape threads (a database's I/O threads, a
packet-processing loop) and wrong for a general worker pool.

### 9.3 Three ways to ask "how many CPUs do I have", and they are different questions

| Call | Question it answers |
|---|---|
| `os.cpu_count()` | How many logical CPUs does this machine have? |
| `os.process_cpu_count()` (3.13+) | How many can *this process* actually use? |
| `len(os.sched_getaffinity(0))` | The raw affinity mask, Linux only |

`os.process_cpu_count()` exists because `os.cpu_count()` is the wrong answer in almost
every deployment: it reports the host's core count, not your affinity mask and not your
cgroup quota. It also honours the `PYTHON_CPU_COUNT` environment variable and the `-X
cpu_count` flag, which is the escape hatch for the cases the kernel cannot tell you about
(§10.4).

**Measured** on the machine here:

```
os.cpu_count()               = 11
os.process_cpu_count()       = 11
len(os.sched_getaffinity(0)) = ABSENT on this platform  (AttributeError)
PYTHON_CPU_COUNT             = None
```

This matters at the Python level because pool sizes are derived from it.
`ThreadPoolExecutor`'s default is `min(32, (os.process_cpu_count() or 1) + 4)` — measured
and discussed in [`29-async-patterns-and-pitfalls.md`](29-async-patterns-and-pitfalls.md)
— and `ProcessPoolExecutor` and `multiprocessing.Pool` default to the CPU count. **A
container limited to 2 CPUs on a 64-core host will, by default, start 64 worker processes,
each ~25 ms to spawn (§2), all sharing 2 CPUs' worth of quota.** That is not a
hypothetical; it is the single most common way the next section's failure mode gets
triggered.

---

## 10. cgroup v2 CPU control, and the throttling failure mode

This section exists to answer the Tier 1 checklist question:

> *Your service has low CPU utilisation but high latency. cgroup CPU throttling is one
> candidate — how would you confirm or eliminate it?*

### 10.1 Two mechanisms, not one

cgroup v2's `cpu` controller offers two fundamentally different things, and conflating
them is the root of most bad container configuration.

**`cpu.weight` — proportional, work-conserving.** From the kernel's cgroup-v2 docs:

> `cpu.weight` — A read-write single value file which exists on non-root cgroups. The
> default is "100". For non idle groups (`cpu.idle = 0`), the weight is in the range
> [1, 10000].

Weight only matters *under contention*. If nobody else wants the CPU, a weight-1 cgroup
gets the whole machine. There is also `cpu.weight.nice`, "an alternative interface for
`cpu.weight` [that] allows reading and setting weight using the same values used by
`nice(2)`" — i.e. the §6.2 weight table, exposed at the cgroup level. This is Kubernetes'
**request**.

**`cpu.max` — absolute, non-work-conserving.** Same doc:

> `cpu.max` — A read-write two value file which exists on non-root cgroups. The default is
> "max 100000". The maximum bandwidth limit. It's in the following format: `$MAX $PERIOD`
> which indicates that the group may consume up to `$MAX` in each `$PERIOD` duration.

Default period 100,000 µs = **100 ms**. This is Kubernetes' **limit**, and it is the one
that hurts. It will idle a CPU rather than let you use it.

Both apply "only [to] processes under the fair-class scheduler" — RT tasks are governed by
the separate RT bandwidth mechanism of §8.3.

### 10.2 The quota model, and why it fails the way it does

From the CFS bandwidth-control documentation:

> Within each given "period" (microseconds), a task group is allocated up to "quota"
> microseconds of CPU time. That quota is assigned to per-cpu run queues in slices as
> threads in the cgroup become runnable. Once all quota has been assigned any additional
> requests for quota will result in those threads being throttled. **Throttled threads will
> not be able to run again until the next period when the quota is replenished.**

The three words that matter are **"per-cpu run queues"**. Quota is not a single global
budget spent smoothly; it is a global pool that gets handed out in slices to individual
CPUs on demand. A multi-threaded runtime runnable on many CPUs at once burns the pool in
parallel.

```
   THE THROTTLING FAILURE MODE
   ═══════════════════════════

   Container: cpu.max = "200000 100000"   (quota 200 ms per 100 ms period
                                            = "2 CPUs")
   Runtime:   16 worker threads, host has 16 CPUs.
   A request needs 40 ms of CPU, spread over 16 threads.

   period N  ├──────────────────── 100 ms wall ─────────────────────┤

   cpu 0     ████████████                                            ← 12.5 ms
   cpu 1     ████████████                                            ← 12.5 ms
   cpu 2     ████████████                                            ← 12.5 ms
   ...       ████████████                                            ← 12.5 ms
   cpu 15    ████████████                                            ← 12.5 ms
             └─── 16 x 12.5 ms = 200 ms of quota, burned in 12.5 ms ──┘
             ▲                    ▲
             │                    └── QUOTA EXHAUSTED at t = 12.5 ms
             │                        ALL 16 THREADS THROTTLED
             │
             │           ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
             │           └────── 87.5 ms of enforced idleness ──────┘
             │                   Nothing runs. Not one thread.
             │
   period N+1├──────────────────── quota replenished ────────────────┤

   WHAT YOUR DASHBOARD SEES:
     CPU utilisation  = 200 ms / (100 ms x 16 cpus) = 12.5%   ← "we're fine"
     p99 latency      = +87.5 ms                              ← "we are not fine"

   The two observations are the SAME EVENT. Utilisation is low BECAUSE
   the kernel is forcibly idling you. This is the signature: low CPU,
   high tail latency, and a tail that is quantised to the period length.
```

The tail being **quantised to the period** is the tell. A throttled service's latency
histogram grows a spike at multiples of ~100 ms, because a throttled thread waits for the
next period boundary, not for a resource. Lock contention does not do that. Slow
downstream dependencies do not do that.

### 10.3 Confirming it: the answer to the checklist question

The evidence is a single file, and it is unambiguous. `cpu.stat` in the cgroup, per the
cgroup-v2 docs, reports:

```
usage_usec      user_usec       system_usec       ← always present
nr_periods      nr_throttled    throttled_usec    ← when the controller is enabled
nr_bursts       burst_usec
```

The procedure:

1. **Read `cpu.stat` twice, N seconds apart, inside the container.**
   `cat /sys/fs/cgroup/cpu.stat` (v2, unified) — for a container this is usually just
   `/sys/fs/cgroup/cpu.stat` because the container sees its own cgroup as the root.
2. **Compute `Δnr_throttled / Δnr_periods`.** This is the fraction of enforcement periods
   in which you hit the wall. Anything non-zero is worth explaining. Anything above ~1% on
   a latency-sensitive service is your bug.
3. **Compute `Δthrottled_usec / Δnr_throttled`.** This is the *mean stall length per
   throttling event*. Compare it to the gap between your p50 and p99. If they match, you
   have your answer.
4. **Cross-check against `cpu.max`.** `$MAX / $PERIOD` is your effective CPU count. Compare
   it to the thread/process count your runtime actually started (§9.3). If you have 64
   workers and a quota of 2, you have found the cause, not just the symptom.
5. **Check `cpu.pressure`.** See §10.5.

**Eliminating it** is the same evidence read the other way: if `Δnr_throttled` is zero
across a window containing your latency spike, throttling is not your problem and you
should stop looking here. That is a real answer, and being able to *rule it out* in thirty
seconds is most of the value.

The bandwidth doc adds a useful calibration for step 2:

> For cgroup cpu constrained applications that are cpu limited this is a relatively moot
> point because they will naturally consume the entirety of their quota […] As a result it is
> expected that `nr_periods` roughly equal `nr_throttled`.

So a batch job pegged at its quota showing `nr_throttled ≈ nr_periods` is *working as
intended*. Throttling is only a bug when it is unexpected, which is why the ratio matters
more than the raw count.

### 10.4 The fixes, in order of preference

1. **Right-size the concurrency to the quota.** If `cpu.max` gives you 2 CPUs, run ~2–3
   workers, not `os.cpu_count()` workers. In Python this means setting `PYTHON_CPU_COUNT`
   or passing explicit pool sizes rather than relying on the default (§9.3). This is nearly
   always the correct fix, and it is free.
2. **Raise the limit, or remove it and rely on `cpu.weight`.** Requests without limits let
   the scheduler do proportional sharing, which is work-conserving. Many production
   Kubernetes guides now recommend exactly this for latency-sensitive services.
3. **Shorten the period.** A 10 ms period with a proportionally smaller quota gives the
   same average throughput with a 10× smaller worst-case stall. The cost is more
   enforcement overhead.
4. **`cpu.max.burst`.** From the docs: "A read-write single value file […] The default is
   '0'. The burst in the range [0, $MAX]." The rationale in the bandwidth doc is
   statistical — it lets you describe a task's demand as a distribution with a p95 and a
   p100 rather than a single worst case, "borrow[ing] time now against our future underrun,
   at the cost of increased interference against the other system users. All nicely
   bounded." Useful for bursty request handlers, not a fix for chronic under-provisioning.

There is also a subtlety in the bandwidth doc that explains "why did we only get throttled
sometimes":

> Once a slice is assigned to a cpu it does not expire. However all but 1ms of the slice may
> be returned to the global pool if all threads on that cpu become unrunnable. […] For
> highly-threaded, non-cpu bound applications this non-expiration nuance allows applications
> to briefly burst past their quota limits by the amount of unused slice on each cpu […]
> typically at most 1ms per cpu.

So a highly-threaded application can hold up to ~1 ms of stranded quota *per CPU it has
touched*. On a 64-core host that is 64 ms of quota parked on runqueues where it is not
being used — which both explains sporadic throttling at apparently-low utilisation and
gives you another reason to constrain thread counts.

### 10.5 PSI: the signal that would have told you sooner

Pressure Stall Information is the metric that was designed for exactly this question. From
the kernel docs:

> The "some" line indicates the share of time in which at least some tasks are stalled on a
> given resource.
>
> The "full" line indicates the share of time in which all non-idle tasks are stalled on a
> given resource simultaneously. In this state actual CPU cycles are going to waste […]
>
> The ratios (in %) are tracked as recent trends over ten, sixty, and three hundred second
> windows […] The total absolute stall time (in us) is tracked and exported as well, to allow
> detection of latency spikes which wouldn't necessarily make a dent in the time averages.

System-wide it is `/proc/pressure/{cpu,memory,io}`; per-cgroup it is `cpu.pressure`,
`memory.pressure`, `io.pressure`. The format:

```
some avg10=0.00 avg60=0.00 avg300=0.00 total=0
full avg10=0.00 avg60=0.00 avg300=0.00 total=0
```

**Why PSI beats utilisation for this problem:** utilisation answers "were the CPUs busy",
which is the wrong question. PSI answers "was anyone waiting", which is the right one. A
throttled container has *low* utilisation and *high* `cpu.pressure some` — the two
together are the fingerprint, and neither alone is.

One caveat straight from the docs, so you do not chase a zero: "**CPU full is undefined at
the system level**, but has been reported since 5.13, so it is set to zero for backward
compatibility." Use `some` for CPU. `full` is meaningful for memory and I/O.

And the `total=` field is the one to alert on. The 10/60/300-second averages will smooth a
200 ms stall into invisibility; `total` is a monotonic microsecond counter, so
`Δtotal / Δwall` over a 5-second window catches spikes that `avg10` cannot see.

---

## 11. Asymmetric cores: when the scheduler picks a *kind* of core

Everything so far assumed that "which CPU you get" affects *when* you run. On a
heterogeneous machine it also affects *how fast you run*, and that is a different
category of problem. The machine used for this document has 5 performance cores and 6
efficiency cores; the E-cores have half the L1d and a quarter of the L2, shared six ways
instead of five ([`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
§2). Same ISA, same binary, very different throughput.

This is worth a section because it is the future, not a curiosity: Intel has shipped P/E
hybrid parts since Alder Lake, ARM's big.LITTLE has been in phones for a decade, and
Linux's Energy Aware Scheduling (EAS) and `uclamp` (`cpu.uclamp.min` / `cpu.uclamp.max` in
the cgroup interface, §10) exist precisely to give the scheduler utilisation hints on such
machines.

### 11.1 A scheduling knob that changes your clock rate

The system here exposes scheduling intent through **QoS classes** rather than priorities.
A thread declares what kind of work it is doing, and the scheduler picks a cluster. The
call is `pthread_set_qos_class_self_np(qos_class_t, int relative_priority)`, reachable
from Python through `ctypes`:

```python
import ctypes, ctypes.util
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
libc.pthread_set_qos_class_self_np.argtypes = [ctypes.c_uint, ctypes.c_int]

QOS_USER_INTERACTIVE = 0x21
QOS_USER_INITIATED   = 0x19
QOS_DEFAULT          = 0x15
QOS_UTILITY          = 0x11
QOS_BACKGROUND       = 0x09

libc.pthread_set_qos_class_self_np(QOS_BACKGROUND, 0)   # from inside the thread
```

`pthread_get_qos_class_np` reads it back, and **every class set below read back correctly
with `rc=0`** *(measured)* — so the failures in the table that follows are not failures to
apply the setting.

### 11.2 Measured: the same workload under five QoS classes

The workload is the deliberately boring integer loop used throughout this folder, run in a
fresh `threading.Thread` that sets its own QoS before starting. `N = 1,000,000` iterations,
best of 5 reps per pass, **median of 5 alternating passes** (the class order is reversed on
odd passes so drift cannot masquerade as an effect). `load1 = 2.08–2.38`.

| QoS class (set per-thread) | min | median | max | spread | vs DEFAULT |
|---|---|---|---|---|---|
| `USER_INTERACTIVE` | 24.04 | 24.23 | 24.30 | 1.01× | 1.01× |
| `USER_INITIATED` | 23.71 | 24.39 | 24.76 | 1.04× | 1.01× |
| `DEFAULT` | 23.80 | 24.08 | 25.48 | 1.07× | 1.00× |
| `UTILITY` | 23.83 | 24.12 | 24.34 | 1.02× | 1.00× |
| `BACKGROUND` | **207.44** | **225.14** | **244.17** | 1.18× | **9.35×** |

*(ns per loop iteration)*

**The scale is binary, not graduated.** Four of the five classes are indistinguishable —
the spread *within* `DEFAULT` (1.07×) is larger than the difference between `DEFAULT` and
`USER_INTERACTIVE`. Only `BACKGROUND` does anything, and what it does is enormous:
**a 9.35× throughput loss with no change to the code.**

A second, independent run comparing every placement knob available:

**Measured**, median of 5 alternating passes, `load1 = 1.91`:

| Knob | min | median | max | vs. none |
|---|---|---|---|---|
| none (inherit) | 24.35 | 24.63 | 24.86 | 1.00× |
| QoS `UTILITY` | 24.18 | 24.26 | 24.83 | 0.98× |
| **QoS `BACKGROUND`** | 116.14 | **162.17** | 179.97 | **6.59×** |
| QoS `BACKGROUND`, `relative_priority = -15` | 103.51 | 147.20 | 173.77 | 5.98× |
| **`os.setpriority(os.PRIO_DARWIN_THREAD, 0, os.PRIO_DARWIN_BG)`** | 106.80 | **146.84** | 199.26 | **5.96×** |
| `os.setpriority(os.PRIO_PROCESS, 0, 20)` (nice +20) | 24.01 | 24.42 | 24.57 | **0.99×** |

Four results, in descending order of usefulness.

**1. There is a pure-stdlib route to the same clamp.** `os.setpriority` with
`os.PRIO_DARWIN_THREAD` and `os.PRIO_DARWIN_BG` — both of which CPython exposes — produced
5.96×, statistically indistinguishable from the `ctypes` QoS call's 6.59×. **You do not
need `ctypes` to demote a Python thread to the efficiency cluster.** I have not seen this
documented anywhere and it is the most directly actionable thing in this section: a
background maintenance thread inside a latency-sensitive Python service can be moved off
the performance cores with one stdlib call.

**2. `nice` does nothing.** 0.99× — inside the noise. `nice` adjusts priority *within* a
scheduling band; it does not select a cluster. This independently reproduces
[`31-measurement-methodology.md`](31-measurement-methodology.md) §3.3's finding at the
process level, now at the thread level: **an engineer who "isolates" a benchmark or
"deprioritizes" a background thread with `nice` on this platform has done nothing at all.**

**3. Relative priority is a no-op.** `relative_priority = -15` within `BACKGROUND` gave
5.98× vs 6.59× for `0` — a difference smaller than the run-to-run spread of either. The
second parameter of `pthread_set_qos_class_self_np` did not measurably matter for this
workload.

**4. The clamps do not stack.** Running the entire script under `taskpolicy -c background`
put the baseline at 132.01 ns — i.e. the whole process was already on the E-cluster — and
every per-thread knob then measured **0.94×–1.11×** relative to it. Once you are clamped,
clamping again does nothing. A clamp is a floor, not an accumulator.

### 11.3 Where this contradicts a sibling document

[`31-measurement-methodology.md`](31-measurement-methodology.md) §3.2 measured the same
cluster migration as **3.19×** (min-aggregated, `taskpolicy -c background` on whole
processes). I measured **6.6×** and **9.4×** in two independent runs of the per-thread
version, and **4.8×** if I aggregate by `min` as doc 31 did (24.35 → 116.14).

I do not think either measurement is wrong. I think the honest conclusion is:

> **The E-cluster penalty is not reproducible to better than about 2× across sessions.**
> The P-core numbers in every table above have a coefficient of variation of 1–7%; the
> E-core numbers move by a factor of two between runs of the identical script minutes
> apart. Six E-cores share a 4 MB L2 with each other *and with every background daemon the
> system parked there*, so the E-cluster result is a measurement of the machine's mood.
> Quote the *direction* and the *order of magnitude* — "clamping to the efficiency cluster
> costs something between 3× and 10×" — and refuse to quote a point estimate.

Doc 31's tables also carry a note that a sustained-load test was running concurrently
during that sweep, which is a second reason the two are not directly comparable. Both
documents agree on everything that matters: `BACKGROUND`/`taskpolicy` relocates you,
`nice` does not, and the effect dwarfs any optimization you are likely to ship.

### 11.4 Linux's version of this problem

Linux does not have QoS classes. Its answer to heterogeneous hardware is
**Energy Aware Scheduling**, which models each CPU's capacity and energy cost and places
tasks to minimize energy subject to meeting utilisation; and **utilisation clamping**,
exposed per-task through `sched_setattr(2)` and per-cgroup through the files documented in
§10:

> `cpu.uclamp.min` — The requested minimum utilization (protection) as a percentage rational
> number, e.g. 12.34 for 12.34%. […] `cpu.uclamp.max` — The requested maximum utilization
> (limit) […] This interface allows reading and setting maximum utilization clamp values
> similar to the `sched_setattr(2)`.

The mental model differs in an instructive way. A QoS class is a *declaration of intent*
that the scheduler translates into placement. A uclamp is a *numeric hint about how busy
this task should be considered*, which feeds the frequency governor and the placement
decision. Both are hints; neither is a guarantee; and on both systems the way to make a
thread run on a fast core is to *avoid marking it as unimportant*, not to mark it as
important — which the table in §11.2 demonstrates precisely, since the four non-background
classes are indistinguishable.

---

## 12. What all of this means for CPython

### 12.1 The GIL's switch interval is not a timeslice

`sys.setswitchinterval()` is described as the interval at which the interpreter switches
threads. It is not a quantum. The mechanism, covered in
[`24-the-gil.md`](24-the-gil.md) §4: a thread that wants the GIL waits on a condition
variable with a timeout of `switchinterval`; if the timeout expires and the GIL has not
changed hands, it sets `gil_drop_request`, and the holding thread drops the GIL at its next
eval-breaker check.

So there are **two independent preemption mechanisms stacked on top of each other**:

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │  PYTHON LEVEL: the GIL                                               │
   │    - waiter times out after switchinterval (default 5 ms)            │
   │    - sets gil_drop_request                                           │
   │    - holder drops at the next eval-breaker check                     │
   │    - check points are the ~22 instructions of doc 30 §4, NOT every   │
   │      bytecode -> a long C call defers the drop indefinitely          │
   └───────────────────────────────┬──────────────────────────────────────┘
                                   │  both threads now runnable
                                   ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │  OS LEVEL: the scheduler                                             │
   │    - has ~11 CPUs and 2 runnable threads: NO CONTENTION AT ALL       │
   │    - preempts a CPU-bound thread ~62 times/second (measured, §4.2)   │
   │    - would happily run both threads in parallel; the GIL forbids it  │
   └──────────────────────────────────────────────────────────────────────┘

   The OS is not the bottleneck and never was. On a GIL build the
   interpreter is a scheduler sitting on top of a scheduler, and the
   inner one is 20x more active than the outer one.
```

### 12.2 Measured: the GIL is 20× the OS preemption rate

Two CPU-bound Python threads, 0.6 s per trial, `ru_nivcsw` differenced and normalized to
per-second. **Measured**, median of 5 trials per setting:

| `sys.setswitchinterval` | vol/s | **invol/s** | vs. the floor |
|---|---|---|---|
| 0.0001 (0.1 ms) | 0 | **30,519** | 372× |
| 0.001 (1 ms) | 0 | 3,388 | 41× |
| **0.005 (default)** | 0 | **1,280** | **16×** |
| 0.05 (50 ms) | 0 | 130 | 1.6× |
| 0.5 (500 ms) | 0 | **82** | 1.0× (floor) |

Read the last row first. At a 500 ms switch interval the GIL essentially stops forcing
handoffs, and the switch rate falls to **82/s** — which is approximately twice the 62/s
that §4.2 measured for a *single* CPU-bound thread with no GIL pressure at all. **That 82
is the OS's own preemption rate.** Everything above it is the GIL.

At the default 5 ms setting the process performs **1,280 context switches per second, of
which ~1,200 are the GIL and ~80 are the operating system.** More than 90% of the
preemption experienced by a multi-threaded CPU-bound Python program is imposed by the
interpreter, not by the kernel.

Two consequences:

- **Tuning the OS scheduler to improve a threaded CPU-bound Python program is treating the
  wrong layer.** The knob that matters by an order of magnitude is `sys.setswitchinterval`.
- **The switch rate scales as ~1/interval, as designed, but not exactly.** At 0.0001 the
  observed 30,519/s is ~3× the naive `2 threads / 0.0001 s = 20,000/s`, and at 0.005 the
  observed 1,280/s is ~3.2× the naive 400/s. The consistent ~3× factor across two decades
  suggests each logical handoff costs more than one accounted switch — plausibly the
  timeout wakeup, the drop, and the re-acquire. I did not isolate this further; see
  [What I could not verify](#what-i-could-not-verify).

Lowering `switchinterval` to "improve responsiveness" therefore buys you 30,000 context
switches per second at ~2.7 µs each (§3.3) — roughly **8% of a core spent purely on
switching**. [`24-the-gil.md`](24-the-gil.md) §4 covers why this usually does not even buy
the responsiveness you wanted.

### 12.3 The experiment I got wrong: the GIL on asymmetric cores

[`30-concurrency-correctness.md`](30-concurrency-correctness.md) §11 raises this and
explicitly declines to measure it:

> **Through the OS scheduler on asymmetric cores.** This machine has 5 performance and 6
> efficiency cores. macOS may schedule a lock-holding thread onto an E-core while a waiter
> sits on a P-core — a hardware analogue of priority inversion. I did not attempt to measure
> this.

So I measured it, using the GIL itself as the contended resource. Two CPU-bound Python
threads, identical work, run for 1.5 s; one at `DEFAULT` QoS (P-cluster), one at
`BACKGROUND` (E-cluster). Work is counted in units of 20,000 loop iterations.

**My prediction was that total throughput would collapse.** The GIL hands out *time*, and
the two threads should split it 50/50 as they do when equal; a thread doing 1/6th the work
per unit of GIL time should therefore drag total throughput down toward half. That
prediction was **wrong**, and the way it was wrong is the interesting part.

**Measured**, median of 5 alternating passes, `load1 = 1.95`:

| Case | A units | B units | total | A on-CPU | B on-CPU |
|---|---|---|---|---|---|
| solo, `DEFAULT` | 2395 | — | 2395 | 1.00 | — |
| solo, `BACKGROUND` | 344 | — | 344 | 1.00 | — |
| 2 threads, both `DEFAULT` | 1194 | 1199 | **2393** | 0.50 | 0.50 |
| 2 threads, `DEFAULT` + `BACKGROUND` | **2199** | **39** | **2238** | 0.93 | 0.13 |

Row 3 is the control and it is textbook: two equal threads split the GIL **1194 / 1199**,
on-CPU 0.50 / 0.50, and total throughput equals the solo figure to within 0.1%. The GIL's
handoff between equals is almost perfectly fair.

Row 4 is the result:

- **The P-core thread kept 92% of its solo throughput** (2199 of 2395). It lost 8.2%, not
  50%.
- **The E-core thread got 39 units — 1.7% of the total work.**
- **Total throughput fell only 6.6%** (2395 → 2238).
- Most striking: **the E-core thread would have completed 344 units running alone on an
  E-core. In competition it completed 39 — a 8.8× starvation factor on top of the 7× core
  penalty it was already paying.**

**Why my prediction failed.** The GIL is not a round-robin time slicer. The handoff is
driven by `gil_drop_request`, which a waiter sets only *after its timeout expires*, and the
dropping thread is then free to re-acquire immediately. Between equals that produces
alternation. Between a fast thread and a slow one it produces something closer to
winner-take-all: the P-core thread reaches its eval-breaker check points ~7× more often, so
it is far more often the thread positioned to grab the GIL after a drop. The scheduler and
the GIL compose into a system that is *throughput-preserving* and *badly unfair*.

**The transferable lesson**, and it is the answer doc 30 §11 was looking for:

> A hardware priority inversion on asymmetric cores does not manifest as "everything gets
> slower". It manifests as **the thread on the slow core being starved**, while aggregate
> throughput stays healthy. If you demote a Python thread to the efficiency cluster, do not
> assume it will merely run 6× slower — under GIL contention it may effectively stop. This
> is a genuine hazard for the §11.2 recommendation: demoting a background thread is safe
> only if that thread has no deadline.

One honesty note: the on-CPU fractions in row 4 sum to 1.06, which is impossible for a
serialized resource. The overshoot is small and consistent; I could not account for it, and
it is recorded in [What I could not verify](#what-i-could-not-verify). It does not affect
the unit counts, which are the load-bearing numbers.

### 12.4 The convoy, and why it belongs to the scheduler

[`24-the-gil.md`](24-the-gil.md) §7 measures the convoy effect and
[`30-concurrency-correctness.md`](30-concurrency-correctness.md) §9 measures lock
convoying at 0.37× on 16 threads. The scheduler-level statement of the same phenomenon, in
this document's vocabulary: **a convoy forms when the cost of transferring a resource
exceeds the work done while holding it.** §3.3 puts a number on the transfer — ~2.7 µs per
switch, ~5.4 µs per handoff round trip. Any critical section shorter than that is spending
more time being handed over than being used. That is the whole mechanism, and it is why
"make the critical section shorter" stops helping below a threshold and you must instead
make it *less frequent* or *less shared*.

### 12.5 Free-threading changes the scheduler's job

On a GIL build the OS scheduler is barely employed: N Python threads produce at most one
runnable thread's worth of work, and §12.2's floor of 82 involuntary switches/s reflects
that. Remove the GIL and the picture inverts:

- **N runnable threads instead of 1.** The OS scheduler starts doing real work: load
  balancing, migration decisions, and — on a cgroup-limited host — burning quota N times
  faster (§10.2). **A service that was never throttled on a GIL build can start throttling
  the day it moves to free-threading, at identical request throughput,** because it can now
  actually use the cores it was configured for.
- **Lock contention becomes real.** §4.3 row 6 measured 2000 contended `Lock` acquisitions
  producing 14 context switches on a GIL build. On a free-threaded build those become
  genuine parks and wakeups at ~2.7 µs each — which is exactly the 0.37× collapse
  [`30-concurrency-correctness.md`](30-concurrency-correctness.md) §9 measures.
- **Stop-the-world GC becomes a scheduler event.** The free-threaded cycle collector must
  pause every thread ([`22-garbage-collection.md`](22-garbage-collection.md),
  [`26-free-threading.md`](26-free-threading.md)), which means N wakeups and N parks per
  collection instead of zero.

The correct summary is not "free-threading is faster" but **"free-threading moves your
bottleneck from the interpreter's scheduler to the operating system's."** Everything in
§§3, 9 and 10 becomes load-bearing for a free-threaded deployment in a way it simply was
not before. See [`26-free-threading.md`](26-free-threading.md) for the measured
single-thread tax and the sharing wall.

### 12.6 Cooperative scheduling as the alternative

[`30-concurrency-correctness.md`](30-concurrency-correctness.md) §13 measures the trade
directly and there is no reason to repeat it here: asyncio's p99 tick latency under a
300 ms blocking call is **306 ms** against threads' **7.58 ms** — a 40× difference, and
that 7.58 ms is "roughly the GIL switch interval (5 ms) plus scheduling noise". Read
against §12.2, that number is now fully explained: the threaded version's tail is bounded
because *something* preempts, and the something is the GIL at 5 ms rather than the OS at
16 ms. Cooperative scheduling has no such bound, by construction.

---

## 13. Three clocks, three questions

Separating on-CPU time from wall time is the cheapest diagnostic in this document and the
most under-used. Python gives you three clocks and they answer three different questions.

**Measured** clock properties on this machine:

| Clock | Implementation | Resolution | Question it answers |
|---|---|---|---|
| `time.perf_counter` | `mach_absolute_time()` | 41.7 ns | How long did this take? |
| `time.process_time` | `clock_gettime(CLOCK_PROCESS_CPUTIME_ID)` | 1.00 µs | How much CPU did **the whole process** use? |
| `time.thread_time` | `clock_gettime(CLOCK_THREAD_CPUTIME_ID)` | 41.7 ns | How much CPU did **this thread** use? |

A three-line demonstration: two threads each run 3,000,000 loop iterations, then the main
thread sleeps 250 ms. **Measured**:

```
perf_counter (wall)         = 0.4056 s
process_time (all threads)  = 0.1518 s
thread_time  (this thread)  = 0.0003 s
```

Every number is a different fact. Wall time includes the sleep. `process_time` is 0.152 s
— the *summed* CPU of both worker threads, and note that it is close to what one thread's
work would cost, because the GIL serialized them. `thread_time` is ~0, correctly reporting
that the calling thread did nothing but wait.

**The diagnostic that follows directly:**

```
    cpu_ratio = process_time_delta / perf_counter_delta

    ratio ≈ 1.0   →  CPU-bound. Profile the code.       (§4.2 row 1: 1.000)
    ratio ≈ 0.0   →  blocked on something. Find it.     (§4.2 row 2: 0.011)
    ratio ≈ 0.5   →  half waiting, half computing.      (§4.2 row 4: 0.447)
    ratio > 1.0   →  genuinely parallel across threads (free-threaded build,
                     or a C extension that released the GIL).
```

That last case is the one worth internalizing: **on a GIL build, `process_time / wall > 1`
is proof that native code released the GIL.** It is the cheapest possible test of the
promise a C extension makes in [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md).

Note also that `resource.RUSAGE_THREAD` **does not exist on this platform** *(measured)*,
which is why `time.thread_time` rather than a per-thread rusage is the tool for
attributing CPU to a thread, and why the §4.2 counters are necessarily whole-process
numbers.

---

## 14. A diagnostic playbook

Ordered by cost. Stop as soon as you have an answer.

| # | Observation | Likely cause | Confirm with |
|---|---|---|---|
| 1 | `cpu/wall ≈ 1`, one thread, involuntary/s ≈ tick rate | healthy CPU-bound work | profile it ([`32-profiling.md`](32-profiling.md)) |
| 2 | `cpu/wall ≈ 1`, many threads, involuntary/s in the thousands | GIL handoffs, not the OS | sweep `sys.setswitchinterval` (§12.2) |
| 3 | `cpu/wall ≪ 1`, high voluntary/s | blocked on I/O or a lock | off-CPU analysis; the wait is the answer |
| 4 | `cpu/wall ≪ 1`, **low** voluntary *and* involuntary, high p99 | **throttled** | `Δnr_throttled / Δnr_periods` in `cpu.stat` (§10.3) |
| 5 | p99 spikes quantised to ~100 ms | cgroup quota period | `cpu.max`, then `cpu.pressure` (§10.5) |
| 6 | p99 spikes quantised to the length of one blocking call | cooperative starvation | doc 30 §13; loop-lag monitor in doc 29 |
| 7 | perfectly periodic stalls at 1 s intervals | RT throttling | `sched_rt_runtime_us` (§8.3) |
| 8 | throughput collapses as threads are added | convoy | doc 30 §9; measure critical-section length against §3.3's 2.7 µs |
| 9 | one thread makes no progress while others are fine | starvation on a slow core, or GIL unfairness | §12.3 |
| 10 | worker count ≫ effective CPU count | default pool sizing vs. quota | `os.process_cpu_count()` vs `cpu.max` (§9.3, §10.4) |

Row 4 is the one people miss, because *every* instinct says "low CPU means not CPU-bound,
look elsewhere". Throttling is the case where low CPU utilisation is the *symptom* of a CPU
problem rather than evidence against one.

---

## Lab exercises

1. **Reproduce the creation ladder (§2).** Time `threading.Thread`, `os.fork`,
   `subprocess.run` of a trivial binary, `subprocess.run` of `python -c pass`, and
   `mp.Process` under each of `mp.get_all_start_methods()`. Median of 5 alternating passes.
   Then compute the break-even work-item size at which a process pool beats a thread pool.
   *Expected shape: thread ~25 µs, fork ~0.8 ms, spawn ~25 ms.*

2. **Measure your own context-switch cost (§3.3).** Socketpair ping-pong between two
   threads and between two forked processes, with a no-switch syscall baseline to subtract.
   Report ns per switch and the process/thread ratio. **Then predict** the ratio before you
   run it, and write down why you were wrong if you were. *If your ratio is much above
   1.1×, find out whether your CPU has PCID/ASID.*

3. **Build the workload-signature table (§4.2).** Four shapes — tight loop, sleep loop,
   socketpair loop, mixed — instrumented with `ru_nvcsw`/`ru_nivcsw` and `cpu/wall`. Then
   run the primitive probe of §4.3 and decide, from evidence, whether your kernel's
   voluntary/involuntary classification means what you assumed.

4. **Find your OS preemption rate.** One CPU-bound thread, no other load, `ru_nivcsw` per
   second. Compare it to `CONFIG_HZ` (Linux) or `kern.clockrate` (here). *Measured here:
   62/s against a 100 Hz statclock.* Explain the gap.

5. **The `switchinterval` sweep (§12.2).** Two CPU-bound threads; sweep
   `sys.setswitchinterval` across four decades; plot involuntary switches/s. Identify the
   floor and argue that the floor is the OS. Then compute the CPU cost of the switches at
   the lowest setting using your own §3.3 number.

6. **Starve a thread on a slow core (§12.3).** If you have a heterogeneous machine, run the
   two-thread asymmetric experiment. If you have a homogeneous one, simulate it with
   `nice` — and then explain why `nice` produces a *different* result than a slow core does,
   given that both make one thread "slower".

7. **Throttle yourself deliberately.** On a Linux box, create a cgroup with
   `cpu.max = "10000 100000"` (10% of one CPU), run a multi-threaded CPU-bound Python
   program in it, and watch `cpu.stat` and `cpu.pressure`. Record: the utilisation your
   monitoring would show, the p99 latency, and `Δthrottled_usec / Δnr_throttled`. **Then
   halve the period and repeat.** The average throughput should be unchanged and the tail
   should halve. That single experiment teaches §10 better than the section does.

8. **Prove the pool-sizing bug.** In the same cgroup, start a `ProcessPoolExecutor` with no
   `max_workers`. Count the processes. Compare to `cpu.max / period`. Then set
   `PYTHON_CPU_COUNT` and measure the p99 difference.

9. **Verify a C extension's GIL promise (§13).** Take any native library you depend on, run
   a heavy call on N threads, and check whether `process_time / perf_counter` exceeds 1.0.
   If it does not, the extension holds the GIL and your thread pool is decorative.

10. **Read the weight table (§6.2).** Without running anything, compute the CPU split for:
    two tasks at nice 0 and nice 3; three tasks at nice −5, 0, +5; and a nice −20 task
    against ten nice 0 tasks. Then verify one of them on a Linux box with `taskset` to a
    single CPU and two spinners. *The arithmetic is the point; the verification is the
    reward.*

---

## Question bank

1. Linux has `fork()` and `pthread_create()`. How many kinds of schedulable object does the
   kernel have? *(One. `task_struct`. The difference is `clone()` flags.)*
2. `clone()` rejects `CLONE_THREAD` without `CLONE_SIGHAND`, and `CLONE_SIGHAND` without
   `CLONE_VM`. What does that tell you about where signal disposition lives?
3. You are told process context switches are "much more expensive" than thread switches.
   Give the historical reason, the modern hardware feature that undermines it, and a
   measurement that would settle it. *(TLB flush; PCID/ASID; §3.3 — 1.06× here.)*
4. Under what circumstances *is* a process switch genuinely much more expensive than a
   thread switch? *(Large, distinct working sets — the indirect cost of §3.2, which scales
   with footprint, not with the switch mechanism.)*
5. Your service shows 12% CPU utilisation and a 90 ms p99. Name the top candidate and the
   single file that confirms or eliminates it in thirty seconds.
6. Distinguish `cpu.weight` from `cpu.max` in one sentence each, and say which one a
   Kubernetes *limit* maps to.
7. Why does throttling get *worse* as you add worker threads, at constant request rate?
   *(Quota is spent in parallel across per-CPU runqueues; the pool drains sooner into a
   longer forced-idle tail.)*
8. Why would shortening the cgroup period improve p99 at unchanged average throughput?
9. What does `nr_throttled ≈ nr_periods` mean, and when is it *not* a bug?
10. Explain `vruntime`. Now explain why nice 0 is 1024 and why adjacent nice levels differ
    by a factor of ~1.25.
11. Two CPU-bound tasks at nice 0 and nice 5. Compute the split. *(1024 : 335 → 75.4% /
    24.6%.)*
12. EEVDF has two gates where CFS had one. Name both, say which guarantee each provides, and
    explain why CFS needed heuristics that EEVDF does not.
13. A task wants 2% of the CPU but within 5 ms. Explain why `nice` cannot express that and
    how EEVDF's request size can.
14. Why is "sleep briefly to reset your lag" an exploit, and what stops it?
15. What does `SCHED_DEADLINE` give you that `SCHED_FIFO` cannot? *(Admission control — it
    refuses an unschedulable set instead of failing silently — plus CBS overrun
    containment.)*
16. Your `SCHED_FIFO` service stalls for exactly 50 ms once per second. Diagnose it.
17. Is a CPU affinity mask a process attribute or a thread attribute? What happens to it
    across `fork` and `exec`? What silently modifies it?
18. `os.cpu_count()`, `os.process_cpu_count()`, `len(os.sched_getaffinity(0))` — three
    different questions. State each, and say which one determines your default
    `ThreadPoolExecutor` size.
19. On a GIL build, two CPU-bound threads. Is `sys.setswitchinterval` or the OS timeslice the
    dominant source of context switches? Justify with an order of magnitude. *(The GIL, by
    ~16× at default settings — §12.2.)*
20. You set `sys.setswitchinterval(0.0001)` to improve responsiveness. Estimate the CPU cost
    using your machine's per-switch number.
21. Two Python threads, one clamped to a slow core. Predict what happens to (a) total
    throughput and (b) each thread's throughput. *(Total barely moves; the slow thread is
    starved ~9× beyond its core penalty — §12.3. Most people, including the author, predict
    the opposite.)*
22. A service is never throttled on a GIL build. It is migrated unchanged to a
    free-threaded build at identical request throughput and starts throttling. Explain.
23. `process_time / perf_counter` for your process is 3.4 on a GIL build. What have you
    learned?
24. `ru_nivcsw` is described as "involuntary context switches, i.e. preemptions". Give a
    workload where that description is measurably false, and say what you would do before
    building an alert on it.
25. You measure a 3.2× effect one week and a 9.4× effect the next, same script, same
    machine. What do you publish?

---

## What I could not verify

This section is deliberately long, because the gaps are as informative as the results.

**Linux, as a class.** Nothing in §§5–10 was executed. The authoring machine runs Darwin
on arm64, and every Linux statement — EEVDF's algorithm and status, the CFS weight table,
`SCHED_DEADLINE`'s admission control, RT throttling defaults, `sched_setaffinity`
semantics, cgroup v2's `cpu.max`/`cpu.stat`/`cpu.pressure` behaviour, PSI's format — is
quoted from kernel source at `git.kernel.org`, `docs.kernel.org`, or `man7.org` and
attributed inline. I have not observed a single throttling event, an `nr_throttled`
counter, or an EEVDF scheduling decision with my own instruments. The numbers I quote from
kernel source (`sysctl_sched_base_slice = 700000`, `sysctl_sched_migration_cost = 500000`,
`sched_prio_to_weight[]`, `sched_rt_runtime_us = 950000`) are *literal source constants*,
which is the strongest form of evidence available without a machine, but they are still
not measurements. Treat §10's playbook as a well-sourced procedure that I have not
personally executed end to end.

**The kernel version the constants come from.** `docs.kernel.org` served pages labelled
`7.2.0-rc5` during this session, and the raw source files were fetched from mainline
`master`. `sysctl_sched_base_slice` was 750,000 ns (0.75 ms) when EEVDF was merged and is
700,000 ns in the tree I read. **Check the constant on your kernel; do not quote mine.**

**The exact wording of EEVDF's release history.** The kernel documentation says Linux
"began transitioning to EEVDF in version 6.6 (as a new option in 2024)", which is internally
ambiguous — 6.6 was released in 2023. I quote the doc verbatim rather than paraphrasing it
into a cleaner but possibly wrong claim. What I verified independently is that
`DELAY_DEQUEUE` and `DELAY_ZERO` appear in `kernel/sched/features.h` with a commit dated
**2024-08-17 by Peter Zijlstra**, and that both are `true` by default in mainline today. I
did *not* verify which release tag first contained them.

**Why `time.sleep` is booked as involuntary (§4.3).** I established the *pattern*
empirically across six primitives with a clean discriminating case (row 5: the same socket
read flips classification purely by adding a timeout). I did **not** read the kernel's
rusage accounting site to confirm the mechanism. My best hypothesis — that the BSD socket
wait path is counted voluntary while lower-level sleep/ulock/poll-with-deadline waits are
counted involuntary — is consistent with all six rows but is a hypothesis, not a verified
mechanism. The negative result stands regardless: the counter does not mean "preemption"
here.

**The ~3× factor in the `switchinterval` sweep (§12.2).** Observed switch rates are
consistently ~3× the naive `n_threads / interval` prediction across two decades of
interval. I did not instrument the GIL handoff to determine whether each logical handoff
costs three accounted switches, or whether the accounting anomaly of §4.3 inflates the
count. Both are plausible; I did not distinguish them.

**On-CPU fractions summing to 1.06 in §12.3.** In the asymmetric two-thread trial,
`thread_time`-derived on-CPU fractions were 0.93 and 0.13, summing to more than one for a
GIL-serialized process. The overshoot was small and consistent across passes. Candidates I
did not eliminate: a window during GIL handoff where both threads are briefly accounted
on-CPU, or `CLOCK_THREAD_CPUTIME_ID` behaving differently for a thread on an efficiency
core. The unit counts — which are the load-bearing numbers in that table — are unaffected.

**The E-cluster penalty is not a reproducible number (§11.3).** Two runs of the identical
script minutes apart gave 6.59× and 9.35×;
[`31-measurement-methodology.md`](31-measurement-methodology.md) §3.2 gives 3.19× by a
different route. I report a range, not a point estimate, and I do not know which of load,
L2 contention from background daemons, or thermal state dominates the variance.

**`relative_priority` within a QoS class.** Measured as a no-op (`-15` vs `0` differed by
less than the run-to-run spread) *for a pure-CPU workload with no competing threads in the
same class*. It may well matter under contention within a class; I did not construct that
test.

**Whether `PRIO_DARWIN_THREAD` and QoS `BACKGROUND` are the same mechanism.** They produced
statistically indistinguishable results (5.96× vs 6.59×, overlapping ranges) and neither
stacked on top of `taskpolicy -c background`. That is consistent with one underlying clamp
but does not prove it. I did not read the kernel to confirm.

**Cache and TLB effects of a context switch (§3.2).** I asserted the mechanism and quoted
the kernel's own `sysctl_sched_migration_cost` estimate. I did **not** measure it. Doing so
requires PMU counters, and there is no `perf(1)` on this machine — a limitation
[`31-measurement-methodology.md`](31-measurement-methodology.md) §3.3 already documents.
The §3.3 measurement therefore captures the *direct* switch cost well and the *indirect*
cost essentially not at all.

**Everything measured here is single-machine, single-session, under load between 1.84 and
3.65.** The P-core numbers are stable to 1–7%; the E-core numbers are not stable at all.
Nothing here was run on a quiet machine, and no claim in this document should be carried
into a capacity model without re-measuring on the target.

---

## Sources

**Kernel source** (fetched this session from `git.kernel.org`, mainline `master`)

- `kernel/sched/core.c` — `sched_prio_to_weight[40]`, `sched_prio_to_wmult[40]`.
  *Verdict: primary and definitive for §6.2. The weight table is the whole of "what nice
  does" and it is 10 lines long. Read it once and you never need a blog post about nice
  again.*
- `kernel/sched/fair.c` — `sysctl_sched_base_slice = 700000ULL`,
  `sysctl_sched_migration_cost = 500000UL`, `update_deadline()`, the `PREEMPT_SHORT` check.
  *Verdict: the two constants are the most useful numbers in this document's Linux half —
  0.7 ms of intended slice and 0.5 ms of cache value. Both are one grep away and neither is
  what people guess.*
- `kernel/sched/features.h` — `PLACE_LAG`, `RUN_TO_PARITY`, `PREEMPT_SHORT`,
  `DELAY_DEQUEUE`, `DELAY_ZERO`, `PICK_BUDDY`, `CACHE_HOT_BUDDY`.
  *Verdict: the fastest way to find out what your scheduler actually does today. Each
  feature is a one-line, dated answer to "did that proposal land?"*

**Kernel documentation** (`docs.kernel.org`)

- *EEVDF Scheduler* (`scheduler/sched-eevdf.rst`).
  *Verdict: short, current, and the authoritative statement of lag/eligibility/virtual
  deadline. Its release-history sentence is ambiguous (see What I could not verify); its
  algorithm description is not.*
- *CFS Scheduler* (`scheduler/sched-design-CFS.rst`).
  *Verdict: still worth reading even though CFS is superseded, because every tunable and
  every piece of institutional folklore is phrased in its vocabulary. The "ideal
  multi-tasking CPU" framing is genuinely the clearest one-paragraph explanation of
  `vruntime` anywhere.*
- *CFS Bandwidth Control* (`scheduler/sched-bwc.rst`).
  *Verdict: the single most important document for §10, and badly under-read. The
  "Caveats" section — quota slices don't expire, ~1 ms strandable per CPU — explains the
  sporadic-throttling-at-low-utilisation cases that otherwise look like magic.*
- *Control Group v2* (`admin-guide/cgroup-v2.rst`), CPU Interface Files.
  *Verdict: definitive for `cpu.max`, `cpu.weight`, `cpu.stat`, `cpu.max.burst`,
  `cpu.uclamp.*`, `cpu.idle`. Note the repeated qualifier "affects only processes under the
  fair-class scheduler" — it is the sentence that explains why cgroup limits don't restrain
  RT tasks.*
- *PSI — Pressure Stall Information* (`accounting/psi.rst`).
  *Verdict: read it before you build another utilisation dashboard. The some/full
  distinction and the warning that CPU `full` is undefined system-wide will both save you a
  false conclusion.*

**Man pages** (`man7.org`, Kerrisk)

- `sched(7)`.
  *Verdict: the best single overview of the policy model, the RT throttling defaults, the
  nice-range history, and autogroup. If you read one thing from this list, read this.*
- `clone(2)`.
  *Verdict: reference, not a read-through — but the `ERRORS` section is unexpectedly
  educational, because the illegal flag combinations map out the kernel's real abstraction
  boundaries.*
- `sched_setaffinity(2)`.
  *Verdict: precise about the three things people get wrong — per-thread not per-process,
  inherited across fork/exec, and silently intersected with cpusets.*

**LWN**

- Jonathan Corbet, *An EEVDF CPU scheduler for Linux* (LWN #925371, 2023).
  *Verdict: the clearest explanation of why EEVDF is the right answer rather than merely a
  different one. The paragraph on shorter slices producing earlier deadlines — same CPU
  total, more responsive — is the insight the kernel docs state but do not motivate.*
- LWN #969062, cited by the kernel's EEVDF doc as reference [3] for the completion work.
  *Verdict: cited on the kernel documentation's authority; I did not read it directly this
  session.*
- Ion Stoica & Hussein Abdel-Wahab, *Earliest Eligible Virtual Deadline First* (1995),
  cited by the kernel doc as reference [1].
  *Verdict: the primary source, thirty years old, and worth knowing exists — Linux's newest
  scheduler is an old paper finally implemented.*

**Books** (per [BOOKS.md](BOOKS.md))

- Arpaci-Dusseau, *Operating Systems: Three Easy Pieces* — free. Chapters 7–10 cover
  scheduling from FIFO through MLFQ to lottery/stride, which is the conceptual ladder EEVDF
  sits at the top of.
  *Verdict: read this before this document, not after. It is the only OS text that makes
  scheduling feel inevitable rather than arbitrary.*
- Kerrisk, *The Linux Programming Interface* — reference for `clone`, the scheduling API,
  and process/thread semantics.
  *Verdict: the man pages above are extracted from the same understanding; use TLPI when a
  man page assumes something you don't have.*
- Gregg, *Systems Performance* 2e, ch. 6 (CPUs).
  *Verdict: the source of the voluntary/involuntary methodology in §4 — which §4.3 then
  measures and partially contradicts on this platform. The methodology is right; the
  counter's portability is the part to verify yourself.*

**CPython source**

- `Lib/multiprocessing/context.py`, 3.14 (read locally).
  *Verdict: the default-start-method logic with the `gh-84559` comment is four lines and
  settles the question definitively for whatever version you actually have installed. Read
  yours.*

**Sibling documents in this folder**

- [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §2 — the
  P/E topology and cache sizes this document's §3.2 and §11 depend on.
- [`24-the-gil.md`](24-the-gil.md) §4, §7 — the handoff protocol and the convoy effect,
  which §12 approaches from the OS side.
- [`26-free-threading.md`](26-free-threading.md) — the measured single-thread tax and the
  sharing wall behind §12.5.
- [`30-concurrency-correctness.md`](30-concurrency-correctness.md) §9 (convoy),
  §10 (starvation as a GIL artifact), §11 (priority inversion — §12.3 answers the question
  it left open), §13 (cooperative vs preemptive).
- [`31-measurement-methodology.md`](31-measurement-methodology.md) §3 — the cluster tax and
  the noise floor every number here had to clear; §11.3 records where we disagree.

---

*Next: [`07-virtual-memory.md`](07-virtual-memory.md) — page tables, minor and major
faults, `mmap`, copy-on-write, overcommit and the OOM killer, and the RSS/VSZ/PSS/USS
distinction. The scheduler decides* when *your thread runs; virtual memory decides how
much it costs when it does. The 500 µs `sysctl_sched_migration_cost` of §3.2 is a
statement about caches; the next document is the one where the TLB, the page fault, and
the copy-on-write write-amplification that makes `fork()` expensive get their own
measurements.*
