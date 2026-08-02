# 11 — IPC and shared memory: what a message between address spaces costs

> **Tier 1, doc 11.** Prerequisites: [`07-virtual-memory.md`](07-virtual-memory.md)
> (`mmap`, page tables, copy-on-write, RSS accounting),
> [`09-syscalls-and-io.md`](09-syscalls-and-io.md) (the trap floor, readiness
> notification, buffering), [`10-signals-fork-exec.md`](10-signals-fork-exec.md)
> (fd inheritance, `PEP 446`, what a forked child gets). Reads well next to
> [`02-atomics-and-memory-models.md`](02-atomics-and-memory-models.md) — §12 of this
> document is that document's problem, moved across a process boundary and made worse.
> Feeds into: [`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md),
> [`28-asyncio-internals.md`](28-asyncio-internals.md) (the self-pipe),
> [`46-production-python.md`](46-production-python.md) (worker models).
>
> **THESIS: there are exactly two kinds of IPC, and every API in this document is one
> of them wearing a costume.** Either you **copy the bytes through the kernel** — a
> syscall per message, a serialization pass, and total safety, because the two
> processes never touch the same memory — or you **share a mapping** and get the bytes
> for free, at which point you have personally taken ownership of the entire memory-model
> problem from doc 02, in a language that does not have a memory model. `multiprocessing`
> is a library whose main achievement is hiding which of the two you picked.
> `Queue` is the first. `SharedMemory` is the second. They are not two points on a
> performance curve; they are two different contracts, and the second one has no
> guardrails.
>
> A secondary claim, which is really the same claim: **the shared-memory family is not
> actually about memory. It is about the synchronization protocol you now have to
> write.** A shared mapping is a `mmap` call. What is hard is the futex underneath the
> mutex on top of it, the fact that a crashed peer leaves that mutex locked forever, and
> the fact that Python's `struct`-packed bytes have no acquire/release semantics
> whatsoever. §9–§12 are about that, and they are the reason this document is not short.

> **Measurement provenance.** Facts labelled *(measured)* were produced on the machine
> this repo lives on: **Apple M3 Pro, macOS 25.5 (Darwin 25.5.0), arm64, 128-byte cache
> lines, 16 KB pages, 11 cores (5 P + 6 E)**, using **CPython 3.14.6**
> (`~/.local/bin/python3.14`). Python stdlib source is quoted from that installation's
> `Lib/multiprocessing/` with line numbers; C source is quoted from the **`3.14` branch
> of github.com/python/cpython** as of Aug 2026 — downloaded and read, not recalled.
>
> **This is a macOS box, and in this document that matters more than in any other except
> doc 10.** The entire Linux IPC toolkit that people reach for — `futex(2)`,
> `eventfd(2)`, `memfd_create(2)`, `/dev/shm`, the abstract socket namespace,
> `SOCK_SEQPACKET` on `AF_UNIX` — **does not exist here**, and I verified each absence
> rather than assuming it (§3.3, §7.5, §10, §11). Two constants that everyone quotes
> from the Linux man page are *different numbers here* and the difference is
> load-bearing: `PIPE_BUF` is **512, not 4096** (§2.3), and the page granularity that
> rounds your shared-memory segment is **16384, not 4096** (§7.4). Linux-only facts are
> cited to `man7.org` and flagged **not measured here** in place.
>
> **On measurement discipline:** this document deliberately does *not* contain a
> grand IPC-throughput table. Round-trip latency for pipes vs sockets vs shared memory
> is dominated by the syscall trap floor and the scheduler wakeup, both of which
> [`09-syscalls-and-io.md`](09-syscalls-and-io.md) §2 already measured on this machine
> (**~81 ns trap floor**). Re-measuring it here would produce a prettier table and no new
> knowledge. What I measured instead are the things where the *number itself is the
> finding*: the capacity limits, the page rounding, the `PIPE_BUF` discrepancy, and the
> `Queue.empty()` race in §6.5.

## Contents

1. [Two families, and why there are only two](#1-two-families-and-why-there-are-only-two)
2. [Pipes](#2-pipes)
3. [UNIX domain sockets](#3-unix-domain-sockets)
4. [Passing file descriptors: `SCM_RIGHTS`](#4-passing-file-descriptors-scm_rights)
5. [The message-oriented IPC you should not use](#5-the-message-oriented-ipc-you-should-not-use)
6. [What `multiprocessing` is actually built out of](#6-what-multiprocessing-is-actually-built-out-of)
7. [Shared memory: the mapping](#7-shared-memory-the-mapping)
8. [Shared memory from Python](#8-shared-memory-from-python)
9. [Why you cannot put a Python object in shared memory](#9-why-you-cannot-put-a-python-object-in-shared-memory)
10. [Synchronization: the futex and its absence](#10-synchronization-the-futex-and-its-absence)
11. [`multiprocessing.Lock` is a named POSIX semaphore](#11-multiprocessinglock-is-a-named-posix-semaphore)
12. [Waking someone up: `eventfd` and the self-pipe](#12-waking-someone-up-eventfd-and-the-self-pipe)
13. [The memory-model problem, in Python](#13-the-memory-model-problem-in-python)
14. [The cost model, and a decision table](#14-the-cost-model-and-a-decision-table)
15. [House rules](#15-house-rules)
16. [What I could not verify](#16-what-i-could-not-verify)
17. [You can answer this](#17-you-can-answer-this)
18. [Sources](#18-sources)

---

## 1. Two families, and why there are only two

Two processes have separate page tables. That is the whole premise. A pointer in one is
meaningless in the other, and the hardware enforces it. So to get information from A to
B there are only two physically available moves:

**Family 1 — copy through the kernel.** A calls `write()`; the kernel copies the bytes
from A's pages into kernel-owned pages; B calls `read()`; the kernel copies them out into
B's pages. Pipes, UNIX sockets, TCP loopback, System V message queues, POSIX message
queues, `multiprocessing.Queue`, `multiprocessing.Pipe`, and every "message passing"
library are this. The bytes are copied twice, at least one syscall is paid per message
in each direction, and — crucially for Python — the *object* must be flattened into
bytes and rebuilt, which is usually more expensive than either copy.

**Family 2 — map the same physical pages into both page tables.** `mmap(MAP_SHARED)` on
a shared object. After that, a store by A is visible to B with no syscall, no copy, and
no notification. POSIX shared memory, System V shared memory, `mmap` of a file,
`multiprocessing.shared_memory`, `multiprocessing.Value`/`Array`, and every zero-copy
data-frame-between-workers scheme is this.

The whole engineering content of this document is in the last five words of that
paragraph: **and no notification**. Family 1 gives you synchronization for free — a
`read()` that returns means the data is there, and the kernel's internal locking made it
so. Family 2 gives you nothing. You get a byte array shared between two schedulable
entities on a weakly-ordered CPU, and you must supply the mutual exclusion, the ordering
guarantees, the liveness, and the crash recovery yourself.

| | Family 1: copy | Family 2: share |
|---|---|---|
| Per-message cost | ≥1 syscall each way, 2 copies, + serialization | 0 syscalls, 0 copies |
| Cost scales with | message size | nothing (after setup) |
| Synchronization | **free, from the kernel** | **yours** |
| Ordering / visibility | guaranteed by the kernel | **your problem** (doc 02) |
| Peer crashes mid-operation | you get `EOF`/`EPIPE` | **lock stays held forever** |
| Type safety in Python | pickle round-trips real objects | `struct`-packed bytes, or ctypes |
| Debuggability | `strace`, `lsof`, the fd shows up | invisible; it's just memory |

There is a third thing people call IPC that is really neither: **passing a file
descriptor** (§4). It moves a *capability*, not data, and it is the mechanism that lets
you build family-2 sharing without a filesystem name.

**The honest ordering rule.** Start in family 1. Move to family 2 only when you have
measured that serialization or copying is your bottleneck, and only for *bulk numeric or
byte data* — never for control flow, and never for Python objects. §14 makes this
concrete.

---

## 2. Pipes

### 2.1 What a pipe actually is

`pipe(2)` creates a kernel-resident ring buffer and returns two file descriptors onto
it: one read end, one write end. There is no filesystem entry, no name, and no way for
an unrelated process to find it. The only way a second process gets access is by
**inheriting the fd across `fork()`** (doc 10 §12) or by being **sent the fd over a UNIX
socket** (§4).

That last sentence is the reason `multiprocessing`'s `spawn` start method needs the
elaborate machinery of §6: with `fork` the child simply has the fd; with `spawn` the
child is a fresh `exec`'d interpreter and the fd has to be handed to it explicitly.

A pipe is **unidirectional and byte-oriented**. It has no message boundaries. If you
write `b'AB'` then `b'CD'`, a reader asking for 3 bytes gets `b'ABC'`. Every
message-oriented protocol on top of a pipe therefore has to frame — which is exactly
what `multiprocessing.Connection` does (§6.2).

### 2.2 Capacity, and what "blocking" means

The ring buffer is finite. On Linux it is 16 pages — 65,536 bytes with 4 KB pages —
adjustable per-pipe with `fcntl(F_SETPIPE_SZ)` and bounded by
`/proc/sys/fs/pipe-max-size` ([`pipe(7)`](https://man7.org/linux/man-pages/man7/pipe.7.html)).

On this machine:

```
pipe capacity (4096-byte writes): 65536 bytes
pipe capacity (1-byte writes):    65536 bytes
```
*(measured)* — the same 64 KiB, and notably **it does not shrink when you write one byte
at a time**, which rules out a per-write metadata overhead. macOS has no
`F_SETPIPE_SZ`; the capacity is what the kernel gives you. (macOS does grow a pipe's
buffer dynamically from 16 KB in some kernels; what is observable here is that a single
producer with no reader stalls at 65,536 bytes.)

Four behaviours follow from "finite buffer", and they are the four production incidents:

- **Writer blocks when full.** `write()` on a full pipe sleeps until a reader drains it.
  This is backpressure, and it is a *feature* — until the reader is your own process's
  other end, in which case it is a deadlock.
- **Reader blocks when empty.** `read()` sleeps until a byte arrives.
- **Reader gets EOF when all write ends are closed.** *All* of them. The single most
  common pipe bug in a forked program is the parent forgetting to close its copy of the
  write end, so the child's `read()` never returns EOF and the program hangs forever.
  This is why every `fork`-and-pipe example closes four fds.
- **Writer gets `SIGPIPE`/`EPIPE` when all read ends are closed** (§2.4).

**The classic self-deadlock.** A parent writes a 1 MB payload to a child through one
pipe and reads the child's response through another. The child reads the whole request
before writing the response. The parent fills the 64 KiB request pipe at byte 65,537 and
sleeps; the child is not going to finish reading, because it is also blocked... no, worse
— the child *is* reading, so this one works. Now reverse it: the parent writes 1 MB, and
the child writes its (large) response as it goes. The child's response pipe fills; the
child blocks in `write()`; it stops reading; the parent's request pipe fills; the parent
blocks in `write()`. Neither will ever move. **This is why `subprocess.Popen.communicate()`
exists and why the documentation tells you not to hand-roll `p.stdin.write()` followed by
`p.stdout.read()`** — `communicate()` uses `selectors` to service both directions
concurrently. The deadlock is not a Python problem; it is arithmetic on a 64 KiB buffer.

### 2.3 `PIPE_BUF`: the atomicity guarantee, and the number that is different here

POSIX guarantees that a `write()` of **at most `PIPE_BUF` bytes to a pipe is atomic** —
it lands as one contiguous run, never interleaved with another writer's bytes. Writes
larger than `PIPE_BUF` may be torn and interleaved
([`pipe(7)`](https://man7.org/linux/man-pages/man7/pipe.7.html)).

This is the one real concurrency primitive a plain pipe gives you, and it is the entire
basis of the "many writers, one reader" logging pattern: N processes each `write()` a
sub-`PIPE_BUF` line to the same pipe, one process reads, and the lines never interleave —
with no lock anywhere.

POSIX requires `PIPE_BUF ≥ 512`. The Linux man page says Linux's value is **4096**, and
that number is what almost everyone has memorised. On this machine:

```
select.PIPE_BUF                        : 512
os.fpathconf(pipe_fd, 'PC_PIPE_BUF')   : 512
```
*(measured)* — **macOS gives you the POSIX floor, 512, an eighth of Linux's.** A
log-line protocol tested on Linux at 1 KB per record is atomic there and torn here.

CPython itself is careful about this, and the care is visible in the source. The
`multiprocessing` resource tracker (§8.3) sends its commands over a pipe from many
processes to one reader, and it will not let a message exceed 512 bytes:

```python
# Lib/multiprocessing/resource_tracker.py, CPython 3.14.6, L345-373
def _send(self, cmd, name, rtype):
    if self._use_simple_format and '\n' not in name:
        msg = f"{cmd}:{name}:{rtype}\n".encode("ascii")
        if len(msg) > 512:
            # posix guarantees that writes to a pipe of less than PIPE_BUF
            # bytes are atomic, and that PIPE_BUF >= 512
            raise ValueError('msg too long')
        ...
    # POSIX guarantees that writes to a pipe of less than PIPE_BUF (512 on Linux)
    # bytes are atomic. ...
    # As we want the overall message to be kept atomic and therefore smaller than 512,
    # we encode the raw name bytes with URL-safe Base64 - so a 255 long name
    # will not exceed 340 bytes.
```

Note that the code is right and the *comment* is wrong: it says "512 on Linux" when 512
is the POSIX minimum and Linux's actual value is 4096. CPython chose the portable floor,
which is the correct engineering decision and makes the tracker's protocol atomic on this
machine as well as on Linux. The base64 escape hatch in the same function exists because
`json.dumps(..., ensure_ascii=True)` expands a non-ASCII byte to a six-character `\uDC80`
escape, and a 255-byte name would blow past 512; base64 caps a 255-byte name at 340.

**The rule to carry:** if a design depends on write atomicity to a pipe, the budget is
**512 bytes, not 4096**, unless you have verified `fpathconf` on the target.

### 2.4 `SIGPIPE`, `EPIPE`, and why Python programs see the second one

Writing to a pipe whose read ends are all closed raises `SIGPIPE`. The default
disposition of `SIGPIPE` is to **kill the process** — a shell-pipeline convenience
(`yes | head -1` must terminate `yes`) that is a hazard in a server.

CPython sets `SIGPIPE` to `SIG_IGN` at startup, so the `write()` returns `-1/EPIPE`
instead and Python raises `BrokenPipeError`. That is why you handle
`BrokenPipeError` in Python and not a signal — and why a C extension that forks and
`exec`s must remember that the child inherits the *ignored* disposition across `exec`
(doc 10 §11.1: ignored dispositions survive `exec`, handled ones are reset to default).
A shell `exec`'d from Python that pipes into `head` will therefore not die when it
should. This asymmetry is the second most common `exec` bug after fd inheritance.

`multiprocessing.Queue`'s feeder thread has an explicit switch for this:

```python
# Lib/multiprocessing/queues.py L272-274
except Exception as e:
    if ignore_epipe and getattr(e, 'errno', 0) == errno.EPIPE:
        return
```

`concurrent.futures.ProcessPoolExecutor` sets `_ignore_epipe = True` so that a worker
dying does not produce a spurious traceback from the feeder thread.

### 2.5 FIFOs: a pipe with a name

`mkfifo(3)` (`os.mkfifo`) puts a pipe in the filesystem namespace so unrelated processes
can open it by path. The kernel object is the same ring buffer; only the rendezvous
changes. Two properties surprise people:

- **`open()` blocks until the other end appears.** Opening a FIFO for reading blocks
  until a writer opens it, and vice versa, unless you pass `O_NONBLOCK`. This is a
  rendezvous, not a mailbox.
- **It is still a single stream, not a queue of clients.** If two writers and two readers
  attach, bytes go to whichever reader is scheduled. There is no per-client separation.
  The moment you want per-client separation you want a UNIX socket, which is §3.

FIFOs are a reasonable choice for a fixed, small number of long-lived cooperating
processes with a filesystem to rendezvous on, and a bad choice for anything
connection-shaped.

---

## 3. UNIX domain sockets

### 3.1 The three types, and which ones exist here

`AF_UNIX` sockets are the general-purpose local IPC transport. They come in three types
([`unix(7)`](https://man7.org/linux/man-pages/man7/unix.7.html)):

| Type | Boundaries | Connection | Ordering |
|---|---|---|---|
| `SOCK_STREAM` | none (byte stream) | yes | yes |
| `SOCK_DGRAM` | **preserved** | no | yes — and, unusually, **reliable** |
| `SOCK_SEQPACKET` | **preserved** | yes | yes |

The `SOCK_DGRAM` row is worth pausing on. Over IP, "datagram" means *may be dropped,
duplicated, or reordered*. Over `AF_UNIX` it means none of those — the man page is
explicit that "UNIX domain datagram sockets are always reliable and don't reorder
datagrams". A local datagram socket is a **reliable message queue with a fixed maximum
message size**, and that is a genuinely useful primitive that people avoid because the
word "datagram" scared them.

`SOCK_SEQPACKET` is the one you actually want for RPC — connection-oriented *and*
message-preserving, so no framing layer. It is also the one that is least portable:

```
AF_UNIX SOCK_STREAM     OK
AF_UNIX SOCK_DGRAM      OK
AF_UNIX SOCK_SEQPACKET  EPROTONOSUPPORT: Protocol not supported
```
*(measured)* — the constant `socket.SOCK_SEQPACKET` **exists** in Python on this machine,
so a `hasattr` feature check passes, and then `socketpair()` fails at runtime. Linux has
supported it since 2.6.4. **Feature-detect by attempting the call, not by checking for
the constant** — this is a general lesson and this is a clean example of it.

### 3.2 What a UNIX socket gives you over a pipe

Everything in this list is a reason `multiprocessing` uses a socketpair rather than two
pipes whenever it can:

- **Bidirectional over one object.** One socketpair replaces two pipes and halves the
  fd-closing ceremony that causes the EOF bug in §2.2.
- **File-descriptor passing** via `SCM_RIGHTS` (§4). Pipes cannot do this. This is the
  big one.
- **Credential passing** — `SO_PASSCRED`/`SCM_CREDENTIALS` on Linux, `LOCAL_PEERCRED` on
  macOS/BSD — letting a server learn the *kernel-attested* uid/gid/pid of its client.
  This is how a privileged daemon authenticates a local client without a password, and it
  is why `AF_UNIX` is the correct transport for a control socket and TCP-on-loopback is
  not.
- **Filesystem permissions as access control.** A socket at a path obeys directory and
  file permissions. A loopback TCP port is reachable by every user on the box.
- **A listening socket with an accept queue**, so a server can serve many clients with
  proper per-client separation.
- **Message boundaries** if you use `SOCK_DGRAM` or `SOCK_SEQPACKET`.

Against loopback TCP specifically, `AF_UNIX` also skips the entire IP/TCP stack —
no checksums, no Nagle, no congestion control, no port-exhaustion or `TIME_WAIT`
accumulation. If two processes are on the same host and you are using
`127.0.0.1:5432`, you are paying for a network you do not have.

### 3.3 Addressing: pathname, unnamed, and the abstract namespace that isn't here

Three ways to name an `AF_UNIX` socket:

**Pathname.** `bind('/tmp/x.sock')` creates a socket file. It **persists after the
process exits** and a subsequent `bind()` on the same path fails with `EADDRINUSE`,
which is why every UNIX-socket server has an `os.unlink()` dance at startup. The path
goes into `sockaddr_un.sun_path`, a fixed char array:

```
path len  90: OK
path len 100: OK
path len 104: rejected ("AF_UNIX path too long")
```
*(measured)* — `sun_path` is **104 bytes on macOS** and 108 on Linux. Note *who* rejected
it: CPython's `socketmodule` raises `OSError("AF_UNIX path too long")` with **no
`errno`** before ever making the syscall. Code that branches on `e.errno` will see
`None`. This bites when someone puts sockets under a long temp path — and
`multiprocessing`'s own temp dir here is
`/var/folders/_b/88w86gts4yvcjb_d8qqdxvtc0000gn/T/pymp-hgwvdgyq` *(measured)*, already 61
characters before a filename.

**Unnamed** — `socket.socketpair()`. No name, no filesystem, inherited across `fork`,
passable over another socket. This is what `multiprocessing.Pipe(duplex=True)` uses.

**Abstract namespace** — Linux-only. A `sun_path` starting with a NUL byte names a socket
in a namespace with no filesystem entry, which disappears automatically when the last
reference closes. It solves the stale-socket-file problem completely. It does not exist
here:

```python
s.bind("\0abstract-test")
# -> ENOENT: No such file or directory
```
*(measured)* — macOS treats the leading NUL as a zero-length path. Portable code cannot
use it. **Not measured here:** on Linux this succeeds and the socket is invisible to
`ls`. Note also that the abstract namespace has **no permission bits** — anyone in the
network namespace can connect — which is a real security difference, not just a
convenience one.

### 3.4 The buffer accounting is different, and smaller

```
socketpair SO_SNDBUF: 8192   SO_RCVBUF: 8192
in-flight capacity before EAGAIN: 8192 bytes
```
*(measured)* — a default socketpair here holds **8 KiB in flight, versus a pipe's 64 KiB**.
That is an 8× difference in how much a producer can run ahead of a consumer before it
blocks, and it is invisible unless you look. If you swap a pipe for a socketpair to get
fd-passing and your throughput drops, this is why; `setsockopt(SO_SNDBUF, ...)` is the fix.

On Linux the accounting is asymmetric in a way that catches people:
`SO_SNDBUF` has an effect on `AF_UNIX` but **`SO_RCVBUF` does not**, and for datagram
sockets `SO_SNDBUF` sets the maximum datagram size (doubled, less 32 bytes of overhead)
([`unix(7)`](https://man7.org/linux/man-pages/man7/unix.7.html)). **Not measured here.**

---

## 4. Passing file descriptors: `SCM_RIGHTS`

### 4.1 What actually crosses

This is the most under-appreciated primitive in UNIX, and the one that makes several
otherwise-impossible architectures possible.

`sendmsg(2)` on an `AF_UNIX` socket can carry **ancillary data** — a control message
alongside the payload. With `cmsg_level = SOL_SOCKET` and `cmsg_type = SCM_RIGHTS`, the
payload of the control message is an array of file descriptors
([`cmsg(3)`](https://man7.org/linux/man-pages/man3/cmsg.3.html)).

**The integer does not cross. The kernel duplicates the open file description into the
receiver's descriptor table and gives it whatever number is free there.** Measured:

```
sent fd 15  ->  received fd 17
os.write(w, b'hello')  ->  os.read(received_fd, 5) == b'hello'
```
*(measured)* — two different numbers, one shared open file description. Shared file
offset, shared status flags, and the underlying object (pipe, socket, file, shared-memory
object) stays alive as long as *either* process holds a reference. It is exactly
`dup()`, across a process boundary.

What this buys you:

- **A privileged opener.** A tiny root process opens a port below 1024 or a restricted
  file and hands the fd to an unprivileged worker. The worker never had the privilege.
- **Zero-downtime restart.** The old process passes its listening socket to the new
  binary; the accept queue is never dropped and no connection is refused. This is how
  `systemd` socket activation, nginx binary upgrade, and every graceful-restart scheme
  works.
- **Shared memory without a name.** Pass the fd from `shm_open`/`memfd_create` and
  immediately `shm_unlink` it. The segment has no filesystem name for anyone to find or
  collide with, and it is destroyed automatically when the last holder closes. This is
  the *correct* way to do POSIX shared memory, and §8.3's entire resource-tracker mess
  exists because `multiprocessing.SharedMemory` does not do it.
- **Handing off a connection** from an acceptor to a worker process — the pre-fork server
  model with a real dispatcher rather than a thundering herd.

### 4.2 The Python API

Since 3.9 the stdlib has the two-liner:

```python
socket.send_fds(sock, buffers, fds)              # -> bytes sent
socket.recv_fds(sock, bufsize, maxfds)           # -> (msg, [fds], flags, addr)
```

Both require `AF_UNIX`, `sendmsg`/`recvmsg`, and `SCM_RIGHTS`. On this machine all three
are present *(measured)*. Before 3.9 you wrote the `sendmsg`/`CMSG_SPACE` incantation by
hand, which is why so much code still carries a vendored copy of it.

### 4.3 How `multiprocessing` uses it

`multiprocessing` feature-detects at import:

```python
# Lib/multiprocessing/reduction.py L24-27
HAVE_SEND_HANDLE = (sys.platform == 'win32' or
                    (hasattr(socket, 'CMSG_LEN') and
                     hasattr(socket, 'SCM_RIGHTS') and
                     hasattr(socket.socket, 'sendmsg')))
```

and implements the primitive itself rather than using `socket.send_fds`:

```python
# Lib/multiprocessing/reduction.py L142-149
def sendfds(sock, fds):
    '''Send an array of fds over an AF_UNIX socket.'''
    fds = array.array('i', fds)
    msg = bytes([len(fds) % 256])
    sock.sendmsg([msg], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])
    if sock.recv(1) != b'A':
        raise RuntimeError('did not receive acknowledgement of fd')
