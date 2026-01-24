# Distributed Counter - 10K Tier (Startup Scale)

A simple monolith implementation for handling **up to 10,000 likes/second** using **PostgreSQL** for both storage and deduplication.

## Architecture

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
│    - user_likes table (dedup via UNIQUE constraint)             │
│    - counters table (aggregated counts)                         │
└─────────────────────────────────────────────────────────────────┘
```

## Features

- **Like/Unlike**: Idempotent operations with PostgreSQL UNIQUE constraint
- **Counter Reads**: Single and batch counter retrieval
- **HasUserLiked**: Check if user has liked specific items
- **Rate Limiting**: Simple in-memory rate limiting
- **Strong Consistency**: Synchronous writes with transactions

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

### Like an Item

```bash
curl -X POST http://localhost:8000/v1/like \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 123,
    "item_id": 456,
    "item_type": "post"
  }'
```

### Unlike an Item

```bash
curl -X DELETE http://localhost:8000/v1/like \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 123,
    "item_id": 456,
    "item_type": "post"
  }'
```

### Get Counter (Single)

```bash
curl "http://localhost:8000/v1/count?item_id=456&item_type=post"
```

### Get Counters (Batch)

```bash
curl -X POST http://localhost:8000/v1/counts \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"item_id": 456, "item_type": "post"},
      {"item_id": 789, "item_type": "post"}
    ]
  }'
```

### Check if User Liked

```bash
curl -X POST http://localhost:8000/v1/has_liked \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": 123,
    "item_ids": [456, 789],
    "item_type": "post"
  }'
```

## Capacity

| Metric | Value |
|--------|-------|
| Max likes/sec | 10,000 |
| Items | < 100 million |
| User-likes | < 1 billion |
| PostgreSQL size | < 100 GB |
| Servers | 1-2 |

## Performance

| Operation | Latency (P99) |
|-----------|---------------|
| Like | < 50ms |
| Unlike | < 50ms |
| Get Count | < 20ms |
| Batch Count | < 30ms |

## When to Upgrade to 100K Tier

- Write latency P99 > 100ms
- Database CPU consistently > 70%
- Read replica lag becomes noticeable
- Need for caching to reduce DB load

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL connection |
| `DEBUG` | `false` | Enable debug mode |
| `RATE_LIMIT_REQUESTS` | `100` | Max likes per minute per user |

## License

MIT
