# Real-Time Recommendation System: Design Document

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|----------|----------|--------|
| **Platform** | What type of content? | Short-form video (15s-3min), TikTok-like |
| **Platform** | Primary surface? | "For You" feed — 80%+ sessions start here |
| **Platform** | Primary engagement signal? | Watch time / completion rate (not clicks) |
| **Scale** | Monthly active users? | 500M MAU, 200M DAU |
| **Scale** | Content corpus size? | 200M items, growing 5M/day |
| **Scale** | Peak QPS? | 500K recommendation requests/sec |
| **Scale** | Read-to-write ratio? | 50:1 (reads dominate) |
| **Latency** | End-to-end P99? | 200ms from request to ranked list returned |
| **Latency** | Feature update propagation? | < 5 seconds from user action to feature available |
| **Freshness** | New item eligibility? | Within 10 minutes of upload (post-moderation) |
| **Freshness** | Preference shift reflection? | Same session (seconds) |
| **Availability** | Target? | 99.99% — rec system IS the product |
| **Geography** | Deployment? | Multi-region: NA, EU, APAC |
| **Geography** | Data residency? | User data sharded by region (GDPR) |
| **ML Models** | Training? | Offline training, online serving (no online learning in v1) |
| **ML Models** | Ranking architecture? | Multi-stage: pre-rank -> heavy rank -> re-rank |
| **Objectives** | Optimization targets? | Watch time, like, share, follow (multi-objective) |
| **Moderation** | Content safety? | Separate system; we consume approve/flag/remove signals |
| **Ads** | In scope? | No, but feed has defined ad insertion points |
| **Cold Start** | New user strategy? | Demographics + trending + rapid preference learning |
| **Cold Start** | New item strategy? | Content features + creator history + exploration budget |

### Explicit Assumptions

1. Content moderation happens upstream — items arrive with status: `approved`, `flagged`, or `removed`.
2. User embeddings and item embeddings are retrained daily in batch; the serving system loads new model checkpoints without downtime.
3. Video content features (visual, audio, text embeddings) are computed at upload time by a separate media processing pipeline and stored in the item feature table.
4. Social graph data (follow/follower relationships) is maintained by a separate service; we read it via a graph query API with P99 < 5ms.
5. The system starts as a single-region deployment (NA) and scales to three regions. The design supports both.
6. User consent and privacy settings are enforced at the API gateway; the rec system receives only consented data.

---

## 2. Capacity Estimates

### 2.1 Request Volume

```
DAU:                    200,000,000
Sessions per user/day:  ~4 (average)
Feed loads per session: ~8 (initial load + scroll refreshes)
Total feed requests/day: 200M * 4 * 8 = 6,400,000,000 (6.4B)

Average QPS:            6.4B / 86,400 = ~74,000 QPS
Peak multiplier:        ~6.7x (concentrated in evening hours, 4 peak hours)
Peak QPS:               ~500,000 QPS (as specified)

Items per feed page:    20
Total items scored/day: 6.4B * 20 = 128B item-scores
```

### 2.2 Feature Store Sizing

```
User features:
  - Number of users:     500M (all MAU need feature vectors)
  - Vector size:         ~1.5 KB average (demographics 100B + history 600B +
                         session features 400B + embeddings 400B)
  - Total user features: 500M * 1.5 KB = 750 GB

Item features:
  - Number of items:     200M (active corpus)
  - Vector size:         ~2 KB average (content embeddings 768B + engagement
                         stats 200B + creator features 200B + metadata 300B +
                         category/tags 200B + quality scores 100B)
  - Total item features: 200M * 2 KB = 400 GB

Cross features (computed at request time, not stored):
  - User-item affinity computed on-the-fly via dot products

Total feature store:    750 GB + 400 GB = 1.15 TB raw
  With replication (3x): ~3.5 TB across Redis cluster
  With headroom (1.5x):  ~5.2 TB provisioned

Feature store QPS:
  - Per request: 1 user lookup + ~2000 item lookups (batched)
  - Peak: 500K * 1 (user) = 500K user reads/sec
  - Peak: 500K * 2000 (items, batched into ~40 multi-gets of 50) = 20M key reads/sec
  - Redis cluster with 50 shards: ~400K ops/shard (well within single-node limits)
```

### 2.3 Embedding Index Sizing (ANN / HNSW)

```
Item embedding dimension: 256 (Two-Tower model output)
Number of items:          200M
Bytes per vector:         256 dims * 4 bytes (float32) = 1,024 bytes = 1 KB

Raw vector data:          200M * 1 KB = 200 GB

HNSW index overhead:
  - M (max connections per node): 32
  - Each connection: 4 bytes (int32 neighbor ID)
  - Connections per node: 32 * 2 levels avg = 64 links
  - Graph overhead per node: 64 * 4 = 256 bytes
  - Total graph overhead: 200M * 256 B = ~51 GB

  - Metadata per node (item_id, status, timestamp): ~32 bytes
  - Metadata total: 200M * 32 B = ~6.4 GB

Total HNSW index:         200 + 51 + 6.4 = ~258 GB per replica

With 3 replicas per region: ~774 GB per region
With 3 regions:             ~2.3 TB total HNSW storage

Single-machine fit?
  - A machine with 512 GB RAM can hold the full index.
  - For redundancy: shard into 4 shards of 50M items each (~65 GB per shard).
  - Each shard on a 128 GB RAM machine with room for OS + query buffers.
  - 4 shards * 3 replicas = 12 machines per region for ANN serving.

Search performance (HNSW with ef_search=200):
  - ~2ms per query on 50M vectors (measured on similar-scale deployments)
  - Top-200 neighbors per channel
  - 4 shards queried in parallel: wall-clock ~2-3ms
```

### 2.4 Model Serving GPU Fleet

```
Heavy ranker (Deep & Cross Network v2):
  - Input: ~500 candidates * ~800 features each
  - Model size: ~50M parameters (float16 = ~100 MB)
  - Inference: ~500 candidates batched per request
  - Time per batch (NVIDIA A10G): ~8 ms for 500 candidates
  - Peak QPS: 500K requests/sec
  - Requests per GPU per second: 1000ms / 8ms = 125 requests/sec
  - GPUs needed: 500K / 125 = 4,000 GPUs (raw)
  - With 70% utilization target: 4,000 / 0.7 = ~5,700 GPUs
  - With 3 regions: ~1,900 GPUs per region

Lightweight ranker (pre-ranking):
  - Input: ~2000 candidates * ~200 features
  - Model: small 2-layer MLP, ~2M params
  - Inference: ~2000 candidates in ~3 ms on CPU (no GPU needed)
  - 500K QPS / (1000/3) = ~1,500 CPU instances (8-core each)
  - With 3 regions: ~500 instances per region

Two-Tower inference (candidate generation):
  - User tower only (item embeddings pre-computed):
    - User embedding: ~1 ms per user on CPU
    - 500K QPS: ~500K embeddings/sec
    - 8-core machine handles ~2000 embeddings/sec
    - Need: 500K / 2000 = 250 machines
    - With 3 regions: ~85 machines per region

Total GPU fleet (heavy ranker only):
  - 5,700 A10G GPUs across 3 regions
  - At ~$0.75/hr (reserved instance), cost: 5,700 * $0.75 * 8760 = ~$37.4M/year
  - Optimization: quantize to INT8 -> ~2x throughput -> ~2,850 GPUs -> ~$18.7M/year
```

### 2.5 Kafka Throughput

```
Event types per user action:
  - impression (item shown): 20 per feed load
  - watch_start: ~15 per feed load (some never play)
  - watch_complete: ~8 per feed load
  - like/share/follow: ~0.5 per feed load
  - skip: ~7 per feed load

Events per feed load: ~50.5 events
Events per second (peak): 500K * 50.5 = ~25M events/sec

Event size: ~200 bytes average
Throughput: 25M * 200 B = 5 GB/sec ingest

Kafka cluster:
  - 6 brokers, 3 replicas
  - 100 partitions per topic
  - ~50 MB/sec per partition (well within Kafka limits)
  - Retention: 72 hours for reprocessing
  - Storage: 5 GB/s * 72h * 3600 = ~1.3 PB (with replication ~3.9 PB)
```

### 2.6 Bandwidth

```
Request payload (user context + device info): ~2 KB
Response payload (20 items with metadata): ~10 KB
Per request total: ~12 KB

Peak bandwidth: 500K * 12 KB = 6 GB/sec = 48 Gbps
With overhead (headers, TLS): ~60 Gbps
Per region (3 regions): ~20 Gbps per region edge

Internal traffic (feature store, model serving, ANN):
  - Feature lookups: ~100 KB per request (batched item features)
  - Model input: ~50 KB per request
  - ANN queries: ~5 KB per request
  - Internal bandwidth per request: ~155 KB
  - Peak internal: 500K * 155 KB = ~77.5 GB/sec = ~620 Gbps internal
```

---

## 3. High-Level Architecture

### 3.1 End-to-End Request Flow

```
User opens app → "For You" feed request
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           API Gateway / Edge                                │
│  - Auth, rate limit, geo-routing, experiment assignment                     │
│  - Attach: user_id, device, location, session_id, experiment_groups        │
│  - Latency budget: ~5ms                                                    │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Recommendation Orchestrator                           │
│  - Coordinates the full pipeline                                            │
│  - Manages latency budgets, fallbacks, pagination                          │
│  - Latency budget: ~5ms overhead                                           │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ├───────────────────────────────┐
         ▼                               ▼
┌─────────────────────┐    ┌─────────────────────────────────────────────────┐
│  Feature Store      │    │          Candidate Generation                   │
│  (user features     │    │                                                 │
│   fetched first)    │    │  ┌──────────┐ ┌──────────┐ ┌──────────┐       │
│                     │    │  │Two-Tower │ │ Collab   │ │ Social   │       │
│  Latency: ~5ms     │    │  │ ANN      │ │ Filter   │ │ Graph    │       │
│                     │    │  └──────────┘ └──────────┘ └──────────┘       │
│                     │    │  ┌──────────┐ ┌──────────┐ ┌──────────┐       │
│                     │    │  │Trending  │ │Cold Start│ │Explore   │       │
│                     │    │  └──────────┘ └──────────┘ └──────────┘       │
│                     │    │                                                 │
│                     │    │  Latency: ~30ms (parallel channels)             │
│                     │    │  Output: ~2000 deduplicated candidates          │
└─────────────────────┘    └─────────────────────────────────────────────────┘
         │                               │
         └───────────────┬───────────────┘
                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Feature Enrichment (Batch Fetch)                        │
│  - Fetch item features for all 2000 candidates                             │
│  - Compute cross-features (user-item affinity)                             │
│  - Latency: ~8ms (batched multi-get from Redis)                            │
└─────────────────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                   Stage 1: Pre-Ranking (Lightweight)                        │
│  - 2-layer MLP on basic features                                           │
│  - Scores 2000 candidates → top 500                                        │
│  - Runs on CPU                                                             │
│  - Latency: ~15ms                                                          │
└─────────────────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                   Stage 2: Heavy Ranking (Deep Model)                       │
│  - Deep & Cross Network v2 (multi-task)                                    │
│  - Scores 500 candidates with full feature set                             │
│  - Runs on GPU (A10G)                                                      │
│  - Latency: ~40ms                                                          │
└─────────────────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│              Stage 3: Re-Ranking / Policy Layer                             │
│  - Multi-objective score combination                                        │
│  - Diversity enforcement (MMR)                                              │
│  - Freshness boost, creator fairness                                       │
│  - Frequency capping, exploration injection                                │
│  - Content safety filtering                                                │
│  - Latency: ~10ms                                                          │
└─────────────────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Response Assembly                                     │
│  - First page: top 20 items                                                │
│  - Pre-compute next 2 pages (items 21-60) → cache                         │
│  - Log: impression events → Kafka                                          │
│  - Latency: ~3ms                                                           │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
    User sees feed
```

### 3.2 Latency Budget Breakdown

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                   Total Latency Budget: P99 ≤ 200ms                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────┐                                                   │
│  │ API Gateway         │  5ms   ████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   │
│  ├─────────────────────┤                                                   │
│  │ User Feature Fetch  │  5ms   ████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   │
│  ├─────────────────────┤        (parallel with candidate gen below)        │
│  │ Candidate Gen (ANN) │ 30ms   ██████████████████░░░░░░░░░░░░░░░░░░░░░   │
│  ├─────────────────────┤                                                   │
│  │ Item Feature Fetch  │  8ms   ██████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   │
│  ├─────────────────────┤                                                   │
│  │ Pre-Ranking (CPU)   │ 15ms   ██████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░   │
│  ├─────────────────────┤                                                   │
│  │ Heavy Ranking (GPU) │ 40ms   ████████████████████████████░░░░░░░░░░░   │
│  ├─────────────────────┤                                                   │
│  │ Re-Ranking + Policy │ 10ms   ███████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   │
│  ├─────────────────────┤                                                   │
│  │ Response Assembly   │  3ms   ██░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   │
│  ├─────────────────────┤                                                   │
│  │ Network (internal)  │  9ms   ██████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   │
│  └─────────────────────┘                                                   │
│                                                                             │
│  Sequential path total:  125ms (P50 ~85ms)                                 │
│  P99 with tail latency:  ~175ms (35ms buffer for retries/jitter)           │
│                                                                             │
│  Note: User feature fetch runs in parallel with candidate generation,      │
│  so they overlap. Effective sequential path:                               │
│    5 (gateway) + max(5, 30) (parallel) + 8 + 15 + 40 + 10 + 3 + 9        │
│    = 5 + 30 + 8 + 15 + 40 + 10 + 3 + 9 = 120ms P50                       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.3 Data Flow Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            ONLINE SERVING PATH                              │
│  (synchronous, latency-critical)                                           │
│                                                                             │
│  App → Gateway → Orchestrator → [CandGen + FeatureStore] → PreRank         │
│       → HeavyRank → ReRank → Response                                      │
└─────────────────────────────────────────────────────────────────────────────┘
                    │ impressions, engagements
                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          NEAR-REAL-TIME PATH                                │
│  (async, seconds latency)                                                  │
│                                                                             │
│  User Action → Event Collector → Kafka → Flink → Feature Store (Redis)    │
│                                       → Kafka → Flink → Engagement Agg    │
│                                       → Kafka → Session Store             │
└─────────────────────────────────────────────────────────────────────────────┘
                    │ daily aggregates, model training data
                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            OFFLINE / BATCH PATH                             │
│  (async, hours latency)                                                    │
│                                                                             │
│  Training Data (HDFS) → Model Training (GPU cluster)                       │
│       → Model Registry → Model Serving (canary → full rollout)             │
│                                                                             │
│  Engagement Logs → Spark → User/Item Aggregates → Feature Store (batch)   │
│                         → Embedding Retraining → ANN Index Rebuild         │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Candidate Generation

### 4.1 Two-Tower Model Architecture

The Two-Tower (dual encoder) model is the backbone of embedding-based retrieval. It independently encodes users and items into the same vector space, enabling fast ANN retrieval at serving time.

```
           User Tower                              Item Tower
    ┌─────────────────────┐                 ┌─────────────────────┐
    │                     │                 │                     │
    │  User ID embedding  │                 │  Item ID embedding  │
    │  Age bucket embed.  │                 │  Video embed (avg)  │
    │  Gender embedding   │                 │  Audio embed (avg)  │
    │  Country embedding  │                 │  Text embed (title) │
    │  Language embedding │                 │  Creator embedding  │
    │  Recent-10 items    │                 │  Category embedding │
    │    (avg pooling)    │                 │  Duration bucket    │
    │  Hour-of-day embed  │                 │  Language embedding │
    │  Device type embed  │                 │                     │
    │  Watch history      │                 │                     │
    │    (attention pool) │                 │                     │
    └─────────┬───────────┘                 └─────────┬───────────┘
              │                                       │
              ▼                                       ▼
    ┌─────────────────────┐                 ┌─────────────────────┐
    │  Concat + BatchNorm │                 │  Concat + BatchNorm │
    │  FC 512 + ReLU      │                 │  FC 512 + ReLU      │
    │  FC 256 + ReLU      │                 │  FC 256 + ReLU      │
    │  L2 Normalize       │                 │  L2 Normalize       │
    └─────────┬───────────┘                 └─────────┬───────────┘
              │                                       │
              ▼                                       ▼
         u ∈ R^256                              v ∈ R^256
              │                                       │
              └──────────────┬────────────────────────┘
                             │
                      score = u · v
                   (dot product similarity)
```

