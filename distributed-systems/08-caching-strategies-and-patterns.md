# Caching Strategies and Patterns

A production-grade reference covering caching from first principles through production architecture. Covers the five foundational read/write strategies, cache invalidation approaches that actually work, thundering herd mitigation, eviction policies and their tradeoffs, Redis internals, distributed cache architecture with multi-level hierarchies, consistent hashing for cache sharding, capacity planning math, failure modes, and the concrete caching patterns that appear in every system design interview. Written for senior and Staff+ engineers who need to discuss caching with precision under interview pressure.

Prerequisites: familiarity with distributed system fundamentals from `00-primitives-and-system-models.md` and consistency models from `04-consistency-models-linearizability-to-eventual.md`.

---

## Table of Contents

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
        """Read-through with cache-aside pattern."""
        # Step 1: Check cache
        cached = self.cache.get(key)
        if cached is not None:
            return json.loads(cached)

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

**Pros**: Cache is always consistent with the database (no stale reads after writes). Simplifies read path since the cache is guaranteed to be fresh. Combined with read-through, gives you a complete caching abstraction.

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
│ Write-Through    │ Hit: low   │ High     │ Strong    │ Medium       │ Read-after-  │
│                  │            │ (2x)     │           │              │ write needs  │
├──────────────────┼────────────┼──────────┼───────────┼──────────────┼──────────────┤
│ Write-Behind     │ Hit: low   │ Very low │ Eventual  │ High         │ Write-heavy  │
│                  │            │          │ (weak)    │              │ workloads    │
├──────────────────┼────────────┼──────────┼───────────┼──────────────┼──────────────┤
│ Refresh-Ahead    │ Always low │ Low      │ Eventual  │ High         │ Hot keys,    │
│                  │ (no miss)  │          │           │              │ predictable  │
└──────────────────┴────────────┴──────────┴───────────┴──────────────┴──────────────┘
```

**Decision matrix**: Start with cache-aside (it is the default). Add write-through if you need read-after-write consistency. Use write-behind only for write-heavy workloads where you can tolerate data loss. Add refresh-ahead for known hot keys. In practice, most production systems use cache-aside with TTL-based invalidation and event-driven invalidation for critical paths.

---

## 3. Cache Invalidation

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

**Tradeoff**: Serializes requests for the same key. If the lock holder is slow or crashes, waiting requests are delayed. The lock TTL must be tuned carefully -- too short and the lock expires before the fetch completes; too long and a crashed lock holder blocks everyone.

### 4.3 Solution 2: Probabilistic Early Expiration (PER)

Instead of all entries expiring at exactly the same time, each access independently decides whether to refresh the entry early, with the probability increasing as the TTL approaches. This spreads out the refresh load.

```python
import math
import random
import time

def should_refresh_early(
    stored_at: float,
    ttl: float,
    beta: float = 1.0  # Tuning parameter: higher = more aggressive refresh
) -> bool:
    """
    Probabilistic Early Recomputation (PER) algorithm.
    Based on "Optimal Probabilistic Cache Stampede Prevention" (Vattani et al.)
    
    Returns True if this request should trigger an early refresh.
    Probability increases exponentially as expiry approaches.
    """
    now = time.time()
    expiry = stored_at + ttl
    remaining = expiry - now

    if remaining <= 0:
        return True  # Already expired

    # XFetch algorithm: P(refresh) = beta * ln(random()) * -compute_time
    # Simplified: probability increases as remaining time decreases
    threshold = remaining / ttl
    return random.random() > threshold ** beta
```

**Tradeoff**: No locks, no coordination. But multiple requests may still refresh simultaneously (though far fewer than without PER). Works best for high-traffic keys where the probabilistic spread is effective.

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
            if key in self._in_flight:
                # Another coroutine is already fetching — wait for its result
                return await self._in_flight[key]

            future = asyncio.get_event_loop().create_future()
            self._in_flight[key] = future

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

    # Even if 10,000 concurrent requests call this for the same user_id,
    # only ONE database query executes
    result = await flight.do(key, lambda: db.fetch_user(user_id))

    await redis.setex(key, 300, json.dumps(result))
    return result
```

**Singleflight is the recommended production solution**. It is simple, deterministic (no probabilistic behavior), and reduces N concurrent requests to exactly 1 database query. Combined with a short mutex as a fallback, it handles every thundering herd scenario.

---

## 5. Cache Eviction Policies

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

Maintains two LRU lists -- one for items accessed once ("recency") and one for items accessed more than once ("frequency") -- and dynamically adjusts the partition between them based on the observed workload. Patented by IBM.

