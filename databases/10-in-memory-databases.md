# In-Memory Databases: A Deep Dive

## Executive Summary

In-memory databases store data primarily in RAM, delivering microsecond-level latency versus the millisecond-level latency of disk-based systems. This document covers the internals of Redis, Memcached, Dragonfly, VoltDB, SAP HANA, Aerospike, and distributed caching platforms. It is written for senior/staff engineers who need to make production architecture decisions.

---

## Table of Contents

1. [Why In-Memory Databases](#1-why-in-memory-databases)
2. [Redis Deep Dive](#2-redis-deep-dive)
3. [Memcached](#3-memcached)
4. [Dragonfly](#4-dragonfly)
5. [VoltDB](#5-voltdb)
6. [SAP HANA (In-Memory Aspects)](#6-sap-hana-in-memory-aspects)
7. [Apache Ignite / Hazelcast](#7-apache-ignite--hazelcast)
8. [Aerospike](#8-aerospike)
9. [In-Memory Caching Patterns](#9-in-memory-caching-patterns)
10. [In-Memory Database Design Considerations](#10-in-memory-database-design-considerations)
11. [In-Memory Database Comparison Table](#11-in-memory-database-comparison-table)

---

## 1. Why In-Memory Databases

### The Speed Advantage: RAM vs SSD vs HDD

```
Access Time Comparison (approximate, order of magnitude)
==========================================================

                          Latency          Relative Speed
                        ──────────        ────────────────
  L1 Cache Reference      ~1 ns               1x
  L2 Cache Reference      ~4 ns               4x
  Main Memory (RAM)      ~100 ns             100x
  NVMe SSD Random Read   ~10 us           10,000x
  SATA SSD Random Read   ~100 us         100,000x
  HDD Random Read        ~10 ms       10,000,000x
  Network Round Trip     ~500 us         500,000x

  ┌──────────────────────────────────────────────────────────────┐
  │  If RAM access = 1 second (human scale):                     │
  │    NVMe SSD read  = ~28 hours                                │
  │    SATA SSD read  = ~11 days                                 │
  │    HDD seek       = ~3.2 years                               │
  │    Network RTT    = ~58 days                                 │
  └──────────────────────────────────────────────────────────────┘
```

### Throughput Comparison

| Storage Medium | Random Read IOPS (4KB) | Sequential Read | Sequential Write |
|----------------|----------------------|-----------------|------------------|
| DDR4 RAM       | ~100,000,000         | ~50 GB/s        | ~50 GB/s         |
| NVMe SSD       | ~500,000             | ~3.5 GB/s       | ~3.0 GB/s        |
| SATA SSD       | ~75,000              | ~550 MB/s       | ~520 MB/s        |
| HDD (7200 RPM) | ~100                 | ~200 MB/s       | ~200 MB/s        |

RAM is 200x-1,000,000x faster depending on the access pattern. For workloads that are latency-sensitive and fit within available memory, the performance difference is not incremental -- it is transformational.

### When Data Fits in Memory, Everything Changes

When your entire working set fits in RAM:

1. **No I/O wait** -- queries never block on disk seeks
2. **No buffer pool management** -- no page eviction decisions, no double-buffering
3. **Simpler indexing** -- pointer-based structures (skip lists, hash tables) become practical since random access is cheap
4. **Predictable latency** -- p99 approaches p50 because you eliminate the tail latency caused by disk I/O
5. **Lock-free structures** -- CPU cache-friendly data structures become the bottleneck, not I/O

### Use Cases

| Use Case | Why In-Memory | Typical DB | Example |
|----------|---------------|------------|---------|
| **Caching** | Sub-ms reads for hot data | Redis, Memcached | Cache database query results |
| **Session Management** | Per-request auth lookup must be fast | Redis | Store JWT sessions with TTL |
| **Real-Time Analytics** | Aggregate millions of events/sec | SAP HANA, VoltDB | Dashboard metrics, fraud scoring |
| **Gaming Leaderboards** | Sorted set operations in O(log N) | Redis | Top-100 players globally |
| **Rate Limiting** | Atomic increment + TTL per window | Redis | 1000 req/min per API key |
| **Pub/Sub** | Fan-out messages to subscribers | Redis Streams | Chat, notifications, event bus |
| **Distributed Locks** | Coordination with TTL-based expiry | Redis (Redlock) | Prevent duplicate job execution |
| **Feature Flags** | Low-latency flag evaluation | Redis | Dark launches, A/B tests |
| **Geospatial** | Radius queries on coordinates | Redis (GEO) | Find nearby drivers/restaurants |
| **Time-Series Ingest** | High write throughput for metrics | VoltDB, Redis TS | IoT sensor data |

### Durability Trade-Offs

```
Durability Spectrum
════════════════════════════════════════════════════════════════════

  Pure Cache          Async Persist       Sync Persist       Replicated + Persist
  (ephemeral)         (lose last ~1s)     (lose nothing*)    (survive node loss)
      │                    │                   │                    │
      ▼                    ▼                   ▼                    ▼
  ┌─────────┐        ┌──────────┐        ┌──────────┐        ┌──────────────┐
  │Memcached│        │Redis AOF │        │Redis AOF │        │Redis Cluster │
  │(no disk)│        │everysec  │        │always    │        │+ AOF always  │
  └─────────┘        └──────────┘        └──────────┘        └──────────────┘
  Fastest             Fast + safe-ish     Slower but safe     Safest, most complex
  Data loss on        Lose <=1s data      Lose 0 data on      Tolerate N/2-1
  any restart         on crash            single crash         node failures
```

**Decision framework:**

- **Can you rebuild from source of truth?** Use pure cache (Memcached, Redis no-persist)
- **Is losing 1 second of data acceptable?** Use Redis AOF `everysec`
- **Zero data loss required?** Use Redis AOF `always` + replicas, or a persistent in-memory DB (VoltDB)
- **Must survive datacenter failure?** Use cross-region replication (Redis Enterprise, Aerospike XDR)

---

## 2. Redis Deep Dive

### Architecture

#### Single-Threaded Event Loop

Redis processes all commands on a **single thread**. This is counterintuitive but works because:

1. **All operations are in-memory** -- no I/O blocking, each command completes in microseconds
2. **No lock contention** -- single thread means zero synchronization overhead
3. **Atomicity for free** -- every command is atomic without needing mutexes or transactions
4. **CPU is rarely the bottleneck** -- network I/O and memory bandwidth are the real limits

```
Redis Single-Threaded Event Loop
══════════════════════════════════════════════════════════════════

  ┌───────────────────────────────────────────────────────────┐
  │                    Main Thread (single)                    │
  │                                                           │
  │  ┌─────────────────────────────────────────────────────┐  │
  │  │              Event Loop (ae.c)                       │  │
  │  │                                                     │  │
  │  │   ┌──────────┐   ┌──────────┐   ┌──────────────┐   │  │
  │  │   │  epoll / │   │  Timer   │   │  Before/After│   │  │
  │  │   │  kqueue  │   │  Events  │   │  Sleep Hooks │   │  │
  │  │   │  wait()  │   │ (cron)   │   │              │   │  │
  │  │   └────┬─────┘   └────┬─────┘   └──────┬───────┘   │  │
  │  │        │              │                 │           │  │
  │  │        ▼              ▼                 ▼           │  │
  │  │   ┌────────────────────────────────────────────┐    │  │
  │  │   │          Process Ready Events              │    │  │
  │  │   │                                            │    │  │
  │  │   │  1. Accept new connections                 │    │  │
  │  │   │  2. Read commands from client buffers      │    │  │
  │  │   │  3. Execute command (single-threaded!)     │    │  │
  │  │   │  4. Write responses to output buffers      │    │  │
  │  │   │  5. Handle background task completions     │    │  │
  │  │   └────────────────────────────────────────────┘    │  │
  │  └─────────────────────────────────────────────────────┘  │
  │                                                           │
  │  Background threads (forked processes):                   │
  │  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐    │
  │  │ BGSAVE   │  │ BGREWRITE│  │ Lazy free (Redis 4+) │    │
  │  │ (RDB)    │  │ AOF      │  │ (UNLINK, FLUSHALL    │    │
  │  │          │  │          │  │  ASYNC)               │    │
  │  └──────────┘  └──────────┘  └──────────────────────┘    │
  └───────────────────────────────────────────────────────────┘
```

A single Redis instance can handle **100,000-200,000 commands/sec** for simple operations (GET/SET). The bottleneck is network I/O, not CPU.

#### I/O Multiplexing: epoll / kqueue

Redis uses the OS-specific I/O multiplexing mechanism:

| OS | Multiplexer | How It Works |
|----|-------------|--------------|
| Linux | `epoll` | Kernel maintains interest list, returns only ready FDs |
| macOS/BSD | `kqueue` | Similar to epoll, event-based notification |
| Solaris | `evport` | Event completion framework |
| Fallback | `select` | Scans all FDs (O(n)), limited to 1024 FDs |

```c
// Simplified Redis event loop (ae.c)
void aeMain(aeEventLoop *eventLoop) {
    eventLoop->stop = 0;
    while (!eventLoop->stop) {
        // Before sleep: flush AOF, handle blocked clients, etc.
        if (eventLoop->beforesleep != NULL)
            eventLoop->beforesleep(eventLoop);

        // Block until events are ready (epoll_wait / kevent)
        aeProcessEvents(eventLoop, AE_ALL_EVENTS | AE_CALL_BEFORE_SLEEP);
    }
}
```

#### Redis 6+: Multi-Threaded I/O

Redis 6 introduced **threaded I/O** -- but command execution remains single-threaded:

```
Redis 6+ Threaded I/O Model
══════════════════════════════════════════════════════════════════

  I/O Thread 1 ──┐                              ┌── I/O Thread 1
  I/O Thread 2 ──┤   ┌──────────────────────┐   ├── I/O Thread 2
  I/O Thread 3 ──┼──>│  Read client buffers  │   │
  I/O Thread 4 ──┤   │  (parse commands)     │   │
                 │   └──────────┬─────────────┘  │
                 │              │                 │
                 │              ▼                 │
                 │   ┌──────────────────────┐    │
                 │   │  MAIN THREAD         │    │
                 │   │  Execute commands    │    │
                 │   │  (still single-      │    │
                 │   │   threaded!)          │    │
                 │   └──────────┬────────────┘   │
                 │              │                 │
                 │              ▼                 │
                 │   ┌──────────────────────┐    │
                 ├──>│  Write responses to   │───┤
                 │   │  client buffers       │   │
                 │   └──────────────────────┘    │
                 │                               │
  Threads READ  ─┘                               └─ Threads WRITE
  in parallel                                      in parallel
```

Enable with `io-threads 4` and `io-threads-do-reads yes` in `redis.conf`. Typically provides 2x throughput improvement on multi-core machines with heavy network I/O.

#### Client Handling and Command Pipeline

```
Client Connection Lifecycle
══════════════════════════════════════════════════════════════════

  Client                              Redis Server
    │                                      │
    │──── TCP connect ────────────────────>│  Accept, create client struct
    │                                      │  (allocate query buffer,
    │                                      │   output buffer, flags)
    │                                      │
    │──── AUTH password ──────────────────>│  Authenticate
    │<─── +OK ────────────────────────────│
    │                                      │
    │──── SELECT 2 ───────────────────────>│  Switch to DB 2
    │<─── +OK ────────────────────────────│
    │                                      │
    │  Pipeline (send multiple w/o wait):  │
    │──── SET key1 val1 ──────────────────>│
    │──── SET key2 val2 ──────────────────>│  Queued in input buffer
    │──── GET key1 ───────────────────────>│
    │<─── +OK ────────────────────────────│  All responses sent
    │<─── +OK ────────────────────────────│  together in output
    │<─── $4\r\nval1\r\n ────────────────│  buffer
    │                                      │
    │──── QUIT ───────────────────────────>│  Close connection
    │<─── +OK ────────────────────────────│
```

**Pipelining** reduces network round trips. Instead of N round trips for N commands, you do 1 round trip. This can improve throughput by 5-10x.

### Data Structures (Internal Implementation)

#### Strings: Simple Dynamic String (SDS)

Redis does not use C strings (`char *`). It uses **SDS** (Simple Dynamic String):

```
C String vs SDS
══════════════════════════════════════════════════════════════════

  C String: "Hello"
  ┌───┬───┬───┬───┬───┬────┐
  │ H │ e │ l │ l │ o │ \0 │    Problems:
  └───┴───┴───┴───┴───┴────┘    1. strlen() is O(n)
                                 2. Unsafe: buffer overflow on append
                                 3. Cannot store binary data (\0)
                                 4. Every modification = realloc

  SDS: "Hello"
  ┌──────┬──────┬───────┬───┬───┬───┬───┬───┬────┬───────────┐
  │ len  │ alloc│ flags │ H │ e │ l │ l │ o │ \0 │ (free     │
  │ = 5  │ = 10 │       │   │   │   │   │   │    │  space)   │
  └──────┴──────┴───────┴───┴───┴───┴───┴───┴────┴───────────┘
   header (varies by type)          buf[]

  SDS advantages:
  1. O(1) length lookup (stored in header)
  2. Safe appends (tracks alloc vs len)
  3. Binary-safe (uses len, not \0, for boundaries)
  4. Pre-allocation: alloc doubles up to 1MB, then +1MB
  5. Lazy free: on shrink, space is kept for future use
```

SDS type variants by string length:

| SDS Type | Header Size | Max Length |
|----------|-------------|------------|
| `sdshdr5` | 1 byte | 31 bytes |
| `sdshdr8` | 3 bytes | 255 bytes |
| `sdshdr16` | 5 bytes | 64 KB |
| `sdshdr32` | 9 bytes | 4 GB |
| `sdshdr64` | 17 bytes | 2^64 bytes |

**Pre-allocation strategy:**
```
if new_len < 1MB:
    alloc = new_len * 2       // double the space
else:
    alloc = new_len + 1MB     // grow by 1MB increments
```

#### Lists: Quicklist (Linked List of Listpacks)

Prior to Redis 7.0, lists used a linked list of ziplists. Now they use **quicklist** -- a doubly-linked list where each node contains a **listpack**.

```
Quicklist Structure
══════════════════════════════════════════════════════════════════

  quicklist
  ┌────────────────────────────────────────────────────────────┐
  │  head ─────────────────────────────────────────> tail      │
  │  count: 15 (total entries)                                 │
  │  len: 3 (number of nodes)                                  │
  │  compress: 1 (compress interior nodes with LZF)            │
  └────────────────────────────────────────────────────────────┘
       │                    │                    │
       ▼                    ▼                    ▼
  ┌──────────┐  ◄──►  ┌──────────┐  ◄──►  ┌──────────┐
  │quicklist │        │quicklist │        │quicklist │
  │Node      │        │Node      │        │Node      │
  │          │        │          │        │          │
  │ listpack │        │ listpack │        │ listpack │
  │ [A,B,C,  │        │ [F,G,H,  │        │ [K,L,M,  │
  │  D,E]    │        │  I,J]    │        │  N,O]    │
  │          │        │ COMPRESS │        │          │
  │ (raw)    │        │ (LZF)   │        │ (raw)    │
  └──────────┘        └──────────┘        └──────────┘
   Head: uncompressed  Interior: may be     Tail: uncompressed
                        LZF compressed

  Listpack (replaces ziplist in Redis 7+):
  ┌───────────┬─────────┬─────────┬─────────┬────────┬─────┐
  │total-bytes│entry 1  │entry 2  │entry 3  │  ...   │ END │
  │ (uint32)  │         │         │         │        │0xFF │
  └───────────┴─────────┴─────────┴─────────┴────────┴─────┘

  Each entry:
  ┌──────────────┬──────┬───────────┐
  │encoding+len  │ data │ back-len  │
  │(1-9 bytes)   │      │(1-5 bytes)│
  └──────────────┴──────┴───────────┘
```

**Why quicklist?** It balances memory efficiency (listpacks are contiguous memory, cache-friendly) with O(1) push/pop at both ends (doubly-linked list). Configuration:

- `list-max-listpack-size`: entries per listpack node (default: -2 = 8KB per node)
- `list-compress-depth`: how many nodes at head/tail to leave uncompressed (default: 0 = no compression)

#### Sets: intset -> Hashtable

```
Set Encoding Transitions
══════════════════════════════════════════════════════════════════

  Small set of integers              Large set or non-integer values
  ┌──────────────────────┐           ┌──────────────────────────┐
  │       intset          │           │        hashtable          │
  │                      │           │                          │
  │  encoding: int16     │  ──────>  │  dict (hash table)       │
  │  length: 5           │  when:    │  key = member            │
  │  contents:           │  - >128   │  val = NULL              │
  │  [2, 7, 11, 42, 99]  │   members │                          │
  │                      │  - non-   │  Uses MurmurHash2        │
  │  Sorted, binary      │   integer │  Incremental rehash      │
  │  search O(log n)     │   added   │  Two tables for resize   │
  └──────────────────────┘           └──────────────────────────┘
```

**intset** stores integers in a sorted array with compact encoding (int16, int32, or int64, auto-upgrading). Thresholds configured via `set-max-intset-entries` (default: 512).

#### Sorted Sets: Skip List + Hashtable

Redis sorted sets (ZSET) use two data structures simultaneously:

```
Sorted Set Dual Structure
══════════════════════════════════════════════════════════════════

  ZADD leaderboard 100 "alice" 200 "bob" 150 "charlie"

  ┌────────────────────────────────────────────────────────────┐
  │                     Sorted Set (zset)                       │
  │                                                            │
  │   ┌──────────────┐          ┌──────────────────────────┐   │
  │   │  Skip List    │          │     Hash Table            │   │
  │   │              │          │                          │   │
  │   │  Sorted by   │          │  O(1) score lookup       │   │
  │   │  score       │          │  by member               │   │
  │   │              │          │                          │   │
  │   │  O(log n)    │          │  "alice"   -> 100        │   │
  │   │  insert,     │          │  "bob"     -> 200        │   │
  │   │  delete,     │          │  "charlie" -> 150        │   │
  │   │  range       │          │                          │   │
  │   └──────────────┘          └──────────────────────────┘   │
  └────────────────────────────────────────────────────────────┘

  Skip List Internals:
  ═══════════════════

  Level 3: HEAD ──────────────────────────────────────> 200(bob) ──> NIL
                                                          │
  Level 2: HEAD ────────────> 100(alice) ──────────────> 200(bob) ──> NIL
                                  │                       │
  Level 1: HEAD ────────────> 100(alice) ──> 150(charlie)──> 200(bob) ──> NIL
                                  │              │           │
  Level 0: HEAD ──> 100(alice) ──> 150(charlie) ──> 200(bob) ──> NIL
             (base level: all elements, sorted by score)

  Each node:
  ┌──────────┬───────┬────────────────────────────┐
  │  member  │ score │ levels[]                    │
  │  (SDS)   │(double)│ [forward_ptr, span] * N   │
  └──────────┴───────┴────────────────────────────┘

  span = number of nodes skipped at this level (used for ZRANK)
```

**Why skip list instead of balanced tree (AVL/Red-Black)?**

| Property | Skip List | Balanced Tree |
|----------|-----------|---------------|
| Implementation complexity | Simple | Complex (rotations) |
| Range queries | Natural (follow forward pointers) | Requires in-order traversal |
| Concurrent modification | Easier lock-free versions | Complex rebalancing |
| Memory locality | Similar | Similar |
| Average time complexity | O(log n) | O(log n) |
| Code size (Redis) | ~350 lines | Would be ~500+ lines |

Antirez (Redis creator) chose skip lists because they are simpler to implement, debug, and modify, while offering the same asymptotic performance. The span field in each level makes `ZRANK` an O(log n) operation.

**Small sorted sets** use a listpack encoding when both conditions hold (configurable):
- `zset-max-listpack-entries` <= 128
- `zset-max-listpack-value` <= 64 bytes

#### Hashes: Listpack -> Hashtable

```
Hash Encoding Transitions
══════════════════════════════════════════════════════════════════

  Small hash (listpack)                    Large hash (hashtable)
  ┌─────────────────────────┐              ┌─────────────────────────┐
  │  Flat key-value pairs   │              │  dict structure          │
  │  in contiguous memory   │    ──────>   │  Two hash tables         │
  │                         │    when:     │  (ht[0] active,          │
  │  [k1][v1][k2][v2]...   │    >128      │   ht[1] for rehash)      │
  │                         │    entries   │                         │
  │  O(n) lookup but fast   │    or val    │  O(1) avg lookup         │
  │  for small n due to     │    >64 bytes │  Incremental rehash      │
  │  cache locality         │              │                         │
  └─────────────────────────┘              └─────────────────────────┘
```

**Incremental rehashing:** When a hashtable needs to resize, Redis does NOT rehash all entries at once (that would block the event loop). Instead:

1. Allocate new table `ht[1]` (2x size for grow, or 1/2 for shrink)
2. On every dict operation (GET, SET, DEL), rehash **one bucket** from `ht[0]` to `ht[1]`
3. New writes go to `ht[1]`; reads check both tables
4. When `ht[0]` is empty, swap pointers: `ht[0] = ht[1]`

This spreads the rehash cost across many operations, keeping latency consistent.

#### Streams: Radix Tree + Listpacks

```
Redis Stream Architecture
══════════════════════════════════════════════════════════════════

  XADD mystream * sensor_id 1234 temp 72.5

  Stream structure:
  ┌──────────────────────────────────────────────────────────┐
  │  stream                                                   │
  │  ┌─────────────────────────────────┐                     │
  │  │  Radix Tree (rax)               │                     │
  │  │                                 │                     │
  │  │  Key: stream entry ID           │                     │
  │  │       (milliseconds-sequence)   │                     │
  │  │                                 │                     │
  │  │  ┌───────────┐                  │                     │
  │  │  │ 1695000000│ (ms prefix)      │                     │
  │  │  │    │      │                  │                     │
  │  │  │    ├─ 000 ──> listpack:      │                     │
  │  │  │    │          [id1, fields]   │                     │
  │  │  │    │          [id2, fields]   │                     │
  │  │  │    │          [id3, fields]   │                     │
  │  │  │    │          (master entry   │                     │
  │  │  │    │           + entries)     │                     │
  │  │  │    │                         │                     │
  │  │  │    └─ 001 ──> listpack:      │                     │
  │  │  │               [id4, fields]  │                     │
  │  │  └───────────┘                  │                     │
  │  └─────────────────────────────────┘                     │
  │                                                           │
  │  Consumer Groups:                                         │
  │  ┌──────────────────────────────────────────────────┐    │
  │  │  Group: "processors"                              │    │
  │  │  last_delivered_id: 1695000000-003                │    │
  │  │                                                    │    │
  │  │  Consumers:                                        │    │
  │  │  ┌──────────────────────────────────────────┐     │    │
  │  │  │  consumer "worker-1"                      │     │    │
  │  │  │    PEL (Pending Entry List):              │     │    │
  │  │  │    [1695000000-001, delivered_at, count]   │     │    │
  │  │  │    [1695000000-002, delivered_at, count]   │     │    │
  │  │  └──────────────────────────────────────────┘     │    │
  │  │  ┌──────────────────────────────────────────┐     │    │
  │  │  │  consumer "worker-2"                      │     │    │
  │  │  │    PEL:                                   │     │    │
  │  │  │    [1695000000-003, delivered_at, count]   │     │    │
  │  │  └──────────────────────────────────────────┘     │    │
  │  └──────────────────────────────────────────────────┘    │
  └──────────────────────────────────────────────────────────┘
```

**Consumer group commands:**
```bash
# Create a consumer group
XGROUP CREATE mystream processors $ MKSTREAM

# Read as consumer (blocks if no new messages)
XREADGROUP GROUP processors worker-1 COUNT 10 BLOCK 2000 STREAMS mystream >

# Acknowledge processing
XACK mystream processors 1695000000-001

# Claim stuck messages (consumer died)
XAUTOCLAIM mystream processors worker-2 60000 0-0 COUNT 10
```

#### Bitmaps, HyperLogLog, Geospatial

| Structure | Underlying Type | Key Operations | Use Case |
|-----------|----------------|----------------|----------|
| **Bitmap** | String (SDS) | SETBIT, GETBIT, BITCOUNT, BITOP | Daily active users, feature flags |
| **HyperLogLog** | String (12KB max) | PFADD, PFCOUNT, PFMERGE | Unique visitor counts (0.81% error) |
| **Geospatial** | Sorted Set | GEOADD, GEODIST, GEOSEARCH | Nearby locations, radius queries |

**HyperLogLog** uses 16384 registers, each 6 bits = 12288 bytes. When there are few elements, Redis uses a **sparse encoding** that is much smaller. It auto-promotes to the dense representation.

**Geospatial** indexes encode latitude/longitude into a 52-bit geohash, then store it as the score in a sorted set. `GEOSEARCH` computes the bounding box of a circle/rectangle, queries the sorted set range, and filters by exact distance.

### Persistence

#### RDB (Redis Database Backup)

```
RDB Snapshot Process (BGSAVE)
══════════════════════════════════════════════════════════════════

  Main Process                    Child Process (fork)
  ┌──────────────────┐           ┌──────────────────────┐
  │                  │           │                      │
  │  Continues       │  fork()   │  Iterates all keys   │
  │  serving         │──────────>│  in all databases    │
  │  clients         │           │                      │
  │                  │           │  Serializes to        │
  │  Writes trigger  │           │  temp-<pid>.rdb      │
  │  copy-on-write   │           │                      │
  │  page faults     │           │  Writes: type, key,  │
  │                  │           │  TTL, value encoding  │
  │  Memory usage    │           │                      │
  │  may temporarily │           │  On complete:        │
  │  double (worst   │           │  rename to dump.rdb  │
  │  case)           │           │                      │
  └──────────────────┘           └──────────────────────┘

  Copy-on-Write (COW):
  ┌───────────────────────────────────────────────────────────┐
  │ Before fork:                                               │
  │   Parent page table ──> [Page A] [Page B] [Page C]        │
  │                                                            │
  │ After fork:                                                │
  │   Parent page table ──┐                                    │
  │                       ├──> [Page A] [Page B] [Page C]     │
  │   Child page table  ──┘    (shared, read-only)            │
  │                                                            │
  │ When parent writes to Page B:                              │
  │   Parent page table ──> [Page A] [Page B'] [Page C]       │
  │                              │              │              │
  │   Child page table  ──> [Page A] [Page B]  [Page C]       │
  │                         (shared) (original) (shared)       │
  │                                                            │
  │ Only modified pages are copied. If workload is read-heavy, │
  │ COW overhead is minimal. Write-heavy = more page copies.   │
  └───────────────────────────────────────────────────────────┘
```

**RDB file format:**
```
┌─────────┬─────────┬─────────────┬────────┬────────────┬─────┬──────────┐
│ "REDIS"  │ version │ aux fields  │ DB sel │ key-value  │ ... │ checksum │
│ (magic)  │ "0011"  │ (metadata)  │ 0xFE+n │ pairs      │     │ CRC64    │
└─────────┴─────────┴─────────────┴────────┴────────────┴─────┴──────────┘
```

#### AOF (Append Only File)

```
AOF Write Path
══════════════════════════════════════════════════════════════════

  Client command: SET user:1 "alice"
       │
       ▼
  ┌─────────────────────────────────────────┐
  │ 1. Execute command (modify in-memory)    │
  │ 2. Append to AOF buffer (server.aof_buf) │
  │ 3. Before returning to event loop:       │
  │    - write() to AOF file (kernel buffer) │
  │ 4. Based on fsync policy:                │
  │    - always:   fsync() now (safest)      │
  │    - everysec: fsync() in bg thread/sec  │
  │    - no:       let OS flush when ready   │
  └─────────────────────────────────────────┘

  AOF file contents (RESP protocol):
  ────────────────────────────────────
  *3\r\n              (array of 3 elements)
  $3\r\n              (bulk string, 3 bytes)
  SET\r\n
  $6\r\n              (bulk string, 6 bytes)
  user:1\r\n
  $5\r\n              (bulk string, 5 bytes)
  alice\r\n
```

**fsync policies compared:**

| Policy | Data Loss Window | Throughput Impact | When to Use |
|--------|-----------------|-------------------|-------------|
| `always` | 0 (no loss) | Highest overhead (fsync per write) | Financial transactions |
| `everysec` | Up to 1 second | Minimal (~2% overhead) | Default, good balance |
| `no` | OS-dependent (up to 30s on Linux) | No overhead from Redis | Pure cache, data is expendable |

**AOF Rewrite (compaction):**
```
Before rewrite (AOF has redundant commands):
  SET counter 1
  INCR counter        # counter = 2
  INCR counter        # counter = 3
  INCR counter        # counter = 4
  SET name "alice"
  SET name "bob"      # overwrites alice

After BGREWRITEAOF:
  SET counter 4       # single command captures final state
  SET name "bob"      # only final value
```

The rewrite is done by a forked child process. New commands during rewrite go to both the old AOF and a rewrite buffer. When the child finishes, the rewrite buffer is appended, and the new AOF replaces the old one.

#### RDB + AOF Hybrid (Redis 4.0+)

```
Hybrid Persistence (aof-use-rdb-preamble yes)
══════════════════════════════════════════════════════════════════

  ┌─────────────────────────────────────────────────────────┐
  │                     AOF File                              │
  │                                                          │
  │  ┌────────────────────────────────────────────────────┐  │
  │  │  RDB Preamble (binary, compact snapshot)           │  │
  │  │  - Full database state at time of rewrite          │  │
  │  │  - Fast to load                                    │  │
  │  └────────────────────────────────────────────────────┘  │
  │  ┌────────────────────────────────────────────────────┐  │
  │  │  AOF Tail (RESP text, incremental commands)        │  │
  │  │  - Commands received after RDB snapshot            │  │
  │  │  - Provides sub-second durability                  │  │
  │  └────────────────────────────────────────────────────┘  │
  └─────────────────────────────────────────────────────────┘

  Benefit: Fast restart (RDB load) + minimal data loss (AOF tail)
```

#### Persistence Strategy Decision

| Scenario | Recommended Strategy | Why |
|----------|---------------------|-----|
| Pure cache | No persistence | Fastest, data is rebuildable |
| Cache with warm restart | RDB only | Fast restart, some data loss OK |
| General purpose | AOF `everysec` + RDB | Good balance of safety and speed |
| Financial / critical data | AOF `always` + replicas | Maximum durability |
| Large datasets, fast restart | Hybrid RDB+AOF | Fast load + minimal loss |

### Memory Management

#### jemalloc

Redis uses **jemalloc** as its memory allocator (not glibc malloc). jemalloc provides:

- Thread-local caches (less contention)
- Size-class-based allocation (reduces fragmentation)
- Arenas for parallelism
- Detailed introspection (`MEMORY DOCTOR`, `MEMORY STATS`)

**Memory fragmentation ratio:**
```
mem_fragmentation_ratio = used_memory_rss / used_memory

  < 1.0:  Redis is using swap (BAD -- severe performance degradation)
  1.0-1.5: Normal, healthy fragmentation
  > 1.5:  Significant fragmentation, wasting memory
  > 2.0:  Severe fragmentation, consider MEMORY PURGE or restart
```

Redis 4+ has **active defragmentation** (`activedefrag yes`) that moves allocations to reduce fragmentation without restart.

#### Eviction Policies

When `maxmemory` is reached, Redis evicts keys based on the configured policy:

```
Eviction Policy Decision Tree
══════════════════════════════════════════════════════════════════

  maxmemory reached?
       │
       ▼
  ┌─────────────────────────────────────────────────────────┐
  │  noeviction:  Return error on writes (default)          │
  │                                                         │
  │  allkeys-lru:    Evict least recently used, any key     │
  │  volatile-lru:   Evict LRU, only keys with TTL          │
  │                                                         │
  │  allkeys-lfu:    Evict least frequently used, any key   │
  │  volatile-lfu:   Evict LFU, only keys with TTL          │
  │                                                         │
  │  allkeys-random: Evict random key                       │
  │  volatile-random:Evict random key with TTL              │
  │                                                         │
  │  volatile-ttl:   Evict key with shortest TTL remaining  │
  └─────────────────────────────────────────────────────────┘
```

**Approximated LRU:** Redis does NOT maintain a true LRU linked list (too much memory overhead). Instead, it samples `maxmemory-samples` keys (default: 5) and evicts the one with the oldest last-access time. With 10 samples, it closely approximates true LRU.

**LFU implementation (Redis 4+):** Uses a **Morris counter** -- a probabilistic logarithmic counter stored in 8 bits:

```
LFU Counter (stored in 24-bit lru field of redisObject)
══════════════════════════════════════════════════════════════════

  ┌──────────────────────┬──────────────────┐
  │  16 bits: ldt        │  8 bits: counter │
  │  (last decrement     │  (log frequency) │
  │   time, minutes      │  (0-255)         │
  │   resolution)        │                  │
  └──────────────────────┴──────────────────┘

  Counter increment logic (Morris counter):
  ──────────────────────────────────────────
  double r = (double)rand() / RAND_MAX;
  double baseval = counter - LFU_INIT_VAL;  // LFU_INIT_VAL = 5
  if (baseval < 0) baseval = 0;
  double p = 1.0 / (baseval * lfu_log_factor + 1);
  if (r < p) counter++;

  With lfu_log_factor = 10 (default):
    Counter 0-5:   Increments easily (new keys start at 5)
    Counter 10:    ~1 million accesses
    Counter 100:   Saturates at ~10 million accesses
    Counter 255:   Maximum (never evicted by LFU)

  Decay: counter is halved every lfu_decay_time minutes (default: 1)
```

#### Key Expiration

Redis uses two mechanisms to expire keys:

1. **Lazy expiration:** On every key access, check if expired. If yes, delete it. Cost: O(1) per access.

2. **Active expiration:** A periodic task (default: 10 Hz) that:
   ```
   repeat:
       1. Sample 20 keys from the expires dict
       2. Delete all that are expired
       3. If >25% were expired, repeat immediately
       4. Otherwise, wait until next cycle
   ```
   This is time-limited to avoid blocking the event loop (25% of CPU time max per cycle).

#### Memory Optimization Tips

| Technique | Savings | How |
|-----------|---------|-----|
| Use hashes for small objects | 10x less memory | `HSET user:1 name alice age 30` vs separate keys |
| Shared integer pool | 8 bytes per integer key | Redis caches integers 0-9999 as shared objects |
| Short key names | Direct savings | `u:1:n` vs `user:1:name` (use judiciously) |
| Listpack thresholds | Keep small collections compact | Tune `*-max-listpack-entries`, `*-max-listpack-value` |
| OBJECT ENCODING | Identify bloated keys | Check if keys are using expected encoding |
| Enable compression | ~50-70% savings for lists | `list-compress-depth 1` (compress interior quicklist nodes) |

### Replication and High Availability

#### Master-Replica Replication

```
Redis Replication Flow
══════════════════════════════════════════════════════════════════

  Initial Sync (full resynchronization):
  ┌────────────┐                          ┌────────────┐
  │   Master   │                          │  Replica   │
  │            │ <── PSYNC ? -1 ─────────│            │
  │            │                          │            │
  │  BGSAVE    │ ── +FULLRESYNC          │            │
  │  (fork)    │    <replid> <offset> ──>│            │
  │            │                          │            │
  │  Send RDB  │ ── RDB file ──────────> │ Load RDB   │
  │  file      │                          │ into memory│
  │            │                          │            │
  │  Repl.     │ ── Buffered commands ──>│ Apply      │
  │  buffer    │    during RDB send      │ backlog    │
  │            │                          │            │
  │            │ ── Ongoing stream ─────>│ Real-time  │
  │            │    (replication stream)  │ replication│
  └────────────┘                          └────────────┘

  Partial Resync (after brief disconnect):
  ┌────────────┐                          ┌────────────┐
  │   Master   │                          │  Replica   │
  │            │ <── PSYNC <replid>       │            │
  │  Backlog   │         <offset> ───────│            │
  │  buffer    │                          │            │
  │  (1MB def.)│ ── +CONTINUE ──────────>│            │
  │            │                          │            │
  │            │ ── Missing commands ───>│ Apply      │
  │            │    from backlog         │ commands   │
  └────────────┘                          └────────────┘

  Key parameter: repl-backlog-size (default 1MB)
  - Too small: frequent full resyncs after network blips
  - Recommended: estimate write rate * expected disconnect time
```

#### Redis Sentinel

```
Redis Sentinel Architecture
══════════════════════════════════════════════════════════════════

  ┌──────────┐     ┌──────────┐     ┌──────────┐
  │Sentinel 1│     │Sentinel 2│     │Sentinel 3│
  │ (monitor)│◄───►│ (monitor)│◄───►│ (monitor)│
  └────┬─────┘     └────┬─────┘     └────┬─────┘
       │                │                │
       │    Gossip protocol (Sentinel)   │
       │    (share state, vote, elect)   │
       │                │                │
       ▼                ▼                ▼
  ┌──────────┐     ┌──────────┐     ┌──────────┐
  │  Master  │────>│ Replica 1│     │ Replica 2│
  │          │────>│          │     │          │
  └──────────┘     └──────────┘     └──────────┘

  Failover Process:
  ─────────────────
  1. Sentinel detects master down (SDOWN - subjective)
  2. Sentinel asks other Sentinels to confirm (ODOWN - objective)
     Requires quorum (e.g., 2 of 3 Sentinels agree)
  3. Sentinels elect a leader (Raft-like algorithm)
  4. Leader selects best replica:
     - Highest priority (replica-priority)
     - Most data (largest replication offset)
     - Lexicographically smallest runid
  5. Leader promotes replica to master (REPLICAOF NO ONE)
  6. Other replicas reconfigured to follow new master
  7. Old master reconfigured as replica when it comes back
  8. Clients notified via Sentinel pub/sub
```

**Sentinel configuration:**
```
sentinel monitor mymaster 192.168.1.10 6379 2    # quorum = 2
sentinel down-after-milliseconds mymaster 5000    # 5s to detect down
sentinel failover-timeout mymaster 60000          # 60s failover timeout
sentinel parallel-syncs mymaster 1                # 1 replica syncs at a time
```

### Redis Cluster Deep Dive

```
Redis Cluster Architecture
══════════════════════════════════════════════════════════════════

  16384 Hash Slots distributed across masters:

  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
  │  Master A        │  │  Master B        │  │  Master C        │
  │  Slots: 0-5460   │  │  Slots: 5461-    │  │  Slots: 10923-   │
  │                  │  │         10922    │  │         16383    │
  │  ┌─────────────┐│  │  ┌─────────────┐│  │  ┌─────────────┐│
  │  │ Replica A1  ││  │  │ Replica B1  ││  │  │ Replica C1  ││
  │  └─────────────┘│  │  └─────────────┘│  │  └─────────────┘│
  │  ┌─────────────┐│  │  ┌─────────────┐│  │  ┌─────────────┐│
  │  │ Replica A2  ││  │  │ Replica B2  ││  │  │ Replica C2  ││
  │  └─────────────┘│  │  └─────────────┘│  │  └─────────────┘│
  └─────────────────┘  └─────────────────┘  └─────────────────┘
         │                    │                    │
         └────────────────────┼────────────────────┘
                              │
                    Cluster Bus (port + 10000)
                    Gossip protocol: PING/PONG
                    every node talks to every other
```

#### Hash Slot Assignment

```python
# How Redis determines which node owns a key:
slot = CRC16(key) % 16384

# Example:
CRC16("user:1000") % 16384 = 7438  # -> Master B (slots 5461-10922)
CRC16("order:555")  % 16384 = 2901  # -> Master A (slots 0-5460)
```

#### MOVED and ASK Redirections

```
MOVED Redirection (permanent slot migration):
══════════════════════════════════════════════════════════════════

  Client                Node A              Node B
    │                     │                    │
    │── GET user:100 ───>│                    │
    │                     │ (slot not here)    │
    │<─ -MOVED 7438      │                    │
    │   192.168.1.2:6379 │                    │
    │                     │                    │
    │── GET user:100 ──────────────────────>  │
    │<─ "alice" ────────────────────────────  │
    │                                          │
    │ (Client updates slot→node mapping cache) │


ASK Redirection (slot being migrated):
══════════════════════════════════════════════════════════════════

  During MIGRATE of slot 7438 from Node A to Node B:

  Client                Node A              Node B
    │                     │                    │
    │── GET user:100 ───>│                    │
    │                     │ (key already       │
    │                     │  migrated)         │
    │<─ -ASK 7438        │                    │
    │   192.168.1.2:6379 │                    │
    │                     │                    │
    │── ASKING ─────────────────────────────> │
    │── GET user:100 ──────────────────────>  │
    │<─ "alice" ────────────────────────────  │
    │                                          │
    │ (Client does NOT update cache -- ASK is  │
    │  temporary, migration may not be done)   │
```

#### Hash Tags for Cross-Slot Operations

```bash
# Problem: MGET on keys in different slots fails in cluster mode
MGET user:1 user:2 user:3   # ERROR: keys in different slots

# Solution: Hash tags -- only the content inside {} is hashed
MGET {user}:1 {user}:2 {user}:3   # All hash to CRC16("user") -- same slot

# Use cases for hash tags:
SET {order:123}:items "[...]"
SET {order:123}:status "shipped"
SET {order:123}:payment "paid"
# All order:123 data in same slot -> can use MULTI/EXEC transaction
```

#### Resharding Without Downtime

```
Live Resharding Process (moving slot 7438: A -> B)
══════════════════════════════════════════════════════════════════

  1. CLUSTER SETSLOT 7438 MIGRATING B     (on Node A)
  2. CLUSTER SETSLOT 7438 IMPORTING A     (on Node B)

  3. For each key in slot 7438 on Node A:
     MIGRATE B_host B_port key 0 timeout REPLACE

     During migration:
     - Reads for unmigrated keys: served by A
     - Reads for migrated keys: A returns ASK -> client goes to B
     - Writes to A for slot 7438: if key exists, serve; else ASK B

  4. CLUSTER SETSLOT 7438 NODE B          (on all nodes)
     - Slot ownership transfers to B
     - MOVED responses from now on
```

#### Split-Brain Handling

Redis Cluster uses `cluster-node-timeout` (default 15s) and majority-based failure detection:

1. Node A cannot reach Node B for `cluster-node-timeout` ms -> marks B as `PFAIL` (possible failure)
2. A gossips PFAIL to other nodes
3. If majority of masters report B as PFAIL within 2x timeout window -> B marked as `FAIL`
4. B's replica gets promoted

**`cluster-require-full-coverage`** (default: yes):
- `yes`: cluster stops accepting writes if any slot is uncovered (no master + replica)
- `no`: cluster continues serving available slots (partial availability)

**Minimum viable cluster:** 3 masters + 3 replicas = 6 nodes. Can survive 1 master failure. For 2 master failures tolerance, use 3 masters with 2 replicas each = 9 nodes.

---

## 3. Memcached

### Architecture

```
Memcached Architecture (Multi-Threaded)
══════════════════════════════════════════════════════════════════

  ┌───────────────────────────────────────────────────────────┐
  │                    Memcached Process                        │
  │                                                           │
  │  ┌──────────────┐                                         │
  │  │ Listener     │  Accepts connections, dispatches to      │
  │  │ Thread       │  worker threads via round-robin          │
  │  └──────┬───────┘                                         │
  │         │                                                  │
  │         ├──────────────┬──────────────┬───────────────┐   │
  │         ▼              ▼              ▼               ▼   │
  │  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌─────────┐│
  │  │ Worker 1  │  │ Worker 2  │  │ Worker 3  │  │Worker N ││
  │  │           │  │           │  │           │  │         ││
  │  │ libevent  │  │ libevent  │  │ libevent  │  │libevent ││
  │  │ event loop│  │ event loop│  │ event loop│  │evt loop ││
  │  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘  └────┬────┘│
  │        │              │              │              │     │
  │        └──────────────┼──────────────┼──────────────┘     │
  │                       ▼                                    │
  │            ┌──────────────────────┐                       │
  │            │  Shared Hash Table   │  Global lock per       │
  │            │  + Slab Allocator    │  item (fine-grained)   │
  │            └──────────────────────┘                       │
  └───────────────────────────────────────────────────────────┘
```

Unlike Redis, Memcached is **multi-threaded from the start**. This means it can utilize all CPU cores for both I/O and command execution. However, it requires fine-grained locking on the hash table and slab allocator.

### Slab Allocator

Memcached avoids memory fragmentation by pre-allocating memory in **slabs** of fixed-size chunks:

```
Slab Allocator
══════════════════════════════════════════════════════════════════

  Memory is divided into slab classes by chunk size:

  Slab Class 1:  chunk size = 96 bytes
  ┌──────┬──────┬──────┬──────┬──────┬──────┬──────────────┐
  │ 96B  │ 96B  │ 96B  │ 96B  │ 96B  │ 96B  │    ...       │
  │(used)│(used)│(free)│(used)│(free)│(used)│              │
  └──────┴──────┴──────┴──────┴──────┴──────┴──────────────┘

  Slab Class 2:  chunk size = 120 bytes
  ┌────────┬────────┬────────┬────────┬────────────────────┐
  │  120B  │  120B  │  120B  │  120B  │       ...          │
  │ (used) │ (free) │ (used) │ (free) │                    │
  └────────┴────────┴────────┴────────┴────────────────────┘

  Slab Class 3:  chunk size = 152 bytes
  ...

  Slab Class 42: chunk size = 1 MB  (max item size, configurable)

  Growth factor (default 1.25):
  96 -> 120 -> 152 -> 192 -> 240 -> 304 -> ... -> 1MB

  Item stored in the SMALLEST slab class that fits:
  ┌──────────────────────────────┐
  │ Item (key + value + metadata)│
  │ = 85 bytes                   │
  │ -> stored in class 1 (96B)  │
  │ -> 11 bytes wasted (11.5%)  │
  └──────────────────────────────┘
```

**Slab rebalancing:** Memcached 1.6+ can move slabs between classes (`slab_reassign` and `slab_automove`). Without this, if slab class 5 is full but class 10 has free space, class 5 items are evicted even though total memory is available.

### LRU Per Slab Class

Each slab class has its own LRU list. Memcached 1.5+ uses a **segmented LRU**:

```
Segmented LRU (per slab class)
══════════════════════════════════════════════════════════════════

  HOT  ──> items recently inserted (stays here briefly)
   │
   ▼
  WARM ──> items accessed at least once since entering WARM
   │
   ▼
  COLD ──> items not recently accessed (eviction candidates)
   │
   ▼
  TEMP ──> items with very short TTL (evicted first, never promoted)

  Eviction order: TEMP first, then COLD tail, then WARM tail
```

### Consistent Hashing

Memcached itself has **no built-in distribution**. The client decides which server to route to. Most clients use **consistent hashing**:

```
Consistent Hash Ring
══════════════════════════════════════════════════════════════════

              Server A (150 vnodes)
               ╱    ╲
              ╱      ╲
  Server D   ╱        ╲   Server B
  (150     ●──●──●──●──●  (150
  vnodes)  │  hash ring  │  vnodes)
           ●──●──●──●──●
             ╲        ╱
              ╲      ╱
               ╲    ╱
              Server C (150 vnodes)

  key "session:abc" -> hash -> lands between Server B and Server C
                    -> routes to Server C (next clockwise server)

  Adding Server E: only keys between D and E rehash (minimal disruption)
  Removing Server B: only B's keys rehash to next server (C)

  Virtual nodes: each server gets ~150 points on ring for even distribution
```

### Text Protocol vs Binary Protocol

| Feature | Text Protocol | Binary Protocol |
|---------|--------------|-----------------|
| Human readable | Yes (`get key\r\n`) | No (binary header) |
| Parsing overhead | Higher (string parsing) | Lower (fixed header) |
| Bandwidth | More bytes | More compact |
| Error handling | Text error messages | Error codes |
| CAS support | Yes (gets/cas) | Yes (native) |
| Recommended | Development/debugging | Production |

### When Memcached Beats Redis

| Scenario | Winner | Why |
|----------|--------|-----|
| Simple key-value cache, multi-core | **Memcached** | True multi-threading, scales on cores |
| Data structures (lists, sets, sorted sets) | **Redis** | Memcached only has strings |
| Persistence needed | **Redis** | Memcached has no persistence |
| Large values (>1MB) | **Memcached** | Configurable max item size, slab-efficient |
| Pure read-heavy cache, many cores | **Memcached** | Better multi-core utilization |
| Pub/sub, streams, scripting | **Redis** | Memcached has none of these |
| Memory efficiency (many small keys) | **Redis** | Compact encodings (listpack, intset) |

### Extstore: SSD-Backed Memcached

Memcached 1.5.4+ supports **extstore** -- storing values on SSD while keeping keys + metadata in RAM:

```
Extstore Architecture
══════════════════════════════════════════════════════════════════

  ┌────────────────────────────────────────────┐
  │               RAM                           │
  │  ┌───────────────────────────────────────┐ │
  │  │  Hash table: key -> metadata          │ │
  │  │  (key, flags, TTL, SSD page pointer)  │ │
  │  │                                       │ │
  │  │  Hot items: full item in RAM          │ │
  │  │  Cold items: metadata only in RAM     │ │
  │  └───────────────────────────────────────┘ │
  └──────────────────────┬─────────────────────┘
                         │ cold items
                         ▼
  ┌────────────────────────────────────────────┐
  │               SSD (extstore)                │
  │  ┌───────────────────────────────────────┐ │
  │  │  Page-based storage                   │ │
  │  │  Values stored contiguously           │ │
  │  │  Read on cache miss (async I/O)       │ │
  │  └───────────────────────────────────────┘ │
  └────────────────────────────────────────────┘

  Benefit: 10-20x more data capacity at SSD-latency for cold items
  Tradeoff: Cold reads add ~100us latency (SSD read)
```

---

## 4. Dragonfly

### Overview

Dragonfly is a modern in-memory datastore (2022+) designed as a Redis/Memcached replacement. It is multi-threaded, Redis-API compatible, and significantly more memory-efficient.

### Multi-Threaded Shared-Nothing Architecture

```
Dragonfly Architecture
══════════════════════════════════════════════════════════════════

  ┌───────────────────────────────────────────────────────────┐
  │                    Dragonfly Process                        │
  │                                                           │
  │  ┌────────────┐  ┌────────────┐       ┌────────────┐     │
  │  │  Thread 1   │  │  Thread 2   │  ...  │  Thread N   │     │
  │  │            │  │            │       │            │     │
  │  │ ┌────────┐ │  │ ┌────────┐ │       │ ┌────────┐ │     │
  │  │ │Dash    │ │  │ │Dash    │ │       │ │Dash    │ │     │
  │  │ │Table   │ │  │ │Table   │ │       │ │Table   │ │     │
  │  │ │(shard) │ │  │ │(shard) │ │       │ │(shard) │ │     │
  │  │ └────────┘ │  │ └────────┘ │       │ └────────┘ │     │
  │  │            │  │            │       │            │     │
  │  │ Event loop │  │ Event loop │       │ Event loop │     │
  │  │ (io_uring/ │  │ (io_uring/ │       │ (io_uring/ │     │
  │  │  epoll)    │  │  epoll)    │       │  epoll)    │     │
  │  └────────────┘  └────────────┘       └────────────┘     │
  │                                                           │
  │  No shared state between threads -- each thread owns      │
  │  its portion of the keyspace. Cross-shard operations      │
  │  use lightweight message passing (no locks).              │
  └───────────────────────────────────────────────────────────┘

  Key routing: hash(key) % num_threads -> thread that owns key
```

### Dash (Dashtable)

The core data structure is **Dashtable** -- a lock-free hash table designed for the shared-nothing architecture:

- **Bucketized cuckoo hashing** with two hash functions
- Each bucket holds ~14 entries (cache-line aligned, 64 bytes)
- **Stash** buckets for overflow (avoids infinite loops in cuckoo hashing)
- O(1) expected lookup with very high load factors (>90%)
- No per-key memory allocation overhead (entries are inline in buckets)

### Memory Efficiency

| Database | Memory for 1M 100-byte values | Overhead |
|----------|-------------------------------|----------|
| Redis | ~180 MB | ~80% overhead |
| Dragonfly | ~120 MB | ~20% overhead |

Dragonfly achieves lower overhead because:
- No `redisObject` wrapper per key (16 bytes saved per key)
- Inline storage in Dashtable buckets (no pointer chasing)
- Compact encoding of small values

### Performance Benchmarks

| Benchmark | Redis 7 (single) | Redis 7 (cluster, 8 nodes) | Dragonfly (8 threads) |
|-----------|-------------------|---------------------------|----------------------|
| SET throughput | ~150K ops/s | ~800K ops/s | ~1.5M ops/s |
| GET throughput | ~180K ops/s | ~1M ops/s | ~2.5M ops/s |
| p99 latency (GET) | ~0.3 ms | ~0.5 ms | ~0.2 ms |
| Memory per 1M keys | ~180 MB | ~180 MB * 8 | ~120 MB |
| Nodes required | 1 | 8 | 1 |

Dragonfly on a single node can match or exceed Redis Cluster throughput at lower latency with less memory.

### Trade-offs

- **Maturity:** Redis has 15+ years of production hardening; Dragonfly is newer
- **Ecosystem:** Redis has vastly more client libraries, tools, and community knowledge
- **Clustering:** Dragonfly scales vertically (single node, many threads) vs Redis horizontal scaling
- **Persistence:** Dragonfly supports RDB snapshots and soon AOF; Redis has battle-tested persistence
- **Compatibility:** ~200 Redis commands supported, but some gaps remain

---

## 5. VoltDB

### Overview

VoltDB is an in-memory **NewSQL** database designed for ultra-high-throughput OLTP with ACID transactions and SQL support.

### Deterministic Execution Model

```
VoltDB Architecture
══════════════════════════════════════════════════════════════════

  ┌───────────────────────────────────────────────────────────┐
  │                      VoltDB Cluster                        │
  │                                                           │
  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐      │
  │  │   Node 1     │  │   Node 2     │  │   Node 3     │      │
  │  │             │  │             │  │             │      │
  │  │ ┌─────────┐ │  │ ┌─────────┐ │  │ ┌─────────┐ │      │
  │  │ │Partition│ │  │ │Partition│ │  │ │Partition│ │      │
  │  │ │ 1 (lead)│ │  │ │ 1 (repl)│ │  │ │ 2 (repl)│ │      │
  │  │ ├─────────┤ │  │ ├─────────┤ │  │ ├─────────┤ │      │
  │  │ │Partition│ │  │ │Partition│ │  │ │Partition│ │      │
  │  │ │ 2 (repl)│ │  │ │ 3 (lead)│ │  │ │ 3 (repl)│ │      │
  │  │ ├─────────┤ │  │ ├─────────┤ │  │ ├─────────┤ │      │
  │  │ │Partition│ │  │ │Partition│ │  │ │Partition│ │      │
  │  │ │ 3 (repl)│ │  │ │ 2 (lead)│ │  │ │ 1 (repl)│ │      │
  │  │ └─────────┘ │  │ └─────────┘ │  │ └─────────┘ │      │
  │  └─────────────┘  └─────────────┘  └─────────────┘      │
  │                                                           │
  │  Each partition has ONE thread -- no locking needed!       │
  │  Transactions execute serially within a partition.         │
  └───────────────────────────────────────────────────────────┘
```

### Key Design Principles

1. **Single-threaded partitions:** Each partition processes transactions serially on one thread. No locks, no latches, no contention. This is similar to Redis but with full SQL and ACID.

2. **Stored procedures:** All transactions are pre-compiled Java stored procedures:
```java
public class TransferMoney extends VoltProcedure {
    public final SQLStmt checkBalance =
        new SQLStmt("SELECT balance FROM accounts WHERE id = ?");
    public final SQLStmt debit =
        new SQLStmt("UPDATE accounts SET balance = balance - ? WHERE id = ?");
    public final SQLStmt credit =
        new SQLStmt("UPDATE accounts SET balance = balance + ? WHERE id = ?");

    public VoltTable[] run(long fromId, long toId, double amount) {
        voltQueueSQL(checkBalance, fromId);
        VoltTable[] results = voltExecuteSQL();

        if (results[0].fetchRow(0).getDouble("balance") < amount)
            throw new VoltAbortException("Insufficient funds");

        voltQueueSQL(debit, amount, fromId);
        voltQueueSQL(credit, amount, toId);
        return voltExecuteSQL(true);  // true = final batch
    }
}
```

3. **Multi-partition transactions:** When a transaction touches multiple partitions, VoltDB coordinates them:
   - Single-partition: runs on the partition thread, ~10us latency
   - Multi-partition: requires coordination, ~30-50ms (much slower)
   - **Design goal:** Keep 95%+ of transactions single-partition

### Durability: Command Logging

VoltDB uses **command logging** (not WAL):
- Logs the stored procedure call + parameters (not the data changes)
- Much smaller log volume than WAL
- On recovery: replay stored procedures deterministically
- Snapshots + command log = full durability

### K-Safety

VoltDB replicates each partition K+1 times across different nodes:
- K=0: no replication (development only)
- K=1: survives 1 node failure (production minimum)
- K=2: survives 2 node failures

### When to Use VoltDB

| Good Fit | Poor Fit |
|----------|----------|
| High-throughput OLTP (100K+ txn/s) | Complex analytical queries |
| Low-latency requirements (<10ms) | Large dataset (>1TB per node) |
| Strong consistency required | Ad-hoc queries (SQL flexibility limited) |
| Telecom billing, fraud detection | Small teams without DB expertise |
| Financial trading systems | Workloads with many multi-partition txns |

---

## 6. SAP HANA (In-Memory Aspects)

### Architecture

```
SAP HANA In-Memory Architecture
══════════════════════════════════════════════════════════════════

  ┌───────────────────────────────────────────────────────────┐
  │                    Column Store                             │
  │                                                           │
  │  Table: ORDERS                                             │
  │                                                           │
  │  ┌──────────────────────────────────────────────────────┐ │
  │  │                  MAIN STORE (read-optimized)          │ │
  │  │                                                      │ │
  │  │  Column: order_id    [1, 2, 3, 4, 5, ...]           │ │
  │  │  Column: customer_id [101, 102, 101, 103, ...]      │ │
  │  │  Column: amount      [99.50, 150.00, 72.30, ...]    │ │
  │  │  Column: status      [dict: 0=new, 1=paid, 2=ship]  │ │
  │  │                      [1, 0, 1, 2, 1, ...]           │ │
  │  │                                                      │ │
  │  │  Compressed, sorted, dictionary-encoded               │ │
  │  │  Read-optimized, batch updates only                   │ │
  │  └──────────────────────────────────────────────────────┘ │
  │                                                           │
  │  ┌──────────────────────────────────────────────────────┐ │
  │  │                  DELTA STORE (write-optimized)         │ │
  │  │                                                      │ │
  │  │  Row-oriented buffer for recent inserts/updates       │ │
  │  │  Not compressed (fast writes)                         │ │
  │  │  Periodically merged into Main Store ("delta merge")  │ │
  │  └──────────────────────────────────────────────────────┘ │
  │                                                           │
  │  ┌──────────────────────────────────────────────────────┐ │
  │  │                  HISTORY (for MVCC)                    │ │
  │  │                                                      │ │
  │  │  Old versions of modified rows                        │ │
  │  │  Used for snapshot isolation                           │ │
  │  │  Garbage collected when no readers need them          │ │
  │  └──────────────────────────────────────────────────────┘ │
  └───────────────────────────────────────────────────────────┘

  Query path: check Delta Store first, then Main Store
  Reads merge results from both stores transparently
```

### Dictionary Compression

```
Dictionary Compression Example
══════════════════════════════════════════════════════════════════

  Column: country (1 billion rows)

  Without compression:
  ["United States", "Germany", "United States", "Japan", "Germany", ...]
  = ~14 bytes avg * 1B = ~14 GB

  With dictionary compression:
  Dictionary:  {0: "Germany", 1: "Japan", 2: "United States"}
  Values:      [2, 0, 2, 1, 0, ...]
  Bit width:   2 bits per value (3 distinct values -> ceil(log2(3)) = 2)

  Storage: 3 strings + 2 bits * 1B = ~250 MB  (56x compression)
```

### NUMA-Aware Memory

SAP HANA is designed for multi-socket servers and is NUMA-aware:
- Allocates column partitions on the NUMA node closest to the processing core
- Avoids cross-socket memory access (2-3x latency penalty)
- Uses huge pages (2MB or 1GB) to reduce TLB misses

### Data Aging (Hot/Warm/Cold Tiers)

| Tier | Storage | Access Speed | Use |
|------|---------|-------------|-----|
| **Hot** | DRAM | Nanoseconds | Current transactions, active analytics |
| **Warm** | Persistent Memory (Optane) or disk | Microseconds | Recent historical data |
| **Cold** | Disk / Data Lake | Milliseconds | Archival, compliance |

Data automatically migrates between tiers based on access patterns and configured policies.

---

## 7. Apache Ignite / Hazelcast

### Distributed In-Memory Computing Platforms

Both Apache Ignite and Hazelcast provide distributed in-memory data grids with compute capabilities.

```
Distributed In-Memory Data Grid
══════════════════════════════════════════════════════════════════

  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐
  │    Node 1      │  │    Node 2      │  │    Node 3      │
  │               │  │               │  │               │
  │ ┌───────────┐ │  │ ┌───────────┐ │  │ ┌───────────┐ │
  │ │ Partition │ │  │ │ Partition │ │  │ │ Partition │ │
  │ │ 1 (prim)  │ │  │ │ 1 (backup)│ │  │ │ 2 (backup)│ │
  │ ├───────────┤ │  │ ├───────────┤ │  │ ├───────────┤ │
  │ │ Partition │ │  │ │ Partition │ │  │ │ Partition │ │
  │ │ 3 (backup)│ │  │ │ 2 (prim)  │ │  │ │ 3 (prim)  │ │
  │ └───────────┘ │  │ └───────────┘ │  │ └───────────┘ │
  │               │  │               │  │               │
  │ ┌───────────┐ │  │ ┌───────────┐ │  │ ┌───────────┐ │
  │ │ Compute   │ │  │ │ Compute   │ │  │ │ Compute   │ │
  │ │ Engine    │ │  │ │ Engine    │ │  │ │ Engine    │ │
  │ └───────────┘ │  │ └───────────┘ │  │ └───────────┘ │
  │               │  │               │  │               │
  │ ┌───────────┐ │  │ ┌───────────┐ │  │ ┌───────────┐ │
  │ │ SQL Engine│ │  │ │ SQL Engine│ │  │ │ SQL Engine│ │
  │ └───────────┘ │  │ └───────────┘ │  │ └───────────┘ │
  └───────────────┘  └───────────────┘  └───────────────┘
         │                  │                  │
         └──────────────────┼──────────────────┘
                  Discovery + Communication
                  (TCP, multicast, or Kubernetes)
```

### Data Grid vs Database

| Feature | Data Grid (Ignite/Hazelcast) | Traditional In-Memory DB |
|---------|------|-------------|
| Primary role | Distributed cache + compute | Data storage + query |
| SQL support | Yes (limited, distributed) | Full SQL (VoltDB, HANA) |
| Compute co-location | Yes (run code where data lives) | No (client-server only) |
| Transaction support | Yes (2PC, optimistic/pessimistic) | Yes |
| Schema enforcement | Optional | Required |
| Persistence | Write-through/behind to external DB | Built-in (WAL, snapshots) |

### Near Cache Pattern

```
Near Cache (Client-Side Local Cache)
══════════════════════════════════════════════════════════════════

  ┌──────────────────┐          ┌──────────────────┐
  │  Application     │          │  Ignite/Hazelcast │
  │                  │          │  Cluster          │
  │ ┌──────────────┐ │          │                  │
  │ │  Near Cache   │ │  miss    │ ┌──────────────┐ │
  │ │  (local heap) │─┼─────────┼>│ Distributed  │ │
  │ │              │ │          │ │ Cache         │ │
  │ │  hit: ~100ns │ │  fill    │ │              │ │
  │ │  (no network)│<┼─────────┼─│ hit: ~500us  │ │
  │ └──────────────┘ │          │ │ (network)    │ │
  │                  │          │ └──────────────┘ │
  └──────────────────┘          └──────────────────┘

  Invalidation:
  - Cluster sends invalidation event when key changes
  - Near cache evicts stale entry
  - Next read fetches from cluster
  - Trade-off: slight staleness for massive read speed improvement
```

### Compute Grid: Running Code Where Data Lives

```java
// Apache Ignite: compute on data nodes (avoids shipping data)
ignite.compute().broadcast((IgniteCallable<Integer>) () -> {
    IgniteCache<Long, Customer> cache = Ignition.ignite().cache("customers");

    int count = 0;
    for (Cache.Entry<Long, Customer> entry : cache.localEntries()) {
        if (entry.getValue().getBalance() > 10000) {
            count++;
        }
    }
    return count;
});
// Each node processes ONLY its local partition -- no data movement
```

### Write-Through and Write-Behind

```
Write-Through vs Write-Behind
══════════════════════════════════════════════════════════════════

  Write-Through (synchronous):
  ┌────────┐    ┌──────────┐    ┌──────────┐
  │  App   │───>│ In-Memory │───>│ Database │
  │        │    │ Grid      │    │ (MySQL)  │
  │        │    │           │    │          │
  │ waits  │<───│ ACK after │<───│ ACK      │
  │        │    │ DB write  │    │          │
  └────────┘    └──────────┘    └──────────┘
  Consistent but slower (DB latency in write path)

  Write-Behind (asynchronous):
  ┌────────┐    ┌──────────┐         ┌──────────┐
  │  App   │───>│ In-Memory │ ──queue──>│ Database │
  │        │    │ Grid      │  (async)  │ (MySQL)  │
  │        │    │           │          │          │
  │ returns│<───│ ACK after │          │          │
  │ fast   │    │ memory    │          │          │
  └────────┘    │ write     │          │          │
                │           │          │          │
                │ Batch     │─ flush ─>│          │
                │ coalesce  │  every   │          │
                │ writes    │  N ms    │          │
                └──────────┘          └──────────┘
  Fast but risk of data loss if grid node fails before flush
```

---

## 8. Aerospike

### Hybrid Memory Architecture

Aerospike's key innovation is **index in RAM, data on SSD** -- getting close to in-memory speed with SSD-level capacity:

```
Aerospike Hybrid Memory Architecture
══════════════════════════════════════════════════════════════════

  ┌───────────────────────────────────────────────────────────┐
  │                        RAM                                 │
  │  ┌─────────────────────────────────────────────────────┐  │
  │  │  Primary Index (red-black tree, ~64 bytes per record)│  │
  │  │                                                     │  │
  │  │  Record key → (set, partition, SSD location)        │  │
  │  │                                                     │  │
  │  │  100M records * 64 bytes = ~6.4 GB RAM              │  │
  │  └─────────────────────────────────────────────────────┘  │
  └──────────────────────────┬────────────────────────────────┘
                             │ pointer
                             ▼
  ┌───────────────────────────────────────────────────────────┐
  │                        SSD                                 │
  │  ┌─────────────────────────────────────────────────────┐  │
  │  │  Data stored in large blocks (128KB-1MB "write       │  │
  │  │  blocks"), written sequentially                      │  │
  │  │                                                     │  │
  │  │  Reads: direct device I/O (bypass filesystem)       │  │
  │  │  Writes: log-structured, append-only                │  │
  │  │  Defragmentation: background compaction             │  │
  │  │                                                     │  │
  │  │  100M records * 1KB avg = ~100 GB SSD               │  │
  │  └─────────────────────────────────────────────────────┘  │
  └───────────────────────────────────────────────────────────┘

  Read path:  Index lookup (RAM, ~1us) -> SSD read (~100us) = ~101us
  Write path: Index update (RAM) + append to write buffer -> batch write
```

### Bypassing the Filesystem

Aerospike writes directly to raw block devices (no ext4/xfs). This eliminates:
- Filesystem metadata overhead
- Double-buffering (page cache + application buffer)
- Journaling overhead
- inode management

The result is **2-5x better SSD throughput** compared to filesystem-based storage.

### Consistency Modes

| Mode | Guarantees | Trade-off |
|------|-----------|-----------|
| **AP (availability)** | Eventual consistency, no data loss with replication | Higher availability, possible stale reads |
| **CP (strong consistency)** | Linearizable reads/writes via Raft-like protocol | Lower availability during partitions |
| **SC (session consistency)** | Read-your-writes within a session | Balance of both |

### Smart Client

```
Aerospike Smart Client
══════════════════════════════════════════════════════════════════

  ┌──────────────────────────────────────────────────────────┐
  │  Application + Aerospike Smart Client                     │
  │                                                          │
  │  ┌──────────────────────────────────────────────────┐    │
  │  │  Partition Map (cached locally)                   │    │
  │  │                                                    │    │
  │  │  Partition 0   -> Node A (master), Node B (repl)  │    │
  │  │  Partition 1   -> Node C (master), Node A (repl)  │    │
  │  │  Partition 2   -> Node B (master), Node C (repl)  │    │
  │  │  ...                                               │    │
  │  │  Partition 4095 -> Node A (master), Node C (repl) │    │
  │  └──────────────────────────────────────────────────┘    │
  │                                                          │
  │  Client hashes key -> partition -> sends directly to     │
  │  the correct master node. NO proxy, NO redirect.         │
  │                                                          │
  │  If node fails, client detects via cluster tend thread   │
  │  and updates partition map automatically.                │
  └──────────────────────────────────────────────────────────┘

  Result: Single-hop reads/writes (no MOVED/ASK like Redis Cluster)
```

### Cross-Datacenter Replication (XDR)

Aerospike supports active-active cross-datacenter replication:
- Asynchronous shipping of write operations to remote clusters
- Conflict resolution via generation count + last-update-time
- Selective replication (per-namespace, per-set)
- Typically 50-200ms replication lag between datacenters

---

## 9. In-Memory Caching Patterns

### Cache-Aside (Lazy Loading)

```
Cache-Aside Pattern
══════════════════════════════════════════════════════════════════

  Read Path:
  ┌────────┐    1. GET    ┌──────────┐
  │  App   │─────────────>│  Cache   │
  │        │<─────────────│          │  2a. HIT: return cached
  │        │              └──────────┘
  │        │
  │        │  2b. MISS:   ┌──────────┐
  │        │─────────────>│ Database │  3. Query DB
  │        │<─────────────│          │  4. Return result
  │        │              └──────────┘
  │        │
  │        │  5. SET      ┌──────────┐
  │        │─────────────>│  Cache   │  6. Store for next read
  └────────┘              └──────────┘

  Write Path:
  ┌────────┐    1. UPDATE ┌──────────┐
  │  App   │─────────────>│ Database │  2. Write to DB
  │        │              └──────────┘
  │        │
  │        │  3. DELETE   ┌──────────┐
  │        │─────────────>│  Cache   │  4. Invalidate cache
  └────────┘              └──────────┘
  (next read will populate cache from DB)
```

**Why DELETE instead of SET on write?** Avoids race conditions:
```
Thread A: UPDATE DB to value X
Thread B: UPDATE DB to value Y
Thread B: SET cache to Y
Thread A: SET cache to X     <-- Cache now has stale value X!

With DELETE:
Thread A: UPDATE DB to value X
Thread B: UPDATE DB to value Y
Thread B: DELETE cache
Thread A: DELETE cache        <-- Cache is empty, next read gets Y from DB
```

### Read-Through Cache

```
Read-Through Pattern
══════════════════════════════════════════════════════════════════

  ┌────────┐    GET     ┌────────────────────────┐    SELECT   ┌──────────┐
  │  App   │──────────>│  Cache (with loader)    │──────────>│ Database │
  │        │<──────────│                        │<──────────│          │
  └────────┘   result  │  Cache manages loading  │   result  └──────────┘
                       │  from DB on miss.       │
                       │  App only talks to cache│
                       └────────────────────────┘

  Difference from cache-aside: the CACHE is responsible for loading
  data from DB, not the application. Simplifies app code.
```

### Write-Through Cache

```
Write-Through Pattern
══════════════════════════════════════════════════════════════════

  ┌────────┐   WRITE   ┌──────────┐   WRITE   ┌──────────┐
  │  App   │─────────>│  Cache   │─────────>│ Database │
  │        │          │          │          │          │
  │        │<─────────│  ACK     │<─────────│  ACK     │
  └────────┘  (after  └──────────┘          └──────────┘
              both
              succeed)

  Pro:  Cache is always consistent with DB
  Con:  Write latency = cache write + DB write (slower)
  Use:  When read-after-write consistency is critical
```

### Write-Behind (Write-Back) Cache

```
Write-Behind Pattern
══════════════════════════════════════════════════════════════════

  ┌────────┐   WRITE   ┌──────────┐            ┌──────────┐
  │  App   │─────────>│  Cache   │            │ Database │
  │        │          │          │            │          │
  │        │<─────────│ ACK now  │            │          │
  └────────┘ (fast)   │          │            │          │
                      │ Queue:   │            │          │
                      │ [w1,w2,  │── batch ──>│ Batch    │
                      │  w3,w4]  │   flush    │ write    │
                      │          │  (async,   │          │
                      │ Coalesce │   every    │          │
                      │ updates  │   100ms)   │          │
                      └──────────┘            └──────────┘

  Pro:  Very fast writes (memory-speed), batch coalescing
  Con:  Risk of data loss if cache fails before flush
  Use:  High write throughput, eventual consistency OK
```

### Cache Invalidation Strategies

| Strategy | Description | Consistency | Complexity |
|----------|-------------|-------------|------------|
| **TTL-based** | Keys expire after fixed time | Eventual (stale window = TTL) | Low |
| **Event-driven** | DB triggers invalidation event (CDC, pub/sub) | Near real-time | Medium |
| **Version-based** | Cache key includes version counter | Strong (on hit) | Medium |
| **Lease-based** | Cache entry has a lease; writer must acquire | Strong | High |
| **Write-invalidate** | Writer deletes cache entry | Eventual (short window) | Low |
| **Write-update** | Writer updates cache and DB | Strong (if atomic) | Medium |

### Thundering Herd / Cache Stampede

```
Cache Stampede Problem
══════════════════════════════════════════════════════════════════

  Popular key expires (or is evicted):

  Thread 1 ──> MISS ──> query DB ──┐
  Thread 2 ──> MISS ──> query DB ──┤
  Thread 3 ──> MISS ──> query DB ──┼──> DB overwhelmed!
  Thread 4 ──> MISS ──> query DB ──┤    (100 concurrent queries
  ...                               │     for the same data)
  Thread N ──> MISS ──> query DB ──┘


  Mitigation 1: Locking (single-flight)
  ═══════════════════════════════════════
  Thread 1 ──> MISS ──> acquire lock ──> query DB ──> SET cache ──> release
  Thread 2 ──> MISS ──> lock busy ──> wait ──> retry ──> HIT
  Thread 3 ──> MISS ──> lock busy ──> wait ──> retry ──> HIT

  Mitigation 2: Probabilistic early expiration (XFetch)
  ═════════════════════════════════════════════════════════
  actual_ttl = ttl - (random_factor * beta * log(rand()))
  // Some clients will refresh BEFORE actual expiry, spreading the load

  Mitigation 3: Background refresh
  ══════════════════════════════════
  - Cache entry has TTL = infinity
  - Background job refreshes every T seconds
  - Cache always has data (possibly slightly stale)
```

### Multi-Level Caching

```
Multi-Level Cache (L1 Local + L2 Distributed)
══════════════════════════════════════════════════════════════════

  ┌────────────────────────────────────────────────────────────┐
  │  Application Server 1                                      │
  │  ┌──────────────────────┐                                  │
  │  │  L1: Local Cache      │  In-process, ~100ns              │
  │  │  (Caffeine, Guava,   │  Small (100MB-1GB)               │
  │  │   LRU dict)          │  Per-instance, not shared         │
  │  └──────────┬───────────┘                                  │
  │             │ miss                                          │
  │             ▼                                              │
  └─────────────┼──────────────────────────────────────────────┘
                │
                ▼
  ┌────────────────────────────────────────────────────────────┐
  │  L2: Distributed Cache (Redis / Memcached)                  │
  │  Network hop (~500us), shared across all app servers        │
  │  Large (10GB-100GB+)                                        │
  └──────────────┬─────────────────────────────────────────────┘
                 │ miss
                 ▼
  ┌────────────────────────────────────────────────────────────┐
  │  Database (PostgreSQL, MySQL, etc.)                         │
  │  ~5-50ms per query                                          │
  └────────────────────────────────────────────────────────────┘

  L1 Invalidation:
  - Short TTL (5-30 seconds) -- tolerate brief staleness
  - Or: subscribe to Redis pub/sub for invalidation events
  - Or: use consistent hashing to ensure same key -> same server
```

### Cache Warming Strategies

| Strategy | Description | When |
|----------|-------------|------|
| **Lazy (on-demand)** | Cache populates on first read | Default, simplest |
| **Pre-warm on deploy** | Script loads hot keys before traffic | Deployment, scaling up |
| **Follower warm** | New cache node replicates from existing node | Adding cache capacity |
| **Dump and load** | Export cache (RDB) from old node, import on new | Redis migration |
| **Shadow traffic** | Replay production reads against new cache | Cache migration |

---

## 10. In-Memory Database Design Considerations

### Data Durability Spectrum

```
Durability vs Performance Tradeoff
══════════════════════════════════════════════════════════════════

  Performance  ▲
  (throughput)  │
               │  ● Ephemeral (pure RAM, no persist)
               │       100% memory speed
               │
               │       ● Async persist (AOF everysec)
               │            ~2% overhead
               │
               │            ● Sync persist (AOF always)
               │                 ~20-40% overhead
               │
               │                 ● Replicated + sync persist
               │                      ~50% overhead (network)
               │
               │                      ● Replicated + sync persist
               │                           + cross-DC
               │                           ~70% overhead
               └──────────────────────────────────────────> Durability
                  none     low      medium     high    maximum
```

### Memory vs Disk Cost Analysis (2024 Pricing)

| Resource | Cost per GB/month (AWS) | 1 TB Cost/month |
|----------|------------------------|-----------------|
| RAM (r6g.xlarge) | ~$5.50 | ~$5,500 |
| NVMe SSD (i3.xlarge) | ~$0.25 | ~$250 |
| GP3 EBS SSD | ~$0.08 | ~$80 |
| S3 Standard | ~$0.023 | ~$23 |

**Rule of thumb:** RAM is ~70x more expensive than SSD, ~240x more expensive than S3. Use in-memory only for data that justifies the cost through latency or throughput requirements.

**Break-even calculation:**
```
If in-memory saves you 10 database servers (at $500/month each):
  $5,000/month saved
  Can afford: ~900 GB RAM at $5.50/GB = $4,950/month
  Or: 1 r6g.16xlarge (512 GB RAM) at ~$2,800/month -> clear win
```

### Handling Data Larger Than Memory

| Approach | How | Example |
|----------|-----|---------|
| **Tiered storage** | Hot data in RAM, warm on SSD | Aerospike, Redis on Flash |
| **Sharding** | Distribute across nodes | Redis Cluster, Ignite |
| **Eviction** | Keep only working set in memory | LRU/LFU eviction policies |
| **Compression** | Reduce memory footprint | Dictionary encoding (HANA), LZF (Redis) |
| **Off-heap** | Store data outside JVM heap (avoid GC) | Ignite, Hazelcast |
| **Memory-mapped files** | Let OS manage RAM/disk paging | RocksDB, LMDB |

### Garbage Collection in JVM-Based In-Memory DBs

JVM-based systems (Ignite, Hazelcast, VoltDB) face GC pressure:

```
GC Impact on Latency
══════════════════════════════════════════════════════════════════

  Normal operation:     p99 = 1ms
  During GC pause:     p99 = 50-500ms (!)

  Mitigation Strategies:
  ┌──────────────────────────────────────────────────────────┐
  │                                                          │
  │  1. Off-heap storage (store data outside JVM heap)       │
  │     - Ignite: uses off-heap memory regions by default    │
  │     - Hazelcast HD Memory: off-heap, no GC impact        │
  │     - Only metadata/pointers on heap                     │
  │                                                          │
  │  2. Tuned GC (if must use heap):                         │
  │     - G1GC with low pause targets (-XX:MaxGCPauseMillis) │
  │     - ZGC (sub-ms pauses, Java 15+)                      │
  │     - Shenandoah (concurrent compaction)                 │
  │                                                          │
  │  3. Object pooling:                                      │
  │     - Reuse objects instead of allocating new ones       │
  │     - Reduces allocation rate -> fewer GCs               │
  │                                                          │
  │  4. Large pages (-XX:+UseLargePages):                    │
  │     - Reduces TLB misses for large heaps                 │
  │     - ~5-10% throughput improvement                      │
  └──────────────────────────────────────────────────────────┘
```

### Serialization Formats

| Format | Speed | Size | Schema | Language Support | Best For |
|--------|-------|------|--------|-----------------|----------|
| **JSON** | Slow | Large | No | Universal | Debug, APIs |
| **MessagePack** | Fast | Compact | No | Wide | Redis modules, cross-language |
| **Protocol Buffers** | Fast | Compact | Yes (.proto) | Wide | gRPC services, Ignite |
| **FlatBuffers** | Very fast | Compact | Yes | Moderate | Zero-copy access, gaming |
| **Cap'n Proto** | Very fast | Compact | Yes | Moderate | Zero-copy, no decode step |
| **Avro** | Fast | Compact | Yes (.avsc) | Wide | Hadoop/Kafka ecosystem |
| **Kryo** | Very fast | Compact | No | Java only | JVM in-memory DBs |

**Zero-copy deserialization** (FlatBuffers, Cap'n Proto): Access fields directly from the serialized buffer without parsing into objects. Critical for in-memory DBs where deserialization overhead can dominate.

### Off-Heap Memory Management

```
JVM Heap vs Off-Heap
══════════════════════════════════════════════════════════════════

  ┌──────────────────────────────────────────────────────────┐
  │  JVM Process                                              │
  │                                                          │
  │  ┌─────────────────────────────┐                         │
  │  │  JVM Heap (managed by GC)   │  Application objects     │
  │  │  -Xmx: 4GB                 │  Metadata, pointers      │
  │  │  GC pauses affect latency  │  Small: hundreds of MB   │
  │  └─────────────────────────────┘                         │
  │                                                          │
  │  ┌─────────────────────────────┐                         │
  │  │  Off-Heap (DirectByteBuffer │  Data storage            │
  │  │  or Unsafe.allocateMemory)  │  No GC pressure          │
  │  │  Size: 100+ GB             │  Manual memory mgmt      │
  │  │  Not visible to GC         │  Must handle leaks       │
  │  └─────────────────────────────┘                         │
  │                                                          │
  │  ┌─────────────────────────────┐                         │
  │  │  Memory-Mapped Files (mmap) │  Persistence + sharing   │
  │  │  OS manages page cache     │  Survives restarts        │
  │  │  Transparent paging        │  File-backed             │
  │  └─────────────────────────────┘                         │
  └──────────────────────────────────────────────────────────┘
```

---

## 11. In-Memory Database Comparison Table

### Feature Comparison

| Feature | Redis | Memcached | Dragonfly | VoltDB | Aerospike | SAP HANA |
|---------|-------|-----------|-----------|--------|-----------|----------|
| **Data Model** | Key-value + data structures | Key-value only | Key-value + data structures | Relational (SQL) | Key-value + document | Relational + column store |
| **Persistence** | RDB, AOF, Hybrid | None (Extstore for SSD) | RDB snapshots | Command log + snapshots | Hybrid (index RAM, data SSD) | Savepoints, logs, persistent memory |
| **Threading** | Single-thread exec, multi-thread I/O (6+) | Multi-threaded | Multi-threaded shared-nothing | Single-thread per partition | Multi-threaded | Multi-threaded |
| **Clustering** | Redis Cluster (16384 slots) | Client-side only | Single-node vertical | Built-in partitioning | Built-in sharding (4096 partitions) | Scale-out, distributed |
| **Max Dataset** | Limited by RAM | Limited by RAM (+Extstore) | Limited by RAM | Limited by RAM | Terabytes (SSD-backed) | Terabytes (tiered storage) |
| **Consistency** | Eventual (async repl) | None (no repl) | Eventual | Strong (ACID) | Configurable (AP or CP) | Strong (ACID) |
| **Use Cases** | Caching, sessions, queues, leaderboards | Simple caching | Redis replacement at scale | High-throughput OLTP | Ad tech, IoT, user profiles | Enterprise analytics, ERP |
| **License** | SSPL (7.0+) / BSD (older) | BSD | BSL 1.1 | AGPL / Proprietary | AGPL / Proprietary | Proprietary |
| **Typical Latency** | <1ms | <1ms | <1ms | <10ms (single-partition) | <1ms (index), <5ms (SSD read) | <10ms (analytics) |
| **Protocol** | RESP | Text / Binary | RESP (Redis compat) | JDBC, custom wire | Custom binary, Smart Client | ODBC, JDBC, custom |
| **Transactions** | MULTI/EXEC (single node) | None | MULTI/EXEC (compat) | Full ACID, stored procs | Single-record atomic | Full ACID, SQL |

### Choosing the Right In-Memory Database

```
Decision Tree
══════════════════════════════════════════════════════════════════

  Need SQL and ACID transactions?
  ├── YES ──> Need real-time analytics on TBs?
  │           ├── YES ──> SAP HANA
  │           └── NO ──> Need >100K txn/sec?
  │                      ├── YES ──> VoltDB
  │                      └── NO ──> PostgreSQL (with enough RAM)
  │
  └── NO ──> Need data structures (lists, sets, sorted sets)?
             ├── YES ──> Need >500K ops/sec on single node?
             │           ├── YES ──> Dragonfly
             │           └── NO ──> Redis
             │
             └── NO ──> Simple key-value cache?
                        ├── YES ──> Need multi-core scaling?
                        │           ├── YES ──> Memcached or Dragonfly
                        │           └── NO ──> Redis
                        │
                        └── NO ──> Need TB+ capacity with low latency?
                                   ├── YES ──> Aerospike
                                   └── NO ──> Need distributed compute?
                                              ├── YES ──> Apache Ignite / Hazelcast
                                              └── NO ──> Redis
```

### Performance Characteristics Summary

| Database | Read Latency (p99) | Write Latency (p99) | Throughput (single node) | Memory Overhead |
|----------|-------------------|---------------------|------------------------|-----------------|
| Redis | 0.2-0.5 ms | 0.2-0.5 ms | 100-200K ops/s | ~80% (pointers, metadata) |
| Memcached | 0.2-0.5 ms | 0.2-0.5 ms | 200-500K ops/s | ~15-60% (slab waste) |
| Dragonfly | 0.1-0.3 ms | 0.1-0.3 ms | 1-4M ops/s | ~20% |
| VoltDB | 1-10 ms | 1-10 ms | 100-300K txn/s | Moderate (JVM) |
| Aerospike | 0.5-2 ms (SSD) | 0.5-5 ms (SSD) | 100-500K ops/s | Low (index only in RAM) |
| SAP HANA | 5-50 ms (analytics) | 1-5 ms (OLTP) | Depends on query | Moderate (compression) |

---

## Production Recommendations

### Redis Production Checklist

```
Redis Production Configuration
══════════════════════════════════════════════════════════════════

  # Memory
  maxmemory 24gb                         # Leave ~25% for OS, fork, fragmentation
  maxmemory-policy allkeys-lfu           # LFU for general workloads
  activedefrag yes                       # Auto-defragment
  lazyfree-lazy-eviction yes             # Background eviction (non-blocking)
  lazyfree-lazy-expire yes               # Background expiry
  lazyfree-lazy-server-del yes           # Background DEL for large keys

  # Persistence (if needed)
  appendonly yes
  appendfsync everysec
  aof-use-rdb-preamble yes               # Hybrid RDB+AOF
  save ""                                # Disable standalone RDB saves
  rdbcompression yes
  rdbchecksum yes

  # Networking
  tcp-backlog 511
  timeout 300                            # Close idle connections after 5min
  tcp-keepalive 60
  io-threads 4                           # Multi-threaded I/O (Redis 6+)
  io-threads-do-reads yes

  # Safety
  rename-command FLUSHALL ""             # Disable dangerous commands
  rename-command FLUSHDB ""
  rename-command KEYS ""                 # KEYS is O(n), use SCAN
  rename-command DEBUG ""

  # Slow log
  slowlog-log-slower-than 10000          # Log commands >10ms
  slowlog-max-len 256

  # Replication
  repl-backlog-size 256mb                # Large backlog for resilient partial sync
  min-replicas-to-write 1               # Require at least 1 replica ACK
  min-replicas-max-lag 10               # Replica must be within 10s
```

### Common Anti-Patterns

| Anti-Pattern | Problem | Solution |
|-------------|---------|----------|
| Using `KEYS *` in production | O(n), blocks event loop | Use `SCAN` with cursor |
| Storing large values (>100KB) | Blocks event loop during I/O | Split into smaller keys, use hashes |
| No `maxmemory` set | Redis grows until OOM kill | Always set maxmemory |
| Single Redis for everything | Single point of failure | Sentinel or Cluster |
| Not monitoring `used_memory_rss` | Fragmentation/swap invisible | Monitor and alert on ratio |
| Using Redis as primary database | Data loss risk | Use as cache + source of truth DB |
| `SAVE` in production | Blocks for seconds/minutes on large DBs | Use `BGSAVE` only |
| Lua scripts with unbounded loops | Block event loop indefinitely | Set time limits, avoid long scripts |

---

## Key Takeaways

1. **RAM is 1000-1,000,000x faster than disk.** When latency matters, in-memory databases are not optional -- they are the architecture.

2. **Redis is the default choice** for caching, sessions, and real-time data structures. Its single-threaded model is not a limitation for most workloads.

3. **Dragonfly is the future of Redis-compatible systems** for single-node vertical scaling. Watch this space.

4. **Memcached still wins** for pure, simple, multi-core caching with no persistence needs.

5. **Aerospike bridges the gap** between in-memory and SSD storage, enabling terabyte-scale datasets with sub-millisecond index lookups.

6. **VoltDB and SAP HANA** are for when you need full SQL and ACID on in-memory data -- different class of system entirely.

7. **Cache-aside with write-invalidate** is the most common and safest caching pattern. Use it unless you have a specific reason not to.

8. **Always plan for cache failure.** Your system must function (perhaps degraded) when the cache is cold or unavailable.

9. **Monitor memory fragmentation, eviction rate, and hit ratio.** These three metrics tell you 90% of what you need to know about cache health.

10. **The most expensive bug in caching is a thundering herd.** Implement locking or probabilistic early expiry for hot keys.
