# Instagram-Scale Feed System: Design Document

## 1. Requirements Clarification

### Assumptions & Constraints

| Category | Assumption | Rationale |
|----------|------------|-----------|
| **Scale** | 500M DAU, 2B total users | Instagram-scale |
| **Posts/day** | 100M new posts/day | ~1.2K TPS average, 5K TPS peak |
| **Feed reads** | 5B feed reads/day | ~58K QPS average, 200K QPS peak |
| **Followers** | Avg 200 followers, median 50 | Power law distribution |
| **Celebrities** | 0.1% users have >1M followers | ~2M celebrity accounts |
| **Post size** | 1KB metadata + media URLs | Actual media stored separately |
| **Feed depth** | Show last 7 days of posts | Balance freshness vs storage |
| **Consistency** | Eventual for feed, strong for actions | Users tolerate slight feed delay |

### Clarifying Questions Answered

| Question | Answer |
|----------|--------|
| How many posts in a single feed request? | 20 posts per page (infinite scroll) |
| How fresh must the feed be? | New posts visible within 30 seconds (99th percentile) |
| Do we show posts from non-followed accounts? | Yes, 10-20% of feed is recommendations/ads |
| What's the ranking priority? | Engagement > Affinity > Recency |
| Should we support stories? | Out of scope (separate system) |
| Multi-device sync? | Yes, feed position should sync |

---

## 2. Architecture by Scale (10K → 100M Users)

> **Start simple, scale when needed.** The sections below show how the feed system evolves as you grow. Don't over-engineer early—each tier builds on the previous one.

### Architecture Comparison Matrix

| Scale | DAU | Posts/day | Feed QPS | Architecture | Team | Monthly Cost |
|-------|-----|-----------|----------|--------------|------|--------------|
| **10K users** | 2K | 500 | 5 | Monolith + PostgreSQL | 1-2 | $100-300 |
| **100K users** | 20K | 5K | 50 | Monolith + Redis cache | 2-3 | $500-1K |
| **1M users** | 200K | 50K | 500 | Monolith + Queue + CDN | 3-5 | $3-5K |
| **10M users** | 2M | 500K | 5K | Microservices + Kafka | 8-15 | $30-50K |
| **100M users** | 20M | 5M | 50K | Distributed + Multi-region | 30-50 | $300-500K |
| **500M+ users** | 200M+ | 50M+ | 200K+ | Full Instagram architecture | 100+ | $2M+ |

---

### 2.1 Tier 1: 10K Users (MVP / Early Startup)

**Scale Profile:**
```
Total users:       10,000
Daily active:      2,000 (20%)
Posts/day:         500
Feed reads/day:    10,000
Feed reads/sec:    ~0.1 QPS (peak ~5 QPS)
Storage/month:     ~500 MB posts + 50 GB media
```

**Architecture: Simple Monolith**

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                  │
│                    (Mobile App / Web)                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Single Server                                │
│              (DigitalOcean / AWS EC2 / Railway)                  │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                 Monolith Application                         ││
│  │                (Node.js / Django / Rails)                    ││
│  │                                                              ││
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐     ││
│  │  │Feed API  │  │Post API  │  │User API  │  │Media API │     ││
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘     ││
│  └─────────────────────────────────────────────────────────────┘│
│                              │                                   │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                     PostgreSQL                               ││
│  │           (users, posts, follows, likes)                     ││
│  └─────────────────────────────────────────────────────────────┘│
│                              │                                   │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                   Local File Storage                         ││
│  │                 (or S3 for media)                            ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

**Database Schema:**

```sql
-- Simple PostgreSQL schema
CREATE TABLE users (
    id              SERIAL PRIMARY KEY,
    username        VARCHAR(30) UNIQUE NOT NULL,
    email           VARCHAR(255) UNIQUE NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    avatar_url      TEXT,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE posts (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id),
    caption         TEXT,
    media_url       TEXT NOT NULL,
    media_type      VARCHAR(10) DEFAULT 'image',
    created_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_posts_user_created ON posts(user_id, created_at DESC);

CREATE TABLE follows (
    follower_id     INTEGER REFERENCES users(id),
    followee_id     INTEGER REFERENCES users(id),
    created_at      TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (follower_id, followee_id)
);
CREATE INDEX idx_follows_followee ON follows(followee_id);

CREATE TABLE likes (
    user_id         INTEGER REFERENCES users(id),
    post_id         INTEGER REFERENCES posts(id),
    created_at      TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (user_id, post_id)
);
CREATE INDEX idx_likes_post ON likes(post_id);
```

**Feed Generation: Pure Pull (Fan-out-on-read)**

```python
# Simple feed query - works great for small scale
def get_feed(user_id: int, limit: int = 20, offset: int = 0):
    """
    Pull-based feed: Query at read time
    Performance: ~50-100ms for 10K users
    """
    return db.execute("""
        SELECT p.*, u.username, u.avatar_url,
               COUNT(DISTINCT l.user_id) as like_count,
               EXISTS(SELECT 1 FROM likes WHERE user_id = %s AND post_id = p.id) as is_liked
        FROM posts p
        JOIN users u ON p.user_id = u.id
        JOIN follows f ON f.followee_id = p.user_id
        LEFT JOIN likes l ON l.post_id = p.id
        WHERE f.follower_id = %s
          AND p.created_at > NOW() - INTERVAL '7 days'
        GROUP BY p.id, u.id
        ORDER BY p.created_at DESC
        LIMIT %s OFFSET %s
    """, [user_id, user_id, limit, offset])
```

**Infrastructure:**

```
Single Server Setup (~$100-300/month):

┌────────────────────────────────────────────────────────┐
│  1x Server (2-4 vCPU, 4-8GB RAM, 80GB SSD)   ~$50-100  │
│    - Application                                        │
│    - PostgreSQL                                         │
│    - nginx                                              │
├────────────────────────────────────────────────────────┤
│  S3 for media storage                         ~$20-50  │
├────────────────────────────────────────────────────────┤
│  Backups, DNS, SSL                            ~$20-50  │
└────────────────────────────────────────────────────────┘

Alternative: Use PaaS (Railway, Render, Heroku)
  - Easier ops, slightly higher cost
  - Good for teams without DevOps experience
```

**When to Move to Next Tier:**
- Feed queries taking >200ms consistently
- Database CPU >70% during peaks
- Single server can't handle traffic spikes
- Need for more reliability (>99% uptime)

---

### 2.2 Tier 2: 100K Users (Growing Startup)

**Scale Profile:**
```
Total users:       100,000
Daily active:      20,000 (20%)
Posts/day:         5,000
Feed reads/day:    200,000
Feed reads/sec:    ~2-3 QPS (peak ~50 QPS)
Storage/month:     ~5 GB posts + 500 GB media
```

**Architecture: Monolith + Redis Cache**

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Load Balancer                               │
│                        (nginx)                                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
         ┌──────────────────┐  ┌──────────────────┐
         │   App Server 1   │  │   App Server 2   │
         │   (Monolith)     │  │   (Monolith)     │
         └──────────────────┘  └──────────────────┘
                    │                   │
                    └─────────┬─────────┘
                              │
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │ PostgreSQL   │  │    Redis     │  │     S3       │
    │ (Primary +   │  │   (Cache)    │  │   (Media)    │
    │  Read Replica)│  │              │  │              │
    └──────────────┘  └──────────────┘  └──────────────┘
```

**Key Changes from Tier 1:**
1. **Add Redis** for caching feed results and hot data
2. **Add read replica** for PostgreSQL
3. **Multiple app servers** behind load balancer
4. **Move to S3** for all media (if not already)

**Caching Strategy:**

```python
import redis
import json

redis_client = redis.Redis(host='localhost', port=6379, db=0)
FEED_CACHE_TTL = 60  # 1 minute

def get_feed_cached(user_id: int, limit: int = 20, offset: int = 0):
    cache_key = f"feed:{user_id}:{offset}"

    # Try cache first
    cached = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    # Cache miss - query database
    feed = get_feed_from_db(user_id, limit, offset)

    # Cache for 1 minute
    redis_client.setex(cache_key, FEED_CACHE_TTL, json.dumps(feed))

    return feed

def invalidate_feed_cache(follower_ids: list):
    """Called when a user posts something new"""
    pipe = redis_client.pipeline()
    for follower_id in follower_ids:
        # Delete first page cache (most accessed)
        pipe.delete(f"feed:{follower_id}:0")
    pipe.execute()

# Cache user profiles (frequently accessed)
def get_user_cached(user_id: int):
    cache_key = f"user:{user_id}"
    cached = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    user = get_user_from_db(user_id)
    redis_client.setex(cache_key, 300, json.dumps(user))  # 5 min TTL
    return user
```

**Simple Feed Pre-computation (Optional):**

```python
# Background job to pre-compute feeds for active users
def precompute_active_feeds():
    """Run every 5 minutes via cron"""
    active_users = db.execute("""
        SELECT DISTINCT user_id FROM sessions
        WHERE last_active > NOW() - INTERVAL '1 hour'
    """)

    for user in active_users:
        feed = get_feed_from_db(user.user_id, limit=100, offset=0)
        post_ids = [p['id'] for p in feed]

        # Store as list in Redis
        key = f"precomputed_feed:{user.user_id}"
        pipe = redis_client.pipeline()
        pipe.delete(key)
        if post_ids:
            pipe.rpush(key, *post_ids)
        pipe.expire(key, 600)  # 10 min TTL
        pipe.execute()
```

**Infrastructure:**

```
Multi-Server Setup (~$500-1,000/month):