**Training:**

```python
import torch
import torch.nn as nn
import torch.nn.functional as F

class UserTower(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.user_id_embed = nn.Embedding(config.num_users, 64)
        self.age_embed = nn.Embedding(10, 8)       # 10 age buckets
        self.gender_embed = nn.Embedding(3, 4)      # M/F/Other
        self.country_embed = nn.Embedding(250, 16)
        self.lang_embed = nn.Embedding(100, 8)
        self.device_embed = nn.Embedding(5, 4)       # iOS/Android/Web/...
        self.hour_embed = nn.Embedding(24, 8)

        # Attention pooling for watch history (last 50 items)
        self.history_attention = nn.MultiheadAttention(
            embed_dim=64, num_heads=4, batch_first=True
        )
        self.history_item_embed = nn.Embedding(config.num_items, 64)

        # Recent-10 items: average pooling of item embeddings
        self.recent_item_embed = nn.Embedding(config.num_items, 64)

        # MLP tower
        input_dim = 64 + 8 + 4 + 16 + 8 + 4 + 8 + 64 + 64  # = 240
        self.fc1 = nn.Linear(input_dim, 512)
        self.bn1 = nn.BatchNorm1d(512)
        self.fc2 = nn.Linear(512, 256)
        self.bn2 = nn.BatchNorm1d(256)

    def forward(self, user_id, age_bucket, gender, country, language,
                device, hour, recent_items, history_items, history_mask):
        # Embed categorical features
        u = self.user_id_embed(user_id)            # [B, 64]
        a = self.age_embed(age_bucket)             # [B, 8]
        g = self.gender_embed(gender)              # [B, 4]
        c = self.country_embed(country)            # [B, 16]
        l = self.lang_embed(language)              # [B, 8]
        d = self.device_embed(device)              # [B, 4]
        h = self.hour_embed(hour)                  # [B, 8]

        # Recent 10 items: average pooling
        recent_emb = self.recent_item_embed(recent_items)  # [B, 10, 64]
        recent_avg = recent_emb.mean(dim=1)                # [B, 64]

        # Watch history: attention pooling
        hist_emb = self.history_item_embed(history_items)  # [B, 50, 64]
        query = u.unsqueeze(1)                             # [B, 1, 64]
        hist_attn, _ = self.history_attention(
            query, hist_emb, hist_emb,
            key_padding_mask=history_mask
        )
        hist_pooled = hist_attn.squeeze(1)                 # [B, 64]

        # Concat all features
        x = torch.cat([u, a, g, c, l, d, h, recent_avg, hist_pooled], dim=1)

        # MLP
        x = F.relu(self.bn1(self.fc1(x)))
        x = self.bn2(self.fc2(x))

        # L2 normalize
        x = F.normalize(x, p=2, dim=1)
        return x  # [B, 256]


class ItemTower(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.item_id_embed = nn.Embedding(config.num_items, 64)
        self.creator_embed = nn.Embedding(config.num_creators, 32)
        self.category_embed = nn.Embedding(config.num_categories, 16)
        self.lang_embed = nn.Embedding(100, 8)
        self.duration_embed = nn.Embedding(20, 8)  # 20 duration buckets

        # Pre-extracted content embeddings (frozen, from multimodal model)
        self.video_proj = nn.Linear(768, 64)   # Project CLIP visual to 64d
        self.audio_proj = nn.Linear(128, 32)   # Project audio features to 32d
        self.text_proj = nn.Linear(384, 32)    # Project sentence-BERT to 32d

        # MLP tower
        input_dim = 64 + 32 + 16 + 8 + 8 + 64 + 32 + 32  # = 256
        self.fc1 = nn.Linear(input_dim, 512)
        self.bn1 = nn.BatchNorm1d(512)
        self.fc2 = nn.Linear(512, 256)
        self.bn2 = nn.BatchNorm1d(256)

    def forward(self, item_id, creator_id, category, language,
                duration_bucket, video_embed, audio_embed, text_embed):
        i = self.item_id_embed(item_id)
        cr = self.creator_embed(creator_id)
        cat = self.category_embed(category)
        lang = self.lang_embed(language)
        dur = self.duration_embed(duration_bucket)

        vid = F.relu(self.video_proj(video_embed))
        aud = F.relu(self.audio_proj(audio_embed))
        txt = F.relu(self.text_proj(text_embed))

        x = torch.cat([i, cr, cat, lang, dur, vid, aud, txt], dim=1)

        x = F.relu(self.bn1(self.fc1(x)))
        x = self.bn2(self.fc2(x))
        x = F.normalize(x, p=2, dim=1)
        return x  # [B, 256]


class TwoTowerModel(nn.Module):
    """Trained with sampled softmax loss on (user, positive_item) pairs."""
    def __init__(self, config):
        super().__init__()
        self.user_tower = UserTower(config)
        self.item_tower = ItemTower(config)
        self.temperature = nn.Parameter(torch.tensor(0.07))

    def forward(self, user_features, pos_item_features, neg_item_features):
        user_emb = self.user_tower(**user_features)        # [B, 256]
        pos_emb = self.item_tower(**pos_item_features)     # [B, 256]
        neg_embs = self.item_tower(**neg_item_features)    # [B, K, 256]

        # Positive scores
        pos_score = (user_emb * pos_emb).sum(dim=1) / self.temperature  # [B]

        # Negative scores (in-batch negatives + hard negatives)
        neg_scores = torch.bmm(
            neg_embs, user_emb.unsqueeze(2)
        ).squeeze(2) / self.temperature  # [B, K]

        # Sampled softmax loss
        logits = torch.cat([pos_score.unsqueeze(1), neg_scores], dim=1)  # [B, K+1]
        labels = torch.zeros(logits.size(0), dtype=torch.long, device=logits.device)
        loss = F.cross_entropy(logits, labels)
        return loss
```

**Training data:** Positive pairs are (user, item_watched_>50%_completion). Negatives are in-batch negatives (other items in the batch) plus hard negatives (items the user was shown but skipped).

**Training schedule:** Daily retrain on last 14 days of engagement data. ~2 hours on 8x A100 GPUs.

### 4.2 Retrieval Channels

All channels run in **parallel** and their results are merged with deduplication.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Candidate Generation: 6 Channels                        │
│                    (all execute in parallel, ~30ms)                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Channel 1: Two-Tower ANN (embedding similarity)                           │
│  ├── Input: user embedding (computed via user tower at request time)        │
│  ├── Index: HNSW on 200M item embeddings                                   │
│  ├── Output: top-500 items by cosine similarity                            │
│  └── Latency: ~5ms (user tower 1ms + ANN search 3ms + network 1ms)        │
│                                                                             │
│  Channel 2: Collaborative Filtering (item-based CF)                        │
│  ├── Input: user's last 20 engaged items                                   │
│  ├── Index: pre-computed item-to-item similarity (top-100 neighbors/item)  │
│  ├── Lookup: for each of 20 items, fetch 100 neighbors = 2000 candidates  │
│  ├── Output: top-300 by aggregated similarity score (deduplicated)         │
│  └── Latency: ~8ms (Redis multi-get)                                      │
│                                                                             │
│  Channel 3: Social Graph                                                    │
│  ├── Input: user's followed creators (top 50 by recent engagement)         │
│  ├── Index: creator → recent items (last 48h, top by engagement)           │
│  ├── Output: top-200 items from followed creators                          │
│  └── Latency: ~10ms (graph service query + item lookup)                    │
│                                                                             │
│  Channel 4: Trending / Popularity                                          │
│  ├── Input: user's region, language, content preferences                   │
│  ├── Index: real-time trending items per (region, language, category)      │
│  ├── Updated: every 30 seconds via Flink streaming job                     │
│  ├── Output: top-200 trending items (filtered by user language/region)     │
│  └── Latency: ~3ms (Redis sorted set lookup)                              │
│                                                                             │
│  Channel 5: Cold-Start Items                                                │
│  ├── Input: items with < 1000 impressions and age < 48 hours               │
│  ├── Index: scored by content features + creator quality                   │
│  ├── Strategy: Thompson sampling for exploration (see Section 10)          │
│  ├── Output: 100-200 new items needing impression budget                   │
│  └── Latency: ~5ms                                                         │
│                                                                             │
│  Channel 6: Exploration / Serendipity                                      │
│  ├── Input: categories the user has NOT engaged with in 7 days            │
│  ├── Strategy: sample from top items in unexplored categories              │
│  ├── Fraction: ~5% of final feed                                          │
│  ├── Output: 100 diverse items from underexplored categories              │
│  └── Latency: ~5ms                                                         │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Candidate Merging and Deduplication

```python
from dataclasses import dataclass, field
from typing import Dict, List, Set

@dataclass
class Candidate:
    item_id: str
    score: float                   # channel-specific score (normalized 0-1)
    source_channels: List[str]     # which channels nominated this item
    source_scores: Dict[str, float] = field(default_factory=dict)

def merge_candidates(
    channel_results: Dict[str, List[Candidate]],
    channel_weights: Dict[str, float],
    max_candidates: int = 2000
) -> List[Candidate]:
    """
    Merge candidates from multiple channels with deduplication.
    Items appearing in multiple channels get a bonus (multi-signal boost).
    """
    # Channel weights (tunable via A/B testing)
    # Default weights:
    #   two_tower: 1.0, collab_filter: 0.8, social: 0.7,
    #   trending: 0.5, cold_start: 0.4, exploration: 0.3

    merged: Dict[str, Candidate] = {}

    for channel_name, candidates in channel_results.items():
        weight = channel_weights.get(channel_name, 0.5)

        for c in candidates:
            if c.item_id in merged:
                # Item seen before: accumulate weighted score + multi-signal bonus
                existing = merged[c.item_id]
                existing.source_channels.append(channel_name)
                existing.source_scores[channel_name] = c.score
                existing.score += c.score * weight
                # Multi-signal bonus: 10% boost per additional channel
                existing.score *= 1.10
            else:
                c.source_channels = [channel_name]
                c.source_scores = {channel_name: c.score}
                c.score = c.score * weight
                merged[c.item_id] = c

    # Sort by merged score, take top max_candidates
    sorted_candidates = sorted(merged.values(), key=lambda x: x.score, reverse=True)
    return sorted_candidates[:max_candidates]
```

### 4.4 HNSW Index Construction and Serving

```python
# Index construction (offline, runs daily after embedding retrain)
import hnswlib
import numpy as np

def build_hnsw_index(
    embeddings: np.ndarray,     # shape: [200M, 256], float32
    item_ids: np.ndarray,       # shape: [200M], int64
    index_path: str
):
    """
    Build HNSW index for 200M items.
    Memory: ~258 GB (see capacity estimates)
    Build time: ~4 hours on 64-core machine with 512 GB RAM
    """
    dim = embeddings.shape[1]  # 256
    num_items = embeddings.shape[0]  # 200M

    index = hnswlib.Index(space='ip', dim=dim)  # inner product (cosine on L2-normed)

    # HNSW parameters
    index.init_index(
        max_elements=num_items,
        ef_construction=400,  # Higher = better recall, slower build
        M=32                  # Connections per node. 32 = good recall/memory balance
    )

    # Add in batches of 1M to manage memory
    batch_size = 1_000_000
    for start in range(0, num_items, batch_size):
        end = min(start + batch_size, num_items)
        index.add_items(
            embeddings[start:end],
            ids=item_ids[start:end],
            num_threads=64
        )
        print(f"Indexed {end}/{num_items} items")

    index.save_index(index_path)
    return index


def serve_ann_query(
    index: hnswlib.Index,
    user_embedding: np.ndarray,  # shape: [1, 256]
    top_k: int = 500,
    ef_search: int = 200         # Search beam width (higher = better recall)
) -> tuple:
    """
    Query HNSW index. Returns top_k nearest items.

    Performance at 200M items, M=32, ef_search=200:
      - Recall@100: ~0.97 (97% of true nearest neighbors found)
      - Latency: ~2-3ms per query
      - QPS per machine: ~5000 (single-threaded per query, parallel across queries)
    """
    index.set_ef(ef_search)
    item_ids, distances = index.knn_query(user_embedding, k=top_k)
    # distances are inner products (higher = more similar for L2-normalized vectors)
    return item_ids[0], distances[0]
```

**Index refresh strategy:**

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     HNSW Index Refresh Pipeline                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Daily (02:00 UTC):                                                        │
│    1. Train Two-Tower model on yesterday's data          (~2 hours)        │
│    2. Compute new item embeddings for all 200M items     (~1 hour)         │
│    3. Build new HNSW index                               (~4 hours)        │
│    4. Upload index to object storage (S3)                (~30 min)         │
│    5. Rolling restart of ANN servers: load new index     (~30 min)         │
│       (blue-green: new replicas load new index,                            │
│        old replicas drain, no downtime)                                    │
│                                                                             │
│  Intra-day incremental (every hour):                                       │
│    - New items (uploaded in last hour): compute embeddings,                │
│      add to a small "fresh items" HNSW index (~5M items)                  │
│    - ANN query fans out to BOTH main index + fresh index                  │
│    - Fresh index merged into main index at next daily build               │
│                                                                             │
│  Item removal:                                                             │
│    - Moderation removes item → item_id added to bloom filter blacklist    │
│    - ANN results post-filtered against blacklist                          │
│    - Blacklisted items removed from index at next daily rebuild           │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 4.5 Item-Based Collaborative Filtering

Pre-computed item-to-item similarity stored in Redis:

```
Key:   item_neighbors:{item_id}
Value: Sorted set of (neighbor_item_id, similarity_score)
Size:  100 neighbors per item * (8B item_id + 4B score) = 1.2 KB per item
Total: 200M items * 1.2 KB = 240 GB in Redis

Computation: Run daily via Spark
  - Input: co-engagement matrix (items co-watched by same users within 1 hour)
  - Method: item-item cosine similarity on engagement vectors
  - Output: top-100 neighbors per item
```

```python
def collab_filter_retrieve(user_recent_items: List[str], top_k: int = 300) -> List[Candidate]:
    """
    For each of the user's last 20 engaged items, fetch similar items.
    Aggregate scores across seeds.
    """
    pipe = redis_client.pipeline()
    for item_id in user_recent_items[:20]:
        pipe.zrevrange(f"item_neighbors:{item_id}", 0, 99, withscores=True)
    results = pipe.execute()

    # Aggregate: items recommended by multiple seeds rank higher
    candidate_scores: Dict[str, float] = {}
    for seed_neighbors in results:
        for neighbor_id, sim_score in seed_neighbors:
            if neighbor_id not in user_recent_items:  # exclude already-seen
                candidate_scores[neighbor_id] = (
                    candidate_scores.get(neighbor_id, 0) + sim_score
                )

    # Sort and return top_k
    sorted_items = sorted(candidate_scores.items(), key=lambda x: x[1], reverse=True)
    return [
        Candidate(item_id=item_id, score=score, source_channels=["collab_filter"])
        for item_id, score in sorted_items[:top_k]
    ]
```

---

## 5. Feature Store Design

