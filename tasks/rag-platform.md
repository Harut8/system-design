## System Design Task: Enterprise RAG Platform

### Problem Statement

Design an **enterprise Retrieval-Augmented Generation (RAG) platform** — internal
infrastructure that lets **hundreds of product teams** across a large
organization build LLM-powered applications (support copilots, internal search,
coding assistants, sales enablement bots, compliance Q&A) without each team
reinventing ingestion, chunking, embedding, vector search, and retrieval quality
evaluation from scratch.

Teams **connect data sources** — Confluence, Google Drive, SharePoint, Slack,
S3 buckets, relational databases, internal REST APIs, and crawled websites —
and declare a **knowledge base**. The platform then:

1. Ingests and parses documents from every connected source on an ongoing basis.
2. Splits documents into chunks using a strategy appropriate to the content type.
3. Embeds chunks with one or more embedding models.
4. Indexes chunks in a vector store that supports both dense (semantic) and
   sparse (lexical) search.
5. Serves a **retrieval API** that returns the top-k most relevant chunks for a
   query, respecting the **exact same document-level permissions** the source
   system enforces — a chunk from a Confluence page a user cannot open must
   never be retrievable by that user, even indirectly.
6. Keeps the index **fresh** as source documents are created, edited, moved,
   and deleted, and exposes freshness as a first-class signal.
7. Lets teams **evaluate and iterate** on retrieval quality — recall, ranking,
   and downstream answer faithfulness — before and after they ship changes.

This is a **platform team's** problem, not a single application's: hundreds of
knowledge bases will be created independently, at wildly different scales (a
50-document team wiki next to a 200-million-document engineering + support
corpus), by teams with no vector-database or IR expertise. The platform must be
safe by default (permissions cannot be an opt-in an application team forgets)
and must degrade gracefully — a runaway crawl in one knowledge base must not
starve retrieval latency for every other tenant.

A deliberate tension the design must resolve, not hand-wave: **freshness vs.
cost vs. latency**. Re-embedding everything on every source edit is
prohibitively expensive at this scale; embedding nothing until a nightly batch
makes the platform unusable for fast-moving sources like Slack. State the
policy, and the mechanism, per source class.

---

### Functional Requirements

1. **Data Source Connectors**

   * First-class connectors for: **Confluence, Google Drive, SharePoint, Slack,
     S3 (and S3-compatible object storage), relational databases (via
     configurable SQL extraction), generic REST APIs (via a declarative schema),
     and web crawlers** (seed URL + domain scoping + robots.txt compliance).
   * Every connector supports **incremental sync** — after the first full
     backfill, subsequent syncs fetch only what changed, using whichever
     mechanism the source offers: change tokens/delta APIs (Google Drive,
     SharePoint), webhooks/event subscriptions (Slack, Confluence), watermark
     polling (databases, S3 `LastModified`), or crawl-diffing (web).
   * Connector configuration is declarative (source credentials, scope —
     specific spaces/drives/channels/buckets/tables — sync cadence, and
     per-source parsing hints) and stored per knowledge base.
   * Connectors must survive source-side rate limits, transient outages, schema
     drift, and partial failures (one bad document must not fail the whole sync
     batch).

2. **Ingestion Pipeline**

   * Parse heterogeneous formats: **PDF (including scanned/OCR), DOCX, PPTX,
     HTML, Markdown, plain text, CSV/TSV, source code (40+ languages), and
     structured JSON/API payloads.**
   * Extract and normalize metadata: title, author, created/modified
     timestamps, source URL, source-native permissions, tags/labels, and a
     content type classification.
   * Language detection per document (and per chunk, for multilingual
     documents), recorded as metadata usable as a retrieval filter.
   * **Deduplication**: exact duplicates via content hashing, and near-duplicate
     detection (e.g., a Confluence page mirrored into Drive) via fuzzy/simhash
     comparison, with a policy for which copy is canonical.
   * Pipeline must be resumable and idempotent — a crash mid-batch must not
     produce duplicate or missing chunks on retry.

