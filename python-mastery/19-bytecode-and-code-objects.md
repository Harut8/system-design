# 19 — Bytecode and code objects: reading what the compiler actually emitted

> **Tier 3, doc 19.** Prerequisites: [`18-lexer-parser-ast.md`](18-lexer-parser-ast.md)
> (tokenizer → PEG parser → AST → symbol table → CFG),
> [`14-pyobject-and-types.md`](14-pyobject-and-types.md) (everything on the stack is a
> `PyObject*`), [`16-object-memory-layout.md`](16-object-memory-layout.md) (what a
> pointer dereference costs). Feeds into: [`20-eval-loop.md`](20-eval-loop.md),
> [`21-tier2-and-jit.md`](21-tier2-and-jit.md),
> [`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md),
> [`42-runtime-code-manipulation.md`](42-runtime-code-manipulation.md).
>
> **THESIS: a code object is not "the bytecode". It is a fixed-layout C struct whose
> bytecode array is one field among twenty-odd, and most of the interesting engineering
> lives in the *other* fields.** The exception table replaced a runtime block stack with
> a compile-time side table. The line table and position table let tracebacks point at
> a sub-expression without costing anything at runtime. The inline caches are holes
> punched into the instruction stream itself. Every one of those is the same trade:
> **move work off the hot path and pay for it in metadata.** Learn to read the metadata
> and the eval loop in doc 20 stops being mysterious.

> **Measurement provenance.** Every disassembly, byte dump, opcode name, struct field
> and constant below was produced on the machine this repo lives on: **Apple M3 Pro,
> macOS, arm64, CPython 3.14.6** (`~/.local/bin/python3.14`), with the **3.14.6
> free-threading build** (`python3.14t`) used where marked. Output labelled *(real
> output)* was pasted from a live interpreter while writing this document — **no
> disassembly here was reconstructed from memory**, because bytecode is the single
> easiest thing in Python to get subtly and confidently wrong. C struct fields come
> from [`Include/cpython/code.h`](https://github.com/python/cpython/blob/3.14/Include/cpython/code.h)
> on the `3.14` branch. Two things I could **not** verify are flagged in place (§8, §12).
> **Re-run everything on your own interpreter.** Opcode numbers change every minor
> release and several changed inside 3.14.

## Contents

1. [The code object is a struct, not a byte string](#1-the-code-object-is-a-struct-not-a-byte-string)
2. [Wordcode: one byte of opcode, one byte of oparg](#2-wordcode-one-byte-of-opcode-one-byte-of-oparg)
3. [`EXTENDED_ARG`: the escape hatch, and its cost](#3-extended_arg-the-escape-hatch-and-its-cost)
4. [Inline caches as `CACHE` pseudo-instructions](#4-inline-caches-as-cache-pseudo-instructions)
5. [Reading `dis` fluently: `adaptive=True` and specialized names](#5-reading-dis-fluently-adaptivetrue-and-specialized-names)
6. [The name tables, and the localsplus trick](#6-the-name-tables-and-the-localsplus-trick)
7. [`co_stacksize`, stack effects, and a real segfault](#7-co_stacksize-stack-effects-and-a-real-segfault)
8. [Zero-cost exception handling](#8-zero-cost-exception-handling)
9. [Source locations: `co_linetable` and `co_positions`](#9-source-locations-co_linetable-and-co_positions)
10. [Comprehension inlining (PEP 709)](#10-comprehension-inlining-pep-709)
11. [Closures: `MAKE_CELL`, `COPY_FREE_VARS`, `LOAD_DEREF`](#11-closures-make_cell-copy_free_vars-load_deref)
12. [Oparg encoding tricks](#12-oparg-encoding-tricks)
13. [Bytecode is a private implementation detail](#13-bytecode-is-a-private-implementation-detail)
14. [Lab exercises](#14-lab-exercises)
15. [Question bank](#15-question-bank)
16. [Sources](#16-sources)

---

## 1. The code object is a struct, not a byte string

Start with the actual definition. From `Include/cpython/code.h` on the 3.14 branch —
all `PyCodeObject` members live in one macro so `deepfreeze.py` can reuse them, which is
why it looks like this:

```c
#define _PyCode_DEF(SIZE) {                                                    \
    PyObject_VAR_HEAD                                                          \
    /* The hottest fields (in the eval loop) are grouped here at the top. */   \
    PyObject *co_consts;           /* list (constants used) */                 \
    PyObject *co_names;            /* list of strings (names used) */          \
    PyObject *co_exceptiontable;   /* Byte string encoding exception handling  \
                                      table */                                 \
    int co_flags;                  /* CO_..., see below */                     \
    /* The rest are not so impactful on performance. */                        \
    int co_argcount;              /* #arguments, except *args */               \
    int co_posonlyargcount;       /* #positional only arguments */             \
    int co_kwonlyargcount;        /* #keyword only arguments */                \
    int co_stacksize;             /* #entries needed for evaluation stack */   \
    int co_firstlineno;           /* first source line number */               \
    /* redundant values (derived from co_localsplusnames and                   \
       co_localspluskinds) */                                                  \
    int co_nlocalsplus;           /* number of spaces for holding local, cell, \
                                     and free variables */                     \
    int co_framesize;             /* Size of frame in words */                 \
    int co_nlocals;               /* number of local variables */              \
    int co_ncellvars;             /* total number of cell variables */         \
    int co_nfreevars;             /* number of free variables */               \
    uint32_t co_version;          /* version number */                         \
    PyObject *co_localsplusnames; /* tuple mapping offsets to names */         \
    PyObject *co_localspluskinds; /* Bytes mapping to local kinds (one byte    \
                                     per variable) */                          \
    PyObject *co_filename;        /* unicode (where it was loaded from) */     \
    PyObject *co_name;            /* unicode (name, for reference) */          \
    PyObject *co_qualname;        /* unicode (qualname, for reference) */      \
    PyObject *co_linetable;       /* bytes object that holds location info */  \
    PyObject *co_weakreflist;                                                  \
    _PyExecutorArray *co_executors;      /* executors from optimizer */        \
    _PyCoCached *_co_cached;      /* cached co_* attributes */                 \
    uintptr_t _co_instrumentation_version;                                     \
    struct _PyCoMonitoringData *_co_monitoring;                                \
    Py_ssize_t _co_unique_id;     /* ID used for per-thread refcounting */     \
    int _co_firsttraceable;       /* index of first traceable instruction */   \
    void *co_extra;                                                            \
    _PyCode_DEF_THREAD_LOCAL_BYTECODE()                                        \
    char co_code_adaptive[(SIZE)];                                             \
}
```

Four structural facts fall straight out of that, and none of them are guessable from the
Python-level API.

**1. `co_code_adaptive` is a flexible array member at the *end* of the struct.** The
bytecode is not behind a pointer. It is allocated inline with the code object, in one
`malloc`. That is a deliberate locality decision (see
[`16-object-memory-layout.md`](16-object-memory-layout.md) §6): the eval loop's
instruction pointer and the code object's hot fields land in nearby cache lines.

**2. There is no `co_code` field.** `co_code` is a *derived, cached* attribute — that's
what `_co_cached` (a `_PyCoCached` holding `_co_code`, `_co_varnames`, `_co_cellvars`,
`_co_freevars`) exists for. Reading `co_code` materializes a `bytes` object from
`co_code_adaptive` **with the specializations stripped back out**. §5 shows this
happening.

**3. There is no `co_varnames`, `co_cellvars`, or `co_freevars` field either.** All three
are views over `co_localsplusnames` + `co_localspluskinds`. §6 and §11 explain why that
matters for reading `LOAD_DEREF`.

**4. `_PyCode_DEF_THREAD_LOCAL_BYTECODE()` expands to `_PyCodeArray *co_tlbc;` only when
`Py_GIL_DISABLED` is set.** On the free-threaded build each thread can get its *own copy*
of the bytecode array, because specialization writes into the instruction stream and two
threads specializing the same instruction differently would race. That single `#ifdef` is
the whole reason free-threaded code objects are bigger — measured, same nested function
on both builds *(real output)*:

| | GIL build 3.14.6 | free-threaded 3.14.6t |
|---|---|---|
| `sys.getsizeof(code)` | **264** | **288** |
| `len(co_code)` | 50 | 50 |

Same bytecode, +24 bytes of struct.

### The Python-level surface

Every `co_*` attribute that actually exists on a 3.14.6 code object *(real output from
`dir()`)*:

```
co_argcount        co_cellvars     co_code           co_consts       co_exceptiontable
co_filename        co_firstlineno  co_flags          co_freevars     co_kwonlyargcount
co_lines           co_linetable    co_lnotab         co_name         co_names
co_nlocals         co_positions    co_posonlyargcount co_qualname    co_stacksize
co_varnames        co_branches
```

Plus exactly one method: `replace()`. Note `co_branches` — added for coverage tooling,
and `co_lnotab`, which is a deprecated compatibility shim that *reconstructs* the pre-3.10
line table format on demand.

---

## 2. Wordcode: one byte of opcode, one byte of oparg

Since 3.6 the instruction stream is **wordcode**: every instruction is exactly two bytes —
one opcode byte, one oparg byte — so instructions are 2-byte aligned and the interpreter
never has to decode a variable-length prefix to find the next one. Instructions that take
no argument still carry an oparg byte; it is simply ignored.

Here is a real function, disassembled and then hand-decoded from its raw bytes.

```python
def calc(a, b, c):
    return a["k"] + b.attr * c
```

*(real output, 3.14.6)*

```
1:0-1:0              RESUME                   0
2:11-2:12            LOAD_FAST_BORROW         0 (a)
2:13-2:16            LOAD_CONST               0 ('k')
2:11-2:17            BINARY_OP               26 ([])
2:20-2:21            LOAD_FAST_BORROW         1 (b)
2:20-2:26            LOAD_ATTR                0 (attr)
2:29-2:30            LOAD_FAST_BORROW         2 (c)
2:20-2:30            BINARY_OP                5 (*)
2:11-2:30            BINARY_OP                0 (+)
2:4-2:30             RETURN_VALUE
```

Ten instructions — but `len(co_code)` is **68 bytes**, not 20. The missing 48 bytes are
inline caches (§4).

A cleaner hand-decode, from the tail of a different real function *(real output)*:

```
raw bytes:   4e 01 | 45 01 | 58 2b | 4e 01 | 35 06 | 23 00
```

| bytes | opcode | oparg | meaning |
|---|---|---|---|
| `4e 01` | `0x4e` = 78 = `LIST_APPEND` | 1 | append TOS to the list 1 below it |
| `45 01` | `0x45` = 69 = `EXTENDED_ARG` | 1 | prefix: high bits of the next oparg |
| `58 2b` | `0x58` = 88 = `LOAD_FAST_CHECK` | 0x2b = 43 | real oparg = `(1<<8)｜43` = **299** |
| `4e 01` | `LIST_APPEND` | 1 | |
| `35 06` | `0x35` = 53 = `CALL_INTRINSIC_1` | 6 | 6 = `INTRINSIC_LIST_TO_TUPLE` |
| `23 00` | `0x23` = 35 = `RETURN_VALUE` | 0 | oparg present but unused |

Everything in that table is checkable in one line: `opcode.opmap['LIST_APPEND']` is 78,
`dis._intrinsic_1_descs[6]` is `'INTRINSIC_LIST_TO_TUPLE'`.

**`HAVE_ARGUMENT` is 43 on 3.14.6** *(real output)*. Opcodes below it take no meaningful
argument; opcodes at or above it do. It is a threshold, not a flag bit, which is why the
opcode numbering is regenerated (and reshuffled) every release — see §13.

The 3.14.6 opcode table has **238 defined names**, of which everything numbered ≥ 256 is a
**pseudo-opcode**: `JUMP`, `JUMP_IF_FALSE`, `LOAD_CLOSURE`, `POP_BLOCK`, `SETUP_FINALLY`,
`SETUP_WITH`, `SETUP_CLEANUP`, `STORE_FAST_MAYBE_NULL`. These exist only inside the
compiler's CFG and are *resolved away* before the code object is built. They cannot appear
in `co_code` — one byte cannot hold 264. Keep `SETUP_FINALLY` in mind; §8 is about its
disappearance.

---

## 3. `EXTENDED_ARG`: the escape hatch, and its cost

One byte of oparg means a maximum of 255. Real programs exceed that: functions with more
than 256 locals, jumps further than 255 code units, tuples of more than 256 elements. The
fix is `EXTENDED_ARG` (opcode 69), which shifts an accumulator left by 8 and ORs in its own
oparg. Up to three may be chained, giving a 32-bit effective argument.

A real forward jump that needed one *(real output)*:

```
  off=   12 start_offset=   12 EXTENDED_ARG         arg=2      argrepr=''
  off=   14 start_offset=   12 POP_JUMP_IF_FALSE    arg=661    argrepr='to L1'
```

`661 = 0x295`, so the encoding is `EXTENDED_ARG 0x02` then `POP_JUMP_IF_FALSE 0x95`
(`0x95` = 149; `(2 << 8) | 149 = 661`). ✓

Note `offset` (14) and `start_offset` (12) differ. **`start_offset` is where the
instruction's `EXTENDED_ARG` prefixes begin; `offset` is where the real opcode sits.**
Jump targets are computed from `start_offset`, because jumping to `offset` would skip the
prefix and produce a wrong oparg. If you write a bytecode-rewriting tool and use the wrong
one, you get silent misbehaviour rather than an error. `dis.Instruction` has carried both
since 3.13 precisely because tool authors kept getting this wrong.

### The compiler's chicken-and-egg problem

`EXTENDED_ARG` makes instructions longer, which moves later instructions, which can make a
jump distance exceed 255, which requires *another* `EXTENDED_ARG`. CPython resolves this by
iterating jump-offset assignment to a fixed point during assembly. This is why "just insert
one instruction" is not a safe bytecode edit: every jump oparg after your insertion point
may need to change width, and widening one can cascade.

### The compiler dodges huge opargs where it can

Trying to force a `BUILD_TUPLE` with oparg > 255 fails, and the failure is instructive.
Measured on 3.14.6, calling `fn(a, a, ..., a)` with a growing argument count *(real
output)*:

| args | strategy | `co_stacksize` |
|---|---|---|
| 3 | `LOAD_FAST…` × 3 + `CALL 3` | 5 |
| 20 | `LOAD_FAST…` × 20 + `CALL 20` | **22** |
| 60 | `BUILD_LIST 0` + `LIST_APPEND` × 60 + `CALL_INTRINSIC_1 6` + `CALL_FUNCTION_EX` | **4** |

The switch happens **exactly at 31 arguments** (measured by bisection). That is not a
coincidence: `Include/internal/pycore_compile.h` defines

```c
#define _PY_STACK_USE_GUIDELINE 30
```

and `Python/codegen.c` tests `argsl + kwdsl + (kwdsl != 0) >= _PY_STACK_USE_GUIDELINE`
before choosing the incremental strategy. **The measured threshold and the source constant
agree.** The compiler is trading instruction count for stack depth — because
`co_stacksize` is preallocated per frame (§7), and a 300-slot frame for one call is a
worse deal than 300 extra instructions.

---

## 4. Inline caches as `CACHE` pseudo-instructions

This is the single biggest thing that changed about reading disassembly in 3.11+, and it is
why offsets in modern `dis` output look wrong until you know about it.

PEP 659's adaptive specializing interpreter needs per-instruction mutable state: a warm-up
counter, a type version tag, a resolved descriptor pointer. Rather than a side table (a
pointer chase, a cache miss), CPython **reserves space directly in the instruction stream**
after the instruction that owns it. Opcode 0 is `CACHE`, and the interpreter never executes
it — it just skips over it, because it knows statically how many entries each opcode has.

`dis.dis(f, show_caches=True)` makes them visible. Real 3.14.6 output for
`def attr_call(a, d): return a.b.c(d)`:

```
  2           RESUME                   0

  3           LOAD_FAST_BORROW         0 (a)
              LOAD_ATTR                0 (b)
              CACHE                    0 (counter: 0)
              CACHE                    0 (version: 0)
              CACHE                    0
              CACHE                    0 (keys_version: 0)
              CACHE                    0
              CACHE                    0 (descr: 0)
              CACHE                    0
              CACHE                    0
              CACHE                    0
              LOAD_ATTR                3 (c + NULL|self)
              CACHE                    0 (counter: 0)
              CACHE                    0 (version: 0)
              CACHE                    0
              CACHE                    0 (keys_version: 0)
              CACHE                    0
              CACHE                    0 (descr: 0)
              CACHE                    0
              CACHE                    0
              CACHE                    0
              LOAD_FAST_BORROW         1 (d)
              CALL                     1
              CACHE                    0 (counter: 0)
              CACHE                    0 (func_version: 0)
              CACHE                    0
              RETURN_VALUE
```

Six real instructions, 56 bytes. `dis.Bytecode(f, show_caches=True)` gives the same
information structurally, per instruction, via `cache_info` *(real output)*:

