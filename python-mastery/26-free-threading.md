# 26 — Free-threading in practice: the migration, the taxes, and the sharing wall

> **Tier 4, doc 26.** Prerequisites: [`24-the-gil.md`](24-the-gil.md) (read §8 first — it
> covers PEP 703's five mechanisms and this document does **not** re-derive them),
> [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §5–6 (MESI, true
> vs false sharing), [`16-object-memory-layout.md`](16-object-memory-layout.md) §2 (the
> +16-byte header finding this document develops),
> [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md) (borrowed vs strong refs).
> Feeds into: [`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md),
> [`31-measurement-methodology.md`](31-measurement-methodology.md),
> [`22-garbage-collection.md`](22-garbage-collection.md) §10, `44-packaging-and-environments.md`.
>
> Doc 24 is the *physics* document: why the GIL exists and how PEP 703 removes it. This is
> the **practitioner's** document: what actually happens when you type `python3.14t` and
> point it at your service.
>
> **THESIS: free-threading does not make your code parallel — it makes the memory system
> your scheduler.** You pay an unconditional single-threaded tax and an unconditional
> per-object memory tax whether or not you ever start a second thread; in exchange you get
> scaling that is bounded not by a lock but by *how much of your object graph your threads
> share*. Two threads on disjoint graphs scale linearly. Two threads on one shared dict
> scale **negatively** — measured below at 0.51× of single-threaded throughput, i.e. worse
> than not threading at all. The migration decision is therefore not "is my code
> thread-safe?" but "**what is the shape of my object graph, and what do my C extensions
> do?**"

> **Measurement provenance.** Every number marked *(measured)* was produced during the
> writing of this document on the machine this repo lives on:
>
> - **Apple M3 Pro**, macOS 26.5.2, arm64, **11 logical cores: 5 performance + 6 efficiency**
>   (`hw.perflevel0.logicalcpu` = 5, `hw.perflevel1.logicalcpu` = 6), 128-byte cache line,
>   18 GB RAM.
> - **GIL build:** `~/.local/bin/python3.14` → `Python 3.14.6 (main, Jun 11 2026)`,
>   `sys._is_gil_enabled()` → `True`, `Py_GIL_DISABLED` = 0, `sys.abiflags` = `''`.
> - **Free-threaded build:** `~/.local/bin/python3.14t` → `Python 3.14.6 free-threading
>   build`, `sys._is_gil_enabled()` → `False`, `Py_GIL_DISABLED` = 1, `sys.abiflags` = `t`,
>   `EXT_SUFFIX` = `.cpython-314t-darwin.so`.
> - **The two builds are unusually well matched**, which matters more than most people
>   realise. Both are `python-build-standalone` distributions of the *same* 3.14.6 source,
>   both built with `--enable-optimizations` (PGO), `--with-lto`, `--with-mimalloc`,
>   `--with-tail-call-interp`, `-O3`, by Clang 22.1.3. The only differences that matter are
>   `--disable-gil` and the JIT: the GIL build was configured
>   `--enable-experimental-jit=yes-off` and reports `sys._jit.is_enabled() == False`, the
>   free-threaded build reports `sys._jit.is_available() == False`. **Since the JIT is off
>   in both, the comparison is fair — but if you re-run this with `PYTHON_JIT=1` set, every
>   number in §3 becomes meaningless.** Check your builds before you compare them.
>
> **Three benchmarking hazards I had to control for, and you will too:**
>
> 1. **Heterogeneous cores.** This CPU has 5 fast cores and 6 slow ones. The *same*
>    benchmark, same interpreter, forced onto efficiency cores with `taskpolicy -b`:
>    **0.718 s → 1.336 s on the GIL build, 0.924 s → 1.599 s on the free-threaded build**
>    *(measured)* — E-cores are ~1.8× slower. Any thread-scaling curve that goes past 5
>    threads on this machine is measuring core quality, not parallelism. Every scaling table
>    in §7 is annotated accordingly.
> 2. **Drift.** All A/B suites alternate interpreters `A,B,B,A` / `A,B,B,A` reversed, take
>    min-of-3 in-process and min across runs, and report the observed run-to-run spread so
>    you can see the noise floor. See [`31-measurement-methodology.md`](31-measurement-methodology.md).
> 3. **These are not `pyperformance`.** My suite is nine hand-written benchmarks skewed
>    toward eval-loop-bound work. Where my geomean disagrees with the official published
>    figure, §3.3 says so and explains why rather than pretending it doesn't.

## Contents

1. [Where free-threading actually stands](#1-where-free-threading-actually-stands)
2. [Getting it, and knowing what you got](#2-getting-it-and-knowing-what-you-got)
3. [The single-threaded tax, measured](#3-the-single-threaded-tax-measured)
4. [The memory tax, and why it is missing from every migration guide](#4-the-memory-tax-and-why-it-is-missing-from-every-migration-guide)
5. [Python-level correctness: no new races, much worse odds](#5-python-level-correctness-no-new-races-much-worse-odds)
6. [The genuinely new hazard: C extensions](#6-the-genuinely-new-hazard-c-extensions)
7. [The sharing wall: the new scaling limit](#7-the-sharing-wall-the-new-scaling-limit)
8. [Stop-the-world, and what the GIL build actually does](#8-stop-the-world-and-what-the-gil-build-actually-does)
9. [Tooling: what works today](#9-tooling-what-works-today)
10. [Ecosystem state, dated](#10-ecosystem-state-dated)
11. [An honest decision framework](#11-an-honest-decision-framework)
12. [Lab exercises](#12-lab-exercises)
13. [Question bank](#13-question-bank)
14. [Sources](#14-sources)

---

## 1. Where free-threading actually stands

Version facts rot. Every claim in this section carries the date I verified it.

### 1.1 The three-phase rollout

PEP 703 was accepted by the Steering Council in **October 2023**, with an explicit
three-phase plan:

| Phase | Meaning | Status as of 2026-08-02 |
|---|---|---|
| **I** | Free-threaded build exists, marked **experimental** | Shipped in **3.13** |
| **II** | Free-threaded build **officially supported**, still optional | Shipped in **3.14** |
| **III** | Free-threaded build becomes **the default** | **Not scheduled. No committed date.** |

**[PEP 779 — "Criteria for supported status for free-threaded Python"](https://peps.python.org/pep-0779/)**
is the document that moved the project from phase I to phase II. Verified on the PEP index
2026-08-02: authors Thomas Wouters, Matt Page, Sam Gross; **Status: Final**; Type: Standards
Track; Created 13-Mar-2025; Python-Version 3.14; **Resolution 16-Jun-2025**. The Steering
Council accepted it "with a non-exhaustive list of requirements to be addressed during
phase II" — which is the polite way of saying phase II is a probation period, not a
graduation.

**What this means practically:** "officially supported" means the CPython team will treat
free-threaded bugs as release blockers and will not delete the build. It does **not** mean
your dependencies work, and it does **not** mean the default `python3.14` binary you get
from your distro has anything to do with it. The GIL build is still what ships everywhere
by default.

### 1.2 What changed in 3.15

I checked `docs.python.org/3.15/whatsnew/3.15.html` on **2026-08-02** (the page serves
**3.15.0b4**; 3.15 final is scheduled for 1 Oct 2026). The free-threading-relevant changes:

- **[PEP 803 — "abi3t": Stable ABI for Free-Threaded Builds](https://peps.python.org/pep-0803/)**
  lands in 3.15. This was the "unverified lead" in my brief and it checks out, with details
  worth getting right — see §1.3.
- **mimalloc becomes the default allocator for raw memory allocations** (`PyMem_RawMalloc`
  and friends), "for better performance on free-threaded builds" (gh-144914, Kumar Aditya).
  Note the direction of travel: the free-threaded build's allocator choice is now bleeding
  into the default build.
- **[PEP 788](https://peps.python.org/pep-0788/) — protecting the C API from interpreter
  finalization.** Adds interpreter guards and interpreter views, plus new APIs to attach and
  detach thread states safely. Critically, **`PyGILState_Ensure()` / `PyGILState_Release()`
  are now soft-deprecated** — no removal planned, but no new `PyGILState` APIs will be added.
  If your extension's threading story is built on `PyGILState`, that is now a legacy path.
- **[PEP 799](https://peps.python.org/pep-0799/)** adds a stdlib sampling profiler
  ("Tachyon") with a `--mode gil` that measures GIL-holding time per thread. Useful for the
  *before* half of a migration (§11), and a nice irony: a new GIL-measurement tool shipping
  in the same release as the free-threaded stable ABI.
- **[PEP 831](https://peps.python.org/pep-0831/)**: frame pointers on by default, which
  improves native stack unwinding for profilers — relevant to §9.

**What is *not* in the 3.15 release notes: any mention of phase III, of the free-threaded
build becoming default, or of the single-threaded overhead being closed.** I searched the
whole page for "phase", "default build", and "GIL" and found nothing on that subject. Treat
any claim that "free-threading is the default in 3.15" as false.

### 1.3 PEP 803 — verified

My brief flagged this as unverified. Here is what it actually is, read from
`peps.python.org/pep-0803/` on **2026-08-02**:

| Field | Value |
|---|---|
| Number & title | **PEP 803 — "abi3t": Stable ABI for Free-Threaded Builds** |
| Authors | Petr Viktorin, Nathan Goldbaum |
| Status | **Final** |
| Type | Standards Track |
| Created | 19-Aug-2025 |
| Python-Version | **3.15** |
| Resolution | **30-Mar-2026** |
| Requires | PEP 703, **PEP 793**, PEP 697 |

The lead was right on substance and right on the tag name. The details that matter:

- `abi3t` is `abi3` **with `PyObject` made opaque**. That is the whole trick: the reason
  there could not be a single stable ABI across both builds is that the object header
  differs (§4), so any extension that embeds `PyObject` in its instance struct bakes the
  wrong layout into its binary.
- Consequently, migrating is **not** a recompile. Per the 3.15 what's-new and the
  [abi3t migration HOWTO](https://docs.python.org/3.15/howto/abi3t-migration.html), you must:
  1. Switch to **PEP 697** APIs — negative `basicsize` and `PyObject_GetTypeData()` — instead
     of making `PyObject` part of your instance struct.
  2. Switch from a `PyInit_*` function to the new **`PyModExport_*`** export hook from
     **PEP 793**, with the `PySlot` structure from **PEP 820**.
- The compile-time knob is **`Py_TARGET_ABI3T`** (deliberately *not* reusing
  `Py_LIMITED_API`; the PEP's "Knob name" section explains that `Py_LIMITED_API` is a
  misnomer it did not want to propagate). You can set it by hand:
  `-DPy_TARGET_ABI3T=0x30f0000`.
- The wheel tag is **`abi3.abi3t`** — you signal compatibility with both at once. The
  version-specific free-threaded tag remains **`cp315t`**.
- **As of the 3.15 documentation being written, no build backend supports it.** The
  what's-new text names setuptools, meson-python, scikit-build-core and Maturin and says
  "at the time of writing, these tools do not support `abi3t`. If this is the case for your
  tool, compile for `cp315t` separately." Verified 2026-08-02. This is the single most
  important caveat: **PEP 803 is Final, and you probably cannot use it yet.**
- PEP 803 also positions itself as transitional — its "Rejected Ideas" section explains that
  `abi3t` is deliberately a smaller change than an `abi4`, "making it work better as a
  transitional state before larger changes like PEP 809's `abi2026`."

**Why this PEP exists at all** is the best one-paragraph summary of the ecosystem's pain.
From the PEP's motivation section: the `cryptography` project ships 48 wheels per release —
14 for `cp38` abi3, 14 for `cp311` abi3, 14 for `cp314t`, 6 for PyPy. Without a free-threaded
stable ABI, by 3.15 they would spend "roughly the same amount of space on PyPI to support
two versions of the free-threaded build as *all* non-EOL versions of the GIL-enabled build."
Alex Gaynor, quoted in the PEP: "What we can't/won't do is O(n) where we need new builds for
every Python release."

---

## 2. Getting it, and knowing what you got

### 2.1 Getting it

```bash
# uv (what this machine uses) — the +freethreaded suffix is the whole API
uv python install 3.14t
uv venv --python 3.14t .venv

# pyenv
pyenv install 3.14.6t

# from source
./configure --disable-gil --enable-optimizations --with-lto && make -j

# official installers: macOS and Windows installers from python.org offer
# free-threaded binaries as an optional component (since 3.13).
```

On this machine the free-threaded interpreter lives at
`~/.local/share/uv/python/cpython-3.14.6+freethreaded-macos-aarch64-none/` — note the
`+freethreaded` in the *directory* name, `python3.14t` for the binary, and `python3.14t` for
the `include/` directory. Manylinux and cibuildwheel both support the `t` suffix.

### 2.2 The four ways to ask "am I free-threaded?" — and which one to use

They answer **different questions**, and conflating them is a real production bug.

| Check | Answers | Use it for |
|---|---|---|
| `sysconfig.get_config_var("Py_GIL_DISABLED")` → `1` | *Was this interpreter built free-threaded?* | **Build/packaging decisions.** The official recommendation. |
| `sys._is_gil_enabled()` → `False` | *Is the GIL off right now, in this process?* | **Runtime monitoring** — the only one that catches §6's cliff. |
| `sys.abiflags` == `"t"`, `EXT_SUFFIX` ends `t-*.so` | *Which wheels/extensions match?* | Wheel selection, debugging import failures. |
| `python -VV` / `sys.version` contains `"free-threading build"` | *Human-readable identification.* | Logs, bug reports. |
| `#ifdef Py_GIL_DISABLED` in C | *Am I compiling for the FT build?* | Extension source. |

Measured on this machine:

```
$ python3.14  -VV   →  Python 3.14.6 (main, Jun 11 2026, 03:55:33) [Clang 22.1.3 ]
$ python3.14t -VV   →  Python 3.14.6 free-threading build (main, Jun 11 2026, 03:55:38) ...
```

**The trap:** `Py_GIL_DISABLED == 1` and `sys._is_gil_enabled() == False` are *not* the same
statement. A free-threaded build can be running **with the GIL turned back on** — because
you asked (`PYTHON_GIL=1`), or because an extension forced it (§6). A deploy check that only
tests the build config will happily certify a process that is running single-threaded-locked.

```python
# The health check that actually earns its keep. Ship this.
import sys, sysconfig
BUILD_IS_FT = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
if BUILD_IS_FT and sys._is_gil_enabled():
    log.error("free-threaded build is running WITH THE GIL — an extension re-enabled it")
    metrics.gauge("python.gil_reenabled", 1)
```

### 2.3 Forcing the issue

```bash
PYTHON_GIL=0 python3.14t app.py     # keep the GIL off no matter what imports demand
python3.14t -X gil=0 app.py         # identical
PYTHON_GIL=1 python3.14t app.py     # run the FT build *with* a GIL — see §3.4
```

`PYTHON_GIL=0` is documented as "at your own risk". It is exactly the right tool for a
one-off experiment ("how much is this extension costing me?") and exactly the wrong tool for
a production default.

---

## 3. The single-threaded tax, measured

This is the number that decides most migrations, and it is the number people quote most
carelessly.

### 3.1 The official figure

From the [official HOWTO](https://docs.python.org/3/howto/free-threading-python.html)
(verified 2026-08-02, page served as 3.14.6 docs):

> "On the pyperformance benchmark suite, the average overhead ranges from about **1% on
> macOS aarch64** to **8% on x86-64 Linux** systems."

This machine is macOS aarch64. So the expectation going in was ~1%.

### 3.2 What I measured

Nine benchmarks, min-of-3 in-process, four alternating process runs per build per round,
two rounds with the run order reversed. *(measured)*

| Benchmark | What it stresses | GIL (s) | FT (s) | FT/GIL | Overhead |
|---|---|---|---|---|---|
| `nbody` | float arithmetic, list indexing | 0.4761 | 0.6050 | 1.271 | **+27.1%** |
| `attr_dispatch` | `__slots__` attribute + method calls | 0.5201 | 0.6360 | 1.223 | **+22.3%** |
| `calls_fib34` | pure function-call overhead | 0.2756 | 0.3203 | 1.162 | +16.2% |
| `regex` | `re.findall` — C matcher, Python objects out | 0.4883 | 0.5476 | 1.122 | +12.2% |
| `dict_churn` | 9M dict get/set on one dict | 0.3954 | 0.4398 | 1.112 | +11.2% |
| `fannkuch` | list slicing/permutation | 2.3638 | 2.4026 | 1.016 | +1.6% |
| `str_ops` | `%`-format, `join`, `split` | 0.4537 | 0.4430 | 0.976 | **−2.4%** |
| `pickle` | C pickler round-trip | 0.4499 | 0.4263 | 0.947 | **−5.3%** |
| `alloc_churn` | allocating tuples/lists/strs in bulk | 0.4632 | 0.4393 | 0.948 | **−5.2%** |
| **GEOMEAN** | | | | **1.081** | **+8.1%** |

Run-to-run spread (max/min across four runs of the same build) was 1.04–1.43× on the GIL
build and 1.01–1.12× on the free-threaded build — the free-threaded build was consistently
the *quieter* of the two, which is itself worth noticing.

**The geomean reproduced at 8.2% in an earlier, shorter-workload run of the same suite.**
Two independent passes, 8.2% and 8.1%.

### 3.3 So who is wrong — me or the docs?

Neither, and the reconciliation is the actual lesson.

- **I am not running pyperformance.** pyperformance is ~60 benchmarks including a lot of
  I/O-ish, startup-ish, and library-heavy work that dilutes eval-loop overhead. My nine are
  deliberately weighted toward tight interpreted loops, which is where biased refcounting's
  extra branch and the wider object header cost the most. **A suite is a weighting scheme,
  and the weighting *is* the result.** If your service looks like `nbody` and
  `attr_dispatch`, 1% is a fantasy and 20% is your number. If it looks like `pickle` and
  `str_ops`, you may come out ahead.
- **Look at the sign flips.** Three benchmarks are *faster* on the free-threaded build. That
  is not noise — `pickle`, `alloc_churn` and `str_ops` are allocation-dominated, and the
  free-threaded build allocates all objects through **mimalloc** while the GIL build uses
  **pymalloc** for small objects. For GC-tracked objects the free-threaded build also has no
  separate `PyGC_Head` to allocate in front of the object (§4), so the allocation is smaller
  *and* the allocator is different. Free-threading is not a uniform tax; it is a different
  cost model with winners and losers.
- **`regex` at +12% is the most instructive row.** `re` matching is pure C and should be
  unaffected. But `findall` *materialises* a list of tuples of `str` objects — thousands of
  short-lived, non-GC-tracked objects, every one of them 16 bytes wider and every refcount
  operation going through the biased-refcount check. **"It's in C" does not mean "it's
  immune."** What matters is how many `PyObject`s cross the boundary.

### 3.4 Why the platform spread is real (1% macOS aarch64 vs 8% x86-64 Linux)

The official range is not measurement sloppiness. Two mechanisms, both from Tier 0:

**Memory ordering.** x86-64 is **TSO** — strong ordering, where ordinary loads and stores
already have acquire/release semantics and only store-load needs a fence. AArch64 is
**weakly ordered**: the compiler must emit explicit `ldar`/`stlr` (acquire/release) or `dmb`
barriers for the atomics that free-threading sprinkles through the refcount and lock paths.
Naïvely you would predict ARM pays *more*. It frequently pays *less*, because on x86 the
`lock`-prefixed read-modify-write instructions used for the shared refcount path are
brutally expensive — they serialise the store buffer — whereas AArch64's LL/SC (`ldxr`/`stxr`)
and LSE atomics (`ldadd`) are comparatively cheap and, crucially, cheaper *uncontended*.
See [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §5 and doc
02 for the underlying model.

**Cache topology.** Apple Silicon has 128-byte cache lines, very large L1/L2, and a low-
latency cluster interconnect. The wider object header (§4) costs relatively less when your
L1 is enormous and your line is 128 bytes, since a 32-byte object and a 48-byte object are
often in the same line either way. On a 64-byte-line x86 server with more sockets and more
cores, the same header growth spills more objects across line boundaries and the coherence
domain is bigger.

**The staff-level version of this answer:** the single-threaded overhead is not one number
because it is not one mechanism. It is (a) an extra predictable branch per refcount, (b) a
wider object header changing cache occupancy, (c) atomic operations on the escape path, and
(d) a different allocator. Each of those four scales differently with ISA and cache
topology, and the mix is workload-dependent. **Measure your workload on your hardware. It is
one afternoon and it replaces an argument.**

### 3.5 The tax you pay before `main()`

Interpreter startup, best of 10 *(measured)*:

| Command | GIL build | FT build | Δ |
|---|---|---|---|
| `-c pass` | 20.5 ms | 24.0 ms | **+17%** |
| `-S -c pass` (no `site`) | 18.7 ms | 19.6 ms | +5% |
| `-c "import json,re,threading"` | 28.3 ms | 35.7 ms | **+26%** |

If you run a fleet of short-lived workers, serverless functions, or a CLI invoked in a loop,
this line item can dominate everything else in this document.

---

## 4. The memory tax, and why it is missing from every migration guide

[`16-object-memory-layout.md`](16-object-memory-layout.md) §2 established, by measurement,
that **every non-GC-tracked object grows by exactly 16 bytes on the free-threaded build,
while GC-tracked containers are unchanged.** That is the finding. This section turns it into
a migration checklist, because the doc-16 result is about `sys.getsizeof` and what you
actually get billed for is RSS.

### 4.1 The mechanism, stated precisely

Doc 24 §8.2 covers biased refcounting. The layout consequence:

```
   GIL BUILD                              FREE-THREADED BUILD
   ─────────────────────                  ────────────────────────────────

   GC-tracked object:                     GC-tracked object:
   ┌──────────────────┐ ← malloc          ┌──────────────────┐ ← malloc & PyObject*
   │ PyGC_Head  16 B  │                   │ ob_tid      8 B  │
   │  _gc_next   8 B  │                   │ ob_flags/  ┐     │
   │  _gc_prev   8 B  │                   │ ob_gc_bits ┘4+4 B│  ← GC state lives IN
   ├══════════════════┤ ← PyObject*       │ ob_ref_local 4 B │     the header now
   │ ob_refcnt   8 B  │                   │ ob_ref_shared 4B │
   │ ob_type     8 B  │                   │ ob_type     8 B  │
   ├──────────────────┤                   ├──────────────────┤
   │ ...fields...     │                   │ ...fields...     │
   └──────────────────┘                   └──────────────────┘
     16 (GC) + 16 (obj) = 32 B of              32 B of header — a wash
     header before your fields

   NON-GC object (int, float, str, bytes):
   ┌══════════════════┐ ← PyObject*       ┌══════════════════┐ ← PyObject*
   │ ob_refcnt   8 B  │                   │ ob_tid      8 B  │
   │ ob_type     8 B  │                   │ ob_flags+gc 8 B  │
   ├──────────────────┤                   │ ob_ref_local 4 B │
   │ ...fields...     │                   │ ob_ref_shared 4B │
   └──────────────────┘                   │ ob_type     8 B  │
     16 B of header                       ├──────────────────┤
                                          │ ...fields...     │
                                          └──────────────────┘
                                            32 B of header — +16 B, unconditionally
```

The official HOWTO states it plainly under *"Non-GC objects have a larger object header"*
(verified 2026-08-02):

> "Instead of having the GC related information allocated before the `PyObject` structure,
> like in the default build, the GC related info is part of the normal object header. For
> example, on the AMD64 platform, `None` uses 32 bytes on the free-threaded build vs 16
> bytes for the default build. GC objects (such as dicts and lists) are the same size for
> both builds since the free-threaded build does not use additional space for the GC info."

> **A correction to doc 16 worth carrying forward.** [`16-object-memory-layout.md`](16-object-memory-layout.md)
> §2 says the GC head "moves into the mimalloc page metadata". That is half right and the
> half matters. Two distinct things happened: (a) the **GC flag bits** moved into the object
> header (`ob_gc_bits`), which is why the header is 16 B wider for *everyone*; and (b) the
> **`_gc_next`/`_gc_prev` doubly-linked list is gone entirely** — the collector finds GC
> objects by traversing mimalloc's heaps, which is the load-bearing reason mimalloc was
> chosen (doc 24 §8.4). Saying "the GC head moved into page metadata" blurs those. The
> observable — containers unchanged, scalars +16 — is right either way.

### 4.2 What that costs in RSS, which is what you are billed for

`sys.getsizeof` reports the object's own size. The allocator rounds. Here is real
resident-set cost per object, measured by allocating 1,000,000 of each into a pre-sized
list and differencing peak RSS *(measured)*:

| Object | GIL RSS/obj | FT RSS/obj | Δ | `getsizeof` GIL → FT | GC-tracked? |
|---|---|---|---|---|---|
| `int` (1 digit) | 32.1 B | 45.0 B | **+12.8** | 28 → 44 | no |
| `float` | 32.1 B | 45.0 B | **+12.8** | 24 → 40 | no |
| `str` (9 chars) | 64.2 B | 77.1 B | **+12.9** | 50 → 66 | no |
| `bytes` (9) | 48.2 B | 61.0 B | **+12.8** | 42 → 58 | no |
| `tuple` (2 × `None`) | 64.2 B | 61.6 B | **−2.6** | 64 → 64 | **yes** |
| `list` (1 × `None`) | 80.3 B | 77.8 B | **−2.4** | 64 → 64 | **yes** |
| `__slots__` instance (2) | 48.2 B | 47.6 B | **−0.6** | 48 → 48 | **yes** |

Read that carefully, because it is a *stronger* result than doc 16's:

1. **The +16 B header growth shows up as ≈ +12.8 B of real RSS**, not +16. Allocator rounding
   eats some of it: pymalloc's 16-byte-aligned size classes already rounded a 24-byte `float`
   up to 32, so part of the growth was pre-paid. Never assume header deltas pass through to
   RSS one-for-one — measure.
2. **GC-tracked containers are not merely a wash on the free-threaded build — they are
   slightly cheaper.** −2.6 B for a 2-tuple, −2.4 B for a 1-list. You lose 16 bytes of
   `PyGC_Head`, gain 16 bytes of header, and then mimalloc's finer size classes give a small
   rebate over pymalloc's 16-byte alignment.
3. **`float` and `int` land on identical RSS in both builds** despite `getsizeof` differing
   by 4 bytes. That is the allocator's size class talking, not the object.

### 4.3 Whole-process peak RSS, which is what gets you OOM-killed

Peak RSS of a process that builds one realistic data structure and holds it *(measured)*:

| Workload | GIL peak RSS | FT peak RSS | Ratio |
|---|---|---|---|
| **baseline (interpreter only)** | **18.0 MB** | **24.7 MB** | **1.37×** |
| 2M `int`s in a list | 96.5 MB | 167.5 MB | **1.74×** |
| 2M `float`s in a list | 96.5 MB | 167.5 MB | **1.74×** |
| 2M 9-char `str`s in a list | 157.8 MB | 199.9 MB | 1.27× |
| 500k 4-tuples of ints | 123.5 MB | 156.2 MB | 1.27× |
| 500k `__slots__` instances | 77.2 MB | 96.8 MB | 1.25× |

Four things to take from this table:

**The 37% baseline.** An empty free-threaded interpreter costs **6.7 MB more** before your
code runs. Multiply by your worker count. A 32-worker gunicorn fleet pays 214 MB for the
privilege of existing. This is the single number most likely to be missing from your capacity
plan, and it appears in no migration guide I found.

**"Container-heavy" workloads are usually scalar-heavy workloads wearing a hat.** The 4-tuple
row shows +27% even though §4.2 proved tuples are individually *cheaper* on the free-threaded
build — because each tuple retains four `int` objects, and the ints dominate. When you audit
your object graph, count the **leaves**, not the nodes.

**The int/float rows are the worst case and they are common.** Feature vectors, time-series
buffers, parsed JSON numbers, ORM row scalars, counters keyed by string. If your service's
heap is a large dict of small Python numbers, budget **+70–75% RSS**. The fix is the same
fix it always was — `array`, `numpy`, `struct`, `bytes` — but free-threading raises the
penalty for not having done it.

**Three more RSS effects the table cannot show, from the HOWTO's "Behavioral changes"
section** (verified 2026-08-02) — every one of them makes memory *later* rather than *more*:

- **All interned strings are immortal.** In the GIL build, `sys.intern()`-ed strings are
  removed from the interned table when the last reference dies. On the free-threaded build
  they survive to interpreter shutdown. If you intern user-controlled strings, you have
  written an unbounded leak.
- **QSBR defers frees.** Lock-free reads of `list` and dict-keys objects require safe memory
  reclamation, so freeing is deferred to a quiescent state. `gc.collect()` flushes it.
- **Per-thread refcounting delays deallocation** for heap types, code objects and module
  `__dict__`s; **deferred refcounting** delays it for modules, top-level functions, class
  methods, descriptors and `threading.local` objects. Both mean the object is freed by the
  *GC*, not by the refcount hitting zero. `gc.collect()` merges the counts.

**Migration checklist — the memory section:**

- [ ] Measure baseline RSS of an empty interpreter on both builds. Multiply by worker count.
- [ ] Classify your heap: what fraction is non-GC-tracked scalars (`int`/`float`/`str`/`bytes`)?
- [ ] Re-run your peak-RSS load test on the free-threaded build. Do not extrapolate.
- [ ] Check cgroup/container limits against the new peak, not the old one +10%.
- [ ] Audit `sys.intern()` calls for user-controlled input.
- [ ] If RSS regressed and you cannot explain it, call `gc.collect()` at a quiet point and
      re-measure — QSBR, deferred and per-thread refcounting all hide behind that call.
- [ ] If you need memory back from the allocator faster, `MIMALLOC_PURGE_DELAY=0` is
      documented — and documented as costing performance.

---

## 5. Python-level correctness: no new races, much worse odds

[`24-the-gil.md`](24-the-gil.md) §6 makes the key claim: free-threading mostly does not
create **new** races in Python code; it raises the **probability** of the ones you already
had. This section proves it with a measurement, then draws the practical line.

### 5.1 What stays atomic, and why

The free-threaded build gives `dict`, `list` and `set` **internal per-object locks**. From
the HOWTO's "Thread safety" section (verified 2026-08-02):

> "Built-in types like `dict`, `list`, and `set` use internal locks to protect against
> concurrent modifications in ways that behave similarly to the GIL. However, Python has not
> historically guaranteed specific behavior for concurrent modifications to these built-in
> types, so this should be treated as a description of the current implementation, not a
> guarantee of current or future behavior."

Read that second sentence twice. **The atomicity you have been relying on was never
specified.** It was an emergent property of an implementation detail, and the free-threaded
build is *choosing* to preserve it. The docs' own recommendation: "use `threading.Lock` or
other synchronization primitives instead of relying on the internal locks of built-in types,
when possible."

The rule that actually predicts the answer:

> **An operation is atomic iff it is one C-level step that never re-enters the eval loop.**
> Under the GIL that step cannot be interleaved because no other thread runs bytecode. Under
> free-threading that step cannot be interleaved because it takes the object's lock.
> **Different mechanism, same guarantee.** Everything else — anything that is more than one
> bytecode, or that calls back into Python — was never atomic and still isn't.

| Operation | GIL build | Free-threaded | Why |
|---|---|---|---|
| `lst.append(x)` | atomic | atomic | one C call, takes the list's lock |
| `d[k] = v`, builtin key | atomic | atomic | one C call, takes the dict's lock |
| `d[k] = v`, key with Python `__hash__` | **not atomic** | **not atomic** | re-enters the eval loop |
| `x += 1` | **not atomic** | **not atomic** | LOAD/BINARY_OP/STORE |
| `d[k] += 1` | **not atomic** | **not atomic** | read-modify-write across bytecodes |
| `if k not in d: d[k] = v` | **not atomic** | **not atomic** | check-then-act |
| `obj.attr += 1` | **not atomic** | **not atomic** | as above |
| `lst[i] = lst[j]` | atomic | atomic | single C-level store |
| iterating a shared iterator | **unsafe** | **unsafe** | see below |

**One genuinely sharper edge:** the HOWTO's *Known limitations* section adds under
"Iterators": *"It is generally not thread-safe to access the same iterator object from
multiple threads concurrently, and threads may see duplicate or missing elements."* This was
true under the GIL too — but under the GIL the interleaving window was one bytecode wide, so
it approximately never happened. Sharing one generator across a thread pool is a pattern
people get away with today and will not get away with tomorrow.

### 5.2 The measurement: how much worse do the odds get?

Four threads, 200,000 `counter += 1` each on a shared module global. Expected: 800,000.

```
GIL build (3.14.6),  run 1:  got 800000  →  lost 0        ( 0.00%)
GIL build (3.14.6),  run 2:  got 800000  →  lost 0        ( 0.00%)
GIL build (3.14.6),  run 3:  got 800000  →  lost 0        ( 0.00%)
FT  build (3.14.6t), run 1:  got 211549  →  lost 588451   (73.56%)
FT  build (3.14.6t), run 2:  got 211860  →  lost 588140   (73.52%)
FT  build (3.14.6t), run 3:  got 228573  →  lost 571427   (71.43%)
```
*(measured)*

That is the whole argument in one table. The **same broken program**:

- On the GIL build it produced the correct answer three times out of three. The interleaving
  window is a handful of bytecodes wide and only opens at a switch-interval boundary (doc 24
  §3), so with 800k increments over ~30 ms the expected number of lost updates is a *small
  integer* — and I observed zero. In production this bug surfaces as one wrong metric a
  month and gets closed as "could not reproduce".
- On the free-threaded build it lost **73% of the updates, reproducibly.**

**This is the strongest practical argument for migrating that exists, and nobody frames it
this way:** the free-threaded build is a *race-condition amplifier*. Running your existing
test suite under `python3.14t` is one of the cheapest concurrency audits available, whether
or not you ever deploy on it.

Control, same harness, four threads × 50,000 `list.append`: **200,000 / 200,000 on both
builds** *(measured)*. The C-level-indivisible operations really do hold.

### 5.3 Two behavioural differences that are not races but will bite

Both from the HOWTO's "Behavioral changes", verified 2026-08-02, and both are *defaults*
that differ between builds:

- **`thread_inherit_context` defaults to true** on the free-threaded build. Threads started
  with `threading.Thread` inherit a *copy* of the caller's `contextvars.Context`. On the GIL
  build the flag defaults to false and threads start with an empty context. If you use
  `contextvars` for request IDs or tenant scoping, **your propagation behaviour silently
  changes when you switch builds.** This is a correctness difference in observability and
  authorization code, not a performance note.
- **`context_aware_warnings` defaults to true** on the free-threaded build, which makes
  `warnings.catch_warnings` use a context variable rather than mutating the global filter
  list. The GIL build's behaviour is not thread-safe; the free-threaded build's is. Tests
  that assert on warning capture across threads may behave differently.

---

## 6. The genuinely new hazard: C extensions

Python-level code mostly survives. C extensions are where the real work is — and where the
failure mode is *silence*.

### 6.1 The opt-in protocol

An extension must **declare** that it is free-threading-safe. There are exactly two ways,
depending on initialisation style. These are the real names, from the
[extensions HOWTO](https://docs.python.org/3/howto/free-threading-extensions.html)
(verified 2026-08-02):

```c
/* Multi-phase init (PyModuleDef_Init) — the modern path */
static struct PyModuleDef_Slot module_slots[] = {
    ...
#if PY_VERSION_HEX >= 0x030D0000
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
#endif
    {0, NULL}
};

/* Single-phase init (PyModule_Create) — the legacy path */
PyMODINIT_FUNC PyInit_mymodule(void) {
    PyObject *m = PyModule_Create(&moduledef);
    if (m == NULL) return NULL;
#ifdef Py_GIL_DISABLED
    PyUnstable_Module_SetGIL(m, Py_MOD_GIL_NOT_USED);
#endif
    return m;
}
```

Note the guards. `Py_mod_gil` needs a `PY_VERSION_HEX` guard because the slot does not exist
before 3.13. `PyUnstable_Module_SetGIL` needs `#ifdef Py_GIL_DISABLED` because **the function
is only defined in the free-threaded build**.

### 6.2 The decision flow, and the cliff

```
                      import some_extension.so
                                │
                                ▼
                  ┌──────────────────────────────┐
                  │ Is this a free-threaded build │
                  │   (Py_GIL_DISABLED == 1) ?    │
                  └───────┬───────────────┬───────┘
                       no │               │ yes
                          ▼               ▼
                 ┌────────────────┐   ┌──────────────────────────────────┐
                 │ load normally. │   │ Does the module declare          │
                 │ Py_mod_gil is  │   │   Py_mod_gil = Py_MOD_GIL_NOT_USED│
                 │ ignored.       │   │   (or call PyUnstable_Module_    │
                 └────────────────┘   │    SetGIL with it)?              │
                                      └───────┬──────────────────┬───────┘
                                          yes │                  │ no
                                              ▼                  ▼
                              ┌────────────────────┐   ┌─────────────────────────────┐
                              │ load normally.     │   │ Is PYTHON_GIL=0 / -Xgil=0   │
                              │ GIL stays OFF.     │   │ set?                        │
                              └────────────────────┘   └──────┬───────────────┬──────┘
                                                          yes │               │ no
                                                              ▼               ▼
                                              ┌───────────────────┐  ┌─────────────────────────┐
                                              │ load. GIL stays   │  │ ***PAUSE ALL THREADS,   │
                                              │ OFF. You own the  │  │    ENABLE THE GIL,      │
                                              │ consequences.     │  │    emit RuntimeWarning,  │
                                              └───────────────────┘  │    continue loading***  │
                                                                     │                         │
                                                                     │ Your process is now a   │
                                                                     │ GIL build that also pays │
                                                                     │ the free-threading tax. │
                                                                     └─────────────────────────┘
```

I built both variants of a minimal extension from the same C file — one with
`PyUnstable_Module_SetGIL(m, Py_MOD_GIL_NOT_USED)`, one without — compiled against the
free-threaded headers, and imported them *(measured)*:

```
$ PYTHONPATH=noopt python3.14t -c "import sys; print(sys._is_gil_enabled()); \
                                   import legacy_ext; print(sys._is_gil_enabled())"
False
True                            ← the GIL came back, mid-process

$ PYTHONPATH=noopt python3.14t -c "import legacy_ext" 2>&1
<frozen importlib._bootstrap>:491: RuntimeWarning: The global interpreter lock (GIL) has
been enabled to load module 'legacy_ext', which has not declared that it can run safely
without the GIL. To override this behavior and keep the GIL disabled (at your own risk),
run with PYTHON_GIL=0 or -Xgil=0.

$ PYTHONPATH=optin  python3.14t -c "import legacy_ext, sys; print(sys._is_gil_enabled())"
False                           ← one macro's difference

$ PYTHONPATH=noopt PYTHON_GIL=0 python3.14t -c "import legacy_ext, sys; print(sys._is_gil_enabled())"
False                           ← override works
```

And the cost, same dict-churn workload, on the free-threaded build with and without that
import *(measured)*:

| | 1 thread | 4 threads |
|---|---|---|
| GIL disabled | 0.178 s | **0.254 s** |
| Legacy extension imported → GIL re-enabled | 0.283 s | **0.673 s** |

**2.65× slower at 4 threads, from an `import`.** And note the single-thread column: 0.178 →
0.283. Re-enabling the GIL does not give you the GIL build's performance — you keep paying
biased refcounting and the wider header *and* you get the GIL back. **This is the worst of
both worlds and it is one transitive dependency away.**

### 6.3 Detecting it in production

The warning is a `RuntimeWarning` from `importlib._bootstrap`. Warnings are, in most
production configurations, invisible. Three levels of defence, in increasing order of
seriousness:

```python
# 1. Assert at startup. Cheapest, catches everything imported so far.
import sys, sysconfig
if sysconfig.get_config_var("Py_GIL_DISABLED") == 1 and sys._is_gil_enabled():
    raise SystemExit("refusing to start: an extension re-enabled the GIL")

# 2. Export it as a metric, and alarm on it. Catches LAZY imports —
#    the ones that happen on first request, hours after startup.
metrics.gauge("python.gil_enabled", int(sys._is_gil_enabled()))
```

```bash
# 3. In CI: make the warning fatal. This is the one that actually prevents the incident.
python3.14t -W error::RuntimeWarning -m pytest
```

Verified *(measured)*: `-W error` turns the import into a hard failure with the full
traceback naming the offending module. Put it in CI on the day you start the migration.

> **Why level 2 matters more than it looks.** Level 1 only sees modules imported before the
> check. A plugin loaded on demand, a `pandas` accessor that imports its C helper lazily, a
> driver imported inside a request handler — all of these flip the GIL on *while serving
> traffic*. The signature in your dashboards is a step change in latency and a collapse in
> CPU utilisation across all cores at a moment that correlates with nothing in your deploy
> log. Ship the gauge.

### 6.4 What extension authors must actually change

Beyond the opt-in macro, the extensions HOWTO enumerates the real hazards. These are the
ones with teeth:

**Borrowed references become genuinely dangerous.** Under the GIL, a borrowed reference was
safe until you released the GIL. Under free-threading the container can be mutated
concurrently and your borrow can dangle. The replacements return **strong** references:

| Borrowed (unsafe if the container may be mutated) | Strong-reference replacement |
|---|---|
| `PyList_GetItem`, `PyList_GET_ITEM` | `PyList_GetItemRef` |
| `PyDict_GetItem`, `PyDict_GetItemWithError` | `PyDict_GetItemRef` |
| `PyDict_GetItemString` | `PyDict_GetItemStringRef` |
| `PyDict_SetDefault` | `PyDict_SetDefaultRef` |
| `PyWeakref_GetObject`, `PyWeakref_GET_OBJECT` | `PyWeakref_GetRef` |
| `PyImport_AddModule` | `PyImport_AddModuleRef` |
| `PyCell_GET` | `PyCell_Get` |

**Accessor macros do no locking.** `PyList_GET_ITEM`, `PyList_SET_ITEM`,
`PySequence_Fast_GET_SIZE` — none of them check or lock. They are fine on an object you
provably own, and unsafe on anything reachable from another thread.

**`PyDict_Next` gets its own warning section** in the HOWTO, because iterating a dict while
another thread mutates it is exactly the shape the free-threaded build cannot rescue you
from.

**The allocation domains became a hard requirement, not a best practice.** From the HOWTO:
"the free-threaded build requires that **only** Python objects are allocated using the
object domain, and that **all** Python objects are allocated using that domain." Search your
extension for `PyObject_Malloc` used for plain buffers and change it to `PyMem_Malloc`. This
was sloppiness you got away with before; now it is a correctness bug, because the object
domain's mimalloc heap is the one the GC traverses.

**Global state that the GIL was implicitly protecting is now unprotected.** Module-level
caches, lazily-initialised singletons, `static` scratch buffers. The HOWTO's advice:
lock them, move them to `thread_local`, or disable the cache in free-threaded builds.

**Critical sections are the tool for per-object locking:**

```c
/* Py_BEGIN_CRITICAL_SECTION / Py_END_CRITICAL_SECTION lock ONE object.
   Py_BEGIN_CRITICAL_SECTION2 / Py_END_CRITICAL_SECTION2 lock TWO.
   Both are no-ops on the GIL build, so the same source compiles for both. */
PyObject *result;
Py_BEGIN_CRITICAL_SECTION(obj);
result = Py_NewRef(obj->count);
Py_END_CRITICAL_SECTION();
return result;
```

They must be used in matching pairs in the same C scope (they open a block). The deadlock
story is the interesting part: unlike a plain mutex, a critical section is **released when
the thread would block or re-enter the interpreter**, which is what keeps a re-entrant call
into Python from deadlocking against a lock the same thread already holds. That is why
`Py_BEGIN_CRITICAL_SECTION2` exists at all — you cannot safely nest two of them, so
two-object locking needs a primitive that acquires both in a defined order.

`PyMutex` is also public now for extension-owned state.

---

## 7. The sharing wall: the new scaling limit

This is doc 24 §9's central claim, and it is the part of free-threading that most changes
how you *design*, not just how you deploy. Doc 24 states it. This section measures it.

### 7.1 The physical picture

```
  TOPOLOGY A — DISJOINT GRAPHS                TOPOLOGY B — ONE SHARED GRAPH
  ════════════════════════════                ════════════════════════════

   T0      T1      T2      T3                  T0     T1     T2     T3
    │       │       │       │                   └──┬───┴───┬───┴───┬──┘
    ▼       ▼       ▼       ▼                      ▼       ▼       ▼
  ┌───┐   ┌───┐   ┌───┐   ┌───┐                    ┌──────────────────┐
  │d0 │   │d1 │   │d2 │   │d3 │                    │   ONE dict       │
  └───┘   └───┘   └───┘   └───┘                    │  + its values    │
                                                   └──────────────────┘
  Each object's ob_tid == its                Every object's ob_tid belongs
  owner. Refcount writes take the            to ONE thread. Everyone else
  BIASED FAST PATH: a plain,                 takes the SLOW PATH:
  non-atomic increment on a line             ─ atomic RMW on ob_ref_shared
  held Exclusive in that core's L1.          ─ the line must migrate to the
                                               writing core: MESI M-state
  Cache-line state per core:                   request-for-ownership
     Core0: M(d0)  Core1: M(d1)              ─ every other core Invalidated
     Core2: M(d2)  Core3: M(d3)              ─ plus the dict's per-object
     ── no interconnect traffic ──             LOCK, which serialises the
                                                mutation itself

  MEASURED: 1.97x @2, 2.93x @3               MEASURED: 0.51x @2, 0.45x @3
            (near-linear)                              (NEGATIVE scaling)

  The bottleneck is your CPU count.          The bottleneck is ONE CACHE LINE,
                                             and it is worse than the GIL was.
```

Everything in the right-hand column is [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
§5–6, unchanged. Doc 01 §6 makes the point that closes the loop: *"If threads genuinely write
the same variable — a shared counter, **a Python reference count** — no layout change helps.
Padding cannot save you."* Free-threading did not repeal that. It removed the lock that was
*hiding* it.

### 7.2 The measurement

Identical work — 3,000,000 iterations per thread of a `dict` read-modify-write, or an
attribute read — under three sharing topologies. Speedup is throughput at N threads divided
by throughput at 1 thread **on the same build**. *(measured)*

**Topology 1 — disjoint: every thread owns its own dict.**

| Threads | GIL wall | GIL speedup | FT wall | **FT speedup** | FT Mops/s |
|---|---|---|---|---|---|
| 1 | 0.220 s | 1.00× | 0.234 s | 1.00× | 12.8 |
| 2 | 0.441 s | 1.00× | 0.238 s | **1.97×** | 25.2 |
| 3 | 0.657 s | 1.00× | 0.239 s | **2.93×** | 37.6 |
| 4 | 0.897 s | 0.98× | 0.258 s | 3.63× | 46.5 |
| 5 | 1.139 s | 0.97× | 0.321 s | 3.64× | 46.7 |
| 8 | 1.733 s | 1.02× | 0.417 s | 4.49× | 57.5 |
| 10 | 2.187 s | 1.01× | 0.487 s | 4.81× | 61.7 |

**Topology 2 — shared read/write: all threads hammer one dict.**

| Threads | GIL wall | GIL speedup | FT wall | **FT speedup** | FT Mops/s |
|---|---|---|---|---|---|
| 1 | 0.219 s | 1.00× | 0.251 s | 1.00× | 11.9 |
| 2 | 0.433 s | 1.01× | 0.992 s | **0.51×** | 6.1 |
| 3 | 0.661 s | 0.99× | 1.671 s | **0.45×** | 5.4 |
| 4 | 0.963 s | 0.91× | 2.206 s | 0.46× | 5.4 |
| 5 | 1.108 s | 0.99× | 2.780 s | 0.45× | 5.4 |
| 8 | 1.764 s | 0.99× | 6.015 s | **0.33×** | 4.0 |
| 10 | 2.210 s | 0.99× | 7.756 s | **0.32×** | 3.9 |

**Topology 3 — shared, READ-ONLY at the Python level: all threads read one list of objects.**

| Threads | GIL wall | GIL speedup | FT wall | **FT speedup** | FT Mops/s |
|---|---|---|---|---|---|
| 1 | 0.113 s | 1.00× | 0.154 s | 1.00× | 19.5 |
| 2 | 0.226 s | 1.00× | 0.158 s | 1.95× | 37.9 |
| 3 | 0.346 s | 0.98× | 0.178 s | 2.59× | 50.5 |
| 4 | 0.452 s | 1.00× | 0.191 s | **3.22×** | 62.7 |
| 5 | 0.577 s | 0.98× | 0.243 s | 3.17× | 61.7 |
| 8 | 0.889 s | 1.02× | 0.371 s | 3.32× | 64.7 |
| 10 | 1.146 s | 0.99× | 0.471 s | 3.27× | 63.7 |

### 7.3 Reading these tables like a staff engineer

**The GIL column is the control, and it is beautiful.** 1.00×, 0.99×, 1.01× — flat as a
board across every topology and every thread count. Adding threads to a CPU-bound workload on
the GIL build does *exactly nothing*, forever. That is the baseline free-threading is trying
to beat, and it is why the disjoint result matters.

**Disjoint scales, and then stops at exactly the number of P-cores.** 1.97× at 2, 2.93× at 3,
then 3.63× at 4 and 3.64× at 5. It does not reach 5× on a 5-P-core machine because thread 5
lands on an efficiency core that is ~1.8× slower (see the provenance block), and because the
main thread and the OS want cycles too. **The bend at 4–5 threads is hardware, not
free-threading.** If you benchmark this on a homogeneous 16-core server you will see a much
cleaner line, and if you report "free-threading only scales to 3.6×" without saying what CPU
you are on, you have written a misleading benchmark.

**Shared read/write scales *negatively*, and this is the headline.** Two threads on one dict
are **half as fast as one thread**. Ten threads are **0.32×** — you have spent ten cores to
get a third of one core's throughput, and you are 3.5× slower than the GIL build doing the
same work. Three mechanisms stack here and you should be able to name all three:

1. **Refcount escape.** The dict's values are created by one thread and touched by all.
   `ob_tid` no longer matches, so every access takes the slow path: an **atomic** RMW on
   `ob_ref_shared`.
2. **Coherence ping-pong.** That atomic forces the cache line into M state on the writing
   core and Invalid everywhere else. Doc 01 §5: "One writer among N readers destroys the
   scaling of all N."
3. **Per-object lock contention.** The dict's internal mutex serialises the mutations, and
   the contended path means futex parks and wakeups, not just spinning.

The GIL was, in effect, a *fair, cheap* version of mechanism 3, with mechanisms 1 and 2
eliminated by fiat. Removing it did not remove the serialisation; it replaced a single
well-tuned global lock with per-object locks *plus* coherence traffic the GIL made
unnecessary.

**Topology 3 is the subtlest and the most important.** The Python code does *nothing but
read*. No mutation, no assignment, no lock needed by any Python-level reasoning. And it caps
at **3.2×**, well short of disjoint's trajectory, and it stops improving at 4 threads.
**Because a read in CPython is a write in hardware** — every `shared[i]` and every `obj.v`
increments and decrements a reference count. Doc 24 §1 calls this "the central perversity of
reference counting on multicore"; here is what it costs you in 2026, on a shipped
interpreter, in a program with no shared mutable state whatsoever.

> **This is the thing to carry out of this entire document.** Under the GIL, the question
> "how much do my threads share?" had one answer: it doesn't matter, you get one core.
> Under free-threading it is *the* design question. Sharing is no longer free at the
> language level, because it was never free at the hardware level — the GIL was just
> charging you for it up front, in a lump sum, whether you shared or not.

### 7.4 Designing around it

The fixes are the ones doc 01 §6 already told you, translated into Python:

- **Shard.** Give each thread its own dict/list/accumulator and merge once at the end. This
  is the disjoint topology and it scales.
- **Make shared data immutable and, where possible, immortal.** Per the HOWTO (verified
  2026-08-02), 3.14's immortalization covers **code constants** (numeric, string and tuple
  literals composed of constants) and **`sys.intern()`-ed strings** — not arbitrary objects
  you would like to be immortal. So this lever is narrower than it sounds.
- **Move shared bulk data out of `PyObject`s.** A `numpy` array, `array.array`, or a
  `bytes`/`memoryview` is **one** refcount for a million elements. This is the single
  highest-leverage change available and it is the same advice as `34-going-native.md`.
- **Pass messages, not objects.** `queue.Queue` of small immutable payloads keeps ownership
  moving rather than shared.
- **Consider whether you wanted processes or subinterpreters** — see
  [`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md).
  Free-threading is not automatically the right answer just because it is the newest one.

### 7.5 The case where free-threading buys you nothing

If your parallelism already comes from a native library that releases the GIL, you were
already scaling. NumPy matrix multiplication in 4 threads *(measured)*:

| | 1 thread | 2 threads | 4 threads |
|---|---|---|---|
| GIL build, numpy 2.5.1 | 0.0394 s | 0.0639 s | **0.1141 s** |
| FT build, numpy 2.5.1 | 0.0486 s | 0.0636 s | **0.1162 s** |

Identical at 2 and 4 threads; the free-threaded build is *slower* at 1 thread. (Caveat: on
macOS the Accelerate BLAS does its own internal threading and ignores `OMP_NUM_THREADS`, so
both builds are saturating the same cores — this measures "already-native-parallel work",
which is exactly the point, but do not read the absolute scaling numbers as BLAS scaling.)

**The rule:** free-threading pays off for **Python-level** CPU work. If your hot loop is
already in C/Rust/BLAS with the GIL released, you have already had free threading since
1995, and switching builds will cost you §3 and §4 for nothing.

---

## 8. Stop-the-world, and what the GIL build actually does

Doc 24 §8.5 states that PEP 703's cycle collector performs **two stop-the-world pauses per
collection**, and calls this "a genuine, new, workload-dependent latency cost the GIL build
does not have". That framing is the standard one. My measurements say it needs qualifying,
and the qualification is interesting.

### 8.1 Is a free-threaded collection really stop-the-world? Yes. Emphatically.

A latency-probe thread increments a counter in a tight loop; the main thread collects a
400,000-node cyclic graph. How far does the counter advance *during* the collection,
relative to how far it would have gone unobstructed? *(measured)*

| Build | `gc.collect()` | counter advanced | expected if unblocked | fraction |
|---|---|---|---|---|
| GIL 3.14.6 | 33.0 ms | 255,725 | 1,598,300 | **16.0%** |
| GIL 3.14.6 | 31.6 ms | 260,092 | 1,611,210 | **16.1%** |
| FT 3.14.6t | 16.1 ms | **1** | 548,401 | **0.0002%** |
| FT 3.14.6t | 25.5 ms | **81** | 558,475 | **0.015%** |

The free-threaded build stopped the other thread **completely** — one loop iteration out of
half a million expected. That is a true stop-the-world, exactly as PEP 703 specifies.

**But look at the GIL row.** The other Python thread made **16% of its free-running progress
during the collection**. Under the GIL, `gc.collect()` is *not* an uninterrupted hold — the
collecting thread yields, repeatedly, and other threads interleave.

> **Honest limit of my model.** I did not chase this to a source line in `Modules/gcmodule.c`
> and I am not going to assert a mechanism I did not verify. The plausible candidates are
> that the collector reaches an eval-breaker check during `delete_garbage` (deallocations can
> re-enter the interpreter), or that untracking/finalizer handling yields. My test objects
> used `__slots__` and had no `__del__`, so the obvious "it ran a Python finalizer"
> explanation does not apply. **Treat the mechanism as unconfirmed and the observable as
> solid** — I measured it twice with consistent results, and you can reproduce it in Lab 6.
> This is the kind of thing that separates rung 4 from rung 5 in README §14: knowing which
> half of your claim is measured.

### 8.2 The pause cost does not scale the way you would guess

Median `gc.collect()` duration on a 300,000-node cyclic graph, as a function of how many
*other* threads are spinning *(measured, 7 trials each, median / max)*:

| Spinning threads | GIL build median | GIL build max | FT build median | FT build max |
|---|---|---|---|---|
| 0 | 17.6 ms | 18.2 ms | 12.4 ms | 17.4 ms |
| 1 | 24.1 ms | 24.4 ms | 11.5 ms | 11.6 ms |
| 2 | 31.3 ms | 32.1 ms | 12.0 ms | 12.3 ms |
| 4 | 49.5 ms | 68.5 ms | 12.2 ms | 12.8 ms |
| 8 | **60.9 ms** | **139.9 ms** | **11.4 ms** | **11.7 ms** |

This is the opposite of the expected result and it is worth sitting with.

- **The free-threaded build is flat.** 11–12 ms regardless of thread count, with a max within
  a millisecond of the median. Its stop-the-world handshake is *cheap and predictable* — this
  is Go-runtime-derived machinery (doc 24 §8.6) and it shows.
- **The GIL build degrades 3.5× and its tail explodes 8×.** At 8 threads the worst collection
  took 140 ms. Because the GIL-build collector yields (§8.1), each yield is a full GIL handoff
  into a queue of 8 contending threads, and the collection's wall time absorbs all of that
  scheduling latency — doc 24 §4's context-switch and futex costs, multiplied by however many
  times it yields.

**The practical conclusion, which contradicts the folklore:** on this machine, at these
thread counts, free-threading did not make GC pauses worse — it made them *shorter and far
more predictable*, while making them *genuinely* stop-the-world. What it removed was the
GIL build's accidental interleaving, which was buying other threads a little progress at the
cost of a much worse tail.

Two caveats before you quote this:

1. My graph is homogeneous and my "other threads" are pure spinners. A workload where threads
   are in long non-yielding native calls is the case where free-threaded stop-the-world hurts:
   **every** thread must reach a safe point before the collection can start, so one
   badly-behaved extension stalls everyone. That case is not in this table.
2. 3.14 and 3.15 are back on the 3.13 generational collector after the incremental GC was
   reverted twice (README §15). Re-measure on your version.

See [`22-garbage-collection.md`](22-garbage-collection.md) §10 for the collector's internals.

---

## 9. Tooling: what works today

All statuses **verified on this machine on 2026-08-02**, macOS arm64, CPython 3.14.6t.

| Tool | Status | Evidence |
|---|---|---|
| `faulthandler` | **works** | `dump_traceback()` printed all threads with correct frames *(measured)* |
| `pdb`, `sys.remote_exec` (PEP 768) | not tested here | — |
| **Thread Sanitizer** | **the primary tool** — see below | — |
| `py-spy` 0.4.2 | **installs** on `cp314t`; runtime **unverified** | `py-spy dump` refused: *"This program requires root on OSX"* — an environmental limit, not a free-threading one. **Unconfirmed as of 2026-08-02.** |
| `memray` 1.19.3 | **installs and runs, but sees almost nothing** — see below | *(measured)* |
| `pytest-run-parallel`, `pytest-freethreaded` | community plugins, listed by the official guide | [py-free-threading.github.io/debugging](https://py-free-threading.github.io/debugging/) |
| `Tachyon` sampling profiler (PEP 799) | ships in **3.15**, has a `--mode gil` | 3.15 what's-new, 2026-08-02 |

### 9.1 The memray finding

Same script, same workload (allocate 2,000,000 `int` objects), profiled under `memray run`
on each build *(measured)*:

| Build | memray "total allocations" | memray "total memory allocated" | memray "peak" | **actual RSS growth** |
|---|---|---|---|---|
| 3.14.6 (GIL) | 380 | 219.2 MB | 81.1 MB | 78.3 MB ✅ matches |
| 3.14.6t (FT) | **11** | **11.0 kB** | **10.4 kB** | **122.9 MB** ❌ |

memray reports the GIL build accurately and reports essentially **nothing** on the
free-threaded build, for a process that grew by 123 MB while it watched. The likely reason is
structural: memray hooks the `PyMem_*` / pymalloc allocation domains, and the free-threaded
build routes *all* object allocation through mimalloc instead (HOWTO: "The free-threaded
build does not use pymalloc and allocates all Python objects using the mimalloc allocator").

**Honest scoping of this claim:** measured on **one machine, one OS (macOS arm64), one memray
version (1.19.3), one CPython (3.14.6t)**. I did not test `--native`, `--follow-fork`, or a
Linux build, and I did not check memray's issue tracker for a known fix. **Do not repeat this
as "memray doesn't support free-threading."** Repeat it as: *on this configuration memray
silently under-reported by four orders of magnitude — verify your memory profiler against a
known-size allocation before you trust it on a free-threaded build.* That verification takes
thirty seconds and it is now part of my checklist.

### 9.2 Thread Sanitizer is the real tool

For C extensions, TSan is not a nice-to-have; it is the only thing that finds the bugs §6
describes. From the community guide (verified 2026-08-02):

- You need a **TSan-instrumented CPython**, not just an instrumented extension.
- CPython maintains a **suppressions file** of its own known races:
  `Tools/tsan/suppressions_free_threading.txt` in the 3.14 branch. Start from it.
- Other projects publish theirs — NumPy (`tools/ci/tsan_suppressions.txt`) and CFFI are named
  in the guide.
- **pytest gotchas that will waste your day:** pytest captures output, so you may see only
  `ThreadSanitizer: reported 2 warnings` with no detail — pass `-s`, or set
  `log_path=` in `TSAN_OPTIONS`. `halt_on_error=1` has caused hangs; try `halt_on_error=0`.
  And **`pytest-xdist` makes TSan output unobtainable entirely** — the guide recommends
  uninstalling it from the TSan environment.

Pair TSan with `faulthandler` and a pytest timeout so a deadlock produces a traceback instead
of a CI job that hangs for an hour.

---

## 10. Ecosystem state, dated

**Every claim in this section is dated. Ecosystem status is the easiest thing in this
document to get wrong, and the fastest thing to rot.**

### 10.1 What I installed and imported, myself, on 2026-08-02

I created a venv on `python3.14t` and installed real packages with `uv 0.11.21`, then
imported each one and checked whether the GIL survived *(measured, 2026-08-02, macOS arm64)*:

| Package | Version installed | Wheel | GIL after import |
|---|---|---|---|
| numpy | 2.5.1 | `cp314t` | **off** ✅ |
| scipy | 1.18.0 | `cp314t` | **off** ✅ |
| pandas | 3.0.5 | `cp314t` | **off** ✅ |
| pyarrow | 25.0.0 | `cp314t` | **off** ✅ |
| lxml | 6.1.1 | `cp314t` | **off** ✅ |
| Pillow | 12.3.0 | `cp314t` | **off** ✅ |
| cffi | 2.1.0 | `cp314t` | **off** ✅ |
| psutil | 7.2.2 | `cp314t` | **off** ✅ |
| pydantic-core | 2.47.0 | `cp314t` (PyO3) | **off** ✅ |
| cryptography | 50.0.0 | `cp314t` (PyO3+CFFI) | **off** ✅ |
| Cython | 3.2.9 | — | **off** ✅ |
| pybind11 | 3.0.4 | — | n/a (headers) |
| nanobind | 2.13.0 | — | **off** ✅ |
| maturin | 1.14.1 | — | n/a (build tool) |
| py-spy | 0.4.2 | — | n/a |
| memray | 1.19.3 | `cp314t` | see §9.1 |
| **orjson** | **3.11.9** | **BUILD FAILED** | — |

`find ftvenv/lib -name '*.so'` → **213 shared objects, 100% of them tagged `cpython-314t`**
*(measured)*. Not one fell back to a source build against the wrong ABI, and **not one
re-enabled the GIL.**

The single failure: **orjson 3.11.9** had no `cp314t` wheel and its source build via
`maturin.build_wheel` failed on this machine. One data point, one platform, one version —
your mileage will differ, and that is exactly the shape of the remaining risk.

**This is a genuinely strong result.** Two years ago this table would have been mostly red.
The scientific and data stack is *done*. The remaining risk is not "does numpy work" — it is
**the long tail of your own dependency graph**, and the only way to know is to try it, which
took me under five minutes.

### 10.2 First version with free-threaded support, per the community tracker

From [py-free-threading.github.io/tracking](https://py-free-threading.github.io/tracking/),
**read 2026-08-02**. This is the community compatibility tracker the official HOWTO points
at. It listed **150 projects**. Selected rows — the "first version with support" column:

| Project | First version with support | | Project | First version |
|---|---|---|---|---|
| NumPy | 2.1.0 | | Cython | 3.1.0 |
| SciPy | 1.15.0 | | pybind11 | 2.13 |
| pandas | 2.2.3 | | nanobind | 2.2.0 |
| PyArrow | 18.0.0 | | **PyO3** | **0.23** |
| scikit-learn | 1.6.0 | | cffi | 2.0.0 |
| matplotlib | 3.9.0 | | maturin | 1.7.5 |
| Pillow | 11.0.0 | | setuptools | 69.5.0 |
| cryptography | 46.0.0 | | meson-python | 0.16.0 |
| pydantic | 2.11.0 | | scikit-build-core | 0.9.5 |
| aiohttp | 3.13.0 | | Numba | 0.65.0 |
| SQLAlchemy | 2.0.45 | | mypyc | 1.20.0 |
| uvloop | 0.22.1 | | Boost.Python | 1.91.0 |
| **polars** | *(blank)* | | **psycopg** | *(blank)* |

**Unconfirmed as of 2026-08-02:** the tracker's "Tested in CI" and "PyPI release" columns
render as icons that I could not reliably extract programmatically, so I am reporting only
the version column, which is unambiguous. **polars** and **psycopg** showed a blank "first
version" cell — that means *the tracker has no recorded supported version*, which is not the
same as "no support"; check
[py-free-threading.github.io/tracking](https://py-free-threading.github.io/tracking/) directly.

### 10.3 The wheel situation on PyPI

Hugo van Kemenade's [free-threaded-wheels tracker](https://hugovk.github.io/free-threaded-wheels/)
covers "the top 360 most-downloaded packages with extensions on PyPI", colour-coded by
whether they ship a `t`-tagged wheel. **I could not extract a current count: the page renders
its list with JavaScript and the served HTML contains no data.** *(attempted 2026-08-02.)*
**Unconfirmed as of 2026-08-02 — open the tracker in a browser for the live number.** I am
not going to invent a percentage, and you should distrust any document that quotes one
without a date.

Both trackers are linked from the official HOWTO, which is the strongest signal available
that they are the canonical sources.

### 10.4 Binding generators: the leverage point

The clearest ecosystem signal in the whole research pass came from PEP 803's motivation
section (read 2026-08-02): initial testing with the experimental `_Py_OPAQUE_PYOBJECT` flag
"indicates that **PyO3, CFFI, and Cython** will all work with PEP 803". PyO3's maintainer
David Hewitt is quoted in support.

**Why that matters strategically:** if you write your extension in Cython, PyO3, CFFI or
nanobind, you inherit free-threading support and eventually `abi3t` support from your
binding layer rather than porting hand-written C-API code yourself. Hand-rolled C extensions
are the ones that will still be blocking migrations in 2028. If you own one and you have been
looking for a reason to port it to a binding generator, this is it.

---

## 11. An honest decision framework

Free-threading is officially supported. That is not the same as "you should use it." Here is
how I would actually decide in 2026.

### 11.1 Move now if all of these are true

- **Your bottleneck is Python-level CPU work in one process.** Not I/O (asyncio already wins),
  not native code that already releases the GIL (§7.5), not database latency.
- **Your threads work on mostly disjoint object graphs.** Per-request state, per-shard
  accumulators, per-task data. If they share one big cache or one big model dict, read §7.2's
  second table again and price it.
- **You measured the single-thread tax on your workload and can afford it.** Not the 1%
  headline — *your* number (§3). Budget 5–25% and be pleasantly surprised.
- **You measured the RSS increase and it fits your limits.** Especially if your heap is
  scalar-heavy (§4.3).
- **Every C extension in your dependency tree opts in**, verified by actually importing them
  and checking `sys._is_gil_enabled()` (§10.1), not by reading a compatibility table.
- **You have a way to detect the GIL being re-enabled in production** (§6.3).

### 11.2 Do not move if any of these are true

- **Your parallelism is already native.** BLAS, Arrow, a Rust core, a database driver. You get
  the taxes and none of the benefit.
- **You are I/O-bound.** `asyncio` (docs 28–29) or threads-on-the-GIL-build already work. The
  GIL is not your problem; see doc 24 §5 for what actually is.
- **`multiprocessing` already works and your data is naturally partitioned.** Processes give
  you fault isolation and memory-limit isolation for free. Free-threading gives you shared
  memory you may not want. See [`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md).
- **You run many short-lived processes.** The startup tax (§3.5, +17–26%) is paid per
  invocation and there is nothing to amortise it against.
- **You have one hand-written C extension you cannot change.** It will re-enable the GIL and
  you will pay every tax for zero benefit (§6.2).
- **You are memory-constrained and scalar-heavy.** +37% baseline, +74% on int-heavy heaps
  (§4.3). RSS is usually the binding constraint in containers, not CPU.
- **Your team cannot yet reason about cache coherence.** That is not a slight — it is a
  scheduling statement. Do Tier 0 first. Free-threading turns docs 01–03 from background
  reading into on-call knowledge.

### 11.3 What every team should do regardless

Even if you are years away from deploying it:

1. **Add `python3.14t` to CI as a non-blocking job.** Your test suite becomes a race detector
   (§5.2). This costs one CI lane and it will find real bugs in your *current*, GIL-build
   production code.
2. **Add `-W error::RuntimeWarning`** to that job so a non-opted-in extension fails loudly
   rather than silently degrading (§6.3).
3. **If you ship a library, publish `cp314t` wheels.** cibuildwheel and manylinux support the
   `t` suffix. You are one config line from unblocking your users, and the ecosystem tables
   in §10 got green because thousands of maintainers did exactly this.
4. **If you maintain a hand-written C extension, plan the move to a binding generator**
   (§10.4). That is where `abi3t` and future ABI work will land first.

### 11.4 The migration order that works

```
  1. Run the existing test suite on python3.14t, GIL on (PYTHON_GIL=1).
     └─ Isolates "does my code build/import" from "is my code thread-safe".

  2. Run it again with the GIL off. Fix what breaks.
     └─ Most breakage here is YOUR pre-existing races (§5), surfaced.

  3. Import every dependency and assert sys._is_gil_enabled() is False.
     └─ This is the go/no-go gate. One offender blocks everything (§6.2).

  4. Measure single-thread wall time and peak RSS, both builds, your workload.
     └─ §3 and §4. If the taxes are unaffordable, stop here — you learned it cheaply.

  5. Measure thread scaling on YOUR object graph, 1..N threads.
     └─ §7. This is where you find out whether you have a disjoint workload
        or a shared one. It is the number the decision actually turns on.

  6. Only now: deploy to a canary with the gil_enabled gauge shipping (§6.3).
```

Steps 1–3 cost a day and eliminate most of the risk. Steps 4–5 cost another day and produce
the artifact that makes the decision for you. **The expensive mistake is skipping to step 6.**

---

## 12. Lab exercises

Reading this document leaves you at **rung 3** of README §14 — fluent, and one "why?" from
collapsing. Every lab below moves you to rung 4 (*built or broke it and measured*). The
labs marked **rung 5** ask you to predict *before* measuring and then say where your model
was wrong; that is the actual bar.

1. **Reproduce the single-thread tax on your hardware.** Write six benchmarks: one
   float-heavy, one call-heavy, one attribute-heavy, one allocation-heavy, one string-heavy,
   one C-library-heavy. Run them A/B/B/A alternating on both builds, min-of-3, and report a
   geomean plus the run-to-run spread. *Proves:* the "1% to 8%" range is a property of the
   *suite*, not the interpreter — and you will find at least one benchmark that is faster on
   the free-threaded build. **Rung 5:** predict the sign of each benchmark's delta before you
   run it, then explain every one you got wrong.

2. **Verify the build pair before you trust any of it.** Print `CONFIG_ARGS`,
   `sys._jit.is_enabled()`, `Py_DEBUG`, `WITH_MIMALLOC` and the compiler for both
   interpreters. *Proves:* most published free-threading comparisons are contaminated by a
   JIT, a PGO, or an allocator difference. If your two builds differ in anything but
   `--disable-gil`, your §3 number is fiction. Cross-ref [`31-measurement-methodology.md`](31-measurement-methodology.md).

3. **Build the memory-tax table for your own object mix.** Allocate 1M each of the types your
   service actually holds, measure RSS per object on both builds, then measure whole-process
   peak RSS for one realistic data structure. *Proves:* §4 — non-GC objects +16 B of header
   (≈ +12.8 B of RSS after allocator rounding), containers a wash or slightly cheaper, and
   that "container-heavy" workloads are usually dominated by their scalar leaves. **Then**
   compute what your fleet's RSS bill becomes.

4. **Amplify a race.** Take §5.2's counter test. Run it on the GIL build until you observe a
   *non-zero* loss — record how many attempts that took. Then run it once on the
   free-threaded build. *Proves:* free-threading does not create the race, it changes its
   probability by orders of magnitude. **Then do the real version:** run your team's actual
   test suite under `python3.14t` and open a bug for everything that fails.

5. **Build the extension cliff yourself.** Compile one C file twice — once with
   `PyUnstable_Module_SetGIL(m, Py_MOD_GIL_NOT_USED)`, once without. Import each on the
   free-threaded build and print `sys._is_gil_enabled()` before and after. Then run a
   4-thread CPU benchmark under both. *Proves:* §6 — one macro is worth 2.6× throughput, and
   the only signal is a `RuntimeWarning` your production logging almost certainly discards.
   Finish by making `-W error::RuntimeWarning` part of your CI command line.

6. **Find your sharing wall.** One CPU-bound workload, three topologies: per-thread private
   data, one shared mutable dict, one shared *read-only* list of objects. Sweep 1..2N threads
   on both builds. *Proves:* §7 — near-linear, negative, and capped-at-~3× respectively.
   **Rung 5:** explain why the read-only topology does not scale linearly, in terms of MESI
   states, without using the word "lock". Cross-ref [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §5.

7. **Measure the stop-the-world.** Build a large cyclic graph, run a latency-probe thread,
   and measure how far the probe advances during `gc.collect()` on each build. Then measure
   collection duration as a function of spinning-thread count. *Proves:* §8 — the
   free-threaded pause is genuinely total but *flat and short*, while the GIL build's is
   partial but degrades badly with thread count. **Rung 5:** my explanation for the GIL
   build's 16% leakage is unverified. Go read `Modules/gcmodule.c` and settle it. Cross-ref
   [`22-garbage-collection.md`](22-garbage-collection.md) §10.

8. **The four-way comparison (README Phase 3's capstone deliverable).** One CPU-bound
   workload — a real one from your job, not a microbenchmark — implemented four ways:
   **threads on the GIL build, threads on the free-threaded build, `multiprocessing`, and
   `concurrent.interpreters` (PEP 734)**. Build one table: wall time, total CPU-seconds, peak
   RSS, startup cost, lines of code, and what each approach *cannot* share. *Proves:* the
   whole of Tier 4 at once. Then run it a second time with the threads sharing one large
   object and watch which column collapses. **This single artifact is worth more in an
   interview than any three chapters of reading**, and it is the only honest way to answer
   "should we move?" for your specific system. Cross-ref
   [`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md).

---

## 13. Question bank

Staff level. If you cannot answer from your own model, the section to reread is noted.

1. Free-threading is "officially supported" as of 3.14. What exactly does that phrase mean,
   what does it *not* mean, and what would have to happen for phase III? *(§1.1)*
2. What is `abi3t`, why could there not simply be one stable ABI covering both builds, and
   why is a PEP being Final not the same as it being usable? *(§1.3, §4.1)*
3. Give four different ways to ask "am I running free-threaded?", say which question each one
   actually answers, and describe a production bug that only one of them catches. *(§2.2, §6.3)*
4. The docs say 1% on macOS aarch64 and 8% on x86-64 Linux. I measured 8.1% on macOS aarch64.
   Explain how both can be correct. *(§3.2, §3.3)*
5. Why is the free-threaded overhead *platform-dependent* at all? Name the two mechanisms.
   *(§3.4)*
6. Three of my nine benchmarks were **faster** on the free-threaded build. Give the mechanism.
   *(§3.3)*
7. On the free-threaded build, `object()` grows by 16 bytes but `[]` does not. Explain both
   halves. Then explain why the measured RSS delta is 12.8 bytes, not 16. *(§4.1, §4.2)*
8. Your service's RSS went up 74% after switching builds and your heap is "mostly dicts".
   Diagnose. *(§4.3)*
9. Is `d[k] += 1` atomic on a free-threaded build? Is `lst.append(x)`? State the general rule
   that decides both, and say why the free-threaded column of the atomicity table is identical
   to the GIL column. *(§5.1)*
10. The same buggy counter program loses 0 updates on the GIL build and 73% on the
    free-threaded build. Is this a new bug? What does the answer imply about how you should
    use `python3.14t` even if you never deploy on it? *(§5.2)*
11. You import a third-party package and your p99 latency triples with no code change and no
    deploy. Walk the diagnosis, and say what you would have shipped six months earlier to
    catch it in one second. *(§6.2, §6.3)*
12. Two threads only *read* a shared list of objects. No mutation anywhere. Why does this cap
    at ~3× speedup instead of scaling linearly? Answer in MESI states. *(§7.2, §7.3)*
13. On my machine, 10 threads sharing one dict achieved **0.32×** of single-threaded
    throughput — 3.5× *slower* than the GIL build doing the same work. Name the three
    stacking mechanisms, and say which one the GIL was providing more cheaply. *(§7.3)*
14. A colleague reports "free-threading only scales to 3.6× on 11 cores, it's overhyped."
    What is the first question you ask? *(§7.3, provenance block)*
15. Free-threaded GC is stop-the-world and the GIL build's is not — yet I measured the
    free-threaded pause as *shorter and flatter*. Reconcile these, and name the workload shape
    where the free-threaded pause is genuinely worse. *(§8.1, §8.2)*
16. Your memory profiler reports 11 kB of allocations for a process that grew 123 MB. What do
    you do, and what general lesson does this teach about tooling on a new runtime? *(§9.1)*
17. Give three conditions under which moving to free-threading is *the wrong call* even though
    your code is perfectly thread-safe. *(§11.2, §7.5)*

---

## 14. Sources

**Primary — specifications.** *Verdict: read 703 §Reference Counting and 803 §Specification in
full; skim the rest.*
- [PEP 703 — Making the Global Interpreter Lock Optional in CPython](https://peps.python.org/pep-0703/)
  (Sam Gross). Accepted **Oct 2023**. The design. Covered in depth in [`24-the-gil.md`](24-the-gil.md) §8;
  this document assumes it.
- [PEP 779 — Criteria for supported status for free-threaded Python](https://peps.python.org/pep-0779/)
  (Wouters, Page, Gross). **Status Final; Resolution 16-Jun-2025; Python-Version 3.14**
  *(verified 2026-08-02)*. Short and worth reading in full — it is the document that defines
  what "supported" means, and its acceptance note explicitly lists unfinished work.
- [PEP 803 — "abi3t": Stable ABI for Free-Threaded Builds](https://peps.python.org/pep-0803/)
  (Viktorin, Goldbaum). **Status Final; Created 19-Aug-2025; Resolution 30-Mar-2026;
  Python-Version 3.15; Requires 703, 793, 697** *(verified 2026-08-02)*. **Verdict: the most
  important new document in this space.** Its Motivation section is the best available
  description of the wheel-matrix problem, with real numbers from cryptography and quotes
  from PyO3's and cryptography's maintainers.

**Primary — official HOWTOs.** *Verdict: authoritative, terse, and the "Behavioral changes"
section is the part everyone skips and shouldn't.*
- [Python support for free threading](https://docs.python.org/3/howto/free-threading-python.html)
  *(read 2026-08-02, served as 3.14.6 docs)* — source of the 1%–8% figure, the immortalization
  scope, the QSBR/deferred/per-thread refcounting behaviour, and the `contextvars` and
  warning-filter default changes.
- [C API Extension Support for Free Threading](https://docs.python.org/3/howto/free-threading-extensions.html)
  *(read 2026-08-02)* — `Py_mod_gil`, `PyUnstable_Module_SetGIL`, the borrowed→strong
  reference table, critical sections, the allocation-domain requirement. **Verdict: if you
  own a C extension, this is a checklist, not an article.**
- [Migrating to Stable ABI for free threading (`abi3t`)](https://docs.python.org/3.15/howto/abi3t-migration.html)
  *(read 2026-08-02, 3.15.0b4 docs)* — the step-by-step for PEP 803, including
  `-DPy_TARGET_ABI3T=0x30f0000` for build tools that do not support it yet (which, as of that
  date, is all of them).
- [What's new in Python 3.15](https://docs.python.org/3.15/whatsnew/3.15.html)
  *(read 2026-08-02, 3.15.0b4)* — PEP 803, mimalloc as the default raw allocator, PEP 788's
  soft-deprecation of `PyGILState_*`, PEP 799's `--mode gil` profiler. **No mention of phase
  III.**

**Community — the compatibility trackers.** *Verdict: check these before believing any
support claim, including mine.*
- [py-free-threading.github.io](https://py-free-threading.github.io/) — the community guide
  the official HOWTO points at. Its [tracking page](https://py-free-threading.github.io/tracking/)
  listed **150 projects** *(read 2026-08-02)*; §10.2 reproduces the version column.
- [py-free-threading.github.io/porting](https://py-free-threading.github.io/porting/) and
  [/debugging](https://py-free-threading.github.io/debugging/) and
  [/thread_sanitizer](https://py-free-threading.github.io/thread_sanitizer/) *(read 2026-08-02)*
  — the TSan page in particular contains the pytest/xdist landmines in §9.2 that are in no
  official document.
- [hugovk.github.io/free-threaded-wheels](https://hugovk.github.io/free-threaded-wheels/) —
  top-360 PyPI packages with extensions, colour-coded by `t`-tagged wheel availability.
  **The page is JavaScript-rendered; I could not extract a count on 2026-08-02.** Open it in
  a browser.
- [`Tools/tsan/suppressions_free_threading.txt`](https://github.com/python/cpython/blob/3.14/Tools/tsan/suppressions_free_threading.txt)
  in the CPython 3.14 branch — CPython's own known-races list. Start your suppressions file
  from this.

**Sibling documents in this folder**
- [`24-the-gil.md`](24-the-gil.md) — §1 (coherence physics), §6 (atomicity table), §8 (PEP
  703's five mechanisms), §9 (the cost-model table). **Read before this document, not after.**
- [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §5–6 — MESI, the
  read/write asymmetry, and "true sharing is the same physics and you cannot pad it away".
  §7 of this document is that section applied to `ob_ref_shared`.
- [`16-object-memory-layout.md`](16-object-memory-layout.md) §2, §12 — the original +16-byte
  measurement and mimalloc. §4 here extends it to RSS and corrects one detail of its
  explanation.
- [`22-garbage-collection.md`](22-garbage-collection.md) §10 — the collector this document
  measures in §8.
- [`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md) —
  the alternatives §11 tells you to consider first.
- [`31-measurement-methodology.md`](31-measurement-methodology.md) — why the provenance block
  at the top of this document is longer than most people's entire methodology section.

**What I could not verify, stated plainly.** *(as of 2026-08-02)*
- **py-spy's runtime behaviour on a free-threaded interpreter.** It installs as a `cp314t`
  wheel; `py-spy dump` requires root on macOS, so I could not attach. **Unconfirmed — test it
  on Linux, and check [github.com/benfred/py-spy](https://github.com/benfred/py-spy).**
- **Whether memray's under-reporting (§9.1) is a known, fixed, or platform-specific issue.**
  I measured the symptom on one configuration and did not read memray's issue tracker.
- **The percentage of top-PyPI packages shipping free-threaded wheels** — the tracker is
  JS-rendered (§10.3). I refuse to guess a number.
- **The mechanism behind the GIL build's 16% GC leakage** (§8.1). Measured twice, mechanism
  unconfirmed, candidate explanations given, not chased into `gcmodule.c`.
- **polars and psycopg free-threading status** — blank cells in the tracker mean "no recorded
  version", not "unsupported". Check upstream.
- **Whether orjson's build failure** on `cp314t` is upstream, platform-specific, or a local
  toolchain problem. One data point, one machine.

---

*Next: [`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md)
— the other two answers to the same question, and the only way to complete Lab 8's four-way
table. Then [`31-measurement-methodology.md`](31-measurement-methodology.md), because
everything in this document is a claim about a number, and numbers are the easiest thing in
engineering to produce badly.*
