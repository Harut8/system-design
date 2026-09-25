# python-mastery — Labs

This is the hands-on companion to the 28 chapters in this folder. It has no theory; each chapter
supplies that. Each lab makes the machine under Python visible: cache misses, page faults,
futex calls, bytecode, refcounts and GC pauses. You finish each task with a number or an artifact
that you can check. Rules:

- **Closed book first.** Answer the checkpoint questions before you reread the chapter.
- **Predict before you run.** Write the prediction down, run the experiment, then write the result next to it.
- **Write results down.** A result that is not in your notebook does not count.
- **Space the review.** Answer the checkpoints again after 1 day, 1 week and 1 month.

## How to use this sheet

- Work in chapter order within a tier (Tier 0 → 5, then 6–8). Each chapter's own lab section is marked "also do §N". Do those as well; this sheet adds different tasks and does not repeat them.
- Tick `- [ ]` boxes as you finish. **Core** tasks are the minimum. **Stretch** tasks need more setup or time.
- Keep a `lab-notebook.md` with one table per task: `| prediction | measured | why they differ |`. Record machine, kernel, and `python -VV` once per session.
- Rerun every experiment at least 3 times and report a median and a spread. Chapter [31](31-measurement-methodology.md) explains why. Do its lab 1, the noise floor, in your first week.
- Spacing: answer each checkpoint closed-book on day 1, day 7 and day 30. Reread the relevant section only for the questions you missed.

## Setup

| Environment | Cost | Used by |
|---|---|---|
| Linux laptop, or Docker Desktop on macOS/Windows running the lab image below (`--privileged`) | Free | all |
| CPython 3.13 and free-threaded 3.13t (via `uv`) | Free | all; 3.13t in 02, 03, 16, 24, 26, 30 |
| Source builds of 3.13: frame-pointer build `/opt/py-fp`, debug build `/opt/py-dbg` | Free, ~10 min each | 00, 08, 12, 15, 17, 20, C3 |
| `perf` with hardware counters: bare-metal Linux, or a VM with vPMU. Docker Desktop usually exposes no PMU; use `cachegrind` there | Free | 00, 01, 02, 12, 20, 31 |
| `bpftrace` and a USDT-enabled CPython build | Free, optional | 12 |
| CPython 3.14/3.14t (`uv python install 3.14 3.14t`) | Free, optional | chapter labs that need 3.14-only features (`asyncio pstree`, `concurrent.interpreters`, `LOAD_FAST_BORROW`) |
| An x86 VM and an ARM VM | Paid, optional | cross-ISA comparisons in 02 |

One image covers every lab. On Apple Silicon it runs as `linux/arm64`; the x86-only tasks say so.

```dockerfile
# Dockerfile
FROM debian:bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential gdb git curl ca-certificates procps psmisc util-linux \
      strace ltrace valgrind linux-perf bpftrace hyperfine \
      libssl-dev zlib1g-dev libbz2-dev libffi-dev libreadline-dev libsqlite3-dev \
      liblzma-dev uuid-dev systemtap-sdt-dev \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN uv python install 3.13 3.13t \
 && uv venv -p 3.13 /opt/venv313 && uv venv -p 3.13t /opt/venv313t \
 && uv pip install --python /opt/venv313/bin/python pyperf py-spy memray scalene numpy \
      hypothesis pytest pytest-repeat coverage mutmut mypy pyright objgraph \
 && uv pip install --python /opt/venv313t/bin/python pyperf numpy pytest
ENV PATH=/opt/venv313/bin:/opt/venv313t/bin:$PATH
```

```bash
docker build -t pylab . && docker run -it --rm --privileged -v "$PWD":/work -w /work pylab
# in the container: python3.13 is the GIL build, python3.13t is free-threaded
python3.13t -c "import sys; print(sys._is_gil_enabled())"      # False
# the two source builds (do once; commit the container or mount /opt)
git clone --depth 1 -b 3.13 https://github.com/python/cpython /src/cpython && cd /src/cpython
./configure --prefix=/opt/py-fp CFLAGS="-fno-omit-frame-pointer -mno-omit-leaf-frame-pointer" \
  && make -j"$(nproc)" && make install && make distclean
./configure --prefix=/opt/py-dbg --with-pydebug && make -j"$(nproc)" && make install
```

For `perf`, set `sysctl kernel.perf_event_paranoid=1` on the host (or the Docker Desktop VM).
Check that it works with `perf stat -e cycles,instructions true`. If it reports `<not supported>`,
you have no PMU; use the `valgrind --tool=cachegrind` variant of each task. For compiled tasks, use
`gcc -O2 -g -pthread`. To build a C extension against any interpreter, use:

```bash
PY=python3.13   # or /opt/py-dbg/bin/python3.13, python3.13t
INC=$($PY -c 'import sysconfig; print(sysconfig.get_paths()["include"])')
EXT=$($PY -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')
gcc -O2 -g -shared -fPIC -I"$INC" tiny.c -o tiny$EXT
```

---

## 00 — The CPU execution model  ([chapter](00-cpu-execution-model.md))
**Time:** ~3 h · **Needs:** Linux + `perf` with PMU, gcc, `/opt/py-fp`. Also do §13 labs 1, 3 and 6.

- [ ] **00.1 Mispredicts through the interpreter** *(Level: Core)*
  - **Goal:** Measure how much of the sorted vs shuffled gap survives in Python.
  - **Do:** `mp.py`: `data = [random.randrange(256) for _ in range(10**6)]`, then sort the list or leave it shuffled depending on `argv[1]`. Run `sum(x for x in data if x >= 128)` 20 times. Then run `perf stat -e cycles,instructions,branches,branch-misses python3.13 mp.py sorted`, and again with `shuffled`.
  - **Predict:** Chapter lab 1 gets about 5x in C. What is the time ratio in Python? How many extra branch misses per element?
  - **Verify:** Compute `(misses_shuffled − misses_sorted) / 2e7`. It should be about 0.5 per element. Multiply the extra misses by ~15 cycles (§2.3) and show that this accounts for most of the time gap. Then explain why the Python ratio is far below 5x.
- [ ] **00.2 Machine instructions per bytecode** *(Level: Core)*
  - **Goal:** Turn "the interpreter is slow" into a number.
  - **Do:** Run `perf stat -e cycles,instructions python3.13 -c "for i in range(10**8): pass"` and the baseline `python3.13 -c pass`. Subtract, then divide by 1e8. Use `dis` on the loop to count bytecodes per iteration.
  - **Predict:** Machine instructions per bytecode, and the IPC.
  - **Verify:** Record instructions/iteration, instructions/bytecode and IPC (§5). Keep these numbers for 20.3.
- [ ] **00.3 Register renaming does not survive the interpreter** *(Level: Core)*
  - **Goal:** Show that one dependency chain can hide another.
  - **Do:** Write two functions with identical bytecode counts. A does `a = a + x` four times per iteration, one chain. B does `a = a + x; b = b + x; c = c + x; d = d + x`, four chains. Check with `dis` that the loop bodies are the same length. Time both with `python3.13 -m pyperf timeit`.
  - **Predict:** In C this gives about 4x (chapter lab 3). What does it give in Python?
  - **Verify:** Report the ratio. Name the chain that dominates instead, using §12.1 and §12.4.
- [ ] **00.4 Find where the misses land** *(Level: Stretch)*
  - **Goal:** Attribute branch misses to instructions inside `_PyEval_EvalFrameDefault`.
  - **Do:** `perf record -e branch-misses /opt/py-fp/bin/python3.13 mp.py shuffled`, then `perf annotate --stdio -s _PyEval_EvalFrameDefault | sort -rn | head -20`.
  - **Verify:** List the top 5 miss sites. Classify each one as indirect dispatch (`jmp *%reg` / `br xN`) or the data-dependent `>= 128` compare, and relate them to §12.1.

**Checkpoint (closed book):**
1. What does a branch mispredict cost on a modern core, and where does that cost come from?
2. Name two things IPC hides.
3. Why is instruction count a bad cost model on an out-of-order core?
<details><summary>Answers</summary>

1. Roughly 15–20 cycles. Everything fetched after the branch is flushed, and the pipeline refills from the front end (§2.3).
2. Whether the retired work was useful (a spin-wait loop has high IPC), and where the stalls were: memory-bound versus front-end-bound. It counts retired instructions and ignores squashed speculative work.
3. Cost depends on dependency chains, latency versus throughput, and port pressure. Independent instructions overlap inside the OoO window, dependent ones serialize, and the same count can differ 4x (§3, §9).
</details>

---

## 01 — The memory hierarchy and caches  ([chapter](01-memory-hierarchy-and-caches.md))
**Time:** ~3 h · **Needs:** gcc, `valgrind`, `perf` (optional), NumPy. Also do §11 labs 1, 2 and 7.

- [ ] **01.1 Row vs column order under cachegrind** *(Level: Core)*
  - **Goal:** See a 64-byte line in the miss counters.
  - **Do:** `static int a[2048][2048]`: fill it, then sum it row-major (`a[i][j]`) or column-major (`a[j][i]`) chosen by `argv[1]`. `gcc -O1 -g trav.c -o trav`, then `valgrind --tool=cachegrind --cache-sim=yes ./trav row` and the same with `col`. On a PMU machine, also run `perf stat -e L1-dcache-loads,L1-dcache-load-misses ./trav col`.
  - **Predict:** The D1 read-miss rate for each order, from the line size and `sizeof(int)`.
  - **Verify:** Row order should give ≈ 4/64 = 6.25%, and column order should be near 100% for the read phase. Then explain why a 2048-wide row makes column order miss on every access (§3, §4).
- [ ] **01.2 The same experiment in Python** *(Level: Core)*
  - **Goal:** Find out which layer eats the effect: layout or boxing.
  - **Do:** NumPy `a = np.random.rand(4096, 4096)`: time `sum(a[i, :].sum() for i in range(4096))` against `sum(a[:, j].sum() for j in range(4096))`. Then build the same data as a list of lists of floats and do both orders in pure Python.
  - **Predict:** Two ratios. How big is the column penalty in NumPy, and in lists of lists?
  - **Verify:** NumPy shows a large penalty. Lists of lists show a much smaller one, because every element is a pointer to a separately allocated float (§10). Explain both numbers.
- [ ] **01.3 Pointer chasing through a shuffled list** *(Level: Core)*
  - **Goal:** Show that the order of the boxed objects in memory matters, not the list.
  - **Do:** `lst = [i + 1000 for i in range(10**7)]`. Time `sum(lst)` (best of 3), then `random.shuffle(lst)` and time it again. It is the same objects and the same list length. Optionally run `perf stat -e cache-misses` on both runs.
  - **Predict:** The slowdown factor after the shuffle.
  - **Verify:** You should see several times slower. The objects were allocated in order, so the in-order walk streams through memory and the prefetcher helps; after the shuffle every element is a dependent random load (§7, §10).
- [ ] **01.4 False sharing between two threads** *(Level: Core)*
  - **Goal:** Measure the cost of two threads writing different variables on the same line, and find it with a tool.
  - **Do:** Two pthreads each increment their own `volatile long` 10^8 times. Layout (a) is `struct { long a; long b; }` and layout (b) puts `b` at offset 128. Time both and run `perf stat -e cache-misses,cycles` on each. With Intel PEBS or AMD IBS on bare metal, also run `perf c2c record ./fs` and `perf c2c report --stdio` on (a).
  - **Predict:** The wall-time ratio (a)/(b) with the threads on two different cores (`taskset -c 0,2`).
  - **Verify:** (a) is several times slower, with far more cache misses. `perf c2c` shows one line with high HITM and two offsets, 0x0 and 0x8, and (b) removes it. Chapter lab 3 sweeps the padding (§5, §6).
- [ ] **01.5 Huge pages vs the TLB** *(Level: Stretch)*
  - **Goal:** Watch dTLB misses fall when the page size grows.
  - **Do:** In C, allocate 1 GiB with `aligned_alloc(2<<20, 1<<30)`, then call `madvise(p, len, MADV_HUGEPAGE)` or `MADV_NOHUGEPAGE`, touch every page, and do 10^8 random 8-byte reads. Check `cat /sys/kernel/mm/transparent_hugepage/enabled`, then run `perf stat -e dTLB-loads,dTLB-load-misses`.
  - **Verify:** `grep AnonHugePages /proc/<pid>/smaps_rollup` is non-zero only in the hugepage run. Record the miss ratio and the time ratio (§8).

**Checkpoint (closed book):**
1. When core B writes a line that core A holds in Shared state, which MESI transitions happen?
2. Why is a random walk over a large array much faster than a pointer chase over the same array?
3. Why is summing a `list` of floats slower than summing an `array('d')` even with the interpreter held constant?
<details><summary>Answers</summary>

