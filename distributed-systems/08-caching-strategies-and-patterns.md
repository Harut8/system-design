# Caching Strategies and Patterns

A production-grade reference covering caching from first principles through production architecture. Covers the five foundational read/write strategies, cache invalidation approaches that actually work, thundering herd mitigation, eviction policies and their tradeoffs, Redis internals, distributed cache architecture with multi-level hierarchies, consistent hashing for cache sharding, capacity planning math, failure modes, and the concrete caching patterns that appear in every system design interview. Written for senior and Staff+ engineers who need to discuss caching with precision under interview pressure.

Prerequisites: familiarity with distributed system fundamentals from `00-primitives-and-system-models.md` and consistency models from `04-consistency-models-linearizability-to-eventual.md`.

---

## Table of Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [Mental Models](#1-mental-models)
2. [Caching Strategies — The Big Five](#2-caching-strategies--the-big-five)
3. [Cache Invalidation](#3-cache-invalidation)
4. [Thundering Herd and Cache Stampede](#4-thundering-herd-and-cache-stampede)
5. [Cache Eviction Policies](#5-cache-eviction-policies)
6. [Redis as a Cache — Deep Dive](#6-redis-as-a-cache--deep-dive)
7. [Distributed Caching Architecture](#7-distributed-caching-architecture)
8. [Consistent Hashing](#8-consistent-hashing)
9. [Cache in System Design Interviews](#9-cache-in-system-design-interviews)
10. [Capacity Planning](#10-capacity-planning)
11. [Failure Modes and Resilience](#11-failure-modes-and-resilience)
12. [Monitoring](#12-monitoring)
13. [Real-world cases — incidents with numbers](#13-real-world-cases--incidents-with-numbers)

---

## Start here — the whole chapter in plain words

**The problem.** Databases are slow and expensive compared with memory. Many apps ask the database
the same question over and over: "what is the price of product 42?" A cache keeps a copy of recent
answers in fast memory so most requests never reach the database. The hard parts are keeping the copy
fresh, surviving the moment a popular copy expires, choosing what to throw out when memory is full,
and staying up when the cache itself breaks.

**A real-world example.** An e-commerce site shows product pages. It gets 20,000 product reads/s and
about 200 price/stock updates/s (a 100:1 read-to-write ratio). One database query takes about 10 ms,
and the database handles about 5,000 such queries/s before it slows down.

- **No cache**: 20,000 queries/s hit a database that can do 5,000. Queues build, pages time out.
- **Cache-aside with Redis** (§2.1): the app checks Redis first (0.5 ms). With a 95% hit rate, only
  5% of reads reach the database: 20,000 x 0.05 = 1,000 queries/s. Average read latency is
  0.95 x 0.5 ms + 0.05 x (0.5 + 10) ms = 1.0 ms instead of 10 ms.
- **Invalidation** (§3): when a price changes, a change-data-capture event deletes `product:42`
  from Redis within milliseconds. A 5-minute TTL is the safety net: even if the event is lost, a
  wrong price lives at most 5 minutes.
- **Stampede protection** (§4): a flash-sale item gets 5,000 reads/s and takes 200 ms to rebuild.
  When its key expires, every read in those 200 ms misses: 5,000 x 0.2 = 1,000 identical queries.
  Singleflight cuts this to 1 query per app server (40 servers → at most 40); early refresh (XFetch)
  rebuilds it before it expires, so usually only 2-3 queries happen.
- **Eviction and sizing** (§5, §10): 2 million products x ~1,625 bytes (key + value + Redis
  overhead) = 3.25 GB; with 1 replica and 20% headroom, about 7.8 GB. When memory is full, the
  eviction policy (LRU/LFU) throws out the items least likely to be read again.
- **Failure and monitoring** (§11, §12): if the hit rate drops from 95% to 80%, database load goes
  from 1,000 to 4,000 queries/s -- four times higher from a "small" 15-point drop. Watch hit rate
  first. If Redis dies, a circuit breaker skips it quickly and the database sheds excess load
  instead of collapsing.

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Cache | a fast copy of data that lives somewhere slower | the sticky note with your wifi password instead of the router manual |
| Hit / miss | the answer was / was not in the cache | the book is on your desk / you walk to the library |
| Hit rate | share of requests answered from the cache | how often the book is already on your desk |
| TTL | how long a copy is trusted before it expires | a "best before" date on milk |
| Cache-aside | the app checks the cache, and on a miss loads the DB and fills the cache | you look in the fridge; if empty, you shop and restock it |
| Write-through / write-behind | on write, update the cache and DB together / cache now, DB later | paying at the till now / running a tab you settle later |
| Invalidation | deleting a copy because the real data changed | crossing out an old phone number in your address book |
| Stale data | a cached copy older than the real data | yesterday's newspaper |
| Stampede (thundering herd) | many requests miss at once and all hit the DB | the whole office rushing to one coffee machine at 9:00 |
| Singleflight | one request fetches, the others wait for its result | one person goes to buy lunch for the team |
| Eviction (LRU / LFU) | throwing items out when memory is full: least recently / least often used | clearing your closet of clothes you haven't worn lately / wear rarely |
| Admission (TinyLFU) | only let a new item in if it looks more popular than the one it replaces | a club bouncer comparing the newcomer with who would have to leave |
| Consistent hashing | a way to split keys across servers so adding one moves few keys | seating guests by table so a new table only moves a few people |
| Negative caching | remembering "this does not exist" | a note on the door: "no, we don't sell stamps" |
| Circuit breaker | stop calling a broken cache for a while | the fuse that cuts power before the wires burn |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| read:write ratio | reads per write for a piece of data | 10:1 – 1000:1 | profile read 1,000 times per edit |
| QPS | requests (queries) per second | 1k – 1M | 20,000 product reads/s |
| hit rate `h` | `hits / (hits + misses)` | 80 – 99% | 19,000 hits of 20,000 reads = 95% |
| miss rate | `1 - h`; the share that reaches the DB | 1 – 20% | 5% of 20,000 = 1,000 DB queries/s |
| `t_hit`, `t_miss` | time to serve from cache / from DB after a miss | 0.5 ms / 10 ms | average = `h x t_hit + (1-h) x (t_hit + t_miss)` = 1.0 ms at 95% |
| p50 / p99 | half / 99% of requests are faster than this | Redis: <1 ms / <5 ms | p99 = 4 ms: 1 in 100 reads is slower than 4 ms |
| RTT | network round-trip time to the cache | 0.1 – 1 ms | 100 GETs x 0.5 ms = 50 ms without pipelining |
| TTL (`ttl`, `EX`) | seconds until a cached entry expires | 30 s – 1 h | `SET key v EX 300` = 5 minutes |
| jitter | random seconds added to each TTL | 10 – 20% of TTL | `300 + random(0, 60)` |
| `stale_ttl` | how long a stale value may still be served (§3.5) | 2x TTL | fresh 300 s, servable until 600 s |
| `lock_ttl` | how long a rebuild lock lives before auto-release (§4.2) | 5 – 10 s | `SET lock:k 1 NX EX 10` |
| `expiry` | absolute time the entry expires (§4.3) | — | stored_at + 300 s |
| `delta` | how long the last rebuild took (§4.3) | 10 ms – 2 s | the trending query took 0.2 s |
| `beta` | XFetch eagerness; >1 refreshes earlier | 1.0 | beta = 2 doubles the average look-ahead |
| `rand()` | uniform random number in (0, 1] | — | `-ln(rand())` averages 1 |
| `N` (items) | number of unique cached items | 1M – 1B | 10 million user profiles |
| `key_size`, `value_size` | average bytes per key / value | 20 – 50 B / 100 B – 10 KB | `user:profile:12345` = 18 B |
| overhead | Redis bookkeeping bytes per key | ~80 – 100 B | 100 B on a 500 B profile |
| replication factor | copies of each item (primary + replicas) | 1 – 2 | 6.25 GB x 2 = 12.5 GB |
| headroom | spare memory kept free | 20% | 12.5 GB x 1.2 = 15 GB |
| `N` (nodes) | number of cache servers when sharding (§8) | 3 – 100 | `hash(key) % N` |
| vnodes | points per server on the hash ring | 100 – 200 | 150 vnodes: each of 3 nodes gets ~32–34% |
| hash slots | Redis Cluster's fixed key buckets | 16,384 | `CRC16(key) % 16384` |
| `maxmemory-samples` | keys Redis samples per eviction | 5 | pick the oldest of 5 random keys |
| eviction rate | keys thrown out per second for lack of memory | ~0 when healthy | 1,000/s sustained → cache too small |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. Mental Models

### 1.1 Cache Is a Bet

Every cache is a bet: you are wagering that the data you stored will be requested again before it becomes stale, and that the cost of occasional staleness is lower than the cost of always fetching fresh data. This is not a technical detail -- it is the foundational tradeoff that drives every caching decision. You are trading consistency for latency, memory for compute, simplicity for throughput.

When an interviewer asks "would you add a cache here?", they are really asking: "do you understand the cost of this tradeoff?" The answer is never just "yes, add Redis." It is "yes, because the read-to-write ratio is 100:1, staleness of 30 seconds is acceptable for this data, and the database is the bottleneck at our projected QPS."

### 1.2 The Three Fundamental Questions

Every caching discussion reduces to three questions. If you can answer these clearly for a given system, you have a complete caching design.

**Question 1: What to cache?**
Not everything benefits from caching. Cache data that is read frequently, expensive to compute or fetch, tolerant of staleness, and relatively stable. A user's profile (read 1000x per write) is an excellent cache candidate. A bank account balance (read and written with equal frequency, zero staleness tolerance) is a terrible one.

**Question 2: When to invalidate?**
This is where most caching systems break. The data in your cache will become stale -- the question is how you detect and handle that. Every invalidation strategy is a point on the spectrum between "never stale, always slow" and "sometimes stale, always fast."

**Question 3: What happens on miss?**
A cache miss is not just "go to the database." It is a latency spike, a potential thundering herd, a cold-start problem, and a capacity planning concern. Your miss path is your degraded path, and it must be designed as carefully as your hit path.

### 1.3 The Read/Write Ratio Rule of Thumb

Caching delivers value proportional to the read-to-write ratio. At 1:1 (equal reads and writes), caching adds complexity with minimal benefit -- every write invalidates the cached value, and the next read must repopulate it. At 10:1, caching starts to pay off. At 100:1 or higher (user profiles, product catalogs, configuration data), caching is almost always the right choice.

```
READ-TO-WRITE RATIO AND CACHE VALUE:

  1:1     Caching adds overhead, minimal benefit. Skip unless compute is extreme.
  10:1    Moderate benefit. Cache if latency matters.
  100:1   Strong benefit. Cache is almost always correct.
  1000:1  Massive benefit. Not caching is likely a bug.

  Exception: even at 1:1, cache if the computation is expensive (ML inference,
  complex aggregations) and staleness is acceptable.
```

---

## 2. Caching Strategies -- The Big Five

> **In plain words.** There are only a few ways to wire a cache to a database. Either the app fills the cache itself on a miss (cache-aside), or the cache does it (read-through). On writes, you update both at once (write-through), update the cache and the DB later (write-behind), or refresh popular items before they expire (refresh-ahead).
>
> **Real-world example.** A chat app caches user profiles with cache-aside: 50,000 profile reads/s, 95% hits, so only 2,500/s reach the DB. The same app counts message views with write-behind: 100 view increments are batched into 1 DB write, cutting DB writes 100x at the risk of losing a few seconds of counts on a crash.

There are five fundamental patterns for how a cache interacts with the backing data store. Every production caching system is one of these, or a hybrid. You must know all five, their tradeoffs, and when to reach for each.

### 2.1 Cache-Aside (Lazy Loading)

The application manages the cache explicitly. On a read, the application checks the cache first. On a miss, it reads from the database, writes the result into the cache, and returns. On a write, the application writes to the database and either invalidates or updates the cache entry.

```
CACHE-ASIDE READ PATH:

  Application ──── 1. GET key ────> Cache
       |                              |
       |           2a. HIT ──────────┘  (return cached value)
       |
       |           2b. MISS
       |
       └──── 3. SELECT ... ──────> Database
       |                              |
       |           4. result ────────┘
       |
       └──── 5. SET key result ──> Cache
       |
       └──── 6. return result to caller
```

```python
# Cache-aside in Python (production pattern)
import redis
import json
import hashlib
from typing import Optional, Any

class CacheAside:
    def __init__(self, redis_client: redis.Redis, default_ttl: int = 300):
        self.cache = redis_client
        self.default_ttl = default_ttl

    def get(self, key: str) -> Optional[Any]:
        """Cache-aside read: the application loads the DB on a miss."""
        # Step 1: Check cache
        cached = self.cache.get(key)
        if cached is not None:
            return json.loads(cached)
        if self.cache.exists(f"neg:{key}"):
            return None  # Known-missing key: negative-cache hit, skip the DB

        # Step 2: Cache miss — fetch from source
        value = self._fetch_from_db(key)
        if value is None:
            # Cache negative result to prevent cache penetration
            self.cache.setex(f"neg:{key}", 60, "null")
            return None

        # Step 3: Populate cache
        self.cache.setex(key, self.default_ttl, json.dumps(value))
        return value

    def invalidate(self, key: str):
        """Invalidate on write — prefer delete over update."""
        self.cache.delete(key)

    def _fetch_from_db(self, key: str):
        # Application-specific DB query
        pass
```

**Pros**: Only caches data that is actually requested (no wasted memory). Application has full control over caching logic. Works with any database. Most commonly used pattern in practice.

**Cons**: First request for any key is always a cache miss (cold start). Application code is more complex -- every read path must handle cache logic. Stale data is possible if writes bypass the cache invalidation path (e.g., direct database updates, batch jobs).

**When to use**: This is the default choice. Use cache-aside unless you have a specific reason to use one of the other patterns. It is the most flexible and the easiest to reason about.

### 2.2 Read-Through

The cache itself is responsible for loading data on a miss. The application only ever talks to the cache, never directly to the database. The cache has a built-in loader function that fetches from the backing store when a key is not found.

```
READ-THROUGH:

  Application ──── 1. GET key ────> Cache
                                      |
                    2a. HIT ─────────┘  (return cached value)
                                      |
                    2b. MISS          |
                                      |
                    3. SELECT ... ──> Database
                                      |
                    4. store + return ┘
```

**Pros**: Simpler application code -- the app does not manage cache misses. Cache logic is centralized in the cache layer. Easier to swap cache implementations.

**Cons**: Cold start is still a problem. The cache must know how to fetch data (coupling). Harder to implement complex loading logic (joins across tables, data from multiple sources). Most remote caches (Redis, Memcached) do not natively support read-through -- you need a wrapper library or sidecar.

**When to use**: When you want a clean abstraction and the loading logic is simple. Common in in-process caches like Caffeine (Java) or go-cache (Go) that support loader functions natively.

### 2.3 Write-Through

Every write goes to both the cache and the database synchronously. The write is only considered successful when both the cache and the database have been updated.

```
WRITE-THROUGH:

  Application ──── 1. write(key, val) ────> Cache
                                              |
                   2. write to DB ──────────> Database
                                              |
                   3. both ACK ──────────────┘
                   4. return success
```

**Pros**: Cache is consistent with the database after each successful write (a writer reads its own writes). This is not strict consistency: concurrent writers, a failed second step, or a write that bypasses the cache can still leave the two out of sync. Simplifies the read path since the cache is usually fresh. Combined with read-through, gives you a complete caching abstraction.

**Cons**: Write latency increases -- every write must wait for both cache and DB. Caches data that may never be read (wasted memory). If the cache and DB write are not atomic, partial failures create inconsistency. Not suitable for write-heavy workloads.

**When to use**: When read-after-write consistency is critical and write volume is low to moderate. Common in systems where users expect to see their own writes immediately (user profile updates, settings changes).

### 2.4 Write-Behind (Write-Back)

Writes go to the cache immediately, and the cache asynchronously flushes to the database in the background. The write returns success as soon as the cache is updated.

```
WRITE-BEHIND:

  Application ──── 1. write(key, val) ────> Cache
                                              |
                   2. ACK (immediate) ───────┘
                                              |
                   3. async flush ──────────> Database
                      (batched, delayed)
```

**Pros**: Extremely fast writes (memory-speed). Write batching reduces database load -- 100 individual writes can become 1 batch write. Absorbs write spikes without overwhelming the database.

**Cons**: Data loss risk -- if the cache node crashes before flushing, unflushed writes are lost. Complexity of managing the async write queue (ordering, retries, failure handling). Debugging is harder because the database lags behind the cache. Not suitable for data that requires strong durability guarantees.

**When to use**: Write-heavy workloads where some data loss is acceptable (analytics counters, view counts, activity feeds). Also used in CPU caches and OS page caches, where the hardware guarantees are different. In application-level caching, use with caution and always with a write-ahead log or replication for durability.

### 2.5 Refresh-Ahead

The cache proactively refreshes entries before their TTL expires. When an entry is accessed and its remaining TTL is below a threshold (e.g., 20% of original TTL remaining), the cache triggers an asynchronous refresh in the background. The stale value is returned immediately while the refresh happens.

```
REFRESH-AHEAD:

  Application ──── GET key ────> Cache
                                   |
       ┌── return stale value ────┘
       |                           |
       |   TTL remaining < threshold?
       |          YES              |
       |                           |
       |   async refresh ────────> Database
       |                           |
       |   update cache <─────────┘
       |
       └── caller sees no latency spike
```

**Pros**: Eliminates cache miss latency for frequently accessed keys. Users always get a fast response. Works well for data that is accessed in predictable patterns.

**Cons**: Wasted refreshes for keys that are accessed once and never again. Increases load on the database (proactive fetches even when the cached value might never be requested again). More complex to implement. Requires good heuristics for the refresh threshold.

**When to use**: Hot keys with predictable access patterns (homepage content, trending items, feature flags). Not suitable for long-tail data where most keys are accessed rarely.

### 2.6 Strategy Comparison

```
┌──────────────────┬────────────┬──────────┬───────────┬──────────────┬──────────────┐
│ Strategy         │ Read       │ Write    │ Consis-   │ Complexity   │ Best For     │
│                  │ Latency    │ Latency  │ tency     │              │              │
├──────────────────┼────────────┼──────────┼───────────┼──────────────┼──────────────┤
│ Cache-Aside      │ Miss: high │ Low      │ Eventual  │ Medium       │ General      │
│                  │ Hit: low   │          │           │              │ purpose      │
├──────────────────┼────────────┼──────────┼───────────┼──────────────┼──────────────┤
│ Read-Through     │ Miss: high │ Low      │ Eventual  │ Low (app)    │ Simple read  │
│                  │ Hit: low   │          │           │ High (cache) │ patterns     │
├──────────────────┼────────────┼──────────┼───────────┼──────────────┼──────────────┤
│ Write-Through    │ Hit: low   │ High     │ Read-your-│ Medium       │ Read-after-  │
│                  │            │ (2x)     │ writes*   │              │ write needs  │
├──────────────────┼────────────┼──────────┼───────────┼──────────────┼──────────────┤
│ Write-Behind     │ Hit: low   │ Very low │ Eventual  │ High         │ Write-heavy  │
│                  │            │          │ (weak)    │              │ workloads    │
├──────────────────┼────────────┼──────────┼───────────┼──────────────┼──────────────┤
│ Refresh-Ahead    │ Always low │ Low      │ Eventual  │ High         │ Hot keys,    │
│                  │ (no miss)  │          │           │              │ predictable  │
└──────────────────┴────────────┴──────────┴───────────┴──────────────┴──────────────┘
```

\* Write-through gives read-your-writes on the happy path, not linearizability: races between concurrent writers and partial failures (DB write OK, cache write failed) can still leave stale entries, so keep a TTL.

**Decision matrix**: Start with cache-aside (it is the default). Add write-through if you need read-after-write consistency. Use write-behind only for write-heavy workloads where you can tolerate data loss. Add refresh-ahead for known hot keys. In practice, most production systems use cache-aside with TTL-based invalidation and event-driven invalidation for critical paths.

---

## 3. Cache Invalidation

> **In plain words.** A cached copy goes out of date the moment the real data changes. Invalidation is how you get rid of old copies: let them expire after a time (TTL), delete them when a change event arrives, or change the key so old copies are never read again.
>
> **Real-world example.** A bank app caches account settings for 10 minutes. A user changes their phone number; a change event deletes `settings:42` within ~50 ms. If that event is lost, the 10-minute TTL still guarantees the old number disappears within 10 minutes.

Phil Karlton famously said there are only two hard things in computer science: cache invalidation and naming things. He was right about the first one. Invalidation is where caching systems break, and it is the part interviewers probe deepest.

### 3.1 TTL-Based Invalidation

The simplest approach: every cache entry has a time-to-live (TTL). After the TTL expires, the entry is evicted (or marked stale). The next read triggers a fresh fetch.

```
TTL LIFECYCLE:

  t=0s     SET key value EX 300    (TTL = 5 minutes)
  t=120s   GET key → HIT           (180s remaining)
  t=250s   GET key → HIT           (50s remaining)
  t=300s   GET key → MISS          (expired, fetch from DB)
  t=300s   SET key new_value EX 300 (reset TTL)
```

**Choosing the right TTL** is a balancing act:

- **Too short** (seconds): High miss rate, increased DB load, cache provides little benefit. You are paying for cache infrastructure without getting much return.
- **Too long** (hours/days): Stale data accumulates. Users see outdated information. Bugs caused by stale data are notoriously hard to diagnose.
- **Sweet spot**: Depends on the data. User profiles: 5-15 minutes. Product catalog: 1-5 minutes. Configuration/feature flags: 30-60 seconds. Session data: match session timeout.

**TTL jitter**: Always add random jitter to TTLs to prevent synchronized mass expiration (cache avalanche). Instead of `TTL = 300`, use `TTL = 300 + random(0, 60)`.

### 3.2 Event-Based Invalidation

On every write to the database, publish an invalidation event. Cache nodes (or the application) subscribe to these events and delete or update the corresponding cache entries.

```
EVENT-BASED INVALIDATION:

  Writer ──── UPDATE users SET name='Bob' WHERE id=42 ──> Database
    |                                                        |
    └──── PUBLISH user:42:updated ──────────────────────> Message Bus
                                                             |
                                              ┌──────────────┼──────────────┐
                                              v              v              v
                                           Cache-1        Cache-2       Cache-3
                                           DEL user:42    DEL user:42   DEL user:42
```

**Implementation approaches**:

- **Application-level**: The application publishes events after writing. Simple but requires discipline -- every write path must remember to publish. Missed events cause stale data.
- **Change Data Capture (CDC)**: Capture database write-ahead log (WAL/binlog) changes and publish them. Debezium for Kafka, DynamoDB Streams, PostgreSQL logical replication. More reliable because the database itself is the event source. Recommended for systems at scale.
- **Database triggers**: The database fires a trigger on write that sends an invalidation. Tight coupling, operational burden, but guarantees no missed events.

### 3.3 Version-Based Invalidation

Instead of invalidating by key, embed a version number in the cache key itself. When data changes, increment the version. Old cache entries are never explicitly deleted -- they simply become unreachable and are eventually evicted by the eviction policy.

```python
# Version-based invalidation
def get_user_profile(user_id: int) -> dict:
    # Version is stored in a fast lookup (Redis, or a separate small cache)
    version = cache.get(f"user:{user_id}:version")  # e.g., "7"
    cache_key = f"user:{user_id}:v{version}"

    profile = cache.get(cache_key)
    if profile:
        return json.loads(profile)

    profile = db.query("SELECT * FROM users WHERE id = %s", user_id)
    cache.setex(cache_key, 3600, json.dumps(profile))
    return profile

def update_user_profile(user_id: int, data: dict):
    db.execute("UPDATE users SET ... WHERE id = %s", user_id)
    cache.incr(f"user:{user_id}:version")  # Old cache key is now orphaned
```

**Pros**: No explicit invalidation needed. No race conditions between invalidation and concurrent reads. Works well for immutable data with version identifiers.

**Cons**: Orphaned cache entries consume memory until evicted. Requires an extra lookup for the version on every read. Version counter itself needs to be consistent.

### 3.4 Lease-Based Invalidation

A lease is a time-limited token that grants exclusive permission to populate a cache entry. When a cache miss occurs, the cache issues a lease to the first requester. Other concurrent requesters either wait for the lease holder to populate the cache, or receive a stale value with a "stale" flag. This directly prevents thundering herds (covered in detail in section 4).

Facebook's Memcache paper (2013) describes this approach in production at enormous scale. The lease also acts as an invalidation mechanism: if an invalidation event arrives while a lease is outstanding, the lease is revoked. The lease holder's subsequent SET is rejected, preventing it from writing stale data that was fetched before the invalidation.

### 3.5 Stale-While-Revalidate

Serve the stale cached value immediately while asynchronously refreshing the entry in the background. The caller gets a fast (possibly stale) response, and the cache is updated for subsequent requests.

```python
import threading
import time

class StaleWhileRevalidate:
    def __init__(self, cache, ttl=300, stale_ttl=600):
        self.cache = cache
        self.ttl = ttl           # Fresh window
        self.stale_ttl = stale_ttl  # Stale-but-servable window
        self._refreshing = set()

    def get(self, key: str, fetch_fn):
        entry = self.cache.get(key)
        if entry is None:
            # Hard miss — must fetch synchronously
            value = fetch_fn(key)
            self._store(key, value)
            return value

        data = json.loads(entry)
        age = time.time() - data["stored_at"]

        if age < self.ttl:
            # Fresh — return immediately
            return data["value"]

        if age < self.stale_ttl:
            # Stale but servable — return immediately, refresh async
            self._async_refresh(key, fetch_fn)
            return data["value"]

        # Beyond stale window — treat as hard miss
        value = fetch_fn(key)
        self._store(key, value)
        return value

    def _async_refresh(self, key, fetch_fn):
        if key in self._refreshing:
            return  # Already refreshing
        self._refreshing.add(key)
        def _do_refresh():
            try:
                value = fetch_fn(key)
                self._store(key, value)
            finally:
                self._refreshing.discard(key)
        threading.Thread(target=_do_refresh, daemon=True).start()

    def _store(self, key, value):
        self.cache.setex(key, self.stale_ttl, json.dumps({
            "value": value,
            "stored_at": time.time()
        }))
```

This pattern is widely used in CDNs (the HTTP `stale-while-revalidate` Cache-Control directive) and in application-level caches for data where a few seconds of staleness is acceptable.

### 3.6 The Production Sweet Spot: TTL + Event-Based

In practice, the most robust invalidation approach combines TTL as a safety net with event-based invalidation for freshness.

- **Event-based** invalidation handles the happy path: when a write occurs, the cache is invalidated promptly (typically within milliseconds to low seconds via pub/sub).
- **TTL** handles the failure cases: if an event is lost (message bus hiccup, subscriber crashed, network partition), the TTL ensures the stale entry is eventually evicted. The TTL is your upper bound on staleness.

This is what most production systems at scale use. The event-based path keeps data fresh under normal operation, and the TTL prevents unbounded staleness when events are lost.

---

## 4. Thundering Herd and Cache Stampede

> **In plain words.** When a very popular cached item expires, hundreds of requests miss at the same moment and all ask the database for the same thing. The fixes: let only one request rebuild it (lock or singleflight), or rebuild it a little early, before it expires.
>
> **Real-world example.** A video platform caches the "trending" list. It gets 2,000 reads/s and takes 0.5 s to rebuild. On expiry, 2,000 x 0.5 = 1,000 identical queries hit the database. With singleflight on 20 servers, at most 20 queries run; with early refresh, usually 1-3.

### 4.1 The Problem

A popular cache key expires. In the instant before it is repopulated, thousands of concurrent requests arrive, all experience a cache miss simultaneously, and all independently query the database for the same data. The database receives thousands of identical queries at once. At best, this causes a latency spike. At worst, it takes down the database.

```
THE THUNDERING HERD:

  t=0     Cache entry for "trending_posts" expires (TTL = 300s)

  t=0.001 Request 1:  cache MISS → query DB
  t=0.002 Request 2:  cache MISS → query DB
  t=0.003 Request 3:  cache MISS → query DB
  ...
  t=0.050 Request 10,000: cache MISS → query DB

  t=0.100 Database receives 10,000 identical queries simultaneously
          Connection pool exhausted. Latency spikes. Possible crash.

  t=0.500 Request 1 returns, writes to cache
  t=0.501 Requests 2-10,000 also return (wasted work), overwrite cache

  Total: 10,000 DB queries for data that could have been served by 1.
```

This is not a theoretical problem. It happens in production at any meaningful scale, especially for hot keys (homepage content, trending items, popular user profiles).

### 4.2 Solution 1: Mutex/Lock

Only one request is allowed to fetch from the database. All other concurrent requests wait for the first one to populate the cache.

```python
import redis
import time
import json

class MutexCache:
    def __init__(self, cache: redis.Redis, lock_ttl: int = 10):
        self.cache = cache
        self.lock_ttl = lock_ttl

    def get_with_lock(self, key: str, fetch_fn, ttl: int = 300):
        # Try cache first
        value = self.cache.get(key)
        if value is not None:
            return json.loads(value)

        # Cache miss — try to acquire lock
        lock_key = f"lock:{key}"
        acquired = self.cache.set(lock_key, "1", nx=True, ex=self.lock_ttl)

        if acquired:
            try:
                # We won the lock — fetch from DB and populate cache
                result = fetch_fn(key)
                self.cache.setex(key, ttl, json.dumps(result))
                return result
            finally:
                self.cache.delete(lock_key)
        else:
            # Another request is fetching — wait and retry
            for _ in range(50):  # Max 5 seconds of waiting
                time.sleep(0.1)
                value = self.cache.get(key)
                if value is not None:
                    return json.loads(value)
            # Fallback: fetch from DB directly (lock holder may have failed)
            return fetch_fn(key)
```

**Tradeoff**: Serializes requests for the same key. If the lock holder is slow or crashes, waiting requests are delayed. The lock TTL must be tuned carefully -- too short and the lock expires before the fetch completes; too long and a crashed lock holder blocks everyone. The sample deletes the lock unconditionally; in production store a random token as the lock value and delete only if it still matches (a small Lua script), so a slow holder never deletes someone else's lock.

### 4.3 Solution 2: Probabilistic Early Expiration (PER)

Instead of all entries expiring at exactly the same time, each access independently decides whether to refresh the entry early, with the probability increasing as the TTL approaches. This spreads out the refresh load.

The standard algorithm is **XFetch** from "Optimal Probabilistic Cache Stampede Prevention" (Vattani, Chierichetti, Lowenstein, VLDB 2015). Store two extra numbers with each entry: `delta` (how long the last recomputation took) and `expiry`. On every read, recompute early if:

```
now - delta * beta * ln(rand()) >= expiry        rand() uniform in (0, 1]
```

Because `ln(rand())` is negative, `-delta * beta * ln(rand())` is a random "look-ahead" with average `delta * beta`. With `remaining = expiry - now`, the chance that one request triggers a refresh is `exp(-remaining / (delta * beta))`: essentially zero far from expiry, rising exponentially as expiry approaches. `beta = 1` is the paper's default; `beta > 1` refreshes earlier.

```python
import math
import random
import time

def should_refresh_early(expiry: float, delta: float, beta: float = 1.0) -> bool:
    """
    XFetch (Vattani et al., VLDB 2015).
    expiry: absolute time the entry expires (seconds since epoch)
    delta:  how long the last recomputation took (seconds)
    beta:   >1 refreshes earlier, <1 later; 1.0 is the default
    """
    now = time.time()
    # 1.0 - random.random() is in (0, 1], so log() never sees 0
    return now - delta * beta * math.log(1.0 - random.random()) >= expiry
```

**Worked example** (checked with python): a hot key gets 1,000 reads/s, recomputing it takes `delta = 0.2 s`, `beta = 1`. Per request, refresh probability is `e^-10 ≈ 0.00005` at 2 s before expiry, `e^-5 ≈ 0.007` at 1 s, and `e^-1 ≈ 0.37` at 0.2 s. The first early refresh typically fires about `delta * ln(rate * delta) = 0.2 * ln(200) ≈ 1.06 s` before expiry, and a simulation averages about 2.7 recomputations per expiry cycle. Without PER, all reads in the 0.2 s after expiry miss: about 1,000 x 0.2 = 200 identical DB queries.

**Tradeoff**: No locks, no coordination. But multiple requests may still refresh simultaneously (a few, instead of hundreds). Works best for high-traffic keys where the probabilistic spread is effective; a key read once a minute rarely gets an early refresh.

### 4.4 Solution 3: Request Coalescing (Singleflight)

Multiple concurrent requests for the same key are coalesced into a single fetch. The first request triggers the actual work; all subsequent requests for the same key wait for the result of the first one. This is the singleflight pattern, popularized by Go's `golang.org/x/sync/singleflight` package.

```go
// Singleflight in Go — the canonical implementation
package cache

import (
    "context"
    "encoding/json"
    "sync"
    "time"

    "github.com/redis/go-redis/v9"
    "golang.org/x/sync/singleflight"
)

type Cache struct {
    rdb    *redis.Client
    group  singleflight.Group
    ttl    time.Duration
}

func NewCache(rdb *redis.Client, ttl time.Duration) *Cache {
    return &Cache{rdb: rdb, group: singleflight.Group{}, ttl: ttl}
}

func (c *Cache) Get(ctx context.Context, key string, fetchFn func() (any, error)) (any, error) {
    // Try cache first
    val, err := c.rdb.Get(ctx, key).Result()
    if err == nil {
        var result any
        json.Unmarshal([]byte(val), &result)
        return result, nil
    }

    // Cache miss — use singleflight to coalesce concurrent fetches
    // All concurrent callers with the same key block here
    // Only ONE of them actually executes fetchFn
    result, err, shared := c.group.Do(key, func() (any, error) {
        // Double-check cache (another goroutine may have populated it)
        val, err := c.rdb.Get(ctx, key).Result()
        if err == nil {
            var r any
            json.Unmarshal([]byte(val), &r)
            return r, nil
        }

        // Actually fetch from DB
        data, err := fetchFn()
        if err != nil {
            return nil, err
        }

        // Populate cache
        encoded, _ := json.Marshal(data)
        c.rdb.Set(ctx, key, encoded, c.ttl)
        return data, nil
    })

    // 'shared' is true if this caller waited for another's result
    _ = shared
    return result, err
}
```

```python
# Singleflight in Python using asyncio
import asyncio
from typing import Any, Callable, Awaitable, Dict

class SingleFlight:
    """Coalesce concurrent requests for the same key into a single fetch."""

    def __init__(self):
        self._in_flight: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def do(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        async with self._lock:
            future = self._in_flight.get(key)
            is_leader = future is None
            if is_leader:
                future = asyncio.get_running_loop().create_future()
                self._in_flight[key] = future

        if not is_leader:
            # Another coroutine is already fetching — wait for its result
            # (outside the lock, so other keys are not blocked meanwhile)
            return await future

        try:
            result = await fn()
            future.set_result(result)
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            async with self._lock:
                del self._in_flight[key]


# Usage
flight = SingleFlight()

async def get_user(user_id: int):
    key = f"user:{user_id}"

    cached = await redis.get(key)
    if cached:
        return json.loads(cached)

    # Even if 10,000 concurrent requests in THIS process call this for the
    # same user_id, only ONE database query executes (per process)
    result = await flight.do(key, lambda: db.fetch_user(user_id))

    await redis.setex(key, 300, json.dumps(result))
    return result
```

**Singleflight is the recommended first defense**. It is simple, deterministic (no probabilistic behavior), and reduces N concurrent requests to 1 database query **per process**. Note the scope: with 50 app servers, a hot-key expiry can still send up to 50 queries to the database. Combine it with a distributed lock (§4.2), early refresh (§4.3), or stale-while-revalidate (§3.5) when that fan-in is still too much.

---

## 5. Cache Eviction Policies

> **In plain words.** Memory is limited, so when the cache is full something must go. LRU throws out what was used longest ago. LFU throws out what is used least often. TinyLFU adds a doorman: a new item only gets in if it looks more popular than the one it would push out.
>
> **Real-world example.** An e-commerce cache holds 1 million products, but 20 million exist. A nightly report reads all 20 million once. With plain LRU, that scan pushes out the popular items and the hit rate drops sharply the next morning. With LFU or TinyLFU, the one-time reads never beat the popular items, so the hit rate barely moves.

When the cache is full and a new entry must be inserted, the eviction policy decides which existing entry to remove. The choice of eviction policy has a significant impact on hit rate, which is the single most important cache metric.

### 5.1 LRU (Least Recently Used)

Evict the entry that has not been accessed for the longest time. The assumption is that recently accessed data is likely to be accessed again soon (temporal locality).

**Implementation**: Typically a hash map (for O(1) lookup) combined with a doubly-linked list (for O(1) eviction and promotion). On every access, the entry is moved to the head of the list. On eviction, the tail entry is removed.

**Strengths**: Simple, effective for workloads with temporal locality, well-understood behavior.

**Weaknesses**: A single full scan (e.g., a batch job reading every key once) can evict the entire hot working set. No frequency information -- an item accessed once recently beats an item accessed 1,000 times but not in the last minute.

### 5.2 LFU (Least Frequently Used)

Evict the entry with the lowest access count. Entries that are accessed often survive; entries accessed rarely are evicted.

**Strengths**: Resistant to scan pollution. Popular items stay cached even if not accessed in the most recent window.

**Weaknesses**: New items have low counts and are evicted immediately, even if they would become popular (the "cold start" problem). Items that were popular in the past but are no longer relevant hold their spots (the "cache pollution" problem). Maintaining accurate counts is expensive at scale.

### 5.3 ARC (Adaptive Replacement Cache)

Maintains two LRU lists -- one for items accessed once ("recency") and one for items accessed more than once ("frequency") -- plus two "ghost" lists that remember only the keys of recently evicted items. A hit in a ghost list tells ARC which side was too small, and it shifts the partition toward that side. Invented at IBM (Megiddo and Modha, FAST 2003) and patented by IBM.

**Strengths**: Adapts to workload changes. Handles both recency-biased and frequency-biased access patterns. Scan-resistant.

**Weaknesses**: More complex to implement. The IBM patent kept some open-source projects away from it for years (PostgreSQL briefly shipped ARC in 8.0, then replaced it). Higher per-operation overhead than simple LRU.

### 5.4 TinyLFU (The Modern Standard)

The key insight of TinyLFU is to separate the **admission policy** from the **eviction policy**. It uses a frequency sketch (Count-Min Sketch) to cheaply estimate access frequencies, and only admits a new item to the cache if its estimated frequency exceeds that of the item it would replace.

W-TinyLFU (Windowed TinyLFU), used in Caffeine (Java's best in-process cache), combines:
1. **Window cache** (starts at 1% of capacity, LRU): Admits all new entries, giving them a chance to build up frequency. Caffeine adapts the window size at runtime (hill climbing) for recency-heavy workloads.
2. **Main cache** (the other ~99%, segmented LRU: a "probation" segment and a "protected" segment of about 80% of the main cache): An item evicted from the window is only admitted to the main cache if TinyLFU's frequency sketch says it is more popular than the main cache's eviction candidate; the loser is evicted.
3. **Count-Min Sketch**: A space-efficient probabilistic data structure that estimates item frequencies using 4 hash functions and small 4-bit counters. When the total number of recorded accesses reaches a sample size (about 10x the cache capacity), every counter is halved (aging), so old popularity fades.

```
W-TinyLFU ARCHITECTURE (Caffeine):

  New item ──> [ Window LRU (1%) ]
                      |
                      v
              TinyLFU Admission Filter
              "Is new item's frequency > eviction candidate's frequency?"
                    /          \
                  YES           NO
                  /               \
       [ Main Segmented LRU ]   Rejected (evicted)
       [     (99%)          ]

  Frequency estimation: Count-Min Sketch (4 hash functions, periodic aging)
```

**Why TinyLFU beats pure LRU for most workloads**: Real-world cache access patterns follow power-law distributions (Zipfian). A small number of items are accessed very frequently, and a long tail of items are accessed rarely. LRU wastes cache space on long-tail items that happen to be accessed recently. TinyLFU's admission filter keeps these out, reserving cache space for genuinely popular items. In the published trace benchmarks (Einziger, Friedman, Manes, "TinyLFU", ACM ToS 2017, and Caffeine's simulator), W-TinyLFU matches or beats LRU, LFU, and ARC on most traces; the size of the gain depends heavily on the workload and cache size.

### 5.5 FIFO Is Back: S3-FIFO and SIEVE (2023–2024)

For decades the rule was "FIFO is poor, LRU is the baseline, anything better is complex". Two
papers from the same group (Juncheng Yang et al.) overturned that with algorithms built from FIFO
queues:

- **S3-FIFO** (SOSP 2023) uses three FIFO queues: a **small** queue (~10% of the space) that every
  new object enters, a **main** queue for objects that were hit again while in the small queue,
  and a **ghost** queue that remembers only the keys of recently evicted objects. Most objects are
  read once and never again ("one-hit wonders"), so the small queue throws them out *quickly*,
  before they push anything useful out of main. Across 6,594 production traces from 14 datasets,
  S3-FIFO had a lower miss ratio than each of the 12 state-of-the-art algorithms it was compared
  with, and it cut LRU's miss ratio by up to 72% on some traces.
- **SIEVE** (NSDI 2024) is even smaller: **one** FIFO queue, one "visited" bit per object and a
  "hand" that moves from the oldest object toward the newest. A hit only sets the bit. On
  eviction the hand clears the bit on visited objects and keeps them *where they are*, then evicts
  the first unvisited one. New objects enter at the head, so objects that were never hit again
  are evicted quickly, as with S3-FIFO's small queue.

Two properties matter as much as the hit rate:

1. **A hit is a bit flip, not a list move.** LRU must move the entry to the head on *every read*,
   which means a lock (or lock-free tricks) on the hottest path. In FIFO-based caches reads don't
   touch the queue, so they scale across cores. That is the main reason in-process caches and
   proxies adopted them.
2. **Quick demotion.** Scan resistance comes from evicting new, unproven objects fast. You get
   most of what TinyLFU's admission filter gives without a frequency sketch.

```python
class _Node:
    __slots__ = ("key", "value", "visited", "prev", "next")

    def __init__(self, key, value):
        self.key, self.value, self.visited = key, value, False
        self.prev = self.next = None


class SieveCache:
    """SIEVE (NSDI 2024): one FIFO queue, one 'visited' bit per entry, one moving hand.
    A hit only sets a bit, so reads need no lock and no list reordering."""

    def __init__(self, capacity: int):
        self.capacity, self.map = capacity, {}
        self.head = self.tail = self.hand = None          # head = newest, tail = oldest

    def get(self, key):
        node = self.map.get(key)
        if node is None:
            return None
        node.visited = True                              # the whole "promotion"
        return node.value

    def put(self, key, value):
        if key in self.map:
            self.map[key].value = value
            self.map[key].visited = True
            return
        if len(self.map) >= self.capacity:
            self._evict()
        node = _Node(key, value)
        node.next, self.head = self.head, node           # insert at head
        if node.next:
            node.next.prev = node
        if self.tail is None:
            self.tail = node
        self.map[key] = node

    def _evict(self):
        node = self.hand or self.tail                    # resume where the hand stopped
        while node.visited:                              # survivors stay in place
            node.visited = False
            node = node.prev or self.tail                # move toward newer, wrap around
        self.hand = node.prev                            # next scan starts here
        if node.prev:
            node.prev.next = node.next
        else:
            self.head = node.next
        if node.next:
            node.next.prev = node.prev
        else:
            self.tail = node.prev
        del self.map[node.key]
```

Replaying the same synthetic trace (1 million requests over 100,000 keys, Zipf popularity)
through FIFO, LRU (`OrderedDict`) and the class above gives these miss ratios:

| Workload | Cache size | FIFO | LRU | SIEVE |
|---|---|---|---|---|
| Zipf α = 1.0 | 1,000 (1%) | 0.535 | 0.494 | **0.404** |
| Zipf α = 1.0 | 10,000 (10%) | 0.300 | 0.265 | **0.224** |
| Zipf α = 1.0 + a 20,000-key scan every 100,000 requests | 10,000 | 0.418 | 0.395 | **0.343** |
| Zipf α = 0.8 (flatter) | 10,000 | 0.573 | 0.533 | **0.463** |

At 10,000 entries, SIEVE sends **15% fewer requests to the database** than LRU (miss ratio
0.224 vs 0.265) with a simpler data structure. This is a synthetic trace. The papers' numbers
come from real traces, where the gap varies by workload, so replay your own access log before
switching.

**Where you will meet them.** Cloudflare's Pingora ships **TinyUFO**, a lock-free in-memory
cache that uses S3-FIFO for eviction and TinyLFU for admission. The authors report production use
at Google, VMware and Redpanda, among others, and SIEVE libraries exist for most languages.
**Redis and Valkey don't use them.** Their `allkeys-lru` / `allkeys-lfu` are sampled
approximations (§6.2), so for a remote cache the choice is still LRU vs LFU. The practical impact
is in-process caches (L1, §7.2), proxies and CDNs, and any cache you write yourself: start with
SIEVE, not a hand-rolled LRU.

### 5.6 Other Policies

- **FIFO (First In, First Out)**: Evict the oldest entry. Simple but ignores access patterns entirely. Plain FIFO is weak, but FIFO plus a visited bit or a small probation queue is state of the art (§5.5).
- **Random**: Evict a random entry. Surprisingly competitive with LRU for uniform access patterns and much simpler to implement. Used in some CPU cache designs. A scan does not flush the whole cache at once (each scanned item only has a small chance of pushing out a hot one), but it is not truly scan-resistant.
- **TTL-based eviction**: Evict entries closest to expiry. Not a standalone eviction policy -- usually combined with LRU/LFU as a secondary signal.

### 5.7 Eviction Policy Comparison

```
┌──────────┬──────────────┬──────────────┬────────────────┬────────────────────┐
│ Policy   │ Hit Rate     │ Scan         │ Implementation │ Used In            │
│          │ (typical)    │ Resistant?   │ Complexity     │                    │
├──────────┼──────────────┼──────────────┼────────────────┼────────────────────┤
│ LRU      │ Good         │ No           │ Low            │ Redis (approx.),   │
│          │              │              │                │ Memcached          │
│ LFU      │ Good         │ Yes          │ Medium         │ Redis (since 4.0)  │
│ ARC      │ Very good    │ Yes          │ High           │ ZFS (PG 8.0 only)  │
│ TinyLFU  │ Excellent    │ Yes          │ High           │ Caffeine (Java)    │
│ S3-FIFO  │ Excellent    │ Yes          │ Low            │ Pingora TinyUFO    │
│ SIEVE    │ Very good    │ Mostly       │ Very low       │ In-process libs    │
│ FIFO     │ Poor         │ N/A          │ Very low       │ Simple buffers     │
│ Random   │ Fair         │ Partly       │ Very low       │ CPU caches (some)  │
└──────────┴──────────────┴──────────────┴────────────────┴────────────────────┘
```

**Interview guidance**: Know LRU (it is the default everywhere), know why TinyLFU is better (admission filtering based on frequency estimation), and know ARC exists for completeness. If asked "which eviction policy would you use?", the answer is: LRU or LFU for a remote cache (in Redis set `maxmemory-policy allkeys-lru` or `allkeys-lfu` -- the default is `noeviction`), TinyLFU/Caffeine for an in-process cache (Java/JVM), SIEVE or S3-FIFO when you implement a cache yourself or need reads that don't take a lock (§5.5), and LRU with manual hot-key pinning for everything else.

---

## 6. Redis as a Cache -- Deep Dive

> **In plain words.** Redis is the most common cache server. It keeps everything in RAM, offers data types beyond plain strings (hashes, sorted sets, counters), and can be split across many servers (Cluster) or given automatic failover (Sentinel). A few settings decide whether it behaves as a good cache.
>
> **Real-world example.** A ride-hailing app stores driver locations and trip state in Redis. A fresh Redis install has `maxmemory-policy noeviction`: when memory fills up, writes start failing instead of old keys being evicted. Setting `allkeys-lru` makes Redis evict old keys and keep accepting writes.

Redis is the dominant cache technology in production systems. Interviewers expect you to know it beyond "it's a key-value store."

### 6.1 Data Structures

Redis is not just key-value. It is a data structure server, and choosing the right data structure for your cache is a design decision with significant performance implications.

- **String**: The basic type. `SET key value EX ttl`. Up to 512MB. Use for simple cached values (JSON blobs, serialized objects). Memory-efficient for values under 44 bytes (embedded encoding).

- **Hash**: A map of field-value pairs under a single key. `HSET user:42 name "Bob" email "bob@x.com"`. Use when you need to read/update individual fields without deserializing the entire value. Memory-efficient for small hashes (<=128 fields, <=64-byte values by default) via the compact listpack encoding (called ziplist before Redis 7.0).

- **Sorted Set (ZSet)**: An ordered set where each member has a score. `ZADD leaderboard 9500 "player:42"`. O(log N) insert and range queries. The go-to structure for leaderboards, rate limiters (sliding window), and any ranked data.

- **List**: Ordered collection, O(1) push/pop at both ends. Use for queues, recent items lists, activity feeds. `LPUSH recent:user:42 "post:789"` with `LTRIM` to cap length.

- **Set**: Unordered unique members. Use for tags, unique visitor tracking, set intersection/union operations (e.g., mutual friends).

- **HyperLogLog**: Probabilistic cardinality estimation. 12KB per key regardless of cardinality. `PFADD unique_visitors "user:42"`, `PFCOUNT unique_visitors`. Error rate ~0.81%. Use for unique counts at scale (unique visitors, unique search queries) where exact counts are not required.

- **Bitmap/Bitfield**: Bit-level operations on strings. Use for feature flags, user activity tracking (bit per day), bloom filter implementation.

### 6.2 Memory Management

Redis stores everything in memory. Understanding memory management is critical for cache sizing and operations.

**maxmemory-policy**: Determines what happens when Redis reaches its memory limit.

```
EVICTION POLICIES (maxmemory-policy):

  noeviction        Reject writes when memory is full (default — bad for cache)
  allkeys-lru       Evict LRU key from all keys (recommended for general cache)
  allkeys-lfu       Evict LFU key from all keys (better for skewed workloads)
  allkeys-random    Evict random key from all keys
  volatile-lru      Evict LRU key among keys with TTL set
  volatile-lfu      Evict LFU key among keys with TTL set
  volatile-random   Evict random key among keys with TTL set
  volatile-ttl      Evict key with shortest remaining TTL
```

**Recommendation**: Use `allkeys-lru` or `allkeys-lfu` for cache workloads. Note that Redis LRU/LFU are *approximate*: on each eviction Redis samples a few keys (`maxmemory-samples`, default 5) and evicts the best candidate among them, rather than keeping an exact global list. The `volatile-*` policies only evict keys with an explicit TTL, which means keys without TTL are never evicted -- dangerous if any cache population path forgets to set a TTL. The `allkeys-lfu` policy is better when access patterns are highly skewed (a few hot keys dominate), which is common in real-world workloads.

**Memory overhead per key**: Every Redis key carries metadata overhead beyond the value itself.

```
APPROXIMATE MEMORY OVERHEAD PER KEY (Redis 7.x):

  Key metadata (dictEntry):        ~70-80 bytes
  Includes: hash table entry, key SDS string, robj pointer,
            expiry (if set), LRU/LFU metadata

  Example: storing a 100-byte JSON string with a key name of 20 bytes
    Key name (SDS):     20 + ~4 bytes (small SDS header + null terminator) = ~24 bytes
    Value (SDS):        100 + ~4 bytes = ~104 bytes
    dict entry:         ~24 bytes (3 pointers)
    robj (key):         ~16 bytes
    robj (value):       ~16 bytes
    Expiry:             ~16 bytes (if TTL is set)
    jemalloc alignment: rounds up to allocation class boundaries

    Total: ~200-260 bytes for 120 bytes of key + value (after allocator rounding)
    Overhead ratio: ~1.7-2.2x for small values, approaches 1x for large values

  Rule of thumb: budget 2x the raw data size for small values (<1KB),
                 1.3x for medium values (1-10KB), 1.1x for large values (>10KB).
```

### 6.3 Persistence: Usually Disabled for Pure Cache

Redis offers two persistence mechanisms. For a pure cache (data can be reconstructed from the database), neither is usually necessary.

- **RDB (point-in-time snapshots)**: Forks the process and writes a complete snapshot to disk. Pro: compact files, fast restart. Con: fork can cause latency spikes on large datasets (copy-on-write memory pressure), data loss between snapshots.

- **AOF (append-only file)**: Logs every write operation. Pro: minimal data loss (configurable fsync policy). Con: larger files, slower restarts, fsync can add write latency.

**For pure cache use**: Disable both (`save ""` and `appendonly no`). The data is reconstructable from the source of truth. Persistence adds latency and operational complexity for no benefit. If Redis restarts, the cache is empty (cold start) and repopulates organically from cache misses.

**Exception**: If Redis is serving as both a cache and a data store (session store, rate limiter state), enable persistence to avoid data loss on restart.

### 6.4 Redis Cluster

For caches that exceed a single node's memory or throughput, Redis Cluster provides horizontal scaling.

**Hash slots**: The keyspace is divided into 16,384 hash slots. Each key is assigned to a slot via `CRC16(key) % 16384`. Each master node owns a subset of slots. Clients route commands directly to the correct node.

**Resharding**: When adding or removing nodes, hash slots are migrated between nodes. During migration, keys in a migrating slot may be on either the source or destination node. The client receives `MOVED` or `ASK` redirections and retries.

**Replication**: Each master has one or more replicas. If a master fails, a replica is promoted (automatic failover). Replication is asynchronous, so recently written data may be lost on failover.

**Multi-key operations**: Multi-key commands (MGET, MSET), MULTI/EXEC transactions, and Lua scripts require all keys to be in the same hash slot, or Redis returns a `CROSSSLOT` error. (Pipelines are fine: cluster-aware clients split them per node.) Use hash tags `{user:42}:profile` and `{user:42}:sessions` to force related keys to the same slot.

### 6.5 Redis Sentinel (High Availability)

For single-master deployments that need automatic failover without the complexity of Redis Cluster.

Sentinel is a separate process that monitors Redis instances, detects master failure, promotes a replica to master, and notifies clients of the topology change. Run at least 3 Sentinels: a configurable quorum must agree the master is down, and a majority of Sentinels must authorize the failover, which prevents two conflicting promotions.

**Sentinel vs. Cluster**: Use Sentinel when your dataset fits on a single node and you only need HA. Use Cluster when you need to shard across multiple nodes.

### 6.6 Pipelining

Redis commands are sent over TCP. Each command has a round-trip cost (~0.1-1ms on a local network). For batch operations, pipelining sends multiple commands in a single network round-trip.

```python
# Without pipelining: 100 commands × 0.5ms RTT = 50ms
for key in keys:
    redis.get(key)

# With pipelining: 100 commands in 1 round-trip = 0.5ms + processing
pipe = redis.pipeline(transaction=False)
for key in keys:
    pipe.get(key)
results = pipe.execute()  # All 100 results returned at once
```

**Impact**: Pipelining can improve throughput by 5-10x for batch operations. Always use it when performing multiple independent operations (bulk cache warming, batch reads, multi-key invalidation).

### 6.7 Redis or Valkey? The 2024–2026 Split

Everything in §6 applies to both, but since 2024 "Redis" means two projects, and the choice is
now a licensing, cost and roadmap decision as much as a technical one.

| Date | Event |
|---|---|
| March 2024 | Redis Inc. moves Redis from BSD to dual RSALv2 / SSPLv1 (source-available, not OSI open source). The Linux Foundation forks the last BSD version as **Valkey**, backed by AWS, Google Cloud, Oracle and others |
| Sept 2024 | **Valkey 8.0**: I/O threads run concurrently with the main thread and batch commands, up to **1.2 million requests/s** on an AWS r7g instance, over 3× the previous version |
| Oct 2024 | **ElastiCache for Valkey** launches **20% cheaper** than the Redis OSS engine on nodes, **33% cheaper** serverless (from about $6/month). Google Memorystore also offers Valkey |
| Early 2025 | **Valkey 8.1**: a new hash table saves about **20 bytes per key** (up to 30 with a TTL) |
| May 2025 | **Redis 8.0** adds **AGPLv3** as a third license option, so it is OSI open source again. Redis 8 also puts JSON, time series, probabilistic types, the Query Engine and **vector sets** (beta) into the core, plus hash-field TTL commands (`HGETEX`, `HSETEX`) |
| Oct 2025 | **Valkey 9.0**: **atomic slot migration** (whole slots move in one operation instead of key by key, so resharding no longer gets stuck on large keys), per-field hash expiry, multiple databases in cluster mode |
| May 2026 | **Valkey 9.1**: redesigned I/O threading (up to 17% more throughput) |

How to choose:

- **Pure cache, managed service:** Valkey is the default. It speaks the same protocol, existing
  clients work unchanged, and on AWS it is cheaper for the same node. The move is an in-place
  engine upgrade on ElastiCache.
- **You need Redis 8 features** (Query Engine, vector sets, JSON in core): use Redis 8. If you
  self-host it *and* offer it to others over a network, have legal review AGPLv3's obligations.
  For internal use as a cache, AGPL rarely matters.
- **Self-hosted and you want BSD without questions:** Valkey.
- **Portability rule:** both keep the core commands in §6.1 compatible, but new commands are
  starting to diverge (Valkey 9.1 and Redis 8 each added hash and multi-key commands the other
  may lack). Stick to the shared core in application code, and put engine-specific calls behind
  one module.

The comparison table in §7.3 says Redis is "single-threaded (mostly)". That still holds for
**command execution** in both projects: one core runs your commands, so a slow Lua script or
`KEYS *` blocks everything. **Network I/O** is multi-threaded when enabled (`io-threads`, since
Redis 6 and much faster in Valkey 8+). That is why a single node now reaches a million+ simple
requests per second, while a hot key (§11.3) still maxes out one core.

---

## 7. Distributed Caching Architecture

> **In plain words.** Caches come in layers: the browser, the CDN near the user, memory inside each app server, a shared cache server like Redis, and finally the database. Each layer is faster but smaller and harder to keep fresh than the one behind it.
>
> **Real-world example.** A news site serves images from a CDN (95% hits, ~20 ms), keeps site config in each app server's memory (~5 microseconds), and stores article bodies in Redis (~1 ms). Only about 1 in 50 article reads reaches the database (~15 ms).

### 7.1 The Four Cache Layers

Production systems use multiple cache layers, each with different characteristics. Understanding when to use each layer is a core interview skill.

```
MULTI-LEVEL CACHE HIERARCHY:

  ┌─────────────────────────────────────────────────────────────────────┐
  │  CLIENT (Browser / Mobile)                                         │
  │  ┌───────────────────────────────────────────────────────────────┐ │
  │  │ L0: HTTP Cache (browser cache, ETags, Cache-Control headers) │ │
  │  │     Latency: 0ms (from disk/memory)                          │ │
  │  │     Capacity: ~100MB-1GB per origin                          │ │
  │  └───────────────────────────────────────────────────────────────┘ │
  └───────────────────────────┬─────────────────────────────────────────┘
                              │ (cache miss or expired)
                              v
  ┌─────────────────────────────────────────────────────────────────────┐
  │  CDN (CloudFront, Cloudflare, Fastly)                              │
  │  ┌───────────────────────────────────────────────────────────────┐ │
  │  │ CDN Edge Cache                                                │ │
  │  │     Latency: 5-50ms (nearest PoP)                            │ │
  │  │     Capacity: Virtually unlimited (distributed)              │ │
  │  │     Best for: Static assets, API responses with Vary headers │ │
  │  └───────────────────────────────────────────────────────────────┘ │
  └───────────────────────────┬─────────────────────────────────────────┘
                              │ (cache miss)
                              v
  ┌─────────────────────────────────────────────────────────────────────┐
  │  APPLICATION SERVER                                                 │
  │  ┌───────────────────────────────────────────────────────────────┐ │
  │  │ L1: In-Process Cache (Caffeine, Guava, go-cache, lru_cache)  │ │
  │  │     Latency: ~1-10 microseconds                              │ │
  │  │     Capacity: 10MB-1GB per instance (JVM heap / process mem) │ │
  │  │     Best for: Hot keys, config, small reference data         │ │
  │  └───────────────────────────────┬───────────────────────────────┘ │
  │                                  │ (L1 miss)                       │
  │                                  v                                 │
  │  ┌───────────────────────────────────────────────────────────────┐ │
  │  │ L2: Remote Shared Cache (Redis, Memcached)                   │ │
  │  │     Latency: ~0.5-2 milliseconds (network hop)              │ │
  │  │     Capacity: 10GB-1TB+ (clustered)                          │ │
  │  │     Best for: Shared state, session data, computed results   │ │
  │  └───────────────────────────────┬───────────────────────────────┘ │
  │                                  │ (L2 miss)                       │
  │                                  v                                 │
  │  ┌───────────────────────────────────────────────────────────────┐ │
  │  │ L3: Database (PostgreSQL, DynamoDB, etc.)                    │ │
  │  │     Latency: 5-50 milliseconds                               │ │
  │  │     Capacity: Unlimited (disk-based)                         │ │
  │  │     Source of truth                                          │ │
  │  └───────────────────────────────────────────────────────────────┘ │
  └─────────────────────────────────────────────────────────────────────┘
```

### 7.2 L1: In-Process Cache

Stored in the application's own memory. No network hop. Microsecond access times.

**When to use**: Configuration and feature flags (read on every request, change rarely). Hot reference data (country codes, currency rates). Request-scoped memoization (avoid recomputing the same value within a single request). Any data small enough to fit in memory and accessed frequently enough to justify the memory cost.

**Challenges**: Each application instance has its own copy -- inconsistency between instances after a write. Memory pressure on the application process. Cache size is limited by instance memory.

**Invalidation**: TTL (short -- 30s to 5min), or broadcast invalidation via pub/sub (when one instance's L1 is invalidated, it publishes an event so other instances invalidate their local copies too).

**Server-assisted invalidation (Redis 6+, Valkey).** Instead of running your own pub/sub, let the
cache server tell each app instance when a key it holds in L1 has changed. After
`CLIENT TRACKING ON`, the server remembers which keys each connection has read and pushes an
`invalidate` message when any of them is modified or evicted. Two modes:

| Mode | Command | Server memory | Messages the client receives |
|---|---|---|---|
| Default | `CLIENT TRACKING ON` | One entry per (key, client) read. Capped by `tracking-table-max-keys`, and past the cap the server invalidates early | Only for keys this client actually read |
| Broadcast | `CLIENT TRACKING ON BCAST PREFIX product:` | None per client | Every change under the prefix, read or not |

With RESP3 the invalidation arrives on the same connection. With RESP2 you `REDIRECT` it to a
second connection subscribed to `__redis__:invalidate`. Three rules make it safe:

1. **When the connection drops, flush the whole L1.** Invalidations sent while you were
   disconnected are lost.
2. **Keep a short L1 TTL anyway.** Invalidations are asynchronous: one network hop of staleness
   is normal, and a bug in the invalidation path must not become unbounded staleness (§3.6).
3. **Use broadcast mode for small, hot prefixes** (config, feature flags, prices of the top
   products), and default mode for large key spaces where each instance reads only a few keys.

Lettuce, Jedis and redis-py (with RESP3) implement it, so check your client before building
invalidation yourself.

**Libraries**: Caffeine (Java -- the gold standard with W-TinyLFU), Guava Cache (Java -- predecessor to Caffeine), go-cache or bigcache (Go), cachetools or functools.lru_cache (Python).

### 7.3 L2: Remote Shared Cache

A dedicated cache service (Redis or Memcached) accessible by all application instances over the network.

**When to use**: Shared state that must be consistent across instances (session data, rate limiter counters). Data too large to fit in each instance's memory. Computed results that are expensive to regenerate.

**Redis vs. Memcached**:

```
┌──────────────────┬──────────────────────────┬──────────────────────────┐
│ Feature          │ Redis                    │ Memcached                │
├──────────────────┼──────────────────────────┼──────────────────────────┤
│ Data structures  │ String, Hash, Set, ZSet, │ String only              │
│                  │ List, HyperLogLog, etc.  │                          │
├──────────────────┼──────────────────────────┼──────────────────────────┤
│ Threading        │ 1 thread runs commands;  │ Multi-threaded           │
│                  │ I/O threads optional §6.7│                          │
├──────────────────┼──────────────────────────┼──────────────────────────┤
│ Persistence      │ RDB + AOF                │ None                     │
├──────────────────┼──────────────────────────┼──────────────────────────┤
│ Clustering       │ Redis Cluster (built-in) │ Client-side sharding     │
├──────────────────┼──────────────────────────┼──────────────────────────┤
│ Memory           │ Higher overhead per key  │ Slab allocator, more     │
│ efficiency       │ (rich metadata)          │ memory-efficient at scale│
├──────────────────┼──────────────────────────┼──────────────────────────┤
│ Max value size   │ 512 MB                   │ 1 MB (default)           │
├──────────────────┼──────────────────────────┼──────────────────────────┤
│ Use case         │ General purpose cache +  │ Simple, high-throughput  │
│                  │ data structure operations │ key-value caching        │
└──────────────────┴──────────────────────────┴──────────────────────────┘

Recommendation: Redis (or Valkey, §6.7) for almost everything. Memcached only when you need
multi-threaded performance for simple key-value workloads at extreme scale
(Facebook's Memcache deployment, described in "Scaling Memcache at Facebook", NSDI 2013, is the canonical example).
```

### 7.4 L1 + L2: The Two-Level Pattern

The most common production architecture combines an in-process L1 cache with a shared Redis L2 cache. Reads check L1 first, then L2, then the database. L1 has a very short TTL (30s-2min) to limit staleness between instances. L2 has a longer TTL (5-30min) and serves as the shared source of cached truth.

```python
class TwoLevelCache:
    def __init__(self, local_cache, redis_client, local_ttl=60, remote_ttl=300):
        self.l1 = local_cache       # In-process (e.g., cachetools.TTLCache)
        self.l2 = redis_client      # Redis
        self.local_ttl = local_ttl
        self.remote_ttl = remote_ttl

    def get(self, key: str, fetch_fn):
        # L1: in-process check (~microseconds)
        value = self.l1.get(key)
        if value is not None:
            return value

        # L2: Redis check (~1ms)
        cached = self.l2.get(key)
        if cached is not None:
            value = json.loads(cached)
            self.l1[key] = value  # Promote to L1
            return value

        # L3: Database (~10ms)
        value = fetch_fn(key)

        # Populate both levels
        self.l2.setex(key, self.remote_ttl, json.dumps(value))
        self.l1[key] = value
        return value

    def invalidate(self, key: str):
        self.l1.pop(key, None)
        self.l2.delete(key)
        # Optionally: publish invalidation event for other instances' L1
```

### 7.5 CDN Caching

Content Delivery Networks cache responses at edge locations geographically close to users. CDN caching is primarily controlled via HTTP headers.

**Key headers**:
- `Cache-Control: public, max-age=3600` -- cacheable for 1 hour.
- `Cache-Control: private, no-store` -- do not cache (user-specific, sensitive data).
- `Vary: Accept-Encoding, Authorization` -- cache separate variants per header value.
- `ETag` + `If-None-Match` -- conditional requests for validation.
- `stale-while-revalidate=60` -- serve stale for 60s while refreshing in the background.

**When to use CDN caching**: Static assets (JS, CSS, images -- long TTL, content-hash in URL for busting). Public API responses that are the same for all users (e.g., product catalog, trending items). Rendered HTML pages for logged-out users.

**When NOT to use CDN caching**: User-specific responses (unless using Vary on a user identifier, which effectively disables caching). Responses that change frequently. Data with strict consistency requirements.

**Security: a shared cache serves everyone the same bytes.** Two attack classes target the
difference between what the cache thinks a request is and what the origin serves:

- **Web cache deception.** The attacker tricks the cache into storing a *victim's* private
  response under a URL the attacker can then fetch. Typical trigger: a CDN rule "cache everything
  ending in `.css`" plus a backend that ignores trailing path segments. The victim clicks
  `/api/auth/session/x.css`, the backend returns their session JSON, and the CDN caches it as a
  public stylesheet. This took over ChatGPT accounts in March 2023 (§13, Case 8). PortSwigger's
  2024 research generalized it to **delimiter and normalization mismatches**. Spring treats `;` as
  a delimiter, so the CDN caches `/api/profile;.css` as CSS while the app serves `/api/profile`.
  Encoded `..%2F` sequences that the cache doesn't decode but the origin does work the same way.
- **Web cache poisoning.** The attacker gets a harmful response cached for *everyone* through an
  input the origin uses but the cache key ignores, e.g. an unkeyed `X-Forwarded-Host` header
  that the page uses to build script URLs.

Rules that prevent both:

1. **Origin decides cacheability, explicitly.** Authenticated and personalized responses send
   `Cache-Control: private, no-store`. Cacheable ones send `public, max-age=...`. Never let a CDN
   rule cache by **file extension** over the origin's headers. That override is the root cause of
   most deception bugs. Where the CDN offers it, turn on a check that the `Content-Type` matches
   the extension (Cloudflare calls this Cache Deception Armor).
2. **Normalize once, identically.** The cache key and the origin router must see the same path:
   the same decoding, dot-segment removal and delimiter handling. Otherwise reject ambiguous paths
   at the edge.
3. **Every input that changes the response is in the cache key, or stripped at the edge.**
   Headers, cookies and query parameters alike.
4. **Cache on an allow-list of routes** (`/static/*`, `/api/catalog/*`), not a deny-list.

**Personal data in caches (GDPR).** A cached copy is still personal data:

- **Erasure must reach every layer.** A deletion request must invalidate L1, L2 and the CDN
  (purge by URL or surrogate key). Otherwise the TTL, plus any `stale-if-error` window, is how long
  deleted data keeps being served. Keep TTLs on personal data short enough to defend in a data
  protection impact assessment.
- **No PII in cache keys.** Keys show up in logs, `SLOWLOG`, `MONITOR`, `--hotkeys` output and
  metrics labels. Key by internal ID (`user:8812`), never by email.
- **Encrypt and restrict.** TLS between app and cache, per-service ACL users (`ACL SETUSER`), and
  encryption at rest on managed services.

---

## 8. Consistent Hashing

> **In plain words.** With several cache servers, each key must live on one of them. The simple rule `hash(key) % N` moves almost every key when you add a server, which is like emptying the whole cache. Consistent hashing moves only about 1/N of the keys.
>
> **Real-world example.** A chat app has 3 cache servers and adds a 4th. With `% N`, 75% of keys move and the database sees a flood of misses. With a hash ring and 150 virtual nodes per server, about 25% of keys move (we measured 24%), and the rest stay warm.

### 8.1 The Problem: Cache Sharding

When a cache is distributed across multiple nodes, you need a way to determine which node holds a given key. The naive approach -- `node = hash(key) % N` -- works until a node is added or removed. When N changes, `hash(key) % N` produces a different result for nearly every key, causing a mass cache miss (effectively a full cache flush) and a thundering herd to the database.

```
MODULO HASHING FAILURE MODE:

  3 nodes: hash("user:42") % 3 = 1  (stored on node 1)
  4 nodes: hash("user:42") % 4 = 2  (now maps to node 2 — MISS)

  Adding 1 node remaps ~75% of keys → massive cache miss storm.
  Removing 1 node remaps ~67% of keys → same problem.
```

### 8.2 Consistent Hashing: How It Works

Consistent hashing maps both keys and nodes onto a fixed ring (typically a 2^32 point hash space). Each key is assigned to the next node clockwise on the ring. When a node is added or removed, only the keys between it and the next node in the ring are affected -- approximately `1/N` of total keys, rather than the `(N-1)/N` with modulo hashing.

```
CONSISTENT HASH RING:

                        0 / 2^32
                          │
                  Node C  ●
                 /                  \
               /                      \
             /                          \
    Node B  ●       Keys a,b,c map        ● Node A
             \      to nearest node      /
               \    clockwise          /
                 \                  /
                   ● ─── ● ─── ●
                 Node D     Node E

  Adding Node F between B and C:
    - Only keys between B and F are remapped (from C to F)
    - All other keys remain on their original node
    - Approximately 1/N keys are remapped
```

### 8.3 Virtual Nodes

A problem with basic consistent hashing: with a small number of physical nodes, the key distribution is uneven. A node may own a disproportionately large or small arc of the ring, leading to hot spots.

**Solution**: Each physical node is mapped to multiple "virtual nodes" (vnodes) on the ring. A physical node with 150 virtual nodes appears at 150 points on the ring, which smooths out the distribution. Adding a physical node adds 150 vnodes, each taking a small slice from different parts of the ring.

```
VIRTUAL NODES:

  Measured with the §8.5 code (MD5, 200,000 keys, nodes redis-1..3):

  Without vnodes (1 point per node):
    redis-1: 25.7% of keys
    redis-2: 50.5% of keys  (owns a large arc -- twice its fair share)
    redis-3: 23.8% of keys

  With 150 vnodes per physical node:
    redis-1: 31.6% of keys
    redis-2: 34.2% of keys
    redis-3: 34.2% of keys   (within ~2 points of the ideal 33.3%)

  Spread shrinks roughly like 1/sqrt(vnodes): more vnodes, more even.
```

**Typical vnode count**: 100-200 per physical node. Higher counts give better distribution but increase the ring metadata size and lookup cost.

### 8.4 Jump Consistent Hash

An alternative to ring-based consistent hashing. Jump consistent hash (Lamping and Veach, Google, 2014) maps a key to one of N buckets using a fast, zero-memory algorithm. It achieves perfectly uniform distribution and moves the minimum number of keys when N changes.

```go
// Jump consistent hash — the entire algorithm
func JumpConsistentHash(key uint64, numBuckets int) int {
    var b, j int64 = -1, 0
    for j < int64(numBuckets) {
        b = j
        key = key*2862933555777941757 + 1
        j = int64(float64(b+1) * (float64(int64(1)<<31) / float64((key>>33)+1)))
    }
    return int(b)
}
```

**Pros**: O(ln N) time, zero memory. Near-perfectly uniform key distribution. Minimal key movement (going from N to N+1 buckets moves 1/(N+1) of keys).

**Cons**: Only supports sequential bucket IDs (0 to N-1). Cannot name or remove specific nodes -- you can only grow or shrink the bucket count from the end. This makes it unsuitable for clusters where arbitrary nodes can fail.

**When to use**: Sharding across a fixed or sequentially-numbered set of nodes (e.g., database shards where shard IDs are 0-N). Not suitable for cache clusters with arbitrary node failures.

### 8.5 Implementation: Consistent Hashing with Virtual Nodes

```python
import hashlib
from bisect import bisect_right
from typing import List, Optional

class ConsistentHash:
    """Consistent hash ring with virtual nodes."""

    def __init__(self, nodes: List[str] = None, vnodes: int = 150):
        self.vnodes = vnodes
        self.ring = {}          # hash_value → node_name
        self.sorted_keys = []   # Sorted hash values for binary search
        if nodes:
            for node in nodes:
                self.add_node(node)

    def _hash(self, key: str) -> int:
        """MD5-based hash (not cryptographic — just uniform distribution)."""
        return int(hashlib.md5(key.encode()).hexdigest(), 16)

    def add_node(self, node: str):
        """Add a physical node with its virtual nodes to the ring."""
        for i in range(self.vnodes):
            vnode_key = f"{node}:vnode:{i}"
            h = self._hash(vnode_key)
            self.ring[h] = node
            self.sorted_keys.append(h)
        self.sorted_keys.sort()

    def remove_node(self, node: str):
        """Remove a physical node and all its virtual nodes."""
        for i in range(self.vnodes):
            vnode_key = f"{node}:vnode:{i}"
            h = self._hash(vnode_key)
            del self.ring[h]
            self.sorted_keys.remove(h)

    def get_node(self, key: str) -> Optional[str]:
        """Find the cache node responsible for a given key."""
        if not self.ring:
            return None
        h = self._hash(key)
        # Find the first node clockwise from the key's hash
        idx = bisect_right(self.sorted_keys, h)
        if idx == len(self.sorted_keys):
            idx = 0  # Wrap around the ring
        return self.ring[self.sorted_keys[idx]]


# Usage
ring = ConsistentHash(["redis-1", "redis-2", "redis-3"])

node = ring.get_node("user:42")       # → "redis-2"
node = ring.get_node("session:abc")    # → "redis-1"

# Adding a node remaps only ~1/N keys (measured: 3 -> 4 nodes moved ~24% of keys)
ring.add_node("redis-4")
node = ring.get_node("user:42")       # Probably still "redis-2"
```

### 8.6 Ketama Algorithm

The Ketama algorithm is the specific consistent hashing implementation used by libmemcached and most Memcached client libraries. It uses MD5 hashing to generate 4 virtual node points per hash (by splitting the 128-bit MD5 output into 4 32-bit values), with a default of 40 hash iterations per physical node, yielding 160 virtual nodes per physical node. Ketama is the de facto standard for Memcached cluster sharding.

---

## 9. Cache in System Design Interviews

> **In plain words.** This section is the interview section of this chapter. It shows the caching patterns interviewers expect for common designs: news feeds, sessions, rate limiters, leaderboards, ML features, API responses, and precomputed counts. For each, know the key format, the data type, the TTL, and what happens on a miss.
>
> **Real-world example.** Asked to design Twitter's timeline, say: "feed:{user_id} is a Redis list of the latest 200 post IDs, TTL 15 minutes, filled by fan-out on write, except for accounts with millions of followers, which we merge in at read time."

Caching appears in nearly every system design interview. Here are the concrete patterns you should be ready to apply.

### 9.1 Feed / Timeline Cache

**Problem**: A user opens their feed. Fetching and ranking posts from all followed users in real-time is too slow.

**Pattern**: Cache the pre-computed feed per user. On new post, fan-out to followers' cached feeds (push model) or invalidate/refresh on next access (pull model).

```
FEED CACHING (hybrid push/pull):

  Key:    feed:{user_id}
  Value:  List of post IDs (most recent N, e.g., 200)
  TTL:    15 minutes
  Write:  On new post → LPUSH feed:{follower_id} post_id
          + LTRIM feed:{follower_id} 0 199  (cap at 200)
  Read:   LRANGE feed:{user_id} 0 19  (page of 20)
          On miss → compute from DB, populate cache

  For celebrities (millions of followers): do NOT fan-out on write.
  Use pull model: compute on read from followed users' post caches.
```

### 9.2 Session Store

**Pattern**: Store session data in Redis with TTL matching the session timeout. Every access refreshes the TTL (sliding window expiration).

```
  Key:    session:{session_id}
  Value:  Hash of session fields (user_id, role, preferences)
  TTL:    30 minutes (refreshed on every access)

  HSET session:abc123 user_id 42 role "admin" last_access 1694000000
  EXPIRE session:abc123 1800

  On every request:
    session = HGETALL session:{session_id}
    if session exists:
        EXPIRE session:{session_id} 1800  (refresh sliding window)
        return session
    else:
        return 401 Unauthorized
```

### 9.3 Rate Limiter

**Pattern**: Sliding window counter using a Redis sorted set. Each request adds a timestamped entry. Count entries within the window to check the limit.

```
SLIDING WINDOW RATE LIMITER:

  Key:    ratelimit:{user_id}:{endpoint}
  Type:   Sorted Set (score = timestamp)

  On each request:
    now = current_timestamp_ms
    window_start = now - 60000  (60-second window)

    ZREMRANGEBYSCORE key 0 window_start   (remove entries outside window)
    count = ZCARD key                      (count entries in window)

    if count >= limit:
        return 429 Too Many Requests

    ZADD key now request_id               (add this request)
    EXPIRE key 61                          (auto-cleanup)

  Run these steps as one Lua script (or MULTI/EXEC): otherwise two
  concurrent requests can both read count = limit-1 and both get through.
```

### 9.4 Leaderboard

**Pattern**: Redis sorted set is purpose-built for leaderboards. O(log N) insert, O(log N + M) range queries.

```
LEADERBOARD:

  ZADD leaderboard 9500 "player:42"      (set/update score)
  ZADD leaderboard 8700 "player:17"

  Top 10:           ZREVRANGE leaderboard 0 9 WITHSCORES
  Player rank:      ZREVRANK leaderboard "player:42"    → 0 (1st place)
  Player score:     ZSCORE leaderboard "player:42"      → 9500
  Players near me:  ZREVRANGE leaderboard rank-5 rank+5 WITHSCORES
```

### 9.5 Feature Store Online Serving

**Pattern**: ML feature stores use Redis hashes to serve pre-computed features at inference time with sub-millisecond latency.

```
FEATURE STORE:

  Key:    features:{entity_type}:{entity_id}
  Type:   Hash
  Value:  Feature name → feature value

  HSET features:user:42 avg_order_value 85.50 order_count 23 days_since_last 3
  HMGET features:user:42 avg_order_value order_count  → ["85.50", "23"]

  Write path: Feature pipeline computes features → bulk HSET to Redis
  Read path:  ML model inference → HMGET features → sub-ms response
```

### 9.6 API Response Cache

**Pattern**: Cache entire API responses keyed by a hash of the request parameters. This is the simplest and most impactful caching pattern.

```python
import hashlib
import json

def cache_key_for_request(endpoint: str, params: dict) -> str:
    """Deterministic cache key from request parameters."""
    # Sort params for consistent key regardless of argument order
    canonical = json.dumps(params, sort_keys=True)
    param_hash = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"api:{endpoint}:{param_hash}"

# Example:
# GET /api/products?category=electronics&sort=price&page=2
# → "api:/api/products:a3f8b2c1d9e0f7a4"
```

**Caution**: Only cache GET requests. Never cache POST/PUT/DELETE. Be careful with user-specific responses -- include user_id in the cache key or use `Cache-Control: private`. Avoid caching responses with sensitive data unless the cache is access-controlled.

### 9.7 Computed / Aggregated Data

**Pattern**: Pre-compute expensive aggregations and cache the results. Refresh on a schedule or on relevant writes.

```
EXAMPLES:

  Trending topics:
    Key:    trending:topics:{region}
    Value:  JSON array of top 50 topics with scores
    TTL:    5 minutes
    Refresh: Background job computes every 5 minutes from event stream

  Dashboard counters:
    Key:    dashboard:{org_id}:counts
    Value:  Hash with field per metric (total_users, active_today, revenue_mtd)
    TTL:    1 minute
    Refresh: Increment on write events (HINCRBY), full recompute hourly

  Search autocomplete:
    Key:    autocomplete:{prefix}
    Value:  JSON array of top 10 suggestions
    TTL:    1 hour
    Refresh: Background indexer updates after catalog changes
```

---

## 10. Capacity Planning

> **In plain words.** Sizing a cache is simple multiplication: number of items x bytes per item (key + value + Redis overhead) x number of copies, plus spare room. Then estimate the hit rate, because the misses are what the database still has to handle.
>
> **Real-world example.** 10 million user profiles x 625 bytes = 6.25 GB. With 1 replica and 20% headroom, 15 GB total. At 10,000 reads/s and a 90% hit rate, the database still sees 1,000 queries/s -- size the database for that, and for what happens if the cache is empty.

### 10.1 Memory Sizing Formula

```
MEMORY CALCULATION:

  Total memory = N × (key_size + value_size + overhead) × replication_factor

  Where:
    N                  = number of unique cached items
    key_size           = average key length in bytes (e.g., "user:profile:12345" = 18 bytes)
    value_size         = average serialized value size in bytes
    overhead           = Redis per-key overhead (~80-100 bytes for Redis 7.x)
    replication_factor = 1 (no replicas) to 2 (1 replica per shard)

  EXAMPLE: User profile cache
    N = 10 million users
    key_size = 25 bytes (avg)
    value_size = 500 bytes (avg JSON profile)
    overhead = 100 bytes (Redis metadata)

    Per item: 25 + 500 + 100 = 625 bytes
    Total:    10M × 625 bytes = 6.25 GB
    With replication (1 replica): 6.25 GB × 2 = 12.5 GB
    With 20% headroom: 12.5 GB × 1.2 = 15 GB

    → 1 primary + 1 replica, each with ~8 GB (6.25 GB x 1.2 = 7.5 GB per node)
```

### 10.2 Hit Rate Estimation

Hit rate is the percentage of requests served from cache. It is the single most important metric for cache effectiveness.

```
HIT RATE FACTORS:

  Cache size:        Larger cache → higher hit rate (more items cached)
  TTL:               Longer TTL → higher hit rate (items live longer)
  Access pattern:    Skewed (Zipfian) → higher hit rate (hot items stay cached)
  Eviction policy:   Better policy → higher hit rate (right items evicted)
  Working set size:  If working set fits in cache → ~100% hit rate

  Typical hit rates by use case:
    CDN static assets:     95-99%
    User session cache:    90-98%
    API response cache:    70-95%
    Database query cache:  60-90%
    Full-text search:      50-80%

  Rule of thumb: If your hit rate is below 80%, either your cache is too small
  for the working set, your TTL is too short, or the access pattern has no
  temporal locality (and caching may not be the right solution).
```

### 10.3 Cost/Benefit Math

```
BREAK-EVEN ANALYSIS:

  Cost per cache hit (Redis):       ~$0.000001  (memory + network)
  Cost per DB query (PostgreSQL):   ~$0.0001    (CPU + I/O + connection)
  Cost ratio:                       ~100x

  If a cached item prevents 100 DB queries before it expires or is evicted,
  the cache has paid for itself 100x over.

  Real example:
    10,000 QPS to a product catalog endpoint
    Without cache: 10,000 DB queries/sec
    With 90% hit rate: 1,000 DB queries/sec + 10,000 cache reads/sec

    Illustrative on-demand prices (check current AWS pricing; they vary by region):
    DB cost: RDS db.r6g.xlarge ≈ $0.48/hr ≈ $350/month
    Cache cost: ElastiCache cache.r6g.large (~13 GB) ≈ $0.17-0.21/hr ≈ $125-150/month

    With cache: DB load reduced 90%, can use smaller DB instance → net savings.
    Cache cost is almost always lower than the DB cost it replaces.

  Memory cost:
    Redis (AWS ElastiCache): ~$10-13/GB/month (on-demand, illustrative)
    Database (RDS SSD): ~$0.10/GB/month (storage) + compute for queries
    The expensive part of DB queries is compute, not storage.
```

### 10.4 Sizing Checklist for Interviews

When discussing cache capacity in an interview, walk through this checklist:

1. **How many unique items?** (total dataset vs. active working set)
2. **Average item size?** (key + value + serialization overhead)
3. **Redis overhead per key?** (~80-100 bytes)
4. **Working set ratio?** (what fraction of total data is actively accessed?)
5. **TTL strategy?** (affects how many items are alive at any point)
6. **Replication factor?** (1 for cache-only, 2+ for HA)
7. **Headroom?** (20% for operational safety, eviction smoothing)
8. **Hit rate target?** (typically 90%+ for the cache to be worthwhile)

---

## 11. Failure Modes and Resilience

> **In plain words.** Caches fail in a few typical ways: many keys expire at once, attackers ask for keys that don't exist, one very popular key expires, or the cache server dies or splits in two. Each has a standard defense, and the golden rule is that a broken cache must never take the whole site down.
>
> **Real-world example.** A payments dashboard caches merchant summaries with exactly 1 hour TTL, all loaded at 09:00 during a deploy. At 10:00 all 500,000 keys expire in the same moment and the next requests all miss together. Adding random jitter (1 h + 0-10 min) spreads the rebuilds over 10 minutes: about 500,000 / 600 s ≈ 830 per second instead of one giant burst.

Caches fail. Understanding how they fail and how to handle each failure mode is what separates production-grade caching from textbook caching.

### 11.1 Cache Avalanche (Mass Expiry)

**Problem**: A large number of cache entries expire at the same time (e.g., all populated simultaneously with the same TTL). The sudden mass miss floods the database.

**Cause**: Batch cache warming with uniform TTL. Synchronized cache population on deploy. Time-aligned TTLs (e.g., all entries expire at midnight).

**Solutions**:
- **TTL jitter**: `TTL = base_ttl + random(0, jitter_range)`. This is the primary defense. Always use it.
- **Staggered warming**: When pre-loading cache on deploy, spread the load over minutes, not seconds.
- **Rate-limited fallback**: When cache miss rate spikes, rate-limit database queries and serve stale data or degraded responses.

### 11.2 Cache Penetration (Querying Non-Existent Data)

**Problem**: Requests for keys that do not exist in the database (and therefore will never be in the cache). Every request is a cache miss that results in a fruitless database query. Attackers can exploit this by requesting random non-existent IDs.

**Solutions**:

- **Cache negative results**: On a DB miss, cache a sentinel value (empty/null) with a short TTL (30-60 seconds). Subsequent requests for the same non-existent key hit the cache instead of the database.

- **Bloom filter**: Before querying the cache or database, check a Bloom filter that contains all existing keys. If the Bloom filter says "not present", the key definitely does not exist -- skip the query entirely. False positives are possible (a few unnecessary queries) but false negatives are not.

```
BLOOM FILTER DEFENSE:

  Request for key "user:99999999"
       │
       v
  Bloom Filter: contains("user:99999999")?
       │
       ├── NO (definitely not in DB) → return 404 immediately
       │
       └── YES (might be in DB) → proceed to cache → DB lookup
```

- **Input validation**: Validate key formats before querying. If user IDs are UUIDs, reject anything that is not a valid UUID before it reaches the cache layer.

### 11.3 Cache Breakdown (Hot Key Expires)

**Problem**: A single extremely popular key expires. Thousands of concurrent requests for that key all miss the cache simultaneously. This is the thundering herd problem applied to a single hot key.

**Solutions**: See section 4 (Thundering Herd). The solutions are the same: mutex/lock, singleflight, probabilistic early expiration. Additionally:

- **Never-expire hot keys**: For keys known to be hot (homepage content, site configuration), set no TTL and invalidate explicitly on write. The key is always in cache.
- **Hot key replication**: Replicate hot keys across multiple cache nodes to distribute the read load. The client randomly selects one of N replicas for each read.

### 11.4 Redis Failover Lag

When a Redis master fails and a replica is promoted, there is a window during which the shard is unavailable or inconsistent. Its length is set mostly by failure detection: Sentinel's `down-after-milliseconds` (30 s in the sample config) or Redis Cluster's `cluster-node-timeout` (default 15 s), plus a few seconds for election and client reconnection. During that window:
- Writes to the old master may be lost (async replication).
- Reads may fail or return stale data from an unsynced replica.
- Clients may be sending requests to the wrong node until topology updates propagate.

**Mitigation**: Design the application to tolerate cache unavailability. Use a circuit breaker that falls through to the database when cache errors exceed a threshold. Never let a cache failure cascade into a system outage.

### 11.5 Split-Brain in Redis Cluster

During a network partition, Redis Cluster may have two partitions that each believe they have a master for the same hash slots. Writes to both sides create conflicting data. When the partition heals, one side's writes are lost.

**Mitigation**: Configure `min-replicas-to-write` to prevent a master from accepting writes if it cannot reach a minimum number of replicas. This prevents writes on the minority side of a partition but reduces availability.

For a pure cache (reconstructable from DB), split-brain is inconvenient but not catastrophic -- stale data is served temporarily, and TTL or invalidation eventually corrects it.

### 11.6 Circuit Breaker for Cache Failures

When the cache is completely down, the naive behavior is for every request to attempt a cache operation, time out, and then fall through to the database. The timeout adds latency, and the database may not be able to handle the full uncached load.

```
CIRCUIT BREAKER FOR CACHE:

  State: CLOSED (normal operation)
    → Cache errors exceed threshold (e.g., 50% error rate in 10s window)

  State: OPEN (cache bypassed)
    → All requests skip cache, go directly to DB
    → DB receives rate-limited traffic (shed excess load)
    → After timeout (e.g., 30s), transition to HALF-OPEN

  State: HALF-OPEN (testing recovery)
    → Small percentage of requests test the cache
    → If cache responds: transition back to CLOSED
    → If cache fails: back to OPEN

  ┌────────┐    errors > threshold    ┌────────┐
  │ CLOSED │ ────────────────────────>│  OPEN  │
  │        │<────────────────────────-│        │
  └────────┘    cache healthy         └───┬────┘
                (half-open test passes)    │
                                          │ timeout
                                          v
                                    ┌───────────┐
                                    │ HALF-OPEN │
                                    └───────────┘
```

**Key principle**: A cache should make your system faster, never slower. If the cache is down, the system should degrade gracefully to direct database access (with load shedding), not hang waiting for a dead cache.

---

## 12. Monitoring

> **In plain words.** Watch hit rate first: a small drop in hit rate means a big rise in database load. Then watch memory use, evictions, latency, and connections. Alert on changes, not only on fixed limits.
>
> **Real-world example.** At 20,000 reads/s, a hit rate falling from 98% to 90% looks small, but database queries go from 400/s to 2,000/s -- five times more. An alert on "hit rate < 90% for 10 minutes" catches it before the database does.

### 12.1 The Essential Metrics

**Hit rate** is the single most important cache metric. Everything else is secondary.

```
CACHE MONITORING METRICS (ordered by importance):

  1. Hit Rate              Target: >90% (ideally >95%)
     = hits / (hits + misses)
     A drop in hit rate is your earliest warning of problems.

  2. Miss Rate             Inverse of hit rate. Watch for spikes.

  3. Eviction Rate         Keys evicted/sec due to memory pressure.
     High eviction = cache too small or TTL too long (too many items).

  4. Memory Utilization    Current memory / max memory.
     Alert at 80%. Evictions start at 100%.

  5. Latency (P50/P99)     Cache read/write latency.
     P50 should be <1ms (Redis). P99 should be <5ms.
     P99 > 10ms indicates network issues or oversized values.

  6. Connection Pool        Active connections / max connections.
     Exhaustion causes queuing and timeout errors.

  7. Key Count             Total keys in cache.
     Unexpected growth may indicate a cache key leak (unbounded keys).

  8. Expired Keys/sec      Keys expiring due to TTL.
     Spikes correlate with batch operations or synchronized TTLs.
```

### 12.2 PromQL Examples

```promql
# Hit rate (Redis with redis_exporter)
sum(rate(redis_keyspace_hits_total[5m])) /
(sum(rate(redis_keyspace_hits_total[5m])) + sum(rate(redis_keyspace_misses_total[5m])))

# Hit rate drop alert (below 85% for 5 minutes)
# ALERT CacheHitRateLow
sum(rate(redis_keyspace_hits_total[5m])) /
(sum(rate(redis_keyspace_hits_total[5m])) + sum(rate(redis_keyspace_misses_total[5m])))
< 0.85

# Memory utilization percentage
redis_memory_used_bytes / redis_memory_max_bytes * 100

# Eviction rate (keys evicted per second)
rate(redis_evicted_keys_total[5m])

# Command latency P99 -- needs a latency histogram; the metric name depends on
# your exporter and version (client-side histograms are often more reliable)
histogram_quantile(0.99, rate(redis_commands_duration_seconds_bucket[5m]))

# Connection pool utilization
redis_connected_clients / redis_config_maxclients * 100

# Cache miss rate spike detection (3x normal baseline)
rate(redis_keyspace_misses_total[5m]) > 3 * avg_over_time(rate(redis_keyspace_misses_total[5m])[1h:5m])
```

### 12.3 Alerting Strategy

```
ALERT PRIORITY MATRIX:

  P1 (page on-call):
    - Cache hit rate < 50% for > 2 minutes (likely total cache failure)
    - Redis master down, failover not completing
    - Memory utilization > 95% with rising eviction rate

  P2 (alert channel, investigate within 1 hour):
    - Cache hit rate < 80% for > 10 minutes
    - P99 latency > 10ms for > 5 minutes
    - Connection pool utilization > 80%
    - Eviction rate > 1000/sec sustained

  P3 (ticket, investigate within 1 day):
    - Cache hit rate < 90% for > 1 hour
    - Memory utilization > 80%
    - Key count growing faster than expected
    - Replication lag > 1 second
```

### 12.4 Debugging Cache Issues

**Hit rate suddenly dropped**:
1. Check if a deploy changed cache key format (all old entries become orphaned).
2. Check if a new feature is querying keys that are never cached (new miss path).
3. Check if TTLs were changed (shorter TTL → more misses).
4. Check if a cache node failed and consistent hashing remapped keys.
5. Check if a batch job is scanning the cache (polluting LRU).

**Memory growing unexpectedly**:
1. Look for unbounded key patterns (keys generated from user input without limits).
2. Check for missing TTLs on cache set operations.
3. Look for serialized values larger than expected (nested objects, base64 blobs).
4. Use `redis-cli --bigkeys` to find the largest keys.
5. Use `MEMORY USAGE key` to measure specific keys.

**P99 latency spike**:
1. Check for large values (serialization/deserialization overhead). Split values over 10KB.
2. Check for slow commands (`SLOWLOG GET 10`). Common culprits: KEYS (never use in production), large SORT operations, operations on large collections.
3. Check for network saturation (bandwidth between app and cache).
4. Check for Redis CPU saturation (single-threaded — one slow command blocks everything).
5. Check for client-side connection pool exhaustion (requests queuing for a connection).

---

## 13. Real-world cases — incidents with numbers

Cases 1–6 are **composite scenarios** built from failure modes this chapter describes; numbers are illustrative but internally consistent. Cases 7–8 are public incidents.

**Quick index:** DB spikes every TTL period → Case 1 · cache restarted empty, DB overwhelmed → Case 2 · hit rate collapses during a nightly job → Case 3 · DB flooded by requests for IDs that don't exist → Case 4 · old prices shown for up to an hour → Case 5 · one Redis shard at 100% CPU, others idle → Case 6 · users see other users' data from the cache → Case 7 · a crafted link leaks a session through the CDN → Case 8

### Case 1: Flash-sale stampede on one product key

- **Setup.** E-commerce checkout. The flash-sale product's details are cached with cache-aside, TTL 60 s. The key gets 8,000 reads/s; rebuilding it (a join of inventory and pricing) takes 300 ms.
- **Symptom.** Every 60 seconds, database CPU jumps to 100% and product-page p99 goes from 40 ms to 4 s, then recovers.
- **Measurement/Diagnosis.** The spikes line up exactly with the key's expiry. In the 300 ms rebuild window, 8,000 x 0.3 = 2,400 requests miss and all run the same query. The DB pool has 200 connections, so 2,200 requests queue.
- **Fix.** Singleflight in each of the 30 app servers (§4.4): at most 30 queries per expiry instead of 2,400. Plus XFetch (§4.3) with `delta = 0.3 s`, `beta = 1`: the first early refresh fires on average about `0.3 x ln(8,000 x 0.3) ≈ 2.3 s` before expiry, so in most cycles the key never actually expires. After: 2–3 rebuild queries per minute; p99 back to ~40 ms.
- **Lesson.** Hot key + expiry = stampede. Coalesce per process and refresh early; per-process singleflight alone still leaves one query per server.

### Case 2: Cold cache after a Redis restart

- **Setup.** Chat app. 40,000 profile reads/s, hit rate 97%, so the DB sees 40,000 x 0.03 = 1,200 queries/s. The DB tops out at about 6,000 queries/s. Redis runs without persistence (§6.3) and no replica.
- **Symptom.** Redis is OOM-killed and restarts empty. Error rate jumps to over 80% for 25 minutes.
- **Measurement/Diagnosis.** Hit rate drops to 0%, so DB demand is 40,000 queries/s -- about 6.7x its capacity. Most queries time out *before* they can fill the cache, so the cache refills very slowly and the outage keeps itself going.
- **Fix.** Add a replica with automatic failover (a restart becomes a ~15–30 s failover to a warm copy); set `maxmemory` below the container limit with `allkeys-lru` so Redis evicts instead of being killed; add request coalescing, and admission control in front of the DB (cap at 5,000 queries/s and let the rest fail fast so admitted queries finish and fill the cache). After a similar event in a test: hit rate back above 90% in ~3 minutes instead of 25.
- **Lesson.** Plan for an empty cache. The miss path must survive 100% misses, or at least shed load so the cache can refill.

### Case 3: Nightly scan wipes the LRU cache

- **Setup.** Video platform. A Redis cache holds 2 million video-metadata entries for 25,000 reads/s at 96% hit rate (DB: 1,000 queries/s). Policy: `allkeys-lru`.
- **Symptom.** Every night at 02:00 the hit rate falls to 60% and DB load rises to 25,000 x 0.40 = 10,000 queries/s for an hour.
- **Measurement/Diagnosis.** `evicted_keys` jumps from ~0 to tens of thousands per second at 02:00. A new analytics job reads all 30 million videos once through the same cache-aside code path. With LRU, each one-time read looks "recent" and pushes out a popular item (§5.1).
- **Fix.** The job reads from a DB replica and bypasses the cache; the policy changes to `allkeys-lfu`, so items read once cannot beat items read thousands of times. After: hit rate stays at 95–96% during the job.
- **Lesson.** Scans kill LRU. Keep batch traffic out of the serving cache, or use a frequency-aware policy (LFU, TinyLFU).

### Case 4: Random-ID scraper causes cache penetration

- **Setup.** Public API `GET /users/{id}`. Normal traffic 5,000 req/s at 97% hit rate: 150 DB queries/s. 50 million valid IDs.
- **Symptom.** DB CPU at 100%, while the cache looks healthy.
- **Measurement/Diagnosis.** A scraper sends 20,000 req/s for random IDs that don't exist. None are ever cached, so the DB now serves 150 + 20,000 = 20,150 queries/s. Negative caching (§11.2) doesn't help: each random ID is asked only once.
- **Fix.** Validate the ID format first, then check a Bloom filter of all 50 million valid IDs. At a 1% false-positive rate it needs about 9.6 bits per ID: ~479 million bits ≈ 60 MB, with 7 hash functions. After: only ~1% of bogus requests pass the filter, so the extra DB load drops from 20,000 to ~200 queries/s. Add rate limiting per client as well.
- **Lesson.** A cache only helps for keys that exist. For "does not exist" traffic, answer before the DB: validation, Bloom filter, rate limits.

### Case 5: Stale prices from a read-replica race

- **Setup.** E-commerce catalog. Writes go to the primary DB, then the app deletes `price:{id}` from the cache. Cache misses read from a DB replica that lags up to 300 ms. TTL: 1 hour.
- **Symptom.** A few customers see an old price for up to an hour after a price change; support tickets follow.
- **Measurement/Diagnosis.** Sampling cache vs. primary shows about 60 stale prices per day out of 50,000 price changes (0.12%). Sequence: writer updates the primary and deletes the key; a reader misses, reads the *replica* (still old), and writes the old price back into the cache for a full hour.
- **Fix.** Delete the key a second time about 1 s after the write (longer than the replica lag, "delayed double delete"), and read from the primary on a cache miss for prices. Lower the TTL to 10 minutes as the upper bound on any staleness left. For stronger protection, use versioned keys (§3.3) or leases (§3.4). After: stale prices drop from ~60/day to ~1/day, and never last longer than 10 minutes.
- **Lesson.** Cache-aside plus replicas has a race window. The TTL is your worst-case staleness, so pick it on purpose.

### Case 6: One hot key melts one Redis shard

- **Setup.** Live-sports app on a 6-shard Redis Cluster. During a final, the key `match:final:score` gets 150,000 reads/s. All reads go to the one shard that owns its hash slot.
- **Symptom.** That shard's CPU is at 100% and its p99 is 40 ms; the other five shards sit at ~15% CPU and ~1 ms.
- **Measurement/Diagnosis.** `redis-cli --hotkeys` (works when an LFU policy is set) shows one key taking most of the shard's commands. Adding shards does not help: one key always lives in one slot.
- **Fix.** Put an in-process L1 cache (§7.4) with a 1-second TTL in front of Redis on the 60 app servers. Redis now sees at most ~60 reads/s for that key (one per server per second) instead of 150,000. The score may be up to 1 s old, which is fine for this screen. After: shard CPU back to ~15%.
- **Lesson.** Sharding spreads *keys*, not *load on one key*. Hot keys need local caching or key replication (§11.3).

### Public incidents — the cache as a security boundary

Cases 1–6 are composites. The two below are **real, public incidents**, taken from OpenAI's
postmortem and the researchers' write-ups. In both, the cache handed one user's data to another.

### Case 7: Another user's data from a corrupted cache connection (ChatGPT, 2023-03-20)

- **Setup.** ChatGPT cached user data in Redis through the `redis-py` asyncio client, with
  connections shared from a pool.
- **Symptom.** Some users saw **other users' chat titles** in their history sidebar. OpenAI took
  ChatGPT offline. For **1.2% of ChatGPT Plus subscribers** active between 01:00 and 10:00 PT,
  another user could have seen their name, email, payment address, card type, the last four
  digits of their card and its expiry date.
- **Measurement/Diagnosis.** A bug in `redis-py`: if a request was **cancelled after it was sent
  but before its response was read**, the connection went back to the pool with that response
  still unread. The next request on the connection, for a different user, read the stale response
  as its own. A server change that morning had sharply increased request cancellations, which
  turned a rare race into a visible leak.
- **Fix.** The library was patched, and OpenAI added **redundant checks that data returned by
  the cache matches the requesting user**.
- **Lesson.** Cancellation and timeouts are part of the cache client's correctness, not just
  its latency. Treat the cache as untrusted for isolation: store the owner ID inside the cached
  value and check it on read, so a client or key bug returns a miss instead of someone else's
  data:

```python
def get_user_scoped(cache, user_id: str, key: str):
    raw = cache.get(key)
    if raw is None:
        return None
    entry = json.loads(raw)
    if entry.get("owner") != user_id:          # wrong owner: treat as a miss and alert
        log.error("cache owner mismatch", extra={"key": key})
        return None
    return entry["value"]
```

### Case 8: Web cache deception takes over accounts (ChatGPT, March 2023)

- **Setup.** ChatGPT's frontend was served through a CDN. `GET /api/auth/session` returned the
  signed-in user's session context, including an **access token**.
- **Symptom.** None visible. A researcher (Gal Nagli) reported that one click on a crafted link
  was enough to take over the clicking user's account.
- **Measurement/Diagnosis.** The CDN cached paths that looked like static files. A link such as
  `/api/auth/session/victim.css` reached the session endpoint with the victim's cookies, and the
  CDN stored the response as a public `.css` file. The attacker then fetched the same URL and got
  the victim's token (§7.5). OpenAI fixed it quickly. A later variant (reported in 2024) used
  a URL-encoded `../` that the CDN didn't decode but the origin did.
- **Fix.** Stop caching the authentication paths, and make cacheability follow the origin's
  headers rather than the file extension.
- **Lesson.** The cache key and the origin must agree on what a URL means. Any gap between the
  two (extension rules, delimiters, encoding) is a data-leak path. Apply §7.5's four rules.

---

## Summary: The Interview Caching Checklist

When caching comes up in a system design interview, walk through these items:

1. **Justify the cache**: State the read/write ratio, the acceptable staleness window, and the performance requirement that makes caching necessary.

2. **Choose the strategy**: Cache-aside (default), with write-through if read-after-write consistency matters. State why.

3. **Choose the tier**: L1 in-process for hot config/reference data, L2 Redis for shared state, CDN for static assets. State which and why.

4. **Define the key schema**: `{entity_type}:{entity_id}:{optional_variant}`. Show you have thought about key collisions and namespace isolation.

5. **Set the TTL**: State a specific number and justify it based on staleness tolerance and access pattern.

6. **Handle invalidation**: TTL as safety net + event-based (CDC or application pub/sub) for freshness. State both.

7. **Handle thundering herd**: Singleflight or mutex for hot keys. TTL jitter for mass expiry.

8. **Handle failures**: Circuit breaker to fall through to DB. Negative caching for penetration. Bloom filter if attackers are a concern.

9. **Size the cache**: Quick back-of-envelope: N items x avg_size x overhead x replication. State the number.

10. **State the key metric**: "We would monitor hit rate, targeting 90%+, with alerts on drops."

11. **Protect the data**: private responses are `no-store` at the CDN, no PII in keys, erasure purges every layer, and cached values carry their owner ID (§7.5, §13 Cases 7–8).

This checklist, delivered fluently in an interview, demonstrates production-grade understanding of caching.