```
off=  0 RESUME            cache_info=None
off=  2 LOAD_FAST_BORROW  cache_info=None
off=  4 LOAD_ATTR         cache_info=[('counter', 1, b'\x00\x00'),
                                      ('version', 2, b'\x00\x00\x00\x00'),
                                      ('keys_version', 2, b'\x00\x00\x00\x00'),
                                      ('descr', 4, b'\x00\x00\x00\x00\x00\x00\x00\x00')]
off= 24 LOAD_ATTR         cache_info=[...same shape...]
off= 44 LOAD_FAST_BORROW  cache_info=None
off= 46 CALL              cache_info=[('counter', 1, b'\x00\x00'),
                                      ('func_version', 2, b'\x00\x00\x00\x00')]
off= 54 RETURN_VALUE      cache_info=None
```

Read the middle number as *code units*: `counter` is 1 unit (2 bytes), `version` is 2 units
(4 bytes), `descr` is 4 units (8 bytes — a pointer). 1+2+2+4 = **9 cache entries** for
`LOAD_ATTR`, so it occupies 2 + 18 = **20 bytes**. That is why the second `LOAD_ATTR` is at
offset 24 and not offset 6.

### The instruction stream, drawn to scale

```
 offset  0     2     4     6           22    24    26          42    44    46    48      54
         │     │     │     │            │     │     │           │     │     │     │       │
         ▼     ▼     ▼     ▼            ▼     ▼     ▼           ▼     ▼     ▼     ▼       ▼
       ┌─────┬─────┬─────┬──────────────┬─────┬─────────────────┬─────┬─────┬───────┬─────┐
       │RESU-│LOAD_│LOAD_│  CACHE × 9   │LOAD_│   CACHE × 9     │LOAD_│CALL │CACHE×3│RETU-│
       │ME   │FAST_│ATTR │ 18 bytes     │ATTR │   18 bytes      │FAST_│     │6 bytes│RN_  │
       │     │BORR.│     │              │     │                 │BORR.│     │       │VALUE│
       └─────┴─────┴─────┴──────────────┴─────┴─────────────────┴─────┴─────┴───────┴─────┘
        2B    2B    2B    ├─ counter 2B  2B    ├─ counter 2B     2B    2B   ├─counter  2B
                          ├─ version 4B        ├─ version 4B                └─func_ver 4B
                          ├─ keys_v  4B        ├─ keys_v  4B
                          └─ descr   8B        └─ descr   8B
                          ^^^^^^^^^^^^^^
                          NOT executed. The interpreter's instruction
                          pointer jumps straight over it — the size is
                          a compile-time constant per opcode, so the
                          skip is `next_instr += N`, not a table lookup.

  6 executable instructions  ·  12 bytes of opcode  ·  44 bytes of cache  ·  56 bytes total
```