```

Three details in six lines, each of which is a scar:

1. **A one-byte payload is sent alongside the fds.** You cannot send ancillary data with
   an empty payload portably — some implementations discard the control message. The
   byte also encodes the fd count so the receiver can cross-check.
2. **The receiver sends back an ack byte `b'A'`.** The comment says why:
   *"We send/recv an Ack byte after the fds to work around an old macOS bug; it isn't
   clear if this is still required but it makes unit testing fd sending easier"*
   ([gh-58874](https://github.com/python/cpython/issues/58874)). It is also a
   synchronization point that prevents the sender from closing the fd before the kernel
   has installed it.
3. **`len(a) % 256 != msg[0]` is checked on receive** and raises `AssertionError`,
   because a truncated `recvmsg` silently loses trailing fds. Which leads to §4.4.

`DupFd` (L191-200) picks the strategy per start method: `fork` needs nothing (the child
inherits), `spawn`/`forkserver` route through `resource_sharer`, which runs a background
thread serving fds over a UNIX socket on demand. **The `resource_sharer` is a real,
undocumented thread in your process** — one more reason `spawn` is not merely "`fork` but
slower".

### 4.4 The four hazards

- **Truncation is silent.** If the receiver's control buffer is too small, `recvmsg`
  sets `MSG_CTRUNC` in the returned flags and **the excess descriptors are closed by the
  kernel** — you lose them with no error. Always size with `CMSG_SPACE(n * 4)` and
  always check `flags & MSG_CTRUNC`. The Python docs note this explicitly:
  "any truncated integers at the end of the ancillary data" are discarded.
- **In-flight fds hold their objects open.** A descriptor sitting in a socket buffer,
  not yet received, still holds a reference to its open file description. Send fds into a
  socket nobody reads and you have a **descriptor leak with no visible holder** — `lsof`
  on either process shows nothing useful. Linux caps this (`RLIMIT_NOFILE`-adjacent, plus
  a garbage collector for the cyclic case), which brings us to:
- **`AF_UNIX` has its own garbage collector, and it has had bugs.** You can send a UNIX
  socket's own fd through itself, creating a cycle of kernel objects that ordinary
  refcounting will never free. Linux carries a dedicated collector for this
  (`net/unix/garbage.c`); it has been the source of several CVEs. Don't build cycles.
- **The receiver must close what it receives.** Received fds are ordinary descriptors
  with no owner. `recv_fds` hands you raw integers, not objects — nothing will close them
  for you.

---

## 5. The message-oriented IPC you should not use

Two API families exist for local message passing that are not pipes or sockets. Both are
worth being able to identify and neither is worth choosing today.

**System V IPC** — `msgget`/`msgsnd`/`msgrcv` (message queues), `semget`/`semop`
(semaphore sets), `shmget`/`shmat` (shared memory)
([`sysvipc(7)`](https://man7.org/linux/man-pages/man7/sysvipc.7.html)). Identified by
integer keys from `ftok()`, not file descriptors. That single design choice is
disqualifying:

- **They are not file descriptors**, so you cannot `select`/`poll`/`epoll` them, cannot
  pass them over a socket, and cannot use them in an event loop. In a Python program,
  that means you cannot integrate them with `asyncio` at all.
- **They have kernel persistence and no owner.** A crashed process leaves its segments
  and semaphores in the kernel until reboot or manual `ipcrm`. Every long-running System
  V shop has an `ipcs | awk | ipcrm` cron job. This is not a joke; it is a genre.
- **`ftok()` collides.** It hashes an inode number and a project byte. Different files on
  different filesystems collide; the same file re-created gets a different key.

The man page's own verdict on the shared-memory variant is blunt: POSIX shared memory
"provides a simpler, and better designed interface"
([`shm_overview(7)`](https://man7.org/linux/man-pages/man7/shm_overview.7.html)). Python
has no System V IPC in the stdlib at all, which is a deliberate and correct omission.

**POSIX message queues** — `mq_open`/`mq_send`/`mq_receive`, names like `/myqueue`,
kernel persistence until `mq_unlink`
([`mq_overview(7)`](https://man7.org/linux/man-pages/man7/mq_overview.7.html)). Better
than System V: on Linux the descriptor *is* a file descriptor and can be polled, and
messages carry priorities with strict priority-ordered delivery, which nothing else here
offers. Still not worth it: **macOS does not implement them at all**, Linux caps them
tightly by default (`/proc/sys/fs/mqueue/msg_max`, default `mq_maxmsg` of 10), they need
`-lrt`, and there is no stdlib Python binding. If you genuinely need priority-ordered
local delivery, you will get there faster with a socket and a heap in userspace.

**The through-line:** the mechanisms that survived are the ones that are file
descriptors. `epoll`/`kqueue` integration, `SCM_RIGHTS` passing, and automatic cleanup on
process death all follow from being an fd. A local IPC primitive that is not an fd is a
primitive from before that lesson was learned.

---

## 6. What `multiprocessing` is actually built out of

Everything in this section is a thin wrapper over §2–§4. Knowing which wrapper you have
tells you its cost and its failure mode.

### 6.1 `mp.Pipe` is a socketpair — or two pipes

```python
# Lib/multiprocessing/connection.py L566-580
def Pipe(duplex=True):
    '''Returns pair of connection objects at either end of a pipe'''
    if duplex:
        s1, s2 = socket.socketpair()
        s1.setblocking(True)
        s2.setblocking(True)
        c1 = Connection(s1.detach())
        c2 = Connection(s2.detach())
    else:
        fd1, fd2 = os.pipe()
        c1 = Connection(fd1, writable=False)
        c2 = Connection(fd2, readable=False)