1. B issues a read-for-ownership, A's copy goes to Invalid, and B's goes to Modified (§5).
2. Memory-level parallelism. The random walk's addresses are independent, so many misses are in flight at once. In a chase, each address depends on the previous load (§1.1, lab 2).
3. The list holds 8-byte pointers to 24-byte float objects spread over the heap: an extra dependent load, more lines touched, and objects that may be scattered. `array('d')` is contiguous raw doubles (§10, lab 7).
</details>

---

## 02 — Atomics and memory models  ([chapter](02-atomics-and-memory-models.md))
**Time:** ~3 h · **Needs:** gcc, 3.13t; an x86 machine for 02.2. Also do §17 labs 1, 3, 7 and 8.

- [ ] **02.1 The uncontended price of each ordering** *(Level: Core)*
  - **Goal:** Put a number on "seq_cst is expensive" with one thread and no contention.
  - **Do:** In a single thread, run 10^8 × `atomic_store_explicit(&x, i, order)` for `relaxed`, `release` and `seq_cst`, and then `atomic_fetch_add`. Time each with `clock_gettime`. Compile with `-O2 -S` and read the store instruction for each (§7).
  - **Predict:** ns/op for each, and which ones compile to the same instruction on your ISA.
  - **Verify:** On x86, `relaxed` and `release` should both be a plain `mov` and `seq_cst` an `xchg`, and the timings should split the same way. On AArch64, `release` and `seq_cst` are both `stlr`. Make the table.
- [ ] **02.2 Split lock on x86, the one the chapter could not run** *(Level: Stretch)*
  - **Goal:** Measure §13's catastrophe instead of quoting it.
  - **Do:** `char buf[128] __attribute__((aligned(64))); uint64_t *p = (uint64_t *)(buf + 60);` then 10^6 × `__atomic_fetch_add(p, 1, __ATOMIC_SEQ_CST)`, compared with the same loop at offset 64. Run `dmesg | grep -i split` afterwards. Run a second, unrelated memory-bound process at the same time and time it too. This is x86 only; on ARM the same code gets SIGBUS, as §13 shows.
  - **Predict:** The slowdown per op, and whether the bystander process slows down.
  - **Verify:** Report ns/op for both offsets and the bystander's slowdown. Say whether the kernel logged `split lock detection` or throttled you (`split_lock_detect=` in `/proc/cmdline`).
- [ ] **02.3 Look for a Python publication race** *(Level: Core)*
  - **Goal:** Practise telling "not observed" apart from "guaranteed".
  - **Do:** On 3.13t, thread A runs `obj.data = i; obj.ready = i`. Thread B spins until `obj.ready == i`, then asserts `obj.data == i`. Run 10^5 rounds with a `threading.Barrier` between them. Run it on x86, and on ARM if you have it.
  - **Predict:** Violations on each architecture.
  - **Verify:** You will very likely see 0. Then write two sentences from §16 on why 0 is not a guarantee: there is no memory model, and what does hold is an implementation property.

**Checkpoint (closed book):**
1. Which reordering does x86-TSO allow, and which litmus test shows it?
2. What does a release store paired with an acquire load guarantee?
3. Why is `volatile` not a synchronization tool in C?
<details><summary>Answers</summary>

1. Store→load reordering, because of the store buffer. The store-buffering (SB) test shows it: both threads can read 0 (§5, §6).
2. If the acquire load reads the value the release store wrote, every write before the release is visible after the acquire (a synchronizes-with edge).
3. It stops the compiler from eliding accesses but emits no hardware ordering or atomicity, so it does not order memory on weakly ordered CPUs, and a race on it is still UB (§4).
</details>

---

## 03 — Lock-free programming and reclamation  ([chapter](03-lockfree-and-reclamation.md))
**Time:** ~3 h · **Needs:** gcc with `-fsanitize=thread`, 3.13t, `strace`. Also do §12 labs 1, 4 and 5.

- [ ] **03.1 Progress guarantees you can see** *(Level: Core)*
  - **Goal:** Show what "lock-free" buys when a thread stalls.
  - **Do:** Four threads increment a shared counter for 5 s, in two designs. (a) A spinlock. Every 1000 ops, thread 0 calls `usleep(2000)` *inside* the critical section, standing in for preemption. (b) A CAS loop. Thread 0 calls `usleep(2000)` *between* its load and its CAS. Count the ops of threads 1–3 only.
  - **Predict:** How much the other threads' throughput drops in each design.
  - **Verify:** In (a), the others stall for every one of thread 0's naps. In (b), they are unaffected, and only thread 0's CAS fails and retries. That is §1's definition, measured.
- [ ] **03.2 Measure contention directly: CAS retries per op** *(Level: Stretch)*
  - **Goal:** Replace "it's contended" with a number.
  - **Do:** Instrument a Treiber push/pop loop (or a CAS counter) to count failed CAS attempts. Sweep 1, 2, 4, 8 and 2×cores threads. Then add exponential backoff (§9) and sweep again.
  - **Predict:** Retries/op at 8 threads, with and without backoff.
  - **Verify:** Plot two curves of retries/op and ops/s. Say where backoff wins throughput and where it only moves waiting around.
- [ ] **03.3 A lock's fast path is a CAS, and futex is the slow path** *(Level: Core)*
  - **Goal:** See how user space and the kernel split the work in a real lock.
  - **Do:** `strace -f -c -e trace=futex python3.13t lock.py 1`: one thread, 10^6 `with lock:` iterations. Then run it with `4`: four threads contending on one `threading.Lock`.
  - **Predict:** The number of futex syscalls in each run.
  - **Verify:** About 0 uncontended and thousands contended. Relate this to §11: `PyMutex` spins and parks, and the kernel is involved only when a thread must sleep.
- [ ] **03.4 Explain it: a design note to a reviewer** *(Level: Core)*
  - **Goal:** Argue against your own lock-free queue.
  - **Do:** Write five sentences to a reviewer on why the service should use a mutex-protected queue, citing your 03.1 and 03.2 numbers and §8 and §10.
  - **Verify:** Every claim in the note cites one number you measured.

**Checkpoint (closed book):**
1. Define lock-free and wait-free precisely.
2. What is ABA, and why does a garbage-collected runtime mostly avoid it?
3. Why is memory reclamation, not the CAS, the hard part of lock-free structures?
<details><summary>Answers</summary>

1. Lock-free: some thread always completes in a finite number of steps (system-wide progress). Wait-free: every thread completes in a bounded number of its own steps.
2. A CAS succeeds because the value matches, even though it went A→B→A in between, and a freed node may have been reused. A GC keeps a node alive while any thread holds a reference, so its address cannot be reused mid-operation.
3. You cannot free a node that another thread may still dereference. You need a protocol (hazard pointers, EBR, QSBR) that proves no reader holds it, and each protocol has costs and failure modes (§7).
</details>

---

## 06 — Processes, threads, and scheduling  ([chapter](06-processes-threads-scheduling.md))
**Time:** ~3 h · **Needs:** Linux, `taskset`, `chrt`, `perf` (software events are enough). Also do the chapter's labs 2, 5 and 7.

- [ ] **06.1 A Python thread is a kernel task** *(Level: Core)*
  - **Goal:** Map Python threads to TIDs with your own eyes.
  - **Do:** Start three threads that sleep, and print `threading.get_native_id()` from each. From another shell, run `ls /proc/$PID/task`, `cat /proc/$PID/task/*/comm`, and `ps -L -o pid,lwp,psr,stat,comm -p $PID`.
  - **Predict:** How many tasks you will see and which ids repeat.
  - **Verify:** You see 4 tasks with one shared PID, and each LWP equals a `get_native_id()` value. Explain this with §1's clone flags.
- [ ] **06.2 Run-queue latency, then real-time priority** *(Level: Core)*
  - **Goal:** Watch the scheduler delay a sleeper, then stop it.
  - **Do:** The probe does 2000 × `time.sleep(0.001)` and records the oversleep. Run `taskset -c 0 python3.13 probe.py` alone, then with 1 and then 4 CPU spinners (`taskset -c 0 python3.13 -c 'while True: pass' &`). Finally run `chrt -f 10 taskset -c 0 python3.13 probe.py` with 4 spinners. Also read field 2 of `/proc/$PID/schedstat`, the run-queue wait in ns.
  - **Predict:** The p50 and p99 oversleep in each of the 4 configurations.
  - **Verify:** The table rises with the number of spinners and collapses under SCHED_FIFO (§6–§8). If `chrt` gets `EPERM` inside Docker, run that step on the host or VM.
- [ ] **06.3 Affinity changes what Python thinks it has** *(Level: Core)*
  - **Goal:** Catch the pool-sizing input changing.
  - **Do:** `taskset -c 0,1 python3.13 -c "import os; print(os.cpu_count(), os.process_cpu_count(), len(os.sched_getaffinity(0)))"`.
  - **Predict:** All three numbers.
  - **Verify:** `cpu_count` is the machine's CPU count, and the other two are 2. Then name which one `ThreadPoolExecutor` and `ProcessPoolExecutor` use by default in 3.13 (§9, §12).
- [ ] **06.4 `perf sched` on a GIL handoff** *(Level: Stretch)*
  - **Goal:** See the scheduling delays that a CPU profile does not show.
  - **Do:** `perf sched record -- python3.13 two_cpu_threads.py`, then `perf sched latency --sort max | head` and `perf sched timehist | head -50`.
  - **Verify:** Record the max and average wait for the Python TIDs, and match the switch rhythm to `sys.getswitchinterval()` (§12).

**Checkpoint (closed book):**
1. To the Linux kernel, what distinguishes a thread from a process?
2. What turns a context switch from voluntary into involuntary?
3. What does `cpu.max = "10000 100000"` do to average throughput vs tail latency?
<details><summary>Answers</summary>

1. Both are `task_struct`s. Threads are created with `clone` flags that share the address space, file table and signal handlers, and they share a TGID (§1).
2. Voluntary: the task blocks (sleep, I/O, futex). Involuntary: it was runnable and the scheduler preempted it (timeslice or a higher-priority wakeup) (§4).
3. At most 10 ms of CPU per 100 ms period. The average is capped at 10%, but a burst that exhausts the quota waits for the next period, so the tail grows by up to ~90 ms (§10).
</details>

---

## 07 — Virtual memory  ([chapter](07-virtual-memory.md))
**Time:** ~3 h · **Needs:** Linux, root for 07.4. Also do §17 labs 1, 3 and 7.

- [ ] **07.1 Which constructors actually allocate?** *(Level: Core)*
  - **Goal:** See lazy allocation from Python.
  - **Do:** Print `VmRSS` from `/proc/self/status` after each of `bytes(1<<30)`, `bytearray(1<<30)`, `mmap.mmap(-1, 1<<30)`, `np.zeros(1<<27)` and `np.ones(1<<27)`. Delete each object before creating the next.
  - **Predict:** The RSS delta for each (0 or ~1 GiB).
  - **Verify:** Some are ~0 and some are ~1 GiB. Explain each result from the constructor's C source (`Objects/bytesobject.c` and `Objects/bytearrayobject.c`; `calloc` vs `memset`) and §4.
- [ ] **07.2 Count faults, then let THP remove them** *(Level: Core)*
  - **Goal:** Tie RSS growth to page faults.
  - **Do:** `/usr/bin/time -v python3.13 -c "m = bytearray(1<<30)"` and read "Minor page faults". Check `cat /sys/kernel/mm/transparent_hugepage/enabled`, and rerun with the THP mode toggled (`echo never|always > …`, VM or host only).
  - **Predict:** The fault count with 4 KiB pages and with 2 MiB pages.
  - **Verify:** About 262,144 vs about 512, plus interpreter overhead. Report the wall-time difference too (§3, §11).
- [ ] **07.3 Copy-on-write at the physical-page level** *(Level: Stretch)*
  - **Goal:** Watch a physical frame split on the first write.
  - **Do:** As root, create `m = mmap.mmap(-1, 4096, flags=mmap.MAP_PRIVATE)`, set `m[0] = 1`, and get `addr = ctypes.addressof(ctypes.c_char.from_buffer(m))`. Read the 8-byte entry at offset `addr // 4096 * 8` in `/proc/self/pagemap`: bit 63 means present, and bits 0–54 are the PFN. Fork. The child prints the PFN, sets `m[0] = 2`, and prints it again.
  - **Predict:** Which of the three PFNs (parent, child before the write, child after) are equal.
  - **Verify:** Parent and child share a PFN until the write, and the child gets a new PFN afterwards. Then repeat with a Python list instead of `m[0]`, where the child only *reads* it, and use §7 to explain why its pages still split.
- [ ] **07.4 Overcommit policy** *(Level: Stretch)*
  - **Goal:** Make `mmap` fail at reservation time instead of at OOM time.
  - **Do:** In a VM, check `CommitLimit` and `Committed_AS` in `/proc/meminfo`. Try `mmap.mmap(-1, 4 * total_ram)` under `vm.overcommit_memory=0`, `1` and `2`. Restore the setting afterwards.
  - **Predict:** Which modes succeed.
  - **Verify:** Record the outcome of each mode and the error, `OSError: [Errno 12]`, under mode 2 (§8).