### 5.1 Feature Taxonomy

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Feature Categories                                 │
├───────────────────┬──────────────────┬──────────────────────────────────────┤
│  Update Cadence   │  Storage Layer   │  Examples                            │
├───────────────────┼──────────────────┼──────────────────────────────────────┤
│                   │                  │                                      │
│  REAL-TIME        │  Redis           │  Session items viewed (last 5)       │
│  (seconds)        │  (hot tier)      │  Session dwell times                 │
│                   │                  │  Session skip count                  │
│                   │                  │  Last action timestamp               │
│                   │                  │  In-session interest vector          │
│                   │                  │  Item impression count (last 1h)     │
│                   │                  │  Item like count (last 1h)           │
│                   │                  │                                      │
├───────────────────┼──────────────────┼──────────────────────────────────────┤
│                   │                  │                                      │
│  NEAR-REAL-TIME   │  Redis           │  User engagement rate (last 24h)     │
│  (minutes)        │  (hot tier)      │  Item CTR (rolling 6h)              │
│                   │                  │  Item completion rate (rolling 6h)   │
│                   │                  │  Creator activity score              │
│                   │                  │  User category affinity (session)    │
│                   │                  │                                      │
├───────────────────┼──────────────────┼──────────────────────────────────────┤
│                   │                  │                                      │
│  BATCH            │  Redis (cache)   │  User embedding (256d)               │
│  (hours/daily)    │  + DynamoDB      │  Item embedding (256d)               │
│                   │  (source)        │  User 7/30/90-day engagement stats  │
│                   │                  │  Item lifetime engagement stats      │
│                   │                  │  User demographic features           │
│                   │                  │  Item content embeddings (768d vis)  │
│                   │                  │  Creator quality score               │
│                   │                  │  Item quality score                  │
│                   │                  │  User-creator follow status          │
│                   │                  │                                      │
└───────────────────┴──────────────────┴──────────────────────────────────────┘
```

### 5.2 Feature Store Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Feature Store Architecture                              │
└─────────────────────────────────────────────────────────────────────────────┘

                    ┌──────────────────────────────────┐
                    │       Feature Serving API         │
                    │   get_user_features(user_id)      │
                    │   get_item_features(item_ids[])   │
                    │   get_cross_features(u_id, i_ids) │
                    │                                    │
                    │   P99 latency: 10ms               │
                    └──────────┬───────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │   L1 Cache   │  │  Redis Cluster│  │  DynamoDB    │
    │  (in-process │  │  (hot tier)   │  │  (cold tier) │
    │   LRU, 1GB)  │  │              │  │              │
    │              │  │  50 shards   │  │  On-demand   │
    │  TTL: 10s    │  │  5.2 TB      │  │  capacity    │
    │  Hit: ~30%   │  │  TTL: varies │  │              │
    │  Latency: 0ms│  │  Latency: 2ms│  │  Latency: 5ms│
    └──────────────┘  └──────────────┘  └──────────────┘
              │                │                │
              │           ┌────┘                │
              │           │                     │
              │    Populated by:                │
              │           │                     │
    ┌─────────┴───────────┴─────────────────────┴─────────────────────────────┐
    │                                                                          │
    │  REAL-TIME PIPELINE              BATCH PIPELINE                          │
    │                                                                          │
    │  User Action                     Spark Job (daily)                       │
    │       │                              │                                   │
    │       ▼                              ▼                                   │
    │  Event Collector               HDFS / Data Lake                          │
    │       │                              │                                   │
    │       ▼                              ▼                                   │
    │  Kafka (events topic)          Aggregate features                        │
    │       │                        (7/30/90-day stats)                       │
    │       ▼                              │                                   │
    │  Flink Streaming Job                 ▼                                   │
    │       │                        Write to DynamoDB                         │
    │       ├─► Update Redis              + populate Redis cache               │
    │       │   (session features,                                             │
    │       │    real-time counters)                                           │
    │       │                                                                  │
    │       └─► Write to Kafka                                                │
    │           (for downstream                                               │
    │            batch consumption)                                            │
    │                                                                          │
    └──────────────────────────────────────────────────────────────────────────┘
```

### 5.3 Redis Feature Schema

```python
# User real-time features (updated within seconds of each action)
# Key: user_rt:{user_id}
# Type: Hash
# TTL: 24 hours (reset on each update)
{
    "session_id": "sess_abc123",
    "session_items": "[item_1, item_2, item_3, item_4, item_5]",  # last 5
    "session_dwells": "[12.5, 45.2, 3.1, 28.7, 60.0]",          # seconds
    "session_skips": "2",
    "session_likes": "1",
    "session_start_ts": "1694000000",
    "last_action_ts": "1694000300",
    "session_category_counts": "{\"comedy\": 3, \"cooking\": 1, \"dance\": 1}",
    "session_interest_vec": "<base64-encoded 256d float16 vector>"  # 512 bytes
}

# User batch features (updated daily by Spark)
# Key: user_batch:{user_id}
# Type: Hash
# TTL: 48 hours (refreshed daily)
{
    "embedding": "<base64-encoded 256d float32 vector>",   # 1024 bytes
    "age_bucket": "3",
    "gender": "1",
    "country": "US",
    "language": "en",
    "device_type": "2",
    "watch_7d": "342",
    "like_7d": "45",
    "share_7d": "8",
    "follow_7d": "3",
    "avg_watch_pct_7d": "0.62",
    "avg_session_len_7d": "1200",
    "top_categories_30d": "{\"comedy\": 0.35, \"cooking\": 0.25, \"dance\": 0.15}",
    "creator_affinity": "{\"creator_1\": 0.9, \"creator_2\": 0.7, ...}"  # top 50
}

# Item features (mix of real-time counters and batch features)
# Key: item:{item_id}
# Type: Hash
# TTL: 7 days
{
    "embedding": "<base64-encoded 256d float32 vector>",
    "creator_id": "creator_abc",
    "category": "comedy",
    "language": "en",
    "duration_sec": "45",
    "upload_ts": "1693900000",
    "video_embed": "<base64 768d>",    # content embedding
    "audio_embed": "<base64 128d>",
    "text_embed": "<base64 384d>",
    "quality_score": "0.82",           # computed at upload from video analysis

    # Real-time counters (updated by Flink)
    "views_1h": "15234",
    "views_24h": "892341",
    "views_total": "4523891",
    "likes_total": "234567",
    "completion_rate_6h": "0.72",
    "like_rate_6h": "0.045",
    "share_rate_6h": "0.008",
    "skip_rate_6h": "0.18",

    # Moderation status
    "mod_status": "approved",          # approved / flagged / removed
    "is_minor_safe": "1"
}
```

### 5.4 Feature Serving Implementation

```python
import asyncio
import redis.asyncio as aioredis
from typing import Dict, List, Optional
import numpy as np
import struct
import base64

class FeatureStore:
    def __init__(self, redis_cluster: aioredis.RedisCluster):
        self.redis = redis_cluster
        self._local_cache = {}  # LRU cache, 1 GB max

    async def get_user_features(self, user_id: str) -> Dict:
        """
        Fetch user features from all tiers. P99 target: 5ms.
        Merges real-time + batch features into a single dict.
        """
        # Parallel fetch from both feature namespaces
        rt_future = self.redis.hgetall(f"user_rt:{user_id}")
        batch_future = self.redis.hgetall(f"user_batch:{user_id}")

        rt_features, batch_features = await asyncio.gather(rt_future, batch_future)

        if not batch_features:
            # Cold start user: return defaults
            return self._default_user_features(user_id)

        # Merge: real-time features override batch where both exist
        merged = {**batch_features, **rt_features}

        # Decode embedding
        if "embedding" in merged:
            merged["embedding"] = self._decode_embedding(merged["embedding"])

        return merged

    async def get_item_features_batch(
        self, item_ids: List[str]
    ) -> Dict[str, Dict]:
        """
        Batch fetch item features for up to 2000 items.
        Uses Redis pipeline for efficiency. P99 target: 8ms for 2000 items.
        """
        pipe = self.redis.pipeline()
        for item_id in item_ids:
            pipe.hgetall(f"item:{item_id}")

        results = await pipe.execute()

        features = {}
        for item_id, result in zip(item_ids, results):
            if result:
                # Filter out removed items
                if result.get("mod_status") == "removed":
                    continue
                features[item_id] = result
            # Items with no features are silently dropped (data issue, not crash)

        return features

    async def get_cross_features(
        self, user_features: Dict, item_features: Dict[str, Dict]
    ) -> Dict[str, Dict]:
        """
        Compute cross-features (user-item affinity) on the fly.
        No storage needed — derived from user and item features.
        """
        user_embedding = user_features.get("embedding")
        user_categories = user_features.get("top_categories_30d", {})
        user_creators = user_features.get("creator_affinity", {})

        cross = {}
        for item_id, item_feat in item_features.items():
            item_embedding = item_feat.get("embedding")

            # Embedding dot product (cosine similarity since both L2-normalized)
            if user_embedding is not None and item_embedding is not None:
                dot = float(np.dot(user_embedding, self._decode_embedding(item_embedding)))
            else:
                dot = 0.0

            # Category affinity
            item_category = item_feat.get("category", "")
            cat_affinity = float(user_categories.get(item_category, 0.0))

            # Creator affinity
            creator_id = item_feat.get("creator_id", "")
            creator_affinity = float(user_creators.get(creator_id, 0.0))

            # Item age in hours
            upload_ts = int(item_feat.get("upload_ts", 0))
            age_hours = max(0, (time.time() - upload_ts) / 3600)

            cross[item_id] = {
                "embedding_dot": dot,
                "category_affinity": cat_affinity,
                "creator_affinity": creator_affinity,
                "item_age_hours": age_hours,
                "has_followed_creator": 1 if creator_affinity > 0 else 0
            }

        return cross

    @staticmethod
    def _decode_embedding(b64_str: str) -> np.ndarray:
        raw = base64.b64decode(b64_str)
        return np.frombuffer(raw, dtype=np.float32)

    @staticmethod
    def _default_user_features(user_id: str) -> Dict:
        return {
            "embedding": np.zeros(256, dtype=np.float32),
            "age_bucket": "0",
            "gender": "0",
            "country": "unknown",
            "language": "en",
            "is_cold_start": True
        }
```

### 5.5 Real-Time Feature Update Pipeline (Flink)

```python
# Flink streaming job: user actions -> feature updates
# Processes ~25M events/sec across 200 task slots

from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.functions import MapFunction, KeyedProcessFunction
from pyflink.common import WatermarkStrategy

class UserSessionFeatureUpdater(KeyedProcessFunction):
    """
    Keyed by user_id. Maintains session state and emits feature updates
    to Redis within seconds of each action.
    """
    def open(self, runtime_context):
        # Session state: last 5 items, dwell times, action counts
        self.session_state = runtime_context.get_state(
            ValueStateDescriptor("session", Types.STRING())
        )
        self.redis = RedisClient(cluster_nodes=REDIS_NODES)

    def process_element(self, event, ctx):
        user_id = event.user_id
        state = self._load_state()

        if event.action == "impression":
            state["session_items"].append(event.item_id)
            state["session_items"] = state["session_items"][-5:]  # keep last 5

        elif event.action == "watch_complete":
            state["session_dwells"].append(event.dwell_time_sec)
            state["session_dwells"] = state["session_dwells"][-5:]
            # Update in-session interest vector (exponential moving average)
            item_embedding = self.redis.hget(f"item:{event.item_id}", "embedding")
            if item_embedding:
                item_vec = decode_embedding(item_embedding)
                alpha = 0.3  # weight of new signal
                old_vec = decode_embedding(state.get("session_interest_vec", zero_vec))
                new_vec = alpha * item_vec + (1 - alpha) * old_vec
                new_vec = new_vec / np.linalg.norm(new_vec)  # re-normalize
                state["session_interest_vec"] = encode_embedding(new_vec)

        elif event.action == "like":
            state["session_likes"] = state.get("session_likes", 0) + 1

        elif event.action == "skip":
            state["session_skips"] = state.get("session_skips", 0) + 1

        state["last_action_ts"] = str(int(event.timestamp))

        # Write updated features to Redis (async, fire-and-forget)
        self.redis.hmset(f"user_rt:{user_id}", state)
        self.redis.expire(f"user_rt:{user_id}", 86400)

        self._save_state(state)


class ItemEngagementCounter(KeyedProcessFunction):
    """
    Keyed by item_id. Aggregates engagement events in micro-batches
    (1-second tumbling windows) and flushes to Redis.
    """
    def open(self, runtime_context):
        self.views_buffer = runtime_context.get_state(
            ValueStateDescriptor("views", Types.LONG())
        )
        self.likes_buffer = runtime_context.get_state(
            ValueStateDescriptor("likes", Types.LONG())
        )
        self.completions_buffer = runtime_context.get_state(
            ValueStateDescriptor("completions", Types.LONG())
        )
        self.impressions_buffer = runtime_context.get_state(
            ValueStateDescriptor("impressions", Types.LONG())
        )
        self.redis = RedisClient(cluster_nodes=REDIS_NODES)

    def process_element(self, event, ctx):
        if event.action == "watch_start":
            self.views_buffer.update((self.views_buffer.value() or 0) + 1)
        elif event.action == "watch_complete":
            self.completions_buffer.update((self.completions_buffer.value() or 0) + 1)
        elif event.action == "like":
            self.likes_buffer.update((self.likes_buffer.value() or 0) + 1)
        elif event.action == "impression":
            self.impressions_buffer.update((self.impressions_buffer.value() or 0) + 1)

        # Register a timer to flush every 1 second
        ctx.timer_service().register_processing_time_timer(
            ctx.timestamp() + 1000  # 1 second
        )

    def on_timer(self, timestamp, ctx):
        item_id = ctx.get_current_key()
        pipe = self.redis.pipeline()

        views = self.views_buffer.value() or 0
        likes = self.likes_buffer.value() or 0
        completions = self.completions_buffer.value() or 0
        impressions = self.impressions_buffer.value() or 0

        if views > 0:
            pipe.hincrby(f"item:{item_id}", "views_1h", views)
            pipe.hincrby(f"item:{item_id}", "views_24h", views)
            pipe.hincrby(f"item:{item_id}", "views_total", views)
        if likes > 0:
            pipe.hincrby(f"item:{item_id}", "likes_total", likes)
        if impressions > 0:
            # Recompute rates
            total_impressions = int(self.redis.hget(f"item:{item_id}", "views_total") or 0) + views
            total_likes = int(self.redis.hget(f"item:{item_id}", "likes_total") or 0) + likes
            if total_impressions > 0:
                pipe.hset(f"item:{item_id}", "like_rate_6h",
                          str(round(total_likes / total_impressions, 4)))

        pipe.execute()

        # Clear buffers
        self.views_buffer.clear()
        self.likes_buffer.clear()
        self.completions_buffer.clear()
        self.impressions_buffer.clear()
```

### 5.6 Batch Feature Pipeline (Spark)

```python
# Daily Spark job: compute aggregated features for all users and items
# Runs at 04:00 UTC, processes ~6.4B events/day
# Runtime: ~2 hours on 200-node Spark cluster

from pyspark.sql import SparkSession
import pyspark.sql.functions as F

spark = SparkSession.builder.appName("feature_pipeline").getOrCreate()

# --- User batch features ---

events = spark.read.parquet("s3://data-lake/events/dt=2024-09-08/")

user_7d_features = (
    events
    .filter(F.col("event_date") >= F.date_sub(F.current_date(), 7))
    .groupBy("user_id")
    .agg(
        F.count(F.when(F.col("action") == "watch_complete", 1)).alias("watch_7d"),
        F.count(F.when(F.col("action") == "like", 1)).alias("like_7d"),
        F.count(F.when(F.col("action") == "share", 1)).alias("share_7d"),
        F.count(F.when(F.col("action") == "follow", 1)).alias("follow_7d"),
        F.avg(F.when(
            F.col("action") == "watch_complete", F.col("watch_pct")
        )).alias("avg_watch_pct_7d"),
        F.avg("session_duration_sec").alias("avg_session_len_7d"),
    )
)

# Top categories per user (last 30 days)
user_categories = (
    events
    .filter(
        (F.col("event_date") >= F.date_sub(F.current_date(), 30)) &
        (F.col("action") == "watch_complete")
    )
    .groupBy("user_id", "item_category")
    .count()
    .withColumn("rank", F.row_number().over(
        Window.partitionBy("user_id").orderBy(F.desc("count"))
    ))
    .filter(F.col("rank") <= 10)
    .groupBy("user_id")
    .agg(F.map_from_arrays(
        F.collect_list("item_category"),
        F.collect_list(F.col("count").cast("double") / F.sum("count").over(
            Window.partitionBy("user_id")
        ))
    ).alias("top_categories_30d"))
)

# Write to DynamoDB + populate Redis cache
user_features = user_7d_features.join(user_categories, "user_id", "left")
user_features.foreachPartition(write_to_dynamodb_and_redis)

# --- Item batch features ---
item_features = (
    events
    .filter(F.col("event_date") >= F.date_sub(F.current_date(), 7))
    .groupBy("item_id")
    .agg(
        F.sum("views").alias("views_total"),
        F.sum("likes").alias("likes_total"),
        F.avg("watch_pct").alias("avg_completion_rate"),
        F.countDistinct("user_id").alias("unique_viewers"),
    )
)
item_features.foreachPartition(write_to_dynamodb_and_redis)
```

---

## 6. Multi-Stage Ranking Pipeline

### 6.1 Stage 1: Pre-Ranking (Lightweight Scorer)

**Purpose:** Reduce 2000 candidates to 500 with a fast model.

**Model:** 2-layer MLP with ~2M parameters, runs on CPU.