3. **Chunking**

   * Support multiple chunking strategies, selectable per knowledge base or
     per source, and combinable:
     * **Fixed-size** (token count with configurable overlap) — the fallback
       for unstructured text.
     * **Recursive character/token splitting** (split on paragraph, then
       sentence, then word boundaries until under the size budget).
     * **Semantic chunking** — embedding-based detection of topic boundaries
       within a document, so chunks don't cut mid-idea.
     * **Document-structure-aware chunking** — respect headings, sections,
       tables, and code blocks (e.g., never split a function or a Markdown
       table mid-row).
     * **Parent-child chunking** — small chunks for precise retrieval, indexed
       against a larger parent chunk/section returned for generation context.
     * **Sliding window with overlap**, with overlap size tunable per content
       type.
   * Preserve enough metadata per chunk to reconstruct its position in the
     source document (document id, section path, page number, char offsets)
     and to re-render a citation link back to the original.

4. **Embedding**

   * Support **multiple embedding models concurrently** (e.g., different
     models per knowledge base, or a default plus opt-in alternatives),
     including both hosted API models and self-hosted open-source models.
   * Batch embedding for throughput; a low-latency path for small
     incremental updates.
   * **Incremental re-embedding when a model changes** — the platform must be
     able to introduce a new/better embedding model, backfill affected
     knowledge bases, and cut over without downtime or a mixed-model index
     serving wrong similarity scores.
   * Configurable embedding **dimensionality** (e.g., via Matryoshka
     representation learning or model choice), trading storage/latency
     against retrieval quality per knowledge base's needs.

5. **Vector Store**

   * Support both **approximate nearest-neighbor indexing** (HNSW) and
     **quantized/inverted indexing** (IVF-PQ), selectable by scale/recall/cost
     trade-off per knowledge base.
   * **Hybrid search**: combine dense vector similarity with sparse lexical
     search (BM25 or learned sparse retrieval like SPLADE) in a single query.
   * **Filtered search**: combine vector similarity with structured metadata
     filters (permissions, source, date range, content type, language) without
     requiring a full post-filter scan.
   * **Multi-tenancy**: knowledge bases must be logically and, above a size
     threshold, physically isolated — one tenant's index growth or query load
     must not degrade another's latency.
   * **Sharding** for knowledge bases that exceed a single index's practical
     size, with query fan-out and result merging.

6. **Retrieval API**

   * A query API accepting: free-text query, `top_k`, similarity-score
     threshold, hybrid search weighting (dense vs. sparse), and arbitrary
     metadata filters (source, date range, author, tags, language,
     permissions context).
   * Pagination/continuation for retrieval beyond the first page of results.
   * Must return, per result: chunk text, score(s), source citation
     (document id, URL, location), and freshness metadata (last synced time).

7. **Reranking**

   * **Cross-encoder reranking** of an initial candidate set for precision.
   * **Reciprocal rank fusion (RRF)** to merge dense and sparse result lists
     before/instead of a cross-encoder pass.
   * **Diversity reranking (MMR — maximal marginal relevance)** to avoid
     returning five near-duplicate chunks from the same document.
   * Reranking must be optional and its latency/quality trade-off must be
     explicit — some callers (e.g., autocomplete-adjacent use cases) need raw
     vector search latency and cannot afford a reranking hop.

8. **Permission-Aware Retrieval**

   * Document-level **ACLs synced from the source system** (Confluence space
     permissions, Drive sharing, Slack channel membership, DB row-level
     grants, S3 bucket policy) must be mirrored into the platform's permission
     index, kept in sync as they change at the source.
   * **Query-time permission filtering**: every retrieval call is scoped to a
     requesting principal (user or service identity plus group memberships),
     and results the principal cannot access at the source must never be
     returned — including in aggregate signals like scores or counts
     ("information leakage" is a hard requirement, not a best-effort one).
   * Group/role expansion (a user's group memberships must be resolved and
     applied, not just direct grants).

