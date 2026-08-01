# 16 — Object memory layout: headers, pymalloc, and the real cost of an object

> **Tier 2, doc 16.** Prerequisites: [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
> (cache lines, pointer chasing), [`14-pyobject-and-types.md`](14-pyobject-and-types.md),
> [`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md). Feeds into:
> [`22-garbage-collection.md`](22-garbage-collection.md), [`26-free-threading.md`](26-free-threading.md),
> [`33-optimizing-python.md`](33-optimizing-python.md), [`35-memory-optimization.md`](35-memory-optimization.md).
>
> **THESIS: in CPython, the object header is the dominant memory cost, and `sys.getsizeof`
> lies to you about it.** An `int` holding the value `1` costs 28 bytes on a normal build —
> 24 of them header. Ten million of them cost you a header-dominated 280 MB before the
> list holding the pointers. Memory optimization in Python is therefore almost never about
> your data; it is about **how many objects exist** and **what shape their headers are**.
> Everything in this document is measured, not recalled.
>
> **Two claims here contradict what you will read everywhere else**, and both are measured
> rather than argued: pymalloc's 16 KB pool has nothing to do with this machine's 16 KB
> page (§3), and `__slots__` saves ~30%, not ~600% (§8, §9). In both cases the popular
> version is what you get from a plausible measurement made with the wrong instrument.

> **Measurement provenance.** Every number below was produced on the machine this repo
> lives on: **Apple M3 Pro, macOS, arm64, CPython 3.14.6** (and **3.14.6 free-threading
> build** where marked), 128-byte cache lines, 16 KB pages. Numbers marked *(measured)*
> came out of a live interpreter during the writing of this document. Anything I could not
> verify is flagged in place. **Re-run the labs on your own build — several of these
> constants have changed within the last three releases.**

## Contents

1. [The three headers](#1-the-three-headers)
2. [The free-threading header tax — measured](#2-the-free-threading-header-tax--measured)
3. [pymalloc: arena → pool → block](#3-pymalloc-arena--pool--block)
4. [The three allocation domains](#4-the-three-allocation-domains)
5. [Why freed memory does not come back](#5-why-freed-memory-does-not-come-back)
6. [Per-type layouts, measured](#6-per-type-layouts-measured)
7. [List overallocation](#7-list-overallocation)
8. [The compact dict and key sharing](#8-the-compact-dict-and-key-sharing)
9. [`__slots__` — a smaller win than you've been told](#9-__slots__--a-smaller-win-than-youve-been-told-for-the-right-reason)
10. [Caching and interning](#10-caching-and-interning)
11. [`sys.getsizeof` lies — and a correct deep sizer](#11-sysgetsizeof-lies--and-a-correct-deep-sizer)
12. [mimalloc in the free-threaded build](#12-mimalloc-in-the-free-threaded-build)
13. [Budgeting: cost per million objects](#13-budgeting-cost-per-million-objects)
14. [Lab exercises](#14-lab-exercises)
15. [Question bank](#15-question-bank)
16. [Sources](#16-sources)

---

## 1. The three headers

Every Python object carries a header. There are three layers of it, and which ones you pay
for depends on the type.

```
        ┌──────────────────────────────────────────────┐
        │  PyGC_Head        (16 B)  ← ONLY if the type  │  Not part of the
        │    _gc_next  (8 B)          is GC-tracked     │  object pointer;
        │    _gc_prev  (8 B)                            │  sits BEFORE it.
        ├══════════════════════════════════════════════┤  ← PyObject* points here
        │  PyObject_HEAD    (16 B)  ← EVERY object      │
        │    ob_refcnt (8 B)                            │
        │    ob_type   (8 B)                            │
        ├──────────────────────────────────────────────┤
        │  ob_size     (8 B)        ← only PyVarObject  │
        │                             (list, tuple,     │
        │                              bytes, int, str) │
        ├──────────────────────────────────────────────┤
        │  ... the actual payload ...                   │
        └──────────────────────────────────────────────┘
```

In C (`Include/object.h`, standard build):

```c
typedef struct _object {
    Py_ssize_t ob_refcnt;      /* offset 0 — see §2 and 24-the-gil.md §1 */
    PyTypeObject *ob_type;
} PyObject;

typedef struct {
    PyObject ob_base;
    Py_ssize_t ob_size;        /* number of items, for variable-size objects */
} PyVarObject;
```

**Measured base costs, CPython 3.14.6, arm64** *(measured)*:

| Object | `getsizeof` | What's in it |
|---|---|---|
| `object()` | **16** | header only, no payload |
| `None` | 16 | header only (a singleton) |
| `float` | 24 | 16 header + 8 `double` |
| `int` 0 or 1 | 28 | 24 (`PyVarObject`) + 4 (one 30-bit digit) |
| `int` 2**30 | 32 | two digits |
| `int` 2**60 | 36 | three digits |
| `int` 2**300 | 68 | eleven digits |
| `complex` | 32 | 16 + two doubles |

Two things to notice immediately:

**`ob_refcnt` is at offset 0.** The single most frequently written word in the process
sits at the front of every object. That placement is the physical root of the GIL — see
[`24-the-gil.md`](24-the-gil.md) §1 and §10.2 of
[`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md).

**A `float` costs 24 bytes to hold 8 bytes of data.** 3× overhead, and that's the *best*
case among scalars. This is the number that justifies NumPy's existence.

### Which objects are GC-tracked

Only objects that can *participate in a reference cycle* carry `PyGC_Head`. Measured
*(measured)*:

| Object | `gc.is_tracked` |
|---|---|
| `1` | False |
| `"x"` | False |
| `(1, 2)` | **False** — tuple of immutables gets *untracked* |
| `([],)` | True — contains a mutable |
| `[]`, `{}` | True |
| instance of a plain class | True |

That `(1, 2)` is untracked is the **tuple untracking optimization**: the collector checks
tuples at collection time and permanently untracks any whose contents cannot form a cycle.
It saves 16 bytes *and* removes the tuple from every future traversal. See
[`22-garbage-collection.md`](22-garbage-collection.md).

### Where an instance's size actually comes from

The type object carries the layout. Three fields decide everything:

| Field | Python-visible | Meaning |
|---|---|---|
| `tp_basicsize` | `T.__basicsize__` | bytes for one instance, excluding variable part |
| `tp_itemsize` | `T.__itemsize__` | bytes per element for `PyVarObject` types (0 otherwise) |
| `tp_dictoffset` | `T.__dictoffset__` | where `__dict__` lives; **`-1` means "managed" — see §8** |

Measured for a plain 3-attribute class *(measured)*:

```python
>>> class Plain:
...     def __init__(self): self.x = 1; self.y = 2; self.z = 3
>>> Plain.__basicsize__, Plain.__dictoffset__
(16, -1)
>>> sys.getsizeof(Plain())
48
```

**A 16-byte basic size for an object holding three attributes.** That is not a rounding
error and the attributes are not free — `getsizeof` is reporting a number that has almost
nothing to do with what the instance costs. §8 explains where the rest lives, and §11
explains why the instrument can't see it.

---

## 2. The free-threading header tax — measured

This is the finding worth the price of the document, and it is not widely discussed.

**On the free-threaded build, `PyObject` is 16 bytes larger.** Biased reference counting
(PEP 703, and [`24-the-gil.md`](24-the-gil.md) §8.2) replaces the single `ob_refcnt` with
an owner thread id plus two separate counts:

```c
/* free-threaded build — conceptual layout; verify against Include/object.h
   for your build, the exact field set has moved between 3.13 and 3.14 */
uintptr_t ob_tid;          /* owning thread id — the fast-path check */
uint32_t  ob_ref_local;    /* non-atomic, owner only. UINT32_MAX == immortal */
Py_ssize_t ob_ref_shared;  /* atomic, everyone else */
PyTypeObject *ob_type;
```

Measured side by side, same machine, 3.14.6 GIL build vs 3.14.6t *(measured)*:

| Object | GIL build | Free-threaded | Δ | GC-tracked? |
|---|---|---|---|---|
| `object()` | 16 | **32** | **+16** | no |
| `None` | 16 | 32 | +16 | no |
| `float` | 24 | 40 | +16 | no |
| `int` 1 | 28 | 44 | +16 | no |
| `int` 2**300 | 68 | 84 | +16 | no |
| `b""` | 33 | 49 | +16 | no |
| `""` | 41 | 57 | +16 | no |
| `bytearray()` | 56 | 72 | +16 | no |
| `[]` | 56 | **56** | **0** | **yes** |
| `{}` | 64 | **64** | **0** | **yes** |
| `()` | 48 | 48 | 0 | yes |
| `set()` | 216 | 216 | 0 | yes |

**Read the last column.** Every *non*-GC-tracked object grew by exactly 16 bytes. Every
GC-*tracked* object stayed identical. That is not a coincidence:

> In the free-threaded build the 16-byte `PyGC_Head` is **gone**. GC state lives in the
> mimalloc page metadata instead (§12), which is precisely why mimalloc was a
> load-bearing choice and not merely "a thread-safe allocator". So containers pay
> **+16 for the bigger header, −16 for the removed GC head — a wash.**

**The practical consequence:** a workload dominated by scalars — ints, floats, strings —
pays a real, roughly **35–60% memory increase on those objects** when you move to
free-threading. A workload dominated by containers pays close to nothing. Nobody puts this
in the migration guides, and it is exactly the kind of thing that turns a free-threading
experiment into an OOM incident.

> **Caveat.** I verified the *sizes* directly on 3.14.6t. I did **not** verify the exact
> struct field names against the 3.14 source while writing this — the field set changed
> between 3.13 and 3.14. Treat the C snippet above as a sketch and check
> `Include/object.h` for your build. The measured sizes are solid.

---

## 3. pymalloc: arena → pool → block

`malloc` is too slow and too fragmenting for objects that are allocated and freed millions
of times a second. CPython therefore ships **pymalloc** (`Objects/obmalloc.c`), a
size-class allocator layered on top of `mmap`.

Three nested levels — **with the real constants from this machine** *(measured, via
`sys._debugmallocstats()` on 3.14.6/arm64)*:

```
  ARENA — 1,048,576 bytes (1 MB), mmap'd from the OS
  ┌────────────────────────────────────────────────────────────────┐
  │ POOL   │ POOL   │ POOL   │ POOL   │  ...  │ POOL   │  64 pools │
  │ 16 KB  │ 16 KB  │ 16 KB  │ 16 KB  │       │ 16 KB  │  per arena│
  └───┬────┴────────┴────────┴────────┴───────┴────────┴───────────┘
      │
      ▼  Each pool is dedicated to ONE size class for its lifetime.
  ┌──────────────────────────────────────────────────────────────┐
  │ pool header │ blk │ blk │ blk │ blk │ blk │ ... │ blk │      │
  │  (~48 B)    │ 64B │ 64B │ 64B │ 64B │ 64B │     │ 64B │      │
  └──────────────────────────────────────────────────────────────┘
                   ▲
                   └── free blocks are threaded into a singly-linked
                       free list *stored inside the free blocks themselves*
                       → allocation is a pointer pop. No search.
```

**Verified constants on this build** *(measured)*:

| Constant | Value | Note |
|---|---|---|
| Small-block threshold | **512 bytes** | above this → straight to `malloc` |
| Size classes | **32** | 16, 32, 48, … 512 |
| Granularity | **16 bytes** | requests round *up*; a 17-byte request costs 32 |
| Pool size | **16,384 bytes (16 KB)** | |
| Arena size | **1,048,576 bytes (1 MB)** | |
| Pools per arena | **64** | |

> **Two constants worth flagging, because most references are out of date.** The classic
> figures quoted everywhere are a 256 KB arena and a 4 KB pool. On this build they are
> **1 MB and 16 KB**. It is tempting to attribute that to this machine's 16 KB page size.
> **That is wrong**, and the source says so — `Include/internal/pycore_obmalloc.h`:
>
> ```c
> #if SIZEOF_VOID_P > 4
> #define USE_LARGE_ARENAS            /* ARENA_BITS 20 → 1 MiB  */
> #if WITH_PYMALLOC_RADIX_TREE
> #define USE_LARGE_POOLS             /* POOL_BITS  14 → 16 KiB */
> #endif
> #endif
> ```
>
> The switch is **64-bit-ness plus the radix tree**, not the OS page size. x86-64 Linux —
> 4 KB pages — gets the same 1 MB / 16 KB. `SYSTEM_PAGE_SIZE` is still hardcoded to 4096
> in that header and is now only consulted in the legacy path. The 256 KB / 4 KB figures
> you'll find in every blog post are the **32-bit / no-radix-tree fallback**, not "the x86
> numbers". Run `sys._debugmallocstats()` on your own target anyway — but expect it to
> agree with this machine.

### The radix tree, and why pools stopped being pages

`free(p)` has to answer one question first: *is `p` mine, or does it belong to the system
`malloc`?* pymalloc's old answer (`address_in_range`) read the pool header at
`p & ~(POOL_SIZE-1)` and checked an arena index — a trick that is only safe if a pool
never straddles a page pymalloc doesn't own, hence the historical `POOL_SIZE ==
SYSTEM_PAGE_SIZE` requirement (and a famous, genuinely rare segfault risk).

Since 3.10 the default build (`WITH_PYMALLOC_RADIX_TREE`) instead keeps a three-level
radix tree of arena addresses, so "is this pointer mine" is a tree lookup on the top bits
of the address. That is what **decoupled pool size from page size** and allowed 16 KB
pools and 1 MB arenas everywhere. Bigger pools mean fewer arena-management operations per
allocation and less pool-header overhead per byte — the reason the constants moved at all.

**The transferable point:** an allocator's fast path is usually limited by how cheaply it
can classify a pointer, not by how cheaply it can find a free block.

Live output from this machine, lightly trimmed *(measured)*:

```
Small block threshold = 512, in 32 size classes.

class   size   num pools   blocks in use  avail blocks
    0     16           1              27           994
    1     32           2             553           467
    2     48           8            2488           232
    3     64          24            5907           213
    4     80          19            3808            68
   ...
2 arenas * 1048576 bytes/arena     =            2,097,152
# bytes in allocated blocks        =            1,425,696
23 unused pools * 16384 bytes      =              376,832
# bytes lost to pool headers       =                5,040
```

Notice size class 3 (64 bytes) holding 5,907 live blocks in 24 pools — that's the busiest
class in a bare interpreter. **Rounding matters:** an object of 65 bytes lands in the
80-byte class and wastes 15 bytes, forever. Shaving one attribute off a hot class can drop
it a size class and cut real memory more than the attribute's own size.

---

## 4. The three allocation domains

CPython exposes three allocator domains, and **mixing them is undefined behaviour** — a
classic C-extension crash that manifests as a corrupted heap far from the actual bug.

| Domain | API | Backed by | Use for |
|---|---|---|---|
| Raw | `PyMem_RawMalloc/Free` | `malloc` directly | memory used without the GIL held; large buffers |
| Mem | `PyMem_Malloc/Free` | pymalloc | general-purpose non-object buffers |
| Object | `PyObject_Malloc/Free` | pymalloc | `PyObject` allocations |

The rule: **free with the same domain you allocated with.** `PyObject_Malloc` +
`PyMem_RawFree` is a bug even though both may bottom out in the same allocator today.

Debug them with `PYTHONMALLOC=debug`, which installs guard bytes around every allocation
and detects domain mismatches and over/underruns at the point of free:

```console
$ PYTHONMALLOC=debug python3.14 your_extension_test.py
```

That environment variable is the single highest-value debugging tool for native extension
work. See [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md).

---

## 5. Why freed memory does not come back

The question every Python service owner eventually asks: *"I deleted the objects, RSS
didn't drop. Where did my memory go?"*

Three distinct mechanisms, and a staff-level answer names which one:

**1. Arena high-water mark / fragmentation.** An arena returns to the OS only when
*every* pool in it is free. One surviving 64-byte object pins the entire 1 MB arena. Load
10 million objects, free 99% of them, and the survivors — scattered across arenas — can
hold nearly all of them resident. Your heap has holes, not garbage.

```
  After the load:          After deleting 99%:
  ┌───────────────┐        ┌───────────────┐
  │███████████████│        │█..............│  ← 1 live block
  │███████████████│  1 MB  │....█..........│  ← 1 live block
  │███████████████│        │...........█...│  ← 1 live block
  └───────────────┘        └───────────────┘
   fully used               3 objects alive, 1 MB still held.
                            RSS: unchanged. free(): called correctly.
```

**2. Free lists.** Several types keep their own free lists of recently-deallocated
objects for fast reuse — you can see them in `_debugmallocstats` output *(measured)*:

```
           5 free PyFloatObjects * 24 bytes each =                  120
            6 free PyListObjects * 40 bytes each =                  240
   2 free 1-sized PyTupleObjects * 40 bytes each =                   80
   ...
 6 free 18-sized PyTupleObjects * 176 bytes each =                1,056
```

Note tuples get a free list **per length**. That's a deliberate bet that programs reuse
tuple shapes, and it's why tuple-heavy code allocates cheaply. Since 3.12 these live in
per-interpreter state rather than in C globals — which is what makes them safe under
subinterpreters ([`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md)),
and the free-threaded build shards them per thread again.

