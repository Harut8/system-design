# 20 — The eval loop: dispatch, specialization, and the frame

> **Tier 3, doc 20.** Prerequisites: [`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md)
> (code objects, wordcode, `CACHE` pseudo-instructions, exception tables — this document
> assumes all of it and does not repeat any of it),
> [`00-cpu-execution-model.md`](00-cpu-execution-model.md) (branch prediction, indirect
> branches, µop cache), [`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md)
> (owned vs borrowed), [`16-object-memory-layout.md`](16-object-memory-layout.md) (what a
> pointer dereference costs). Feeds into: [`21-tier2-and-jit.md`](21-tier2-and-jit.md),
> [`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md),
> [`28-asyncio-internals.md`](28-asyncio-internals.md),
> [`32-profiling.md`](32-profiling.md). Cross-reference:
> [`24-the-gil.md`](24-the-gil.md) §4 owns the eval-breaker / `gil_drop_request` handoff;
> §12 below only says where in the instruction stream the check lives.
>
> **THESIS: `_PyEval_EvalFrameDefault()` is not where the interpreter's behaviour is
> written, and has not been since 3.12.** The semantics of every instruction live in a
> C-like DSL in `Python/bytecodes.c`; a build-time program in `Tools/cases_generator/`
> compiles that DSL into `Python/generated_cases.c.h`, `Include/opcode_ids.h`,
> `Python/opcode_targets.h`, the tier-2 micro-op tables and the Python-visible metadata
> in `Lib/_opcode_metadata.py` — *from one source of truth*. That single decision is what
> made PEP 659 tractable: an instruction can be decomposed into guards + action, the
> guards can be shared, the same definition can emit a tier-1 instruction and a tier-2
> micro-op, and nothing can drift. **Everything else in this document — adaptive
> specialization, inline caches, deoptimization, the tail-call interpreter, inlined
> Python-to-Python calls — is downstream of "the interpreter is generated, not written."**

> **Verification provenance.** Two kinds of fact appear below and they are labelled
> differently.
>
> **Source facts** — every function name, macro, struct field, constant and code quotation
> was read from the **`3.14` branch of github.com/python/cpython** during the writing of
> this document, from the files named inline (`Python/bytecodes.c`, `Python/ceval.c`,
> `Python/ceval_macros.h`, `Python/generated_cases.c.h`, `Python/specialize.c`,
> `Python/flowgraph.c`, `Python/pystate.c`, `Include/internal/pycore_backoff.h`,
> `Include/internal/pycore_code.h`, `Include/internal/pycore_stackref.h`,
> `Include/internal/pycore_interpframe_structs.h`, `Include/cpython/pystate.h`,
> `Tools/cases_generator/README.md`, `InternalDocs/frames.md`,
> `InternalDocs/interpreter.md`). Quotations are verbatim.
>
> **Runtime facts** — anything marked *(real output)* was produced on the machine this repo
> lives on: **Apple M3 Pro, macOS, arm64, CPython 3.14.6** (`~/.local/bin/python3.14`),
> during writing. Opcode names and family membership were checked against
> `_opcode_metadata._specializations` on that interpreter, not recalled.
>
> **What is NOT here.** I did **not** build CPython with `--with-tail-call-interp`, did not
> run pyperformance, and did not measure a single interpreter-level speedup. **Every
> performance number in §3 is cited from a named source, and the disagreement between
> those sources is the point of that section.** Six further claims I could not verify are
> flagged inline (§3, §7, §9, §12, §13, §14) and listed again in §17.

## Contents

1. [What `_PyEval_EvalFrameDefault` actually is](#1-what-_pyeval_evalframedefault-actually-is)
2. [The DSL: the interpreter is generated, not written](#2-the-dsl-the-interpreter-is-generated-not-written)
3. [Dispatch: switch, computed gotos, and the 3.14 tail-call interpreter](#3-dispatch-switch-computed-gotos-and-the-314-tail-call-interpreter)
4. [Anatomy of an adaptive instruction](#4-anatomy-of-an-adaptive-instruction)
5. [The warmup counter, decoded byte by byte](#5-the-warmup-counter-decoded-byte-by-byte)
6. [Deoptimization, cooldown, and why polymorphic sites don't thrash](#6-deoptimization-cooldown-and-why-polymorphic-sites-dont-thrash)
7. [The guards: what a specialization is actually betting on](#7-the-guards-what-a-specialization-is-actually-betting-on)
8. [Frames: `_PyInterpreterFrame` and the chunked data stack](#8-frames-_pyinterpreterframe-and-the-chunked-data-stack)
9. [`f_locals`: PEP 558 withdrawn, PEP 667 shipped](#9-f_locals-pep-558-withdrawn-pep-667-shipped)
10. [PEP 523: the frame evaluation API, and what it costs everyone else](#10-pep-523-the-frame-evaluation-api-and-what-it-costs-everyone-else)
11. [The call protocol: `tp_call` → vectorcall → inlined Python calls](#11-the-call-protocol-tp_call--vectorcall--inlined-python-calls)
12. [`RESUME`, the eval breaker, and generators](#12-resume-the-eval-breaker-and-generators)
13. [`LOAD_FAST_BORROW`, `_PyStackRef`, and refcounting on the hot path](#13-load_fast_borrow-_pystackref-and-refcounting-on-the-hot-path)
14. [What all of this means when you open a profiler](#14-what-all-of-this-means-when-you-open-a-profiler)
15. [Lab exercises](#15-lab-exercises)
16. [Question bank](#16-question-bank)
17. [Unverified claims, collected](#17-unverified-claims-collected)
18. [Sources](#18-sources)

---

## 1. What `_PyEval_EvalFrameDefault` actually is

The signature, from `Python/ceval.c` on the 3.14 branch:

```c
PyObject* _Py_HOT_FUNCTION DONT_SLP_VECTORIZE
_PyEval_EvalFrameDefault(PyThreadState *tstate, _PyInterpreterFrame *frame, int throwflag)
```

Three things in that one line are worth stopping on.

**It takes a `_PyInterpreterFrame*`, not a `PyFrameObject*`.** PEP 523's published
signature was `PyObject* (*)(PyFrameObject*, int)`. It is now
`PyObject* (*)(PyThreadState*, struct _PyInterpreterFrame*, int)` — verified in
`Include/cpython/pystate.h`. The heap frame object is gone from the hot path entirely
(§8), and the PEP's text is stale (§10).

**`DONT_SLP_VECTORIZE` is a compiler workaround with a bug number attached.** From
`ceval.c`:

```c
#if (defined(__GNUC__) && __GNUC__ >= 10 && !defined(__clang__)) && defined(__x86_64__)
/*
 * gh-129987: The SLP autovectorizer can cause poor code generation for
 * opcode dispatch in some GCC versions (observed in GCCs 12 through 15,
 * ...
 */
#define DONT_SLP_VECTORIZE __attribute__((optimize ("no-tree-slp-vectorize")))
```

There is a second one immediately above it: on MSVC before 19.43 with PGO, the function is
compiled with `#pragma optimize("t", off)` because it is *too large to optimize for speed*.
The eval loop is a function so big and so hot that mainstream compilers mis-handle it, and
CPython carries per-compiler escape hatches in the source. Keep that in mind for §3 — it is
the same phenomenon that produced the tail-call interpreter's headline number.

**`throwflag` is the generator `.throw()` entry point.** A non-zero `throwflag` means "an
exception is already set; do not execute `RESUME`, go straight to unwinding." That single
parameter is the whole reason generator resumption doesn't need a separate interpreter.

### The shape of the function body

The body is roughly:

1. Recursion check (`_Py_EnterRecursiveCallTstate`).
2. Declare the **local "register" variables** — `next_instr`, `stack_pointer`, `opcode`,
   `oparg`. `ceval.c` comments them exactly that way: *"Local 'register' variables. These
   are cached values from the frame and code object."* The instruction pointer and stack
   pointer live in C locals, not in the frame, for the duration of execution; they are
   spilled back to `frame->instr_ptr` / `frame->stackpointer` only when something needs to
   see them.
3. Set up an `_PyEntryFrame entry` — a **shim frame** on the C stack (§8).
4. `goto start_frame;` immediately followed by `#include "generated_cases.c.h"`.

That include is the punchline of the whole document. **The instruction implementations are
textually pasted into the middle of the function from a generated file**, and that file is
not written by hand.

```c
#if defined(_Py_TIER2) && !defined(_Py_JIT)
    /* Tier 2 interpreter state */
    _PyExecutorObject *current_executor = NULL;
    const _PyUOpInstruction *next_uop = NULL;
#endif
#if Py_TAIL_CALL_INTERP
    ...
#else
    goto start_frame;
#   include "generated_cases.c.h"
#endif
```

The labels the generated code jumps to are declared at the bottom of `bytecodes.c` and are
therefore also generated: `dispatch_opcode`, `error`, `exception_unwind`, `exit_unwind`,
`handle_eval_breaker`, `resume_frame`, `start_frame`, `unbound_local_error`.

---

## 2. The DSL: the interpreter is generated, not written

This is the most misunderstood part of modern CPython, and the fastest way to understand it
is to read the top of `Python/bytecodes.c`:

```c
// This file contains instruction definitions.
// It is read by generators stored in Tools/cases_generator/
// to generate Python/generated_cases.c.h and others.
// Note that there is some dummy C code at the top and bottom of the file
// to fool text editors like VS Code into believing this is valid C code.
// The actual instruction definitions start at // BEGIN BYTECODES //.
// See Tools/cases_generator/README.md for more information.
```

And then, a few dozen lines later, the "dummy C code" that makes the file compile-ish so
editors and linters don't riot:

```c
#define USE_COMPUTED_GOTOS 0
#include "ceval_macros.h"

/* Flow control macros */

#define inst(name, ...) case name:
#define op(name, ...) /* NAME is ignored */
#define macro(name) static int MACRO_##name
#define super(name) static int SUPER_##name
#define family(name, ...) static int family_##name
#define pseudo(name) static int pseudo_##name
#define label(name) name:
```

**`bytecodes.c` is never compiled into the interpreter.** It is *parsed*. The parser is
`Tools/cases_generator/parsing.py` (recursive descent with unlimited backtracking, on top
of `lexer.py`, a C lexer originally written by Mark Shannon), and the README states plainly:
*"We do not run the C preprocessor."*

### The DSL, in its own words

The grammar is specified in `Tools/cases_generator/interpreter_definition.md`. The parts you
need to read real definitions:

```
  object:
    NAME [":" type] [ "if" "(" C-expression ")" ]
  stream:
    NAME "/" size
  array:
    object "[" C-expression "]"
  family:
    "family" "(" NAME ")" = "{" NAME ("," NAME)+ [","] "}" ";"
```

> * `inst`: A normal instruction, as previously defined by `TARGET(NAME)` in `ceval.c`.
> * `op`: A part instruction from which macros can be constructed.
> * `macro`: A bytecode instruction constructed from ops and cache effects.
>
> The objects before the "`--`" are the objects on top of the stack at the start of the
> instruction. Those after the "`--`" are the objects on top of the stack at the end of the
> instruction. […] The number in a `stream` define how many codeunits are consumed from the
> instruction stream.

So `(counter/1, owner -- owner)` means: consume 1 code unit of inline cache named `counter`,
pop `owner`, push `owner` back. `(left, right -- res)` means pop two, push one. **The stack
effect is declarative.** Nobody writes `stack_pointer -= 2` by hand; `stack.py` computes it,
which is also where `opcode.stack_effect()` from doc 19 §7 comes from.

### One instruction, three forms

Here is `LOAD_ATTR_INSTANCE_VALUE` — the specialization doc 19 §5 showed appearing in
`dis(adaptive=True)` output. First, **what a human wrote**, in `bytecodes.c`:

```c
        op(_GUARD_TYPE_VERSION, (type_version/2, owner -- owner)) {
            PyTypeObject *tp = Py_TYPE(PyStackRef_AsPyObjectBorrow(owner));
            assert(type_version != 0);
            EXIT_IF(FT_ATOMIC_LOAD_UINT_RELAXED(tp->tp_version_tag) != type_version);
        }

        op(_CHECK_MANAGED_OBJECT_HAS_VALUES, (owner -- owner)) {
            PyObject *owner_o = PyStackRef_AsPyObjectBorrow(owner);
            assert(Py_TYPE(owner_o)->tp_dictoffset < 0);
            assert(Py_TYPE(owner_o)->tp_flags & Py_TPFLAGS_INLINE_VALUES);
            DEOPT_IF(!FT_ATOMIC_LOAD_UINT8(_PyObject_InlineValues(owner_o)->valid));
        }

        op(_LOAD_ATTR_INSTANCE_VALUE, (offset/1, owner -- attr)) {
            PyObject *owner_o = PyStackRef_AsPyObjectBorrow(owner);
            PyObject **value_ptr = (PyObject**)(((char *)owner_o) + offset);
            PyObject *attr_o = FT_ATOMIC_LOAD_PTR_ACQUIRE(*value_ptr);
            DEOPT_IF(attr_o == NULL);
            ...
            STAT_INC(LOAD_ATTR, hit);
            PyStackRef_CLOSE(owner);
        }

        macro(LOAD_ATTR_INSTANCE_VALUE) =
            unused/1 + // Skip over the counter
            _GUARD_TYPE_VERSION +
            _CHECK_MANAGED_OBJECT_HAS_VALUES +
            _LOAD_ATTR_INSTANCE_VALUE +
            unused/5 +
            _PUSH_NULL_CONDITIONAL;
```

That last block is the DSL doing the thing that matters: **an instruction is an addition of
micro-operations plus cache padding.** `unused/1` skips the counter. `unused/5` skips the
five cache code units this specialization doesn't need — because
`InternalDocs/interpreter.md` requires that *"all members of the family must have the same
number of inline cache entries, to ensure correct execution"* (doc 19 §4: `LOAD_ATTR` has 9).

Second, **what the generator emitted**, verbatim from `Python/generated_cases.c.h`:

```c
        TARGET(LOAD_ATTR_INSTANCE_VALUE) {
            #if Py_TAIL_CALL_INTERP
            int opcode = LOAD_ATTR_INSTANCE_VALUE;
            (void)(opcode);
            #endif
            _Py_CODEUNIT* const this_instr = next_instr;
            (void)this_instr;
            frame->instr_ptr = next_instr;
            next_instr += 10;
            INSTRUCTION_STATS(LOAD_ATTR_INSTANCE_VALUE);
            static_assert(INLINE_CACHE_ENTRIES_LOAD_ATTR == 9, "incorrect cache size");
            _PyStackRef owner;
            _PyStackRef attr;
            _PyStackRef *null;
            /* Skip 1 cache entry */
            // _GUARD_TYPE_VERSION
            {
                owner = stack_pointer[-1];
                uint32_t type_version = read_u32(&this_instr[2].cache);
                PyTypeObject *tp = Py_TYPE(PyStackRef_AsPyObjectBorrow(owner));
                assert(type_version != 0);
                if (FT_ATOMIC_LOAD_UINT_RELAXED(tp->tp_version_tag) != type_version) {
                    UPDATE_MISS_STATS(LOAD_ATTR);
                    assert(_PyOpcode_Deopt[opcode] == (LOAD_ATTR));
                    JUMP_TO_PREDICTED(LOAD_ATTR);
                }
            }
            // _CHECK_MANAGED_OBJECT_HAS_VALUES
            { ... }
            // _LOAD_ATTR_INSTANCE_VALUE
            {
                uint16_t offset = read_u16(&this_instr[4].cache);
                ...
            }
            /* Skip 5 cache entries */
            // _PUSH_NULL_CONDITIONAL
            { ... }
            stack_pointer += (oparg & 1);
            assert(WITHIN_STACK_BOUNDS());
            DISPATCH();
        }
```

Compare the two. Everything mechanical was filled in by the generator:

| Written in the DSL | Generated into C |
|---|---|
| `(type_version/2, owner -- owner)` | `owner = stack_pointer[-1];` and `read_u32(&this_instr[2].cache)` — **the cache offset `[2]` was computed from the `unused/1` before it** |
| `EXIT_IF(...)` / `DEOPT_IF(...)` | `UPDATE_MISS_STATS(LOAD_ATTR); JUMP_TO_PREDICTED(LOAD_ATTR);` |
| the `macro(...) = a + b + c` sum | `next_instr += 10;` (1 opcode + 9 caches) and the `static_assert` |
| nothing | `stack_pointer += (oparg & 1); assert(WITHIN_STACK_BOUNDS());` |

**Every place a human could get an offset wrong, the generator computes it.** Doc 19 §7's
SIGSEGV came from a hand-edited `co_stacksize`; this is the same class of bug, eliminated at
the source level. `read_u32(&this_instr[2].cache)` is where the type version from doc 19 §4's
`CACHE 0 (version: 0)` slot is finally read.

### What else comes out of the same file

From `Makefile.pre.in`, `make regen-cases` runs eight generators over `bytecodes.c`:

| Generator | Output |
|---|---|
| `opcode_id_generator.py` | `Include/opcode_ids.h` — the opcode *numbers* (doc 19 §13: they move every release) |
| `target_generator.py` | `Python/opcode_targets.h` — the computed-goto jump table (§3) |
| `tier1_generator.py` | `Python/generated_cases.c.h` |
| `tier2_generator.py` | the tier-2 micro-op interpreter ([`21-tier2-and-jit.md`](21-tier2-and-jit.md)) |
| `optimizer_generator.py` | `Python/optimizer_cases.c.h`, from `bytecodes.c` **+** `Python/optimizer_bytecodes.c` |
| `opcode_metadata_generator.py` | `Include/internal/pycore_opcode_metadata.h` |
| `uop_id_generator.py`, `uop_metadata_generator.py` | `Include/internal/pycore_uop_ids.h`, `pycore_uop_metadata.h` |
| `py_metadata_generator.py` | **`Lib/_opcode_metadata.py`** — which is where `_specializations` comes from |

That last row closes a loop from doc 19: the `_opcode_metadata._specializations` dict you
introspect from pure Python is generated from the same `family(...)` declarations the C
interpreter is generated from. When you print it, you are reading `bytecodes.c`.

**This is why "add an opcode" is a tractable capstone** (README §12, doc 19 lab 8): you edit
one DSL block, run `make regen-cases`, and the opcode number, the jump-table entry, the
stack-effect table, the tier-2 micro-op and the Python-visible metadata all update together.
In 3.10 that was eight files edited by hand.

---

## 3. Dispatch: switch, computed gotos, and the 3.14 tail-call interpreter

Everything above generated the *bodies*. This section is about how control gets from one
body to the next. All of it is in `Python/ceval_macros.h`.

The core is three macros:

```c
#define NEXTOPARG()  do { \
        _Py_CODEUNIT word  = {.cache = FT_ATOMIC_LOAD_UINT16_RELAXED(*(uint16_t*)next_instr)}; \
        opcode = word.op.code; \
        oparg = word.op.arg; \
    } while (0)

#define DISPATCH() \
    { \
        assert(frame->stackpointer == NULL); \
        NEXTOPARG(); \
        PRE_DISPATCH_GOTO(); \
        DISPATCH_GOTO(); \
    }
```

`NEXTOPARG()` is doc 19 §2's wordcode paying off: **one 16-bit load, two field extracts, no
length decoding.** `assert(frame->stackpointer == NULL)` is the invariant that the stack
pointer currently lives in a C local and has *not* been spilled to the frame.

`DISPATCH_GOTO()` has three definitions, selected at build time.

### 3a. `switch` — the fallback

```c
#  define TARGET(op) case op: TARGET_##op:
#  define DISPATCH_GOTO() goto dispatch_opcode
```

One `switch` at the top of a loop. Every instruction ends by jumping back to **one shared
indirect branch**. On a CPU with a per-branch-address predictor, that single site sees the
whole opcode mix, so its prediction accuracy is roughly the entropy of the program's opcode
stream — poor. See [`00-cpu-execution-model.md`](00-cpu-execution-model.md) for why a
mispredict costs what it costs.

### 3b. Computed gotos — the default since 3.1

```c
#  define TARGET(op) TARGET_##op:
#  define DISPATCH_GOTO() goto *opcode_targets[opcode]
```

`opcode_targets` is a generated table of GNU C label addresses, from
`Python/opcode_targets.h`:

```c
#if !Py_TAIL_CALL_INTERP
static void *opcode_targets[256] = {
    &&TARGET_CACHE,
    &&TARGET_BINARY_SLICE,
    &&TARGET_BUILD_TEMPLATE,
    ...
```

Note where it is included — **inside the function body**, because `&&label` is only valid
there:

```c
#if USE_COMPUTED_GOTOS && !Py_TAIL_CALL_INTERP
/* Import the static jump table */
#include "opcode_targets.h"
#endif
```

The mechanism, and the *intended* mechanism, is **indirect threading**: every instruction
body ends with its *own* indirect branch, so the predictor sees N distinct branch sites and
can learn per-site opcode-pair correlations (after `FOR_ITER` usually comes `STORE_FAST`;
after `LOAD_FAST` usually comes `LOAD_CONST`). Selection is by build:

```c
#ifdef HAVE_COMPUTED_GOTOS
    #ifndef USE_COMPUTED_GOTOS
    #define USE_COMPUTED_GOTOS 1
    #endif
#else
    #if defined(USE_COMPUTED_GOTOS) && USE_COMPUTED_GOTOS
    #error "Computed gotos are not supported on this compiler."
    #endif
```

### 3c. The tail-call interpreter — new in 3.14

```c
#if Py_TAIL_CALL_INTERP
#   if defined(__clang__) || defined(__GNUC__)
#       if !_Py__has_attribute(preserve_none) || !_Py__has_attribute(musttail)
#           error "This compiler does not have support for efficient tail calling."
#       endif
#   elif defined(_MSC_VER)
#       error "Tail calling not supported for MSVC."
#   endif

#   define Py_MUSTTAIL [[clang::musttail]]
#   define Py_PRESERVE_NONE_CC __attribute__((preserve_none))
    Py_PRESERVE_NONE_CC typedef PyObject* (*py_tail_call_funcptr)(TAIL_CALL_PARAMS);

#   define TARGET(op) Py_PRESERVE_NONE_CC PyObject *_TAIL_CALL_##op(TAIL_CALL_PARAMS)
#   define DISPATCH_GOTO() \
        do { \
            Py_MUSTTAIL return (INSTRUCTION_TABLE[opcode])(TAIL_CALL_ARGS); \
        } while (0)
#    define LABEL(name) TARGET(name)
```

with

```c
#   define TAIL_CALL_PARAMS _PyInterpreterFrame *frame, _PyStackRef *stack_pointer, \
                            PyThreadState *tstate, _Py_CODEUNIT *next_instr, int oparg
```

**The same `TARGET(op)` macro that was a label is now a function definition.** Each
instruction becomes its own C function `_TAIL_CALL_LOAD_ATTR(...)`; dispatch is a guaranteed
tail call through a function-pointer table. Even the *labels* become functions —
`LABEL(name)` is `TARGET(name)`, so `start_frame` and `error` are `_TAIL_CALL_start_frame`
and `_TAIL_CALL_error`, which is why `ceval.c` contains:

```c
#if Py_TAIL_CALL_INTERP
#   if Py_STATS
        return _TAIL_CALL_start_frame(frame, NULL, tstate, NULL, 0, lastopcode);
#   else
        return _TAIL_CALL_start_frame(frame, NULL, tstate, NULL, 0);
#   endif
#else
    goto start_frame;
#endif
```

Two attributes make it work and both are load-bearing:

- **`musttail`** — the compiler must emit a jump, not a call. Without it the C stack grows
  by one frame per bytecode and the interpreter dies in microseconds.
- **`preserve_none`** — a calling convention in which the callee preserves *no* registers,
  so the five hot values (`frame`, `stack_pointer`, `tstate`, `next_instr`, `oparg`) stay
  pinned in registers across the whole interpreter instead of being spilled at each
  boundary. This is the actual optimization; the tail call is the delivery mechanism.

Requirements, from the 3.14 What's New: **Clang 19+ on x86-64 or AArch64**, opt-in via
`--with-tail-call-interp`, PGO strongly recommended. MSVC is a hard `#error`.

### 3d. The performance story — and why it belongs in this document

The 3.14 What's New says:

> Preliminary benchmarks suggest a geometric mean of 3-5% faster on the standard
> `pyperformance` benchmark suite, depending on platform and architecture. **The baseline is
> Python 3.14 built with Clang 19, without this new interpreter.**

That bolded sentence is doing enormous work, and the story behind it is the best worked
example of baseline selection in this entire folder.

The change was **announced at 10–15%**. Nelson Elhage spent roughly three weeks
benchmarking and found the gain was *primarily an accidental workaround for a regression in
LLVM 19*. His headline table, with `clang18` as the baseline, all builds LTO+PGO (his
figures, not mine — I measured nothing):

| Platform | clang18 | clang19 | clang19.taildup | clang19.tc | gcc |
|---|---|---|---|---|---|
| Raptor Lake i5-13500 | (ref) | 1.09× slower | 1.01× faster | **1.03× faster** | 1.02× faster |
| Apple M1 MacBook Air | (ref) | 1.12× slower | 1.02× slower | **1.00× slower** | N/A |

`clang19.taildup` is clang 19 with computed gotos plus `-mllvm` flags that undo the
regression. Read the table as: **clang 19 made the computed-goto interpreter 9–12% slower;
the tail-call interpreter is immune to that regression; measured against a healthy baseline
the tail-call interpreter is worth about 0–3%.**

There is a second table in the same post that is arguably more interesting, comparing
against the still-supported `switch` build (`.nocg`):

| | clang18 | clang18.nocg | clang19.nocg | clang19 |
|---|---|---|---|---|
| Performance change | (ref) | 1.01× faster | 1.02× slower | 1.09× slower |

**The `switch` interpreter built with clang 18 was 1% *faster* than the computed-goto one.**
Elhage's explanation: modern Clang compiles the `switch` into a jump table and then performs
tail duplication *anyway*, replicating the dispatch into each opcode body — exactly the
transformation computed gotos were hand-written to force. He counts indirect jumps in the
binary: clang18 has 332, clang19 has 3. LLVM 19 stopped doing the duplication; that is the
regression. He also cites the literature: the historical claim for dispatch replication ran
from 20% to 100%, but more recent work on modern branch predictors puts it at **2–4%**, and
his own `.nocg` numbers reproduce that 2%.

**Three staff-level lessons, and they are the reason this section is long.**

1. **"3–5% faster" and "10–15% faster" were both honestly measured.** They differ only in
   baseline. Doc 31's entire thesis, appearing in the CPython core team's own release notes.
2. **The eval loop's performance is substantially a property of your compiler**, not of
   CPython. Two `#pragma`/`__attribute__` workarounds in `ceval.c` (§1) say the same thing.
   If you ship a self-built Python, your interpreter speed depends on a toolchain choice
   nobody wrote down.
3. **Computed gotos may be obsolete.** They exist to force a transformation compilers now
   perform themselves. That is not a settled question, but it is an open one in the source.

> **Could not verify:** I did not reproduce any of the above. I have no Clang 19 build, no
> `--with-tail-call-interp` build, and no pyperformance run. The numbers are Elhage's and
> the CPython docs', reproduced with attribution. I also did **not** verify Elhage's
> indirect-jump counts or his claim about GCC 15 supporting `musttail`. Cross-check against
> his data repository before repeating any of it, and note his post is dated **March 2025**
> — LLVM has had many releases since.

---

## 4. Anatomy of an adaptive instruction

Now the mechanism doc 19 §4 could only photograph sitting still.

PEP 659's model has three moving parts, all of which you have now seen in source:

> Quickening is the process of replacing slow instructions with faster variants. […]
> Quickened code has a number of advantages over immutable bytecode: it can be changed at
> runtime; it can use super-instructions that span lines and take multiple operands; it does
> not need to handle tracing as it can fallback to the original bytecode for that.

**One 3.14 correction to the PEP's vocabulary.** PEP 659 (2021) describes a separate
"quickened" copy of the bytecode. That is not how it works now: since 3.12 there is one
mutable array, `co_code_adaptive`, allocated inline with the code object (doc 19 §1), and
the *compiler* emits the adaptive form directly. The word "quickening" survives in exactly
one place in 3.14 — `_QUICKEN_RESUME`, which promotes `RESUME` to `RESUME_CHECK` (§12).
`co_code` reconstructs the unspecialized bytes on demand (doc 19 §5).

### The generic form is itself a macro

```c
macro(BINARY_OP) = _SPECIALIZE_BINARY_OP + unused/4 + _BINARY_OP;

macro(LOAD_ATTR) = _SPECIALIZE_LOAD_ATTR + unused/8 + _LOAD_ATTR;
```

So the *unspecialized* `LOAD_ATTR` is: a specializing op that consumes the counter, eight
unused cache units, then the generic implementation. And the specializing op:

```c
        specializing op(_SPECIALIZE_LOAD_ATTR, (counter/1, owner -- owner)) {
            #if ENABLE_SPECIALIZATION_FT
            if (ADAPTIVE_COUNTER_TRIGGERS(counter)) {
                PyObject *name = GETITEM(FRAME_CO_NAMES, oparg>>1);
                next_instr = this_instr;
                _Py_Specialize_LoadAttr(owner, next_instr, name);
                DISPATCH_SAME_OPARG();
            }
            OPCODE_DEFERRED_INC(LOAD_ATTR);
            ADVANCE_ADAPTIVE_COUNTER(this_instr[1].counter);
            #endif  /* ENABLE_SPECIALIZATION_FT */
        }
```

Read it as a four-line algorithm:

1. If the counter has hit zero, **rewind `next_instr` to this instruction**, call
   `_Py_Specialize_LoadAttr()` (in `Python/specialize.c`) which **overwrites the opcode byte
   in place**, then `DISPATCH_SAME_OPARG()` — re-dispatch the *same* instruction, which now
   has a different opcode and therefore runs the specialized body immediately. Nothing is
   deferred to the next iteration.
2. Otherwise decrement the counter and fall through to the generic implementation.

`DISPATCH_SAME_OPARG()` is why specialization is free of any "warm up on the next call"
delay:

```c
#define DISPATCH_SAME_OPARG() \
    { \
        opcode = next_instr->op.code; \
        PRE_DISPATCH_GOTO(); \
        DISPATCH_GOTO(); \
    }
```

Note `oparg` is deliberately *not* reloaded — the specialized form shares the generic form's
oparg by construction.

`oparg>>1` is doc 19 §12's low-bit trick, reappearing exactly where it is decoded.

### The dispatch loop, drawn with its caches

```
   _PyEval_EvalFrameDefault(tstate, frame, throwflag)
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │ C locals held in registers for the whole loop:                               │
   │   next_instr  ──▶ into co_code_adaptive        stack_pointer ──▶ into frame  │
   │   tstate      ──▶ thread state                 oparg / opcode                │
   └───────────────────────────────┬──────────────────────────────────────────────┘
                                   │
                  ┌────────────────▼─────────────────┐
                  │ NEXTOPARG(): ONE 16-bit load     │◀────────────────────────┐
                  │   opcode = word.op.code          │                         │
                  │   oparg  = word.op.arg           │                         │
                  └────────────────┬─────────────────┘                         │
                                   │                                           │
                  ┌────────────────▼─────────────────┐                         │
                  │ DISPATCH_GOTO()                  │                         │
                  │  switch : goto dispatch_opcode   │  ← 1 indirect branch    │
                  │  cgoto  : goto *opcode_targets[] │  ← N indirect branches  │
                  │  tailcall: musttail INSTRUCTION_ │  ← N indirect calls,    │
                  │            TABLE[opcode](...)    │    regs pinned          │
                  └────────────────┬─────────────────┘                         │
                                   │                                           │
   co_code_adaptive:               ▼                                           │
   ┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐     │
   │LOAD_ │LOAD_ │count-│type_version │keys_version │  descr (8 bytes)   │     │
   │FAST_ │ATTR  │ er   │  (4 bytes)  │  (4 bytes)  │                    │     │
   │BORROW│      │(2 B) │             │             │                    │     │
   └──────┴───┬──┴───┬──┴──────┬──────┴──────┬──────┴─────────┬──────────┘     │
              │      │         │             │                │                │
     this_instr      │  this_instr[1]  this_instr[2]    this_instr[4]           │
                     │         │             │                │                │
        ┌────────────▼─────────▼─────────────▼────────────────▼─────────────┐   │
        │  BODY of LOAD_ATTR_INSTANCE_VALUE (generated_cases.c.h)           │   │
        │   1. read_u32(&this_instr[2].cache)  -> type_version              │   │
        │   2. GUARD: Py_TYPE(owner)->tp_version_tag == type_version ?      │   │
        │        NO ──▶ UPDATE_MISS_STATS + JUMP_TO_PREDICTED(LOAD_ATTR) ───┼──▶│  deopt:
        │   3. GUARD: inline values still valid ?     NO ──▶ same           │   │  run the
        │   4. read_u16(&this_instr[4].cache)   -> offset                   │   │  GENERIC
        │   5. attr = *(PyObject**)((char*)owner + offset)   ← ONE add,     │   │  LOAD_ATTR,
        │                                                     ONE load      │   │  which
        │   6. stack_pointer[-1] = attr                                     │   │  decrements
        │   7. next_instr += 10;  DISPATCH()  ───────────────────────────────┼───┘  the counter
        └───────────────────────────────────────────────────────────────────┘
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
              Steps 1 and 4 are reads of memory 4 and 8 bytes past the
              instruction being executed — the SAME cache line the
              instruction fetch already pulled in. That is the entire
              argument for inline caches over a side table.
```

The two cache reads in that body are the payoff for doc 19 §4's observation that 79% of a
real function's bytecode is cache space. A side table would have been a pointer chase into a
cold line; this is a displacement off a pointer the CPU has already loaded.

---

## 5. The warmup counter, decoded byte by byte

The counter is 16 bits and it is not a plain countdown. From
`Include/internal/pycore_backoff.h`:

> The 16-bit counter is structured as a 12-bit unsigned 'value' and a 4-bit 'backoff' field.
> When resetting the counter, the backoff field is incremented (until it reaches a limit) and
> the value is set to a bit mask representing the value `2**backoff - 1`. The maximum backoff
> is 12 (the number of bits in the value). There is an exceptional value which must not be
> updated, `0xFFFF`.

```c
#define BACKOFF_BITS 4
#define MAX_BACKOFF 12
#define UNREACHABLE_BACKOFF 15

static inline _Py_BackoffCounter
advance_backoff_counter(_Py_BackoffCounter counter)
{
    _Py_BackoffCounter result;
    result.value_and_backoff = counter.value_and_backoff - (1 << BACKOFF_BITS);
    return result;
}

static inline bool
backoff_counter_triggers(_Py_BackoffCounter counter)
{
    /* Test whether the value is zero and the backoff is not UNREACHABLE_BACKOFF */
    return counter.value_and_backoff < UNREACHABLE_BACKOFF;
}
```

Two design notes fall out immediately. **"Decrement the value" is subtracting 16 from the
whole word** — one instruction, no shifting, no masking. **"Has it hit zero" is a single
unsigned comparison** against 15, which simultaneously tests the value *and* excludes the
`UNREACHABLE_BACKOFF` sentinel. The entire adaptive counter costs a subtract and a compare
per execution of an unspecialized instruction.

And the initial values, from `Include/internal/pycore_code.h`, quoted in full because the
comments explain the reasoning:

```c
// A value of 1 means that we attempt to specialize the *second* time each
// instruction is executed. Executing twice is a much better indicator of
// "hotness" than executing once, but additional warmup delays only prevent
// specialization. Most types stabilize by the second execution, too:
#define ADAPTIVE_WARMUP_VALUE 1
#define ADAPTIVE_WARMUP_BACKOFF 1

// A value of 52 means that we attempt to re-specialize after 53 misses (a prime
// number, useful for avoiding artifacts if every nth value is a different type
// or something). Setting the backoff to 0 means that the counter is reset to
// the same state as a warming-up instruction (value == 1, backoff == 1) after
// deoptimization. This isn't strictly necessary, but it is bit easier to reason
// about when thinking about the opcode transitions as a state machine:
#define ADAPTIVE_COOLDOWN_VALUE 52
#define ADAPTIVE_COOLDOWN_BACKOFF 0
```

### The counter, observed

`dis.Bytecode(f, show_caches=True)` exposes `cache_info`, and the counter is always the first
entry. So the state machine is directly observable from pure Python. Real 3.14.6 session,
`def hot(o): return o.k` against a plain instance *(real output)*:

```
cold      : LOAD_ATTR                counter = b'\x11\x00'
after 1   : LOAD_ATTR                counter = b'\x01\x00'
after 2   : LOAD_ATTR_INSTANCE_VALUE counter = b'@\x03'
after 62  : LOAD_ATTR_INSTANCE_VALUE counter = b'@\x03'
```

Decoding those four bytes against the source constants:

| observed | little-endian `uint16` | `value = w >> 4` | `backoff = w & 15` | matches |
|---|---|---|---|---|
| `b'\x11\x00'` | `0x0011` = **17** | 1 | 1 | `adaptive_counter_warmup()` = `(1<<4)｜1` = **17** ✓ |
| `b'\x01\x00'` | `0x0001` = **1** | 0 | 1 | `17 − 16 = 1`; `1 < 15` → **triggers next execution** ✓ |
| `b'@\x03'` | `0x0340` = **832** | 52 | 0 | `adaptive_counter_cooldown()` = `(52<<4)｜0` = **832** ✓ |

**Every observed byte matches a constant in `pycore_code.h` exactly**, and the "attempt to
specialize the *second* time" comment is confirmed: 17 → 1 after execution #1, and execution
#2 finds `1 < 15`, calls `_Py_Specialize_LoadAttr`, and re-dispatches into the specialized
body in the same instruction. Doc 19 §5's before/after disassembly is this, one execution at
a time.

The last row is the subtle one. **After specializing, the counter stops moving.** 60 further
calls leave it at 832 — because `macro(LOAD_ATTR_INSTANCE_VALUE)` begins `unused/1 +`, i.e.
the specialized body *skips* the counter and never advances it. The counter only ticks when
the generic form runs. So it does not count executions at all after specialization; it
counts **misses** (§6).

`RESUME` also flipped to `RESUME_CHECK` after the first call in that trace — a separate
mechanism (§12), on a separate trigger.

---

## 6. Deoptimization, cooldown, and why polymorphic sites don't thrash

A specialization is a bet. `Python/specialize.c` handles both settling up and re-betting:

```c
specialize(_Py_CODEUNIT *instr, uint8_t specialized_opcode)
{
    ...
    STAT_INC(_PyOpcode_Deopt[specialized_opcode], success);
    set_counter((_Py_BackoffCounter *)instr + 1, adaptive_counter_cooldown());
}

static inline void
unspecialize(_Py_CODEUNIT *instr)
{
    uint8_t opcode = FT_ATOMIC_LOAD_UINT8_RELAXED(instr->op.code);
    uint8_t generic_opcode = _PyOpcode_Deopt[opcode];
    STAT_INC(generic_opcode, failure);
    if (!set_opcode(instr, generic_opcode)) { ... }
    _Py_BackoffCounter *counter = (_Py_BackoffCounter *)instr + 1;
    _Py_BackoffCounter cur = load_counter(counter);
    set_counter(counter, adaptive_counter_backoff(cur));
}
```

`_PyOpcode_Deopt[]` is the generated table mapping any specialized opcode back to its family
head — the C-side twin of `_opcode_metadata._specializations`.

Two distinct failure modes, and conflating them is the most common misunderstanding of
PEP 659:

- **Specialization failure** (`unspecialize`): `_Py_Specialize_*` looked at the operands and
  found nothing it can specialize for — a `__getattr__`-overriding type, a non-string dict
  key, a C function with an unsupported flag combination. The instruction stays generic and
  the counter goes into **exponential backoff**: `restart_backoff_counter` sets
  `value = 2**(backoff+1) − 1` and increments `backoff`, up to `MAX_BACKOFF = 12`, i.e. up
  to 4095 executions between attempts. A site that can never be specialized costs
  asymptotically nothing.
- **A miss** (`DEOPT_IF` / `EXIT_IF` firing): the specialized instruction *exists* and its
  guard failed. The instruction is **not** rewritten. `JUMP_TO_PREDICTED(LOAD_ATTR)` runs
  the generic body instead, which advances the counter by one. Only after the counter
  reaches zero does the site re-specialize — for whatever type it happens to see then.

### The state machine

```
                        ┌──────────────────────────────────────────────┐
                        │  compiler emits the GENERIC opcode with a    │
                        │  counter cache slot = 17  (value 1, boff 1)  │
                        └────────────────────┬─────────────────────────┘
                                             │
                    ┌────────────────────────▼────────────────────────────┐
                    │            GENERIC / ADAPTIVE  (e.g. LOAD_ATTR)     │
     ┌─────────────▶│  _SPECIALIZE_LOAD_ATTR runs first, every time:      │
     │              │    ADAPTIVE_COUNTER_TRIGGERS(counter)?              │
     │              │      NO  → counter -= 16 ; run generic body         │
     │              │      YES → _Py_Specialize_LoadAttr(owner, ...)      │
     │              └──────┬──────────────────────────────┬──────────────┘
     │                     │ can specialize               │ cannot specialize
     │                     ▼                              ▼
     │        ┌────────────────────────┐    ┌───────────────────────────────────┐
     │        │ specialize():          │    │ unspecialize():                   │
     │        │  opcode byte := SPEC   │    │  opcode byte := generic (no-op)   │
     │        │  fill version/offset/  │    │  counter := restart_backoff(cur)  │
     │        │       descr caches     │    │    value = 2**(boff+1) - 1        │
     │        │  counter := 832        │    │    boff  = min(boff+1, 12)        │
     │        │      (value 52, b 0)   │    │  → 3, 7, 15, ... 4095 execs       │
     │        │  DISPATCH_SAME_OPARG() │    │    before the next attempt        │
     │        └───────────┬────────────┘    └───────────────┬───────────────────┘
     │                    │                                 │
     │                    ▼                                 └──────▶ back to GENERIC
     │   ┌──────────────────────────────────────────────────────┐
     │   │   SPECIALIZED  (e.g. LOAD_ATTR_INSTANCE_VALUE)       │
     │   │   macro = unused/1 + guards + action + unused/5      │
     │   │           ^^^^^^^^ counter SKIPPED — never ticks     │
     │   │                                                      │
     │   │   guard holds  → do the fast thing, DISPATCH()       │
     │   │        ↺ steady state: 2 compares + 1 add + 1 load   │
     │   └──────────────────────┬───────────────────────────────┘
     │                          │ guard FAILS  (DEOPT_IF / EXIT_IF)
     │                          ▼
     │   ┌──────────────────────────────────────────────────────┐
     │   │  UPDATE_MISS_STATS(LOAD_ATTR)                        │
     │   │  JUMP_TO_PREDICTED(LOAD_ATTR)                        │
     │   │    → the opcode byte is UNCHANGED; the generic body  │
     │   │      runs THIS time and decrements the counter by 1  │
     │   └──────────────────────┬───────────────────────────────┘
     │                          │
     └──────────────────────────┘  after 53 misses the counter reaches 0
                                   and the site re-specializes for whatever
                                   type it sees at that moment.
```

**The asymmetry is the design.** Specializing is cheap and eager (2 executions). Giving up is
slow and reluctant (53 misses). That ratio is what stops a bimodal call site from rewriting
its own opcode byte on every other iteration — which would be a store to the instruction
stream, an I-cache invalidation, and on some microarchitectures a µop-cache flush, in the
inner loop.

### Watching it thrash, deliberately

Real 3.14.6 session. One site, `def hot(o): return o.k`, alternating between a normal
instance (`LOAD_ATTR_INSTANCE_VALUE`) and a `__slots__` instance (`LOAD_ATTR_SLOT`)
*(real output)*:

```
cold          LOAD_ATTR                 counter= 17  value=1  backoff=1
3x A          LOAD_ATTR_INSTANCE_VALUE  counter=832  value=52 backoff=0
1x B (miss)   LOAD_ATTR_INSTANCE_VALUE  counter=816  value=51 backoff=0
1x A          LOAD_ATTR_INSTANCE_VALUE  counter=816  value=51 backoff=0
200x alt      LOAD_ATTR_SLOT            counter= 48  value=3  backoff=0
4000x alt     LOAD_ATTR_INSTANCE_VALUE  counter=816  value=51 backoff=0
```

Line by line: the miss on `B` cost exactly one counter decrement (832 → 816) and **did not
change the opcode**. The subsequent hit on `A` cost nothing and did not restore the counter —
the counter is monotone between re-specializations. After 200 alternating calls the site had
flipped to `LOAD_ATTR_SLOT`; after 4000 it was back on `LOAD_ATTR_INSTANCE_VALUE`. It is
oscillating with a period of 53 misses ≈ 106 calls, exactly as the constants predict.

**The cost model you should carry away from that table:** a 50/50 bimorphic site pays a guard
failure plus a full generic execution on ~half its executions, forever, and rewrites its
opcode roughly once per 106 executions. It is *not* catastrophic — but it is strictly worse
than either monomorphic case, and no amount of warmup fixes it. This is the mechanism behind
"split the loop by type" being a real optimization in Python and not cargo cult. Contrast:
`LOAD_GLOBAL` is described in `InternalDocs/interpreter.md` as *"near ideal,
`Nmiss/sum(Ni) ≈ 0`"*.

The same document gives the formula the core team uses to decide whether a specialization is
worth adding at all:

> `Tadaptive = (sum(Ti*Ni) + Tmiss*Nmiss)/(sum(Ni)+Nmiss)` […] The ideal situation is where
> misses are rare and the specialized forms are much faster than the base instruction.

### The families, on this interpreter

*(real output, 3.14.6, from `_opcode_metadata._specializations` — 17 families)*

| family | n | family | n |
|---|---|---|---|
| `CALL` | **20** | `TO_BOOL` | 6 |
| `BINARY_OP` | **15** | `FOR_ITER` | 4 |
| `LOAD_ATTR` | **13** | `COMPARE_OP`, `STORE_ATTR`, `UNPACK_SEQUENCE`, `CALL_KW` | 3 |
| | | `LOAD_GLOBAL`, `JUMP_BACKWARD`, `CONTAINS_OP`, `STORE_SUBSCR`, `LOAD_SUPER_ATTR`, `LOAD_CONST` | 2 |
| | | `RESUME`, `SEND` | 1 |

`family(LOAD_CONST, 0)` — with **zero** cache entries — is worth a second look. Its two
members are `LOAD_CONST_MORTAL` and `LOAD_CONST_IMMORTAL`, and it specializes without a
counter at all, using a compare-exchange on the opcode byte directly. That is the smallest
possible instance of the pattern: the "runtime information" being specialized on is nothing
more than whether the constant is immortal (PEP 683), which decides whether the load needs a
refcount operation. Same idea as §13.

---

## 7. The guards: what a specialization is actually betting on

Every specialized instruction opens with guards. There are only about five kinds, and knowing
them tells you exactly which source-level changes invalidate a hot loop.

**1. Type version tag.**

```c
op(_GUARD_TYPE_VERSION, (type_version/2, owner -- owner)) {
    PyTypeObject *tp = Py_TYPE(PyStackRef_AsPyObjectBorrow(owner));
    assert(type_version != 0);
    EXIT_IF(FT_ATOMIC_LOAD_UINT_RELAXED(tp->tp_version_tag) != type_version);
}
```

A 32-bit integer compare against a value stored in the inline cache. Every specialization
that resolved an attribute through a type carries one. `tp_version_tag` is bumped whenever
the type is modified — so **monkey-patching a class, or assigning to any class attribute,
invalidates every specialized attribute access on every instance of that class, everywhere in
the program.** That is the actual runtime cost of "we patch the class at import time"; it is
paid once, but it is global to the type.

There is a free-threading variant, `_GUARD_TYPE_VERSION_AND_LOCK`, which takes the object
lock before checking and unlocks on failure — the per-object locking from
[`26-free-threading.md`](26-free-threading.md) appearing inside a guard.

**2. Dict keys version.** For `LOAD_GLOBAL` and `LOAD_ATTR_MODULE`:

```c
op(_GUARD_GLOBALS_VERSION, (version/1 --)) {
    PyDictObject *dict = (PyDictObject *)GLOBALS();
    DEOPT_IF(!PyDict_CheckExact(dict));
    PyDictKeysObject *keys = FT_ATOMIC_LOAD_PTR_ACQUIRE(dict->ma_keys);
    DEOPT_IF(FT_ATOMIC_LOAD_UINT32_RELAXED(keys->dk_version) != version);
    assert(DK_IS_UNICODE(keys));
}
```

`dk_version` is on the **keys object**, not the dict — so *rebinding* an existing global does
not invalidate it (values change, keys don't), but *adding or deleting* a global does. That
is precisely why `LOAD_GLOBAL_MODULE` can then do:

```c
PyDictUnicodeEntry *entries = DK_UNICODE_ENTRIES(keys);
PyObject *res_o = FT_ATOMIC_LOAD_PTR_RELAXED(entries[index].me_value);
```

— an **array index with a cached index**, replacing a hash lookup in globals and possibly a
second in builtins (doc 19 §6's table). The classic "hoist `len` into a local" trick is
mostly obsolete because `LOAD_GLOBAL_BUILTIN` already turned it into two compares and a load.

> **Could not verify:** I have seen it claimed that specialization uses CPython's **dict
> watcher** API (`PyDict_AddWatcher`, added in 3.12) to invalidate global caches. Everything
> I read in `Python/bytecodes.c` on the 3.14 branch uses **version-tag comparison at
> execution time**, not a watcher callback. I did not audit `Python/specialize.c` or
> `Objects/dictobject.c` for a watcher-based invalidation path, so I cannot say whether one
> exists elsewhere (the tier-2 optimizer is a plausible home for it —
> [`21-tier2-and-jit.md`](21-tier2-and-jit.md)). Treat "dict watchers guard LOAD_GLOBAL" as
> **unconfirmed** and grep your own checkout.

**3. Object shape.** `_CHECK_MANAGED_OBJECT_HAS_VALUES` tests
`_PyObject_InlineValues(owner_o)->valid` — the inline-values array from
[`16-object-memory-layout.md`](16-object-memory-layout.md) §8 (key-sharing dicts). It goes
invalid when an instance's dict is materialized, e.g. by `obj.__dict__`, by adding an
attribute the shared keys don't have, or by `vars(obj)`. **Touching `__dict__` on a hot
object permanently deoptimizes attribute access on it.**

**4. Function version.** For calls:

```c
op(_CHECK_FUNCTION_VERSION, (func_version/2, callable, unused, unused[oparg] -- ...)) {
    EXIT_IF(!PyFunction_Check(callable_o));
    PyFunctionObject *func = (PyFunctionObject *)callable_o;
    EXIT_IF(func->func_version != func_version);
}
```

Per *function object*, not per code object. Reassigning `__defaults__`, `__code__` or the
function's globals bumps it. Decorators that rebuild a function at runtime therefore reset
every call site that had specialized on the old one.

**5. Capacity/interpreter-state checks**, which are not about types at all:

```c
op(_CHECK_PEP_523, (--))            { DEOPT_IF(tstate->interp->eval_frame); }
op(_CHECK_STACK_SPACE, (...))       { DEOPT_IF(!_PyThreadState_HasStackSpace(tstate, code->co_framesize)); }
op(_CHECK_RECURSION_REMAINING, (--)){ DEOPT_IF(tstate->py_recursion_remaining <= 1); }
```

The first of those is §10. It is worth noticing now that **a guard named after a PEP exists
in the instruction set**, and it is checked on every specialized Python-to-Python call.

---

## 8. Frames: `_PyInterpreterFrame` and the chunked data stack

Up to 3.10, every Python call heap-allocated a `PyFrameObject` — a full `PyObject` with a
header, GC tracking, and a `tp_dealloc`. `InternalDocs/interpreter.md`:

> Up through 3.10, the call stack was implemented as a singly-linked list of frame objects.
> This was expensive because each call would require a heap allocation for the stack frame.

Since 3.11 there are **two** structures. The one that executes, from
`Include/internal/pycore_interpframe_structs.h`:

```c
struct _PyInterpreterFrame {
    _PyStackRef f_executable; /* Deferred or strong reference (code object or None) */
    struct _PyInterpreterFrame *previous;
    _PyStackRef f_funcobj; /* Deferred or strong reference. Only valid if not on C stack */
    PyObject *f_globals; /* Borrowed reference. Only valid if not on C stack */
    PyObject *f_builtins; /* Borrowed reference. Only valid if not on C stack */
    PyObject *f_locals; /* Strong reference, may be NULL. Only valid if not on C stack */
    PyFrameObject *frame_obj; /* Strong reference, may be NULL. Only valid if not on C stack */
    _Py_CODEUNIT *instr_ptr; /* Instruction currently executing (or about to begin) */
    _PyStackRef *stackpointer;
#ifdef Py_GIL_DISABLED
    /* Index of thread-local bytecode containing instr_ptr. */
    int32_t tlbc_index;
#endif
    uint16_t return_offset;  /* Only relevant during a function call */
    char owner;
    ...
    /* Locals and stack */
    _PyStackRef localsplus[1];
};
```

**No `PyObject_HEAD`.** It is not an object. It has no refcount, no type pointer, is not GC
tracked, and `localsplus[1]` is a flexible array holding locals, cells, free variables *and*
the evaluation stack in one contiguous run — doc 19 §6's localsplus and doc 19 §7's
`co_stacksize` allocation, in one place.

`tlbc_index` under `#ifdef Py_GIL_DISABLED` is doc 19 §1's `co_tlbc` seen from the other
side: on a free-threaded build the frame remembers *which thread-local copy of the bytecode*
its `instr_ptr` points into, because specialization writes into the instruction stream and
two threads must not race on that.

### The chunked data stack

Frames are bump-allocated from per-thread chunks. From `Include/cpython/pystate.h`:

```c
/* Minimum size of data stack chunk */
#define _PY_DATA_STACK_CHUNK_SIZE (16*1024)
```

and `Python/pystate.c`:

```c
_PyInterpreterFrame *
_PyThreadState_PushFrame(PyThreadState *tstate, size_t size)
{
    if (_PyThreadState_HasStackSpace(tstate, (int)size)) {
        _PyInterpreterFrame *res = (_PyInterpreterFrame *)tstate->datastack_top;
        tstate->datastack_top += size;
        return res;
    }
    return push_chunk(tstate, (int)size);
}
```

**A Python function call, in the common case, is a pointer increment.** Not `malloc`, not
pymalloc, not even a freelist — `datastack_top += co_framesize`. `push_chunk` doubles the
allocation until the frame fits and keeps one `datastack_cached_chunk` in reserve so that a
call/return pattern straddling a chunk boundary doesn't allocate and free repeatedly.

```
PyThreadState
 ├── datastack_chunk ──▶ ┌───────────────────────── 16 KB chunk ─────────────────────────┐
 │                       │ hdr │ frame A            │ frame B        │ frame C   │ free  │
 │                       │     │ specials|locals|stk│ specials|...   │ ...       │       │
 │                       └─────┴────────────────────┴────────────────┴─────┬─────┴───┬───┘
 ├── datastack_top  ───────────────────────────────────────────────────────┘         │
 ├── datastack_limit ──────────────────────────────────────────────────────────────── ┘
 └── datastack_cached_chunk ──▶ (one chunk kept warm to avoid alloc/free churn)

  push:  if (top + co_framesize <= limit) { f = top; top += co_framesize; }   ← the fast path
         else push_chunk()                                                     ← rare

  Layout inside one frame (InternalDocs/frames.md): Specials · Locals · Stack
    "The specials have a fixed size, so the offset of the locals is known. The
     interpreter needs to hold two pointers, a frame pointer and a stack pointer."

  Generator/coroutine frames are NOT here — they are embedded in the generator object:
      struct _PyGenObject { PyObject_HEAD ... _PyInterpreterFrame gi_iframe; }
```

`InternalDocs/frames.md` even documents the layout they *rejected* (Locals · Specials · Stack,
used briefly in 3.11 alpha), because it would have avoided copying arguments at call time but
required a third pointer in registers. That is a real engineering trade recorded in the
repository, and it is a good answer to "why does CPython copy arguments into the frame?"

### The heap frame is created lazily

> When creating a backtrace or when calling `sys._getframe()` the frame becomes visible to
> Python code. When this happens a new `PyFrameObject` is created and a strong reference to
> it is placed in the `frame_obj` field of the specials section. The `frame_obj` field is
> initially `NULL`. […] This mechanism provides the appearance of persistent, heap-allocated
> frames for each activation, but with low runtime overhead.

If the `PyFrameObject` outlives the stack-allocated `_PyInterpreterFrame`, the frame is
**copied into it** — `take_ownership()` in `Python/frame.c`:

```c
_PyInterpreterFrame *new_frame = (_PyInterpreterFrame *)f->_f_frame_data;
_PyFrame_Copy(frame, new_frame);
new_frame->f_executable = PyStackRef_DUP(new_frame->f_executable);
f->f_frame = new_frame;
new_frame->owner = FRAME_OWNED_BY_FRAME_OBJECT;
```

and the `previous` links are converted into `PyFrameObject.f_back` links at the same time.
The `owner` field enumerates who is responsible for the storage:

```c
enum _frameowner {
    FRAME_OWNED_BY_THREAD = 0,
    FRAME_OWNED_BY_GENERATOR = 1,
    FRAME_OWNED_BY_FRAME_OBJECT = 2,
    FRAME_OWNED_BY_INTERPRETER = 3,
    FRAME_OWNED_BY_CSTACK = 4,
};
```

**The engineering consequence is direct and testable:** `sys._getframe()`,
`inspect.currentframe()`, `traceback.extract_stack()` and anything that walks frames are not
free introspection — each converts a pointer-bump activation record into a heap object with a
refcount and GC tracking. A logging call that captures the stack on every request is
materializing one `PyFrameObject` per frame per request. This is a real, avoidable production
cost, and it did not exist as a distinct cost before 3.11 because frames were *always*
objects.

### The shim frame

`FRAME_OWNED_BY_INTERPRETER` is the `_PyEntryFrame entry` from §1:

> On entry to `_PyEval_EvalFrameDefault()` a shim `_PyInterpreterFrame` is pushed. This frame
> is stored on the C stack, and popped when `_PyEval_EvalFrameDefault()` returns. This extra
> frame is inserted so that `RETURN_VALUE`, `YIELD_VALUE`, and `RETURN_GENERATOR` do not need
> to check whether the current frame is the entry frame. The shim frame points to a special
> code object containing the `INTERPRETER_EXIT` instruction which cleans up the shim frame
> and returns.

```c
entry.frame.instr_ptr = (_Py_CODEUNIT *)_Py_INTERPRETER_TRAMPOLINE_INSTRUCTIONS + 1;
entry.frame.owner = FRAME_OWNED_BY_INTERPRETER;
```

**A branch in the hottest instruction in the interpreter was replaced by a sentinel frame
containing a single fake instruction.** `RETURN_VALUE` unconditionally pops and jumps to the
caller's return address; when the caller is the shim, that address holds `INTERPRETER_EXIT`,
which returns from the C function. Same trick as a sentinel node in a linked list, applied to
the call stack.

> **Note on stale documentation.** `InternalDocs/interpreter.md` still describes this in
> terms of a `frame->is_entry` flag: *"There is a flag in the frame (`frame->is_entry`) that
> indicates whether the frame was inlined."* **That field does not exist in the 3.14
> struct** — it was replaced by `owner`. The internals docs are excellent and are, in places,
> a release or two behind. Read them for the *design*, and confirm field names in the header.

---

## 9. `f_locals`: PEP 558 withdrawn, PEP 667 shipped

Once locals live in a flat `_PyStackRef` array and there is no dict, `frame.f_locals` has to
be manufactured. The pre-3.13 answer was a **snapshot dict** plus a
`PyFrame_LocalsToFast()` C API to write changes back. PEP 667 is blunt about the result:

> This can result in the array and dictionary getting out of sync with each other. Writes to
> the `f_locals` frame attribute may not show up as modifications to local variables if
> `PyFrame_LocalsToFast()` is never called. Writes to local variables can get lost if a
> dictionary snapshot created before the variables were modified is written back to the frame
> (since *every* known variable stored in the snapshot is written back to the frame, even if
> the value stored on the frame had changed since the snapshot was taken).
>
> By making `frame.f_locals` return a view on the underlying frame, these problems go away.
> `frame.f_locals` is always in sync with the frame because it is a view of it, not a copy of
> it.

**Get the PEP numbers right.** There were two competing proposals:

| PEP | Author | Status | Landed |
|---|---|---|---|
| **558** — Defined semantics for `locals()` | Alyssa Coghlan | **Withdrawn** *(verified on peps.python.org)* | never |
| **667** — Consistent views of namespaces | Mark Shannon, Tian Gao | Accepted | **3.13** |

PEP 558 is frequently cited as the source of modern `f_locals` behaviour. It is not; it was
withdrawn in favour of 667. Both are worth reading — 558's analysis is what made 667
possible — but only one is implemented.

*(real output, 3.14.6)*

```
f_locals type: FrameLocalsProxy | module-level f_locals is globals: True
write-through result: 99
```

Three facts in three lines. In an **optimized** scope (`CO_OPTIMIZED`, doc 19 §6),
`f_locals` is a `FrameLocalsProxy` — a live view, and assigning through it genuinely changed
the local (`x` was `1`, the function returned `99`). At **module** scope, `f_locals` *is* the
globals dict, identity-equal. And in a class body — the third case — it is the real mapping
the class body executes in, which is why `__init_subclass__` machinery works at all
([`41-metaclasses-and-class-construction.md`](41-metaclasses-and-class-construction.md)).

The supporting fields are on `PyFrameObject`, not on `_PyInterpreterFrame`:
`f_extra_locals` (*"Dict for locals set by users using f_locals, could be NULL"*) and
`f_locals_cache`. That is where a key that has no localsplus slot goes.

> **Could not verify:** I did not test the write-through path against a *cell* or *free*
> variable, only a plain local, nor did I check `FrameLocalsProxy` behaviour on a suspended
> generator frame. PEP 667 specifies both; I only observed the simple case.

The connection back to §8 is worth stating explicitly: **`f_locals` on an optimized frame
requires a `PyFrameObject`**, so touching it materializes the heap frame. `sys.settrace`
handing you `frame.f_locals` on every line event is, mechanically, doc 32's 5.09× distortion.

---

## 10. PEP 523: the frame evaluation API, and what it costs everyone else

PEP 523 (Cannon & Viehland, 3.6) added a per-interpreter hook:

```c
/* Frame evaluation API */
typedef PyObject* (*_PyFrameEvalFunction)(PyThreadState *tstate, struct _PyInterpreterFrame *, int);

PyAPI_FUNC(_PyFrameEvalFunction) _PyInterpreterState_GetEvalFrameFunc(PyInterpreterState *interp);
PyAPI_FUNC(void) _PyInterpreterState_SetEvalFrameFunc(PyInterpreterState *interp,
                                                      _PyFrameEvalFunction eval_frame);
```

*(verified in `Include/cpython/pystate.h` on the 3.14 branch — note the signature differs
from the PEP text, which still shows `PyObject* (*)(PyFrameObject*, int)`. Doc 19's warning
about private APIs applies to the PEP itself.)*

Set `interp->eval_frame` and **you replace the interpreter**, per frame, from an extension
module. The PEP's own worked example is Pyjion, and the sketch it gives is still the shape
every user of this API has:

```python
def eval_frame(frame, throw_flag):
    pyjion_code = frame.code.co_extra
    ...
        elif pyjion_code.exec_count > 20_000:
            if jit_compile(frame): ...
    return _PyEval_EvalFrameDefault(frame, throw_flag)
```

`co_extra` — doc 19 §1's last struct field — exists for exactly this: a per-code-object void
pointer that a frame evaluator can hang its compiled artifact on.

The living users are **debuggers and profilers**, not JITs: `pydevd`/PyCharm and `debugpy`
use it to install per-code-object breakpoint trampolines with zero cost on code that has no
breakpoint. (PEP 768's remote debugging and PEP 669's `sys.monitoring` are the newer,
supported answers — [`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md).)

### The tax it imposes on everyone

Here is the part that is not in the PEP. Because a frame evaluator must see *every* frame, the
3.11+ inlined-call fast path **must not fire** when one is installed. So the source contains:

```c
op(_CHECK_PEP_523, (--)) {
    DEOPT_IF(tstate->interp->eval_frame);
}

macro(CALL_PY_EXACT_ARGS) =
    unused/1 + // Skip over the counter
    _CHECK_PEP_523 +
    _CHECK_FUNCTION_VERSION +
    _CHECK_FUNCTION_EXACT_ARGS +
    _CHECK_STACK_SPACE +
    _CHECK_RECURSION_REMAINING +
    _INIT_CALL_PY_EXACT_ARGS +
    _SAVE_RETURN_OFFSET +
    _PUSH_FRAME;
```

and the generic `_DO_CALL` checks it too, as part of the inlining condition:

```c
if (Py_TYPE(callable_o) == &PyFunction_Type &&
    tstate->interp->eval_frame == NULL &&
    ((PyFunctionObject *)callable_o)->vectorcall == _PyFunction_Vectorcall)
```

and `_PUSH_FRAME` asserts it:

```c
assert(tstate->interp->eval_frame == NULL);
```

**Two consequences, both of which show up as production incidents.**

1. Everyone who never uses PEP 523 pays one predictable load-and-test per specialized Python
   call. Cheap, but non-zero, and it is a *guard*, so on the free-threaded build it is a
   guard on shared interpreter state.
2. **Attaching a PEP 523 debugger deoptimizes every inlined Python-to-Python call in the
   process, permanently, process-wide.** Not "adds overhead to the function you're debugging"
   — `_CHECK_PEP_523` fails at *every* `CALL_PY_EXACT_ARGS` site, each of which then falls
   back through the generic path and pushes a C stack frame. This is a large part of why
   "the app is 3× slower under the debugger even with no breakpoints set" is a real and
   correct observation, and why `sys.monitoring` was designed to avoid the frame-eval hook
   entirely.

---

## 11. The call protocol: `tp_call` → vectorcall → inlined Python calls

Three generations of calling convention coexist in 3.14, and every `CALL` instruction picks
among them.

**Generation 1 — `tp_call`.** `PyObject *(*ternaryfunc)(PyObject *self, PyObject *args,
PyObject *kwargs)`. The caller must **build a tuple** of arguments and, if there are keywords,
a dict. For an interpreter that already has the arguments laid out contiguously on its
evaluation stack, that is a pure-loss allocation on every call.

**Generation 2 — vectorcall (PEP 590, 3.8).** From the PEP:

> The unused slot `printfunc tp_print` is replaced with `tp_vectorcall_offset`. It has the
> type `Py_ssize_t`. A new `tp_flags` flag is added, `_Py_TPFLAGS_HAVE_VECTORCALL`, which
> must be set for any class that uses the vectorcall protocol.

The signature takes `PyObject *const *args` — a pointer into an existing array — plus a count
with a flag ORed in:

> The flag `PY_VECTORCALL_ARGUMENTS_OFFSET` should be added to `n` if the callee is allowed to
> temporarily change `args[-1]`. […] Whenever they can do so cheaply (without allocation),
> callers are encouraged to use `PY_VECTORCALL_ARGUMENTS_OFFSET`. Doing so will allow
> callables such as bound methods to make their onward calls cheaply. **The bytecode
> interpreter already allocates space on the stack for the callable, so it can use this trick
> at no additional cost.**

That last sentence is doc 19 §12's low oparg bit, from the other end. `LOAD_ATTR` with the low
bit set pushes `NULL`-or-`self` *below* the arguments precisely so a bound method can write
`self` into `args[-1]` and pass the whole thing onward without copying. **The stack layout,
the oparg bit and the calling convention were co-designed.**

The eval loop's non-inlined path is literally one call:

```c
PyObject *res_o = PyObject_Vectorcall(
    callable_o, args_o,
    total_args | PY_VECTORCALL_ARGUMENTS_OFFSET,
    NULL);
```

**`METH_FASTCALL` is vectorcall for C functions.** From `Include/methodobject.h`:

```c
typedef PyObject *(*PyCFunctionFast) (PyObject *, PyObject *const *, Py_ssize_t);
#define METH_VARARGS  0x0001
#define METH_KEYWORDS 0x0002
#define METH_NOARGS   0x0004
#define METH_O        0x0008
#  define METH_FASTCALL  0x0080
#define METH_METHOD 0x0200
```

A `METH_VARARGS` function receives a tuple that CPython built for it and immediately
un-builds with `PyArg_ParseTuple`. A `METH_FASTCALL` function receives the interpreter's own
stack slice. The specialization families make the distinction visible: `CALL_BUILTIN_FAST`
and `CALL_BUILTIN_FAST_WITH_KEYWORDS` exist as separate specializations from
`CALL_BUILTIN_O`. **If you write C extensions, `METH_FASTCALL` vs `METH_VARARGS` decides
which specialization your function is eligible for** —
[`17-c-api-and-extensions.md`](17-c-api-and-extensions.md).

**Generation 3 — inlined Python-to-Python calls (3.11).** From
`InternalDocs/interpreter.md`:

> In 3.10 and before, […] the `CALL` opcode would call the `tp_call` dispatch function of the
> callee, which would extract the code object, create a new frame for the call stack, and
> then call back into the interpreter. This approach is very general but consumes several C
> stack frames for each nested Python call, thereby increasing the risk of an
> (unrecoverable) C stack overflow.

The 3.14 mechanism, from `_DO_CALL`:

```c
_PyInterpreterFrame *new_frame = _PyEvalFramePushAndInit(
    tstate, callable, locals, arguments, total_args, NULL, frame);
...
frame->return_offset = INSTRUCTION_SIZE;
DISPATCH_INLINED(new_frame);
```

and `DISPATCH_INLINED` from `ceval_macros.h`:

```c
#define DISPATCH_INLINED(NEW_FRAME)                     \
    do {                                                \
        assert(tstate->interp->eval_frame == NULL);     \
        _PyFrame_SetStackPointer(frame, stack_pointer); \
        assert((NEW_FRAME)->previous == frame);         \
        frame = tstate->current_frame = (NEW_FRAME);    \
        CALL_STAT_INC(inlined_py_calls);                \
        JUMP_TO_LABEL(start_frame);                     \
    } while (0)
```

**A Python function call became a `goto`.** No C recursion, so:

- **`sys.setrecursionlimit()` and the C stack are decoupled.** Python recursion depth is
  tracked by `tstate->py_recursion_remaining` (`_CHECK_RECURSION_REMAINING`), not by actual C
  frames. This is why deep pure-Python recursion stopped segfaulting.
- **`_PyEval_EvalFrameDefault` appears once in the C stack for an arbitrarily deep Python
  call chain.** Native stack unwinders (`gdb`, `lldb`, `perf`, `py-spy` in native mode) see
  one C frame where there are 200 Python frames. That is a substantial part of doc 32's
  "profile shows 60% in `_PyEval_EvalFrameDefault`" non-answer: it is not a hot function, it
  is *every* function.
- **The frame push is `_PyEvalFramePushAndInit` → `_PyThreadState_PushFrame` → pointer
  bump**, plus `initialize_locals()` copying arguments into `frame->localsplus`.

*(real output, 3.14.6)* — the specialization arriving, on a plain two-function program:

```
caller cold CALL : ['CALL', 'CALL']
caller warm CALL : ['CALL', 'CALL_PY_EXACT_ARGS']
blt    cold      : ['CALL']
blt    warm      : ['CALL_LEN']
```

Two different specializations of the same opcode: one that inlines a Python frame, one that
calls `len` without going through `PyObject_Vectorcall` at all. `CALL` has 20 family members
because "call something" is the most polymorphic operation in the language.

---

## 12. `RESUME`, the eval breaker, and generators

### `RESUME` is a bundle of four micro-ops

```c
macro(RESUME) =
    _LOAD_BYTECODE +
    _MAYBE_INSTRUMENT +
    _QUICKEN_RESUME +
    _CHECK_PERIODIC_IF_NOT_YIELD_FROM;
```

- **`_LOAD_BYTECODE`** — free-threaded only: if this thread's thread-local bytecode index no
  longer matches the frame's, re-point `instr_ptr` into this thread's copy. That is `co_tlbc`
  again.
- **`_MAYBE_INSTRUMENT`** — compare `tstate->eval_breaker & ~_PY_EVAL_EVENTS_MASK` (the
  global monitoring version) against `code->_co_instrumentation_version`; if they differ, call
  `_Py_Instrument()` to rewrite this code object's instructions into `INSTRUMENTED_*` variants.
  **PEP 669's zero-cost monitoring is a version number packed into the same word as the eval
  breaker.**
- **`_QUICKEN_RESUME`** — the only surviving use of the word:

  ```c
  op(_QUICKEN_RESUME, (--)) {
      #if ENABLE_SPECIALIZATION_FT
      if (tstate->tracing == 0 && this_instr->op.code == RESUME) {
          FT_ATOMIC_STORE_UINT8_RELAXED(this_instr->op.code, RESUME_CHECK);
      }
      #endif
  }
  ```

  No counter. First execution outside tracing promotes `RESUME` to `RESUME_CHECK`.
- **`_CHECK_PERIODIC_IF_NOT_YIELD_FROM`** — the eval-breaker test.

And the promoted form collapses all of that into three loads and a compare:

```c
inst(RESUME_CHECK, (--)) {
    uintptr_t eval_breaker = _Py_atomic_load_uintptr_relaxed(&tstate->eval_breaker);
    uintptr_t version = FT_ATOMIC_LOAD_UINTPTR_ACQUIRE(_PyFrame_GetCode(frame)->_co_instrumentation_version);
    assert((version & _PY_EVAL_EVENTS_MASK) == 0);
    DEOPT_IF(eval_breaker != version);
    ...
}
```

**One comparison tests "no pending events" and "instrumentation unchanged" simultaneously**,
because the event bits live in the low bits of `eval_breaker` and the version occupies the
rest. If *anything* is pending — a GIL drop request, a signal, a GC, or a new
`sys.monitoring` tool — the words differ and it deoptimizes to full `RESUME`.

### Where the eval breaker is checked

The check itself:

```c
op(_CHECK_PERIODIC, (--)) {
    _Py_CHECK_EMSCRIPTEN_SIGNALS_PERIODICALLY();
    QSBR_QUIESCENT_STATE(tstate);
    if (_Py_atomic_load_uintptr_relaxed(&tstate->eval_breaker) & _PY_EVAL_EVENTS_MASK) {
        int err = _Py_HandlePending(tstate);
        ERROR_IF(err != 0);
    }
}
```

**[`24-the-gil.md`](24-the-gil.md) §4 owns everything downstream of `_Py_HandlePending` —
the `gil_drop_request` bit, the handoff protocol, `sys.setswitchinterval`, and the "a thread
that never reaches a check point never yields" failure mode. Do not re-derive it here.** The
only thing doc 20 adds is *where*, in the generated instruction set, the check physically
sits: inside `RESUME` (function entry and post-`await`/`yield` resumption) and inside
`JUMP_BACKWARD` (every loop back-edge), plus their instrumented variants.

`QSBR_QUIESCENT_STATE(tstate)` in the same op is worth noticing: on free-threaded builds every
eval-breaker check is also a **quiescent-state announcement** for the QSBR reclamation scheme
from [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md). Two unrelated
subsystems share one already-hot check point.

### What generators do to the frame

A generator's frame cannot live on the data stack, because it outlives its activation. So it
is embedded in the generator object itself:

```c
#define _PyGenObject_HEAD(prefix)                                           \
    PyObject_HEAD                                                           \
    ...                                                                     \
    int8_t prefix##_frame_state;                                            \
    _PyInterpreterFrame prefix##_iframe;                                    \

struct _PyGenObject   { _PyGenObject_HEAD(gi) };
struct _PyCoroObject  { _PyGenObject_HEAD(cr) };
struct _PyAsyncGenObject { _PyGenObject_HEAD(ag) };
```

> Generators […] have a `_PyInterpreterFrame` embedded in them, so that they can be created
> with a single memory allocation.

`RETURN_GENERATOR` is the transfer:

```c
inst(RETURN_GENERATOR, (-- res)) {
    PyGenObject *gen = (PyGenObject *)_Py_MakeCoro(func);
    assert(STACK_LEVEL() == 0);
    SAVE_STACK();
    _PyInterpreterFrame *gen_frame = &gen->gi_iframe;
    frame->instr_ptr++;
    _PyFrame_Copy(frame, gen_frame);
    gen->gi_frame_state = FRAME_CREATED;
    gen_frame->owner = FRAME_OWNED_BY_GENERATOR;
    _Py_LeaveRecursiveCallPy(tstate);
    _PyInterpreterFrame *prev = frame->previous;
    _PyThreadState_PopFrame(tstate, frame);
    frame = tstate->current_frame = prev;
    LOAD_IP(frame->return_offset);
    ...
}
```

Read the order: allocate the generator, **copy the whole frame** out of the data stack into
it, flip `owner` to `FRAME_OWNED_BY_GENERATOR`, pop the data-stack frame, return the
generator. *(real output)* confirms this is the literal first instruction of a generator
function:

```
gen prologue: ['RETURN_GENERATOR', 'POP_TOP', 'RESUME', 'LOAD_GLOBAL']
```

**Calling a generator function does not execute its body — it executes exactly one
instruction and returns.** The `RESUME` that follows only runs on the first `next()`.

On the way back out, `YIELD_VALUE` does the reverse and one extra thing:

```c
inst(YIELD_VALUE, (retval -- value)) {
    // NOTE: It's important that YIELD_VALUE never raises an exception!
    frame->instr_ptr++;
    PyGenObject *gen = _PyGen_GetGeneratorFromFrame(frame);
    gen->gi_frame_state = FRAME_SUSPENDED + oparg;
    SAVE_STACK();
    tstate->exc_info = gen->gi_exc_state.previous_item;
    gen->gi_exc_state.previous_item = NULL;
    _Py_LeaveRecursiveCallPy(tstate);
    _PyInterpreterFrame *gen_frame = frame;
    frame = tstate->current_frame = frame->previous;
    gen_frame->previous = NULL;
    ...
}
```

The `exc_info` swap is the mechanism behind "a generator remembers the exception it was
handling across a yield" — the per-generator exception state is unlinked from the thread's
chain on suspend and relinked on resume. And `gen_frame->previous = NULL` is what makes a
suspended generator's frame **not** part of any thread's call stack, which is why a traceback
through a suspended generator shows nothing until it is resumed. Everything `asyncio` does
with `await` sits on top of this and `SEND`/`SEND_GEN`
([`28-asyncio-internals.md`](28-asyncio-internals.md)).

> **Could not verify:** I did not trace the `SEND_GEN` → generator-frame-push path in the
> source, nor confirm how `FRAME_SUSPENDED_YIELD_FROM` (the `+ oparg` above) changes
> eval-breaker behaviour — I only observed that
> `_CHECK_PERIODIC_IF_NOT_YIELD_FROM` skips the check when
> `(oparg & RESUME_OPARG_LOCATION_MASK) >= RESUME_AFTER_YIELD_FROM`. The *reason* for that
> skip is my inference (avoid a redundant check when resuming into a delegating frame that
> just did one), not something I confirmed.

---

## 13. `LOAD_FAST_BORROW`, `_PyStackRef`, and refcounting on the hot path

Doc 19 §12 flagged `LOAD_FAST_BORROW` as new in 3.14 and explicitly said it *could not
verify* which analysis decides when it is legal. **That question is resolved here.**

The instruction itself is trivial:

```c
replicate(8) pure inst(LOAD_FAST, (-- value)) {
    assert(!PyStackRef_IsNull(GETLOCAL(oparg)));
    value = PyStackRef_DUP(GETLOCAL(oparg));
}

replicate(8) pure inst (LOAD_FAST_BORROW, (-- value)) {
    assert(!PyStackRef_IsNull(GETLOCAL(oparg)));
    value = PyStackRef_Borrow(GETLOCAL(oparg));
}
```

(`replicate(8)` is another DSL directive: generate eight copies specialized on
`oparg = 0..7`, so the common case needs no oparg indexing at all.)

### What `PyStackRef_Borrow` compiles to

`_PyStackRef` is a tagged word, not a `PyObject*`. On the **default GIL build**, from
`Include/internal/pycore_stackref.h`:

```c
#define Py_INT_TAG 3
#define Py_TAG_REFCNT 1

static inline bool
PyStackRef_RefcountOnObject(_PyStackRef ref)
{
    return (ref.bits & Py_TAG_REFCNT) == 0;
}

static inline _PyStackRef
PyStackRef_Borrow(_PyStackRef ref)
{
    return (_PyStackRef){ .bits = ref.bits | Py_TAG_REFCNT };
}
```

**Borrowing is one OR of an immediate into a register.** No memory is touched. The object's
header is not read and not written. Contrast `PyStackRef_DUP`, which must `Py_INCREF` — a
read-modify-write of `ob_refcnt`, which:

- **dirties a cache line** in the object header, which is the *hottest* line for that object
  ([`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md));
- on a multicore machine forces that line into M state, so any other core reading the same
  object stalls — [`24-the-gil.md`](24-the-gil.md) §1's entire argument;
- on a free-threaded build is an atomic operation.

On the **free-threaded build** the same function exists with a different name for the same
bit:

```c
#define Py_TAG_DEFERRED Py_TAG_REFCNT

static inline _PyStackRef
PyStackRef_Borrow(_PyStackRef stackref)
{
    return (_PyStackRef){ .bits = stackref.bits | Py_TAG_DEFERRED };
}
```

so the borrow bit and the deferred-refcounting bit are literally the same bit. **The
`_PyStackRef` tagging scheme was the enabling change for both `LOAD_FAST_BORROW` and PEP 703
deferred reference counting** — one representation, two payoffs. `Py_INT_TAG 3` in the same
header is a third: small integers can live on the evaluation stack as tagged immediates with
no object at all.

### When is it legal? (doc 19's open question, answered)

`Python/flowgraph.c`, function `optimize_load_fast()`, called twice from the optimization
pipeline. The comment above it is the specification:

```c
/*
 * Strength reduce LOAD_FAST{_LOAD_FAST} instructions into faster variants that
 * load borrowed references onto the operand stack.
 *
 * This is only safe when we can prove that the reference in the frame outlives
 * the borrowed reference produced by the instruction. We make this tractable
 * by enforcing the following lifetimes:
 *
 * 1. Borrowed references loaded onto the operand stack live until the end of
 *    the instruction that consumes them from the stack. Any borrowed
 *    references that would escape into the heap (e.g. into frame objects or
 *    generators) are converted into new, strong references.
 *
 * 2. Locals live until they are either killed by an instruction
 *    (e.g. STORE_FAST) or the frame is unwound. Any local that is overwritten
 *    via `f_locals` is added to a tuple owned by the frame object.
 * ...
 * We use abstract interpretation to identify instructions that meet these
 * criteria. For each basic block, we simulate the effect the bytecode has on a
 * stack of abstract references and note any instructions that violate the
 * criteria above. Once we've processed all the instructions in a block, any
 * non-violating LOAD_FAST{_LOAD_FAST} can be optimized.
 */
static int
optimize_load_fast(cfg_builder *g)
```

with the rewrite at the end:

```c
instr->i_opcode = LOAD_FAST_BORROW;
...
instr->i_opcode = LOAD_FAST_BORROW_LOAD_FAST_BORROW;
```

**So: a compile-time abstract interpretation over the CFG, per basic block, tracking a stack
of abstract references.** Doc 19's guess ("valid where the compiler can prove the local
outlives the use") was right in spirit and now has a function name, a file, and an algorithm.

Two escape hatches in that comment are worth internalizing because they close loops with §8
and §9:

- *"Any borrowed references that would escape into the heap (e.g. into frame objects or
  generators) are converted into new, strong references"* — and `{RETURN,YIELD}_VALUE`
  convert borrowed to strong. **This is why materializing a `PyFrameObject` (§8) is safe**
  even though the stack is full of borrowed refs.
- *"Any local that is overwritten via `f_locals` is added to a tuple owned by the frame
  object"* — **PEP 667's write-through proxy (§9) had to keep the old value alive**, because
  a borrowed reference on the stack may still point at it. The `f_locals` design and the
  borrow optimization constrain each other.

**The engineering summary:** doc 19 showed you `LOAD_FAST_BORROW` in disassembly; the reason
it merited its own opcode is that in a refcounting interpreter, *not writing to memory* is a
larger win than almost any instruction-count reduction. The same reasoning produced
`LOAD_CONST_IMMORTAL` (§6) and deferred refcounting (PEP 703).

---

## 14. What all of this means when you open a profiler

Six operational conclusions, each traceable to a section above.

**1. `_PyEval_EvalFrameDefault` at 60% is not a finding.** §11: Python-to-Python calls are
`goto`s inside one C frame. A native profiler attributes an arbitrarily deep Python call tree
to that single symbol. [`32-profiling.md`](32-profiling.md) makes this its worked example;
§11 is the mechanism.

**2. You cannot see specialization in a normal profiler, but you can see it in three other
ways.** `dis.dis(f, adaptive=True)` (doc 19 §5); `dis.Bytecode(f, show_caches=True)` for the
counter bytes (§5); and a `--enable-pystats` build plus
`Tools/scripts/summarize_stats.py`, which is what the `STAT_INC(LOAD_ATTR, hit)` and
`UPDATE_MISS_STATS(...)` calls all over the generated code feed. The first two cost nothing
and are available on a stock interpreter.

**3. A specialized instruction is fast because it does not branch, and it becomes slow the
moment it has to.** The steady-state `LOAD_ATTR_INSTANCE_VALUE` is two integer compares, an
add and a load (§4). The miss path is a jump plus a full generic execution plus, eventually,
a re-specialization. §6's measured oscillation is the shape of the middle case.

**4. Four ordinary source-level changes deoptimize hot code, and none look like performance
changes** (§7): modifying a class after startup (`tp_version_tag`); adding or deleting a
global at runtime (`dk_version`); touching `obj.__dict__` (inline values invalid); rebuilding
a function object (`func_version`).

**5. Attaching a PEP 523 tool is a process-wide deoptimization, not a local one** (§10).
Prefer `sys.monitoring` (PEP 669) and PEP 768 remote debugging when you have the choice.

**6. Frame introspection is object allocation** (§8). `sys._getframe()`, `f_locals` on an
optimized frame, and stack-capturing log records each materialize a `PyFrameObject` that
would otherwise never exist.

> **Could not verify:** I did not build a `--enable-pystats` interpreter or run
> `Tools/scripts/summarize_stats.py`. I confirmed the script exists on the 3.14 branch
> (HTTP 200) and that `STAT_INC`/`UPDATE_MISS_STATS` are compiled in only under `Py_STATS`
> (`ceval_macros.h`), but I have not seen its output. Everything I claim about *what it
> reports* is inference from the macro names.

---

## 15. Lab exercises

Reading this document leaves you at **rung 3** on the README §14 ladder — you can now say
"adaptive specializing interpreter with inline caches and exponential backoff" fluently and
collapse on the first "why 53?". These move you to rung 4. **All of them are `dis`-based,
single-shot, and run in under a second on a stock 3.14; none is a benchmark.** Lab 7 needs a
source checkout; Lab 8 needs a build.

**1 — Decode the counter.** Take any function with a `LOAD_ATTR`. Print
`dis.Bytecode(f, show_caches=True)`'s first `cache_info` entry as a little-endian `uint16`
before any call, after one call, after two, and after fifty. Convert each to
`(value, backoff)` and match it against `ADAPTIVE_WARMUP_VALUE` / `ADAPTIVE_COOLDOWN_VALUE`
in `Include/internal/pycore_code.h`. *Proves §5, and it is the cheapest possible proof that
you are reading the real state machine and not a description of it.*

**2 — Make one site oscillate.** Reproduce §6's table: one function, two classes (one with
`__slots__`), alternate. Record `(opname, counter)` every 10 calls for 500 calls and plot or
tabulate it. Predict the period *before* you run it from `ADAPTIVE_COOLDOWN_VALUE`. *Proves
§6 and gives you a number to cite the next time someone says polymorphism is free.*

**3 — Break each guard on purpose.** Warm a `LOAD_ATTR` to
`LOAD_ATTR_INSTANCE_VALUE`, then do each of these separately and re-disassemble: (a) set a
new attribute on the *class*; (b) read `obj.__dict__`; (c) add a new global to the module.
Say which guard from §7 each one invalidates, and which of the three does *not* affect that
particular instruction. *Proves §7 — this is the lab that changes how you write code.*

**4 — Find the specialization cliff for `CALL`.** Write `def f(a, b=1)` and call it four
ways: `f(1)`, `f(1, 2)`, `f(1, b=2)`, `f(*args)`. Disassemble the call site after warmup in
each case and identify which of the 20 `CALL` family members you got, or why you got none.
*Proves §11 and teaches you to read `_opcode_metadata._specializations` as a capability list.*

**5 — Prove the generator prologue.** Disassemble a generator function and a plain function
that returns a list. Confirm `RETURN_GENERATOR` is instruction #1 and that `RESUME` follows
it. Then show, by printing inside the body, that calling the generator function runs no body
code. Explain from §12 which frame the body will eventually run in. *Proves §12 and is the
prerequisite for [`28-asyncio-internals.md`](28-asyncio-internals.md).*

**6 — Catch `LOAD_FAST_BORROW` failing.** Write three functions: one where every local load
becomes `LOAD_FAST_BORROW`, one where at least one stays `LOAD_FAST`, and one containing
`sys._getframe()`. Disassemble all three, then read `optimize_load_fast()`'s comment in
`Python/flowgraph.c` and explain which of its two lifetime rules your second and third
functions violated. *Proves §13, and closes doc 19 §12's open question with your own
evidence.*

**7 — Read the DSL and its output side by side.** Pick any specialized instruction from
`_specializations`. Find its `macro(...)` in `Python/bytecodes.c`, then find its
`TARGET(...)` in `Python/generated_cases.c.h`. List every line the generator added, and
account for each cache offset (`this_instr[N]`) from the `unused/N` terms in the macro.
*Proves §2 — and after this you can read any instruction in CPython.*

**8 — Regenerate the interpreter.** In a source checkout, add a `family(...)` member or
change one `unused/N` in `bytecodes.c`, run `make regen-cases`, and `git diff`. Count how
many generated files changed. Then revert. *Proves §2's manifest table concretely, and is
the on-ramp to doc 19's lab 8 and the README §12 capstone.*

---

## 16. Question bank

Staff-level. Section references are where to reread if your model doesn't produce the answer.

1. Where are the semantics of `BINARY_OP` written, and what compiles them into the interpreter? Name the input file, the tool directory, and at least three output files. *(§2)*
2. In the DSL, what does `(counter/1, owner -- owner)` mean, and what C does the generator emit for the `/1`? *(§2)*
3. `macro(LOAD_ATTR_INSTANCE_VALUE)` contains `unused/1` and `unused/5`. Why does a specialization need padding it never reads? *(§2, §4)*
4. Give the three `DISPATCH_GOTO()` implementations in 3.14 and the hardware argument for each. *(§3)*
5. The tail-call interpreter was announced at 10–15% and documented at 3–5%. Both were measured honestly. Explain. *(§3)*
6. What do `musttail` and `preserve_none` each contribute, and what happens if you have one but not the other? *(§3)*
7. Walk a cold `LOAD_ATTR` to `LOAD_ATTR_INSTANCE_VALUE` execution by execution, giving the counter value at each step. Why does it specialize on the *second* execution rather than the first? *(§4, §5)*
8. After specializing, the counter stops changing. Why — and what does it start counting instead? *(§5, §6)*
9. Distinguish a *miss* from a *specialization failure*. Which rewrites the opcode byte, and what happens to the counter in each case? *(§6)*
10. Why 52/53, and why is the design asymmetric (2 executions to specialize, 53 misses to re-specialize)? *(§6)*
11. Name four ordinary source-level changes that invalidate a specialization guard, and say which guard each one breaks. *(§7)*
12. What is `_PyInterpreterFrame` missing that `PyFrameObject` has, and what does a Python function call cost in the common case? *(§8)*
13. When does a `PyFrameObject` come into existence, and what does `sys._getframe()` cost that people assume is free? *(§8)*
14. What is the shim frame for, and what instruction lives in its fake code object? *(§8)*
15. PEP 558 or PEP 667 — which one describes how `f_locals` behaves on 3.14, and what is the status of the other? *(§9)*
16. Someone attaches a PEP 523-based debugger with no breakpoints set and the service gets 3× slower. Give the mechanism, naming the guard. *(§10)*
17. Why does `LOAD_ATTR` with the low oparg bit set push two stack entries, and how does that connect to `PY_VECTORCALL_ARGUMENTS_OFFSET`? *(§11, doc 19 §12)*
18. `METH_VARARGS` vs `METH_FASTCALL` for a C extension function — what changes at the call site, and which specializations become available? *(§11)*
19. A Python call chain 200 frames deep. How many C stack frames, and what does that do to `perf` output? *(§11, §14)*
20. `RESUME` becomes `RESUME_CHECK` with no counter at all. What does `RESUME_CHECK`'s single comparison actually test, and name three unrelated events that make it deoptimize. *(§12)*
21. Calling a generator function executes exactly one instruction. Which, and where does the frame end up? *(§12)*
22. `PyStackRef_Borrow` on a GIL build — what machine instruction is it, and why is that the interesting part? *(§13)*
23. Which analysis, in which file, decides whether `LOAD_FAST` may become `LOAD_FAST_BORROW`, and what are its two lifetime rules? *(§13)*
24. A profile shows 60% of samples in `_PyEval_EvalFrameDefault`. What have you learned, and what would you run next? *(§14, [`32-profiling.md`](32-profiling.md))*

---

## 17. Unverified claims, collected

Repeated here so they are impossible to miss. Everything else in this document is either a
verbatim source quotation from the `3.14` branch or output from a live 3.14.6 interpreter.

1. **§3 — every tail-call and computed-goto performance number.** I built nothing and
   measured nothing. Figures are the CPython 3.14 What's New (3–5%) and Nelson Elhage's
   March 2025 benchmarks (his tables, reproduced with attribution). I did not verify his
   indirect-jump counts, his GCC-15 `musttail` remark, or that his conclusions still hold on
   current LLVM.
2. **§7 — dict watchers.** The 3.14 `bytecodes.c` specializations guard on `dk_version`
   comparison, not on a `PyDict_AddWatcher` callback. I did not audit `specialize.c` or
   `dictobject.c` for a watcher-based path elsewhere. Treat "dict watchers guard
   `LOAD_GLOBAL`" as unconfirmed.
3. **§9 — `f_locals` write-through** was verified only for a plain local in an optimized
   scope. Cell/free variables and suspended generator frames were not tested.
4. **§12 — `SEND_GEN` and `FRAME_SUSPENDED_YIELD_FROM`.** I read
   `_CHECK_PERIODIC_IF_NOT_YIELD_FROM`'s condition but did not trace the generator-resume
   path; my explanation of *why* the check is skipped is inference.
5. **§14 — `Tools/scripts/summarize_stats.py`.** Confirmed to exist; never run. Claims about
   its output are inferred from the `STAT_INC` / `UPDATE_MISS_STATS` macro names.
6. **Free-threaded specifics.** `_GUARD_TYPE_VERSION_AND_LOCK`, `_LOAD_BYTECODE`/`tlbc_index`,
   `Py_TAG_DEFERRED` and `QSBR_QUIESCENT_STATE` are quoted from source but were **not
   exercised on a `python3.14t` build** for this document. Doc 19 §1 and
   [`26-free-threading.md`](26-free-threading.md) have the measured free-threading material.
7. **3.15.** This document describes **3.14**. 3.15 is in its release-candidate window as of
   August 2026 and I did not diff `bytecodes.c` between branches beyond spot-checking that
   `_PY_DATA_STACK_CHUNK_SIZE` is unchanged on `main`. Assume opcode numbers, family
   membership and cache sizes have moved — doc 19 §13 is the standing warning.

---

## 18. Sources

**Primary — the interpreter itself. Verify against these, not against this document.**

- [`Python/bytecodes.c`](https://github.com/python/cpython/blob/3.14/Python/bytecodes.c) — **the** source of truth. ~5,550 lines, and *not compiled*. **Verdict: this is the single most valuable file in CPython to be able to read, and it is far more readable than `ceval.c` ever was. Start at `// BEGIN BYTECODES //` and read `LOAD_FAST`, `LOAD_ATTR`, `BINARY_OP`, `CALL` in that order.**
- [`Python/generated_cases.c.h`](https://github.com/python/cpython/blob/3.14/Python/generated_cases.c.h) — ~12,500 lines of generated C. **Verdict: never edit it, but read one `TARGET(...)` block beside its DSL original once (Lab 7). That single comparison teaches more than any blog post about the generator.**
- [`Python/ceval.c`](https://github.com/python/cpython/blob/3.14/Python/ceval.c) — now mostly *scaffolding*: entry/exit, frame push/pop, argument binding, the two compiler workarounds in §1. **Verdict: if you last read this file in the 3.9 era, your model is wrong. The instructions left.**
- [`Python/ceval_macros.h`](https://github.com/python/cpython/blob/3.14/Python/ceval_macros.h) — 440 lines containing all three dispatch strategies, `DISPATCH`, `NEXTOPARG`, `DISPATCH_INLINED`, the backoff macros. **Verdict: the highest information density per line in the whole runtime. Read it in full — it is short.**
- [`Python/specialize.c`](https://github.com/python/cpython/blob/3.14/Python/specialize.c) — `_Py_Specialize_*`, `specialize()`, `unspecialize()`, and the `SPECIALIZATION_FAIL` taxonomy. **Verdict: read `specialize`/`unspecialize` (30 lines) and skim the `SPEC_FAIL_*` enum — it is a catalogue of every Python idiom that defeats the optimizer.**
- [`Include/internal/pycore_backoff.h`](https://github.com/python/cpython/blob/3.14/Include/internal/pycore_backoff.h) and [`pycore_code.h`](https://github.com/python/cpython/blob/3.14/Include/internal/pycore_code.h) — the counter, `ADAPTIVE_WARMUP_VALUE`, `ADAPTIVE_COOLDOWN_VALUE`. **Verdict: 130 lines that explain §5 and §6 completely. The comments are the design document.**
- [`Include/internal/pycore_stackref.h`](https://github.com/python/cpython/blob/3.14/Include/internal/pycore_stackref.h) — `_PyStackRef`, `Py_TAG_REFCNT`, `Py_TAG_DEFERRED`, `Py_INT_TAG`. **Verdict: §13's source. Read the three `PyStackRef_Borrow` definitions and note they are the same idea three times.**
- [`Include/internal/pycore_interpframe_structs.h`](https://github.com/python/cpython/blob/3.14/Include/internal/pycore_interpframe_structs.h), [`Include/cpython/pystate.h`](https://github.com/python/cpython/blob/3.14/Include/cpython/pystate.h), [`Python/pystate.c`](https://github.com/python/cpython/blob/3.14/Python/pystate.c) — the frame struct, `_PY_DATA_STACK_CHUNK_SIZE`, `_PyThreadState_PushFrame`, `push_chunk`. **Verdict: §8 in three files.**
- [`Python/flowgraph.c`](https://github.com/python/cpython/blob/3.14/Python/flowgraph.c) — `optimize_load_fast()`. **Verdict: read only the 40-line comment above it; it is a complete specification of §13.**

**Devguide internals docs — underread, and better than most secondary material**

- [`InternalDocs/interpreter.md`](https://github.com/python/cpython/blob/3.14/InternalDocs/interpreter.md) — instruction decoding, inline caches, Python-to-Python calls, the specialization design guide and the `Tadaptive` formula. **Verdict: the best single overview. Caveat: partly stale — it still refers to `frame->is_entry`, which no longer exists (§8).**
- [`InternalDocs/frames.md`](https://github.com/python/cpython/blob/3.14/InternalDocs/frames.md) — 136 lines covering §8 completely, including the *rejected* frame layout and the `instr_ptr`/`return_offset` rationale. **Verdict: read it in full; it is shorter than this section.**
- [`Tools/cases_generator/README.md`](https://github.com/python/cpython/blob/3.14/Tools/cases_generator/README.md) and [`interpreter_definition.md`](https://github.com/python/cpython/blob/3.14/Tools/cases_generator/interpreter_definition.md) — the DSL grammar and the generator inventory. **Verdict: the second is the DSL's actual spec and nothing else on the internet replaces it.**

**PEPs**

- [PEP 659 — Specializing Adaptive Interpreter](https://peps.python.org/pep-0659/) (Shannon, Informational, Final). **Verdict: essential, and now partly historical — its "quickened copy of the bytecode" model was superseded by in-place adaptive bytecode in 3.12. Read it for the *reasoning*, then read `pycore_code.h` for the *implementation*. The PEP itself now carries a banner pointing at the 3.11 What's New as canonical.**
- [PEP 523 — Adding a frame evaluation API to CPython](https://peps.python.org/pep-0523/) (Cannon & Viehland, Final, 3.6). **Verdict: short, and the Pyjion pseudo-code is the clearest statement of what the hook is for. The signature in the text is out of date (§10) — confirm against `Include/cpython/pystate.h`.**
- [PEP 590 — Vectorcall](https://peps.python.org/pep-0590/) (Shannon & Demeyer, Final, 3.8). **Verdict: read the Specification and the `PY_VECTORCALL_ARGUMENTS_OFFSET` section. It explains a bytecode stack-layout decision from doc 19 §12 that otherwise looks arbitrary.**
- [PEP 667 — Consistent views of namespaces](https://peps.python.org/pep-0667/) (Shannon & Gao, 3.13) — **the one that shipped.** **Verdict: read the Rationale; it is the clearest available account of why the old snapshot design lost writes.**
- [PEP 558 — Defined semantics for `locals()`](https://peps.python.org/pep-0558/) (Coghlan) — **Withdrawn** *(verified)*. **Verdict: read only if you want the history. Cite 667.**
- [PEP 744 — JIT Compilation](https://peps.python.org/pep-0744/) and PEP 836 — tier 2. **Verdict: deferred entirely to [`21-tier2-and-jit.md`](21-tier2-and-jit.md).**
- [PEP 669 — Low impact monitoring](https://peps.python.org/pep-0669/), [PEP 768 — Safe external debugger interface](https://peps.python.org/pep-0768/) — the supported alternatives to PEP 523. See [`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md).

**Secondary — read with the baseline question in mind**

- [Nelson Elhage, *Performance of the Python 3.14 tail-call interpreter*](https://blog.nelhage.com/post/cpython-tail-call/) (Mar 2025). **Verdict: the most important performance-methodology writeup in recent CPython history, and the source of every §3 number I did not take from the docs. Read the "Baselines" section even if you never touch an interpreter. Caveat: it is a snapshot of a moving toolchain, and the author is explicit that he cannot fully explain the magnitude of the LLVM regression.**
- [What's New in Python 3.14 — "A new type of interpreter"](https://docs.python.org/3.14/whatsnew/3.14.html) (Ken Jin et al.). **Verdict: the official 3–5% figure and the exact build requirements. Note it names its own baseline, which is what makes it honest.**

**Tools you should have used by the end of §15**

- `dis.dis(f, adaptive=True, show_caches=True, show_offsets=True)` — all three flags, together.
- `dis.Bytecode(f, show_caches=True)` → `.cache_info` — the *only* supported window onto the counter.
- `_opcode_metadata._specializations` and `_opcode_metadata._specialized_opmap` — private, generated from `family(...)`, and will move.
- `./configure --enable-pystats` + `Tools/scripts/summarize_stats.py` — hit/miss/deopt rates per instruction *(§14: I confirmed this exists but did not run it)*.
- `./configure --with-tail-call-interp` (Clang 19+, x86-64/AArch64) and `--without-computed-gotos`, if you want to reproduce §3 yourself. **Doing so, properly, on your own hardware, is the most valuable thing in this document that I did not do.**

**Sibling docs**

- [`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md) — everything this document executes. §4's `CACHE` entries are §4–§7 here; its §12 open question is answered in §13 here.
- [`21-tier2-and-jit.md`](21-tier2-and-jit.md) — where the micro-ops (`op(...)`) from §2 go next, and what `JUMP_BACKWARD_JIT` / `co_executors` are for.
- [`24-the-gil.md`](24-the-gil.md) §4 — the eval breaker downstream of §12's `_CHECK_PERIODIC`.
- [`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md) — why §13 exists at all.
- [`32-profiling.md`](32-profiling.md) — §11 and §14 are the mechanism behind its `_PyEval_EvalFrameDefault` non-answer.
- [`31-measurement-methodology.md`](31-measurement-methodology.md) — §3 is a case study for it, written by the CPython core team by accident.

---

*Next: [`21-tier2-and-jit.md`](21-tier2-and-jit.md) — what happens when the counter in
`JUMP_BACKWARD` reaches zero, how the `op(...)` micro-ops from §2 become a trace, and why the
copy-and-patch JIT's numbers should be read with §3 in mind.*