```

`duplex=True` (the default) gets you a UNIX socketpair, with the 8 KiB buffer from §3.4.
`duplex=False` gets you a real pipe, with the 64 KiB buffer from §2.2. **The default is
the smaller buffer**, and if you only need one direction, `duplex=False` gives you 8×
the in-flight capacity and one fewer fd per end. That is the entire trade and it is not
documented anywhere near the API.

### 6.2 `Connection` framing: length-prefixed, with two special cases

A byte stream needs framing. `Connection` prefixes each message with its length:

```python
# Lib/multiprocessing/connection.py L427-448
def _send_bytes(self, buf):
    n = len(buf)
    if n > 0x7fffffff:
        pre_header = struct.pack("!i", -1)
        header = struct.pack("!Q", n)
        self._send(pre_header)
        self._send(header)
        self._send(buf)
    else:
        # For wire compatibility with 3.7 and lower
        header = struct.pack("!i", n)
        if n > 16384:
            # The payload is large so Nagle's algorithm won't be triggered
            # and we'd better avoid the cost of concatenation.
            self._send(header)
            self._send(buf)
        else:
            # Issue #20540: concatenate before sending, to avoid delays due
            # to Nagle's algorithm on a TCP socket.
            self._send(header + buf)
```

Three things worth extracting:

- **The wire format is a 4-byte big-endian length**, with `-1` as an escape introducing
  an 8-byte length for payloads over 2 GiB. The comment says why the escape exists rather
  than just widening the field: **wire compatibility with Python 3.7**. `Connection` is
  a cross-version protocol.
- **The 16384-byte branch is a Nagle workaround.** Below the threshold, header and
  payload are concatenated into one `send()`; above it, two sends. The reason is
  [bpo-20540](https://github.com/python/cpython/issues/64739): a small header sent alone
  is held by Nagle's algorithm waiting for an ACK, adding up to 40 ms of latency **on a
  TCP `Connection`** — `multiprocessing.connection` also speaks TCP for its
  `Listener`/`Client` API. On a socketpair Nagle does not exist, so this branch costs a
  concatenation for nothing. It is the right call anyway: one syscall beats two, and the
  copy is cheap below 16 KiB.
- **`send_bytes`/`recv_bytes` skip pickle entirely.** If your payload is already bytes
  — a serialized frame, a NumPy buffer — `send_bytes` is strictly cheaper than `send`
  and does not go near `pickle`. Most code that "uses `multiprocessing.Pipe`" is really
  paying for `pickle` it does not need.

### 6.3 `mp.Queue` is a pipe, plus a thread, plus two locks, plus a semaphore

The file's own section header does not undersell it:

```python
# Lib/multiprocessing/queues.py L30
# Queue type using a pipe, buffer and thread
```

Construction (L37-47) allocates: a `Pipe(duplex=False)` — so a **real pipe**, 64 KiB —
a read lock, a write lock, and a `BoundedSemaphore(maxsize)` where an unbounded queue
uses `SEM_VALUE_MAX`, measured at **32767** on this machine. Then `put`:

```python
# Lib/multiprocessing/queues.py L84-94
def put(self, obj, block=True, timeout=None):
    if self._closed:
        raise ValueError(f"Queue {self!r} is closed")
    if not self._sem.acquire(block, timeout):
        raise Full
    with self._notempty:
        if self._thread is None:
            self._start_thread()
        self._buffer.append(obj)
        self._notempty.notify()
