# 42 — Runtime code manipulation: decorators, exec, the import system, and knowing when to stop

> **Tier 7, doc 42.** Prerequisites: [`40-data-model-and-descriptors.md`](40-data-model-and-descriptors.md)
> (descriptors, attribute lookup — decorators and `functools` are built on them),
> [`41-metaclasses-and-class-construction.md`](41-metaclasses-and-class-construction.md)
> (class creation is the other half of metaprogramming),
> [`18-lexer-parser-ast.md`](18-lexer-parser-ast.md) (the AST is the thing we transform),
> [`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md) (code objects are
> what `compile` emits). Feeds into: [`43-testing-strategy.md`](43-testing-strategy.md)
> (pytest's assertion rewriting is an import hook), [`45-supply-chain-and-security.md`](45-supply-chain-and-security.md)
> (`eval`/`pickle`/import hooks are attack surface), [`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md)
> (PEP 669 monitoring is the sanctioned instrumentation path).
>
> **THESIS: every technique in this document lets you change what code *does* without
> changing what it *says*, and that gap is the entire cost.** A decorator, an import hook,
> an AST transform, a monkeypatch — each one makes the source on screen a lie about the
> behaviour at runtime. Sometimes that lie pays for itself a thousand times over (pytest's
> assertion rewriting, `functools.wraps`, an APM agent). Usually it costs the next reader,
> the debugger, the type checker, and the profiler more than it ever saves. The staff-level
> skill this doc teaches is **not** how to write a meta-path finder — it's the judgment to
> know that you almost never should, and the depth to do it correctly on the day you must.

> **Measurement provenance.** Every code block below was written to a file and run on the
> machine this repo lives on: **Apple M3 Pro, macOS, arm64, CPython 3.14.6** (`~/.local/bin/python3.14`,
> `Python 3.14.6 (main, Jun 11 2026)`). Output pasted inline is **real interpreter output**,
> not reconstructed. The flagship meta-path finder in §5 was executed end to end; its call
> log is copied verbatim. PEP statuses were verified against `peps.python.org` on 2026-08-02,
> not recalled — the ones I could and could not confirm are listed explicitly in §11 and §13.

## Contents

