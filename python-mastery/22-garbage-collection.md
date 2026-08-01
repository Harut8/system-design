# 22 — Garbage collection: the cycle detector, walked line by line

> **Tier 3, doc 22.** Prerequisites: [`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md)
> (owned vs borrowed refs, `Py_DECREF`), [`16-object-memory-layout.md`](16-object-memory-layout.md)
> (`PyGC_Head`, which objects carry one), [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
> §10 (pointer chasing, cache lines). Feeds into: [`26-free-threading.md`](26-free-threading.md),
> [`32-profiling.md`](32-profiling.md), [`35-memory-optimization.md`](35-memory-optimization.md),
> [`46-production-python.md`](46-production-python.md).
>
> **THESIS: CPython does not have "a garbage collector." It has a deallocator
> (reference counting) that handles ~all objects, plus a *cycle detector* that exists
> solely to fix refcounting's one structural blind spot.** Every property people find
> surprising — the generations, the thresholds, `__del__` ordering, resurrection,
> `gc.freeze()`, the stop-the-world pauses on free-threaded builds, and the fact that a
> *shipped* collector was un-shipped in a patch release in May 2026 — falls out of that
> one sentence. And because the cycle detector's core loop is *pointer chasing across the
> entire live heap*, a GC pause costs you the pause **plus** the cold cache afterwards.

> **Measurement provenance.** Every number labelled *(measured)* was produced on the
> machine this repo lives on: **Apple M3 Pro, macOS, arm64, 128-byte cache lines, 16 KB
> pages, 5 P-cores + 6 E-cores**, using **CPython 3.14.6** (`~/.local/bin/python3.14`) and
> the **3.14.6 free-threading build** (`~/.local/bin/python3.14t`, `sys._is_gil_enabled()`
> → `False`). No `perf(1)` on this box, so §11 reaches an **honestly inconclusive**
> result and says so rather than inventing one. C source excerpts are quoted from the
> **`3.14` branch of github.com/python/cpython** as of Aug 2026 and were downloaded and
> read, not recalled. Anything I could not verify is flagged **in place**.

## Contents

1. [Two mechanisms, and why there must be two](#1-two-mechanisms-and-why-there-must-be-two)
2. [Where the code actually lives](#2-where-the-code-actually-lives)
3. [Which objects are GC-tracked](#3-which-objects-are-gc-tracked)
4. [The algorithm, walked on a concrete 4-object graph](#4-the-algorithm-walked-on-a-concrete-4-object-graph)
5. [Generations, thresholds, and the 2000 that used to be 700](#5-generations-thresholds-and-the-2000-that-used-to-be-700)
6. [`tp_traverse` and `tp_clear`: the C extension contract](#6-tp_traverse-and-tp_clear-the-c-extension-contract)
7. [Finalizers: `__del__`, PEP 442, and resurrection](#7-finalizers-__del__-pep-442-and-resurrection)
8. [Weakrefs and callback ordering](#8-weakrefs-and-callback-ordering)
9. [The incremental GC saga](#9-the-incremental-gc-saga)
10. [Free-threaded GC: two stop-the-world pauses](#10-free-threaded-gc-two-stop-the-world-pauses)
11. [GC as a cache-hostility problem](#11-gc-as-a-cache-hostility-problem)
12. [Practical tuning and leak hunting](#12-practical-tuning-and-leak-hunting)
13. [Lab exercises](#13-lab-exercises)
14. [Question bank](#14-question-bank)
15. [Sources](#15-sources)

---

## 1. Two mechanisms, and why there must be two

**Mechanism one: reference counting.** Every `PyObject` starts with `ob_refcnt` at offset
0 ([`16-object-memory-layout.md`](16-object-memory-layout.md) §1). When it reaches zero,
`tp_dealloc` runs *immediately*, on the thread that dropped the last reference. This is
prompt, deterministic, incremental, and it handles the overwhelming majority of objects.
It is also the reason the GIL exists ([`24-the-gil.md`](24-the-gil.md) §1).

**Its one structural failure: cycles.** If A references B and B references A, and nothing
else references either, both counts are 1 forever. Neither will ever hit zero. Refcounting
cannot detect this locally — that is the entire point of a *local* algorithm.

*(measured)* — the whole doc in eight lines:

```console
$ python3.14 lab_cycle.py
refcount a before del: 2
after del, gc disabled:  wa()=Node(a) wb()=Node(b)     ← still alive. leaked.
gc.collect() freed objects: 2
after gc.collect():      wa()=None wb()=None           ← the cycle detector got them
acyclic after del, no gc: wc()=None wd()=None          ← refcounting handled this alone
```

**Mechanism two: the cycle detector**, a mark-and-sweep-flavoured tracing collector that
runs only over *container* objects and only occasionally. Note what it is **not**: it is
not a general tracing GC. It never frees a non-cyclic object — refcounting got there
first. It exists to answer exactly one question: *which of these containers form a group
whose only remaining references are to each other?*

This division has a consequence people miss: **the cycle detector's cost is not
proportional to your garbage. It is proportional to your live heap.** A program with 50
million live objects and zero cycles still pays for full collections that walk all 50
million. That asymmetry drives §5, §9, §11, and §12.

---

## 2. Where the code actually lives

This moved recently, and quoting the old path marks you as reading a stale blog post.
Verified by fetching each path from the CPython git branches *(verified Aug 2026)*:

| Path | 3.12 | 3.13 | 3.14 | What it is |
|---|---|---|---|---|
| `Modules/gcmodule.c` | ✅ (everything) | ✅ | ✅ | **now just the `gc` module wrapper** |
| `Python/gc.c` | ❌ 404 | ✅ | ✅ (2057 lines) | **the collector, GIL builds** |
| `Python/gc_free_threading.c` | ❌ 404 | ✅ | ✅ (3006 lines) | the collector, free-threaded builds |
| `Include/internal/pycore_gc.h` | ✅ | ✅ | ✅ | flags, `gc_refs` accessors, tracking predicates |
| `Include/internal/pycore_interp_structs.h` | — | — | ✅ | `PyGC_Head`, `_gc_runtime_state`, thresholds |
| `InternalDocs/garbage_collector.md` | ✅ | ✅ | ✅ | the design doc — **read it** |

The header of `Modules/gcmodule.c` on 3.14 says it itself:

```c
/*
 * Python interface to the garbage collector.
 *
 * See Python/gc.c for the implementation of the garbage collector.
 */
```

Note the fork at `Python/gc.c` vs `Python/gc_free_threading.c`: **there are two complete,
separately-maintained collectors in the tree**, selected by `Py_GIL_DISABLED`. That is a
real maintenance cost and it is why §9's "just keep both collectors around as an option"
proposal was contentious.

---

## 3. Which objects are GC-tracked

Only types with `Py_TPFLAGS_HAVE_GC` allocate a `PyGC_Head` and can be tracked. On GIL
builds the head is 16 bytes sitting **before** the `PyObject*`; on free-threaded builds it
is gone entirely and the state lives in mimalloc page metadata plus an `ob_gc_bits` byte
([`16-object-memory-layout.md`](16-object-memory-layout.md) §2, §12):

```c
/* Include/internal/pycore_interp_structs.h — GC information is stored BEFORE
   the object structure. */
typedef struct {
    uintptr_t _gc_next;   // Tagged pointer to next object in the list.
                          // 0 means the object is not tracked
    uintptr_t _gc_prev;   // Tagged pointer to previous object in the list.
                          // Lowest two bits are used for flags documented later.
} PyGC_Head;
```

*(measured, 3.14.6)*:

| Expression | `gc.is_tracked` | Why |
|---|---|---|
| `1`, `1.5`, `"x"`, `object()` | False | type lacks `Py_TPFLAGS_HAVE_GC` — cannot reference anything |
| `[]`, `{}`, `set()`, `frozenset([1])` | True | |
| `{1: 2}` | **True** | see the dict note below |
| instance of a plain class | True | |
| a `lambda` | True | closures and `__globals__` |
| `tuple([1, 2])` | **False** | born untracked — see below |
| `tuple(range(2))` | **True**, then False after `gc.collect()` | born tracked, untracked at collection |
| `(1, 2)` as a code constant | True when fresh, False after a collection | same object every `LOAD_CONST` |

### The tuple untracking optimization — and a 3.14 surprise

Two *separate* mechanisms untrack tuples, and conflating them produced the inconsistent
readings above.

**(a) Collection-time untracking.** `untrack_tuples()` runs over the young generation on
every collection and calls `_PyTuple_MaybeUntrack`:

```c
/* Objects/tupleobject.c */
void
_PyTuple_MaybeUntrack(PyObject *op)
{
    if (!PyTuple_CheckExact(op) || !_PyObject_GC_IS_TRACKED(op))
        return;
    for (i = 0; i < n; i++) {
        PyObject *elt = PyTuple_GET_ITEM(t, i);
        if (!elt || _PyObject_GC_MAY_BE_TRACKED(elt))
            return;            /* something in here could still form a cycle */
    }
    _PyObject_GC_UNTRACK(op);  /* permanently off the collector's list */
}
```

A tuple whose contents cannot themselves be tracked can never be part of a cycle, so it is
removed from every future traversal. The `pycore_gc.h` comment is explicit that this is
best-effort: *"It may take more than one cycle to untrack a tuple"* — because the C API
lets you create a tuple and fill it in afterwards.

**(b) Birth-time untracking (3.14).** `_PyTuple_FromArray`, `_PyTuple_FromArraySteal` and
`PyTuple_Pack` never track in the first place if no element's *type* is GC-capable:

```c
    bool track = false;
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *item = src[i];
        if (!track && maybe_tracked(item)) {
            track = true;
        }
        ...
    }
    if (track) {
        _PyObject_GC_TRACK(tuple);
    }