```

**`put()` does not write to the pipe.** It takes a slot from the cross-process semaphore,
appends the *live Python object* to an in-process `deque`, and signals a condition
variable. A background feeder thread — started lazily on first `put` — does the real
work:

```python
# Lib/multiprocessing/queues.py L261-270
# serialize the data before acquiring the lock
obj = _ForkingPickler.dumps(obj)
if wacquire is None:
    send_bytes(obj)
else:
    wacquire()
    try:
        send_bytes(obj)
    finally:
        wrelease()
```

Note the ordering, which is a genuinely good piece of engineering: **pickle first, then
take the write lock.** Serialization is the expensive part and it happens outside the
critical section, so N producers serialize in parallel and contend only for the
`send_bytes`.

So one `Queue.put(obj)` costs: a semaphore acquire (a cross-process syscall, §11), a
`deque.append` under a `threading.Lock`, a condition notify, a thread wakeup, a
`pickle.dumps`, a cross-process lock acquire, and a `write()` to a pipe. The
corresponding `get()` costs the read lock, a `read()`, a `pickle.loads`, and a semaphore
release. Calling this "a queue" is accurate and calling it "cheap" is not.

### 6.4 The five consequences of the feeder thread

This design is the source of nearly every `multiprocessing.Queue` surprise:

1. **`put()` is asynchronous.** It returns before the data is anywhere a reader can see.
2. **A process can exit with data still in the buffer.** Hence `Queue.close()` +
   `join_thread()`, and hence the documented deadlock: a process that has put items on a
   queue will not terminate until the feeder has flushed, so a parent that `join()`s a
   child before draining the queue deadlocks. This is in the docs as a warning; it is
   really a consequence of the architecture.
3. **`qsize()` is unreliable, and here it does not exist at all** (§11.2).
4. **`empty()` can lie.** Measured below.
5. **The feeder thread is subject to the GIL and to `fork` hazards.** Doc 10 §9: fork a
   process whose feeder thread holds the write lock and the child inherits a locked lock
   with no owner.

**The `empty()` race, measured.** Immediately after `q.put(1)`:

```
q.empty() returns True immediately after q.put():   28/200 trials (cold queue)
                                                     24/200 trials (warm queue)
