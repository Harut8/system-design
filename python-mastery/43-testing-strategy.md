# 43 — Testing strategy: suites that find bugs, not suites that pass

> **Tier 8, doc 43.** Prerequisites: [`42-runtime-code-manipulation.md`](42-runtime-code-manipulation.md)
> (the import system, meta-path finders, AST transformation, `mock.patch` mechanics),
> [`31-measurement-methodology.md`](31-measurement-methodology.md) (what a result is worth,
> the noise floor, why a bad measurement is worse than none),
> [`32-profiling.md`](32-profiling.md) §4 (`sys.monitoring` vs `sys.setprofile`, measured).
> Useful but not required: [`24-the-gil.md`](24-the-gil.md) §6 and §9,
> [`26-free-threading.md`](26-free-threading.md) §5,
> [`30-concurrency-correctness.md`](30-concurrency-correctness.md).
> Feeds into: [`44-packaging-and-environments.md`](44-packaging-and-environments.md),
> [`45-supply-chain-and-security.md`](45-supply-chain-and-security.md),
> [`46-production-python.md`](46-production-python.md).
>
> **THESIS: a test suite is a *measuring instrument*, and almost nobody measures the
> instrument.** Coverage tells you which lines ran, which is a statement about your tests'
> *reach*, not their *power*. The two things that actually tell you whether a suite can
> detect a defect are **mutation testing** (inject known defects, count how many the suite
> catches) and **property-based testing** (let a machine search the input space instead of
> asking a human to imagine it). This document is built around a single measured result
> from §10: a module at **100% line coverage and 100% branch coverage** whose test suite
> still fails to kill **6 of 16 injected mutants** — and every one of the six survivors is
> a boundary off-by-one, the single most common real bug in business logic.
>
> The second thesis, which is really a corollary: **property-based testing is the highest
> leverage idea in this tier**, and its most valuable component is not generation — it is
> **shrinking**. Generation finds a failure somewhere in a 400-element list of random
> integers. Shrinking turns that into `[1000]`. The first is a lottery ticket; the second
> is a bug report.

> **Verification provenance.** Every number marked *(measured)* below came out of a live
> process on the machine this repo lives on — **Apple M3 Pro, macOS, arm64** — during the
> writing of this document, on **CPython 3.14.6** (`~/.local/bin/python3.14`) in a fresh
> `uv` venv containing **pytest 9.1.1**, **Hypothesis 6.165.0**, **coverage 7.15.2**,
> **pluggy 1.6.0**, and **mutmut 3.7.0**. Facts marked *(verified)* were read out of the
> installed source or a primary document, not recalled. Versions were resolved against the
> PyPI JSON API on 2026-08-02: pytest **9.1.1** (2026-06-19), Hypothesis **6.165.0**
> (2026-08-02 — it ships several releases a week, so yours will be newer), coverage
> **7.15.2**, mutmut **3.7.0**, cosmic-ray **8.4.6**, atheris **3.1.0**, pluggy **1.6.0**.
>
> **The machine was not quiet.** `load1` sat between 2.2 and 2.9 throughout. Per
> [`31-measurement-methodology.md`](31-measurement-methodology.md), every timing below was
> run twice in alternation and both passes are reported; where the two passes disagree
> materially I say so and treat the result as inconclusive rather than rounding it into a
> conclusion. §11.5 is exactly such a case, and it is left in deliberately.
>
> **What I could not verify is flagged in place**, in the text, at the point of use.

## Contents

