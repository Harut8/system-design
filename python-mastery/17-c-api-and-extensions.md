# 17 — The C API and extensions: ownership, ABI tiers, and the boundary where Python stops

> **Tier 2, doc 17.** Prerequisites: [`14-pyobject-and-types.md`](14-pyobject-and-types.md)
> (`PyObject`, `PyTypeObject`, the `tp_*` slots),
> [`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md) (borrowed vs new vs
> stolen at the *concept* level — this doc makes it a compiler problem),
> [`16-object-memory-layout.md`](16-object-memory-layout.md) §2 (the free-threading header)
> and §4 (the three allocation domains), [`24-the-gil.md`](24-the-gil.md) §3 (what
> `Py_BEGIN_ALLOW_THREADS` is *for*). Feeds into:
> [`26-free-threading.md`](26-free-threading.md), [`34-going-native.md`](34-going-native.md),
> [`44-packaging-and-environments.md`](44-packaging-and-environments.md).
>
> **THESIS: the C API is not "Python with more typing" — it is a contract in which the
> compiler enforces nothing that matters.** Ownership, error state, thread state, and
> allocation domain are all invariants held together by convention and by the reader's
> attention. Get one wrong and the failure surfaces somewhere else entirely: a segfault
> three frames back, a heap corruption 40 ms later, a class that leaks forever, a
> free-threaded build that silently loses 11% of its data. Everything else in this
> document — the ABI tiers, multi-phase init, the buffer protocol, critical sections, the
> binding generators — exists to move one of those invariants from "you remember it" to
> "the machine checks it".

> **Build provenance.** Everything below was built and run on the machine this repo lives
> on: **Apple M3 Pro, macOS 15 (Darwin 25.5.0), arm64**, **Apple clang 21.0.0**, against
> **CPython 3.14.6** (`~/.local/bin/python3.14`) and **CPython 3.14.6 free-threading**
> (`~/.local/bin/python3.14t`), both from `uv`'s python-build-standalone distributions.
> I actually built and imported **five** extension modules for this document:
> `pmx17` (the centerpiece, §6, built for both interpreters), `pmx17_legacy` (the
> anti-pattern, §8), `badmem` (deliberate allocator bugs, §14), `abi3demo` (a real
> Limited-API/`abi3` module, §2), and `calls` (calling-convention microbenchmark, §12).
> Numbers marked *(measured)* came out of those runs. `lldb` output in §14 is a real
> backtrace from a real segfault. Where I read something in the 3.15 docs but could not
> compile it (I have no 3.15 here), it is flagged **(3.15, not compiled here)**.
> **Every C API name in this document was checked** against the headers in this build's
> `include/python3.14/` or against docs.python.org. §17 lists what I could not verify.

## Contents

1. [Why there is a C API, and what it costs](#1-why-there-is-a-c-api-and-what-it-costs)
2. [The four API tiers — with the compiler as witness](#2-the-four-api-tiers--with-the-compiler-as-witness)
3. [PEP 803 (`abi3t`) and PEP 793 (`PyModExport`)](#3-pep-803-abi3t-and-pep-793-pymodexport)
4. [Reference semantics: new, borrowed, stolen](#4-reference-semantics-new-borrowed-stolen)
5. [Error handling: the thread-state exception indicator](#5-error-handling-the-thread-state-exception-indicator)
6. [The complete extension module](#6-the-complete-extension-module)
7. [Defining types in C: static vs heap](#7-defining-types-in-c-static-vs-heap)
8. [Multi-phase init, per-module state, and subinterpreters](#8-multi-phase-init-per-module-state-and-subinterpreters)
9. [The buffer protocol (PEP 3118)](#9-the-buffer-protocol-pep-3118)
10. [Releasing the GIL — the exact contract, measured](#10-releasing-the-gil--the-exact-contract-measured)
11. [Free-threading rules for extensions](#11-free-threading-rules-for-extensions)
12. [Calling protocols: `tp_call`, vectorcall, `METH_FASTCALL`](#12-calling-protocols-tp_call-vectorcall-meth_fastcall)
13. [The binding-generator landscape, compared honestly](#13-the-binding-generator-landscape-compared-honestly)
14. [Debugging native extensions](#14-debugging-native-extensions)
15. [Lab exercises](#15-lab-exercises)
16. [Question bank](#16-question-bank)
17. [Sources](#17-sources)

---

## 1. Why there is a C API, and what it costs

CPython is a C program. Its "public API" is the set of C declarations in `Include/` that
an external `.so`/`.dylib`/`.pyd` may call. Historically that meant: *almost all of it*.
`PyObject` was a struct you could dereference. `PyTypeObject` was a struct you could
statically allocate and fill in. `ob_refcnt` was a field you incremented with a macro that
expanded to `++`.

That decision bought Python the scientific stack. NumPy, SciPy, lxml, Pillow, psycopg,
cryptography, PyTorch — every one of them exists because the C API was permissive enough
to be *fast* at the boundary. It is the single largest reason Python won.

It also priced every subsequent runtime change:

| Runtime change | What the C API cost it |
|---|---|
| Removing the GIL | The whole Gilectomy (see [`24-the-gil.md`](24-the-gil.md) §7). Refcounting is *in the ABI*. |
| Moving objects (compacting GC) | Impossible. Extensions hold raw `PyObject*` across arbitrary code. |
| Changing `PyObject` layout | Breaks every statically-allocated type and every `PyObject_HEAD` struct. |
| Subinterpreters | Blocked for 20 years by module-level C globals (§8). |
| A JIT with unboxed values | Every `PyObject*` that crosses the boundary must be materialized. |

The last decade of C-API work — PEP 384, 489, 573, 590, 630, 687, 689, 697, 793, 803 —
is one long, coordinated effort to *narrow* the contract without breaking the ecosystem.
You cannot understand why the modern API looks the way it does without holding that frame.
Almost every "why is this so verbose now?" has the same answer: **because the old, terse
version exposed a memory layout, and CPython needs to change that layout.**

---

## 2. The four API tiers — with the compiler as witness

There are four tiers, and mixing them up is the most common source of packaging pain.

```
 ┌───────────────────────────────────────────────────────────────────────────────┐
 │  INTERNAL API                     Include/internal/pycore_*.h                  │
 │  _PyInterpreterState_GET, _PyDict_GetItem_KnownHash, pycore_critical_section.h │
 │  Requires Py_BUILD_CORE. No stability at all — changes in point releases.      │
 │  If you `#define Py_BUILD_CORE` in a third-party extension, you are on your own│
 └───────────────────────────────────────────────────────────────────────────────┘
                                      ▲  not for you
 ┌───────────────────────────────────────────────────────────────────────────────┐
 │  UNSTABLE API                     PyUnstable_*  (PEP 689, 3.12)                │
 │  PyUnstable_Module_SetGIL, PyUnstable_Object_ClearWeakRefsNoCallbacks,         │
 │  PyUnstable_Eval_RequestCodeExtraIndex ...                                     │
 │  Public. Documented. MAY CHANGE IN MINOR RELEASES (3.14 → 3.15) WITHOUT        │
 │  DEPRECATION. The `PyUnstable_` prefix is the whole point: it is a name you    │
 │  can grep the ecosystem for.                                                   │
 └───────────────────────────────────────────────────────────────────────────────┘
                                      ▲  use, but pin your CPython
 ┌───────────────────────────────────────────────────────────────────────────────┐
 │  FULL / "CPython" API             Include/*.h + Include/cpython/*.h            │
 │  Everything else. Source-compatible across minor releases (with deprecation),  │
 │  but NOT binary-compatible: you rebuild for every 3.x.                         │
 │  Wheel tag: cp314-cp314-macosx_11_0_arm64                                      │
 └───────────────────────────────────────────────────────────────────────────────┘
                                      ▲  where 95% of extensions live
 ┌───────────────────────────────────────────────────────────────────────────────┐
 │  LIMITED API                      #define Py_LIMITED_API 0x030B0000            │
 │    ↓ compiles to ↓                                                             │
 │  STABLE ABI  (abi3)               PEP 384, 3.2+                                │
 │  One binary loads on 3.11, 3.12, 3.13, 3.14, ... GIL builds.                   │
 │  Wheel tag: cp311-abi3-macosx_11_0_arm64     File: mymod.abi3.so               │
 │  Cost: no struct access, no static types, no macros that dereference.          │
 └───────────────────────────────────────────────────────────────────────────────┘
                                      ▲  ~1% of extensions, and they know why
 ┌───────────────────────────────────────────────────────────────────────────────┐
 │  STABLE ABI FOR FREE-THREADING (abi3t)   PEP 803, 3.15  ← NEW, see §3          │
 │  #define Py_TARGET_ABI3T ; PyObject becomes fully opaque.                      │
 │  Wheel tag: abi3t (or abi3.abi3t for both)   File: mymod.abi3t.so              │
 └───────────────────────────────────────────────────────────────────────────────┘
```

Two vocabulary points people get wrong constantly:

- **Limited API is a compile-time thing. Stable ABI is a link/load-time thing.** You opt
  into the *Limited API* (a subset of declarations) in order to produce a binary that
  conforms to the *Stable ABI*. PEP 803 explicitly calls the `Py_LIMITED_API` name
  "increasingly a misnomer", since for things like `Py_TYPE` the macro doesn't remove the
  API, it *selects a forward-compatible implementation* (a real DLL function call rather
  than an inline pointer dereference). That is why the new knob is named
  `Py_TARGET_ABI3T` — a compilation *target*, not a limitation.
- **`abi3` is a floor, not a version.** `Py_LIMITED_API 0x030B0000` means "3.11 and later".
  You get 3.11's subset, and your one wheel loads on everything from 3.11 up.

### The compiler as witness — measured

Talk is cheap. I took the module from §6 — ordinary, modern, full-API C — and compiled it
against two Limited API floors. *(measured)*

```console
$ clang -DPy_LIMITED_API=0x030A0000 ... -c pmx17.c
pmx17.c:64:5: error: use of undeclared identifier 'Py_buffer'
pmx17.c:69:40: error: use of undeclared identifier 'PyBUF_SIMPLE'
pmx17.c:69:9: error: call to undeclared function 'PyObject_GetBuffer'
...  (20 errors)

$ clang -DPy_LIMITED_API=0x030D0000 ... -c pmx17.c
pmx17.c:116:9:  error: call to undeclared function 'PyTuple_SET_ITEM'
pmx17.c:206:56: error: incomplete definition of type 'PyTypeObject'
pmx17.c:223:7:  error: incomplete definition of type 'PyTypeObject'
pmx17.c:248:5:  error: call to undeclared function 'Py_BEGIN_CRITICAL_SECTION'
...  (7 errors)
```

Read the delta. Between a 3.10 floor and a 3.13 floor, the entire buffer protocol became
available (it entered the Limited API in 3.11) — **20 errors down to 7**. Every remaining
error names exactly one of the Limited API's three real constraints:

1. `PyTuple_SET_ITEM` — an **unchecked macro that dereferences the struct**. Limited API
   gives you `PyTuple_SetItem` (a function call, with bounds checking) instead.
2. `incomplete definition of type 'PyTypeObject'` — the struct is **opaque**. You cannot
   write `type->tp_alloc(...)`; you must call `PyType_GetSlot(type, Py_tp_alloc)`.
   This is why static types are impossible under the Limited API.
3. `Py_BEGIN_CRITICAL_SECTION` — free-threading primitives are **not in the 3.14 Limited
   API**. Which is a nice segue, because as of 3.14 the free-threaded build does not
   support the Limited API *at all*.

### Two builds, one binary — and where it breaks

I built a small module (`abi3demo.c`) with `#define Py_LIMITED_API 0x030B0000`, produced
`abi3demo.abi3.so`, and pointed both interpreters at it. *(measured)*

```console
$ python3.14  -c "import abi3demo; print(abi3demo.__file__, abi3demo.whoami())"
3.14  -> abi3demo.abi3.so {'compiled_against': '0x30e06f0', 'running_on': '0x30e06f0'}

$ python3.14t -c "import abi3demo; print(abi3demo.whoami())"
SystemError: init function of abi3demo returned uninitialized object
```

**That is not a bug in my module.** Look at what the free-threaded interpreter advertises:

```console
$ python3.14t -c "import importlib.machinery as m; print(m.EXTENSION_SUFFIXES)"
['.cpython-314t-darwin.so', '.abi3.so', '.so']
```

It *offers to load* `.abi3.so`, then fails. And it cannot possibly succeed, because the
free-threaded headers refuse to compile the Limited API at all:

```c
/* python3.14t/Include/Python.h, lines 50-53 — verbatim from this build */
// gh-111506: The free-threaded build is not compatible with the limited API
// or the stable ABI.
#if defined(Py_LIMITED_API) && defined(Py_GIL_DISABLED)
#  error "The limited API is not currently supported in the free-threaded build"
#endif
```

The mechanism is the object layout. I compiled the same three-line program against both
sets of headers *(measured)*:

| | GIL build 3.14.6 | free-threaded 3.14.6 |
|---|---|---|
| `sizeof(PyObject)` | **16** | **32** |
| `sizeof(PyModuleDef)` | **104** | **120** |
| `offsetof(PyModuleDef, m_name)` | **40** | **56** |

That first row is [`16-object-memory-layout.md`](16-object-memory-layout.md) §2's +16-byte
tax, now visible at the C level rather than through `sys.getsizeof`. And while I was in
there I resolved the caveat that document flagged — here is the **actual** free-threaded
`struct _object` from `python3.14t/Include/object.h`, which has three fields doc 16's
sketch omitted:

```c
struct _object {
    uintptr_t  ob_tid;         /* owning thread id, or 0 (unowned/immortal/merged) */
    uint16_t   ob_flags;
    PyMutex    ob_mutex;       /* per-object lock — one byte */
    uint8_t    ob_gc_bits;     /* gc state, since there is no PyGC_Head */
    uint32_t   ob_ref_local;   /* non-atomic, owner only */
    Py_ssize_t ob_ref_shared;  /* atomic, everyone else */
    PyTypeObject *ob_type;
};
```

The `PyModuleDef` rows are the *real* killer, and they are the direct motivation for
PEP 793. A `PyModuleDef` **is a `PyObject`** — it begins with `PyModuleDef_HEAD_INIT`.
Virtually every extension allocates one **statically**. A statically-allocated `PyObject`
has its header baked into your `.so` at compile time at compile-time offsets. Move the
fields and that static object is garbage on the other build — hence `SystemError: init
function returned uninitialized object`. One statically allocated struct is the entire
reason "one wheel for both builds" was impossible.

### Symbol counts

`nm -u` on the two builds *(measured)*:

```console
$ nm -u abi3demo.abi3.so | grep Py          # Limited API build
_PyModuleDef_Init
_Py_BuildValue
_Py_Version                                  # 3 undefined symbols

$ nm -u pmx17.cpython-314-darwin.so | grep Py | tail -4   # full API build
__Py_Dealloc
__Py_FalseStruct
__Py_NoneStruct
__Py_TrueStruct                              # 26 undefined symbols
```

The leading double underscore on `__Py_NoneStruct` (one from the Mach-O `_` prefix, one
real) tells the story: the full-API build links against **CPython's private data symbols**.
`Py_None` is a macro for `&_Py_NoneStruct`. `Py_DECREF` inlines a call to `_Py_Dealloc`.
Those are the exact things the Limited API hides behind function calls so that CPython can
change them. **The Stable ABI's cost is one indirect call per operation; its benefit is
that CPython can move the furniture.** See [`04-binary-abi-and-linking.md`](04-binary-abi-and-linking.md)
for the general form of this trade.

---

## 3. PEP 803 (`abi3t`) and PEP 793 (`PyModExport`)

This is the frontier as of Aug 2026, and it is the answer to §2's dead end.

### PEP 803 — verified

> **PEP 803 — "abi3t": Stable ABI for Free-Threaded Builds.**
> Authors: Petr Viktorin, Nathan Goldbaum. **Status: Final.** Standards Track.
> Requires PEP 703, 793, 697. Created 19-Aug-2025. **Python-Version: 3.15.**
> Resolution: **30-Mar-2026.** — *(verified against peps.python.org/pep-0803/, 2026-08-02)*

So the lead was right on both the number and the substance. Specifics, from the PEP text:

- A new stable ABI, `abi3t`. Extensions built for `abi3t` 3.*x* are compatible with
  **free-threading** builds of CPython 3.*x* and above — mirroring `abi3`'s promise for
  GIL-enabled builds.
- Opt in with **`Py_TARGET_ABI3T`**, deliberately *not* named `Py_LIMITED_API` (see §2).
- The limited API for free-threaded builds is a **subset of the 3.15 Limited API**.
- **`PyObject` becomes fully opaque.** You do not write `PyObject_HEAD` in your struct.
  Instance data is reached through `PyObject_GetTypeData()` with the defining class, which
  you obtain either from a `PyCMethod`-signature method's `defining_class` argument or via
  a `Py_tp_token` + `PyType_GetBaseByToken()`. This is PEP 697 ("Limited C API for
  Extending Opaque Types", Final, 3.12) being cashed in.
- **Wheel/filename tags**: ABI tag `abi3t`; filename `mymod.abi3t.so`. The PEP recommends
  building for both and tagging `abi3.abi3t`.
- The suffix lists change, and this closes §2's hole (quoting the PEP, for a Linux build):
  - `python3.15`: `['.cpython-315-x86_64-linux-gnu.so', '.abi3.so', '.abi3t.so', '.so']`
  - `python3.15t`: `['.cpython-315-x86_64-linux-gnu.so', '.abi3t.so', '.so']`

  **Free-threaded 3.15 will no longer offer to load `.abi3.so`** — which is exactly the
  failure I reproduced in §2 on 3.14t. GIL-enabled builds *will* load `.abi3t.so`; the PEP
  is candid that this "breaks the conceptual purity of `abi3` and `abi3t` being separate
  ABIs" for practical reasons.
- Version detection changes too: `PY_VERSION_HEX` no longer tells you what you're running
  on. Use **`Py_Version`** (runtime) and `Py_TARGET_ABI3T` / `Py_LIMITED_API` (compile
  time). My §2 experiment shows why this matters — `abi3demo` reported
  `compiled_against == running_on == 0x30e06f0`, because `PY_VERSION_HEX` is just the
  header's version, not the ABI floor.

### PEP 793 — the piece that makes it possible

> **PEP 793.** Petr Viktorin. **Status: Final.** Standards Track. Created 23-May-2025.
> **Python-Version: 3.15.** Resolution: **23-Oct-2025.** — *(verified 2026-08-02)*

PEP 793 adds a **new module export hook**, `PyModExport_<name>`, which returns an array of
module slots directly — **no `PyModuleDef` at all**. From the PEP's own abstract: this
"allows extension authors to avoid using a statically allocated `PyObject`, lifting the
most common obstacle to making one compiled library file usable with both regular and
free-threaded builds of CPython." That is precisely the 104-vs-120-byte `PyModuleDef`
problem I measured in §2.

New slot IDs replace the `PyModuleDef` fields (confirmed present in the 3.15
`c-api/module.html` docs, all marked *Part of the Stable ABI since version 3.15*):
`Py_mod_name`, `Py_mod_doc`, `Py_mod_methods`, `Py_mod_state_size`, `Py_mod_token`, plus
`PyModule_FromSlotsAndSpec()` for dynamic creation. The classic `PyInit_*` hook is
**soft-deprecated**: still supported, still documented, but no new features.

`Py_mod_token` deserves a note, because it replaces a pattern you will see everywhere in
older code. The old way to ask "is this module mine?" was `PyModule_GetDef(module) ==
&my_def` — pointer identity on a static struct. With no static struct, you instead declare
`static char my_token;` and compare via `PyModule_GetToken()` / `PyType_GetModuleByToken()`.
Same idea (a unique address), no `PyObject` required.

> **(3.15, not compiled here.)** I read the 3.15.0b4 `abi3t-migration` HOWTO and
> `c-api/module.html`, but I have only 3.14.6 locally, so none of §3's code shapes were
> compiled. The 3.15 HOWTO also shows a `PySlot` / `PySlot_STATIC_DATA(...)` / `PySlot_END`
> spelling for slot arrays that I have not seen elsewhere and cannot verify against a
> header. **Treat §3 as read, not run.** Everything in §2 and §4–§14 was executed.

### The practical decision, today

| You are | Do this |
|---|---|
| Shipping a pure C extension, care about wheel count | Target `abi3` today; port to `abi3t` when 3.15 is final and you can drop <3.12 |
| Using Cython / PyO3 / pybind11 / nanobind | **Wait for your generator.** The 3.15 HOWTO says so explicitly. PyO3 0.29 already has `abi3t` features (§13) |
| Needing peak performance at the boundary | Full API, one wheel per version. This is what NumPy does |
| Needing free-threading on 3.14 | Full API only. Limited API is a compile error there |

---

## 4. Reference semantics: new, borrowed, stolen

This is the invariant that kills people, so it gets a diagram.

Every `PyObject*` crossing a function boundary carries one of three ownership contracts,
and **none of them is expressed in the C type system**. `PyObject *` is `PyObject *` in all
three cases. The contract lives in the documentation and in your head.

```
  ╔═══════════════════════════════════════════════════════════════════════════════╗
  ║  OWNERSHIP FLOW THROUGH ONE C FUNCTION                                        ║
  ╚═══════════════════════════════════════════════════════════════════════════════╝

   static PyObject *f(PyObject *module, PyObject *arg)
                          │             │
     BORROWED ────────────┘             └──────── BORROWED
     (module outlives the call;         (the caller's stack holds a ref for the
      you may read, must not             whole call; safe to read, NOT safe to
      DECREF)                            stash in a struct without Py_INCREF)
        │
        │   PyObject *tmp = PyLong_FromLong(42);        ┌── NEW REFERENCE ──┐
        │   ────────────────────────────────────────────┤  you own it       │
        │                                               │  you must dispose │
        │                                               └───────┬───────────┘
        │                                                       │
        ├── (a) return it ─────────────────────────────▶ ownership TRANSFERS
        │                                                 to the caller. done.
        │
        ├── (b) PyTuple_SET_ITEM(t, 0, tmp);  ─────────▶ STOLEN. the tuple now
        │       PyList_SET_ITEM(l, 0, tmp);              owns it. Do NOT decref.
        │       PyException_SetCause(e, tmp);            Do NOT touch tmp again.
        │       PyModule_AddObject(m, "x", tmp);  ← steals ONLY ON SUCCESS (!!)
        │
        ├── (c) PyDict_SetItem(d, k, tmp);   ──────────▶ NOT stolen. dict took
        │       PyList_Append(l, tmp);                   its own ref. YOU still
        │       PyModule_AddObjectRef(m,"x",tmp);        owe a Py_DECREF.
        │       ... Py_DECREF(tmp);
        │
        └── (d) something failed ───────────────────────▶ Py_DECREF(tmp);
                                                          return NULL;
                                                          ^^^^^^^^^^^^^ the line
                                                          people forget.

   ┌──────────────────────────────────────────────────────────────────────────┐
   │ THE INVARIANT: at every `return` from this function, the number of        │
   │ Py_INCREFs you performed (including implicit ones from *_New / *_From*)   │
   │ minus the number of Py_DECREFs equals the number of new references you    │
   │ are handing back. Not "usually". At EVERY return, including error ones.   │
   └──────────────────────────────────────────────────────────────────────────┘
```

### The vocabulary, precisely

| Term | Meaning | You must |
|---|---|---|
| **New reference** | The callee incremented the count on your behalf | `Py_DECREF` it, or transfer it |
| **Borrowed reference** | You got a pointer, nobody incremented anything | Nothing — but you may not outlive the owner |
| **Stolen reference** | You gave a function a reference and it took ownership | Nothing. Never touch it again |

Naming heuristics that actually hold up: `*_New`, `*_From*`, `PyObject_Call*`,
`PyObject_GetAttr*`, `PyDict_GetItemRef`, `Py_NewRef` return **new** references.
`PyDict_GetItem`, `PyList_GetItem`, `PyTuple_GetItem`, `PyList_GET_ITEM`, `PyErr_Occurred`,
`PyModule_GetDict`, `PySequence_Fast_GET_ITEM` return **borrowed** ones. The stealing set
is small and worth memorizing outright: `PyTuple_SET_ITEM`, `PyList_SET_ITEM`,
`PyTuple_SetItem`, `PyList_SetItem`, `PyException_SetCause`, `PyException_SetContext`,
`PyErr_SetRaisedException`, and the legacy `PyModule_AddObject`.

> **`PyModule_AddObject` is the worst contract in the C API and you should never use it.**
> It steals your reference **only if it succeeds**. On failure you still own it. Which
> means the correct call site is:
> ```c
> if (PyModule_AddObject(m, "Thing", obj) < 0) { Py_DECREF(obj); return -1; }
> ```
> and approximately nobody writes that. It is deprecated. Use **`PyModule_AddObjectRef`**
> (3.10+, does not steal) or `PyModule_Add` (3.13+). This one function is responsible for a
> genuinely large fraction of historical extension leaks.

### The five macros

```c
Py_INCREF(o)    /* o must not be NULL */
Py_DECREF(o)    /* o must not be NULL; may run arbitrary Python code via __del__ */
Py_XDECREF(o)   /* NULL-safe; the workhorse of error paths and single-exit cleanup */
Py_CLEAR(o)     /* the ONLY correct way to drop a reference held in a struct field  */
Py_NewRef(o)    /* 3.10+: incref and return, so you can write `return Py_NewRef(x);` */
```

**`Py_CLEAR` is not "`Py_XDECREF` plus assign NULL", and the difference is the point.**
It is defined (in `Include/refcount.h`) to *first* set the field to `NULL`, *then* decref
the old value. Order matters because `Py_DECREF` can run `__del__`, which can re-enter your
object and read that same field. If you wrote:

```c
Py_XDECREF(self->cache);   /* __del__ runs here and reads self->cache -> DANGLING */
self->cache = NULL;
```

you have a use-after-free that only fires when the object being dropped has a finalizer and
that finalizer touches your object. Which is to say: never in your tests, once in
production. In `tp_clear` and `m_clear`, **always `Py_CLEAR`**.

### The two classic bugs

**Bug 1 — a borrowed reference outliving its owner.**

```c
/* WRONG */
PyObject *item = PyList_GetItem(list, 0);   /* borrowed */
PyObject *result = PyObject_CallObject(callback, NULL);  /* may mutate `list` */
use(item);   /* `item` may have been freed by list.clear() inside the callback */
```

Anything that can run Python code — a call, a comparison, a `__hash__`, an allocation that
triggers GC, a `Py_DECREF` that runs `__del__` — can invalidate a borrowed reference.
The fix is `PyList_GetItemRef` (3.13+), which returns a strong reference. This bug is
**latent under the GIL and immediate under free-threading**; see §11.

**Bug 2 — leaking on the error path.** The one my §6 module is structured to avoid:

```c
/* WRONG — three leaks hiding in plain sight */
PyObject *a = PyLong_FromLong(1);
PyObject *b = PyLong_FromLong(2);
if (b == NULL) return NULL;            /* leaks a */
PyObject *c = PyLong_FromLong(3);
if (c == NULL) return NULL;            /* leaks a and b */
return Py_BuildValue("(OOO)", a, b, c);/* leaks a, b, c — BuildValue's "O" increfs! */
```

The discipline that fixes it, and the reason C extensions are full of `goto`:

```c
PyObject *a = NULL, *b = NULL, *c = NULL, *res = NULL;
a = PyLong_FromLong(1); if (a == NULL) goto done;
b = PyLong_FromLong(2); if (b == NULL) goto done;
c = PyLong_FromLong(3); if (c == NULL) goto done;
res = Py_BuildValue("(OOO)", a, b, c);
done:
    Py_XDECREF(a); Py_XDECREF(b); Py_XDECREF(c);
    return res;      /* NULL on any failure, with the exception still set */
```

Initialize everything to `NULL`, one exit label, `Py_XDECREF` everything, return the
result variable. `goto` in C extensions is not sloppiness — it is the language's only
`finally`.

---

## 5. Error handling: the thread-state exception indicator

There are no exceptions in C. CPython emulates them with a **per-thread-state exception
indicator** plus a return-value convention. Both halves must be right.

### The two conventions

| Return type | Success | Failure |
|---|---|---|
| `PyObject *` | non-`NULL` | **`NULL` with an exception set** |
| `int` | `0` (or `>0` for "found") | **`-1` with an exception set** |
| `Py_ssize_t` | `>= 0` | **`-1` with an exception set** |
| `void` | — | cannot fail, or reports via `PyErr_Occurred()` |

The invariant runs in both directions and both violations are real bugs:

- **`NULL`/`-1` without an exception set** → the interpreter raises
  `SystemError: <fn> returned NULL without setting an exception`.
- **A set exception with a success return** → far worse. The exception leaks into an
  unrelated later operation and you get a traceback pointing at innocent code. This is why
  you occasionally see a `ValueError` raised "from" a line that cannot raise it.

### Checking `PyErr_Occurred` correctly

`PyErr_Occurred()` returns a **borrowed** reference to the exception *type*, or `NULL`.
It is the right tool exactly when the return value is ambiguous:

```c
long n = PyLong_AsLong(obj);        /* returns -1 on error... and -1 is a valid long */
if (n == -1 && PyErr_Occurred()) {
    return NULL;
}
```

`PyLong_AsLong`, `PyObject_IsTrue`, `PySequence_Size` — anything whose error sentinel is
also a legal value — needs this two-part check. Getting it wrong gives you a program that
works until someone passes `-1`.

### Setting and chaining

```c
PyErr_SetString(PyExc_ValueError, "chunk size must be positive");
PyErr_Format(PyExc_TypeError, "expected bytes, got %.100s", Py_TYPE(o)->tp_name);
PyErr_SetFromErrno(PyExc_OSError);              /* wraps the C errno */
PyErr_NoMemory();                               /* returns NULL, convenient */
```

**Implicit chaining (`__context__`) is automatic.** If an exception is already set when you
call `PyErr_SetString`, CPython attaches the old one as `__context__` — the same rule as
raising inside an `except:` block in Python. You get it for free.

**Explicit chaining (`raise X from Y`, i.e. `__cause__`) you must do by hand**, with the
modern 3.12+ exception-object API:

```c
PyObject *cause = PyErr_GetRaisedException();     /* NEW ref; clears the indicator  */
PyErr_SetString(st->ChecksumError, "not a checksummable buffer");
PyObject *exc = PyErr_GetRaisedException();       /* NEW ref                        */
PyException_SetCause(exc, cause);                 /* STEALS cause                   */
PyErr_SetRaisedException(exc);                    /* STEALS exc                     */
return NULL;
```

`PyErr_GetRaisedException` / `PyErr_SetRaisedException` (3.12+) replaced the old
`PyErr_Fetch` / `PyErr_Restore` / `PyErr_NormalizeException` triple-pointer dance. The old
API handed you three separate `PyObject*`s (type, value, traceback) that might or might not
be normalized; the new one hands you one exception object. If you are reading pre-3.12
extension code, that triple is what you'll find.

Here is that exact code path, running, from the module in §6 *(measured, identical on both
builds)*:

```console
chained    : ChecksumError | argument is not a checksummable buffer
  __cause__            : TypeError("a bytes-like object is required, not 'int'")
  __context__          : None
  __suppress_context__ : True
```

Note `__suppress_context__` is `True` — `PyException_SetCause` sets it, matching Python's
`raise ... from ...` semantics exactly. I did not assume that; I printed it.

### Exceptions where you cannot raise

Destructors, `tp_dealloc`, `tp_clear`, and callbacks invoked from C code with no error
channel cannot propagate an exception. The correct escape hatch is
**`PyErr_WriteUnraisable(obj)`** or, since 3.13, **`PyErr_FormatUnraisable(fmt, ...)`**,
which routes through `sys.unraisablehook`. Swallowing the error silently is the wrong
answer; so is calling `PyErr_Clear()` and pretending.

---

## 6. The complete extension module

This is the centerpiece. It is real, it compiles clean at `-Wall -Wextra`, and it imports
and runs on both interpreters on this machine. Everything in §4, §5, §7–§12 shows up in it.

### `pmx17.c`

```c
/* pmx17.c — a minimal but *complete* CPython extension module. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <stddef.h>     /* offsetof */

/* --------------------------------------------------------------------- */
/* 1. Per-module state (PEP 573). Everything a C global would have held.  */
/* --------------------------------------------------------------------- */

typedef struct {
    PyObject *ChecksumError;    /* strong ref */
    PyObject *AccumulatorType;  /* strong ref */
} pmx_state;

static inline pmx_state *
get_state(PyObject *module)
{
    void *st = PyModule_GetState(module);
    assert(st != NULL);
    return (pmx_state *)st;
}

/* --------------------------------------------------------------------- */
/* 2. Pure C. No PyObject in sight — this is what runs without the GIL.   */
/* --------------------------------------------------------------------- */

#define FNV_OFFSET 1469598103934665603ULL
#define FNV_PRIME  1099511628211ULL

static uint64_t
fnv1a64(uint64_t h, const unsigned char *p, Py_ssize_t n)
{
    for (Py_ssize_t i = 0; i < n; i++) {
        h ^= (uint64_t)p[i];
        h *= FNV_PRIME;
    }
    return h;
}

/* --------------------------------------------------------------------- */
/* 3. Module-level functions                                             */
/* --------------------------------------------------------------------- */

/* checksum(buf) -> int
 * METH_O: exactly one argument, no tuple is built, no parsing happens. */
static PyObject *
pmx_checksum(PyObject *module, PyObject *arg)
{
    Py_buffer view;
    /* PyBUF_SIMPLE: "give me a flat, C-contiguous block". On failure it has
     * ALREADY set the exception; we return NULL and add nothing. Returning
     * NULL without an exception set is a SystemError (see §5). */
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }

    uint64_t h;
    /* The buffer export pins the memory: the exporter cannot be resized or
     * freed while a Py_buffer is outstanding. That is exactly what makes it
     * safe to touch view.buf with the GIL released. */
    Py_BEGIN_ALLOW_THREADS
    h = fnv1a64(FNV_OFFSET, (const unsigned char *)view.buf, view.len);
    Py_END_ALLOW_THREADS

    PyBuffer_Release(&view);
    return PyLong_FromUnsignedLongLong(h);
}

