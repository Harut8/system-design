# 03 — Lock-free algorithms and the reclamation problem

> **Tier 0, doc 03.** Prerequisites: [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md)
> (cache lines, MESI, the read/write asymmetry, false sharing at 128 B),
> [`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md) (CAS, LL/SC,
> acquire/release, why ARM is not x86-TSO). Feeds into:
> [`24-the-gil.md`](24-the-gil.md) §8, [`22-garbage-collection.md`](22-garbage-collection.md) §10,
> [`25-threads-and-synchronization.md`](25-threads-and-synchronization.md),
> [`26-free-threading.md`](26-free-threading.md),
> [`30-concurrency-correctness.md`](30-concurrency-correctness.md).
>
> **THESIS: the hard part of lock-free programming is not the algorithm, it is
> deciding when it is safe to call `free()`.** Every published lock-free container —
> Treiber's stack, the Michael–Scott queue, Harris's list — is a page of code you can
> transcribe correctly in an afternoon. The part nobody transcribes is the part that
> answers *"another thread may still be holding a pointer into the node I just
> unlinked; when may I release its memory?"* That question has no cheap universal
> answer, which is why it has spawned an entire literature — hazard pointers, epochs,
> QSBR, RCU, deferred refcounting — and why **CPython's free-threaded build ships
> `Python/qsbr.c`, a memory-reclamation scheme borrowed from FreeBSD, but ships almost
> no custom lock-free data structures.** The corollary, which this document argues
> with measurements rather than taste: **a well-designed adaptive lock usually beats
> the lock-free structure you were about to write.**

> **Measurement provenance.** Every number and every listing marked *(measured)* was
> produced on the machine this repo lives on: **Apple M3 Pro, macOS 15 (Darwin 25.5.0),
> arm64, 5 performance + 6 efficiency cores, 128-byte cache lines, 16 KB pages**, with
> **Apple clang 21.0.0** and CPython **3.14.6** / **3.14.6 free-threading**. CPython
> source excerpts were read from `python/cpython` `main` during the writing of this
> document and are quoted verbatim; every file path and symbol name below was checked to
> exist. Anything I could not verify is flagged in place, and §7.3 documents a bug
> AddressSanitizer found **in my own code while writing this document** — that section
> is not decoration, it is the argument.
>
> **This laptop is a hostile benchmarking host** ([`01`](01-memory-hierarchy-and-caches.md) §2):
> heterogeneous cores, no `perf(1)`, no reliable pinning. Contended-atomics numbers here
> vary by 2× run to run and I show the spread rather than a single tidy figure.

## Contents

1. [Progress guarantees, precisely](#1-progress-guarantees-precisely)
2. [What CAS actually compiles to on this machine](#2-what-cas-actually-compiles-to-on-this-machine)
3. [The Treiber stack](#3-the-treiber-stack)
4. [The ABA problem — the centerpiece](#4-the-aba-problem--the-centerpiece)
5. [Tagged pointers, double-width CAS, and stealing address bits](#5-tagged-pointers-double-width-cas-and-stealing-address-bits)
6. [The Michael–Scott queue](#6-the-michaelscott-queue)
7. [Reclamation is the real problem](#7-reclamation-is-the-real-problem)
8. [Lock-free is usually slower than a lock — measured](#8-lock-free-is-usually-slower-than-a-lock--measured)
9. [Backoff and the elimination-backoff stack](#9-backoff-and-the-elimination-backoff-stack)
10. [When not to write lock-free code (almost always)](#10-when-not-to-write-lock-free-code-almost-always)
11. [CPython: `qsbr.c`, `PyMutex`, and the free-threaded build](#11-cpython-qsbrc-pymutex-and-the-free-threaded-build)
12. [Lab exercises](#12-lab-exercises)
13. [Question bank](#13-question-bank)
14. [Sources](#14-sources)

---

## 1. Progress guarantees, precisely

"Lock-free" is the most misused term in concurrency. It does not mean "fast", it does
not mean "doesn't use a mutex", and it does not mean "scales". It is a **liveness
property**, and the definitions are precise. Learn them in this form:

| Guarantee | Definition | What it survives |
|---|---|---|
| **Wait-free** | *Every* thread completes its operation in a **bounded number of its own steps**, regardless of what other threads do. | Any thread being descheduled, preempted, page-faulted, or killed. |
| **Lock-free** | **At least one** thread makes progress in a bounded number of *system-wide* steps. Individual threads may starve forever. | Any thread being descheduled indefinitely. |
| **Obstruction-free** | A thread completes in a bounded number of its own steps *if it eventually runs in isolation* (all other threads pause). | Nothing, under contention — livelock is permitted. |
| **Blocking** (lock-based) | No guarantee. If the thread holding the lock stops, everyone stops. | Nothing. |

Two things to internalize.

**The hierarchy is strict.** wait-free ⊂ lock-free ⊂ obstruction-free. Every wait-free
algorithm is lock-free; the converse is emphatically false. A Treiber stack is lock-free
but *not* wait-free: an unlucky thread can lose the CAS race forever while others make
progress. Nothing in the definition of lock-free bounds any individual thread's latency.

```
              obstruction-free
       ┌──────────────────────────────────────────────────┐
       │              lock-free                           │
       │   ┌───────────────────────────────────────────┐  │
       │   │           wait-free                       │  │
       │   │  ┌─────────────────────────────────────┐  │  │
       │   │  │ atomic fetch-add counter            │  │  │
       │   │  │ single-writer seqlock READER (ret.  │  │  │
       │   │  │   may retry → NOT wait-free!)       │  │  │
       │   │  └─────────────────────────────────────┘  │  │
       │   │  Treiber stack · Michael–Scott queue      │  │
       │   │  Harris linked list · lock-free hash set  │  │
       │   └───────────────────────────────────────────┘  │
       │  Herlihy/Luchangco/Moir obstruction-free deque   │
       │  most STM implementations                        │
       └──────────────────────────────────────────────────┘
                          ▲
                          │  everything outside this box is BLOCKING:
                          │  pthread_mutex, PyMutex, the GIL, Python's
                          │  threading.Lock, every per-object lock in
                          │  the free-threaded build.
```

**The property that actually matters operationally is not speed — it is
preemption-immunity.** This is the one honest reason to reach for lock-free code, and
it is narrow:

- A **signal handler** must not take a lock that the interrupted thread might hold.
  Async-signal-safety is a lock-freedom requirement in disguise
  ([`10-signals-fork-exec.md`](10-signals-fork-exec.md)).
- A **hard-real-time** or **audio** thread must not be able to block on a lower-priority
  thread that got preempted mid-critical-section (priority inversion).
- A **crash-tolerant shared-memory** region — if a process dies holding a lock in
  `/dev/shm`, the structure is permanently wedged. Lock-free structures degrade to
  "leaked memory" instead of "deadlocked forever."
- A **profiler or debugger** reading another thread's state at an arbitrary instant.
  This is exactly the constraint behind PEP 768's remote debugging interface
  ([`23-tracing-and-runtime-hooks.md`](23-tracing-and-runtime-hooks.md)).

Notice that "we want more throughput" is not on that list. §8 measures why.

> **The distinction that gets missed in interviews.** "Lock-free" says nothing about
> *fairness* or *tail latency*. A lock-free stack under heavy contention can have a
> **worse p99 than a mutex**, because a mutex's wait queue is FIFO-ish while CAS retry is
> a lottery you can lose arbitrarily many times in a row. A good adaptive mutex
> (§11.3) actively hands off ownership to prevent starvation. The lock-free stack has no
> such mechanism, by construction. If someone tells you lock-free is "better for
> latency", ask them *which* latency.

---

## 2. What CAS actually compiles to on this machine

You cannot reason about lock-free code without knowing what your compiler emits. Doc 02
covers the memory model; this section is the concrete instruction selection on **arm64**,
because it differs from x86 in a way that matters for §4 and §5.

Compiled here *(measured)*:

```c
typedef struct { void *ptr; uintptr_t tag; } tagged_t;
_Atomic tagged_t th;                 /* 16 bytes */
_Atomic(void*)   sh;                 /*  8 bytes */

int cas128(tagged_t *exp, tagged_t nw)
{ return atomic_compare_exchange_weak_explicit(&th, exp, nw,
      memory_order_acq_rel, memory_order_relaxed); }
int cas64(void **exp, void *nw)
{ return atomic_compare_exchange_weak_explicit(&sh, exp, nw,
      memory_order_acq_rel, memory_order_relaxed); }
long fetchadd(_Atomic long *p)
{ return atomic_fetch_add_explicit(p, 1, memory_order_relaxed); }
```

```console
$ clang -O2 -std=c11 -S -o - cas.c        # default: Apple M3 => ARMv8.4+, LSE present
_cas128:  caspal  x2, x3, x4, x5, [x8]    ← single-instruction 128-bit CAS pair
_cas64:   casal   x9, x1, [x10]           ← single-instruction 64-bit CAS
_fetchadd: ldadd  x8, x0, [x0]            ← single-instruction atomic add

$ clang -O2 -Xclang -target-feature -Xclang -lse -S -o - cas.c   # LSE disabled
_cas128:  ldaxp x10, x9, [x12] ... stlxp w13, x1, x2, [x12]      ← LL/SC retry loop
_cas64:   ldaxr x8,  [x9]      ... stlxr w10, x1, [x9]           ← LL/SC retry loop
```

Three facts follow, and all three are load-bearing later:

**1. Base ARMv8 has no CAS instruction at all.** It has **LL/SC**: `ldxr`/`ldaxr`
(load-exclusive, optionally acquire) sets a per-core *exclusive monitor* on the address;
`stxr`/`stlxr` (store-exclusive, optionally release) succeeds only if the monitor is
still held. Any intervening write to the line — from *any* core — clears the monitor and
the store fails, and you loop. So `compare_exchange` on ARM is a *software* loop around
hardware primitives, whereas x86's `lock cmpxchg` is one instruction.

**2. LSE (Large System Extensions, ARMv8.1) added real CAS**, and Apple Silicon has it.
`casal` is a single-instruction compare-and-swap; `caspal` is the **128-bit pair**
version operating on an aligned register pair. This is ARM's answer to x86's
`cmpxchg16b`, and it is why the tagged-pointer technique in §5 is cheap here.

**3. LL/SC is *stronger* than CAS in one specific way, and this is a classic exam
question.** CAS compares values; LL/SC detects *any store* to the location, even one that
writes back the same value. So a *narrow* ABA — pointer changes A→B→A entirely between
one thread's `ldxr` and its `stxr` — is impossible under LL/SC, because the intervening
stores clear the monitor. **This does not save you.** In a real Treiber stack, the window
between reading `head` and CAS-ing it spans a dereference of `head->next` and arbitrary
scheduling delay, which is thousands of times longer than an LL/SC monitor can survive.
The compiler emits `ldaxr`/`stlxr` back-to-back around the comparison only; your
algorithm's window is far wider. §4 reproduces the bug *on this LL/SC machine* to prove
it.

> **Cost model reminder from [`01`](01-memory-hierarchy-and-caches.md) §1.** An
> uncontended atomic RMW is 20–50 cycles. A contended one — where the line must migrate
> from another core's L1 — is 100–500+. A *failed* CAS costs the same as a successful
> one, because it still had to acquire the line exclusively. **Retry loops are not free
> spinning; every iteration is a full coherence transaction.** That single sentence
> explains most of §8.

---

## 3. The Treiber stack

R. Kent Treiber's 1986 IBM technical report gives the canonical lock-free stack: a
singly-linked list where `push` and `pop` are each a CAS on the head pointer.

```
   push(N):                              pop():
     ┌───┐                                 ┌───┐
     │ N │──┐                              │ A │◀── head        old = head
     └───┘  │  N->next = head              └─┬─┘                next = old->next
            ▼  CAS(head, old, N)             │                  CAS(head, old, next)
     ┌───┐  ┌───┐  ┌───┐                   ┌─▼─┐  ┌───┐
     │ A │─▶│ B │─▶│ C │                   │ B │─▶│ C │◀── head afterwards
     └───┘  └───┘  └───┘                   └───┘  └───┘
       ▲
      head (before)
```

Real, compiling C11 *(this exact code was built with `clang -O2 -std=c11 -Wall -pthread`
and run — see §8)*:

```c
typedef struct node { struct node *next; long val; } node_t;
static _Alignas(128) _Atomic(node_t *) head;     /* own cache line: 01 §6 */

static void push(node_t *n) {
    node_t *old = atomic_load_explicit(&head, memory_order_relaxed);
    do {
        n->next = old;                            /* n is thread-private here */
    } while (!atomic_compare_exchange_weak_explicit(
                 &head, &old, n,
                 memory_order_release,            /* publish n->next before n */
                 memory_order_relaxed));
}

static node_t *pop(void) {
    node_t *old = atomic_load_explicit(&head, memory_order_acquire);
    for (;;) {
        if (!old) return NULL;
        node_t *next = old->next;                 /* <-- THE dangerous load */
        if (atomic_compare_exchange_weak_explicit(
                &head, &old, next,
                memory_order_acquire, memory_order_relaxed))
            return old;
        /* CAS wrote the observed value back into `old`; loop with it. */
    }
}
```

Details that separate a working transcription from an understanding:

- **`compare_exchange_weak` may fail spuriously** — that is exactly the LL/SC
  `stlxr`-failed case from §2. It is correct *and faster* inside a loop you were going to
  write anyway. Use `_strong` only when there is no loop.
- **On failure, C11's `compare_exchange` writes the observed value into `expected`.**
  That is why the loop needs no explicit reload. Forgetting this and reloading manually is
  harmless; forgetting it and *not* reloading is an infinite loop.
- **`release` on push, `acquire` on pop** is the minimum. The `release` publishes the
  store to `n->next` so a popper that `acquire`-loads `head` sees an initialized node.
  Drop to `relaxed` on both and this code is broken on ARM and fine on x86 — the exact
  hazard [`02`](02-atomics-and-memory-models.md) exists to prevent.
- **`_Alignas(128)`.** `head` is the single most contended word in the program. Sharing
  its 128-byte line with anything else costs you for free
  ([`01`](01-memory-hierarchy-and-caches.md) §6).

**This code is correct only if nodes are never freed.** It has two independent bugs the
moment you call `free()`:

1. `node_t *next = old->next;` dereferences a node that another thread may have already
   popped and freed → **use-after-free** (§7).
2. Even with no `free()` at all — even with a perfect allocator — the pointer comparison
   in the CAS can succeed against a node that was popped and *re-pushed* → **ABA** (§4).

Those are different bugs with different fixes, and conflating them is the single most
common confusion in this area. ABA is a *logic* bug about pointer identity. Use-after-free
is a *lifetime* bug about memory. A tagged pointer fixes ABA and does nothing for
lifetime. Hazard pointers fix lifetime and, as a side effect, most ABA. You generally need
to think about both.

---

## 4. The ABA problem — the centerpiece

### 4.1 The statement

> **ABA:** a thread reads a shared location and observes value **A**. Other threads change
> it to **B** and then back to **A**. The first thread's `compare_exchange` compares
> against **A**, succeeds — and is *wrong*, because the world it validated its decision
> against no longer exists.

A CAS is not "nothing happened since I looked." It is only **"the value here equals the
value I remember."** Those are the same statement exactly when values are never reused.
Heap pointers are reused constantly — that is what an allocator *is* — so for pointers the
two statements come apart, and every lock-free algorithm that CASes a pointer has to
close the gap.

### 4.2 The worked interleaving

Stack contains `A → B → C`. Thread T1 calls `pop()`. Thread T0 does three operations.

```
 TIME │  THREAD T1  (a pop, interrupted)      │  THREAD T0                        │ head
──────┼───────────────────────────────────────┼───────────────────────────────────┼──────────
  1   │ old  = load(head)          → A        │                                   │ A→B→C
      │ next = old->next           → B        │                                   │
      │      T1 now holds the pair (A, B)     │                                   │
──────┼───────────────────────────────────────┼───────────────────────────────────┼──────────
  2   │        *** DESCHEDULED ***            │ pop() → returns A                 │ B→C
      │  (page fault / preemption / an        │   T0 now OWNS node A              │
      │   E-core migration / GC pause /       │                                   │
      │   the OS just felt like it)           │                                   │
──────┼───────────────────────────────────────┼───────────────────────────────────┼──────────
  3   │              ...                      │ pop() → returns B                 │ C
      │                                       │   T0 now OWNS node B.             │
      │                                       │   T0 may free(B) RIGHT HERE.      │
      │                                       │   B->next is still C — nobody     │
      │                                       │   clears it, and nobody must.     │
──────┼───────────────────────────────────────┼───────────────────────────────────┼──────────
  4   │              ...                      │ push(A)   ← the SAME address A    │ A→C
      │                                       │   (recycled node, or a fresh      │
      │                                       │    malloc that reused the block)  │
      │                                       │        *** this is the second A ***
──────┼───────────────────────────────────────┼───────────────────────────────────┼──────────
  5   │ CAS(&head, expected=A, desired=B)     │                                   │
      │   head == A ?  YES  ✅  → SUCCEEDS    │                                   │ B→C
      │                                       │                                   │
      │   T1 returns A, believing it popped   │                                   │
      │   the stack from A→C to C.            │                                   │
      │   It actually set head = B.           │                                   │
──────┴───────────────────────────────────────┴───────────────────────────────────┴──────────

 RESULT:  head → B → C
          • B is owned by T0 (it popped it at step 3) AND is back in the stack.
          • If T0 freed B at step 3, `head` is now a DANGLING POINTER and the
            next pop() reads freed memory.
          • If T0 kept B, the next pop() hands B to a second owner: a duplicate
            pop — and eventually a double free.
          • C was never lost, which is what makes this so hard to spot in review:
            the structure still "looks" like a valid stack.
```

Step 5 is the whole problem in one line. **T1's CAS asked the right question and got a
true answer that meant something false.** `head == A` was true. "Nothing has changed since
step 1" was catastrophically false.

### 4.3 It reproduces — deterministically, and by accident

I built the above as a runnable program (`aba.c`, ~250 lines) with a two-condvar gate so
the interleaving is forced exactly as numbered. Verbatim output *(measured, Apple M3 Pro,
`clang -O2 -std=c11 -Wall -pthread`)*:

```console
$ ./aba forced
== forced ABA, plain CAS ==
  init: head -> A(1) -> B(2) -> C(3)
  T1 step1: read head=0x10338de80 (id=1), next=0x10338db90 (id=2)
  T0 step2: pop() -> A(1).  head -> B -> C
  T0 step3: pop() -> B(2).  head -> C   [B is now MINE]
  T0 step4: push(A).        head -> A -> C   *** the A-B-A ***
  T1 step5: CAS(head, 0x10338de80, 0x10338db90) -> SUCCEEDED

  after : head = 0x10338db90 (id=2)
  *** CORRUPT: head points at B(2), which T0 already popped
  walk  : 2 -> 3
  owner : B(2) is owned by T0 (popped) AND is back in the stack -> double free / duplicate pop
```

Now add `free(B)` at step 3 and build with AddressSanitizer *(measured)*:

```console
$ clang -O1 -g -std=c11 -pthread -fsanitize=address aba.c -o aba_asan && ./aba_asan uaf
...
  T0 step3: pop() -> B(2).  head -> C   [B is now MINE]
  T0       : free(B)
  T1 step5: CAS(head, 0x..., 0x...) -> SUCCEEDED

=================================================================
==56536==ERROR: AddressSanitizer: heap-use-after-free on address 0x602000000110
READ of size 4 at 0x602000000110 thread T0
    #0 0x000102c79738 in run_forced aba.c:116
freed by thread T0 here:
    #0 free  #1 0x000102c7942c in run_forced aba.c:106
previously allocated by thread T0 here:
    #0 calloc  #1 0x000102c79164 in run_forced aba.c:91
SUMMARY: AddressSanitizer: heap-use-after-free aba.c:116 in run_forced
```

**"But that's a rigged schedule."** Fair. So here is the same stack with *no* forced
schedule at all: three nodes, N threads looping `pop(); mark-exclusively-owned; push()`,
where a "duplicate pop" means two threads simultaneously believed they owned the same
node. Two independent 2-second runs *(measured)*:

| Threads | ops (run 1) | duplicate pops (run 1) | ops (run 2) | duplicate pops (run 2) |
|---|---|---|---|---|
| 2  | 19.0 M | **0** | 21.5 M | **0** |
| 3  | 19.8 M | **61,838** | 16.1 M | **0** |
| 4  | 15.7 M | **537,427** | 25.4 M | **428,472** |
| 8  | 13.9 M | **4,294,343** | 14.3 M | **4,108,352** |
| 11 | 22.8 M | 6,326,869 | — | — |

Read that table carefully, because it is the most useful thing in this document:

- **At 2 threads it never happened in ~40 M operations.** You could ship this, load-test
  it on a 2-vCPU box, and see a perfect green build forever.
- **At 3 threads it happened in one run and not the other.** This is the shape of the
  bug that is closed as "could not reproduce."
- **At 8 threads, roughly 30% of all operations are corrupt.** The difference between
  "flawless" and "catastrophic" is a machine size, not a code change.
- The M3 Pro has **5 P-cores**. The cliff between 4 and 8 threads is where threads start
  being preempted and migrated to E-cores — i.e. where step 2's "descheduled" becomes
  routine. **Your ABA rate is a function of your scheduler, not your algorithm**, which
  is precisely why testing cannot be your defence.

And note the machine: this is arm64, where CAS is `casal`, and where §2's LL/SC exclusive
monitor is often cited as "ABA-immune." The monitor protects a window of a few
instructions. The algorithm's window is a full `pop()` body plus arbitrary preemption.
**LL/SC did not help.**

### 4.4 What ABA is *not*

- It is **not** a memory-model or barrier problem. Add `seq_cst` everywhere and it still
  happens; the run above already uses acquire/release correctly.
- It is **not** specific to stacks. Any CAS on a pointer that can be recycled has it:
  queues, freelists, lock-free hash tables, `epoch` counters that wrap
  (see `QSBR_LT` in §11.1 — CPython's wrap-safe comparison macros are ABA-avoidance for
  *sequence numbers*).
- It is **not** always a bug. If the value CASed is a monotonically increasing counter, a
  version number, or an index into a table that is never reused, A cannot come back and
  there is nothing to fix. **ABA is a hazard of value reuse**, and the cheapest fix is
  often "stop reusing values."

---

## 5. Tagged pointers, double-width CAS, and stealing address bits

### 5.1 The versioned (tagged) pointer

Attach a monotonically increasing counter to the pointer and CAS both together. Now
"A with tag 3" and "A with tag 6" are different values, and step 5 fails.

```c
typedef struct { node_t *ptr; uintptr_t tag; } tagged_t;   /* 16 bytes */
static _Atomic tagged_t head;

/* every successful modification bumps the tag */
tagged_t nw = { .ptr = next, .tag = old.tag + 1 };
atomic_compare_exchange_strong(&head, &old, nw);
```

Same forced schedule as §4.3, tagged version *(measured)*:

```console
$ ./aba tagged
== tagged pointer, same schedule ==
  atomic_is_lock_free(tagged_t{ptr,tag} = 16 bytes): 1
  T1 step1: read {ptr=0x100ee9e80 id=1, tag=3}
  T0       : head is now {ptr=0x100ee9e80 id=1, tag=6}
  T1 step5: CAS on {ptr,tag} -> failed  <-- ABA DEFEATED   (saw tag=6, wanted 3)
  after : head id=1  (correct: A(1), C still below it)
```

Note `atomic_is_lock_free` returned **1** for a 16-byte struct: clang lowered it to
`caspal` (§2), not to a hidden mutex. **Always check this.** On a target without
double-width CAS, `_Atomic` on a 16-byte struct silently becomes a libatomic
lock-protected sequence, and your "lock-free" stack is a lock-based stack with a global
lock table and worse constants.

### 5.2 Double-width CAS across ISAs

| ISA | Instruction | Notes |
|---|---|---|
| x86-64 | **`cmpxchg16b`** | Requires the `CX16` CPUID bit (universal since ~2006, but *absent on early AMD64*). Operands must be 16-byte aligned. `lock cmpxchg16b` is a full barrier. |
| x86-32 | `cmpxchg8b` | 64-bit; the original home of the 32-bit-pointer + 32-bit-tag trick. |
| ARMv8.1+ (LSE) | **`casp` / `caspa` / `caspl` / `caspal`** | Aligned register *pair*. What Apple Silicon and Graviton3+ use. |
| ARMv8.0 base | **`ldxp` / `stxp`** (and `ldaxp`/`stlxp`) | LL/SC on a 128-bit pair. Works, but a retry loop, and the exclusive monitor on a 16-byte granule is more easily disturbed. |
| RISC-V | *(no standard DWCAS)* | Base `A` extension has 64-bit `lr.d`/`sc.d` and `amoswap.d` only. Portable code cannot assume 128-bit atomics. |
| POWER | `lqarx` / `stqcx.` | 128-bit LL/SC, ISA 2.07+. |

**The portability trap:** `std::atomic<T>::is_lock_free()` / `atomic_is_lock_free` is the
only honest test, and it must be checked **on every target you ship to**, not on your
laptop. A structure that is lock-free on your M3 and lock-*based* on a RISC-V edge device
is a latency incident waiting for a customer.

### 5.3 Stealing bits from the pointer — and why it's fragile

If you cannot do a 128-bit CAS, you can hide the tag *inside* the pointer, because
pointers are not fully used:

```
 A 64-bit pointer on arm64 macOS, 16 KB pages:

  63          48 47                            14 13         0
  ┌─────────────┬────────────────────────────────┬────────────┐
  │  unused /   │      canonical VA bits         │ page offset│
  │  TBI / PAC  │                                │            │
  └─────────────┴────────────────────────────────┴────────────┘
        ▲                                            ▲
        │ "16 free high bits"                        │ "N free low bits if the
        │  — TBI ignores 63:56 on ARM                │  allocation is aligned"
        │  — but PAC (arm64e) SIGNS these            │  malloc aligns to 16 → 4 bits
        │  — and 5-level paging / LAM / MTE          │  cache-line align → 7 bits
        │    are all claiming them                   │  page align → 14 bits
```

- **Low bits are the safe ones.** If every node is `aligned_alloc(64, ...)`, the bottom 6
  bits are provably zero and yours. This is exactly what CPython does: `PyMutex` packs
  `_Py_LOCKED` and `_Py_HAS_PARKED` into the low bits of a byte, and `_PyRawMutex` packs
  a lock bit into the low bit of a *pointer* (§11.3). It is also what
  `Python/obmalloc.c` does — `free_delayed(((uintptr_t)ptr)|0x01, 0)` tags a pointer's
  low bit to distinguish a `PyObject*` from a raw block on the deferred-free queue
  *(verified in `Objects/obmalloc.c`)*.
- **High bits are a trap.** ARM's **Top-Byte-Ignore** makes bits 63:56 appear free, and
  then **PAC** (pointer authentication, arm64e) puts a cryptographic signature there and
  **MTE** (memory tagging) puts an allocation tag in 59:56. x86-64 was "48-bit
  addresses" until **5-level paging** made it 57, and **LAM** started masking high bits
  for its own purposes. Every generation of hardware has re-monetized those bits. Code
  that assumed 16 free high bits has been broken by: Solaris on SPARC (32-bit VA
  assumptions), Linux `mmap` above 47 bits, Apple's arm64e, and Intel LAM. This is not a
  hypothetical.
- **You get far fewer bits than you think.** With 6 low bits you have a **64-value tag**.
  A 64-wrap is a full ABA, and 64 operations on a hot stack takes microseconds. Tag width
  is a probability argument, not a proof — even at 64 bits, which is why §7 exists at all.

> **The honest summary of §5:** tagging converts ABA from "will happen" to "will happen
> after 2^k modifications." That is usually enough for a 64-bit tag and never enough for a
> 6-bit one. **And it does absolutely nothing about use-after-free.** A tagged pointer
> lets you safely *fail* a CAS; it does not let you safely *dereference* `old->next`. That
> is §7's job, and it is the harder one.

---

## 6. The Michael–Scott queue

Maged Michael and Michael Scott's 1996 PODC paper is the most-implemented lock-free
structure in existence — it is the basis of `java.util.concurrent.ConcurrentLinkedQueue`,
of Boost's lock-free queue, and of a thousand in-house work queues. Two properties make
it clever:

1. **A permanent dummy node** decouples `head` and `tail` so that an empty queue still
   has something for both to point at, and the enqueue and dequeue paths never CAS the
   same word.
2. **The tail is allowed to lag**, and any thread that notices a lagging tail **helps**
   advance it. That helping is precisely what makes the algorithm lock-free rather than
   obstruction-free: a stalled enqueuer cannot block anyone, because whoever arrives next
   finishes its work.

```
   A correctly-formed queue (dummy D, values 1 and 2):

      head ──▶ ┌───┐    ┌───┐    ┌───┐ ◀── tail
               │ D │───▶│ 1 │───▶│ 2 │───▶ NULL
               └───┘    └───┘    └───┘
                        ^^^^^ dequeue returns next->val and moves head to `next`;
                              the OLD head (D) becomes garbage. The new dummy is
                              node 1, whose value has been logically consumed.

   The INTERMEDIATE state — the whole reason this algorithm is subtle.
   Enqueue is TWO CASes and a thread may be descheduled between them:

      head ──▶ ┌───┐    ┌───┐    ┌───┐
               │ D │───▶│ 1 │───▶│ 2 │───▶ NULL
               └───┘    └───┘    └───┘
                          ▲        ▲
                        tail       │  CAS #1 (link) DONE
                        (lagging)  │  CAS #2 (swing tail) NOT DONE
                                   │
      Any thread — enqueuer or dequeuer — that sees tail->next != NULL
      must first do:  CAS(&tail, tail, tail->next)   ← "helping"
      before it can proceed. Nobody waits for the stalled thread.
```

The enqueue path, verbatim from the program I compiled and ran *(measured)*:

```c
static void q_enqueue(long v) {
    qnode_t *n = calloc(1, sizeof *n);
    n->val = v; atomic_store(&n->next, (qnode_t *)NULL);
    for (;;) {
        qnode_t *tail = atomic_load_explicit(&Q_tail, memory_order_acquire);
        qnode_t *next = atomic_load_explicit(&tail->next, memory_order_acquire);
        if (tail != atomic_load_explicit(&Q_tail, memory_order_acquire)) continue;
        if (next == NULL) {
            qnode_t *exp = NULL;
            if (atomic_compare_exchange_weak_explicit(&tail->next, &exp, n,
                    memory_order_release, memory_order_relaxed)) {
                /* CAS #2: swing the tail. Failure is FINE — someone helped. */
                atomic_compare_exchange_strong_explicit(&Q_tail, &tail, n,
                    memory_order_release, memory_order_relaxed);
                return;
            }
        } else {
            /* tail was lagging: help. This is what makes it lock-free. */
            atomic_compare_exchange_strong_explicit(&Q_tail, &tail, next,
                memory_order_release, memory_order_relaxed);
        }
    }
}
```

And the dequeue, with the two orderings that people get wrong:

```c
static int q_dequeue(long *out) {
    for (;;) {
        qnode_t *h    = atomic_load_explicit(&Q_head, memory_order_acquire);
        qnode_t *t    = atomic_load_explicit(&Q_tail, memory_order_acquire);
        qnode_t *next = atomic_load_explicit(&h->next, memory_order_acquire);
        if (h != atomic_load_explicit(&Q_head, memory_order_acquire)) continue;
        if (h == t) {
            if (next == NULL) return 0;                 /* genuinely empty */
            /* h == t but t->next != NULL: tail lags. Help, then retry. */
            atomic_compare_exchange_strong_explicit(&Q_tail, &t, next,
                memory_order_release, memory_order_relaxed);
        } else {
            long v = next->val;          /* READ BEFORE THE CAS — see below */
            if (atomic_compare_exchange_weak_explicit(&Q_head, &h, next,
                    memory_order_acquire, memory_order_relaxed)) {
                *out = v;
                /* `h` is garbage now. We deliberately LEAK it here. */
                return 1;
            }
        }
    }
}
```

**Why `v = next->val` must come before the CAS.** The instant the CAS succeeds, `next` is
the new dummy and another dequeuer may immediately consume it and free the node. Reading
`next->val` afterwards is a textbook use-after-free. This one line is the difference
between the paper's algorithm and the version in a hundred blog posts.

**Why the re-read `if (h != Q_head) continue;` exists.** `h`, `t`, and `next` are three
separate loads; without the validation you can act on a snapshot that never existed
simultaneously. This is a *snapshot* problem, not a barrier problem — `seq_cst` on all
three loads would not fix it.

Correctness check on this machine, 8 threads, every value enqueued once and required to be
dequeued exactly once *(measured)*:

```console
$ ./hp msq 8 2
msq   t=8  enq=100000 deq=100000  duplicates=0  missing=0  9.32 Mops/s -> PASS
$ ./hp_asan msq 8 2      # AddressSanitizer build
msq   t=8  enq=100000 deq=100000  duplicates=0  missing=0  9.32 Mops/s -> PASS
MSQ: ASAN CLEAN
```

ASan is clean **only because that comment says `we deliberately LEAK it`**. The moment you
add `free(h)` you have the §7 problem. Michael & Scott's original paper solved it with a
freelist plus tagged pointers, which bounds the leak but not the ABA window; Michael's
*next* paper — 2004, hazard pointers — is his own answer to his own 1996 problem. That
eight-year gap is the shape of this whole field.

Scaling, same machine *(measured, 2 s per point)*: 140.4 Mops/s at 1 thread, then
17.7 / 19.0 / 9.6 Mops/s at 2 / 4 / 8. **A 7.9× collapse from 1 to 2 threads.** Adding the
second thread does not halve throughput, it divides it by eight, and §8 explains why.

---

## 7. Reclamation is the real problem

Restate it starkly:

> A thread executing `pop()` holds a raw pointer `old` to a node it has *not yet removed*.
> Between its load of `head` and its CAS, any number of other threads may remove and free
> that node. Its next instruction — `old->next` — reads freed memory. **There is no
> ordering, no barrier, and no CAS that fixes this**, because the problem is not
> visibility or atomicity. It is *lifetime*.

Lock-based code has this problem solved for free: you hold the lock, so nobody can free
anything. Remove the lock and you have to reintroduce lifetime management by hand. Garbage
collection solves it for free too — which is why lock-free structures are *drastically*
easier in Java, Go, and C# than in C, C++, or Rust, and why "just port the Java version"
is a plan that ends badly.

### 7.0 The failure, reproduced

Same Treiber stack, threads calling `free()` on the node they popped, 8 threads, 1 second
*(measured, plain `-O2` build, three consecutive runs)*:

```console
$ ./hp naive 8 1 ; echo exit=$?     →  exit=134   (SIGABRT: malloc metadata corruption)
$ ./hp naive 8 1 ; echo exit=$?     →  exit=133   (SIGTRAP)
$ ./hp naive 8 1 ; echo exit=$?     →  exit=139   (SIGSEGV)
```

Three runs, three *different* signals. That is the signature of heap corruption: the crash
site is unrelated to the bug site, and the symptom is unstable. Under ASan the diagnosis
is instant *(measured)*:

```console
$ ./hp_asan naive 8 1
==61301==ERROR: AddressSanitizer: heap-use-after-free on address 0x602000006010
READ of size 8 at 0x602000006010 thread T6
    #0 0x000100b4da90 in worker hp.c:185        ← `node_t *next = old->next;`
freed by thread T3 here:
    #0 free  #1 0x000100b4d638 in worker hp.c:187
previously allocated by thread T6 here:
    #0 malloc #1 0x000100b4d790 in worker hp.c:192
```

Read the thread ids: **T6 is reading memory that T3 freed.** That is the whole problem in
one line of sanitizer output.

Every scheme below is an answer to: *how does a would-be reclaimer learn that no reader
holds a pointer to this node?*

### 7.1 Hazard pointers (Michael, 2004)

**Idea: readers announce what they are about to dereference; reclaimers check the
announcements.**

Each thread owns a small fixed number K of single-writer, multi-reader slots. Before
dereferencing a node, a thread **publishes** the pointer into a slot, then **re-validates**
that the node is still reachable. Reclaimers scan all slots and free only unannounced
nodes.

```
   Thread 2 popping                   The published hazard array
   ─────────────────                  (single-writer per row, everyone reads)
   h = load(head)      ──────────▶    ┌────────────────────────────────┐
   hp[me][0] = h       ──publish──▶   │ T0: [ 0x…a10 ][  NULL  ]       │
   if (load(head) != h) retry ←validate│ T1: [  NULL  ][  NULL  ]       │
   next = h->next      ← now SAFE     │ T2: [ 0x…b40 ][  NULL  ]  ◀─ me│
   CAS(head, h, next)                 │ T3: [ 0x…a10 ][ 0x…c88 ]       │
   hp[me][0] = NULL                   └────────────────────────────────┘
                                                 ▲
   Reclaimer (any thread):                       │
     retire(n) → thread-local list               │  scan when the local list
     when |list| >= R:  ───────────────────────▶ │  exceeds R = 2·threads·K
       snapshot every non-NULL slot
       free(x) for every retired x NOT in the snapshot
       keep the rest for next time
```

The **publish-then-validate** order is the entire correctness argument, and it needs a
`seq_cst` store (or an explicit fence) between the two:

```c
static node_t *s_pop_hp(int tid) {
    for (;;) {
        node_t *h = atomic_load_explicit(&head, memory_order_acquire);
        if (!h) return NULL;
        atomic_store(&hprec[tid].hp[0], h);        /* seq_cst PUBLISH */
        if (atomic_load_explicit(&head, memory_order_acquire) != h) continue;  /* VALIDATE */
        node_t *next = h->next;                    /* now provably safe */
        if (atomic_compare_exchange_weak_explicit(&head, &h, next,
                memory_order_acquire, memory_order_relaxed)) {
            atomic_store(&hprec[tid].hp[0], NULL);
            return h;
        }
    }
}
```

If you relax that store to `release`, the store can sink below the re-load on ARM and the
protocol silently breaks: a reclaimer scans, sees an empty slot, frees the node, *then*
your publish lands. Doc [`02`](02-atomics-and-memory-models.md)'s store-buffer discussion
is not academic here — this is a real, shipped-in-production class of bug.

**Properties, measured on this machine** *(measured, 2 s per point)*:

| Threads | leak (no reclamation) | hazard pointers | max nodes held back |
|---|---|---|---|
| 1 | 83.48 Mops/s | 56.79 Mops/s (−32%) | 0 |
| 2 | 9.70 | 8.17 | 1 |
| 4 | 4.49 | 5.22 | 3 |
| 8 | 2.05 | 2.14 | 3 |

Two findings worth more than the throughput column:

1. **The single-threaded cost of hazard pointers is real and large: −32%.** It is two
   extra stores and a redundant load on the hottest path, and on a weakly-ordered machine
   one of those stores is a full fence.
2. **The memory bound is spectacular: never more than 4 nodes deferred.** That is the
   defining property — hazard pointers give **hard-bounded** deferred memory (at most
   `R` per thread, `R` chosen by you), because a reclaimer only ever waits on pointers
   that are *currently published*. Compare with epochs below.

### 7.2 Epoch-based reclamation (Fraser, 2004)

**Idea: don't track pointers, track time.** A global epoch counter advances only when
every active thread has been observed in the current epoch. Anything retired in epoch *X*
is free once the global epoch reaches *X+2*.

```
      global epoch:   ...  X ─────────────▶ X+1 ─────────────▶ X+2
                            │                │                  │
   T0  ──[enter X]──[exit]──┼──[enter X+1]───┼──[exit]──────────┼──▶
   T1  ────[enter X]────────┼────────[exit]──┼──[enter X+2]─────┼──▶
   T2  ──[OFFLINE — does not block advancement]──────────────────────▶
                            │                │                  │
   retire(N) during X ──────┴────────────────┴──────────────────┴──▶ free(N) is
                                                                     safe HERE
   Why X+2 and not X+1?  Advancing X→X+1 only proves every thread was *at* X.
   Advancing X+1→X+2 proves every thread has since had a quiescent point.
   Only then can no reader still hold a pointer obtained during X.
```

The advantages over hazard pointers are exactly the mirror image: **the read path is
nearly free** (one load of the epoch, one store of your local epoch, per *critical
section* rather than per *pointer*), and it protects an unbounded number of pointers at
once — you can traverse a whole list under one epoch entry.

The disadvantage is the reason RCU-style schemes have a bad reputation in some shops:
**memory is unbounded.** One thread that enters an epoch and then blocks — on I/O, on a
page fault, on a `futex`, on a debugger breakpoint — stalls epoch advancement for
*everyone*, and every retired object in the process piles up. Measured here *(measured)*:

| Threads | EBR Mops/s | epoch advances | **max nodes held back** |
|---|---|---|---|
| 1 | 42.33 | 2,650,323 | 32 |
| 2 | 8.58 | 535,990 | **2,048** |
| 4 | 4.17 | 254,298 | **1,837** |
| 8 | 2.42 | 122,562 | **1,542** |

Against hazard pointers' 0–3, that is a **500×** difference in retained memory on an
identical workload. This is the single most important line in the comparison table in
§7.7, and it is the reason the Linux kernel added `rcu_barrier`, OOM-triggered
`expedited` grace periods, and a whole subsystem of "RCU stall" detection.

### 7.3 The bug AddressSanitizer found in my epoch reclaimer

**This section is here because the honest version of this document requires it.**

My first EBR implementation was 40 lines, structurally identical to the textbook
presentation, and I believed it was correct. AddressSanitizer disagreed within two
seconds *(measured)*:

```console
$ ./hp_asan ebr 8 2
==62555==ERROR: AddressSanitizer: heap-use-after-free on address 0x602005a33670
READ of size 8 at 0x602005a33670 thread T4
    #0 0x000104c29ea8 in worker hp.c:234
freed by thread T1 here: ...
```

Two independent defects, both of which I had to *reason* my way to — no amount of staring
at the code revealed them, and no amount of testing at 2 threads would have:

**Defect 1 — publishing a stale epoch.** I wrote `ep_enter` as "load the global epoch,
store it to my slot." I assumed a stale published value was merely *conservative* (it
would block advancement, costing memory but not safety). Wrong. With three epoch slots and
`bag[epoch % 3]`, a thread descheduled between the load and the store can publish an epoch
**three behind**, at which point the modular index **aliases the bag being filled right
now**. Fix: re-read and retry until the published value is current.

```c
static inline void ep_enter(int tid) {
    unsigned long e;
    do {
        e = atomic_load_explicit(&g_epoch, memory_order_acquire);
        atomic_store(&eprec[tid].local, e);              /* seq_cst */
    } while (atomic_load_explicit(&g_epoch, memory_order_acquire) != e);
}
```

**Defect 2 — reclaiming against a stale epoch after a lost CAS.** My advance routine
freed `bag[(e+1) % 3]` whether or not its own CAS on the global epoch succeeded. When
another thread wins that CAS, `e` is stale and `bag[(e+1) % 3]` is the *live* bag.

**Defect 3, the one that survived both fixes.** Even with those corrected, a thread that
advances only every 64 retirements can fall three or more epochs behind while *other*
threads drive the counter — and then bag slots from epoch *X* and epoch *X+3* alias in the
same modular slot. ASan kept failing. The real fix was to abandon modular indexing
entirely and **tag each bag with the epoch it was filled in**, freeing a bag only when
`global_epoch >= bag.epoch + 2`:

```c
typedef struct { void *r[2048]; int n; unsigned long epoch; int used; } bag_t;
static _Thread_local bag_t bags[NBAGS];

static void ep_drain(void) {
    unsigned long g = atomic_load_explicit(&g_epoch, memory_order_acquire);
    for (int i = 0; i < NBAGS; i++)
        if (bags[i].used && g >= bags[i].epoch + 2) {
            for (int j = 0; j < bags[i].n; j++) free(bags[i].r[j]);
            bags[i].n = 0; bags[i].used = 0;
        }
}
```

After that, three consecutive ASan runs at 8 threads were clean.

> **What to take from this.** I have read this literature. I knew about the X+2
> invariant, I wrote the publish-validate protocol correctly for hazard pointers on the
> first try, and I still shipped three use-after-free bugs into a 40-line epoch
> reclaimer — in a document whose explicit purpose is to explain epoch reclamation. Every
> one of them was invisible at 2 threads and instantly fatal at 8. **This is the strongest
> argument in this document for §10.** If you are about to write reclamation code, the
> correct next step is not "be more careful"; it is "use a library that has had ten years
> of eyes on it," and if you must write it, **run it under ASan/TSan at more threads than
> you have cores, in CI, every commit.** That is not a nice-to-have. It is the only reason
> I know my second version is right, and I am still not certain it is.

### 7.4 QSBR — quiescent-state-based reclamation

QSBR is EBR with the critical sections removed. Instead of `enter()`/`exit()` around each
operation, the application promises: **"at these specific points, I hold no pointers to
shared data."** Those points are *quiescent states*, and a **grace period** is an interval
during which every thread has passed through at least one.

```
                 grace period for objects retired at ▼
   T0 ──●────────●────────●─────────●──────●───────────●──▶   ● = quiescent state
   T1 ──────●───────────●──────────────●─────────●────────▶
   T2 ──●──────────●────────────────────────●─────────●───▶
                          │                       │
                          └───────────────────────┘
                    every thread reported at least once
                    ⇒ nothing retired before the left edge
                      can still be referenced ⇒ free it
```

Read cost is **zero on the fast path** — literally nothing, no fence, no store — which is
its whole selling point. The price: it needs a *natural* place in the program where
threads are provably pointer-free, and it needs threads to *reach* it. A thread in a long
computation never becomes quiescent, and reclamation stalls.

CPython has exactly such a natural place — **the eval breaker** — which is why QSBR is
what PEP 703 chose. §11 walks the real implementation.

### 7.5 RCU (McKenney & Slingwine, 1998)

**Read-Copy-Update** is QSBR plus a discipline for updates. The name is the algorithm:
to modify a shared structure, **Read** it, **Copy** the part you're changing, **Update**
the copy, then publish the new version with a single pointer store; readers see either the
old or the new version, never a torn one. The old version is retired and freed after a
grace period.

The Linux kernel's classic-RCU read side is:

```c
rcu_read_lock();                     /* in a non-preemptible kernel: a NO-OP */
p = rcu_dereference(gp);             /* one load + a dependency-ordering barrier */
do_something(p->field);
rcu_read_unlock();                   /* also a no-op */
```

`rcu_read_lock()` compiles to **nothing** in `CONFIG_PREEMPT_NONE` builds — the grace
period is inferred from context switches, which are already tracked. That is the most
extreme point on the read-cost/write-cost trade-off curve in existence: readers pay
literally zero, writers pay a full grace period (milliseconds), and memory is unbounded
during a stall.

Two things engineers get wrong about RCU:

- **`synchronize_rcu()` blocks, `call_rcu()` doesn't.** The blocking form is a grace-period
  wait that can take many milliseconds; using it on a hot path is a classic kernel
  performance bug.
- **RCU is not a general-purpose lock replacement.** It is optimal for *read-mostly*
  structures where writers are rare and readers are hot — routing tables, security policy,
  module lists. It is terrible for a work queue. Choosing RCU for a write-heavy structure
  produces the worst of both worlds.

### 7.6 Deferred and reference-counted reclamation

Two more families, for completeness, because they are what most *real* systems actually
use:

**Reference counting on the nodes themselves.** Attach a refcount to each node; a reader
increments before dereferencing, decrements after. The problem is immediate and fatal:
**to safely increment the refcount you must first safely dereference the node**, which is
the original problem. Solutions exist — split reference counting, `atomic_shared_ptr`,
Herlihy's "lock-free reference counting," and the DWCAS-based counted pointers — and all
of them are slower than hazard pointers, because every read is now a contended atomic RMW
on a shared line ([`01`](01-memory-hierarchy-and-caches.md) §5: **one writer among N
readers destroys the scaling of all N**). This is also, precisely, why CPython's refcount
is the GIL's root cause ([`24`](24-the-gil.md) §1).

**Deferred free / "just wait for the GC."** Hand retired nodes to an existing safepoint
mechanism you already pay for. This is what CPython does as a *second* tier: some
free-threaded reclamation is deferred to the next cycle-collection pause (§11.6), and
`Python/qsbr.c`'s deferred-advance optimization (§11.2) is a deliberate batching of the
same idea. In a managed runtime this is nearly free; in C it means adopting a GC.

### 7.7 The comparison table

The table this whole section exists to produce. Read the first two columns together —
**every scheme trades bounded memory against read cost, and there is no row that wins
both.**

| Scheme | Bounded memory? | Read-side cost | Write/reclaim cost | Complexity | Fails when |
|---|---|---|---|---|---|
| **Never free** | ❌ leaks forever | none | none | trivial | always, eventually |
| **Tagged pointers only** | ❌ (still no lifetime) | none | one extra word in the CAS | low | you dereference a retired node |
| **Reference counting** | ✅ tight | **atomic RMW per read** — contended | atomic RMW | medium | read-heavy sharing; cycles |
| **Hazard pointers** (Michael 2004) | ✅ **hard bound** (`R` per thread) | 1 seq_cst store + 1 re-load **per pointer** | O(threads × K) scan, amortized | **high** — one slot per live pointer, and you must get publish-validate exactly right | you forget a slot, or relax the store |
| **EBR** (Fraser 2004) | ❌ unbounded | 1 load + 1 store **per critical section** | epoch scan; amortized | medium-high | one blocked thread stalls all reclamation |
| **QSBR** | ❌ unbounded | **zero** on the fast path | scan all threads for min sequence | medium | no natural quiescent point; long-running threads |
| **RCU** (McKenney 1998) | ❌ unbounded | **zero** (non-preemptible kernels) | grace period: ms | medium (as a user); very high (as an implementer) | write-heavy workloads |
| **Defer to GC / safepoint** | ⚠️ bounded by GC period | none | pays for a GC you already run | low **if you have a GC** | you don't have a GC |

**Measured on this machine, same Treiber-stack workload, 8 threads** *(measured)*:

| | throughput | max nodes deferred |
|---|---|---|
| no reclamation (leak) | 2.05 Mops/s | — |
| hazard pointers | 2.14 Mops/s | **3** |
| epoch-based | 2.42 Mops/s | **1,542** |

> **An honest caveat on those throughput numbers.** At ≥2 threads all three converge,
> because this microbenchmark `malloc`s and `free`s a node per iteration and the
> *allocator*, not the reclamation scheme, becomes the bottleneck. The differences that
> matter here are in the **single-threaded** column (leak 83.5 → HP 56.8 → EBR 42.3 Mops/s
> — i.e. read-side cost, which is what the table claims) and in the **deferred-memory**
> column (3 vs 1,542 — i.e. the memory bound, which is the other thing the table claims).
> A throughput comparison of reclamation schemes needs a workload where reclamation, not
> `malloc`, dominates; I did not build one, and I am not going to pretend the ≥2-thread
> rows say anything. See Hart, McKenney, Brown & Walpole (2007) for the study that does
> this properly.

---

## 8. Lock-free is usually slower than a lock — measured

This is the section that changes people's behaviour, so it is all measurement.

Same workload — N threads each doing `push(); pop();` on one shared stack — implemented
five ways. **Two independent runs**, 2 s each, reported side by side so you can see the
noise *(measured, Apple M3 Pro, 5 P + 6 E cores, unpinned)*. Throughput in **Mops/s**,
higher is better:

| Threads | `shard` (no sharing) | `mutex` (pthread) | `treiber` (pure CAS) | `backoff` (CAS + exp. backoff) | `elim` (elimination) |
|---|---|---|---|---|---|
| 1  | 2846 / 2558 | 196 / 172 | **260 / 178** | 301 / 345 | 263 / 269 |
| 2  | 5408 / 4546 | 66 / 64 | **49 / 54** | 290 / 297 | 260 / 281 |
| 4  | 10808 / 7562 | 42 / 36 | **23 / 25** | 36 / 313 | 244 / 266 |
| 8  | 15119 / 11127 | 50 / 43 | **22 / 20** | 137 / 45 | 251 / 222 |
| 11 | 17062 / 12147 | 54 / 51 | **20 / 22** | 44 / 75 | 39 / 35 |

Five conclusions, in order of how often they surprise people:

**1. The naive lock-free stack is 2× SLOWER than a `pthread_mutex` at 4+ threads.**
23 vs 42 Mops/s at 4 threads; 20 vs 54 at 11. This is not a strawman mutex — it is the
platform mutex, and it wins. The reason is [`01`](01-memory-hierarchy-and-caches.md) §5:
under a mutex, the winner holds the head's cache line in **M** state for the whole
critical section and the losers are *parked in the kernel, generating no coherence
traffic at all*. Under pure CAS, every loser is spinning in a retry loop, and **every
retry is a full request-for-ownership that steals the line from the thread trying to make
progress**. Contention makes the lock-free version actively self-destructive.

**2. Lock-free wins only when uncontended.** At 1 thread: 260 vs 196 Mops/s, a ~1.3×
edge, because a CAS is cheaper than lock/unlock. That is a real and reproducible win, and
it is the *only* throughput win in the table.

**3. Nothing scales.** Look across any row. `treiber` goes 260 → 49 → 23 → 22 → 20. The
mutex goes 196 → 66 → 42 → 50 → 54. **Both are worse at 11 threads than at 1.** The
shared head is a single cache line; the maximum aggregate rate is the rate at which one
128-byte line can migrate between cores, and no algorithm beats physics. Whatever you
imagine "lock-free scalability" means, it is not this.

**4. The only thing that actually scales is not sharing.** `shard` — each thread on its
own private stack, identical work — runs at **2,846 Mops/s at one thread and 17,062 at
eleven**, a 6× speedup and a **~780× advantage over the lock-free version at 11 threads.**
If you have a contended data structure, the highest-value change available to you is
almost never "make it lock-free." It is "make it not shared": shard it, batch it,
thread-local it, and combine at the end.

**5. Backoff is bimodal and unstable.** Look at `backoff` at 4 threads: **36 in one run,
313 in the other** — a 9× run-to-run spread. Backoff works by *deliberately introducing
delay so one thread can complete a burst while holding the line*; whether that happens
depends on core placement, and on this heterogeneous machine placement is a coin flip.
Elimination is far steadier (244–281 across 2–8 threads) until 11 threads oversubscribe
the machine and it falls off a cliff too.

> **The heterogeneous-core caveat, stated plainly.** These numbers were taken on a laptop
> with 5 P-cores and 6 E-cores, unpinned, with no `perf(1)` available. The **direction and
> magnitude** of every effect above (lock-free loses under contention; sharding wins by
> orders of magnitude; backoff is bimodal) reproduced across runs. The **absolute values**
> should not be quoted. Re-run this yourself on your deployment target — that is lab 5 in
> §12, and it is the point.

**Retry storms, named.** The mechanism behind row 1 deserves a name because you will
diagnose it by symptom:

```
   N threads CAS-ing one line, no backoff:

   T0 ──[RFO]──[CAS ✓]───────────────────────────────────────▶  1 winner
   T1 ──[RFO]──[CAS ✗]──[RFO]──[CAS ✗]──[RFO]──[CAS ✗]───────▶  N−1 losers,
   T2 ──[RFO]──[CAS ✗]──[RFO]──[CAS ✗]──[RFO]──[CAS ✗]───────▶  each stealing
   T3 ──[RFO]──[CAS ✗]──[RFO]──[CAS ✗]──[RFO]──[CAS ✗]───────▶  the line from
                                                                the winner
   Useful work: 1 op.   Coherence transactions: 4N.   Wall time: worse than N=1.
```

**Symptom to recognize in production:** CPU utilization near 100%, throughput *falling* as
you add threads or cores, no lock in the profile, and time attributed to an
innocuous-looking atomic instruction. That is a retry storm (or its cousin, refcount
contention — [`24`](24-the-gil.md) §7, where it cost the Gilectomy 30% and *got worse with
more cores*). It looks nothing like lock contention on a flame graph, and telling them
apart is a genuine staff-level skill.

---

## 9. Backoff and the elimination-backoff stack

### 9.1 Backoff

If the failure mode is "everyone retries immediately and nobody makes progress," the fix
is Ethernet's fix: **exponential backoff**. On CAS failure, wait a randomized interval
drawn from a window that doubles, capped.

```c
static void bo_delay(unsigned *limit) {
    unsigned n = xorshift() % (*limit ? *limit : 1);
    for (unsigned i = 0; i < n; i++) cpu_pause();     /* arm64: `isb`; x86: `pause` */
    if (*limit < 4096) *limit <<= 1;
}
```

Three implementation notes that are not optional:

- **Randomize.** Deterministic backoff resynchronizes the threads into lockstep and you
  get the same storm one step later.
- **Use the ISA's pause hint.** x86 has `pause` (`rep nop`), which yields SMT resources and
  avoids a memory-order-violation pipeline flush on exit from the spin. ARM has `yield`,
  `wfe`, and `isb`; the code above uses `isb` because it is a reliable, cheap
  serialization point on Apple Silicon. Do **not** spin on an empty `for` loop — the
  compiler will delete it or the core will burn power for nothing.
- **Use a thread-local PRNG.** My first version called `rand()`, which has global state.
  Adding a hidden shared mutex to your contention-avoidance code is a very funny bug to
  find in a profile.

Measured effect *(measured, from §8)*: at 2 threads, backoff took the Treiber stack from
**49 → 290 Mops/s**, a 6× improvement. At 4–11 threads it became bimodal and unreliable
(36–313 Mops/s). **Backoff is a real and large win, and it is not a fix** — it converts a
throughput collapse into a latency lottery.

### 9.2 The elimination-backoff stack (Hendler, Shavit & Yerushalmi, 2004)

The elegant idea. Instead of *waiting* when the CAS fails, use the wait productively:
a `push` and a `pop` that collide can **cancel each other out** without ever touching the
stack. The stack's semantics permit it — a push immediately followed by a pop of the same
value is indistinguishable, to any observer, from neither happening.

```
        contended head                    elimination array
        ┌──────────┐                 ┌────┬────┬────┬────┬────┐
   ┌───▶│  head    │                 │ s0 │ s1 │ s2 │ s3 │ .. │  each slot on its
   │    └──────────┘                 └────┴──▲─┴────┴────┴────┘  own cache line
   │  CAS fails                              │
   │                            pusher offers│its node here,
   │                            spins briefly, then withdraws
   │                                         │
   └── on failure, pick a RANDOM slot ───────┘
                                             ▲
                            popper looks in a random slot and,
                            if it finds an offer, TAKES it — and
                            both operations complete having never
                            touched `head` at all.
```

The magic property: **elimination gets *better* as contention increases**, because
collisions are what it feeds on. That inverts the usual curve, and it is why the measured
`elim` row in §8 is nearly flat from 1 to 8 threads (263 → 260 → 244 → 251 Mops/s) where
`treiber` falls off a cliff (260 → 49 → 23 → 22).

The withdrawal protocol is where the subtlety lives:

```c
/* pusher */
node_t *empty = NULL;
if (CAS(slot, &empty, my_node)) {                    /* offer */
    for (int i = 0; i < SPIN; i++)
        if (load(slot) != my_node) return DONE;      /* a popper took it */
    node_t *mine = my_node;
    if (!CAS(slot, &mine, NULL))                     /* withdraw */
        return DONE;                                 /* taken as we withdrew! */
}
fall_back_to_the_stack();
```

**A failed withdrawal means success.** If `CAS(slot, my_node, NULL)` fails, the slot no
longer holds my node, and the only agent that can have changed it from `my_node` is a
popper who took it. That inverted return is the single most error-prone line in the
algorithm, and getting it backwards produces a duplicated node that behaves exactly like
the ABA bug in §4.

Caveats, honestly:

- **Elimination only works for structures with cancelling operation pairs.** Stacks: yes.
  Counters: yes (increment/decrement). FIFO queues: **no** — eliminating an enqueue
  against a dequeue violates FIFO order unless the queue is empty, and detecting that
  safely is its own problem.
- **Sizing the array is a tuning problem** — too small and you re-serialize on the slots,
  too large and colliders never find each other. It wants to be adaptive.
- At 11 threads on this machine the elimination row collapsed to 39/35 Mops/s. Once you
  oversubscribe the cores, an offering thread gets descheduled mid-spin and the whole
  mechanism degrades to backoff plus overhead.

---

## 10. When not to write lock-free code (almost always)

The decision procedure, in order. Do not skip a step.

```
 ┌─ Is this actually your bottleneck?  Have you PROFILED it? ───────────────┐
 │  NO  → stop. You are about to spend two weeks on 0.3% of your runtime.   │
 └──────────────────────────────┬──────────────────────────────────────────┘
                                │ YES
 ┌──────────────────────────────▼──────────────────────────────────────────┐
 │ Can you STOP SHARING?  Shard per thread/core, batch, thread-local +      │
 │ combine, partition by key, one queue per consumer.                       │
 │  → §8 measured this at 780× versus the lock-free version. Take it.       │
 └──────────────────────────────┬──────────────────────────────────────────┘
                                │ genuinely must share
 ┌──────────────────────────────▼──────────────────────────────────────────┐
 │ Can you shrink the critical section, or split one lock into many?        │
 │ Per-bucket locks, striped locks, reader-writer locks, seqlocks for       │
 │ read-mostly data. This is where CPython went: PER-OBJECT locks (§11).    │
 └──────────────────────────────┬──────────────────────────────────────────┘
                                │ still contended
 ┌──────────────────────────────▼──────────────────────────────────────────┐
 │ Is there a well-tested LIBRARY?  folly (F14, MPMCQueue, hazptr),         │
 │ Boost.Lockfree, liburcu, crossbeam / seize (Rust), java.util.concurrent, │
 │ moodycamel::ConcurrentQueue, C++26 std::hazard_pointer / rcu.            │
 │  → Use it. Someone else has already paid the §7.3 tax.                   │
 └──────────────────────────────┬──────────────────────────────────────────┘
                                │ no library fits
 ┌──────────────────────────────▼──────────────────────────────────────────┐
 │ Do you have a HARD requirement lock-freedom uniquely satisfies?          │
 │  • signal handler / async-signal-safety                                  │
 │  • hard real-time or audio deadline (priority inversion is fatal)        │
 │  • shared memory across processes that may crash                         │
 │  • reading another thread's state at an arbitrary instant (a profiler)   │
 │  NO → use a lock. You have exhausted the good options and the lock wins. │
 │  YES → proceed, with the checklist below.                                │
 └─────────────────────────────────────────────────────────────────────────┘
```

**If you get to the bottom, the entry fee is:**

- A **written reclamation plan** before a line of code. "We'll figure out freeing later"
  means you have not designed the thing.
- **ASan + TSan in CI, every commit, at 2–4× your core count.** §4.3 and §7.3 both show
  the bug rate is a step function of thread count. A 2-thread test proves nothing.
- **A model check.** CDSChecker, GenMC, Loom (Rust), or a hand-written exhaustive
  interleaving harness for the small cases. Ideally a TLA+/PlusCal spec for the protocol.
- **A stress test that runs for hours**, on the weakest-ordered hardware you ship to
  (ARM, not x86 — code that is accidentally correct on x86-TSO breaks here, per
  [`02`](02-atomics-and-memory-models.md)).
- **The `atomic_is_lock_free` assertion** for every DWCAS target you build for (§5.2).
- **A second reviewer who has done this before.** Not a rubber stamp — this code cannot be
  reviewed by someone learning the technique from the diff.

If that list looks disproportionate for a work queue, that is the correct reaction, and it
is the argument.

> **The meta-lesson, and it is the same one as [`24-the-gil.md`](24-the-gil.md) §8.6:**
> the Gilectomy tried to invent a mechanism and failed; PEP 703 assembled proven ones and
> succeeded. In lock-free programming the equivalent move is: **take the algorithm from
> the paper, take the reclamation from a library, and take the lock wherever you can get
> away with it.**

---

## 11. CPython: `qsbr.c`, `PyMutex`, and the free-threaded build

Now the payoff. Free-threaded CPython (PEP 703, officially supported since 3.14 via
PEP 779) is a large, production system that had to solve every problem in this document.
What it chose is instructive precisely because of how *little* lock-free data structure it
contains.

### 11.1 `Python/qsbr.c` exists — here is what is actually in it

**Verified.** `Python/qsbr.c` is 291 lines on `main`, with `Include/internal/pycore_qsbr.h`
(173 lines) and a design document at `InternalDocs/qsbr.md` (153 lines). The file header
says, verbatim:

```c
/*
 * Implementation of safe memory reclamation scheme using
 * quiescent states.  See InternalDocs/qsbr.md.
 *
 * This is derived from the "GUS" safe memory reclamation technique
 * in FreeBSD written by Jeffrey Roberson. It is heavily modified. Any bugs
 * in this code are likely due to the modifications.
 *
 * The original copyright is preserved below.
 *
 * Copyright (c) 2019,2020 Jeffrey Roberson <jeff@FreeBSD.org>
 */
```

So the provenance line in [`24-the-gil.md`](24-the-gil.md) §8.6 ("QSBR — from FreeBSD") is
literally true, down to the BSD licence text carried in-tree. FreeBSD calls the technique
**GUS — Global Unbounded Sequences** (`sys/kern/subr_smr.c`).

The mechanism is §7.4 with sequence numbers instead of epochs *(all names verified in
`Include/internal/pycore_qsbr.h`)*:

```c
#define QSBR_OFFLINE 0
#define QSBR_INITIAL 1
#define QSBR_INCR    2

/* Wrap-around safe comparison — a holdover from FreeBSD's 32-bit sequences. */
#define QSBR_LT(a, b)  ((int64_t)((a)-(b)) < 0)
#define QSBR_LEQ(a, b) ((int64_t)((a)-(b)) <= 0)

struct _qsbr_shared {              /* per interpreter */
    uint64_t wr_seq;               /* write sequence: always ODD, +2 each advance */
    uint64_t rd_seq;               /* min observed read sequence of all threads */
    struct _qsbr_pad *array;       /* per-thread states, 64-byte aligned */
    void *array_raw;
    Py_ssize_t size;
    PyMutex mutex;                 /* guards the freelist */
    struct _qsbr_thread_state *freelist;
};

struct _qsbr_thread_state {        /* per thread */
    uint64_t seq;                  /* last observed write seq, or 0 == OFFLINE */
    struct _qsbr_shared *shared;
    PyThreadState *tstate;
    int    deferred_count;         /* items retired since our last advance */
    size_t deferred_memory;        /* estimated bytes held back */
    size_t deferred_page_memory;   /* mimalloc pages held back */
    bool   should_process;
    bool   allocated;
    struct _qsbr_thread_state *freelist_next;
};

struct _qsbr_pad {                 /* padding to avoid false sharing */
    struct _qsbr_thread_state qsbr;
    char __padding[64 - sizeof(struct _qsbr_thread_state)];
};
```

Six details worth stopping on:

**1. `wr_seq` is always odd, incremented by two.** `QSBR_OFFLINE` is `0`. Because a valid
sequence is always odd, it can never collide with the offline marker *even if the counter
wraps* — which is a §4.4-style "make the value un-reusable" ABA avoidance for sequence
numbers. The header says so explicitly.

**2. The wrap-safe comparison macros.** `QSBR_LT(a,b)` is a signed subtraction, not `a<b`.
That is the standard TCP-sequence-number trick, inherited from FreeBSD's 32-bit
implementation. CPython uses 64-bit sequences and the code comments admit the macros are
now belt-and-braces: *"We currently use 64-bit sequence numbers, so wrap-around is
unlikely."*

**3. `struct _qsbr_pad` and the 64-byte alignment are false-sharing defence**, exactly
[`01`](01-memory-hierarchy-and-caches.md) §6. `grow_thread_array()` even over-allocates:

```c
// Overallocate by 63 bytes so we can align to a 64-byte boundary.
// This avoids potential false sharing between the first entry and other
// allocations.
size_t alignment = 64;
size_t alloc_size = (size_t)new_size * sizeof(struct _qsbr_pad) + alignment - 1;
```

*(Worth flagging: that is **64**, and this machine's cache line is **128**. Per
[`01`](01-memory-hierarchy-and-caches.md) §6 the padding may therefore be insufficient on
Apple Silicon. I have not measured whether it matters — the array is scanned, not hammered
— but it is exactly the class of platform assumption that document warns about.)*

**4. The poll is a linear scan of all threads**, and it is honest about the consequence:

```c
static uint64_t
qsbr_poll_scan(struct _qsbr_shared *shared)
{
    // Synchronize with store in _Py_qsbr_attach(). We need to ensure that
    // the reads from each thread's sequence number are not reordered to see
    // earlier "offline" states.
    _Py_atomic_fence_seq_cst();

    uint64_t min_seq = _Py_atomic_load_uint64(&shared->wr_seq);
    struct _qsbr_pad *array = shared->array;
    for (Py_ssize_t i = 0, size = shared->size; i != size; i++) {
        struct _qsbr_thread_state *qsbr = &array[i].qsbr;
        uint64_t seq = _Py_atomic_load_uint64(&qsbr->seq);
        if (seq != QSBR_OFFLINE && QSBR_LT(seq, min_seq)) {
            min_seq = seq;
        }
    }
    uint64_t rd_seq = _Py_atomic_load_uint64(&shared->rd_seq);
    if (QSBR_LT(rd_seq, min_seq)) {
        // It's okay if the compare-exchange failed: another thread updated it
        (void)_Py_atomic_compare_exchange_uint64(&shared->rd_seq, &rd_seq, min_seq);
        rd_seq = min_seq;
    }
    return rd_seq;
}
```

That leading `_Py_atomic_fence_seq_cst()` is the *same* correctness requirement as the
`seq_cst` publish in my hazard-pointer code (§7.1) and the same one my EBR got wrong
(§7.3): **the reclaimer's reads of thread state must not be reordered before the reader's
publish.** And note `_Py_qsbr_attach` stores with an explicit comment `// needs seq_cst`.
This is not decoration; it is the protocol.

`InternalDocs/qsbr.md` names the scan as the known scaling limit: *"Determining the
`rd_seq` requires scanning over all thread states. This operation could become a
bottleneck in applications with a very large number of threads (e.g., >1,000)."* That is
the §7.7 table's "write/reclaim cost: O(threads)" row, admitted in-tree.

**5. Growing the thread array takes a stop-the-world pause.** `_Py_qsbr_reserve()`:

```c
if (qsbr == NULL) {
    _PyEval_StopTheWorld(interp);
    if (grow_thread_array(shared) == 0) {
        qsbr = qsbr_allocate(shared);
    }
    _PyEval_StartTheWorld(interp);
}
```

The array is resized by *pausing every thread* rather than by a lock-free resize, and the
function returns an **index** rather than a pointer because "the array may be resized and
the pointer invalidated." That is a deliberate choice of a blocking mechanism over a
lock-free one, in the middle of the reclamation subsystem, and it is §10's decision
procedure applied by the CPython developers.

**6. There is a typo in the public-ish API name** — `_Py_qbsr_goal_reached` (note `qbsr`,
not `qsbr`) is spelled that way in `pycore_qsbr.h` and used that way in `qsbr.c` and
`obmalloc.c`. Harmless, but it is a good reminder that you should grep the source rather
than trusting your memory of an API.

### 11.2 What QSBR actually protects in CPython

Not "objects" — objects are reference counted. QSBR covers the things that are *reachable
from* a reference-counted object but are **not themselves refcounted**, and can be
replaced while another thread reads them lock-free. Verified in-tree:

| Protected thing | Where | Why it can't just be freed |
|---|---|---|
| `_PyListArray` — a list's backing array | `Objects/listobject.c`: `free_list_items(items, use_qsbr)` with `use_qsbr = is_resize && _PyObject_GC_IS_SHARED(a)`, calling `_PyMem_FreeDelayed(array, size)` | A reader doing `lst[i]` without a lock may hold a pointer into the old array while a writer resizes. |
| `PyDictKeysObject` and `PyDictValues` | `Objects/dictobject.c`: `free_keys_object(keys, use_qsbr)`, `free_values(values, use_qsbr)` → `_PyMem_FreeDelayed(...)` | Same, for dict lookups. |
| **mimalloc `mi_page_t`** | `Objects/obmalloc.c`: `page->qsbr_goal`, `_PyMem_mi_page_clear_qsbr`, `_PyMem_mi_heap_collect_qsbr` | The deep one — see below. |

The mimalloc-page case is the one that shows how far the design commits. From
`InternalDocs/qsbr.md`:

> *"Non-locking dictionary and list accesses require cooperation from the memory
> allocator. If an object is freed and its memory is reused, we must ensure the new
> object's reference count field is at the same memory location. In practice, this means
> when a mimalloc page (`mi_page_t`) becomes empty, we don't immediately allow it to be
> reused for allocations of a different size class."*

Read that twice. **A lock-free reader may `Py_INCREF` a pointer it loaded a moment ago,
after the object died.** That is safe only if the memory at that address is still
*shaped* like a `PyObject` — i.e. the refcount field is still a refcount field. CPython
guarantees it by refusing to repurpose a mimalloc page's size class until QSBR says every
thread has passed a quiescent state. **This is the strongest possible confirmation of
[`16-object-memory-layout.md`](16-object-memory-layout.md) §12's claim that the allocator
choice is load-bearing for the concurrency design, not just for allocation speed.**

The deferred-advance optimization is textbook batching, with real constants *(verified in
`Objects/obmalloc.c`)*:

| Constant | Value | Meaning |
|---|---|---|
| `QSBR_DEFERRED_LIMIT` | **127** | advance `wr_seq` after this many deferred frees |
| `QSBR_FREE_MEM_LIMIT` | **1024*1024** (1 MiB) | advance if a block, or the accumulated deferred memory, exceeds this |
| `QSBR_PAGE_MEM_LIMIT` | **4096*20** | same, for mimalloc pages held back |

Those three constants are the §7.7 table's "unbounded memory" row being *bounded by hand*:
CPython buys back the bound that QSBR does not give you by forcing an advance whenever
deferred memory crosses a threshold. The design doc says so: *"This optimization improves
runtime speed but may increase peak memory usage by slightly delaying when memory can be
reclaimed; the size-based thresholds above bound that extra memory."*

**And where is the quiescent state?** The eval breaker — the same mechanism the GIL build
uses for `gil_drop_request` ([`24-the-gil.md`](24-the-gil.md) §3). CPython already had a
periodic, cheap, guaranteed-to-be-reached point where a thread holds no interior pointers.
QSBR is the reclamation scheme that fits the runtime CPython already had. That is not a
coincidence; it is the reason it was chosen over hazard pointers (which would need a slot
per live borrowed pointer — an unthinkable change to the C-API) or EBR (whose enter/exit
would need to bracket every unlocked container read).

### 11.3 `PyMutex` — and why an adaptive lock beat rolling custom lock-free structures

Here is the punchline of the whole document. Free-threaded CPython's answer to
"we need thread-safe containers" was overwhelmingly **not** lock-free data structures. It
was **a very good lock, applied per object.**

`Include/internal/pycore_lock.h` opens with, verbatim:

```c
// Lightweight locks and other synchronization mechanisms.
//
// These implementations are based on WebKit's WTF::Lock. See
// https://webkit.org/blog/6161/locking-in-webkit/ for a description of the
// design.
```

And `Include/cpython/pylock.h` defines the whole thing:

```c
// A mutex that occupies one byte. The lock can be zero initialized to
// represent the unlocked state.
// ...
// Only the two least significant bits are used. The remaining bits are always zero:
// 0b00: unlocked
// 0b01: locked
// 0b10: unlocked and has parked threads
// 0b11: locked and has parked threads
typedef struct PyMutex { uint8_t _bits; } PyMutex;
```

**One byte.** Two bits. Zero-initializable, so a `PyMutex` costs nothing to embed in every
object and nothing to initialize. Compare `pthread_mutex_t`: 64 bytes on macOS, 40 on
glibc, and requiring explicit init/destroy. **You cannot put a `pthread_mutex_t` in every
Python object; you can put a `PyMutex` in every Python object.** The size *is* the design.

Where do the waiters go if the mutex has no wait queue? Into a **parking lot** — a global
side table (`Python/parking_lot.c`) hashed by address, exactly WebKit's `ParkingLot`.
Uncontended locks pay one byte and one CAS; only contended ones pay for queue
infrastructure, and they pay it out of a shared structure rather than per object.

The lock path, verbatim from `Python/lock.c`:

```c
// If a thread waits on a lock for longer than TIME_TO_BE_FAIR_NS (1 ms), then
// the unlocking thread directly hands off ownership of the lock. This avoids
// starvation.
static const PyTime_t TIME_TO_BE_FAIR_NS = 1000*1000;

// Spin for a bit before parking the thread. This is only enabled for
// `--disable-gil` builds because it is unlikely to be helpful if the GIL is
// enabled.
#if Py_GIL_DISABLED
static const int MAX_SPIN_COUNT = 40;
static const int RELOAD_SPIN_MASK = 3;
#else
static const int MAX_SPIN_COUNT = 0;
static const int RELOAD_SPIN_MASK = 1;
#endif
```

Everything in §8 and §9 is visible in those twenty lines:

- **Adaptive spin-then-park (40 iterations).** Short critical sections resolve in
  userspace with no syscall — the uncontended-CAS win from §8 row 1. Long ones park in the
  kernel and stop generating coherence traffic — the reason the mutex *beat* the
  lock-free stack at 4+ threads in §8.
- **Barging control with a fairness deadline.** A waiter that has waited 1 ms gets
  ownership **handed to it directly** rather than racing for it. This is the *exact*
  starvation guarantee a lock-free structure cannot give you (§1), implemented in a
  blocking lock. The `struct mutex_entry { PyTime_t time_to_be_fair; int handed_off; }`
  in `lock.c` is the mechanism.
- **Contention-avoidance in the spin loop itself**, with a comment that reads like §9.1:

```c
// Using thread-id as a way of reducing contention further in the reload below.
// It adds a pseudo-random starting offset to the recurrence, so that threads
// are less likely to try and run compare-exchange at the same time.
// The lower bits of platform thread ids are likely to not be random,
// hence the right shift.
const Py_ssize_t tid = (Py_ssize_t)(_Py_ThreadId() >> 12);
```

That is randomized backoff, in CPython, for the reason §9.1 gives.

- **`MAX_SPIN_COUNT = 0` on the GIL build.** Spinning is *disabled* when the GIL is
  enabled, because a spinner cannot win — the holder needs the GIL to release. A tuning
  parameter that is correct in one configuration and actively harmful in the other. That
  is what "adaptive" means in practice.

**Why this beat rolling custom lock-free containers.** Sam Gross's estimate for
per-object locking overhead, quoted in [`24-the-gil.md`](24-the-gil.md) §8.4, is about
**1.5%**. Set that against what the alternative would have cost:

1. **The C-API is the constraint.** Thousands of extensions call `PyList_GET_ITEM` and
   hold borrowed references. A lock-free list would have to make every borrowed reference
   safe against concurrent reclamation — a hazard-pointer slot per borrow, or an API
   break. PEP 703 chose neither: it kept refcounting and added QSBR *only* for the
   non-refcounted interiors (§11.2).
2. **Reclamation would have been needed anyway.** Even a perfect lock-free dict has the
   §7 problem. You do not escape reclamation by going lock-free; you *acquire* it.
3. **The measured win wasn't there.** §8 is the general form of the argument: at the
   contention levels a per-object lock sees — which is *low*, because the lock is
   per-object and objects are mostly thread-local — a spin-then-park lock is at or near
   the uncontended CAS cost, and it degrades gracefully instead of storming.
4. **Correctness cost.** §7.3 is my 40-line demonstration of what a lock-free rewrite of
   `dict` would have cost in review time and latent bugs, multiplied by every container in
   the language.

So: CPython used **one** reclamation scheme (QSBR), for **three** specific
non-refcounted things, and a **very good lock** for everything else. That allocation of
effort is the correct one and it is §10's flowchart, executed by people who had the option
to do otherwise.

### 11.4 How it all composes: per-object locks + biased refcounting + mimalloc

These three are usually described separately. They are one mechanism:

```
    ┌───────────────────────────────────────────────────────────────────────┐
    │  A thread reads  obj.attr  on the free-threaded build                 │
    └────────────────────────────────┬──────────────────────────────────────┘
                                     ▼
    ┌───────────────────────────────────────────────────────────────────────┐
    │ 1. BIASED REFCOUNTING (24-the-gil.md §8.2)                            │
    │    if (ob_tid == my_tid)  ob_ref_local++      ← plain, non-atomic     │
    │    else                   ob_ref_shared += 1  ← atomic + coherence    │
    │    if immortal (ob_ref_local == UINT32_MAX)   ← nothing at all        │
    │    → decides whether the read costs 0, ~1, or ~100+ cycles            │
    └────────────────────────────────┬──────────────────────────────────────┘
                                     ▼
    ┌───────────────────────────────────────────────────────────────────────┐
    │ 2. LOCK-FREE CONTAINER READ (no PyMutex taken on the fast path)       │
    │    load ob_item / dk_entries, index it, INCREF the result             │
    │    → the loaded array may be REPLACED under you by a concurrent       │
    │      resize. This is exactly §3's `old->next` hazard.                 │
    └────────────────────────────────┬──────────────────────────────────────┘
                                     ▼
    ┌───────────────────────────────────────────────────────────────────────┐
    │ 3. QSBR (Python/qsbr.c)                                               │
    │    the writer called _PyMem_FreeDelayed(old_array) instead of free(); │
    │    the block is released only after every thread hits the eval        │
    │    breaker → the reader above cannot have been holding it.            │
    └────────────────────────────────┬──────────────────────────────────────┘
                                     ▼
    ┌───────────────────────────────────────────────────────────────────────┐
    │ 4. MIMALLOC (16-object-memory-layout.md §12)                          │
    │    thread-local heaps → allocation needs no cross-thread sync;        │
    │    page metadata → the GC enumerates objects without a global         │
    │      registry, which is why PyGC_Head could be DELETED;               │
    │    QSBR-held pages → a page is not repurposed to a different size     │
    │      class until it is safe, so a racing INCREF still lands on a      │
    │      real refcount field.                                             │
    └───────────────────────────────────────────────────────────────────────┘
```

Pull any one out and the others stop working. Biased refcounting without immortalization
still ping-pongs on `None`. Lock-free container reads without QSBR are §7.0's
use-after-free. QSBR without mimalloc's page-level cooperation cannot make step 2's
`Py_INCREF` safe. **This is a single design, and describing it as four independent
optimizations is the most common way people misunderstand PEP 703.**

### 11.5 Why free-threaded cycle collection stops the world — twice

Reclamation appears once more, at the top of the stack. The cycle collector must observe a
consistent snapshot of every refcount to decide what is garbage; under the GIL it got that
for free. Without the GIL, PEP 703 pauses all Python-executing threads.

**Verified in `Python/gc_free_threading.c`, function `gc_collect_internal()`** — the two
pauses are literal `_PyEval_StopTheWorld` / `_PyEval_StartTheWorld` pairs:

```c
static void
gc_collect_internal(PyInterpreterState *interp, struct collection_state *state, int generation)
{
    _PyEval_StopTheWorld(interp);                    /* ── PAUSE 1 ── */
        ... merge per-thread refcounts for types; merge queued objects ...
        process_delayed_frees(interp, state);        /* ← QSBR's deferred list! */
        ... gc_mark_alive_from_roots ... deduce_unreachable_heap ...
        find_weakref_callbacks(state);
    _PyEval_StartTheWorld(interp);

    /* World RUNNING. Callbacks and finalizers may execute arbitrary Python,
       may take locks, may block. Running them under the pause would introduce
       deadlocks that do not exist under the GIL. */
    call_weakref_callbacks(state);
    finalize_garbage(state);

    _PyEval_StopTheWorld(interp);                    /* ── PAUSE 2 ── */
        handle_resurrected_objects(state);           /* PEP 442 resurrection */
        _PyGC_ClearAllFreeLists(interp);
        clear_weakrefs(state);
    _PyEval_StartTheWorld(interp);
}
```

Three observations:

**1. The two-pause structure is forced by finalizers, not by the algorithm.** Pause 1
computes what is unreachable. Then the world *must* restart, because `__del__` and weakref
callbacks run arbitrary Python that can acquire any lock — running them inside a
stop-the-world is a deadlock generator. Pause 2 exists solely to re-check what those
finalizers resurrected (PEP 442 — see
[`22-garbage-collection.md`](22-garbage-collection.md) §10). **The finalizer semantics
Python promised in 2014 are why the free-threaded GC pauses twice in 2026.**

**2. `process_delayed_frees()` runs *inside* pause 1.** The GC pause is used as a
guaranteed grace period: with every thread stopped, every thread is trivially quiescent,
so the entire QSBR deferred-free list can be drained at once. Elsewhere in the same file:

```c
// While we are in a "stop the world" pause, we can observe the latest
// ...
_Py_qsbr_advance(&interp->qsbr);
_Py_qsbr_quiescent_state(current_tstate->qsbr);
```

QSBR and the cycle collector are not two systems that happen to coexist; the GC pause is
QSBR's backstop, and QSBR is why the GC can run *less often* in the free-threaded build.

**3. This is a genuine new latency cost the GIL build does not have.** Two
stop-the-world pauses per collection, whose duration scales with heap size and thread
count. If you are moving a latency-sensitive service to free-threading, this — not the
1–8% single-threaded overhead — is the thing to measure. See
[`22-garbage-collection.md`](22-garbage-collection.md) §10 and
[`24-the-gil.md`](24-the-gil.md) §8.5, and note from README §15 that the incremental GC
that was supposed to shorten these pauses **was reverted twice**.

### 11.6 What this means for you, at the Python level

You will almost never write a CAS in Python. What you inherit from all of the above:

- **`threading.Lock` on a free-threaded build is a `PyMutex`-backed adaptive lock**, not a
  raw futex. Uncontended acquisition is roughly a byte-sized CAS. Do not avoid it out of
  superstition.
- **Atomicity of Python operations did not change** — see the table in
  [`24-the-gil.md`](24-the-gil.md) §6. `lst.append(x)` is still atomic (now via a
  per-object lock); `d[k] += 1` is still not.
- **The new scaling wall is object sharing**, not the GIL. Threads on disjoint object
  graphs scale; threads hammering one shared dict hit biased-refcount escape, shared-count
  atomics, per-object lock contention, and coherence ping-pong. **Sharding is the fix at
  the Python level for exactly the reason §8's `shard` row beats everything at the C
  level.**
- **If you write a C extension** ([`17-c-api-and-extensions.md`](17-c-api-and-extensions.md)):
  borrowed references are now genuinely dangerous, because the owner can vanish
  concurrently. Prefer the strong-reference APIs (`PyObject_GetItemRef` and friends). And
  any module-level mutable state that was implicitly protected by the GIL now needs a real
  lock — a `PyMutex` is one byte, so there is no excuse.

---

## 12. Lab exercises

Reading this document leaves you at **rung 3** of README §14 — you can now explain ABA
fluently and will collapse on the first "so when exactly can you free it?" These labs are
the rung-4 ladder: *you have built or broken it and measured the result.* Labs 2 and 4 are
the ones that move you to rung 5, because they end with you not trusting your own code.

**1 — Reproduce ABA deterministically.** Build the §4.3 harness: a Treiber stack, a victim
thread gated between its read of `head` and its CAS, and a main thread that performs
pop-A / pop-B / push-A. Print the corruption. Then add `free(B)` and rerun under
`-fsanitize=address`. *Proves you can construct an interleaving rather than recognize one
— the difference between rungs 3 and 4 on this topic.*

**2 — Find the ABA cliff on your own machine.** Run the unforced version (three nodes,
N threads, pop/mark/push, count double-ownership) at N = 1, 2, 3, 4, 6, 8, 2×cores. Plot
duplicate-pops against N. **Predict where the cliff is before you run it**, then explain
the location from your core count and the scheduler. *Proves that concurrency bug rates
are a property of the machine, not the code — and it will permanently change how you read
a green CI run.*

**3 — Defeat it three ways, and break two of them.** Take lab 1's harness and fix it with
(a) a 128-bit tagged pointer, (b) an 8-bit tag stolen from a 256-byte-aligned pointer's
low bits, (c) hazard pointers. Assert `atomic_is_lock_free` in (a). Then **make (b) fail**
by driving 256 modifications through the window, and **make (c) fail** by relaxing the
publishing store from `seq_cst` to `release` and running on this ARM machine under TSan.
*Proves tag width is a probability argument and that memory ordering in the HP protocol is
load-bearing.*

**4 — Write an epoch reclaimer and let a sanitizer humiliate you.** Implement EBR from the
description in §7.2 *without reading §7.3 first*. Run it under ASan at 2 threads (it will
pass) and then at 2× your core count. Only then read §7.3 and see how many of the three
defects you shipped. *Proves §10's premise better than any argument in this document. This
is the single highest-value lab here.*

**5 — Reproduce §8's table on your deployment target.** Five implementations — private
shard, `pthread_mutex`, plain Treiber, Treiber + exponential backoff, elimination — swept
over thread count, **two independent runs per point**. Answer three questions: at what
thread count does the mutex overtake the lock-free stack? What is your run-to-run spread?
How much does the `shard` row beat everything? *Proves the central practical claim of this
document on hardware you actually ship to, and gives you a noise floor to carry into
[`31-measurement-methodology.md`](31-measurement-methodology.md).*

**6 — Build the Michael–Scott queue and then break it on purpose.** Implement it with the
correctness harness from §6 (every value dequeued exactly once). Then introduce two bugs
one at a time: move `v = next->val` to *after* the CAS, and delete the `if (h != Q_head)
continue;` re-validation. Find the thread count at which each becomes detectable.
*Proves you understand why those two lines exist, which is the whole difference between
transcribing the paper and understanding it.*

**7 — Read `Python/qsbr.c` end to end and map it onto §7.4.** 291 lines. Identify: the
quiescent state (`_Py_qsbr_quiescent_state`), the grace-period test
(`_Py_qsbr_poll` / `QSBR_LEQ`), the retirement point (`_PyMem_FreeDelayed` in
`Objects/obmalloc.c`), and the three deferred-advance thresholds. Then find every
`use_qsbr` call site in `Objects/listobject.c` and `Objects/dictobject.c` and explain, for
each, *which unsynchronized reader* it protects. *Proves you can read production
reclamation code, which is a rarer skill than writing toy versions of it.*

**8 — Measure the free-threaded GC pause.** On `python3.14t`, build a large cyclic object
graph, then time `gc.collect()` while N busy threads run. Sweep N. Compare against
`python3.14`. Then use `gc.freeze()` before the loop and re-measure. *Proves §11.5 is a
real cost with a real mitigation, and it is the number you must bring to any
free-threading migration review alongside the memory delta from
[`16-object-memory-layout.md`](16-object-memory-layout.md) §2.*

---

## 13. Question bank

Staff-level. If you cannot answer from your own model, the section to reread is noted.

1. Define wait-free, lock-free, and obstruction-free precisely. Which one does a Treiber stack satisfy, and which does it *not*? *(§1)*
2. Name three situations where lock-freedom is a hard requirement that no lock can satisfy. Note that "we need more throughput" is not one. *(§1, §8)*
3. Your ARM CPU uses LL/SC, which detects *any* store to the location, not just a value change. Why does that not prevent ABA? *(§2, §4.3)*
4. Walk the ABA interleaving on a Treiber stack step by step, and state exactly what the successful CAS proved and what it failed to prove. *(§4.2)*
5. A colleague fixes ABA with a tagged pointer and says the structure is now safe. What have they not fixed? *(§5.3, §7)*
6. You steal 6 low bits of a 64-byte-aligned pointer for a tag. How many modifications until the fix fails, and is that acceptable? *(§5.3)*
7. Why are high pointer bits a worse place to hide a tag than low bits? Name two hardware features that broke code doing it. *(§5.3)*
8. In the Michael–Scott queue, why must a dequeuer read `next->val` *before* the CAS? *(§6)*
9. What makes the MS queue lock-free rather than obstruction-free? Point at the specific mechanism. *(§6)*
10. State the reclamation problem in one sentence, without using the words "ABA" or "race". *(§7)*
11. Hazard pointers vs epoch-based reclamation: which bounds memory, which has the cheaper read path, and what is the failure mode of each? *(§7.1, §7.2, §7.7)*
12. Why does a hazard pointer's publishing store need `seq_cst` rather than `release`, and what breaks on ARM if you get it wrong? *(§7.1)*
13. In EBR, why is a retired object safe at epoch X+2 and not X+1? *(§7.2)*
14. Why is naive per-node reference counting a circular solution to the reclamation problem? *(§7.6)*
15. Your lock-free queue is 2× slower than the `pthread_mutex` version at 8 threads. Explain the mechanism, and say what you would change. *(§8)*
16. Distinguish a retry storm from lock contention using only a CPU profile and a throughput-vs-threads curve. *(§8)*
17. Why does the elimination-backoff stack get *better* as contention rises, and why can't the same trick work for a FIFO queue? *(§9.2)*
18. In the elimination protocol, why does a *failed* withdrawal CAS mean the operation *succeeded*? *(§9.2)*
19. Why is `PyMutex` one byte, and what design decision does that size make possible? *(§11.3)*
20. `MAX_SPIN_COUNT` is 40 on free-threaded builds and 0 on GIL builds. Why is spinning actively wrong when the GIL is enabled? *(§11.3)*
21. What does CPython's QSBR protect that reference counting does not, and why can't refcounting cover it? *(§11.2)*
22. Why must a mimalloc page not be reused for a different size class until QSBR says so? *(§11.2)*
23. Free-threaded cycle collection takes *two* stop-the-world pauses. Why not one? *(§11.5)*
24. PEP 703 shipped one reclamation scheme and a very good lock, rather than lock-free containers. Give three reasons, at least one of which is about the C-API. *(§11.3)*

---

## 14. Sources

**Primary papers — the actual literature**
- **R. Kent Treiber, *Systems Programming: Coping with Parallelism*, IBM Almaden Research Center, Technical Report RJ 5118 (1986).** The origin of the lock-free stack. **Verdict:** historically essential, practically unnecessary — it is an internal IBM report that is hard to obtain and every modern treatment (Herlihy & Shavit ch. 11) presents the algorithm more clearly. Cite it; read Herlihy.
- **Maged M. Michael & Michael L. Scott, *Simple, Fast, and Practical Non-Blocking and Blocking Concurrent Queue Algorithms*, PODC 1996.** [PDF](https://www.cs.rochester.edu/~scott/papers/1996_PODC_queues.pdf) 🆓 **Verdict: read it in full — it is nine pages and it is the best-written paper in this field.** The helping mechanism and the two-CAS enqueue are explained better in the original than anywhere since. §6 is a compressed retelling.
- **Maged M. Michael, *Hazard Pointers: Safe Memory Reclamation for Lock-Free Objects*, IEEE TPDS 15(6), June 2004.** (Preceded by *Safe Memory Reclamation for Dynamic Lock-Free Objects Using Atomic Reads and Writes*, PODC 2002.) **Verdict: read §§1–4.** Note that this is Michael solving, eight years later, the problem his own 1996 queue paper left open — the most instructive fact in this document's history.
- **Keir Fraser, *Practical Lock-Freedom*, PhD thesis, University of Cambridge, Technical Report **UCAM-CL-TR-579**, February 2004, 116 pages.** [Free PDF](https://www.cl.cam.ac.uk/techreports/UCAM-CL-TR-579.pdf) 🆓 *(citation verified against the Cambridge Computer Laboratory TR index during writing; the dissertation was submitted September 2003.)* **Verdict: the epoch-based reclamation chapter is the canonical source and worth reading; the rest is a thesis and reads like one.**
- **Paul E. McKenney & John D. Slingwine, *Read-Copy Update: Using Execution History to Solve Concurrency Problems*, PDCS 1998.** And McKenney's [*What is RCU, Fundamentally?*](https://lwn.net/Articles/262464/) 🆓 (LWN, 3 parts) plus the freely available [*Is Parallel Programming Hard, And, If So, What Can You Do About It?*](https://mirrors.edge.kernel.org/pub/linux/kernel/people/paulmck/perfbook/perfbook.html) 🆓 **Verdict: start with the LWN series, not the papers.** perfbook ch. 9 is the most thorough treatment of deferred reclamation in print and it is free.
- **Danny Hendler, Nir Shavit & Lena Yerushalmi, *A Scalable Lock-Free Stack Algorithm*, SPAA 2004.** **Verdict: read it if §9.2 interested you** — the withdrawal protocol is subtle enough to deserve the original.
- **Maurice Herlihy, *Wait-Free Synchronization*, ACM TOPLAS 13(1), January 1991.** **Verdict: read the consensus-number result, skip the rest** unless you want the theory. It is why CAS is universal and test-and-set is not.
- **Maurice Herlihy, Victor Luchangco & Mark Moir, *Obstruction-Free Synchronization: Double-Ended Queues as an Example*, ICDCS 2003.** **Verdict: reference only** — read it to understand where the third progress condition came from.
- **Timothy Harris, *A Pragmatic Implementation of Non-Blocking Linked-Lists*, DISC 2001.** **Verdict: read it when you need a lock-free set** — the logical-deletion-mark technique is the standard answer.
- **Thomas Hart, Paul McKenney, Angela Demke Brown & Jonathan Walpole, *Performance of Memory Reclamation for Lockless Synchronization*, JPDC 67(12), 2007.** **Verdict: this is the study §7.7 wishes it were.** If you need to choose a reclamation scheme on evidence, read this one.
- **Trevor Brown, *Reclaiming Memory for Lock-Free Data Structures: There Has to Be a Better Way*, PODC 2015.** **Verdict: read the survey portion** — the best short taxonomy of the field.

**Books**
- **Herlihy, Shavit, Luchangco & Spear, *The Art of Multiprocessor Programming*, 2e (Morgan Kaufmann, 2020).** **Verdict: the single best book for this document's material.** Ch. 3 (progress conditions), ch. 7 (spin locks & contention, incl. backoff), ch. 10–11 (queues and stacks, incl. elimination). §1, §3, §6 and §9 here are a compressed version of those chapters. Buy it.
- **Anthony Williams, *C++ Concurrency in Action*, 2e.** **Verdict: ch. 7 is the most practical lock-free-in-C++ writing that exists**, including an honest hazard-pointer implementation and an honest account of why he doesn't recommend it.

**CPython — verified against `python/cpython` `main` during writing**
- [`Python/qsbr.c`](https://github.com/python/cpython/blob/main/Python/qsbr.c) (291 lines) and [`Include/internal/pycore_qsbr.h`](https://github.com/python/cpython/blob/main/Include/internal/pycore_qsbr.h). **Verdict: read both in full — they are short, and `qsbr.c` is the cleanest production reclamation code you will find in a language runtime.**
- [`InternalDocs/qsbr.md`](https://github.com/python/cpython/blob/main/InternalDocs/qsbr.md). **Verdict: read this first**, before the C. It documents the deferred-advance thresholds and admits the >1,000-thread scan limitation. Source of the mimalloc-page reasoning in §11.2.
- [gh-115103 — *Implement delayed free mechanism for free-threaded builds*](https://github.com/python/cpython/issues/115103) — the original QSBR proposal that `InternalDocs/qsbr.md` was derived from. **Verdict: read the issue discussion** for the design alternatives that were rejected.
- [`Include/cpython/pylock.h`](https://github.com/python/cpython/blob/main/Include/cpython/pylock.h), [`Include/internal/pycore_lock.h`](https://github.com/python/cpython/blob/main/Include/internal/pycore_lock.h), [`Python/lock.c`](https://github.com/python/cpython/blob/main/Python/lock.c), [`Python/parking_lot.c`](https://github.com/python/cpython/blob/main/Python/parking_lot.c). **Verdict: `pylock.h`'s comment block is 20 lines and tells you the entire design.**
- [`Objects/obmalloc.c`](https://github.com/python/cpython/blob/main/Objects/obmalloc.c) — `_PyMem_FreeDelayed`, `_PyMem_ProcessDelayed`, `QSBR_DEFERRED_LIMIT`, `QSBR_FREE_MEM_LIMIT`, `QSBR_PAGE_MEM_LIMIT`, `_PyMem_mi_heap_collect_qsbr`.
- [`Objects/listobject.c`](https://github.com/python/cpython/blob/main/Objects/listobject.c) (`free_list_items`, `_PyListArray`) and [`Objects/dictobject.c`](https://github.com/python/cpython/blob/main/Objects/dictobject.c) (`free_keys_object`, `free_values`) — the `use_qsbr` call sites.
- [`Python/gc_free_threading.c`](https://github.com/python/cpython/blob/main/Python/gc_free_threading.c), function `gc_collect_internal` — the two stop-the-world pauses in §11.5.
- [PEP 703 — *Making the Global Interpreter Lock Optional in CPython*](https://peps.python.org/pep-0703/), §Reference Counting and §Garbage Collection. [PEP 779 — *Criteria for supported status for free-threaded Python*](https://peps.python.org/pep-0779/).
- [**Locking in WebKit**](https://webkit.org/blog/6161/locking-in-webkit/) (Filip Pizlo, 2016). **Verdict: read it — it is the design document for `PyMutex`**, cited by name in `pycore_lock.h`, and the clearest explanation anywhere of why a one-byte adaptive lock beats both a spinlock and a `pthread_mutex`.

**Background referenced above**
- FreeBSD `sys/kern/subr_smr.c` — "GUS", Jeffrey Roberson's original, which `Python/qsbr.c` is derived from. Also Joel Fernandes' [*GUS vs RCU*](https://people.kernel.org/joelfernandes/gus-vs-rcu) 🆓, linked from `InternalDocs/qsbr.md`.
- [Arm Architecture Reference Manual](https://developer.arm.com/documentation/ddi0487/latest/) — `LDXR`/`STXR`, `CAS`/`CASP` (FEAT_LSE), TBI, PAC, MTE. Reference only; use it to settle arguments about §2 and §5.3.
- ThreadSanitizer and AddressSanitizer docs; **CDSChecker** and **GenMC** for model checking; **Loom** (Rust) for exhaustive interleaving. §7.3 exists because of ASan.

**Sibling docs**
- [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) §5–§6 — MESI, the read/write asymmetry, and 128-byte false sharing. §8's entire explanation lives there.
- [`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md) — acquire/release, LL/SC, why §7.1's `seq_cst` store cannot be weakened.
- [`24-the-gil.md`](24-the-gil.md) §8 — PEP 703's five tiers; this document is the *reclamation* half of that story, and §7 of the GIL doc (the Gilectomy's atomic-refcount failure) is §8 of this one playing out at runtime scale.
- [`22-garbage-collection.md`](22-garbage-collection.md) §10 — the free-threaded collector, PEP 442 resurrection, and why §11.5 pauses twice.
- [`26-free-threading.md`](26-free-threading.md) — the migration in practice.
- [`16-object-memory-layout.md`](16-object-memory-layout.md) §12 — mimalloc's load-bearing role, which §11.2 confirms from the QSBR side.
- [`30-concurrency-correctness.md`](30-concurrency-correctness.md) — testing and fuzzing concurrent code, i.e. the tooling §10 demands.

---

*Next: [`04-binary-abi-and-linking.md`](04-binary-abi-and-linking.md) — but if you came
here from Tier 4, go straight to [`24-the-gil.md`](24-the-gil.md) §8 and reread it with
§7 and §11 of this document in hand. The GIL doc explains what PEP 703 replaced the GIL
*with*; this one explains what it had to build underneath to make that legal.*