```python
class PreRanker(nn.Module):
    """
    Lightweight model for fast scoring of ~2000 candidates.
    Uses a small subset of features for speed.

    Input features per candidate (~200 dims):
      - User-item embedding dot product (1)
      - Item engagement stats: views, like_rate, completion_rate, share_rate (4)
      - Item age bucket (1)
      - User category affinity for this item's category (1)
      - User-creator affinity (1)
      - Channel source one-hot (6)
      - Item quality score (1)
      - Item duration bucket (1)
      - User device type (1)
      - Hour of day (1)
      - User is_cold_start flag (1)
      ... total ~200 features after embeddings
    """
    def __init__(self, input_dim=200, hidden_dim=256):
        super().__init__()
        self.fc1 = nn.Linear(input_dim, hidden_dim)
        self.bn1 = nn.BatchNorm1d(hidden_dim)
        self.fc2 = nn.Linear(hidden_dim, 128)
        self.bn2 = nn.BatchNorm1d(128)
        self.output = nn.Linear(128, 1)
        self.dropout = nn.Dropout(0.1)

    def forward(self, x):
        # x: [batch_size, num_candidates, input_dim]
        B, N, D = x.shape
        x = x.view(B * N, D)

        x = F.relu(self.bn1(self.fc1(x)))
        x = self.dropout(x)
        x = F.relu(self.bn2(self.fc2(x)))
        x = self.output(x)  # [B*N, 1]

        return x.view(B, N)  # [B, N] scores

    def score_and_select(self, features, top_k=500):
        """Score all candidates, return top-k indices."""
        with torch.no_grad():
            scores = self.forward(features)  # [1, 2000]
            _, top_indices = torch.topk(scores, k=top_k, dim=1)
        return top_indices[0].tolist(), scores[0].tolist()
```

**Latency:** ~3ms for 2000 candidates on 8-core CPU (vectorized operations via PyTorch). 15ms budget includes feature assembly overhead.

### 6.2 Stage 2: Heavy Ranking (Deep & Cross Network v2)

**Purpose:** Accurately score 500 candidates with a deep model that captures complex feature interactions.

**Architecture:** DCN-v2 (Deep & Cross Network v2) with multi-task heads predicting multiple objectives.

```python
class CrossNetworkV2(nn.Module):
    """
    Cross Network v2: learns explicit feature crosses efficiently.
    Replaces the outer-product cross from DCN-v1 with a mixture of experts.
    """
    def __init__(self, input_dim, num_layers=3, num_experts=4):
        super().__init__()
        self.num_layers = num_layers
        self.num_experts = num_experts

        self.experts = nn.ModuleList([
            nn.ModuleList([
                nn.Linear(input_dim, input_dim, bias=False)
                for _ in range(num_experts)
            ])
            for _ in range(num_layers)
        ])

        self.gates = nn.ModuleList([
            nn.Linear(input_dim, num_experts, bias=False)
            for _ in range(num_layers)
        ])

        self.biases = nn.ParameterList([
            nn.Parameter(torch.zeros(input_dim))
            for _ in range(num_layers)
        ])

    def forward(self, x0):
        """
        x0: [batch_size, input_dim]  (the original input)
        Returns: [batch_size, input_dim]  (cross features of same dimension)
        """
        x = x0
        for layer_idx in range(self.num_layers):
            # Compute expert outputs
            expert_outputs = torch.stack([
                expert(x) for expert in self.experts[layer_idx]
            ], dim=1)  # [B, num_experts, D]

            # Gating
            gate_values = F.softmax(
                self.gates[layer_idx](x), dim=1
            )  # [B, num_experts]

            # Weighted sum of experts
            expert_mix = torch.einsum(
                "be,bed->bd", gate_values, expert_outputs
            )  # [B, D]

            # Cross: element-wise multiply with x0 + residual
            x = x0 * expert_mix + self.biases[layer_idx] + x

        return x


class HeavyRanker(nn.Module):
    """
    DCN-v2 with multi-task heads for multi-objective optimization.

    Input features per candidate (~800 dims total):
      - User embedding (256)
      - Item embedding (256)
      - User-item dot product (1)
      - User real-time session features (32)
      - User batch features (64)
      - Item engagement stats (16)
      - Item content features (64)
      - Cross features (16)
      - Context features (time, device, region) (16)
      - Category/creator embeddings (64)
      - Item freshness features (8)
      - Source channel features (6)
      ... ~800 total

    Outputs: 5 objective scores (probabilities)
      - P(watch_complete): probability user watches >50%
      - P(like): probability user likes
      - P(share): probability user shares
      - P(follow): probability user follows creator
      - P(skip): probability user skips within 2 seconds
    """
    def __init__(self, input_dim=800, deep_dims=[1024, 512, 256],
                 cross_layers=3, num_experts=4):
        super().__init__()

        # Cross Network (explicit feature interactions)
        self.cross_net = CrossNetworkV2(
            input_dim, num_layers=cross_layers, num_experts=num_experts
        )

        # Deep Network (implicit feature interactions)
        layers = []
        prev_dim = input_dim
        for dim in deep_dims:
            layers.extend([
                nn.Linear(prev_dim, dim),
                nn.BatchNorm1d(dim),
                nn.ReLU(),
                nn.Dropout(0.1)
            ])
            prev_dim = dim
        self.deep_net = nn.Sequential(*layers)

        # Combine cross + deep
        combined_dim = input_dim + deep_dims[-1]  # 800 + 256 = 1056

        # Multi-task heads (shared bottom, task-specific towers)
        self.shared_layer = nn.Sequential(
            nn.Linear(combined_dim, 512),
            nn.BatchNorm1d(512),
            nn.ReLU(),
            nn.Dropout(0.1)
        )

        # Task-specific towers
        self.watch_head = self._make_task_tower(512, 1)
        self.like_head = self._make_task_tower(512, 1)
        self.share_head = self._make_task_tower(512, 1)
        self.follow_head = self._make_task_tower(512, 1)
        self.skip_head = self._make_task_tower(512, 1)

    def _make_task_tower(self, input_dim, output_dim):
        return nn.Sequential(
            nn.Linear(input_dim, 128),
            nn.ReLU(),
            nn.Dropout(0.05),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Linear(64, output_dim),
            nn.Sigmoid()
        )

    def forward(self, x):
        """
        x: [batch_size, input_dim]
        Returns dict of task predictions, each [batch_size, 1]
        """
        # Parallel cross + deep
        cross_out = self.cross_net(x)        # [B, 800]
        deep_out = self.deep_net(x)          # [B, 256]

        # Concatenate
        combined = torch.cat([cross_out, deep_out], dim=1)  # [B, 1056]

        # Shared bottom
        shared = self.shared_layer(combined)  # [B, 512]

        # Task-specific predictions
        return {
            "p_watch": self.watch_head(shared).squeeze(1),     # [B]
            "p_like": self.like_head(shared).squeeze(1),       # [B]
            "p_share": self.share_head(shared).squeeze(1),     # [B]
            "p_follow": self.follow_head(shared).squeeze(1),   # [B]
            "p_skip": self.skip_head(shared).squeeze(1),       # [B]
        }
```

**Model size:**

```
Parameters breakdown:
  Cross Network:  800 * 800 * 4 experts * 3 layers = ~7.7M params
  Deep Network:   800*1024 + 1024*512 + 512*256     = ~1.5M params
  Shared layer:   1056*512                           = ~0.5M params
  Task towers:    5 * (512*128 + 128*64 + 64*1)     = ~0.4M params
  Total:          ~50M parameters

  float16 model size: 50M * 2 bytes = 100 MB (fits easily on GPU)
```

**Training:**

```python
class MultiTaskLoss(nn.Module):
    """
    Multi-task loss with uncertainty-based weighting (Kendall et al. 2018).
    Learns task weights automatically during training.
    """
    def __init__(self, num_tasks=5):
        super().__init__()
        # Log-variance parameters (learned)
        self.log_vars = nn.Parameter(torch.zeros(num_tasks))

    def forward(self, predictions, labels):
        losses = []
        task_names = ["p_watch", "p_like", "p_share", "p_follow", "p_skip"]

        for i, task in enumerate(task_names):
            pred = predictions[task]
            target = labels[task]

            # Binary cross-entropy loss per task
            task_loss = F.binary_cross_entropy(pred, target, reduction='mean')

            # Uncertainty weighting: L_total = sum(1/(2*sigma^2) * L_i + log(sigma))
            precision = torch.exp(-self.log_vars[i])
            weighted_loss = precision * task_loss + self.log_vars[i]
            losses.append(weighted_loss)

        return sum(losses), {
            task: loss.item()
            for task, loss in zip(task_names, losses)
        }
```

**Serving configuration (TorchServe):**

```yaml
# torchserve/config.properties
inference_address=http://0.0.0.0:8080
management_address=http://0.0.0.0:8081
number_of_gpu=1
job_queue_size=1000
batch_size=500          # Score 500 candidates per request
max_batch_delay=5       # ms; wait up to 5ms to fill batch
model_store=/models
load_models=heavy_ranker.mar

# GPU memory: ~100MB model + ~2GB activations for batch=500 = fits in 8GB A10G
# Throughput: ~125 requests/sec per GPU (8ms per batch of 500)
```

### 6.3 Stage 3: Re-Ranking / Policy Layer

```python
import random
from typing import List, Dict, Tuple

class ReRanker:
    """
    Re-ranking and policy enforcement layer.
    Takes heavy ranker scores + multi-objective weights and produces final feed.
    Runs on CPU, pure Python logic. Latency: ~10ms for 500 candidates.
    """
    def __init__(self, config: Dict):
        self.config = config

    def rerank(
        self,
        candidates: List[Dict],
        user_features: Dict,
        experiment_config: Dict
    ) -> List[Dict]:
        """
        Full re-ranking pipeline:
        1. Combine multi-objective scores
        2. Apply content safety filters
        3. Apply diversity constraints (MMR)
        4. Apply freshness boost
        5. Apply creator fairness
        6. Apply frequency capping
        7. Inject exploration candidates
        8. Return final ordered list
        """
        # Step 1: Multi-objective score combination
        weights = experiment_config.get("objective_weights", {
            "w_watch": 1.0,
            "w_like": 0.3,
            "w_share": 0.5,
            "w_follow": 0.2,
            "w_skip": -0.8
        })
        for c in candidates:
            c["final_score"] = (
                weights["w_watch"] * c["p_watch"] +
                weights["w_like"]  * c["p_like"] +
                weights["w_share"] * c["p_share"] +
                weights["w_follow"]* c["p_follow"] +
                weights["w_skip"]  * c["p_skip"]
            )

        # Step 2: Content safety filter
        candidates = self._filter_safety(candidates, user_features)

        # Step 3: Sort by final_score
        candidates.sort(key=lambda c: c["final_score"], reverse=True)

        # Step 4: Apply MMR for diversity
        candidates = self._apply_mmr_diversity(candidates, lambda_param=0.7)

        # Step 5: Freshness boost
        candidates = self._apply_freshness_boost(candidates)

        # Step 6: Creator fairness
        candidates = self._enforce_creator_fairness(candidates)

        # Step 7: Frequency capping
        session_seen = set(user_features.get("session_items", []))
        candidates = [c for c in candidates if c["item_id"] not in session_seen]

        # Step 8: Diversity constraint enforcement
        candidates = self._enforce_consecutive_diversity(candidates)

        # Step 9: Exploration injection (5% of slots)
        candidates = self._inject_exploration(candidates, experiment_config)

        return candidates[:60]  # 3 pages of 20

    def _filter_safety(self, candidates, user_features):
        """Remove unsafe content. Suppress flagged content for minors."""
        is_minor = user_features.get("age_bucket", 99) < 2  # bucket 0-1 = under 18
        filtered = []
        for c in candidates:
            if c.get("mod_status") == "removed":
                continue
            if c.get("mod_status") == "flagged" and is_minor:
                continue
            if c.get("mod_status") == "flagged":
                c["final_score"] *= 0.3  # heavy de-rank
            filtered.append(c)
        return filtered

    def _apply_mmr_diversity(
        self, candidates: List[Dict], lambda_param: float = 0.7, top_k: int = 100
    ) -> List[Dict]:
        """
        Maximal Marginal Relevance (MMR):
        Select items that are both relevant (high score) and diverse
        (dissimilar to already-selected items).

        MMR(i) = lambda * score(i) - (1 - lambda) * max_j_in_S sim(i, j)

        lambda=0.7 means 70% weight on relevance, 30% on diversity.
        """
        if not candidates:
            return candidates

        selected = [candidates[0]]
        remaining = candidates[1:]

        while len(selected) < top_k and remaining:
            best_mmr = -float('inf')
            best_idx = 0

            for idx, candidate in enumerate(remaining):
                relevance = candidate["final_score"]

                # Max similarity to any already-selected item
                max_sim = max(
                    self._item_similarity(candidate, s)
                    for s in selected
                )

                mmr = lambda_param * relevance - (1 - lambda_param) * max_sim

                if mmr > best_mmr:
                    best_mmr = mmr
                    best_idx = idx

            selected.append(remaining.pop(best_idx))

        return selected + remaining  # append rest in case we need them

    def _item_similarity(self, item_a: Dict, item_b: Dict) -> float:
        """
        Similarity = weighted combination of:
        - Same category: 0.5
        - Same creator: 0.8
        - Embedding cosine similarity: 0-1
        """
        sim = 0.0
        if item_a.get("category") == item_b.get("category"):
            sim += 0.5
        if item_a.get("creator_id") == item_b.get("creator_id"):
            sim += 0.8
        # Embedding similarity (pre-computed or approximated)
        emb_sim = item_a.get("embedding_dot_with", {}).get(item_b["item_id"], 0)
        sim += emb_sim * 0.3
        return min(sim, 1.0)

    def _apply_freshness_boost(self, candidates: List[Dict]) -> List[Dict]:
        """
        Boost items < 24 hours old:
          - 0-6 hours:  score *= 1.3
          - 6-12 hours: score *= 1.2
          - 12-24 hours: score *= 1.1
          - 24+ hours: no boost
        """
        import time
        now = time.time()
        for c in candidates:
            age_hours = (now - c.get("upload_ts", 0)) / 3600
            if age_hours < 6:
                c["final_score"] *= 1.3
            elif age_hours < 12:
                c["final_score"] *= 1.2
            elif age_hours < 24:
                c["final_score"] *= 1.1
        candidates.sort(key=lambda c: c["final_score"], reverse=True)
        return candidates

    def _enforce_creator_fairness(self, candidates: List[Dict]) -> List[Dict]:
        """
        Ensure no single creator dominates the feed.
        Max 3 items per creator in the top 60.
        """
        creator_counts = {}
        result = []
        overflow = []
        for c in candidates:
            creator = c.get("creator_id", "unknown")
            count = creator_counts.get(creator, 0)
            if count < 3:
                result.append(c)
                creator_counts[creator] = count + 1
            else:
                overflow.append(c)
        return result + overflow

    def _enforce_consecutive_diversity(self, candidates: List[Dict]) -> List[Dict]:
        """
        No more than 2 consecutive items from the same creator or category.
        Swap violating items with the next non-violating item.
        """
        if len(candidates) < 3:
            return candidates

        for i in range(2, len(candidates)):
            # Check creator consecutive
            if (candidates[i].get("creator_id") ==
                candidates[i-1].get("creator_id") ==
                candidates[i-2].get("creator_id")):
                # Find next item with different creator
                for j in range(i + 1, len(candidates)):
                    if candidates[j].get("creator_id") != candidates[i].get("creator_id"):
                        candidates[i], candidates[j] = candidates[j], candidates[i]
                        break

            # Check category consecutive
            if (candidates[i].get("category") ==
                candidates[i-1].get("category") ==
                candidates[i-2].get("category")):
                for j in range(i + 1, len(candidates)):
                    if candidates[j].get("category") != candidates[i].get("category"):
                        candidates[i], candidates[j] = candidates[j], candidates[i]
                        break

        return candidates

    def _inject_exploration(
        self, candidates: List[Dict], experiment_config: Dict
    ) -> List[Dict]:
        """
        Insert exploration candidates at defined positions.
        Exploration rate: 5% (configurable via experiment).
        Positions: every 20th slot (position 5, 25, 45).
        """
        explore_rate = experiment_config.get("exploration_rate", 0.05)
        explore_positions = experiment_config.get("explore_positions", [5, 25, 45])

        explore_items = [c for c in candidates if "exploration" in c.get("source_channels", [])]
        non_explore = [c for c in candidates if "exploration" not in c.get("source_channels", [])]

        result = list(non_explore)
        for pos in explore_positions:
            if explore_items and pos < len(result):
                result.insert(pos, explore_items.pop(0))

        return result
```