/* The identical computation with the GIL held. Kept only so the two can be
 * measured against each other — see §10. Never ship this shape. */
static PyObject *
pmx_checksum_gil(PyObject *module, PyObject *arg)
{
    Py_buffer view;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    uint64_t h = fnv1a64(FNV_OFFSET, (const unsigned char *)view.buf, view.len);
    PyBuffer_Release(&view);
    return PyLong_FromUnsignedLongLong(h);
}

/* Helper: Py_ssize_t[] -> tuple, or None if the array is absent. */
static PyObject *
sizes_to_tuple(const Py_ssize_t *arr, int n)
{
    if (arr == NULL) {
        Py_RETURN_NONE;
    }
    PyObject *t = PyTuple_New(n);
    if (t == NULL) {
        return NULL;
    }
    for (int i = 0; i < n; i++) {
        PyObject *v = PyLong_FromSsize_t(arr[i]);
        if (v == NULL) {
            Py_DECREF(t);          /* error path: do not leak the tuple */
            return NULL;
        }
        PyTuple_SET_ITEM(t, i, v); /* STEALS our reference to v */
    }
    return t;
}

/* describe_buffer(obj) -> dict: the full PEP 3118 view. */
static PyObject *
pmx_describe(PyObject *module, PyObject *arg)
{
    Py_buffer v;
    if (PyObject_GetBuffer(arg, &v, PyBUF_FULL_RO) < 0) {
        return NULL;
    }
    PyObject *shape = NULL, *strides = NULL, *subs = NULL, *res = NULL;

    shape = sizes_to_tuple(v.shape, v.ndim);
    if (shape == NULL) { goto done; }
    strides = sizes_to_tuple(v.strides, v.ndim);
    if (strides == NULL) { goto done; }
    subs = sizes_to_tuple(v.suboffsets, v.ndim);
    if (subs == NULL) { goto done; }

    res = Py_BuildValue(
        "{s:n,s:i,s:s,s:n,s:O,s:O,s:O,s:O,s:O,s:O}",
        "len", v.len,
        "ndim", v.ndim,
        "format", v.format ? v.format : "B",
        "itemsize", v.itemsize,
        "readonly", v.readonly ? Py_True : Py_False,
        "shape", shape,
        "strides", strides,
        "suboffsets", subs,
        "c_contiguous", PyBuffer_IsContiguous(&v, 'C') ? Py_True : Py_False,
        "f_contiguous", PyBuffer_IsContiguous(&v, 'F') ? Py_True : Py_False);

done:
    /* Single exit. Every temporary is released exactly once whether we got
     * here by success or by any of the three failure branches. Py_XDECREF
     * tolerates NULL so the same three lines cover all four cases. */
    Py_XDECREF(shape);
    Py_XDECREF(strides);
    Py_XDECREF(subs);
    PyBuffer_Release(&v);
    return res;
}

