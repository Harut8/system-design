# Instagram-Scale Distributed Like Counter: Design Document

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|----------|----------|--------|
| **Scale** | Items with counters? | Tens of billions (posts, reels, comments) |
| **Scale** | Peak write throughput? | Millions of likes/unlikes per second |
| **Scale** | Peak read throughput? | Tens of millions per second |
| **Accuracy** | Acceptable error bounds? | ±0.1% for display, exact for dedup |
| **Consistency** | Read-after-write required? | Local immediate feedback, global eventual |
| **Latency** | Write P99? | ≤ 50ms |
| **Latency** | Read P99? | ≤ 20ms |
| **Availability** | Target uptime? | 99.99% |
| **Durability** | Data loss tolerance? | Zero permanent loss |
| **Idempotency** | Duplicate handling? | Must be idempotent (user can only like once) |

### Key Assumptions

1. **Eventual consistency is acceptable** - Users tolerate seeing slightly stale counts
2. **Exact deduplication required** - User-item like relationships must be exact
3. **Read-heavy workload** - Reads outnumber writes 100:1 on average
4. **Viral posts create hot spots** - Top 0.1% of posts get 50%+ of likes
5. **Counter extensibility** - Same system for views, shares, saves

---

## 2. Capacity Estimates

### Scale Numbers

```
Active users:          500 million daily
Items (posts/reels):   50 billion total
Items with likes:      10 billion (20% have at least 1 like)

Likes per day:         5 billion
Likes per second:      ~60,000 avg, ~500,000 peak (10x burst)
Unlikes per second:    ~6,000 avg (10% of likes)

Reads per second:      ~10 million avg, ~50 million peak
Batch reads:           50 items per feed load
```

### Storage Estimates

```
User-Item Like Records (deduplication):
  - Record: user_id (8B) + item_id (8B) + timestamp (8B) + type (1B) = 25 bytes
  - 500B total likes × 25B = 12.5 TB
  - With indexes: ~25 TB

Counter Storage:
  - Counter: item_id (8B) + count (8B) + metadata (16B) = 32 bytes
  - 50B items × 32B = 1.6 TB
  - Hot counters in memory: Top 100M items × 32B = 3.2 GB

Daily Growth:
  - Like records: 5B × 25B = 125 GB/day
  - New items: 100M × 32B = 3.2 GB/day
```

### Memory Requirements

```
Hot counter cache (Redis):
  - 100M hot items × 50 bytes = 5 GB
  - With replication: 15 GB (3 replicas)

Dedup cache (Bloom filters):
  - 1B user-item pairs × 10 bits = 1.25 GB per shard
  - 10 shards = 12.5 GB total

Write buffer (in-memory aggregation):
  - 10M pending increments × 32B = 320 MB per node
```

---

## 3. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CLIENTS (Global)                                │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         CDN + Global Load Balancer                           │
│                    (CloudFlare / AWS Global Accelerator)                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
            ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
            │  Region US  │   │  Region EU  │   │ Region APAC │
            └─────────────┘   └─────────────┘   └─────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            API Gateway Layer                                 │
│                      (Rate limiting, Auth, Routing)                          │
└─────────────────────────────────────────────────────────────────────────────┘
            │                         │                         │
            ▼                         ▼                         ▼
    ┌───────────────┐         ┌───────────────┐         ┌───────────────┐
    │  Like Service │         │ Counter Read  │         │  HasUserLiked │
    │  (Write Path) │         │   Service     │         │    Service    │
    └───────────────┘         └───────────────┘         └───────────────┘
            │                         │                         │
            ▼                         ▼                         ▼
    ┌───────────────┐         ┌───────────────┐         ┌───────────────┐
    │    Kafka      │         │ Counter Cache │         │   Dedup DB    │
    │ (Like Events) │         │   (Redis)     │         │ (Cassandra)   │
    └───────────────┘         └───────────────┘         └───────────────┘
            │                         ▲                         ▲
            ▼                         │                         │
    ┌───────────────────────────────────────────────────────────────────────┐
    │                     Counter Aggregation Service                        │
    │               (Flink - batches increments, deduplicates)               │
    └───────────────────────────────────────────────────────────────────────┘
            │                         │
            ▼                         ▼
    ┌───────────────┐         ┌───────────────┐
    │  Counter DB   │         │   Dedup DB    │
    │ (Sharded PG / │         │ (Cassandra)   │
    │  ScyllaDB)    │         │               │
    └───────────────┘         └───────────────┘
```

---

## 4. Data Modeling

### 4.1 User-Item Like Records (Deduplication Store)

**Storage: Cassandra (ScyllaDB)**

```sql
-- Primary table for deduplication and "has user liked" queries
CREATE TABLE user_likes (
    user_id       BIGINT,
    item_type     TINYINT,      -- 1=post, 2=reel, 3=comment
    item_id       BIGINT,
    liked_at      TIMESTAMP,
    PRIMARY KEY ((user_id), item_type, item_id)
) WITH CLUSTERING ORDER BY (item_type ASC, item_id DESC);

-- Reverse index for "who liked this item" (optional, for admin)
CREATE TABLE item_likes (
    item_id       BIGINT,
    item_type     TINYINT,
    user_id       BIGINT,
    liked_at      TIMESTAMP,
    PRIMARY KEY ((item_id, item_type), user_id)
);
```

**Why Cassandra?**
- Handles billions of records with predictable latency
- Partition by user_id distributes load evenly
- Fast point lookups for deduplication
- Tunable consistency (LOCAL_QUORUM for writes)

### 4.2 Counter Storage

**Storage: Sharded PostgreSQL or ScyllaDB**

```sql
-- Counter table (PostgreSQL)
CREATE TABLE counters (
    item_id       BIGINT NOT NULL,
    item_type     SMALLINT NOT NULL,
    counter_type  SMALLINT NOT NULL,  -- 1=likes, 2=views, 3=shares
    count         BIGINT DEFAULT 0,
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (item_id, item_type, counter_type)
);

-- Partition by item_id hash for horizontal scaling
-- 256 shards, shard_key = item_id % 256
```

**ScyllaDB Alternative (higher scale):**

```sql
CREATE TABLE counters (
    item_id       BIGINT,
    item_type     TINYINT,
    counter_type  TINYINT,
    count         COUNTER,
    PRIMARY KEY ((item_id, item_type), counter_type)
);
```

### 4.3 Hot Counter Cache

**Storage: Redis Cluster**

```
Key:    counter:{item_type}:{item_id}:{counter_type}
Value:  INT64 (count value)
TTL:    1 hour (re-populated on miss)

Example:
  counter:1:123456789:1 = 1523847  (post 123456789 has 1.5M likes)