9. **Freshness Management**

   * Incremental sync on a per-source cadence appropriate to that source's
     rate of change (Slack messages vs. a rarely-edited PDF policy doc).
   * **Staleness detection**: track time since last successful sync per
     document/source and flag sources whose sync is failing or lagging.
   * **Freshness scoring** usable as a retrieval signal (recency boost,
     or a filter to exclude stale results for freshness-sensitive use cases).
   * **Tombstone handling**: a document deleted or unshared at the source must
     stop being retrievable within a bounded time, even before the next full
     sync completes.
   * **Source-of-truth reconciliation**: periodic full reconciliation to catch
     drift that incremental sync missed (missed webhooks, clock skew, connector
     bugs).

10. **Evaluation**

    * **Retrieval metrics**: recall@k, precision@k, MRR, NDCG against a
      labeled golden dataset per knowledge base.
    * **End-to-end RAG metrics**: faithfulness (is the generated answer
      supported by retrieved context), answer relevance, context relevance/
      precision, and hallucination rate.
    * **Golden dataset management**: creation, versioning, and maintenance of
      query→relevant-document(s) judgment sets, including from real query logs.
    * Automated evaluation pipelines runnable on demand (e.g., in CI before a
      chunking-strategy change ships) and on a schedule (regression detection).
    * Support **A/B testing** of retrieval configuration changes
      (chunking strategy, embedding model, hybrid weights, reranker) against
      the golden dataset and/or live traffic.

11. **Knowledge Base Management**

    * CRUD for knowledge bases: create, configure sources, set chunking/
      embedding/retrieval defaults, delete (with cascading cleanup of chunks,
      embeddings, and permission entries).
    * Source configuration UI/API: add/remove/reconfigure connectors per
      knowledge base.
    * Schema/metadata management: define custom metadata fields usable as
      retrieval filters per knowledge base.
    * **Usage analytics**: query volume, latency, top queries, zero-result
      queries, retrieval quality trend, cost attribution per knowledge base/
      team.

---

### Non-Functional Requirements

1. **Scale**

   * **500+ knowledge bases**, owned by 200+ independent teams.
   * **50 million source documents** platform-wide, growing to **500 million**
     within two years.
   * **10 billion chunks** platform-wide at steady state (avg ~20 chunks/doc),
     scaling to tens of billions.
   * Largest single knowledge base: **2 billion chunks**.
   * **20,000 queries/sec** sustained across the platform at peak; a single
     large knowledge base may see 2,000 queries/sec.
   * **Ingestion throughput**: sustained 50M chunks/day platform-wide
     (embedding + indexing), with burst capacity for large initial backfills
     (e.g., a new 10M-document knowledge base backfilling within 48 hours).

2. **Latency**

   * Retrieval API (vector + hybrid search, no reranking) P50 ≤ **80 ms**,
     P99 ≤ **300 ms**, for `top_k ≤ 50` on a knowledge base up to 1B chunks.
   * With cross-encoder reranking of top-100 candidates: P50 ≤ **250 ms**,
     P99 ≤ **700 ms**.
   * Permission filtering must add ≤ **15 ms** P99 to the unfiltered query.
   * End-to-end ingestion latency (document changed at source → retrievable,
     for high-priority sources like Slack): P50 ≤ **2 minutes**, P99 ≤ **15
     minutes**.
   * Bulk/backfill sources (e.g., a nightly DB export): freshness SLA of
     ≤ 24 hours is acceptable and should be priced accordingly cheaper.

3. **Availability**

   * Retrieval API: **99.95%** — this is on the hot path of production
     customer-facing applications built on top of the platform.
   * Ingestion pipeline: **99.9%** — brief ingestion outages are tolerable if
     they do not cause data loss or permission-sync gaps, and if they self-heal
     via reconciliation.
   * A single knowledge base's misbehavior (malformed documents, connector
     outage, query storm) must not degrade availability or latency for other
     knowledge bases (**noisy-neighbor isolation**).

4. **Consistency & Correctness**

   * **Zero information leakage** is a hard invariant: a permission-sync lag
     must fail closed (exclude), never fail open (leak).
   * Deletions/unshares at the source must be reflected (tombstoned) in
     retrieval results within **5 minutes** of the platform receiving the
     change event, independent of the next full sync.
   * Eventual consistency is acceptable for freshness of *content*, but never
     for permission *revocation* beyond the stated bound.

