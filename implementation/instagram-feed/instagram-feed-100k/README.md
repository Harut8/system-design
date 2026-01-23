# Instagram Feed - 100K Tier (Growing Startup)

A monolith implementation with **Redis caching** for handling **up to 100,000 users**. Introduces cached feed results and basic fan-out-on-write for improved read performance.

## Architecture

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

## Key Features

- **Feed Caching**: Redis caches feed results (1 min TTL)
- **Basic Fan-out**: Push post IDs to follower feeds for small accounts
- **Profile Caching**: User profiles cached (5 min TTL)
- **Post Caching**: Hot posts cached (1 hour TTL)

## Scale Profile

```
Total users:       100,000
Daily active:      20,000 (20%)
Posts/day:         5,000
Feed reads/day:    200,000
Feed reads/sec:    ~2-3 QPS (peak ~50 QPS)
Storage/month:     ~5 GB posts + 500 GB media
```

## Quick Start

### Option 1: Docker Compose (Recommended)

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

### Option 2: Full Docker

```bash
docker-compose up -d --build
```

## API Documentation

Once running, visit:
- **Swagger UI**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health

## New Features (vs 10K Tier)

### Cache Headers
Response includes cache metadata:
```json
{
  "posts": [...],
  "cached": true,
  "cache_age_seconds": 45
}
```

### Cache Invalidation
Posts are automatically invalidated from cache on:
- New post creation (follower feeds invalidated)
- Post deletion
- Like/unlike (post cache updated)

## Caching Strategy

| Cache Key | TTL | Purpose |
|-----------|-----|---------|
| `feed:{user_id}` | 60s | Cached feed results |
| `user:{user_id}` | 300s | User profile cache |
| `post:{post_id}` | 3600s | Post metadata cache |
| `feed_ids:{user_id}` | 600s | Pre-computed feed post IDs |

## Performance

| Operation | Latency (P99) | Cache Hit |
|-----------|---------------|-----------|
| Get Feed (cached) | < 20ms | Yes |
| Get Feed (miss) | < 100ms | No |
| Create Post | < 50ms | N/A |
| Like Post | < 30ms | N/A |

## When to Upgrade to 1M Tier

- Redis cache hit rate dropping below 80%
- Need background job processing (async tasks)
- Feed query complexity increasing
- Worker queue backlog growing
- Need for CDN for API responses

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL connection |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `DEBUG` | `false` | Enable debug mode |
| `FEED_CACHE_TTL` | `60` | Feed cache TTL in seconds |
| `PUSH_THRESHOLD` | `5000` | Max followers for fan-out-on-write |

## License

MIT
