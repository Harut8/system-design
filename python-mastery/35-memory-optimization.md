# 35 — Memory optimization: cutting RSS you can explain

> **Tier 5, doc 35.** Prerequisites: [`31-measurement-methodology.md`](31-measurement-methodology.md)
> (you cannot report a memory win without a noise floor),
> [`07-virtual-memory.md`](07-virtual-memory.md) (RSS, COW, fragmentation, the OOM path),
> [`16-object-memory-layout.md`](16-object-memory-layout.md) (what an object costs),
> [`22-garbage-collection.md`](22-garbage-collection.md) §12,
> [`32-profiling.md`](32-profiling.md) §7. Feeds into:
> `27-multiprocessing-and-subinterpreters.md`, `46-production-python.md`.
>
> **THESIS 1: peak RSS is what kills your container, and peak RSS is not a property of
> your data — it is a property of how many stages of your pipeline are alive at once.**
> The same pipeline, the same input, the same output, measured here: **2,095.9 MB peak
> versus 0.1 MB**, decided entirely by whether the intermediate stages were lists or
> generators (§5). No object got smaller. Nothing was cached differently. The bytes that
> mattered were the ones that overlapped in time.
>
> **THESIS 2: past that, the biggest remaining win is not making Python objects smaller —
> it is having fewer of them.** A forked child sweeping 2 M inherited tuples privatises
> **93.6%** of the parent's heap; the same data as two `array.array`s privatises
> **12.7%** — **16.3× less per worker** (§11). `__slots__`, by comparison, buys 23%
> (§8). The ladder in §4 is ordered by that arithmetic, and most teams start four rungs
> too low.

> **Measurement provenance.** All numbers marked *(measured)* were produced on the machine
> this repo lives on: **Apple M3 Pro, macOS (Darwin 25.5.0), arm64, CPython 3.14.6**,
> 16 KB pages, 18 GB RAM, 5 P-cores + 6 E-cores. RSS figures are peak
> (`ru_maxrss`) unless the table says *current*, each arm in its own fresh process, and —
> per [`31`](31-measurement-methodology.md) §3 — **ratios within one run are the claim;
> absolute megabytes across runs are not.** Memory measurements are far quieter than
> timing measurements on this machine: repeated runs of the §8 table moved by under
> 0.5 MB. The timing columns are not.
>
> **Platform boundary.** macOS has no `/proc`, no `smaps`, no PSS/USS, and no cgroups.
> Everything about **container budgets, `memory.max`, the OOM killer, `malloc_trim`,
> `MALLOC_ARENA_MAX`, and glibc trimming (§12, §14) is Linux, and is cited from primary
> sources rather than measured here.** Those sections are marked *(cited)*. Do not read
> them as verified on this hardware; do read the sources.

## Contents

