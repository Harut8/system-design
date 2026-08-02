# python-mastery — Roadmap: Bare Metal → OS → CPython

A Staff-Engineer-level path through Python, built downward-first. The premise of this
folder is that **you cannot reason about Python performance, concurrency, or memory
without reasoning about the machine underneath it.** Every question that separates a
senior engineer from a staff engineer — *why did p99 latency triple when we added a
thread?*, *why does RSS grow but `tracemalloc` show nothing?*, *why is free-threaded
Python slower for us?* — resolves one or two layers below Python itself.

So this roadmap runs the full stack: cache lines → memory model → kernel → allocator →
CPython's C internals → bytecode → GC → GIL → asyncio → performance → types →
metaprogramming → production quality.

Companion file: **[BOOKS.md](BOOKS.md)** — the researched reading list (editions,
verdicts, and *when* in this roadmap each book actually pays off).

> **How to use this folder.** Don't read it linearly on day one. Read Tier 0–1 once for
> vocabulary, then live in Tiers 2–6 and come back down whenever something doesn't make
> sense. The rung-3 trap is real here: you can learn to *say* "false sharing" and
> "biased reference counting" in a week and still fail the follow-up question. Every
> tier below has a **"you can answer this"** checklist — that, not page count, is the
> completion criterion.

---

## Table of contents

