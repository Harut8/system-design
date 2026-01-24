# Twitter-Scale Real-Time Search: Design Document

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|----------|----------|--------|
| **Scale** | Daily tweet volume? | 100k/day (start small, design for scale) |
| **Scale** | Read:Write ratio? | 2:3 (more reads than writes) |
| **Scale** | Geographic distribution? | Globally distributed users |
| **Search** | Lookback window? | 7 days |
| **Search** | Boolean operators? | AND only |
| **Search** | Average query length? | ~10 characters |
| **Trending** | Time window? | 1 hour sliding window |
| **Autocomplete** | Suggestions count? | 3 suggestions |
| **Data Lifecycle** | Deletion strategy? | Soft delete |
| **Ranking** | Available signals? | Likes, Views, Retweets (feature store) |

---

## 2. Capacity Estimates

### Current Scale (100k tweets/day)

```
Tweets/day:        100,000
Tweets/second:     ~1.2 TPS (peak ~5 TPS)
Reads/second:      ~1.7 QPS (2:3 ratio means 1.5x reads)

Tweet size (avg):  300 bytes (text + metadata)
Daily storage:     100k × 300B = 30 MB/day
7-day index:       210 MB (active index)

Index overhead:    ~3x for inverted index + metadata
Total index size:  ~630 MB (fits in memory easily)
```

### Scale Target (Twitter-like: 500M tweets/day)

```
Tweets/day:        500,000,000
Tweets/second:     ~5,800 TPS (peak ~20,000 TPS)
Reads/second:      ~8,700 QPS baseline, peaks to 50,000+ QPS

Daily storage:     500M × 300B = 150 GB/day
7-day index:       ~1 TB raw data
Total index size:  ~3 TB (with index overhead)

Shards needed:     30-50 shards (50-100 GB per shard)
Replicas:          3 per shard (read scaling + HA)
```

---

## 3. Architecture Comparison

| Aspect | Simple Monolith | Distributed (Kafka + ES) |
|--------|-----------------|--------------------------|
| **Best for** | < 1M tweets/day | > 10M tweets/day |
| **Complexity** | Low | High |
| **Team size** | 1-3 engineers | 5+ engineers |
| **Ops overhead** | Minimal | Significant |
| **Latency** | < 50ms | < 200ms |
| **Cost/month** | ~$200-500 | ~$5,000+ |
| **Time to build** | 1-2 weeks | 2-3 months |

**Recommendation:** Start with Simple Monolith, migrate when you hit limits.

---

## 4. Simple Monolith Architecture (PostgreSQL + Redis)

### 4.1 Overview

For 100k tweets/day, a monolith with PostgreSQL full-text search and Redis caching is sufficient, simpler to operate, and cheaper.

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Load Balancer (nginx)                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                                                                  │
│                    Monolith Application                          │
│                    (Node.js / Python / Go)                       │
│                                                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │ Search API  │  │ Trending API│  │ Autocomplete API        │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │              Background Workers (in-process)                 ││
│  │  - Trending aggregator (cron every 30s)                      ││
│  │  - Autocomplete rebuilder (cron every 5min)                  ││
│  │  - Cleanup job (cron nightly)                                ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
          │                                       │
          ▼                                       ▼
┌─────────────────────┐               ┌─────────────────────┐
│     PostgreSQL      │               │       Redis         │
│                     │               │                     │
│  - tweets table     │               │  - Search cache     │
│  - Full-text index  │               │  - Trending sets    │
│  - GIN index        │               │  - Autocomplete     │
│  - Engagement cols  │               │  - Feature store    │
└─────────────────────┘               └─────────────────────┘
```

### 4.2 PostgreSQL Schema

```sql
-- Main tweets table with full-text search
CREATE TABLE tweets (
    id              BIGSERIAL PRIMARY KEY,
    tweet_id        VARCHAR(32) UNIQUE NOT NULL,
    user_id         VARCHAR(32) NOT NULL,
    text            TEXT NOT NULL,
    hashtags        TEXT[] DEFAULT '{}',
    language        VARCHAR(5),
    geo_lat         DECIMAL(10, 8),
    geo_lon         DECIMAL(11, 8),

    -- Engagement (denormalized for speed)
    likes           INTEGER DEFAULT 0,
    retweets        INTEGER DEFAULT 0,
    views           BIGINT DEFAULT 0,

    -- Soft delete
    is_deleted      BOOLEAN DEFAULT FALSE,
    deleted_at      TIMESTAMPTZ,

    -- Timestamps
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),

    -- Full-text search vector (auto-updated)
    search_vector   TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', text), 'A') ||
        setweight(to_tsvector('simple', array_to_string(hashtags, ' ')), 'B')
    ) STORED
);

