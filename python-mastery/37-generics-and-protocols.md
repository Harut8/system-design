# 37 — Generics and protocols: variance, structure, and the holes left on purpose

> **Tier 6, doc 37.** Prerequisites: [`36-type-system-foundations.md`](36-type-system-foundations.md)
> (gradual typing, nominal vs structural, assignability), [`40-data-model-and-descriptors.md`](40-data-model-and-descriptors.md)
> for §15's runtime machinery. Feeds into: [`38-type-checking-in-practice.md`](38-type-checking-in-practice.md)
> (rollout, stubs, strictness), [`39-api-and-abstraction-design.md`](39-api-and-abstraction-design.md)
> (ABC vs Protocol as a design decision), [`41-metaclasses-and-class-construction.md`](41-metaclasses-and-class-construction.md).
>
> **THESIS: Python's generics are a *specification* that four independent programs
> implement differently, over a runtime that erases almost all of it.** Three separate
> things are called "the type system" and you must keep them apart: the
> [typing spec](https://typing.python.org/en/latest/spec/), which defines assignability;
> the checkers, which approximate it and *measurably disagree*; and CPython, which stores
> your annotations and otherwise ignores them. Every hard question in this document —
> why `list[Dog]` isn't `list[Animal]`, why `runtime_checkable` lies, why a green CI run
> doesn't prevent a `TypeError` — lives in the gap between those three.

> **Tooling provenance.** Everything below ran on the machine this repo lives on:
> **Apple M3 Pro, macOS, arm64, CPython 3.14.6** (`~/.local/bin/python3.14`). Checkers:
> **mypy 2.3.0** (compiled), **pyright 1.1.411**, **ty 0.0.65** (`87de836df`, 2026-07-29),
> **pyrefly 1.2.0**. Every `reveal_type` line, every error message, and every timing in
> this document is **real output captured while writing it**. Where I could not verify a
> claim I say so in place. Checker behaviour changes fast — ty is still pre-1.0 — so
> **re-run the labs against your own pinned versions** rather than trusting this text.

## Contents

1. [What generics are for, and the one-sentence rule](#1-what-generics-are-for-and-the-one-sentence-rule)
2. [Variance from first principles](#2-variance-from-first-principles)
3. [PEP 695: new syntax, three new scopes, lazy evaluation](#3-pep-695-new-syntax-three-new-scopes-lazy-evaluation)
4. [Variance is now inferred, not declared](#4-variance-is-now-inferred-not-declared)
5. [Bounds vs constraints](#5-bounds-vs-constraints)
6. [PEP 696: type parameter defaults](#6-pep-696-type-parameter-defaults)
7. [PEP 612: ParamSpec and the decorator-signature problem](#7-pep-612-paramspec-and-the-decorator-signature-problem)
8. [PEP 646: TypeVarTuple and array shapes](#8-pep-646-typevartuple-and-array-shapes)
9. [PEP 544: Protocols and structural typing](#9-pep-544-protocols-and-structural-typing)
10. [`runtime_checkable`: the check that isn't](#10-runtime_checkable-the-check-that-isnt)
11. [`Self`, and why overloads are a last resort](#11-self-and-why-overloads-are-a-last-resort)
12. [The precision toolkit: TypedDict, Literal, Annotated, Never](#12-the-precision-toolkit-typeddict-literal-annotated-never)
13. [`@dataclass_transform`: how attrs and pydantic get checked](#13-dataclass_transform-how-attrs-and-pydantic-get-checked)
14. [PEP 649/749: annotations became lazy in 3.14](#14-pep-649749-annotations-became-lazy-in-314)
15. [Runtime machinery, and what erasure costs](#15-runtime-machinery-and-what-erasure-costs)
16. [Honest unsoundness: the gradual bargain](#16-honest-unsoundness-the-gradual-bargain)
17. [Checker behaviour, measured](#17-checker-behaviour-measured)
18. [Lab exercises](#18-lab-exercises)
19. [Question bank](#19-question-bank)
20. [Sources](#20-sources)

---

## 1. What generics are for, and the one-sentence rule

A generic is a **type-level function**. `list` is not a type; `list[int]` is. `list` is a
function from a type to a type, and `T` is its parameter.

That framing pays off immediately, because it tells you what the hard question is. For an
ordinary function you ask "what does it return?" For a type-level function you ask a
strictly harder question: **given that `Dog` is a subtype of `Animal`, what is the
relationship between `F[Dog]` and `F[Animal]`?** That relationship is called *variance*,
it is not something you get to choose freely, and getting it wrong is how a fully
type-checked program segfaults on its own logic.

The one-sentence rule, which the rest of §2 derives:

> **A type parameter may be covariant only where values of that type are *produced*,
> contravariant only where they are *consumed*, and must be invariant wherever it is
> both.** Mutability is what makes it both.

---

## 2. Variance from first principles

Forget the vocabulary. Derive it.

Take `Animal`, `Dog <: Animal`, `Cat <: Animal`. Ask whether `list[Dog]` should be usable
where `list[Animal]` is expected. Here is the proof that it must not be, in four lines:

```python
def add_a_cat(animals: list[Animal]) -> None:
    animals.append(Cat())          # perfectly legal for a list[Animal]

dogs: list[Dog] = [Dog()]
add_a_cat(dogs)                    # if this were allowed...
dogs[1].fetch()                    # ...this AttributeErrors
```

Nothing in `add_a_cat` is wrong. Nothing in `dogs[1].fetch()` is wrong. The *call* is the
error, and it is an error precisely because `list` has a method that **consumes** `T`
(`append`) and a method that **produces** `T` (`__getitem__`). The container faces both
ways, so no widening or narrowing is safe. That is invariance, and it is forced by the
shape of the API, not chosen.

Now delete the write. `Sequence` has `__getitem__` and `__len__` and no mutators. `T`
appears only in output position, so widening is safe: every `Dog` you pull out really is
an `Animal`. `Sequence[Dog] <: Sequence[Animal]` — covariant.

Now delete the read instead. A hypothetical `Sink[T]` with only `put(self, v: T) -> None`
uses `T` only in input position, so *narrowing* the parameter is what's safe: something
that accepts any `Animal` will certainly accept every `Dog`, so `Sink[Animal]` is usable
wherever `Sink[Dog]` is. Contravariant — the arrow flips.

```
                      WHERE DOES T APPEAR?

    ┌─────────────────────────────────────────────────────────────────────┐
    │  OUTPUT position only          │  INPUT position only               │
    │  (return types, read-only      │  (parameters, write-only           │
    │   properties)                  │   sinks)                           │
    │                                │                                    │
    │      Producer[Dog]             │      Sink[Animal]                  │
    │            │                   │            │                       │
    │            ▼  is-a             │            ▼  is-a                 │
    │      Producer[Animal]          │      Sink[Dog]                     │
    │                                │                                    │
    │      COVARIANT                 │      CONTRAVARIANT                 │
    │      follows the subtype arrow │      reverses it                   │
    └────────────────┬───────────────┴──────────────┬─────────────────────┘
                     │                              │
                     └──────────────┬───────────────┘
                                    ▼
                     ┌──────────────────────────────┐
                     │  BOTH positions              │
                     │  (any mutable attribute,     │
                     │   list, dict values, set)    │
                     │                              │
                     │      Box[Dog]   ✗   Box[Animal]
                     │      neither direction holds │
                     │                              │
                     │      INVARIANT               │
                     └──────────────────────────────┘
```

**Callables are the case people get backwards.** `Callable[[P], R]` is *contravariant in
`P`* and *covariant in `R`*, for exactly the reason above: a callback's parameter is a
consumption site. A function that handles any `Animal` can be used where a `Dog` handler
is wanted; the reverse crashes. All four checkers agree, verbatim:

```console
$ mypy --strict var1.py
var1.py:13: error: Argument 1 to "add_a_cat" has incompatible type "list[Dog]"; expected "list[Animal]"  [arg-type]
var1.py:13: note: "list" is invariant -- see https://mypy.readthedocs.io/en/stable/common_issues.html#variance
var1.py:13: note: Consider using "Sequence" instead, which is covariant
var1.py:26: error: Incompatible types in assignment (expression has type "Callable[[Dog], None]", variable has type "Callable[[Animal], None]")  [assignment]

$ ty check var1.py
error[invalid-argument-type]: Argument to function `add_a_cat` is incorrect
info: `list` is invariant in its type parameter
info: Consider using the covariant supertype `collections.abc.Sequence`
error[invalid-assignment]: Object of type `def handle_dog(d: Dog) -> None` is not assignable to `(Animal, /) -> None`
info: the first parameter has an incompatible type: `Animal` is not assignable to `Dog`
```

**The design consequence, and it is the most useful thing in this section:** when you
write a function that only reads a collection, annotate the parameter `Sequence`,
`Iterable`, `Mapping`, or `Collection` — never `list` or `dict`. Not for style. Because
the invariant annotation *rejects correct callers*, and the covariant one doesn't. Almost
every "mypy is being annoying about list types" complaint is really "I asked for write
permission I don't use".

This is Liskov substitution stated in types, and checkers enforce the method-override
half of it too:

```console
$ mypy --strict lsp.py
lsp.py:10: error: Argument 1 of "admit" is incompatible with supertype "Shelter"; supertype defines the argument type as "Animal"  [override]
lsp.py:10: note: This violates the Liskov substitution principle
```

Narrowing an override's *parameter* is illegal (contravariant position); narrowing its
*return* is fine (covariant position). Run that file and you get
`AttributeError: 'Animal' object has no attribute 'fetch'` — the checker was reporting a
real crash, not a rule.

---

## 3. PEP 695: new syntax, three new scopes, lazy evaluation

[PEP 695](https://peps.python.org/pep-0695/) (Python 3.12) replaced the module-level
`TypeVar` ceremony with declaration-site syntax:

```python
class Repo[T]: ...
def first[T](xs: list[T]) -> T: return xs[0]
type Alias[T] = list[T] | None
```

The syntax is the least interesting part. Three things underneath it are not.

### 3.1 It introduces a new kind of scope

Type parameter lists, `type` statement bodies, and (since 3.14, §14) annotations all run
in an **annotation scope** — a real, separate scope with its own code object.

```
   module scope
   ├── class C:                       ← class scope
   │     X = int                      ← lives in the class namespace
   │     ┌──────────────────────────────────────────────────────┐
   │     │ ANNOTATION SCOPE for  def m[T: X](...)               │
   │     │   • behaves like a function scope, EXCEPT            │
   │     │   • it CAN see the enclosing class namespace         │
   │     │     (a comprehension in the same place cannot)       │
   │     │   • evaluated LAZILY, on attribute access            │
   │     └──────────────────────────────────────────────────────┘
   │     def m[T: X](self, a: T) -> T: ...
```

That "can see the class namespace" bullet surprised me, so I checked it *(measured,
3.14.6)*:

```python
class C:
    X = int
    def m[T: X](self, a: T) -> T: return a
C.X = str
print(C.m.__type_params__[0].__bound__)     # <class 'str'>  ← re-read, live
```

The bound resolves against the class namespace, and because evaluation is lazy it reads
the *current* value — rebinding `C.X` after class creation changes the bound. `del`ing the
name inside the class body makes `__bound__` raise `NameError`. Contrast the classic
comprehension gotcha in the identical position, which still fails:

```python
class E:
    Z = [1, 2, 3]
    V = [Z for _ in range(1)]     # NameError: name 'Z' is not defined
```

The [language reference](https://docs.python.org/3/reference/executionmodel.html#annotation-scopes)
states the rule explicitly: annotation scopes "have access to their enclosing class
namespace", and comprehensions do not. Two constructs, same textual position, opposite
scoping. Worth knowing before you debug it at 2am.

### 3.2 Bounds, constraints, defaults, and alias values are lazy

```python
type Broken = DoesNotExist[int]     # defines fine
Broken.__value__                    # NameError: name 'DoesNotExist' is not defined

def f[T: Undefined1](x: T) -> T: return x   # defines fine
f.__type_params__[0].__bound__              # NameError only here
```

This is what makes mutually recursive aliases work without quotes:

```python
type Recursive = list[Recursive] | int      # fine
```

The value is evaluated once and cached: a `type Cached = print("EVALUATED") or int`
prints exactly once across two `__value__` accesses *(measured)*.

### 3.3 The runtime objects

*(measured, 3.14.6)*

| Expression | Runtime result |
|---|---|
| `Repo.__type_params__` | `(T,)` — a tuple of `typing.TypeVar` |
| `first.__type_params__` | `(T,)` — functions carry them too |
| `type(Alias)` | `typing.TypeAliasType` |
| `Alias.__value__` | `list[T] \| None`, lazily |
| `type(Repo[int])` | `typing._GenericAlias` |
| `type(Alias[int])` | **`types.GenericAlias`** — a different class |
| `Repo.__mro__` | `(Repo, typing.Generic, object)` — `Generic` is injected |
| `T.__infer_variance__` | `True` (always, for PEP 695 params) |

Note the asymmetry in row 5 vs 6: subscripting a generic *class* goes through
`typing`'s Python-level machinery; subscripting a `TypeAliasType` produces the C-level
`types.GenericAlias`. And `Repo` picked up `typing.Generic` as a base without you writing
it — the compiler did that.

A `type`-statement alias is **not** a class and cannot be used as one:

```console
$ mypy al.py
al.py:13: error: Type alias defined using "type" statement not valid as base class  [misc]
$ ty check al.py
al.py:13:11: warning[unsupported-base] Unsupported class base with type `<type alias 'Pair[int]'>`
```

(ty grades this a *warning*; the others make it an error. First of many small divergences
— §17 collects them.)

---

## 4. Variance is now inferred, not declared

This is the most consequential semantic change in PEP 695, and it is easy to miss because
it shows up as an *absence* of syntax.

Legacy `TypeVar` made you declare variance up front:

```python
T = TypeVar("T")                       # invariant
T_co = TypeVar("T_co", covariant=True) # covariant, by fiat
```

Declaring it is a promise the checker then has to police, which is why you get errors like
`Cannot use a covariant type variable as a parameter`. PEP 695 deletes the promise: the
checker **computes** variance from how the parameter is used. The PEP's algorithm builds
two specializations of the class (`upper` and `lower`), substitutes a dummy type for the
parameter under test, and checks assignability in each direction — covariant if the
subtype relation survives one way, contravariant if the other, invariant if neither.

Three identical-looking classes, three different inferred variances *(measured — all four
checkers agree)*:

```python
class Box[T]:                       # mutable attribute  -> INVARIANT
    def __init__(self, v: T) -> None: self.value = v
class ROBox[T]:                     # only returned      -> COVARIANT
    def __init__(self, v: T) -> None: self._v = v
    @property
    def value(self) -> T: return self._v
class Sink[T]:                      # only accepted      -> CONTRAVARIANT
    def put(self, v: T) -> None: ...
```

```console
$ pyright var3.py
var3.py:18:18 - error: Type "Box[Dog]" is not assignable to declared type "Box[Animal]"
      Type parameter "T@Box" is invariant, but "Dog" is not the same as "Animal"
var3.py:21:20 - error: Type "Sink[Dog]" is not assignable to declared type "Sink[Animal]"
      Type parameter "T@Sink" is contravariant, but "Dog" is not a supertype of "Animal"
```

`ROBox[Dog]` → `ROBox[Animal]` and `Sink[Animal]` → `Sink[Dog]` both pass silently. No
`covariant=` anywhere in the file.

**Where inference and declaration genuinely differ.** Take a class whose body is *used*
covariantly but *declared* with a plain `TypeVar`:

```python
class LegacyRO(Generic[T_legacy]):        # T_legacy = TypeVar("T_legacy")
    def __init__(self, v: T_legacy) -> None: self._v = v
    def get(self) -> T_legacy: return self._v

class New695RO[T]:                        # byte-for-byte identical body
    def __init__(self, v: T) -> None: self._v = v
    def get(self) -> T: return self._v
```

```console
$ mypy --strict var4.py
var4.py:20: error: Incompatible types in assignment (expression has type "LegacyRO[Dog]", variable has type "LegacyRO[Animal]")  [assignment]
```

The PEP 695 version of the same class is accepted. Same code, different assignability,
purely because of how the type parameter was spelled. **This is a real migration hazard in
both directions:** mechanically rewriting `Generic[T]` to `[T]` can silently *widen* your
public API's assignability, and rewriting a `covariant=True` protocol to PEP 695 syntax
can silently *narrow* it if the body also uses `T` in a parameter position.

If you need the legacy syntax but want inference, `TypeVar("T", infer_variance=True)` is
the bridge (PEP 695 §Auto Variance For TypeVar); PEP 695 parameters always report
`__infer_variance__ == True` at runtime *(measured)*.

> **Divergence worth flagging.** mypy 2.3.0, pyright 1.1.411 and pyrefly 1.2.0 all reject
> `class BadCov(Generic[T_cov]): def put(self, v: T_cov) -> None: ...` —
> a declared-covariant variable used contravariantly. **ty 0.0.65 does not flag it at
> all.** If you still have legacy variance declarations, ty will not police them for you
> today.

---

## 5. Bounds vs constraints

Two different restrictions with two different inference behaviours, and people reach for
the wrong one constantly.

```python
def bounded[T: Base](x: T) -> T: ...            # bound: T is ANY subtype of Base
def constrained[T: (int, str)](x: T) -> T: ...  # constraints: T is EXACTLY int or EXACTLY str
```

*(measured — mypy, pyright, ty and pyrefly all produce the same solutions)*

| Call | Solved `T` | Why |
|---|---|---|
| `bounded(Sub())` | `Sub` | bound preserves the argument's own type |
| `constrained(3)` | `int` | picks a constraint |
| `constrained(MyInt(3))` | **`int`** | **widened to the constraint — `MyInt` is lost** |
| `pair_b(Base(), Sub())` | `Base` | bound solves to the join |
| `pair_c(3, "a")` | *error* | no single constraint fits both |

Row 3 is the one that bites. `MyInt(3)` is a subclass of `int`, but a constrained TypeVar
solves to the constraint itself, so `constrained(MyInt(3))` returns `int` — you lost the
subtype. A bound would have preserved it. **Use a bound unless you genuinely need the
"exactly one of these, checked separately" semantics.**

That "checked separately" is the other half. The body of a constrained generic is
type-checked **once per constraint**:

```python
def upper_it[T: (int, str)](x: T) -> T:
    return x.upper()
```

```console
$ mypy --strict bc2.py
bc2.py:2: error: "int" has no attribute "upper"  [attr-defined]
$ pyrefly check -p default bc2.py
ERROR bc2.py:2:12-19: Object of class `int` has no attribute `upper` [missing-attribute]
```

That is the feature: constraints let one implementation body be validated against each
alternative independently, which is exactly what `AnyStr` in typeshed is for. A bound
cannot do this — with `T: str`, `x.upper()` returns `str`, not `T`, and every checker
correctly rejects `return x.upper()` as a return-type error.

> **Divergence.** `pair_c(3, "a")` — the "no single constraint matches" error — is caught
> by mypy (`Value of type variable "T" of "pair_c" cannot be "object"`), pyright, and
> pyrefly. **ty 0.0.65 reports nothing.**

---

## 6. PEP 696: type parameter defaults

[PEP 696](https://peps.python.org/pep-0696/) (Python 3.13) lets a type parameter carry a
default, so `Response` means `Response[str]` instead of `Response[Unknown]`:

```python
class Response[BodyT = str]:
    def get(self) -> BodyT: ...

def f(r: Response) -> None:  reveal_type(r.get())   # str
def g(r: Response[bytes]) -> None: reveal_type(r.get())  # bytes
```

All four checkers agree *(measured)*. The problem it solves is API evolution: you can add
a type parameter to an existing public class without breaking every unparameterised
annotation in every downstream codebase. That is the whole motivation, and it is a big
one for library authors.

**The ordering rules mirror function defaults**, and unlike most typing rules these are
enforced by the *interpreter*:

```console
$ python3.14 -c 'exec("class Bad[T = int, U]: ...")'
SyntaxError: non-default type parameter 'U' follows default type parameter

$ python3.14 -c 'D = TypeVar("D", default=str); T = TypeVar("T"); class Bad(Generic[D, T]): ...'
TypeError: Type parameter ~T without a default follows type parameter with a default
```

A `SyntaxError` at compile time for PEP 695 syntax, a `TypeError` at class-creation time
for the legacy spelling. (PEP 696 only says a `TypeError` "should ideally" be raised;
CPython 3.14.6 does raise it.)

A default may reference an **earlier** type parameter, and the reference resolves through
subscription *(measured, mypy and ty agree)*:

```python
class Pair[A, B = A]: ...
def h(p: Pair[int]) -> None:
    reveal_type(p.a)   # int
    reveal_type(p.b)   # int   <- B defaulted to A, which was solved to int
def i(p: Pair[int, str]) -> None:
    reveal_type(p.b)   # str
```

Defaults must satisfy their own bound, and every checker enforces it:

```console
$ mypy --strict def1.py
def1.py:14: error: TypeVar default must be a subtype of the bound type  [misc]
$ ty check def1.py
def1.py:14:24: error[invalid-type-variable-default] TypeVar default is not assignable to the TypeVar's upper bound
$ pyrefly check def1.py
ERROR def1.py:14:24-27: Expected default `str` of `T` to be assignable to the upper bound of `int` [invalid-type-var]
```

---

## 7. PEP 612: ParamSpec and the decorator-signature problem

Before [PEP 612](https://peps.python.org/pep-0612/), a decorator was a type-system black
hole. You could name the return type or the parameters, never both, so everyone wrote
`Callable[..., Any]` and every decorated function in the codebase silently became untyped.

That is not a small loss. It is the single largest source of accidental `Any` in typical
Python code, because decorators cluster on exactly the functions you most want checked:
route handlers, tasks, retries, caches.

```python
def timed[**P, R](f: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(f)
    def w(*a: P.args, **k: P.kwargs) -> R:
        ...
    return w
```

`P` is a `ParamSpec` — it captures an entire parameter list as one variable. `P.args` and
`P.kwargs` are not `tuple` and `dict`; they are the two halves of that captured list, and
the spec requires them to appear **together, in that order, as the last two parameters** of
the wrapper. The syntax exists solely so the checker can prove the wrapper forwards
everything unchanged.

*(measured, all four checkers, identical results)*

```console
naive:  Type of "greet_naive" is "(...) -> Any"
timed:  Type of "greet"       is "(name: str, times: int = 1) -> str"

ps1.py:27: error: Argument 1 to "greet" has incompatible type "int"; expected "str"
ps1.py:28: error: Argument "times" to "greet" has incompatible type "str"; expected "int"
```

The `naive` version accepts `greet_naive(123, "nope", bogus=1)` without a murmur.

**`Concatenate` handles the decorator that *changes* the signature** — the injection
pattern behind Flask/Click/pytest-style wrappers:

```python
def with_conn[**P, R](f: Callable[Concatenate[Conn, P], R]) -> Callable[P, R]:
    def w(*a: P.args, **k: P.kwargs) -> R: return f(Conn(), *a, **k)
    return w

@with_conn
def query(conn: Conn, sql: str, limit: int = 10) -> list[str]: ...
```

```console
Type of "query" is "(sql: str, limit: int = 10) -> list[str]"
ps1.py:40: error: Argument 1 to "query" has incompatible type "Conn"; expected "str"
```

`Concatenate[Conn, P]` says "a callable whose first parameter is a `Conn` and whose
remaining parameters are `P`". The decorator consumes the first parameter and the checker
knows it. Passing the `Conn` yourself is now an error — which is the correct API, and you
got it for free.

---

## 8. PEP 646: TypeVarTuple and array shapes

[PEP 646](https://peps.python.org/pep-0646/) generalises a type parameter to stand for an
*arbitrary number* of types. The motivating example is the one that made it worth doing:
array shapes.

```python
class Array[*Shape]:
    def __init__(self, *shape: *Shape) -> None: self.shape = shape

type H = Literal["height"]; type W = Literal["width"]; type C = Literal["channels"]

def add[*S](a: Array[*S], b: Array[*S]) -> Array[*S]: ...
def strip_batch[B, *S](a: Array[B, *S]) -> Array[*S]: ...
```

*(measured, all four checkers)*

```console
add(img, img)          ->  Array[Literal['height'], Literal['width'], Literal['channels']]
strip_batch(batch)     ->  Array[Literal['height'], Literal['width'], Literal['channels']]
strip_batch(img)       ->  Array[Literal['width'], Literal['channels']]

tvt2.py:15: error: Cannot infer value of type parameter "S" of "add"        # mypy
tvt2.py:15:10: error[invalid-argument-type] Expected `Array[H, W, C]`, found `Array[Literal[64], H, W, C]`   # ty
```

That last line is a shape mismatch caught at type-check time — the class of bug that
otherwise surfaces three layers into a training run. Note the prefix/suffix unpacking in
`strip_batch`: `[B, *S]` binds the first element and the rest, exactly like Python's own
starred assignment, which is where the syntax comes from.

**The honest limits.** This encodes *rank* and *labels*, not arithmetic. There is no
type-level `H * W`, no broadcasting rules, no way to say "these two dimensions must
multiply to that one". Numeric shape checking in Python remains a runtime concern; PEP 646
gets you dimension count and dimension identity, which is genuinely most of the practical
value but is not dependent typing.

A packaging reality: only one type parameter list may contain a `TypeVarTuple`, and NumPy's
own annotations use it only lightly. Treat this as a tool for your own array wrappers
rather than something you inherit from the ecosystem.

---

## 9. PEP 544: Protocols and structural typing

Everything so far has been nominal: `Dog` is an `Animal` because the class statement says
so. [PEP 544](https://peps.python.org/pep-0544/) adds the other half — **structural**
subtyping, where compatibility is decided by shape.

```python
class Closeable(Protocol):
    def close(self) -> None: ...
```

Any class with a matching `close` satisfies `Closeable`, with no inheritance, no
registration, and no import of your protocol at all. That last point is the design
argument: an ABC forces every implementer to depend on you, which inverts your dependency
graph in the wrong direction. A Protocol lets *consumers* declare the shape they need,
which is Dependency Inversion actually implemented rather than merely diagrammed. See
[`39-api-and-abstraction-design.md`](39-api-and-abstraction-design.md) for when to pick
which.

**Protocol variance follows exactly the §2 rules, inferred**, with no `_co` suffixes:

```python
class Producer[T](Protocol):
    def get(self) -> T: ...          # covariant
class Consumer[T](Protocol):
    def put(self, v: T) -> None: ... # contravariant
class Cell[T](Protocol):
    value: T                         # MUTABLE ATTRIBUTE -> invariant
```

```console
$ pyright pr2.py
pr2.py:29:4 - error: Argument of type "DogCell" cannot be assigned to parameter "c" of type "Cell[Animal]"
    "DogCell" is incompatible with protocol "Cell[Animal]"
      "value" is invariant because it is mutable
```

"`value` is invariant because it is mutable" is the whole of §2 in one diagnostic. If you
want a covariant data protocol, declare it as a read-only `@property`, not an attribute.

---

## 10. `runtime_checkable`: the check that isn't

`@runtime_checkable` makes `isinstance()` work against a Protocol. It is the most
dangerous convenience in `typing`, and the reason is stated plainly in the docs and
ignored universally: **it checks only the presence of members, never their signatures.**

Here is the failure, end to end. *(This is mandatory lab (c).)*

```python
@runtime_checkable
class Closeable(Protocol):
    def close(self, *, timeout: float = 0.0) -> None: ...

class TrapResource:                       # right name, wrong signature
    def close(self, force: bool) -> None: ...

def shutdown(r: Closeable) -> None:
    if isinstance(r, Closeable):          # <- the "safety" check
        r.close(timeout=1.5)
```

```console
$ python3.14 proto1.py
closed 1.5
isinstance says: True
Traceback (most recent call last):
  File "proto1.py", line 15, in shutdown
    r.close(timeout=1.5)
TypeError: TrapResource.close() got an unexpected keyword argument 'timeout'
```

`isinstance` returned `True`. The next line raised. The runtime check inspected
`hasattr(obj, "close")` and stopped.

Interestingly, **pyright warns about exactly this and the others don't** *(measured)*:

```console
$ pyright proto1.py
proto1.py:19:38 - error: Class overlaps "Closeable" unsafely and could produce a match at runtime
    Attributes of "TrapResource" have the same names as the protocol (reportGeneralTypeIssues)
```

Four more runtime facts you need *(measured, 3.14.6)*:

| Behaviour | Result |
|---|---|
| `isinstance(C(), HasName)` where `__init__` sets `self.name` | `True` |
| `isinstance(D(), HasName)` where `name: str` is **only an annotation** | **`False`** |
| `issubclass(C, HasName)` on a protocol with a data member | `TypeError: Protocols with non-method members don't support issubclass()` |
| `isinstance([], Sized)` vs `isinstance([], list)` | **154 ns vs 21 ns — 7×** |

Row 2 is a genuine trap: a bare annotation creates no attribute, so a class that "clearly"
satisfies the protocol fails `isinstance` at runtime while passing the static check. Row 4
matters if the check is in a hot loop — it walks the protocol's member list on every call.

**Rule:** `@runtime_checkable` is acceptable for coarse dispatch on a single-method
protocol you also control. It is not a validation boundary. If you need real validation at
a trust boundary, use pydantic or explicit checks — see
[`38-type-checking-in-practice.md`](38-type-checking-in-practice.md).

---

## 11. `Self`, and why overloads are a last resort

[PEP 673](https://peps.python.org/pep-0673/)'s `Self` solves the fluent-interface problem:
a method returning "the same type as the receiver, whatever that turns out to be".

```python
class Query:
    def where(self, c: str) -> Self: return self
    def clone_bad(self) -> "Query": return self
class UserQuery(Query):
    def by_email(self, e: str) -> Self: return self
```

*(measured, all four checkers agree)*

```console
UserQuery().where("x")      ->  UserQuery
UserQuery().clone_bad()     ->  Query
pr2.py:41: error: "Query" has no attribute "by_email"  [attr-defined]
```

Without `Self`, every builder method in the base class truncates the subtype and breaks
chaining. The same applies to `__enter__`, `copy()`, `from_dict()` classmethods, and every
`__iadd__`.

### `@overload`

Overloads let one function have several signatures. They are also the feature most often
reached for too early, so here is the resolution order that makes that judgement:

The [spec's overload call evaluation](https://typing.python.org/en/latest/spec/overload.html)
runs in six steps. The parts that decide your design:

1. **Step 1–2: first match wins.** Overloads are tried in source order.
2. **Step 3: argument type expansion.** If nothing matches, union arguments are expanded
   one at a time, left to right, and the results are unioned.
3. **Step 5: `Any` poisons the result.** If more than one overload survives and their
   return types are not *equivalent*, the call evaluates to `Any`.

Step 1's consequence is the classic bug — order from most specific to least:

```console
$ mypy --strict ovl.py
ovl.py:19: error: Overloaded function signature 2 will never be matched: signature 1's parameter type(s) are the same or broader  [overload-cannot-match]
$ pyright ovl.py
ovl.py:19:5 - error: Overload 2 for "bad" will never be used because its parameters overlap overload 1 (reportOverlappingOverload)
```

**pyrefly 1.2.0 does not flag unreachable overloads at all** *(measured — 0 errors on the
same file)*.

Step 5 is the one that quietly destroys type safety. With two non-overlapping overloads
and an `Any` argument, all four checkers correctly produce `Any`/`Unknown`. But push it
slightly:

```python
@overload
def get(key: str) -> str: ...
@overload
def get(key: str, default: None) -> str | None: ...
@overload
def get[T](key: str, default: T) -> str | T: ...

a: Any = 1
reveal_type(get("k", a))
```

| Checker | Result |
|---|---|
| mypy 2.3.0 | `Any` |
| ty 0.0.65 | `Unknown` (its spelling of `Any`) |
| pyright 1.1.411 | **`str \| None`** |
| pyrefly 1.2.0 | **`str \| None`** |

Reading Step 5 literally, `Any` looks right to me: not all materializations of `Any` are
assignable to `None`, so the second overload cannot eliminate the third, and the surviving
return types `str | None` and `str | Any` are not equivalent. **But I am reporting a
divergence, not declaring a winner** — pyright's authors wrote that spec section, so there
is likely a step-ordering subtlety I'm missing. File it as: *do not rely on cross-checker
agreement for overloads with `Any` arguments.*

**When to use overloads:** when the return type genuinely depends on the *value* or
*arity* of an argument in a way no single signature expresses (`open()`'s binary vs text
mode is the canonical case, keyed off `Literal`). **When not to:** as a substitute for a
union return, or to paper over an API that should have been two functions. Overloads are
unchecked against each other for consistency in most configurations, they multiply the
surface a checker must reason about, and they are where the four implementations diverge
most.

---

## 12. The precision toolkit: TypedDict, Literal, Annotated, Never

Types for the shapes real programs actually have.

### TypedDict — PEP 589 + 655 + 705

```python
class Movie(TypedDict):
    title: ReadOnly[str]      # PEP 705
    year: int
    tagline: NotRequired[str] # PEP 655
```

```console
$ ty check td.py
td.py:10:3: error[invalid-assignment] Cannot assign to key "title" on TypedDict `Movie`: key is marked read-only
td.py:11:3: error[invalid-key] Unknown key "bogus" for TypedDict `Movie`
td.py:12:13: info[revealed-type] Revealed type: `str | None`      # .get() of a NotRequired key
```

[PEP 705](https://peps.python.org/pep-0705/)'s `ReadOnly` exists for a variance reason
that should now be familiar: a mutable TypedDict item is in both input and output position,
so it is **invariant**, which blocks the natural structural subtyping you wanted. Mark it
`ReadOnly` and the item becomes covariant — a `Movie` becomes usable as a
`Named` (`{title: ReadOnly[str]}`) and subclasses may narrow the value type. `ReadOnly` is
also what makes width subtyping safe rather than a hole.

`Required`/`NotRequired` ([PEP 655](https://peps.python.org/pep-0655/)) replaced the old
`total=False` two-class dance. `total=` still works; per-item markers are strictly better.

### Literal and LiteralString

`Literal` ([PEP 586](https://peps.python.org/pep-0586/)) types a *value*.
`LiteralString` ([PEP 675](https://peps.python.org/pep-0675/)) types a whole *provenance
class*: strings the programmer wrote, including their concatenations and joins, but never
strings derived from input. It exists to make SQL injection a type error.

```python
def q(sql: LiteralString) -> None: ...
user = input()
q(f"SELECT * FROM t WHERE name = '{user}'")   # must be an error
```

```console
$ pyright ls1.py
ls1.py:4:3 - error: Argument of type "str" cannot be assigned to parameter "sql" of type "LiteralString"
$ ty check ls1.py
ls1.py:4:3: error[invalid-argument-type] Expected `LiteralString`, found `str`
$ pyrefly check ls1.py
ERROR ls1.py:4:3-43: Argument `str` is not assignable to parameter `sql` with type `LiteralString`

$ mypy --strict ls1.py
(no output, exit 0)
```

**mypy 2.3.0 does not enforce `LiteralString`.** For a feature whose entire purpose is
injection prevention, that is a material gap and a genuine reason to run a second checker
in CI on security-sensitive code. I verified this on the isolated four-line file above;
I did not investigate whether a mypy flag enables it.

### Annotated, Never, assert_type

`Annotated[T, ...]` ([PEP 593](https://peps.python.org/pep-0593/)) attaches metadata that
the type system ignores and libraries read — it is the mechanism behind FastAPI's
`Depends`, pydantic's `Field`, and Typer's options. `Annotated[int, x]` *is* `int` to a
checker.

`Never` is the bottom type: assignable to everything, nothing assignable to it. It is what
exhaustiveness checking is built on:

```console
$ mypy --strict nv.py
nv.py:13: error: Argument 1 to "assert_never" has incompatible type "Literal['c']"; expected "Never"
```

Add a member to your `Literal` union or your enum, and every `assert_never` in the
codebase becomes a compile error pointing at the switch you forgot. This is the single
highest-value pattern in this section — it turns "add a case" from a code-review problem
into a checker problem.

`assert_type(expr, T)` asserts the *inferred* type at a point. It is how you write tests
for your own annotations, and it is how I found the divergence in §17.4.

---

## 13. `@dataclass_transform`: how attrs and pydantic get checked

[PEP 681](https://peps.python.org/pep-0681/) answers a question every typed Python
codebase eventually hits: `@dataclass` gets a synthesised `__init__` because checkers
special-case it in their source. What about attrs, pydantic, SQLAlchemy, msgspec, or your
own model base?

`@dataclass_transform` is the extension point. Decorate a metaclass, base class, or
decorator factory with it, and checkers apply dataclass-like `__init__` synthesis to
anything it produces.

```python
@dataclass_transform(kw_only_default=True, field_specifiers=(Field,), frozen_default=True)
class ModelMeta(type): ...
class Model(metaclass=ModelMeta): ...

class User(Model):
    id: int
    name: str = Field(default="anon")
```

*(measured — all four checkers)*

```console
Type of "User.__init__" is "(self: User, *, id: int, name: str = "anon") -> None"

dct.py:19: error: Too many positional arguments for "User"        # kw_only_default=True
dct.py:20: error: Argument "id" to "User" has incompatible type "str"; expected "int"
dct.py:22: error: Property "id" defined in "User" is read-only    # frozen_default=True
```

That signature was synthesised from class-body annotations by four independent checkers
from one decorator. `field_specifiers=(Field,)` is what tells them `Field(default=...)` is
a field declaration rather than a value — note that in my minimal reproduction, `Field`
returning `Field` produces `error: Type "Field" is not assignable to declared type "str"`,
which is why real libraries annotate their field factory's return type as `Any`. That
`-> Any` is not sloppiness; it is load-bearing.

---

## 14. PEP 649/749: annotations became lazy in 3.14

This landed in the stable interpreter you are running, and it changes the runtime half of
this document.

**Before:** annotations were evaluated eagerly at function/class definition time, so a
forward reference had to be a string, and `from __future__ import annotations`
([PEP 563](https://peps.python.org/pep-0563/)) turned *all* of them into strings — which
fixed forward references and broke every library that reads `__annotations__`.

**Now** ([PEP 649](https://peps.python.org/pep-0649/), with implementation details revised
by [PEP 749](https://peps.python.org/pep-0749/)): the compiler emits an `__annotate__`
function per object. `__annotations__` is a descriptor that calls it on first access and
caches. You get real objects *and* forward references.

*(measured, 3.14.6)*

```python
@dataclass
class Node:
    value: int
    parent: Node | None = None       # UNQUOTED self-reference. Works.
    kids: list[Node] | None = None
```

```console
get_type_hints:      {'value': int, 'parent': Node | None, 'kids': list[Node] | None}
raw __annotations__: {'value': int, 'parent': Node | None, 'kids': list[Node] | None}
fields(Node)[1].type: ForwardRef('Node | None', is_class=True, owner=<class 'Node'>)
```

### `annotationlib` and the four formats

The new stdlib module exposes the machinery. `annotationlib.Format` has four members
*(measured)*: `VALUE`, `VALUE_WITH_FAKE_GLOBALS`, `FORWARDREF`, `STRING`.

```python
def f(a: Undefined, b: int = 3) -> "AlsoUndefined": ...

get_annotations(f, format=Format.VALUE)      # NameError: name 'Undefined' is not defined
get_annotations(f, format=Format.FORWARDREF) # {'a': ForwardRef('Undefined', owner=<function f>), 'b': int, ...}
get_annotations(f, format=Format.STRING)     # {'a': 'Undefined', 'b': 'int', 'return': 'AlsoUndefined'}
```

`FORWARDREF` is the format that makes tooling robust: unresolvable names come back as
`ForwardRef` objects instead of exploding. `dataclasses` in 3.14 uses it, which is why
`fields(...).type` is now a `ForwardRef` and no longer a `str` — **that is the porting
break most likely to hit your code.**

### What happened to `from __future__ import annotations`

*(verified against the 3.14 What's New)*: it is **deprecated and expected to be removed**,
but its behaviour in 3.14 is *unchanged* — annotations still become plain strings:

```console
$ python3.14 -c 'from __future__ import annotations
def h(x: int) -> str: ...
print(h.__annotations__)'
{'x': 'int', 'return': 'str'}
```

Removal will not happen until after 3.13 reaches end of life in 2029 (3.13 being the last
release without deferred evaluation). So: **stop adding the future import to new files, do
not urgently strip it from old ones, and audit every place you read `__annotations__`
directly.** Use `annotationlib.get_annotations` (or the `typing_extensions` backport for
cross-version code).

---

## 15. Runtime machinery, and what erasure costs

### How subscription works

`Repo[int]` calls `type(Repo).__getitem__` → `Repo.__class_getitem__(int)`. For builtins
it returns a `types.GenericAlias`, a C-level object holding `__origin__` and `__args__`.
`get_origin`/`get_args` read exactly those.

`__mro_entries__` is the second half. A `GenericAlias` is not a class, so it cannot be a
base — when the compiler sees `class Sub(list[int])`, it calls `list[int].__mro_entries__(...)`,
which returns `(list,)`. The alias is replaced by the real class for MRO purposes and the
original is preserved on `__orig_bases__` *(measured)*:

```console
list[int].__mro_entries__(()) = (<class 'list'>,)
Sub.__mro__                   = (Sub, list, object)
Sub.__orig_bases__            = (list[int],)
```

This is the same mechanism that lets `class C(Protocol[T])` work at all. See
[`41-metaclasses-and-class-construction.md`](41-metaclasses-and-class-construction.md).

### Erasure

```
   STATIC WORLD                       │  RUNTIME WORLD
   ───────────────────────────────────┼─────────────────────────────────────
   Repo[int]  ≠  Repo[str]            │  type(r_int) is type(r_str) is Repo
   T is solved, checked, propagated   │  T is a TypeVar object in a tuple
   list[Dog] is not list[Animal]      │  isinstance([], list[int]) -> TypeError
                                      │
                     ┌────────────────┴─────────────────┐
                     │  the annotations are DATA.       │
                     │  Nothing enforces them.          │
                     └──────────────────────────────────┘
```

*(measured)*

```console
type(Repo[int]([1,2])) is type(Repo[str](["a"])) is Repo   ->  True
isinstance([], list[int])  ->  TypeError: isinstance() argument 2 cannot be a parameterized generic
```

There is one partial escape hatch, and it is a trap:

```console
Repo[int]():
  inside __init__, __orig_class__: ABSENT      <- set AFTER __init__ returns
  after construction:              Repo[int]
Repo():
  after construction:              ABSENT      <- unparameterised call, nothing recorded
```

`__orig_class__` exists only when you construct through the subscripted alias, and it is
assigned *after* `__init__` completes. Every "get T at runtime" recipe built on it breaks
on both counts. **The only reliable pattern is to pass the type explicitly** —
`def __init__(self, t: type[T])` — which is why `TypeAdapter(int)` and
`Field(type=...)` look the way they do across the ecosystem.

### What subscription costs

*(measured, M3 Pro, 3.14.6, 200k iterations)*

| Expression | ns/op |
|---|---|
| `Repo([])` | **58** |
| `list[int]` | 57 |
| `Repo[int]` | 301 |
| `Repo[int]([])` | **437** |

**Subscripting a generic class at runtime costs ~7.5× a plain construction.** `list[int]`
is cheap (C-level `GenericAlias`), but `Repo[int]` goes through `typing._GenericAlias`.
Writing `Repo[int](...)` in a hot loop is pure waste — the parameter is erased anyway.
Annotate the variable instead:

```python
r: Repo[int] = Repo([])     # same static type, 58 ns instead of 437 ns
```

---

## 16. Honest unsoundness: the gradual bargain

Python's type system is **deliberately unsound**. It is not a bug list; it is a design
position, and the *gradual guarantee* is its statement: adding annotations to a working
program must never change its runtime behaviour, and removing them must never introduce
type errors. Soundness would break that. So the checkers accept programs that crash.

This section is mandatory lab (a). All four files below **pass mypy `--strict`, pyright,
ty, and pyrefly with zero diagnostics**, and all of them crash.

### The `Any` membrane

```
        ┌──────────────────────────────────────────────────────────┐
        │                                                          │
        │   int ──────────▶ ┌───────┐ ──────────▶ list[Dog]        │
        │                   │       │                              │
        │   Protocol ──────▶│  Any  │──────────▶ Callable[[], int] │
        │                   │       │                              │
        │   anything ──────▶└───────┘──────────▶ anything          │
        │                                                          │
        │   assignable TO Any          assignable FROM Any         │
        │   (like `object`)            (like `Never`)              │
        └──────────────────────────────────────────────────────────┘

        `object` is a top: everything goes in, nothing comes out.
        `Never`  is a bottom: nothing goes in, everything comes out.
        `Any` is BOTH ENDS AT ONCE. It is not a type; it is a hole
        in the lattice, and every value that passes through one
        loses its history.
```

```python
def load_ages(blob: str) -> dict[str, int]:
    ages: dict[str, int] = json.loads(blob)   # json.loads returns Any. Silent.
    return ages

load_ages('{"alice": "thirty"}')["alice"] + 1
# TypeError: can only concatenate str (not "int") to str
```

`json.loads` is annotated `-> Any` in typeshed because it *has* to be. Every
deserialization boundary in Python — JSON, YAML, pickle, `os.environ` parsing, ORM rows,
`**kwargs` from a config file — is an `Any` frontier. **`--strict` alone does not close
it:** mypy's `no-any-return` catches `return json.loads(x)` but not
`x: dict[str,int] = json.loads(...)`, and pyright/ty/pyrefly catch neither at default
settings. (mypy's `--disallow-any-expr` exists; almost nobody can run it.)

### `cast` is an assertion, not a conversion

```python
def as_dog(a: Animal) -> Dog:
    return cast(Dog, a)     # generates NO runtime code whatsoever
```

`cast` compiles to returning its second argument. It is you telling the checker to shut
up, and it is right roughly as often as your reasoning is.

### The variance escape (mandatory lab (b))

```python
def add_a_cat(animals: list[Animal]) -> None:
    animals.append(Cat())

dogs: list[Dog] = [Dog()]
add_a_cat(cast(list[Animal], dogs))   # the cast is the lie
for d in dogs: d.fetch()
# AttributeError: 'Cat' object has no attribute 'fetch'
```

mypy `--strict` exits 0. This is §2's proof, executed. One `cast` reopened the hole that
invariance exists to close — and note that the *same* hole opens with no `cast` at all
whenever the list arrives through an `Any`-typed boundary.

### `type[T]` does not constrain `__init__`

The prettiest one, because there is no `Any` and no `cast`:

```python
class Base:
    def __init__(self) -> None: ...
class Sub(Base):
    def __init__(self, x: int) -> None: self.x = x

def build(cls: type[Base]) -> Base:
    return cls()          # accepted by all four checkers

build(Sub)                # type[Sub] <: type[Base], so this is legal too
# TypeError: Sub.__init__() missing 1 required positional argument: 'x'
```

`type[Base]` promises the class is a subclass of `Base`. It promises **nothing** about the
constructor signature, because `__init__` is not covariant under subclassing and Python's
type system does not require it to be. Every plugin registry, every
`cls = REGISTRY[name]; cls()`, every `type[T]` factory parameter has this hole. Guard it
with a Protocol that declares `__call__`, or with an explicit factory callable.

### The other holes worth naming

- **`# type: ignore` and `# pyright: ignore`** — the honest ones, at least.
- **Unchecked functions.** mypy skips the bodies of unannotated functions by default;
  `--check-untyped-defs` is off unless you enable it.
- **`__eq__`, `__contains__`, `__getitem__`** are typed against `object` in typeshed for
  compatibility, so `"a" in [1, 2]` and `x == "wrong type"` are not errors.
- **Third-party code without stubs** is `Any` at every boundary (PEP 561).
- **Unreachable code is not checked at all** by mypy and pyright — see §17.5.

**The staff-level framing:** a type checker is a *high-precision, low-recall* bug finder.
It finds a class of bug essentially perfectly and is blind to everything else, by design.
Anyone who tells you a green mypy run means the code is type-safe has not read this
section. What it actually buys you is refactoring confidence and interface documentation
that cannot rot — which is worth a great deal, just not what people claim.

---

## 17. Checker behaviour, measured

Four independent implementations of one spec. They agree on most of §2–§13 — genuinely,
every variance, ParamSpec, TypeVarTuple, `Self`, and `dataclass_transform` result in this
document was identical across all four. The disagreements are concentrated and worth
knowing.

### 17.1 Speed

*(measured — `rich` 14.x, 100 files, 26,587 lines, M3 Pro, best of two runs, wall clock)*

| Checker | Cold | Warm | CPU utilisation |
|---|---|---|---|
| mypy 2.3.0 | **0.95 s** | 0.07 s (incremental cache) | ~99% (single-threaded) |
| pyright 1.1.411 | **2.85 s** | n/a | ~175% |
| ty 0.0.65 | **0.08 s** | n/a | ~710% |
| pyrefly 1.2.0 | **0.14 s** | n/a | ~480% |

ty is **~12× faster than mypy cold and ~35× faster than pyright** on this corpus, and it
gets there by actually using the machine — 7 cores' worth on a chip mypy runs one thread
on. Caveat: the four checkers reported 140 / 217 / 195 / 88 diagnostics respectively on
the same code, so they are not doing identical work. Treat this as an order-of-magnitude
result, which is what matters for the thing it changes: **sub-second whole-project
checking makes type checking an editor-latency feature instead of a CI step.**

### 17.2 Defaults are the biggest difference

**pyrefly, run with no config file, uses its `basic` preset** and reported **0 errors** on
§2's variance file that every other checker flagged. `basic` is documented as "parse errors
and a small set of high-confidence, locally-fixable checks" — it is an LSP-oriented mode,
not a type checker. Every pyrefly result in this document uses `-p default`.

If you evaluate pyrefly by running `pyrefly check` on a repo with no `pyrefly.toml` and
conclude it "found nothing", you have measured the preset, not the checker.

### 17.3 The divergence table

Everything here was reproduced on the versions in the provenance header.

| # | Case | mypy 2.3.0 | pyright 1.1.411 | ty 0.0.65 | pyrefly 1.2.0 |
|---|---|---|---|---|---|
| 1 | Covariant `TypeVar` in parameter position (§4) | error | error | **silent** | error |
| 2 | Constraint violation `pair_c(3, "a")` (§5) | error | error | **silent** | error |
| 3 | Unreachable / overlapping overload (§11) | error | error | silent | **silent** |
| 4 | `LiteralString` enforcement (§12) | **silent** | error | error | error |
| 5 | Statements after a `Never`-returning call (§17.5) | **not checked** | **not checked** | checked | checked |
| 6 | Overload + `Any` arg, 3 overloads (§11) | `Any` | `str \| None` | `Unknown` | `str \| None` |
| 7 | `type`-alias used as base class (§3.3) | error | error | **warning** | error |
| 8 | `x: T` declared but never assigned (§8 draft) | fine | unbound-variable | unbound-variable | unbound-variable |
| 9 | `bool` in `bool \| int \| ...` union | kept | kept | **simplified away** | kept |
| 10 | Revealed alias names | expanded | preserved | **preserved** | expanded |

Rows 1, 2 and 3 are *recall* gaps — the checker is missing a real error. Rows 6–10 are
*presentation or precision* differences. Row 4 is the one I would act on: if you rely on
`LiteralString` for injection safety, mypy is not enforcing it.

### 17.4 Narrowing precision: a three-way split

`assert_type` found this. Given a TypedDict with `year: int`:

```python
m: M = {"year": 1990}
reveal_type(m["year"])     # (a)
m["year"] = 1991
reveal_type(m["year"])     # (b)
```

| Checker | (a) | (b) |
|---|---|---|
| mypy 2.3.0 | `int` | `int` |
| pyright 1.1.411 | `int` | `Literal[1991]` |
| pyrefly 1.2.0 | `int` | `Literal[1991]` |
| ty 0.0.65 | **`Literal[1990]`** | `Literal[1991]` |

Three different answers. mypy performs no narrowing of TypedDict item types at all; ty
narrows even at the declaration. The practical consequence: `assert_type(m["year"], int)`
**passes on mypy and fails on the other three.** If you write type tests — and you should
— they are not portable across checkers without care.

### 17.5 Unreachable code

```python
def bottom() -> Never: raise RuntimeError
def h() -> None:
    y: int = bottom()
    z: Never = 1
    "clearly wrong" + 1
```

```console
$ mypy --strict unre.py      → (nothing, exit 0)
$ pyright unre.py            → 0 errors, 0 warnings
$ ty check unre.py
unre.py:5:16: error[invalid-assignment] Object of type `Literal[1]` is not assignable to `Never`
unre.py:6:5: error[unsupported-operator] Operator `+` is not supported between `Literal["clearly wrong"]` and `Literal[1]`
$ pyrefly check -p default unre.py    → 2 errors (same two)
```

mypy and pyright stop analysing after a `NoReturn`/`Never` call and never see the garbage
below it. `mypy --warn-unreachable` will at least tell you the code is dead
(`unre.py:5: error: Statement is unreachable`) — **turn it on**, because a
`NoReturn`-annotated function that stops raising silently un-checks everything after every
call site.

### 17.6 Practical recommendation

- **mypy** is the reference implementation for a lot of ecosystem behaviour, has the
  richest strictness flags, and has the best incremental story (`dmypy`). Weakest on
  `LiteralString` and narrowing precision.
- **pyright** has the best diagnostics of the four (`Type parameter "T@Box" is invariant`,
  `"value" is invariant because it is mutable`), the widest feature coverage, and it warns
  about unsafe `runtime_checkable` overlaps that nothing else does. Slowest here.
- **ty** (Astral) is at **0.0.65 — pre-1.0, and it shows** in rows 1, 2 and 7. It is also
  35× faster than pyright and has the most precise literal inference. Watch it; don't make
  it your only gate yet.
- **pyrefly** (Meta) is at **1.2.0 stable**, fast, thorough on unreachable code — and its
  unconfigured default preset checks almost nothing. Always commit a `pyrefly.toml`.

Running two checkers in CI is not paranoia; rows 1–5 are each a real bug class one of them
misses. Cost/benefit for a large codebase is in
[`38-type-checking-in-practice.md`](38-type-checking-in-practice.md).

---

## 18. Lab exercises

Reading this leaves you at rung 3 of the README §14 ladder — fluent, and one "why?" from
collapse. These move you to rung 4. Use `~/.local/bin/python3.14` plus at least two
checkers.

**1 — The gradual bargain (mandatory).** Reproduce §16: write one file containing the
`Any` membrane, a `cast`, the variance escape, and the `type[T]` constructor hole. Get it
to **zero diagnostics under mypy `--strict`, pyright, ty and pyrefly**, then make each one
crash. *Proves the single most important thing in this document: green ≠ safe. Rung 4 the
moment you've done it yourself.*

**2 — Variance violation (mandatory).** Build `Box`/`ROBox`/`Sink` from §4 with no
variance declarations and confirm the inferred variance with `assert_type` in six
directions. Then port them to legacy `TypeVar` and find the assignment that changes
meaning. *Proves you can predict inference rather than recite the definitions.*

**3 — `runtime_checkable` betrayal (mandatory).** Reproduce §10: a protocol whose method
takes a keyword-only argument, an impostor with a positional one, `isinstance` returning
`True`, and a `TypeError` on the next line. Then check whether *your* production code uses
`runtime_checkable` for anything that matters. *Proves you will never again treat it as
validation.*

**4 — Build the divergence table yourself.** Take §17.3, pin your own checker versions, and
re-run all ten rows. Report which have been fixed. *Proves the rung-5 skill of knowing when
your model has expired — this table will be wrong within two releases.*

**5 — Decorate something real.** Find a `Callable[..., Any]` decorator in a codebase you
own. Convert it to `[**P, R]`. Count the errors that appear in code that was previously
clean. *Proves §7's claim about accidental `Any` empirically, on your own code.*

**6 — Measure erasure.** Reproduce §15's timing table on your machine, then find every
`SomeGeneric[T](...)` construction in a hot path and convert it to an annotated variable.
*Proves you know which parts of the type system have runtime cost — most people assume
zero.*

**7 — Port to 3.14 annotations.** Take a module using `from __future__ import annotations`
and any `get_type_hints`/`__annotations__` introspection. Remove the future import, run on
3.14, and fix what breaks — especially `dataclasses.fields(...).type` now being a
`ForwardRef`. Then make it work on 3.12 too via `typing_extensions`. *Proves §14 in the
only way that counts.*

**8 — Type-test your own library.** Write an `assert_type` suite for one generic class you
maintain. Run it under two checkers. *Proves §17.4 — and if it passes on both, you have a
regression guard for your public API's inference behaviour, which is a rare and genuinely
valuable artifact.*

---

## 19. Question bank

1. Why is `list[Dog]` not a `list[Animal]`, and why *is* `Sequence[Dog]` a `Sequence[Animal]`? Derive it, don't recite it. *(§2)*
2. `Callable[[Dog], None]` vs `Callable[[Animal], None]` — which is assignable to which, and why does that feel backwards? *(§2)*
3. A colleague annotates a read-only helper's parameter as `list[str]`. What does that cost, and what should it be? *(§2)*
4. What are the three new scoping/evaluation behaviours PEP 695 introduces, and which one differs from comprehensions in the same textual position? *(§3)*
5. Why can `type X = list[X]` work without quotes, and at what moment would a typo in it raise? *(§3.2)*
6. Variance is inferred under PEP 695. Describe the inference algorithm, and give a class whose assignability *changes* when you port it from `Generic[T]`. *(§4)*
7. Bound vs constraint: which one loses the subtype of its argument, and which one type-checks the body more than once? *(§5)*
8. What problem do PEP 696 defaults solve for a library author, and what is the ordering rule? Where is it enforced — checker, compiler, or runtime? *(§6)*
9. Write the `[**P, R]` decorator. What do `P.args`/`P.kwargs` mean, where must they appear, and what does `Concatenate` add? *(§7)*
10. `@runtime_checkable` returns `True` and the next line raises `TypeError`. Explain exactly what was checked. Name two more runtime surprises from that decorator. *(§10)*
11. Overload resolution: what does an `Any` argument do to the result type, and why is that a soundness decision rather than an implementation detail? *(§11)*
12. Why does `ReadOnly` (PEP 705) exist? Answer in terms of §2, not in terms of "immutability". *(§12)*
13. What does `LiteralString` prevent, and which mainstream checker did not enforce it on the version tested here? *(§12)*
14. `from __future__ import annotations` in 3.14: deprecated, removed, or unchanged? What replaced it, and what broke in `dataclasses`? *(§14)*
15. Name a program that fully type-checks under four checkers and still crashes — without using `Any` or `cast`. *(§16)*
16. Generics are erased. Given an instance, how would you recover its type argument, and give two reasons your answer is unreliable. *(§15)*
17. `Repo[int]()` vs `Repo()`: measured cost difference, and the correct fix. *(§15)*
18. State the gradual guarantee, and explain why soundness is incompatible with it. *(§16)*
19. Your CI runs mypy `--strict` and is green. List five distinct bug classes it structurally cannot see. *(§16)*
20. You must pick one checker for a 400k-line codebase in Aug 2026. Defend the choice with the maturity, speed, and recall trade-offs — and say what you'd run as a second gate. *(§17)*

---

## 20. Sources

**Primary — the specification, not the tutorials**

- [Specification for the Python type system](https://typing.python.org/en/latest/spec/) — **the** authoritative document; supersedes the PEPs for anything they conflict on. *Verdict: read [Generics](https://typing.python.org/en/latest/spec/generics.html), [Protocols](https://typing.python.org/en/latest/spec/protocol.html), and [Overloads](https://typing.python.org/en/latest/spec/overload.html) in full — the six-step overload algorithm in the last one is not written down anywhere else.*
- [Python language reference §4.2.3 Annotation scopes](https://docs.python.org/3/reference/executionmodel.html#annotation-scopes) — *Verdict: short, and the only place the class-namespace exception in §3.1 is stated normatively.*
- [`typing` module docs](https://docs.python.org/3/library/typing.html) and [`annotationlib`](https://docs.python.org/3/library/annotationlib.html) — *Verdict: reference, not reading. `annotationlib` is new in 3.14 and worth a full pass.*

**PEPs (all numbers verified against peps.python.org while writing)**

| PEP | Title | Verdict |
|---|---|---|
| [695](https://peps.python.org/pep-0695/) | Type Parameter Syntax | Read §Variance Inference and §Lazy Evaluation. The rest is syntax. |
| [696](https://peps.python.org/pep-0696/) | Type Defaults for Type Parameters | Short. Read the ordering/scoping rules. |
| [612](https://peps.python.org/pep-0612/) | Parameter Specification Variables | Read the motivation; it is the clearest statement of the decorator problem. |
| [646](https://peps.python.org/pep-0646/) | Variadic Generics | Read the motivation only unless you own an array library. |
| [544](https://peps.python.org/pep-0544/) | Protocols: Structural subtyping | Read in full — including §Rejected Ideas on protocol variance. |
| [673](https://peps.python.org/pep-0673/) | Self Type | Ten minutes. |
| [589](https://peps.python.org/pep-0589/) / [655](https://peps.python.org/pep-0655/) / [705](https://peps.python.org/pep-0705/) | TypedDict, Required/NotRequired, ReadOnly | Read 705's motivation; it is a variance argument. |
| [675](https://peps.python.org/pep-0675/) | Arbitrary Literal String Type | Read the threat model. |
| [681](https://peps.python.org/pep-0681/) | Data Class Transforms | Read if you maintain a model base class. |
| [649](https://peps.python.org/pep-0649/) / [749](https://peps.python.org/pep-0749/) | Deferred annotations, and its implementation | 749 supersedes parts of 649 — read 749's §"The future of `from __future__ import annotations`". |
| [586](https://peps.python.org/pep-0586/) / [593](https://peps.python.org/pep-0593/) / [604](https://peps.python.org/pep-0604/) / [613](https://peps.python.org/pep-0613/) | Literal, Annotated, `X \| Y`, explicit TypeAlias | Reference. |
| [484](https://peps.python.org/pep-0484/) / [563](https://peps.python.org/pep-0563/) / [561](https://peps.python.org/pep-0561/) | The originals | 484 for history; 563 to know what you're migrating off. |
| [742](https://peps.python.org/pep-0742/) / [728](https://peps.python.org/pep-0728/) | TypeIs, TypedDict extra items | Adjacent; not covered here. |

**Checker documentation**

- [mypy docs](https://mypy.readthedocs.io/) — *Verdict: the "Common issues" page on variance is linked directly from its own error messages and is genuinely good.*
- [pyright configuration & type-concepts docs](https://microsoft.github.io/pyright/) — *Verdict: the best free prose explanation of assignability and narrowing that exists; better than the spec for learning.*
- [ty](https://docs.astral.sh/ty/) — *Verdict: pre-1.0 (0.0.65 as tested). Docs are thin; the release notes are the real changelog.*
- [pyrefly](https://pyrefly.org/) — *Verdict: read the configuration page **first**; the preset system (§17.2) will otherwise mislead you.*

**Sibling docs**

- [`36-type-system-foundations.md`](36-type-system-foundations.md) — gradual typing and assignability, which §2 and §16 assume.
- [`38-type-checking-in-practice.md`](38-type-checking-in-practice.md) — strictness ladder, stubs, and rolling this into a large codebase.
- [`39-api-and-abstraction-design.md`](39-api-and-abstraction-design.md) — Protocol vs ABC as a *design* decision rather than a typing one.
- [`40-data-model-and-descriptors.md`](40-data-model-and-descriptors.md) and [`41-metaclasses-and-class-construction.md`](41-metaclasses-and-class-construction.md) — `__class_getitem__`, `__mro_entries__`, and the machinery under §15.

---

*Next: [`38-type-checking-in-practice.md`](38-type-checking-in-practice.md) — where §17's
divergence table stops being trivia and becomes a tooling decision for a codebase you can't
rewrite.*