1. [The one-page picture](#1-the-one-page-picture)
2. [The doc manifest — every topic, numbered](#2-the-doc-manifest--every-topic-numbered)
3. [Tier 0 — bare metal](#tier-0--bare-metal-0005)
4. [Tier 1 — operating system](#tier-1--operating-system-0612)
5. [Tier 2 — CPython as a C program](#tier-2--cpython-as-a-c-program-1317)
6. [Tier 3 — compiler & runtime](#tier-3--compiler--runtime-1823)
7. [Tier 4 — concurrency](#tier-4--concurrency-2430)
8. [Tier 5 — performance engineering](#tier-5--performance-engineering-3135)
9. [Tier 6 — type system & API design](#tier-6--type-system--api-design-3639)
10. [Tier 7 — metaprogramming](#tier-7--metaprogramming-4042)
11. [Tier 8 — engineering quality](#tier-8--engineering-quality-4346)
12. [Tier 9 — capstones](#tier-9--capstones-4749)
13. [Suggested sequence](#13-suggested-sequence)
14. [The competence ladder](#14-the-competence-ladder)
15. [State of the runtime — as of Aug 2026](#15-state-of-the-runtime--as-of-aug-2026)
16. [Traps this roadmap exists to prevent](#16-traps-this-roadmap-exists-to-prevent)

---

## 1. The one-page picture

Read this diagram top-down once. Then read it **bottom-up**, which is the direction
causality actually flows.

```
┌───────────────────────────────────────────────────────────────────────────────┐
│  YOUR PYTHON CODE                                                             │
│  classes · generics · decorators · async def · dataclasses · protocols        │
│                                                     ─── docs 36–42            │
└───────────────────────────────┬───────────────────────────────────────────────┘
                                │  compile()
                                ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  COMPILER FRONTEND                                            ─── doc 18      │
│  tokenizer → PEG parser → AST → symbol table → CFG → codegen                  │
└───────────────────────────────┬───────────────────────────────────────────────┘
                                │  code objects + exception tables
                                ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  BYTECODE + EVAL LOOP                                         ─── docs 19–21  │
│  adaptive specializing interpreter (PEP 659) · inline caches                  │
│  tier-2 micro-ops → optimizer → copy-and-patch JIT (PEP 744/836)              │
└───────────────────────────────┬───────────────────────────────────────────────┘
                                │  every operation is a call on a PyObject*
                                ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  OBJECT MODEL & MEMORY                                        ─── docs 14–16  │
│  PyObject · PyTypeObject / tp_ slots · refcounting · immortal objects         │
│  pymalloc arenas/pools/blocks · interning · key-sharing dicts · __slots__     │
└──────────────┬────────────────────────────────────┬───────────────────────────┘
               │                                    │
               ▼                                    ▼
┌───────────────────────────────┐  ┌────────────────────────────────────────────┐
│  GARBAGE COLLECTION  ─ doc 22 │  │  CONCURRENCY RUNTIME        ─── docs 24–30 │
│  refcount + cycle collector   │  │  GIL · free-threading (PEP 703/779)        │
│  generations · weakrefs       │  │  subinterpreters (PEP 684/734) · asyncio   │
│  finalizers · resurrection    │  │  biased refcounting · per-object locks     │
└──────────────┬────────────────┘  └────────────────┬───────────────────────────┘
               │                                    │
               └────────────────┬───────────────────┘
                                ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  CPYTHON = A C PROGRAM                                        ─── docs 13, 17 │
│  C-API · stable ABI · extension modules (Cython/pybind11/nanobind/PyO3)       │
│  buffer protocol · GIL release in native code                                 │
└───────────────────────────────┬───────────────────────────────────────────────┘
                                │  malloc/mmap · pthreads · syscalls
                                ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  OPERATING SYSTEM                                             ─── docs 06–12  │
│  scheduler (EEVDF) · virtual memory & page faults · allocators (glibc/mimalloc)│
│  syscalls & vDSO · epoll / io_uring · signals · fork semantics · cgroups      │
└───────────────────────────────┬───────────────────────────────────────────────┘
                                │  instructions · loads/stores · interrupts
                                ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  BARE METAL                                                   ─── docs 00–05  │
│  out-of-order superscalar core · branch prediction · µop cache                │
│  L1/L2/L3 · cache lines · false sharing · TLB · NUMA                          │
│  memory model (x86-TSO vs ARM) · atomics · CAS · ABA · hazard pointers        │
└───────────────────────────────────────────────────────────────────────────────┘
```

**The single most important idea in this folder:** a Python-level statement is a
*claim about the layer below it*, and every performance or correctness surprise is that
claim being false. `x += 1` is not atomic because it is four bytecodes. Four bytecodes
are not atomic because the eval loop can switch threads between them. Threads can switch
because the GIL is released on a counter. And even without a GIL it still isn't atomic,
because the load and the store are separate memory operations on a machine whose memory
model permits reordering. Same question, four layers, four answers — you need all four.

---

## 2. The doc manifest — every topic, numbered

This is the concrete build order. Each row is one document to write and then to reread.
Status starts empty; fill it in as you go.

| # | Doc | Core topics | Status |
|---|-----|-------------|--------|
| **T0** | **Bare metal** | | |
| 00 | `00-cpu-execution-model.md` | pipelining, superscalar, out-of-order, branch prediction & misprediction cost, speculation, µops, execution ports, IPC, SMT, inter-core communication, interrupts, context switching | ⬜ |
| 01 | [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) | latency ladder, cache lines, associativity & the power-of-two trap, **MESI coherence**, **false sharing (128 B on Apple Silicon)**, prefetcher limits, TLB reach & 16 KB pages, NUMA, **why CPython's pointer chasing is the real cost** | ✅ **written** |
| 02 | [`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md) | store buffers & invalidate queues, compiler vs hardware reordering, **`volatile` is not a concurrency tool (proved in assembly)**, **x86-TSO vs AArch64**, **SB/MP litmus tests measured on real hardware**, C11 orderings and what each compiles to on both ISAs, CAS, LL/SC & livelock, `weak` vs `strong`, **the atomic cost model & scaling collapse (measured)**, alignment & SIGBUS, data races as UB, SC-DRF, **CPython's `_Py_atomic_*` verified**, **Python has no memory model** | ✅ **written** |
| 03 | [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md) | progress guarantees (wait-/lock-/obstruction-free/blocking), `casal`/`caspal` vs `ldaxr`/`stlxr` **on this machine**, Treiber stack, Michael–Scott queue, **the ABA problem reproduced — forced *and* by accident (measured)**, tagged pointers & DWCAS & pointer-bit-stealing, **reclamation as the real difficulty**: hazard pointers / EBR / QSBR / RCU with a comparison table, **an ASan-caught use-after-free in my own epoch reclaimer**, why lock-free loses to a mutex under contention *(measured)*, backoff & the elimination stack, **when not to write any of it**, CPython's `Python/qsbr.c` · `PyMutex` · two-pause free-threaded GC | ✅ **written** |
| 04 | `04-binary-abi-and-linking.md` | ELF/Mach-O, symbols, relocation, PLT/GOT, static vs dynamic linking, `LD_PRELOAD`, calling conventions, ABI vs API, PIC, symbol visibility | ⬜ |
| 05 | `05-representation.md` | IEEE-754, denormals, FMA, catastrophic cancellation, integer overflow/UB, endianness, alignment & padding, Unicode/UTF-8/normalization | ⬜ |
| **T1** | **Operating system** | | |
| 06 | [`06-processes-threads-scheduling.md`](06-processes-threads-scheduling.md) | one `task_struct` and a `clone()` sharing dial, **the creation ladder measured (thread 25 µs → `fork` 806 µs → `spawn` 25 ms = 1016×)**, **context-switch cost measured: 2.70 µs threads vs 2.88 µs processes — the process/thread gap is only 1.06×, because ASID/PCID killed the TLB flush**, voluntary vs involuntary signatures (**and the counter measurably not meaning "preemption" on this kernel**), CFS `vruntime` & the real nice-weight table (1024, ×1.25/level), **EEVDF's two gates — eligibility *and* virtual deadline — and why shorter slices buy latency without buying CPU**, RT classes & the 5% throttling reserve, affinity's three "how many CPUs" questions, **cgroup v2 `cpu.max`: the low-utilisation/high-latency failure mode and the `nr_throttled` procedure that confirms or eliminates it in 30 s**, **per-thread QoS measured: `BACKGROUND` costs 6–9× and `nice` does nothing — plus a pure-stdlib route to it via `PRIO_DARWIN_THREAD`**, **the GIL out-preempts the OS 16:1 (1,280/s vs an 82/s floor)**, and **the experiment I predicted wrong: one thread on a slow core is starved 8.8× while total throughput falls only 6.6%** | ✅ **written** |
| 07 | [`07-virtual-memory.md`](07-virtual-memory.md) | page tables & the walk (and why the 16 KB granule makes it 3 levels here), the fault path drawn end to end, **minor vs major fault measured: 0.5 µs vs 15.9 µs vs a 12 ns warm access**, **lazy allocation — 1 GiB mapped costs 3 pages of RSS, and one byte costs 16,384**, `mmap` in all its modes, copy-on-write, **the CPython COW catastrophe: a child that only *reads* privatises 88% of its parent's heap**, **`gc.freeze()` measured at 295× on GC traversal and *zero* on refcount writes — the folklore is half right**, overcommit modes, the OOM killer & `oom_score_adj` & cgroup `memory.high`/`max`, RSS vs VSZ vs PSS vs USS (and `Private_Dirty` as *the* diagnostic), THP & the three reasons databases disable it, swap/reclaim/PSI, `madvise`, **arena retention: same 20,000 survivors, 211 MB or 22 MB depending only on placement**, **`tracemalloc` understates RSS by 2.7×**, a 7-rung attribution ladder | ✅ **written** |
| 08 | `08-allocators.md` | `brk` vs `mmap`, glibc malloc (bins, tcache, arenas), jemalloc, **mimalloc**, tcmalloc, fragmentation (internal/external), why freed memory doesn't return to the OS | ⬜ |
| 09 | [`09-syscalls-and-io.md`](09-syscalls-and-io.md) | the `svc`/`syscall` trap path drawn end to end and **the trap floor measured at ~81 ns — 3.3× a userspace clock read**, why `getpid` is a broken benchmark, Spectre/Meltdown's effect on entry cost, the vDSO, blocking vs non-blocking vs completion, `select`→`poll`→`epoll`/`kqueue` with **the C10K curve measured: kqueue flat at 0.29 µs from 10 to 4000 fds (−0.01 ns/fd) while poll goes linear to 594 µs — 2019×**, and `select` raising at `FD_SETSIZE` exactly where the man page says it will; **level vs edge triggering with the never-woken-again bug reproduced** (edge + partial read = permanently stalled connection) and the syscall trade measured both ways; **io_uring in full** — the shared rings, `user_data`, unordered completions, `SQPOLL`'s zero-syscall steady state, registered buffers/files, multishot, and the security history that gets it disabled; the page cache (**cold vs warm measured at 16–17× across four files**), readahead, writeback; **`fsync` vs `F_FULLFSYNC` measured at 118× — a durability *correctness* finding, 340 durable commits/s**, the directory-fsync rule, and **the Postgres fsync-gate walked step by step**; zero-copy (**`os.sendfile` 5.2× over read+send**), `splice`, `MSG_ZEROCOPY`, `O_DIRECT`; **batching as the general fix — 139× throughput between the worst and best buffer size, and a `writev` cost model of 570 ns fixed + 13.5 ns per iovec (42× leverage)**; CPython's `io` layering (**`buffering=4096` with 4 KiB reads does nothing — 8193 syscalls either way**; `DEFAULT_BUFFER_SIZE` is now 131072), and **an asyncio round trip billed at exactly 8.01 syscalls** | ✅ **written** |
| 10 | [`10-signals-fork-exec.md`](10-signals-fork-exec.md) | the C handler that runs **no Python** (`trip_signal` → one eval-breaker bit → main thread only), **which C calls are signal-blind, measured** (`sorted` and `zlib` are; a 14.9 s catastrophic regex is *not* — `sre_lib.h` checks every 4096 steps), async-signal-safety vs Python's weaker "async-*bytecode*-safety" and the reentrancy bugs that survive it, **PEP 475** (interrupted `sleep(1.0)` still takes 1.0031 s), `pthread_sigmask`/`sigwait`/`set_wakeup_fd`, the fork inheritance table, **the classic deadlock reproduced: 26/40 children wedged on a lock nobody holds — and 22/40 free-threaded, because stop-the-world freezes threads without releasing what they own**, the 3.12 warning & the **3.14 `forkserver` default (gh-84559)**, macOS's Objective-C fork check, what survives `exec` (**`SIG_IGN` does, handlers don't — measured**), PEP 446 & `CLOEXEC`, zombies & why a `SIGCHLD` reaper must loop, process groups and who really gets your Ctrl-C | ✅ **written** |
| 11 | `11-ipc-and-shared-memory.md` | pipes, UNIX sockets, FD passing, POSIX shm, `mmap` sharing, **futex**, eventfd, semantics & hazards of shared memory from Python | ⬜ |
| 12 | `12-observing-a-process.md` | `perf`, PMU counters, `ftrace`, **eBPF/bpftrace**, `strace`, `/proc`, flamegraphs, on-CPU vs off-CPU analysis, USE & RED method applied to one process | ⬜ |
| **T2** | **CPython as a C program** | | |
| 13 | `13-cpython-source-map.md` | repo layout, building from source, `--with-pydebug`, configure flags, `Tools/`, the devguide, reading the C sources without drowning | ⬜ |
| 14 | `14-pyobject-and-types.md` | `PyObject`/`PyVarObject`, `PyTypeObject`, `tp_*` slots, number/sequence/mapping protocols, MRO & C3 linearization, static vs heap types | ⬜ |
| 15 | [`15-refcounting-and-ownership.md`](15-refcounting-and-ownership.md) | the trade refcounting makes, new/borrowed/stolen, **ownership by API family + the strong-ref replacements**, the five classic bugs, `Py_CLEAR` & reentrancy, what refcounting can't do, **immortal/deferred/biased**, **the `fork()` tax & `gc.freeze()`**, the three-part cost model | ✅ **written** |
| 16 | [`16-object-memory-layout.md`](16-object-memory-layout.md) | the three headers, **the free-threading +16B tax (measured)**, pymalloc arena→pool→block with real constants, string kinds, list overallocation, compact dicts, `__slots__` as locality, interning, mimalloc, cost-per-million budgeting | ✅ **written** |
| 17 | [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md) | API/ABI tiers, **PEP 803 `abi3t`** & **PEP 793 `PyModExport`**, ownership rules, error handling, heap types, multi-phase init & per-module state, buffer protocol, GIL release **measured**, critical sections, calling conventions, **binding-generator comparison**, `PYTHONMALLOC=debug` + lldb | ✅ **written** |
| **T3** | **Compiler & runtime** | | |
| 18 | `18-lexer-parser-ast.md` | tokenizer, **PEG parser**, AST shapes, symbol table & scope resolution, CFG construction, the `ast` module as a tool | ⬜ |
| 19 | [`19-bytecode-and-code-objects.md`](19-bytecode-and-code-objects.md) | `PyCodeObject` field by field, wordcode & `EXTENDED_ARG`, **inline caches as `CACHE` pseudo-instructions**, reading `adaptive=True` disassembly, localsplus, `co_stacksize` (with a real segfault), **zero-cost exception tables**, `co_linetable`/`co_positions`, PEP 709 comprehension inlining, closures, oparg bit tricks | ✅ **written** |
| 20 | [`20-eval-loop.md`](20-eval-loop.md) | `_PyEval_EvalFrameDefault` as scaffolding, **the `bytecodes.c` DSL and `Tools/cases_generator/`** (one instruction shown as DSL → generated C), switch vs computed gotos vs the **3.14 tail-call interpreter** (and the LLVM-19 baseline scandal), **PEP 659 end to end** — families, the backoff counter **decoded byte-by-byte against the source constants**, guards, deopt & the 53-miss cooldown **measured thrashing**, `_PyInterpreterFrame` on the 16 KB chunked data stack, lazy `PyFrameObject`, **PEP 667** (not 558), **PEP 523's `_CHECK_PEP_523` tax**, vectorcall/`METH_FASTCALL`/inlined Python calls, `RESUME`→`RESUME_CHECK`, generators, **`LOAD_FAST_BORROW` and `optimize_load_fast()`** | ✅ **written** |
| 21 | `21-tier2-and-jit.md` | micro-ops, trace projection, the tier-2 optimizer, **copy-and-patch JIT**, PEP 744 → PEP 836, guards & bailouts, when the JIT helps and when it can't | ⬜ |
| 22 | [`22-garbage-collection.md`](22-garbage-collection.md) | the cycle-detection algorithm walked on a real object graph, GC tracking & tuple untracking, generations, `tp_traverse`/`tp_clear`, PEP 442 finalizers & **resurrection**, weakref callback ordering, **the incremental-GC saga (dated, sourced)**, free-threaded stop-the-world, `gc.freeze()` before fork, **GC as cache hostility** | ✅ **written** |
| 23 | `23-tracing-and-runtime-hooks.md` | `sys.settrace`/`setprofile`, **PEP 669 monitoring**, PEP 578 audit hooks, PEP 768 remote debugging & `sys.remote_exec`, sampling vs deterministic profilers | ⬜ |
| **T4** | **Concurrency** | | |
| 24 | [`24-the-gil.md`](24-the-gil.md) | cache-coherence origin of the GIL, `ceval_gil.c` internals, `gil_drop_request`/eval breaker, futex & scheduler interaction, the old GIL & the GIL battle, what releases the GIL, signals/`Ctrl-C`/fork, the convoy effect *measured*, per-interpreter GIL, the Gilectomy (Hastings), PEP 703's five-tier answer, free-threaded C-extension contract, `py-spy`-based diagnosis | ✅ **written** |
| 25 | `25-threads-and-synchronization.md` | `threading`, Lock/RLock/Condition/Event/Barrier/Semaphore, `queue`, thread-locals, **which operations are and are not atomic**, lock granularity | ⬜ |
| 26 | [`26-free-threading.md`](26-free-threading.md) | **PEP 703/779/803**, getting & detecting the build, **the +8.1% single-thread tax measured on this machine**, the memory tax, race amplification, **the sharing wall (shared dict scales 0.32×)**, the extension GIL-cliff, stop-the-world GC, **16-package ecosystem audit**, a decision framework | ✅ **written** |
| 27 | `27-multiprocessing-and-subinterpreters.md` | fork/spawn/forkserver, pickling costs, `shared_memory`, COW and refcount write-amplification, **PEP 684 per-interpreter GIL**, **PEP 734 `concurrent.interpreters`** | ⬜ |
| 28 | [`28-asyncio-internals.md`](28-asyncio-internals.md) | `async def` as one `co_flags` bit, `await` disassembled (`GET_AWAITABLE`/`SEND`/`END_SEND`), **await-chain cost measured: +1 `PY_RESUME` and +1 `PY_YIELD` per nesting level, per suspension**, one loop iteration exactly, the ready deque & timer heap, kqueue/epoll & the self-pipe, `Future`/`Task`, cancellation & `uncancel()`, `TaskGroup`/`ExceptionGroup`, eager tasks, uvloop's architectural edge | ✅ **written** |
| 29 | [`29-async-patterns-and-pitfalls.md`](29-async-patterns-and-pitfalls.md) | the five production failure modes; **backpressure measured — unbounded vs bounded queue moves the same items in the same wall time, but p99 goes 1127 ms → 3.9 ms (Little's Law confirmed to <1%)**; **when a fire-and-forget task is *actually* collected** (only when its waker is unreachable — the folklore rule is too strong); **`gather` leaves orphans measured: sibling ran 10 more ticks after the caller gave up**, `TaskGroup` doesn't; **cancellation is edge-triggered — asyncio 0/3 later checkpoints raise, trio 3/3**; `shield` as deliberate orphaning; `uncancel()`/`cancelling()`; timeouts (**~1.2 ms floor**) & deadline propagation; **a loop-lag monitor: 3/3 blocks caught, overhead below the noise floor**; sync↔async bridges & the loop-thread deadlock; executors (**`to_thread` 22.3 µs vs 12.0 µs, pool = `min(32, process_cpu_count()+4)`**); contextvars (**copy is O(1) — flat at 0/10/100 vars**); graceful shutdown; anyio/trio | ✅ **written** |
| 30 | [`30-concurrency-correctness.md`](30-concurrency-correctness.md) | data race vs race condition (**pure Python can't have the former**), **the same bug measured across 5 interpreters: 42.7% lost on 3.9, 0% on 3.11–3.14, 57.4% free-threaded**, **the 22 bytecodes where a thread may switch** (and PR #18334, which closed the window by accident), the measured atomicity table, check-then-act, Python's absent memory model, deadlock/livelock/starvation with production signatures, **lock convoying (0.37× at 16 threads)**, **starvation proved to be a GIL artifact, not a lock one**, priority inversion, lock ordering (`RankedLock`), cooperative vs preemptive (**p99 306 ms vs 7.6 ms**), clock drift, false sharing & NUMA, **wait-freedom in depth** — helping, Herlihy's consensus hierarchy, Kogan–Petrank, and **the free-threaded `Py_INCREF` classified wait-free on all three paths** (and only lock-free on pre-LSE ARM), transactional memory (**no `FEAT_TME` on this CPU**), **WebAssembly — why `_CHECK_PERIODIC` polls for Ctrl-C, PEP 776, and the main thread where `Atomics.wait` is illegal so blocking synchronization cannot exist**, work-stealing (**`ThreadPoolExecutor` has none**), scheduler fuzzing & free-threaded CI | ✅ **written** |
| **T5** | **Performance engineering** | | |
| 31 | [`31-measurement-methodology.md`](31-measurement-methodology.md) | clock resolution, **the hostile laptop measured (P/E cores)**, the noise catalogue, **this machine's noise floor**, `timeit` traps, `pyperf` properly, statistics that matter, microbenchmark lies, **"the experiment that fooled me"**, production A/B, a decision framework, house rules | ✅ **written** |
| 32 | [`32-profiling.md`](32-profiling.md) | **the 5.09× distortion measured** (profilers reorder, not just inflate), sampling vs deterministic, **`sys.monitoring` vs `setprofile` measured**, tool inventory, the `_PyEval_EvalFrameDefault` non-answer, memory's four questions, **off-CPU analysis**, production profiling, the workflow | ✅ **written** |
| 33 | `33-optimizing-python.md` | algorithmic first, data layout, allocation reduction, attribute-lookup cost, comprehension vs loop, interning, batching across the C boundary | ⬜ |
| 34 | `34-going-native.md` | NumPy internals (strides, views, dtypes), BLAS, Numba, Cython, Rust/PyO3, SIMD, Arrow & zero-copy, releasing the GIL for real parallelism | ⬜ |
| 35 | `35-memory-optimization.md` | measuring RSS truthfully, leak vs fragmentation vs cache, COW-friendly forking, zero-copy, mmap, memory-mapped files, object-graph analysis, arena return behaviour, memory budgets per container | ⬜ |
| **T6** | **Type system & API design** | | |
| 36 | `36-type-system-foundations.md` | gradual typing, nominal vs structural, **variance** (co/contra/invariant), `Any`/`Never`/`Unknown`, narrowing & flow analysis, deliberate soundness gaps | ⬜ |
| 37 | [`37-generics-and-protocols.md`](37-generics-and-protocols.md) | variance from first principles, **PEP 695** syntax & inferred variance, bounds vs constraints, **696** defaults, **612** ParamSpec, **646** TypeVarTuple, Protocols & **`runtime_checkable`'s broken promise**, `Self`, overloads, TypedDict/Literal/Annotated, `@dataclass_transform`, **PEP 649 lazy annotations**, erasure, **honest unsoundness**, **measured mypy/pyright/ty/pyrefly divergence** | ✅ **written** |
| 38 | `38-type-checking-in-practice.md` | mypy vs pyright vs the Rust-based checkers (ty, pyrefly), strictness ladder, stubs & typeshed, runtime validation vs static typing, rolling types into a large codebase | ⬜ |
| 39 | `39-api-and-abstraction-design.md` | ABC vs Protocol, dependency inversion, immutability, dataclasses/attrs/pydantic trade-offs, error design, deprecation & backwards compatibility | ⬜ |
| **T7** | **Metaprogramming** | | |
| 40 | `40-data-model-and-descriptors.md` | dunder protocols, **descriptors**, `property`, attribute lookup order, `__getattr__` vs `__getattribute__`, `__set_name__`, MRO in anger | ⬜ |
| 41 | `41-metaclasses-and-class-construction.md` | `type()`, `__init_subclass__`, metaclasses, `__class_getitem__`, `abc` machinery, **and when not to use any of it** | ⬜ |
| 42 | [`42-runtime-code-manipulation.md`](42-runtime-code-manipulation.md) | closures & cells, `functools.wraps`/`lru_cache` method leak, **why there is no safe `eval`** (with a worked sandbox escape), the import system in full, a working AST-instrumenting meta-path finder, pytest assertion rewriting, bytecode patching as a trap, PEP 649 `annotationlib`, PEP 669 monitoring, **when to say no** | ✅ **written** |
| **T8** | **Engineering quality** | | |
| 43 | [`43-testing-strategy.md`](43-testing-strategy.md) | pluggy & the 52-hook collect→fixture→call lifecycle, conftest resolution order, fixture caching by scope, **assertion rewriting as an import hook**, **property-based testing with Hypothesis — the shrinker's algorithm read from its own source**, typed choice sequences & swappable providers, stateful testing (**a 3-step shrunk repro, measured**), the example database (**63 → 2 calls**), **mutation testing: 100% line *and* branch coverage, 6 of 16 mutants survive — all boundary off-by-ones (measured)**, coverage cores measured (`sysmon` 1.06× vs `ctrace` 1.92×) **plus one honestly inconclusive result**, concurrency as probability not verdict, the flakiness taxonomy & why "rerun until green" trades sensitivity for precision, atheris | ✅ **written** |
| 44 | `44-packaging-and-environments.md` | PEP 517/518/621, wheels & manylinux, ABI tags (incl. free-threaded), uv/poetry, lockfiles & reproducibility, editable installs, entry points | ⬜ |
| 45 | `45-supply-chain-and-security.md` | SBOM, sigstore/attestations, hash-pinning, `pickle` & deserialization, audit hooks, sandboxing, dependency CVE triage | ⬜ |
| 46 | `46-production-python.md` | startup time, memory footprint per worker, container & cgroup tuning, gunicorn/uvicorn worker models, graceful shutdown, config, observability hooks | ⬜ |
| **T9** | **Capstones** | | |
| 47 | `47-labs.md` | the hands-on drill book (see below) | ⬜ |
| 48 | `48-question-bank.md` | staff-level questions with real answers, per tier | ⬜ |
| 49 | `49-frontier.md` | 3.15+, JIT maturity, free-threading rollout, subinterpreter ecosystem, WASM, typed dialects | ⬜ |

---

## Tier 0 — bare metal (00–05)

**Why it's first.** Every "Python is slow" conversation is really about pointer chasing
and cache misses. Every "why isn't my threaded code faster" conversation is really about
contended cache lines and memory ordering. You cannot skip this and then understand
free-threading.

**You can answer this:**
- Why does iterating a `list` of objects touch memory differently than a NumPy array, and what does that do to the prefetcher?
- Two threads increment two adjacent counters, no shared variable. Throughput collapses. Why? How do you fix it? *(false sharing / padding to cache-line size)*
- What is the ABA problem, why does a plain CAS not prevent it, and name three reclamation schemes that do.
- Why can code that is correct on x86 break on ARM (Apple Silicon, Graviton) with no source change?
- Why is `0.1 + 0.2 != 0.3`, and why is that *not* a Python bug?
- What is the cost of a branch misprediction, an L3 hit, and a main-memory access, in cycles? Order-of-magnitude is enough — but you must know the order.

**Key reads:** CS:APP ch. 3–6; Drepper's *What Every Programmer Should Know About Memory*;
Bakhvalov ch. 3–4; Herlihy & Shavit ch. 7, 9–11; McKenney's *Memory Barriers: a Hardware
View for Software Hackers*.

---

## Tier 1 — operating system (06–12)

**Why it's second.** CPython's memory behaviour *is* the allocator's behaviour, which is
the kernel's VM behaviour. Its concurrency *is* pthreads and futexes. Its I/O ceiling is
`epoll`'s. When a container gets OOM-killed at 2 GB while `tracemalloc` reports 400 MB,
the explanation is entirely in this tier.

**You can answer this:**
- Precisely: RSS vs VSZ vs PSS. Which one does the OOM killer care about? Which one does your dashboard show?
- You `free()` 1 GB. RSS doesn't drop. Give two distinct mechanisms that explain this.
- Why is calling `fork()` from a multi-threaded process dangerous, and what specifically can deadlock? *(and why `multiprocessing` default start methods changed)*
- Level- vs edge-triggered `epoll`: what bug does each invite?
- What does io_uring change about the cost model of an async server relative to `epoll`?
- Your service has low CPU utilisation but high latency. cgroup CPU throttling is one candidate — how would you confirm or eliminate it?

**Key reads:** OSTEP (free, whole book); Kerrisk *TLPI* as a reference not a read-through;
Gregg *Systems Performance* 2e ch. 5–9; Gregg *BPF Performance Tools*.

---

## Tier 2 — CPython as a C program (13–17)

**Why it matters.** This is where the roadmap stops being general systems knowledge and
becomes *Python* expertise. Everything above Tier 2 is an emergent property of what
happens here. Build CPython from source in this tier — with `--with-pydebug` — and never
be intimidated by the C sources again.

**You can answer this:**
- Draw the memory layout of `[1, 2, 3]`. How many allocations? How many pointer hops to read `x[1]`?
- What are immortal objects (PEP 683) and what problem in *both* forking and free-threading do they solve?
- Explain biased reference counting: fast path, slow path, and why refcounting is the core obstacle to removing the GIL.
- When does `__slots__` actually save memory, and when does a key-sharing dict already give you most of that win for free?
- You write a C extension doing a 10-second matrix op. What must you do so other Python threads run meanwhile — and what are you promising when you do it?
- Stable ABI vs Limited API vs the free-threaded ABI tag: what breaks when, and what does that mean for shipping wheels?

**Key reads:** the [CPython devguide](https://devguide.python.org/) (primary source, free);
Shaw *CPython Internals*; the `Include/` and `Objects/` sources themselves.

---

## Tier 3 — compiler & runtime (18–23)

**You can answer this:**
- Disassemble `a.b.c(d)` and account for every instruction. Which ones have inline caches?
- What does PEP 659 specialization actually do to `BINARY_OP` after a few thousand int-int iterations, and what makes it deoptimize?
- "Zero-cost exceptions": zero cost *when*, and what replaced the old block stack?
- Why is the tier-2/JIT speedup modest so far, and what class of program does copy-and-patch help most?
- Reference cycles: exactly which objects can CPython not free by refcounting alone, and how does the cycle detector find them without stopping the world forever?
- A `__del__` on a cyclic object — what's guaranteed, what isn't, and why is `weakref.finalize` usually the better answer?

---

## Tier 4 — concurrency (24–30)

The highest-leverage tier for staff-level interviews and for real incidents. Do not start
it before Tier 0 doc 02 and 03.

> **[`24-the-gil.md`](24-the-gil.md) is written, and it sets the depth standard for
> every other doc in this folder.** It runs the full vertical slice — MESI cache-line
> ping-pong → `ceval_gil.c` struct fields → the `gil_drop_request` handoff protocol →
> futex/scheduler costs → the convoy effect with real numbers → Larry Hastings'
> Gilectomy (including the buffered-refcount source and the "seven cores to break even"
> result) → PEP 703's five-tier answer. If a doc in this manifest doesn't reach that
> level, it isn't finished.

**You can answer this:**
- What does the GIL protect? Give one thing people *believe* it protects that it does not.
- Is `list.append` atomic? Is `d[k] += 1`? Is `x = x + 1`? Justify each from bytecode, and then say whether your answer changes on a free-threaded build.
- Free-threaded Python is officially supported but the GIL build is still the default. Give the engineering reasons on both sides — including the single-thread overhead number.
- Under what workload shape do subinterpreters (PEP 734) beat both threads and `multiprocessing`? What can't they share?
- `asyncio`: what actually happens between `await` and the coroutine resuming? Where does the event loop block?
- Someone calls a blocking DB driver inside a coroutine. Describe the failure signature you'd see in production metrics, and how you'd detect it automatically.
- Design a bounded work queue with backpressure across threads. Now say what changes if you must do it lock-free.

---

## Tier 5 — performance engineering (31–35)

**You can answer this:**
- Your microbenchmark says the change is 40% faster; production says no change. List five reasons this happens.
- CPU profile shows 60% in `_PyEval_EvalFrameDefault`. What have you learned? *(almost nothing — and knowing why is the point)*
- Distinguish, with the tool you'd use for each: a leak, fragmentation, an unbounded cache, and a GC-tuning problem.
- When does moving code to Cython/Rust *not* help?
- How do you measure a latency improvement in prod with confidence when p99 is dominated by a downstream dependency?

---

## Tier 6 — type system & API design (36–39)

Type safety is treated here as an **engineering-scale** concern, not a syntax topic: what
it costs to adopt, what it actually catches, and where it is unsound on purpose.

**You can answer this:**
- Why is `list[Dog]` not a `list[Animal]`, and why *is* `Sequence[Dog]` a `Sequence[Animal]`?
- Write a generic with PEP 695 syntax, a bound, and a default. Now explain why variance is now inferred rather than declared.
- When do you need `ParamSpec`, and what does `Concatenate` add?
- `Protocol` vs `ABC`: pick one for a plugin interface and defend it.
- Your codebase is 400k lines, untyped. Give a rollout plan that produces value in month one and doesn't stall in month six.
- Name a type-checker-approved program that still crashes. *(there are many — that's gradual typing's bargain)*

---

## Tier 7 — metaprogramming (40–42)

**You can answer this:**
- Implement `property` from scratch using the descriptor protocol. Now explain why `@cached_property` needs `__set_name__`.
- Full attribute lookup order for `obj.x`, including data vs non-data descriptors and `__getattribute__`.
- `__init_subclass__` vs a metaclass — when is the metaclass genuinely required?
- Write a decorator that preserves the signature well enough that both `inspect` and a type checker are happy.
- Explain the import system deeply enough to write a meta-path finder, and give one legitimate production use.
- Give three reasons to reject a metaclass in code review even when it works.

---

## Tier 8 — engineering quality (43–46)

**You can answer this:**
- What does a property-based test find that example-based tests structurally cannot?
- 100% line coverage, still broken — give three shapes of bug coverage cannot see.
- Diagnose a flaky test that fails 1 in 200 in CI and never locally.
- Explain wheels, ABI tags, and manylinux well enough to say why a package won't install on a free-threaded interpreter.
- Why is `pickle` a security boundary, and what do you use instead across a trust boundary?
- Container gets OOM-killed with 8 gunicorn workers but not 4, at the same total traffic. Walk the whole diagnosis. *(this question spans Tiers 1, 2, 5 and 8 — it's the exit exam)*

---

## Tier 9 — capstones (47–49)

Reading produces recognition; building produces knowledge. Pick capstones, not chapters.

| Capstone | Proves you understand |
|---|---|
| Build CPython from source, add a bytecode instruction, and make the compiler emit it | 13, 18–20 |
| Write a C or Rust extension that releases the GIL and beat threads on a CPU-bound task | 17, 24, 34 |
| Implement a lock-free stack with hazard-pointer reclamation (in C/Rust), then break it deliberately with ABA | 02, 03 |
| Write a sampling profiler that reads another process's frames | 07, 12, 20, 23, 32 |
| Take one real service from GIL build to free-threaded and report an honest before/after | 24, 26, 31 |
| Cut a service's RSS by 40% and explain every megabyte | 07, 08, 16, 22, 35 |
| Type a 20k-line untyped module to `strict` and log every real bug found | 36–38 |
| Write an import hook + AST transform that instruments a codebase without touching source | 18, 42 |
| Reproduce, then fix, a `fork()`-in-threaded-process deadlock | 06, 10, 27 |

---

## 13. Suggested sequence

**Phase 1 — foundations (4–6 weeks).** Docs 00–03, 06–09. Skim 04, 05, 10–12 for
vocabulary. Deliverable: the false-sharing benchmark and the RSS-vs-heap experiment,
both measured on your own machine. *Don't move on until the numbers surprised you at
least once.*

**Phase 2 — CPython internals (5–7 weeks).** Docs 13–22. Build CPython with
`--with-pydebug`. Deliverable: added bytecode instruction + a written trace of one
`a.b(c)` call from source text to machine memory access.

**Phase 3 — concurrency (5–7 weeks).** Docs 23–30. Deliverable: the same CPU-bound
workload implemented four ways — threads on GIL build, threads on free-threaded build,
`multiprocessing`, subinterpreters — with a measured, explained comparison table. This
single artifact is worth more in an interview than any three chapters.

**Phase 4 — performance (4–5 weeks).** Docs 31–35. Deliverable: profile a real service
you own, ship one optimization, prove it in production metrics.

**Phase 5 — types, metaprogramming, quality (5–6 weeks).** Docs 36–46. These are more
parallelizable than the earlier phases; interleave them with real work.

**Phase 6 — capstones & frontier (ongoing).** Docs 47–49.

**If you have only four weekends:** 01 (caches) → 07 (virtual memory) → 16 (object
memory) → 24 (GIL) → 26 (free-threading) → 31 (measurement). That path answers most of
what people actually get wrong about Python performance.

---

## 14. The competence ladder

Apply this per topic. It is the antidote to the confident-sounding-but-hollow failure
mode this folder is designed to prevent.

| Rung | What it looks like | Honest label |
|---|---|---|
| 1 | You know the term exists | *Aware* |
| 2 | You can define it correctly | *Reciting* |
| 3 | You can explain it fluently — and collapse on one "why?" | **The trap** |
| 4 | You've built or broken it and measured the result | *Working knowledge* |
| 5 | You can predict behaviour in an unfamiliar system, and say where your model stops | *Staff level* |

Rung 5 includes **knowing the boundary of your own model**. "I'd expect X because Y, but
I'd measure it, because Z could dominate" is a stronger answer than false certainty —
in an interview and in an incident channel.

---

## 15. State of the runtime — as of Aug 2026

Version-specific facts rot fast. Verify against the release notes for the interpreter
you're actually running before quoting any of this.

- **Latest stable: Python 3.14** (released 7 Oct 2025). **3.15 is at rc1** (rc1 dated
  2026-08-04), final scheduled **1 Oct 2026**.
- **Free-threading is officially supported, not default.** PEP 703 was accepted Oct 2023;
  3.13 shipped it experimentally; **PEP 779 made it officially supported in 3.14** —
  phase II of a three-phase rollout. Per the official HOWTO, single-threaded overhead on
  pyperformance "ranges from about 1% on macOS aarch64 to 8% on x86-64 Linux".
  **We could not reproduce the 1% figure.** On this M3 Pro,
  [`26-free-threading.md`](26-free-threading.md) §3 measured **+8.1% geomean** across 9
  benchmarks (A/B/B/A alternated, reproduced at 8.2% on a second pass) — the *macOS
  aarch64* build behaving like the documented *worst* case. Range: +27% (nbody) to
  −5% (allocation-heavy work, where mimalloc beats pymalloc). Treat the official figure
  as a floor for some workload, not a forecast for yours. The GIL build remains the
  default, and "GIL off by default" is a *later* phase with no committed date.
- **A free-threaded stable ABI lands in 3.15.** [PEP 803](https://peps.python.org/pep-0803/)
  (`abi3t`, Viktorin & Goldbaum) is **Final**, resolved 30 Mar 2026, built on
  [PEP 793](https://peps.python.org/pep-0793/) (`PyModExport`). It makes `PyObject`
  opaque; the knob is `Py_TARGET_ABI3T`, the wheel tag `abi3.abi3t`. **Caveat: no build
  backend supports it yet** — not setuptools, meson-python, scikit-build-core or Maturin.
  See [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md) §3.
- **The JIT is still experimental and opt-in.** PEP 744 (informational, 3.13) introduced
  the copy-and-patch tier-2 JIT; official macOS/Windows binaries ship it **built but
  disabled**. Reported gains reach roughly **4–12% geomean on tier-1 platforms in 3.15**.
  **PEP 836** proposes the criteria for making it a supported, non-experimental feature.
- **The incremental GC was reverted — twice.** It was attempted in 3.13 and pulled
  (delaying 3.13 by a week), shipped in 3.14.0–3.14.4, then **reverted again in 3.14.5
  (10 May 2026)** after a reproduction showed unbounded memory growth; 3.14 and 3.15 are
  back on the 3.13 generational collector. Reintroduction is under discussion for 3.16,
  this time via the PEP process. **This is the single best worked example in the whole
  roadmap** of why GC design is hard and why "fewer objects scanned" ≠ "less memory".
- **Subinterpreters reached the stdlib** as `concurrent.interpreters` in 3.14 (PEP 734),
  on top of the per-interpreter GIL (PEP 684).
- **Remote debugging is built in**: PEP 768 added a zero-overhead external debugger
  interface with `sys.remote_exec`; `pdb` can attach to a running process.
- **Explicit lazy imports are coming in 3.15.** [PEP 810](https://peps.python.org/pep-0810/)
  was accepted unanimously by the Steering Council on 3 Nov 2025 and adds a **`lazy`
  keyword** (`lazy import json`, `lazy from json import dumps`) plus a `__lazy_modules__`
  migration hook. It is the successor to the *rejected* PEP 690, and deliberately avoids
  PEP 690's dict-lookup hooks in favour of lightweight proxy objects. Startup time and
  import-graph cost become a language-level concern rather than a packaging trick — see
  [`42-runtime-code-manipulation.md`](42-runtime-code-manipulation.md) §4.
- **Typing**: PEP 695 syntax (3.12) + PEP 696 defaults (3.13) are the modern baseline.
  Rust-based checkers (ty, pyrefly) are reshaping the tooling landscape alongside
  mypy and pyright.

---

## 16. Traps this roadmap exists to prevent

1. **Starting at Tier 3.** Bytecode is the most *fun* tier and the least useful without
   Tiers 0–2. `dis` output means nothing until you know what a pointer dereference costs.
2. **Reading about concurrency instead of measuring it.** Concurrency intuition is
   reliably wrong. Every claim in Tier 4 has a benchmark attached for a reason.
3. **Treating free-threading as "the GIL is gone, threads are fast now."** It's a
   different cost model — refcount contention, per-object locks, and a single-thread tax
   you pay whether or not you use threads.
4. **Optimizing before measuring, and measuring badly.** Doc 31 precedes docs 32–35 on
   purpose. A bad benchmark is worse than none: it produces confident wrong decisions.
5. **Collecting metaprogramming tricks.** Tier 7 is included so you can *read* a
   framework and *judge* a design, not so you ship metaclasses. Most of its value is
   knowing when to say no.
6. **Believing version-specific facts forever.** §15 will be stale within a year. The
   incremental-GC reversal is the proof: a "shipped" feature was un-shipped in a patch
   release. Check the release notes, always.
7. **Stopping at rung 3.** The whole ladder in §14 exists because fluent explanation
   feels identical, from the inside, to understanding.
8. **Concluding that a race is absent because you couldn't reproduce it.**
   [`30-concurrency-correctness.md`](30-concurrency-correctness.md) §3 measures the
   textbook `x += 1` race losing **42.7%** of its updates on 3.9, **0%** on 3.11–3.14,
   and **57.4%** on the free-threaded build — the *same source file*. Since 3.10, CPython
   only checks the eval breaker at ~22 instructions (calls, loop back-edges, `RESUME`),
   so most read-modify-write windows contain no switch point at all. A GIL-build test
   suite is not evidence of thread safety; it is evidence about where the check points
   happen to be.

---

*Next: pick Phase 1 and write `00-cpu-execution-model.md`. The manifest in §2 is the
build order; the checklists per tier are the definition of done.*