**Strengths**: Adapts to workload changes. Handles both recency-biased and frequency-biased access patterns. Scan-resistant.

**Weaknesses**: More complex to implement. Patent encumbered (though the patent has expired in some jurisdictions). Higher per-operation overhead than simple LRU.

### 5.4 TinyLFU (The Modern Standard)

The key insight of TinyLFU is to separate the **admission policy** from the **eviction policy**. It uses a frequency sketch (Count-Min Sketch) to cheaply estimate access frequencies, and only admits a new item to the cache if its estimated frequency exceeds that of the item it would replace.

W-TinyLFU (Windowed TinyLFU), used in Caffeine (Java's best in-process cache), combines:
1. **Window cache** (1% of capacity, LRU): Admits all new entries, giving them a chance to build up frequency.
2. **Main cache** (99% of capacity, segmented LRU): An item from the window cache is only promoted to the main cache if TinyLFU's frequency sketch says it is more popular than the main cache's eviction candidate.
3. **Count-Min Sketch**: A space-efficient probabilistic data structure that estimates item frequencies using 4 hash functions and a compact array. Periodically halved (aging) to adapt to changing access patterns.

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

**Why TinyLFU beats pure LRU for most workloads**: Real-world cache access patterns follow power-law distributions (Zipfian). A small number of items are accessed very frequently, and a long tail of items are accessed rarely. LRU wastes cache space on long-tail items that happen to be accessed recently. TinyLFU's admission filter keeps these out, reserving cache space for genuinely popular items. In benchmarks, Caffeine with W-TinyLFU consistently outperforms LRU, LFU, and ARC across diverse workloads, often by 10-30% in hit rate.

### 5.5 Other Policies

- **FIFO (First In, First Out)**: Evict the oldest entry. Simple but ignores access patterns entirely. Useful only when all entries are equally likely to be accessed (rare in practice).
- **Random**: Evict a random entry. Surprisingly competitive with LRU for uniform access patterns and much simpler to implement. Used in some CPU cache designs.
- **TTL-based eviction**: Evict entries closest to expiry. Not a standalone eviction policy -- usually combined with LRU/LFU as a secondary signal.

### 5.6 Eviction Policy Comparison

```
┌──────────┬──────────────┬──────────────┬────────────────┬────────────────────┐
│ Policy   │ Hit Rate     │ Scan         │ Implementation │ Used In            │
│          │ (typical)    │ Resistant?   │ Complexity     │                    │
├──────────┼──────────────┼──────────────┼────────────────┼────────────────────┤
│ LRU      │ Good         │ No           │ Low            │ Redis, Memcached   │
│ LFU      │ Good         │ Yes          │ Medium         │ Redis (since 4.0)  │
│ ARC      │ Very good    │ Yes          │ High           │ ZFS, PostgreSQL    │
│ TinyLFU  │ Excellent    │ Yes          │ High           │ Caffeine (Java)    │
│ FIFO     │ Poor         │ N/A          │ Very low       │ Simple buffers     │
│ Random   │ Fair         │ Yes          │ Very low       │ CPU caches (some)  │
└──────────┴──────────────┴──────────────┴────────────────┴────────────────────┘
```

**Interview guidance**: Know LRU (it is the default everywhere), know why TinyLFU is better (admission filtering based on frequency estimation), and know ARC exists for completeness. If asked "which eviction policy would you use?", the answer is: LRU for a remote cache (Redis default), TinyLFU/Caffeine for an in-process cache (Java/JVM), and LRU with manual hot-key pinning for everything else.

---

## 6. Redis as a Cache -- Deep Dive

Redis is the dominant cache technology in production systems. Interviewers expect you to know it beyond "it's a key-value store."

### 6.1 Data Structures

Redis is not just key-value. It is a data structure server, and choosing the right data structure for your cache is a design decision with significant performance implications.

- **String**: The basic type. `SET key value EX ttl`. Up to 512MB. Use for simple cached values (JSON blobs, serialized objects). Memory-efficient for values under 44 bytes (embedded encoding).

- **Hash**: A map of field-value pairs under a single key. `HSET user:42 name "Bob" email "bob@x.com"`. Use when you need to read/update individual fields without deserializing the entire value. Memory-efficient for small hashes (<128 fields, <64 byte values) via ziplist encoding.

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

**Recommendation**: Use `allkeys-lru` or `allkeys-lfu` for cache workloads. The `volatile-*` policies only evict keys with an explicit TTL, which means keys without TTL are never evicted -- dangerous if any cache population path forgets to set a TTL. The `allkeys-lfu` policy is better when access patterns are highly skewed (a few hot keys dominate), which is most real-world workloads.

**Memory overhead per key**: Every Redis key carries metadata overhead beyond the value itself.

```
APPROXIMATE MEMORY OVERHEAD PER KEY (Redis 7.x):

  Key metadata (dictEntry):        ~70-80 bytes
  Includes: hash table entry, key SDS string, robj pointer,
            expiry (if set), LRU/LFU metadata

  Example: storing a 100-byte JSON string with a key name of 20 bytes
    Key name (SDS):     20 + 9 bytes (SDS header + null terminator) = ~29 bytes
    Value (SDS):        100 + 9 bytes = ~109 bytes
    dict entry:         ~24 bytes (3 pointers)
    robj (key):         ~16 bytes
    robj (value):       ~16 bytes
    Expiry:             ~16 bytes (if TTL is set)
    jemalloc alignment: rounds up to allocation class boundaries

    Total: ~240-280 bytes for a 100-byte value
    Overhead ratio: ~1.5-1.8x for small values, approaches 1x for large values

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

**Multi-key operations**: Commands that operate on multiple keys (MGET, pipeline) require all keys to be on the same node. Use hash tags `{user:42}:profile` and `{user:42}:sessions` to force related keys to the same slot.

### 6.5 Redis Sentinel (High Availability)

For single-master deployments that need automatic failover without the complexity of Redis Cluster.

Sentinel is a separate process that monitors Redis instances, detects master failure, promotes a replica to master, and notifies clients of the topology change. Requires a quorum of Sentinel instances (typically 3) to agree on a failover to prevent split-brain.

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

---

## 7. Distributed Caching Architecture

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
│ Threading        │ Single-threaded (mostly) │ Multi-threaded           │
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

Recommendation: Redis for almost everything. Memcached only when you need
multi-threaded performance for simple key-value workloads at extreme scale
(Facebook's TAO-era Memcache fleet is the canonical example).
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

---

## 8. Consistent Hashing

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

  Without vnodes (3 physical nodes, uneven distribution):
    Node A: 45% of keys  (owns large arc)
    Node B: 35% of keys
    Node C: 20% of keys  (owns small arc)

  With 150 vnodes per physical node:
    Node A: ~33.2% of keys
    Node B: ~33.5% of keys
    Node C: ~33.3% of keys   (approximately uniform)
```

**Typical vnode count**: 100-200 per physical node. Higher counts give better distribution but increase the ring metadata size and lookup cost.

### 8.4 Jump Consistent Hash

An alternative to ring-based consistent hashing. Jump consistent hash (Lamport and Thaler, 2014) maps a key to one of N buckets using a fast, zero-memory algorithm. It achieves perfectly uniform distribution and moves the minimum number of keys when N changes.

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

**Pros**: O(ln N) time, zero memory. Perfect key distribution. Minimal key movement.

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

# Adding a node remaps only ~1/N keys
ring.add_node("redis-4")
node = ring.get_node("user:42")       # Probably still "redis-2"
```

### 8.6 Ketama Algorithm

The Ketama algorithm is the specific consistent hashing implementation used by libmemcached and most Memcached client libraries. It uses MD5 hashing to generate 4 virtual node points per hash (by splitting the 128-bit MD5 output into 4 32-bit values), with a default of 40 hash iterations per physical node, yielding 160 virtual nodes per physical node. Ketama is the de facto standard for Memcached cluster sharding.

---

## 9. Cache in System Design Interviews

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

### 10.1 Memory Sizing Formula

```
MEMORY CALCULATION:

  Total memory = N × (key_size + value_size + overhead) × replication_factor

  Where:
    N                  = number of unique cached items
    key_size           = average key length in bytes (e.g., "user:profile:12345" = 19 bytes)
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

    → 2 Redis nodes × 8GB each, or 1 node with 16GB
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

    DB cost: RDS db.r6g.xlarge = $0.48/hr = ~$350/month
    Cache cost: ElastiCache r6g.large (13GB) = $0.17/hr = ~$125/month

    With cache: DB load reduced 90%, can use smaller DB instance → net savings.
    Cache cost is almost always lower than the DB cost it replaces.

  Memory cost:
    Redis (AWS ElastiCache): ~$13/GB/month (on-demand)
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

When a Redis master fails and a replica is promoted, there is a window (typically 10-30 seconds with Sentinel, shorter with Cluster) during which:
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

# Command latency P99 (using Redis latency tracking)
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

This checklist, delivered fluently in an interview, demonstrates production-grade understanding of caching.