---

## 7. Multi-Objective Scoring

### 7.1 Scoring Formula

The final ranking score combines multiple predicted probabilities into a single scalar:

```
final_score = w1 * P(watch_complete)
            + w2 * P(like)
            + w3 * P(share)
            + w4 * P(follow)
            - w5 * P(skip)
            + w6 * freshness_boost
            + w7 * quality_score
```

**Default weights (tuned via offline experiments + A/B testing):**

| Weight | Objective | Default | Range | Business Rationale |
|--------|-----------|---------|-------|--------------------|
| w1 | P(watch_complete) | 1.0 | 0.5 - 2.0 | Primary engagement; drives session time |
| w2 | P(like) | 0.3 | 0.1 - 0.8 | Explicit positive signal; low noise |
| w3 | P(share) | 0.5 | 0.1 - 1.0 | Viral growth; new user acquisition |
| w4 | P(follow) | 0.2 | 0.05 - 0.5 | Creator ecosystem health |
| w5 | P(skip) | 0.8 | 0.3 - 1.5 | Negative signal; user dissatisfaction |
| w6 | freshness_boost | varies | 0.0 - 0.5 | Surface new content; encourage creation |
| w7 | quality_score | 0.1 | 0.0 - 0.3 | Prevent race to bottom |

### 7.2 Weight Tuning Without Retraining

The key insight: the model predicts each objective independently. The combination weights are applied post-inference. This decouples ML from product decisions.

```python
class ObjectiveWeightManager:
    """
    Manages objective weights per experiment group.
    Weights are stored in a config service (e.g., LaunchDarkly)
    and fetched at request time with sub-1ms latency (cached locally).
    """
    def __init__(self, config_client):
        self.config_client = config_client
        self._cache = {}
        self._cache_ttl = 30  # seconds

    def get_weights(self, experiment_groups: List[str]) -> Dict[str, float]:
        """
        Return objective weights for this user's experiment groups.
        If user is in a weight experiment, return the experiment weights.
        Otherwise return default weights.
        """
        for group in experiment_groups:
            if group.startswith("objective_weights_"):
                # User is in a weight A/B test
                weights = self.config_client.get_json(
                    f"experiments/{group}/weights"
                )
                if weights:
                    return weights

        return self._default_weights()

    def _default_weights(self) -> Dict[str, float]:
        return {
            "w_watch": 1.0,
            "w_like": 0.3,
            "w_share": 0.5,
            "w_follow": 0.2,
            "w_skip": -0.8
        }
```

**Experiment example:** Testing whether boosting share probability improves weekly user growth:

```yaml
# experiment_config.yaml
experiment:
  name: "boost_share_weight_v2"
  id: "exp_20240908_share_boost"
  start_date: "2024-09-08"
  end_date: "2024-09-22"
  traffic_allocation: 10%  # 10% of users

  control:
    weights:
      w_watch: 1.0
      w_like: 0.3
      w_share: 0.5     # default
      w_follow: 0.2
      w_skip: -0.8

  treatment_a:
    weights:
      w_watch: 1.0
      w_like: 0.3
      w_share: 0.8     # boosted
      w_follow: 0.2
      w_skip: -0.8

  treatment_b:
    weights:
      w_watch: 1.0
      w_like: 0.3
      w_share: 1.2     # aggressively boosted
      w_follow: 0.2
      w_skip: -0.8

  metrics:
    primary:
      - "share_rate_per_session"     # should increase
      - "watch_time_per_session"     # guardrail: must not drop > 2%
    guardrail:
      - "day7_retention"             # must not drop > 0.1%
      - "report_rate"                # must not increase > 5%
```

### 7.3 Pareto-Optimal Weight Search

For finding the right weight balance offline before A/B testing:

```python
import numpy as np
from scipy.optimize import minimize

def pareto_weight_search(
    predictions: np.ndarray,  # [N_samples, 5] model predictions
    labels: np.ndarray,       # [N_samples, 5] ground truth
    metric_fn,                # function that computes aggregate metrics
    n_trials: int = 1000
):
    """
    Search for Pareto-optimal weight configurations using
    multi-objective optimization on historical data.

    Returns a set of weight configs that are not dominated
    (no other config improves all objectives simultaneously).
    """
    pareto_front = []

    for _ in range(n_trials):
        # Random weight sample (Dirichlet for positive weights, uniform for skip)
        w_pos = np.random.dirichlet([1, 1, 1, 1])  # watch, like, share, follow
        w_skip = np.random.uniform(0.3, 1.5)

        weights = {
            "w_watch": w_pos[0] * 3.0,   # scale up since primary
            "w_like": w_pos[1] * 1.5,
            "w_share": w_pos[2] * 2.0,
            "w_follow": w_pos[3] * 1.0,
            "w_skip": -w_skip
        }

        # Compute final scores with these weights
        final_scores = (
            weights["w_watch"] * predictions[:, 0] +
            weights["w_like"] * predictions[:, 1] +
            weights["w_share"] * predictions[:, 2] +
            weights["w_follow"] * predictions[:, 3] +
            weights["w_skip"] * predictions[:, 4]
        )

        # Evaluate: simulate ranking and compute metrics
        metrics = metric_fn(final_scores, labels)
        # metrics = {"watch_time": X, "like_rate": Y, "share_rate": Z, "diversity": W}

        pareto_front.append((weights, metrics))

    # Filter to Pareto front (non-dominated solutions)
    return _extract_pareto_front(pareto_front)
```

---

## 8. Real-Time Feedback Loop

### 8.1 End-to-End Data Flow

This is the critical path: a user action must change subsequent recommendations within seconds.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│               REAL-TIME FEEDBACK LOOP: Complete Data Flow                   │
│               (User action → changed recommendation in < 5 seconds)        │
└─────────────────────────────────────────────────────────────────────────────┘

 T+0ms: User skips video #42 after watching 1.5 seconds
         │
         ▼
 T+50ms: Client SDK batches the event and sends to Event Collector
         {
           "user_id": "u_123",
           "session_id": "sess_abc",
           "item_id": "item_42",
           "action": "skip",
           "dwell_time_sec": 1.5,
           "watch_pct": 0.05,     // 1.5s of a 30s video
           "timestamp": 1694000300,
           "client_ts": 1694000300050
         }
         │
         ▼
 T+100ms: Event Collector (HTTP endpoint, stateless)
          - Validates event schema
          - Enriches: adds server_ts, region, device_info
          - Publishes to Kafka topic: "user_actions"
          - Returns 202 Accepted to client
         │
         ▼
 T+200ms: Kafka "user_actions" topic (100 partitions, keyed by user_id)
          - Partition = hash(user_id) % 100
          - Ensures ordering per user
          - Replication factor 3, acks=all
         │
         ├─────────────────────────────────────────────┐
         ▼                                             ▼
 T+500ms: Flink Job: UserSessionFeatureUpdater    Flink Job: ItemEngagementCounter
          (keyed by user_id)                       (keyed by item_id)
          │                                             │
          │  Updates:                                   │  Updates:
          │  - session_items: [42, 41, 40, 39, 38]     │  - item:42 skip_count += 1
          │  - session_dwells: [1.5, 45, 3, 28, 60]    │  - item:42 skip_rate recalc
          │  - session_skips: 3                         │  - item:42 completion_rate_6h
          │  - session_interest_vec updated             │
          │    (decayed, since skip indicates           │
          │     negative signal for this topic)         │
          │  - last_action_ts: 1694000300               │
          │                                             │
          ▼                                             ▼
 T+800ms: Redis HMSET user_rt:u_123              Redis HINCRBY item:42 ...
          │
          │  Feature now available in Redis
          │
 T+1000ms: (Meanwhile, client auto-advances to next video)
          │
 T+2000ms: User scrolls past video #43 (next in pre-fetched list)
           Client requests next page of feed
          │
          ▼
 T+2050ms: Recommendation Orchestrator receives request
           - Reads user_rt:u_123 from Redis → SEES the skip at T+0
           - Session features now reflect:
             * High skip count → user is not engaged with current topic
             * Short dwell times → interest may be shifting
             * Updated session interest vector → moved away from skipped topic
          │
          ▼
 T+2100ms: Candidate Generation
           - Two-Tower ANN now uses updated session interest vector
             → retrieves candidates DIFFERENT from what was already
               generating before the skip
           - Exploration channel weight may increase if user
             appears to be in a "seeking" state (many skips)
          │
          ▼
 T+2200ms: Heavy Ranker scores candidates
           - Input features include updated session skip count
           - Cross features include negative signal for item_42's category
           - P(skip) prediction higher for similar items → they rank lower
          │
          ▼
 T+2300ms: Re-ranking applies diversity boost for non-recent categories
          │
          ▼
 T+2350ms: Response returned with new recommendations that
           DE-PRIORITIZE the category of the skipped video
           and SURFACE alternatives from other categories

 Total latency: action at T+0, changed recommendation seen at T+2350ms
                Feature propagation: ~800ms
                Next request (user-dependent): ~2000ms
                Recommendation pipeline: ~350ms
                ────────────────────────────────
                Effective feedback delay: < 3 seconds
```

### 8.2 Session State Management

```python
class SessionManager:
    """
    Manages per-session state that influences ranking.
    State is stored in Redis with session_id as the key prefix.
    TTL: 4 hours (sessions rarely last longer).
    """
    def __init__(self, redis_client):
        self.redis = redis_client

    async def get_session_context(self, session_id: str) -> Dict:
        """
        Retrieve session context for ranking decisions.
        Called by the orchestrator on each feed request.
        """
        pipe = self.redis.pipeline()
        pipe.get(f"session:{session_id}:shown_items")      # bloom filter or set
        pipe.get(f"session:{session_id}:shown_creators")
        pipe.get(f"session:{session_id}:shown_categories")
        pipe.get(f"session:{session_id}:engagement_rate")
        pipe.get(f"session:{session_id}:skip_streak")
        pipe.get(f"session:{session_id}:request_count")

        (shown_items, shown_creators, shown_categories,
         engagement_rate, skip_streak, request_count) = await pipe.execute()

        return {
            "shown_items": decode_set(shown_items),
            "shown_creators": decode_set(shown_creators),
            "shown_categories": decode_set(shown_categories),
            "engagement_rate": float(engagement_rate or 0.5),
            "skip_streak": int(skip_streak or 0),
            "request_count": int(request_count or 0),
            "interest_drift_detected": int(skip_streak or 0) >= 3
        }

    async def update_session_on_impression(
        self, session_id: str, items: List[Dict]
    ):
        """Called when a page of items is shown to the user."""
        pipe = self.redis.pipeline()

        for item in items:
            pipe.sadd(f"session:{session_id}:shown_items", item["item_id"])
            pipe.sadd(f"session:{session_id}:shown_creators", item["creator_id"])
            pipe.sadd(f"session:{session_id}:shown_categories", item["category"])

        pipe.incr(f"session:{session_id}:request_count")

        # Set TTL on all keys
        for suffix in ["shown_items", "shown_creators", "shown_categories",
                        "engagement_rate", "skip_streak", "request_count"]:
            pipe.expire(f"session:{session_id}:{suffix}", 14400)  # 4 hours

        await pipe.execute()

    async def detect_interest_drift(self, session_id: str, user_id: str) -> bool:
        """
        Detect if user's interests are shifting mid-session.
        Signals:
          - 3+ consecutive skips
          - Dwell time dropped below 5s for last 3 items
          - Engagement rate dropped 50%+ vs session average
        """
        skip_streak = int(await self.redis.get(
            f"session:{session_id}:skip_streak"
        ) or 0)

        if skip_streak >= 3:
            return True

        rt_features = await self.redis.hgetall(f"user_rt:{user_id}")
        dwells = json.loads(rt_features.get("session_dwells", "[]"))
        if len(dwells) >= 3 and all(d < 5.0 for d in dwells[-3:]):
            return True

        return False
```

### 8.3 Adaptive Behavior on Feedback

```python
class AdaptiveOrchestrator:
    """
    Adjusts the recommendation pipeline based on real-time session signals.
    """
    def adjust_pipeline(self, session_context: Dict) -> Dict:
        """
        Returns pipeline parameter overrides based on session state.
        """
        overrides = {}

        # If user is in a skip streak, increase exploration
        if session_context.get("interest_drift_detected"):
            overrides["exploration_rate"] = 0.15  # 3x normal
            overrides["candidate_diversity_lambda"] = 0.5  # more diversity
            overrides["trending_channel_weight"] = 0.8  # more trending
            overrides["two_tower_channel_weight"] = 0.6  # less personalized

        # If user is deeply engaged (high watch completion), lean into signal
        if session_context.get("engagement_rate", 0) > 0.8:
            overrides["two_tower_channel_weight"] = 1.2
            overrides["exploration_rate"] = 0.03  # reduce exploration

        # If session is long (>50 requests), increase diversity to avoid fatigue
        if session_context.get("request_count", 0) > 50:
            overrides["candidate_diversity_lambda"] = 0.5
            overrides["freshness_boost_multiplier"] = 1.5

        return overrides
```

---

## 9. Cold-Start Strategies

### 9.1 New User Cold Start

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     New User Cold Start Progression                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Phase 0: First request ever (0 interactions)                              │
│  ├── Candidate sources:                                                    │
│  │   ├── 40% Trending in user's region/language                            │
│  │   ├── 30% Globally popular (last 48h, high quality score)               │
│  │   ├── 20% Diverse category sample (seed exploration)                    │
│  │   └── 10% Random from approved new-item pool                           │
│  ├── Ranking: popularity-based (no personalization)                        │
│  └── Key signal collected: which categories user watches vs skips          │
│                                                                             │
│  Phase 1: After 5-10 interactions                                          │
│  ├── Candidate sources:                                                    │
│  │   ├── 30% Trending                                                      │
│  │   ├── 30% Content-based (categories user engaged with)                 │
│  │   ├── 20% Two-Tower (user embedding from limited history)              │
│  │   └── 20% Exploration                                                  │
│  ├── Ranking: lightweight model with limited features                     │
│  └── User embedding: "warm start" from average of engaged item embeddings │
│                                                                             │
│  Phase 2: After 20-50 interactions                                         │
│  ├── Candidate sources: full pipeline with reduced Two-Tower weight        │
│  ├── Ranking: full heavy ranker with partial feature set                  │
│  └── User embedding: reasonable quality, updated next daily retrain       │
│                                                                             │
│  Phase 3: After 100+ interactions (graduated, "warm" user)                │
│  ├── Full pipeline, all channels, full feature set                        │
│  └── User embedding: high quality, daily refresh                          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Warm-start embedding for new users:**

```python
def compute_warm_start_embedding(
    engaged_item_ids: List[str],
    item_embeddings: Dict[str, np.ndarray],
    engagement_weights: List[float]
) -> np.ndarray:
    """
    Compute a provisional user embedding from their first few interactions.
    Weighted average of engaged item embeddings, with more recent items
    weighted higher.

    Called after each interaction in the first 50 interactions.
    Result stored in user_rt:{user_id}:warm_embedding in Redis.
    """
    if not engaged_item_ids:
        return np.zeros(256, dtype=np.float32)

    embeddings = []
    weights = []
    for item_id, weight in zip(engaged_item_ids, engagement_weights):
        if item_id in item_embeddings:
            embeddings.append(item_embeddings[item_id])
            weights.append(weight)

    if not embeddings:
        return np.zeros(256, dtype=np.float32)

    embeddings = np.array(embeddings)  # [N, 256]
    weights = np.array(weights)        # [N]
    weights = weights / weights.sum()  # normalize

    user_embedding = np.average(embeddings, axis=0, weights=weights)
    user_embedding = user_embedding / np.linalg.norm(user_embedding)  # L2 normalize

    return user_embedding
