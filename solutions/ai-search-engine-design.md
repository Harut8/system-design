# AI-Powered Search Engine: Design Document

> Solution to [`tasks/ai-search-engine.md`](../tasks/ai-search-engine.md).

---

## Table of Contents

1. [Requirements Clarification](#1-requirements-clarification)
2. [Capacity Estimates](#2-capacity-estimates)
3. [High-Level Architecture](#3-high-level-architecture)
4. [Crawling and Indexing Pipeline](#4-crawling-and-indexing-pipeline)
5. [Index Architecture](#5-index-architecture)
6. [Query Understanding Pipeline](#6-query-understanding-pipeline)
7. [Multi-Stage Ranking System](#7-multi-stage-ranking-system)
8. [AI Answer Generation](#8-ai-answer-generation)
9. [Query Routing Logic](#9-query-routing-logic)
10. [Serving Architecture](#10-serving-architecture)
11. [Freshness Architecture](#11-freshness-architecture)
12. [Failure Walkthroughs](#12-failure-walkthroughs)
13. [Cost Model](#13-cost-model)
14. [Evolution Path](#14-evolution-path)
15. [Trade-offs](#15-trade-offs)

---

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|----------|----------|--------|
| Scope | Are we building the LLM itself? | No. We assume a capable 70B-parameter instruction-following model is available (self-hosted or API). We design the retrieval, routing, and orchestration around it. |
| Scope | Is advertising/monetization in scope? | No, but the SERP layout and ranking pipeline must accommodate ad placement without re-architecting. |
| Scale | What is the full corpus size? | 200 billion web documents. 50 billion of those have dense vector embeddings (high-quality tier). |
| Scale | Daily query volume? | 5 billion queries/day, sustained 60K QPS, peak 150K+ QPS. |
| Scale | What fraction of queries trigger AI answers? | ~20% (1 billion/day). The rest are served by traditional ranking, knowledge graph, or structured data. |
| Latency | P50/P99 for a non-AI SERP? | P50 <= 200ms, P99 <= 500ms from query submission to rendered page. |
| Latency | Time-to-first-token for AI answers? | P50 <= 800ms, P99 <= 2s. Full answer: P50 <= 3s, P99 <= 8s. |
| Freshness | Breaking news target? | Indexed and searchable within 1 minute of first crawl. |
| Quality | NDCG target? | NDCG@10 >= 0.75 on a representative query sample (human-evaluated). |
| Quality | AI answer faithfulness? | >= 95% of generated answers grounded in retrieved sources. Zero tolerance on YMYL queries contradicting authoritative sources. |
| Cost | Is LLM cost a hard constraint? | Yes. At 1B AI answers/day, even $0.001/answer = $1M/day. The design must make this viable. |
| Infrastructure | Global presence? | Data centers in North America, Europe, Asia-Pacific. Users routed to nearest region. |
| Crawl | Crawl rate? | 5+ billion pages/day recrawled or newly discovered. |
| Privacy | PII handling? | Query logs anonymized/pseudonymized per GDPR/CCPA. No user PII in LLM prompts. |
| Safety | Content moderation? | Safe search mandatory for minors. YMYL queries get extra guardrails. Adversarial prompt injection from web pages must be detected and blocked. |

### Key Assumptions

1. **Not every query deserves an LLM call.** Navigational queries ("facebook login"), simple factual queries ("weather in SF"), and transactional queries ("buy iPhone 16") are better served by shortcuts, knowledge graph, or structured data. The query router is the most important cost-control lever.
2. **The web is adversarial.** SEO spam, content farms, and prompt injection via web pages (designed to manipulate AI answers) are production realities, not theoretical concerns.
3. **Freshness and completeness are in tension.** A real-time index for breaking news coexists with a deep archival index that takes hours to fully update. Queries get results from both, merged at serving time.
4. **Self-hosted LLMs are the primary inference path** for cost viability at 1B answers/day. API-based models serve as overflow/fallback.
5. **The system starts as a vertical search engine** (e.g., academic papers, code, or a specific content domain) and scales toward general web search. The architecture supports both without re-design.
6. **Team grows from 10 to 100+ engineers.** Every subsystem must be independently deployable, testable, and debuggable.

### What We Are Explicitly Not Building

- The LLM itself. We consume a model; we do not train one.
- An advertising system. But the SERP and ranking pipeline leave room for ad insertion.
- A general-purpose chatbot. This is a search engine with AI-enhanced answers, not a conversation partner. Multi-turn is supported but scoped to search refinement.

---

## 2. Capacity Estimates

### Index Storage

```
Total documents:                   200,000,000,000 (200B)
Avg document text (compressed):    2 KB
Raw text storage:                  200B x 2 KB = 400 TB

Inverted index:
  Avg unique terms/doc:            200
  Posting list entry:              12 bytes (doc_id 6B + term_freq 2B + position_offset 4B)
  Raw postings:                    200B x 200 x 12 B = 480 TB
  With delta-encoding + varint compression (~4x): ~120 TB
  Term dictionary overhead:        ~5 TB (50B unique terms, 100 B each)
  Total inverted index:            ~125 TB

Vector index (high-quality tier, 50B docs):
  Embedding dimension:             768 (float16 = 1,536 B per vector)
  Raw vectors:                     50B x 1,536 B = 76.8 TB
  With int8 scalar quantization:   50B x 768 B = 38.4 TB
  HNSW graph overhead (1.5x):      ~57.6 TB (on unquantized) or ~38 TB (on quantized, but
                                    graph edges stored separately)
  Total vector index (quantized + graph): ~60-75 TB

Knowledge graph:
  Entities:                        5 billion
  Relationships:                   50 billion triples
  Per triple:                      ~100 B (subject_id + predicate + object_id + metadata)
  Raw:                             50B x 100 B = 5 TB
  With indexes (SPO, POS, OSP):    ~15 TB

PageRank / link graph:
  Unique URLs:                     200B
  Avg outlinks/page:               50
  Per link:                        16 B (src_id 8B + dst_id 8B)
  Raw:                             200B x 50 x 16 B = 160 TB
  Compressed adjacency lists:      ~40 TB

Summary (single replica):
  Inverted index:                  ~125 TB
  Vector index:                    ~70 TB
  Knowledge graph:                 ~15 TB
  Link graph:                      ~40 TB
  Document metadata store:         ~50 TB (250 B metadata x 200B docs)
  Total:                           ~300 TB per replica

With 3x replication across data centers: ~900 TB globally
```

### Query Throughput

```
Sustained QPS:                     60,000
Peak QPS:                          150,000
AI-answer queries (20%):           12,000 QPS sustained, 30,000 peak

Per query fan-out:
  Inverted index shards hit:       ~50 (of 2,000 total shards for 200B docs)
  Vector index shards hit:         ~20 (for top-tier semantic retrieval)
  Internal RPC fan-out per query:  ~70-100 shard queries

Total internal shard QPS:          60,000 x 80 avg fan-out = 4,800,000 shard queries/sec
```

### Inverted Index Shard Sizing

```
Target shard size:                 100M documents (manageable in RAM for posting lists)
200B docs / 100M per shard =       2,000 shards

Per shard:
  Posting data:                    ~62.5 GB (125 TB / 2,000)
  In-memory term dictionary:       ~2.5 GB
  Total per shard:                 ~65 GB
  With replica factor 3:           6,000 shard replicas total

Shard query rate:
  Each query hits ~50 shards (document-partitioned, but queries
  only fan out to tier-1 shards for most queries; tier-2 only
  on recall-sensitive queries)
  Per shard:  60,000 QPS x 50 / 2,000 shards = 1,500 QPS per shard
  Each shard serves from 3 replicas: ~500 QPS per replica — comfortable
```

### Vector Index Shard Sizing

```
50B vectors / 25M per shard =      2,000 vector shards

Per shard (int8 quantized):
  Vectors:                         25M x 768 B = 19.2 GB
  HNSW graph overhead:             ~10 GB
  Total per shard:                 ~30 GB RAM
  With replica factor 3:           6,000 vector shard replicas

Per-shard QPS:
  Only top-tier queries hit vector index (~40% of queries)
  24,000 QPS x 20 shards / 2,000 = 240 QPS per shard
  Per replica: ~80 QPS — very comfortable for HNSW search
```

### LLM Inference Fleet

```
AI answers/day:                    1,000,000,000
AI answers/second:                 ~11,574 (sustained), peak ~30,000

Model:                             70B parameter, self-hosted (8-bit quantized)
Hardware:                          NVIDIA H100 (80 GB HBM3)
GPUs per model instance:           4 H100s (tensor-parallel, 70B int8 fits in 4x80 GB)

Throughput per instance:
  Avg input tokens (query + context): ~2,000 tokens
  Avg output tokens (answer):         ~300 tokens
  With continuous batching + speculative decoding: ~30 requests/sec per 4-GPU instance
  (batch size ~16, input prefill overlapped with decode via chunked prefill)

Instances needed (sustained):      11,574 / 30 = 386 instances
GPUs needed (sustained):           386 x 4 = 1,544 H100s
For peak (2.5x headroom):         ~3,860 H100s

With 15% operational headroom:     ~4,400 H100s total for the AI-answer fleet
```

### Answer Cache Impact

```
Cache hit rate (realistic):        30-40% of AI-answer queries (popular/repeated queries)
Effective AI inferences/sec:       11,574 x 0.65 = ~7,523 (after 35% cache hit)
GPU fleet reduction:               ~7,523 / 30 = 251 instances = 1,004 H100s sustained
Peak with headroom:                ~2,800 H100s

This cache alone saves ~1,000 H100s — at ~$2.50/GPU-hour, that is
$2,500/hour = $60,000/day = $21.9M/year in GPU compute savings.
```

---

## 3. High-Level Architecture

```
                                    ┌──────────────────────────────────┐
                                    │          Edge / CDN              │
                                    │  (DNS routing, TLS termination,  │
                                    │   static assets, DDoS protection)│
                                    └───────────────┬──────────────────┘
                                                    │
                                                    ▼
                                    ┌──────────────────────────────────┐
                                    │        API Gateway / LB          │
                                    │  (rate limiting, auth, routing)  │
                                    └───────────────┬──────────────────┘
                                                    │
                          ┌─────────────────────────┼──────────────────────────┐
                          │                         │                          │
                          ▼                         ▼                          ▼
                 ┌─────────────────┐   ┌──────────────────────┐   ┌─────────────────────┐
                 │  Autocomplete   │   │   Query Processor     │   │  Conversational     │
                 │  Service        │   │                        │   │  Session Service    │
                 │  (trie + ML,   │   │  1. Spell correct      │   │  (multi-turn ctx,   │
                 │   <50ms P99)   │   │  2. Intent classify    │   │   coreference       │
                 └─────────────────┘   │  3. Query rewrite     │   │   resolution)       │
                                       │  4. Entity linking     │   └─────────┬───────────┘
                                       │  5. Safety classify    │             │
                                       │  6. Route decision     │◀────────────┘
                                       └───────────┬────────────┘
                                                   │
                          ┌────────────────────────┼────────────────────────┐
                          │                        │                        │
                          ▼                        ▼                        ▼
                ┌──────────────────┐  ┌────────────────────┐  ┌──────────────────────┐
                │  Shortcut /      │  │  Search Serving    │  │  AI Answer           │
                │  Instant Answer  │  │  (retrieval +      │  │  Generator           │
                │  (KG, struct     │  │   ranking)         │  │  (RAG pipeline)      │
                │   data, nav)     │  │                    │  │                      │
                └──────────────────┘  └────────┬───────────┘  └──────────┬───────────┘
                                               │                        │
                          ┌────────────────────┼────────────────────────┘
                          │                    │
                          ▼                    ▼
                ┌──────────────────────────────────────────┐
                │             SERP Assembler                │
                │  (merge ranked results + AI answer +      │
                │   verticals + knowledge panel +            │
                │   "people also ask")                       │
                └──────────────────────────────────────────┘

    ═══════════════════════════  OFFLINE PLANE  ════════════════════════════

    ┌──────────────────────────────────────────────────────────────────────┐
    │                      Crawling & Ingestion                            │
    │                                                                      │
    │  ┌─────────────┐  ┌──────────────┐  ┌────────────┐  ┌────────────┐  │
    │  │ URL Frontier │  │  Fetcher     │  │  Parser /  │  │  Document  │  │
    │  │ (priority    │──▶  (distributed │──▶  Content   │──▶  Under-   │  │
    │  │  scheduler)  │  │   HTTP)      │  │  Extractor │  │  standing  │  │
    │  └──────────────┘  └──────────────┘  └────────────┘  │  Pipeline  │  │
    │                                                       └─────┬──────┘  │
    └─────────────────────────────────────────────────────────────┼─────────┘
                                                                  │
                                                                  ▼
    ┌──────────────────────────────────────────────────────────────────────┐
    │                        Indexing Pipeline                              │
    │                                                                      │
    │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
    │  │  Inverted    │  │  Vector      │  │  Knowledge   │               │
    │  │  Index       │  │  Index       │  │  Graph       │               │
    │  │  Builder     │  │  Builder     │  │  Builder     │               │
    │  └──────────────┘  └──────────────┘  └──────────────┘               │
    │                                                                      │
    │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
    │  │  PageRank    │  │  Near-Dup    │  │  Real-Time   │               │
    │  │  Computer    │  │  Detector    │  │  Index       │               │
    │  └──────────────┘  └──────────────┘  └──────────────┘               │
    └──────────────────────────────────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────────────────────┐
    │                     Quality & Evaluation                             │
    │  Human raters ── NDCG/MRR metrics ── A/B testing ── Anti-abuse      │
    └──────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Scaling axis |
|-----------|---------------|--------------|
| Edge/CDN | TLS termination, static SERP assets, geographic routing, DDoS absorption | PoPs worldwide |
| API Gateway | Rate limiting, authentication, request routing, query logging | Horizontal, stateless |
| Autocomplete Service | Sub-50ms query suggestions from trie + query-log popularity + trending signals | In-memory, replicated per region |
| Query Processor | Spell correction, intent classification, query rewriting, entity linking, safety filtering, route decision | Horizontal, stateless; ML models served via sidecar |
| Search Serving | Fan-out to index shards, multi-stage ranking, result merging | Stateless coordinators + sharded index fleet |
| AI Answer Generator | RAG pipeline: passage retrieval, context assembly, LLM inference, citation extraction, grounding check | GPU fleet, autoscaled by queue depth |
| Shortcut/Instant Answer | Knowledge graph lookups, structured data extraction, navigational redirects | In-memory KG replicas |
| SERP Assembler | Merge all result streams, insert vertical modules, render final response | Stateless, latency-critical |
| Crawling & Ingestion | URL discovery, HTTP fetch, content extraction, document understanding, dedup | Horizontal crawler fleet, rate-limited per domain |
| Indexing Pipeline | Build/update inverted index, vector index, knowledge graph, link graph | Batch + incremental (MapReduce for batch, streaming for incremental) |
| Quality & Evaluation | Human rater pipelines, automated NDCG/MRR, A/B test framework, anti-abuse detection | Offline batch + real-time anomaly detection |

### Why This Shape

- **Online and offline planes are fully decoupled.** Crawling, indexing, and model training run independently of query serving. A backfill or re-index cannot starve live query latency.
- **The Query Processor is the brain.** It decides what happens to every query before any expensive work begins. A navigational query never touches the vector index or the LLM fleet.
- **AI answer generation is a parallel, optional path.** It runs concurrently with traditional ranking so the SERP can render ranked results immediately while the AI answer streams in. If the LLM fleet is degraded, the user still gets traditional results.
- **Every vertical (images, news, video, shopping, local) is a plug-in module** in the SERP Assembler, not a separate search engine. They share the query understanding pipeline but have their own index and ranking stacks.

---

## 4. Crawling and Indexing Pipeline

### URL Frontier and Crawl Prioritization

The URL frontier is the scheduler that decides which URLs to crawl next. At 5 billion pages/day, this is ~58,000 URLs/second sustained.

```python
class CrawlPriority:
    """ML-based crawl priority scoring. Trained on features:
    - historical change frequency (pages that change often get recrawled sooner)
    - PageRank / domain authority (high-value pages get priority)
    - content type (news pages >> static about pages)
    - time since last crawl vs. predicted change interval
    - click-through rate from search logs (pages users actually visit)
    """
    def score(self, url_record: URLRecord) -> float:
        features = {
            "domain_authority": url_record.domain_pagerank,
            "page_pagerank": url_record.page_pagerank,
            "change_frequency": url_record.avg_changes_per_day,
            "hours_since_crawl": url_record.hours_since_last_fetch,
            "predicted_change_hours": url_record.predicted_ttl_hours,
            "staleness_ratio": (url_record.hours_since_last_fetch /
                                max(url_record.predicted_ttl_hours, 1)),
            "content_type": url_record.content_type_id,  # news=1, blog=2, static=3, ...
            "click_rate_7d": url_record.click_through_rate_7d,
            "is_seed_domain": url_record.is_seed,
        }
        return self.model.predict(features)  # LightGBM, ~1us per prediction
```

The frontier is partitioned by domain to enforce politeness (one outstanding fetch per domain at a time, minimum delay between requests per `robots.txt` Crawl-delay directive or a default of 1 second).

```
URL Frontier architecture:

┌─────────────────────────────────────────────────┐
│  Priority Queue (per-domain buckets)             │
│                                                   │
│  example.com: [url_a(0.95), url_b(0.72), ...]   │
│  news.org:    [url_c(0.99), url_d(0.88), ...]   │
│  ...                                              │
│  Total domains tracked: ~500M                     │
│  Total URLs in frontier: ~50B                     │
└───────────────────────┬─────────────────────────┘
                        │ domain-level round-robin
                        │ weighted by domain priority
                        ▼
┌─────────────────────────────────────────────────┐
│  Fetcher Fleet (~10,000 workers)                 │
│                                                   │
│  Each worker:                                     │
│  1. Dequeue URL from frontier                     │
│  2. Check robots.txt cache (refreshed hourly)     │
│  3. Respect domain politeness delay               │
│  4. HTTP GET with timeout (30s connect, 60s read) │
│  5. Follow redirects (max 5 hops)                 │
│  6. Enqueue raw response to parser pipeline       │
│                                                   │
│  Throughput: ~500 fetches/sec per worker           │
│  Fleet total: 10,000 x 500 = 5M fetches/sec peak  │
│  (but politeness-limited to ~58,000 useful/sec)    │
└─────────────────────────────────────────────────┘
```

### Content Extraction and Document Understanding

Every fetched page goes through a multi-stage document understanding pipeline:

```
Raw HTML/PDF/media
      │
      ▼
┌──────────────────┐
│  1. Format Parse  │  HTML: readability-style main content extraction (ML-based
│                   │         boilerplate removal, not rule-based — trained on
│                   │         labeled data of "main content" vs. nav/footer/ads)
│                   │  PDF: Tika + OCR fallback (Tesseract)
│                   │  Images: CLIP embedding + OCR for text-in-image
│                   │  Structured: JSON-LD, schema.org, Open Graph extraction
└────────┬─────────┘
         ▼
┌──────────────────┐
│  2. NER + Entity  │  Named entity recognition (people, orgs, locations, dates,
│     Linking       │  products) using a fine-tuned BERT-NER model.
│                   │  Entities linked to knowledge graph IDs where possible.
└────────┬─────────┘
         ▼
┌──────────────────┐
│  3. Topic &       │  Multi-label topic classification (200 top-level categories).
│     Quality       │  Content quality score (0-1): trained on human-rated
│     Scoring       │  page quality data. Scores below 0.2 flagged as spam/low-quality.
│                   │  Factual claim extraction for high-quality pages.
└────────┬─────────┘
         ▼
┌──────────────────┐
│  4. Passage       │  Split document into passages (~200 tokens each) for
│     Segmentation  │  dense embedding. Heading-aware splitting (never breaks
│                   │  mid-paragraph). Each passage retains its document context
│                   │  (title, URL, section heading).
└────────┬─────────┘
         ▼
┌──────────────────┐
│  5. Embedding     │  Dense embedding (768d) for top-tier documents only (50B of
│     Generation    │  200B). Criteria: quality_score > 0.5, PageRank > threshold,
│                   │  or document is in a freshness-critical category (news).
│                   │  Model: fine-tuned E5-large or similar bi-encoder.
│                   │  Throughput: ~2,000 passages/sec per A10G GPU.
└────────┬─────────┘
         ▼
┌──────────────────┐
│  6. Near-Dup      │  SimHash (64-bit) for fuzzy duplicate detection.
│     Detection     │  Documents with Hamming distance <= 3 from an existing
│                   │  document are clustered. Canonical URL selected by:
│                   │  1. Original publisher (detected via first-crawl timestamp)
│                   │  2. Highest PageRank
│                   │  3. Most complete content
│                   │  Non-canonical copies indexed but demoted in ranking.
└──────────────────┘
```

### Crawl Throughput Math

```
Pages/day target:           5,000,000,000
Pages/second:               ~57,870

Document understanding pipeline per page:
  Content extraction:       ~50ms CPU
  NER + entity linking:     ~20ms GPU (batched)
  Topic/quality scoring:    ~10ms GPU (batched)
  Passage segmentation:     ~5ms CPU
  Embedding (if top-tier):  ~15ms GPU per passage, avg 5 passages/doc = 75ms
  Near-dup detection:       ~2ms CPU
  Total per page:           ~100-160ms (depending on whether embedding runs)

At 58K pages/sec, with 150ms avg per page:
  CPU workers:              58,000 x 0.15s = 8,700 CPU-seconds/sec of work
  With 4-core workers:      ~2,200 worker instances (CPU portion)
  GPU for NER/topic/embed:  ~500 A10G GPUs (batched inference)
```

---

## 5. Index Architecture

### Inverted Index

The inverted index is the backbone of lexical search, handling the majority of candidate retrieval.

```
Index structure (per shard, 100M documents):

Term Dictionary (in-memory, ~2.5 GB per shard):
  hash_map<term_id, PostingListPointer>
  50M unique terms per shard
  Each entry: term_hash(8B) + offset(8B) + doc_freq(4B) + metadata(4B) = 24B
  50M x 24B = 1.2 GB (plus hash table overhead ≈ 2.5 GB)

Posting Lists (memory-mapped, ~62 GB per shard):
  For each term: sorted list of (doc_id, term_frequency, field_weights)
  Delta-encoded doc_ids + varint compression (4x reduction from raw)

Field-level weighting (BM25F):
  title_weight:     3.0    (matches in title are 3x more valuable)
  heading_weight:   2.0
  body_weight:      1.0
  anchor_weight:    2.5    (anchor text from inbound links)
  url_weight:       1.5
```

**BM25 scoring** at query time:

```python
def bm25_score(query_terms: list[str], doc_id: int, shard: IndexShard) -> float:
    """Standard BM25 with field weighting (BM25F variant)."""
    k1 = 1.2
    b = 0.75
    score = 0.0
    avg_dl = shard.avg_doc_length
    doc_length = shard.doc_length(doc_id)

    for term in query_terms:
        df = shard.doc_frequency(term)
        idf = math.log((shard.num_docs - df + 0.5) / (df + 0.5) + 1)

        # Field-weighted term frequency
        tf_weighted = (
            shard.term_freq(term, doc_id, field="title") * 3.0 +
            shard.term_freq(term, doc_id, field="heading") * 2.0 +
            shard.term_freq(term, doc_id, field="body") * 1.0 +
            shard.term_freq(term, doc_id, field="anchor") * 2.5
        )
        numerator = tf_weighted * (k1 + 1)
        denominator = tf_weighted + k1 * (1 - b + b * doc_length / avg_dl)
        score += idf * (numerator / denominator)

    return score
```

### Tiered Index

Not all 200B documents deserve the same retrieval latency:

| Tier | Documents | Criteria | Serving | Latency budget |
|------|-----------|----------|---------|---------------|
| **Tier 0 (real-time)** | ~100M | Breaking news, live events, < 1 hour old | In-memory, all replicas | < 10ms |
| **Tier 1 (premium)** | 10B | High PageRank, high quality score, high click rate | SSD + hot cache, all replicas | < 50ms |
| **Tier 2 (standard)** | 50B | Moderate quality, moderate traffic | SSD, partial replicas | < 100ms |
| **Tier 3 (long-tail)** | 140B | Low quality, rarely accessed | SSD + cold storage, fewer replicas | < 200ms |

Most queries only hit Tier 0 + Tier 1 (enough for top-10 results). Tier 2/3 are queried only when Tier 0+1 yield insufficient results or the user pages deep.

### Vector Index (Semantic Search)

```
50 billion passages with 768-dimensional embeddings.
Index type: IVF-PQ (HNSW infeasible at this scale — see RAG platform §8.2)

IVF-PQ configuration:
  nlist (coarse centroids):        262,144 (2^18)
  PQ subvectors (m):              96
  PQ bits per subvector:           8 (256 centroids each)
  Code size per vector:            96 bytes (vs. 1,536 raw float16 — 16x compression)

Storage:
  PQ codes:  50B x 96 B = 4.8 TB (compressed, fits across shard fleet)
  Centroid index: 262,144 x 768 x 2 B (float16) = 384 MB (tiny, replicated to all nodes)
  Full-precision vectors (for rescore): stored on SSD, 50B x 1,536 B = 76.8 TB

Query flow:
  1. Encode query with same bi-encoder → 768d vector
  2. Find nearest nprobe=32 coarse centroids (fast, centroid table is small)
  3. Within those 32 clusters, PQ-distance scan of ~6M candidate vectors
  4. Top-500 candidates rescored against full-precision vectors from SSD
  5. Return top-50 to ranking pipeline

Shard sizing:
  50B / 25M per shard = 2,000 shards (same calculation as §2)
  Per shard PQ codes: 25M x 96 B = 2.4 GB (fits in RAM)
  Per shard full-precision: 25M x 1,536 B = 38.4 GB (SSD-resident)
```

### Knowledge Graph

A structured representation of entities and their relationships, used for instant answers and entity-enriched ranking.

```sql
-- Entity store (5 billion entities)
CREATE TABLE entities (
    entity_id       BIGINT PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    entity_type     SMALLINT NOT NULL,  -- person, org, location, product, concept, ...
    description     TEXT,
    properties      JSONB,              -- birth_date, headquarters, market_cap, etc.
    source_urls     TEXT[],             -- provenance for each property
    confidence      FLOAT,
    updated_at      TIMESTAMPTZ
);

-- Relationship store (50 billion triples)
CREATE TABLE triples (
    subject_id      BIGINT REFERENCES entities,
    predicate       SMALLINT NOT NULL,  -- founded_by, located_in, part_of, ...
    object_id       BIGINT REFERENCES entities,
    confidence      FLOAT,
    source_urls     TEXT[],
    valid_from      DATE,
    valid_to        DATE,               -- NULL = still true
    PRIMARY KEY (subject_id, predicate, object_id)
);

-- Indexes for all three access patterns
CREATE INDEX idx_spo ON triples (subject_id, predicate, object_id);
CREATE INDEX idx_pos ON triples (predicate, object_id, subject_id);
CREATE INDEX idx_osp ON triples (object_id, subject_id, predicate);
```

The KG is queried for:
1. **Instant answers**: "Who founded OpenAI?" -> KG lookup, no ranking needed.
2. **Entity cards**: "Albert Einstein" -> structured entity panel on SERP.
3. **Query enrichment**: "apple stock price" -> entity link to Apple Inc. (not fruit) for disambiguation.

### Real-Time Index

For breaking news and rapidly changing content, a separate in-memory index with sub-minute update latency:

```
Architecture:
  - Dedicated crawl stream for news sources (~50,000 domains)
  - RSS/Atom feed polling every 30 seconds for registered news sites
  - Twitter/social firehose integration for trending event detection
  - Sub-minute indexing: content → parse → index, skipping heavy NLP

Index:
  - In-memory inverted index (Lucene NRT / custom)
  - ~100M documents at any time (rolling 24-hour window)
  - Merged with main index results at query time
  - Documents "promoted" to the main Tier-1 index after validation
    (quality scoring, dedup) within 1-2 hours

Size:
  100M docs x 2 KB avg = 200 GB text
  Inverted index: ~600 GB (fits in RAM on a few high-memory nodes)
  Replicated 3x per region for availability
```

---

## 6. Query Understanding Pipeline

Every query passes through a multi-stage understanding pipeline before any retrieval begins. The total budget for this pipeline is 20-50ms.

### Latency Budget Breakdown

```
Total query understanding budget: 50ms max (P99)

Step 1: Spell correction + normalization       5ms
Step 2: Intent classification                  8ms
Step 3: Query rewriting / expansion           15ms  (LLM-based, only for complex queries)
Step 4: Entity linking                         8ms
Step 5: Safety classification                  5ms
Step 6: Route decision                         2ms
                                             -----
Total (worst case, all steps):               43ms
Typical (no LLM rewrite):                    28ms

Steps 1-2 and 4-5 run in parallel where possible.
Step 3 (LLM rewrite) only triggers for complex informational queries.
Step 6 depends on the output of steps 1-5.
```

### Intent Classification

A distilled BERT model (6 layers, 30M parameters) classifies each query into one of six intent categories:

```python
INTENT_CATEGORIES = {
    "navigational":          0,  # "youtube", "gmail login"
    "informational_simple":  1,  # "capital of france", "weather SF"
    "informational_complex": 2,  # "how does mRNA vaccine immunity wane"
    "transactional":         3,  # "buy iphone 16 pro", "cheapest flights to NYC"
    "local":                 4,  # "pizza near me", "dentist open sunday"
    "ambiguous":             5,  # "apple" (fruit? company? records?)
}

class IntentClassifier:
    """Fine-tuned DistilBERT, trained on 50M labeled query-intent pairs
    from search logs + human rater annotations.

    Accuracy: 94% on held-out test set.
    Latency: P50 = 3ms, P99 = 8ms on CPU (batched).
    """
    def classify(self, query: str) -> tuple[str, float]:
        tokens = self.tokenizer(query, max_length=64, truncation=True)
        logits = self.model(tokens)
        probs = softmax(logits)
        intent_id = argmax(probs)
        return INTENT_CATEGORIES[intent_id], probs[intent_id]
```

### Query Rewriting and Expansion

For complex informational queries, a small LLM (7B parameter, quantized) rewrites the query for better retrieval:

```python
REWRITE_PROMPT = """You are a search query optimizer. Given a user's search query,
generate 2-3 reformulated versions that would retrieve better documents.

Rules:
- Expand abbreviations and acronyms
- Make implicit context explicit
- Decompose multi-part questions into sub-queries
- Keep each reformulation concise (under 20 words)

User query: {query}
{conversation_context}

Reformulated queries (one per line):"""

class QueryRewriter:
    """Only invoked for intent=informational_complex (about 15% of queries).
    Uses a small, fast LLM (7B, 4-bit quantized) for low latency."""

    def rewrite(self, query: str, session_context: list[str] | None = None) -> list[str]:
        context = ""
        if session_context:
            context = f"\nPrevious queries in this session: {session_context[-3:]}"

        prompt = REWRITE_PROMPT.format(query=query, conversation_context=context)
        response = self.llm.generate(prompt, max_tokens=100, temperature=0.3)
        rewrites = [line.strip() for line in response.strip().split("\n") if line.strip()]
        return [query] + rewrites[:3]  # original + up to 3 rewrites
```

### Entity Linking

Entities in the query are identified and linked to knowledge graph IDs:

```python
class EntityLinker:
    """Two-stage: mention detection (NER) + disambiguation (entity linking).

    Example:
      "apple stock price" → [Entity(mention="apple", kg_id=KG_APPLE_INC,
                                     type="ORG", confidence=0.97)]
      "apple pie recipe"  → [Entity(mention="apple", kg_id=KG_APPLE_FRUIT,
                                     type="FOOD", confidence=0.91)]

    Disambiguation uses:
      1. Query context (other terms in the query)
      2. Entity prior probability (Apple Inc. is searched 100x more than the fruit)
      3. User's recent search history (if opted in)
    """
    def link(self, query: str) -> list[LinkedEntity]:
        mentions = self.ner_model.detect(query)  # ~3ms
        linked = []
        for mention in mentions:
            candidates = self.kg.candidate_entities(mention.text, limit=10)  # ~2ms
            if candidates:
                scored = self.disambiguator.rank(mention, candidates, query_context=query)
                if scored[0].confidence > 0.7:
                    linked.append(scored[0])
        return linked
```

### Safety Classification

```python
class SafetyClassifier:
    """Multi-label classifier detecting:
    - CSAM indicators → block immediately, report
    - Self-harm / violence → route to safety-filtered results
    - PII in query → strip before logging, warn user
    - Harmful intent → restricted result set

    Model: fine-tuned DistilBERT, 4ms P99.
    False positive rate: < 0.01% (aggressively tuned to avoid blocking benign queries).
    """
    ACTIONS = {
        "csam":       "block_and_report",
        "self_harm":  "safety_filter",
        "violence":   "safety_filter",
        "pii":        "strip_and_warn",
        "harmful":    "restricted",
        "safe":       "proceed",
    }
```

---

## 7. Multi-Stage Ranking System

### Pipeline Overview

```
                   candidates from retrieval
                   (thousands of documents)
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  STAGE 1: Candidate Retrieval          Budget: 50-100ms          │
│                                                                   │
│  BM25 lexical (inverted index):   top 1,000 from Tier 0+1       │
│  ANN vector (semantic):           top 500 from vector index      │
│  Union + dedup:                   ~1,200 unique candidates        │
└───────────────────────────────┬──────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  STAGE 2: Lightweight Ranker           Budget: 20-30ms           │
│                                                                   │
│  Model: LightGBM (gradient-boosted trees)                        │
│  Input features (per candidate):                                  │
│    - BM25 score                                                   │
│    - Vector similarity score                                      │
│    - PageRank (log-scaled)                                        │
│    - Domain authority                                              │
│    - Click-through rate (7-day rolling, position-debiased)        │
│    - Dwell time signal                                             │
│    - Freshness (hours since last crawl)                            │
│    - URL depth                                                     │
│    - Query-document language match                                 │
│    - Content quality score                                         │
│  Throughput: ~50,000 candidates/sec per core                      │
│  Output: top 100 candidates                                       │
└───────────────────────────────┬──────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  STAGE 3: Neural Cross-Encoder Reranker   Budget: 50-80ms        │
│                                                                   │
│  Model: fine-tuned cross-encoder (MiniLM-L12, 33M params)        │
│  Input: (query, document_passage) pairs, top 100 candidates       │
│  Batch inference on GPU, 100 pairs in one forward pass            │
│  Output: fine-grained relevance scores, top 20 candidates         │
│                                                                   │
│  P50 latency: 40ms (GPU, batched)                                 │
│  P99 latency: 80ms                                                │
└───────────────────────────────┬──────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  STAGE 4: Business Logic & Policy         Budget: 5-10ms         │
│                                                                   │
│  - Domain diversity: max 2 results from same domain               │
│  - Freshness boost for time-sensitive queries (2x score for       │
│    content < 24h old on news/event queries)                       │
│  - Safe-search filtering (block adult content if enabled)         │
│  - Legal removals (DMCA, right-to-be-forgotten)                   │
│  - Dedup: suppress near-duplicate pages (SimHash clusters)        │
│  Output: final ranked list of 10-20 results for SERP              │
└──────────────────────────────────────────────────────────────────┘
```

### Stage 2 Feature Engineering

```python
class LightweightRankerFeatures:
    """Feature extraction for the Stage 2 LightGBM model.
    All features must be computable in < 1ms per candidate."""

    def extract(self, query: str, candidate: Document, query_analysis: QueryAnalysis) -> dict:
        return {
            # Retrieval signals
            "bm25_score": candidate.bm25_score,
            "vector_sim": candidate.vector_similarity,  # -1 if no vector match

            # Static document quality
            "pagerank_log": math.log1p(candidate.pagerank),
            "domain_authority": candidate.domain.authority_score,
            "content_quality": candidate.quality_score,
            "spam_score": candidate.spam_score,

            # Engagement signals (from click logs, position-debiased)
            "ctr_7d": candidate.click_through_rate_7d,
            "avg_dwell_time_sec": candidate.avg_dwell_time,
            "pogo_stick_rate": candidate.pogo_stick_rate,  # quick back-clicks (bad signal)
            "long_click_rate": candidate.long_click_rate,   # dwell > 30s (good signal)

            # Freshness
            "hours_since_publish": candidate.hours_since_publish,
            "hours_since_crawl": candidate.hours_since_last_crawl,
            "is_time_sensitive_query": query_analysis.is_time_sensitive,

            # Query-document match
            "title_match_ratio": term_overlap(query, candidate.title),
            "url_match": int(any(t in candidate.url for t in query.split())),
            "language_match": int(query_analysis.language == candidate.language),
            "entity_match_count": count_entity_overlap(query_analysis.entities,
                                                        candidate.entities),
            # Structural
            "url_depth": candidate.url.count("/") - 2,
            "has_schema_org": int(candidate.has_structured_data),
        }
```

### Click-Through Feedback Loop

Click signals are the most powerful ranking feature but require careful debiasing:

```python
class PositionDebiasedCTR:
    """Position bias correction: a result at position 1 gets clicked more
    often than the same result at position 10, purely due to visibility.

    We use the Inverse Propensity Weighting (IPW) approach:
    - Estimate position_bias[pos] from randomized experiments
    - Adjust each click by 1/position_bias[pos]
    """
    # Learned from randomized position experiments
    POSITION_BIAS = {
        1: 1.0, 2: 0.65, 3: 0.45, 4: 0.35, 5: 0.28,
        6: 0.22, 7: 0.18, 8: 0.15, 9: 0.12, 10: 0.10
    }

    def compute_debiased_ctr(self, doc_id: str, clicks: list[ClickEvent]) -> float:
        weighted_clicks = sum(
            1.0 / self.POSITION_BIAS.get(c.position, 0.05)
            for c in clicks if c.doc_id == doc_id
        )
        weighted_impressions = sum(
            1.0 / self.POSITION_BIAS.get(c.position, 0.05)
            for c in clicks  # all impressions at that position
        )
        return weighted_clicks / max(weighted_impressions, 1)
```

---

## 8. AI Answer Generation

This is the hard part. The RAG pipeline for generating AI-synthesized answers with inline citations.

### RAG Pipeline End-to-End

```
Query (intent=informational_complex)
      │
      ▼
┌──────────────────────────────────────────────────────────────────┐
│  1. PASSAGE RETRIEVAL (parallel with ranking)     Budget: 100ms  │
│                                                                   │
│  Retrieve top-20 passages from vector index + inverted index     │
│  (using rewritten queries from §6 if available).                  │
│  Passages are ~200 tokens each, from diverse sources.             │
│  Dedup: no two passages from the same document.                   │
│  MMR applied to maximize information diversity (lambda=0.6).      │
└───────────────────────────────┬──────────────────────────────────┘
                                │ 20 passages (~4,000 tokens)
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  2. CONTEXT ASSEMBLY + RELEVANCE FILTERING     Budget: 30ms      │
│                                                                   │
│  Re-score passages with cross-encoder against original query.     │
│  Keep top-8 passages that score above relevance threshold.        │
│  Order by relevance (most relevant first — LLMs attend better     │
│  to content at the beginning and end of context).                 │
│  Total context: ~1,600 tokens                                     │
└───────────────────────────────┬──────────────────────────────────┘
                                │ 8 passages with source metadata
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  3. PROMPT CONSTRUCTION                         Budget: 5ms      │
│                                                                   │
│  Assemble the prompt from system instructions, retrieved          │
│  passages (with source IDs), and the user query.                  │
└───────────────────────────────┬──────────────────────────────────┘
                                │ prompt (~2,000 tokens)
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  4. LLM INFERENCE (streaming)                   Budget: 800ms    │
│                                                  TTFT target      │
│  Self-hosted 70B model with:                                      │
│  - 4x H100 tensor parallel                                       │
│  - Continuous batching (batch 16-32)                              │
│  - KV cache reuse for common system prompt prefix                 │
│  - Speculative decoding with 7B draft model (~1.8x speedup)      │
│                                                                   │
│  Stream tokens to client as they're generated.                    │
│  Avg output: ~300 tokens, ~2.5s total generation time.            │
└───────────────────────────────┬──────────────────────────────────┘
                                │ streamed answer tokens
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  5. CITATION EXTRACTION (inline, during generation)              │
│                                                                   │
│  The prompt instructs the LLM to cite sources using [1], [2]     │
│  notation. Post-processing maps these to source URLs.             │
│  Any claim without a citation is flagged for grounding check.     │
└───────────────────────────────┬──────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│  6. GROUNDING VERIFICATION (async, post-generation) Budget: 200ms│
│                                                                   │
│  For each factual claim in the answer:                            │
│  - Check if it's supported by the cited passage                   │
│  - Flag unsupported claims (potential hallucination)              │
│  - For YMYL queries: block answer if any claim is unsupported     │
│                                                                   │
│  Uses a small NLI (natural language inference) model:             │
│  entailment classifier on (passage, claim) pairs.                 │
│  Model: DeBERTa-v3-large fine-tuned on NLI, ~15ms per pair.      │
└──────────────────────────────────────────────────────────────────┘
```

### Prompt Template

```python
RAG_SYSTEM_PROMPT = """You are a search assistant. Answer the user's question using
ONLY the provided source passages. Follow these rules strictly:

1. Base your answer entirely on the provided sources. Do not use outside knowledge.
2. Cite sources inline using [1], [2], etc. corresponding to the source numbers below.
3. Every factual claim MUST have at least one citation.
4. If sources disagree, present both perspectives and note the disagreement.
5. If the sources do not contain enough information to fully answer the question,
   say so explicitly rather than guessing.
6. Keep the answer concise but complete. Target 150-300 words.
7. Use a neutral, informative tone."""

RAG_CONTEXT_TEMPLATE = """Sources:

{sources}

---

Question: {query}

Answer:"""

def build_prompt(query: str, passages: list[ScoredPassage]) -> str:
    sources = []
    for i, passage in enumerate(passages, 1):
        sources.append(f"[{i}] {passage.title} ({passage.url})\n{passage.text}")

    context = RAG_CONTEXT_TEMPLATE.format(
        sources="\n\n".join(sources),
        query=query,
    )
    return RAG_SYSTEM_PROMPT + "\n\n" + context
```

### Grounding Verification

```python
class GroundingChecker:
    """Post-generation verification that each claim in the AI answer
    is supported by the cited sources.

    Uses a DeBERTa-v3 NLI model (entailment / neutral / contradiction).
    """
    def verify(self, answer: str, passages: list[ScoredPassage],
               query_category: str) -> GroundingResult:
        claims = self.claim_extractor.extract(answer)  # sentence-level decomposition
        results = []

        for claim in claims:
            cited_sources = self.extract_citations(claim.text)
            if not cited_sources:
                results.append(ClaimResult(claim, "UNCITED", None))
                continue

            # Check entailment against each cited source
            supported = False
            for source_idx in cited_sources:
                passage = passages[source_idx - 1]
                label, confidence = self.nli_model.predict(
                    premise=passage.text,
                    hypothesis=claim.text_without_citations
                )
                if label == "entailment" and confidence > 0.8:
                    supported = True
                    break
                elif label == "contradiction" and confidence > 0.8:
                    results.append(ClaimResult(claim, "CONTRADICTED", source_idx))
                    break

            if not supported:
                results.append(ClaimResult(claim, "UNSUPPORTED", None))
            else:
                results.append(ClaimResult(claim, "SUPPORTED", source_idx))

        faithfulness_score = sum(
            1 for r in results if r.status == "SUPPORTED"
        ) / max(len(results), 1)

        # YMYL queries: block the entire answer if ANY claim is unsupported
        if query_category in ("medical", "financial", "legal") and faithfulness_score < 1.0:
            return GroundingResult(
                passed=False,
                faithfulness=faithfulness_score,
                action="BLOCK_AND_FALLBACK_TO_RANKED_RESULTS",
                claims=results,
            )

        # Non-YMYL: allow if >= 90% supported, flag unsupported claims with a caveat
        return GroundingResult(
            passed=faithfulness_score >= 0.90,
            faithfulness=faithfulness_score,
            action="SHOW" if faithfulness_score >= 0.90 else "SHOW_WITH_CAVEAT",
            claims=results,
        )
```

### Contradiction Handling

When retrieved sources disagree, the answer must surface the disagreement rather than silently picking a side:

```python
CONTRADICTION_PROMPT_ADDENDUM = """
IMPORTANT: When sources present conflicting information, you MUST:
1. Acknowledge the disagreement explicitly
2. Present each perspective with its source citation
3. If one source is clearly more authoritative (e.g., a government agency
   vs. a blog post), note this, but still present both views
4. Do NOT silently choose one side

Example format for disagreements:
"According to [1], X is true. However, [3] presents a different view,
stating Y. The discrepancy may be due to [your analysis of why they differ]."
"""
```

### LLM Inference Optimization

The LLM fleet is the most expensive component. Every optimization matters at 1B answers/day:

```
Optimization                         Impact on throughput      Notes
─────────────────────────────────────────────────────────────────────────────
Continuous batching (vLLM/TRT-LLM)   2-3x vs. static batching  Processes new requests
                                                                 without waiting for
                                                                 longest sequence to finish

Speculative decoding (7B draft model) ~1.8x decode speedup      Draft model generates
                                                                 candidate tokens, 70B
                                                                 verifies in parallel

KV cache reuse for system prompt      ~20% prefill savings      System prompt is identical
                                                                 across requests; cache once
                                                                 per batch

PagedAttention (vLLM)                 ~2-4x memory efficiency   Eliminates KV cache
                                                                 fragmentation, enables
                                                                 larger batches

int8 weight quantization (GPTQ/AWQ)   2x memory reduction,     Enables 70B on 4xH100
                                      ~5% quality loss          instead of 8xH100

Chunked prefill                       Avoids head-of-line       Long-context requests don't
                                      blocking                  block short ones during
                                                                 prefill phase
```

### Answer Cache

```python
class AnswerCache:
    """Cache AI-generated answers for repeated or similar queries.

    Cache key: normalized_query + top-k_passage_ids (content-addressed).
    This ensures a cached answer is only served when the same evidence
    would be retrieved, so the citations remain valid.

    TTL: 1 hour for non-time-sensitive queries, 5 minutes for news queries.
    """
    def cache_key(self, query: str, passage_ids: list[str]) -> str:
        normalized = query.strip().lower()
        passage_fingerprint = hashlib.sha256(
            "|".join(sorted(passage_ids)).encode()
        ).hexdigest()[:16]
        return f"answer:{normalized}:{passage_fingerprint}"

    def get(self, query: str, passage_ids: list[str]) -> CachedAnswer | None:
        key = self.cache_key(query, passage_ids)
        cached = self.redis.get(key)
        if cached:
            answer = CachedAnswer.deserialize(cached)
            if answer.is_expired():
                return None
            return answer
        return None

    def put(self, query: str, passage_ids: list[str], answer: str,
            citations: list[Citation], ttl_seconds: int) -> None:
        key = self.cache_key(query, passage_ids)
        cached = CachedAnswer(answer=answer, citations=citations,
                              created_at=now(), ttl=ttl_seconds)
        self.redis.setex(key, ttl_seconds, cached.serialize())
```

---

## 9. Query Routing Logic

The query router is the most important cost-control mechanism. It decides within milliseconds which treatment each query receives.

```python
class QueryRouter:
    """Routes each query to the cheapest treatment that meets quality expectations.

    Decision tree (in priority order):
    1. Safety-blocked queries → safety response (no search)
    2. Navigational queries → direct URL redirect (no ranking, no LLM)
    3. Simple factual queries → knowledge graph instant answer (no LLM)
    4. Live data queries → structured data API (weather, stocks, sports)
    5. Transactional queries → product/shopping vertical (no LLM)
    6. Local queries → local search vertical (no LLM)
    7. Complex informational → full ranking + AI answer (LLM invoked)
    8. Ambiguous → diversified ranked results + "did you mean?" (no LLM)
    """

    def route(self, query_analysis: QueryAnalysis) -> RouteDecision:
        intent = query_analysis.intent
        confidence = query_analysis.intent_confidence

        # 1. Safety
        if query_analysis.safety_action != "proceed":
            return RouteDecision(
                treatment="safety",
                invoke_llm=False,
                cost_tier="zero",
            )

        # 2. Navigational (high confidence)
        if intent == "navigational" and confidence > 0.85:
            return RouteDecision(
                treatment="navigate",
                invoke_llm=False,
                cost_tier="minimal",  # only URL lookup
            )

        # 3. Simple factual with KG answer
        if intent == "informational_simple" and confidence > 0.80:
            kg_answer = self.knowledge_graph.try_answer(query_analysis)
            if kg_answer and kg_answer.confidence > 0.9:
                return RouteDecision(
                    treatment="instant_answer",
                    invoke_llm=False,
                    instant_answer=kg_answer,
                    cost_tier="low",
                )

        # 4. Live data
        if query_analysis.needs_live_data:
            return RouteDecision(
                treatment="live_data",
                invoke_llm=False,
                data_api=query_analysis.live_data_source,  # weather/stocks/sports
                cost_tier="low",
            )

        # 5. Transactional
        if intent == "transactional" and confidence > 0.80:
            return RouteDecision(
                treatment="shopping",
                invoke_llm=False,
                cost_tier="medium",
            )

        # 6. Local
        if intent == "local" and confidence > 0.80:
            return RouteDecision(
                treatment="local_search",
                invoke_llm=False,
                cost_tier="medium",
            )

        # 7. Complex informational — the LLM path
        if intent == "informational_complex" and confidence > 0.70:
            return RouteDecision(
                treatment="ai_answer",
                invoke_llm=True,
                cost_tier="high",
            )

        # 8. Ambiguous or low-confidence
        return RouteDecision(
            treatment="diversified_ranking",
            invoke_llm=False,
            cost_tier="medium",
        )
```

### Routing Cost Impact

```
Query distribution (from search log analysis):

Intent                    % of queries    LLM invoked?    Cost/query
─────────────────────────────────────────────────────────────────────
Navigational              25%             No              $0.00001
Informational (simple)    20%             No              $0.00005
Informational (complex)   20%             Yes             $0.0015
Transactional             15%             No              $0.00008
Local                     10%             No              $0.00006
Ambiguous                 10%             No              $0.00005
─────────────────────────────────────────────────────────────────────

Without routing (LLM on every query):
  5B queries/day x $0.0015 = $7,500,000/day

With routing (LLM on 20%):
  1B x $0.0015 + 4B x $0.00005 = $1,500,000 + $200,000 = $1,700,000/day

Routing saves $5.8M/day = $2.1B/year
This is why the query router is the single most valuable component in the system.
```

---

## 10. Serving Architecture

### End-to-End Query Flow with Latency Budget

```
User types query and hits Enter
      │  t=0ms
      ▼
Edge CDN: TLS termination, geographic routing
      │  t=10ms
      ▼
API Gateway: rate limit check, auth, request ID assignment
      │  t=15ms
      ▼
Query Processor:
  ├── Spell correction + normalization               t=20ms
  ├── Intent classification (parallel)                t=23ms
  ├── Entity linking (parallel)                       t=23ms
  ├── Safety classification (parallel)                t=23ms
  ├── Route decision                                  t=25ms
  └── Query rewrite (if complex, parallel with above) t=35ms
      │
      ▼  t=35ms
      │
      ├─────────────────────────────────────────────────────┐
      │ TRADITIONAL RANKING PATH                             │ AI ANSWER PATH
      │ (always runs)                                        │ (only for complex queries)
      │                                                      │
      ▼                                                      ▼
Search Serving:                                    AI Answer Generator:
  Stage 1: Candidate retrieval                       Passage retrieval
    ├── BM25 fan-out to index shards (50ms)           (reuses ranking candidates)
    └── ANN fan-out to vector shards (50ms)           t=85ms (50ms parallel with ranking)
    (parallel, take the slower: 50ms)                  │
    t=85ms                                             ▼
      │                                              Context assembly + prompt build
      ▼                                               t=115ms
  Stage 2: Lightweight ranker (20ms)                   │
    t=105ms                                            ▼
      │                                              LLM inference begins (streaming)
      ▼                                               First token: t=800ms (target)
  Stage 3: Neural reranker (60ms)                     Full answer: t=3000ms (target)
    t=165ms                                            │
      │                                                │ (tokens stream to client
      ▼                                                │  as they're generated)
  Stage 4: Business logic (5ms)                        │
    t=170ms                                            │
      │                                                │
      ▼                                                ▼
SERP Assembler:                                    Answer + citations appended
  Merge ranked results + verticals + KG panel       to SERP (streaming)
  t=180ms
      │
      ▼
Response sent to client
  Non-AI SERP: t=180-200ms (P50)
  AI answer first token: t=800ms (P50), continues streaming
  AI answer complete: t=3000ms (P50)
```

### Fan-Out and Shard Coordination

```python
class SearchCoordinator:
    """Coordinates fan-out to index shards and merges results.

    Fan-out strategy:
    - Tier 0 (real-time): always queried (all shards, small index)
    - Tier 1 (premium): always queried (subset of shards based on query hash)
    - Tier 2 (standard): queried if Tier 0+1 yield < 50 candidates
    - Tier 3 (long-tail): queried only on explicit "more results" pagination

    Per-shard timeout: 150ms (P99). A slow shard is abandoned; results
    are assembled from responding shards. Missing one shard out of 50
    costs ~2% recall — acceptable for latency.
    """
    async def search(self, query: ProcessedQuery, config: SearchConfig) -> SearchResults:
        # Phase 1: Always query Tier 0 + Tier 1 (parallel)
        tier0_task = self.fan_out(query, tier=0, timeout_ms=50)
        tier1_task = self.fan_out(query, tier=1, timeout_ms=150)

        tier0_results, tier1_results = await asyncio.gather(
            tier0_task, tier1_task
        )

        candidates = merge_results(tier0_results, tier1_results)

        # Phase 2: If insufficient candidates, query Tier 2
        if len(candidates) < config.min_candidates:
            tier2_results = await self.fan_out(query, tier=2, timeout_ms=200)
            candidates = merge_results(candidates, tier2_results)

        return candidates

    async def fan_out(self, query, tier, timeout_ms):
        shards = self.shard_router.shards_for(query, tier)
        tasks = [shard.search(query) for shard in shards]
        results = await asyncio.gather(
            *tasks, return_exceptions=True
        )
        # Filter out timeouts/errors — partial results are fine
        return [r for r in results if not isinstance(r, Exception)]
```

### Graceful Degradation

```
Degradation hierarchy (from least to most impact):

Level 0 (normal):     Full pipeline — AI answers + ranked results + verticals
Level 1 (LLM stress): Disable AI answers for low-confidence queries
                       (tighten routing threshold: complex_confidence > 0.9)
Level 2 (LLM down):   No AI answers; traditional SERP only.
                       Users see ranked results, KG instant answers,
                       "AI answers temporarily unavailable" banner.
Level 3 (ranking degraded): Skip Stage 3 reranker; serve Stage 2 results.
                       ~5% quality drop, but 60ms latency savings.
Level 4 (partial index): Some shards down. Results from available shards
                       only. Recall degrades proportionally.
Level 5 (region down): Route all traffic to surviving regions.
                       Latency increases by 50-100ms (cross-region).
```

---

## 11. Freshness Architecture

### Breaking News Pipeline

```
Goal: news article published → indexed and searchable in < 1 minute.

                 ┌──────────────────────────────────────────────┐
                 │  News Source Feeds                            │
                 │  - 50,000 RSS/Atom feeds polled every 30s    │
                 │  - PubSubHubbub/WebSub push for major sources│
                 │  - Social signal detection (trending topics   │
                 │    trigger targeted crawl of related URLs)    │
                 └───────────────────────┬──────────────────────┘
                                         │
                                         ▼
                 ┌──────────────────────────────────────────────┐
                 │  Fast-Track Fetcher                           │
                 │  - Dedicated crawler fleet for news           │
                 │  - Skip politeness delay for known news sites │
                 │    (pre-negotiated crawl agreements)          │
                 │  - Fetch + parse in < 5 seconds               │
                 └───────────────────────┬──────────────────────┘
                                         │
                                         ▼
                 ┌──────────────────────────────────────────────┐
                 │  Lightweight Processing (skip heavy NLP)      │
                 │  - Title + body extraction (readability)      │
                 │  - Entity detection (fast NER only)           │
                 │  - Quick quality check (is it spam?)          │
                 │  - No dense embedding (added later in batch)  │
                 │  Total: < 10 seconds                          │
                 └───────────────────────┬──────────────────────┘
                                         │
                                         ▼
                 ┌──────────────────────────────────────────────┐
                 │  Real-Time Index (Tier 0)                     │
                 │  - In-memory inverted index                   │
                 │  - Near-real-time commit (NRT): new doc        │
                 │    searchable within 1 second of write         │
                 │  - Rolling 24-hour window                      │
                 │  - Merged with main index at query time        │
                 └──────────────────────────────────────────────┘
                                         │
                                         │ (async, within 1-2 hours)
                                         ▼
                 ┌──────────────────────────────────────────────┐
                 │  Promotion to Main Index                      │
                 │  - Full NLP pipeline (NER, topic, quality)    │
                 │  - Dense embedding generation                 │
                 │  - Near-dup detection and canonical selection │
                 │  - Inserted into Tier 1 inverted + vector     │
                 │    index with full ranking signals             │
                 └──────────────────────────────────────────────┘

Latency breakdown (news article → searchable):
  Feed detection:            0-30s (polling interval)
  Fetch:                     2-5s
  Lightweight processing:    3-10s
  Real-time index write:     < 1s
  ─────────────────────────────────
  Total:                     5-45s (P50 ~15s, P99 ~55s)
```

### Freshness-Aware Ranking

For time-sensitive queries (detected by intent classifier), the ranking pipeline applies a freshness boost:

```python
def freshness_boost(doc_age_hours: float, query_is_time_sensitive: bool) -> float:
    """Multiplicative boost applied in Stage 2 ranking for fresh content.

    For time-sensitive queries (news, events, "latest"):
      - Content < 1 hour old: 3x boost
      - Content < 6 hours old: 2x boost
      - Content < 24 hours old: 1.5x boost
      - Content > 7 days old: 0.8x penalty

    For non-time-sensitive queries:
      - No boost/penalty (freshness is not a ranking signal)
    """
    if not query_is_time_sensitive:
        return 1.0

    if doc_age_hours < 1:
        return 3.0
    elif doc_age_hours < 6:
        return 2.0
    elif doc_age_hours < 24:
        return 1.5
    elif doc_age_hours > 168:  # > 7 days
        return 0.8
    return 1.0
```

---

## 12. Failure Walkthroughs

### Failure 1: Data Center Goes Down

```
Scenario: US-East data center suffers a complete power failure.

Detection:
  - Health checks from other regions fail within 10 seconds
  - DNS health-check probes (Route 53 style) detect the failure within 30s
  - Automated failover begins

Impact:
  - US-East served ~35% of global traffic (60K QPS → ~21K QPS affected)

Response:
  t=0s:     Power failure. US-East stops responding.
  t=10s:    Health checks fail. Load balancers mark US-East unhealthy.
  t=30s:    DNS failover routes US-East users to US-West and EU-West.
  t=60s:    Traffic redistributed. US-West and EU-West absorb the extra load.
            Pre-provisioned headroom (150K peak capacity across all regions)
            handles the redistributed 21K QPS.
  t=60s+:   Latency increases 50-100ms for affected users (cross-region hop).
            No data loss: all indexes are replicated 3x across regions.
            AI answer capacity may be strained → tighten routing thresholds
            (Level 1 degradation: only highest-confidence queries get LLM answers).

Recovery:
  - US-East comes back online, resyncs any index updates missed during outage.
  - DNS gradually shifts traffic back (10% increments over 30 minutes).
  - Full recovery within 1 hour of datacenter restoration.

Key design property:
  No single-region failure causes user-visible outage. All data is replicated.
  Cross-region traffic adds latency but not errors.
```

### Failure 2: LLM Serving Layer Degradation

```
Scenario: GPU node failures reduce LLM fleet capacity by 40%.

Detection:
  - LLM request queue depth spikes (> 100 requests per instance)
  - P99 TTFT exceeds 5 seconds (target: 2s)
  - Automated capacity alert fires

Response:
  t=0s:     40% of LLM instances fail (GPU hardware errors after firmware update).
  t=5s:     Queue depth monitoring detects backpressure.
  t=10s:    Automated degradation engages:
            1. Query router tightens AI-answer threshold
               (confidence > 0.7 → confidence > 0.95)
               → reduces AI answer traffic by ~60%
            2. Remaining 60% of fleet handles ~40% of normal AI traffic
               → roughly balanced again
  t=30s:    Additional cost optimization:
            - Enable aggressive answer caching (extend TTL from 1h to 4h)
            - Route overflow to smaller model (13B) for simple AI answers
            - Complex AI answers only from the 70B fleet
  t=60s:    Stable. Users see:
            - AI answers for 8% of queries (down from 20%)
            - Traditional ranked results for the rest
            - "AI answer unavailable for this query" for some complex queries

  Ongoing:  Auto-scaler provisions replacement GPU instances.
            (Cloud GPU provisioning: 5-30 minutes depending on availability)
            Full capacity restored within 1-2 hours.

User impact: Degraded, not broken. Most users never notice because most
queries don't trigger AI answers anyway. The 12% of queries that lose
AI answers still get high-quality ranked results.
```

### Failure 3: Ranking Model Returns Garbage

```
Scenario: A bad model update to Stage 2 ranker causes relevance scores
to be essentially random.

Detection (layered, because no single signal catches this immediately):
  1. Automated NDCG regression detection (runs hourly on golden query set):
     NDCG@10 drops from 0.78 to 0.31 → P0 alert fires.
     Detection time: up to 1 hour.

  2. Online engagement metrics (faster):
     - Click-through rate on position 1 drops from 45% to 12%
     - Pogo-stick rate (quick back-clicks) spikes from 8% to 35%
     - Long-click rate drops from 32% to 8%
     Detection time: 15-30 minutes (need statistical significance)

  3. Manual escalation: user complaints spike in support channels.
     Detection time: variable, but often fastest for catastrophic failures.

Response:
  t=0min:   Bad model pushed to production.
  t=15min:  Online metrics anomaly detection fires.
  t=20min:  Automated rollback triggered:
            1. Stage 2 ranker reverted to previous model version
               (model binaries are versioned, rollback is a config change)
            2. Traffic gradually shifted: 10% → 50% → 100% on old model
               over 5 minutes (canary rollback)
  t=25min:  Old model fully serving. NDCG recovering.

Prevention:
  - All model updates go through shadow evaluation on live traffic
    before receiving any real traffic (shadow mode: new model scores
    candidates alongside old model, results compared but not served)
  - Canary deployment: new model serves 1% of traffic for 1 hour,
    engagement metrics compared with control before wider rollout
  - Automatic rollback trigger if NDCG drops > 5% relative
```

### Failure 4: Coordinated SEO Spam Attack

```
Scenario: A spam network creates 10M pages optimized to rank for health
queries, containing misleading medical information designed to also
manipulate AI-generated answers via prompt injection in page content.

Detection layers:
  1. Crawl-time quality scoring:
     - Content quality model flags 70% of spam pages (quality < 0.2)
     - Domain authority: new domains with no link graph presence → suspicious
     - Burst detection: 10M pages from a small set of new domains in 48 hours

  2. Index-time dedup:
     - SimHash detects 80% of spam pages as near-duplicates of each other
     - Cluster of 10M near-identical pages → automatic spam flag

  3. Ranking-time signals:
     - Zero click-through history on these new pages
     - Domain has no engagement signals
     - Stage 2 ranker naturally deprioritizes (no CTR, no dwell time,
       no PageRank)

  4. AI answer protection:
     - Prompt injection detection in retrieved passages:
       Pattern matching for common injection patterns
       ("ignore previous instructions", "you are now", etc.)
     - Grounding check (§8) catches hallucinated claims
     - YMYL detection blocks ungrounded medical claims

Response:
  t=0h:     Spam pages start appearing in crawl pipeline.
  t=1h:     Quality scoring flags most pages. Few enter the index.
  t=6h:     Anti-abuse team notified of the domain cluster anomaly.
  t=12h:    Manual review confirms spam campaign. Actions:
            1. Domain-level blacklist for the spam network
            2. Retroactive removal of any indexed pages from these domains
            3. Crawl frontier deprioritizes related IP ranges
            4. AI answer prompt injection patterns added to blocklist

Impact: minimal if detection layers work. The multi-layered approach
(quality scoring + dedup + ranking signals + grounding) means no single
layer needs to be perfect — spam must evade ALL layers to actually
affect search results or AI answers.
```

---

## 13. Cost Model

### Per-Query Cost Breakdown

```
Component                     Cost/query    Basis
──────────────────────────────────────────────────────────────────
Edge / CDN                    $0.000002     bandwidth: ~20 KB/query, CDN at $0.10/GB
API Gateway                   $0.000001     compute: negligible at amortized fleet cost
Query understanding           $0.000008     CPU: ~50ms, at $0.05/core-hour
  (spell, intent, entity, safety)
Index shard queries           $0.000020     80 shard RPCs at ~$0.00000025 each
  (BM25 + ANN fan-out)                      (shared fleet, amortized)
Stage 2 ranking               $0.000005     CPU: ~20ms on 1,200 candidates
Stage 3 neural reranker       $0.000015     GPU: ~60ms on 100 candidates, shared A10G fleet
Business logic / assembly     $0.000002     CPU: negligible
──────────────────────────────────────────────────────────────────
Total (non-AI query):         ~$0.00005     = $0.05 per 1,000 queries

AI answer (when triggered):
  Passage retrieval (incl.)   $0.000020     (included above, uses same retrieval path)
  Context assembly            $0.000002     CPU
  LLM inference (70B, H100)   $0.001200     ~2,300 tokens in + 300 tokens out
                                             on 4xH100 instance at $12/GPU-hr
                                             ($48/instance-hr / ~30 req/s = $0.00044/req
                                             x 2.7 for overhead/peaks ≈ $0.0012)
  Grounding check             $0.000030     NLI model: 8 claims x ~15ms GPU each
  Streaming overhead          $0.000005     long-lived connection, bandwidth
──────────────────────────────────────────────────────────────────
Total (AI query):             ~$0.0015      = $1.50 per 1,000 queries
```

### Daily Cost at Scale

```
Non-AI queries:  4,000,000,000 x $0.00005 = $200,000/day
AI queries:      1,000,000,000 x $0.0015  = $1,500,000/day
──────────────────────────────────────────────────────────────
Total query serving:                         $1,700,000/day

With 35% answer cache hit rate:
AI queries billed:  650,000,000 x $0.0015 = $975,000/day
Total:                                       $1,175,000/day

Crawling & indexing (amortized daily):
  Crawler fleet:     $50,000/day    (10K workers, ~$5 each amortized)
  Parsing/NLP:       $30,000/day    (CPU + GPU fleet)
  Embedding gen:     $20,000/day    (GPU fleet for new/updated passages)
  Index building:    $15,000/day    (MapReduce jobs, incremental updates)
  Storage:           $25,000/day    (~900 TB globally at $0.03/GB-month)
  ─────────────────────────────────
  Total offline:     $140,000/day

Grand total:         $1,315,000/day = ~$480M/year

Revenue context: Google's search revenue is ~$300B/year.
At $480M/year in infrastructure cost, this is ~0.16% of that revenue.
Even with generous margins, this is economically viable for a major
search engine operator.
```

### Cost Levers

| Lever | Mechanism | Savings potential |
|-------|-----------|------------------|
| **Query routing** | Only invoke LLM for complex queries (20% vs 100%) | 4.4x reduction in LLM cost |
| **Answer caching** | Cache popular AI answers (35% hit rate) | 35% reduction in LLM cost |
| **Model tiering** | Use 13B model for simpler AI answers, 70B only for complex | 2-3x reduction for simple answers |
| **Speculative decoding** | 7B draft + 70B verifier | 1.8x throughput improvement |
| **Quantization** | int8 weights, PagedAttention | 2x memory efficiency → fewer GPUs |
| **Prompt caching** | Cache KV for system prompt prefix | ~20% prefill savings |
| **Tiered retrieval** | Query lower tiers only when needed | 30-50% reduction in shard queries |
| **Off-peak scaling** | Scale down GPU fleet during low-traffic hours | 15-20% compute savings |

---

## 14. Evolution Path

### v1: Vertical Search Engine (3-6 months, team of 10)

**Scope**: A specific domain, e.g., academic paper search or code search.

```
Corpus size:           10-50M documents
Queries/day:           1-10M
AI answers:            100K-1M/day
Infrastructure:        50-100 servers, 16-32 GPUs

Components built:
  - Single-tier inverted index (Elasticsearch/Solr)
  - Vector index (HNSW in Qdrant/Weaviate, or pgvector)
  - Basic query understanding (spell check, intent classification)
  - Two-stage ranking (BM25 + lightweight reranker)
  - Simple RAG pipeline (retrieve top-5, prompt LLM, no grounding check)
  - Basic SERP with AI answers
  - Batch crawler (scheduled, not real-time)

Not built yet:
  - Multi-tier index
  - Real-time index
  - Knowledge graph
  - Neural cross-encoder reranker
  - Grounding verification
  - Multi-datacenter replication
  - Sophisticated query routing

Cost: ~$50K-100K/month
Team: 3 backend, 2 ML, 2 infra, 1 frontend, 1 PM, 1 lead
```

### v2: Multi-Vertical with Quality (6-18 months, team of 30)

```
Corpus size:           1-10B documents
Queries/day:           50-500M
AI answers:            10-100M/day
Infrastructure:        500-2,000 servers, 200-500 GPUs

New components:
  - Tiered index (Tier 1 + Tier 2)
  - Custom inverted index (outgrow Elasticsearch sharding limits)
  - Knowledge graph (bootstrapped from Wikidata + extracted entities)
  - Cross-encoder reranker (Stage 3)
  - Grounding verification for AI answers
  - Query routing with cost awareness
  - Answer caching
  - A/B testing infrastructure
  - Multi-region deployment (2 regions)
  - Real-time index for news vertical
  - Image search (CLIP embeddings)

Cost: ~$500K-2M/month
```

### v3: Web-Scale (18-36 months, team of 100+)

```
Corpus size:           200B documents
Queries/day:           5B+
AI answers:            1B/day
Infrastructure:        50,000+ servers, 4,000+ GPUs

New components:
  - Full 4-tier index with Tier 3 (long-tail)
  - IVF-PQ vector index at 50B scale
  - Complete knowledge graph (5B entities)
  - ML-driven crawl prioritization
  - Sub-minute freshness for breaking news
  - Multi-turn conversational search
  - All verticals (news, video, shopping, local, academic)
  - Full anti-abuse / anti-manipulation pipeline
  - 3+ datacenter global deployment
  - Speculative decoding + model tiering for LLM fleet
  - Human evaluation pipeline at scale

Cost: ~$30-50M/month
```

### Migration Strategies Between Versions

```
v1 → v2:
  - Replace Elasticsearch with custom sharded inverted index
    (Elasticsearch sharding model doesn't scale past ~10B docs efficiently)
  - Migration: dual-write period where both old and new index receive writes,
    shadow-compare query results, cutover when parity confirmed.
  - Knowledge graph: bootstrap from Wikidata dump, then enrich incrementally
    from crawled entity extractions.

v2 → v3:
  - Vector index: migrate from HNSW to IVF-PQ for the growing corpus.
    Handled identically to the RAG platform's model migration protocol (§7.4):
    build new IVF-PQ index in parallel, shadow-evaluate recall, cutover.
  - Crawl infrastructure: transition from batch crawler to continuous crawler
    with ML-driven prioritization. The URL frontier is a new component;
    the fetcher fleet scales horizontally.
  - LLM fleet: start with API-based models in v1, transition to self-hosted
    as volume makes self-hosting cheaper (crossover at ~10M AI answers/day
    based on §13's cost math).
```

---

## 15. Trade-offs

### Lexical vs. Semantic Retrieval Balance

```
Decision: hybrid search with tunable alpha, not one or the other.

Lexical (BM25) excels at:
  - Exact match queries ("error ERR_TIMEOUT_504")
  - Named entity queries ("Barack Obama birthday")
  - Navigational queries ("github login")
  - Queries with rare/technical terms
  Cost: cheap (inverted index is CPU-only, well-understood)

Semantic (vector) excels at:
  - Paraphrase queries ("how to fix a slow computer" matching "speed up PC")
  - Conceptual queries ("what causes climate change")
  - Multi-hop reasoning queries
  - Cross-language retrieval
  Cost: expensive (embedding generation + GPU-resident index)

Trade-off:
  At web scale (200B docs), vector-indexing ALL documents is prohibitively
  expensive (~$75 TB of vector storage). We vector-index only the top-quality
  tier (50B docs, 25% of corpus). This means semantic search only covers
  high-quality content — which is acceptable because the long tail of low-
  quality pages rarely benefits from semantic retrieval anyway (they're
  typically not the kind of content that needs paraphrase matching).

  alpha (dense weight) defaults:
    News/event queries:     0.3 (more lexical — exact terms matter)
    Technical queries:      0.4
    General informational:  0.6 (balanced)
    Conceptual queries:     0.8 (more semantic)
  Alpha is set by the intent classifier, not hardcoded globally.
```

### LLM Answer Quality vs. Latency vs. Cost

```
The three-way trade-off at the heart of AI search:

                    Quality
                      /\
                     /  \
                    /    \
                   /      \
                  / sweet   \
                 /  spot     \
                /             \
               /______________\
           Latency ———————————— Cost

Option A: Largest model (175B+), high quality, slow (5-10s), very expensive
  → Only viable for <1% of queries at web scale.

Option B: Medium model (70B quantized), good quality, moderate (2-4s), expensive
  → Our primary choice for complex queries (20% of traffic).

Option C: Small model (13B), decent quality, fast (0.5-1.5s), cheap
  → For "medium complexity" queries where a simpler answer suffices.

Option D: No model (traditional results), varies, fastest, cheapest
  → For 80% of queries. The default.

Our resolution:
  - Route queries to the cheapest option that meets quality expectations (§9)
  - Use model tiering: simple AI answers → 13B, complex → 70B
  - Use answer caching to amortize cost across repeated queries
  - Use speculative decoding to get closer to "Option C latency, Option B quality"
  - Accept that AI answers add 2-3 seconds to user-perceived latency, but
    stream tokens so the user sees content within 800ms
```

### Freshness vs. Index Completeness

```
Decision: dual-index architecture (real-time + main).

Problem: you cannot have both sub-minute freshness AND full document
understanding (NER, quality scoring, dense embedding, PageRank update)
on the same content. Full processing takes minutes to hours.

Resolution:
  - Real-time index: fast, shallow processing. Content searchable in
    < 1 minute but with degraded ranking signals (no PageRank, no
    dense embedding, no full NER). Acceptable because breaking news
    queries are dominated by recency, not relevance.
  - Main index: slow, deep processing. Full understanding pipeline,
    dense embeddings, PageRank. Content promoted from real-time to
    main within 1-2 hours.
  - Query-time merge: results from both indexes are combined, with
    the real-time index receiving a freshness boost for time-sensitive
    queries (§11 ranking boost).

The trade-off: a brand-new page in the real-time index has weaker
ranking signals. A rare non-time-sensitive query that should match
a just-crawled page might not rank it well until it moves to the main
index. This is acceptable because such queries are rare (most queries
matching brand-new content ARE time-sensitive).
```

### Personalization vs. Privacy

```
Decision: opt-in, feature-level personalization. No personalized index.

What we personalize (opt-in):
  - Language preference (strongly impacts result set)
  - Location (for local queries)
  - SafeSearch level
  - Recent search context (for multi-turn disambiguation)

What we do NOT personalize:
  - Ranking based on browsing history (too privacy-invasive)
  - Filter bubble effects (showing only confirming results)
  - Personalized AI answers (same evidence → same answer for everyone)

Implementation:
  - Personalization signals are ranking FEATURES in Stage 2, not
    hard filters. They adjust scores by ~5-10%, never dominate.
  - All personalization data is stored client-side or in an ephemeral
    session. No persistent user profiles in the search backend.
  - Query logs are pseudonymized within 24 hours per GDPR/CCPA.
  - LLM prompts never contain user PII — only the query and
    retrieved passages.

Why not deeper personalization:
  1. Privacy regulations make it increasingly costly and risky
  2. Filter bubbles degrade search quality for informational queries
  3. Personalization adds latency (profile lookup) and complexity
  4. At the 80/20 level, language + location capture most of the
     value of personalization with minimal privacy cost
```

### Self-Hosted vs. API-Based LLM

```
At 1B AI answers/day, self-hosting dominates:

                   Self-Hosted (70B, 8-bit)     API-Based
──────────────────────────────────────────────────────────────
Cost/answer        ~$0.0012                     ~$0.003-0.01
Daily cost (1B)    $1.2M                        $3-10M
Latency control    Full (tune batch size,       Limited (provider
                   speculative decode, etc.)    controls infra)
Data privacy       Full (queries stay           Query text sent to
                   on-premise)                  third party
Availability       You own it (upside           Provider SLA
                   and downside)                (99.9% typical)
Model flexibility  Pin exact version,           Provider may
                   A/B test variants            update/deprecate
Operational cost   ~50 ML infra engineers       Near zero
──────────────────────────────────────────────────────────────

Decision: self-hosted primary, API-based overflow.
  - Self-hosted fleet handles 100% of normal load
  - API-based provider used for:
    1. Burst overflow (during peak events, spin up API usage)
    2. Fallback when self-hosted fleet is degraded
    3. A/B testing new model versions before self-hosting them
  - Crossover point: self-hosting becomes cheaper at ~10M AI answers/day
    (below that, the fixed cost of GPU fleet + ML ops team exceeds API costs)
```

### Index Partitioning: Document-Partitioned vs. Term-Partitioned

```
Decision: document-partitioned index.

Document-partitioned (chosen):
  - Each shard holds a complete index for a subset of documents
  - A query fans out to all (or many) shards in parallel
  - Each shard returns its top-k independently
  - Pro: each shard is self-contained, easy to add/remove shards,
         straightforward replication
  - Con: high fan-out per query (50+ shards queried)

Term-partitioned (rejected for web scale):
  - Each shard holds the complete posting list for a subset of terms
  - A query only hits shards containing its query terms (low fan-out)
  - Pro: fewer shards queried per query
  - Con: load imbalance (popular terms create hot shards),
         adding documents requires updating all term shards,
         multi-term queries require cross-shard joins

Why document-partitioned wins at web scale:
  - At 200B documents and 60K QPS, the fan-out to 50 shards is
    manageable (each shard handles ~1,500 QPS from 3 replicas — light)
  - Term-partitioned would have crippling hot spots on common terms
    ("the", "is", popular entity names) requiring complex load balancing
  - Document-partitioned replication is simple: replicate a shard, done.
    Term-partitioned replication needs to handle cross-shard coordination
  - All major web search engines (Google, Bing) use document-partitioned
    indexes for this reason
```

---

## Appendix: Specialized Search Verticals

### Image Search

```
Architecture:
  - CLIP embeddings (512d) for every image in the corpus
  - Combined signals: CLIP similarity + surrounding text relevance +
    alt-text match + image quality score
  - Separate image index (~20B images, IVF-PQ compressed)
  - OCR for text within images (memes, infographics, screenshots)

Query flow:
  1. Text query → CLIP text encoder → 512d vector
  2. ANN search against image CLIP index
  3. Merge with text-based image search (alt-text, surrounding text BM25)
  4. Rerank by: CLIP similarity (40%) + text relevance (30%) +
     image quality (15%) + SafeSearch compliance (15%)
```

### News Search

```
Architecture:
  - Dedicated news crawl pipeline (50K news domains, 30s polling)
  - News clustering: group articles about the same event using
    headline embedding similarity (threshold > 0.85)
  - Deduplication within clusters: select canonical source by
    publisher authority + publication time
  - Cluster headline generation: extract the most representative
    headline from the cluster
  - Timeline view: ordered events within a story cluster

Freshness:
  - Articles searchable within 1 minute (real-time index)
  - Cluster formation within 5 minutes of first article
  - Cluster updates (new articles added) within 2 minutes
```

### Video Search

```
Architecture:
  - Speech-to-text transcripts (Whisper-large) for all indexed videos
  - Key-frame extraction: 1 frame every 10 seconds, CLIP-embedded
  - Metadata: title, description, channel, view count, duration
  - Combined ranking: transcript relevance (40%) + metadata match (25%) +
    key-frame similarity (15%) + engagement (20%)
  - Timestamp-level search: link to the specific moment in the video
    where the query topic is discussed
```

---

## Appendix: Monitoring and SLOs

### Key SLOs

| SLO | Target | Measurement |
|-----|--------|-------------|
| Search availability | 99.99% | Synthetic probes from 10 global locations, 10s interval |
| AI answer availability | 99.9% | Fraction of AI-routed queries that return an answer |
| Non-AI SERP P50 latency | <= 200ms | Server-side measurement, edge to response |
| Non-AI SERP P99 latency | <= 500ms | Server-side measurement |
| AI TTFT P50 | <= 800ms | Time from query to first streamed token |
| AI TTFT P99 | <= 2,000ms | Time from query to first streamed token |
| AI faithfulness | >= 95% | Automated grounding check on sampled answers |
| NDCG@10 | >= 0.75 | Hourly golden-set evaluation |
| Freshness (news) | < 1 minute | Probe: publish → searchable latency |
| Index freshness (main) | < 24 hours | P99 age of documents in main index |

### Alerting Hierarchy

```
P0 (page immediately):
  - Search availability < 99.9% for 5+ minutes
  - AI faithfulness < 85% on hourly sample
  - NDCG@10 drops > 10% relative to 24h baseline
  - Real-time index freshness > 5 minutes

P1 (page within 30 minutes):
  - Search P99 latency > 800ms for 15+ minutes
  - AI TTFT P99 > 4s for 15+ minutes
  - LLM fleet utilization > 85% sustained
  - Answer cache hit rate drops > 20% from baseline

P2 (ticket, next business day):
  - Crawl rate below 80% of daily target
  - Index shard replica count below target for > 1 hour
  - Stage 2 model NDCG regression > 2% on golden set
  - Anti-abuse: anomalous spike in low-quality indexed content
```