```

---

## 5. Write Path: Like/Unlike Flow

### 5.1 Synchronous Path (User-Facing)

```
┌──────────────────────────────────────────────────────────────────┐
│                      POST /v1/like                                │
│              { "user_id": 123, "item_id": 456, "type": "post" }   │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  1. Rate Limit Check (Redis)                                      │
│     - Max 100 likes/minute per user                               │
│     - Return 429 if exceeded                                      │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  2. Dedup Check (Bloom Filter + Cassandra)                       │
│     - Check Bloom filter first (fast negative)                   │
│     - If positive, verify with Cassandra                         │
│     - If already liked, return success (idempotent)              │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  3. Write to Kafka (async, fire-and-forget)                      │
│     - Topic: like-events                                         │
│     - Key: item_id (for ordering)                                │
│     - Acks: 1 (leader only for speed)                            │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  4. Optimistic Counter Increment (Redis)                          │
│     - INCR counter:{type}:{item_id}:1                             │
│     - For immediate UI feedback                                   │
│     - May be corrected later by aggregator                       │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  5. Return 200 OK                                                 │
│     - Include optimistic new count                               │
│     - Total latency target: < 20ms                               │
└──────────────────────────────────────────────────────────────────┘
```

### 5.2 Asynchronous Path (Counter Aggregation)

```
┌──────────────────────────────────────────────────────────────────┐
│                     Kafka: like-events                            │
│                   (Partitioned by item_id)                        │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                 Counter Aggregation Service (Flink)               │
│                                                                   │
│  1. Consume events in micro-batches (1-second windows)           │
│  2. Deduplicate within window (user_id + item_id)                │
│  3. Verify against Cassandra (exactly-once processing)           │
│  4. Write dedup record to Cassandra                              │
│  5. Aggregate increments per item_id                              │
│  6. Batch write to Counter DB                                    │
│  7. Update Redis cache                                           │
│  8. Update Bloom filters                                          │
└──────────────────────────────────────────────────────────────────┘
```

### 5.3 Handling Write Contention (Hot Items)

Problem: Viral posts get millions of likes per second → single row bottleneck

**Solution: Sharded Counters**

```
Instead of:
  counter:1:viral_post:1 = 5000000

Use:
  counter:1:viral_post:1:shard_0 = 625000
  counter:1:viral_post:1:shard_1 = 625000
  counter:1:viral_post:1:shard_2 = 625000
  ...
  counter:1:viral_post:1:shard_7 = 625000

Write: Randomly pick a shard → INCR counter:1:viral_post:1:shard_{rand(8)}
Read:  SUM all shards (8 MGET calls, parallel)
```

**When to shard:**
- Detect hot items: > 1000 writes/second for 10 seconds
- Automatically create 8 shards
- Merge back to single counter after cooling (< 100 writes/sec for 1 hour)

```python
def increment_counter(item_id, item_type, counter_type):
    base_key = f"counter:{item_type}:{item_id}:{counter_type}"

    # Check if sharded
    shard_count = redis.get(f"{base_key}:shards")

    if shard_count:
        # Hot item: write to random shard
        shard = random.randint(0, int(shard_count) - 1)
        redis.incr(f"{base_key}:shard_{shard}")
    else:
        # Normal item: single counter
        redis.incr(base_key)


def get_counter(item_id, item_type, counter_type):
    base_key = f"counter:{item_type}:{item_id}:{counter_type}"

    shard_count = redis.get(f"{base_key}:shards")

    if shard_count:
        # Sum all shards
        keys = [f"{base_key}:shard_{i}" for i in range(int(shard_count))]
        values = redis.mget(keys)
        return sum(int(v or 0) for v in values)
    else:
        return int(redis.get(base_key) or 0)
```

---

## 6. Read Path: Counter Retrieval

### 6.1 Single Item Read

```
┌──────────────────────────────────────────────────────────────────┐
│               GET /v1/count?item_id=456&type=post                 │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  1. Check Redis Cache                                             │
│     - Key: counter:1:456:1                                        │
│     - Hit rate: ~95%                                              │
│     - Latency: < 1ms                                              │
└──────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │ Cache Miss        │
                    ▼                   ▼
┌───────────────────────────┐  ┌───────────────────────────┐
│  2. Read from Counter DB  │  │   Cache Hit: Return       │
│     - Query by PK         │  │   Latency: < 2ms          │
│     - Latency: ~5-10ms    │  │                           │
└───────────────────────────┘  └───────────────────────────┘
                    │
                    ▼
┌───────────────────────────┐
│  3. Populate Cache        │
│     - SET with 1hr TTL    │
│     - Return to client    │
└───────────────────────────┘
```

### 6.2 Batch Read (Feed Load)

```
┌──────────────────────────────────────────────────────────────────┐
│     GET /v1/counts?item_ids=1,2,3,...,50&type=post                │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  1. MGET from Redis (single round-trip)                           │
│     - Keys: counter:1:1:1, counter:1:2:1, ... counter:1:50:1     │
│     - Returns: [1523, null, 847, null, ...]                       │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  2. For cache misses: Batch query Counter DB                      │
│     - WHERE item_id IN (2, 4, 7, ...)                             │
│     - Parallel queries to different shards                       │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  3. Populate cache for misses + Return all                        │
│     - Pipeline SET commands                                       │
│     - Total latency target: < 15ms                                │
└──────────────────────────────────────────────────────────────────┘
```

### 6.3 HasUserLiked API

```
┌──────────────────────────────────────────────────────────────────┐
│  GET /v1/has_liked?user_id=123&item_ids=1,2,3,...,50&type=post    │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  1. Check local Bloom Filter (per-user shard)                     │
│     - Bloom filter partitioned by user_id % 1000                  │
│     - Fast negative: If not in bloom, definitely not liked       │
│     - False positive rate: ~1%                                    │
└──────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
              Possible Yes         Definite No
                    ▼                   ▼
┌───────────────────────────┐  ┌───────────────────────────┐
│  2. Verify with Cassandra │  │   Return: not liked       │
│     - Multi-get query     │  │                           │
│     - Latency: ~5ms       │  │                           │
└───────────────────────────┘  └───────────────────────────┘
```

---

## 7. Idempotency & Deduplication

### 7.1 Deduplication Strategy

```
┌─────────────────────────────────────────────────────────────────┐
│                  Three-Layer Deduplication                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Layer 1: Bloom Filter (In-Memory)                               │
│    - Ultra-fast (nanoseconds)                                    │
│    - False positive rate: 1%                                     │
│    - Size: ~1.25 GB for 1B entries                               │
│    - Use case: Quick reject of definitely-new likes             │
│                                                                  │
│  Layer 2: Redis Set (Recent likes)                               │
│    - Key: recent_likes:{user_id}                                 │
│    - SADD returns 0 if already exists                            │
│    - TTL: 24 hours                                               │
│    - Size: Last 1000 likes per user                              │
│                                                                  │
│  Layer 3: Cassandra (Source of Truth)                            │
│    - Final verification                                          │
│    - Write with IF NOT EXISTS (LWT)                              │
│    - Or use timestamp-based last-write-wins                      │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 7.2 Like Flow with Deduplication