**Checkpoint (closed book):**
1. Minor vs major fault: what is the difference, and what does each cost?
2. RSS, PSS, USS: which one do you sum across pre-forked workers?
3. Why does a child that only *reads* a Python object graph still dirty shared pages?
<details><summary>Answers</summary>

1. Minor: the page is already in memory (zero page, page cache, COW source), so the kernel only maps it, in about a µs. Major: it needs I/O from disk or swap, which takes ms (§3).
2. PSS. It divides shared pages among the sharers, so the sum is correct. RSS double-counts shared pages (§10).
3. Reading an object writes its refcount, and a GC pass writes the GC headers. Each write privatizes the whole 4 KiB page (§7).
</details>

---

## 08 — Allocators  ([chapter](08-allocators.md))
**Time:** ~2.5 h · **Needs:** `strace`, `ltrace`, `/opt/py-fp` (its libpython is linked statically, so ltrace sees the calls). Also do §20 labs 2, 3 and 8.

- [ ] **08.1 strace the allocator** *(Level: Core)*
  - **Goal:** See pymalloc arenas arrive as syscalls.
  - **Do:** `strace -f -e trace=brk,mmap,munmap python3.13 -c "x = [str(i) for i in range(10**6)]; del x" 2>&1 | awk '{print $1,$2}' | sort | uniq -c | sort -rn | head`. Then run it again with `PYTHONMALLOC=malloc`.
  - **Predict:** The arena mmap size, how many arenas you get, and whether `del x` returns any of them.
  - **Verify:** pymalloc shows repeated `mmap(NULL, 1048576, …)` and matching `munmap` calls. Under `malloc`, the same work appears as hundreds of `brk` calls (§2, §13).
- [ ] **08.2 Count `malloc` calls** *(Level: Core)*
  - **Goal:** Measure how much traffic pymalloc absorbs.
  - **Do:** `ltrace -c -e malloc+free+realloc+calloc /opt/py-fp/bin/python3.13 -c "x=[str(i) for i in range(10000)]"`, run with the default allocator and with `PYTHONMALLOC=malloc`. Keep N small, because ltrace is slow.
  - **Predict:** The ratio of `malloc` calls between the two runs.
  - **Verify:** About 25x more calls under `malloc`. Then compute ns per call from ltrace's column, and explain why you must not trust ltrace's absolute timings (§17).
- [ ] **08.3 `malloc_trim` against a pinned heap top** *(Level: Stretch)*
  - **Goal:** Find out whether glibc can return memory from the middle of the heap.
  - **Do:** `x = [bytes(1000) for _ in range(10**6)]`. Objects over 512 B go to glibc, not pymalloc. Keep `x[-1]`, delete the rest, and print RSS. Then `ctypes.CDLL("libc.so.6").malloc_trim(0)` and print RSS again.
  - **Predict:** Whether RSS drops after the delete, and after the trim.
  - **Verify:** It does not drop after the delete, and it drops a lot after the trim. glibc ≥ 2.8 `madvise`s free pages inside the heap even though `brk` cannot move (§6). Relate this to chapter lab 3, which trims from C.
- [ ] **08.4 Explain it: a note to the SRE on call** *(Level: Core)*
  - **Goal:** Write the explanation you will be asked for during an incident.
  - **Do:** Write five sentences on why RSS stays high after a batch job frees its data, and what you would change. Cite your 08.1 and 08.3 numbers and §6.
  - **Verify:** The note names the allocator layer (pymalloc or glibc) responsible for each part of the retained memory.

**Checkpoint (closed book):**
1. What does pymalloc handle, and in what units does it get memory from the OS?
2. What is glibc's default mmap threshold, and why does it move?
3. Why do many threads multiply glibc's RSS?
<details><summary>Answers</summary>

1. Requests ≤ 512 bytes. It gets 1 MiB arenas via `mmap`, which are split into 16 KiB pools of one size class each (§13).
2. 128 KiB. It is dynamic: freeing an mmapped chunk raises it, up to 32 MiB on 64-bit, so later large allocations go to the heap and may never be returned (§2.3).
3. glibc creates per-thread arenas, up to 8 × cores on 64-bit. Each has its own free lists and fragmentation, and freed memory in one arena cannot serve another (§5).
</details>

---

## 09 — Syscalls and I/O  ([chapter](09-syscalls-and-io.md))
**Time:** ~2.5 h · **Needs:** Linux, `strace`. Also do §19 labs 1, 3 and 10.

- [ ] **09.1 strace a blocking call: where does each one park?** *(Level: Core)*
  - **Goal:** Map Python blocking APIs to kernel waits.
  - **Do:** Write one script per call: `time.sleep(1)`, `queue.Queue().get(timeout=1)`, `threading.Event().wait(1)`, `subprocess.run(["sleep","1"])`, and `sock.recv(1)` on a socketpair with a timer thread that sends after 1 s. Run each under `strace -f -T -e 'trace=!mmap,munmap,brk,rt_sigaction'`.
  - **Predict:** The syscall each one blocks in.
  - **Verify:** Find the one call with `-T ≈ 1.0` in each trace. Expect `clock_nanosleep`, `futex`, `futex`, `wait4` and `recvfrom`. Note that a `queue` or `Event` wait is a futex and not a poll loop (§4).
- [ ] **09.2 The vDSO, counted** *(Level: Core)*
  - **Goal:** Prove that some "syscalls" never enter the kernel.
  - **Do:** `strace -c python3.13 -c "import time; [time.time() for _ in range(10**6)]"`, then the same with `os.getppid()`.
  - **Predict:** The syscall count in each.
  - **Verify:** `clock_gettime` barely appears, while `getppid` shows about 10^6 calls (§3).
- [ ] **09.3 Buffering, measured in write calls** *(Level: Core)*
  - **Goal:** Predict the syscall count from the buffer size.
  - **Do:** Write 10^5 short lines three ways: `open(p, "w")`, `print(..., flush=True)`, and `open(p, "wb", buffering=0)`. Count the calls with `strace -c -e trace=write`. Print `os.stat(p).st_blksize` and `io.DEFAULT_BUFFER_SIZE` first.
  - **Predict:** The write count for each, from the total bytes and the buffer size.
  - **Verify:** The buffered count ≈ total bytes / buffer size, and the other two ≈ 10^5. Report the wall-time ratio (§14, §15).
- [ ] **09.4 The syscall bill of one asyncio request** *(Level: Stretch)*
  - **Goal:** Price a request in syscalls.
  - **Do:** Run an asyncio echo server with `asyncio.start_server`. A client sends 10^4 request/response pairs over 10 persistent connections. Attach `strace -c -f -p $SERVER_PID` for the run.
  - **Predict:** Syscalls per request, and which three dominate.
  - **Verify:** Compute (total syscalls) / 10^4. Compare the result with §16, and name one batching change that would cut it.

**Checkpoint (closed book):**
1. Why does `time.time()` not enter the kernel on Linux?
2. What bug does edge-triggered epoll invite, and what is the rule that prevents it?
3. After `write()` returns, where is your data? After `fsync()` returns?
<details><summary>Answers</summary>

1. The vDSO maps kernel timekeeping data and code into the process, so the clock is read in user space (§3).
2. You get one notification per readiness *edge*. If you read once and leave data in the buffer, you never hear about it again. Rule: read until `EAGAIN` (§7).
3. After `write()`: in the page cache only. After `fsync()`: the file's data and metadata have been sent to the device with a flush. A failed fsync may have already dropped the dirty pages (§10, §11).
</details>

---

## 10 — Signals, fork, and exec  ([chapter](10-signals-fork-exec.md))
**Time:** ~3 h · **Needs:** Linux, `py-spy`, `ps`. Reproduce §9 first; the chapter has no lab section.

- [ ] **10.1 Which C calls are signal-blind?** *(Level: Core)*
  - **Goal:** Answer the chapter's §17 question by experiment.
  - **Do:** Install a SIGALRM handler that records `time.perf_counter()`, and arm it with `signal.setitimer(signal.ITIMER_REAL, 0.1)`. Then run, one at a time and each sized to take about 3 s: `zlib.compress(big)`, `str(10**200000)` (raise the limit with `sys.set_int_max_str_digits(0)`), `math.factorial(200000)`, and `sorted(big_list)`. Report the handler delay for each.
  - **Predict:** Which calls delay the handler until the call returns.
  - **Verify:** Make a table of call and delay in ms. Explain each row with §3.2 (eval breaker) and §4.
- [ ] **10.2 Fork-safety: reproduce, patch, then fix properly** *(Level: Core)*
  - **Goal:** Go past §9's reproduction.
  - **Do:** (a) Run §9's script with `python3.13 -W error::DeprecationWarning` and read the error. (b) Raise `signal.alarm` in the child to 30, let a child wedge, then run `py-spy dump --pid <child>` to find it in `lock.acquire`. (c) Add `os.register_at_fork(before=lock.acquire, after_in_parent=lock.release, after_in_child=lock.release)` and recount the wedged children. (d) Switch to `multiprocessing.get_context("forkserver")`.
  - **Predict:** The wedged count after (c).
  - **Verify:** It drops from about 60% to 0/40. Then write why (c) does not scale: you cannot register handlers for every lock in libraries you do not own. That is why (d) and §10 are the real fix.
- [ ] **10.3 File descriptors across exec** *(Level: Core)*
  - **Goal:** See PEP 446 at work.
  - **Do:** Open a file, then run `subprocess.run(["ls", "-l", "/proc/self/fd"])`. Repeat after `os.set_inheritable(fd, True)` with `close_fds=False`, and again with `pass_fds=(fd,)`.
  - **Predict:** Which fds the child lists in each case.
  - **Verify:** Your fd appears only in the last two runs (§12).
- [ ] **10.4 Zombies and process groups** *(Level: Stretch)*
  - **Goal:** Create the states from §13 and §14 and inspect them.
  - **Do:** Fork 5 children that `os._exit(0)` while the parent sleeps, then run `ps -o pid,ppid,stat,cmd --ppid $PID`. Repeat with `signal.signal(signal.SIGCHLD, signal.SIG_IGN)`. Then start `subprocess.Popen(["sleep","100"])` with and without `start_new_session=True`, press Ctrl-C, and check `ps -o pid,pgid,sid,stat,cmd`.
  - **Verify:** You see 5 `Z` rows, then none. The child survives Ctrl-C only when it is in its own session.

**Checkpoint (closed book):**
1. When does a Python signal handler actually run, and on which thread?
2. What does a fork child inherit from a multi-threaded parent, and what does it lose?
3. What survives `exec()`: handlers, ignored signals, the signal mask, fds?
<details><summary>Answers</summary>

1. The C handler only sets a flag and the eval breaker bit. The Python handler runs later, on the main thread of the main interpreter, at the next eval-breaker check (§3).
2. It gets a copy of the whole address space, including every lock's state, but only the calling thread. Locks held by other threads stay held forever (§8, §9).
3. Handlers reset to default, ignored signals stay ignored, the mask is kept, and fds survive unless marked close-on-exec, which PEP 446 makes the default for Python-created fds (§11, §12).
</details>

---

## 11 — IPC and shared memory  ([chapter](11-ipc-and-shared-memory.md))
**Time:** ~3 h · **Needs:** Linux. The chapter has no lab section.

- [ ] **11.1 The pipe deadlock, with a byte count** *(Level: Core)*
  - **Goal:** Reproduce §2.2's hang and predict where it happens.
  - **Do:** `p = subprocess.Popen(["cat"], stdin=PIPE, stdout=PIPE)`. Write to `p.stdin` in 4 KiB chunks, printing the running total and flushing, and never read stdout. Print `fcntl.fcntl(p.stdin.fileno(), fcntl.F_GETPIPE_SZ)` first. Then fix it with `p.communicate(data)`.
  - **Predict:** The total written before the hang.
  - **Verify:** It hangs at about 2 × pipe capacity plus `cat`'s own buffer. Explain the number.
- [ ] **11.2 `mmap` shared memory between processes** *(Level: Core)*
  - **Goal:** Make `MAP_SHARED` vs `MAP_PRIVATE` concrete.
  - **Do:** (a) `m = mmap.mmap(-1, 4096, flags=mmap.MAP_SHARED)`, fork, the child writes, the parent reads. (b) The same with `MAP_PRIVATE`. (c) Two unrelated shells: `SharedMemory(name="lab", create=True, size=100)` in one and attach by name in the other. Run `ls -l /dev/shm`. Then `kill -9` the creator and check `/dev/shm` again.
  - **Predict:** What is visible in (a) and (b), the `.size` in (c), and whether the segment outlives `kill -9`.
  - **Verify:** Record the outcomes. Say which process unlinked the segment, or failed to, and when, using §8.3's resource tracker. Then repeat (c) with `track=False` (new in 3.13) in the attaching shell.