```

### 9.2 New Item Cold Start

New items lack engagement statistics, so they cannot be scored by the ranker's engagement features. We use a combination of content-based scoring and Thompson sampling for exploration.

```python
class NewItemExplorer:
    """
    Manages cold-start item exploration using Thompson Sampling.
    Each new item has a Beta distribution prior on its engagement rate.
    As we observe impressions and positive engagements, the posterior updates.
    """
    def __init__(self, redis_client):
        self.redis = redis_client

    async def get_cold_start_candidates(
        self, n: int = 200, region: str = "US", language: str = "en"
    ) -> List[Candidate]:
        """
        Select new items using Thompson Sampling.

        Each item has:
          - alpha: number of positive engagements (watches > 50%) + prior (1)
          - beta: number of negative engagements (skips/short watches) + prior (1)

        Thompson Sampling draws a sample from Beta(alpha, beta) for each item
        and selects items with the highest sampled values. This naturally
        balances exploration (uncertain items with few observations get wide
        distributions) with exploitation (items with proven engagement get
        high means).
        """
        # Get all cold-start items (< 1000 impressions, < 48h old)
        cold_items = await self._get_cold_item_pool(region, language)

        if not cold_items:
            return []

        # Thompson sampling
        sampled_scores = []
        for item in cold_items:
            alpha = item.get("positive_engagements", 0) + 1  # Beta prior
            beta = item.get("negative_engagements", 0) + 1

            # Draw from Beta distribution
            sampled_rate = np.random.beta(alpha, beta)

            # Boost by content quality score (prior knowledge)
            quality = item.get("quality_score", 0.5)
            creator_quality = item.get("creator_quality_score", 0.5)

            # Combined score: Thompson sample + content priors
            score = (
                0.6 * sampled_rate +
                0.25 * quality +
                0.15 * creator_quality
            )

            sampled_scores.append((item, score))

        # Sort by sampled score, return top N
        sampled_scores.sort(key=lambda x: x[1], reverse=True)

        return [
            Candidate(
                item_id=item["item_id"],
                score=score,
                source_channels=["cold_start"]
            )
            for item, score in sampled_scores[:n]
        ]

    async def update_item_engagement(
        self, item_id: str, is_positive: bool
    ):
        """
        Update Beta distribution parameters after an engagement observation.
        Called by the Flink streaming job for items in cold-start pool.
        """
        if is_positive:
            await self.redis.hincrby(f"cold_item:{item_id}", "positive_engagements", 1)
        else:
            await self.redis.hincrby(f"cold_item:{item_id}", "negative_engagements", 1)

        # Graduate item out of cold-start when it has enough data
        impressions = await self.redis.hincrby(f"cold_item:{item_id}", "impressions", 1)
        if impressions >= 1000:
            # Item has enough data for normal ranking pipeline
            await self.redis.delete(f"cold_item:{item_id}")
            # Mark item as graduated in item features
            await self.redis.hset(f"item:{item_id}", "is_cold_start", "0")

    async def _get_cold_item_pool(
        self, region: str, language: str
    ) -> List[Dict]:
        """
        Get pool of cold-start items filtered by region and language.
        Maintained by a Flink job that indexes new items as they arrive.
        Stored as a Redis sorted set, scored by upload timestamp.
        """
        item_ids = await self.redis.zrevrangebyscore(
            f"cold_items:{region}:{language}",
            max="+inf",
            min=int(time.time()) - 48 * 3600,  # last 48 hours
            start=0,
            num=5000  # max pool size to consider
        )

        pipe = self.redis.pipeline()
        for item_id in item_ids:
            pipe.hgetall(f"cold_item:{item_id}")

        results = await pipe.execute()

        return [
            {**r, "item_id": item_id}
            for item_id, r in zip(item_ids, results)
            if r and r.get("mod_status") == "approved"
        ]
```

### 9.3 New Creator Cold Start

```python
class NewCreatorBootstrap:
    """
    New creators get:
    1. Content-based scoring from video features (no engagement data needed)
    2. Similarity matching to established creators (same category/style)
    3. Minimum exposure guarantee: first N items get at least M impressions
    """
    MINIMUM_IMPRESSIONS_PER_ITEM = 500
    ITEMS_WITH_GUARANTEE = 5  # First 5 items

    async def get_creator_quality_prior(
        self, creator_id: str, item_features: Dict
    ) -> float:
        """
        Estimate new creator quality from content features and
        similar established creators.
        """
        # 1. Content quality signals (from upload-time analysis)
        video_quality = float(item_features.get("video_quality_score", 0.5))
        audio_quality = float(item_features.get("audio_quality_score", 0.5))
        production_score = 0.6 * video_quality + 0.4 * audio_quality

        # 2. Similar creator performance (embedding-based)
        creator_embedding = await self._get_creator_embedding(creator_id)
        if creator_embedding is not None:
            similar_creators = await self._find_similar_creators(
                creator_embedding, top_k=10
            )
            # Average engagement rate of similar established creators
            avg_rate = np.mean([c["avg_engagement_rate"] for c in similar_creators])
        else:
            avg_rate = 0.3  # population average

        # 3. Category baseline
        category = item_features.get("category", "other")
        category_avg = await self._get_category_avg_engagement(category)

        # Weighted combination
        quality_prior = (
            0.4 * production_score +
            0.35 * avg_rate +
            0.25 * category_avg
        )

        return quality_prior

    async def check_exposure_guarantee(
        self, creator_id: str, item_id: str
    ) -> bool:
        """
        Returns True if this item still needs guaranteed impressions.
        Used by the re-ranker to boost items that haven't met their quota.
        """
        creator_items = await self.redis.zrange(
            f"creator_items:{creator_id}", 0, self.ITEMS_WITH_GUARANTEE - 1
        )

        if item_id not in creator_items:
            return False  # Not in the guaranteed set

        impressions = int(
            await self.redis.hget(f"item:{item_id}", "views_total") or 0
        )

        return impressions < self.MINIMUM_IMPRESSIONS_PER_ITEM
```

---

## 10. Serving Architecture

### 10.1 Infrastructure Layout

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          REGION: US-EAST                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────────────────────────────────────────────┐                  │
│  │              Edge / API Gateway Layer                 │                  │
│  │  - 50 instances (c6i.4xlarge, 16 vCPU, 32 GB)       │                  │
│  │  - TLS termination, auth, rate limiting              │                  │
│  │  - Experiment assignment (consistent hashing)        │                  │
│  │  - Capacity: 10K RPS per instance = 500K RPS total   │                  │
│  └──────────────────────────────────────────────────────┘                  │
│                              │                                              │
│  ┌──────────────────────────────────────────────────────┐                  │
│  │           Recommendation Orchestrator                │                  │
│  │  - 200 instances (c6i.4xlarge)                       │                  │
│  │  - Manages pipeline, latency budgets, fallbacks      │                  │
│  │  - Capacity: 2.5K RPS per instance                   │                  │
│  └──────────────────────────────────────────────────────┘                  │
│         │              │              │              │                      │
│  ┌──────┴──────┐ ┌────┴────┐  ┌─────┴─────┐ ┌─────┴──────┐              │
│  │  ANN Cluster│ │Pre-Rank │  │Heavy Rank  │ │Feature     │              │
│  │             │ │ Service  │  │Service     │ │Store       │              │
│  │  12 machines│ │500 inst. │  │1900 GPUs   │ │(Redis)     │              │
│  │  128GB RAM  │ │(CPU)     │  │(A10G)      │ │            │              │
│  │  4 shards   │ │          │  │            │ │50 shards   │              │
│  │  x3 replica │ │8-core ea.│  │~240 servers│ │~1.7 TB     │              │
│  └─────────────┘ └─────────┘  └────────────┘ └────────────┘              │
│                                                                             │
│  ┌──────────────────────────────────────────────────────┐                  │
│  │              Kafka Cluster (6 brokers)                │                  │
│  │  - 100 partitions per topic                          │                  │
│  │  - 3x replication                                    │                  │
│  │  - Topics: user_actions, impressions, features        │                  │
│  └──────────────────────────────────────────────────────┘                  │
│                              │                                              │
│  ┌──────────────────────────────────────────────────────┐                  │
│  │              Flink Cluster (200 task slots)           │                  │
│  │  - UserSessionFeatureUpdater (100 slots)             │                  │
│  │  - ItemEngagementCounter (50 slots)                  │                  │
│  │  - TrendingAggregator (25 slots)                     │                  │
│  │  - ColdStartTracker (25 slots)                       │                  │
│  └──────────────────────────────────────────────────────┘                  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

Replicated in 3 regions: US-EAST, EU-WEST, APAC-SOUTH
```

### 10.2 Caching Strategy

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Caching Layers                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Layer 1: Pre-Computed Feed Cache (Redis)                                  │
│  ├── Key: feed_cache:{user_id}:{page}                                      │
│  ├── Value: pre-ranked list of item_ids                                    │
│  ├── TTL: 60 seconds (very short — preferences change fast)               │
│  ├── Use: serve subsequent scroll pages without re-running pipeline        │
│  ├── Invalidation: on any user action event                                │
│  └── Hit rate: ~30% (only helps for scroll-next within 60s)               │
│                                                                             │
│  Layer 2: Feature Store L1 Cache (In-Process LRU)                          │
│  ├── Size: 1 GB per orchestrator instance                                  │
│  ├── Contents: item features for popular items (~500K items cover 80%)     │
│  ├── TTL: 10 seconds                                                       │
│  └── Hit rate: ~35% (popular items re-queried across users)               │
│                                                                             │
│  Layer 3: ANN Result Cache (Redis)                                         │
│  ├── Key: ann_cache:{quantized_user_embedding_hash}                        │
│  ├── Value: top-500 item_ids from ANN                                      │
│  ├── TTL: 30 seconds                                                       │
│  ├── Use: users with very similar embeddings share ANN results             │
│  ├── Quantize user embedding to 8-bit → hash → cache key                  │
│  └── Hit rate: ~15% (embeddings diverse, but some clusters form)          │
│                                                                             │
│  Layer 4: Model Inference Cache (GPU Server Local)                          │
│  ├── Key: hash(feature_vector)                                             │
│  ├── Contents: model outputs (5 objective scores)                          │
│  ├── Size: 512 MB per GPU server                                          │
│  ├── TTL: 30 seconds                                                       │
│  └── Hit rate: ~10% (feature vectors are mostly unique)                   │
│                                                                             │
│  Overall effect: caching reduces effective QPS on heavy ranker by ~25%    │
│  5,700 GPUs → effectively serving ~625K QPS capacity                      │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 10.3 Graceful Degradation

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Graceful Degradation Cascade                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Level 0: NORMAL OPERATION                                                 │
│  └── Full pipeline: CandGen → PreRank → HeavyRank → ReRank               │
│      Latency: P50 ~85ms, P99 ~175ms                                       │
│                                                                             │
│  Level 1: HEAVY RANKER DEGRADED (GPU fleet partial failure)               │
│  ├── Trigger: HeavyRank P99 > 80ms or error rate > 5%                     │
│  ├── Action: skip HeavyRank, use PreRank scores directly                  │
│  ├── Pipeline: CandGen → PreRank (top 500 → top 60) → ReRank             │
│  ├── Latency: P50 ~55ms, P99 ~100ms                                       │
│  └── Quality impact: ~8% drop in watch time (measured via A/B)            │
│                                                                             │
│  Level 2: FEATURE STORE DEGRADED (Redis partial failure)                  │
│  ├── Trigger: Feature fetch P99 > 20ms or timeout rate > 10%              │
│  ├── Action: use cached features (stale up to 60s) + default features     │
│  ├── Pipeline: full, but with degraded feature quality                    │
│  ├── Latency: P50 ~90ms, P99 ~180ms                                       │
│  └── Quality impact: ~5% drop in watch time                               │
│                                                                             │
│  Level 3: CANDIDATE GENERATION DEGRADED (ANN cluster failure)             │
│  ├── Trigger: ANN latency > 30ms or error rate > 20%                      │
│  ├── Action: fall back to non-ANN channels only                           │
│  │   (collab filter + social graph + trending + cold start)               │
│  ├── Pipeline: reduced CandGen → PreRank → HeavyRank → ReRank            │
│  ├── Latency: P50 ~75ms, P99 ~160ms                                       │
│  └── Quality impact: ~15% drop in watch time (reduced personalization)    │
│                                                                             │
│  Level 4: EMERGENCY - FULL FALLBACK                                        │
│  ├── Trigger: overall pipeline P99 > 200ms or error rate > 30%            │
│  ├── Action: serve pre-computed popularity-based feed                     │
│  │   - Regional trending items (refreshed every 30 seconds)               │
│  │   - Filtered by user language/region                                   │
│  │   - No personalization                                                  │
│  ├── Source: Redis sorted set trending:{region}:{language}                │
│  ├── Latency: P50 ~5ms, P99 ~15ms                                         │
│  └── Quality impact: ~40% drop in watch time, but feed is ALIVE          │
│                                                                             │
│  Level 5: CATASTROPHIC - STATIC FEED                                       │
│  ├── Trigger: Redis and backend all down                                  │
│  ├── Action: CDN-cached static feed (refreshed hourly)                    │
│  │   - Editor-curated content                                             │
│  │   - Same for all users in a region                                     │
│  ├── Latency: ~10ms (CDN edge)                                            │
│  └── Quality impact: ~60% drop, but app is not a blank screen            │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 10.4 Request Batching and Parallelism

```python
class RecommendationOrchestrator:
    """
    Orchestrates the full pipeline with parallel execution
    and latency budget enforcement.
    """
    TOTAL_BUDGET_MS = 180  # leave 20ms for network overhead to hit 200ms P99

    async def get_recommendations(
        self, request: RecRequest
    ) -> RecResponse:
        start = time.monotonic()

        # Phase 1: Parallel - fetch user features + candidate generation
        # These have no dependency on each other
        user_feat_task = asyncio.create_task(
            self.feature_store.get_user_features(request.user_id)
        )
        session_task = asyncio.create_task(
            self.session_mgr.get_session_context(request.session_id)
        )
        candidate_tasks = {
            "two_tower": asyncio.create_task(
                self.two_tower_retriever.retrieve(request.user_id)
            ),
            "collab_filter": asyncio.create_task(
                self.cf_retriever.retrieve(request.user_id)
            ),
            "social": asyncio.create_task(
                self.social_retriever.retrieve(request.user_id)
            ),
            "trending": asyncio.create_task(
                self.trending_retriever.retrieve(request.region, request.language)
            ),
            "cold_start": asyncio.create_task(
                self.cold_start_retriever.retrieve(request.region, request.language)
            ),
            "exploration": asyncio.create_task(
                self.exploration_retriever.retrieve(request.user_id)
            ),
        }

        # Wait for all Phase 1 with timeout
        remaining_budget = self.TOTAL_BUDGET_MS - self._elapsed_ms(start)
        user_features = await asyncio.wait_for(
            user_feat_task, timeout=remaining_budget / 1000
        )
        session_context = await asyncio.wait_for(
            session_task, timeout=remaining_budget / 1000
        )

        # Gather candidate results (accept partial failures)
        channel_results = {}
        for name, task in candidate_tasks.items():
            try:
                result = await asyncio.wait_for(task, timeout=0.03)  # 30ms max
                channel_results[name] = result
            except asyncio.TimeoutError:
                # Channel timed out — skip it, others cover
                self.metrics.increment(f"candidate_channel_timeout.{name}")

        if not channel_results:
            # All channels failed — emergency fallback
            return await self._fallback_trending(request)

        # Phase 2: Merge candidates + fetch item features (parallel)
        candidates = merge_candidates(
            channel_results,
            self.channel_weights,
            max_candidates=2000
        )

        # Deduplicate against session already-shown items
        shown = session_context.get("shown_items", set())
        candidates = [c for c in candidates if c.item_id not in shown]

        item_ids = [c.item_id for c in candidates]
        item_features = await self.feature_store.get_item_features_batch(item_ids)

        # Phase 3: Pre-ranking
        remaining_budget = self.TOTAL_BUDGET_MS - self._elapsed_ms(start)
        if remaining_budget < 60:
            # Skip heavy ranker if we're running late
            pre_rank_scores = await self.pre_ranker.score(
                candidates, user_features, item_features
            )
            final = self.re_ranker.rerank(
                pre_rank_scores[:60], user_features, request.experiment_config
            )
            return self._build_response(final, degraded=True)

        pre_ranked = await self.pre_ranker.score_and_select(
            candidates, user_features, item_features, top_k=500
        )

        # Phase 4: Heavy ranking
        remaining_budget = self.TOTAL_BUDGET_MS - self._elapsed_ms(start)
        try:
            heavy_ranked = await asyncio.wait_for(
                self.heavy_ranker.score(
                    pre_ranked, user_features, item_features
                ),
                timeout=min(remaining_budget / 1000, 0.05)  # 50ms max
            )
        except asyncio.TimeoutError:
            # Heavy ranker timed out — use pre-rank scores
            heavy_ranked = pre_ranked
            self.metrics.increment("heavy_ranker_timeout")

        # Phase 5: Re-ranking
        final = self.re_ranker.rerank(
            heavy_ranked, user_features, request.experiment_config
        )

        # Log impressions asynchronously
        asyncio.create_task(self._log_impressions(request, final[:20]))

        # Cache next pages
        asyncio.create_task(self._cache_next_pages(request, final))

        return self._build_response(final[:20])

    def _elapsed_ms(self, start):
        return (time.monotonic() - start) * 1000
```