```python
async def process_like(user_id: int, item_id: int, item_type: int):
    # Layer 1: Bloom filter check
    key = f"{user_id}:{item_type}:{item_id}"
    if bloom_filter.might_contain(key):
        # Possible duplicate - verify

        # Layer 2: Recent likes cache
        recent_key = f"recent_likes:{user_id}"
        if await redis.sismember(recent_key, f"{item_type}:{item_id}"):
            return {"status": "already_liked", "deduplicated": True}

        # Layer 3: Cassandra verification
        existing = await cassandra.execute("""
            SELECT liked_at FROM user_likes
            WHERE user_id = ? AND item_type = ? AND item_id = ?
        """, [user_id, item_type, item_id])

        if existing:
            # Update bloom filter and recent cache for next time
            await redis.sadd(recent_key, f"{item_type}:{item_id}")
            return {"status": "already_liked", "deduplicated": True}

    # New like - process it
    # 1. Write to Cassandra (idempotent upsert)
    await cassandra.execute("""
        INSERT INTO user_likes (user_id, item_type, item_id, liked_at)
        VALUES (?, ?, ?, ?)
    """, [user_id, item_type, item_id, datetime.utcnow()])

    # 2. Update bloom filter
    bloom_filter.add(key)

    # 3. Update recent likes cache
    await redis.sadd(recent_key, f"{item_type}:{item_id}")
    await redis.expire(recent_key, 86400)  # 24 hours

    # 4. Publish event for counter increment
    await kafka.produce("like-events", {
        "user_id": user_id,
        "item_id": item_id,
        "item_type": item_type,
        "action": "like",
        "timestamp": datetime.utcnow().isoformat()
    })

    return {"status": "liked", "deduplicated": False}
```

### 7.3 Unlike Handling

```python
async def process_unlike(user_id: int, item_id: int, item_type: int):
    # Verify the like exists
    existing = await cassandra.execute("""
        SELECT liked_at FROM user_likes
        WHERE user_id = ? AND item_type = ? AND item_id = ?
    """, [user_id, item_type, item_id])

    if not existing:
        return {"status": "not_liked"}

    # 1. Delete from Cassandra
    await cassandra.execute("""
        DELETE FROM user_likes
        WHERE user_id = ? AND item_type = ? AND item_id = ?
    """, [user_id, item_type, item_id])

    # 2. Remove from recent cache
    await redis.srem(f"recent_likes:{user_id}", f"{item_type}:{item_id}")

    # 3. Note: Cannot remove from Bloom filter (false positives acceptable)

    # 4. Publish decrement event
    await kafka.produce("like-events", {
        "user_id": user_id,
        "item_id": item_id,
        "item_type": item_type,
        "action": "unlike",
        "timestamp": datetime.utcnow().isoformat()
    })

    return {"status": "unliked"}
```

---

## 8. Storage Architecture

### 8.1 Storage Tiers

```
┌─────────────────────────────────────────────────────────────────┐
│                      Storage Hierarchy                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Tier 0: In-Process Cache (Go/Java heap)                         │
│    - Size: 100 MB per instance                                   │
│    - Latency: < 0.1ms                                            │
│    - Contents: Top 10K counters, recent dedup checks             │
│    - TTL: 1 second                                               │
│                                                                  │
│  Tier 1: Redis Cluster                                           │
│    - Size: 50 GB (10 nodes × 5 GB)                               │
│    - Latency: < 1ms                                              │
│    - Contents: All hot counters, recent likes sets               │
│    - TTL: 1 hour for counters, 24h for likes                     │
│                                                                  │
│  Tier 2: Counter DB (Sharded PostgreSQL / ScyllaDB)              │
│    - Size: 5 TB                                                  │
│    - Latency: 5-10ms                                             │
│    - Contents: All counter values, source of truth               │
│    - Shards: 256, by item_id hash                                │
│                                                                  │
│  Tier 3: Dedup DB (Cassandra)                                    │
│    - Size: 30 TB                                                 │
│    - Latency: 5-15ms                                             │
│    - Contents: All user-item like records                        │
│    - Replication: 3                                              │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 8.2 Sharding Strategy

**Counter DB Sharding:**

```
Shard count: 256
Shard key: hash(item_id) % 256

Benefits:
  - Even distribution (item_ids are sequential)
  - Easy to add shards (consistent hashing)
  - Parallel batch queries

Example:
  item_id 123456789 → hash = 0x7B → shard 123

Shard placement:
  Shards 0-63:    Node 1 (primary), Node 5 (replica)
  Shards 64-127:  Node 2 (primary), Node 6 (replica)
  Shards 128-191: Node 3 (primary), Node 7 (replica)
  Shards 192-255: Node 4 (primary), Node 8 (replica)
```

**Cassandra Partitioning:**

```
Partition key: user_id
Clustering key: (item_type, item_id)

Benefits:
  - All user's likes on same node
  - Range queries possible (all likes for a user)
  - No hot partitions (users have similar like counts)

Virtual nodes: 256 per physical node
Replication factor: 3
```

---

## 9. Caching Strategy

### 9.1 Cache Layers

```
┌─────────────────────────────────────────────────────────────────┐
│                      Redis Cache Structure                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. Counter Cache                                                │
│     Key:     counter:{item_type}:{item_id}:{counter_type}        │
│     Type:    STRING (int64)                                      │
│     TTL:     1 hour                                              │
│     Size:    ~500 MB for 100M hot items                          │
│                                                                  │
│  2. Sharded Counter Metadata                                     │
│     Key:     counter:{type}:{id}:{ctype}:shards                  │
│     Type:    STRING (int: shard count)                           │
│     TTL:     1 hour                                              │
│                                                                  │
│  3. Hot Item Detection                                           │
│     Key:     hot_items                                           │
│     Type:    ZSET (item_id → write_count)                        │
│     TTL:     1 hour sliding window                               │
│                                                                  │
│  4. Recent User Likes (Dedup)                                    │
│     Key:     recent_likes:{user_id}                              │
│     Type:    SET (item_type:item_id)                             │
│     TTL:     24 hours                                            │
│     Max:     1000 entries per user (SREM oldest)                 │
│                                                                  │
│  5. Rate Limit Counters                                          │
│     Key:     rate:{user_id}:likes                                │
│     Type:    STRING (count)                                      │
│     TTL:     1 minute                                            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 9.2 Cache Invalidation

**Strategy: Write-through + TTL**

```python
async def update_counter_cache(item_id: int, item_type: int, delta: int):
    key = f"counter:{item_type}:{item_id}:1"

    # Atomic increment (handles concurrent updates)
    new_value = await redis.incrby(key, delta)

    # Ensure TTL is set
    await redis.expire(key, 3600)  # 1 hour

    return new_value


async def get_counter_with_fallback(item_id: int, item_type: int):
    key = f"counter:{item_type}:{item_id}:1"

    # Try cache first
    cached = await redis.get(key)
    if cached is not None:
        return int(cached)

    # Cache miss: read from DB
    result = await counter_db.query(
        "SELECT count FROM counters WHERE item_id = $1 AND item_type = $2",
        [item_id, item_type]
    )

    count = result[0]['count'] if result else 0

    # Populate cache
    await redis.setex(key, 3600, count)

    return count
```

### 9.3 Handling Cache Inconsistency