-- Indexes
CREATE INDEX idx_tweets_search ON tweets USING GIN(search_vector);
CREATE INDEX idx_tweets_created ON tweets(created_at DESC) WHERE NOT is_deleted;
CREATE INDEX idx_tweets_user ON tweets(user_id, created_at DESC) WHERE NOT is_deleted;
CREATE INDEX idx_tweets_hashtags ON tweets USING GIN(hashtags) WHERE NOT is_deleted;
CREATE INDEX idx_tweets_geo ON tweets USING GIST(
    ll_to_earth(geo_lat, geo_lon)
) WHERE geo_lat IS NOT NULL AND NOT is_deleted;

-- Partial index for 7-day window (much smaller, faster)
CREATE INDEX idx_tweets_recent ON tweets(created_at DESC)
    WHERE created_at > NOW() - INTERVAL '7 days' AND NOT is_deleted;
```

### 4.3 Search Query (PostgreSQL Full-Text)

```sql
-- Search with AND logic, ranking by recency + engagement
SELECT
    tweet_id,
    user_id,
    text,
    hashtags,
    likes,
    retweets,
    views,
    created_at,
    -- Ranking score
    (
        ts_rank(search_vector, query) * 0.3 +
        (1.0 / (1 + EXTRACT(EPOCH FROM NOW() - created_at) / 21600)) * 0.4 +
        (LN(1 + likes) * 0.02 + LN(1 + retweets) * 0.02 + LN(1 + views/1000) * 0.01) * 0.3
    ) AS score
FROM tweets,
     plainto_tsquery('english', 'keyword1 keyword2') AS query  -- AND by default
WHERE
    search_vector @@ query
    AND NOT is_deleted
    AND created_at > NOW() - INTERVAL '7 days'
ORDER BY score DESC
LIMIT 20;
```

**Performance at 100k/day:**
- 7-day index size: ~50MB (fits in RAM)
- Query time: 5-20ms with proper indexes
- No need for Elasticsearch at this scale

### 4.4 Trending Topics (PostgreSQL + Redis)

```sql
-- Materialized view refreshed every 30 seconds
CREATE MATERIALIZED VIEW trending_hashtags AS
SELECT
    unnest(hashtags) AS hashtag,
    COUNT(*) AS tweet_count,
    'global' AS region
FROM tweets
WHERE
    created_at > NOW() - INTERVAL '1 hour'
    AND NOT is_deleted
GROUP BY unnest(hashtags)
ORDER BY tweet_count DESC
LIMIT 100;

-- Add regional (assuming region column exists)
-- UNION ALL with WHERE region = 'us', etc.

CREATE UNIQUE INDEX idx_trending_hashtag ON trending_hashtags(hashtag, region);
```

**Background worker (every 30s):**
```python
def refresh_trending():
    # Refresh materialized view
    db.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY trending_hashtags")

    # Cache in Redis for fast reads
    trends = db.execute("SELECT * FROM trending_hashtags WHERE region = 'global' LIMIT 10")

    redis.delete("trending:global")
    for trend in trends:
        redis.zadd("trending:global", {trend.hashtag: trend.tweet_count})
    redis.expire("trending:global", 60)
```

### 4.5 Autocomplete (Redis)

```python
def build_autocomplete():
    """Runs every 5 minutes"""

    # Get sources
    trending = redis.zrevrange("trending:global", 0, 50, withscores=True)

    popular_queries = db.execute("""
        SELECT query, COUNT(*) as cnt
        FROM search_logs
        WHERE created_at > NOW() - INTERVAL '24 hours'
        GROUP BY query
        ORDER BY cnt DESC
        LIMIT 200
    """)

    # Build prefix index
    terms = {}
    for hashtag, score in trending:
        terms[hashtag] = score
    for row in popular_queries:
        terms[row.query] = terms.get(row.query, 0) + row.cnt * 0.5

    # Write to Redis
    pipe = redis.pipeline()
    for term, score in terms.items():
        for i in range(1, min(len(term) + 1, 15)):
            prefix = term[:i].lower()
            pipe.zadd(f"ac:{prefix}", {term: score})
            pipe.expire(f"ac:{prefix}", 600)
    pipe.execute()


def get_autocomplete(prefix: str, limit: int = 3):
    """O(1) lookup"""
    return redis.zrevrange(f"ac:{prefix.lower()}", 0, limit - 1)