┌────────────────────────────────────────────────────────┐
│  2x App Servers (2 vCPU, 4GB each)            ~$100    │
├────────────────────────────────────────────────────────┤
│  1x PostgreSQL Primary (4 vCPU, 8GB, 200GB)   ~$150    │
│  1x PostgreSQL Read Replica (2 vCPU, 4GB)     ~$80     │
├────────────────────────────────────────────────────────┤
│  1x Redis (2 vCPU, 4GB)                       ~$80     │
│  (or ElastiCache/Memorystore)                          │
├────────────────────────────────────────────────────────┤
│  S3 + CloudFront (500GB, 1TB transfer)        ~$100    │
├────────────────────────────────────────────────────────┤
│  Load Balancer, Monitoring, Backups           ~$100    │
└────────────────────────────────────────────────────────┘
```

**When to Move to Next Tier:**
- Redis cache hit rate dropping below 80%
- Need background job processing (async tasks)
- Feed query complexity increasing
- Read replica falling behind
- Need for CDN for API responses

---

### 2.3 Tier 3: 1M Users (Scaling Startup)

**Scale Profile:**
```
Total users:       1,000,000
Daily active:      200,000 (20%)
Posts/day:         50,000
Feed reads/day:    4,000,000
Feed reads/sec:    ~50 QPS (peak ~500 QPS)
Storage/month:     ~50 GB posts + 5 TB media
```

**Architecture: Monolith + Queue + CDN**

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CLIENTS                                         │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CDN (CloudFront)                                   │
│                    - Media delivery                                          │
│                    - API response caching (optional)                         │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Application Load Balancer                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
            ┌───────────────────────┼───────────────────────┐
            ▼                       ▼                       ▼
    ┌──────────────┐        ┌──────────────┐        ┌──────────────┐
    │  App Server  │        │  App Server  │        │  App Server  │
    │  (API)       │        │  (API)       │        │  (API)       │
    └──────────────┘        └──────────────┘        └──────────────┘
            │                       │                       │
            └───────────────────────┼───────────────────────┘
                                    │
    ┌───────────────────────────────┼───────────────────────────────┐
    │                               │                               │
    ▼                               ▼                               ▼
┌──────────────┐            ┌──────────────┐            ┌──────────────┐
│ PostgreSQL   │            │    Redis     │            │    SQS /     │
│ (Sharded or  │            │   Cluster    │            │   RabbitMQ   │
│  RDS)        │            │              │            │              │
└──────────────┘            └──────────────┘            └──────────────┘
                                                               │
                                                               ▼
                                                    ┌──────────────────┐
                                                    │  Worker Servers  │
                                                    │  - Fan-out       │
                                                    │  - Media proc.   │
                                                    │  - Notifications │
                                                    └──────────────────┘
```

**Key Changes from Tier 2:**
1. **Add message queue** (SQS/RabbitMQ) for async processing
2. **Add worker servers** for background jobs
3. **Redis Cluster** or multiple Redis instances
4. **CDN** for all media delivery
5. **Introduce fan-out-on-write** for feeds

**Hybrid Feed Strategy (Push for small accounts):**

```python
from celery import Celery

app = Celery('tasks', broker='redis://localhost:6379/1')

PUSH_THRESHOLD = 5000  # Push if author has < 5000 followers

@app.task
def process_new_post(post_id: int, author_id: int):
    """Async task triggered when post is created"""
    follower_count = get_follower_count(author_id)

    if follower_count < PUSH_THRESHOLD:
        # Fan-out-on-write for regular users
        fan_out_to_followers(post_id, author_id)
    else:
        # Store in "celebrity posts" for pull at read time
        store_celebrity_post(post_id, author_id)

def fan_out_to_followers(post_id: int, author_id: int):
    """Push post_id to each follower's feed cache"""
    followers = get_followers_paginated(author_id)

    pipe = redis_client.pipeline()
    for batch in chunks(followers, 1000):
        for follower_id in batch:
            feed_key = f"feed:{follower_id}"
            pipe.lpush(feed_key, post_id)
            pipe.ltrim(feed_key, 0, 499)  # Keep last 500
        pipe.execute()
        pipe = redis_client.pipeline()

def get_feed(user_id: int, limit: int = 20, cursor: int = 0):
    """Hybrid feed: cached + pull for celebrities"""
    # Get pre-pushed posts
    feed_key = f"feed:{user_id}"
    cached_ids = redis_client.lrange(feed_key, cursor, cursor + limit * 2)

    # Get celebrity followees
    celebrity_followees = get_celebrity_followees(user_id)
    celebrity_post_ids = []

    for celeb_id in celebrity_followees[:20]:
        recent = redis_client.zrevrange(
            f"celebrity_posts:{celeb_id}", 0, 10
        )
        celebrity_post_ids.extend(recent)

    # Merge and dedupe
    all_ids = list(dict.fromkeys(cached_ids + celebrity_post_ids))

    # Fetch posts and sort by recency
    posts = batch_get_posts(all_ids)
    posts.sort(key=lambda p: p['created_at'], reverse=True)

    return posts[:limit]
```

**Media Processing Pipeline:**

```python
@app.task
def process_media_upload(upload_id: str):
    """Async media processing"""
    # Download from temp S3 location
    original = download_from_s3(f"uploads/{upload_id}")

    # Generate multiple sizes
    sizes = {
        'thumbnail': (150, 150),
        'small': (320, 320),
        'medium': (640, 640),
        'large': (1080, 1080),
    }

    for name, dimensions in sizes.items():
        resized = resize_image(original, dimensions)
        compressed = compress_image(resized, quality=85)
        upload_to_s3(f"media/{upload_id}/{name}.webp", compressed)

    # Update post with media URLs
    update_post_media_urls(upload_id)

    # Invalidate CDN if needed
    invalidate_cdn_cache(f"/media/{upload_id}/*")
```

**Database Optimization:**

```sql
-- Add indexes for feed query optimization
CREATE INDEX idx_posts_created_7days ON posts(created_at DESC)
    WHERE created_at > NOW() - INTERVAL '7 days';

-- Partition likes table by post_id range
CREATE TABLE likes_partitioned (
    user_id     INTEGER,
    post_id     INTEGER,
    created_at  TIMESTAMP DEFAULT NOW()
) PARTITION BY RANGE (post_id);

-- Materialized view for like counts (refresh every minute)
CREATE MATERIALIZED VIEW post_like_counts AS
SELECT post_id, COUNT(*) as like_count
FROM likes
GROUP BY post_id;

CREATE UNIQUE INDEX idx_post_like_counts ON post_like_counts(post_id);

-- Refresh via cron
-- REFRESH MATERIALIZED VIEW CONCURRENTLY post_like_counts;
```

**Infrastructure:**

```
Scaled Setup (~$3,000-5,000/month):

┌────────────────────────────────────────────────────────┐
│  3-5x App Servers (4 vCPU, 8GB each)          ~$400    │
│  2-3x Worker Servers (2 vCPU, 4GB each)       ~$150    │
├────────────────────────────────────────────────────────┤
│  RDS PostgreSQL (8 vCPU, 32GB, 500GB, Multi-AZ)        │
│                                               ~$800    │
├────────────────────────────────────────────────────────┤
│  ElastiCache Redis Cluster (3 nodes)          ~$400    │
├────────────────────────────────────────────────────────┤
│  SQS / RabbitMQ                               ~$50     │
├────────────────────────────────────────────────────────┤
│  S3 (5TB) + CloudFront (10TB transfer)        ~$600    │
├────────────────────────────────────────────────────────┤
│  ALB, CloudWatch, Backups                     ~$300    │
└────────────────────────────────────────────────────────┘
```

**When to Move to Next Tier:**
- PostgreSQL can't handle write load (>5K TPS)
- Single region latency issues for global users
- Need for real-time features (live updates)
- Worker queue backlog growing
- Team growing, need service boundaries

---

### 2.4 Tier 4: 10M Users (Scale-Up)

**Scale Profile:**
```
Total users:       10,000,000
Daily active:      2,000,000 (20%)
Posts/day:         500,000
Feed reads/day:    50,000,000
Feed reads/sec:    ~500 QPS (peak ~5,000 QPS)
Storage/month:     ~500 GB posts + 50 TB media
```

**Architecture: Microservices + Kafka**

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                 CLIENTS                                          │
└─────────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              CDN (Multi-POP)                                     │
└─────────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              API Gateway                                         │
│                    (Kong / AWS API Gateway)                                      │
│             - Rate limiting, Auth, Routing                                       │
└─────────────────────────────────────────────────────────────────────────────────┘
                                      │
            ┌─────────────────────────┼─────────────────────────┐
            ▼                         ▼                         ▼
┌───────────────────┐     ┌───────────────────┐     ┌───────────────────┐
│   Feed Service    │     │   Post Service    │     │   User Service    │
│   (8 instances)   │     │   (5 instances)   │     │   (3 instances)   │
└───────────────────┘     └───────────────────┘     └───────────────────┘
            │                         │                         │
            ▼                         ▼                         ▼
┌───────────────────┐     ┌───────────────────┐     ┌───────────────────┐
│  Feed Cache       │     │   Post Store      │     │  User Store       │
│  (Redis Cluster)  │     │  (Cassandra)      │     │  (PostgreSQL)     │
└───────────────────┘     └───────────────────┘     └───────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              KAFKA CLUSTER                                       │
│                                                                                  │
│    Topics: posts.created, posts.deleted, interactions, feed.fanout               │
└─────────────────────────────────────────────────────────────────────────────────┘
                                      │
            ┌─────────────────────────┼─────────────────────────┐
            ▼                         ▼                         ▼
┌───────────────────┐     ┌───────────────────┐     ┌───────────────────┐
│  Fan-out Workers  │     │  Counter Workers  │     │  Media Workers    │
│  (20 instances)   │     │  (5 instances)    │     │  (10 instances)   │
└───────────────────┘     └───────────────────┘     └───────────────────┘
```

**Key Changes from Tier 3:**
1. **Split into microservices** (Feed, Post, User, Media)
2. **Introduce Cassandra** for post storage (write-heavy)
3. **Kafka** for event streaming (replaces SQS)
4. **Kubernetes** for orchestration
5. **Separate databases** per service

**Service Boundaries:**

```yaml
# Service definitions
services:
  feed-service:
    responsibilities:
      - Feed generation and caching
      - Feed ranking
      - Feed pagination
    data_stores:
      - Redis Cluster (feed cache)
      - Read from Post Service API

  post-service:
    responsibilities:
      - Post CRUD
      - Post storage
      - Media URL management
    data_stores:
      - Cassandra (posts)
      - S3 (media references)
    events_produced:
      - posts.created
      - posts.deleted
      - posts.updated

  user-service:
    responsibilities:
      - User profiles
      - Social graph (follows)
      - Authentication
    data_stores:
      - PostgreSQL (users, follows)
      - Redis (session cache)

  interaction-service:
    responsibilities:
      - Likes, comments, saves
      - Engagement counters
    data_stores:
      - Cassandra (interactions)
      - Redis (counter cache)
    events_produced:
      - interactions.like
      - interactions.comment
