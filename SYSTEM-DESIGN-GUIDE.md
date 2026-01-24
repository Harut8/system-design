# System Design Guide

A practical, senior-engineer-level guide for system design interviews and real-world architecture.

---

## Table of Contents

1. [The Framework](#the-framework)
2. [Step-by-Step Process](#step-by-step-process)
3. [Questions to Ask](#questions-to-ask)
4. [Back-of-Envelope Calculations](#back-of-envelope-calculations)
5. [Core Building Blocks](#core-building-blocks)
6. [Database Selection](#database-selection)
7. [Scaling Patterns](#scaling-patterns)
8. [Hard Parts & How to Handle Them](#hard-parts--how-to-handle-them)
9. [Common Mistakes to Avoid](#common-mistakes-to-avoid)
10. [System Design Templates](#system-design-templates)

---

## The Framework

Use this structure for every system design problem:

```text
1. Requirements    (5 min)  → What are we building?
2. Estimations     (5 min)  → How big is it?
3. API Design      (5 min)  → What's the interface?
4. High-Level      (10 min) → Draw the boxes
5. Deep Dive       (15 min) → Solve the hard parts
6. Trade-offs      (5 min)  → Justify decisions
```

---

## Step-by-Step Process

### Step 1: Gather Requirements

**Functional Requirements** — What the system does:
- Core features (must-have)
- Secondary features (nice-to-have)
- Out of scope (explicitly exclude)

**Non-Functional Requirements** — How the system behaves:
- Scale (users, requests, data)
- Latency (p50, p99 targets)
- Availability (99.9%? 99.99%?)
- Consistency (strong vs eventual)
- Durability (can we lose data?)

### Step 2: Capacity Estimation

Calculate these numbers:
- **QPS** (queries per second)
- **Storage** (total data size)
- **Bandwidth** (data transfer)
- **Memory** (cache requirements)

### Step 3: Define API

Write actual endpoints:

```text
POST   /api/v1/resource
GET    /api/v1/resource/{id}
PUT    /api/v1/resource/{id}
DELETE /api/v1/resource/{id}
GET    /api/v1/resource?filter=value&cursor=abc
```

Include:
- Request/response payloads
- Authentication method
- Rate limiting headers
- Pagination strategy

### Step 4: High-Level Design

Draw the architecture:

```text
┌──────────┐     ┌──────────────┐     ┌─────────────┐
│  Client  │────▶│ Load Balancer│────▶│ API Servers │
└──────────┘     └──────────────┘     └─────────────┘
                                             │
                      ┌──────────────────────┼──────────────────────┐
                      ▼                      ▼                      ▼
               ┌───────────┐          ┌───────────┐          ┌───────────┐
               │   Cache   │          │  Database │          │   Queue   │
               │  (Redis)  │          │ (Postgres)│          │  (Kafka)  │
               └───────────┘          └───────────┘          └───────────┘
```

### Step 5: Deep Dive

Pick the hardest 2-3 problems and solve them:
- Data model and schema
- Sharding strategy
- Caching approach
- Failure handling

### Step 6: Discuss Trade-offs

For every major decision, explain:
- What alternatives exist
- Why you chose this approach
- What you're giving up
- When you'd reconsider

---

## Questions to Ask

### Functional Questions

```text
□ What is the primary use case?
□ Who are the users? (consumers, businesses, internal)
□ What actions can users perform?
□ What data do we need to store?
□ What data do we need to display?
□ Do we need real-time updates?
□ Do we need search/filtering?
□ Do we need analytics/reporting?
□ What's the read:write ratio?
□ Is there user-generated content?
```

### Scale Questions

```text
□ How many users? (total, DAU, MAU)
□ How many requests per second?
□ How much data per user?
□ How long do we retain data?
□ What are peak traffic patterns?
□ Is traffic geographically distributed?
□ Expected growth rate?
```

### Consistency & Availability Questions

```text
□ Can we show stale data? For how long?
□ What happens during a failure?
□ Is eventual consistency acceptable?
□ Do we need transactions?
□ What's the SLA requirement?
□ What's the acceptable downtime?
```

### Constraints Questions

```text
□ Existing tech stack?
□ Budget constraints?
□ Team expertise?
□ Regulatory requirements? (GDPR, HIPAA)
□ Multi-region requirements?
□ On-prem vs cloud?
```

---

## Back-of-Envelope Calculations

### Key Numbers to Memorize

```text
1 day      = 86,400 seconds   ≈ 100K seconds
1 month    = 2.5 million seconds
1 year     = 30 million seconds

1 KB       = 1,000 bytes
1 MB       = 1,000 KB
1 GB       = 1,000 MB
1 TB       = 1,000 GB

1 char     = 1 byte (ASCII) or 2-4 bytes (UTF-8)
1 integer  = 4-8 bytes
1 UUID     = 16 bytes (binary) or 36 bytes (string)
1 timestamp = 8 bytes
```

### Latency Numbers

```text
L1 cache reference                    0.5 ns
L2 cache reference                      7 ns
Main memory reference                 100 ns
SSD random read                    16,000 ns  (16 μs)
HDD seek                       10,000,000 ns  (10 ms)
Same datacenter round trip       500,000 ns  (0.5 ms)
Cross-continent round trip   150,000,000 ns  (150 ms)
```

### Common Calculations

**QPS (Queries Per Second):**

```text
QPS = DAU × actions_per_user / 86,400

Example:
- 10M DAU
- 20 actions/day per user
- QPS = 10M × 20 / 86,400 ≈ 2,300 QPS
- Peak QPS = 2-3× average ≈ 5,000-7,000 QPS
```

**Storage:**

```text
Storage = records × size_per_record × retention_period

Example:
- 1M new records/day
- 1 KB per record
- 5 years retention
- Storage = 1M × 1KB × 365 × 5 = 1.8 TB
```

**Bandwidth:**

```text
Bandwidth = QPS × payload_size

Example:
- 5,000 QPS
- 10 KB average response
- Bandwidth = 5,000 × 10 KB = 50 MB/s = 400 Mbps
```

**Cache Size:**

```text
Cache = hot_data × size_per_record

Rule: Cache 20% of daily traffic (80/20 rule)

Example:
- 10M daily reads
- 2 KB per record
- Cache 20% = 2M × 2KB = 4 GB
```

---

## Core Building Blocks

### Load Balancer

**When to use:** Always, for any multi-server setup

**Options:**
| Type | Example | Use Case |
| ---- | ------- | -------- |
| L4 (TCP) | HAProxy, NLB | High throughput, simple routing |
| L7 (HTTP) | Nginx, ALB | Path-based routing, SSL termination |
| Global | Cloudflare, Route53 | Geographic distribution |

**Algorithms:**
- Round Robin — equal distribution
- Least Connections — route to least busy
- IP Hash — sticky sessions
- Weighted — different server capacities

### Cache

**When to use:** Read-heavy workloads, expensive computations

**Cache Patterns:**

```text
Cache-Aside (Lazy Loading):
1. Check cache
2. If miss, read from DB
3. Store in cache
4. Return data

Write-Through:
1. Write to cache
2. Cache writes to DB
3. Return success

Write-Behind (Write-Back):
1. Write to cache
2. Return success immediately
3. Cache async writes to DB
```

**Cache Invalidation:**
- TTL (Time To Live) — simple but stale data
- Event-based — complex but accurate
- Version-based — append version to key

**Eviction Policies:**
- LRU (Least Recently Used) — most common
- LFU (Least Frequently Used) — for hot data
- FIFO — simple but not optimal

### Message Queue

**When to use:** Async processing, decoupling services, load leveling

**Types:**
| Type | Example | Guarantee |
| ---- | ------- | --------- |
| At-most-once | Basic queue | Fast, may lose messages |
| At-least-once | SQS, Kafka | Safe, may duplicate |
| Exactly-once | Kafka transactions | Slowest, most complex |

**Patterns:**
- Point-to-Point — one consumer per message
- Pub/Sub — multiple consumers per message
- Fan-out — broadcast to all consumers
- Fan-in — aggregate from multiple producers

### CDN (Content Delivery Network)

**When to use:** Static assets, global users, reduce latency

**Types:**
- Pull CDN — CDN fetches from origin on miss
- Push CDN — You upload to CDN directly

**What to cache:**
- Images, videos, static files
- CSS, JavaScript bundles
- API responses (carefully, with headers)

### Database

See [Database Selection](#database-selection) section.

---

## Database Selection

### Decision Tree

```text
                    What's your data?
                          │
           ┌──────────────┼──────────────┐
           ▼              ▼              ▼
      Structured    Semi-structured   Unstructured
           │              │              │
           ▼              ▼              ▼
    Need ACID?      Key-Value?      Blob Storage
       │  │              │              │
      Yes No            Yes            S3/GCS
       │  │              │
       ▼  ▼              ▼
   SQL  NoSQL      Redis/DynamoDB
```

### Database Types

| Type | Examples | Best For | Avoid When |
| ---- | -------- | -------- | ---------- |
| Relational | PostgreSQL, MySQL | ACID, complex queries, joins | Massive scale, unstructured |
| Document | MongoDB, Firestore | Flexible schema, nested data | Complex transactions |
| Key-Value | Redis, DynamoDB | Simple lookups, caching | Complex queries |
| Wide-Column | Cassandra, HBase | Time-series, write-heavy | Ad-hoc queries |
| Graph | Neo4j, Neptune | Relationships, traversals | Simple data |
| Search | Elasticsearch | Full-text search, analytics | Primary storage |
| Time-Series | InfluxDB, TimescaleDB | Metrics, IoT, logs | General purpose |

### SQL vs NoSQL

**Choose SQL when:**
- Data is structured and relational
- Need ACID transactions
- Complex queries with joins
- Data integrity is critical

**Choose NoSQL when:**
- Schema changes frequently
- Massive scale needed
- Simple access patterns
- High write throughput

### Replication Strategies

```text
Single-Leader:
┌────────┐    writes    ┌────────┐    replicates    ┌────────┐
│ Client │─────────────▶│ Leader │─────────────────▶│Follower│
└────────┘              └────────┘                  └────────┘
                             │                          │
                        reads/writes                reads only

Multi-Leader:
┌────────┐              ┌────────┐
│Leader A│◀────────────▶│Leader B│  (conflict resolution needed)
└────────┘              └────────┘

Leaderless:
┌────────┐    ┌────────┐    ┌────────┐
│ Node A │◀──▶│ Node B │◀──▶│ Node C │  (quorum reads/writes)
└────────┘    └────────┘    └────────┘
```

### Sharding Strategies

**Range-Based:**

```text
User IDs 1-1M      → Shard 1
User IDs 1M-2M     → Shard 2
User IDs 2M-3M     → Shard 3
```

Pros: Range queries efficient
Cons: Hotspots if data skewed

**Hash-Based:**

```text
Shard = hash(user_id) % num_shards
```

Pros: Even distribution
Cons: Range queries require scatter-gather

**Directory-Based:**

```text
Lookup service maps key → shard
```

Pros: Flexible
Cons: Single point of failure, extra hop

**Consistent Hashing:**

```text
       Node A
         │
    ┌────●────┐
   ╱           ╲
  ●             ●  Node B
 Node D         │
  ╲           ╱
    └────●────┘
       Node C
```

Pros: Minimal remapping on node changes
Cons: More complex implementation

---

## Scaling Patterns

### Vertical vs Horizontal

```text
Vertical Scaling (Scale Up):
┌─────────┐         ┌─────────────┐
│ Small   │   →     │   Large     │
│ Server  │         │   Server    │
└─────────┘         └─────────────┘
Pros: Simple, no code changes
Cons: Hardware limits, expensive, single point of failure

Horizontal Scaling (Scale Out):
┌─────────┐         ┌─────────┐ ┌─────────┐ ┌─────────┐
│ Server  │   →     │ Server  │ │ Server  │ │ Server  │
└─────────┘         └─────────┘ └─────────┘ └─────────┘
Pros: No limit, fault tolerant, cheaper
Cons: Complex, distributed systems problems
```

### Stateless Services

**Rule:** Keep application servers stateless

```text
Bad:
┌─────────┐  session stored  ┌─────────┐
│ Client  │─────────────────▶│ Server  │  (sticky sessions required)
└─────────┘                  └─────────┘

Good:
┌─────────┐                  ┌─────────┐     ┌─────────┐
│ Client  │─────────────────▶│ Server  │────▶│  Redis  │
└─────────┘  token/session   └─────────┘     │(session)│
             in request                      └─────────┘
```

### Database Scaling Path

```text
1. Single Server
   └── Add read replicas

2. Read Replicas
   └── Add caching layer

3. Cache Layer
   └── Vertical scaling

4. Bigger Server
   └── Functional partitioning (separate DBs per feature)

5. Multiple DBs
   └── Horizontal sharding

6. Sharded Database
   └── Consider NewSQL (CockroachDB, Spanner)
```

### Rate Limiting

**Algorithms:**

```text
Token Bucket:
- Bucket holds N tokens
- Each request takes 1 token
- Tokens refill at rate R
- Allows bursts up to bucket size

Sliding Window:
- Track requests in time window
- Count = requests in [now - window, now]
- Reject if count > limit
- More accurate, more memory

Fixed Window:
- Reset counter each interval
- Simple but edge case: 2× burst at window boundary
```

**Implementation:**

```text
Redis-based rate limiter:

MULTI
  INCR user:{user_id}:requests
  EXPIRE user:{user_id}:requests 60
EXEC

if requests > limit:
    return 429 Too Many Requests
```

---

## Hard Parts & How to Handle Them

### 1. Data Consistency

**Problem:** Keeping data synchronized across multiple locations

**Solutions:**

| Strategy | Consistency | Availability | Use Case |
| -------- | ----------- | ------------ | -------- |
| Strong (sync) | Guaranteed | Lower | Financial, inventory |
| Eventual (async) | Delayed | Higher | Social feeds, analytics |
| Causal | Ordered | Medium | Comments, messaging |

**Patterns:**
- Two-phase commit (2PC) — strong but slow
- Saga pattern — eventual with compensation
- Event sourcing — audit trail, replay
- CQRS — separate read/write models

### 2. Distributed Transactions

**Problem:** ACID across multiple services/databases

**Saga Pattern:**

```text
Order Service         Payment Service       Inventory Service
     │                      │                      │
     │──── Create Order ───▶│                      │
     │                      │──── Reserve ────────▶│
     │                      │                      │
     │◀─── Success ─────────│◀─── Success ─────────│
     │                      │                      │
     │      (if failure, compensating transactions)
     │◀─── Refund ──────────│◀─── Release ─────────│
```

**Outbox Pattern:**

```text
1. Write to business table AND outbox table in same transaction
2. Background worker reads outbox, publishes to queue
3. Delete from outbox after confirmed publish

┌─────────────────────────────────────┐
│           Database                  │
│  ┌───────────┐  ┌───────────────┐   │
│  │  Orders   │  │    Outbox     │   │
│  │  (data)   │  │  (events)     │   │  ──▶ Message Queue
│  └───────────┘  └───────────────┘   │
└─────────────────────────────────────┘
        Single Transaction
```

### 3. Idempotency

**Problem:** Duplicate requests causing duplicate effects

**Solutions:**

```text
Idempotency Key:
POST /api/payments
Headers:
  Idempotency-Key: uuid-12345

Server:
1. Check if key exists in Redis/DB
2. If exists, return cached response
3. If not, process and store result with key
4. Return response
```

**Natural Idempotency:**
- PUT (update entire resource) — naturally idempotent
- DELETE — naturally idempotent
- GET — naturally idempotent (no side effects)
- POST — NOT idempotent, needs explicit handling

### 4. Distributed Locking

**Problem:** Preventing concurrent modifications

**Redis Lock (Redlock):**

```text
SET lock:resource value NX EX 30

NX = only if not exists
EX = expire in 30 seconds

Release:
if GET lock:resource == my_value:
    DEL lock:resource
```

**Considerations:**
- Always set expiration (prevent deadlock)
- Use unique value per client (prevent wrong release)
- Consider lock extension for long operations
- Handle clock skew in distributed systems

### 5. Hot Partitions / Hotspots

**Problem:** One shard receiving disproportionate traffic

**Solutions:**

```text
1. Add randomness to key:
   key = celebrity_id + random(0, N)
   Read: scatter-gather across N keys

2. Separate hot data:
   - Dedicated cache for hot items
   - Separate service for viral content

3. Time-based sharding:
   key = user_id + hour
   Spreads writes, complicates reads

4. Pre-computed aggregates:
   - Don't count on read
   - Increment counter on write
```

### 6. Failure Handling

**Retry Strategy:**

```text
Exponential Backoff with Jitter:

attempt = 1
while attempt <= max_retries:
    try:
        result = make_request()
        return result
    except RetriableError:
        delay = min(base_delay * (2 ** attempt), max_delay)
        delay = delay + random(0, delay * 0.1)  # jitter
        sleep(delay)
        attempt += 1
```

**Circuit Breaker:**

```text
States: CLOSED → OPEN → HALF-OPEN

CLOSED: Normal operation
  - Track failure rate
  - If failures > threshold → OPEN

OPEN: Fail fast
  - Return error immediately
  - After timeout → HALF-OPEN

HALF-OPEN: Testing recovery
  - Allow limited requests
  - If success → CLOSED
  - If failure → OPEN
```

**Bulkhead Pattern:**

```text
Isolate failures by resource pools:

┌─────────────────────────────────────┐
│           Service                   │
│  ┌───────────┐  ┌───────────────┐   │
│  │ DB Pool   │  │  API Pool     │   │
│  │ (10 conn) │  │  (20 conn)    │   │
│  └───────────┘  └───────────────┘   │
└─────────────────────────────────────┘
If DB pool exhausted, API calls still work
```

### 7. Ordering & Causality

**Problem:** Events arriving out of order

**Solutions:**

```text
1. Sequence numbers:
   - Attach monotonic ID to each event
   - Receiver reorders based on ID

2. Vector clocks:
   - Track causality across nodes
   - Detect concurrent events

3. Single writer:
   - Route all writes for entity to same partition
   - Kafka: partition by entity ID

4. Timestamps (careful!):
   - Only works with synchronized clocks
   - NTP provides ~10ms accuracy
   - Use hybrid logical clocks for better accuracy
```

### 8. Data Migration

**Problem:** Changing schema/database at scale

**Dual Write Pattern:**

```text
Phase 1: Dual write
  - Write to old AND new system
  - Read from old

Phase 2: Backfill
  - Migrate historical data
  - Verify consistency

Phase 3: Switch reads
  - Read from new
  - Continue dual write

Phase 4: Cleanup
  - Stop writing to old
  - Decommission old system
```

---

## Common Mistakes to Avoid

### In Interviews

| Mistake | Better Approach |
| ------- | --------------- |
| Jumping into design | Spend 5 min on requirements |
| Not doing math | Always estimate scale |
| Single point of failure | Add redundancy everywhere |
| Ignoring failures | Discuss what happens when X fails |
| Over-engineering | Start simple, scale when needed |
| Not discussing trade-offs | Every choice has pros/cons |
| Forgetting about costs | Mention cost implications |
| No security consideration | Authentication, encryption, validation |

### In Real Systems

| Mistake | Consequence |
| ------- | ----------- |
| No connection pooling | Database overwhelmed |
| Unbounded queues | Memory exhaustion |
| No timeouts | Thread/connection starvation |
| Chatty services | Latency death by 1000 cuts |
| Large payloads | Network bottleneck |
| No monitoring | Blind to problems |
| No rate limiting | DDoS vulnerability |
| Hardcoded configs | Deployment nightmares |

---

## System Design Templates

### Read-Heavy System (10:1 read:write)

```text
┌────────┐     ┌─────┐     ┌──────────┐     ┌───────┐
│ Client │────▶│ CDN │────▶│   Cache  │────▶│  DB   │
└────────┘     └─────┘     │  (Redis) │     │(Read  │
                          └──────────┘     │Replica│
                                           └───────┘
Strategy:
- Aggressive caching
- Read replicas
- CDN for static content
- Cache-aside pattern
```

### Write-Heavy System (1:10 read:write)

```text
┌────────┐     ┌───────────┐     ┌───────┐     ┌─────────┐
│ Client │────▶│ API Server│────▶│ Queue │────▶│ Workers │
└────────┘     └───────────┘     └───────┘     └────┬────┘
                                                    │
                                               ┌────▼────┐
                                               │   DB    │
                                               │(Sharded)│
                                               └─────────┘
Strategy:
- Async processing
- Batch writes
- Sharding
- Append-only logs
```

### Real-Time System

```text
┌────────┐     ┌───────────┐     ┌────────────┐
│ Client │◀───▶│ WebSocket │────▶│ Pub/Sub    │
└────────┘     │  Server   │     │ (Redis)    │
               └───────────┘     └────────────┘
                    │
               ┌────▼────┐
               │   DB    │
               └─────────┘
Strategy:
- WebSocket for bidirectional
- Pub/Sub for broadcasting
- Connection state management
- Heartbeat/reconnection logic
```

### Analytics/Metrics System

```text
┌────────┐     ┌───────────┐     ┌───────┐     ┌──────────┐
│ Client │────▶│ Collector │────▶│ Kafka │────▶│ Spark/   │
└────────┘     └───────────┘     └───────┘     │ Flink    │
                                               └────┬─────┘
                                                    │
                    ┌───────────────────────────────┤
                    ▼                               ▼
              ┌──────────┐                  ┌────────────┐
              │ Data Lake│                  │ Time-Series│
              │  (S3)    │                  │    DB      │
              └──────────┘                  └────────────┘
Strategy:
- Fire-and-forget collection
- Stream processing
- Columnar storage for analytics
- Time-series DB for metrics
```

---

## Quick Reference Checklist

Before finalizing any design, verify:

```text
□ Requirements clarified
□ Scale estimated (QPS, storage, bandwidth)
□ API defined
□ Data model designed
□ Database selected with justification
□ Caching strategy defined
□ Load balancing addressed
□ Single points of failure eliminated
□ Failure scenarios discussed
□ Monitoring/alerting mentioned
□ Security considered
□ Trade-offs explained
□ Future scaling path identified
```

---

## Practice Problems (Ordered by Difficulty)

### Beginner
1. URL Shortener
2. Paste Bin
3. Rate Limiter

### Intermediate
4. Twitter/News Feed
5. Instagram/Photo Sharing
6. Chat System (WhatsApp)
7. Notification System
8. Search Autocomplete

### Advanced
9. YouTube/Video Streaming
10. Uber/Ride Sharing
11. Google Docs (Collaborative Editing)
12. Distributed Task Scheduler
13. Payment System
14. Stock Exchange

---

*Remember: There's no perfect design. The goal is to show structured thinking, understand trade-offs, and make reasonable decisions based on requirements.*