```
Problem: Redis counter may drift from DB due to:
  - Network partitions
  - Failed async updates
  - Counter aggregation lag

Solution: Periodic reconciliation

Every 5 minutes:
  1. Sample 10,000 random counters from Redis
  2. Compare with Counter DB values
  3. If drift > 1%: refresh from DB
  4. Log drift metrics for monitoring

For viral posts (> 100k likes):
  1. More frequent reconciliation (every 30 seconds)
  2. Accept higher drift (up to 5%) for availability
```

---

## 10. Failure Handling

### 10.1 Failure Scenarios

| Failure | Impact | Mitigation |
|---------|--------|------------|
| Redis down | Read latency increase | Fall back to DB, serve stale |
| Kafka down | Likes not counted | Buffer in Redis, retry |
| Counter DB down | New counts lost | Serve cached, queue writes |
| Cassandra down | Dedup fails | Accept duplicates, reconcile later |
| Network partition | Inconsistent counts | Eventual consistency by design |

### 10.2 Retry Strategy

```python
# Retry configuration
RETRY_CONFIG = {
    "redis": {
        "max_retries": 3,
        "backoff": "exponential",
        "base_delay_ms": 10,
        "max_delay_ms": 100,
        "timeout_ms": 50
    },
    "kafka": {
        "max_retries": 5,
        "backoff": "exponential",
        "base_delay_ms": 100,
        "max_delay_ms": 5000,
        "timeout_ms": 30000
    },
    "counter_db": {
        "max_retries": 3,
        "backoff": "exponential",
        "base_delay_ms": 50,
        "max_delay_ms": 500,
        "timeout_ms": 5000
    },
    "cassandra": {
        "max_retries": 3,
        "backoff": "fixed",
        "delay_ms": 100,
        "timeout_ms": 5000
    }
}
```

### 10.3 Graceful Degradation

```
┌─────────────────────────────────────────────────────────────────┐
│                   Degradation Modes                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Mode 1: Full Operation                                          │
│    - All systems healthy                                         │
│    - Full deduplication                                          │
│    - Accurate counts                                             │
│                                                                  │
│  Mode 2: Read-Only Mode                                          │
│    - Kafka/DB write issues                                       │
│    - Accept likes but buffer in Redis                            │
│    - Serve cached counts (may be stale)                          │
│                                                                  │
│  Mode 3: Degraded Accuracy                                       │
│    - Cassandra dedup issues                                      │
│    - Skip dedup check, risk duplicates                           │
│    - Count correction on recovery                                │
│                                                                  │
│  Mode 4: Cache-Only Mode                                         │
│    - Counter DB down                                             │
│    - Serve from Redis only                                       │
│    - Return X-Stale-Data header                                  │
│                                                                  │
│  Mode 5: Static Fallback                                         │
│    - Redis and DB down                                           │
│    - Return last-known counts from CDN                           │
│    - Accept all likes to local buffer                            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 10.4 Data Recovery

```python
async def reconcile_counters():
    """
    Runs after outage to fix any count discrepancies.
    Replays Kafka events from checkpoint.
    """
    # 1. Get last processed offset from checkpoint
    last_offset = await get_checkpoint("like-events")

    # 2. Replay events from that offset
    events = await kafka.consume("like-events", from_offset=last_offset)

    # 3. Recompute counts
    counter_deltas = {}
    for event in events:
        item_key = (event.item_id, event.item_type)

        # Verify dedup in Cassandra
        if event.action == "like":
            exists = await cassandra.check_like_exists(
                event.user_id, event.item_id, event.item_type
            )
            if exists:
                continue  # Already counted

            counter_deltas[item_key] = counter_deltas.get(item_key, 0) + 1
        else:
            counter_deltas[item_key] = counter_deltas.get(item_key, 0) - 1

    # 4. Apply corrections
    for (item_id, item_type), delta in counter_deltas.items():
        await counter_db.execute("""
            UPDATE counters
            SET count = count + $1
            WHERE item_id = $2 AND item_type = $3
        """, [delta, item_id, item_type])

        # Update cache
        await redis.incrby(f"counter:{item_type}:{item_id}:1", delta)

    # 5. Update checkpoint
    await save_checkpoint("like-events", events[-1].offset)
```

---

## 11. API Design

### 11.1 Like API

```http
POST /v1/like
Content-Type: application/json
Authorization: Bearer {token}

{
  "item_id": "123456789",
  "item_type": "post"   // post, reel, comment
}

Response 200:
{
  "status": "liked",
  "count": 1524,              // Optimistic count
  "count_formatted": "1.5K",  // For display
  "user_liked": true
}

Response 200 (already liked - idempotent):
{
  "status": "already_liked",
  "count": 1524,
  "user_liked": true
}

Response 429 (rate limited):
{
  "error": "rate_limited",
  "retry_after": 60
}
```

### 11.2 Unlike API

```http
DELETE /v1/like
Content-Type: application/json
Authorization: Bearer {token}

{
  "item_id": "123456789",
  "item_type": "post"
}

Response 200:
{
  "status": "unliked",
  "count": 1523,
  "user_liked": false
}
```

### 11.3 GetCount API (Single)

```http
GET /v1/count?item_id=123456789&item_type=post

Response 200:
{
  "item_id": "123456789",
  "item_type": "post",
  "counts": {
    "likes": 1524,
    "views": 89234,
    "shares": 156
  },
  "cached": true,
  "as_of": "2024-01-15T10:30:00Z"
}
```

### 11.4 GetCount API (Batch)

```http
POST /v1/counts
Content-Type: application/json

{
  "items": [
    {"item_id": "1", "item_type": "post"},
    {"item_id": "2", "item_type": "post"},
    {"item_id": "3", "item_type": "reel"}
  ],
  "counter_types": ["likes", "views"]
}

Response 200:
{
  "counts": {
    "post:1": {"likes": 1524, "views": 89234},
    "post:2": {"likes": 847, "views": 12345},
    "reel:3": {"likes": 15623, "views": 892340}
  },
  "took_ms": 12
}
```

### 11.5 HasUserLiked API

```http
POST /v1/has_liked
Content-Type: application/json
Authorization: Bearer {token}

{
  "item_ids": ["1", "2", "3", "4", "5"],
  "item_type": "post"
}

Response 200:
{
  "liked": {
    "1": true,
    "2": false,
    "3": true,
    "4": false,
    "5": false
  }
}
```

---

## 12. Monitoring & Observability

### 12.1 Key Metrics

| Metric | Target | Alert Threshold |
|--------|--------|-----------------|
| Like P99 latency | < 50ms | > 100ms |
| Read P99 latency | < 20ms | > 50ms |
| Cache hit rate | > 95% | < 90% |
| Dedup rate | ~5% (normal) | > 20% (spam attack) |
| Kafka consumer lag | < 1000 | > 10000 |
| Counter drift | < 0.1% | > 1% |
| Error rate | < 0.01% | > 0.1% |
| Hot item count | < 1000 | > 5000 |

### 12.2 Dashboards

```
┌─────────────────────────────────────────────────────────────────┐
│              Counter System Health Dashboard                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  [Likes/sec]  [Reads/sec]  [Error Rate]  [Kafka Lag]            │
│                                                                  │
│  [P50/P99 Write Latency]  [P50/P99 Read Latency]                │
│                                                                  │
│  [Cache Hit Rate]  [Hot Items]  [Counter Drift %]               │
│                                                                  │
│  [Top 10 Hot Items]  [Dedup Rate]  [Rate Limit Hits]            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 12.3 Alerting Rules