**79% of this function's bytecode is cache space.** That is the deal PEP 659 struck: a
much larger code object in exchange for specialization state that is one predictable
sequential read away from the instruction that needs it — no pointer chase, no separate
cache line. Go back to [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
and this is obviously the right call; go back to
[`16-object-memory-layout.md`](16-object-memory-layout.md) and you can see who pays for it
(anything that keeps a lot of rarely-executed code resident, e.g. a large import graph in a
memory-constrained container).

The full per-opcode cache table on 3.14.6, from `dis._inline_cache_entries` *(real output)*:

| opcode | entries | opcode | entries |
|---|---|---|---|
| `LOAD_ATTR` | **9** | `TO_BOOL` | 3 |
| `BINARY_OP` | **5** | `CALL` / `CALL_KW` | 3 |
| `LOAD_GLOBAL` | 4 | `COMPARE_OP`, `CONTAINS_OP` | 1 |
| `STORE_ATTR` | 4 | `FOR_ITER`, `SEND` | 1 |
| `JUMP_BACKWARD` | 1 | `POP_JUMP_IF_*` (4 of them) | 1 |
| `STORE_SUBSCR`, `UNPACK_SEQUENCE`, `LOAD_SUPER_ATTR` | 1 | | |

`BINARY_OP` having 5 is new-ish and 3.14-specific: subscription was folded into
`BINARY_OP` (there is no `BINARY_SUBSCR` opcode in 3.14 — `a[b]` compiles to
`BINARY_OP 26 ([])`), and the merged instruction needs the larger cache. If you have a
3.13-era table memorized, it is already wrong.

---

## 5. Reading `dis` fluently: `adaptive=True` and specialized names

The 3.14.6 signature *(real output)*:

```python
dis.dis(x=None, *, file=None, depth=None, show_caches=False,
        adaptive=False, show_offsets=False, show_positions=False)
```

`adaptive=True` is the one that teaches you the most, and it only does anything on a code
object that has actually **run**. Here is the same function before and after 200 calls ×
50 iterations — real 3.14.6 output, one interpreter session:

```python
def hot(xs, obj):
    t = 0
    for v in xs:
        t = t + v + obj.k
    return t
```

**Before warm-up** — `dis.dis(hot, adaptive=True)`:

```
  4         LOAD_FAST_BORROW         0 (xs)
            GET_ITER
    L1:     FOR_ITER                28 (to L2)
            STORE_FAST               3 (v)
  5         LOAD_FAST_BORROW_LOAD_FAST_BORROW 35 (t, v)
            BINARY_OP                0 (+)
            LOAD_FAST_BORROW         1 (obj)
            LOAD_ATTR                0 (k)
            BINARY_OP                0 (+)
            STORE_FAST               2 (t)
            JUMP_BACKWARD           30 (to L1)
```

**After warm-up** — same call, same code object:

```
  2         RESUME_CHECK             0
  4         LOAD_FAST_BORROW         0 (xs)
            GET_ITER
    L1:     FOR_ITER_RANGE          28 (to L2)
            STORE_FAST               3 (v)
  5         LOAD_FAST_BORROW_LOAD_FAST_BORROW 35 (t, v)
            BINARY_OP_ADD_INT        0 (+)
            LOAD_FAST_BORROW         1 (obj)
            LOAD_ATTR_INSTANCE_VALUE 0 (k)
            BINARY_OP_ADD_INT        0 (+)
            STORE_FAST               2 (t)
            JUMP_BACKWARD_NO_JIT    30 (to L1)
```

Five instructions rewrote themselves in place. `FOR_ITER` learned it was iterating a
`range`; `BINARY_OP` learned both operands were `int`; `LOAD_ATTR` learned the attribute
lives in the instance values array; `RESUME` learned it does not need the full eval-breaker
check; `JUMP_BACKWARD` recorded that this loop is not JIT-hot. Each of those is a shorter
instruction body with a **guard** at the top that deoptimizes back to the generic form if
the assumption breaks. That mechanism is doc 20's subject; what matters here is that you
can *see* it, per instruction, from pure Python.

### The de-specialization trap

Run `dis.dis(hot, adaptive=False)` on that same warmed code object and you get the generic
names back — `RESUME`, `FOR_ITER`, `BINARY_OP`, `LOAD_ATTR`. Verified at byte level *(real
output)*:

```
co_code identical before/after 1000 warm calls: True
dis(adaptive=True)  -> ['RESUME_CHECK', 'LOAD_FAST_BORROW', 'LOAD_ATTR_INSTANCE_VALUE', 'RETURN_VALUE']
dis(adaptive=False) -> ['RESUME',       'LOAD_FAST_BORROW', 'LOAD_ATTR',                'RETURN_VALUE']
```

**`co_code` is a lie by design.** It reconstructs the *unspecialized* bytecode, with cache
slots zeroed, every time you read it. The live array is `co_code_adaptive` and there is no
Python-level accessor for it. This is deliberate and load-bearing: it means `co_code` is
stable and hashable, `marshal`/`.pyc` round-trips are deterministic, and tools that read
`co_code` see the same bytes whether the function is cold or blazing hot. It also means
**you cannot measure specialization by reading `co_code`** — `adaptive=True` is the only
supported window, and on a free-threaded build you are looking at one thread's copy of
`co_tlbc` (§1).

The full specialization families are in `_opcode_metadata._specializations` — 13 variants
for `LOAD_ATTR`, 20 for `CALL`, 15 for `BINARY_OP` on 3.14.6. When you see an unfamiliar
`SCREAMING_SNAKE_NAME` in adaptive output, that dict tells you its base opcode.

---

## 6. The name tables, and the localsplus trick

An oparg is 8 bits (or 32 with `EXTENDED_ARG`) — it cannot hold a string. Every name in
Python bytecode is therefore an **index into a tuple on the code object**, and knowing
*which* tuple is how you read an oparg.

| Instruction family | oparg indexes | Lookup at runtime |
|---|---|---|
| `LOAD_CONST` | `co_consts` | array index — free |
| `LOAD_NAME`, `LOAD_GLOBAL`, `STORE_GLOBAL`, `LOAD_ATTR`, `STORE_ATTR`, `IMPORT_NAME` | `co_names` | **dict lookup** by string |
| `LOAD_FAST`, `STORE_FAST`, `DELETE_FAST` | localsplus (see below) | array index — free |
| `LOAD_DEREF`, `STORE_DEREF`, `MAKE_CELL` | localsplus | array index + one cell deref |

That table *is* the reason `LOAD_FAST` is fast and `LOAD_GLOBAL` is not. Same oparg width,
completely different work: one is `frame->localsplus[oparg]`, the other is a hash lookup in
the globals dict and possibly a second one in builtins. The classic
"hoist `len` into a local" micro-optimization is exactly this table, and PEP 659's
`LOAD_GLOBAL_MODULE` / `LOAD_GLOBAL_BUILTIN` specializations exist to close the gap by
caching the dict's version tag.

### localsplus

Locals, cell variables, and free variables all live in **one flat array** on the frame,
described by `co_localsplusnames` and `co_localspluskinds`. `co_varnames`, `co_cellvars`
and `co_freevars` are derived views over it, in that order. Measured on a real closure
*(real output)*:

```
inner.co_varnames = ('k', 'j')
inner.co_cellvars = ()
inner.co_freevars = ('acc', 'n')
implied localsplus  = ('k', 'j', 'acc', 'n')

LOAD_DEREF  arg=2  argrepr='acc'      <- index 2 in localsplus, NOT index 0 in co_freevars
LOAD_DEREF  arg=3  argrepr='n'
```

**This is the single most common misreading of closure bytecode.** `LOAD_DEREF 2` does not
mean "the third free variable". It means "localsplus slot 2", which happens to be the first
free variable because there are two positional locals ahead of it. If you index
`co_freevars[2]` you get an `IndexError` or, worse, the wrong name. The correct
reconstruction is `(co_varnames + co_cellvars + co_freevars)[oparg]` — which is exactly
what `dis` does to produce that `argrepr`.

### `co_flags`

Measured on 3.14.6 *(real output)*, with `inspect.CO_*` names:

| Construct | `co_flags` | Set flags |
|---|---|---|
| `def plain(a, b=1, *args, **kw)` | `0x0000000f` | `CO_OPTIMIZED｜CO_NEWLOCALS｜CO_VARARGS｜CO_VARKEYWORDS` |
| `def gen(): yield 1` | `0x00000023` | `+ CO_GENERATOR` |
| `async def coro()` | `0x00000083` | `+ CO_COROUTINE` |
| `async def agen(): yield 1` | `0x00000203` | `+ CO_ASYNC_GENERATOR` |
| `def doc(): "hello"` | `0x04000003` | `+ CO_HAS_DOCSTRING` |
| a method in a class body | `0x08000003` | `+ CO_METHOD` |
| `lambda: 0` | `0x00000003` | `CO_OPTIMIZED｜CO_NEWLOCALS` |
| module top level | `0x00000000` | **none** |
| class body | `0x00000000` | **none** |

Two things to take from this. **`CO_OPTIMIZED` is what makes `LOAD_FAST` legal** — module
and class bodies don't have it, which is why they use `LOAD_NAME` (a dict lookup) for
everything, and why a loop at module scope is measurably slower than the identical loop in
a function. And `CO_HAS_DOCSTRING` (0x4000000) and `CO_METHOD` (0x8000000) are recent
additions in the high bits; anyone treating `co_flags` as a small integer or comparing it
for equality against a hardcoded constant has already broken.

Argument counts are three separate fields. For `def sig(p, /, q, *args, r, s=1, **kw)`
*(real output)*: `co_argcount=2`, `co_posonlyargcount=1`, `co_kwonlyargcount=2`,
`co_nlocals=6`, `co_varnames=('p','q','r','s','args','kw')`. Note `co_argcount` **includes**
positional-only args (2 = `p` and `q`, of which 1 is positional-only), and that `*args` and
`**kw` sort to the *end* of `co_varnames`, after the keyword-only ones. Getting this
ordering wrong is how signature-reconstructing decorators break.

---

## 7. `co_stacksize`, stack effects, and a real segfault

CPython's eval loop is a stack machine. The value stack is **not** a growable structure —
it is a contiguous `_PyStackRef` array preallocated as part of the frame, sized from
`co_stacksize`. From `InternalDocs/interpreter.md`:

> Its maximum depth is calculated by the compiler and stored in the `co_stacksize` field of
> the code object, so that the stack can be pre-allocated as a contiguous array of
> `PyObject*` pointers, when the frame is created.

That preallocation is why frame creation is cheap, and it is why `co_stacksize` being
correct is a **memory-safety invariant**, not a hint.

### How it is computed

Not by simulating the bytecode linearly — that would be wrong in the presence of jumps.
`calculate_stackdepth()` in `Python/flowgraph.c` walks the **CFG**: it seeds the entry block
at depth 0, then does a worklist traversal, propagating each block's exit depth to its
successors and asserting consistency where paths merge (`"Invalid CFG, inconsistent
stackdepth"`). The maximum depth seen anywhere becomes `co_stacksize`. Exception handler
targets are seeded from the `depth` recorded in the exception table (§8), which is exactly
why that field exists.

`opcode.stack_effect(op, arg, jump=...)` exposes the per-instruction deltas the traversal
uses. Real values on 3.14.6 *(real output)*:

| instruction | oparg | stack effect |
|---|---|---|
| `LOAD_FAST` / `LOAD_CONST` | 0 | **+1** |
| `POP_TOP` | – | −1 |
| `BINARY_OP` | 0 | −1 (pops 2, pushes 1) |
| `CALL` | 1 | **−2** |
| `CALL` | 3 | **−4** |
| `BUILD_TUPLE` | 5 | −4 |
| `UNPACK_SEQUENCE` | 3 | +2 |
| `PUSH_EXC_INFO` | – | +1 |
| `COPY` | 1 | +1 |
| `SWAP` | 2 | 0 |
| `CACHE` | – | **0** |
| `LOAD_ATTR` | **0** | **0** |
| `LOAD_ATTR` | **1** | **+1** |
| `LOAD_GLOBAL` | **0** | +1 |
| `LOAD_GLOBAL` | **1** | **+2** |

Note `CALL n` is `−(n+1)`, not `−n`: the callable *and* a `NULL`/`self` slot are both on the
stack below the arguments, and the result replaces all of it. And look at the last four
rows — `LOAD_ATTR`'s stack effect **depends on the low bit of its oparg**. That is §12.

### The invariant, tested

Setting `co_stacksize` too low is accepted by the constructor without complaint, because
`code.replace()` does no verification. Whether it crashes depends on whether the overflow
reaches unmapped memory. Two real attempts:

```
def deep(a): return [a, a, ... 64 times ...]
  true co_stacksize = 2   (the compiler used BUILD_LIST + LIST_APPEND, §3)
  forced to 1, called -> survived, returned a 64-element list.  exit code 0
```

```
def deep(a, fn): return fn(a, a, ... 20 times ...)
  true co_stacksize = 22
  forced to 1, called ->  exit code 139
```

**Exit code 139 is SIGSEGV.** No exception, no `SystemError`, no traceback — the process
died writing 21 pointers past the end of the frame. The first attempt "passed" only because
the compiler had already flattened that expression to a stack depth of 2 (the `n=60` row in
§3's table); once I picked an expression that genuinely needs a deep stack, the invariant
bit immediately.

This is the concrete answer to "why is bytecode a private implementation detail" (§13):
`co_code`, `co_stacksize` and `co_exceptiontable` are three mutually-consistent
descriptions of the same program, nothing checks that they agree, and disagreement is
memory corruption rather than an error.

---

## 8. Zero-cost exception handling

Before 3.11, a `try` block executed a `SETUP_FINALLY` instruction on entry and a
`POP_BLOCK` on exit, pushing and popping an entry on a per-frame **block stack**. That is
real work on the path where **nothing goes wrong** — which is the overwhelmingly common
path. From `InternalDocs/exception_handling.md`:

> `SETUP_FINALLY` and `POP_BLOCK` have no effect when no exceptions are raised. The idea of
> zero-cost exception handling is to replace these pseudo-instructions by metadata which is
> stored alongside the bytecode, and which is inspected only when an exception occurs. This
> metadata is the exception table, and it is stored in the code object's
> `co_exceptiontable` field.

And, plainly, on what "zero-cost" means:

> In the common case (where no exception is raised) the cost is reduced to zero (or close
> to zero). The cost of raising an exception is increased, but not by much.

**"Zero-cost" is a claim about the non-exceptional path only.** Raising got *more*
expensive: instead of popping a block off a stack you already maintained, the unwinder now
does a **binary search over a compressed varint table**. If you are using exceptions as
control flow in a hot loop — `try: return d[k] except KeyError:` on a mostly-missing key —
3.11+ made that trade against you. The idiom is still fine when the exception is genuinely
exceptional; it is a real regression when it isn't. Measure, don't assume.

### The table, for a real function

```python
def guarded(x):
    try:
        r = risky(x)
    except ValueError as e:
        r = handle(e)
    finally:
        cleanup()
    return r
```

`dis` prints the table after the disassembly *(real output, 3.14.6)*:

```
ExceptionTable:
  L1 to L2 -> L3 [0]
  L3 to L4 -> L8 [1] lasti
  L4 to L5 -> L6 [1] lasti
  L5 to L6 -> L9 [0]
  L6 to L8 -> L8 [1] lasti
  L8 to L9 -> L9 [0]
  L9 to L10 -> L10 [1] lasti
```

and `dis.Bytecode(guarded).exception_entries` gives it numerically *(real output)*:

```
start=  4 end= 26 target= 50 depth=0 lasti=False
start= 50 end= 72 target=114 depth=1 lasti=True
start= 72 end= 94 target=104 depth=1 lasti=True
start= 94 end=104 target=120 depth=0 lasti=False
start=104 end=114 target=114 depth=1 lasti=True
start=114 end=120 target=120 depth=0 lasti=False
start=120 end=144 target=144 depth=1 lasti=True
```

Seven entries for one `try/except/finally`. The `finally` body is **duplicated** in the
bytecode — once at offset 26 for the normal path, once at offset 122 for the exceptional
path — which is another instance of the same trade: bigger code object, no runtime
bookkeeping.

### What a raise actually does

```
 RAISE at offset 84, inside `handle(e)`
    │
    ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  1. unwinder reads frame->instr_ptr  ->  lasti = 84                      │
 └───────────────────────────────┬──────────────────────────────────────────┘
                                 ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  2. BINARY SEARCH co_exceptiontable for the entry with start <= 84 < end │
 │                                                                          │
 │      [  4, 26) -> 50   depth 0                                           │
 │      [ 50, 72) ->114   depth 1  lasti                                    │
 │      [ 72, 94) ->104   depth 1  lasti   ◀── 72 <= 84 < 94.  MATCH.       │
 │      [ 94,104) ->120   depth 0                                           │
 │      [104,114) ->114   depth 1  lasti                                    │
 │      [114,120) ->120   depth 0                                           │
 │      [120,144) ->144   depth 1  lasti                                    │
 │                                                                          │
 │   entries are sorted and non-overlapping by construction, so this is     │
 │   O(log n) — but it is a *search*, which the block stack never was.      │
 └───────────────────────────────┬──────────────────────────────────────────┘
                                 ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  3. POP the value stack down to depth == 1, DECREF everything above      │
 │     (this is why `depth` is in the table at all: the handler needs a     │
 │      known stack shape, and the unwinder must not leak the temporaries)  │
 └───────────────────────────────┬──────────────────────────────────────────┘
                                 ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  4. lasti is set  ->  PUSH the offset 84 as an int, so the handler can   │
 │     re-raise with the correct originating instruction                    │
 └───────────────────────────────┬──────────────────────────────────────────┘
                                 ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  5. PUSH the exception object; JUMP to offset 104                        │
 │     (offset 104 starts the `del e` cleanup, then RERAISE 1)              │
 └──────────────────────────────────────────────────────────────────────────┘

 No match at any level  ->  pop the frame, repeat in the caller.
 That loop is how a traceback is built: one frame per unwinding step.
```

### The varint encoding, decoded by hand

`co_exceptiontable` for `guarded` is 29 bytes *(real output)*:

```
82 0b 19 00  99 0b 39 03  a4 0b 34 03  af 05 3c 00  b4 05 39 03  b9 03 3c 00  bc 0c 41 08 03
```

Each entry is four varints: `start`, `length`, `target`, `depth_and_lasti`. Each varint byte
carries **6 data bits** (bits 0–5); **bit 6 (`0x40`) means "continue"**; **bit 7 (`0x80`)
marks the first byte of an entry**. All offsets are in *code units*, so multiply by 2 for
byte offsets.

Take the last entry, `bc 0c 41 08 03` — the only one here that needs a continuation byte:

```
 bc = 1011_1100   bit7=1 START · bit6=0 no-cont · data=0b111100=60
                  -> start  = 60 units = 120 bytes
 0c = 0000_1100   bit6=0 · data=12
                  -> length = 12 units =  24 bytes   -> end = 144
 41 = 0100_0001   bit6=1 CONTINUE · data=1
 08 = 0000_1000   bit6=0 · data=8      -> (1 << 6) | 8 = 72
                  -> target = 72 units = 144 bytes
 03 = depth_and_lasti = 3
                  -> lasti = 3 & 1 = True ,  depth = 3 >> 1 = 1
```

Which is exactly the last row of the numeric dump above: `start=120 end=144 target=144
depth=1 lasti=True`. ✓

I wrote a 12-line decoder against this spec and compared all seven entries to
`dis.Bytecode(...).exception_entries` — **exact match** *(real output:
`hand-decoded == dis.exception_entries ? True`)*. The decoder is Lab 4.

Two design notes worth internalizing. `depth` and `lasti` are packed into one varint
(`depth << 1 | lasti`) because entries are dense and one saved byte per entry matters at
stdlib scale. And **`SETUP_FINALLY` still exists** — as pseudo-opcode 264, used inside the
compiler's CFG and resolved into table entries before assembly. The instruction didn't
disappear from the compiler, only from the instruction stream.

---

## 9. Source locations: `co_linetable` and `co_positions`

Two related mechanisms, two PEPs, one storage field.

**PEP 626 (3.10)** required that every executed instruction has a line number and that
tracing sees every executed line — which killed the old `co_lnotab` delta encoding and
introduced `co_linetable`. `co_lines()` is the supported reader *(real output for `calc`)*:

```
bytes [  0,  2) -> line 1
bytes [  2, 68) -> line 2
```

**PEP 657 (3.11)** extended each entry from a line number to a full
`(lineno, end_lineno, col_offset, end_col_offset)` quadruple, stored in the *same*
`co_linetable` bytes. `co_positions()` reads it back. Correctly aligned via
`Instruction.positions` *(real output)*:

| offset | opname | line | col | endcol | source slice |
|---|---|---|---|---|---|
| 0 | `RESUME` | 1 | 0 | 0 | `''` |
| 2 | `LOAD_FAST_BORROW` | 2 | 11 | 12 | `'a'` |
| 4 | `LOAD_CONST` | 2 | 13 | 16 | `'"k"'` |
| 6 | `BINARY_OP` | 2 | 11 | 17 | `'a["k"]'` |
| 18 | `LOAD_FAST_BORROW` | 2 | 20 | 21 | `'b'` |
| 20 | `LOAD_ATTR` | 2 | 20 | 26 | `'b.attr'` |
| 40 | `LOAD_FAST_BORROW` | 2 | 29 | 30 | `'c'` |
| 42 | `BINARY_OP` | 2 | 20 | 30 | `'b.attr * c'` |
| 54 | `BINARY_OP` | 2 | 11 | 30 | `'a["k"] + b.attr * c'` |
| 66 | `RETURN_VALUE` | 2 | 4 | 30 | `'return a["k"] + b.attr * c'` |

That last column is the whole point: each instruction's span is exactly the sub-expression
it evaluates, and the spans nest the way the AST does. Which is what produces this *(real
output — an actual traceback from 3.14.6)*:

```
Traceback (most recent call last):
  File "/tmp/pep657.py", line 3, in <module>
    calc({"k": 1}, None, 1)
    ~~~~^^^^^^^^^^^^^^^^^^^
  File "/tmp/pep657.py", line 2, in calc
    return a["k"] + b.attr * c
                    ^^^^^^
AttributeError: 'NoneType' object has no attribute 'attr'
```

The carets under `b.attr` are `col_offset=20, end_col_offset=26` on the `LOAD_ATTR` at
offset 20, read straight out of `co_linetable`. **This is the highest-value feature in this
document for day-to-day work** and it costs zero runtime — it is metadata consulted only
when formatting a traceback.

### The alignment trap

`co_positions()` yields **one entry per code unit**, including `CACHE` entries. `dis.Bytecode()`
yields **one entry per executable instruction**, hiding caches. Measured for `calc`
*(real output)*:

```
len(co_code)//2                = 34
len(list(co_positions()))      = 34
instructions shown by dis      = 10
```

Naively `zip()`-ing those two iterators — which is the obvious thing to write, and which I
did first while producing this document — silently misattributes every position after the
first cached instruction. In my first run `LOAD_ATTR` came out pointing at `'a["k"]'`
instead of `'b.attr'`, and nothing errored. **Use `Instruction.positions`, or
`dis.dis(f, show_positions=True)`, and never zip.**

> **Could not verify:** I did **not** hand-decode the `co_linetable` byte format the way I
> did the exception table in §8. PEP 626 is explicit that it is *"opaque, unspecified and
> may be changed without notice"*, and unlike the exception table there is no stable public
> decoder to check a hand-written one against. The raw bytes for `calc` are
> `80 00 d8 0b 0c 88 53 8d 36 90 41 97 46 91 46 98 51 95 4a d5 0b 1e d0 04 1e` (25 bytes
> for 34 code units) if you want to attack it — but treat any explanation of those bytes,
> including one you find online, as version-specific until you check it against
> `Objects/locations.md` and `Objects/codeobject.c` for **your** build. **Use `co_lines()`
> and `co_positions()`; never parse `co_linetable` yourself.**

---

## 10. Comprehension inlining (PEP 709)

Before 3.12, a list/dict/set comprehension compiled to a **separate code object** that was
wrapped in a throwaway function and called. Every evaluation allocated a function object,
pushed a frame, ran, popped, and threw both away.

PEP 709 (3.12) inlined them. The "before" bytecode below is **quoted from PEP 709 itself,
not generated locally** — I have no 3.11 interpreter on this machine and will not fabricate
disassembly:

> ```
> 2           2 LOAD_CONST               1 (<code object <listcomp> at 0x...)
>             4 MAKE_FUNCTION            0
>             6 LOAD_FAST                0 (lst)
>             8 GET_ITER
>            10 CALL                     ...
> ```

Here is the real 3.14.6 "after", for `def comp(xs): return [y*2 for y in xs if y]`
*(real output)*:

```
  3          2       LOAD_FAST_BORROW         0 (xs)
             4       GET_ITER
             6       LOAD_FAST_AND_CLEAR      1 (y)
             8       SWAP                     2
      L1:   10       BUILD_LIST               0
            12       SWAP                     2
      L2:   14       FOR_ITER                21 (to L5)
            18       STORE_FAST_LOAD_FAST    17 (y, y)
            20       TO_BOOL
      L3:   28       POP_JUMP_IF_TRUE         3 (to L4)
            32       NOT_TAKEN
            34       JUMP_BACKWARD           12 (to L2)
      L4:   38       LOAD_FAST_BORROW         1 (y)
            40       LOAD_SMALL_INT           2
            42       BINARY_OP                5 (*)
            54       LIST_APPEND              2
            56       JUMP_BACKWARD           23 (to L2)
      L5:   60       END_FOR
            62       POP_ITER
      L6:   64       SWAP                     2
            66       STORE_FAST               1 (y)
            68       RETURN_VALUE

  --   L7:   70       SWAP                     2
            72       POP_TOP
   3         74       SWAP                     2
            76       STORE_FAST               1 (y)
            78       RERAISE                  0
ExceptionTable:
  L1 to L3 -> L7 [2]
  L4 to L6 -> L7 [2]
```

and the proof that it is genuinely inlined *(real output)*:

```
co_consts    = (2,)                  <- no nested code object
co_varnames  = ('xs', 'y')           <- y is a local of comp() itself
nested code objects: []
```

No `MAKE_FUNCTION`. No `CALL`. No frame. The comprehension body is straight-line code in
the enclosing function.

### The `LOAD_FAST_AND_CLEAR` / `SWAP` dance

Inlining creates a scoping problem: the comprehension's iteration variable `y` must not
leak into or clobber an enclosing `y`. Since it is now a real local of `comp`, CPython
saves and restores it around the comprehension:

1. `LOAD_FAST_AND_CLEAR 1 (y)` — push the *old* value of `y` (or `NULL` if unbound) and set
   the slot to `NULL`. It is the "and clear" that makes the variable unbound inside, so a
   `NameError` still happens if you'd expect one.
2. `SWAP 2` twice, to bury the saved value beneath the accumulator list.
3. …loop…
4. `SWAP 2; STORE_FAST 1 (y)` — restore.

And that saved value must be restored **even if the loop body raises**, which is what the
two exception-table entries are for: `L1 to L3` and `L4 to L6` both target `L7`, whose only
job is to restore `y` and re-raise. **A pure performance optimization needed the exception
table from §8 to preserve semantics.** That is the mechanism worth remembering.

The dict comprehension does the same with three saved slots *(real output)*:
`LOAD_FAST_AND_CLEAR 1 (k)`, `LOAD_FAST_AND_CLEAR 2 (v)`, `SWAP 3`, one exception entry
`L1 to L4 -> L5 [3]`.

### What is *not* inlined

Generator expressions. Real 3.14.6 output for `def gen(xs): return (y*2 for y in xs if y)`:

```
  3          2       LOAD_CONST               0 (<code object <genexpr> at 0x...>)
             4       MAKE_FUNCTION
             6       LOAD_FAST_BORROW         0 (xs)
             8       GET_ITER
            10       CALL                     0
            18       RETURN_VALUE
```

Still a separate code object, still a `MAKE_FUNCTION`. It has to be: a genexp's frame
outlives the expression by construction — that is what makes it lazy. So the
"comprehension vs genexp" performance question has a *structural* answer since 3.12, not a
folklore one: comprehensions became meaningfully cheaper and genexps did not change.

The user-visible consequences PEP 709 accepted: the comprehension frame no longer appears
in tracebacks or in `sys.settrace` output, and there is no `<listcomp>` code object to find
via `co_consts`. Tools that counted frames broke.

---

## 11. Closures: `MAKE_CELL`, `COPY_FREE_VARS`, `LOAD_DEREF`

```python
def outer(n):
    acc = 0
    def inner(k):
        return acc + k + n
    return inner
```

**`outer`** *(real output, 3.14.6)*:

```
  --          0       MAKE_CELL                0 (n)
              2       MAKE_CELL                2 (acc)
   2          4       RESUME                   0
   3          6       LOAD_SMALL_INT           0
              8       STORE_DEREF              2 (acc)
   4         10       LOAD_FAST_BORROW         2 (acc)
             12       LOAD_FAST_BORROW         0 (n)
             14       BUILD_TUPLE              2
             16       LOAD_CONST               1 (<code object inner ...>)
             18       MAKE_FUNCTION
             20       SET_FUNCTION_ATTRIBUTE   8 (closure)
             22       STORE_FAST               1 (inner)
   6         24       LOAD_FAST_BORROW         1 (inner)
             26       RETURN_VALUE
```

**`inner`** *(real output)*:

```
  --          0       COPY_FREE_VARS           2
   4          2       RESUME                   0
   5          4       LOAD_DEREF               1 (acc)
              6       LOAD_FAST_BORROW         0 (k)
              8       BINARY_OP                0 (+)
             20       LOAD_DEREF               2 (n)
             22       BINARY_OP                0 (+)
             34       RETURN_VALUE
```

The mechanism, in order:

**`MAKE_CELL` runs before `RESUME`** — note the `--` line marker, meaning "no source line".
It is *prologue*, executed once at frame setup, and it replaces localsplus slot *i* with a
fresh `cell` object wrapping whatever was there. `n` is a parameter, so its slot already
holds the argument; `MAKE_CELL 0` boxes it in place. This is why closing over a parameter
costs an allocation the moment any nested function references it, and why the same function
without the nested `def` has no cells at all.

**`STORE_DEREF` writes *through* the cell**, `STORE_FAST` writes the slot. That one-word
difference is the entire "why does the nested function see my later assignment?" question:
the cell is shared, so both frames see one mutable box. It is also the mechanism behind the
late-binding closure surprise (`[lambda: i for i in range(3)]` all returning 2) — every
lambda holds the *same cell*, not a snapshot.

**The closure is built as a plain tuple of cells and attached to the function object**, not
to the code object: `BUILD_TUPLE 2` then `SET_FUNCTION_ATTRIBUTE 8 (closure)`. The code
object is immutable and shared across every `inner` ever created; the *cells* are per-call.
That separation is what lets one code object back a thousand distinct closures.

**`COPY_FREE_VARS 2` is `inner`'s prologue**: copy 2 cells from `func->func_closure` into
the tail of this frame's localsplus array. After it runs, free variables are indexed exactly
like locals — which is why the eval loop needs no special case for them, and why
`LOAD_DEREF 1` means localsplus slot 1 (§6). In `inner`, `co_varnames=('k',)` and
`co_freevars=('acc','n')`, so slot 0 is `k`, slot 1 is `acc`, slot 2 is `n`. ✓

Free-threading note: this is identical on 3.14.6t *(verified — same opargs, same
`COPY_FREE_VARS 2`)*. Cells are ordinary `PyObject`s and get per-object locking like
anything else; the bytecode did not need to change.

---

## 12. Oparg encoding tricks

An oparg is a scarce 8 bits, and CPython spends the low bits on flags rather than adding
opcodes. Two patterns, both verified.

### The `LOAD_ATTR` / `LOAD_GLOBAL` low bit

*(real output, 3.14.6)*

| source | instruction | oparg | `arg >> 1` | name | `arg & 1` | `argrepr` |
|---|---|---|---|---|---|---|
| `o.meth(1)` | `LOAD_ATTR` | **1** | 0 | `'meth'` | **1** | `'meth + NULL｜self'` |
| `o.plain` | `LOAD_ATTR` | **0** | 0 | `'plain'` | 0 | `'plain'` |
| `helper(2)` | `LOAD_GLOBAL` | **1** | 0 | `'helper'` | **1** | `'helper + NULL'` |
| `SOMEGLOBAL` | `LOAD_GLOBAL` | **0** | 0 | `'SOMEGLOBAL'` | 0 | `'SOMEGLOBAL'` |

**The name index is `oparg >> 1`. The low bit means "this is about to be called."**

- `LOAD_ATTR` with the low bit set is the old `LOAD_METHOD`: it pushes the bound method
  *or* — for the common case of a plain function found on the type — it pushes the
  underlying function plus `self` as two separate stack entries, **skipping the bound-method
  object allocation entirely**. That is the single biggest reason method calls got cheaper
  in 3.11+.
- `LOAD_GLOBAL` with the low bit set additionally pushes a `NULL` where a `self` would go,
  so the `CALL` sequence has one uniform stack shape whether or not there is a receiver.
  `CALL` doesn't need to know which case it is in.

This is directly observable in the stack effects from §7: `LOAD_ATTR 0` → **0**,
`LOAD_ATTR 1` → **+1**; `LOAD_GLOBAL 0` → +1, `LOAD_GLOBAL 1` → **+2**. Same opcode,
different arity, decided by one bit of oparg.

**The trap:** `co_names[instr.arg]` is wrong for these two opcodes and right for every
other name-using opcode. It will silently return the wrong name (or `IndexError`) on any
method call. Always `co_names[instr.arg >> 1]` for `LOAD_ATTR` and `LOAD_GLOBAL`.

### The 4+4 packed pairs

3.13/3.14 added superinstructions that fuse two adjacent local accesses, packing **two
4-bit indices into one oparg**. Verified *(real output)*:

```
co_varnames = ('p', 'q', 'x', 'y')
LOAD_FAST_BORROW_LOAD_FAST_BORROW  oparg=1  -> hi=0 (p)  lo=1 (q)   argrepr='p, q'
```

`oparg >> 4` is the first index, `oparg & 15` the second. The family on 3.14.6 is
`LOAD_FAST_LOAD_FAST`, `LOAD_FAST_BORROW_LOAD_FAST_BORROW`, `STORE_FAST_LOAD_FAST`,
`STORE_FAST_STORE_FAST`. Seen earlier in this document: `STORE_FAST_LOAD_FAST 17 (y, y)`
— `17 = 0x11` → hi=1, lo=1, both `y`. And `LOAD_FAST_BORROW_LOAD_FAST_BORROW 35 (t, v)` —
`35 = 0x23` → hi=2 (`t`), lo=3 (`v`). ✓

Consequence: **these superinstructions only work for the first 16 localsplus slots.** A
function with many locals stops getting them past slot 15 — a small, real, entirely
invisible performance cliff. Note also `LOAD_FAST_BORROW`, new in 3.14: it loads a local
*without incrementing its refcount*, valid where the compiler can prove the local outlives
the use. Given [`24-the-gil.md`](24-the-gil.md) §1, eliminating a refcount write is worth
its own opcode.

> **Could not verify:** I did not confirm from the 3.14 source *which* analysis in
> `Python/flowgraph.c` decides when `LOAD_FAST` may be demoted to `LOAD_FAST_BORROW`. The
> opcode's existence and its use in real disassembly are verified above; the safety
> condition is my reading of the name, not something I checked. Grep `LOAD_FAST_BORROW` in
> `Python/flowgraph.c` and `Python/bytecodes.c` on your own checkout before repeating it.

---

## 13. Bytecode is a private implementation detail

The `dis` documentation says this outright, and it is not boilerplate:

> Bytecode is an implementation detail of the CPython interpreter. No guarantees are made
> that bytecode will not be added, removed, or changed between versions of Python.

Here is what that means concretely, from things established in this document:

**Opcode *numbers* are regenerated every release.** They are assigned by a build-time script
into `Include/opcode_ids.h`, ordered so that all argument-less opcodes sort below
`HAVE_ARGUMENT` (43 on 3.14.6). Add one instruction and every number after it moves. Any
tool with a hardcoded opcode number is version-locked by construction.

**Opcode *names* change too, inside a single release line.** Three concrete 3.14 changes,
all verified above: `BINARY_SUBSCR` **no longer exists** (`a[b]` is now `BINARY_OP 26 ([])`,
and `BINARY_OP` grew from 1 to 5 cache entries as a result); `LOAD_FAST_BORROW` and
`LOAD_FAST_BORROW_LOAD_FAST_BORROW` are new; `POP_ITER`, `NOT_TAKEN`, `BUILD_TEMPLATE` and
`BUILD_INTERPOLATION` (t-strings, PEP 750) are new.

**Instruction *sizes* change**, because cache counts change. A tool that computes offsets
from a 3.13 cache table produces valid-looking, silently wrong offsets on 3.14.

**The consistency invariants are unchecked and unforgiving.** §7 produced a real SIGSEGV
from one wrong integer. `co_code`, `co_stacksize`, `co_exceptiontable` and `co_linetable`
must all agree, and nothing validates that they do.

**And `co_code` isn't even what runs** (§5). The live array is `co_code_adaptive`, mutated
by specialization, and on free-threaded builds there is one copy per thread (`co_tlbc`).

This is why bytecode-patching libraries break every single release, and why the mature ones
maintain per-version opcode tables and simply refuse to run on an unrecognized version.
Nothing you can do prevents this; the surface is genuinely unstable by policy.

**The engineering conclusion.** For *reading*: `dis` is a first-class, stable, supported
API — use it constantly. For *writing*: prefer AST transformation (stable, documented,
covered in [`18-lexer-parser-ast.md`](18-lexer-parser-ast.md) and
[`42-runtime-code-manipulation.md`](42-runtime-code-manipulation.md)) or `sys.monitoring`
(PEP 669, [`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md)) over
bytecode rewriting. Almost everything people reach for bytecode patching to do — coverage,
profiling, tracing, instrumentation — has a supported mechanism now that did not exist when
those libraries were written.

---

## 14. Lab exercises

Reading this document leaves you at **rung 3** on the README §14 ladder — you can now say
"zero-cost exception tables" fluently and collapse on the first "why?". These labs move you
to rung 4. All are pure Python (`dis`, `opcode`, `types`, `inspect`) and run on any 3.14
build; only Lab 8 needs a source checkout.

**1 — Account for every byte of `a.b.c(d)`.** Run `dis.dis(f, show_caches=True,
show_offsets=True)` on it. Predict `len(co_code)` from the instruction count and the
`dis._inline_cache_entries` table *before* you measure. Then do the same for a function
whose body is `a[b] + c` and explain why `BINARY_OP` costs 12 bytes. *Proves you can read
modern disassembly at all — this is §2/§4 and the prerequisite for everything else.*

**2 — Force `EXTENDED_ARG` three different ways.** Get one from a long jump, one from a
function with >256 locals, and try to get one from `BUILD_TUPLE`. When the third fails,
find `_PY_STACK_USE_GUIDELINE` by bisection and explain the compiler's reasoning. Then
verify `start_offset != offset` on your `EXTENDED_ARG`-prefixed instruction. *Proves §3,
and the bisection is the point: you derived a source constant from behaviour alone.*

**3 — Watch specialization happen.** Write a function with a `for` loop over a list doing
`int` arithmetic and one attribute load. `dis(adaptive=True)` cold, run it 10,000 times,
`dis(adaptive=True)` again. Then feed it `float`s instead and disassemble a third time —
identify which instructions deoptimized and which stayed. Confirm `co_code` never changed.
*Proves §5, and is the on-ramp to [`20-eval-loop.md`](20-eval-loop.md).*

**4 — Write the exception-table decoder.** Parse `co_exceptiontable` from raw bytes into
`(start, end, target, depth, lasti)` tuples and assert equality against
`dis.Bytecode(f).exception_entries` for every function in a real stdlib module. Then find
the function in that module with the most entries and explain its shape. *Proves you
understand §8 rather than recognizing it — a decoder either matches or it doesn't.*

**5 — Measure what "zero-cost" costs.** Benchmark, with the same total iteration count:
(a) a loop with no `try`, (b) a loop with a `try` that never raises, (c) a loop where the
`try` raises and is caught every iteration, (d) the same logic using `dict.get` with a
sentinel instead of `except KeyError`. Report the four numbers. *Proves the §8 trade-off
empirically, and produces the number you need to defend or reject EAFP in a hot path.*

**6 — Break the invariants deliberately.** Use `code.replace()` to (a) set `co_stacksize=1`
on a function that needs 20 and confirm SIGSEGV, (b) strip `co_exceptiontable` from a
function with a `try` and observe what a raise does, (c) truncate `co_linetable` and see
what the traceback looks like. Run each in a subprocess and report the exit codes.
*Proves §7 and §13: these fields are not advice. Do this once and you will never trust a
bytecode-patching library again.*

**7 — Prove comprehension inlining, then break it.** Confirm from `co_consts` and
`co_varnames` that a listcomp is inlined and a genexp is not. Then write a comprehension
whose body raises, and use the exception table to explain how the shadowed variable is
restored. Finally, benchmark listcomp vs genexp vs explicit loop for a fully-consumed
result. *Proves §10, and settles a question people usually answer from folklore.*

**8 — Add an opcode.** Build CPython from source (`13-cpython-source-map.md`), add an
instruction to `Python/bytecodes.c`, regenerate with `make regen-cases`, emit it from
`Python/codegen.c`, and disassemble a function that uses it. Check whether every opcode
number after yours shifted. *This is the Tier 9 capstone from README §12 and the single
best proof that you understand this document and doc 18 together.*

---

## 15. Question bank

Staff-level. Section references are where to reread if your model doesn't produce the
answer.

1. Disassemble `a.b.c(d)` and account for every byte. Which instructions have inline caches, and how many bytes each? *(§2, §4)*
2. Why is `co_code` not a field of `PyCodeObject`, and what does reading it actually do? *(§1, §5)*
3. `co_positions()` yields 34 entries but `dis` shows 10 instructions for the same function. Explain, and say what goes wrong if you `zip()` them. *(§9)*
4. What is `EXTENDED_ARG`, and why do `Instruction.offset` and `Instruction.start_offset` differ? Which one is a jump target? *(§3)*
5. `LOAD_ATTR` with oparg 1 and oparg 0 have different stack effects. Why, and what is the name index in each case? *(§7, §12)*
6. "Zero-cost exception handling" — zero cost *when*? What got more expensive, and name a real idiom that regressed. *(§8)*
7. What replaced the block stack, how is it encoded, and why does each entry store a stack `depth`? *(§8, §7)*
8. Walk a raise at offset 84 through the exception table lookup, end to end. *(§8)*
9. `SETUP_FINALLY` is still in `opcode.opmap` on 3.14. Why can it never appear in `co_code`? *(§2, §8)*
10. Why does `LOAD_DEREF 2` not mean "the third free variable"? What is the correct lookup? *(§6, §11)*
11. Why does `MAKE_CELL` execute before `RESUME`, and what does it cost you to close over a function parameter? *(§11)*
12. A comprehension is now inlined but still needs two exception-table entries. What are they for? *(§10)*
13. Comprehensions were inlined in 3.12 but generator expressions were not. Give the structural reason. *(§10)*
14. You set `co_stacksize` too low. What happens, and why is it not an exception? *(§7)*
15. A colleague proposes a bytecode-patching library to add tracing to a large codebase. Give three specific mechanisms by which it will break, and name two supported alternatives. *(§13)*

---

## 16. Sources

**Primary — verify against these, not against this document**

- [`dis` — Disassembler for Python bytecode (3.14)](https://docs.python.org/3.14/library/dis.html) — the authoritative opcode reference *and* the authoritative statement that none of it is stable. **Verdict: read the whole page once, then keep it open. The per-opcode "Changed in version" notes are the real value.**
- [`Include/cpython/code.h`](https://github.com/python/cpython/blob/3.14/Include/cpython/code.h) — the `_PyCode_DEF` macro quoted in §1. **Verdict: primary source for every field name here. Short and readable; read it in full.**
- [`InternalDocs/exception_handling.md`](https://github.com/python/cpython/blob/3.14/InternalDocs/exception_handling.md) — the exception table format and the honest framing of what "zero-cost" means. **Verdict: the single best document on §8. Two pages. Read it before believing anything else on the topic.**
- [`InternalDocs/code_objects.md`](https://github.com/python/cpython/blob/3.14/InternalDocs/code_objects.md) and [`InternalDocs/interpreter.md`](https://github.com/python/cpython/blob/3.14/InternalDocs/interpreter.md) — source of the `co_stacksize` framing in §7. **Verdict: the devguide's internals docs are underread and better than most blog posts.**
- CPython sources by name: `Python/codegen.c` (AST → CFG, ~6,500 lines in 3.14 — this is where `compile.c` moved to), `Python/flowgraph.c` (`calculate_stackdepth`, jump resolution, superinstruction fusion), `Python/compile.c` (now only ~1,750 lines, the driver), `Python/bytecodes.c` (the DSL every instruction is generated from), `Lib/dis.py`, `Include/opcode_ids.h` (generated), `Include/internal/pycore_compile.h` (`_PY_STACK_USE_GUIDELINE`).

**PEPs — each one is a section of this document**

- [PEP 659 — Specializing Adaptive Interpreter](https://peps.python.org/pep-0659/) — why inline caches exist. **Verdict: informational, and the best-written of the four. Read before [`20-eval-loop.md`](20-eval-loop.md).**
- [PEP 626 — Precise line numbers for debugging](https://peps.python.org/pep-0626/) — `co_linetable`. **Verdict: read the Specification section; note it explicitly refuses to specify the byte format (§9).**
- [PEP 657 — Fine-grained error locations in tracebacks](https://peps.python.org/pep-0657/) — `co_positions()`. **Verdict: short. Read the Examples section; it's the best advertisement for the feature.**
- [PEP 709 — Inlined comprehensions](https://peps.python.org/pep-0709/) — source of the pre-3.12 disassembly quoted in §10. **Verdict: read the Specification and Backwards Compatibility sections — the latter is the list of tools it broke.**
- [PEP 669 — Low impact monitoring](https://peps.python.org/pep-0669/) — the supported alternative to bytecode patching (§13). Covered in [`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md).

**Tools you should have used by the end of §14**

- `dis.dis(f, show_caches=True, show_offsets=True, show_positions=True, adaptive=True)` — all four flags. Most people know none of them.
- `dis.Bytecode(f)` — the programmatic API. `.exception_entries`, `.cache_info`, `.positions`, `.start_offset`.
- `opcode.opmap`, `opcode.stack_effect`, `dis._inline_cache_entries`, `_opcode_metadata._specializations` — the last two are private and will move; that is itself the §13 lesson.
- `python -m dis file.py` from the shell, and `compile()` + `exec()` in a REPL to disassemble a string without touching disk.

**Sibling docs**

- [`18-lexer-parser-ast.md`](18-lexer-parser-ast.md) — where code objects come from, and the *stable* place to do program transformation.
- [`20-eval-loop.md`](20-eval-loop.md) — what executes all of this; §4 and §5 are its prologue.
- [`21-tier2-and-jit.md`](21-tier2-and-jit.md) — `co_executors` from §1, and what `JUMP_BACKWARD_NO_JIT` in §5 was recording.
- [`42-runtime-code-manipulation.md`](42-runtime-code-manipulation.md) — `types.CodeType`, `code.replace()`, and §13 in anger.
- [`24-the-gil.md`](24-the-gil.md) §1 — why `LOAD_FAST_BORROW` (§12) exists at all.

---

*Next: [`20-eval-loop.md`](20-eval-loop.md) — `_PyEval_EvalFrameDefault`, computed gotos,
and what the interpreter does with the `CACHE` entries in §4 that this document could only
show you sitting there, zeroed and waiting.*
