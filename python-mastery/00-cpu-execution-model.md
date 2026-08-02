# 00 — The CPU execution model: pipelines, speculation, and what "one instruction" costs

> **Tier 0, doc 00.** Prerequisites: none — this is the floor of the curriculum. Feeds
> directly into [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
> (which assumes you know why a stall isn't always a stall),
> [`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md) (reordering, which
> starts here), [`06-processes-threads-scheduling.md`](06-processes-threads-scheduling.md)
> (context switches as pipeline events), [`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md)
> and [`20-eval-loop.md`](20-eval-loop.md) (dispatch, specialization, and the whole
> interpreter-branch-prediction argument), [`24-the-gil.md`](24-the-gil.md),
> [`31-measurement-methodology.md`](31-measurement-methodology.md), and
> `33-optimizing-python.md`.
>
> **The thesis of this document:** *the CPU does not execute your instructions.* It
> decodes them into an internal dataflow graph, renames away most of the dependencies you
> think you wrote, speculates aggressively about what comes next, executes whatever is
> ready in whatever order it likes, and then retires the results in program order so the
> illusion holds. Performance is therefore **not** a function of how many instructions you
> execute. It is a function of how well your instruction stream feeds that machine —
> whether its predictions come true, and whether its dependency chains are short.
> CPython's eval loop is, structurally, close to the worst input you can hand it, and
> essentially every optimization in modern CPython (PEP 659 in particular) is a scheme to
> make it a better one. **You cannot understand the eval loop until you understand this
> document.**

## Contents

1. [The model you have, and why it's wrong](#1-the-model-you-have-and-why-its-wrong)
2. [The pipeline](#2-the-pipeline)
3. [Superscalar, out-of-order, and the real machine](#3-superscalar-out-of-order-and-the-real-machine)
4. [Two machines, side by side](#4-two-machines-side-by-side)
5. [IPC: what it measures and what it hides](#5-ipc-what-it-measures-and-what-it-hides)
6. [Branch prediction](#6-branch-prediction)
7. [Speculation beyond branches](#7-speculation-beyond-branches)
8. [Decode, µops, and the x86 tax](#8-decode-µops-and-the-x86-tax)
9. [Execution ports: why instruction *count* is a bad model](#9-execution-ports-why-instruction-count-is-a-bad-model)
10. [SMT — and the machine that doesn't have it](#10-smt--and-the-machine-that-doesnt-have-it)
11. [Interrupts, exceptions, and context switches as pipeline events](#11-interrupts-exceptions-and-context-switches-as-pipeline-events)
12. [What all of this means for CPython](#12-what-all-of-this-means-for-cpython)
13. [Lab exercises](#13-lab-exercises)
14. [Question bank](#14-question-bank)
15. [Sources](#15-sources)

---

## 1. The model you have, and why it's wrong

Almost everyone carries a mental model that goes: the CPU fetches an instruction, does it,
fetches the next one, does it. Instructions cost cycles; add up the cycles; that's your
runtime. This model is not merely imprecise — it is wrong in a way that inverts the
conclusions you'll draw.

Four things break it, and each gets a section below.

**It executes many instructions at once (§3).** A modern high-performance core sustains
6–8 instructions *retired* per cycle at peak. So the same "number of instructions" can
cost 1 cycle or 100 depending on how they relate to each other.

**It executes them out of order (§3).** The core maintains a window of a thousand-plus
in-flight instructions and issues each one the moment its inputs are ready. Program order
is reconstructed only at retirement, for the benefit of you and the exception model.

**It executes instructions it hasn't decided to run yet (§6, §7).** Every branch is
predicted, and work proceeds down the predicted path speculatively. Modern cores also
speculate on memory: whether a load aliases an earlier store, what address a load will
use, and — on Apple's M3 and later — *what value a load will return*.

**It is not the bottleneck (§5).** Execution units are rarely the limit. As Chester Lam
puts it in the Golden Cove analysis: "Execution units are rarely a bottleneck on modern
CPUs. Usually, branch prediction along with cache and memory latency are limiting
factors." Your instruction stream's job is to keep a large, fast, speculative machine fed.
Most of the time it fails, and the machine idles.

The correct replacement model, in one sentence:

> **The CPU is a dataflow engine wearing a von Neumann mask.** It converts your sequential
> instruction stream into a dependency graph, executes the graph as parallel-ly as the
> graph allows, and guesses at anything that would otherwise serialize it.

Everything you can do to make code fast reduces to one of three things: **shorten the
dependency chains**, **make the guesses come true**, or **stop touching memory the machine
can't predict** (that last is doc 01's subject).

### 1.1 Why a Python programmer should care

Because Python doesn't insulate you from this — it *amplifies* it.

Every Python-level operation costs tens to hundreds of machine instructions, so the
constant factor lives entirely at this level. And the shape of those instructions is
hostile: the eval loop is one giant indirect branch executed hundreds of millions of times
(§12.1), refcounting is a serialized read-modify-write chain on shared cache lines
(§12.4), and every operand is a pointer to a heap object whose address the prefetcher
cannot guess (doc 01). When people say "Python is 50× slower than C," this document and
doc 01 are jointly the explanation. Neither one alone is.

---

## 2. The pipeline

### 2.1 The classic five stages

The textbook RISC pipeline splits instruction processing into stages that can run
concurrently on *different* instructions:

```
      IF   →   ID   →   EX   →   MEM   →   WB
    fetch   decode  execute   memory   writeback
```

Cycle 1 fetches instruction A. Cycle 2 decodes A while fetching B. At steady state, five
instructions are in flight and one completes per cycle. The pipeline doesn't make any
single instruction faster — **latency is unchanged, throughput improves 5×.** That
distinction (latency vs throughput) recurs at every level of this curriculum; it's the
same distinction doc 01 §1.1 makes about memory-level parallelism.

Three things break the steady state — the classic **hazards**:

| Hazard | Cause | Modern fix |
|---|---|---|
| **Structural** | Two instructions want the same unit | More units (§9), better scheduling |
| **Data** | B needs A's result before A produces it | Forwarding/bypass; register renaming (§3.2); OoO execution |
| **Control** | We don't know what to fetch until a branch resolves | **Branch prediction + speculation (§6)** |

### 2.2 Real pipelines are much deeper — and that's the whole problem

Nobody ships a 5-stage pipeline in a performance core. Fetch alone takes several cycles;
x86 decode takes several more; rename, dispatch, schedule, execute, and retire each add
stages. Depth buys clock frequency (less work per stage → shorter cycle) and costs you on
every misprediction, because a misprediction throws away everything in flight behind the
branch.

The historic extreme is instructive. Intel's Pentium 4 (NetBurst) reached 20 stages, and
its Prescott revision 31 stages, chasing clock speed. Its mispredict penalty was roughly
20 and 30+ cycles respectively, and the design was abandoned — the frequency it bought
did not pay for the speculation it wasted. The lesson generalizes: **pipeline depth is a
bet that your branches are predictable.**

Contemporary numbers, measured rather than claimed:

| Core | Branch misprediction penalty |
|---|---|
| Apple Firestorm (M1 P-core) | **13 cycles** ([7-cpu.com](https://www.7-cpu.com/cpu/Apple_M1.html)) |
| Intel Skylake, µop-cache hit | **~16.5 cycles** ([7-cpu.com](https://www.7-cpu.com/cpu/Skylake.html)) |
| Intel Skylake, µop-cache miss | **19–20 cycles** (the extra is re-decode — §8) |
| Pentium 4 Prescott (historical) | ~30+ cycles |

Note the Skylake pair. **The same mispredict costs 16.5 or 20 cycles depending on whether
the recovery path hits the µop cache.** The penalty isn't a property of the branch; it's a
property of the branch *plus* the front-end state you left behind. That's the first hint
that this machine has no single "cost" for anything.

### 2.3 What a mispredict actually costs you

The cycle count above is the *minimum* — the refill time for an otherwise-perfect
front end. Multiply it by width to get the real damage:

> On an 8-wide machine, a 13-cycle bubble is **~104 instruction slots discarded**. On
> Golden Cove (6-wide alloc, ~17-cycle recovery) it's ~100. A mispredict is not "13 cycles
> lost," it is "one hundred instructions of potential thrown away."

This is why §6 gets more space than any other section, and why the interpreter-dispatch
folklore in §12.2 mattered so much for so long.

---

## 3. Superscalar, out-of-order, and the real machine

### 3.1 The stages, honestly

A modern out-of-order core, in the order data flows:

```
 ┌────────────── FRONT END (in order) ──────────────┐
 │ branch predictor → L1i fetch → decode → µop queue│   ← §6, §8
 └──────────────────────┬───────────────────────────┘
                        │
 ┌──────────── RENAME / ALLOCATE (in order) ────────┐
 │ map arch regs → physical regs; allocate ROB entry│   ← §3.2, §3.3
 └──────────────────────┬───────────────────────────┘
                        │
 ┌────────── SCHEDULER + EXECUTION (out of order) ──┐
 │ wait for operands → issue to a port → execute    │   ← §9
 └──────────────────────┬───────────────────────────┘
                        │
 ┌────────────── RETIRE (in order) ─────────────────┐
 │ commit results, check exceptions, free resources │   ← §3.3
 └──────────────────────────────────────────────────┘
```

**In-order at the ends, out-of-order in the middle.** The two in-order ends are what
preserve the illusion; the out-of-order middle is what produces the performance.

### 3.2 Register renaming: most of your dependencies are fake

Consider:

```asm
    ldr  x0, [x1]      ; A: load
    add  x2, x0, #1    ; B: needs A
    ldr  x0, [x3]      ; C: reuses x0 — but depends on nothing
    add  x4, x0, #1    ; D: needs C
```

Naively, C must wait for B, because both use `x0`. That's a **write-after-read** (WAR)
hazard — a *name* dependency, not a *data* dependency. The two halves compute unrelated
things that happen to share an architectural register name because the ISA only gives you
31 of them.

Renaming dissolves it. The core maintains a large **physical register file** and a map
from architectural names to physical registers. `ldr x0` in C allocates a *fresh* physical
register; B keeps reading the old one. The two chains now run in parallel.

Consequences worth internalizing:

- **Only true (read-after-write) dependencies cost you.** WAR and WAW are free.
- The physical register file is a finite resource, and running out of it stalls the front
  end just as surely as a full ROB (§3.3).
- Some instructions are eliminated entirely at rename: register-to-register `mov` on both
  x86 and AArch64 is often *zero-cycle* (it's a map update, not an execution), and `xor
  eax, eax` / `mov x0, #0` are recognized as zeroing idioms. Dougall Johnson's Firestorm
  work observes retire groups of seven µops "only for eliminated instructions, such as
  `nop` and `mov`."
- This is why **instruction count is a poor cost model** and why the compiler's habit of
  emitting "extra" register moves usually costs nothing.

### 3.3 The reorder buffer and the out-of-order window

The **ROB** tracks every in-flight instruction so retirement can happen in program order.
Its size bounds how far ahead the core can look for independent work — the **out-of-order
window**. A big window is exactly what lets a core survive a 300-cycle DRAM miss: it
keeps finding independent instructions behind the stalled load (this is the mechanism
behind doc 01 §1.1's 24× MLP result).

But **the ROB is rarely the actual limit.** Travis Downs' "speed limits" analysis (linked
from the Golden Cove writeup) makes the point that the window is bounded by whichever
structure runs out first: ROB entries, physical registers, load-queue entries,
store-queue entries, scheduler entries, or branch-order-buffer entries. Vendors size all
of them together; you should reason about all of them together.

Apple's design is a good illustration that "ROB size" isn't even a well-defined number
across vendors. Dougall Johnson found Firestorm uses **two** structures:

> "Firestorm uses an unconventional reorder buffer, which I described as a ~330 entry
> 'coalesced retire queue' and a ~623 'rename retire queue'… Firestorm coalesces µops into
> **retire groups**, which all retire together. A retire group may contain up to seven
> µops… The coalesced retire queue consists of ~330 such groups. **This allows an
> out-of-order window of just over 1000 (contrived) instructions that issue, or over 2200
> `nop` instructions.**"

So the widely-quoted "~630-entry ROB" for Apple's M1 P-core is the *rename retire queue*
(the register-reclaim structure), and the effective window is larger than that number
suggests. When you compare vendors' ROB figures, you are frequently comparing different
things.

### 3.4 Memory ordering starts here

Because loads and stores also execute out of order, **the order in which your stores
become visible to other cores is not your program order.** Store buffers, load/store
queues, and speculative load execution are all in play. That is the entire subject of
[`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md), and it is the reason
x86-TSO and AArch64's weak model differ in ways you can measure. For now, hold onto one
fact: **the reordering is not a compiler artifact; the silicon does it, and it does it to
every program you have ever run.**

---

## 4. Two machines, side by side

The rest of this document is architecture-neutral, but the numbers are not, so here are
two concrete instantiations. The left column is the classic x86 out-of-order design; the
right is the machine this curriculum measures on.

### 4.1 Cores

| | **Intel Golden Cove / Raptor Cove** (Alder/Raptor Lake P-core) | **Apple Firestorm** (M1 P-core; best-documented Apple P-core) |
|---|---|---|
| ISA | x86-64, variable-length (1–15 bytes) | AArch64, fixed 4-byte |
| Decode width | **6** (up from 4 in Skylake) | **8** |
| Allocate/rename width | 6 | 8 µops/cycle "pipeline width" |
| ROB | **512 entries** (Sunny Cove: 352) | ~330 coalesced retire groups (≤7 µops each) + ~623 rename entries → **>1000-instruction window** |
| Integer ALUs | **5** — most in any x86 core to date | **6** |
| Load/store units | 2 load + 2 store (Golden Cove adds a 3rd load port in some configs) | 4 (2 load-only, 1 load/store, 1 store) |
| SIMD/FP units | 3 | 4 |
| µop cache | Yes (~4K entries) | **N/A** — fixed-width decode makes it unnecessary (§8) |
| Mispredict penalty | ~16.5–20 cycles (Skylake-measured; similar class) | **13 cycles** |
| SMT | **Yes** (2 threads/core) | **No** |
| Cache line | 64 B | **128 B** |
| Page size | 4 KB (2 MB/1 GB huge pages) | **16 KB** |

The Golden Cove figures come from Chips and Cheese's [Popping the Hood on Golden
Cove](https://chipsandcheese.com/p/popping-the-hood-on-golden-cove); the Firestorm figures
from [Dougall Johnson's microarchitecture
work](https://dougallj.github.io/applecpu/firestorm.html) and
[7-cpu.com](https://www.7-cpu.com/cpu/Apple_M1.html).

AMD's Zen 5 is worth a row of its own because it does something neither of the above does:
448-entry ROB, 8-wide dispatch/retire, and — uniquely — **dual-ported instruction fetch
feeding two 4-wide decode clusters**, driven by a *2-ahead* branch predictor that resolves
multiple prediction windows per cycle. Chips and Cheese traces the idea back to [Seznec et
al., "Multiple-block ahead branch predictors"
(1996)](https://dl.acm.org/doi/10.1145/237090.237169). Note who that is; his name appears
again in §6.

### 4.2 This machine, concretely

Everything in the `*(measured)*` blocks of this curriculum runs on one machine. Read from
`sysctl` *(measured)*:

```
machdep.cpu.brand_string : Apple M3 Pro
hw.ncpu / hw.physicalcpu / hw.logicalcpu : 11 / 11 / 11     ← note: equal. No SMT (§10)
hw.nperflevels           : 2
hw.perflevel0.name       : Performance  (5 cores)
  l1icachesize 196608 (192 KB)   l1dcachesize 131072 (128 KB)   l2cachesize 16777216 (16 MB, shared by 5)
hw.perflevel1.name       : Efficiency   (6 cores)
  l1icachesize 131072 (128 KB)   l1dcachesize  65536 ( 64 KB)   l2cachesize  4194304 (4 MB, shared by 6)
hw.cachelinesize         : 128
hw.pagesize              : 16384
hw.optional.arm.FEAT_LSE / FEAT_LSE2 / FEAT_SB / FEAT_BTI : 1 / 1 / 1 / 1
```

Four things to notice, because each is load-bearing later:

1. **`hw.physicalcpu == hw.logicalcpu == 11`.** There is no SMT. Every "hyperthreading"
   intuition you have from x86 is inapplicable here, and §10 explains why that changes
   how you reason about GIL-bound workloads.
2. **Heterogeneous cores.** 5 P + 6 E, with different cache sizes *and different
   microarchitectures*. A benchmark that migrates between them is measuring the scheduler,
   not your code. This is the noise source that
   [`31-measurement-methodology.md`](31-measurement-methodology.md) spends a section on.
3. **192 KB of L1 instruction cache** on a P-core — six times Skylake's 32 KB. Hold that
   number; §12.1 needs it, because CPython's eval loop is enormous.
4. **`FEAT_BTI`** — Branch Target Identification, an ARMv8.5 control-flow-integrity
   feature that constrains indirect branch targets. It exists because §7's speculation is
   also an attack surface.

The Python used throughout, from `sysconfig` *(measured)*:

```
3.14.6 (main, Jun 11 2026) [Clang 22.1.3]
CONFIG_ARGS: … --with-tail-call-interp --with-mimalloc --enable-optimizations
             --enable-experimental-jit=yes --enable-shared
sys._jit.is_available() → True     sys._jit.is_enabled() → False
sys._is_gil_enabled()   → True
```

So this interpreter is a **tail-call interpreter build** with the tier-2 JIT compiled in
but not enabled at runtime. That matters for §12.2, and it is the kind of thing you should
check before quoting any interpreter benchmark — including your own.

---

## 5. IPC: what it measures and what it hides

**IPC** — instructions retired per cycle — is the headline efficiency number. Peak IPC is
the retire width: 6 on Golden Cove, 8 on Firestorm and Zen 5. Real code rarely approaches
it.

The useful decomposition is **the top-down model** (Yasin, 2014; the basis of `perf`'s
`--topdown` and of Bakhvalov's book). Every issue slot in every cycle is one of four
things:

```
                     ┌── Retiring          (useful work — but see below)
   issue slots ──────┤── Bad speculation   (work done and thrown away — §6, §7)
                     ├── Front-end bound   (no µops delivered — §8, L1i misses, BTB misses)
                     └── Back-end bound    (µops delivered, can't execute — §9, cache misses)
```

Two rules that will save you weeks:

**Rule 1 — high IPC is not the goal.** A busy-wait loop retires beautifully. `Retiring`
counts *slots*, not *usefulness*. A change that removes work will typically *lower* IPC
while raising performance, because what remains is the memory-bound part. Chase wall-clock
time; use IPC to explain it, never to optimize it directly.

**Rule 2 — the four buckets tell you which document to read.** Front-end bound sends you
to §6/§8 and doc 01's instruction-cache material. Back-end bound sends you to doc 01
(memory) or §9 (ports). Bad speculation sends you to §6. Retiring, with bad wall-clock,
means you're executing too many instructions and belongs to `33-optimizing-python.md`.

**The measurement caveat.** All of this requires hardware performance counters. On Linux
that's `perf stat -e ...` or `perf stat --topdown`. On macOS, PMU access is gated: as
[`12-observing-a-process.md`](12-observing-a-process.md) documents *(measured)*, the
platform refuses several categories of introspection outright, and `perf`'s topdown
support is Intel-specific in any case. **Practical consequence: if you want to reason
about IPC and speculation for CPython, you need a Linux box with an Intel or AMD CPU.**
That is not a limitation of the theory; it is a limitation of your laptop, and knowing
which is which is the skill doc 12 is about.

---

## 6. Branch prediction

### 6.1 The problem

The front end must fetch instruction *n+1* before instruction *n* has executed. When *n*
is a conditional branch, the front end doesn't know the answer. It has three options: stall
(catastrophic — a bubble per branch, and branches are roughly one instruction in five),
execute both paths (expensive, and doesn't compose past one branch), or **guess**.

Everyone guesses. The prediction is made in the front end, execution proceeds
speculatively, and if the guess is wrong the pipeline is flushed back to the branch and
refilled — the 13-to-20-cycle penalty of §2.2, which is 100+ instruction slots of §2.3.

Note that there are actually two prediction problems, and they use different hardware:

- **Direction** (taken / not-taken) for conditional branches → the **conditional branch
  predictor (CBP)**, typically TAGE-class today.
- **Target address** for indirect branches, returns, and jumps → the **branch target
  buffer (BTB)**, the **indirect branch predictor (IBP)**, and the **return address stack
  (RAS)**.

Interpreters live and die on the second one. Doc 20's dispatch discussion is entirely a
statement about the IBP.

### 6.2 TAGE and ITTAGE — the predictors that changed the argument

The 1990s model was a table of 2-bit saturating counters indexed by branch address,
possibly combined with a global history register (gshare, and the tournament predictors of
the Alpha 21264). Those predictors are genuinely bad at interpreter dispatch, and that is
the origin of the folklore in §6.3.

Modern predictors are **TAGE** (TAgged GEometric history length, Seznec & Michaud, 2006)
and its indirect-target sibling **ITTAGE**. Nelson Elhage's [excellent
walkthrough](https://blog.nelhage.com/post/ittage-branch-predictor/) summarizes them
precisely:

> Both TAGE and ITTAGE:
> - Predict branch behavior by mapping from *(PC, PC history) → past behavior*, and hoping
>   that the future resembles the past.
> - Store many such tables, using a **geometrically-increasing series of history lengths**.
> - Attempt to **dynamically choose the correct table (history length) for each branch**.
> - Do so by adaptively moving to a longer history on error, and using a careful
>   replacement policy to preferentially keep useful entries.

Read the third bullet again, because it is the whole story for interpreters. A predictor
with a *fixed* short history sees a bytecode dispatch as "this indirect branch goes
everywhere" — unpredictable. A predictor that can select a *long* history sees "the last
17 branches were this exact sequence, and after that sequence this dispatch always goes to
`LOAD_FAST`" — highly predictable. **Real programs' bytecode sequences are enormously
repetitive**, so long-history correlation works.

TAGE is not academic. The reverse-engineering literature confirms it in shipping silicon:
[Chen et al. (2024)](https://arxiv.org/html/2411.13900v1) recovered the conditional branch
predictors of **Apple Firestorm** and **Qualcomm Oryon** and found TAGE-style
multi-PHT structures — for Firestorm, a path history register of **100 bits**
(their experiments inject into `PHR[99]`, the highest bit) feeding a set of pattern history
tables at geometrically increasing history lengths. The same paper notes TAGE deployment
in AMD Zen 2, ARM Cortex-A53, and Intel Alder Lake. AMD's Zen 5 goes further with 2-taken,
2-ahead TAGE.

### 6.3 The folklore, and its refutation

This is the most important intellectual history in the document, and the reason doc 20
exists in the shape it does.

**2003 — Ertl & Gregg, [*The Structure and Performance of Efficient
Interpreters*](https://www.jilp.org/vol5/v5paper12.pdf).** On the hardware of the day, the
single `switch`-dispatch indirect branch in a bytecode interpreter mispredicted
catastrophically — a single branch site with dozens of targets, predicted from a
short history, is nearly a coin flip. The fix they popularized is **dispatch replication**
(a.k.a. threaded code / computed gotos): instead of one shared `goto *table[opcode]` at the
bottom of a `switch`, put a *copy* of the dispatch jump at the end of *every* opcode
handler. Now the predictor sees N distinct branch sites, each with its own history, and
each learns the local correlation "after `LOAD_FAST` usually comes `LOAD_CONST`." This is
exactly why CPython has `--with-computed-gotos` and has had it for over a decade.

**2015 — Rohou, Swamy & Seznec, [*Branch Prediction and the Performance of
Interpreters — Don't Trust Folklore*](https://inria.hal.science/hal-01100647)** (CGO
2015). The rebuttal. Using hardware counters on Intel Haswell *and* simulation of ITTAGE,
they showed that **the accuracy of indirect branch prediction is no longer critical for
interpreters.** The predictors caught up. The folklore did not.

**Why both are right.** Ertl & Gregg described 2003 hardware accurately. Seznec's own
ITTAGE — the predictor he designed — is what invalidated the conclusion, which is a
pleasingly complete arc. The methodological lesson is the one doc 31 hammers: *a
performance result is a claim about a machine, not about a program.* Cite the 2003 paper
for the mechanism and the 2015 paper for whether the mechanism still matters, in that
order, and never cite the first alone.

**2025 — the empirical closing argument.** See §12.2. Nelson Elhage counted indirect jumps
in CPython binaries and measured them, and the answer on modern hardware is that computed
gotos buy approximately nothing *for prediction*; what they buy is protection against the
compiler.

### 6.4 What actually makes a branch unpredictable

Predictors handle far more than you'd think. What they cannot handle:

- **Data-dependent branches on genuinely random data.** `if (rand() % 2)` is a 50%
  mispredict rate, permanently. No history helps, because there is no pattern.
- **Working sets that exceed the tables.** Chips and Cheese measured Golden Cove holding
  up until "the repeating pattern length exceeds 48" with 512 branches in the loop, while
  Zen 3 managed 96. A branch-heavy program with a huge footprint thrashes the predictor
  the same way a big array thrashes the cache. **Predictor capacity is a cache-capacity
  problem in disguise**, and it is one of the ways enormous codebases get slow for reasons
  no profiler attributes to any one line.
- **Indirect branches with a target set that shifts.** A megamorphic virtual call site, a
  `switch` over unpredictable input, a hash-bucket dispatch.
- **BTB misses.** Even a correctly-*predicted-direction* branch costs if the target isn't
  cached. Golden Cove has a three-level BTB where a level miss costs 1 cycle; AMD's L2 BTB
  hit costs 3.

Practical mitigations, roughly in order of value:

1. **Make the data sorted or otherwise patterned** — the classic "sorted array makes the
   branchy loop 3× faster" Stack Overflow demo, which is real and reproducible.
2. **Branchless formulation** — `cmov` / `csel`, arithmetic masking, `min`/`max`. But
   note the trade: a conditional move *always* takes the data dependency, so on a
   *predictable* branch it is slower. Branchless is a bet that prediction will fail.
3. **Fewer, hotter branch sites** — helps predictor capacity.
4. **Profile-guided optimization** — lets the compiler lay out the taken path fall-through
   and improves front-end density. CPython's `--enable-optimizations` is exactly this, and
   it is why the build on this machine uses it.

---

## 7. Speculation beyond branches

Control flow is just the most famous thing CPUs guess about. Two more matter.

### 7.1 Memory dependence prediction

A load cannot safely execute before an earlier store whose address is still unknown —
they might alias. Waiting for every prior store address would serialize memory. So cores
**predict** that a load does *not* alias, execute it early, and detect violations later
(a memory-order violation triggers a flush much like a mispredict). This is why
`store-to-load forwarding` shows up in every optimization guide, and why writing then
immediately reading nearby memory has a cost the instruction count doesn't show.

### 7.2 Data-value speculation — new, and Apple-specific

Recent Apple cores speculate on *data*, not just control flow. This was made public
knowledge by security research, which is often how microarchitectural details escape:

- **SLAP** (Kim, Genkin & Yarom, IEEE S&P 2025): "Apple CPUs **starting with the
  M2/A15** are equipped with a **Load Address Predictor (LAP)**, which improves performance
  by guessing the next memory address the CPU will retrieve data from based on prior memory
  access patterns."
- **FLOP** (Kim, Chuang, Genkin & Yarom, USENIX Security 2025): "**Apple's M3/A17
  generation and newer** CPUs are equipped with a **Load Value Predictor (LVP)**. The LVP
  improves performance on data dependencies by guessing the data value that will be
  returned by the memory subsystem on the next access, *before the value is actually
  available*."

Sit with the LVP for a second. **The machine executes instructions on a value it has not
loaded yet.** If the guess holds, a load-use dependency chain — the thing pointer chasing
is made of — is broken speculatively. This is directly relevant to CPython: chasing
`PyObject*` pointers is the single most common thing the interpreter does (doc 01 §10),
and on an M3 or later some of those chains are being broken by value prediction. It is
also the reason a benchmark on an M3 may behave qualitatively unlike the same benchmark on
an M1, in a way no published spec sheet explains.

The security consequence is the same one Spectre established: **a mispredicted speculative
execution leaves microarchitectural traces even after the architectural state is rolled
back.** SLAP and FLOP turn the LAP and LVP into arbitrary-read primitives from sandboxed
JavaScript in Safari and Chrome. Related: doc 01 §7's **GoFetch**, on Apple's
data-memory-dependent prefetcher.

### 7.3 The mitigation tax, and why it belongs in a Python book

Spectre/Meltdown mitigations are not free, and their cost lands on exactly the operations
Python programs do constantly:

- **KPTI/PTI** (Meltdown) made the kernel-entry path far more expensive by unmapping the
  kernel from user page tables — every syscall pays a TLB cost. That cost is quantified in
  [`09-syscalls-and-io.md`](09-syscalls-and-io.md), where the trap floor is measured at
  ~81 ns on this machine.
- **Retpolines / IBRS / IBPB** (Spectre v2) constrain indirect branch prediction. A
  retpoline deliberately *defeats* the IBP for protected indirect branches — which is,
  precisely, the mechanism a bytecode interpreter depends on. Kernel-side only in most
  configurations, but if you ever build an interpreter with `-mretpoline`, expect to
  reproduce the 2003 folklore numbers exactly.
- **ARMv8.5 `FEAT_BTI`** (present on this machine, per §4.2) and `FEAT_SB` (speculation
  barrier) are the AArch64 answers: constrain legal indirect-branch targets and provide an
  explicit barrier, rather than disabling prediction.

---

## 8. Decode, µops, and the x86 tax

An x86 instruction is 1 to 15 bytes long, so **you cannot find instruction boundaries
without decoding**. Decoding six instructions per cycle in parallel therefore requires
speculating about lengths, and it costs real power and area. AArch64 instructions are
always exactly 4 bytes, so finding eight boundaries is a wire, not a computation. **This is
the single clearest architectural advantage AArch64 has**, and it is a large part of why
Apple ships 8-wide decode while Intel reached 6 only with Golden Cove.

x86's answer is caching the *decoded* form:

- **The µop cache (DSB)** holds already-decoded µops, ~4K entries on recent Intel. Hits
  bypass decode entirely — which is why Skylake's mispredict penalty is 16.5 cycles on a
  DSB hit and 19–20 on a miss (§2.2). Front-end-bound code that overflows the µop cache
  falls off a cliff that has no analogue on AArch64.
- **The loop stream detector / µop queue** can replay small loops without re-fetching.
  Chips and Cheese notes Golden Cove "can unroll loops within its µop queue, giving it an
  incredible throughput of **two taken branches per cycle**. In a sense, Intel's µop queue
  acts like a tiny trace cache."
- **Macro-op fusion** merges `cmp`+`jcc` into one µop, recovering some of the loss.

Zen 5's approach — dual-ported fetch feeding two independent 4-wide decode clusters (§4.1)
— is a different answer to the same problem: if you can't decode 8-wide in one cluster,
run two clusters.

**Why a Python programmer cares:** CPython's `_PyEval_EvalFrameDefault` is one of the
largest functions in common software. With computed gotos it contains hundreds of copies
of the dispatch sequence, and with 267 opcodes in 3.14 (`len(opcode.opname)` *(measured)*)
plus their specialized variants, the interpreter's hot code footprint is measured in tens
of kilobytes. **That is an instruction-cache and µop-cache problem, and it is why doc 01's
§10.6 lab measures per-call cost rising from ~330 ns to ~685 ns as the number of distinct
call sites grows.** On this M3 Pro's 192 KB L1i the eval loop fits comfortably; on a
32 KB-L1i Skylake it does not, and the same interpreter behaves differently. Same code,
different machine, different bottleneck — §5 Rule 2 in action.

---

## 9. Execution ports: why instruction *count* is a bad model

Instructions don't execute "in the core," they execute on a **port** (Intel's term) or
**unit/pipe** (Apple's). Each port serves a specific set of operations, one per cycle. The
port layout is the real throughput model.

Dougall Johnson's Firestorm port map is worth reading closely:

```
Integer units:
  1: alu + ubfm/sbfm + flags + branch + adr + msr/mrs nzcv + mrs
  2: alu + ubfm/sbfm + flags + branch + adr + msr/mrs nzcv + INDIRECT BRANCH + ptrauth
  3: alu + ubfm/sbfm + flags + mov-from-simd/fp?
  4: alu + ubfm/sbfm + mov-from-simd/fp?
  5: alu + ubfm/sbfm + mul + div
  6: alu + ubfm/sbfm + mul + madd + crc + bfm/extr

Load/store units:
  7: store + amx      8: load/store + amx      9: load      10: load

SIMD units:
  11–14: fp/simd (14 also handles fdiv, fcmp, sha, …)
```

Three readings of that map:

1. **Six ALUs, but only two can take a branch, and exactly one can take an indirect
   branch.** Unit 2 is the only one with `indirect branch`. So a bytecode interpreter's
   dispatch — an indirect branch every few instructions — contends for a *single* issue
   port on this machine, no matter how many ALUs are idle. That is a structural hazard
   (§2.1) in 2026 silicon, and it is the kind of detail that explains a benchmark result no
   amount of C-level reasoning would.
2. **Only two ports do multiplies; only one does divides.** Integer division is scarce
   everywhere; this is why `%` by a constant gets compiled into a multiply-and-shift and
   why hash-table implementations obsess over avoiding a real modulo.
3. **Two pure load ports plus one load/store.** Load throughput is 3/cycle, store
   throughput 2/cycle. Golden Cove's 5 integer ALUs are "the most in any x86 CPU to date"
   per Chips and Cheese — and even so, Firestorm has six.

The practical model this replaces "count instructions" with:

> **Throughput = max over ports of (µops sent to that port) / (port count).**
> **Latency = the longest dependency chain.**
> Your actual runtime is `max(throughput bound, latency bound, memory bound)`.

For hand-optimization on x86 this is what [uops.info](https://uops.info/) and
`llvm-mca` compute for you. Both exist, both are free, and both will teach you more in an
afternoon than a week of guessing.

---

## 10. SMT — and the machine that doesn't have it

**Simultaneous multithreading** (Intel: Hyper-Threading) duplicates architectural state —
registers, program counter — but shares the execution resources. Two threads issue into
the same ports, the same ROB slots, the same caches. The premise is that one thread alone
leaves the machine idle (§5), so a second thread fills the gaps.

What it does and doesn't do:

- **Typical gain: 10–30% throughput**, occasionally negative. It never doubles anything.
- **It cannot help a thread that is execution-bound**; it helps threads that stall.
- **It halves your effective cache per thread**, which for a memory-bound workload
  (i.e. most Python) can be a net loss.
- Zen 5 statically partitions decode clusters when 2 threads are active (§4.1) — the
  sharing is not always dynamic.
- It is a **side-channel surface**, which is why some cloud and HPC operators disable it
  entirely.

**And this machine doesn't have it.** `hw.physicalcpu == hw.logicalcpu == 11` *(measured)*.
No Apple silicon core has ever shipped SMT. The design bet is the opposite one: rather
than filling one core's idle slots with a second thread, build a core wide enough and a
window deep enough that a single thread keeps it busy (§3.3's >1000-instruction window),
and add more physical cores.

**Three consequences for Python work:**

1. `os.cpu_count()` on x86 reports logical CPUs including SMT siblings, and sizing a
   process pool by it routinely oversubscribes. `os.process_cpu_count()` (3.13+) respects
   affinity but still counts logical CPUs.
   [`06-processes-threads-scheduling.md`](06-processes-threads-scheduling.md) treats
   "how many CPUs" as three separate questions for exactly this reason. On this M3 Pro all
   three answers happen to coincide at 11, which makes it a *bad* machine for catching that
   class of bug.
2. **SMT is nearly useless for GIL-bound CPython anyway** — two threads that can't run
   Python simultaneously don't fill each other's stalls. Free-threaded builds (PEP 703,
   doc 26) change that calculus on x86 and not at all here.
3. A benchmark that "scales to 16 threads" on an 8-core/16-thread x86 box and doesn't on an
   11-core Mac is not necessarily reporting a software difference.

---

## 11. Interrupts, exceptions, and context switches as pipeline events

Everything above assumes the core runs your code undisturbed. It doesn't.

**Interrupts** (timer, device, IPI) are external and asynchronous. The core must reach a
precise architectural state, which means **draining or flushing the pipeline**: speculative
work discarded, in-flight instructions retired or squashed. Then it vectors to the handler
— cold in L1i, cold in the BTB, cold in the TLB.

**Exceptions/faults** (page fault, `SIGSEGV`, divide-by-zero) are synchronous and precise:
the ROB is exactly the mechanism that makes them precise, since retirement in program
order lets the core name the offending instruction. A page fault's *direct* cost is a
pipeline flush; its real cost is in [`07-virtual-memory.md`](07-virtual-memory.md), which
measures a minor fault at 0.5 µs and a major fault at 15.9 µs against a 12 ns warm access
*(measured)*.

**Context switches** are the expensive case, and their cost is mostly *indirect*:

| Component | Where it shows up |
|---|---|
| Pipeline flush + drain | Here (§2.3) |
| Save/restore registers, kernel entry/exit | [doc 09](09-syscalls-and-io.md) — ~81 ns trap floor *(measured)* |
| Scheduler decision | [doc 06](06-processes-threads-scheduling.md) |
| **Cold L1/L2/L3 on resume** | [doc 01](01-memory-hierarchy-and-caches.md) — the dominant term |
| **Cold branch predictor and BTB** | Here — and rarely accounted for |
| TLB effects (mitigated by ASID/PCID) | [doc 07](07-virtual-memory.md) |

Doc 06 measures the whole thing end to end at **2.70 µs for threads and 2.88 µs for
processes** *(measured)* — a ratio of only 1.06×, because ASID/PCID removed the TLB flush
that used to make process switches dramatically worse. At ~4 GHz, 2.7 µs is roughly
**11,000 cycles**, or ~85,000 discarded issue slots on an 8-wide core. Compare that to the
13-cycle mispredict of §2.2 and you have the scale that matters: a mispredict is a
pothole; a context switch is a detour.

**The CPython-specific version:** the GIL's `gil_drop_request` mechanism forces a thread
switch every `sys.getswitchinterval()` (5 ms default) whenever another thread is waiting.
Doc 06 measures the GIL **out-preempting the OS 16:1 — 1,280 switches/s against an 82/s
floor** *(measured)*. Every one of those pays the table above. That is a large part of
why GIL-bound multithreaded Python is often *slower* than single-threaded, and it is
argued properly in [`24-the-gil.md`](24-the-gil.md) §on the convoy effect.

---

## 12. What all of this means for CPython

### 12.1 The eval loop as a branch-predictor stress test

`_PyEval_EvalFrameDefault` is, stripped of detail:

```c
for (;;) {
    opcode = next_instr->op.code;
    oparg  = next_instr->op.arg;
    next_instr++;
    switch (opcode) {              /* or: goto *opcode_targets[opcode]; */
        case LOAD_FAST:  ... DISPATCH();
        case LOAD_CONST: ... DISPATCH();
        /* × 267 opcodes in 3.14, measured via len(opcode.opname) */
    }
}
```

Every Python bytecode costs **one indirect branch** with up to 267 targets, plus the
handler's own branches. On this machine that indirect branch can only issue on Firestorm's
integer unit 2 (§9). A tight Python loop executes this hundreds of millions of times, so:

- If the IBP predicts it well, dispatch is nearly free and the cost is the handler bodies.
- If not, you pay 13–20 cycles × hundreds of millions.

The entire history in §6.3 is about which of those is true, and the answer changed
somewhere between 2003 and 2015.

There is a second, less-discussed cost: the loop's **instruction footprint**. 267 opcodes,
many with specialized variants, each with a dispatch epilogue, is tens of KB of hot code.
On a 32 KB-L1i x86 core that thrashes; on this M3 Pro's 192 KB L1i it doesn't (§4.2, §8).
**The same interpreter is front-end bound on one machine and not on another.**

### 12.2 Computed gotos, dispatch replication, and the tail-call saga

CPython has built with computed gotos (`--with-computed-gotos`, replicating the dispatch
into every handler — §6.3's Ertl-Gregg fix) since 3.1. In 3.14 it gained a third option:
the **tail-call interpreter** (`--with-tail-call-interp`, which this machine's build uses,
§4.2), where each opcode becomes its own function and dispatch is a guaranteed tail call
(`[[clang::musttail]]`). The initial headline was a **10–15% speedup**.

Then Nelson Elhage [investigated](https://blog.nelhage.com/post/cpython-tail-call/), and
the result is the best empirical data anyone has on this whole question. His measured
`pyperformance` comparison, all builds using LTO+PGO, `clang18` as baseline:

| Platform | clang18 | clang19 | clang19.taildup | clang19.tc | gcc |
|---|---|---|---|---|---|
| Raptor Lake i5-13500 | (ref) | **1.09× slower** | 1.01× faster | 1.03× faster | 1.02× faster |
| Apple M1 MacBook Air | (ref) | **1.12× slower** | 1.02× slower | **1.00× slower** | N/A |

The 10–15% was almost entirely **a regression in Clang 19 being worked around**, not a win.
Against a good baseline the tail-call interpreter is worth ~1–5% on x86 and, on the M1,
*nothing*. Ken Jin, who did the original work, [wrote a public
correction](https://fidget-spinner.github.io/posts/apology-tail-call.html); the episode was
covered by [LWN](https://lwn.net/Articles/1013581/). The underlying Clang bug had been
[reported in August 2024 by Mikulas Patocka](https://lwn.net/Articles/1013581/) and was
fixed by reading GCC's source, which already documented the hazard: *be careful with
computed gotos as used in threaded bytecode interpreters.*

But the deepest result is Elhage's second table, which almost nobody quotes. He counted
indirect jumps in the compiled interpreter and benchmarked with computed gotos disabled:

| | clang18 | clang18.nocg | clang19.nocg | clang19 |
|---|---|---|---|---|
| Performance | (ref) | **1.01× faster** | 1.02× slower | 1.09× slower |
| **# of indirect jumps** | **332** | 306 | **3** | **3** |

Read the bottom row first. `clang19` compiled the computed-goto interpreter down to **3**
indirect jumps — it *undid* the dispatch replication, folding 332 sites back into a shared
one, which is precisely the 2003 pathology. That's the regression.

Now read the top row. On Clang 18, **turning computed gotos off made CPython 1% *faster*.**

> **The conclusion:** on 2020s hardware, dispatch replication buys ~nothing *as a
> prediction technique* — §6.3's refutation, confirmed on the exact program the folklore
> was about. What computed gotos (and `musttail`) actually buy is **control over the
> compiler**: a guarantee that it won't merge your dispatch sites, spill your hot state, or
> otherwise destroy a structure you built on purpose. That is a real and sufficient
> justification. It is just not the one everybody gives.

If you take one thing from this document into doc 20, take that.

### 12.3 PEP 659 is a speculation story

[PEP 659](https://peps.python.org/pep-0659/)'s specializing adaptive interpreter is, in
this document's vocabulary, **speculation at the bytecode level, with the same structure as
hardware branch prediction**:

| Hardware (§6) | PEP 659 |
|---|---|
| Predict from history | Specialize `LOAD_ATTR` → `LOAD_ATTR_INSTANCE_VALUE` after warmup |
| Guard: check the prediction | Guard on `type_version` / `dict_version` / keys pointer |
| Mispredict → flush & recover | Deoptimize → re-execute the generic form |
| Backoff counters to avoid thrashing | Warmup and backoff counters (doc 20 decodes them from source) |
| Cost of being wrong: 13–20 cycles | Cost of being wrong: guard + deopt + re-dispatch |

`opcode._specialized_opmap` on this build begins `BINARY_OP_ADD_FLOAT`,
`BINARY_OP_ADD_INT`, `BINARY_OP_ADD_UNICODE`, `BINARY_OP_EXTEND`,
`BINARY_OP_INPLACE_ADD_UNICODE`, `BINARY_OP_MULTIPLY_FLOAT`, … *(measured)*.

PEP 659 reports speedups "in the range 10% – 60%," and is explicit about the split:

> "Most of the speedup comes directly from specialization. The largest contributors are
> speedups to attribute lookup, global variables, and calls. A small, but useful, fraction
> is from improved dispatch such as super-instructions and other optimizations enabled by
> quickening."

**"A small, but useful, fraction is from improved dispatch."** That is the same conclusion
§12.2 reached from the other direction. The win is in doing less work per opcode, not in
predicting the dispatch better — the hardware already predicts the dispatch fine.

There's a second-order effect worth noting: specialization *also* makes the dispatch
sequence more predictable, because a specialized opcode stream is more repetitive than a
generic one, which is exactly what a long-history ITTAGE wants. And it makes it *less*
predictable in one way: more distinct opcodes means more BTB pressure and a bigger
instruction footprint. Which effect dominates is an empirical question and a good lab
(§13, lab 7).

### 12.4 Refcounting is a dependency chain

`Py_INCREF`/`Py_DECREF` is a load, an add, and a store to the object header. Under
free-threading it's an atomic RMW. Either way it is:

- **A true (RAW) dependency** that renaming cannot dissolve (§3.2) — the increment must
  read the value the previous increment wrote.
- **Serialized on the same cache line** as everything else in the object header
  (doc 01 §6, doc 16).
- On the *critical path* of nearly every operation, since every operand touch refcounts.

So refcounting is precisely the thing §3's out-of-order machine cannot help with: a long
serial chain of dependent memory operations. Immortal objects (PEP 683) and deferred
refcounting exist to *break the chain*, not merely to save the instruction. That framing —
in [`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md) and
[`26-free-threading.md`](26-free-threading.md) — only makes sense once you have §3.2.

### 12.5 The summary you should carry forward

CPython, viewed as an input to the machine in §3:

| Machine wants | CPython delivers |
|---|---|
| Long runs of independent instructions | Serialized refcount RMWs and pointer chases |
| Predictable branches | A 267-target indirect branch per bytecode (predicted well — §6.3) |
| Small hot instruction footprint | Tens of KB of eval loop |
| Predictable memory access | `PyObject*` chasing (doc 01 §10) |
| Wide issue | Low IPC (measure it on Linux — lab 6) |

Two of those five have been fixed or found not to matter (dispatch prediction, dispatch
replication). Three have not, and they are memory-shaped, not CPU-shaped. **That is the
handoff to doc 01,** and it is why doc 01's thesis is "Python is slow" ≈ "Python has
terrible cache locality" rather than anything about interpretation overhead.

---

## 13. Lab exercises

Reading this leaves you at rung 3 (README §14). Labs 1–5 run anywhere; labs 6–8 need a
Linux box with PMU access, and finding that out is part of the point (§5).

**1 — Feel a mispredict.** Sum the elements of a large `uint8_t` array greater than 128, in
C, over (a) shuffled data and (b) the same data sorted. Predict the ratio before running.
Then replace the branch with a branchless formulation and run *both* data orders again.
*You should find branchless wins on shuffled data and loses on sorted data* — that
asymmetry is the whole cost model of §6.4 in one experiment.

**2 — Find your out-of-order window.** A pointer-chase loop with a long-latency dependent
load, into which you inject N independent `add` instructions. Sweep N and plot time. The
knee is where you exhaust the window. Predict >1000 on an Apple P-core (§3.3), ~512 on a
Golden Cove. *Then find a second knee* by injecting loads instead of adds — you'll hit the
load queue first, which is Travis Downs' point in §3.3.

**3 — Prove renaming is real.** Two loops with identical instruction counts: one a chain of
dependent `add`s, one four independent chains interleaved. Same instructions, same data.
*Expect roughly a 4× difference on any core with ≥4 ALUs.* This is the single most
convincing demonstration that instruction count is not a cost model.

**4 — Reproduce the interpreter-dispatch question.** Build CPython twice, with and without
`--with-computed-gotos`, both with `--enable-optimizations`. Run `pyperformance`. Compare
your answer to §12.2's table. *If you don't reproduce Elhage's ~1% result, the interesting
question is which of your compiler, your CPU, or your methodology differs* — and doc 31
is about answering that rather than shrugging.

**5 — Count the indirect branches yourself.** `objdump -d` the interpreter from lab 4 and
count indirect jumps in `_PyEval_EvalFrameDefault` (`jmp *` on x86, `br x*` on AArch64).
Elhage got 332 vs 3 for two Clang versions on the *same source*. This lab takes ten
minutes and permanently changes how much you trust "the compiler will handle it."

**6 — Get CPython's IPC and top-down breakdown.** On Linux:
`perf stat -e cycles,instructions,branches,branch-misses python3 -m pyperformance ...`,
then `perf stat --topdown`. Predict which of the four buckets dominates before you look.
Then rerun with a NumPy-heavy workload and watch the answer change completely. Carry the
number into [`32-profiling.md`](32-profiling.md).

**7 — Does specialization help or hurt the front end?** Run a hot loop under `perf stat -e
branch-misses,iTLB-load-misses,L1-icache-load-misses` with `PYTHONUOPS`/specialization in
its default state, then with specialization effectively suppressed (a workload that
deoptimizes constantly — polymorphic call sites, alternating operand types). §12.3 predicts
two effects in opposite directions. Which wins?

**8 — Price a context switch in pipeline terms.** Reproduce doc 06's context-switch
benchmark while collecting `branch-misses` and `L1-icache-load-misses`. Attribute the
2.7 µs (§11) across pipeline flush, cold cache, and cold predictor. *Most people assume the
flush dominates; it doesn't.*

**9 — Confirm you have no SMT, and then confirm you can't reason about it.** Verify
`hw.physicalcpu == hw.logicalcpu` on this machine (§4.2). Then take any thread-scaling
benchmark you trust and explain what its result would look like on an 8-core/16-thread x86
box. Notice how much of the explanation is guesswork. That gap is the argument for owning a
Linux test machine.

**10 — Watch the M3's load-value predictor, if you dare.** §7.2 says the M3 predicts loaded
*values*. Construct a dependent chain where the loaded value is constant across iterations
and one where it varies, holding the access pattern identical. A significant gap for the
constant case is the LVP. *(The FLOP authors report exactly this shape: a marked speedup
from ~40 iterations onward when load values are constant.)* Then reflect that this is a
hardware feature nobody documented until it was attacked.

---

## 14. Question bank

Answer these out loud before moving to doc 01.

1. A function has 20% fewer instructions and runs 10% slower. Give three mechanisms from
   this document that explain it.
2. Why is a WAR dependency free and a RAW dependency not? What hardware makes that true?
3. Your profile shows 3% branch-miss rate and IPC of 0.8 on a 6-wide core. Is branch
   prediction your problem? Show the arithmetic.
4. Why does the same mispredict cost 16.5 cycles or 20 cycles on Skylake?
5. Ertl & Gregg (2003) and Rohou et al. (2015) reach opposite conclusions about interpreter
   dispatch. Neither is wrong. Explain.
6. Why did Clang 19 make CPython 9% slower, and why did nobody notice for months?
7. On Clang 18, disabling computed gotos made CPython 1% *faster*. What, then, are computed
   gotos for?
8. Map PEP 659's specialize/guard/deoptimize cycle onto branch predict/verify/flush. Where
   does the analogy break?
9. Why can't out-of-order execution hide the cost of refcounting?
10. Your service gets 40% faster when you disable Hyper-Threading. Give two explanations
    and the experiment that distinguishes them.
11. `os.cpu_count()` returns 16 on an 8-core machine. You size a `ProcessPoolExecutor` by
    it. What have you done, and what would you have done differently on an M3 Pro?
12. A context switch costs ~2.7 µs. How many issue slots is that on an 8-wide core, and
    which line item in §11's table dominates?
13. Why does AArch64 not need a µop cache?
14. Firestorm has six integer ALUs but only one can execute an indirect branch. Why does
    that specifically matter for CPython?
15. The M3 has a load *value* predictor. Name one way that could make a CPython benchmark
    faster and one way it could make a benchmark *lie to you*.
16. Retpolines defeat indirect branch prediction. What would building CPython with them do,
    and which paper's numbers would you expect to reproduce?
17. You want to know whether CPython is front-end bound. What do you run, and why can't you
    run it on your Mac?
18. Explain to a colleague why "the CPU executes your instructions in order" is a useful
    lie, and name the structure that maintains the lie.

---

## 15. Sources

**Foundational**
- **Bryant & O'Hallaron, *Computer Systems: A Programmer's Perspective*, 3e** — ch. 4 (processor architecture) and ch. 5 (optimizing) are the right first treatment of everything in §2–§3.
- **Hennessy & Patterson, *Computer Architecture: A Quantitative Approach*, 6e** — ch. 3 is the rigorous version: Tomasulo, speculation, and the limits of ILP.
- **Denis Bakhvalov, [*Performance Analysis and Tuning on Modern CPUs*, 2e](https://easyperf.net/)** (2024) 🆓 — the practical complement, and the best explanation of the **top-down methodology** in §5. If you read one thing after this document, read this.
- **Agner Fog, [*The microarchitecture of Intel, AMD and VIA CPUs*](https://www.agner.org/optimize/microarchitecture.pdf)** 🆓 — the reference work for x86 internals; his instruction tables are the companion.
- **[uops.info](https://uops.info/)** 🆓 and `llvm-mca` — machine-readable port/latency data for §9. Use these instead of reasoning.

**Branch prediction — read in this order**
- **Ertl & Gregg, [*The Structure and Performance of Efficient Interpreters*](https://www.jilp.org/vol5/v5paper12.pdf)** (2003) 🆓 — the origin of dispatch replication. Correct about 2003 hardware; cite it for the mechanism, never for the conclusion.
- **Rohou, Swamy & Seznec, [*Branch Prediction and the Performance of Interpreters — Don't Trust Folklore*](https://inria.hal.science/hal-01100647)** (CGO 2015) 🆓 — the refutation, using Haswell counters and ITTAGE simulation. §6.3.
- **Seznec & Michaud, *A case for (partially) TAgged GEometric history length branch prediction*** (JILP 2006) — TAGE itself.
- **Nelson Elhage, [*The ITTAGE indirect branch predictor*](https://blog.nelhage.com/post/ittage-branch-predictor/)** (2025) 🆓 — the clearest available explanation of how TAGE/ITTAGE actually work. §6.2's block quote.
- **Chen, Qu et al., [*Dissecting Conditional Branch Predictors of Apple Firestorm and Qualcomm Oryon*](https://arxiv.org/html/2411.13900v1)** (2024) 🆓 — reverse-engineered TAGE structures in shipping Apple silicon, including the 100-bit path history register. §6.2.
- **Chips and Cheese, [*Zen 5's 2-Ahead Branch Predictor Unit*](https://chipsandcheese.com/p/zen-5s-2-ahead-branch-predictor-unit-how-30-year-old-idea-allows-for-new-tricks)** 🆓 — and the [Seznec et al. 1996 paper](https://dl.acm.org/doi/10.1145/237090.237169) it traces back to.

**Microarchitecture specifics used in this document**
- **Dougall Johnson, [*Firestorm Overview*](https://dougallj.github.io/applecpu/firestorm.html)** 🆓 — the port map in §9, the retire-queue structure in §3.3. The best public Apple-silicon microarchitecture work that exists.
- **Chips and Cheese, [*Popping the Hood on Golden Cove*](https://chipsandcheese.com/p/popping-the-hood-on-golden-cove)** 🆓 — §4.1's Intel column, the BTB discussion, and the "execution units are rarely a bottleneck" framing.
- **[7-cpu.com](https://www.7-cpu.com/)** 🆓 — measured mispredict penalties for [Apple M1](https://www.7-cpu.com/cpu/Apple_M1.html) (13 cycles) and [Skylake](https://www.7-cpu.com/cpu/Skylake.html) (16.5/19–20).
- **Travis Downs, [*Speed Limits*](https://travisdowns.github.io/blog/2019/06/11/speed-limits.html)** 🆓 — why the ROB is rarely the binding constraint (§3.3).
- **`sysctl -a | grep hw.`** on the machine itself — always the ground truth for §4.2. Published Apple Silicon specs are frequently wrong.

**Speculation as an attack surface**
- **[SLAP & FLOP](https://predictors.fail/)** (Kim, Chuang, Genkin & Yarom; S&P 2025 and USENIX Security 2025) 🆓 — the Load Address Predictor (M2/A15+) and Load Value Predictor (M3/A17+). §7.2.
- **[Spectre](https://spectreattack.com/)** and **[Meltdown](https://meltdownattack.com/)** 🆓 — the originals. The mitigation costs land in [`09-syscalls-and-io.md`](09-syscalls-and-io.md).
- **[GoFetch](https://gofetch.fail/)** (Chen et al., 2024) 🆓 — Apple's data-memory-dependent prefetcher; developed in [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §7.

**Applied to CPython**
- **Nelson Elhage, [*Performance of the Python 3.14 tail-call interpreter*](https://blog.nelhage.com/post/cpython-tail-call/)** (2025) 🆓 — §12.2's two tables. The single best piece of empirical interpreter-performance writing in recent years, and a better methodology lecture than most methodology lectures.
- **Ken Jin, [*I'm Sorry for Python's tail-calling Interpreter's Results*](https://fidget-spinner.github.io/posts/apology-tail-call.html)** (2025) 🆓 — how to handle being wrong in public.
- **[LWN: *Python tail-call speedup based on LLVM regression*](https://lwn.net/Articles/1013581/)** 🆓 — the write-up, and a comment thread containing Anton Ertl himself on 20 years of fighting compilers over interpreter code.
- **[PEP 659 – Specializing Adaptive Interpreter](https://peps.python.org/pep-0659/)** 🆓 — §12.3. Note the explicit "a small, but useful, fraction is from improved dispatch."
- **[What's new in Python 3.14](https://docs.python.org/3.14/whatsnew/3.14.html)** 🆓 — the tail-call interpreter's actual status and build requirements.
- [`20-eval-loop.md`](20-eval-loop.md) — where §12.1–§12.3 get taken apart at the source level. This document is its prerequisite.

---

*Next: [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) — this
document's machine, stalled for 300 cycles on a pointer dereference, and why that turns out
to be the number that decides how fast Python is.*