time until the feeder makes it visible: median 3.1 us, p95 6.2 us, max 22.2 us
```
*(measured)* — **about one time in seven, a queue you just put an item into reports
itself empty**, and the window is a few microseconds. Note the warm-queue number: this is
not a thread-startup artifact, it is the steady-state behaviour. Any code shaped like
`if not q.empty(): q.get()` is a race, and any code shaped like
`while not q.empty(): process(q.get())` will exit early under load. The correct patterns
are `get(timeout=...)` with `except Empty`, or a sentinel value — never `empty()`.

`SimpleQueue` is the one without the thread: a pipe and two locks, and `put()` pickles
and writes synchronously on the calling thread. If you want a queue whose `put()` means
what it says, that is the one — at the cost of `put()` blocking when the pipe fills.

### 6.5 `mp.Manager` is a different animal entirely

A `Manager` starts a **separate server process** holding the real objects, and hands out
proxies. Every attribute access and every method call on a proxy is a full round trip:
pickle the call, write to a socket, the server unpickles, executes, pickles the result,
writes back. `manager_dict['k'] += 1` is two round trips and is **not atomic** — a
textbook check-then-act race across three processes.

Managers buy you two things nothing else does: arbitrary Python objects shared by
reference-semantics, and **access from processes that are not descendants**, over a TCP
socket with an auth key. They cost 3–4 orders of magnitude more per operation than shared
memory. Use them for coordination and configuration, never in a loop.

---

## 7. Shared memory: the mapping

### 7.1 The four calls

POSIX shared memory is a four-step recipe
([`shm_overview(7)`](https://man7.org/linux/man-pages/man7/shm_overview.7.html)):

```c
fd = shm_open("/name", O_CREAT|O_EXCL|O_RDWR, 0600);  // a name -> an fd
ftruncate(fd, size);                                   // new objects are 0 bytes
p  = mmap(NULL, size, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
close(fd);                                             // the mapping keeps it alive
// ... later, exactly once, by someone:
shm_unlink("/name");                                   // remove the NAME
```

Two facts do all the work in the rest of this document:

**The fd is not the resource.** After `mmap`, you can `close(fd)` and the mapping
survives. The mapping holds the reference.

**`shm_unlink` removes the *name*, not the object.** Exactly like `unlink(2)` on a file:
existing mappings keep working, the memory is freed when the last one goes away, but no
new process can open it by that name. Every cleanup design for shared memory is a
consequence of this split, and §8.3 is what happens when a library gets it wrong.

On Linux, `shm_open` is implemented as `open()` under `/dev/shm`, a `tmpfs`. That means
you can `ls /dev/shm`, you can see segment sizes with `du`, and the memory is charged
against the tmpfs limit (typically half of RAM) — which is why "we ran out of shared
memory" is usually "`/dev/shm` is 64 MB in this container". On this machine:

```
os.path.isdir("/dev/shm") -> False
```
*(measured)* — macOS implements POSIX shared memory in the kernel with no filesystem
view. **You cannot list your leaked segments on macOS.** There is no `ls` for them.
This makes §8.3's leak warnings the only signal you get.

### 7.2 `MAP_SHARED` vs `MAP_PRIVATE`: the flag that decides everything

`mmap`'s flags decide whether writes are visible to anyone else
([`mmap(2)`](https://man7.org/linux/man-pages/man2/mmap.2.html)):

- **`MAP_SHARED`** — writes go to the underlying object and are visible to every other
  process mapping it. This is IPC.
- **`MAP_PRIVATE`** — copy-on-write. You see the object's initial contents; your writes
  fault a private copy and are visible to nobody. This is `fork` semantics (doc 07),
  and it is how `malloc` gets its arenas.

Getting this backwards produces a bug with the worst possible signature: everything
works in one process, and the other process sees stale data forever, with no error.

Anonymous shared memory — `mmap(-1, size, MAP_SHARED | MAP_ANONYMOUS)` — has no backing
object and no name, and is inherited across `fork`:

```python
m = mmap.mmap(-1, 4096, flags=mmap.MAP_SHARED | mmap.MAP_ANONYMOUS)
if os.fork() == 0:
    m[0:5] = b'child'; os._exit(0)
os.waitpid(pid, 0)
bytes(m[0:5])   # -> b'child'
```
*(measured)* — the child's store is visible in the parent. For a parent and its forked
children this is the **cleanest shared memory available**: no name, no filesystem, no
cleanup, no resource tracker, and it disappears when the last mapping does. It is
strictly better than `SharedMemory` for the `fork`-only case, and almost nobody uses it
because it is not in `multiprocessing`.

### 7.3 `memfd_create`: the Linux answer to naming

`memfd_create(2)` creates an anonymous file living in RAM, returning an fd with **no
filesystem name at all**. Combine it with `SCM_RIGHTS` (§4) and you have shared memory
with no name to collide on, no cleanup to get wrong, and no window in which a third
process can open your segment. It also supports **file sealing** (`F_SEAL_SHRINK`,
`F_SEAL_WRITE`) so a receiver can verify the sender cannot shrink the file out from under
its mapping — which closes a real `SIGBUS` attack.

```
hasattr(os, 'memfd_create') -> False
```
*(measured)* — Linux 3.17+ only; not on macOS. **Not measured here:** the sealing
semantics above are from
[`memfd_create(2)`](https://man7.org/linux/man-pages/man2/memfd_create.2.html).

The `SIGBUS` hazard is worth stating even without seals, because it applies everywhere:
**if the backing object shrinks below a mapped page, touching that page raises `SIGBUS`,
not a Python exception.** `ftruncate` down while another process has it mapped and that
process dies on a memory access. This is the sharpest edge in all of family 2 and it is
one `ftruncate` away.

### 7.4 Page granularity: the size you asked for is not the size you got

```
mmap.PAGESIZE               : 16384
mmap.ALLOCATIONGRANULARITY  : 16384
SharedMemory(create=True, size=1)    -> .size == 16384, len(buf) == 16384
SharedMemory(create=True, size=4096) -> .size == 16384
```
*(measured)* — mappings are page-granular, and **this machine's page is 16 KB, four times
Linux's x86-64 default.** A "1-byte" segment costs 16 KiB of address space and, once
touched, 16 KiB of RSS.

Two consequences:

- **A thousand small segments cost 16 MB here and 4 MB on Linux**, for the same code. If
  you are sharding shared memory per-worker, the granularity is a real budget item, and
  it is a *portable code, different bill* situation.
- **`SharedMemory.size` is authoritative, not your argument.** The code reads it back
  from the kernel rather than trusting the request:

```python
# Lib/multiprocessing/shared_memory.py L113-117
if create and size:
    os.ftruncate(self._fd, size)
stats = os.fstat(self._fd)
size = stats.st_size
self._mmap = mmap.mmap(self._fd, size)
```

  So `shm.buf` is longer than you asked for. Code that does `shm.buf[:] = payload`
  after attaching will raise on a size mismatch, and code that treats `len(shm.buf)` as
  the payload length will read garbage. **Store your own length in the segment**, or pass
  it out of band. The docs say only that the size "may be larger or equal to the
  requested" — here it is always larger for anything under 16 KiB.

---

## 8. Shared memory from Python

Four APIs, in increasing order of how much rope they give you.

### 8.1 `mmap` directly

The `mmap` module maps a file (or anonymous memory with `fileno=-1`) and gives you a
mutable buffer supporting slicing, `find`, `read`, `write`, `flush`, `madvise`, and the
buffer protocol. `access=ACCESS_WRITE` is `MAP_SHARED`; `access=ACCESS_COPY` is
`MAP_PRIVATE`.

For sharing a large *read-mostly* dataset between workers on the same host, mapping a
file is the best-value option in this document: the page cache backs it, so N workers
mapping the same file consume the pages **once** in physical memory regardless of N, and
the kernel handles eviction. `numpy.memmap` is a thin layer on this and is the right
answer far more often than `shared_memory` is.

`flush()` (`msync`) matters only for file-backed mappings you want durable on disk. It
is **not** a synchronization primitive between processes — the pages are already shared;
there is nothing to flush to make a store visible to a peer. People reach for it as if it
were a memory barrier. It is not. §13.

### 8.2 `multiprocessing.shared_memory.SharedMemory`

The stdlib's named-segment API (3.8+):

```python
shm = SharedMemory(create=True, size=1_000_000)   # -> shm.name like 'psm_0b5cb824'
other = SharedMemory(name=shm.name)               # attach from another process
shm.buf[0:4] = b'\x01\x02\x03\x04'
shm.close()      # drop this process's mapping
shm.unlink()     # remove the name — exactly once, globally
```

Names are generated as `'/psm_' + secrets.token_hex(4)` — `_SHM_SAFE_NAME_LENGTH = 14`,
`_SHM_NAME_PREFIX = '/psm_'` (L31-46) — deliberately short because some platforms cap
`shm_open` names well below `NAME_MAX`. Collisions are handled by retrying in a loop
(L92-101).

`ShareableList` sits on top and stores a fixed-length list of `int` (signed 64-bit),
`float`, `bool`, `str`, `bytes`, or `None` by `struct.pack_into`-ing a header of offsets
plus per-item format codes plus the data. It cannot change length and cannot be sliced
into a new instance. It is a demo of the layout technique, not a data structure to build
on: every read is a `struct.unpack` and a fresh Python object, and there is **no locking
whatsoever**.

### 8.3 The resource tracker, and the design mistake it papers over

Family 2's cleanup problem, restated: `shm_unlink` must be called **exactly once,
globally**, by someone, eventually — and no participant knows when it is last out. If
everyone unlinks, the second call fails. If nobody does, the segment survives every
process that knew about it, and on macOS you cannot even list it (§7.1).

`multiprocessing`'s answer is a **separate resource-tracker process**. Every
`SharedMemory` registers itself (`shared_memory.py` L121-122), every `unlink` unregisters
(L253-254), and the tracker — which ignores `SIGINT`/`SIGTERM` and reads commands from a
pipe using the sub-`PIPE_BUF` protocol of §2.3 — unlinks whatever is still registered
when the pipe closes, printing:

```python
# Lib/multiprocessing/resource_tracker.py L475-480
warnings.warn(
    f'resource_tracker: There appear to be '
    f'{len(rtype_cache)} leaked {rtype} objects to '
    f'clean up at shutdown: {rtype_cache}'
)
```

Everyone who has used `SharedMemory` has seen that warning. Here is why it is not
(always) your bug.

**The bug: [gh-82300](https://github.com/python/cpython/issues/82300), "resource tracker
destroys shared memory segments when other processes should still have valid access."**
The tracker is per-*process-tree*. Processes created by `multiprocessing` from a common
ancestor share one tracker and the bookkeeping works. But a process that merely
*attaches* to a segment by name — a `subprocess`, a standalone script, a different
service — **spawns its own tracker**, which registers the segment it attached to and
then unlinks it on exit. From the docs:

> "This will cause the shared memory to be deleted by the resource tracker of the first
> process that terminates."

So the intended cross-process use case — a producer writing a segment, an independent
consumer reading it — **destroys the segment when the consumer exits first**, and the
producer is left mapping memory with no name, silently. The bug was opened in 2019 and
the fix, merged for 3.13, was not to fix the tracking but to add an escape hatch:

```python
SharedMemory(name='psm_...', track=False)   # do not register with the tracker
```

**The rule:** if the process **created** the segment, use the default `track=True` and
call `unlink()` yourself when done. If the process is only **attaching** to a segment
someone else owns, pass **`track=False`**, and never call `unlink()`. On Windows `track`
is ignored — the OS refcounts handles and the whole problem does not exist.

**And the deeper point:** none of this is necessary if you never give the segment a name.
Anonymous `MAP_SHARED` (§7.2) for forked children, or `memfd_create` + `SCM_RIGHTS`
(§4.1, §7.3) for the general case, are both automatically cleaned up by the kernel's own
refcounting. The resource tracker is a userspace reimplementation of `close()`, made
necessary by choosing a namespace-based API. **`multiprocessing.shared_memory` picked the
one POSIX shared-memory idiom that requires manual global cleanup.**

### 8.4 `Value`, `Array`, and the arena underneath

`multiprocessing.Value('i', 0)` and `Array('d', 4)` return ctypes objects allocated from a
shared heap. The heap is a set of mmap'd arenas:

```python
# Lib/multiprocessing/heap.py L67-89
class Arena(object):
    """A shared memory area backed by a temporary file (POSIX)."""
    if sys.platform == 'linux':
        _dir_candidates = ['/dev/shm']
    else:
        _dir_candidates = []

    def __init__(self, size, fd=-1):
        ...
        self.fd, name = tempfile.mkstemp(
             prefix='pym-%d-'%os.getpid(),
             dir=self._choose_dir(size))
        os.unlink(name)
        util.Finalize(self, os.close, (self.fd,))
        os.ftruncate(self.fd, size)
        self.buffer = mmap.mmap(self.fd, self.size)
```

This is the **right** idiom, and it is instructive that it lives one module away from
§8.3's wrong one: create a temp file, `mmap` it, then **`os.unlink` the name
immediately**. The mapping and the fd keep the object alive; there is no name for anyone
to collide with, leak, or unlink at the wrong time; the kernel frees it when the last
reference closes. No resource tracker required.

Note `_dir_candidates`: on Linux it prefers `/dev/shm` (`tmpfs`, never touches a disk)
and falls back to the temp dir; on macOS there is no candidate, so the arena is a file in
`/var/folders/.../pymp-*` *(measured)*, backed by the filesystem. Pages are still in the
page cache so it is fast, but it is not the same thing, and on a machine under memory
pressure those pages can be written to disk.

Three things about `Value`/`Array` that the API does not make obvious:

- **`Value(...)` has a lock by default; `RawValue` does not.** `Value` returns a
  `Synchronized` wrapper holding an `RLock` — measured: `<Synchronized wrapper for
  c_int(0)>` whose `.get_obj()` is a plain `ctypes.c_int`.
- **The lock only guards the wrapper's own accessors.** `v.value += 1` is
  read-modify-write through the property and is **not atomic** even with the lock,
  because the lock is taken *inside* `.value`'s getter and again inside its setter, not
  across both. You need `with v.get_lock(): v.value += 1`. The docs warn that setting and
  getting an element of an `Array` "is potentially non-atomic"; the increment case is
  worse than that and is the actual bug people ship.
- **These are inherited, not attached.** They must be passed to `Process(...)` at
  construction; with `spawn` they are pickled through `sharedctypes.reduce_ctype`, which
  transmits the arena fd. You cannot look one up by name later. That is the trade against
  `SharedMemory`: less flexibility, and none of §8.3's problems.

---

## 9. Why you cannot put a Python object in shared memory

This question comes up in every design review, and the answer is not "the API doesn't
support it" — it is structural, and there are four independent reasons, each of which is
individually fatal.

**1. `PyObject*` is an address, and addresses are per-process.** Even if two processes
map the same segment, they may map it at different virtual addresses. A `list` in shared
memory is a `PyListObject` whose `ob_item` points at a separate allocation; that pointer
is a number meaningful only in the writer's page tables. The `multiprocessing`
documentation states the consequence flatly for the one case it exposes:

> "Although it is possible to store a pointer in shared memory remember that this will
> refer to a location in the address space of a specific process. However, the pointer is
> quite likely to be invalid in the context of a second process and trying to dereference
> the pointer from the second process may cause a crash."

Even mapping at a fixed address (`MAP_FIXED`) does not save you: ASLR, existing mappings,
and any allocation outside the segment break it. This alone ends the discussion.

**2. Refcounts.** Every `PyObject` carries `ob_refcnt`, mutated by every process that
touches it. `Py_INCREF` in the GIL build is a plain non-atomic increment (doc 15), so two
processes incrementing concurrently lose updates and the object is freed while in use —
**a use-after-free across a process boundary, in shared memory, with no GIL to serialize
it** (the GIL is per-process; it provides exactly zero protection here). Even the
free-threaded build's atomic refcounts would not save you, because deallocation calls
`free()` into a **per-process allocator** that knows nothing about the segment.

**3. The allocator.** Objects come from `pymalloc` arenas obtained via `mmap(MAP_PRIVATE)`
(doc 08). To put an object in shared memory you would need CPython to allocate from your
segment, which means a custom allocator domain, which means the segment must contain its
own heap metadata, which must itself be concurrency-safe across processes.

**4. Type pointers.** `ob_type` points at a type object, which lives at a different
address in every process — different for `spawn`ed children by construction, and
different even for forked ones once anything triggers a COW fault. Dereferencing it in
the wrong process reads whatever happens to be there.

**Therefore the only two things you can put in shared memory from Python are:**

- **Fixed-layout binary data** — `struct`-packed records, ctypes structures, NumPy arrays
  over the buffer, raw bytes. No Python-object headers cross the boundary; you rebuild
  objects on each side from the bytes.
- **Serialized objects** — pickle into the segment. But then you have paid the
  serialization cost, which was the entire reason you left family 1. The only thing you
  saved is the two kernel copies, which for a large payload is real but is usually the
  smaller half of the bill.

This is why `ShareableList` supports exactly six types, why `SharedMemory` gives you a
`memoryview` and nothing else, and why the actual production pattern is
`np.ndarray(shape, dtype, buffer=shm.buf)` — NumPy's array object lives in your private
heap and only its *data pointer* aims at the shared pages. The object is private; the
bytes are shared. That is the only shape that works.

**And the corollary that people miss:** attaching a NumPy view costs nothing, so the
zero-copy win is real for a 1 GB array and imaginary for a 1 KB one. Below roughly a few
tens of KB, the cost of `SharedMemory` setup, the resource tracker round trip, and the
16 KB page rounding exceed the pickling you avoided. §14.

---

## 10. Synchronization: the futex and its absence

Family 2 gives you memory and no protocol. Here is what the protocol is made of.

### 10.1 The futex, and the idea behind it

A **futex** — fast userspace mutex — is a 4-byte, 4-byte-aligned integer in memory plus
two kernel operations
([`futex(2)`](https://man7.org/linux/man-pages/man2/futex.2.html)):

- **`FUTEX_WAIT(uaddr, val)`** — atomically: *if* `*uaddr == val`, sleep. Otherwise
  return immediately with `EAGAIN`.
- **`FUTEX_WAKE(uaddr, n)`** — wake up to `n` waiters on that address.

The entire design is in the word *if*. The compare-and-sleep is atomic with respect to
other futex operations, which closes the lost-wakeup race: without it, a thread could
check "the lock is held", get preempted, have the holder release and wake nobody, and
then sleep forever.

The payoff: **an uncontended lock never enters the kernel at all.** Lock is a CAS on the
integer; unlock is a store. You call `FUTEX_WAIT` only when you actually have to block
and `FUTEX_WAKE` only when someone is actually waiting — which the fast path tracks with
a bit in the same word. A mutex costs a few nanoseconds when nobody is contending and a
syscall only when the alternative is spinning. This is why "locks are expensive" is
folklore rather than fact; *contention* is expensive.

The man page notes there is **no explicit initialization or destruction** — the kernel
maintains state for a futex only while an operation is in flight. A futex is just an
integer; you can put one in shared memory and it works across processes, which is
precisely what `pthread_mutex_t` with `PTHREAD_PROCESS_SHARED` does.

`FUTEX_PRIVATE_FLAG` (Linux 2.6.22+) tells the kernel the futex is not shared between
processes, letting it skip the page-table walk that maps the address to a global
identity. **A process-shared mutex is measurably more expensive than a process-private
one** for this reason. Beyond the basics, Linux offers `FUTEX_WAIT_BITSET` (selective
wakeup), the `FUTEX_LOCK_PI` family (priority inheritance, which fixes the priority
inversion of doc 30), and `FUTEX_WAITV` (wait on several at once). **Not measured here:**
all of it, because —

### 10.2 There is no futex on this machine

macOS has no `futex(2)`. The equivalent is Apple's private `__ulock_wait`/`__ulock_wake`
in `libsystem_platform`, sitting under `os_unfair_lock` and `pthread_mutex`. The *idea*
is the same — an in-memory word, a compare-and-sleep, no syscall when uncontended — but
the interface is private, undocumented, and not something to build on. macOS 14 exposed
`os_sync_wait_on_address`, a public futex-like API, which is the sanctioned path forward.
**Not measured here**; I did not verify what CPython links against at the libplatform
level, and §16 says so.

The portable consequence: **"just use a futex" is not portable advice**, and anything you
write that assumes `futex(2)` is Linux-only.

### 10.3 CPython's own answer: `PyMutex` and the parking lot

CPython does not use futexes directly. It implements a **parking lot** — the WebKit
`WTF::Lock` design, credited in the header:

```c
// Include/internal/pycore_lock.h L1-5
// Lightweight locks and other synchronization mechanisms.
//
// These implementations are based on WebKit's WTF::Lock. See
// https://webkit.org/blog/6161/locking-in-webkit/ for a description of the
// design.
```

A `PyMutex` is **one byte**: bit 0 is `_Py_LOCKED`, bit 1 is `_Py_HAS_PARKED`. All the
waiter bookkeeping lives in a side table (`Python/parking_lot.c`) keyed by the address of
the lock. That is what makes a one-byte mutex possible, and it is why free-threaded
CPython can afford a lock in every object header.

The acquire path (`Python/lock.c` L53-120) is worth reading as the canonical shape:

```c
uint8_t v = _Py_atomic_load_uint8_relaxed(&m->_bits);
if ((v & _Py_LOCKED) == 0) {
    if (_Py_atomic_compare_exchange_uint8(&m->_bits, &v, v|_Py_LOCKED)) {
        return PY_LOCK_ACQUIRED;          // fast path: one CAS, no syscall
    }
}
...
if (!(v & _Py_HAS_PARKED) && spin_count < MAX_SPIN_COUNT) {
    _Py_yield();                          // sched_yield(), bounded
    spin_count++;
    continue;
}
...
newv = v | _Py_HAS_PARKED;                // announce that a waiter exists
...
int ret = _PyParkingLot_Park(&m->_bits, &newv, sizeof(newv), timeout,
                             &entry, (flags & _PY_LOCK_DETACH) != 0);
```

Three details:

```c
// Python/lock.c L22-31
static const PyTime_t TIME_TO_BE_FAIR_NS = 1000*1000;
#if Py_GIL_DISABLED
static const int MAX_SPIN_COUNT = 40;
#else
static const int MAX_SPIN_COUNT = 0;
#endif
```

- **Spinning is 40 iterations on free-threaded builds and 0 on the default build**, with
  the comment "it is unlikely to be helpful if the GIL is enabled" — correct, since a
  spinning thread that holds the GIL is spinning against a thread that cannot run.
- **`TIME_TO_BE_FAIR_NS = 1 ms`**: if a waiter has been queued longer than a millisecond,
  the unlocker hands ownership *directly* to it rather than releasing and letting the
  next CAS win. This is barging prevention — the fix for the lock convoying and starvation
  of doc 30.
- **`_PY_LOCK_DETACH`** releases the GIL before parking. A `PyMutex` acquired without it
  blocks with the GIL held, which stops the world.

And the platform sting: `_PySemaphore`, the thing a parked thread actually sleeps on,
picks its implementation from a config test:

```c
// Include/internal/pycore_semaphore.h L25-29
#if (defined(_POSIX_SEMAPHORES) && (_POSIX_SEMAPHORES+0) != -1 && \
        defined(HAVE_SEM_TIMEDWAIT))