```

**Cassandra Schema for Posts:**

```sql
-- Cassandra: Posts by user (for profile view)
CREATE TABLE posts_by_user (
    user_id     UUID,
    post_id     TIMEUUID,
    caption     TEXT,
    media_urls  LIST<TEXT>,
    created_at  TIMESTAMP,
    PRIMARY KEY ((user_id), post_id)
) WITH CLUSTERING ORDER BY (post_id DESC);

-- Cassandra: Posts by ID (for feed hydration)
CREATE TABLE posts_by_id (
    post_id     TIMEUUID PRIMARY KEY,
    user_id     UUID,
    caption     TEXT,
    media_urls  LIST<TEXT>,
    like_count  INT,
    comment_count INT,
    created_at  TIMESTAMP
);

-- Cassandra: Engagement counters
CREATE TABLE post_counters (
    post_id     TIMEUUID PRIMARY KEY,
    like_count  COUNTER,
    comment_count COUNTER,
    save_count  COUNTER,
    view_count  COUNTER
);
```

**Kafka-Based Fan-out:**

```python
from kafka import KafkaConsumer, KafkaProducer

# Fan-out consumer
consumer = KafkaConsumer(
    'posts.created',
    bootstrap_servers=['kafka:9092'],
    group_id='fanout-workers',
    auto_offset_reset='latest'
)

def process_fanout():
    for message in consumer:
        event = json.loads(message.value)
        post_id = event['post_id']
        author_id = event['author_id']
        follower_count = event['follower_count']

        if follower_count < CELEBRITY_THRESHOLD:
            # Batch fan-out
            fan_out_batch(post_id, author_id)
        else:
            # Index for pull-based retrieval
            index_celebrity_post(post_id, author_id)

def fan_out_batch(post_id: str, author_id: str):
    """Fan-out with batching and rate limiting"""
    followers = get_followers_stream(author_id)

    batch = []
    for follower_id in followers:
        batch.append(follower_id)

        if len(batch) >= 1000:
            # Batch write to Redis
            write_to_feed_cache_batch(batch, post_id)
            batch = []

            # Rate limit to avoid Redis overload
            time.sleep(0.01)  # 100 batches/sec max

    if batch:
        write_to_feed_cache_batch(batch, post_id)
```

**Simple Ranking (Rule-Based):**

```python
def rank_feed_posts(posts: list, user_id: str) -> list:
    """
    Simple scoring without ML:
    - Recency (50%)
    - Engagement (30%)
    - Author affinity (20%)
    """
    user_affinities = get_user_affinities(user_id)
    now = datetime.utcnow()

    for post in posts:
        # Recency score (0-1, half-life = 12 hours)
        age_hours = (now - post['created_at']).total_seconds() / 3600
        recency = math.exp(-age_hours / 12)

        # Engagement score (log-scaled)
        engagement = (
            math.log1p(post['like_count']) * 0.5 +
            math.log1p(post['comment_count']) * 0.3 +
            math.log1p(post.get('save_count', 0)) * 0.2
        ) / 15  # Normalize

        # Affinity score
        affinity = user_affinities.get(post['user_id'], 0.5)

        # Combined score
        post['score'] = recency * 0.5 + engagement * 0.3 + affinity * 0.2

    return sorted(posts, key=lambda p: p['score'], reverse=True)
```

**Infrastructure:**

```
Kubernetes-Based Setup (~$30,000-50,000/month):

┌─────────────────────────────────────────────────────────────────┐
│  EKS/GKE Cluster                                                 │
│  - 15-20 nodes (8 vCPU, 32GB each)                    ~$8,000   │
│  - Auto-scaling, spot instances for workers           ~$3,000   │
├─────────────────────────────────────────────────────────────────┤
│  Cassandra Cluster (6 nodes, i3.xlarge)               ~$5,000   │
├─────────────────────────────────────────────────────────────────┤
│  PostgreSQL RDS (Multi-AZ, 16 vCPU)                   ~$2,000   │
├─────────────────────────────────────────────────────────────────┤
│  ElastiCache Redis Cluster (6 nodes)                  ~$2,000   │
├─────────────────────────────────────────────────────────────────┤
│  MSK Kafka Cluster (6 brokers)                        ~$3,000   │
├─────────────────────────────────────────────────────────────────┤
│  S3 (50TB) + CloudFront (100TB transfer)              ~$5,000   │
├─────────────────────────────────────────────────────────────────┤
│  Monitoring, Logging, Security                        ~$2,000   │
└─────────────────────────────────────────────────────────────────┘
```

**When to Move to Next Tier:**
- Need for multi-region deployment
- Single Kafka cluster hitting limits
- Need for ML-based ranking
- P99 latency > 300ms
- Operational complexity requiring dedicated teams

---

### 2.5 Tier 5: 100M Users (Large Platform)

**Scale Profile:**
```
Total users:       100,000,000
Daily active:      20,000,000 (20%)
Posts/day:         5,000,000
Feed reads/day:    500,000,000
Feed reads/sec:    ~5,000 QPS (peak ~50,000 QPS)
Storage/month:     ~5 TB posts + 500 TB media
```

**Architecture: Distributed Multi-Region**

```
                              ┌────────────────────┐
                              │    Global DNS      │
                              │   (Route 53)       │
                              │  Latency-based     │
                              └────────────────────┘
                                       │
            ┌──────────────────────────┼──────────────────────────┐
            ▼                          ▼                          ▼
    ┌───────────────┐          ┌───────────────┐          ┌───────────────┐
    │   US-WEST     │          │   EU-WEST     │          │  AP-SOUTH     │
    │   (Primary)   │          │  (Secondary)  │          │  (Secondary)  │
    └───────────────┘          └───────────────┘          └───────────────┘
            │                          │                          │
    ┌───────┴───────┐          ┌───────┴───────┐          ┌───────┴───────┐
    │               │          │               │          │               │
    ▼               ▼          ▼               ▼          ▼               ▼
┌───────┐     ┌─────────┐  ┌───────┐    ┌─────────┐  ┌───────┐    ┌─────────┐
│ CDN   │     │Services │  │ CDN   │    │Services │  │ CDN   │    │Services │
│ Edge  │     │ Cluster │  │ Edge  │    │ Cluster │  │ Edge  │    │ Cluster │
└───────┘     └─────────┘  └───────┘    └─────────┘  └───────┘    └─────────┘
                  │                          │                          │
    ┌─────────────┴─────────────┬────────────┴────────────┬─────────────┘
    │                           │                         │
    ▼                           ▼                         ▼
┌─────────────┐           ┌─────────────┐           ┌─────────────┐
│ Cassandra   │◄─────────▶│ Cassandra   │◄─────────▶│ Cassandra   │
│ (DC: us)    │  Multi-DC │ (DC: eu)    │  Multi-DC │ (DC: ap)    │
└─────────────┘           └─────────────┘           └─────────────┘
    │                           │                         │
    ▼                           ▼                         ▼
┌─────────────┐           ┌─────────────┐           ┌─────────────┐
│ Kafka       │◄─────────▶│ Kafka       │◄─────────▶│ Kafka       │
│ Cluster     │ MirrorMkr │ Cluster     │ MirrorMkr │ Cluster     │
└─────────────┘           └─────────────┘           └─────────────┘
```

**Key Changes from Tier 4:**
1. **Multi-region deployment** (active-active or active-passive)
2. **Regional Kafka clusters** with MirrorMaker
3. **Cassandra multi-DC** replication
4. **ML-based ranking** service
5. **Dedicated recommendation service**
6. **Feature store** for ML features

**Regional Service Deployment:**

```yaml
# Kubernetes regional deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: feed-service
  labels:
    region: us-west-2
spec:
  replicas: 20
  selector:
    matchLabels:
      app: feed-service
  template:
    spec:
      containers:
      - name: feed-service
        image: feed-service:v2.3.1
        resources:
          requests:
            memory: "4Gi"
            cpu: "2"
          limits:
            memory: "8Gi"
            cpu: "4"
        env:
        - name: REGION
          value: "us-west-2"
        - name: CASSANDRA_DC
          value: "us-west"
        - name: REDIS_CLUSTER
          value: "redis-us-west.internal:6379"
        - name: KAFKA_BROKERS
          value: "kafka-us-west.internal:9092"
```

**ML Ranking Integration:**

```python
class RankingService:
    def __init__(self):
        self.model = load_model("ranking_model_v3.onnx")
        self.feature_store = FeatureStoreClient()

    async def rank(self, user_id: str, candidates: list, limit: int = 20):
        # Stage 1: Quick filter (rule-based)
        filtered = self.quick_filter(candidates, limit * 5)

        # Stage 2: ML ranking
        features = await self.feature_store.batch_get(
            user_id=user_id,
            post_ids=[p['post_id'] for p in filtered]
        )

        scores = []
        for post, feat in zip(filtered, features):
            feature_vector = self.build_feature_vector(user_id, post, feat)
            score = self.model.predict(feature_vector)
            scores.append((post, score))

        # Sort and apply diversity
        ranked = sorted(scores, key=lambda x: x[1], reverse=True)
        return self.apply_diversity(ranked, limit)

    def build_feature_vector(self, user_id, post, features):
        return np.array([
            features['post_age_hours'],
            features['like_count'],
            features['comment_count'],
            features['save_count'],
            features['author_follower_count'],
            features['user_author_affinity'],
            features['user_engagement_rate'],
            features['post_engagement_rate'],
            features['hour_of_day'],
            features['day_of_week'],
            # ... more features
        ])
