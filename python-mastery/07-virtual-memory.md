# 07 — Virtual memory: page tables, faults, and why RSS is not what you allocated

> **Tier 1, doc 07.** Prerequisites: [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
> (cache lines, the TLB, 16 KB pages), [`06-processes-threads-scheduling.md`](06-processes-threads-scheduling.md).
> Feeds into: [`08-allocators.md`](08-allocators.md), [`16-object-memory-layout.md`](16-object-memory-layout.md),
> [`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md),
> [`35-memory-optimization.md`](35-memory-optimization.md), [`46-production-python.md`](46-production-python.md).
>
> **THESIS: every number your process reports about its own memory is a claim about
> page tables, and most of those claims are answers to a different question than the one
> you asked.** `malloc` returning is not memory. RSS is not what you allocated. `free()`
> is not memory returned. The gap between "bytes I asked for" and "pages the kernel is
> holding on my behalf" is where OOM kills, mysterious RSS growth, and the entire
> pre-forking-server memory story live. This document closes that gap.
>
> The single most important consequence for CPython: **a reference count is a write**,
> and a write to a shared page is a page copy. Section 7 measures a forked child that
> *only reads* a shared object graph and still privatises **88% of its parent's heap**.

> **Provenance.** Measurements were produced on the machine this repo lives on:
> **Apple M3 Pro (11 cores: 5 P + 6 E), 18 GiB RAM, macOS 26.5.2 / Darwin 25.5.0 arm64,
> xnu-12377.121.10**, **16 KB pages**, 128-byte cache lines, 47-bit user virtual
> addresses. Interpreter: **CPython 3.14.6** (`~/.local/bin/python3.14`). Numbers marked
> *(measured)* came out of a live process during the writing of this document; timing
> figures are **medians of ≥5 alternating passes with min/max spread reported**, per the
> house rules in [`31-measurement-methodology.md`](31-measurement-methodology.md).
>
> **The machine was loaded throughout: load average 1.93–2.37 (1-minute) across the
> session**, with 2.2 GB already in swap and 1.07 M pages held by the compressor. That is
> a hostile measurement environment and it showed — §3.3 documents a benchmark that
> produced a completely false result before the noise was tracked down.
>
> **Linux-specific material — overcommit modes, the OOM killer, THP, PSI, `smaps`/PSS —
> is researched from primary sources, not measured here.** It is attributed inline
> ("the kernel documentation specifies…", "Kerrisk documents…"). Nothing researched is
> ever presented as a measurement.

## Contents

1. [What virtual memory actually buys you](#1-what-virtual-memory-actually-buys-you)
2. [Page tables and the walk](#2-page-tables-and-the-walk)
3. [Demand paging: the fault path, minor and major](#3-demand-paging-the-fault-path-minor-and-major)
4. [Lazy allocation, measured](#4-lazy-allocation-measured)
5. [`mmap` in all its modes](#5-mmap-in-all-its-modes)
6. [Copy-on-write and `fork`](#6-copy-on-write-and-fork)
7. [The CPython COW catastrophe — the measurement that matters](#7-the-cpython-cow-catastrophe--the-measurement-that-matters)
8. [Overcommit](#8-overcommit)
9. [The OOM killer](#9-the-oom-killer)
10. [RSS vs VSZ vs PSS vs USS](#10-rss-vs-vsz-vs-pss-vs-uss)
11. [Transparent huge pages — and why databases turn them off](#11-transparent-huge-pages--and-why-databases-turn-them-off)
12. [Swap, reclaim, and pressure stall information](#12-swap-reclaim-and-pressure-stall-information)
13. [`madvise` and giving memory back](#13-madvise-and-giving-memory-back)
14. [Why freed memory does not come back, measured](#14-why-freed-memory-does-not-come-back-measured)
15. [Attributing a Python process's memory](#15-attributing-a-python-processs-memory)
16. [The cost model](#16-the-cost-model)
17. [Lab exercises](#17-lab-exercises)
18. [Question bank](#18-question-bank)
19. [What I could not verify](#19-what-i-could-not-verify)
20. [Sources](#20-sources)

---

## 1. What virtual memory actually buys you

Three things, and it is worth separating them because people conflate them constantly:

1. **Isolation.** Process A cannot name a byte in process B. This is a *safety* property
   and it is the reason the abstraction is not optional.
2. **Indirection.** The address in your pointer is not the address on the DRAM bus. That
   indirection is what makes `fork` cheap, shared libraries shared, `mmap` possible, and
   copy-on-write expressible at all.
3. **Overcommitment of a scarce resource.** The sum of every process's address space
   vastly exceeds physical RAM, and that is fine, because address space is free and only
   *residency* is expensive.

Point 3 is where the engineering lives. The kernel's whole job is to keep the promise
"this address is valid" while backing as few of those promises with physical frames as
it can get away with. Every mechanism in this document — demand paging, COW, reclaim,
overcommit, the OOM killer — is either part of making that bet or part of paying it off
when it goes wrong.

The unit of the bet is the **page**. On this machine *(measured)*:

```
$ sysctl -n hw.pagesize hw.cachelinesize hw.memsize machdep.virtual_address_size
16384          <- 16 KB pages
128            <- 128-byte cache lines
19327352832    <- 18 GiB
47             <- 47-bit user virtual addresses
```

16 KB pages, consistent with [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §8,
which works out the TLB-reach consequences (2.5 MB L1 reach, 48 MB L2 reach) and which
this document builds on rather than repeats. On x86-64 Linux the base page is 4 KB, and
almost every published number about page-table overhead, THP thresholds, and fault rates
assumes that. **Where a figure depends on page size, this document says which.**

A 47-bit address space is 128 TiB per process. The measured baseline `VSZ` of a
do-nothing CPython 3.14.6 process on this machine is **425 GB** *(measured)* — which
should immediately tell you what `VSZ` is worth as a memory metric. See §10.

---

## 2. Page tables and the walk

The page table is a radix tree indexed by the bits of a virtual address. Each level's
table is itself one page, holding `PAGE_SIZE / 8` descriptors, so the number of index
bits per level is `log2(PAGE_SIZE / 8)`.

That one formula generates every configuration you will meet:

| Granule | Entries/table | Bits/level | Levels for a 48-bit VA |
|---|---|---|---|
| 4 KB | 512 | 9 | 4 (9+9+9+9+12) |
| 16 KB | 2048 | 11 | 4, but the top level holds only 2 entries |
| 64 KB | 8192 | 13 | 3 (13+13+13+16 → 55 bits, capped) |

**x86-64 with 4 KB pages** is the canonical four-level walk: PGD → PUD → PMD → PTE,
9 bits each plus a 12-bit offset = 48 bits. Intel's 5-level paging (LA57) adds P4D for a
57-bit space; Linux supports it and enables it only when the hardware reports it.

**This machine — arm64 with a 16 KB granule** — works out differently, and it is worth
doing the arithmetic because it is a trap:

```
16 KB granule, 8-byte descriptors -> 2048 entries per table -> 11 index bits per level

  L3 table: 11 bits + 14-bit page offset      = 25 bits  -> one L3 table covers   32 MB
  L2 table: 11 bits more                      = 36 bits  -> one L2 table covers   64 GB
  L1 table: 11 bits more                      = 47 bits  -> one L1 table covers  128 TB
  L0 table: would add 1 more bit (2 entries)  = 48 bits

  measured: machdep.virtual_address_size = 47
         -> the user-space walk starts at L1 and is THREE levels deep, not four.
```

So on this configuration a TLB miss costs **three** dependent memory accesses for user
addresses, not four. Doc 01 §8 says "up to four dependent memory accesses" — that is the
correct general statement (and the right one for the 4 KB x86-64 case that dominates
production); the 16 KB granule with a 47-bit VA is the narrower, better case. I derived
the level count from the granule arithmetic plus the measured VA width, **not** by
reading `TCR_EL1`, which is not accessible from user space — see §19.

Here is the walk, drawn for the 4 KB / 48-bit configuration you will actually be
debugging on a Linux server:

```
                 VIRTUAL ADDRESS (x86-64, 4 KB pages, 48-bit)
   63        48 47      39 38      30 29      21 20      12 11         0
  ┌────────────┬──────────┬──────────┬──────────┬──────────┬───────────┐
  │ sign-extend│  PGD idx │  PUD idx │  PMD idx │  PTE idx │  offset   │
  │  (unused)  │  9 bits  │  9 bits  │  9 bits  │  9 bits  │  12 bits  │
  └────────────┴────┬─────┴────┬─────┴────┬─────┴────┬─────┴─────┬─────┘
                    │          │          │          │           │
   CR3 ──────┐      │          │          │          │           │
             ▼      ▼          │          │          │           │
        ┌─────────────┐        │          │          │           │
        │  PGD (4 KB) │────┐   │          │          │           │
        └─────────────┘    │   │          │          │           │
                    ┌──────┘   │          │          │           │
                    ▼          ▼          │          │           │
              ┌─────────────────┐         │          │           │
              │   PUD (4 KB)    │────┐    │          │           │
              └─────────────────┘    │    │          │           │
                            ┌────────┘    │          │           │
                            ▼             ▼          │           │
                      ┌─────────────────────┐        │           │
                      │      PMD (4 KB)     │───┐    │           │
                      └─────────────────────┘   │    │           │
                          │                     │    │           │
                          │ PS bit set?         └────┴──┐        │
                          │ -> 2 MB huge page,           ▼       │
                          │    walk STOPS here     ┌──────────┐  │
                          │    (this is THP)       │PTE (4 KB)│  │
                          ▼                        └────┬─────┘  │
                    ╔═══════════════╗                   │        │
                    ║ 2 MB PHYSICAL ║                   ▼        ▼
                    ╚═══════════════╝            ┌──────────────────┐
                                                 │  4 KB PHYSICAL   │
                                                 │      FRAME       │
                                                 └──────────────────┘
                    ▲
                    └── each level is ONE dependent memory access.
                        4 accesses, each of which can miss in cache.
                        This is why the TLB exists (doc 01 §8), and why
                        huge pages help: they delete a level AND
                        multiply TLB reach by 512.
```

Two things fall out of this diagram that matter later:

**Page tables cost memory.** They are ordinary pages holding descriptors. A process
mapping a lot of memory sparsely pays for a lot of near-empty tables. On this machine the
`footprint` tool breaks it out explicitly *(measured, on a CPython process holding 1.5 M
tuples)*:

```
  Dirty      Clean  Reclaimable    Regions    Category
    ---        ---          ---        ---    ---
 186 MB        0 B          0 B        191    untagged (VM_ALLOCATE)
  11 MB        0 B          0 B          1    MALLOC_LARGE
3856 KB        0 B          0 B          5    MALLOC_SMALL
 385 KB        0 B          0 B          1    page table          <---
```

**385 KB of page tables for 204 MB of footprint** — about 0.18%, or roughly one page-table
byte per 540 bytes mapped. With 16 KB pages each leaf descriptor covers 4× what a 4 KB
descriptor covers, so the equivalent 4 KB-page process would pay closer to 0.7%. That is
small until you multiply it by a few hundred forked workers, at which point page tables
alone are a measurable line item — and unlike most memory, **page tables are never
shared between processes after `fork`**; each child gets its own copy of the tree even
when every leaf points at the same shared frame.

**A block descriptor short-circuits the walk.** If the PMD entry has its page-size bit
set, it maps a 2 MB region directly and there is no PTE level. That is exactly what a
transparent huge page is (§11) — one fewer memory access *and* one TLB entry covering
512× more address space.

---

## 3. Demand paging: the fault path, minor and major

Nothing is resident until it is touched. The kernel's `mmap` gives you a *promise*
recorded in a VMA (`vm_area_struct` on Linux — Gorman's *Understanding the Linux Virtual
Memory Manager* ch. 4 is the canonical walkthrough), and the page table entries stay
empty until the first access traps.

```
                    CPU issues a load/store to a virtual address
                                     │
                                     ▼
                          ┌────────────────────┐
                          │  TLB lookup        │
                          └─────────┬──────────┘
                              hit   │   miss
                    ┌───────────────┘   └────────────────┐
                    ▼                                    ▼
            ┌───────────────┐                  ┌──────────────────────┐
            │ done, ~0 extra│                  │  hardware page walk  │
            │ cost          │                  │  (§2, 3-4 accesses)  │
            └───────────────┘                  └──────────┬───────────┘
                                            PTE present?  │
                                    ┌─────────────────────┴──────────┐
                                yes │                                │ no / not writable
                                    ▼                                ▼
                            ┌──────────────┐              ╔═════════════════════╗
                            │ fill TLB,    │              ║    PAGE FAULT       ║
                            │ done         │              ║  trap to kernel     ║
                            └──────────────┘              ╚══════════╤══════════╝
                                                                     │
                              ┌──────────────────────────────────────┤
                              │                                      │
                              ▼                                      ▼
                   ┌─────────────────────┐              ┌────────────────────────┐
                   │  is the address in  │  no          │  SIGSEGV               │
                   │  any VMA / mapping? ├─────────────▶│  (this is the ONLY     │
                   └──────────┬──────────┘              │   "invalid" outcome)   │
                              │ yes                     └────────────────────────┘
                              ▼
              ┌───────────────────────────────────┐
              │  what does it need?               │
              └───┬──────────┬──────────┬─────────┘
                  │          │          │
     anonymous,   │  COW:    │  file-backed, not in page cache
     never touched│  page is │          │
                  │  shared &│          │
                  ▼  RO      ▼          ▼
      ┌────────────────┐ ┌──────────┐ ┌────────────────────────┐
      │ MINOR FAULT    │ │  MINOR   │ │   MAJOR FAULT          │
      │ map the shared │ │  FAULT   │ │   issue disk I/O,      │
      │ zero page (RO) │ │ copy the │ │   BLOCK the thread,    │
      │ or allocate +  │ │ page,    │ │   context switch away, │
      │ zero a frame   │ │ remap RW │ │   come back on I/O     │
      │                │ │          │ │   completion           │
      │  ~0.5-0.6 µs   │ │ ~0.5 µs  │ │      ~15.9 µs          │
      │   (measured)   │ │(measured)│ │      (measured)        │
      └────────────────┘ └──────────┘ └────────────────────────┘
                  │          │          │
                  └──────────┴──────────┴──▶ update PTE, fill TLB, RETRY the
                                             faulting instruction. The program
                                             never knows it happened.
```

The vocabulary matters because the two fault kinds have different costs and different
cures:

- A **minor fault** (Linux: `ru_minflt`) is resolved without I/O: map the zero page,
  allocate a frame, copy a COW page, or find the page already in the page cache.
- A **major fault** (`ru_majflt`) requires reading from a backing store. The thread
  blocks. This is what "thrashing" is made of.

### 3.1 The measured costs

Both, on this machine, sweeping one byte per page over a 128 MiB anonymous mapping
(8,192 pages), 7 passes, median with spread *(measured)*:

| Event | Cost | vs. a warm access |
|---|---|---|
| Warm access (already resident, in the loop) | **12.08 ns** (min 12.07, max 13.49) | 1× |
| — of which Python loop overhead alone | 2.58 ns | — |
| Minor fault, first-touch zero-fill | **~0.53–0.63 µs** | **~40–52×** |
| Minor fault, copy-on-write | **~0.46–0.61 µs** | **~40–51×** |
| Major fault, file-backed, cold | **15.9 µs** | **~1,300×** |

Two independent runs of the whole benchmark gave zero-fill 626.8 ns and 532.2 ns, COW
611.2 ns and 460.5 ns — so treat these as *half-microsecond-ish*, not as three
significant figures. The ratios are the durable part.

The major-fault figure comes from a separate experiment: a 256 MiB file written with
`F_NOCACHE`, then `mmap`ed read-only in a fresh process and swept one byte per page
*(measured)*:

```
swept 16,384 pages of a file-backed PRIVATE read-only mapping
  RSS delta        : 256.03 MB
  footprint delta  : 0.13 MB     <- file pages are not charged as anonymous
  minor faults     : 1
  MAJOR faults     : 16,385
  ns/page          : 15883.3
```

Re-running the identical sweep with the file now warm in the page cache:

```
  minor faults     : 16,386
  MAJOR faults     : 0
  ns/page          : 643.7
```

**Same code, same mapping, same page count: 15,883 ns/page vs 643.7 ns/page — 24.7×,
and the only difference is whether the data was in RAM.** That is the entire argument for
caring about your page cache hit rate, and it is why `ru_majflt` climbing is a
five-alarm signal in a way that `ru_minflt` climbing is not.

Note also that `MAJOR faults` (16,385) exceeds the page count by one, and the warm run
reports 16,386 minor faults for 16,384 pages. The off-by-one-or-two is mapping setup;
the ratio is unaffected.

### 3.2 Faults are not errors

The single most common confusion: a page fault is a normal, expected, extremely frequent
control transfer. This machine's kernel has serviced **818,044,288** "translation faults"
since boot *(measured, `vm_stat`)*. A fault only becomes `SIGSEGV` when the faulting
address is in no mapping at all — the leftmost branch of the diagram.

### 3.3 The benchmark that lied — and what it taught

The first version of the §3.1 benchmark reported a warm-access cost of **140 ns** and a
"fault cost" of 516 ns. Both were wrong, and the drift control caught it: the benchmark
took two warm sweeps per pass, and they disagreed by **13×** (140.1 ns vs 10.9 ns).

Chasing it down produced three findings worth more than the original number:

1. **It was not DVFS or P/E-core migration.** The obvious suspect on this machine
   (doc 31 documents exactly that failure mode) was ruled out by timing an *empty* loop
   over the same index list: **2.8 ns/iteration, flat from iteration 0**, no ramp at all.
2. **It was not the interpreter warming up.** Pre-warming the loop on a scratch mapping
   and *then* taking a fresh mapping reproduced the decay unchanged.
3. **It was `subprocess`.** The instrumented version called `vm_stat` between sweeps to
   read compressor counters. `vm_stat` runs via `fork`+`exec` — and **`fork` re-protects
   every writable private page of the *parent* for copy-on-write.** So every "warm"
   sweep after the first was silently taking 8,192 COW faults:

```
sweep   ns/page   minflt  majflt  pageins
    0    2080.9    8,406       4        1
    1    1590.5    8,421       0        0
    2     759.7    8,418       0        0     <- ~8,400 faults EVERY sweep,
    3     641.9    8,413       0        0        injected by the measuring
    4     624.0    8,416       0        0        instrument itself
```

**The instrument was generating the phenomenon it was measuring.** This is the §3 lesson
in miniature and it generalises: any profiler, any `ps` call, any shell-out inside a
timed region on a `fork`-ing platform perturbs the page tables of the process under test.

It also handed me a *better* instrument. A deliberate `fork()` + immediate `_exit()` is a
clean, repeatable COW-fault generator on an already-resident mapping — exactly one fault
per page, verifiable against `ru_minflt`. That is how the COW row of the §3.1 table was
measured, and it is a technique worth stealing.

---

## 4. Lazy allocation, measured

The clearest single demonstration that allocation and residency are different things.
Map 1 GiB anonymously, touch nothing *(measured)*:

```
--- after mmap(1,073,741,824) anonymous private, ZERO touches ---
RSS delta            :        49,152  (0.05 MB)
footprint delta      :             0  (0.00 MB)
VSZ delta            : 1,073,741,824  (exactly 1 GiB)
minor faults delta   : 359   major: 0
```

**VSZ went up by exactly one gibibyte. Resident memory went up by three pages** — and
those three are the `mmap` object's own bookkeeping, not the mapping. The kernel recorded
a promise and allocated nothing.

Now touch one byte in each of the 65,536 pages *(measured)*:

```
--- after touching 1 byte in each of 65,536 pages ---
RSS delta from mmap  : 1,073,758,208  (1024.02 MB)
bytes of RSS / touch :      16,384.2
minor faults delta   : 65,917   major: 1
minor faults / page  : 1.0058
```

Three things to take from this:

1. **16,384.2 bytes of RSS per single-byte write.** The page size is confirmed
   empirically, from first principles, without asking `sysctl`. Writing one byte costs
   16 KB because 16 KB is the granularity of the promise the kernel can keep.
2. **1.0058 minor faults per page** — one fault per page, as the model predicts, plus a
   0.6% garnish of unrelated interpreter activity.
3. Then, writing **every** byte of the first 16 MB of that same mapping:

```
--- writing EVERY byte of the first 16 MB (already faulted in) ---
RSS delta            :             0
minor faults delta   :             1
```

Zero. One fault. **Residency is per-page and one-time**; after the first touch the page
is yours and the millionth write to it is free. This is why "touch every page once at
startup" (`MAP_POPULATE`, or a warmup loop) converts a stream of latency spikes into one
predictable up-front cost — the single most effective trick for latency-sensitive
services that `mmap` large regions.

**The 4× page-size caveat.** On 4 KB-page Linux the same experiment yields 4,096 bytes
per touch and 262,144 faults for 1 GiB — **4× as many faults for the same memory**. Page
size is a fault-rate multiplier, and it is one of the reasons THP (§11) exists.

---

## 5. `mmap` in all its modes

`mmap` is the one syscall that exposes the VM system directly, and its flag combinations
are genuinely orthogonal. Two axes:

|  | **Anonymous** (`MAP_ANONYMOUS`) | **File-backed** (an `fd`) |
|---|---|---|
| **`MAP_PRIVATE`** | Zero-filled memory. This is what `malloc` uses for large requests, and what pymalloc uses for arenas. Writes are yours alone. | The file's contents, copy-on-write. Writes are **discarded**, never reach the file. How executables and `.so` text/data segments are mapped. |
| **`MAP_SHARED`** | Zero-filled memory visible to children across `fork`. The basis of `multiprocessing.shared_memory`. | The page cache itself, mapped into your address space. Writes go to the file *and* to every other mapper. |

The `MAP_PRIVATE`-on-a-file case is the one people get wrong, so it is worth proving.
Same file, same one-byte write through the mapping, read back with a plain `read()`
*(measured)*:

```
MAP_SHARED   wrote 235 into the mapping; file now holds 235  -> PERSISTED
MAP_PRIVATE  wrote 236 into the mapping; file now holds 235  -> discarded (COW)
```

`MAP_PRIVATE` on a file gives you a private, copy-on-write *view*. Your writes are real,
visible to you, and vanish when you unmap. This is exactly the mechanism that lets a
hundred processes share one libc `.text` while each keeps its own relocated `.data`.

### The flags that matter

Attribution: the following semantics are from `mmap(2)` and Kerrisk's *TLPI* ch. 49,
not measured here.

- **`MAP_FIXED`** — map at exactly this address, **silently unmapping anything already
  there**. The man page is unusually blunt: the only safe use is over a range you
  previously reserved with your own mapping, because otherwise a concurrent thread can
  acquire the range between your check and your call, and you will destroy its mapping.
  Linux added **`MAP_FIXED_NOREPLACE`** (5.1) which fails with `EEXIST` instead — use it.
- **`MAP_POPULATE`** — prefault the whole range at `mmap` time. Converts the §4 stream of
  faults into one up-front cost. `MAP_LOCKED` additionally tries to lock pages in, but
  the man page warns the guarantee is weaker than `mlock(2)`: "major faults might happen
  later on", so use `mmap` + `mlock` when major faults are genuinely unacceptable.
- **`MAP_NORESERVE`** — do not reserve swap for this mapping. Interacts directly with
  overcommit (§8): under `vm.overcommit_memory=2` this is what lets you map a sparse
  region larger than the commit limit.
- **`MAP_HUGETLB`** — back the mapping with explicitly reserved huge pages (distinct from
  THP, §11).

CPython's `mmap` module exposes a platform-dependent subset. On this machine
*(measured)*, `MAP_FIXED`, `MAP_POPULATE` and `MAP_HUGETLB` are **absent** and the
available flags are `MAP_ANON(YMOUS)`, `MAP_PRIVATE`, `MAP_SHARED`, `MAP_NORESERVE`,
`MAP_JIT`, `MAP_NOCACHE`, and a handful of Darwin-specific ones. If you need the missing
flags from Python you are dropping to `ctypes` or a C extension.

### Where `mmap` shows up in CPython whether you asked for it or not

- **pymalloc arenas.** 1 MiB anonymous private mappings — see
  [`16-object-memory-layout.md`](16-object-memory-layout.md) §3 for the arena/pool/block
  constants verified on this build (1 MiB arena, 16 KB pool, 512-byte small-object
  threshold, 32 size classes).
- **The system allocator** for anything above 512 bytes, which itself switches from `brk`
  to `mmap` above a threshold — [`08-allocators.md`](08-allocators.md).
- **`multiprocessing.shared_memory`** — `MAP_SHARED` over POSIX shm.
- **Importing an extension module** — file-backed `MAP_PRIVATE` of the `.so`.
- **The interpreter's own data stack**, chunked at 16 KB — see
  [`20-eval-loop.md`](20-eval-loop.md).

---

## 6. Copy-on-write and `fork`

`fork()` does not copy memory. It copies **page tables**, marks every writable private
page read-only in *both* parent and child, and lets the fault handler sort it out. The
first write by either side traps, the kernel copies that one page, marks the copy
writable for the writer, and retries.

```
  BEFORE fork:                    AFTER fork:                AFTER child writes page 2:

  parent PT                       parent PT   child PT       parent PT   child PT
  ┌───────┐                       ┌───────┐   ┌───────┐      ┌───────┐   ┌───────┐
  │ p0 RW │──┐                    │ p0 RO │─┐ │ p0 RO │─┐    │ p0 RO │─┐ │ p0 RO │─┐
  │ p1 RW │──┼──▶ ╔═══════╗       │ p1 RO │─┼─│ p1 RO │─┼──▶ │ p1 RO │─┼─│ p1 RO │─┼─▶╔═══════╗
  │ p2 RW │──┼──▶ ║ FRAME ║       │ p2 RO │─┼─│ p2 RO │─┼──▶ │ p2 RO │─┘ │ p2 RW │ │  ║FRAMES ║
  │ p3 RW │──┘    ║   S   ║       │ p3 RO │─┘ │ p3 RO │─┘    │ p3 RO │   └───┬───┘ └─▶║       ║
  └───────┘       ╚═══════╝       └───────┘   └───────┘      └───────┘       │        ╚═══════╝
                                                                             ▼
   4 frames                        STILL 4 frames.            ╔══════════════════╗
                                   Zero copied.               ║ 1 NEW FRAME      ║
                                   Two page-table trees.      ║ (copy of p2)     ║
                                                              ╚══════════════════╝
                                                              5 frames total.
```

Consequences that follow immediately:

- **`fork` is cheap in proportion to page-table size, not heap size.** A 10 GB parent
  forks about as fast as a 1 GB one.
- **The cost is deferred and paid per written page**, at the COW-fault price measured in
  §3.1: **~0.46–0.61 µs each** *(measured)*.
- **The parent pays too.** This is the part people miss, and §3.3 proves it: after
  `fork`, the *parent's* pages are read-only as well, so the parent's next write to each
  page also takes a COW fault. A parent that forks in a loop repeatedly re-protects its
  own heap.
- **Sharing decays monotonically.** Nothing ever un-COWs a page. Shared-ness only goes
  down over a worker's lifetime.

`fork` in a *threaded* process is a separate and much nastier topic — only the calling
thread survives, and any mutex held by another thread at fork time is locked forever in
the child. That is [`10-signals-fork-exec.md`](10-signals-fork-exec.md) and it is why
`multiprocessing`'s default start method changed.

---

## 7. The CPython COW catastrophe — the measurement that matters

Here is the production scenario. You have a pre-forking server — gunicorn, uWSGI,
`multiprocessing` with the `fork` start method. You load a large read-only structure in
the parent (a model, a routing table, a cache), fork N workers, and expect to pay for it
once. **You will not.**

The reason is one field. `ob_refcnt` is at **offset 0 of every Python object**
([`16-object-memory-layout.md`](16-object-memory-layout.md) §1). Reading a Python object
increments and decrements its reference count. **A read at the Python level is a write at
the hardware level**, and a write to a COW page copies the page.

### The experiment

Parent builds 800,000 three-element lists (~184 MB RSS / 173 MB footprint), `gc.collect()`s,
optionally `gc.freeze()`s, then forks one child. The child does one of four things. Each
(arm, freeze) pair runs as **its own process** so the parent heap is identical and
uncontaminated; the child's growth is measured with `proc_pid_rusage` — **live** resident
size, deliberately *not* `ru_maxrss`, which is a high-water mark and which
[`16-object-memory-layout.md`](16-object-memory-layout.md) §11 documents producing a
wrong answer in this folder before.

**Results** *(measured; parent heap 173.2 MB footprint, 800,000 rows)*:

| Child does | `gc.freeze()`? | Child RSS growth | Minor faults | % of parent heap privatised |
|---|---|---|---|---|
| nothing | no | **0.39 MB** | 17 | 0.2% |
| nothing | yes | 0.39 MB | 15 | 0.2% |
| **reads the graph** | **no** | **153.7 MB** | 19,245 | **88%** |
| **reads the graph** | **yes** | **153.7 MB** | 19,244 | **88%** |
| `gc.collect()` only | **no** | **156.9 MB** | 13,312 | **90%** |
| `gc.collect()` only | **yes** | **0.53 MB** | 26 | **0.3%** |
| reads + `gc.collect()` | no | 156.9 MB | 19,586 | 90% |
| reads + `gc.collect()` | yes | 153.8 MB | 19,256 | 88% |

Read those rows carefully, because there are two completely different lessons in them.

**Lesson 1 — merely reading the graph privatises 88% of the parent's heap.** The child
executed `for row in data: total += row[0]`. It mutated nothing. It allocated essentially
nothing. It grew by **153.7 MB** because 19,245 pages had to be copied so that reference
counts could be incremented and decremented on them. Eight workers doing this turn a
173 MB parent heap into ~1.4 GB of private memory. **This is the mechanism behind the
overwhelming majority of "our containers OOM after warmup" incidents.**

**Lesson 2 — `gc.freeze()` fixes a real and different problem, and it fixes it
spectacularly.** Look at the `gc.collect()`-only rows: **156.9 MB → 0.53 MB, a 295×
reduction**, and minor faults collapse from 13,312 to 26. A GC pass walks every tracked
container's `PyGC_Head` to link and unlink it from generation lists — also a write, also
to every page holding a container. `gc.freeze()` moves everything currently alive into a
permanent generation the collector never traverses, so those writes stop happening.
`gc.get_freeze_count()` reported **809,451** frozen objects *(measured)*.

**And now the honest part, which is the whole point of running all four arms:**
`gc.freeze()` did **nothing at all** for the read-only traversal — 153.7 MB either way,
identical to three significant figures. It cannot help. It addresses GC traversal writes;
it has no effect whatsoever on refcount writes. In the combined arm the traversal has
already dirtied nearly everything, so freezing buys back only ~3 MB of the ~157 MB.

So the folklore — "call `gc.freeze()` before forking and COW works" — is **half right**,
and the half it gets wrong is the half that usually dominates. Compare this with
[`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md) §10, which ranks
`gc.freeze()` first among mitigations and calls it "the single highest-value line of code
in a pre-forking Python server", and [`16-object-memory-layout.md`](16-object-memory-layout.md)
§5.1, which does the same. **Neither is contradicted on the facts** — both explicitly
also list "keep bulk data out of Python objects" — but the measurement here sharpens the
ranking considerably: `gc.freeze()` is necessary, cheap, and worth doing unconditionally,
yet on its own it addressed **0%** of the dominant cost in this workload. Doc 15's
point 3 and doc 16's point 3 are the ones doing the real work.

### What actually fixes it

In measured order of effect for this workload:

1. **Have fewer Python objects.** One NumPy array, one `mmap`, one Arrow buffer has *one*
   refcount for megabytes of payload. 800,000 lists have 800,000 counters spread across
   every page. The fork problem is not really about `fork` — it is about **per-object
   metadata density**.
2. **Don't fork it at all.** Load the data in a separate process and share it via
   `multiprocessing.shared_memory`, or let the filesystem page cache do it: a
   `MAP_SHARED` file-backed mapping is shared by construction and *stays* shared, because
   there is no COW to break (§5).
3. **`gc.freeze()` after loading, immediately before forking.** Cheap, one line, removes
   the entire GC-traversal component (295× on the arm where it applies). Do it. Just do
   not expect it to be sufficient.
4. **Immortal objects (PEP 683)** already removed refcount writes for `None`, `True`,
   `False`, small ints and interned strings — a COW fix as much as a cache-coherence one.
   See [`24-the-gil.md`](24-the-gil.md) §12.1.

### One number I cannot explain

The read-only child took **19,245 minor faults** but grew by 153.7 MB, which is only
9,835 pages — **1.96 faults per page actually privatised**. I do not have an account of
the roughly 2× discrepancy. Candidates: faults on shared-library and interpreter pages
unrelated to the data; a read fault and a subsequent write fault being counted separately
for the same page; or Darwin's `ru_minflt` counting something the Linux one would not.
Flagged in §19 rather than papered over.

---

## 8. Overcommit

> **Researched, not measured.** Source: the kernel's `Documentation/mm/overcommit-accounting.rst`
> and `Documentation/admin-guide/sysctl/vm.rst`. This machine is not Linux and has no
> equivalent knob.

`malloc` succeeding does not mean the memory exists. Linux lets you allocate address
space it could not possibly back, on the bet that you will not touch most of it — the
same bet §4 measured. `vm.overcommit_memory` selects the policy, and the kernel
documentation defines exactly three modes:

| Mode | Name | Behaviour (per kernel docs) |
|---|---|---|
| **0** | Heuristic (default) | "Obvious overcommits of address space are refused… ensures a seriously wild allocation fails while allowing overcommit to reduce swap usage." |
| **1** | Always overcommit | Never refuse. "Appropriate for some scientific applications. Classic example is code using sparse arrays and just relying on the virtual memory consisting almost entirely of zero pages." |
| **2** | Don't overcommit | Total commit may not exceed `swap + overcommit_ratio%` of RAM (**default 50%**). "In most situations this means a process will not be killed while accessing pages but will receive errors on memory allocation as appropriate." |

Mode 2 is the one people reach for after an OOM incident and then quietly revert. It
trades an unpredictable `SIGKILL` for a predictable `ENOMEM` — which sounds strictly
better until you discover how much software handles `malloc` returning `NULL` by
crashing anyway, and that the default 50% ratio leaves a lot of RAM unusable. The tuning
knobs are `vm.overcommit_ratio` (percentage) or `vm.overcommit_kbytes` (absolute); the
docs note these have effect **only** in mode 2.

The live accounting is visible in `/proc/meminfo` as **`CommitLimit`** (the ceiling) and
**`Committed_AS`** (how much has been promised). `Committed_AS` exceeding `CommitLimit`
under mode 0 or 1 is normal and not by itself a problem — it is the definition of
overcommitting.

**Why this matters for Python specifically.** CPython allocates in small pieces and
touches essentially everything it allocates, so it does not benefit from overcommit the
way a sparse-array scientific code does. What it *does* do is fork, and mode 2 accounts
the child's full private-copy potential at fork time — which is why mode 2 plus a large
pre-forking server is a classic way to get `ENOMEM` from `fork()` on a machine with
plenty of free RAM.

---

## 9. The OOM killer

> **Researched, not measured.** Sources: `proc_pid_oom_score_adj(5)`, the cgroup-v2
> kernel documentation, Gregg's *Systems Performance* 2e ch. 7.

When the bet in §8 goes bad and reclaim (§12) cannot free enough, the kernel picks a
process and kills it. Two distinct paths, and conflating them is the single most common
error in production post-mortems.

### 9.1 The global OOM killer

Fires when the *system* is out of memory. It scores every candidate task with a "badness"
heuristic. Per `proc_pid_oom_score_adj(5)`:

> The badness heuristic assigns a value to each candidate task ranging from 0 (never
> kill) to 1000 (always kill)… The units are roughly a proportion along that range of
> allowed memory the process may allocate from, based on an estimation of its current
> memory and swap use. For example, if a task is using all allowed memory, its badness
> score will be 1000. If it is using half of its allowed memory, its score will be 500.

Two details worth memorising:

- **Root processes get a 3% memory bonus** — an explicit thumb on the scale.
- **`/proc/PID/oom_score_adj` ranges from −1000 to +1000** and is *added* to the score.
  **−1000 (`OOM_SCORE_ADJ_MIN`) disables OOM-killing for that task entirely**, because it
  will always report a badness of 0. The man page notes this makes it "very simple for
  user space to define the amount of memory to consider for each task": setting +500
  means the task is treated as if it were using 50% more memory than it is.

The scoring is **proportional to memory used**, which is why the OOM killer reliably
kills your largest, most important process — a database, or the JVM, or the Python worker
holding the model — rather than the thing that actually caused the pressure. That is the
motivation for `oom_score_adj` and for cgroup limits.

### 9.2 The cgroup-v2 memory controller — what actually kills your container

This is the one that matters in 2026, because your Python service is in a container and
the container has a memory limit. The kernel documentation defines a four-level scheme:

| Knob | Semantics (per kernel docs) |
|---|---|
| `memory.min` | **Hard protection.** Memory within this boundary "won't be reclaimed under any conditions. If there is no unprotected reclaimable memory available, OOM killer is invoked." |
| `memory.low` | **Best-effort protection.** Not reclaimed "unless there is no reclaimable memory available in unprotected cgroups." |
| `memory.high` | **Throttle.** Exceeding it does not kill; processes are "throttled and routed to perform direct memory reclaim". Your service gets *slow*, not dead. |
| `memory.max` | **Hard limit.** Exceed it and reclaim fails → the cgroup OOM killer fires. |

Three practical consequences:

1. **`memory.high` is the knob you want and almost nobody sets.** It converts a hard kill
   into backpressure you can observe and alert on, via the `high` counter in
   `memory.events` — "the number of times processes of the cgroup are throttled and routed
   to perform direct memory reclaim". A Python service that has gone quiet and slow with
   a rising `high` count is a memory problem, not a CPU problem, and nothing in a CPU
   profile will tell you that.
2. **`memory.oom.group`** kills the *entire* cgroup as a unit rather than one process.
   Without it, a pre-forking server loses one worker, the supervisor restarts it, it
   reloads the model, and you get an OOM loop that looks like a crash loop.
3. **`memory.current` and `memory.peak`** are the numbers the limit is enforced against —
   not RSS, and **they include page cache**. A container doing heavy file I/O can be
   OOM-killed on page cache it does not think it owns.

`memory.events`' `oom_kill` counter is the ground truth for "were we OOM-killed", and it
is the field to put on a dashboard. `dmesg` will also carry the kernel's kill message
with the badness scores of every candidate — the single most useful artifact in an OOM
post-mortem.

### 9.3 The connection back to §7

Now put §7 and §9.2 together, because this is the exit-exam question from the README's
Tier 8 checklist ("container gets OOM-killed with 8 gunicorn workers but not 4, at the
same total traffic"):

Eight workers do not use 2× the memory of four. They use 2× the *privatised* memory, and
§7 measured privatisation running to **88% of the parent heap per worker** once each
worker has served enough traffic to touch the whole graph. Four workers might land under
`memory.max`; eight will not. The traffic is identical; the memory is not, because
memory scales with **workers × pages touched**, not with request rate.

---

## 10. RSS vs VSZ vs PSS vs USS

The README asks for this one precisely, so here it is precisely.

| Metric | Definition | Sums to something meaningful across processes? |
|---|---|---|
| **VSZ** | Total address space with a mapping. Includes never-touched, file-backed-not-resident, and guard regions. | **No.** Meaningless. |
| **RSS** | Pages currently resident, **counted in full in every process that maps them**. | **No** — double-counts shared pages. |
| **PSS** | Proportional Set Size: each page's cost divided by the number of processes mapping it. | **Yes.** This is the one that sums. |
| **USS** | Unique Set Size: only pages mapped by this process alone. What you'd get back by killing it. | Yes, but under-counts. |

**VSZ is noise.** Measured on this machine, a do-nothing CPython 3.14.6 process reports
**425 GB of VSZ** *(measured)* — address-space reservations, guard regions, and the
shared library cache. Anyone alerting on VSZ is alerting on nothing. Even on Linux, where
the number is far smaller, it counts `MAP_NORESERVE` regions and every untouched
allocation from §4.

**RSS is the honest default and the one that double-counts.** Its specific failure: fork
8 workers off a 1 GB parent before any COW breakage, and each reports ~1 GB RSS. Summing
gives 8 GB; the true cost is ~1 GB. Any dashboard that adds up per-process RSS on a
forking server is overstating memory, sometimes by the worker count.

**PSS is what fixes that**, and it is why it exists. On Linux each page is charged
`1/N` to each of the N processes mapping it, so the parent and its 8 children report
~111 MB each and the sum is the truth. Per `proc_pid_smaps(5)`, every mapping in
`/proc/PID/smaps` carries the full breakdown:

```
00400000-0048a000 r-xp 00000000 fd:03 960637      /bin/bash
Size:                552 kB     <- VSZ contribution
Rss:                 460 kB     <- resident, full charge
Pss:                 100 kB     <- resident, divided by sharers
Shared_Clean:        452 kB
Shared_Dirty:          0 kB
Private_Clean:         8 kB
Private_Dirty:         0 kB     <- USS = Private_Clean + Private_Dirty
Swap:                  0 kB
```

`/proc/PID/smaps_rollup` gives the same fields pre-summed for the whole process, which is
what you actually want to scrape — parsing full `smaps` on a process with thousands of
mappings is itself expensive.

**The field to watch for §7's problem is `Private_Dirty`.** Anonymous, private, modified
— that is precisely "pages this worker copied away from its parent". A pre-forking Python
server whose per-worker `Private_Dirty` climbs steadily after startup while total
allocation is flat is showing you refcount COW breakage and nothing else. It is the
single most diagnostic number in this entire document, and it is why doc 16 §5.1 names it
specifically.

**Which one does the OOM killer care about?** Neither, strictly. §9.1's badness heuristic
works from an estimate of the task's memory *and swap* usage — closer to RSS than to PSS,
which is part of why it mis-targets forked workers. The cgroup limit that actually kills
your container (§9.2) is enforced against `memory.current`, which is a **cgroup-wide**
charge that counts each page once, no matter how many processes map it, and includes page
cache. So: **your dashboard shows RSS, the global killer approximates RSS, and the thing
that actually kills you counts something closer to PSS plus page cache.** Three different
numbers, which is exactly why the question is asked.

> **A brief aside on this platform.** macOS has no `smaps`, and therefore no PSS or USS.
> Its nearest analogue is `phys_footprint` — the number it enforces memory limits
> against — which counts a process's own dirty and compressed anonymous pages and
> notably **excludes clean file-backed pages**. §3.1's file-backed sweep shows the
> difference starkly *(measured)*: RSS +256.03 MB, footprint +0.13 MB for the same
> 16,384 resident pages. It is a genuinely better default than RSS for "what would I get
> back", but it is not PSS and it does not sum across processes either.

---

## 11. Transparent huge pages — and why databases turn them off

> **Researched, not measured.** Source: the kernel's
> `Documentation/admin-guide/mm/transhuge.rst` and LWN's LSFMM coverage.

§2 showed that a PMD entry with its size bit set maps 2 MB directly, skipping the PTE
level. THP makes the kernel do this automatically, without the application asking. The
payoff is real and it is mostly about the TLB: one entry covering 2 MB instead of 4 KB is
**512× the reach per entry**, which for a large working set can be the difference between
a workload that fits in TLB reach and one that page-walks constantly (doc 01 §8).

The controls, per the kernel docs, are per-size and set through sysfs:

```
echo always  >/sys/kernel/mm/transparent_hugepage/enabled   # everywhere
echo madvise >/sys/kernel/mm/transparent_hugepage/enabled   # only MADV_HUGEPAGE regions
echo never   >/sys/kernel/mm/transparent_hugepage/enabled   # off
```

Modern kernels support multiple THP sizes with an `inherit` setting per size; by default
PMD-sized hugepages are `inherit` and all other sizes are `never`. A trap worth knowing:
the documentation states plainly that setting "never" everywhere does **not** disable THP
globally, because `madvise(MADV_COLLAPSE)` "ignores these settings and collapses ranges
to PMD-sized huge pages unconditionally".

**`khugepaged`** is the background daemon that retroactively collapses eligible 4 KB
ranges into huge pages. Its knobs — `pages_to_scan`, `scan_sleep_millisecs`,
`alloc_sleep_millisecs` — control how hard it works; the docs note you can set
`scan_sleep_millisecs` to 0 to "run khugepaged at 100% utilization of one core", which is
a sentence that should tell you something about the failure mode.

### Why databases disable it

The standard advice from PostgreSQL, MongoDB, Redis, Oracle and Couchbase is to set THP
to `madvise` or `never`. Three distinct reasons, and they are worth separating:

1. **Allocation-time latency.** A huge page needs 2 MB of *physically contiguous* free
   memory. On a fragmented machine the kernel may run **direct compaction** — moving
   pages around — synchronously, in the faulting process's context. LWN's coverage of the
   2015 LSFMM discussion records Vlastimil Babka's framing exactly: the compaction work
   "can create significant latencies for the faulting process. The cost can, in fact,
   outweigh the performance benefits of using huge pages in the first place." A tail
   latency spike of tens of milliseconds inside what your code thinks is a memory write
   is invisible to every application-level profiler.
2. **Internal fragmentation / memory bloat.** Touching one byte faults in 2 MB. For an
   allocator with sparse, scattered access patterns this inflates RSS badly. The same LWN
   piece notes this happening specifically with jemalloc. Recent kernels mitigate it with
   `shrink_underused`, which splits THPs whose zero-filled page count exceeds
   `max_ptes_none` back into small pages under pressure — an admission that the problem
   is real.
3. **COW amplification — and this is the Python-specific one.** §6's COW fault copies
   *one page*. If that page is a 2 MB THP, the fault copies **2 MB**. Now re-read §7: a
   forked CPython worker whose refcount writes are scattered across a large object graph
   will privatise memory in 2 MB units instead of 4 KB units. THP can therefore make the
   pre-forking-server COW problem dramatically *worse*, and it is the reason the Instagram-style
   `gc.freeze()` advice sometimes appears not to work — the granularity of the damage
   changed underneath it.

**The rule.** `madvise` is the right default for a general-purpose server: it lets
software that has proven it benefits (a JVM heap, a database buffer pool) opt in, and
leaves everything else on 4 KB pages. `always` is a bet that your allocation pattern is
dense and your machine is unfragmented — a bet a long-lived Python service with a forking
worker model should not take.

---

## 12. Swap, reclaim, and pressure stall information

> **Researched, not measured**, except where noted.

When free memory runs low the kernel reclaims. The kernel's concepts overview divides
pages into **reclaimable** (page cache and anonymous memory — recoverable because the
data exists elsewhere or can be written to swap) and **unreclaimable** (kernel structures,
DMA buffers, pinned pages). Reclaim runs asynchronously via `kswapd` when watermarks are
crossed, or **synchronously in the allocating process's own context** — "direct reclaim" —
when things are worse. Direct reclaim is a latency event charged to whoever happened to
allocate.

**`vm.swappiness`** biases the choice between evicting page cache and swapping anonymous
memory. The kernel documentation now frames it as a *relative IO cost*, from 0 to 200,
default **60**: "At 100, the VM assumes equal IO cost and will thus apply memory pressure
to the page cache and swap-backed pages equally; lower values signify more expensive swap
IO." Notably the docs endorse values **above 100** for zram/zswap setups where swap is
faster than the filesystem, with a worked formula. The old folklore "set swappiness to 0
on a database server" predates that reframing; at 0 the kernel "will not initiate swap
until the amount of free and file-backed pages is less than the high watermark".

**Swapping a Python process is uniquely bad**, and §3.1 has the number: a major fault
costs **15.9 µs** against a warm access's **12 ns** *(measured)*. CPython's access
pattern is pointer chasing across a large object graph (doc 01 §10), which is close to
worst-case for locality — a GC pass alone will touch every tracked container. A Python
service that has begun to swap does not degrade gracefully; it falls off a cliff, because
the collector's traversal converts "some of the heap is on disk" into "all of the heap
gets read back, repeatedly".

> **On this platform.** macOS does not swap in the classic sense by default; it
> **compresses** pages in RAM first, and only writes compressed segments out under
> continued pressure. `vm_stat` reports it directly, and on this machine right now
> *(measured)*: 1,078,054 pages stored in the compressor occupying 445,761 pages —
> a **2.42× compression ratio**, holding ~16.4 GB of nominal pages in ~6.8 GB. Linux's
> equivalent, opted into rather than default, is zswap/zram — which is exactly the case
> the swappiness docs say to set values above 100 for.

### Pressure stall information

PSI is the best memory-health signal Linux exposes and it is badly underused. Per the
kernel's `Documentation/accounting/psi.rst`, `/proc/pressure/memory` reports:

```
some avg10=0.00 avg60=0.00 avg300=0.00 total=0
full avg10=0.00 avg60=0.00 avg300=0.00 total=0
```

The distinction between the two lines is the entire value of the interface:

- **`some`** — "the share of time in which **at least some** tasks are stalled on a given
  resource." Work is still getting done. This is an early warning.
- **`full`** — "the share of time in which **all** non-idle tasks are stalled…
  simultaneously. In this state actual CPU cycles are going to waste, and a workload that
  spends extended time in this state is considered to be **thrashing**."

Averages are over 10, 60, and 300 second windows, plus a `total` in microseconds. The
docs specifically recommend `total` for "detection of latency spikes which wouldn't
necessarily make a dent in the time averages" — the right field for alerting.

**Why this beats every other memory metric for alerting.** Free memory near zero is
*normal* — a healthy Linux box uses all of it for page cache. RSS climbing is normal.
Swap-in-use is ambiguous. But `memory.full` above zero for a sustained window means your
processes are stopped, waiting on memory, doing nothing. It is a direct measure of harm
rather than a proxy for it. The same files exist per-cgroup, so you can attribute the
stall to one container. If you take one operational thing from this document, make it
this: **alert on cgroup `memory` PSI `full avg60`, not on RSS.**

---

## 13. `madvise` and giving memory back

`madvise(2)` tells the kernel how you intend to use a range. Most values are pure hints
with no semantic effect, but the two that matter change what your program can observe.

Per the Linux man page:

- **`MADV_DONTNEED`** — the kernel may free the pages immediately. The man page flags it
  as the one conventional advice value whose semantics *do* differ from POSIX, and the
  consequence is the important part: for private anonymous mappings, **subsequent
  accesses succeed but see zeroes**. Your data is gone. This is how an allocator returns
  memory to the OS without giving up the address range.
- **`MADV_FREE`** (Linux 4.5+) — lazier and cheaper. "The kernel can thus free these
  pages, but the freeing could be delayed until memory pressure occurs. For each of the
  pages that has been marked to be freed but has not yet been freed, the free operation
  will be **canceled if the caller writes into the page**." So RSS may not drop at all
  until the system actually needs memory. This is what modern allocators (jemalloc,
  mimalloc) prefer, because it avoids the fault-back-in cost when the memory is reused
  quickly — and it is a common source of "my allocator says it freed it but RSS is
  unchanged" confusion that is *not* a bug.

### What happened when I tried it here

CPython's `mmap` object exposes `madvise()`, and this platform offers `MADV_DONTNEED`,
`MADV_FREE`, `MADV_FREE_REUSABLE` and `MADV_FREE_REUSE` *(measured)*. Applying each to a
freshly-touched 256 MiB anonymous private mapping *(measured)*:

```
MADV_DONTNEED        touched +  256.03 MB   after madvise  256.03 MB   dropped 0.0%   data survived: True
MADV_FREE            touched +  256.00 MB   after madvise  256.00 MB   dropped 0.0%   data survived: True
MADV_FREE_REUSABLE   touched +  256.00 MB   after madvise  256.00 MB   dropped 0.0%   data survived: True
SHARED+DONTNEED      touched +  256.00 MB   after madvise  256.00 MB   dropped 0.0%   data survived: True
```

**None of them moved resident size, and the data survived in every case** — including
`MADV_DONTNEED`, where the Linux documentation says reads should return zeroes. This is a
clean negative result and it is a real portability lesson rather than a curiosity:
`madvise` is *advice*, the standard permits ignoring it, and code that relies on
`MADV_DONTNEED` actually reducing RSS is relying on Linux-specific behaviour. I could not
reproduce the documented Linux semantics here and have not verified them myself — see
§19.

---

## 14. Why freed memory does not come back, measured

The question every Python service owner eventually asks. §14 is the measurement that
settles it.

An arena is returned to the OS only when **every** pool inside it is free
([`16-object-memory-layout.md`](16-object-memory-layout.md) §3: 1 MiB arena, 64 pools of
16 KB). One surviving object pins the whole megabyte. To show that the *placement* of
survivors matters more than their *number*, four arms — identical object count, identical
survivor count, one process each *(measured)*:

Allocate 2,000,000 tuples (**105.5 bytes each**, +211 MB), then free 99% of them:

| Arm | Survivors | RSS retained after `gc.collect()` | % of peak retained |
|---|---|---|---|
| free nothing | 2,000,000 | 211.0 MB | 100.0% |
| **keep every 100th** (scattered) | **20,000** | **211.0 MB** | **100.0%** |
| **keep the first 1%** (contiguous) | **20,000** | **22.3 MB** | **10.6%** |
| free everything | 0 | 20.3 MB | 9.6% |

**The two middle rows are the entire lesson.** Same program, same 2 million objects, same
20,000 survivors — **211 MB versus 22 MB, a 9.5× difference, decided purely by where the
survivors landed.** Scattered survivors pin every arena they touch; contiguous survivors
pin a handful and the rest go back.

```
  After the load:              Scattered survivors:         Contiguous survivors:
  ┌───────────────┐            ┌───────────────┐            ┌───────────────┐
  │███████████████│            │█..............│ 1 MB held  │███████████████│ 1 MB held
  │███████████████│            │....█..........│ 1 MB held  │███░░░░░░░░░░░░│ partly
  │███████████████│            │.........█.....│ 1 MB held  │░░░░░░░░░░░░░░░│ RETURNED
  │███████████████│            │..............█│ 1 MB held  │░░░░░░░░░░░░░░░│ RETURNED
  └───────────────┘            └───────────────┘            └───────────────┘
     211 MB peak                 211 MB retained              22 MB retained
                                 20,000 objects alive         20,000 objects alive
```

This is **fragmentation**, not a leak, and the distinction is the whole diagnostic skill:
a leak is unreachable-but-uncollected memory; this is memory correctly freed into arenas
that cannot be released. `gc.collect()` cannot help — there is nothing to collect.
Restarting the worker is the only cure, which is why `--max-requests` exists in gunicorn.

So, the README's question — **"you `free()` 1 GB and RSS doesn't drop, give two distinct
mechanisms"** — has at least four good answers, and the strongest response names them
with their signatures:

1. **Allocator arena retention / fragmentation** (measured above). Signature: RSS flat
   after a large delete; `sys._debugmallocstats()` shows many arenas with few used pools.
2. **Free lists.** CPython keeps per-type free lists (including one *per tuple length*),
   so the objects are destroyed but their memory is parked for reuse. Doc 16 §5.
3. **The system allocator's own retention.** Above 512 bytes you are in `malloc`, which
   has its own policy about returning to the OS — glibc's `M_TRIM_THRESHOLD`, or
   jemalloc/mimalloc deliberately holding memory with `MADV_FREE` (§13) rather than
   giving it back. [`08-allocators.md`](08-allocators.md).
4. **`MADV_FREE` semantics specifically** (§13): the allocator genuinely released it, the
   kernel genuinely accepted, and RSS still will not drop until there is pressure.

None of these is a bug. All four are the system working as designed.

---

## 15. Attributing a Python process's memory

Now assemble everything into the diagnosis you will actually have to perform: *this
process is using 2 GB, where did it go?*

### 15.1 The gap, measured

`tracemalloc` is the standard first reach, and it answers a narrower question than people
think: it counts **bytes requested through CPython's allocators**, tagged by traceback.
It does not count allocator rounding, pool and arena overhead, memory below the C level,
or anything a C extension allocated with plain `malloc`.

Allocating 1,500,000 two-tuples *(measured)*:

```
tracemalloc current     :   204,012,000  (194.56 MB)
RSS delta               :   665,239,552  (634.42 MB)
RSS / tracemalloc       :          3.26x
```

**3.26×.** And `tracemalloc` is itself an observer with a cost: stopping it released
107 MB, so the honest ratio for the data alone is **527 MB resident against 194 MB
attributed — still 2.71×.** Freeing everything left 352 MB resident (§14 again).

**This is the arithmetic behind "the container was OOM-killed at 2 GB but `tracemalloc`
says 600 MB".** Nobody is lying. They are measuring different things: `tracemalloc`
measures your program's requests, the cgroup measures the kernel's pages. Everything in
§14 lives in the gap, and doc 32's point applies — a profiler that perturbs the heap by
20% is not measuring your program.

### 15.2 The ladder

Work down it; each rung answers a question the one above cannot.

| Rung | Tool | Answers |
|---|---|---|
| 1 | cgroup `memory.current`, `memory.peak`, `memory.events` | Are we near the limit? Have we been killed before? |
| 2 | PSI `memory` `full avg60` (§12) | Is this *hurting*, or just large? |
| 3 | `smaps_rollup`: `Private_Dirty` vs `Shared_Clean` (§10) | Is it our own dirty memory, or COW breakage (§7), or file cache? |
| 4 | `ru_minflt` / `ru_majflt` growth (§3) | Are we faulting steadily? Are we *swapping*? |
| 5 | `sys._debugmallocstats()` — arenas allocated vs pools in use | Fragmentation (§14) or genuine live data? |
| 6 | `tracemalloc` / `memray` snapshots, diffed | Which Python code requested it? |
| 7 | `gc.get_objects()` counts by type, sampled over time | Which objects are accumulating? Leak or cache? |

The ordering is deliberate: **rungs 1–5 are cheap and tell you which *kind* of problem you
have; rung 6 is expensive and only worth reaching for once you know the answer is
"Python actually allocated it".** Most people start at rung 6, find nothing, and conclude
there is no problem.

### 15.3 The four shapes, and their signatures

| Shape | RSS | `tracemalloc` | `Private_Dirty` | Fix |
|---|---|---|---|---|
| **Leak** (unbounded live set) | rises forever | rises with it | rises | find the reference |
| **Fragmentation** (§14) | rises, plateaus high | flat/low | flat | restart workers; reduce churn |
| **Unbounded cache** | rises, plateaus | rises with it | rises | bound the cache |
| **COW breakage** (§7) | rises **per worker** after fork | **flat** | **rises** | §7's list |

The bottom row is the one this document exists to make diagnosable. `tracemalloc` is
**flat** — the worker allocated nothing — while RSS climbs steadily. Every other tool
says the program is innocent, and it is: the memory is being consumed by the hardware
faulting on reference-count writes to pages it inherited. Without §7 you cannot even
form the hypothesis.

---

## 16. The cost model

Everything above, as numbers to reason with. Measured on this machine unless the row says
otherwise.

| Operation | Cost | Source |
|---|---|---|
| Warm access to a resident page | **12 ns** | measured, §3.1 |
| TLB miss → page walk | 3 accesses here (16 KB granule, 47-bit VA); 4 on 4 KB x86-64 | derived, §2 |
| Minor fault (zero-fill) | **~0.5–0.6 µs** | measured, §3.1 |
| Minor fault (copy-on-write) | **~0.5–0.6 µs** | measured, §3.1 |
| Major fault (disk/page cache miss) | **15.9 µs** | measured, §3.1 |
| `mmap` 1 GiB, untouched | 3 pages of RSS | measured, §4 |
| RSS per single-byte first touch | **16,384 B** (4,096 on 4 KB pages) | measured, §4 |
| Page tables | ~0.18% of footprint here; ~0.7% at 4 KB | measured, §2 |
| A CPython object, all-in | ~105 B for a 1-tuple | measured, §14 |
| Forked child reading a shared graph | **88% of parent heap privatised** | measured, §7 |
| `gc.freeze()` on GC-traversal COW | **295× reduction** | measured, §7 |
| `gc.freeze()` on refcount COW | **no effect** | measured, §7 |
| Scattered vs contiguous survivors | **9.5× RSS**, same object count | measured, §14 |
| `tracemalloc` vs RSS | **2.7–3.3×** understated | measured, §15 |

**Four sentences to remember:**

1. **Allocation is free; residency is expensive; the first touch is where you pay.**
2. **A read in Python is a write in hardware**, and that single fact explains the entire
   pre-forking memory story.
3. **RSS double-counts sharing, PSS does not, and the thing that kills your container
   counts a third thing.**
4. **Freed is not returned** — and whether it comes back is decided by where the
   survivors landed, not how many there are.

---

## 17. Lab exercises

**1 — Confirm your page size from first principles.** Redo §4 on your own machine: map
1 GiB, touch one byte per page, divide the RSS delta by the touch count. You should get
your page size to within a rounding error. *Proves §4 — and it is the fastest way to
discover you are on a 16 KB-page machine when you assumed 4 KB.*

**2 — Build the fault-cost table.** Reproduce §3.1's three rows. Take medians of ≥5
passes and report spread. **Then deliberately break it**: call `subprocess.run(["true"])`
between timed sweeps and watch the warm number inflate by 50×. *Proves §3.3 — the most
transferable methodology lesson here.*

**3 — Measure the fork tax on your own object graph.** Run §7's four arms against a
structure your service actually loads. Report privatised MB per worker and multiply by
your worker count. *Proves §7 — and it is the single highest-value measurement in this
document for anyone running gunicorn or uWSGI.*

**4 — Prove `gc.freeze()`'s limits.** Run the `gc.collect()`-only arm and the read-only
arm with and without freeze. Confirm for yourself that freeze does everything for one and
nothing for the other. *Proves §7's contradiction of the folklore — do not take my word
for it.*

**5 — Reproduce the fragmentation cliff.** Run §14's scattered vs contiguous arms. Then
add a fifth arm: keep every 1000th object. Where does retention fall off? *Proves §14 and
builds intuition for how few survivors it takes to pin an arena.*

**6 — Watch a major fault happen.** Write a file larger than your free RAM, `mmap` it,
sweep it, and watch `ru_majflt` and your wall clock. Then sweep it again warm. *Proves
§3.1's 24.7× and makes "the page cache" concrete.*

**7 — Read your own `smaps_rollup`** (Linux). Fork a worker off a large parent, then diff
`Private_Dirty` before and after the worker serves 1,000 requests. *Proves §10 — this is
the number to put on a dashboard.*

**8 — Alert on the right thing.** Set `memory.high` below `memory.max` on a container,
drive it into throttling, and watch `memory.events`' `high` counter and PSI
`memory full avg10` move while RSS looks unremarkable. *Proves §9.2 and §12.*

---

## 18. Question bank

Staff-level. The section to reread is noted.

1. Precisely: RSS vs VSZ vs PSS vs USS. Which sums correctly across processes, and which one does the thing that kills your container actually enforce against? *(§10)*
2. You `free()` 1 GB and RSS doesn't drop. Give two distinct mechanisms — then give four. *(§14)*
3. Why does writing one byte increase RSS by 16 KB, and what would that number be on x86-64 Linux? *(§4)*
4. Distinguish a minor from a major fault by cost, cause, and cure. Roughly what's the ratio? *(§3)*
5. A page fault is not an error. When *is* it an error? *(§3.2)*
6. `fork()` a 10 GB parent. How long does it take, and what did the kernel actually copy? *(§6)*
7. Your forked worker only *reads* the shared model. Why does its RSS climb to nearly the parent's heap size? *(§7)*
8. Your colleague adds `gc.freeze()` before forking and RSS is unchanged. Were they wrong to add it? What did it fix, and what did it not? *(§7)*
9. What does `MAP_PRIVATE` on a file mean for your writes? Where does this mechanism show up in every process on the machine? *(§5)*
10. Why is `MAP_FIXED` dangerous even when the address looks free, and what's the fix? *(§5)*
11. `vm.overcommit_memory=2` sounds strictly safer than the default. Give two reasons people revert it. *(§8)*
12. Explain the OOM badness heuristic. What does `oom_score_adj=-1000` do, and why is the killer biased toward your most important process? *(§9.1)*
13. `memory.high` vs `memory.max`: which produces a page, which produces an incident, and which do most teams fail to set? *(§9.2)*
14. Container OOM-kills with 8 workers but not 4, at identical traffic. Walk the diagnosis. *(§7, §9.3, §10)*
15. Give three reasons a database vendor tells you to disable THP. Which one is worst for a forking Python server? *(§11)*
16. Why is PSI `full` a better alert than free memory, RSS, or swap-in-use? *(§12)*
17. What is the difference between `MADV_DONTNEED` and `MADV_FREE`, and why might an allocator prefer the one that doesn't reduce RSS? *(§13)*
18. Same object count, same survivor count, 9.5× difference in retained RSS. Explain. *(§14)*
19. `tracemalloc` reports 600 MB; the cgroup killed you at 2 GB. Who is lying? *(§15.1)*
20. Distinguish a leak, fragmentation, an unbounded cache, and COW breakage — by the signature each leaves across RSS, `tracemalloc`, and `Private_Dirty`. *(§15.3)*
21. Why does a swapping Python process degrade so much worse than a swapping C program? *(§12)*
22. How many memory accesses does a TLB miss cost on 4 KB x86-64? On this machine? Why the difference? *(§2)*
23. You need to measure page-fault cost. Name three things that will silently corrupt the benchmark. *(§3.3)*
24. Page tables are memory too. When does that stop being a rounding error? *(§2)*

---

## 19. What I could not verify

This section is deliberately long, because the gap between what I measured and what I
researched is large and specific.

**Everything in §8, §9, §11, §12's PSI subsection, and §10's PSS/USS material is
researched from primary sources and was not measured.** This machine is macOS on arm64;
it has no `vm.overcommit_memory`, no Linux OOM killer or `oom_score_adj`, no THP or
`khugepaged`, no `/proc/pressure/*`, and no `/proc/PID/smaps` — and therefore no PSS or
USS at all. I have quoted the kernel documentation and man pages directly and attributed
inline rather than paraphrasing numbers into something that might read as measured.

Specific items I flagged in place:

1. **The arm64 page-table level count (§2).** I derived "three levels for user addresses"
   from the 16 KB granule arithmetic (11 index bits/level) plus the *measured*
   `machdep.virtual_address_size = 47`. I did **not** read `TCR_EL1.T0SZ` or `TG0`, which
   are not accessible from user space, and I did not confirm whether XNU uses a different
   configuration for some mappings. If the kernel runs a 48-bit T0SZ with a 2-entry L0,
   the walk is four levels and my §2 sidebar is wrong in a way that would not change any
   measurement in this document.

2. **The 1.96 faults-per-privatised-page discrepancy (§7).** The read-only child took
   19,245 minor faults but privatised only 9,835 pages' worth of RSS. I offered three
   candidate explanations and confirmed none of them. This is the single loosest end here.

3. **`madvise` did nothing measurable (§13).** All four advice values left RSS unchanged
   and left the data intact, including `MADV_DONTNEED`. The Linux man page's documented
   behaviour — subsequent reads of a private anonymous mapping returning zeroes — is
   quoted from the man page and **was not observed and could not be tested here.** I do
   not know whether this platform ignores the advice entirely, defers it, or accounts it
   somewhere `proc_pid_rusage` does not show.

4. **Timing spread is wider than I would like.** Two full runs of the §3.1 benchmark gave
   zero-fill fault costs of 626.8 ns and 532.2 ns (an 18% swing) and COW costs of 611.2 ns
   and 460.5 ns (a 33% swing) — on a machine at load average ~2.2 with 2.2 GB already
   swapped. I report these as "~0.5–0.6 µs" for that reason. The *ratios* (40–50× a warm
   access; 24.7× between major and minor) were stable across runs and are the part I would
   defend.

5. **The `tracemalloc` ratio is workload-specific.** §15.1's 2.71–3.26× is for 1.5 M small
   tuples built by a list comprehension, which includes list-resize churn. A different
   allocation pattern will give a different ratio. The *direction* and the *reason* are
   general; the multiplier is not.

6. **`gc.freeze()`'s 295× is one workload, one object graph.** It is the ratio for a child
   that runs `gc.collect()` and nothing else. In any realistic worker the traversal
   happens too, and the combined arm measured only ~3 MB of benefit out of ~157 MB. Do not
   quote 295× as what freeze will do for your service.

7. **I did not measure THP's effect on COW granularity (§11's third reason).** The
   mechanism follows directly from §6 and §11 — a COW fault copies whatever the mapping's
   page size is — but the claim that this materially worsens forked CPython workers is
   *reasoned*, not measured, and it deserves a benchmark on a Linux box with
   `always` vs `madvise`.

8. **No Linux figure in this document is mine.** Where I state a default (overcommit ratio
   50%, swappiness 60, badness range 0–1000, `oom_score_adj` −1000..+1000, PSI's 10/60/300
   second windows) it is from the kernel docs or man pages cited in §20, current as of the
   7.2-rc kernel documentation tree I read.

---

## 20. Sources

**Primary — kernel documentation** (read directly; current as of the 7.2-rc doc tree)

- [Overcommit Accounting](https://docs.kernel.org/mm/overcommit-accounting.html) — *Verdict:* the only authoritative statement of the three modes; two screens long and settles every argument about §8.
- [Documentation for /proc/sys/vm/](https://www.kernel.org/doc/html/latest/admin-guide/sysctl/vm.html) — *Verdict:* the reference for every VM sysctl; the `swappiness` entry has been rewritten around relative IO cost and invalidates a lot of blog advice.
- [Transparent Hugepage Support](https://docs.kernel.org/admin-guide/mm/transhuge.html) — *Verdict:* essential for §11; the "never does not mean never" note about `MADV_COLLAPSE` is the kind of thing only the primary source tells you.
- [PSI — Pressure Stall Information](https://docs.kernel.org/accounting/psi.html) — *Verdict:* short, and the `some` vs `full` distinction is worth more than any dashboard you currently have.
- [Concepts overview (mm)](https://docs.kernel.org/admin-guide/mm/concepts.html) — *Verdict:* the best short orientation to reclaimable vs unreclaimable memory; start here if §12 moved too fast.
- [Control Group v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) — *Verdict:* the memory-controller section is the actual specification of what kills your container; long, but §"Memory Interface Files" is the part to read.
- [Kernel Samepage Merging](https://docs.kernel.org/admin-guide/mm/ksm.html) — *Verdict:* not used above, but the other answer to "many processes, similar memory"; relevant if you run many similar VMs or workers.

**Primary — man pages**

- [`mmap(2)`](https://man7.org/linux/man-pages/man2/mmap.2.html) — *Verdict:* the flag list is the ground truth for §5; the "Using MAP_FIXED safely" section should be mandatory reading before anyone uses it.
- [`madvise(2)`](https://man7.org/linux/man-pages/man2/madvise.2.html) — *Verdict:* settles the `MADV_DONTNEED` vs `MADV_FREE` semantics precisely, which almost no secondary source does.
- [`proc_pid_smaps(5)`](https://man7.org/linux/man-pages/man5/proc_pid_smaps.5.html) — *Verdict:* the field-by-field definition behind §10; `smaps_rollup` is the one to actually scrape.
- [`proc_pid_oom_score_adj(5)`](https://man7.org/linux/man-pages/man5/proc_pid_oom_score_adj.5.html) — *Verdict:* short and exact on the badness heuristic, including the 3% root bonus that surprises people.
- [`proc_meminfo(5)`](https://man7.org/linux/man-pages/man5/proc_meminfo.5.html) — *Verdict:* the decoder ring for `CommitLimit`, `Committed_AS`, `AnonPages`, `Mapped`; consult, don't read.

**Books** (see [BOOKS.md](BOOKS.md) for the folder's full verdicts)

- **OSTEP**, Arpaci-Dusseau — *Verdict:* 📖 free, and the virtualization half (ch. 12–24) is the best prose explanation of everything in §1–§6. Read it before Kerrisk.
- **The Linux Programming Interface**, Kerrisk — *Verdict:* 🔍 reference. Ch. 49 (memory mappings) and 50 (virtual memory operations) are the chapters for this document; do not read it through.
- **Systems Performance** 2e, Gregg — *Verdict:* 📖 ch. 7 (Memory) is the operational counterpart to this document — the methodology for §15's ladder comes from here.
- **Understanding the Linux Virtual Memory Manager**, Mel Gorman — *Verdict:* 🆓 dated (2.4/2.6) and still the clearest walkthrough of VMAs and the fault path anywhere; ch. 4 is what §3's diagram is based on.

**Secondary**

- [LWN — Improving huge page handling (LSFMM 2015)](https://lwn.net/Articles/636162/) — *Verdict:* the source for §11's compaction-latency argument, in Vlastimil Babka's own framing; explains *why* the advice exists rather than repeating it.

**Sibling docs**

- [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §8 — TLB reach and 16 KB pages; the foundation §2 and §11 build on.
- [`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md) §10 — the fork tax and `gc.freeze()`; §7 here sharpens its ranking with measurements.
- [`16-object-memory-layout.md`](16-object-memory-layout.md) §3, §5, §5.1, §11 — arena/pool constants, the four retention mechanisms, and the `ru_maxrss` trap §7 avoids.
- [`22-garbage-collection.md`](22-garbage-collection.md) — what the GC traversal in §7 is actually doing to your pages.
- [`08-allocators.md`](08-allocators.md) — the layer between `mmap` and `PyObject`.
- [`31-measurement-methodology.md`](31-measurement-methodology.md) — the house rules §3.3 follows, including this machine's noise floor.

---

*Next: [`08-allocators.md`](08-allocators.md) — `brk` vs `mmap`, glibc's bins and tcache,
jemalloc and mimalloc, and the fragmentation §14 measured, explained from the allocator's
side of the boundary.*
