# Instagram Feed - 10M Tier (Scale-Up)

A **microservices architecture** with **Kafka** for handling **up to 10 million users**. This tier introduces service boundaries, event-driven architecture, and dedicated data stores per service.

## Architecture

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
│  Fan-out Workers  │     │  Counter Workers  │     │  Notification     │
│  (20 instances)   │     │  (5 instances)    │     │  Workers          │
└───────────────────┘     └───────────────────┘     └───────────────────┘
```

## Services

### Feed Service
- Responsible for feed generation and caching
- Assembles feeds from cached posts and celebrity posts
- No direct database access - calls Post Service API

### Post Service
- Handles post CRUD operations
- Stores posts in Cassandra for high write throughput
- Publishes events to Kafka on post creation/deletion

### User Service (simplified in this example)
- User profiles and social graph
- Stores data in PostgreSQL

### Worker Service
- Kafka consumers for async processing
- Fan-out workers for feed distribution
- Counter aggregation workers

## Scale Profile

```
Total users:       10,000,000
Daily active:      2,000,000 (20%)
Posts/day:         500,000
Feed reads/day:    50,000,000
Feed reads/sec:    ~500 QPS (peak ~5,000 QPS)
Storage/month:     ~500 GB posts + 50 TB media
```

## Quick Start

```bash
# Start all services
docker-compose up -d

# Or start individually
docker-compose up -d postgres redis kafka zookeeper

# Run services
docker-compose up -d feed-service post-service worker-service
```

## Key Differences from 1M Tier

| Aspect | 1M Tier | 10M Tier |
|--------|---------|----------|
| Architecture | Monolith | Microservices |
| Data Store | PostgreSQL | Cassandra + PostgreSQL |
| Message Queue | Celery (Redis) | Kafka |
| Fan-out | Sync/Async via Celery | Event-driven via Kafka |
| Scaling | Vertical + some horizontal | Fully horizontal |
| Service Boundaries | None | Clear per-domain |

## Kafka Topics

| Topic | Purpose | Partitions |
|-------|---------|------------|
| `posts.created` | New post events | 32 |
| `posts.deleted` | Post deletion events | 8 |
| `interactions` | Likes, comments, saves | 64 |
| `feed.fanout` | Fan-out commands | 128 |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BOOTSTRAP_SERVERS` | `kafka:9092` | Kafka brokers |
| `REDIS_CLUSTER` | `redis:6379` | Redis cluster |
| `CASSANDRA_HOSTS` | `cassandra:9042` | Cassandra hosts |
| `CELEBRITY_THRESHOLD` | `10000` | Celebrity cutoff |

## License

MIT