```

That is why `tuple([1,2])` (which goes through `PyList_AsTuple` → `_PyTuple_FromArray`)
reads `False` immediately, while `tuple(range(2))` (built incrementally through
`PyTuple_New`) reads `True` until the next collection. **The observable answer to
"is this tuple tracked?" depends on the construction path.** Do not build a mental model
on `gc.is_tracked` of a literal in a REPL.

### Dicts changed in 3.14 — the stdlib docs are stale

The `gc` module docs still show `gc.is_tracked({})` → `False`. On 3.14.6 *(measured)* it
is **`True`**. Per `InternalDocs/garbage_collector.md`:

> Dictionaries are always tracked from creation and are not untracked by the garbage
> collector. Earlier versions (up to 3.13) used lazy tracking… That machinery was removed
> in 3.14 (GH-127010) because the per-set-item cost of checking the tracking invariant
> outweighed the savings on full collections.

This is a clean example of a GC trade-off flipping sign: the check was cheap *per
collection* and expensive *per `__setitem__`, forever*. Removing it makes every dict
insertion faster and every full collection slightly slower.

---

## 4. The algorithm, walked on a concrete 4-object graph

This is the section everyone hand-waves. Here is the whole thing, on four real objects,
with the real function names, verified against a live interpreter.

### 4.1 Where `gc_refs` lives

There is no separate `gc_refs` field. It is **packed into the top bits of `_gc_prev`**,
whose bottom two bits are flags:

```c
/* Include/internal/pycore_gc.h */
#define _PyGC_PREV_MASK_FINALIZED  ((uintptr_t)1)   /* tp_finalize was called   */
#define _PyGC_PREV_MASK_COLLECTING ((uintptr_t)2)   /* in the generation being GCed */
#define _PyGC_PREV_SHIFT           2

/* Python/gc.c */
static inline Py_ssize_t gc_get_refs(PyGC_Head *g)
    { return (Py_ssize_t)(g->_gc_prev >> _PyGC_PREV_SHIFT); }
static inline void gc_decref(PyGC_Head *g)
    { g->_gc_prev -= 1 << _PyGC_PREV_SHIFT; }
```

So during a collection `_gc_prev` **stops being a pointer** and becomes a counter; the
list is temporarily singly-linked through `_gc_next`, and `move_unreachable` restores the
back-pointers on its way out. That is a 16-byte-per-object saving paid for with a very
delicate invariant. `_gc_next`'s low bit also carries `NEXT_MASK_UNREACHABLE`.

### 4.2 The graph

```python
A = [None]                 # a list, also bound to a local name  → 1 external ref
B = Obj();  A[0] = B; B.ref = A      # A ↔ B  cycle, but A is externally reachable
C = Obj();  D = Obj();  C.ref = D; D.ref = C     # C ↔ D  pure trash cycle
```

*(measured)* — `sys.getrefcount` minus the call's own temporary: **A: 2, B: 1, C: 1, D: 1**,
and `gc.collect()` returns **2**, freeing exactly C and D.

### 4.3 The walk

```
 gen0 list          A(list)          B(Obj)          C(Obj)          D(Obj)
 ────────────────────────────────────────────────────────────────────────────────
 real refs to it    local name       A[0]            D.ref           C.ref
                    + B.ref
 ob_refcnt              2               1               1               1


 STEP 1 — update_refs():  gc_refs := ob_refcnt, set PREV_MASK_COLLECTING on all
 ────────────────────────────────────────────────────────────────────────────────
   gc_refs             [2]             [1]             [1]             [1]
   (immortal objects are UNTRACKED here and skipped entirely — PEP 683)


 STEP 2 — subtract_refs():  for each object, tp_traverse(op, visit_decref)
           visit_decref decrements the referent's gc_refs, but ONLY if the
           referent is in this generation (gc_is_collecting())
 ────────────────────────────────────────────────────────────────────────────────
   A ──traverse──▶ B      B: 1 → 0
   B ──traverse──▶ A      A: 2 → 1
   C ──traverse──▶ D      D: 1 → 0
   D ──traverse──▶ C      C: 1 → 0

   gc_refs             [1]             [0]             [0]             [0]
                        ▲               ▲
                        │               └── "unreachable *so far*" — not proven
                        └── 1 reference from OUTSIDE the set. A is definitely alive.


 STEP 3 — move_unreachable(young, unreachable):  single left-to-right scan
 ────────────────────────────────────────────────────────────────────────────────
   visit A: gc_refs=1 > 0  → REACHABLE. Keep in young, clear COLLECTING,
                             restore _gc_prev, then tp_traverse(A, visit_reachable):
                               sees B with gc_refs == 0  →  gc_set_refs(B, 1)
                                                            ("resurrect" into young)
   visit B: gc_refs=1 > 0  → REACHABLE. traverse → A, but A is no longer
                             COLLECTING, so visit_reachable ignores it.
   visit C: gc_refs=0      → move to `unreachable`, set NEXT_MASK_UNREACHABLE
   visit D: gc_refs=0      → move to `unreachable`

 ────────────────────────────────────────────────────────────────────────────────
   young       = [ A , B ]        ← survivors, promoted to the next generation
   unreachable = [ C , D ]        ← cyclic trash
```

The two subtleties that make this work:

**Why `gc_refs == 0` does not mean "dead".** It means "no references from outside the set
*that we have found yet*". B had `gc_refs == 0` after step 2 and is very much alive. The
proof only completes when the scan finishes — this is a breadth-first closure, and
`visit_reachable` can pull an object *back out* of the unreachable list mid-scan:

```c
    if (gc->_gc_next & NEXT_MASK_UNREACHABLE) {
        /* This had gc_refs = 0 when move_unreachable got to it, but turns
         * out it's reachable after all.  Move it back to move_unreachable's
         * 'young' list, and move_unreachable will eventually get to it again. */
        ...
        gc_list_append(gc, reachable);
        gc_set_refs(gc, 1);
    }