**3. `malloc` didn't return it either.** Above 512 bytes you're in the platform
allocator, which has its own retention policy. See `08-allocators.md`.

**None of these is a leak.** A leak is unreachable-but-uncollected memory. These are
reachable-or-retained. Distinguishing them is the whole skill in
[`35-memory-optimization.md`](35-memory-optimization.md).

### 5.1 The fourth mechanism: refcounts destroy copy-on-write

This one isn't about your heap at all, and it is the most common way a real Python
service runs out of memory.

`fork()` gives the child a copy-on-write view of the parent's pages: nothing is copied
until someone *writes*. A pre-forking server (gunicorn, uWSGI, `multiprocessing` with the
`fork` start method) leans on this — load a 2 GB model in the parent, fork 8 workers, pay
for it once.

Except **`ob_refcnt` is at offset 0 of every object** (§1). A child that merely *reads*
those objects still increments and decrements their refcounts, dirtying the page each
object sits on. And the GC makes it worse: a collection pass walks every tracked object's
`PyGC_Head`, which is *also* a write, to every page holding a container.

```
  t=0  fork:            8 workers × 0 MB private   — all shared with the parent
  t=1  workers serve:   refcount writes dirty pages one by one
  t=60 steady state:    8 workers × most-of-the-heap private
                        RSS looks like 8 full copies of a heap you allocated once.
```