---

## 11. Experimentation Platform

### 11.1 Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Experimentation Platform                                 │
└─────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────┐     ┌───────────────────────┐
│  Experiment Config   │     │  User Assignment      │
│  Service             │     │  Service              │
│                      │     │                       │
│  - CRUD experiments  │────▶│  - Consistent hashing │
│  - Traffic allocation│     │  - hash(user_id +     │
│  - Mutual exclusion  │     │    experiment_id +    │
│  - Experiment layers │     │    salt) % 10000      │
│                      │     │  - Cached in-process  │
└──────────────────────┘     └───────────────────────┘
         │                              │
         ▼                              ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         Recommendation Pipeline                              │
│  - Receives experiment_groups[] with each request                            │
│  - Applies experiment-specific model/weights/features/rules                 │
│  - Logs: (user_id, session_id, experiment_groups, shown_items, actions)     │
└──────────────────────────────────────────────────────────────────────────────┘
         │
         ▼ event logs
┌──────────────────────────────────────────────────────────────────────────────┐
│                         Metric Computation (Spark)                           │
│  - Daily batch: compute per-experiment-group metrics                        │
│  - Metrics: watch_time, like_rate, share_rate, retention, diversity         │
│  - Statistical testing: two-sample t-test, Bonferroni correction            │
│  - Guardrail checks: flag if any metric degrades beyond threshold           │
└──────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                      Experiment Dashboard                                    │
│  - Real-time metric tracking                                                │
│  - Statistical significance indicators                                      │
│  - Automated guardrail alerts                                               │
│  - One-click rollout / rollback                                             │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 11.2 Experiment Assignment

```python
import hashlib

class ExperimentAssigner:
    """
    Deterministic user-to-experiment-group assignment.
    Same user always gets same group for experiment duration.
    Supports multiple concurrent experiments via layer-based isolation.
    """
    def __init__(self, config_client):
        self.config_client = config_client
        self._experiment_cache = {}
        self._cache_ttl = 30  # seconds

    def assign(self, user_id: str) -> List[str]:
        """
        Returns list of experiment group IDs this user belongs to.
        E.g., ["exp_model_v2_treatment", "exp_share_weight_control"]
        """
        experiments = self._get_active_experiments()
        groups = []

        for exp in experiments:
            # Hash user_id + experiment_id for consistent assignment
            hash_input = f"{user_id}:{exp['id']}:{exp['salt']}"
            hash_val = int(hashlib.md5(hash_input.encode()).hexdigest(), 16)
            bucket = hash_val % 10000  # 0.01% granularity

            # Check if user falls in experiment traffic
            if bucket < exp["traffic_pct"] * 100:
                # Determine which group within experiment
                group_bucket = hash_val % len(exp["groups"])
                group_name = exp["groups"][group_bucket]["name"]
                groups.append(f"{exp['id']}_{group_name}")

        return groups

    def _get_active_experiments(self) -> List[Dict]:
        """Fetch from config service with caching."""
        # Returns list of active experiment configs
        return self.config_client.get("active_experiments")
```

**Experiment configuration example:**

```yaml
experiments:
  - id: "exp_dcnv2_vs_dcnv1"
    name: "DCN v2 Heavy Ranker vs DCN v1"
    salt: "random_salt_2024"
    traffic_pct: 5          # 5% of users
    start_date: "2024-09-08"
    end_date: "2024-09-22"
    layer: "ranking_model"   # mutual exclusion within same layer
    groups:
      - name: "control"
        config:
          heavy_ranker_model: "dcn_v1_20240901"
      - name: "treatment"
        config:
          heavy_ranker_model: "dcn_v2_20240907"
    metrics:
      primary: ["watch_time_per_session", "day1_retention"]
      secondary: ["like_rate", "share_rate", "follow_rate"]
      guardrails:
        - metric: "day7_retention"
          max_degradation: 0.001  # 0.1%
        - metric: "report_rate"
          max_increase: 0.05     # 5% relative increase
        - metric: "creator_diversity_gini"
          max_increase: 0.02     # Gini shouldn't increase much
    min_sample_size: 1000000     # 1M users per group for significance

  - id: "exp_share_weight_boost"
    name: "Boost Share Weight in Scoring"
    salt: "salt_share_2024"
    traffic_pct: 10
    start_date: "2024-09-08"
    end_date: "2024-09-22"
    layer: "scoring_weights"   # different layer, can run concurrently
    groups:
      - name: "control"
        config:
          objective_weights:
            w_share: 0.5
      - name: "treatment_a"
        config:
          objective_weights:
            w_share: 0.8
      - name: "treatment_b"
        config:
          objective_weights:
            w_share: 1.2
    metrics:
      primary: ["share_rate_per_session", "new_user_invites"]
      guardrails:
        - metric: "watch_time_per_session"
          max_degradation: 0.02  # 2%
```

### 11.3 Metric Computation

```python
# Daily Spark job: compute experiment metrics
from scipy import stats

def compute_experiment_metrics(experiment_id: str, date: str):
    """
    Compute per-group metrics and run statistical tests.
    """
    # Load events for this experiment's users
    events = (
        spark.read.parquet(f"s3://events/dt={date}/")
        .filter(F.array_contains(F.col("experiment_groups"), experiment_id))
    )

    # Compute per-user metrics
    user_metrics = events.groupBy("user_id", "experiment_group").agg(
        F.sum("watch_time_sec").alias("total_watch_time"),
        F.count(F.when(F.col("action") == "like", 1)).alias("like_count"),
        F.count(F.when(F.col("action") == "share", 1)).alias("share_count"),
        F.countDistinct("session_id").alias("session_count"),
        F.countDistinct("creator_id").alias("unique_creators_viewed"),
    )

    # Split by group
    control = user_metrics.filter(F.col("experiment_group").endswith("_control"))
    treatment = user_metrics.filter(F.col("experiment_group").endswith("_treatment"))

    # Statistical tests
    control_watch = control.select("total_watch_time").rdd.flatMap(lambda x: x).collect()
    treatment_watch = treatment.select("total_watch_time").rdd.flatMap(lambda x: x).collect()

    t_stat, p_value = stats.ttest_ind(treatment_watch, control_watch)

    result = {
        "experiment_id": experiment_id,
        "date": date,
        "control_n": len(control_watch),
        "treatment_n": len(treatment_watch),
        "metrics": {
            "watch_time": {
                "control_mean": np.mean(control_watch),
                "treatment_mean": np.mean(treatment_watch),
                "lift_pct": (np.mean(treatment_watch) - np.mean(control_watch))
                            / np.mean(control_watch) * 100,
                "p_value": p_value,
                "significant": p_value < 0.05
            }
            # ... repeat for other metrics
        }
    }

    # Check guardrails
    for guardrail in experiment_config["guardrails"]:
        metric_name = guardrail["metric"]
        # Compute and check
        if check_guardrail_violated(control, treatment, guardrail):
            result["guardrail_violations"] = result.get("guardrail_violations", [])
            result["guardrail_violations"].append(metric_name)
            # Auto-alert on-call
            send_alert(f"Guardrail violated: {metric_name} in {experiment_id}")

    return result
```

---

## 12. Failure Walkthroughs

### 12.1 Feature Store Down

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  SCENARIO: Redis Feature Store cluster partially down (50% shards lost)    │
│  ROOT CAUSE: Network partition in Redis Cluster                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  T+0s:     Redis shards 25-50 become unreachable                           │
│                                                                             │
│  T+1s:     Feature Store client detects timeouts on 50% of requests        │
│            Circuit breaker starts counting failures                         │
│                                                                             │
│  T+3s:     Circuit breaker trips for affected shards                       │
│            Feature Store falls back to:                                     │
│            1. L1 in-process cache (hits for ~35% of popular items)         │
│            2. DynamoDB cold tier (5ms latency, but available)              │
│            3. Default feature vectors for remaining items                   │
│                                                                             │
│  T+5s:     Monitoring alerts fire:                                         │
│            - "feature_store_error_rate > 10%"                              │
│            - "feature_fetch_p99 > 20ms"                                    │
│                                                                             │
│  T+10s:    Orchestrator detects degraded feature quality                   │
│            Automatically adjusts:                                           │
│            - Heavy ranker receives incomplete features → quality drops     │
│            - System uses more engagement-stats-based ranking               │
│            - Personalization accuracy degrades 10-20%                      │
│                                                                             │
│  T+30s:    Real-time feature updates fail for affected user shards         │
│            - Session features stale for ~25% of users                      │
│            - Feedback loop broken for these users                          │
│                                                                             │
│  T+60s:    On-call acknowledges. Options:                                  │
│            a) Wait for network partition to heal                           │
│            b) Promote replicas in healthy network partition                │
│            c) Failover affected shards to a different AZ                   │
│                                                                             │
│  T+5min:   Network partition heals. Redis cluster auto-recovers.          │
│            Feature store resumes normal operation.                          │
│            Circuit breakers reset (half-open → closed).                    │
│                                                                             │
│  IMPACT:   ~5 minutes of degraded recommendations for ~25% of users       │
│            Watch time drop: ~3-5% during incident                          │
│            No data loss (Kafka events buffered, replayed to features)      │
│                                                                             │
│  POST-MORTEM ACTION: Increase Redis Cluster replica count from 3 to 5     │
│  for critical shards. Add cross-AZ replication.                            │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 12.2 Ranker Model Returning Garbage

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  SCENARIO: New heavy ranker model deployed, returning near-uniform scores  │
│  ROOT CAUSE: Feature schema mismatch — model trained on v3 features,      │
│              serving receives v2 features (column ordering shifted)         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  T+0:      New model deployed via canary (5% traffic)                      │
│                                                                             │
│  T+2min:   Canary monitoring detects:                                      │
│            - Score entropy increased 300% (scores near-uniform 0.45-0.55)  │
│            - P(watch_complete) predictions cluster around 0.5              │
│            - Score variance dropped from 0.08 to 0.002                     │
│                                                                             │
│  T+3min:   Automatic canary health check FAILS:                            │
│            - Rule: "if treatment score_variance < 0.5 * control, halt"    │
│            - Canary automatically rolled back                              │
│            - 5% of users affected for ~3 minutes                          │
│                                                                             │
│  T+5min:   Alert to ML team: "Canary failed: score distribution anomaly"  │
│                                                                             │
│  WHAT IF canary was missed (manual override / canary disabled)?            │
│                                                                             │
│  T+10min:  With uniform scores, re-ranker produces effectively random     │
│            ordering. Watch time drops 25-30%.                               │
│                                                                             │
│  T+15min:  Guardrail metric alert fires:                                   │
│            "watch_time_per_session dropped > 5% vs yesterday"              │
│                                                                             │
│  T+20min:  On-call rolls back to previous model version.                  │
│            Recovery within 2 minutes of rollback.                          │
│                                                                             │
│  TOTAL IMPACT (with canary): 5% of users * 3 minutes = minimal            │
│  TOTAL IMPACT (without canary): 100% of users * 20 minutes = severe       │
│                                                                             │
│  PREVENTION:                                                               │
│  1. Model validation gate: before deployment, run on shadow traffic        │
│     and assert score distribution matches expected bounds                  │
│  2. Feature schema versioning: model manifest declares expected schema    │
│  3. Canary with automated rollback (never disable)                        │
│  4. Real-time score distribution monitoring per model version             │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 12.3 Viral Item Creating Thundering Herd

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  SCENARIO: A video goes mega-viral (100M views in 1 hour)                  │
│  EFFECT: Thundering herd on item features + engagement counters            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  T+0:      Video starts trending. Views: 1M/hour.                          │
│                                                                             │
│  T+30min:  Video goes mega-viral via external social media shares.        │
│            Views spike to 10M/hour. Trending channel picks it up.          │
│            Item appears in 30% of all recommendation responses.            │
│                                                                             │
│  T+45min:  Hotspot problems emerge:                                        │
│            - Redis key item:viral_123 receives 200K reads/sec              │
│            - Flink engagement counter for this item overwhelmed            │
│            - All reads hitting same Redis shard (hot key problem)          │
│                                                                             │
│  MITIGATION (built-in):                                                    │
│                                                                             │
│  1. Hot Key Detection + Read Replicas:                                     │
│     - Redis Cluster tracks key access frequency                            │
│     - Keys with > 10K reads/sec automatically replicated to               │
│       ALL shards (read-any)                                                │
│     - Feature Store client load-balances reads across replicas            │
│                                                                             │
│  2. In-Process Feature Cache:                                              │
│     - L1 LRU cache absorbs 35% of reads for viral item                   │
│     - TTL 10s means feature staleness bounded                              │
│                                                                             │
│  3. Engagement Counter Batching:                                           │
│     - Flink already batches engagement updates per 1s window              │
│     - For viral items: increase batch window to 5s                        │
│     - Counter precision: +-5s accuracy (acceptable for this item)         │
│                                                                             │
│  4. Candidate Generation Deduplication:                                    │
│     - Item appears in multiple channels (trending, collab_filter, etc)    │
│     - Merging deduplicates: scored once, not 6 times                      │
│                                                                             │
│  5. Creator Fairness Cap:                                                  │
│     - Re-ranker limits this creator to 3 items max in any feed            │
│     - Prevents the viral video from dominating ALL feeds                  │
│                                                                             │
│  RESULT: System handles 100M views/hour without degradation.              │
│  Key insight: the hot key problem is the real risk, not compute.          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 12.4 Bot Farm Manipulating Engagement Signals

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  SCENARIO: Coordinated bot farm inflates engagement on 1000 items          │
│  ATTACK: 50K bot accounts each like/watch target items to boost them      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  DETECTION SIGNALS:                                                        │
│                                                                             │
│  1. Engagement Velocity Anomaly:                                           │
│     - Normal item: 0-500 likes in first hour                              │
│     - Attacked item: 50,000 likes in first hour with low view count       │
│     - Detector: like_rate / view_rate ratio > 3 standard deviations       │
│                                                                             │
│  2. User Behavior Anomaly:                                                 │
│     - Bot accounts: similar signup dates, similar engagement patterns      │
│     - Detector: clustering analysis on user engagement vectors             │
│     - Signal: 50K users all engaging with same 1000 items = suspicious    │
│                                                                             │
│  3. Feature Distribution Shift:                                            │
│     - Attacked items suddenly appear in candidate generation for           │
│       unrelated users (like_rate artificially high)                        │
│     - Detector: monitor item score drift rate                              │
│                                                                             │
│  DEFENSE LAYERS:                                                           │
│                                                                             │
│  Layer 1: Engagement Rate Limiting                                         │
│  ├── Max engagement actions per user per hour: 200                         │
│  ├── Max likes on single item per user: 1 (enforced at API)              │
│  └── Impact: limits bot throughput, does not stop distributed attack      │
│                                                                             │
│  Layer 2: Engagement Quality Scoring (Flink real-time)                    │
│  ├── Each engagement event scored for "authenticity":                      │
│  │   - Account age < 7 days: score *= 0.1                                 │
│  │   - Watch time < 3 seconds but liked: score *= 0.2                     │
│  │   - Device fingerprint matches known bot cluster: score = 0            │
│  ├── Only high-quality engagements update item engagement features        │
│  └── Impact: 90% of bot engagement filtered out of feature computation    │
│                                                                             │
│  Layer 3: Item-Level Anomaly Detection (hourly batch)                      │
│  ├── Compare item engagement trajectory to similar items                  │
│  ├── Flag items with > 5x expected engagement velocity                    │
│  ├── Flagged items: freeze engagement features at pre-anomaly values     │
│  └── Impact: prevents already-inflated items from ranking highly         │
│                                                                             │
│  Layer 4: Account-Level Bot Detection (daily batch)                        │
│  ├── ML classifier on user behavior: session length, engagement pattern  │
│  ├── Accounts classified as bots: all engagements retroactively removed  │
│  ├── Item engagement stats recomputed without bot contributions          │
│  └── Impact: cleanest defense, but delayed by up to 24 hours            │
│                                                                             │
│  TIMELINE:                                                                 │
│  T+0:      Bot attack begins                                              │
│  T+1min:   Layer 1 (rate limiting) catches aggressive bots                │
│  T+5min:   Layer 2 (quality scoring) filters 90% of bot engagement       │
│  T+1hour:  Layer 3 (anomaly detection) freezes suspicious items          │
│  T+24hour: Layer 4 (bot detection) removes all bot influence             │
│                                                                             │
│  NET IMPACT: Minimal. Layer 2 catches most bot engagement within          │
│  minutes. Some items may rank slightly higher for 1 hour until            │
│  Layer 3 catches them.                                                     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 13. Content Safety Integration

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Content Safety in Recommendation Pipeline               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Integration Points:                                                       │
│                                                                             │
│  1. ITEM INGESTION (upload time)                                           │
│     └── Content moderation service classifies item:                        │
│         ├── approved → eligible for recommendation                         │
│         ├── flagged → eligible with restrictions                           │
│         └── removed → blocked from all channels                            │
│         Result stored in item:{item_id}.mod_status                         │
│                                                                             │
│  2. CANDIDATE GENERATION                                                   │
│     └── ANN post-filter: bloom filter of removed item_ids                 │
│         Updated every 30 seconds from moderation service                   │
│         Size: ~1M removed items * 10 bits = 1.25 MB (tiny)               │
│                                                                             │
│  3. FEATURE ENRICHMENT                                                     │
│     └── Items with mod_status=removed filtered out (should not reach      │
│         here due to step 2, but defense in depth)                         │
│                                                                             │
│  4. RE-RANKING                                                             │
│     └── Flagged items:                                                     │
│         ├── Score multiplied by 0.3 (heavy de-rank)                       │
│         ├── Never shown to users flagged as minors                        │
│         ├── Excluded from trending and exploration channels               │
│         └── Max 1 flagged item per page of 20                              │
│                                                                             │
│  5. REAL-TIME MODERATION UPDATES                                           │
│     └── When moderation status changes (e.g., approved → removed):        │
│         ├── Kafka event published to moderation_updates topic             │
│         ├── Flink consumer updates item:{item_id}.mod_status in Redis     │
│         ├── Bloom filter updated within 30 seconds                        │
│         ├── Any cached feed pages containing item invalidated             │
│         └── Propagation: item removed from recs within < 2 minutes        │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 14. Multi-Region Deployment