```

**Why it moves the *unreachable* objects rather than the reachable ones**, when most
objects are reachable — the comment in `deduce_unreachable()` is one of the best in the
tree:

> The key is that this dance leaves the objects in order C, B, A — it's reversed from the
> original order. On all _subsequent_ scans, none of them will move. Since most objects
> aren't in cycles, this can save an unbounded number of moves across an unbounded number
> of later collections. It can cost more only the first time the chain is scanned.

An optimization that costs more on the first pass and zero on every pass thereafter. That
is the shape of most real GC engineering.

### 4.4 What happens to `unreachable` after that

`gc_collect_main()` then runs, in order:

1. `untrack_tuples()` — §3.
2. `move_legacy_finalizers()` — objects with a **non-NULL `tp_del`** (pre-PEP-442) are
   pulled out, along with everything reachable *from* them
   (`move_legacy_finalizer_reachable`), and end up in `gc.garbage`. §7.
3. `handle_weakrefs()` — clear weakrefs, queue callbacks. §8.
4. `finalize_garbage()` — call `tp_finalize` (i.e. `__del__`) on each object exactly once.
5. **`handle_resurrected_objects()` — a second `deduce_unreachable()` pass**, because a
   finalizer may have stored a reference somewhere live. §7.
6. `delete_garbage()` — call `tp_clear` on what is still unreachable, breaking the cycles
   so refcounting can finish the job.

Steps 1–3 and 5 are the reason PEP 703 needs **two** stop-the-world pauses, not one (§10).

---

## 5. Generations, thresholds, and the 2000 that used to be 700

The weak generational hypothesis: most objects die young. So CPython keeps three
doubly-linked lists and collects the young one often.

```c
/* Include/internal/pycore_interp_structs.h */
#define NUM_GENERATIONS 3

#define GC_GENERATION_INIT \
    .generations = {       \
        { .threshold = 2000, },   /* gen 0: allocations − deallocations   */ \
        { .threshold = 10,   },   /* gen 1: gen-0 collections since last  */ \
        { .threshold = 10,   },   /* gen 2: gen-1 collections since last  */ \
    },
```

*(measured, 3.14.6, both builds)*:

```console
>>> gc.get_threshold()
(2000, 10, 10)
```

**That first number was 700 for two decades. Verify the history before repeating it —
I did:** `Include/internal/pycore_runtime_init.h` on the **3.12** branch has
`{ .threshold = 700, }`; the **3.13** branch has `{ .threshold = 2000, }`. So the change
landed in **3.13**, not 3.14, and it is a side-effect of the incremental-GC episode (§9):
the threshold was raised to 5000 in 3.13 alpha 5/6 as part of the incremental work, and
when the incremental collector was ripped back out days before 3.13.0, the tuning survived
at 2000. Neil Schemenauer, on the thread, September 2024:

> The non-incremental GC is quite aggressively tuned, with the youngest generation
> threshold at 700. That makes it safe in terms of quickly freeing resources involving
> cyclic garbage but it also means that it often does more work than required. … The
> current value is 700 and that was set many years ago when compute[rs were different]

His measured Sphinx numbers on that thread are the argument in one table (threshold →
time, max RSS): 700 → 2.59 s / 87 MB; 5,600 → 1.78 s / 87 MB; 70,000 → 1.70 s / 93 MB;
700,000 → 1.71 s / **122 MB**. Almost all the speed is available before RSS starts moving.
The counter-anecdote in the same thread is the one to remember: Itamar Turner-Trauring
reported that applying Meta's well-tested `(14_000, 100, 100)` to *all* Python workloads
"caused at least a dozen services to start crashing with OOMs."

*(measured)* — watching promotion happen, allocating 2000 tracked objects per step and
keeping them all alive:

```
  allocs                  count   gen0   gen1   gen2      ← cumulative collections
    2000             (12, 1, 0)      3      0      3
    6000             (11, 3, 0)      5      0      3
   20000             (4, 10, 0)     12      0      3
   40000           (1995, 7, 1)     20      1      3      ← gen1 count hit 10 → gen1 collect
   80000           (1975, 3, 3)     38      3      3
```

Gen 2 never runs, despite 38 gen-0 collections and 3 gen-1 collections. That is the
**long-lived-pending heuristic**, and it is the single most important tuning fact in the
collector:

```c
/* Python/gc.c — gc_select_generation() */
    if (i == NUM_GENERATIONS - 1
        && gcstate->long_lived_pending < gcstate->long_lived_total / 4)
    {
        continue;     /* skip the full collection */
    }