```yaml
alerts:
  - name: HighWriteLatency
    condition: p99_write_latency > 100ms for 2 minutes
    severity: warning
    action: page_on_call

  - name: KafkaConsumerLag
    condition: consumer_lag > 10000 for 5 minutes
    severity: critical
    action: page_on_call, scale_consumers

  - name: CounterDrift
    condition: counter_drift_percent > 1% for 10 minutes
    severity: warning
    action: trigger_reconciliation

  - name: HotItemOverload
    condition: hot_item_count > 5000
    severity: warning
    action: increase_shard_count

  - name: CacheHitRateDrop
    condition: cache_hit_rate < 90% for 5 minutes
    severity: warning
    action: investigate_cache_issues
```

---

## 13. Trade-offs & Design Decisions

| Decision | Chose | Over | Why |
|----------|-------|------|-----|
| Dedup storage | Cassandra | DynamoDB | Better write throughput, tunable consistency |
| Counter storage | Sharded PG | Single DB | Horizontal scale, parallel queries |
| Write path | Async (Kafka) | Sync | Latency < 50ms requirement |
| Consistency | Eventual | Strong | 99.99% availability requirement |
| Hot item handling | Sharded counters | Single row | Handle viral posts without locks |
| Dedup check | Bloom + DB | DB only | Reduce DB load by 90% |
| Read cache | Redis | Memcached | Atomic increments, data structures |
| Counter update | Batched | Real-time | Reduce write amplification 100x |

### What We're NOT Optimizing

1. **Exact real-time counts** - Accepts eventual consistency (seconds of lag)
2. **Historical analytics** - Separate system for "likes over time"
3. **Cross-counter transactions** - No atomic "like + view" updates
4. **Global strong consistency** - Regional consistency only

---

## 14. Multi-Region Architecture

### 14.1 Topology

```
                         ┌─────────────────┐
                         │  Global DNS     │
                         │  (Latency-based)│
                         └─────────────────┘
                                  │
           ┌──────────────────────┼──────────────────────┐
           ▼                      ▼                      ▼
    ┌─────────────┐        ┌─────────────┐        ┌─────────────┐
    │   US-WEST   │        │   EU-WEST   │        │  AP-SOUTH   │
    │   Primary   │        │   Primary   │        │   Primary   │
    └─────────────┘        └─────────────┘        └─────────────┘
           │                      │                      │
           ▼                      ▼                      ▼
    ┌─────────────┐        ┌─────────────┐        ┌─────────────┐
    │    Kafka    │◄──────▶│    Kafka    │◄──────▶│    Kafka    │
    │   Cluster   │ Mirror │   Cluster   │ Mirror │   Cluster   │
    └─────────────┘        └─────────────┘        └─────────────┘
           │                      │                      │
           └──────────────────────┼──────────────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────┐
                    │    Global Counter       │
                    │    Aggregation (Flink)  │
                    └─────────────────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────┐
                    │   Global Counter DB     │
                    │   (Cross-region sync)   │
                    └─────────────────────────┘
```

### 14.2 Regional Data Flow

```
User in EU likes a post:
  1. Request → EU-WEST API
  2. Dedup check → EU Cassandra (LOCAL_QUORUM)
  3. Write to EU Kafka → like-events-eu
  4. Optimistic increment → EU Redis
  5. Return success (< 50ms)

Async:
  6. EU Kafka mirrors to global topic
  7. Global Flink aggregates all regions
  8. Updates global Counter DB
  9. Counter DB syncs to all regional Redis caches
```

### 14.3 Conflict Resolution

```
Scenario: Same user likes same item from two regions simultaneously

Resolution: Last-write-wins with timestamp

1. EU request: like(user=123, item=456, ts=T1)
2. US request: like(user=123, item=456, ts=T2)

If T1 < T2:
  - US write wins
  - Only one like recorded
  - Counter incremented once

Implementation:
  - Cassandra uses timestamp-based LWW
  - Flink deduplicates by (user_id, item_id) in 5-second windows
  - Counter corrections applied if needed
```

---

## 15. Evolution Path

### Phase 1: MVP
- Single region
- PostgreSQL counters
- Redis cache
- Basic deduplication

### Phase 2: Scale
- Kafka event streaming
- Flink aggregation
- Cassandra for dedup
- Sharded counters

### Phase 3: Global
- Multi-region deployment
- Cross-region Kafka mirroring
- Global counter aggregation
- Regional cache warming

### Phase 4: Advanced
- Probabilistic counters for ultra-hot items (HyperLogLog)
- ML-based hot item prediction
- Real-time counter streaming to clients (WebSockets)
- Counter versioning for audit trails

---

## 16. Load-Specific Solutions

This section provides concrete architecture recommendations for different scale tiers. Each tier builds upon the previous, allowing progressive scaling as your system grows.

### 16.1 Tier 1: 10K Likes/Second (Startup Scale)

**Use Case:** Small to medium apps, MVPs, early-stage products

**Architecture: Simple Monolith with PostgreSQL**

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FastAPI Application                           │
│                                                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │  Like API   │  │ Count API   │  │   HasLiked API          │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      PostgreSQL                                  │
│    - user_likes table (dedup)                                   │
│    - counters table                                             │
│    - Unique constraint for idempotency                          │
└─────────────────────────────────────────────────────────────────┘
```

**Key Characteristics:**
- **Storage:** Single PostgreSQL instance (or primary + read replica)
- **Deduplication:** UNIQUE constraint on (user_id, item_id, item_type)
- **Counter Update:** Synchronous increment/decrement with transaction
- **Caching:** None required at this scale
- **Latency:** P99 < 50ms for writes, < 20ms for reads

**Data Model:**
```sql
-- Likes table (source of truth + dedup)
CREATE TABLE user_likes (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    item_id BIGINT NOT NULL,
    item_type SMALLINT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, item_id, item_type)
);
CREATE INDEX idx_user_likes_item ON user_likes(item_id, item_type);

-- Counter table
CREATE TABLE counters (
    item_id BIGINT NOT NULL,
    item_type SMALLINT NOT NULL,
    like_count BIGINT DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (item_id, item_type)
);
```

**Like Operation:**
```python
async def like_item(user_id: int, item_id: int, item_type: int):
    async with db.transaction():
        # Insert like (fails if duplicate due to UNIQUE)
        try:
            await db.execute("""
                INSERT INTO user_likes (user_id, item_id, item_type)
                VALUES ($1, $2, $3)
            """, user_id, item_id, item_type)
        except UniqueViolation:
            return {"status": "already_liked"}

        # Increment counter
        await db.execute("""
            INSERT INTO counters (item_id, item_type, like_count)
            VALUES ($1, $2, 1)
            ON CONFLICT (item_id, item_type)
            DO UPDATE SET like_count = counters.like_count + 1,
                          updated_at = NOW()
        """, item_id, item_type)

    return {"status": "liked"}
