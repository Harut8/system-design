# Twitter Search Monolith (1M Scale)

A production-ready monolith implementation of Twitter-like search functionality using **PostgreSQL full-text search** and **Redis caching**.

**Target Scale**: ~1,000,000 tweets/day, ~10,000 concurrent users

> **Note**: This is the maximum scale for a monolith architecture. Beyond this, consider migrating to a distributed architecture with Elasticsearch, Kafka, and microservices.

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
│  │ Search API  │  │Trending API │  │ Autocomplete API        │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │              Background Tasks (APScheduler)                  ││
│  │  - Trending refresh (30s)   - Autocomplete rebuild (5m)     ││
│  │  - View buffer flush (10s)  - Cleanup deleted tweets (1h)   ││
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
└─────────────────────┘               └─────────────────────┘
```

## Features

- **Full-text Search**: PostgreSQL `tsvector` with GIN index, AND logic, relevance ranking
- **Trending Topics**: 1-hour sliding window, refreshed every 30 seconds
- **Autocomplete**: Prefix-based suggestions from trending terms and popular queries
- **Engagement Tracking**: Likes, retweets, views with buffered writes
- **Soft Delete**: Immediate soft delete, hard delete after 24 hours
- **Caching**: Redis caching for search results, trending, and autocomplete

## Quick Start

### Option 1: Docker Compose (Recommended)

```bash
# Start PostgreSQL and Redis
docker-compose up -d postgres redis

# Copy environment file
cp .env.example .env

# Install Python dependencies
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt

# Run the application
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Option 2: Full Docker

```bash
# Build and start all services
docker-compose up -d --build

# View logs
docker-compose logs -f app
```

## API Documentation

Once running, visit:
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **Health Check**: http://localhost:8000/health

## API Examples

### Create a Tweet

```bash
curl -X POST http://localhost:8000/v1/tweets \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "text": "Excited about the new AI developments! #tech #ai #innovation",
    "language": "en"
  }'
```

### Search Tweets

```bash
# Basic search
curl "http://localhost:8000/v1/search?q=AI+tech"

# With filters
curl "http://localhost:8000/v1/search?q=AI&user_id=user123&limit=10"
```

### Get Trending Topics

```bash
curl "http://localhost:8000/v1/trending?limit=10"
```

### Autocomplete

```bash
curl "http://localhost:8000/v1/autocomplete?q=tec"
```

### Engagement

```bash
# Like a tweet
curl -X POST http://localhost:8000/v1/tweets/abc123/engagement \
  -H "Content-Type: application/json" \
  -d '{"tweet_id": "abc123", "action": "like"}'

# Record a view
curl -X POST http://localhost:8000/v1/tweets/abc123/engagement \
  -H "Content-Type: application/json" \
  -d '{"tweet_id": "abc123", "action": "view"}'
```

## Generate Sample Data

After the application is running, you can generate sample data:

```bash
# Connect to PostgreSQL
docker-compose exec postgres psql -U postgres -d twitter_search

# Generate 1000 sample tweets
SELECT generate_sample_tweets(1000);
```

Or via the API:

```bash
# Create multiple tweets
for i in {1..100}; do
  curl -X POST http://localhost:8000/v1/tweets \
    -H "Content-Type: application/json" \
    -d "{
      \"user_id\": \"user$((i % 5 + 1))\",
      \"text\": \"Sample tweet $i about #tech and #innovation\"
    }"
done
```

## Project Structure

```
twitter-search-monolith/
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI application entry point
│   ├── config.py         # Pydantic settings
│   ├── database.py       # Database and Redis connections
│   ├── models.py         # SQLAlchemy models
│   ├── schemas.py        # Pydantic schemas
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py       # Dependency injection
│   │   └── routes.py     # API routes
│   ├── services/
│   │   ├── __init__.py
│   │   ├── search.py     # Search service
│   │   ├── trending.py   # Trending service
│   │   ├── autocomplete.py
│   │   └── tweet.py      # Tweet CRUD service
│   └── tasks/
│       ├── __init__.py
│       └── scheduler.py  # Background tasks
├── scripts/
│   └── init.sql          # Database initialization
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

## Configuration

Environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `DEBUG` | `false` | Enable debug mode |
| `SEARCH_LOOKBACK_DAYS` | `7` | Days to search back |
| `TRENDING_WINDOW_HOURS` | `1` | Trending calculation window |
| `TRENDING_REFRESH_SECONDS` | `30` | Trending cache refresh interval |

## Scaling Notes

This monolith is designed for **~1M tweets/day** (maximum monolith scale). When you outgrow it:

1. **Add read replicas** - required at this scale for read-heavy workloads
2. **Use PgBouncer** - connection pooling is essential
3. **Consider Elasticsearch** when query latency P99 > 100ms
4. **Add Kafka** when write throughput > 1000 TPS
5. **Go multi-region** when you need global distribution

**Beyond 1M tweets/day**: Migrate to a distributed architecture with:
- Elasticsearch for search
- Kafka for event streaming
- Microservices architecture
- Multi-region deployment

## Performance

At 1M tweets/day:

| Operation | Latency (P99) |
|-----------|---------------|
| Search | < 100ms |
| Trending | < 5ms (cached) |
| Autocomplete | < 15ms |
| Create Tweet | < 30ms |

## Development

```bash
# Run tests
pytest

# Format code
black app/
isort app/

# Type checking
mypy app/
```

## License

MIT