```

### 4.6 Feature Store (Direct in PostgreSQL)

At 100k/day, no need for separate feature store. Store engagement directly in tweets table:

```python
def increment_like(tweet_id: str):
    # Direct update, simple and transactional
    db.execute("""
        UPDATE tweets
        SET likes = likes + 1, updated_at = NOW()
        WHERE tweet_id = %s
    """, [tweet_id])

    # Invalidate search cache for this tweet (if cached)
    redis.delete(f"tweet:{tweet_id}")


def increment_view(tweet_id: str):
    # Batch views to reduce write load
    # Use Redis as buffer, flush every 10 seconds
    redis.hincrby(f"views_buffer:{tweet_id}", "count", 1)


def flush_views():
    """Runs every 10 seconds"""
    keys = redis.scan_iter("views_buffer:*")
    for key in keys:
        tweet_id = key.split(":")[1]
        count = redis.hget(key, "count")
        if count:
            db.execute("""
                UPDATE tweets
                SET views = views + %s
                WHERE tweet_id = %s
            """, [int(count), tweet_id])
            redis.delete(key)
```

### 4.7 Soft Delete (Simple)

```python
def delete_tweet(tweet_id: str):
    # Single transaction, immediate
    db.execute("""
        UPDATE tweets
        SET is_deleted = TRUE, deleted_at = NOW()
        WHERE tweet_id = %s
    """, [tweet_id])

    # Invalidate caches
    redis.delete(f"tweet:{tweet_id}")

    return {"status": "deleted"}


def cleanup_deleted_tweets():
    """Nightly cron job"""
    db.execute("""
        DELETE FROM tweets
        WHERE is_deleted = TRUE
        AND deleted_at < NOW() - INTERVAL '24 hours'
    """)

    # Vacuum to reclaim space
    db.execute("VACUUM ANALYZE tweets")
```

### 4.8 Caching Strategy

```
┌─────────────────────────────────────────────────────────────────┐
│                      Redis Cache Layers                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. Search Results Cache                                        │
│     Key: search:{hash(query+filters)}                           │
│     TTL: 30 seconds                                             │
│     Hit rate: ~60-80% (many repeat queries)                     │
│                                                                 │
│  2. Trending Cache                                              │
│     Key: trending:{region}                                      │
│     TTL: 60 seconds                                             │
│     Hit rate: ~99% (rarely changes)                             │
│                                                                 │
│  3. Autocomplete Cache                                          │
│     Key: ac:{prefix}                                            │
│     TTL: 10 minutes                                             │
│     Hit rate: ~95%                                              │
│                                                                 │
│  4. Individual Tweet Cache (optional)                           │
│     Key: tweet:{id}                                             │
│     TTL: 5 minutes                                              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 4.9 API Implementation (Python/FastAPI Example)

```python
from fastapi import FastAPI, Query
from typing import Optional
import asyncpg
import redis.asyncio as redis

app = FastAPI()
db_pool = None
redis_client = None

@app.get("/v1/search")
async def search(
    q: str,
    user_id: Optional[str] = None,
    lang: Optional[str] = None,
    limit: int = Query(20, le=100)
):
    # Check cache first
    cache_key = f"search:{hash(f'{q}:{user_id}:{lang}')}"
    cached = await redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    # Build query
    sql = """
        SELECT tweet_id, user_id, text, hashtags, likes, retweets, views, created_at,
               ts_rank(search_vector, query) AS relevance
        FROM tweets, plainto_tsquery('english', $1) AS query
        WHERE search_vector @@ query
          AND NOT is_deleted
          AND created_at > NOW() - INTERVAL '7 days'
    """
    params = [q]

    if user_id:
        sql += " AND user_id = $2"
        params.append(user_id)
    if lang:
        sql += f" AND language = ${len(params) + 1}"
        params.append(lang)

    sql += " ORDER BY relevance DESC, created_at DESC LIMIT $" + str(len(params) + 1)
    params.append(limit)

    rows = await db_pool.fetch(sql, *params)
    results = [dict(row) for row in rows]

    # Cache for 30 seconds
    await redis_client.setex(cache_key, 30, json.dumps(results, default=str))

    return {"results": results, "took_ms": ...}


@app.get("/v1/trending")
async def trending(region: str = "global", limit: int = 10):
    trends = await redis_client.zrevrange(f"trending:{region}", 0, limit - 1, withscores=True)
    return {
        "trends": [{"term": t[0], "tweet_count": int(t[1])} for t in trends],
        "region": region
    }


@app.get("/v1/autocomplete")
async def autocomplete(q: str, limit: int = 3):
    if len(q) < 1:
        return {"suggestions": []}

    suggestions = await redis_client.zrevrange(f"ac:{q.lower()}", 0, limit - 1, withscores=True)
    return {
        "suggestions": [{"term": s[0], "score": s[1]} for s in suggestions]
    }
```