```

**Capacity:**
| Metric | Value |
|--------|-------|
| Max likes/sec | 10,000 |
| Items | < 100 million |
| User-likes | < 1 billion |
| PostgreSQL size | < 100 GB |
| Servers | 1-2 |

**When to Upgrade:** Move to Tier 2 when:
- Write latency P99 > 100ms
- Database CPU consistently > 70%
- Read replica lag becomes noticeable

---

### 16.2 Tier 2: 100K Likes/Second (Growth Scale)

**Use Case:** Growing apps, significant user base, frequent viral content

**Architecture: Cached Monolith with Redis**

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FastAPI Application                           │
│                       (Multiple instances)                       │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │  Like API   │  │ Count API   │  │   HasLiked API          │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
          │                   │                      │
          ▼                   ▼                      ▼
┌─────────────────┐  ┌─────────────────┐   ┌─────────────────────┐
│ Write Path:     │  │ Read Path:      │   │ Dedup Check:        │
│ Redis + PG      │  │ Redis Cache     │   │ Redis Set + PG      │
└─────────────────┘  └─────────────────┘   └─────────────────────┘
          │                   │                      │
          └───────────────────┼──────────────────────┘
                              ▼
          ┌─────────────────────────────────────────────┐
          │             Redis Cluster                    │
          │  - Counter cache (STRING)                    │
          │  - Recent likes per user (SET)               │
          │  - Rate limiting                             │
          └─────────────────────────────────────────────┘
                              │
                              ▼
          ┌─────────────────────────────────────────────┐
          │     PostgreSQL (Primary + Read Replicas)     │
          │  - user_likes (sharded by user_id % 16)      │
          │  - counters (source of truth)                │
          └─────────────────────────────────────────────┘
```

**Key Characteristics:**
- **Storage:** PostgreSQL with logical sharding (16 partitions)
- **Deduplication:** Redis SET for recent likes + PostgreSQL UNIQUE constraint
- **Counter Update:** Write-through cache (Redis + PostgreSQL)
- **Caching:** Redis for counters (95%+ hit rate)
- **Latency:** P99 < 30ms for writes, < 5ms for cached reads

**Redis Cache Structure:**
```
# Counter cache
counter:{item_type}:{item_id}  →  INT (like count)
TTL: 1 hour

# Recent user likes (for fast dedup)
recent_likes:{user_id}  →  SET of "item_type:item_id"
TTL: 24 hours, Max 1000 entries

# Rate limiting
rate:{user_id}:likes  →  INT
TTL: 1 minute
```

**Like Operation with Caching:**
```python
async def like_item(user_id: int, item_id: int, item_type: int):
    # 1. Rate limit check
    rate_key = f"rate:{user_id}:likes"
    current = await redis.incr(rate_key)
    if current == 1:
        await redis.expire(rate_key, 60)
    if current > 100:
        raise HTTPException(429, "Rate limited")

    # 2. Quick dedup check (Redis)
    recent_key = f"recent_likes:{user_id}"
    like_member = f"{item_type}:{item_id}"
    if await redis.sismember(recent_key, like_member):
        return {"status": "already_liked"}

    # 3. PostgreSQL write (source of truth)
    async with db.transaction():
        try:
            await db.execute("""
                INSERT INTO user_likes (user_id, item_id, item_type)
                VALUES ($1, $2, $3)
            """, user_id, item_id, item_type)
        except UniqueViolation:
            # Update Redis cache for next time
            await redis.sadd(recent_key, like_member)
            return {"status": "already_liked"}

        await db.execute("""
            INSERT INTO counters (item_id, item_type, like_count)
            VALUES ($1, $2, 1)
            ON CONFLICT (item_id, item_type)
            DO UPDATE SET like_count = counters.like_count + 1
        """, item_id, item_type)

    # 4. Update caches
    await redis.sadd(recent_key, like_member)
    await redis.expire(recent_key, 86400)
    await redis.incr(f"counter:{item_type}:{item_id}")

    return {"status": "liked"}
```

**Capacity:**
| Metric | Value |
|--------|-------|
| Max likes/sec | 100,000 |
| Items | < 1 billion |
| User-likes | < 10 billion |
| PostgreSQL size | < 1 TB |
| Redis memory | 10-20 GB |
| App servers | 4-8 |

**When to Upgrade:** Move to Tier 3 when:
- PostgreSQL write contention causes timeouts
- Hot items create single-row bottlenecks
- Need for async processing becomes critical

---

### 16.3 Tier 3: 1M Likes/Second (Scale-Up)

**Use Case:** Large social platforms, frequent viral events, multiple products

**Architecture: Async Processing with Kafka**

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    API Gateway / Load Balancer                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Like Service (Stateless)                      │
│                       (10+ instances)                            │
│                                                                  │
│  1. Rate limit → 2. Quick dedup → 3. Publish to Kafka           │
│                    → 4. Optimistic cache update                  │
└─────────────────────────────────────────────────────────────────┘
          │                   │                      │
          ▼                   ▼                      ▼
┌─────────────────┐  ┌─────────────────┐   ┌───────────────────────┐
│   Redis         │  │    Kafka        │   │  PostgreSQL           │
│   Cluster       │  │    Cluster      │   │  (Sharded)            │
│                 │  │                 │   │                       │
│ - Counters      │  │ - like-events   │   │ - user_likes (64      │
│ - Recent likes  │  │   topic         │   │   partitions)         │
│ - Rate limits   │  │ - Partitioned   │   │ - counters            │
│                 │  │   by item_id    │   │                       │
└─────────────────┘  └─────────────────┘   └───────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                Counter Aggregation Workers                       │
│                    (Flink / Custom)                              │
│                                                                  │
│  1. Consume micro-batches (1-second windows)                    │
│  2. Deduplicate within window                                   │
│  3. Verify with PostgreSQL                                      │
│  4. Batch update counters                                       │
│  5. Update Redis cache                                          │
└─────────────────────────────────────────────────────────────────┘
```

**Key Characteristics:**
- **Write Path:** Async via Kafka (fire-and-forget from API)
- **Deduplication:** Multi-layer (Redis → PostgreSQL)
- **Counter Update:** Batched aggregation (100x write reduction)
- **Caching:** Redis cluster with TTL-based invalidation
- **Latency:** P99 < 20ms (API), eventual consistency for counts

**Kafka Topic Design:**
```
Topic: like-events
Partitions: 64 (by item_id for ordering)
Replication: 3
Retention: 7 days

