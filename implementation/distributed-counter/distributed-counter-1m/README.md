# Distributed Counter - 1M Tier (Scale-Up)

An async processing implementation for handling **up to 1,000,000 likes/second** using **Kafka** for event streaming and batched counter aggregation.

## Architecture

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
│ - Counters      │  │ - like-events   │   │ - user_likes          │
│ - Recent likes  │  │   topic         │   │ - counters            │
│ - Rate limits   │  │ - Partitioned   │   │                       │
└─────────────────┘  └─────────────────┘   └───────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                Counter Aggregation Worker                        │
│                                                                  │
│  1. Consume micro-batches (1-second windows)                    │
│  2. Deduplicate within window                                   │
│  3. Verify with PostgreSQL                                      │
│  4. Batch update counters                                       │
│  5. Update Redis cache                                          │
└─────────────────────────────────────────────────────────────────┘
```

## Features

- **Async Processing**: Fire-and-forget writes via Kafka
- **Batched Aggregation**: 100x write reduction to database
- **Optimistic UI**: Immediate cache updates for user feedback
- **Eventual Consistency**: Counters converge within seconds
- **Horizontal Scaling**: Add more workers for throughput

## Quick Start

```bash
# Start all services
docker-compose up -d

# Install dependencies
pip install -r requirements.txt

# Run API server
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Run aggregation worker (separate terminal)
python -m app.workers.aggregator
```

## Kafka Topics

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
    "action": "like",
    "timestamp": "2024-01-15T10:30:00Z"
}
```

## Capacity

| Metric | Value |
|--------|-------|
| Max likes/sec | 1,000,000 |
| Items | < 10 billion |
| User-likes | < 100 billion |
| PostgreSQL clusters | 4-8 shards |
| Kafka brokers | 6-10 |
| Redis memory | 50-100 GB |
| App servers | 20-50 |

## Performance

| Operation | Latency (P99) |
|-----------|---------------|
| Like (API) | < 20ms |
| Counter convergence | < 2 seconds |
| Get Count (cached) | < 5ms |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka brokers |
| `KAFKA_TOPIC` | `like-events` | Events topic |
| `AGGREGATION_WINDOW_SECONDS` | `1` | Batch window size |
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis |

## License

MIT