#   define _Py_USE_SEMAPHORES
#endif
```

```
sysconfig HAVE_SEM_TIMEDWAIT = 0
```
*(measured)* — **macOS does not have `sem_timedwait`, so `_Py_USE_SEMAPHORES` is not
defined here** and every parked thread in CPython on this machine sleeps on the
`pthread_mutex_t` + `pthread_cond_t` fallback (`parking_lot.c`) rather than on a POSIX
semaphore. Same semantics, different code path, different cost — and a difference nobody
would guess from the Python level. §16 notes I did not quantify the gap.

---

## 11. `multiprocessing.Lock` is a named POSIX semaphore

### 11.1 What it is

Not a futex, not a `PyMutex`, not a `pthread_mutex` in shared memory. A
`multiprocessing.Lock` is a **named POSIX semaphore** with an initial value of 1:

```c
// Modules/_multiprocessing/semaphore.c L225
#define SEM_CREATE(name, val, max) sem_open(name, O_CREAT | O_EXCL, 0600, val)
```

```python
# Lib/multiprocessing/synchronize.py L173-176
class Lock(SemLock):
    def __init__(self, *, ctx):
        SemLock.__init__(self, SEMAPHORE, 1, 1, ctx=ctx)
```

Measured: `mp.Lock()._semlock.name == '/mp-pdpb8zto'`. It is a kernel object with a
global name, and therefore it has §8.3's cleanup problem — solved the same way, with
the resource tracker, and with one clever optimization:

```python
# Lib/multiprocessing/synchronize.py L52-53, 70-80
self._is_fork_ctx = ctx.get_start_method() == 'fork'
unlink_now = sys.platform == 'win32' or self._is_fork_ctx
...
if self._semlock.name is not None:
    # We only get here if we are on Unix with forking disabled. ...
    from .resource_tracker import register
    register(self._semlock.name, "semaphore")
