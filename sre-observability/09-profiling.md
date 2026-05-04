# 09 — Profiling: Continuous Profiling, eBPF, pprof, Symbolization

> The fourth observability signal. Metrics tell you *that* a service is slow; logs tell you *what message* it printed; traces tell you *which call path* was slow. Profiles tell you **which line of code** burned the CPU, allocated the memory, blocked on the lock. This chapter is about how profilers actually collect a stack trace from a running process, ship it across a fleet, symbolize stripped binaries, store billions of stack samples per day, and let you diff "today vs yesterday" or "v2.6.13 vs v2.6.12" in milliseconds. By the end you should be able to roll out continuous profiling on a polyglot fleet (Go, Java, Python, Node, Rust, C++) without a 20% CPU regression, and pick the right backend (Parca, Grafana Pyroscope, Polar Signals, Datadog Profiler).

This chapter assumes you've read [03 — Instrumentation](./03-instrumentation.md), [04 — Collection & Edge](./04-collection-and-edge.md), and the framing of metrics/logs/traces in [chapters 6, 7, 8](./06-metrics-storage.md). The query layer is [chapter 10](./10-query-layer.md), dashboards are [chapter 11](./11-dashboards.md), and cardinality-as-cost discipline is [chapter 18](./18-cardinality-and-cost.md). Profiling is the last leg of the observability tetrahedron — and it closes the gap between "we know the service is slow" and "we know which line of code is slow."

---

## Table of Contents