```

**Data Replication Strategy:**

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        DATA REPLICATION STRATEGY                                 │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Cassandra (Multi-DC):                                                           │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  Replication: NetworkTopologyStrategy                                      │ │
│  │  US: 3 replicas, EU: 3 replicas, AP: 3 replicas                            │ │
│  │  Consistency: LOCAL_QUORUM (reads/writes)                                  │ │
│  │  Cross-DC lag: < 100ms typical                                             │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  Redis (Active-Active with CRDT):                                                │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  Feed lists: Last-writer-wins with timestamp                               │ │
│  │  Counters: CRDT counters (conflict-free merge)                             │ │
│  │  Cross-region sync: < 50ms                                                 │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  Kafka (MirrorMaker 2):                                                          │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  Topics mirrored: posts.*, interactions.*                                  │ │
│  │  Replication lag: < 1 second                                               │ │
│  │  Consumers in each region process independently                            │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

**Infrastructure:**

```
Multi-Region Setup (~$300,000-500,000/month):

PER REGION (x3 regions):
┌─────────────────────────────────────────────────────────────────┐
│  Kubernetes Cluster                                              │
│  - 30-50 nodes (16 vCPU, 64GB)                        ~$30,000  │
│  - GPU nodes for ML (4x p3.2xlarge)                   ~$10,000  │
├─────────────────────────────────────────────────────────────────┤
│  Cassandra Cluster (12 nodes, i3.2xlarge)             ~$15,000  │
├─────────────────────────────────────────────────────────────────┤
│  Redis Cluster (12 nodes, r6g.xlarge)                 ~$8,000   │
├─────────────────────────────────────────────────────────────────┤
│  Kafka Cluster (12 brokers, m5.2xlarge)               ~$10,000  │
├─────────────────────────────────────────────────────────────────┤
│  Regional subtotal                                    ~$73,000  │
└─────────────────────────────────────────────────────────────────┘

GLOBAL:
┌─────────────────────────────────────────────────────────────────┐
│  S3 (500TB) + CloudFront (1PB transfer)               ~$80,000  │
├─────────────────────────────────────────────────────────────────┤
│  ML Training Infrastructure                           ~$20,000  │
├─────────────────────────────────────────────────────────────────┤
│  Monitoring, Security, Compliance                     ~$30,000  │
├─────────────────────────────────────────────────────────────────┤
│  Global subtotal                                      ~$130,000 │
└─────────────────────────────────────────────────────────────────┘

TOTAL: 3 × $73,000 + $130,000 ≈ $350,000/month
```

---

### 2.6 Migration Path Summary

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         MIGRATION PATH                                           │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  10K → 100K:                                                                     │
│  ├── Add Redis for caching                                                       │
│  ├── Add PostgreSQL read replica                                                 │
│  ├── Move to multiple app servers                                                │
│  └── ~1-2 weeks engineering effort                                               │
│                                                                                  │
│  100K → 1M:                                                                       │
│  ├── Add message queue (SQS/RabbitMQ)                                            │
│  ├── Add worker processes                                                        │
│  ├── Implement basic fan-out-on-write                                            │
│  ├── Add CDN for media                                                           │
│  └── ~4-6 weeks engineering effort                                               │
│                                                                                  │
│  1M → 10M:                                                                        │
│  ├── Split monolith into services                                                │
│  ├── Migrate posts to Cassandra                                                  │
│  ├── Replace queue with Kafka                                                    │
│  ├── Deploy to Kubernetes                                                        │
│  └── ~3-6 months engineering effort                                              │
│                                                                                  │
│  10M → 100M:                                                                      │
│  ├── Add second region                                                           │
│  ├── Implement cross-region replication                                          │
│  ├── Add ML ranking service                                                      │
│  ├── Build feature store                                                         │
│  └── ~6-12 months engineering effort                                             │
│                                                                                  │
│  100M → 500M+:                                                                    │
│  ├── Add third+ regions                                                          │
│  ├── Implement full hybrid fan-out                                               │
│  ├── Advanced ML personalization                                                 │
│  ├── Custom infrastructure optimizations                                         │
│  └── Continuous evolution                                                        │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Capacity Estimates (500M+ Scale)

### Traffic Analysis

```
Users:
  Total users:           2,000,000,000
  Daily active users:    500,000,000
  Peak concurrent users: 50,000,000

Posts:
  New posts/day:         100,000,000
  Posts/second (avg):    1,157 TPS
  Posts/second (peak):   ~5,000 TPS

Feed Reads:
  Feed requests/day:     5,000,000,000
  Reads/second (avg):    57,870 QPS
  Reads/second (peak):   ~200,000 QPS

Interactions:
  Likes/day:             4,000,000,000
  Comments/day:          500,000,000
  Saves/day:             200,000,000
```

### Storage Estimates

```
Post Metadata (per post):
  - post_id:        8 bytes
  - user_id:        8 bytes
  - caption:        500 bytes (avg)
  - hashtags:       100 bytes
  - location:       50 bytes
  - media_urls:     200 bytes
  - timestamps:     16 bytes
  - counters:       24 bytes
  Total:            ~1 KB per post

Daily post storage:     100M * 1KB = 100 GB/day
7-day post storage:     700 GB
1-year post storage:    ~35 TB

Feed Cache (per user):
  - 500 post IDs cached
  - 8 bytes per post_id
  - Total: 4 KB per user

Active user feed cache: 500M * 4KB = 2 TB

Media Storage:
  - Avg image: 500 KB (multiple resolutions)
  - Avg video: 10 MB (multiple qualities)
  - 70% images, 30% videos
  - Daily: 70M * 500KB + 30M * 10MB = 335 TB/day
```

### Bandwidth Estimates

```
Feed Delivery:
  - 20 posts per request
  - Post metadata: 20 KB
  - Media thumbnails: 200 KB
  - Total per request: ~220 KB

Peak bandwidth:
  - 200K QPS * 220KB = 44 GB/s outbound
  - CDN handles 95%+ of media traffic
```

---

## 4. High-Level Architecture (500M+ Scale)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              MOBILE CLIENTS                                      │
│                         (iOS, Android, Web)                                      │
└─────────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                            CDN (CloudFront/Akamai)                               │
│                    - Media delivery (images, videos)                             │
│                    - Edge caching for static content                             │
│                    - 95%+ cache hit rate for media                               │
└─────────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           API Gateway / Load Balancer                            │
│                    - Rate limiting (1000 req/min per user)                       │
│                    - Authentication (JWT validation)                             │
│                    - Request routing                                             │
│                    - SSL termination                                             │
└─────────────────────────────────────────────────────────────────────────────────┘
                                      │
            ┌─────────────────────────┼─────────────────────────┐
            ▼                         ▼                         ▼
┌───────────────────┐     ┌───────────────────┐     ┌───────────────────┐
│   Feed Service    │     │   Post Service    │     │ Interaction Svc   │
│                   │     │                   │     │                   │
│ - Get feed        │     │ - Create post     │     │ - Like/unlike     │
│ - Refresh feed    │     │ - Delete post     │     │ - Comment         │
│ - Paginate        │     │ - Edit post       │     │ - Save/unsave     │
└───────────────────┘     └───────────────────┘     └───────────────────┘
            │                         │                         │
            ▼                         ▼                         ▼
┌───────────────────┐     ┌───────────────────┐     ┌───────────────────┐
│  Ranking Service  │     │   Media Service   │     │  Social Graph     │
│                   │     │                   │     │                   │
│ - ML ranking      │     │ - Upload handler  │     │ - Followers       │
│ - Feature scoring │     │ - Transcoding     │     │ - Following       │
│ - Personalization │     │ - CDN integration │     │ - Affinity scores │
└───────────────────┘     └───────────────────┘     └───────────────────┘
            │                         │                         │
            └─────────────────────────┼─────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              DATA LAYER                                          │
├───────────────┬───────────────┬───────────────┬───────────────┬─────────────────┤
│ Feed Cache    │ Post Store    │ Social Graph  │ Feature Store │ Object Storage  │
│ (Redis        │ (Cassandra)   │ (MySQL +      │ (Redis)       │ (S3)            │
│  Cluster)     │               │  Graph DB)    │               │                 │
└───────────────┴───────────────┴───────────────┴───────────────┴─────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           ASYNC PROCESSING                                       │
├───────────────────────────────────────────────────────────────────────────────────┤
│  Kafka Topics:                                                                    │
│  - posts.created    → Fan-out workers, Notification service                       │
│  - posts.deleted    → Feed invalidation, CDN purge                                │
│  - interactions     → Counter updates, Ranking feature updates                    │
│  - recommendations  → ML pipeline, A/B experiments                                │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Feed Generation Strategy

### The Core Problem

When User A creates a post, how do we make it appear in their 200 followers' feeds efficiently?

### Strategy Comparison

| Strategy | How it Works | Pros | Cons |
|----------|-------------|------|------|
| **Fan-out-on-write (Push)** | On post creation, push to all followers' feed caches | Fast reads (O(1)), simple ranking | Slow writes for celebrities, wasted storage for inactive users |
| **Fan-out-on-read (Pull)** | On feed request, query all followees' posts | Fast writes, no storage waste | Slow reads (N queries), complex ranking |
| **Hybrid** | Push for regular users, pull for celebrities | Best of both worlds | Complex to implement |

### Our Choice: Hybrid Approach

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         FEED GENERATION STRATEGY                                 │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  User Type Classification:                                                       │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  Regular User:    < 10,000 followers    → Fan-out-on-write (PUSH)          │ │
│  │  Celebrity:       >= 10,000 followers   → Fan-out-on-read (PULL)           │ │
│  │  Super Celebrity: >= 1,000,000 followers → Pull + aggressive caching       │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  Feed Composition (20 posts):                                                    │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  [14-16 posts] From pre-computed feed cache (pushed content)               │ │
│  │  [2-3 posts]   From celebrities user follows (pulled at read time)         │ │
│  │  [2-3 posts]   Recommendations/Ads (injected at read time)                 │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Fan-out-on-Write Flow (Regular Users)

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  User posts  │────▶│  Post saved  │────▶│ Kafka event  │────▶│  Fan-out     │
│  new photo   │     │  to DB       │     │  published   │     │  workers     │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
                                                                       │
                                                                       ▼
                                                         ┌─────────────────────────┐
                                                         │ For each follower:      │
                                                         │ 1. Get follower_id      │
                                                         │ 2. LPUSH to feed cache  │
                                                         │ 3. LTRIM to 500 items   │
                                                         └─────────────────────────┘
```

