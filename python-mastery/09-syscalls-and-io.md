# 09 — Syscalls and I/O

> **Provenance.** The subject of this document is the UNIX/Linux I/O interface: the trap,
> the vDSO, the readiness family (`select`/`poll`/`epoll`/`kqueue`), `io_uring`, the page
> cache, `fsync` durability, and the zero-copy calls. Linux material is researched from
> primary sources — the man-pages project, the kernel's own `Documentation/`, LWN's
> io_uring and fsync coverage, Kerrisk's *TLPI* and Gregg's *Systems Performance* 2e —
> and attributed inline.
>
> Measurements are illustration, not the argument. Everything measured here ran in this
> session on an **Apple M3 Pro** (5P + 6E, 11 logical CPUs, 128-byte cache lines, 16 KiB
> pages, 18 GiB RAM), Darwin 25.5.0 / macOS 26.5.2 (build 25F84, `xnu-12377.121.10`,
> arm64), on **APFS on the internal SSD** (`diskutil`: *Solid State: Yes*, protocol
> *Apple Fabric*), with **CPython 3.14.6**. The machine was **not quiet** — `load1` ran
> **1.9–2.4** for the whole session. Timings are **medians of ≥5 alternating passes**
> with spread reported; disk benchmarks were kept to tens of megabytes and the test files
> were deleted. One measurement (§10.4) came back as a **null result** and is reported as
> one.
>
> **One honesty rule, applied throughout: no researched number is ever presented as
> though I measured it.** Measured tables say so in the caption.
>
> This document is the layer underneath [`28-asyncio-internals.md`](28-asyncio-internals.md).
> Doc 28 §9–§10 already covers the asyncio side — the `selectors` module, the self-pipe,
> and the full `await` → `kevent` path. **This document does not repeat it**; it explains
> the kernel interface that path lands on, and doc 28 links forward here for exactly that
> reason.

---

## Contents