### 14.1 Data Sharding by Region

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Regional Data Architecture                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  USER DATA: sharded by region (GDPR compliance)                            │
│  ├── US-EAST: NA users (~180M MAU)                                         │
│  ├── EU-WEST: EU users (~120M MAU)                                         │
│  └── APAC-SOUTH: APAC users (~200M MAU)                                   │
│                                                                             │
│  User features NEVER leave their home region.                              │
│  Cross-region queries not needed (user is in one region).                  │
│                                                                             │
│  ITEM DATA: replicated globally (items are not user data)                  │
│  ├── Items are visible globally (a Korean video can trend in US)           │
│  ├── Item features replicated to all 3 regions                            │
│  ├── Replication lag target: < 5 seconds (async)                          │
│  └── ANN index: full replica in each region (same 200M items)             │
│                                                                             │
│  EVENT DATA: processed locally, aggregated centrally                       │
│  ├── User actions processed by local Flink → local Redis                  │
│  ├── Local Kafka retains 72h for reprocessing                             │
│  ├── Events mirrored to central data lake (S3) for training               │
│  └── ML training happens centrally; models deployed to all regions        │
│                                                                             │
│  MODEL ARTIFACTS: trained centrally, deployed to all regions               │
│  ├── Training: central GPU cluster (most cost-efficient)                  │
│  ├── Model stored in S3 → replicated to regional model stores             │
│  └── Deployment: canary in one region → full rollout                      │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 14.2 Cross-Region Consistency

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Consistency Guarantees                                   │
├───────────────────┬──────────────────┬──────────────────────────────────────┤
│  Data Type        │  Consistency     │  Rationale                           │
├───────────────────┼──────────────────┼──────────────────────────────────────┤
│  User features    │  Strong (local)  │  All reads/writes in home region    │
│  Item features    │  Eventual (5s)   │  Cross-region async replication     │
│  Moderation       │  Eventual (2min) │  Removed items must propagate fast  │
│  Session state    │  Strong (local)  │  Sessions don't cross regions       │
│  ANN index        │  Eventual (daily)│  Rebuilt daily, incremental hourly  │
│  Model weights    │  Strong (deploy) │  Deployed atomically per region     │
│  Experiment config│  Strong (config) │  Distributed config with versioning │
└───────────────────┴──────────────────┴──────────────────────────────────────┘
```

---

## 15. Trade-offs

### 15.1 Exploration vs. Exploitation

| Dimension | Exploitation (score-based) | Exploration (diversity) | Our Choice |
|-----------|---------------------------|------------------------|------------|
| **Short-term engagement** | Higher (show what we know user likes) | Lower (unfamiliar content) | 95% exploitation |
| **Long-term retention** | Risk: filter bubble fatigue | Better: discover new interests | 5% exploration |
| **New content discovery** | Poor (new items rarely surface) | Good (Thompson sampling) | Dedicated cold-start channel |
| **Creator ecosystem** | Top creators dominate | Emerging creators get exposure | Minimum exposure guarantee |
| **Algorithm** | Greedy: highest-scored item first | Thompson sampling + MMR | Hybrid in re-ranker |

**Our approach:** 95/5 exploration/exploitation split in production. The exploration percentage is an A/B testable parameter. We run a dedicated exploration channel (Channel 6) plus Thompson sampling in the cold-start channel (Channel 5), ensuring both new items and new user interests are discovered.

### 15.2 Engagement vs. Diversity

| Metric | Engagement-Optimized | Diversity-Optimized | Our Balance |
|--------|---------------------|---------------------|-------------|
| Watch time/session | Highest | -10-15% | -2% (MMR lambda=0.7) |
| Category diversity (Gini) | 0.7 (concentrated) | 0.3 (uniform) | 0.5 (moderate) |
| Creator diversity | Low (top 100 dominate) | High | Medium (fairness caps) |
| User satisfaction (survey) | High short-term | Higher long-term | Guardrail on 7-day retention |

**Implementation levers:**
- MMR lambda parameter (0.7 = 70% relevance, 30% diversity)
- Creator cap (max 3 items per creator per feed)
- Consecutive category limit (max 2 consecutive same-category)
- Exploration rate (5%)

### 15.3 Real-Time vs. Batch Features

| Dimension | Real-Time Features | Batch Features |
|-----------|--------------------|----------------|
| **Freshness** | Seconds | Hours |
| **Completeness** | Session-level only | Full history |
| **Accuracy** | Noisy (small sample) | Stable (large aggregates) |
| **Cost** | High (Flink + Redis hot tier) | Low (Spark + cold storage) |
| **Feature types** | Session context, recent counters | Aggregated stats, embeddings |
| **Impact on rec quality** | Critical for within-session adaptation | Critical for cross-session personalization |

**Our approach:** Use both. The feature store merges real-time features (session-level, updated in seconds) with batch features (historical aggregates, updated daily). Real-time features are the competitive advantage: the ability to adapt within a session is what distinguishes a great rec system from a good one.

**Cost tradeoff:**
```
Real-time pipeline cost (Flink + Redis hot tier):
  - Flink cluster: 200 task slots * $0.20/hr = $40/hr = ~$350K/year
  - Redis hot tier: 50 shards * $0.30/hr = $15/hr = ~$131K/year
  - Total real-time: ~$481K/year

Batch pipeline cost (Spark + DynamoDB):
  - Spark cluster: 200 nodes * 2 hours/day * $0.50/hr = ~$73K/year
  - DynamoDB: ~$150K/year (on-demand capacity)
  - Total batch: ~$223K/year

Real-time premium: ~$258K/year additional
Worth it? A 2% improvement in watch time from within-session adaptation
translates to ~$50M+ in ad revenue at this scale. Cost is negligible.
```

### 15.4 Model Complexity vs. Serving Latency

| Model | Accuracy (NDCG@20) | Latency (500 items) | GPU Cost |
|-------|--------------------|--------------------|----------|
| Logistic Regression | 0.35 | 2ms (CPU) | $0 |
| 2-layer MLP | 0.42 | 5ms (CPU) | $0 |
| DCN v1 | 0.48 | 15ms (GPU) | $12M/year |
| DCN v2 (ours) | 0.51 | 40ms (GPU) | $18.7M/year |
| DCN v2 + Transformer | 0.53 | 120ms (GPU) | $55M/year |

**Our choice:** DCN v2. The jump from DCN v1 to v2 (+3 NDCG points) justifies the GPU cost increase. The Transformer variant pushes latency above our 50ms budget for the heavy ranker without sufficient accuracy gain.

**Mitigation for latency:** INT8 quantization of DCN v2 reduces inference time from 40ms to ~22ms with < 0.5% accuracy loss, halving GPU fleet requirements.

### 15.5 Consistency vs. Availability (Feature Store)

| Choice | Consistency | Availability | Our Decision |
|--------|-------------|--------------|--------------|
| Strong consistency (sync replication) | User always sees latest features | Risk: one shard down = all requests fail | No |
| Eventual consistency (async) | Features may be seconds stale | 99.99% (continue serving with stale data) | Yes |

**Rationale:** A recommendation with 3-second-stale features is far better than no recommendation at all. The rec system is not a financial transaction; slightly stale features produce slightly suboptimal rankings, not incorrect data. Availability wins.

---

## 16. Evolution Path

### Phase 1: Single-Region MVP (Month 1-3)

```
- One region (US-EAST)
- 50M MAU target
- Two-Tower + trending + popularity channels
- Pre-ranker only (no heavy ranker GPU fleet)
- Redis feature store (user + item features)
- Basic Kafka + Flink pipeline for real-time features
- No A/B testing platform (manual experiments)

Infrastructure:
  - 10 ANN servers (32 GB RAM each, ~50M items)
  - 50 pre-ranker CPU instances
  - 10-shard Redis cluster (~200 GB)
  - 3-broker Kafka cluster
  - 20 Flink task slots

Cost: ~$50K/month
```

### Phase 2: Scale + Quality (Month 4-8)

```
- Add heavy ranker (DCN v2 on GPU)
- Add collab filter + social graph channels
- Add cold-start with Thompson sampling
- Build A/B testing platform
- Add model canary deployment
- Scale to 200M MAU
- Still single-region

Infrastructure addition:
  - 500 A10G GPUs for heavy ranker
  - 100 pre-ranker instances
  - 30-shard Redis cluster
  - 6-broker Kafka cluster
  - 100 Flink task slots
  - Experiment config service

Cost: ~$500K/month
```

### Phase 3: Global Multi-Region (Month 9-14)

```
- Add EU-WEST and APAC-SOUTH regions
- User data sharding by region (GDPR)
- Cross-region item feature replication
- Regional ANN indices
- Regional Kafka + Flink clusters
- Global model training, regional serving
- Full A/B testing with regional experiments
- Scale to 500M MAU

Infrastructure (per region):
  - Same as Phase 2, replicated 3x
  - Cross-region replication infrastructure
  - Central model training cluster (8x A100 nodes)

Total cost: ~$2M/month
```

### Phase 4: Advanced Optimization (Month 15+)

```
- Online learning (real-time model updates)
- Advanced exploration (contextual bandits)
- Multi-tower retrieval (separate towers per signal type)
- Sequence models for session understanding (Transformer-based)
- Causal inference for long-term optimization
- Automated feature discovery
- Neural architecture search for ranker models

Cost: ~$3M/month (dominated by GPU fleet)
```

---

## 17. Monitoring & Observability

### 17.1 Key Metrics

| Metric | Target | Alert Threshold | Dashboard |
|--------|--------|-----------------|-----------|
| End-to-end rec latency P50 | < 100ms | > 120ms | Real-time |
| End-to-end rec latency P99 | < 200ms | > 250ms | Real-time |
| Candidate generation latency | < 30ms | > 50ms | Real-time |
| Feature fetch latency P99 | < 10ms | > 20ms | Real-time |
| Heavy ranker latency P99 | < 50ms | > 80ms | Real-time |
| Feature store error rate | < 0.1% | > 1% | Real-time |
| Rec pipeline error rate | < 0.1% | > 0.5% | Real-time |
| Kafka consumer lag | < 1000 | > 10000 | Real-time |
| Feature update propagation | < 5s | > 15s | Sampled |
| Watch time per session | tracked | -5% vs 7d avg | Hourly |
| Model score distribution | tracked | entropy +/- 50% | Per-deploy |
| Candidate channel timeout rate | < 1% per channel | > 5% | Real-time |
| Graceful degradation activations | 0 | > 0 | Real-time |
| ANN recall@100 (offline eval) | > 0.95 | < 0.90 | Daily |
| Cold-start item coverage | > 80% items get 500 impressions in 48h | < 60% | Daily |

### 17.2 Health Check Probes

```python
# /health endpoint on recommendation orchestrator
async def health_check() -> Dict:
    checks = {}

    # Feature store connectivity
    try:
        t0 = time.monotonic()
        await feature_store.redis.ping()
        checks["feature_store"] = {
            "status": "ok",
            "latency_ms": (time.monotonic() - t0) * 1000
        }
    except Exception as e:
        checks["feature_store"] = {"status": "error", "error": str(e)}

    # ANN cluster connectivity
    try:
        t0 = time.monotonic()
        dummy = np.random.randn(256).astype(np.float32)
        await ann_client.search(dummy, top_k=1)
        checks["ann_cluster"] = {
            "status": "ok",
            "latency_ms": (time.monotonic() - t0) * 1000
        }
    except Exception as e:
        checks["ann_cluster"] = {"status": "error", "error": str(e)}

    # Heavy ranker (GPU) health
    try:
        t0 = time.monotonic()
        dummy_input = torch.randn(1, 800)
        await heavy_ranker_client.predict(dummy_input)
        checks["heavy_ranker"] = {
            "status": "ok",
            "latency_ms": (time.monotonic() - t0) * 1000
        }
    except Exception as e:
        checks["heavy_ranker"] = {"status": "error", "error": str(e)}

    # Kafka producer health
    checks["kafka"] = {"status": "ok" if kafka_producer.connected else "error"}

    overall = "ok" if all(c["status"] == "ok" for c in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}
```

---

## 18. Cost Summary

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Annual Cost Estimate (Full Scale, 3 Regions)             │
├─────────────────────────────────────────┬───────────────────────────────────┤
│  Component                              │  Annual Cost                      │
├─────────────────────────────────────────┼───────────────────────────────────┤
│  GPU Fleet (Heavy Ranker)               │                                   │
│    5,700 A10G (INT8 optimized: 2,850)   │  $18.7M (or $9.4M quantized)    │
├─────────────────────────────────────────┼───────────────────────────────────┤
│  CPU Fleet (Orchestrator, Pre-Ranker,   │                                   │
│    Gateway, ANN, Two-Tower)             │                                   │
│    ~3,000 instances across roles        │  $8.4M                           │
├─────────────────────────────────────────┼───────────────────────────────────┤
│  Redis Feature Store (150 shards total) │  $3.9M                           │
├─────────────────────────────────────────┼───────────────────────────────────┤
│  Kafka Clusters (18 brokers + storage)  │  $2.1M                           │
├─────────────────────────────────────────┼───────────────────────────────────┤
│  Flink Clusters (600 task slots)        │  $1.0M                           │
├─────────────────────────────────────────┼───────────────────────────────────┤
│  DynamoDB (cold feature tier)           │  $0.5M                           │
├─────────────────────────────────────────┼───────────────────────────────────┤
│  Object Storage (S3, data lake, models) │  $0.8M                           │
├─────────────────────────────────────────┼───────────────────────────────────┤
│  Model Training (GPU cluster, shared)   │  $1.5M                           │
├─────────────────────────────────────────┼───────────────────────────────────┤
│  Networking (cross-region, CDN)         │  $1.2M                           │
├─────────────────────────────────────────┼───────────────────────────────────┤
│  Monitoring, Logging, Observability     │  $0.4M                           │
├─────────────────────────────────────────┼───────────────────────────────────┤
│  TOTAL (with INT8 quantized ranker)     │  ~$29.2M/year                    │
│  TOTAL (without quantization)           │  ~$38.5M/year                    │
├─────────────────────────────────────────┼───────────────────────────────────┤
│  Cost per MAU per year                  │  $0.058 (~6 cents per user)      │
│  Cost per recommendation request        │  $0.0000014 (~$1.40 per M reqs) │
└─────────────────────────────────────────┴───────────────────────────────────┘
```