### 4.10 Infrastructure (Simple)

```
Production Setup (~$300/month):

┌────────────────────────────────────────────────────────────┐
│  1x App Server (4 vCPU, 8GB RAM)           ~$80/month     │
│     - Monolith application                                │
│     - nginx reverse proxy                                 │
│     - Background workers (cron)                           │
├────────────────────────────────────────────────────────────┤
│  1x PostgreSQL (2 vCPU, 4GB RAM, 100GB SSD) ~$100/month   │
│     - Primary database                                    │
│     - Full-text search                                    │
│     - Or use managed: RDS, Cloud SQL                      │
├────────────────────────────────────────────────────────────┤
│  1x Redis (1 vCPU, 2GB RAM)                 ~$50/month    │
│     - Caching                                             │
│     - Trending/Autocomplete                               │
│     - Or use managed: ElastiCache, Memorystore            │
├────────────────────────────────────────────────────────────┤
│  Backups, monitoring, DNS                   ~$50/month    │
└────────────────────────────────────────────────────────────┘

Total: ~$280-350/month
```

### 4.11 When to Migrate to Distributed Architecture

Migrate when you hit these limits:

| Signal | Threshold | Action |
|--------|-----------|--------|
| PostgreSQL CPU | > 70% sustained | Add read replicas first |
| Query latency P99 | > 100ms | Consider Elasticsearch |
| Tweets/day | > 1M | Shard or move to ES |
| Write throughput | > 500 TPS | Add Kafka for buffering |
| Search complexity | Fuzzy, synonyms, ML | Elasticsearch required |
| Multi-region | Required | Full distributed setup |

### 4.12 Migration Path

```
Phase 1 (Current): Monolith
    └── PostgreSQL + Redis

Phase 2 (1M+ tweets/day): Add Search Engine
    └── PostgreSQL (source of truth)
    └── Elasticsearch (search only)
    └── Redis (cache)
    └── Simple sync job (pg → es)

Phase 3 (10M+ tweets/day): Add Event Streaming
    └── PostgreSQL
    └── Kafka (event bus)
    └── Elasticsearch
    └── Redis
    └── Flink (trending)

Phase 4 (100M+ tweets/day): Full Distributed
    └── Multi-region
    └── Sharded everything
    └── (The distributed architecture in section 5)
```

---

## 5. Distributed Architecture (For Scale: 10M+ tweets/day)

> **Note:** Only implement this when you outgrow the monolith (see section 4.11)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CLIENTS (Global)                                │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         CDN + Global Load Balancer                          │
│                    (CloudFlare / AWS Global Accelerator)                    │
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
│                         (Rate limiting, Auth, Routing)                       │
└─────────────────────────────────────────────────────────────────────────────┘
            │                         │                         │
            ▼                         ▼                         ▼
    ┌───────────────┐         ┌───────────────┐         ┌───────────────┐
    │ Search Service│         │Trending Service│        │Autocomplete   │
    │               │         │               │         │Service        │
    └───────────────┘         └───────────────┘         └───────────────┘
            │                         │                         │
            ▼                         ▼                         ▼
    ┌───────────────┐         ┌───────────────┐         ┌───────────────┐
    │ Search Index  │         │ Stream Proc.  │         │ Trie Cache    │
    │ (Elasticsearch)│        │ (Flink/Kafka) │         │ (Redis)       │
    └───────────────┘         └───────────────┘         └───────────────┘
            │                         │                         │
            └─────────────────────────┼─────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Data Ingestion Layer                               │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
            ┌─────────────────────────┼─────────────────────────┐
            ▼                         ▼                         ▼
    ┌───────────────┐         ┌───────────────┐         ┌───────────────┐
    │  Tweet Store  │         │ Feature Store │         │  Event Stream │
    │  (PostgreSQL) │         │   (Redis)     │         │   (Kafka)     │
    └───────────────┘         └───────────────┘         └───────────────┘
```

---

### 5.1 Core Components Design

#### 5.1.1 Tweet Ingestion Pipeline

```
┌──────────┐    ┌──────────┐    ┌──────────────┐    ┌─────────────┐
│  Tweet   │───▶│  Kafka   │───▶│  Indexer     │───▶│ Elasticsearch│
│  API     │    │  Topic   │    │  Consumer    │    │  Cluster    │
└──────────┘    └──────────┘    └──────────────┘    └─────────────┘
                     │                                      │
                     ▼                                      │
              ┌──────────────┐                              │
              │ Trending     │                              │
              │ Processor    │                              │
              └──────────────┘                              │
                     │                                      │
                     ▼                                      ▼
              ┌──────────────┐                    ┌─────────────────┐
              │ Redis        │                    │ Feature Store   │
              │ (Trends)     │                    │ (Engagement)    │
              └──────────────┘                    └─────────────────┘
