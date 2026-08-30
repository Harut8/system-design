# Enterprise RAG Platform: Design Document

> Solution to [`tasks/rag-platform.md`](../tasks/rag-platform.md).

### Prerequisites and Learning Resources

This solution is best studied alongside the curriculum chapters that explain the theory behind each component. Read the chapter first, then the corresponding section here to see how the theory becomes a production system.

| Section in this doc | Curriculum chapter | What it covers |
|--------------------|--------------------|----------------|
| §6 Chunking Engine | [`ai-rag/02-chunking-and-document-processing.md`](../ai-rag/02-chunking-and-document-processing.md) | All chunking strategies in depth: fixed-size, semantic, recursive, document-structure-aware, parent-child, overlap tuning |
| §7 Embedding Service | [`ai-rag/01-embeddings-and-representation.md`](../ai-rag/01-embeddings-and-representation.md) | Embedding models, vector spaces, similarity metrics, dimensionality trade-offs, matryoshka embeddings |
| §8 Vector Store | [`ai-rag/03-indexing-and-vector-stores.md`](../ai-rag/03-indexing-and-vector-stores.md) | HNSW vs IVF, quantization (SQ/PQ), sharding, filtered search, benchmarking methodology |
| §9–10 Retrieval + Reranking | [`ai-rag/04-retrieval-hybrid-and-reranking.md`](../ai-rag/04-retrieval-hybrid-and-reranking.md) | Dense + sparse hybrid search, BM25/SPLADE, cross-encoder reranking, RRF, MMR |
| §13 Evaluation | [`ai-rag/08-evaluation-methodology.md`](../ai-rag/08-evaluation-methodology.md) | Recall@k, MRR, NDCG, faithfulness, LLM-as-judge, golden dataset design |
| §5 Ingestion Pipeline | [`ai-rag/appendix-d-doc-processing-benchmarks.md`](../ai-rag/appendix-d-doc-processing-benchmarks.md) | Parser benchmarks (Tika, Unstructured, Docling), extraction quality by format |
| §3 Architecture | [`ai-rag/00-mental-models.md`](../ai-rag/00-mental-models.md) | The represent→retrieve→generate mental model that shapes the entire platform |
| §15 Deployment | [`ai-rag/appendix-e-deployment-and-compute.md`](../ai-rag/appendix-e-deployment-and-compute.md) | GPU provisioning for embedding models, inference optimization, cost modeling |

---

## Table of Contents

