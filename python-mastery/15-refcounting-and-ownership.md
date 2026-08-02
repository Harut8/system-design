# 15 — Reference counting and ownership

> **Tier 2, doc 15.** Prerequisites: [`14-pyobject-and-types.md`](14-pyobject-and-types.md),
> [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §5 (MESI — you
> need to know what a write to shared memory costs). Feeds into:
> [`16-object-memory-layout.md`](16-object-memory-layout.md),
> [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md),
> [`22-garbage-collection.md`](22-garbage-collection.md),
> [`24-the-gil.md`](24-the-gil.md), [`26-free-threading.md`](26-free-threading.md).
>
> **THESIS: reference counting is not a detail of CPython's memory management — it is the
> decision from which the GIL, the C-API's shape, the cycle collector, `fork()`'s memory
> behaviour, and the entire free-threading project all follow.** Every other language
> runtime chose a tracing collector and got cheap mutation plus stop-the-world pauses.
> CPython chose refcounting and got deterministic destruction plus a write on every read.
> This document is about the second half of that trade, because it is the half that
> explains almost everything else in this folder.

> **Verification note.** The standard-build struct definitions here are quoted from
> `Include/object.h`. The free-threaded layout in §9 was read from **this machine's
> `python3.14t` headers** *(verified)*; note that it differs from PEP 703's *proposed*
> layout, which is what most secondary sources reproduce — including an earlier draft of
> [`16-object-memory-layout.md`](16-object-memory-layout.md) §2, which was wrong until
> [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md) corrected it. Check your own
> build's header before quoting field names.

## Contents

1. [Why refcounting at all](#1-why-refcounting-at-all)
2. [The mechanics](#2-the-mechanics)
3. [Three kinds of reference](#3-three-kinds-of-reference)
4. [The ownership rules, by API family](#4-the-ownership-rules-by-api-family)
5. [The five classic bugs](#5-the-five-classic-bugs)
6. [`Py_CLEAR` and reentrancy](#6-py_clear-and-reentrancy)
7. [`sys.getrefcount` and why it always lies by one](#7-sysgetrefcount-and-why-it-always-lies-by-one)
8. [What refcounting cannot do](#8-what-refcounting-cannot-do)
9. [Immortal, deferred, and biased](#9-immortal-deferred-and-biased)
10. [Refcounting versus `fork()`](#10-refcounting-versus-fork)
11. [The cost model](#11-the-cost-model)
12. [Debugging refcount bugs](#12-debugging-refcount-bugs)
13. [Lab exercises](#13-lab-exercises)
14. [Question bank](#14-question-bank)
15. [Sources](#15-sources)

---

## 1. Why refcounting at all

It is easy to read this document as a catalogue of refcounting's costs and conclude
CPython made a mistake. It didn't — it made a *trade*, in 1990, and got real things for it:

| Property | Refcounting | Tracing GC |
|---|---|---|
| Reclamation timing | **immediate, deterministic** | whenever the collector runs |
| Pause behaviour | **no stop-the-world** | STW pauses (or complex concurrent GC) |
| `__del__` / RAII patterns | **work predictably** | unpredictable, often discouraged |
| Implementation complexity | **low** — a counter and two macros | high |
| C extension authoring | simple rules, no rooting | needs precise rooting / handles |
| Memory ceiling | tight — freed at once | needs headroom |
| Cost of *reading* an object | **a write** ✗ | free ✓ |
| Cycles | **cannot collect** ✗ | handled naturally ✓ |
| Multicore scaling | **catastrophic** ✗ | good ✓ |

The top half of that table is why `with open(...)` works the way you expect, why a large
list is freed the instant it goes out of scope, and why writing a C extension for CPython
is dramatically easier than writing one for a JVM. Those are not small wins.

The bottom half is the rest of this folder.

> **The one sentence to keep.** Refcounting trades *cheap reads* for *predictable
> deaths*. Every language runtime makes this trade; CPython is unusual in having made it
> in the direction that becomes expensive exactly when you add cores.

---

## 2. The mechanics

Every `PyObject` begins with its refcount. From `Include/object.h` (standard build):

```c
typedef struct _object {
    Py_ssize_t ob_refcnt;      /* offset 0 */
    PyTypeObject *ob_type;
} PyObject;
```

The rules are two macros:

- **`Py_INCREF(op)`** — "I am now keeping a reference to `op`."
- **`Py_DECREF(op)`** — "I am done with `op`." If the count reaches zero, call
  `op->ob_type->tp_dealloc(op)` immediately.

Deallocation is **recursive and synchronous**. Dropping the last reference to a list
decrefs every element, which may deallocate them, which decrefs *their* contents. Freeing
one object can free a million, on the spot, in the thread that dropped the last reference.

```
    del big_tree
        │
        ▼
    Py_DECREF(root)  →  0  →  tp_dealloc(root)
                                  │
                                  ├─▶ Py_DECREF(child1) → 0 → tp_dealloc → ...
                                  ├─▶ Py_DECREF(child2) → 0 → tp_dealloc → ...
                                  └─▶ free(root)
                              ▲
                              └── all of this happens inside your `del` statement,
                                  on your thread, with the GIL held.
```

**Two consequences people are surprised by:**

1. **A `del` can take arbitrarily long.** Dropping the last reference to a 10 GB object
   graph is a multi-second, unbudgeted pause on the calling thread. CPython has no
   pauses "because it has no GC" — it has pauses distributed into your assignment
   statements, which is worse for reasoning about latency, not better.
2. **Deep structures can blow the C stack.** A million-deep linked list can segfault the
   interpreter on deallocation. CPython has trashcan machinery
   (`Py_TRASHCAN_BEGIN`/`END`) precisely to bound this recursion for container types.

---

## 3. Three kinds of reference

This vocabulary is the whole of C-API correctness. Every function you call returns, or
takes, exactly one of these.

**New (strong) reference.** The function incremented the count for you. **You own it and
must `Py_DECREF` it** when done.

```c
PyObject *n = PyLong_FromLong(42);   /* new ref — yours */
...
Py_DECREF(n);                        /* required */
```

**Borrowed reference.** You get a pointer with *no* count increment. Valid only as long as
the true owner keeps it alive. **You must not decref it**, and you must not keep it past
the owner's lifetime.

```c
PyObject *item = PyList_GetItem(list, 0);   /* borrowed — the list owns it */
/* if `list` is mutated or freed here, `item` dangles */
```

**Stolen reference.** You pass a reference *in*, and the callee takes over ownership. You
must not decref it afterwards. Rare and deliberately so — it exists for performance in
container construction.

```c
PyObject *v = PyLong_FromLong(7);   /* new ref */
PyTuple_SetItem(tup, 0, v);         /* STEALS v — do NOT decref v */
```

```
   NEW          you own it       ──▶ you must DECREF
   BORROWED     someone else's   ──▶ you must NOT DECREF, and must not outlive owner
   STOLEN       you gave it away ──▶ you must NOT DECREF
```

There is no type-system help for any of this. The compiler sees three identical
`PyObject *`. **The documentation is the only specification**, which is why §4 exists and
why every C-API function's docs state its behaviour explicitly.

---

## 4. The ownership rules, by API family

The patterns are learnable; the exceptions are what bite.

| Family | Returns | Notes |
|---|---|---|
| `Py*_New`, `Py*_From*` | **new** | `PyLong_FromLong`, `PyUnicode_FromString`, … |
| `PyObject_Call*` | **new** | all call results are new refs |
| `PyObject_GetAttr`, `PyObject_GetItem` | **new** | the generic protocols are safe |
| `PyDict_GetItem` | **borrowed** | ⚠️ the classic footgun |
| `PyList_GetItem`, `PyTuple_GetItem` | **borrowed** | ⚠️ fast but unsafe |
| `PySequence_GetItem` | **new** | the safe sibling of `PyList_GetItem` |
| `PyList_SetItem`, `PyTuple_SetItem` | — | ⚠️ **steal** their value argument |
| `PyList_Append`, `PyDict_SetItem` | — | do **not** steal — they incref |
| `PyImport_ImportModule` | **new** | |
| `PyModule_GetDict` | **borrowed** | |
| `Py_BuildValue` | **new** | |

**Note the inconsistency in the middle of that table.** `PyList_SetItem` steals but
`PyList_Append` does not. `PyDict_SetItem` increfs but `PyTuple_SetItem` steals. There is
no principle here you can derive — these are historical decisions, and you look them up
every time. Anyone who tells you they have memorized the C-API's ownership rules is
telling you they have memorized the ones they use.

**The modern fix.** CPython has been adding strong-reference-returning replacements for
the borrowed-reference APIs, because borrowed references are far more dangerous under
free-threading (§9, and [`26-free-threading.md`](26-free-threading.md)). Verified present
in this build's headers *(verified)*:

| Old (borrowed) | New (strong) |
|---|---|
| `PyDict_GetItem` | **`PyDict_GetItemRef`** |
| `PyDict_GetItemString` | **`PyDict_GetItemStringRef`** |
| `PyList_GetItem` | **`PyList_GetItemRef`** |
| `PyObject_GetAttr` (soft-fail) | **`PyObject_GetOptionalAttr`** |
| `PyDict_SetDefault` | **`PyDict_SetDefaultRef`** |

These return `int` (1 found / 0 not found / −1 error) and write the result through an
out-parameter, so "missing key" stops being conflated with "error". **Use them in new
code.** They are the single easiest C-API modernization available.

---

## 5. The five classic bugs

**1 — Borrowed reference outliving its owner.** The archetype:

```c
PyObject *item = PyList_GetItem(list, 0);   /* borrowed */
PyList_SetItem(list, 0, other);             /* list drops its ref → item may be freed */
PyObject_Print(item, stdout, 0);            /* use-after-free */
```

The fix is `Py_INCREF(item)` immediately, or `PySequence_GetItem`/`PyList_GetItemRef`.
**This bug is invisible in testing** whenever the object happens to be referenced
elsewhere — which, for small ints and interned strings, is always (§9).

**2 — Leak on the error path.** The most common leak in real extensions:

```c
PyObject *a = PyLong_FromLong(1);
PyObject *b = PyLong_FromLong(2);
if (!b) return NULL;                /* ← leaks `a` */
```

Every early return must release everything acquired so far. C has no destructors; the
conventional discipline is a single `goto error:` cleanup block.

**3 — Decref'ing a borrowed reference.** Over-decref is *worse* than a leak: it frees a
live object and corrupts memory that other code still uses. The crash lands somewhere
unrelated — see §12.

**4 — Decref during iteration.** Mutating a container while holding borrowed references
into it. Same shape as bug 1, harder to see.

**5 — Forgetting that `tp_dealloc` can run arbitrary Python code.** A `__del__`, a weakref
callback, or a buffer release can re-enter the interpreter and mutate the very structure
you are in the middle of tearing down. This is what §6 is about.

---

## 6. `Py_CLEAR` and reentrancy

Here is a bug that looks impossible:

```c
Py_DECREF(self->attr);      /* may run __del__, which may read self->attr */
self->attr = NULL;
```

`Py_DECREF` can drop the count to zero, invoke `tp_dealloc`, and that can call a Python
`__del__` — which can read `self->attr`, **which still points at the object being
destroyed.** The window between the decref and the `NULL` assignment is a reentrancy hole.

`Py_CLEAR` exists solely to close it, by doing the operations in the correct order:

```c
/* conceptually: */
#define Py_CLEAR(op)                    \
    do {                                \
        PyObject *_tmp = (PyObject *)(op); \
        if (_tmp != NULL) {             \
            (op) = NULL;                /* 1. unpublish FIRST */ \
            Py_DECREF(_tmp);            /* 2. then release */    \
        }                               \
    } while (0)
```

**Always use `Py_CLEAR` when clearing a struct member.** Never the naive two-liner. This is
mandatory in `tp_clear` implementations, where the cycle collector is actively breaking
references and reentrancy is guaranteed rather than hypothetical — see
[`22-garbage-collection.md`](22-garbage-collection.md).

The general principle generalizes far beyond C: **make the object unreachable before you
destroy it**, because destruction can run code that goes looking for it.

---

## 7. `sys.getrefcount` and why it always lies by one

```python
>>> x = object()
>>> sys.getrefcount(x)
2          # not 1
```

Passing `x` to `getrefcount` binds it to the function's parameter — a reference that
exists *during* the call. So the answer is always one higher than "the count outside this
call". Subtract one, or use `sys.getrefcount(x) - 1` and remember why.

Worse, the number is often meaningless for small values:

```python
>>> sys.getrefcount(1)
1000000000+          # immortal — see §9
>>> sys.getrefcount("hello")
# large and unstable — interned, shared across the interpreter
```

**`getrefcount` is a debugging aid for objects you created, never a correctness tool.**
Any code branching on a refcount value is broken, because the value depends on
interpreter internals you don't control. The one legitimate use is leak hunting: watch
whether a count *grows* across iterations, ignoring its absolute value.

---

## 8. What refcounting cannot do

Cycles. That's the whole gap, and it's structural:

```python
a = {}
b = {}
a['b'] = b        # b.refcount = 2
b['a'] = a        # a.refcount = 2
del a, b          # each drops to 1 — neither reaches 0
                  # unreachable, but immortal to the refcounter
```

Both objects are unreachable from any root and neither will ever be freed by refcounting
alone. Hence CPython's **second** memory manager: the cycle collector, which exists purely
to clean up after this one limitation. That is
[`22-garbage-collection.md`](22-garbage-collection.md)'s entire subject.

Note the architectural cost: CPython carries **two** memory management systems, and every
container type must implement `tp_traverse` and `tp_clear` to cooperate with the second
one. A tracing collector would have needed neither. That is part of refcounting's price,
paid by every C extension author forever.

Cycles are not exotic. They appear in doubly-linked lists, parent/child trees, any
`self.callback = self.method` binding, exception tracebacks (which reference the frame
that references the exception), and most graph structures.

---

## 9. Immortal, deferred, and biased

Three refinements, each attacking a different pathology. All three are really *coherence*
optimizations dressed as refcounting optimizations — see
[`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §5–6.

### Immortal objects (PEP 683, Python 3.12+)

Some objects provably live for the whole process: `None`, `True`, `False`, small ints
(−5…256), interned strings, static type objects. Marking them **immortal** makes
`Py_INCREF`/`Py_DECREF` no-ops.

Why it matters: `None`'s refcount was the single hottest contended word in a
multi-threaded CPython process. Every thread touching `None` wrote to the same cache line,
ping-ponging it between cores. Immortalization lets that line sit in **Shared** MESI state
on every core forever, never invalidated. See [`24-the-gil.md`](24-the-gil.md) §8.1.

It also fixes §10's fork problem for the most-touched objects.

### Deferred reference counting

For objects read constantly from many threads but rarely destroyed — top-level functions,
modules, heap types — even per-thread schemes escape to the slow path, because the sharing
is genuine. Deferred refcounting skips interpreter-stack refcount updates for these and
reconciles during garbage collection.

### Biased reference counting (free-threaded builds)

Borrowed from Swift. Split the count: an **owner** thread updates a local count with plain
non-atomic instructions; other threads use atomics on a shared count. The overwhelmingly
common case — an object created and used by one thread — costs exactly what it did under
the GIL.

> **The actual struct differs from PEP 703's proposal.** Read from this machine's
> `python3.14t/Include/object.h` *(verified)*, `struct _object` opens with a union:
>
> ```c
> union {
>     PY_INT64_T ob_refcnt_full;   /* for efficient init with Clang on ARM */
>     struct {
>         uint32_t ob_refcnt;
>         uint16_t ob_overflow;
>         uint16_t ob_flags;
>     };
> };
> ```
>
> Additional fields including `ob_tid`, `ob_ref_local`, `ob_ref_shared`, `ob_mutex` and
> `ob_gc_bits` follow — [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md) §2 has
> the full verified layout. **Most secondary sources reproduce PEP 703's proposal instead
> of the shipped struct.** Read the header.

---

## 10. Refcounting versus `fork()`

`fork()` gives the child a copy-on-write view of the parent's memory. Nothing is copied
until written. In principle a pre-forking server should share almost all its memory with
its workers.

**Refcounting destroys this**, because reading an object writes to it:

```
   Parent loads a 2 GB model into memory, then forks 8 workers.
   Expected:  2 GB total   (all shared, COW)
   Reality:   ~16 GB       (every worker touched every refcount)

   ┌──────────┐  fork   ┌──────────┐
   │ page: obj│ ──────▶ │ shared   │   worker merely READS the object
   │ refcnt=1 │         │ (COW)    │            │
   └──────────┘         └──────────┘            ▼
                                          Py_INCREF → write → page copied
                                          4 KB (or 16 KB here) copied
                                          to hold a +1 on one counter.
```

**The mitigations, in order of effectiveness:**

1. **Keep bulk data out of Python objects.** A NumPy array or `mmap` buffer has *one*
   refcount for the whole thing, so COW works as intended. Ten million Python objects have
   ten million counters spread across every page. **This is the mitigation that addresses
   the dominant cost**, and the measurement below is why it is ranked first.
2. **`gc.freeze()` before forking.** Moves all currently-tracked objects into a permanent
   generation the collector won't touch, so **GC traversal** doesn't dirty pages. The
   standard incantation:
   ```python
   # in the parent, after loading everything, before forking
   gc.collect()
   gc.freeze()
   ```
   This is the Instagram technique. Know precisely what it does and does not do:

   > **Measured** (600,000 dicts, forked child, child's own RSS growth, reproduced twice
   > — see [`07-virtual-memory.md`](07-virtual-memory.md) §7):
   >
   > | child does | no freeze | `gc.freeze()` | benefit |
   > |---|---|---|---|
   > | only `gc.collect()` | 200.8 MB | 0.8 MB | **~245×** |
   > | only **reads** the graph | 198.7 MB | 198.8 MB | **1.0× — none** |
   >
   > `gc.freeze()` eliminates the *collector's* writes. It does **nothing** about
   > `Py_INCREF`/`Py_DECREF` on ordinary reads, which is the write traffic that actually
   > privatises your heap. A worker that merely walks a large object graph pays the full
   > COW cost with or without it.

   So it is a genuine win — the 245× is real and worth having — but it is not, as an
   earlier draft of this document claimed, "the single highest-value line of code in a
   pre-forking Python server." It closes one of two write sources, and usually not the
   larger one.
3. **Immortal objects (§9)** removed a large slice of this problem in 3.12 for free —
   `None`, small ints, and interned strings no longer take refcount writes at all
   ([`30-concurrency-correctness.md`](30-concurrency-correctness.md) §16.4 classifies that
   path).

Point 3 is the real lesson: the fork problem is not really about `fork`, it is about
**per-object metadata density**. See
[`35-memory-optimization.md`](35-memory-optimization.md).

---

## 11. The cost model

**The refcount is the most frequently written word in a CPython process.** Not one of the
most — *the* most. Every name load, argument pass, return, and iteration step touches one.

Three costs, which staff-level answers keep separate:

| Cost | Where it bites |
|---|---|
| **Instructions** | 2 extra memory ops per reference. Small, and well-predicted. |
| **Cache** | The counter shares a line with the object header, so every read dirties the line and forces a writeback. |
| **Coherence** | The killer on multicore: a shared object's counter ping-pongs between cores at ~40–300 cycles a hop. |

The third is the one that matters, and it is why:

- The GIL existed at all — under it, refcount updates need no atomics
  ([`24-the-gil.md`](24-the-gil.md) §2).
- Larry Hastings' first Gilectomy attempt — just making refcounts atomic — cost **~30%**
  and got *worse* with more cores ([`24-the-gil.md`](24-the-gil.md) §7).
- Free-threading needed **five** separate mechanisms rather than one
  ([`24-the-gil.md`](24-the-gil.md) §8).
- Free-threaded builds still carry a measured **+8.1%** single-thread tax on this machine
  ([`26-free-threading.md`](26-free-threading.md) §3).

> **The through-line of this entire folder.** `ob_refcnt` sits at offset 0 of every
> object. That one layout decision, made when machines had one core, is why Python's
> concurrency story looks the way it does thirty-five years later. Architecture is
> the choices that are expensive to reverse — and this is the most expensive one CPython
> ever made.

---

## 12. Debugging refcount bugs

Refcount bugs are the hardest class of Python bug, because **the symptom is arbitrarily
far from the cause**. An over-decref frees an object that some unrelated code is still
using; the crash happens later, elsewhere, in code that is entirely correct.

The toolkit, roughly in order:

| Tool | Finds |
|---|---|
| **`PYTHONMALLOC=debug`** | domain mismatches, buffer over/underruns, use-after-free of freed pattern bytes. **First reach, always.** |
| **Debug build** (`--with-pydebug`) | assertions, `sys.gettotalrefcount()` for leak detection |
| `sys.gettotalrefcount()` | total refs across the interpreter — watch it across iterations |
| ASan / UBSan | use-after-free with a real allocation/free stack |
| `gc.get_referrers(obj)` | *who is keeping this alive* — the leak question |
| `objgraph` | reference-chain visualization for Python-level leaks |
| `sys.getrefcount` deltas | growth across a loop (never the absolute value — §7) |

**The leak-hunting loop** for a suspected Python-level leak:

```python
import gc
gc.collect()
before = len(gc.get_objects())
run_the_suspect_operation()
gc.collect()
after = len(gc.get_objects())
print(after - before)          # should be ~0 across repeated runs
```

If it grows linearly with iterations, you have a leak; `gc.get_referrers` on a sample of
the leaked type tells you who is holding it. Nine times in ten the answer is a module-level
cache, a logger holding a traceback, or an `lru_cache` on a method
([`42-runtime-code-manipulation.md`](42-runtime-code-manipulation.md) §2).

---

## 13. Lab exercises

Reading this leaves you at rung 3 (README §14). **These are all light — no benchmarking
required.**

**1 — Watch a cascade.** Build a nested structure a few million objects deep-ish, then time
a single `del`. Confirm that one statement takes measurable time. *Proves §2 — CPython's
pauses hide in your assignments.*

**2 — Prove the cycle gap.** Build the two-dict cycle from §8. Use `gc.disable()`, delete
both names, and confirm via `gc.get_objects()` that they survive. Enable GC, collect, watch
them go. *Proves §8, and is the on-ramp to doc 22.*

**3 — `getrefcount` off-by-one.** Confirm a fresh object reports 2. Then check `1`,
`True`, and `"hello"` and explain each. *Proves §7 and §9.*

**4 — Find the immortals.** Write a loop over `range(-10, 300)` printing `sys.getrefcount`.
Identify exactly where the small-int cache ends. Then do the same for strings and work out
which are interned. *Proves §9.*

**5 — Measure the fork tax.** Load a large list of Python objects, `fork`, have the child
merely *iterate* it without mutating, and compare child RSS with and without `gc.freeze()`
in the parent. *Proves §10 — the highest-value lab here for anyone running a pre-forking
server.* (Use a modest object count; this does not need to be a big run.)

**6 — Read the two headers side by side.** Open `Include/object.h` from both
`python3.14` and `python3.14t` and diff `struct _object`. Write down every field the
free-threaded build adds, and say what each is for. *Proves §9 — and inoculates you
against secondary sources reproducing PEP 703's proposal instead of the shipped struct.*

**7 — Trigger `PYTHONMALLOC=debug`.** Write (or borrow from
[`17-c-api-and-extensions.md`](17-c-api-and-extensions.md)) a small extension with a
deliberate domain mismatch. Run it with and without the env var. *Proves §12 — and the
difference between a mystery segfault and a one-line diagnosis.*

---

## 14. Question bank

1. What does refcounting buy that a tracing collector doesn't? Name three things. *(§1)*
2. Why can a `del` statement take several seconds? *(§2)*
3. Distinguish new, borrowed, and stolen references. Which does `PyList_GetItem` return? *(§3, §4)*
4. `PyList_SetItem` steals; `PyList_Append` doesn't. What principle explains this? *(§4 — trick question)*
5. Why is `PyDict_GetItemRef` preferable to `PyDict_GetItem` in new code? *(§4, §9)*
6. Write the borrowed-reference use-after-free in three lines, then fix it two ways. *(§5)*
7. Why does `Py_CLEAR` assign `NULL` before decref'ing, and what breaks if you swap them? *(§6)*
8. Why does `sys.getrefcount(x)` return 2 for a fresh object? *(§7)*
9. Exactly which objects can refcounting never free, and what handles them instead? *(§8)*
10. Why is immortalizing `None` better described as a coherence fix than a refcount fix? *(§9, §11)*
11. Your pre-forking server uses 8× the memory you predicted. Explain, and give three fixes ranked. *(§10)*
12. Name the three separate costs of a refcount update. Which dominates on multicore, and why? *(§11)*
13. Why did making refcounts atomic cost ~30% *and get worse with more cores*? *(§11, [`24`](24-the-gil.md) §7)*
14. A segfault occurs in `list_dealloc` inside `sum()`, and your extension is nowhere on the stack. What is your first hypothesis and first tool? *(§5, §12)*
15. Argue that `ob_refcnt`'s position at offset 0 explains Python's concurrency history. *(§11)*

---

## 15. Sources

**Primary — verify against these**
- [`Include/object.h`](https://github.com/python/cpython/blob/main/Include/object.h) — the struct and the macros. **Read it in both builds** (lab 6); it is short and it is the ground truth.
- [C-API: Reference Counting](https://docs.python.org/3/c-api/refcounting.html) and [Objects, Types and Reference Counts](https://docs.python.org/3/c-api/intro.html#objects-types-and-reference-counts) — **read the second one properly**; it is the actual specification for §3–§4 and most C extension bugs are violations of it.
- [PEP 683 — Immortal Objects](https://peps.python.org/pep-0683/) — read the motivation section for §9 and §10.
- [PEP 703](https://peps.python.org/pep-0703/) §Reference Counting — biased and deferred refcounting. **Note it describes the proposal; check the header for what shipped.**
- [`gc` module docs](https://docs.python.org/3/library/gc.html) — `freeze()` for §10.

**Background**
- [The Garbage Collection Handbook, 2e](https://gchandbook.org/) (Jones, Hosking & Moss, 2023) — ch. 5 is the definitive treatment of reference counting, including deferred and buffered variants. Reference; read ch. 5 if §9 interested you.
- Instagram engineering's write-ups on `gc.freeze` and COW — the origin of §10's technique.

**Sibling docs**
- [`24-the-gil.md`](24-the-gil.md) §1, §7, §8 — the consequences of everything here.
- [`16-object-memory-layout.md`](16-object-memory-layout.md) — where the counter physically sits.
- [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md) — §3–§6 in practice, with a compilable module and the verified free-threaded struct.
- [`22-garbage-collection.md`](22-garbage-collection.md) — what §8 hands off to.
- [`26-free-threading.md`](26-free-threading.md) — §9's biased refcounting, measured.

---

*Next: [`16-object-memory-layout.md`](16-object-memory-layout.md) — where this counter
lives, what it shares a cache line with, and what a million of them actually cost.*