The mitigations, in order:

1. **`gc.freeze()` right after loading and just before forking.** It moves everything
   currently alive into a permanent generation the collector never traverses, so GC stops
   writing to those pages. This is the single highest-leverage line in a pre-forking
   server's boot path.
2. **Immortal objects (PEP 683)** already remove the refcount writes for `None`, `True`,
   `False`, small ints and interned strings — a CoW fix as much as a coherence one.
3. **Move the big thing out of the object graph**: a NumPy array, an `mmap`, or Arrow
   buffers have one refcount for megabytes of payload, so CoW actually holds.
4. **Don't fork it at all** — load the data in a separate process and share it via
   `multiprocessing.shared_memory` or the filesystem page cache.

The diagnostic that names this mechanism specifically: on Linux, per-worker `Private_Dirty`
in `/proc/<pid>/smaps_rollup` climbing over time while total allocation is flat. See
[`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §10.2 for why the
write happens at all, and `07-virtual-memory.md` for the page-level mechanics.

---

## 6. Per-type layouts, measured

All *(measured)*, CPython 3.14.6, arm64.

### Strings — the kind system (PEP 393)

CPython picks the narrowest representation that fits, per string:

| Value | `getsizeof` | Kind | Bytes/char |
|---|---|---|---|
| `""` | 41 | compact ASCII | — |
| `"a"` | 42 | compact ASCII | 1 |
| `"ab"` | 43 | compact ASCII | 1 |
| `"a"*10` | 51 | compact ASCII | 1 |
| `"é"` | 61 | UCS-1 (latin-1) | 1 |
| `"é"*10` | 67 | UCS-1 | 1 |
| `"☃"` | 60 | UCS-2 | 2 |
| `"😀"` | 64 | UCS-4 | 4 |

Read that carefully: the **ASCII header is 41 bytes** but the moment one non-ASCII
character appears the header jumps to ~57 (a `PyCompactUnicodeObject` carries an extra
`utf8`/`utf8_length` field set the ASCII path doesn't need). **One emoji in a
million-string dataset changes the per-string overhead for that string by ~20 bytes and
quadruples its per-character cost.**

The relevant C types are `PyASCIIObject` and `PyCompactUnicodeObject` in
`Include/cpython/unicodeobject.h`.

### Containers, empty

| Type | Size | Note |
|---|---|---|
| `tuple` | 48 | GC head + PyVarObject; payload inline |
| `list` | 56 | GC head + header + `ob_item` ptr + `allocated` |
| `dict` | 64 | header + keys ptr + values ptr + used/version |
| `set` | **216** | preallocated 8-slot table inline |
| `bytes` | 33 | 32 header + NUL terminator |
| `bytearray` | 56 | |

**A `set()` costs 216 bytes empty** — nearly 4× a dict. It preallocates its small table
inline. If you're creating millions of tiny sets, that's the number that kills you; a
`frozenset` costs the same.

### Tuple vs list — the structural difference

```
  tuple (1, 2, 3):  payload INLINE, one allocation, one cache line
  ┌──────────────────────────────────────────┐
  │ gc │ refcnt │ type │ size=3 │ p1│p2│p3   │
  └──────────────────────────────────────────┘

  list [1, 2, 3]:  payload INDIRECT, two allocations, two cache lines
  ┌────────────────────────────────────┐
  │ gc │ refcnt │ type │ size │ ob_item│──┐  allocated=4
  └────────────────────────────────────┘  │
                                          ▼
                              ┌──────────────────┐
                              │ p1 │ p2 │ p3 │ - │
                              └──────────────────┘
```

That extra hop is why tuples beat lists on read-heavy paths, and it is a *locality*
argument, not a "tuples are immutable so they're faster" hand-wave.

---

## 7. List overallocation

`list.append` must be amortized O(1), so the list overallocates. Measured growth of
`allocated` as you append *(measured)*:

| len | `getsizeof` | capacity |
|---|---|---|
| 1 | 88 | 4 |
| 5 | 120 | 8 |
| 9 | 184 | 16 |
| 17 | 248 | 24 |
| 25 | 312 | 32 |
| 33 | 376 | 40 |
| 41 | 472 | 52 |
| 53 | 568 | 64 |
| 65 | 664 | 76 |
| 77 | 792 | 92 |
| 93 | 920 | 108 |
| 109 | 1080 | 128 |
| 129 | 1240 | 148 |
| 149 | 1432 | 172 |

The growth factor is roughly **1.125× plus a constant** — far more conservative than the
2× many languages use. That trades a few extra reallocations for much lower peak waste.

**The practical lever** *(measured)*: `list(range(100))` occupies **856 bytes**, because
constructing from a sized iterable pre-sizes exactly. The same 100 elements reached by
`append` sit inside an overallocated buffer. If you know the length, build the list in one
shot — a comprehension or `list(iterable)` — rather than appending in a loop. It's faster
*and* smaller.

---

## 8. The compact dict and key sharing

Since 3.6, `dict` uses a **split representation**: a dense array of entries in insertion
order, plus a sparse array of indices into it.

```
   indices (sparse, small ints — i8/i16/i32 depending on size)
   ┌────┬────┬────┬────┬────┬────┬────┬────┐
   │ -1 │  0 │ -1 │ -1 │  2 │  1 │ -1 │ -1 │
   └────┴────┴────┴────┴────┴────┴────┴────┘
             │              │    │
             ▼              ▼    ▼
   entries (dense, insertion-ordered)
   ┌──────────────────────────────────────┐
   │ 0: hash, *key, *value                │
   │ 1: hash, *key, *value                │
   │ 2: hash, *key, *value                │
   └──────────────────────────────────────┘
```

Two wins at once: iteration walks a **contiguous** array (cache-friendly — see
[`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §3), and insertion
order became a free side effect, which is why it got promoted from implementation detail
to language guarantee in 3.7.