1. [Requirements Clarification](#1-requirements-clarification)
2. [Capacity Estimates](#2-capacity-estimates)
3. [High-Level Architecture](#3-high-level-architecture)
4. [Data Source Connectors](#4-data-source-connectors)
5. [Ingestion Pipeline](#5-ingestion-pipeline)
6. [Chunking Engine](#6-chunking-engine)
7. [Embedding Service](#7-embedding-service)
8. [Vector Store](#8-vector-store)
9. [Retrieval Service](#9-retrieval-service)
10. [Reranking](#10-reranking)
11. [Permission-Aware Retrieval](#11-permission-aware-retrieval)
12. [Freshness Management](#12-freshness-management)
13. [Evaluation](#13-evaluation)
14. [Data Models](#14-data-models)
15. [Sequence Flows](#15-sequence-flows)
16. [API Design](#16-api-design)
17. [Observability and SLOs](#17-observability-and-slos)
18. [Scaling](#18-scaling)
19. [Failure Modes](#19-failure-modes)
20. [Cost Model](#20-cost-model)
21. [Trade-offs](#21-trade-offs)
22. [Evolution Path](#22-evolution-path)
23. [Exercises](#23-exercises)

---

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|---|---|---|
| Scope | Does the platform own the LLM generation step? | No. The platform's contract ends at a ranked, permission-filtered, cited set of chunks. Generation/orchestration is the application team's concern, but the platform still owns *evaluating* faithfulness of downstream answers because that signal feeds retrieval quality decisions. |
| Tenancy | Who owns a knowledge base? | One team per knowledge base (namespace), but a knowledge base can span many sources and serve many downstream applications/service identities. |
| Permissions | Whose permissions apply at query time? | The permissions of the **calling end user**, passed as a resolved identity + group set by the caller (application backend), never the service account used to ingest. |
| Freshness | Is "freshness" the same target for every source? | No — per-source-class SLA (Slack: minutes; wiki: hours; nightly DB export: 24h) declared in §12. |
| Consistency | Is the vector index linearizable with the source? | No — it is deliberately **eventually consistent for content**, but has a hard, bounded consistency requirement for **permission revocation** (§11). |
| Scale | Largest single knowledge base? | 2B chunks, ~2 TB of float16 vectors at 768d before quantization. |
| Embedding | One embedding model platform-wide? | No — multiple models coexist; each knowledge base pins one "primary" model version at a time, with a migration protocol (§7.4) for changing it. |
| Chunking | Is chunking strategy global or configurable? | Configurable per source within a knowledge base — a Confluence source and a Slack source in the same KB legitimately want different strategies. |
| Evaluation | Who writes golden datasets? | The owning team, platform tooling assists by mining query logs + click/thumbs feedback into candidate judgments for human review. |
| Ops | Team size operating this platform? | ~15-20 engineers across connector, retrieval/index, and ML/eval sub-teams — this shapes how much must be config-driven vs. hand-built per source. |

### Key Assumptions

1. **Read-heavy, write-heavy-in-bursts.** Query traffic is roughly steady; ingestion traffic is bursty (new KB backfills, bulk re-embeds) and must not starve query serving — these are architecturally separated (§3, §18).
2. **Permissions are the hardest constraint, not the biggest one.** Scale is "big but standard" vector-search scale; the differentiator versus a generic vector DB is that a permission mistake is a security incident, not a quality regression.
3. **Source systems are authoritative.** The platform never originates content or permission truth; it is always a derived, rebuildable index. This makes "delete the knowledge base and re-backfill" always a valid (if slow) recovery path.
4. **Most knowledge bases are small; a few are huge.** Power-law distribution — 90% of KBs are under 1M chunks, but the top 1% (5 KBs) hold 40%+ of total chunks. The architecture must not force every KB to pay large-KB overhead (§8.5, §18).
5. **Embedding models improve over time and must be swappable** without a flag day for every downstream application.
6. **Not every source offers clean incremental primitives.** Some connectors are stuck polling and diffing; the freshness SLA for those sources is honestly worse, not force-fit to match webhook-backed sources.

### What We Are Explicitly *Not* Promising

- Not strict/linearizable consistency between a source edit and its retrievability — bounded staleness per source class instead.
- Not cross-knowledge-base search by default — each retrieval call is scoped to one (or an explicitly authorized federated set of) knowledge base(s); there is no implicit "search everything" that would make permission reasoning intractable.
- Not synchronous generation — the platform returns chunks, not answers, though it exposes an eval pipeline that scores answers applications generate.
- Not full Byzantine-fault tolerance on connectors — a compromised/misconfigured connector can ingest garbage into its own KB; blast radius is contained to that KB, not treated as an adversarial-input problem platform-wide.

---

## 2. Capacity Estimates

Doing the arithmetic up front constrains every later design choice.

### Storage

| Quantity | Value |
|---|---|
| Total chunks (steady state, 2yr horizon) | 10,000,000,000 (10B), growing toward tens of billions |
| Avg chunk text size | ~1,000 bytes (≈250 tokens) |
| Raw chunk text storage | 10B × 1 KB = **10 TB** |
| Embedding dim (primary model) | 768 (float32 = 3072 B, float16 = 1536 B) |
| Raw vector storage (float32, unquantized) | 10B × 3072 B = **30.7 TB** |
| Raw vector storage (float16) | 10B × 1536 B = **15.4 TB** |
| With int8 scalar quantization (1 B/dim + overhead) | 10B × ~900 B ≈ **9 TB** |
| Metadata per chunk (doc id, ACL refs, timestamps, source, ~500 B avg) | 10B × 500 B = **5 TB** |
| **Total logical footprint (quantized, single replica)** | ≈ **24-30 TB** |
| With 3x replication for availability | **~75-90 TB** |
| With HNSW graph overhead (~1.5-2x raw vectors for graph edges) | add ~15-20 TB on top of vector storage |

### Ingestion throughput

| Quantity | Value |
|---|---|
| Steady-state ingest | 50M chunks/day = 578 chunks/sec average, design for 5-10x burst = **3,000-6,000 chunks/sec peak** |
| Large KB backfill target | 10M docs × ~20 chunks/doc = 200M chunks in 48h = **1,157 chunks/sec** sustained for that one job alone |
| Embedding calls (batched, 100 chunks/batch) | 6,000 chunks/sec ÷ 100 = 60 batch calls/sec at peak |
| Embedding compute (self-hosted, e.g. bge-base on A10G, ~1,500 chunks/sec/GPU batched) | ~6,000/1,500 ≈ **4 GPUs** at peak for one model version; provision ~8-12 for concurrent backfills + steady state + headroom |

### Query throughput

| Quantity | Value |
|---|---|
| Platform peak QPS | 20,000 qps |
| Largest single KB peak | 2,000 qps |
| Vector search cost per query (HNSW, ef_search=100, top_k=50, single shard) | ~2-5 ms CPU |
| With reranking (cross-encoder, top-100 candidates) | +100-400 ms depending on batch size/GPU vs CPU (§10.4) — this is why reranking is opt-in per call |
| Fraction of queries expected to use reranking | ~30% (quality-sensitive interactive apps); rest use raw hybrid search |

### Shard sizing (HNSW)

A single HNSW index holding 100M vectors at 768d float16 needs roughly:
`100M × 1536 B (vectors) × 1.5 (graph overhead) ≈ 230 GB` — too large for one node's RAM budget alongside OS/other services (target ≤ 64 GB usable per shard for headroom and query concurrency).

`64 GB ÷ (1536 B × 1.5) ≈ 27.7M vectors per shard` → round down to **25M vectors/shard** as the sharding target.

10B chunks ÷ 25M/shard = **400 shards** platform-wide at steady state, growing with corpus size. This number recurs in §8.5 (sharding strategy) and §18 (scaling).

### Query fan-out bandwidth

For the largest sharded KBs (§8.5), a single query fans out to every shard, each returning an overfetched candidate list (§9.3) — worth sizing so the fan-out itself isn't a hidden bottleneck:

```
Largest KB: 2B chunks ÷ 25M/shard = 80 shards
Per-shard overfetch: top_k=10 requested → top-30 returned per shard (3x overfetch)
Per-result payload: ~1.2 KB (chunk text + metadata + scores, before rerank
                             truncates it down to the final top_k)
Per-query fan-out traffic: 80 shards × 30 results × 1.2 KB ≈ 2.9 MB per query

At this KB's peak of 2,000 qps (§2): 2,000 × 2.9 MB ≈ 5.8 GB/sec of
internal fan-out traffic — comfortably within a modern data-center
network fabric's budget, but large enough that the Retrieval Service
instances handling this KB's traffic are deliberately placed
network-adjacent to that KB's shard fleet (§18's "colocate with vector
store shards to minimize network hops"), rather than assuming cross-AZ
bandwidth is free at this volume.
```

The 3x overfetch factor is the direct multiplier on this number — it's the same knob that improves cross-shard result quality (§9.3) and inflates fan-out bandwidth, one more concrete instance of §21's recurring theme that a quality lever and a cost/capacity lever are often the same knob viewed from two directions.

---

## 3. High-Level Architecture

```
                                   ┌─────────────────────────────┐
                                   │      Control Plane          │
                                   │  KB Registry / Connector     │
                                   │  Config / Schema / IAM       │
                                   └──────────────┬───────────────┘
                                                  │ config, credentials
        ┌─────────────────────────────────────────┼───────────────────────────────────┐
        │                         DATA / INGESTION PLANE                              │
        │                                                                             │
        │  ┌────────────┐   ┌──────────────┐   ┌───────────────┐   ┌───────────────┐  │
        │  │ Connector  │──▶│  Ingestion    │──▶│   Chunking    │──▶│   Embedding   │  │
        │  │  Service   │   │  Pipeline     │   │    Engine     │   │    Service    │  │
        │  │ (per-src)  │   │ (parse, dedup,│   │ (strategy per │   │ (batch infer, │  │
        │  │            │   │  metadata)    │   │  source/type) │   │  multi-model) │  │
        │  └─────┬──────┘   └──────┬───────┘   └───────┬───────┘   └───────┬───────┘  │
        │        │                 │                    │                   │          │
        │        ▼                 ▼                    ▼                   ▼          │
        │  ┌────────────┐   ┌──────────────┐    (DLQ for failures at every stage)      │
        │  │ Permission │   │   Document    │                                          │
        │  │   Sync     │   │    Store      │──────────────────────┐                   │
        │  │ (ACL index)│   │ (raw + parsed)│                      │                   │
        │  └─────┬──────┘   └───────────────┘                      ▼                   │
        │        │                                          ┌──────────────┐            │
        │        │                                          │ Vector Store │            │
        │        │                                          │ (dense+BM25, │            │
        │        │                                          │  sharded)    │            │
        │        │                                          └──────┬───────┘            │
        └────────┼──────────────────────────────────────────────────┼──────────────────┘
                 │                                                  │
                 ▼                                                  ▼
        ┌─────────────────┐  filter   ┌────────────────────────────────────┐
        │  Permission      │◀─────────│         Retrieval Service          │
        │  Service (cache) │──────────▶│ query expansion → hybrid search →  │
        └─────────────────┘  allow-set│  merge → permission filter → rank  │
                                       └───────────────┬────────────────────┘
                                                        │
                                                        ▼
                                              ┌───────────────────┐
                                              │  Reranking Service │
                                              │ (cross-encoder,    │
                                              │  RRF, MMR)         │
                                              └─────────┬──────────┘
                                                        │
                                                        ▼
                                              ┌───────────────────┐
                                              │  Retrieval API     │──▶ Application teams
                                              └───────────────────┘

        ┌─────────────────────────────────────────────────────────────────┐
        │                        Evaluation Plane                          │
        │  Golden Dataset Store ── Eval Runner ── LLM-as-Judge ── Reports  │
        │  (reads from Vector Store + Retrieval API, writes to KB Registry) │
        └─────────────────────────────────────────────────────────────────┘

        ┌─────────────────────────────────────────────────────────────────┐
        │                        Freshness Manager                         │
        │  Sync Scheduler ── Staleness Tracker ── Tombstone Propagator      │
        │  (drives Connector Service cadence, feeds retrieval freshness    │
        │   scoring, reconciles against Permission Service)                │
        └─────────────────────────────────────────────────────────────────┘
```

### Component responsibilities

| Component | Responsibility | Scaling axis |
|---|---|---|
| Control Plane | KB CRUD, connector config, credential vault, schema registry, tenant quotas | Low QPS, strong consistency, small data — a classic CRUD service over Postgres |
| Connector Service | Per-source-type workers that pull/receive changes, normalize to a common `RawDocument` envelope | Horizontal by source-type + KB shard; rate-limited per source API |
| Ingestion Pipeline | Parse, extract metadata, detect language, dedup, write to Document Store | Horizontal, stateless workers behind a durable queue |
| Chunking Engine | Apply configured strategy, emit `Chunk` records with lineage metadata | Horizontal, stateless, CPU-bound (semantic chunking is GPU-assisted) |
| Embedding Service | Batch-embed chunks, manage model versions, re-embed on migration | Horizontal by GPU pool; queue-depth autoscaled |
| Permission Sync | Pull ACLs per source, normalize into a permission graph, propagate tombstones | Per-source, event-driven where possible |
| Vector Store | Store + index chunk vectors and sparse postings, serve ANN + BM25 + filtered search | Sharded by KB, replicated |
| Retrieval Service | Orchestrate query pipeline: expansion → search → merge → permission filter → return | Stateless, horizontal, colocated with vector store shards for fan-out |
| Reranking Service | Cross-encoder scoring, RRF, MMR | GPU pool, horizontal, latency-budgeted |
| Permission Service | Resolve principal → allow-set, cache group expansions | Horizontal, heavily cached, low-latency reads |
| Freshness Manager | Schedule syncs, track staleness, propagate tombstones, drive reconciliation | Control-plane-adjacent, low QPS but must be reliable |
| Evaluation Plane | Golden datasets, offline/online eval runs, LLM-as-judge, regression reports | Batch-oriented, off critical path |

### Why this shape

- **Ingestion and query serving are fully decoupled** (separate services, separate infra, only meeting at the Vector Store and Permission Service). A backfill storm cannot starve query latency because it competes for different resource pools; the only shared resource is the vector store's write path, which is rate-limited per KB (§18.4).
- **The Permission Service sits on both write and read paths.** It is consulted at index build time (to attach ACL references to chunks) and, critically, at query time (to compute the live allow-set) — never trusting a stale index-time permission alone (§11).
- **Everything downstream of the Document Store is derived and rebuildable.** Chunking strategy change, embedding model migration, and even vector-store schema change are all "replay from Document Store" operations, not special-cased migrations.

### Architecture alternatives considered

Two shape decisions are worth arguing explicitly, because the obvious-looking alternative to each is a reasonable design that a smaller-scoped project would legitimately choose — the reasons they're wrong *here* are scale- and multi-tenancy-specific, not universal.

**Alternative 1: build directly on a managed vector-database vendor (Pinecone, Weaviate Cloud, etc.) instead of an owned Vector Store service.**

| | Managed vendor | Owned Vector Store (chosen) |
|---|---|---|
| Time to first KB | Fast — days | Slower — the platform has to be built first |
| Multi-tenancy model | Vendor's namespace/index model, often coarser-grained than this design's per-KB isolation + physical-isolation-at-scale (§8.5) | Purpose-built for the platform's specific tenancy and permission-filtering requirements |
| Permission pre-filtering at ANN traversal time (§8.6, §11.3) | Vendor-dependent — not all ANN services expose a filter-aware traversal hook; some only offer post-filter, which is precisely the pathological case this design avoids | Full control — the pre-filter/bitset mechanism is a first-class design constraint, not something retrofitted onto a general-purpose vector DB's filter API |
| Cost at 10B-chunk scale | Managed per-vector pricing tends to be materially more expensive than owned infrastructure at this scale (the same "hosted vs. self-hosted" arithmetic as §7's embedding cost comparison, but for storage/query instead of embedding) | Cost model in §20 assumes owned infra specifically because the arithmetic doesn't favor a managed vendor at this scale |
| Operational burden | Low — vendor operates it | Real — needs the ~15-20 person team assumed in §1 |

**Decision: owned Vector Store**, specifically *because* the permission pre-filtering requirement (§11) is not a bolt-on feature request but the platform's core differentiator, and because the scale (§2) crosses the point where managed per-vector pricing becomes the dominant cost line. A smaller-scale internal tool serving one team, without the zero-leakage requirement, would reasonably choose a managed vendor instead — this decision is scale- and requirement-contingent, not a universal "always self-host" stance.

**Alternative 2: one shared vector index for the whole platform (single large multi-tenant index with a `kb_id` metadata filter) instead of per-KB sharding.**

This looks appealing at first — one index is operationally simpler than hundreds. It fails for two independent reasons:

1. **Permission filtering composes badly with tenant filtering.** Every query would need *both* a `kb_id` filter and a permission-allow-set filter applied simultaneously as pre-filters, and the selectivity math from §8.6's benchmark table gets worse, not better, when the base filter set (a KB's chunk range) is itself a small fraction of the whole index — the pre-filter bitset would need to be `kb_id_membership AND allow_set`, a more expensive intersection computed fresh per query rather than the current design's cheap "query only touches this KB's shard(s) at all."
2. **Noisy-neighbor isolation is structurally impossible in a shared index.** HNSW graph traversal for a query against KB A's chunks and a concurrent query against KB B's chunks would contend for the same underlying graph structure's cache locality and traversal resources — the physical/logical isolation guarantee in §8.5's multi-tenancy table simply cannot be built on top of one shared structure, no matter how the metadata filter is optimized.

**Decision: per-KB (or per-KB-shard for the largest KBs) index isolation**, accepting the operational cost of managing hundreds of index structures in exchange for permission-filtering performance and noisy-neighbor isolation that a shared index cannot provide at this platform's tenancy count and scale.

---

## 4. Data Source Connectors

### Connector framework

Every connector implements a small interface; the platform provides the plumbing (queueing, retries, credential injection, rate limiting, checkpointing) so a new source type is mostly "how do I list and fetch documents and their ACLs from here."

```python
class Connector(Protocol):
    def full_sync(self, config: SourceConfig, checkpoint: None) -> Iterator[RawDocument]:
        """Full backfill. Must be resumable via yielded checkpoints."""

    def incremental_sync(self, config: SourceConfig, checkpoint: Checkpoint) -> Iterator[RawDocument]:
        """Fetch only what changed since checkpoint. Returns new checkpoint."""

    def fetch_acl(self, config: SourceConfig, doc_ref: DocRef) -> AclEntry:
        """Fetch/refresh permissions for one document."""

    def supports_webhook(self) -> bool: ...
    def register_webhook(self, config: SourceConfig, callback_url: str) -> None: ...
```

### Incremental sync mechanism by source

| Source | Mechanism | Cadence | Notes |
|---|---|---|---|
| Confluence | Webhook (page created/updated/removed) + hourly reconciliation poll | Near-real-time + hourly | Webhooks can be dropped; reconciliation poll uses `lastModified` CQL query as backstop |
| Google Drive | Changes API (`changes.list` with `pageToken`) | Poll every 60s (cheap, delta-only) | Drive's changes API is delta-native — no full-scan needed after initial backfill |
| SharePoint | Microsoft Graph delta query (`/delta`) | Poll every 5 min | Similar delta-token model to Drive |
| Slack | Events API webhook (message, edit, delete, channel membership change) | Real-time | High-velocity source; also the tightest freshness SLA |
| S3 | S3 Event Notifications (SQS) on `ObjectCreated`/`ObjectRemoved`, fallback to `ListObjectsV2` + `LastModified` diff nightly | Real-time via events, nightly reconciliation | Event notifications can be missed during outages; nightly diff catches drift |
| Databases | Watermark polling on an `updated_at` column (CDC via Debezium where available) | 1-15 min depending on config | CDC preferred when the source DB exposes a binlog/WAL; else polling with a monotonic watermark |
| Generic REST APIs | Declarative schema (JSONPath extraction) + either a source-provided delta parameter or full-poll with response diffing | Configurable, default 15 min | Weakest guarantees — documented as such to the owning team |
| Web crawler | Scheduled re-crawl + sitemap `lastmod` where available, content-hash diffing otherwise | Configurable, default daily | Respects `robots.txt`, crawl budget per domain, politeness delay |

### Change token / checkpoint protocol

```
Checkpoint {
  source_id: str
  cursor: bytes          # opaque, source-specific (page token, delta link, watermark ts, etc.)
  last_success_at: datetime
  consecutive_failures: int
}
```

Checkpoints are persisted **after** the corresponding batch is durably enqueued into the ingestion pipeline (not after processing completes) — this makes sync resumable and idempotent: a crash between "enqueue" and "checkpoint write" simply re-fetches and re-enqueues a batch the ingestion pipeline will dedupe by content hash (§5.4).

### Credential management

- OAuth tokens and API keys live in a **credential vault** (e.g., Vault/KMS-backed), referenced by the connector config as an opaque `credential_ref`, never stored inline.
- OAuth refresh handled by a shared token-refresh sidecar per source-type, so individual connector workers never handle refresh races.
- Per-source-type **service principal** used for ingestion (distinct from any end-user identity) — this is intentional: ingestion needs broad read access to enumerate ACLs, but that access must never be conflated with a *user's* access at query time (§11.1).

**Token refresh, made concrete** — the failure mode this exists to prevent is two connector workers racing to refresh the same expiring OAuth token, one succeeding and the other invalidating it with a redundant refresh call moments later, causing in-flight requests on a third worker to suddenly 401:

```python
class TokenRefreshSidecar:
    def get_valid_token(self, credential_ref: str) -> str:
        token = self.vault.read(credential_ref)
        if token.expires_at - now() > REFRESH_SKEW:   # e.g. 5 min buffer
            return token.access_token

        # Distributed lock: only one refresh happens per credential_ref,
        # regardless of how many connector workers hit this path at once
        with self.distributed_lock(f"refresh:{credential_ref}", timeout=10s):
            token = self.vault.read(credential_ref)  # re-check after lock acquired
            if token.expires_at - now() > REFRESH_SKEW:
                return token.access_token  # another worker already refreshed it
            new_token = self.oauth_client.refresh(token.refresh_token)
            self.vault.write(credential_ref, new_token)
            return new_token.access_token
```

The re-check immediately after acquiring the lock is the detail that actually prevents the double-refresh race — without it, every worker that queued behind the lock would still perform a redundant refresh call once it's their turn, some of which OAuth providers treat as invalidating the just-issued token (refresh-token rotation).

### Rate limiting against source APIs

Each connector worker pool is rate-limited to the source's documented API budget (e.g., Confluence Cloud: 1000 req/hour/app by default tier; Slack Web API: tiered per-method limits). Rate limiting is implemented as a **token bucket per (source instance, API method)**, shared across all workers for that source via a distributed limiter (Redis-backed), so parallelizing a backfill across many workers cannot accidentally violate the source's limit and get the whole org's connector throttled or banned.

### Connector lifecycle & error handling

```
        register ──▶ PENDING_AUTH ──▶ AUTHORIZED ──▶ BACKFILLING ──▶ ACTIVE
                                            │                            │
                                            │                    ┌───────┴────────┐
                                            │                    ▼                ▼
                                            │              DEGRADED         PAUSED (quota/
                                            │          (elevated error       admin action)
                                            │            rate, still            │
                                            │             syncing)              ▼
                                            └──────────────────────────────▶ DISABLED
```

- **Partial batch failure isolation**: one malformed document (corrupt PDF, oversized payload) is routed to a per-source **dead letter queue** with the error, and does not fail the rest of the batch. A DLQ item is retried with backoff up to N times, then surfaced to the owning team's dashboard for manual triage (§5.5).
- **DEGRADED** state: entered automatically when error rate over a rolling window exceeds a threshold (e.g., >10% of fetches failing); still syncs at reduced concurrency, alerts the platform on-call, and — if the errors look like a source-side outage rather than a config issue — backs off exponentially rather than hammering a struggling upstream.
- A connector that stays in DEGRADED beyond its source's freshness SLA window automatically raises staleness alerts (§12.2), separate from the connector's own health alerts, because "the connector is technically running but too slow" and "the connector is down" have different operator responses.

**Schema drift handling.** Generic REST API and database connectors (§4's weakest-guarantee sources) are the most exposed to the source silently changing its response/table shape — a field renamed, a type changed from string to object, a column dropped. The connector framework treats this as a distinct failure class from a transient fetch error:

```python
def parse_with_drift_detection(raw_response: dict, expected_schema: JSONSchema) -> ParsedDoc:
    validation_errors = expected_schema.validate(raw_response)
    if not validation_errors:
        return parse(raw_response)

    if is_additive_only(validation_errors):
        # new, unexpected fields present but all REQUIRED fields still
        # there and correctly typed — safe to proceed, log for visibility
        log_schema_drift(severity="info", errors=validation_errors)
        return parse(raw_response, ignore_unknown_fields=True)

    # a required field is missing, or a known field's type changed —
    # NOT safe to guess; route to DLQ rather than parse something wrong
    # and silently ingest corrupted/misleading content
    raise SchemaDriftError(validation_errors, severity="blocking")
```

A **blocking** schema-drift error pauses that specific source (not the whole connector fleet) and pages the owning team rather than the DLQ's default best-effort retry — retrying a fetch against a source whose shape genuinely changed will just fail identically every time, so the standard exponential-backoff DLQ handling (appropriate for transient errors) is explicitly bypassed in favor of an immediate, specific alert: "this source's config likely needs an update," not "this document failed, will retry."

### Per-connector configuration examples

Concrete `SourceConfig` payloads for four representative connectors, showing how the same declarative shape (§4.1) accommodates very different sources:

```yaml
# Confluence — space-scoped, webhook + reconciliation
source_type: confluence
scope:
  spaces: ["ENG", "SUPPORT"]
  include_archived: false
credentials_ref: vault://connectors/confluence/eng-wiki-sa
parsing_hints:
  chunking_strategy: structure_aware
  strip_macros: ["toc", "expand"]      # Confluence-specific markup to discard
sync_policy:
  mechanism: webhook
  reconciliation_cadence: hourly

# Slack — channel allowlist, message + thread capture
source_type: slack
scope:
  channels: ["#eng-incidents", "#support-escalations"]
  include_threads: true
  include_dm: false                    # DMs excluded from enterprise KBs by default
credentials_ref: vault://connectors/slack/eng-wiki-bot
parsing_hints:
  chunking_strategy: parent_child      # message = child, thread = parent (§6.5)
sync_policy:
  mechanism: webhook
  reconciliation_cadence: daily

# Relational database — watermark polling, declarative extraction
source_type: database
scope:
  connection_ref: vault://connectors/db/support-tickets-ro
  query: |
    SELECT id, subject, body, status, updated_at, assignee_email
    FROM tickets
    WHERE updated_at > :watermark
    ORDER BY updated_at ASC
  watermark_column: updated_at
  primary_key: id
  acl_mapping:
    # row-level grants derived from a join, not the base table
    query: |
      SELECT ticket_id, team_email AS principal_id, 'group' AS principal_type
      FROM ticket_team_access WHERE ticket_id = :id
parsing_hints:
  chunking_strategy: fixed_unit        # one row = one chunk, no splitting
sync_policy:
  mechanism: watermark_poll
  cadence: 5m

# Web crawler — domain-scoped, sitemap-assisted
source_type: web
scope:
  seed_urls: ["https://internal-docs.company.com/"]
  domain_allowlist: ["internal-docs.company.com"]
  max_depth: 6
  respect_robots_txt: true
  crawl_budget_per_day: 50000          # hard cap, prevents a runaway crawl (§17)
credentials_ref: null                   # internal docs site, network-ACL gated instead
parsing_hints:
  chunking_strategy: structure_aware
sync_policy:
  mechanism: sitemap_diff
  cadence: daily
```

Two things worth calling out from these examples: (1) the **database connector's `acl_mapping`** is a second declarative query, not a hardcoded assumption — row-level permissions in enterprise databases are almost always modeled via a join table, and forcing that mapping to be explicit at config time is what lets the platform apply the exact same permission-sync machinery (§11) to a database source as to a Confluence space; (2) the **web crawler's `crawl_budget_per_day`** is a config-level guardrail, not just a runtime safety net — most "runaway crawl" incidents are actually a misconfigured `domain_allowlist` or `max_depth`, and a hard budget turns a potential incident into a "sync stopped early, check the config" dashboard entry instead.

### Connector-specific quirks worth designing around

| Source | Quirk | Design response |
|---|---|---|
| Confluence | Page permissions can be **space-level defaults overridden per-page**, and the override can be *more* or *less* restrictive | ACL sync always resolves the effective (not just declared) permission via the API's own permission-check endpoint, never inferred client-side |
| Google Drive | A file can be shared via **link** ("anyone with the link") which has no enumerable principal list | Modeled as a distinct `principal_type: link_share` grant; whether link-shared content is ingestible at all is a KB-level policy toggle (default: excluded, since "anyone with the link" is not a verifiable identity the platform can re-check at query time) |
| Slack | **Channel membership changes constantly**, and a message's visibility follows *current* channel membership, not membership at post time | Slack messages are treated as permission-dynamic — the ACL is "current channel members," re-resolved at query time via the same group-expansion path as any other group (§11.3), not snapshotted at ingest time |
| SharePoint | Delta query tokens **expire** after a source-side retention window (commonly 30 days); an expired token forces a full re-sync | Checkpoint staleness is monitored explicitly; a connector idle long enough to risk token expiry (e.g., paused for 3+ weeks) proactively triggers a full re-sync rather than waiting to discover the token is dead |
| Databases | No universal "what changed" primitive — some tables have no `updated_at`, some have soft-deletes with no delete timestamp | Documented per-table capability tiers (full CDC / watermark column / append-only / full-scan-only) with the freshness SLA explicitly downgraded for the weakest tier, communicated to the owning team at connector setup time rather than discovered later |
| Web crawl | Content-hash diffing produces false "changed" positives on pages with embedded timestamps/ads/A-B test markup | Boilerplate-aware hashing (strip common noise patterns before hashing) reduces but doesn't eliminate this; crawl-diff false-positive rate is tracked as its own metric because it directly inflates re-embedding cost (§20) |

---

## 5. Ingestion Pipeline

### Pipeline orchestration

Built on a durable, replayable pipeline (Temporal-style workflow engine) rather than a plain queue-of-tasks, because ingestion is multi-step with partial failure and needs per-document retry state, not just per-batch:

```
RawDocument (from connector)
      │
      ▼
┌─────────────┐     ┌────────────┐     ┌──────────────┐     ┌────────────┐
│   Parse      │────▶│  Metadata   │────▶│  Language     │────▶│  Dedup      │
│ (format-      │     │  Extraction │     │  Detection    │     │ (hash +     │
│  specific)    │     │             │     │               │     │  fuzzy)     │
└─────────────┘     └────────────┘     └──────────────┘     └──────┬─────┘
                                                                    │ not a dup
                                                                    ▼
                                                          ┌───────────────────┐
                                                          │  Document Store     │
                                                          │  write (versioned)  │
                                                          └─────────┬──────────┘
                                                                    │ emits event
                                                                    ▼
                                                          Chunking Engine (§6)
```

Each step is a Temporal **activity** with its own retry policy; the workflow (one per document, or one per small batch for very high-volume sources like Slack) is the unit of idempotency — replaying a workflow for the same `(source_id, doc_id, content_hash)` is a no-op if that hash was already fully processed.

```python
@workflow.defn
class IngestDocumentWorkflow:
    @workflow.run
    async def run(self, raw_doc: RawDocument) -> IngestResult:
        workflow_id = f"{raw_doc.source_id}:{raw_doc.doc_id}:{raw_doc.content_hash}"
        # Temporal dedupes on workflow_id automatically — a redelivered
        # RawDocument with an identical content_hash starts a workflow
        # that immediately completes as a no-op rather than reprocessing

        parsed = await workflow.execute_activity(
            parse_document, raw_doc,
            retry_policy=RetryPolicy(max_attempts=3, backoff=exponential),
            start_to_close_timeout=timedelta(minutes=5),
        )
        metadata = await workflow.execute_activity(extract_metadata, parsed)
        language = await workflow.execute_activity(detect_language, parsed)

        dup_result = await workflow.execute_activity(check_dedup, parsed, metadata)
        if dup_result.is_duplicate:
            return IngestResult(status="deduped", canonical_id=dup_result.canonical_id)

        doc = await workflow.execute_activity(
            write_document_store, parsed, metadata, language,
        )
        await workflow.execute_activity(emit_document_updated_event, doc.id)
        return IngestResult(status="ingested", document_id=doc.id)
```

The `retry_policy` is scoped **per activity**, not per workflow — a transient failure in `parse_document` (e.g., a momentary OCR-service timeout) retries just that step, while a hard failure after exhausting retries fails the workflow and routes to the DLQ (§5.5) with the specific activity and error preserved, rather than restarting the whole document from scratch or losing which step actually failed.

### Document parsing

| Format | Parser | Notes |
|---|---|---|
| PDF | Unstructured.io / Apache Tika, with OCR fallback (Tesseract) for scanned pages | Preserve page numbers and, where possible, layout (tables, columns) for structure-aware chunking |
| DOCX / PPTX | python-docx / python-pptx via Tika | Extract embedded images' alt text/captions as supplementary text, not the images themselves in v1 |
| HTML | Readability-style boilerplate stripping + structural parse (headings, lists, tables) | Critical for web crawler and Confluence-exported-as-HTML sources |
| Markdown | Native AST parse (preserves heading hierarchy directly — best case for structure-aware chunking) | |
| Code | Tree-sitter, per-language grammars (40+ languages) | Chunk boundaries respect function/class boundaries (§6.4) |
| CSV/TSV | Row-to-document or row-to-chunk mapping, schema-aware | Large CSVs are *not* embedded row-by-row as prose; see §6 for tabular strategy |
| JSON/API payloads | Declarative field mapping (JSONPath) configured per REST connector | Non-text fields become metadata, not chunk text |

Parsing runs in a resource-isolated worker pool (separate from chunking/embedding) because PDF/OCR parsing is CPU-heavy and unpredictable in duration — isolating it prevents one giant PDF from head-of-line-blocking lightweight Markdown parsing.

### Metadata extraction

Extracted into a normalized `DocumentMetadata` envelope (§14) regardless of source:

```json
{
  "title": "Q3 Incident Postmortem: Payments Outage",
  "author": "jdoe@company.com",
  "created_at": "2024-03-01T10:00:00Z",
  "modified_at": "2024-08-12T14:32:00Z",
  "source_url": "https://company.atlassian.net/wiki/spaces/ENG/pages/12345",
  "source_type": "confluence",
  "content_type": "postmortem",
  "language": "en",
  "labels": ["incident", "payments", "q3-2024"],
  "source_acl_ref": "confluence:space:ENG:page:12345"
}
```

`content_type` classification uses a lightweight heuristic + ML classifier (title patterns, source folder conventions, and — for ambiguous cases — a small fine-tuned classifier) so downstream retrieval filters ("only search postmortems") work without every team hand-tagging documents.

### Language detection

Run per document (fastText lid.176 or similar, sub-millisecond) and, for documents mixing languages (common in international orgs), re-run per chunk after chunking — a chunk's `language` field can differ from its parent document's dominant language. This becomes a retrieval filter and also routes to language-appropriate embedding models where the platform offers multilingual vs. English-optimized variants.

### Deduplication

Two-tier:

1. **Exact duplicate**: SHA-256 of normalized (whitespace-collapsed, boilerplate-stripped) content. O(1) lookup against a content-hash index scoped to the knowledge base. Exact dupes are recorded as an alias to the canonical document (first-seen or highest-authority-source wins, configurable) rather than re-ingested.
2. **Near-duplicate**: SimHash (64-bit) computed per document, indexed in a locality-sensitive structure (banded LSH) for approximate lookup with Hamming-distance threshold (e.g., ≤3 bits ≈ >95% similar). Near-dupes across sources (a PDF policy doc that's *also* pasted into a Confluence page) are flagged, both are kept (their permissions may legitimately differ!), but retrieval-time diversity reranking (§10.3, MMR) suppresses returning both in the same result set.

Canonical-copy policy is configurable per knowledge base (e.g., "prefer Confluence over Drive" for a wiki-first org), defaulting to most-recently-modified.

### Dead letter queue

Failures at any pipeline stage (unparseable file, extraction timeout, embedding API error) land in a per-KB DLQ with:

```json
{
  "doc_ref": "s3://bucket/key.pdf",
  "stage": "parse",
  "error": "PDFSyntaxError: invalid xref table",
  "attempt": 3,
  "first_failed_at": "...",
  "last_failed_at": "..."
}
```

Retried with exponential backoff (up to 5 attempts over 24h, to ride out transient issues), then surfaces in the KB's ingestion-health dashboard as an actionable item, and is excluded from staleness alerting math (a permanently-broken PDF shouldn't hold a whole source's freshness score down — it's tracked as its own failure signal instead).

---

## 6. Chunking Engine

### Strategy selection heuristics

The chunking engine doesn't pick one universal strategy; the ingestion pipeline attaches a `content_type` + `format` to every document (§5), and a per-KB (overridable per-source) **strategy policy** maps that to a chunker:

| Content signal | Default strategy | Why |
|---|---|---|
| Markdown/HTML with clear heading hierarchy | Document-structure-aware | Headings are free, high-quality semantic boundaries |
| Long prose (postmortems, policy docs, PDFs) | Recursive splitting, target 400 tokens, 15% overlap | Cheap, robust default for unstructured prose |
| Highly technical/dense prose (legal, compliance) | Semantic chunking | Boundary quality matters more than cost here; lower query volume tolerates higher ingest cost |
| Source code | Structure-aware (AST/tree-sitter: function/class boundaries) | Never split a function mid-body; a chunk should be a coherent unit |
| Slack threads | Parent-child: message-level child chunks, thread-level parent | Precise retrieval of "who said X" with full-thread context for generation |
| Tables / CSV rows | Row-group chunking with header repeated per chunk | A chunk without its header row is meaningless |
| Chat/FAQ-style Q&A pairs | Fixed unit = one Q&A pair, no splitting | The natural unit already matches retrieval granularity |

### Recursive character/token splitting

The workhorse fallback. Splits on a priority-ordered list of separators, backing off to a smaller separator only if the chunk still exceeds the token budget:

```python
SEPARATORS = ["\n\n", "\n", ". ", " "]  # paragraph → line → sentence → word

def recursive_split(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    if count_tokens(text) <= max_tokens:
        return [text]
    for sep in SEPARATORS:
        if sep in text:
            parts = text.split(sep)
            return _merge_to_budget(parts, sep, max_tokens, overlap_tokens)
    # No separator worked (e.g. one giant token-dense line) — hard character split
    return _hard_split(text, max_tokens, overlap_tokens)
```

Overlap is applied by carrying the trailing `overlap_tokens` of chunk *N* into the start of chunk *N+1* — this is the single biggest lever against "the answer spans a chunk boundary and neither half retrieves well" (§21.1).

### Semantic chunking

Detects topic-boundary discontinuities using sentence-level embedding similarity rather than a fixed token count:

```python
def semantic_chunk(sentences: list[str], embed_fn, threshold_percentile=95, max_tokens=600):
    embeddings = embed_fn(sentences)                      # cheap small embedding model
    sims = [cosine(embeddings[i], embeddings[i+1]) for i in range(len(sentences)-1)]
    breakpoint_threshold = percentile(sims, 100 - threshold_percentile)  # low-similarity = boundary
    chunks, current = [], [sentences[0]]
    for i, sim in enumerate(sims):
        if sim < breakpoint_threshold or count_tokens(current) >= max_tokens:
            chunks.append(" ".join(current))
            current = []
        current.append(sentences[i+1])
    chunks.append(" ".join(current))
    return chunks
```

This costs an extra embedding pass at chunk-boundary-detection time (using a small, cheap model — not the primary retrieval embedding model), so it's reserved for content classes where boundary quality materially affects downstream answer quality and ingest volume is low enough to absorb the cost (§21.1 trade-off).

### Document-structure-aware chunking

Walks the parsed document tree (Markdown AST / HTML DOM / PDF layout blocks) and treats each heading's subtree as a candidate chunk, recursively splitting only sections that exceed the token budget, and **never splitting across a heading boundary**:

```
# Incident Postmortem              ← H1, becomes parent context for all children
## Summary                         ← H2, chunk 1 (with H1 title prefixed as context)
## Timeline                        ← H2, if >max_tokens, recursively split within
### 14:32 UTC — Alert fired        ← H3, chunk 2a
### 14:40 UTC — Mitigation applied ← H3, chunk 2b
## Root Cause                      ← H2, chunk 3
## Action Items                    ← H2, chunk 4 (table preserved intact)
```

Each chunk is prefixed with its heading breadcrumb (`"Incident Postmortem > Timeline > 14:32 UTC — Alert fired"`) before embedding — this single trick measurably improves retrieval because the embedding now encodes context the raw section text alone wouldn't carry.

### Parent-child chunking

Two-tier storage: small **child chunks** (150-300 tokens) are what gets embedded and searched for precision; each child stores a `parent_chunk_id` pointing to a larger **parent chunk** (the full section, 1,000-2,000 tokens) that is what actually gets returned to the generation step for context:

```
ParentChunk(id=P1, text=<full "Timeline" section, 1400 tokens>)
   ├── ChildChunk(id=C1, text=<"14:32 UTC — Alert fired..." 220 tokens>, parent_id=P1, embedding=e1)
   └── ChildChunk(id=C2, text=<"14:40 UTC — Mitigation..." 240 tokens>, parent_id=P1, embedding=e2)
```

Retrieval matches against child embeddings (better precision — a query about "when was the alert fired" matches a focused 220-token chunk far better than a diffuse 1400-token one), but the Retrieval API can be configured to **expand to parent** before returning, giving the generation step enough surrounding context to not hallucinate details the child chunk alone omitted. This is the default for knowledge bases prioritizing answer quality over minimal token usage.

### Tabular data chunking

CSV/database-row content needs its own strategy because "prose-style" splitting on a spreadsheet export destroys the one thing that makes a row meaningful — its column headers:

```python
def chunk_tabular(rows: list[dict], headers: list[str], rows_per_chunk: int = 20) -> list[str]:
    """Group rows into chunks, repeating the header in every chunk so each
    chunk is independently interpretable without the original file."""
    chunks = []
    for i in range(0, len(rows), rows_per_chunk):
        group = rows[i:i + rows_per_chunk]
        header_line = " | ".join(headers)
        row_lines = [" | ".join(str(row[h]) for h in headers) for row in group]
        chunk_text = f"Columns: {header_line}\n" + "\n".join(row_lines)
        chunks.append(chunk_text)
    return chunks
```

`rows_per_chunk` is tuned down (as low as 1, i.e., one row = one chunk) for tables where each row is independently query-relevant (e.g., a support-ticket export — a query about one ticket shouldn't have to compete for embedding "attention" with 19 unrelated rows), and tuned up for tables where rows are only meaningful in aggregate context (e.g., a small reference/lookup table). This mirrors the database connector's `chunking_strategy: fixed_unit` default from §4.6 — for structured sources the platform generally prefers letting the source's own row/record boundary define the chunk boundary rather than imposing a token-count-driven split that would ignore it.

### Sliding window overlap tuning

| Content type | Chunk size (tokens) | Overlap | Rationale |
|---|---|---|---|
| Prose (postmortems, docs) | 400 | 15% (~60 tok) | Enough to catch boundary-spanning sentences without much redundant storage |
| Code | 300 (or one function, whichever larger) | 0-10% | Code has hard syntactic boundaries; overlap mostly wasted tokens |
| Slack messages | 1 message (or thread window of 5) | N/A (message-granular) | Overlap concept doesn't apply to discrete message units |
| Dense legal/compliance text | 250 | 25% (~60 tok) | Higher overlap because losing a qualifying clause at a boundary is costly |

### Chunk metadata preservation

Every chunk carries enough lineage to reconstruct citation and support parent-child expansion without a document re-fetch:

```json
{
  "chunk_id": "ch_9f2a...",
  "document_id": "doc_7c31...",
  "parent_chunk_id": "ch_88b1...",
  "section_path": "Incident Postmortem > Timeline > 14:32 UTC — Alert fired",
  "char_start": 4210,
  "char_end": 4890,
  "page_number": null,
  "chunk_index": 4,
  "chunking_strategy": "structure_aware_v2",
  "language": "en",
  "token_count": 218
}
```

---

## 7. Embedding Service

### Model selection

| Model | Dim | Notes | When used |
|---|---|---|---|
| OpenAI `text-embedding-3-large` | 3072 (truncatable via Matryoshka to 256-1536) | Hosted API, strong general quality, per-token cost | Default for new KBs wanting best-effort quality without self-hosting |
| Cohere `embed-v3` | 1024 | Hosted, strong multilingual + input-type-aware (query vs. document) | KBs with heavy non-English content |
| Open-source `bge-large-en` / `gte-large` | 1024 | Self-hosted, no per-call cost, full data control | Cost-sensitive or data-residency-constrained KBs |
| Open-source `bge-small-en` | 384 | Self-hosted, fast, lower quality | High-volume, latency-sensitive, or budget-constrained KBs; also used as the *cheap boundary-detection* model in semantic chunking (§6.2) |

Selection is a per-knowledge-base config (`embedding_model_id`), not a platform-wide constant — this is what makes §7.4 (model migration) a first-class supported operation rather than a rare emergency.

### Cost comparison, worked

Using the initial-backfill scenario from §2 (10M documents, ~200M chunks, ~250 tokens/chunk average) to make the hosted-vs-self-hosted trade-off concrete rather than qualitative:

```
Total tokens to embed: 200M chunks × 250 tokens ≈ 50B tokens

Hosted API (illustrative pricing, ~$0.02-0.13 per 1M tokens depending on
model tier):
  50,000M tokens × $0.02/1M  ≈ $1,000   (cheapest tier)
  50,000M tokens × $0.13/1M  ≈ $6,500   (highest-quality tier)
  → one-time backfill cost: roughly $1,000-$6,500, no infra to run,
    fully elastic, but recurring on every re-embed (§7.4 migrations
    are NOT free even though they reuse the same pipeline as ingestion)

Self-hosted (bge-large-en class model, ~1,500 chunks/sec/GPU from §2):
  200M chunks ÷ 1,500 chunks/sec ≈ 133,333 sec ≈ 37 GPU-hours
  At ~$1.50/GPU-hour (A10G on-demand): 37 × $1.50 ≈ $56 in raw compute
  → dramatically cheaper per backfill, but requires standing GPU
    infrastructure, model-serving ops, and doesn't parallelize past
    the platform's provisioned GPU pool size the way a hosted API's
    elastic capacity does
```

The order-of-magnitude gap (thousands of dollars vs. tens of dollars for the *same backfill*) is why self-hosted models are the platform default recommendation for any KB expecting recurring high-volume re-embeds (frequent model migrations, high edit-velocity sources), while hosted APIs remain attractive for KBs that embed once and rarely churn, where avoiding GPU-fleet operational overhead is worth the per-token premium.

### Batch processing

```python
async def embed_batch(chunks: list[Chunk], model: EmbeddingModel) -> list[Embedding]:
    # Group by model to allow one API/GPU call to serve many chunks
    texts = [c.text for c in chunks]
    if model.is_hosted_api:
        # hosted APIs: batch up to provider limit (e.g. 2048 inputs/call for OpenAI),
        # with concurrency capped by the platform's negotiated rate limit for that provider
        vectors = await hosted_client.embed(texts, batch_size=2048, model=model.name)
    else:
        # self-hosted: dynamic batching on the GPU inference server (e.g. vLLM/TEI),
        # padded-batch up to GPU memory limit, sorted by length to minimize padding waste
        vectors = await gpu_client.embed(texts, model=model.name)
    return [Embedding(chunk_id=c.id, vector=v, model_id=model.id) for c, v in zip(chunks, vectors)]
```

Two lanes:

- **Bulk lane** (backfills, re-embeds): maximizes throughput, large batches, tolerates minutes of queueing latency, autoscales GPU pool by queue depth.
- **Incremental lane** (single-document edits from live sources like Slack): small batches, prioritized scheduling, optimizes for the freshness SLA (§12) over raw throughput — a Slack message edit shouldn't wait behind a 10M-document backfill.

Both lanes share the GPU pool but incremental-lane requests are placed on a higher-priority queue so they preempt bulk-lane batch formation (a standard priority-queue admission policy, not physically separate GPUs, to avoid under-utilizing hardware during quiet bulk periods).

### GPU inference optimization

- **Dynamic batching** (via Text Embeddings Inference / vLLM-style serving): incoming requests are coalesced into GPU batches up to a max wait (e.g., 10ms) or max batch size, trading a small latency tax for large throughput gains.
- **Length-bucketed batching**: chunks are sorted/bucketed by token length before batching to minimize padding waste — a batch mixing a 20-token and a 500-token chunk wastes 96% of the padded compute on the short one.
- **Quantized inference** (fp16/bf16, or int8 for the smaller models) roughly doubles throughput per GPU with negligible embedding-quality loss for retrieval purposes (unlike quantizing the *stored* vectors, which trades recall directly — see §8.2).
- Roughly **1,500 chunks/sec per A10G** for `bge-base`-class models at 768d with dynamic batching — the number used in §2's capacity math.

Representative serving configuration (Text-Embeddings-Inference-style server), showing how the bulk/incremental lane priority split (§7.2) maps onto actual serving knobs rather than staying purely conceptual:

```yaml
model_id: bge-large-en-v2
max_batch_tokens: 16384          # caps GPU memory use per batch
max_batch_requests: 256
max_wait_ms:
  incremental_lane: 5            # low latency floor — small batches, frequent flushes
  bulk_lane: 50                  # tolerates more queueing to build fuller batches
length_bucketing: true            # sort-by-length before batch formation (above)
precision: bf16
queue_priority:
  incremental: 10                 # higher number = served first when both queues
  bulk: 1                         # have pending work, without starving bulk entirely
admission_control:
  bulk_lane_max_gpu_share: 0.7    # bulk work can never claim more than 70% of the
                                   # pool even under a large backfill (§18.3)
```

The two `max_wait_ms` values are the concrete mechanism behind the abstract "priority lane" description in §7.2 — a single physical GPU pool serves both lanes, and the only difference between them is how long the scheduler is willing to wait to accumulate a fuller (more efficient) batch before dispatching, which is exactly the latency/throughput trade each lane is optimizing for.

### Model versioning and migration

Every embedding is tagged with `model_id` (name + version). A knowledge base has exactly one **active** model per "embedding slot" but can have **multiple slots concurrently populated** during a migration:

```
Migration timeline for KB "eng-wiki" switching bge-large-en-v1 → bge-large-en-v2:

t0: KB config: primary=v1. All queries embed with v1, search v1 index.
t1: Migration triggered. Backfill job re-embeds all existing chunks with v2,
    written to a PARALLEL v2 vector index. New/updated chunks embedded with
    BOTH v1 and v2 during the migration window (dual-write).
t2: Backfill completes (progress tracked, resumable — same chunk stream as
    §5's pipeline, replayed from Document Store, not from source connectors).
t3: Shadow evaluation: run the KB's golden dataset (§13) against the v2
    index, compare recall@k/NDCG against v1 baseline. Must meet or beat
    threshold before cutover.
t4: Cutover: KB config flips primary=v2 atomically (a config write, not a
    data migration) — queries now search v2 index exclusively.
t5: v1 index retained for a rollback window (default 7 days), then
    garbage-collected.
```

Because a migration is "replay documents through chunking+embedding again," it reuses the exact same pipeline as initial ingestion — there is no separate "re-embed" code path to maintain and drift from the real one.

### Dimensionality trade-offs

| Dimension | Storage/vector (fp16) | Relative recall (vs. full dim, typical) | When to use |
|---|---|---|---|
| 1536 (full) | 3,072 B | baseline | Default for quality-sensitive KBs with moderate scale |
| 768 | 1,536 B | ~99% | Good default — most of the quality at half the storage/compute |
| 384 | 768 B | ~96-97% | High-scale or latency-sensitive KBs (billions of chunks) |
| 256 | 512 B | ~93-95% | Extreme-scale or cost-constrained; still usable for coarse pre-filtering before rerank |

**Matryoshka Representation Learning (MRL)** models (e.g., OpenAI `text-embedding-3-*`, some open-source MRL-trained models) are trained so that a **truncated prefix** of the full vector is itself a valid, still-good embedding — meaning a KB can start at full dimension and later truncate to a smaller stored dimension *without re-embedding*, just by storing/indexing a slice. This decouples the dimensionality decision from the migration protocol above for MRL-capable models specifically: truncation is a reindex, not a re-embed.

---

## 8. Vector Store

### Index types

| Index | Build cost | Query latency | Recall | Memory | When used |
|---|---|---|---|---|---|
| **Flat (brute force)** | none | O(n), slow at scale | 100% | 1x vectors | Small KBs (<100k chunks) where exactness beats speed and n is cheap to scan |
| **HNSW** | High (graph build) | O(log n), fast | 95-99% (tunable via `ef_search`) | ~1.5-2x vectors (graph edges) | Default for most KBs — best latency/recall trade-off at moderate memory cost |
| **IVF-PQ** | Moderate (train centroids + quantize) | Fast, scans few clusters | 85-95% (tunable via `nprobe`, PQ bits) | Much lower — PQ compresses vectors 8-32x | Very large KBs (100M+) where HNSW's memory footprint is prohibitive |

Per-KB index type is chosen by size and recall requirements at KB creation, with the platform defaulting HNSW → IVF-PQ automatically once a KB crosses a configurable chunk-count threshold (e.g., 200M), migrating in the background (same replay-from-Document-Store mechanism as §7.4).

### HNSW parameter tuning

Three parameters dominate the HNSW latency/recall/memory trade-off, and the platform exposes sane per-scale defaults rather than making every KB owner learn ANN theory:

| Parameter | Effect | Platform default | Tuning guidance |
|---|---|---|---|
| `M` (max edges per node) | Higher `M` → better recall, more memory, slower build | 16 (small/medium KBs), 32 (large KBs prioritizing recall) | Rarely changed post-creation — it's baked into the graph structure, so changing it means a full rebuild, not a config flip |
| `ef_construction` (candidate list size at build time) | Higher → better graph quality, slower ingest | 200 | Bumped only for KBs where ingest is infrequent/batchy (nightly backfill) and can absorb slower builds for better steady-state recall |
| `ef_search` (candidate list size at query time) | Higher → better recall, higher latency | 100 (auto-scaled down under load shedding, §17) | The one HNSW knob exposed **per query**, not just per KB — an application can trade recall for latency on individual calls (e.g., a lower-stakes autocomplete-style query can request `ef_search=40`) |

The `ef_search` numbers from §8's benchmark table (50 → 3ms/92% recall, 200 → 9ms/98% recall) are what this default is calibrated against: 100 sits close to the knee of the latency/recall curve for the platform's stated P50 ≤ 80ms target, leaving headroom for the permission pre-filter and hybrid-fusion overhead layered on top.

### IVF-PQ sizing example

Worked arithmetic for the largest single KB (2B chunks, §2), the case that forces IVF-PQ rather than HNSW:

```
Vectors:              2,000,000,000
Raw dim:               768 (float32 = 3,072 B/vector)
Raw storage (no PQ):   2B × 3,072 B ≈ 5.7 TB  — infeasible to keep in RAM as HNSW

PQ config:  m = 96 subvectors, 8 bits/subvector (256 centroids each)
PQ code size per vector:  96 × 1 byte = 96 B      (32x compression vs. raw float32)
PQ-compressed storage:    2B × 96 B ≈ 179 GB       — fits across a modest shard fleet

IVF clustering:  nlist = 65,536 coarse centroids (≈ sqrt(N) x a few, standard heuristic)
Avg vectors/cluster:  2B / 65,536 ≈ 30,500
Query with nprobe=32:  scans 32 of 65,536 clusters ≈ 32 × 30,500 ≈ 976,000 candidate
                        codes — a ~2000x reduction vs. a full 2B-vector scan,
                        then PQ-distance-scored, then top ~500 rescored full-
                        precision (the recall-recovery step from §8.2)
```

This is the concrete justification behind the "100M+ chunks → IVF-PQ" threshold in the index-type table above: HNSW's ~1.5-2x graph overhead on 5.7 TB of raw vectors would mean 8.5-11.4 TB of RAM-resident graph for one KB alone — outside what the platform is willing to dedicate to a single tenant regardless of replication strategy, whereas IVF-PQ's 179 GB compressed footprint is a shape the sharding strategy (§8.5) can spread comfortably across a dedicated node pool.

### Quantization

- **Scalar quantization (int8)**: each float dimension mapped to an 8-bit int via a learned per-dimension min/max scale. ~4x storage reduction, typically <1% recall loss. Cheap, reversible-enough, applied broadly.
- **Product quantization (PQ)**: vector split into `m` subvectors, each independently vector-quantized against a learned codebook (e.g., 256 centroids/subvector = 8 bits/subvector). Achieves 8-32x compression but with more recall loss (varies 5-15% depending on `m` and data); paired with IVF for the largest KBs, and typically followed by a **rerank pass against full-precision vectors for the top-N survivors** to recover most of the lost recall cheaply (rescore 100-500 candidates full-precision after a fast approximate PQ scan — far cheaper than scanning all vectors full-precision).

### Multi-tenancy

Isolation between knowledge bases is layered, not a single mechanism, because the failure modes at each layer are different:

| Layer | Mechanism | Protects against |
|---|---|---|
| **Logical** | Every index entry is namespaced by `kb_id`; a query without an explicit, authorized `kb_id` matches nothing — there is no cross-tenant default scope | Accidental cross-KB data mixing in code, not malicious access (that's §11's job) |
| **Resource quotas** | Per-KB caps on ingest throughput (chunks/sec), query QPS, and storage — enforced at admission, not just monitored after the fact | One KB's traffic spike or backfill consuming shared-pool capacity that other KBs depend on |
| **Logical shard isolation** (small/medium KBs) | Each KB's shard(s) are a distinct index structure, even when co-located on shared hardware — no shared graph, no shared postings list | A slow query against KB A cannot slow down a concurrent query against KB B on the same node, since they touch disjoint memory structures and are scheduled independently |
| **Physical isolation** (large KBs, >500M chunks) | Dedicated node pools (§8.5) — not just a separate index, a separate machine fleet | The remaining risk after logical isolation: a KB large/hot enough to saturate a shared node's CPU/network/disk I/O even while touching "its own" index structure |

The threshold for physical isolation (500M chunks, matching the sharding table below) is deliberately conservative — most noisy-neighbor incidents in practice come from the top handful of largest/hottest KBs, so isolating those completely removes the vast majority of cross-tenant risk without paying dedicated-hardware cost for the long tail of small KBs that logical isolation already handles well.

### Sharding strategy

Two shard axes, chosen per KB by scale:

1. **By knowledge base** (default): every KB up to the single-shard capacity (§2: ~25M vectors) gets one shard. This is the common case (90% of KBs) — no fan-out needed, simplest operationally, and gives clean noisy-neighbor isolation for free (§18.4).
2. **By KB + hash-partition** (large KBs): a KB exceeding single-shard capacity is split across N shards by a hash of `chunk_id` (uniform distribution, no hot shard from e.g. alphabetical document-id skew). Queries fan out to all N shards and merge (§9.3).

```
KB size < 25M chunks  → 1 shard   (the common case, ~90% of KBs)
KB size 25M - 500M    → N = ceil(size / 25M) shards, hash-partitioned
KB size > 500M        → same, but shards distributed across dedicated
                         node pools to guarantee isolation from smaller
                         co-tenanted KBs (physical, not just logical, isolation)
```

### Replication

Each shard replicated **3x** across availability zones (mirrors the durability reasoning of a classic distributed store — survive node loss and AZ loss with zero query downtime). Replicas serve reads round-robin/least-loaded; writes (new/updated embeddings) go through a primary per shard and asynchronously propagate to replicas, with a bounded replication lag SLO (§12.4 ties this to the freshness contract — replication lag is one component of end-to-end ingest-to-retrievable latency).

### Hybrid search architecture

Dense vector search alone misses exact-match/lexical signals (error codes, product SKUs, person names, acronyms) that keyword search excels at. Every chunk is indexed in **two parallel structures**:

```
Chunk("The payments-api-gateway threw ERR_TIMEOUT_504 during the outage")
   │
   ├──▶ Dense index (HNSW/IVF): embedding vector
   │
   └──▶ Sparse index (BM25 inverted index, or SPLADE learned-sparse):
        term postings — "payments-api-gateway", "err_timeout_504", "outage", ...
```

Query time runs **both** searches and combines:

```python
def hybrid_search(query: str, kb_id: str, top_k: int, alpha: float = 0.5):
    dense_results = dense_index.search(embed(query), top_k=top_k * 4)   # overfetch
    sparse_results = bm25_index.search(query, top_k=top_k * 4)
    fused = reciprocal_rank_fusion(dense_results, sparse_results, weight_dense=alpha)
    return fused[:top_k]
```

`alpha` (dense vs. sparse weight) is tunable per query or per KB default — KBs heavy on structured/technical content (runbooks full of error codes) often default lower `alpha` (more lexical weight) than KBs of narrative prose (postmortem summaries).

**SPLADE** (learned sparse retrieval) is offered as an upgrade over plain BM25 for KBs that show a measurable eval lift (§13) from it — it captures term-expansion (matching "car" against a document saying "automobile") that BM25 cannot, at the cost of a small model-inference step at both index and query time, so it's opt-in rather than default.

### Filtered search: pre-filter vs. post-filter

This is the single highest-stakes performance decision in the vector store, because **permission filtering is a filtered search** (§11) and it runs on every query.

| Approach | Mechanism | Pro | Con |
|---|---|---|---|
| **Post-filter** | Run ANN search unfiltered, discard results failing the filter, refetch if too few survive | Simple, reuses unmodified ANN index | Pathological when filter is selective (e.g., permission filter excludes 99% of a KB for a low-privilege user) — may need many refetch rounds or silently under-return |
| **Pre-filter** | Restrict the ANN search itself to only the allowed candidate set before/during graph traversal | Correct result count even under highly selective filters | Requires filter-aware index traversal (e.g., HNSW variants that check a bitset during graph walk) — more complex, some added per-hop cost |
| **Hybrid (adaptive)** | Estimate filter selectivity; use post-filter with overfetch when selectivity is high (filter passes >~20% of candidates), switch to pre-filter/bitset-restricted traversal when selectivity is low | Gets the cheap path most of the time, correctness under the worst case | Requires selectivity estimation (cheap: maintain approximate per-filter-value cardinality) |

**Decision: adaptive, defaulting toward pre-filter for permission filters specifically.** Permission filters are exactly the pathological case (a document set a user *can* see is often a small fraction of a large shared KB), so the platform treats the permission allow-set as a **bitset passed into the ANN traversal** (most modern HNSW implementations, e.g., via a `filter` callback checked during candidate expansion) rather than ever relying on post-filter-and-hope for security-relevant filtering. Non-permission metadata filters (date range, source, content type) use the adaptive approach since getting slightly fewer results back on a rare selective filter is a quality issue, not a security one.

### Benchmarks (indicative, single shard, 25M vectors, 768d, HNSW)

| Configuration | P50 latency | P99 latency | Recall@10 |
|---|---|---|---|
| Dense only, `ef_search=50` | 3 ms | 12 ms | 92% |
| Dense only, `ef_search=200` | 9 ms | 28 ms | 98% |
| Dense + BM25 hybrid (RRF) | 14 ms | 40 ms | 97% (hybrid recall metric) |
| + permission pre-filter (bitset, ~30% allow-rate) | +2-4 ms | +8-15 ms | unchanged (correctness preserved) |
| + permission post-filter (naive, ~5% allow-rate — pathological) | +40-150 ms (multiple refetch rounds) | +300ms+ | degrades — under-returns without refetch logic |

The last row is included deliberately: it is the empirical justification for the pre-filter decision above, not a hypothetical.

**IVF-PQ, for comparison** (indicative, single shard, 2B vectors from the §8.2 sizing example, `nlist=65536`):

| Configuration | P50 latency | P99 latency | Recall@10 |
|---|---|---|---|
| `nprobe=8` (fast, low recall) | 4 ms | 15 ms | 78% |
| `nprobe=32` (platform default for this scale) | 11 ms | 35 ms | 89% |
| `nprobe=32` + full-precision rescore of top-500 (§8.2's recovery step) | 18 ms | 55 ms | 96% |
| `nprobe=128` (high recall, high cost) | 40 ms | 120 ms | 94% (rescore still wins — fewer clusters + rescore beats more clusters without it) |

The third row is the platform's actual default for IVF-PQ-tier KBs: it costs roughly 60% more latency than the raw `nprobe=32` scan, but the recall jump (89% → 96%) is large enough that the rescore step is treated as mandatory rather than optional for this index type, unlike HNSW's `ef_search`, which is tunable per-query precisely because its default `ef_search=100` already sits close enough to the quality/latency knee that most callers don't need to think about it.

### Backup and restore

Even with 3x replication (§8.4), the vector store needs a backup strategy distinct from replication — replication protects against hardware loss, not against a bad deploy that corrupts an index, an operator error that deletes the wrong KB, or a bug that silently writes malformed vectors that only a point-in-time restore can undo.

| Mechanism | What it protects against | Restore time |
|---|---|---|
| Continuous replication (§8.4) | Node/AZ hardware loss | Seconds (automatic failover, no restore needed) |
| Nightly index snapshot (per shard, to object storage) | Index corruption, bad deploy, accidental shard-level deletion | Minutes to low hours depending on shard size — a snapshot restore is a direct copy, faster than the Document-Store-replay rebuild path |
| Document Store replay (§18.2) | Total loss of a shard's vector data (all replicas + all snapshots gone) — the ultimate fallback | Hours — re-runs chunking + embedding, the slowest but always-available option since the vector store is a derived index by design |

The three-tier structure is deliberate: snapshots exist specifically to make the **common** corruption/operator-error case fast (minutes, not hours) without relying on the much slower full-replay path, while the replay path remains the guaranteed-correct fallback for the rare case where snapshots themselves are unavailable or untrusted (e.g., a corruption bug that had already been silently writing bad snapshots for a while — the replay path re-derives from Document Store content directly, so it can't inherit a vector-store-layer bug the way restoring a snapshot of the same buggy index would).

A KB's snapshot retention (default 7 days of nightly snapshots) is a knob the owning team can extend for compliance-sensitive KBs, surfaced through the same schema/config API as other KB-level settings (§16).

---

## 9. Retrieval Service

### Query processing pipeline

```
Query("how do we roll back a failed payments deploy?", user=alice, kb="eng-wiki")
   │
   ▼
1. Query understanding / expansion (optional, per KB config)
   - Multi-query: generate 2-3 paraphrases via a small LLM, search each, merge
   - HyDE: generate a hypothetical answer, embed *that*, search with it
        (helps when queries are short/underspecified vs. prose-like chunks)
   │
   ▼
2. Permission resolution (parallel with step 3's setup)
   - Resolve alice → allow-set bitset for KB "eng-wiki" (Permission Service, §11)
   │
   ▼
3. Hybrid search execution (dense + sparse, pre-filtered by allow-set, §8.6)
   - Fan out to all shards of "eng-wiki" if sharded, else single shard
   │
   ▼
4. Result merging (across shards, across multi-query variants if used)
   - Score normalization across shards (min-max or z-score per shard before merge)
   │
   ▼
5. Metadata filtering (date range, source, content type — already partly
   pushed into step 3 as pre-filters where selective; residual filters
   applied here on the smaller merged candidate set)
   │
   ▼
6. Reranking (optional, §10)
   │
   ▼
7. Response assembly (chunk text, citation, scores, freshness metadata)
```

### Query expansion in depth

- **Multi-query**: for short/ambiguous queries, an LLM generates 2-3 alternative phrasings; each is searched independently and results are merged via RRF. Measurably improves recall for queries that don't lexically/semantically match the phrasing of the source documents, at the cost of 2-3x search fan-out and an LLM call's added latency (~100-300ms) — gated behind a per-KB or per-call flag, not default-on for latency-sensitive callers.
- **HyDE (Hypothetical Document Embeddings)**: instead of embedding the query directly, an LLM first drafts a plausible answer, and *that* draft is embedded and searched. Effective when queries are short keyword-style ("payments rollback procedure") but the corpus is written in prose that a keyword-style embedding matches poorly. Same latency trade-off as multi-query; the two are usually mutually exclusive per query (pick one), not stacked.

### Result merging across shards

For sharded KBs (§8.5), each shard returns its local top-`k*overfetch`, scored on its own local scale. Because HNSW similarity scores aren't perfectly comparable across independently-built shard indices at the tail, the service **overfetches per shard** (e.g., top-3k from each of N shards) and re-ranks the merged 3k·N candidates by score, trusting relative order more within a shard than absolute score comparability across shards — this is a standard scatter-gather caveat and is why reranking (§10) matters more, not less, for sharded KBs.

Score normalization, made concrete (min-max per shard before merge, the cheapest option that avoids one shard's naturally-tighter score distribution dominating the merge purely due to scale):

```python
def merge_shard_results(shard_results: list[list[ScoredChunk]], top_k: int) -> list[ScoredChunk]:
    normalized = []
    for results in shard_results:
        if not results:
            continue
        scores = [r.score for r in results]
        lo, hi = min(scores), max(scores)
        span = (hi - lo) or 1e-9   # guard against a degenerate single-score shard
        for r in results:
            r.normalized_score = (r.score - lo) / span
        normalized.extend(results)
    return sorted(normalized, key=lambda r: -r.normalized_score)[:top_k]
```

This is a deliberately cheap normalization, not a statistically rigorous one — it's good enough to make cross-shard ordering *reasonable* for the overfetched candidate pool feeding into reranking, but it is explicitly **not** trusted as the final relevance signal on its own, which is exactly why sharded KBs benefit more from reranking (§10) than single-shard ones: the cross-encoder pass re-scores every surviving candidate against the actual query text, sidestepping the cross-shard score-comparability problem entirely rather than trying to solve it more precisely at the merge step.

### Metadata filtering

Filters are typed and declared per KB via the schema registry (§16), e.g., `source: enum`, `created_after: timestamp`, `labels: array<string>`. High-selectivity, high-cardinality filters (like permission allow-sets) are pushed to pre-filter (§8.6); low-selectivity filters (like `language = "en"` on an English-majority KB) are cheaper as post-filters on the smaller candidate set and are handled there by default, with per-KB override if a filter proves pathological in practice (observed via the query-analytics pipeline, §16 KB management).

### Pagination

Cursor-based, not offset-based (offset pagination on an ANN index re-runs the search each page and can return duplicates/gaps as the index mutates underneath). A retrieval response includes an opaque `next_cursor` encoding the query embedding hash + rank offset + a snapshot marker of the index version searched, valid for a bounded TTL (5 minutes) after which a new query must be issued (protects against paginating against a now-stale, GC'd index version).

### Caching hot queries

- **Query-result cache**: keyed on `(kb_id, normalized_query, filters, top_k, principal_allow_set_version)` — critically including the allow-set version, so a cached result is never served to a principal whose permissions have since changed (cache entries are naturally invalidated by allow-set version bumps, not by a separate invalidation sweep). TTL default 5 minutes, tunable down for freshness-sensitive KBs, effectively disabled (TTL≈0) for KBs whose eval configuration shows caching materially hurts freshness-sensitive use cases.
- **Embedding cache**: the query embedding itself (expensive relative to the cache lookup) is cached independent of the full result, keyed on `(model_id, normalized_query_text)` — reused even across different KBs/filters if the same text is queried against multiple KBs, and reused when only filters (not query text) differ between two calls.

Cache key construction and cursor encoding, made concrete:

```python
def query_cache_key(kb_id: str, query: str, filters: dict, top_k: int, allow_set_version: int) -> str:
    normalized_query = query.strip().lower()
    filter_fingerprint = hashlib.sha256(
        json.dumps(filters, sort_keys=True).encode()
    ).hexdigest()[:16]
    # allow_set_version is bumped by the Permission Service on any grant
    # change affecting this (principal, kb) pair — see §11.4 — so a stale
    # cache entry is naturally unreachable, never explicitly hunted down
    return f"{kb_id}:{normalized_query}:{filter_fingerprint}:{top_k}:v{allow_set_version}"

def encode_cursor(query_hash: str, rank_offset: int, index_version: str, ttl_s: int = 300) -> str:
    payload = {
        "q": query_hash, "offset": rank_offset, "idx_ver": index_version,
        "exp": int(time.time()) + ttl_s,
    }
    # signed, not just base64-encoded, so a client cannot forge a cursor
    # that reads past its authorized allow-set snapshot
    return sign_and_encode(payload, key=CURSOR_SIGNING_KEY)

def decode_cursor(cursor: str) -> CursorPayload:
    payload = verify_and_decode(cursor, key=CURSOR_SIGNING_KEY)  # raises on tamper/expiry
    if payload["exp"] < time.time():
        raise CursorExpiredError("re-issue the original query")
    if payload["idx_ver"] != current_index_version(payload["kb_id"]):
        raise CursorStaleIndexError("index changed underneath this cursor, re-query")
    return payload
```

The signed-cursor detail matters more than it looks: an **unsigned** cursor (plain base64 of `{offset, query}`) would let a client hand-craft an arbitrary offset into a query it never actually issued through the permission-filtered path — a signed cursor closes that gap by making the cursor only ever derivable from a request that already passed permission filtering, consistent with §11.6's zero-leakage claims applying to *every* code path that returns chunk data, not just the first page.

---

## 10. Reranking

### Cross-encoder reranking

Unlike the bi-encoder embeddings used for initial retrieval (query and document embedded independently, compared by vector similarity — fast but leaves quality on the table because the model never sees query and document together), a **cross-encoder** takes `(query, chunk)` pairs jointly through a transformer, producing a much more accurate relevance score at much higher per-pair cost:

```python
def rerank(query: str, candidates: list[Chunk], model: CrossEncoder, top_n: int) -> list[ScoredChunk]:
    pairs = [(query, c.text) for c in candidates]
    scores = model.predict(pairs, batch_size=32)   # e.g. ms-marco-MiniLM, or Cohere Rerank API
    return sorted(zip(candidates, scores), key=lambda x: -x[1])[:top_n]
```

Typical models: `ms-marco-MiniLM-L-6-v2` (self-hosted, fast, good baseline), `bge-reranker-large` (self-hosted, stronger), Cohere Rerank v3 (hosted API, strong, adds network latency). Reranking is applied to the **top ~100 candidates** from initial retrieval (not the full corpus, obviously) — this bounds the cost to a fixed, budgetable number of cross-encoder calls regardless of KB size.

### Reciprocal rank fusion (RRF) for hybrid search

Used to merge the dense and sparse ranked lists (§8.6) before or instead of a cross-encoder pass — cheap (no model inference), rank-based (robust to the two lists having incomparable raw scores):

```python
def reciprocal_rank_fusion(*ranked_lists: list[ChunkId], k: int = 60) -> list[ChunkId]:
    scores = defaultdict(float)
    for ranked_list in ranked_lists:
        for rank, chunk_id in enumerate(ranked_list):
            scores[chunk_id] += 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda c: -scores[c])
```

The constant `k=60` is the standard RRF damping factor (de-weights the tail without a tunable per-KB knob needed in practice).

### MMR for diversity

Given a relevance-ranked candidate list, **Maximal Marginal Relevance** re-orders to balance relevance against redundancy, so five near-duplicate chunks from the same document (or the near-duplicate documents flagged in §5.4) don't crowd out genuinely different information:

```python
def mmr(query_vec, candidates: list[ScoredChunk], lambda_param=0.7, top_k=10):
    selected, remaining = [], candidates[:]
    while remaining and len(selected) < top_k:
        def mmr_score(c):
            relevance = cosine(query_vec, c.embedding)
            redundancy = max([cosine(c.embedding, s.embedding) for s in selected], default=0)
            return lambda_param * relevance - (1 - lambda_param) * redundancy
        best = max(remaining, key=mmr_score)
        selected.append(best)
        remaining.remove(best)
    return selected
```

`lambda_param` near 1 behaves like pure relevance ranking; near 0 maximizes diversity at the cost of relevance. Default 0.7, exposed as a per-call tuning parameter for applications (e.g., a "give me a broad survey" use case wants lower lambda than a "find the one right answer" use case).

**Complexity note**: the naive MMR loop above is `O(top_k × |candidates|)` pairwise-similarity comparisons — for the platform's typical MMR input (candidates ≤ 100, top_k ≤ 20) that's at most 2,000 cosine comparisons, comfortably under a millisecond even unoptimized, which is why §10.4's latency table shows MMR adding only 1-3ms. It would **not** stay cheap if applied to the full pre-rerank candidate pool at large-KB scale (thousands of candidates) — the platform enforces MMR only ever runs after the candidate set has already been narrowed by hybrid search + optional cross-encoder rerank, never as a first-pass filter over an unbounded result set.

### Reranking latency budget and when to skip it

| Stage | Added P50 latency | Added P99 latency |
|---|---|---|
| RRF (rank fusion, no model) | <1 ms | <2 ms |
| MMR (over top 50, embeddings already in hand) | 1-3 ms | 5 ms |
| Cross-encoder rerank, self-hosted, top-100, GPU batch | 40-80 ms | 150-250 ms |
| Cross-encoder rerank, hosted API (Cohere), top-100 | 80-150 ms | 300-500 ms |

Given the platform's P99 ≤ 300ms target for unreranked queries and ≤ 700ms for reranked (§ task NFRs), cross-encoder reranking is **opt-in per call** (`rerank: true` in the retrieval request), with the Retrieval Service documentation steering callers explicitly:

- **Skip reranking**: autocomplete-adjacent/typeahead use cases, high-QPS internal tools where raw hybrid search recall is already sufficient, or any caller whose own eval (§13) shows no measurable quality lift from reranking on their query distribution.
- **Use reranking**: user-facing Q&A/copilot applications where answer quality materially matters and the extra 100-400ms is acceptable within the overall LLM-generation latency budget (which is usually seconds anyway, making reranking's cost proportionally small).

RRF and MMR, being cheap, are on by default for hybrid-search and multi-result use cases respectively; only the cross-encoder step is latency-gated.

---

## 11. Permission-Aware Retrieval

This is the section where a mistake is a security incident, not a quality regression — treated accordingly.

### ACL sync from sources

Each connector's `fetch_acl` (§4.1) normalizes source-native permissions into a common model:

```json
{
  "doc_id": "doc_7c31...",
  "source_acl_ref": "confluence:space:ENG:page:12345",
  "grants": [
    {"principal_type": "user", "principal_id": "jdoe@company.com", "level": "read"},
    {"principal_type": "group", "principal_id": "eng-all@company.com", "level": "read"},
    {"principal_type": "group", "principal_id": "eng-leads@company.com", "level": "write"}
  ],
  "inherited_from": "confluence:space:ENG",   // space-level defaults, if page doesn't override
  "synced_at": "2024-08-12T14:35:00Z"
}
```

Group principals (`eng-all@company.com`) are stored **as groups**, not expanded to member lists at sync time — expansion happens at query time against the identity provider (§11.3), because a group's membership changes far more often than document ACLs do, and pre-expanding would mean re-syncing every document's ACL on every team-membership change (a combinatorial freshness problem the platform avoids entirely by deferring expansion).

### Permission index

A dedicated store, separate from the vector store's metadata, optimized for the query pattern "given a document set, which does principal P have access to" — implemented as an inverted index from `(kb_id, chunk_id) → grant_list` plus a cached, TTL'd `(principal_id) → resolved_group_set` mapping refreshed from the identity provider.

```
Permission Index
  chunk_id → [grant, grant, ...]        (small, cacheable, updated on ACL sync)

Group Expansion Cache
  user_id → {group_id, group_id, ...}   (TTL 5 min, refreshed from IdP, or
                                          push-invalidated on group-membership
                                          change events where the IdP supports them)
```

### Query-time permission filtering strategy: pre-filter vs. post-filter

Already decided in principle in §8.6 (bitset pre-filter into ANN traversal); here is the full reasoning and the fallback layering, because permissions get **two independent enforcement points**, not one — defense in depth against any single bug:

```
Query(user=alice, kb="eng-wiki")
   │
   ▼
1. Resolve alice's allow-set:
   allow_set = {chunks where alice is a direct grantee}
             ∪ {chunks where any of alice's resolved groups is a grantee}
   Represented as a compact bitset/roaring-bitmap over the KB's chunk-id space.
   │
   ▼
2. PRIMARY enforcement: pass allow_set as a pre-filter into the ANN/BM25
   search itself (§8.6) — the search literally cannot surface a chunk
   outside the allow-set. This is also what makes filtered search fast
   under highly selective permission filters.
   │
   ▼
3. SECONDARY enforcement (defense in depth): before results leave the
   Retrieval Service, re-check every returned chunk_id against allow_set
   directly (cheap — it's already an in-memory bitset). Any chunk that
   somehow appears despite step 2 (index bug, stale cache, race) is
   dropped here, and the mismatch is logged as a P0-severity anomaly
   for immediate investigation — this should never fire in steady state.
```

**Why both layers**: pre-filtering at the index gets correctness *and* performance under selective filters. The secondary in-service check costs almost nothing (bitset membership test) and converts "the index had a bug" from an information leak into a dropped result plus a loud alarm — the difference between a security incident and a caught bug.

### Group expansion correctness

Group expansion is the most common source of permission bugs in practice (nested groups, group-of-groups, recently-removed members). The Permission Service:

- Resolves **transitive** group membership (a group containing a group) up to a bounded depth (e.g., 10 levels — logged/alerted if exceeded, since deeper nesting usually indicates an IdP modeling problem worth fixing upstream).
- Treats a **failed or timed-out** group-expansion call as "resolve to empty/direct-grants-only," i.e., **fails closed** — a principal whose group memberships can't currently be resolved sees *less* than they're entitled to, never more. This is the single load-bearing invariant in the whole permission design, called out explicitly because "fail open on error" is the natural (wrong) instinct when optimizing for availability.

**Worked example**, tracing a query from a principal in a nested group through to the allow-set bitset:

```
Principal: bob@company.com
IdP group memberships (direct): {support-l2}
Group hierarchy in IdP:  support-l2 ⊂ support-all ⊂ eng-all-readers

Document doc_9931 has grants:
  - principal_type=group, principal_id=eng-all-readers, level=read

Resolution:
  1. Direct groups for bob: {support-l2}
  2. Transitive closure (depth-bounded, max 10):
       support-l2 → support-all (depth 1)
       support-all → eng-all-readers (depth 2)
     Resolved set: {support-l2, support-all, eng-all-readers}
  3. allow_set(bob, kb) = union of chunk_ids where any grant.principal_id
     ∈ {bob@company.com} ∪ {support-l2, support-all, eng-all-readers}
  4. doc_9931's chunks: grant.principal_id = eng-all-readers ∈ resolved
     set → INCLUDED in bob's allow-set.

Now suppose the IdP call in step 2 times out after resolving support-l2 →
support-all but before reaching eng-all-readers (a partial-depth failure,
not a total failure):
  - Fail-closed policy: the resolution is treated as failed for THIS
    request, not partially-successful — bob's allow-set for this query
    falls back to {support-l2, support-all} only (no eng-all-readers),
    so doc_9931 is EXCLUDED even though a complete resolution would have
    included it. Bob sees one false negative this query; he sees zero
    false positives, ever, by construction.
```

The last paragraph is the concrete illustration of "fails closed" from a design principle into an actual behavior: a partial group-expansion failure costs the user a missed result, not a security incident, and the next query (cache TTL expired, or a fresh resolution attempt) self-heals it.

### Permission cache and revocation latency

The allow-set bitset (§11.2) is cached per `(principal_id, kb_id)` for **at most 60 seconds** — short enough that a revoked grant or removed group membership is reflected in retrieval within the platform's stated bound, long enough to keep the common case (repeated queries by the same user) cheap. Cache entries are also **actively invalidated** (not just left to expire) on:

- A group-membership-change webhook from the IdP, where available.
- A source-side permission-change event from the connector (someone unshares a Drive doc), which propagates as an ACL-sync event feeding a tombstone-and-cache-bust for every principal who lost access to that specific document — this is a targeted invalidation (only affected principal×doc pairs), not a full cache flush.

Combined with the pipeline's ≤5-minute tombstone SLA (§12.3) and this ≤60s cache bound, the platform's stated permission-revocation guarantee is: **a revoked grant stops being retrievable within 5 minutes, and a cached allow-set is never more than 60 seconds stale even before that pipeline catches it** — two independent, additive bounds, not one number picked to sound good.

### Federated multi-KB search

Most retrieval calls target one KB, but some legitimate use cases (an org-wide "search everything I have access to" tool) need to query several. Rather than build a shared multi-tenant index that would undermine §3's isolation argument, federation is implemented as **fan-out at the Retrieval Service layer, with each KB's permission-filtering pipeline run to completion independently**:

```python
def federated_retrieve(principal: Principal, kb_ids: list[str], query: str, top_k: int):
    # Each KB call runs the FULL single-KB pipeline (§9's 7 steps, including
    # both permission-filter layers, §11.3) independently and completely —
    # there is no shared intermediate result set between KBs at any point
    per_kb_results = parallel_map(
        lambda kb_id: retrieve(principal, kb_id, query, top_k=top_k),  # full pipeline
        kb_ids,
    )
    # Merge happens ONLY after each KB's results have already been fully
    # permission-filtered and are individually safe to expose to `principal`
    merged = reciprocal_rank_fusion(*[r.results for r in per_kb_results])
    return merged[:top_k]
```

The property this construction guarantees: **a result from KB B can only appear in the merged output if it already passed KB B's own complete, independent permission pipeline** — there is no code path where a candidate from one KB is evaluated against another KB's allow-set, and no shared reranking step runs before each KB's individual permission filtering completes (reranking, if requested, applies to the already-merged, already-filtered set). If the principal is not authorized for KB B at all (no allow-set could be resolved, or the KB itself denies the principal access), that KB's fan-out call returns an empty result set rather than an error — a caller federating across 10 KBs, authorized for 6, transparently gets results from those 6 without needing to know in advance which ones would fail.

### Zero-information-leakage guarantee, stated precisely

"Zero leakage" needs to be more than a slogan. The concrete claims the design makes, and the mechanism backing each:

| Claim | Mechanism |
|---|---|
| A user cannot retrieve chunk text from a document they can't access at the source | Pre-filter (§11.3 step 2) + secondary in-service check (step 3) |
| A user cannot infer a document's *existence* from result counts/scores of a query they're not authorized for | Retrieval API never returns "N results were filtered out"; result counts reflect only the allow-set-filtered set from the start (pre-filtering, not post-filtering-and-reporting-totals) |
| A user cannot infer restricted content via a *reranking* score leak (e.g., a cross-encoder call accidentally scoring a chunk outside the allow-set) | Reranking operates strictly on the post-permission-filtered candidate set (§9 pipeline step ordering: permission filter happens in step 2-3, before step 6 reranking) — a restricted chunk never reaches the reranker at all |
| A permission-sync lag results in under-sharing, never over-sharing | Fail-closed default (§11.4) — a chunk with *no* successfully synced ACL is treated as **zero-grantee** (visible to nobody but KB admins) until a sync succeeds, not treated as public |
| Cross-knowledge-base queries can't be used to bypass a single KB's ACLs | No implicit "search all KBs" mode (task NFR); federated multi-KB search requires the caller to be independently authorized against each KB's own ACL layer, with the same pre-filter applied per KB before merge |

---

## 12. Freshness Management

### Incremental sync scheduling

Driven by the Freshness Manager, which owns a per-source **sync policy** (cadence, priority tier) informed by the source-class SLA table below, and which schedules Connector Service work accordingly (webhook-backed sources are event-driven rather than polled, but the Freshness Manager still tracks their *last observed event time* to detect a silently-dead webhook subscription — see staleness detection).

| Source class | Freshness SLA (P50 / P99) | Mechanism |
|---|---|---|
| Slack | 30s / 2 min | Real-time webhook |
| Confluence, SharePoint, Drive | 1 min / 15 min | Webhook + short-interval poll fallback |
| S3 (event-driven) | 1 min / 10 min | S3 event notifications |
| Databases (CDC-capable) | 2 min / 15 min | Change-data-capture stream |
| Databases (polling only) | 5 min / 30 min | Watermark polling |
| Generic REST APIs | 15 min / 1 hr | Scheduled poll |
| Web crawl | 4 hr / 24 hr | Scheduled re-crawl |
| Bulk/nightly exports | 24 hr / 48 hr | Scheduled batch |

### Staleness detection

Two independent signals, because a connector can be "running" but not actually keeping up:

1. **Sync health**: is the connector successfully completing sync cycles (§4.4 lifecycle state)? Tracked per source, alerts on DEGRADED/PAUSED beyond a grace period.
2. **Data staleness**: `now() - last_successful_sync_covering_this_document`, tracked **per document**, not just per source — a source can be "healthy" in aggregate while a specific document silently failed every retry and sits in the DLQ (§5.5) for days. The platform surfaces both a source-level staleness percentile (P50/P99 age of "last confirmed fresh" across all documents in the source) and a count of documents exceeding the SLA threshold.

For webhook-backed sources specifically, staleness detection also catches the classic silent failure mode — a webhook subscription that quietly expired or got unregistered at the source, after which the connector looks "idle" (no errors, because nothing is arriving) rather than "failing." This is caught by comparing observed event rate against the source's historical baseline rate and alerting on an anomalous drop, plus the reconciliation poll (below) which would eventually catch the drift anyway but on a much longer cycle.

**SLA compliance, computed concretely.** The staleness dashboard doesn't just show "P99 age" as an abstract number — it computes compliance against the source-class SLA table directly, so a violation is unambiguous rather than requiring interpretation:

```
Source: Slack (#eng-incidents), SLA: P50 ≤ 30s, P99 ≤ 2min

Sample of 10,000 documents' (now() - last_successful_sync) at query time:
  P50 = 22s   → within 30s SLA ✓
  P90 = 58s
  P99 = 3m 40s → EXCEEDS 2min SLA ✗  → staleness alert fires

Root-cause breakdown (what the dashboard shows next, not just the
violation itself):
  - 9,847 of 10,000 docs (98.5%) synced within SLA — the webhook path
    is healthy for the vast majority
  - 153 docs (1.5%) are the P99 tail — cross-referencing against the
    DLQ (§5.5) shows 140 of these hit a transient Slack API 429 during
    a burst and are retrying with backoff (self-healing, not urgent)
  - 13 docs show no DLQ entry at all — these are the actionable signal:
    likely a webhook delivery gap the reconciliation poll hasn't yet
    caught, worth a targeted re-sync rather than waiting for the next
    scheduled reconciliation pass
```

This breakdown — SLA violation *and* the decomposition into "self-healing tail" vs. "actually stuck" — is what turns a staleness alert into something actionable within minutes rather than triggering a broad, low-signal investigation every time the P99 twitches.

### Freshness scoring in retrieval

Every chunk's response includes `last_synced_at` and a derived `freshness_score` (0-1, decayed by source-class-appropriate half-life — a Slack message decays much faster than a policy PDF):

```python
def freshness_score(last_synced_at: datetime, source_class: str) -> float:
    half_life_hours = FRESHNESS_HALF_LIFE[source_class]  # e.g. slack=72h, wiki=720h, policy_pdf=2160h
    age_hours = (now() - last_synced_at).total_seconds() / 3600
    return 0.5 ** (age_hours / half_life_hours)
```

This is exposed two ways: (1) as a **ranking signal** — KBs can configure a small freshness boost blended into final ranking (default off, opt-in, since "freshest" and "most relevant" aren't the same axis and conflating them by default would be a silent quality regression); (2) as a **hard filter** — freshness-sensitive applications (e.g., "current on-call runbook") can require `freshness_score > threshold` or `last_synced_at > cutoff`, excluding stale results outright rather than merely deprioritizing them.

### Tombstone handling

A document deleted, moved out of scope, or unshared at the source produces a **tombstone event**, which must stop that document's chunks from being retrievable **before** the corresponding physical deletion completes across all replicas (tombstone-first, physical-delete-eventually — the same pattern as the KV store's design for handling deletes under replication):

```
Source delete/unshare event
      │
      ▼
Connector emits Tombstone(doc_id, kb_id, reason, ts)
      │
      ▼
Tombstone Propagator writes a tombstone marker to:
  1. Vector Store metadata (chunk_id → tombstoned=true) — checked as an
     implicit filter on EVERY query, same code path as permission
     pre-filtering, so a tombstoned chunk is excluded with the same
     "can't accidentally forget to check it" guarantee (§11.3's pattern
     reused, not a separate ad hoc check)
  2. Permission Index (zero-grantee, belt-and-suspenders with #1)
      │
      ▼
Async physical deletion job removes the chunk's vectors, text, and
metadata from all shards/replicas within a bounded window (target: 24h),
independent of the tombstone's immediate query-time effect above.
```

The tombstone marker is what gives the ≤5-minute permission-revocation bound (§11.4) its teeth — it's a fast, cheap, universally-checked flag, decoupled from the slower, heavier physical-deletion cleanup job.

### Source-of-truth reconciliation

Incremental sync (webhooks, delta tokens, CDC) is fast but not infallible — a missed webhook, a connector bug, clock skew on a watermark, or an IdP outage during group resolution can all cause silent drift. A **periodic full reconciliation** (cadence per source class: daily for high-velocity sources, weekly for slow ones) re-walks the source's complete document and ACL listing and diffs against the platform's current state:

- Documents present at source but missing/stale in the platform → re-ingested.
- Documents present in the platform but absent at source → tombstoned (catches missed delete events).
- ACL mismatches → re-synced, with the source always winning (platform state is corrected to match, never the reverse).

Reconciliation runs are logged with a diff summary (`N documents re-ingested, M tombstoned, K ACL corrections`) surfaced on the KB's health dashboard — a reconciliation run that finds a large diff is itself a signal the incremental sync mechanism for that source needs investigation, not just a routine cleanup.

**Cost of full reconciliation, worked**, for why the cadence table (daily/weekly) isn't chosen arbitrarily:

```
Confluence source, 500,000 pages, daily full reconciliation:
  Full listing API calls: 500,000 pages ÷ 100 pages/call ≈ 5,000 calls
  Against a 1,000 req/hour budget (§4.5): 5,000 ÷ 1,000 ≈ 5 hours just
  to LIST — before diffing or re-fetching anything that changed.

  This is the concrete reason reconciliation cadence is source-size-
  aware, not a flat "daily for everyone" policy: a 500k-page space
  running daily reconciliation would consume 5 of Confluence's 24
  hourly rate-limit windows just enumerating, competing directly with
  the webhook-driven incremental path's own budget (§4.5's shared
  token bucket) and risking the exact freshness SLA reconciliation
  exists to protect.

  Platform policy: reconciliation cadence scales inversely with source
  size above a threshold — sources under ~50,000 documents reconcile
  daily; larger sources reconcile weekly, with the listing calls
  scheduled during the source's lowest-traffic window (learned from
  the source's own request-volume history) to minimize contention with
  live incremental sync.
```

### Eventual consistency model, stated precisely

| Aspect | Consistency | Bound |
|---|---|---|
| Content freshness | Eventually consistent | Per source-class SLA table above |
| Permission grants (new access) | Eventually consistent | Same as content freshness — no urgency asymmetry, granting access late is a usability issue not a security one |
| Permission revocations | Bounded eventual consistency, **fail-closed** | ≤5 min via tombstone/ACL-sync pipeline, ≤60s via permission cache bound (§11.4) — whichever catches it first |
| Deletions (tombstone → physically gone) | Eventually consistent, tombstone effect is immediate-ish | Tombstone flag: same bound as revocation above; physical purge: ≤24h, no query-visible effect |

---

## 13. Evaluation

### Retrieval metrics

| Metric | Definition | Use |
|---|---|---|
| **Recall@k** | Fraction of relevant documents (per golden judgment) found in the top-k results | Primary "did we find the right stuff" metric |
| **Precision@k** | Fraction of top-k results that are relevant | Complements recall; matters more when top-k is small and shown directly to users |
| **MRR (Mean Reciprocal Rank)** | Average of `1/rank` of the first relevant result across queries | Rewards getting *a* good answer near the top, not just somewhere in top-k |
| **NDCG@k** | Rank-and-relevance-weighted metric using graded (not just binary) relevance judgments | Best single summary metric when golden judgments are graded (0-3 relevance) rather than binary |

### Worked example

A concrete pass through the arithmetic, not just the definitions, for one query against a golden judgment set with `k=5`:

```
Query: "how do we roll back a failed payments deploy?"
Golden relevant chunks (graded): {ch_a1: 3, ch_a2: 2, ch_a3: 1}
Retrieved top-5, in rank order: [ch_x9, ch_a1, ch_a4, ch_a2, ch_z2]

Recall@5    = |{a1, a2} found| / |{a1, a2, a3}| = 2/3 ≈ 0.667
Precision@5 = |relevant in top-5| / 5 = 2/5 = 0.4
MRR         = 1 / rank_of_first_relevant = 1/2 = 0.5   (a1 is the first relevant hit, rank 2)

NDCG@5:
  DCG  = Σ (2^rel_i - 1) / log2(rank_i + 1)
       = (2^3-1)/log2(3) + (2^2-1)/log2(5)     # a1 at rank2, a2 at rank4
       = 7/1.585 + 3/2.322 ≈ 4.416 + 1.292 ≈ 5.708
  IDCG = ideal ordering [a1(3), a2(2), a3(1), -, -]
       = (2^3-1)/log2(2) + (2^2-1)/log2(3) + (2^1-1)/log2(4)
       = 7/1 + 3/1.585 + 1/2 ≈ 7 + 1.893 + 0.5 ≈ 9.393
  NDCG@5 = DCG/IDCG ≈ 5.708/9.393 ≈ 0.608
```

This single query already shows why NDCG is the platform's primary gating metric rather than recall alone: recall@5 (0.667) looks fine, but NDCG@5 (0.608) correctly penalizes the fact that the *most* relevant document (`a1`, grade 3) wasn't ranked first — something recall@k structurally cannot see, since it only asks "is it in the top-k," never "is it near the top of the top-k."

### End-to-end RAG metrics

| Metric | Definition | How measured |
|---|---|---|
| **Faithfulness** | Is every claim in the generated answer supported by the retrieved context? | LLM-as-judge: decompose answer into claims, check each against retrieved chunks, score = supported_claims / total_claims |
| **Answer relevance** | Does the answer actually address the query? | LLM-as-judge scoring, or reverse-engineer candidate questions the answer would answer and compare embedding similarity to the actual query |
| **Context relevance / precision** | Of the chunks retrieved, how many were actually used/useful in the answer? | LLM-as-judge per-chunk relevance labeling against the query |
| **Hallucination rate** | Fraction of generated claims with no support in context AND not verifiable as general knowledge | Faithfulness's complement, tracked separately because "unfaithful but true" and "unfaithful and false" have very different severity |

### Golden dataset management

```json
{
  "eval_dataset_id": "ds_eng-wiki-v3",
  "kb_id": "eng-wiki",
  "created_from": ["manual_curation", "query_log_mining", "user_feedback"],
  "examples": [
    {
      "query": "how do we roll back a failed payments deploy?",
      "relevant_chunk_ids": ["ch_a1", "ch_a2"],
      "relevance_grades": {"ch_a1": 3, "ch_a2": 2},
      "source": "human_labeled",
      "labeled_by": "eng-platform-team",
      "labeled_at": "2024-07-01"
    }
  ],
  "version": 3,
  "size": 850
}
```

- **Seeding**: platform tooling mines query logs, surfaces high-volume and zero-result queries, and pairs them with candidate relevant documents (via existing retrieval results plus thumbs-up/down feedback signals) for human review — this dramatically cuts the cold-start cost of building a golden set from scratch.
- **Versioning**: golden datasets are versioned; an eval run always records which dataset version it ran against, so a metric trend line is never silently comparing apples to oranges after a dataset edit.
- **Maintenance**: datasets are living artifacts — flagged for review when the underlying documents they reference are edited/deleted (a relevance judgment about a now-changed document may no longer hold).

### Automated evaluation pipelines

```
On-demand (CI-style):
  Chunking/embedding/reranking config change proposed
       │
       ▼
  Eval Runner executes the KB's golden dataset against BOTH the current
  and proposed configuration (using a shadow index for the proposed
  config — no production traffic impact)
       │
       ▼
  Diff report: recall@k, NDCG, MRR delta; regressions flagged if any
  metric drops beyond a configured tolerance (e.g., >2% relative)
       │
       ▼
  Gate: config change requires explicit approval to ship if regressed

Scheduled (regression detection):
  Nightly eval run against production index for every KB with a golden
  dataset, trend stored and graphed — catches slow drift (e.g., corpus
  growth diluting recall, an embedding model degrading, a connector
  silently dropping documents) that a point-in-time eval wouldn't.
```

### LLM-as-judge for quality

Faithfulness/relevance/hallucination metrics (§13.2) are expensive to hand-label at scale, so a calibrated LLM judge is used, with two safeguards against judge unreliability:

1. **Calibration**: the judge's scores are periodically validated against a smaller human-labeled sample (e.g., 100 examples/month); if judge-human agreement drops below a threshold (e.g., Cohen's kappa < 0.6), the judge prompt/model is flagged for review before its scores are trusted for gating decisions.
2. **Structured rubrics, not free-form scoring**: the judge is prompted with an explicit decomposition (list claims → check each against provided context → aggregate), not asked for a bare 1-10 "quality" number — this is both more reproducible and more debuggable when a score looks wrong.

Concrete faithfulness-judging prompt structure (simplified):

```
SYSTEM: You are evaluating whether an AI-generated answer is faithful to
its provided source context. Follow these steps exactly:

1. Decompose the ANSWER into a numbered list of atomic factual claims.
2. For each claim, search the CONTEXT for direct support. Label each claim:
   - "supported": context directly states or clearly implies this claim
   - "unsupported": context does not address this claim at all
   - "contradicted": context states something that conflicts with this claim
3. Output a JSON object: {"claims": [{"text": ..., "label": ...,
   "supporting_chunk_id": ... or null}], "faithfulness_score":
   supported_count / total_claims}

Do not use outside knowledge to judge support — a true claim not found in
CONTEXT is "unsupported," not "supported."

QUERY: {query}
CONTEXT: {retrieved_chunks_with_ids}
ANSWER: {generated_answer}
```

The explicit instruction *not* to use outside knowledge is the single most important line in the prompt — without it, capable judge models default to fact-checking against their own training knowledge, which measures "is this answer true" (a different, also useful, but distinct question) rather than "is this answer grounded in what was actually retrieved," which is the metric that tells the platform whether *retrieval* did its job.

### A/B testing retrieval strategies

For KBs with enough query volume to support it, the Retrieval Service supports **traffic splitting** on a config axis (chunking strategy, embedding model, hybrid alpha, reranker on/off) with:

- Consistent bucketing per user (a user doesn't flip between variants mid-session).
- Online metrics: click-through on returned citations, thumbs feedback, downstream answer-accepted signals where the calling application reports them back.
- Combined with the offline golden-dataset eval (§13.4) as a pre-flight gate — A/B testing validates real-world behavior the golden dataset might not anticipate (genuine query distribution shift), it doesn't replace offline eval as the first gate.

---

## 14. Data Models

```
KnowledgeBase
  id, name, owning_team, created_at
  default_chunking_strategy, default_embedding_model_id
  index_type (hnsw | ivf_pq), shard_count
  retention_policy, access_policy_ref

DataSource
  id, kb_id, source_type (confluence|drive|sharepoint|slack|s3|db|api|web)
  config (scope, credentials_ref, parsing_hints)
  sync_policy (cadence, priority_tier)
  connector_state (PENDING_AUTH|BACKFILLING|ACTIVE|DEGRADED|PAUSED|DISABLED)
  checkpoint (cursor, last_success_at, consecutive_failures)

Document
  id, kb_id, source_id, source_doc_ref
  content_hash, near_dup_simhash, canonical_document_id (nullable)
  metadata (title, author, created_at, modified_at, source_url,
            content_type, language, labels)
  source_acl_ref
  version, last_synced_at
  status (active | tombstoned)

Chunk
  id, document_id, parent_chunk_id (nullable)
  text, section_path, char_start, char_end, page_number, chunk_index
  chunking_strategy, language, token_count
  status (active | tombstoned)

Embedding
  chunk_id, model_id, vector, dimension
  created_at
  # one Chunk can have multiple Embedding rows during model migration (§7.4)

PermissionGrant
  doc_id, principal_type (user|group), principal_id, level (read|write)
  synced_at, source_acl_ref

EvalDataset
  id, kb_id, version, examples[] (query, relevant_chunk_ids, relevance_grades)
  created_from, size

EvalResult
  id, eval_dataset_id, eval_dataset_version, run_at
  config_snapshot (chunking, embedding_model, hybrid_alpha, reranker)
  metrics (recall_at_k, precision_at_k, mrr, ndcg, faithfulness, ...)
  compared_to_result_id (nullable, for diff reports)
```

### Entity relationships

```
KnowledgeBase (1) ──── (N) DataSource
      │                        │
      │                        │ produces
      │                        ▼
      │                  Document (N) ──tombstoned/canonical──▶ Document
      │                        │
      │                        │ produces
      │                        ▼
      │                  Chunk (N) ──parent_chunk_id──▶ Chunk (self-referential,
      │                        │                          parent-child, §6.5)
      │                        │ has
      │                        ▼
      │                  Embedding (N per Chunk — one per active model_id
      │                        │       during a migration window, §7.4)
      │                        │
      │                  PermissionGrant (N per Document, via source_acl_ref)
      │
      └──── (N) EvalDataset ──── (N) EvalResult
```

The two self-referential edges — `Document.canonical_document_id` (deduplication, §5.4) and `Chunk.parent_chunk_id` (parent-child chunking, §6.5) — are both nullable foreign keys back into their own table, which is why both dedup resolution and parent-expansion at query time are cheap single-hop lookups rather than requiring a separate join table.

### Representative payloads

```json
// KnowledgeBase
{
  "id": "kb_eng-wiki",
  "name": "Engineering Wiki",
  "owning_team": "eng-platform",
  "created_at": "2024-01-15T00:00:00Z",
  "default_chunking_strategy": "structure_aware",
  "default_embedding_model_id": "bge-large-en-v2",
  "index_type": "hnsw",
  "shard_count": 1,
  "chunk_count": 840000,
  "retention_policy": {"tombstone_purge_days": 1},
  "custom_schema_fields": ["severity", "team_owner"]
}

// DataSource
{
  "id": "src_confluence-eng",
  "kb_id": "kb_eng-wiki",
  "source_type": "confluence",
  "connector_state": "ACTIVE",
  "checkpoint": {
    "cursor": "opaque-delta-token-abc123",
    "last_success_at": "2024-08-12T14:40:00Z",
    "consecutive_failures": 0
  },
  "sync_policy": {"mechanism": "webhook", "reconciliation_cadence": "hourly"}
}

// Document
{
  "id": "doc_7c31a9",
  "kb_id": "kb_eng-wiki",
  "source_id": "src_confluence-eng",
  "content_hash": "sha256:9f2a...",
  "near_dup_simhash": "3a7fe21c9b8d0044",
  "canonical_document_id": null,
  "metadata": {
    "title": "Q3 Incident Postmortem: Payments Outage",
    "language": "en",
    "content_type": "postmortem",
    "modified_at": "2024-08-12T14:32:00Z"
  },
  "source_acl_ref": "confluence:space:ENG:page:12345",
  "version": 4,
  "status": "active"
}

// Chunk
{
  "id": "ch_9f2a41",
  "document_id": "doc_7c31a9",
  "parent_chunk_id": "ch_88b1c2",
  "section_path": "Incident Postmortem > Timeline > 14:32 UTC — Alert fired",
  "token_count": 218,
  "chunking_strategy": "structure_aware_v2",
  "status": "active"
}
```

---

## 15. Sequence Flows

### 15.1 End-to-end ingestion: a Confluence page edit becomes retrievable

```
User edits a Confluence page
      │
      ▼
Confluence fires a page-updated webhook ────────────────────────┐
      │                                                          │
      ▼                                                          │ (if webhook lost)
Connector Service (Confluence worker) receives webhook,          │
validates signature, enqueues RawDocument fetch job              │
      │                                                          │
      ▼                                                          │
Connector fetches full page content + ACL via Confluence API,    │
respecting the source's rate-limit token bucket (§4.5)           │
      │                                                          │
      ▼                                                          │
RawDocument{source_doc_ref, content, acl, fetched_at} enqueued   │
onto the Ingestion Pipeline's durable queue                      │
      │                                                          │
      ▼                                                          │
Ingestion workflow (Temporal) starts, keyed on                   │
(source_id, doc_id, content_hash) for idempotency                │
      │                                                          │
      ├─▶ Parse (HTML → structured text)                         │
      ├─▶ Metadata extraction (title, author, timestamps, url)   │
      ├─▶ Language detection                                      │
      └─▶ Dedup check (content hash against existing hash index) │
              │ not a duplicate                                   │
              ▼                                                   │
      Document Store write (new version of doc_7c31...)          │
              │ emits DocumentUpdated event                       │
              ▼                                                   │
      Chunking Engine picks up event, applies the source's        │
      configured strategy (structure-aware for Confluence HTML)   │
              │                                                    │
              ▼                                                    │
      New/changed Chunk records written, old chunk_ids for this   │
      doc version diffed against previous version (unchanged      │
      chunks are NOT re-embedded — see below)                      │
              │                                                    │
              ▼                                                    │
      Embedding Service embeds only the NET-NEW/changed chunks     │
      (incremental lane, high priority) — reuses embeddings for    │
      byte-identical chunk text carried over from the prior        │
      version, a meaningful cost saving on typo-fix-scale edits    │
              │                                                    │
              ▼                                                    │
      Vector Store write: new/updated vectors indexed on the       │
      shard(s) owning this KB; old chunk_ids belonging to the      │
      previous doc version but absent from the new version are     │
      tombstoned (§12.4)                                            │
              │                                                    │
              ▼                                                    │
      Permission Sync (parallel, triggered by the same RawDocument │
      event) updates PermissionGrant rows from the fetched ACL      │
              │                                                    │
              ▼                                                    │
      Chunk is now retrievable, with last_synced_at = now()        │
              │                                                    │
              ▼                                                    │
      Elapsed time budget: this path targets P50 ≤ 1 min,          │
      P99 ≤ 15 min end-to-end (§ task NFRs) ◀─────────────────────┘
                                              hourly reconciliation
                                              poll (§12.5) is the
                                              backstop if the webhook
                                              path above is silently
                                              dropped
```

### 15.2 End-to-end query: a permission-filtered, reranked retrieval call

```
Application backend calls Retrieval API on behalf of "alice"
      │
      ▼
Retrieval Service receives request, validates service token,
extracts principal_id=alice, resolves kb_id="eng-wiki"
      │
      ├──────────────────────────────┐
      ▼                               ▼
Permission Service:                Query Understanding:
  - check allow-set cache            - (optional) multi-query
    for (alice, eng-wiki)              expansion or HyDE
  - cache miss → resolve             - embed query text
    alice's groups via IdP,            (cache-checked, §9.5)
    build allow-set bitset,
    cache w/ 60s TTL
      │                               │
      └───────────────┬───────────────┘
                       ▼
        Hybrid search fan-out to KB "eng-wiki" shard(s):
          - dense ANN search, pre-filtered by allow_set bitset
          - sparse BM25 search, pre-filtered by allow_set bitset
          - per-shard timeout 150ms
                       │
                       ▼
        Result merge across shards (score normalization) +
        RRF fusion of dense/sparse lists
                       │
                       ▼
        Residual metadata filters applied (date range,
        content_type — non-permission filters, §9.4)
                       │
                       ▼
        Secondary permission check (§11.3 step 3, defense in
        depth — bitset membership test on every surviving
        candidate; anomaly-log + drop if any fail, should never
        fire)
                       │
                       ▼
        rerank=true? ──yes──▶ Reranking Service: cross-encoder
          │                    scores top-100 candidates (batched,
          │                    GPU), returns top_k reordered
          no
          │
          ▼
        expand_to_parent=true? ──yes──▶ fetch ParentChunk text
          │                              for each surviving child
          no                             chunk (§6.5)
          │
          ▼
        MMR diversity pass (if requested), freshness metadata
        attached per result (§12.3)
                       │
                       ▼
        Response assembled: chunk text, scores, citation,
        freshness_score, next_cursor
                       │
                       ▼
        query_id logged for feedback loop (§13.4) and query-log
        mining (§13.3 golden dataset seeding)

Latency budget (P99): permission resolution ≤15ms (cached) or
~40ms (cold) │ hybrid search ≤300ms │ rerank +150-250ms if used
```

---

## 16. API Design

### Knowledge base management: schema, analytics, and lifecycle

Before the endpoint list, three pieces of Knowledge Base Management deserve their own treatment, since they're easy to under-specify as "just CRUD."

**Schema registry.** Every KB can declare custom metadata fields beyond the platform-standard set (title, author, timestamps, source), which become first-class, typed, indexed retrieval filters:

```json
PATCH /v1/knowledge-bases/{kb_id}/schema
{
  "custom_fields": [
    {"name": "severity", "type": "enum", "values": ["sev1", "sev2", "sev3", "sev4"]},
    {"name": "team_owner", "type": "string", "indexed": true},
    {"name": "resolved_at", "type": "timestamp", "nullable": true}
  ]
}
```

Field values are populated via connector `parsing_hints` (§4's config examples — e.g., the database connector's SELECT can map a `status` column directly to a custom field) or via a post-ingestion enrichment hook for sources that don't carry the field natively. Schema changes are additive-only through this API (removing a field that's in active use as a retrieval filter is a breaking change gated behind an explicit deprecation window, not an in-place delete) — this mirrors how a shared platform must treat schema evolution more conservatively than a single application would.

**Usage analytics.** Beyond the health dashboard (§17), the analytics endpoint answers questions the owning team actually asks week to week:

```json
GET /v1/knowledge-bases/{kb_id}/analytics?window=7d

{
  "query_volume": {"total": 842000, "daily_avg": 120285},
  "latency": {"p50_ms": 76, "p99_ms": 264},
  "top_queries": [
    {"query": "payments rollback procedure", "count": 1240, "avg_score": 0.81},
    {"query": "vpn setup instructions", "count": 980, "avg_score": 0.74}
  ],
  "zero_result_queries": [
    {"query": "kubernetes cost allocation dashboard", "count": 34}
  ],
  "retrieval_quality_trend": {
    "ndcg_at_10": [0.71, 0.72, 0.70, 0.73, 0.74, 0.73, 0.75],
    "dataset_version": "ds_eng-wiki-v3"
  },
  "cost_attribution": {
    "embedding_usd": 42.10, "storage_usd": 118.00, "query_compute_usd": 205.50
  },
  "feedback": {"thumbs_up": 3120, "thumbs_down": 410}
}
```

`zero_result_queries` and `top_queries` are the two fields owning teams look at most in practice — zero-result queries are the single best signal for "what content is missing," directly actionable as a connector-scope or chunking-strategy fix, and are also the primary feed into golden-dataset mining (§13.3).

**Deletion cascade.** Deleting a knowledge base is not a single operation — it fans out across every derived store, and the ordering matters for correctness (permissions must be the *first* thing to go, not the last, so a deletion-in-progress KB never has a window where content is un-tombstoned but permissions have already been dropped):

```
DELETE /v1/knowledge-bases/{kb_id}
  1. KB marked `status: deleting` in the Control Plane (immediately
     blocks new queries — return 404, not a partially-served result)
  2. Permission Index: all grants for this KB's documents purged
  3. Vector Store: all shards for this KB scheduled for teardown
  4. Document Store: all documents for this KB tombstoned, then
     physically purged on the standard retention timer
  5. Connector configs disabled and credentials revoked from the vault
  6. Eval datasets and results archived (not deleted — retained for
     audit/analytics even after the KB itself is gone) unless the
     request explicitly opts into full purge
  7. KB record itself removed from the Control Plane once steps 2-5
     confirm complete
```

Step 1's immediate query-blocking is deliberate: a KB mid-deletion is a state where "is this chunk still authorized" cannot be trusted, so the safe default is to stop serving it entirely rather than risk a half-torn-down KB serving stale-permission results during the teardown window.

### Endpoint reference

```
POST   /v1/knowledge-bases
GET    /v1/knowledge-bases/{kb_id}
PATCH  /v1/knowledge-bases/{kb_id}
DELETE /v1/knowledge-bases/{kb_id}

POST   /v1/knowledge-bases/{kb_id}/sources
GET    /v1/knowledge-bases/{kb_id}/sources/{source_id}
PATCH  /v1/knowledge-bases/{kb_id}/sources/{source_id}
DELETE /v1/knowledge-bases/{kb_id}/sources/{source_id}
POST   /v1/knowledge-bases/{kb_id}/sources/{source_id}/sync   # trigger manual sync

GET    /v1/knowledge-bases/{kb_id}/documents/{document_id}
GET    /v1/knowledge-bases/{kb_id}/documents/{document_id}/chunks
DELETE /v1/knowledge-bases/{kb_id}/documents/{document_id}    # manual removal

GET    /v1/knowledge-bases/{kb_id}/analytics
GET    /v1/knowledge-bases/{kb_id}/health   # sync status, staleness, DLQ counts
```

### Retrieval

```http
POST /v1/knowledge-bases/{kb_id}/retrieve
Content-Type: application/json
Authorization: Bearer <service-token>
X-Principal-Id: alice@company.com
X-Principal-Groups: eng-all,eng-platform   # or resolved server-side from token

{
  "query": "how do we roll back a failed payments deploy?",
  "top_k": 10,
  "similarity_threshold": 0.7,
  "hybrid_alpha": 0.6,
  "filters": {
    "source": ["confluence", "slack"],
    "created_after": "2024-01-01T00:00:00Z",
    "content_type": ["runbook", "postmortem"]
  },
  "rerank": true,
  "diversity": { "mmr_lambda": 0.7 },
  "expand_to_parent": true,
  "min_freshness_score": 0.3
}
```

```json
{
  "results": [
    {
      "chunk_id": "ch_9f2a...",
      "text": "To roll back a failed payments deploy, run `deploy rollback payments-api`...",
      "score": 0.89,
      "dense_score": 0.91,
      "sparse_score": 0.78,
      "rerank_score": 0.94,
      "document": {
        "id": "doc_7c31...",
        "title": "Payments Deploy Runbook",
        "source_url": "https://company.atlassian.net/wiki/...",
        "source": "confluence"
      },
      "section_path": "Payments Deploy Runbook > Rollback Procedure",
      "last_synced_at": "2024-08-12T14:35:00Z",
      "freshness_score": 0.87
    }
  ],
  "next_cursor": "opaque_token...",
  "query_id": "qr_a83f...",
  "index_version": "eng-wiki-v2-20240812"
}
```

### Evaluation

```
POST /v1/knowledge-bases/{kb_id}/eval-datasets
POST /v1/knowledge-bases/{kb_id}/eval-datasets/{ds_id}/examples
POST /v1/knowledge-bases/{kb_id}/eval-runs        # body: config_snapshot, dataset_id
GET  /v1/knowledge-bases/{kb_id}/eval-runs/{run_id}
GET  /v1/knowledge-bases/{kb_id}/eval-runs?compare_to={run_id}
```

Feedback loop (used to seed golden datasets and drive online A/B metrics):

```
POST /v1/knowledge-bases/{kb_id}/feedback
{
  "query_id": "qr_a83f...",
  "chunk_id": "ch_9f2a...",
  "signal": "thumbs_up" | "thumbs_down" | "cited_in_answer" | "clicked"
}
```

---

## 17. Observability and SLOs

### The handful of metrics that actually tell you the platform is healthy

| Metric | Why it matters | Alert threshold (indicative) |
|---|---|---|
| Retrieval API P50/P99 latency, per KB | Direct user-facing SLO (§ task NFRs) | P99 > 300ms unreranked, or > 700ms reranked, sustained 5 min |
| Retrieval API error rate, per KB | Availability SLO | > 0.1% over 5 min |
| Permission-check secondary-mismatch count (§11.3 step 3) | Should be **zero** in steady state; any nonzero value is a P0 | Any occurrence > 0 |
| Staleness: P50/P99 document age, per source | Freshness SLA compliance (§12) | Exceeds source-class SLA bound for > 30 min |
| Connector error rate / DEGRADED time, per source | Ingestion health | DEGRADED > 1h continuous |
| DLQ depth, per KB | Silent data loss risk | Growing DLQ depth over 24h, or depth > 0.1% of KB's document count |
| Embedding queue depth (bulk + incremental lanes separately) | Ingestion backpressure | Incremental lane depth > 10k for > 10 min (should drain fast) |
| Eval metric trend (recall@k, NDCG), per KB with a golden dataset | Slow quality regression detection (§13.4) | > 2% relative drop week-over-week |
| Vector store shard replica divergence (chunk count / checksum mismatch) | Data integrity | Any divergence detected on scheduled consistency check |
| Cache hit rate: permission allow-set cache, query-result cache, embedding cache | Cost + latency efficiency, not correctness | Informational; sustained drop worth investigating but not itself an incident |
| Reranking Service GPU utilization / queue depth | Capacity planning signal for §18's "reranking adoption" scaling risk | Utilization > 85% sustained, or queue wait > 100ms P99 |

### Proving guarantees hold in production, not just in design

The permission-leakage guarantee (§11.6) is the platform's highest-stakes claim, so it gets its own **continuous verification job**, not just point-in-time testing:

```
Continuous permission audit (runs every 15 min, samples live traffic):
  1. Sample N recent query_ids (from query logs, §15.2) across KBs.
  2. For each, independently re-resolve the querying principal's
     allow-set from the Permission Service (a *second*, offline
     computation path, not reusing the cached value the live query
     used) and re-derive PermissionGrant rows directly from source
     ACL data.
  3. Diff: every chunk_id actually returned in the logged response
     must be a member of the independently-recomputed allow-set.
  4. Any mismatch is a P0 page — not a metric, an incident.
```

This is the production analogue of "prove linearizability holds" in a consistency-critical store: a design argument (§11) is necessary but not sufficient — an always-on adversarial-style check against the platform's own live traffic is what makes the zero-leakage claim something ops can actually stand behind, not just something the design doc asserts.

Freshness SLA compliance is similarly audited: a scheduled job compares a random sample of documents' `last_synced_at` in the platform against a direct source-side fetch of that document's true last-modified time, flagging any sample exceeding the source class's stated bound — catching cases where the staleness *tracker itself* might be wrong (e.g., miscomputing an age) rather than only catching cases where the underlying sync is slow.

### Dashboards

- **Per-KB health**: connector status, staleness percentiles, DLQ depth, query latency/error rate, eval trend — the single view an owning team checks.
- **Platform capacity**: shard count/growth, GPU pool utilization (embedding + reranking), cost by component, noisy-neighbor incidents.
- **Security posture**: permission-audit pass rate (target 100%), revocation-latency distribution, fail-closed event count (documents currently unretrievable due to sync lag — should trend toward zero, not accumulate).

### Sample incident: permission-audit mismatch page

Walking through what the on-call response actually looks like when §17.2's continuous permission audit fires, to make "P0 page" concrete rather than aspirational:

```
ALERT: permission-audit-mismatch
  kb_id: kb_support-tickets
  query_id: qr_4f21a9
  principal: contractor-jane@vendor.com
  mismatched_chunk_id: ch_88c1
  detail: chunk WAS returned in live query response, but is NOT a member
          of the independently-recomputed allow-set for this principal

On-call response (first 15 minutes):
  1. Immediate containment: kb_support-tickets's Retrieval Service
     instance is NOT taken down (that would be a broader outage for a
     single confirmed mismatch) — instead, the specific principal's
     cached allow-set entry is force-invalidated, and a query-level
     circuit breaker is set requiring FRESH (non-cached) allow-set
     resolution for this (principal, kb) pair for the next hour.
  2. Root-cause triage: check whether ch_88c1's PermissionGrant row was
     recently modified (a sync race — ACL update landed between the
     live query's allow-set resolution and the independent re-check) vs.
     a structural bug (e.g., a bitset index-offset error in the pre-
     filter implementation itself).
  3. If sync race: this is a KNOWN acceptable window (§12.6's bounded
     eventual consistency) as long as it's within the 60s cache /
     5min tombstone bounds — confirm the timestamps, downgrade from P0
     to a logged-and-tracked event, no code change needed.
  4. If structural bug: this is a genuine P0 — the query path itself is
     capable of leaking. Immediate mitigation: disable pre-filter fast
     path platform-wide, fall back to a slower but independently-
     re-verified filtering path (§11.3 step 3's secondary check,
     promoted to primary) until the bitset bug is found and fixed.
```

The branch between step 3 and step 4 is the entire point of running the audit continuously rather than trusting the design argument alone — it's what lets the platform tell the difference between "the eventual-consistency window worked as designed" and "the zero-leakage guarantee has an actual bug," which look identical from a single alert firing but demand completely different responses.

### Chaos testing the permission guarantee

The continuous audit (§17.2) catches leakage in live, organic traffic, but it only samples traffic that's actually happening — it won't necessarily exercise the specific edge cases (deep group nesting, mid-flight revocation, sharded-KB fan-out) where a bug is most likely to hide. A scheduled **synthetic chaos suite** complements it by deliberately manufacturing those edge cases:

| Scenario | What it injects | What must hold |
|---|---|---|
| Revoke-mid-query | A synthetic principal's grant is revoked at the source at the exact moment a query against a large KB is executing (query start before revocation, response after) | The response reflects either fully-pre-revocation or fully-post-revocation state — never a partial mix of chunks from two different permission snapshots within one response |
| Nested-group depth exhaustion | A synthetic group hierarchy nested exactly at the 10-level bound (§11.3) plus one level beyond it | Depth-11 grants are correctly excluded (logged as a depth-exceeded event, §11.3), not silently included via a truncation bug |
| IdP timeout injection | Group-expansion calls are made to fail/time out for a synthetic principal on a schedule | Fail-closed behavior (§11.3's worked example) holds exactly, verified by re-running the same query with the IdP healthy and diffing — the healthy result must be a **superset** of the degraded result, never a mismatched set |
| Cross-shard leak probe | A synthetic KB is sharded across N shards with a permission grant deliberately placed so only one shard holds the authorizing document | Verifies the pre-filter bitset is correctly applied per-shard during fan-out (§9.3), not just at a merge step that would be too late to prevent an unauthorized shard-local candidate from ever being computed |
| Federated fan-out probe | A synthetic principal authorized for KB A but not KB B, querying both via §11.5's federation path | KB B's fan-out call returns empty, contributes nothing to the merged result, and produces no partial/leaked score signal from KB B in the final response |

Each scenario runs nightly against a dedicated synthetic KB (never against real tenant data) and any failure is a release-blocking signal for the next deploy, not just a dashboard entry — this is the platform's answer to "how do you know the zero-leakage guarantee still holds after every code change," since the continuous production audit alone only proves it held for the traffic that happened to occur, not for the traffic patterns a bug fix might have missed.

---

## 18. Scaling

### Horizontal scaling per component

| Component | Bottleneck first hit at scale | Scaling mechanism |
|---|---|---|
| Connector Service | Source API rate limits (external, not platform-controlled) | More parallel workers within the source's rate budget; cannot out-scale a slow source — freshness SLA is honestly capped by it |
| Ingestion Pipeline | Parsing CPU (large PDFs/OCR) | Horizontal worker pool, autoscaled on queue depth, isolated pool for OCR specifically |
| Chunking Engine | Semantic chunking's embedding-boundary pass | Horizontal, GPU-assisted for semantic strategy only; cheap strategies scale on plain CPU |
| Embedding Service | GPU throughput | Horizontal GPU pool, autoscaled on queue depth, bulk/incremental lane priority (§7.2) |
| Vector Store | Shard memory/CPU at large single-KB scale | Sharding (§8.5) + read replicas; large KBs get dedicated node pools |
| Retrieval Service | Fan-out latency to many shards on huge KBs | Stateless, scale replicas; colocate with vector store shards to minimize network hops |
| Reranking Service | GPU throughput at high `rerank:true` adoption | Horizontal GPU pool, same dynamic-batching approach as embedding |
| Permission Service | Group-expansion calls to IdP | Aggressive caching (§11.4) is the primary lever, not raw horizontal scaling — the IdP itself is the real bottleneck |

### Vector store scaling walkthrough

As a KB grows past the single-shard threshold (~25M vectors, §2):

1. Platform auto-detects the threshold crossing during a scheduled capacity check.
2. A resharding job is scheduled (off-peak), computing the new shard count `N = ceil(size / 25M)`.
3. New shards are populated by **replaying from the Document Store** (same mechanism as embedding migration, §7.4) rather than physically splitting the existing single shard — simpler, and doubles as a validation that the KB is fully reconstructable from source-of-truth.
4. Retrieval Service's shard map is updated atomically once the new shards pass a consistency check (chunk counts reconcile against the Document Store); old single shard decommissioned after a rollback window.

### Embedding throughput optimization

Beyond §7.3's dynamic/length-bucketed batching: for very large backfills (a new 200M-chunk KB), the Embedding Service supports a **priority-lowered bulk mode** that intentionally trades latency for GPU utilization — larger batches, longer max-wait-to-batch, and explicit admission control that caps bulk-mode's share of the GPU pool (e.g., 70% max) so it can never fully starve the incremental lane even during a massive concurrent backfill.

### Query fan-out

For multi-shard KBs, fan-out is parallel (not sequential) with a **per-shard timeout** shorter than the overall query budget (e.g., 150ms shard timeout inside a 300ms P99 query budget) — a single slow/unhealthy shard degrades that KB's recall slightly (missing one shard's candidates) rather than blowing the whole query's latency budget. This is a deliberate availability-over-completeness trade for the fan-out path specifically, distinct from the fail-closed posture on permissions (missing a shard loses candidate *recall*, it never *adds* unauthorized results, so it doesn't compromise the leakage guarantee).

### Where the platform hits limits first as it grows 10x

1. **The Permission Service's dependency on the IdP** — group expansion at 10x query volume needs either a much bigger IdP-side budget or a much longer effective cache TTL (which directly trades against the revocation-latency guarantee, §11.4) — likely the first real architectural tension to resurface.
2. **Reranking GPU capacity** if `rerank:true` adoption grows faster than raw query volume (a very plausible trend as more teams build quality-sensitive apps) — mitigated by the priority-lane and admission-control patterns already in place, but capacity planning needs to track adoption rate as its own metric, not just QPS.
3. **Source API rate limits** on the handful of highest-volume connectors (Slack, Confluence) as more KBs pull from the same shared org-wide source instance — mitigated by negotiating higher enterprise-tier API budgets and/or a shared, deduplicated cross-KB sync for documents multiple KBs would otherwise redundantly pull from the same source.

### Capacity planning cadence

Reactive autoscaling (queue-depth-triggered GPU scale-out, threshold-triggered resharding) handles day-to-day variance, but the three risks above are all **slow-building trends**, not sudden spikes — autoscaling alone won't surface them early enough to act on calmly. The platform runs a monthly capacity review that specifically tracks trend lines, not just current utilization: `rerank:true` adoption rate as a fraction of total query volume, per-source connector request volume against its negotiated rate-limit budget, and the Permission Service's cache-hit-rate trend (a slow decline signals query pattern diversity outpacing the cache's effectiveness, an early warning for the IdP-dependency risk above well before it becomes a P99 latency problem). Each trend has an explicit lead-time target — e.g., "renegotiate the Confluence API tier when projected demand crosses 70% of the current budget, not when it crosses 100%" — because renegotiating a source's rate-limit tier or provisioning additional GPU capacity are both multi-week lead-time actions that reactive, threshold-crossing alerts surface too late to act on gracefully.

---

## 19. Failure Modes

| Failure | Detection | Immediate effect | Recovery |
|---|---|---|---|
| Connector fails mid-sync | Elevated error rate → DEGRADED state (§4.4) | Partial batch isolated via per-doc DLQ; sync continues for unaffected documents | Automatic retry w/ backoff; reconciliation (§12.5) catches anything missed; alert if DEGRADED exceeds SLA window |
| Embedding model degrades (quality regression, e.g., a provider silently updates a hosted model) | Nightly eval run (§13.4) trend detects metric drop | New embeddings continue to be produced (no hard stop) but flagged | Pin to a known-good model version explicitly (hosted APIs are usually versioned for exactly this reason); trigger migration (§7.4) to a validated replacement if needed |
| Embedding provider outage (hosted API down) | Elevated error rate on Embedding Service calls | Incremental-lane embedding backs up in queue; bulk lane pauses | Circuit breaker trips, queue buffers (bounded, with alerting past a depth threshold); optional automatic failover to a secondary self-hosted model for the incremental lane only, with a note that mixed-model chunks get re-embedded with the primary once it recovers |
| Vector store shard corruption / loss | Replica consistency check (chunk count / checksum mismatch) fails | Queries route around the bad replica automatically (§8.4 replication) | Rebuild the shard from a healthy replica; if all replicas lost, rebuild from Document Store replay (§18.2) — slower, but always possible since the vector store is a derived index, never source of truth |
| Permission sync lag | Staleness tracker (§12.2) on the Permission Sync pipeline specifically | Fail-closed (§11.4) — affected documents become unretrievable rather than under-protected | Alert if lag exceeds the 5-min bound; investigate source-side (IdP outage?) vs. platform-side (Permission Sync worker backlog?) |
| Stale content served past SLA | Staleness tracker (§12.2) per-document metric | Freshness score/filter (§12.3) lets freshness-sensitive callers self-protect even before the platform-level alert fires | Prioritize the lagging source's sync; if source-side outage, communicate degraded freshness on the KB health dashboard |
| Runaway crawl / backfill in one KB | Per-KB rate limiting and quota enforcement on ingestion pipeline throughput | Contained to that KB's ingestion lane; query serving unaffected (decoupled planes, §3) | Automatic quota-based throttling; platform on-call notified if a KB's ingestion volume anomaly exceeds a threshold (possible runaway crawl or misconfiguration) |
| Query storm on one KB | Per-KB QPS quota + noisy-neighbor isolation (§18, dedicated node pools for large KBs) | Bounded to that KB's shard pool; other KBs' latency unaffected | Rate-limit/429 the offending caller past quota; scale that KB's replica count if the load is legitimate and sustained |
| Reranker unavailable | Health check / elevated error rate on Reranking Service | Retrieval Service falls back to un-reranked (RRF-only) results rather than failing the whole query | Alert; circuit breaker; queries automatically resume reranking once the service recovers |

### Two failure walkthroughs, traced end to end

The table above is the reference; two of its rows are worth walking through in full narrative detail, because they're the ones most likely to be under-specified in a design review.

**Walkthrough 1 — a vector store shard is lost.**

A node hosting one replica of shard 47 (part of a 500M-chunk KB) suffers a disk failure. Sequence of events:

1. Health checks on that replica start failing within seconds; the shard's other two replicas (different AZs, §8.4) continue serving all traffic for shard 47 — the Retrieval Service's shard map marks the dead replica unhealthy and routes exclusively to the surviving two. **No query-visible impact** at this point; this is exactly what 3x replication is for.
2. The platform's control plane detects the persistent replica failure (not a transient blip — health checks fail for longer than the flap-tolerance window, e.g., 2 minutes) and triggers a **rebuild**: a new node is provisioned, and the shard is repopulated from one of the two healthy replicas via a bulk copy (fast — this is the common case, most shard losses are single-replica).
3. Rebuild completes, new replica passes a consistency check (chunk count and a sampled checksum against a healthy replica), and rejoins the routing pool. Total time-to-full-redundancy: minutes to low tens of minutes depending on shard size, during which the KB ran at 2/3 replication — degraded durability margin, but zero degraded availability.
4. **The genuinely bad case**: a correlated failure takes out 2 of 3 replicas simultaneously (e.g., an AZ-wide event coinciding with the first failure). Now shard 47 is down to one replica — queries against it still succeed (one replica is enough to serve, just not to tolerate another failure), but the platform is one more failure away from actual data loss for that shard's chunk range. This state pages on-call immediately (not just logs a warning) and triggers an **emergency third-replica rebuild** at elevated priority, competing for rebuild bandwidth ahead of routine maintenance.
5. **The catastrophic case**: all three replicas are lost (extremely unlikely given AZ-spread, but the design must still answer it). Recovery falls through the backup tiers in order (§8.7): first, restore from the most recent nightly snapshot (minutes to low hours) if snapshots for that shard are intact and trusted; only if snapshots are also unavailable does the platform fall back to the slowest, always-correct path — rebuilding from the Document Store by replaying chunking + embedding for every document whose chunks lived on that shard (§18.2), hours rather than minutes since it re-runs the embedding step rather than copying vectors. Either way, the KB's owning team sees a partial-recall window for that shard's chunk range during the rebuild, surfaced explicitly on their health dashboard rather than silently degrading.

The throughline: every layer of this failure has a defined, bounded, non-silent response, and the worst case is bounded by "re-derive from source of truth," which is only possible because §3's architectural choice — vector store as a rebuildable derived index — was made deliberately up front, not discovered as a lucky accident during an incident.

**Walkthrough 2 — a knowledge base attempts a 50M-document backfill without warning.**

A team enables a new Confluence-wide connector scoped far more broadly than intended (a config mistake — `spaces: ["*"]` instead of a specific space list), effectively triggering a 50M-document full backfill against a KB provisioned for ~1M documents.

1. The Connector Service begins enumerating and fetching documents at the source's rate-limit ceiling (§4.5) — this alone bounds the ingestion rate regardless of platform capacity, so the "attack" is naturally rate-limited by Confluence's own API budget.
2. Documents flow into the Ingestion Pipeline and Chunking Engine; per-KB ingestion throughput quotas (§18) cap how fast this KB's chunks can be embedded and written to the vector store, so the backlog **queues** rather than consuming unbounded shared capacity — other KBs' ingestion is unaffected, per the noisy-neighbor isolation guarantee.
3. The KB's chunk count crosses its provisioned single-shard capacity threshold (§2/§8.5) mid-backfill. The platform's capacity monitor detects this and **automatically schedules a resharding job** rather than letting one shard silently balloon past its memory budget — this is the same mechanism as planned growth (§18.2), just triggered earlier and faster than expected.
4. The KB's ingestion volume anomaly (order-of-magnitude above its historical baseline) trips an alert to platform on-call *and* to the owning team, well before the backfill completes — this is the detection path from the failure-mode table's "runaway crawl" row, generalized: any KB whose ingestion rate deviates sharply from its own history is flagged, not just crawler-specific runaways.
5. The owning team is notified, confirms the config mistake, and scopes the connector down; already-ingested out-of-scope documents are removed via the standard document-deletion path (§15's tombstone flow), which also correctly retracts their chunks from the resharded index and their grants from the permission index — cleanup uses the same mechanism as a routine deletion, not a special-cased incident-response script.

The throughline here: nothing in this scenario required a human to intervene *before* damage was contained — quotas, rate limits, and anomaly detection did the containment; the human intervention (scoping down the connector) is about correctness of *intent*, not about stopping a system that would otherwise have kept degrading.

---

## 20. Cost Model

### Cost components

| Component | Driver | Approximate lever |
|---|---|---|
| Embedding (hosted API) | tokens embedded × per-token price | Dominant cost for KBs on hosted models at high chunk-turnover sources |
| Embedding (self-hosted) | GPU-hours | Dominant cost at very large steady-state scale; cheaper per-chunk at volume, higher fixed cost |
| Vector storage | bytes stored × replication factor | Dimensionality + quantization choice (§7.4, §8.2) is the single biggest lever |
| Query compute | QPS × (search cost + optional rerank cost) | Reranking is the biggest per-query cost differential — gating it (§10.4) is a direct cost lever, not just a latency one |
| Reranking (if hosted API) | reranked-query volume × per-call price | Self-hosting a reranker trades a fixed GPU cost for eliminating this variable cost past a break-even query volume |

### Concrete optimization levers

1. **Dimensionality reduction** (§7.4): dropping from 1536 to 768 dims roughly **halves vector storage** with ~1% recall loss for MRL-capable models — essentially free quality-for-cost trade for most KBs, applied as the platform default rather than an opt-in.
2. **Quantization** (§8.2): int8 scalar quantization gives another ~4x storage reduction on top, near-zero recall loss — also a default. PQ for the largest KBs trades more recall for much larger (8-32x) compression, applied selectively where the eval framework (§13) confirms the recall loss is acceptable for that KB's use case.
3. **Storage tiering**: chunks/embeddings for documents with `freshness_score` below a low threshold and no recent query hits (tracked via query-log chunk-hit analytics) are candidates for a colder, cheaper storage tier (higher-latency retrieval acceptable) — this targets the long tail of rarely-touched historical content that dominates storage but not query cost.
4. **Reranking gating** (§10.4): the single biggest per-query cost lever — steering low-stakes callers away from `rerank: true` via clear API guidance and per-KB eval-backed defaults, rather than reranking universally "just in case."
5. **Self-hosting break-even**: for a KB doing >~5M reranked queries/month, self-hosting a reranker on owned GPU capacity is typically cheaper than a hosted per-call API — the platform tracks per-KB rerank-call volume specifically to identify candidates for this migration.
6. **Batch-size discipline on embedding** (§7.3): maximizing batch fill (length-bucketing, dynamic batching) is a pure efficiency lever with no quality trade-off — treated as an SRE-owned optimization target, not a product decision.

### Platform-wide monthly cost estimate

Rough order-of-magnitude, built from the capacity numbers in §2, to sanity-check that the design is economically sane at stated scale (not a finance-grade estimate, but the arithmetic a design review should demand):

```
STORAGE (steady state, 10B chunks, quantized, 3x replication — §2, §20)
  ~75-90 TB across dense vectors + metadata + raw text
  @ ~$0.023/GB-month (object-store-class pricing for the bulk of it,
    a smaller hot fraction on faster disk) ≈ 80,000 GB × $0.023
  ≈ $1,840/month storage (dominated by the cold long-tail majority of
    chunks; the hot working set on faster media adds a few thousand
    more — call it $3,000-5,000/month all-in)

EMBEDDING (steady state ingest, 50M chunks/day, self-hosted default)
  50M chunks/day ÷ 1,500 chunks/sec/GPU ÷ 86,400 sec/day ≈ 0.4 GPU-days
    of raw throughput — but bursty, provisioned for peak (§2: 8-12 GPUs)
  12 GPUs × 24h × 30d × $1.50/GPU-hour ≈ $12,960/month
  (this is the "always-on capacity for burst headroom" number, not the
   theoretical minimum — most of this pool sits idle off-peak, which is
   why the bulk/incremental lane priority split, §7.2, matters: it lets
   this same fleet serve both without needing to size for their sum)

QUERY COMPUTE (20,000 qps average, ~30% reranked — §2, §10.4)
  Vector search: mostly CPU, amortized into the vector-store node fleet
    cost (folded into a broader "serving infra" line, not broken out
    per-query at this level of estimate)
  Reranking: 6,000 qps × ~60ms GPU-time/query ≈ 360 GPU-seconds/sec
    of sustained demand ≈ needs ~360 concurrent GPU-query-slots at
    peak batching efficiency — roughly a 20-30 GPU pool provisioned
    for peak, less for average
  20 GPUs × 24h × 30d × $1.50/GPU-hour ≈ $21,600/month

RERANKING (if partially hosted-API instead of self-hosted, illustrative)
  At Cohere-Rerank-class pricing (~$2/1000 searches), 6,000 qps reranked
  × 86,400 sec/day × 30d ≈ 15.5B reranked calls/month — clearly past
  the self-hosting break-even (§20.5) by orders of magnitude, confirming
  self-hosted reranking is the only economically sane choice at this
  query volume, not merely a nice-to-have optimization

TOTAL (rough): storage $3-5k + embedding ~$13k + reranking GPU ~$22k
  + serving/orchestration overhead (control plane, connectors, ingestion
  workers — comparable order of magnitude to the above) ≈ low-to-mid
  six figures per month at full stated scale (500+ KBs, 10B chunks,
  20k qps) — the kind of number that makes chargeback (below) not
  optional but load-bearing for the platform team's own budget defense.
```

The headline takeaway from this arithmetic: **reranking GPU capacity, not storage, is the largest single line item** at the platform's target query volume — directly validating §10.4's decision to make reranking opt-in and eval-gated rather than default-on, and §18's identification of reranking-adoption growth as the first place the platform is likely to hit a capacity wall as usage scales.

### Chargeback

Every cost component is attributable per KB (embedding calls tagged by `kb_id`, storage metered by KB's shard allocation, query compute metered by `kb_id` in request logs) and rolled up into the KB analytics dashboard (§16) — making cost visible to the owning team is itself the biggest lever, since most of the cost growth in practice comes from defaults (unbounded chunk size, reranking-on-everything, no dimensionality reduction) that teams change readily once they see the bill attributed to them.

---

## 21. Trade-offs

### 21.1 Chunking granularity

| Smaller chunks | Larger chunks |
|---|---|
| Higher retrieval precision (less irrelevant text per chunk) | Better context continuity, fewer boundary-split ideas |
| More chunks → more storage, more embedding cost, more index memory | Fewer chunks → cheaper, but coarser matching |
| Risk: a fact and its context land in different chunks | Risk: a chunk is "about" many things, diluting its embedding, hurting precision |

**Resolution**: parent-child chunking (§6.5) is the platform's answer to not actually having to choose — retrieve small (precision), generate with large (context) — at the cost of roughly doubling stored chunk records (children + parents) and added indexing complexity. For KBs unwilling to absorb that complexity, a single mid-sized (400-token) chunk with overlap is the pragmatic middle default.

### 21.2 Embedding dimensionality vs. quality

Already quantified in §7.4's table — the trade-off is close to monotonic (more dims, more quality, more cost) but with **steeply diminishing returns** past ~768 dims for most retrieval workloads, which is why 768 is the platform default rather than the theoretical-max 1536+ some hosted models offer. KBs with genuinely quality-critical, low-volume use cases (e.g., legal/compliance search where a missed document has real consequences) are the ones that should deliberately opt up to full dimensionality; defaulting everyone there would be paying 2x storage for most KBs' unmeasurable benefit.

### 21.3 Pre-filter vs. post-filter permissions

Fully argued in §8.6/§11.3: pre-filter chosen for permissions specifically because selectivity is often extreme and the failure mode of post-filtering (silent under-return, or expensive refetch storms) is unacceptable for a security-relevant filter. The cost is implementation complexity (filter-aware ANN traversal) and a small fixed per-hop overhead even for permissive users — accepted because the alternative's worst case is worse than its best case is good.

### 21.4 Freshness vs. query latency/cost

- Real-time webhook-driven sync (Slack, Confluence) buys tight freshness SLAs but costs more infrastructure (persistent connections, event processing) than periodic polling.
- Freshness-weighted ranking (§12.3) or freshness hard-filters cost nothing extra at query time (the score is precomputed at index time, just read at query time) — the real trade-off isn't freshness-at-query-time cost, it's **ingestion pipeline cost/complexity to make freshness tight in the first place** (§4's per-source mechanism table is really a menu of freshness-vs-engineering-effort trade-offs, source by source).
- Aggressive query-result caching (§9.5) directly trades against freshness — the platform's answer is TTL-per-KB rather than a global constant, so freshness-insensitive high-QPS KBs can cache hard while freshness-critical KBs set TTL near zero.

### 21.5 Single embedding model vs. many

| Single platform-wide model | Multiple concurrent models (chosen, §7.1) |
|---|---|
| Simpler operations — one model to monitor, one migration path, one capacity plan | Per-KB model choice lets cost-sensitive KBs pick a small self-hosted model while quality-critical KBs pick a large hosted one — no KB pays for capability it doesn't need |
| No risk of comparing scores across incompatible vector spaces by mistake, because there's only one space | Requires discipline: a query must never compare/merge raw similarity scores computed by two different models (§9.3's per-shard score-normalization caveat generalizes to per-model too) — a real correctness trap if the discipline slips |
| Cannot take advantage of a better model for one KB without forcing every other KB through the same migration | Model migration (§7.4) is inherently per-KB, so a KB can upgrade independently without coordinating a platform-wide flag day |
| Embedding infrastructure sizing is a single, simpler forecast | GPU pool must serve several model architectures' inference simultaneously — more operational surface area (§7.3's dynamic batching has to be model-aware, not just batch-aware) |

**Decision: multiple concurrent models**, because the cost-sensitivity spread across 500+ independently-owned KBs (§1's assumption 4 — a strong power-law in KB size and, in practice, in budget) makes a single platform-wide model either overkill for small KBs or underpowered for the largest, quality-critical ones. The operational cost (serving several model architectures, disciplined per-model score handling) is accepted because the alternative — forcing every team onto one model's cost/quality point — would push some teams to abandon the platform for a bespoke solution, defeating its purpose as shared infrastructure.

### 21.6 Hosted vs. self-hosted inference: rate limits and blast radius

Beyond the raw cost comparison (§7.4), hosted and self-hosted embedding/reranking inference differ in a way that matters specifically *because* this is a shared multi-tenant platform:

| | Hosted API | Self-hosted |
|---|---|---|
| Rate-limit ownership | Shared across the **entire platform's** usage of that provider — one KB's burst can throttle every other KB's calls to the same provider unless the platform implements its own per-KB sub-allocation on top of the provider's overall limit | Rate limits are whatever the platform's own GPU pool and admission control (§18.3) decide — fully within the platform's control to allocate fairly per KB |
| Blast radius of an outage | A single hosted provider outage degrades **every KB** using that model simultaneously — a correlated, platform-wide failure (§19's "embedding provider outage" row) | A self-hosted GPU pool failure is contained to whatever fraction of KBs are pinned to models served by the affected pool — smaller, more contained blast radius, at the cost of the platform owning that infrastructure's reliability directly |
| Data residency / compliance | Document text leaves the platform's network boundary to reach the provider — a real constraint for KBs containing regulated or highly sensitive content, independent of the provider's own security posture | Data never leaves platform-controlled infrastructure — the only viable option for KBs under a data-residency constraint that forbids third-party processing |
| Time-to-availability for a new model | Immediate — a config change (§7.1's per-KB `embedding_model_id`) | Requires provisioning, serving-stack integration, and validation (§13's eval gate) before it's available as an option at all |

This is why the platform's per-KB `embedding_model_id` config (§7.1) isn't purely a cost/quality knob — for a subset of KBs (anything touching regulated data), self-hosting is a **hard requirement**, not an optimization, and the platform's connector/schema framework surfaces a `data_residency_constrained` flag at KB creation time that restricts the selectable model list to self-hosted-only options, closing off the hosted-API path entirely rather than relying on every KB owner to remember to avoid it.

---

## 22. Evolution Path

### v1 — Basic RAG

- Single embedding model, single chunking strategy (recursive splitting) platform-wide default.
- Dense-only vector search, HNSW, single shard per KB.
- No reranking.
- Coarse permissions: KB-level access only (a user can see the whole KB or none of it) — document-level ACL sync deferred.
- Manual/no freshness tracking beyond "when did the connector last run."
- No evaluation tooling — quality assessed ad hoc by owning teams.
- Handful of connectors: Confluence, Google Drive, S3.

### v2 — Hybrid search + reranking

- Hybrid dense + BM25 search with RRF (§8.6, §10.2).
- Cross-encoder reranking, opt-in (§10.1, §10.4).
- Multiple chunking strategies, configurable per source (§6).
- Multiple embedding models supported; migration protocol (§7.4) introduced.
- Retrieval metrics (recall@k, MRR, NDCG) against manually curated golden sets (§13.1, §13.3).

### v3 — Permissions + freshness (this document's full design)

- Document-level ACL sync and query-time permission filtering with the pre-filter/secondary-check pattern (§11).
- Full incremental sync + tombstone + reconciliation freshness pipeline (§12).
- Full connector suite (Slack, SharePoint, databases, generic APIs, web crawl).
- Sharding, quantization, dimensionality tuning at scale (§8).
- End-to-end RAG evaluation (faithfulness, hallucination) with LLM-as-judge (§13.2, §13.5).
- Cost attribution and chargeback (§20).

### v4 — Agentic RAG with tool use

- Retrieval Service exposes a **tool-callable** interface (structured function-calling schema) so an agent can issue multiple, iterative, reasoning-driven retrieval calls within one user turn — re-querying based on what the first retrieval returned, rather than a single fixed retrieval-then-generate pass.
- **Query planning**: an agent decomposes a complex query into sub-queries routed to different KBs (e.g., "compare our Q3 incident rate to last year's policy on postmortem SLAs" → one sub-query to an incidents KB, one to a policy KB), with the platform providing a federated-search convenience layer that still enforces per-KB permission checks independently for each sub-query (§11.5's federation constraint holds).
- **Self-correcting retrieval**: the agent evaluates its own retrieved context's sufficiency (using the same faithfulness/context-relevance signals from §13.2, now applied inline rather than only offline) and re-retrieves with reformulated queries or wider filters if the initial context looks insufficient, before generating a final answer.
- **Tool-augmented sources**: beyond static document retrieval, agentic RAG calls live tools (a database query API, an internal search-the-web tool) mid-reasoning — this pushes the platform's connector framework (§4) toward also exposing certain sources as live-query tools, not only as pre-indexed corpora, for sources where freshness-at-generation-time matters more than freshness-at-index-time ever could.
- Evaluation extends to **multi-step trajectory quality** (did the agent retrieve efficiently, did it stop retrieving at the right point, not just "was the final answer good") — a materially harder eval problem than v3's single-shot retrieval eval, deliberately deferred until the single-shot foundation (§13) is solid and trusted.

### Migration mechanics between versions

Each version bump is deliberately designed to be a **live migration for existing knowledge bases**, not a breaking cutover — a KB built on v1 must keep serving traffic throughout its upgrade to v2, v3, and eventually v4.

```
v1 → v2 (adding hybrid search + reranking):
  1. BM25 sparse index built for existing KBs as a background job,
     reading from the already-populated Document Store (no re-embed
     needed — this is a new parallel index, not a change to the
     existing dense one).
  2. Once a KB's sparse index backfill completes, hybrid_alpha defaults
     to a conservative dense-heavy value (e.g., 0.8) rather than an
     untested 0.5 — the KB's golden dataset (once v2 introduces it) is
     what justifies moving the default lower over time, not a platform-
     wide guess.
  3. Reranking is available immediately as opt-in; no migration needed
     since it's a query-time addition with no stored-data dependency.

v2 → v3 (adding document-level permissions):
  This is the highest-stakes migration, because a KB that was
  previously KB-level-access-only now needs document-level ACLs BEFORE
  any query can safely run with the new filtering enabled.
  1. Permission Sync backfills ACLs for every existing document
     (§4/§11) — this can take significant wall-clock time for a large
     KB and runs fully in shadow (computed and stored, not yet enforced).
  2. A KB stays on the OLD coarse KB-level-access model for queries
     until its ACL backfill reaches 100% coverage AND passes a
     validation pass (spot-check a sample of documents' resolved ACLs
     against the source, confirming no widespread resolution failures).
  3. Cutover to document-level enforcement is a single atomic config
     flip per KB (mirroring the embedding-migration cutover pattern,
     §7.4) — never a gradual per-document rollout, because a KB
     straddling two permission models simultaneously is exactly the
     kind of inconsistent intermediate state the zero-leakage guarantee
     (§11.6) cannot tolerate.

v3 → v4 (adding agentic/tool-call retrieval):
  Purely additive at the API layer — the existing single-shot retrieval
  API (§16) continues to work unchanged; the tool-callable interface is
  a new API surface built on top of it, so no existing application
  integration breaks, and teams opt into agentic patterns per
  application rather than being migrated wholesale.
```

The v2→v3 sequencing is the one worth internalizing beyond this document: **any migration that changes a security-relevant guarantee must have an explicit, validated, all-or-nothing cutover per tenant** — the same atomic-flip pattern used for embedding-model migration (§7.4) generalizes here precisely because "gradual rollout" and "security guarantee change" are close to a contradiction in terms.

---

## 23. Exercises

1. **Selective permission filter under load.** A KB has 50M chunks; a particular principal's allow-set covers only 0.3% of them (a highly restricted contractor account). Walk through what happens with (a) naive post-filtering with 10x overfetch, (b) the pre-filter bitset approach from §8.6/§11.3. Estimate query latency for both and state at what allow-set selectivity post-filtering becomes acceptable.

2. **Embedding model migration under continuous writes.** Design the exact sequencing (extending §7.4) for migrating a 500M-chunk, actively-written-to KB (documents still being added/edited during the migration) from one embedding model to another with zero query downtime and no window where a query could silently mix scores from two incompatible vector spaces. What happens to a document edited *during* the migration window?

3. **Tombstone race.** A document is deleted at the source at the same moment a query is mid-flight against a shard that hasn't yet received the tombstone. Trace exactly where in the pipeline (§8, §9, §12.3) this is caught, and confirm whether the answer changes if the deletion is a permission revocation instead of a full delete.

4. **Chunking strategy regression.** A team changes their KB's chunking strategy from fixed-size to document-structure-aware. Design the eval gate (§13.4) that must pass before this ships to production, including how you'd detect that the new strategy silently drops content types the old one used to chunk successfully (e.g., documents with no heading structure at all).

5. **Cost blowup diagnosis.** A KB's monthly cost triples with no corresponding growth in document count. Using the cost model (§20) and chargeback data, list the top 3 hypotheses you'd check first and the specific metric/dashboard (from earlier sections) that would confirm or rule out each.

6. **Cross-KB federated query design.** Design the API and permission-enforcement flow (extending §11.5 and the v4 evolution note in §22) for a single retrieval call that searches 3 KBs owned by different teams and merges results, without ever letting a result from KB B leak to a principal unauthorized for KB B even transiently in a merged/reranked intermediate result set.

7. **Staleness SLA violation triage.** The staleness dashboard (§12.2) shows a source's P99 document age has crept from 15 minutes to 6 hours over two weeks, with no connector errors logged. Enumerate the failure modes from §4, §12 that are consistent with "no errors, but slow," and design the additional signal(s) that would have caught this earlier.

8. **Small-scale variant.** A single team wants a 50,000-chunk internal KB with one Confluence source, no reranking budget, and one part-time owner. Which components from this design collapse or become no-ops (mirroring the KV store design's Variant C exercise), and at what chunk-count/QPS threshold does each dropped component need to come back?

---