```

**Under `fork`, the semaphore is unlinked immediately after creation** — the child
inherits the open semaphore through the fork, so the name is never needed and there is
nothing to leak. This is exactly §8.4's arena trick again. Under `spawn`/`forkserver` the
name *is* needed (the child re-opens it by name, L568: `handle = sem_open(name, 0)`), so
it must stay linked and the tracker must clean it up. **Another way of saying that:
`spawn` is what creates the leak surface, not shared memory as such.**

Note also L109's guard: a `SemLock` created in a `fork` context and then pickled to a
`spawn` process raises `RuntimeError` — because it has already been unlinked and there is
no name to re-open. Mixing start methods breaks locks, loudly, which is better than the
alternative.

### 11.2 The macOS tax, in the source

Two config variables on this machine reach up through several layers of C and change what
Python-level code does:

```
HAVE_SEM_TIMEDWAIT       = 0
HAVE_BROKEN_SEM_GETVALUE = 1
```
*(measured)*

**`HAVE_SEM_TIMEDWAIT = 0` means `Lock.acquire(timeout=...)` polls.** CPython supplies
its own `sem_timedwait`:

```c
// Modules/_multiprocessing/semaphore.c L245-300 (abridged)
static int
sem_timedwait_save(sem_t *sem, struct timespec *deadline, PyThreadState *_save)
{
    for (delay = 0 ; ; delay += 1000) {
        if (sem_trywait(sem) == 0)          /* poll */
            return 0;
        ...
        /* check delay not too long -- maximum is 20 msecs */
        if (delay > 20000)
            delay = 20000;
        if (delay > difference)
            delay = difference;
        ...
        if (select(0, NULL, NULL, NULL, &tvdelay) < 0)   /* sleep */
            return MP_STANDARD_ERROR;
        Py_BLOCK_THREADS
        res = PyErr_CheckSignals();
        Py_UNBLOCK_THREADS
```

A **spin-then-back-off poll loop**, ramping the sleep by 1 ms per iteration to a 20 ms
ceiling, using `select()` with no fds as a sleep. So on this machine:

- A timed `acquire()` on a contended lock can sit up to **20 ms past the moment the lock
  became free**. Blocking `acquire()` with no timeout uses real `sem_wait` and does not
  have this problem — **the timeout is what costs you**, which is precisely backwards
  from what anyone would guess.
- The loop calls `PyErr_CheckSignals()` on every iteration, so a timed acquire *is*
  Ctrl-C-interruptible (doc 10 §4) — an accidental benefit of the polling.
- The 20 ms floor means a timed-acquire-based work loop can never exceed ~50 handoffs per
  second per contended lock on macOS.

**`HAVE_BROKEN_SEM_GETVALUE = 1` means `Queue.qsize()` does not exist here:**

```python
>>> q = mp.Queue(); q.put(1); q.qsize()
NotImplementedError
```
*(measured)* — macOS's `sem_getvalue` does not report what CPython needs, so
`semaphore.c` emulates it with `sem_trywait` + `sem_post` (L414-420, L656-660) and
`Queue.qsize` raises. Code that works on Linux and calls `qsize()` fails on a developer's
Mac with a bare `NotImplementedError`. And on Linux, where it *does* work, it is racy
anyway (§6.4). **`qsize()` is never the right call.**

### 11.3 The hazard family 1 does not have: a cross-process lock has no owner

A `threading.Lock` dies with its process. A cross-process lock does not.

**If a process crashes while holding a `multiprocessing.Lock`, the lock stays held
forever.** There is no owner for the kernel to notice died, no cleanup, no timeout. Every
other worker blocks in `sem_wait` until someone notices and kills the whole tree. A
`kill -9` on a worker mid-critical-section takes the pool down with it, silently, with a
stack trace pointing at a perfectly innocent `lock.acquire()`.

Linux has an answer — **robust futexes** (`pthread_mutexattr_setrobust`): the kernel
walks the dead thread's robust list, marks the mutex, and the next acquirer gets
`EOWNERDEAD` and a chance to repair the invariant with `pthread_mutex_consistent`.
Python exposes none of this. Neither does macOS.

**So the practical rules for family 2 are:**

- **Keep critical sections tiny and non-blocking.** Never do I/O under a cross-process
  lock; never call into user code.
- **Prefer a design with no cross-process lock at all** — single writer with per-worker
  regions, or a lock-free ring buffer with the ordering worked out (doc 03), or a
  supervisor that owns all writes.
- **If you must, add a watchdog** that can detect a stuck pool and restart it, because
  the lock itself will never tell you.
- **Fewer participants beats faster locks.** The one thing that reliably reduces this
  risk is having less shared mutable state.

---

## 12. Waking someone up: `eventfd` and the self-pipe

Family 2 has no notification. So how does a process *sleep* until shared memory changes,
in a way that composes with an event loop?

**`eventfd(2)`** is Linux's answer: an fd wrapping a kernel-maintained 64-bit counter. A
`write()` adds to it; a `read()` returns and resets it (or decrements by 1 with
`EFD_SEMAPHORE`); it is readable exactly when the counter is nonzero. It costs **one fd
instead of a pipe's two**, has no buffer to fill, and is `select`/`poll`/`epoll`-able —
so it is the standard way to make "something happened in shared memory" visible to an
event loop.

```
hasattr(os, 'eventfd') -> False
```
*(measured)* — `os.eventfd` was added in Python 3.10 but is Linux-only, and it is not
here. **Not measured here:** everything above about `eventfd`'s semantics, from
[`eventfd(2)`](https://man7.org/linux/man-pages/man2/eventfd.2.html).

**The portable fallback is the self-pipe trick**, which doc 10 §7.3 and doc 28 both lean
on: create a pipe, register the read end with your event loop, and have the notifier
write one byte. Two fds instead of one, a buffer that can fill (drain it with
`O_NONBLOCK` reads in a loop, or you will wedge), and the same `PIPE_BUF` atomicity
guarantee from §2.3 if multiple notifiers write. This is exactly what
`asyncio`'s `_UnixSelectorEventLoop` does for `call_soon_threadsafe`, and what
`signal.set_wakeup_fd` exists for.

**The composition rule this section is really about:** a semaphore and a futex are not
pollable, so a process that blocks on either cannot simultaneously wait for I/O. The
moment you need "wake on shared-memory change **or** socket readable **or** timeout", the
notification must be an fd — `eventfd` on Linux, a self-pipe everywhere. This is the
single strongest argument for keeping a family-1 control channel alongside your family-2
data channel: **share the bytes, but signal over a pipe.** That hybrid is the shape of
almost every good design in §14.

---

## 13. The memory-model problem, in Python

Doc 02 established that on AArch64 — this machine — stores by one core become visible to
another **out of order** unless barriers say otherwise, and that C11's acquire/release
semantics are how you say otherwise. Doc 30 established that pure Python cannot have a
data race in the C sense because the GIL serializes bytecode.

**Shared memory revokes that second guarantee.** The GIL is per-process. Two processes
writing the same mapped page are two cores writing the same cache line with no
serialization of any kind, and now the doc 02 problem is yours in a language that does
not have the vocabulary to solve it:

- **There is no `volatile`, no `atomic`, no acquire/release in Python.** `shm.buf[0] = 1`
  is a `memoryview` store. The language specification says nothing about when another
  process observes it. CPython emits an ordinary store; the CPU may reorder it with
  respect to your other stores.
- **The classic flag pattern is broken.** Write the payload, then set `buf[0] = 1` as a
  "ready" flag; the reader spins on `buf[0]` and then reads the payload. On AArch64 the
  flag store can become visible *before* the payload stores, and the reader consumes
  garbage. In C you would write a release-store and an acquire-load. In Python you cannot
  express that at all.
- **`mmap.flush()` is not a barrier.** It is `msync`, about durability to a backing file.
  It does not order stores with respect to another process's loads. Reaching for it here
  is a category error.
- **Torn reads are real.** An 8-byte `struct.pack_into('q', ...)` is a `memcpy`; it is
  not guaranteed to be a single atomic 8-byte store, and there is no alignment guarantee
  on your offset. A reader can observe half of an updated value.
- **False sharing applies across processes.** Two workers updating adjacent counters in
  the same 128-byte line (doc 01) ping-pong that line between cores exactly as two
  threads would. The processes are irrelevant; the cache line is what matters.

**What you can actually do about it, in order of preference:**

1. **Don't synchronize through shared memory at all.** Use it for bulk data with a
   handoff protocol carried on a pipe or socket (§12). The pipe write is a syscall, and a
   syscall is a full barrier — the kernel's own locking orders everything you wrote
   before it. **This is the answer.** It costs one message per batch, not per byte, and
   it makes the whole section moot.
2. **Use a real lock** — `multiprocessing.Lock` — around every access. `sem_wait`/
   `sem_post` are proper acquire/release operations, so this is correct. It costs a
   syscall per access and reintroduces §11.3's crash hazard.
3. **Single writer, single reader, immutable-after-publish.** Write the whole buffer,
   then send its identity over a pipe; the reader never writes and never reads before the
   message. No concurrent access exists, so no ordering question exists.
4. **Drop to C/Cython/Rust** for the atomics if you truly need lock-free shared
   structures. Python cannot express the memory model, so use a language that can.

**The honest summary:** *Python has no memory model, so lock-free shared-memory
algorithms cannot be written correctly in pure Python — only accidentally.* Code that
appears to work is relying on x86's strong ordering, on the coincidence that CPython's
`memoryview` stores are single instructions for its data type, and on timing. It will
break on ARM, and it will break on ARM in production, not in your tests.

---

## 14. The cost model, and a decision table

### 14.1 Where the money goes

For a family-1 message the bill has four line items:

1. **Serialization.** `pickle.dumps` + `loads`. For anything but trivial payloads this
   **dominates everything else** — typically microseconds where the syscalls are
   hundreds of nanoseconds. Doc 09 measured the syscall trap floor on this machine at
   **~81 ns**; a pickle round-trip of a modest dict is one to two orders of magnitude
   more.
2. **Syscalls.** ≥1 `write` + ≥1 `read`, at ~81 ns of pure trap overhead each, plus the
   copy.
3. **Two copies**, user→kernel and kernel→user, at memory bandwidth.
4. **A scheduler wakeup** if the peer was blocked — the largest single item when the
   message rate is low, and doc 06's territory.

For family 2, items 1–4 are all zero per message. What you pay instead is setup
(`shm_open`+`ftruncate`+`mmap`, plus the resource tracker round trip), the 16 KB page
rounding of §7.4, and then the synchronization protocol of §10–§13 — which, if you do it
right per §13.1, means you are paying a family-1 message per *batch*.

**Which gives the actual rule: the crossover is not about speed, it is about the ratio of
bytes to messages.** Family 1's cost scales with payload; family 2's does not. Below a
few tens of KB the fixed costs of family 2 swamp what you saved, and you have taken on
§11.3 and §13 for nothing.

### 14.2 The decision table

| You want to… | Use | Why not the others |
|---|---|---|
| Send work items / results between processes | `multiprocessing.Queue` or `SimpleQueue` | Correct by construction; pickle cost is real but you are not moving MBs |
| Same, but `put()` must mean "sent" | **`SimpleQueue`** | `Queue`'s feeder thread makes `put()` async (§6.4) |
| Move a large array / frame between processes | `SharedMemory` + NumPy view, **handoff over a Queue** | Pickling 1 GB twice is the whole cost; §13.1 pattern 3 |
| Share a large read-only dataset with N workers | **`mmap` a file** (`numpy.memmap`) | Page cache charges the pages once regardless of N; no tracker, no cleanup |
| Share a buffer with **forked** children only | **anonymous `mmap(MAP_SHARED)`** (§7.2) | No name, no tracker, no leak, kernel cleans up |
| Share a small scalar/array with children | `Value`/`Array` | Arena is anonymous already (§8.4); no §8.3 problem |
| Bidirectional messages, one peer each side | `mp.Pipe(duplex=True)` | Socketpair; note the 8 KiB buffer (§3.4) |
| One direction, high volume | **`mp.Pipe(duplex=False)`** | 64 KiB buffer instead of 8 KiB (§6.1) |
| Talk to a non-descendant process | UNIX socket, or `Manager` with an authkey | Neither inheritance nor `SharedMemory` names are safe across trees (§8.3) |
| Hand a socket/file to another process | **`SCM_RIGHTS`** (§4) | The only mechanism that moves a capability |
| Restart without dropping connections | Pass the listening socket via `SCM_RIGHTS` | Nothing else preserves the accept queue |
| Authenticate a local client | UNIX socket + peer credentials | Loopback TCP has no identity and no permissions (§3.2) |
| Wake an event loop from another process | **`eventfd`** (Linux) / self-pipe | Semaphores and futexes are not pollable (§12) |
| Share arbitrary Python objects by reference | `Manager` — and reconsider | Structurally impossible in shared memory (§9); Managers cost 3–4 orders of magnitude |
| Priority-ordered local messages | A socket + a userspace heap | POSIX mqueues: no macOS, no stdlib, tight limits (§5) |
| Anything, on a new design | **Family 1 until measured otherwise** | You cannot accidentally corrupt memory with a pipe |

### 14.3 The pattern that is almost always right

```
producer:                              consumer:
  shm = SharedMemory(create=True,        shm = SharedMemory(name=msg.name,
                     size=nbytes)                           track=False)   # §8.3
  arr = np.ndarray(shape, dtype,         arr = np.ndarray(msg.shape, msg.dtype,
                   buffer=shm.buf)                        buffer=shm.buf)
  arr[:] = payload                       result = work(arr)                # read only
  queue.put(Handle(shm.name, shape,      shm.close()
                   dtype, nbytes))       queue_out.put(result)             # small
  # keep shm alive until the consumer acks, then close() + unlink()
```

Bulk data in family 2, control in family 1. The `queue.put` is the release barrier and
the `queue.get` is the acquire barrier — for free, from the kernel — so §13 evaporates.
The consumer passes `track=False` because it did not create the segment (§8.3). The
producer owns the lifetime and is the only one that ever calls `unlink()`. There is no
cross-process lock, so §11.3 cannot happen.

Every element of that is a lesson from a different section, which is the point.

---

## 15. House rules

**Family 1 (messages)**

1. **Default to it.** You cannot corrupt another process's memory with a pipe.
2. **`send_bytes`/`recv_bytes` when the payload is already bytes.** Skips pickle entirely.
3. **Never call `Queue.empty()` or `qsize()`.** The first lies ~1 in 7 times here
   *(measured, §6.4)*; the second raises `NotImplementedError` on macOS and is racy on
   Linux. Use `get(timeout=…)`/`except Empty`, or a sentinel.
4. **`Queue.put()` is asynchronous.** Close and `join_thread()` before exiting, and never
   `join()` a child before draining its queue.
5. **Budget pipe atomicity at 512 bytes, not 4096**, unless you have checked
   `os.fpathconf` on the target *(measured, §2.3)*.
6. **Never write-then-read a subprocess by hand.** `communicate()` or a selector; the
   64 KiB buffer will deadlock you.
7. **One direction? `Pipe(duplex=False)`** — 64 KiB of buffer instead of 8 KiB.

**Family 2 (shared memory)**

8. **Only fixed-layout bytes go in.** No Python objects, ever, for four independent
   structural reasons (§9).
9. **Prefer anonymous.** `mmap(MAP_SHARED|MAP_ANONYMOUS)` for forked children;
   `memfd_create`+`SCM_RIGHTS` on Linux otherwise. A named segment is a lifetime problem
   you chose to have.
10. **`track=False` when attaching to someone else's segment; `unlink()` exactly once, by
    the creator.** This is [gh-82300](https://github.com/python/cpython/issues/82300) and
    it is still sharp.
11. **Never trust the size you asked for.** Read `shm.size`; store your own payload
    length in the segment. 16 KB granularity here, 4 KB on Linux *(measured, §7.4)*.
12. **Never `ftruncate` down on a mapped segment.** `SIGBUS`, not an exception.
13. **Synchronize with a pipe message, not with a flag in the buffer.** A syscall is a
    barrier; a `memoryview` store is not (§13).
14. **If you must use a cross-process lock: tiny critical sections, no I/O, and a
    watchdog.** A crash under the lock hangs the pool forever with no diagnostic (§11.3).
15. **Pad shared counters to 128 bytes** on this machine. False sharing does not care
    about process boundaries.

**Portability**

16. **Feature-detect by calling, not by `hasattr`.** `socket.SOCK_SEQPACKET` exists here
    and does not work *(measured, §3.1)*.
17. **Assume nothing Linux-only exists:** no `futex`, no `eventfd`, no `memfd_create`, no
    `/dev/shm`, no abstract namespace, no `F_SETPIPE_SZ`, no robust mutexes, no POSIX
    mqueues *(all measured absent, §7.1/§10.2/§12)*.
18. **`Lock.acquire(timeout=…)` polls on macOS** with a 20 ms ceiling; untimed
    `acquire()` does not *(§11.2)*. Do not build a latency-sensitive handoff on the timed
    form.

---

## 16. What I could not verify

Stated plainly, because the alternative is quiet over-claiming:

- **Every Linux-specific mechanism in this document is unmeasured here.** `futex(2)` and
  its whole family, `eventfd`, `memfd_create` and file sealing, `/dev/shm`, the abstract
  socket namespace, `F_SETPIPE_SZ`, `SO_PASSCRED`/`SCM_CREDENTIALS`, `SOCK_SEQPACKET` on
  `AF_UNIX`, robust futexes, POSIX message queues, and `net/unix/garbage.c`. I verified
  their **absence** on this machine and cite `man7.org` for their behaviour. I did not
  boot a Linux box.
- **What macOS actually uses under `pthread_mutex`.** I said `__ulock_wait`/`__ulock_wake`
  and `os_sync_wait_on_address`; that is from Apple's open-source `libplatform`/`libpthread`
  and from the macOS 14 release notes, not from disassembling what CPython links against
  here. The claim "there is no futex syscall on Darwin" is solid; the claim about the
  specific replacement is inherited, not measured.
- **The cost of the `_Py_USE_SEMAPHORES` fallback.** I established from `sysconfig` that
  `HAVE_SEM_TIMEDWAIT = 0` and therefore that CPython's parking lot uses the
  `pthread_mutex`+`pthread_cond` path here rather than POSIX semaphores *(measured)*. I
  did not measure how much slower that is, and I would not guess.
- **The 20 ms polling ceiling's real-world effect.** I read the loop
  (`semaphore.c` L245-300) and the arithmetic is unambiguous, but I did not construct a
  contended `Lock.acquire(timeout=…)` workload and measure the resulting latency
  distribution. The claim "up to 20 ms" is from the source, not from a histogram.
- **No IPC throughput table.** Deliberate, and explained in the provenance block: the
  numbers would be re-derivations of doc 09's syscall floor plus doc 06's wakeup cost.
  The *ratios* in §14.1 are reasoned from those measured components, not independently
  measured end to end. If you need a number for your workload, measure your workload —
  doc 31 explains why anyone else's number would not transfer anyway.
- **macOS pipe capacity dynamics.** I measured a stall at exactly 65,536 bytes with both
  4096-byte and 1-byte writes. Some XNU versions grow a pipe's buffer from 16 KB on
  demand; what I can defend is the observed limit, not the mechanism that produces it.
- **The `Queue.empty()` race rate.** 28/200 and 24/200 are this machine, this load, this
  moment. The *existence* of the race is structural and certain; the frequency is not a
  constant of nature.

---

## 17. You can answer this

- There are only two families of IPC. Name the physical fact that makes it two and not
  three, and give the one thing family 1 gives you for free that family 2 does not.
- A parent writes 1 MB to a child's stdin and reads its stdout. It hangs. Explain with a
  number, and give two fixes.
- What is `PIPE_BUF` for, what is it on Linux, and what is it here? Design a multi-writer
  log protocol that is correct on both.
- Why does `multiprocessing.Pipe(duplex=True)` have 8× less buffer than
  `Pipe(duplex=False)`, and when does that matter?
- Trace `q.put(obj)` from the call to the byte hitting the pipe. Name every lock and
  thread involved, and say why `pickle.dumps` happens *before* the write lock is taken.
- `q.put(1); assert not q.empty()` fails about one time in seven. Why? Now say why
  `SimpleQueue` does not have this problem and what you gave up.
- Give four independent reasons a `list` cannot live in shared memory. Which one is fatal
  even if you fixed the other three?
- What does `shm_unlink` actually remove? Now explain the resource tracker, and why a
  `subprocess` that only *reads* a segment can destroy it.
- `SharedMemory(size=100).size` is not 100. What is it on this machine, what is it on
  Linux, and what breaks if you assume otherwise?
- Explain a futex in two sentences: the operation and the race it closes. Then say what
  `FUTEX_PRIVATE_FLAG` saves and why a process-shared mutex costs more.
- `multiprocessing.Lock` is not a futex and not a `PyMutex`. What is it, and what happens
  to the pool when a worker is `kill -9`'d inside its critical section?
- Why does `Queue.qsize()` raise `NotImplementedError` on macOS but work on Linux — and
  why should you not use it on Linux either?
- You want to wake a process when shared memory changes, and it also has to serve a
  socket. Why can't you use a semaphore? What do you use on Linux, and what do you use
  everywhere?
- Write a shared-memory ready-flag protocol in pure Python. Now explain why it is wrong
  on this CPU and right on x86, and what you should have done instead.
- You need to move a 2 GB NumPy array from a producer to a consumer process 30 times a
  second. Design it, and name the four hazards from this document your design has to
  avoid.
- Your service restarts without dropping a single connection. What kernel mechanism makes
  that possible, and what exactly crosses the socket?

---

## 18. Sources

**Primary — CPython 3.14.6 stdlib** (read locally with line numbers, Aug 2026, from
`cpython-3.14.6-macos-aarch64-none/lib/python3.14/multiprocessing/`):

- `connection.py` — `Pipe` (L566-580), `_send_bytes` (L427-448) and the Nagle branch at
  L438, `_recv_bytes` (L450), `CONNECTION_TIMEOUT` (L45)
- `queues.py` — the "pipe, buffer and thread" header (L30), `__init__` (L37-47),
  `put` (L84-94), `_start_thread` (L173-183), `_feed` (L230-275) and the
  pickle-before-lock ordering at L261-270, the `ignore_epipe` branch (L272-274)
- `shared_memory.py` — `_SHM_SAFE_NAME_LENGTH`/`_make_filename` (L31-46),
  `shm_open`+`ftruncate`+`mmap`+`register` (L92-122), `close` (L225-236),
  `unlink` (L238-254), `ShareableList` layout (L257+)
- `resource_tracker.py` — `_send` and the `PIPE_BUF` reasoning (L345-373),
  `_CLEANUP_FUNCS` (L40-56), `main` and the leak warning (L421-490)
- `reduction.py` — `HAVE_SEND_HANDLE` (L24-27), `sendfds`/`recvfds` (L142-177),
  `DupFd` (L191-200), `resource_sharer` (L243-268)
- `synchronize.py` — `SemLock.__init__` and the fork/spawn unlink split (L49-80),
  `_cleanup` (L84-87), the mixed-context guard (L109), `Lock` (L173-176)
- `heap.py` — `Arena` and the `mkstemp`+`unlink`+`mmap` idiom (L67-95), `_choose_dir`
  and the `/dev/shm` preference (L72-75, L90-96)

**Primary — CPython 3.14 branch on GitHub** (downloaded and read, Aug 2026):

- `Modules/_multiprocessing/semaphore.c` — `SEM_CREATE`→`sem_open` (L225),
  `sem_timedwait_save` polling loop (L241-302), `HAVE_BROKEN_SEM_GETVALUE` paths
  (L414-420, L656-660), `sem_open(name, 0)` re-open (L568)
- `Python/lock.c` — `TIME_TO_BE_FAIR_NS` (L22), `MAX_SPIN_COUNT` 40/0 (L24-31),
  `_PyMutex_LockTimed` (L53-120), `_Py_yield` (L43-51)
- `Python/parking_lot.c` — `_PySemaphore_Init`/`_Wait`/`_Wakeup` and the three platform
  paths
- `Include/internal/pycore_lock.h` — the WTF::Lock credit (L1-5), `_Py_LOCKED` /
  `_Py_HAS_PARKED`
- `Include/internal/pycore_semaphore.h` — the `_Py_USE_SEMAPHORES` condition (L25-29)

**Python documentation**

- [`multiprocessing.shared_memory`](https://docs.python.org/3/library/multiprocessing.shared_memory.html)
  — the `track` parameter's own description of the first-terminating-process failure
- [`multiprocessing`](https://docs.python.org/3/library/multiprocessing.html) —
  `sharedctypes`, the pointer-in-shared-memory warning, `Queue` deadlock warnings
- [`socket`](https://docs.python.org/3/library/socket.html) — `send_fds`/`recv_fds`
  (3.9+), and the note on truncated ancillary integers
- [`mmap`](https://docs.python.org/3/library/mmap.html) — `ACCESS_WRITE`/`ACCESS_COPY`,
  `flush`, `madvise`

**Issues & PEPs**

- [gh-82300](https://github.com/python/cpython/issues/82300) — "resource tracker destroys
  shared memory segments when other processes should still have valid access", and
  [PR #110778](https://github.com/python/cpython/pull/110778) which added `track=`
- [gh-58874](https://github.com/python/cpython/issues/58874) — the macOS fd-passing bug
  behind the ack byte in `sendfds`
- [bpo-20540](https://github.com/python/cpython/issues/64739) — Nagle delay on
  `Connection._send_bytes`, the origin of the 16384 branch
- [PEP 446](https://peps.python.org/pep-0446/) — non-inheritable fds by default; why
  `spawn` must pass handles explicitly
- [PEP 734](https://peps.python.org/pep-0734/) — multiple interpreters in the stdlib;
  `interpreters.Queue` pickles most objects but **shares the underlying `Py_buffer` for
  buffer-protocol objects**, i.e. the same family-1/family-2 split reappears one level
  down. Follow-up in doc 27.

**POSIX / kernel man pages** (man7.org, Aug 2026)

- [`pipe(7)`](https://man7.org/linux/man-pages/man7/pipe.7.html) — capacity, `PIPE_BUF`
  atomicity table, `F_SETPIPE_SZ`, `pipe-user-pages-soft`
- [`unix(7)`](https://man7.org/linux/man-pages/man7/unix.7.html) — the three socket
  types, "datagram sockets are always reliable", `sun_path`, the abstract namespace,
  `SO_SNDBUF`/`SO_RCVBUF` asymmetry, ancillary message types
- [`cmsg(3)`](https://man7.org/linux/man-pages/man3/cmsg.3.html) — `CMSG_SPACE`/`CMSG_LEN`
  and the `SCM_RIGHTS` layout
- [`futex(2)`](https://man7.org/linux/man-pages/man2/futex.2.html) — the wait/wake
  contract, "no explicit initialization or destruction", `FUTEX_PRIVATE_FLAG`,
  `FUTEX_WAIT_BITSET`, the PI family
- [`eventfd(2)`](https://man7.org/linux/man-pages/man2/eventfd.2.html) — the counter,
  `EFD_SEMAPHORE`, `EFD_NONBLOCK`
- [`shm_overview(7)`](https://man7.org/linux/man-pages/man7/shm_overview.7.html) — the
  four-call recipe and the verdict on System V
- [`mmap(2)`](https://man7.org/linux/man-pages/man2/mmap.2.html) — `MAP_SHARED` vs
  `MAP_PRIVATE`, `MAP_ANONYMOUS`, `SIGBUS` on truncation
- [`memfd_create(2)`](https://man7.org/linux/man-pages/man2/memfd_create.2.html) —
  anonymous fds and file sealing
- [`sem_overview(7)`](https://man7.org/linux/man-pages/man7/sem_overview.7.html),
  [`mq_overview(7)`](https://man7.org/linux/man-pages/man7/mq_overview.7.html),
  [`sysvipc(7)`](https://man7.org/linux/man-pages/man7/sysvipc.7.html)

**Other**

- Filip Pizlo, [*Locking in WebKit*](https://webkit.org/blog/6161/locking-in-webkit/) —
  the parking-lot design CPython's `PyMutex` is based on, credited in
  `pycore_lock.h`
- Ulrich Drepper, [*Futexes Are Tricky*](https://www.akkadia.org/drepper/futex.pdf) — the
  canonical treatment of why the compare-and-sleep must be atomic

**Books** (see [BOOKS.md](BOOKS.md) for verdicts)

- Kerrisk, *The Linux Programming Interface* — ch. 44 (pipes/FIFOs), 45–48 (System V
  IPC), 49 (`mmap`), 53–54 (POSIX semaphores), 57 (UNIX domain sockets), 61.13
  (`SCM_RIGHTS`). The reference for this entire document.
- Stevens & Rago, *Advanced Programming in the UNIX Environment* 3e — ch. 15, 17.
- Stevens, *UNIX Network Programming, Vol. 2: Interprocess Communication* — the only
  book-length treatment of family 2 that takes the synchronization seriously.
- OSTEP ch. 30–31 — condition variables and semaphores, for §10.

---

*Next in Tier 1: [`12-*.md`](README.md) — closing out the operating-system tier.*
*Sideways: [`27-multiprocessing-and-subinterpreters.md`](27-multiprocessing-and-subinterpreters.md)
takes §6 and §9 and asks what changes when the "processes" are interpreters in one
address space — where `Py_buffer` sharing (PEP 734) makes family 2 available without any
of §7's setup, and where the refcount argument of §9 becomes the whole design problem.*
