# BOOKS — the researched reading list

Companion to [README.md](README.md). Every entry says **what it's for**, **when in the
roadmap it pays off**, and an honest **verdict** — including when *not* to read something.

Three rules that make this list work:

1. **Most of these are references, not reads.** Kerrisk's TLPI is 1,500 pages; nobody
   reads it. You *consult* it. The read-cover-to-cover list is deliberately short and
   marked **📖 READ**.
2. **Match the book to the phase.** Reading the GC Handbook before you've built CPython
   from source is wasted. Timing is most of the value here.
3. **Free is often better.** OSTEP, the CPython devguide, McKenney's *perfbook*, and
   Bakhvalov 2e are free *and* first-choice in their categories.

Legend: **📖 READ** cover-to-cover · **🔍 REF** consult as needed · **🎯 SKIM** targeted chapters only · **🆓** free/legally online

---

## Tier 0 — bare metal

| Book | Verdict | When |
|---|---|---|
| **Computer Systems: A Programmer's Perspective** (CS:APP), Bryant & O'Hallaron, 3e | **📖 READ** ch. 1–6. *The* foundation text: if you read one book from Tier 0, this is it. Ch. 5–6 (optimization & memory hierarchy) rewire how you read any code. | Phase 1, first |
| **What Every Programmer Should Know About Memory**, Ulrich Drepper (2007) | **📖 READ** 🆓 A ~100-page paper, not a book. Dated on specific hardware, permanently correct on the concepts. The best explanation of cache coherence and false sharing anywhere. | Phase 1, week 1 |
| **Performance Analysis and Tuning on Modern CPUs**, Denis Bakhvalov, **2e (Nov 2024)** | **📖 READ** 🆓 (free PDF from the author) The modern complement to Drepper: top-down microarchitecture analysis, PMU counters, real profiling on current CPUs. Best single source on *measuring* the physical layer. | Phase 1, then again in Phase 4 |
| **The Art of Multiprocessor Programming**, Herlihy, Shavit, Luchangco, Spear, **2e (2020)** | **🎯 SKIM** ch. 7 (spin locks & contention), 9–11 (linked lists, queues, stacks), 16. Covers hazard pointers and the ABA problem — the canonical academic treatment. Dense; don't read linearly. | Phase 1, doc 03 |
| **Is Parallel Programming Hard, And, If So, What Can You Do About It?** ("perfbook"), Paul McKenney | **🔍 REF** 🆓 Continuously updated. The practitioner's counterweight to Herlihy & Shavit: RCU, memory barriers, deferred reclamation, from someone who maintains this in the Linux kernel. | Phase 1, docs 02–03 |
| **Memory Barriers: a Hardware View for Software Hackers**, McKenney | **📖 READ** 🆓 Short paper. The clearest existing explanation of *why* store buffers and invalidate queues force memory barriers to exist. | Phase 1, doc 02 |
| **Computer Architecture: A Quantitative Approach**, Hennessy & Patterson, 6e | **🔍 REF** The authority, but heavy for this roadmap's purposes. CS:APP + Bakhvalov cover what you need. Reach for it only when you want the *why* behind a microarchitectural claim. | As needed |
| **C++ Concurrency in Action**, Anthony Williams, 2e | **🎯 SKIM** ch. 5 & 7. Best practical treatment of the C++11 memory model (acquire/release/seq_cst) — which is the model CPython's atomics are written against. Ignore that it's C++. | Phase 1, doc 02 |

---

## Tier 1 — operating system

| Book | Verdict | When |
|---|---|---|
| **Operating Systems: Three Easy Pieces** (OSTEP), Arpaci-Dusseau | **📖 READ** 🆓 The best OS book, and it's free. Virtualization → concurrency → persistence, all three parts. Readable in a way no other OS text is. Start here, not Tanenbaum. | Phase 1 |
| **The Linux Programming Interface**, Michael Kerrisk | **🔍 REF** The definitive syscall reference. 1,500 pages — do **not** read it through. Consult the chapters on signals (20–22), threads (29–33), and memory (48–49) when docs 07/10/11 need them. | Phase 1, on demand |
| **Systems Performance**, Brendan Gregg, 2e (2020) | **📖 READ** ch. 5–9 (CPU, memory, filesystems, disks, network); **🔍 REF** the rest. The methodology (USE method, workload characterization) is worth as much as the tooling. | Phase 1 & Phase 4 |
| **BPF Performance Tools**, Brendan Gregg | **🔍 REF** The toolbox for doc 12. Pairs with Systems Performance; use it when you need to observe something you can't otherwise see. | Phase 1, doc 12 |
| **Linkers and Loaders**, John Levine | **🎯 SKIM** 🆓 (drafts online) Old but nothing has replaced it. Read it the first time a `.so` fails to load or a symbol collides — that day will come. | Phase 1, doc 04 |
| **Modern Operating Systems**, Tanenbaum, 4e | **⏭️ SKIP** unless you like it. OSTEP is better for this roadmap and free. Listed only so you know it's a deliberate omission. | — |

