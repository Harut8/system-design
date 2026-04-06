# OS and Hardware Internals for Database Engineers

How the operating system and hardware actually work beneath your database. This document covers the full stack from CPU instructions to disk platters: memory hierarchy, virtual memory, TLB, page faults, system calls, I/O models (buffered, direct, mmap, io_uring), NUMA, CPU features, and why databases make the low-level choices they do. Understanding this layer explains *why* databases implement their own buffer pools, avoid mmap, use direct I/O, and obsess over cache-line alignment.

---

## Table of Contents

1. [The Full Stack: From `SELECT` to Electrons](#1-the-full-stack-from-select-to-electrons)
2. [CPU Architecture and the Memory Hierarchy](#2-cpu-architecture-and-the-memory-hierarchy)
3. [Virtual Memory and Address Translation](#3-virtual-memory-and-address-translation)
4. [The TLB: Translation Lookaside Buffer](#4-the-tlb-translation-lookaside-buffer)
5. [Page Faults: Minor and Major](#5-page-faults-minor-and-major)
6. [System Calls and Context Switches](#6-system-calls-and-context-switches)
7. [File I/O: The OS Perspective](#7-file-io-the-os-perspective)
8. [Buffered I/O (The Default)](#8-buffered-io-the-default)
9. [Direct I/O (O_DIRECT)](#9-direct-io-o_direct)
10. [mmap: Memory-Mapped I/O](#10-mmap-memory-mapped-io)
11. [Why Databases Should NOT Use mmap](#11-why-databases-should-not-use-mmap)
12. [io_uring: Asynchronous I/O for Linux](#12-io_uring-asynchronous-io-for-linux)
13. [fsync, fdatasync, and Durability](#13-fsync-fdatasync-and-durability)
14. [Disk Hardware: HDD vs SSD vs NVMe](#14-disk-hardware-hdd-vs-ssd-vs-nvme)
15. [NUMA: Non-Uniform Memory Access](#15-numa-non-uniform-memory-access)
16. [CPU Features Databases Exploit](#16-cpu-features-databases-exploit)
17. [Kernel Bypass and Userspace I/O](#17-kernel-bypass-and-userspace-io)
18. [Putting It All Together: A Page Read, Step by Step](#18-putting-it-all-together-a-page-read-step-by-step)
19. [OS CPU Scheduling and Why Databases Care](#19-os-cpu-scheduling-and-why-databases-care)
20. [I/O Scheduling: From Elevator Algorithms to Multi-Queue](#20-io-scheduling-from-elevator-algorithms-to-multi-queue)
21. [Database-Level Scheduling: Thread Models and Userspace Schedulers](#21-database-level-scheduling-thread-models-and-userspace-schedulers)
22. [Anatomy of a Read/Write: Thread Lifecycle from Submission to Completion](#22-anatomy-of-a-readwrite-thread-lifecycle-from-submission-to-completion)

---

## 1. The Full Stack: From `SELECT` to Electrons

When a query touches a single page, the request crosses every layer in the stack. Each layer adds latency, and databases are designed to minimize crossings.

```
┌───────────────────────────────────────────────────────────────────────┐
│  APPLICATION LAYER                                                    │
│  SELECT * FROM users WHERE id = 42                                   │
│  → Parser → Optimizer → Executor                                     │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
                               ▼
┌───────────────────────────────────────────────────────────────────────┐
│  DATABASE STORAGE ENGINE                                              │
│  Buffer Pool lookup → miss → issue I/O request                       │
│  (Manages its own page cache, eviction, prefetch)                    │
└──────────────────────────────┬────────────────────────────────────────┘
                               │  read() / pread() / io_uring_submit()
                               ▼
┌───────────────────────────────────────────────────────────────────────┐
│  SYSTEM CALL INTERFACE                                                │
│  User mode ──trap──► Kernel mode                                     │
│  (Context switch: save registers, switch stack, verify args)         │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
                               ▼
┌───────────────────────────────────────────────────────────────────────┐
│  VIRTUAL FILE SYSTEM (VFS)                                            │
│  Route to correct filesystem driver (ext4, xfs, zfs, btrfs)         │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
                               ▼
┌───────────────────────────────────────────────────────────────────────┐
│  PAGE CACHE (OS Buffer Cache)                                         │
│  • Check if page already cached in RAM                               │
│  • If hit → copy to user buffer → return                             │
│  • If miss → issue block I/O request                                 │
│  (With O_DIRECT this layer is bypassed entirely)                     │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
                               ▼
┌───────────────────────────────────────────────────────────────────────┐
│  BLOCK I/O LAYER                                                      │
│  I/O scheduler (none/mq-deadline/bfq/kyber)                         │
│  Merge adjacent requests, reorder for locality                       │
└──────────────────────────────┬────────────────────────────────────────┘
                               │
                               ▼
┌───────────────────────────────────────────────────────────────────────┐
│  DEVICE DRIVER (NVMe / SCSI / SATA)                                  │
│  Translate block request → device commands                           │
│  NVMe: submission queue → doorbell register → completion queue       │
└──────────────────────────────┬────────────────────────────────────────┘
                               │  PCIe / SATA bus
                               ▼
┌───────────────────────────────────────────────────────────────────────┐
│  STORAGE HARDWARE                                                     │
│  SSD: FTL → NAND flash read                                         │
│  HDD: Seek arm → rotate platter → read sector                       │
└───────────────────────────────────────────────────────────────────────┘
```

**Key insight**: Every layer adds latency. Databases spend enormous effort to either *stay in the upper layers* (buffer pool hits) or *skip intermediate layers* (direct I/O, io_uring, kernel bypass).

---

## 2. CPU Architecture and the Memory Hierarchy

### The Memory Hierarchy

Every datum a CPU processes must ultimately reach a register. The hierarchy exists because fast memory is expensive and small, while cheap memory is slow and large.

```
                    ┌─────────┐
                    │Registers│  ~0.3 ns    64-bit × ~200 registers
                    │  (RF)   │             (architectural + rename)
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │ L1 Cache│  ~1 ns      32-64 KB per core
                    │ (split: │             (split I-cache + D-cache)
                    │  I + D) │             64-byte cache lines
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │ L2 Cache│  ~4-7 ns    256 KB - 1 MB per core
                    │(unified)│
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │ L3 Cache│  ~10-20 ns  8-64 MB shared across cores
                    │(shared) │             (LLC - Last Level Cache)
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │  DRAM   │  ~50-100 ns  GBs - TBs
                    │  (RAM)  │              ~25-50 GB/s bandwidth
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │  NVMe   │  ~10-20 μs   TBs
                    │  SSD    │              ~3-7 GB/s bandwidth
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │  SATA   │  ~50-100 μs  TBs
                    │  SSD    │              ~550 MB/s bandwidth
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │  HDD    │  ~5-10 ms    TBs
                    │         │              ~100-200 MB/s seq bandwidth
                    └─────────┘
```

### The Numbers Every Database Engineer Should Know

| Operation | Latency | Ratio to L1 |
|-----------|---------|-------------|
| L1 cache reference | 1 ns | 1x |
| L2 cache reference | 4 ns | 4x |
| L3 cache reference | 10 ns | 10x |
| DRAM reference | 100 ns | 100x |
| NVMe SSD random read (4KB) | 10,000 ns (10 μs) | 10,000x |
| SATA SSD random read (4KB) | 100,000 ns (100 μs) | 100,000x |
| HDD random read (4KB) | 10,000,000 ns (10 ms) | 10,000,000x |
| Network round-trip (same DC) | 500,000 ns (0.5 ms) | 500,000x |
| Network round-trip (cross-region) | 100,000,000 ns (100 ms) | 100,000,000x |

### Cache Lines: The Atomic Unit of Memory Transfer

The CPU never reads a single byte from memory. It reads an entire **cache line** (typically 64 bytes) at once. This is fundamental to database design:

```
Memory Address Space:
┌────────────────────────────────────────────────────────────┐
│ ... │ Byte 960 │ Byte 961 │ ... │ Byte 1023 │ Byte 1024 │ ...
└────────────────────────────────────────────────────────────┘
       ◄──────── Cache Line (64 bytes) ────────►

When CPU reads Byte 980:
  1. Check L1 cache for line containing bytes 960-1023
  2. Cache miss → fetch entire 64-byte line from L2/L3/DRAM
  3. Bytes 960-1023 now in L1 cache
  4. Accessing Byte 981, 982, ... 1023 is now FREE (already cached)
```

**Why databases care about cache lines:**

| Technique | How It Exploits Cache Lines |
|-----------|----------------------------|
| **Column stores** | Scanning one column = sequential 64-byte reads of the same type. Perfect spatial locality. |
| **B-tree node sizing** | Nodes sized as multiples of cache lines. A 256-byte node = 4 cache lines, read in one burst. |
| **Struct padding** | Hot fields grouped together so one cache line fetch gives you everything you need. |
| **Pointer chasing** | Following pointers (linked lists, trees) is catastrophic — each pointer dereference potentially misses the cache. This is why B-trees beat binary trees. |
| **Prefetching** | `__builtin_prefetch()` — tell the CPU to load a cache line you'll need soon, hiding memory latency behind computation. |

### False Sharing: A Multicore Cache Disaster

When two cores modify different variables that happen to share the same cache line, the hardware coherency protocol (MESI/MOESI) forces constant invalidation and reload:

```
Core 0                              Core 1
┌──────────┐                        ┌──────────┐
│ L1 Cache │                        │ L1 Cache │
│          │                        │          │
│ Line X:  │                        │ Line X:  │
│ [A] [B]  │  ◄── INVALIDATE ──►   │ [A] [B]  │
│  ▲        │                        │      ▲   │
│  │        │                        │      │   │
│  writes A │                        │  writes B│
└──────────┘                        └──────────┘

Both A and B on same cache line.
Core 0 writes A → invalidates line on Core 1.
Core 1 writes B → invalidates line on Core 0.
Ping-pong invalidation. MASSIVE performance hit.
```

**Database solution**: Pad frequently-modified shared counters to their own cache lines:

```c
// BAD: lock_count and ref_count share a cache line
struct PageHeader {
    int lock_count;    // Modified by many threads
    int ref_count;     // Modified by many threads
};

// GOOD: Each on its own cache line
struct PageHeader {
    alignas(64) int lock_count;
    alignas(64) int ref_count;
};
```

---

## 3. Virtual Memory and Address Translation

### Why Virtual Memory Exists

Every process thinks it has a contiguous, private address space starting from 0. The hardware (MMU — Memory Management Unit) translates these **virtual addresses** to **physical addresses** in DRAM.

```
Process A (database)                    Physical RAM
┌──────────────────────┐               ┌──────────────────┐
│ Virtual Page 0  ─────┼──────────────►│ Physical Frame 7  │
│ Virtual Page 1  ─────┼────┐         │                    │
│ Virtual Page 2  ─────┼──┐ │         │ Physical Frame 2  ◄┼─── Process B VP 0
│       ...            │  │ │         │                    │
│ Virtual Page N  ─────┼┐ │ │         │ Physical Frame 12 ◄┼─┐
└──────────────────────┘│ │ │         │                    │ │
                        │ │ │         │ Physical Frame 42 ◄┘ │
                        │ │ └────────►│ Physical Frame 99  │ │
                        │ └──────────►│ Physical Frame 5   │ │
                        └────────────►│ Physical Frame 31  │ │
                                      └──────────────────┘ │
Process B (web server)                                      │
┌──────────────────────┐                                    │
│ Virtual Page 0  ─────┼────────────────────────────────────┘
│       ...            │
└──────────────────────┘
```

### Page Tables: The Translation Mapping

The OS maintains a **page table** per process that maps virtual page numbers to physical frame numbers. On x86-64 with 4KB pages, a 48-bit virtual address is translated through a **4-level page table**:

```
48-bit Virtual Address
┌──────┬──────┬──────┬──────┬─────────────┐
│ PML4 │ PDPT │  PD  │  PT  │   Offset    │
│(9bit)│(9bit)│(9bit)│(9bit)│  (12 bit)   │
└──┬───┴──┬───┴──┬───┴──┬───┴──────┬──────┘
   │      │      │      │          │
   │      │      │      │          │  12 bits = 4096 byte page offset
   ▼      ▼      ▼      ▼          │
┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐   │
│PML4 │→│PDPT │→│ PD  │→│ PT  │   │
│Table│ │Table│ │Table│ │Table│   │
│     │ │     │ │     │ │     │   │
│[idx]│ │[idx]│ │[idx]│ │[idx]│──►Physical Frame Number
└─────┘ └─────┘ └─────┘ └─────┘   │
                                    ▼
                          Physical Address = Frame# + Offset
```

**Each level is itself a 4KB page containing 512 entries (512 × 8 bytes = 4096).**

### The Cost of Address Translation

Without caching, every memory access requires **4 additional memory accesses** just for translation:

```
Without TLB (worst case):
  Memory access = Page walk (4 × ~100 ns) + Data access (~100 ns)
                = 500 ns per access  (5x slower!)

With TLB hit:
  Memory access = TLB lookup (~1 ns) + Data access (~100 ns)
                = ~101 ns per access  (almost zero overhead)
```

### Huge Pages: Reducing Translation Overhead

Standard 4KB pages mean a 256 GB buffer pool requires ~67 million page table entries. **Huge pages** reduce this dramatically:

| Page Size | Pages for 256 GB | Page Table Entries | TLB Pressure |
|-----------|-------------------|--------------------|-------------|
| 4 KB | 67,108,864 | 67M entries across 4 levels | Extreme |
| 2 MB | 131,072 | 131K entries (3-level walk) | Moderate |
| 1 GB | 256 | 256 entries (2-level walk) | Minimal |

**Why databases use huge pages:**
- Buffer pools are large and long-lived — perfect for huge pages.
- Fewer TLB misses during page scans (each TLB entry covers 2MB instead of 4KB).
- PostgreSQL: `huge_pages = on` in postgresql.conf.
- Oracle: Requires HugePages configured at OS level, uses them by default.
- Linux: `echo 4096 > /proc/sys/vm/nr_hugepages` (reserves 4096 × 2MB = 8GB).

---

## 4. The TLB: Translation Lookaside Buffer

### What the TLB Is

The TLB is a small, extremely fast **hardware cache** inside the MMU that stores recent virtual-to-physical page translations. It is the *most important* cache for database performance that most engineers have never heard of.

```
CPU executes: MOV RAX, [0x7fff_1234_5678]

┌──────────────────────────────────────────────────────────────┐
│                      MMU (Hardware)                            │
│                                                                │
│  Virtual Address: 0x7fff_1234_5678                            │
│  Virtual Page #:  0x7fff_1234_5   (top 36 bits with 4KB pages)│
│  Page Offset:     0x678           (bottom 12 bits)            │
│                                                                │
│  ┌─────────────────────────────────────────────────────┐     │
│  │                    TLB Lookup                        │     │
│  │                                                       │     │
│  │  VPN 0x7fff_1234_5 → Frame 0x3A2F1  ✓ HIT          │     │
│  │                                                       │     │
│  │  Physical Address = 0x3A2F1_678                      │     │
│  │  Time: ~1 ns                                          │     │
│  └─────────────────────────────────────────────────────┘     │
│                                                                │
│  If TLB MISS:                                                  │
│  ┌─────────────────────────────────────────────────────┐     │
│  │  Hardware Page Walker activates                       │     │
│  │  Walk 4-level page table in memory                    │     │
│  │  4 sequential memory reads × ~100 ns = ~400 ns       │     │
│  │  Install result in TLB for next time                  │     │
│  └─────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────┘
```

### TLB Structure

| TLB Type | Entries (typical) | Hit Time | Purpose |
|----------|-------------------|----------|---------|
| L1 ITLB (instructions) | 64-128 | ~1 ns | Code fetch translations |
| L1 DTLB (data) | 64-72 | ~1 ns | Data access translations |
| L2 STLB (shared/unified) | 1024-2048 | ~4-7 ns | Backup for L1 misses |

**Coverage calculation:**
- L1 DTLB: 72 entries × 4KB pages = **288 KB** of addressable memory without misses.
- L1 DTLB: 72 entries × 2MB huge pages = **144 MB** of addressable memory without misses.
- L2 STLB: 2048 entries × 4KB = **8 MB** / with 2MB pages = **4 GB**.

This is why scanning a large buffer pool with 4KB pages causes TLB thrashing — you can only cover 8 MB before every new page triggers a page walk.

### TLB Shootdown: The Multicore Tax

When the OS modifies a page table (e.g., munmap, page migration), it must **invalidate** stale TLB entries on **all** cores that might have cached them:

```
Core 0 unmaps a page:

Core 0                Core 1                Core 2                Core 3
  │                     │                     │                     │
  │ munmap(addr, len)   │                     │                     │
  │                     │                     │                     │
  ├─── IPI ────────────►│                     │                     │
  ├─── IPI ───────────────────────────────────►│                     │
  ├─── IPI ──────────────────────────────────────────────────────►│
  │                     │                     │                     │
  │   WAITING...        │ invalidate TLB      │ invalidate TLB      │ invalidate TLB
  │                     │ send ACK            │ send ACK            │ send ACK
  │                     │                     │                     │
  │◄─── ACK ───────────┤                     │                     │
  │◄─── ACK ──────────────────────────────────┤                     │
  │◄─── ACK ─────────────────────────────────────────────────────┤
  │                     │                     │                     │
  │ continue...         │                     │                     │

IPI = Inter-Processor Interrupt
All cores STALL while handling the interrupt.
```

**This is a major reason databases avoid mmap**: `munmap` and `madvise(MADV_DONTNEED)` trigger TLB shootdowns. With 128 cores, each shootdown interrupts 127 cores. Databases running buffer pool eviction thousands of times per second would cause a storm of TLB shootdowns.

---

## 5. Page Faults: Minor and Major

### What Is a Page Fault?

A page fault is a **CPU exception** triggered when the MMU cannot complete a virtual-to-physical translation. The CPU traps into the kernel, which resolves the fault and resumes the instruction.

```
CPU attempts to access virtual address 0x7f0012340000

MMU checks page table entry:
  ┌──────────────────────────────────────────────────────────┐
  │ Page Table Entry for Virtual Page 0x7f0012340            │
  │                                                            │
  │ ┌─────────┬───────┬───────┬──────┬───────┬────────────┐  │
  │ │ Present │ R/W   │ User  │ Dirty│ Access│ Frame #     │  │
  │ │   bit   │ bit   │ bit   │ bit  │ bit   │             │  │
  │ │    0    │  ...  │  ...  │  ... │  ...  │  0x00000    │  │
  │ └────┬────┴───────┴───────┴──────┴───────┴────────────┘  │
  │      │                                                      │
  │      └── Present = 0 → PAGE FAULT EXCEPTION (#PF)          │
  └──────────────────────────────────────────────────────────┘
```

### Minor Page Fault (Soft Fault)

The page exists in physical memory but the page table mapping is not yet established. **No disk I/O required.**

```
Scenario: Process calls malloc(1MB), then writes to it for the first time.

Timeline:
  1. malloc(1MB)
     └─► Kernel allocates virtual address range (mmap internally)
         Does NOT allocate physical pages yet (demand paging)
         Page table entries: present = 0

  2. First write to byte 0:
     └─► CPU: page fault exception
         Kernel: allocate one physical frame from free list
         Kernel: update page table entry (present = 1, frame = 0x3A2F1)
         Kernel: zero the frame (security: don't leak other process data)
         CPU: retry the instruction → success
         Time: ~1-5 μs

  3. Write to byte 4097 (page 2):
     └─► Same thing. Another minor fault, another frame allocated.
```

**Cost**: ~1-5 μs per minor fault. No disk I/O, but still a kernel trap + page table modification + TLB update.

### Major Page Fault (Hard Fault)

The page is **not in physical memory at all** — it must be read from disk. This is catastrophically slow.

```
Scenario: mmap'd file, page not yet loaded, or page was evicted (swapped out).

Timeline:
  1. CPU accesses virtual address mapping to mmap'd file
  2. Page table: present = 0
  3. PAGE FAULT → trap to kernel
  4. Kernel checks: this is a file-backed mapping
  5. Kernel: find the page in the page cache?
     ├─ YES → minor fault (just update page table)
     └─ NO → MAJOR FAULT:
        a. Allocate physical frame
        b. Issue disk I/O: read 4KB from file at offset
        c. BLOCK the thread until I/O completes
        d. I/O completes: copy data to frame
        e. Update page table (present = 1)
        f. Wake the thread, retry instruction

  Time: 10 μs (NVMe) to 10 ms (HDD) per major fault
```

### Why Major Page Faults Are Devastating for Databases

```
Consider: Sequential scan of 1 GB table using mmap

If table is NOT in page cache:
  1 GB / 4 KB per page = 262,144 pages
  262,144 major page faults × 10 μs (NVMe) = 2.6 seconds JUST in fault handling

  But it's worse than that:
  - Each fault blocks the thread (it's synchronous)
  - The kernel doesn't know your access pattern (can't prefetch optimally)
  - Readahead helps somewhat but is generic and conservative
  - Each fault = kernel trap + scheduler involvement + TLB update

With explicit read() or pread():
  - Database can issue large sequential reads (e.g., 1 MB chunks)
  - 1 GB / 1 MB = 1,024 system calls (not 262,144 faults)
  - Database can use readahead hints or io_uring for async
  - Database controls the I/O scheduling entirely
```

---

## 6. System Calls and Context Switches

### What Happens During a System Call

A system call (syscall) is how userspace asks the kernel to do something. On x86-64 Linux, the `syscall` instruction enters kernel mode:

```
User Space Program                           Kernel Space
─────────────────                           ──────────────
  pread(fd, buf, 4096, offset)
        │
        ▼
  libc wrapper:
    mov rax, 17        ← syscall number (pread64)
    mov rdi, fd
    mov rsi, buf
    mov rdx, 4096
    mov r10, offset
    syscall             ← CPU privilege transition
        │
        ├── 1. CPU switches to ring 0 (kernel mode)
        ├── 2. Save user registers on kernel stack
        ├── 3. Switch to kernel stack
        ├── 4. KPTI: switch page tables (Meltdown mitigation)
        ├── 5. Look up syscall handler in sys_call_table[17]
        ├── 6. Execute handler: validate args, do work
        ├── 7. KPTI: switch page tables back
        ├── 8. Restore user registers
        ├── 9. sysret instruction → back to ring 3
        │
        ▼
  pread returns (data in buf)
```

### System Call Overhead

| Component | Cost |
|-----------|------|
| `syscall` + `sysret` instructions | ~100 ns |
| KPTI page table switch (Meltdown mitigation) | ~100-200 ns |
| Argument validation | ~10-50 ns |
| **Total overhead (minimum)** | **~200-400 ns per syscall** |
| Actual I/O work | Varies (0 if page cache hit, 10μs+ if disk) |

**200-400 ns per syscall** seems small, but at 1 million IOPS (common for NVMe), that's 200-400 ms of *pure syscall overhead* per second. This is why io_uring exists — to amortize the syscall cost over many I/O operations.

### Context Switch vs System Call

A **system call** temporarily enters kernel mode and returns. A **context switch** puts the current thread to sleep and runs a different thread. Context switches are far more expensive:

```
Context Switch Costs:
┌────────────────────────────────────────────────────────────┐
│ 1. Save ALL registers of Thread A        ~100 ns          │
│ 2. Save FPU/SSE/AVX state               ~200 ns          │
│ 3. Switch page tables (if different process) ~200 ns      │
│ 4. Flush pipeline (speculation barrier)  ~100 ns          │
│ 5. Load ALL registers of Thread B        ~100 ns          │
│ 6. Load FPU/SSE/AVX state               ~200 ns          │
│ 7. Cold caches (L1/L2/TLB misses)       ~5,000-50,000 ns │
│                                                            │
│ TOTAL: ~1-5 μs direct + ~10-50 μs indirect (cache misses) │
└────────────────────────────────────────────────────────────┘
```

**The indirect cost dominates**: After switching, Thread B's data and code are not in L1/L2 caches or the TLB. The first thousand memory accesses are all cache misses.

**Why databases minimize context switches:**
- Thread-per-connection model (PostgreSQL): 1000 connections = 1000 threads = constant context switching.
- Thread pool model (MySQL, modern systems): Small number of worker threads, multiplex connections.
- Coroutine/fiber model: Userspace scheduling, avoid kernel context switches entirely.

---

## 7. File I/O: The OS Perspective

### The Linux I/O Stack in Detail

```
                    Application
                        │
            ┌───────────┴───────────┐
            │                       │
        Buffered I/O            Direct I/O
        (default)              (O_DIRECT)
            │                       │
            ▼                       │
    ┌──────────────┐                │
    │  Page Cache   │                │
    │  (OS Buffer   │                │
    │   Cache)      │                │
    │  ~50-90% of   │                │
    │  free RAM     │                │
    └──────┬───────┘                │
           │                        │
           ├────────────────────────┘
           │
           ▼
    ┌──────────────┐
    │ Filesystem   │    ext4: journal + extent tree
    │ Driver       │    xfs: allocation groups + B+ tree
    │              │    zfs: copy-on-write + checksums
    └──────┬───────┘
           │
           ▼
    ┌──────────────┐
    │ Block Layer  │    I/O Scheduler (merge, reorder)
    │              │    Pluggable: none, mq-deadline, bfq, kyber
    └──────┬───────┘
           │
           ▼
    ┌──────────────┐
    │ Device Driver│    NVMe: multiple hardware queues
    │              │    SCSI: single queue (legacy)
    └──────┬───────┘
           │
           ▼
        Hardware
```

### I/O Method Comparison at a Glance

| Property | Buffered I/O | Direct I/O (O_DIRECT) | mmap | io_uring |
|----------|-------------|----------------------|------|----------|
| Page cache | Uses OS cache | Bypasses OS cache | Uses OS cache | Depends on flags |
| Alignment | No restrictions | Must align to 512B/4KB | No restrictions | Depends on flags |
| Copy cost | Kernel→User copy | DMA direct to user buf | Zero-copy (shared mapping) | Zero-copy possible |
| Async support | Fake (still blocks on cache miss) | Real with AIO/io_uring | None (faults block) | True async |
| Eviction control | OS decides (LRU-ish) | Database decides | OS decides | Database decides |
| Prefetch control | OS readahead | Database controls | OS readahead | Database controls |
| Syscall overhead | Per read/write | Per read/write | Zero (until fault) | Amortized (batched) |
| Error handling | Return value | Return value | SIGBUS / SIGSEGV | Completion queue |

---

## 8. Buffered I/O (The Default)

### How Buffered I/O Works

When you call `read()` or `write()` without special flags, the kernel routes everything through the **page cache**:

```
Application calls: read(fd, buf, 4096)

Step 1: Page Cache Lookup
┌──────────────────────────────────────────────────┐
│ Page Cache (radix tree / xarray indexed by       │
│ (inode, offset) pairs)                           │
│                                                    │
│  (inode=42, offset=0)    → page @ 0x3A2F1000 ✓  │
│  (inode=42, offset=4096) → page @ 0x3B001000 ✓  │
│  (inode=42, offset=8192) → ???               ✗  │
│  ...                                              │
└──────────────────────────────────────────────────┘

If page found in cache (CACHE HIT):
  → memcpy(user_buf, kernel_page, 4096)
  → Return. Total time: ~1-2 μs (syscall + memcpy)

If page NOT in cache (CACHE MISS):
  → Allocate page frame
  → Submit block I/O request to device
  → Block thread until I/O completes
  → memcpy(user_buf, kernel_page, 4096)
  → Page stays in cache for future reads
  → Total time: 10 μs (NVMe) to 10 ms (HDD)
```

### Write Path: Buffered Write

```
Application calls: write(fd, data, 4096)

Step 1: Find or create page in page cache
Step 2: memcpy(kernel_page, user_data, 4096)
Step 3: Mark page as DIRTY
Step 4: Return immediately to application
        (data is NOT on disk yet!)

Later (asynchronously):
  Kernel writeback daemon (pdflush / kworker):
  - Triggers when dirty pages > dirty_ratio threshold
  - Or when dirty page age > dirty_expire_centisecs
  - Or when sync/fsync is called
  - Writes dirty pages to disk
  - Marks pages as CLEAN
```

### The Double-Buffering Problem

Buffered I/O means data exists in **two places** in RAM:

```
┌─────────────────────────────────────────────────────────┐
│ RAM                                                       │
│                                                           │
│  ┌─────────────────────┐                                 │
│  │ Database Buffer Pool │                                 │
│  │                       │                                 │
│  │  Page 42: [data...]  │  ◄── Database manages this     │
│  │                       │                                 │
│  └─────────────────────┘                                 │
│                                                           │
│  ┌─────────────────────┐                                 │
│  │ OS Page Cache         │                                 │
│  │                       │                                 │
│  │  Page 42: [data...]  │  ◄── OS also caches this!      │
│  │                       │      SAME DATA, WASTED RAM     │
│  └─────────────────────┘                                 │
│                                                           │
│  Total: 8 KB used for one 4 KB page                      │
└─────────────────────────────────────────────────────────┘
```

With a 128 GB server running a database with a 64 GB buffer pool, up to 64 GB of OS page cache might be duplicating the buffer pool's contents. This is one reason databases use **O_DIRECT** — to eliminate double buffering.

---

## 9. Direct I/O (O_DIRECT)

### What O_DIRECT Does

`O_DIRECT` tells the kernel to **bypass the page cache** entirely. Data goes directly between user-space buffers and the storage device via DMA.

```
Buffered I/O:                          Direct I/O:

Application Buffer                     Application Buffer
      │                                      │
      ▼ (memcpy)                             │
OS Page Cache                                │ (DMA - Direct Memory Access)
      │                                      │
      ▼ (DMA)                               │
Disk Controller                              ▼
      │                                 Disk Controller
      ▼                                      │
Storage Media                                ▼
                                        Storage Media

Extra copy: YES                         Extra copy: NO
Double buffering: YES                   Double buffering: NO
OS controls eviction: YES               DB controls eviction: YES
```

### O_DIRECT Alignment Requirements

DMA requires proper alignment. The buffer, the file offset, and the transfer size must all be aligned:

```c
// BAD: Will fail with EINVAL
char buf[4096];
pread(fd, buf, 4096, 0);  // buf is not aligned

// GOOD: Properly aligned
void *buf;
posix_memalign(&buf, 4096, 4096);  // 4096-byte aligned allocation
pread(fd, buf, 4096, 0);           // offset aligned, size aligned

// Requirements (typical):
// - Buffer address: aligned to logical block size (usually 512 or 4096)
// - File offset:    aligned to logical block size
// - Transfer size:  multiple of logical block size
```

### Who Uses O_DIRECT

| Database | O_DIRECT Usage | Why |
|----------|---------------|-----|
| **PostgreSQL** | Off by default, configurable | Historically relied on OS page cache, moving toward direct I/O |
| **MySQL/InnoDB** | On by default (`innodb_flush_method=O_DIRECT`) | InnoDB has its own buffer pool, double buffering wastes RAM |
| **Oracle** | Async direct I/O by default | Has managed its own cache since the 1980s |
| **ScyllaDB** | Always O_DIRECT + io_uring | Seastar framework bypasses OS entirely |
| **RocksDB** | O_DIRECT for reads/writes (configurable) | LSM compaction generates huge I/O, would thrash page cache |
| **SQLite** | Off by default | Designed for simplicity, relies on OS cache |

---

## 10. mmap: Memory-Mapped I/O

### How mmap Works

`mmap()` maps a file (or region) into the process's virtual address space. After mapping, you access the file's contents as if they were ordinary memory — no `read()`/`write()` calls needed.

```c
// Map a file into memory
void *addr = mmap(NULL, file_size, PROT_READ | PROT_WRITE,
                  MAP_SHARED, fd, 0);

// Now access file contents directly:
int value = *(int *)(addr + offset);  // reading the file
*(int *)(addr + offset) = 42;         // writing the file
```

```
Process Virtual Address Space:
┌──────────────────────────────────────────────────────────────┐
│  0x0000...  Code segment (.text)                             │
│  0x1000...  Data segment (.data, .bss)                       │
│  0x2000...  Heap (malloc)                                    │
│     ...                                                       │
│  0x7f00...  ┌──────────────────────────────────────┐         │
│             │  mmap region: data.db                  │         │
│             │  Virtual pages backed by file pages     │         │
│             │                                         │         │
│             │  Page 0 → file offset 0-4095           │         │
│             │  Page 1 → file offset 4096-8191        │         │
│             │  Page 2 → not yet accessed (no frame)  │         │
│             │  ...                                    │         │
│             └──────────────────────────────────────┘         │
│     ...                                                       │
│  0x7fff...  Stack                                             │
└──────────────────────────────────────────────────────────────┘
```

### mmap Access Pattern: What Really Happens

```
Step 1: mmap(fd) → Kernel creates page table entries (all present=0)
        No data loaded. No physical memory allocated. Instant.

Step 2: First read of byte at offset 1000
        ├── CPU: page fault (present=0)
        ├── Kernel: is page in page cache?
        │   ├── YES → map it into process (minor fault, ~1 μs)
        │   └── NO  → read from disk (major fault, ~10 μs - 10 ms)
        ├── Kernel: set page table entry (present=1, frame=physical)
        └── CPU: retry instruction → succeeds

Step 3: Second read of byte at offset 1001 (same page)
        └── No fault. Page already mapped. Direct memory access. ~100 ns.

Step 4: First read of byte at offset 5000 (different page)
        └── Another page fault. Repeat Step 2.
```

### The Seductive Appeal of mmap

| Advantage | Explanation |
|-----------|-------------|
| **Zero-copy** | No memcpy between kernel and user buffers |
| **No syscall per access** | After mapping, access is just a memory load |
| **Simple code** | No buffer management, no read()/write() calls |
| **Lazy loading** | Only pages actually accessed are loaded |
| **Automatic caching** | OS page cache handles caching transparently |

---

## 11. Why Databases Should NOT Use mmap

The Andy Pavlo et al. paper *"Are You Sure You Want to Use MMAP in Your Database Management System?"* (CIDR 2022) systematically demonstrates why mmap is a poor choice for database storage engines. Here's the complete argument:

### Problem 1: Uncontrollable Eviction

The OS has **no idea** which pages are important to the database.

```
Database workload: Frequently scanning hot index pages + rarely touching cold data

What the database buffer pool would do:
  ┌──────────────────────────────┐
  │  Buffer Pool (LRU-K / Clock) │
  │                                │
  │  Hot index pages: KEEP         │  ← Database knows these are critical
  │  Cold data pages: EVICT FIRST │  ← Database knows these are expendable
  └──────────────────────────────┘

What the OS page cache does with mmap:
  ┌──────────────────────────────┐
  │  Page Cache (approximate LRU) │
  │                                │
  │  Recently scanned cold pages:  │  ← Just touched during scan
  │    KEEP (recently used!)       │    OS thinks these are "hot"
  │  Hot index pages:              │
  │    EVICT (not used "recently") │  ← OS has NO IDEA these matter
  └──────────────────────────────┘

Result: Index pages evicted, next query triggers major page faults on critical data.
```

`madvise()` hints (`MADV_SEQUENTIAL`, `MADV_DONTNEED`, `MADV_WILLNEED`) exist but are **advisory only** — the kernel can and does ignore them under memory pressure.

### Problem 2: I/O Stalls Are Invisible and Uncontrollable

With a buffer pool, the database can:
- Issue prefetch requests before data is needed
- Schedule I/O across multiple threads
- Choose *when* to block

With mmap, I/O stalls happen **transparently inside any memory access**:

```
// This innocent-looking code might block for 10 ms:
if (page->header.flags & LEAF_NODE) {    // ← page fault here: 10 ms stall
    return page->data[slot_id];           // ← another fault possible: 10 ms stall
}

// The database has ZERO CONTROL over when I/O happens.
// Any pointer dereference might be a 10 ms disk read.
// You cannot prefetch, you cannot schedule, you cannot cancel.
```

### Problem 3: Error Handling Is a Nightmare

With `read()`/`pread()`, I/O errors are return values you can check:

```c
// Direct I/O: Clean error handling
ssize_t ret = pread(fd, buf, 4096, offset);
if (ret < 0) {
    // Handle: EIO (disk error), EINVAL (bad offset), etc.
    log_error("Read failed: %s", strerror(errno));
    return ERROR;
}

// mmap: Error = SIGBUS signal (process killed by default!)
void *data = mmap(...);
int value = *(int *)(data + offset);  // If disk error → SIGBUS → process crash

// To handle it, you need:
// 1. Install a SIGBUS signal handler
// 2. Use setjmp/longjmp to recover
// 3. Hope you can figure out WHICH access failed
// 4. Hope the kernel gives you enough info to retry
// This is fragile, non-portable, and error-prone.
```

### Problem 4: TLB Shootdown Storm

Database eviction requires unmapping pages. With mmap, this means `munmap()` or `madvise(MADV_DONTNEED)`, both of which trigger TLB shootdowns across all cores:

```
Database with 128 cores doing buffer pool eviction:

With buffer pool (O_DIRECT):
  Evict page: remove from hash table, free buffer frame.
  No TLB involvement. No inter-core coordination.
  Cost: ~100 ns per eviction.

With mmap:
  Evict page: madvise(MADV_DONTNEED, page_addr, 4096)
  ├── Kernel: send IPI to 127 other cores
  ├── Each core: interrupt, flush TLB entry, send ACK
  ├── Wait for all 127 ACKs
  └── Cost: ~5-20 μs per eviction (50-200x slower!)

  At 100,000 evictions/second:
    mmap: 100,000 × 127 = 12.7 million IPIs/second
    Direct I/O: 0 IPIs/second
```

### Problem 5: No Control Over Write Ordering

Databases need strict write ordering for crash recovery (WAL before data pages). With mmap + `MAP_SHARED`, the kernel can write dirty pages to disk **in any order at any time**:

```
Correct write order (what WAL requires):
  1. Write WAL record for "update page 42"
  2. fsync WAL
  3. Modify page 42 in buffer pool
  4. Later: write page 42 to disk

  If crash after step 2 but before step 4:
    → WAL has the change, replay it. Data is safe.

mmap write order (what actually happens):
  1. Modify page 42 via mmap (kernel marks page dirty)
  2. Write WAL record
  3. Kernel decides to flush page 42 to disk (dirty_expire triggered)
     ← This can happen BEFORE the WAL is fsynced!
  4. Crash!

  Result: Page 42 has partial changes on disk, WAL doesn't have the record yet.
          DATA CORRUPTION.
```

To prevent this, you'd need `msync()` on every page modification, which defeats the purpose of mmap.

### Problem 6: Inability to Perform Async I/O

mmap access is fundamentally **synchronous**. When you touch a page that's not in memory, the faulting thread blocks until I/O completes. There's no way to:

- Issue multiple reads in parallel from one thread
- Cancel a pending read
- Set timeouts on reads
- Prioritize certain reads over others

### Databases That Use mmap (and Their Pain)

| Database | mmap Usage | Problems Encountered |
|----------|-----------|---------------------|
| **MongoDB (WiredTiger)** | Moved away from mmap (original MMAPv1 engine retired) | Uncontrollable eviction, write ordering issues |
| **SQLite** | Optional mmap mode | Acceptable for single-writer, small databases |
| **LMDB** | Core design uses mmap for reads | Works because LMDB is read-heavy, copy-on-write, and single-writer |
| **QuestDB** | Uses mmap | Accepts the tradeoffs for time-series append workload |
| **SingleStore (MemSQL)** | Used mmap, replaced with custom storage | Performance unpredictability under memory pressure |

### When mmap Is Acceptable

- **Read-only** or **read-mostly** workloads (no write-ordering issues)
- **Dataset fits in RAM** (no major page faults, no eviction needed)
- **Single-threaded** or few cores (TLB shootdowns are cheap)
- **Crash recovery not critical** (analytics, non-durable caching)
- **Append-only** workloads where you never modify existing pages

---

## 12. io_uring: Asynchronous I/O for Linux

### The Problem io_uring Solves

Traditional Linux I/O suffers from:
1. **One syscall per I/O operation**: 200-400 ns overhead each.
2. **Synchronous by default**: Thread blocks on cache miss.
3. **Linux AIO (libaio)**: Only works with O_DIRECT, limited operations, poorly designed.

io_uring (added in Linux 5.1, 2019) solves all three.

### How io_uring Works: Shared Ring Buffers

io_uring creates **two ring buffers** shared between user space and kernel space via mmap. The application and kernel communicate through these rings with **zero syscalls** in the fast path.

```
┌─────────────────────────────────────────────────────────────────┐
│                     SHARED MEMORY (mmap'd)                       │
│                                                                   │
│  Submission Queue (SQ)              Completion Queue (CQ)        │
│  ┌─────────────────────┐          ┌─────────────────────┐       │
│  │ ┌───┬───┬───┬───┐   │          │ ┌───┬───┬───┬───┐   │       │
│  │ │SQE│SQE│SQE│   │   │          │ │CQE│CQE│   │   │   │       │
│  │ │ 1 │ 2 │ 3 │   │   │          │ │ 1 │ 2 │   │   │   │       │
│  │ └───┴───┴───┴───┘   │          │ └───┴───┴───┴───┘   │       │
│  │  tail ──────► head   │          │  head ──────► tail   │       │
│  │  (app writes)        │          │  (app reads)         │       │
│  │  (kernel reads)      │          │  (kernel writes)     │       │
│  └─────────────────────┘          └─────────────────────┘       │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘

  Application (user space)              Kernel
  ─────────────────────────            ──────
  1. Write SQE to SQ ring   ────►
  2. Write SQE to SQ ring   ────►
  3. Write SQE to SQ ring   ────►     Sees new SQEs
  4. (optional) io_uring_enter()       Processes SQEs
     or SQPOLL mode: no syscall!       Issues I/O to devices

                                       I/O completes:
                              ◄────    Write CQE to CQ ring
                              ◄────    Write CQE to CQ ring
  5. Read CQEs from CQ ring
  6. Process completions
```

### Submission Queue Entry (SQE)

```c
struct io_uring_sqe {
    __u8    opcode;     // IORING_OP_READ, IORING_OP_WRITE, IORING_OP_FSYNC, ...
    __u8    flags;      // IOSQE_IO_LINK, IOSQE_FIXED_FILE, ...
    __u16   ioprio;     // I/O priority
    __s32   fd;         // File descriptor
    __u64   off;        // File offset
    __u64   addr;       // Buffer address
    __u32   len;        // Buffer length
    __u64   user_data;  // Opaque user data (returned in CQE)
    // ... more fields for advanced features
};
```

### Completion Queue Entry (CQE)

```c
struct io_uring_cqe {
    __u64   user_data;  // Copied from SQE — identifies which request completed
    __s32   res;        // Result (bytes read/written, or -errno)
    __u32   flags;      // IORING_CQE_F_BUFFER, etc.
};
```

### io_uring Operating Modes

```
Mode 1: Basic (io_uring_enter for submission)
─────────────────────────────────────────────
  App: fill SQEs → syscall io_uring_enter() → kernel processes → poll CQEs
  Syscalls: 1 per batch (not per I/O operation!)

  Batch of 32 reads: 1 syscall instead of 32 syscalls
  Overhead savings: 31 × 300 ns = ~9.3 μs saved per batch

Mode 2: SQPOLL (kernel thread polls SQ, zero syscalls)
──────────────────────────────────────────────────────
  App: fill SQEs → kernel polling thread sees them → processes → CQEs appear
  Syscalls: ZERO in fast path!

  Kernel spawns a dedicated thread that busy-polls the SQ ring.
  If idle too long, thread sleeps; app uses io_uring_enter() to wake it.

  Cost: 1 CPU core dedicated to polling.
  Benefit: Absolute minimum latency for high-IOPS workloads.

Mode 3: IOPOLL (polling completion instead of interrupts)
────────────────────────────────────────────────────────
  Instead of waiting for device interrupts, actively poll for completion.
  Eliminates interrupt latency (~2-5 μs per I/O).
  Used with NVMe devices that support polling mode.
```

### io_uring Advanced Features

| Feature | Description |
|---------|-------------|
| **SQE Linking** | Chain operations: read → process → write, atomically |
| **Fixed Files** | Pre-register FDs, avoid per-call fd lookup overhead |
| **Fixed Buffers** | Pre-register buffers, avoid per-call memory pinning |
| **Multishot** | One SQE triggers multiple CQEs (e.g., accept connections) |
| **Buffer Rings** | Kernel picks buffer from pre-registered ring (zero-copy recv) |
| **io_uring_register** | Register resources once, reuse across operations |

### Database Adoption of io_uring

| Database / System | io_uring Usage | Impact |
|-------------------|---------------|--------|
| **ScyllaDB** | Core I/O engine (Seastar) | ~2x IOPS improvement over libaio |
| **RocksDB** | Optional (MultiRead, compaction) | Reduced compaction latency |
| **PostgreSQL** | Under development (PG 16+) | Async WAL writes, prefetch |
| **TiKV** | Uses via tokio-uring | Reduced tail latency |
| **Ceph** | BlueStore backend | Higher throughput for OSD |
| **liburing** | C wrapper library by Jens Axboe | Simplifies io_uring usage |

### io_uring vs Other I/O Models

```
Operation: Read 32 pages (4KB each) from NVMe SSD

Synchronous pread() (one thread):
  32 × (syscall overhead + NVMe latency)
  32 × (300 ns + 10 μs) ≈ 330 μs total
  Throughput: 32 × 4KB / 330 μs ≈ 388 MB/s

Synchronous pread() (32 threads):
  1 × (syscall overhead + NVMe latency)  [parallel]
  ≈ 10.3 μs total (but 32 threads context-switch overhead)
  Thread management overhead dominates

Linux AIO (libaio):
  1 syscall to submit 32 reads
  Wait for 32 completions
  300 ns + max(10 μs) ≈ 10.3 μs
  But: O_DIRECT only, limited to read/write

io_uring (batched):
  Fill 32 SQEs, 1 io_uring_enter() syscall
  Poll CQ for completions
  300 ns + max(10 μs) ≈ 10.3 μs
  Works with buffered I/O, supports fsync, accept, etc.

io_uring (SQPOLL mode):
  Fill 32 SQEs, kernel thread polls automatically
  0 syscalls!
  max(10 μs) ≈ 10 μs
  Absolute lowest latency
```

---

## 13. fsync, fdatasync, and Durability

### The Write Durability Stack

When you call `write()`, your data is NOT on disk. It's in the OS page cache. You need explicit flush calls to guarantee durability.

```
write(fd, data, len) completes:

Where is the data?
┌───────────────────────────────────────────┐
│ ✓ Application buffer                       │
│ ✓ OS page cache (kernel memory)            │
│ ✗ Disk controller write cache              │  ← volatile!
│ ✗ Disk media (NAND flash / magnetic platter)│  ← durable
└───────────────────────────────────────────┘

After fsync(fd) completes:
┌───────────────────────────────────────────┐
│ ✓ Application buffer                       │
│ ✓ OS page cache                            │
│ ✓ Disk controller write cache → flushed    │
│ ✓ Disk media                               │  ← GUARANTEED durable
└───────────────────────────────────────────┘
```

### fsync vs fdatasync vs sync_file_range

| Call | What It Flushes | Metadata Updated? | Typical Use |
|------|----------------|-------------------|------------|
| `fsync(fd)` | All dirty pages of fd + metadata | Yes (size, mtime, etc.) | WAL commit |
| `fdatasync(fd)` | All dirty pages of fd + essential metadata | Only if size changed | Data files |
| `sync_file_range()` | Specific byte range, non-blocking option | No | Async writeback hints |
| `sync()` | ALL dirty pages system-wide | Yes | Don't use in databases |

### fdatasync Optimization

```
fsync: Always writes inode metadata (modification time, etc.)
       Even if only data changed, must update and flush inode.

fdatasync: Skips metadata IF file size hasn't changed.
           For in-place updates (overwriting existing pages):
             → Only flushes data pages
             → Skips inode write (saves 1 I/O)
           For appends (file grows):
             → Must flush data + metadata (size changed)
             → Same cost as fsync

WAL optimization:
  Pre-allocate WAL file to 64 MB (fallocate)
  Write WAL records into pre-allocated space (no size change)
  fdatasync → only flushes WAL data, not metadata
  When WAL fills up, allocate new file (one fsync for metadata)
```

### The Disk Write Cache Trap

Modern SSDs and HDDs have volatile DRAM write caches. Even after the OS writes data to the device, it might sit in the controller's volatile cache:

```
write() → OS page cache → device driver → disk controller DRAM cache
                                                    │
                                                    │ Power failure here!
                                                    ▼
                                              DATA LOST

fsync() forces a cache flush (FUA - Force Unit Access):
  → Disk controller writes cache contents to stable media
  → Returns only when data is on NAND/platter

DANGER: Some cheap SSDs lie about fsync completion!
  → They acknowledge fsync before data reaches stable media
  → Enterprise SSDs have capacitors to flush cache on power loss
  → This is why databases recommend enterprise-grade storage
```

### Write Barriers and Ordering

```
Database crash recovery requires:
  1. WAL record written to disk  BEFORE  data page written to disk

Implementation:
  write(wal_fd, wal_record);
  fdatasync(wal_fd);          ← BARRIER: everything before is durable
  write(data_fd, page);       ← Can happen any time after

Without the fdatasync barrier:
  - Disk might reorder writes internally
  - Data page could reach disk before WAL record
  - Crash → data page updated but no WAL record → unrecoverable corruption
```

---

## 14. Disk Hardware: HDD vs SSD vs NVMe

### HDD: Mechanical Storage

```
┌──────────────────────────────────────────────────────────────┐
│                        HDD Architecture                       │
│                                                                │
│    Spindle Motor                                              │
│        │                                                       │
│   ┌────┴────┐                                                 │
│   │         │  ← Platter (magnetic disk)                      │
│   │  ┌───┐  │     Spins at 5400/7200/10000/15000 RPM         │
│   │  │   │  │                                                  │
│   │  └───┘  │  ← Track (concentric circle of sectors)        │
│   │         │                                                  │
│   └─────────┘                                                 │
│       ▲                                                        │
│       │  ← Read/Write Head on actuator arm                    │
│       │     Moves radially to seek different tracks            │
│                                                                │
│   Access Time = Seek Time + Rotational Latency + Transfer Time│
│                                                                │
│   Seek Time: 3-15 ms (moving arm to correct track)            │
│   Rotational Latency: avg 4.2 ms (7200 RPM) / 2 ms (15K RPM)│
│   Transfer: ~150-200 MB/s sequential                          │
│                                                                │
│   Random 4KB Read:  ~10 ms (the arm must physically move!)    │
│   Sequential 1MB:   ~5 ms  (1 MB / 200 MB/s)                 │
│   Sequential/Random ratio: ~200:1                              │
└──────────────────────────────────────────────────────────────┘
```

**Why B-trees exist**: Minimize random seeks. A B-tree with fanout 500 needs only 3 seeks to find any row among 125 million. Each seek is 10 ms on HDD, so 30 ms total vs. scanning the entire table.

### SSD: NAND Flash Storage

```
┌──────────────────────────────────────────────────────────────────┐
│                        SSD Architecture                           │
│                                                                    │
│  ┌──────────────────────────────────────────────────────┐        │
│  │                    SSD Controller                      │        │
│  │  ARM/RISC cores + DRAM cache (256MB-4GB) + FTL logic  │        │
│  └──────────┬────────────┬────────────┬──────────────────┘        │
│             │            │            │                             │
│         Channel 0    Channel 1    Channel N   (parallel channels)  │
│             │            │            │                             │
│         ┌───┴───┐    ┌───┴───┐    ┌───┴───┐                      │
│         │ Die 0 │    │ Die 0 │    │ Die 0 │                      │
│         │ Die 1 │    │ Die 1 │    │ Die 1 │                      │
│         └───────┘    └───────┘    └───────┘                      │
│                                                                    │
│  Each Die:                                                         │
│  ┌─────────────────────────────────────────────┐                  │
│  │  Plane 0              Plane 1                │                  │
│  │  ┌──────────────┐    ┌──────────────┐       │                  │
│  │  │ Block 0      │    │ Block 0      │       │                  │
│  │  │  Page 0 (4KB)│    │  Page 0 (4KB)│       │                  │
│  │  │  Page 1 (4KB)│    │  Page 1 (4KB)│       │                  │
│  │  │  ...         │    │  ...         │       │                  │
│  │  │  Page 255    │    │  Page 255    │       │                  │
│  │  │ Block 1      │    │ Block 1      │       │                  │
│  │  │  ...         │    │  ...         │       │                  │
│  │  └──────────────┘    └──────────────┘       │                  │
│  └─────────────────────────────────────────────┘                  │
│                                                                    │
│  Key constraints:                                                  │
│  - Read unit:  Page (4-16 KB)                                     │
│  - Write unit: Page (4-16 KB) — can only write to ERASED pages    │
│  - Erase unit: Block (256-1024 pages, 1-4 MB) — must erase whole │
│  - Write amplification: erasing blocks to rewrite pages           │
└──────────────────────────────────────────────────────────────────┘
```

### Flash Translation Layer (FTL)

The FTL is firmware inside the SSD that makes NAND flash look like a block device:

```
OS sees:            SSD internally:
Logical Block 0 ──► Physical Page in Die 2, Plane 0, Block 7, Page 3
Logical Block 1 ──► Physical Page in Die 0, Plane 1, Block 12, Page 0
Logical Block 2 ──► Physical Page in Die 1, Plane 0, Block 3, Page 99

FTL responsibilities:
┌────────────────────────────────────────────────────────────┐
│ 1. Logical-to-Physical mapping (like virtual memory!)       │
│ 2. Wear leveling: distribute writes evenly across cells     │
│ 3. Garbage collection: reclaim blocks with stale pages      │
│ 4. Bad block management: remap failed NAND blocks           │
│ 5. Write buffering: coalesce small writes in DRAM cache     │
│ 6. Read disturb mitigation: refresh pages near heavy reads  │
└────────────────────────────────────────────────────────────┘
```

### NVMe: The Protocol That Changed Everything

NVMe (Non-Volatile Memory Express) replaced AHCI/SCSI for SSDs. It was designed from scratch for parallelism and low latency:

```
SATA/AHCI (legacy):                    NVMe:
┌─────────────────────┐               ┌─────────────────────────┐
│ Single command queue │               │ Up to 65,535 queues      │
│ Max 32 outstanding   │               │ Max 65,536 per queue     │
│ commands             │               │                           │
│                       │               │ Queue 0 (admin)          │
│ CPU → AHCI register  │               │ Queue 1 (I/O, core 0)   │
│ → SATA cable         │               │ Queue 2 (I/O, core 1)   │
│ → SSD controller     │               │ Queue 3 (I/O, core 2)   │
│                       │               │ ...                       │
│ Bottleneck: single   │               │ Queue N (I/O, core N)   │
│ queue, high latency  │               │                           │
│ ~6 GB/s max (SATA    │               │ Directly on PCIe bus     │
│ III: 600 MB/s)       │               │ ~7-14 GB/s (PCIe 4/5)   │
└─────────────────────┘               └─────────────────────────┘

NVMe command submission (no syscall with io_uring SQPOLL!):
  1. Write command to submission queue (SQ) in host memory
  2. Ring doorbell register (single MMIO write to NVMe BAR)
  3. NVMe controller fetches command via DMA
  4. Controller processes command, reads/writes NAND
  5. Controller writes completion entry to CQ via DMA
  6. Controller raises MSI-X interrupt (or app polls)
```

### Performance Comparison

| Metric | HDD (7200 RPM) | SATA SSD | NVMe SSD | NVMe (latest) |
|--------|----------------|----------|----------|----------------|
| Random Read (4KB) | 100 IOPS | 50K IOPS | 500K-1M IOPS | 1-2M IOPS |
| Random Write (4KB) | 100 IOPS | 30K IOPS | 200K-500K IOPS | 500K-1M IOPS |
| Sequential Read | 150 MB/s | 550 MB/s | 3.5 GB/s | 7-14 GB/s |
| Sequential Write | 150 MB/s | 520 MB/s | 3.0 GB/s | 5-10 GB/s |
| Latency (read) | 5-10 ms | 50-100 μs | 10-20 μs | 5-10 μs |
| Power | 5-10W | 2-5W | 5-10W | 7-25W |

---

## 15. NUMA: Non-Uniform Memory Access

### What NUMA Is

In multi-socket servers, each CPU socket has its own local DRAM. Accessing memory attached to *another* socket crosses the interconnect (Intel UPI, AMD Infinity Fabric) and takes longer:

```
┌─────────────────────────────────────────────────────────────────┐
│                      NUMA Architecture                           │
│                                                                   │
│  NUMA Node 0                          NUMA Node 1                │
│  ┌──────────────────────┐            ┌──────────────────────┐   │
│  │  CPU Socket 0         │            │  CPU Socket 1         │   │
│  │  ┌────┬────┬────┬───┐│            │  ┌────┬────┬────┬───┐│   │
│  │  │ C0 │ C1 │ C2 │...││            │  │ C8 │ C9 │C10 │...││   │
│  │  └────┴────┴────┴───┘│            │  └────┴────┴────┴───┘│   │
│  │  L3 Cache (shared)    │            │  L3 Cache (shared)    │   │
│  └──────────┬───────────┘            └──────────┬───────────┘   │
│             │                                    │                │
│    ┌────────▼────────┐              ┌────────────▼────────┐     │
│    │  Local DRAM      │              │  Local DRAM          │     │
│    │  128 GB          │              │  128 GB              │     │
│    │  Access: ~100 ns │              │  Access: ~100 ns     │     │
│    └────────┬────────┘              └──────────┬──────────┘     │
│             │                                    │                │
│             │     ┌──────────────────┐           │                │
│             └────►│  Interconnect     │◄──────────┘                │
│                   │  (UPI / Infinity  │                            │
│                   │   Fabric)         │                            │
│                   │  ~150-200 ns      │                            │
│                   │  cross-node access│                            │
│                   └──────────────────┘                            │
│                                                                   │
│  Local access:  ~100 ns                                          │
│  Remote access: ~150-200 ns  (1.5-2x penalty)                   │
└─────────────────────────────────────────────────────────────────┘
```

### NUMA's Impact on Databases

```
Scenario: PostgreSQL with shared_buffers = 64 GB on 2-socket NUMA server

Default (NUMA-unaware):
  Linux interleaves pages across NUMA nodes (round-robin)
  Every other buffer pool page access crosses the interconnect
  Average latency: ~130 ns (mix of local and remote)

NUMA-aware (pin buffer pool to local node):
  All buffer pool pages on Node 0's DRAM
  Cores on Node 0: ~100 ns access (local)
  Cores on Node 1: ~150-200 ns access (all remote!) → WORSE for them

Best practice:
  Run one database instance per NUMA node:
    Instance 0: pinned to Node 0 cores + Node 0 memory
    Instance 1: pinned to Node 1 cores + Node 1 memory

  Or: NUMA interleave for buffer pool (predictable ~130 ns everywhere)
    numactl --interleave=all postgres
```

### NUMA-Aware Database Features

| Database | NUMA Strategy |
|----------|--------------|
| **PostgreSQL** | `numactl --interleave=all` recommended. No built-in NUMA awareness. |
| **MySQL** | `innodb_numa_interleave=1` (interleave buffer pool allocation) |
| **Oracle** | Automatic NUMA support since 11g. Per-node buffer pool partitions. |
| **SQL Server** | Hardware NUMA node aware. Memory allocations partitioned per node. |
| **ScyllaDB** | Seastar pins each shard to a core with local memory. Full NUMA awareness. |

---

## 16. CPU Features Databases Exploit

### SIMD (Single Instruction, Multiple Data)

Process multiple data elements in parallel with one instruction. Modern CPUs have 256-bit (AVX2) or 512-bit (AVX-512) SIMD registers.

```
Scalar comparison (checking 16 bytes, one at a time):
  CMP byte[0], 'A' → match?
  CMP byte[1], 'A' → match?
  CMP byte[2], 'A' → match?
  ... 16 iterations

SIMD comparison (checking 32 bytes at once with AVX2):
  VPCMPEQB ymm0, ymm1, ymm2   ← Compare 32 bytes simultaneously!
  VPMOVMSKB eax, ymm0           ← Extract match bits to integer

  1 instruction replaces 32 scalar comparisons.

Database uses:
┌────────────────────────────────────┬──────────────────────────────┐
│ Operation                          │ SIMD Speedup                  │
├────────────────────────────────────┼──────────────────────────────┤
│ String comparison (WHERE name = X) │ 4-16x (compare 16-64 bytes) │
│ Null bitmap scanning               │ 32x (check 256 bits at once)│
│ Hash computation                   │ 2-4x (CRC32C instruction)   │
│ Predicate evaluation (filters)     │ 4-8x (batch filter columns) │
│ Decompression (LZ4, Snappy)       │ 2-3x                         │
│ Aggregation (SUM, COUNT)          │ 4-8x (vectorized accumulate)│
│ Bloom filter probing              │ 8x+ (parallel hash lookup)  │
│ JSON parsing (simdjson)           │ 4-8x                         │
└────────────────────────────────────┴──────────────────────────────┘
```

### Branch Prediction and Branchless Programming

Modern CPUs predict which way branches (if/else) will go. Misprediction costs ~15-20 cycles (~5-7 ns):

```
// BRANCHY (unpredictable): ~5 ns per misprediction
for each row:
    if (row.age > 30)      ← CPU guesses TRUE or FALSE
        count++;            ← If wrong: flush pipeline, 15+ cycle penalty

// BRANCHLESS: Consistent cost, no misprediction
for each row:
    count += (row.age > 30);   ← Always executes, no branch to predict
    // Compiler turns this into CMOV or similar branchless instruction

When selectivity is ~50% (unpredictable):
  Branchy:     ~7 ns per row (frequent mispredictions)
  Branchless:  ~2 ns per row (always same cost)

When selectivity is ~99% (very predictable):
  Branchy:     ~1 ns per row (prediction almost always correct)
  Branchless:  ~2 ns per row (still same cost)
```

**Database application**: Vectorized query engines (DuckDB, ClickHouse, Velox) evaluate predicates in tight, branchless loops over column arrays. The scan function doesn't branch per-row; it processes thousands of values with SIMD and branchless arithmetic.

### Hardware Prefetching and Software Prefetch

```
// Hardware prefetcher: Detects sequential and strided access patterns.
// Automatically loads next cache lines before you need them.
// Works great for sequential scans.

// Software prefetch: Tell CPU to load data you'll need soon.
// Used when access pattern is non-obvious to hardware.

// B-tree traversal with software prefetch:
Node* traverse(Node* root, Key key) {
    Node* node = root;
    while (!node->is_leaf) {
        int child_idx = binary_search(node, key);
        Node* child = node->children[child_idx];

        // Prefetch the child node while we finish processing current node
        __builtin_prefetch(child, 0, 3);  // (addr, write=0, locality=high)

        // By the time we dereference child, it's already in L1 cache
        node = child;
    }
    return node;
}
```

### CRC32C and Hardware Checksums

```
// SSE 4.2 includes CRC32C instruction
// Used by databases for page checksums

// Software CRC32: ~1 GB/s
// Hardware CRC32C: ~20+ GB/s (single core)

// PostgreSQL: data page checksums use CRC32C when available
// RocksDB: block checksums use CRC32C
// ScyllaDB: all checksums use hardware CRC32C
```

### AES-NI (Hardware Encryption)

```
// AES-NI instructions accelerate encryption/decryption
// Software AES: ~500 MB/s
// AES-NI: ~5-10 GB/s (10-20x speedup)

// Used for:
// - Transparent Data Encryption (TDE) in Oracle, SQL Server, PostgreSQL
// - SSL/TLS connections to the database
// - Encrypted backups
// - Encrypted WAL
```

### CLFLUSH / CLWB (Cache Line Flush/Writeback)

Used with persistent memory (Intel Optane PMEM, CXL memory):

```
// With persistent memory (byte-addressable, durable):
// Writes go to CPU cache first, NOT immediately to PMEM
// Need explicit flush to ensure durability:

store(pmem_addr, data);     // Data in CPU cache (volatile!)
CLWB(pmem_addr);            // Write back cache line to PMEM
SFENCE();                    // Ensure ordering

// CLFLUSH: Flush + invalidate (slow: future reads miss cache)
// CLWB: Flush + keep in cache (fast: can still read from cache)
// CLFLUSHOPT: Non-ordered flush (can pipeline multiple flushes)
```

---

## 17. Kernel Bypass and Userspace I/O

### Why Bypass the Kernel?

At extreme performance levels (millions of IOPS, μs latencies), the kernel becomes the bottleneck. Each syscall costs hundreds of nanoseconds that could be spent doing actual work.

```
Kernel overhead per I/O operation:
┌──────────────────────────────────────────────┐
│ System call entry/exit:    200-400 ns         │
│ Page cache lookup/update:  50-200 ns          │
│ I/O scheduler:             50-100 ns          │
│ Block layer processing:    50-100 ns          │
│ Context switches:          1,000-5,000 ns     │
│ Interrupt handling:        2,000-5,000 ns     │
│                                                │
│ Total kernel overhead:     ~2-10 μs per I/O   │
│ NVMe hardware latency:    ~5-10 μs per I/O   │
│                                                │
│ Kernel overhead ≈ hardware latency!            │
│ Half your time is spent in the kernel!         │
└──────────────────────────────────────────────┘
```

### SPDK (Storage Performance Development Kit)

Intel's SPDK moves the NVMe driver entirely to userspace:

```
Traditional I/O Stack:                  SPDK:

Application                             Application
    │                                       │
    ▼                                       ▼
System Call                             SPDK Library (userspace)
    │                                       │
    ▼                                       │  (no kernel involvement!)
VFS Layer                                   │
    │                                       │
    ▼                                       │
Page Cache                                  │
    │                                       │
    ▼                                       │
Block Layer                                 │
    │                                       │
    ▼                                       │
NVMe Driver (kernel)                        │
    │                                       │
    ▼                                       ▼
NVMe Hardware                           NVMe Hardware
                                        (direct MMIO from userspace)

SPDK approach:
1. Unbind NVMe device from kernel driver
2. Map NVMe BAR (Base Address Register) into userspace via VFIO/UIO
3. Application directly writes to NVMe submission queues
4. Poll completion queues (no interrupts)
5. Zero kernel involvement for I/O

Results:
- Latency: ~2-3 μs (vs ~10 μs with kernel)
- IOPS: 10M+ per device (vs ~1M through kernel)
- CPU: 1 core can saturate an NVMe device
```

### Who Uses Kernel Bypass for Storage?

| System | Bypass Method | Use Case |
|--------|--------------|----------|
| **ScyllaDB** | SPDK + Seastar | Full userspace I/O for all data |
| **Ceph** | SPDK optional backend | High-performance OSD nodes |
| **RocksDB** | SPDK plugin (experimental) | Extreme IOPS workloads |
| **DPDK** | Network bypass (not storage) | Used with databases for network I/O |
| **FIO** | SPDK engine | Storage benchmarking |

---

## 18. Putting It All Together: A Page Read, Step by Step

A database reads page 42 from an NVMe SSD using direct I/O. Here is every step that happens at the hardware and OS level:

```
1. BUFFER POOL MISS
   Query executor calls: buffer_pool->get_page(table_id=5, page_no=42)
   Hash table lookup: page not in buffer pool.
   Decision: must read from disk.

2. FIND FREE FRAME IN BUFFER POOL
   Clock/LRU-K scan finds victim frame #371 (clean page, no write needed).
   Evict victim: remove from hash table, mark frame as "I/O in progress".

3. COMPUTE FILE OFFSET
   file_offset = page_no × page_size = 42 × 8192 = 344,064 bytes
   Buffer address: frame #371 at pool_base + 371 × 8192 (aligned to 4KB)

4. ISSUE pread SYSTEM CALL
   pread(fd=7, buf=0x7f001234000, count=8192, offset=344064)

   4a. CPU executes SYSCALL instruction
       - Switch to Ring 0 (kernel mode)
       - KPTI: load kernel page tables (~200 ns)
       - Save user registers to kernel stack

   4b. Kernel validates arguments
       - fd 7 is valid, buf is mapped, offset is aligned (O_DIRECT)

   4c. Filesystem (ext4) translates file offset → physical block
       - Extent tree lookup: file offset 344064 → block device LBA 0x1A3F00

   4d. Block layer creates bio (block I/O) request
       - LBA: 0x1A3F00, length: 16 sectors (8KB ÷ 512)
       - O_DIRECT: skip page cache, DMA target = user buffer

   4e. NVMe driver submits command to hardware queue
       - Write NVMe Read command to submission queue (SQ)
       - SQ entry: opcode=READ, LBA=0x1A3F00, length=16, PRP=0x7f001234000
       - Ring SQ doorbell register (MMIO write to NVMe BAR)

5. NVMe HARDWARE PROCESSES COMMAND
   5a. NVMe controller fetches SQ entry via DMA from host memory
   5b. FTL translates LBA 0x1A3F00 → physical NAND location
       - Die 2, Plane 0, Block 157, Page 43
   5c. Issue NAND read command on appropriate channel
   5d. NAND read latency: ~50-75 μs (raw NAND)
       - Charge sensing on floating gate transistors
       - ECC decoding (LDPC): ~5-10 μs
   5e. Data transferred to controller DRAM: 8192 bytes
   5f. Controller DMAs data to host memory at PRP address 0x7f001234000
   5g. Controller writes Completion Queue Entry (CQE)
   5h. Controller raises MSI-X interrupt

6. INTERRUPT HANDLING
   6a. CPU receives interrupt on target core
   6b. Save current execution state
   6c. Jump to NVMe interrupt handler
   6d. Handler reads CQE: status = SUCCESS, 8192 bytes transferred
   6e. Wake up blocked thread
   6f. Return from interrupt (~2-5 μs overhead)

7. RETURN TO USER SPACE
   7a. Kernel: pread returns 8192 (bytes read)
   7b. KPTI: restore user page tables (~200 ns)
   7c. SYSRET instruction: back to Ring 3
   7d. Total syscall + I/O time: ~10-20 μs

8. BUFFER POOL BOOKKEEPING
   8a. Verify page checksum (CRC32C hardware instruction)
       - Read checksum from page header
       - Compute CRC32C over page body
       - Compare. If mismatch → page corruption detected!
   8b. Insert page into buffer pool hash table
       - Key: (table_id=5, page_no=42), Value: frame #371
   8c. Set page pin count = 1 (caller holds a reference)
   8d. Update LRU/Clock metadata
   8e. Return pointer to frame #371 to query executor

9. VIRTUAL MEMORY DURING ACCESS
   When query executor accesses buf[0]:
   9a. CPU: virtual address 0x7f001234000
   9b. TLB lookup for virtual page 0x7f0012340
       - TLB HIT (likely — we just wrote to this address during DMA)
       - Physical frame: 0x3A2F1
   9c. L1 cache check for cache line at physical address
       - L1 MISS (new data, never accessed by CPU)
       - L2 MISS
       - L3 likely HIT (DMA coherency: modern CPUs snoop DMA)
       - Load cache line (64 bytes) into L1
   9d. Read completes: ~10-20 ns

10. QUERY EXECUTOR PROCESSES THE PAGE
    Parse slot array, find target tuple, evaluate predicate.
    All subsequent accesses to this 8KB page: L1/L2 cache hits (~1-4 ns).

    When done: unpin page (decrement pin count).
    Page remains in buffer pool for future queries.
```

### Total Time Breakdown

```
┌────────────────────────────────────────────────────────┐
│ Component                          │ Time              │
├────────────────────────────────────┼───────────────────┤
│ Buffer pool hash lookup (miss)     │ ~50 ns            │
│ Find victim frame                  │ ~100-500 ns       │
│ System call overhead (entry+exit)  │ ~400 ns           │
│ Filesystem extent lookup           │ ~200 ns           │
│ Block layer + NVMe submission      │ ~200 ns           │
│ NVMe hardware (NAND read + DMA)   │ ~10,000 ns (10 μs)│
│ Interrupt handling                 │ ~3,000 ns (3 μs)  │
│ Buffer pool bookkeeping            │ ~200 ns           │
│ CRC32C checksum verification       │ ~100 ns           │
├────────────────────────────────────┼───────────────────┤
│ TOTAL                              │ ~14-15 μs         │
│                                    │                   │
│ Of which is actual hardware I/O:   │ ~10 μs (67%)      │
│ Of which is software overhead:     │ ~4-5 μs (33%)     │
└────────────────────────────────────┴───────────────────┘

Compare with buffer pool HIT: ~50-100 ns (100-300x faster!)
This is why the buffer pool hit ratio is the most important database metric.
```

---

## 19. OS CPU Scheduling and Why Databases Care

### How the Linux Scheduler Works

The Linux kernel decides which thread runs on which CPU core and for how long. Every nanosecond a database thread spends *waiting* to be scheduled is a nanosecond a query takes longer. Understanding the scheduler explains latency spikes, tail latency, and why databases fight for CPU time.

### Thread States

```
Every thread on Linux is in one of these states:

TASK_RUNNING (R)          ── On a CPU or in the "run queue" waiting for a CPU
TASK_INTERRUPTIBLE (S)    ── Sleeping, will wake on signal or event (e.g., I/O complete)
TASK_UNINTERRUPTIBLE (D)  ── Sleeping, will NOT wake on signal (e.g., disk I/O in progress)
TASK_STOPPED (T)          ── Stopped (SIGSTOP / ptrace)
TASK_ZOMBIE (Z)           ── Exited, parent hasn't called wait()

State machine for a database worker thread:

 ┌──────────┐   pread() syscall   ┌───────────────────────┐
 │ RUNNING  │ ────────────────►  │ UNINTERRUPTIBLE (D)    │
 │ (on CPU) │                     │ waiting for disk I/O   │
 └──────────┘                     └───────────┬───────────┘
      ▲                                        │ I/O complete
      │                                        │ (interrupt)
      │                                        ▼
      │                           ┌───────────────────────┐
      │    scheduler picks it     │ RUNNING (in run queue) │
      │◄──────────────────────── │ waiting for CPU        │
      │                           └───────────────────────┘

The "D" state is critical: you've seen "D" state processes in `top` —
these are threads stuck in I/O. They CANNOT be killed (not even SIGKILL)
because the kernel must complete the I/O operation first.
```

### CFS: Completely Fair Scheduler

Linux's default scheduler (since 2.6.23, 2007) tries to give every thread a "fair" share of CPU time. It tracks **virtual runtime** (vruntime) — how much CPU time a thread has consumed, weighted by priority.

```
CFS Core Idea:
  - Each thread has a "vruntime" counter
  - Thread that has used LEAST vruntime runs next
  - Higher priority (lower nice) → vruntime ticks slower → gets more CPU
  - Lower priority (higher nice) → vruntime ticks faster → gets less CPU

Data Structure: Red-Black Tree (sorted by vruntime)

                    vruntime
        ┌──────────────────────────────────────────┐
        │                                            │
     ┌──┴──┐                                        │
     │ 105 │  ← Thread A (ran a lot recently)       │
     └──┬──┘                                        │
   ┌────┴────┐                                      │
┌──┴──┐   ┌──┴──┐                                   │
│  87 │   │  92 │                                   │
└─────┘   └─────┘                                   │
  ▲                                                   │
  │                                                   │
  Thread C: LOWEST vruntime → RUNS NEXT             │
                                                      │
Time Slice (scheduling granularity):                  │
  Default: ~4 ms (sysctl kernel.sched_min_granularity_ns) │
  With 4 runnable threads: each gets ~4 ms before preemption │
  This is NOT a fixed quantum — CFS adapts based on load     │
```

### EEVDF: Earliest Eligible Virtual Deadline First (Linux 6.6+)

Linux 6.6 (2023) replaced CFS internals with EEVDF — same fairness goals but better latency guarantees:

```
CFS problem: A thread that just woke up has low vruntime and immediately
preempts the running thread. This is unfair to computation-heavy threads
and causes unnecessary context switches.

EEVDF improvement:
  - Each thread has a "virtual deadline" = when it's entitled to its next slice
  - Scheduler picks the thread with the EARLIEST ELIGIBLE deadline
  - "Eligible" = the thread has accumulated enough wait time to deserve CPU
  - Result: latency-sensitive threads (short bursts) get quick response
           compute-heavy threads (long runs) get uninterrupted chunks

For databases:
  CFS:   Query parser (short CPU burst) wakes → preempts long-running scan
         → scan loses its warm caches → resumes with cold cache penalties
  EEVDF: Query parser wakes → scheduled at its virtual deadline
         → scan keeps running if its deadline hasn't passed yet
         → fewer unnecessary preemptions, better cache behavior
```

### Priority, Nice, and Scheduling Classes

```
Linux scheduling classes (highest to lowest priority):

┌──────────────────────────────────────────────────────────────────────┐
│  SCHED_DEADLINE    │ Real-time, guaranteed CPU within a deadline      │
│                    │ Parameters: runtime, deadline, period            │
│                    │ Example: "I need 1ms of CPU every 10ms"         │
├────────────────────┼─────────────────────────────────────────────────┤
│  SCHED_FIFO        │ Real-time FIFO — runs until it yields/blocks    │
│                    │ Priority: 1-99 (higher = runs first)            │
│                    │ Starves ALL lower-priority threads!             │
├────────────────────┼─────────────────────────────────────────────────┤
│  SCHED_RR          │ Real-time Round-Robin — like FIFO + time slice  │
│                    │ Same priority threads rotate                     │
│                    │ Priority: 1-99                                   │
├────────────────────┼─────────────────────────────────────────────────┤
│  SCHED_OTHER (CFS) │ Default for all normal threads                  │
│  (aka SCHED_NORMAL)│ Nice value: -20 (highest) to +19 (lowest)      │
│                    │ Nice -20 gets ~20x more CPU than nice +19       │
├────────────────────┼─────────────────────────────────────────────────┤
│  SCHED_BATCH       │ Like CFS but hints "I'm not interactive"       │
│                    │ Scheduler gives slightly less preemption         │
├────────────────────┼─────────────────────────────────────────────────┤
│  SCHED_IDLE        │ Only runs when NO other thread wants CPU        │
│                    │ For truly background work (defrag, statistics)  │
└──────────────────────────────────────────────────────────────────────┘

Nice values for database processes:

  nice -20 postgres    ← Highest normal priority. Use for critical DB.
  nice  0  postgres    ← Default. Fine for most setups.
  nice 19  pg_dump     ← Backup — shouldn't compete with queries.

  ionice -c 1 -n 0 postgres  ← Also set I/O priority (see I/O scheduling)
```

### The Run Queue and CPU Affinity

```
Each CPU core has its own run queue. The scheduler balances threads across cores:

Core 0 Run Queue          Core 1 Run Queue          Core 2 Run Queue
┌──────────────┐          ┌──────────────┐          ┌──────────────┐
│ DB Worker 1  │ Running  │ DB Worker 2  │ Running  │ Checkpoint   │ Running
│ DB Worker 5  │ Waiting  │ Compaction   │ Waiting  │              │
│ WAL Writer   │ Waiting  │              │          │              │
└──────────────┘          └──────────────┘          └──────────────┘

Load balancing:
  - Kernel periodically checks if queues are unbalanced
  - Migrates threads from busy cores to idle cores
  - PROBLEM: Thread migration = cold L1/L2 cache on new core!

CPU Affinity (pinning):
  Databases can pin threads to specific cores to avoid migration:

  taskset -c 0-7 postgres          # Pin postgres to cores 0-7
  pthread_setaffinity_np(thread, sizeof(cpuset), &cpuset);  # Per-thread

  ScyllaDB: One shard per core, each shard pinned to its core.
  No thread migration, no lock contention, no cache pollution.

Per-core cache state after thread migration:
  Before migration (Core 0):  L1 warm, L2 warm, TLB populated  → ~1 ns access
  After migration (Core 3):   L1 cold, L2 cold, TLB empty      → ~100 ns access
  Cost: ~10,000 cache line reloads × 50 ns = ~500 μs penalty (invisible!)
```

### Priority Inversion: A Real Database Problem

```
Priority inversion: A HIGH-priority thread waits for a LOW-priority thread
that's preempted by a MEDIUM-priority thread.

Example in a database:

  Thread H (high priority): WAL writer (latency-critical for commits)
  Thread M (medium priority): Background analytics query
  Thread L (low priority): Statistics collector

  1. Thread L acquires buffer pool latch (lightweight lock)
  2. Thread L gets preempted — scheduler runs Thread M (higher priority)
  3. Thread H wakes up, needs the same buffer pool latch
  4. Thread H BLOCKS waiting for Thread L to release the latch
  5. Thread L can't run because Thread M is using the CPU
  6. Thread H (highest priority) is effectively blocked by Thread M!

     Thread H: ████████░░░░░░░░░░░░░░░░░░░░  BLOCKED (waiting for latch)
     Thread M: ░░░░░░░░████████████████████  RUNNING (doesn't need latch)
     Thread L: ░░░░░░░░░░░░░░░░░░░░░░░░░░░░  PREEMPTED (holds latch!)

Solutions:
  - Priority inheritance: When H blocks on L's lock, temporarily boost L to H's priority
    Linux futexes support PI: PTHREAD_PRIO_INHERIT
  - Lock-free data structures: Avoid the lock entirely
  - Short critical sections: L holds the latch for so little time it rarely gets preempted
  - Databases mostly use: short critical sections + spin-then-yield latches
```

### Scheduler Latency: The Enemy of Tail Latency

```
A thread that becomes runnable (I/O completed, lock released) doesn't
run immediately. It goes to the run queue and WAITS for the scheduler
to pick it up.

Schedule latency = time from "thread becomes runnable" to "thread runs on CPU"

┌─────────────────────────────────────────────────────────────────────┐
│                                                                       │
│  I/O complete!                                                       │
│  Thread wakes up (TASK_RUNNING)                                      │
│       │                                                               │
│       │  ┌── Schedule Latency ──┐                                    │
│       ▼  ▼                      ▼                                    │
│  ─────[WAKE]══════WAITING═══════[RUN]──────────────────────►        │
│                                                                       │
│  This wait can be:                                                   │
│    Best case:  ~1-5 μs  (idle core available, immediate dispatch)   │
│    Typical:    ~10-50 μs (core busy, wait for time slice to expire) │
│    Worst case: ~1-10 ms  (all cores busy, long time slices)         │
│                                                                       │
│  For a query with p99 latency target of 1 ms:                       │
│    If scheduler adds 500 μs, that's 50% of your budget GONE.       │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘

Tuning scheduler latency for databases:
  # Reduce minimum time slice (more preemptions, but lower schedule latency)
  sysctl kernel.sched_min_granularity_ns = 1000000    # 1ms (default: 3ms)
  sysctl kernel.sched_wakeup_granularity_ns = 500000  # 0.5ms (default: 4ms)

  # Or: use isolcpus to reserve cores exclusively for the database
  # Boot parameter: isolcpus=4-15
  # Cores 4-15 will ONLY run threads explicitly pinned to them
  # No kernel threads, no other processes — zero scheduling contention
```

### cgroups: Resource Isolation for Database Workloads

```
cgroups (control groups) let you partition CPU, memory, and I/O
among groups of processes. Essential for multi-tenant database deployments.

cgroup v2 CPU controller:

/sys/fs/cgroup/
├── database.slice/
│   ├── cpu.weight = 500          # 5x more CPU than default (100)
│   ├── cpu.max = 800000 1000000  # Max 80% of one CPU (800ms per 1000ms)
│   ├── cpuset.cpus = 0-15        # Pin to cores 0-15
│   ├── cpuset.mems = 0           # NUMA node 0 memory only
│   └── memory.max = 64G          # Hard memory limit
│
├── analytics.slice/
│   ├── cpu.weight = 100          # Default priority
│   ├── cpu.max = 400000 1000000  # Max 40% of one CPU
│   └── cpuset.cpus = 16-23      # Isolated to different cores
│
└── background.slice/
    ├── cpu.weight = 10           # Very low priority
    └── io.weight = 10            # Very low I/O priority

This prevents:
  - Analytics queries starving OLTP workload
  - Backup processes causing latency spikes
  - Runaway queries consuming all CPU
```

---

## 20. I/O Scheduling: From Elevator Algorithms to Multi-Queue

### Why I/O Scheduling Exists

The disk is the slowest component in the system. The I/O scheduler sits between the filesystem and the device driver, **reordering** and **merging** requests to maximize throughput and fairness.

```
Without I/O scheduler:                With I/O scheduler:

Requests arrive:                      Requests reordered:
  1. Read LBA 1000                      1. Read LBA 1000  ┐
  2. Read LBA 9000                      2. Read LBA 1001  ┘ merged!
  3. Read LBA 1001                      3. Read LBA 5000
  4. Read LBA 5000                      4. Read LBA 9000

HDD: 4 seeks (expensive!)             HDD: 3 seeks (1+3 merged, sequential order)
SSD: 4 random reads (fast anyway)     SSD: 3 reads (merge still helps, fewer commands)
```

### The Classic Elevator Algorithm (Legacy Context)

```
The original disk scheduler worked like an elevator:

Disk arm position: ──────────────────────────────────────────►
                   LBA 0                               LBA MAX

SCAN (Elevator):
  Arm moves in one direction, servicing requests along the way.
  When it reaches the end, it reverses direction.

  Position: ████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
            ↑ arm here

  Pending requests: LBA 100, 500, 200, 800, 300

  Service order: 200, 300, 500, 800, → reverse → 100

  Like an elevator that goes up, stopping at all requested floors,
  then goes back down.

This was critical for HDDs. For SSDs? Not really needed — no arm to move.
```

### Linux I/O Schedulers (Modern Multi-Queue)

Linux 5.0+ removed all legacy single-queue schedulers. Modern Linux uses the **blk-mq** (multi-queue block layer) with these schedulers:

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Linux blk-mq Architecture                          │
│                                                                        │
│  Per-CPU software queues          Hardware dispatch queues             │
│                                                                        │
│  CPU 0: [req][req][req]──┐                                            │
│  CPU 1: [req][req]───────┤       ┌─────────────────────┐             │
│  CPU 2: [req][req][req]──┼──────►│  I/O Scheduler      │             │
│  CPU 3: [req]────────────┤       │  (merge, reorder,   │             │
│  ...                      │       │   prioritize)       │             │
│  CPU N: [req][req]───────┘       └──────────┬──────────┘             │
│                                              │                         │
│                                   ┌──────────▼──────────┐             │
│                                   │  Hardware Queue 0    │ → NVMe SQ 0│
│                                   │  Hardware Queue 1    │ → NVMe SQ 1│
│                                   │  Hardware Queue N    │ → NVMe SQ N│
│                                   └─────────────────────┘             │
│                                                                        │
│  Key: Each CPU submits to its own queue (no contention!)              │
│  Scheduler merges/reorders, then dispatches to hardware queues.       │
└──────────────────────────────────────────────────────────────────────┘
```

### Scheduler: `none` (No-Op / Passthrough)

```
What it does: Nothing. Passes requests directly to hardware queues.
              Only merges adjacent requests (bio merging at block layer).

When to use:
  - NVMe SSDs: The device has its own internal scheduler (FTL).
               Adding another layer of scheduling is pure overhead.
  - io_uring with high queue depth: Requests already optimally batched.
  - SPDK/kernel bypass: No block layer involved anyway.

Performance:
  - Lowest CPU overhead (no sorting, no bookkeeping)
  - Lowest latency (no extra queuing delay)
  - Highest throughput for devices with internal parallelism (NVMe)
  - UNFAIR: A heavy writer can starve readers. No priority enforcement.

Set it:
  echo none > /sys/block/nvme0n1/queue/scheduler
```

### Scheduler: `mq-deadline`

```
What it does:
  - Maintains SORTED queues for reads and writes (by LBA)
  - Each request gets a DEADLINE (default: reads=500ms, writes=5000ms)
  - Normally services requests in LBA order (like elevator)
  - BUT: if any request's deadline expires → service it immediately

┌───────────────────────────────────────────────────────────────────┐
│  mq-deadline internals                                              │
│                                                                      │
│  Read Queue (sorted by LBA):    [100] [200] [500] [800] [1200]    │
│  Read FIFO (sorted by deadline): [500ms] [490ms] [450ms] [400ms]  │
│                                                                      │
│  Write Queue (sorted by LBA):   [50] [300] [700]                   │
│  Write FIFO (sorted by deadline):[4.5s] [4.0s] [3.5s]             │
│                                                                      │
│  Algorithm:                                                          │
│  1. Check if any read deadline expired → service that read          │
│  2. Check if any write deadline expired → service that write        │
│  3. Otherwise: dispatch from sorted read queue (LBA order)          │
│  4. After servicing 'writes_starved' reads (default 2), do a write │
│                                                                      │
│  Reads prioritized over writes (reads are latency-sensitive)        │
│  Deadlines prevent starvation of any individual request             │
└───────────────────────────────────────────────────────────────────┘

When to use:
  - SATA SSDs: Good balance of throughput and latency fairness
  - HDDs: LBA sorting reduces seeks, deadlines prevent starvation
  - Databases that need predictable latency (no request starves)

Set it:
  echo mq-deadline > /sys/block/sda/queue/scheduler

Tunable parameters:
  /sys/block/sda/queue/iosched/
  ├── read_expire = 500        # Read deadline in ms
  ├── write_expire = 5000      # Write deadline in ms
  ├── writes_starved = 2       # Service N reads before 1 write
  └── fifo_batch = 16          # Batch size before checking deadlines
```

### Scheduler: `bfq` (Budget Fair Queuing)

```
What it does:
  - Per-process I/O budgets (like CFS for I/O)
  - Each process gets a "budget" (number of sectors)
  - Process with lowest virtual time gets to dispatch next
  - Guarantees fairness: no process can monopolize the device

When to use:
  - Multi-tenant environments (multiple databases sharing storage)
  - Desktop/interactive (ensures responsiveness)
  - HDDs (BFQ's LBA batching reduces seeks)

When NOT to use:
  - High-IOPS NVMe: BFQ's per-request bookkeeping adds ~5 μs overhead
    At 1M IOPS, that's 5 seconds of CPU per second (!)
  - Single-database servers: No need for fairness, just go fast

Set it:
  echo bfq > /sys/block/sda/queue/scheduler

  # Give database higher I/O priority:
  ionice -c 1 -n 0 -p $(pidof postgres)   # Real-time I/O class, highest
  ionice -c 2 -n 0 -p $(pidof postgres)   # Best-effort, highest priority
  ionice -c 3 -p $(pidof pg_dump)         # Idle — only when device is free
```

### Scheduler: `kyber`

```
What it does:
  - Lightweight, latency-focused scheduler
  - Two queues: reads (latency-sensitive) and writes (throughput)
  - Monitors completion latencies and THROTTLES queue depth to hit targets
  - If read latency exceeds target → reduce number of outstanding writes

Kyber's approach:
  ┌──────────────────────────────────────────────────────────┐
  │  Target latencies:                                        │
  │    Reads:  2 ms  (default, tunable)                      │
  │    Writes: 10 ms (default, tunable)                      │
  │                                                            │
  │  Mechanism:                                                │
  │    Monitor actual completion latencies                     │
  │    If reads taking > 2 ms:                                │
  │      → Reduce write queue depth (fewer outstanding writes)│
  │      → More device bandwidth available for reads          │
  │    If reads well under 2 ms:                              │
  │      → Increase write queue depth (more write throughput) │
  │                                                            │
  │  Very low CPU overhead (no per-request sorting)           │
  │  Good for NVMe where latency control matters              │
  └──────────────────────────────────────────────────────────┘

When to use:
  - NVMe SSDs that need latency control (not just raw throughput)
  - Mixed read/write workloads where reads are latency-sensitive

Set it:
  echo kyber > /sys/block/nvme0n1/queue/scheduler
```

### Which Scheduler for Which Database Setup?

```
┌─────────────────────┬──────────────┬──────────────────────────────────┐
│ Storage Device       │ Scheduler    │ Rationale                         │
├─────────────────────┼──────────────┼──────────────────────────────────┤
│ NVMe (dedicated DB)  │ none         │ Lowest overhead, device has own   │
│                      │              │ scheduler. Most databases use this│
├─────────────────────┼──────────────┼──────────────────────────────────┤
│ NVMe (shared/multi-  │ kyber or     │ Need fairness or latency control │
│ tenant)              │ mq-deadline  │ between competing workloads       │
├─────────────────────┼──────────────┼──────────────────────────────────┤
│ SATA SSD             │ mq-deadline  │ Deadline prevents starvation,    │
│                      │              │ read priority helps query latency │
├─────────────────────┼──────────────┼──────────────────────────────────┤
│ HDD                  │ mq-deadline  │ LBA ordering critical for seeks  │
│                      │ or bfq       │ BFQ if sharing between workloads │
├─────────────────────┼──────────────┼──────────────────────────────────┤
│ NVMe + io_uring      │ none         │ io_uring already batches well,   │
│                      │              │ scheduler adds pure overhead     │
├─────────────────────┼──────────────┼──────────────────────────────────┤
│ NVMe + SPDK          │ N/A          │ Kernel block layer bypassed      │
│                      │              │ entirely. No scheduler.          │
└─────────────────────┴──────────────┴──────────────────────────────────┘
```

### I/O Priority Classes (ionice)

```
Linux supports per-process I/O priority (used by BFQ and CFQ):

Class 1 — Real-Time:
  Always serviced before other classes.
  Priority levels 0-7 (0 = highest).
  DANGER: Can starve other processes. Use only for critical DB.

Class 2 — Best-Effort (default):
  Fair scheduling among all best-effort processes.
  Priority levels 0-7 (derived from nice value if not set).
  This is what 99% of database processes should use.

Class 3 — Idle:
  Only gets I/O when NO other process wants the device.
  Perfect for: backups, VACUUM, compaction (if you can tolerate slowness).

Usage:
  ionice -c 2 -n 0 -p $(pidof postgres)    # Best-effort, highest
  ionice -c 3 -p $(pidof pg_basebackup)    # Idle class for backup
```

### Queue Depth: The Hidden Performance Lever

```
Queue depth = number of I/O requests "in flight" simultaneously.

Too shallow (queue depth = 1):
  ┌──────┐     ┌──────┐     ┌──────┐     ┌──────┐
  │ Req 1 │────│ Wait │────│ Req 2 │────│ Wait │────►
  └──────┘     └──────┘     └──────┘     └──────┘
  Device is idle between requests. Terrible throughput.
  NVMe at QD=1: ~50K IOPS, ~10 μs latency

Too deep (queue depth = 1024):
  ████████████████████████████████████████████████
  Device saturated, requests queuing up.
  NVMe at QD=1024: ~1M IOPS, but ~1 ms latency (queuing delay!)

Sweet spot depends on workload:
  OLTP (latency-sensitive):   QD 4-32 per device
  OLAP (throughput):          QD 64-256 per device
  Compaction/background I/O:  QD 1-4 (stay out of the way)

  NVMe can handle 65,536 outstanding commands.
  The question is: how much queuing delay can your workload tolerate?

  ┌────────────────────────────────────────────────────┐
  │ Queue Depth vs Latency/Throughput (typical NVMe)   │
  │                                                      │
  │ Throughput ──►                                       │
  │    │                     ┌─────────────────────     │
  │    │                 ┌───┘                           │
  │    │             ┌───┘         ← Throughput plateau │
  │    │         ┌───┘                                   │
  │    │     ┌───┘                                       │
  │    │ ┌───┘                                           │
  │    └─┴──────────────────────────────────── QD ──►  │
  │    1    4   16   32   64  128  256  512             │
  │                                                      │
  │ Latency ──►                                          │
  │    │                                 ┌───────────── │
  │    │                             ┌───┘               │
  │    │                         ┌───┘                   │
  │    │                     ┌───┘                       │
  │    │ ────────────────────┘  ← Latency knee          │
  │    └──────────────────────────────────── QD ──►    │
  │    1    4   16   32   64  128  256  512             │
  │                                                      │
  │ Sweet spot: QD where throughput plateaus but         │
  │ latency hasn't yet exploded (~16-64 for most NVMe) │
  └────────────────────────────────────────────────────┘
```

---

## 21. Database-Level Scheduling: Thread Models and Userspace Schedulers

### Why Databases Build Their Own Schedulers

The OS scheduler is general-purpose. It doesn't know that:
- A WAL writer is more important than a background compaction thread
- A short OLTP query should preempt a long OLAP scan
- A lock-holding thread should not be preempted
- Certain threads should run on NUMA-local cores

Databases implement **application-level scheduling** to make these decisions.

### Thread Model 1: Process-Per-Connection (PostgreSQL)

```
┌──────────────────────────────────────────────────────────────────┐
│  PostgreSQL: Fork-on-Connect                                       │
│                                                                     │
│  Postmaster (main process)                                         │
│       │                                                             │
│       ├── fork() → Backend 1  (handles Client 1)                   │
│       ├── fork() → Backend 2  (handles Client 2)                   │
│       ├── fork() → Backend 3  (handles Client 3)                   │
│       ├── ...                                                       │
│       ├── fork() → Backend N  (handles Client N)                   │
│       │                                                             │
│       ├── Background Writer (writes dirty buffers)                 │
│       ├── WAL Writer (flushes WAL)                                 │
│       ├── Checkpointer (periodic checkpoint)                       │
│       ├── Autovacuum Launcher → Autovacuum Workers                 │
│       └── Stats Collector                                           │
│                                                                     │
│  Problems with 1000 connections:                                   │
│  - 1000 processes × 10 MB private memory = 10 GB overhead         │
│  - OS scheduler: 1000 runnable processes = constant context switch │
│  - Context switch cost: 5-50 μs each (cache cold on resume)       │
│  - Each process may hold shared buffer pool latches               │
│  - Lock contention on shared memory structures explodes            │
│                                                                     │
│  Typical fix: connection pooler (PgBouncer, Odyssey)              │
│    PgBouncer:  500 app connections → 50 backend connections       │
│    Reduces: context switches, memory, lock contention              │
└──────────────────────────────────────────────────────────────────┘
```

### Thread Model 2: Thread Pool (MySQL, Oracle)

```
┌──────────────────────────────────────────────────────────────────┐
│  MySQL / Oracle: Thread Pool                                       │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────┐      │
│  │               Connection Queue                            │      │
│  │  [Conn1] [Conn2] [Conn3] ... [Conn 10,000]              │      │
│  └────────────────────┬────────────────────────────────────┘      │
│                        │                                            │
│  ┌─────────────────────▼────────────────────────────────────┐     │
│  │                  Thread Pool                                │     │
│  │                                                              │     │
│  │  Worker Group 0 (cores 0-3):                                │     │
│  │    Worker 0: executing query for Conn 42                    │     │
│  │    Worker 1: executing query for Conn 107                   │     │
│  │    Worker 2: waiting for I/O (pread on data file)          │     │
│  │    Worker 3: idle → picks up Conn 5003 from queue          │     │
│  │                                                              │     │
│  │  Worker Group 1 (cores 4-7):                                │     │
│  │    Worker 4: executing query for Conn 8                     │     │
│  │    Worker 5: executing query for Conn 2191                  │     │
│  │    Worker 6: idle → picks up Conn 999 from queue           │     │
│  │    Worker 7: idle                                            │     │
│  │                                                              │     │
│  │  Total: 8 workers handling 10,000 connections               │     │
│  │  Context switches: only 8 (not 10,000!)                     │     │
│  └──────────────────────────────────────────────────────────┘     │
│                                                                     │
│  Oracle thread pool adds:                                          │
│  - Short-query queue (< 100 μs) vs long-query queue               │
│  - If a query runs too long → moved to long queue                  │
│  - Short queries never starved by long analytics scans             │
│                                                                     │
│  MySQL thread_pool_size = number of worker groups (default: CPUs)  │
│  MySQL thread_pool_stall_limit = time before creating new thread   │
└──────────────────────────────────────────────────────────────────┘
```

### Thread Model 3: Shard-Per-Core (ScyllaDB / Seastar)

```
┌──────────────────────────────────────────────────────────────────┐
│  ScyllaDB / Seastar: One Shard Per Core                           │
│                                                                     │
│  Core 0                Core 1                Core 2               │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     │
│  │ Shard 0       │     │ Shard 1       │     │ Shard 2       │     │
│  │               │     │               │     │               │     │
│  │ Own:          │     │ Own:          │     │ Own:          │     │
│  │ - Buffer pool │     │ - Buffer pool │     │ - Buffer pool │     │
│  │ - Memtable    │     │ - Memtable    │     │ - Memtable    │     │
│  │ - Connections │     │ - Connections │     │ - Connections │     │
│  │ - Task queue  │     │ - Task queue  │     │ - Task queue  │     │
│  │               │     │               │     │               │     │
│  │ No locks!     │     │ No locks!     │     │ No locks!     │     │
│  │ No sharing!   │     │ No sharing!   │     │ No sharing!   │     │
│  └──────────────┘     └──────────────┘     └──────────────┘     │
│        │                     │                     │               │
│        │    Cross-shard message passing (rare)      │               │
│        └─────────────────────┴─────────────────────┘               │
│                                                                     │
│  Each shard is a single-threaded event loop:                       │
│                                                                     │
│  while (true) {                                                    │
│      // 1. Poll for completed I/O (io_uring / SPDK)               │
│      // 2. Poll for network events (epoll / io_uring)             │
│      // 3. Run ready tasks from task queue                         │
│      // 4. Run timer callbacks                                     │
│      // 5. Repeat — NEVER BLOCK                                   │
│  }                                                                  │
│                                                                     │
│  Benefits:                                                          │
│  - Zero context switches (1 thread per core, never sleeps)         │
│  - Zero lock contention (nothing shared between shards)            │
│  - Zero cache pollution (no other threads touch your L1/L2)       │
│  - NUMA-perfect (each shard uses only local memory)               │
│  - Predictable latency (no OS scheduler interference)             │
│                                                                     │
│  Drawback:                                                          │
│  - Complexity: must implement own I/O scheduler, timer system,    │
│    task scheduling, memory allocator, cross-shard communication   │
│  - Cooperative: a long task can block the entire shard            │
│    (must manually yield: seastar::yield())                         │
└──────────────────────────────────────────────────────────────────┘
```

### Userspace Scheduling: Coroutines and Fibers

OS context switches cost 5-50 μs. Userspace task switches cost ~10-100 ns. That's 100-1000x cheaper.

```
OS Thread Context Switch:
  - Save ALL registers (including AVX-512: 2KB of state!)
  - Switch kernel stack
  - Switch page tables (if cross-process)
  - Invalidate branch predictor state
  - Cold L1/L2/TLB on resume
  → 5,000 - 50,000 ns

Userspace Coroutine/Fiber Switch:
  - Save a few callee-saved registers (6-8 registers on x86-64)
  - Swap stack pointer
  - No kernel involvement, no privilege transition
  - Same address space, same page tables, same TLB
  - L1/L2 likely still warm (same core, just different stack)
  → 10 - 100 ns

This is why modern databases increasingly use coroutines:

┌──────────────────────────────────────────────────────────────────┐
│  Coroutine-based database I/O:                                    │
│                                                                     │
│  Worker Thread (1 per core):                                       │
│                                                                     │
│  ┌─────────┐  yield()  ┌─────────┐  yield()  ┌─────────┐        │
│  │ Query A  │──────────►│ Query B  │──────────►│ Query C  │──►...  │
│  │ needs I/O│          │ needs I/O│          │ CPU work │        │
│  └────┬────┘          └────┬────┘          └─────────┘        │
│       │                     │                                      │
│       │  I/O submitted      │  I/O submitted                      │
│       │  (io_uring)         │  (io_uring)                         │
│       │                     │                                      │
│       ▼                     ▼                                      │
│  I/O completes          I/O completes                              │
│  Resume Query A          Resume Query B                            │
│                                                                     │
│  1 OS thread handles THOUSANDS of queries concurrently!           │
│  Each query is a coroutine that yields when it needs I/O.         │
│  No OS context switch between queries, just coroutine swap.       │
└──────────────────────────────────────────────────────────────────┘
```

### Database Coroutine/Fiber Adoption

| Database | Approach | Details |
|----------|---------|---------|
| **ScyllaDB** | Seastar futures/promises | Continuations + coroutines (C++20). Every I/O is async, never blocks. |
| **TiDB/TiKV** | Go goroutines | Go runtime multiplexes goroutines onto OS threads. ~2KB per goroutine. |
| **CockroachDB** | Go goroutines | Same Go model. Scheduler work-steals across threads. |
| **PostgreSQL** | Traditional processes | No coroutines. Each backend is a full process. Actively discussed for future. |
| **MySQL** | Thread pool + async | No coroutines yet. Thread pool reduces OS scheduling cost. |
| **DuckDB** | Task-based parallelism | Pipeline-based tasks with morsel-driven scheduling. |
| **FoundationDB** | Flow (actor model) | Custom deterministic concurrency framework with simulation testing. |
| **SingleStore** | Lock-free + coroutines | Memory-optimized tables use lock-free skiplist, async I/O for disk tables. |

### Task Prioritization and Admission Control

```
Not all database work is equal. A well-designed internal scheduler
prioritizes tasks based on their importance:

┌──────────────────────────────────────────────────────────────────┐
│  Priority Queue (typical database task scheduler)                  │
│                                                                     │
│  CRITICAL (run immediately):                                       │
│    - WAL sync (commit latency depends on this)                     │
│    - Lock release (other transactions blocked)                     │
│    - Heartbeat/liveness checks                                     │
│                                                                     │
│  HIGH (run next):                                                   │
│    - OLTP queries (short, user-facing)                              │
│    - Replication apply (keeps replicas up-to-date)                 │
│                                                                     │
│  MEDIUM (run when resources available):                            │
│    - OLAP queries (long, analytical)                                │
│    - Index builds (can run in background)                           │
│                                                                     │
│  LOW (run when idle):                                               │
│    - Compaction (LSM tree maintenance)                              │
│    - Statistics gathering (optimizer stats)                         │
│    - VACUUM/garbage collection                                      │
│                                                                     │
│  BACKGROUND (run when nothing else needs CPU/I/O):                 │
│    - Backups                                                        │
│    - Defragmentation                                                │
│    - Pre-warming caches                                             │
└──────────────────────────────────────────────────────────────────┘

Admission Control:
  Don't start work you can't finish. If the system is overloaded,
  REJECT new queries rather than making everything slow.

  ┌──────────────────────────────────────────────────────────────┐
  │  if active_queries >= max_concurrent_queries:                  │
  │      if query.priority == HIGH:                                │
  │          preempt_lowest_priority_query()                       │
  │      else:                                                      │
  │          queue_or_reject(query)                                 │
  │                                                                  │
  │  RocksDB rate limiter:                                          │
  │    Limits compaction I/O to N MB/s during peak hours            │
  │    Prevents compaction from starving foreground queries          │
  │                                                                  │
  │  MySQL resource groups (8.0+):                                  │
  │    CREATE RESOURCE GROUP analytics                               │
  │      TYPE = USER                                                 │
  │      VCPU = 4-7                                                  │
  │      THREAD_PRIORITY = 10;                                       │
  │    SET RESOURCE GROUP analytics;   -- bind session to group     │
  └──────────────────────────────────────────────────────────────┘
```

### Work Stealing: Load Balancing Without a Central Scheduler

```
Problem: Some cores finish their tasks faster than others.
         Central task queue = contention bottleneck.

Solution: Each core has its OWN task queue. Idle cores STEAL from busy cores.

  Core 0 queue:  [T1] [T2] [T3] [T4] [T5]  ← busy
  Core 1 queue:  [T6]                         ← almost idle
  Core 2 queue:  [T7] [T8]                    ← moderate
  Core 3 queue:  []                            ← idle → STEAL from Core 0!

  Core 3 steals T5 from Core 0's queue (take from the TAIL, not head).

  After stealing:
  Core 0 queue:  [T1] [T2] [T3] [T4]
  Core 1 queue:  [T6]
  Core 2 queue:  [T7] [T8]
  Core 3 queue:  [T5]  ← stolen work

Why steal from the tail?
  - Owner processes from head (LIFO — recently added, cache-warm)
  - Thief steals from tail (oldest tasks, larger chunks of work)
  - This minimizes cache conflicts between owner and thief

Database adoption:
  - Go runtime: goroutine scheduler uses work stealing
  - DuckDB: morsel-driven parallelism, workers steal morsels
  - Velox (Meta): task-based execution with work stealing
  - Tokio (Rust): async runtime used by TiKV, uses work stealing
```

### Morsel-Driven Parallelism (DuckDB, Hyper/Umbra)

```
Traditional parallelism: partition table, assign fixed chunks to threads.
Problem: Some chunks have more matching rows → thread finishes early/late.

Morsel-driven: break work into small "morsels" (~10,000 rows).
Workers grab morsels from shared queue as they finish.

┌──────────────────────────────────────────────────────────────────┐
│  Table Scan: SELECT COUNT(*) FROM orders WHERE amount > 100      │
│                                                                     │
│  Table: 10 million rows, morsel size: 10,000 rows                  │
│  → 1,000 morsels                                                    │
│                                                                     │
│  Morsel Queue: [M0] [M1] [M2] [M3] ... [M999]                     │
│                                                                     │
│  Worker 0: grab M0, scan, filter → 342 matches                    │
│            grab M4, scan, filter → 891 matches                     │
│            grab M8, scan, filter → ...                              │
│                                                                     │
│  Worker 1: grab M1, scan, filter → 567 matches                    │
│            grab M5, scan, filter → 123 matches                     │
│            ...                                                       │
│                                                                     │
│  Worker 2: grab M2 ... (same pattern)                              │
│  Worker 3: grab M3 ... (same pattern)                              │
│                                                                     │
│  Automatic load balancing: fast workers grab more morsels.         │
│  No thread sits idle while others are busy.                         │
│  Small morsels = work fits in L1/L2 cache.                          │
│  Pipeline-friendly: scan → filter → aggregate in one pass.         │
│                                                                     │
│  DuckDB, Hyper, Umbra all use this approach.                        │
└──────────────────────────────────────────────────────────────────┘
```

---

## 22. Anatomy of a Read/Write: Thread Lifecycle from Submission to Completion

### What Happens When a Database Thread Calls pread()

Here is the complete lifecycle of a single blocking read, from the database thread's perspective, through the kernel, to hardware, and back:

```
Database Worker Thread                  Kernel                     Hardware
═══════════════════                  ══════                     ════════

 TASK_RUNNING (on CPU)
 │
 │ 1. SUBMIT: pread(fd, buf, 8192, offset)
 │
 ├──► SYSCALL instruction
 │    ├── Switch Ring 3 → Ring 0
 │    ├── KPTI: swap to kernel page tables
 │    ├── Save user registers to kernel stack
 │    │
 │    ├── VFS: resolve fd → inode → filesystem
 │    ├── ext4: extent tree → physical LBA
 │    ├── Block layer: create bio struct
 │    │
 │    ├── I/O scheduler:
 │    │   ├── Check for merge with adjacent requests
 │    │   ├── Insert into sorted queue (mq-deadline)
 │    │   │   OR pass through (none)
 │    │   └── Dispatch to hardware queue
 │    │
 │    ├── NVMe driver:
 │    │   ├── Write NVMe command to Submission Queue
 │    │   ├── DMA address = user buffer (O_DIRECT)     ──────────►
 │    │   ├── Ring doorbell register                    NVMe controller
 │    │   │                                              fetches command
 │    │   │                                              via DMA
 │    │   │                                              │
 │    │   │                                              FTL: LBA → NAND
 │    │   │                                              │
 │    │   │                                              NAND read (~50μs)
 │    │   │                                              │
 │    │   │                                              ECC decode
 │    │   │                                              │
 │    │   │                                              DMA data to
 │    │   │                                              host memory
 │    │   │                                              │
 │    │   │                                              Write CQE
 │    │   │                                              │
 │    │   │                                              MSI-X interrupt
 │    │   │                                     ◄────────────────────
 │    │   │
 │    │   └── Interrupt handler:
 │    │       ├── Read CQE: status=SUCCESS
 │    │       ├── Complete bio → wake blocked thread
 │    │       └── Return from interrupt
 │    │
 │    │  *** WHAT HAPPENED TO OUR THREAD DURING I/O? ***
 │    │
 │    ├── 2. SLEEP: Thread can't proceed until data arrives
 │    │   ├── Thread state → TASK_UNINTERRUPTIBLE (D)
 │    │   ├── Thread removed from CPU run queue
 │    │   ├── Thread placed on I/O wait queue
 │    │   ├── Scheduler called: schedule()
 │    │   │   ├── Pick next thread from run queue (CFS/EEVDF)
 │    │   │   ├── Context switch to next thread
 │    │   │   │   ├── Save our registers
 │    │   │   │   ├── Load next thread's registers
 │    │   │   │   ├── Switch stacks
 │    │   │   │   └── Another thread now runs on this core
 │    │   │   │
 │    │   │   │  ... Our thread is ASLEEP ...
 │    │   │   │  ... Time passes: ~10-20 μs for NVMe ...
 │    │   │   │  ... Other threads use this CPU core ...
 │    │   │   │
 │    │   └── 3. WAKE: I/O completion interrupt arrives
 │    │       ├── Interrupt handler calls wake_up_process(our_thread)
 │    │       ├── Thread state → TASK_RUNNING
 │    │       ├── Thread placed back on CPU run queue
 │    │       ├── MAY NOT RUN IMMEDIATELY!
 │    │       │   └── Must wait for scheduler to pick it
 │    │       │       (schedule latency: ~1-50 μs)
 │    │       │
 │    │       └── 4. RESUME: Scheduler picks our thread
 │    │           ├── Context switch TO our thread
 │    │           ├── Restore our registers
 │    │           ├── We're back in the kernel pread() path
 │    │           ├── pread returns 8192 (success)
 │    │           ├── KPTI: swap to user page tables
 │    │           └── SYSRET: Ring 0 → Ring 3
 │
 │ ◄── pread() returns, data in buf
 │
 TASK_RUNNING (on CPU, processing query)
```

### The Hidden Costs: Where Time Actually Goes

```
┌───────────────────────────────────────────────────────────────────────┐
│  Blocking pread() Latency Breakdown (NVMe, O_DIRECT)                  │
│                                                                         │
│  ═══ ON CPU (our thread) ═══                                           │
│  Syscall entry + KPTI:                ~400 ns     ████                 │
│  VFS + filesystem lookup:             ~200 ns     ██                   │
│  Block layer + NVMe submit:           ~200 ns     ██                   │
│  Sleep setup (state change, sched):   ~500 ns     █████                │
│                                                                         │
│  ═══ ASLEEP (other threads run) ═══                                    │
│  Context switch away:                 ~3,000 ns   ██████████████████   │
│  NVMe hardware processing:           ~10,000 ns  ██████████████████████│
│  Interrupt handling:                  ~3,000 ns   ██████████████████   │
│                                                                         │
│  ═══ WAITING (in run queue) ═══                                        │
│  Schedule latency (waiting for CPU):  ~5,000 ns   ████████████████████ │
│  Context switch back:                 ~3,000 ns   ██████████████████   │
│                                                                         │
│  ═══ ON CPU AGAIN ═══                                                  │
│  Return from kernel + KPTI:          ~400 ns     ████                 │
│                                                                         │
│  TOTAL:                              ~25,700 ns (~26 μs)              │
│  Of which actual NVMe hardware:      ~10,000 ns (39%)                 │
│  Of which scheduling overhead:       ~11,000 ns (43%)  ← THIS!       │
│  Of which kernel overhead:           ~4,700 ns (18%)                  │
│                                                                         │
│  The scheduling overhead is the BIGGEST cost, not the disk!           │
│  This is why databases go to extreme lengths to avoid blocking.       │
└───────────────────────────────────────────────────────────────────────┘
```

### Non-Blocking Read with io_uring: Eliminating the Sleep

```
With io_uring, the thread NEVER sleeps for I/O. Compare:

Blocking pread():
  ─────[submit]═══SLEEP═══[wake]───[sched]───[resume]─────
        400ns    ~10μs      ~5μs    ~3μs      400ns
  Total: ~19 μs, thread did NO useful work during I/O

io_uring async read:
  ─────[submit SQE]───[do other work]───[poll CQE]───[process]────
        ~100 ns        (useful queries!)    ~100 ns     ~100 ns
  Total: ~300 ns of overhead, thread STAYED BUSY the whole time

How this works:
  ┌────────────────────────────────────────────────────────────────┐
  │  Database worker thread (single thread, never sleeps):          │
  │                                                                  │
  │  Task Queue: [Query1] [Query2] [Query3] [Query4] ...           │
  │                                                                  │
  │  while (true) {                                                 │
  │      // 1. Submit pending I/O for queries that need it          │
  │      for (query : queries_needing_io) {                         │
  │          submit_sqe(query->io_request);  // non-blocking!       │
  │          query->state = WAITING_FOR_IO;                          │
  │      }                                                           │
  │                                                                  │
  │      // 2. Check for completed I/O                               │
  │      while (cqe = poll_cq()) {                                  │
  │          query = cqe->user_data;                                 │
  │          query->io_result = cqe->res;                            │
  │          query->state = READY;                                   │
  │          ready_queue.push(query);                                │
  │      }                                                           │
  │                                                                  │
  │      // 3. Run ready queries (CPU work)                          │
  │      for (query : ready_queue) {                                 │
  │          execute_next_step(query);                                │
  │          // May generate new I/O → back to step 1               │
  │      }                                                           │
  │  }                                                               │
  │                                                                  │
  │  Thread utilization: ~95-99% (always doing useful work!)        │
  │  vs blocking model: ~30-50% (sleeping half the time)            │
  └────────────────────────────────────────────────────────────────┘
```

### What Happens During a Write: Buffered vs Direct

```
════════════════════════════════════════════════════════════════════════
BUFFERED WRITE: write(fd, data, 8192) — without O_DIRECT
════════════════════════════════════════════════════════════════════════

Thread                              Kernel
──────                              ──────
write(fd, data, 8192)
  │
  ├──► syscall entry (~400 ns)
  │
  ├──► Find or create page in page cache
  │    ├── Page cache lookup (xarray): ~50 ns
  │    ├── If page exists: lock page (spinlock): ~10-50 ns
  │    ├── If page doesn't exist: allocate frame + add to cache: ~200 ns
  │    │
  │    └── memcpy(kernel_page, user_data, 8192): ~100 ns
  │        (2 pages × 64 cache lines × ~1.5 ns per cacheline move)
  │
  ├──► Mark page as DIRTY: ~10 ns
  │    (set PG_dirty flag, add to dirty list)
  │
  ├──► syscall return (~400 ns)
  │
  └──► RETURNS IMMEDIATELY. Data is NOT on disk.

Total: ~1-2 μs (just memcpy to kernel memory)
Thread was NEVER put to sleep. No I/O wait. Very fast.

BUT: Data is in volatile kernel memory. Power loss → DATA LOST.

════════════════════════════════════════════════════════════════════════
When does data actually reach disk?
════════════════════════════════════════════════════════════════════════

Scenario A — Background writeback (default, no fsync):
  │
  ├── Kernel writeback thread (kworker) wakes up when:
  │   ├── dirty_ratio exceeded (default 20% of RAM is dirty)
  │   ├── dirty_expire_centisecs elapsed (default 30 seconds)
  │   └── dirty_background_ratio exceeded (default 10%)
  │
  ├── kworker: writes dirty pages to disk
  │   ├── Groups pages by inode for sequential I/O
  │   ├── Submits bio requests to block layer
  │   └── Clears PG_dirty flag after device ACKs
  │
  └── Data durable 0-30 seconds later (unpredictable!)

Scenario B — Explicit fsync(fd):
  │
  ├── Thread calls fsync(fd)
  │   ├── Find all dirty pages for this file's inode
  │   ├── Submit write I/O for all dirty pages
  │   ├── Wait for ALL writes to complete (device ACK)
  │   ├── Issue FLUSH CACHE command to device
  │   ├── Wait for flush (device writes volatile cache → media)
  │   └── Return. Data is GUARANTEED on stable media.
  │
  └── fsync time: ~50 μs (NVMe, few pages) to ~50 ms (HDD, many pages)
      Thread BLOCKS during entire fsync (sleeping in D state)


════════════════════════════════════════════════════════════════════════
DIRECT WRITE: write(fd, data, 8192) — with O_DIRECT
════════════════════════════════════════════════════════════════════════

Thread                              Kernel                    Hardware
──────                              ──────                    ────────
write(fd, data, 8192)
  │
  ├──► syscall entry (~400 ns)
  │
  ├──► Verify alignment (buf, offset, size must be aligned)
  │
  ├──► Skip page cache entirely
  │
  ├──► Create bio: DMA from user buffer directly
  │
  ├──► Submit to NVMe SQ                               ──────────►
  │                                                       NVMe write
  │    Thread sleeps (TASK_UNINTERRUPTIBLE)                │
  │    ... ~10-20 μs ...                                  NAND program
  │                                                       (~200-500 μs
  │                                                        for TLC NAND)
  │    Wait... NVMe has DRAM write cache,                 │
  │    so it ACKs after caching, not after                Controller ACK
  │    NAND program (unless FUA flag set)            ◄────────────
  │
  ├──► I/O complete, thread wakes
  │
  ├──► syscall return (~400 ns)
  │
  └──► RETURNS. Data in device write cache (volatile!)
       Need fsync/fdatasync to ensure media durability.

Total: ~10-20 μs (dominated by NVMe latency)
Thread was ASLEEP during I/O. Context switch overhead applies.

NOTE: The NVMe SSD acknowledges the write after copying to its
internal DRAM cache. The actual NAND program takes 200-500 μs for
TLC NAND, but the controller hides this latency. Enterprise SSDs
have power-loss capacitors to flush this cache on power failure.
```

### The Complete Write Path for a Database Commit

```
Application: COMMIT

What actually happens (e.g., PostgreSQL + WAL):

1. Executor calls transaction_commit()

2. WAL Writer:
   ├── Serialize WAL record (in shared memory WAL buffer)
   │   Contents: transaction ID, table OID, tuple data, LSN
   │   Time: ~200 ns (memcpy to shared WAL buffer)
   │
   ├── write(wal_fd, wal_buffer, wal_size)  [buffered or O_DIRECT]
   │   Time: ~1 μs (buffered) or ~15 μs (direct)
   │
   ├── fdatasync(wal_fd)  ←── THE CRITICAL PATH
   │   ├── Flush all dirty WAL pages to device
   │   ├── Issue FLUSH CACHE to NVMe
   │   ├── Wait for device acknowledgment
   │   └── Time: ~10-50 μs (NVMe) or ~2-5 ms (HDD)
   │
   └── Return to executor: "commit is durable"

3. Executor returns "COMMIT" to client
   Total commit latency: ~20-100 μs (NVMe) or ~5-10 ms (HDD)

4. LATER (asynchronously):
   ├── Dirty data pages written by background writer (checkpoint)
   ├── This is NOT latency-critical — WAL guarantees recovery
   └── If crash before data page write:
       Recovery replays WAL → data is reconstructed

═══════════════════════════════════════════════════════════════

Group Commit Optimization:
  Problem: 10,000 transactions/sec × 1 fsync each = 10,000 fsyncs/sec
           NVMe: each fsync ~20 μs → 200 ms/sec spent on fsync alone

  Solution: Group commit — batch multiple transactions into one fsync

  Transaction 1: write WAL ─┐
  Transaction 2: write WAL ─┤
  Transaction 3: write WAL ─┼──► ONE fdatasync() for all three
  Transaction 4: write WAL ─┤    Time: ~20 μs (same as single fsync!)
  Transaction 5: write WAL ─┘

  Result: 5 transactions committed in ~20 μs instead of 5 × 20 = 100 μs
  PostgreSQL: commit_delay / commit_siblings control group commit behavior
  MySQL: binlog_group_commit_sync_delay, binlog_group_commit_sync_no_delay_count
```

---

## Summary: Why Databases Build Their Own Everything

```
┌──────────────────────────────┬───────────────────────────────────────┐
│ OS Feature                    │ Why Databases Replace It               │
├──────────────────────────────┼───────────────────────────────────────┤
│ Page cache                    │ Double buffering, no eviction control │
│ mmap                          │ No write ordering, TLB shootdowns,   │
│                                │ SIGBUS errors, no async I/O          │
│ malloc                        │ Fragmentation, no NUMA awareness,    │
│                                │ no huge page control                  │
│ read()/write()               │ Per-call syscall overhead, blocking   │
│ Filesystem readahead          │ Generic, can't predict DB patterns   │
│ I/O scheduler                │ Doesn't know query priorities         │
│ Thread scheduler              │ Context switch overhead, no workload │
│                                │ awareness, no query prioritization   │
│ I/O scheduler                 │ Doesn't know query priorities,        │
│                                │ can't coordinate with buffer pool    │
│ Blocking I/O model            │ Thread sleeps per I/O = scheduling   │
│                                │ overhead > hardware latency          │
│ Kernel NVMe driver            │ Interrupt overhead, kernel crossing  │
│                                │ (extreme case: SPDK replaces it)    │
└──────────────────────────────┴───────────────────────────────────────┘

The fundamental principle:
  The OS is designed for GENERAL-PURPOSE workloads.
  Databases have SPECIFIC, KNOWN access patterns.
  By managing resources directly, databases can make better decisions
  than the OS kernel ever could for their workload.
```
