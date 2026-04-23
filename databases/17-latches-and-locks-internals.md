# Database Latches & Locks Internals: Why Databases Build Their Own

A staff-engineer-level deep dive into why database engineers reject OS-level synchronization primitives and build custom latching infrastructure instead. Covers the full spectrum from transistor-level bus locks to high-level lock managers: user space vs kernel space lock boundaries, futex internals and futex2 evolution, Intel vs AMD hardware lock semantics (LOCK# prefix, store buffers, cache coherence protocol differences), page-level and memory-level locking mechanics, why `pthread_mutex` is too expensive for databases, custom latch designs (spinlocks, queued locks, reader-writer latches, optimistic latches), lock-free and wait-free techniques, epoch-based reclamation, modern approaches in production databases, and language-specific synchronization semantics across C/C++, Rust, Java, and Go.

---

## Table of Contents

1. [The Two Worlds: Latches vs Locks](#1-the-two-worlds-latches-vs-locks)
2. [How OS-Level Locks Actually Work](#2-how-os-level-locks-actually-work)
3. [User Space vs Kernel Space: The Lock Boundary](#3-user-space-vs-kernel-space-the-lock-boundary)
4. [Why Database Engineers Reject OS Locks](#4-why-database-engineers-reject-os-locks)
5. [Hardware Foundations: Atomics and Memory Ordering](#5-hardware-foundations-atomics-and-memory-ordering)
6. [Intel vs AMD: Hardware Lock Semantics Deep Dive](#6-intel-vs-amd-hardware-lock-semantics-deep-dive)
7. [Page-Level and Memory-Level Locking Mechanics](#7-page-level-and-memory-level-locking-mechanics)
8. [Database Latch Designs](#8-database-latch-designs)
9. [Reader-Writer Latches](#9-reader-writer-latches)
10. [Optimistic Latching and OLC](#10-optimistic-latching-and-olc)
11. [Lock-Free and Wait-Free Data Structures](#11-lock-free-and-wait-free-data-structures)
12. [Epoch-Based Reclamation](#12-epoch-based-reclamation)
13. [Latch Implementations in Production Databases](#13-latch-implementations-in-production-databases)
14. [Modern Approaches and Research](#14-modern-approaches-and-research)
15. [Language-Specific Synchronization Semantics](#15-language-specific-synchronization-semantics)
16. [Diagnosing Latch Contention in Production](#16-diagnosing-latch-contention-in-production)
17. [Comparative Summary](#17-comparative-summary)

---

## 1. The Two Worlds: Latches vs Locks

Database systems have two completely separate synchronization layers that unfortunately share the same English word "lock." This causes endless confusion. These are fundamentally different mechanisms solving fundamentally different problems.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    DATABASE SYNCHRONIZATION STACK                       │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  LAYER 1: LOGICAL LOCKS  (what users see)                              │
│  ─────────────────────────────────────────                              │
│  • Protect logical database objects: rows, tables, key ranges           │
│  • Held for the duration of a transaction (ms to seconds)               │
│  • Managed by a lock manager with wait queues and deadlock detection    │
│  • Logged in WAL, re-acquired during crash recovery                     │
│  • Modes: S, X, IS, IX, SIX, key-range, predicate                     │
│  • Visible: pg_locks, SHOW ENGINE INNODB STATUS, sys.dm_tran_locks     │
│                                                                         │
│  LAYER 2: PHYSICAL LATCHES  (what engineers build)                     │
│  ─────────────────────────────────────────────────                      │
│  • Protect in-memory data structures: B-tree nodes, buffer frames,      │
│    hash buckets, WAL buffers, catalog caches                            │
│  • Held for nanoseconds to microseconds                                 │
│  • NO deadlock detection (prevented by coding discipline)               │
│  • NOT logged, NOT recovered after crash                                │
│  • Modes: usually just shared/exclusive (sometimes exclusive-only)      │
│  • Invisible to users; visible only via perf, wait events, dtrace       │
│                                                                         │
│  LAYER 0: HARDWARE ATOMICS  (what CPUs provide)                        │
│  ──────────────────────────────────────────────                         │
│  • CAS (compare-and-swap), XCHG, FAA (fetch-and-add)                  │
│  • Memory fences / barriers                                             │
│  • Cache coherence protocols (MESI/MOESI)                               │
│  • This is the raw material from which latches are built                │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**The key question this document answers:** Why don't databases just use `pthread_mutex_t` or `std::mutex` for Layer 2? Why do they build custom latches from raw atomics?

---

## 2. How OS-Level Locks Actually Work

To understand why databases avoid OS locks, you must first understand exactly what happens when you call `pthread_mutex_lock()`.

### 2.1 The Anatomy of pthread_mutex

`pthread_mutex` is typically implemented as a **futex** (fast userspace mutex) on Linux. The critical insight: it has both a fast path (userspace-only) and a slow path (kernel involvement).

```
pthread_mutex_lock(mutex):

  FAST PATH (uncontended -- ~25 ns on modern x86):
  ─────────────────────────────────────────────────
  1. Atomically try CAS(mutex->__lock, 0, 1)      // attempt to set 0→1
  2. If CAS succeeds:
     - We own the mutex, return immediately
     - NEVER entered the kernel
     - Cost: single atomic instruction + memory fence ≈ 5-25 ns

  SLOW PATH (contended -- 1-10 μs minimum):
  ──────────────────────────────────────────
  3. If CAS fails (someone else holds the lock):
     a. Optional: spin for a limited number of iterations
        (glibc adaptive spinning: ~100 iterations on multi-core)
     b. If still contended after spinning:
        - Set mutex->__lock = 2 (indicates waiters exist)
        - SYSCALL: futex(FUTEX_WAIT, &mutex->__lock, 2)
          * Context switch to kernel mode (~1-5 μs)
          * Kernel adds thread to a wait queue keyed by &mutex->__lock
          * Thread is descheduled (removed from run queue)
          * Thread sleeps until woken

pthread_mutex_unlock(mutex):
  1. old = atomic_exchange(mutex->__lock, 0)
  2. If old == 2 (there were waiters):
     - SYSCALL: futex(FUTEX_WAKE, &mutex->__lock, 1)
       * Kernel wakes one thread from the wait queue
       * Context switch overhead for the woken thread

Cost breakdown:
  Uncontended lock + unlock:   ~25 ns  (pure userspace, just atomics)
  Contended, won after spin:   ~100-500 ns
  Contended, went to sleep:    ~1-10 μs  (two context switches)
  Contended, NUMA cross-node:  ~5-50 μs  (cache line bouncing + ctx switch)
```

### 2.2 Futex Internals (Linux)

The futex ("fast userspace mutex") is the kernel primitive underneath most userspace synchronization on Linux.

```
Futex Architecture:

  Userspace:
  ┌─────────────────────────────┐
  │  int futex_word = 0;        │  Shared memory word
  │                             │  that userspace and
  │  // Uncontended: just CAS   │  kernel both look at
  │  // Contended: syscall      │
  └─────────────┬───────────────┘
                │ futex(FUTEX_WAIT/WAKE)
                ▼
  Kernel:
  ┌─────────────────────────────────────────────────┐
  │  Futex Hash Table (global, 256 buckets default) │
  │                                                  │
  │  bucket = hash(&futex_word) % num_buckets        │
  │                                                  │
  │  Each bucket:                                    │
  │  ┌───────────┐    ┌───────────┐                  │
  │  │ wait_queue │───►│ wait_queue │───► ...          │
  │  │ thread: T1 │    │ thread: T2 │                  │
  │  │ key: addr1 │    │ key: addr1 │                  │
  │  └───────────┘    └───────────┘                  │
  │                                                  │
  │  Bucket is protected by a spinlock               │
  │  → Potential contention point under high load!   │
  └─────────────────────────────────────────────────┘

FUTEX_WAIT(&futex_word, expected_value):
  1. Kernel acquires bucket spinlock
  2. Re-checks: if *futex_word != expected_value → return EAGAIN
     (avoids lost-wakeup race)
  3. Adds current thread to wait queue
  4. Releases bucket spinlock
  5. Deschedules thread → sleeps

FUTEX_WAKE(&futex_word, num_to_wake):
  1. Kernel acquires bucket spinlock
  2. Walks wait queue, wakes up to num_to_wake threads
  3. Releases bucket spinlock
```

### 2.3 Windows: CRITICAL_SECTION and SRWLock

```
Windows CRITICAL_SECTION:
  ┌──────────────────────────────────────────────────────┐
  │  Fast path: InterlockedCompareExchange (CAS)         │
  │  Spin: SpinCount iterations (default 0, set to 4000  │
  │         for heap locks)                               │
  │  Slow path: WaitForSingleObject → kernel transition  │
  │                                                       │
  │  Extra overhead: debugging support, ownership tracking│
  │  sizeof(CRITICAL_SECTION) = 40 bytes on x64          │
  └──────────────────────────────────────────────────────┘

Windows SRWLock (Slim Reader-Writer Lock):
  ┌──────────────────────────────────────────────────────┐
  │  Extremely compact: sizeof(SRWLOCK) = 8 bytes (!)    │
  │  No spin count, no recursion, no thread ownership     │
  │  Fast path: single interlocked operation              │
  │  Slow path: keyed event (kernel, like futex)          │
  │                                                       │
  │  Used by SQL Server internally (starting ~2016)       │
  │  Advantage: small size = many can fit in cache line   │
  └──────────────────────────────────────────────────────┘
```

---

## 3. User Space vs Kernel Space: The Lock Boundary

Understanding where locks live — user space, kernel space, or straddling both — is foundational. Every synchronization mechanism occupies a specific position in this hierarchy, and the boundary crossings are where the real costs hide.

### 3.1 The Address Space Divide

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    VIRTUAL ADDRESS SPACE (per process)                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  0xFFFFFFFFFFFFFFFF  ┌──────────────────────────────────────────────────┐   │
│                      │  KERNEL SPACE (upper half on x86-64)             │   │
│                      │                                                   │   │
│                      │  • Kernel spinlocks (spin_lock_t)                │   │
│                      │  • Kernel mutexes (struct mutex)                  │   │
│                      │  • Kernel semaphores (struct semaphore)           │   │
│                      │  • RW semaphores (struct rw_semaphore)            │   │
│                      │  • RCU (read-copy-update)                         │   │
│                      │  • Seqlocks (struct seqlock_t)                    │   │
│                      │  • Futex wait queues (kernel-side)                │   │
│                      │  • Page table locks (PTE spinlocks)               │   │
│                      │  • Scheduler locks (rq->lock)                     │   │
│                      │                                                   │   │
│                      │  Access: Ring 0 only. Any access from Ring 3      │   │
│                      │  causes a General Protection Fault (#GP).         │   │
│                      │                                                   │   │
│  0xFFFF800000000000  ├──────────────────────────────────────────────────┤   │
│                      │  NON-CANONICAL HOLE                               │   │
│                      │  (addresses here fault immediately — the x86-64   │   │
│                      │   canonical address gap)                          │   │
│  0x00007FFFFFFFFFFF  ├──────────────────────────────────────────────────┤   │
│                      │  USER SPACE (lower half)                          │   │
│                      │                                                   │   │
│                      │  • pthread_mutex_t (futex word lives here)       │   │
│                      │  • pthread_rwlock_t                               │   │
│                      │  • pthread_spinlock_t                              │   │
│                      │  • C++11 std::mutex, std::atomic<T>              │   │
│                      │  • Custom database latches (atomic words)         │   │
│                      │  • Lock-free data structure pointers              │   │
│                      │  • mmap'd shared memory locks (shared futexes)   │   │
│                      │                                                   │   │
│                      │  Access: Ring 3 (user mode).                      │   │
│                      │  All database latch operations live here.         │   │
│  0x0000000000000000  └──────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Lock Categories by Where They Execute

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  CATEGORY 1: PURE USERSPACE LOCKS                                            │
│  ─────────────────────────────────                                           │
│  Execute entirely in user space. NEVER enter the kernel.                     │
│                                                                              │
│  Examples:                                                                   │
│  • Pure spinlocks (TAS, TTAS, ticket lock)                                  │
│  • MCS / CLH queue locks (when spinning, not sleeping)                      │
│  • Optimistic version checks (OLC)                                          │
│  • Lock-free CAS loops                                                       │
│  • pthread_spinlock_t (PTHREAD_PROCESS_PRIVATE)                              │
│                                                                              │
│  Hardware used: atomic instructions (LOCK CMPXCHG, LOCK XCHG, LOCK XADD)   │
│  Cost: 5-500 ns (spinning cost, never syscall cost)                          │
│  When they hurt: CPU is burned while spinning. Bad if critical section is    │
│  long or thread holding the lock is descheduled.                             │
│                                                                              │
│  Database use: HOT PATH latches. Buffer page content latches, B-tree node   │
│  latches, WAL insertion latches. These are the latches that databases build  │
│  custom because they need absolute minimum overhead.                         │
├──────────────────────────────────────────────────────────────────────────────┤
│  CATEGORY 2: HYBRID USERSPACE/KERNEL LOCKS (futex-based)                    │
│  ────────────────────────────────────────────────────────                    │
│  Fast path in user space, slow path in kernel.                               │
│                                                                              │
│  Examples:                                                                   │
│  • pthread_mutex_t (PTHREAD_MUTEX_DEFAULT)                                   │
│  • pthread_rwlock_t                                                          │
│  • pthread_cond_t (condition variables)                                      │
│  • C++ std::mutex (glibc implementation)                                     │
│  • Go sync.Mutex (uses futex on Linux)                                       │
│  • Java synchronized / ReentrantLock (uses futex via Parker)                │
│  • Rust std::sync::Mutex (uses futex on Linux since Rust 1.62)              │
│  • Windows CRITICAL_SECTION, SRWLock (keyed events)                         │
│                                                                              │
│  Hardware used:                                                              │
│    Fast path: atomic CAS (user space only)                                   │
│    Slow path: syscall(futex) → kernel wait queue → scheduler interaction    │
│                                                                              │
│  Cost: 5-25 ns (uncontended) to 1-50 μs (contended + sleeping)             │
│                                                                              │
│  Database use: Connection management, metadata locks, background task        │
│  coordination. Operations that happen thousands of times/sec, not millions. │
├──────────────────────────────────────────────────────────────────────────────┤
│  CATEGORY 3: PURE KERNEL LOCKS                                               │
│  ─────────────────────────────                                               │
│  Execute entirely in kernel space. User space cannot use directly.           │
│                                                                              │
│  Examples:                                                                   │
│  • spin_lock_t (kernel spinlock)                                             │
│  • struct mutex (kernel sleeping mutex)                                      │
│  • struct rw_semaphore (kernel reader-writer semaphore)                      │
│  • struct seqlock_t (kernel sequence lock — like OLC!)                       │
│  • RCU (rcu_read_lock/rcu_read_unlock)                                      │
│  • PTE spinlocks (page table entry locks)                                    │
│  • rq->lock (scheduler run queue lock)                                       │
│  • inode->i_lock, mapping->i_mmap_rwsem (filesystem locks)                  │
│                                                                              │
│  Cost: 5-200 ns (within kernel)                                              │
│  User space sees: only the EFFECTS (slower syscalls when kernel locks are   │
│  contended). Cannot observe or control these directly.                       │
│                                                                              │
│  Database impact: page faults, mmap operations, file I/O all acquire kernel │
│  locks. High I/O concurrency → kernel lock contention → invisible stalls.   │
│  This is ONE MORE REASON databases use direct I/O and bypass the page cache.│
└──────────────────────────────────────────────────────────────────────────────┘
```

### 3.3 Anatomy of a Kernel Crossing (syscall path)

When a futex-based lock must sleep, here is what happens at the hardware and OS level:

```
USER SPACE                              KERNEL SPACE
──────────                              ────────────

Thread executing
pthread_mutex_lock():

1. CAS fails (contended)
2. Spin fails (still contended)
3. Need to sleep — call futex()

   ┌─────────────────────────┐
   │ SYSCALL instruction      │
   │ (SYSCALL on x86-64)      │
   │                          │
   │ What happens on x86-64:  │
   │                          │
   │ a. CPU loads kernel RIP  │
   │    from IA32_LSTAR MSR   │
   │ b. CPU loads kernel RSP  │──────────►  4. Entry: entry_SYSCALL_64
   │    from IA32_KERNEL_GS_  │             │
   │    BASE                  │             │  a. Save user registers on
   │ c. CPU swaps GS base     │             │     kernel stack (pt_regs)
   │    (SWAPGS instruction)  │             │  b. Look up syscall number
   │ d. CPU sets CPL=0        │             │     (RAX=202 = __NR_futex)
   │    (privilege Ring 3→0)  │             │  c. Call sys_futex()
   │ e. Saves user RIP→RCX   │             │
   │ f. Saves RFLAGS→R11     │             ▼
   │                          │          5. sys_futex(FUTEX_WAIT):
   │ Total: ~50-70 cycles     │             │
   │ (~15-25 ns on 3+ GHz)   │             │  a. Validate user pointer
   └─────────────────────────┘             │     (access_ok, get_user)
                                           │  b. Hash &futex_word to find
                                           │     hash bucket
                                           │  c. Acquire bucket spinlock
                                           │  d. Re-read *futex_word from
                                           │     user memory (might fault!)
                                           │  e. If *futex_word != expected:
                                           │     → release spinlock, return
                                           │       EAGAIN (spurious)
                                           │  f. Add current task to wait
                                           │     queue (struct futex_q)
                                           │  g. Release bucket spinlock
                                           │  h. Call schedule():
                                           │     → save full context
                                           │     → pick next task
                                           │     → switch to it
                                           │
                                           ▼
                                        6. Thread is NOW SLEEPING
                                           In task state:
                                           TASK_INTERRUPTIBLE
                                           Not on any CPU run queue.
                                           Will be woken by futex(FUTEX_WAKE).

Cost accounting:
  SYSCALL instruction itself:       ~20 ns  (ring transition)
  Register save/restore:            ~10 ns
  Futex hash + bucket lock:         ~20-50 ns
  User memory access from kernel:   ~10-30 ns (may TLB miss)
  Schedule + context switch:        ~1-5 μs (THE BIG COST)
    Includes: full register save, TLB flush (or ASID switch),
    cache cold effects on resume (~1-10 μs extra)
  ────────────────────────────────────────────────
  Total one-way:                    ~1.5-6 μs
  Round-trip (sleep + wake):        ~3-12 μs

The context switch is the killer. CPU caches go cold. On resume,
the first ~100 memory accesses will be cache misses (~5 ns each).
That is another ~500 ns of invisible "cache warmup" tax.
```

### 3.4 Futex Deep Dive: The Complete Architecture

```
FUTEX INTERNALS — THE FULL PICTURE

Futex (Fast Userspace muTEX) was added to Linux 2.5.7 (2002) by
Hubertus Franke, Matthew Kirkwood, Ingo Molnar, and Rusty Russell.
It is THE foundational syscall for all userspace synchronization
on Linux — every pthread lock, every Go mutex, every JVM monitor
ultimately calls futex().

FUTEX WORD:
  A single 32-bit integer in user-accessible memory (usually on the
  stack, heap, or in mmap'd shared memory). The kernel NEVER modifies
  this word — it only reads it to avoid races. Userspace is entirely
  responsible for updating the futex word with atomic operations.

  ┌──────────────────────────────────────────────────────────────────┐
  │  IMPORTANT: The kernel does NOT set the futex word.              │
  │  This is what makes futex "fast" — the uncontended path          │
  │  is 100% userspace atomic operations. The kernel only provides   │
  │  a wait queue indexed by the futex word's address.               │
  └──────────────────────────────────────────────────────────────────┘

FUTEX HASH TABLE (kernel side):

  ┌────────────────────────────────────────────────────────────────────────┐
  │  struct futex_hash_bucket {                                            │
  │      atomic_t waiters;            // number of waiters in this bucket  │
  │      spinlock_t lock;             // protects the chain                │
  │      struct plist_head chain;     // priority-sorted list of waiters   │
  │  };                                                                    │
  │                                                                        │
  │  // Global hash table (since Linux 2.6.x):                             │
  │  static struct futex_hash_bucket futex_queues[1 << futex_hashsize];   │
  │  // futex_hashsize = ilog2(256 * num_cpus) by default                  │
  │  // 8 CPUs → 2048 buckets; 64 CPUs → 16384 buckets                   │
  │                                                                        │
  │  Hash function: jhash2(&futex_key, ...) to select bucket               │
  │  Key includes: (page frame number, offset within page) for             │
  │  private futexes, or (inode, offset) for shared futexes                │
  └────────────────────────────────────────────────────────────────────────┘

FUTEX KEY TYPES (how the kernel identifies a futex):

  PRIVATE FUTEX (FUTEX_PRIVATE_FLAG, Linux 2.6.22+):
    Key = (current->mm, virtual address)
    Only threads in the SAME process can use this futex.
    Faster: no need to look up the backing page.
    This is what pthread_mutex uses by default.

  SHARED FUTEX:
    Key = (inode of backing file OR anonymous page, offset in page)
    Works across processes (shared memory, mmap'd files).
    Slower: kernel must call get_user_pages() to resolve the
    physical page, then extract inode + offset.
    Used for: cross-process semaphores (sem_open), databases
    using shared memory (PostgreSQL shared buffers!).

  Performance difference:
    Private futex WAIT/WAKE:  ~1.5-3 μs per operation
    Shared futex WAIT/WAKE:   ~3-8 μs per operation (2× slower)
    Reason: shared requires page table walk + inode lookup

FUTEX OPERATIONS (the full set):

  ┌─────────────────────────────┬──────────────────────────────────────────┐
  │  FUTEX_WAIT                  │  Sleep if *uaddr == val                  │
  │  FUTEX_WAKE                  │  Wake up to N waiters                    │
  │  FUTEX_WAIT_BITSET           │  Sleep with bitmask (selective wake)     │
  │  FUTEX_WAKE_BITSET           │  Wake waiters matching bitmask           │
  │  FUTEX_REQUEUE               │  Move waiters from one futex to another  │
  │  FUTEX_CMP_REQUEUE           │  Atomic compare + requeue               │
  │  FUTEX_WAKE_OP               │  Wake + atomic operation on another word │
  │  FUTEX_LOCK_PI               │  Priority-inheritance lock (RT)          │
  │  FUTEX_UNLOCK_PI             │  Priority-inheritance unlock             │
  │  FUTEX_TRYLOCK_PI            │  Non-blocking PI lock attempt            │
  │  FUTEX_WAIT_REQUEUE_PI       │  Wait + requeue to PI futex             │
  └─────────────────────────────┴──────────────────────────────────────────┘

  FUTEX_REQUEUE (critical for databases!):
    Scenario: pthread_cond_broadcast() needs to wake ALL waiters.
    Naive: FUTEX_WAKE(N) wakes all N threads. They all immediately
    try to acquire the mutex. N-1 fail and must re-sleep on the mutex's
    futex. That is N-1 futile wakeups ("thundering herd").

    FUTEX_CMP_REQUEUE: Wake 1 thread, MOVE remaining N-1 waiters from
    the condvar's futex queue to the mutex's futex queue WITHOUT waking
    them. The 1 woken thread acquires the mutex. When it releases, the
    kernel wakes the next from the mutex's queue. Perfect serialization,
    zero thundering herd.

    This is how glibc implements pthread_cond_broadcast() and
    pthread_cond_signal() since glibc 2.5.

  FUTEX_LOCK_PI (Priority Inheritance):
    For real-time systems. When a high-priority thread blocks on a futex
    held by a low-priority thread, the kernel BOOSTS the low-priority
    thread to the high-priority level. This prevents "priority inversion"
    where a medium-priority thread prevents the low-priority holder from
    running, indirectly blocking the high-priority waiter.

    Used by: some real-time database configurations (PREEMPT_RT kernel).
    Most databases do NOT use PI futexes because the overhead is ~2× and
    priority inversion is handled by keeping critical sections very short.

FUTEX HASHING PROBLEM — A DATABASE SCALABILITY CONCERN:

  The global futex hash table is a contention point:

  ┌──────────────────────────────────────────────────────────────────┐
  │  Problem scenario:                                               │
  │  PostgreSQL with 1000 backends, 128 shared buffer mapping       │
  │  LWLock partitions. Under heavy contention:                     │
  │                                                                  │
  │  Hundreds of backends call futex(FUTEX_WAIT) simultaneously.    │
  │  Many hash to the SAME bucket (birthday paradox with 2048       │
  │  buckets and 1000 threads).                                     │
  │                                                                  │
  │  Each futex operation acquires the bucket spinlock.              │
  │  Under heavy futex traffic: bucket spinlock becomes hot.        │
  │                                                                  │
  │  Observed in production: high %sys CPU on systems with many     │
  │  connections contending on shared buffers.                       │
  │                                                                  │
  │  Mitigation: Linux 5.x+ increased default hash table size       │
  │  based on number of CPUs. But fundamental O(waiters) scanning   │
  │  in wake operations remains.                                     │
  └──────────────────────────────────────────────────────────────────┘
```

### 3.5 futex2 and the Future (Linux 5.16+)

```
FUTEX2 (futex_waitv syscall, Linux 5.16):

  The original futex() is a single multiplexed syscall (one uaddr,
  one operation). Modern applications need to wait on MULTIPLE futexes
  simultaneously (like epoll/poll for synchronization).

  NEW: sys_futex_waitv():
    Wait on up to 128 futexes simultaneously.
    Returns when ANY of them is woken.

    struct futex_waitv {
        __u64 val;        // expected value
        __u64 uaddr;      // user address
        __u32 flags;       // FUTEX_32, FUTEX2_SIZE_U8, etc.
        __u32 __reserved;
    };

    sys_futex_waitv(struct futex_waitv *waiters, int nr_futexes,
                    unsigned int flags, struct timespec *timeout,
                    clockid_t clockid);

  Why this matters for databases:
    A database thread waiting on "either page P becomes available OR
    the WAL is flushed OR a cancel signal arrives" previously needed
    multiple threads or eventfd hacks. futex_waitv enables clean
    multi-wait on different synchronization objects.

  FUTEX2 SIZE EXTENSIONS (proposed, evolving):
    Original futex: only 32-bit words.
    futex2 proposes: 8-bit, 16-bit, 32-bit, 64-bit futex words.
    8-bit: compact latches (parking_lot-style 1-byte locks) can have
    kernel sleeping support without wasting 32 bits.
    16-bit: ticket locks (two 16-bit halves) can be natively waited on.

  STATUS:
    futex_waitv(): merged in Linux 5.16, available now.
    Other futex2 features: under development, not yet stable ABI.
```

### 3.6 macOS, FreeBSD, and Other Platforms

```
PLATFORM DIFFERENCES IN KERNEL WAITING MECHANISMS:

  macOS (Darwin/XNU kernel):
  ──────────────────────────
    NO futex syscall. Apple never adopted it.
    Instead uses:
      • ulock_wait / ulock_wake (private SPI, undocumented)
        Used internally by libpthread since macOS 10.12.
        Similar to futex but not exposed in public headers.
      • __psynch_mutexwait / __psynch_mutexdrop (older mechanism)
        Used by pthread_mutex on older macOS.
      • os_unfair_lock (macOS 10.12+, public API):
        Apple's recommended lightweight lock.
        8 bytes. No spinning. Blocks immediately on contention.
        Kernel-scheduled wake-up order (no starvation).
        Cannot be used across processes (private only).
      • Mach semaphores (semaphore_wait/semaphore_signal):
        Heavyweight. Rarely used for latching.

    Impact on databases: PostgreSQL, MySQL on macOS use pthread_mutex
    which goes through ulock_wait — slightly different performance
    profile than Linux futex. Cross-platform custom latches using CAS
    + spin avoid this difference entirely.

  FreeBSD:
  ────────
    _umtx_op() syscall (since FreeBSD 5.0):
    Similar to Linux futex but more feature-rich.
    Supports: mutex, rwlock, semaphore, condition variable operations
    directly in one syscall family.
    Used by FreeBSD's libthr (their pthreads implementation).
    PostgreSQL on FreeBSD uses this path.

  Windows:
  ────────
    WaitOnAddress / WakeByAddressSingle / WakeByAddressAll (Win 8+):
    Microsoft's equivalent of futex. Implemented via keyed events
    in the kernel. Added after they saw how well futex worked.
    Used by: SRWLock, CONDITION_VARIABLE, InitOnceExecuteOnce.
    SQL Server uses a combination of SRWLock and custom SOS spinlocks.

  Comparison of kernel wait mechanisms:
  ┌───────────────┬──────────────┬──────────────┬──────────────────────┐
  │  Platform      │  Syscall      │  Min Size    │  Features            │
  ├───────────────┼──────────────┼──────────────┼──────────────────────┤
  │  Linux         │  futex()      │  4 bytes     │  PI, requeue, bitset │
  │  Linux 5.16+   │  futex_waitv  │  1-8 bytes   │  Multi-wait          │
  │  macOS         │  ulock_*      │  4 bytes     │  Unfair/fair modes   │
  │  FreeBSD       │  _umtx_op()   │  4 bytes     │  Integrated RW/CV    │
  │  Windows 8+    │  WaitOnAddress│  1-8 bytes   │  Any-size wait       │
  │  Windows (old) │  NtKeyedEvent │  4 bytes     │  Pair-based wait     │
  └───────────────┴──────────────┴──────────────┴──────────────────────┘
```

### 3.7 Why Databases Avoid the Kernel When Possible

```
KERNEL CROSSING COSTS — THE FULL PICTURE:

  Even beyond the raw syscall overhead, kernel involvement has
  second-order costs that databases are uniquely sensitive to:

  1. TLB PRESSURE
     Some kernel entries flush TLB entries (or require ASID rotation).
     After returning to user space, the database's page table entries
     are cold. For a database with 100 GB buffer pool → thousands of
     hot pages → TLB misses cost ~7 ns each on L2 TLB miss.
     100 TLB misses after a context switch = 700 ns invisible tax.

  2. CACHE POLLUTION
     Kernel code executes in kernel text pages, kernel stack, kernel
     data structures. These displace user-space cache lines (database
     hot pages, B-tree nodes). On return, the database suffers L1/L2
     cache misses on data it was actively using.

     Measured on Intel Skylake:
       L1d miss after context switch: ~200-500 extra misses
       At 4 ns per L1d miss (L2 hit): 0.8-2 μs hidden cost
       L2 miss (L3 hit): ~10 ns each → even worse

  3. SCHEDULER NON-DETERMINISM
     Once in the kernel, the scheduler might:
     - Run a different thread entirely (if higher priority)
     - Migrate the woken thread to a different CPU (cache-cold!)
     - Delay wake-up due to timer granularity (1 ms on older kernels,
       ~50 μs on modern NOHZ_FULL kernels)

  4. SPECTRE/MELTDOWN MITIGATIONS (post-2018)
     On affected CPUs, every kernel entry/exit has additional overhead:
     - Kernel Page Table Isolation (KPTI): full CR3 switch on entry/exit
       → TLB flush (mitigated by PCID but still ~100 ns extra)
     - Indirect Branch Prediction Barrier (IBPB): flushes branch predictors
     - Return Stack Buffer (RSB) stuffing
     Combined overhead: ~100-400 ns per syscall on affected hardware.

  BOTTOM LINE:
    A futex round-trip that "should" cost 3 μs can easily cost 5-15 μs
    when you account for TLB refills, cache warming, and speculative
    execution mitigations. This is why every database team that measures
    carefully ends up building custom latches that avoid the kernel path.
```

---

## 4. Why Database Engineers Reject OS Locks

The uncontended fast path of `pthread_mutex` (~25 ns) seems fast enough. But database workloads expose multiple problems that compound under real conditions.

### 4.1 The Core Problems

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    WHY OS LOCKS FAIL FOR DATABASES                       │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  PROBLEM 1: SIZE                                                        │
│  ───────────────                                                        │
│  pthread_mutex_t = 40 bytes (Linux x86-64)                             │
│  A B+Tree with 10M leaf nodes needs one latch per node.                │
│  10M × 40 bytes = 400 MB just for mutexes!                             │
│  Database latch: 1-8 bytes. Same tree: 10-80 MB.                       │
│                                                                         │
│  PROBLEM 2: MEMORY LAYOUT / CACHE EFFICIENCY                           │
│  ───────────────────────────────────────────                            │
│  pthread_mutex has 40 bytes of internal state.                          │
│  A cache line is 64 bytes.                                              │
│  Mutex + protected data may span two cache lines.                       │
│  Database latch (4-8 bytes) can live IN the page/node header,          │
│  meaning the latch and the data it protects share one cache line.      │
│  One cache miss instead of two.                                         │
│                                                                         │
│  PROBLEM 3: KERNEL TRANSITION COST                                      │
│  ────────────────────────────────                                       │
│  When contended, OS locks make syscalls: futex(FUTEX_WAIT/WAKE).       │
│  Each syscall = context switch ≈ 1-5 μs.                               │
│  A B+Tree traversal touches 3-4 nodes. If each latch sleeps:          │
│  4 × 5 μs = 20 μs overhead — the ENTIRE lookup should take <1 μs.    │
│  Database latches spin briefly then yield; many never enter kernel.     │
│                                                                         │
│  PROBLEM 4: SCHEDULING CONTROL                                         │
│  ────────────────────────────                                           │
│  When a thread blocks on a futex, the OS scheduler picks what to run.  │
│  The OS has no idea that thread T2 (holding the latch on a hot page)   │
│  is more important than thread T3 (running a background checkpoint).   │
│  Databases want control over wake-up order and priority.               │
│                                                                         │
│  PROBLEM 5: NO MODE SPECIALIZATION                                     │
│  ────────────────────────────────                                       │
│  pthread_mutex is exclusive-only. For read-heavy workloads, databases  │
│  need shared/exclusive (reader-writer) latches.                         │
│  pthread_rwlock_t exists but is 56 bytes and has suboptimal fairness.  │
│  Database custom RW latches: 4-8 bytes, tuned fairness policies.       │
│                                                                         │
│  PROBLEM 6: NO OPTIMISTIC/TRY-UPGRADE SEMANTICS                       │
│  ───────────────────────────────────────────────                        │
│  Databases need: "read the version, proceed without holding anything,  │
│  then validate the version hasn't changed."                             │
│  This optimistic protocol cannot be expressed with OS locks.            │
│  They also need try-lock, lock-upgrade (S→X), and lock-downgrade.      │
│  OS primitives have limited or broken upgrade support.                  │
│                                                                         │
│  PROBLEM 7: NO HIERARCHICAL ORDERING ENFORCEMENT                      │
│  ────────────────────────────────────────────────                       │
│  Databases acquire latches in strict orders (parent before child in    │
│  B-tree, lower page_id before higher). OS locks do not enforce         │
│  ordering — if a bug violates the protocol, you get a deadlock with    │
│  no diagnostic help. Database debug builds can verify ordering.         │
│                                                                         │
│  PROBLEM 8: PORTABILITY AND BEHAVIORAL CONSISTENCY                     │
│  ─────────────────────────────────────────────────                      │
│  pthread_mutex behavior differs across Linux versions, glibc versions, │
│  macOS (no futex!), BSDs, Solaris, Windows. Database code must behave  │
│  identically everywhere. Custom latches built on CAS are portable and  │
│  predictable.                                                           │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Quantified Comparison

```
Operation                        pthread_mutex    Custom DB Latch    Ratio
─────────────────────────────────────────────────────────────────────────────
sizeof                           40 bytes         4-8 bytes          5-10×
Uncontended lock+unlock          ~25 ns           ~5-10 ns           2-5×
Contended (short spin)           ~100-500 ns      ~20-100 ns         3-5×
Contended (sleep)                ~1-10 μs         ~200ns-2 μs        3-5×
Cache lines touched              1-2              1                  1-2×
Supports reader-writer           No (need rwlock) Yes, built-in      —
Supports optimistic read         No               Yes                —
Supports try-lock                Yes              Yes                —
Supports lock ordering checks    No               Yes (debug build)  —
Kernel involvement               Yes (contended)  Rarely/never       —

Why the uncontended gap exists:
  pthread_mutex_lock must:
    1. Check for recursive locking (even if not recursive type)
    2. Check mutex type (normal, errorcheck, recursive, adaptive)
    3. Handle robust mutex protocol (owner-died detection)
    4. Memory fence (full barrier on x86)

  Custom latch:
    1. CAS or atomic exchange — done
    2. If read-path: may be just an atomic load + version check
```

### 4.3 The Buffer Pool Multiplier Effect

The reason size and speed matter so much is the sheer number of latch acquisitions per query:

```
A simple SELECT * FROM users WHERE id = 42 (B+Tree lookup):

  Step 1: Acquire latch on buffer pool hash table partition     ← latch #1
  Step 2: Look up root page in buffer pool
  Step 3: Release hash table latch                               ← unlatch #1
  Step 4: Acquire shared content latch on root page              ← latch #2
  Step 5: Binary search root page, find child pointer
  Step 6: Release root page latch                                ← unlatch #2
  Step 7: Acquire latch on buffer pool hash table partition      ← latch #3
  Step 8: Look up internal page in buffer pool
  Step 9: Release hash table latch                               ← unlatch #3
  Step 10: Acquire shared content latch on internal page         ← latch #4
  Step 11: Binary search, find leaf pointer
  Step 12: Release internal page latch                           ← unlatch #4
  Step 13: Acquire latch on buffer pool hash table partition     ← latch #5
  Step 14: Look up leaf page in buffer pool
  Step 15: Release hash table latch                              ← unlatch #5
  Step 16: Acquire shared content latch on leaf page             ← latch #6
  Step 17: Binary search, find row
  Step 18: Release leaf page latch                               ← unlatch #6

  Total: 6 latch acquisitions + 6 releases for ONE point lookup

  At 100,000 queries/second: 600,000 latch pairs/second
  At 25 ns each (pthread_mutex): 600K × 25 ns = 15 ms/sec of pure latch overhead
  At 8 ns each (custom latch):   600K × 8 ns  = 4.8 ms/sec of pure latch overhead

  The difference compounds: 10 ms/sec saved × 60 cores = significant throughput gain.
  Under contention, the gap widens dramatically.
```

---

## 5. Hardware Foundations: Atomics and Memory Ordering

Custom latches are built from CPU atomic instructions. Understanding what the hardware provides is essential.

### 5.1 Core Atomic Instructions (x86-64)

```
INSTRUCTION          WHAT IT DOES                                    COST (cycles)
──────────────────────────────────────────────────────────────────────────────────
LOCK CMPXCHG         Compare-And-Swap (CAS):                         15-30
(CAS)                if (*addr == expected) { *addr = new; return true; }
                     else { expected = *addr; return false; }
                     Foundation of most lock-free algorithms.

LOCK XCHG            Atomic exchange: old = *addr; *addr = new;      10-20
                     return old;
                     Simpler than CAS, used for simple spinlocks.

LOCK XADD            Fetch-And-Add: old = *addr; *addr += val;       10-20
(FAA)                return old;
                     Used for reference counting, sequence numbers.

LOCK BTS/BTR/BTC     Bit Test-and-Set/Reset/Complement               10-20
                     Atomic bit manipulation. Used for lock bits
                     embedded in pointers.

MFENCE               Full memory fence. Orders all loads and stores.  30-50
                     Expensive! Databases avoid when possible.

LFENCE / SFENCE      Load fence / Store fence.                        ~5
                     Rarely needed on x86 (strong memory model).

PAUSE                Spin-wait hint. Saves power, avoids memory       ~10
                     order violation pipeline flush.
                     CRITICAL in spin loops.
```

### 5.2 Cache Coherence: Why Contention Is Expensive

```
MESI Protocol (simplified):
  Each cache line (64 bytes) has a state per core:

  M (Modified):  This core has the only copy, it's dirty
  E (Exclusive): This core has the only copy, it's clean
  S (Shared):    Multiple cores have clean copies
  I (Invalid):   This core's copy is stale/nonexistent

What happens during latch contention (3 cores fighting for one latch):

  Time 0: Core 0 holds latch (cache line in M state on Core 0)

  Time 1: Core 1 tries CAS on the latch word
           ┌─────────────────────────────────────────────────────┐
           │  1. Core 1 sends "Read with Intent to Modify"       │
           │  2. Interconnect delivers request to Core 0          │
           │  3. Core 0 flushes cache line to shared cache (L3)  │
           │  4. Core 0 marks its copy Invalid                    │
           │  5. Core 1 gets the cache line in M state            │
           │  6. Core 1 performs the CAS                          │
           │  Cost: 40-100 ns on same socket                      │
           │  Cost: 100-300 ns cross-socket (NUMA)                │
           └─────────────────────────────────────────────────────┘

  Time 2: Core 2 tries CAS on the same latch word
           Same bouncing: cache line moves from Core 1 → Core 2

  This is "cache line bouncing" or "cache line ping-pong."

  Under high contention with N cores:
  ┌──────────────────────────────────────────────┐
  │  Each attempt costs ~100 ns (cache transfer)  │
  │  N cores × 100 ns = throughput collapse        │
  │  Worse: invalidations interfere with          │
  │  productive work on nearby cache lines         │
  └──────────────────────────────────────────────┘

  This is why databases use techniques that REDUCE atomic operations
  under contention rather than just "spin harder."
```

### 5.3 Memory Ordering Models

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    MEMORY ORDERING MODELS                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  x86/x86-64 (Intel, AMD): Total Store Order (TSO)                      │
│  ──────────────────────────────────────────────                         │
│  • Stores are seen by other cores in program order                      │
│  • Loads are NOT reordered with other loads                             │
│  • Loads CAN be reordered before earlier stores (store buffer)         │
│  • Consequence: acquire/release semantics are essentially FREE         │
│    (just regular loads/stores, no extra fences needed)                  │
│  • Only need MFENCE for sequential consistency (rare)                  │
│  • This is why x86 database code looks deceptively simple              │
│                                                                         │
│  ARM (ARMv8-A, Apple M-series, Graviton): Weakly Ordered              │
│  ─────────────────────────────────────────────────────                  │
│  • Almost any reordering is allowed unless you say otherwise            │
│  • Loads can be reordered with loads                                    │
│  • Stores can be reordered with stores                                  │
│  • Must use explicit barrier instructions:                              │
│    LDAR (load-acquire), STLR (store-release), DMB (full barrier)       │
│  • Consequence: code that "works on x86" may SILENTLY BREAK on ARM    │
│  • ARMv8.1+: LSE atomics (CAS, SWP, LDADD) — hardware CAS!           │
│    Before LSE: CAS was emulated with LL/SC (LDXR/STXR) loop          │
│                                                                         │
│  RISC-V: Weakly Ordered (with RVTSO optional extension)               │
│  ────────────────────────────────────────────────────                   │
│  • Default: relaxed ordering, needs fence instructions                  │
│  • RVTSO extension: x86-like TSO (for easier porting)                  │
│  • AMO instructions: atomic memory operations                           │
│  • LR/SC: load-reserved / store-conditional (like ARM LL/SC)           │
│                                                                         │
│  PRACTICAL IMPACT FOR DATABASE ENGINEERS:                               │
│  ────────────────────────────────────────                               │
│  A spinlock on x86:                                                     │
│    lock:   while (xchg(&flag, 1) != 0) { pause(); }                   │
│    unlock: flag = 0;  // plain store is fine (TSO guarantees order)    │
│                                                                         │
│  Same spinlock on ARM (WRONG without barriers):                        │
│    lock:   while (xchg(&flag, 1) != 0) { yield(); }                   │
│    unlock: flag = 0;  // BUG! store can be reordered before           │
│                       // critical section writes                        │
│                                                                         │
│  Correct on ARM:                                                        │
│    lock:   while (atomic_exchange_acquire(&flag, 1) != 0) { yield(); }│
│    unlock: atomic_store_release(&flag, 0);                             │
│                                                                         │
│  This is why C11/C++11 atomics with memory_order tags exist.           │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 6. Intel vs AMD: Hardware Lock Semantics Deep Dive

Intel and AMD both implement x86-64 but differ meaningfully in how their hardware executes atomic instructions, manages store buffers, and maintains cache coherence. These differences directly impact latch performance in databases.

### 6.1 The LOCK# Prefix: What Actually Happens on the Bus

```
When you write: LOCK CMPXCHG [mem], reg

  The CPU must guarantee that the entire read-modify-write sequence
  is ATOMIC — no other core can see an intermediate state.

  HOW THIS WORKS — TWO DIFFERENT MECHANISMS:

  MECHANISM 1: BUS LOCK (old, slow, still exists)
  ─────────────────────────────────────────────────
    The CPU asserts the LOCK# signal on the memory bus.
    ALL other cores are blocked from accessing memory until the
    instruction completes. This is essentially a global stop-the-world.

    When bus lock is used:
      • Operand spans two cache lines (misaligned access)
      • Operand is in uncacheable memory (UC, WC memory types)
      • Split-lock: a locked operation on data that crosses a
        cache line boundary (64 bytes)

    Cost: 100-500+ cycles (catastrophic for performance)

    ┌──────────────────────────────────────────────────────────────┐
    │  SPLIT LOCKS — THE SILENT DATABASE KILLER                    │
    │                                                              │
    │  If an atomic variable is misaligned and spans two cache     │
    │  lines (e.g., a uint64_t at address 0x...3C which spans     │
    │  cache lines at 0x...00 and 0x...40), the CPU CANNOT use    │
    │  cache locking. It MUST assert LOCK# on the bus.            │
    │                                                              │
    │  One split lock can stall ALL cores for 100+ μs on some CPUs.│
    │                                                              │
    │  Linux 5.7+: CONFIG_SPLIT_LOCK_DETECT (Intel only)          │
    │    Alignment Check (#AC) on split lock → SIGBUS or           │
    │    rate-limiting the offending process.                       │
    │                                                              │
    │  Database implication: ALWAYS align atomic variables to at   │
    │  least their own size. Align latches to cache line (64 bytes)│
    │  when possible. alignas(64) in C++ or #pragma pack.          │
    │                                                              │
    │  Intel CPUs since Tremont/Alder Lake: can trap on split lock │
    │  AMD CPUs since Zen 5: similar detection capability           │
    └──────────────────────────────────────────────────────────────┘

  MECHANISM 2: CACHE LOCK (modern, fast, default)
  ────────────────────────────────────────────────
    Instead of locking the bus, the CPU locks the cache line.
    The cache coherence protocol (MESI/MOESI) ensures exclusivity.

    How:
    a. Core issues LOCK CMPXCHG on address A
    b. Core's cache controller obtains the cache line in E or M state
       (exclusive ownership — no other core has a valid copy)
    c. Core performs the read-modify-write within its own L1 cache
    d. Other cores see the cache line as I (Invalid)
    e. The LOCK prefix ensures the line stays exclusive for the
       duration of the RMW operation (no intervening snoops honored)

    Cost: 10-30 cycles (local L1 hit), 40-100 cycles (must fetch line)
    This is what happens in the VAST MAJORITY of cases.
```

### 6.2 Intel-Specific Microarchitecture Details

```
INTEL CORE ARCHITECTURE (Skylake → Golden Cove → Raptor Cove → Lion Cove):

  STORE BUFFER:
  ──────────────
    Intel CPUs have a per-core store buffer (56 entries on Skylake,
    72 on Golden Cove, ~80 on Lion Cove).

    Stores go into the store buffer and drain to L1 cache asynchronously.
    Loads can snoop the store buffer (store-to-load forwarding).

    ┌──────────────────────────────────────────────────────────────────┐
    │  Core                                                            │
    │  ┌─────────┐   ┌──────────────┐   ┌─────────┐                  │
    │  │ Execution│──►│ Store Buffer  │──►│ L1d     │──► L2 ──► L3    │
    │  │ Units    │   │ (56-80 slots) │   │ Cache   │                  │
    │  └─────────┘   │              │   │ (48KB   │                  │
    │                 │ Drains FIFO  │   │  12-way) │                  │
    │                 │ to L1 cache  │   └─────────┘                  │
    │                 └──────────────┘                                  │
    │                                                                   │
    │  KEY: LOCK prefix forces store buffer drain BEFORE the locked    │
    │  instruction executes. This is part of why LOCK is expensive.    │
    │  The pipeline stalls until all pending stores are visible.        │
    └──────────────────────────────────────────────────────────────────┘

  LOCK INSTRUCTION EXECUTION (Intel):
    1. Drain store buffer (ensure all prior stores are globally visible)
    2. Acquire cache line in Exclusive/Modified state
    3. Execute the read-modify-write atomically
    4. The result is in the store buffer (or L1 if direct)
    5. Implicit memory fence: all loads/stores before LOCK are visible
       to other cores before the LOCK result is visible

  INTEL-SPECIFIC OPTIMIZATIONS:
    • Hardware Lock Elision (HLE): Deprecated since Alder Lake.
      Was: hint prefix (XACQUIRE/XRELEASE) to speculatively elide locks.
      Removed due to security concerns and limited real-world benefit.

    • TSX (Transactional Synchronization Extensions): Disabled/removed
      on most CPUs since 11th gen. See section 14.3 for details.

    • WAITPKG (Tpause, Umonitor, Umwait — since Tremont/Alder Lake):
      New instructions for efficient spin-waiting.
      TPAUSE: like PAUSE but allows specifying a deadline (TSC value).
        Thread enters low-power state until deadline or interrupt.
        Two power states: C0.1 (light sleep, ~20 cycle wakeup) and
        C0.2 (deeper sleep, ~100 cycle wakeup).
      UMONITOR/UMWAIT: monitor a cache line and wait for it to change.
        Like MWAIT (kernel-only) but usable in Ring 3!
        Instead of spinning in a TTAS loop:
          UMONITOR &latch_word      // monitor this address
          UMWAIT C0.1, deadline     // sleep until line changes or timeout
        Advantage: zero cache line traffic while waiting, saves power,
        wakes precisely when the line is written.
      Database impact: could replace PAUSE in spin loops. Not yet widely
      adopted (requires OS to enable via CR4.UMIP and XCR0 bits).

    • SERIALIZE (since Alder Lake):
      Full pipeline serialization instruction. More expensive than MFENCE
      but guarantees all prior instructions have completed (not just
      memory operations). Used by: some latch implementations for
      extremely strong ordering guarantees.

  INTEL CACHE COHERENCE — MESIF PROTOCOL:
    Intel extends MESI with an F (Forward) state:

    ┌────────┬─────────────────────────────────────────────────────────┐
    │ State   │ Meaning                                                 │
    ├────────┼─────────────────────────────────────────────────────────┤
    │ M       │ Modified: only copy, dirty                              │
    │ E       │ Exclusive: only copy, clean                             │
    │ S       │ Shared: multiple copies, clean                          │
    │ I       │ Invalid: stale, don't use                               │
    │ F       │ Forward: shared, BUT this core responds to snoops      │
    │         │ (only ONE core has F, others have S)                    │
    │         │ Avoids multiple cores answering the same snoop request  │
    │         │ Reduces interconnect traffic by ~50% for shared data    │
    └────────┴─────────────────────────────────────────────────────────┘

    F state matters for databases: when 64 cores all hold a shared latch
    (S state), a new reader's snoop is answered by exactly ONE core (the
    F holder) instead of 63 cores all responding. This is Intel's
    optimization for read-heavy sharing patterns.

  INTEL INTERCONNECT (Ring Bus → Mesh):
    Skylake-SP+ server chips use a mesh interconnect.
    Latency depends on distance between cores on the mesh:

    Adjacent tiles:     ~20 ns for cache-to-cache transfer
    Opposite corners:   ~50 ns on large dies (28+ cores)
    Cross-socket (UPI): ~100-140 ns (2-socket), ~150-200 ns (4-socket)

    For latches: the physical location of the core that last touched
    the cache line matters. NUMA-aware latch designs help here.
```

### 6.3 AMD-Specific Microarchitecture Details

```
AMD ZEN ARCHITECTURE (Zen 3 → Zen 4 → Zen 5):

  STORE BUFFER:
  ──────────────
    AMD Zen 4: 64-entry store buffer per core.
    AMD Zen 5: ~72-entry store buffer per core.
    Similar to Intel in principle but with different forwarding rules.

    AMD-specific: Store-to-load forwarding has stricter alignment
    requirements than Intel. A misaligned store-to-load forward may
    fail and go to L1 instead (adds ~5 cycles). Databases should
    ensure atomic variables are naturally aligned.

  AMD LOCK INSTRUCTION EXECUTION:
    Similar to Intel but with AMD-specific pipeline behavior:
    1. Drain store buffer
    2. Cache line acquisition via MOESI protocol
    3. Execute RMW atomically
    4. Implicit fence

    KEY DIFFERENCE: AMD's LOCK implementation has been observed to be
    slightly faster than Intel's for uncontended cases on some Zen
    generations, but slightly slower under heavy contention due to
    differences in the coherence protocol.

  AMD CACHE COHERENCE — MOESI PROTOCOL:
    AMD uses MOESI, which adds an O (Owned) state to MESI:

    ┌────────┬─────────────────────────────────────────────────────────┐
    │ State   │ Meaning                                                 │
    ├────────┼─────────────────────────────────────────────────────────┤
    │ M       │ Modified: only copy, dirty                              │
    │ O       │ Owned: this core has the authoritative dirty copy,     │
    │         │ but OTHER cores may have S (stale-shared) copies.       │
    │         │ This core must supply the data on snoops.               │
    │         │ KEY DIFFERENCE from Intel MESIF!                        │
    │ E       │ Exclusive: only copy, clean                             │
    │ S       │ Shared: may be clean or stale (if an O copy exists)    │
    │ I       │ Invalid: stale, don't use                               │
    └────────┴─────────────────────────────────────────────────────────┘

    The O state means AMD can do DIRTY SHARING:
    Core 0 modifies a cache line → line is in M state.
    Core 1 reads the line → on Intel, Core 0 must write back to L3
    and both get S. On AMD, Core 0 moves to O state and Core 1 gets S.
    The data is NEVER written to L3. This saves an L3 write.

    Impact on databases:
    ┌──────────────────────────────────────────────────────────────────┐
    │  Pattern: Core 0 acquires latch (writes M), releases it.        │
    │  Core 1 reads the latch word to check if free.                  │
    │                                                                  │
    │  Intel MESIF:                                                    │
    │    Core 0 (M) → snoop from Core 1 → writeback to L3 → both S   │
    │    Cost: L3 write (~10 ns) + snoop (~20-40 ns) = ~30-50 ns     │
    │                                                                  │
    │  AMD MOESI:                                                      │
    │    Core 0 (M→O) → supplies data directly to Core 1 (S)         │
    │    Cost: direct core-to-core transfer = ~20-40 ns               │
    │    NO L3 writeback! Saves ~10 ns.                                │
    │                                                                  │
    │  Winner for latch passing: AMD MOESI (marginally faster for     │
    │  the common pattern of "release on one core, acquire on another")│
    └──────────────────────────────────────────────────────────────────┘

  AMD CCD/CCX TOPOLOGY (critical for NUMA-aware latching):

    ┌──────────────────────────────────────────────────────────────────┐
    │  AMD EPYC (Zen 4, e.g., Genoa 9654):                            │
    │                                                                  │
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
    │  │   CCD 0      │  │   CCD 1      │  │   CCD 2      │  ...x12  │
    │  │ ┌───┐ ┌───┐ │  │ ┌───┐ ┌───┐ │  │ ┌───┐ ┌───┐ │            │
    │  │ │C0 │ │C1 │ │  │ │C8 │ │C9 │ │  │ │C16│ │C17│ │            │
    │  │ ├───┤ ├───┤ │  │ ├───┤ ├───┤ │  │ ├───┤ ├───┤ │            │
    │  │ │C2 │ │C3 │ │  │ │C10│ │C11│ │  │ │C18│ │C19│ │            │
    │  │ ├───┤ ├───┤ │  │ ├───┤ ├───┤ │  │ ├───┤ ├───┤ │            │
    │  │ │C4 │ │C5 │ │  │ │C12│ │C13│ │  │ │C20│ │C21│ │            │
    │  │ ├───┤ ├───┤ │  │ ├───┤ ├───┤ │  │ ├───┤ ├───┤ │            │
    │  │ │C6 │ │C7 │ │  │ │C14│ │C15│ │  │ │C22│ │C23│ │            │
    │  │ └───┘ └───┘ │  │ └───┘ └───┘ │  │ └───┘ └───┘ │            │
    │  │  32MB L3     │  │  32MB L3     │  │  32MB L3     │            │
    │  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘            │
    │         │                 │                 │                     │
    │         └────────► Infinity Fabric ◄────────┘                    │
    │                    (on-die interconnect)                          │
    │                                                                  │
    │  Latency within CCD:  ~15-20 ns (shared L3)                     │
    │  Latency cross-CCD:   ~40-80 ns (Infinity Fabric)               │
    │  Latency cross-socket: ~120-200 ns (Infinity Fabric link)       │
    │                                                                  │
    │  The CCD boundary is a MINI-NUMA boundary even within a socket! │
    │  A latch held by Core 0 (CCD 0) and contested by Core 8 (CCD 1)│
    │  pays the cross-CCD penalty even on the same physical chip.     │
    │                                                                  │
    │  Database implication: On AMD EPYC, thread-to-core affinity     │
    │  within the same CCD for threads that share latches significantly│
    │  reduces latency. Some databases pin buffer pool partitions to   │
    │  CCD boundaries (Oracle does this, PostgreSQL does not yet).     │
    └──────────────────────────────────────────────────────────────────┘
```

### 6.4 Intel vs AMD: Head-to-Head Comparison

```
┌─────────────────────────────────┬────────────────────┬────────────────────┐
│  Feature                         │  Intel (Sapphire    │  AMD (Zen 4 / 5    │
│                                  │  Rapids / Granite   │  EPYC Genoa /      │
│                                  │  Rapids)            │  Turin)             │
├─────────────────────────────────┼────────────────────┼────────────────────┤
│  Cache coherence protocol        │  MESIF              │  MOESI              │
│  Dirty data sharing              │  No (writeback to   │  Yes (O state,     │
│                                  │  L3, then share)    │  direct transfer)  │
│  L1d cache                       │  48 KB, 12-way      │  32-48 KB, 8-way   │
│  L2 cache                        │  2 MB per core      │  1 MB per core     │
│  L3 cache                        │  Shared, 30-60 MB   │  Per-CCD, 32 MB    │
│  L3 topology                     │  Shared across all  │  Private per CCD   │
│                                  │  cores (mesh)       │  (not shared!)     │
│  Cache line size                 │  64 bytes           │  64 bytes           │
│  Uncontended LOCK CMPXCHG       │  ~15-25 cycles      │  ~12-20 cycles     │
│  Contended LOCK CMPXCHG (same   │  ~40-70 cycles      │  ~35-60 cycles     │
│  socket, different core)         │                     │                     │
│  Contended cross-socket          │  ~200-350 cycles    │  ~250-400 cycles   │
│  PAUSE instruction latency       │  ~140 cycles (SKL+) │  ~65 cycles (Zen4) │
│                                  │  ~40 cycles (ICL+)  │  ~45 cycles (Zen5) │
│  Store buffer entries            │  56-80              │  64-72              │
│  MFENCE cost                     │  ~30-50 cycles      │  ~25-40 cycles     │
│  Split-lock detection            │  Yes (Tremont+)     │  Yes (Zen 5)       │
│  WAITPKG (UMWAIT/TPAUSE)        │  Yes (Alder Lake+)  │  No (as of Zen 5)  │
│  CMPXCHG16B (128-bit CAS)       │  Yes                │  Yes                │
│  LZCNT, TZCNT (bit manipulation)│  Yes                │  Yes                │
├─────────────────────────────────┼────────────────────┼────────────────────┤
│  IMPACT ON DATABASE LATCHES:     │                     │                     │
│  ─────────────────────────────  │                     │                     │
│  Spin loop efficiency            │  Worse (PAUSE is    │  Better (PAUSE is  │
│                                  │  very slow on SKL)  │  faster)           │
│  RW latch passing                │  Slightly slower    │  Slightly faster   │
│  (read after write, core-to-core)│  (no O state)      │  (O state, direct  │
│                                  │                     │   cache transfer)  │
│  Large shared dataset (>32MB)    │  Better (shared L3) │  Worse (per-CCD L3 │
│                                  │                     │   → cross-CCD miss)│
│  Many-core scaling (64+ cores)   │  Better (mesh,      │  Good (IF fabric,  │
│                                  │  shared L3)         │  but CCD boundaries│
│                                  │                     │  add latency hops) │
└─────────────────────────────────┴────────────────────┴────────────────────┘

PAUSE INSTRUCTION STORY — AN IMPORTANT DETAIL:

  The PAUSE instruction is used in EVERY spin loop in EVERY database.
  Its latency changed dramatically and caught everyone off guard:

  Intel Sandy Bridge - Broadwell: PAUSE ≈ 10 cycles
  Intel Skylake (2015):           PAUSE ≈ 140 cycles (14× SLOWER!)
  Intel Ice Lake+ (2019):         PAUSE ≈ 40 cycles (tuned back)

  AMD Zen 1-3:                    PAUSE ≈ 50-65 cycles
  AMD Zen 4-5:                    PAUSE ≈ 45 cycles

  When Intel changed PAUSE from 10 to 140 cycles on Skylake, databases
  that had been tuned with SPIN_COUNT=1000 on Broadwell suddenly had
  spin loops that took 14× longer. Latencies spiked, throughput dropped.

  This led to:
  - PostgreSQL adjusting DEFAULT_SPINS_PER_DELAY
  - MySQL retuning innodb_spin_wait_delay
  - Many databases adding adaptive spin counts

  LESSON: Never hardcode spin counts. Make them configurable or adaptive.
  The hardware WILL change under you.

CMPXCHG16B — DOUBLE-WIDTH CAS:

  Both Intel and AMD support CMPXCHG16B: an atomic 128-bit CAS.
  This is critical for lock-free algorithms that need to atomically
  update a pointer + counter (to solve ABA problem) or two adjacent
  pointers.

  Usage in databases:
    Tagged pointers: [48-bit pointer | 16-bit counter]
    or double-pointer: [ptr1 | ptr2] atomically swapped
    Lock-free stacks and queues use this extensively.

  Caveats:
    - Requires 16-byte alignment (or split-lock penalty!)
    - ~2× slower than regular CMPXCHG (two cache line accesses possible)
    - On x86-64: guaranteed available (it's a required feature of x86-64)
    - On ARM: 128-bit atomics via LDXP/STXP (load-exclusive pair) in
      ARMv8, or CASP (compare-and-swap pair) with LSE
```

### 6.5 MFENCE, SFENCE, LFENCE: Intel vs AMD Semantics

```
FENCE INSTRUCTIONS — SUBTLY DIFFERENT SEMANTICS:

  MFENCE (Full Memory Fence):
  ───────────────────────────
    Intel: Orders ALL loads and stores. Drains the store buffer.
           Also serializes some non-memory operations.
    AMD:   Same semantics but historically faster execution.

    When databases use MFENCE:
      - Almost never! On x86 TSO, acquire/release are free.
      - Sequential consistency (memory_order_seq_cst) needs it.
      - Some use: LOCK ADDL $0, (%rsp) as a cheaper alternative
        to MFENCE (Intel optimization manuals suggest this).
        This "locked add of zero to stack top" acts as a full fence
        but is cheaper because it doesn't serialize as aggressively.

  SFENCE (Store Fence):
  ─────────────────────
    Orders STORES only. Does NOT order loads.
    Intel and AMD: same semantics.
    Only needed for WC (write-combining) memory regions.
    Databases almost never need this.
    Exception: databases using non-temporal stores (MOVNTI) for
    bulk data movement must SFENCE after the non-temporal writes.

  LFENCE (Load Fence):
  ────────────────────
    Intel: Orders loads AND serializes instruction stream.
           (Since Spectre mitigation: LFENCE is a serializing instruction
            on Intel, meaning the pipeline cannot execute past LFENCE
            until all prior instructions retire.)
    AMD:   Originally: orders loads only (weaker than Intel).
           Post-Spectre: AMD added MSR to make LFENCE serializing
           (MSR C001_1029 bit 1). Most Linux distros enable this.

    Database use of LFENCE:
      - Speculative execution barriers (prevent Spectre gadgets)
      - RDTSC ordering (LFENCE; RDTSC ensures RDTSC doesn't execute
        speculatively ahead of prior instructions — important for
        accurate latency measurement in database profiling)

  PRACTICAL ENCODING IN DATABASES:

    // Full fence (portable, works on Intel + AMD)
    std::atomic_thread_fence(std::memory_order_seq_cst);
    // Compiles to: MFENCE on GCC/Clang
    // Or sometimes: LOCK OR DWORD PTR [rsp], 0

    // Acquire fence (on x86: this is a NO-OP in generated code!)
    std::atomic_thread_fence(std::memory_order_acquire);
    // On x86: compiler barrier only, no hardware instruction
    // On ARM: DMB ISHLD

    // Release fence (on x86: also NO-OP in generated code!)
    std::atomic_thread_fence(std::memory_order_release);
    // On x86: compiler barrier only
    // On ARM: DMB ISH

    KEY INSIGHT: On x86 (both Intel and AMD), acquire and release
    fences compile to NOTHING — just compiler barriers. TSO gives
    you acquire/release for free. This is why x86 database code
    often has no visible fence instructions despite being correct.
    Port to ARM and the fences become real hardware instructions.
```

---

## 7. Page-Level and Memory-Level Locking Mechanics

This section covers how locking interacts with the memory subsystem — virtual memory pages, page tables, TLB, and mmap — and why databases care deeply about these interactions.

### 7.1 Virtual Memory Pages vs Database Pages

```
CRITICAL DISTINCTION:

  "Page" means DIFFERENT THINGS in different contexts:

  ┌─────────────────────────────────────────────────────────────────────────┐
  │  OS PAGE (Virtual Memory Page):                                         │
  │  ──────────────────────────────                                         │
  │  • Fixed size: 4 KB (default) or 2 MB / 1 GB (huge pages)             │
  │  • Managed by the OS virtual memory subsystem                           │
  │  • Each page has a Page Table Entry (PTE) with protection bits         │
  │  • TLB (Translation Lookaside Buffer) caches virtual→physical mapping  │
  │  • Protection bits: R (read), W (write), X (execute), U (user), etc.  │
  │                                                                         │
  │  DATABASE PAGE (Buffer Pool Page):                                      │
  │  ─────────────────────────────────                                      │
  │  • Configurable size: 8 KB (PostgreSQL), 16 KB (InnoDB), 32 KB, etc.  │
  │  • Managed by the database buffer pool manager                          │
  │  • Each page has a latch (custom lightweight lock)                      │
  │  • The buffer pool is typically allocated as a large chunk of           │
  │    anonymous mmap'd memory, subdivided into database-page-sized frames │
  │                                                                         │
  │  RELATIONSHIP:                                                          │
  │  A single 8 KB database page spans 2 OS pages (if 4 KB page size).    │
  │  A 16 KB database page spans 4 OS pages.                                │
  │  With 2 MB huge pages: 256 database pages (8 KB each) fit in one       │
  │  OS huge page.                                                          │
  └─────────────────────────────────────────────────────────────────────────┘
```

### 7.2 Page Table Entry Locking (Kernel Level)

```
PTE LOCKING — WHAT HAPPENS WHEN THE DATABASE TRIGGERS A PAGE FAULT:

  When a database process accesses a virtual address that is not yet
  mapped to a physical frame (e.g., first access to a buffer pool page
  after mmap), a page fault occurs. The kernel must update the page table.

  Page Table Structure (x86-64, 4-level):

    PML4 (Page Map Level 4)   ──► 512 entries, each covering 512 GB
      └► PDPT (Page Dir Ptr)  ──► 512 entries, each covering 1 GB
          └► PD (Page Dir)    ──► 512 entries, each covering 2 MB
              └► PT (Page Tab) ──► 512 entries, each covering 4 KB
                  └► Physical Frame

  Each level has entries that must be atomically updated.

  PTE LOCKING MECHANISMS (Linux kernel):

  ┌──────────────────────────────────────────────────────────────────────┐
  │  SPLIT PTE LOCKS (CONFIG_SPLIT_PTLOCK_CPUS):                        │
  │                                                                      │
  │  Each page table PAGE (not entry) has its own spinlock.              │
  │  One PT page contains 512 PTEs (covering 2 MB of virtual memory).   │
  │  Lock granularity: one spinlock per 512 PTEs.                        │
  │                                                                      │
  │  When two threads fault on addresses in the SAME 2 MB region:       │
  │    → They contend on the same PT page spinlock                       │
  │    → Serialized (but different 2 MB regions are independent)         │
  │                                                                      │
  │  When two threads fault on addresses in DIFFERENT 2 MB regions:     │
  │    → Different PT page spinlocks → no contention                     │
  │                                                                      │
  │  This affects database buffer pool initialization:                    │
  │  First access to each buffer pool page causes a minor page fault.    │
  │  With many concurrent connections all faulting on nearby pages:       │
  │  PT spinlock contention can be significant during warmup.             │
  │                                                                      │
  │  MITIGATION: Use huge pages (2 MB or 1 GB):                          │
  │    → Fewer page table entries (1 per 2 MB instead of 1 per 4 KB)     │
  │    → Fewer faults during buffer pool warmup                           │
  │    → No split PTE lock contention (PUD/PMD level locks instead)      │
  │    → Better TLB coverage (one TLB entry covers 2 MB, not 4 KB)       │
  └──────────────────────────────────────────────────────────────────────┘

  mmap_lock (formerly mmap_sem):
  ──────────────────────────────
  A per-process rw_semaphore that protects the entire VMA (Virtual
  Memory Area) tree. ANY operation that changes the virtual address
  space (mmap, munmap, mprotect, page fault, brk) must acquire this.

  ┌──────────────────────────────────────────────────────────────────────┐
  │  mmap_lock is the MOST CONTENDED kernel lock for databases.          │
  │                                                                      │
  │  Scenario: database using mmap for I/O (e.g., MongoDB WiredTiger    │
  │  with mmap'd files, SQLite, LMDB):                                  │
  │                                                                      │
  │  Thread 1: page fault on mmap'd region → mmap_lock(READ)            │
  │  Thread 2: page fault on mmap'd region → mmap_lock(READ)            │
  │  Thread 3: extending mmap (growing file) → mmap_lock(WRITE)          │
  │                                                                      │
  │  Thread 3 blocks ALL readers until the VMA update completes.         │
  │  With 1000 concurrent connections: thundering herd on mmap_lock.     │
  │                                                                      │
  │  Linux 5.1+: Speculative page fault handling (partial)               │
  │  Linux 6.1+: per-VMA locking (VMA lock, reduces mmap_lock pressure) │
  │    → Page faults can now take a per-VMA lock instead of the process  │
  │      mmap_lock for many common cases. Major improvement.              │
  │                                                                      │
  │  This is ONE MORE REASON databases avoid mmap and use direct I/O:   │
  │  direct I/O bypasses the page cache AND avoids mmap_lock contention. │
  └──────────────────────────────────────────────────────────────────────┘
```

### 7.3 TLB and Lock Interaction

```
TLB (TRANSLATION LOOKASIDE BUFFER) IMPACT ON LATCH PERFORMANCE:

  Every memory access — including every latch acquire — goes through
  the TLB. A TLB miss adds 7-30 ns to the access.

  TLB Capacity (typical):
  ┌────────────────┬──────────────────────────────────────────────────────┐
  │  Level          │  Intel (Sapphire Rapids)    AMD (Zen 4)             │
  ├────────────────┼──────────────────────────────────────────────────────┤
  │  L1 DTLB        │  64 entries (4KB pages)     72 entries (4KB)        │
  │                 │  + 8 entries (2MB pages)    + 8 entries (2MB)       │
  │  L2 STLB        │  2048 entries (4KB/2MB)     3072 entries (4KB/2MB)  │
  │  Coverage (4KB) │  8 MB                       12 MB                   │
  │  Coverage (2MB) │  4 GB (L1) + 4 GB (L2)     16 GB + 6 GB            │
  └────────────────┴──────────────────────────────────────────────────────┘

  Problem for databases:
    A database with 100 GB buffer pool has 25 million 4-KB OS pages.
    The L2 TLB covers only 8-12 MB (2048-3072 pages).
    Result: accessing a random latch in the buffer pool will TLB-miss
    with very high probability.

    SOLUTION: HUGE PAGES
    With 2 MB huge pages: 100 GB = 50,000 pages.
    L2 STLB with 2048 entries for 2 MB pages → covers 4 GB.
    With 1 GB huge pages: 100 GB = 100 pages. Fits trivially.

    Impact on latch performance:
    ┌────────────────────────────────────────────────────────────────┐
    │  Random latch access on 100 GB buffer pool:                    │
    │                                                                │
    │  4 KB pages:  ~95% L2 TLB miss rate → +20 ns per access       │
    │  2 MB pages:  ~10% L2 TLB miss rate → +2 ns average           │
    │  1 GB pages:  ~0% TLB miss rate     → +0 ns                   │
    │                                                                │
    │  With 600,000 latch operations/sec (from section 4.3):         │
    │  4 KB: 600K × 20 ns = 12 ms/sec extra TLB overhead            │
    │  2 MB: 600K × 2 ns  = 1.2 ms/sec                              │
    │  1 GB: 600K × 0 ns  = 0 ms                                    │
    └────────────────────────────────────────────────────────────────┘

  TLB SHOOTDOWN (the hidden killer):
    When the kernel needs to unmap a page (e.g., mmap region changed,
    page evicted, mprotect called), it must invalidate the TLB entry
    on ALL cores that might have cached it. This is called a TLB
    shootdown.

    Mechanism (x86): IPI (Inter-Processor Interrupt)
    1. Core 0 sends IPI to all other cores in the process's CPU set
    2. Each core receives the interrupt, flushes the specified TLB entries
    3. Each core acknowledges the flush
    4. Core 0 waits for all acknowledgments before proceeding

    Cost: 1-10 μs (depends on number of cores, IPI latency)

    Impact on databases:
    - During mmap operations: every mmap/munmap can trigger shootdown
    - During transparent huge page (THP) migration/splitting: shootdown
    - During page compaction: shootdown
    - Each shootdown interrupts ALL cores — including cores that are
      in the middle of a latched critical section!

    ┌──────────────────────────────────────────────────────────────────┐
    │  Scenario: Core A holds a latch, executing critical section.     │
    │  Core B triggers mmap_lock → TLB shootdown → IPI to Core A.     │
    │  Core A is INTERRUPTED. Handles IPI, flushes TLB entries.        │
    │  Core A resumes. But now its TLB is partially cold.              │
    │  Other cores waiting for Core A's latch waited an extra ~5 μs.  │
    │                                                                  │
    │  This is INVISIBLE in application metrics. It shows up only in   │
    │  perf counters: dtlb_load_misses, tlb:tlb_flush events.         │
    └──────────────────────────────────────────────────────────────────┘
```

### 7.4 Huge Pages: The Database Performance Multiplier

```
HUGE PAGES AND DATABASE LATCH PERFORMANCE:

  ┌───────────────────────────────────────────────────────────────────────────┐
  │  WHY EVERY PRODUCTION DATABASE SHOULD USE HUGE PAGES                      │
  ├───────────────────────────────────────────────────────────────────────────┤
  │                                                                           │
  │  1. TLB COVERAGE (as shown above)                                         │
  │     4 KB pages: 8-12 MB TLB coverage → constant TLB misses on large     │
  │     buffer pools                                                          │
  │     2 MB pages: 4-6 GB TLB coverage → most buffer pool accesses hit     │
  │     1 GB pages: 2-4 TB TLB coverage → entire buffer pool covered        │
  │                                                                           │
  │  2. FEWER PAGE FAULTS DURING WARMUP                                       │
  │     100 GB buffer pool:                                                   │
  │     4 KB pages: 25,600,000 minor page faults to initialize               │
  │     2 MB pages:      50,000 minor page faults                            │
  │     1 GB pages:          100 minor page faults                            │
  │                                                                           │
  │  3. FEWER PAGE TABLE ENTRIES → LESS PTE LOCK CONTENTION                  │
  │     100 GB with 4 KB pages: 25.6M PTEs across ~50,000 PT pages          │
  │     100 GB with 2 MB pages: 50K PMD entries                               │
  │     Fewer entries = fewer locks = less kernel contention.                  │
  │                                                                           │
  │  4. FEWER TLB SHOOTDOWNS                                                  │
  │     Fewer page table entries to manage → fewer shootdown events.          │
  │                                                                           │
  │  5. PAGE TABLE MEMORY SAVINGS                                             │
  │     100 GB with 4 KB pages: ~200 MB of page table memory                 │
  │     100 GB with 2 MB pages: ~400 KB of page table memory                 │
  │     The saved 200 MB can be used by the database instead!                 │
  └───────────────────────────────────────────────────────────────────────────┘

  CONFIGURATION:

  PostgreSQL:
    huge_pages = on                    (postgresql.conf)
    vm.nr_hugepages = 51200            (sysctl, for 100 GB at 2 MB each)
    shared_buffers = 100GB

  MySQL/InnoDB:
    large-pages = ON                   (my.cnf)
    innodb_buffer_pool_size = 100G
    vm.nr_hugepages = 51200            (sysctl)
    mysqld must be started with CAP_IPC_LOCK or SHM_HUGETLB permission

  Oracle:
    USE_LARGE_PAGES = ONLY             (init.ora / spfile)
    vm.nr_hugepages = 51200
    Oracle also supports 1 GB huge pages on supported platforms.

  Linux Transparent Huge Pages (THP):
    Sounds convenient but is DANGEROUS for databases:
    - THP compaction can stall processes for milliseconds
    - THP splitting triggers TLB shootdowns
    - THP migration causes latency spikes
    - Almost every database vendor says: DISABLE THP!
      echo never > /sys/kernel/mm/transparent_hugepage/enabled
```

### 7.5 mmap Locking Semantics and Why Databases Avoid Them

```
mmap AS A LOCKING MECHANISM — THE ANTI-PATTERN:

  Some databases use mmap to map data files into the process address
  space, relying on the OS page cache for caching and the OS VM system
  for page management. This seems convenient but has severe locking
  problems.

  ┌──────────────────────────────────────────────────────────────────────┐
  │  The mmap Locking Stack (what actually happens on each access):     │
  │                                                                      │
  │  Layer 1: Application code reads memory address (virtual)            │
  │     │                                                                │
  │     ▼  (if page is in TLB and page cache: fast path, ~1 ns)        │
  │                                                                      │
  │  Layer 2: TLB miss → page table walk                                 │
  │     │  (~7-30 ns, involves L1/L2 cache for PTE lookups)             │
  │     ▼                                                                │
  │                                                                      │
  │  Layer 3: Page fault (page not in physical memory)                   │
  │     │  Triggers kernel entry (SYSCALL-equivalent, ~20 ns)            │
  │     ▼                                                                │
  │                                                                      │
  │  Layer 4: mmap_lock (per-VMA lock in 6.1+, else process-wide)       │
  │     │  Contended under high concurrency                              │
  │     ▼                                                                │
  │                                                                      │
  │  Layer 5: Page cache lookup (address_space → xarray)                │
  │     │  Involves: i_pages lock (xa_lock) on the page cache tree      │
  │     │  Multiple threads faulting on the same file contend here       │
  │     ▼                                                                │
  │                                                                      │
  │  Layer 6: If not in page cache → disk I/O (blocking)                │
  │     │  The kernel chooses WHEN and HOW to do I/O.                    │
  │     │  Database has no control. No prefetching strategy.             │
  │     │  No control over eviction priority.                             │
  │     ▼                                                                │
  │                                                                      │
  │  Layer 7: Page installed, PTE updated, TLB refilled                  │
  │     │  May trigger TLB shootdowns on other cores                     │
  │     ▼                                                                │
  │                                                                      │
  │  Total locks acquired for ONE mmap page fault:                       │
  │  1. mmap_lock (read or write)                                        │
  │  2. PTE spinlock (page table page lock)                              │
  │  3. Page cache xa_lock (xarray/radix tree lock)                     │
  │  4. LRU lock (page reclaim list lock)                                │
  │  5. If I/O needed: block device queue lock                           │
  │  6. If huge page: compound page lock                                 │
  │  7. Various rcu_read_locks along the way                             │
  │                                                                      │
  │  Compare to buffer pool + direct I/O:                                │
  │  1. One custom latch on the buffer pool hash partition                │
  │  2. One custom latch on the page frame                               │
  │  If I/O needed: io_uring submission (no kernel lock)                 │
  │  TOTAL: 2 database latches, often no kernel involvement.             │
  └──────────────────────────────────────────────────────────────────────┘

  DATABASES THAT USE mmap AND THE CONSEQUENCES:
  ┌──────────────────────────┬──────────────────────────────────────────┐
  │  Database                 │  mmap Status                             │
  ├──────────────────────────┼──────────────────────────────────────────┤
  │  LMDB                    │  Full mmap. Copy-on-write B+tree via    │
  │                          │  mmap. Single-writer avoids many issues.│
  │                          │  Works well for read-heavy, small data. │
  │  MongoDB (WiredTiger)    │  Used mmap in old engine (MMAPv1),      │
  │                          │  removed in favor of WiredTiger with    │
  │                          │  its own buffer pool. Lesson learned.   │
  │  SQLite                  │  Optional mmap via sqlite3_mmap_size.   │
  │                          │  Default OFF. Single-writer model.      │
  │  PostgreSQL              │  Never used mmap for buffer pool.       │
  │                          │  Uses shared memory (shmget/mmap) for   │
  │                          │  shared buffers but manages pages itself.│
  │  MySQL/InnoDB            │  Never used mmap. Own buffer pool.      │
  │  Oracle                  │  Never used mmap. SGA is own management.│
  └──────────────────────────┴──────────────────────────────────────────┘
```

### 7.6 Memory Protection as a Locking Mechanism

```
USING MPROTECT FOR WRITE PROTECTION — A CLEVER TRICK:

  Some databases use the OS memory protection mechanism (mprotect)
  as a coarse-grained write lock:

  Strategy:
    1. Mark buffer pool pages as READ-ONLY via mprotect(addr, len, PROT_READ)
    2. When a page needs modification:
       a. The write attempt triggers a SIGSEGV (segfault)
       b. Signal handler catches it
       c. Handler calls mprotect(page, 4096, PROT_READ|PROT_WRITE)
       d. Handler marks page as dirty in database metadata
       e. Handler returns, instruction retries and succeeds
    3. After modification, optionally re-protect as read-only

  Why would anyone do this?
    - AUTOMATIC dirty page tracking without any application overhead
    - Every modified page is caught by the hardware MMU
    - No need to set dirty flags in application code
    - Used by some research databases and specialized storage engines

  Problems:
    - Each mprotect call is a SYSCALL (~200-500 ns)
    - Each mprotect changes PTEs → TLB shootdown → IPI to all cores
    - Signal handling overhead: ~1-5 μs per SIGSEGV
    - Very coarse-grained (entire 4 KB OS page, not specific tuple)
    - Not practical for high-write workloads

  Used by:
    - LMDB: uses copy-on-write at the OS page level
    - Some checkpoint mechanisms: mprotect all pages read-only, then
      fork() for copy-on-write snapshot (PostgreSQL uses fork instead)
    - Research systems: LLAMA, Silo (for write tracking)

CACHE LINE-LEVEL PROTECTION (what databases actually want):

  The ideal would be protection at the cache line level (64 bytes) —
  know exactly which 64-byte regions were modified. Hardware does not
  provide this directly, but some tricks approximate it:

  1. Intel Processor Trace (PT):
     Can trace memory accesses. Extremely high overhead. Not usable
     for production latching.

  2. WATCHPOINTS (hardware breakpoints):
     x86-64 has 4 debug registers (DR0-DR3) that can watch specific
     addresses. When the watched address is written, a #DB exception
     fires. Limited to 4 addresses — useless for a buffer pool with
     millions of pages.

  3. DIRTY BITS IN PTEs:
     The hardware sets the Dirty bit in the PTE when a page is written.
     The OS can read this to find dirty pages without signal handling.
     But granularity is still 4 KB (or 2 MB with huge pages).
     Used by: many databases for checkpointing (scan PTEs to find
     dirty pages, then write them to WAL/disk).

  4. APPLICATION-LEVEL DIRTY TRACKING:
     The practical solution: set a dirty flag in the page header when
     modifying any data on the page. This is what PostgreSQL, MySQL,
     and most databases do. It requires discipline (every write path
     must set the flag) but avoids all OS-level overhead.
```

### 7.7 NUMA Memory Placement and Lock Locality

```
NUMA AND LATCH INTERACTION:

  On NUMA (Non-Uniform Memory Access) systems, physical memory is
  local to specific CPU sockets. Accessing remote memory is 1.5-3×
  slower than local memory.

  ┌──────────────────────────────────────────────────────────────────────┐
  │  Socket 0                              Socket 1                      │
  │  ┌────────────────────┐               ┌────────────────────┐        │
  │  │ Cores 0-31          │               │ Cores 32-63         │        │
  │  │ Local DRAM: 256 GB  │◄── ~100 ns ──►│ Local DRAM: 256 GB  │        │
  │  │                     │   interconnect │                     │        │
  │  │ Local access: ~80 ns│               │ Local access: ~80 ns│        │
  │  │ Remote access:      │               │ Remote access:      │        │
  │  │   ~130-160 ns       │               │   ~130-160 ns       │        │
  │  └────────────────────┘               └────────────────────┘        │
  │                                                                      │
  │  A latch word physically in Socket 0's DRAM:                         │
  │    Core 0 CAS on latch: ~15 ns (L1 hit) to ~80 ns (DRAM)           │
  │    Core 32 CAS on latch: ~130-160 ns (remote DRAM)                  │
  │    That is 2× slower for EVERY latch operation across sockets!       │
  └──────────────────────────────────────────────────────────────────────┘

  MEMORY ALLOCATION POLICIES THAT AFFECT LATCH PERFORMANCE:

  1. FIRST-TOUCH (default Linux policy):
     Physical memory is allocated on the NUMA node of the CPU that
     first touches (faults on) the virtual address.

     Problem: if thread on Socket 0 initializes the buffer pool, ALL
     buffer pool memory lives on Socket 0's DRAM. Threads on Socket 1
     always access remote memory for every latch operation.

     ┌───────────────────────────────────────────────────────────┐
     │  Buffer pool (100 GB) all on Socket 0's DRAM:             │
     │                                                           │
     │  Socket 0 threads: latch access = ~15-80 ns (LOCAL)      │
     │  Socket 1 threads: latch access = ~130-160 ns (REMOTE!)  │
     │                                                           │
     │  Socket 1 threads are PERMANENTLY 2× slower.              │
     └───────────────────────────────────────────────────────────┘

  2. INTERLEAVE (numactl --interleave=all):
     Physical memory is round-robined across NUMA nodes.
     On average, 50% of accesses are local, 50% remote.
     More predictable than first-touch. No thread is permanently slow.
     THIS IS WHAT MOST DATABASE GUIDES RECOMMEND.

     PostgreSQL: numactl --interleave=all pg_ctl start ...
     MySQL:      numactl --interleave=all mysqld_safe ...
     Oracle:     _enable_NUMA_optimization = TRUE (11g+)
                 Oracle does its own NUMA-aware allocation internally.

  3. MEMBIND (numactl --membind=0,1):
     Restrict allocation to specific nodes. Used when partitioning
     buffer pool across NUMA nodes manually.

  4. DATABASE-LEVEL NUMA AWARENESS:
     Some databases manage NUMA themselves:

     SQL Server:
       Automatically creates one buffer pool per NUMA node.
       Memory allocated local to each node.
       Worker threads prefer their local NUMA node's buffer pool.
       sys.dm_os_memory_nodes shows per-node allocation.

     Oracle:
       NUMA-aware SGA allocation since 11g.
       Distributes buffer cache, shared pool, redo log buffer
       across NUMA nodes proportionally.

     PostgreSQL:
       No built-in NUMA awareness (as of PG 17).
       Relies on OS-level interleave policy.
       Community discussions about NUMA-local buffer pool partitioning.
```

---

## 8. Database Latch Designs

### 8.1 Test-And-Set Spinlock (Simplest)

```c
// The simplest possible latch: a single byte
struct TASLatch {
    std::atomic<uint8_t> locked;  // 1 byte!
};

void lock(TASLatch* l) {
    while (l->locked.exchange(1, std::memory_order_acquire) != 0) {
        // Spin until we get it
        while (l->locked.load(std::memory_order_relaxed) != 0) {
            _mm_pause();  // x86 PAUSE instruction — critical for perf
        }
        // Re-attempt the exchange only when we see it's free
        // This is "test-and-test-and-set" (TTAS) optimization
    }
}

void unlock(TASLatch* l) {
    l->locked.store(0, std::memory_order_release);
}
```

```
TAS vs TTAS — why the inner load matters:

  TAS (naive):
    Thread 0: XCHG → fail → XCHG → fail → XCHG → fail → ...
    Each XCHG generates cache invalidation to ALL other cores.
    Under contention: storm of invalidations, bus saturation.

  TTAS (test-and-test-and-set):
    Thread 0: load → locked → load → locked → load → FREE! → XCHG → success
    Loads while spinning READ from local cache (S state).
    No invalidations generated. Only the final XCHG causes traffic.
    Dramatically better under contention.

    ┌─────────────────────────────────────────┐
    │  Throughput (millions of lock/unlock/s)  │
    │                                          │
    │  16 │          ·                          │
    │     │        ·   · TTAS                   │
    │  12 │      ·       ·                      │
    │     │    ·           ·                     │
    │   8 │  ·               ·                   │
    │     │·                   ·                  │
    │   4 │  ·                   ·  TAS           │
    │     │    ·  ·  ·  ·  ·  ·  ·               │
    │   0 └────────────────────────────────────  │
    │     1   4   8   16  32  64  128  threads   │
    └─────────────────────────────────────────┘
```

**Problem with TAS/TTAS:** Unfair. No ordering guarantee. A thread that just released and re-acquires can starve threads that have been spinning longer ("lock starvation").

### 8.2 Ticket Lock (Fair)

```c
struct TicketLatch {
    std::atomic<uint16_t> next_ticket;    // 2 bytes
    std::atomic<uint16_t> now_serving;    // 2 bytes
};  // Total: 4 bytes

void lock(TicketLatch* l) {
    uint16_t my_ticket = l->next_ticket.fetch_add(1, std::memory_order_relaxed);
    while (l->now_serving.load(std::memory_order_acquire) != my_ticket) {
        _mm_pause();
    }
}

void unlock(TicketLatch* l) {
    l->now_serving.fetch_add(1, std::memory_order_release);
}
```

```
Ticket Lock Behavior:

  Thread A: Takes ticket 0, enters (now_serving = 0)
  Thread B: Takes ticket 1, spins on now_serving
  Thread C: Takes ticket 2, spins on now_serving
  Thread D: Takes ticket 3, spins on now_serving

  Thread A unlocks: now_serving = 1 → Thread B enters (FIFO order!)
  Thread B unlocks: now_serving = 2 → Thread C enters

  Fairness: PERFECT FIFO ordering. No starvation possible.

  Problem: ALL spinning threads watch the SAME cache line (now_serving).
  When it changes, ALL spinners get a cache invalidation simultaneously.
  This is the "thundering herd" problem — O(N) cache traffic per release.

  For databases with 64+ cores: this becomes a real bottleneck.
```

### 8.3 MCS Lock (Scalable, Fair)

The MCS lock (Mellor-Crummey & Scott, 1991) solves the thundering herd problem. Each thread spins on its **own** cache line.

```c
struct MCSNode {
    std::atomic<MCSNode*> next;     // 8 bytes
    std::atomic<bool> locked;        // 1 byte (+ padding to cache line)
};  // Padded to 64 bytes (one cache line)

struct MCSLatch {
    std::atomic<MCSNode*> tail;      // 8 bytes (the latch itself)
};

void lock(MCSLatch* l, MCSNode* me) {
    me->next.store(nullptr, std::memory_order_relaxed);
    me->locked.store(true, std::memory_order_relaxed);

    MCSNode* prev = l->tail.exchange(me, std::memory_order_acquire);
    if (prev != nullptr) {
        // Queue behind predecessor
        prev->next.store(me, std::memory_order_release);
        // Spin on OUR OWN flag (local spinning!)
        while (me->locked.load(std::memory_order_acquire)) {
            _mm_pause();
        }
    }
}

void unlock(MCSLatch* l, MCSNode* me) {
    MCSNode* next = me->next.load(std::memory_order_relaxed);
    if (next == nullptr) {
        // Try to set tail to nullptr (we might be the last)
        MCSNode* expected = me;
        if (l->tail.compare_exchange_strong(expected, nullptr,
                std::memory_order_release)) {
            return;  // We were the last; lock is free
        }
        // Someone is in the process of enqueuing behind us; wait
        while ((next = me->next.load(std::memory_order_relaxed)) == nullptr) {
            _mm_pause();
        }
    }
    // Wake up next thread by setting their flag
    next->locked.store(false, std::memory_order_release);
}
```

```
MCS Lock Operation:

  State: tail ──► null  (lock is free)

  Thread A acquires:
    tail ──► [A: locked=false, next=null]
    A enters critical section immediately.

  Thread B tries to acquire:
    tail ──► [B: locked=true, next=null]
    A.next ──► B
    B spins on B.locked (its OWN cache line!)

  Thread C tries to acquire:
    tail ──► [C: locked=true, next=null]
    B.next ──► C
    C spins on C.locked (its OWN cache line!)

  A unlocks:
    A sets B.locked = false → B wakes up and enters
    (Only B's cache line is invalidated, C is NOT disturbed)

  Thread A    Thread B    Thread C
  ┌───────┐  ┌───────┐  ┌───────┐
  │next: B│─►│next: C│─►│next: ─│
  │lock: F│  │lock: T│  │lock: T│   T = spinning, F = free
  └───────┘  └───────┘  └───────┘
                 │            │
           spins here    spins here
         (local cache)  (local cache)

  KEY ADVANTAGE: Each unlock causes EXACTLY ONE cache invalidation,
  regardless of how many threads are waiting. O(1) vs O(N).
```

**Downside:** Each thread needs to allocate/provide an MCSNode (typically on the stack or in a per-thread area). The latch is just a pointer, but the full node is 64 bytes per waiter. Databases often use a thread-local pool of MCS nodes.

### 8.4 CLH Lock (Alternative Scalable Lock)

```
CLH Lock (Craig, Landin, Hagersten):
  Similar to MCS but threads spin on the PREDECESSOR's node instead
  of their own. The queue is implicit (linked backward).

  MCS: me.locked is set by predecessor → I spin on me.locked
  CLH: pred.locked is set by pred    → I spin on pred.locked

  CLH is slightly more cache-friendly for NUMA because after
  acquiring, you can reuse the predecessor's node for the next
  operation (the predecessor has moved on).

  Used less often in databases than MCS because the "spin on
  predecessor" pattern is harder to adapt for reader-writer variants.
```

### 8.5 Spin-Then-Yield / Spin-Then-Sleep (Hybrid)

Most production databases use a hybrid approach rather than pure spinning:

```
Typical database latch acquisition strategy:

  Phase 1: SPIN (optimistic, stay on CPU)
  ─────────────────────────────────────────
    for i in 0..SPIN_COUNT:       // SPIN_COUNT is typically 100-1000
        if try_lock() succeeds:
            return SUCCESS
        _mm_pause()               // save power, hint to CPU

  Phase 2: YIELD (polite, give up time slice)
  ─────────────────────────────────────────────
    for i in 0..YIELD_COUNT:      // YIELD_COUNT is typically 10-50
        sched_yield()             // let other threads run
        if try_lock() succeeds:
            return SUCCESS

  Phase 3: SLEEP (last resort, block)
  ─────────────────────────────────────
    enqueue self on latch wait queue
    futex(FUTEX_WAIT) or park()   // go to sleep
    // woken up by unlock()

Why this works well:
  - Very short holds (common): resolved in Phase 1 (nanoseconds)
  - Medium holds (uncommon): resolved in Phase 2 (microseconds)
  - Long holds (rare/contended): Phase 3 avoids wasting CPU

Tuning SPIN_COUNT:
  Too low:  threads sleep too eagerly → unnecessary context switches
  Too high: threads waste CPU spinning → bad for mixed workloads

  Adaptive spinning (used by HotSpot JVM and some databases):
    - Track how often spinning succeeds vs fails per latch
    - Increase SPIN_COUNT for latches where spinning usually wins
    - Decrease SPIN_COUNT for latches where we usually end up sleeping
```

---

## 9. Reader-Writer Latches

Database workloads are heavily read-biased. A B+Tree traversal for a SELECT takes shared (read) latches on every node; only INSERT/UPDATE/DELETE take exclusive (write) latches. Reader-writer latches exploit this asymmetry.

### 9.1 Basic Design: Counter-Based RW Latch

```c
struct RWLatch {
    std::atomic<uint32_t> state;  // 4 bytes total
    //
    // Bit layout:
    //   bit 31:    EXCLUSIVE flag (1 = writer holds it)
    //   bit 30:    WRITER_WAITING flag (1 = writer wants it)
    //   bits 0-29: reader count (supports up to ~1 billion concurrent readers)
};

#define EXCLUSIVE   (1U << 31)
#define WANT_WRITE  (1U << 30)
#define READER_MASK 0x3FFFFFFFU

void read_lock(RWLatch* l) {
    while (true) {
        uint32_t s = l->state.load(std::memory_order_relaxed);
        // Can acquire if no writer holds AND no writer waiting
        // (writer-preference to avoid starvation)
        if ((s & (EXCLUSIVE | WANT_WRITE)) == 0) {
            if (l->state.compare_exchange_weak(s, s + 1,
                    std::memory_order_acquire)) {
                return;  // Got read latch
            }
        } else {
            _mm_pause();  // Writer active or waiting; back off
        }
    }
}

void read_unlock(RWLatch* l) {
    l->state.fetch_sub(1, std::memory_order_release);
}

void write_lock(RWLatch* l) {
    // First: signal intent to write (blocks new readers)
    while (true) {
        uint32_t s = l->state.load(std::memory_order_relaxed);
        if ((s & EXCLUSIVE) == 0) {
            if (l->state.compare_exchange_weak(s, s | WANT_WRITE,
                    std::memory_order_relaxed)) {
                break;
            }
        } else {
            _mm_pause();
        }
    }
    // Second: wait for existing readers to drain
    while (true) {
        uint32_t s = l->state.load(std::memory_order_relaxed);
        if ((s & READER_MASK) == 0) {
            if (l->state.compare_exchange_weak(s, EXCLUSIVE,
                    std::memory_order_acquire)) {
                return;  // Got exclusive latch
            }
        }
        _mm_pause();
    }
}

void write_unlock(RWLatch* l) {
    l->state.store(0, std::memory_order_release);
}
```

### 9.2 Fairness Policies

```
┌──────────────────────────────────────────────────────────────────────────┐
│                   READER-WRITER FAIRNESS POLICIES                        │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  READER-PREFERENCE (reader-biased):                                      │
│  ──────────────────────────────────                                      │
│  New readers can always acquire if no writer HOLDS the latch.            │
│  Writers can be starved if readers keep arriving.                        │
│  Used when: read latency matters more than write latency.                │
│  Example: catalog cache lookups (reads vastly outnumber DDL writes)      │
│                                                                          │
│  WRITER-PREFERENCE (writer-biased):                                      │
│  ──────────────────────────────────                                      │
│  Once a writer is WAITING, new readers are blocked.                      │
│  Existing readers drain, then writer enters.                             │
│  Readers can be briefly starved during write bursts.                     │
│  Used when: writers must make progress (WAL flush, dirty page write)     │
│  This is the most common choice in databases.                            │
│                                                                          │
│  FAIR / PHASE-FAIR:                                                      │
│  ──────────────────                                                      │
│  Requests are served in arrival order (like ticket lock).                │
│  Alternates between reader "phases" and writer "phases."                 │
│  Most predictable latency, slightly lower peak throughput.               │
│  Used when: worst-case latency guarantees matter.                        │
│                                                                          │
│  WHICH DATABASES USE WHAT:                                               │
│  ─────────────────────────                                               │
│  PostgreSQL LWLock:     Writer-preference (writers set LW_FLAG_HAS_WAITERS)│
│  MySQL/InnoDB rw_lock:  Writer-preference (with configurable spin count) │
│  SQL Server latch:      Phase-fair (SOS scheduler integration)           │
│  WiredTiger (MongoDB):  Writer-preference rwlock                         │
│  RocksDB:               pthread_rwlock or custom port::RWMutex           │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 9.3 The Reader Scalability Problem

Even well-designed RW latches have a hidden scalability ceiling:

```
Problem: READER COUNTER IS A SHARED CACHE LINE

  64 cores all acquire a shared read latch:

    Core  0: FAA(+1) on state → cache line bounces to Core 0    (~100 ns)
    Core  1: FAA(+1) on state → cache line bounces to Core 1    (~100 ns)
    ...
    Core 63: FAA(+1) on state → cache line bounces to Core 63   (~100 ns)

    Total cost: 64 × 100 ns = 6.4 μs of serialized atomic operations!

    This is READ contention — even with no writers, readers serialize
    on the atomic counter increment.

Solutions:

  1. DISTRIBUTED COUNTERS (used by Linux kernel rw_semaphore):
     ┌──────────────────────────────────────────────────────┐
     │  Per-CPU reader counters:                             │
     │  Core 0: counter[0]++  (local cache, no bouncing!)   │
     │  Core 1: counter[1]++  (local cache, no bouncing!)   │
     │  ...                                                  │
     │  Writer must sum ALL per-CPU counters to check drain  │
     │  Write path is slower, read path is vastly faster     │
     │  Trade-off: good when reads >> writes                 │
     └──────────────────────────────────────────────────────┘

  2. OPTIMISTIC READS (eliminate the counter entirely):
     ┌──────────────────────────────────────────────────────┐
     │  Reader does NOT acquire a latch at all.              │
     │  Instead: read a version number, do work, re-check.  │
     │  No atomic increments on the read path.               │
     │  Zero cache line contention for readers.              │
     │  This is the modern approach — see Section 7.          │
     └──────────────────────────────────────────────────────┘
```

---

## 10. Optimistic Latching and OLC

Optimistic Latch Coupling (OLC) is the dominant modern approach for B-tree latching. It avoids acquiring any latch for read operations.

### 10.1 The Core Idea

```
Traditional (pessimistic) B-tree read:
  Lock root (shared) → read → lock child (shared) → unlock root → read →
  lock grandchild (shared) → unlock child → ...
  EVERY node: atomic FAA to increment reader count.

Optimistic B-tree read:
  Read root version → read root → read child version → read child → ...
  At each step: just LOAD a version counter (plain memory read!).
  No atomic RMW (read-modify-write) instructions AT ALL on the read path.

Why this is revolutionary:
  Plain loads are local cache hits (S state). No invalidations.
  100 readers on 100 cores can all read the same node simultaneously
  with ZERO interference. The cache line is Shared on every core.
```

### 10.2 Optimistic Lock Coupling (OLC) Implementation

```c
struct OLCLatch {
    std::atomic<uint64_t> version_lock;  // 8 bytes
    //
    // Bit layout:
    //   bit 0:     LOCKED flag (1 = exclusively locked)
    //   bits 1-63: version counter (incremented on every write)
    //
    // Even version + unlocked = consistent readable state
    // Odd version or locked   = being modified, retry
};

// Reader: No locking! Just version validation.
uint64_t read_begin(OLCLatch* l) {
    uint64_t v;
    while ((v = l->version_lock.load(std::memory_order_acquire)) & 1) {
        _mm_pause();  // wait if locked
    }
    return v;  // return the version to validate later
}

bool read_validate(OLCLatch* l, uint64_t start_version) {
    // Compiler fence to prevent reads from being reordered past this
    std::atomic_thread_fence(std::memory_order_acquire);
    return l->version_lock.load(std::memory_order_relaxed) == start_version;
}

// Writer: Takes exclusive lock, increments version on unlock.
void write_lock(OLCLatch* l) {
    while (true) {
        uint64_t v = l->version_lock.load(std::memory_order_relaxed);
        if ((v & 1) == 0) {  // not locked
            if (l->version_lock.compare_exchange_weak(v, v | 1,
                    std::memory_order_acquire)) {
                return;
            }
        }
        _mm_pause();
    }
}

void write_unlock(OLCLatch* l) {
    // Increment version by 2 (keeps bit 0 = 0, i.e., unlocked)
    // version goes from v|1 to (v+2)|0
    l->version_lock.fetch_add(1, std::memory_order_release);
    // Wait — this changes locked=1 to locked=0 AND increments version
    // Actually: store(version_lock + 1) which clears the lock bit and bumps version
    // Let me be precise:
}

// More precisely:
void write_unlock_v2(OLCLatch* l) {
    // Current value has bit 0 = 1 (locked). Adding 1 clears bit 0
    // and increments the version portion.
    l->version_lock.store(
        l->version_lock.load(std::memory_order_relaxed) + 1,
        std::memory_order_release
    );
}
```

### 10.3 OLC B-Tree Traversal

```
Optimistic Latch Coupling B-Tree Search (used in modern databases):

  search(key):
    node = root
    version = read_begin(node.latch)

    while node is not leaf:
        child = find_child(node, key)       // read node without holding lock
        child_version = read_begin(child.latch)

        if NOT read_validate(node.latch, version):
            RESTART from root               // node was modified; retry

        node = child
        version = child_version

    result = search_leaf(node, key)
    if NOT read_validate(node.latch, version):
        RESTART from root                   // leaf was modified; retry

    return result

  INSERT/UPDATE (write path) — only locks the leaf:
    1. Traverse optimistically (same as read, no locks)
    2. At leaf: write_lock(leaf.latch)
    3. Re-validate parent version (ensure traversal was correct)
    4. If validation fails: unlock, restart
    5. If validation succeeds: modify leaf, write_unlock
    6. If leaf splits: lock parent, then handle split
       (this is the rare slow path — called "pessimistic restart")

Performance characteristics:
┌────────────────────────────────────────────────────────────┐
│  Workload             Traditional RW    OLC                │
│  ──────────────────────────────────────────────────────────│
│  100% reads           Good              PERFECT            │
│                       (RW contention    (zero contention,  │
│                        on counters)      plain loads only)  │
│  95% read / 5% write  Good              Excellent          │
│  50% read / 50% write Decent            Good               │
│  100% writes          Best (no retries) Slightly worse     │
│                                          (some retries)     │
│  ──────────────────────────────────────────────────────────│
│  Retries add overhead, but are rare:                        │
│  With 5% writers, retry rate is typically <0.1% of reads   │
└────────────────────────────────────────────────────────────┘
```

### 10.4 Hybrid Approaches: Optimistic + Pessimistic

```
Many modern systems use a two-phase approach:

  Phase 1: OPTIMISTIC
    - Try the operation with OLC (no locks, just version checks)
    - Works 99%+ of the time for reads
    - Works ~95% of the time for writes (if no structural changes)

  Phase 2: PESSIMISTIC FALLBACK
    - If optimistic attempt fails (version mismatch, split needed):
    - Restart with traditional lock coupling (latch parent, then child)
    - Guaranteed to succeed, but slower

  This is used by:
    - Bw-Tree (Microsoft Hekaton) — lock-free optimistic always
    - ART (Adaptive Radix Tree) with OLC — used in HyPer/Umbra
    - OpenBw-Tree — OLC variant
    - Many modern B+Tree implementations in research databases
```

---

## 11. Lock-Free and Wait-Free Data Structures

Some databases go further and eliminate latches entirely for certain data structures.

### 11.1 Definitions

```
┌─────────────────────────────────────────────────────────────────────────┐
│  PROGRESS GUARANTEES (from weakest to strongest):                       │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  OBSTRUCTION-FREE:                                                      │
│  A thread will complete in a finite number of steps IF it runs alone.  │
│  If threads contend, nobody is guaranteed to make progress.            │
│  (Weakest useful guarantee. Rarely targeted in practice.)              │
│                                                                         │
│  LOCK-FREE:                                                             │
│  At least ONE thread makes progress in a finite number of steps,       │
│  regardless of other threads' behavior (even if they crash/pause).     │
│  Individual threads may starve, but the SYSTEM always makes progress.  │
│  Implementation: CAS retry loops.                                       │
│  Used by: most "lock-free" database structures.                         │
│                                                                         │
│  WAIT-FREE:                                                             │
│  EVERY thread completes in a bounded number of steps.                  │
│  No thread can be starved, not even under worst-case scheduling.       │
│  Implementation: helping protocols (if I'm slow, others help me).      │
│  Extremely hard to implement. Rarely used in practice.                 │
│                                                                         │
│  PRACTICAL REALITY:                                                     │
│  ─────────────────                                                      │
│  "Lock-free" in database marketing often means "latch-free" —          │
│  i.e., uses CAS loops instead of traditional mutexes, but still has    │
│  CAS retry loops that can theoretically spin indefinitely under        │
│  contention. True lock-freedom (system-wide progress guarantee) is     │
│  what matters, and most CAS-loop designs provide it.                   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 11.2 Lock-Free Linked List (Foundation for Many Structures)

```
Harris's Lock-Free Linked List (2001):

  Key insight: Use the LOWEST BIT of the next pointer as a "logically
  deleted" mark. Since pointers are aligned to at least 2 bytes, bit 0
  is always 0 in a valid pointer. Setting bit 0 = 1 means "this node
  is marked for deletion."

  Structure:
    Node: [key | value | next_ptr (with mark bit)]

  Search: Follow next pointers, skip marked nodes.
  Insert: CAS the next pointer of the predecessor.
  Delete: Two-phase:
    1. MARK: CAS next pointer to set the mark bit (logical delete)
    2. UNLINK: CAS predecessor's next to skip marked node (physical delete)

  Why two phases?
    If we just CAS the predecessor's next pointer:
      Thread A: deleting node B, about to CAS pred.next = B.next
      Thread B: inserting node C after node B, CAS B.next = C
      Thread A: CAS pred.next = B.next (OLD value, missing C!)
      Node C is LOST — a correctness bug.

    With marking:
      Thread A: marks B (sets B.next mark bit)
      Thread B: tries to insert after B, sees mark → restarts
      Thread A: unlinks B → safe
```

### 11.3 Lock-Free Hash Table

```
Lock-Free Split-Ordered Hash Table (Shalev & Shavit):

  Used as the basis for concurrent hash maps in several databases.

  Key ideas:
    1. Bucket array grows by splitting (like linear hashing)
    2. All elements live in a single lock-free sorted linked list
    3. Bucket array entries are "sentinel nodes" in the list
    4. Resizing doesn't move any nodes — just adds new sentinels

  Alternative: Lock-free cuckoo hashing
    - Two hash functions, two tables
    - Lookup: check two positions (always O(1))
    - Insert: CAS into position, displace on collision
    - Used in some network-oriented databases for session tables

  In practice, many databases use SHARDED HASH TABLES instead:
    - Partition into 64-256 buckets, each with its own latch
    - Simpler to implement, debug, and reason about
    - Good enough for most workloads (contention is spread across shards)
    - PostgreSQL: 128 buffer mapping partitions
    - MySQL/InnoDB: adaptive hash index with configurable partitions
```

### 11.4 The Bw-Tree (Lock-Free B-Tree)

```
Bw-Tree (Microsoft Research, 2013):
Used in SQL Server Hekaton (In-Memory OLTP) and Azure Cosmos DB.

Core idea: Instead of modifying pages in-place (which requires latches),
prepend "delta records" to a chain. Periodically consolidate.

  Mapping Table:
  ┌─────────┬──────────────┐
  │ Page ID  │  Pointer      │     Logical-to-physical indirection.
  │    0     │  ──────►      │     Every page access goes through this.
  │    1     │  ──────►      │     This is the KEY innovation.
  │    2     │  ──────►      │     All structural modifications are
  │   ...    │  ...          │     a single CAS on this table.
  └─────────┴──────────────┘

  Page with delta chain:

  Mapping Table[5] ──► [Delta: insert K=42]
                           │
                           ▼
                       [Delta: delete K=17]
                           │
                           ▼
                       [Base Page: K=10, K=17, K=25, K=30]

  INSERT K=42:
    1. Allocate delta record: {type: INSERT, key: 42, value: ...}
    2. Set delta.next = mapping_table[page_id]
    3. CAS(mapping_table[page_id], old_ptr, &delta)
    4. If CAS fails: retry (someone else modified this page)
    5. If CAS succeeds: done! No latch was held at any point.

  CONSOLIDATION (when delta chain gets too long):
    1. Create new base page with all deltas applied
    2. CAS(mapping_table[page_id], old_chain, new_base_page)
    3. Old chain + old base page → garbage collection (epoch-based)

  PAGE SPLIT:
    1. Create new right page with half the keys
    2. Create "split delta" record on old page (indicates split boundary)
    3. CAS split delta onto old page's chain
    4. Create "index entry delta" on parent to add pointer to new page
    5. CAS index entry delta onto parent's chain
    All done with CAS operations — no latches!

Trade-offs:
  + Completely latch-free (excellent for many-core machines)
  + No reader blocking (readers just follow the chain)
  − Delta chains add overhead to reads (must traverse chain)
  − Consolidation and garbage collection are complex
  − Mapping table indirection is an extra cache miss per page access
  − In benchmarks, often similar performance to well-tuned OLC B-trees
```

---

## 12. Epoch-Based Reclamation

Lock-free data structures face a critical problem: when can you safely free memory that was unlinked from a concurrent data structure? A reading thread might still be accessing it.

### 12.1 The Problem

```
The ABA Problem and Deferred Reclamation:

  Thread 1                          Thread 2
  ─────────                         ─────────
  Read node A, gets pointer P
  (context switch, paused)
                                    Delete node A, free memory at P
                                    Allocate new node B at same address P
                                    Insert node B into structure
  Resume, uses pointer P
  Thinks it's still node A — BUG!
  (data corruption, crash, security vulnerability)

This is why you CANNOT just call free() after removing a node
from a lock-free structure. You must use SAFE MEMORY RECLAMATION.
```

### 12.2 Epoch-Based Reclamation (EBR)

```
Epoch-Based Reclamation (used by Silo, LMDB, many modern databases):

  Global epoch counter: E = 0, 1, 2 (cycles through values)
  Per-thread epoch: each thread records which epoch it's in
  Garbage lists: one per epoch

  ┌────────────────────────────────────────────────────────────┐
  │  Global Epoch: 2                                           │
  │                                                            │
  │  Thread 0: entered at epoch 2  [active]                    │
  │  Thread 1: entered at epoch 2  [active]                    │
  │  Thread 2: entered at epoch 1  [active — lagging!]         │
  │  Thread 3: not in any epoch    [idle]                       │
  │                                                            │
  │  Garbage lists:                                             │
  │    Epoch 0: [node X, node Y]  ← safe to free if no thread │
  │                                  is in epoch 0              │
  │    Epoch 1: [node Z]          ← NOT safe (Thread 2 is      │
  │                                  still in epoch 1)          │
  │    Epoch 2: [node W]          ← definitely not safe        │
  └────────────────────────────────────────────────────────────┘

  Algorithm:
    1. Thread enters epoch: my_epoch = global_epoch
    2. Thread does work (reads, inserts, deletes)
    3. When deleting: add freed node to garbage list for current epoch
    4. Thread exits epoch: my_epoch = INACTIVE

    Periodically (any thread can do this):
    5. Try to advance global epoch:
       - Check: are ALL threads either inactive or in current epoch?
       - If yes: advance global_epoch++
       - Old garbage (two epochs ago) is now safe to free

  Why TWO epochs behind is safe:
    - Epoch advances only when all threads have caught up
    - A node freed in epoch E-2 was already unlinked in epoch E-2
    - All threads have since exited and re-entered at epoch ≥ E-1
    - Therefore, no thread can hold a reference to nodes from epoch E-2
```

### 12.3 Alternatives to EBR

```
┌──────────────────────────────────────────────────────────────────────────┐
│              SAFE MEMORY RECLAMATION TECHNIQUES                          │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  EPOCH-BASED (EBR):                                                      │
│  Pros: Very fast read-side (just write thread-local epoch)               │
│  Cons: One slow/stalled thread blocks ALL reclamation                    │
│  Used by: Silo, Bw-Tree, many research databases                        │
│                                                                          │
│  HAZARD POINTERS (Maged Michael, 2004):                                  │
│  Each thread publishes pointers it's currently accessing.                │
│  Reclaimers scan hazard pointers before freeing.                         │
│  Pros: No global epoch → stalled thread only protects its own nodes     │
│  Cons: Read-side overhead (must publish pointers, memory fence)          │
│  Used by: ConcurrentHashMap in some implementations                      │
│                                                                          │
│  QUIESCENT-STATE BASED RECLAMATION (QSBR):                              │
│  Similar to EBR but threads explicitly signal "quiescent states"        │
│  (points where they hold no references).                                 │
│  Pros: Fastest read path (no per-access overhead)                        │
│  Cons: Requires cooperative threads that regularly reach quiescent states│
│  Used by: Linux kernel RCU (Read-Copy-Update)                            │
│                                                                          │
│  READ-COPY-UPDATE (RCU):                                                 │
│  Linux kernel's approach. Readers run in "RCU read sections"            │
│  with zero overhead. Writers copy-then-update; old copies freed         │
│  after a "grace period" (all CPUs have been through a quiescent state). │
│  Pros: Absolute fastest read side, proven at scale                       │
│  Cons: Kernel integration needed, writers may wait long grace periods    │
│  Adapted for userspace: liburcu (used by LTTng, some storage engines)   │
│                                                                          │
│  HYALINE (2020) / NBR (2021):                                           │
│  Modern alternatives that bound reclamation delay better than EBR.       │
│  Research-grade; not yet widely deployed in production databases.         │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 13. Latch Implementations in Production Databases

### 13.1 PostgreSQL: LWLock (Lightweight Lock)

```
PostgreSQL LWLock Architecture:

  PostgreSQL calls its latches "LWLocks" (lightweight locks).
  These are DISTINCT from "heavyweight locks" (the lock manager for
  row/table locks that users see in pg_locks).

  Implementation (src/backend/storage/lmgr/lwlock.c):

  struct LWLock {
      pg_atomic_uint32 state;     // 4 bytes
      proclist_head waiters;      // wait queue (linked list of PGPROCs)
      // ... tranche ID for debugging
  };

  State bits:
    LW_FLAG_HAS_WAITERS (1 << 30):  someone is on the wait queue
    LW_FLAG_RELEASE_OK  (1 << 29):  OK to release waiters
    LW_FLAG_LOCKED      (1 << 28):  exclusive mode held
    Bits 0-23: number of shared lockers (up to 16M)

  Acquisition strategy:
    1. Atomic CAS attempt (fast path)
    2. Spin loop: NUM_DELAYS iterations (starts at 0, doubles each retry)
       Each delay: pg_spin_delay() which calls _mm_pause() in a loop
       with random backoff
    3. If still contended: add self to wait queue, go to sleep
       (waits on per-backend semaphore or condition variable)

  KEY DESIGN CHOICES:
    - Writer-preference: once HAS_WAITERS is set, new readers may
      be blocked (depends on the specific LWLock tranche)
    - Partitioned: many LWLock "tranches" for different subsystems
      (buffer mapping, WAL insert, CLOG, proc array, etc.)
    - Diagnostic: each LWLock has a tranche ID so wait events show up
      as "LWLock:BufferMapping" or "LWLock:WALInsert" in pg_stat_activity

  Notable LWLock tranches in PostgreSQL:
  ┌──────────────────────────┬──────────────────────────────────────┐
  │  Tranche                 │  What it protects                     │
  ├──────────────────────────┼──────────────────────────────────────┤
  │  BufferMapping           │  Buffer pool page table (128 partns) │
  │  BufferContent           │  Individual buffer page content       │
  │  WALInsert               │  WAL buffer insertion (8 partitions)  │
  │  WALWrite                │  WAL file writing                     │
  │  CLogTruncation          │  Commit log truncation                │
  │  ProcArray               │  Transaction ID array                 │
  │  XidGen                  │  Transaction ID generation             │
  │  RelCacheInit            │  Relation cache initialization        │
  │  SerializableXact        │  Serializable transaction tracking    │
  │  AutovacuumSchedule      │  Autovacuum scheduling                │
  └──────────────────────────┴──────────────────────────────────────┘
```

### 13.2 MySQL/InnoDB: rw_lock_t and Custom Mutexes

```
InnoDB Latch Architecture (storage/innobase/include/sync0rw.h):

  InnoDB has gone through several latch implementations:

  EVOLUTION:
    MySQL 5.5: OS mutex (pthread_mutex) + custom rw_lock
    MySQL 5.6: Custom spin-based mutex (TTASEventMutex)
    MySQL 5.7: Refactored: ib_mutex_t with policy-based design
    MySQL 8.0: Further optimization, removed global mutex_list
    MySQL 8.0.21+: Lock-free hash for AHI (Adaptive Hash Index)

  Current rw_lock_t (simplified):
  struct rw_lock_t {
      volatile lint lock_word;  // Atomic counter
      // Positive: available for readers
      // 0: exclusively locked
      // Negative: readers present, value = -num_readers
      // X_LOCK_DECR: marker for exclusive

      volatile uint32_t waiters;  // Are there waiters?
      os_event_t event;            // For blocking
      os_event_t wait_ex_event;    // For exclusive waiters
      // ... debug fields, mutex for the wait list
  };

  SPIN STRATEGY:
    srv_n_spin_wait_rounds = 30  (configurable)
    srv_spin_wait_delay = 6       (microseconds between spins)

    Phase 1: Spin for srv_n_spin_wait_rounds iterations
    Phase 2: os_event_wait() → kernel sleep

  Buffer Pool Mutex Partitioning:
    innodb_buffer_pool_instances = 8 (default for pool > 1GB)
    Each instance has its own mutex.
    Page → instance mapping: hash(space_id, page_no) % instances

  Notable: InnoDB tracks latch ordering in DEBUG builds:
    sync_check_find(latch) verifies that the current thread is not
    violating the latch ordering rules (would cause deadlock).
    The latch levels form a DAG; acquiring a lower-level latch while
    holding a higher-level one is a bug.
```

### 13.3 SQL Server: SOS Scheduler and Spinlocks

```
SQL Server Synchronization Architecture:

  SQL Server runs its own cooperative scheduler called SOS
  (SQL Operating System). This changes the latch story significantly.

  SOS Scheduler:
    - SQL Server does NOT let the OS schedule worker threads freely
    - Each scheduler (typically 1 per CPU core) has a run queue
    - Workers VOLUNTARILY yield at well-defined points
    - No preemption within a scheduler (cooperative multitasking)

  ┌──────────────────────────────────────────────────────────────┐
  │  SOS Scheduler 0 (bound to CPU core 0)                       │
  │                                                               │
  │  Run Queue: [Worker A] → [Worker B] → [Worker C]             │
  │  Running:    Worker A                                          │
  │                                                               │
  │  Worker A runs until:                                          │
  │    1. It voluntarily yields (SwitchContext)                    │
  │    2. It needs to wait for I/O or a latch                     │
  │    3. Its quantum expires (4ms safety net)                     │
  │                                                               │
  │  When Worker A waits on a latch:                               │
  │    → Worker A goes to the waiter list                          │
  │    → Scheduler picks Worker B from run queue                   │
  │    → NO kernel context switch! Just swaps stack pointers.      │
  └──────────────────────────────────────────────────────────────┘

  SQL Server Spinlocks:
    - Used for very short critical sections (< ~20 instructions)
    - Pure spin (no sleep, no scheduler interaction)
    - Examples: LOCK_HASH spinlock (lock manager buckets),
      BUF_HASH spinlock (buffer pool lookup)
    - Exponential backoff: spins 1, 2, 4, 8, ... iterations
    - Monitors: sys.dm_os_spinlock_stats shows contention

  SQL Server Latches:
    - For longer critical sections
    - Integration with SOS scheduler:
      * Spin briefly (configurable)
      * If still contended: enqueue on latch's waiter list
      * SOS scheduler context-switches to another worker (NOT kernel!)
      * When latch is released: waiter is moved to scheduler's run queue
    - Modes: SH, UP, EX, DT (shared, update, exclusive, destroy)
    - Visible: sys.dm_os_latch_stats, sys.dm_os_wait_stats

  Buffer Latch Modes in SQL Server:
  ┌──────────┬──────────────────────────────────────────────────────┐
  │  Mode    │  Purpose                                              │
  ├──────────┼──────────────────────────────────────────────────────┤
  │  SH      │  Read a page (SELECT)                                │
  │  UP      │  Read a page with intent to modify (prevents SH→EX  │
  │          │  upgrade deadlock)                                    │
  │  EX      │  Modify a page (INSERT/UPDATE/DELETE)                │
  │  DT      │  "Destroy" — evict page from buffer pool             │
  │  KP      │  "Keep" — prevent eviction while reading              │
  └──────────┴──────────────────────────────────────────────────────┘
```

### 13.4 WiredTiger (MongoDB): rwlock and Hazard Pointers

```
WiredTiger Synchronization (src/include/mutex.h):

  WiredTiger uses a layered approach:

  1. SPINLOCKS: For very short critical sections
     - TAS-based with backoff
     - Used for: statistics counters, connection-level metadata

  2. READ-WRITE LOCKS: For page access
     - Custom implementation: writer-preference
     - Readers: atomic increment of reader counter
     - Writers: set exclusive flag, wait for readers to drain
     - Diagnostic: configurable spinning via wiredtiger_open config

  3. HAZARD POINTERS: For safe page eviction
     - Each session (thread) has a hazard pointer array
     - Before accessing an in-memory page, session sets a hazard pointer
     - Eviction thread checks ALL sessions' hazard pointers before freeing
     - If any session has a hazard pointer to the page → skip eviction

     ┌────────────────────────────────────┐
     │  Session 0: hazard_ptrs = [P5, P8] │
     │  Session 1: hazard_ptrs = [P5, P12]│
     │  Session 2: hazard_ptrs = [P30]    │
     │                                     │
     │  Eviction wants to free page P5:    │
     │  → Scans all hazard pointers        │
     │  → Finds P5 in Session 0 and 1     │
     │  → CANNOT evict P5, skip it         │
     └────────────────────────────────────┘

  4. CONDITIONAL WAIT (ticket-based):
     - For longer waits (e.g., waiting for checkpoint to finish)
     - pthread_cond_t underneath with ticket numbers for ordering
```

### 13.5 RocksDB: Mutexes and Lock-Free MemTable

```
RocksDB Synchronization Strategy:

  RocksDB takes a pragmatic approach: use OS locks where they're
  good enough, custom structures where critical.

  MUTEX: port::Mutex (wraps pthread_mutex or Windows CRITICAL_SECTION)
    - Used for: DB metadata, compaction scheduling, version management
    - Fine for these because: low frequency, medium hold times

  RW LOCK: port::RWMutex (wraps pthread_rwlock)
    - Used for: read-only table metadata

  LOCK-FREE MEMTABLE (the performance-critical path):
    - SkipList implementation is LOCK-FREE
    - Concurrent inserts without any locks
    - Single writer + multiple readers OR concurrent writers
      depending on configuration
    - InlineSkipList: arena-allocated, cache-friendly

  WRITE PATH:
    - WriteBatch → write queue → single writer serializes WAL writes
    - "Pipelined writes" (since RocksDB 5.5):
      * Writer 1 writes to WAL while Writer 2 builds next batch
      * Reduces serialization bottleneck
    - The WAL write itself uses a mutex (write_mutex_)

  KEY INSIGHT: RocksDB accepts OS-level locks for operations that
  happen thousands of times per second (metadata changes, compaction),
  but uses lock-free structures for operations that happen MILLIONS
  of times per second (MemTable inserts).
```

---

## 14. Modern Approaches and Research

### 14.1 Flat Combining

```
Flat Combining (Hendler, Incze, Shavit, 2010):

  Instead of N threads each fighting for a latch, ONE thread does
  all the work on behalf of everyone.

  How it works:
    1. Each thread posts its operation to a publication list
    2. A "combiner" thread (whoever wins the lock) scans the list
    3. Combiner executes ALL pending operations in one batch
    4. Combiner publishes results; other threads read their results

  ┌──────────────────────────────────────────────────────────────┐
  │  Publication List:                                            │
  │  Thread 0: [INSERT key=42]     → result: OK                  │
  │  Thread 1: [LOOKUP key=17]     → result: value=99            │
  │  Thread 2: [DELETE key=5]      → result: OK                  │
  │  Thread 3: [INSERT key=88]     → result: OK                  │
  │                                                               │
  │  Combiner (Thread 0 won the lock):                            │
  │    1. Acquire lock on data structure                           │
  │    2. Read all 4 operations from publication list              │
  │    3. Execute all 4 operations (single-threaded, no latching!)│
  │    4. Write results back to publication list                   │
  │    5. Release lock                                             │
  │                                                               │
  │  Other threads: spin/sleep on their result slot               │
  └──────────────────────────────────────────────────────────────┘

  Benefits:
    - Data structure is accessed single-threaded → no cache bouncing
    - Combiner can batch and reorder operations for efficiency
    - Excellent for contended data structures with short operations

  Used in practice:
    - WAL buffer writes (similar concept: group commit)
    - Some concurrent queues and stacks
    - Lock manager hash bucket updates
```

### 14.2 NUMA-Aware Latches

```
NUMA-Aware Locking Strategies:

  On multi-socket servers, cache line bouncing across sockets is
  10× more expensive than within a socket.

  Problem:
    ┌───────────────────────────────────────────────────────┐
    │  Socket 0           Interconnect         Socket 1     │
    │  ┌──────────┐      ┌──────────┐      ┌──────────┐   │
    │  │ Core 0-15│◄────►│  QPI /   │◄────►│Core 16-31│   │
    │  │ L1/L2/L3 │      │  UPI     │      │ L1/L2/L3 │   │
    │  └──────────┘      │ ~100 ns  │      └──────────┘   │
    │                     └──────────┘                      │
    │                                                       │
    │  CAS on same socket:   ~20-40 ns                      │
    │  CAS across sockets:   ~100-300 ns  (5-10× slower)   │
    └───────────────────────────────────────────────────────┘

  COHORT LOCKS (Dice, Lev, Marathe, 2012):
    - Per-socket local lock + global lock
    - Threads first acquire their socket's local lock
    - Local lock owner acquires global lock
    - Pass global lock to SAME-SOCKET threads first
    - Cross-socket transfer only when local queue empties
    - Keeps cache lines on one socket as long as possible

  HIERARCHICAL MCS:
    - Two-level MCS: local queue per socket + global queue
    - Local spinning stays on same socket (local cache)
    - Similar principle: batch same-socket acquisitions

  USED BY:
    - Linux kernel (qspinlock since v4.2 — NUMA-aware MCS variant)
    - SQL Server (SOS scheduler is NUMA-aware: schedulers per NUMA node)
    - Oracle (NUMA-aware buffer pool partitioning)
```

### 14.3 Hardware Transactional Memory (HTM)

```
Hardware Transactional Memory (Intel TSX / IBM POWER):

  The CPU itself provides atomic multi-word transactions.

  Intel TSX (Restricted Transactional Memory — RTM):
    _xbegin():   Start hardware transaction
    _xend():     Commit (atomically makes all writes visible)
    _xabort():   Explicitly abort

    All reads/writes between _xbegin and _xend are tracked in L1 cache.
    If no conflict: _xend commits atomically.
    If conflict (another core touches same cache line): hardware aborts,
    all writes are rolled back, execution jumps to fallback path.

  Use as a latch replacement:
    void access_btree_node(Node* n) {
        if (_xbegin() == _XBEGIN_STARTED) {
            // Hardware transaction — no latch needed!
            // Read/modify the node
            // If anyone else touches this node: automatic abort + retry
            _xend();
        } else {
            // Fallback: acquire traditional latch
            latch_lock(n->latch);
            // ... do work ...
            latch_unlock(n->latch);
        }
    }

  CHALLENGES:
    - Intel DISABLED TSX on many CPUs (security vulnerabilities: TAA, ZDI)
    - TSX was removed from 12th-gen Intel (Alder Lake) and later
    - Transactions abort for many reasons: cache capacity, interrupts,
      page faults, certain instructions, system calls
    - Abort rate can be high under real workloads
    - IBM POWER still supports HTM but is niche for databases

  STATUS (2024-2025):
    - TSX is effectively DEAD for new Intel hardware
    - ARM TME (Transactional Memory Extension) is specified but
      not widely implemented
    - Research databases (Silo, DBx1000) showed promising results
      but the hardware disappeared
    - Lesson: don't depend on hardware features that vendors may remove
```

### 14.4 Coroutine-Based Latching

```
Coroutine-Based Cooperative Latching (emerging approach):

  Traditional: Thread blocks on latch → OS deschedules → context switch
  Coroutine:   Coroutine suspends on latch → executor runs another
               coroutine → NO OS context switch, NO kernel involvement

  ┌──────────────────────────────────────────────────────────────┐
  │  Thread 0 running coroutine executor:                         │
  │                                                               │
  │  Coroutine A: B-tree lookup, needs latch on page 42          │
  │    → Page 42 is latched by someone else                       │
  │    → co_await latch(page_42)                                  │
  │    → Coroutine A suspends (saves state to its stack frame)   │
  │                                                               │
  │  Executor immediately picks Coroutine B:                      │
  │    → B-tree lookup on a different page, proceeds              │
  │                                                               │
  │  When page 42's latch is released:                            │
  │    → Coroutine A is added back to executor's ready queue      │
  │    → Executor resumes Coroutine A when convenient             │
  │                                                               │
  │  Total OS threads: equal to number of CPU cores               │
  │  Total coroutines: thousands to millions                      │
  │  Context switch cost: ~10-50 ns (save/restore a few registers)│
  │  vs OS context switch: ~1-5 μs (100× more expensive)          │
  └──────────────────────────────────────────────────────────────┘

  Used by:
    - Umbra (TUM database, successor to HyPer): morsel-driven +
      coroutine-based I/O
    - TigerBeetle: io_uring + coroutines for financial transactions
    - DuckDB: task-based parallelism (not coroutines per se, but
      similar cooperative scheduling)
    - Emerging in PostgreSQL community (discussion for AIO integration)
```

---

## 15. Language-Specific Synchronization Semantics

Different programming languages provide very different abstractions over the same hardware primitives. This affects how databases are built in each language.

### 15.1 C/C++ (The Traditional Database Language)

```
C11/C++11 Memory Model and Atomics:

  Before C11/C++11: No standard memory model. Databases used compiler
  intrinsics (__sync_*, __atomic_*), inline assembly, or platform-specific
  APIs. Portable concurrent code was essentially impossible to write correctly.

  C11/C++11 introduced:
    - std::atomic<T> — atomic types with defined semantics
    - Memory ordering tags — explicit control over reordering

  MEMORY ORDER OPTIONS:
  ┌────────────────────────────┬──────────────────────────────────────────┐
  │  memory_order_relaxed       │  No ordering guarantees.                 │
  │                             │  Only atomicity is guaranteed.           │
  │                             │  Cheapest. Used for counters, statistics.│
  ├────────────────────────────┼──────────────────────────────────────────┤
  │  memory_order_acquire       │  All reads/writes AFTER this load       │
  │  (on loads)                │  cannot be reordered BEFORE it.          │
  │                             │  "Acquire the lock, then read data."    │
  │                             │  On x86: FREE (TSO provides it).        │
  │                             │  On ARM: LDAR instruction.              │
  ├────────────────────────────┼──────────────────────────────────────────┤
  │  memory_order_release       │  All reads/writes BEFORE this store     │
  │  (on stores)               │  cannot be reordered AFTER it.           │
  │                             │  "Write data, then release the lock."   │
  │                             │  On x86: FREE (TSO provides it).        │
  │                             │  On ARM: STLR instruction.              │
  ├────────────────────────────┼──────────────────────────────────────────┤
  │  memory_order_acq_rel       │  Both acquire and release.              │
  │  (on read-modify-write)    │  Used for CAS in lock implementations.  │
  ├────────────────────────────┼──────────────────────────────────────────┤
  │  memory_order_seq_cst       │  Full sequential consistency.           │
  │  (default if not specified)│  Total order across ALL seq_cst ops.    │
  │                             │  On x86: MFENCE or LOCK prefix.         │
  │                             │  On ARM: DMB + LDAR/STLR.              │
  │                             │  EXPENSIVE. Databases avoid this.       │
  └────────────────────────────┴──────────────────────────────────────────┘

  PRACTICAL PATTERN (custom spinlock in C++):

    class SpinLatch {
        std::atomic<bool> flag_{false};
    public:
        void lock() {
            while (flag_.exchange(true, std::memory_order_acquire)) {
                while (flag_.load(std::memory_order_relaxed)) {
                    // TTAS: spin on relaxed load (cheap, local cache)
                    #ifdef __x86_64__
                        _mm_pause();
                    #elif defined(__aarch64__)
                        asm volatile("yield");
                    #endif
                }
            }
        }
        void unlock() {
            flag_.store(false, std::memory_order_release);
        }
    };

  KEY C++ PITFALLS FOR DATABASE ENGINEERS:
    1. volatile != atomic! volatile prevents compiler reordering but
       NOT CPU reordering. Never use volatile for synchronization.
    2. std::mutex uses seq_cst internally. For hot-path latches,
       custom atomics with acquire/release are faster.
    3. Compiler fences (asm volatile("" ::: "memory")) prevent
       COMPILER reordering but NOT CPU reordering. Use atomic fences.
    4. False sharing: two atomics on the same cache line will cause
       unnecessary invalidations. Use alignas(64) or padding.
```

### 15.2 Rust (The Modern Database Language)

```
Rust's Approach to Synchronization:

  Rust's ownership system provides COMPILE-TIME data race prevention.
  This is a fundamental advantage for database development.

  CORE PRINCIPLE: If you can compile it, there are no data races.

  ┌──────────────────────────────────────────────────────────────────┐
  │  Rust Ownership Rules (relevant to concurrency):                  │
  │                                                                   │
  │  1. A value has exactly ONE owner                                 │
  │  2. You can have EITHER:                                          │
  │     - Multiple immutable references (&T)    → like shared latch  │
  │     - ONE mutable reference (&mut T)         → like exclusive latch│
  │     - NOT both at the same time                                   │
  │  3. These rules are enforced at COMPILE TIME                      │
  │                                                                   │
  │  The compiler IS a latch verifier.                                │
  └──────────────────────────────────────────────────────────────────┘

  SYNCHRONIZATION PRIMITIVES:

  std::sync::Mutex<T>:
    - NOT like C++ std::mutex! Rust's Mutex OWNS the data it protects
    - Cannot access data without holding the lock (enforced by types)
    - Lock returns a MutexGuard<T> that auto-unlocks on drop
    - Poisoning: if a thread panics while holding a Mutex, the Mutex
      becomes "poisoned" and future lock() calls return Err
    - Implementation: parking_lot crate is preferred over std
      (smaller: 1 byte vs 40+ bytes, faster, no poisoning)

    let data = Mutex::new(BTreeNode::new());
    {
        let mut guard = data.lock().unwrap();  // acquire
        guard.insert(key, value);               // use
    } // guard dropped here → automatic unlock

  std::sync::RwLock<T>:
    - Reader-writer lock, same ownership semantics
    - read() returns RwLockReadGuard, write() returns RwLockWriteGuard
    - Again, parking_lot::RwLock is preferred (smaller, faster)

  std::sync::atomic (mirrors C++11 atomics):
    - AtomicBool, AtomicU32, AtomicU64, AtomicPtr<T>
    - Same memory ordering options: Relaxed, Acquire, Release, AcqRel, SeqCst
    - Ordering::Relaxed = memory_order_relaxed, etc.

  crossbeam crate (concurrent data structures):
    - crossbeam::epoch: Epoch-based garbage collection for lock-free structures
    - crossbeam::deque: Work-stealing dequeue (for parallel query execution)
    - crossbeam::queue: Lock-free MPMC queue
    - crossbeam::utils::CachePadded<T>: Prevents false sharing

  parking_lot crate (better locks):
  ┌────────────────────────────────────┬───────────────┬───────────────┐
  │  Feature                           │  std::sync     │  parking_lot   │
  ├────────────────────────────────────┼───────────────┼───────────────┤
  │  Mutex size                        │  40 bytes      │  1 byte        │
  │  RwLock size                       │  56 bytes      │  1 byte        │
  │  Condvar size                      │  48 bytes      │  1 byte        │
  │  Poisoning                         │  Yes           │  No             │
  │  Upgradable read locks             │  No            │  Yes            │
  │  Fair (anti-starvation)            │  Varies        │  Yes            │
  │  Adaptive spinning                 │  Varies        │  Yes            │
  │  Lock ordering (try_lock_for)      │  No            │  Yes            │
  └────────────────────────────────────┴───────────────┴───────────────┘

  DATABASES WRITTEN IN RUST:
    - TiKV (PingCAP): Uses parking_lot, crossbeam::epoch
    - SurrealDB: Rust async (tokio), standard locks
    - Neon (Postgres storage): Rust for the storage layer
    - ReadySet (query cache): Lock-free data structures
    - Databend: Modern OLAP, uses tokio + parking_lot
    - GlueSQL: Pure Rust embedded SQL
```

### 15.3 Java (JVM Databases)

```
Java Synchronization Model:

  Java was one of the first mainstream languages with a defined
  memory model (JMM, Java Memory Model — JSR-133, finalized 2004).

  BUILT-IN: synchronized keyword
    synchronized(obj) {
        // Intrinsic lock on obj
        // Automatic acquire on entry, release on exit
        // Reentrant (same thread can re-enter)
    }

  HotSpot JVM Lock Implementation (important for databases!):
  ┌──────────────────────────────────────────────────────────────────┐
  │  BIASED LOCKING (JDK 6-14, deprecated JDK 15, removed JDK 18): │
  │  If only one thread ever locks an object, lock acquisition is    │
  │  just a CAS to write the thread ID into the object header.      │
  │  Subsequent locks by the same thread: just a comparison!         │
  │  Cost: ~1 ns for biased lock, ~5 ns for thin lock.              │
  │  Removed because: revocation pause was unpredictable.            │
  │                                                                   │
  │  THIN LOCK (current fast path, JDK 18+):                        │
  │  CAS on the object's mark word. Similar to a TAS spinlock.       │
  │  Cost: ~5-15 ns uncontended.                                     │
  │                                                                   │
  │  INFLATED LOCK (contended):                                       │
  │  Object's mark word points to an ObjectMonitor (heavy struct).   │
  │  Has entry queue, wait queue, adaptive spinning.                  │
  │  Uses OS parking (futex on Linux) for blocking.                  │
  │  Cost: ~50 ns - several μs.                                      │
  └──────────────────────────────────────────────────────────────────┘

  java.util.concurrent (Doug Lea's masterwork):

  ReentrantLock:
    - Explicit lock/unlock (unlike synchronized which is scoped)
    - Fair mode available (FIFO ordering, ~2× slower)
    - tryLock() with timeout
    - Multiple Condition objects (vs single wait/notify)
    - Implementation: AbstractQueuedSynchronizer (AQS)
      * CLH-variant queue (each waiter spins on predecessor's state)
      * State managed with a single int + CAS

  ReentrantReadWriteLock:
    - Shared/exclusive modes
    - Fair or non-fair
    - Lock downgrade: write → read (supported)
    - Lock upgrade: read → write (NOT supported — deadlocks!)
    - AQS state: high 16 bits = shared count, low 16 bits = exclusive count

  StampedLock (Java 8+):
    - OPTIMISTIC READS — exactly what databases need!
    - tryOptimisticRead() returns a "stamp" (version number)
    - Do reads, then validate(stamp)
    - If valid: no locking overhead at all
    - If invalid: fall back to readLock()
    - This is the Java equivalent of OLC!

    StampedLock sl = new StampedLock();

    // Optimistic read (no lock acquired)
    long stamp = sl.tryOptimisticRead();
    int x = this.x;  // read shared state
    int y = this.y;
    if (!sl.validate(stamp)) {
        // Someone wrote; fall back to real read lock
        stamp = sl.readLock();
        try {
            x = this.x;
            y = this.y;
        } finally {
            sl.unlockRead(stamp);
        }
    }

  java.util.concurrent.atomic:
    - AtomicInteger, AtomicLong, AtomicReference<T>
    - LongAdder: distributed counter (per-cell, like per-CPU counters)
      → Eliminates reader-counter bottleneck in RW locks
    - VarHandle (Java 9+): low-level memory fence control
      * getAcquire(), setRelease(), compareAndExchange()
      * Equivalent to C++ memory_order_acquire/release
      * Used by database library authors who need C++-level control

  JVM DATABASES AND THEIR LOCKING:
    - H2: synchronized + ReentrantReadWriteLock for page access
    - Apache Derby: custom latch manager with compatibility matrix
    - CockroachDB (Go, but Pebble storage in Go): see Go section
    - Apache Cassandra: mostly lock-free (ConcurrentSkipListMap for memtable)
    - Apache Kafka: ReentrantLock for partition logs, lock-free batching
```

### 15.4 Go (Emerging Database Language)

```
Go Synchronization Model:

  Go has a simple concurrency model: goroutines (lightweight threads)
  + channels (typed message passing). For low-level database work,
  it also provides sync primitives.

  GOROUTINE SCHEDULING:
  ┌──────────────────────────────────────────────────────────────────┐
  │  Go runtime multiplexes M goroutines onto N OS threads.          │
  │  Default: N = GOMAXPROCS = number of CPU cores.                  │
  │                                                                   │
  │  G (Goroutine): lightweight, 2KB initial stack (grows as needed) │
  │  M (Machine):   OS thread                                        │
  │  P (Processor): logical processor, holds run queue               │
  │                                                                   │
  │  A goroutine blocking on a mutex does NOT block the OS thread —  │
  │  the runtime parks the goroutine and schedules another.           │
  │  This is similar to SQL Server's SOS scheduler!                  │
  │                                                                   │
  │  Consequence for databases: goroutine-per-connection is fine      │
  │  (unlike thread-per-connection which is expensive).               │
  │  CockroachDB, TiDB, and Dgraph all use goroutine-per-connection. │
  └──────────────────────────────────────────────────────────────────┘

  sync.Mutex:
    - Implementation: hybrid spin + park
    - Normal mode: TTAS-like spinning, then goroutine parking
    - Starvation mode: if a goroutine waits > 1ms, switches to FIFO
      (prevents tail latency spikes)
    - NOT reentrant (acquiring twice = deadlock — by design)
    - No try_lock() (controversial decision — intentional)
      → Go philosophy: if you need try_lock, redesign your concurrency
      → Actually added in Go 1.18 as Mutex.TryLock() after long debate
    - Size: 8 bytes (state int32 + sema uint32)

  sync.RWMutex:
    - Multiple readers OR one writer
    - Writer-preference (once writer calls Lock, new readers block)
    - Size: 40 bytes (writerSem, readerSem, readerCount, readerWait, w Mutex)
    - Implementation:
      * readerCount: atomic counter for readers (can go negative!)
      * When writer calls Lock: atomically subtracts rwmutexMaxReaders
        from readerCount → new readers see negative count → block
      * Writer waits for active readers (tracked by readerWait) to finish
      * On Unlock: adds rwmutexMaxReaders back → readers unblocked

  sync/atomic package:
    - atomic.Int32, atomic.Int64, atomic.Pointer[T] (Go 1.19+)
    - Load, Store, Swap, CompareAndSwap, Add
    - NO explicit memory ordering control!
    - All Go atomic ops are SEQUENTIALLY CONSISTENT
    - This is simpler but prevents optimizations C++/Rust can do:
      * Cannot use relaxed loads for TTAS inner loop
      * Cannot use acquire/release for lock/unlock
      * Every atomic op is a full fence
    - For most databases in Go: this overhead is acceptable
    - For ultra-hot-path code: some projects use unsafe + assembly

  DATABASES WRITTEN IN GO:
    - CockroachDB: sync.Mutex, sync.RWMutex, custom latching for MVCC
      * Pebble (LSM engine): custom internal latches
      * Latch manager for key-span concurrency control
    - TiDB (SQL layer): sync.Mutex, channels for coordination
    - Dgraph: badger (LSM engine) with sync.RWMutex
    - BoltDB/bbolt: single-writer (file lock), readers use mmap
    - Vitess: sync.Mutex for connection pooling, vtgate routing

  GO-SPECIFIC ISSUE: THE RACE DETECTOR
    - go run -race / go test -race
    - Instruments ALL memory accesses at runtime
    - Detects data races with ~10× overhead
    - Critical for database development: catches races that only
      manifest under production load
    - Equivalent to C++ ThreadSanitizer (TSan) but easier to use
```

---

## 16. Diagnosing Latch Contention in Production

### 16.1 Wait Event Monitoring

```
PostgreSQL:
──────────────
  SELECT wait_event_type, wait_event, count(*)
  FROM pg_stat_activity
  WHERE wait_event_type = 'LWLock'
  GROUP BY 1, 2 ORDER BY 3 DESC;

  Common results:
    LWLock | WALInsert      | 12    ← WAL insertion bottleneck
    LWLock | BufferMapping  | 8     ← buffer pool hash contention
    LWLock | lock_manager   | 5     ← lock manager internal latch
    LWLock | BufferContent  | 3     ← page-level read/write contention

  Fixes by wait event:
    WALInsert:      Increase wal_buffers, faster WAL disk, full_page_writes=off
    BufferMapping:  Increase shared_buffers (fewer evictions)
    BufferContent:  Partition hot tables, reduce long-running queries

MySQL/InnoDB:
──────────────
  -- Performance Schema (latch-level detail)
  SELECT event_name, count_star, sum_timer_wait/1e12 as total_secs
  FROM performance_schema.events_waits_summary_global_by_event_name
  WHERE event_name LIKE 'wait/synch/mutex/innodb%'
     OR event_name LIKE 'wait/synch/rwlock/innodb%'
  ORDER BY sum_timer_wait DESC LIMIT 10;

  Common results:
    wait/synch/mutex/innodb/buf_pool_mutex           ← buffer pool
    wait/synch/rwlock/innodb/btr_search_latch         ← adaptive hash index
    wait/synch/mutex/innodb/trx_sys_mutex             ← transaction system
    wait/synch/mutex/innodb/log_sys_mutex              ← redo log

SQL Server:
──────────────
  -- Spinlock stats (very short latches)
  SELECT name, collisions, spins, spins_per_collision,
         sleep_time, backoffs
  FROM sys.dm_os_spinlock_stats
  ORDER BY spins DESC;

  -- Latch stats (longer latches)
  SELECT latch_class, waiting_requests_count,
         wait_time_ms, max_wait_time_ms
  FROM sys.dm_os_latch_stats
  WHERE waiting_requests_count > 0
  ORDER BY wait_time_ms DESC;
```

### 16.2 OS-Level Profiling

```
Linux perf (works for any database):
──────────────────────────────────────
  # Record lock contention events
  perf lock record -p <db_pid> -- sleep 10
  perf lock report

  # CPU profiling to find hot spinlocks
  perf record -g -p <db_pid> -- sleep 10
  perf report
  # Look for functions like: spin_lock, LWLockAcquire, mutex_spin_on_owner

  # Off-CPU analysis (where threads BLOCK)
  # Using bpftrace:
  bpftrace -e '
    tracepoint:sched:sched_switch /pid == <db_pid>/ {
        @[kstack] = count();
    }'

  # Futex contention analysis:
  perf trace -e futex -p <db_pid> -- sleep 5
  # Shows every futex syscall — high counts = latch contention going to kernel

Flamegraphs for latch analysis:
──────────────────────────────────
  1. perf record -F 99 -g -p <db_pid> -- sleep 30
  2. perf script | stackcollapse-perf.pl | flamegraph.pl > latch.svg
  3. Search for: LWLock, mutex, spin, rwlock, latch in the flamegraph
  4. Width of these frames = proportion of CPU time spent on latching
```

---

## 17. Comparative Summary

```
┌──────────────────┬────────┬──────────┬─────────┬───────┬───────────────┐
│ Technique         │ Size   │ Fairness │ Reader  │ Write │ Complexity    │
│                   │ (bytes)│          │ Scaling │ Perf  │               │
├──────────────────┼────────┼──────────┼─────────┼───────┼───────────────┤
│ pthread_mutex     │ 40     │ Varies   │ N/A     │ Good  │ Low           │
│ pthread_rwlock    │ 56     │ Varies   │ Poor    │ Good  │ Low           │
│ TAS Spinlock      │ 1      │ None     │ N/A     │ Fair  │ Very Low      │
│ TTAS Spinlock     │ 1      │ None     │ N/A     │ Good  │ Low           │
│ Ticket Lock       │ 4      │ FIFO     │ N/A     │ Fair  │ Low           │
│ MCS Lock          │ 8+64   │ FIFO     │ N/A     │ Best  │ Medium        │
│ Custom RW Latch   │ 4-8    │ Tunable  │ Medium  │ Good  │ Medium        │
│ OLC (Optimistic)  │ 8      │ N/A      │ Perfect │ Good  │ High          │
│ Bw-Tree (CAS)     │ 8      │ Lock-free│ Perfect │ Fair  │ Very High     │
│ Flat Combining     │ Varies │ Tunable  │ N/A     │ Best  │ High          │
│ Cohort (NUMA)     │ 64+    │ Batched  │ N/A     │ Best  │ High          │
│ parking_lot(Rust) │ 1      │ Fair     │ Poor    │ Best  │ Low (for user)│
└──────────────────┴────────┴──────────┴─────────┴───────┴───────────────┘

Decision framework for database engineers:

  IF your hot path is read-dominated (>90% reads):
    → Use OLC (optimistic latch coupling)
    → Readers touch ZERO shared state
    → Example: B-tree index traversals

  IF your hot path is write-dominated:
    → Use MCS or flat combining
    → Minimize cache line bouncing
    → Example: WAL buffer insertion, counter updates

  IF you have a few hundred latches (metadata, catalogs):
    → pthread_mutex / std::mutex is fine
    → Simplicity > micro-optimization
    → Example: table metadata, connection pools

  IF you have millions of latches (per-page, per-node):
    → Custom 4-8 byte latch (TTAS or ticket)
    → Size matters: 10M nodes × 40 bytes = 400 MB wasted
    → Example: buffer pool pages, B-tree nodes

  IF you run on NUMA (multi-socket servers):
    → Cohort locks or NUMA-aware MCS
    → 10× latency gap between local and remote atomics
    → Example: large PostgreSQL/Oracle on 2-4 socket machines

  IF you need lock-free for fault tolerance:
    → Bw-Tree, lock-free skip lists, epoch-based reclamation
    → Thread crash cannot corrupt data structure
    → Example: in-memory OLTP (Hekaton), concurrent memtables
```

---

## Further Reading

**Papers:**
- Mellor-Crummey & Scott, "Algorithms for Scalable Synchronization on Shared-Memory Multiprocessors" (1991) — MCS lock
- Harris, "A Pragmatic Implementation of Non-Blocking Linked-Lists" (2001)
- Dice, Hendler, Mircea, "Flat Combining and the Synchronization-Parallelism Tradeoff" (2010)
- Leis et al., "The Adaptive Radix Tree: ARTful Indexing for Main-Memory Databases" (2013)
- Levandoski et al., "The Bw-Tree: A B-tree for New Hardware Platforms" (2013)
- Leis et al., "LeanStore: In-Memory Data Management Beyond Main Memory" (2018)

**Source code to study:**
- PostgreSQL LWLock: `src/backend/storage/lmgr/lwlock.c`
- MySQL/InnoDB: `storage/innobase/sync/`
- WiredTiger: `src/support/rwlock.c`, `src/support/hazard.c`
- parking_lot (Rust): `github.com/Amanieu/parking_lot`
- crossbeam-epoch (Rust): `github.com/crossbeam-rs/crossbeam`
- Java AQS: `java.util.concurrent.locks.AbstractQueuedSynchronizer`