1. [What a syscall actually is](#1-what-a-syscall-actually-is)
2. [What a trap costs, and why](#2-what-a-trap-costs-and-why)
3. [The escape hatch: the vDSO](#3-the-escape-hatch-the-vdso)
4. [Blocking, non-blocking, and the third thing](#4-blocking-non-blocking-and-the-third-thing)
5. [Readiness notification: `select` → `poll` → `epoll`](#5-readiness-notification-select--poll--epoll)
6. [The scaling curve, measured](#6-the-scaling-curve-measured)
7. [Level- vs edge-triggered, and the bug each invites](#7-level--vs-edge-triggered-and-the-bug-each-invites)
8. [`io_uring`: the completion model](#8-io_uring-the-completion-model)
9. [What `io_uring` changes about an async server's cost model](#9-what-io_uring-changes-about-an-async-servers-cost-model)
10. [The page cache](#10-the-page-cache)
11. [`fsync` does not mean what you think](#11-fsync-does-not-mean-what-you-think)
12. [Zero-copy: `sendfile`, `splice`, `MSG_ZEROCOPY`](#12-zero-copy-sendfile-splice-msg_zerocopy)
13. [`O_DIRECT`: bypassing the cache on purpose](#13-o_direct-bypassing-the-cache-on-purpose)
14. [Batching: the only general-purpose fix](#14-batching-the-only-general-purpose-fix)
15. [CPython's I/O stack](#15-cpythons-io-stack)
16. [The syscall bill of one request](#16-the-syscall-bill-of-one-request)
17. [A review checklist](#17-a-review-checklist)
18. [What I could not verify](#18-what-i-could-not-verify)
19. [Lab exercises](#19-lab-exercises)
20. [Question bank](#20-question-bank)
21. [Sources](#21-sources)

---

## 1. What a syscall actually is

A system call is not a function call. It is a **deliberate, controlled exception**: the
program executes an instruction whose entire purpose is to fault into a higher privilege
level at an address the kernel chose in advance.

The instruction differs by architecture:

| ISA | Instruction | Where the kernel entry point lives |
|---|---|---|
| AArch64 | `svc #0` (Linux) / `svc #0x80` (Darwin) | `VBAR_EL1` — the exception vector base register |
| x86-64 | `syscall` | `MSR_LSTAR` — a model-specific register loaded at boot |
| x86-32 (legacy) | `int $0x80` | IDT vector 0x80 |
| x86-32 (fast) | `sysenter` | `MSR_SYSENTER_EIP` |

The `vdso(7)` man page is explicit about why the legacy path was abandoned: `int $0x80`
"is expensive: it goes through the full interrupt-handling paths in the processor's
microcode as well as in the kernel."

You can see the real thing on any machine. Here is `getpid` inside
`libsystem_kernel.dylib` on this box, disassembled with `otool -tV` *(observed)*:

```
_getpid:
    adrp   x9, 75 ; 0x4c000
    add    x9, x9, #0x8
    ldr    w0, [x9]         ; load a cached pid from libc's own data
    cmp    w0, #0x0
    b.le   0x1190
    ret                     ; ← fast path: NO syscall at all
0x1190:
    mov    x16, #0x14       ; x16 = 20 = SYS_getpid
    svc    #0x80            ; ← the trap
```

Two things are visible in eight instructions. First, **the syscall number goes in a
register** (`x16` on Darwin, `x8` on Linux/AArch64, `rax` on x86-64) and arguments go in
the ordinary argument registers — the ABI is a register convention plus one trapping
instruction. Second, and more usefully, **libc lies to you**: `getpid()` on this platform
usually performs no syscall whatsoever, because the pid cannot change under a process and
libc caches it. Keep that in mind for §2; it is the reason half the "how fast is a
syscall" microbenchmarks on the internet measure nothing.

### 1.1 What the CPU does at `svc`

```
   USER MODE (EL0 on arm64, ring 3 on x86)
   ┌──────────────────────────────────────────────────────────────────────┐
   │  your code                                                           │
   │    mov  x8, #63            ; SYS_read                                │
   │    mov  x0, fd                                                       │
   │    mov  x1, buf                                                      │
   │    mov  x2, count                                                    │
   │    svc  #0                 ─────────┐                                │
   └─────────────────────────────────────┼────────────────────────────────┘
                                         │
              ①  the core takes a synchronous exception:
                 · PC → ELR_EL1, PSTATE → SPSR_EL1
                 · privilege level EL0 → EL1
                 · jump to VBAR_EL1 + fixed offset
                 · the pipeline is serialized here — in-flight speculation
                   for the user context is discarded
                                         │
   KERNEL MODE (EL1 / ring 0)            ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │  ② entry trampoline                                                  │
   │     · switch to the kernel stack for this task                       │
   │     · save the full user register set                                │
   │     · [x86 + KPTI] swap CR3 to the kernel page tables → TLB effects  │
   │     · [x86 + Spectre-v2] IBPB / retpoline-protected dispatch          │
   │  ③ validate: is x8 a legal syscall number? seccomp-BPF filter?        │
   │  ④ dispatch through the syscall table → sys_read()                   │
   │  ⑤ do the work: fget(fd), permission checks, VFS → filesystem →      │
   │     page cache lookup → copy_to_user(buf, page, count)               │
   │  ⑥ on the way out: check for pending signals, need_resched,          │
   │     rseq fixups, audit; possibly schedule() here                     │
   │  ⑦ restore user registers, [KPTI] swap CR3 back, mitigate again      │
   │     eret  ────────────────────────────────────────┐                  │
   └───────────────────────────────────────────────────┼──────────────────┘
                                                       ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │  execution resumes at ELR_EL1, return value in x0, EL0 again         │
   │  ...with a colder branch predictor, a colder L1i, and possibly a     │
   │     colder TLB than you had before                                   │
   └──────────────────────────────────────────────────────────────────────┘
```

**The mode switch is the cheap part.** The expensive parts are ②, ⑦, and the invisible
tail: you come back to a core whose branch predictors, µop cache, L1i and (on x86 with
page-table isolation) TLB have been partly evicted by the kernel's own execution. This is
the microarchitectural argument from [`00-cpu-execution-model.md`](00-cpu-execution-model.md)
and [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) applied to
the kernel boundary: *a syscall is a control-flow event that pollutes every predictor you
own.*

### 1.2 What Meltdown and Spectre did to that number

Before 2018, a null syscall on a mainstream x86-64 chip was a low-hundreds-of-nanoseconds
affair dominated by the register save/restore. Then:

- **Meltdown (variant 3)** forced **KPTI** — kernel page-table isolation. The kernel and
  user address spaces stop sharing a page-table root, so entry and exit each require a
  `CR3` write. On hardware without **PCID** every such write flushes the TLB outright;
  with PCID the flush is avoided but the TLB is still partitioned. This is the single
  largest syscall-cost regression of the era, and it scales with how TLB-hungry your
  workload is.
- **Spectre variant 2** forced indirect-branch protection. The kernel's Spectre
  documentation describes the toolkit: the compiler rewrites indirect calls and jumps
  into **retpolines** ("return trampolines") that trap speculative execution "in an
  infinite loop to prevent any speculative execution jumping to a gadget"; on hardware
  with **eIBRS** the retpolines are disabled at runtime in favour of the hardware bit.
  The same document notes that in high-security mode, **IBPB** flushes the branch target
  buffer on every switch to a new program and **STIBP** stays on permanently — and states
  plainly that this "will add overhead as indirect branch speculations for all programs
  will be restricted."

The practical staff-level takeaway is not a number, it is a **shape**: post-2018, the
fixed per-syscall cost on x86-64 went up enough that "make fewer syscalls" moved from a
micro-optimization to an architectural principle — and `io_uring` (§8), whose entire
premise is *amortize or eliminate the trap*, was merged **thirteen months after
Meltdown was disclosed**. That is not a coincidence.

Apple Silicon is not affected by Meltdown and does not run KPTI, which is one reason the
numbers in §2 are on the low end of what you would see on a mitigated x86-64 server.

---

## 2. What a trap costs, and why

**Measured on this machine.** CPython 3.14.6, 150,000 iterations per pass, 7 alternating
passes, medians. "Net" subtracts the empty-loop cost (5.55 ns), which is the honest
per-call figure.

| call | gross ns | **net ns** | spread | traps? |
|---|---:|---:|---:|---|
| empty `for` loop iteration | 5.55 | 0.00 | 4.2% | — |
| `os.getpid()` | 16.08 | **10.53** | 3.0% | **no** — libc caches (§1) |
| `time.monotonic()` | 30.55 | **24.99** | 11.0% | no — `mach_absolute_time` |
| `time.time_ns()` | 35.15 | 29.59 | 26.1% | no |
| `os.getegid()` | 86.28 | **80.73** | 1.7% | **yes** |
| `os.umask()` (restore) | 87.14 | 81.58 | 3.0% | yes |
| `os.getppid()` | 88.30 | **82.75** | 4.9% | **yes** |
| `os.getuid()` | 89.72 | 84.16 | 3.4% | yes |
| `os.sched_yield()` | 116.38 | 110.82 | 9.7% | yes + scheduler |
| `fcntl(fd, F_GETFD)` | 152.91 | 147.35 | 7.8% | yes + fd table |
| `os.lseek(fd, 0, 0)` | 230.49 | 224.94 | 6.5% | yes + file lock |
| `os.dup(fd)` + `os.close()` | 295.60 | 290.05 | 9.0% | two, + allocation |
| `os.read(devnull, 0)` | 278.67 | 273.01 | 7.3% | yes + VFS + object alloc |

Four things fall out of this table.

**1. The trap floor is ~81 ns.** Four different "do nothing but ask the kernel a question"
calls — `getegid`, `umask`, `getppid`, `getuid` — land in a **80.7–84.2 ns** band with
spreads under 5%. That tight cluster across independent syscalls is the strongest
evidence in this document that we are measuring the transition itself and not the work.
On this CPU, **crossing into the kernel and back costs about 80 nanoseconds**, or roughly
**320 cycles** at 4 GHz.

**2. The folklore benchmark measures nothing.** `os.getpid()` costs 10.5 ns — *one eighth*
of `os.getppid()`, an otherwise identical operation. The difference is entirely §1's
cached-pid fast path. If you benchmark syscall cost with `getpid`, you will conclude
syscalls are nearly free and you will be wrong by 8×.

**3. Real syscalls cost multiples of the floor.** `lseek` is 2.8× the floor because it
takes the open-file-description lock; `read` is 3.4× because it walks the VFS, finds a
page, and copies. The floor is a floor, not an estimate.

**4. FFI has its own tax.** Going through `ctypes` instead of a CPython C wrapper added a
flat **~88 ns** (measured: `ctypes` calling `libc.getpid()`, which as established performs
*no* syscall, cost 88.68 ns net). `ctypes.syscall(SYS_getpid)` — the deprecated indirect
syscall path — cost 220.3 ns net, i.e. ~132 ns of actual trap once the FFI tax is removed,
noticeably worse than the direct `svc` at 81 ns. **The indirect syscall entry point is a
slow path.** If you are measuring syscall cost from Python, use the `os` module, not
`ctypes`.

> **Reconciling with [`30-concurrency-correctness.md`](30-concurrency-correctness.md) §14.**
> Doc 30 measured `time.monotonic()` at **40.1 ns** and `time.time()` at **37.7 ns** over
> 500,000 calls. I measure 30.55 ns gross / 24.99 ns net, and a `timeit`-style min-of-7
> cross-check gives 27.8 ns. These are **the same finding at slightly different scaffolding
> cost** — my loop hoists the callable into a local default argument, which removes a
> global lookup per iteration, and the machine's load differed. Doc 30's conclusion
> ("~40 ns, 2–3× a dict lookup, a real cost in a hot loop") stands unchanged; if anything
> the clock is a touch cheaper than doc 30 credits. I flag it because a folder that
> reports two numbers for the same thing should say so out loud.

### 2.1 The ratio that matters

```
   a real syscall trap   ~81 ns   ████████████████████████████████
   a userspace clock read ~25 ns   ██████████
   an empty Python call    ~6 ns   ██
```

**A trap costs about 3.3× a userspace-served time read**, and about 13× an empty Python
function call. That ratio — not the absolute nanoseconds — is the number to carry around,
because it is what motivates everything in §3 and §8. Both endpoints are cheap in absolute
terms; the point is that one of them scales with your request rate and the other does not
have to.

---

## 3. The escape hatch: the vDSO

Some syscalls ask for information the kernel is willing to publish read-only. Time is the
obvious one: `clock_gettime` is called by essentially every logging line, every timeout
check, every metric. Trapping for it is absurd.

Linux's answer is the **vDSO** — the *virtual dynamic shared object*. From `vdso(7)`:

> The "vDSO" (virtual dynamic shared object) is a small shared library that the kernel
> automatically maps into the address space of all user-space applications. […] Why does
> the vDSO exist at all? There are some system calls the kernel provides that user-space
> code ends up using frequently, to the point that such calls can dominate overall
> performance. This is due both to the frequency of the call as well as the context-switch
> overhead that results from exiting user space and entering the kernel.

Mechanically: the kernel maps a small ELF object into every process (you can see it as
`[vdso]` in `/proc/self/maps`, and find its base via `getauxval(AT_SYSINFO_EHDR)`). It
exports a handful of symbols; glibc resolves them and calls them as ordinary functions.
The kernel keeps a shared data page updated with the current timekeeping state, and the
vDSO code reads the hardware counter and applies the published multiplier — **entirely in
user mode, with no privilege transition at all.**

The exported set is architecture-specific. On x86-64 it is essentially
`__vdso_clock_gettime`, `__vdso_gettimeofday`, `__vdso_time`, and `__vdso_getcpu`; the
`vdso(7)` tables show the equivalent sets for riscv (`__vdso_clock_gettime`,
`__vdso_getcpu`, `__vdso_flush_icache`, …), s390 (`__kernel_clock_gettime`, …), and the
rest. The common thread: **read-mostly, high-frequency, no privileged side effects.**

Three consequences worth knowing:

- **`getcpu` in the vDSO is why per-CPU userspace data structures are viable.** Sharded
  counters and per-CPU allocator caches need to know their CPU cheaply; a trap per shard
  lookup would defeat the purpose.
- **The vDSO is why `strace` shows no `clock_gettime`.** People regularly conclude their
  program stopped calling the clock. It did not; the call never entered the kernel, so
  `ptrace` never saw it. This is a recurring source of false conclusions in
  [`12-observing-a-process.md`](12-observing-a-process.md) territory.
- **The vDSO can fall back.** If the clocksource is not vDSO-capable (some virtualized
  environments fall back to `hpet` or a paravirt clock), the vDSO function performs a real
  syscall internally and your clock reads become ~4× more expensive with no source change.
  This is a genuine production failure mode: the same binary, the same code path, a 4×
  regression caused by a hypervisor setting.

> **The Darwin equivalent, in one aside.** macOS has no vDSO. It uses the **commpage** —
> a fixed, kernel-maintained page mapped into every process. On this machine
> `_mach_absolute_time` disassembles to a load from a hard-coded commpage address
> (`0xffff'fc08'8`), a read of the `CNTVCT_EL0` virtual counter, and a re-read of the
> commpage word to detect a concurrent update *(observed via `otool -tV`)* — no `svc`
> anywhere. Different mechanism, identical idea: publish the data, skip the trap. The 25 ns
> figure in §2 is that path.

---

## 4. Blocking, non-blocking, and the third thing

There are exactly three answers to "what does my thread do while the data isn't there
yet", and every I/O API is one of them.

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ ① BLOCKING            read(fd, buf, n)                                    │
 │    the thread is put on a wait queue and descheduled until data arrives.  │
 │    Cost: one thread (≈8 KiB kernel stack + user stack) per concurrent     │
 │          operation, plus a context switch each way.                       │
 │    Simple, correct, and the reason "thread per connection" dies at ~10⁴.  │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ ② NON-BLOCKING        fcntl(fd, F_SETFL, O_NONBLOCK); read(...)           │
 │    returns immediately: data, or -1/EAGAIN.                               │
 │    You now need a way to know WHEN to retry — otherwise you spin.         │
 │    → this is what §5's readiness family exists to answer.                 │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ ③ ASYNCHRONOUS        submit(op); ... ; harvest(completion)                │
 │    you hand the kernel the whole operation and collect the RESULT later.  │
 │    POSIX AIO, Linux AIO (io_submit), Windows IOCP, io_uring.              │
 └──────────────────────────────────────────────────────────────────────────┘
```

**The distinction between ② and ③ is the single most important idea in this document.**

- ② is **readiness**: the kernel says *"a `read` on fd 7 will not block right now."* You
  then perform the `read` yourself. That is two syscalls minimum per transfer.
- ③ is **completion**: the kernel says *"the `read` you asked for is done; here are the
  200 bytes."* One submission, one completion, and the submission can be batched with
  others.

`epoll` and `kqueue` are readiness. IOCP and `io_uring` are completion. Doc 28 §9 already
notes that this is why Windows asyncio needs an entirely different loop class
(`ProactorEventLoop`) rather than a different selector — the models are not
interchangeable at the API level.

### 4.1 `O_NONBLOCK` and `EAGAIN` are not free

A non-blocking `read` that returns `EAGAIN` still costs a full trap. Every entry in §2's
table applies. This is why the edge-triggered idiom ("read until `EAGAIN`") is not
unambiguously cheaper than level-triggered: it *guarantees* one wasted syscall per drain,
in exchange for saving a readiness notification. Which wins depends on how much data
arrives per wakeup. §7.3 measures both sides.

There is also a semantic trap: **`O_NONBLOCK` is a property of the open file description,
not the file descriptor.** Set it on an fd you got from `dup()`, or one inherited across
`fork()`, and you have changed it for every sharer. The classic incident is a library
setting `O_NONBLOCK` on fd 1, and an unrelated part of the program getting `EAGAIN` from a
`print()` it has no error handling for.

And `O_NONBLOCK` **does not work on regular files**. On Linux a `read()` from a regular
file always "succeeds" — it just blocks in the page-cache miss path (§10) with no way to
say no. That single fact is why `epoll` cannot be used for disk I/O, why every
readiness-based event loop hands file reads to a thread pool, and why `io_uring` exists.

---

## 5. Readiness notification: `select` → `poll` → `epoll`

Three generations of the same API, each fixing a specific structural flaw in the last.

### 5.1 `select` — the 1983 answer

```c
int select(int nfds, fd_set *readfds, fd_set *writefds, fd_set *exceptfds,
           struct timeval *timeout);
```

Three bitmaps in, three bitmaps out. The `select(2)` man page's own BUGS section is
unusually blunt about the design:

> **WARNING**: `select()` can monitor only file descriptors numbers that are less than
> **FD_SETSIZE** (1024)—an unreasonably low limit for many modern applications—and this
> limitation will not change.

and

> The implementation of the `fd_set` arguments as value-result arguments is a design error
> that is avoided in `poll(2)` and `epoll(7)`.

Three flaws, in ascending order of seriousness:

1. **`FD_SETSIZE` = 1024.** The kernel imposes no limit; glibc's fixed-size `fd_set` does.
   You cannot watch fd 1024 with glibc's macros, full stop.
2. **Destructive arguments.** The kernel overwrites your input sets with the output. You
   must rebuild all three on every single call — O(n) *userspace* work before every O(n)
   *kernel* scan. (Linux also modifies the `timeout` argument; portable code can rely on
   neither behaviour.)
3. **O(n) per call, where n is the whole set.** The kernel walks every watched fd on every
   call, whether or not anything changed.

### 5.2 `poll` — same complexity, better ergonomics

```c
struct pollfd { int fd; short events; short revents; };
int poll(struct pollfd *fds, nfds_t nfds, int timeout);
```

`poll` fixes flaws 1 and 2: no `FD_SETSIZE` limit, and input (`events`) is separate from
output (`revents`), so the array survives the call. It does **not** fix flaw 3 — the
kernel still scans every entry every time. And the array is now 8 bytes per fd instead of
1 bit, so per-fd it is *less* cache-friendly than `select`'s bitmap. §6 measures exactly
this.

### 5.3 `epoll` — state in the kernel

`epoll` is the structural fix, and the fix is to **stop passing the set on every call.**
From `epoll(7)`, the central concept is an in-kernel object holding two lists:

> - The *interest* list (sometimes also called the **epoll** set): the set of file
>   descriptors that the process has registered an interest in monitoring.
> - The *ready* list: the set of file descriptors that are "ready" for I/O. The ready list
>   is a subset of […] the file descriptors in the interest list. **The ready list is
>   dynamically populated by the kernel as a result of I/O activity on those file
>   descriptors.**

That last sentence is the whole design. The registration is amortized — you pay
`epoll_ctl(EPOLL_CTL_ADD)` once per fd, not once per wait. And the wait does not scan:
when a socket receives data, the driver's wakeup path *pushes* that fd onto the ready
list. `epoll_wait` pops from a list whose length is the number of ready fds, not the
number of watched fds.

```
   poll / select:  O(watched)          epoll / kqueue:  O(ready)

   ┌──────────────────────────┐        ┌────────────────────────────────┐
   │ user array of 10,000     │        │ kernel: interest list (rbtree) │
   │ pollfds                  │        │   10,000 fds, registered once  │
   └───────────┬──────────────┘        └───────────────┬────────────────┘
               │ copied in                             │ driver wakeup
               ▼ EVERY call                            ▼ pushes ready fds
   ┌──────────────────────────┐        ┌────────────────────────────────┐
   │ kernel scans all 10,000  │        │ kernel: ready list  [fd 4711]  │
   │ calls ->poll() on each   │        └───────────────┬────────────────┘
   └───────────┬──────────────┘                        │ epoll_wait pops
               ▼ copied out                            ▼
        10,000 pollfds back                     1 epoll_event back
```

Three flags on `epoll_ctl` are worth knowing by name:

- **`EPOLLET`** — edge-triggered. §7.
- **`EPOLLONESHOT`** — after one report, the fd is disabled in the interest list; you must
  rearm with `EPOLL_CTL_MOD`. Per `epoll(7)`, this exists because "even with edge-triggered
  epoll, multiple events can be generated upon receipt of multiple chunks of data". It is
  the standard way to guarantee that only one worker thread ever handles a given
  connection at a time.
- **`EPOLLEXCLUSIVE`** (Linux 4.5+) — the thundering-herd fix for the accept path.
  `epoll(7)` notes that with edge-triggered notification and multiple threads blocked on
  the same epoll fd, "just one of the threads (or processes) is awoken", which "provides a
  useful optimization for avoiding 'thundering herd' wake-ups".

`epoll(7)` also documents the single most-hit `epoll` footgun: closing an fd removes it
from interest lists — **but only when the last reference to the underlying open file
description goes away.** If you `dup()`'d the fd, or forked, the description survives and
events keep being reported for a descriptor number you have already reused for something
else. Every event-loop implementation has a bug report of this shape.

### 5.4 `kqueue` — the BSD design, and what asyncio uses here

`kqueue` (FreeBSD, macOS) reached the same conclusion independently and generalized
further. A single `kevent()` call both **mutates** the interest set and **waits**, so
registration and wait can be one syscall:

```c
int kevent(int kq, const struct kevent *changelist, int nchanges,
           struct kevent *eventlist,  int nevents, const struct timespec *timeout);
```

And a `kevent` is not restricted to fd readiness. `EVFILT_READ`, `EVFILT_WRITE`,
`EVFILT_VNODE` (file changed), `EVFILT_PROC` (child exited), `EVFILT_SIGNAL`,
`EVFILT_TIMER`, `EVFILT_USER` are all the same queue. That unification is nicer than
Linux's assortment of `signalfd`/`timerfd`/`eventfd`/`pidfd` — though Linux's answer has
the virtue that every one of those *is* an fd, so it composes with anything taking an fd.
The knob that matters for §7 is **`EV_CLEAR`**, kqueue's spelling of `EPOLLET`.

`selectors.DefaultSelector` picks `KqueueSelector` on this platform — doc 28 §9 verifies
that live and has the platform table. This document does not repeat it.

---

## 6. The scaling curve, measured

This is the C10K argument, and it is one of the few systems claims you can reproduce on a
laptop in ninety seconds.

**Setup** *(measured on this machine)*: N `socketpair()`s, all registered, all idle except
one, which has a byte waiting. Each mechanism must find the one ready fd among N. 400
calls per pass, 5 alternating passes, medians. Non-blocking (zero timeout), so the number
is pure lookup cost.

| N watched fds | `select` µs | `poll` µs | `kqueue` µs | select/kqueue | poll/kqueue |
|---:|---:|---:|---:|---:|---:|
| 10 | 0.73 | 1.68 | **0.31** | 2.3× | 5.3× |
| 50 | 2.14 | 6.82 | **0.29** | 7.3× | 23.2× |
| 100 | 4.03 | 14.33 | **0.29** | 14.1× | 50.1× |
| 250 | 10.18 | 37.69 | **0.28** | 35.7× | 132.2× |
| 500 | 20.71 | 77.55 | **0.29** | 72.2× | 270.4× |
| 1000 | *`ValueError`* | 151.51 | **0.29** | — | 519.1× |
| 2000 | *`ValueError`* | 308.18 | **0.29** | — | 1067.3× |
| 4000 | *`ValueError`* | 593.92 | **0.29** | — | **2019.0×** |

Marginal cost per additional watched fd, from the regression endpoints:

| mechanism | ns per watched fd |
|---|---:|
| `select` | 40.78 |
| `poll` | 148.43 |
| `kqueue` | **−0.01** |

**kqueue is flat.** 0.31 µs at N=10 and 0.29 µs at N=4000 — the marginal cost per fd is
−0.01 ns/fd, which is zero within the noise of a laptop at load 2. Four hundred times more
fds, identical cost. That is what "the ready list is dynamically populated by the kernel"
buys you, and it is why `epoll`/`kqueue` and not `poll` is what every event loop written
after 2002 uses.

**`select` hit its wall, exactly where the man page says it does.** At N=1000 the harness
raised `ValueError: filedescriptor out of range in select()` — 1000 socketpairs means
~2000 file descriptors, and CPython's `select.select` enforces `FD_SETSIZE`. This is not a
Python limitation; it is glibc's fixed-size `fd_set` and BSD's, faithfully surfaced. The
`select(2)` man page's "this limitation will not change" is a promise, and here is the
exception it produces.

**The surprise: `poll` is 3.6× *worse* per fd than `select`.** 148 ns/fd vs 41 ns/fd. Two
reasons, and they are both instructive. In the kernel, `select`'s input is a **bitmap** —
128 bytes describes 1024 fds, so the scan is extremely cache-dense — while `poll`'s input
is an **array of 8-byte structs**, 8000 bytes for the same 1000 fds, or 62 cache lines
versus 1. On top of that, CPython's `poll` object stores its registrations in a dict and
materializes the `pollfd` array on each call, so part of that 148 ns/fd is userspace
marshalling rather than kernel scanning. The honest reading is: **`poll` did not beat
`select` on speed. It beat it on not falling over at fd 1024.** Both are O(n) and both
lose to the ready-list design by three orders of magnitude at scale.

> **The staff-level version of this table.** At 10 fds, `kqueue` is 2.3× faster than
> `select` — nobody's architecture is decided by that. At 4000 fds, `poll` is spending
> **594 µs per loop iteration doing nothing but bookkeeping**, which at any meaningful
> event rate means the event loop is now the bottleneck and the CPU is 100% busy scanning
> idle sockets. The curve, not the point measurement, is the argument. This is the same
> methodological point [`31-measurement-methodology.md`](31-measurement-methodology.md)
> makes about single-point benchmarks.

---

## 7. Level- vs edge-triggered, and the bug each invites

This is the README's Tier-1 checklist question, so here is the answer in full, with both
bugs reproduced.

**Level-triggered**: "tell me whenever this fd *is* readable."
**Edge-triggered**: "tell me when this fd *becomes* readable."

The difference only shows up when you do not consume everything.

### 7.1 The edge-triggered bug: read once, hang forever

**Measured on this machine**, using `kqueue` with `KQ_EV_CLEAR` (= `EPOLLET`). 100 bytes
arrive in one `send`. The handler deliberately reads only 10 of them, then waits again,
three times, with a 200 ms timeout:

```
--- A. LEVEL-triggered, read 10 of 100 bytes
    round 0: woken, data= 100 bytes pending, read 10
    round 1: woken, data=  90 bytes pending, read 10
    round 2: woken, data=  80 bytes pending, read 10

--- B. EDGE-triggered,  read 10 of 100 bytes        <-- THE BUG
    round 0: woken, data= 100 bytes pending, read 10
    round 1: TIMEOUT (no wakeup)

--- C. EDGE-triggered,  drain to EAGAIN
    round 0: woken, data= 100 bytes pending, read 100
    round 1: TIMEOUT (no wakeup)                     <-- correct: nothing left
```

In case B the connection is **permanently stalled with 90 bytes sitting in the socket
buffer.** No error, no exception, no log line. The peer is waiting for a response that
will never come; the fd is readable and nobody will ever be told. It will sit there until
a timeout somewhere else fires, which in most production systems means the client's.

This is *the* edge-triggered bug and it has a distinctive production signature:
**a small fraction of connections hang forever while the service otherwise looks perfectly
healthy.** It correlates with message size (large messages that don't fit one read),
appears under load, and cannot be reproduced by a single-request test. If you are handed
that symptom, "someone wrote an edge-triggered handler that doesn't drain" should be the
first hypothesis.

`epoll(7)` prescribes the fix as an explicit two-part contract:

> The suggested way to use **epoll** as an edge-triggered (**EPOLLET**) interface is as
> follows:
> (1) with nonblocking file descriptors; and
> (2) by waiting for an event only after `read(2)` or `write(2)` return **EAGAIN**.

Both halves are load-bearing. Without (1), the drain loop's final `read` **blocks forever**
— and now you have starved every *other* connection this thread was handling, which is the
same outage with a wider blast radius. `epoll(7)` says exactly this: an application using
`EPOLLET` "should use nonblocking file descriptors to avoid having a blocking read or
write starve a task that is handling multiple file descriptors."

### 7.2 The level-triggered bug: the busy loop

Level-triggered's failure is the mirror image, and it is much friendlier: **if you register
for writability and don't have anything to write, you get woken continuously.** A socket's
send buffer is almost always writable, so `EPOLLOUT` in level-triggered mode is a 100%-CPU
spin.

The correct level-triggered idiom is therefore asymmetric, and every event loop implements
it:

- **register for read permanently** — you almost always want to know about incoming data,
  and a readable socket you're not reading is backpressure, not a spin;
- **register for write only when a write returned `EAGAIN`, and deregister as soon as the
  buffer drains.**

That deregistration is a real `epoll_ctl` syscall per burst. It is the level-triggered
model's tax, and it is why `EPOLLET` exists at all.

There is a second, subtler level-triggered problem in multi-threaded servers: if several
threads call `epoll_wait` on the same epoll fd, a level-triggered ready fd wakes **all** of
them (thundering herd), and they race to handle the same connection. `EPOLLEXCLUSIVE` and
`EPOLLONESHOT` are the two standard answers.

### 7.3 The actual trade, measured

**Measured on this machine.** 8192 bytes were delivered into a socketpair (its buffer
capacity), then drained by each idiom:

| idiom | `kevent` calls | `recv` calls |
|---|---:|---:|
| level-triggered, one `recv` per wakeup | **2** | 2 |
| edge-triggered, drain to `EAGAIN` | **1** | 2 (+1 returning `EAGAIN`) |

Edge-triggering halved the readiness syscalls and added one `EAGAIN` `recv`. **That is the
entire trade.** Roughly: edge-triggering wins when a wakeup typically brings more data than
one `read` returns (high-throughput streaming, big messages), and loses when it brings less
(request/response with small messages), because then you pay a guaranteed extra `EAGAIN`
syscall to learn what you already knew.

### 7.4 What asyncio does, and why

`selectors` registers **level-triggered**, always. Doc 28 §9 states the reasoning and I
will not re-derive it: a partial read leaves the fd readable, so the loop calls you again;
edge-triggering would make every missed byte a permanently stalled connection — §7.1,
exactly. asyncio chose correctness over syscall count.

That is the right default for a general-purpose library, and it is worth understanding as a
*deliberate* choice rather than an oversight. The libraries that do choose edge-triggering
(nginx by default, libev optionally, some Rust runtimes) are ones where a single team owns
every handler and can enforce the drain contract by review. **A framework that runs
arbitrary user callbacks cannot enforce that contract, so it must not depend on it.**

---

## 8. `io_uring`: the completion model

### 8.1 Why it exists

Linux had asynchronous I/O before `io_uring`, and nobody liked it. LWN's Jonathan Corbet,
introducing `io_uring` in January 2019:

> While the kernel has had support for asynchronous I/O (AIO) since the 2.5 development
> cycle, it has also had people complaining about AIO for about that long. The current
> interface is seen as difficult to use and inefficient; additionally, some types of I/O
> are better supported than others.

The specific failures of Linux AIO (`io_setup`/`io_submit`/`io_getevents`):

- It only behaved asynchronously with **`O_DIRECT`**. Buffered `io_submit` would silently
  block, which defeats the entire purpose.
- **`io_submit` itself could block**, e.g. on metadata reads.
- It required **two syscalls and a copy** per batch, plus per-operation memory pinning.
- It covered a narrow slice of operations — no `accept`, no `send`/`recv`, no `openat`.

And Corbet's framing of the *general* problem, a year later, is the sentence to remember:

> Classic Unix I/O is inherently synchronous. As far as an application is concerned, an
> operation is complete once a system call like `read()` returns, even if some processing
> may continue behind its back. There is no way to launch an operation asynchronously and
> wait for its completion at some future time — a feature that many other operating systems
> had for many years before Unix was created.

Jens Axboe's `io_uring` landed in **kernel 5.1 (May 2019)**.

### 8.2 The two rings

The core idea is in one sentence of `io_uring_setup(2)`:

> The submission and completion queues are **shared between userspace and the kernel**,
> which eliminates the need to copy data when initiating and completing I/O.

`io_uring_setup(entries, params)` returns an fd. You then `mmap` that fd at three magic
offsets — `IORING_OFF_SQ_RING`, `IORING_OFF_CQ_RING`, `IORING_OFF_SQES` — to get shared
memory. (`io_uring(7)`'s example notes that since kernel 5.4, `IORING_FEAT_SINGLE_MMAP`
lets both rings come from one mapping; older kernels need three `mmap` calls.)

```
   ┌─────────────────────────── USER SPACE ────────────────────────────────┐
   │                                                                       │
   │  SQ ring (shared)                          CQ ring (shared)           │
   │  ┌───────────────────────┐                 ┌────────────────────────┐ │
   │  │ head (kernel writes)  │                 │ head (user writes)     │ │
   │  │ tail (USER writes) ───┼──┐           ┌──┼─ tail (kernel writes)  │ │
   │  │ ring_mask, array[]    │  │           │  │ cqes[]                 │ │
   │  └───────────────────────┘  │           │  └────────────────────────┘ │
   │            │                │           │              ▲              │
   │            ▼                │           │              │              │
   │  SQE array (shared)         │           │      ┌───────┴───────────┐  │
   │  ┌────────────────────────┐ │           │      │ struct io_uring_cqe│ │
   │  │ opcode  IORING_OP_READ │ │           │      │  user_data (echo) │  │
   │  │ fd, off, addr, len     │ │           │      │  res  (= retval)  │  │
   │  │ user_data  ← YOUR TAG  │ │           │      │  flags            │  │
   │  │ flags: IOSQE_IO_LINK…  │ │           │      └───────────────────┘  │
   │  └────────────────────────┘ │           │                             │
   └─────────────────────────────┼───────────┼─────────────────────────────┘
        ① fill SQEs, bump tail   │           │  ④ read CQEs, bump head
           (NO SYSCALL)          │           │     (NO SYSCALL)
                                 │           │
   ═══════════════════════════════╪═══════════╪═════════════════════════════
                                 ▼           │
   ┌─────────────────────────── KERNEL ──────┼─────────────────────────────┐
   │  ② io_uring_enter(fd, to_submit, min_complete, flags)                 │
   │       ...OR, with IORING_SETUP_SQPOLL, a kernel thread that is        │
   │          already polling the SQ tail — and then ② DOES NOT EXIST      │
   │  ③ execute: inline if it won't block, else hand to io-wq workers      │
   └───────────────────────────────────────────────────────────────────────┘
```

The submission and completion sides are **independent ring buffers with producer/consumer
indices in shared memory**. Filling an SQE and advancing the SQ tail is a couple of stores
and a memory barrier — the store-release/load-acquire discipline from
[`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md), applied across a
privilege boundary rather than between two threads. Reaping a CQE is a load and an index
bump. **Neither is a syscall.**

The only syscall in the steady state is `io_uring_enter(2)`, which does two things at once:
submit `to_submit` new SQEs, and optionally block until `min_complete` CQEs are available
(`IORING_ENTER_GETEVENTS`). From `io_uring(7)`:

> Optionally, `io_uring_enter(2)` can also wait for a specified number of requests to be
> processed by the kernel before it returns. If you specified a certain number of
> completions to wait for, the kernel would have placed at least those many number of CQEs
> on the CQ, which you can then readily read, right after the return from
> `io_uring_enter(2)`.

**One syscall submits N operations and harvests M results.** That is the entire cost-model
change, and everything else is elaboration.

### 8.3 Completions are unordered

`io_uring(7)` is emphatic, and this bites everyone once:

> It is important to remember that I/O requests submitted to the kernel can complete in any
> order. It is not necessary for the kernel to process one request after another, in the
> order you placed them. […] When you dequeue CQEs off the CQ, you should always check
> which submitted request it corresponds to. The most common method for doing so is
> utilizing the `user_data` field in the request, which is passed back on the completion
> side.

`user_data` is an opaque 64-bit value echoed into the CQE. It is your correlation ID —
usually a pointer to your own request struct. If you need ordering, you ask for it
explicitly with **`IOSQE_IO_LINK`**, which chains SQEs so each starts only after its
predecessor succeeds.

### 8.4 `IORING_SETUP_SQPOLL`: zero syscalls in the steady state

The flag that makes people's eyes widen. From `io_uring(7)`:

> With SQ Polling, **io_uring** starts a kernel thread that polls the submission queue for
> any I/O requests you submit by adding SQEs. With SQ Polling enabled, **there is no need
> for you to call `io_uring_enter(2)`**, letting you avoid the overhead of system calls. A
> designated kernel thread dequeues SQEs off the SQ as you add them and dispatches them for
> asynchronous processing.

Read that against §2's table. In the SQPOLL steady state, an application performs I/O by
**writing to memory**. The 81 ns trap does not happen — not amortized, *eliminated*. This
is the only mainstream mechanism on Linux for issuing I/O with literally zero syscalls.

The price is stated in the same `io_uring_params` struct: `sq_thread_cpu` and
`sq_thread_idle`. You are dedicating a kernel thread to spinning on your queue. If it goes
idle for `sq_thread_idle` milliseconds it sleeps, and you must then wake it with an
`io_uring_enter` carrying `IORING_ENTER_SQ_WAKEUP` — so the "zero syscalls" property holds
only while you are *continuously* busy. **SQPOLL trades a core for latency.** For a
storage engine saturating an NVMe device, that is an obviously good trade. For a web
service at 30% utilization, you have just burned a core to save 81 ns per request, and
you have made your cloud bill worse.

### 8.5 The rest of the toolkit

The features that turn the ring from "batched syscalls" into a different cost model. All
from `io_uring_setup(2)`, `io_uring_enter(2)`, `io_uring_register(2)`, and LWN's coverage.

| Feature | What it removes | Since |
|---|---|---|
| **`IORING_REGISTER_BUFFERS`** + `IORING_OP_{READ,WRITE}_FIXED` | per-op `get_user_pages` pinning/unpinning. LWN: "if the buffers will be used many times […] it is far more efficient to map them once and leave them in place." Counts against `RLIMIT_MEMLOCK`. | 5.1 |
| **`IORING_REGISTER_FILES`** + `IOSQE_FIXED_FILE` | per-op `fget`/`fput` and fd-table lookup. Direct descriptors can be *created* by `openat`/`accept` without ever entering the fd table. | 5.1 |
| **`IORING_SETUP_IOPOLL`** | the completion IRQ — busy-wait for completion instead. Requires a polling-capable block device and filesystem. | 5.1 |
| **`IOSQE_IO_LINK`** | a round trip per dependency: chain `openat`→`read`→`close` in one submission. | 5.3 |
| **`IORING_SETUP_R_DISABLED` + `IORING_REGISTER_RESTRICTIONS`** | attack surface — create the ring disabled, whitelist the opcodes you need, then enable. | 5.10 |
| **`IOSQE_CQE_SKIP_SUCCESS`** (`IORING_FEAT_CQE_SKIP`) | the completion entry itself, when success needs no acknowledgement. Errors still post a CQE. | 5.17 |
| **`IORING_SETUP_SUBMIT_ALL`** | a re-submit round trip when one SQE in a batch errors — continue instead of halting. | 5.18 |
| **`IORING_SETUP_COOP_TASKRUN`** | the IPI. By default io_uring interrupts a task running in userspace when a completion arrives; the man page calls this "overkill" for many cases and notes the cost of "the inter-processor interrupt used to do this, the kernel/user transition, [and] the needless interruption". | 5.19 |
| **Multishot ops** (`io_uring_multishot(7)`) | re-arming. One `accept` SQE yields a CQE per connection, indefinitely. | 5.19+ |
| **Provided buffers** (`io_uring_provided_buffers(7)`) | committing a buffer to each in-flight read. Hand the kernel a pool; it picks one at completion time. | 5.7+ |
| **`IORING_OP_EPOLL_WAIT`** | the last `epoll_wait` in a hybrid loop. The man page's stated use case: "for legacy event loops that still use epoll for some file descriptors", letting an application "unify its event handling through io_uring while maintaining backwards compatibility". | 6.15 |
| **`IORING_REGISTER_ZCRX_IFQ`** | the receive-side copy — zero-copy RX, completions posted as auxiliary CQEs. | 6.15 |

The presence of `IORING_OP_EPOLL_WAIT` is the most telling entry in that table. It is an
explicit migration ramp: it exists because real applications cannot convert to `io_uring`
atomically, and the kernel developers built them a bridge.

### 8.6 The security history, and why some deployments turn it off

`io_uring` has had a hard security record, and this is a legitimate part of the engineering
picture rather than a footnote.

The structural reason is visible in §8.5: `io_uring` is a **second, parallel entry point to
a large fraction of the kernel's I/O surface**, reachable from an unprivileged process, with
its own asynchronous execution context (the `io-wq` worker threads) that does not carry the
same credentials-and-context assumptions as a synchronous syscall. Anything that assumed
"this code runs in the caller's task context" was a potential bug.

LWN documented the friction directly in *Security requirements for new kernel features*
(Corbet, July 2022):

> The relatively new io_uring subsystem has changed the way asynchronous I/O is done on
> Linux systems and improved performance significantly. It has also, however, begun to run
> up a record of disagreements with the kernel's security community. A recent discussion
> about security hooks for the new `uring_cmd` mechanism shows how easily requirements can
> be overlooked in a complex system with no overall supervision.

That issue is worth understanding as a *pattern*: `uring_cmd` is io_uring's answer to
`ioctl` — a device-specific escape hatch — and the LSM hooks that let SELinux/AppArmor
mediate the equivalent `ioctl()` had not been wired up on the new path. Same operation,
second route, no mediation. That is the recurring shape of io_uring's security bugs:
**a policy check that existed on the old path and not the new one.**

The consequences in practice:

- The kernel gained explicit restriction machinery — `IORING_SETUP_R_DISABLED` plus
  `IORING_REGISTER_RESTRICTIONS` (§8.5) — so a process can create a ring, whitelist the
  opcodes it actually needs, and only then enable it. In anything security-sensitive this
  is not optional hardening; it is the intended usage. A system-wide `io_uring_disabled`
  sysctl exists on modern kernels for administrators who want it gone entirely.
- Container runtimes and seccomp profiles commonly block `io_uring_setup` outright, which
  means **"we'll switch to io_uring" can be blocked by your deployment platform rather
  than your code** — a real planning consideration.

Google publicly reported disabling io_uring across ChromeOS, Android, and their production
servers in 2023, citing exploitability. **I could not retrieve that primary source in this
session** (the URL 404'd) and it is listed in §18 accordingly; treat the specific claim as
widely-reported rather than as verified here. The *pattern* — that io_uring's attack
surface has caused multiple large operators to restrict it — is well supported by the LWN
coverage above and by the existence of the restriction machinery in the man pages.

---

## 9. What `io_uring` changes about an async server's cost model

This is the README's Tier-1 checklist question. Here is the answer as a cost model rather
than a slogan.

### 9.1 The readiness bill

Under `epoll`/`kqueue`, handling one message on one connection costs, at minimum:

```
   epoll_wait()  →  "fd 7 is readable"          ← 1 syscall (amortized over
                                                   the batch of ready fds)
   read(7, ...)  →  the bytes                    ← 1 syscall
   ... process ...
   write(7, ...) →  the reply                    ← 1 syscall
   [ if the write was short: epoll_ctl(MOD, EPOLLOUT), then later
     another epoll_wait, another write, another epoll_ctl(MOD) ]
```

**Measured on this machine** — an asyncio echo server over loopback TCP, 2000 round trips
of 32-byte messages, with every selector and socket call counted by a wrapper:

| operation | calls | per round trip |
|---|---:|---:|
| `select()` (→ `kevent`) | 8006 | **4.00** |
| `recv()` | 4001 | **2.00** |
| `send()` | 4000 | **2.00** |
| `register`/`modify` | 0 | 0.00 |
| **total** | **16011** | **8.01** |

A round trip is two legs (client→server, server→client), both through the same loop, so
that is **4 syscalls per one-way message: 2 readiness waits, 1 `recv`, 1 `send`.** Wall
time was 57.75 µs per round trip, which at 32 bytes is entirely overhead — Python's, mostly,
not the kernel's.

Note the zero in the `modify` row: the writes never went short, so the level-triggered
write-registration dance from §7.2 never fired. Under real backpressure it does, and the
bill goes up.

### 9.2 The completion bill

Under `io_uring`, the same work is:

```
   [fill N SQEs for N connections: recv, recv, recv, ...]   ← 0 syscalls
   io_uring_enter(submit=N, min_complete=M)                 ← 1 syscall
   [read M CQEs: here are the bytes]                        ← 0 syscalls
   [fill M SQEs: send, send, send, ...]                     ← 0 syscalls
   ...and the next io_uring_enter carries them
```

**With SQPOLL: zero syscalls.**

So the cost model changes along four axes:

| axis | readiness (`epoll`) | completion (`io_uring`) |
|---|---|---|
| **syscalls per op** | ~2–3, fixed | ~1/batch, → 0 with SQPOLL |
| **what a syscall costs you** | dominates at small payloads | amortized over batch depth |
| **disk I/O** | **cannot participate** — regular files are always "ready"; must go to a thread pool | first-class; `read`/`write`/`fsync`/`openat`/`statx` are all SQEs |
| **copies** | one `copy_to_user` per read | registered buffers avoid pinning; ZCRX avoids the receive copy |

**The third row is the one people underrate.** `epoll` fundamentally cannot do file I/O
(§4.1), which is why every readiness-based async runtime — asyncio, Node, Netty — hands
disk reads to a thread pool and pays a thread handoff plus two context switches per file
operation. `io_uring` erases that entire category of workaround. For a server that mixes
sockets and files (any static file server, any database, any log shipper), that is a bigger
architectural change than the syscall count.

### 9.3 When it does not help

Be able to say this part too, because it is what separates a considered answer from a
recited one:

- **If your batch depth is 1, you save almost nothing.** One SQE plus one
  `io_uring_enter` is one syscall — the same as `read()`. `io_uring`'s advantage is
  *amortization*, and amortization needs a batch. A low-QPS service gets the complexity
  and none of the win.
- **If you are not syscall-bound, it is irrelevant.** At 4 syscalls × ~1 µs of real kernel
  work per request against 5 ms of request handling, you are optimizing 0.08% of your
  latency. Profile first — [`32-profiling.md`](32-profiling.md) exists for this.
- **The completion model is harder to write.** Buffer lifetime becomes your problem: a
  buffer handed to an SQE is owned by the kernel until its CQE arrives, and you cannot
  free, resize, or reuse it before then. Cancellation becomes asynchronous (there is a
  whole man page, `io_uring_cancelation(7)`). This is a genuine source of use-after-free
  bugs in the same family as the reclamation problems in
  [`03-lockfree-and-reclamation.md`](03-lockfree-and-reclamation.md).
- **You may not be allowed to use it** (§8.6).
- **There is no `io_uring` event loop in the Python standard library**, and doc 28 §9
  explains why: `BaseSelectorEventLoop` is built on the readiness model at the API level.
  `add_reader(fd, callback)` is not expressible as a completion. Supporting `io_uring`
  properly means a different loop class, the way Windows needed `ProactorEventLoop`.
  Third-party bindings exist (`liburing` wrappers, and Rust/C++ runtimes like tokio-uring
  and Seastar), but from CPython today this is a "call into a native extension" story, not
  a stdlib one.

---

## 10. The page cache

Every ordinary `read()` and `write()` goes through the page cache. It is not a
nice-to-have; it is the reason file I/O is usable at all.

```
   write(fd, buf, n)                            read(fd, buf, n)
        │                                             │
        ▼                                             ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  PAGE CACHE  — kernel-managed, page-granular, unified with mmap │
   │                                                                 │
   │   [clean][clean][DIRTY][clean][DIRTY][clean] …                  │
   │      ▲                    │                                     │
   │      │ hit: memcpy        │ miss: block until the read finishes │
   │      │ (~15 GiB/s)        ▼        (~1 GiB/s here)              │
   └──────┼─────────────────── │ ────────────────────────────────────┘
          │                    │        ▲
          │        writeback   │        │ readahead: the kernel notices
          │        (background │        │ sequential access and fetches
          │         flusher,   ▼        │ ahead of you, asynchronously
          │         or fsync)  │        │
   ┌──────┴────────────────────▼────────┴────────────────────────────┐
   │  BLOCK LAYER  → I/O scheduler → driver → device queue           │
   └─────────────────────────────────┬───────────────────────────────┘
                                     ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  DEVICE  ── and its own volatile write cache ──                 │
   │  ★ data acknowledged here is NOT durable until the cache is     │
   │    flushed. This is §11's entire subject.                       │
   └─────────────────────────────────────────────────────────────────┘
```

### 10.1 What a cache hit is worth

**Measured on this machine.** Four large binaries that had not been touched this session,
each read once (cold), then twice more (warm). 256 KiB reads. No cache purging was
performed — the cold reads are genuinely first-touch, and there is exactly one of them per
file, which is why this table has no error bars.

| file | size | cold | cold MiB/s | warm | warm MiB/s | **ratio** |
|---|---:|---:|---:|---:|---:|---:|
| `dsymutil` | 93.4 MiB | 94.16 ms | 992 | 5.92 ms | 15,790 | **15.9×** |
| `sourcekit-lsp` | 70.3 MiB | 76.74 ms | 917 | 4.60 ms | 15,296 | **16.7×** |
| `tapi` | 48.1 MiB | 50.80 ms | 947 | 3.10 ms | 15,538 | **16.4×** |
| `libswiftCore.dylib` | 22.1 MiB | 20.72 ms | 1,066 | 1.21 ms | 18,226 | **17.1×** |

Four independent files, four independent cold reads, **16–17× every time.** The third read
matched the second in every case, so the warm number is stable and not a one-off.

The practical readings:

- **Your benchmark is measuring the page cache.** If a storage benchmark's numbers improve
  on re-run, it is not warmed up — it is invalid.
- **A cold-start latency budget is a different budget.** A service whose steady-state p99
  is fine can miss its SLO for minutes after a deploy purely on cache repopulation. This is
  a real and frequently misdiagnosed incident shape.
- **Container memory limits evict page cache.** A cgroup v2 memory limit counts page cache
  against you; under pressure the kernel reclaims it, and a service that was hitting cache
  starts hitting disk with no code change and no traffic change. That is a doc 07 / doc 46
  topic, but this is where the 16× comes from.

### 10.2 Readahead

The kernel detects sequential access and fetches ahead of the application, so a
`read()`-in-a-loop is not a serial chain of device round trips. Two Linux interfaces let
you influence it:

- **`posix_fadvise(2)`** — declare intent over a byte range. `POSIX_FADV_SEQUENTIAL`
  doubles the readahead window; `POSIX_FADV_RANDOM` disables it; `POSIX_FADV_WILLNEED`
  starts a fetch now; `POSIX_FADV_DONTNEED` drops those pages from the cache.
  `POSIX_FADV_NOREUSE` has a small history worth knowing — the man page records that
  "before Linux 2.6.18 [it] had the same semantics as `POSIX_FADV_WILLNEED`. This was
  probably a bug; from Linux 2.6.18 until Linux 6.2 this flag was a no-op. Since Linux 6.3,
  `POSIX_FADV_NOREUSE` signals that the kernel page replacement algorithm can ignore access
  to mapped page cache marked by this flag." A flag that did nothing for sixteen years is a
  useful reminder to check the version, not just the header.
- **`readahead(2)`** — the imperative version: "initiates readahead on a file so that
  subsequent reads from that file will be satisfied from the cache, and not block on disk
  I/O (assuming the readahead was initiated early enough and that other activity on the
  system did not in the meantime flush pages from the cache)." Note the parenthetical: it
  is a hint with two failure conditions, not a guarantee.

`POSIX_FADV_DONTNEED` is the honest way to run a cold-cache benchmark on Linux — it drops
one file's pages rather than nuking the machine's cache with `drop_caches`.

### 10.3 Writeback

A `write()` that hits the page cache marks pages dirty and returns. **Measured**: 1.80 µs
for a 4 KiB `write()` — 554,000 writes per second — with the data nowhere near the device
(§11's table). Actually getting it to storage happens later, driven by background flusher
threads under thresholds like Linux's `dirty_ratio` / `dirty_background_ratio`, or
immediately by `fsync`.

Two consequences engineers routinely get wrong:

- **`write()` returning success means nothing about durability.** It means the kernel
  accepted the bytes. §11.
- **Writeback can stall you anyway.** When dirty pages exceed `dirty_ratio`, `write()`
  starts blocking, throttled by the kernel to keep the dirty set bounded. A program "just
  writing to a buffer" suddenly has multi-millisecond `write()` calls, and the latency
  spike appears in code containing no I/O wait you can see.

### 10.4 A null result

**Measured, and it did not show what I expected.** macOS's `F_NOCACHE` fcntl is often
described as the rough analogue of `O_DIRECT`. Reading a 48 MiB file three ways, 5
alternating passes:

| mode | ms | MiB/s | spread |
|---|---:|---:|---:|
| page cache (warm) | 2.42 | 19,822 | 7.2% |
| `F_NOCACHE` | 2.48 | 19,383 | 14.4% |
| `F_NOCACHE` + `F_RDAHEAD` off | 2.45 | 19,562 | 18.5% |

**Ratio: 1.0×.** `F_NOCACHE` produced no measurable slowdown, which means it did not
produce a device read — the file was already resident from having been written, and
`F_NOCACHE` on this platform means "do not *retain* these pages" rather than "do not *use*
the cache". The 20 GiB/s figure is memory bandwidth in all three rows.

I am reporting this as a **null result, not a finding**. It is also why §10.1 uses
never-before-read files rather than `F_NOCACHE` to get its cold numbers: when the
instrument does not do what you assumed, change the instrument, do not reinterpret the
reading.

---

## 11. `fsync` does not mean what you think

This is a **correctness** section that happens to have performance numbers attached.

### 11.1 What the call promises

`fsync(2)`:

> `fsync()` transfers ("flushes") all modified in-core data of (i.e., modified buffer cache
> pages for) the file referred to by the file descriptor `fd` to the disk device (or other
> permanent storage device) so that all changed information can be retrieved even if the
> system crashes or is rebooted. **This includes writing through or flushing a disk cache if
> present.** The call blocks until the device reports that the transfer has completed.

Two clauses in that page are the ones that get missed:

> As well as flushing the file data, `fsync()` also flushes the metadata information
> associated with the file.
>
> **Calling `fsync()` does not necessarily ensure that the entry in the directory containing
> the file has also reached disk. For that an explicit `fsync()` on a file descriptor for
> the directory is also needed.**

**You must fsync the directory too.** Write `data.tmp`, fsync it, rename it over `data`,
and then crash — and the rename may not have reached storage. The file you carefully made
durable is not reachable. The correct atomic-replace sequence is:

```
write(tmp) → fsync(tmp) → close(tmp) → rename(tmp, final) → fsync(dirfd)
```

Every one of those steps is load-bearing, and the last one is the one that gets omitted.

`fdatasync(2)` is the cheaper sibling: it skips metadata "unless that metadata is needed in
order to allow a subsequent data retrieval to be correctly handled" — so `st_mtime` is
skipped but a file-size change is not. For append-heavy workloads on Linux this is a real
saving, because a size update forces an inode write. **It does not exist on this platform**
*(verified: `hasattr(os, "fdatasync")` is `False` on macOS)*, which is worth knowing before
you write portable code that assumes it.

### 11.2 The durability gap, measured

**Measured on this machine**, APFS on the internal SSD. 200 × 4 KiB appends per pass, 5
alternating passes, medians:

| mode | µs per op | ops/s | vs `fsync` |
|---|---:|---:|---:|
| `write()` only | **1.80** | 554,338 | 0.07× |
| `fsync()` | **24.83** | 40,269 | 1× |
| `F_BARRIERFSYNC` | 298.87 | 3,346 | 12.0× |
| **`F_FULLFSYNC`** | **2941.40** | **340** | **118.4×** |

Three orders of magnitude separate "the kernel accepted my bytes" from "the drive has
promised me the bytes survive power loss."

> **Independent reproduction — and a caveat on the ratio.** The same experiment run
> separately (25 × 4 KiB `pwrite` at increasing offsets, median of 25) gave
> **`fsync` 50.8 µs, `F_FULLFSYNC` 2992.2 µs — a ratio of 59.0×, not 118.4×.**
>
> Look at *which* number moved. `F_FULLFSYNC` reproduced almost exactly (2992 vs 2941 µs,
> 1.7% apart), and so did the durable-commit ceiling (**334/s vs 340/s**). The entire
> discrepancy is in the `fsync` baseline, which doubled (50.8 vs 24.83 µs) under a
> different write pattern and machine load — unsurprising, since `fsync` here only has to
> reach the kernel, and that cost depends on how much dirty data is pending.
>
> **So treat the ~3 ms and the ~340 durable commits/s as the robust findings, and the
> multiplier as indicative.** The ratio is a quotient of one stable number and one noisy
> one; quoting it to four significant figures, as the table above does, overstates its
> precision.
>
> Note also that **`fcntl.F_BARRIERFSYNC` does not exist in Python's `fcntl` module**
> (`hasattr` is `False` on 3.14.6); reaching it requires the raw constant `85`. Combined
> with its 3× measured spread (§18), the 12.0× row is the least trustworthy in the table.

**`fsync` on this platform does not flush the drive's write cache.** The system header says
so in the comment next to the constant *(verified,
`$(xcrun --show-sdk-path)/usr/include/sys/fcntl.h`)*:

```c
#define F_PREALLOCATE     42   /* Preallocate storage */
#define F_RDAHEAD         45   /* turn read ahead off/on for this fd */
#define F_NOCACHE         48   /* turn data caching off/on for this fd */
#define F_FULLFSYNC       51   /* fsync + ask the drive to flush to the media */
#define F_BARRIERFSYNC    85   /* fsync + issue barrier to drive */
```

*"fsync + ask the drive to flush to the media"* — the `+` is the admission. `fsync` alone
gets the data out of the page cache and into the device; `F_FULLFSYNC` additionally tells
the device to commit its volatile cache to the medium. The 118× gap is the physical cost of
that promise.

`F_BARRIERFSYNC` sits between them: it issues a **barrier** rather than a full flush, so
writes cannot be reordered across it but are not necessarily on the medium yet. That is
enough for journal/log ordering (which is what most databases actually need for
crash-consistency) at 12× rather than 118×. Its measured spread was wide (161–504 µs vs
`F_FULLFSYNC`'s tight 2865–3006 µs), consistent with it queueing a barrier rather than
waiting on a physical commit.

**How this is a correctness finding, not a performance one.** Any Python code that does
this:

```python
f.write(data)
f.flush()          # userspace buffer -> kernel
os.fsync(f.fileno())   # kernel -> device... and no further
```

is *not* crash-safe against power loss on this platform, and the code contains no hint of
that. Getting the real guarantee requires the platform-specific call:

```python
import fcntl, os
f.flush()
try:
    fcntl.fcntl(f.fileno(), fcntl.F_FULLFSYNC)   # macOS: 340 ops/s
except (AttributeError, OSError):
    os.fsync(f.fileno())                          # elsewhere
```

SQLite has shipped this branch for two decades; most application code has not. And note
what the number means for design: **340 durable commits per second is your ceiling.** If
your design assumed thousands, the design is wrong, not the disk.

### 11.3 The fsync-gate: when `fsync` reports success after losing your data

The deeper problem is not latency. It is that **on Linux, `fsync` could return success
after an I/O error had already destroyed your data** — and the incident that surfaced it is
the best worked example of "the guarantee you assumed was never offered" in systems
programming.

Craig Ringer reported it to pgsql-hackers in late March 2018. LWN's account (Corbet, April
18, 2018):

> Developers of database management systems are, by necessity, concerned about getting data
> safely to persistent storage. So when the PostgreSQL community found out that the way the
> kernel handles I/O errors could result in data being lost without any errors being
> reported to user space, a fair amount of unhappiness resulted. […]
>
> In short, PostgreSQL assumes that a successful call to `fsync()` indicates that all data
> written since the last successful call made it safely to persistent storage. But that is
> not what the kernel actually does. **When a buffered I/O write fails due to a
> hardware-level error, filesystems will respond differently, but that behavior usually
> includes discarding the data in the affected pages and marking them as being clean.** So
> a read of the blocks that were just written will likely return something other than the
> data that was written.

Unpack the failure, because the shape of it generalizes:

1. Postgres writes to a data file. The bytes land in the page cache. `write()` returns 0.
2. Background writeback tries to put the page on disk. **It fails** — a bad sector, a
   thin-provisioned volume out of space, an unplugged USB disk, an EBS volume having a bad
   day.
3. The filesystem marks the page **clean** and discards it. The dirty data is simply gone.
4. Postgres calls `fsync()`. The error is reported **once**, to whoever calls `fsync` next.
5. Postgres, following the then-universal convention, treated the `fsync` error as
   retryable and **called `fsync` again**. The second call found no dirty pages — they were
   discarded in step 3 — and **returned success.**
6. Postgres concluded the checkpoint was durable and advanced the WAL. The data was gone,
   and now so was the ability to replay it.

Two independent bugs compound here: **the error is reported at most once** (and to an
arbitrary fd, not necessarily yours), and **retrying `fsync` after a failure "succeeds"
while the data is unrecoverable.**

Both sides changed:

- **The kernel side.** Linux 4.13 replaced the per-`address_space` error flag with
  `errseq_t`-based writeback error tracking, so an error is reported to *every* file
  description open at the time of the error rather than only to the first caller. The
  PostgreSQL wiki lists the specific commits — "fs: new infrastructure for writeback error
  handling and reporting", "ext4: use errseq_t based error handling for reporting data
  writeback errors", and a `Documentation/` update fleshing out the vfs.txt section on
  storing and reporting writeback errors. This narrowed the reporting hole; it did not make
  the discarded data come back.
- **The PostgreSQL side.** The project's own wiki records the resolution: "As of this
  PostgreSQL 12 commit, PostgreSQL will now **PANIC on fsync() failure**. It was backpatched
  to PostgreSQL 11, 10, 9.6, 9.5 and 9.4." A PANIC forces a crash-recovery cycle from the
  WAL, which is the only remaining correct response.

**The engineering lesson generalizes far past Postgres:** an `fsync` error is not a
transient failure you retry. It is an assertion that your in-memory state and your on-disk
state have diverged irrecoverably, and the only safe responses are to crash and recover
from a log, or to fail loudly. If you have ever written `except OSError: retry()` around an
`fsync`, you have written this bug. The PostgreSQL wiki also notes this "turns out not to be
unique to Linux" — the problem is inherent to buffered I/O with delayed error reporting, not
to one kernel.

---

## 12. Zero-copy: `sendfile`, `splice`, `MSG_ZEROCOPY`

The normal way to send a file over a socket is embarrassing:

```
   DISK ──DMA──▶ page cache ──copy──▶ user buffer ──copy──▶ socket buffer ──DMA──▶ NIC
                              ↑                       ↑
                          read()                  write()
                     2 syscalls, 2 CPU copies, and the data was never
                     even looked at by your process
```

You paid two traps and two full memory copies to move bytes you never examined. The
zero-copy family exists to delete both copies.

### 12.1 `sendfile`

```c
ssize_t sendfile(int out_fd, int in_fd, off_t *offset, size_t count);
```

`sendfile(2)` states the point directly:

> `sendfile()` copies data between one file descriptor and another. **Because this copying
> is done within the kernel**, `sendfile()` is more efficient than the combination of
> `read(2)` and `write(2)`, which would require transferring data to and from user space.

Constraints, all from the man page and all things people trip on:

- `in_fd` "must correspond to a file which supports `mmap(2)`-like operations (i.e., it
  cannot be a socket)". Socket-to-socket proxying is not `sendfile`'s job — that is
  `splice`'s.
- Since Linux 5.12, `out_fd` may be a pipe (`sendfile` desugars to `splice`); historically
  `out_fd` had to be a socket.
- It transfers at most `0x7ffff000` (2,147,479,552) bytes per call, on both 32- and 64-bit
  systems. Loop.
- If `out_fd` is a socket or pipe with zero-copy support, "callers must ensure the
  transferred portions of the file referred to by `in_fd` remain unmodified until the reader
  on the other end of `out_fd` has consumed the transferred data." **Zero-copy means the
  kernel is referencing your pages, not copying them.** Modify the file mid-flight and you
  will send whatever is there when the DMA happens.
- Applications should fall back to `read`/`write` on `EINVAL` or `ENOSYS`.

**Measured on this machine.** `os.sendfile` exists on macOS with a different signature and
different semantics from Linux's (Darwin's takes and updates a length in/out; Linux's takes
an offset pointer), but the mechanism is the same. Sending a 48 MiB file into an AF_UNIX
socketpair drained by a reader thread, median of 3:

| method | ms | MiB/s |
|---|---:|---:|
| `os.read()` + `sock.sendall()` (256 KiB chunks) | 33.60 | 1,429 |
| **`os.sendfile()`** | **6.43** | **7,463** |

**5.2× faster**, and the CPU that would have run those `memcpy`s is available for something
else. The gap is larger than a naive "we saved two copies" estimate predicts, because
`sendfile` also eliminated ~192 `read`+`send` syscall pairs and all the associated Python
object allocation.

This is why every serious static file server (nginx, Apache with `EnableSendfile`, Caddy)
uses it, and why `http.server` — which does not — is a toy.

### 12.2 `splice`

```c
ssize_t splice(int fd_in, off64_t *off_in, int fd_out, off64_t *off_out,
               size_t len, unsigned int flags);
```

`splice` is the general form, with one architectural constraint: **one end must be a pipe.**
As `sendfile(2)` puts it, "the Linux-specific `splice(2)` call supports transferring data
between arbitrary file descriptors provided one (or both) of them is a pipe."

The pipe is not an inconvenience; it is the mechanism. A Linux pipe *is* a ring of page
references, so "splice into a pipe" means "make the pipe point at these pages" and "splice
out of a pipe" means "hand those page references to the destination". Nothing is copied;
page pointers move. `SPLICE_F_MOVE` was the flag intended to make that explicit (it has
been a no-op for a long time, another version-check reminder), and `SPLICE_F_MORE` hints
that more data is coming, which matters for packet coalescing.

The idiom for socket-to-socket proxying — which `sendfile` cannot do — is therefore a pipe
used as a kernel-side bounce buffer that never touches userspace:

```
   socket_in ──splice──▶ [pipe] ──splice──▶ socket_out
```

`tee(2)` duplicates pipe contents without consuming them (for the "log it and forward it"
shape), and `vmsplice(2)` maps user pages into a pipe.

### 12.3 `MSG_ZEROCOPY`

For data your process *does* generate — a serialized protobuf, a rendered response —
`sendfile` does not apply because the bytes are in your heap, not in the page cache.
`MSG_ZEROCOPY` (Linux 4.14+) is the answer for `send()`: the kernel pins your pages and
transmits from them directly instead of copying into socket buffers.

The kernel's own documentation is refreshingly candid about the catch:

> Passing flag MSG_ZEROCOPY is a hint to the kernel to apply copy avoidance, and a contract
> that the kernel will queue a completion notification. **It is not a guarantee that the
> copy is elided.**
>
> Copy avoidance is not always feasible. Devices that do not support scatter-gather I/O
> cannot send packets made up of kernel generated protocol headers plus zerocopy user data.
> A packet may need to be converted to a private copy of data deep in the stack, say to
> compute a checksum.

And the sharpest warning:

> **Deferred copies can be more expensive than a copy immediately in the system call, if the
> data is no longer warm in the cache.** The process also incurs notification processing cost
> for no benefit. For this reason, the kernel signals if data was completed with a copy, by
> setting flag `SO_EE_CODE_ZEROCOPY_COPIED` in field `ee_code` on return. A process may use
> this signal to stop passing flag MSG_ZEROCOPY on subsequent requests on the same socket.

So `MSG_ZEROCOPY` can be **slower** than a plain `send`, and the kernel provides a bit
telling you so, and correct use means *reading that bit and adapting*. That is an unusually
honest API.

The other cost is asynchrony. Your buffer is not free when `send()` returns — it is free
when a completion notification arrives on the socket's error queue (`MSG_ERRQUEUE`), which
you must `poll` and `recvmsg` for. Per the docs, "in practice, it is more efficient to not
wait for notifications, but read without blocking every couple of send calls." You have
just turned a synchronous send into a completion-model operation with buffer-lifetime
management — §9.3's complexity argument, in miniature. The documented threshold is that it
pays off above roughly 10 KiB per send; below that, the notification bookkeeping dominates.

Note also: "A zerocopy completion notification is **not** a transmit completion
notification" — it tells you the kernel released your pages, not that the peer got the data.

### 12.4 The honest summary

| technique | moves data between | copies removed | main catch |
|---|---|---|---|
| `sendfile` | file → socket/pipe | both | source must be mmap-able; don't modify in flight |
| `splice` | anything ↔ anything, via a pipe | both | needs a pipe; Linux-only |
| `vmsplice` / `tee` | user pages → pipe / pipe → pipe | both | niche |
| `MSG_ZEROCOPY` | user memory → socket | the send-side copy | not guaranteed; async buffer lifetime; >10 KiB |
| `io_uring` ZCRX | NIC → user memory | the receive-side copy | 6.15+, needs NIC support |

**Zero-copy is a bandwidth optimization, not a latency one.** It buys back CPU and memory
bandwidth on large transfers. If your payloads are 200 bytes, none of this matters and the
syscall count (§14) is your problem instead.

---

## 13. `O_DIRECT`: bypassing the cache on purpose

`open(2)`'s `O_DIRECT` flag tells the kernel: do not use the page cache; DMA between the
device and my buffer.

You want it in exactly one situation: **you are a database, and you have a better cache
than the kernel does.** Postgres (partly), MySQL/InnoDB, Oracle, and most storage engines
maintain their own buffer pool with domain knowledge the kernel lacks — which pages are
index nodes, which are about to be checkpointed, which are cold. Running that on top of the
page cache means every page is cached twice, once in your pool and once in the kernel's, and
your memory budget is silently halved. That is the *double caching* problem, and it is the
real motivation.

The costs are steep and the man page enumerates them:

- **Alignment.** Transfer length, memory buffer address, and file offset must all be
  aligned to the logical block size (historically 512 bytes; 4096 on modern devices;
  `statx()` with `STATX_DIOALIGN` is the modern way to ask). Get it wrong and you get
  `EINVAL` — not a slow path, an error. In practice: `posix_memalign` for every buffer.
- **No readahead.** You are the readahead now. Sequential scans the kernel would have
  pipelined become serial device round trips unless you implement your own prefetch — which
  is precisely why `O_DIRECT` and `io_uring` are so often used together: you need deep
  queues to hide the latency you just took responsibility for.
- **Semantics are underspecified.** Mixing `O_DIRECT` and normal I/O on the same file, or
  `O_DIRECT` and `mmap`, has undefined coherence behaviour; NFS has its own rules entirely.
- **It does not imply durability.** `O_DIRECT` bypasses the page cache, not the device's
  volatile write cache. You still need `fsync`/`fdatasync` (§11), and this surprises
  people constantly.

The man page also reproduces Linus Torvalds' famous verdict, which is worth quoting because
it captures the design tension precisely: `O_DIRECT` is "a horrible interface that was
probably designed by a deranged monkey on some serious mind-controlling substances." The
kernel community's position has consistently been that applications should use `madvise`
and `fadvise` to *inform* the cache rather than bypass it, and the database community's
position has consistently been that they cannot. Both are right about their own workload.

> **Note on the platform used for measurement.** This machine has no `O_DIRECT`;
> `F_NOCACHE` is the nearest analogue and §10.4 shows it did not behave like one — it is a
> retention hint, not a bypass, and it happily accepted a deliberately unaligned read
> (offset 1, length 999) that `O_DIRECT` would reject with `EINVAL` *(measured)*. None of
> §13's `O_DIRECT` behaviour was measured here; it is from the man page.

---

## 14. Batching: the only general-purpose fix

Everything above is a specific mechanism. This section is the general principle, and it is
the one to apply when you do not have `io_uring`.

**A syscall has a large fixed cost and a small marginal cost. Therefore move more data per
syscall.** That is the whole idea, and it is worth measuring because the numbers are more
dramatic than intuition suggests.

### 14.1 Buffer size vs throughput

**Measured on this machine.** A warm 32 MiB file read with `os.read(fd, N)` for varying N.
Syscall counts are exact (`bytes / N + 1`), medians of 5 passes:

| read size | syscalls | ms | MiB/s | ns per syscall |
|---:|---:|---:|---:|---:|
| 64 B | 524,289 | 213.55 | **149.8** | 407.3 |
| 512 B | 65,537 | 29.55 | 1,083.0 | 450.9 |
| 4 KiB | 8,193 | 4.85 | 6,596.6 | 592.1 |
| 16 KiB | 2,049 | 2.61 | 12,267.0 | 1,273.1 |
| 64 KiB | 513 | 1.67 | 19,173.2 | 3,253.4 |
| **256 KiB** | **129** | **1.54** | **20,813.0** | 11,918.6 |
| 1 MiB | 33 | 2.21 | 14,450.2 | 67,106.1 |
| 4 MiB | 9 | 2.47 | 12,955.0 | 274,453.7 |

**139× throughput between the worst and best buffer size, on identical data with identical
code.** The only variable is how many times you cross into the kernel.

Read the last column as the cost model: at 64 bytes a `read` costs 407 ns, of which only a
few nanoseconds is the copy — that number *is* the fixed overhead. It stays near 450–590 ns
through 4 KiB, then starts growing as the copy begins to dominate.

**And it gets worse past 256 KiB.** 1 MiB reads are 30% *slower* than 256 KiB reads. The
syscall count is already negligible at that point, so the fixed cost has stopped mattering,
and what is left is that a 1 MiB destination buffer no longer fits comfortably in cache —
you are now paying to evict and refill L2 on every call. This is
[`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) asserting itself
inside an I/O benchmark, and it is the reason "just use a bigger buffer" is wrong advice
past a point. **The sweet spot is where syscall overhead has become negligible and the
buffer still fits in cache** — here, 64–256 KiB.

### 14.2 `writev`: batching without a bigger buffer

Sometimes you cannot use one big buffer because your data is in pieces — a header, a body,
a trailer. Copying them together to make one `write()` is a copy you did not need.
`writev`/`readv` take an array of `iovec`s and do one syscall over all of them.

**Measured on this machine.** 4096 chunks of 4 KiB each, moved to `/dev/null` in batches of
K per `writev` call:

| K (iovecs/call) | syscalls | total µs | ns per chunk | ns per syscall |
|---:|---:|---:|---:|---:|
| 1 | 4096 | 2385.8 | **582.5** | 582.5 |
| 2 | 2048 | 1238.8 | 302.4 | 604.9 |
| 4 | 1024 | 576.9 | 140.8 | 563.4 |
| 8 | 512 | 336.1 | 82.1 | 656.4 |
| 16 | 256 | 206.7 | 50.5 | 807.3 |
| 32 | 128 | 130.0 | 31.7 | 1015.6 |
| 64 | 64 | 146.0 | 35.6 | 2280.6 |
| 128 | 32 | 77.9 | 19.0 | 2434.9 |
| 256 | 16 | 103.7 | 25.3 | 6481.8 |
| 512 | 8 | 61.5 | 15.0 | 7692.6 |
| **1024** | **4** | **57.5** | **14.0** | 14375.0 |

Fit the last column and the cost model falls out cleanly:

```
   cost(K) ≈ 570 ns  +  13.5 ns × K
             ^^^^^^     ^^^^^^^^^^^
             the trap   per-iovec marginal cost
```

**The fixed cost is 42× the marginal cost.** That single ratio is the justification for
every batching API in this document — `writev`, `epoll_wait`'s `maxevents`, `recvmmsg`,
`io_uring`'s SQ depth, and every "flush every N records" in every logging library ever
written. It is also why the curve flattens: once K is large enough that 13.5×K dominates
570, further batching buys nothing.

### 14.3 The general rule

> **The cost of a syscall is fixed. The value of a syscall is proportional to how much work
> it carries. Batching is not an optimization; it is the design.**

Where this shows up in Python, concretely:

- `socket.sendall()` on a list of small pieces → build one `bytes`, or use `sendmsg` with
  multiple buffers.
- `f.write()` per log line with `flush=True` → the flush *is* the syscall; buffer instead.
- `cursor.execute()` per row → `executemany`, or `COPY`.
- `os.stat()` per file in a directory walk → `os.scandir()`, which reuses the directory
  read's data and is the whole reason it was added.

---

## 15. CPython's I/O stack

### 15.1 The three layers

`io` is three layers, and the file you get from `open()` depends on the arguments:

```
   ┌───────────────────────────────────────────────────────────────┐
   │ TextIOWrapper        str  ⇄  bytes                            │  open("f")
   │   encoding, newline translation, incremental decode           │
   ├───────────────────────────────────────────────────────────────┤
   │ BufferedReader / BufferedWriter / BufferedRandom              │  open("f","rb")
   │   the syscall-amortization layer. io.DEFAULT_BUFFER_SIZE.     │
   ├───────────────────────────────────────────────────────────────┤
   │ FileIO  (io.RawIOBase)                                        │  open("f","rb",
   │   one method = one syscall. read/readinto/write/seek.         │        buffering=0)
   └───────────────────────────────────────────────────────────────┘
                              │
                              ▼   read(2) / write(2) / lseek(2)
```

`open(path)` gives you all three stacked; `open(path, "rb")` gives you two; `buffering=0`
gives you the raw layer (and is only legal in binary mode — there is no unbuffered text
mode, because incremental decoding needs a buffer).

### 15.2 What the buffering layer actually does, measured

**Measured on this machine.** The same 32 MiB file, read with `f.read(4096)` in a loop,
through different buffering settings. Raw `readinto`/`read` calls were counted by
subclassing `io.FileIO`:

| configuration | raw syscalls | ms |
|---|---:|---:|
| `buffering=0` (raw `FileIO`) | 8,193 | 6.57 |
| `buffering=4096` | **8,193** | 6.25 |
| `buffering=65536` | 513 | 3.42 |
| `buffering=131072` (= `io.DEFAULT_BUFFER_SIZE`) | 257 | 3.55 |
| `buffering=1048576` | 33 | 3.03 |

Two findings.

**`buffering=4096` did nothing.** Same 8,193 syscalls as unbuffered. `BufferedReader`
bypasses its own buffer when the requested read is at least the buffer size — copying 4096
bytes through a 4096-byte buffer would be pure overhead, so it doesn't. The lesson: **a
buffer smaller than or equal to your read size is not a buffer.** If you set
`buffering=4096` and read in 4 KiB chunks believing you have buffered I/O, you have raw I/O
with extra object overhead. Buffer size must exceed read size by a healthy margin to do
anything at all.

**`io.DEFAULT_BUFFER_SIZE` is 131072** on CPython 3.14.6 *(verified)*. It was 8192 for most
of Python's history; the 16× increase is a recent change and matches §14.1's finding that
the sweet spot is tens-to-hundreds of KiB. If you have a mental model that says "Python
reads files 8 KiB at a time", update it.

### 15.3 The GIL is released around blocking I/O

Every blocking call in `posixmodule.c` and `socketmodule.c` is wrapped:

```c
Py_BEGIN_ALLOW_THREADS
n = read(fd, buf, count);
Py_END_ALLOW_THREADS
```

This is why threads are a *correct* (if unfashionable) answer to I/O concurrency in Python,
and why "the GIL makes Python bad at concurrency" is wrong as stated: the GIL makes Python
bad at **CPU** parallelism. A hundred threads blocked in `recv` hold zero GILs.

[`24-the-gil.md`](24-the-gil.md) §5 has the full list of what releases the GIL and what does
not, and [`17-c-api-and-extensions.md`](17-c-api-and-extensions.md) measures the release
cost. Not repeated here. The two facts to carry into this document:

- The release/reacquire is not free — it is a lock handoff, and there is a scheduling
  latency to getting the GIL back after your `read` returns. On a busy interpreter this
  turns into the convoy effect that doc 24 §7 measures. **This is a real reason a threaded
  I/O server has worse tail latency than an async one at the same throughput**, quite
  separate from thread stack memory.
- Crucially, `Py_BEGIN_ALLOW_THREADS` **only helps if the call actually blocks in the
  kernel.** Non-blocking `recv` returning `EAGAIN` releases and reacquires the GIL for
  nothing. This is one of the small structural taxes an event loop written in Python pays,
  and one of the things uvloop avoids by staying in C across the whole loop iteration (doc
  28 §21).

### 15.4 What the stdlib gives you from this document

| this document | Python |
|---|---|
| `O_NONBLOCK` | `os.set_blocking(fd, False)`, `sock.setblocking(False)` |
| `EAGAIN` | `BlockingIOError` (a subclass of `OSError`) |
| `select`/`poll`/`kqueue`/`epoll` | `select` module, `selectors` module (portable façade) |
| `EPOLLET` / `EV_CLEAR` | `select.KQ_EV_CLEAR`, `select.EPOLLET` — **not** exposed through `selectors` |
| `fsync` | `os.fsync`; `os.fdatasync` **where available** (not macOS) |
| `F_FULLFSYNC` | `fcntl.fcntl(fd, fcntl.F_FULLFSYNC)` |
| `sendfile` | `os.sendfile`; `socket.sendfile` (with a `send` fallback); `shutil.copyfile` uses `os.sendfile`/`copy_file_range` where it can |
| `readv`/`writev` | `os.readv`, `os.writev`, `os.preadv`, `os.pwritev` |
| `posix_fadvise` | `os.posix_fadvise` (Linux) |
| `splice` / `MSG_ZEROCOPY` / `O_DIRECT` / `io_uring` | **nothing** — `os.O_DIRECT` exists as a flag on Linux, but the alignment work is yours |

The `selectors` module deliberately exposes only `EVENT_READ`/`EVENT_WRITE` and
level-triggered semantics — §7.4's argument, expressed as an API boundary.

---

## 16. The syscall bill of one request

Put the whole document together on one request. This is the model to reason with when
someone asks where the time goes.

```
   HTTP request arriving at a Python async server, level-triggered readiness:

   ┌─ event loop ──────────────────────────────────────────────────────────┐
   │  epoll_wait/kevent          ← 1 trap, amortized over ready fds        │
   │  accept4()                  ← 1 trap  (new connections only)          │
   │  epoll_ctl(ADD)             ← 1 trap  (new connections only)          │
   │  recv()                     ← 1 trap  (× however many for the headers)│
   │  ── your handler runs ──                                              │
   │     open() + read() a file  ← 2 traps, and a THREAD POOL HANDOFF      │
   │                               because regular files are always        │
   │                               "ready" (§4.1) — 2 context switches     │
   │     send() the response     ← 1 trap                                  │
   │     [short write?]          ← + epoll_ctl(MOD) + epoll_wait + send    │
   │                                 + epoll_ctl(MOD)                      │
   │  close()                    ← 1 trap                                  │
   └───────────────────────────────────────────────────────────────────────┘

   MEASURED FLOOR for a warm keep-alive echo round trip on this machine:
       8.01 syscalls per round trip  =  4 kevent + 2 recv + 2 send
       i.e. 4 syscalls per one-way message.        (§9.1)

   At ~81 ns of pure transition each (§2), that floor is ~0.6 µs of trap per
   round trip — against a measured 57.75 µs of actual round trip. So on THIS
   workload the traps are ~1% and the interpreter is the other 99%.
```

**That last line is the point, and it is the antidote to cargo-culting this document.** On
a Python server at these message sizes, syscall overhead is not your problem;
[`20-eval-loop.md`](20-eval-loop.md) is. The syscall count starts to matter when (a) you are
in a compiled language, (b) your payloads are small and your rate is enormous, or (c) you
have accidentally made it 10× larger than the floor — by reading 64 bytes at a time (§14.1),
by flushing every log line, by calling `stat()` per file, or by making one round trip per
row. **The realistic win in Python is almost never "shave a syscall"; it is "stop making
100,000 of them."**

---

## 17. A review checklist

Things worth catching in a code review, each traceable to a section above.

**Durability**
- [ ] `fsync` on the file **and** on the containing directory after a rename (§11.1).
- [ ] `fsync` errors are fatal, not retried (§11.3). `except OSError: retry` around an
      `fsync` is a data-loss bug.
- [ ] If the claim is "survives power loss" on macOS, `F_FULLFSYNC` — and the design
      budgets for ~340 commits/s, not thousands (§11.2).
- [ ] Nobody assumes `write()` returning success means anything durable (§10.3).

**Event loops**
- [ ] Edge-triggered handlers drain to `EAGAIN`, and the fd is non-blocking (§7.1).
- [ ] Level-triggered write registration is added on `EAGAIN` and removed on drain (§7.2).
- [ ] No `select()` anywhere that can exceed 1024 fds (§6).
- [ ] No blocking call inside a coroutine — see
      [`29-async-patterns-and-pitfalls.md`](29-async-patterns-and-pitfalls.md) §9 for
      detection.
- [ ] fds are not `dup()`ed or leaked into children while registered (§5.3).

**Syscall count**
- [ ] Buffer sizes are 64 KiB–256 KiB for bulk I/O, not 4 KiB and not 4 MiB (§14.1).
- [ ] `buffering=` is meaningfully larger than the read size, or it does nothing (§15.2).
- [ ] Scatter/gather (`writev`, `sendmsg`) instead of concatenating then writing (§14.2).
- [ ] `os.scandir` not `os.listdir` + `os.stat` (§14.3).
- [ ] Log flushing is not per-line in a hot path.

**Before reaching for the exotic**
- [ ] There is a profile showing syscalls are the bottleneck (§16).
- [ ] `sendfile` before hand-rolled zero-copy (§12.1).
- [ ] `O_DIRECT` only with a real buffer pool, real alignment handling, and a separate
      durability story (§13).
- [ ] `io_uring` only with a batch depth above 1, a plan for buffer lifetime, and
      confirmation that the deployment platform permits it (§8.6, §9.3).

---

## 18. What I could not verify

Stated explicitly, in the style this folder requires.

1. **Everything Linux-specific in §3, §5, §7 (the `epoll` spellings), §8, §9.2, §10.2,
   §12.2, §12.3 and §13 is researched, not measured.** No `epoll`, `io_uring`, `splice`,
   `MSG_ZEROCOPY`, `O_DIRECT`, `posix_fadvise` or vDSO call was executed in this session.
   The sources are the man-pages project, the kernel's `Documentation/`, and LWN, and each
   claim is attributed inline. **No number in this document is presented as measured unless
   its table caption says "measured on this machine".**

2. **The `io_uring` performance claims are architectural, not empirical.** I describe what
   the interface removes (syscalls, pinning, fd lookups, IPIs) with citations. I do not
   quote a throughput figure for it, because I have not run one and I am not willing to
   repeat a benchmark I cannot inspect.

3. **Google's 2023 disabling of `io_uring`** across ChromeOS, Android, and production
   servers is widely reported, and I referenced it in §8.6 — but **the primary source
   returned HTTP 404 when I fetched it in this session.** The surrounding argument rests on
   the LWN security coverage and the restriction machinery documented in the man pages,
   both of which I did retrieve; the Google specifics should be re-checked before you repeat
   them.

4. **The Spectre/Meltdown syscall-cost regression (§1.2) has no number attached, on
   purpose.** The mechanisms (KPTI's `CR3` writes, retpolines, IBPB/STIBP) are from the
   kernel's own documentation. The magnitude is heavily dependent on CPU generation,
   microcode, PCID availability, and which mitigations are enabled — and this ARM machine
   runs none of them. Any specific percentage I gave you would be invented.

5. **`F_NOCACHE` produced a null result (§10.4)** and I could not obtain a repeatable cold
   read. §10.1's cold numbers are **single-shot per file** — genuinely cold, but with n=1
   each. The consistency across four independent files (15.9×, 16.4×, 16.7×, 17.1×) is what
   makes me confident in the ~16× figure; no individual row is a median. I did not purge the
   system page cache, by instruction and by preference.

6. **`F_BARRIERFSYNC`'s spread was 161–504 µs**, a 3× range, against `F_FULLFSYNC`'s tight
   2865–3006 µs. I report the median (298.87 µs) but I do not trust the third significant
   figure. The qualitative finding — barrier is roughly an order of magnitude cheaper than
   a full flush and an order of magnitude dearer than plain `fsync` — is solid; the exact
   value is not.

7. **The `select` vs `poll` per-fd comparison (§6) is partly a CPython artifact.**
   CPython's poll object rebuilds its `pollfd` array from a dict on each call, so some of
   the 148 ns/fd is interpreter marshalling, not kernel scanning. The conclusion (both are
   O(n); the ready-list design wins by 3 orders of magnitude) is unaffected, but do not
   quote "poll is 3.6× worse than select per fd" as a kernel property. I did not write the C
   version that would separate them.

8. **The asyncio syscall count (§9.1) is loopback TCP with 32-byte messages on an idle
   connection.** Real traffic with backpressure, partial writes, and TLS will produce a
   different and larger bill. The 8.01 figure is a **floor**, and the zero in the `modify`
   row is specifically the artifact of writes never going short.

9. **`os.sendfile`'s 5.2× (§12.1) was measured over an AF_UNIX socketpair, not a NIC.** The
   loopback path has no DMA, so this measures the saved copies and syscalls but not the
   full zero-copy-to-hardware story. Over a real network interface the ratio would differ,
   probably favourably, and I did not measure it.

10. **Machine noise.** `load1` ran 1.9–2.4 throughout. Every timing here is a median of
    alternating passes for exactly that reason —
    [`30-concurrency-correctness.md`](30-concurrency-correctness.md) §9 documents a case
    where a single run on this machine produced a completely false result. The spread
    columns are reported so you can see where I am on thin ice: the two clock-read rows in
    §2 have 26–27% spread and should be read as "roughly 30 ns", not as three significant
    figures.

11. **I did not verify `io.DEFAULT_BUFFER_SIZE`'s history.** I verified it is **131072** on
    this 3.14.6 build. The claim that it was 8192 for most of Python's history is from
    memory, not from a bisect of the CPython source.

---

## 19. Lab exercises

Reading this leaves you at rung 3 of the ladder in `README.md`. These are what move you.

1. **Find your machine's trap floor.** Reproduce §2: time `os.getppid()`, `os.getuid()`,
   `os.getegid()` and an empty loop, medians of 7 alternating passes. Then time
   `os.getpid()` and explain the 8× discrepancy *without looking at §1*. Then disassemble
   your libc's `getpid` and confirm.

2. **Draw the C10K curve on your own hardware.** N socketpairs, one ready, sweep N from 10
   to 10,000, plot `select`/`poll`/`epoll` or `kqueue`. You must reproduce three things:
   the flat line, the two sloped lines, and the exact N at which `select` raises. Then
   compute the marginal ns/fd for each.

3. **Break an edge-triggered handler and diagnose it as a stranger would.** Write a small
   echo server with `EPOLLET`/`EV_CLEAR` that reads once per event. Drive it with 10-byte
   and 10,000-byte messages. Observe that small messages work perfectly and large ones hang
   forever. *Now write the incident report* — you have just produced the hardest kind of
   production bug from first principles.

4. **Measure your durability ceiling.** Reproduce §11.2 on your own storage: `write`,
   `fsync`, and the strongest flush your platform offers. Then answer: how many durable
   commits per second can a single-threaded service on this disk perform, and does your
   current design assume more?

5. **Reproduce fsync-gate in miniature.** On Linux, back a filesystem with a loop device or
   `dm-error`, force a writeback error mid-write, and observe: the first `fsync` fails, the
   second **succeeds**, and the data is gone. This is the single most valuable hour in this
   document. (If you cannot arrange the error injection, read the LWN article and the
   pgsql-hackers thread instead and write down the six-step sequence from §11.3 from
   memory.)

6. **Find the buffer-size sweet spot on your storage.** Sweep read sizes from 64 B to 8 MiB
   on a warm file and plot MiB/s. Locate both the syscall-bound region and the
   cache-bound regression at the top. Explain the peak's position in terms of your L2 size.

7. **Count the syscalls in something you own.** `strace -c -f` (Linux) or `dtruss` (macOS)
   a real service under a realistic minute of load. Sort by count. Then answer, for the top
   three: what would eliminate 90% of them? This exercise has never once failed to find
   something embarrassing.

8. **Build the writev cost model.** Reproduce §14.2, fit `cost = a + b·K`, and report your
   machine's `a` and `b`. The ratio `a/b` is the batching leverage available to you, and it
   is a number worth knowing about your production hardware.

9. **Write an `io_uring` echo server** (C or Rust, or Python via a `liburing` binding).
   Then add `IORING_SETUP_SQPOLL` and confirm with `strace` that the steady state makes
   **zero** syscalls. Nothing else in systems programming makes the completion model click
   as fast as watching an empty `strace` output while the server serves traffic.

10. **Prove the page cache to yourself.** Take a file you have never read, time the read,
    time it again. Then `posix_fadvise(POSIX_FADV_DONTNEED)` it and time a third read.
    Reproduce §10.1's ratio and note how much of your "storage performance" intuition was
    actually memory bandwidth.

---

## 20. Question bank

Staff-level. Answers should include the mechanism *and* the boundary of your model.

1. Walk a `read()` from the `svc`/`syscall` instruction to the data landing in your buffer.
   Name three things that are colder when you return than when you left.
2. Why is `getpid()` a bad benchmark for syscall cost on most platforms? What would you use
   instead?
3. What is the vDSO, which calls live in it, and why? Give a scenario where a vDSO call
   silently becomes a real syscall and your latency regresses with no code change.
4. `select` vs `poll` vs `epoll`: give the complexity of each per wait, and say precisely
   what structural change makes `epoll` different — not "it's faster."
5. **Level- vs edge-triggered `epoll`: what bug does each invite?** Give the production
   signature of the edge-triggered one and the two-part contract from `epoll(7)` that
   prevents it.
6. Why can't `epoll` be used for regular file I/O, and what does every readiness-based
   async runtime do about it?
7. **What does `io_uring` change about the cost model of an async server relative to
   `epoll`?** Answer in four axes, then give three situations where it would not help.
8. What does `IORING_SETUP_SQPOLL` do, what does it cost, and when is that trade wrong?
9. A colleague proposes `io_uring` for a service doing 200 requests/second with 5 ms
   handlers. Argue against it on cost-model grounds, not on "it's complicated."
10. Your `write()` returned success and the machine lost power. Is your data there? Walk
    every layer between the call and the platter and say what each one guarantees.
11. `fsync()` returns `EIO`. What do you do, and why is retrying wrong? What did Postgres
    change, and in which release?
12. Why must you `fsync` the directory after an atomic rename?
13. Explain the double-caching problem and why a database might want `O_DIRECT`. Then give
    three things you must now implement yourself.
14. `sendfile` vs `splice` vs `MSG_ZEROCOPY`: which applies to a static file server, a TCP
    proxy, and a service serializing responses from its own heap? Why can `MSG_ZEROCOPY`
    be *slower*, and how does the kernel tell you it was?
15. Reading a 1 GiB file 64 bytes at a time is 139× slower than reading it 256 KiB at a
    time on this hardware. Account for the whole factor. Then explain why 4 MiB is slower
    than 256 KiB.
16. You set `buffering=4096` and read in 4096-byte chunks. How many syscalls do you make,
    and why?
17. A service's p99 tripled after a deploy and recovered over ~10 minutes, with no change
    to the hot path. Give three hypotheses from this document and how you'd distinguish them.
18. Given a syscall floor of ~80 ns and a measured 8 syscalls per round trip, how much of a
    57 µs Python round trip is kernel transition? What does that tell you about where to
    optimize, and what would have to change for the answer to flip?

---

## 21. Sources

**Primary — the man pages (these are the specification; read them, not blog summaries)**
- [`epoll(7)`](https://man7.org/linux/man-pages/man7/epoll.7.html) — the interest list / ready
  list model, `EPOLLET`, `EPOLLONESHOT`, `EPOLLEXCLUSIVE`, and the Q&A section at the end.
  *Verdict: the single most valuable page in this list. The "suggested way to use epoll as
  an edge-triggered interface" paragraph is §7's entire answer, in five lines.*
- [`select(2)`](https://man7.org/linux/man-pages/man2/select.2.html) — *Verdict: read the BUGS
  section first. "This limitation will not change" and "a design error that is avoided in
  poll(2) and epoll(7)" are unusually direct for a man page, and both are load-bearing.*
- [`poll(2)`](https://man7.org/linux/man-pages/man2/poll.2.html) — *Verdict: short. Read it to
  see exactly which of `select`'s problems it fixes and which it doesn't.*
- [`io_uring(7)`](https://man7.org/linux/man-pages/man7/io_uring.7.html) — the ring model, the
  submission/completion walkthrough, SQ polling, and a complete worked example.
  *Verdict: start here for §8; the example program is the fastest way to understand the
  three `mmap` calls.*
- [`io_uring_setup(2)`](https://man7.org/linux/man-pages/man2/io_uring_setup.2.html) — every
  `IORING_SETUP_*` flag and `IORING_FEAT_*` capability with its kernel version.
  *Verdict: the definitive table for "what does my kernel actually support". §8.5 is
  essentially a reading of this page.*
- [`io_uring_enter(2)`](https://man7.org/linux/man-pages/man2/io_uring_enter.2.html) — the
  opcode catalogue and the CQE error semantics. *Verdict: enormous; grep it, don't read it.
  The opcode list is the honest measure of how much of the kernel io_uring now reaches.*
- [`vdso(7)`](https://man7.org/linux/man-pages/man7/vdso.7.html) — *Verdict: the "Example
  background" section explains why `int $0x80` was abandoned better than any textbook, and
  the per-architecture symbol tables settle arguments.*
- [`fsync(2)`](https://man7.org/linux/man-pages/man2/fsync.2.html) — *Verdict: four paragraphs,
  and the directory-fsync sentence in it is the one most production code violates.*
- [`open(2)`](https://man7.org/linux/man-pages/man2/open.2.html) — the `O_DIRECT` section and
  its caveats. *Verdict: read the O_DIRECT notes in full before ever using it; the alignment
  and coherence rules are not intuitive, and the Torvalds quote is in there for a reason.*
- [`sendfile(2)`](https://man7.org/linux/man-pages/man2/sendfile.2.html) and
  [`splice(2)`](https://man7.org/linux/man-pages/man2/splice.2.html) — *Verdict: read them
  together; `sendfile`'s NOTES section tells you when to reach for `splice` instead.*
- [`posix_fadvise(2)`](https://man7.org/linux/man-pages/man2/posix_fadvise.2.html) and
  [`readahead(2)`](https://man7.org/linux/man-pages/man2/readahead.2.html) — *Verdict: the
  `POSIX_FADV_NOREUSE` history (a no-op from 2.6.18 to 6.2) is a good lesson in checking
  versions rather than headers.*

**Kernel documentation**
- [`Documentation/networking/msg_zerocopy.rst`](https://www.kernel.org/doc/html/latest/networking/msg_zerocopy.html)
  — *Verdict: the most honest performance documentation in the kernel tree. It tells you the
  optimization may not happen, may be slower, and gives you the flag to detect it. Read it
  as a model for how to document a performance feature.*
- [`Documentation/admin-guide/hw-vuln/spectre.rst`](https://www.kernel.org/doc/html/latest/admin-guide/hw-vuln/spectre.html)
  — *Verdict: for §1.2, read the "Turning on mitigation" and "Mitigation selection guide"
  sections. It is explicit that the high-security settings cost performance, which is more
  than most vendors will tell you.*

**LWN — the historical record**
- Jonathan Corbet, [*Ringing in a new asynchronous I/O API*](https://lwn.net/Articles/776703/)
  (15 Jan 2019). *Verdict: io_uring's introduction, including the specific complaints about
  Linux AIO that motivated it. Read it before the man pages — it explains the "why" the man
  pages assume.*
- Jonathan Corbet, [*The rapid growth of io_uring*](https://lwn.net/Articles/810414/)
  (24 Jan 2020). *Verdict: one year on, and the source of §8.1's framing that classic UNIX
  I/O is inherently synchronous. Also covers registered buffers and files.*
- Jonathan Corbet, [*Security requirements for new kernel features*](https://lwn.net/Articles/902466/)
  (28 Jul 2022). *Verdict: the primary source for §8.6. The `uring_cmd`/LSM-hook gap is the
  clearest single example of io_uring's structural security problem — a second path to the
  same operation without the same mediation.*
- Jonathan Corbet, [*PostgreSQL's fsync() surprise*](https://lwn.net/Articles/752063/)
  (18 Apr 2018). *Verdict: **read this one in full.** It is the best-written account of a
  data-loss bug caused by a misunderstood interface contract, and every paragraph is
  transferable to systems you work on.*

**PostgreSQL**
- [PostgreSQL wiki: Fsync Errors](https://wiki.postgresql.org/wiki/Fsync_Errors) — *Verdict:
  the "Current status" section is the resolution (PANIC on fsync failure, PG12, backpatched
  to 9.4) and it links the specific Linux 4.13 `errseq_t` commits. The authoritative
  follow-up to the LWN piece, maintained by the people who lived it.*
- Jens Axboe, [liburing](https://github.com/axboe/liburing) — *Verdict: the man pages in
  `man/` are the up-to-date io_uring documentation; the kernel's own are older. If a claim
  about io_uring conflicts, liburing wins.*

**Books (see [BOOKS.md](BOOKS.md) for the roadmap's full verdicts)**
- Michael Kerrisk, *The Linux Programming Interface* — ch. 13 (file I/O buffering), 14
  (filesystems), 63 (alternative I/O models: `select`, `poll`, signal-driven I/O, `epoll`),
  61 (`sendfile`). *Verdict: **ch. 63 is the canonical treatment of §4–§7** and is worth
  reading straight through, which is unusual advice for this book. It predates io_uring, so
  §8 has no TLPI chapter — that gap is what the LWN articles and liburing's man pages fill.*
- Brendan Gregg, *Systems Performance*, 2e (2020) — ch. 8 (file systems: page cache, write-back,
  the I/O stack diagram), ch. 9 (disks), ch. 10 (network). *Verdict: **ch. 8 is §10.** Gregg's
  layered I/O-stack diagrams are the mental model this document's §10 diagram is a simplified
  version of, and the methodology chapters tell you how to find out which layer is hurting
  you rather than guessing.*
- Arpaci-Dusseau, *OSTEP* (free) — the Persistence section, especially the chapters on
  file-system implementation, journaling, and crash consistency. *Verdict: read the crash
  consistency chapter alongside §11. It gives you the vocabulary (ordering, atomicity,
  journaling modes) that makes the fsync-gate story legible rather than merely alarming.*
- W. Richard Stevens & Stephen Rago, *Advanced Programming in the UNIX Environment*, 3e —
  ch. 3 and 14. *Verdict: the portable-UNIX view, which is where the `select`/`poll`
  distinction and the buffered-vs-unbuffered discussion originally come from. Reference, not
  a read.*

**Siblings in this folder**
- [`28-asyncio-internals.md`](28-asyncio-internals.md) §9–§10 — the selector, the self-pipe,
  and the full `await` → `kevent` path. *Verdict: read §10's diagram immediately after §9 of
  this document; together they are the complete path from `await` to the trap.*
- [`24-the-gil.md`](24-the-gil.md) §5–§7 — what releases the GIL, and the convoy effect.
- [`29-async-patterns-and-pitfalls.md`](29-async-patterns-and-pitfalls.md) §9–§10 — detecting
  a blocked event loop, which is the operational counterpart to §4.1's "regular files are
  always ready".
- [`30-concurrency-correctness.md`](30-concurrency-correctness.md) §14 — clock costs, and the
  methodological warning behind every median in this document.
- [`01-memory-hierarchy-and-caches.md`](01-memory-hierarchy-and-caches.md) — why §14.1's
  curve turns back up past 256 KiB.
- [`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md) — the acquire/release
  discipline that makes §8.2's shared rings safe across the privilege boundary.

---

*Next: [`10-signals-fork-exec.md`](10-signals-fork-exec.md) — the other way the kernel
interrupts your process. Signal delivery and async-signal-safety, `fork()` in a threaded
program and the deadlock it invites, `exec`, and FD inheritance and `CLOEXEC` — which is the
direct continuation of §5.3's "closing an fd does not remove it from the interest list if
something else still holds the open file description."*