Message format:
{
    "event_id": "uuid",
    "user_id": 123,
    "item_id": 456,
    "item_type": 1,
    "action": "like",  // or "unlike"
    "timestamp": "2024-01-15T10:30:00Z"
}
```

**Aggregation Worker Logic:**
```python
class CounterAggregator:
    def __init__(self):
        self.window_seconds = 1
        self.pending = defaultdict(int)  # (item_id, item_type) → delta
        self.seen = set()  # (user_id, item_id, item_type)

    async def process_batch(self, events: List[LikeEvent]):
        # 1. Deduplicate within window
        for event in events:
            key = (event.user_id, event.item_id, event.item_type)
            if key in self.seen:
                continue
            self.seen.add(key)

            # 2. Verify with database (batch query)
            # Skip if already in DB

            counter_key = (event.item_id, event.item_type)
            delta = 1 if event.action == "like" else -1
            self.pending[counter_key] += delta

        # 3. Batch write to database
        if self.pending:
            await self.batch_update_counters(self.pending)
            await self.batch_update_redis(self.pending)

        # 4. Clear window state
        self.pending.clear()
        self.seen.clear()

    async def batch_update_counters(self, deltas):
        # Use PostgreSQL COPY or multi-row UPDATE
        values = [(k[0], k[1], v) for k, v in deltas.items()]
        await db.executemany("""
            UPDATE counters SET like_count = like_count + $3
            WHERE item_id = $1 AND item_type = $2
        """, values)
```

**Capacity:**
| Metric | Value |
|--------|-------|
| Max likes/sec | 1,000,000 |
| Items | < 10 billion |
| User-likes | < 100 billion |
| PostgreSQL clusters | 4-8 shards |
| Kafka brokers | 6-10 |
| Redis memory | 50-100 GB |
| App servers | 20-50 |

**When to Upgrade:** Move to Tier 4 when:
- Viral posts (millions of likes/minute) cause counter contention
- Single counter row becomes bottleneck
- Need sub-second counter accuracy

---

### 16.4 Tier 4: 10M Likes/Second (Internet Scale)

**Use Case:** Top-tier social platforms, global events, celebrity posts

**Architecture: Sharded Counters with Hot Item Detection**

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    CDN + Global Load Balancer                    │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        ┌──────────┐   ┌──────────┐   ┌──────────┐
        │ Region A │   │ Region B │   │ Region C │
        │   API    │   │   API    │   │   API    │
        └──────────┘   └──────────┘   └──────────┘
              │               │               │
              └───────────────┼───────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Hot Item Detection Service                    │
│                                                                  │
│  - Tracks write rate per item (sliding window)                  │
│  - Auto-shards counters when rate > 1000/sec                    │
│  - Auto-merges when rate < 100/sec for 1 hour                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Sharded Counter Layer                         │
│                                                                  │
│  Normal items:                                                  │
│    counter:1:123:1 = 5000                                       │
│                                                                  │
│  Hot items (auto-sharded to 16 shards):                         │
│    counter:1:viral_post:1:shard_0 = 312500                      │
│    counter:1:viral_post:1:shard_1 = 312500                      │
│    ...                                                          │
│    counter:1:viral_post:1:shard_15 = 312500                     │
│                                                                  │
│  Read: MGET all shards → SUM                                    │
│  Write: INCR random shard                                       │
└─────────────────────────────────────────────────────────────────┘
```

**Hot Item Detection Algorithm:**
```python
class HotItemDetector:
    def __init__(self):
        self.window_seconds = 10
        self.shard_threshold = 1000  # writes/sec
        self.merge_threshold = 100   # writes/sec
        self.default_shards = 16

    async def record_write(self, item_id: int, item_type: int):
        key = f"hot:{item_type}:{item_id}"

        # Increment sliding window counter
        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, self.window_seconds)

        # Check if should shard
        rate = current / self.window_seconds
        shard_key = f"counter:{item_type}:{item_id}:shards"

        if rate > self.shard_threshold:
            existing_shards = await redis.get(shard_key)
            if not existing_shards:
                await self.create_shards(item_id, item_type)

    async def create_shards(self, item_id: int, item_type: int):
        base_key = f"counter:{item_type}:{item_id}"

        # Get current value
        current = int(await redis.get(base_key) or 0)

        # Distribute across shards
        per_shard = current // self.default_shards
        remainder = current % self.default_shards

        pipe = redis.pipeline()
        for i in range(self.default_shards):
            value = per_shard + (1 if i < remainder else 0)
            pipe.set(f"{base_key}:shard_{i}", value)

        pipe.set(f"{base_key}:shards", self.default_shards)
        pipe.delete(base_key)
        await pipe.execute()


class ShardedCounter:
    async def increment(self, item_id: int, item_type: int):
        base_key = f"counter:{item_type}:{item_id}"
        shard_count = await redis.get(f"{base_key}:shards")

        if shard_count:
            # Hot item: random shard
            shard = random.randint(0, int(shard_count) - 1)
            return await redis.incr(f"{base_key}:shard_{shard}")
        else:
            # Normal item: single counter
            return await redis.incr(base_key)

    async def get(self, item_id: int, item_type: int):
        base_key = f"counter:{item_type}:{item_id}"
        shard_count = await redis.get(f"{base_key}:shards")

        if shard_count:
            # Sum all shards (parallel MGET)
            keys = [f"{base_key}:shard_{i}" for i in range(int(shard_count))]
            values = await redis.mget(keys)
            return sum(int(v or 0) for v in values)
        else:
            return int(await redis.get(base_key) or 0)
```

**Deduplication with Cassandra:**
```sql
-- Cassandra schema for dedup at scale
CREATE KEYSPACE likes WITH replication = {
    'class': 'NetworkTopologyStrategy',
    'us-east': 3,
    'eu-west': 3,
    'ap-south': 3
};

CREATE TABLE likes.user_likes (
    user_id BIGINT,
    item_type TINYINT,
    item_id BIGINT,
    liked_at TIMESTAMP,
    PRIMARY KEY ((user_id), item_type, item_id)
) WITH CLUSTERING ORDER BY (item_type ASC, item_id DESC)
  AND compaction = {'class': 'LeveledCompactionStrategy'}
  AND gc_grace_seconds = 864000;
```

**Capacity:**
| Metric | Value |
|--------|-------|
| Max likes/sec | 10,000,000 |
| Items | < 100 billion |
| User-likes | < 1 trillion |
| Cassandra nodes | 50-100 |
| Redis cluster nodes | 30-50 |
| Kafka partitions | 256+ |
| App servers | 100+ |

---

### 16.5 Tier 5: 100M Likes/Second (Planetary Scale)

**Use Case:** Instagram/Facebook scale, global events, multi-region deployment

**Architecture: Full Distributed with Regional Isolation**