/* checksum_strict(buf): like checksum(), but re-raises any failure as this
 * module's own ChecksumError with the original attached as __cause__. */
static PyObject *
pmx_checksum_strict(PyObject *module, PyObject *arg)
{
    PyObject *result = pmx_checksum(module, arg);
    if (result != NULL) {
        return result;
    }
    PyObject *cause = PyErr_GetRaisedException();   /* new ref */
    pmx_state *st = get_state(module);
    PyErr_SetString(st->ChecksumError, "argument is not a checksummable buffer");
    PyObject *exc = PyErr_GetRaisedException();     /* new ref */
    PyException_SetCause(exc, cause);               /* STEALS cause */
    PyErr_SetRaisedException(exc);                  /* STEALS exc */
    return NULL;
}

static PyMethodDef pmx_methods[] = {
    {"checksum",        pmx_checksum,        METH_O,
     "checksum(buf) -> int -- FNV-1a 64 over any buffer, GIL released."},
    {"checksum_gil",    pmx_checksum_gil,    METH_O,
     "checksum_gil(buf) -> int -- identical, but holds the GIL. For §10 only."},
    {"checksum_strict", pmx_checksum_strict, METH_O,
     "checksum_strict(buf) -> int -- raises ChecksumError from the original."},
    {"describe_buffer", pmx_describe,        METH_O,
     "describe_buffer(obj) -> dict -- the raw Py_buffer fields."},
    {NULL, NULL, 0, NULL}
};

/* --------------------------------------------------------------------- */
/* 4. A heap type                                                        */
/* --------------------------------------------------------------------- */

typedef struct {
    PyObject_HEAD
    uint64_t   hash;
    Py_ssize_t nbytes;
} AccumulatorObject;

static PyObject *
Accumulator_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    AccumulatorObject *self = (AccumulatorObject *)type->tp_alloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    self->hash = FNV_OFFSET;
    self->nbytes = 0;
    return (PyObject *)self;
}

static void
Accumulator_dealloc(PyObject *self)
{
    /* The canonical heap-type dealloc. Note the Py_DECREF(tp) at the end:
     * an instance of a heap type owns a reference to its own class, and if
     * you forget this the class leaks forever. Static types don't do this. */
    PyTypeObject *tp = Py_TYPE(self);
    PyObject_GC_UnTrack(self);
    tp->tp_free(self);
    Py_DECREF(tp);
}

static int
Accumulator_traverse(PyObject *self, visitproc visit, void *arg)
{
    /* Required since 3.9 for GC-enabled heap types: the instance->class
     * edge must be visible to the cycle collector. */
    Py_VISIT(Py_TYPE(self));
    return 0;
}

static PyObject *
Accumulator_update(PyObject *self, PyObject *arg)
{
    AccumulatorObject *acc = (AccumulatorObject *)self;
    Py_buffer view;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    /* No-op on the GIL build; a real per-object lock on the free-threaded
     * build. Without it, two threads calling update() concurrently lose
     * bytes: the read-modify-write of acc->hash is not atomic. See §11. */
    Py_BEGIN_CRITICAL_SECTION(self);
    acc->hash = fnv1a64(acc->hash, (const unsigned char *)view.buf, view.len);
    acc->nbytes += view.len;
    Py_END_CRITICAL_SECTION();

    PyBuffer_Release(&view);
    Py_RETURN_NONE;
}

/* The same thing WITHOUT the critical section. Correct under the GIL,
 * silently lossy under free-threading. Exists only to be measured. */
static PyObject *
Accumulator_update_unlocked(PyObject *self, PyObject *arg)
{
    AccumulatorObject *acc = (AccumulatorObject *)self;
    Py_buffer view;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    acc->hash = fnv1a64(acc->hash, (const unsigned char *)view.buf, view.len);
    acc->nbytes += view.len;
    PyBuffer_Release(&view);
    Py_RETURN_NONE;
}

/* METH_FASTCALL: args is a raw C array, nargs is its length. No argument
 * tuple is ever allocated (PEP 590, §12). */
static PyObject *
Accumulator_update_many(PyObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    for (Py_ssize_t i = 0; i < nargs; i++) {
        PyObject *r = Accumulator_update(self, args[i]);
        if (r == NULL) {
            return NULL;     /* args are BORROWED — nothing to release */
        }
        Py_DECREF(r);
    }
    Py_RETURN_NONE;
}

static PyObject *
Accumulator_digest(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    AccumulatorObject *acc = (AccumulatorObject *)self;
    uint64_t h;
    Py_BEGIN_CRITICAL_SECTION(self);
    h = acc->hash;
    Py_END_CRITICAL_SECTION();
    return PyLong_FromUnsignedLongLong(h);
}

static PyMethodDef Accumulator_methods[] = {
    {"update",          Accumulator_update,          METH_O, "update(buf)"},
    {"update_unlocked", Accumulator_update_unlocked, METH_O, "update(buf), no lock"},
    {"update_many", (PyCFunction)(void(*)(void))Accumulator_update_many,
                                             METH_FASTCALL, "update_many(*bufs)"},
    {"digest",      Accumulator_digest,      METH_NOARGS,   "digest() -> int"},
    {NULL, NULL, 0, NULL}
};

static PyMemberDef Accumulator_members[] = {
    {"nbytes", Py_T_PYSSIZET, offsetof(AccumulatorObject, nbytes), Py_READONLY,
     "bytes consumed so far"},
    {NULL, 0, 0, 0, NULL}
};

static PyType_Slot Accumulator_slots[] = {
    {Py_tp_doc,      (void *)"Incremental FNV-1a 64 accumulator."},
    {Py_tp_new,      Accumulator_new},
    {Py_tp_dealloc,  Accumulator_dealloc},
    {Py_tp_traverse, Accumulator_traverse},
    {Py_tp_methods,  Accumulator_methods},
    {Py_tp_members,  Accumulator_members},
    {0, NULL}
};

static PyType_Spec Accumulator_spec = {
    .name = "pmx17.Accumulator",
    .basicsize = sizeof(AccumulatorObject),
    .itemsize = 0,
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_BASETYPE,
    .slots = Accumulator_slots,
};

/* --------------------------------------------------------------------- */
/* 5. Multi-phase init (PEP 489)                                         */
/* --------------------------------------------------------------------- */

static int
pmx_exec(PyObject *module)
{
    pmx_state *st = get_state(module);

    st->ChecksumError = PyErr_NewException("pmx17.ChecksumError", NULL, NULL);
    if (st->ChecksumError == NULL) {
        return -1;
    }
    /* PyModule_AddObjectRef does NOT steal. Its predecessor
     * PyModule_AddObject stole *only on success* — see §4. */
    if (PyModule_AddObjectRef(module, "ChecksumError", st->ChecksumError) < 0) {
        return -1;
    }

    PyObject *t = PyType_FromModuleAndSpec(module, &Accumulator_spec, NULL);
    if (t == NULL) {
        return -1;
    }
    st->AccumulatorType = t;   /* module state takes the strong ref */
    if (PyModule_AddObjectRef(module, "Accumulator", t) < 0) {
        return -1;
    }

#ifdef Py_GIL_DISABLED
    if (PyModule_AddStringConstant(module, "BUILD", "free-threaded") < 0) {
        return -1;
    }
#else
    if (PyModule_AddStringConstant(module, "BUILD", "gil") < 0) {
        return -1;
    }
#endif
    return 0;
}

static int
pmx_traverse(PyObject *module, visitproc visit, void *arg)
{
    pmx_state *st = get_state(module);
    Py_VISIT(st->ChecksumError);
    Py_VISIT(st->AccumulatorType);
    return 0;
}

static int
pmx_clear(PyObject *module)
{
    pmx_state *st = get_state(module);
    Py_CLEAR(st->ChecksumError);
    Py_CLEAR(st->AccumulatorType);
    return 0;
}

static void
pmx_free(void *module)
{
    (void)pmx_clear((PyObject *)module);
}

static PyModuleDef_Slot pmx_slots[] = {
    {Py_mod_exec, (void *)pmx_exec},
    /* "You may load me into more than one interpreter, each with its own
     * GIL." You can only promise this because there are no C globals. */
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
#ifdef Py_mod_gil
    /* "Do not re-enable the GIL on my account." */
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
#endif
    {0, NULL}
};

static struct PyModuleDef pmx_module = {
    .m_base = PyModuleDef_HEAD_INIT,
    .m_name = "pmx17",
    .m_doc = "Doc 17's demonstration extension.",
    .m_size = sizeof(pmx_state),   /* > 0 == per-module state exists */
    .m_methods = pmx_methods,
    .m_slots = pmx_slots,
    .m_traverse = pmx_traverse,
    .m_clear = pmx_clear,
    .m_free = pmx_free,
};

PyMODINIT_FUNC
PyInit_pmx17(void)
{
    /* Multi-phase: return the *definition*, not a module. The import
     * machinery calls create/exec later, possibly more than once. */
    return PyModuleDef_Init(&pmx_module);
}
```

### Building it — macOS specifics

```console
$ PY=~/.local/bin/python3.14
$ clang -O2 -Wall -Wextra -Wno-unused-parameter \
        -shared -undefined dynamic_lookup \
        $($PY-config --includes) \
        -o pmx17$($PY -c "import sysconfig;print(sysconfig.get_config_var('EXT_SUFFIX'))") \
        pmx17.c