- [ ] **11.3 Torn reads, then a seqlock** *(Level: Stretch)*
  - **Goal:** Break an invariant across processes, then protect it.
  - **Do:** Put `mv = memoryview(m).cast("Q")` over a shared mapping. The writer loops `mv[0] = i; mv[1] = i`. The reader checks `mv[0] == mv[1]` 10^7 times and counts mismatches. Then add a sequence counter in `mv[2]`: odd while writing, and the reader retries if it changed.
  - **Predict:** The mismatch count before and after the seqlock, on x86 and on ARM.
  - **Verify:** Mismatches are non-zero before the seqlock and 0 after it on x86. Then write why the pure-Python seqlock is only correct by accident of TSO (§13).
- [ ] **11.4 Moving 1 GB: pipe vs Queue vs shared memory** *(Level: Core)*
  - **Goal:** Reproduce the §14 cost model on your machine.
  - **Do:** Send 1000 × 1 MB messages parent→child over `os.pipe` + `os.write`, `multiprocessing.Queue`, and a `SharedMemory` ring with a `Pipe` for notification only. Measure MB/s and run `strace -f -c` on each.
  - **Predict:** Rank the three and estimate the ratios.
  - **Verify:** A table of MB/s and syscalls per message. Explain the Queue's extra cost (pickle plus the feeder thread, §6.3).

**Checkpoint (closed book):**
1. Why are there only two families of IPC?
2. What does `shm_unlink` remove, and when is the memory freed?
3. Why can a Python `list` not live in shared memory?
<details><summary>Answers</summary>

1. Processes either copy bytes through the kernel (pipes, sockets) or map the same physical pages (shared memory). There is no third way to move data (§1).
2. It removes the name. The memory is freed when the last mapping or fd goes away (§7, §8).
3. It holds pointers into one process's heap, and its refcount and GC header are written on every access. Its type pointer and allocator are also process-local. The pointers alone are fatal (§9).
</details>

---

## 12 — Observing a process  ([chapter](12-observing-a-process.md))
**Time:** ~3 h · **Needs:** Linux, `perf`, `py-spy`, `strace`; `bpftrace` for 12.4. Also do §24 labs 1, 2 and 5.

- [ ] **12.1 Read three states from `/proc`** *(Level: Core)*
  - **Goal:** Diagnose a process without attaching a tool.
  - **Do:** Run three processes: one spins, one `time.sleep`s, and one blocks on a `threading.Lock` held by another thread. For each, read `State` and `voluntary_ctxt_switches` from `/proc/$PID/status`, plus `/proc/$PID/wchan`, `/proc/$PID/schedstat` and (as root) `/proc/$PID/task/*/stack`.
  - **Predict:** The state letter and kernel wait function for each.
  - **Verify:** A 3-row table that tells the three apart using `/proc` only (§3).
- [ ] **12.2 `py-spy --native` vs plain** *(Level: Core)*
  - **Goal:** See what C frames add to a Python profile.
  - **Do:** Write a workload that spends time in `zlib.compress`, `json.dumps` and a pure-Python loop. Run `py-spy record -o plain.svg -- python3.13 w.py`, then `py-spy record --native -o native.svg -- python3.13 w.py`.
  - **Predict:** What the plain profile shows for the `zlib` line.
  - **Verify:** Plain attributes the time to one Python line, and native shows the C function below it (for example `deflate`). Screenshot both flame graphs into your notebook (§7, §13).
- [ ] **12.3 The observer's price list** *(Level: Core)*
  - **Goal:** Measure §2's "four prices" yourself.
  - **Do:** Run one CPU-bound script (~10 s) under each: nothing, `py-spy record --rate 100`, `py-spy record --rate 1000`, `perf record -F 999 -g -- python3.13 -X perf`, and `python3.13 -m cProfile`. Time each with `hyperfine --runs 3`.
  - **Predict:** The slowdown % for each.
  - **Verify:** A table of tool and overhead %. Mark which tools sample and which trace.
- [ ] **12.4 bpftrace and USDT on CPython** *(Level: Stretch)*
  - **Goal:** Aggregate in the kernel instead of logging every event.
  - **Do:** Count syscalls with `bpftrace -e 'tracepoint:syscalls:sys_enter_* /pid == $1/ { @[probe] = count(); }' $PID` and compare the overhead with `strace -c` (§11). Then build CPython with `./configure --prefix=/opt/py-dt --with-dtrace` and run `bpftrace -e 'usdt:/opt/py-dt/bin/python3.13:python:function__entry { @[str(arg1)] = count(); }'`.
  - **Verify:** Top 10 syscalls and the top 10 Python functions by call count, taken with no code change (§9.4).

**Checkpoint (closed book):**
1. Rank counting, sampling and tracing by overhead, and give one tool for each.
2. Why does a native profiler show only `_PyEval_EvalFrameDefault` for Python code?
3. A service is slow at 5% CPU. Which profile do you take?
<details><summary>Answers</summary>

1. Counting (`perf stat`, `/proc`) costs almost nothing. Sampling (`perf record`, `py-spy`) costs about 1% at typical rates. Tracing (`strace`, `settrace`) scales with the event rate and can reach tens of times slower (§2).
2. Python frames are data structures in the interpreter's memory, not machine frames. The C stack shows the eval loop recursing, and `-X perf` trampolines or a Python-aware unwinder are needed to name them (§7, §8).
3. An off-CPU profile (wait time: locks, I/O, scheduler), because the on-CPU profile covers only the 5% (§14).
</details>

---

## 15 — Refcounting and ownership  ([chapter](15-refcounting-and-ownership.md))
**Time:** ~2 h · **Needs:** 3.13, `/opt/py-dbg`. Also do §13 labs 2, 3 and 5.

- [ ] **15.1 Account for every reference** *(Level: Core)*
  - **Goal:** Predict `sys.getrefcount` step by step.
  - **Do:** `x = []`, then one step at a time: `l = [x]`, `d = {"k": x}`, `t = (x, x)`, a closure `def f(): return x`, and `def g(a=x): pass`. After each step, print `sys.getrefcount(x) - 1` and `len(gc.get_referrers(x))`.
  - **Predict:** Both numbers after each step.
  - **Verify:** Match every increment to a holder. The closure step is the odd one: a global name makes no cell (§3, §7).
- [ ] **15.2 Leak and over-free on purpose, without C** *(Level: Core)*
  - **Goal:** Watch a refcount bug with the debug build's instruments.
  - **Do:** On `/opt/py-dbg/bin/python3.13`: `o = object()`, then call `ctypes.pythonapi.Py_IncRef(ctypes.py_object(o))` 1000 times. Print `sys.getrefcount(o)` and the `sys.gettotalrefcount()` delta. Then, in a subprocess, call `Py_DecRef` twice on an object held by one name and `del` the name.
  - **Predict:** Both deltas, and what the debug build does on the over-free.
  - **Verify:** Both deltas are +1000. The over-free aborts with a negative-refcount fatal error; record the message. On the release build, record what happens instead (§5, §12).
- [ ] **15.3 Find the statement that frees the object** *(Level: Core)*
  - **Goal:** Predict deallocation exactly, including the traceback trap.
  - **Do:** Build a class `Big` with `__del__` that prints "freed". (a) `a = Big(); b = [a]; del a; print("x"); b.clear(); print("y")`. (b) `def work(): big = Big(); raise ValueError`, then `try: work()` / `except ValueError as e: saved = e`, then `print("after")` and `del saved`.
  - **Predict:** Where "freed" prints in each case.
  - **Verify:** (a) Between "x" and "y". (b) Only at `del saved`, because the traceback keeps `work`'s frame and therefore `big` alive (§2, §8).
- [ ] **15.4 Explain it: to a teammate who branches on refcounts** *(Level: Stretch)*
  - **Goal:** Say why this is wrong and where the idea still has legitimate use.
  - **Do:** Write five sentences on why `if sys.getrefcount(x) == 2:` is broken, and on the one legitimate use of `getrefcount` (§7). Use `sys.getrefcount(None)` on 3.13 and on 3.13t as evidence.
  - **Verify:** The note names immortality (§9) and the extra reference from the call argument.

**Checkpoint (closed book):**
1. New, borrowed and stolen references: define each and give one API that returns or takes each.
2. Why does `sys.getrefcount(x)` report one more than you expect?
3. What does making an object immortal buy on a multicore machine?
<details><summary>Answers</summary>

1. New: the caller owns it and must `DECREF` (`PyLong_FromLong`). Borrowed: no ownership, valid only while the owner lives (`PyList_GET_ITEM`). Stolen: the callee takes over your reference (`PyList_SET_ITEM`, `PyTuple_SET_ITEM`) (§3, §4).
2. The argument binding holds a temporary reference during the call (§7).
3. INCREF/DECREF become no-ops, so the object's cache line stays Shared on every core with no invalidation traffic. It also removes COW faults for those objects after fork (§9, §10).
</details>

---

## 16 — Object memory layout  ([chapter](16-object-memory-layout.md))
**Time:** ~2.5 h · **Needs:** 3.13 and 3.13t. Also do §14 labs 3, 4 and 5.

- [ ] **16.1 Read the header out of raw memory** *(Level: Core)*
  - **Goal:** Find `ob_refcnt` and `ob_type` by address.
  - **Do:** `x = 12345678901`, then `words = [ctypes.c_void_p.from_address(id(x) + 8*i).value for i in range(6)]`. Find `words.index(id(int))` on 3.13 and on 3.13t. Also read `ctypes.c_ssize_t.from_address(id(x)).value` on 3.13, and create two more names bound to `x` to watch it change.
  - **Predict:** The `ob_type` offset on each build, and the refcount word's value.
  - **Verify:** Offset 8 on 3.13 and 24 on 3.13t. Account for the 3.13t words (`ob_tid`, flags/mutex/gc bits plus `ob_ref_local`, `ob_ref_shared`) (§1, §2).
- [ ] **16.2 Derive the list growth formula** *(Level: Core)*
  - **Goal:** Recover `list_resize` from observations alone.
  - **Do:** Append 200 items and record the capacity each time `sys.getsizeof` changes: `(size - sys.getsizeof([])) // 8`.
  - **Predict:** The first 8 capacities.
  - **Verify:** You get 4, 8, 16, 24, 32, 40, 52, 64 … Fit a formula, then compare it with `list_resize` in `Objects/listobject.c` (§7).
- [ ] **16.3 Size classes from `id()` strides** *(Level: Core)*
  - **Goal:** See allocator size classes from pure Python.
  - **Do:** For each of `[i + 10**6 for i in range(10**4)]`, `[(i, i, i) for ...]`, `[C() for ...]` (a plain class) and `[float(i) for ...]`, compute the most common `id(b) - id(a)` across neighbours with `collections.Counter`. Run on 3.13 and 3.13t.
  - **Predict:** The modal stride for each type on each build, from `sys.getsizeof` and §3's 16-byte alignment.
  - **Verify:** On 3.13, int gives 32, the 3-tuple 64 (the GC header is included), and so on. Explain each mismatch with `getsizeof` and the differences on 3.13t (mimalloc, §12).
- [ ] **16.4 Explain it: "getsizeof is the memory cost"** *(Level: Stretch)*
  - **Goal:** Correct a colleague with evidence.
  - **Do:** Write five sentences using your 16.3 strides and §11's deep sizer. Cover what `getsizeof` leaves out: referents, GC header rounding, size-class rounding and the managed dict.
  - **Verify:** Each claim is backed by one measured number.

**Checkpoint (closed book):**
1. What are the three headers a GC-tracked object can have, and their sizes on the GIL build?
2. Why does freeing 99% of small objects often not reduce RSS?
3. What does `__slots__` save, and why is it less than folklore claims?
<details><summary>Answers</summary>

1. `PyObject` (refcount + type, 16 B), `PyVarObject` (+ `ob_size`, 24 B), and the GC header before the object (16 B) (§1).
2. pymalloc returns an arena only when every pool in it is empty. Scattered survivors pin arenas (§5).
3. It removes the per-instance dict or managed values. In 3.13 the managed/inline values are already compact, so it saves tens of bytes per instance and a little time per attribute read, not 5–10x (§9).
</details>

---

## 17 — The C API and extensions  ([chapter](17-c-api-and-extensions.md))
**Time:** ~4 h · **Needs:** gcc, 3.13, `/opt/py-dbg`, `valgrind`, `memray`. Also do §15 labs 1, 4 and 7.

- [ ] **17.1 A tiny extension with correct refcounting** *(Level: Core)*
  - **Goal:** Write the ownership rules yourself, from a blank file.
  - **Do:** In `tiny.c`, write `pair(a, b)`, which returns `(a, b)` built with `PyTuple_New` + `PyTuple_SET_ITEM`, so you `Py_INCREF` each item before it is stolen. Write `pair2` using `PyTuple_Pack`, which increfs for you. Write `get(d, k, default)` using `PyDict_GetItemRef` (3.13, returns a new reference). Build with the Setup command. Test by calling each 10^5 times and checking that `sys.getrefcount(a)` is unchanged.
  - **Predict:** Which of the three would leak or crash if you copied the INCREF pattern from one into another.
  - **Verify:** The refcount delta after 10^5 calls is 0 for all three (§4).