1. [Decorators are closures with a naming problem](#1-decorators-are-closures-with-a-naming-problem)
2. [`functools`: `wraps`, `lru_cache`, `singledispatch` — and the method leak](#2-functools-wraps-lru_cache-singledispatch--and-the-method-leak)
3. [`exec`/`eval`/`compile` and why there is no safe `eval`](#3-execevalcompile-and-why-there-is-no-safe-eval)
4. [The import system, in full](#4-the-import-system-in-full)
5. [Flagship: a meta-path finder that AST-instruments on import](#5-flagship-a-meta-path-finder-that-ast-instruments-on-import)
6. [AST transformation and pytest's assertion rewriting](#6-ast-transformation-and-pytests-assertion-rewriting)
7. [Bytecode manipulation is a trap (with legitimate niches)](#7-bytecode-manipulation-is-a-trap-with-legitimate-niches)
8. [Monkeypatching: mechanics, C-type walls, patch-where-used](#8-monkeypatching-mechanics-c-type-walls-patch-where-used)
9. [Deferred annotations (PEP 649/749) and what it does to introspection](#9-deferred-annotations-pep-649749-and-what-it-does-to-introspection)
10. [Runtime introspection and PEP 669 monitoring](#10-runtime-introspection-and-pep-669-monitoring)
11. [The judgment section: when to say no](#11-the-judgment-section-when-to-say-no)
12. [Lab exercises](#12-lab-exercises)
13. [Question bank](#13-question-bank)
14. [Sources](#14-sources)

---

## 1. Decorators are closures with a naming problem

A decorator is not a language feature so much as a syntax convenience: `@d` above `def f`
means `f = d(f)`. Everything interesting is in what `d` returns, and the machinery that
makes it return a *usable* function is the closure — see
[`40-data-model-and-descriptors.md`](40-data-model-and-descriptors.md) for why the returned
object still binds as a method.

### The cell object is the mechanism

A closure is not "the function remembers its enclosing variables." It is concrete: free
variables live in **cell objects**, and the function holds them in `__closure__`. Measured
*(measured)*:

```python
def make_counter(start):
    n = start
    def inc(step=1):
        nonlocal n
        n += step
        return n
    return inc

c = make_counter(10)
c.__code__.co_freevars      # ('n',)
c.__closure__               # (<cell at 0x...: int object at 0x...>,)
c.__closure__[0].cell_contents   # 10
c(5)
c.__closure__[0].cell_contents   # 15   — the cell is shared, mutable state
c.__closure__[0].cell_contents = 100   # you can WRITE it
c()                          # 101
```

Two things fall out of this that people get wrong:

- **The cell is shared, not copied.** `nonlocal n` compiles the inner function's `n` to a
  `LOAD_DEREF`/`STORE_DEREF` against the same cell the outer scope wrote. That is why the
  classic `[lambda: i for i in range(3)]` bug happens: all three lambdas close over the
  *same* cell, and all print `2`.
- **A cell can be empty.** `types.CellType()` with no argument produces a cell whose read
  raises — real output: `ValueError: Cell is empty`. This is the state of a free variable
  captured before it is assigned.

The outer function has `co_cellvars = ('n',)` (it *creates* the cell) while the inner has
`co_freevars = ('n',)` (it *consumes* it). `make_counter.__closure__` is `None` — it closes
over nothing.

### The naming problem, and why it matters to tools

The moment you wrap a function, the wrapper's identity replaces the target's. A naive
decorator:

```python
def naive(fn):
    def wrapper(*a, **kw):
        return fn(*a, **kw)
    return wrapper
```

applied to `def target(a: int, b: str = "x", *, c: float = 1.0) -> bool` produces, measured
*(measured)*:

```
name    : wrapper                       # was 'target'
qualname: naive.<locals>.wrapper
doc     : None                          # docstring gone
sig     : (*a, **kw)                    # inspect.signature now lies
```

That last line is the real damage. `inspect.signature` reports `(*a, **kw)`, so **every
tool that reads signatures is now wrong about this function**: IDE autocomplete, `help()`,
Sphinx, FastAPI/Typer/Click parameter extraction, and static type checkers that follow the
decorator. A decorator that drops the signature silently degrades the entire tooling
ecosystem around the function — and it does it invisibly, because the function still *runs*.

---

## 2. `functools`: `wraps`, `lru_cache`, `singledispatch` — and the method leak

### `functools.wraps` copies exactly six attributes and updates one

`wraps` is `update_wrapper` as a decorator. It does **not** magically fix the signature — it
copies metadata and sets `__wrapped__` so that signature-following tools can walk back to
the real function. Measured on 3.14.6 *(measured)*:

```
WRAPPER_ASSIGNMENTS: ('__module__', '__name__', '__qualname__', '__doc__',
                      '__annotate__', '__type_params__')
WRAPPER_UPDATES    : ('__dict__',)
```

Note the two newcomers versus older Python: **`__annotate__`** (the deferred-annotation
function from PEP 649 — §9) and **`__type_params__`** (PEP 695 generics). If you have ever
copied `WRAPPER_ASSIGNMENTS` into a hand-rolled `update_wrapper`, it is now stale and your
wrapper loses annotations and type parameters. Use the real thing.

`ASSIGNMENTS` are *overwritten* on the wrapper; `UPDATES` (`__dict__`) is *merged* so the
wrapper keeps attributes it added. And critically, `wraps` sets `__wrapped__ = fn`. With
`wraps`, measured *(measured)*:

```
name    : target
sig     : (a: int, b: str = 'x', *, c: float = 1.0) -> bool   # correct!
__wrapped__ is target: True
```

The signature is right because `inspect.signature` **follows `__wrapped__` by default**.
Prove the mechanism: `inspect.signature(w, follow_wrapped=False)` returns `(*a: int, **kw) -> bool`
— the wrapper's *own* signature. The code object underneath is still `wrapper`; `wraps`
changed metadata, not behaviour. This is the difference between a decorator that cooperates
with introspection and one that defeats it, and it is a single line.

### Decorators with arguments are three layers deep

`@retry(times=3)` requires `retry(times=3)` to *return* a decorator. So the structure is
factory → decorator → wrapper, three nested closures. This is where most decorator bugs
live: forgetting the middle layer, or forgetting `@wraps` on the innermost.

### Class decorators

`@d` above `class C` is `C = d(C)`. It receives the fully-built class (after the metaclass
has run — see [`41-metaclasses-and-class-construction.md`](41-metaclasses-and-class-construction.md))
and returns anything. `dataclasses.dataclass` is the canonical example: it reads
`__annotations__`, synthesizes `__init__`/`__repr__`/`__eq__` as source strings, `exec`s
them, and attaches them. A class decorator is strictly weaker than a metaclass (it can't
influence subclass creation) and strictly simpler — which is exactly why it should be your
default when you think you need a metaclass.

### `lru_cache`/`cache` internals — and the leak-by-design on methods

`functools.cache` is `lru_cache(maxsize=None)`. The wrapper is **C-accelerated**: `type(f)`
is `functools._lru_cache_wrapper` *(measured)*, not a Python closure. It keeps a dict keyed
by a hashable of the arguments; `cache_info()` exposes `hits/misses/maxsize/currsize` and
`cache_parameters()` returns `{'maxsize': ..., 'typed': ...}`. Arguments must be hashable —
`f([1])` raises `TypeError: unhashable type: 'list'` *(measured)*.

Now the trap. **`lru_cache` on a method keys the cache on `self`, and the cache lives on the
function object, which lives on the class, which lives forever.** So the cache holds a strong
reference to every instance it was ever called on. Measured *(measured)*:

```python
class Big:
    def __init__(self, n): self.payload = bytearray(n)
    @functools.lru_cache(maxsize=None)
    def compute(self, k): return k * 2

b = Big(10_000_000)          # 10 MB instance
r = weakref.ref(b)
b.compute(1); del b; gc.collect()
r() is not None              # True  — b is STILL ALIVE, pinned by the class-level cache
Big.compute.cache_clear(); gc.collect()
r() is not None              # False — only clearing the cache frees it
```

This is not a bug in `lru_cache`; it is a consequence of where the cache is stored. The
10 MB instance survives its own `del` because a cache attached to the class holds it. In a
long-running service, `@lru_cache` on a method is an unbounded memory leak wearing a
performance-optimization costume. The correct tools:

- **`functools.cached_property`** for per-instance memoization — it stores the result in the
  instance `__dict__`, so it dies with the instance. Measured: after `del o; gc.collect()`
  the weakref is dead — **no leak** *(measured)*. It needs `__set_name__` to learn its
  attribute name (see [`40-data-model-and-descriptors.md`](40-data-model-and-descriptors.md)).
- A per-instance cache, or a `WeakValueDictionary`, if you genuinely need method-level
  memoization with lifetime tied to the instance.

### `singledispatch`

`functools.singledispatch` turns a function into a type-dispatched generic. Registration is
by annotation or explicit type; dispatch walks the **MRO** and respects ABC registration.
Measured *(measured)*:

```python
@functools.singledispatch
def render(obj): return f"<generic {obj!r}>"
@render.register
def _(obj: int): return f"<int {obj}>"
@render.register(numbers.Number)
def _(obj): return f"<number {obj}>"

render(3)      # '<int 3>'
render(3.5)    # '<number 3.5>'   — float is a numbers.Number
render(True)   # '<int True>'     — bool's MRO hits int first
render.dispatch(bool)   # the int implementation
```

`render.registry` exposes the mapping; there is an internal dispatch cache keyed on type,
invalidated when the ABC registry changes. It is the clean, introspectable answer to the
`if isinstance(...): elif isinstance(...):` ladder — and unlike a chain of `isinstance`, it
is *extensible from outside* the original module, which is either its best feature or a
maintenance hazard depending on your codebase.

---

## 3. `exec`/`eval`/`compile` and why there is no safe `eval`

### Three compile modes

`compile(src, filename, mode)` has three modes, and they differ in what the top-level code
object does with the result. Measured first opcodes *(measured)*:

| mode | accepts | last/telling op | meaning |
|---|---|---|---|
| `"eval"` | a single expression | `RETURN_VALUE` | evaluates to a value; `eval` returns it |
| `"exec"` | a sequence of statements | `STORE_NAME`, ends implicit `return None` | runs statements for effect |
| `"single"` | one interactive statement | `CALL_INTRINSIC_1` (displayhook) | like the REPL: prints the expression's value |

`"single"` is why the REPL echoes `1 + 2` but a script doesn't — it wraps bare expressions
in a `sys.displayhook` call. You almost never compile in `"single"` mode yourself.

### globals/locals semantics, and the surprises

`exec(code, globals, locals)` and `eval` take up to two namespace mappings. If you pass
neither, they use the caller's. Pass only `globals` and it doubles as `locals`. Three
behaviours that trip people:

**1. `exec` cannot create local variables in a function.** Measured *(measured)*:

```python
def f():
    exec("z = 99")
    print(z)      # NameError: name 'z' is not defined
```

Function-local name resolution is compiled to array slots (`LOAD_FAST`) at *compile* time,
before `exec` ever runs. `exec` writes into a dict (the function's `locals()` snapshot),
which the compiled body never consults. There is no slot for `z`, so the read fails. This is
not a rule you can work around — it is a consequence of how fast locals work
([`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md)). To get a value out,
pass an explicit dict: `ns = {}; exec("z = 99", ns); ns['z']` → `99` *(measured)*.

**2. Class bodies and comprehensions have surprising scope.** A comprehension runs in its
**own function scope**, so it cannot see class-body variables. Measured *(measured)*:

```python
class D:
    rows = [1, 2, 3]
    n = 10
    bad = [r for r in rows]        # OK: the *iterable* rows is evaluated in class scope
    worse = [r + n for r in rows]  # NameError: name 'n' is not defined
```

The outermost iterable (`rows`) is evaluated in the enclosing (class) scope, but the
comprehension *body* runs in a nested function where the class namespace is invisible. This
is a genuine, regularly-hit gotcha in `__init_subclass__`-heavy and enum-heavy code.

**3. `exec`/`eval` add `__builtins__` to globals if you don't.** Passing `{}` as globals
does not give you an empty environment — the interpreter inserts `__builtins__` on entry
*(measured)*: `'__builtins__' in ns` is `True` after `exec("z=99", ns)`.

### There is no safe `eval` of untrusted input

This is the security lesson, and it is absolute: **you cannot sandbox `eval`/`exec` in pure
Python.** The usual naive attempt is `eval(untrusted, {"__builtins__": {}})` — remove the
builtins, surely nothing dangerous remains. It does not work, and here is the proof, run on
this interpreter *(measured)*:

```python
env = {"__builtins__": {}}
subs = eval("().__class__.__base__.__subclasses__()", env)   # 171 classes reachable
# From any class, reach a function whose __globals__ still carry real builtins:
for c in subs:
    fn = c.__dict__.get("__init__")
    g = getattr(fn, "__globals__", None)
    if isinstance(g, dict) and "__builtins__" in g:
        bi = g["__builtins__"]
        imp = bi["__import__"] if isinstance(bi, dict) else bi.__import__
        break
os = imp("os")
os.getuid()     # 501  — arbitrary code execution from eval with __builtins__={}
```

The gadget that worked on this build was `_WeakValueDictionary.__init__` — its
`__globals__` (the `weakref` module's globals) still contained a live `__import__`. The
attack is structural, not incidental: **every object graph reachable from `object` leads,
within a few hops, to a function object, and a function object carries its defining module's
globals, which carry builtins.** You cannot prune this without breaking Python itself. The
subclass list changes between versions and imports (171 in a bare interpreter here, more once
you import libraries), but *a* path always exists.

The correct posture: **never `eval`/`exec` untrusted input.** For expressions over data, use
`ast.literal_eval` (parses literals only, no calls, no attribute access). For real sandboxing,
the boundary must be the OS or a separate interpreter with dropped privileges — a subprocess
with seccomp/landlock, a container, a WASM runtime — not a dictionary. See
[`45-supply-chain-and-security.md`](45-supply-chain-and-security.md).

---

## 4. The import system, in full

This is the largest section on purpose: almost everyone is vague about the exact path from
`import x` to a module object, and that vagueness is what makes import hooks feel like black
magic. They are not. The whole thing is three lists on `sys` and two protocols.

### The three registries

| `sys` attribute | what it is |
|---|---|
| `sys.meta_path` | ordered list of **meta-path finders**, tried first for *every* import |
| `sys.path_hooks` | callables that turn a `sys.path` entry (a string) into a **path entry finder** |
| `sys.path_importer_cache` | memoized `{path_entry: finder}` so hooks run once per directory |
| `sys.modules` | the import **cache**: `{fullname: module}` |

Measured defaults on 3.14.6 *(measured)*:

```
sys.meta_path : ['BuiltinImporter', 'FrozenImporter', 'PathFinder']
sys.path_hooks: [zipimport.zipimporter, FileFinder.path_hook]
```

### The full flow: `import x` → module object

This is the diagram everyone is fuzzy about. Follow it once top to bottom; it is the whole
system.

```
  import foo.bar
        │
        ▼
  ┌───────────────────────────────────────────────────────────────────────┐
  │ 1. sys.modules['foo.bar'] present?  ──yes──▶ bind it, DONE.            │
  │    (this is why re-import is free, and why circular imports see a      │
  │     half-built module — §"circular imports" below)                    │
  └───────────────────────────────┬───────────────────────────────────────┘
                                  │ no
                                  ▼
  ┌───────────────────────────────────────────────────────────────────────┐
  │ 2. import parent 'foo' first (recursively), then ask each finder in    │
  │    sys.meta_path IN ORDER:                                             │
  │        spec = finder.find_spec('foo.bar', foo.__path__, target)       │
  │    First non-None spec wins. None from all → ModuleNotFoundError.     │
  └───────────────────────────────┬───────────────────────────────────────┘
                                  │
             ┌────────────────────┴─────────────────────┐
             ▼                                            ▼
   BuiltinImporter / FrozenImporter              PathFinder  (the interesting one)
   (C-level, for sys.builtin_module_names)             │
                                                        ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │ 3. PathFinder walks foo.__path__ (or sys.path at top level). For each   │
   │    entry string, it consults sys.path_importer_cache; on a miss it      │
   │    runs each hook in sys.path_hooks until one accepts the entry,        │
   │    producing a PATH ENTRY FINDER (usually a FileFinder) it then caches. │
   └───────────────────────────────┬────────────────────────────────────────┘
                                   ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │ 4. FileFinder.find_spec looks for, in order: a package dir 'bar/' with  │
   │    __init__.py; then bar.py / bar.so via its loaders; else it may       │
   │    record a namespace-package PORTION (PEP 420) and keep looking.       │
   │    Returns a ModuleSpec (or None).                                      │
   └───────────────────────────────┬────────────────────────────────────────┘
                                   ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │ 5. A ModuleSpec (PEP 451) carries: name, loader, origin, cached,        │
   │    submodule_search_locations, parent, has_location. importlib then:    │
   │        module = loader.create_module(spec)  (or a default module)       │
   │        sys.modules[name] = module      ◀── inserted BEFORE exec!         │
   │        loader.exec_module(module)       (runs the body into its __dict__)│
   │    On exec failure, the name is REMOVED from sys.modules again.          │
   └───────────────────────────────┬────────────────────────────────────────┘
                                   ▼
                        bind 'foo' in the current namespace, DONE.
```

The two protocols are tiny:

- **`MetaPathFinder`**: implement `find_spec(fullname, path, target=None) -> ModuleSpec | None`.
- **`Loader`**: implement `create_module(spec) -> module | None` (return `None` for the
  default) and `exec_module(module) -> None` (run the body).

`ModuleSpec` (PEP 451, `Final`, verified) is the decoupling that makes this clean: **finding**
a module (produce a spec) is separate from **loading** it (execute via the spec's loader). Before
PEP 451 the finder returned a loader directly and metadata was set implicitly; the spec made
`create_module`/`exec_module` the two-phase standard and is why `importlib.util.module_from_spec`
exists.

### `sys.modules` is a cache *and* the reason circular imports fail

The insertion at step 5 happens **before** `exec_module` runs. That ordering is deliberate —
it's what lets a module import itself recursively without infinite regress — and it is exactly
why circular imports fail the way they do. Measured *(measured)*:

```python
# mod_a.py:  import mod_b; X = "a-defined"
# mod_b.py:  import mod_a; Y = mod_a.X
import mod_a
# AttributeError: partially initialized module 'mod_a' ...
#   has no attribute 'X' (most likely due to a circular import)
```

Walk it: `import mod_a` inserts a half-built `mod_a` into `sys.modules`, starts executing its
body, hits `import mod_b`, which inserts and runs `mod_b`, which does `import mod_a` — finds
the **half-built** `mod_a` already in `sys.modules` (step 1 hits!), and reads `mod_a.X` — but
`X` hasn't been assigned yet, because `mod_a`'s body is still paused at its first line. Hence
`AttributeError: partially initialized module`. The fix is never "break the cycle magically";
it's to move the `from x import name` to a function-local import, or restructure so the
cross-reference happens after both modules finish their top level. `from mod_a import X` fails
*harder* than `import mod_a` here because it binds the name at import time; `import mod_a` +
late `mod_a.X` access can survive if the access is deferred.

### `__init__.py` vs namespace packages (PEP 420)

A directory with `__init__.py` is a **regular package**: `__file__` points at the
`__init__.py`, `__path__` is an ordinary `list`. A directory *without* `__init__.py` can still
be a **namespace package** (PEP 420, verified `Final`), assembled from multiple `sys.path`
roots. Measured *(measured)*, same package name split across two roots:

```
nsp.__file__      : None                       # no single file backs it
nsp.__path__ type : _NamespacePath             # not a plain list
nsp.__path__      : ['.../root1/nsp', '.../root2/nsp']   # merged!
from nsp import one, two   →  1 2               # one from each root
```

The namespace `__path__` is a live `_NamespacePath` object that re-scans `sys.path` so the
package can grow at runtime. This is how implicit-namespace plugins work (e.g.
`google.cloud.*`, many `*-plugins` ecosystems). The cost: no `__init__.py` means no package
initialization hook and slightly slower resolution. Prefer regular packages unless you are
deliberately building a split-distribution namespace.

### The `.pyc` format and invalidation (PEP 552)

A `.pyc` is a 16-byte header + a marshalled code object. The header is `magic(4) + flags(4) +
word2(4) + word3(4)`. Measured on 3.14.6 *(measured)*:

```
MAGIC_NUMBER: 2b0e0d0a        # changes EVERY release — this is why .pyc is version-locked
timestamp      : flags=0b00 kind=timestamp   check_source=False words=(mtime, source_size)
checked-hash   : flags=0b11 kind=hash-based  check_source=True  words=(hash_lo, hash_hi)
unchecked-hash : flags=0b01 kind=hash-based  check_source=False words=(hash_lo, hash_hi)
```

Two invalidation strategies (PEP 552, verified `Final`, Python 3.7):

- **Timestamp-based (default, `flags` bit 0 = 0):** words 2–3 are the source mtime and size.
  On import, if either differs, recompile. Simple, but **not reproducible** — the same source
  yields different `.pyc` bytes on different machines (different mtimes), which breaks
  content-addressed build caches and reproducible builds.
- **Hash-based (bit 0 = 1):** words 2–3 are a 64-bit SipHash of the source. If bit 1
  (`check_source`) is set (**checked**), the interpreter re-hashes the source on import and
  recompiles on mismatch — deterministic *and* correct. If unset (**unchecked**), the
  interpreter trusts the `.pyc` blindly and never reads the source — the fastest startup, at
  the cost of "you must regenerate `.pyc` yourself when source changes" (build-system
  territory). The `--check-hash-based-pycs={default,always,never}` flag overrides the
  `check_source` bit at runtime.

The magic number changing every release is the whole reason you can't ship `.pyc` across
versions, and why bytecode manipulation (§7) is a trap.

### The status of lazy imports — verified, not recalled

- **PEP 690 (global "Lazy Imports") was rejected.** Verified against `peps.python.org/pep-0690`:
  `Status: Rejected`, `Python-Version: 3.12`, created 2022. Its design made *all* imports
  lazy by making the module `__dict__` itself resolve lazy placeholders on lookup — the
  Steering Council rejected it over unpredictability and the "action at a distance" of a
  global mode changing every library's import semantics.
- **PEP 810 (Explicit Lazy Imports) is its successor and it landed.** Verified against
  `peps.python.org/pep-0810`: `Status: Final`, `Type: Standards Track`,
  **`Python-Version: 3.15`**, `Resolution: 03-Nov-2025` (authors incl. Pablo Galindo,
  Germán Méndez Bravo, Thomas Wouters, Dino Viehland). It takes the opposite tack: imports
  are eager unless you write the soft keyword `lazy`:

  ```python
  lazy import json          # bound to a lazy proxy; not loaded yet
  print('json' in sys.modules)   # False
  json.dumps({})            # first use loads json AND reifies the name
  print('json' in sys.modules)   # True
  ```

  Design points that matter for this doc: the `lazy` keyword is **only allowed at module
  level** — a `SyntaxError` inside functions, class bodies, `try` blocks, and `import *`, and
  never on `from __future__ import`. It uses lightweight **proxy objects** rather than hooking
  dict lookups (690's controversial mechanism). Laziness is **local and non-recursive** — a
  lazy import does not cascade into the imported module's own imports. Error/side-effect timing
  shifts to first use. A global override exists for fleet operators (`-X lazy_imports=<mode>`,
  `PYTHON_LAZY_IMPORTS`, `sys.set_lazy_imports()`, modes `normal`/`all`/`none`) but is
  explicitly documented as an advanced feature libraries should not touch. **As of 3.14.6 (this
  interpreter) the `lazy` keyword is not available** — it targets 3.15. I did not run it; I
  verified its specification from the PEP.

Until 3.15, the deployed way to get lazy imports is a third-party shim (`importlib`-based
proxies) or the manual `importlib.util.LazyLoader`, which wraps a loader so `exec_module` is
deferred until first attribute access on the module.

---

## 5. Flagship: a meta-path finder that AST-instruments on import

Everything in §4 and §6 combines here. The goal: **transparently instrument every function
in a target package so that calling it logs its qualified name — without editing a single
line of the target's source.** This is the shape of a coverage tool, an APM auto-instrumenter,
or a test-time tracer. It was written to a temp dir and **run end to end on 3.14.6**; the
output below is verbatim.

The finder (abridged; full source ran successfully):

```python
class _TraceInjector(ast.NodeTransformer):
    """Insert `__trace__('mod.func')` as the first statement of every function."""
    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        qual = f"{self.modname}.{'.'.join(self.stack)}"
        probe = ast.Expr(ast.Call(func=ast.Name(id="__trace__", ctx=ast.Load()),
                                  args=[ast.Constant(value=qual)], keywords=[]))
        insert_at = 1 if _has_docstring(node) else 0   # keep docstrings first
        node.body.insert(insert_at, probe)
        self.generic_visit(node)                        # recurse into nested defs
        self.stack.pop()
        return node

class InstrumentingLoader(importlib.abc.Loader):
    def create_module(self, spec): return None          # default module object
    def exec_module(self, module):
        source = open(self.path).read()
        tree = _TraceInjector(self.fullname).visit(ast.parse(source, self.path))
        ast.fix_missing_locations(tree)                 # transformer left nodes location-less
        module.__dict__["__trace__"] = CALL_LOG.append  # inject the probe target
        exec(compile(tree, self.path, "exec"), module.__dict__)

class InstrumentingFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != self.prefix and not fullname.startswith(self.prefix + "."):
            return None                                 # not ours → defer to next finder
        real = importlib.machinery.PathFinder.find_spec(fullname, path)   # WHERE is it?
        if real is None or not (real.origin or "").endswith(".py"):
            return None                                 # only pure-Python source
        loader = InstrumentingLoader(fullname, real.origin)
        spec = importlib.util.spec_from_loader(fullname, loader, origin=real.origin)
        spec.submodule_search_locations = real.submodule_search_locations  # keep pkg __path__
        return spec
```

Real run output *(executed)*:

```
meta_path[0] is our finder: True
area(3, 4)      = 12
perimeter(3, 4) = 14
call log        = ['pkg_under_test.geometry.area', 'pkg_under_test.geometry._mul', 'pkg_under_test.geometry.perimeter']
area.__doc__    = docstring stays first
cached in sys.modules: True
module loader   = InstrumentingLoader
```

The target module (`geometry.area`, `_mul`, `perimeter`) has **no idea** it was rewritten:
`from pkg_under_test import geometry` is an ordinary import statement. Every function logged
its own name on call, the docstring survived (the transformer inserts the probe *after* a
leading string literal), and `sys.modules` cached the instrumented module so re-import is free.

Three lessons the *build* of this taught, in order:

1. **You must delegate to find the file.** The finder itself doesn't know where `geometry.py`
   lives — it asks the stock `PathFinder.find_spec` for the real origin, then substitutes only
   the *loader*. Reimplementing path resolution would be a second bug farm.
2. **`spec_from_loader` drops package-ness.** The first run failed with
   `ImportError: cannot import name 'geometry' from 'pkg_under_test' (unknown location)` —
   because a package needs `submodule_search_locations` (its `__path__`) for submodules to
   resolve, and `spec_from_loader` doesn't set it. Copying it from the real spec fixed it.
   This is the exact class of subtle breakage that makes import hooks expensive to own.
3. **`fix_missing_locations` is mandatory.** The transformer produced nodes with no
   `lineno`; `compile` raised `TypeError: required field "lineno" missing from stmt` until it
   ran (proven in §6). Every AST transform pays this tax.

A finder like this is genuinely useful — and it is also a permanent tax on everyone who ever
debugs `pkg_under_test`, because the code they read is not the code that runs. §11 is about
when that trade is worth making. (Cross-ref: the roadmap's Tier 9 capstone "import hook + AST
transform that instruments a codebase without touching source" is exactly this.)

---

## 6. AST transformation and pytest's assertion rewriting

### The `ast` toolkit

`ast.parse(src) -> Module`, walk/edit with `NodeVisitor` (read-only) or `NodeTransformer`
(rewrite), then `ast.fix_missing_locations`, then `compile(tree, filename, mode)`. `ast.unparse`
turns a tree back into source — invaluable for debugging a transform and for error messages.
A minimal but real assert-rewriter, run on 3.14.6 *(measured)*:

```python
class AssertRewriter(ast.NodeTransformer):
    def visit_Assert(self, node):
        # assert a == b   →   if not (a == b): raise AssertionError(f"...{a!r}...{b!r}...")
        left, right = node.test.left, node.test.comparators[0]
        msg = ast.JoinedStr(values=[
            ast.Constant(f"assert {ast.unparse(node.test)}\n  left  = "),
            ast.FormattedValue(value=left, conversion=114),   # 114 == ord('r'), i.e. !r
            ast.Constant("\n  right = "),
            ast.FormattedValue(value=right, conversion=114)])
        new = ast.If(test=ast.UnaryOp(ast.Not(), node.test),
                     body=[ast.Raise(ast.Call(ast.Name("AssertionError", ast.Load()),
                                              [msg], []))], orelse=[])
        return ast.copy_location(new, node)
```

Feeding it `assert a == b` and calling `check(2, 3)` produces, at runtime *(measured)*:

```
assert a == b
  left  = 2
  right = 3
```

That is the entire idea behind pytest's most beloved feature, in 12 lines.

### How pytest's assertion rewriting actually works

pytest installs an **import hook** — `_pytest.assertion.rewrite.AssertionRewritingHook`, a
meta-path finder inserted at `sys.meta_path[0]` (exactly the §5 mechanism). When it loads a
test module (or a plugin), it:

1. Parses the source to an AST.
2. Walks every `assert` statement and **rewrites it** into code that captures the
   subexpressions' runtime values and, on failure, builds pytest's rich multi-line explanation
   (the `assert 2 == 3` / `where 2 = f(...)` output). It does far more than the toy above:
   it introspects comparisons, `in`, boolean ops, and calls, binding each intermediate to a
   temporary so the failure report can show every operand.
3. Compiles the rewritten AST and **caches it as a `.pyc`** in `__pycache__` with its own
   header tag, so the (nontrivial) rewrite cost is paid once per source change, not per run —
   it reuses the PEP 552 machinery from §4.

This is the canonical production example of AST transformation because the value is enormous
(every assertion in the Python-testing world gets readable failures for free) and the
maintenance cost is real and *visible*: the rewriter is one of pytest's most intricate
modules, it must track every AST change across Python versions (new node types, the
`ast.Constant` unification, positional-only args, match statements), and it is a frequent
source of "works differently under `-p no:cacheprovider`" and "my plugin wasn't rewritten
because it was imported too early" bug reports. That is the trade AST transformation always
offers: **enormous leverage, paid for in a permanent coupling to the AST shape of the Python
version you run on.**

---

## 7. Bytecode manipulation is a trap (with legitimate niches)

Everything above operates on source or AST — representations Python *promises* to keep stable.
Bytecode is the opposite: **`co_code` is an implementation detail that changes every release,
sometimes every point release.** [`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md)
catalogs how much has moved — `EXTENDED_ARG`, the exception table replacing the block stack,
the adaptive/specializing opcodes, `RESUME`, comprehension inlining. Any tool that reaches into
`co_code` is betting against the interpreter, and the interpreter always wins on the next
upgrade.

`types.CodeType` is nominally mutable via `.replace()`, and it works *(measured)*:

```python
newcode = orig.__code__.replace(co_name="renamed")
newfn = types.FunctionType(newcode, orig.__globals__, "renamed")
newfn(2, 3)   # 5, and newfn.__code__.co_name == 'renamed'
```

`.replace()` is the *supported* surface — swap a name, a filename, a flags field, a constants
tuple. But `co_code` itself is opaque bytes (18 bytes for `return a + b` here), and to edit it
correctly you must also keep `co_consts`, `co_names`, the line table (`co_linetable`,
`co_positions`), the exception table (`co_exceptiontable`), and stack-depth metadata all
mutually consistent — any drift is a segfault or silent corruption, not an exception.

**When it is nonetheless the right tool** — a short, honest list:

- **Coverage tools** (`coverage.py`) historically inserted trace callbacks; modern ones now
  prefer PEP 669 `sys.monitoring` (§10) precisely to get *off* bytecode.
- **Debuggers** setting breakpoints, and **APM agents** (Datadog, New Relic, Sentry profilers)
  that must instrument code they cannot recompile from source.
- Libraries like `bytecode`/`Cython`-adjacent tooling that maintain per-version opcode maps.

Every one of these ships a per-Python-version compatibility layer and treats each CPython
release as a porting event. If you are not prepared to do that — and to have your tool break on
`3.15.0rc1` — do not manipulate bytecode. Operate on the AST (§6), which is stable, or on
runtime objects (§8), which are stable. Bytecode is the layer with the worst
effort-to-durability ratio in the whole stack.

---

## 8. Monkeypatching: mechanics, C-type walls, patch-where-used

Monkeypatching is reassigning an attribute on a module, class, or instance at runtime. The
mechanism is nothing more than `setattr` — which is exactly why it's so tempting and so
corrosive.

### It works on pure-Python classes

```python
class Service:
    def price(self): return 100
Service.price = lambda self: 999
Service().price()      # 999   (measured)
```

The class `__dict__` is a writable mapping; you replaced an entry. Every instance sees it
immediately because attribute lookup goes through the class
([`40-data-model-and-descriptors.md`](40-data-model-and-descriptors.md)).

### It fails on C types — by design

```python
int.bit_length = lambda self: None
# TypeError: cannot set 'bit_length' attribute of immutable type 'int'   (measured)
str.upper  = ...   # same TypeError
list.append = ...  # same TypeError
```

Static (C-defined) types have no writable `tp_dict` for Python code — their method tables are
fixed C structures ([`14-pyobject-and-types.md`](14-pyobject-and-types.md)), and CPython
refuses the write to protect every other user of `int` in the process. This is the wall the
`forbiddenfruit` package famously circumvents with C-level hacks, and the fact that it *needs*
C-level hacks is the point: the interpreter is telling you no. (Heap types created with `type()`
*are* patchable; the wall is specifically for built-in/extension types.)

### `unittest.mock.patch`: patch where it's *used*, not where it's defined

This is the single most common mocking bug, and the mechanism explains it exactly. If module
`app` does `from time import time`, then `app` has its **own** name `time` bound to the
function object at import time. Patching `time.time` does nothing to `app.time`. Measured
*(measured)*:

```python
# app.py:  from time import time
#          def now(): return time()
with mock.patch("time.time", return_value=1.0):
    app.now()      # 1785615672.43...   — UNAFFECTED: app.time is a separate binding
with mock.patch("app.time", return_value=42.0):
    app.now()      # 42.0               — correct: patch the name the code actually reads
```

`from x import y` copies the reference; it does not create a live link. So you must patch the
attribute **on the namespace that performs the lookup** — `app.time`, not `time.time`. `mock.patch`
does this by `setattr`-ing the target and restoring the original on exit; it is monkeypatching
with a guaranteed teardown, which is the only reason it's acceptable — the mutation is scoped
to the `with` block.

### The maintenance argument

A monkeypatch is a change to a module or class that lives in a *different* file from that module
or class. The next reader of `Service.price` sees `return 100` and has no way to know some
imported plugin rewrote it to `return 999`. Monkeypatching defeats grep, defeats "go to
definition," defeats code review of the patched file, and creates order-dependent behaviour
(whoever imports last wins). In test code with `mock.patch`'s scoped teardown it is a
controlled, temporary lie. In production code it is a permanent one, and it should survive
review only when patching a third-party library you cannot fork and the alternative is worse.

---

## 9. Deferred annotations (PEP 649/749) and what it does to introspection

**Verified on this interpreter:** PEP 649 (deferred evaluation of annotations) and its
implementation PEP 749 landed in **3.14** and the `annotationlib` module is present and
importable on 3.14.6. Both PEPs are `Final` (PEP 749 confirmed `Final`, per `peps.python.org`).
This changes introspection meaningfully and every decorator author needs to know it.

Since 3.14, annotations are **not evaluated at definition time**. Instead the compiler emits an
`__annotate__` function per object; `__annotations__` is computed lazily on first access by
calling it. This means annotations can reference names that don't exist yet (forward refs)
without `from __future__ import annotations` and without quotes-everywhere. The new
`annotationlib.get_annotations` takes a `Format`:

- `Format.VALUE` — evaluate to real objects (raises if a name is missing).
- `Format.FORWARDREF` — evaluate what you can, return `ForwardRef` objects for what you can't.
- `Format.STRING` — return the source text of each annotation, never evaluating.

Measured *(measured)* on a class with a genuine forward reference and a function annotated with
names that **do not exist**:

```python
def f(x: DoesNotExist) -> AlsoMissing: ...
get_annotations(f, format=Format.VALUE)      # NameError: name 'DoesNotExist' is not defined
get_annotations(f, format=Format.FORWARDREF) # {'x': ForwardRef('DoesNotExist', owner=<function f>), ...}
get_annotations(f, format=Format.STRING)     # {'x': 'DoesNotExist', 'return': 'AlsoMissing'}
```

The function *defined fine* — `f.__annotate__ is not None` — even though its annotations name
undefined types. Under pre-3.14 semantics that `def` would have raised `NameError` at
definition time (without the `__future__` import). This is a real behavioural change that can
un-break code that used to need stringized annotations.

**The consequence for decorators:** annotations now live behind `__annotate__`, which is in
`functools.WRAPPER_ASSIGNMENTS` (§2). A wrapper *without* `@wraps` loses annotations entirely —
measured, a bare wrapper reports `get_annotations(...)` → `{}`, while a `@wraps` wrapper
correctly reports `{'a': int, 'return': str}` *(measured)*. So the cost of a naive decorator is
now *also* an annotations cost, not just a signature cost, and the fix is the same one line.

**`get_type_hints` still works** and still resolves forward refs when the names exist
(`typing.get_type_hints(Ok)` → `{'a': int, 'b': <class 'Ok'>}` *(measured)*), but it forces
`Format.VALUE` evaluation, so it can raise on genuinely-missing names where `annotationlib`'s
`FORWARDREF`/`STRING` formats would not. Tools that walk annotations for documentation or
serialization should prefer `annotationlib` with `FORWARDREF` to survive partially-defined
modules. See [`37-generics-and-protocols.md`](37-generics-and-protocols.md) and
[`38-type-checking-in-practice.md`](38-type-checking-in-practice.md).

---

## 10. Runtime introspection and PEP 669 monitoring

### The introspection surface

- `inspect` — `signature`, `getsource`, `getmembers`, `getfullargspec`, `unwrap` (follows
  `__wrapped__` chains). It is the polite, stable front door and it cooperates with `wraps`.
- `func.__code__` — the code object (§7): `co_varnames`, `co_freevars`, `co_cellvars`,
  `co_consts`, `co_flags`, `co_positions()`.
- `func.__closure__` — the cells (§1).
- `func.__globals__` — the defining module's globals (the §3 escape gadget).
- `sys._getframe([depth])` — the current call stack's frame objects. **Portability caveat:**
  the leading underscore is a promise it is CPython-specific; PyPy and other implementations
  may not provide it or may make it expensive. Frame walking (`f_locals`, `f_back`) is how
  logging libraries find the caller's module, and it is fragile — `f_locals` on an optimized
  frame is a snapshot, and under the specializing interpreter
  ([`20-eval-loop.md`](20-eval-loop.md)) frame introspection can force deoptimization.

### PEP 669 `sys.monitoring` is the sanctioned instrumentation path

For instrumenting *execution* — call counts, coverage, line tracing — the modern, low-overhead
mechanism is PEP 669 monitoring (3.12+), covered in depth in
[`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md). Its defining trick is the
`DISABLE` return value: a callback can tell the interpreter "never fire this event at *this
code location* again," so a hot call site is instrumented once and then costs nothing. Measured
*(measured)*:

```python
mon = sys.monitoring
mon.use_tool_id(mon.PROFILER_ID, "demo")
def on_call(code, offset, callable_, arg0):
    events.append(callable_.__name__)
    return mon.DISABLE                       # this (code, offset) never fires again
mon.register_callback(mon.PROFILER_ID, mon.events.CALL, on_call)
mon.set_events(mon.PROFILER_ID, mon.events.CALL)
a(); a(); a()      # events: ['a', 'b', 'a', 'a']  — three distinct call SITES for a(), one for b()
```

`DISABLE` is **per `(code object, instruction offset)`**, which is why the three `a()` calls on
one line each fire once (three separate `CALL` bytecodes at different offsets) while `b()`,
called from a single site inside `a`, fires once. This is the mechanism that lets `coverage.py`
and modern profilers instrument at near-zero steady-state cost — the opposite of the old
`sys.settrace`, which fires on every line forever. **If you reach for bytecode rewriting to
instrument execution (§7), check whether `sys.monitoring` already does it first; it usually
does, and it is stable across versions where bytecode is not.**

---

## 11. The judgment section: when to say no

This is the section the whole document exists for. Every technique above works. The staff-level
question is never "can I?" — it is "what does this cost the next person to touch this code?"

### The four-reader test

Before shipping any runtime manipulation, ask what it does to each of these, because all four
are real people or tools that will pay for it:

| Reader | What metaprogramming costs it |
|---|---|
| **The next engineer** | The source no longer says what the program does. "Go to definition," grep, and code review all point at the wrong bytes. A decorator/monkeypatch/import hook is *action at a distance*. |
| **The debugger** | Stepping lands in `wrapper` frames, generated code, or `exec`'d strings with no source file. Breakpoints on the source line may never hit because that line was rewritten (§5, §6). |
| **The type checker** | A decorator that drops the signature (§1) or annotations (§9) blinds mypy/pyright to the real function. `exec`'d code is invisible to static analysis entirely. |
| **The profiler** | Time attributed to `wrapper` or to `<string>` frames instead of the real function. Bytecode-rewritten frames may not map back to source lines. |

If a technique degrades three of the four, it needs an extraordinary justification. If it
degrades all four (a bytecode-rewriting import hook that `exec`s generated strings), it needs to
be a framework other people opted into, not a line in your service.

### Concrete criteria for rejecting metaprogramming in review

Reject — or demand a written justification — when you see:

1. **A metaclass or import hook where a class decorator, `__init_subclass__`, or an explicit
   function call would do.** Reach for the weakest tool that works
   ([`41-metaclasses-and-class-construction.md`](41-metaclasses-and-class-construction.md)).
   Weakest-tool-that-works is the entire heuristic.
2. **A decorator without `functools.wraps`.** It silently breaks `inspect.signature`,
   `help()`, annotations (§9), and type checking. There is no upside to omitting it.
3. **`eval`/`exec` on anything derived from input.** No exceptions for "but it's just config" —
   §3 proved the sandbox doesn't exist. `ast.literal_eval` or a real parser instead.
4. **Monkeypatching in non-test code**, except to patch a third-party library you cannot fork,
   with a comment explaining why and a link to the upstream issue.
5. **Any reach into `co_code` / bytecode** outside a tool that explicitly owns a per-version
   opcode compatibility layer (§7). For instrumentation, `sys.monitoring` first (§10).
6. **`lru_cache` on a method** (§2) — it's a memory leak; `cached_property` or a scoped cache
   instead.
7. **An import hook to solve a problem that a plain function, a plugin registry, or a build step
   solves.** Import hooks are for frameworks (pytest, coverage, APM), not for application code.

### The one honest caveat

The techniques in this doc are not forbidden — they are *expensive*, and the whole point of
understanding them at this depth is to spend the expense deliberately. `functools.wraps` is
metaprogramming and you should use it constantly. pytest's assertion rewriting is a
bytecode-caching import hook and it is one of the best features in the ecosystem. The line is
not "never" — it is **"only when the leverage is enormous, the mechanism is understood, and the
cost to the four readers is paid down"** (with `wraps`, with `__wrapped__`, with source maps,
with `sys.monitoring` instead of bytecode). Collecting these tricks to feel clever is the trap
the roadmap's §16.5 names explicitly. Knowing when to say no is rung 5.

---

## 12. Lab exercises

Reading this leaves you at rung 3 (README §14). These use `~/.local/bin/python3.14`.

**1 — Closures and cells.** Build `make_counter`, print `__closure__[0].cell_contents`, mutate
the counter, then *write* the cell directly and confirm the function's behaviour changes.
Reproduce the `[lambda: i for i in range(3)]` late-binding bug and fix it with a default arg.
*Proves you understand that a closure is a shared mutable cell, not a value copy (§1).*

**2 — The signature-preservation drill.** Write a `@timed` decorator two ways: without and with
`functools.wraps`. For each, print `inspect.signature`, `__doc__`, and
`annotationlib.get_annotations`. Confirm the naive one breaks all three and `wraps` fixes them.
This is the README §Tier-7 checklist item verbatim. *Proves rung 4 on decorators (§2, §9).*

**3 — Break the sandbox.** Start from `eval(src, {"__builtins__": {}})` and reach `os.getuid()`
via `__subclasses__` traversal. Then try to *defend* it — remove `__subclasses__`, block dunder
access with a regex on the source — and demonstrate a bypass of each defense. *Proves §3's
central claim: there is no safe eval, and you can feel why.*

**4 — Write the meta-path finder.** Reproduce §5 from scratch: a package in a temp dir, an
`InstrumentingFinder`/`InstrumentingLoader` pair, an AST transform that injects a call probe.
Make it survive a *package* (the `submodule_search_locations` bug) and a docstring. This is the
roadmap's Tier 9 capstone. *Proves you can operate the whole import machinery, not recite it (§4, §5).*

**5 — The `lru_cache` method leak.** Put `@lru_cache` on a method of a class whose instances
hold a large `bytearray`. Take a `weakref`, `del` the instance, `gc.collect()`, and show the
weakref is still alive. Then `cache_clear()` and show it dies. Repeat with `cached_property` and
show no leak. *Proves §2 — the single most common accidental Python memory leak.*

**6 — Patch where it's used.** Write module `app` doing `from time import time`. Show that
`mock.patch("time.time")` fails to affect `app.now()` and `mock.patch("app.time")` succeeds.
Explain from the bytecode/`from-import` binding why. *Proves the §8 mechanism, the most common
mocking bug in real test suites.*

**7 — pyc archaeology.** Compile one source three ways (timestamp, checked-hash, unchecked-hash)
and parse the 16-byte headers to recover the flags and the mtime-vs-hash words. Change the
source and observe which `.pyc` variants recompile and which don't under
`--check-hash-based-pycs=never`. *Proves you understand PEP 552 invalidation (§4).*

**8 — `sys.monitoring` vs `settrace`.** Instrument function calls two ways: `sys.settrace` and
PEP 669 monitoring with `DISABLE`. Measure the steady-state overhead of each in a hot loop.
Explain why `DISABLE` makes monitoring near-free and `settrace` isn't. *Proves §10 and connects
to [`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md).*

---

## 13. Question bank

Staff-level. If you can't answer from your own model, the section to reread is noted.

1. What object holds a closure's free variable, and what happens if you read it before it's
   assigned? *(§1)*
2. Exactly which attributes does `functools.wraps` copy, and why did the list grow in 3.14?
   *(§2, §9)*
3. `inspect.signature` reports the *real* signature through a `@wraps` decorator. By what
   mechanism? *(§2)*
4. Why does `@lru_cache` on a method leak memory, and what's the fix? *(§2)*
5. Name the three `compile` modes and one program that behaves differently in `single` vs
   `exec`. *(§3)*
6. Why can't `exec("z = 1")` create a usable local inside a function? *(§3)*
7. Walk the sandbox escape from `eval(src, {"__builtins__": {}})` to arbitrary code. Why is
   there no pure-Python fix? *(§3)*
8. Trace `import foo.bar` through `sys.meta_path`, `sys.path_hooks`, the spec, and the loader.
   Where does `sys.modules` get written, and why *before* `exec_module`? *(§4)*
9. Why does a circular import raise `partially initialized module ... has no attribute`, and
   why does `from x import y` fail harder than `import x`? *(§4)*
10. Regular package vs PEP 420 namespace package: what is `__path__` in each, and when do you
    want a namespace package? *(§4)*
11. Timestamp vs hash-based `.pyc`: what's in the header, and which one do reproducible builds
    need? *(§4)*
12. What is the current status of lazy imports in CPython — which PEP, which Python version,
    eager or lazy by default, and what keyword? *(§4)*
13. Explain how pytest gives you rich assertion failures without you writing messages. Name the
    component and the caching. *(§6)*
14. Why is bytecode manipulation a worse bet than AST transformation, and name a legitimate use
    of each. *(§6, §7)*
15. Why does `int.bit_length = ...` raise but `MyClass.method = ...` doesn't? *(§8)*
16. Under PEP 649, what does `get_annotations(f, format=FORWARDREF)` return for an annotation
    naming an undefined type, and why did the `def` not raise? *(§9)*
17. What does `sys.monitoring`'s `DISABLE` return value do, and why is it near-zero-overhead
    where `settrace` isn't? *(§10)*
18. Give three concrete reasons to reject a decorator, an import hook, or a monkeypatch in code
    review even though it works. *(§11)*

---

## 14. Sources

**Primary — the authoritative import references (use these, not blog posts)**
- [The import system — Python Language Reference](https://docs.python.org/3/reference/import.html) — the single most detailed and correct account of meta_path/path_hooks/spec/loader. Read §5's finders/loaders and the ModuleSpec section in full. *Verdict: authoritative; everything in §4 traces to here.*
- [`importlib` — The implementation of import](https://docs.python.org/3/library/importlib.html) — the API surface for §4/§5: `abc.MetaPathFinder`, `abc.Loader`, `util.spec_from_loader`, `util.module_from_spec`, `machinery.PathFinder`, `util.LazyLoader`. *Verdict: authoritative reference; pair with the language ref for the *why*.*
- CPython sources: `Lib/importlib/_bootstrap.py`, `Lib/importlib/_bootstrap_external.py` (`FileFinder`, `SourceFileLoader`, the `.pyc` header code), `Python/import.c`. *Verdict: ground truth; read `_bootstrap_external.py` for the actual pyc-header bytes in §4.*

**PEPs — statuses verified against peps.python.org on 2026-08-02**
- [PEP 451 — A ModuleSpec Type for the Import System](https://peps.python.org/pep-0451/) — `Final`. The finder/loader decoupling behind §4/§5.
- [PEP 420 — Implicit Namespace Packages](https://peps.python.org/pep-0420/) — `Final`. The §4 namespace-package behaviour.
- [PEP 552 — Deterministic pycs](https://peps.python.org/pep-0552/) — `Final` (3.7). The §4 hash-based `.pyc` and `check_source` bit.
- [PEP 690 — Lazy Imports](https://peps.python.org/pep-0690/) — **`Rejected`** (targeted 3.12). The global-lazy design the Steering Council turned down.
- [PEP 810 — Explicit Lazy Imports](https://peps.python.org/pep-0810/) — **`Final`, `Python-Version: 3.15`, Resolution 03-Nov-2025.** The accepted `lazy` keyword. *Verified header fields directly; the keyword is not in 3.14.6, so §4's example is spec-derived, not run.*
- [PEP 649 — Deferred Evaluation of Annotations Using Descriptors](https://peps.python.org/pep-0649/) and [PEP 749 — Implementing PEP 649](https://peps.python.org/pep-0749/) — both `Final`, shipped in 3.14. The §9 `__annotate__`/`annotationlib` behaviour, verified live on 3.14.6.

**Library docs**
- [`functools`](https://docs.python.org/3/library/functools.html) — `wraps`, `WRAPPER_ASSIGNMENTS`, `lru_cache`, `cache`, `cached_property`, `singledispatch`. *Verdict: read the `lru_cache` note about method caching; it warns about §2's leak.*
- [`ast`](https://docs.python.org/3/library/ast.html) — `NodeTransformer`, `fix_missing_locations`, `unparse`. *Verdict: essential for §6; the "Node classes" table changes per version.*
- [`annotationlib`](https://docs.python.org/3/library/annotationlib.html) — the new module; `Format`, `get_annotations`, `ForwardRef`. *Verdict: the correct way to read annotations post-3.14.*
- [`sys.monitoring`](https://docs.python.org/3/library/sys.monitoring.html) — PEP 669 API for §10. *Verdict: prefer over `settrace` for all new instrumentation.*

**Production examples worth reading**
- pytest: `_pytest/assertion/rewrite.py` (the `AssertionRewritingHook` and `AssertionRewriter`) — the §6 canonical example. *Verdict: the best real-world AST-transform-in-an-import-hook to study; intricate on purpose.*
- `coverage.py` and py-spy/austin/Scalene — for how real tools moved from `settrace`/bytecode toward `sys.monitoring` (§7, §10).

**Sibling docs**
- [`40-data-model-and-descriptors.md`](40-data-model-and-descriptors.md) — why decorated functions still bind as methods; `cached_property`'s `__set_name__`.
- [`41-metaclasses-and-class-construction.md`](41-metaclasses-and-class-construction.md) — the weaker-tool ladder that §11 invokes.
- [`18-lexer-parser-ast.md`](18-lexer-parser-ast.md), [`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md) — what §6 and §7 manipulate.
- [`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md) — PEP 669 in depth (§10).
- [`45-supply-chain-and-security.md`](45-supply-chain-and-security.md) — §3's "no safe eval" as attack surface, plus `pickle` and import-hook risks.

---

*Next: [`43-testing-strategy.md`](43-testing-strategy.md) — where pytest's assertion-rewriting
import hook (§6) stops being a curiosity and becomes the tool you build test infrastructure on.*