---

## Tier 2–3 — CPython internals

| Book | Verdict | When |
|---|---|---|
| **The CPython Developer's Guide** (devguide.python.org) | **📖 READ** 🆓 **Primary source, and it beats every book on this list for accuracy.** Build instructions, the compiler pipeline, the object model, the C-API rules. Books go stale; this doesn't. | Phase 2, first |
| **CPython Internals**, Anthony Shaw (Real Python) | **📖 READ** The best-established guided tour of the interpreter. Written against 3.9, so it **predates** the adaptive specializing interpreter (3.11), zero-cost exceptions (3.11), the JIT (3.13) and free-threading (3.13/3.14). Still the best structural map — read it for architecture, then get the recent changes from PEPs and the devguide. | Phase 2 |
| **CPython: A Complete Guide to CPython's Architecture and Performance** (Apress, Nov 2025) | **🎯 SKIM** Recent enough to cover the modern interpreter. Newer and less battle-tested than Shaw; useful as a second angle on `PyObject`/`PyTypeObject` and the VM. | Phase 2, optional |
| **CPython Internals Explained**, Ethan Garrett (2025) | **🎯 SKIM** Claims coverage through 3.11–3.14 including free-threading. Recent, but unvetted relative to Shaw — treat as supplementary, verify against the devguide. | Phase 2, optional |
| **Inside the Python Virtual Machine**, Obi Ike-Nwosu | **🎯 SKIM** 🆓 Short and free. Good on the eval loop and frame mechanics. Dated, but the core VM shape it describes is still recognizable. | Phase 2, doc 20 |
| **The Garbage Collection Handbook**, Jones, Hosking & Moss, **2e (2023)** | **🎯 SKIM** ch. 1–6 + the reference-counting chapter. The definitive GC text — parallel, incremental, concurrent, real-time collection. Read it *after* doc 22, when you want to know why CPython's choices are what they are (and why the incremental-GC attempt failed twice). | Phase 2, after doc 22 |
| **Crafting Interpreters**, Robert Nystrom | **📖 READ** 🆓 Part II especially. Not about Python at all — and that's the point. Building a bytecode VM yourself makes CPython's source legible in a way no amount of reading does. The single best *preparation* for Tier 3. | Before Phase 2 |
| **Engineering a Compiler**, Cooper & Torczon, 3e | **🔍 REF** For doc 18 if you want real depth on parsing and SSA. Optional — the PEG parser is well documented in PEP 617. | As needed |

**Read the PEPs directly for anything post-3.10.** No book is current. The essential set:
PEP 659 (specializing interpreter), 703 (free-threading), 683 (immortal objects), 684
(per-interpreter GIL), 734 (subinterpreters), 744 & 836 (JIT), 669 (monitoring),
768 (remote debugging), 617 (PEG parser), 695/696 (typing).

---

## Tier 4 — concurrency