- [ ] **17.2 Plant a leak and find it three ways** *(Level: Core)*
  - **Goal:** Learn which tool sees which kind of leak.
  - **Do:** Variant A: an extra `Py_INCREF(a)` in `pair`. Variant B: `PyLong_FromLong(1000000 + i)` created and never released. Small ints are immortal and would hide the leak. For each variant, run 10^5 calls and check: `sys.getrefcount(a)`, the `tracemalloc` snapshot diff (`compare_to(..., "lineno")`), `memray run --native` + `memray flamegraph --leaks` (with `PYTHONMALLOC=malloc`), and the `sys.gettotalrefcount()` delta on the debug build (rebuild against `/opt/py-dbg`).
  - **Predict:** A 4 × 2 table of which tool flags which variant.
  - **Verify:** A leaks references but no memory, so only the refcount instruments see it. B leaks memory that tracemalloc and memray attribute to the calling Python line. Explain the difference (§4, §14).
- [ ] **17.3 Over-decref: time to diagnosis** *(Level: Core)*
  - **Goal:** Compare the three instruments on a use-after-free.
  - **Do:** Add a stray `Py_DECREF(a)` to `pair`. Run the test (a) on the release build, (b) under `PYTHONMALLOC=malloc valgrind --tool=memcheck python3.13 t.py`, and (c) on `/opt/py-dbg`.
  - **Predict:** What each run prints and how far it gets.
  - **Verify:** (a) Crashes late or not at all. (b) Reports `Invalid read` with a stack. (c) Fails sooner, with a refcount fatal error or a crash on the debug allocator's `0xDD` fill pattern. Record which one pointed at the line (§14).
- [ ] **17.4 Break the GIL contract** *(Level: Stretch)*
  - **Goal:** See what the debug build says when you touch objects without the GIL.
  - **Do:** Add `checksum(buf)` using `PyObject_GetBuffer`. Run the loop inside `Py_BEGIN_ALLOW_THREADS`, then (the bug) call `PyErr_SetString` or `Py_DECREF` inside that region. Run it on `/opt/py-dbg` and on the release build.
  - **Verify:** The debug build fails at the first C-API call without a thread state, with a fatal error or crash; record the message. Record where the release build fails, if it fails at all, and state §10's contract in one sentence.

**Checkpoint (closed book):**
1. `PyList_SetItem` vs `PyList_Append`: which one steals, and what goes wrong if you get it backwards?
2. Why does `tracemalloc` miss an extra `Py_INCREF`?
3. What must never happen between `Py_BEGIN_ALLOW_THREADS` and `Py_END_ALLOW_THREADS`?
<details><summary>Answers</summary>

1. `SetItem` steals your reference, while `Append` increfs its own. Treat `SetItem` like `Append` and you leak a reference; treat `Append` like `SetItem` (no DECREF) and you leak; DECREF after `SetItem` gives you a use-after-free (§4).
2. It traces allocations. An extra INCREF allocates nothing; it only keeps an existing block alive forever (§14).
3. Any touch of a Python object or the C API that needs a thread state: refcounts, error setting, allocation. Only pure C work on memory you have pinned, such as a held buffer (§10).
</details>

---

## 19 — Bytecode and code objects  ([chapter](19-bytecode-and-code-objects.md))
**Time:** ~2 h · **Needs:** 3.13 (and 3.12 via `uv python install 3.12` for 19.3). Also do §14 labs 1, 3 and 5.

- [ ] **19.1 `dis` before and after an optimization** *(Level: Core)*
  - **Goal:** Predict a speedup by counting instructions per iteration.
  - **Do:** `before(data)`: `out = []`, then `for x in data: out.append(math.sqrt(x))`. `after(data, sqrt=math.sqrt)`: `return [sqrt(x) for x in data]`. Run `dis.dis` on both and count the instructions between `FOR_ITER` and `JUMP_BACKWARD`. Time both with `pyperf timeit` over 10^5 floats.
  - **Predict:** The per-iteration instruction counts and the speedup.
  - **Verify:** Record the instruction counts before and after and the measured ratio. Say which removed instructions (`LOAD_GLOBAL`, `LOAD_ATTR`, the method call) cost most, and whether the ratio tracks the count (§5, §10).
- [ ] **19.2 What the compiler folds** *(Level: Core)*
  - **Goal:** Know which constants are free.
  - **Do:** Run `dis` on and inspect `co_consts` for `2**10`, `"a"*5`, `"a"*5000`, `(1,2)+(3,)`, `x in [1,2,3]`, `x in {1,2,3}` and `-1`.
  - **Predict:** Folded or not, and the type of each resulting constant.
  - **Verify:** `"a"*5000` is not folded (size limit). The list and set literals in `in` become a `tuple` and a `frozenset` constant. Record the rest.
- [ ] **19.3 Bytecode is private: diff two versions** *(Level: Stretch)*
  - **Goal:** See §13's warning in real output.
  - **Do:** Write `dis_diff(src)`, which runs `python3.12` and `python3.13` as subprocesses, dumps `[(i.opname, i.argrepr) for i in dis.get_instructions(f)]` from each, and prints a `difflib.unified_diff`. Run it on a function with a loop, a comprehension, an f-string and a `try`.
  - **Verify:** List every opcode that was renamed, merged (superinstructions such as `LOAD_FAST_LOAD_FAST`) or removed between the two versions.

**Checkpoint (closed book):**
1. How big is one instruction in CPython 3.11+, and where do the inline caches live?
2. What makes `try` "zero-cost", and what does it cost when an exception is raised?
3. Why does a list comprehension not create a function object in 3.12+?
<details><summary>Answers</summary>

1. 2 bytes (opcode + oparg). The caches are `CACHE` entries inline in `co_code` right after the instruction (§2, §4).
2. There is no setup instruction; handlers are found in `co_exceptiontable` only when something raises. Raising pays for a table lookup, stack unwinding and exception object creation (§8).
3. PEP 709 inlines it into the enclosing function's bytecode, which saves the function and frame creation (§10).
</details>

---

## 20 — The eval loop  ([chapter](20-eval-loop.md))
**Time:** ~2.5 h · **Needs:** 3.13, `/opt/py-fp`, `perf`. Also do §15 labs 1, 2 and 3.

- [ ] **20.1 The price of a megamorphic site** *(Level: Core)*
  - **Goal:** Measure what losing specialization costs in ns.
  - **Do:** Define 8 classes `C0…C7`, each setting `self.x = 1`. `get(objs)` sums `o.x` over a list. Time it with a mono list (10^5 × `C0()`) and a poly list (cycling through all 8). Run `dis.dis(get, adaptive=True)` after each run.
  - **Predict:** ns per attribute load in each case, and the opname you will see.
  - **Verify:** `LOAD_ATTR_INSTANCE_VALUE` for mono. Poly shows generic `LOAD_ATTR` most of the time (§6 explains the backoff). Report the ratio (§6, §7).
- [ ] **20.2 The slow path shows up as C functions** *(Level: Core)*
  - **Goal:** Read specialization failure off a native profile.
  - **Do:** `perf record -g /opt/py-fp/bin/python3.13 -X perf bench.py mono`, then `perf report --no-children --sort symbol | head -25`. Repeat with `poly`.
  - **Predict:** Which C functions appear only in the poly profile.
  - **Verify:** Generic lookup functions (for example `_PyType_Lookup`, `PyObject_GenericGetAttr`) rise in poly and are absent in mono. Record their share (§14).
- [ ] **20.3 Count executed bytecodes with `sys.monitoring`** *(Level: Core)*
  - **Goal:** Get the dynamic instruction count and ns per bytecode.
  - **Do:** `mon = sys.monitoring`; `mon.use_tool_id(3, "cnt")`; register an `INSTRUCTION` callback that increments a counter; call `mon.set_local_events(3, f.__code__, mon.events.INSTRUCTION)`; call `f(1000)`, where `f` is a `for i in range(k): s += i` loop. Then turn the events off and time `f(10**7)` with nothing attached.
  - **Predict:** The count for `f(1000)`, from `dis`.
  - **Verify:** About 6 × 1000 plus a small constant. Compute ns/bytecode from the uninstrumented timing, and put it next to 00.2's machine instructions per bytecode.
- [ ] **20.4 Explain it: "our polymorphic helper is slow"** *(Level: Stretch)*
  - **Goal:** Write the review comment you would actually leave.
  - **Do:** Write five sentences to a colleague. Use your 20.1 and 20.2 numbers, name the guard that fails, and propose a fix (split the call site, or normalize the types first).
  - **Verify:** The note cites §7's guard by name.

**Checkpoint (closed book):**
1. What does an adaptive instruction bet on, and what happens when the bet fails?
2. Why do polymorphic sites not thrash endlessly between specializations?
3. Where do Python frames live in 3.11+, and when does a frame object get created?
<details><summary>Answers</summary>

1. It bets on the type or version tags cached inline, such as the type version or keys version. When a guard fails it deopts to the generic instruction, and repeated misses trigger re-specialization after a backoff counter (§4, §7).
2. Exponential backoff on the counter: after failures it waits longer before trying again (§6).
3. In `_PyInterpreterFrame`s on a per-thread chunked data stack. A `PyFrameObject` is materialized lazily, for example by `sys._getframe`, a traceback or `f_locals` (§8).
</details>

---

## 22 — Garbage collection  ([chapter](22-garbage-collection.md))
**Time:** ~2.5 h · **Needs:** 3.13. Also do §13 labs 1, 3 and 7.

- [ ] **22.1 `gc.callbacks` pause histogram under cyclic garbage** *(Level: Core)*
  - **Goal:** Measure the GC pauses your service actually pays.
  - **Do:** Add a callback to `gc.callbacks` that records `perf_counter_ns()` on `"start"`, and on `"stop"` records the duration, `info["generation"]` and `info["collected"]`. Workload: build and drop 200 trees of 10^4 nodes with parent back-pointers (cycles). Then repeat with the parent pointer held as a `weakref.ref` (no cycles).
  - **Predict:** The number of collections per generation, and the p50, p99 and max pause, for both workloads.
  - **Verify:** A table per workload. The acyclic run still collects just as often, because the trigger is allocation counts, but `collected` is about 0 (§5).
- [ ] **22.2 The threshold trade-off** *(Level: Core)*
  - **Goal:** Plot total GC time against the pause length.
  - **Do:** Print `gc.get_threshold()`. Rerun 22.1's cyclic workload with `threshold0` ∈ {700, 2000, 10000, 50000}, and record total GC time, the number of collections, max pause and `ru_maxrss`.
  - **Predict:** How each of the four columns moves as `threshold0` rises.
  - **Verify:** A table with one row per `threshold0` and your recommendation for a latency-sensitive service (§5, §12).
- [ ] **22.3 Find which class makes the cycles** *(Level: Core)*
  - **Goal:** Hunt cyclic garbage by type.
  - **Do:** `gc.set_debug(gc.DEBUG_SAVEALL)`, run a mixed workload, `gc.collect()`, then `Counter(type(o).__name__ for o in gc.garbage).most_common(5)`.
  - **Predict:** The top type.
  - **Verify:** It is your node class (or its `dict`, `cell` or `method`). Remove the cycle and show the count drops to about 0 (§12).
- [ ] **22.4 Manual GC at request boundaries** *(Level: Stretch)*
  - **Goal:** Test the "disable GC, collect between requests" pattern.
  - **Do:** Simulate requests of about 2 ms that allocate cyclic garbage. Compare p50, p99 and max request latency with automatic GC against `gc.disable()` plus `gc.collect(0)` every N requests, plus `gc.freeze()` after warmup.
  - **Verify:** A table of p99 and peak RSS for each setting, and the N at which RSS stops being bounded.

**Checkpoint (closed book):**
1. Why does CPython need both refcounting and a cycle collector?
2. What does the collector compute in `update_refs` / `subtract_refs`, and what does a non-zero result mean?
3. What does `gc.freeze()` do, and when does it help?
<details><summary>Answers</summary>

1. Refcounting frees acyclic garbage immediately but can never reach zero for a cycle. The collector finds unreachable cycles among container objects (§1).
2. For each tracked object it takes the refcount and subtracts the references coming from inside the scanned set. A non-zero result means an external reference, so that object and everything reachable from it survive (§4).
3. It moves all tracked objects into a permanent generation that is never scanned. That cuts GC work and, after fork, the COW writes from GC scans. It does not stop refcount writes (§12).
</details>

---

## 24 — The GIL  ([chapter](24-the-gil.md))
**Time:** ~3 h · **Needs:** 3.13 and 3.13t, ≥ 4 cores, `strace`, `py-spy`. Also do §18 labs 2, 3 and 6.