```
                         ┌─────────────────┐
                         │  Global DNS     │
                         │  (Latency-based)│
                         └─────────────────┘
                                  │
           ┌──────────────────────┼──────────────────────┐
           ▼                      ▼                      ▼
┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐
│      US-WEST        │ │      EU-WEST        │ │      AP-SOUTH       │
│                     │ │                     │ │                     │
│  ┌───────────────┐  │ │  ┌───────────────┐  │ │  ┌───────────────┐  │
│  │   API Fleet   │  │ │  │   API Fleet   │  │ │  │   API Fleet   │  │
│  │   (50 pods)   │  │ │  │   (50 pods)   │  │ │  │   (50 pods)   │  │
│  └───────────────┘  │ │  └───────────────┘  │ │  └───────────────┘  │
│         │           │ │         │           │ │         │           │
│         ▼           │ │         ▼           │ │         ▼           │
│  ┌───────────────┐  │ │  ┌───────────────┐  │ │  ┌───────────────┐  │
│  │ Redis Cluster │  │ │  │ Redis Cluster │  │ │  │ Redis Cluster │  │
│  │   (20 nodes)  │  │ │  │   (20 nodes)  │  │ │  │   (20 nodes)  │  │
│  └───────────────┘  │ │  └───────────────┘  │ │  └───────────────┘  │
│         │           │ │         │           │ │         │           │
│         ▼           │ │         ▼           │ │         ▼           │
│  ┌───────────────┐  │ │  ┌───────────────┐  │ │  ┌───────────────┐  │
│  │ Kafka Cluster │◄─┼─┼─►│ Kafka Cluster │◄─┼─┼─►│ Kafka Cluster │  │
│  │  (MirrorMaker)│  │ │  │  (MirrorMaker)│  │ │  │  (MirrorMaker)│  │
│  └───────────────┘  │ │  └───────────────┘  │ │  └───────────────┘  │
│         │           │ │         │           │ │         │           │
│         ▼           │ │         ▼           │ │         ▼           │
│  ┌───────────────┐  │ │  ┌───────────────┐  │ │  ┌───────────────┐  │
│  │  Cassandra    │◄─┼─┼─►│  Cassandra    │◄─┼─┼─►│  Cassandra    │  │
│  │  (30 nodes)   │  │ │  │  (30 nodes)   │  │ │  │  (30 nodes)   │  │
│  └───────────────┘  │ │  └───────────────┘  │ │  └───────────────┘  │
│                     │ │                     │ │                     │
└─────────────────────┘ └─────────────────────┘ └─────────────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────┐
                    │  Global Flink Cluster   │
                    │  (Counter Aggregation)  │
                    │                         │
                    │  - Consumes all regions │
                    │  - Deduplicates globally│
                    │  - Updates all caches   │
                    └─────────────────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────┐
                    │   ScyllaDB / CockroachDB│
                    │   (Global Counter Store)│
                    │                         │
                    │   - 256 shards          │
                    │   - Multi-region sync   │
                    │   - Source of truth     │
                    └─────────────────────────┘
```

**Key Features:**

1. **Regional Isolation:** Each region handles local traffic independently
2. **Async Replication:** Kafka MirrorMaker syncs events across regions
3. **Global Deduplication:** Flink cluster processes all events globally
4. **Conflict Resolution:** Last-write-wins with vector clocks

**HyperLogLog for Ultra-Hot Items:**
```python
class HyperLogLogCounter:
    """
    For items with 100M+ interactions where exact count isn't needed.
    Error rate: ~0.81% (standard error)
    Memory: 12KB per counter vs potentially GB for exact dedup
    """

    async def add_user(self, item_id: int, item_type: int, user_id: int):
        key = f"hll:{item_type}:{item_id}"
        await redis.pfadd(key, user_id)

    async def get_count(self, item_id: int, item_type: int):
        key = f"hll:{item_type}:{item_id}"
        return await redis.pfcount(key)

    async def merge_regions(self, item_id: int, item_type: int, regions: List[str]):
        """Merge HLL counters from multiple regions"""
        keys = [f"hll:{region}:{item_type}:{item_id}" for region in regions]
        dest = f"hll:global:{item_type}:{item_id}"
        await redis.pfmerge(dest, *keys)
        return await redis.pfcount(dest)
```

**Global Event Processing:**
```python
class GlobalCounterAggregator:
    def __init__(self):
        self.regions = ["us-west", "eu-west", "ap-south"]
        self.dedup_window_seconds = 5

    async def process_global_events(self):
        """
        Consumes from all regional Kafka clusters,
        deduplicates globally, updates counters.
        """
        # 1. Consume from all regions
        events = []
        for region in self.regions:
            regional_events = await self.consume_region(region)
            events.extend(regional_events)

        # 2. Sort by timestamp
        events.sort(key=lambda e: e.timestamp)

        # 3. Deduplicate (last-write-wins)
        seen = {}  # (user_id, item_id, item_type) → event
        for event in events:
            key = (event.user_id, event.item_id, event.item_type)
            if key not in seen or event.timestamp > seen[key].timestamp:
                seen[key] = event

        # 4. Calculate deltas
        deltas = defaultdict(int)
        for event in seen.values():
            if await self.verify_with_cassandra(event):
                counter_key = (event.item_id, event.item_type)
                delta = 1 if event.action == "like" else -1
                deltas[counter_key] += delta

        # 5. Update global counter store
        await self.batch_update_scylla(deltas)

        # 6. Update all regional Redis caches
        for region in self.regions:
            await self.update_regional_cache(region, deltas)
```

**Capacity:**
| Metric | Value |
|--------|-------|
| Max likes/sec | 100,000,000 |
| Items | 100+ billion |
| User-likes | 10+ trillion |
| Regions | 3-5 |
| Cassandra nodes | 100+ per region |
| Redis cluster nodes | 60+ per region |
| Kafka brokers | 30+ per region |
| Flink workers | 100+ |
| Total servers | 500+ |

---

### 16.6 Load Tier Comparison

| Aspect | 10K | 100K | 1M | 10M | 100M |
|--------|-----|------|-----|-----|------|
| **Architecture** | Monolith | Cached Monolith | Async + Kafka | Sharded | Multi-Region |
| **Database** | PostgreSQL | PostgreSQL | PostgreSQL Sharded | Cassandra | ScyllaDB Global |
| **Cache** | None | Redis | Redis Cluster | Redis Cluster | Regional Redis |
| **Queue** | None | None | Kafka | Kafka | Kafka + MirrorMaker |
| **Dedup** | UNIQUE constraint | Redis + PG | Redis + PG | Bloom + Cassandra | Global Flink |
| **Hot Items** | N/A | N/A | Batching | Auto-shard | HLL + Shard |
| **Consistency** | Strong | Strong | Eventual | Eventual | Eventual |
| **Write P99** | 50ms | 30ms | 20ms | 15ms | 10ms |
| **Read P99** | 20ms | 5ms | 3ms | 2ms | 1ms |
| **Servers** | 1-2 | 4-8 | 20-50 | 100+ | 500+ |
| **Monthly Cost** | $500 | $5K | $50K | $500K | $5M+ |

---

### 16.7 Migration Strategies

**10K → 100K:**
1. Add Redis layer in front of PostgreSQL
2. Implement write-through caching
3. Add application load balancing
4. Duration: 1-2 weeks

**100K → 1M:**
1. Deploy Kafka cluster
2. Convert sync writes to async
3. Build aggregation workers
4. Shard PostgreSQL tables
5. Duration: 4-6 weeks

**1M → 10M:**
1. Migrate dedup to Cassandra
2. Implement hot item detection
3. Add automatic counter sharding
4. Deploy Flink for aggregation
5. Duration: 2-3 months

**10M → 100M:**
1. Deploy to multiple regions
2. Set up Kafka MirrorMaker
3. Implement global deduplication
4. Add HyperLogLog for ultra-hot items
5. Migrate to ScyllaDB
6. Duration: 6-12 months