```

**Flow:**
1. Tweet API writes to Kafka topic `tweets-created`
2. Indexer consumer reads, transforms, writes to Elasticsearch
3. Trending processor reads same topic, updates sliding window counts
4. Feature store updated async when engagement events arrive

#### 5.1.2 Search Index Schema (Elasticsearch)

```json
{
  "mappings": {
    "properties": {
      "tweet_id":     { "type": "keyword" },
      "user_id":      { "type": "keyword" },
      "text":         { "type": "text", "analyzer": "twitter_analyzer" },
      "hashtags":     { "type": "keyword" },
      "created_at":   { "type": "date" },
      "language":     { "type": "keyword" },
      "geo": {
        "type": "geo_point"
      },
      "is_deleted":   { "type": "boolean" },
      "engagement": {
        "properties": {
          "likes":    { "type": "integer" },
          "retweets": { "type": "integer" },
          "views":    { "type": "long" }
        }
      }
    }
  }
}
```

**Custom Analyzer:**
```json
{
  "twitter_analyzer": {
    "type": "custom",
    "tokenizer": "standard",
    "filter": ["lowercase", "twitter_synonyms", "edge_ngram"]
  }
}
```

#### 5.1.3 Sharding Strategy

| Scale | Strategy | Shard Key | Notes |
|-------|----------|-----------|-------|
| **100k/day** | Single shard | N/A | Simple, fits in memory |
| **1M+/day** | Time-based | `created_at` (daily) | Easy retention, hot/cold |
| **100M+/day** | Hybrid | `hash(tweet_id) + time` | Distribute writes evenly |

**Index Lifecycle:**
```
Day 0-1:   HOT    (SSD, 3 replicas, primary queries)
Day 2-4:   WARM   (SSD, 2 replicas, force-merge)
Day 5-7:   COLD   (HDD, 1 replica, read-only)
Day 8+:    DELETE (or archive to S3)
```

---

### 5.2 Search Service Design

#### 5.2.1 Query Flow

```
┌──────────────────────────────────────────────────────────────────┐
│                        Search Request                            │
│  GET /search?q=keyword1+keyword2&from=user123&lang=en            │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                      Query Parser                                │
│  - Tokenize (AND logic)                                          │
│  - Extract filters (user, lang, time range)                      │
│  - Validate & sanitize                                           │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                      Cache Check (Redis)                         │
│  Key: hash(normalized_query + filters)                           │
│  TTL: 30 seconds (balance freshness vs load)                     │
└──────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │ Cache Miss        │
                    ▼                   ▼
┌───────────────────────────┐  ┌───────────────────────────┐
│   Scatter to Shards       │  │   Cache Hit: Return       │
│   (Parallel fan-out)      │  │                           │
└───────────────────────────┘  └───────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────────────────────┐
│                      Gather & Rank                               │
│  1. Merge results from all shards                                │
│  2. Fetch engagement scores from Feature Store                   │
│  3. Apply ranking: score = recency_weight + engagement_weight    │
│  4. Return top K                                                 │
└──────────────────────────────────────────────────────────────────┘
```

#### 5.2.2 Ranking Formula

```python
def calculate_score(tweet, query_time):
    # Time decay (exponential, half-life = 6 hours)
    age_hours = (query_time - tweet.created_at).total_seconds() / 3600
    recency_score = math.exp(-age_hours / 6)  # 0 to 1

    # Engagement (log-scaled to prevent viral dominance)
    engagement_score = (
        math.log1p(tweet.likes) * 0.4 +
        math.log1p(tweet.retweets) * 0.4 +
        math.log1p(tweet.views / 1000) * 0.2
    )

    # Final score
    return (recency_score * 0.6) + (engagement_score * 0.4)
