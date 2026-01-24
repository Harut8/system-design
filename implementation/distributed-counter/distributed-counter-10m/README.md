# Distributed Counter - 10M Tier (Internet Scale)

A sharded counter implementation for handling **up to 10,000,000 likes/second** with automatic hot item detection and counter sharding.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    CDN + Global Load Balancer                    │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        ┌──────────┐   ┌──────────┐   ┌──────────┐
        │ Region A │   │ Region B │   │ Region C │
        │   API    │   │   API    │   │   API    │
        └──────────┘   └──────────┘   └──────────┘
              │               │               │
              └───────────────┼───────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Hot Item Detection Service                    │
│                                                                  │
│  - Tracks write rate per item (sliding window)                  │
│  - Auto-shards counters when rate > 1000/sec                    │
│  - Auto-merges when rate < 100/sec for 1 hour                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Sharded Counter Layer (Redis)                 │
│                                                                  │
│  Normal items:                                                  │
│    counter:post:123 = 5000                                      │
│                                                                  │
│  Hot items (auto-sharded to 16 shards):                         │
│    counter:post:viral:shard_0 = 312500                          │
│    counter:post:viral:shard_1 = 312500                          │
│    ...                                                          │
│    counter:post:viral:shard_15 = 312500                         │
│                                                                  │
│  Read: MGET all shards → SUM                                    │
│  Write: INCR random shard                                       │
└─────────────────────────────────────────────────────────────────┘
```

## Features

- **Auto-Sharding**: Automatically shard hot counters
- **Hot Item Detection**: Identify viral content in real-time
- **Bloom Filters**: Fast negative dedup checks
- **Counter Merging**: Auto-merge cold counters

## Quick Start

```bash
docker-compose up -d
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
python -m app.workers.aggregator
```

## Capacity

| Metric | Value |
|--------|-------|
| Max likes/sec | 10,000,000 |
| Items | < 100 billion |
| User-likes | < 1 trillion |
| Redis cluster nodes | 30-50 |
| Kafka partitions | 256+ |
| App servers | 100+ |

## License

MIT
