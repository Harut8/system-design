# Instagram Feed - 1M Tier (Scaling Startup)

A monolith implementation with **async processing via Celery** for handling **up to 1 million users**. Introduces hybrid fan-out strategy with background workers.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CLIENTS                                         │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CDN (CloudFront)                                   │
│                    - Media delivery                                          │
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
│ PostgreSQL   │            │    Redis     │            │    Redis     │
│ (Primary +   │            │   (Cache)    │            │   (Celery    │
│  Read Replica)│            │              │            │   Broker)    │
└──────────────┘            └──────────────┘            └──────────────┘
                                                               │
                                                               ▼
                                                    ┌──────────────────┐
                                                    │  Worker Servers  │
                                                    │  - Fan-out       │
                                                    │  - Notifications │
                                                    │  - Media proc.   │
                                                    └──────────────────┘
```

## Key Features

- **Hybrid Fan-out**: Push for users with < 10K followers, pull for celebrities
- **Async Processing**: Celery workers for background fan-out
- **Celebrity Post Cache**: Special handling for high-follower accounts
- **Media Processing**: Background image resizing/optimization (stub)

## Scale Profile

```
Total users:       1,000,000
Daily active:      200,000 (20%)
Posts/day:         50,000
Feed reads/day:    4,000,000
Feed reads/sec:    ~50 QPS (peak ~500 QPS)
Storage/month:     ~50 GB posts + 5 TB media
```

## Quick Start

### Option 1: Docker Compose (Recommended)

```bash
# Start all services
docker-compose up -d

# Or start individually
docker-compose up -d postgres redis

# Copy environment file
cp .env.example .env

# Install Python dependencies
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run the API server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Run Celery worker (in another terminal)
celery -A app.workers.tasks worker --loglevel=info
```

### Option 2: Full Docker

```bash
docker-compose up -d --build
```

## API Documentation

Once running, visit:
- **Swagger UI**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health
- **Celery Flower** (if enabled): http://localhost:5555

## Hybrid Fan-out Strategy

```
User posts new content:
                    │
                    ▼
        ┌───────────────────┐
        │ Check follower    │
        │ count             │
        └───────────────────┘
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
  < 10K followers         >= 10K followers
        │                       │
        ▼                       ▼
  Fan-out-on-write        Store in celebrity
  (push to feeds)         post cache (pull)
```

## New Features (vs 100K Tier)

- **Celery Workers**: Async fan-out processing
- **Celebrity Detection**: Automatic classification based on follower count
- **Hybrid Feed Assembly**: Combines pushed and pulled content
- **Rate-limited Fan-out**: Prevents system overload

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL connection |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis cache connection |
| `CELERY_BROKER_URL` | `redis://localhost:6379/1` | Celery broker |
| `CELEBRITY_THRESHOLD` | `10000` | Follower count for celebrity status |
| `FAN_OUT_BATCH_SIZE` | `1000` | Followers per batch in fan-out |

## Performance

| Operation | Latency (P99) |
|-----------|---------------|
| Get Feed (cached) | < 30ms |
| Get Feed (hybrid) | < 100ms |
| Create Post | < 50ms (async fan-out) |
| Like Post | < 30ms |

## When to Upgrade to 10M Tier

- PostgreSQL can't handle write load (>5K TPS)
- Single region latency issues for global users
- Need for real-time features (live updates)
- Worker queue backlog growing consistently
- Team growing, need service boundaries

## License

MIT