```

#### 5.2.3 Elasticsearch Query (AND logic)

```json
{
  "query": {
    "bool": {
      "must": [
        { "match": { "text": "keyword1" }},
        { "match": { "text": "keyword2" }}
      ],
      "filter": [
        { "term": { "is_deleted": false }},
        { "range": { "created_at": { "gte": "now-7d" }}}
      ]
    }
  },
  "sort": [
    { "_score": "desc" },
    { "created_at": "desc" }
  ]
}
```

---

### 5.3 Trending Topics Service

#### 5.3.1 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Kafka: tweets-created                       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Apache Flink / Kafka Streams                   │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  1. Extract hashtags + significant phrases                 │ │
│  │  2. Key by: (term, region)                                 │ │
│  │  3. Tumbling window: 1 hour                                │ │
│  │  4. Count occurrences                                      │ │
│  │  5. Emit top N per region                                  │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Redis Sorted Sets                           │
│                                                                  │
│  Key: trending:global          Key: trending:us                  │
│  Score: count                  Score: count                      │
│  Member: hashtag               Member: hashtag                   │
│                                                                  │
│  ZREVRANGE trending:global 0 9  → Top 10 global                  │
└─────────────────────────────────────────────────────────────────┘
```

#### 5.3.2 Sliding Window Implementation

```python
# Pseudo-code for Flink job
tweets_stream \
    .flat_map(extract_hashtags_and_phrases) \
    .key_by(lambda x: (x.term, x.region)) \
    .window(TumblingEventTimeWindows.of(Time.hours(1))) \
    .trigger(ContinuousProcessingTimeTrigger.of(Time.seconds(30))) \
    .aggregate(CountAggregate()) \
    .process(TopNByRegion(n=50)) \
    .add_sink(RedisSink())
```

**Why 1-hour tumbling + 30s trigger?**
- 1-hour window captures enough signal for trends
- 30s trigger emits partial results for near real-time updates
- Simpler than sliding windows, lower state overhead

#### 5.3.3 Spam/Bot Filtering

```
Pre-filter before counting:
├── Rate limit per user (max 10 tweets/minute count toward trends)
├── Ignore duplicate tweets (exact text hash)
├── Blocklist known spam hashtags
└── Minimum account age (7 days)
```

---

### 5.4 Autocomplete Service

#### 5.4.1 Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                    Autocomplete Request                        │
│                    GET /autocomplete?q=elec                    │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│                      Redis: Prefix Trie                        │
│                                                                │
│  autocomplete:e       → [election, elon, economy...]          │
│  autocomplete:el      → [election, elon...]                   │
│  autocomplete:ele     → [election, electric...]               │
│  autocomplete:elec    → [election, electric, electricity]     │
│                                                                │
│  Each entry: ZSET with score = popularity                     │
│  ZREVRANGE autocomplete:elec 0 2 → Top 3 suggestions           │
└────────────────────────────────────────────────────────────────┘
```

#### 5.4.2 Data Sources (Priority Order)

| Source | Weight | Update Frequency |
|--------|--------|------------------|
| Trending terms | 1.0 | Real-time (from trending service) |
| Popular queries (last 24h) | 0.7 | Every 5 minutes |
| Popular hashtags | 0.5 | Hourly |

#### 5.4.3 Update Pipeline

```python
# Runs every 5 minutes
def update_autocomplete_index():
    terms = []

    # 1. Get trending terms (highest priority)
    trending = redis.zrevrange("trending:global", 0, 100)
    terms.extend([(t, 1.0) for t in trending])

    # 2. Get popular queries from logs
    popular_queries = query_log_aggregator.top_queries(limit=500)
    terms.extend([(q, 0.7) for q in popular_queries])

    # 3. Build prefix entries
    for term, weight in terms:
        for i in range(1, len(term) + 1):
            prefix = term[:i].lower()
            redis.zadd(f"autocomplete:{prefix}", {term: weight})
            redis.expire(f"autocomplete:{prefix}", 3600)  # 1 hour TTL