**Implementation:**

```python
# Fan-out worker (Kafka consumer)
async def process_post_created(event: PostCreatedEvent):
    post_id = event.post_id
    author_id = event.author_id

    # Check if author is celebrity
    follower_count = await social_graph.get_follower_count(author_id)
    if follower_count >= CELEBRITY_THRESHOLD:
        # Don't fan out for celebrities
        await celebrity_posts.add(author_id, post_id, event.created_at)
        return

    # Get all followers (paginated for memory efficiency)
    cursor = None
    while True:
        followers, cursor = await social_graph.get_followers(
            author_id, cursor=cursor, limit=1000
        )

        # Batch write to Redis
        pipe = redis.pipeline()
        for follower_id in followers:
            feed_key = f"feed:{follower_id}"
            pipe.lpush(feed_key, post_id)
            pipe.ltrim(feed_key, 0, FEED_CACHE_SIZE - 1)
        await pipe.execute()

        if cursor is None:
            break

    # Update fan-out metrics
    metrics.increment("fanout.posts", tags={"type": "regular"})
    metrics.histogram("fanout.followers", follower_count)
```

### Fan-out-on-Read Flow (Celebrities)

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  User opens  │────▶│ Get cached feed  │────▶│ Get celebrity    │
│  feed        │     │ (pushed posts)   │     │ followees list   │
└──────────────┘     └──────────────────┘     └──────────────────┘
                                                       │
                                                       ▼
                                          ┌────────────────────────┐
                                          │ For each celebrity:    │
                                          │ Fetch recent posts     │
                                          │ (from sorted set cache)│
                                          └────────────────────────┘
                                                       │
                                                       ▼
                                          ┌────────────────────────┐
                                          │ Merge + Rank all posts │
                                          │ Return top 20          │
                                          └────────────────────────┘