- [ ] **24.1 Scaling with 1, 2 and 4 threads on 3.13 vs 3.13t** *(Level: Core)*
  - **Goal:** Get the scaling table that this whole tier depends on.
  - **Do:** `work(n)`: a pure-Python loop (`s += i * i % 7`) that takes ~0.5 s. Submit 8 chunks to a `ThreadPoolExecutor(max_workers=T)` for T ∈ {1, 2, 4}. Record wall time, and the ratio `time.process_time()` / wall. Run on `python3.13`, `python3.13t`, and `PYTHON_GIL=1 python3.13t`. Print `sys._is_gil_enabled()` in each run.
  - **Predict:** Speedup at T=4 for each of the three interpreters, and the CPU/wall ratio.
  - **Verify:** A 3 × 3 table. Speedup is about 1 with the GIL and approaches T on 3.13t. Also record the single-thread tax, T=1 on 3.13t vs 3.13 (§13).
- [ ] **24.2 Which C calls release the GIL?** *(Level: Core)*
  - **Goal:** Predict scaling from the source, not from the docs.
  - **Do:** On 3.13 with 4 threads, run 200 calls each of `hashlib.sha256(buf).digest()` (buf = 1 MB), `zlib.compress(buf)`, `json.dumps(big_dict)` and `sorted(big_list)`. Compare with 1 thread.
  - **Predict:** Which of the four scale.
  - **Verify:** hashlib (above 2 KiB) and zlib scale; json and sorted do not. Find the `Py_BEGIN_ALLOW_THREADS` in `Modules/_hashopenssl.c` or `zlibmodule.c` that explains it (§5).
- [ ] **24.3 See the handoff as futex calls** *(Level: Core)*
  - **Goal:** Make the switch interval visible in the kernel.
  - **Do:** Run two CPU-bound threads for 5 s under `strace -f -c -e trace=futex python3.13 two.py`. Repeat with `sys.setswitchinterval(0.0005)`, then on `python3.13t`.
  - **Predict:** The futex count for each run, from `sys.getswitchinterval()`.
  - **Verify:** The count scales with 5 s / interval on the GIL build and is near 0 on 3.13t. Explain the `FUTEX_WAIT` timeouts with §6.
- [ ] **24.4 Explain it: why `time.sleep` doesn't block other threads and `sum(range(10**9))` does** *(Level: Stretch)*
  - **Goal:** Explain how a starved thread looks from outside.
  - **Do:** Measure first: run a ticker thread that records its maximum gap between ticks, while the main thread runs each of the two calls. Then write five sentences to a junior engineer.
  - **Verify:** The note cites both max gaps and the difference between §4's eval breaker and §5's GIL release.

**Checkpoint (closed book):**
1. When does a thread holding the GIL give it up in CPython 3.2+?
2. What does the GIL guarantee to Python code, and what does it not?
3. Why did earlier attempts to remove the GIL (the Gilectomy) slow single-threaded code?
<details><summary>Answers</summary>

1. When it blocks in a call that releases the GIL (I/O, sleep, and C code that opts in), or when another thread has waited longer than the switch interval (5 ms) and set a drop request, which is checked at eval-breaker points (§3, §4).
2. It keeps the interpreter's internals consistent and makes single bytecodes indivisible. It does not make compound operations (`x += 1`, check-then-act) atomic (§9).
3. Atomic refcount operations on shared objects made every INCREF/DECREF a contended cache-line write, and throughput fell as cores were added (§1, §11).
</details>

---

## 26 — Free-threading  ([chapter](26-free-threading.md))
**Time:** ~3 h · **Needs:** 3.13t, ≥ 4 cores, a C compiler. Also do §12 labs 1, 3 and 5.

- [ ] **26.1 Check what you actually got** *(Level: Core)*
  - **Goal:** Never benchmark the wrong interpreter.
  - **Do:** On each interpreter, print `sysconfig.get_config_var("Py_GIL_DISABLED")`, `sys._is_gil_enabled()` and `sys.flags`. Then run `python3.13t -X gil=1` and `PYTHON_GIL=0 python3.13`.
  - **Predict:** The output of each command.
  - **Verify:** 3.13t reports `Py_GIL_DISABLED=1` and GIL off, and `-X gil=1` turns it back on. On the GIL build `PYTHON_GIL=0` fails with an error. Note the error text (§2).
- [ ] **26.2 Distinct slots in one list are not private** *(Level: Core)*
  - **Goal:** Find true sharing that looks like private data.
  - **Do:** On 3.13t with 4 threads × 10^6 increments, run three layouts: (a) each thread updates `shared[tid] += 1` in one list; (b) each thread updates its own list `mine[0] += 1`; (c) each thread updates a local variable. Measure throughput at 1, 2 and 4 threads.
  - **Predict:** The scaling of (a). Padding the slots 8 apart would fix false sharing; would it fix (a)?
  - **Verify:** Report scaling for all three. Use §7 to explain (a): the list's per-object lock and its shared refcount are true sharing, so padding does nothing.
- [ ] **26.3 Check-then-act with a Barrier** *(Level: Core)*
  - **Goal:** Amplify a real-world race: lazy initialization.
  - **Do:** Write `get_instance()`: `if _inst is None: _inst = Expensive()`, where the constructor does ~1 ms of work and counts how often it runs. 8 threads wait on a `threading.Barrier(8)` and then call it. Repeat 1000 times on 3.13 and 3.13t.
  - **Predict:** The mean number of constructions per round on each build.
  - **Verify:** It is about 1 on the GIL build (rarely more) and more than 1 on 3.13t. Fix it with double-checked locking and confirm the count is exactly 1 (§5).
- [ ] **26.4 Explain it: should service X move to 3.13t?** *(Level: Stretch)*
  - **Goal:** Write the decision note your team lead needs.
  - **Do:** One page: the 24.1 scaling table, the single-thread tax, the §4 memory tax on your object mix, the extension audit (`python3.13t -W error::RuntimeWarning -c "import X"` for each dependency), and the 26.2 sharing result. Apply §11's framework.
  - **Verify:** The note ends with a yes/no/not-yet recommendation, with one measured number per argument.

**Checkpoint (closed book):**
1. What makes refcounting cheap in the free-threaded build when an object stays on one thread?
2. Why does free-threading make existing Python races fail more often without creating new ones?
3. What happens when 3.13t imports a C extension that does not declare `Py_mod_gil`?
<details><summary>Answers</summary>

1. Biased reference counting: the owning thread updates `ob_ref_local` non-atomically, and only other threads use atomics on `ob_ref_shared` (§1, doc 15 §9).
2. The races were always legal, but the GIL made interleavings rare (switches only every 5 ms at safe points). With true parallelism they overlap constantly (§5).
3. The GIL is re-enabled for the whole process, and a `RuntimeWarning` is emitted (§6).
</details>

---

## 28 — asyncio internals  ([chapter](28-asyncio-internals.md))
**Time:** ~2.5 h · **Needs:** 3.13, `strace`. Also do §22 labs 4, 5 and 7 (lab 7 is the debug-mode stall detector).

- [ ] **28.1 strace the event loop** *(Level: Core)*
  - **Goal:** See `await asyncio.sleep` as an `epoll_wait` timeout, and a thread wakeup as a self-pipe byte.
  - **Do:** Write `main()`: `threading.Timer(0.2, lambda: loop.call_soon_threadsafe(print, "woken")).start()` then `await asyncio.sleep(0.5)`. Run `strace -f -tt -e trace=/epoll,sendto,recvfrom python3.13 loop.py`.
  - **Predict:** The timeout argument of each `epoll_wait`, and the syscall that wakes the loop at 0.2 s.
  - **Verify:** The first wait shows a timeout of ≈500. The thread's `sendto` on the socketpair wakes it, and the next wait shows the remaining ≈300. Map each line to §9's self-pipe.
- [ ] **28.2 The cancelled-timer heap** *(Level: Core)*
  - **Goal:** Watch §8's `TimerHandle` leak get purged.
  - **Do:** In a coroutine, create `hs = [loop.call_later(3600, print) for _ in range(10**6)]` and cancel them all. Print `len(loop._scheduled)` (a private attribute; this is a lab) and RSS, then `await asyncio.sleep(0)` and print both again.
  - **Predict:** The heap length before and after one loop iteration.
  - **Verify:** 10^6, then 0: the purge rule in `base_events.py` fires once more than half the handles are cancelled. Record the RSS change.
- [ ] **28.3 A stall that debug mode misses** *(Level: Core)*
  - **Goal:** Find the blind spot of `slow_callback_duration`.
  - **Do:** 50 tasks each do 50 ms of CPU work and then `await asyncio.sleep(0)`, in a loop. Run with `asyncio.run(main(), debug=True)`. Add a monitor task that sleeps 10 ms and records `loop.time()` lateness.
  - **Predict:** Debug-mode warnings, and the maximum lag the monitor sees.
  - **Verify:** No warnings (each step is under 100 ms) but ≈ 2.5 s maximum lag. Explain why a per-callback threshold cannot see this (§17).
- [ ] **28.4 The eager task factory** *(Level: Stretch)*
  - **Goal:** Measure what §15 saves.
  - **Do:** Time creating and awaiting 10^5 tasks whose coroutine returns immediately. Run with and without `loop.set_task_factory(asyncio.eager_task_factory)`, then repeat with coroutines that do `await asyncio.sleep(0)` first.
  - **Predict:** The speedup in both cases.
  - **Verify:** A large win when tasks finish synchronously and little when they suspend. Explain why using §15.

**Checkpoint (closed book):**
1. What happens in one iteration of `_run_once`?
2. How does `call_soon_threadsafe` wake a loop blocked in `epoll_wait`?
3. Why can a CPU-bound coroutine not be cancelled?
<details><summary>Answers</summary>

1. Compute the timeout from the ready queue and the timer heap, run `select`/`epoll_wait`, queue the I/O callbacks, move due timers to the ready queue, and run every callback that was ready at the start (§7).
2. It appends the callback and writes a byte to the self-pipe (socketpair), whose read end is registered with the selector (§8, §9).
3. `Task.cancel()` only schedules a `CancelledError` to be thrown at the next `await` that suspends. Code that never awaits never sees it (§13.3).
</details>

---

## 29 — Async patterns and pitfalls  ([chapter](29-async-patterns-and-pitfalls.md))
**Time:** ~2.5 h · **Needs:** 3.13. Also do §18 labs 2, 6 and 10.

- [ ] **29.1 An unbounded queue, measured** *(Level: Core)*
  - **Goal:** See Little's Law in RSS and latency.
  - **Do:** A producer puts 10^4 items/s and a consumer takes 5×10^3/s, for 10 s. Stamp each item with `loop.time()`. Compare `asyncio.Queue()` with `Queue(maxsize=100)`. Record `qsize()`, peak `tracemalloc` memory and p99 item latency.
  - **Predict:** Final queue length and p99 latency for each queue.
  - **Verify:** Unbounded: ~5×10^4 items and latency growing to ~10 s. Bounded: the producer slows down and latency stays ≈ 100 / 5000 s (§2).
- [ ] **29.2 A fire-and-forget task gets collected** *(Level: Core)*
  - **Goal:** Reproduce §3's "Task was destroyed but it is pending!".
  - **Do:** `asyncio.create_task(waiter())` with no reference kept, where `waiter` awaits a `loop.create_future()` that nobody else holds. Then call `gc.collect()` and `await asyncio.sleep(0.1)`.
  - **Predict:** Whether the task survives.
  - **Verify:** It is destroyed and the warning is logged. Keep a reference in a set with a `done_callback` discard and show the warning is gone.
- [ ] **29.3 Swallowed cancellation defeats a timeout** *(Level: Core)*
  - **Goal:** Watch a timeout silently fail.
  - **Do:** `worker()` wraps `await asyncio.sleep(1)` in `try`/`except asyncio.CancelledError: pass`, then does `await asyncio.sleep(1)` and returns "done". Time `async with asyncio.timeout(0.1): await worker()`.
  - **Predict:** The wall time and whether `TimeoutError` is raised.
  - **Verify:** ≈ 2 s, and it returns "done" with no `TimeoutError`. Fix it with `raise` in the handler, or by checking `asyncio.current_task().cancelling()`, and confirm ≈ 0.1 s plus `TimeoutError` (§5, §7).
- [ ] **29.4 Context propagation across the thread boundary** *(Level: Stretch)*
  - **Goal:** Find out which bridge carries `contextvars`.
  - **Do:** Set `request_id.set("abc")`, then read it inside a function run through `asyncio.to_thread(f)` and through `loop.run_in_executor(None, f)`.
  - **Predict:** What each call prints.
  - **Verify:** `to_thread` sees "abc", and `run_in_executor` sees the default. Explain this with `copy_context()` (§12, §13).

**Checkpoint (closed book):**
1. What does a bounded queue turn unbounded memory growth into?
2. Why does `gather` leave orphans when one child fails, and what does `TaskGroup` do instead?
3. Why must you re-raise `CancelledError`?
<details><summary>Answers</summary>