```

---

## 6. Soft Delete Implementation (Distributed)

### Why Soft Delete?

| Approach | Pros | Cons |
|----------|------|------|
| **Hard delete** | Clean data, smaller index | Slow propagation, complex distributed delete |
| **Soft delete** | Fast (single field update), auditable | Index bloat, must filter on read |

**Decision: Soft delete with async cleanup**

### Implementation

```
┌──────────────────────────────────────────────────────────────────┐
│                      Delete Request                              │
│                 DELETE /tweets/{tweet_id}                        │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  1. Update PostgreSQL: is_deleted = true, deleted_at = now()    │
│  2. Publish to Kafka: tweets-deleted topic                       │
│  3. Return 202 Accepted (async processing)                       │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                Delete Consumer (Async)                           │
│                                                                  │
│  1. Update Elasticsearch: is_deleted = true                      │
│  2. Invalidate cache entries containing this tweet               │
│  3. Decrement trending counts if applicable                      │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│           Nightly Cleanup Job (Hard Delete)                      │
│                                                                  │
│  DELETE FROM tweets WHERE is_deleted = true                      │
│    AND deleted_at < now() - interval '24 hours'                  │
│                                                                  │
│  ES: delete_by_query { "is_deleted": true, age > 24h }           │
└──────────────────────────────────────────────────────────────────┘
```

### Query Filter (Always Applied)

```json
{
  "query": {
    "bool": {
      "filter": [
        { "term": { "is_deleted": false }}
      ]
    }
  }
}
```

---

## 7. Feature Store Design (Distributed)

### 7.1 Structure (Redis)

```
┌────────────────────────────────────────────────────────────────┐
│                     Feature Store (Redis)                       │
│                                                                 │
│  Key: engagement:{tweet_id}                                     │
│  Type: HASH                                                     │
│  Fields:                                                        │
│    - likes: 1523                                                │
│    - retweets: 342                                              │
│    - views: 89234                                               │
│    - updated_at: 1704567890                                     │
│                                                                 │
│  TTL: 7 days (matches search lookback)                          │
└────────────────────────────────────────────────────────────────┘
```

### 7.2 Update Flow

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ Like Event  │───▶│   Kafka     │───▶│  Consumer   │
│ RT Event    │    │ engagement  │    │  (batched)  │
│ View Event  │    │   topic     │    │             │
└─────────────┘    └─────────────┘    └─────────────┘
                                             │
                                             ▼
                                    ┌─────────────────┐
                                    │  Redis HINCRBY  │
                                    │  (atomic incr)  │
                                    └─────────────────┘
```

**Batching:** Views are high-volume. Batch into 10-second windows before writing:
```python
# Instead of: HINCRBY engagement:123 views 1 (per view)
# Do: HINCRBY engagement:123 views 847 (every 10 seconds)
```

### 7.3 Sync to Elasticsearch (Periodic)

```python
# Every 5 minutes: sync hot tweets
def sync_engagement_to_es():
    hot_tweets = get_tweets_with_high_activity(last_5_min)

    bulk_updates = []
    for tweet_id in hot_tweets:
        engagement = redis.hgetall(f"engagement:{tweet_id}")
        bulk_updates.append({
            "_op_type": "update",
            "_id": tweet_id,
            "doc": {"engagement": engagement}
        })

    elasticsearch.bulk(bulk_updates)
```

---

## 8. Multi-Region Architecture

### 8.1 Topology

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
    │   Primary   │        │   Replica   │        │   Replica   │
    └─────────────┘        └─────────────┘        └─────────────┘
           │                      │                      │
           ▼                      ▼                      ▼
    ┌─────────────┐        ┌─────────────┐        ┌─────────────┐
    │    Kafka    │◄──────▶│    Kafka    │◄──────▶│    Kafka    │
    │   Cluster   │  Mirror│   Cluster   │  Mirror│   Cluster   │
    └─────────────┘        └─────────────┘        └─────────────┘
           │                      │                      │
           ▼                      ▼                      ▼
    ┌─────────────┐        ┌─────────────┐        ┌─────────────┐
    │     ES      │───────▶│     ES      │───────▶│     ES      │
    │   Primary   │  CCR   │   Follower  │  CCR   │   Follower  │
    └─────────────┘        └─────────────┘        └─────────────┘
```

### 8.2 Replication Strategy

| Component | Strategy | Lag Target |
|-----------|----------|------------|
| Kafka | MirrorMaker 2 | < 1 second |
| Elasticsearch | Cross-Cluster Replication | < 5 seconds |
| Redis (trending) | Active-Active (CRDT) | < 1 second |
| PostgreSQL | Streaming replication | < 1 second |

### 8.3 Failover

```
Normal:     User → US-WEST (latency-based routing)

US-WEST down:
  1. Health check fails (3 consecutive, 10s interval)
  2. DNS TTL expires (30 seconds)
  3. Traffic routes to EU-WEST
  4. EU-WEST ES promoted to primary (manual or automated)
  5. Kafka MirrorMaker reversed

RTO: < 2 minutes
RPO: < 5 seconds (async replication lag)
```

---

## 9. Failure Handling

### 9.1 Backpressure

```
┌─────────────────────────────────────────────────────────────────┐
│                    Backpressure Strategy                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  API Gateway:                                                   │
│    - Rate limit: 100 req/s per user                             │
│    - Circuit breaker: open after 50% errors in 10s window       │
│    - Return 429 with Retry-After header                         │
│                                                                 │
│  Kafka:                                                         │
│    - Consumer lag monitoring (alert if > 10k messages)          │
│    - Auto-pause consumption if downstream is slow               │
│                                                                 │
│  Elasticsearch:                                                 │
│    - Bulk indexing with exponential backoff                     │
│    - Queue depth limit: 1000 documents, then drop oldest        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 9.2 Retry Strategy