```

**Implementation:**

```python
async def get_feed(user_id: str, cursor: str = None, limit: int = 20) -> FeedResponse:
    # 1. Get pre-computed feed (pushed posts)
    feed_key = f"feed:{user_id}"
    start_idx = decode_cursor(cursor) if cursor else 0

    # Fetch more than needed for ranking
    cached_post_ids = await redis.lrange(feed_key, start_idx, start_idx + limit * 3)

    # 2. Get celebrity posts user follows
    celebrity_followees = await social_graph.get_celebrity_followees(user_id)
    celebrity_posts = []

    if celebrity_followees:
        # Parallel fetch from celebrity post caches
        tasks = [
            get_recent_celebrity_posts(celeb_id)
            for celeb_id in celebrity_followees[:50]  # Limit to 50 celebrities
        ]
        results = await asyncio.gather(*tasks)
        celebrity_posts = flatten(results)

    # 3. Merge candidates
    all_candidates = set(cached_post_ids) | set(celebrity_posts)

    # 4. Fetch post metadata + engagement
    posts = await post_store.batch_get(list(all_candidates))

    # 5. Apply ranking
    ranked_posts = await ranking_service.rank(
        user_id=user_id,
        candidates=posts,
        limit=limit
    )

    # 6. Inject recommendations (10-20% of feed)
    recommendations = await recommendation_service.get_posts(
        user_id=user_id,
        count=max(2, limit // 10)
    )
    final_feed = interleave_recommendations(ranked_posts, recommendations)

    return FeedResponse(
        posts=final_feed[:limit],
        next_cursor=encode_cursor(start_idx + limit)
    )
```

### Celebrity Post Cache

```
Redis Structure for Celebrity Posts:

Key: celebrity_posts:{user_id}
Type: Sorted Set
Score: Unix timestamp (for recency ordering)
Member: post_id

ZADD celebrity_posts:kim_k 1704567890 "post_12345"
ZREVRANGEBYSCORE celebrity_posts:kim_k +inf (now-7days) LIMIT 0 100

TTL: 7 days (automatic cleanup via TTL or background job)
```

---

## 6. Data Storage Design

### 6.1 Post Store (Cassandra)

**Why Cassandra?**
- High write throughput (100M posts/day)
- Linear scalability
- Tunable consistency
- Time-series friendly

```sql
-- Posts table (partitioned by user_id for efficient author queries)
CREATE TABLE posts (
    user_id         UUID,
    post_id         TIMEUUID,
    caption         TEXT,
    hashtags        SET<TEXT>,
    location_id     UUID,
    location_name   TEXT,
    media_urls      LIST<FROZEN<media_info>>,
    like_count      COUNTER,
    comment_count   COUNTER,
    save_count      COUNTER,
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP,
    is_deleted      BOOLEAN,
    PRIMARY KEY ((user_id), post_id)
) WITH CLUSTERING ORDER BY (post_id DESC)
  AND default_time_to_live = 31536000;  -- 1 year

-- Post by ID lookup (for feed hydration)
CREATE TABLE posts_by_id (
    post_id         TIMEUUID,
    user_id         UUID,
    caption         TEXT,
    hashtags        SET<TEXT>,
    media_urls      LIST<FROZEN<media_info>>,
    like_count      INT,
    comment_count   INT,
    created_at      TIMESTAMP,
    PRIMARY KEY (post_id)
);

-- User-defined type for media
CREATE TYPE media_info (
    media_id        UUID,
    media_type      TEXT,      -- 'image' or 'video'
    url_template    TEXT,      -- CDN URL with resolution placeholder
    width           INT,
    height          INT,
    duration_ms     INT        -- for videos
);
```

### 6.2 Feed Cache (Redis Cluster)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           REDIS FEED CACHE                                       │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  User Feed (List):                                                               │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  Key:     feed:{user_id}                                                   │ │
│  │  Type:    LIST                                                             │ │
│  │  Values:  [post_id_1, post_id_2, ..., post_id_500]                         │ │
│  │  TTL:     7 days (refreshed on access)                                     │ │
│  │                                                                            │ │
│  │  Commands:                                                                 │ │
│  │  - LPUSH feed:{user_id} {post_id}     -- Add new post to front             │ │
│  │  - LTRIM feed:{user_id} 0 499         -- Keep only 500 posts               │ │
│  │  - LRANGE feed:{user_id} 0 19         -- Get first page                    │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  Celebrity Posts (Sorted Set):                                                   │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  Key:     celebrity_posts:{user_id}                                        │ │
│  │  Type:    ZSET                                                             │ │
│  │  Score:   created_at (unix timestamp)                                      │ │
│  │  Member:  post_id                                                          │ │
│  │  TTL:     7 days                                                           │ │
│  │                                                                            │ │
│  │  Commands:                                                                 │ │
│  │  - ZADD celebrity_posts:{user_id} {ts} {post_id}                           │ │
│  │  - ZREVRANGEBYSCORE ... LIMIT 0 50    -- Get recent posts                  │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  Post Metadata Cache (Hash):                                                     │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  Key:     post:{post_id}                                                   │ │
│  │  Type:    HASH                                                             │ │
│  │  Fields:  user_id, caption, media_urls, like_count, comment_count, etc.    │ │
│  │  TTL:     1 hour                                                           │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

**Redis Cluster Configuration:**

```
Cluster Size: 100 nodes (50 primaries + 50 replicas)
Memory per node: 64 GB
Total capacity: 3.2 TB

Sharding: CRC16 hash slot (16384 slots)
Replication: 1 replica per primary
Persistence: RDB snapshots every 15 minutes (for cold start recovery)
```

### 6.3 Social Graph (MySQL + Redis)

**MySQL (Source of Truth):**

```sql
-- Followers relationship
CREATE TABLE follows (
    follower_id     BIGINT NOT NULL,
    followee_id     BIGINT NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (follower_id, followee_id),
    INDEX idx_followee (followee_id, follower_id)
) ENGINE=InnoDB
  PARTITION BY HASH(follower_id) PARTITIONS 256;

-- User metadata
CREATE TABLE users (
    user_id         BIGINT PRIMARY KEY,
    username        VARCHAR(30) UNIQUE,
    follower_count  INT DEFAULT 0,
    following_count INT DEFAULT 0,
    is_celebrity    BOOLEAN GENERATED ALWAYS AS (follower_count >= 10000) STORED,
    created_at      DATETIME NOT NULL
);
```

**Redis (Cache + Fast Lookups):**

```
# Who does user X follow? (for feed assembly)
Key: following:{user_id}
Type: SET
Members: [followee_id_1, followee_id_2, ...]
TTL: 24 hours

# Celebrity followees (subset for fast lookup)
Key: following_celebrities:{user_id}
Type: SET
Members: [celebrity_id_1, celebrity_id_2, ...]
TTL: 24 hours

# Follower count (for celebrity detection)
Key: follower_count:{user_id}
Type: STRING
Value: count
TTL: 1 hour
```

### 6.4 Feature Store (Redis)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           FEATURE STORE                                          │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Post Features (for ranking):                                                    │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  Key: post_features:{post_id}                                              │ │
│  │  Type: HASH                                                                │ │
│  │  Fields:                                                                   │ │
│  │    - like_count: 1523                                                      │ │
│  │    - comment_count: 89                                                     │ │
│  │    - save_count: 234                                                       │ │
│  │    - view_count: 45000                                                     │ │
│  │    - like_velocity: 120   (likes in last hour)                             │ │
│  │    - engagement_rate: 0.034                                                │ │
│  │    - author_avg_engagement: 0.028                                          │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  User Affinity Scores:                                                           │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  Key: affinity:{user_id}                                                   │ │
│  │  Type: ZSET                                                                │ │
│  │  Score: affinity_score (0-1)                                               │ │
│  │  Member: followee_id                                                       │ │
│  │                                                                            │ │
│  │  Calculated from:                                                          │ │
│  │    - Profile visits                                                        │ │
│  │    - Likes on their posts                                                  │ │
│  │    - Comments on their posts                                               │ │
│  │    - DM frequency                                                          │ │
│  │    - Time spent viewing their content                                      │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 7. Ranking Architecture

### 7.1 Two-Stage Ranking

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           RANKING PIPELINE                                       │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Stage 1: Candidate Generation (1000 → 200 posts)                                │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  - Retrieve from feed cache                                                │ │
│  │  - Retrieve from celebrity posts                                           │ │
│  │  - Retrieve recommendations                                                │ │
│  │  - Filter: seen posts, blocked users, deleted posts                        │ │
│  │  - Quick score: recency + basic engagement                                 │ │
│  │  - Keep top 200 candidates                                                 │ │
│  │  Latency budget: 20ms                                                      │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                      │                                           │
│                                      ▼                                           │
│  Stage 2: Precision Ranking (200 → 20 posts)                                     │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  - Fetch full features from Feature Store                                  │ │
│  │  - Apply ML ranking model (lightweight, <5ms inference)                    │ │
│  │  - Score each candidate                                                    │ │
│  │  - Apply diversity rules (no >3 posts from same author)                    │ │
│  │  - Select top 20                                                           │ │
│  │  Latency budget: 30ms                                                      │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  Total ranking latency: <50ms                                                    │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 7.2 Ranking Score Formula

**Stage 1 (Quick Score):**

```python
def quick_score(post, user_id, now):
    # Time decay (half-life = 6 hours)
    age_hours = (now - post.created_at).total_seconds() / 3600
    recency = math.exp(-age_hours / 6)  # 0 to 1

    # Basic engagement (log-scaled)
    engagement = (
        math.log1p(post.like_count) * 0.5 +
        math.log1p(post.comment_count) * 0.3 +
        math.log1p(post.save_count) * 0.2
    ) / 10  # Normalize to ~0-1

    return recency * 0.4 + engagement * 0.6
```

**Stage 2 (ML Score):**

```python
def ml_score(post, user_context, features):
    """
    Lightweight GBDT model (XGBoost)
    Trained to predict P(engagement | post, user)
    """
    feature_vector = [
        # Post features
        features.like_count,
        features.comment_count,
        features.save_count,
        features.view_count,
        features.like_velocity,
        features.engagement_rate,
        features.post_age_hours,
        features.author_follower_count,
        features.author_avg_engagement,
        features.is_video,
        features.has_hashtags,
        features.caption_length,

        # User-post affinity
        features.user_author_affinity,
        features.user_author_interaction_count,
        features.user_liked_similar_posts,

        # User context
        user_context.hour_of_day,
        user_context.day_of_week,
        user_context.session_length,
        user_context.posts_seen_this_session,
    ]

    # Model inference (<5ms)
    return model.predict_proba(feature_vector)[1]
```

### 7.3 Feature Computation

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        FEATURE COMPUTATION PIPELINE                              │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Real-time Features (computed at ranking time):                                  │
│  ├── post_age_hours                                                              │
│  ├── user_session_context                                                        │
│  └── current_time_features                                                       │
│                                                                                  │
│  Near-real-time Features (updated every few seconds via Kafka):                  │
│  ├── like_count, comment_count, save_count                                       │
│  ├── like_velocity (rolling 1-hour window)                                       │
│  └── engagement_rate                                                             │
│                                                                                  │
│  Batch Features (updated hourly via Spark):                                      │
│  ├── author_avg_engagement                                                       │
│  ├── user_author_affinity                                                        │
│  ├── user_interest_vectors                                                       │
│  └── content_embeddings                                                          │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

**Affinity Score Calculation (Batch Job):**

```python
def compute_affinity(user_id, followee_id, window_days=30):
    """
    Run as Spark job, results stored in Feature Store
    """
    interactions = get_interactions(user_id, followee_id, days=window_days)

    # Weighted interaction score
    score = (
        interactions.profile_visits * 0.1 +
        interactions.likes * 0.3 +
        interactions.comments * 0.4 +
        interactions.saves * 0.2 +
        interactions.dms * 0.5 +
        interactions.time_spent_seconds / 3600 * 0.3  # hours of viewing
    )

    # Normalize to 0-1
    return min(score / 100, 1.0)
```

---

## 8. Media Handling

### 8.1 Upload Flow

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Client     │────▶│  API Gateway │────▶│Media Service │────▶│ S3 (temp)    │
│  (multipart) │     │  (auth)      │     │ (presigned)  │     │              │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
                                                                       │
                                                                       ▼
                                                         ┌─────────────────────────┐
                                                         │ Media Processing Queue  │
                                                         │ (Kafka: media.uploaded) │
                                                         └─────────────────────────┘
                                                                       │
                              ┌────────────────────────────────────────┼────────────┐
                              ▼                                        ▼            ▼
                    ┌──────────────────┐                    ┌────────────┐  ┌────────────┐
                    │ Image Processor  │                    │ Video      │  │ Thumbnail  │
                    │ - Resize         │                    │ Transcoder │  │ Generator  │
                    │ - Compress       │                    │ (H.264)    │  │            │
                    │ - Format convert │                    └────────────┘  └────────────┘
                    └──────────────────┘
                              │
                              ▼
                    ┌──────────────────┐     ┌──────────────┐
                    │ S3 (permanent)   │────▶│ CDN origin   │
                    │ Multiple sizes   │     │ invalidation │
                    └──────────────────┘     └──────────────┘
```

### 8.2 Image Processing

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        IMAGE PROCESSING VARIANTS                                 │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Generated Sizes:                                                                │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  thumbnail    150x150   WebP   ~10KB   (grid view, notifications)          │ │
│  │  small        320x320   WebP   ~30KB   (feed preview, low bandwidth)       │ │
│  │  medium       640x640   WebP   ~80KB   (feed default)                      │ │
│  │  large        1080x1080 WebP   ~200KB  (feed HD, tap to view)              │ │
│  │  original     max 2048  JPEG   ~500KB  (full resolution)                   │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  S3 Path Structure:                                                              │
│  s3://ig-media/{user_id_prefix}/{user_id}/{post_id}/{size}.webp                  │
│  Example: s3://ig-media/a1/a1b2c3d4/p5f6g7h8/medium.webp                         │
│                                                                                  │
│  CDN URL:                                                                        │
│  https://cdn.instagram.com/v/{post_id}/{size}.webp                               │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 8.3 Video Transcoding

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        VIDEO TRANSCODING VARIANTS                                │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Quality Levels (Adaptive Bitrate):                                              │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  240p    426x240    H.264   300kbps   (2G/Edge)                             │ │
│  │  360p    640x360    H.264   800kbps   (3G)                                  │ │
│  │  480p    854x480    H.264   1.5Mbps   (LTE weak)                            │ │
│  │  720p    1280x720   H.264   3Mbps     (LTE/WiFi default)                    │ │
│  │  1080p   1920x1080  H.264   6Mbps     (WiFi HD)                             │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  Streaming Format: HLS (HTTP Live Streaming)                                     │
│  - Playlist: master.m3u8 (lists all quality levels)                              │
│  - Segments: 2-second chunks for quick quality switching                         │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 8.4 CDN Integration

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              CDN ARCHITECTURE                                    │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│                           ┌───────────────────┐                                  │
│                           │  DNS (Route 53)   │                                  │
│                           │  Latency routing  │                                  │
│                           └───────────────────┘                                  │
│                                    │                                             │
│            ┌───────────────────────┼───────────────────────┐                     │
│            ▼                       ▼                       ▼                     │
│    ┌─────────────┐         ┌─────────────┐         ┌─────────────┐              │
│    │ Edge POP    │         │ Edge POP    │         │ Edge POP    │              │
│    │ US-East     │         │ EU-West     │         │ AP-South    │              │
│    │             │         │             │         │             │              │
│    │ Cache:      │         │ Cache:      │         │ Cache:      │              │
│    │ - 95% hits  │         │ - 95% hits  │         │ - 90% hits  │              │
│    │ - 100TB SSD │         │ - 100TB SSD │         │ - 50TB SSD  │              │
│    └─────────────┘         └─────────────┘         └─────────────┘              │
│            │                       │                       │                     │
│            └───────────────────────┼───────────────────────┘                     │
│                                    ▼                                             │
│                         ┌───────────────────┐                                    │
│                         │   Origin Shield   │                                    │
│                         │   (CloudFront)    │                                    │
│                         └───────────────────┘                                    │
│                                    │                                             │
│                                    ▼                                             │
│                         ┌───────────────────┐                                    │
│                         │      S3 Origin    │                                    │
│                         │   (Multi-region)  │                                    │
│                         └───────────────────┘                                    │
│                                                                                  │
│  Cache Strategy:                                                                 │
│  - Images: Cache-Control: public, max-age=31536000 (1 year, immutable)           │
│  - Videos: Cache-Control: public, max-age=86400 (1 day)                          │
│  - Cache key: URL path only (no query params for static media)                   │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 9. Scalability Strategy

### 9.1 Horizontal Scaling

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        SERVICE SCALING STRATEGY                                  │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Service          │ Scaling Trigger         │ Scale Unit │ Max Instances        │
│  ─────────────────┼─────────────────────────┼────────────┼───────────────────── │
│  Feed Service     │ CPU > 60%               │ 10 pods    │ 500                  │
│  Post Service     │ CPU > 70%               │ 5 pods     │ 200                  │
│  Ranking Service  │ CPU > 50% (ML heavy)    │ 10 pods    │ 300                  │
│  Fan-out Workers  │ Kafka lag > 10k msgs    │ 20 pods    │ 1000                 │
│  Media Service    │ Queue depth > 1000      │ 10 pods    │ 200                  │
│                                                                                  │
│  Auto-scaling: Kubernetes HPA + Custom metrics (Kafka lag, Redis latency)        │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 9.2 Hot User Mitigation

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         HOT USER HANDLING                                        │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Problem: Celebrity with 100M followers posts → 100M feed updates needed         │
│                                                                                  │
│  Solution 1: Don't fan-out for celebrities (implemented)                         │
│  - Posts from users with >10K followers stored separately                        │
│  - Pulled at read time, merged into feed                                         │
│                                                                                  │
│  Solution 2: Rate-limited fan-out for borderline users (10K-100K followers)      │
│  - Fan-out at 100K updates/second max                                            │
│  - Complete fan-out within 10 minutes                                            │
│  - Acceptable delay for these users                                              │
│                                                                                  │
│  Solution 3: Feed cache partitioning                                             │
│  - Hot users' feeds distributed across more Redis nodes                          │
│  - Consistent hashing with virtual nodes                                         │
│  - Detect hot keys, add replicas dynamically                                     │
│                                                                                  │
│  Hot Key Detection:                                                              │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  1. Redis MONITOR samples (1% of commands)                                 │ │
│  │  2. Detect keys with >1000 ops/second                                      │ │
│  │  3. Add to "hot_keys" set                                                  │ │
│  │  4. Route hot keys to dedicated replica set                                │ │
│  │  5. TTL on hot_keys: 1 hour (auto-remove if traffic drops)                 │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 9.3 Feed Cache Invalidation

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                      CACHE INVALIDATION STRATEGY                                 │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Scenario: Post deleted → Remove from all followers' feeds                       │
│                                                                                  │
│  Approach: Lazy invalidation (don't proactively remove)                          │
│                                                                                  │
│  1. Mark post as deleted in posts_by_id table                                    │
│  2. When feed is fetched:                                                        │
│     a. Get post IDs from cache                                                   │
│     b. Batch fetch post metadata                                                 │
│     c. Filter out deleted posts                                                  │
│     d. Async: LREM to remove deleted post IDs from cache (best effort)           │
│                                                                                  │
│  Why lazy?                                                                       │
│  - Deletes are rare (<0.1% of posts)                                             │
│  - Proactive invalidation would require N writes (one per follower)              │
│  - Filter at read time is cheap (batch lookup anyway)                            │
│                                                                                  │
│  Code:                                                                           │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  async def get_feed_posts(user_id, post_ids):                              │ │
│  │      posts = await post_store.batch_get(post_ids)                          │ │
│  │      valid_posts = [p for p in posts if not p.is_deleted]                  │ │
│  │                                                                            │ │
│  │      # Async cleanup (fire and forget)                                     │ │
│  │      deleted_ids = [p.id for p in posts if p.is_deleted]                   │ │
│  │      if deleted_ids:                                                       │ │
│  │          asyncio.create_task(                                              │ │
│  │              cleanup_deleted_from_cache(user_id, deleted_ids)              │ │
│  │          )                                                                 │ │
│  │                                                                            │ │
│  │      return valid_posts                                                    │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 10. Failure Handling

### 10.1 Fan-out Backpressure

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        FAN-OUT BACKPRESSURE                                      │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Scenario: Viral post causes fan-out queue to back up                            │
│                                                                                  │
│  Detection:                                                                      │
│  - Monitor Kafka consumer lag per partition                                      │
│  - Alert threshold: lag > 100,000 messages                                       │
│  - Critical threshold: lag > 1,000,000 messages                                  │
│                                                                                  │
│  Response:                                                                       │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  Level 1 (lag > 100K):                                                     │ │
│  │    - Auto-scale fan-out workers (2x current)                               │ │
│  │    - Alert on-call                                                         │ │
│  │                                                                            │ │
│  │  Level 2 (lag > 500K):                                                     │ │
│  │    - Prioritize active users (logged in within 24h)                        │ │
│  │    - Skip inactive users (fan-out on next login)                           │ │
│  │    - Scale workers to max (1000 pods)                                      │ │
│  │                                                                            │ │
│  │  Level 3 (lag > 1M):                                                       │ │
│  │    - Enable "pull mode" for all users temporarily                          │ │
│  │    - Stop fan-out writes                                                   │ │
│  │    - Switch feed service to pull-based generation                          │ │
│  │    - Resume normal operation when lag < 100K                               │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  Active User Tracking:                                                           │
│  Key: active_users (Redis HyperLogLog, updated on login)                         │
│  Key: last_active:{user_id} (TTL 24 hours)                                       │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 10.2 Retry Mechanisms

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          RETRY CONFIGURATION                                     │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Operation              │ Retries │ Backoff           │ Timeout │ Fallback      │
│  ───────────────────────┼─────────┼───────────────────┼─────────┼────────────── │
│  Redis feed read        │ 2       │ Fixed 10ms        │ 50ms    │ Pull from DB  │
│  Redis feed write       │ 3       │ Exponential 50ms  │ 500ms   │ DLQ           │
│  Cassandra post read    │ 2       │ Fixed 50ms        │ 200ms   │ Cache only    │
│  Cassandra post write   │ 3       │ Exponential 100ms │ 2s      │ DLQ           │
│  Ranking service        │ 1       │ None              │ 100ms   │ Recency sort  │
│  Media upload (S3)      │ 3       │ Exponential 1s    │ 30s     │ Client retry  │
│  Kafka produce          │ 5       │ Exponential 100ms │ 30s     │ Local buffer  │
│                                                                                  │
│  DLQ = Dead Letter Queue (manual review)                                         │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 10.3 Graceful Degradation

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                       GRACEFUL DEGRADATION MATRIX                                │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Component Down         │ Fallback Behavior                                      │
│  ───────────────────────┼───────────────────────────────────────────────────── │
│  Redis feed cache       │ Generate feed on-the-fly from followee posts          │
│                         │ (slower, ~500ms, but functional)                       │
│                                                                                  │
│  Ranking service        │ Sort by recency only (no ML ranking)                   │
│                         │ Add header: X-Feed-Quality: degraded                   │
│                                                                                  │
│  Feature store          │ Use cached/stale features (up to 1 hour old)           │
│                         │ Fall back to basic engagement counts from post metadata│
│                                                                                  │
│  Recommendation svc     │ Skip recommendations, show only followed content       │
│                         │ Reduce feed diversity, but functional                  │
│                                                                                  │
│  Social graph           │ Use cached followee list (up to 24h stale)             │
│                         │ New follows won't appear until recovery                │
│                                                                                  │
│  CDN                    │ Serve directly from S3 origin (slower)                 │
│                         │ May hit S3 rate limits, show placeholder images        │
│                                                                                  │
│  One region down        │ Failover to nearest region                             │
│                         │ Slight latency increase (50-100ms)                     │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 10.4 Circuit Breakers

```python
# Example using resilience4j-style circuit breaker
from circuitbreaker import circuit

@circuit(
    failure_threshold=5,
    recovery_timeout=30,
    expected_exception=TimeoutError
)
async def get_ranking_scores(candidates, user_context):
    return await ranking_service.rank(candidates, user_context)


async def get_feed_with_fallback(user_id, candidates):
    try:
        ranked = await get_ranking_scores(candidates, user_context)
    except CircuitBreakerError:
        # Fallback to recency sort
        ranked = sorted(candidates, key=lambda p: p.created_at, reverse=True)
        metrics.increment("feed.ranking.fallback")

    return ranked
```

---

## 11. API Design

### 11.1 Feed API

```http
GET /v1/feed?cursor={cursor}&limit=20

Headers:
  Authorization: Bearer {jwt_token}
  X-Device-Type: ios|android|web
  X-Network-Quality: wifi|lte|3g

Response 200:
{
  "posts": [
    {
      "post_id": "abc123",
      "author": {
        "user_id": "user456",
        "username": "johndoe",
        "avatar_url": "https://cdn.instagram.com/u/user456/avatar.jpg",
        "is_verified": true
      },
      "media": [
        {
          "media_id": "m789",
          "type": "image",
          "urls": {
            "thumbnail": "https://cdn.instagram.com/v/m789/thumbnail.webp",
            "medium": "https://cdn.instagram.com/v/m789/medium.webp",
            "large": "https://cdn.instagram.com/v/m789/large.webp"
          },
          "dimensions": {"width": 1080, "height": 1350}
        }
      ],
      "caption": "Beautiful sunset! #photography #nature",
      "hashtags": ["photography", "nature"],
      "location": {"name": "San Francisco, CA", "id": "loc123"},
      "engagement": {
        "like_count": 1523,
        "comment_count": 89,
        "is_liked": false,
        "is_saved": false
      },
      "created_at": "2024-01-15T18:30:00Z"
    }
  ],
  "next_cursor": "eyJvZmZzZXQiOjIwfQ==",
  "has_more": true,
  "feed_session_id": "fs_abc123"
}

Response Headers:
  X-Feed-Quality: normal|degraded
  X-Cache-Status: hit|miss|stale
```

### 11.2 Create Post API

```http
POST /v1/posts

Headers:
  Authorization: Bearer {jwt_token}
  Content-Type: application/json

Request:
{
  "caption": "My new post!",
  "media_ids": ["upload_123", "upload_456"],
  "location_id": "loc789",
  "hashtags": ["travel", "adventure"]
}

Response 201:
{
  "post_id": "post_abc123",
  "status": "processing",
  "estimated_ready_at": "2024-01-15T18:30:30Z"
}

Response 202 (async processing):
{
  "post_id": "post_abc123",
  "status": "media_processing",
  "webhook_url": "/v1/posts/post_abc123/status"
}
```

### 11.3 Interaction APIs

```http
# Like a post
POST /v1/posts/{post_id}/like
Response 200: {"liked": true, "like_count": 1524}

# Unlike a post
DELETE /v1/posts/{post_id}/like
Response 200: {"liked": false, "like_count": 1523}

# Save a post
POST /v1/posts/{post_id}/save
Response 200: {"saved": true}

# Add comment
POST /v1/posts/{post_id}/comments
Request: {"text": "Great photo!"}
Response 201: {
  "comment_id": "c123",
  "text": "Great photo!",
  "created_at": "2024-01-15T18:35:00Z"
}
```

### 11.4 Media Upload API

```http
# Step 1: Request upload URL
POST /v1/media/upload-url
Request: {
  "content_type": "image/jpeg",
  "file_size": 2048576
}
Response 200: {
  "upload_id": "upload_123",
  "upload_url": "https://s3.amazonaws.com/ig-uploads/...",
  "expires_at": "2024-01-15T19:00:00Z"
}

# Step 2: Upload to S3 (client direct upload)
PUT {upload_url}
Headers: Content-Type: image/jpeg
Body: [binary image data]

# Step 3: Confirm upload
POST /v1/media/upload-url/upload_123/complete
Response 200: {
  "media_id": "m456",
  "status": "processing",
  "preview_url": "https://cdn.instagram.com/v/m456/thumbnail.webp"
}
```

---

## 12. Multi-Region Architecture

### 12.1 Global Topology

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          MULTI-REGION DEPLOYMENT                                 │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│                           ┌───────────────────┐                                  │
│                           │   Global DNS      │                                  │
│                           │  (Route 53)       │                                  │
│                           │  Latency-based    │                                  │
│                           └───────────────────┘                                  │
│                                    │                                             │
│         ┌──────────────────────────┼──────────────────────────┐                  │
│         ▼                          ▼                          ▼                  │
│  ┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐         │
│  │   US-WEST-2     │       │   EU-WEST-1     │       │  AP-SOUTHEAST-1 │         │
│  │   (Primary)     │       │   (Secondary)   │       │   (Secondary)   │         │
│  │                 │       │                 │       │                 │         │
│  │ ┌─────────────┐ │       │ ┌─────────────┐ │       │ ┌─────────────┐ │         │
│  │ │ Feed Svc    │ │       │ │ Feed Svc    │ │       │ │ Feed Svc    │ │         │
│  │ │ Post Svc    │ │       │ │ Post Svc    │ │       │ │ Post Svc    │ │         │
│  │ │ Ranking Svc │ │       │ │ Ranking Svc │ │       │ │ Ranking Svc │ │         │
│  │ └─────────────┘ │       │ └─────────────┘ │       │ └─────────────┘ │         │
│  │                 │       │                 │       │                 │         │
│  │ ┌─────────────┐ │       │ ┌─────────────┐ │       │ ┌─────────────┐ │         │
│  │ │ Redis       │◄├───────├─┤ Redis       │◄├───────├─┤ Redis       │ │         │
│  │ │ (Primary)   │ │ CRDT  │ │ (Replica)   │ │ CRDT  │ │ (Replica)   │ │         │
│  │ └─────────────┘ │       │ └─────────────┘ │       │ └─────────────┘ │         │
│  │                 │       │                 │       │                 │         │
│  │ ┌─────────────┐ │       │ ┌─────────────┐ │       │ ┌─────────────┐ │         │
│  │ │ Cassandra   │◄├───────├─┤ Cassandra   │◄├───────├─┤ Cassandra   │ │         │
│  │ │ (DC: us)    │ │ Multi │ │ (DC: eu)    │ │  DC   │ │ (DC: ap)    │ │         │
│  │ └─────────────┘ │       │ └─────────────┘ │       │ └─────────────┘ │         │
│  │                 │       │                 │       │                 │         │
│  │ ┌─────────────┐ │       │ ┌─────────────┐ │       │ ┌─────────────┐ │         │
│  │ │ Kafka       │◄├───────├─┤ Kafka       │◄├───────├─┤ Kafka       │ │         │
│  │ │ (Primary)   │ │Mirror │ │ (Replica)   │ │Maker  │ │ (Replica)   │ │         │
│  │ └─────────────┘ │       │ └─────────────┘ │       │ └─────────────┘ │         │
│  └─────────────────┘       └─────────────────┘       └─────────────────┘         │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 12.2 Data Replication

| Data Store | Replication Strategy | Lag Target | Consistency |
|------------|---------------------|------------|-------------|
| Cassandra | Multi-DC async | < 100ms | LOCAL_QUORUM reads/writes |
| Redis Feed Cache | Active-Active (CRDT lists) | < 50ms | Eventual |
| Redis Feature Store | Active-Active (CRDT counters) | < 50ms | Eventual |
| Kafka | MirrorMaker 2 | < 1s | Eventual |
| MySQL (Social Graph) | GTID-based async replication | < 1s | Read-your-writes (session) |
| S3 (Media) | Cross-Region Replication | < 15 min | Eventual |

### 12.3 Write Routing

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         WRITE ROUTING STRATEGY                                   │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Post Creation:                                                                  │
│  - Write to local Cassandra DC (LOCAL_QUORUM)                                    │
│  - Publish to local Kafka (async replication to other regions)                   │
│  - Fan-out happens in all regions independently                                  │
│                                                                                  │
│  Interactions (likes, comments):                                                 │
│  - Write to local region                                                         │
│  - Counters use CRDT (conflict-free merge)                                       │
│  - Eventual consistency acceptable (users see their own action immediately)      │
│                                                                                  │
│  Social Graph Changes:                                                           │
│  - Route to primary region (US-WEST-2)                                           │
│  - Replicate to other regions                                                    │
│  - New follows may take 1-2 seconds to reflect in other regions                  │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 13. Monitoring & Observability

### 13.1 Key Metrics

| Metric | Target | Alert Threshold | Critical |
|--------|--------|-----------------|----------|
| Feed P50 latency | < 100ms | > 150ms | > 300ms |
| Feed P99 latency | < 200ms | > 300ms | > 500ms |
| Post creation latency | < 500ms | > 1s | > 3s |
| Fan-out lag | < 30s | > 1 min | > 5 min |
| Redis cache hit rate | > 95% | < 90% | < 80% |
| CDN cache hit rate | > 95% | < 90% | < 85% |
| Error rate (5xx) | < 0.01% | > 0.1% | > 1% |
| Kafka consumer lag | < 10K | > 100K | > 1M |

### 13.2 Distributed Tracing

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    EXAMPLE TRACE: GET /v1/feed                                   │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  [API Gateway] ──────────────────────────────────────────────────────▶ 150ms    │
│       │                                                                          │
│       ├─[Auth] ────────────────────▶ 5ms                                         │
│       │                                                                          │
│       └─[Feed Service] ──────────────────────────────────────────────▶ 140ms    │
│              │                                                                   │
│              ├─[Redis: Get feed cache] ──────▶ 8ms                               │
│              │                                                                   │
│              ├─[Redis: Get celebrity followees] ──────▶ 3ms                      │
│              │                                                                   │
│              ├─[Redis: Get celebrity posts] (parallel) ──────▶ 15ms              │
│              │    ├─ celeb_1: 12ms                                               │
│              │    ├─ celeb_2: 15ms                                               │
│              │    └─ celeb_3: 10ms                                               │
│              │                                                                   │
│              ├─[Cassandra: Batch get posts] ──────▶ 25ms                         │
│              │                                                                   │
│              ├─[Ranking Service] ──────────────────▶ 45ms                        │
│              │    ├─ Feature fetch: 20ms                                         │
│              │    └─ ML scoring: 25ms                                            │
│              │                                                                   │
│              └─[Recommendation Service] ──────▶ 30ms                             │
│                                                                                  │
│  Total: 150ms (P50 target: <100ms, P99 target: <200ms)                           │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 14. Trade-offs & Decisions

### 14.1 Key Decisions

| Decision | Chose | Over | Rationale |
|----------|-------|------|-----------|
| Feed storage | Redis Lists | Cassandra | Sub-ms reads, simple pagination, fits in memory |
| Post storage | Cassandra | PostgreSQL | Better write throughput, native time-series, linear scale |
| Fan-out strategy | Hybrid | Pure push/pull | Balances write cost (celebrities) with read latency (regular users) |
| Ranking | Two-stage | Single ML model | Latency budget: can't run heavy ML on 1000 candidates |
| Consistency | Eventual | Strong | 99.99% availability more important than perfect consistency |
| Media delivery | CDN + S3 | Self-hosted | Cost-effective at scale, global coverage, no ops burden |
| Message queue | Kafka | SQS/RabbitMQ | Replayable, high throughput, multi-consumer |

### 14.2 What We're Sacrificing

| Sacrifice | Impact | Mitigation |
|-----------|--------|------------|
| Strong consistency | User may not see own post in feed for ~30s | Show "Your post" banner at top |
| Celebrity post freshness | Celebrity posts may be up to 5 min delayed | Aggressive caching of celebrity posts |
| Perfect feed ordering | Posts may appear out of order across sessions | Consistent feed session ID |
| Real-time counters | Like counts may lag by seconds | Optimistic UI update on client |

### 14.3 Async vs Sync

| Operation | Mode | Why |
|-----------|------|-----|
| Post creation (metadata) | Sync | User needs confirmation |
| Media processing | Async | Can take 5-30 seconds |
| Fan-out | Async | Can take minutes for popular users |
| Ranking | Sync | Must complete for feed response |
| Counter updates | Async | High volume, eventual consistency OK |
| Affinity score calculation | Batch | Expensive, updates hourly |

---

## 15. Summary

### System at a Glance

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                      INSTAGRAM FEED SYSTEM SUMMARY                               │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Scale: 500M DAU, 100M posts/day, 5B feed reads/day                              │
│  Latency: P99 < 200ms for feed fetch                                             │
│  Availability: 99.99% (multi-region active-active)                               │
│                                                                                  │
│  Core Architecture:                                                              │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  Fan-out: Hybrid (push for <10K followers, pull for celebrities)           │ │
│  │  Feed cache: Redis Lists (500 posts per user, 2TB total)                   │ │
│  │  Post store: Cassandra (100M posts/day, multi-DC)                          │ │
│  │  Ranking: Two-stage (quick filter → ML ranking)                            │ │
│  │  Media: S3 + CloudFront CDN (95%+ cache hit rate)                          │ │
│  │  Events: Kafka (posts, interactions, fan-out)                              │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  Key Trade-offs:                                                                 │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  ✓ Fast reads (pre-computed feeds) over fast writes (fan-out cost)         │ │
│  │  ✓ Availability over consistency (eventual consistency for feeds)          │ │
│  │  ✓ Simplicity over perfection (lazy invalidation, CRDT counters)           │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```