```

Four macOS/arm64 notes, because this is where cross-platform build scripts break:

1. **`-undefined dynamic_lookup`.** A Python extension on macOS is a *bundle* that
   references `Py*` symbols supplied by whichever `python` binary loads it. Historically
   you passed `-undefined dynamic_lookup` to tell the linker "these will resolve at load
   time". Apple's new linker (`ld_prime`, Xcode 15+) deprecates the flag and it emits a
   warning on some toolchains — **on Apple clang 21.0.0 with this SDK it compiled clean,
   no warning** *(measured)*. The forward-compatible alternative is
   `-Wl,-undefined,dynamic_lookup` or linking directly against `libpython3.14.dylib`
   (present in this distribution at `lib/libpython3.14.dylib`). On Linux you use
   `-shared -fPIC` and undefined symbols are fine by default.
2. **The extension suffix is not `.so` plus guesswork.** Ask sysconfig. Here they are
   `.cpython-314-darwin.so` and `.cpython-314t-darwin.so` *(measured)* — note the `t`, which
   is the free-threaded ABI tag from PEP 703 surfacing in the filename.
3. **`python3.14-config` lives next to the interpreter**, not on `PATH` via the `bin` shim.
   In this uv layout it is
   `~/.local/share/uv/python/cpython-3.14-macos-aarch64-none/bin/python3.14-config`. The
   free-threaded one reports `-I.../include/python3.14t` — a **different include directory**,
   which is how the two builds' headers stay apart.
4. `-Wall -Wextra` is not optional in this domain. It is the only static checking you get.

### It runs — both builds

Real output, unedited *(measured)*:

```console
=========== GIL BUILD ===========
interp     : 3.14.6 gil_enabled=True
module     : pmx17.cpython-314-darwin.so BUILD = gil
checksum   : 0xe1d7a701437f78f9
accumulator: digest=0xe1d7a701437f78f9 nbytes=11  matches one-shot: True
1-D bytes  : {'len': 4, 'ndim': 1, 'format': 'B', 'itemsize': 1, 'readonly': True,
              'shape': (4,), 'strides': (1,), 'suboffsets': None,
              'c_contiguous': True, 'f_contiguous': True}
2-D int32  : {'len': 48, 'ndim': 2, 'format': 'i', 'itemsize': 4, 'readonly': False,
              'shape': (3, 4), 'strides': (16, 4), 'suboffsets': None,
              'c_contiguous': True, 'f_contiguous': False}
strided 1D : {'len': 16, 'ndim': 1, 'format': 'i', 'itemsize': 4, 'readonly': False,
              'shape': (4,), 'strides': (12,), 'suboffsets': None,
              'c_contiguous': False, 'f_contiguous': False}
chained    : ChecksumError | argument is not a checksummable buffer
  __cause__            : TypeError("a bytes-like object is required, not 'int'")
  __context__          : None
  __suppress_context__ : True

=========== FREE-THREADED BUILD ===========
interp     : 3.14.6 gil_enabled=False
module     : pmx17.cpython-314t-darwin.so BUILD = free-threaded
checksum   : 0xe1d7a701437f78f9
accumulator: digest=0xe1d7a701437f78f9 nbytes=11  matches one-shot: True
  ... (identical) ...
```

Same source, two builds, two binaries, identical behaviour — and `gil_enabled=False` stays
`False` after the import, which is the `Py_mod_gil` slot doing its job (§11).

---

## 7. Defining types in C: static vs heap

`Accumulator` above is a **heap type**, created at runtime by `PyType_FromModuleAndSpec`.
The older shape is a **static type**: a `PyTypeObject` you declare as a file-scope C global
and fill in field by field.

```c
/* the OLD way — a statically allocated PyObject */
static PyTypeObject AccumulatorType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pmx17.Accumulator",
    .tp_basicsize = sizeof(AccumulatorObject),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = Accumulator_new,
    .tp_dealloc = Accumulator_dealloc,
    .tp_methods = Accumulator_methods,
};
/* ... then PyType_Ready(&AccumulatorType); in module init */
```

It is shorter and it is why every tutorial written before ~2020 uses it. Here is why it is
now the wrong default:

| | Static type | Heap type (`PyType_FromSpec` family) |
|---|---|---|
| Storage | file-scope C global, one per **process** | heap object, one per **module instance** |
| Subinterpreters | shared across all of them → state leaks | isolated per interpreter |
| Limited API | **impossible** (`PyTypeObject` is opaque) | the only option |
| `abi3t` / free-threading | **impossible** (statically allocated `PyObject`) | fine |
| Module access from a method | `PyType_GetModule` **does not work** | works — the type knows its module |
| `tp_dealloc` | plain | must `Py_DECREF(Py_TYPE(self))` |
| Attribute assignment from Python | not allowed | allowed (it's a real class) |
| Cost | zero at runtime | one type object built at import |

The single argument that ends the debate: **a static `PyTypeObject` is a statically
allocated `PyObject`, which is exactly what PEP 793 and PEP 803 need to eliminate** (§2,
§3). Static types are on the same road as single-phase init.

### The three heap-type gotchas

**1. The dealloc dance.** An instance of a heap type holds a strong reference to its class
(taken by `tp_alloc`). If your `tp_dealloc` doesn't release it, **the class object never
dies** — a leak of the whole type, its dict, its methods, and transitively its module. The
canonical form, exactly as in §6:

```c
PyTypeObject *tp = Py_TYPE(self);
PyObject_GC_UnTrack(self);      /* only if HAVE_GC */
/* ... clear your own fields with Py_CLEAR ... */
tp->tp_free(self);
Py_DECREF(tp);                  /* <-- the line that is always missing */
```

Note the order: cache `tp` *before* freeing `self`, since after `tp_free` the memory is
gone.

**2. `tp_traverse` must visit the type.** Since 3.9 (bpo-40217), a GC-enabled heap type's
`tp_traverse` must `Py_VISIT(Py_TYPE(self))`. Skip it and the instance→class edge is
invisible to the cycle collector, so a class whose instances reference it can never be
collected. See [`22-garbage-collection.md`](22-garbage-collection.md) for why an
incomplete `tp_traverse` is a correctness bug and not a performance one.

**3. `PyType_FromModuleAndSpec` vs `PyType_FromSpec`.** Use the *ModuleAndSpec* variant
whenever the type's methods need module state. It associates the type with the module, so
`PyType_GetModuleState(Py_TYPE(self))` works inside a method. That association is the whole
PEP 573 mechanism (§8). `PyType_FromMetaclass` (3.12+) is the newest and most general
entry point — it additionally lets you specify the metaclass, which the older two hard-code
to `type`.

---

## 8. Multi-phase init, per-module state, and subinterpreters

### The bug that a C global now is

```c
static long counter = 0;                    /* one per PROCESS */
static PyObject *cached_exception = NULL;   /* one per PROCESS */
```

For 25 years this was normal. Three things broke it:

1. **Reloading.** `importlib.reload()` on a single-phase module doesn't re-run
   initialization; `sys.modules` gets a fresh module object whose `__dict__` is
   *shared* with the old one, because the contents come from a per-process struct.
2. **Subinterpreters (PEP 684, per-interpreter GIL, 3.12; PEP 734,
   `concurrent.interpreters`, 3.14).** Two interpreters, one C global. Your "module state"
   silently becomes cross-interpreter shared mutable state — with no lock, and, under
   PEP 684, no shared GIL either.
3. **Free-threading.** The GIL used to be an implicit mutex around every C global. It
   isn't any more. See [`24-the-gil.md`](24-the-gil.md) §9.

The full fix is a stack of PEPs, all by roughly the same people:

| PEP | Title | Status / version | What it gives you |
|---|---|---|---|
| 3121 | Extension Module Initialization and Finalization | Final, 3.0 | `m_size`, `PyModule_GetState` |
| 489 | Multi-phase extension module initialization | **Final, 3.5** | `PyModuleDef_Init`, `Py_mod_create`, `Py_mod_exec` |
| 630 | Isolating Extension Modules | Informational, Final | the how-to guide for the whole migration |
| 573 | Module State Access from C Extension Methods | **Final, 3.9** | `PyType_FromModuleAndSpec`, `PyType_GetModule`, `PyCMethod` |
| 687 | Isolating modules in the standard library | Final, 3.12 | the stdlib eating its own dog food |
| 684 | A Per-Interpreter GIL | Final, 3.12 | `Py_MOD_PER_INTERPRETER_GIL_SUPPORTED` becomes meaningful |
| 734 | Multiple Interpreters in the Stdlib | Final, 3.14 | `concurrent.interpreters` |
| 793 | (new module export hook) | **Final, 3.15** | `PyModExport_*` — no `PyModuleDef` at all (§3) |

### Single-phase vs multi-phase, mechanically

```
  SINGLE-PHASE  (PyModule_Create)          MULTI-PHASE  (PyModuleDef_Init, PEP 489)
  ──────────────────────────────           ──────────────────────────────────────
  import machinery
     │                                        │
     ▼                                        ▼
  PyInit_foo()                             PyInit_foo()
     │  creates the module object             │  returns &foo_def  (a PyModuleDef,
     │  right here, runs all your init        │  which IS a PyObject — see §2)
     │  code, returns a live module           │
     ▼                                        ▼
  interpreter has a module, and NO        interpreter reads m_slots BEFORE running
  chance to inspect it beforehand.        any of your code. It can now check
                                          Py_mod_multiple_interpreters and Py_mod_gil.
                                             │
                                             ├─ Py_mod_create (optional): return a
                                             │   custom module object
                                             ├─ allocate m_size bytes of state
                                             ├─ set __spec__, __name__, __loader__
                                             └─ Py_mod_exec(module): YOUR init code,
                                                 with the module already fully formed
```

The load-bearing difference is the **word "before"**. With multi-phase init the interpreter
can read your declarations *without executing your code*. That is the only reason
`Py_mod_gil` can work at all: the free-threaded interpreter must decide whether to re-enable
the GIL *before* your module runs anything.

PEP 793 spells this out as the design flaw it fixes: because single-phase modules can only
be interrogated by calling them, CPython currently has to *temporarily switch to the main
interpreter*, call the hook there, and then switch back and redo the import. The PEP calls
this "unnecessary and fragile extra work" that "highlights the underlying design issue".

### Measured: subinterpreters see through the lie

I built `pmx17_legacy.c` — 35 lines, single-phase, one C global — as the control. Then, on
the GIL build with `concurrent.interpreters` (PEP 734, stdlib since 3.14) *(measured)*:

```console
  sub: pmx17 imported OK. ChecksumError id = 0x7b11efc10
  sub: pmx17 imported OK. ChecksumError id = 0x7b11ebc10
main: pmx17.ChecksumError id = 0x7b11a0810
main: legacy module in a subinterpreter -> ExecutionFailed
       ImportError: module pmx17_legacy does not support loading in subinterpreters
```

Three interpreters, **three distinct `ChecksumError` objects** — because the exception lives
in per-module state, allocated once per module instance. And the single-phase module is
refused outright: CPython will not load it into a subinterpreter, because it cannot
guarantee isolation. That is not a warning you can ignore; it is an `ImportError`.

### The per-module state pattern, end to end

1. `m_size = sizeof(my_state)` in the `PyModuleDef`. (`-1` means "single-phase, no state";
   `0` means "multi-phase, no state".)
2. `PyModule_GetState(module)` inside module-level functions — the `self` argument of a
   `METH_*` module function **is** the module.
3. From a *method of a type*, you need the module first:
   - `PyType_GetModuleState(Py_TYPE(self))` if the type was created with
     `PyType_FromModuleAndSpec`; or
   - use the `PyCMethod` signature with `METH_METHOD | METH_FASTCALL | METH_KEYWORDS`,
     which passes `PyTypeObject *defining_class` explicitly. **This is the PEP 573
     contribution**, and it exists because `Py_TYPE(self)` is wrong for subclasses.
4. `m_traverse` / `m_clear` must visit and clear every `PyObject*` in your state. A module
   holding a type holding a method holding the module is a cycle; without `m_traverse` it
   leaks.
5. `m_free` runs at module teardown; delegating to `m_clear` is the standard idiom.

---

## 9. The buffer protocol (PEP 3118)

> **PEP 3118 — Revising the buffer protocol.** Travis Oliphant, Carl Banks. Final, 3.0.
> *(verified 2026-08-02.)*

The buffer protocol is how a C extension says *"here is a block of memory, in this shape,
with this element type — read it directly, do not copy it, and do not free it until I say
so."* It is the reason NumPy, `memoryview`, `array`, `bytes`, Pillow, and Arrow can hand
data to each other for free.

### The struct

```c
typedef struct {
    void      *buf;         /* start of the logical block                 */
    PyObject  *obj;         /* the EXPORTER — a strong ref, keeps it alive */
    Py_ssize_t len;         /* total bytes = product(shape) * itemsize     */
    Py_ssize_t itemsize;    /* bytes per element                          */
    int        readonly;
    int        ndim;
    char      *format;      /* struct-module syntax: "i", "d", "3f", ...  */
    Py_ssize_t *shape;      /* ndim entries                               */
    Py_ssize_t *strides;    /* ndim entries, IN BYTES                     */
    Py_ssize_t *suboffsets; /* ndim entries, or NULL                      */
    void      *internal;
} Py_buffer;
```

`buf`, `shape`, `strides`, `suboffsets`, `format` and `internal` all belong to the
exporter. `obj` is a **strong reference** — which is exactly why holding a `Py_buffer`
makes it safe to touch `buf` after releasing the GIL (§10). Every successful
`PyObject_GetBuffer` must be paired with **exactly one** `PyBuffer_Release`.

### Request flags — ask for the least you can handle

```
  PyBUF_SIMPLE       flat, contiguous, no shape/strides given.  ← the 90% case
  PyBUF_WRITABLE     fail if the exporter is read-only
  PyBUF_FORMAT       fill in `format`
  PyBUF_ND           fill in shape        (implies SIMPLE)
  PyBUF_STRIDES      fill in strides      (implies ND)
  PyBUF_C_CONTIGUOUS / _F_CONTIGUOUS / _ANY_CONTIGUOUS
  PyBUF_INDIRECT     fill in suboffsets — the exporter may be a pointer array
  PyBUF_FULL         STRIDES|WRITABLE|FORMAT|INDIRECT
  PyBUF_FULL_RO      same, read-only allowed
```

The flags are a **negotiation**. `PyBUF_SIMPLE` on a non-contiguous `memoryview` raises
`BufferError` — which is correct and is the point: you asked for a flat block and the
exporter cannot honestly give you one. Asking for more than you can handle is the bug;
asking for the minimum makes the interpreter reject bad input for you.

### Strides, measured

Real output from `pmx17.describe_buffer` *(measured)*:

```python
>>> pmx17.describe_buffer(b"abcd")
{'len': 4, 'ndim': 1, 'format': 'B', 'itemsize': 1, 'readonly': True,
 'shape': (4,), 'strides': (1,), 'suboffsets': None,
 'c_contiguous': True, 'f_contiguous': True}

>>> m = memoryview(array.array('i', range(12))).cast('B').cast('i', (3, 4))
>>> pmx17.describe_buffer(m)
{'len': 48, 'ndim': 2, 'format': 'i', 'itemsize': 4, 'readonly': False,
 'shape': (3, 4), 'strides': (16, 4), 'suboffsets': None,
 'c_contiguous': True, 'f_contiguous': False}

>>> pmx17.describe_buffer(memoryview(array.array('i', range(12)))[::3])
{'len': 16, 'ndim': 1, 'format': 'i', 'itemsize': 4, 'readonly': False,
 'shape': (4,), 'strides': (12,), 'suboffsets': None,
 'c_contiguous': False, 'f_contiguous': False}