| Operation | Max Retries | Backoff | Timeout |
|-----------|-------------|---------|---------|
| ES index | 3 | Exponential (100ms, 200ms, 400ms) | 5s |
| ES search | 2 | Fixed 50ms | 200ms |
| Redis read | 2 | Fixed 10ms | 50ms |
| Kafka produce | 5 | Exponential | 30s |

### 9.3 Graceful Degradation

```
If Elasticsearch is slow/down:
  └── Return cached results (stale up to 5 min) + warning header

If Feature Store is down:
  └── Return results without engagement ranking (recency only)

If Trending service is down:
  └── Serve last known trending from cache (up to 1 hour stale)

If Autocomplete is down:
  └── Disable autocomplete, search still works
```

---

## 10. API Design

### 10.1 Search API

```http
GET /v1/search?q=keyword1+keyword2&user_id=123&lang=en&since=2024-01-01
    &until=2024-01-07&geo=37.7749,-122.4194,10km&limit=20&cursor=abc123

Response:
{
  "results": [
    {
      "tweet_id": "1234567890",
      "user_id": "user123",
      "text": "This is a tweet about keyword1 and keyword2",
      "created_at": "2024-01-05T14:30:00Z",
      "engagement": {
        "likes": 150,
        "retweets": 42,
        "views": 5200
      }
    }
  ],
  "next_cursor": "def456",
  "took_ms": 45
}
```

### 10.2 Trending API

```http
GET /v1/trending?region=us&limit=10

Response:
{
  "trends": [
    {"term": "#Election2024", "tweet_count": 125000, "rank": 1},
    {"term": "Climate", "tweet_count": 89000, "rank": 2}
  ],
  "as_of": "2024-01-05T14:30:00Z",
  "region": "us"
}
```

### 10.3 Autocomplete API

```http
GET /v1/autocomplete?q=elec&limit=3

Response:
{
  "suggestions": [
    {"term": "election", "score": 0.95},
    {"term": "electric", "score": 0.72},
    {"term": "electricity", "score": 0.45}
  ]
}
```

---

## 11. Monitoring & Observability

### 11.1 Key Metrics

| Metric | Target | Alert Threshold |
|--------|--------|-----------------|
| Search P99 latency | < 200ms | > 300ms |
| Indexing lag | < 5s | > 30s |
| Trending freshness | < 1 min | > 2 min |
| ES cluster health | Green | Yellow > 5min |
| Kafka consumer lag | < 1000 | > 10000 |
| Cache hit rate | > 80% | < 60% |
| Error rate | < 0.1% | > 1% |

### 11.2 Dashboards

```
┌─────────────────────────────────────────────────────────────────┐
│  Search Performance Dashboard                                    │
├─────────────────────────────────────────────────────────────────┤
│  [QPS over time]  [Latency percentiles]  [Error rate]           │
│  [Cache hit/miss] [Query types breakdown] [Slow queries log]    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 12. Trade-offs & Decisions

| Decision | Chose | Over | Why |
|----------|-------|------|-----|
| Search engine | Elasticsearch | Solr, custom | Battle-tested, great tooling, CCR for multi-region |
| Stream processing | Flink | Spark Streaming | True streaming, lower latency, better state management |
| Feature store | Redis | DynamoDB | Sub-ms latency, atomic operations, sorted sets |
| Message queue | Kafka | RabbitMQ, SQS | Replayable, multi-consumer, high throughput |
| Consistency | Eventual | Strong | 99.99% availability > perfect consistency for search |
| Delete strategy | Soft | Hard | Faster, simpler distributed delete propagation |
| Ranking | Simple formula | ML model | Start simple, ML adds complexity + latency |

### What We're NOT Optimizing (for now)

1. **Personalization** - No user graph, no ML ranking (add later if needed)
2. **Advanced NLP** - No semantic search, no entity extraction (simple keyword match)
3. **Real-time analytics** - No per-query analytics, just aggregate metrics
4. **Cost** - Prioritizing reliability over cost optimization

---

## 13. Evolution Path

### Phase 1: MVP (Current Design)
- Single region
- Simple ranking
- Basic monitoring

### Phase 2: Scale
- Multi-region deployment
- Advanced caching
- ML ranking model

### Phase 3: Advanced
- Semantic search (embeddings)
- Personalization
- Real-time ML feature updates