1. Backpressure: the producer blocks on `put`, so latency is bounded by `maxsize` / service rate (§2).
2. `gather` propagates the first exception but does not cancel the siblings, so they keep running unobserved. `TaskGroup` cancels the siblings and waits for them before raising an `ExceptionGroup` (§4).
3. Cancellation is delivered once, at an await (edge-triggered). If you swallow it, the canceller (a timeout or `TaskGroup`) believes you stopped, and its own accounting (`uncancel`) breaks (§5, §7).
</details>

---

## 30 — Concurrency correctness  ([chapter](30-concurrency-correctness.md))
**Time:** ~3 h · **Needs:** 3.13, 3.13t, `py-spy`. Also do §23 labs 2, 3 and 6.

- [ ] **30.1 Deadlock, diagnosed from stacks** *(Level: Core)*
  - **Goal:** Find a lock-order deadlock without reading the code.
  - **Do:** Thread 1 takes A then B, and thread 2 takes B then A, with `time.sleep(0.01)` between the two acquires. Add `faulthandler.dump_traceback_later(3, exit=True)`, then also attach `py-spy dump --pid $PID`.
  - **Predict:** Which lines each thread is blocked on.
  - **Verify:** Both dumps show each thread in `acquire` on the other's lock. Fix it with a global order (§12) and show the program finishes.
- [ ] **30.2 An async check-then-act race** *(Level: Core)*
  - **Goal:** Show that single-threaded code can still race.
  - **Do:** `get(key)`: `if key not in cache: cache[key] = await fetch(key)`, where `fetch` sleeps 10 ms and counts its calls. Run `asyncio.gather(*(get("k") for _ in range(100)))`.
  - **Predict:** The fetch count.
  - **Verify:** 100. Fix it by caching the *future* (or with a per-key `asyncio.Lock`) and get 1 (§6, §13).
- [ ] **30.3 Turn a rare bug into a common one** *(Level: Core)*
  - **Goal:** Measure how scheduling changes the probability of a bug.
  - **Do:** `Counter.incr`: `v = self.n; self.n = v + one()`, where `one()` is a function (a call is a switch point). 4 threads × 10^5 increments; one run counts as failing if the total is short. Do 100 runs at the default switch interval, at `sys.setswitchinterval(1e-6)`, and on 3.13t.
  - **Predict:** The failure rate in each configuration.
  - **Verify:** Three rates. Then explain why "passed 100 times" tells you little until you know the rate (§4, §20).
- [ ] **30.4 A wall-clock jump breaks a timeout** *(Level: Stretch)*
  - **Goal:** Show §14's clock bug in practice.
  - **Do:** In a VM, run two loops: one waits until `time.time() > t0 + 10` and one until `time.monotonic() > m0 + 10`. After 2 s, run `date -s "-1 hour"` as root. Restore the clock afterwards.
  - **Predict:** When each loop exits.
  - **Verify:** The monotonic loop exits at 10 s and the wall-clock loop runs about an hour longer. Grep your own code for `time.time()` used in deadlines.

**Checkpoint (closed book):**
1. Data race vs race condition: define both, and give a Python example of a race condition with no data race.
2. Name the four Coffman conditions, and the one that lock ordering breaks.
3. Why is `time.monotonic()` required for timeouts?
<details><summary>Answers</summary>

1. A data race is unsynchronized conflicting access to one memory location. A race condition is a correctness bug that depends on timing. Example: `if k not in d: d[k] = v` with each operation atomic, where two threads both insert (§2).
2. Mutual exclusion, hold-and-wait, no preemption, circular wait. Ordering breaks circular wait (§8, §12).
3. The wall clock can jump (NTP, admin, VM restore). A deadline computed from it can fire early or never (§14).
</details>

---

## 31 — Measurement methodology  ([chapter](31-measurement-methodology.md))
**Time:** ~3 h · **Needs:** 3.13, `pyperf`, `hyperfine`; `perf` for 31.3. Also do §14 labs 1, 3 and 4. Do lab 1 before any other benchmark in this sheet.

- [ ] **31.1 pyperf vs timeit: run-to-run variance** *(Level: Core)*
  - **Goal:** Measure the instrument before you trust it.
  - **Do:** `S='import random; random.seed(0); d=[random.random() for _ in range(1000)]'`. Run `for i in $(seq 20); do python3.13 -m timeit -s "$S" "sorted(d)"; done` and record the 20 reported values. Then run `for i in $(seq 5); do python3.13 -m pyperf timeit -q -s "$S" "sorted(d)" -o r$i.json; done` and `python3.13 -m pyperf stats r1.json`.
  - **Predict:** The max/min ratio across runs for each tool.
  - **Verify:** A table of tool, median, max/min and stdev. Explain why pyperf's spawned worker processes (§7.1) capture variance that a single `timeit` process hides (§6.2).
- [ ] **31.2 Whole-process timing with hyperfine** *(Level: Core)*
  - **Goal:** Price interpreter startup with confidence intervals.
  - **Do:** `hyperfine --warmup 3 -N 'python3.13 -c pass' 'python3.13 -S -c pass' 'python3.13 -I -S -c pass' 'python3.13 -c "import asyncio"'`. Then, as root and without `-N` (the prepare step needs a shell), rerun the first command with `--prepare 'sync; echo 3 > /proc/sys/vm/drop_caches'`.
  - **Predict:** The ms for each variant, and the cold-cache penalty.
  - **Verify:** Record hyperfine's mean ± σ for each. Then say which differences are smaller than your 31-lab-1 noise floor.
- [ ] **31.3 Cycles and instructions vary less than time** *(Level: Stretch)*
  - **Goal:** Find a lower-noise metric for A/B tests.
  - **Do:** `perf stat -r 20 -e task-clock,cycles,instructions python3.13 bench.py`. Run it idle, then with `stress-ng --cpu 2` or a compile running.
  - **Predict:** The ± % for each of the three events in each condition.
  - **Verify:** `instructions` is tightest, and `task-clock` moves most under load. Then write when an instruction-count comparison misleads (memory-bound code, §9).
- [ ] **31.4 Explain it: "CI says 5% slower"** *(Level: Core)*
  - **Goal:** Answer a regression alert with statistics.
  - **Do:** Write five sentences to your team. Say whether a 5% delta is resolvable, using your noise floor, 31.1's spread, and §8's bootstrap CI, and what design you would run to settle it (§7.6).
  - **Verify:** The note quotes a CI width you measured.

**Checkpoint (closed book):**
1. Why does `timeit` disable GC, and when does that make its result wrong?
2. Why does pyperf spawn many processes instead of looping in one?
3. Why is `min` contested as a summary statistic?
<details><summary>Answers</summary>

1. To reduce noise. It is wrong for any allocation-heavy or cyclic workload, where GC cost is part of the real cost (§6.1).
2. Much of the variance is per-process (ASLR, hash seed, memory layout, which core you land on). A single process samples only one of those states (§7.1).
3. It is robust to additive noise but throws away the distribution. It rewards lucky states, spreads more across copies than the median does (§8.6), and does not predict typical performance (§6.3).
</details>

---

## 32 — Profiling  ([chapter](32-profiling.md))
**Time:** ~2.5 h · **Needs:** 3.13, `py-spy`, `scalene`, `memray`, `pyperf`; FlameGraph scripts (`git clone https://github.com/brendangregg/FlameGraph`) for 32.4. Also do §11 labs 1, 4 and 7.

- [ ] **32.1 Scalene's Python/native/system split** *(Level: Core)*
  - **Goal:** Separate interpreter time from native time per line.
  - **Do:** Write a program with three hot lines: a pure-Python loop, a NumPy matrix multiply, and a `time.sleep` or file read. Run `scalene --cli --reduced-profile prog.py`.
  - **Predict:** The Python %, native % and system % for each line.
  - **Verify:** Record the three columns per line. The NumPy line should be mostly native and the loop mostly Python. Say what cProfile would have shown for the same program (§2, §5).
- [ ] **32.2 memray: by count vs by size** *(Level: Core)*
  - **Goal:** Learn that the hottest allocator and the biggest allocator are different lines.
  - **Do:** Write a program that allocates millions of small tuples on one line and a few 50 MB `bytes` on another. Run `memray run -o run.bin prog.py`, `memray stats run.bin` and `memray flamegraph run.bin`.
  - **Predict:** The top line by allocation count and the top line by bytes.
  - **Verify:** Two different lines. Say which one matters for peak RSS and which for CPU (§7).
- [ ] **32.3 Find, fix and prove** *(Level: Core)*
  - **Goal:** Run §10's workflow end to end.
  - **Do:** Write a toy with a hidden O(n²) (`if x in some_list` inside a loop). `py-spy record -o before.svg`, fix it with a `set`, save `pyperf` results before and after (`-o before.json`, `-o after.json`), then run `python3.13 -m pyperf compare_to before.json after.json`.
  - **Predict:** The speedup at n = 10^4.
  - **Verify:** The flame graph pointed at the line. `compare_to` reports the speedup and says whether it is significant.
- [ ] **32.4 Differential flame graph** *(Level: Stretch)*
  - **Goal:** See what changed between two profiles.
  - **Do:** Run `py-spy record -f raw -o a.txt` before and `-o b.txt` after a change, then `FlameGraph/difffolded.pl a.txt b.txt | FlameGraph/flamegraph.pl > diff.svg`.
  - **Verify:** Red and blue frames match the change you made, and nothing else moved beyond noise.

**Checkpoint (closed book):**
1. Why do deterministic profilers inflate functions with many cheap calls?
2. What can a sampling profiler miss?
3. Why is memory profiling a different problem from CPU profiling?
<details><summary>Answers</summary>

1. Each call and return event pays a fixed hook cost. Functions with many short calls absorb that overhead in proportion to their call count, not their work (§2).
2. Short or rare events between samples, and anything off-CPU unless it samples idle threads. Without native unwinding it also misses time in native code (§3, §8).
3. You care about what is *retained* (live heap, peak, fragmentation), not only where allocations happen. Native allocations can be invisible to Python-level tracers (§7).
</details>

---

## 35 — Memory optimization  ([chapter](35-memory-optimization.md))
**Time:** ~2.5 h · **Needs:** 3.13, `memray`, `objgraph`. Also do §19 labs 1, 4 and 6.

- [ ] **35.1 A memray flame graph of a leak** *(Level: Core)*
  - **Goal:** Find a leak by its allocation site.
  - **Do:** `app.py` handles 10^5 fake requests. Each one stores a 2 KB payload in a module-level `_seen = {}` keyed by request id (the leak), and also uses a bounded `functools.lru_cache(maxsize=1024)` (not a leak). Run `PYTHONMALLOC=malloc memray run --native -o leak.bin app.py`, then `memray flamegraph --leaks leak.bin` and `memray summary leak.bin`.
  - **Predict:** The leaked bytes, and which of the two caches appears.
  - **Verify:** The largest leak frame is the `_seen[...] =` line, with ≈ 200 MB and more. The lru_cache is absent or small. Explain why `--leaks` needs `PYTHONMALLOC=malloc` (pymalloc keeps freed blocks in its arenas).
- [ ] **35.2 The same leak with tracemalloc** *(Level: Core)*
  - **Goal:** Compare the stdlib tool with memray.
  - **Do:** `tracemalloc.start(10)`, take a snapshot after 10^3 and after 10^4 requests, and print `s2.compare_to(s1, "lineno")[:5]`. Measure the slowdown with tracing on vs off.
  - **Predict:** Whether the top line matches 35.1, and tracemalloc's overhead.
  - **Verify:** The same line comes out on top, and the `size_diff` scales with 9000 requests. Record the overhead ratio (§2.4).
- [ ] **35.3 Find the retainer** *(Level: Stretch)*
  - **Goal:** Answer "who keeps this object alive?"
  - **Do:** Hide the leak behind indirection: a registered callback closure keeps a reference to a `Session`, which holds the payloads. Run `objgraph.show_growth()` before and after a batch, then print the path from `objgraph.find_backref_chain(objgraph.by_type("Session")[0], objgraph.is_proper_module)`.
  - **Verify:** The chain ends at the module attribute that holds the callback list. Break the chain with a `weakref.WeakMethod` and show that `show_growth()` goes flat (§16).
- [ ] **35.4 Explain it: a postmortem paragraph** *(Level: Core)*
  - **Goal:** Write what you would put in the incident doc.
  - **Do:** Five sentences covering the symptom (the RSS time series shape from §3), the tool that found it, the retainer, the fix, and the guard you added: a bounded cache or a per-cache size metric.
  - **Verify:** Every sentence contains a number from 35.1 to 35.3.

**Checkpoint (closed book):**
1. Why is `ru_maxrss` a poor leak detector?
2. Linear RSS growth vs a sawtooth vs a plateau: what does each shape suggest?
3. Why can RSS stay high after a leak is fixed and the objects are freed?
<details><summary>Answers</summary>

1. It is a high-water mark that never falls, and its units differ by platform (KiB on Linux, bytes on macOS) (§2.2, §2.3).
2. Linear: an unbounded retention (leak). Sawtooth: normal churn with GC or cache eviction. Plateau: bounded caches, or allocator retention and fragmentation (§3).
3. Arena and heap retention: pymalloc arenas and glibc heaps are pinned by a few survivors, and freed memory is reused but not returned to the OS (§12, §13; docs 07, 08).
</details>