```

Read the third one carefully. `[::3]` on a 12-element `int32` array produced a view with
**`len=16`, `shape=(4,)`, `strides=(12,)`, and `c_contiguous=False`** — and **zero bytes
were copied**. `strides=(12,)` means "advance 12 bytes to reach the next element" — three
`int32`s. That is the entire idea of a strided view:

```
   underlying array (12 × int32 = 48 bytes)
   ┌────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┐
   │ 0  │ 1  │ 2  │ 3  │ 4  │ 5  │ 6  │ 7  │ 8  │ 9  │ 10 │ 11 │
   └─▲──┴────┴────┴─▲──┴────┴────┴─▲──┴────┴────┴─▲──┴────┴────┘
     │              │              │              │
     └── stride 12 ─┴── stride 12 ─┴── stride 12 ─┘
     view: buf = &array[0], shape = (4,), strides = (12,), len = 16
     "4 elements of 4 bytes each, spaced 12 bytes apart"

   Address of element i:  buf + i*strides[0]
   For an N-d view:       buf + Σ(index[k] * strides[k])
```

And **`suboffsets`** — the field nobody uses and everybody should recognize — handles the
case where the data is not one block at all but a **pointer-indirection array** (the C
`int **` layout, PIL's old row-pointer images). Where `suboffsets[k] >= 0`, you must
*dereference* the pointer you land on and then add the suboffset. It exists so that
protocols like NumPy's can talk to libraries that never had a flat layout. If your code
requests `PyBUF_SIMPLE`/`PyBUF_ND`/`PyBUF_STRIDES` you will never see a non-`NULL`
`suboffsets`, because the exporter must refuse rather than lie — only `PyBUF_INDIRECT`
opens that door.

### Where this shows up

- **`memoryview`** is the buffer protocol wearing a Python costume. Slicing it is O(1).
- **NumPy** is a buffer exporter and consumer; `np.frombuffer` and `np.asarray` of a
  buffer-exporting object are zero-copy. [`34-going-native.md`](34-going-native.md) covers
  strides, dtypes and views properly.
- **Arrow** deliberately went further, defining its own C data interface rather than using
  PEP 3118, because PEP 3118 has no notion of nulls, nested types, or dictionary encoding.
  Knowing *why* Arrow didn't reuse it is a better answer than knowing that it didn't.
- **`bytes(view)` copies. `view` does not.** The most common accidental copy in Python
  data code is a `bytes()` call on something that was already zero-copy.

### The pinning contract

While a `Py_buffer` is outstanding, the exporter must not reallocate. This is enforced —
try it:

```python
>>> ba = bytearray(b"hello")
>>> mv = memoryview(ba)
>>> ba.append(1)
BufferError: Existing exports of data: object cannot be re-sized
```

That is not politeness; it is the guarantee that lets §10 release the GIL and keep
dereferencing `view.buf` from another thread. **A `Py_buffer` you forgot to release is a
`bytearray` that can never grow again** — a real, and genuinely confusing, production bug
shape.

---

## 10. Releasing the GIL — the exact contract, measured

[`24-the-gil.md`](24-the-gil.md) §3 established *why* the eval loop drops the GIL and what
`_PyThreadState_Swap(NULL)` protects. This section is the extension author's side of the
same protocol.

### What the macro actually is

Verbatim from `include/python3.14/ceval.h` in this build:

```c
#define Py_BEGIN_ALLOW_THREADS { \
                        PyThreadState *_save; \
                        _save = PyEval_SaveThread();
#define Py_BLOCK_THREADS        PyEval_RestoreThread(_save);
#define Py_UNBLOCK_THREADS      _save = PyEval_SaveThread();
#define Py_END_ALLOW_THREADS    PyEval_RestoreThread(_save); \
                 }
```

Three things follow directly from those five lines:

1. **It opens a C block.** `_save` is a local in a `{...}` scope. `Py_END_ALLOW_THREADS`
   closes the brace. Unbalanced macros are a syntax error, which is the one part of this
   contract the compiler *does* check.
2. **You cannot `return` out of the middle.** The header says so in capital letters. If you
   must bail early, insert `Py_BLOCK_THREADS` first — which is why that macro exists.
3. **`PyEval_SaveThread()` does two things**: it releases the GIL *and* it detaches the
   thread state (setting the current thread state to `NULL`). Those must move together —
   see [`24-the-gil.md`](24-the-gil.md) §3.4, where desynchronizing them produces
   `Py_FatalError("tstate mix-up")`.

The header also warns: **`WARNING: NEVER NEST CALLS TO Py_BEGIN_ALLOW_THREADS AND
Py_END_ALLOW_THREADS!!!`** — because `_save` would shadow, and you'd restore the wrong
state.

### The contract, stated as rules

Between `Py_BEGIN_ALLOW_THREADS` and `Py_END_ALLOW_THREADS` you promise:

- **No `PyObject*` is touched.** Not read, not written, not `Py_INCREF`ed. Not `Py_None`.
  Not a borrowed reference you're "just reading". A `Py_DECREF` here is a data race with
  every other thread.
- **No C API call**, except the handful explicitly documented as GIL-free
  (`PyMem_RawMalloc`/`RawFree`, `PyGILState_Ensure`, `PyEval_RestoreThread`).
- **No exception is set or checked.** The exception indicator lives in the thread state you
  just detached.
- **Any pointer you dereference is pinned by something that outlives the block** — a
  `Py_buffer` export (§9), a `malloc`ed copy, or memory you own outright.

And you gain: other Python threads run. On the GIL build that is the *only* way to get
parallelism out of a CPU-bound native routine.

### Measured — and the result is more interesting than expected

Same 64 MB FNV-1a hash, two entry points differing only by the macro pair. N threads, min
of 3 runs, `speedup = (1-thread time × N) / N-thread time` *(measured, M3 Pro)*:

```
=========== GIL BUILD (python3.14) ===========
threads |  RELEASES GIL  speedup |   HOLDS GIL  speedup
      1 |        0.075s    1.01x |      0.078s    0.94x
      2 |        0.077s    1.95x |      0.149s    0.98x
      4 |        0.078s    3.83x |      0.301s    0.98x
      8 |        0.088s    6.83x |      0.607s    0.97x

=========== FREE-THREADED BUILD (python3.14t) ===========
threads |  RELEASES GIL  speedup |   HOLDS GIL  speedup
      1 |        0.075s    0.96x |      0.077s    0.97x
      2 |        0.081s    1.77x |      0.085s    1.76x
      4 |        0.095s    3.02x |      0.079s    3.76x
      8 |        0.091s    6.34x |      0.105s    5.67x
```

Three readings, in order of increasing interest:

**1. The GIL build's "holds GIL" column is a flat line at 0.97×.** Eight threads, eight
cores available, and wall time grows exactly linearly: 0.078 → 0.149 → 0.301 → 0.607.
That is the GIL, drawn from the inside. **Two identical C loops; one macro pair; 7× the
throughput.**

**2. The GIL build reaches 6.83× on 8 threads.** Not 8×, because this is a 6P+6E core
machine and the E-cores are slower — a detail that would be invisible on a homogeneous x86
box and is worth remembering when you benchmark on Apple Silicon.

**3. On the free-threaded build, both columns scale.** The "holds GIL" version reaches
5.67× on 8 threads — because *there is no GIL to hold*. `Py_BEGIN_ALLOW_THREADS` still
detaches the thread state (which the GC needs), but it no longer gates other threads.

That third point is the one worth internalizing: **`Py_BEGIN_ALLOW_THREADS` is a
GIL-build-shaped optimization that becomes near-irrelevant to throughput on a free-threaded
build.** It is *not* irrelevant to correctness — you still must release around blocking
calls so the stop-the-world cycle collector can run
([`24-the-gil.md`](24-the-gil.md) §8.5) — but the dramatic speedup was always a story
about the GIL, and the GIL is what's going away.

### Foreign threads: `PyGILState_Ensure`

A thread created by C code (a pthread you spawned, an audio callback, a Qt worker, a Rust
`std::thread`) has **no `PyThreadState`**. Calling any C API from it is undefined behaviour.

```c
void *worker(void *arg)                 /* a thread CPython has never seen */
{
    PyGILState_STATE gstate = PyGILState_Ensure();   /* creates a tstate if needed */
    /* ... full C API access here ... */
    PyGILState_Release(gstate);                       /* must be LIFO with Ensure   */
    return NULL;
}
```

- `PyGILState_Ensure` is **reentrant** and idempotent: it detects an existing thread state
  and reuses it. That's the whole reason the opaque `PyGILState_STATE` return value exists —
  `Release` needs to know whether *this* call was the one that created the state.
- Pairs must nest strictly (LIFO). Crossing them corrupts the thread-state stack.
- **It still applies on the free-threaded build.** The official HOWTO is explicit: "if you
  create a thread outside of Python, you must call `PyGILState_Ensure()` before calling into
  the Python API to ensure that the thread has a valid Python thread state." Removing the
  GIL did not remove the thread state.
- It applies only to the **main interpreter** by default. With subinterpreters you want
  `PyThreadState_New` / `PyThreadState_Swap` against the right `PyInterpreterState`.

---

## 11. Free-threading rules for extensions

[`24-the-gil.md`](24-the-gil.md) §9 made the claim: *the genuinely new hazards live in C
extensions, not in Python-level code.* This section is that claim, executed.

### The opt-in

An extension is presumed GIL-requiring. You must say otherwise, and there are two ways:

```c
/* Multi-phase (preferred) — a slot, readable BEFORE your code runs. */
static PyModuleDef_Slot slots[] = {
    {Py_mod_exec, exec_fn},
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
    {0, NULL}
};

/* Single-phase (legacy) — a call, guarded, because the function only
 * exists on the free-threaded build. */
PyMODINIT_FUNC PyInit_mymodule(void) {
    PyObject *m = PyModule_Create(&moduledef);
    if (m == NULL) return NULL;
#ifdef Py_GIL_DISABLED
    PyUnstable_Module_SetGIL(m, Py_MOD_GIL_NOT_USED);
#endif
    return m;
}
```

Note the `PyUnstable_` prefix (PEP 689): this API is public, documented, and **may change
without deprecation in 3.15**. It is spelled that way on purpose so the ecosystem can grep
for it. The values are `Py_MOD_GIL_USED` (`(void*)0`, the default) and
`Py_MOD_GIL_NOT_USED` (`(void*)1`) — verified in `include/python3.14/moduleobject.h`.

### The failure mode, measured

If you don't declare it, importing your module **turns the GIL back on for the entire
process**, at runtime, silently except for a warning. Real output, free-threaded 3.14.6
*(measured)*:

```console
$ python3.14t gilslot.py
<frozen importlib._bootstrap>:491: RuntimeWarning: The global interpreter lock (GIL) has
been enabled to load module 'pmx17_legacy', which has not declared that it can run safely
without the GIL. To override this behavior and keep the GIL disabled (at your own risk),
run with PYTHON_GIL=0 or -Xgil=0.
startup                : False
after import pmx17     : False   (declares Py_MOD_GIL_NOT_USED)
after import legacy    : True    (no Py_mod_gil slot)
```

**This is the single most important operational fact in free-threading rollout.** One
transitive dependency — a logging handler, a JSON accelerator, a metrics client — that
hasn't declared the slot, and your carefully benchmarked free-threaded service is running
with the GIL on. It is a `RuntimeWarning`, which nobody's log aggregator alerts on.

Make it fail loudly instead:

```console
$ python3.14t -W error::RuntimeWarning app.py
RuntimeWarning: The global interpreter lock (GIL) has been enabled to load module ...
```

Or assert `sys._is_gil_enabled() is False` at the end of startup. Do one of these in CI.

### Why borrowed references get much more dangerous

Under the GIL, a borrowed reference is invalidated only if *your own thread* runs Python
code (§4, Bug 1). That's a rule you can follow by inspection: look for calls that can
re-enter the interpreter.

Without the GIL, **another thread can free the object between your load and your use, with
no call of yours in between.**

```c
/* Under the GIL: subtly wrong. Without it: a use-after-free with a race window. */
PyObject *item = PyList_GetItem(list, 0);   /* borrowed */
                                            /* <-- another thread: list.clear() */
Py_ssize_t n = PyList_Size(item);           /* reads freed memory */
```

The replacement APIs return **strong references**. All of these are verified present in
this build's headers:

| Borrowed (unsafe under concurrency) | Strong-reference replacement | Since |
|---|---|---|
| `PyList_GetItem`, `PyList_GET_ITEM` | **`PyList_GetItemRef`** | 3.13 |
| `PyDict_GetItem`, `PyDict_GetItemWithError` | **`PyDict_GetItemRef`** | 3.13 |
| `PyDict_GetItemString` | **`PyDict_GetItemStringRef`** | 3.13 |
| `PyDict_SetDefault` | **`PyDict_SetDefaultRef`** | 3.13 |
| `PyObject_GetAttr` (raising on missing) | **`PyObject_GetOptionalAttr`** | 3.13 |
| `PyObject_GetAttrString` | **`PyObject_GetOptionalAttrString`** | 3.13 |
| `PyImport_AddModule` | `PyImport_AddModuleRef` | 3.13 |
| `PyWeakref_GetObject` | `PyWeakref_GetRef` | 3.13 |

*(`PyDict_GetItemRef`, `PyList_GetItemRef`, `PyDict_GetItemStringRef`,
`PyDict_SetDefaultRef`, `PyObject_GetOptionalAttr` and `PyObject_GetOptionalAttrString`
were each confirmed by grepping this build's `include/python3.14/` headers — see §17.)*

Note the `*_GetItemRef` signature change: they return `int` (`1` found, `0` not found,
`-1` error) and write the object through an out-parameter. That is deliberate — it removes
the old "`NULL` might mean not-found or might mean error, call `PyErr_Occurred` to tell"
ambiguity from §5 at the same time.

### Critical sections

`Py_BEGIN_CRITICAL_SECTION(op)` locks `op`'s per-object mutex — the `PyMutex ob_mutex`
field you can see in the free-threaded `struct _object` in §2. From
`include/python3.14/cpython/critical_section.h`, verbatim:

```c
/* On the free-threaded build: */
#define Py_BEGIN_CRITICAL_SECTION(op)                                  \
    {                                                                  \
        PyCriticalSection _py_cs;                                      \
        PyCriticalSection_Begin(&_py_cs, _PyObject_CAST(op))
#define Py_END_CRITICAL_SECTION()                                      \
        PyCriticalSection_End(&_py_cs);                                \
    }

/* On the GIL build (Py_GIL_DISABLED undefined): */
#define Py_BEGIN_CRITICAL_SECTION(op)      {
#define Py_END_CRITICAL_SECTION()          }
```

**Literally a bare brace pair on the GIL build.** Zero cost, zero risk to add. There is a
two-object form `Py_BEGIN_CRITICAL_SECTION2(a, b)` for operations touching two containers
(the implementation handles deadlock avoidance, so argument order doesn't affect
correctness), and `*_MUTEX` variants that take a `PyMutex*` directly.

Four rules from the official HOWTO that surprise people:

1. **Critical sections may be temporarily suspended.** If code inside blocks — acquires
   another lock, does I/O, calls back into Python — **all** critical-section locks held by
   the thread are released. Entering one does *not* give you exclusive access for the
   section's duration. Reload anything you cached across such a call.
2. **Only the top-most (most recently entered) critical section's lock is guaranteed
   held.** Outer nested ones may be suspended.
3. **At most two objects.** Need three? Restructure.
4. Re-locking the same object won't deadlock, but it's less efficient than a purpose-built
   reentrant lock.

### Measured: what the critical section actually buys

Eight threads × 20,000 calls × 64 bytes = 10,240,000 expected bytes, three runs
*(measured)*:

```
=========== GIL BUILD ===========
gil_enabled=True   expected nbytes = 10,240,000
  update           run 0: 10,240,000  OK
  update           run 1: 10,240,000  OK
  update           run 2: 10,240,000  OK
  update_unlocked  run 0: 10,240,000  OK      ← the GIL was the lock
  update_unlocked  run 1: 10,240,000  OK
  update_unlocked  run 2: 10,240,000  OK