```

A full collection only runs if at least **25%** of the long-lived population is
"pending" — has survived non-full collections but never been through a full one. The
rationale, from the source, is Martin von Löwis's 2008 analysis:

> …the cost of a full collection is proportional to the total number of long-lived
> objects, which is virtually unbounded. … "each full garbage collection is more and more
> costly as the number of objects grows, but we do fewer and fewer of them."

Without it, building a large list of tracked objects is **quadratic**. With it, amortized
linear. Remember this shape — §9 is what happens when the equivalent heuristic in a new
collector gets the arithmetic wrong.

---

## 6. `tp_traverse` and `tp_clear`: the C extension contract

The collector cannot see into your C struct. It calls two slots, and if you implement
them wrong you get either leaks or crashes — nothing in between.

**`tp_traverse(self, visit, arg)`** must call `Py_VISIT(field)` on **every** `PyObject*`
the object owns a strong reference to. That is the entire contract, and both directions of
violating it are bad:

- **Miss a field** → the collector under-counts internal references, so
  `subtract_refs` leaves `gc_refs > 0` on a genuinely dead object, and the cycle is never
  collected. A silent, permanent leak that `tracemalloc` will happily attribute to your
  extension's allocation site with no hint of why.
- **Visit a field you don't own** (a borrowed reference) → the collector over-subtracts,
  `gc_decref` can drive `gc_refs` below zero, and CPython will free a live object. In a
  debug build you get `_PyObject_ASSERT_WITH_MSG(op, gc_refs > 0, "refcount is too
  small")` — which you can see in `visit_reachable` and `gc_decref` in the excerpts above.
  In a release build you get a use-after-free at a random later point.

**`tp_clear(self)`** must drop strong references (`Py_CLEAR`) so the cycle breaks. It is
called from `delete_garbage()`:

```c
    inquiry clear;
    if ((clear = Py_TYPE(op)->tp_clear) != NULL) {
        Py_INCREF(op);
        (void) clear(op);
        ...
        Py_DECREF(op);
    }
    if (GC_NEXT(collectable) == gc) {
        /* object is still alive, move it, it may die later */
        gc_clear_collecting(gc);
        gc_list_move(gc, old);
    }
```

Note the `Py_INCREF`/`Py_DECREF` bracket: `tp_clear` runs on a *live* object and may
trigger arbitrary deallocation, including of the object itself, so the collector keeps a
reference across the call and then checks whether the object survived.

Three rules that follow:

1. **`tp_traverse` must be pure.** It runs mid-collection with `_gc_prev` holding a
   counter instead of a pointer. Allocating, calling back into Python, or raising will
   corrupt or crash the collector.
2. **A type with `Py_TPFLAGS_HAVE_GC` must implement `tp_traverse`.** `subtract_refs`
   calls it unconditionally — there is no NULL check.
3. **Heap types must visit `Py_TYPE(self)`.** Since 3.9 a heap type is a strong reference
   from the instance, and instance→type→module→instance is a real cycle. Forgetting this
   is the most common leak in `pybind11`/hand-written extension modules.

---

## 7. Finalizers: `__del__`, PEP 442, and resurrection

### Before PEP 442 (Python ≤ 3.3): cycles with `__del__` were uncollectable

The old slot was `tp_del`. The collector could not order finalizer calls within a cycle,
and calling `tp_del` on an object whose cycle-mates had already been `tp_clear`ed would
hand user code a half-destroyed object. So it refused: any cycle containing an object with
`tp_del` went to `gc.garbage` and leaked, permanently. "Never write `__del__`" was
correct advice for a decade.

### PEP 442 (Python 3.4): `tp_finalize`, and a bit in the GC header

PEP 442 split finalization from deallocation. `__del__` now maps to **`tp_finalize`**, and
the collector runs finalizers *before* breaking anything:

```c
/* Python/gc.c — finalize_garbage() */
        if (!_PyGC_FINALIZED(op) &&
            (finalize = Py_TYPE(op)->tp_finalize) != NULL)
        {
            _PyGC_SET_FINALIZED(op);
            Py_INCREF(op);
            finalize(op);
            assert(!_PyErr_Occurred(tstate));
            Py_DECREF(op);
        }
```

`_PyGC_SET_FINALIZED` sets `_PyGC_PREV_MASK_FINALIZED` — bit 0 of `_gc_prev` (§4.1), or
`_PyGC_BITS_FINALIZED` in `ob_gc_bits` on free-threaded builds. **The object is finalized
at most once, ever.** From the PEP:

> On the internal side, a bit is reserved in the GC header for GC-managed objects to
> signal that they were finalized. This helps avoid finalizing an object twice (and,
> especially, finalizing a CT object after it was broken by the GC).

Every object in the trash gets `tp_finalize` called *before* any of them gets `tp_clear`,
so every finalizer sees an intact graph. Ordering *among* the finalizers is still
unspecified — and it has to be; there is no defensible order in a cycle.

*(measured, 3.14.6)*:

```console
--- PEP 442: a cycle whose members define __del__ ---
  before collect: gc.garbage = []
   __del__ ran for a
   __del__ ran for b
  collect() returned 2  gc.garbage = []
```

`gc.garbage` is empty. That would have been two permanently leaked objects on Python 3.3.
`tp_del` still exists for compatibility, but per the PEP *"a non-NULL `tp_del` is not
encountered anymore in the CPython source tree (except for testing purposes)."* In
practice `gc.garbage` non-empty today means a third-party C extension.

### Resurrection, and its exact rules

A finalizer receives a live object and may store it somewhere reachable. That is
**resurrection**. *(measured — note the return value)*:

```console
collect#1 -> 0 | __del__ calls: ['a', 'b']
saved: <__main__.Lazarus object at 0x103894590> | gc.is_finalized(saved): True
saved.other is still alive?: <__main__.Lazarus object at 0x103874690>
collect#2 -> 2 | __del__ calls now: ['a', 'b']  <- 'a' NOT repeated
```

Read that first line: **`gc.collect()` returned 0, not 2.** The cycle was proven
unreachable, the finalizers ran, one of them stashed `self` in a global — and the second
`deduce_unreachable()` pass in `handle_resurrected_objects()` found *both* objects
reachable again and merged them into the old generation. Nothing was freed. That second
pass is not defensive programming; it is load-bearing.

The rules, precisely:

1. **Resurrecting one member of a cycle resurrects the whole cycle.** `saved.other` is
   alive above — the collector cannot un-break a graph selectively.
2. **`tp_finalize` will never be called again on that object.** `gc.is_finalized(saved)`
   is `True` and stays true. Drop the reference, collect again: freed silently, no second
   `__del__`.
3. **Resurrected objects are moved to the oldest generation** (`gc_list_merge(resurrected,
   old_generation)`), so they will not be re-examined for a long time.
4. **A `__del__` on a resurrected object cannot be relied upon to release a resource.** It
   fires once, at a time you do not control, possibly during interpreter shutdown when
   module globals are already `None`.

### Why `weakref.finalize` is usually correct

```python
import weakref
class Conn:
    def __init__(self, sock):
        self.sock = sock
        self._fin = weakref.finalize(self, sock.close)   # not a __del__
```

`weakref.finalize` beats `__del__` on every axis that matters:

- **The callback does not hold a strong reference to the object**, so registering it does
  not keep the object alive and does not make the object's type harder to collect.
- **It cannot resurrect** — the callback gets whatever arguments you bound, not `self`.
- **It runs exactly once**, and you can query `.alive` and force it with `.detach()` /
  calling the finalizer object.
- **It is guaranteed to run at interpreter exit by default** (`atexit=True`), which
  `__del__` is not.
- *(measured)*: on a two-object cycle, `weakref.finalize` fired and `fin.alive` became
  `False`. It works fine on cyclic garbage.

The remaining legitimate uses of `__del__` are: a last-resort "you forgot to `close()`"
`ResourceWarning`, and C-level types where you're implementing `tp_finalize` anyway.
Everything else should be a context manager first, `weakref.finalize` second.

---

## 8. Weakrefs and callback ordering

Weakrefs interact with the collector in a way that has one rule worth memorizing.

```c
/* Python/gc.c — handle_weakrefs()
 * Note that we cannot invoke any callbacks until all weakrefs to unreachable
 * objects are cleared, lest the callback resurrect an unreachable object via a
 * still-active weakref. */
```

So the order is: **clear every weakref to the trash first; only then invoke callbacks.**
A callback that calls `wr()` gets `None`, always. This closes a resurrection hole that
`tp_finalize`'s once-only bit does not cover.

**The rule: if a weakref is itself part of the trash cycle, its callback is not called.**
*(measured)*:

```console
weakref inside the trash cycle  -> callbacks fired: NONE
weakref outside the trash cycle -> callbacks fired: ['OUTSIDE callback']
```

The reasoning is the same as for finalizer ordering: the callback is about to be
destroyed too, and there is no meaningful order in which to run callbacks that are
themselves garbage. The comment in `Python/gc.c` puts it as *"it's possible for such
weakrefs to be outside the unreachable set — indeed, those are precisely the weakrefs
whose callbacks must be invoked."*

**Production consequence:** an observer/cache built on `WeakValueDictionary` whose
callback does cleanup will silently skip that cleanup for any entry that ends up inside a
cycle with the dictionary. If the cleanup is important (releasing an fd, decrementing a
counter), that's a slow leak that only appears under the exact object shapes that create
the cycle. `WeakValueDictionary` and `WeakSet` handle their own internal case correctly;
your callback on top of them is what breaks.

---

## 9. The incremental GC saga

This is the best worked example in the whole roadmap of why GC design is hard, and it is
the reason README §15 says version facts rot. **A feature shipped in `.0` and was
un-shipped in `.5` of the same release series.** Timeline, verified against primary
sources (confidence notes at the end of this section):

| When | What | Source |
|---|---|---|
| 3.13 alphas (2024) | Mark Shannon's incremental collector merged. Two generations (young/old); each collection does a *fraction* of the old space. Young threshold raised 700 → 5000, later 2000. | CPython git history; `pycore_runtime_init.h` diff 3.12 → 3.13 |
| Sept 2024 | Alex Waygood et al. trace a large Sphinx slowdown to it ([gh-124567](https://github.com/python/cpython/issues/124567)). | discuss.python.org t/65285 |
| **28 Sept 2024** | Release manager **Thomas Wouters**: *"I don't think we should release 3.13.0 with the incremental GC."* Rolls it back, cuts rc3 on 30 Sept, and **pushes 3.13.0 final back a week to 7 Oct 2024.** | [discuss t/65285](https://discuss.python.org/t/incremental-gc-and-pushing-back-the-3-13-0-release/65285) |
| **7 Oct 2025** | 3.14.0 ships **with** the incremental collector. `get_threshold()` now returns `(2000, 10, 0)`: value 1 is the young threshold, value 2 is the *old-space scan rate*, value 3 is meaningless. | [whatsnew 3.14](https://docs.python.org/3/whatsnew/3.14.html) |
| 10 Dec 2025 | [gh-142516](https://github.com/python/cpython/issues/142516) — "Observed memory leak in ssl library: Python 3.14 GC issue". Reporter's chain: MSAL → `requests` → `urllib3` → `ssl.SSLContext.load_verify_locations`. Memray traces attached. | GitHub |
| 20 Apr 2026 | Adam Johnson publishes a Django reproduction: `migrate` on a Heroku dyno with a low memory cap; workaround is forcing `gc.collect()` after each migration. | [adamj.eu](https://adamj.eu/tech/2026/04/20/django-python-3.14-incremental-gc/) |
| **16 Apr 2026** | Hugo van Kemenade announces the revert **in both 3.14 and 3.15**, back to the 3.13 generational collector. | [discuss t/107014](https://discuss.python.org/t/reverting-the-incremental-gc-in-python-3-14-and-3-15/107014) |
| 23 Apr 2026 | Tim Peters posts the minimal reproduction and the mechanism (below). | [discuss t/107067](https://discuss.python.org/t/improving-incremental-gc/107067) |
| **10 May 2026** | **3.14.5 ships the revert.** | [blog.python.org](https://blog.python.org/2026/05/python-3145-is-out/) |
| Aug 2026 | 3.14.6 and 3.15 are on the generational collector. Reintroduction for **3.16 is under discussion, via the PEP process**, most likely **opt-in with the old collector as default**. | discuss t/107014, t/107067 |

### The mechanism of the failure

Tim Peters' toy: an infinite loop that creates one cycle per iteration and, after
iteration 1000, converts one to trash per iteration. Never more than 1000 *reachable*
cycles. Under 3.13's generational collector, a gen-0 collection fires around iteration
2000, reclaims 1000 trash cycles, promotes 1000, and repeats smoothly at 4000, 6000, 8000.
Under 3.14's incremental collector:

> Nothing is collected at 2000 iterations. And still not by 4000 iterations. Or 6000,
> 8000, … Nothing at all gets collected until about the 20 thousandth iteration. gc is
> invoked along the way … but it returns without collecting anything until iteration
> 20_000. Then it collects about 18_000 trash cycles. … It eventually (after about 750K
> iterations) reaches a "steadyish state", always with over 90K trash cycles awaiting
> collection, but not more than 100K.
>
> …I don't understand the current "work to do" logic, and especially not how "the math"
> can end up making it **negative(!)** at times. But intuition says "work to do" should
> always include gen0.

Neil Schemenauer's independent finding, on the same threads: *"process memory use can be
dramatically higher (5x was the worst case I saw) and runtime is slower"* — while
confirming the incremental collector genuinely **did** deliver smaller maximum pauses.

His proposed fix is one sentence: *"we trigger GC every 2000 net new objects, like the
generational GC. We size the increments (how many old objects to look at) such that we
effectively do a full collection often enough."* His prototype kept max RSS and trash
count low **while preserving the short pauses**. The core team and the Steering Council
still chose the full revert, because — Hugo's words — *"the old GC is a known quantity,
the new incremental GC didn't go through the PEP process."*

### The five transferable lessons

1. **"Fewer objects scanned per pause" is not "less memory."** The incremental collector
   optimized the pause-time metric perfectly and let the *backlog* metric run free. If
   your GC change has one number attached to it, you have not evaluated it.
2. **A latency win that is a throughput-and-memory loss is a trade, not an improvement**,
   and the default must be chosen for the workload you can't see.
3. **CPython has no benchmark for this.** Neil, April 2026: *"The pyperformance suite
   contains basically no interesting benchmarks in terms of exercising the cyclic GC in a
   realistic way."* The failure was found by a synthetic toy and by production users, in
   that order — not by CI.
4. **Reverts have blast radius too.** Tim Peters: users who spent real effort tuning
   `gc.set_threshold()` *for* the incremental collector had that effort silently
   invalidated by 3.14.5.
5. **The fossils are in the struct.** `_gc_runtime_state` on the 3.14 branch today:

```c
    /* dummy members to preserve other offsets */
    Py_ssize_t dummy1; /* was work_to_do */
    int dummy2; /* was visited_space */
    int dummy3; /* was phase */
```

Three named holes, kept so the struct offsets don't move in a patch release. That is what
"we reverted a GC in a patch release" looks like at the byte level.

**Confidence.** High on everything in the table with a linked source: the dates, the
3.14.0–3.14.4 window, 3.14.5 on 2026-05-10, the double revert (3.13 pre-release and
3.14.5/3.15), and the 3.16-via-PEP intent are all directly quoted from python.org
properties. **One correction to the version of this story I was given:** I could **not**
find any HTTPX-specific report. The production reproductions I can verify are
**urllib3/`ssl`** (gh-142516) and **Django `migrate`** (Adam Johnson). If an HTTPX report
exists, it is not in the revert thread, the improving-incremental-gc thread, or
gh-142516.

---

## 10. Free-threaded GC: two stop-the-world pauses

Under the GIL, the collector gets stable refcounts for free — nothing else runs. Remove
the GIL and that guarantee is gone: `gc_refs` arithmetic is meaningless if another thread
is mutating references mid-scan. PEP 703's answer:

> The current CPython cyclic garbage collector involves two cycle-detection passes during
> each garbage collection cycle. Consequently, this requires **two stop-the-world pauses**
> when running the garbage collector without the GIL. The first cycle-detection pass
> identifies cyclic trash. The second pass runs after finalizers to identify which objects
> still remain unreachable. Note that **other threads are resumed before finalizers and
> `tp_clear` functions are called** to avoid introducing potential deadlocks that are not
> present in the current CPython behavior.

Map that onto §4.4: pause 1 wraps `deduce_unreachable()`, pause 2 wraps
`handle_resurrected_objects()`'s second `deduce_unreachable()`. Between and after them,
threads run — because a finalizer or `tp_clear` can execute arbitrary Python, and running
arbitrary Python with every other thread frozen is a deadlock generator (finalizer takes
lock L; another thread holds L and is suspended by the STW; done).

There are also free-threading-specific pieces in `Python/gc_free_threading.c` visible in
`_gc_runtime_state`:

```c
#ifdef Py_GIL_DISABLED
    int freeze_active;          /* True if gc.freeze() has been used. */
    Py_ssize_t last_mem;        /* Memory usage of the process (RSS + swap) after last GC. */
    Py_ssize_t deferred_count;  /* accumulates when collection is deferred due to
                                   the RSS increase condition not being met */
    PyMutex mutex;
#endif
```

**The free-threaded build triggers collections partly on measured RSS growth, not purely
on object counts.** That is exactly the improvement Antoine Pitrou argued for on the
incremental-GC threads, already shipped on one build and not the other. Also note
`ob_gc_bits`, replacing the `_gc_prev` flag bits: `_PyGC_BITS_TRACKED`,
`_PyGC_BITS_FINALIZED`, `_PyGC_BITS_UNREACHABLE`, `_PyGC_BITS_FROZEN`, `_PyGC_BITS_SHARED`,
`_PyGC_BITS_ALIVE`, `_PyGC_BITS_DEFERRED`.

*(measured)* — `gc.collect()` wall time over a 2M-object live graph, with four spinning
Python threads running:

| Build | `gc.collect()` wall time, 5 runs (ms) |
|---|---|
| 3.14.6 GIL | 72.0, 60.9, 79.5, 72.3, 67.4 |
| 3.14.6t free-threaded | **22.0, 31.4, 31.3, 31.7, 31.1** |

**Do not read this as "free-threaded GC is 2× faster."** Read it as: on the GIL build the
collecting thread is *competing with four bytecode-executing threads for the one GIL*, so
its wall-clock time includes waiting; on the free-threaded build it stops them and runs
alone. The GIL build's number is contention; the free-threaded number is the true
stop-the-world pause — during which **all four other threads made zero progress**. Same
work, different accounting. Measuring the *application-visible* pause on both builds
(instrument the spinners, not the collector) is Lab 6.

---

## 11. GC as a cache-hostility problem

Cross-reference [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
§10.4 before reading this section, and then look at what `subtract_refs` actually does:
for every tracked object in the generation, dereference `Py_TYPE(op)->tp_traverse`, call
it, and have it dereference every `PyObject*` field to reach `AS_GC(op)` — which is 16
bytes *before* the object, i.e. a different cache line in the general case.

**That is a pointer-chase over the entire live tracked heap, with essentially no
spatial locality, executed at least twice per collection.** The prefetcher cannot help;
the access pattern is data-dependent. It is the single most cache-hostile loop the
interpreter runs.

*(measured)* — same object count, same graph shape, only the *order* in which objects are
linked differs (sequential allocation order vs. a shuffled permutation):

| Live tracked objects | `gc.collect()`, sequential links | shuffled links | ratio |
|---|---|---|---|
| 10,000 | 0.60 ms | 0.62 ms | 1.03× |
| 100,000 | 3.24 ms | 3.37 ms | 1.04× |
| 1,000,000 | 28.32 ms | 94.01 ms | **3.32×** |
| 4,000,000 | 115.11 ms | 420.46 ms | **3.65×** |

Below ~100k objects the whole graph fits in cache and layout is free. Above ~1M it is a
**3.65× difference in GC pause from nothing but memory layout**. The collector's work is
identical — same objects, same edges, same `tp_traverse` calls. This is the clearest
demonstration in this folder that Python performance is a memory-layout problem wearing an
interpreter costume.

It also explains a class of production mystery: *"our GC pauses got 3× worse and we didn't
change anything."* You changed allocation order. Loading data in a different sequence,
adding a shuffle, switching from batch to streaming ingestion — any of these re-arranges
the heap without changing a single object count.

### The honest part: I could not measure the post-GC cold-cache penalty

The claim I wanted to prove is that a GC pause costs the pause **plus** a degraded period
afterwards, because the collector evicted the application's working set. I tried twice:

- **Attempt 1** — 3M-object cold heap for the collector to walk, a ~1 MB hot working set
  for the "application" to chase. `gc.collect()` pause: 72.9 ms. Steady-state loop:
  3279 µs. First loop after the GC: 3304 µs — **+1%**, inside the noise.
- **Attempt 2** — 4M-object cold heap, hot set enlarged to ~14 MB (bigger than L2, sized
  to live in the SLC), randomized traversal order, 5 trials × 4 sweeps. Mean first
  post-GC sweep 2.62 ms vs 2.55 ms warm: **+2.7%**, with individual post-GC sweeps
  ranging 2.48–2.82 ms — i.e. some *faster* than the warm baseline.

**Verdict: inconclusive, and I am not going to dress it up.** The most likely explanation
is instrument failure rather than a false hypothesis: each iteration of a Python pointer
chase costs ~10 ns of interpreter overhead (`LOAD_ATTR` + specialization check + refcount
traffic), which is the same order as the L2/SLC miss I'm trying to detect. The signal is
inside the interpreter's own noise floor. Settling it needs PMU counters
(`L1D_CACHE_REFILL`, `LLC_MISSES`) attributed to a window immediately after
`gc.callbacks` fires `'stop'` — and this machine has no `perf(1)`. On Apple silicon the
path would be Instruments' CPU Counters template or `kperf`; I have not done it.

What I *can* say with the measurements in hand: **the pause itself (73–420 ms) is so much
larger than any plausible cold-cache tail that the tail is not where you should spend
your attention.** Fix the pause. See [`31-measurement-methodology.md`](31-measurement-methodology.md)
for why "my experiment showed nothing" is a result and not a failure.

---

## 12. Practical tuning and leak hunting

### 12.1 `gc.freeze()` before `fork()` — the Instagram technique

This is the highest-leverage GC intervention that exists for pre-forking servers, and it
is worth understanding *why* rather than cargo-culting the three-line recipe.

A forked child shares the parent's pages copy-on-write. Nothing is copied until something
writes. Then the child runs a garbage collection — and `update_refs()` writes `gc_refs`
into the `_gc_prev` field of **every tracked object in the generation**. Those writes are
16 bytes apart across the entire heap, so they dirty essentially every page holding a
tracked object. The child has now privately copied the parent's whole object graph without
allocating anything.

`gc.freeze()` moves every currently-tracked object into a **permanent generation** that is
never scanned:

```
   BEFORE fork, no freeze                 BEFORE fork, gc.freeze()
   ┌────────────────────────┐             ┌────────────────────────┐
   │ gen0 │ gen1 │ gen2     │             │ permanent generation   │
   │  ●●●●●●●●●●●●●●●●●●●●  │             │  ●●●●●●●●●●●●●●●●●●●●  │
   └────────────────────────┘             └────────────────────────┘
            │ fork()                               │ fork()
            ▼                                      ▼
   child gc.collect():                    child gc.collect():
     update_refs() writes _gc_prev          permanent gen is never visited
     on every object                        → no writes → pages stay shared
     → COW faults on ~every page
```

*(measured)* — 1.5M `__slots__` records (≈3.0M tracked objects), 4 children, each running
`gc.collect()` five times, reporting its own RSS growth:

```
mode=nofreeze  parent RSS:    397.5 MiB  frozen=       0  still-tracked= 3005339
          per-child RSS growth from gc.collect(): [381.9, 381.9, 381.8, 381.9] MiB
          total = 1527.5 MiB

mode=freeze    parent RSS:    397.4 MiB  frozen= 3005339  still-tracked=       0
          per-child RSS growth from gc.collect(): [0.6, 0.7, 0.7, 0.7] MiB
          total = 2.7 MiB
```

**1,527 MiB of copy-on-write un-sharing, reduced to 2.7 MiB, by one function call.** Each
child privately copied 96% of the parent's 397 MiB heap purely to write GC bookkeeping
into it. With 4 workers that is 1.5 GB of RAM your container is paying for and your
dashboard attributes to "the app".

The full recipe, from the `gc` docs:

```python
gc.disable()          # early in the parent: avoid creating freed "holes" in pages
...load everything...
gc.freeze()           # immediately before fork()
os.fork()             # or: let gunicorn/uvicorn --preload do it
gc.enable()           # early in each child
```

`gc.unfreeze()` puts them back in the oldest generation; `gc.get_freeze_count()` tells you
it worked. Frozen objects are still freed by refcounting — freezing only removes them from
*cycle detection*.

### 12.2 `gc.disable()` — when it is actually safe

`gc.disable()` stops *automatic* collection. `gc.collect()` still works. It is safe when:

- The process is short-lived and its peak RSS fits comfortably (build scripts, CLI tools,
  Lambda-style handlers). mypy does this.
- You genuinely create no cycles — rare, and easy to be wrong about: any exception
  traceback holds a frame that holds the exception, generators reference their frames,
  and every heap type is a cycle with its module.
- You control collection explicitly at a safe point — e.g. between requests, or Adam
  Johnson's Django workaround of `gc.collect()` after each migration.

It is **not** safe as a general latency fix on a long-running service: you are trading a
bounded pause for unbounded memory. The measured consequence, if you're wrong, is an OOM
kill, which is a much worse p100 than a 400 ms pause.

Tuning without disabling: `gc.set_threshold(20000, 10, 10)` is the conservative knob (~10×
less frequent gen-0 work). *(measured)* — there is **no** `PYTHON_GC_THRESHOLD`
environment variable and no `-X gc_threshold` on 3.14.6; both were proposed on the 2024
thread and I could not find them in this build. `gc.set_threshold(0, ...)` disables gen-0
collection while leaving `gc.isenabled()` `True`, which is a good way to confuse your
future self.

### 12.3 Leak hunting

```python
gc.set_debug(gc.DEBUG_COLLECTABLE | gc.DEBUG_STATS)
```

*(measured)*, on a three-object cycle:

```
gc: collecting generation 2...
gc: objects in each generation: 11 0 5293
gc: objects in permanent generation: 0
gc: collectable <N 0x105cb0590>
gc: collectable <N 0x105c90590>
gc: collectable <N 0x105c90690>
gc: done, 3 unreachable, 0 uncollectable, 0.0003s elapsed
```

The flags, in the order you'll want them:

| Flag | Use |
|---|---|
| `DEBUG_STATS` | per-collection generation sizes and elapsed time — the cheapest GC observability there is |
| `DEBUG_COLLECTABLE` | print each cyclic object found |
| `DEBUG_UNCOLLECTABLE` | print objects that went to `gc.garbage` (→ a C extension with `tp_del`) |
| `DEBUG_SAVEALL` | **put everything unreachable into `gc.garbage` instead of freeing it**, so you can inspect it |
| `DEBUG_LEAK` | `COLLECTABLE \| UNCOLLECTABLE \| SAVEALL` |

The workflow that actually finds things:

```python
gc.set_debug(gc.DEBUG_SAVEALL)
gc.collect()
for obj in gc.garbage:
    print(type(obj), [type(r).__name__ for r in gc.get_referrers(obj)])
```

*(measured)* — for a self-referential instance this reports referrers
`['dict', 'list', 'Leaky']`: its own `__dict__`, the `gc.garbage` list itself, and the
instance. **`gc.get_referrers` includes the frame you called it from and the container you
put results in** — always subtract the observer. It is also slow (it traverses everything)
and returns *containers*, not attribute names; for anything beyond a handful of objects
use [`objgraph`](https://mg.pov.lt/objgraph/) or **memray** instead
([`32-profiling.md`](32-profiling.md)).

For continuous observability, `gc.callbacks` is better than polling *(measured — a
two-object cycle, `'start'`→`'stop'` delta of 265 µs)*:

```python
gc.callbacks.append(lambda phase, info: metrics.emit(phase, info))
# ('start', {'generation': 2, 'collected': 0, 'uncollectable': 0})
# ('stop',  {'generation': 2, 'collected': 2, 'uncollectable': 0})
```

Emit a histogram of stop−start per generation. A p99 GC pause metric costs you almost
nothing and is the difference between diagnosing §11 in an hour and in a quarter.

### 12.4 The decision table

| Symptom | First hypothesis | Instrument |
|---|---|---|
| RSS climbs forever, `gc.collect()` fixes it | cycles + a threshold too high, or a disabled GC | `gc.get_count()`, `DEBUG_STATS` |
| RSS climbs forever, `gc.collect()` does **not** fix it | not a GC problem: unbounded cache, fragmentation, or a C-extension leak | memray, [`16`](16-object-memory-layout.md) §5 |
| Periodic multi-hundred-ms latency spikes | full collections over a large live heap | `gc.callbacks` histogram, then §11/§12.1 |
| Worker RSS = N × parent RSS after fork | COW un-sharing from `update_refs` | §12.1, `gc.freeze()` |
| `gc.garbage` non-empty | a C extension with a legacy `tp_del` | `DEBUG_UNCOLLECTABLE` |
| `__del__` not running | a cycle, or resurrection, or interpreter shutdown | §7; switch to `weakref.finalize` |

---

## 13. Lab exercises

Reading this leaves you at **rung 3** of the ladder in [README §14](README.md#14-the-competence-ladder) —
fluent, and one "why?" from collapse. These move you to rung 4. All use
`~/.local/bin/python3.14` and `python3.14t`.

**1 — Prove refcounting cannot free a cycle.** *(mandatory)* Build a two-object cycle,
hold weakrefs, `gc.disable()`, `del` the names, show both objects alive. Then
`gc.collect()` and show them gone. Repeat with an *acyclic* pair and show refcounting
handles it with no collector involvement. *Proves the two-mechanism split in §1 — and it
is the answer to the most common interview question on this topic.*

**2 — Walk §4 on your own graph.** Build the A/B/C/D graph, print `sys.getrefcount` for
each, hand-compute `gc_refs` after `update_refs` and after `subtract_refs`, predict what
`gc.collect()` returns, then run it. Now change B to be reachable from a second external
name and re-predict. *Proves you can run the algorithm, not just describe it. This is the
rung-4/rung-5 boundary for this doc.*

**3 — `gc.freeze()` before fork.** *(mandatory)* Load ≥1M tracked objects, fork 4
children, have each child `gc.collect()` and report its own RSS delta through a pipe. Run
with and without `gc.freeze()`. Predict the delta before you run it. Then try
`gc.freeze()` *without* `gc.disable()` during the load and explain why the docs recommend
both. *Proves §12.1 and is directly applicable to any gunicorn/uvicorn deployment.*

**4 — Make GC pauses 3× worse with no code change.** Reproduce §11: same object count,
same edge count, sequential vs. shuffled link order. Find the object count at which the
curves separate on your machine and relate it to your L2/SLC size from
[`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md). *Proves that GC
cost is a memory-layout property.*

**5 — Break `tp_traverse` deliberately.** Write a small C extension (or use Cython with
`cdef class`) with a `Py_TPFLAGS_HAVE_GC` type. Version A omits one field from
`tp_traverse`; version B visits a borrowed reference it doesn't own. Build both against a
`--with-pydebug` interpreter. Version A should leak; version B should trip
`"refcount is too small"`. *Proves §6, and it is the single best preparation for reviewing
extension code.*

**6 — Measure the real free-threaded pause.** Take §10's benchmark but instrument the
*spinner* threads: have each record a timestamp every N iterations and report its maximum
inter-timestamp gap. Run on `python3.14` and `python3.14t`. The GIL build should show
gaps from GIL contention; the free-threaded build should show a clean STW plateau. *Proves
you understand why §10's table does not mean what it looks like it means.*

**7 — Resurrect an object and count the collections.** Reproduce §7: a cycle whose
`__del__` stashes `self`. Show `gc.collect()` returns 0, show `gc.is_finalized` is True,
show the cycle-mate survived too, then drop the reference and show it is freed with no
second `__del__`. Rewrite the whole thing with `weakref.finalize` and show it cannot
happen. *Proves §7's four rules.*

**8 — Reproduce the incremental-GC failure shape.** You cannot run 3.14.0–3.14.4 here, so
do it structurally: on 3.14.6, write a loop that creates one cycle per iteration and
trashes one per iteration after warmup. Instrument with `gc.callbacks` and count trash
cycles awaiting collection. Now `gc.set_threshold(200000, 10, 10)` to *simulate* a
collector that defers work, and plot RSS. *Proves §9's lesson — "fewer objects scanned"
≠ "less memory" — with your own numbers rather than Tim Peters'.*

---

## 14. Question bank

Staff-level. Section references are where to reread if your model can't produce the answer.

1. CPython has reference counting. Why does it *also* need a cycle detector, and why is that detector's cost proportional to your **live** heap rather than your garbage? *(§1)*
2. Where is `gc_refs` stored, and what happens to `_gc_prev`'s normal job during a collection? *(§4.1)*
3. Walk `update_refs` → `subtract_refs` → `move_unreachable` on a 4-object graph where one member of a cycle is externally referenced. Which objects have `gc_refs == 0` after step 2, and why is that not the answer? *(§4.3)*
4. Why does `move_unreachable` move the *unreachable* objects, when most objects are reachable? *(§4.3)*
5. Two Python processes disagree on `gc.is_tracked((1, 2))`. Give two distinct mechanisms that could explain it. *(§3)*
6. `gc.get_threshold()` returns `(2000, 10, 10)`. What does each number count, when did the first one change from 700, and why? *(§5)*
7. You allocate 10 million long-lived tracked objects. Why doesn't the collector go quadratic, and what is the exact hardwired constant that prevents it? *(§5)*
8. Your C extension's type leaks under some workloads and segfaults under others. Give the two `tp_traverse` bugs that produce each, and which build catches them. *(§6)*
9. Before Python 3.4, a cycle containing an object with `__del__` leaked forever. What exactly did PEP 442 change, and where is the "already finalized" state stored? *(§7)*
10. `gc.collect()` returns 0 on a cycle you know is unreachable, and `__del__` definitely ran. Explain. *(§7)*
11. Your `WeakValueDictionary` callback that closes file descriptors fires for most entries and silently skips some. What shape of object graph causes that? *(§8)*
12. The incremental GC shipped in 3.14.0 and was removed in 3.14.5. State the exact failure mechanism, not just "memory grew." *(§9)*
13. Free-threaded builds have two stop-the-world pauses per collection. Why two, and why are threads *resumed* before finalizers run? *(§10)*
14. Your GC pauses tripled after a release that changed only data-loading order. Explain, and name the measurement that would confirm it. *(§11)*
15. A pre-forking server with 8 workers uses 8× the parent's RSS within minutes. Diagnose it, fix it, and predict the size of the fix before you measure. *(§12.1)*
16. When is `gc.disable()` safe in production, and what is the failure mode when your safety argument is wrong? *(§12.2)*

---

## 15. Sources

**Primary — read these, not this document**
- [`InternalDocs/garbage_collector.md`](https://github.com/python/cpython/blob/3.14/InternalDocs/garbage_collector.md) — the official design doc, ~37 KB, in the tree. **Verdict: the single best source on this topic; read it end to end before anything else.** Note it moved here from `Doc/` and from the devguide (`devguide.python.org/internals/garbage-collector/` is now a 404 — *verified Aug 2026*).
- [`Python/gc.c`](https://github.com/python/cpython/blob/3.14/Python/gc.c) — the collector for GIL builds. **Verdict: essential and surprisingly readable.** Start at `gc_collect_main()` ("This is the main function. Read this to understand how the collection process works"), then `deduce_unreachable()`, then `move_unreachable()`. The comments are load-bearing.
- [`Python/gc_free_threading.c`](https://github.com/python/cpython/blob/3.14/Python/gc_free_threading.c) — the free-threaded collector. **Verdict: read only after `gc.c`; it is a separate implementation, not a variant.**
- [`Include/internal/pycore_gc.h`](https://github.com/python/cpython/blob/3.14/Include/internal/pycore_gc.h) and [`pycore_interp_structs.h`](https://github.com/python/cpython/blob/3.14/Include/internal/pycore_interp_structs.h) — `PyGC_Head`, the `_gc_prev` flag bits, `ob_gc_bits`, `_gc_runtime_state`, and the threshold initializers. **Verdict: the authority for every constant in §3–§5.**
- [`gc` — Garbage Collector interface](https://docs.python.org/3/library/gc.html) — **Verdict: authoritative on API, but the `gc.is_tracked({})` example is stale as of 3.14 (§3). Trust your interpreter over the docs.**
- [PEP 442 — Safe object finalization](https://peps.python.org/pep-0442/) — **Verdict: short, and it is the whole of §7. Read the "C-level changes" section.**
- [PEP 703 §Garbage Collection](https://peps.python.org/pep-0703/) — **Verdict: the two-STW-pause design and the `gc_refs`/deferred-refcounting interaction, straight from Sam Gross. Read alongside [`24-the-gil.md`](24-the-gil.md) §8.5.**
- [PEP 683 — Immortal Objects](https://peps.python.org/pep-0683/) — why `update_refs` untracks immortals outright.

**The incremental GC saga (§9)**
- [Reverting the incremental GC in Python 3.14 and 3.15](https://discuss.python.org/t/reverting-the-incremental-gc-in-python-3-14-and-3-15/107014) — Hugo van Kemenade, 16 Apr 2026, 20 posts. **Verdict: the decision, the rationale, and the 3.16 plan. Primary source; read all of it.**
- [Improving incremental gc](https://discuss.python.org/t/improving-incremental-gc/107067) — Tim Peters, 23 Apr 2026. **Verdict: the best technical post-mortem. The toy reproduction and the "the math can go negative(!)" observation are here.**
- [Incremental GC and pushing back the 3.13.0 release](https://discuss.python.org/t/incremental-gc-and-pushing-back-the-3-13-0-release/65285) — Thomas Wouters, 28 Sep 2024. **Verdict: the *first* revert, plus Neil Schemenauer's threshold table and Itamar Turner-Trauring's "a dozen services OOMed" anecdote. This is where the 2000 came from.**
- [gh-142516 — memory leak in ssl library: Python 3.14 GC issue](https://github.com/python/cpython/issues/142516) — **Verdict: the production report the revert announcement links to (MSAL → requests → urllib3 → ssl). Opened 2025-12-10.**
- [Django: fixing a memory "leak" from Python 3.14's incremental GC](https://adamj.eu/tech/2026/04/20/django-python-3.14-incremental-gc/) — Adam Johnson. **Verdict: the best real-world write-up, with a working mitigation.**
- [Python 3.14.5 rolls back the incremental garbage collector](https://pydevtools.com/blog/python-3145-rolls-back-the-incremental-garbage-collector/) — Tim Hopper. **Verdict: accurate secondary summary; good for orientation, then go to the discuss threads.**
- [What's New in Python 3.14 — Garbage collection](https://docs.python.org/3/whatsnew/3.14.html#garbage-collection) — **Verdict: the canonical statement, and it documents *both* states (3.14.0–3.14.4 and 3.14.5+). Quote this one in a design doc.**

**Background**
- [The Garbage Collection Handbook, 2e](https://gchandbook.org/) (Jones, Hosking & Moss) — ch. 5 (reference counting) and ch. 9 (generational). **Verdict: reference, not a read-through; but read ch. 5's treatment of cycles once and §1 of this doc becomes obvious.**
- [`gc_weakref.txt`](https://github.com/python/cpython/blob/3.14/Modules/gc_weakref.txt) — the design note `handle_weakrefs()` points at for §8.

**Sibling docs**
- [`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md) — mechanism one.
- [`16-object-memory-layout.md`](16-object-memory-layout.md) §1–2 — `PyGC_Head`, and why it vanishes on free-threaded builds. *(Its §1 table says `(1, 2)` is untracked; §3 here refines that — it depends on the construction path and whether a collection has run.)*
- [`24-the-gil.md`](24-the-gil.md) §8.5 — the stop-the-world design, from the concurrency side.
- [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §10.4 — the pointer-chasing cost model §11 measures.
- [`35-memory-optimization.md`](35-memory-optimization.md) — applying §12 to a real service.

---

*Next: [`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md) — `gc.callbacks`
generalized: PEP 669 monitoring, audit hooks, and watching a running interpreter without
paying for it.*