Measured growth *(measured)*:

| entries | `getsizeof` |
|---|---|
| 1 | 224 |
| 6 | 352 |
| 11 | 632 |
| 22 | 1168 |
| 43 | 2264 |
| 86 | 4688 |
| 171 | 9304 |

Resizes at 6, 11, 22, 43, 86, 171 — roughly 2× each time, triggered at ~2/3 load factor.

### Key-sharing dicts, managed dicts, and inline values

For instances, the *keys* are the same for every object of a class, so CPython stores one
shared key table per class and gives each instance only a values array. This is the
optimization that makes `self.x = 1` in `__init__` affordable at scale.

Since 3.11 it goes considerably further than that, and **this is the single most
misreported area of Python memory layout.** A class that doesn't define `__slots__` gets
`Py_TPFLAGS_MANAGED_DICT`, and its instances get a **pre-header** — words sitting *before*
the `PyObject*`, alongside `PyGC_Head`:

```
                 ┌───────────────────────────────┐
   pre-header    │ dict-or-values pointer        │  ← managed dict
                 │ weakref list       (3.12+)    │  ← managed weakref
                 ├═══════════════════════════════┤  ← PyObject* points here
                 │ ob_refcnt │ ob_type           │  tp_basicsize = 16
                 ├───────────────────────────────┤
   inline values │ values[0] │ values[1] │ ...   │  ← the attributes, in
                 └───────────────────────────────┘    shared-key order

   the class holds ONE PyDictKeysObject:  {'x': 0, 'y': 1, 'z': 2}
```