5. **Cost**

   * Embedding cost, vector storage cost, and query compute cost must be
     attributable per knowledge base/team for chargeback.
   * The design should articulate concrete cost levers (dimensionality,
     quantization, storage tiering, reranking gating) and their trade-offs —
     this is a cost-sensitive shared platform, not a single well-funded
     product.

6. **Operability**

   * Onboarding a new data source type should not require platform-team
     code changes for common cases (config-driven connector framework).
   * Re-embedding an entire large knowledge base (model migration) must be
     doable with zero query downtime and a safe rollback.
   * Every quality and freshness guarantee must be **observable** — dashboards
     and alerts, not just design intent.

---

### Constraints and Assumptions

* Source systems (Confluence, Drive, Slack, etc.) are the **source of truth**
  for both content and permissions; the platform is a derived, eventually
  consistent index, never authoritative.
* Assume standard enterprise identity: users and service principals resolve to
  a stable identity plus group memberships via an internal identity provider;
  you do not need to design SSO/IdP itself, only integrate with it.
* Embedding and LLM inference may use both hosted third-party APIs and
  internally hosted models; assume both are available but have different
  cost/latency/rate-limit profiles worth reasoning about.
* Some source systems (arbitrary internal REST APIs, hand-rolled DB exports)
  will not offer clean incremental-change primitives — the design must degrade
  gracefully to polling/diffing for these.
* Not in scope: designing the LLM generation/orchestration layer itself
  (agents, prompt templates, chat UI) — the platform's contract ends at the
  retrieval API and its evaluation tooling. You should, however, define the
  evaluation of *generation quality* (faithfulness, hallucination) since it
  depends on what was retrieved.

---

### What You Should Deliver

1. Requirement clarification and explicit assumptions.
2. High-level architecture: every major service, the data plane and control
   plane, and how a document flows from source to retrievable chunk, and how a
   query flows from request to ranked, permission-filtered result.
3. Detailed design for **each** functional requirement area above — connectors,
   ingestion, chunking, embedding, vector store, retrieval, reranking,
   permissions, freshness, evaluation, and knowledge base management.
4. Data models for the core entities (knowledge base, data source, document,
   chunk, embedding, permission, eval dataset/result) and the key APIs.
5. Capacity estimates with the arithmetic shown, not just final numbers.
6. Failure-mode analysis: what happens, precisely, when a connector fails
   mid-sync, an embedding model degrades, the vector store loses a shard, a
   permission sync lags, or a knowledge base tries to backfill 50M documents
   at once.
7. Scaling strategy for each component, and where the bottlenecks will appear
   first as the platform grows 10x.
8. A cost model with concrete levers for reducing embedding, storage, and
   query cost, and the quality/latency trade-offs each lever costs.
9. An evolution path: what ships in v1 vs. what is deliberately deferred, and
   why.
10. Explicit trade-off discussion for at least: chunking granularity, embedding
    dimensionality vs. quality, pre-filter vs. post-filter permission
    enforcement, and freshness vs. query latency/cost.

---

### Expectations

* **Do the arithmetic.** Chunk counts, storage footprint, embedding cost,
  index memory, and shard counts should appear as numbers with the calculation
  shown, not adjectives.
* **Treat permissions as a security boundary, not a feature.** State exactly
  where enforcement happens (index-time filter, query-time filter, or both)
  and why an information leak is structurally impossible, not just unlikely.
* **Be precise about consistency.** "Freshness" and "up to date" need bounded,
  numeric definitions per source class, mirroring how the key-value store
  design demands a precise definition of "consistent."
* **Name concrete mechanisms** — HNSW vs. IVF-PQ, RRF, MMR, cross-encoders,
  Matryoshka embeddings, change-data-capture, webhook vs. polling sync — and
  say what each buys and what it costs.
* Prefer a design a mid-sized platform team can actually **operate and evolve**
  over one that is maximal on day one.
* Assume this platform will onboard data sources and teams nobody has thought
  of yet — the connector and chunking frameworks should be extensible without
  a redesign.

---