=========== FREE-THREADED BUILD ===========
gil_enabled=False  expected nbytes = 10,240,000
  update           run 0: 10,240,000  OK
  update           run 1: 10,240,000  OK
  update           run 2: 10,240,000  OK
  update_unlocked  run 0:  9,932,352  LOST 307,648 bytes (3.0%)
  update_unlocked  run 1:  9,722,880  LOST 517,120 bytes (5.0%)
  update_unlocked  run 2:  9,080,640  LOST 1,159,360 bytes (11.3%)
```

That is the whole migration risk in one table. The unlocked code is **100% correct on the
GIL build, three times out of three** — because `acc->nbytes += view.len` happened inside a
single C call, and the GIL made every C call atomic with respect to other Python threads.
On the free-threaded build the same code silently loses 3–11% of its data, non-deterministically,
with no error, no warning, and no crash. It does not fail; it *lies*.

**This is why the migration risk lives in extensions.** Your Python code's races were
already races. Your C code's non-races just became races, because the thing that made them
safe was an implementation detail of the interpreter.

### Allocation domains got stricter

[`16-object-memory-layout.md`](16-object-memory-layout.md) §4 called mixing the three
domains "undefined behaviour" and a best practice. On the free-threaded build the official
HOWTO upgrades it to a hard requirement: **only Python objects may be allocated with the
object domain, and all Python objects must be.** Use `PyMem_Malloc` for buffers, never
`PyObject_Malloc`. §14 shows how to catch violations.

### Other free-threading hazards

- **`static` caches inside functions.** `static PyObject *cached = NULL; if (!cached)
  cached = ...;` is now a race. Move it to module state (§8) or use
  `PyMutex`/`std::call_once`.
- **Free lists and object pools** in your extension need locking or thread-local storage.
- **`tp_dealloc` can now run concurrently** with other threads touching adjacent objects.
- **Wheel tags.** Free-threaded wheels are `cp314t`, and you need a separate build. On 3.14
  you cannot use the Limited API there at all (§2); on 3.15 you can, via `abi3t` (§3).

---

## 12. Calling protocols: `tp_call`, vectorcall, `METH_FASTCALL`

### The layers

```
   f(a, b, key=c)
        │
        ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │  PyObject_Call(callable, args_tuple, kwargs_dict)   — the OLD path      │
   │     builds a tuple. builds a dict. calls tp_call.                       │
   └───────────────────────────────┬────────────────────────────────────────┘
                                   │  tp_call(self, args, kwargs)
                                   ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │  VECTORCALL (PEP 590, 3.8)                                              │
   │  PyObject_Vectorcall(callable, PyObject *const *args,                   │
   │                      size_t nargsf, PyObject *kwnames)                  │
   │                                                                         │
   │    args ──▶ [ self? ][ a ][ b ][ c ]     a flat C array on the C stack  │
   │                              └─ kwnames = ("key",)  a tuple of NAMES    │
   │    nargsf = npositional | PY_VECTORCALL_ARGUMENTS_OFFSET                │
   │                                                                         │
   │  NO TUPLE. NO DICT. Zero allocations for the common call.               │
   │  The callable opts in with Py_TPFLAGS_HAVE_VECTORCALL + tp_vectorcall_  │
   │  offset pointing at a `vectorcallfunc` field in the instance.           │
   └───────────────────────────────┬────────────────────────────────────────┘
                                   │  and at the METH_* level:
                                   ▼
   METH_NOARGS    f(self, NULL)                          — no args at all
   METH_O         f(self, PyObject *arg)                 — exactly one, unparsed
   METH_FASTCALL  f(self, PyObject *const *args, Py_ssize_t nargs)
   METH_FASTCALL|METH_KEYWORDS
                  f(self, PyObject *const *args, Py_ssize_t nargs,
                    PyObject *kwnames)
   METH_METHOD|METH_FASTCALL|METH_KEYWORDS      (PyCMethod, PEP 573)
                  f(self, PyTypeObject *defining_class, args, nargs, kwnames)
   METH_VARARGS   f(self, PyObject *args_tuple)          — a tuple IS built
   METH_VARARGS|METH_KEYWORDS
                  f(self, PyObject *args, PyObject *kwargs)  — tuple AND dict
```

`PY_VECTORCALL_ARGUMENTS_OFFSET` is the clever bit: the caller may set that bit to promise
that `args[-1]` is writable, so a bound-method call can prepend `self` **in place** rather
than copying the array. Free performance for method calls, at the cost of one flag.

### Measured — the cost of each convention

Five trivial C functions (all `return None`), one Python function, `min` of 7 × 3,000,000
calls, GIL build *(measured)*:

| Call form | ns/call | vs `METH_O` |
|---|---|---|
| `METH_O` `f(x)` | **7.3** | 1.00× |
| `METH_NOARGS` `f()` | 8.9 | 1.22× |
| `METH_FASTCALL` `f(x)` | 9.2 | 1.26× |
| **pure Python `def f(a): return None`** | **11.0** | 1.51× |
| `METH_VARARGS` `f(x)` + `PyArg_ParseTuple` | 20.5 | **2.81×** |
| `METH_VARARGS`+`METH_KEYWORDS` `f(x)` | 23.0 | 3.15× |
| `METH_VARARGS`+`METH_KEYWORDS` `f(a=x)` | **45.9** | **6.29×** |

Four things worth extracting:

1. **`METH_VARARGS` costs 2.8× `METH_O`** for a one-argument function that does nothing.
   The tuple allocation plus `PyArg_ParseTuple`'s format-string interpretation is ~13 ns of
   pure overhead per call. If your C function is called in a tight loop and takes one
   argument, `METH_O` is free money.
2. **Passing that argument *by keyword* costs 6.3×.** 45.9 ns to call a function that
   returns `None`. Keyword arguments at a C boundary are genuinely expensive when the
   callee uses the old convention.
3. **A pure-Python function (11.0 ns) beats `METH_VARARGS` (20.5 ns).** Read that twice.
   "Rewrite it in C" is not automatically faster if you keep the 1990s calling convention.
   This is the single most useful number in this section.
4. `METH_FASTCALL` is *slower* than `METH_O` here (9.2 vs 7.3) — but that is an artifact of
   my microbenchmark: my FASTCALL function checks `nargs != 1` and `METH_O` checks nothing.
   FASTCALL's win appears with **two or more** arguments, where `METH_O` isn't available and
   the alternative is `METH_VARARGS`. Don't over-read a 2 ns delta on a do-nothing function.

### Argument Clinic

Writing `METH_FASTCALL|METH_KEYWORDS` argument parsing by hand is miserable and
error-prone. **Argument Clinic** is CPython's own preprocessor for exactly this: you write a
declarative docstring-shaped block in your `.c` file, run `Tools/clinic/clinic.py`, and it
generates the parsing code, the docstring, the signature (so `inspect.signature` works on
your C function), and the `PyMethodDef` entry.

```c
/*[clinic input]
pmx17.checksum
    buf: Py_buffer
    /
Compute FNV-1a 64 over a buffer.
[clinic start generated code]*/
```

It is officially an *internal* CPython tool — the devguide says so — and its output pins you
to a CPython version's conventions. But it is the standard answer inside CPython and in
several large extensions, and reading its generated code is the fastest way to learn what
optimal argument parsing looks like. If you are writing an extension by hand with more than
a few functions taking keywords, use it or use a binding generator (§13).

---

## 13. The binding-generator landscape, compared honestly

**All ecosystem claims below are dated. Verify before relying on any of them.** Versions
and dates were pulled from the projects' own changelogs and from the GitHub/PyPI APIs on
**2026-08-02**.

| | **Raw C API** | **Cython** | **pybind11** | **nanobind** | **PyO3** | **ctypes** | **cffi** | **HPy** |
|---|---|---|---|---|---|---|---|---|
| **What it is** | you write CPython C | Python-like lang → C | C++11 header-only | C++17 header-only | Rust proc-macros | stdlib FFI, no build | FFI, ABI or API mode | alternative C API |
| **Language** | C | Cython | C++ | C++ | Rust | Python | Python + C decls | C |
| **Build complexity** | low (one `clang`) | medium (`.pyx` → C) | medium (C++ toolchain) | medium (C++17 + CMake) | **high** (cargo + maturin) | **none** | low–medium | low |
| **Compile time** | fast | medium | **slow** (heavy templates) | ~4× faster than pybind11¹ | slow (Rust) | n/a | fast | fast |
| **Binary size** | smallest | medium | large | ~5× smaller than pybind11¹ | large (Rust std) | n/a | small | small |
| **Runtime overhead at boundary** | zero (it *is* the API) | very low | ~10× nanobind¹ | very low | very low | **high** (per-call marshalling) | medium | low on CPython |
| **`abi3` support** | yes, by hand | yes (3.0+, `limited_api`) | yes (`py_limited_api`) | yes (stable-ABI builds) | yes (`abi3-py3xx`) | n/a (no build) | yes (API mode) | ABI-stable by design |
| **`abi3t` (PEP 803)** | 3.15, `Py_TARGET_ABI3T` | not yet² | not yet² | not yet² | **yes — 0.29.0 (2026-06-11)** | n/a | **yes — 2.1.0 (2026-07-06)** | no |
| **Free-threading** | you do the work | **3.1.0 (2025-05-08)**, `freethreading_compatible` | **2.13.0 (2024-06-25)**, `py::mod_gil_not_used()` | **2.2.0 (2024-10-03)** | **0.23.0 (2024-11-15)**, `#[pymodule(gil_used=false)]`; **opt-out since 0.28.0 (2026-02-01)** | n/a | **2.0.0** | no |
| **Critical sections** | `Py_BEGIN_CRITICAL_SECTION` | yes (3.1+ primitives) | `py::scoped_critical_section` (3.0.0, 2025-07-10) | `nb::ft_mutex` / `ft_lock_guard`³ | `PyList::locked_for_each`, etc. | n/a | no | no |
| **Subinterpreters** | `Py_mod_multiple_interpreters` | yes | **3.0.0** `py::multiple_interpreters::per_interpreter_gil()` | partial³ | partial³ | n/a | no | no |
| **Debuggability** | **best** — it's your code | good (annotated C, `cython -a`) | poor (template soup in gdb) | medium | good (Rust backtraces) | poor (segfaults w/ no info) | medium | medium |
| **Memory safety** | none | none | none (C++) | none (C++) | **Rust's** | none | none | handles, not raw ptrs |
| **Last release** | — | 3.2.9 (2026-07-24) | 3.0.4 (2026-04-19) | 2.13.0 (2026-06-18) | 0.29.0 (2026-06-11) | stdlib | 2.1.0 (2026-07-06) | **0.9.0 (2023-09-22)** |
| **Last commit** | — | 2026-08-01 | 2026-08-01 | 2026-07-26 | 2026-08-01 | — | 2026-07-26 | **2025-05-26** |

¹ nanobind's own published benchmark headline: "bindings compile up to ~4× faster and
produce ~5× smaller binaries with ~10× lower runtime overheads compared to pybind11", and
vs Cython "3–12× binary size reduction, 1.6–4× compilation time reduction, similar runtime
performance." **These are vendor-reported numbers on nanobind's own microbenchmark.** They
are plausible and widely corroborated in direction, but I did not reproduce them.

² "Not yet" as of 2026-08-02 means I found no `abi3t` entry in the project's changelog. The
3.15 `abi3t` migration HOWTO itself says: "If your extension uses a code generator (like
Cython) or language binding (like PyO3), it's best to wait until that tool has support."
Python 3.15 is still pre-release (rc1), so this will move quickly.

³ Flagged as partial/unverified — see §17.

### Is HPy still active? — the honest answer

**No, not meaningfully.** HPy's premise was excellent: replace raw `PyObject*` with opaque
*handles* (`HPy`), so that the API makes no promise about object identity, layout, or
refcounting. That would have made CPython free to move objects, and would have let PyPy and
GraalPy run extensions at native speed instead of emulating CPython's refcounting.

The numbers as of **2026-08-02** *(measured, via the GitHub and PyPI APIs)*:

- Latest release: **0.9.0, published 2023-09-22** — nearly three years old.
- Last commit to `master`: **2025-05-26** — over 14 months ago.
- 1,139 stars. Repository not archived, but not moving either.

HPy's website still describes the project as "under active development... working hard
towards a stable release", which as of Aug 2026 the commit history does not support. Treat
the site copy as stale.

**Why it stalled, and why it matters anyway.** HPy required every extension to be *ported*,
with no incremental path and no immediate payoff on CPython. Meanwhile CPython absorbed most
of HPy's good ideas into the mainline API on an incremental path: opaque types (PEP 697),
per-module state (PEP 573), heap types everywhere, `PyModExport` (PEP 793), and `abi3t`
(PEP 803) — which delivers HPy's central promise, "one binary across interpreter
configurations", without a rewrite. HPy lost by being right too early and too expensively.
That is a genuinely useful lesson about API migrations at ecosystem scale.

### How to choose

- **`ctypes`**: for calling an existing shared library a handful of times. Zero build. Per
  call it is slow (it marshals argument types at runtime) and every mistake in an
  `argtypes`/`restype` declaration is a silent segfault. Great for a one-off; never for a
  hot path.
- **`cffi`**: `ctypes` done properly. *ABI mode* parses C declarations at runtime (no
  compiler); *API mode* generates and compiles a real extension, which is faster and
  type-checked by the C compiler. Preferred whenever you're wrapping a C library rather
  than writing new native code. Was PyPy's recommended path.
- **Cython**: the right answer when you have *Python code* that needs to be fast and
  gradually typed, and when you're already in the scientific stack. `cython -a` producing
  annotated HTML that shows exactly which lines still touch the C API is a debugging
  superpower no other tool here matches.
- **pybind11**: the right answer when you have an *existing C++ library* to expose and you
  want the largest community and the most Stack Overflow answers. Pay for it in compile time
  and binary size.
- **nanobind**: pybind11's author-adjacent successor for the same job, when compile time and
  binary size matter and you can require C++17. The migration is real work but mostly
  mechanical.
- **PyO3**: the right answer for *new* native code where memory safety is worth a build-system
  step change. It is also the most aggressive about free-threading — free-threaded support
  became **opt-out** in 0.28.0, and `Python::with_gil` was renamed `Python::attach`, which
  is a nicely honest acknowledgement that "the GIL" is no longer the thing you're acquiring.
- **Raw C API**: when you need total control, minimum size, no toolchain dependency, or you
  are writing something CPython-version-specific. And — as this document argues — when you
  need to *understand* what every other tool on this list is generating.

---

## 14. Debugging native extensions

### Why a segfault means "a refcount bug three frames back"

Here is a real, reproduced example. `badmem.over_decref(x)` does exactly one wrong thing:
`Py_DECREF` on a borrowed argument. The script decrefs 200 lists, prints, allocates some
garbage, then sums the lists' lengths.

```python
import badmem
big = [[i] for i in range(200)]
for x in big:
    badmem.over_decref(x)          # each list's refcount is now 1 too low
print("no crash yet -- the objects are still 'alive'", flush=True)
for i in range(200):
    junk = [object() for _ in range(2000)]   # churn the allocator
print("total:", sum(len(x) for x in big))
```

```console
$ python3.14 crash.py
no crash yet -- the objects are still 'alive'
[1]    60912 segmentation fault  python3.14 crash.py
$ echo $?
139
```

And the backtrace *(measured, real lldb output)*:

```console
$ lldb -b -o "run crash.py" -o "bt 12" -- ~/.local/bin/python3.14
Process 60912 stopped
* thread #1, queue = 'com.apple.main-thread', stop reason = EXC_BAD_ACCESS (code=1, address=0x0)
    frame #0: 0x00000001008d9b60 python3.14`list_dealloc + 56