The attributes live in a **values array attached to the instance**, indexed by position in
the class's shared key table. **There is no dict.** `self.x` is an index into that array,
and the interpreter specializes the load accordingly — this is exactly what
`LOAD_ATTR_INSTANCE_VALUE` is *(measured, via `dis.get_instructions(f, adaptive=True)`
after warmup)*:

```
  plain class     →  LOAD_ATTR_INSTANCE_VALUE
  __slots__ class →  LOAD_ATTR_SLOT
```

A real `dict` is **materialized lazily**, only when something forces it: `obj.__dict__`,
`vars(obj)`, `__reduce__`/pickle, or an attribute set that the shared keys can't
accommodate. That is why `tp_dictoffset` reads `-1` — "managed, ask the runtime."

**Which is precisely why the obvious measurement is a trap.** `sys.getsizeof(obj)` never
counts the inline values, and `sys.getsizeof(obj.__dict__)` *creates the dict it claims to
be measuring* — you have to un-optimize the object to observe it. Measured, same class,
varying attribute count *(measured)*:

| attrs | `getsizeof(instance)` | real RSS/instance, 1M instances |
|---|---|---|
| 0 | 48 | 91 B |
| 1 | 48 | 91 B |
| 3 | **48** | **107 B** |
| 10 | **48** | **171 B** |
| 20 | **48** | **270 B** |

**`getsizeof` returns 48 for all of them.** The instrument is not merely imprecise here;
it is constant while the truth triples.

### Settling it with the right instrument

1M instances of a 3-attribute class, `ru_maxrss` delta, one process per variant, bytes per
instance (each figure includes the 8 B list slot holding the reference) *(measured)*:

| Variant | B/instance | vs. plain |
|---|---|---|
| plain class (managed dict, keys shared) | **107** | — |
| plain class, `__dict__` touched on every instance | **171** | **+60%** |
| plain class, one instance given a 4th attribute *before* the million | 123 | +15% |
| `__slots__ = ('x','y','z')` | **75** | **−30%** |

Three conclusions, and they overturn the folklore:

- **Key sharing plus inline values already captures most of the `__slots__` win.** The
  real gap is ~1.4×, not the ~6× you get by adding a materialized `__dict__` to the
  instance's `getsizeof`. See §9.
- **Materializing `__dict__` costs ~60%.** Anything that calls `vars()` on every object,
  or pickles the whole population, silently un-optimizes your entire heap. This is a real
  production failure mode and it is invisible to `getsizeof`.
- **Widening the shared key table costs everyone.** One instance acquiring a 4th attribute
  grew *every* instance by ~16 B, because the shared keys — and therefore each values
  array — got wider. Attributes conditionally set in one branch of `__init__` are paid for
  by every object of the class.

> **Verified and unverified.** The measurements above are solid and repeatable. The exact
> accounting of the fixed ~48 B/instance gap between the managed-dict and `__slots__`
> variants — pre-header words vs. values-array header vs. pymalloc rounding — I have
> *not* pinned down field by field; check `_PyObject_InlineValues` and
> `Include/internal/pycore_object.h` on your build. Lab 4 now extends this rather than
> settling it.
>
> This section previously reported the experiment as inconclusive, because it was run with
> `getsizeof`. Keeping the wrong instrument was the entire error — the lesson
> [`31-measurement-methodology.md`](31-measurement-methodology.md) exists to teach.

---

## 9. `__slots__` — a smaller win than you've been told, for the right reason

Nearly every article about `__slots__` reports a 5–10× memory saving. That number comes
from adding a materialized `__dict__` to the instance's `getsizeof` — and as §8 showed,
a modern instance *doesn't have one* until you go looking. Measured properly, by RSS over
1M instances, 3 attributes *(measured)*:

| Variant | B/instance (RSS) | `getsizeof` says | ns per attribute read |
|---|---|---|---|
| plain class | **107** | 48 (+ 296 if you force `__dict__`) | **5.70** |
| `__slots__ = ('x','y','z')` | **75** | 56 | **4.73** |

**~1.4× memory, ~1.2× speed.** Real, worth having in bulk, and nothing like the folklore.

Across widths *(measured, RSS B/instance)*:

| attrs | plain | `__slots__` | ratio |
|---|---|---|---|
| 0 | 91 | 42 | 2.2× |
| 3 | 107 | 75 | 1.4× |
| 10 | 171 | 123 | 1.4× |
| 20 | 270 | 203 | 1.3× |

The saving is roughly a **constant ~35–50 bytes per instance** — the pre-header and the
values-array bookkeeping — not a proportional one. So `__slots__` pays best on objects
with *few* attributes and many instances, which is the opposite of the intuition that
"more attributes ⇒ more to save."

### Why the hop diagram in most references is out of date

```
  THE FOLKLORE (true before 3.11) — 3 hops to read self.x
    instance ──▶ __dict__ ──▶ entries array ──▶ value object

  WHAT ACTUALLY HAPPENS NOW (managed dict, LOAD_ATTR_INSTANCE_VALUE)
    instance ──▶ inline values[0] ──▶ value object
                 ^ allocated with the instance; usually the same cache line

  WITH __slots__ (LOAD_ATTR_SLOT)
    instance[slot 0] ──▶ value object
```

Both specialized forms are **one hop plus the guard**. `__slots__` wins the remaining
margin by skipping the pre-header indirection and the "have the shared keys changed?"
check — worth ~1 ns per read here, not a category difference. The 3-hop version is what
you get *after* something materializes the dict, which is the real cliff worth avoiding
(§8).