1. [What a test suite is actually for](#1-what-a-test-suite-is-actually-for)
2. [pytest's architecture: pluggy, collection, conftest](#2-pytests-architecture-pluggy-collection-conftest)
3. [Fixtures: resolution, caching, and the scope trap](#3-fixtures-resolution-caching-and-the-scope-trap)
4. [Parametrization and the collection/run boundary](#4-parametrization-and-the-collectionrun-boundary)
5. [Assertion rewriting: an import hook that edits your source](#5-assertion-rewriting-an-import-hook-that-edits-your-source)
6. [Property-based testing: the shape of the idea](#6-property-based-testing-the-shape-of-the-idea)
7. [Inside Hypothesis: choice sequences, generation, shrinking](#7-inside-hypothesis-choice-sequences-generation-shrinking)
8. [Stateful testing: properties over sequences of operations](#8-stateful-testing-properties-over-sequences-of-operations)
9. [What property tests find that example tests structurally cannot](#9-what-property-tests-find-that-example-tests-structurally-cannot)
10. [Mutation testing: measuring the instrument](#10-mutation-testing-measuring-the-instrument)
11. [Coverage semantics and their limits](#11-coverage-semantics-and-their-limits)
12. [Testing concurrency: probability, not correctness](#12-testing-concurrency-probability-not-correctness)
13. [Flakiness as a systems problem](#13-flakiness-as-a-systems-problem)
14. [Test design: doubles, contracts, golden tests, and when not to mock](#14-test-design-doubles-contracts-golden-tests-and-when-not-to-mock)
15. [Fuzzing Python: atheris and where it belongs](#15-fuzzing-python-atheris-and-where-it-belongs)
16. [Judgment: what a good suite optimizes for, and when to delete a test](#16-judgment-what-a-good-suite-optimizes-for-and-when-to-delete-a-test)
17. [Lab exercises](#17-lab-exercises)
18. [Question bank](#18-question-bank)
19. [Sources](#19-sources)

---

## 1. What a test suite is actually for

Before any tooling, get the objective function right, because almost every bad testing
decision is a good decision against the wrong objective.

A test suite has exactly three jobs, and they are in tension:

| Job | What it buys | What it costs |
|---|---|---|
| **Fast feedback** | you learn you broke something in seconds, while the change is still in your head | you must keep the suite fast, which means most tests can't touch a network, a database, or a clock |
| **Defect localization** | a failure names the broken thing, not "the checkout flow is red" | you must test small units, which means more tests and more coupling to structure |
| **Refactor safety** | you can restructure internals without rewriting tests | you must test *behaviour through stable interfaces*, which is the opposite of "test small units" |

Notice that jobs 2 and 3 pull in opposite directions. This is the entire content of the
"unit vs integration" argument, and the reason it never resolves. There is no universal
answer; there is a per-codebase equilibrium that you choose deliberately or drift into
accidentally.

A fourth thing suites are often asked to do — **serve as documentation** — is real but
secondary, and it is the job most often used to justify keeping a test that has no
detection power (§16).

**What a suite is not for:** producing a coverage number. Coverage is an *input* to
judgment, and §11 is about exactly how weak an input it is.

> **The framing this document runs on.** Treat the suite as an instrument for detecting
> defects. Then the natural questions are the ones you'd ask of any instrument:
> what is its *sensitivity* (§10 — what fraction of injected defects does it detect?),
> its *precision* (§13 — what fraction of its alarms are real?), and its *domain*
> (§9 — which regions of the input space has it never sampled?). Coverage answers none
> of those three. That is why it dominates the conversation: it is the only one that is
> cheap to compute.

---

## 2. pytest's architecture: pluggy, collection, conftest

pytest is not a test runner with a plugin system bolted on. **pytest is a pluggy
application**, and essentially all of its own behaviour is implemented as plugins calling
its own hooks. Once you see that, its extension model and its failure modes both become
obvious.

### 2.1 pluggy in one page

`pluggy` (1.6.0 *(verified installed)*) is a hook-dispatch library. Its entire public
surface is small enough to list *(verified from `pluggy/__init__.py`'s `__all__`)*:

```
PluginManager · HookCaller · HookRelay · HookImpl
HookspecMarker · HookimplMarker · HookspecOpts · HookimplOpts
Result · HookCallError · PluginValidationError
PluggyWarning · PluggyTeardownRaisedWarning
```

The model:

- A **hookspec** declares a name and a signature. pytest's live in
  `_pytest/hookspec.py` — **52 hooks** *(measured: `grep -c '^def pytest_'`)*, from
  `pytest_addhooks` through `pytest_leave_pdb`.
- A **hookimpl** is any function in a registered plugin whose name matches a hookspec.
  Registration comes from three places: `conftest.py` files, `-p name` on the command
  line, and installed distributions declaring a **`pytest11` entry point** *(verified:
  `_pytest/config/__init__.py` calls `load_setuptools_entrypoints("pytest11")`)*.
- Calling a hook calls **every** implementation and returns the list of non-`None`
  results — **LIFO by registration order**, so the most recently registered plugin runs
  first. `conftest.py` files deeper in the tree register later, hence run earlier.

Four modifiers control that ordering and shape *(verified from `pluggy/_hooks.py`'s
`HookimplOpts`)*: `tryfirst`, `trylast`, `wrapper`, `hookwrapper` (the deprecated
old-style form), plus `optionalhook` and `specname`. And on the spec side, `firstresult`
— **exactly 17 of pytest's 52 hookspecs are `firstresult=True`** *(measured, by parsing
`hookspec.py`)*, meaning dispatch stops at the first non-`None` return. The full set:

```
pytest_cmdline_parse      pytest_cmdline_main        pytest_collection
pytest_ignore_collect     pytest_collect_directory   pytest_make_collect_report
pytest_pycollect_makemodule                          pytest_pycollect_makeitem
pytest_pyfunc_call        pytest_make_parametrize_id pytest_runtestloop
pytest_runtest_protocol   pytest_runtest_makereport  pytest_fixture_setup
pytest_report_to_serializable   pytest_report_from_serializable
pytest_report_teststatus
```

That list is worth reading as a map of pytest's *replaceable* seams: a plugin can take
over the whole run loop (`pytest_runtestloop`), the per-test protocol
(`pytest_runtest_protocol`), how a test function is invoked (`pytest_pyfunc_call`), or how
a fixture is constructed (`pytest_fixture_setup`) — rather than merely observing any of
them. Note what is *not* on the list: `pytest_collect_file` is a normal all-implementations
hook *(verified — a natural thing to get wrong, since its sibling
`pytest_collect_directory` is `firstresult`)*.

A **wrapper** is a generator that yields exactly once *(verified against
docs.pytest.org "Writing hook functions")*:

```python
@pytest.hookimpl(wrapper=True)
def pytest_pyfunc_call(pyfuncitem):
    setup()
    res = yield          # raises here if the inner impls raised
    return post(res)     # you MUST return a result or raise
```

The `yield` returns the inner result and *re-raises* inner exceptions at the yield point.
This is the mechanism behind timing plugins, `pytest-cov`'s bookkeeping, retry plugins,
and every "wrap the whole test" behaviour you've seen. It is also the mechanism behind
the classic plugin bug: an old-style `hookwrapper=True` implementation that raises in its
teardown half triggers `PluggyTeardownRaisedWarning` and swallows the original failure.

**The consequence for you:** almost anything you want pytest to do differently is a hook,
not a fork. Before writing a wrapper script around pytest, check whether the behaviour is
reachable from `pytest_collection_modifyitems` (reorder/deselect), `pytest_runtest_setup`
(skip conditionally), `pytest_runtest_makereport` (reclassify outcomes), or
`pytest_report_teststatus` (change what a letter means).

### 2.2 The lifecycle, drawn

This is the diagram to hold in your head. Hook names are real *(verified against
`_pytest/hookspec.py`)*; the vertical axis is time.

```
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ STARTUP                                                                      │
 │   pytest_cmdline_main(config)                       [firstresult]            │
 │     └ pytest_load_initial_conftests   ← rootdir + initial conftest.py files  │
 │       └ pluginmanager.load_setuptools_entrypoints("pytest11")                │
 │         └ pytest_addoption / pytest_configure                                │
 │           └ AssertionRewritingHook installed at sys.meta_path[0]   (§5)      │
 └───────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ COLLECTION            pytest_collection(session)    [firstresult]            │
 │                                                                              │
 │   for each arg path, walk down:                                              │
 │     pytest_ignore_collect(path)      [firstresult] → skip subtree?           │
 │     pytest_collect_directory(path)   [firstresult] → Dir / Package node      │
 │     pytest_collect_file(path)                     → Module node (all impls)  │
 │        ↑ THIS is the moment conftest.py files on that path are imported      │
 │                                                                              │
 │     Module.collect():                                                        │
 │       pytest_pycollect_makemodule   [firstresult]                            │
 │       pytest_pycollect_makeitem     [firstresult]  ← per name in the module  │
 │       pytest_generate_tests(metafunc) ← PARAMETRIZATION HAPPENS HERE  (§4)   │
 │         └ metafunc.parametrize(...) → N Function items, ids computed now     │
 │                                                                              │
 │   pytest_collection_modifyitems(session, config, items)  ← reorder/deselect  │
 │   pytest_collection_finish(session)      (session.items is already set)      │
 └───────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ RUN                   pytest_runtestloop(session)                            │
 │   for item in session.items:                                                 │
 │     pytest_runtest_protocol(item, nextitem)         [firstresult]            │
 │       ├ pytest_runtest_logstart                                              │
 │       ├ SETUP    pytest_runtest_setup(item)                                  │
 │       │            └ fixture graph resolved & instantiated:      (§3)        │
 │       │                 pytest_fixture_setup(fixturedef, request)            │
 │       │                 …cache hit? return cached_result[0]                  │
 │       ├ CALL     pytest_runtest_call(item)                                   │
 │       │            └ pytest_pyfunc_call(pyfuncitem)  [firstresult]           │
 │       │                 └ testfunction(**testargs)   ← your test body        │
 │       ├ TEARDOWN pytest_runtest_teardown(item, nextitem)                     │
 │       │            └ finalizers, LIFO, only down to nextitem's shared scope  │
 │       │                 pytest_fixture_post_finalizer(...)                   │
 │       └ each phase → pytest_runtest_makereport [firstresult]                 │
 │                    → pytest_runtest_logreport  → terminal / junitxml / …     │
 └───────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ FINISH   pytest_sessionfinish → pytest_terminal_summary → pytest_unconfigure │
 └─────────────────────────────────────────────────────────────────────────────┘
```

Three things this diagram is meant to make unavoidable:

1. **There are three reports per test, not one** — setup, call, teardown. This is why a
   fixture error shows as `ERROR` and an assertion failure as `FAILED`, and why
   `pytest_runtest_makereport` is where retry and flaky plugins hook in.
2. **Parametrization is a *collection*-time event.** By the time anything runs, the N
   parameter sets are already N distinct `Function` items with fixed ids. §4 is entirely
   about the consequences.
3. **Teardown is not "after each test."** It is "down to the scope shared with
   `nextitem`". That `nextitem` argument on `pytest_runtest_teardown` is the whole reason
   a session fixture isn't torn down between tests — and the reason reordering tests
   changes how many times a module-scoped fixture is constructed.

### 2.3 `conftest.py` resolution — the rules that actually bite

`conftest.py` is a plugin that is auto-registered based on filesystem position. The rules
*(verified from `_pytest/config/__init__.py`, `_loadconftestmodules` / `_is_in_confcutdir`)*:

- For a collected path, pytest loads the `conftest.py` in **every directory from the
  rootdir (or `confcutdir`) down to that path**, outermost first.
- Loading happens **at collection time for that path**, not at startup — except for
  "initial" conftests near the invocation directory, which load during
  `pytest_load_initial_conftests`. pytest 9.1.1 has a bug fix specifically restoring
  loading of `<invocation dir>/test*` conftests when pytest is invoked with no arguments
  *(verified: pytest 9.1.1 changelog, issue #14608)* — a good illustration of how subtle
  this ordering is even for the maintainers.
- Registration is LIFO, so **deeper `conftest.py` hook implementations run before
  shallower ones**, and fixture definitions in a deeper `conftest.py` *shadow* the same
  name defined higher up.
- `--confcutdir` bounds the upward search. `-p no:name` cannot disable a conftest —
  pytest 9.0.3 made that an explicit `UsageError` rather than an internal assertion
  failure *(verified: 9.0.3 changelog, issue #13634)*.

The rule that catches everyone: **a fixture override can request the fixture it
overrides.** Same name, different scope in the graph:

```python
# conftest.py
@pytest.fixture
def func(sess): ...

# pkg/conftest.py
@pytest.fixture
def func(func):        # the OUTER func, not infinite recursion
    return func + "+pkg"
```

*(measured — this runs, and `--setup-plan` shows both being constructed; see §3.)*

**The hazard `conftest.py` creates.** It is an implicit, position-dependent import that
executes before your tests and can register hooks that change collection globally. A
`conftest.py` five directories up can deselect your test, monkeypatch your environment, or
install an import hook. When someone says "the test passes locally but not in CI", the
second thing to check (after §13's list) is whether the two runs had the same rootdir and
therefore the same conftest chain. `pytest --collect-only -q` and `pytest --co --setup-plan`
are the tools; `pytest --rootdir` and the header line pytest prints are the evidence.

### 2.4 What changed in pytest 9 (and why you'll hit it)

Verified from the pytest 9.0.0 / 9.1.0 release notes:

- **Subtests are core.** `pytest.Subtests` (`subtests` fixture) merged in from
  `pytest-subtests`; `unittest.TestCase.subTest` is supported. Marked experimental —
  the reporting shape may change.
- **Native TOML configuration.** `[tool.pytest]` in `pyproject.toml`, or `pytest.toml` /
  `.pytest.toml`. The old `[tool.pytest.ini_options]` "INI compatibility mode" (all values
  strings) still works, but the two tables cannot both be used.
- **`strict` mode**, enabling `strict_config`, `strict_markers`,
  `strict_parametrization_ids`, `strict_xfail` at once — and, by design, any future
  strictness option too. Only enable it against a pinned pytest.
- **`PytestRemovedIn9Warning` became an error by default in 9.0**, and the affected
  features were removed in 9.1. If you skipped a major version, this is where your suite
  breaks.
- **Overlapping path arguments changed semantics**: `pytest a/ a/b` is now equivalent to
  `pytest a`, and `pytest x.py x.py` runs the file once. `--keep-duplicates` restores the
  old behaviour.
- **`faulthandler_exit_on_timeout`** (default false) will now actually *kill* the process
  on a faulthandler timeout instead of only dumping thread tracebacks. For a suite that
  deadlocks in CI, turning this on converts a 6-hour hung job into a stack dump plus a
  non-zero exit — see §12.
- **Deprecated for removal in pytest 10:** class-scoped fixtures defined as instance
  methods without `@classmethod`; `request.getfixturevalue()` during teardown for a
  fixture not already requested; non-`Collection` iterables (generators!) as
  `parametrize` `argvalues`. That last one is worth acting on now: a generator as
  `argvalues` is exhausted after the first collection, so tests silently vanish when the
  module is collected twice.

---

## 3. Fixtures: resolution, caching, and the scope trap

### 3.1 Resolution

A fixture is a function registered under a name. When a test (or another fixture) has a
parameter with that name, pytest resolves it by searching, nearest-first:

1. the test class,
2. the test module,
3. `conftest.py` files from the test's directory upward,
4. plugins (including builtins: `tmp_path`, `monkeypatch`, `capsys`, `request`, …).

The resolved set forms a DAG, topologically sorted, instantiated in dependency order.
`request.getfixturevalue("name")` escapes into dynamic resolution — useful, and now
deprecated during teardown for fixtures not already requested (§2.4).

### 3.2 Caching is per-`FixtureDef`, keyed by scope

The mechanism, read from source *(verified: `_pytest/fixtures.py`)*: each `FixtureDef`
holds `self.cached_result`, a 3-tuple of `(value, cache_key, exception_info)`. On
`execute()`, if `cached_result` exists and `self.cache_key(request)` matches the stored
key, the cached value is returned — or, if the third element is set, **the stored
exception is re-raised**. That last detail is why a fixture that blows up once produces
identical errors for every dependent test in scope instead of retrying: the *failure* is
cached, not just the value.

`cache_key` is the request's param (for parametrized fixtures) — so a fixture with
`params=[...]` gets one cache entry per param value, and the whole dependent subtree is
re-run per value.

Scopes are an ordered enum *(verified: `_pytest/scope.py`)*:

```
Function < Class < Module < Package < Session
```

`Scope` is `@total_ordering`, and that ordering is load-bearing: **a fixture may not
depend on a fixture of narrower scope.** A session fixture requesting a function fixture
is an error, because the session fixture would outlive the value it captured.

### 3.3 Seeing it, rather than believing it

`--setup-plan` prints the whole plan without running anything. On the toy tree from §2.3
(a session fixture `sess`, a function fixture `func`, overridden in `pkg/conftest.py`, and
a 3-way parametrized test) *(measured)*:

```
        demo/pkg/sub/test_demo.py::test_assertion_rewriting
SETUP    S sess
        SETUP    F n[1]
        SETUP    F func (fixtures used: sess)      ← outer func
        SETUP    F func (fixtures used: func)      ← pkg override, wrapping it
        demo/pkg/sub/test_demo.py::test_scope[1] (fixtures used: func, n, sess)
        TEARDOWN F func
        TEARDOWN F func
        TEARDOWN F n[1]
        SETUP    F n[2]
        ...
TEARDOWN S sess
```

Read it carefully: `S sess` is constructed **once** at the top and torn down **once** at
the bottom, across all four tests. The two `func` entries are the override chain, torn
down LIFO. The parametrization `n[1..3]` is a *fixture* (`--setup-plan` shows it as
`SETUP F n[1]`), which is exactly what §4 explains.

`--setup-show` does the same during a real run. **Use these two flags before arguing about
fixture behaviour**; they turn an argument into an observation, which is the same move
[`31-measurement-methodology.md`](31-measurement-methodology.md) makes about performance.

### 3.4 The scope trap, and the two real failure modes

**Failure mode 1: state leaks through a broad-scoped fixture.** A session-scoped database
handle, a module-scoped `TestClient`, a session-scoped `tmp_path_factory` directory — any
of these accumulates state, and tests then depend on execution order without saying so.
This is the single largest source of "passes alone, fails in the suite" (§13). The
diagnostic is to shuffle the order — install `pytest-randomly` (4.1.0 *(verified)*), which
randomizes test order by default once installed and prints the seed it used
(`-p no:randomly` turns it off) — or simply run the failing test alone. The fix is either
narrowing the scope or adding an explicit reset in the fixture body.

**Failure mode 2: broad scope taken for speed, then defeated.** Teams promote a fixture to
`session` scope because setup is slow, then add a `function`-scoped "reset" fixture that
undoes most of the setup. Net effect: the same cost, plus an implicit ordering dependency.
Measure before promoting — `--durations=20` tells you whether setup is actually the cost.

**A Hypothesis-specific trap that belongs here:** function-scoped fixtures interact badly
with `@given`, because the fixture is set up **once per test function**, not once per
generated example. Hypothesis detects this and raises the
`HealthCheck.function_scoped_fixture` health check *(verified: it is one of the seven
members of `HealthCheck` — `data_too_large`, `filter_too_much`, `too_slow`,
`large_base_example`, `function_scoped_fixture`, `differing_executors`, `nested_given`)*.
Suppressing that health check without understanding it is how you get a property test
where example #2 sees the state example #1 left behind.

---

## 4. Parametrization and the collection/run boundary

`@pytest.mark.parametrize` is not a loop. It is an instruction to `pytest_generate_tests`
to produce **N separate collected items**, at collection time, each with its own id,
its own report, its own fixture instantiation.

This has consequences people trip over constantly:

- **Each parameter set is an independent test.** One failing does not stop the others; each
  gets its own setup/teardown; `-x` stops at the first failing *item*.
- **Ids are computed at collection time** from the values (`pytest_make_parametrize_id`
  is the hook). Non-unique ids used to be silently disambiguated with `0`, `1`, …;
  pytest 9.0's `strict_parametrization_ids` makes that an error *(verified: 9.0.0
  changelog, issue #13737)*, which is worth enabling — duplicate ids almost always mean
  a copy-paste bug in the parameter table.
- **`argvalues` is consumed at collection.** A generator works exactly once, so a module
  collected twice (`--doctest-modules`, `pytest.main()` called twice, class-level
  decorators) silently yields zero tests the second time. Deprecated in pytest 9.1
  *(verified: issue #13409)*. Materialize your parameter tables.
- **Indirect parametrization** (`indirect=True`) routes values into a *fixture*'s
  `request.param` instead of into the test argument — which changes the caching key (§3.2)
  and therefore how often the fixture is rebuilt. pytest 9.1.1 fixed a regression where
  overriding a parametrized fixture with an indirect `parametrize` raised "duplicate
  parametrization" *(verified: issue #14591)*, which tells you how intricate that
  interaction is.

**Where parametrization stops being the right tool.** When the parameter table starts
encoding a *rule* — "for all sorted inputs, the output is sorted" — you are hand-rolling a
weak property test. Six hand-picked tuples sample six points of an infinite space, chosen
by the same person who wrote the bug. That is the exact gap §6 exists to close.

pytest 9's **subtests** are the other direction: when the values aren't known until run
time (globbing files, iterating over rows from a fixture), `subtests.test(...)` reports
each iteration separately without needing collection-time knowledge.

---

## 5. Assertion rewriting: an import hook that edits your source

The mechanism is covered in [`42-runtime-code-manipulation.md`](42-runtime-code-manipulation.md)
§6 — a meta-path finder that parses test modules to an AST, rewrites every `assert`
statement into code that binds subexpressions to temporaries and builds a rich failure
message, then compiles and caches the result. Read that first. This section is about
**what that mechanism means for you as a user of pytest**, which is a different and more
practical question.

### 5.1 It is real, and it is verifiable in ten seconds

*(measured)* Same test, same interpreter, two runs:

```
$ pytest -q                                     $ pytest -q --assert=plain
>       assert helper(2) == 5 and sum(xs) == 6  >    assert helper(2) == 5 and sum(xs) == 6
E       assert (4 == 5)                         E    AssertionError
E        +  where 4 = helper(2)
```

The left column tells you the value of `helper(2)`. The right column tells you nothing.
That difference is a compile-time source transformation, and it is the reason `assert`
in pytest feels like a real assertion library while `assert` in `unittest` does not.

Note what it did *not* show: `sum(xs)`. `and` short-circuits, so the right operand was
never evaluated and has no value to report. The rewriter reports what actually ran — a
detail worth knowing before you write compound assertions and wonder why half of them are
invisible.

### 5.2 The cache tag, and why it matters

*(verified, live)*:

```python
>>> from _pytest.assertion.rewrite import PYTEST_TAG, PYC_TAIL
PYTEST_TAG = 'cpython-314-pytest-9.1.1'
PYC_TAIL   = '.cpython-314-pytest-9.1.1.pyc'
```

and on disk after a run *(measured)*:

```
demo/__pycache__/conftest.cpython-314-pytest-9.1.1.pyc
demo/pkg/__pycache__/conftest.cpython-314-pytest-9.1.1.pyc
demo/pkg/sub/__pycache__/test_demo.cpython-314-pytest-9.1.1.pyc
```

The tag embeds **both** the interpreter version and the pytest version, because the
rewritten bytecode is only valid for the pair. Upgrade either and every rewritten `.pyc`
is invalidated wholesale. This is PEP 552's machinery reused, and it is why the rewrite
cost is amortized across runs rather than paid per run.

### 5.3 The three rules you must internalize

**1. Only test modules and registered plugins are rewritten.** Your application code is
not, and neither is a helper module in `tests/` that isn't collected as a test. If you
write shared assertion helpers, register them explicitly:

```python
# conftest.py — at the very top, before importing the helper
pytest.register_assert_rewrite("tests.assertions")
```

`_should_rewrite` and `_early_rewrite_bailout` in `_pytest/assertion/rewrite.py`
*(verified: both exist as methods on `AssertionRewritingHook`)* are what decide this, and
`_early_rewrite_bailout` exists purely to make the common "not a test module" case cheap.

**2. Import order beats registration.** The hook is inserted at `sys.meta_path[0]`
*(verified: `sys.meta_path.insert(0, hook)` in `_pytest/assertion/__init__.py`)*. A module
already in `sys.modules` when the hook is installed **will not be rewritten** — the finder
never sees it. `AssertionRewritingHook` tracks `self._rewritten_names` and warns about
modules imported too early. This is the mechanism behind every "my plugin's asserts print
nothing" bug report.

**3. `PYTEST_DONT_REWRITE` in a module docstring opts out.** Necessary for modules where
the transform interacts badly with other machinery.

### 5.4 The honest cost

pytest's rewriter is arguably the most-used AST transformation in the Python world, and it
is the standing counterexample to "AST manipulation is too clever for production." But
price it correctly:

- it is **permanently coupled to the AST shape of the Python version you run on** — new
  node types, `ast.Constant` unification, positional-only args, `match` statements each
  required work;
- it makes the *first* import of every test module measurably more expensive (amortized by
  the `.pyc` cache — but a cold CI container pays it every run, which is a real and
  frequently-unnoticed CI cost);
- it makes test tracebacks describe code that **does not exist in your source file**,
  which is confusing exactly once and then never again.

That is the standing trade for AST work: enormous leverage, paid for in coupling. Doc 42's
§11 ("when to say no") is the general version; assertion rewriting is the case where the
answer was correctly *yes*.

---

## 6. Property-based testing: the shape of the idea

This is the centerpiece. If you take one thing from Tier 8, take this.

### 6.1 The structural gap in example-based testing

An example test is a claim about **one point** in the input space, chosen by a human. That
human is usually the same person who wrote the code, holding the same mental model,
including the same wrong parts of it. The inputs you think of are, by construction, the
inputs you already had in mind when you wrote the implementation.

The empty list. The negative number. The Unicode combining character. The empty string as
a dictionary key. `nan`. The value exactly at the boundary. A list containing the
separator you used to join it. These are not exotic — they are the *entire* bug population
of most pure functions — and they share the property that **you don't write a test for the
case you didn't think of.**

A property test inverts the responsibility. You state an invariant that must hold for all
inputs of some shape; the library searches for a violation.

```python
from hypothesis import given, strategies as st

@given(st.lists(st.integers()))
def test_sort_is_idempotent(xs):
    assert sorted(sorted(xs)) == sorted(xs)
```

### 6.2 The four property shapes that cover most real uses

Naming these makes them findable, which is the hard part in practice:

| Shape | Form | Examples |
|---|---|---|
| **Round-trip** | `decode(encode(x)) == x` | serialization, parsers, compression, URL encoding, ORM save/load |
| **Oracle / differential** | `fast(x) == slow(x)` | optimized vs reference implementation, new vs old code path, your impl vs the stdlib's |
| **Invariant / metamorphic** | `P(f(x))` holds for all `x`; or `f(g(x)) == h(f(x))` | sorted output is sorted; total is conserved; `len(a+b) == len(a)+len(b)` |
| **Never-crashes** | `f(x)` raises only documented exceptions | the cheapest possible property; finds an astonishing number of `TypeError`s |

Round-trip is the highest-yield shape and the easiest to get right. If your system has any
serialization boundary at all — and it does — that is where to start.

### 6.3 The four measured demonstrations

All run on Hypothesis 6.165.0 with `database=None`, `phases=(generate, shrink)`, a fixed
`@seed`, one shot each *(measured)*:

| Property under test | Calls to the test function | Final counterexample | Wall |
|---|---|---|---|
| `sum(xs) < 1000` over `lists(integers())` | 56 | **`[1000]`** | 0.05 s |
| `len(s.upper()) == len(s)` over `text()` | 113 | **`'ß'`** | 0.06 s |
| `",".join(row).split(",") == row` over `lists(text(), min_size=1)` | 25 | **`[',']`** | 0.01 s |
| `list(set(xs)) == list(dict.fromkeys(xs))` | 37 | **`[1, 0]`** | 0.02 s |

Look at the second row. The property "uppercasing doesn't change length" is one a
competent engineer would assert without hesitation, and it is **false** — `'ß'.upper()` is
`'SS'`. Hypothesis found it in 113 calls and 60 milliseconds, and reported the *single
character* rather than the 40-character random string it originally failed on.

Look at the third row. A naive CSV encoder is broken by a field containing a comma. Every
engineer knows this in the abstract; the test suite for the naive encoder almost never
contains a field with a comma in it, because the person writing the tests was thinking
about names and cities.

That is the whole argument. It took 0.14 seconds total to produce four bugs of a kind that
example-based suites structurally miss.

---

## 7. Inside Hypothesis: choice sequences, generation, shrinking

Understanding the engine changes how you write strategies, how you read failures, and how
much you trust the result. Everything in this section is read from Hypothesis 6.165.0's
installed source *(verified)*.

### 7.1 The core data structure: a typed choice sequence

Hypothesis does not generate values directly. It generates a **sequence of primitive
choices**, and strategies are pure functions from that sequence to values.

```python
# hypothesis/internal/conjecture/choice.py   (verified)
ChoiceT: TypeAlias = int | str | bool | float | bytes
```

Five primitive types. Every strategy — `lists`, `dictionaries`, `from_type`, your
`@composite` — is ultimately a program that consumes `draw_integer`, `draw_boolean`,
`draw_float`, `draw_string`, `draw_bytes` from a **provider**
*(verified: the `PrimitiveProvider` ABC in `internal/conjecture/providers.py` declares
exactly those five methods)*.

Two enormous consequences follow, and they are the reason Hypothesis's design is
interesting rather than merely convenient:

**(a) Shrinking operates on the choice sequence, not on your values.** Make the choices
simpler, re-run the strategy, and you get a simpler value *of the right type, satisfying
all the strategy's constraints, by construction*. You never need a per-type shrinker, and
a shrunk example can never be invalid. This is what DRMacIver calls **integrated
shrinking**, as against QuickCheck's separate `shrink` function — and it is the reason
`st.lists(st.integers(min_value=1))` shrinks to `[1]` rather than to `[0]` or `[]`
depending on how carefully you wrote a shrinker.

**(b) The provider is swappable.** `AVAILABLE_PROVIDERS` *(verified)* ships with
`hypothesis` (the random generator) and `hypothesis-urandom`. Third parties register
others — most notably **CrossHair** (`crosshair-tool` 0.0.109 *(verified on PyPI, with
cp314 wheels)*), which supplies *symbolic* values and uses an SMT solver instead of
randomness. Same tests, different search strategy, selected with `settings(backend=...)`.
That is a genuinely unusual amount of leverage from one abstraction boundary.

### 7.2 The loop

```
                    ┌──────────────────────────────────────────────┐
   settings.phases  │  explicit  →  @example(...) cases first      │
   (default, all 6) │  reuse     →  replay failures from the DB    │  ← §7.4
   verified:        │  generate  →  random search, ≤ max_examples  │
   explicit, reuse, │  target    →  hill-climb on target() metric  │
   generate, target,│  shrink    →  minimize the counterexample    │  ← §7.3
   shrink, explain  │  explain   →  which lines differ pass/fail   │  ← §7.5
                    └──────────────────────────────────────────────┘

    GENERATE                          FAIL                       SHRINK
 ┌───────────────┐              ┌──────────────┐          ┌──────────────────┐
 │ ConjectureData│  choices     │ test raises  │          │ shrink_target =  │
 │  draw_integer │─────────────▶│  → status    │─────────▶│  best known      │
 │  draw_string  │  [7, 'q',    │    INTERESTING│         │  INTERESTING     │
 │  draw_float   │   True, ...] │  + origin    │          │  choice sequence │
 │  …            │              └──────────────┘          └────────┬─────────┘
 └───────┬───────┘                     ▲                           │
         │  strategy                   │  re-run test              ▼
         ▼                             │                  ┌──────────────────┐
   value = [40, -3, 981, …]            └──────────────────│ run a shrink pass│
                                                          │  propose simpler │
   max_examples = 100 (default)         accept if the new │  choice sequence │
   MIN_TEST_CALLS = 10                  sequence is       └────────┬─────────┘
   BUFFER_SIZE = 8192                   shortlex-smaller           │
   (all verified from engine.py)        AND still fails            │
                                                 ▲                 │
                                                 └─────────────────┘
                                  loop until NO pass makes progress
                                  (fixed point), or MAX_SHRINKS = 500,
                                  or MAX_SHRINKING_SECONDS = 300
```

`max_examples=100`, `deadline=200 ms`, `stateful_step_count=50`, and the six-phase tuple
above are the verified defaults of `settings.default` on 6.165.0 *(measured, live)*.
`MAX_SHRINKS = 500`, `MAX_SHRINKING_SECONDS = 300`, `MIN_TEST_CALLS = 10`,
`BUFFER_SIZE = 8192` are verified constants in `internal/conjecture/engine.py`.

### 7.3 Shrinking: the algorithm's *intent*

This is the part worth understanding properly, because it is the feature that converts
random testing from a curiosity into a tool.

**The objective.** From the `Shrinker` docstring *(verified, quoted)*: given a
`ConjectureData` satisfying a predicate (in practice: "the test failed with this
`interesting_origin`"), find one with a **shortlex-smaller choice sequence** that still
satisfies it. The ordering is defined by `sort_key` *(verified, quoted)*:

```python
return (len(nodes), tuple(choice_to_index(node.value, node.constraints) for node in nodes))
```

Shorter first; then, at equal length, lexicographically smaller indices. The docstring
gives the three reasons, and they are worth reading as design rationale rather than
implementation detail: a shorter sequence means fewer decisions were made; a lower index
means a simpler choice; **and early choices are prioritised because they influence more of
the eventual value.**

**The method.** The shrinker keeps a `shrink_target` (current best failing example) and
repeatedly runs **shrink passes** — functions that propose modified sequences. Any proposal
that is shortlex-smaller *and* still fails becomes the new target. The 14 registered passes
*(verified: `self.shrink_passes` in `shrinker.py`)*:

```
try_trivial_spans          node_program("XXXXX")     node_program("XXXX")
node_program("XXX")        node_program("XX")        node_program("X")
pass_to_descendant         reorder_spans             minimize_duplicated_choices
minimize_individual_choices                          redistribute_numeric_pairs
lower_integers_together    lower_duplicated_characters   normalize_unicode_chars
```

Read those names as a catalogue of what shrinking has to be *smart* about:

- `node_program("X"*k)` deletes runs of k choices — the basic "make it shorter" move,
  with an adaptive ladder so deleting long runs is tried before short ones.
- `pass_to_descendant` replaces a value with one of its own sub-parts — how a nested
  structure collapses to its innermost failing piece.
- `redistribute_numeric_pairs` and `lower_integers_together` exist because naïvely lowering
  one integer at a time gets stuck when a test fails only for `a + b > k`: neither `a` nor
  `b` can be reduced alone. Coupled moves are required to escape those local minima.
- `minimize_duplicated_choices` and `lower_duplicated_characters` handle the case where the
  same value appears in several places and must change *together* to stay failing.
- `normalize_unicode_chars` maps a character to a simpler one via case-mapping and NFD/NFKD
  decomposition *(verified: `_natural_simpler_chars`)* — which is why the §6.3 counterexample
  came back as `'ß'` and not `U+1E9E`.

**The correctness discipline.** The docstring states the invariant every pass must satisfy
*(verified, quoted)*: *"whether it makes progress must be deterministic… if you run a
shrink pass, it makes no progress, and then you immediately run it again, it should never
succeed on the second time."* That single rule is what makes `fixate_shrink_passes` able
to terminate: it loops until one full sweep over all passes changes nothing, i.e. a **fixed
point of every pass simultaneously**.

**The complexity discipline** is the other half, and it's the interesting engineering: passes
must not run substantially more test calls on success than on failure. The docstring's own
example is the deletion loop that *doesn't* restart when the sequence changes under it:

```python
i = 0
while i < len(self.shrink_target.nodes):
    if not self.consider_new_nodes(nodes[:i] + nodes[i+1:]):
        i += 1
```

`i` stays valid regardless of what the successful deletion did to the sequence, and total
work stays bounded by the no-op case. There is also an explicit anti-stall heuristic —
`max_failures = 20` consecutive failures before moving to the next pass, plus a `max_stall`
budget that grows adaptively *(verified)* — because a shrink that succeeds early makes all
subsequent work cheaper, so the shrinker actively prioritises *finding* progress over
finding the *best* progress.

**And the honest limitation, stated in the source itself:** greedy shrinking finds a local
minimum, not a global one. `greedy_shrink` "will only ever move to a better target"
*(verified, quoted)*. There exist smaller failing examples that no single pass can reach in
one step. In practice this almost never matters, and when it does, the symptom is a
counterexample that is small but not *minimal* — annoying, not misleading.

> **The transferable idea.** Shrinking is test-case reduction, the same problem C compiler
> fuzzers solve with `creduce` / `delta`. Hypothesis's contribution — the subject of
> MacIver & Donaldson's ECOOP 2020 tool-insights paper *(verified: title, authors and DOI
> 10.4230/LIPIcs.ECOOP.2020.13 confirmed at drops.dagstuhl.de)* — is that reducing the
> **generator's input** rather than the **generated value** makes the reducer generic,
> type-agnostic, and incapable of producing invalid examples. If you ever build a
> generator for anything, steal this.

### 7.4 The example database

The `reuse` phase replays previously-failing examples *first*, before generating anything.
The default is a `DirectoryBasedExampleDatabase` under `.hypothesis/examples`
*(measured, live: that is `settings.default.database`)*. Other implementations ship in
`hypothesis.database` *(verified)*: `InMemoryExampleDatabase`, `ReadOnlyDatabase`,
`MultiplexedDatabase`, `BackgroundWriteDatabase`, `GitHubArtifactDatabase`.

What it buys, measured on a trivial property (`x < 12345` over `integers()`), same code,
two consecutive runs *(measured)*:

| Run | Calls to the test function | Wall |
|---|---|---|
| 1 — cold database | **63** | 0.057 s |
| 2 — warm database | **2** | 0.002 s |

**31× fewer calls, 28× faster.** That is the difference between "the property test is a
slow random search" and "the property test is a fast regression test for every bug it has
ever found." The failing example is stored keyed by a hash of the test function, so it
survives across runs and is retried immediately.

The operational consequence people miss: **`.hypothesis/` is per-machine by default, so CI
gets a cold database on every run**, and a bug found once in CI is not automatically
retried. Fixes, in increasing order of effort: commit the directory (works, ugly);
`MultiplexedDatabase` with a shared read-only backend; `GitHubArtifactDatabase` to pull the
database from a CI artifact; or a Redis-backed database via `hypothesis.extra.redis`
*(verified: `redis` is one of the shipped `hypothesis.extra` modules, alongside
`array_api`, `codemods`, `dateutil`, `django`, `dpcontracts`, `ghostwriter`, `lark`,
`numpy`, `pandas`, `pytestplugin`, `pytz`)*.

Related: `@reproduce_failure(...)` and `settings(print_blob=True)` encode a failing choice
sequence into a decorator you can paste into a bug report. And `@example(...)` pins a
specific case forever — **the correct response to any bug a property test finds is to add
an `@example` for it**, so the regression is deterministic rather than dependent on the
database.

### 7.5 The `explain` phase, and a tool-ID collision worth knowing

The `explain` phase (default-on) reports *which lines* of your code differ between passing
and failing runs. Its implementation is a coverage tracer:
`hypothesis/internal/scrutineer.py` uses `sys.monitoring` on 3.12+ and falls back to
`sys.settrace` *(verified, and see [`32-profiling.md`](32-profiling.md) §4 for why that
matters — PEP 669 with `DISABLE` is 2.1× cheaper than `setprofile` for the same
information)*.

It registers **`MONITORING_TOOL_ID = 3`** *(verified)*, and guards with
`sys.monitoring.get_tool(3) is None`, backing off if another tool holds it. CPython
reserves `DEBUGGER_ID = 0`, `COVERAGE_ID = 1`, `PROFILER_ID = 2` and `OPTIMIZER_ID = 5`
*(measured, live on 3.14.6)*, so 3 is legitimately free — but note the shape of the hazard: **`sys.monitoring` callbacks do not fire for other
monitoring tools' instrumentation**, so running Hypothesis under coverage.py's `sysmon`
core is a case where two tools are instrumenting the same code with different IDs. If you
see `explain` output go missing under coverage, this is why, not a bug in your test.

### 7.6 The rest of the API you will actually use

- **`assume(predicate)`** discards the current example and generates another. It is *not*
  a filter you should lean on: too many rejections trips `HealthCheck.filter_too_much`.
  The rule is **generate valid data, don't filter for it** — reach for `@composite`,
  `.filter()` on a narrow strategy, or `builds()` with constrained arguments before
  reaching for `assume`.
- **`@composite`** builds a strategy from a `draw` function — the way to express
  dependencies between values (a list plus a valid index into it, a date range where end ≥
  start).
- **`target(value, label=...)`** turns the `target` phase into hill-climbing: Hypothesis
  preferentially explores inputs that *increase* your metric. Use it when failure is a
  matter of degree — floating-point error, memory use, queue depth.
- **`st.from_type(T)`** infers a strategy from a type annotation, and `register_type_strategy`
  teaches it about yours. Combined with `hypothesis.extra.ghostwriter` (which emits test
  source for a module), this is the fastest possible on-ramp for a legacy codebase.
- **`@settings(...)`** and `settings.register_profile(...)` / `load_profile(...)` — the
  standard pattern is a `dev` profile with `max_examples=20` and a `ci` profile with
  `max_examples=1000` and `derandomize=False`.
- **`derandomize=True`** makes generation deterministic from the test's identity. It trades
  the ability to find new bugs over time for reproducibility. **Do not set it in CI** — a
  nondeterministic property test that finds a new bug on Tuesday is doing its job, and §13
  is about how to handle that without calling it flakiness.

---

## 8. Stateful testing: properties over sequences of operations

Everything so far tests a *function*. Most real bugs live in *objects with state*, where
the failure requires a specific **sequence** of operations. Hypothesis's
`RuleBasedStateMachine` generates those sequences and shrinks them.

The vocabulary *(verified: all present in `hypothesis.stateful`)* — `RuleBasedStateMachine`,
`rule`, `initialize`, `invariant`, `precondition`, `Bundle`, `consumes`, `multiple`,
`run_state_machine_as_test`, `MultipleResults`:

| Name | Role |
|---|---|
| `@rule(**strategies)` | one operation the machine may perform; args are drawn |
| `@initialize(...)` | runs once, before any rule, in a randomized position among other initializers |
| `@invariant()` | checked after every rule — where most assertions belong |
| `@precondition(lambda self: ...)` | gates a rule on machine state (don't `pop` an empty stack) |
| `Bundle("name")` | a named pool of values produced by rules and consumable by others |
| `consumes(bundle)` | draw from a bundle and *remove* it (modelling "this handle is now closed") |
| `multiple(a, b)` | a rule returning several values into a bundle |

### 8.1 A measured run

A deliberately buggy fixed-capacity cache: it increments a `size` counter on insert but
forgets to decrement it on eviction. `@invariant` asserts `len(cache) == len(cache.d)`.
Settings: `max_examples=100`, `stateful_step_count=20`, `database=None` *(measured)*:

```
stateful: found in 0.09s
AssertionError: size=3 real=2

Failing test case:
state = CacheMachine()
state.size_matches()
state.put(k=0, v=0)
state.size_matches()
state.put(k=1, v=0)
state.size_matches()
state.put(k=2, v=0)
state.teardown()
```

**Three `put` calls, all with `v=0`, keys 0/1/2.** That is the minimal program that
exhibits the bug, and it is *runnable Python* — copy it into a test file and you have a
deterministic regression test. Shrinking here is doing something qualitatively harder than
in §6: it removed rules from the sequence, reordered them, and minimized every argument,
all while preserving the failure.

### 8.2 What this is good for, and what it isn't

**Good for:** caches with eviction, connection pools, state machines with explicit
transitions, parsers with modes, anything with `open`/`close`/`reset` semantics,
data structures where you can write a slow-but-obviously-correct model and assert the fast
implementation agrees with it after every operation. That last pattern — **model-based
testing** — is where stateful testing pays for itself most reliably: your model is a
`dict` or a `list`, your implementation is a B-tree or an LRU cache, and the invariant is
"they agree."

**Not good for:** anything where a single operation is slow (100 examples × 50 steps is
5,000 operations by default — `stateful_step_count=50` *(verified default)*), and anything
where the operations have side effects you cannot cheaply undo. It is also the place where
`HealthCheck.too_slow` will find you.

**The honest caveat:** stateful testing has a much steeper authoring cost than `@given`.
You are writing a *model* of your system, and a wrong model produces false failures that
cost real time. Start with `@given` on pure functions, get value, and reach for stateful
testing on the two or three genuinely stateful components that carry your correctness risk.

---

## 9. What property tests find that example tests structurally cannot

This deserves its own section because the answer is *structural*, not a matter of effort.
Four categories, each with the reason it is structural:

**1. Inputs no human enumerates.** `'ß'`. `nan`. The empty string as a dict key.
A combining character. A string containing your delimiter. The integer exactly at your
boundary. A dict whose keys collide in hash but not equality. Example tests sample points
a human chose; the human's choices are drawn from the same distribution as the human's
mental model of the code, which is the model that contains the bug. *No amount of
discipline fixes this, because the failure mode is the discipline itself.*
Measured: §6.3 found `'ß'` in 113 calls.

**2. Interactions between generators.** `st.lists(st.text())` explores combinations —
empty lists of non-empty strings, long lists of empty strings, lists whose elements share
a prefix. A parametrize table with 8 rows explores 8 combinations. The state space
multiplies; hand-written tables add.

**3. Sequences of operations.** §8. The bug that requires `put`, `put`, `put` with those
particular keys, in that order, is a *program*, and humans write short programs in tests.
Hypothesis writes 20-step programs and then shrinks them to 3.

**4. The minimal reproducer, automatically.** This one is under-appreciated. A random test
that fails on a 400-element list has told you almost nothing — you still have hours of
manual bisection ahead. Shrinking is what converts "there exists a bug" into "here is the
bug." **Randomized testing without shrinking is not a weaker version of property-based
testing; it is a different and much less useful activity.**

### 9.1 What property tests do *not* replace

Be precise here, because the failure mode of enthusiasm is deleting tests you needed:

- **Specific regression tests.** When a bug is found, add `@example(...)` *and* keep an
  ordinary test naming the incident. The property says "this class of bug is gone"; the
  example says "*this* bug is gone" and survives a refactor of the strategy.
- **Integration and contract tests.** Property tests are about your logic, not your
  wiring. They will not tell you the migration didn't run.
- **Anything where the property is as hard to write as the implementation.** If your
  invariant restates the implementation (`assert f(x) == <copy of f's body>`), you have
  written a tautology that will pass forever. This is the property-testing equivalent of a
  mock-only test, and it is the most common way property testing is done badly.

### 9.2 Adoption, concretely

The path that works, in order:

1. Find one **round-trip** in your codebase — a serializer, a parser, an encoder. Write
   `decode(encode(x)) == x`. This takes ten minutes and it usually finds something.
2. Add **never-crashes** properties to your public API boundary functions. Cheapest
   possible property; catches an embarrassing number of `TypeError`s on unusual input.
3. Add an **oracle** wherever you have optimized something: keep the naive implementation
   as a test-only reference and assert agreement. This is also the correct way to test any
   optimization from [`33-optimizing-python.md`](33-optimizing-python.md) — it is the only
   thing that will tell you your fast path is wrong on the cases the slow path handled.
4. Only then reach for stateful testing, and only on the components that carry real risk.

---

## 10. Mutation testing: measuring the instrument

### 10.1 The question coverage cannot answer

Coverage asks: *did this line run?* Mutation testing asks the question you actually care
about: **if this line were wrong, would a test fail?**

The method is simple and brutal. Take the source. Make a small, semantically meaningful
change — `<` becomes `<=`, `+` becomes `-`, a constant becomes `None`, `True` becomes
`False`. That is a **mutant**. Run the test suite. If a test fails, the mutant is
**killed**. If the suite passes, the mutant **survived**, and you have found a defect the
suite cannot see. **Mutation score = killed / total.**

A survived mutant is a *proof by construction* that a specific defect can be introduced
into your code without any test noticing.

### 10.2 The measured result — 100% coverage, 62.5% mutation score

Module under test — two functions, **8 statements and 4 branches** by coverage.py's
counting *(measured)*:

```python
def apply_discount(price, pct):
    """Apply a percentage discount, clamped to [0, 100]."""
    if pct < 0:
        pct = 0
    if pct > 100:
        pct = 100
    return price * (1 - pct / 100)

def is_eligible(age, member):
    return age >= 65 or member
```

**Suite A** — the suite most people write: two tests, `apply_discount(100, 10) == 90.0`
and `is_eligible(70, False) is True`.

**Suite B** — Suite A plus two tests exercising the clamps:
`apply_discount(100, -5) == 100.0` and `apply_discount(100, 250) == 0.0`.

| | Suite A | Suite B |
|---|---|---|
| Line coverage *(measured, `coverage 7.15.2`)* | 75% (2 of 8 statements missed) | **100%** |
| Branch coverage *(measured)* | 67% (2 of 4 branches partial) | **100%** (4/4 branches) |
| Mutants generated *(measured, `mutmut 3.7.0`)* | 16 | 16 |
| Mutants **killed** | 6 | **10** |
| Mutants **survived** | 10 | **6** |
| **Mutation score** | 37.5% | **62.5%** |

**Suite B has perfect line coverage and perfect branch coverage and still fails to detect
six distinct introduced defects.** Here is every one of them *(measured, via `mutmut show`)*:

```diff
-    if pct < 0:            -    if pct < 0:
+    if pct <= 0:           +    if pct < 1:

-    if pct > 100:          -    if pct > 100:
+    if pct >= 100:         +    if pct > 101:

-    return age >= 65 or member    -    return age >= 65 or member
+    return age > 65 or member     +    return age >= 66 or member
```

**Every single survivor is an off-by-one at a boundary.** Not an exotic bug class — *the*
bug class. And the reason is precise and generalizable: the tests chose inputs comfortably
inside each region (`-5`, `250`, `70`) rather than *at* the boundary (`0`, `100`, `65`,
`64`). Coverage cannot possibly see this, because "which line ran" is invariant under
moving an input from `70` to `65`.

Fix the suite by testing `apply_discount(100, 0)`, `apply_discount(100, 100)`,
`is_eligible(65, False)` and `is_eligible(64, False)` and the survivors die. **The mutation
run told you exactly which four tests to write.** That is the practical value: not the
score, but the *list of survivors*, which is a work queue.

### 10.3 How mutmut 3 actually works — the trampoline

mutmut 3.7.0's architecture is worth knowing because it explains both its speed and its
sharp edges *(verified from the installed source)*.

Older mutation tools re-wrote a source file, re-imported everything, and ran the suite —
once per mutant. mutmut 3 instead generates, **once**, a `mutants/` tree in which every
mutated function is expanded into a **trampoline**:

```python
# mutants/pricing/discount.py     (measured — actual generated output)
from mutmut.mutation.trampoline import wrap_in_trampoline as _mutmut_mutated, MutantDict
mutants_x_apply_discount__mutmut: MutantDict = {}
@_mutmut_mutated(mutants_x_apply_discount__mutmut)
def apply_discount(price, pct):
    ...
def x_apply_discount__mutmut_orig(price, pct): ...
def x_apply_discount__mutmut_1(price, pct): ...
def x_apply_discount__mutmut_2(price, pct): ...
...   # 13 variants for this one function
```

Every variant is defined in the same module; the trampoline dispatches to whichever mutant
is selected at run time. Selecting a mutant is therefore a *dictionary lookup*, not a
re-import. mutmut also records, per test, which functions it hit, so it can run **only the
tests that exercise the mutated function** — hence the measured **111–122 mutations/second**
on this tiny project *(measured, two runs)*.

The mutation operators are named functions *(verified: `mutmut/mutation/mutators.py`)*:

```
operator_number   operator_string   operator_name       operator_assignment
operator_augmented_assignment       operator_remove_unary_ops
operator_dict_arguments             operator_arg_removal
operator_symmetric_string_methods_swap
operator_unsymmetrical_string_methods_swap
operator_lambda   operator_keywords operator_swap_op    operator_match
```

with an explicit operator table — `Add↔Subtract`, `Multiply↔Divide`, `LessThan↔LessThanEqual`,
`Equal↔NotEqual`, `And↔Or`, `In↔NotIn`, `Break→Return`, `Continue→Break`, `True↔False`
*(verified)*. Note `operator_name` maps `deepcopy → copy`, which is a wonderfully specific
piece of accumulated field experience.

**The sharp edges I hit, first-hand:**
- mutmut 3 refuses a package literally named `src` — `AssertionError: Failed trampoline
  hit. Module name starts with 'src.'` *(measured)*. Renaming the package fixed it.
- `paths_to_mutate` is deprecated in favour of `source_paths` *(measured: it warns)*.
- The `mutants/` tree left behind can confuse a subsequent coverage run if you point
  coverage at the working tree without cleaning — I hit exactly this and got a nonsense
  25% reading before re-running in a clean copy. **A contaminated measurement that
  reproduces is still contaminated**; see [`31-measurement-methodology.md`](31-measurement-methodology.md).

**cosmic-ray** (8.4.6) is the alternative, with a different architecture (a work-database,
distributed execution) and an explicit operator catalogue whose module names are
self-describing *(verified via the GitHub contents API)*: `binary_operator_replacement`,
`boolean_replacer`, `break_continue`, `comparison_operator_replacement`,
`exception_replacer`, `keyword_replacer`, `no_op`, `number_replacer`, `remove_decorator`,
`unary_operator_replacement`, `variable_inserter`, `variable_replacer`,
`zero_iteration_for_loop`. Pick cosmic-ray when you need distribution or operator control;
pick mutmut when you want a result today.

### 10.4 The costs, honestly

- **Runtime.** Cost ≈ (number of mutants) × (time to run the relevant tests). Mutmut's
  per-test hit tracking makes this far better than N × full-suite, but on a large codebase
  it is still an overnight or weekly job, not a per-commit gate.
- **Equivalent mutants.** Some mutations do not change behaviour at all (`x = x + 0`,
  a mutation inside dead code, a changed constant in a log message). They can never be
  killed, so a 100% mutation score is generally unattainable and chasing it is waste.
  This is the well-known undecidable-in-general problem of the field, and the practical
  answer is to look at the *list* of survivors, not the *score*.
- **It amplifies a slow suite.** If your tests take 10 minutes, mutation testing is
  infeasible until they don't. This is a feature: it is an incentive aligned with §1's
  first job.

**How to actually use it.** Not as a CI gate on a whole repo. Run it on **the module you
are about to change**, before you change it. Ten minutes on one file tells you whether the
tests you are about to rely on can detect anything. That single habit is, in my judgment,
the highest-value testing practice in this document after property-based testing.

---

## 11. Coverage semantics and their limits

### 11.1 The three definitions

| Kind | Question | Cost |
|---|---|---|
| **Statement / line** | did this line execute? | cheap; the default |
| **Branch** | did each conditional take *both* outcomes? | modest; `--branch` |
| **Path** | did each *combination* of branch outcomes execute? | exponential; not offered by any mainstream Python tool |

Line coverage is the weakest and by far the most reported. `if a and b: f()` reaches 100%
line coverage with a single test. Branch coverage requires both outcomes of the `if` but
still does not require exercising both operands of the `and`. Path coverage is what would
be needed to be confident, and it grows as 2^n in the number of branches, which is why
nobody offers it.

### 11.2 Three shapes of bug coverage structurally cannot see

1. **Wrong values on covered lines.** §10's six survivors. `pct < 0` and `pct <= 0` execute
   the same line. Coverage is *blind by construction* to the difference between a correct
   and an incorrect expression.
2. **Missing code.** Coverage measures the lines that exist. The `if amount < 0: raise`
   you never wrote has no line to be uncovered. **This is the largest category of real
   production defects and coverage cannot see any of it.**
3. **Wrong assertions, or none.** A test that calls your function and asserts nothing
   produces exactly the same coverage as a test that checks every postcondition. There are
   very large codebases whose "coverage" is substantially of this form, and the only tool
   that distinguishes them is §10.

Add a fourth for completeness: **coverage aggregated across a suite tells you nothing about
any individual test.** 100% coverage from 500 tests does not mean any single test would
catch any single defect; that is a property of the *intersection*, not the union.

### 11.3 `sys.monitoring` changed the cost, and coverage.py's default

PEP 669 (`sys.monitoring`, 3.12+) gave tools per-code-object event registration and — the
key feature — the ability for a callback to return `sys.monitoring.DISABLE`, meaning
*"never call me for this location again."* A coverage tool marks a line once and then runs
at nearly full speed. That is impossible with `sys.settrace`, where every event costs
forever. See [`32-profiling.md`](32-profiling.md) §4, which measured PEP 669 at **2.66×**
against `setprofile`'s **5.68×** for equivalent information.

coverage.py's adoption timeline *(verified from the coverage.py changelog)*:

- **7.4.0 (2023-12-27)** — experimental `COVERAGE_CORE=sysmon`, opt-in, line coverage only;
  "should be faster for line coverage, but not for branch coverage."
- **7.9.1 (2025-06-13)** — **"On Python 3.14+, the 'sysmon' core is now the default if it's
  supported for your configuration."**
- **7.11.1 (2025-11-07)** — refined the fallback: if sysmon is the *default* but your
  settings conflict (dynamic contexts, `greenlet`/`eventlet`/`gevent` concurrency), it
  silently falls back to `ctrace`; if you *explicitly* asked for sysmon and it conflicts,
  that is now an error.

The gate is visible in the installed source *(verified: `coverage/env.py`)*:

```python
SYSMON_DEFAULT = CPYTHON and PYVERSION >= (3, 14)
branch_right_left = pep669 and (PYVERSION > (3, 14, 0, "alpha", 5, 0))
```

The second line is the reason the default waited for 3.14: `sys.monitoring` could not do
**branch** coverage until CPython grew the `BRANCH_LEFT` / `BRANCH_RIGHT` events (they are
in `sys.monitoring.events` on 3.14.6 *(measured)*, and emitted from `INSTRUMENTED_*` jump
instructions in `Python/bytecodes.c` *(verified)*). Before that, a single `BRANCH` event
could not distinguish the two arms.

### 11.4 The overhead, measured

Tight-loop workload (one small function, 3,000 × 2,000 iterations — a few distinct lines
executed millions of times), `--branch`, alternating passes *(measured)*:

| Core | Pass 1 | Pass 2 | Overhead vs baseline |
|---|---|---|---|
| no coverage | 0.68 s | 0.70 s | 1.00× |
| `COVERAGE_CORE=sysmon` (PEP 669) | 0.73 s | 0.73 s | **≈1.06×** |
| `COVERAGE_CORE=ctrace` (C tracer) | 1.33 s | 1.32 s | **≈1.92×** |
| `COVERAGE_CORE=pytrace` (pure Python) | 3.45 s | 3.46 s | **≈4.98×** |

Both passes agree to within 1.5%, so this is a real effect and not laptop noise (contrast
with §11.5). **Branch coverage went from a ~1.9× tax to a ~1.06× tax.** That is the
difference between "we only measure coverage on a nightly job" and "coverage is always on."

### 11.5 An inconclusive experiment, reported as inconclusive

The number above is the *best case for* `sys.monitoring`, because `DISABLE` pays off in
proportion to how many times a location is re-executed after being marked. So I ran the
opposite shape: a generated module with **20,000 distinct lines, each executed exactly
once**, where `DISABLE` should buy nothing *(measured)*:

| Core | Pass 1 | Pass 2 | Same module, `run()` never called |
|---|---|---|---|
| no coverage | 0.04 s | 0.04 s | 0.06 s |
| `sysmon` | 0.17 s | **0.14 s** | 0.11 s |
| `ctrace` | 0.12 s | 0.12 s | 0.08 s |

The direction reversed — `sysmon` is *slower* here — and that direction is consistent with
the mechanism. **But I am not going to claim it.** The reasons:

- The absolute times are 0.04–0.17 s, against a baseline where **interpreter startup plus
  compiling a 20,000-line module is 0.04–0.06 s**. Most of what I measured is not tracing.
- The "never called" control column shows coverage's *fixed* startup cost differs by core
  (0.11 s sysmon vs 0.08 s ctrace) — so subtracting controls, the actual per-line tracing
  costs are roughly 0.03–0.06 s (sysmon) vs 0.04 s (ctrace). Those intervals **overlap**.
- The two sysmon passes differ by 18% (0.17 vs 0.14), which is far above this machine's
  noise floor for the §11.4 workload (1.5%).

**Verdict: inconclusive.** The mechanism predicts that `sysmon`'s advantage shrinks toward
zero — and possibly past it — on wide-and-shallow code, and this experiment is *consistent*
with that prediction while being far too noisy to establish it. To settle it I would need a
workload with tens of millions of distinct-line executions, which is not a shape real code
has. What I will assert is the narrow, twice-reproduced claim in §11.4 and nothing more.

This is the same discipline as [`16-object-memory-layout.md`](16-object-memory-layout.md)
§8: report the experiment, report the confound, do not round an ambiguous result into a
conclusion because it would make a better sentence.

### 11.6 What to do with coverage in practice

- **Measure branch coverage, not line coverage.** On 3.14 it is nearly free (§11.4).
- **Use it as a *finder*, not a *target*.** The useful output is the uncovered-lines
  report — "nobody has ever run this error handler" is genuinely valuable information. The
  aggregate percentage is not.
- **Never set a coverage percentage gate above roughly where you already are.** Goodhart
  applies with unusual force here: the cheapest way to raise coverage is to write tests
  with no assertions, and a gate rewards exactly that. If you must gate, gate on
  *"coverage did not decrease"* for the diff.
- **`# pragma: no cover` is a real tool**, for `if TYPE_CHECKING:` blocks, `...` protocol
  bodies, and platform-specific branches. Abuse of it is visible in review, which is more
  than can be said for assertion-free tests.

---

## 12. Testing concurrency: probability, not correctness

> **Discussion only.** Nothing in this section was executed while writing it — running
> stress tests was explicitly out of scope for this document, and the surrounding material
> in [`24-the-gil.md`](24-the-gil.md), [`26-free-threading.md`](26-free-threading.md) and
> [`30-concurrency-correctness.md`](30-concurrency-correctness.md) carries the measured
> claims about the runtime itself.

### 12.1 Why a concurrency test is a different kind of object

A sequential test is a *proof over one input*: run it, and either the assertion holds or it
doesn't. A concurrency test is a **sample from a distribution over interleavings**. Passing
means "this interleaving, on this machine, under this load, at this moment, did not
fail." It says nothing about the interleaving that happens in production at 03:00 under a
different scheduler.

So the honest model is: **a concurrency test has a detection probability, not a verdict.**
If a race manifests in 1 interleaving in 10,000 and your test explores one interleaving per
run, a single run detects it with probability 10⁻⁴. Ten thousand runs give you roughly a
63% chance. That is the arithmetic, and it explains everything else in this section —
including why "it passed CI" is nearly worthless evidence for a concurrency fix.

### 12.2 Free-threading raises defect *probability*, not defect *count*

This is the single most important framing, and it is [`24-the-gil.md`](24-the-gil.md) §9's
conclusion applied to testing. The atomicity table there has an identical free-threaded
column: `list.append` is atomic in both builds (per-object locking preserves C-level
indivisibility); `x += 1` and `d[k] += 1` are non-atomic in both. **Removing the GIL does
not create new Python-level race conditions — it widens the window on the ones you already
had**, from "once a month in production" to "immediately."

[`26-free-threading.md`](26-free-threading.md) §5 is titled, correctly, "no new races, much
worse odds." The genuinely new hazards live in **C extensions** (§14 of doc 24, §6 of doc
26), where the GIL really was acting as your module's mutex.

**The testing consequence is a gift, not a threat.** A free-threaded build is a *race
amplifier*. Running your existing concurrency tests on a `cp314t` build under load is one
of the cheapest ways to convert a latent 10⁻⁶ race into a reproducible failure. Treat it
as a fuzzer for concurrency, not as a migration risk. (And assert `sys._is_gil_enabled()`
is `False` in that job — doc 24 §14's cliff, where one non-declaring extension silently
re-enables the GIL and your amplifier quietly stops amplifying.)

### 12.3 The approaches, ranked by what they actually buy

| Approach | What it buys | What it costs / cannot do |
|---|---|---|
| **Design it away** — immutability, message passing, one owner per mutable object, `queue.Queue` instead of shared dicts | eliminates the defect class | design effort; sometimes genuinely impossible |
| **Stress / soak** — run the operation N thousand times across T threads, with jitter | probabilistic detection that scales with N; finds real bugs | slow; nondeterministic; a green run proves nothing; the classic flaky-test factory (§13) |
| **Amplification** — free-threaded build, `sys.setswitchinterval(1e-6)`, artificial `sleep(0)` at suspected windows, oversubscribed thread counts | raises detection probability per unit time by orders of magnitude | still probabilistic; changing the switch interval changes the system you are testing |
| **Deterministic scheduling** — drive the interleaving explicitly with `threading.Event`/`Barrier` so thread A is *guaranteed* to be between statements 2 and 3 when B runs | a real, repeatable regression test for one specific race | tests exactly one interleaving; requires you to already know the race |
| **Model checking** — extract the protocol and check it in TLA+/Alloy, or exhaustively explore small interleavings | genuine coverage of a state space | you are verifying a *model*, not your code; the gap between them is where the bug lives |
| **Sanitizers** — TSan/ASan on the C extension | true data-race detection at the memory level | only for native code; large slowdown; not applicable to Python-level race *conditions* |

**The rung-5 answer to "how do you test concurrent code" is: you mostly don't — you design
so that the dangerous states are unrepresentable, and you use deterministic scheduling to
pin the specific races you have already found.** Stress testing is a *bug-finding* tool,
not a *regression* tool. Once a stress test has found a race, the correct follow-up is a
deterministic test that reproduces it every time, and the stress test moves to a nightly
job where its nondeterminism is a feature rather than a source of red builds.

### 12.4 Practical notes

- **A deterministic race test is written with `Barrier`, not `sleep`.** `time.sleep(0.1)`
  to "let the other thread get there" is a race against the scheduler and the origin of an
  enormous fraction of flaky tests (§13). `threading.Barrier(2)` is a synchronization
  point with a guarantee.
- **Set a hard timeout on concurrency jobs.** A deadlocked test hangs forever. pytest 9's
  `faulthandler_exit_on_timeout` (§2.4) plus `faulthandler_timeout` turns a hung CI job
  into a stack dump of every thread, which is exactly what you need to diagnose it.
  `pytest-timeout` (2.4.0 *(verified)*) is the per-test alternative.
- **`pytest-xdist` (3.8.0) parallelism is not a concurrency test.** It runs tests in
  separate *processes*, which finds *test isolation* bugs (shared temp files, shared
  database rows, port collisions) — a genuinely useful and completely different thing.
- **Hypothesis composes with this**: generate the *operation sequences* with
  `RuleBasedStateMachine` (§8) and execute them concurrently. Bugs found this way shrink,
  which is the only thing that makes concurrency bug reports readable.

---

## 13. Flakiness as a systems problem

### 13.1 The scale, from a primary source

Google's testing blog, *Flaky Tests at Google and How We Mitigate Them* (2016)
*(verified — fetched and quoted)*:

> "Almost 16% of our tests have some level of flakiness associated with them! … about 84%
> of the transitions we observe from pass to fail involve a flaky test."

Sixteen percent, written by very good engineers with very good infrastructure. And the
second number is the operationally devastating one: **when your CI goes red, five times out
of six it is lying.** Once that is true, engineers stop reading red builds, and the suite
has stopped being an instrument. Flakiness does not degrade a suite gradually; it destroys
its precision, and precision is what makes an alarm worth responding to.

### 13.2 The taxonomy — by mechanism, because the mechanism implies the fix

The canonical academic treatment is Luo, Hariri, Eloussi & Marinov, *An Empirical Analysis
of Flaky Tests*, FSE 2014 *(verified: authors, venue and DOI 10.1145/2635868.2635920
confirmed via Crossref)*. **I could not extract the paper's per-category percentages from
the PDF in this session, so I am not quoting them**; the category names below are the
standard ones, and the ordering is my own judgment about Python codebases, not the paper's.

| Cause | Mechanism | Diagnostic | Fix |
|---|---|---|---|
| **Async wait** | `sleep(0.1)` used as synchronization; the machine was slower today | fails under CI load, never locally | poll with a deadline, or use a real synchronization primitive (`Barrier`, `Event`, a queue) |
| **Shared state / test order** | a broad-scoped fixture, a module global, a class attribute, an `lru_cache`, a singleton | passes alone, fails in the suite — or vice versa | shuffle order with `pytest-randomly`; narrow the fixture scope (§3.4); reset explicitly |
| **Hash randomization** | `PYTHONHASHSEED` is random per process, so `set` and pre-3.7-style iteration order varies | fails ~1 run in N with no code change | see §13.3 |
| **Time** | `datetime.now()`, timezone, DST, month boundaries, leap seconds, a test that breaks at midnight UTC | fails at a specific wall-clock time | inject a clock; `freezegun`/`time-machine`; never assert on `now()` |
| **Randomness** | unseeded `random`, `uuid4`, dict ordering assumptions | fails ~1 run in N | seed it — and Hypothesis's `register_random()` *(verified)* tells Hypothesis about your `Random` instance so it manages the seed |
| **Network / external service** | DNS, TLS, rate limits, someone else's flaky staging environment | fails in bursts, correlated across tests | don't do it in the unit suite; contract tests (§14.4) instead |
| **Resource leaks** | file descriptors, ports, threads, temp dirs accumulating across tests | fails only late in a long run; `EMFILE`, `EADDRINUSE` | fixtures with real teardown; `tmp_path`; bind port 0 |
| **GC timing** | a test asserting an object was collected, or a `__del__` that runs at an unpredictable moment or on an unpredictable thread | fails under a different GC schedule or a different Python version | `gc.collect()` explicitly, or don't assert on collection; see [`22-garbage-collection.md`](22-garbage-collection.md) |
| **Parallelism (xdist)** | two workers using the same temp file, port, or database row | fails only with `-n auto` | worker-scoped resources (`worker_id` fixture), unique names |
| **Genuine concurrency bug** | the code is actually racy (§12) | fails rarely and everywhere | it is not a flaky test; it is a flaky *program* |

That last row is the one that matters most and gets misdiagnosed most. **"Flaky test" is
frequently a mislabel for "the test correctly detects a real race that we don't want to
fix."** Quarantining it deletes a true positive.

### 13.3 Hash randomization, concretely

`str` hash randomization has been **on by default since Python 3.3** (it existed as the
opt-in `-R` flag in 3.2.3 first; PEP 456 later replaced the algorithm with SipHash in 3.4).
The seed is chosen per process unless `PYTHONHASHSEED` pins it, so `str` hashes — and
therefore `set` iteration order and small-dict collision order — differ between runs.
*(measured, three runs each on 3.14.6)*:

```
$ PYTHONHASHSEED=1 python -c "print({'alpha','beta','gamma','delta','epsilon'})"
{'epsilon', 'beta', 'delta', 'gamma', 'alpha'}
$ PYTHONHASHSEED=2 …   {'beta', 'delta', 'gamma', 'alpha', 'epsilon'}
$ PYTHONHASHSEED=3 …   {'gamma', 'beta', 'epsilon', 'delta', 'alpha'}
```

Different seed, different order — deterministically so. Any test that compares
`str(a_set)`, iterates a set into a list, or relies on `set` ordering for a snapshot is a
time bomb that goes off on some fraction of CI runs.

**Do not fix this by pinning `PYTHONHASHSEED=0` globally.** That hides the bug and buys you
a test that will fail the moment anything changes. Fix the test — `sorted(...)`, or compare
sets to sets. Pinning the seed is a *reproduction* tool for a failure you are actively
debugging, not a *remedy*. (`pytest-randomly` conveniently prints the seed it used for
both test order and `random`, which is exactly the reproduction handle you want.)

### 13.4 Why "rerun until green" is a bug-hiding machine

`pytest-rerunfailures` (16.4 *(verified)*) exists and is widely deployed. Understand
precisely what it does to your instrument.

Let a test have a true failure probability `p` (the real bug's manifestation rate). With
`k` retries, the probability the suite reports a failure is `p^(k+1)`. With `p = 0.3` and
`k = 2`, you have taken a bug that appeared in 30% of runs and reduced its report rate to
**2.7%** — while changing the code not at all.

You have not fixed anything. You have **turned down the sensitivity of your defect
detector, uniformly, for every defect that test could ever find**, including the ones you
haven't written yet. And because the mechanism is invisible in the green checkmark, nobody
will ever revisit the decision.

Worse: retries also mask *deterministic* failures in the presence of shared state. If test
A poisons state that test B depends on, B's retry may pass because A's damage was
transient — so the retry hides the isolation bug too.

**The policy that works instead:**

1. **Detect.** Track per-test pass/fail history across runs. A test whose outcome ever
   changes without a code change is flaky by definition, and you cannot manage this without
   the data. This is the piece almost everyone skips.
2. **Quarantine, with a name and a date.** Move it out of the blocking suite into a
   non-blocking job. Record who owns it and when it expires.
3. **Give quarantine an expiry.** A quarantined test is deleted (or fixed) within N days —
   a fortnight is a reasonable N. **Quarantine without an expiry is deletion with extra
   steps and a worse conscience.**
4. **Fix the cause, not the symptom**, using §13.2's mechanism column.
5. **Reserve retries for genuinely external, genuinely non-deterministic boundaries** — a
   third-party HTTP call in a smoke test, say — and never for your own logic. And when you
   do use them, log every retry, so the rate is visible on a dashboard rather than hidden
   in a green tick.

> **The framing that makes this decision easy.** Flakiness is a *precision* problem, in the
> §1 sense: the fraction of alarms that are real. Retries improve precision by destroying
> sensitivity. Quarantine improves precision by *explicitly* removing a test from the
> instrument, which is honest and reversible. The difference between the two is entirely
> whether the loss of detection power is visible.

---

## 14. Test design: doubles, contracts, golden tests, and when not to mock

### 14.1 The pyramid, and its critics

The classic pyramid (many unit, fewer integration, fewest end-to-end) is a claim about
**cost and speed**, and on that axis it is still right: fast tests are cheap to run and
cheap to keep green.

The critique — "Write tests. Not too many. Mostly integration." (Kent C. Dodds' *Testing
Trophy*, and independently Google's small/medium/large taxonomy) — is a claim about
**defect distribution**, and on that axis it has a real point: in a well-typed, well-factored
codebase, most surviving defects are in the *seams* between units, and unit tests
structurally cannot see seams.

Both are right about different things, and the synthesis is what actually matters:

- **The shape of your pyramid should follow where your defects actually are.** Look at your
  last 20 production incidents. If they were logic errors, invest in unit + property tests.
  If they were integration errors — wrong config, wrong serialization, wrong version of a
  dependency, a migration that didn't run — no quantity of unit tests will help, and you
  should be buying contract tests and a staging smoke suite.
- **The pyramid's real content is a cost gradient, not a count ratio.** Push each test as
  far down as it can go *while still testing something real*. A test pushed down past that
  point becomes a mock-only test, which is §14.3.
- **Property tests sit at the bottom and change the economics.** One property test replaces
  a large family of unit tests *and* has strictly more detection power. In a suite with
  good property coverage, the "many unit tests" layer can be genuinely small.

### 14.2 Doubles: the vocabulary, precisely

Meyer/Fowler's taxonomy, because using the words correctly makes design arguments shorter:

| Double | What it is | When it earns its place |
|---|---|---|
| **Dummy** | a value passed to satisfy a signature, never used | filling required args |
| **Stub** | returns canned answers; no assertions about calls | you need the collaborator to return something |
| **Spy** | a stub that records calls for later inspection | you need to assert an effect happened |
| **Mock** | pre-programmed with expectations; **fails if called wrongly** | you are testing an interaction protocol |
| **Fake** | a real, working, simplified implementation | in-memory repository, SQLite for Postgres, `tmp_path` for S3 |

**Prefer fakes.** A fake is exercised by every test that uses it, so it stays honest, and
it lets you assert on *state* ("after this, the repository contains one user") rather than
on *interaction* ("`save` was called once with these arguments"). State assertions survive
refactors; interaction assertions do not.

`unittest.mock` gives you `Mock`, `MagicMock`, `patch`, `patch.object`, and — the one that
should be your default — **`autospec=True` / `create_autospec`**, which makes the double
reject calls that don't match the real signature. A bare `Mock()` accepts *any* attribute
and *any* call, so a test using one keeps passing after you rename the method it was
supposed to be checking. That is a test that has silently stopped testing.

### 14.3 `mock.patch`: patch where it's *used*

The mechanism is worked through in
[`42-runtime-code-manipulation.md`](42-runtime-code-manipulation.md) §8, with measured
output. The one-line version, because it is the single most common mocking bug:

```python
# app.py:  from time import time
with mock.patch("time.time", return_value=1.0):
    app.now()      # UNAFFECTED — app.time is a separate binding
with mock.patch("app.time", return_value=42.0):
    app.now()      # 42.0 — correct
```

`from x import y` **copies the reference**; it does not create a live link. So patch the
attribute on the namespace that performs the lookup. If you find yourself unable to state
which namespace that is, that is useful information about the module's coupling.

`monkeypatch` (pytest's fixture) is the same mechanism with automatic, scoped teardown, and
is preferable to `mock.patch` for environment variables (`monkeypatch.setenv`), `sys.path`,
and attribute patching inside a test body.

### 14.4 When *not* to mock

Four rules, each with a reason:

1. **Never mock what you don't own.** A mock of a third-party client encodes *your belief*
   about its behaviour. When the library changes — or when your belief was wrong to begin
   with — the mock keeps agreeing with you and the test keeps passing. Wrap the dependency
   in a thin adapter you *do* own, mock the adapter, and test the adapter against the real
   thing (or a contract test, below).
2. **Never mock the thing under test.** `patch.object(self.thing, "_helper")` means you are
   now testing a chimera that will never exist in production.
3. **Never mock pure logic.** If it's fast and deterministic, call it. A mock here is pure
   cost.
4. **Watch for the mock-count smell.** More than two or three patches in one test says the
   unit has too many collaborators. The test is telling you about a design problem; the
   correct fix is upstream, in the code.

The failure mode all four guard against is the same: **a test that asserts your code calls
the functions you wrote it to call.** It passes forever, survives every refactor by
being rewritten alongside it, and detects nothing. It has coverage. It has no mutation
score. It is the purest form of the §1 confusion between reach and power.

### 14.5 Contract tests

The correct answer to "we mocked the service and the mock was wrong." A contract test is a
shared, executable specification of the interface between a consumer and a provider:

- the **consumer** records the requests it makes and the responses it expects;
- the **provider** runs those expectations against its real implementation in *its* CI.

If the provider breaks the contract, the provider's build goes red — not the consumer's,
three weeks later, in production. Tooling: Pact is the mature cross-language option;
**Schemathesis** (4.24.3 *(verified)*) is the property-based one for HTTP, generating
requests from an OpenAPI schema and checking responses against it. Schemathesis is built on
Hypothesis, which means you get §7's shrinking on API failures — a genuinely underused
combination.

### 14.6 Golden / approval tests

Assert that output matches a checked-in file; when it changes intentionally, review the
diff and re-bless it. Excellent for compilers, formatters, serializers, report generators,
CLI output, generated code — anywhere the output is large, structured, and its correctness
is easier to *recognize* than to *specify*.

Two failure modes, both about the review step:

- **Blessing without reading.** `--update-snapshots` run reflexively converts a golden test
  into a very expensive no-op. The mitigation is a diff small enough to read, which means
  many small golden files rather than one enormous one.
- **Non-determinism baked in.** Timestamps, UUIDs, absolute paths, hash-ordered sets
  (§13.3), floating-point formatting. Every one of these is a §13 flake with extra steps.
  Normalize before writing.

Golden tests pair unusually well with property tests: the property says "output is always
valid JSON"; the golden file says "and it currently looks exactly like *this*."

---

## 15. Fuzzing Python: atheris and where it belongs

**Atheris** (3.1.0 *(verified on PyPI, with `cp312`/`cp313`/`cp314` wheels — no 3.15 wheel
yet as of 2026-08-02)*) is Google's coverage-guided Python fuzzer, built on **libFuzzer**.

```python
import atheris, sys
with atheris.instrument_imports():
    import my_parser

def TestOneInput(data):
    fdp = atheris.FuzzedDataProvider(data)
    try:
        my_parser.parse(fdp.ConsumeUnicodeNoSurrogates(1024))
    except my_parser.ParseError:
        pass          # documented failure — not a bug

atheris.Setup(sys.argv, TestOneInput)
atheris.Fuzz()
```

*(API names — `instrument_imports`, `instrument_all`, `FuzzedDataProvider`, `Setup`,
`Fuzz` — verified against the atheris README.)*

### 15.1 How it differs from Hypothesis

| | Hypothesis | Atheris |
|---|---|---|
| Input | **typed values** from strategies | a **byte string** |
| Guidance | structural, plus optional `target()` hill-climbing | **coverage feedback** from instrumented bytecode |
| Goal | falsify a stated property | find a crash / hang / sanitizer report |
| Output | a shrunk, *typed* counterexample | a crashing input file (libFuzzer minimizes it) |
| Native code | not covered | **covered — with ASan/UBSan** |
| Runtime | seconds, in your test suite | minutes to CPU-days, in a dedicated job |

They are not competitors; they answer different questions. Hypothesis asks "is this
property true?" Atheris asks "can I make this crash?"

### 15.2 Where atheris actually earns its keep

- **Parsers and decoders that consume untrusted bytes.** File formats, network protocols,
  compression, image decoding, deserialization. This is the canonical case and the reason
  the tool exists.
- **C extensions.** This is the strongest argument. Pure-Python code mostly fails safely —
  you get an exception. A C extension fails with a **segfault, a heap overflow, or a silent
  memory corruption**, and those are security bugs. Atheris + AddressSanitizer +
  UndefinedBehaviorSanitizer over your `pybind11`/Cython/PyO3 module is the highest-value
  fuzzing you can do in the Python ecosystem, and it connects directly to
  [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md).
- **`pickle` and any deserialization boundary** —
  [`45-supply-chain-and-security.md`](45-supply-chain-and-security.md) territory.
- **Differential fuzzing.** Feed the same bytes to two implementations and assert
  agreement. This is an oracle property (§6.2) with a coverage-guided generator, and it is
  how a startling number of parser bugs get found.

**Where it does not belong:** business logic. If your function takes a `User` and a
`Decimal`, a random byte string tells you nothing that `@given(st.builds(User), st.decimals())`
doesn't tell you faster and with a readable counterexample.

### 15.3 The middle ground: HypoFuzz

**HypoFuzz** (25.11.1 *(verified on PyPI, supporting 3.10–3.14)*) runs your existing
Hypothesis property tests as a **coverage-guided fuzzing campaign** — hours or days instead
of `max_examples=100` — and writes discoveries back into the Hypothesis example database
(§7.4) so your ordinary test run replays them immediately. Same tests, two budgets: seconds
in CI, hours overnight.

That is, in my judgment, the correct architecture for most teams: **write properties once;
spend seconds on them in CI and hours on them nightly.** It requires no new test code at
all, which is the reason it is worth mentioning in a strategy document.

---

## 16. Judgment: what a good suite optimizes for, and when to delete a test

### 16.1 The properties of a good suite

Ordered by how often they are the binding constraint in real teams:

1. **Fast.** Sub-minute for the inner loop. Below about ten seconds, people run it
   constantly and it changes how they work; above about ten minutes, it moves to CI and
   stops being feedback at all. This is a *behavioural* threshold, not an aesthetic one.
2. **Deterministic.** §13. A suite that lies 16% of the time is not an instrument.
3. **Localizing.** A failure names the broken thing. Two hundred failures from one bug is
   an over-coupled suite; one failure from a bug in an untested area is a suite with a hole.
4. **Powerful.** It can actually detect defects — §10 is how you find out, and it is the
   property nobody measures.
5. **Refactor-safe.** It tests behaviour through interfaces you intend to keep. This is
   almost entirely a function of §14: state assertions over interaction assertions, fakes
   over mocks.
6. **Cheap to read.** A failing test whose failure message requires archaeology costs more
   than the bug. §5 is why pytest's assertion rewriting matters, and §7.3 is why shrinking
   matters — both exist to make failures *legible*.

Note what is not on the list: coverage percentage, test count, and "every function has a
test."

### 16.2 The concrete criteria for deleting a test

Deleting tests is a normal, healthy engineering activity that almost no team does, because
the social cost of deleting a test is high and the cost of keeping one is diffuse. So make
it a checklist. **Delete a test when any of these is true:**

1. **It asserts nothing** (or only that no exception was raised, when the code path can't
   raise). It contributes coverage and zero detection power. §11.2's third shape.
2. **It only asserts that your code calls your code.** The mock-only test of §14.4. If
   every assertion in the test is `mock.assert_called_with`, it will pass for as long as
   the implementation and the test are edited together — i.e. forever.
3. **It is strictly subsumed by a property test.** Once `@given` covers the general rule,
   the six hand-written examples that were sampling that rule are redundant. **Keep exactly
   the ones that correspond to real past incidents**, converted to `@example` (§7.4), and
   delete the rest.
4. **Its failure has never once indicated a real defect, and it fails regularly.** That is
   the definition of a pure false-positive generator. §13's quarantine expiry is the
   process that makes this decision on schedule instead of never.
5. **It tests a behaviour the product no longer has.** Obvious, and yet.
6. **It duplicates another test with a different name.** Cheap to find with mutation
   testing: two tests that kill exactly the same mutant set are, for detection purposes,
   one test.
7. **Its cost exceeds its value.** A 90-second end-to-end test that has caught one bug in
   two years, running on every commit, is a bad trade. Move it to a nightly job — that is
   not deletion, it is repricing, and it is often the right answer.

**Do not delete a test because it is failing.** That is the one case where the correct move
is to understand it first. Rule 4 requires a *history* of false positives, not a single
inconvenient red.

### 16.3 A defensible default stack, as of Aug 2026

Nothing here is exotic; the point is that each piece has a *reason*, traceable to a section
above.

```toml
# pyproject.toml — pytest 9 native TOML (§2.4)
[tool.pytest]
minversion = "9.0"
addopts = ["-ra", "--strict-markers", "--strict-config"]
testpaths = ["tests"]
xfail_strict = true
filterwarnings = ["error"]        # a new DeprecationWarning is a real signal
```

- **pytest 9.x** with `strict_markers` + `strict_config` (a typo'd marker should be an
  error, not a silent no-op) and `filterwarnings = error` (§2.4's deprecation cascade is
  much cheaper handled early).
- **coverage 7.x with `--branch`**, on 3.14 defaulting to the `sysmon` core, at ~1.06×
  (§11.4). Reported, not gated (§11.6).
- **Hypothesis** for anything with a round-trip, an oracle, or an invariant (§6.2), with a
  `ci` profile at higher `max_examples`, a shared example database (§7.4), and `@example`
  for every bug it has ever found.
- **mutmut**, run on the module you are about to change, not as a repo-wide gate (§10.4).
- **`pytest-randomly`** to surface order dependencies (§13.2), because finding them
  deliberately beats finding them on a random Tuesday.
- **`pytest-xdist`** for wall-clock, and as a free test-isolation check (§12.4).
- **A free-threaded (`cp314t`) job** running the concurrency-relevant subset, as a race
  amplifier (§12.2), asserting `sys._is_gil_enabled() is False`.
- **Atheris** on parsers and every C extension you ship (§15.2).
- **Zero retries in the blocking suite** (§13.4), plus per-test flakiness history and a
  quarantine list with expiry dates.

### 16.4 The one-paragraph version

Coverage tells you where your tests have *been*. Mutation testing tells you what they can
*detect*. Property testing expands where they *go*, and shrinking makes what they find
*readable*. Flakiness destroys whether anyone *believes* them. Every other decision in this
document is downstream of those five sentences, and the reason the industry's testing
conversation is stuck on the first one is that it is the only one that is free to compute.

---

## 17. Lab exercises

Reading this document leaves you at **rung 3** of the ladder in
[`README.md`](README.md) §14 — fluent, and one "why?" from collapse. These are the
cheapest available moves to rung 4. All are deliberately small: none should take more than
a few minutes of machine time, and none needs a heavy test run.

1. **See the lifecycle, don't recall it.** Take any project with tests. Run
   `pytest --collect-only -q`, then `pytest --setup-plan`, then `pytest --setup-show -x`.
   Write down: how many items were collected per test *function*, when each fixture was
   constructed, and where teardown happened relative to the next test.
   *Proves you can read pytest's execution model off the tool instead of guessing it (§2, §3).*

2. **Break the rewriter, then fix it.** Write a shared assertion helper in a non-test
   module, use it in a test, and observe that the failure output is bare. Add
   `pytest.register_assert_rewrite("your.module")` at the top of `conftest.py` and observe
   the rich output return. Then import the helper *before* the register call and watch it
   go bare again.
   *Proves you understand that the hook is a `sys.meta_path` finder and import order beats
   registration (§5.3, doc 42 §6).*

3. **Find a real bug in your own code in ten minutes.** Pick the most obvious round-trip in
   your codebase — a serializer, a config parser, an encoder. Write
   `@given(...) def test_roundtrip(x): assert decode(encode(x)) == x`. Run it once.
   *Proves §6's central claim, or disproves it for your codebase — either outcome is worth
   knowing, and the second is rarer than you expect.*

4. **Watch the shrinker work.** Take the failing property from lab 3 (or `len(s.upper()) ==
   len(s)`), run it with `settings(verbosity=Verbosity.verbose)`, and watch the examples
   get simpler. Then set `phases=(Phase.generate,)` to disable shrinking and look at the raw
   counterexample you'd have had to debug.
   *Proves why shrinking, not generation, is the feature that makes property testing usable
   (§7.3).*

5. **Measure your own instrument.** Pick the smallest module in your codebase that has real
   logic and real tests. Run `coverage run --branch` and record the number. Then run mutmut
   on that one module. Compare the two numbers, and read the list of survivors.
   *Proves §10's core claim on your code, and produces a concrete list of tests to write.
   This is the single highest-value lab in the document.*

6. **Reproduce a hash-order flake on purpose.** Write a test asserting
   `list({'alpha','beta','gamma','delta','epsilon'}) == [...]` with some fixed order. Run it
   ten times. Then run it ten times under `PYTHONHASHSEED=0`.
   *Proves §13.3's mechanism, and the difference between pinning the seed (hiding) and
   sorting (fixing).*

7. **Model a stateful component.** Take a cache, a pool, or a small state machine you own.
   Write a `RuleBasedStateMachine` with 3–4 `@rule`s and one `@invariant` that compares it
   against a trivially-correct dict-or-list model. Cap `max_examples=50`.
   *Proves §8, and if it finds nothing, you have learned something real about that
   component's quality.*

8. **Price your retries.** Find every `flaky`/`rerun` marker in your repo. For each, compute
   `p^(k+1)` for a plausible `p` and the configured `k`, and write down the sensitivity you
   gave up. Then check whether any of those tests has ever caught a real defect.
   *Proves §13.4 numerically, on your own suite, which is the only place the argument
   actually lands.*

---

## 18. Question bank

Staff level. If you cannot answer from your own model, the section to reread is noted.

1. pytest is "a pluggy application." What does that buy, and name the four `hookimpl`
   modifiers and what `firstresult` does on the spec side. *(§2.1)*
2. A test fails only when the whole suite runs, never alone. Give four distinct mechanisms,
   and the single command you'd run first for each. *(§3.4, §13.2)*
3. Three `conftest.py` files exist on the path to a test module, all defining a fixture
   `db`. Which wins, and in what order do their `pytest_collection_modifyitems` hooks run?
   Why are those two answers "opposite"? *(§2.3)*
4. Why does a fixture that raises produce identical errors for every dependent test instead
   of retrying? Answer from the data structure. *(§3.2)*
5. Your `parametrize` table is a generator expression and half your tests silently vanish
   in one CI job. Explain, and say what pytest 9.1 did about it. *(§4)*
6. Assertion rewriting: what is `sys.meta_path[0]`, why does the cached `.pyc` tag contain
   *two* version numbers, and why does your plugin's `assert` sometimes print nothing?
   *(§5, doc 42 §6)*
7. Name one thing pytest's assertion rewriter costs you that a simpler design would not.
   *(§5.4)*
8. What does a property test find that an example test *structurally* cannot? Give four
   categories, each with the reason it is structural rather than a matter of effort.
   *(§9)*
9. Hypothesis shrinks the *choice sequence*, not the value. Give two consequences of that
   design that a value-shrinking design (classic QuickCheck) does not get. *(§7.1)*
10. Define the shrinker's ordering. Why is it `(length, then lexicographic)` rather than
    just length, and why are *early* choices prioritised? *(§7.3)*
11. Why must a shrink pass's *progress* be deterministic even though its *specific*
    progress may be random? What breaks without that rule? *(§7.3)*
12. A test fails only when `a + b > 1000`. Why does lowering integers one at a time get
    stuck, and which named shrink pass exists to handle it? *(§7.3)*
13. Your property test found a bug in CI on Tuesday and passed on Wednesday with no code
    change. Is that flakiness? What do you do? *(§7.4, §7.6, §13)*
14. What does `@given` + `assume` do that you should usually do differently, and why?
    *(§7.6)*
15. Design a stateful test for an LRU cache. What is the model, what are the rules, what is
    the invariant, and what does `consumes()` buy you? *(§8)*
16. A module has 100% line coverage and 100% branch coverage. Give three shapes of bug that
    are still invisible, and one measured example. *(§10.2, §11.2)*
17. What exactly does a *surviving* mutant prove? What does a *killed* one prove? What does
    the aggregate score fail to tell you? *(§10.1, §10.4)*
18. Explain mutmut 3's trampoline architecture and why it makes mutation testing fast enough
    to run per-module. *(§10.3)*
19. What is an equivalent mutant, why can't a tool eliminate them in general, and what does
    that imply about targeting a mutation score? *(§10.4)*
20. `sys.monitoring` made coverage cheap. What is the specific feature that does it, and why
    did coverage.py wait until CPython 3.14 to make it the default? *(§11.3, doc 32 §4)*
21. You measure a 1.06× coverage overhead on one workload and 1.4× on another. Before
    reporting either, what must you check? *(§11.4, §11.5, doc 31)*
22. Free-threading and your test suite: does it create new race conditions in Python code?
    Justify from the atomicity table, and say what it *does* change for testing. *(§12.2,
    doc 24 §9, doc 26 §5)*
23. Write a test that reliably reproduces a specific two-thread interleaving. What primitive
    do you use, and why is `sleep` wrong? *(§12.4)*
24. A stress test found a race. What is the correct follow-up, and why does the stress test
    not become the regression test? *(§12.3)*
25. With `p = 0.3` and two retries, what fraction of real failures does CI report? What have
    you actually traded away? *(§13.4)*
26. A test fails one run in eight with no code change and passes under `PYTHONHASHSEED=0`.
    Diagnose it, and say why pinning the seed is the wrong fix. *(§13.3)*
27. Give a quarantine policy that does not turn into permanent deletion, and say what makes
    the expiry enforceable. *(§13.4)*
28. Stub vs mock vs fake vs spy. For an in-memory user repository, which are you building
    and why does it matter for refactor safety? *(§14.2)*
29. `from time import time` in the module under test. `mock.patch("time.time")` does
    nothing. Explain from the import mechanism. *(§14.3, doc 42 §8)*
30. Give three rules for when *not* to mock, each with the failure mode it prevents. *(§14.4)*
31. When is a contract test the right answer instead of a mock, and what does Schemathesis
    add on top of a plain contract test? *(§14.5)*
32. Atheris vs Hypothesis: what question does each answer, and where is atheris the only
    option? *(§15.1, §15.2)*
33. Give five concrete criteria for deleting a test, and one situation where a failing test
    must *not* be deleted. *(§16.2)*
34. You inherit a suite: 92% coverage, 40-minute runtime, 6% of runs red for no reason. Rank
    your first three interventions and justify the ordering. *(§13, §16.1)*

---

## 19. Sources

Grouped, with a verdict per source. Versions and claims were checked on 2026-08-02; the
fast-moving ones say so.

**pytest — primary**
- [docs.pytest.org](https://docs.pytest.org/) — the reference. **Verdict:** start at
  *How-to guides → Writing hook functions* and *Reference → Fixtures*; the rest is
  lookup, not reading. The hook reference is the single highest-value page.
- [pytest changelog](https://docs.pytest.org/en/stable/changelog.html) and the
  [GitHub releases](https://github.com/pytest-dev/pytest/releases) — **verdict: read the
  9.0.0 entry in full** if you are on 8.x. Subtests, native TOML config, `strict` mode, the
  overlapping-argument semantics change and the `PytestRemovedIn9Warning`→error flip are
  all there, and all of them will affect you.
- Installed source: `_pytest/hookspec.py` (51 hooks, 19 `firstresult`),
  `_pytest/fixtures.py` (`cached_result`, `cache_key`), `_pytest/scope.py` (the ordered
  `Scope` enum), `_pytest/assertion/rewrite.py` (`AssertionRewritingHook`, `PYTEST_TAG`),
  `_pytest/config/__init__.py` (`pytest11`, conftest resolution, `confcutdir`).
  **Verdict:** these five files are the whole architecture; an afternoon in them is worth
  more than any tutorial.
- [pluggy docs](https://pluggy.readthedocs.io/) — **verdict:** short and worth reading
  end-to-end once. The call-ordering and wrapper semantics are the part people get wrong.

**Property-based testing — primary**
- [Hypothesis documentation](https://hypothesis.readthedocs.io/) — **verdict:** the
  *Strategies Reference* and the *stateful testing* page are the two you will return to.
  Note the project ships several releases a week, so pin what you read against
  `hypothesis.__version__`.
- Installed source: `hypothesis/internal/conjecture/shrinker.py`. **Verdict: the best-written
  algorithm docstring in the Python ecosystem.** Read the `Shrinker` class docstring and
  `sort_key` before reading anything *about* shrinking. `choice.py` (`ChoiceT`),
  `providers.py` (`PrimitiveProvider`), `engine.py` (`MAX_SHRINKS`, `MIN_TEST_CALLS`) are
  the supporting cast.
- MacIver & Donaldson, *Test-Case Reduction via Test-Case Generation: Insights from the
  Hypothesis Reducer*, ECOOP 2020 —
  [doi:10.4230/LIPIcs.ECOOP.2020.13](https://doi.org/10.4230/LIPIcs.ECOOP.2020.13)
  *(title, authors and DOI verified at drops.dagstuhl.de)*. **Verdict:** the academic
  statement of §7.3. Read it if you ever build a generator for anything.
- [hypothesis.works articles](https://hypothesis.works/articles/) — DRMacIver on
  integrated vs compositional shrinking. **Verdict:** the design rationale in blog form;
  faster than the paper, less precise.
- [HypoFuzz](https://hypofuzz.com/) — **verdict:** the right way to spend a nightly CPU
  budget if you already have property tests.
- [Schemathesis](https://schemathesis.readthedocs.io/) — **verdict:** property-based API
  testing from an OpenAPI schema; the cheapest possible on-ramp for an HTTP service.

**Mutation testing**
- [mutmut](https://mutmut.readthedocs.io/) and its source (`mutmut/mutation/mutators.py`,
  `trampoline.py`). **Verdict:** the tool to reach for first; the operator table is short
  enough to read and tells you exactly what it will and won't test. Note the 3.x rewrite
  changed the architecture (libcst + trampolines) and the config key
  (`paths_to_mutate` → `source_paths`).
- [cosmic-ray](https://cosmic-ray.readthedocs.io/) — **verdict:** choose it for
  distributed runs or fine-grained operator control; heavier setup.

**Coverage**
- [coverage.py docs](https://coverage.readthedocs.io/) and the
  [change history](https://coverage.readthedocs.io/en/latest/changes.html) — **verdict:**
  the changelog is the authoritative record of the `sys.monitoring` migration; 7.4.0,
  7.9.1 and 7.11.1 are the three entries that matter, and 7.9.1 is the one that changed
  your default.
- [PEP 669 — Low impact monitoring for CPython](https://peps.python.org/pep-0669/) —
  **verdict:** read the `DISABLE` semantics; that one feature is the whole performance
  story. Cross-read with [`32-profiling.md`](32-profiling.md) §4 for the measured numbers.
- Ned Batchelder's blog and talks on coverage internals — **verdict:** the most honest
  running commentary on why coverage measurement is harder than it looks.

**Flakiness**
- [Flaky Tests at Google and How We Mitigate Them](https://testing.googleblog.com/2016/05/flaky-tests-at-google-and-how-we.html)
  (2016) — **verdict: read it, it is four pages.** Source of the 16% / 84% figures quoted
  in §13.1, which are the numbers that make the argument for you in a planning meeting.
- Luo, Hariri, Eloussi & Marinov, *An Empirical Analysis of Flaky Tests*, FSE 2014 —
  [doi:10.1145/2635868.2635920](https://doi.org/10.1145/2635868.2635920) *(authors, venue
  and DOI verified via Crossref; **I could not extract the paper's per-category percentages
  in this session and have not quoted them**)*. **Verdict:** the canonical taxonomy; the
  category names are the useful export.
- [`pytest-randomly`](https://pypi.org/project/pytest-randomly/) (4.1.0),
  [`pytest-rerunfailures`](https://pypi.org/project/pytest-rerunfailures/) (16.4),
  [`pytest-timeout`](https://pypi.org/project/pytest-timeout/) (2.4.0),
  [`pytest-xdist`](https://pypi.org/project/pytest-xdist/) (3.8.0). **Verdict:** the first
  finds bugs, the second hides them, the third and fourth are infrastructure.

**Fuzzing**
- [Atheris](https://github.com/google/atheris) — **verdict:** the README is the
  documentation and it is sufficient. The section on sanitizers and native extensions is
  the part that justifies the tool.
- [OSS-Fuzz](https://google.github.io/oss-fuzz/) — **verdict:** if you maintain a widely
  used parser or C extension, this is free continuous fuzzing; the integration cost is a
  day.

**Test design**
- Martin Fowler, [*Mocks Aren't Stubs*](https://martinfowler.com/articles/mocksArentStubs.html)
  and [*Test Double*](https://martinfowler.com/bliki/TestDouble.html) — **verdict:** still
  the clearest statement of the vocabulary in §14.2, and worth re-reading specifically for
  the state-vs-interaction verification distinction.
- Kent C. Dodds, [*The Testing Trophy*](https://kentcdodds.com/blog/the-testing-trophy-and-testing-classifications)
  — **verdict:** the useful half of the anti-pyramid argument; read it as a claim about
  *defect distribution*, not as a rule.
- [Pact](https://docs.pact.io/) — **verdict:** the mature consumer-driven contract testing
  option; heavier than you want unless you have several teams and several services.

**Cross-references in this folder**
- [`42-runtime-code-manipulation.md`](42-runtime-code-manipulation.md) §6 (assertion
  rewriting mechanism), §8 (`mock.patch` patch-where-used, measured).
- [`31-measurement-methodology.md`](31-measurement-methodology.md) — every timing claim
  here obeys its rules; §11.5 is what obeying them looks like when the answer is "no."
- [`32-profiling.md`](32-profiling.md) §4 — `sys.monitoring` vs `setprofile`, measured.
- [`24-the-gil.md`](24-the-gil.md) §9 (the atomicity table), §6 (scheduler interaction).
- [`26-free-threading.md`](26-free-threading.md) §5 (race amplification), §6 (C extensions).
- [`30-concurrency-correctness.md`](30-concurrency-correctness.md) — the correctness theory
  §12 assumes.
- [`16-object-memory-layout.md`](16-object-memory-layout.md) §8 — the model for §11.5's
  honest non-result.

---

*Next: [`44-packaging-and-environments.md`](44-packaging-and-environments.md) — because a
suite that passes on your machine and cannot be installed on the deployment target has
measured the wrong system. Wheels, ABI tags (including the free-threaded and `abi3t` ones),
lockfiles, and reproducibility are what make "it passed CI" mean anything at all.*
