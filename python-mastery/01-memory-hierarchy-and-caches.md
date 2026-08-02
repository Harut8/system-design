# 01 — The memory hierarchy: cache lines, coherence, and why Python is slow

> **Tier 0, doc 01.** Prerequisites: [`00-cpu-execution-model.md`](00-cpu-execution-model.md) (out-of-order execution,
> why a stall isn't always a stall). Feeds directly into: `02-atomics-and-memory-models.md`,
> `03-lockfree-and-reclamation.md`, `16-object-memory-layout.md`,
> [`24-the-gil.md`](24-the-gil.md), `33-optimizing-python.md`, `35-memory-optimization.md`.
>
> **The thesis of this document:** on a modern machine, the CPU is not the scarce
> resource. *Memory locality is.* A core can retire several instructions per cycle and
> will happily sit idle for 300+ cycles waiting for a pointer dereference. Python's
> object model — where every value is a heap-allocated `PyObject*` reached by chasing a
> pointer — is a machine for generating those stalls. **"Python is slow" is, to a first
> approximation, "Python has terrible cache locality."** Everything in Tier 5 depends on
> your understanding this physically rather than as a slogan.

## Contents

1. [The numbers you must internalize](#1-the-numbers-you-must-internalize)
2. [Your machine, concretely](#2-your-machine-concretely)
3. [The cache line is the quantum of memory traffic](#3-the-cache-line-is-the-quantum-of-memory-traffic)
4. [Associativity, indexing, and the power-of-two trap](#4-associativity-indexing-and-the-power-of-two-trap)
5. [MESI: how coherence actually works](#5-mesi-how-coherence-actually-works)
6. [False sharing — and the 128-byte problem](#6-false-sharing--and-the-128-byte-problem)
7. [Prefetchers: what they can and cannot see](#7-prefetchers-what-they-can-and-cannot-see)
8. [The TLB and page size](#8-the-tlb-and-page-size)
9. [NUMA, briefly](#9-numa-briefly)
10. [What all of this means for CPython](#10-what-all-of-this-means-for-cpython)
11. [Lab exercises](#11-lab-exercises)
12. [Question bank](#12-question-bank)
13. [Sources](#13-sources)

---

## 1. The numbers you must internalize

Not exact values — **orders of magnitude and ratios**. If you know these, you can predict
performance. If you don't, you are guessing.

| Operation | Cycles | ~Time @ 4 GHz | Relative |
|---|---|---|---|
| Register access | 0–1 | ~0.25 ns | 1× |
| L1 data cache hit | **3–5** | ~1 ns | ~4× |
| L2 cache hit | **12–20** | ~4 ns | ~15× |
| L3 / SLC hit | **30–60** | ~12 ns | ~50× |
| **Main memory (DRAM)** | **200–400** | **~80–100 ns** | **~300×** |
| Cache line from another core's L1 (dirty) | 40–100+ | ~20 ns | ~70× |
| Branch misprediction | 15–20 | ~5 ns | ~18× |
| Atomic RMW, uncontended | 20–50 *(x86)* · **~8 (this machine)** | ~10 ns · **1.95 ns** | ~40× · **~8×** |
| Atomic RMW, contended across cores | 100–500+ | 25–125 ns | ~400× |
| TLB miss (page walk, cached) | 10–30 | ~5 ns | ~20× |
| Context switch (direct + indirect) | ~10,000+ | 3–50 µs | ~10,000× |
| SSD read | — | ~50–100 µs | ~300,000× |

> **Correction, measured.** The 20–50 cycle figure for an uncontended atomic RMW is the
> *x86* number, and it is what this table originally claimed outright. Measured on this
> M3 Pro it is **~1.95 ns ≈ 8 cycles** — because AArch64's LSE extension provides true
> single-instruction atomics (`ldadd`, `casal`) rather than x86's `lock`-prefixed
> read-modify-write. Uncontended atomics are roughly **4–6× cheaper here than the
> textbook figure**. The *contended* row is unaffected — contention is a coherence
> problem (§5), not an instruction-cost problem, and that is where the real penalty
> lives. See [`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md) §6.

**The one ratio that matters: L1 hit vs DRAM is roughly 1:100.** A loop that hits cache
runs ~100× faster than the identical loop that misses. No compiler optimization, no
language choice, no algorithmic micro-tweak moves the needle like that. This is why
`33-optimizing-python.md` puts *data layout* above *code cleverness*.

### 1.1 Latency is not throughput — the number nobody quotes

Every table above lists **latency**, and latency alone will mislead you. A core does not
stop at one outstanding miss: it has ~10–20 miss-status registers (MSHRs / line-fill
buffers) and can have that many cache misses **in flight simultaneously**. That capacity
is called **memory-level parallelism (MLP)**, and it is the difference between a slow
program and a stopped one.

Measured on this M3 Pro, 256 MB working set, same array, same number of accesses, C
compiled `-O2` *(measured)*:

| Access pattern | ns/access | vs. chase |
|---|---|---|
| Pointer chase — each address depends on the previous load | **122.2** | 1× |
| Random, but addresses known in advance (`a[idx[i]]`) | **5.0** | **24× faster** |
| Sequential | **0.3** | **400× faster** |

Read the middle row twice. **Both of the first two rows miss cache on essentially every
access, at the same addresses.** The only difference is whether the hardware is allowed to
have twenty of those misses outstanding at once. 122 ns is roughly one DRAM latency per
access, fully exposed; 5 ns is ~20 misses overlapping. Same misses, 24× the throughput.

This is the single most useful correction to the naive model, and it has three
consequences you will use constantly:

- **"Random access is slow" is imprecise.** *Dependent* access is slow. Independent
  random access is 20× better than the latency table predicts.
- **Pointer chasing is the worst case not because it misses, but because it serializes**
  the misses. §7 develops this; §10 is what it does to CPython.
- **Your benchmark can be measuring either one.** Lab 1 and lab 2 in §11 differ *only* in
  this, which is why they will report numbers a factor of ~20 apart on the same data.

Real measured latencies on Apple M1 Firestorm (7-cpu.com, 16 KB page mode) — note how
the curve steps as you exceed each level:

| Working set | Latency |
|---|---|
| 128 KB | 3 cycles *(L1d)* |
| 256 KB | 11 cycles *(fell out of L1 → L2)* |
| 1 MB | 16 cycles |
| 4 MB | 20 cycles *(+6 — L1 TLB miss appears)* |
| 16 MB | 24 cycles + 9 ns |
| 32 MB | 24 cycles + 44 ns *(+91 ns — DRAM)* |
| 64 MB | 32 cycles + 66 ns *(+26 — L2 TLB miss)* |
| 1 GB | 55 cycles + 90 ns *(+24 — page-directory-cache miss)* |

Read that table carefully. **There are two independent curves happening**: the data cache
hierarchy *and* the TLB hierarchy (§8). At 4 MB you start missing the L1 TLB; at 64 MB
you start missing the L2 TLB. Those costs are additive with the data misses, and people
who only think about "cache size" are surprised by them every time.

---

## 2. Your machine, concretely

Everything below is measurable on the machine this repo lives on. Ground truth:

```console
$ sysctl -a | grep -E 'hw.(cachelinesize|pagesize|perflevel)'
machdep.cpu.brand_string: Apple M3 Pro
hw.cachelinesize:              128        ← NOT 64
hw.pagesize:                   16384      ← NOT 4096
hw.perflevel0.name:            Performance
hw.perflevel0.physicalcpu:     5
hw.perflevel0.l1dcachesize:    131072     ← 128 KB
hw.perflevel0.l1icachesize:    196608     ← 192 KB
hw.perflevel0.l2cachesize:     16777216   ← 16 MB, shared by all 5 P-cores
hw.perflevel0.cpusperl2:       5
hw.perflevel1.name:            Efficiency
hw.perflevel1.physicalcpu:     6
hw.perflevel1.l1dcachesize:    65536      ← 64 KB
hw.perflevel1.l2cachesize:     4194304    ← 4 MB, shared by all 6 E-cores
hw.perflevel1.cpusperl2:       6
```

```
                    Apple M3 Pro (5P + 6E)

   ┌─── PERFORMANCE CLUSTER ────────┐   ┌─── EFFICIENCY CLUSTER ─────────┐
   │  P0    P1    P2    P3    P4    │   │  E0   E1   E2   E3   E4   E5   │
   │ ┌──┐ ┌──┐ ┌──┐ ┌──┐ ┌──┐       │   │ ┌──┐ ┌──┐ ┌──┐ ┌──┐ ┌──┐ ┌──┐ │
   │ │L1│ │L1│ │L1│ │L1│ │L1│ 128KB │   │ │L1│ │L1│ │L1│ │L1│ │L1│ │L1│ │
   │ └┬─┘ └┬─┘ └┬─┘ └┬─┘ └┬─┘  each │   │ └┬─┘ └┬─┘ └┬─┘ └┬─┘ └┬─┘ └┬─┘ │
   │  └────┴────┼────┴────┘         │   │  └────┴────┼────┴────┴────┘   │
   │       ┌────┴─────┐             │   │      ┌─────┴────┐             │
   │       │ L2 16 MB │             │   │      │ L2  4 MB │             │
   └───────┴────┬─────┴─────────────┘   └──────┴────┬─────┴─────────────┘
                └──────────────┬────────────────────┘
                      ┌────────┴─────────┐
                      │ SLC (~12 MB)     │   ← system level cache, on the
                      │ shared: CPU/GPU/ │     memory controller. Not exposed
                      │ Neural Engine    │     via sysctl.
                      └────────┬─────────┘
                      ┌────────┴─────────┐
                      │  LPDDR5  18 GB   │   unified — GPU sees the same memory
                      └──────────────────┘
```

**Four things about this machine that will break textbook assumptions:**

1. **128-byte cache lines, not 64.** Padding tuned for x86 (64 B) does *not* prevent
   false sharing here. See §6 — this is the single most practical difference.
2. **16 KB pages, not 4 KB.** Each TLB entry covers 4× more memory, so TLB pressure is
   genuinely lower than on x86 Linux. Also: `mmap` granularity and RSS accounting are in
   16 KB units, which matters in `07-virtual-memory.md`.
3. **No conventional L3.** There's a *System Level Cache* shared with the GPU and Neural
   Engine, hanging off the memory controller. It behaves differently from an inclusive
   x86 L3 and isn't reported by `sysctl`.
4. **Heterogeneous cores.** P-cores and E-cores have *different cache sizes*. A thread
   migrated from a P-core to an E-core sees its L1 halve and its L2 shrink 4×.
   **This makes your laptop a hostile benchmarking environment** — see §11 and
   `31-measurement-methodology.md`. Any benchmark you run here without pinning or
   QoS control has cluster-migration noise baked in.

> **Tooling note.** There is no `perf` on macOS. For PMU counters you have `xctrace`
> / Instruments (limited on Apple Silicon), or a Linux box. Most labs below are written
> to need only wall-clock timing, which is enough to see every effect in this document.

---

## 3. The cache line is the quantum of memory traffic

**Caches do not store bytes. They store lines.** Reading one byte transfers an entire
line — 64 B on x86, 128 B on your M3 Pro — from memory into L1.

Three consequences follow, and they explain most of what people find mysterious about
performance:

### Consequence 1: sequential access is nearly free

```
Reading a 128-byte-aligned int64 array, one element at a time:

  idx:   0   1   2   3   4   5  ...  15 | 16  17 ...
        ┌───────────────────────────────┬────────────
line 0  │ MISS  hit hit hit ... hit hit │ line 1: MISS ...
        └───────────────────────────────┴────────────
        ^ one 300-cycle stall           ^ next stall
          amortized over 16 elements      (and the prefetcher
                                           probably hid it — §7)
```

One miss per 16 elements, and the prefetcher usually eliminates even that. Effective
cost: **near zero**.

### Consequence 2: random access to pointers is catastrophic

```
Reading a Python list of ints, one element at a time:

  lst ──▶ [ptr][ptr][ptr][ptr]...     ← the pointer array IS sequential
            │    │    │    │
            ▼    ▼    ▼    ▼
          obj  obj  obj  obj          ← the objects are scattered
          MISS MISS MISS MISS            across the heap

  = one cache miss PER ELEMENT, and the prefetcher cannot help
    because it can't know the addresses until it loads the pointers.
```

This is **pointer chasing**, and it is the defining performance characteristic of
CPython. §10 develops it fully.

### Consequence 3: you pay for what you don't use

Load one `int64` from a struct and you've pulled 128 bytes into L1 — 120 of which you may
never touch. If your hot loop reads one field from an array of large structs, you're
using **6% of your memory bandwidth** on useful data. This is the argument for
struct-of-arrays over array-of-structs, and it's why NumPy's columnar layout is fast for
reasons that have nothing to do with C vs Python.

### Consequence 4: a write is also a read

Storing to a line you don't own is not free and is not "half a miss". The core must first
obtain the line — **read-for-ownership**: fetch it from memory or another core, take it to
**M** state (§5), *then* apply your store. Writing 8 bytes to a cold location costs a full
line fetch, and the dirty line must later be written back.

Two practical corollaries:

- **Initializing a large buffer costs bandwidth in both directions** unless the platform
  uses non-temporal / streaming stores that bypass the cache (what a good `memset` or
  `np.zeros` on a large array does). This is why "just allocate and fill it" is not free.
- **Refcounting is a write.** Reading a Python object takes its line to **M** state on
  your core. Hold that thought until §10.2 — it is the whole of the GIL argument.

### Alignment matters at the boundary

An 8-byte value at offset 124 in a 128-byte line **straddles two lines** — two lookups,
possibly two misses, and on some microarchitectures a serious penalty for atomics
(an atomic RMW spanning two lines can be catastrophically slow or unsupported). Malloc
implementations align to 16 bytes for this reason; caring about it yourself matters when
you control layout (`__slots__`, `struct`, C extensions).

---

## 4. Associativity, indexing, and the power-of-two trap

A cache can't check every line for every address — too slow. Instead the address is
split:

```
   64-bit virtual address
  ┌──────────────┬──────────┬────────────┐
  │     TAG      │   SET    │   OFFSET   │
  └──────────────┴──────────┴────────────┘
                              ^ which byte within the line
                                (7 bits for a 128 B line)
                   ^ which set to look in
   ^ compared against the tags of the N lines in that set
```

An **N-way set-associative** cache has N candidate slots per set. A line's address
determines its set; only N lines with the same set index can be resident simultaneously.

**The trap:** if your access stride is a large power of two, every access maps to the
*same set*, and you thrash an N-way cache with only N+1 live values — while 99% of the
cache sits empty.

```python
# The classic demonstration: a matrix whose row stride is a power of two.
# Walking a column touches one element per row — all mapping to the same set.
# NOTE: it must be a *contiguous typed* buffer. A Python list-of-lists would
# measure pointer chasing (§3, consequence 2), which swamps the effect.

import numpy as np

a = np.zeros((1024, 1024))   # row stride 8192 B — a power of two
a[:, 0].sum()                # every row's element 0: same set. Conflict-miss storm.

b = np.zeros((1024, 1025))   # row stride 8200 B — sets rotate
b[:, 0].sum()                # spreads across sets. Often 2–5× faster,
                             # despite touching a *larger* array.
```

This is why FFT and matrix libraries deliberately pad array dimensions to *avoid* powers
of two, and why a benchmark can get faster when you make the array *bigger*. If you ever
see that and think your measurement is broken — it isn't, this is why.

The Python-level lesson is the comment, not the code: **this effect is invisible from
pure Python, because pure Python has a bigger problem.** You only get to care about
conflict misses once your data is contiguous and typed — which is exactly the point at
which NumPy/`array`/Arrow users start caring about them, and nobody else does.

**The three misses, named** (Hill's taxonomy — know these terms, they're how you
communicate a diagnosis):

| Type | Cause | Fix |
|---|---|---|
| **Compulsory** | First-ever touch of the line | Prefetching; nothing else |
| **Capacity** | Working set exceeds cache size | Blocking/tiling; shrink the data |
| **Conflict** | Too many live lines map to one set | Change stride/padding |

---

## 5. MESI: how coherence actually works

Multiple cores each have a private L1. If two cores cache the same address and one
writes, the other must not keep reading stale data. The hardware guarantees this — for a
price, and that price is the subject of [`24-the-gil.md`](24-the-gil.md).

Each cache line, in each core's cache, is in one of four states:

| State | Meaning | Can read? | Can write? |
|---|---|---|---|
| **M**odified | I have the only copy, and it's dirty | yes | **yes, free** |
| **E**xclusive | I have the only copy, and it's clean | yes | yes → becomes M |
| **S**hared | Others may have copies too | yes | **no — must upgrade first** |
| **I**nvalid | My copy is stale/absent | no | no |

### The critical asymmetry

```
  READS are cheap and scale:
    Core0: S    Core1: S    Core2: S    Core3: S
    All four read concurrently at full L1 speed, forever.
    Zero interconnect traffic. Perfect scaling.

  WRITES serialize:
    Core0 wants to write a line in state S
      → broadcasts Request-For-Ownership
      → Core1, Core2, Core3 must Invalidate their copies
      → Core0 transitions S → M
      → the other three now MISS on their next read

    Now Core1 wants to write:
      → the whole dance runs again, in reverse.
```

**One writer among N readers destroys the scaling of all N.** The line ping-pongs
between cores, each transfer costing an interconnect round trip. And it gets worse across
NUMA nodes or Apple's cluster boundaries.

This is the single most important mechanism in Tier 0. Read the §1 row again:
"cache line from another core's L1 (dirty), 40–100+ cycles". That is the tax, per access,
paid by *every* core involved.

> **Real hardware note.** MESI is the teaching model. Real implementations add states
> (MESIF on Intel, MOESI on AMD) to let a dirty line be forwarded core-to-core without a
> memory writeback. The *asymmetry* — reads share, writes serialize — is universal, and
> that's the part you reason with.

### Store buffers: where the memory model comes from

A core doesn't stall waiting for a write to become globally visible. It drops the store
into a **store buffer** and continues. That buffer is why:

- Your writes become visible to other cores *later* than they execute.
- You can read your own write before anyone else sees it (store-to-load forwarding).
- **Memory barriers must exist** — a barrier is, mechanically, "drain the store buffer
  before proceeding."
- x86 (TSO) has a strong model that mostly hides this; **ARM — your M3 Pro — is weakly
  ordered and does not.** Code that is accidentally correct on x86 can break here.

That last point is `02-atomics-and-memory-models.md` in one sentence, and it's why doc 02
immediately follows this one.

---

## 6. False sharing — and the 128-byte problem

**False sharing:** two threads write to *different variables* that happen to occupy the
*same cache line*. Logically independent; physically in a knife fight.

```
  A 128-byte cache line on your M3 Pro:
  ┌────────────────────────────────────────────────────────────────┐
  │ counter_a (8B) │ counter_b (8B) │ .......... unused .......... │
  └────────────────────────────────────────────────────────────────┘
      ▲                  ▲
      │                  │
   Thread 0           Thread 1
   writes only        writes only
   counter_a          counter_b

  No shared variable. No race. No lock needed.
  And throughput collapses by 10–100×, because the LINE ping-pongs.
```

The fix is padding to cache-line size — and here is where your machine bites:

```c
// Correct on x86-64. WRONG on Apple Silicon.
struct counter { _Alignas(64) uint64_t value; };

// Correct on both.
struct counter { _Alignas(128) uint64_t value; };
```

**`hw.cachelinesize` on your M3 Pro is 128.** A struct padded to 64 bytes puts *two*
counters in one line and false-shares exactly as if you hadn't padded at all. Cross-
platform code should query at runtime or pad to 128 unconditionally — the wasted memory
is trivial next to the coherence cost.

> **An honest complication, because this is the kind of detail that separates rungs 3
> and 5.** "The cache line size" is not one number. On *this one machine*, three
> authorities disagree:
>
> | Authority | Says | *(measured)* |
> |---|---|---|
> | `sysctl hw.cachelinesize` | **128** | the OS's answer |
> | 7-cpu.com, M1 Firestorm L1d | **64** | an empirical answer |
> | `std::hardware_destructive_interference_size`, clang → arm64 | **256** | the compiler's answer |
> | …the same compiler, `-target x86_64` | **64** | |
>
> Those are real outputs — compile a two-line program printing
> `__GCC_DESTRUCTIVE_SIZE` for both targets and you get 256 and 64. (So the widely
> repeated claim that the constant is 128 on x86 is **false** for clang/libstdc++; 128 is
> folly's convention, chosen because Intel's spatial prefetcher has fetched lines in
> adjacent pairs since Sandy Bridge.) The L1 line size, the L2 line size, the **coherence
> granule**, and the **prefetch granule** can all differ, and Daniel Lemire has noted that
> empirical false-sharing experiments on M1 don't cleanly confirm 128 either.
>
> **The practical rule: pad to 128, and measure.** Not because 128 is the truth — nothing
> here is — but because it is the cheapest number that is wrong in the safe direction.
> Lab 3 in §11 has you determine the effective granule on your own hardware rather than
> trusting any published number, including all four in that table.

### True sharing is the same physics, and you cannot pad it away

If threads genuinely write the same variable — a shared counter, **a Python reference
count** — no layout change helps. The only fixes are architectural: don't share, shard
per thread and combine, or make the object immortal so nobody writes at all.

That last option is exactly what PEP 703 does for `None`, `True`, `False`, small ints and
interned strings. Immortalization means those lines sit in **S** state on every core
forever — never invalidated, never ping-ponged. See [`24-the-gil.md` §8.1](24-the-gil.md#81-immortalization--the-dont-count-at-all-tier).
It is a coherence optimization dressed up as a refcounting optimization.

---

## 7. Prefetchers: what they can and cannot see

Modern cores speculatively load lines before you ask. Understanding what the prefetcher
can *see* tells you which access patterns are fast.

| Pattern | Prefetchable? | Why |
|---|---|---|
| `a[i]`, i ascending | ✅ trivially | constant stride +1 |
| `a[i]`, i descending | ✅ | constant stride −1 |
| `a[i * 16]` | ✅ usually | constant stride, within detectable range |
| Two/three interleaved sequential streams | ✅ | trackers handle several streams |
| `a[b[i]]` (gather / indirect) | ❌ | address unknown until `b[i]` loads |
| Linked list / tree traversal | ❌ | address unknown until the node loads |
| **Python object graph traversal** | ❌ | **it's pointer chasing all the way down** |
| Hash table probe | ❌ | address is pseudorandom by design |
| Across a page boundary | ⚠️ often stops | prefetchers usually don't cross pages |

**Pointer chasing defeats prefetching by construction.** The address of the next load is
*the result of* the current load — the dependency chain is serial, and out-of-order
execution cannot hide it because there is nothing independent to run. Every hop is a full
memory latency, exposed.

```
Sequential array scan:      Pointer chase (linked list, Python objects):

  load a[0] ─┐                load node    ──▶ 300 cycles ──┐
  load a[1] ─┤ all in                                        ▼
  load a[2] ─┤ flight                       load node.next ──▶ 300 cycles ──┐
  load a[3] ─┘ at once                                                       ▼
                                                          load node.next ──▶ ...
  → memory-level parallelism
    hides the latency          → strictly serial. Latency is fully exposed.
                                 The core is idle ~99% of the time.
```

That right-hand diagram is a Python `for` loop over a list of objects. Hold onto it —
it's the whole of §10.

But note the precise claim, given §1.1: the chase is slow because the loads are
**dependent**, not because they are random. The middle row of that table — random,
independent, 5 ns — is what "unprefetchable" costs when the addresses are merely unknown
to the prefetcher rather than unknown to the *core*. Out-of-order execution rescues the
second case completely and the first not at all.

### The exception on your exact machine: Apple's DMP

Apple Silicon (M1 onwards) ships a **data memory-dependent prefetcher**: it inspects loaded
values, and if one *looks like a pointer* into a mapped region, it speculatively
dereferences it. That is precisely the case §7's table calls unprefetchable — so on this
hardware, "pointer chasing defeats prefetching by construction" is an overstatement.

Two reasons to know this:

1. **Your pointer-chase benchmark may be partly defeated by it**, especially if your nodes
   are laid out in an order the DMP can exploit. Sattolo-shuffled chases through a large
   region mostly evade it; neat, freshly-allocated linked lists may not.
2. **It was a security hole.** The GoFetch attack (2024) used the DMP to leak constant-time
   cryptographic secrets, because "this value looks like a pointer, let me fetch it" is a
   data-dependent memory access by definition. A microarchitectural optimization aimed at
   exactly the workload in this section turned into a side channel — a good reminder that
   the hierarchy is not a neutral substrate.

### What the *instruction* side sees

Everything above is about data. The core is fetching instructions through the same kind of
hierarchy, and your `sysctl` dump in §2 has the number: **`l1icachesize: 196608`** — a
192 KB L1i, larger than the 128 KB L1d, which should tell you Apple expects instruction
footprint to matter.

Three structures on the instruction side behave analogously to §3–§7:

| Structure | Analogue of | Fails when |
|---|---|---|
| L1i + iTLB | L1d + dTLB | hot code footprint exceeds it — big interpreters, unrolled/inlined megamorphic code |
| Branch predictor | prefetcher | branch direction is data-dependent and unbiased |
| **Branch target buffer** (indirect branches) | prefetcher, but for *where next* | one indirect jump serves many targets |

That last row is the one that decides interpreter performance, and §10.6 is about it.

---

## 8. The TLB and page size

Virtual addresses must be translated to physical ones. The page table lives in memory, so
translation is cached in the **TLB** (Translation Lookaside Buffer). A TLB miss triggers
a **page walk** — up to four dependent memory accesses, each potentially a cache miss.

Measured on M1 Firestorm in 16 KB page mode:

| Structure | Capacity | Miss penalty |
|---|---|---|
| L1 data TLB | 160 entries | 6 cycles |
| L2 data TLB | 3,072 entries | 26 cycles |
| Page-directory cache | covers 768 MB | ~24 cycles |

**TLB reach** = entries × page size. This is the number that matters:

| Config | L1 TLB reach | L2 TLB reach |
|---|---|---|
| x86, 4 KB pages, 64 L1 entries | 256 KB | ~6 MB (1536 entries) |
| **Apple, 16 KB pages, 160 entries** | **2.5 MB** | **48 MB** |
| x86, 2 MB huge pages | 100+ MB | GBs |

Apple's 16 KB page size buys 4× the reach per entry for free — a real, structural
advantage for large working sets, and one reason Apple Silicon does well on
pointer-heavy workloads despite the pointer chasing.

**Why you care in Python:** a large `dict`, a big object graph, or a multi-GB NumPy array
can exceed TLB reach while still "fitting in cache" by size. You then pay translation
misses on top of data misses. The symptom is a workload that slows down more than its
size increase predicts — the 7-cpu table in §1 shows exactly this at 4 MB and 64 MB.

On Linux, this is what transparent huge pages (THP) address, and why some allocators let
you back the heap with 2 MB pages. Covered in `07-virtual-memory.md`.

---

## 9. NUMA, briefly

On multi-socket servers (not your laptop, but every big machine you'll deploy to),
memory is attached to specific sockets. Accessing another socket's memory crosses an
interconnect: **1.5–2× the latency, and much worse for coherence traffic.**

The three rules:

1. **First-touch allocation.** Linux places a page on the node of the thread that first
   *writes* it — not the one that `malloc`ed it. Allocate in the thread that will use it.
2. **Pin threads to nodes.** `numactl --cpunodebind=0 --membind=0`. Unpinned threads
   migrate and drag their working set across the interconnect.
3. **Cross-socket false sharing is brutal.** The §6 penalty, multiplied.

Apple's P/E cluster split is a mild analogue: cross-cluster L2 access carries a penalty,
which is why the thread-migration warning in §2 is not pedantry.

**One more server-only asymmetry your laptop can't show you: SMT.** Two hyperthreads on
one x86 core *share* the L1 and the L2, so "16 vCPUs" may be 8 cores' worth of cache split
16 ways. A worker count tuned by vCPU count can halve each worker's effective cache. Apple
Silicon has no SMT, so this effect is invisible here and will surprise you in production —
another entry in the "your laptop is not the target machine" ledger.

For a forking Python server (`27-multiprocessing-and-subinterpreters.md`), the practical
version is: pin workers to nodes, and be aware that copy-on-write pages are placed by
whoever touches them first.

---

## 10. What all of this means for CPython

Here is where Tier 0 pays for itself. Everything above is why Python performs the way it
does — not "interpreted languages are slow", but specific, physical reasons.

### 10.1 Every value is a pointer chase

```python
total = 0
for x in lst:      # lst is a list of ints
    total += x
```

Per iteration, the hardware does:
1. Load `lst->ob_item[i]` — a pointer. *Sequential, prefetchable, cheap.*
2. Dereference it to reach the `PyLongObject`. **Random. Cache miss. ~300 cycles.**
3. Read the refcount and type pointer from its header. *Same line, cheap.*
4. Read the digit(s). *Same line.*
5. `Py_INCREF`/`Py_DECREF` — **write** to the refcount.
6. Allocate a *new* `PyLongObject` for the result (unless it's a small cached int).

Compare the NumPy equivalent: one contiguous `int64` array, fully sequential, fully
prefetched, no allocation, no refcounting. **The ~50–100× gap between a Python loop and
a vectorized operation is mostly steps 2, 5 and 6 — memory behaviour, not interpretation
overhead.** People attribute the gap to "the interpreter"; the interpreter is real but
it is not the biggest term.

### 10.2 Refcounting writes to memory you are only reading

Step 5 above is the deep one. **Reading a Python object mutates it.** The refcount lives
at offset 0 of every `PyObject`, so:

- Every read dirties the line, forcing writeback later.
- A read-only shared object still generates M-state coherence traffic across cores.
- The refcount and the data share a line, so you can't separate hot-write metadata from
  cold-read payload.

Now re-read §5 and §6. This is the physical origin of the GIL. CPython's answer in 1992
was "only one thread runs bytecode, so those writes are never concurrent." Everything in
[`24-the-gil.md`](24-the-gil.md) follows from this paragraph.

### 10.3 Object layout is your lever

| Structure | Layout | Cache behaviour |
|---|---|---|
| `list` of ints | contiguous pointers → scattered objects | 1 miss/element |
| `list` of small ints (−5..256) | pointers → *cached, shared* objects | often L1-hot; the small-int cache is a locality optimization |
| `array.array('q')` | contiguous raw int64 | sequential, prefetched |
| NumPy `ndarray` | contiguous typed buffer | sequential, prefetched, SIMD-able |
| Plain instance (3.11+, managed dict) | values array attached to the object | **1 hop** once `LOAD_ATTR_INSTANCE_VALUE` specializes |
| Plain instance *after* `vars()`/pickle | object → dict → entries → values | **3+ hops**, and ~60% more memory |
| Instance with `__slots__` | values inline in the object | **1 hop; often same line** |
| `dict` (compact, 3.6+) | dense entry array + sparse index | far better locality than pre-3.6 |
| Key-sharing dict | keys stored once per class | saves memory *and* lines |

**The `__slots__` row needs care, because the folklore is wrong.** Since 3.11 a plain
instance's attributes already live in a values array attached to the object, with the keys
shared per class — so the "three hops" story describes what happens *after* something
materializes a real `__dict__`, not the normal case. Measured, the remaining `__slots__`
win is ~1.4× memory and ~1.2× attribute-read speed, not the 6× commonly quoted.
[`16-object-memory-layout.md`](16-object-memory-layout.md) §8–§9 has the numbers and the
methodology error that produced the folklore.

The row that *is* worth 60% of your heap is the second one: an object graph that gets
`vars()`'d or pickled wholesale un-optimizes every instance it touches.

### 10.4 The GC is cache-hostile by nature

A cycle-collection pass traverses the object graph — pointer chasing, by definition,
across the whole heap. It evicts your working set as it goes. So a GC pause costs you
twice: the pause itself, *and* the cold cache your code resumes into.

This is background for `22-garbage-collection.md`, and it's part of why the incremental-GC
attempt was subtle enough to be reverted twice (README §15): changing *when* you traverse
changes *what's in cache* when you do.

### 10.5 The practical hierarchy of optimizations

Ordered by effect size, which is the order §33 argues for:

1. **Don't touch the memory.** Better algorithm, less data. Unbeatable.
2. **Make the layout contiguous and typed.** NumPy, `array`, Arrow, `struct`. This is the
   ~100× lever from §1.
3. **Reduce hops.** `__slots__`, hoist attribute lookups out of loops, flatten nesting.
4. **Reduce allocation.** Every new object is a potential compulsory miss plus allocator work.
5. **Then** consider Cython/Rust/C — which mostly works *because* it lets you control 2–4.

Notice that "rewrite it in C" is fifth, and that its benefit is largely a memory-layout
benefit. An engineer who reaches for a native extension without first fixing layout
usually gets a disappointing 2× instead of the available 50×.

### 10.6 The interpreter is an instruction-side workload too

Everything above concerns the data your program touches. But an interpreter is unusual:
**its instruction footprint is enormous relative to what it accomplishes.** The eval loop
is one function containing a case per opcode; in 3.14 that's several hundred cases, plus
the specialized variants PEP 659 generates, plus the tier-2 machinery. Executing one
Python bytecode may touch a few hundred bytes of a 192 KB L1i — and the *next* bytecode
jumps somewhere else entirely.

Two mechanisms, both from §7's last table:

1. **Indirect branch prediction.** Dispatch is one indirect jump serving every opcode.
   The classic result (Ertl & Gregg, 2003) is that a single shared dispatch site
   mispredicts badly, and that *replicating* the dispatch — a computed `goto` at the end
   of each opcode, which CPython uses where the compiler supports it — gives the predictor
   per-opcode history to work with. Modern ITTAGE-class predictors are far better at this
   than 2003 hardware, so treat the size of the effect today as **unmeasured here**; the
   mechanism is not in doubt, the magnitude is.
2. **Footprint.** More distinct hot code means more L1i, more iTLB, more BTB pressure —
   *and*, in CPython specifically, more `PyCodeObject`s each carrying their own inline
   caches and specialization state, which is **data**-side footprint that scales with how
   much of your program is hot.

A crude but revealing measurement — identical 40-line function bodies, called in random
order, `python3.14` *(measured)*:

| Distinct functions | ns/call |
|---|---|
| 1 | **331** |
| 8 | 657 |
| 512 | 617 |
| 4096 | **685** |

**Roughly 2× slower per call purely from having more hot code**, with the same work done
per call. *Honest caveat:* this confounds L1i/BTB pressure with inline-cache and
code-object footprint, and I have no PMU counters on macOS to separate them (§2's tooling
note). Both are footprint effects; which one dominates is a genuine open question you'd
need a Linux box and `perf stat -e icache_misses,br_misp_retired` to settle.

**Why this matters at staff level:** it's the physical argument against the
"tiny-functions-everywhere" style in hot paths, against deep decorator stacks, and against
generated code that inflates the hot set. It is also why specialization (PEP 659) is a
double-edged optimization: it makes each opcode cheaper *and* the hot code larger. See
[`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md) and `20-eval-loop.md`.

---

## 11. Lab exercises

Reading this leaves you at rung 3 (README §14). These are written for your M3 Pro; all
need only wall-clock timing.

Where a lab has a known answer on this machine, it's given — a lab you can't grade is a
lab you can fool yourself with.

**1 — Draw the latency curve.** Pointer-chase through a shuffled array (a Sattolo shuffle
gives a single cycle with no fixed point, so the prefetcher gets nothing), for sizes from
16 KB to 512 MB. Plot ns/access vs size on a log axis. **Predict the step locations before
you run it**: 128 KB (L1d), then 16 MB (L2). Note what that implies about the ~12 MB SLC —
it sits *behind* a 16 MB L2, so on this chip you should expect **no separately visible SLC
step at all**; if you think you see one, you're probably looking at a TLB step (§8). Then
find the real TLB steps hiding in the same curve. *Expected at 256 MB: ~120 ns/access.*

**2 — Sequential vs random vs dependent, same data.** Three loops over the same array,
same number of accesses: sequential, random-with-precomputed-indices, and pointer chase.
*Expected on this machine at 256 MB: ~0.3 / ~5 / ~122 ns.* Predict the ordering **and the
gaps** first. Most people predict two groups and find three — the 24× between random and
chase is §1.1's MLP, and it's the result worth internalizing from this whole document.

**3 — Determine your true false-sharing granule.** N threads, each incrementing its own
counter in a shared array, with padding swept over 8/16/32/64/**128**/256 bytes. Plot
throughput vs padding. Where does it plateau? Compare to `hw.cachelinesize` = 128 and to
the caveat in §6. **Do not trust this document's answer over your own measurement.**

**4 — Reproduce the Gilectomy's first failure in 30 lines.** Extend lab 3: (a) thread-local
counters, (b) separate counters sharing a line, (c) one shared *atomic* counter. Plot
throughput vs thread count. You should get flat, collapsed, and collapsed-worse — and
(c) *getting worse as you add cores* is precisely the 30% regression that killed
atomic refcounting ([`24-the-gil.md` §7](24-the-gil.md#7-the-gilectomy-larry-hastings-seven-core-lesson)).

**5 — The power-of-two trap.** Sum one column of an N×N matrix for N = 1024 and N = 1025.
Explain the difference using §4. Then find another N that's *slower* than a larger N.

**6 — `__slots__` locality.** One million instances with and without `__slots__`.
Measure attribute-access throughput *and* RSS. Attribute the two effects separately —
which part is memory saved, which part is hops removed?

**7 — Watch Python lose to layout, not to interpretation.** Sum 10M values as: a Python
list of ints, an `array.array('q')`, and a NumPy array — all in pure Python loops
(no `.sum()`). The gap between list and `array` is *pure memory layout*, with the
interpreter held constant. That number is the thesis of this document.

**8 — Prove your laptop is a bad benchmark host.** Run lab 2 pinned to low QoS (E-cores)
vs default (P-cores), and run it while a background build is going. Record the spread.
Carry that number into `31-measurement-methodology.md` — it's your noise floor, and
it's probably larger than the effects you'll want to measure later.

**9 — Catch the DMP in the act (§7).** Run lab 1's chase twice over the same size: once
through a Sattolo-shuffled permutation, once through a chase whose nodes were allocated in
traversal order. If Apple's data memory-dependent prefetcher is helping, the second is
substantially faster despite being "the same" dependent chain. *This is also the honest
way to find out whether your chase benchmark was ever measuring what you thought.*

**10 — Price the instruction side (§10.6).** Generate N identical Python functions and call
them in random order for N = 1, 8, 512, 4096. *Expected: ~330 ns/call at N=1 rising to
~685 at N=4096.* Then argue for the confound: how much of that is L1i/BTB and how much is
inline-cache footprint? Design the experiment that would separate them — and notice you
need PMU counters, i.e. a Linux box, to actually run it.

---

## 12. Question bank

1. Why is an L1 hit ~100× faster than DRAM, and why does that ratio matter more than absolute numbers? *(§1)*
2. Your cache line is 128 bytes but you padded to 64. What happens, and on which machines? *(§6, §2)*
3. Two threads write to different variables and throughput collapses. Diagnose and fix. *(§6)*
4. Two threads write to *the same* variable. Why can't padding fix it, and what are the three real options? *(§6)*
5. Why does making an array *larger* sometimes make a loop *faster*? *(§4)*
6. Why can't the prefetcher help a linked-list traversal, and why doesn't out-of-order execution rescue it? *(§7)*
6b. Two loops miss cache on every access, at the same addresses. One is 24× faster. Why? *(§1.1)*
6c. On what hardware is "pointer chasing cannot be prefetched" false, and what did that optimization cost the industry? *(§7)*
6d. Your cache line is 128 bytes, `sysctl` says 128, the compiler says 256, and 7-cpu measured 64. Which do you pad to, and why is that not a contradiction? *(§6)*
7. What is TLB reach, and why does Apple's 16 KB page size matter for large object graphs? *(§8)*
8. A workload fits in L2 by size but runs at DRAM speed. Give two explanations. *(§4 conflict misses, §8 TLB)*
9. Reading a Python object writes to memory. Why, and what are the three consequences? *(§10.2)*
10. Explain, physically, why NumPy beats a Python loop by ~100× — without using the word "interpreted". *(§10.1)*
11. Why is immortalizing `None` a *coherence* optimization rather than a refcounting one? *(§6, §10.2)*
12. Why is `__slots__` better described as a locality optimization than a memory optimization? *(§10.3)*
13. Your benchmark on this laptop varies 30% run to run. Give three hardware-level causes. *(§2, §11 lab 8)*
14. Why does a GC pause cost more than the pause duration? *(§10.4)*
15. Why is storing 8 bytes to a cold address more expensive than reading 8 bytes from one? *(§3)*
16. An interpreter has a small data working set and is still memory-bound. Explain how. *(§10.6)*
17. Specialization makes each opcode cheaper. Give the mechanism by which it can also make a program slower. *(§10.6)*

---

## 13. Sources

**Foundational**
- **Ulrich Drepper, [*What Every Programmer Should Know About Memory*](https://people.freebsd.org/~lstewart/articles/cpumemory.pdf)** (2007) 🆓 — dated hardware, permanently correct concepts. §3–§6 of this doc are a compressed, modernized retelling. Read the original.
- **Bryant & O'Hallaron, *Computer Systems: A Programmer's Perspective*, 3e** — ch. 6 is the best textbook treatment of the hierarchy; ch. 5 connects it to optimization.
- **Denis Bakhvalov, [*Performance Analysis and Tuning on Modern CPUs*, 2e](https://easyperf.net/)** (2024) 🆓 — the modern practical complement: top-down analysis and how to *measure* everything here.

**The instruction side (§10.6)**
- **Ertl & Gregg, [*The Structure and Performance of Efficient Interpreters*](https://www.jilp.org/vol5/v5paper12.pdf)** (2003) 🆓 — indirect-branch prediction and why dispatch replication works. Dated hardware, and say so when you cite it.
- **Rohou, Swamy & Seznec, [*Branch Prediction and the Performance of Interpreters — Don't Trust Folklore*](https://inria.hal.science/hal-01100647/document)** (2015) 🆓 — the modern rebuttal: ITTAGE-class predictors handle interpreter dispatch far better than the 2003 numbers imply. Read both, in that order.

**Coherence & concurrency**
- **Herlihy, Shavit, Luchangco & Spear, *The Art of Multiprocessor Programming*, 2e** (2020) — ch. 7 on spin locks and contention is the theory behind §5–§6.
- **Paul McKenney, [*Memory Barriers: a Hardware View for Software Hackers*](http://www.rdrop.com/users/paulmck/scalability/paper/whymb.2010.07.23a.pdf)** 🆓 — store buffers and invalidate queues, i.e. *why* §5's last subsection is true. Continued in `02-atomics-and-memory-models.md`. See also his [*Is Parallel Programming Hard?*](https://www.kernel.org/pub/linux/kernel/people/paulmck/perfbook/perfbook.html) 🆓.
- **Sorin, Hill & Wood, *A Primer on Memory Consistency and Cache Coherence*, 2e** — the rigorous treatment if MESI's details matter to you.

**Hardware specifics used in this document**
- [7-cpu.com — Apple M1](https://www.7-cpu.com/cpu/Apple_M1.html) — source of the §1 and §8 measured latency/TLB tables (16 KB page mode).
- [Daniel Lemire — Measuring the size of the cache line empirically](https://lemire.me/blog/2023/12/12/measuring-the-size-of-the-cache-line-empirically/) — method for lab 3, and the source of the honest caveat in §6.
- [GoFetch](https://gofetch.fail/) (Chen et al., 2024) — the attack that made Apple's data memory-dependent prefetcher public knowledge. §7's exception, and the reason to distrust "pointer chasing can't be prefetched" on this machine.
- `clang -target … -E` on `__GCC_DESTRUCTIVE_SIZE` — the fourth authority in §6's table. Two lines of C++; run it before quoting anyone's cache-line constant.
- [Measuring Cache Hierarchy on Apple M4 with Pointer Chasing](https://soohamurai.com/2026/01/31/Measuring-Cache-Hierarchy-on-Apple-M4/) — a worked version of lab 1 on Apple Silicon.
- `sysctl -a | grep hw.` on the machine itself — always the ground truth. Published specs for Apple Silicon are frequently wrong.

**Applied to Python**
- [`24-the-gil.md`](24-the-gil.md) — §5 and §6 of this doc are its prerequisites.
- `16-object-memory-layout.md` — where §10.3's table gets measured.
- [PEP 703](https://peps.python.org/pep-0703/) §Reference Counting — immortalization and biased refcounting as coherence engineering.

---

*Next: `02-atomics-and-memory-models.md` — §5's store buffers, taken seriously, on a
weakly-ordered machine.*