(lldb) bt 12
* thread #1, stop reason = EXC_BAD_ACCESS (code=1, address=0x0)
  * frame #0: python3.14`list_dealloc + 56
    frame #1: python3.14`_TAIL_CALL_STORE_FAST + 168
    frame #2: python3.14`gen_iternext + 316
    frame #3: python3.14`builtin_sum + 964
    frame #4: python3.14`_TAIL_CALL_CALL + 320
    frame #5: python3.14`_PyEval_Vector + 780
    frame #6: python3.14`PyEval_EvalCode + 160
    frame #7: python3.14`run_mod + 292
    frame #8: python3.14`pyrun_file + 164
    frame #9: python3.14`_PyRun_SimpleFileObject + 256
    frame #10: python3.14`_PyRun_AnyFileObject + 80
    frame #11: python3.14`pymain_run_file_obj + 164
```

**`badmem` does not appear anywhere in that stack.** The crash is in `list_dealloc`, called
from `builtin_sum`, on the *last line of the script* — hundreds of thousands of allocations
after the actual bug. The stack tells you truthfully where the program died and nothing at
all about why.

That is the shape of essentially every refcount bug:

```
   the BUG                    the DAMAGE                     the CRASH
   ───────                    ──────────                     ─────────
   one extra Py_DECREF   →    refcount hits 0 early     →    some later code
   in your function           object is freed and             dereferences the
   (frame you never see)      its memory reused               reused memory
                                                              (the stack you get)
```

The one free clue CPython gives you is in the fatal-error report:

```
Extension modules: badmem (total: 1)
```

CPython lists loaded extension modules on fatal errors and in `faulthandler` output,
precisely because the culprit is usually one of them. If your crash report names three
extensions, those are your three suspects.

(Incidental bonus from that trace: `_TAIL_CALL_STORE_FAST` and `_TAIL_CALL_CALL` are
3.14's tail-calling interpreter — see [`20-eval-loop.md`](20-eval-loop.md).)

### `PYTHONMALLOC=debug` — the highest-value tool in this document

[`16-object-memory-layout.md`](16-object-memory-layout.md) §4 introduced the three
allocation domains and promised this section. Here it is, working.

I wrote `badmem.c` with three deliberate bugs: `PyMem_Malloc` freed with `PyObject_Free`,
`PyMem_RawMalloc` freed with `PyMem_Free`, and a one-byte buffer overrun. First, the
default build *(measured)*:

```console
$ python3.14 -c "import badmem; badmem.mismatch(); badmem.mismatch_raw(); badmem.overrun(); print('...')"
...all three bugs: no complaint, exit 0
```

**Three memory bugs, clean exit.** Now flip one environment variable:

```console
$ PYTHONMALLOC=debug python3.14 -c "import badmem; badmem.mismatch()"
Debug memory block at address p=0x107d6fca0: API 'm'
    64 bytes originally requested
    The 7 pad bytes at p-7 are FORBIDDENBYTE, as expected.
    The 8 pad bytes at tail=0x107d6fce0 are FORBIDDENBYTE, as expected.
    Data at p: cd cd cd cd cd cd cd cd ... cd cd cd cd cd cd cd cd

Enable tracemalloc to get the memory block allocation traceback

Fatal Python error: _PyMem_DebugRawFree: bad ID: Allocated using API 'm', verified using API 'o'
Python runtime state: initialized

Current thread 0x00000001f13bdd80 (most recent call first):
  File "<string>", line 1 in <module>

Extension modules: badmem (total: 1)
```

Read `bad ID: Allocated using API 'm', verified using API 'o'`. The debug allocator stores a
one-byte **API identifier** in the header of every block — `'r'` raw, `'m'` mem, `'o'`
object — and checks it at free time. Domain mismatch, caught at the exact instruction,
with a Python-level traceback.

The overrun is caught the same way, by guard bytes *(measured)*:

```console
$ PYTHONMALLOC=debug python3.14 -c "import badmem; badmem.overrun()"
Debug memory block at address p=0x103530bb0: API 'm'
    16 bytes originally requested
    The 7 pad bytes at p-7 are FORBIDDENBYTE, as expected.
    The 8 pad bytes at tail=0x103530bc0 are not all FORBIDDENBYTE (0xfd):
        at tail+0: 0x58 *** OUCH
        at tail+1: 0xfd
        ...
    Data at p: cd cd cd cd cd cd cd cd cd cd cd cd cd cd cd cd
```

`0x58` is `'X'` — the byte I wrote one past the end — sitting in the `0xfd` guard region.
The debug allocator's byte patterns are worth memorizing because you will see them in
crashes:

| Pattern | Meaning |
|---|---|
| `0xcd` (`CLEANBYTE`) | freshly allocated, never written. Seeing it in "real" data = **uninitialized read** |
| `0xdd` (`DEADBYTE`) | freed memory. Seeing it = **use-after-free** |
| `0xfd` (`FORBIDDENBYTE`) | guard padding before/after the block. Modified = **overrun/underrun** |

And add `PYTHONTRACEMALLOC=5` to get the allocation site, not just the free site
*(measured)*:

```console
$ PYTHONMALLOC=debug PYTHONTRACEMALLOC=5 python3.14 -c "import badmem; badmem.overrun()"
...
Memory block allocated at (most recent call first):
  File "<string>", line 1
```

**Run your extension's test suite under `PYTHONMALLOC=debug PYTHONTRACEMALLOC=5` in CI.**
It costs a couple of seconds and it converts a class of bug that manifests as
"segfault next Tuesday" into "test failure with a line number".

### The rest of the toolbox

**A debug build of CPython (`--with-pydebug`)** is the heavier hammer. It gives you:
`Py_REF_DEBUG` (a process-wide total refcount, so `sys.gettotalrefcount()` before and after
a loop detects leaks *of any object*), `Py_TRACE_REFS` (a linked list of all live objects),
assertions throughout the interpreter, and `PYTHONMALLOC=debug` on by default. It is
roughly 2–3× slower and **ABI-incompatible with release builds** — extensions must be
rebuilt against it (the `d` ABI flag). See [`13-cpython-source-map.md`](13-cpython-source-map.md)
for building one.

The leak test it enables is worth writing down:

```python
import sys
def leaks(fn, warmup=5, n=1000):
    for _ in range(warmup): fn()          # let caches settle
    before = sys.gettotalrefcount()
    for _ in range(n): fn()
    return sys.gettotalrefcount() - before   # should be ~0, not n
