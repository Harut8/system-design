# 08 — Allocators: `brk`, `mmap`, and the four programs that stand between you and the kernel

> **Tier 1, doc 08.** Prerequisites: [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
> (cache lines, locality), [`06-processes-threads-scheduling.md`](06-processes-threads-scheduling.md)
> (threads, contention), [`07-virtual-memory.md`](07-virtual-memory.md) (pages, faults, RSS —
> **read that one first; this document assumes every word of it**).
> Feeds into: [`16-object-memory-layout.md`](16-object-memory-layout.md),
> [`24-the-gil.md`](24-the-gil.md), [`26-free-threading.md`](26-free-threading.md),
> [`35-memory-optimization.md`](35-memory-optimization.md),
> [`46-production-python.md`](46-production-python.md).
>
> **THESIS: `malloc` is not a system call, and that is the whole subject.** Between your
> `list.append` and the kernel's page tables sit two user-space programs — CPython's
> pymalloc and the C library's allocator — each with its own size classes, its own free
> lists, its own idea of when to hand memory back, and its own failure mode. Almost every
> "Python leaks memory" bug is one of these two programs deciding, correctly and by design,
> not to return memory that your process is no longer using.
>
> Doc 07 established that **freed is not returned**. This document explains *who* is doing
> the not-returning, *why* it is the right call, and *which knob* changes it.

> **Provenance.** Measurements were produced on the machine this repo lives on:
> **Apple M3 Pro (11 cores: 5 P + 6 E), 18 GiB RAM, macOS 26.5.2 / Darwin 25.5.0 arm64**,
> **16 KB pages**, 128-byte cache lines, 47-bit user virtual addresses. Interpreter:
> **CPython 3.14.6** (`~/.local/bin/python3.14`, `Py_GIL_DISABLED=0`). Numbers marked
> *(measured)* came out of a live process while this document was written; timings are
> **medians of ≥5 alternating passes with min/max spread reported**, per the house rules in
> [`31-measurement-methodology.md`](31-measurement-methodology.md). Load average was
> **1.92–2.06** throughout.
>
> **Source constants marked *(source)* were read directly from the primary source on
> 2 August 2026**, with file and line: the **glibc development tree at version 2.44.9000**
> (`malloc/malloc.c`, `malloc/arena.c`, `NEWS`) and **CPython `main`**
> (`Include/internal/pycore_obmalloc.h`, `Objects/obmalloc.c`). Where a constant differs
> between glibc releases, §5.2 says so — and it does differ, in a way the man page has not
> caught up with.
>
> **This machine does not run glibc.** Everything in §3–§6 is researched from glibc source
> and man pages and is *not* measured here. `PYTHONMALLOC=malloc` on this box selects
> Apple's `libmalloc`, not glibc's; §8 and §15 say so explicitly and are labelled as
> macOS numbers. Nothing researched is ever presented as a measurement.

## Contents

1. [The layer cake](#1-the-layer-cake)
2. [How a process gets memory from the kernel: `brk` vs `mmap`](#2-how-a-process-gets-memory-from-the-kernel-brk-vs-mmap)
3. [glibc malloc: the chunk](#3-glibc-malloc-the-chunk)
4. [glibc malloc: bins, tcache, and the allocation algorithm](#4-glibc-malloc-bins-tcache-and-the-allocation-algorithm)
5. [Arenas and heaps — and the RSS multiplication trap](#5-arenas-and-heaps--and-the-rss-multiplication-trap)
6. [Why freed memory does not come back](#6-why-freed-memory-does-not-come-back)
7. [Fragmentation: three kinds, one metric](#7-fragmentation-three-kinds-one-metric)
8. [Internal fragmentation, measured](#8-internal-fragmentation-measured)
9. [jemalloc: extents, decay, and fragmentation avoidance](#9-jemalloc-extents-decay-and-fragmentation-avoidance)
10. [tcmalloc: three tiers and a per-CPU cache](#10-tcmalloc-three-tiers-and-a-per-cpu-cache)
11. [mimalloc: free-list sharding](#11-mimalloc-free-list-sharding)
12. [Choosing and installing an allocator](#12-choosing-and-installing-an-allocator)
13. [Where CPython sits on all this](#13-where-cpython-sits-on-all-this)
14. [The 512-byte cliff, measured](#14-the-512-byte-cliff-measured)
15. [pymalloc vs the system allocator, measured](#15-pymalloc-vs-the-system-allocator-measured)
16. [mimalloc in the free-threaded build](#16-mimalloc-in-the-free-threaded-build)
17. [Observability: reading each allocator's mind](#17-observability-reading-each-allocators-mind)
18. [A diagnosis ladder](#18-a-diagnosis-ladder)
19. [The cost model](#19-the-cost-model)
20. [Lab exercises](#20-lab-exercises)
21. [Question bank](#21-question-bank)
22. [What I could not verify](#22-what-i-could-not-verify)
23. [Sources](#23-sources)

---

## 1. The layer cake

Write `x = [1, 2, 3]` and four allocators run, in this order, each one usually answering
without consulting the next:

```
  ┌──────────────────────────────────────────────────────────────────────┐
  │  your code            x = [1, 2, 3]                                  │
  ├──────────────────────────────────────────────────────────────────────┤
  │  CPython freelists    list/tuple/float/dict freelists, small ints,   │
  │                       interned strings — a hit here costs a pointer   │
  │                       swap and never reaches an allocator at all      │
  ├──────────────────────────────────────────────────────────────────────┤
  │  pymalloc             requests ≤ 512 B carved from 16 KiB pools       │
  │  (obmalloc.c)         inside 1 MiB arenas.  32 size classes, 16 B     │
  │                       apart.  Single-threaded-by-GIL.                 │
  ├──────────────────────────────────────────────────────────────────────┤
  │  libc malloc          everything > 512 B, plus pymalloc's arenas      │
  │  (glibc / libmalloc   themselves.  Bins, per-thread caches, arenas.   │
  │   / jemalloc / …)     Thread-safe, lock-based or lock-free.           │
  ├──────────────────────────────────────────────────────────────────────┤
  │  kernel               brk() / mmap() hand out *address space*.        │
  │                       Physical frames arrive later, on first touch.   │
  └──────────────────────────────────────────────────────────────────────┘
```

Four consequences fall straight out of the picture, and they organise the rest of this
document:

1. **A `free()` returns memory to layer 3, not to layer 4.** The C library keeps it. This
   is §6, and it is the single most misunderstood fact in the whole area.
2. **Each layer rounds up.** Four roundings compose, and the composed waste is what §7 and
   §8 call internal fragmentation.
3. **Each layer has a fast path and a slow path**, and the boundary between them is a
   *size*. Cross it and cost jumps discontinuously. §14 measures one such cliff at exactly
   512 bytes.
4. **Only layer 4 knows about pages, and only layer 4 can shrink your RSS.** Layers 2 and 3
   can only *ask*. Whether they ask, and when, is a policy decision baked into each
   allocator — and it is different for every one of them.

A useful reflex: when someone says "Python is using 4 GB", the first question is not *what
allocated it* but **which layer is holding it**. The answer changes the fix completely.
§18 turns that into a procedure.

---

## 2. How a process gets memory from the kernel: `brk` vs `mmap`

There are exactly two ways for a Unix process to acquire anonymous memory, and every
allocator in this document is a strategy for using them.

### 2.1 `brk` — one pointer, one direction

The historical interface. A process has a **program break**: the address just past the end
of its initialised data segment. `brk(addr)` sets it; `sbrk(increment)` moves it and
returns the old value.

The `brk(2)` man page is blunt about what this actually is:

> "`brk()` sets the end of the data segment to the value specified by *addr* … Increasing
> the program break has the effect of allocating memory to the process; decreasing the
> break deallocates memory."

The whole heap under `brk` is **one contiguous region that can only grow and shrink at one
end**. That is a stack discipline imposed on a data structure that is not a stack, and it
is the origin of the single most consequential allocator behaviour in this document:

> **If one live object sits near the top of the `brk` heap, nothing below it can be
> returned to the kernel — no matter how much of it is free.**

That is not a bug and not a tuning failure. It is arithmetic. `brk` is a single number.

`brk` is also, per its own man page, not a POSIX interface any more:

> "`brk()` and `sbrk()` are not defined in the POSIX.1 specification. However, several
> systems provide them… Avoid using `brk()` and `sbrk()`: the malloc(3) memory allocation
> package is the portable and comfortable way of allocating memory."

### 2.2 `mmap` — many regions, independently releasable

`mmap(NULL, len, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0)` asks for a fresh
region of address space anywhere the kernel likes. `munmap` gives *that region* back,
regardless of what is happening in any other region.

Doc 07 §5 covers `mmap`'s modes in detail. The allocator-relevant properties are three:

| Property | Consequence for an allocator |
|---|---|
| Regions are independent | Any region can be returned; no "top of heap" constraint |
| Regions are page-granular | A 100-byte `mmap` costs a whole page (16 KiB here, 4 KiB on x86-64) |
| Fresh anonymous pages are zeroed by the kernel | `mmap` is more expensive per byte than reusing a dirty chunk |

The `mallopt(3)` man page states the trade-off exactly, and it is worth quoting because it
is the reason the threshold in §2.3 exists at all:

> "Allocating memory using `mmap(2)` has the significant advantage that the allocated
> memory blocks can always be independently released back to the system. (By contrast, the
> heap can be trimmed only if memory is freed at the top end.) On the other hand, there are
> some disadvantages to the use of `mmap(2)`: deallocated space is not placed on the free
> list for reuse by later allocations; memory may be wasted because `mmap(2)` allocations
> must be page-aligned; and the kernel must perform the expensive task of zeroing out
> memory allocated via `mmap(2)`."

### 2.3 The mmap threshold, and why it moves

glibc picks per allocation. Requests at or above `M_MMAP_THRESHOLD` get their own `mmap`;
everything else comes out of the heap.

```c
/* glibc malloc/malloc.c:791, 800-802, 901  (source, 2.44.9000) */
#define DEFAULT_MMAP_THRESHOLD_MIN (128 * 1024)
#  define DEFAULT_MMAP_THRESHOLD_MAX (512 * 1024)              /* 32-bit */
#  define DEFAULT_MMAP_THRESHOLD_MAX (4 * 1024 * 1024 * sizeof(long))  /* 64-bit → 32 MiB */
#define DEFAULT_MMAP_THRESHOLD DEFAULT_MMAP_THRESHOLD_MIN
```

So: starts at **128 KiB**, ceiling **32 MiB** on 64-bit *(source)*.

The threshold is not a constant. From `mallopt(3)`:

> "*Note*: Nowadays, glibc uses a dynamic mmap threshold by default. The initial value of
> the threshold is 128\*1024, but when blocks larger than the current threshold and less
> than or equal to `DEFAULT_MMAP_THRESHOLD_MAX` are freed, the threshold is adjusted upward
> to the size of the freed block."

Read that carefully, because it is a self-modifying policy with a real failure mode:

> **An application that allocates and frees one 4 MiB buffer teaches glibc that 4 MiB
> allocations should come from the heap. From then on, they do — and they can no longer be
> returned individually.**

This is exactly how a program that "was fine" starts retaining hundreds of megabytes after
someone bumps a buffer size. Setting `M_MMAP_THRESHOLD` explicitly — via `mallopt()` or the
`MALLOC_MMAP_THRESHOLD_` environment variable — **freezes** the threshold and disables the
dynamic adjustment. That is often the entire fix.

```bash
# freeze the threshold: every allocation ≥ 128 KiB gets its own mapping, forever
MALLOC_MMAP_THRESHOLD_=131072 python app.py
```

The cost of freezing it low is real: more `mmap`/`munmap` syscalls, more kernel zeroing,
more page faults on every reuse. Doc 07 §3.1 measured a zero-fill minor fault at **~0.5–0.6
µs** on this machine. A 4 MiB buffer at 16 KiB pages is 256 pages, so **~130–150 µs of pure
fault cost every time you re-`mmap` it** — versus roughly zero for a heap chunk you already
own. If your service allocates a big buffer per request, freezing the threshold low can be
a serious regression. Measure both.

---

## 3. glibc malloc: the chunk

glibc's allocator (`ptmalloc2`, derived from Doug Lea's `dlmalloc`) is **chunk-oriented**.
The sourceware wiki's own description:

> "Glibc's malloc is chunk-oriented. It divides a large region of memory (a 'heap') into
> chunks of various sizes. Each chunk includes meta-data about how big it is (via a size
> field in the chunk header), and thus where the adjacent chunks are."

### 3.1 The boundary-tag layout

```
        ┌────────────────────────┐  ← chunk pointer (mchunkptr)
        │ prev_size  (8 B)       │  valid only if the PREVIOUS chunk is free;
        │                        │  otherwise the previous chunk's payload lives here
        ├────────────────────────┤
        │ size (8 B) | A M P     │  size, with 3 flag bits in the low bits
        ├────────────────────────┤  ← pointer returned to you (chunk2mem)
        │                        │
        │   your bytes           │  … and when free, this space becomes fd/bk pointers
        │                        │
        ├────────────────────────┤
        │ (next chunk's prev_size — yours to overwrite while allocated) │
        └────────────────────────┘
```

```c
/* glibc malloc/malloc.c:1107, 1110  (source) */
#define CHUNK_HDR_SZ (2 * SIZE_SZ)                       /* 16 on 64-bit */
#define chunk2mem(p) ((void*)((char*)(p) + CHUNK_HDR_SZ))
```

The three flag bits in the low bits of `size` are the reason the whole scheme works. From
the wiki:

- **`P` (`PREV_INUSE`, 0x1)** — the previous chunk is in use. Zero means the previous chunk
  is free and its `prev_size` field is valid, so this chunk can find and coalesce with it.
- **`M` (`IS_MMAPPED`, 0x2)** — this chunk came from its own `mmap` and is not in any heap.
- **`A` (`NON_MAIN_ARENA`, 0x4)** — this chunk belongs to a secondary arena (see §5).

That `P` bit is **the** enabler of O(1) coalescing. On `free`, the allocator can look
backwards (via `prev_size`, if `P` is clear) and forwards (via `size`) and merge with
adjacent free neighbours without searching anything. Boundary tags are the reason glibc
does not accumulate arbitrary external fragmentation from adjacent frees.

### 3.2 The overhead is 8 bytes, not 16

The header is 16 bytes but the *cost* is 8, because a chunk's payload is allowed to spill
into the next chunk's `prev_size` field while it is allocated. The rounding function makes
this explicit:

```c
/* glibc malloc/malloc.c:1136-1141  (source) */
static __always_inline size_t
checked_request2size (size_t req)
{
  if (__glibc_unlikely (req > PTRDIFF_MAX))
    return SIZE_MAX;
  return (req + SIZE_SZ + MALLOC_ALIGN_MASK < MINSIZE
	  ? MINSIZE
	  : (req + SIZE_SZ + MALLOC_ALIGN_MASK) & ~MALLOC_ALIGN_MASK);
}
```

With `SIZE_SZ = 8` and `MALLOC_ALIGNMENT = 16` on 64-bit: `chunk = round_up_16(req + 8)`,
floored at `MINSIZE`.

```c
/* glibc malloc/malloc.c:1116, 1120-1121  (source) */
#define MIN_CHUNK_SIZE  (offsetof(struct malloc_chunk, fd_nextsize))   /* 32 on 64-bit */
#define MINSIZE  (unsigned long)(((MIN_CHUNK_SIZE+MALLOC_ALIGN_MASK) & ~MALLOC_ALIGN_MASK))
```

`MIN_CHUNK_SIZE` is the offset of `fd_nextsize` in `struct malloc_chunk` — that is,
`prev_size + size + fd + bk` = **32 bytes on 64-bit** *(source)*. A free chunk must be able
to hold two list pointers, so no chunk can be smaller than that, however small your request.

The resulting ladder for small requests, all *(source)*-derived:

| You ask for | glibc chunk | Waste |
|---|---|---|
| 1 B | 32 B | 31 B (**3100%**) |
| 8 B | 32 B | 24 B |
| 24 B | 32 B | 8 B |
| 25 B | 48 B | 23 B |
| 40 B | 48 B | 8 B |
| 41 B | 64 B | 23 B |
| 1000 B | 1008 B | 8 B |

Two rules to carry away:

1. **`malloc(1)` costs 32 bytes.** Under glibc, a million one-byte allocations is 32 MB.
2. **The marginal cost is 16 bytes per size class step, and the *best case* overhead is
   8 bytes.** Sizes of the form `16k − 8` are exactly efficient; `16k − 7` costs 16 more.

This is where CPython's `__slots__` and `array` advice starts to pay off — but note that
for objects under 512 bytes CPython does *not* reach glibc at all. See §13.

---

## 4. glibc malloc: bins, tcache, and the allocation algorithm

A free chunk goes on a list. Which list depends on its size and on how recently it was
freed. glibc has five kinds.

### 4.1 The five free lists

| List | Chunk sizes | Linkage | Coalescing | Locking |
|---|---|---|---|---|
| **tcache** | small: 64 bins; large: 12 bins up to 4 MiB | singly-linked, LIFO | never while in tcache | **none** — thread-local |
| **fastbins** | ≤ `M_MXFAST` (default 64 B usable / 80 B chunk) | singly-linked, LIFO | deferred | lock-free atomics |
| **unsorted** | any | doubly-linked | on the way out | arena mutex |
| **smallbins** | < 1024 B — 62 bins, one exact size each | doubly-linked, FIFO | yes | arena mutex |
| **largebins** | ≥ 1024 B — 63 bins, size *ranges*, sorted | doubly-linked + `fd_nextsize` skip list | yes | arena mutex |

```c
/* glibc malloc/malloc.c:1375-1379  (source) */
#define NBINS             128
#define NSMALLBINS         64
#define SMALLBIN_WIDTH    MALLOC_ALIGNMENT                     /* 16 on 64-bit */
#define MIN_LARGE_SIZE    ((NSMALLBINS - SMALLBIN_CORRECTION) * SMALLBIN_WIDTH)
```

With `MALLOC_ALIGNMENT == 2 * SIZE_SZ` on 64-bit, `SMALLBIN_CORRECTION` is 0, so
`MIN_LARGE_SIZE = 64 × 16 = 1024`. **1024 bytes is the small/large boundary** *(source)*.

The smallbin/largebin split exists because the search strategies differ. A smallbin holds
exactly one size, so "is there one?" is a pointer test. A largebin holds a range, so you
must find a *best fit*, and glibc keeps largebins sorted largest-first with an auxiliary
`fd_nextsize` skip list linking one chunk per distinct size — so scanning skips duplicates.
The wiki notes a small optimisation with a real consequence:

> "if multiple chunks of a given size are present, the *second* one is typically the chosen
> one, so that the next-size linked list need not be adjusted."

### 4.2 The tcache — and a number that just changed

The tcache is a per-thread array of singly-linked lists, hit before any lock is taken. From
the wiki:

> "Each thread has a per-thread cache (called the *tcache*) containing a small collection of
> chunks which can be accessed without needing to lock an arena… If the tcache bin is empty
> for a given requested size, the next larger sized chunk is not used (could cause internal
> fragmentation), instead the fallback is to use the normal malloc routines i.e. locking the
> thread's arena."

Here is where the primary source pays for itself. Nearly every published description of
glibc's tcache — including material written well into 2025 — says **64 bins, 7 chunks
each**. The current tree says otherwise:

```c
/* glibc malloc/malloc.c:287-309  (source, 2.44.9000, read 2026-08-02) */
# define TCACHE_SMALL_BINS		64
# define TCACHE_LARGE_BINS		12 /* Up to 4M chunks */
# define TCACHE_MAX_BINS	(TCACHE_SMALL_BINS + TCACHE_LARGE_BINS)
...
/* This is another arbitrary limit, which tunables can change.  Each
   tcache bin will hold at most this number of chunks.  */
# define TCACHE_FILL_COUNT 16
```

**76 bins, 16 chunks each.** The large-bin half arrived in glibc 2.42; from `NEWS`:

> "The thread-local cache in malloc (tcache) now supports caching of large blocks. This
> feature can be enabled by setting the tunable `glibc.malloc.tcache_max` to a larger value
> (max 4194304). Tcache is also significantly faster for small sizes."

The tcache bin index arithmetic is worth internalising, because it tells you exactly which
requests share a bin:

```c
/* glibc malloc/malloc.c:293-303  (source) */
# define tidx2csize(idx)	(((size_t) idx) * MALLOC_ALIGNMENT + MINSIZE)
# define csize2tidx(x)      (((x) - MINSIZE) / MALLOC_ALIGNMENT)
/* With rounding and alignment, the bins are...
   idx 0   bytes 0..24 (64-bit) or 0..12 (32-bit)
   idx 1   bytes 25..40 or 13..20
   idx 2   bytes 41..56 or 21..28
   etc.  */
```

Two engineering consequences:

- **The tcache is a cache with a retention policy, and retention costs RSS.** Up to
  76 bins × 16 chunks × *(bin size)* per thread, held out of circulation. With large bins
  enabled that is a materially bigger number than it used to be. `glibc.malloc.tcache_count`
  and `glibc.malloc.tcache_max` are the tunables; setting `tcache_count=0` disables it.
- **A tcache hit is an exact-size match only.** Ask for 25 bytes when the 24-byte bin is
  full and the 40-byte bin is empty, and you take the arena lock. Size stability in a hot
  loop is worth more than it looks.

### 4.3 The algorithm, in order

The wiki's own summary of `malloc`, which is the version to memorise:

> - If there is a suitable (exact match only) chunk in the tcache, it is returned to the
>   caller. No attempt is made to use an available chunk from a larger-sized bin.
> - If the request is large enough, `mmap()` is used to request memory directly from the
>   operating system. Note that the threshold for mmap'ing is dynamic…
> - If the appropriate fastbin has a chunk in it, use that. If additional chunks are
>   available, also pre-fill the tcache.
> - If the appropriate smallbin has a chunk in it, use that, possibly pre-filling the
>   tcache here also.
> - If the request is "large", take a moment to take everything in the fastbins and move
>   them to the unsorted bin, coalescing as you go.
> - Start taking chunks off the unsorted list, and moving them to small/large bins,
>   coalescing as you go… If a chunk of the right size is seen, use that.
> - If the request is "large", search the appropriate large bin, and successively larger
>   bins, until a large-enough chunk is found.
> - If we still have chunks in the fastbins…, consolidate those and repeat…
> - Split off part of the "top" chunk, possibly enlarging "top" beforehand.

Note the shape of it. **The unsorted bin is a deferred-sorting trick**: freed chunks go
there without classification, and the classification work happens lazily on the next
allocation that has to walk it. This is amortisation, and it is why `free()` is cheap and
why *some* `malloc()` calls are surprisingly expensive. If you are chasing a tail-latency
outlier in an allocation-heavy service, an unsorted-bin walk is a candidate.

Note also the last line. **The top chunk is the fallback**, and it is the boundary with the
kernel. Everything in §6 is about the top chunk.

### 4.4 Fastbins: a deliberate fragmentation trade

Fastbins hold small chunks *without coalescing them* and *without clearing the `P` bit of
the following chunk* — so neighbours cannot merge with them. That makes free/alloc of small
chunks nearly free, at the cost of letting small holes persist. Consolidation happens
lazily: on a large request, or in `malloc_trim`, or when the top chunk needs to grow.

`M_MXFAST` (default 64 bytes of usable space, i.e. 80-byte chunks on 64-bit) sets the
ceiling. Setting it to 0 disables fastbins entirely, trading throughput for less
fragmentation. This is a real knob for a long-running service with a fragmentation problem
— and it is worth trying *before* switching allocators, because it is a one-line change.

---

## 5. Arenas and heaps — and the RSS multiplication trap

### 5.1 The structure

One global lock over `malloc` would be a disaster on a 64-core box. glibc's answer is
**arenas** — independent allocator instances, each with its own bins and its own mutex.
From the wiki:

> "As pressure from thread collisions increases, additional arenas are created via mmap to
> relieve the pressure… Each arena structure has a mutex in it which is used to control
> access to that arena… Contention for this mutex is the reason why multiple arenas are
> created — threads assigned to different arenas need not wait for each other. Threads will
> automatically switch to unused (unlocked) arenas if contention requires it."

The **main arena** is special: it is the original heap and it grows with `brk`. Every
**secondary arena** is built from `mmap`ed **heaps**:

```c
/* glibc malloc/arena.c:27-32  (source) */
#define HEAP_MIN_SIZE (32 * 1024)
#  define HEAP_MAX_SIZE (2 * DEFAULT_MMAP_THRESHOLD_MAX)   /* 64 MiB on 64-bit */
```

So a secondary arena is a chain of **64 MiB** address-space reservations *(source)* — each
`mmap`ed `PROT_NONE` and committed incrementally as the arena grows. This is why
`ps` shows enormous `VSZ` for threaded C programs: address space, not memory. Doc 07 §10 is
the reference for why `VSZ` is worthless as a memory metric.

### 5.2 The arena cap changed in glibc 2.44 — the docs have not caught up

Every secondary source says the same thing. The sourceware wiki:

> "The number of arenas is capped at eight times the number of CPUs in the system (unless
> the user specifies otherwise, see mallopt), which means a heavily threaded application
> will still see some contention, but the trade-off is that there will be less
> fragmentation."

That has been true for over a decade. **It is no longer true in the current tree.** Here is
`arena_get2`, read on 2 August 2026:

```c
/* glibc malloc/arena.c:798-807  (source, 2.44.9000) */
if (narenas_limit == 0)
  {
    if (mp_.arena_max != 0)
      narenas_limit = mp_.arena_max;
    else if (narenas >= mp_.arena_test)
      {
        narenas_limit = __get_nprocs ();
        if (narenas_limit < mp_.arena_test)
          narenas_limit = mp_.arena_test;
      }
  }
```

`narenas_limit = __get_nprocs()`. **One arena per core, not eight.** The
`NARENAS_FROM_NCORES(n)` macro — `n * 8` on 64-bit — is gone.

I checked when *(source)*, by reading `malloc/arena.c` at each release tag:

| glibc | Arena cap expression |
|---|---|
| 2.39 | `NARENAS_FROM_NCORES (n)` → 8 × cores |
| 2.41 | `NARENAS_FROM_NCORES (n)` → 8 × cores |
| 2.42 | `NARENAS_FROM_NCORES (n)` → 8 × cores |
| 2.43 | `NARENAS_FROM_NCORES (n)` → 8 × cores |
| **2.44** | **`__get_nprocs ()`** → 1 × cores |

The floor below which no new arenas are created at all:

```c
/* glibc malloc/malloc.c:1657  (source) */
  .arena_test = sizeof (long) == 4 ? 2 : 8,
```

So on 64-bit: the first 8 arenas are created on demand; past that, the cap is the core
count *(source, 2.44.9000)*.

**Why this matters more than it sounds.** The classic glibc memory-blowup story is:
*N* threads → *N* arenas → each arena independently retains free chunks → RSS is roughly
*N* times the working set of one thread. On a 64-core machine, the old cap of 512 arenas
× 64 MiB of heap reservation each was a genuinely explosive configuration, and
`MALLOC_ARENA_MAX=2` became folk wisdom in the Java, Ruby, and Python server communities
for exactly that reason. glibc 2.44 cuts the worst case by 8×. **If you are carrying a
`MALLOC_ARENA_MAX` setting from 2019, re-measure it on a current glibc before assuming it
still helps** — you may now be paying contention for no memory benefit.

The knob itself, from `mallopt(3)`:

> "**M_ARENA_MAX** — If this parameter has a nonzero value, it defines a hard limit on the
> maximum number of arenas that can be created… The trade-off is between the number of
> threads and the number of arenas. The more arenas you have, the lower the per-thread
> contention, but the higher the memory usage. The default value of this parameter is 0,
> meaning that the limit on the number of arenas is determined according to the setting of
> **M_ARENA_TEST**."

```bash
MALLOC_ARENA_MAX=2 python app.py          # environment variable
# or: GLIBC_TUNABLES=glibc.malloc.arena_max=2 python app.py
```

### 5.3 Why this is mostly not CPython's problem

Under the default GIL build, CPython allocates from one thread at a time by construction —
so arena multiplication driven by *Python-level* allocation does not happen. Where it *does*
bite Python services:

- **C extensions that allocate off the GIL.** NumPy releasing the GIL around a large
  operation, a compression library, a database driver's I/O buffers — these run `malloc`
  concurrently on real threads and will spread across arenas.
- **The free-threaded build.** Multiple interpreter threads allocating simultaneously is
  the entire point. See §16.
- **Anything above the 512-byte pymalloc threshold**, which goes straight to libc from
  whatever thread is running. A thread pool decoding 64 KiB JSON payloads is a libc-arena
  workload wearing a Python costume.

---

## 6. Why freed memory does not come back

This is the section to read twice.

### 6.1 Three separate mechanisms

Memory returns to the kernel by exactly three routes, and each has a different trigger:

**Route 1 — `munmap` of an individually-mapped chunk.** If the chunk had the `M` bit set
(§3.1), `free()` calls `munmap` and the RSS drops immediately. This is why large
allocations behave "correctly" and small ones do not.

**Route 2 — trimming the top of the heap.** From `mallopt(3)`:

> "**M_TRIM_THRESHOLD** — When the amount of contiguous free memory at the top of the heap
> grows sufficiently large, `free(3)` employs `sbrk(2)` to release this memory back to the
> system… The `M_TRIM_THRESHOLD` parameter specifies the minimum size (in bytes) that this
> block of memory must reach before `sbrk(2)` is used to trim the heap. The default value
> for this parameter is 128\*1024. Setting `M_TRIM_THRESHOLD` to -1 disables trimming
> completely."

The load-bearing word is **contiguous** and the load-bearing phrase is **at the top of the
heap**. One live 32-byte chunk above a 500 MB free region pins all 500 MB. That is §2.1's
arithmetic showing up as an operational incident.

**Route 3 — an explicit `malloc_trim()`.** From the man page:

> "The `malloc_trim()` function attempts to release free memory from the heap (by calling
> `sbrk(2)` or `madvise(2)` with suitable arguments). The *pad* argument specifies the
> amount of free space to leave untrimmed at the top of the heap."

And, critically:

> "Since glibc 2.8 this function frees memory in all arenas and in all chunks with whole
> free pages. Before glibc 2.8 this function only freed memory at the top of the heap in
> the main arena."

> "Only the main heap (using `sbrk(2)`) honors the *pad* argument; thread heaps do not."

**This is the important one and it is widely misunderstood.** Modern `malloc_trim` does not
just chop the top — it walks every arena and `madvise(MADV_DONTNEED)`s every *whole free
page* it finds, wherever it is. It can therefore recover memory that trimming-on-`free`
never will. It is O(number of free chunks), it takes every arena lock, and it is the reason
some Python services call `ctypes.CDLL("libc.so.6").malloc_trim(0)` after a large batch job:

```python
# Linux/glibc only. Returns 1 if any memory was released, 0 otherwise.
import ctypes, platform
if platform.libc_ver()[0] == "glibc":
    libc = ctypes.CDLL("libc.so.6")
    libc.malloc_trim(ctypes.c_size_t(0))
```

Do not put this on a hot path. It is a batch-boundary tool: after loading and discarding a
dataset, after a request that ballooned, at the end of a worker's job. And it does nothing
for memory pymalloc is still holding — see §13.4.

**M_TOP_PAD** is the counterweight, from `mallopt(3)`:

> "When the heap is trimmed as a consequence of calling `free(3)`… this much free space is
> preserved at the top of the heap… Modifying **M_TOP_PAD** is a trade-off between
> increasing the number of system calls (when the parameter is set low) and wasting unused
> memory at the top of the heap (when the parameter is set high). The default value for this
> parameter is 128\*1024."

### 6.2 The decision table

Every allocator in this document makes the same three-way choice; only the policy differs.

| Allocator | Default policy for returning free memory |
|---|---|
| **glibc** | Trim top of heap when ≥ 128 KiB contiguous; `munmap` individually-mapped chunks; nothing else until `malloc_trim` |
| **jemalloc** | Time-based decay: dirty pages purged after `dirty_decay_ms`, then muzzy pages after `muzzy_decay_ms`; optional background threads do the work |
| **tcmalloc** | Background release at a configurable **rate** (bytes/second) |
| **mimalloc** | Purge pages `purge_delay` ms after they become free (default 1000 ms in v3) |
| **pymalloc** | Return a 1 MiB arena to libc only when **every** pool in it is free |

Three of the four are **time-based**; glibc's is **shape-based**. That difference explains
most of the observed behaviour gap: jemalloc, tcmalloc, and mimalloc give memory back on a
schedule whether or not the heap happens to be shaped conveniently, and glibc does not.

**If your one-line summary of this document is "switch to jemalloc and RSS goes down", this
row is why** — and it is also why the improvement is not free. Purging costs `madvise`
syscalls and re-faults on reuse (doc 07 §3.1: ~0.5–0.6 µs per fault here).

---

## 7. Fragmentation: three kinds, one metric

"Fragmentation" is three unrelated failures sharing a word. Separating them is most of the
diagnostic work.

### 7.1 Internal fragmentation — the rounding tax

You asked for 33 bytes; you got a 48-byte chunk; 15 bytes are unusable. Deterministic,
predictable from the size-class table, and measurable in advance. §8 measures it.

**Characteristic signature:** RSS is a stable multiple of your accounted live bytes,
constant over time.

### 7.2 External fragmentation — the holes

Free memory exists, in sufficient total quantity, but no single run is large enough for the
request — or the runs are in the wrong place to be returned. Boundary tags and coalescing
(§3.1) suppress the classic version of this; the version that survives in modern allocators
is the *placement* version, which doc 07 §14 measured directly: **the same 20,000 surviving
objects cost 211 MB or 22 MB of RSS depending only on where they landed.**

**Characteristic signature:** RSS grows, plateaus at a high level, and never comes down,
while your live-object count is flat.

### 7.3 Allocator retention — the caches

Not fragmentation at all, but it looks identical from outside: memory that is free, is
coalesced, is contiguous, and the allocator is simply *choosing* to keep it. tcaches,
fastbins, thread caches, un-decayed dirty pages, pymalloc's partially-used arenas.

**Characteristic signature:** RSS drops when you call `malloc_trim(0)` or lower a decay
setting. If it does, it was retention, not fragmentation — and the fix is a tunable, not a
rewrite.

### 7.4 The one metric

All three collapse into a single ratio worth putting on a dashboard:

```
                   RSS (private, dirty)
  bloat factor =  ──────────────────────
                    live bytes you can account for
```

Numerator from `Private_Dirty` in `/proc/self/smaps_rollup` (doc 07 §10); denominator from
`tracemalloc` or your own accounting — knowing, per doc 07 §15.1, that `tracemalloc`
understates RSS by **2.7–3.3×** on small-object workloads and does not see anything C
extensions allocate.

Rough reading of the ratio, from experience rather than measurement:

| Bloat factor | Reading |
|---|---|
| ~1.3–1.6× | Normal. Headers, rounding, allocator metadata. |
| ~2× | Worth a look. Usually retention (§7.3) — try `malloc_trim` first. |
| > 3× | Something structural. Placement (§7.2), arena multiplication (§5.2), or a genuine leak. |

The famous rebuttal to fragmentation panic is Johnstone & Wilson's *The Memory
Fragmentation Problem: Solved?* (ISMM '98), which measured real programs against a set of
allocation policies and found that **best-fit and address-ordered first-fit produced almost
no true fragmentation** — the observed waste was overwhelmingly the allocator's own
rounding and retention. Nearly thirty years later, that is still the right prior:
**suspect §7.1 and §7.3 before §7.2.**

---

## 8. Internal fragmentation, measured

macOS's `libmalloc` exposes `malloc_size()`, which reports the *usable* size of an
allocation. That makes the size-class ladder directly enumerable, so this is a rare case
where the measurement is trivially better than reading a table. Sweeping every request size
from 1 byte to 256 KiB and recording each distinct usable size *(measured)*:

```
num size classes up to 256 KiB: 54

     1 ->      16          257 ->     320       32769 ->   49152
    17 ->      32          321 ->     384       49153 ->   65536
    33 ->      48          385 ->     448       65537 ->   81920
    49 ->      64          449 ->     512       81921 ->   98304
    65 ->      80          513 ->     640       98305 ->  114688
    81 ->      96          641 ->     768      114689 ->  131072
    97 ->     112          769 ->     896      131073 ->  147456
   113 ->     128          897 ->    1024      147457 ->  163840
   129 ->     160         1025 ->    1280      163841 ->  180224
   161 ->     192         1281 ->    1536      180225 ->  196608
   193 ->     224         1537 ->    1792      196609 ->  212992
   225 ->     256         1793 ->    2048      212993 ->  229376
                                               229377 ->  245760
                                               245761 ->  262144
```

Read the structure off it:

- **1–128 B: a linear ladder, 16-byte quantum.** Eight classes. Maximum waste 15 B.
- **129–256 B: 32-byte quantum.** Then 64, then 128… The quantum doubles every four
  classes — a **quarter-power ladder**, ~19% apart geometrically.
- **Above 32 KiB: a 16 KiB quantum**, which is exactly this machine's page size. Above the
  page size the allocator stops trying and just rounds to pages.

Averaged over a uniform distribution of request sizes from 1 to 1024 bytes *(measured)*:

```
mean internal waste, uniform 1..1024 B: 42.5 B (8.3% of mean request)
```

**~8% is the price of a size-class allocator**, and that is a good number to carry as a
prior for jemalloc, tcmalloc, and mimalloc too — all four use quarter-power-style ladders
with comparable spacing. It is *not* the number for glibc, whose 16-byte-uniform ladder
(§3.2) is finer-grained and wastes less on small sizes but gives up the O(1) class lookup.

Two cross-checks against the theory:

- `malloc(1) → 16` on macOS versus `→ 32` on glibc *(source)*. Apple's minimum block is
  smaller because its small-region metadata is out-of-band, not in a boundary tag.
- The class boundaries land exactly on 512, 1024, 2048 … so **powers of two are the *worst*
  sizes to request**: `malloc(1025)` gets 1280 bytes, wasting 255. If you control an
  object's size, land it just below a boundary, never just above.

> **Provenance note.** These are **macOS `libmalloc`** numbers, not glibc. The default zone
> on this process reports as `DefaultMallocZone` *(measured)*; setting `MallocNanoZone=0`
> made no difference to the §15 benchmark (152.7 vs 152.1 ms median, inside the noise), so
> the nano zone is not doing the work here. The *shape* of the ladder generalises; the exact
> boundaries do not.

---

## 9. jemalloc: extents, decay, and fragmentation avoidance

jemalloc's own one-line self-description:

> "jemalloc is a general purpose `malloc(3)` implementation that emphasizes **fragmentation
> avoidance** and scalable concurrency support. jemalloc first came into use as the FreeBSD
> libc allocator in 2005… In 2010 jemalloc development efforts broadened to include
> developer support features such as heap profiling and extensive monitoring/tuning hooks."

That is the pitch and it is accurate: **jemalloc is the allocator you reach for when RSS is
the problem**, and its profiling story is the best of the four.

### 9.1 Structure

From the man page's implementation notes:

> "Traditionally, allocators have used `sbrk(2)` to obtain memory, which is suboptimal for
> several reasons, including race conditions, increased fragmentation, and artificial
> limitations on maximum usable memory. If `sbrk(2)` is supported by the operating system,
> this allocator uses both `mmap(2)` and `sbrk(2)`, in that order of preference; otherwise
> only `mmap(2)` is used."

> "This allocator uses multiple arenas in order to reduce lock contention for threaded
> programs on multi-processor systems. This works well with regard to threading scalability,
> but incurs some costs. There is a small fixed per-arena overhead, and additionally, arenas
> manage memory completely independently of each other, which means a small fixed increase
> in overall memory fragmentation."

> "In addition to multiple arenas, this allocator supports thread-specific caching, in order
> to make it possible to completely avoid synchronization for most allocation requests. Such
> caching allows very fast allocation in the common case, but it increases memory usage and
> fragmentation, since a bounded number of objects can remain allocated in each thread
> cache."

> "Memory is conceptually broken into extents."

The hierarchy: **arena → extent → slab → region.** An extent is a run of pages; a slab is an
extent dedicated to one size class; regions are the equal-sized blocks inside it. Small
allocations are bitmap-indexed within a slab — **no per-object header at all**, which is
where jemalloc's low overhead comes from. Large allocations get their own extent.

The arena binding is per-thread by default, with a `percpu` mode available:

> "Per CPU arena mode. Use the 'percpu' setting to enable this feature, which uses number of
> CPUs to determine number of arenas, and bind threads to arenas dynamically based on the
> CPU the thread runs on currently. 'phycpu' setting uses one arena per physical CPU, which
> means the two hyper threads on the same CPU share one arena… The default is 'disabled'."

### 9.2 Dirty and muzzy — the decay model

This is jemalloc's most distinctive idea and the reason it behaves so differently from
glibc under §6.2. Pages move through three states:

```
   allocated ──free──▶  DIRTY  ──dirty_decay_ms──▶  MUZZY  ──muzzy_decay_ms──▶  CLEAN
                          │                           │                           │
                   still has your data          MADV_FREE'd:                  MADV_DONTNEED'd
                   reuse is free                kernel may reclaim,          / decommitted;
                                                reuse is still cheap         reuse costs a fault
```

From the man page:

> "Approximate time in milliseconds from the creation of a set of unused dirty pages until
> an equivalent set of unused dirty pages is purged (i.e. converted to muzzy via e.g.
> `madvise(...MADV_FREE)` if supported by the operating system, or converted to clean
> otherwise) and/or reused. Dirty pages are defined as previously having been potentially
> written to…"

`TUNING.md` is unusually direct about what to set:

> "`dirty_decay_ms` and `muzzy_decay_ms` — Decay time determines how fast jemalloc returns
> unused pages back to the operating system, and therefore provides a fairly straightforward
> trade-off between CPU and memory usage."

and on background threads:

> "Enabling jemalloc background threads generally improves the tail latency for application
> threads, since unused memory purging is shifted to the dedicated background threads. In
> addition, unintended purging delay caused by application inactivity is avoided with
> background threads. **Suggested:** `background_thread:true` when jemalloc managed threads
> can be allowed."

and on metadata:

> "`metadata_thp` — Allowing jemalloc to utilize transparent huge pages for its internal
> metadata usually reduces TLB misses significantly, especially for programs with large
> memory footprint and frequent allocation / deallocation activities… **Suggested for
> allocation intensive programs:** `metadata_thp:auto` or `metadata_thp:always`."

A reasonable memory-first starting configuration for a Python service, all from `TUNING.md`:

```bash
export MALLOC_CONF="background_thread:true,metadata_thp:auto,dirty_decay_ms:5000,muzzy_decay_ms:5000"
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2 python app.py
```

Setting either decay to `0` purges immediately (lowest RSS, highest syscall and fault cost);
`-1` disables purging entirely (highest RSS, fastest).

### 9.3 The `background_thread` caveat

From the man page, and this bites people:

> "Internal background worker threads enabled/disabled. Because of potential circular
> dependencies, enabling background thread using this option may cause crash or deadlock
> during initialization. For a reliable way to use this feature, see `background_thread` for
> dynamic control options and details."

Also: **jemalloc's background threads do not survive `fork()`** in the child. A pre-forking
server (gunicorn, uWSGI) must re-enable them per worker via the `background_thread` mallctl
after fork, or the workers silently never purge. If you deploy jemalloc under a pre-forking
server and see no RSS improvement, check this before anything else.

---

## 10. tcmalloc: three tiers and a per-CPU cache

Google's tcmalloc is the throughput-first design. Its stated goals:

> "Fast, uncontended allocation and deallocation for most objects. Objects are cached,
> depending on mode, either per-thread, or per-logical-CPU. Most allocations do not need to
> take locks…"

### 10.1 The three tiers

> "We can break TCMalloc into three components. The front-end, middle-end, and back-end."

- **Front-end** — a per-thread or per-CPU cache. Lock-free in the common case. Serves a
  request from a size-class free list.
- **Middle-end** — refills and drains the front-end. "The middle-end comprises the Transfer
  cache and the Central free list. Although these are often referred to as singular, there
  is one transfer cache and one central free list per size-class. These caches are each
  protected by a mutex lock — so there is a serialization cost to accessing them."
- **Back-end** — the page heap: fetches memory from the OS, manages spans of pages.

The per-size-class locking in the middle end is the key scalability property: contention is
sharded by size class, so unrelated workloads on different sizes do not serialise against
each other.

### 10.2 Per-CPU caches and restartable sequences

The distinctive piece. Per-*thread* caches waste memory in proportion to thread count;
per-*CPU* caches waste it in proportion to core count, which is bounded. But a per-CPU data
structure needs the update to be atomic with respect to preemption, and that is not
something user space can normally arrange.

> "TCMalloc implements its per-CPU caches using restartable sequences (`man rseq(2)`) on
> Linux."

> "The practical implication of this for TCMalloc is that the code can use a restartable
> sequence like `TcmallocSlab_Internal_Push` to fetch from or return an element to a per-CPU
> array without needing locking. The restartable sequence ensures that either the array is
> updated without the thread being interrupted, or the sequence is restarted if the thread
> was interrupted (for example, by a context switch that enables a different thread to run
> on that CPU)."

This is a genuinely different point in the design space: the fast path is a handful of
non-atomic instructions in a region the kernel will restart if you get preempted. It is also
**Linux-only** — `rseq` has no equivalent elsewhere, so tcmalloc on other platforms falls
back to per-thread caches and loses part of its advantage.

### 10.3 Hugepage awareness (Temeraire)

tcmalloc's back end can manage memory in **hugepage-sized chunks**:

> "The hugepage aware pageheap which manages memory in chunks of hugepage sizes. Managing
> memory in hugepage chunks enables the allocator to improve application performance by
> reducing TLB misses."

The sub-allocators, from the design doc:

> - "The **filler** cache holds hugepages which have had some memory allocated from them…
>   Allocation requests for sizes of less than a hugepage in size are (typically) returned
>   from the filler cache."
> - "The **region** cache which handles allocations of greater than a hugepage… This is
>   particularly useful for allocations that slightly exceed the size of a hugepage (for
>   example, 2.1 MiB)."
> - "The **hugepage cache** handles large allocations of at least a hugepage."

And the routing policy, which is a nice illustration of how much of allocator design is
heuristic:

> "Small allocations are handed directly to the filler… For slightly larger allocations
> (still under a full hugepage), we *try* the filler, but don't grow it if there's not
> currently space. Instead, we look in the regions for free space… The changeover point
> between 1) and 2) is just a tuning decision (any choice would produce a usable binary).
> Half a hugepage was picked arbitrarily; this seems to work well."

Doc 07 §11 covers why databases turn transparent huge pages *off*. tcmalloc's approach is
the reconciliation: use hugepages, but let the *allocator* decide placement rather than
`khugepaged` guessing.

### 10.4 The three knobs

From `tuning.md`:

> "There are three user accessible controls that we can use to performance tune TCMalloc:
> the logical page size for TCMalloc (4KiB, 8KiB, 32KiB, 256KiB); the per-thread or per-cpu
> cache sizes; the rate at which memory is released to the OS. **None of these tuning
> parameters are clear wins**, otherwise they would be the default."

That last sentence deserves to be quoted at anyone who arrives with a tuning cargo cult.

---

## 11. mimalloc: free-list sharding

mimalloc (Microsoft Research, Daan Leijen) is the youngest of the four, the smallest, and —
for our purposes — the most important, because **CPython's free-threaded build uses it**
(§16). Current release: **v3.4.4 (2026-08-01)**, with v2 (2.4.4) as the stable line.

### 11.1 The core idea

From the design notes:

> "**free list sharding**: instead of one big free list (per size class) we have many smaller
> lists per 'mimalloc page' which reduces fragmentation and increases locality — things that
> are allocated close in time get allocated close in memory. (A mimalloc page contains blocks
> of one size class and is usually 64KiB on a 64-bit system)."

> "**free list multi-sharding**: the big idea! Not only do we shard the free list per mimalloc
> page, but for each page we have multiple free lists. In particular, there is one list for
> thread-local `free` operations, and another one for concurrent `free` operations. Free-ing
> from another thread can now be a single CAS without needing sophisticated coordination
> between threads. Since there will be thousands of separate free lists, contention is
> naturally distributed over the heap, and the chance of contending on a single location will
> be low — this is quite similar to randomized algorithms like skip lists where adding a
> random oracle removes the need for a more complex algorithm."

Read that twice. **Cross-thread `free` — the hard case for every allocator on this list — is
one CAS on a list nobody else is likely to be touching.** No central free list, no transfer
cache, no arena lock. The contention problem is solved by having so many independent
structures that collisions are improbable, rather than by making collisions cheap.

The third design element closes the fragmentation loop:

> "**eager page purging**: when a 'page' becomes empty (with increased chance due to free list
> sharding) the memory is marked to the OS as unused (reset or decommitted) reducing (real)
> memory pressure and fragmentation, especially in long running programs."

Sharding *increases the probability that a page becomes completely empty*, which makes
purging actually possible. This is the direct answer to doc 07 §14's placement problem —
and it is the same insight pymalloc encodes with its "arena freed only when all pools are
free" rule (§13.4), except mimalloc gets to act on it 64 KiB at a time instead of 1 MiB at a
time.

### 11.2 Practical properties

> "**small and consistent**: the library is about 10k LOC using simple and consistent data
> structures. This makes it very suitable to integrate and adapt in other projects. For
> runtime systems it provides hooks for a monotonic *heartbeat* and deferred freeing (for
> bounded worst-case times with reference counting)."

"Deferred freeing… for bounded worst-case times with reference counting" is not a
coincidence — mimalloc was built for the Koka and Lean runtimes, which are refcounted. It is
also precisely what CPython needs.

Purging is time-based (§6.2):

> "`MIMALLOC_PURGE_DELAY=N`: the delay in `N` milli-seconds (by default `1000` in v3) after
> which mimalloc will purge OS pages that are not in use… Setting `N` to `0` purges
> immediately when a page becomes unused which can improve memory usage but also decreases
> performance. Setting it to `-1` disables purging completely."

> "`MIMALLOC_PURGE_DECOMMITS=1`: By default 'purging' memory means unused memory is
> decommitted (`MEM_DECOMMIT` on Windows, `MADV_DONTNEED` (which decreases rss immediately)
> on `mmap` systems)."

### 11.3 Secure mode

Unique among the four in offering hardening as a build option:

> - "All internal mimalloc page meta-data is surrounded by guard pages (so a buffer overflow
>   exploit cannot reach into the metadata)."
> - "All free list pointers are encoded with per-page keys which is used both to prevent
>   overwrites with a known pointer, as well as to detect heap corruption."
> - "Double free's are detected (and ignored)."
> - "The free lists are initialized in a random order and allocation randomly chooses between
>   extension and reuse within a page to mitigate against attacks that rely on a predicable
>   allocation order."

with the honest caveat, which applies to every mitigation list ever written:

> "As always, evaluate with care as part of an overall security strategy as all of the above
> are mitigations but not guarantees."

This is worth knowing about in contrast to glibc, where the in-band boundary tags of §3.1 are
*themselves* the attack surface — the entire heap-exploitation literature (tcache poisoning,
unsorted-bin attacks, house-of-*) exists because glibc stores allocator pointers inside
attacker-controllable payloads.

### 11.4 On the benchmark claims

The README's performance section says mimalloc "outperforms other leading allocators
(*jemalloc*, *tcmalloc*, *Hoard*, etc), and has a similar memory footprint" — but it also
notes **"Last update: 2021-01-30"** and adds its own caveats:

> "General memory allocators are interesting as there exists no algorithm that is optimal —
> for a given allocator one can usually construct a workload where it does not do so well."

> "As always, interpret these results with care since some benchmarks test synthetic or
> uncommon situations that may never apply to your workloads. For example, most allocators do
> not do well on `xmalloc-testN` but that includes even the best industrial allocators like
> *jemalloc* and *tcmalloc* that are used in some of the world's largest systems."

Take the ranking as "mimalloc is competitive with the best," not as a number. Five-year-old
benchmarks against actively developed competitors are not evidence about today. §12 gives the
only ranking that matters.

---

## 12. Choosing and installing an allocator

### 12.1 The comparison

| | **glibc** | **jemalloc** | **tcmalloc** | **mimalloc** |
|---|---|---|---|---|
| Optimises for | compatibility, ubiquity | **fragmentation avoidance** | **throughput** | throughput + simplicity |
| Small-alloc fast path | tcache, thread-local, no lock | tcache, thread-local, no lock | per-CPU (rseq) or per-thread, no lock | per-page sharded free list, no lock |
| Cross-thread free | arena lock | arena lock | middle-end lock | **single CAS** |
| Per-object header | **16 B boundary tag** | none (bitmap in slab) | none (span metadata) | none (page metadata) |
| Concurrency unit | arena (≤ ncores, 2.44+) | arena (per-thread or per-CPU) | per-CPU cache | per-thread heap + per-page lists |
| Returns memory | top-of-heap trim, `munmap`, `malloc_trim` | **time decay**, background threads | background **rate** limit | **time delay** (1 s default) |
| Profiling | `malloc_stats`, `mallinfo2`, `malloc_info` | **`jeprof`, best in class** | `MallocExtension`, pprof | `MIMALLOC_SHOW_STATS` |
| Hardened mode | no | no | no | **yes** (`MI_SECURE`) |
| Deployment | already there | `LD_PRELOAD` / link | link (or `LD_PRELOAD`) | `LD_PRELOAD` / link |
| Linux-only feature | — | — | **per-CPU needs `rseq`** | — |

### 12.2 How to actually swap one in

```bash
# jemalloc — the usual first thing to try for an RSS problem
sudo apt install libjemalloc2
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2 \
MALLOC_CONF="background_thread:true,dirty_decay_ms:5000,muzzy_decay_ms:5000" \
  python app.py

# tcmalloc — the usual first thing to try for an allocation-throughput problem
sudo apt install libtcmalloc-minimal4
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4 python app.py

# mimalloc
LD_PRELOAD=/usr/lib/libmimalloc.so MIMALLOC_SHOW_STATS=1 python app.py
```

`LD_PRELOAD` replaces `malloc` for the whole process, **including every C extension** — which
is usually what you want, since NumPy, Pillow, and database drivers are often where the bytes
actually are.

Two deployment cautions:

- **`LD_PRELOAD` does not survive `exec` into a different container/image** and is silently
  dropped for setuid binaries. In Kubernetes, set it in the pod spec `env`, not in a shell
  wrapper someone will later refactor away.
- **Alpine/musl is not glibc.** musl's allocator (mallocng) is a different design with
  different behaviour, and much of §3–§6 does not apply. If you moved to Alpine and memory
  behaviour changed, that is the first thing to check.

### 12.3 The decision procedure

**Do not choose an allocator by reading benchmarks. Choose it by measuring your service.**
The honest procedure is four steps and takes an afternoon:

1. **Establish the bloat factor** (§7.4) on the current allocator, under production-shaped
   load, for long enough to reach steady state (hours, not minutes).
2. **Try the free knobs first.** `MALLOC_ARENA_MAX`, `MALLOC_MMAP_THRESHOLD_`, an explicit
   `malloc_trim(0)` at a batch boundary. These are one-line changes and they resolve a
   surprising share of cases. If `malloc_trim(0)` recovers the memory, you had retention
   (§7.3) and you are done.
3. **Then try jemalloc with decay tuning.** It is the highest-probability win for an RSS
   problem and its profiler will tell you where the bytes are even if you end up switching
   back.
4. **Only then consider throughput allocators.** And measure both dimensions — tcmalloc and
   mimalloc trade memory for speed by design, and "we fixed latency and OOMed" is a real
   outcome.

At every step, measure **RSS and latency together**. Every knob in this document trades one
for the other.

---

## 13. Where CPython sits on all this

### 13.1 The three domains

CPython does not have one allocator; it has three, documented in the C API:

| Domain | Function | Used for | Backed by (default GIL build) |
|---|---|---|---|
| **Raw** | `PyMem_RawMalloc` | memory that may be freed without the GIL held | system `malloc` |
| **Mem** | `PyMem_Malloc` | internal buffers, non-object memory | **pymalloc** |
| **Object** | `PyObject_Malloc` | Python objects | **pymalloc** |

The full default table from the docs:

| Configuration | Name | `PyMem_RawMalloc` | `PyMem_Malloc` | `PyObject_Malloc` |
|---|---|---|---|---|
| Release build | `"pymalloc"` | `malloc` | pymalloc | pymalloc |
| Debug build | `"pymalloc_debug"` | `malloc` + debug | pymalloc + debug | pymalloc + debug |
| Release, without pymalloc | `"malloc"` | `malloc` | `malloc` | `malloc` |
| Debug, without pymalloc | `"malloc_debug"` | `malloc` + debug | `malloc` + debug | `malloc` + debug |
| **Free-threaded build** | `"mimalloc"` | mimalloc | mimalloc | mimalloc |
| Free-threaded debug | `"mimalloc_debug"` | mimalloc + debug | mimalloc + debug | mimalloc + debug |

Select at runtime with `PYTHONMALLOC`. `PYTHONMALLOC=malloc` is the single most useful
debugging setting in this document: it takes pymalloc out of the picture entirely, so
Valgrind, ASan, and your platform's malloc debugging tools can see every Python allocation.
`PYTHONMALLOC=pymalloc_debug` (or `-X dev`) installs CPython's own debug hooks — guard bytes,
fill patterns, API-mismatch detection.

The mismatch rule is absolute and worth stating because it is a real crash source in
extensions: **memory allocated by one domain must be freed by the same domain.**
`PyObject_Malloc` + `free()` is undefined behaviour, and under `PYTHONMALLOC=pymalloc` it
will usually appear to work for years before it doesn't.

### 13.2 pymalloc's geometry, on this build

pymalloc is a three-level structure: **arena → pool → block.** Doc 16 §3 covers it in full;
here is the part that matters for the boundary with libc, read from the source *(source)* and
confirmed at runtime *(measured)*.

```c
/* CPython Include/internal/pycore_obmalloc.h:137-138, 163-164  (source) */
#define ALIGNMENT              16               /* 64-bit; 8 on 32-bit */
#define SMALL_REQUEST_THRESHOLD 512
#define NB_SMALL_SIZE_CLASSES   (SMALL_REQUEST_THRESHOLD / ALIGNMENT)   /* = 32 */

/* :195-198, 217-223, 235-240 */
#if SIZEOF_VOID_P > 4
#define USE_LARGE_ARENAS                 /* 64-bit → 1 MiB arenas */
#if WITH_PYMALLOC_RADIX_TREE
#define USE_LARGE_POOLS                  /* → 16 KiB pools */
#endif
#endif
#  define ARENA_BITS            20       /* 1 MiB  (21 = 2 MiB with hugepages) */
#define POOL_BITS               14       /* 16 KiB */
```

`sys._debugmallocstats()` on this interpreter confirms every one of those *(measured)*:

```
Small block threshold = 512, in 32 size classes.
2 arenas * 1048576 bytes/arena     =            2,097,152
23 unused pools * 16384 bytes      =              376,832
# bytes lost to pool headers       =                5,040
# bytes lost to arena map root     =              262,144
# bytes lost to arena map mid      =              262,144
# bytes lost to arena map bot      =              131,072
```

Three things to notice:

1. **1 MiB arenas, 16 KiB pools, 32 size classes 16 bytes apart, threshold 512.** Note that
   the pool size (16 KiB) coincidentally equals this machine's page size — on x86-64 with
   4 KiB pages, a pool spans four pages. The source's `SYSTEM_PAGE_SIZE` is hardcoded to
   4 KiB and is only used for the non-radix-tree fallback path.
2. **655,360 bytes of radix-tree map overhead at startup** — a fixed cost of the
   `address_in_range` mechanism that lets pymalloc tell "this pointer is mine" from "this
   pointer is libc's" in O(1). Two thirds of a megabyte before you allocate anything. It buys
   the large-pool support in return.
3. **The arena is allocated with `mmap`,** per the source comment: *"Arenas are allocated
   with `mmap()` on systems supporting anonymous memory mappings to reduce heap
   fragmentation."* pymalloc deliberately bypasses the `brk` heap for exactly the reason in
   §2.1.

### 13.3 The routing rule

```
  PyObject_Malloc(n)
      │
      ├── n ≤ 512  ──▶ pymalloc: size class ⌈n/16⌉, block from a 16 KiB pool
      │                          (never reaches libc; the arena is already mapped)
      │
      └── n > 512  ──▶ libc malloc(n) directly
```

**That rule, and the 1 MiB arena granularity, are the entire interface between Python and
everything in §2–§12.** Small objects — which is nearly all of them — never touch libc at
allocation time. Large ones always do.

### 13.4 pymalloc's return policy is the strictest of all

An arena is `munmap`ed back to the OS only when **every pool in it is free**. One live
32-byte object pins 1 MiB.

This is §2.1's problem again, one layer up, and doc 07 §14 measured its consequence
directly: **the same 20,000 surviving objects retained 211 MB or 22 MB of RSS depending
only on placement.** Doc 16 §5 covers the same ground from the object-layout side.

The operational corollary, which surprises people:

> **`malloc_trim(0)` cannot recover memory held by a partially-used pymalloc arena.** From
> libc's point of view the arena is a live 1 MiB allocation. glibc is right; pymalloc is
> right; the memory is still gone.

What *does* work: not creating the shape in the first place. Allocate long-lived objects
together and early (so they cluster into arenas that will never be freed anyway), keep
transient allocations transient, and prefer one big buffer to a million small objects when
the data is homogeneous — the `array` / `bytes` / NumPy advice from doc 35, which is really
this section's advice restated.

---

## 14. The 512-byte cliff, measured

§13.3 claims a routing rule. Here it is as a number.

**Method.** Allocate and immediately free tuples of *k* elements, sweeping *k* so that the
allocation size crosses 512 bytes. Tuples above 20 elements bypass CPython's tuple freelist,
so every iteration is a real allocator round trip. 300,000 iterations per point, median of 5
passes, run twice — once under `PYTHONMALLOC=pymalloc` (the default) and once under
`PYTHONMALLOC=malloc`, which removes pymalloc from the path entirely. *(measured)*

| *k* | `getsizeof` | **pymalloc** ns/alloc+free | **system malloc** ns/alloc+free |
|---|---|---|---|
| 52 | 464 | 73.3 | 91.7 |
| 53 | 472 | 74.1 | 95.9 |
| 54 | 480 | 77.9 | 95.3 |
| 55 | 488 | 76.5 | 95.6 |
| 56 | 496 | 78.8 | 95.4 |
| 57 | 504 | 79.3 | 96.1 |
| **58** | **512** | **80.5** | 99.2 |
| **59** | **520** | **99.7** ⟵ | 97.5 |
| 60 | 528 | 99.9 | 99.4 |
| 61 | 536 | 103.6 | 102.3 |
| 62 | 544 | 105.5 | 108.6 |
| 63 | 552 | 105.1 | 106.9 |

**The pymalloc column steps 80.5 → 99.7 ns between 512 and 520 bytes — a 19.2 ns, 24% jump
in one 8-byte increment**, against a smooth ~1 ns/step background trend. The system-malloc
column, over exactly the same sizes, does nothing at all: 99.2 → 97.5, inside the noise.

That is `SMALL_REQUEST_THRESHOLD` (§13.2), visible from Python, to the byte. Above it,
pymalloc forwards to libc and the two columns converge — as they must, since above 512 they
are running the same code.

**What the 19 ns buys.** It is the difference between "pop a block off a pool's free list"
and "call into libc's allocator, take its fast path, and come back." Both are fast; one is
2.4× the other on this workload.

**What to do with it.** Very little, honestly, and that is worth saying plainly. Do not
contort a data structure to stay under 512 bytes for 19 ns. The value of this measurement is
diagnostic:

- It **confirms the layer boundary is real and locatable**, so when you see an allocation
  profile change shape at a size threshold, you know what you are looking at.
- It tells you that **objects above ~512 bytes are libc's problem, not pymalloc's** — so
  everything in §5, §6, and §12 applies to them and nothing in §13.4 does. A service whose
  memory is in 4 KiB buffers should be tuning `MALLOC_MMAP_THRESHOLD_`, not thinking about
  pymalloc arenas.
- Conversely, a service whose memory is in millions of small objects is a **pymalloc arena
  retention** story, and no `LD_PRELOAD` will help it.

Knowing which of those two you are is most of the diagnosis. §18 makes it a procedure.

---

## 15. pymalloc vs the system allocator, measured

The complementary question: what does pymalloc actually buy, in aggregate?

**Method.** An allocation-heavy loop — 150 iterations of building and dropping a 20,000-tuple
list comprehension, ≈3 M tuples plus ≈3 M boxed integers per run. Warm-up pass discarded,
then median of 5 timed passes per process; 5 alternating process pairs. *(measured)*

```
cfg       median_ms   min     max    maxrss_MB
pymalloc      80.8    79.8    82.4      19.9
malloc       152.7   151.7   154.6      19.5
pymalloc      79.4    78.8    81.2      19.8
malloc       152.6   151.1   153.9      19.4
pymalloc      78.0    77.2    78.3      19.8
malloc       152.2   151.3   155.2      19.6
pymalloc      79.3    79.0    79.6      19.7
malloc       151.7   150.1   153.0      19.6
pymalloc      80.8    78.9    81.5      19.7
malloc       152.6   152.1   154.1      19.5
```

**Median 79.3 ms vs 152.4 ms — pymalloc is 1.92× faster on this workload, with a
between-pass spread under 3% on both arms.** That is about as clean a result as this machine
produces.

Two readings, and the second is the interesting one.

**On time.** The workload performs roughly 6 M alloc/free pairs (one tuple and one integer
per comprehension element, the integers above 256 being uncached). The 73.1 ms gap over
~6 M pairs is **≈12 ns of extra cost per allocation** when the request goes to libc instead
of pymalloc — consistent with the 19 ns cliff in §14, which measured a slightly larger object
on a slightly different path. The allocation count is derived, not instrumented, so treat
the 12 ns as an order-of-magnitude figure; the 1.92× ratio is the measured claim.

**On space: `maxrss` is identical — 19.7 vs 19.5 MB.** pymalloc did not save a byte here.

That deserves emphasis because it contradicts a common belief. **pymalloc is a throughput
optimisation, not a memory optimisation.** Its 16-byte size classes are *finer* than
libmalloc's quarter-power ladder in the 128–512 byte range (§8), so it wins slightly on
rounding; but it pays that back in pool headers, in the 655 KB radix tree (§13.2), and in
1 MiB arena granularity. On this workload the effects cancel almost exactly.

If you are chasing memory, pymalloc is not where the win is. If you are chasing allocation
throughput in a small-object workload, it is worth ~2×.

> **Provenance.** `PYTHONMALLOC=malloc` on macOS selects Apple's `libmalloc`, **not glibc**.
> The 1.92× is a pymalloc-vs-libmalloc number on arm64. The direction is expected to hold on
> glibc — pymalloc exists because the general-purpose allocators are slower for this access
> pattern — but **the magnitude is not transferable** and I did not measure it there.

---

## 16. mimalloc in the free-threaded build

Remove the GIL and the entire analysis changes, because pymalloc's central assumption —
that one thread allocates at a time — is gone.

PEP 703 states the problem and the choice:

> "CPython currently uses an internal allocator, pymalloc, which is optimized for small
> object allocation. **The pymalloc implementation is not thread-safe without the GIL.** This
> PEP proposes replacing pymalloc with mimalloc, a general-purpose thread-safe allocator with
> good performance, including for small allocations."

The interesting part is that thread-safety was not the only reason. Two more fell out:

> "Using mimalloc, with some modifications, also addresses two other issues related to
> removing the GIL. First, **traversing the internal mimalloc structures allows the garbage
> collector to find all Python objects without maintaining a linked list.** This is described
> in more detail in the garbage collection section. Second, **mimalloc heaps and allocations
> based on size class enable collections like dict to generally avoid acquiring locks during
> read-only operations.**"

Both are worth pausing on, because they are the kind of second-order design win that
justifies a dependency:

1. **The GC no longer needs the doubly-linked list threaded through every container object.**
   Under the GIL build, every GC-tracked object carries a `PyGC_Head` with two pointers, and
   the collector walks that list. mimalloc's page metadata already knows which blocks in a
   page are live, and the pages are typed by size class — so the collector can enumerate
   objects by walking *the allocator*. Doc 22 covers the consequences for collection.

2. **Size-class-homogeneous pages make lock-free reads safe.** A dict's key table can be read
   without a lock partly because a freed block is guaranteed to be reused only for another
   allocation of the same size class in the same page — so a racing reader that follows a
   stale pointer reads a *valid, correctly-typed-in-layout* object rather than arbitrary
   memory. This is a deliberate exploitation of an allocator invariant, and it is why
   free-threaded CPython could not simply have used any thread-safe allocator.

PEP 703 also adds a constraint that extension authors must now honour:

> "Python objects must be allocated through object allocation APIs, such as
> `PyType_GenericAlloc`, `PyObject_Malloc`, or other Python APIs that wrap those calls.
> Python objects should not be allocated through other APIs, such as raw calls to C's malloc
> or the C++ new operator. Additionally, `PyObject_Malloc` should be used only for allocating
> Python objects."

Under the GIL build, mixing these is a latent bug. Under free-threading, it breaks GC
enumeration — the object becomes invisible to the collector. Doc 17 covers what this means
for extension code.

Doc 16 §12 and doc 26 carry the free-threaded memory-layout consequences, including the +16
byte per-object header tax measured there. The allocator-level summary for this document:
**in the free-threaded build, §13.2–§13.4 do not apply at all** — there are no pymalloc
arenas, no 512-byte threshold, and no all-pools-free return rule. There is mimalloc, and §11
and §6.2 apply instead.

---

## 17. Observability: reading each allocator's mind

You cannot tune what you cannot see, and each allocator exposes a different window.

### 17.1 CPython

```python
import sys, tracemalloc, gc

sys._debugmallocstats()        # pymalloc arenas/pools/size classes → stderr (§13.2)

tracemalloc.start(25)
# ... workload ...
for stat in tracemalloc.take_snapshot().statistics("traceback")[:10]:
    print(stat)
```

Know the limits, per doc 07 §15.1: **`tracemalloc` understates RSS by 2.7–3.3×** on
small-object workloads, because it counts requested bytes and not headers, rounding, pool
overhead, or arena granularity. And it sees **nothing** a C extension allocates outside the
Python APIs. It answers "which Python line requested these bytes", which is a different and
also useful question.

### 17.2 glibc

```c
#include <malloc.h>
malloc_stats();          /* human-readable summary to stderr */
struct mallinfo2 mi = mallinfo2();
malloc_info(0, stdout);  /* XML, per-arena — the only per-arena view */
```

From `malloc_stats(3)`:

> "The `malloc_stats()` function prints (on standard error) statistics about memory allocated
> by `malloc(3)` and related functions."

`mallinfo2` replaced `mallinfo` precisely because the old struct used `int` fields that
overflowed past 2 GB — **if you find code calling `mallinfo()`, its numbers are wrong on any
modern heap.** Use `mallinfo2` (glibc 2.33+).

From Python, without writing C:

```python
import ctypes
libc = ctypes.CDLL("libc.so.6")
libc.malloc_stats()                      # → stderr
libc.malloc_trim(ctypes.c_size_t(0))     # → returns 1 if anything was released
```

`malloc_info(0, stdout)`'s XML is the one that answers "how many arenas do I have and how
much is each holding" — the §5.2 question. There is no other way to get it.

### 17.3 The others

```bash
# jemalloc — statistics dump at exit, plus heap profiles
MALLOC_CONF="stats_print:true" python app.py
MALLOC_CONF="prof:true,prof_prefix:/tmp/jeprof" python app.py
jeprof --show_bytes --pdf $(which python) /tmp/jeprof.*.heap > heap.pdf

# mimalloc
MIMALLOC_SHOW_STATS=1 python app.py
MIMALLOC_VERBOSE=1 python app.py

# tcmalloc — via MallocExtension, or the pprof-compatible heap profiler
```

**jemalloc's `jeprof` is the best tool in this document.** It gives you a call-graph
attribution of native heap bytes, which is the one thing `tracemalloc` structurally cannot
do. If your memory is in a C extension, this is how you find it. It is often worth deploying
jemalloc *purely to run the profiler once*, even if you then switch back.

### 17.4 Kernel-side

Doc 07 §10 and §15 are the reference. The two lines to know:

```bash
grep -E "^(Rss|Pss|Private_Dirty)" /proc/self/smaps_rollup   # Linux
```

`Private_Dirty` is the number that matters — pages only this process has, that have been
written, that cannot be reclaimed without swap. It is the numerator in §7.4.

---

## 18. A diagnosis ladder

"Python is using too much memory." Work down; stop when a rung answers it.

**1 — Is it actually Python?** `Private_Dirty` from `smaps_rollup` versus RSS versus VSZ
(doc 07 §10). If VSZ is huge and `Private_Dirty` is small, there is no problem; someone is
reading the wrong column. This rung resolves more incidents than the rest combined.

**2 — Is it growing, or is it high and flat?** Growing without bound over hours is a leak or
unbounded cache. High and flat is fragmentation or retention. **These have nothing in common
and the rest of the ladder assumes flat.**

**3 — Is it above or below 512 bytes?** `sys._debugmallocstats()` gives pymalloc's total. If
pymalloc's arenas account for most of RSS, you are in §13.4 — small objects, arena retention,
and no allocator swap will help. If they account for little, the memory is in libc, and §5–§6
apply. **This is the fork in the road** and §14 is why the threshold is exactly where it is.

**4 — Does `malloc_trim(0)` recover it?** One `ctypes` call (§17.2). If RSS drops, it was
retention (§7.3): tune `M_TRIM_THRESHOLD` / `M_MMAP_THRESHOLD_`, or call `malloc_trim` at
batch boundaries, and you are done. If it does not, continue.

**5 — How many arenas?** `malloc_info(0, stdout)` (§17.2). If the count is large and each
holds a similar amount, you have arena multiplication (§5.2). Try `MALLOC_ARENA_MAX=2` and
measure both RSS *and* latency. Note the glibc 2.44 change — on a current libc the default
cap is already 8× lower than the advice you will find online.

**6 — Is it one big allocation size?** If a buffer size crossed 128 KiB and the dynamic mmap
threshold learned it (§2.3), those allocations are now heap-resident and non-returnable.
`MALLOC_MMAP_THRESHOLD_=131072` freezes it. This is a specific, checkable, one-line fix and it
is under-diagnosed.

**7 — Where are the bytes, natively?** Deploy jemalloc with `prof:true` and run `jeprof`
(§17.3). This is the rung that finds C-extension leaks, which `tracemalloc` cannot see at all.

**8 — Only now, change allocator.** jemalloc with decay tuning for an RSS problem; tcmalloc or
mimalloc for a throughput problem. Measure both dimensions, under production-shaped load, for
long enough to reach steady state.

**9 — If nothing works, change the shape of the data.** A million small objects will retain
arenas under any allocator (§7.2, doc 07 §14). One `array`, one `bytes`, one NumPy buffer, or
one `__slots__` class will not. Doc 35 is the reference. **This rung is the real fix more
often than rungs 4–8 are**, and it is the one people try last.

---

## 19. The cost model

Everything above, as numbers to reason with.

| Fact | Value | Source |
|---|---|---|
| pymalloc small-object alloc+free | **~80 ns** (tuple, ≤512 B, this machine) | measured, §14 |
| libmalloc same operation | **~99 ns** | measured, §14 |
| The 512-byte cliff | **+19.2 ns, +24%**, in one 8-byte step | measured, §14 |
| pymalloc vs libmalloc, alloc-heavy loop | **1.92×** faster | measured, §15 |
| pymalloc vs libmalloc, RSS | **no difference** (19.7 vs 19.5 MB) | measured, §15 |
| Derived per-allocation delta | **≈12 ns** | derived, §15 |
| libmalloc size classes ≤ 256 KiB | **54** | measured, §8 |
| Mean internal waste, uniform 1–1024 B | **42.5 B, 8.3%** | measured, §8 |
| `malloc(1)`, macOS / glibc | **16 B** / **32 B** | measured §8 / source §3.2 |
| glibc per-allocation overhead | **8 B** best case, 16 B header | source, §3.2 |
| glibc size-class step | **16 B** | source, §3.2 |
| glibc small/large boundary | **1024 B** | source, §4.1 |
| glibc tcache | **64 small + 12 large bins, 16 chunks each** | source, §4.2 |
| glibc mmap threshold | **128 KiB**, dynamic, max **32 MiB** | source, §2.3 |
| glibc trim threshold / top pad | **128 KiB** each | man page, §6.1 |
| glibc secondary heap reservation | **64 MiB** each | source, §5.1 |
| glibc arena cap, ≤ 2.43 | **8 × ncores** | source, §5.2 |
| glibc arena cap, **2.44+** | **1 × ncores** | source, §5.2 |
| pymalloc threshold / classes / alignment | **512 B / 32 / 16 B** | source + measured, §13.2 |
| pymalloc pool / arena | **16 KiB / 1 MiB** | source + measured, §13.2 |
| pymalloc radix-tree fixed overhead | **655,360 B** at startup | measured, §13.2 |
| pymalloc arena release rule | **all pools free, or nothing** | source, §13.4 |

**Five sentences to remember:**

1. **`malloc` is not a system call**, and the allocator that answered it is holding your
   memory on purpose.
2. **The `brk` heap is a stack**: one live object on top pins everything below it, and no
   amount of freeing changes that.
3. **glibc returns memory by shape; jemalloc, tcmalloc, and mimalloc return it by clock** —
   that one row of §6.2 explains most of the observed difference between them.
4. **pymalloc is a speed optimisation, not a space one** — 1.92× faster, 0% smaller.
5. **Below 512 bytes it is pymalloc's problem; above 512 bytes it is libc's** — and knowing
   which one you have is most of the diagnosis.

---

## 20. Lab exercises

**1 — Enumerate your allocator's size classes.** Redo §8 on your platform. On glibc use
`malloc_usable_size()`; on macOS `malloc_size()`. Plot usable-vs-requested and find the
quantum doublings. *Proves §3.2 and §8 — and the ladder you get is the one your service is
actually paying for, which no blog post can tell you.*

**2 — Find your 512-byte cliff.** Reproduce §14 with `PYTHONMALLOC=pymalloc` and
`PYTHONMALLOC=malloc`. Confirm the step appears in one column and not the other. *Proves
§13.3 from the outside, with no C.*

**3 — Make the `brk` heap refuse to shrink.** In C: `malloc` 1000 chunks of 1 KiB, free all
but the last, call `malloc_trim(0)`, and watch RSS. Then free the last one and trim again.
*Proves §2.1 and §6.1 — the most viscerally convincing five minutes in this document.*

**4 — Trigger the dynamic mmap threshold.** Allocate and free a 4 MiB buffer once, then
allocate and free 100 more and watch RSS. Repeat with `MALLOC_MMAP_THRESHOLD_=131072`.
*Proves §2.3 — a real production bug you can create on purpose in ten lines.*

**5 — Multiply your arenas.** Run 64 threads each doing `malloc(1024)`/`free` in a loop
(from Python, via a C extension or `ctypes` — the GIL must not serialise it). Compare RSS
at `MALLOC_ARENA_MAX` unset, 8, and 1. Then check your glibc version against §5.2's table
and predict the result before you run it. *Proves §5.2, and tests whether the folklore you
inherited is still true.*

**6 — Measure the decay trade-off.** Run the same workload under jemalloc with
`dirty_decay_ms` at 0, 1000, 10000, and −1. Plot RSS and p99 latency against each other.
*Proves §6.2 and §9.2, and produces the only chart that can justify an allocator change to
someone else.*

**7 — Find a C-extension leak with `jeprof`.** Allocate a large NumPy array in a loop
without releasing it, profile with jemalloc, and confirm `tracemalloc` shows nothing while
`jeprof` shows everything. *Proves §17.1's limitation and §17.3's value — and it is the
single most useful skill in this document.*

**8 — Pin an arena with one object.** Create 1,000,000 small objects, keep every 10,000th,
drop the rest, and read `sys._debugmallocstats()`. Count arenas that are nearly empty but
not free. Then redo it keeping the *first* 100 instead of every 10,000th. *Proves §13.4 and
doc 07 §14 — placement, not count.*

**9 — Compare all four.** Take a real workload and run it under glibc, jemalloc, tcmalloc,
and mimalloc via `LD_PRELOAD`. Record RSS *and* throughput *and* p99. Resist the urge to
declare a winner from one workload. *Proves §12.3, and §11.4's warning about benchmarks.*

---

## 21. Question bank

**Fundamentals**

1. Why is `malloc` not a system call? What would go wrong if it were?
2. Draw the four layers of §1 and say which ones can shrink your RSS.
3. What are the two ways a process gets anonymous memory from the kernel, and what is the
   single structural difference that matters?
4. Why can the `brk` heap only be trimmed at the top?

**glibc**

5. How many bytes does `malloc(1)` cost on 64-bit glibc, and where does the number come from?
6. Why is glibc's per-allocation overhead 8 bytes when the chunk header is 16?
7. What is the `PREV_INUSE` bit for, and what would break without it?
8. Why do fastbins not coalesce? What is being traded for what?
9. Why is the unsorted bin a good idea, and what latency behaviour does it cause?
10. What is the small/large boundary, and why do the two bin families need different search
    strategies?
11. Your service's RSS grows to 8× a single thread's working set on a 64-core box. Name the
    mechanism and two fixes.
12. Why does `MALLOC_ARENA_MAX=2` reduce memory, and what does it cost?
13. What changed about the arena cap in glibc 2.44, and how would you check which behaviour
    your production box has?
14. A colleague sets a 4 MiB buffer size and RSS grows by 400 MB that never comes back.
    Explain, then fix it in one environment variable.
15. What does `malloc_trim(0)` do that trimming-on-`free` does not?

**Fragmentation**

16. Distinguish internal fragmentation, external fragmentation, and allocator retention. Give
    the observable signature of each.
17. Your bloat factor is 2.1× and stable. What do you try first, and why that?
18. Why are powers of two often the *worst* allocation sizes?
19. What did Johnstone & Wilson conclude, and how should it change your priors?

**The others**

20. What is jemalloc's dirty/muzzy/clean pipeline, and which `madvise` call moves a page
    between each pair?
21. Why does jemalloc's decay model return memory in situations where glibc's trim threshold
    does not?
22. You deploy jemalloc under gunicorn and RSS does not improve. What is the first thing to
    check?
23. What problem do restartable sequences solve for tcmalloc, and why is it Linux-only?
24. What is free-list *multi*-sharding, and what specific operation does it make cheap?
25. Why does mimalloc's sharding improve *purging*, not just contention?
26. Why is glibc's boundary-tag design a security liability that mimalloc's is not?

**CPython**

27. Name CPython's three allocation domains and what each is for. What happens if you mix
    them?
28. What is `SMALL_REQUEST_THRESHOLD` and what is on each side of it?
29. Why are pymalloc arenas allocated with `mmap` rather than `malloc`?
30. What is the 655 KB the radix tree costs at startup buying you?
31. When is a pymalloc arena returned to libc? Why is that rule so strict?
32. Why can't `malloc_trim(0)` recover memory held by a partially-used pymalloc arena?
33. pymalloc is 1.92× faster than the system allocator on a small-object workload and uses
    the same RSS. Explain both halves.
34. Give three reasons PEP 703 chose mimalloc, only one of which is thread-safety.
35. Why does free-threaded CPython require objects to be allocated through the Python APIs
    rather than raw `malloc`?
36. Your service holds 4 GB of RSS with 800 MB of live Python objects. Walk the §18 ladder
    out loud.

---

## 22. What I could not verify

**Everything in §3, §4, §5, §6, §9, §10, and §12.2 is researched, not measured.** This
machine is macOS on arm64 and does not run glibc, jemalloc, tcmalloc, or Linux `mmap`
semantics. I read primary sources — the glibc development tree at 2.44.9000, the man pages,
the jemalloc/tcmalloc/mimalloc project documentation — and attributed inline. Specifically:

1. **No glibc number here is mine.** Every glibc constant is quoted with file and line from
   the source I read on 2 August 2026, or from the `mallopt(3)` / `malloc_trim(3)` /
   `malloc(3)` man pages. I did not compile or run glibc.

2. **The glibc 2.44 arena-cap change is a source reading, not a behavioural observation.** I
   verified it by reading `malloc/arena.c` at each release tag from 2.39 to 2.44 and
   confirming where `NARENAS_FROM_NCORES` disappears. I did not run a 2.43 and a 2.44 binary
   side by side, and I did not find a `NEWS` entry announcing the change — which is itself a
   reason to double-check before acting on it. **If you are tuning `MALLOC_ARENA_MAX` in
   production, verify against your actual libc rather than trusting this table.**

3. **The tcache constants are from the development tree, not a release.** `TCACHE_FILL_COUNT
   16` and the 12 large bins are current `HEAD`; the large-bin feature landed in 2.42 per
   `NEWS`, but I did not check whether the fill count of 16 shipped in a release or is
   unreleased. Anything running an older glibc has 64 bins × 7.

4. **§8 and §15 are macOS `libmalloc` numbers wearing a "system allocator" label.** The size
   classes, the 8.3% waste figure, and the 1.92× ratio are all Apple's allocator on arm64. I
   expect the *direction* of §15 to hold on glibc and the *shape* of §8's ladder to be
   familiar, but **neither magnitude transfers** and I did not measure either on Linux.

5. **`MallocNanoZone=0` changed nothing measurable** (152.7 vs 152.1 ms median, three pairs)
   and the default zone reported as `DefaultMallocZone` rather than a nano zone. I do not
   know whether the nano zone is disabled on this configuration, is not used for these sizes,
   or is used but is not the bottleneck. §8's ladder is therefore "what this process's
   default zone did", not "what Apple's nano allocator does".

6. **The ≈12 ns per-allocation figure in §15 is derived, not instrumented.** I estimated the
   allocation count from the workload's shape (one tuple plus one uncached integer per
   element) rather than counting allocations. If the real count is 4 M rather than 6 M the
   figure is 18 ns, not 12. The 1.92× ratio does not depend on this.

7. **§7.4's bloat-factor bands are experience, not measurement.** The 1.3–1.6× / 2× / 3×
   thresholds are a rule of thumb I find useful, not a result. Your workload's baseline could
   legitimately sit anywhere.

8. **I did not measure any allocator swap.** Every claim about what jemalloc, tcmalloc, or
   mimalloc would do to a Python service's RSS is from project documentation and design
   reasoning. §12.3 exists precisely because I do not think those claims should be acted on
   without your own measurement.

9. **mimalloc's benchmark claims are from a page dated 2021-01-30**, as the README itself
   notes. I have not independently compared any of the four allocators, and §11.4 says so.

10. **§16's claims about GC enumeration and lock-free dict reads are PEP 703's**, quoted
    directly. I did not read the free-threaded implementation to confirm the mechanism works
    as described, and this build has `Py_GIL_DISABLED=0`, so nothing in §16 was exercised
    here.

---

## 23. Sources

**Primary — source code** (read directly on 2026-08-02, with file and line cited inline)

- [glibc `malloc/malloc.c`](https://sourceware.org/git/?p=glibc.git;a=blob_plain;f=malloc/malloc.c;hb=HEAD) — *Verdict:* the ground truth for §3 and §4, and far more readable than its reputation; the comment blocks at the top are a design document in their own right. Version read: 2.44.9000.
- [glibc `malloc/arena.c`](https://sourceware.org/git/?p=glibc.git;a=blob_plain;f=malloc/arena.c;hb=HEAD) — *Verdict:* short, and `arena_get2` is the only place that will tell you the truth about the arena cap (§5.2). Read it before believing any tuning advice.
- [glibc `NEWS`](https://sourceware.org/git/?p=glibc.git;a=blob_plain;f=NEWS;hb=HEAD) — *Verdict:* the only record of when malloc behaviour changed; grep it for `tcache` and `malloc` before diagnosing anything version-dependent.
- [CPython `Include/internal/pycore_obmalloc.h`](https://github.com/python/cpython/blob/main/Include/internal/pycore_obmalloc.h) — *Verdict:* every pymalloc constant in §13, with the tunables clearly marked and the 1998 design commentary still in place and still accurate.
- [CPython `Objects/obmalloc.c`](https://github.com/python/cpython/blob/main/Objects/obmalloc.c) — *Verdict:* the domain plumbing and the arena lifecycle; the `usable_arenas` sorting logic is the §13.4 return rule in code.

**Primary — official documentation**

- [glibc wiki: MallocInternals](https://sourceware.org/glibc/wiki/MallocInternals) — *Verdict:* the best single explanation of glibc malloc anywhere, with the diagrams. **Caveat: its arena-cap statement is now stale (§5.2)** — a good reminder that wikis lag source.
- [`mallopt(3)`](https://man7.org/linux/man-pages/man3/mallopt.3.html) — *Verdict:* the specification of every glibc tunable and the reasoning behind each default; the `M_MMAP_THRESHOLD` dynamic-adjustment note in §2.3 is worth the read on its own.
- [`malloc_trim(3)`](https://man7.org/linux/man-pages/man3/malloc_trim.3.html) — *Verdict:* four paragraphs that overturn what most people believe about `malloc_trim`; the "since glibc 2.8" note is the important one.
- [`brk(2)`](https://man7.org/linux/man-pages/man2/brk.2.html) — *Verdict:* short and historically clarifying; read it once to understand why every modern allocator prefers `mmap`.
- [`malloc(3)`](https://man7.org/linux/man-pages/man3/malloc.3.html) — *Verdict:* consult, don't read; the environment-variable section is the useful part.
- [`mallinfo2(3)`](https://man7.org/linux/man-pages/man3/mallinfo2.3.html), [`malloc_stats(3)`](https://man7.org/linux/man-pages/man3/malloc_stats.3.html) — *Verdict:* §17.2's reference; the `mallinfo` → `mallinfo2` overflow story is a good cautionary tale about `int` in APIs.
- [jemalloc(3)](https://jemalloc.net/jemalloc.3.html) — *Verdict:* enormous, and the definitive reference for the `MALLCTL` namespace; navigate by search, never read linearly. The IMPLEMENTATION NOTES section is the design summary.
- [jemalloc `TUNING.md`](https://github.com/jemalloc/jemalloc/blob/dev/TUNING.md) — *Verdict:* two screens, and the highest ratio of actionable advice to words in this entire list. Read it before touching `MALLOC_CONF`.
- [TCMalloc design doc](https://github.com/google/tcmalloc/blob/master/docs/design.md) — *Verdict:* the clearest exposition of a three-tier allocator in print; §10's structure is entirely from here.
- [TCMalloc rseq doc](https://github.com/google/tcmalloc/blob/master/docs/rseq.md) — *Verdict:* the best explanation of restartable sequences aimed at people who are not kernel developers.
- [Temeraire: Hugepage-Aware Allocator](https://google.github.io/tcmalloc/temeraire.html) — *Verdict:* read alongside doc 07 §11; the "half a hugepage was picked arbitrarily" admission is a healthy corrective to allocator mystique.
- [TCMalloc tuning](https://github.com/google/tcmalloc/blob/master/docs/tuning.md) — *Verdict:* short, and its "none of these tuning parameters are clear wins" framing should be quoted at anyone bearing a config snippet.
- [mimalloc README](https://github.com/microsoft/mimalloc) and [`doc/mimalloc-doc.h`](https://github.com/microsoft/mimalloc/blob/dev/doc/mimalloc-doc.h) — *Verdict:* the design bullets in §11.1 are the densest statement of the idea; the doc header also carries the complete option list, which the README does not.
- [PEP 703 — Making the GIL Optional](https://peps.python.org/pep-0703/) — *Verdict:* the Memory Management section is short and is the authoritative statement of why free-threaded CPython uses mimalloc; §16 is built on it.
- [CPython — Memory Management (C API)](https://docs.python.org/3/c-api/memory.html) — *Verdict:* the domain table in §13.1 and the `PYTHONMALLOC` matrix; the debug-hooks section is the reference for `-X dev`.

**Papers**

- **The Memory Fragmentation Problem: Solved?**, Johnstone & Wilson, ISMM '98 — *Verdict:* 📖 the paper that should calm you down before you rewrite anything; its finding that real programs fragment far less than folklore claims is §7.4's prior.
- **Dynamic Storage Allocation: A Survey and Critical Review**, Wilson, Johnstone, Neely & Boles, IWMM '95 — *Verdict:* 🔍 the taxonomy everything else in this area uses; long, and the survey half is the part to read.
- [**Mesh: Compacting Memory Management for C/C++ Applications**](https://arxiv.org/abs/1902.04738), Powers, Tench, Berger & McGregor, PLDI '19 — *Verdict:* 📖 the one genuinely new idea in this space in a decade — compaction *without* moving objects, by remapping virtual pages onto shared physical pages. Not something you will deploy, but it will change how you think about §7.2.
- **Mimalloc: Free List Sharding in Action**, Leijen, Zorn & de Moura, MSR-TR-2019-18 — *Verdict:* 🔍 the design rationale behind §11 in full, including why the sharding is *multi*-sharded.

**Books** (see [BOOKS.md](BOOKS.md) for the folder's full verdicts)

- **The Linux Programming Interface**, Kerrisk — *Verdict:* 🔍 reference. Ch. 7 (memory allocation) is the chapter for §2 and §6; short and exact.
- **Systems Performance** 2e, Gregg — *Verdict:* 📖 ch. 7 (Memory) is the operational counterpart; the allocator section is brief but the methodology in §18 is descended from it.
- **What Every Programmer Should Know About Memory**, Drepper — *Verdict:* 🆓 dated on hardware specifics, still the best explanation of *why* locality-preserving allocation (§11.1) matters at all.
- **CPython Internals**, Shaw — *Verdict:* 📖 the pymalloc chapter is the gentlest introduction to §13; go to the source afterwards, because the constants have changed.