At 56 bytes a 3-slot instance fits inside **one 128-byte cache line on this machine**;
at 20 slots (192 B) it no longer does, and lab 5 asks you to find where that shows up.

### The practical ladder

| Shape | B/instance, 3 fields | Buys you |
|---|---|---|
| plain class | 107 | dynamic attributes, `__dict__`, weakrefs |
| `@dataclass(slots=True)` | 75 *(`getsizeof` 56)* | slots without writing the tuple by hand |
| `NamedTuple` | *(`getsizeof` 72)* | immutable, tuple-compatible, inline payload |
| `array.array('q')` / `struct` | 8 | no per-object header at all |
| NumPy / Arrow column | 8 | the above, plus vectorization |

**The costs of `__slots__`, honestly:** no dynamic attributes, no `__dict__`, no
`__weakref__` unless you add it, multiple-inheritance restrictions, and it interacts
awkwardly with some serialization. And the honest framing of the benefit: if you have
enough instances for 30% to matter, you are usually already at the point where step 4 of
that ladder — dropping the per-object header entirely — is the answer that actually moves
the number. `__slots__` is the cheap, low-risk step on the way there.

---

## 10. Caching and interning

CPython pre-creates and reuses certain objects so the header cost is paid once. Measured
*(measured)*:

```python
(-5) is (-5)          # True   — small int cache
(256) is (256)        # True   — top of the cache
int('257') is int('257')   # False  — outside it
'ab' is 'ab'          # True   — compile-time interning
('a' + 'b') is 'ab'   # True   — constant-folded by the compiler
sys.intern(s1) is sys.intern(s2)   # True — explicit interning
```

- **Small ints −5…256** are preallocated singletons. This is why `for i in range(100)`
  allocates no integers, and why the boundary at 257 is a real (if rarely relevant) cliff.
- **String interning** applies automatically to identifier-like literals and compile-time
  constants. Runtime-built strings are *not* interned unless you call `sys.intern`.
- **`sys.intern` is a genuine production technique** for workloads with massive key
  duplication — parsing millions of records with repeated field names. It trades a hash
  lookup for deduplicated storage *and* pointer-equality fast paths in dict lookups.

### Does interning leak? Version-dependent — measured here

The standard warning is that interned strings live forever, so interning unbounded
user-supplied strings is an unbounded leak. That was true when 3.12 made interned strings
immortal. On **3.14.6 it is not** *(measured)*:

```python
>>> s = sys.intern(build_unique_string())
>>> id_before = id(s); del s; gc.collect()
>>> sys.intern(build_unique_string()) is id_before   # → a different object
False
>>> sys.getrefcount(sys.intern(build_unique_string()))
2      # our name + getrefcount's argument. The interned table holds no counted reference.
```

The string is released once your last reference dies — mortal interning, with the table
entry cleaned up. So the modern shape of the advice is:

- **Intern deliberately, at a bounded cardinality** (field names, enum-like values,
  column labels) — that's where the win is, and it's large.
- **Do not assume the leak behaviour either way across versions.** It changed in 3.12 and
  changed again by 3.14; `sys.getrefcount` after dropping your reference tells you what
  *your* build does in three lines. This is a good example of the doc-level rule: check
  the constant, don't recite it.

**Never write `is` for value comparison.** The measurements above are implementation
details that vary by build and version; `==` is the correct operator. The interpreter even
warns you now — `SyntaxWarning: "is" with 'int' literal` appeared in the output while
producing this document.

Related but distinct: **immortal objects** (PEP 683, 3.12+) mark `None`, `True`, `False`,
small ints and interned strings so their refcounts are never touched. That's a *coherence*
optimization, not a memory one — see [`24-the-gil.md`](24-the-gil.md) §8.1.

---

## 11. `sys.getsizeof` lies — and a correct deep sizer

`getsizeof` reports **only the object itself**. It does not follow references. So:

```python
>>> sys.getsizeof([1, 2, 3])
80        # the list. NOT the three ints it points at.
```

It also excludes: pymalloc's size-class rounding, the arena overhead, and (unless the type
implements `__sizeof__` carefully) any external buffers.

**Three blind spots that bite in practice:**

1. **Inline attribute values (§8).** `getsizeof` on an instance returns `tp_basicsize` plus
   pre-header — 48 bytes whether the object has 0 attributes or 20 *(measured)*. The
   attributes are simply not counted.
2. **The UTF-8 cache.** A non-ASCII `str` can carry a cached UTF-8 encoding
   (`PyCompactUnicodeObject.utf8`, filled by C-API calls needing a `char*`), and
   `unicode_sizeof` doesn't add it. *Flagged, partly unverified:* the field is real, but
   I could not trigger it from pure Python here — `.encode()` and failed `getattr` both
   left RSS flat over 500k strings. If you find the trigger, that's a lab.
3. **Anything reached by a pointer.** `getsizeof([1,2,3])` is 80: the list, not the ints.

A correct recursive sizer must track identity to survive cycles and shared references —
and must be careful not to *change* what it measures:

```python
import gc
import sys
from collections import deque
from itertools import chain

_ATOMIC = (str, bytes, bytearray, int, float, complex, type(None))

def _slot_names(o):
    """All slots, including inherited ones. __slots__ may be a bare string."""
    for klass in type(o).__mro__:
        slots = klass.__dict__.get('__slots__', ())
        if isinstance(slots, str):
            slots = (slots,)
        yield from slots

def deep_getsizeof(obj, seen=None):
    """Total footprint of an object graph. Counts each object once.

    Excludes classes and modules by design. Cannot see: allocator rounding,
    arena waste, inline values (§8), cached encodings.
    """
    seen = set() if seen is None else seen
    total = 0
    stack = deque([obj])
    while stack:
        o = stack.popleft()
        if id(o) in seen or isinstance(o, type):
            continue                    # a class drags in the whole module graph
        seen.add(id(o))                 # NOTE: stores a bare id — see below
        total += sys.getsizeof(o)
        if isinstance(o, _ATOMIC):
            continue                    # no outgoing edges worth following
        if isinstance(o, dict):
            stack.extend(chain(o.keys(), o.values()))
        elif isinstance(o, (list, tuple, set, frozenset, deque)):
            stack.extend(o)
        else:
            # tp_traverse. Sees inline values and an *already materialized*
            # __dict__, and materializes nothing. Touching o.__dict__ here
            # would create the dict we claim to be measuring (§8).
            stack.extend(gc.get_referents(o))
        stack.extend(getattr(o, s) for s in _slot_names(o) if hasattr(o, s))
    return total
```

