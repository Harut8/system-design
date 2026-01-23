# Instagram Feed - 10K Tier (MVP / Early Startup)

A simple monolith implementation for handling **up to 10,000 users** using **PostgreSQL** for storage with a pure pull-based feed generation (fan-out-on-read).

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                  │
│                    (Mobile App / Web)                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     FastAPI Application                          │
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │
│  │Feed API  │  │Post API  │  │User API  │  │ Interaction API  │ │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      PostgreSQL                                  │
│         (users, posts, follows, likes, comments)                 │
└─────────────────────────────────────────────────────────────────┘
```

## Features

- **Feed Generation**: Pure pull-based (fan-out-on-read)
- **Post CRUD**: Create, read, delete posts with media URLs
- **Social Graph**: Follow/unfollow users
- **Interactions**: Like, unlike, comment on posts
- **Pagination**: Cursor-based infinite scroll

## Scale Profile

```
Total users:       10,000
Daily active:      2,000 (20%)
Posts/day:         500
Feed reads/day:    10,000
Feed reads/sec:    ~0.1 QPS (peak ~5 QPS)
Storage/month:     ~500 MB posts + 50 GB media
```

## Quick Start

### Option 1: Docker Compose (Recommended)

```bash
# Start PostgreSQL
docker-compose up -d postgres

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

## API Examples

### Create a Post

```bash
curl -X POST http://localhost:8000/v1/posts \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 123,
    "caption": "Beautiful sunset!",
    "media_url": "https://cdn.example.com/photos/sunset.jpg",
    "media_type": "image"
  }'
```

### Get User Feed

```bash
curl "http://localhost:8000/v1/feed?user_id=123&limit=20"
```

### Follow a User

```bash
curl -X POST http://localhost:8000/v1/follow \
  -H "Content-Type: application/json" \
  -d '{
    "follower_id": 123,
    "followee_id": 456
  }'
```

### Like a Post

```bash
curl -X POST http://localhost:8000/v1/posts/789/like \
  -H "Content-Type: application/json" \
  -d '{"user_id": 123}'
```

### Get User Profile with Posts

```bash
curl "http://localhost:8000/v1/users/456/posts?limit=20"
```

## Database Schema

The schema includes:
- `users`: User profiles and follower counts
- `posts`: Post metadata with media URLs
- `follows`: Social graph (follower/followee relationships)
- `likes`: Post likes with deduplication
- `comments`: Post comments

## Performance

| Operation | Latency (P99) |
|-----------|---------------|
| Get Feed | < 100ms |
| Create Post | < 50ms |
| Like Post | < 30ms |
| Follow User | < 30ms |

## When to Upgrade to 100K Tier

- Feed queries taking >200ms consistently
- Database CPU >70% during peaks
- Single server can't handle traffic spikes
- Need for more reliability (>99% uptime)
- Cache hit rate benefits (repeated feed views)

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL connection |
| `DEBUG` | `false` | Enable debug mode |
| `FEED_LIMIT_DEFAULT` | `20` | Default posts per feed page |
| `FEED_DAYS_LIMIT` | `7` | Days of posts to include in feed |

## License

MIT