1. [What you are optimizing: four numbers, one of which kills you](#1-what-you-are-optimizing-four-numbers-one-of-which-kills-you)
2. [Measuring RSS truthfully](#2-measuring-rss-truthfully)
3. [Triage before optimization: the four shapes](#3-triage-before-optimization-the-four-shapes)
4. [The ladder, ordered by effect size](#4-the-ladder-ordered-by-effect-size)
5. [Rung 1 — Don't hold it: streaming, measured](#5-rung-1--dont-hold-it-streaming-measured)
6. [Rung 2 — Bound every cache](#6-rung-2--bound-every-cache)
7. [Rung 3 — Don't duplicate: dedup and interning, measured](#7-rung-3--dont-duplicate-dedup-and-interning-measured)
8. [Rung 4 — Change the representation, measured](#8-rung-4--change-the-representation-measured)
9. [Rung 5 — Get the data out of the object graph: buffers and zero-copy](#9-rung-5--get-the-data-out-of-the-object-graph-buffers-and-zero-copy)
10. [Rung 6 — `mmap` and memory-mapped files, measured](#10-rung-6--mmap-and-memory-mapped-files-measured)
11. [Rung 7 — Share it: COW-friendly forking, measured](#11-rung-7--share-it-cow-friendly-forking-measured)
12. [Rung 8 — The allocator and the runtime](#12-rung-8--the-allocator-and-the-runtime)
13. [Arena return behaviour and worker recycling, measured](#13-arena-return-behaviour-and-worker-recycling-measured)
14. [Memory budgets per container *(cited)*](#14-memory-budgets-per-container-cited)
15. [The free-threaded build's memory story, measured](#15-the-free-threaded-builds-memory-story-measured)
16. [Object-graph analysis: finding the retainer](#16-object-graph-analysis-finding-the-retainer)
17. [The cost model](#17-the-cost-model)
18. [What I could not verify](#18-what-i-could-not-verify)
19. [Lab exercises](#19-lab-exercises)
20. [Question bank](#20-question-bank)
21. [Sources](#21-sources)

---

## 1. What you are optimizing: four numbers, one of which kills you

"Reduce memory usage" is not a specification. There are at least four numbers, they move
independently, and only one of them ends your process.

| Number | What it is | Who cares |
|---|---|---|
| **Total bytes allocated** | Everything requested over the process lifetime | Nobody, directly. It is an *allocator throughput* concern, not a footprint one. |
| **Live set** | Bytes reachable right now | You, when reasoning about the design |
| **Current RSS** | Physical pages the kernel has attributed to you right now | Your dashboard |
| **Peak RSS** | The high-water mark of the above | **The OOM killer.** This is the one that kills you. |

The four are related by inequalities, not equations:

```
  live set  ≤  current RSS  ≤  peak RSS  ≤  total allocated
              └── allocator ──┘└─ time ─┘└── reuse ────────┘
                 overhead,       overlap
                 fragmentation
```

Each `≤` is a different engineering problem, and **each gap is attacked by a different
rung of §4**:

- `live set → current RSS` is allocator overhead and fragmentation
  ([`16`](16-object-memory-layout.md) §3, [`07`](07-virtual-memory.md) §14, §13 below).
- `current RSS → peak RSS` is *time overlap* — the thing §5 is about, and the thing
  nearly every "memory optimization" effort ignores.
- `peak RSS → total allocated` is reuse, which the allocator handles for you and which
  you should almost never spend effort on.

Two consequences worth stating before any code:

**You are not optimizing a scalar, you are optimizing against a budget.** The question is
never "is 900 MB a lot?" — it is "does peak RSS × workers + headroom fit under
`memory.max`?" (§14). A change that cuts average RSS 40% and leaves peak untouched has
bought you nothing you can spend.

**Allocation volume and peak footprint are nearly uncorrelated.** §5 measures a pipeline
where the low-peak arm allocates *the same objects* as the high-peak arm and touches them
the same number of times. If your instinct is "allocate less," you will optimize the wrong
gap.

---

## 2. Measuring RSS truthfully

### 2.1 The instruments, and what each one actually counts

| Instrument | Counts | Blind to |
|---|---|---|
| `resource.getrusage().ru_maxrss` | **Peak** RSS since process start | Everything current; it never decreases |
| `psutil.Process().memory_info().rss` | Current RSS, incl. shared pages | Which pages are yours vs shared |
| `psutil.Process().memory_full_info().uss` | Current **private** bytes (Linux/Win) | Cost of computing it — walks `smaps` |
| `/proc/self/smaps_rollup` (Linux) | `Private_Dirty`, `Shared_Clean`, PSS | Anything about *why* |
| cgroup `memory.current` / `memory.peak` (Linux) | What the kernel bills the container | Attribution to Python code |
| `tracemalloc` | Bytes **requested through CPython's allocators**, by traceback | C-extension `malloc`, allocator overhead, arenas, mappings |
| `sys._debugmallocstats()` | pymalloc arena/pool/block accounting | Anything above 512 bytes |
| `sys.getsizeof(o)` | One object's shallow size | Everything it points at |
| `memray` | Every allocation, with native stacks, over time | Nothing much — it is the right tool (§16) |

The single most useful sentence about this table: **`tracemalloc` and the OOM killer are
measuring different things and both are correct.** [`07`](07-virtual-memory.md) §15.1
measured the gap at **3.26×** on a 1.5 M-tuple load; §5 below independently reproduces it
at **2.36×** on a different workload. When someone says "tracemalloc says 600 MB but the
container died at 2 GB," nobody is lying — one is counting requests, the other is counting
pages.

### 2.2 Trap: `ru_maxrss` units are platform-dependent

The `getrusage(2)` field is documented as "maximum resident set size" without a unit, and
the unit differs. On this machine, after touching a 200 MB `bytearray` *(measured)*:

```
ru_maxrss raw = 230,080,512
  -> if bytes: 219.4 MB   ✓  matches `ps -o rss=`
  -> if KB:    224,688 MB  ✗  larger than the machine
```

**macOS reports bytes. Linux reports kilobytes.** A memory dashboard that hardcodes one of
these is wrong by 1024× on the other platform, and the direction of the error means it
usually looks *plausible* on the platform it is wrong on. The portable form:

```python
import resource, sys
_RU_SCALE = 1 if sys.platform == "darwin" else 1024
def peak_rss_bytes() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RU_SCALE
```

### 2.3 Trap: `ru_maxrss` is a high-water mark and never falls

[`32`](32-profiling.md) §7 flags this; here it is concretely. In the same process
*(measured)*:

```
after touching 200 MB : ps rss delta 200.0 MB   ru_maxrss 219.4 MB
after `del` + collect  : ps rss delta   0.0 MB   ru_maxrss 219.4 MB   ← unchanged
```

`ru_maxrss` answers "how close did I come to the limit," which is exactly the right
question for a *budget*, and exactly the wrong question for "did my fix work?" For the
latter you need current RSS, which on macOS means `ps -o rss=` or `psutil`, and on Linux
means `/proc/self/statm` or the cgroup counter.

### 2.4 The instrument changes the measurement — `tracemalloc`'s own cost, measured

Building 500,000 small dicts, with and without tracing *(measured)*:

| `tracemalloc` frames | Peak RSS | Build time |
|---|---|---|
| off | **151.7 MB** | **86.3 ms** |
| 1 | 390.7 MB | 887.0 ms |
| 10 | 390.7 MB | 876.0 ms |
| 25 | 390.7 MB | 877.6 ms |

**`tracemalloc` cost 2.58× the memory and 10.3× the time**, and — the surprising row —
**frame depth changed neither.** The usual advice ("use `tracemalloc=1` in production,
deeper only when debugging") is optimizing the wrong term for this shape of workload. The
mechanism: `tracemalloc` keeps a hash map from every live pointer to its trace, plus a
*deduplicated* pool of tracebacks. When allocations come from few call sites — one
comprehension here — there is a handful of distinct tracebacks no matter how deep each
one is, and the per-block pointer map dominates. Deep frames cost you when the *sites* are
many, not when the stacks are tall.

The consequence for this document: **a `tracemalloc`-attributed number is not a footprint
number.** Use it to rank *which code* allocated, never to answer *how much is resident*.
This is [`32`](32-profiling.md)'s thesis applied to memory: the profiler does not merely
inflate, it reorders — and here it inflates the very quantity you are trying to reduce.

### 2.5 The measurement protocol

Per [`31`](31-measurement-methodology.md), a memory result is reportable when:

1. **Each arm runs in a fresh process.** Interpreter state, import graph, and allocator
   history are all path-dependent. Comparing arms inside one process compares histories.
2. **You report peak *and* current.** Peak answers the budget question; current answers
   the fix question. The difference between them is your transient headroom.
3. **You subtract a baseline arm** that does the setup and none of the work. The
   interpreter itself was 15.8 MB here *(measured)*; that is not part of your win.
4. **You state whether the profiler was attached.** §2.4 is why.
5. **You do not run `timeit` for memory.** It disables the GC
   ([`31`](31-measurement-methodology.md) §6.1), which is a semantic change for footprint.

---

## 3. Triage before optimization: the four shapes

[`07`](07-virtual-memory.md) §15.3 gives the definitive table; this is the operational
procedure that uses it. **Do this before you change one line**, because three of the four
shapes are immune to the fix that works on the fourth.

| Shape | RSS over time | `tracemalloc` | Fix | Rung |
|---|---|---|---|---|
| **Leak** | rises forever, no plateau | rises with it | find the reference (§16) | — |
| **Unbounded cache** | rises, plateaus at a big number | rises with it | bound it (§6) | 2 |
| **Fragmentation** | rises, plateaus, immune to `gc.collect()` | **flat/low** | reduce churn; recycle workers (§13) | 8 |
| **COW breakage** | rises **per worker** after fork | **flat** | change representation (§11) | 7 |
| **Just big** | flat from startup | matches RSS | rungs 1–5 | 1–5 |

### 3.1 The time-series test comes first

The four shapes are distinguished by the *shape of the curve*, not by any single reading.
One RSS number cannot tell a leak from a cache from a large live set. Sample
`memory.current` (or `ps -o rss=`) every 10 s for an hour under representative load, and
plot it. Then:

- **Rises without plateau** → leak or unbounded cache. Distinguish with §16.
- **Sawtooth with a flat floor** → healthy. The allocator is reusing memory.
- **Sawtooth with a rising floor** → fragmentation. §13 measures exactly this.
- **Step up per worker after fork, then flat** → COW breakage. §11.

### 3.2 The fragmentation signature, measured

`sys._debugmallocstats()` settles fragmentation in one reading. After loading 500,000
2-tuples and deleting every other one *(measured)*:

```
# arenas allocated total           =                   63
# arenas reclaimed                 =                    0
# arenas allocated current         =                   63
63 arenas * 1048576 bytes/arena    =           66,060,288
# bytes in allocated blocks        =           33,415,824      ← live
# bytes in available blocks        =           32,265,440      ← free, unreturnable
Total                              =           66,060,288
```

**48.8% of the resident arena space is free and cannot be given back**, and
`# arenas reclaimed = 0` says so directly. That is the diagnostic: *if `bytes in available
blocks` is a large fraction of `Total`, and `arenas reclaimed` is near zero, you have
fragmentation, and no amount of `gc.collect()` will help* — there is nothing to collect.
[`07`](07-virtual-memory.md) §14 measured the extreme case: 20,000 surviving objects
pinning 211 MB when scattered versus 22 MB when contiguous, a **9.5× difference decided
purely by placement**.

The corollary that people resist: **fragmentation is not fixed by allocating less. It is
fixed by allocating differently, or by restarting.** §13.

---

## 4. The ladder, ordered by effect size

This is the document in one table. Work down it. **The ordering is by measured effect on
this machine, and it is not the order people actually try things in** — the last column
says where the popular starting point sits.

| Rung | Move | Measured effect | Cost to you | §|
|---|---|---|---|---|
| **1** | **Don't hold it** — stream instead of materialize | **2,095.9 MB → 0.1 MB peak** | Restructure the pipeline; sometimes *faster* | §5 |
| **2** | **Bound every cache** | unbounded → bounded, by construction | One decorator argument | §6 |
| **3** | **Don't duplicate** — dedup strings on ingest | **1.00× → 0.79×** | ~10 lines at the parse boundary | §7 |
| **4** | **Change the representation** — dict → slots | **1.00× → 0.56×** (0.77× vs plain class) | Class rewrite; ← *most people start here* | §8 |
| **5** | **Leave the object graph** — arrays/buffers | **1.00× → 0.09–0.12×** | Rewrite access patterns | §9 |
| **6** | **`mmap` what you only partly read** | **512 MB → 1.1 MB** for a 1 MB slice | Careful about §10's traps | §10 |
| **7** | **Share it across workers** — COW-friendly | **261 MB → 16 MB privatised per worker** | Depends on rung 5 | §11 |
| **8** | **Tune the allocator / recycle workers** | 0% here; real on glibc | Config only | §12, §13 |

Three observations about this ordering that are worth more than the table itself:

**Rungs 1–3 are cheap and are almost never done.** They require reading the data-flow, not
rewriting a class. Rung 1 changed peak RSS by four orders of magnitude here, and its diff
is generally "delete some square brackets."

**Rung 4 is where nearly everyone starts, and it is a 1.3–1.8× move.** `__slots__` is the
most-recommended Python memory advice in existence and it is *fourth*. This is not an
argument against it — it is an argument about sequencing.

**Rung 5 is the discontinuity.** Rungs 1–4 shave a per-object constant. Rung 5 removes the
per-object constant entirely, which is why it is an order of magnitude rather than a
factor, and why it is the precondition for rung 7 working at all.

**Rung 8 is last for a reason.** It is the only rung you can apply without understanding
your program, which is exactly why it is the one people reach for, and why its measured
effect here was **zero** (§12).

---

## 5. Rung 1 — Don't hold it: streaming, measured

Three implementations of one pipeline: generate 2,000,000 records, enrich each, filter to
one third, sum a field. Identical output, identical per-record work, one process each
*(measured)*:

```python
def source():
    for i in range(N):
        yield {"id": i, "v": i * 1.5}

def enrich(r):
    r = dict(r); r["v2"] = r["v"] * 2.0; return r

def keep(r):
    return r["id"] % 3 == 0
```

**Arm A — materialize every stage** (the shape most pipelines are written in):

```python
rows = list(source())
rows = [enrich(r) for r in rows]
rows = [r for r in rows if keep(r)]
result = sum(r["v2"] for r in rows)
```

**Arm B — stream** (generator expressions; not one `list`):

```python
g = source()
g = (enrich(r) for r in g)
g = (r for r in g if keep(r))
result = sum(r["v2"] for r in g)
```

**Arm C — materialize once**, fused into a single comprehension:

```python
rows = [enrich(r) for r in source() if keep(r)]
result = sum(r["v2"] for r in rows)
```

| Arm | Result | **Peak RSS** | `tracemalloc` peak | at end | Time |
|---|---|---|---|---|---|
| **A** materialize every stage | 1999998999999.0 | **2,095.9 MB** | 887.2 MB | 173.5 MB | 3,229 ms |
| **B** stream | 1999998999999.0 | **0.1 MB** | 0.0 MB | 0.0 MB | 1,094 ms |
| **C** materialize once | 1999998999999.0 | **464.7 MB** | 173.5 MB | 173.5 MB | 855 ms |

**A to B is 2,095.9 MB → 0.1 MB — and B is also 3.0× faster.** This is the doc's first
thesis, and it is not a memory/speed trade-off. It is both, in the same direction, because
the 2 GB that arm A materialized also had to be allocated, faulted in, and dragged through
cache ([`01`](01-memory-hierarchy-and-caches.md)).

Four things to read out of that table, in order of how often they are missed:

**1. Peak is set by stage overlap, not by data size.** Arm A's final live set is 173.5 MB
(the `tracemalloc` "at end" column) — the same as arm C's. Its peak is 12× that, because
`list(source())`, the enriched list, and the filtered list were alive simultaneously. **A
list comprehension over a list you still hold is, for one moment, two copies.** Chain
three of them and you own the whole input three times over.

**2. The intermediate `list()` is the bug.** `rows = list(source())` is the single line
that converts a streaming problem into a 2 GB one. It appears constantly, because
generators are single-use and someone got bitten by iterating one twice.

**3. `tracemalloc` understated peak RSS by 2.36×** (887.2 vs 2,095.9) — an independent
reproduction of [`07`](07-virtual-memory.md) §15.1's 3.26×. Same lesson: the gap is
allocator overhead, arena retention, and interpreter memory, none of which `tracemalloc`
counts.

**4. Streaming is not free CPU-wise: B (1,094 ms) is slower than C (855 ms).** Three
chained generators cost a resume/yield pair per item per stage
([`28`](28-asyncio-internals.md) §2 measures the analogous cost for `await`). The honest
framing: **streaming beat naive materialization on both axes; it lost to *fused*
materialization on time by 28% while winning on peak memory by 4,600×.** If your working
set fits comfortably, fuse. If it doesn't, stream.

### 5.1 The rung-1 checklist

- Every `list(...)`, `.readlines()`, `.read()`, `json.load` of a whole file, `.fetchall()`,
  and `dict(...)` over a generator is a materialization point. **Each one is a decision;
  most were not decided.**
- `for line in f:` streams; `for line in f.readlines():` does not.
- `sum`, `min`, `max`, `any`, `all`, `set`, `"".join` all accept iterators. Passing a list
  comprehension where a generator expression would do is a materialization
  (`"".join([...])` is the documented exception — `join` needs two passes and materializes
  internally anyway).
- Sorting and `random.shuffle` genuinely need the whole thing. Say so explicitly, and
  make that the *only* place the data is whole.
- Database cursors: `fetchall()` vs server-side cursors is the same decision at the
  driver layer, and the driver's default is usually `fetchall`.
- If a stage must be re-read, write it to a file and re-stream it. **Disk is a legitimate
  place to put a pipeline stage**, and §10 makes re-reading it cheap.

---

## 6. Rung 2 — Bound every cache

Rung 2 is short because it is not subtle. It is on the ladder above representation changes
because **an unbounded cache defeats every other optimization**: making objects 40%
smaller extends the time to OOM by 40% and changes nothing else.

The rule: **every cache needs an eviction policy chosen on purpose, and "the process
restarts eventually" is not one.**

| Pattern | Bounded? | Note |
|---|---|---|
| `@functools.lru_cache(maxsize=None)` / `@cache` | **No** | Grows without limit. The default `maxsize=128` is bounded; `@cache` is not. |
| `@functools.lru_cache(maxsize=10_000)` | Yes | And the entries hold their arguments *and* results alive |
| `_cache = {}` at module scope | **No** | The most common Python leak that is not a leak |
| `weakref.WeakValueDictionary` | Self-limiting | Entries vanish when the value dies elsewhere |
| `functools.cached_property` | Per-instance | Bounded by instance lifetime — which may be the process |
| Memoized methods (`lru_cache` on a method) | **No, and worse** | The cache keys on `self`, so **it keeps every instance alive forever** |

That last row deserves its own sentence, because it is a genuine trap:
`@lru_cache` on a method stores `self` in the key tuple, so the cache is a strong reference
to every instance the method was ever called on. This is a leak with a perfectly innocent
diff. The fix is `@cached_property`, or a module-level cache keyed on the fields that
actually matter.

Sizing a bounded cache is a memory-budget question, not a hit-rate question:
`maxsize × bytes_per_entry` must fit in the budget from §14. Measure `bytes_per_entry`
with the deep sizer from [`16`](16-object-memory-layout.md) §11 — not `sys.getsizeof`,
which will tell you a `dict` value costs 64 bytes when it costs 3 KB.

---

## 7. Rung 3 — Don't duplicate: dedup and interning, measured

Parsers create fresh string objects. `json.loads` does not intern your field values, so a
500,000-record document with 5 distinct category strings produces 500,000 string objects,
not 5. That is a pure-waste factor of 100,000 on those fields, and it costs nothing to fix
at the parse boundary.

500,000 records, four fields each, categorical values drawn from small sets *(measured)*:

| Arm | Peak RSS | vs baseline | Saved |
|---|---|---|---|
| as parsed (fresh strings) | 144.1 MB | 1.00× | — |
| `sys.intern` on every `str` value | **113.5 MB** | **0.79×** | 30.6 MB |
| manual dedup `dict` (no intern) | **113.5 MB** | **0.79×** | 30.6 MB |
| dedup + `__slots__` record (rung 3+4) | **51.9 MB** | **0.36×** | 92.2 MB |

**21% for ten lines at the parse boundary**, and it composes with rung 4 to 0.36×. The
implementation is the entire technique:

```python
def dedup_strings(record, _pool={}):
    for k, v in record.items():
        if type(v) is str:
            record[k] = _pool.setdefault(v, v)
    return record
```

Notes that matter:

**`sys.intern` and a plain `dict` pool measured identically** (113.5 MB both). They are the
same idea; `sys.intern` uses the interpreter's own table. Prefer the manual pool when you
want to *drop* the pool later — the interpreter's interned table is not something you can
clear, and [`16`](16-object-memory-layout.md) §10 measures its lifetime behaviour by
version. Prefer `sys.intern` when you also want the fast-path pointer comparison in dict
lookups.

**`type(v) is str`, not `isinstance`.** `sys.intern` rejects `str` subclasses, and the
identity check is the honest intent here.

**This only pays on repeated values.** Deduplicating UUIDs costs you a dict entry per
unique string and saves nothing. Dedup *categorical* fields: status, region, type, source,
enum-like values, and — often the biggest — repeated **keys** when records are built
dynamically rather than from literals.

**Ints have a smaller version of this problem.** CPython caches small ints
(−5…256); everything above is a fresh 28–32-byte object. If your records carry a bounded
set of large integer codes, the same pool trick works, and `array.array` (§8) removes the
question entirely.

---

## 8. Rung 4 — Change the representation, measured

One million records of `(int, float, int, bool)`, one process per arm, RSS net of a 0.3 MB
baseline arm *(measured)*:

| Representation | Peak RSS | B/record | vs dict |
|---|---|---|---|
| `dict` per record | 278.0 MB | 291.6 | 1.00× |
| plain class (`__dict__`) | 201.5 MB | 211.3 | 0.72× |
| `NamedTuple` | 187.9 MB | 197.0 | 0.68× |
| `tuple` | 170.3 MB | 178.6 | 0.61× |
| `dataclass(slots=True)` | 158.3 MB | 166.0 | 0.57× |
| **`__slots__` class** | **155.0 MB** | **162.5** | **0.56×** |
| **5 parallel `array.array` (SoA)** | **32.5 MB** | **34.1** | **0.12×** |
| **one packed `bytearray` + `struct`** | **23.8 MB** | **25.0** | **0.09×** |

### 8.1 Read this table carefully — the headline number is misleading

`__slots__` shows as **0.56× versus a dict literal**, and you will see that number quoted.
It is the wrong comparison. The honest one is **`__slots__` versus a plain class:
155.0 / 201.5 = 0.77×, a 23% saving** — which is exactly what
[`16`](16-object-memory-layout.md) §9 concludes ("~30%, not ~600%") and why that section
exists. The gap between 0.56× and 0.77× is **key-sharing dicts**
([`16`](16-object-memory-layout.md) §8): a plain class's instance dict already shares its
key table across instances, so it was never paying what a dict *literal* pays. The
`__slots__` "600% win" folklore compares against a straw man.

Meanwhile `dataclass(slots=True)` measured 158.3 MB against a hand-written `__slots__`
class's 155.0 MB — **a 2% difference.** Use the dataclass. It generates the same layout and
you get `__eq__`, `__repr__`, and the field list for free.

### 8.2 The real discontinuity is the last two rows

Everything from `dict` down to `__slots__` is a **1.3–1.8× band**. Then the last two rows
drop by another **5–6×**, and the reason is not that arrays are a cleverer container:

> **A `__slots__` class with four fields still allocates four `PyObject`s per record.**
> The 162.5 bytes is the instance (~72 B) *plus* a fresh `int`, a fresh `float`, and a
> fresh `int` — each a 28–32-byte heap object with a 16-byte header
> ([`16`](16-object-memory-layout.md) §1). `array.array("q")` stores an 8-byte machine
> integer. **The win is not a smaller container; it is that the scalars stop being
> objects.**

25.0 bytes/record for the packed `bytearray` is essentially the payload
(`struct.calcsize("<qdq?")` = 25 with alignment) — the object overhead has gone to zero,
amortized across a single object.

### 8.3 When each row is the right answer

| Situation | Use |
|---|---|
| < 100 k records, or heterogeneous fields | `dict` — it is fine, stop optimizing |
| Many instances, fixed fields, normal Python access | **`dataclass(slots=True)`** |
| Fields accessed positionally, tuple semantics wanted | `NamedTuple` |
| Millions of records, numeric fields, columnar access | **parallel `array.array`, or NumPy** |
| Millions of records, need mmap / IPC / zero-copy | **packed buffer** (§9, §10, §11) |

Costs the table does not show: SoA and packed buffers **lose per-record identity** (no
object to attach a method to, no `weakref`, no subclassing), **lose `None`** (you need a
sentinel or a validity mask), and move bounds-checking into your code. The packed arm's
row access is `fmt.unpack_from(buf, i * fmt.size)` — which allocates a tuple, so
**random single-record access in a packed buffer can be *slower* than a `__slots__`
class**. Packed buffers win when access is bulk or columnar; they lose when it is
"give me record 4,318 and call a method on it."

And the honest ceiling: with NumPy installed (it is not, here — see §18) the SoA row
becomes a `np.recarray` or a set of typed columns, which is the same 0.09–0.12× with a
vectorized API on top. `34-going-native.md` covers that; §9 covers the interop.

---

## 9. Rung 5 — Get the data out of the object graph: buffers and zero-copy

Rung 5 is the generalization of §8.2. Once bulk data lives in one object rather than
millions, three separate wins unlock at once, and the third is the one people don't
anticipate.

1. **Footprint** — measured in §8: 0.09–0.12×.
2. **Copy avoidance** — a buffer can be sliced, passed, and pickled without duplicating.
3. **Copy-on-write survival** — one refcount instead of millions. This is §11, and it is
   worth more than the other two combined in a pre-fork server.

### 9.1 The buffer protocol is the mechanism

`memoryview` exposes any buffer-protocol object as a sliceable, castable, zero-copy view.
Everything in this section is that one idea:

```python
mv = memoryview(buf)
window = mv[1000:2000]          # no copy — a new view over the same pages
cols = mv.cast("d")             # no copy — reinterpret as doubles
mv[0:8] = other[0:8]            # no intermediate bytes object
```

Types that participate: `bytes`, `bytearray`, `array.array`, `mmap.mmap`, NumPy arrays,
Arrow buffers, and any C extension exporting `tp_as_buffer`
([`17-c-api-and-extensions.md`](17-c-api-and-extensions.md)). The request flags
(`PyBUF_SIMPLE` → `PyBUF_ND` → `PyBUF_STRIDES` → `PyBUF_INDIRECT`) determine how much
structure the exporter must describe; strided views are what make a NumPy transpose free.

**The rule that saves the most memory:** slicing `bytes` **copies**; slicing a
`memoryview` **does not**.

```python
chunk = data[1_000_000:2_000_000]              # 1 MB copy
chunk = memoryview(data)[1_000_000:2_000_000]  # 0 bytes
```

A parser that slices its input buffer with `[]` allocates a copy of the entire input,
piecewise, and this is a top-three cause of "the parser uses 4× the file size."

Two gotchas worth knowing before you deploy this:

- **A `memoryview` of a `bytearray` blocks resizing** while it is alive
  (`BufferError: Existing exports of data: object cannot be re-sized`). Release it with
  `mv.release()` or a `with` block.
- **A view keeps the whole underlying object alive.** A 10-byte view of a 2 GB `mmap`
  pins 2 GB of mapping. This is the buffer-protocol equivalent of the classic substring
  leak, and §16's retainer hunt will find it as "2 GB held by one `memoryview`."

### 9.2 Zero-copy across process boundaries: PEP 574

Standard `pickle` copies buffer data into the pickle stream, so sending a 1 GB array to a
worker costs 1 GB in the pickle, 1 GB in the pipe, and 1 GB on the far side. **Pickle
protocol 5** (PEP 574, Python 3.8+) lets large buffers travel *out of band*:

```python
buffers = []
data = pickle.dumps(obj, protocol=5, buffer_callback=buffers.append)
# `data` is small metadata; `buffers` holds PickleBuffer views over the original memory
obj2 = pickle.loads(data, buffers=buffers)
```

PEP 574 states the consequence plainly: in-process, "the unpickled object may be backed by
the same buffer as the original pickled object" — the round trip is genuinely zero-copy.
Across processes you still transfer the bytes, but you transfer them *once*, and you can
transfer them over shared memory instead of a pipe. This is the mechanism behind
`multiprocessing`'s efficient array passing and Dask/Ray's data plane.

The type that opts in is `PickleBuffer`, and a class opts in via `__reduce_ex__`:

```python
def __reduce_ex__(self, protocol):
    if protocol >= 5:
        return type(self)._from_buffer, (pickle.PickleBuffer(self._buf), self._shape)
    return type(self)._from_bytes, (bytes(self._buf), self._shape)
```

PEP 574 deliberately rejects `PickleBuffer` under protocol ≤ 4 rather than silently
copying twice — so the protocol-dependent branch above is mandatory, not defensive.

### 9.3 Arrow, and the columnar option

Apache Arrow is the mature answer to "columnar data that many processes and languages read
without copying." `pyarrow.Buffer` wraps `arrow::Buffer`, supports the Python buffer
protocol, and can be zero-copy sliced; `MemoryMappedFile` combines it with §10. If your
service moves tabular data between Python and anything else — a database driver, Spark,
DuckDB, a Rust extension — Arrow's memory format is likely already what both sides speak,
and going through it removes a serialization *and* a copy. See `34-going-native.md`.

---

## 10. Rung 6 — `mmap` and memory-mapped files, measured

`mmap` is the most over-promised tool in this document. Reading a 512 MB file six ways, one
process per arm *(measured)*:

| Arm | Peak RSS |
|---|---|
| `open(p,"rb").read()`, touch 1 byte per 4 KB | 512.2 MB |
| **`mmap`, touch 1 byte per 4 KB (whole file)** | **512.1 MB** ← *identical* |
| **`mmap`, read only a 1 MB slice** | **1.1 MB** |
| `mmap` + `memoryview` slice of 100 MB, touched | 100.1 MB |
| **`mmap` then `bytes(m)`** | **1,024.2 MB** ← *the trap* |
| `read()` in 1 MB chunks, discard each | 2.1 MB |

### 10.1 The three lessons, in order of how badly they are misunderstood

**1. `mmap` does not reduce RSS for a full sweep.** Row 2 versus row 1: 512.1 vs
512.2 MB. **You pay for every page you touch, exactly as with `read()`.** The folklore
"mmap the file so it doesn't use memory" is false for any access pattern that reads the
whole thing. What `mmap` gives you is *demand paging* — row 3 is the payoff: touching
1 MB of a 512 MB file costs **1.1 MB**, a 465× saving, because
[`07`](07-virtual-memory.md) §4's rule holds — **nothing is resident until it is touched.**

**2. `bytes(m)` doubles everything.** Row 5 is 1,024.2 MB: the mapping is faulted in *and*
a full private copy is made. This is the single most common `mmap` bug, and it hides inside
innocent code — `json.loads(m)`, `m.read()`, `hashlib.sha256(m).hexdigest()` on some
versions, and any `bytes(...)` coercion at an API boundary. **Pass `memoryview(m)` or a
slice of it; never materialize the mapping.**

**3. Chunked reading (row 6, 2.1 MB) beats `mmap` for a pure streaming sweep.** If you
read forward once and discard, a `while chunk := f.read(1<<20)` loop is simpler, more
portable, and lower-footprint than any mapping. `mmap` is for **random access to a subset**.

### 10.2 The lesson the RSS column cannot show

Rows 1 and 2 read 512.1 vs 512.2 MB, and are **not equivalent under memory pressure**:

| | `read()` into `bytes` | `mmap` of a file |
|---|---|---|
| Page type | **anonymous**, private, dirty | **file-backed**, clean |
| To reclaim, the kernel must | **swap it out** (or OOM-kill you) | **drop it** — it's on disk already |
| Under cgroup pressure | counts, and is expensive to evict | counts, and is evicted **first** |
| Cost to get it back | major fault from swap | major fault from page cache/disk (**15.9 µs**, [`07`](07-virtual-memory.md) §3.1) |
| Shared between processes | no — each copy is private | **yes** — one page cache entry serves all |

**The real value of `mmap` is not lower RSS but *reclaimable* RSS, plus sharing across
processes.** Ten workers that `read()` the same 512 MB model hold 5 GB. Ten workers that
`mmap` it share one page-cache copy. Under a cgroup limit, clean file pages are what the
kernel reclaims before it starts thinking about the OOM killer (§14) — so the same number
on the dashboard means "we have slack" in one case and "we are about to die" in the other.
This distinction is invisible to `ru_maxrss` and visible in `smaps_rollup` as
`Shared_Clean` vs `Private_Dirty` ([`07`](07-virtual-memory.md) §10).

### 10.3 When to reach for it

**Use `mmap` when:** the file is much larger than the part you need; many processes read
the same immutable data (models, embeddings, indices, dictionaries); you want persistence
without a serialization step; you need random access with OS-managed caching.

**Do not use `mmap` when:** you sweep the whole file once (chunked `read` is better); the
file is on a network filesystem (a page fault can now block on the network, uninterruptibly,
and `SIGBUS` on truncation becomes a real failure mode); the file may be truncated under
you (`SIGBUS`, not an exception); or you are on 32-bit anything.

**Flags that matter** ([`07`](07-virtual-memory.md) §5 has the full treatment):
`ACCESS_READ` for shared read-only; `ACCESS_COPY` for private copy-on-write (writes are
discarded, never reach the file); `mmap.madvise(mmap.MADV_RANDOM)` to suppress readahead
when access is scattered; `MADV_SEQUENTIAL` when it is not; `MADV_DONTNEED` to drop
resident pages you know you are finished with.

---

## 11. Rung 7 — Share it: COW-friendly forking, measured

This is the doc's second thesis, and its highest-leverage measurement. A pre-fork server
loads data in the parent and forks W workers. In theory every worker shares the parent's
pages. In practice [`07`](07-virtual-memory.md) §7 measured **88% of the parent heap
privatised** by a child that only *read* it, because **a read in Python is a write in
hardware** — every reference touches a refcount, which dirties the page
([`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md)).

The question §11 answers is: *given that, what actually fixes it?* Parent loads 2 M
records, forks one child, child sweeps the data read-only *(measured)*:

| Parent representation | Parent RSS | **Child privatised** | Fraction |
|---|---|---|---|
| 2 M tuples (Python objects) | 278.9 MB | **261.1 MB** | **93.6%** |
| 2 M tuples + `gc.freeze()` | 278.9 MB | **261.1 MB** | **93.6%** |
| 2 `array.array` (one refcount each) | 125.9 MB | **16.0 MB** | **12.7%** |
| one packed `bytes` object | 108.4 MB | **16.2 MB** | 14.9% |

**261.1 MB → 16.0 MB per worker: 16.3×.** And the 16 MB is not inherited pages at all —
it is the child's own allocation during the sweep (the loop creates a fresh `int` per
index). **The array arms privatise essentially nothing.**

### 11.1 `gc.freeze()` did nothing here, and that is not a contradiction

Rows 1 and 2 are identical to the tenth of a megabyte. `gc.freeze()` is the Instagram
technique, [`22`](22-garbage-collection.md) §12.1 documents it, and
[`07`](07-virtual-memory.md) §7 measured it delivering a **295× reduction** — so why zero
here?

Because **there are two COW mechanisms and `gc.freeze()` addresses only one**:

| Mechanism | What dirties the page | `gc.freeze()` |
|---|---|---|
| **GC traversal** — the collector writes `gc_refs` into every tracked object's header | a collection in the child | **fixes it** (295×, [`07`](07-virtual-memory.md) §7) |
| **Refcount writes** — every `Py_INCREF`/`DECREF` on an inherited object | *reading the data at all* | **no effect** |

My child sweeps all 2 M tuples, incrementing and decrementing a refcount on each. That
dirties every page holding an object, and no GC setting can prevent it. [`07`](07-virtual-memory.md)
§16's cost model states both rows explicitly — *"`gc.freeze()` on GC-traversal COW: 295×;
`gc.freeze()` on refcount COW: no effect."* §11 is the second row, measured on a different
workload.

**So `gc.freeze()` remains correct and worth doing** — it is one line and it removes an
entire mechanism — **but it is not a substitute for rung 5.** If your workers read the
shared data, refcount COW will eat the parent's heap no matter what you tell the collector.
Immortal objects (PEP 683, Python 3.12+) remove refcount writes for a specific set —
`None`, `True`, `False`, small ints, interned strings, static types — which is why PEP 683
lists "Avoiding Copy-on-Write" as a motivation and names Instagram and YouTube. It does not
extend to your data.

### 11.2 The pre-fork budget arithmetic

This is why §11 sits where it does on the ladder. Eight workers under a 2 GB limit:

| Representation | Parent | Per worker | **Total for W=8** | Fits in 2 GB? |
|---|---|---|---|---|
| tuples | 278.9 MB | 261.1 MB | **2,367.7 MB** | **No — OOM** |
| `array.array` | 125.9 MB | 16.0 MB | **253.9 MB** | Yes, 8× over |

Same data. Same worker count. Same machine. **The representation decision made in §8 is
what decides whether the deployment in §14 is feasible**, and it is invisible in
single-process testing — the tuples arm looks like a perfectly reasonable 279 MB service
right up until you scale it out.

### 11.3 The full pre-fork checklist

1. **Load everything before forking.** Anything loaded after fork is per-worker by
   definition.
2. **`gc.disable()` early, `gc.collect()` then `gc.freeze()` immediately before fork,
   `gc.enable()` in the child** — the sequence the `gc` docs prescribe verbatim for this
   case. Removes GC-traversal COW.
3. **Put bulk data in buffers, not objects** (§8, §9). This is the 16.3×. Everything else
   is a rounding error against it.
4. **For genuinely shared *mutable* state, use `multiprocessing.shared_memory`** — a named
   POSIX shared-memory block exposing the buffer protocol, so `memoryview`, `array`, and
   NumPy can sit on top with no copy. Remember to `unlink()` exactly once; a leaked block
   survives the process.
5. **For shared *immutable* state, prefer `mmap` of a file** (§10). The page cache does the
   sharing, one copy serves every worker, and the pages are clean and reclaimable.
6. **Measure a child, not the parent.** The parent's RSS is the number that looks fine.
7. **Consider not forking.** `spawn` costs a full interpreter per worker but has no COW
   surprises; free-threading (§15) shares by construction at a measured +23.9% object cost;
   `27-multiprocessing-and-subinterpreters.md` covers per-interpreter GIL (PEP 684) and
   `concurrent.interpreters` (PEP 734).

---

## 12. Rung 8 — The allocator and the runtime

Rung 8 is last because it is the only rung you can apply without understanding your
program. That is precisely why it is the first one people try, and here is what it bought,
measured — 1 M `__slots__` objects, one process per arm:

| Configuration | RSS | Interpreter base |
|---|---|---|
| 3.14, default (`pymalloc`) | 132.4 MB | 15.8 MB |
| 3.14, `PYTHONMALLOC=pymalloc` | 132.4 MB | 15.8 MB |
| **3.14, `PYTHONMALLOC=malloc`** | **132.4 MB** | 16.4 MB |
| 3.14t free-threaded (`mimalloc`) | 164.0 MB | 17.3 MB |

**Bypassing pymalloc entirely changed the footprint by 0.0 MB on this workload.** (The env
var *was* honoured — `sys._debugmallocstats()` prints pymalloc's arena accounting under
`pymalloc` and prints none of it under `malloc`.) macOS's libmalloc happens to have
comparable per-block overhead to pymalloc for this size class. **On glibc the result
differs**, and the difference is the point of the rest of this section — but the
transferable lesson is that **allocator swaps are a single-digit-percent move, and you
should have exhausted rungs 1–7 first.**

### 12.1 `PYTHONMALLOC` — a debugging tool, not a tuning knob

| Value | `PyMem_Malloc` | `PyObject_Malloc` | Use |
|---|---|---|---|
| `pymalloc` | pymalloc | pymalloc | default (GIL build) |
| `mimalloc` | mimalloc | mimalloc | default (free-threaded build) |
| `malloc` | `malloc` | `malloc` | **make every allocation visible to Valgrind/ASan/`memray`** |
| `pymalloc_debug` / `malloc_debug` | + debug hooks | + debug hooks | detect buffer overruns, use-after-free, API misuse |

The real use of `PYTHONMALLOC=malloc` is **visibility**, not footprint: it makes every
Python object allocation a `malloc` call that native tools can see. `memray`'s
`--trace-python-allocators` achieves the same visibility *without* disabling pymalloc,
which is strictly better — you keep the production allocator and still see each object.

### 12.2 glibc specifics *(cited — Linux, not measured here)*

Three glibc behaviours cause "our container's RSS is much higher than our Python heap" on
Linux, and none of them exists on macOS:

**Per-thread arenas.** glibc creates additional malloc arenas as threads contend, up to
8 × `nproc` on 64-bit. In a container with a low memory limit and a high visible core
count, this can add hundreds of MB of unusable-but-resident heap. `MALLOC_ARENA_MAX=2` is
the standard mitigation. This is the most common cause of the "same app, more RSS in
Kubernetes than on my laptop" report.

**`malloc_trim(0)`.** `malloc_trim(3)` "attempts to release free memory from the heap by
calling `sbrk(2)` or `madvise(2)`," and — importantly — **"since glibc 2.8 this function
frees memory in all arenas and in all chunks with whole free pages"**, not just the main
arena's top. Calling it after a large batch job can return real RSS. It is not free (it
walks the arenas) and it cannot fix pymalloc-level fragmentation (§13), only glibc-level.
Reach it via `ctypes.CDLL("libc.so.6").malloc_trim(0)`.

**The dynamic mmap threshold.** `mallopt(3)` documents that glibc adjusts `M_MMAP_THRESHOLD`
upward at runtime — starting at 128 KB, rising toward 32 MB as large blocks are freed —
and that **"dynamic adjustment of the mmap threshold is disabled if any of `M_TRIM_THRESHOLD`,
`M_TOP_PAD`, `M_MMAP_THRESHOLD`, or `M_MMAP_MAX` is set."** So setting one tunable silently
disables the adaptive behaviour of the others. Blocks above the threshold are `mmap`ed and
returned to the OS on `free`; blocks below it go on the heap and are subject to trimming
policy. This is why a workload that allocates 1 MB buffers may return memory promptly and
one that allocates 100 KB buffers may not.

### 12.3 Replacement allocators *(cited)*

Swapping in **jemalloc** (`LD_PRELOAD=libjemalloc.so`) or **mimalloc** is the standard
last-resort move for RSS on long-running Linux services, and both expose the knob that
actually matters — how aggressively unused pages go back to the OS:

- **jemalloc**: `dirty_decay_ms` / `muzzy_decay_ms` control how fast unused pages are
  purged; `background_thread:true` moves that purging off the application threads, which
  its own tuning guide recommends because it "generally improves the tail latency for
  application threads."
- **mimalloc**: `MIMALLOC_PURGE_DELAY=N` (default 1000 ms) sets the delay before unused OS
  pages are purged; `0` purges immediately (lower RSS, slower); `-1` disables purging.
  `MIMALLOC_PURGE_DECOMMITS=1` uses `MADV_DONTNEED`, **which decreases RSS immediately**,
  versus `MADV_FREE`, which does not.

That last distinction is the one to internalize, and [`07`](07-virtual-memory.md) §13
measures it: **`MADV_FREE` means the allocator released it, the kernel accepted it, and
RSS still does not drop until there is pressure.** An allocator swap can therefore make
your dashboard look *worse* while making your system healthier — or look better while
adding latency. Decide which you are optimizing before you turn the knob.

---

## 13. Arena return behaviour and worker recycling, measured

The question every service owner eventually asks — *"I freed it, why is RSS still high?"* —
is settled definitively in [`07`](07-virtual-memory.md) §14 (four arms, 9.5× decided by
survivor placement). What §13 adds is the **longitudinal** view: what happens over repeated
bursts, which is what a real server actually does.

One process, sequential phases, current RSS via `ps` *(measured)*:

```
start            rss=  18.7 MB   peak=  18.7 MB
after burst      rss= 466.1 MB   peak= 466.1 MB   ← 2M dicts allocated
after free-all   rss=  38.1 MB   peak= 466.1 MB   ← 91.8% returned ✓
2nd burst        rss= 481.4 MB   peak= 481.4 MB
after free-all   rss=  54.4 MB   peak= 481.4 MB   ← floor rose 16.3 MB
1% scattered     rss= 496.7 MB   peak= 496.7 MB   ← 20,000 objects alive
```

Three findings, and the third is the operationally important one:

**1. Freeing *everything* does return memory.** 466.1 → 38.1 MB, 91.8% returned. The
folk claim "CPython never gives memory back" is false. An arena is returned when *every*
pool in it is free ([`16`](16-object-memory-layout.md) §3), and when you drop the whole
working set, that condition is met.

**2. The floor rises with each cycle.** 18.7 → 38.1 → 54.4 MB. This is the **sawtooth with
a rising floor** from §3.1 — the signature of fragmentation, distinguished from a leak by
the fact that it rises *per cycle* rather than per unit of work, and that it decelerates.

**3. 20,000 surviving objects held 496.7 MB.** The last line reproduces
[`07`](07-virtual-memory.md) §14's scattered-survivor result on a different workload:
~1% of the objects pinning ~100% of the arenas. This is the shape that looks exactly like a
leak on a dashboard and is not one.

### 13.1 What you can actually do about it

Ranked by effectiveness, which is the reverse of how appealing they sound:

| Move | Effect | Note |
|---|---|---|
| **Don't create the churn** (§5) | Removes the problem | Streaming allocates fewer objects that outlive each other |
| **Keep bulk data out of pymalloc** (§8, §9) | Removes the problem | Blocks > 512 B bypass pymalloc entirely; one big buffer has no fragmentation |
| **Batch allocations by lifetime** | Large | Objects that die together should be *born* together — that is what makes survivors contiguous |
| **Recycle workers** (`--max-requests`) | Reliable | The only cure that works after the fact |
| `gc.collect()` | **Zero** | There is nothing to collect. This is the #1 wasted fix |
| `malloc_trim(0)` (glibc) | Partial | Reaches glibc's heap, not pymalloc's arenas |

**Worker recycling is not an admission of defeat.** Gunicorn's `--max-requests` (with
`--max-requests-jitter` so workers don't all restart together) exists precisely because
fragmentation is a property of allocation *history*, and the cheapest way to discard a
history is to discard the process. Set the limit from the measured floor-rise per cycle
and your budget headroom: if the floor rises 16 MB per 2 M-object burst and you have
400 MB of headroom, you have 25 bursts before recycling. Choose a number well inside that.

---

## 14. Memory budgets per container *(cited)*

> **Linux only, and not measured on this machine** — macOS has no cgroups. Everything here
> is from the kernel's cgroup-v2 documentation. Verify on your platform.

### 14.1 The arithmetic

The budget is not "how much memory does the app use." It is:

```
memory.max  ≥  parent RSS
             + W × per-worker private RSS        ← §11 decides this term
             + page cache you actually need       ← §10 lives here
             + peak transient allocation          ← §5 decides this term
             + headroom for the spike you haven't seen yet
```

**Two terms in that sum are set by decisions from earlier sections**, which is the reason
this document is ordered the way it is. §11 measured `W × per-worker` swinging from
2,367.7 MB to 253.9 MB on the same data. §5 measured the transient term swinging by
2,095.8 MB on the same pipeline. **You cannot budget your way out of a §5 or §11 mistake;
you can only pay for it in instance size.**

### 14.2 The interfaces to read

| File | What it tells you |
|---|---|
| `memory.current` | Current charge — **the number that is compared against the limit** |
| `memory.peak` | High-water mark since creation or last reset (writable to reset) |
| `memory.max` | The hard limit. Exceeding it triggers reclaim, then OOM |
| `memory.high` | Soft limit — throttles and reclaims rather than killing |
| `memory.stat` | The breakdown: `anon`, `file`, `slab`, `sock`, and more |
| `memory.events` | `low`, `high`, `max`, `oom`, `oom_kill` counters — **"have we been killed before?"** |
| `memory.oom.group` | If set, the whole cgroup is killed together rather than one task |
| PSI: `memory.pressure` | `full avg60` — **is this hurting, or just large?** |

The `anon` vs `file` split in `memory.stat` is §10.2's distinction made operational: `file`
is reclaimable, `anon` mostly is not. A container at 95% of its limit that is 80% `file` is
healthy; the same number that is 95% `anon` is one allocation from death.

**`memory.events`' `oom_kill` counter is the first thing to read on any memory
investigation**, before any profiler. It answers "is this actually happening" in one
`cat`, and it distinguishes "the app crashed" from "the kernel killed it."

### 14.3 Exit code 137 and what it does and does not mean

A container that exits **137** was killed by SIGKILL (128 + 9). In Kubernetes this usually
means the cgroup OOM killer fired, and the pod shows `OOMKilled`. Three things people get
wrong:

- **No Python traceback exists.** SIGKILL cannot be handled ([`10-signals-fork-exec.md`](10-signals-fork-exec.md)),
  so there is no `MemoryError`, no `atexit`, no log line. **The absence of an error in your
  logs is the expected observation, not evidence against OOM.**
- **`MemoryError` is a *different* event.** That is `malloc` returning NULL, which on an
  overcommitting Linux system ([`07`](07-virtual-memory.md) §8) is rare. Getting
  `MemoryError` usually means you hit `RLIMIT_AS` or a 32-bit address-space limit, not that
  the machine was out of memory.
- **The killed process is not always the guilty one.** The cgroup OOM killer picks by
  `oom_score`, so a small sidecar can die for the main process's allocation. `memory.oom.group`
  makes the kill unit explicit.

### 14.4 Setting the limit

- **Set `memory.max` from measured peak, not average.** §2.3 is why `ru_maxrss` exists.
- **Set `memory.high` below `max`** so you get reclaim pressure and PSI signal *before*
  the kill. A pod that logs rising `memory.pressure` for ten minutes is diagnosable; one
  that vanishes is not.
- **Alert on `memory.events`' `high` and `oom` counters**, not on a percentage of the
  limit. The counters are events; the percentage is a level that page cache makes noisy.
- **Requests vs limits**: in Kubernetes, scheduling uses `requests` and killing uses
  `limits`. Setting them equal (Guaranteed QoS) removes a class of surprise where the node
  is overcommitted and your pod dies for someone else's spike.
- **Budget the workers, not the process.** §11.2's table is the calculation, and
  `W × per-worker` is the term you have the most leverage over.

---

## 15. The free-threaded build's memory story, measured

[`26-free-threading.md`](26-free-threading.md) measures the **+8.1% single-thread CPU tax**.
The memory tax is separate and larger. 1 M `__slots__` objects, same source, two builds
*(measured)*:

| Build | RSS for 1 M objects | Interpreter base |
|---|---|---|
| 3.14 (GIL, pymalloc) | **132.4 MB** | 15.8 MB |
| 3.14t (free-threaded, mimalloc) | **164.0 MB** | 17.3 MB |
| **Difference** | **+23.9%** | +9.5% |

**+23.9% per object.** The mechanism is in [`16`](16-object-memory-layout.md) §2: the
free-threaded object header carries additional fields for biased reference counting and
per-object locking, and the build uses mimalloc rather than pymalloc, with different size
classes and different page-retention policy (§12.3).

For a memory budget the trade is explicit, and it can go either way:

- **Against**: every object costs ~24% more, so a memory-bound single-process workload gets
  smaller by that factor.
- **For**: free-threading lets you replace W processes with W threads in **one** address
  space. §11.2's tuples row — 278.9 MB parent + 8 × 261.1 MB — becomes roughly
  `1.24 × 278.9 ≈ 346 MB` **total**, against 2,367.7 MB for the pre-fork arm. That is a
  6.8× reduction *from the concurrency model*, which swamps the 23.9% object tax by a
  factor of 28.

**So the free-threaded build's memory tax is a per-object cost that buys the elimination of
per-worker duplication** — which means it is a loss for single-process batch jobs and
potentially an enormous win for pre-fork web servers whose workers share a large read-only
dataset. That is exactly the population §11 is about. [`26`](26-free-threading.md)'s
decision framework and ecosystem audit apply before you act on this.

---

## 16. Object-graph analysis: finding the retainer

Every leak in Python is the same bug: **something you forgot about holds a reference.** The
skill is not finding *what* is large (that is easy) but *who* is holding it (that is the
work).

### 16.1 The workflow

**Step 1 — Confirm it's a leak, not one of the other three shapes.** §3. Skipping this step
is how people spend a week profiling a fragmentation problem.

**Step 2 — Find what is growing, by type.** Cheap, works in production, no profiler:

```python
import gc, collections
def type_histogram(top=15):
    c = collections.Counter(type(o).__name__ for o in gc.get_objects())
    return c.most_common(top)
```

Sample it twice, minutes apart, under load, and diff. The type that grows monotonically is
your target. `objgraph.show_growth()` is the packaged version of exactly this. Note that
`gc.get_objects()` only returns **GC-tracked** objects ([`22`](22-garbage-collection.md)
§3) — it will not show you a growing `bytes` or a growing `str`, which is a real blind
spot with a real workaround: watch the containers holding them instead.

**Step 3 — Find who refers to it.**

```python
import gc
victims = [o for o in gc.get_objects() if type(o).__name__ == "Suspect"]
refs = gc.get_referrers(victims[0])
for r in refs:
    print(type(r), repr(r)[:120])
```

`gc.get_referrers` is slow (it walks every tracked object) and returns frames and temporary
containers that are artifacts of your own inspection — including the list you just built.
`objgraph.show_backrefs([obj], max_depth=5)` renders the same information as a graph, which
is dramatically easier to read, and `objgraph.find_backref_chain` gives you the shortest
path from a GC root.

**Step 4 — Confirm the size.** `sys.getsizeof` will lie to you
([`16`](16-object-memory-layout.md) §11 has a correct deep sizer). Use it, or use `memray`.

**Step 5 — Attribute to code.** `memray` is the tool. `memray run --native` captures C/C++
stacks too, so a leak inside a native extension is visible; `memray run --follow-fork`
handles pre-fork servers; `--trace-python-allocators` records every object rather than only
the requests that reached the system allocator, at a substantial slowdown. Its flamegraph
answers "which line allocated the bytes that are still alive."

### 16.2 The usual suspects, in the order I would check them

| Suspect | Signature |
|---|---|
| Module-level `dict`/`list` accumulator | Grows forever; the classic |
| `@lru_cache`/`@cache` with no `maxsize` | §6 |
| `@lru_cache` on a **method** | Pins every instance ever seen. §6 |
| Logging handlers holding records / exception objects | Tracebacks hold frames hold locals hold **everything** |
| A cycle with `__del__` on 3.3 and earlier | Uncollectable. PEP 442 fixed it ([`22`](22-garbage-collection.md) §7) |
| `sys.exc_info()` / a caught exception stored in a local | The traceback holds the entire frame stack |
| A `memoryview`/slice pinning a huge buffer | §9.1 — 10 bytes holding 2 GB |
| Thread-locals in a thread pool | Bounded by pool size × per-thread data, and pools outlive requests |
| C-extension refcount bugs | `objgraph.get_leaking_objects()`; invisible to `tracemalloc` |
| `asyncio` tasks nobody awaits | The event loop holds them ([`29`](29-async-patterns-and-pitfalls.md)) |

### 16.3 The production-safe subset

`memray` and `tracemalloc` are too expensive to leave on (§2.4: 2.58× RSS, 10.3× time).
What you *can* run continuously:

1. `memory.events` / `memory.peak` scraped as metrics — free (§14.2).
2. `len()` of every cache, exported as a gauge — free, and it catches §6 directly.
3. A type histogram (step 2) on a timer, sampled — expensive but bounded; run it every few
   minutes on one instance, not every request on all of them.
4. `gc.get_stats()` collection counts — free, and rising gen-2 counts correlate with a
   growing live set.
5. `sys._debugmallocstats()` to stderr on a signal (§3.2) — free until triggered.

The ordering principle from [`07`](07-virtual-memory.md) §15.2 applies unchanged: **the
cheap rungs tell you which *kind* of problem you have; the expensive rung tells you which
line. Most people start at the expensive rung, find nothing, and conclude there is no
problem.**

---

## 17. The cost model

Everything above as numbers to reason with. Measured on this machine unless the row says
otherwise.

| Fact | Number | Source |
|---|---|---|
| **Materialize-every-stage vs stream, peak RSS** | **2,095.9 MB → 0.1 MB** | measured, §5 |
| Streaming vs naive materialization, time | **3.0× faster** | measured, §5 |
| Streaming vs *fused* materialization, time | 1.28× slower | measured, §5 |
| `tracemalloc` peak vs RSS peak | **2.36× understated** (3.26× in [`07`](07-virtual-memory.md) §15) | measured, §5 |
| `tracemalloc` memory overhead | **2.58×** | measured, §2.4 |
| `tracemalloc` time overhead | **10.3×** | measured, §2.4 |
| `tracemalloc` frame depth 1 vs 25, cost | **no difference** (few call sites) | measured, §2.4 |
| String dedup on categorical fields | **0.79×** | measured, §7 |
| Dedup + `__slots__` | **0.36×** | measured, §7 |
| dict → `__slots__` | 0.56× | measured, §8 |
| **plain class → `__slots__`** (the honest comparison) | **0.77×** | measured, §8 |
| `dataclass(slots=True)` vs hand-written `__slots__` | 1.02× — use the dataclass | measured, §8 |
| **objects → parallel `array.array`** | **0.12×** | measured, §8 |
| **objects → packed buffer** | **0.09×** | measured, §8 |
| `mmap` whole-file sweep vs `read()` | **identical** (512.1 vs 512.2 MB) | measured, §10 |
| `mmap` 1 MB slice of a 512 MB file | **1.1 MB (465×)** | measured, §10 |
| `bytes(mmap_obj)` | **2× the file** | measured, §10 |
| Chunked `read()` sweep | 2.1 MB | measured, §10 |
| **Fork child privatised, tuples** | **93.6% of parent** | measured, §11 |
| **Fork child privatised, arrays** | **12.7%** | measured, §11 |
| **Per-worker cost, tuples → arrays** | **16.3×** | measured, §11 |
| `gc.freeze()` on refcount COW | **no effect** | measured, §11 |
| `gc.freeze()` on GC-traversal COW | 295× | [`07`](07-virtual-memory.md) §7 |
| `PYTHONMALLOC=malloc` on macOS | **0% change** | measured, §12 |
| Free-threaded per-object tax | **+23.9%** | measured, §15 |
| Free-threaded interpreter base tax | +9.5% | measured, §15 |
| Full free-all returns to OS | **91.8%** | measured, §13 |
| Fragmentation floor rise per burst cycle | **+16.3 MB** | measured, §13 |
| Scattered 1% survivors pin | **~100% of arenas** | measured §13; [`07`](07-virtual-memory.md) §14 |
| CPython interpreter baseline RSS | 15.8 MB (16.4 MB with `PYTHONMALLOC=malloc`) | measured, §12 |

**Five sentences to remember:**

1. **Peak RSS is set by how many stages are alive at once, not by how big your data is.**
2. **`tracemalloc` measures requests; the OOM killer counts pages; the gap is 2–3× and it
   is not a bug in either.**
3. **`__slots__` is a 23% win and it is rung 4; the order-of-magnitude win is getting the
   scalars out of the object graph entirely.**
4. **`mmap` does not lower RSS for data you read — it makes the RSS *reclaimable and
   shared*, which is a different and usually better property.**
5. **In a pre-fork server, your representation choice is your worker-count choice**, and
   `gc.freeze()` fixes the smaller of the two COW mechanisms.

---

## 18. What I could not verify

Stated plainly, in the spirit of [`07`](07-virtual-memory.md) §19.

**1. Everything about cgroups, the OOM killer, PSS/USS, `malloc_trim`, `MALLOC_ARENA_MAX`,
and glibc trim behaviour (§12.2, §14).** macOS has no `/proc`, no `smaps`, and no cgroups.
Those sections are cited from the kernel documentation, `mallopt(3)`, and `malloc_trim(3)`,
and are marked *(cited)*. **I have no measurement on this hardware to back any of them.**
The §14 arithmetic is arithmetic; the interface descriptions are quotes.

**2. NumPy, memray, psutil, and pyarrow are not installed here.** The §8 table's SoA row
uses `array.array` as the stand-in for a typed column, and §9's Arrow material and §16's
`memray` workflow are described from their documentation, not exercised. The `array.array`
result (0.12×) is a lower bound on what NumPy would show, since NumPy adds a vectorized API
over the same flat storage — but I did not measure it.

**3. `PYTHONMALLOC=malloc` measuring 0.0 MB of difference (§12) is a macOS result, and I
did not establish *why* to my satisfaction.** The env var was honoured (verified via
`sys._debugmallocstats()`). My hypothesis is that macOS libmalloc's per-block overhead for
this size class happens to match pymalloc's, but I have not instrumented libmalloc's
size-class table to confirm it. **Do not carry this number to glibc.** On glibc I would
expect pymalloc to win measurably on small-object footprint, and the honest statement is
that I have not measured it.

**4. The §11 fork experiment used one child, not W children.** The §11.2 budget table
extrapolates linearly (`parent + W × per-worker`), which is right for private pages and
wrong at the margin — workers touch overlapping subsets, so real privatisation per worker
is somewhat below the single-child figure. **The 16.3× ratio between representations is the
claim; the absolute 8-worker totals are a model.**

**5. The §13 churn measurement is one process, one shape of object, five phases.** The
16.3 MB/cycle floor rise is not a universal constant — it depends on object size class,
survivor placement, and allocation order. Treat the *shape* (rising floor) as the finding
and the *slope* as workload-specific.

**6. Timing columns throughout are single-run, best-effort, and on a heterogeneous CPU.**
Per [`31`](31-measurement-methodology.md) §3.2, cluster migration alone can move them
double-digit percentages. The memory columns are quiet (< 0.5 MB across repeats); the time
columns are not, and I have flagged them where I lean on them (§5).

---

## 19. Lab exercises

**1 — Reproduce the thesis.** Write any three-stage pipeline over ≥ 1 M records. Implement
it materialized and streamed. Measure peak RSS with `ru_maxrss` (units per §2.2) in fresh
processes. You should see two to four orders of magnitude. Then add a fourth arm that
materializes only the *final* stage, and explain why it is not halfway between.

**2 — Find your own §2.2 bug.** Grep your codebase and your dashboards for `ru_maxrss`,
`maxrss`, and `getrusage`. Determine which platform each consumer assumes. Fix or document
every one.

**3 — Build the representation table for your data.** Take a real record type from your
system and implement it as `dict`, `dataclass(slots=True)`, `NamedTuple`, and parallel
`array.array`. Load 1 M of them, one process per arm. Then compute the §11.2 budget table
for your actual worker count and limit. **Note whether the answer changes your deployment.**

**4 — Measure your own COW tax.** Load your largest read-only structure, `fork()`, have the
child sweep it read-only, and report `ru_maxrss` delta *in the child*. Then add
`gc.collect(); gc.freeze()` before the fork and re-measure. Predict, before you run it,
whether freeze will help — using §11.1's two-mechanism table — and say why.

**5 — The `bytes(m)` trap in the wild.** `mmap` a file ≥ 512 MB and pass it to three
library functions of your choosing (a parser, a hasher, a compressor). Measure peak RSS for
each. Find at least one that silently materializes the mapping, and find the line in its
source that does it.

**6 — Fragmentation, deliberately.** Allocate 2 M objects and free 99% in two arms:
contiguous survivors and scattered survivors. Confirm [`07`](07-virtual-memory.md) §14's
9.5×. Then dump `sys._debugmallocstats()` for both and identify the two lines that
distinguish them without any RSS reading at all.

**7 — Build the production-safe monitor.** Implement §16.3's five signals as a module that
adds < 1% overhead. Include a `SIGUSR1` handler that dumps a type histogram and
`sys._debugmallocstats()` to stderr. Run it against exercise 6's fragmentation arm and
confirm you can identify the shape from the output alone.

**8 — Dedup your real ingest path.** Add §7's `dedup_strings` at your parse boundary,
measure peak RSS before and after on a real payload, and compute the dict-pool's own cost.
Find the payload size at which the pool costs more than it saves.

**9 — *(Linux)* The container arithmetic, for real.** Run your service in a cgroup with
`memory.max` set to 1.3× measured peak. Drive it to OOM. Confirm exit 137, confirm
`memory.events`' `oom_kill` incremented, and confirm no Python traceback was produced.
Then set `memory.high` at 1.0× peak and observe PSI `memory.pressure` rise *before* the
kill. Report which signal you would have alerted on.

**10 — *(Free-threaded)* The concurrency-model trade.** Build the §15 comparison for your
own data on `3.14` and `3.14t`. Then compute total footprint for W=8 under (a) pre-fork
with tuples, (b) pre-fork with arrays, (c) free-threaded with tuples. Rank them, and check
your ranking against [`26`](26-free-threading.md)'s decision framework before you believe it.

---

## 20. Question bank

1. Name the four memory numbers from §1 and state which one the OOM killer uses. *(§1)*
2. Two pipelines produce identical output and allocate identical objects. One peaks at
   2 GB, the other at 0.1 MB. What differs? *(§5)*
3. `ru_maxrss` returns 230,080,512 on a process you know used ~220 MB. What platform are
   you on? *(§2.2)*
4. Why does `ru_maxrss` not fall after you free 200 MB? What should you read instead? *(§2.3)*
5. `tracemalloc` says 887 MB; RSS peaked at 2,096 MB. Give three components of the gap. *(§2.1, §5)*
6. Why did increasing `tracemalloc`'s frame depth from 1 to 25 cost nothing on the §2.4
   workload, and when would it cost a lot? *(§2.4)*
7. RSS rises, plateaus, and `gc.collect()` does not move it, while `tracemalloc` stays flat.
   What shape is this and what is the fix? *(§3)*
8. Which two lines of `sys._debugmallocstats()` diagnose fragmentation? *(§3.2)*
9. Why is `__slots__` rung 4 rather than rung 1? *(§4)*
10. `__slots__` measured 0.56× against a dict and 0.77× against a plain class. Which is the
    honest number, and what mechanism explains the difference? *(§8.1)*
11. Why do parallel arrays beat `__slots__` by 5× when both hold four fields per record? *(§8.2)*
12. Give two things you lose by moving to a packed buffer, and one access pattern where the
    packed buffer is *slower*. *(§8.3)*
13. `@lru_cache` on a method leaks. Precisely what does it retain, and why? *(§6)*
14. Slicing `bytes` versus slicing a `memoryview`: what is the memory difference, and what
    is the lifetime hazard of the cheaper one? *(§9.1)*
15. What does pickle protocol 5 change, and why does PEP 574 refuse to serialize a
    `PickleBuffer` under protocol 4? *(§9.2)*
16. `mmap`ing a 512 MB file and sweeping it used the same RSS as `read()`ing it. So what is
    `mmap` actually for? *(§10.1, §10.2)*
17. Two processes show 512 MB RSS; one used `read()`, one used `mmap`. Which is closer to
    being OOM-killed, and why? *(§10.2)*
18. A forked child that only *reads* inherited data privatised 93.6% of the parent's heap.
    Why? *(§11, [`15`](15-refcounting-and-ownership.md))*
19. `gc.freeze()` gave 295× in [`07`](07-virtual-memory.md) §7 and 0× in §11. Both are
    correct — explain. *(§11.1)*
20. Your 8-worker service OOMs at 2 GB. Single-process testing showed 279 MB. Where did
    2 GB come from, and what is the one change that fixes it? *(§11.2)*
21. Why is `PYTHONMALLOC=malloc` a debugging tool rather than a tuning knob, and what does
    `memray --trace-python-allocators` do better? *(§12.1)*
22. What is `MALLOC_ARENA_MAX` for, and why does the problem it solves appear in containers
    but not on your laptop? *(§12.2)*
23. Setting `M_TRIM_THRESHOLD` has a documented side effect on an unrelated tunable. What is
    it? *(§12.2)*
24. `MADV_FREE` versus `MADV_DONTNEED`: which one moves your RSS graph, and which one is
    better for your system? *(§12.3, [`07`](07-virtual-memory.md) §13)*
25. Freeing everything returned 91.8% of RSS, but the floor rose 16.3 MB per cycle. Name
    both phenomena. *(§13)*
26. Why is `gc.collect()` the wrong fix for fragmentation, and why is restarting the worker
    the right one? *(§13.1)*
27. Write the container budget inequality from §14.1 and name which two terms earlier
    sections control. *(§14.1)*
28. A container is at 95% of `memory.max`. What single field tells you whether to worry? *(§14.2)*
29. Your pod exits 137 with no traceback and no `MemoryError`. Is that consistent with OOM?
    Is `MemoryError` evidence *against* it? *(§14.3)*
30. The free-threaded build costs +23.9% per object. Give the deployment where that is a
    6.8× *win*. *(§15)*
31. `gc.get_objects()` has a blind spot that matters for leak hunting. What is it, and what
    do you do instead? *(§16.1)*
32. Rank §16.3's five production-safe signals by cost, and say which one catches an
    unbounded cache directly. *(§16.3)*

---

## 21. Sources

**Primary — CPython**
- [`tracemalloc` docs](https://docs.python.org/3/library/tracemalloc.html) — read the snapshot/`compare_to` API; §2.4 measures what the docs do not mention.
- [Memory Management, C-API](https://docs.python.org/3/c-api/memory.html) — the three allocation domains and the `PYTHONMALLOC` table quoted in §12.1. **Read the default-allocators table.**
- [`gc` docs — `gc.freeze()`](https://docs.python.org/3/library/gc.html) — prescribes the `disable`/`collect`/`freeze`/`enable` sequence in §11.3 verbatim. Read that paragraph.
- [`mmap` docs](https://docs.python.org/3/library/mmap.html) — `ACCESS_*`, `madvise`, `MAP_PRIVATE` vs `MAP_SHARED`; §10.
- [`multiprocessing.shared_memory` docs](https://docs.python.org/3/library/multiprocessing.shared_memory.html) — §11.3 rung 4, including `unlink()` lifetime.
- [Buffer Protocol, C-API](https://docs.python.org/3/c-api/buffer.html) — the `PyBUF_*` request flags behind §9.1.
- [`resource` docs](https://docs.python.org/3/library/resource.html) + `getrusage(2)` — §2.2's unit trap.
- [`sys` docs](https://docs.python.org/3/library/sys.html) — `getsizeof`, `intern`, `_debugmallocstats`.
- [Command line and environment](https://docs.python.org/3/using/cmdline.html) — `PYTHONMALLOC`, `PYTHONTRACEMALLOC`, `-X` options.

**Primary — PEPs**
- [PEP 574 — Pickle protocol 5 with out-of-band data](https://peps.python.org/pep-0574/) — **read the "Data sharing" and "Rejected alternatives" sections**; they are §9.2.
- [PEP 683 — Immortal Objects](https://peps.python.org/pep-0683/) — its "Avoiding Copy-on-Write" motivation names Instagram and YouTube; §11.1.
- [PEP 703 — Making the GIL Optional](https://peps.python.org/pep-0703/) — the memory-layout changes behind §15's +23.9%.

**Primary — Linux (§12.2, §14 are cited from these, not measured)**
- [cgroup-v2 documentation, Memory controller](https://docs.kernel.org/admin-guide/cgroup-v2.html) — `memory.current`, `.max`, `.high`, `.peak`, `.stat`, `.events`, `.oom.group`. **§14.2 is a summary; read the interface-files section.**
- [`mallopt(3)`](https://man7.org/linux/man-pages/man3/mallopt.3.html) — the dynamic mmap threshold and the disabling interaction quoted in §12.2.
- [`malloc_trim(3)`](https://man7.org/linux/man-pages/man3/malloc_trim.3.html) — "since glibc 2.8 this function frees memory in all arenas"; §12.2.

**Tools**
- [memray](https://bloomberg.github.io/memray/) — the right memory profiler. Read [Python allocators](https://bloomberg.github.io/memray/python_allocators.html) and the [`run` options](https://bloomberg.github.io/memray/run.html) (`--native`, `--follow-fork`, `--trace-python-allocators`, `--aggregate`) before §16.
- [objgraph](https://mg.pov.lt/objgraph/) — `show_growth`, `show_backrefs`, `find_backref_chain`, `get_leaking_objects`; §16.1. The "Memory leak example" page is the fastest introduction to retainer hunting that exists.
- [psutil](https://psutil.readthedocs.io/) — `memory_info()` vs `memory_full_info()` (USS/PSS); §2.1.
- [jemalloc TUNING.md](https://github.com/jemalloc/jemalloc/blob/dev/TUNING.md) and [`jemalloc(3)`](https://jemalloc.net/jemalloc.3.html) — `dirty_decay_ms`, `muzzy_decay_ms`, `background_thread`; §12.3.
- [mimalloc](https://github.com/microsoft/mimalloc) — read the environment-options section: `MIMALLOC_PURGE_DELAY`, `MIMALLOC_PURGE_DECOMMITS`, and its explicit note that `MADV_FREE` "does not decrease rss immediately"; §12.3.
- [Apache Arrow — Memory and IO](https://arrow.apache.org/docs/python/memory.html) — `Buffer`, `py_buffer`, `MemoryMappedFile`; §9.3.

**Sibling docs**
- [`07-virtual-memory.md`](07-virtual-memory.md) — **the prerequisite.** §14 (fragmentation, 9.5×), §7 (COW catastrophe, 295×), §15 (attribution ladder, the 3.26× gap), §10 (RSS/VSZ/PSS/USS), §13 (`madvise`). This document is what you *do* about all of it.
- [`16-object-memory-layout.md`](16-object-memory-layout.md) — §1 (headers), §3 (arenas/pools), §8 (key-sharing dicts, which is §8.1's explanation), §9 (`__slots__`, ~30%), §11 (the correct deep sizer), §13 (cost per million objects).
- [`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md) §10 — "a read in Python is a write in hardware," and the forward reference this document answers.
- [`22-garbage-collection.md`](22-garbage-collection.md) §12 — `gc.freeze()`, `gc.disable()`, leak hunting, and the decision table §3 extends.
- [`31-measurement-methodology.md`](31-measurement-methodology.md) — the protocol in §2.5. Do not report a memory win without it.
- [`32-profiling.md`](32-profiling.md) §7 — memory profiling as a distinct problem; §2.4 is its thesis applied to footprint.
- [`26-free-threading.md`](26-free-threading.md) — the decision framework §15 defers to.
- `33-optimizing-python.md`, `34-going-native.md` — the CPU siblings; §8's arrays and §9's Arrow are where the three documents meet.
- `46-production-python.md` — where §14's budgets become deployment configuration.

---

*Next: `36-type-system-foundations.md` — Tier 5 ends here. You can now find a hot spot
([`32`](32-profiling.md)), trust the finding ([`31`](31-measurement-methodology.md)), and
explain every megabyte a service holds. Tier 6 changes the subject from what the machine
does to what the code means.*
