# Distributed Counter - 100K Tier (Growth Scale)

A cached monolith implementation for handling **up to 100,000 likes/second** using **PostgreSQL** for persistence and **Redis** for caching and fast deduplication.

## Architecture

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
┌─────────────────────────────────────────────────────────────────┐
│                      Redis Cluster                               │
│    - Counter cache (STRING with TTL)                            │
│    - Recent likes per user (SET)                                │
│    - Rate limiting (INCR with EXPIRE)                           │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│               PostgreSQL (Primary + Read Replicas)               │
│    - user_likes table (source of truth)                         │
│    - counters table (persistent storage)                        │
└─────────────────────────────────────────────────────────────────┘
```

## Features

- **Redis Caching**: 95%+ cache hit rate for counter reads
- **Fast Deduplication**: Redis SET for recent likes (24hr window)
- **Rate Limiting**: Distributed rate limiting via Redis
- **Write-Through Cache**: Counter updates go to both Redis and PostgreSQL
- **Strong Consistency**: PostgreSQL remains source of truth

## Quick Start

### Docker Compose (Recommended)

```bash
# Start PostgreSQL and Redis
docker-compose up -d postgres redis

# Copy environment file
cp .env.example .env

# Install Python dependencies
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run the application
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## API Documentation

- **Swagger UI**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health

## API Examples

### Like an Item

```bash
curl -X POST http://localhost:8000/v1/like \
  -H "Content-Type: application/json" \
  -d '{"user_id": 123, "item_id": 456, "item_type": "post"}'
```

### Get Counter (Cached)

```bash
curl "http://localhost:8000/v1/count?item_id=456&item_type=post"
```

## Redis Key Structure

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

## Capacity

| Metric | Value |
|--------|-------|
| Max likes/sec | 100,000 |
| Items | < 1 billion |
| User-likes | < 10 billion |
| PostgreSQL size | < 1 TB |
| Redis memory | 10-20 GB |
| App servers | 4-8 |

## Performance

| Operation | Latency (P99) |
|-----------|---------------|
| Like | < 30ms |
| Unlike | < 30ms |
| Get Count (cached) | < 5ms |
| Get Count (miss) | < 20ms |
| Batch Count | < 15ms |

## When to Upgrade to 1M Tier

- PostgreSQL write contention causes timeouts
- Hot items create single-row bottlenecks
- Need for async processing becomes critical

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL connection |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `CACHE_TTL_SECONDS` | `3600` | Counter cache TTL |
| `RECENT_LIKES_TTL` | `86400` | Recent likes cache TTL |
| `RATE_LIMIT_REQUESTS` | `100` | Max likes per minute |

## License

MIT
