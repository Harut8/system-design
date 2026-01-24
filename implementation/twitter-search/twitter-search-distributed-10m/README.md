# Twitter Search Distributed (10M Scale)

A distributed implementation of Twitter-like search functionality using **Elasticsearch**, **Kafka**, and **microservices architecture**.

**Target Scale**: ~10,000,000 tweets/day, ~100,000 concurrent users

> **Note**: This is a distributed architecture designed for massive scale. For smaller deployments, see `twitter-search-monolith-*` implementations.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    API Gateway / Load Balancer                   │
│                    (Multi-region, 50+ instances)                 │
└─────────────────────────────────────────────────────────────────┘
          │                   │                      │
          ▼                   ▼                      ▼
┌─────────────────┐  ┌─────────────────┐   ┌───────────────────────┐
│  Search Service │  │ Trending Service│   │   Tweet Service       │
│  (20 instances) │  │ (10 instances)  │   │   (30 instances)      │
└─────────────────┘  └─────────────────┘   └───────────────────────┘
          │                   │                      │
          ▼                   ▼                      ▼
┌─────────────────┐  ┌─────────────────┐   ┌───────────────────────┐
│  Elasticsearch  │  │  Redis Cluster  │   │      Kafka            │
│  (15+ nodes)    │  │  (100+ GB)      │   │   (20+ brokers)       │
└─────────────────┘  └─────────────────┘   └───────────────────────┘
                                                     │
                              ┌───────────────────────┤
                              ▼                       ▼
                    ┌─────────────────┐   ┌───────────────────────┐
                    │  Index Workers  │   │  PostgreSQL Shards    │
                    │  (20 instances) │   │  (16-32 shards)       │
                    └─────────────────┘   └───────────────────────┘
```

## Features

- **Elasticsearch Search**: Distributed full-text search with relevance ranking
- **Kafka Event Streaming**: Async tweet indexing and processing
- **Microservices**: Independent scaling of search, trending, and tweet services
- **Redis Cluster**: Distributed caching for trending and autocomplete
- **Multi-Region**: Global deployment with regional data centers
- **Horizontal Scaling**: Add capacity on-demand

## Quick Start

```bash
# Start all services (requires substantial resources)
docker-compose up -d

# Install dependencies
pip install -r requirements.txt

# Run API server
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Run index worker (separate terminal)
python -m app.workers.indexer
```

## Kafka Topics

```
Topic: tweet-events
Partitions: 256 (by tweet_id for ordering)
Replication: 3
Retention: 14 days

Topic: search-index
Partitions: 128
Replication: 3
Retention: 7 days
```

## Capacity

| Metric | Value |
|--------|-------|
| Max tweets/day | 10,000,000 |
| Total tweets | < 50 billion |
| Search QPS | 100,000+ |
| Elasticsearch nodes | 15-30 |
| Kafka brokers | 20-50 |
| Redis memory | 100-300 GB (clustered) |
| App servers | 50-100 per service |
| Regions | 3-5 |

## Performance

| Operation | Latency (P99) |
|-----------|---------------|
| Search | < 200ms |
| Trending | < 10ms (cached) |
| Autocomplete | < 30ms |
| Create Tweet | < 50ms |
| Index Propagation | < 10 seconds |
| Cross-region sync | < 60 seconds |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ELASTICSEARCH_URL` | `http://localhost:9200` | Elasticsearch cluster |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka brokers |
| `REDIS_CLUSTER_URL` | `redis://localhost:6379` | Redis cluster |
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL (sharded) |

## Scaling Notes

At 10M scale, this architecture requires:

1. **Elasticsearch Cluster** - 15+ nodes with 3+ master nodes
2. **Kafka Cluster** - 20+ brokers with ZooKeeper/KRaft
3. **Redis Cluster** - 100+ GB across multiple nodes
4. **PostgreSQL Shards** - 16-32 shards for write distribution
5. **Multi-Region** - 3-5 regions for global coverage
6. **CDN** - Edge caching for static content and API responses

## License

MIT