1. [What a Profile Actually Is](#1-what-a-profile-actually-is)
2. [The pprof Data Format](#2-the-pprof-data-format)
3. [Stack Walking — The Hard Part](#3-stack-walking--the-hard-part)
4. [Symbolization](#4-symbolization)
5. [In-Process Profilers](#5-in-process-profilers)
6. [System-Wide Profilers](#6-system-wide-profilers)
7. [Continuous Profiling Architecture](#7-continuous-profiling-architecture)
8. [Storage Layer](#8-storage-layer)
9. [Compression of Stack Traces](#9-compression-of-stack-traces)
10. [Symbolizer Pipeline at Ingest](#10-symbolizer-pipeline-at-ingest)
11. [Query Patterns](#11-query-patterns)
12. [Flame Graph Rendering](#12-flame-graph-rendering)
13. [Cost and Overhead](#13-cost-and-overhead)
14. [eBPF Deep Section](#14-ebpf-deep-section)
15. [Fleet Rollout Patterns](#15-fleet-rollout-patterns)
16. [Operational Pitfalls](#16-operational-pitfalls)
17. [Decision Tree](#17-decision-tree)
18. [End-to-End: Life of One CPU Sample](#18-end-to-end-life-of-one-cpu-sample)

---

## 1. What a Profile Actually Is

A profile is a **weighted set of stack traces**. Each sample says "at this moment, the program was executing this stack of N frames; assign it weight W." The whole profile is a histogram over stacks, summing to a total weight (e.g., total CPU time observed).

```
profile = {
  (stack_1, weight_1),
  (stack_2, weight_2),
  (stack_3, weight_3),
  ...
}

stack = [frame_top, frame_below, ..., frame_bottom]
        most-recently-called-fn → ... → main()
weight = number of samples × sampling period (cpu time, alloc bytes, etc.)
```

A flame graph is just this set rendered as a stacked-bar tree, with width proportional to weight (§12). A "top functions" report is the same set, summed over the leaf frames.

### 1.1 Profile types

```
CPU         what was on-CPU at sample time            (sampling profiler)
Wall clock  what was scheduled OR waiting             (sampling profiler, wall-time clock)
Allocations what code allocated bytes/objects         (instrumented at malloc)
Goroutine   what goroutines existed at sample time   (Go-specific)
Mutex       contention on locks                       (Go: blockprofile / mutexprofile)
Off-CPU     what code was BLOCKED (and on what)       (eBPF: kernel scheduler tracepoints)
Locks       which lock primitives blocked threads     (Java: jstack / async-profiler --lock)
Threads     thread state distribution                  (less common)
Java mixed  CPU + alloc combined                       (async-profiler -e cpu,alloc)
```

CPU profiling is by far the most common. Off-CPU profiling — finding "where my service was blocked, not where it was burning CPU" — is the rare-but-game-changing kind for I/O-bound workloads.

### 1.2 Sampling vs instrumenting

```
SAMPLING PROFILER                            INSTRUMENTING PROFILER
─────────────────                            ──────────────────────
fire a timer at 99 Hz                        wrap every function:
on tick: walk current thread's stack           on entry: t0 = now()
record (stack, weight=10ms)                    on exit:  t1 = now(); record(stack, t1-t0)

✓ low overhead (<2% at 99 Hz)                ✗ high overhead (often 10-50%)
✓ language-agnostic (with eBPF)              ✓ exact (no statistical noise)
✓ catches all code, even leaf libraries      ✗ requires recompilation/instrumentation
✗ statistical only (must aggregate)          ✗ slow code distorts measurements
```

**Continuous profiling** = sampling profilers. Instrumentation only makes sense for "I want to profile this one function call at this moment", not for fleet-wide always-on observability.

### 1.3 Why 99 Hz?

The default sampling rate in most profilers is 99 Hz (or 19, 49). Why these prime numbers and not 100?

```
The OS scheduler wakes processes on 100 Hz boundaries (HZ=100 by default).
A profiler firing at 100 Hz lock-steps with the scheduler, biasing samples
toward kernel-internal timer-handling code.

A prime-number rate (97, 99) is offset from any multiple of 10 ms,
guaranteeing the sampler observes the program at "random" phases of its
execution.
```

99 Hz × 60 s = 5,940 samples per minute per process. Statistically that's enough to characterize the top-20 functions. Higher rates (999 Hz) = better tail visibility but 10× the overhead and storage.

### 1.4 What a sample carries

```
sample {
  stack:        [pc_0, pc_1, pc_2, ..., pc_N]   // program counters / addresses
  values:       [10ms_cpu]                       // can be multi-dimensional
  labels:       {pid=1234, thread=worker-7}      // optional context
  resource:     {service=checkout, host=...}     // resolved at ingest
}
```

The raw addresses (`pc_i`) require **symbolization** (§4) to become "main.handleCheckout / db/sql.Conn.Query / runtime/...". Many architectural decisions hinge on whether you symbolize at the agent (less data shipped, debug info needed locally) or at ingest (raw addresses shipped, debug info centralized).

> **Mental model.** A profile is a *streaming histogram of stacks*. Continuous profiling is just *that same histogram, kept for weeks, indexed by service+version+time, queryable as a flame graph or diff*. Once you internalize this, the whole architecture is "how cheaply can I store many such histograms."

---

## 2. The pprof Data Format

The `Profile` protobuf — originally Google's pprof — is the lingua franca. Every modern profiler emits it; every modern store ingests it; every visualizer reads it.

### 2.1 The schema

```
message Profile {
  repeated ValueType sample_type = 1;        // e.g., [{type="cpu",unit="nanoseconds"},
                                              //         {type="samples",unit="count"}]
  repeated Sample    sample      = 2;        // one per distinct stack
  repeated Mapping   mapping     = 3;        // ELF/DLL mappings: addr range → file
  repeated Location  location    = 4;        // resolved-or-unresolved frame
  repeated Function  function    = 5;        // resolved function metadata
  repeated string    string_table = 6;       // dictionary-encoded strings
  int64              time_nanos    = 9;
  int64              duration_nanos = 10;
  int64              period         = 12;    // 1e7 = 10ms = 100Hz
  ValueType          period_type    = 11;
  repeated Label     comment        = 13;
}

message Sample {
  repeated uint64 location_id = 1;           // top-of-stack first
  repeated int64  value       = 2;           // [cpu_ns, samples]
  repeated Label  label       = 3;           // {key="thread", str=42}
}

message Location {
  uint64           id         = 1;
  uint64           mapping_id = 2;
  uint64           address    = 3;           // raw PC
  repeated Line    line       = 4;           // function_id, line; > 1 for inlined
  bool             is_folded  = 5;
}

message Function {
  uint64 id          = 1;
  int64  name        = 2;        // index into string_table
  int64  system_name = 3;        // mangled name
  int64  filename    = 4;
  int64  start_line  = 5;
}

message Mapping {
  uint64 id              = 1;
  uint64 memory_start    = 2;
  uint64 memory_limit    = 3;
  uint64 file_offset     = 4;
  int64  filename        = 5;
  int64  build_id        = 6;
  bool   has_functions   = 7;
  bool   has_filenames   = 8;
}
```

### 2.2 Why pprof won

- **Multi-dimensional values.** A single sample can carry `(cpu_ns, alloc_bytes, alloc_objects)`. One profile, multiple views.
- **String dictionary.** Function names like `runtime/proc.go:Lock` repeat thousands of times in a profile; storing them as int64 indexes into a single string table cuts a 10 MB profile to 1 MB.
- **Lazy symbolization.** A `Location` may be a raw `(mapping_id, address)` until ingest, deferring symbol resolution to a place that has the debug info.
- **Build IDs in mappings.** The GNU build-id of the binary is shipped with the profile, enabling cross-host symbolization (§4).
- **Foldable inlines.** A single PC with multiple `Line` entries represents inlined functions; visualizers can fold or expand.

### 2.3 The on-disk size

Typical 60-second CPU profile of a busy Go service:

```
Raw protobuf:        ~600 KB
After gzip:          ~80 KB
After zstd(3):       ~60 KB
```

A fleet of 1000 services × 1 profile/min × 60 KB = ~85 GB/day raw, ~10–15 GB/day on a deduplicated store.

---

## 3. Stack Walking — The Hard Part

The profiler has 50 microseconds to walk the current thread's call stack. The choice of *how* defines the entire profiler.

### 3.1 Frame pointer walking

Compiler emits a **frame pointer** in `RBP` (`%rbp` on x86_64) at every function entry:

```
function prologue:
  push %rbp
  mov  %rsp, %rbp        # save old frame ptr, set new
  ...
function epilogue:
  pop  %rbp
  ret
```

Walking is then trivial:

```c
uint64_t pc = current_pc();
uint64_t rbp = current_rbp();
while (rbp != NULL) {
    push_frame(pc);
    pc  = *(uint64_t*)(rbp + 8);    // saved return address
    rbp = *(uint64_t*)rbp;          // saved old rbp
}
```

**Speed**: 5–20 ns per frame. **Reliability**: requires `-fno-omit-frame-pointer` at compile time. Most distros (Debian, Fedora, Ubuntu) **omitted frame pointers** for ~20 years to save the one register; this is changing in 2023–2024 (Fedora 38+, Ubuntu 24.04+ default ON for AMD64). Until the entire userspace stack has frame pointers, you cannot rely on this method alone.

### 3.2 DWARF unwinding

When frame pointers are absent, the compiler still records *unwind tables* in `.eh_frame` (used for C++ exception handling) and `.debug_frame` (DWARF debug info). These tables describe, for each PC, "to recover the previous frame, do X to the CPU registers."

```
.eh_frame entry for fn `foo`:
  pc range: 0x40010c .. 0x40012f
  CFA = rsp + 16                        # canonical frame address
  return address = [CFA - 8]
  rbp           = [CFA - 16]
  rbx           = unchanged
  ...
```

The unwinder evaluates this for the current PC. **Speed**: 100–1000 ns per frame, dominated by table lookup. **Reliability**: high, requires only the binary itself (no rebuild). The cost: in-kernel/in-eBPF DWARF interpretation is hard — eBPF in particular has a verifier limit on instructions, so naive DWARF unwinding doesn't fit.

Parca and Polar Signals solved this by **compiling DWARF unwind tables into BPF map data** at agent start; the eBPF program just looks up "for this PC, what's the offset to the saved RBP" in a sorted table. This is the breakthrough that made language-agnostic eBPF profiling viable in 2022.

### 3.3 Language-specific unwinders

```
Go         gopclntab      — Go's own table mapping PC → function/file/line.
                            Frame pointers are emitted by Go (since Go 1.7+ on amd64).
                            Stack walking is fast and reliable.

Java       AsyncGetCallTrace — JVM internal API; gives a stack of jmethodIDs.
                              async-profiler uses this + perf_events for safe
                              sampling (avoids safepoint bias). JFR uses safepoints
                              and is bias-prone.
                              perf-map agent emits /tmp/perf-<pid>.map mapping
                              JIT-compiled code addresses to method names.

Python     py-spy / sys.settrace — py-spy attaches via ptrace, walks the CPython
                                    interpreter's frame chain (PyFrameObject) using
                                    DWARF + Python-aware unwinder.

Node/V8    inspector / perf-prof — V8's --perf-basic-prof writes a JIT map similar
                                    to JVM's perf-map.

.NET       ETW + EventPipe       — Windows ETW; on Linux, EventPipe + dotnet-trace.
                                    The .NET runtime emits its own stack samples.
```

The polyglot challenge: a fleet has Go binaries (frame pointers OK), Java services (need perf-map + JVMTI), Python workers (need py-spy or PyFrame-aware eBPF), and C++ legacy (need DWARF). A single fleet-wide profiler must handle all of them.

### 3.4 eBPF stack walking

The eBPF profiler attaches a **perf_event** sampler. On each tick, the eBPF program executes in kernel context, walking the current thread's stack:

```c
// simplified kernel-side eBPF
SEC("perf_event")
int sample_stack(struct bpf_perf_event_data *ctx) {
    struct stack_key key = {0};
    key.pid    = bpf_get_current_pid_tgid() >> 32;
    key.kernel_stack_id = bpf_get_stackid(ctx, &kernel_stacks, BPF_F_REUSE_STACKID);
    key.user_stack_id   = bpf_get_stackid(ctx, &user_stacks,
                                          BPF_F_USER_STACK | BPF_F_REUSE_STACKID);
    increment_count(&samples, &key);
    return 0;
}
```

Two BPF maps:

- `BPF_MAP_TYPE_STACK_TRACE`: keyed by stack_id, value is `[pc_0, pc_1, ..., pc_N]`. Capped at **127 frames** by the kernel. Deeper stacks are truncated.
- `kernel_stacks` and `user_stacks` are separate; userspace stacks may need DWARF (no frame pointers) — that's where Parca's compile-DWARF-to-BPF approach kicks in.

The userspace-side agent reads the `samples` map periodically, joins with `user_stacks` and `kernel_stacks` to get the actual addresses, ships them as pprof profiles.

> **Pitfall.** `BPF_F_REUSE_STACKID` is the standard flag. If two threads have the *same* stack hash but the second thread's stack is different (rare, hash collision), the older entry is silently kept. Stack-id collisions are real but rare; expect a small fraction of samples with subtly wrong stacks.

---

## 4. Symbolization

Resolving raw program counters into `(function, file, line)`. The most operationally complex part of running a profiling fleet.

### 4.1 Build IDs

Linux ELF binaries carry a **GNU build-id** — an SHA1 hash of (parts of) the binary's contents — in the `.note.gnu.build-id` section. Tools:

```
$ readelf -n ./mybinary | grep 'Build ID'
Build ID: 7e5b6a82e3f9c1a4d3c9e1...

$ file /usr/lib/debug/.build-id/7e/5b6a82e3f9c1a4d3c9e1... .debug
ELF 64-bit LSB shared object, ... not stripped
```

The convention: stripped binaries are shipped to production; their full debug info lives in `.debug` files keyed by build-id under `/usr/lib/debug/.build-id/<2 chars>/<rest>.debug` (Fedora/Debian convention). The profiler ships `(address, build_id)` pairs; the symbolizer fetches the matching `.debug` file and resolves.

### 4.2 debuginfod

A standard HTTP service (RFC: <https://www.mankier.com/8/debuginfod>) that maps `build_id → debug-info bundle`:

```
GET /buildid/<build_id>/executable
GET /buildid/<build_id>/debuginfo
GET /buildid/<build_id>/source/<path>
```

Fedora and Red Hat operate public debuginfod (`https://debuginfod.fedoraproject.org/`) for stock distro binaries. Internal teams run their own debuginfod-compatible servers indexing every artifact built by their CI pipeline. Parca and Pyroscope can pull debug info from such servers.

### 4.3 Local debug bundles

Alternative to debuginfod: at deploy time, the same artifact pipeline that ships the binary also pushes the matching `.debug` to a known location (e.g., S3 keyed by build-id). The symbolizer fetches from there.

### 4.4 JIT runtimes

JIT-compiled code (Java, V8, .NET, Erlang) doesn't have static symbols — addresses move and methods get inlined. The conventional escape hatch:

```
Java + perf-map-agent:
  generates /tmp/perf-<pid>.map with one line per JIT method:
    7f2a1c2c0000 1234 ClassName::methodName
  perf and async-profiler read this file.

Java + JFR (Java Flight Recorder):
  built into the JVM; records its own samples + symbols.
  Pro: zero extra agent. Con: safepoint bias (samples skewed to safepoints).

Java + async-profiler (recommended):
  uses AsyncGetCallTrace + perf_events → unbiased samples
  emits jfr or pprof; flame graphs work natively.

V8 / Node:
  --perf-basic-prof flag writes /tmp/perf-<pid>.map identical to Java's.

.NET:
  EventPipe + dotnet-trace; Linux jitdump format.
```

Production agents handle these by reading the per-pid map files at symbolize time.

### 4.5 Container/k8s wrinkle

The profile is collected on the **host**; the binary lives in a **container image**. The host doesn't have the symbols.

```
host ────────────────────────────────────
  ├── /proc/<pid>/exe   → /var/lib/docker/overlay/.../merged/usr/local/bin/myapp
  ├── perf_event sample taken here
  └── stack: pc=0x40010c, build_id=7e5b...

agent (DaemonSet on host) ────────────────
  ├── reads /proc/<pid>/exe, computes build_id
  ├── reads /proc/<pid>/maps for memory mappings
  └── ships (build_id, addresses) to ingestor

ingestor / symbolizer ────────────────────
  ├── lookup build_id in debuginfod
  ├── if not present: fetch the *image* the container was built from
  │     (image registry → blob with .debug / unstripped binary)
  ├── resolve addresses → function/line
  └── store symbolized profile
```

The "container image symbol fetcher" is the production-critical piece. Parca's recommended pattern is "every CI artifact pushes its debuginfo to a debuginfod-compatible store keyed by build-id." Without that, the ingestor only resolves stock distro libraries; user code shows up as `0x7fff1c2c0010` in the flame graph.

### 4.6 Online vs offline symbolization

```
ONLINE (at query time)                      OFFLINE (at ingest time)
─────────────────────                       ──────────────────────
profile stored as raw addresses             profile stored as resolved frames
 + build_ids                                 (function, line indexes)
                                            
✓ smaller storage (no string table bloat)   ✓ fast queries (no per-query symbolize)
✓ can re-symbolize with newer debug info    ✗ stuck with whatever symbols were available
✗ first query is slow (lookup + cache)         at ingest time
                                            ✓ debug info doesn't have to live forever
                                            ✗ larger storage (function names per profile)
```

Most production stores do **offline symbolization at ingest** (Parca, Pyroscope, Polar Signals). Online-only is rare and used when debug info changes frequently.

---

## 5. In-Process Profilers

Profilers that live inside the application's address space.

### 5.1 Go: `runtime/pprof` and `net/http/pprof`

```go
import (
    "net/http"
    _ "net/http/pprof"
)

func main() {
    go http.ListenAndServe(":6060", nil)
    // ... rest of program
}
```

Endpoints exposed:

```
/debug/pprof/profile?seconds=30   CPU profile (default 30s)
/debug/pprof/heap                 heap allocations (current)
/debug/pprof/allocs               heap allocations (cumulative)
/debug/pprof/goroutine            goroutine count by stack
/debug/pprof/block                blocking events (must enable runtime.SetBlockProfileRate)
/debug/pprof/mutex                mutex contention (must enable runtime.SetMutexProfileFraction)
/debug/pprof/threadcreate         threads created
/debug/pprof/trace?seconds=5      execution trace (a different beast)
```

The CPU profile is signal-based: `runtime.SetCPUProfileRate(100)` sets `setitimer(ITIMER_PROF, ...)` which delivers `SIGPROF` to a runtime-installed handler that walks the goroutine's stack. Frame pointers + gopclntab make this fast and accurate.

Pulling profiles:

```
$ go tool pprof http://prod-svc:6060/debug/pprof/profile?seconds=30
$ go tool pprof -http=:8080 cpu.pprof    # interactive flame graph
$ go tool pprof -base old.pprof new.pprof  # diff
```

For continuous profiling, agents (Parca-Agent or Pyroscope's Go SDK) hit these endpoints periodically and ship pprof files to the backend.

### 5.2 JVM: async-profiler (the default for new fleets)

```
$ java -agentpath:/path/to/libasyncProfiler.so=start,event=cpu,file=profile.html MyApp

$ java -jar app.jar
# in another shell:
$ jcmd <pid> JFR.start name=cpu duration=30s settings=profile filename=cpu.jfr
$ asprof -d 30 -e cpu -f profile.html <pid>
```

Or via JFR:

```
java -XX:StartFlightRecording=duration=60s,filename=app.jfr,settings=profile MyApp
```

async-profiler uses `AsyncGetCallTrace` (an internal JVM API) plus `perf_events` to capture stacks **without safepoints** — meaning samples are taken at any instruction, not just safepoints. The result: unbiased CPU samples, far more accurate than JFR's safepoint-only sampling for CPU work that's optimized into long stretches between safepoints.

Pyroscope and Parca embed async-profiler as their JVM backend; the agent injects the .so and harvests profiles periodically.

### 5.3 Python: py-spy

```
$ py-spy record -o profile.svg -d 30 --pid 1234
$ py-spy top --pid 1234         # live top-N
$ py-spy dump --pid 1234        # current stack of every thread
```

py-spy is a Rust binary that uses `ptrace` (or process_vm_readv for less-invasive sampling) to read the target process's memory. It walks the CPython interpreter's frame chain by knowing the layout of `PyFrameObject` for the running CPython version. **Sampling overhead**: ~1% at 100 Hz. **No code changes** required in the target process.

### 5.4 Node.js: clinic.js and node --prof

```
$ clinic flame -- node app.js
$ clinic doctor -- node app.js          # multi-signal diagnostic
$ node --prof app.js                    # writes isolate-*.log
$ node --prof-process isolate-*.log    # process the v8 log
```

clinic.js wraps several profilers into one workflow. For continuous profiling, agents speak to V8's inspector protocol or use `--perf-basic-prof`.

### 5.5 .NET

```
$ dotnet-trace collect -p <pid> --duration 00:00:30 --providers Microsoft-DotNETCore-SampleProfiler
$ dotnet-counters monitor -p <pid>
```

EventPipe is the cross-platform profiling API (Windows uses ETW; Linux and macOS use EventPipe). The output is `.nettrace` which can be converted to flame graphs.

### 5.6 Rust: pprof-rs

```rust
use pprof::{ProfilerGuardBuilder, Symbol};

fn main() {
    let guard = ProfilerGuardBuilder::default()
        .frequency(99)
        .blocklist(&["libc", "libgcc"])
        .build()
        .unwrap();

    // ... do work ...

    if let Ok(report) = guard.report().build() {
        let mut file = std::fs::File::create("profile.pb").unwrap();
        report.pprof().unwrap().write_to_writer(&mut file).unwrap();
    }
}
```

Uses `setitimer` + `backtrace-rs` for stack walking. Works well on Rust; less well on C/C++ libraries linked in (no frame pointers by default).

---

## 6. System-Wide Profilers

Profilers that operate at the kernel level and observe all processes.

### 6.1 Linux `perf`

The original. `perf record` registers a perf_event sampler:

```
$ perf record -F 99 -p <pid> -g -- sleep 30
$ perf report --stdio
$ perf script | stackcollapse-perf.pl | flamegraph.pl > profile.svg
```

`-F 99` = 99 Hz; `-g` = call-graph (stack traces). The `-g` mode defaults to "fp" (frame pointer); use `-g dwarf` for DWARF unwinding (slower, copies stack to userspace). `-g lbr` uses Last Branch Records on Intel CPUs (very fast, limited depth).

`perf record` writes to `perf.data` (binary). `perf script` decodes; `stackcollapse-perf.pl` (Brendan Gregg's) reduces it to "stack;sep;col1 count" format consumable by `flamegraph.pl`.

### 6.2 perf_event_open syscall

```c
struct perf_event_attr attr = {
    .type     = PERF_TYPE_SOFTWARE,
    .config   = PERF_COUNT_SW_CPU_CLOCK,
    .sample_type = PERF_SAMPLE_IP | PERF_SAMPLE_TID | PERF_SAMPLE_CALLCHAIN,
    .freq     = 1,
    .sample_freq = 99,
    .precise_ip = 2,
    .size     = sizeof(struct perf_event_attr),
};

int fd = perf_event_open(&attr, pid, cpu, group_fd, flags);
ioctl(fd, PERF_EVENT_IOC_ENABLE, 0);
// read samples from fd's mmap'd ring buffer
```

This is the syscall every Linux profiler ultimately calls — `perf record`, `eBPF` programs attached to perf events, async-profiler, all of them.

### 6.3 eBPF profilers

The new generation (2020+):

- **Parca-Agent** (Polar Signals, MIT license)
- **Grafana Pyroscope eBPF** (formerly Pixie / formerly profefe)
- **Polar Signals Cloud Agent** (commercial, similar tech)
- **Inspektor Gadget**, **opentelemetry-ebpf-profiler** (newer entrants; OTel donated Elastic's profiler in 2024)

Architecture (all roughly the same):

```
┌─ kernel ─────────────────────────────────────┐
│ perf_event_open (PERF_COUNT_SW_CPU_CLOCK,    │
│                  freq=99 Hz)                 │
│      ↓                                       │
│  eBPF program: walk user + kernel stack      │
│  store in BPF_MAP_TYPE_STACK_TRACE           │
│  increment counter in samples map            │
│      ↓                                       │
│  ringbuf or perf buffer to userspace         │
└──────────────────────────────────────────────┘
       ↓
┌─ userspace agent ────────────────────────────┐
│ read maps, resolve addresses to mappings     │
│ tag with k8s pod/container metadata          │
│ batch into pprof profiles                    │
│ upload to backend (parca/pyroscope/cloud)    │
└──────────────────────────────────────────────┘
```

The advantages:

- **Zero code change** in target processes.
- **Polyglot**: Go, Rust, C++, Python — all observed by the same agent.
- **System-wide**: covers kernel time, OS daemons, sidecar containers.
- **Low overhead**: ~0.5–1% CPU at 99 Hz.

The constraints:

- Kernel ≥ 4.9 for basic eBPF; ≥ 5.4 for production-grade unwinding; ≥ 5.13 for ringbuf.
- Frame pointers OR DWARF tables (compiled to BPF) required for userspace.
- 127-frame limit per stack from `bpf_get_stackid`.

### 6.4 /proc/kallsyms

For kernel symbols, the agent reads `/proc/kallsyms`:

```
ffffffff8108c4f0 T __schedule
ffffffff8108cb20 T schedule
ffffffff810d40d0 T run_timer_softirq
...
```

Address-to-symbol resolution for kernel addresses is a binary search on this table. The kernel hides addresses by default (`kptr_restrict=2`); profiling agents need `CAP_SYSLOG` or `kptr_restrict=1`.

### 6.5 BCC / bpftrace (one-off)

For ad-hoc profiling and debugging:

```
$ profile -F 99 -p <pid> 30 > stacks.txt   # BCC tool
$ bpftrace -e 'profile:hz:99 /pid == 1234/ { @[ustack] = count(); }'
```

BCC (BPF Compiler Collection) and bpftrace are the "interactive eBPF" tools. The continuous-profiling agents share the same kernel primitives.

---

## 7. Continuous Profiling Architecture

The shift from "I'll profile when there's a problem" to "every process is being profiled, all the time, kept for weeks."

### 7.1 The fleet model

```
service A pod 1 ──┐
service A pod 2 ──┤
...                ├──► node DaemonSet (eBPF agent)
service B pod 1 ──┤
service B pod 2 ──┘
                       │
                       ▼  push pprof every 60s, gzipped
                  ┌──────────────────┐
                  │  Profile gateway  │
                  │  (per-tenant      │
                  │   ingest, label   │
                  │   enrichment,     │
                  │   ratelimits)     │
                  └────────┬─────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │  Profile store    │
                  │  (Parca/Pyroscope │
                  │   /Polar Signals) │
                  └────────┬─────────┘
                           │
                           ▼
                  Object storage (S3/GCS) + indexes
```

### 7.2 Sample budget

A typical sane setting:

```
sampling rate:        19 Hz (lower than 99 Hz; less data, still good top-N)
sample window:        60 s
upload interval:      60 s
retention:            14 days hot, 90 days warm, 1 year archive
```

Per process: 19 × 60 = ~1140 samples per minute, deduped to a few hundred unique stacks. Per service: × pods × replicas. Per fleet: billions of samples per day.

### 7.3 Why "low rate, long retention" beats "high rate, short retention"

- High-rate samples pay overhead at the source (CPU + network).
- Long retention enables **regression detection**: "v2.6.13 is 3% slower than v2.6.12 because hash.go's MD5 path is hotter."
- The dominant value of a profile is the *change over time*, not the absolute snapshot.

### 7.4 Service discovery and labels

Every profile must be tagged with:

```
service.name        from k8s deployment label or env var
service.version     from image tag
host.name           from hostname
k8s.namespace       from pod metadata
k8s.pod.uid         from pod metadata
profile_type        cpu | alloc_objects | alloc_space | inuse_space | goroutine | mutex
```

These labels are the index keys in the storage layer (§8). Cardinality concerns from chapter 18 apply: don't add `request_id` as a profile label.

---

## 8. Storage Layer

The four production backends. All consume pprof; storage details differ.

### 8.1 Parca + FrostDB

Parca is the open-source backend from Polar Signals, Apache 2.0 licensed. Its storage engine is **FrostDB**, an embeddable Apache Arrow-native columnar store designed exactly for this workload.

Schema (logical):

```
columns (per profile sample)
─────────────────────────
profile_id     hash of (timestamp, labels, stack)
timestamp      ns since epoch
labels         Map(string, string)         ← service, version, etc.
stack_id       fk → stacks table
value          int64                        ← cpu_ns, alloc_bytes, etc.
value_type     enum

stacks table
─────────────
stack_id      hash of frame_ids list
frame_ids     List(frame_id)

frames table
─────────────
frame_id      hash of (function_id, line, address)
function_id   fk
line          int32
address       uint64                       ← raw, kept for re-symbolization

functions table
─────────────
function_id   hash of (name, filename, system_name)
name          string
system_name   string
filename      string
```

Insert flow: a new pprof is split into samples; each sample's stack is hashed and matched against `stacks` (insert if new); each frame matched against `frames`; functions are deduped. The result is **maximal sharing**: a `runtime.morestack_noctxt` frame appears once per service instead of once per sample.

Storage on disk: Parquet files written to local SSD (active) and then to S3 (compacted). A 60s profile of a busy Go service compresses from 600 KB raw pprof to ~30–50 KB on FrostDB after dedup, sometimes far less for repetitive workloads.

### 8.2 Grafana Pyroscope (formerly Pyroscope OSS, now part of Grafana)

Pyroscope evolved from a hosted service into Grafana's open-source profiling backend. Its architecture is **Loki-style**:

```
distributor → ingester → object storage
                            (chunks of segmented profiles)
                            (label index TSDB-style)
querier → object storage + ingesters
```

The "segment" is the storage unit: a time-bucketed bundle of profiles for one label set. The label index is the same TSDB code Loki uses (and Prometheus uses for series), repurposed for profile streams.

Profiles inside a segment are stored as compressed pprof; a query fetches segments matching the label selector, decompresses, merges, returns a flame graph.

### 8.3 Polar Signals Cloud

Commercial offering on top of the same Parca/FrostDB stack. Adds:

- A managed agent fleet
- Multi-tenancy with strong isolation
- Continuous symbolization via uploaded debug info
- Diff queries optimized for CI integration ("did this PR regress p95 CPU?")

### 8.4 Datadog Continuous Profiler / New Relic CodeStream

Closed-source, integrated with the rest of the vendor's APM. Pricing per host or per indexed-profile. Solid UX, strong language support (especially Java, Python, Go), and integrates flame graphs into trace views.

### 8.5 The dedup story

The single biggest cost optimization is **stack/frame/function deduplication across profiles**. A function called from N different paths is one row in `functions`; a stack `[A, B, C]` repeated across thousands of samples is one row in `stacks`.

```
Naive store: 1 profile = 600 KB × 1M profiles/day = 600 GB/day raw
Deduped store (FrostDB style):
  unique stacks per fleet ≈ 100k
  unique frames ≈ 1M
  unique functions ≈ 50k
  total: ~5–10 GB/day storage after compression
```

The dedup ratio on a real-world fleet is typically 30–60×.

---

## 9. Compression of Stack Traces

Stack traces compress phenomenally well because of three structural properties:

### 9.1 Function-name repetition

Across the fleet, the same function (`runtime.gcAssistAlloc`, `net/http.(*Server).serve`) appears in millions of stacks. A dictionary of function names + 32-bit indexes per frame is far smaller than embedded strings.

### 9.2 Common stack prefixes

Most CPU work lives in a small set of "hot stacks":

```
Top 1000 unique stacks typically cover 80–95% of all samples.
Top 100   unique stacks typically cover 50–70% of all samples.
Top 10    unique stacks typically cover 10–30% of all samples.
```

Storing these as **call tree** rather than as flat list (the **calltree** representation) lets you encode a stack as a path in the tree:

```
main
└── http.HandlerFunc.ServeHTTP
    └── handler.checkout
        ├── db.Query     ← stack 1
        ├── cache.Get    ← stack 2
        └── pricing.Compute
            └── http.Get ← stack 3
```

Encoding a stack as a node ID in this tree compresses dramatically — and is exactly what FrostDB and Pyroscope's segment writer do.

### 9.3 Time-series dedup

Across profiles, the *same call tree* recurs minute after minute. Storage layers segment by time and only write *new* nodes per segment.

### 9.4 Compression layers

```
Layer 1: shared dictionary of function names + filenames     ~10× reduction
Layer 2: call tree structure replacing flat stack lists      ~3× reduction
Layer 3: time-series dedup (per-segment incremental writes)  ~2-5× reduction
Layer 4: ZSTD on the columnar files                          ~3× reduction
─────────────────────────────────────────────────────────────────────────
Cumulative                                                    ~150-450×
```

A raw pprof of 600 KB ends up as 1.3–4 KB on disk. This is why "store every profile of every process forever" is genuinely affordable.

---

## 10. Symbolizer Pipeline at Ingest

The hardest moving part of a profiling backend.

### 10.1 The flow

```
agent ships pprof:
  - mappings (file_offset, build_id, pathname)
  - locations with raw addresses
  - some functions, mostly resolved by gopclntab on Go; mostly unresolved on C++

ingestor:
  1. for each Mapping with a build_id and unresolved Functions:
       look up build_id in symbol cache
       if miss:
         a. query debuginfod (internal first, then public fallback)
         b. on hit: download .debug, extract:
            - DWARF symbol info (function names, file, line per address range)
         c. cache in local symbol store (compressed) keyed by build_id
       on hit:
         resolve each address → (function, file, line)
  2. for each Location, fill in Line entries
  3. write resolved profile to FrostDB / Pyroscope segment store
```

### 10.2 The symbol cache

Per-build_id cached debug info, keyed in a fast lookup. Memory pressure: a fleet with 5000 distinct build-ids × 5 MB symbol info = 25 GB of cached symbols. LRU + S3 spill is the standard pattern.

### 10.3 Failure modes

```
Missing debug info:
  build_id not in any debuginfod
  → location stays unresolved: address shown in flame graph as 0x7fff...
  Mitigation: enforce "every CI build pushes debuginfo" in production policy.

Mismatched debug:
  binary stripped with one build, debug from another (rare with build-ids)
  → wrong function names in flame graph (silent corruption)
  Mitigation: hashes are checked; build-id mismatch is a hard error.

Stripped runtime libraries (libc, libpthread):
  fall back to public Fedora/Debian debuginfod
  Mitigation: maintain mirror inside the network; cache aggressively.

Container image purged:
  the container's image was deleted from the registry between deploy and profile ingest
  → debug info gone unless agent uploaded it eagerly
  Mitigation: upload debug bundles at build time, not on demand.
```

### 10.4 Re-symbolization

Some backends keep raw addresses + build_ids long-term, alongside resolved data. When a profile from 90 days ago has a `0x7fff...` frame and someone uploads the matching debug info today, the backend can re-resolve. Most production systems don't bother — they accept "after a year, some old profiles are partially unsymbolized."

---

## 11. Query Patterns

### 11.1 Top-N hottest functions

```
SELECT function.name, sum(value)
FROM samples
WHERE service='checkout' AND time >= now() - INTERVAL 1 HOUR
GROUP BY function.name
ORDER BY sum(value) DESC
LIMIT 20;
```

A fundamental SQL aggregation. Parca, Pyroscope, and Polar Signals all expose this as the "Top" view.

### 11.2 Flame graph

Reconstruct the call tree for a label-selected, time-bounded set of profiles, then render (§12).

### 11.3 Diff (commit A vs commit B)

```
flame_graph_A: profile of v2.6.12, last hour
flame_graph_B: profile of v2.6.13, last hour
diff = flame_graph_B − flame_graph_A   (per-stack signed difference)
```

Render with red = added time, blue = removed time. The single most valuable profiling artifact for engineers; tells you "your PR regressed CPU here."

### 11.4 Filter by label

```
service=checkout AND version=v2.6.13 AND region=eu-west-1
```

Index lookup, same as Loki / Prometheus. Cardinality discipline matters.

### 11.5 Allocation profile vs CPU profile

Same profile type, different `value_type`:

```
cpu_ns        sum of CPU time per stack
alloc_objects number of allocations per stack
alloc_space   bytes allocated per stack
inuse_objects live objects (heap snapshot)
inuse_space   live bytes (heap snapshot)
```

A query selects the value_type. Memory regressions look very different from CPU regressions — always check both when chasing a perf issue.

### 11.6 Off-CPU analysis

```
Where was my thread BLOCKED?

eBPF off-CPU profiler attaches to:
  - sched_switch tracepoint        (when a thread leaves CPU)
  - sched_wakeup tracepoint        (when a thread becomes runnable)
  records (stack at sleep, duration off-CPU)
```

Off-CPU flame graphs show "where my code was waiting" — futex, syscall, network I/O, GC pause. Game-changing for I/O-bound services (most services, in practice). Pyroscope and Parca both support off-CPU profiling via eBPF.

---

## 12. Flame Graph Rendering

Brendan Gregg's invention. The single most-influential observability visualization.

### 12.1 The algorithm

```
input: list of (stack, weight)
output: SVG with stacked bars

steps:
  1. Sort each stack so root is at index 0, leaf at end.
  2. Build a tree: root node = sum of all weights;
     each child = a function called from parent;
     child weight = sum of weights of stacks that pass through this child.
  3. Layout:
     row 0 = root, full width
     row 1 = direct children, side-by-side, widths proportional to weight
     row 2 = grandchildren, ...
     row N = leaves
  4. Render each function as a rectangle; color by hash(name) for visual distinction.
```

### 12.2 Two orientations

```
Flame graph (icicle bottom-up)              Icicle (top-down)
                                            
     ┌──leaf┌─leaf─┐                           ┌────────main─────────┐
     ├──f3──┤──f4──┤                           ├──f1──┬──f2──┤
     ├──f2──┴──────┤                           ├──f3──┤──f4──┤
     ├──main───────┤                           └─leaf─┴─leaf─┘
     └─────────────┘                           
   wide bottom = root                        wide top = root
```

Both communicate the same data; choice is preference. Rust's flamegraph crate defaults to icicle; perf's traditional output is bottom-up.

### 12.3 Differential flame graphs

```
input: profile A (baseline) and profile B (new)
output: same layout, but each function colored:
        red if B > A  (regression)
        blue if A > B (improvement)
        proportional saturation = magnitude of change
```

This is the killer view in CI: "compare profile of build N to build N-1; flag regressions > 5%." Polar Signals and Datadog both ship CI integrations that surface this in PR comments.

### 12.4 Folding and search

UIs expose:

```
- "fold" identical leaves: collapse runtime.systemstack into a single entry
- "search": highlight all stacks matching a regex
- "zoom": click a function, treat it as the new root, recompute layout
```

Pyroscope and Parca's UIs implement these client-side over the rendered flame graph.

---

## 13. Cost and Overhead

### 13.1 CPU overhead at the source

```
Pure pprof CPU profile (Go): ~0.5% at 100 Hz, ~1.5% at 999 Hz
async-profiler (JVM):        ~0.5% at 100 Hz
py-spy:                      ~1-2% at 100 Hz (ptrace overhead)
eBPF profiler:               ~0.5-1% at 99 Hz on modern kernels
```

These are aggregate numbers; spikes during sampling are higher but brief. For a fleet, plan on a 1% CPU tax — acceptable in exchange for never-have-to-write-to-disk debugging.

### 13.2 Memory overhead

In-process profilers maintain ring buffers for samples (a few MB). eBPF profilers use kernel maps (configurable, typically 64 MB total per node). Symbol caches in the agent and backend dominate memory usage on the receiver side.

### 13.3 Network

```
Per process: 60 KB compressed pprof every minute = 1 KB/s
Per node (50 pods): 50 KB/s = 0.05 Mbps
Per cluster (1000 nodes): 50 Mbps total profile traffic
```

Negligible compared to log shipping. Network is rarely the bottleneck.

### 13.4 Storage

```
Raw:        ~85 GB/day per 1000 services × 1 profile/min
After dedup + compression: ~5-15 GB/day
30-day retention: ~150-450 GB
1-year retention: ~2-5 TB
```

On S3 at ~$0.023/GB-month: a year of fleet-wide profiles costs ~$50–150/month in storage. Cheap.

### 13.5 The math: do you save more than you spend?

```
1% CPU overhead × 1000 services × 4 cores × $0.05/core-hr × 24h × 30d
= ~$1,440/month

Storage + ingest of profiles:
= ~$300/month

If continuous profiling helps you avoid ONE engineer-week of debugging per quarter
(typical at scale), you save ~$8,000+ in engineering time. Net positive.
```

This is the standard ROI argument for continuous profiling, and it holds up in practice.

---

## 14. eBPF Deep Section

Why eBPF profiling is the dominant new architecture.

### 14.1 The kernel primitives

```
1. perf_event_open(PERF_TYPE_SOFTWARE, PERF_COUNT_SW_CPU_CLOCK, freq=99)
   - kernel fires the eBPF program at sample time
2. BPF_PROG_TYPE_PERF_EVENT: the program type that runs in this context
3. BPF_MAP_TYPE_STACK_TRACE: map type for storing stacks (capped at 127 frames)
4. bpf_get_stackid(): captures current stack into the map, returns hash
5. ringbuf or perf buffer: ship samples to userspace
```

The eBPF program is loaded at agent start, attached to a perf_event, and runs in kernel context on every sample tick.

### 14.2 The verifier and its limits

eBPF programs must pass a kernel verifier:

- Linux ≤ 4.x: max 4096 instructions
- Linux ≥ 5.2: 1 million instructions for privileged users
- No unbounded loops (must prove termination)
- Pointer arithmetic must be checked
- Stack size: 512 bytes

These limits are why DWARF unwinding doesn't fit *naturally* in BPF — naive table interpretation is too many instructions. Parca's solution:

```
agent userspace:
  parse .eh_frame for every loaded mapping
  for each PC range, compile to a tiny BPF map entry:
    (pc_low, pc_high, cfa_offset, rbp_offset, ra_offset)
  load this table into BPF map at startup

eBPF program at sample time:
  binary search the table for current PC
  apply offsets directly: ra = *(rbp + ra_offset); rbp = *(rbp + rbp_offset);
  no DWARF interpretation in kernel — just table lookup
```

The result is "DWARF-like" unwinding in kernel without DWARF interpretation. This is the key insight that made the entire eBPF profiler generation possible.

### 14.3 Stack-id collisions

`BPF_MAP_TYPE_STACK_TRACE` keys stacks by hash. Hash collisions → wrong stack for some samples. With a million-entry map, collision rate is ~0.05%. The flag `BPF_F_REUSE_STACKID` keeps the first-seen stack on collision; without it, lookups can fail.

### 14.4 Ringbuf vs perf buffer

```
Ringbuf (BPF_MAP_TYPE_RINGBUF, 5.13+):
  ✓ Single shared per-CPU buffer, lower memory
  ✓ Reservation-then-commit semantics, no copy
  ✓ Wakes userspace less often (epoll-friendly)

Perf buffer (BPF_MAP_TYPE_PERF_EVENT_ARRAY):
  ✓ Older API (4.x+)
  ✗ Per-CPU ringbuffers, more memory
  ✗ Higher overhead per sample
```

Modern profilers use ringbuf where possible.

### 14.5 The "no recompilation" superpower

All language runtimes that emit reasonable frame pointers OR generate `.eh_frame` (which is required for C++ exceptions and increasingly required for Rust/Go) can be profiled without changes. This is unprecedented:

- Java: works (via async-profiler embedded in agent or pure eBPF reading PerfMap files)
- Go: works (frame pointers since 1.7)
- Rust: works (frame pointers default in release; .eh_frame always)
- C++: works if compiled with frame pointers OR .eh_frame
- Python: PyFrame-aware unwinding extension required (Parca, py-spy do this)
- Node: needs --perf-basic-prof for JIT
- .NET: works on Linux (jitdump)

### 14.6 The kernel ≥ 4.9 floor

eBPF was introduced in 3.18 but production-grade profiling needs:

- 4.9+: bpf_get_stackid
- 4.18+: ringbuf-style features
- 5.4+: BTF (BPF Type Format) for CO-RE — critical for portability across kernel versions
- 5.13+: ringbuf

Most cloud Linux (Amazon Linux 2, Ubuntu 20.04+, RHEL 8+) is on 5.4+, comfortably in range.

### 14.7 CO-RE (Compile Once, Run Everywhere)

Pre-CO-RE, every eBPF program had to be rebuilt against the running kernel's headers. CO-RE uses BTF to relocate field offsets at load time:

```c
SEC("perf_event")
int sample_stack(struct bpf_perf_event_data *ctx) {
    struct task_struct *task = (void *)bpf_get_current_task();
    pid_t pid = BPF_CORE_READ(task, pid);   // CO-RE relocatable read
    ...
}
```

The agent ships ONE eBPF binary that runs on any 5.4+ kernel. This is what made eBPF profilers shippable as DaemonSets across heterogeneous fleets.

---

## 15. Fleet Rollout Patterns

### 15.1 Three deployment shapes

```
A) DaemonSet eBPF agent
   ✓ One agent per node, profiles everything
   ✓ Polyglot, no per-language config
   ✓ Best operational model
   ✗ Requires recent kernel + privileged container
   
B) Sidecar profiler
   ✓ Per-pod control, language-specific (e.g., async-profiler sidecar for Java)
   ✓ Works on old kernels
   ✗ Resource overhead per sidecar
   ✗ Doesn't see node-level/system code

C) In-process SDK
   ✓ No cluster-side change needed; app emits to pprof endpoint or ships directly
   ✓ Highest fidelity for the language
   ✗ Per-language code change
   ✗ Doesn't see kernel time
```

The standard production pattern in 2024+: **DaemonSet eBPF agent for everything**, with **in-process SDK for Java fleets that want async-profiler-grade fidelity**.

### 15.2 Discovery and labeling

```
Parca-Agent on a k8s node:
  watches /proc/<pid>/cgroup files
  joins to k8s API for pod labels
  applies labels to each profile:
    - service.name = pod label "app"
    - service.version = pod label "version"
    - k8s.namespace, k8s.pod.uid, k8s.container.name
    - host.name, cloud.region
```

The cardinality of these labels matters; the same chapter 18 rules apply.

### 15.3 Upload schedule and backpressure

Agents push profiles every 30–60 s. The gateway / ingestor enforces:

- per-tenant rate limits (profiles/s, bytes/s)
- bytes-per-profile caps (reject anything >5 MB)
- queue depth limits (drop oldest pending profiles on overflow)

Agent-side: ring buffer of last N profiles in case of network outage; backoff on 429 / 503.

### 15.4 Per-tenant isolation

In multi-tenant setups (Pyroscope, Parca with Polar Signals Cloud), tenants are separated by:

- header (`X-Scope-OrgID`)
- per-tenant chunks/segments in storage
- per-tenant cardinality and rate limits
- per-tenant query budgets

Same pattern as Loki.

---

## 16. Operational Pitfalls

### 16.1 Frame pointers omitted

```
gcc / clang default in some distros: -fomit-frame-pointer
=> %rbp is reused as a general register
=> stack walking via fp is broken
=> profile shows broken or shallow stacks
```

Fix: add `-fno-omit-frame-pointer` to all internal CFLAGS. Or use DWARF unwinding (slower, but reliable).

### 16.2 Debug info purged

The CI registry GCs the image after 7 days; profile ingest tries to symbolize a 30-day-old profile and fails. Frames shown as `0x7fff1c...`.

Fix: separate debuginfo storage with its own retention (longer than profile retention). Push to a debuginfod or a flat S3 store at build time.

### 16.3 PID reuse

A short-lived pod restarts; the new container reuses PID 1234. Profile samples shipped after the restart get tagged with the *new* container's labels even though the stack came from the *old* container.

Fix: agent reads `/proc/<pid>/start_time` and includes it as a label; if the start_time changes mid-profile, drop pending samples for that pid.

### 16.4 Overhead from misconfigured 999 Hz

A new SRE sets `frequency: 999` on a small fleet "for better resolution"; the per-pod overhead jumps from 1% to 8%.

Fix: enforce a max sampling rate at the agent config (e.g., `max_freq: 99`).

### 16.5 Profile-every-1s in CI

A CI workflow uploads a profile every second of every test run; storage exploded. Per-tenant rate limits saved the platform but caused failed test uploads.

Fix: opt-in CI profiling with explicit budgets; tag CI profiles with `env=ci` so they don't bloat the production tier.

### 16.6 Symbol cache balloon

A fleet with 50,000 distinct build-ids (frequent builds × many services) blows the symbol cache memory budget. Every profile is a cache miss.

Fix: LRU eviction tied to memory pressure; per-build_id storage on S3 with download-on-demand.

### 16.7 JIT addresses moving between samples

JVM JIT recompiles hot methods over time; the same method has different addresses at minute 1 vs minute 60. Without a per-sample perf-map snapshot, addresses resolve to the wrong method.

Fix: use async-profiler's AsyncGetCallTrace path, which resolves to jmethodID rather than raw address; or take perf-map snapshots at sample time.

### 16.8 Inlined functions

Compiler inlines small helpers; the address falls inside the caller's range, so a naive symbolizer reports the caller. The flame graph misses the inlined hot spot.

Fix: respect DWARF inline information (`DW_TAG_inlined_subroutine`); pprof's `Line` array supports multiple entries for inlined frames.

### 16.9 CGO and other-language sandwiches

A Go program calls a C library that calls back into Go. The stack mixes goroutine and C frames; the unwinder may stop at the Go-C boundary if metadata is missing.

Fix: Go 1.20+ improved cgo unwinding; add `-finstrument-functions` to the C side or accept partial visibility.

---

## 17. Decision Tree

```
What's your primary need?
│
├── "OSS, self-host, polyglot, Kubernetes"
│   → Parca (OSS) or Grafana Pyroscope (OSS).
│   Parca wins for the cleanest data model + columnar (FrostDB).
│   Pyroscope wins if you're already in Grafana stack.
│
├── "Vendor-managed, multi-cloud, with great UX"
│   → Polar Signals Cloud (Parca-based) or Datadog Continuous Profiler.
│   Datadog if you're already a DD shop. Polar Signals for OSS-compatible UX.
│
├── "Java-first fleet"
│   → async-profiler in-process + any backend (or JFR if older JDKs).
│   Datadog and New Relic both have great Java profilers.
│
├── "Lightweight, just-want-flame-graphs"
│   → perf record + flamegraph.pl (Brendan Gregg).
│   No backend, no agent, just one-shot dives.
│
├── "CI integration: detect regressions per PR"
│   → Polar Signals Cloud (best DX) or Datadog (good).
│   Roll your own with Parca + scripted diff checks.
│
└── "I want to know where my service is BLOCKED, not just CPU"
    → eBPF off-CPU profiler in Parca or Pyroscope.
    The single highest-leverage view for I/O-bound services.
```

---

## 18. End-to-End: Life of One CPU Sample

A worked example, in the style of [ROADMAP §8](./ROADMAP.md#8-end-to-end-trace-of-one-request).

```
T+0       Service `pricing` (Go binary, build_id=7e5b...) is running on
          host ip-10-0-1-29 in pod pricing-7c5b-9f2.
          Currently inside vendor.foo.HMACSign — a hot function this morning.

T+0       perf_event timer fires (99 Hz) on CPU 3.
          The kernel's perf subsystem prepares the event sample.

T+0+5µs   The eBPF profiler program (loaded by parca-agent at boot) is invoked
          in kernel context. It executes:

            user_stack_id = bpf_get_stackid(ctx, &user_stacks,
                                             BPF_F_USER_STACK | BPF_F_REUSE_STACKID)
            kernel_stack_id = bpf_get_stackid(ctx, &kernel_stacks,
                                               BPF_F_REUSE_STACKID)

          User stack walks the goroutine via frame pointers (Go 1.21):
            [0]: vendor/foo.HMACSign+0x42
            [1]: pricing.(*Client).computePrice+0x1bc
            [2]: pricing/handler.checkout+0x4f
            [3]: net/http.(*ServeMux).ServeHTTP
            [4]: net/http.serverHandler.ServeHTTP+0x32
            ...
            [12]: runtime.goexit+0x1

          Kernel stack: 0 frames (we were in user mode at sample time).

          The eBPF program increments samples[user_stack_id, pid] += 1
          and emits to ringbuf.

T+0+30µs  parca-agent (running in DaemonSet on the node) reads the ringbuf
          batch. It joins:
            - pid → /proc/<pid>/cgroup → k8s namespace + pod_uid
            - pod metadata (cached) → service.name=pricing,
                                       service.version=v2.6.13
            - build_id from /proc/<pid>/maps → 7e5b...
          Stores in the agent's local in-memory aggregation:
            [(stack=user_stack_id, pid, build_id=7e5b...)] += 1

T+60s     Agent flushes 60 s of aggregated samples.
          Constructs a pprof Profile:
            - sample_type: [{type=cpu, unit=nanoseconds},
                            {type=samples, unit=count}]
            - samples: ~140 unique stacks (deduplicated)
            - mappings: pricing binary @ 0x40000 with build_id=7e5b...
                        glibc, libpthread, kernel mappings
            - locations: addresses, NOT YET symbolized
            - functions: empty (no Go gopclntab parsing in agent)
            - string_table: filenames, etc.
          Compresses with zstd, ~50 KB.

T+60s+10ms POST to parca-gateway over HTTPS (mTLS):
            X-Scope-OrgID: tenant-eng
            Content-Encoding: zstd
            Content-Type: application/x-protobuf

T+60.05s  parca-gateway:
            - rate-limit check passes
            - decompresses; validates pprof structure
            - enqueues to ingest worker

T+60.1s   Ingest worker:
            for each Mapping with unresolved addresses:
              build_id = 7e5b...
              cache_lookup(build_id) → MISS
              query internal debuginfod: GET /buildid/7e5b.../debuginfo
                → fetch 4 MB DWARF bundle
              extract DWARF symbol table; cache it (LRU, 1 GB cap)
            for each Location (raw address):
              symbolize via DWARF table:
                0x40010c → vendor/foo.HMACSign at hmac.go:147
                0x4012ab → pricing.(*Client).computePrice at client.go:93
                ...
            populate Function and Line entries

T+60.5s   Resolved profile written to FrostDB:
            - new functions inserted (3 new function_ids this minute)
            - new frames inserted (5 new frame_ids)
            - new stacks inserted (12 new stack_ids; rest deduplicated)
            - 140 sample rows inserted into samples table with labels
              {service=pricing, version=v2.6.13, pod=pricing-7c5b-9f2,
               profile_type=cpu_ns}

T+5min    A senior engineer pushed v2.6.14 30 minutes ago.
          They open the Polar Signals UI:
            - select service=pricing
            - compare v2.6.13 (last hour) vs v2.6.14 (last hour)
            - render diff flame graph

T+5min+1s Polar Signals query:
            SELECT stack_id, sum(value)
            FROM samples
            WHERE service='pricing' AND version IN ('v2.6.13', 'v2.6.14')
              AND profile_type='cpu_ns'
              AND timestamp >= now() - INTERVAL 1 HOUR
            GROUP BY stack_id, version

          FrostDB scans columnar parquet files; ~50 ms.
          Result: per-stack samples for both versions.

T+5min+2s Server reconstructs call trees, computes diff:
            stack [vendor/foo.HMACSign] → v2.6.14: +35% time
            stack [crypto/sha256] → v2.6.14: -20% time

          UI renders icicle flame graph; HMACSign function bar is RED;
          tooltip says "+35% in v2.6.14".

T+5min+3s Engineer clicks the bar → drill-down opens source view.
          Polar Signals fetches source via debuginfod's /source endpoint:
            hmac.go:147 — `for i := 0; i < len(data); i++ { ... }`
          Engineer sees they accidentally did per-byte iteration instead
          of `copy(dst, src)`. Files PR.

T+1h      v2.6.15 with the fix is rolled out. Ten minutes later, the diff
          shows HMACSign at v2.6.13 baseline. Regression closed.

T+14d     The profile ages out of hot tier; FrostDB compacts and moves
          parquet files to S3. Queries against this profile now cost ~300 ms
          (S3 GET) instead of ~50 ms. Symbol info is still cached.

T+90d    Profile retention deletes the parquet file. The build_id symbol
          cache is the longest-lived artifact — still resident for the
          rare case someone replays old data.

The same single CPU sample participated in:
  - 1 eBPF perf_event tick
  - 1 kernel-side stack walk via frame pointers
  - 1 userspace agent aggregation
  - 1 zstd-compressed pprof shipment over the network
  - 1 debuginfod fetch + DWARF symbolization
  - 1 columnar FrostDB row insertion (after frame/function dedup)
  - 1 differential flame graph render that surfaced a CI regression
  - 1 PR + revert cycle that closed the loop

That is the whole life cycle. The total cost: ~5 microseconds at the source,
~100 KB of network traffic per minute, ~3 KB of storage after dedup. The
total value: an engineering hour saved, a regression caught at hour 1
instead of week 1.
```

---

**TL;DR.** A profile is a weighted set of stack traces. The pprof protobuf is the universal format. Stack walking is the hard part: frame pointers are fastest, DWARF is most reliable, and eBPF agents now do both at the kernel level for any language. Symbolization is the operational nightmare: solve it by pushing every CI artifact's debug info to a debuginfod-compatible store. Storage is dominated by **stack-and-frame deduplication**: a 600 KB raw pprof becomes ~3 KB on disk in a columnar engine like FrostDB. Continuous profiling — sampling at 19–99 Hz, keeping for weeks — costs ~1% CPU and unlocks two flagship workflows: **differential flame graphs** ("did this PR regress?") and **off-CPU analysis** ("where was my service blocked?"). Pick Parca or Grafana Pyroscope for OSS; Polar Signals Cloud or Datadog for managed; async-profiler for Java fidelity. The single highest-leverage rollout is "DaemonSet eBPF agent + every CI build pushes its build-id'd debuginfo." Get those right and profiling becomes the cheapest signal in the observability tetrahedron.