That `gc.get_referents` line is the whole trick, and it is verifiable: sizing 200,000
managed-dict instances moves RSS by **0.2 B/instance**, while touching `o.__dict__` on the
same population moves it by **64 B/instance** *(measured)*. A profiler that perturbs the
heap by 60% is not measuring your program.

Even this **undercounts**, and now you can name every way: allocator rounding, arena
waste, inline values, cached encodings — and `seen` stores bare `id()`s, so a temporary
freed mid-traversal can have its address reused by a later object, which then gets skipped.
Keeping references to fix that would inflate what you're measuring. There is no version of
this function that is both correct and non-invasive; that is the point of the next
paragraph.

**The honest instrument is RSS**, not `getsizeof`. Measure the process before and after,
with `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss` (bytes on macOS, kilobytes on
Linux — a classic portability trap), or use **memray** / **tracemalloc** for attribution.
`getsizeof` is for understanding *layout*; RSS is for answering *"how much memory does this
cost"*. See [`32-profiling.md`](32-profiling.md).

---

## 12. mimalloc in the free-threaded build

PEP 703 replaced pymalloc with **mimalloc** (originally from Microsoft Research, developed
for Koka and Lean) in the free-threaded build. The usual explanation — "it's thread-safe" —
is the wrong one, as an LWN commenter correctly pointed out at the time: any serious
allocator is thread-safe.

The real reasons:

1. **Thread-local heaps** mean allocation needs no cross-thread synchronization on the
   fast path — the same design goal as biased reference counting, applied to memory.
2. **The GC can enumerate all objects by walking mimalloc's page metadata**, so CPython
   doesn't need a separate global registry of GC-tracked objects. This is what allows the
   `PyGC_Head` to disappear — which is exactly the 16-byte saving measured in §2.
3. Its size-segregated page structure supports the lock-free reads that per-object locking
   depends on.

**The allocator choice is load-bearing for the GC design**, not just for allocation speed.
That's the transferable lesson: in runtime engineering, the allocator, the GC, and the
concurrency model are one decision, not three.

---

## 13. Budgeting: cost per million objects

The practical output of this document. Per **one million** objects, standard 3.14 build.
The instance rows are **RSS-measured** (§8, §9), including the 8 B list slot; the rest are
`getsizeof` rounded up to the pymalloc size class, so add ~8 MB per million for whatever
container holds the references:

| Structure | Bytes each | Per 1M | Notes |
|---|---|---|---|
| `int` (small, cached) | 0 | **0** | −5…256 are free |
| `int` (arbitrary) | 28 → 32 (rounded) | **32 MB** | + 8 MB for the pointers holding them |
| `float` | 24 → 32 | **32 MB** | |
| short ASCII `str` | ~48 → 48 | **48 MB** | |
| `tuple` of 3 | 72 → 80 | **80 MB** | |
| `list` of 3 | 120 (two allocs) | **120 MB** | |
| plain instance, 3 attrs | **107** *(measured)* | **107 MB** | key-shared, inline values |
| …same, after `vars()`/pickle touches `__dict__` | **171** *(measured)* | **171 MB** | **+60% for asking a question** |
| **slotted instance, 3 attrs** | **75** *(measured)* | **75 MB** | **1.4× better, not 6×** |
| `NamedTuple` of 3 | 72 → 80 | **80 MB** | |
| `array.array('q')` element | 8 | **8 MB** | no header at all |
| NumPy `float64` element | 8 | **8 MB** | 4× better than a Python float |

**The decision rule this produces:** if you have more than ~1 million of something, it
should not be a plain Python object — but notice where the steps actually are. Plain →
`__slots__` is ~1.4×. `__slots__` → headerless (`array`/NumPy/Arrow) is **~9×**. The big
win is dropping the per-object header, not shaving it, and the intermediate step is worth
taking mainly because it is cheap and non-invasive.

**The cheapest win on this table is negative:** don't let anything materialize a million
`__dict__`s. One `vars()` in a serializer costs more than `__slots__` saves.

---

## 14. Lab exercises

Reading this leaves you at rung 3 (README §14). These use the local `~/.local/bin/python3.14`
and `python3.14t` builds.

**1 — Reproduce the header table.** Write the `getsizeof` sweep for scalars and containers
on both builds. Confirm the +16/0 split from §2 and explain the pattern from `gc.is_tracked`.
*Proves you understand what a header is made of.*

**2 — Find your allocator constants.** Run `sys._debugmallocstats()`. Extract arena size,
pool size, threshold, and class count. Compare with the 4 KB/256 KB figures published in
most references. *Proves constants must be measured, not quoted.*

**3 — Build a fragmentation trap.** Allocate 5 million small objects, keep every 1000th,
delete the rest, force `gc.collect()`, and measure RSS before/during/after. Explain why RSS
doesn't return. Then repeat, keeping the *first* 5000 contiguously instead of every 1000th,
and explain the difference. *Proves §5 — the single most valuable lab here.*

**4 — Reproduce the managed-dict table (§8), then break it.** Create 1,000,000 instances of
a 3-attribute class and measure **process RSS**, not `getsizeof`. Expect **~107 B/instance**;
`getsizeof` will insist on 48 for every width you try. Then find the operations that
materialize the dict — `vars()`, `pickle.dumps`, `copy.deepcopy`, `__reduce__`, a
`**self.__dict__` splat — and confirm each one costs you the ~60 B. *Proves you can design
a measurement when the obvious instrument is lying to you, and gives you a checklist of
things not to do in a hot serializer.*

**5 — The `__slots__` win, honestly.** One million instances with and without `__slots__`.
Expect **~75 vs ~107 B** and **~4.7 vs ~5.7 ns** per attribute read — a ~1.4× and ~1.2×
win. Now find the article that claims 5–10× and work out exactly which measurement
produced it. Then try 20 attributes (192 B — no longer one 128-byte cache line) and see
which of the two effects moves. *Proves the folklore wrong with your own hands, which is
the only way it stops being folklore.*

**5b — Cost the copy-on-write path (§5.1).** Load ~500 MB of Python objects, `fork()` 4
children that only *read* them, and watch per-child private RSS climb. Then add
`gc.freeze()` before the fork and repeat. On Linux read `Private_Dirty` from
`/proc/<pid>/smaps_rollup`; on macOS use `footprint -p`. *Proves the mechanism behind most
"why does my gunicorn deployment use 8× the memory" incidents.*

