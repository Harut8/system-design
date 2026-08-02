# 02 — Atomics and memory models: what a store actually promises

> **Tier 0, doc 02.** Prerequisites: [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
> — specifically §5 (MESI, and the store-buffer paragraph that ends it). This document is
> the continuation of that paragraph. Also useful: [`00-cpu-execution-model.md`](00-cpu-execution-model.md)
> (out-of-order execution, speculation). Feeds directly into:
> [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md),
> [`24-the-gil.md`](24-the-gil.md), [`26-free-threading.md`](26-free-threading.md),
> [`30-concurrency-correctness.md`](30-concurrency-correctness.md),
> `25-threads-and-synchronization.md`.
>
> **THESIS: a store instruction is not a promise that anyone else will see the value, and
> it is certainly not a promise about *when*.** Doc 01 established that coherence makes
> every core eventually agree on the value of *one* location. That is a much weaker
> guarantee than programmers assume, because it says nothing about the *order* in which
> different locations become visible. The gap between "coherent" and "ordered" is where
> store buffers live, and it is the entire subject matter of this document. On x86 that
> gap is narrow enough that broken code usually works anyway. **On the ARM machine this
> repo lives on, the gap is wide, and broken code breaks.** Everything free-threaded
> CPython does with `_Py_atomic_*` is an attempt to name that gap precisely enough to
> stay on the right side of it.

> **Measurement provenance.** Every number and every assembly listing below was produced
> on the machine this repo lives on: **Apple M3 Pro (5 P-cores + 6 E-cores), macOS,
> arm64, 128-byte cache lines, 16 KB pages**, with **Apple clang 21.0.0
> (clang-2100.1.1.101)**, **CPython 3.14.6** and **CPython 3.14.6 free-threading**
> (`sys._is_gil_enabled()` verified `False`). Assembly was generated with
> `clang -O2 -S`; x86-64 listings come from `clang -arch x86_64 -O2 -S` on the same
> machine — **cross-compiled, and I verified the cross-compiler works** (see §7.3 for
> the one place I also *ran* x86-64 code, and the large caveat attached to it).
> Items marked *(measured)* are live output captured while writing this document.
> Items marked *(documented)* are architectural facts from the ARM ARM or the C
> standard that I did **not** independently verify by execution. Anything I could not
> confirm is flagged in place. **The heterogeneous P/E cores make this a hostile
> benchmarking host — see §12.4 before trusting any contended number, including mine.**

## Contents

1. [Where doc 01 stopped: the store buffer](#1-where-doc-01-stopped-the-store-buffer)
2. [Invalidate queues: the same problem on the reader's side](#2-invalidate-queues-the-same-problem-on-the-readers-side)
3. [Two reorderings, two different enemies](#3-two-reorderings-two-different-enemies)
4. [`volatile` is not a concurrency tool — proved in assembly](#4-volatile-is-not-a-concurrency-tool--proved-in-assembly)
5. [x86-TSO vs AArch64: the spine of this document](#5-x86-tso-vs-aarch64-the-spine-of-this-document)
6. [The litmus tests, run on this machine](#6-the-litmus-tests-run-on-this-machine)
7. [The C11/C++11 memory model, and what it compiles to](#7-the-c11c11-memory-model-and-what-it-compiles-to)
8. [Fences vs. ordered accesses](#8-fences-vs-ordered-accesses)
9. [Read-modify-write: CAS, LL/SC, and the exclusive monitor](#9-read-modify-write-cas-llsc-and-the-exclusive-monitor)
10. [`compare_exchange_weak` vs `strong`, in real instructions](#10-compare_exchange_weak-vs-strong-in-real-instructions)
11. [Why `fetch_add` beats a CAS loop under contention](#11-why-fetch_add-beats-a-cas-loop-under-contention)
12. [The atomic cost model — measured](#12-the-atomic-cost-model--measured)
13. [Alignment, and what a split atomic really does](#13-alignment-and-what-a-split-atomic-really-does)
14. [Data races are undefined behaviour, and SC-DRF is the bargain](#14-data-races-are-undefined-behaviour-and-sc-drf-is-the-bargain)
15. [CPython's `_Py_atomic_*` API, verified against the source](#15-cpythons-_py_atomic_-api-verified-against-the-source)
16. [Python has no memory model](#16-python-has-no-memory-model)
17. [Lab exercises](#17-lab-exercises)
18. [Question bank](#18-question-bank)
19. [Sources](#19-sources)

---

## 1. Where doc 01 stopped: the store buffer

[Doc 01 §5](01-memory-hierarchy-and-caches.md#5-mesi-how-coherence-actually-works) ended
with four bullet points about store buffers. Here is the mechanism behind them.

MESI is *correct* but *slow*. To write a line held in **S**hared state, a core must
broadcast a request-for-ownership and wait for every other core to acknowledge
invalidation. That is an interconnect round trip — 40–100+ cycles on this machine's
figures, and worse across the P/E cluster boundary or a NUMA hop.

A core that stalled for that on every store would retire roughly one store per hundred
cycles. So it doesn't stall. It writes the value into a small, fully-associative,
per-core FIFO called the **store buffer** and retires the instruction immediately. The
coherence transaction completes in the background, and the entry drains into L1 when
ownership arrives.

```
                    CORE 0                                   CORE 1
  ┌──────────────────────────────────────┐   ┌──────────────────────────────────────┐
  │  execution units                     │   │  execution units                     │
  │       │ store x=1        ▲ load y    │   │       │ store y=1        ▲ load x    │
  │       ▼                  │           │   │       ▼                  │           │
  │  ┌─────────────┐         │           │   │  ┌─────────────┐         │           │
  │  │STORE BUFFER │─────────┘           │   │  │STORE BUFFER │─────────┘           │
  │  │ [x=1] ...   │  store-to-load      │   │  │ [y=1] ...   │  forwarding          │
  │  └──────┬──────┘  forwarding: I see  │   │  └──────┬──────┘  (my own writes      │
  │         │         my own store even  │   │         │          are visible to me   │
  │         │ drains  though nobody      │   │         │ drains   immediately)        │
  │         ▼         else does yet      │   │         ▼                              │
  │  ┌─────────────┐                     │   │  ┌─────────────┐                       │
  │  │  L1D  y=0   │                     │   │  │  L1D  x=0   │                       │
  │  └──────┬──────┘                     │   │  └──────┬──────┘                       │
  └─────────┼────────────────────────────┘   └─────────┼────────────────────────────┘
            └───────────────┬───────────────────────────┘
                    ┌───────┴────────┐
                    │ coherence      │   ← both stores are "in flight" here.
                    │ fabric (MESI)  │     Neither core's store has reached the
                    └────────────────┘     other core's L1 yet.

  RESULT: Core 0's load of y misses the buffered y=1 and reads 0 from L1.
          Core 1's load of x misses the buffered x=1 and reads 0 from L1.
          BOTH threads read 0. Under sequential consistency this is impossible:
          one of the two stores must have "happened first".
```

Three consequences follow immediately, and they are the whole game:

**1. Your stores become visible to others later than they execute.** The store retired;
the value is not architecturally visible to anyone else. There is a window.

**2. You can read your own write before anyone else can.** *Store-to-load forwarding*
lets a load snoop the store buffer. Single-threaded code therefore sees a perfectly
sequential world — which is exactly why single-threaded reasoning survives — while
cross-thread observers see something else.

**3. A memory barrier is, mechanically, "wait for the store buffer to drain."** That is
not a metaphor. It is what the instruction does, and it is why barriers cost real time
rather than being free annotations. §12 measures it.

The diagram above is the **SB litmus test** (also called Dekker's, because it is exactly
the failure mode of Dekker's mutual-exclusion algorithm without fences). §6 runs it on
real hardware and observes it **15,093 times per million trials** *(measured)*.

> **Why this is not a bug.** Nothing here violates coherence. Coherence is a per-location
> guarantee: all cores agree on the *sequence of values* taken by `x`, and separately on
> the sequence for `y`. Nobody promised that the *interleaving between* `x` and `y` is
> consistent across cores. That stronger promise is called **sequential consistency**,
> and no shipping CPU provides it for free, because it would cost the store buffer.

---

## 2. Invalidate queues: the same problem on the reader's side

The store buffer is the writer's optimization. There is a symmetric one on the reader's
side, and it is the reason ARM is weaker than x86 rather than merely equal to it.

When Core 0 broadcasts an invalidate for line L, Core 1 must acknowledge it. If Core 1's
cache is busy, acknowledging promptly would stall the coherence fabric. So Core 1
acknowledges *immediately* and parks the invalidate in an **invalidate queue**, applying
it to L1 later.

```
   Core 0                                        Core 1
   ──────                                        ──────
   store DATA = 42   ─┐                          ┌── L1D:  DATA = 0   (stale, Shared)
                      │ store buffer             │        FLAG = 0
   store FLAG = 1   ─┐│                          │
                     ││                          │  ┌──────────────────┐
                     ▼▼                          │  │ INVALIDATE QUEUE │
              ┌──────────────┐   invalidate DATA │  │  [inval DATA] ←──┼── acked, but
              │  coherence   │ ─────────────────────▶│                 │   NOT YET
              │   fabric     │   invalidate FLAG │  │  [inval FLAG]    │   APPLIED
              └──────────────┘ ─────────────────────▶└──────────────────┘
                                                  │
                                                  │  Core 1 executes:
                                                  │    r1 = FLAG   → misses L1, refetches → 1  ✓
                                                  │    r2 = DATA   → HITS stale L1 line   → 0  ✗
                                                  │                  (its invalidate is
                                                  │                   still in the queue)
                                                  ▼
                              r1 == 1 && r2 == 0     ← the MP test's forbidden outcome
```

That outcome — reading the flag as set but the data as unwritten — is the **MP (message
passing) litmus test**, and it is the single most consequential difference between x86
and ARM:

- **On x86-TSO it cannot happen.** TSO has no invalidate-queue-visible reordering and no
  StoreStore reordering; if you observe `FLAG == 1` you are guaranteed to observe
  `DATA == 42`. *(documented; and consistent with the x86 run in §6)*
- **On AArch64 it happens.** §6 observes it **923–1,190 times per million trials**
  *(measured)*.

This is why the "it works on my Intel laptop, it corrupts on Graviton" bug exists as a
genre. The publish-then-read-flag idiom — the most common lock-free pattern there is —
is *accidentally correct* on x86 and *actually wrong* on ARM.

> **An honest limit on this explanation.** The invalidate queue is McKenney's model, and
> it is the standard pedagogy. My §6 measurement proves the *outcome* is architecturally
> permitted and empirically frequent on an M3 Pro. It does **not** prove which
> microarchitectural structure produced it: Apple does not document its coherence
> implementation, and the same outcome can arise from store-buffer drain reordering on
> the writer, load speculation/reordering on the reader, or a non-multi-copy-atomic
> interconnect. Do not claim you have measured an invalidate queue. You have measured
> that the AArch64 memory model permits what x86-TSO forbids, which is the part you
> reason with.

---

## 3. Two reorderings, two different enemies

Every discussion of memory ordering that stays confusing is confusing because it merges
two independent phenomena. Separate them and everything gets simpler.

| | **Compiler reordering** | **Hardware reordering** |
|---|---|---|
| Who does it | the optimizer, at build time | the core, at run time |
| Why | register allocation, CSE, dead-store elimination, loop-invariant hoisting, vectorization | store buffers, invalidate queues, out-of-order issue, speculation |
| What it's allowed to do | anything invisible to a **single-threaded** observer (the "as-if" rule) | anything the **ISA memory model** permits |
| Visible where | in the generated assembly | only across cores |
| Defeated by | compiler barriers, `volatile`, `_Atomic` | CPU barriers, ordered instructions |
| Present on x86? | **yes, fully** | partly (StoreLoad only) |
| Present on ARM? | **yes, fully** | yes, all four |

**Both must be stopped, and they are stopped by different mechanisms.** A C11 atomic
operation with a non-relaxed order stops both at once, which is the entire reason the
C11 model exists: it gives you one knob instead of two, expressed in terms of *program
semantics* rather than *this year's CPU*.

The four elementary reorderings, in the standard naming (Preshing's, and it is worth
memorizing):

```
  LoadLoad    a later load moves before an earlier load
  LoadStore   a later store moves before an earlier load
  StoreStore  a later store moves before an earlier store      ← MP test dies here
  StoreLoad   a later load moves before an earlier store       ← SB test dies here
```

| Reordering | x86-TSO | AArch64 |
|---|---|---|
| LoadLoad | no | **yes** |
| LoadStore | no | **yes** |
| StoreStore | no | **yes** |
| StoreLoad | **yes** | **yes** |

Read that table as the compressed form of this whole document. x86 permits exactly one
of the four; ARM permits all four. **StoreLoad is the one nobody can avoid**, because
avoiding it means draining the store buffer before every load, which no design does. It
is why even x86 needs a fence in Dekker's algorithm, and why `mfence` / `dmb ish` exist
at all.

---

## 4. `volatile` is not a concurrency tool — proved in assembly

This is the most durable misconception in systems programming, and it is settleable by
looking at real output rather than arguing. Three C functions, compiled here
*(measured)*:

```c
int data; int flag;
volatile int vdata; volatile int vflag;
atomic_int adata, aflag;

void plain_reorder(void)    { flag = 1;  data = 42;  flag = 2; }
void volatile_reorder(void) { vflag = 1; vdata = 42; vflag = 2; }

void volatile_publish(void) { vdata = 42; vflag = 1; }
void release_publish(void)  { atomic_store_explicit(&adata, 42, memory_order_relaxed);
                              atomic_store_explicit(&aflag,  1, memory_order_release); }
```

### 4.1 What `volatile` *does* buy you: the compiler stops deleting your stores

`clang -O2 -S`, AArch64 *(measured)*:

```asm
_plain_reorder:                     ; flag=1; data=42; flag=2;
        mov     w9, #42
        str     w9, [x8]            ; data = 42
        mov     w9, #2
        str     w9, [x8]            ; flag = 2
        ret                         ; ← "flag = 1" IS GONE. Dead-store elimination.
                                    ;   Two stores emitted for three in the source.

_volatile_reorder:                  ; vflag=1; vdata=42; vflag=2;
        mov     w9, #1
        str     w9, [x8]            ; vflag = 1    ← survives
        mov     w10, #42
        str     w10, [x9]           ; vdata = 42
        mov     w9, #2
        str     w9, [x8]            ; vflag = 2
        ret                         ; ← all three, in source order.
```

So `volatile` is real: it forbids elision, duplication, and reordering *of volatile
accesses relative to each other*. That is genuinely useful for memory-mapped I/O
registers, for variables touched by a signal handler on the same thread, and for
`setjmp`/`longjmp` locals. Those are its three legitimate uses.

### 4.2 What `volatile` does not buy you: any hardware ordering at all

Same compiler, same flags, the publish idiom *(measured)*:

```asm
_volatile_publish:                  ; vdata = 42; vflag = 1;
        mov     w9, #42
        str     w9, [x8]            ; plain store
        mov     w9, #1
        str     w9, [x8]            ; plain store    ← NO BARRIER. NOTHING.
        ret

_release_publish:                   ; relaxed store; release store
        mov     w9, #42
        str     w9, [x8]            ; plain store
        mov     w9, #1
        stlr    w9, [x8]            ; STORE-RELEASE  ← the ordering instruction
        ret
```

**`volatile_publish` emits two ordinary `str` instructions.** The AArch64 core is free to
let those two stores reach other cores in either order, and §6 measures that it does.
`volatile` constrained the *compiler* and said nothing to the *machine*. On a weakly
ordered ISA that is approximately half of the job, and the half it skips is the half that
bites.

### 4.3 The reason the myth is so persistent

Compile the same release store for x86-64 *(measured)*:

```asm
_release_publish:                   ## x86-64
        movq    _adata@GOTPCREL(%rip), %rax
        movl    $42, (%rax)         ## plain mov
        movq    _aflag@GOTPCREL(%rip), %rax
        movl    $1, (%rax)          ## plain mov   ← a RELEASE store is a plain mov
        retq
```

On x86-64, a release store compiles to *exactly the same instruction* as a `volatile`
store, because TSO already forbids StoreStore reordering. So on x86, `volatile` really
does happen to give you release/acquire ordering for aligned word-sized accesses — by
accident, as a side effect of the ISA. A generation of Windows and Linux developers
learned "volatile works" on x86, and MSVC even formalized the accident with
`/volatile:ms`. **That knowledge does not port.** It is the single most common source of
code that is correct on x86-64 and broken on aarch64 with no source change — the exact
question README's Tier 0 checklist asks.

**The rule:** `volatile` is for memory that changes for reasons outside the C abstract
machine. `_Atomic` / `std::atomic` is for memory shared with other threads. They are not
interchangeable, they solve different problems, and only one of them talks to the CPU.

---

## 5. x86-TSO vs AArch64: the spine of this document

### 5.1 x86-TSO in one paragraph

**Total Store Order.** Every core has a FIFO store buffer. Loads may be satisfied from
the local store buffer (forwarding) or from memory. Stores drain to a single global
memory in FIFO order per core, and all cores observe that single global sequence of
drains. Consequences: a core's stores reach memory *in program order* (no StoreStore
reordering); loads are not reordered with each other (no LoadLoad); and the only visible
reordering is a load overtaking an earlier store to a *different* address (StoreLoad).
Locked read-modify-writes are full barriers.

x86-TSO is a genuinely rigorous model — Sewell, Sarkar, Owens et al. formalized it after
Intel's own prose documentation was found to be ambiguous and, in places, wrong. That
history is worth knowing: **the vendor could not state its own memory model precisely
until academics forced the issue.**

### 5.2 AArch64 in one paragraph

**Weakly ordered, other-multi-copy-atomic.** Loads and stores to different addresses may
be observed in any order by other cores unless ordering is explicitly requested. Ordering
comes from three places: address/data/control dependencies (which the hardware respects),
explicit barriers (`dmb`, `dsb`, `isb`), and the load-acquire / store-release instruction
family (`ldar`/`ldapr`/`stlr` and the `ldaxr`/`stlxr` exclusives). Since ARMv8, the
architecture is *other-multi-copy-atomic*: if any core other than the writer observes a
store, all such cores observe it — which rules out the nastiest IRIW-style outcomes that
ARMv7 and POWER permit. *(documented — ARM ARM.)*

That last property matters more than it sounds. ARMv8 is weak, but it is **not** as weak
as POWER or the DEC Alpha. Alpha famously reordered *dependent* loads, which is why the
Linux kernel needed `smp_read_barrier_depends()` — a barrier for a case every other
architecture handles for free. ARMv8 respects address dependencies. You still cannot
rely on that in portable C, because the *compiler* will happily break a dependency the
hardware would have honoured (§14).

### 5.3 The comparison that matters

| | x86-64 (TSO) | AArch64 (weak) |
|---|---|---|
| Plain load | acquire-ish for free | **no ordering** |
| Plain store | release-ish for free | **no ordering** |
| Acquire load | `mov` (free) | `ldapr` / `ldar` |
| Release store | `mov` (free) | `stlr` |
| Seq-cst store | `xchg` (or `mov`+`mfence`) | `stlr` |
| Seq-cst fence | `lock or $0,(%rsp)` / `mfence` | `dmb ish` |
| RMW | `lock`-prefixed, always a full barrier | `ldadd`/`cas` families, ordering is **encoded per-instruction** |
| Cost of getting it wrong | usually nothing | **usually a bug** |

The bottom-right cell is the practical thesis. On x86 the memory model is forgiving
enough that a large fraction of incorrect concurrent code passes its tests. On ARM the
same code fails — and fails rarely, non-deterministically, and under load, which is the
worst possible failure profile.

The right-hand column also explains something in
[`24-the-gil.md` §9](24-the-gil.md#9-free-threadings-new-cost-model): the free-threading
single-thread overhead is reported at roughly **1% on macOS aarch64 and 8% on x86-64
Linux**. That spread is not noise and it is not "ARM is faster". It is that AArch64's
acquire/release instructions are *individually ordered* — `ldar` and `stlr` are single
instructions carrying their own semantics — whereas x86 must express the same intent
either for free (release/acquire) or with a full-barrier RMW (`lock`-prefixed, seq-cst,
no cheaper option available). Where CPython needs a seq-cst RMW, x86 has exactly one
price and it is high; AArch64 has a ladder of prices and CPython can sometimes buy
lower down.

---

## 6. The litmus tests, run on this machine

A **litmus test** is a minimal program whose outcome distinguishes memory models. Two
threads, a handful of accesses, and one outcome that is either permitted or forbidden.
This is how memory models are specified, tested, and argued about — the `litmus7` /
`herd7` tools from the Sewell/Maranget group do exactly this at scale.

The harness used here: a sense-reversing two-thread barrier, then a randomized
`nop`-delay so the two threads' critical windows overlap, then the test, then a second
barrier, then a referee that classifies the round. 1,000,000 rounds per configuration.
Full source is in lab 1 (§17); the delay magnitude is a swept parameter because the
observation rate is extremely sensitive to it (see §6.3).

### 6.1 SB — store buffering (Dekker)

```
   initially X = 0, Y = 0

   Thread 0                          Thread 1
   ─────────────────────             ─────────────────────
   store X = 1                       store Y = 1
   r1 = load Y                       r2 = load X

   Forbidden under sequential consistency:   r1 == 0 && r2 == 0
   Permitted under x86-TSO:                  YES  (StoreLoad)
   Permitted under AArch64:                  YES  (StoreLoad)
```

*(measured, native arm64, 1,000,000 rounds each)*

| Configuration | `r1==0 && r2==0` observed | Rate |
|---|---|---|
| both accesses `memory_order_relaxed` | **15,093** | 1.509% |
| both accesses `memory_order_seq_cst` | **0** | 0% |
| relaxed + `atomic_thread_fence(seq_cst)` between store and load | **0** | 0% |

Two things to take from this. First, **1.5% is not a rare race.** A million-iteration
loop hits it fifteen thousand times. Anyone who says "the window is too small to matter
in practice" has not measured it. Second, both fixes work and they work completely —
zero occurrences in a million rounds, not "fewer".

### 6.2 MP — message passing, the one that separates the architectures

```
   initially DATA = 0, FLAG = 0

   Thread 0 (publisher)              Thread 1 (consumer)
   ─────────────────────             ─────────────────────
   store DATA = 42                   r1 = load FLAG
   store FLAG = 1                    r2 = load DATA

   Forbidden under sequential consistency:   r1 == 1 && r2 == 0
   Permitted under x86-TSO:                  NO   ← no StoreStore, no LoadLoad
   Permitted under AArch64:                  YES  ← both are permitted
```

*(measured, 1,000,000 rounds each)*

| Configuration | Binary | `FLAG==1 && DATA==0` | Rate |
|---|---|---|---|
| all `relaxed` | **native arm64** | **1,190** / **923** (two runs) | 0.119% / 0.092% |
| `release` store + `acquire` load | native arm64 | **0** | 0% |
| all `relaxed` | **x86-64** (see caveat) | **0** | 0% |
| `release` store + `acquire` load | x86-64 | **0** | 0% |

**That table is the document.** Identical C source. Identical machine. Identical physical
cores. The arm64 binary observes the outcome roughly a thousand times per million; the
x86-64 binary observes it exactly zero times, because x86-TSO forbids it. And on arm64,
changing two `memory_order_relaxed` tokens to `release`/`acquire` — a change that costs
you `str`→`stlr` and `ldr`→`ldapr`, two instructions — eliminates it entirely.

The SB test is the control: it should be observable on *both*, since both models permit
StoreLoad. It is *(measured)*:

| SB relaxed | Observed / 1M | Rate |
|---|---|---|
| native arm64 | 14,335 | 1.434% |
| x86-64 binary | 13,694 | 1.369% |

Nearly identical rates. The two architectures agree about SB and disagree about MP,
which is exactly what the §3 reordering table predicts. That the control matches is what
makes the MP result trustworthy rather than an artifact of the x86 build being slower or
differently scheduled.

> ### The Rosetta caveat — read this before quoting the x86 numbers
>
> I do not have an Intel or AMD machine. The x86-64 binary above was cross-compiled with
> `clang -arch x86_64` and **executed under Rosetta 2 on the same M3 Pro**. Rosetta
> translates x86-64 to AArch64 and enables Apple Silicon's hardware **TSO mode**, a
> per-thread configuration bit that makes the core's load/store unit enforce
> total-store-order semantics. So what I measured is: *x86-TSO semantics, enforced by
> Apple hardware, on the same physical cores.*
>
> **What this legitimately shows:** the ordering *semantics* are what x86-TSO specifies,
> and the difference is attributable to the memory model rather than to the machine,
> the scheduler, the cores, or the thermal state — every one of which is held constant.
> As a controlled experiment isolating the memory model, it is unusually clean.
>
> **What it does not show:** that a real Intel or AMD chip produces these exact rates.
> Rosetta's TSO mode is an *implementation* of TSO, not an Intel core. The SB rates
> matching within 5% is encouraging but is not proof of equivalence, and I have not
> validated Rosetta's TSO mode against real x86 silicon. **If you have an x86 box,
> lab 2 asks you to rerun this and tell me whether I'm right.**

### 6.3 The methodological trap, stated honestly

My first three attempts at this measurement produced **zero** observations, and the
conclusion "AArch64 doesn't reorder" was available and completely wrong. What was
actually happening:

1. The first harness had a **broken barrier** — a non-sense-reversing counter that
   deadlocked as soon as one thread lapped the other. It hung for two minutes and
   produced nothing.
2. The fixed harness produced 0/100,000 because the post-barrier delay was too short.
   The two threads exit a barrier hundreds of cycles apart (the second arriver proceeds
   immediately; the first is spinning on a cache line that must migrate), so their
   critical windows never overlapped.

Sweeping the delay magnitude *(measured, 200,000 rounds, SB relaxed)*:

| max delay (nops) | observed | rate |
|---|---|---|
| 2 | 11 | 0.006% |
| 8 | 25 | 0.013% |
| 32 | 2 | 0.001% |
| 128 | 266 | 0.133% |
| **512** | **3,184** | **1.592%** |
| 2048 | 1,182 | 0.591% |

There is a resonance at ~512 nops and it falls off on both sides. **A negative result in
a memory-model experiment is nearly worthless unless you have swept the timing.** This
is the concurrency-testing version of the lesson in
[`31-measurement-methodology.md`](31-measurement-methodology.md), and it is why
[`30-concurrency-correctness.md`](30-concurrency-correctness.md) argues for model
checkers (`herd7`, TSan, Loom) over stress tests: a stress test that finds nothing has
told you nothing.

---

## 7. The C11/C++11 memory model, and what it compiles to

C11 and C++11 adopted essentially the same model (Boehm & Adve's design). It is the
first time a mainstream language specified what a shared-memory program *means*, and
every language since — Rust, Swift, and CPython's `_Py_atomic_*` — is either a copy of it
or a deliberate deviation from it.

### 7.1 The five orderings

| Order | Guarantee | Use it for |
|---|---|---|
| `relaxed` | **atomicity only** — no ordering with any other access | counters you only ever read at the end; refcount fast paths; statistics |
| `acquire` | on a **load**: no later access may move before it | acquiring a lock; reading a flag whose payload you're about to read |
| `release` | on a **store**: no earlier access may move after it | releasing a lock; publishing a payload then setting its flag |
| `acq_rel` | both, on a **read-modify-write** | a CAS that both consumes and publishes |
| `seq_cst` | acquire/release **plus** a single total order all threads agree on | the default; anything you are not sure about |

Two mental models are worth carrying:

**Acquire/release are one-way barriers.** This is the picture that makes them click:

```
       ── ACQUIRE (a load) ──                    ── RELEASE (a store) ──

   ...anything...                            ...anything...
        │                                         │
        │  ▲ may move DOWN across             ▼   │  may move UP across
        ▼  │ (upward motion blocked)          │   ▼  (downward motion blocked)
   ┏━━━━━━━━━━━━━━━━━━┓                  ┏━━━━━━━━━━━━━━━━━━┓
   ┃  ldapr / ldar    ┃                  ┃      stlr        ┃
   ┗━━━━━━━━━━━━━━━━━━┛                  ┗━━━━━━━━━━━━━━━━━━┛
        ▲  │                                 │   ▲
        │  │ nothing below may move UP    ───┘   │ nothing above may move DOWN
        │  ▼ past the acquire                    │
   ...critical section...                   ...critical section...
```

A critical section is exactly an acquire at the top and a release at the bottom. Things
may leak *into* it from either side; nothing may leak *out*. That asymmetry is why the
pair is cheaper than two full barriers, and why the MP test needs precisely one of each.

**`release` + `acquire` on the same variable creates a *synchronizes-with* edge.** If
thread A does a release store to `F` and thread B does an acquire load of `F` and *reads
the value A stored*, then everything A did before the store *happens-before* everything B
does after the load. That is the entire cross-thread ordering primitive; locks,
channels, futures and `Py_INCREF`'s escape path are all built from it.

### 7.2 What each ordering actually compiles to

`clang -O2 -S` on this machine, both targets, from the same source file *(measured)*.
This table is the one to memorize:

| C11 operation | **AArch64** (Apple target) | **x86-64** |
|---|---|---|
| `load(relaxed)` | `ldr w0, [x8]` | `movl (%rax), %eax` |
| `load(acquire)` | **`ldapr w0, [x8]`** | `movl (%rax), %eax` |
| `load(seq_cst)` | **`ldar w0, [x8]`** | `movl (%rax), %eax` |
| `store(relaxed)` | `str w0, [x8]` | `movl %edi, (%rax)` |
| `store(release)` | **`stlr w0, [x8]`** | `movl %edi, (%rax)` |
| `store(seq_cst)` | `stlr w0, [x8]` | **`xchgl %edi, (%rax)`** |
| `fetch_add(relaxed)` | **`ldadd w9, w0, [x8]`** | `lock xaddl %eax, (%rcx)` |
| `fetch_add(acq_rel)` | **`ldaddal w9, w0, [x8]`** | `lock xaddl %eax, (%rcx)` |
| `fetch_add(seq_cst)` | `ldaddal w9, w0, [x8]` | `lock xaddl %eax, (%rcx)` |
| `exchange(seq_cst)` | `swpal w0, w0, [x8]` | `xchgl %eax, (%rcx)` |
| `compare_exchange_weak` | `casal w9, w1, [x10]` | `lock cmpxchgl %esi, (%rdx)` |
| `compare_exchange_strong` | `casal w9, w1, [x10]` | `lock cmpxchgl %esi, (%rdx)` |
| `thread_fence(acquire)` | **`dmb ishld`** | *(nothing emitted)* |
| `thread_fence(release)` | `dmb ish` | *(nothing emitted)* |
| `thread_fence(seq_cst)` | `dmb ish` | **`lock orl $0, -64(%rsp)`** |

**Five things in that table are worth stopping on.**

**(a) On x86-64, the entire left half of the ordering spectrum is free.** Relaxed,
acquire, and seq-cst *loads* are the identical `movl`. Relaxed and release *stores* are
the identical `movl`. There is literally no instruction difference. This is TSO's dividend
and it is why x86 programmers underestimate memory ordering: on their machine three of
the five orderings are the same instruction.

**(b) On x86-64, seq-cst *stores* are where you suddenly pay.** `store(seq_cst)` becomes
`xchgl` — a locked RMW, a full barrier, tens of cycles — while `store(release)` was a
free `mov`. **The single largest x86 optimization in concurrent code is downgrading a
seq-cst store to a release store**, and it is invisible in the source until you look at
the assembly. (Clang chooses `xchg` over `mov; mfence` here; both are correct and `xchg`
is generally faster on modern parts.)

**(c) The acquire load is `ldapr`, not `ldar`.** `ldar` is Load-Acquire with
*sequentially consistent* semantics (RCsc): it also orders against earlier `stlr`s.
`ldapr` is Load-AcquirePC (RCpc, ARMv8.3-A) and provides only the acquire guarantee C11
asks for. Clang emits `ldapr` for `memory_order_acquire` and reserves `ldar` for
`memory_order_seq_cst` *(measured — and confirmed by targeting a pre-8.3 core: compiling
the same source with `-mcpu=cortex-a53` emits `ldar` for the acquire load, because
`ldapr` does not exist there)*. **This is a real, visible cost difference between
acquire and seq_cst on AArch64 that has no x86 analogue.** *(The RCpc-vs-RCsc semantics
are documented in the ARM ARM; I verified the instruction selection, not the hardware
semantics.)*

**(d) AArch64 encodes the ordering into the RMW opcode.** `ldadd` / `ldadda` / `ldaddl` /
`ldaddal` are four distinct instructions for relaxed / acquire / release / acq_rel. x86
has one `lock xadd` and it is always seq-cst. So on ARM, `fetch_add(relaxed)` is
genuinely cheaper than `fetch_add(seq_cst)`; on x86 they are byte-identical. §12
measures whether that translates into time (spoiler: uncontended, barely; contended,
yes).

**(e) Clang does not use `mfence` for a seq-cst fence.** It emits
`lock orl $0, -64(%rsp)` — a locked no-op on the red zone. A locked RMW is a full barrier
and is measurably faster than `mfence` on most modern x86 parts; the `-64(%rsp)` offset
keeps it clear of live stack data. This is the kind of detail that makes people think
they've found a compiler bug. They haven't. *(measured emission; the "faster than mfence"
claim is well-established folklore that I did not benchmark here.)*

### 7.3 The `consume` ordering, and why you should ignore it

C11 also defines `memory_order_consume`, intended to expose the fact that hardware
respects address dependencies so a dependent load needs no barrier at all (the RCU idiom).
It has never been implemented: **every major compiler silently promotes `consume` to
`acquire`**, because tracking dependency chains through arbitrary optimizer passes turned
out to be intractable to specify. It is effectively deprecated. Know the word so you
recognize it in kernel code; do not use it. This is a rare, honest instance of a
standards committee shipping something that could not be built.

---

## 8. Fences vs. ordered accesses

There are two ways to express ordering and they are not equivalent.

**Ordered accesses** (`ldar`, `stlr`, `ldaddal`) attach ordering to a *specific* memory
operation. **Fences** (`dmb ish`) order *everything* before against *everything* after,
and are not tied to any address.

```
   ORDERED ACCESS (stlr)                    FENCE (dmb ish)

   str  w0, [data]                          str  w0, [data]
   stlr w1, [flag]   ← ordering rides       dmb  ish          ← orders ALL prior
                       on this one store    str  w1, [flag]     against ALL later
                       to this one address

   Cheaper. Expresses "publish this".       More expensive. Expresses "everything
   The hardware can be surgical.            up to here is done." Blunt instrument.
```

Prefer ordered accesses. Reach for a fence when the ordering isn't attached to a single
access — most commonly the SB/Dekker pattern, where you need StoreLoad ordering between a
store to `X` and a load of `Y`, two different addresses. §6.1 measured that
`relaxed store; fence(seq_cst); relaxed load` fixes SB just as completely as making both
accesses seq_cst.

Note the asymmetry the §7.2 table exposes: on AArch64 `thread_fence(acquire)` is
`dmb ishld` (a *load*-ordering barrier — cheaper, it lets stores pass) while
`thread_fence(release)` is the full `dmb ish`. There is no "store-only" `dmb` variant
suitable for a release fence, so release fences cost more than acquire fences on ARM
*(measured emission)*.

---

## 9. Read-modify-write: CAS, LL/SC, and the exclusive monitor

Ordering is half the problem. The other half is **atomicity of read-modify-write**:
performing load-modify-store such that no other core can interleave. Two hardware
families exist.

### 9.1 Atomic instructions (x86 always; ARM since ARMv8.1-A "LSE")

A single instruction does the whole RMW. `lock xadd`, `lock cmpxchg`, `xchg` on x86;
`ldadd`, `swp`, `cas` on AArch64 with **LSE** (Large System Extensions). Apple Silicon
has LSE, which is why every listing in §7.2 shows `ldadd` and `casal` rather than a loop.

The important architectural property: **an LSE atomic can be executed at the point of
coherence** rather than requiring the line to migrate into the requesting core's L1. For
a heavily contended counter, that turns "N cores take turns owning the line" into "N
cores send N requests to one place". That is a fundamentally better scaling shape, and
it is why ARM added LSE for server parts. §12 shows this machine still degrades under
contention — LSE improves the constant, not the asymptote.

### 9.2 Load-Linked / Store-Conditional (the classic ARM/RISC-V/POWER mechanism)

Before LSE, and still on any pre-8.1 core and on RISC-V, ARM has no atomic RMW
instruction at all. It has a *pair*:

- **`ldxr`** (Load Exclusive) — load, and mark this address in the core's **exclusive
  monitor**.
- **`stxr`** (Store Exclusive) — store *only if* the monitor is still marked; write
  success/failure into a register.

Anything that might have disturbed the location clears the monitor: another core's write,
a context switch, sometimes an interrupt, sometimes an unrelated access to the same
**reservation granule** (an implementation-defined block, typically the cache line, not
the word).

Forcing a pre-LSE target *(measured, `clang -mcpu=cortex-a53 -O2 -S`)*:

```asm
_fa_relaxed:                        ; atomic_fetch_add_explicit(&g, 1, relaxed)
        adrp    x8, _g@GOTPAGE
        ldr     x8, [x8, _g@GOTPAGEOFF]
LBB6_1:
        ldxr    w0, [x8]            ; load exclusive, set monitor
        add     w9, w0, #1
        stxr    w10, w9, [x8]       ; store conditional; w10 = 0 on success
        cbnz    w10, LBB6_1         ; ← retry loop. THE ATOMIC IS A LOOP.
        ret

_fa_seqcst:                         ; same, seq_cst
LBB8_1:
        ldaxr   w0, [x8]            ; load-acquire exclusive
        add     w9, w0, #1
        stlxr   w10, w9, [x8]       ; store-release conditional
        cbnz    w10, LBB8_1
        ret
```

Note that the ordering is again encoded in the opcode: `ldxr`/`stxr` for relaxed,
`ldaxr`/`stlxr` for seq-cst. Same loop, different letters.

### 9.3 Why LL/SC can livelock

```
   Core 0                          Core 1                       Reservation granule
   ──────                          ──────                       (one cache line)
   ldxr  [addr]  ── monitor set ─▶ ...                          Core0 owns
   add                             ldxr  [addr] ── monitor set ▶ Core1 owns
                                                                (Core0's CLEARED)
   stxr  [addr]  ── FAILS ────────                              retry
   ldxr  [addr]  ── monitor set ─▶                              (Core1's CLEARED)
                                   stxr  [addr] ── FAILS ──────  retry
   ...                             ldxr  [addr] ── monitor set ▶
   stxr  [addr]  ── FAILS ────────                              retry
   ...                             ...                          forever
```

Neither core is blocked; both are making no progress. This is **livelock**, and it is a
different failure from deadlock — nothing is waiting, everything is spinning. The
architectures give only weak forward-progress guarantees here; ARM specifies constraints
on when an LL/SC pair *must* eventually succeed, but they are conditional on the loop
being small and containing no intervening memory accesses, which optimizers and
debuggers can silently violate. In practice you get:

- **Exponential backoff** in the retry loop (add a `yield`/`wfe` and grow the delay).
- **LSE** — the real fix, and why ARM built it. `ldadd` and `cas` cannot livelock, because
  there is no reservation to lose.
- On RISC-V, the ISA defines a **constrained LR/SC sequence** with an architectural
  forward-progress guarantee, but only if you obey strict rules about what may appear
  between the two instructions.

Two secondary hazards worth naming:

**False sharing kills LL/SC harder than it kills anything else.** The reservation granule
is typically a whole cache line. Two threads doing LL/SC on *different* words in the same
128-byte line will clear each other's monitors forever. This is
[doc 01 §6](01-memory-hierarchy-and-caches.md#6-false-sharing--and-the-128-byte-problem)'s
false sharing upgraded from "slow" to "may never terminate."

**LL/SC solves ABA that CAS doesn't** — the monitor is cleared by *any* intervening write,
so an A→B→A sequence fails the `stxr` even though the value compares equal. That is a
genuine advantage of LL/SC over CAS, and it is the entry point to
[`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md).

> **Honesty note.** I did **not** reproduce an LL/SC livelock. Apple Silicon has LSE and
> clang targets it by default, so the compiler will not emit LL/SC for me without a
> `-mcpu` override, and constructing a genuine livelock requires inline assembly plus
> favourable scheduling. The livelock mechanism above is documented architecture
> (ARM ARM, and it is the standard motivation given for LSE), not something I measured.
> Lab 6 attempts it if you want to settle it yourself.

---

## 10. `compare_exchange_weak` vs `strong`, in real instructions

C11 gives you two CAS variants and the difference confuses everyone. It stops being
confusing the moment you look at the code generation, because the difference *is* the
code generation.

```c
atomic_compare_exchange_weak_explicit(&g, &expected, desired, acq_rel, acquire);
atomic_compare_exchange_strong_explicit(&g, &expected, desired, acq_rel, acquire);
```

**`strong`** fails only if `*obj != expected`. **`weak`** may *also* fail spuriously —
returning false with `expected` unchanged even though the comparison would have succeeded.

### 10.1 On an LSE target, they are identical

*(measured, default Apple target)*:

```asm
_cas_weak:                          _cas_strong:
        ldr     w8, [x0]                    ldr     w8, [x0]
        mov     x9, x8                      mov     x9, x8
        casal   w9, w1, [x10]               casal   w9, w1, [x10]
        cmp     w9, w8                      cmp     w9, w8
        cset    w8, eq                      cset    w8, eq
        ...                                 ...
```

Byte-for-byte the same, because `casal` is a single instruction that cannot fail
spuriously. Same on x86-64: both are `lock cmpxchgl` *(measured)*. **On both of the
architectures most people use today, `weak` and `strong` generate identical code for a
single CAS.** So why does the distinction exist?

### 10.2 On an LL/SC target, the difference is an entire loop

*(measured, `-mcpu=cortex-a53`)*:

```asm
_cas_weak:
        ldaxr   w8, [x9]            ; load exclusive
        cmp     w8, w10
        b.ne    LBB10_2             ; value mismatch → report failure
        stlxr   w10, w1, [x9]       ; try to store
        cmp     w10, #0
        csetm   w9, eq              ; success = (stxr returned 0)
        ...                         ; ← NO RETRY. A failed stxr just returns false.
LBB10_2:
        mov     w9, wzr
        clrex                       ; clear the reservation
        ...

_cas_strong:
LBB11_1:                            ; ←──────────────────┐
        ldaxr   w9, [x8]                                 │
        cmp     w9, w10                                  │
        b.ne    LBB11_4             ; real mismatch → out│
        stlxr   w11, w1, [x8]                            │
        cbnz    w11, LBB11_1        ; spurious failure ──┘  RETRY
        ...
LBB11_4:
        mov     w8, wzr
        clrex
        ...
```

There it is. **`strong` is `weak` plus a retry loop**, and the retry exists precisely to
absorb a failed `stxr` that was *not* caused by a value mismatch. A spurious failure is
not a fiction; it is a cleared exclusive monitor.

### 10.3 The rule that follows

```c
/* CORRECT and optimal: you were going to loop anyway. Use weak. */
long expected = atomic_load_explicit(&counter, memory_order_relaxed);
while (!atomic_compare_exchange_weak_explicit(
           &counter, &expected, expected + 1,
           memory_order_release, memory_order_relaxed)) {
    /* `expected` was reloaded for us by the failed CAS — recompute and retry */
}

/* CORRECT: a one-shot CAS with no loop of your own. Use strong,
   or you may report failure when you should have succeeded. */
if (atomic_compare_exchange_strong_explicit(&head, &expected, node,
        memory_order_acq_rel, memory_order_acquire)) { /* claimed it */ }
```

**If your CAS is already inside a retry loop, use `weak`** — you pay nothing for a
spurious failure because you were going to go round again anyway, and you save the
compiler's inner loop. **If a single failure changes your control flow, use `strong`.**
On LSE and x86 this costs you nothing today; on any LL/SC target it is the difference
between correct and subtly wrong, and it is free portability.

A detail people trip on: on failure, `expected` is **updated** to the value actually
found. That is the mechanism that makes the loop converge, and it means you must
recompute the desired value from the new `expected` inside the loop body — not hoist it
out. Hoisting it is one of the classic lock-free bugs.

---

## 11. Why `fetch_add` beats a CAS loop under contention

A CAS loop can implement any RMW, so it is tempting to reach for it always. It is a trap,
and the reason is structural rather than a matter of instruction cost.

```
   fetch_add: EVERY participant succeeds.        CAS loop: ONE participant succeeds
                                                           per round.

   4 cores, 4 increments:                        4 cores, 4 increments:

   core0  ldadd ──▶ ✓                            core0  ld;cas ──▶ ✓
   core1  ldadd ──▶ ✓                            core1  ld;cas ──▶ ✗ retry ─▶ ✓
   core2  ldadd ──▶ ✓                            core2  ld;cas ──▶ ✗ ✗ retry ─▶ ✓
   core3  ldadd ──▶ ✓                            core3  ld;cas ──▶ ✗ ✗ ✗ retry ─▶ ✓

   4 line acquisitions, 4 increments.            10 line acquisitions, 4 increments.
   Work per increment: O(1).                     Work per increment: O(N).
   Progress guarantee: WAIT-FREE.                Progress guarantee: LOCK-FREE only.
                                                 Any individual thread may starve.
```

Two distinct problems compound:

1. **Quadratic wasted work.** With N contending threads, the expected number of CAS
   attempts to complete N increments is O(N²). Every failed attempt still acquired the
   cache line exclusively, so every failure costs a full coherence round trip — you are
   paying maximum price for zero progress.
2. **The progress guarantee is weaker.** `fetch_add` is **wait-free**: every thread
   completes in a bounded number of its own steps. A CAS loop is only **lock-free**: the
   *system* makes progress, but an unlucky thread can fail forever. On heterogeneous
   cores this is not theoretical — an E-core thread competing with P-core threads for the
   same line loses repeatedly. (Progress hierarchies get their proper treatment in
   [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md).)

Measured on this machine, aggregate throughput in millions of increments/second, all
threads hammering one shared counter *(measured, 50M ops per thread)*:

| threads | `fetch_add(relaxed)` | CAS loop | CAS loop penalty |
|---|---|---|---|
| 1 | 511 | 503 | 1.0× |
| 2 | 265 | 321 | *0.83×* (CAS faster) |
| 3 | 188 | 148 | 1.27× |
| 4 | 122 | 98 | 1.25× |
| **5** | **77** | **37** | **2.11×** |
| **6** | **90** | **31** | **2.93×** |
| 8 | 52 | 39 | 1.33× |

Uncontended they are the same instruction cost. **At 5–6 threads the CAS loop is 2–3×
worse**, which is exactly where §12 shows contention becoming the dominant term. (The
non-monotonicity at 2 threads and at 8 is real run-to-run structure on this machine, not
a typo — see §12.4 on why this laptop cannot produce a clean scaling curve.)

**The rule: if the operation you need has a native atomic, use it.** `fetch_add`,
`fetch_sub`, `fetch_or`, `fetch_and`, `exchange`. Reserve CAS for operations that
genuinely have no atomic form — updating a pointer, a multi-field state word, or anything
conditional. This is also why CPython's `Py_INCREF` uses `_Py_atomic_add_ssize` on its
escape path rather than a CAS loop (§15).

---

## 12. The atomic cost model — measured

This is the mandatory lab from the roadmap, and it is the number every engineer arguing
about lock-free data structures should have in their head.

### 12.1 Uncontended: what one atomic costs

Single thread, 50,000,000 operations, `clang -O2` *(measured, ns/op, two runs each)*:

| Operation | AArch64 instruction | ns/op | ≈ cycles @ ~4 GHz |
|---|---|---|---|
| plain `volatile` increment (baseline) | `ldr`/`add`/`str` | **0.27** | ~1 |
| atomic load, relaxed | `ldr` | 1.20 – 1.27 | ~5 |
| atomic load, acquire | `ldapr` | 1.36 | ~5.5 |
| atomic store, release | `stlr` | **0.27** | ~1 |
| `fetch_add`, relaxed | `ldadd` | 1.95 | ~8 |
| `fetch_add`, seq_cst | `ldaddal` | 1.90 – 1.92 | ~8 |
| CAS loop (uncontended) | `casal` | 1.90 | ~8 |

**Findings, and two of them are surprising.**

**An uncontended atomic RMW costs about 2 ns — roughly 7× a plain increment, not 100×.**
The folklore figure of "an atomic costs 100 cycles" is about the *contended* case. When
the line is already in your L1 in M state and nobody else wants it, an `ldadd` is
single-digit cycles. Doc 01 §1's table says 20–50 cycles for an uncontended atomic RMW;
on this machine I measure roughly 8. **That row in doc 01 is pessimistic for Apple
Silicon with LSE, and I am flagging it rather than quietly agreeing with it.**

**Relaxed and seq_cst `fetch_add` cost the same uncontended (1.95 vs 1.90 ns).** Despite
being different instructions (`ldadd` vs `ldaddal`), the ordering is nearly free when
there is nothing to order against — the store buffer is empty and there is no coherence
traffic to serialize. **The cost of ordering is not the barrier instruction; it is the
work the barrier makes you wait for.** This is the single most useful correction to
intuition in this section, and it means "use relaxed for speed" is close to worthless
advice in the uncontended case and worth real money in the contended one (§12.2).

**A release store (`stlr`) is as cheap as a plain store here (0.27 ns each).** Publishing
correctly costs approximately nothing on this microarchitecture. There is no performance
excuse for the `volatile` publish idiom of §4.2.

### 12.2 Contended: the scaling collapse

All threads hammering one shared 64-bit counter, versus each thread owning a
128-byte-padded counter. Aggregate throughput in **millions of operations/second**
*(measured, 50M ops per thread)*:

| threads | **padded** (per-thread) | **shared relaxed** | **shared seq_cst** | **CAS loop** | **false-shared** (8 B apart) |
|---|---|---|---|---|---|
| 1 | 485 | 511 | 490 | 503 | 502 |
| 2 | 1,007 | 265 | 153 | 321 | 252 |
| 3 | 1,484 | 188 | 158 | 148 | 195 |
| 4 | 1,972 | 122 | 113 | 98 | 146 |
| 5 | 2,331 | 77 | 60 | 37 | 59 |
| 6 | 2,631 | 90 | 65 | 31 | 83 |
| 8 | **3,260** | **52** | **46** | **39** | **53** |

```
   Aggregate Mops/s vs thread count (log-ish, measured on M3 Pro)

   3260 ┤                                                    ●  padded
        │                                        ●
   2000 ┤                            ●
        │                ●
   1000 ┤        ●
        │
    500 ┤●●●●● ← all five modes start together at 1 thread
        │  ╲
    250 ┤   ●───●
        │        ╲───●
    100 ┤             ╲──●
        │                 ╲●───●──● shared / false-shared / seq_cst / CAS
     25 ┤
        └──┬────┬────┬────┬────┬────┬────┬──
           1    2    3    4    5    6    8   threads
```

**Read the first and last rows against each other.** Going from 1 thread to 8:

- **Padded: 485 → 3,260 Mops/s.** A 6.7× speedup on 8 threads (11 cores, 5 of them fast).
  Near-linear until the E-cores join. This is what "scales" looks like.
- **Shared: 511 → 52 Mops/s.** A **9.8× *slowdown*** in total system throughput. Eight
  cores doing one tenth the aggregate work of one core.

That is the whole lesson. **Contention on an atomic does not merely fail to scale — it
scales negatively.** Every added core makes the *total* worse, because each core spends
its time acquiring a cache line that the next core immediately takes away. Per-operation
cost went from 1.96 ns to 19.05 ns *(measured)*: a 10× penalty per operation, on top of
getting no parallelism for it.

This is exactly the shape of the Gilectomy's first failure —
[`24-the-gil.md` §7](24-the-gil.md#7-the-gilectomy-larry-hastings-seven-core-lesson):
*"roughly a 30% slowdown — and it got worse with more threads."* Larry Hastings made
`ob_refcnt` atomic and got this table. You have now reproduced it in 60 lines of C.

**Ordering is not free once contended.** `seq_cst` vs `relaxed` on the *same* shared
counter: 490 vs 511 at one thread (a wash, §12.1), but **60 vs 77 at five threads and 46
vs 52 at eight** — a consistent 10–25% penalty *(measured)*. The `ldaddal`'s ordering
requirement forces the store buffer to drain while a coherence transaction is already
outstanding. That is the mechanism by which "ordering costs nothing" becomes false.

**False sharing costs as much as true sharing.** The 8-bytes-apart column tracks the
truly-shared column within noise across the whole sweep (53 vs 52 at 8 threads). The
hardware cannot tell the difference, because the hardware's unit is the line. This is
[doc 01 §6](01-memory-hierarchy-and-caches.md#6-false-sharing--and-the-128-byte-problem)
measured with atomics instead of plain stores, and it confirms doc 01's advice: pad to
128 bytes on this machine, unconditionally.

### 12.3 The cost model to carry in your head

| Situation | Cost | What dominates |
|---|---|---|
| Atomic, line in local L1, no sharers | **~2 ns / ~8 cycles** | instruction latency |
| Atomic, line shared read-only, first write | + coherence round trip | invalidating other cores |
| Atomic, line ping-ponging between 2 cores | ~4 ns | line migration |
| Atomic, line ping-ponging among 5+ cores | **~13–27 ns and rising** | serialization |
| Atomic under a CAS loop, 5+ cores | **~27–32 ns** | wasted retries |
| Per-thread padded atomic, any N | **~0.3–2 ns**, scales linearly | nothing |

**The design rule that falls out: the cost of an atomic is not a property of the
instruction. It is a property of how many cores touch the line.** Which means the
optimization is never "use a cheaper atomic"; it is always "make fewer cores touch the
line." Sharding, per-thread accumulators, biased reference counting, immortalization —
every technique in PEP 703 is an instance of that one move.

### 12.4 Why you should distrust my contended numbers (and yours)

This laptop is a bad benchmark host and I want that on the record next to the table:

- **Heterogeneous cores.** 5 P-cores (128 KB L1d, 16 MB shared L2) and 6 E-cores (64 KB
  L1d, 4 MB shared L2). At ≤5 threads macOS *usually* uses P-cores; beyond that you get a
  mix, and a mixed set of cores contending for one line behaves qualitatively differently
  from a homogeneous set. **The 5→6 thread non-monotonicity in every shared column
  (77→90, 60→65, 59→83) is almost certainly this**, not a real inflection.
- **Cross-cluster coherence.** A line ping-ponging between a P-core and an E-core crosses
  a cluster boundary. That is a different, more expensive transaction than P↔P.
- **No pinning, no PMU.** macOS gives no `sched_setaffinity` equivalent for hard pinning
  and there is no `perf(1)`; I cannot confirm which cores ran, and I cannot count
  coherence events to prove the mechanism.
- **No frequency control.** No way to disable turbo or fix the clock. The cycle
  conversions in §12.1 assume ~4 GHz and are therefore approximate by construction.

**What survives all of that:** the *shapes*. Padded scales up; shared collapses; the
collapse is an order of magnitude; CAS is worse than `fetch_add` under contention;
seq_cst is worse than relaxed under contention. Those conclusions are robust to a factor
of two in any individual cell. The individual cells are not. Carry the shapes, re-measure
the cells. See [`31-measurement-methodology.md`](31-measurement-methodology.md).

---

## 13. Alignment, and what a split atomic really does

Every treatment of atomics warns that an atomic straddling two cache lines is
catastrophic. On x86 that is true and specific: `lock`-prefixed instructions on
misaligned memory are architecturally required to work, and the CPU implements that by
falling back to a **bus lock** — historically asserting `LOCK#`, on modern parts stalling
the entire coherence fabric. A single split-lock instruction can cost thousands of cycles
and stalls *every core in the system*, not just the offender. Linux gained
`split_lock_detect` to trap them, precisely because one misbehaving process could degrade
an entire host. *(documented — I have no x86 hardware to demonstrate it.)*

**On AArch64 the answer is completely different, and I measured it.**

64-bit `__atomic_fetch_add` at various offsets in a 256-byte-aligned buffer, 2,000,000
iterations each *(measured)*:

| offset | 8-byte aligned? | straddles a 128 B line? | result |
|---|---|---|---|
| 0 | yes | no | OK, 1.94 ns/op |
| 8 | yes | no | OK, 1.95 ns/op |
| **60** | **no** | no | **SIGBUS** |
| 64 | yes | no | OK, 1.96 ns/op |
| 120 | yes | no | OK, 1.95 ns/op |
| **124** | **no** | yes | **SIGBUS** |
| **126** | **no** | yes | **SIGBUS** |
| **127** | **no** | yes | **SIGBUS** |

And 128-bit atomics (which on AArch64 use the `casp` register-pair family), 1,000,000
iterations *(measured)*:

| offset | 16-byte aligned? | straddles a 128 B line? | result |
|---|---|---|---|
| 0 | yes | no | OK, 8.14 ns/op |
| 16 | yes | no | OK, 7.24 ns/op |
| 112 | yes | no | OK, 8.43 ns/op |
| **120** | **no** | **yes** | **SIGBUS** |
| 128 | yes | no | OK, 6.76 ns/op |

**The finding, stated precisely: on AArch64 a misaligned atomic is not slow, it is
illegal.** The core raises an alignment fault and the process takes `SIGBUS`. Note offset
60 — inside a single line, not straddling anything, but not 8-byte aligned: it faults
anyway. The architecture requires *natural* alignment for atomics regardless of line
geometry.

Then notice the corollary, which is the actually useful part: **a naturally aligned atomic
can never straddle a cache line.** An 8-byte value at an 8-byte-aligned address cannot
cross a 64- or 128-byte boundary; a 16-byte value at a 16-byte-aligned address cannot
either. The only way to construct offset 120's failure is to *deliberately* under-align a
16-byte atomic, which C11 will not let you do (`_Atomic` types carry their alignment
requirement) — I had to reach for `__atomic_fetch_add` on a hand-cast pointer.

**So the practical guidance differs by architecture and both halves matter:**

- **x86-64:** misaligned atomics are legal and catastrophic. This is a *performance and
  blast-radius* problem, invisible until profiled, and it happens in real code — packed
  structs, `#pragma pack`, hand-rolled serialization buffers, atomics inside a
  `char[]` arena.
- **AArch64:** misaligned atomics are illegal and immediate. This is a *crash*, which is
  strictly better engineering: you find it on the first execution rather than in
  production tail latency.
- **Both:** use `_Atomic`/`std::atomic` types and let the compiler enforce alignment.
  Never place an atomic in a packed struct. Never cast a `char*` offset into an atomic
  pointer.

Also worth noting from the second table: **a 128-bit atomic costs ~7–8 ns versus ~2 ns for
a 64-bit one — roughly 4× more, uncontended** *(measured)*. Double-width CAS is the
standard implementation of tagged pointers for ABA avoidance, so that 4× is the price of
the simplest ABA fix. [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md)
weighs it against hazard pointers and epochs, and this measurement is the input to that
comparison.

---

## 14. Data races are undefined behaviour, and SC-DRF is the bargain

Everything so far has been mechanism. This section is about what the *standard* says,
because that is what the optimizer obeys.

### 14.1 The definitions, precisely

A **data race** exists when two threads access the same memory location, at least one
access is a write, they are not ordered by *happens-before*, and at least one is
non-atomic. **In C11, C++11 and Rust, a program with a data race has undefined
behaviour — the entire program, not just the racy access.**

A **race condition** is different and is not necessarily a bug in the language-semantics
sense: it is an outcome that depends on timing. `x += 1` from two threads under the GIL
is a race condition (you lose updates) but not a data race (the GIL orders the accesses).
[`24-the-gil.md` §6](24-the-gil.md#6-what-the-gil-does-and-does-not-guarantee) draws
exactly this line, and it is the distinction that makes the atomicity table there make
sense.

### 14.2 Why "UB" is not lawyer-talk

The standard could have said "a racy read returns some value." It says UB instead, and
the reason is that the optimizer *needs* the stronger statement to do ordinary work:

```c
/* The optimizer is entitled to assume no other thread writes `flag`,
   because if one did without synchronization, the program is UB. */
while (!flag) { do_work(); }

/* So it may legally hoist the load out of the loop: */
if (!flag) { for (;;) { do_work(); } }      /* infinite loop. Legal. */
```

This is not hypothetical; it is one of the most common real manifestations of racy code,
and it is why "the value will be stale for a while but eventually we'll see it" is a
false model. There is no "eventually." The load may never be executed again.

Similarly, the compiler may **invent** writes (speculative store into a location it
proved it would write anyway), **tear** a write it thinks is unobserved, or **rematerialize**
a load so two reads of "the same" variable return different values within one expression.
Every one of these is legal for non-atomic accesses and forbidden for atomic ones. Making
a variable `_Atomic` with `memory_order_relaxed` costs you *nothing at the instruction
level* on either architecture (§7.2: relaxed load is a plain `ldr`/`movl`) and buys you
all of these guarantees. **Relaxed atomics are, on both of these architectures, free
protection against the compiler.** That is why PEP 703 requires them (§15).

### 14.3 SC-DRF: the deal the whole model is built on

The C11/C++11 model's central theorem is **SC-DRF — sequential consistency for
data-race-free programs**:

> If your program contains no data races when every atomic operation is `seq_cst`, then
> its behaviour is exactly as if all operations executed in a single global interleaving
> of the threads' program orders.

Read that as a contract with two sides:

- **You promise:** no data races. Every conflicting access pair is either ordered by
  synchronization or uses atomics.
- **The implementation promises:** you may reason with sequential consistency — the
  simple, intuitive model where the machine interleaves your threads and nothing
  reorders.

This is why "just use `seq_cst` and locks" is genuinely correct engineering advice and not
a cop-out. It buys back the mental model that store buffers took away. The relaxed
orderings are an *opt-out* from SC-DRF, and every use of them is a promise that you have
reasoned about the specific reordering you just permitted. Herlihy & Shavit's framing is
the one to keep: **sequential consistency is not the default behaviour of hardware; it is
a service you purchase with barriers, and DRF is the discount coupon.**

CPython, notably, has adopted exactly this posture in its own atomics API — the header
says so in as many words (§15).

---

## 15. CPython's `_Py_atomic_*` API, verified against the source

**Everything in this section was extracted from the actual header on this machine**,
`Include/cpython/pyatomic.h` from the CPython 3.14.6 installation at
`~/.local/share/uv/python/cpython-3.14.6-macos-aarch64-none/` *(measured — I parsed the
installed header, not the GitHub `main` branch, and not the `.c` sources, which are not
part of this installation)*.

### 15.1 The shape of the API

`Include/cpython/pyatomic.h` declares the portable surface; three backends implement it:

| File | Backend |
|---|---|
| `Include/cpython/pyatomic.h` | the public declarations + the documentation comment |
| `Include/cpython/pyatomic_gcc.h` | GCC/clang `__atomic_*` builtins |
| `Include/cpython/pyatomic_msc.h` | MSVC intrinsics (`_Interlocked*`) |
| `Include/cpython/pyatomic_std.h` | C11 `<stdatomic.h>` fallback |

**153 distinct `_Py_atomic_*` functions** *(measured — counted by parsing the header)*.
The ordering-suffix census:

| Suffix | Count | Meaning |
|---|---|---|
| *(none)* | **88** | **sequentially consistent** |
| `_relaxed` | 49 | relaxed |
| `_release` | 8 | release |
| `_acquire` | 7 | acquire |
| `_seq_cst` | 1 | `_Py_atomic_fence_seq_cst` |

And the header states its philosophy directly *(quoted verbatim from the installed
file)*:

```c
// Operations are sequentially consistent unless they have a suffix indicating
// otherwise. If in doubt, prefer the sequentially consistent operations.
```

**That is CPython choosing the §14.3 bargain, in a header comment.** The default is the
safe one; you must type extra characters to give up sequential consistency. Compare the
C++ standard library, where `memory_order_seq_cst` is also the default but the API makes
relaxed equally easy to reach.

### 15.2 The design decision hiding in the census

Look at which operations have relaxed variants and which do not *(measured — exact
function-name families from the header)*:

| Family | Orderings available |
|---|---|
| `_Py_atomic_load_*` | seq_cst, **`_relaxed`**, **`_acquire`** |
| `_Py_atomic_store_*` | seq_cst, **`_relaxed`**, **`_release`** |
| `_Py_atomic_add_*` | **seq_cst only** |
| `_Py_atomic_and_*`, `_Py_atomic_or_*` | **seq_cst only** |
| `_Py_atomic_exchange_*` | **seq_cst only** |
| `_Py_atomic_compare_exchange_*` | **seq_cst only** |
| `_Py_atomic_fence_*` | `_acquire`, `_release`, `_seq_cst` |

**There is no `_Py_atomic_add_ssize_relaxed`.** Every read-modify-write in CPython is
sequentially consistent, full stop. That is a deliberate, conservative choice: RMWs are
where lock-free algorithms live, they are where subtle ordering bugs are unfixable
after the fact, and the extra cost is small relative to the coherence traffic the RMW was
always going to cause (§12.1: 1.95 vs 1.90 ns uncontended — *nothing*). Relaxed is offered
only for plain loads and stores, where the win is real and the reasoning is local.

The exact acquire/release surface is small enough to list *(measured)*:

```
  _Py_atomic_load_int_acquire      _Py_atomic_store_int_release
  _Py_atomic_load_ptr_acquire      _Py_atomic_store_ptr_release
  _Py_atomic_load_ssize_acquire    _Py_atomic_store_ssize_release
  _Py_atomic_load_uint32_acquire   _Py_atomic_store_uint32_release
  _Py_atomic_load_uint64_acquire   _Py_atomic_store_uint64_release
  _Py_atomic_load_uintptr_acquire  _Py_atomic_store_uint_release
                                   _Py_atomic_store_uintptr_release
```

Pointer-sized and word-sized only. These are for exactly one job: **publishing an object
and then publishing the pointer to it** — the MP pattern of §6.2, which is the pattern
CPython's lock-free `dict` and `list` reads depend on.

The GCC backend is a thin, honest wrapper *(measured, verbatim)*:

```c
static inline void *
_Py_atomic_load_ptr_acquire(const void *obj)
{ return (void *)__atomic_load_n((void * const *)obj, __ATOMIC_ACQUIRE); }

static inline void
_Py_atomic_store_ptr_release(void *obj, void *value)
{ __atomic_store_n((void **)obj, value, __ATOMIC_RELEASE); }

static inline Py_ssize_t
_Py_atomic_add_ssize(Py_ssize_t *obj, Py_ssize_t value)
{ return __atomic_fetch_add(obj, value, __ATOMIC_SEQ_CST); }

static inline void
_Py_atomic_fence_seq_cst(void) { __atomic_thread_fence(__ATOMIC_SEQ_CST); }
```

So on this machine, `_Py_atomic_load_ptr_acquire` compiles to `ldapr` and
`_Py_atomic_store_ptr_release` to `stlr` (§7.2), and on x86-64 both compile to a plain
`mov`. Everything in §7 applies directly to CPython's source.

> **A note for readers of [`24-the-gil.md` §3](24-the-gil.md#3-the-eval-loop-where-the-gil-is-dropped).**
> That document quotes eval-loop code using a *generic* `_Py_atomic_load_relaxed(...)`.
> In 3.14.6 the names are **type-suffixed** — `_Py_atomic_load_int_relaxed`,
> `_Py_atomic_load_uintptr_relaxed`, and so on. The generic spelling is from an older
> CPython (the pre-3.13 `pyatomic.h` used `_Py_atomic_load_relaxed` on an
> `_Py_atomic_int` struct type). The mechanism is unchanged; the spelling is not. Check
> the header for the version you are reading.

### 15.3 The payoff: `Py_INCREF` on the free-threaded build

Here is the free-threaded `Py_INCREF`, verbatim from `Include/refcount.h` of the
**3.14.6 free-threading** installation *(measured)*:

```c
#if defined(Py_GIL_DISABLED)
    uint32_t local = _Py_atomic_load_uint32_relaxed(&op->ob_ref_local);
    uint32_t new_local = local + 1;
    if (new_local == 0) {
        _Py_INCREF_IMMORTAL_STAT_INC();
        // local is equal to _Py_IMMORTAL_REFCNT_LOCAL: do nothing
        return;
    }
    if (_Py_IsOwnedByCurrentThread(op)) {
        _Py_atomic_store_uint32_relaxed(&op->ob_ref_local, new_local);
    }
    else {
        _Py_atomic_add_ssize(&op->ob_ref_shared, (1 << _Py_REF_SHARED_SHIFT));
    }
```

Every claim in this document is visible in those eleven lines:

**The immortality check is first** (`new_local == 0`, i.e. `ob_ref_local` was
`UINT32_MAX`). No memory is written at all for `None`, `True`, small ints, interned
strings. Zero coherence traffic — [doc 01 §6](01-memory-hierarchy-and-caches.md#6-false-sharing--and-the-128-byte-problem)'s
"only *not writing* can save you", implemented.

**The owned fast path uses `_relaxed` load and `_relaxed` store.** Per §7.2 those compile
to a plain `ldr` and a plain `str` on AArch64 — **byte-identical to the GIL build's
non-atomic increment**. So why write `_Py_atomic_*` at all? Because of §14.2: another
thread *may* concurrently read `ob_ref_local` (the GC, a debugger, `sys.getrefcount`, the
escape path), and a plain non-atomic access racing with any concurrent access is UB, which
licenses the compiler to tear, invent, or hoist it. **The relaxed atomic buys correctness
against the optimizer at exactly zero instruction cost.** PEP 703 says this explicitly:

> *"Note that the above is pseudocode: in practice, the implementation should use
> 'relaxed atomics' to access `ob_tid` and `ob_ref_local` to avoid undefined behavior in
> C and C++."*
> — PEP 703, §Biased Reference Counting

That sentence is the entire §14 argument, in one line, in a PEP.

**The escape path uses `_Py_atomic_add_ssize` — seq_cst, and `fetch_add`, not CAS.** §11's
rule, applied: the operation has a native atomic, so use it rather than a CAS loop. And
it is seq_cst because §15.2 offers nothing else — CPython does not permit itself relaxed
RMWs.

**The refcount states are transitioned with CAS.** PEP 703 describes `ob_ref_shared`'s
low two bits as a state machine (`0b00` default → `0b01` weakrefs → `0b10` queued → `0b11`
merged) and notes: *"Transitioning states requires an atomic compare-and-swap on the
`ob_ref_shared` field."* That is the legitimate use of CAS from §10.3 — a conditional
multi-field update with no native atomic form.

**One more measured fact that surprised me:** `Include/cpython/pyatomic.h` is
**byte-identical between the GIL build and the free-threaded build** *(measured — I
compared the two installed files)*. The atomics API is always present and always
compiled; only the *call sites* differ, gated on `Py_GIL_DISABLED`. That is a sane
engineering choice — one API, no `#ifdef` maze at the point of use — and it means you can
read and reason about `pyatomic.h` from a normal Python install.

### 15.4 Where free-threaded CPython actually needs acquire/release

Per PEP 703's *Optimistically Avoiding Locking* section, `dict.__getitem__`,
`list.__getitem__`, `PyDict_FetchItem`/`GetItem`, `PyList_FetchItem`/`GetItem`, and the
`dict`/`list` iterators built on them all have a **lock-free fast path**. The PEP's
motivation is worth quoting because it is the coherence argument from doc 01, applied:

> *"Dictionaries hold top-level functions in modules and methods for classes. These
> dictionaries are inherently highly shared by many threads in multi-threaded programs.
> Contention on these locks in multi-threaded programs for loading methods and functions
> would inhibit efficient scaling in many basic programs."*

Every method call in every thread touches the class dict. Put a lock on it and you have
rebuilt the GIL with extra steps. So those reads are lock-free — which means they are
*exactly* the MP pattern from §6.2, and they need release stores on the writer and
acquire loads on the reader, or free-threaded CPython on ARM would read a published
pointer to an unpublished object. That is what the pointer-sized acquire/release surface
in §15.2 exists for, and it is why **the `dict` fast path is a memory-model artifact**,
not just a lock-elision trick.

Reclaiming the memory those lock-free readers might still be looking at is a separate,
harder problem, solved with **QSBR** in `Python/qsbr.c` (borrowed from FreeBSD, per
[`24-the-gil.md` §8.6](24-the-gil.md#86-the-design-is-almost-entirely-borrowed--and-thats-the-point)).
That is [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md)'s subject.

---

## 16. Python has no memory model

Here is the fact that ends most conversations: **the Python language specification does
not define a memory model.** There is no chapter of the language reference that says what
one thread is guaranteed to observe of another thread's writes. There is no
happens-before relation, no synchronizes-with edge, no data-race definition. C has one.
C++ has one. Java has a famously rigorous one. Go has a deliberately minimal one. Rust
inherits C++'s. Python has none.

For thirty years this did not matter, because the GIL *was* the memory model: one thread
runs bytecode at a time, so a bytecode-granular sequential interleaving was the whole
answer. Free-threading removed the mechanism without replacing the specification.

### 16.1 What free-threading guarantees instead

PEP 703 does not specify a memory model. It specifies **container thread-safety** — a
different, weaker, and much more operational promise:

> *"In CPython, the global interpreter lock protects against corruption of internal
> interpreter states when multiple threads concurrently access or modify Python objects.
> For example, if multiple threads concurrently modify the same list, the GIL ensures
> that the length of the list (`ob_size`) accurately matches the number of elements, and
> that the reference counts of each element accurately reflect the number of references
> to those elements. Without the GIL — and absent other changes — concurrent
> modifications would corrupt those fields and likely lead to program crashes."*
> — PEP 703, §Container Thread-Safety

So the guarantee is: **the interpreter's own invariants hold, even under racy Python-level
access.** Your program may compute nonsense; the runtime will not corrupt itself, segfault,
or hand you a `list` whose `ob_size` disagrees with its contents.

Sam Gross situated this explicitly, in an LWN comment responding to a reader who pointed
out that Java's and Go's guarantees are quite different from one another:

> *"Russ describes Go's approach to data races as a middle ground between Java's and
> C++'s approaches. The no-GIL CPython also occupies a middle ground, but it's a different
> one from Go. The motivation is similar: to make 'errant programs more reliable and
> easier to debug.' The no-GIL CPython needs some behavior that is **stronger** than
> Java's guarantees. For example, `dict` and `list` need to behave reasonably even with
> racy accesses. (In Java, racy accesses to `HashMap` can lead to infinite loops.) Other
> behavior is **weaker**. For example, CPython doesn't have the sandbox security of the
> JVM."*
> — Sam Gross (`colesbury`), [LWN comment, 15 Oct 2021](https://lwn.net/Articles/873070/)

That is the precise statement, and it is worth being able to reproduce it:

| | Java | C/C++/Rust | Go | **Free-threaded CPython** |
|---|---|---|---|---|
| Formal memory model | **yes**, rigorous | **yes**, rigorous | yes, deliberately minimal | **no** |
| Data race = UB? | no — constrained to "unpredictable but legal values" | **yes, whole-program UB** | no UB, but racy access to multi-word values may corrupt | **no** |
| Racy container access | may hang (`HashMap` infinite loop) | UB | may corrupt | **must not corrupt** |
| What you get instead | happens-before + final-field semantics | SC-DRF | happens-before, minimal | **operational container guarantees** |

The row that defines CPython's position is the third: **stronger than Java on containers,
weaker than Java overall, and with no formal model at all.** Sam Gross's "different middle
ground" is exactly that trade.

### 16.2 What this means for you, measured

The guarantee is real and it is worth seeing. Eight threads, 200,000 operations each,
both builds *(measured, 5 repetitions each)*:

**Test 1 — `obj.n += 1`, the classic lost-update race:**

| Build | Result across 5 reps | Lost |
|---|---|---|
| 3.14.6 (GIL) | 1,600,000 / 1,600,000 in **all five reps** | **0.00%** |
| 3.14.6t (free-threaded) | 457,066 / 404,517 / 421,825 / 414,671 / 402,572 | **71.4% – 74.8%** |

**Test 2 — `list.append` from 8 threads:**

| Build | `len(L)` | All elements valid |
|---|---|---|
| 3.14.6 (GIL) | exactly 1,600,000 | yes |
| 3.14.6t (free-threaded) | **exactly 1,600,000** | **yes** |

**Test 3 — `dict` under 8 concurrent writers + 2 concurrent readers** (readers doing
`len()`, `list(D)`, `D.get()` in a loop):

| Build | Corruption | Exception |
|---|---|---|
| 3.14.6 (GIL) | none | none |
| 3.14.6t (free-threaded) | **none** | **none** |

Read those three tables together, because together they *are* §16.1:

**Test 1 is the race condition, and free-threading exposes it brutally.** The GIL build
lost **zero** updates in five consecutive runs — the race is real, the bytecode is still
`LOAD/ADD/STORE`, but the interleaving window essentially never opened at this scale. The
free-threaded build lost **three quarters of all updates, every single run.** This is
[`24-the-gil.md` §6](24-the-gil.md#6-what-the-gil-does-and-does-not-guarantee)'s claim —
*"free-threading doesn't create new race conditions in Python code so much as it raises
the probability of ones you already had from 'once a month in prod' to 'immediately'"* —
measured, and the measurement is more violent than the prose suggests. Not "more likely."
**Reliably, reproducibly, 73% wrong.**

**Tests 2 and 3 are the PEP 703 guarantee holding.** Eight threads hammering a `list` with
no lock produced a list of exactly the right length with exactly the right contents. Eight
writers and two readers on a `dict` produced no exception and no corruption. That is not
luck; it is the per-object locks and the acquire/release-based lock-free read paths of
§15.4 doing their job.

**The distinction those tables teach is the one that matters in review:** free-threaded
Python protects the *runtime's* invariants, not *your* invariants. `list.append` is
atomic; `if x not in L: L.append(x)` is not, and never was. The runtime will not crash.
Your program will be wrong.

### 16.3 The gap is recognized, and there is a live proposal

This is not settled ground as of August 2026. Community discussion has characterized
Python's memory model as *"whatever CPython happened to have implemented at the time PEP
703 was written."* In November 2025, **Mark Shannon published a draft PEP 805, "Safe
Parallel Python"**, proposing runtime-enforced thread safety: additional per-object state
so the interpreter can check cheaply, at runtime, whether an operation is thread-safe and
**raise an exception when it is not** — objects must be explicitly made shareable, or
sharing is prohibited. LWN covered it in ["An explicit thread-safety proposal for
Python"](https://lwn.net/Articles/1043568/) (Daroc Alden, 3 Nov 2025).

> **Status, honestly.** As of my research for this document, **PEP 805 is a draft**
> (first published Sept 2025), had **no working prototype and therefore no measured
> performance impact**, and has not gone to the Steering Council. I could not confirm any
> status change between then and August 2026 — the canonical URL I found was still a
> readthedocs *preview* build rather than a published `peps.python.org` page, which is
> itself a signal that it has not advanced. **Treat it as "a serious proposal exists",
> not as "Python is getting this."** Verify before repeating.

**What to say in an interview.** "Python has no formal memory model. Under the GIL that
didn't matter — the GIL was the model. PEP 703 replaced it with an operational guarantee:
the interpreter's own invariants survive racy access, so `dict` and `list` can't be
corrupted, which is deliberately stronger than Java on containers. But there is no
happens-before relation you can reason with, which means for Python-level code the only
sound rule is 'use a lock', and for C-extension code you are in C's memory model with all
of C's undefined behaviour. Mark Shannon's draft PEP 805 is one proposal to close the
gap." That answer is rung 5 on README §14's ladder, because it names what is guaranteed,
what isn't, and where the model stops.

---

## 17. Lab exercises

Reading this document leaves you at rung 3 (README §14) — you can now *say* "store
buffer" and "acquire/release" fluently, which is exactly the trap. These labs move you
to rung 4. All were run on an M3 Pro; all need only `clang` and wall-clock timing.

**1 — Reproduce the MP litmus test, and get a negative result first.** Write the two-thread
harness: sense-reversing barrier, randomized `nop` delay, MP test, referee. Run it with
a *short* delay and observe zero violations. Then sweep the delay from 2 to 2048 nops and
find your machine's resonance. **Do not skip the negative result** — the point of this lab
is that you can "prove" AArch64 is sequentially consistent by measuring badly. *Proves you
can distinguish "did not observe" from "cannot happen", which is the single most
transferable skill in this document. (§6)*

**2 — Run it on x86 and settle my Rosetta caveat.** Same source, real Intel or AMD
hardware (a cloud VM is fine). Confirm SB is observable and MP is not. If you have both
x86 and Graviton, run both and put the table in your notes — that table is the most
persuasive artifact you can bring to a design review about ARM migration. *Proves §5's
architecture table with hardware I did not have. (§6.2)*

**3 — Measure the atomic cost model and find the collapse.** N threads incrementing (a)
private padded counters, (b) one shared atomic, (c) one shared atomic via a CAS loop, (d)
counters 8 bytes apart. Sweep N from 1 to your core count. **Predict the shape of all four
curves before you run it.** Then explain why (b) *loses total throughput* as you add
cores. *Proves §12, and it is the same experiment as
[doc 01 lab 4](01-memory-hierarchy-and-caches.md#11-lab-exercises) and
[`24-the-gil.md` lab 1](24-the-gil.md#10-lab-exercises) — do it once, use it three times.*

**4 — Read the assembly for all five orderings, both ISAs.** Write the twelve one-line
functions from §7.2, compile with `clang -O2 -S` for arm64 and `-arch x86_64`, and build
the table yourself. Then find the three cells where the two architectures disagree most
and explain each. *Proves you can answer "what does `release` cost?" with an instruction
instead of an adjective. (§7)*

**5 — Break `volatile` on purpose.** Take the MP test and implement the publisher with
`volatile` instead of atomics. Confirm from the assembly that no barrier is emitted, then
confirm from execution that the violation rate is the same as the fully-relaxed version.
Then compile the same source for x86-64 and observe that `volatile` "works" there.
*Proves §4, and inoculates you against the most persistent myth in the field.*

**6 — Force LL/SC and try to livelock it.** Compile with `-mcpu=cortex-a53` to defeat LSE,
confirm `ldxr`/`stxr` in the assembly, then run N threads doing CAS loops on one word and
look for a thread that starves. **I did not manage this and I have said so (§9.3) — if
you succeed, you have gone past this document.** Then re-enable LSE and compare throughput.
*Proves §9, and rewards you for not trusting a document that admits its gaps.*

**7 — Fix Dekker's algorithm.** Implement Dekker's or Peterson's mutual-exclusion
algorithm with plain `_Atomic` relaxed accesses. Instrument it to detect both threads in
the critical section simultaneously, and watch it fail. Add exactly one
`atomic_thread_fence(memory_order_seq_cst)` in the right place and watch it stop failing.
Then find the *minimum* ordering that works and justify it. *Proves §6.1 and §8, and it is
the classic exercise for a reason.*

**8 — Measure the free-threading race amplification.** Run the §16.2 tests on both builds
on your machine: `obj.n += 1`, `list.append`, and a `dict` under concurrent write+read.
Then write the one racy Python program that *does* produce visibly wrong output from
`list` — e.g. `if x not in L: L.append(x)` from 8 threads — and confirm the list is
internally consistent while the program's logic is wrong. *Proves §16.2's central
distinction: the runtime's invariants versus yours. Take this one to
[`26-free-threading.md`](26-free-threading.md).*

---

## 18. Question bank

Staff-level. If you can't answer from your own model, the section to reread is noted.

1. A store instruction retires. Name three things that are still not true about it. *(§1)*
2. Why does store-to-load forwarding exist, and why does it make single-threaded reasoning safe while breaking multi-threaded reasoning? *(§1)*
3. Which of the four elementary reorderings does x86-TSO permit, and why can no practical architecture forbid that one? *(§3)*
4. Your code is correct on x86-64 and corrupts on Graviton with no source change. Give the most likely litmus test, and the two-token fix. *(§6.2)*
5. `volatile` prevents the compiler from reordering. Why is that not enough on ARM, and why *is* it usually enough on x86? *(§4)*
6. What does `memory_order_release` compile to on x86-64? What does `memory_order_seq_cst` store compile to? Why is that difference the largest optimization available in x86 concurrent code? *(§7.2)*
7. Clang emits `ldapr` for an acquire load and `ldar` for a seq_cst load. What is the difference, and what does it cost you? *(§7.2)*
8. When would you use a fence instead of an ordered load or store? *(§8)*
9. Explain spurious failure of `compare_exchange_weak` in terms of hardware. On which targets does it actually occur? *(§10)*
10. You have a CAS loop and a `fetch_add` available for the same operation. Give the asymptotic argument for `fetch_add`, and then the progress-guarantee argument. *(§11)*
11. An uncontended atomic RMW costs ~2 ns on this machine. A contended one costs ~19 ns. What changed, and what is the *only* class of fix? *(§12.3)*
12. Eight cores incrementing one shared atomic have *less* aggregate throughput than one core. Explain the mechanism, and name the CPython project that discovered this the expensive way. *(§12.2, [`24-the-gil.md` §7](24-the-gil.md#7-the-gilectomy-larry-hastings-seven-core-lesson))*
13. What happens to a misaligned atomic on x86-64? On AArch64? Why is the AArch64 behaviour better engineering? *(§13)*
14. Why is a data race undefined behaviour rather than "you get a stale value"? Give a concrete optimization that exploits it. *(§14.2)*
15. State SC-DRF as a two-sided contract. Which side do you break when you write `memory_order_relaxed`? *(§14.3)*
16. CPython's `Py_INCREF` fast path uses relaxed atomics that compile to a plain `ldr`/`str`. If the instructions are identical to a non-atomic access, what did the atomic buy? *(§15.3, §14.2)*
17. Why does `Include/cpython/pyatomic.h` have no relaxed variant of `_Py_atomic_add_ssize`? *(§15.2)*
18. Does Python have a memory model? What does free-threaded CPython guarantee instead, and how does it differ from Java's guarantees in *both* directions? *(§16)*
19. Eight threads do `list.append` with no lock. Is the list valid afterwards? Eight threads do `obj.n += 1`. Is the count right? Justify both from §16.1's guarantee, not from bytecode. *(§16.2)*
20. Your free-threaded build loses 73% of increments where the GIL build lost 0%. Did free-threading introduce a bug? *(§16.2)*

---

## 19. Sources

**Primary — the hardware view**
- **Paul McKenney, [*Memory Barriers: a Hardware View for Software Hackers*](https://www.puppetlabs.com/)** — the source of §1 and §2. Store buffers and invalidate queues explained by someone who had to make Linux correct on a dozen architectures. **Verdict: read it once, completely, before anything else here.** Also in expanded form as Appendix C of [*Is Parallel Programming Hard, And, If So, What Can You Do About It?*](https://mirrors.edge.kernel.org/pub/linux/kernel/people/paulmck/perfbook/perfbook.html) 🆓, which is the best free book on this topic that exists.
- **Adve & Gharachorloo, [*Shared Memory Consistency Models: A Tutorial*](https://www.hpl.hp.com/techreports/Compaq-DEC/WRL-95-7.pdf)** (1995) 🆓 — the paper that made "memory consistency model" a term working engineers use. Predates C11 and is better for it: it reasons about hardware models directly. **Verdict: the single best 25 pages on why SC is not free.**
- **ARM Architecture Reference Manual, ARMv8-A**, §B2 (The AArch64 Application Level Memory Model) — the normative source for `ldar`/`ldapr`/`stlr`, `ldxr`/`stxr`, `dmb` variants, and other-multi-copy-atomicity. **Verdict: reference, not reading. Consult it for a specific instruction; do not attempt it linearly.**
- **Sewell, Sarkar, Owens et al., [*x86-TSO: A Rigorous and Usable Programmer's Model for x86 Multiprocessors*](https://www.cl.cam.ac.uk/~pes20/weakmemory/cacm.pdf)** (CACM 2010) 🆓 — where §5.1's model comes from, including the history of Intel's own documentation being ambiguous. **Verdict: read the first four pages; the formalism after that is optional.**
- **[`herd7` / `litmus7`](https://diy.inria.fr/)** (Alglave, Maranget et al.) — the tools that turn §6 from an experiment into a decision procedure. **Verdict: use these instead of stress tests once you are past this document.**

**Primary — the language view**
- **Boehm & Adve, [*Foundations of the C++ Concurrency Memory Model*](https://www.hpl.hp.com/techreports/2008/HPL-2008-56.pdf)** (PLDI 2008) 🆓 — the design rationale for §7 and §14, including *why* data races are UB rather than merely unspecified. **Verdict: the answer to "why did they make it so complicated?"**
- **Hans Boehm, [*Threads Cannot Be Implemented As a Library*](https://www.hpl.hp.com/techreports/2004/HPL-2004-209.pdf)** (2005) 🆓 — the paper that forced C and C++ to specify a memory model at all. **Verdict: short, devastating, and the historical key to §14.**
- **[cppreference — `std::memory_order`](https://en.cppreference.com/w/cpp/atomic/memory_order)** — the normative-adjacent reference for §7.1. Note its own warning about `memory_order_consume`. **Verdict: the page to keep open while writing atomics.**

**Preshing — the best free explanations anywhere**
- [Weak vs. Strong Memory Models](https://preshing.com/20120930/weak-vs-strong-memory-models/) — §3 and §5 in blog form. **Verdict: if §5 didn't land, read this instead and come back.**
- [Acquire and Release Semantics](https://preshing.com/20120913/acquire-and-release-semantics/) — the source of the one-way-barrier picture in §7.1.
- [Memory Reordering Caught in the Act](https://preshing.com/20120515/memory-reordering-caught-in-the-act/) — the SB experiment and, crucially, the randomized-delay methodology that §6.3 rediscovered the hard way. **Verdict: read this BEFORE attempting lab 1 and save yourself my two wasted hours.**
- [An Introduction to Lock-Free Programming](https://preshing.com/20120612/an-introduction-to-lock-free-programming/) — the bridge to [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md).

**Books**
- **Herlihy, Shavit, Luchangco & Spear, *The Art of Multiprocessor Programming*, 2e** (2020) — ch. 3 (concurrent objects, linearizability) and ch. 7 (spin locks, contention, backoff). **Verdict: the theory §11's progress guarantees come from. Buy it if you intend to write lock-free code; skip it if you intend to use locks.**
- **Sorin, Hill & Wood, *A Primer on Memory Consistency and Cache Coherence*, 2e** (2020) — the rigorous joint treatment of coherence (doc 01 §5) and consistency (this document). **Verdict: the book that makes clear these are two different problems, which is the confusion this document exists to prevent.**
- **Anthony Williams, *C++ Concurrency in Action*, 2e** — ch. 5 is the most practical prose on the C11/C++11 model. **Verdict: best applied treatment of §7 even if you don't write C++.**

**CPython — verify against these, not against this document**
- [`Include/cpython/pyatomic.h`](https://github.com/python/cpython/blob/main/Include/cpython/pyatomic.h) and its three backends `pyatomic_gcc.h`, `pyatomic_msc.h`, `pyatomic_std.h` — §15's source. **You already have these**: they ship in any CPython install's `include/` directory. **Verdict: read the top comment block in full; it states the design philosophy in ten lines.**
- [`Include/refcount.h`](https://github.com/python/cpython/blob/main/Include/refcount.h) — §15.3's `Py_INCREF`. Read the `Py_GIL_DISABLED` branch alongside PEP 703's pseudocode and note where they differ.
- [`Python/qsbr.c`](https://github.com/python/cpython/blob/main/Python/qsbr.c) — the reclamation scheme that makes §15.4's lock-free reads safe. Deferred to [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md).
- [PEP 703](https://peps.python.org/pep-0703/) — read §Reference Counting (the "relaxed atomics" note quoted in §15.3), §Container Thread-Safety, and §Optimistically Avoiding Locking. **Verdict: the three sections that make this document's Python half make sense.**

**The memory-model gap**
- [Sam Gross on Java, Go and CPython's "different middle ground"](https://lwn.net/Articles/873070/) — LWN comment, 15 Oct 2021. §16.1's quote. **Verdict: the single most precise statement of what free-threaded Python guarantees, and it is a comment on a news site rather than a specification. That is itself the point.**
- [Russ Cox, *Updating the Go Memory Model*](https://research.swtch.com/gomm) — the "middle ground" framing Gross is referencing, and the best argument anywhere for *saying less* in a memory model. **Verdict: read it for the design philosophy even though it's about Go.**
- [LWN — An explicit thread-safety proposal for Python](https://lwn.net/Articles/1043568/) (Daroc Alden, 3 Nov 2025) — coverage of Mark Shannon's draft PEP 805. **Verdict: the current state of the argument; check for movement before quoting §16.3.**
- [Thread safety now and in the future (no-gil)](https://discuss.python.org/t/thread-safety-now-and-in-the-future-no-gil/104297) — discuss.python.org, Oct 2025. Where the "whatever CPython happened to have implemented" characterization appears.

**Sibling docs**
- [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §5–§6 — MESI and false sharing. This document is its §5 continued; §12.2 is its lab 3 with atomics.
- [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md) — ABA (teased in §9.3), tagged pointers (priced in §13), hazard pointers, epochs, QSBR.
- [`24-the-gil.md`](24-the-gil.md) §1, §7, §9 — the coherence origin of the GIL, and the Gilectomy's atomic-refcount failure, which §12.2 reproduces.
- [`26-free-threading.md`](26-free-threading.md) — where §15 and §16 get applied to migration decisions.
- [`30-concurrency-correctness.md`](30-concurrency-correctness.md) — data race vs race condition (§14.1), and the testing tools that §6.3 argues you need.
- [`31-measurement-methodology.md`](31-measurement-methodology.md) — the discipline §6.3 and §12.4 are pleading for.

---

*Next: [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md) — you can now
make a single word change atomically and in order. The remaining problem is freeing the
memory a lock-free reader might still be looking at, and that turns out to be harder than
everything in this document.*