---

## 37 — Generics and protocols  ([chapter](37-generics-and-protocols.md))
**Time:** ~2 h · **Needs:** 3.13, `mypy`, `pyright`. Also do §18 labs 1, 2 and 3.

- [ ] **37.1 Predict the checker** *(Level: Core)*
  - **Goal:** Train variance intuition against a real checker.
  - **Do:** Write one file with 6 assignments: `list[int]` → `list[float]`, `Sequence[int]` → `Sequence[float]`, `dict[str, int]` → `Mapping[str, float]`, `Callable[[float], None]` → `Callable[[int], None]`, a class with a matching `close()` → a `Protocol` with `close()`, and `frozenset[bool]` → `frozenset[int]`. Run `mypy --strict v.py` and `pyright v.py`.
  - **Predict:** Pass or error for each line, before you run anything.
  - **Verify:** Your score out of 6. Explain each miss in terms of invariance, covariance or contravariance (§2, §4).
- [ ] **37.2 The runtime price of `runtime_checkable`** *(Level: Core)*
  - **Goal:** Measure what structural `isinstance` costs.
  - **Do:** Time with `pyperf timeit`: `isinstance(x, SupportsClose)` for a `@runtime_checkable` Protocol with 1 and with 5 methods, `isinstance(x, ABCClose)` for an ABC, and `hasattr(x, "close")`.
  - **Predict:** The ns for each.
  - **Verify:** A table. The protocol check costs far more than the ABC check and grows with the method count. Connect this to §10 (it checks presence only, not signatures).
- [ ] **37.3 A ParamSpec decorator, and the `Any` hole** *(Level: Core)*
  - **Goal:** See signature preservation in the checker's output.
  - **Do:** Write `def retry[**P, R](f: Callable[P, R]) -> Callable[P, R]` using PEP 695 syntax. Decorate `def fetch(url: str, timeout: float) -> bytes`, then `reveal_type(fetch)` and call `fetch(1)`. Repeat with `Callable[..., Any]`.
  - **Predict:** What each version reveals, and whether `fetch(1)` is flagged.
  - **Verify:** The ParamSpec version preserves the signature and flags the bad call. The `...` version silently accepts it (§7).
- [ ] **37.4 Shape types with TypeVarTuple** *(Level: Stretch)*
  - **Goal:** Push generics to the edge where checkers disagree.
  - **Do:** Write `class Array[*Shape]` and `def transpose[A, B](x: Array[A, B]) -> Array[B, A]`, using `NewType` dimensions `H` and `W`. Pass the result of `transpose` to a function that expects `Array[H, W]`.
  - **Verify:** Both checkers flag the mismatch, or you record where they differ (§8, §17).

**Checkpoint (closed book):**
1. Why is `list` invariant but `Sequence` covariant?
2. What does `@runtime_checkable` check, and what does it not?
3. What problem does `ParamSpec` solve that `TypeVar` cannot?
<details><summary>Answers</summary>

1. `list` is mutable. If `list[int]` were a `list[float]`, you could append a float to it. `Sequence` is read-only, so a sequence of subtypes is safe to read as a sequence of the supertype (§2).
2. Only that the named attributes and methods exist. It does not check signatures, types or arity (§10).
3. It captures a callable's whole parameter list (names, kinds, defaults), so a decorator's wrapper can keep the exact signature (§7).
</details>

---

## 42 — Runtime code manipulation  ([chapter](42-runtime-code-manipulation.md))
**Time:** ~2.5 h · **Needs:** 3.13, `hyperfine`, `pyperf`. Also do §12 labs 1, 4 and 5.

- [ ] **42.1 Import cost, then a lazy import** *(Level: Core)*
  - **Goal:** Find startup time you can delete.
  - **Do:** Run `python3.13 -X importtime -c "import json, asyncio, decimal" 2> imp.log`, then `sort -t'|' -k2 -n imp.log | tail -5`. Move the heaviest import in a CLI script of yours into the function that uses it, and compare the two versions with `hyperfine -N`.
  - **Predict:** The top 3 cumulative importers, and the ms saved.
  - **Verify:** Report the top 3 from the log and the hyperfine delta ± σ (§4).
- [ ] **42.2 Decorator call overhead, per layer** *(Level: Core)*
  - **Goal:** Put a price on each `*args, **kwargs` wrapper.
  - **Do:** Wrap a trivial function in 0, 1 and 3 `functools.wraps` decorators and time each with `pyperf timeit`. Run `dis` on one wrapper and count its `CALL` instructions.
  - **Predict:** ns per layer.
  - **Verify:** Per-layer cost ≈ constant. Relate it to the cost of calling with `*args`/`**kwargs`, which blocks specialization (doc 20 §11).
- [ ] **42.3 An AST rewrite you can see in `dis`** *(Level: Core)*
  - **Goal:** Transform code and prove the change at the bytecode level.
  - **Do:** Write an `ast.NodeTransformer` that rewrites `x ** 2` into `x * x` (a `Name` operand only). Run `compile(ast.fix_missing_locations(tree), ...)`, then `dis` before and after. Time a hot loop both ways. Then feed it a class whose `__pow__` and `__mul__` differ.
  - **Predict:** The speedup, and whether the rewrite is always safe.
  - **Verify:** `BINARY_OP (**)` becomes `BINARY_OP (*)`. Report the speedup and show the class that proves the rewrite unsound (§6, §11).
- [ ] **42.4 Stale type cache: patch a builtin behind CPython's back** *(Level: Stretch)*
  - **Goal:** See the C-type wall and the method cache.
  - **Do:** Confirm `int.__flags__ & (1 << 8)` (`Py_TPFLAGS_IMMUTABLETYPE`) and that `int.double = ...` raises `TypeError`. In a subprocess: call `(5).double()` (it fails), then `gc.get_referents(int.__dict__)[0]["double"] = lambda s: s * 2`, then call `(5).double()` again. Then run `ctypes.pythonapi.PyType_Modified(ctypes.py_object(int))` and call it once more.
  - **Predict:** The three outcomes.
  - **Verify:** It fails, fails again (the negative result is cached under the type's version tag), then returns 10. Explain this with §8 and doc 20 §7.

**Checkpoint (closed book):**
1. Why is there no safe `eval`, even with `{"__builtins__": {}}`?
2. What does `mock.patch` need as its target, and why?
3. What are the steps of an import, from `import x` to a module object?
<details><summary>Answers</summary>

1. Every object reaches `object.__subclasses__()` through attribute traversal (for example `().__class__.__base__`), and from there dangerous classes and functions (§3).
2. The name *where it is looked up*, such as `app.time`, not `time.time`. `from x import y` binds a new name in the importing module (§8).
3. Check `sys.modules`, then ask the `sys.meta_path` finders for a spec, then the loader creates and executes the module, which is inserted into `sys.modules` before execution (§4).
</details>

---

## 43 — Testing strategy  ([chapter](43-testing-strategy.md))
**Time:** ~2.5 h · **Needs:** 3.13, `pytest`, `hypothesis`, `coverage`. Also do §17 labs 1, 4 and 5.

- [ ] **43.1 How many examples until Hypothesis finds your bug?** *(Level: Core)*
  - **Goal:** Measure a property test's detection power.
  - **Do:** Implement `LRU(capacity)` and a property test against a dict-plus-order model. Inject an off-by-one into eviction (evict when `len > capacity + 1`). Run `pytest --hypothesis-show-statistics` with 20 different `--hypothesis-seed=N` values.
  - **Predict:** The median number of examples to the first failure.
  - **Verify:** Record the median and the maximum, and the shrunk counterexample. Say whether the default `max_examples=100` is enough (§7).
- [ ] **43.2 100% coverage, bug still there** *(Level: Core)*
  - **Goal:** Show what coverage does not measure.
  - **Do:** `f(a, b)` has two independent `if`s, and the bug fires only when both are true. Write 2 tests that give 100% line and branch coverage (`coverage run --branch -m pytest && coverage report -m`) without hitting the (True, True) path. Then add a Hypothesis test.
  - **Predict:** Coverage %, and whether Hypothesis finds the bug.
  - **Verify:** 100% with the bug missed, then the Hypothesis failure. Explain this with §11: coverage counts edges, not paths.
- [ ] **43.3 The fixture scope trap** *(Level: Core)*
  - **Goal:** Make a test's result depend on test order.
  - **Do:** A `scope="session"` fixture returns a `dict`. `test_a` mutates it and `test_b` asserts it is empty. Run `pytest t.py::test_a t.py::test_b` and then the reverse order. Run `pytest --setup-show` as well.
  - **Predict:** Which order fails.
  - **Verify:** A-then-B fails and B-then-A passes. Fix it with scope or a copy, and show that `--setup-show` exposes the shared instance (§3).
- [ ] **43.4 A timing flake under load** *(Level: Stretch)*
  - **Goal:** Treat flakiness as a systems problem.
  - **Do:** Write a test asserting that an operation finishes in under 10 ms, and run it 500 times with `pytest --count=500`: idle, then with `stress-ng --cpu $(nproc)` running. Then inject a fake clock and rerun.
  - **Predict:** The failure rate idle and under load.
  - **Verify:** Both rates, then 0 with the injected clock. Compute how often 3 retries would hide a real bug that fails 50% of runs: 1 − 0.5⁴ (§13.4).

**Checkpoint (closed book):**
1. Why do Hypothesis counterexamples come out small?
2. What does mutation testing measure that coverage cannot?
3. Why are retries on flaky tests a sensitivity trade?
<details><summary>Answers</summary>

1. Hypothesis records the choice sequence that produced a failing example and shrinks that sequence, then replays it, keeping only failing variants (§7).
2. Whether the tests *detect* changed behaviour. Coverage only says that code ran (§10).
3. A real bug that makes a test fail with probability p is reported only when all k+1 attempts fail, which happens with probability p^(k+1). You buy green builds by giving up detection of intermittent real bugs (§13.4).
</details>

---

## Capstone projects

Each takes 1–3 days, runs on a laptop (≥ 4 cores), and draws on several chapters. Write each one up
as a short report with a measurement table and one paragraph on "where my model stopped".

### C1 — One CPU pipeline, four ways (06, 11, 24, 26, 31)
- **Spec:** Choose a real CPU-bound task, such as JSON-to-feature extraction over 10^6 records or image thumbnailing with Pillow. Implement it as (a) threads on 3.13, (b) threads on 3.13t, (c) `multiprocessing` with `SharedMemory` input, and (d) an asyncio front end plus `ProcessPoolExecutor`.
- **Acceptance:** All four produce identical output (checksum). Every number is a median of ≥ 5 runs with a spread. There is a 3.13t run with `PYTHON_GIL=1` as a control.
- **Measure:** Wall time at 1/2/4/8 workers, total CPU-seconds, peak PSS (sum over processes from `smaps_rollup`), startup cost, `strace -c -f` syscalls per record, and the futex count (24.3).

### C2 — An in-process sampling profiler (12, 20, 32)
- **Spec:** A background thread samples `sys._current_frames()` at 100 Hz. It folds the stacks into `func;func;func count` lines, which `flamegraph.pl` renders. The tool is a context manager with a CLI wrapper.
- **Acceptance:** Its top-5 functions on a mixed workload agree with `py-spy record -f raw` within 5 percentage points each. Overhead is < 5% at 100 Hz, measured with pyperf.
- **Measure:** Overhead at 10/100/1000 Hz. The disagreement with py-spy on a workload that holds the GIL in C (`sorted` of a huge list) is the safe-point bias; explain it with doc 20 §12 and doc 24 §4.

### C3 — Leak hunt in a small async service (08, 15, 17, 22, 29, 35)
- **Spec:** An asyncio HTTP-ish service (`asyncio.start_server`) with three planted leaks: a C-extension reference leak (17.2 variant A), an unbounded dict cache, and a cycle through a closure with a `__del__` that resurrects. Someone else plants them, or you plant them and wait a week.
- **Acceptance:** RSS stays flat (±5%) over a 30-minute soak after the fixes. For each leak, the report names the tool that found it and the tools that could not.
- **Measure:** An RSS time series before and after, `gc.callbacks` pause p99, `sys.gettotalrefcount` slope on the debug build, and memray leak totals.

### C4 — A cache-aware columnar record store (01, 07, 11, 16, 35)
- **Spec:** Store 10^7 records with 5 numeric fields in two ways: a list of dicts and a columnar `mmap`-backed file of `array`/NumPy columns. Support a filter-and-sum query, and share the store read-only with 4 forked workers.
- **Acceptance:** Both designs give identical query results. The columnar version's per-worker private memory is < 5% of the store size.
- **Measure:** Bytes per record (RSS), query time, cachegrind D1/LL miss rates for the query kernel, `Private_Dirty` per worker after 1000 queries, and the page faults at first touch vs warm.