**6 — The free-threading memory tax.** Load a realistic dataset (a million dicts of scalars
parsed from JSON) under `python3.14` and `python3.14t`. Measure peak RSS on each. Predict
the delta from §2 before you run it. *Proves the §2 finding on real data, and is exactly
the check to run before any free-threading migration.*

**7 — Interning at scale.** Parse a million records with ~20 repeated string keys, with and
without `sys.intern` on the keys. Measure RSS and lookup throughput. *Proves §10 is a
production technique, not trivia.*

**8 — Size-class cliffs.** Find a class whose instance size sits just above a pymalloc size
class boundary. Remove one attribute, confirm it drops a class, and measure the RSS change
over a million instances against the "obvious" prediction of 8 bytes each.

---

## 15. Question bank

1. An `int` holding `1` costs 28 bytes. Account for every byte. *(§1)*
2. Which objects carry a `PyGC_Head`, and why is `(1, 2)` not one of them? *(§1)*
3. On the free-threaded build, why do `int` and `float` grow by 16 bytes but `list` and `dict` don't? *(§2)*
4. What is pymalloc's small-block threshold, and what happens above it? *(§3)*
5. pymalloc's pool is 16 KB and this machine's page is 16 KB. Why is that a coincidence, and what actually determines the pool size? *(§3)*
6. You free 99% of your objects and RSS doesn't move. Give three distinct mechanisms and how you'd distinguish them. *(§5)*
6b. Eight forked workers share a 2 GB parent heap. Private RSS climbs to 2 GB each anyway. Explain the mechanism and give the one-line mitigation. *(§5.1)*
7. Why is a `set()` 216 bytes empty while a `dict` is 64? *(§6)*
8. Why does one emoji in a string change both its header size and its per-character cost? *(§6)*
9. `list(range(100))` vs 100 `append`s — which is smaller, and why? *(§7)*
10. Explain the compact dict's two arrays, and why insertion ordering became a free guarantee. *(§8)*
10b. Where do a plain instance's attributes actually live in 3.11+, and what makes `tp_dictoffset` report `-1`? *(§8)*
10c. A colleague benchmarks `__slots__` and reports a 6× memory saving. Their code is correct. What did they actually measure? *(§8, §9)*
10d. One instance of a class acquires a fourth attribute. Why does that cost every *other* instance memory? *(§8)*
11. `__slots__` saves ~30%, not ~600%. Where does the saving come from, why is it roughly constant per instance, and when is it still the wrong move? *(§9)*
12. Why is `sys.getsizeof` the wrong tool for "how much memory does this cost", and what's right? Name three things it cannot see. *(§11)*
12b. Why does a recursive sizer have to use `gc.get_referents` rather than `obj.__dict__`? *(§11, §8)*
13. Why was mimalloc load-bearing for the *GC* design, not just for allocation speed? *(§12, §2)*
14. You have 50 million records in memory and are out of RAM. Walk the escalation. *(§13)*
15. A colleague reports `sys.getsizeof(my_dict)` as their memory usage in a design doc. What's wrong, and what should they have measured? *(§11)*

---

## 16. Sources

**Primary — verify against these, not against this document**
- [`Objects/obmalloc.c`](https://github.com/python/cpython/blob/main/Objects/obmalloc.c) — pymalloc, with an extensive comment block at the top that is the best documentation that exists. **Read the comments.**
- [`Include/internal/pycore_obmalloc.h`](https://github.com/python/cpython/blob/main/Include/internal/pycore_obmalloc.h) — where `ARENA_BITS`, `POOL_BITS`, `ALIGNMENT` and `WITH_PYMALLOC_RADIX_TREE` actually live. §3's correction came from reading this; most secondary sources predate it.
- [`Include/internal/pycore_object.h`](https://github.com/python/cpython/blob/main/Include/internal/pycore_object.h) — `_PyObject_InlineValues`, the managed-dict pre-header, and `_PyObject_GetManagedDict`. The primary source for §8.
- [`Include/object.h`](https://github.com/python/cpython/blob/main/Include/object.h), [`Include/cpython/dictobject.h`](https://github.com/python/cpython/blob/main/Include/cpython/dictobject.h), [`Objects/dictobject.c`](https://github.com/python/cpython/blob/main/Objects/dictobject.c) — the struct definitions and, in `dictobject.c`, another excellent design comment.
- `sys._debugmallocstats()` on **your** build — the only trustworthy source for allocator constants. Reference only; it changes.
- [PEP 393 — Flexible String Representation](https://peps.python.org/pep-0393/) — the string kind system in §6.
- [PEP 703](https://peps.python.org/pep-0703/) §Memory Management — mimalloc's role. Read this alongside [`24-the-gil.md`](24-the-gil.md) §8.
- [PEP 683 — Immortal Objects](https://peps.python.org/pep-0683/).

**Background**
- [The Garbage Collection Handbook, 2e](https://gchandbook.org/) (Jones, Hosking & Moss, 2023) — ch. on allocation. Reference only; skim the allocator chapters.
- [mimalloc: Free List Sharding in Action](https://www.microsoft.com/en-us/research/publication/mimalloc-free-list-sharding-in-action/) (Leijen, Zorn, de Moura) — the design paper. Read it if §12 interested you.
- Brandt Bucher / Faster CPython team writeups on the compact dict and instance-attribute optimizations.

**Tools**
- [memray](https://bloomberg.github.io/memray/) — the best Python memory profiler. Read the docs, use it in every lab above.
- `tracemalloc` (stdlib) — allocation attribution, lower fidelity than memray but always available.
- `PYTHONMALLOC=debug` — §4. Essential for native extension work.

**Sibling docs**
- [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §3, §10 — why layout beats cleverness.
- [`24-the-gil.md`](24-the-gil.md) §1, §8 — why `ob_refcnt` at offset 0 shaped the whole runtime.
- [`22-garbage-collection.md`](22-garbage-collection.md) — tuple untracking, and what `PyGC_Head` is for.
- [`35-memory-optimization.md`](35-memory-optimization.md) — applying §5 and §13 to a real service.

---

*Next: [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md) — allocating and owning
these objects from C, where the rules in §4 stop being advice and start being segfaults.*