| Book | Verdict | When |
|---|---|---|
| **Python Concurrency with asyncio**, Matthew Fowler (Manning, 2022) | **📖 READ** The best current book on asyncio specifically. Goes past tutorial level into the loop, executors, and multiprocessing integration. | Phase 3, docs 28–29 |
| **Using Asyncio in Python**, Caleb Hattingh (O'Reilly) | **🎯 SKIM** Short, opinionated, excellent on *why* asyncio's API looks the way it does and which parts to avoid. Read alongside Fowler. | Phase 3 |
| **The Art of Multiprocessor Programming** 2e | *(see Tier 0)* — return to it here for the theory behind per-object locking and lock-free containers. | Phase 3 |
| **Java Concurrency in Practice**, Goetz | **🎯 SKIM** ch. 2–5, 10. Yes, Java. It is still the clearest book ever written on the *discipline* of shared-state concurrency — publication, safe construction, deadlock avoidance. Directly applicable to free-threaded Python. | Phase 3, doc 30 |

> **Nothing published covers free-threading properly yet.** For docs 24 and 26, the
> sources in [`24-the-gil.md` §12](24-the-gil.md#12-sources) — PEP 703, the free-threading
> HOWTO, Stinner's blog, the LWN articles, and Hastings' Gilectomy talks — *are* the
> literature. Books will lag this by years.

---

## Tier 5 — performance

| Book | Verdict | When |
|---|---|---|
| **High Performance Python**, Gorelick & Ozsvald, 2e | **📖 READ** The standard text for Python-specific optimization: profiling, data structures, going native, clusters. 2e is 2020, so it predates the 3.11+ interpreter speedups — take its *methods*, re-measure its *numbers*. | Phase 4 |
| **Systems Performance** 2e, Gregg | *(see Tier 1)* — the methodology half is the important half here. | Phase 4 |
| **Performance Analysis and Tuning on Modern CPUs** 2e | *(see Tier 0)* — reread ch. 5–7 once you're profiling real native code. | Phase 4 |
| **Every Computer Performance Book**, Bob Wescott | **🎯 SKIM** Short. Good on the *statistics* of performance work — queueing, measurement error, capacity — which is where most engineers are weakest. | Phase 4, doc 31 |

---

## Tier 6–7 — types, design, metaprogramming

| Book | Verdict | When |
|---|---|---|
| **Fluent Python**, Luciano Ramalho, 2e (2022) | **📖 READ** The single best advanced Python book. Data model, descriptors, metaclasses, iterators, concurrency, type hints — ~1,000 pages and worth all of them. If you skip everything else in Tiers 6–7, read Part V (metaprogramming) and the typing chapters. | Phase 5, first |
| **Robust Python**, Patrick Viafore (2e, 2025) | **📖 READ** The best book on type safety as an *engineering* practice rather than a syntax feature — adoption strategy, what types actually catch, enforcing design intent. Directly targets doc 38. | Phase 5, docs 36–38 |
| **Python Distilled**, David Beazley (2021) | **🎯 SKIM** Beazley's compressed re-explanation of the language by someone who understands the runtime. Short. Excellent for filling gaps you didn't know you had. | Any time |
| **Effective Python**, Brett Slatkin, 3e (Nov 2024) | **🎯 SKIM** 125 items, covers through 3.13. Idiom-level rather than internals-level, but the most current of the "how to write good Python" books. | Any time |
| **Architecture Patterns with Python**, Percival & Gregory | **📖 READ** 🆓 (free online) For doc 39. Ports & adapters, repository, unit of work, event-driven — the only good book on structuring *large* Python systems. | Phase 5, doc 39 |
| **Serious Python**, Julien Danjou | **🎯 SKIM** Packaging, testing, performance, distribution from a long-time OpenStack maintainer. Practical, uneven, but strong on the operational chapters. | Phase 5 |

**For typing specifically, the primary sources beat the books:** the
[typing spec](https://typing.readthedocs.io/en/latest/spec/), PEP 695 (type parameter
syntax), PEP 696 (defaults), PEP 612 (ParamSpec), PEP 646 (TypeVarTuple), and the
mypy/pyright documentation. This area moves fast — Rust-based checkers (ty, pyrefly) are
reshaping the tooling, and no book covers them yet.

---

## Tier 8 — quality & production

| Book | Verdict | When |
|---|---|---|
| **Python Testing with pytest**, Brian Okken, 2e | **📖 READ** The pytest book. Fixtures and plugin architecture especially. | Phase 5, doc 43 |
| **Hypothesis documentation** | **📖 READ** 🆓 No book needed; the docs are excellent. Property-based testing is the highest-leverage idea in Tier 8 and most engineers have never used it. | Phase 5, doc 43 |
| **Site Reliability Engineering** (Google) | **🎯 SKIM** 🆓 For doc 46. You already have `../sre-observability/` for this — cross-reference rather than re-read. | Phase 5 |
| **Securing the Software Supply Chain** / SLSA docs | **🔍 REF** 🆓 For doc 45. The formal frameworks matter more than any book here. | Phase 5, doc 45 |

---

## The honest minimum

If you'll realistically read four books, read these — in this order:

1. **Computer Systems: A Programmer's Perspective**, ch. 1–6 — the machine
2. **Operating Systems: Three Easy Pieces** 🆓 — the kernel
3. **Crafting Interpreters** 🆓, Part II — how a bytecode VM works, by building one
4. **Fluent Python** 2e — Python at the level the rest of this folder assumes

Then the CPython devguide and the PEPs, forever, because that's where the truth lives.

**Everything past that is a reference you consult when a doc in the manifest sends you
there.** The roadmap in [README.md](README.md) is the spine; this file is the library it
points into.

---

## Watch-list — things that will change this file

- No good book on **free-threading** exists yet. Expect one within ~2 years; until then
  PEP 703 and the free-threading HOWTO are the text.
- **CPython Internals** (Shaw) needs a new edition for 3.11+ (specializing interpreter,
  JIT, free-threading). If one appears, it likely displaces two entries above.
- **High Performance Python** 3e would supersede 2e's now-dated benchmarks.
- The **typing** entries will churn as ty/pyrefly mature.
- The **incremental GC** story (README §15) will get a PEP for 3.16 — that PEP will be
  the best reading on GC trade-offs in CPython when it lands.