```

If the delta is proportional to `n`, you leak one reference per call. `sys.gettotalrefcount`
exists only on debug builds.

**Sanitizers.** ASan catches use-after-free, heap overflow, and leaks with a real
allocation-site backtrace; UBSan catches alignment, integer overflow, and invalid casts.

```console
$ clang -fsanitize=address,undefined -fno-omit-frame-pointer -g -O1 ... -o mymod...so mymod.c
$ ASAN_OPTIONS=detect_leaks=1 python3.14 -X faulthandler test.py
```

Two practical warnings. First, you get a wall of false positives from CPython itself unless
you use a suppression file — CPython ships one at `Misc/ASAN.supp` for exactly this reason,
and you generally want `PYTHONMALLOC=malloc` so ASan sees individual allocations instead of
pymalloc's arenas. Second, **LeakSanitizer does not work on macOS/arm64**; `detect_leaks` is
a Linux-only feature in practice. **This is one of the places you need a Linux box** (or a
container) even if you develop on a Mac. Use ASan/UBSan on macOS for memory *errors*, and a
Linux CI job for leak detection.

**lldb, not gdb, on macOS.** gdb requires code-signing gymnastics on macOS and is
effectively unsupported on arm64. lldb is what ships with the Command Line Tools and what
the trace above came from. Useful invocations:

```console
$ lldb -- python3.14 crash.py          # then: run / bt / frame variable / p *(PyObject*)0x...
$ lldb -p <pid>                        # attach to a hung process
$ lldb -b -o "run x.py" -o "bt 20" -- python3.14      # batch, for CI
```

CPython ships `Tools/gdb/libpython.py`, which teaches gdb to print `PyObject*`s and Python
frames. There is **no equally maintained lldb equivalent**; the community `lldb_libpython`
scripts exist but are patchy. Practically: **debug native crashes with lldb on macOS, and do
your Python-level stack inspection with `faulthandler` or `py-spy dump` instead.**

**`faulthandler`.** Free, always available, no rebuild:

```console
$ python3.14 -X faulthandler myscript.py
```

On segfault it prints the **Python** stack. That is exactly the information lldb can't give
you, and combining the two — lldb for the C frames, faulthandler for the Python frames — is
usually enough to localize a crash.

**Checklist for a native crash you cannot explain:**

1. `-X faulthandler` → which Python line?
2. `PYTHONMALLOC=debug PYTHONTRACEMALLOC=5` → domain error? overrun? use-after-free?
3. lldb `bt` → which C function? Is it a `*_dealloc`? (If yes: refcount bug, look for who
   decrefs that type.)
4. `gc.collect()` / `gc.disable()` around the suspect region → does GC timing change it?
   (If yes: `tp_traverse` is wrong.)
5. Debug build + `sys.gettotalrefcount()` delta per call → which function leaks or
   over-frees?
6. ASan on Linux → the allocation-site backtrace you actually wanted at step 3.
7. Only then: read your code.

---

## 15. Lab exercises

Reading this leaves you at **rung 3** on README §14's ladder — fluent, and one "why?" from
collapse. Every lab below is designed to move one specific claim from rung 3 to **rung 4
(built or broken it, and measured)**. Labs 4, 6 and 7 are the ones that reach **rung 5**,
because they force you to predict before you measure and to say where your model stops.

**1 — Build the module, then break it three ways.** Type `pmx17.c` in (don't paste it —
typing it is the point) and build it for both interpreters. Then introduce, one at a time:
(a) delete the `Py_DECREF(tp)` from `Accumulator_dealloc`; (b) delete the
`PyBuffer_Release` from `pmx_checksum`; (c) change `PyModule_AddObjectRef` to
`PyModule_AddObject` without adding the compensating `Py_DECREF`. For each: write down
what you predict will happen, then find an experiment that detects it. (a) needs
`sys.gettotalrefcount` or a growing `gc.get_objects()`; (b) shows up as
`BufferError: Existing exports of data` on a `bytearray`; (c) is a slow leak. *Proves §4
and §7 — that these are not style rules.*

**2 — Find your Limited API wall.** Compile `pmx17.c` with `-DPy_LIMITED_API` at 3.10,
3.11, 3.12, 3.13. Tabulate the error count at each floor and attribute every distinct
error to one of §2's three constraints (macro dereference / opaque struct / not-in-the-subset).
Then actually port one function — `pmx_checksum` — to compile clean at 3.11, and diff the
before/after. *Proves §2, and it is the fastest way to internalize what "Limited" means.*

**3 — Reproduce the abi3-on-free-threaded failure.** Build an `abi3` module, confirm it
imports on `python3.14`, confirm `python3.14t` lists `.abi3.so` in `EXTENSION_SUFFIXES`,
and confirm it fails with `SystemError`. Then compile the three-line program from §2 that
prints `sizeof(PyObject)` and `sizeof(PyModuleDef)` against both header sets, and write one
paragraph explaining the failure from the numbers. *Proves §2–§3, and gives you the
one-paragraph answer to "what is PEP 803 for?"*

**4 — The GIL-release table, on your hardware.** Build both `checksum` variants and
reproduce §10's 2×2 table. **Predict all sixteen numbers before running it.** You will
probably get the GIL-build column right and the free-threaded "holds GIL" column wrong —
that is the point. Then explain why 8 threads gives 6.83× and not 8× on your machine.
*Proves §10; the prediction step is what makes it rung 5.*

**5 — Break the critical section.** Reproduce §11's data-loss table. Then vary: chunk size
(8 bytes vs 64 KB), thread count, and iteration count, and find the regime where
`update_unlocked` looks correct. Now explain why "I tested it and it worked" is not
evidence. *Proves §11, and it is the single most important lab in this document for anyone
about to ship a free-threaded extension.*

**6 — Audit a real dependency.** Pick an installed extension module you depend on
(`_ssl`, `_json`, `orjson`, `numpy.core._multiarray_umath`, a database driver). Determine,
without reading its docs: (a) does it declare `Py_mod_gil`? (b) does it support
subinterpreters? (c) is it `abi3`? Use `python3.14t -W error::RuntimeWarning -c "import X"`,
`concurrent.interpreters`, and the filename. Write down the method you used — that method
is the deliverable, not the answer. *Proves §8 and §11 apply to code you didn't write.*

**7 — Debug a crash you didn't write.** Have someone else (or a script) insert exactly one
refcount error into a copy of `pmx17.c` — an extra decref, a missing incref, a missing
`Py_VISIT`, or a wrong allocation domain. Find it using §14's checklist, in order, and
record which step actually localized it. Do this three times with different bugs.
*Proves §14 and §4, and it is the closest thing to the real experience of owning a native
extension in production.*

**8 — Calling conventions on your data.** Reproduce §12's table, then extend it: add a
2-argument and a 5-argument function in `METH_VARARGS` and `METH_FASTCALL`, and find the
argument count at which FASTCALL clearly wins. Then take one hot `METH_VARARGS` function
from a real project and estimate, from your numbers, what converting it would buy at your
call rate. *Proves §12, and produces the argument you'd actually need to justify the work.*

**9 — Zero-copy end to end.** Extend `describe_buffer` to also report the address of
`view.buf`. Then show that `numpy.frombuffer(b)`, `memoryview(b)[::2]`, and
`array.array('i', ...)` all hand your C code pointers into the *same* allocation, and that
`bytes(view)` does not. Then find one place in a codebase you own where a copy is happening
that didn't need to. *Proves §9 is a production technique, not trivia.*

**10 — Port it to a binding generator.** Reimplement `pmx17` in **one** of Cython, pybind11,
nanobind, or PyO3. Measure: lines of code, cold compile time, stripped binary size, ns/call
for `checksum(b"")`, and whether it declares `Py_mod_gil` by default. Compare against the
raw-C numbers. *Proves §13 — and one honest measured row beats the whole table above.*

---

## 16. Question bank

Staff-level. If you can't answer from your own model, the section to reread is noted.

1. Limited API, Stable ABI, `abi3`, `abi3t`, `PyUnstable_`, internal API — define all six and say which one you'd ship a wheel against and why. *(§2, §3)*
2. Free-threaded 3.14 lists `.abi3.so` in `EXTENSION_SUFFIXES` but cannot load one. Explain the mechanism, from the object layout up. *(§2)*
3. What does PEP 803 change about wheel tags, and what does PEP 793 have to do with it? *(§3)*
4. A function returns a `PyObject*`. Name the three ownership contracts it could be under and say how you'd determine which, given only the docs. *(§4)*
5. Why is `Py_CLEAR(self->x)` not the same as `Py_XDECREF(self->x); self->x = NULL;`? Construct the crash. *(§4)*
6. `PyModule_AddObject` vs `PyModule_AddObjectRef` — what is the difference and why is the first one deprecated? *(§4)*
7. Your C function returns `NULL`. What else must be true, and what happens if it isn't? Now the reverse case. *(§5)*
8. Implement `raise MyError from original` in C, and say which calls steal references. *(§5)*
9. Why is a static `PyTypeObject` now the wrong default? Give the reason that has nothing to do with subinterpreters. *(§7, §2)*
10. What does an instance of a heap type own that an instance of a static type does not, and what happens if `tp_dealloc` forgets it? *(§7)*
11. Single-phase vs multi-phase init: what can the interpreter do in one and not the other, and why does `Py_mod_gil` depend on it? *(§8)*
12. Your module has `static PyObject *cache = NULL;`. Name three separate things that breaks. *(§8, §11)*
13. Why is it safe to dereference `view.buf` after `Py_BEGIN_ALLOW_THREADS`, and what exactly guarantees it? *(§9, §10)*
14. A `memoryview` slice reports `strides=(12,)` and `c_contiguous=False`. What is the underlying data, and how do you compute element *i*'s address? What are `suboffsets` for? *(§9)*
15. Write out what `Py_BEGIN_ALLOW_THREADS` expands to and derive three rules from the expansion alone. *(§10)*
16. Same C loop, one macro pair difference: 0.088s vs 0.607s at 8 threads on the GIL build, but 0.091s vs 0.105s on the free-threaded build. Explain both columns. *(§10)*
17. When must you call `PyGILState_Ensure`, and does that change on a free-threaded build? *(§10)*
18. Code that is correct 3/3 runs on the GIL build loses 3–11% of its data on the free-threaded build with no error. What is the bug class, and why does the GIL hide it? *(§11)*
19. Why do borrowed references become qualitatively more dangerous without the GIL, and name the strong-reference replacements for `PyDict_GetItem` and `PyList_GetItem`. *(§11, §4)*
20. Entering a critical section does not guarantee exclusive access for its duration. Why not, and what does that mean for values you cached before a blocking call? *(§11)*
21. A pure-Python function costs 11 ns/call; a C function with `METH_VARARGS` costs 20.5 ns. Explain, and say what you'd change. *(§12)*
22. What does `PY_VECTORCALL_ARGUMENTS_OFFSET` buy, and who benefits? *(§12)*
23. HPy solved a real problem and is effectively dormant. What was the problem, what solved it instead, and what is the transferable lesson? *(§13)*
24. Your extension segfaults in `list_dealloc` inside `builtin_sum`. Where is the bug, and what is your first diagnostic step? *(§14)*
25. `PYTHONMALLOC=debug` reports `bad ID: Allocated using API 'm', verified using API 'o'`. Translate, and say what the fix is. *(§14, [`16-object-memory-layout.md`](16-object-memory-layout.md) §4)*
26. You see `0xdd` bytes in a struct field. What happened? What about `0xcd`? *(§14)*
27. You develop on macOS/arm64 and need leak detection on your extension. What's your plan? *(§14)*

---

## 17. Sources

**Primary — the C API itself**
- [C API Reference](https://docs.python.org/3/c-api/) — the whole thing. **Read the ownership annotation on every function you call; it is there, and it is the contract.** Verdict: this is the only authority; everything else, including this document, is commentary.
- [Extending and Embedding the Python Interpreter](https://docs.python.org/3/extending/) — the official tutorial. Verdict: good for shape, dated in places on heap types; prefer the how-to guides below.
- [Defining Extension Modules (3.15)](https://docs.python.org/3.15/extending/extending.html) and [`c-api/module.html` (3.15)](https://docs.python.org/3.15/c-api/module.html) — the canonical home of PEP 793's `PyModExport_*`, `Py_mod_name`, `Py_mod_token`, `Py_mod_methods`, `Py_mod_state_size`, `PyModule_FromSlotsAndSpec`. Verdict: read this before writing any new module targeting 3.15.
- The headers in **your own build**: `Include/object.h`, `Include/refcount.h`, `Include/ceval.h`, `Include/moduleobject.h`, `Include/cpython/critical_section.h`, `Include/cpython/lock.h`. Verdict: **the ground truth.** Every macro expansion and struct layout in this document came from grepping these, not from memory.

**Primary — free-threading**
- [C API Extension Support for Free Threading (official HOWTO)](https://docs.python.org/3/howto/free-threading-extensions.html) — `Py_mod_gil`, critical sections, the borrowed→strong replacement table, the allocation-domain hardening. Verdict: **short, dense, and mandatory** before touching a free-threaded extension. Source of the "critical sections may be suspended" rules in §11.
- [Python support for free threading](https://docs.python.org/3/howto/free-threading-python.html) — the user-facing companion. Verdict: read for the overhead numbers.
- [PEP 703 — Making the GIL Optional](https://peps.python.org/pep-0703/) §Backwards Compatibility. Verdict: read alongside [`24-the-gil.md`](24-the-gil.md) §8.
- [Python Free-Threading Guide](https://py-free-threading.github.io/) and its [compatibility tracker](https://py-free-threading.github.io/tracking/) — community-maintained ecosystem status. Verdict: the right place to check *live* package status; I did not transcribe its contents here because they change weekly.

**PEPs — all headers verified against peps.python.org on 2026-08-02**
- [PEP 384 — Defining a Stable ABI](https://peps.python.org/pep-0384/) — von Löwis, Final, 3.2. Verdict: short; read the Rationale for why the boundary is drawn where it is.
- [PEP 489 — Multi-phase extension module initialization](https://peps.python.org/pep-0489/) — Viktorin/Behnel/Coghlan, Final, 3.5. Verdict: **the single most important PEP in this document.**
- [PEP 573 — Module State Access from C Extension Methods](https://peps.python.org/pep-0573/) — Viktorin/Coghlan/Snow/Plch, Final, 3.9. Verdict: read the Motivation; it explains why `Py_TYPE(self)` is the wrong answer.
- [PEP 590 — Vectorcall](https://peps.python.org/pep-0590/) — Shannon/Demeyer, Final, 3.8. Verdict: read §Specification for the `nargsf` bit trick.
- [PEP 3118 — Revising the buffer protocol](https://peps.python.org/pep-3118/) — Oliphant/Banks, Final, 3.0. Verdict: long and NumPy-flavoured; skim the struct and the flag table, skip the format-string grammar until you need it.
- [PEP 630 — Isolating Extension Modules](https://peps.python.org/pep-0630/) — Viktorin, Informational, Final. Verdict: **the practical how-to for §8's whole migration.** Start here, not at PEP 489.
- [PEP 689 — Unstable C API tier](https://peps.python.org/pep-0689/) — Viktorin, Final, 3.12. Verdict: two pages; explains the `PyUnstable_` prefix.
- [PEP 697 — Limited C API for Extending Opaque Types](https://peps.python.org/pep-0697/) — Viktorin, Final, 3.12. Verdict: prerequisite for understanding `abi3t`.
- [PEP 793 — new module export hook](https://peps.python.org/pep-0793/) — Viktorin, **Final, 3.15**, resolved 23-Oct-2025. Verdict: read Background & Motivation even if you never write a `PyModExport_*`; it is the clearest existing statement of why static `PyObject`s are a problem.
- [PEP 803 — "abi3t": Stable ABI for Free-Threaded Builds](https://peps.python.org/pep-0803/) — Viktorin/Goldbaum, **Final, 3.15**, resolved 30-Mar-2026, requires 703/793/697. Verdict: **the answer to the §2 experiment.** Read Specification and Rejected Ideas.
- [PEP 684 — A Per-Interpreter GIL](https://peps.python.org/pep-0684/) (Final, 3.12) and [PEP 734 — Multiple Interpreters in the Stdlib](https://peps.python.org/pep-0734/) (Final, 3.14) — Snow. Verdict: context for §8; the extension-facing consequence is one module slot.
- [Migrating to Stable ABI for free threading (`abi3t`) — 3.15 HOWTO](https://docs.python.org/3.15/howto/abi3t-migration.html). Verdict: the step-by-step port. Note its own advice to *wait* if you use a binding generator.

**Binding libraries — changelogs are the authority, not the marketing pages**
- [Cython](https://cython.readthedocs.io/) / [CHANGES.rst](https://github.com/cython/cython/blob/master/CHANGES.rst). 3.1.0 (2025-05-08) added `freethreading_compatible`; 3.2.3 (2025-12-14) made `Py_mod_gil` settable by C macro. Verdict: `cython -a` is the feature that justifies the tool.
- [pybind11](https://pybind11.readthedocs.io/) / [changelog](https://github.com/pybind/pybind11/blob/master/docs/changelog.md). 2.13.0 (2024-06-25) added `py::mod_gil_not_used()`; 3.0.0 (2025-07-10) added `py::scoped_critical_section` and subinterpreter support.
- [nanobind](https://nanobind.readthedocs.io/) / [changelog](https://github.com/wjakob/nanobind/blob/master/docs/changelog.rst) / [benchmarks](https://nanobind.readthedocs.io/en/latest/benchmark.html). 2.2.0 (2024-10-03) added free-threading. Verdict on the benchmark page: **vendor numbers on a vendor microbenchmark** — directionally credible, not independently verified here.
- [PyO3](https://pyo3.rs/) / [CHANGELOG](https://github.com/PyO3/pyo3/blob/main/CHANGELOG.md). 0.23.0 (2024-11-15) free-threading; 0.28.0 (2026-02-01) made it opt-*out*; 0.29.0 (2026-06-11) added `abi3t` features and dropped 3.13t. Verdict: currently the most aggressive on free-threading of anything in §13.
- [cffi](https://cffi.readthedocs.io/) — 2.0.0 free-threading, 2.1.0 (2026-07-06) `abi3t`.
- [HPy](https://hpyproject.org/) / [github.com/hpyproject/hpy](https://github.com/hpyproject/hpy). Verdict: **effectively dormant** — last release 0.9.0 (2023-09-22), last commit 2025-05-26, checked 2026-08-02. The website's "under active development" copy is stale. Read the [c-api-next-level manifesto](https://github.com/hpyproject/hpy/wiki/c-api-next-level-manifesto) anyway; it is the clearest statement of what is wrong with the C API.

**Debugging**
- [`PYTHONMALLOC`](https://docs.python.org/3/using/cmdline.html#envvar-PYTHONMALLOC) and [Memory Management](https://docs.python.org/3/c-api/memory.html). Verdict: §14's proof that one env var beats an afternoon in a debugger.
- [CPython devguide — Debug tools / Running tests under ASan](https://devguide.python.org/). Verdict: the source for `Misc/ASAN.supp` and the debug-build flags.
- [`faulthandler`](https://docs.python.org/3/library/faulthandler.html) — free Python-level stacks on segfault.
- lldb: `help bt`, `help frame`. There is no well-maintained lldb equivalent of CPython's `Tools/gdb/libpython.py`; see §14.

**Sibling docs**
- [`14-pyobject-and-types.md`](14-pyobject-and-types.md) — the `tp_*` slots §7 fills in.
- [`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md) — §4 at the concept level.
- [`16-object-memory-layout.md`](16-object-memory-layout.md) §2 (the +16 header, confirmed at the C level in §2 here) and §4 (the domains, exercised in §14).
- [`22-garbage-collection.md`](22-garbage-collection.md) — why `tp_traverse` is correctness, not performance.
- [`24-the-gil.md`](24-the-gil.md) §3 (the handoff protocol §10 hooks into), §9 (the migration risk §11 measures).
- [`26-free-threading.md`](26-free-threading.md) — the full migration; §11 is its C-side chapter.
- [`34-going-native.md`](34-going-native.md) — NumPy strides, Arrow, and choosing §13's tool for a real workload.
- [`44-packaging-and-environments.md`](44-packaging-and-environments.md) — wheels, ABI tags, and shipping what §2 and §3 describe.

---

### Appendix — what I could not verify

Stated plainly, because the C API is full of plausible-but-wrong names and the honest move
is to list the gaps rather than smooth them over.

**Verified by grepping this build's headers** (all present, all spelled as written):
`PyDict_GetItemRef`, `PyDict_GetItemStringRef`, `PyDict_SetDefaultRef`, `PyList_GetItemRef`,
`PyObject_GetOptionalAttr`, `PyObject_GetOptionalAttrString`, `PyType_FromSpec`,
`PyType_FromModuleAndSpec`, `PyType_FromMetaclass`, `PyType_GetModuleState`,
`PyModule_GetState`, `PyModule_AddObjectRef`, `PyModuleDef_Init`, `PyModule_FromDefAndSpec`,
`Py_mod_create`, `Py_mod_exec`, `Py_mod_multiple_interpreters`, `Py_mod_gil`,
`Py_MOD_GIL_USED`, `Py_MOD_GIL_NOT_USED`, `Py_MOD_PER_INTERPRETER_GIL_SUPPORTED`,
`PyUnstable_Module_SetGIL`, `Py_BEGIN_CRITICAL_SECTION`, `Py_BEGIN_CRITICAL_SECTION2`,
`PyMutex_Lock`, `PyGILState_Ensure`, `PyObject_GetBuffer`, `PyBuffer_Release`,
`PyBuffer_IsContiguous`, `METH_FASTCALL`, `PyObject_Vectorcall`, `PyErr_GetRaisedException`,
`PyErr_SetRaisedException`, `PyException_SetCause`, `PyException_SetContext`,
`PyErr_FormatUnraisable`, `Py_CLEAR`, `Py_XDECREF`, `Py_NewRef`, `Py_XNewRef`,
`PyUnstable_Object_ClearWeakRefsNoCallbacks`, `Py_T_PYSSIZET`, `Py_READONLY`.

**Verified by compiling and running**: everything in §6, plus §2's Limited-API error output,
§10's scaling table, §11's data-loss table, §12's ns/call table, and §14's crash and
`PYTHONMALLOC` output.

**Read but NOT compiled or run** (I have no 3.15 locally): everything in §3 —
`Py_TARGET_ABI3T`, `PyModExport_<name>`, `Py_mod_name`, `Py_mod_token`, `Py_mod_methods`,
`Py_mod_state_size`, `PyModule_FromSlotsAndSpec`, `PyModule_GetToken`,
`PyType_GetModuleByToken`, `PyType_GetBaseByToken`, `PyObject_GetTypeData`. The names come
from peps.python.org/pep-0803, peps.python.org/pep-0793, and the 3.15.0b4 docs; the
*semantics* I describe are paraphrase, not execution. The 3.15 `abi3t` HOWTO additionally
shows a `PySlot` / `PySlot_STATIC_DATA(...)` / `PySlot_END` slot-array spelling that I could
not corroborate against any header or reference page — **treat that particular spelling as
unverified.**

**Ecosystem claims I could not verify directly** (dated 2026-08-02, from changelogs and the
GitHub/PyPI APIs, not from building anything):
- nanobind's `nb::ft_mutex` / `nb::ft_lock_guard` names — I saw free-threading support
  attributed to 2.2.0 in the changelog but did not confirm those two identifiers in the
  nanobind headers.
- nanobind and PyO3 subinterpreter support ("partial" in §13's table) — I found no explicit
  changelog statement either way and am inferring from the absence of a
  `Py_mod_multiple_interpreters` equivalent. **Assume nothing; check.**
- The "not yet" `abi3t` entries for Cython, pybind11 and nanobind mean *"I found no
  changelog entry"*, not *"the maintainers have said no"*. 3.15 is at rc1; this will change.
- nanobind's 4×/5×/10× headline figures are the project's own published benchmark numbers,
  reproduced here as attributed claims, not as measurements of mine.
- The macOS `-undefined dynamic_lookup` deprecation history: I know the flag compiled clean
  with **Apple clang 21.0.0 on this SDK** *(measured)*. The broader claim that newer Xcode
  linkers warn on it is from memory of the toolchain's release notes and I did not
  reproduce a warning here.

---

*Next: [`18-lexer-parser-ast.md`](18-lexer-parser-ast.md) begins Tier 3 and goes back up the
stack — but if this document was interesting, the two docs that actually continue it are
[`26-free-threading.md`](26-free-threading.md) (§11 at service scale) and
[`34-going-native.md`](34-going-native.md) (§9 and §13 applied to a real numerical
workload). And do Lab 5 before you ship a free-threaded extension.*
