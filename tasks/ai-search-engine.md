## System Design Task: AI-Powered Search Engine

### Problem Statement

Design an **AI-powered search engine** that combines **traditional information
retrieval (lexical search, link analysis, web crawling)** with **modern ML/LLM
capabilities (semantic understanding, query rewriting, answer synthesis, and
retrieval-augmented generation)** to serve billions of web documents to hundreds
of millions of users.

Traditional search engines rank documents by keyword matching and link-graph
signals (PageRank). They return ten blue links and force the user to click
through, read, and synthesize the answer themselves. AI-powered search changes
this contract: the engine **understands the user's intent**, retrieves the most
relevant evidence from across the web, and **synthesizes a direct answer**
citing its sources — while still providing ranked document results for
exploration.

The challenge is doing this at web scale (hundreds of billions of documents),
at Google-class latency (sub-second end-to-end), with the reliability and
freshness users expect, while keeping LLM inference costs economically viable
for billions of daily queries. Not every query needs an AI-synthesized answer —
navigational queries ("facebook login") should shortcut to the destination, and
simple factual queries ("weather in SF") are best served by structured data.
The system must **route each query to the right treatment** and only invoke
expensive LLM generation when it genuinely adds value.

This is not a wrapper around an existing search engine plus a chatbot. It is a
ground-up design of a search system where AI is integrated into every stage:
crawling prioritization, document understanding, query interpretation, ranking,
answer generation, and result presentation.

---

### Functional Requirements

1. **Web Crawling and Document Ingestion**

   * A distributed web crawler that discovers, fetches, and indexes the
     **public web** — billions of URLs, with incremental recrawling on a
     per-domain freshness policy.
   * **Crawl prioritization using ML**: predict which pages are likely to be
     high-value, frequently changing, or newly published, and allocate crawl
     budget accordingly — rather than round-robin or purely link-count-based
     prioritization.
   * Document parsing and content extraction: HTML (main content extraction
     via ML-based boilerplate removal), PDF, images (OCR), structured data
     (JSON-LD, schema.org, Open Graph), and multimedia metadata.
   * **Document understanding pipeline**: for every crawled page, extract:
     * Title, headings, and structural hierarchy
     * Named entities (people, organizations, locations, dates, products)
     * Topic classification and content quality score (spam/low-quality
       filtering)
     * Factual claims and their source confidence
     * Sentiment and freshness signals
   * **Near-duplicate detection**: cluster near-duplicate pages (syndicated
     content, scraped copies) and select a canonical URL, using content
     hashing and learned similarity.
   * **robots.txt compliance, politeness policies** (crawl rate limits per
     domain), and legal/ethical crawling constraints.

2. **Index Architecture**

   * **Inverted index** for lexical (keyword) search over the full web
     corpus, partitioned by document and tiered by quality (high-quality
     pages in a fast tier, long-tail in a larger but slower tier).
   * **Vector index** for semantic search: dense embeddings per document (or
     per passage) enabling similarity-based retrieval for queries where
     keyword matching fails (paraphrase, conceptual queries, multi-hop
     reasoning).
   * **Knowledge graph**: a structured representation of entities and
     relationships extracted from the web, supporting direct answers to
     factual queries ("Who founded OpenAI?") and query entity linking.
   * **Structured data index**: tables, lists, and schema.org data indexed
     separately for direct-answer extraction (featured snippets, knowledge
     panels).
   * **Real-time index**: a separate short-lived index for breaking news and
     rapidly changing content (social media, live events), merged with the
     main index at query time but updated with sub-minute latency.
   * **Index serving**: sharded and replicated across data centers, with each
     query fanning out to the relevant shards, collecting results, and
     merging with a latency budget.

3. **Query Understanding**

   * **Intent classification**: classify every query into one of:
     * **Navigational** ("youtube", "gmail login") → shortcut to the
       destination URL.
     * **Informational — simple** ("capital of France") → answer from
       knowledge graph or structured data.
     * **Informational — complex** ("how does mRNA vaccine immunity wane over
       time") → AI-synthesized answer from multiple sources.
     * **Transactional** ("buy iPhone 16 Pro") → shopping/product results.
     * **Local** ("pizza near me") → local business results with maps.
     * **Ambiguous** → present clarification or diversified results.
   * **Query rewriting / expansion**: use an LLM to reformulate the query for
     better retrieval — expand abbreviations, resolve coreferences from
     conversation history, decompose multi-part questions, and generate
     sub-queries for multi-hop reasoning.
   * **Spell correction and normalization**: correct typos, normalize Unicode,
     handle multi-language queries, and detect query language.
   * **Entity linking**: identify entities in the query and link them to the
     knowledge graph for structured augmentation.
   * **Safety classification**: detect queries with harmful intent, CSAM
     indicators, or PII, and route them to appropriate policy treatment
     rather than standard search.

4. **Ranking System**

   * **Multi-stage ranking pipeline**:
     * **Stage 1 — Candidate retrieval**: BM25 lexical retrieval + ANN
       vector retrieval, union of candidates (thousands of documents).
     * **Stage 2 — Lightweight ranker**: a fast ML model (e.g., gradient-
       boosted tree or small transformer) that scores thousands of
       candidates using features: BM25 score, embedding similarity,
       PageRank, click-through rate, freshness, domain authority.
     * **Stage 3 — Neural reranker**: a cross-encoder transformer that
       jointly encodes (query, document) pairs for the top ~100 candidates,
       producing a fine-grained relevance score.
     * **Stage 4 — Business logic and policy layer**: apply diversity
       (no more than 2 results from the same domain), freshness boost for
       time-sensitive queries, safe-search filtering, and legal removals
       (DMCA, right-to-be-forgotten).
   * **Personalization signals** (opt-in): search history, location,
     language preference, device type — used as ranking features, never as
     the primary signal.
   * **Click-through feedback loop**: aggregate click, dwell-time, and
     pogo-sticking signals to improve ranking models, with safeguards
     against position bias and click fraud.
   * **Freshness-aware ranking**: for queries with time sensitivity (news,
     events, stock prices), aggressively boost recent content.

5. **AI Answer Generation**

   * **Retrieval-Augmented Generation (RAG)**: for complex informational
     queries, retrieve the top-k most relevant passages, feed them as
     context to an LLM, and generate a synthesized answer that:
     * Directly answers the user's question.
     * Cites specific sources inline (with links to the originating pages).
     * Includes a confidence indicator when the evidence is ambiguous or
       conflicting.
   * **Streaming generation**: stream the AI answer to the user as it's
     generated (token by token), so they see content within 1-2 seconds
     rather than waiting for the full answer.
   * **Answer quality guardrails**:
     * **Grounding**: every factual claim in the answer must be traceable to
       a retrieved source — hallucinated claims must be detectable and
       suppressible.
     * **Freshness**: the LLM's parametric knowledge is stale by definition;
       retrieved context must take precedence for time-sensitive facts.
     * **Attribution**: inline citations are mandatory, not decorative —
       the user must be able to verify each claim.
     * **Contradiction handling**: when retrieved sources disagree, present
       the disagreement rather than silently picking a side.
   * **Cost-aware generation routing**: not every query gets an LLM-generated
     answer. The system must decide, within milliseconds, whether to:
     * Return traditional ranked results only (navigational, transactional).
     * Return a knowledge-graph instant answer (simple factual).
     * Invoke the LLM for a synthesized answer (complex informational).
   * **Multi-turn context**: for conversational search (follow-up queries
     like "what about their revenue?"), maintain session context and resolve
     coreferences against the prior turn.

6. **Specialized Search Verticals**

   * **Image search**: content-based image retrieval (CLIP embeddings),
     combined with surrounding text context and OCR.
   * **News search**: real-time news aggregation, clustering, and
     deduplication with sub-minute freshness.
   * **Video search**: index video transcripts (speech-to-text), metadata,
     and key-frame embeddings.
   * **Shopping/Product search**: structured product data, price comparison,
     reviews aggregation.
   * **Local search**: business listings, maps integration, review
     aggregation, opening hours.
   * **Academic/Scholar search**: paper indexing, citation graph, author
     disambiguation.

7. **User Interface and Experience**

   * **Search results page (SERP)**: a unified layout combining:
     * AI-synthesized answer (when triggered) at the top with inline
       citations.
     * Traditional ranked web results below.
     * Vertical-specific modules (images, news, videos, shopping) inserted
       contextually.
     * Knowledge panel (entity card) on the side for entity queries.
     * "People also ask" — related questions generated via query-log
       analysis and LLM suggestion.
   * **Autocomplete**: real-time query suggestions as the user types, from
     query logs, trending queries, and personalized history, with
     sub-100ms latency.
   * **Conversational interface**: a chat-style interface for multi-turn
     search, where the user can ask follow-up questions and the system
     maintains context.

8. **Feedback and Quality**

   * **Human evaluation pipeline**: a pool of search quality raters who
     evaluate (query, result) relevance on a defined scale, feeding into
     ranking model training and regression detection.
   * **Automated quality metrics**: NDCG, MRR, and satisfaction metrics
     computed continuously over live traffic and golden datasets.
   * **A/B testing infrastructure**: run controlled experiments on ranking
     model changes, UI changes, and answer generation strategies, with
     statistical rigor (minimum detectable effect, sample size, duration).
   * **Anti-abuse**: detect and mitigate SEO spam, adversarial content
     injection, and attempts to manipulate AI-generated answers via
     poisoned web pages.

---

### Non-Functional Requirements

1. **Scale**

   * **Index size**: 200+ billion web documents, 50+ billion with dense
     vector embeddings (high-quality tier).
   * **Daily queries**: 5+ billion, with peaks during major global events.
   * **QPS**: sustained 60,000 QPS, peak 150,000+ QPS.
   * **Crawl rate**: 5+ billion pages/day recrawled or newly discovered.
   * **AI answer generation**: triggered for ~20% of queries (1 billion/day),
     with the rest served by traditional ranking alone.

2. **Latency**

   * **End-to-end SERP (non-AI)**: P50 ≤ **200 ms**, P99 ≤ **500 ms**
     (from query submission to full results page rendered).
   * **Time-to-first-token for AI answer**: P50 ≤ **800 ms**, P99 ≤ **2
     seconds** — the user sees the answer start streaming quickly.
   * **Full AI answer generation**: P50 ≤ **3 seconds**, P99 ≤ **8
     seconds** for a complete synthesized answer.
   * **Autocomplete**: P99 ≤ **50 ms**.
   * **Real-time index freshness**: breaking news indexed and searchable
     within **1 minute** of first crawl.

3. **Availability**

   * Search serving: **99.99%** (≈52 minutes downtime/year).
   * AI answer generation: **99.9%** — degraded mode (traditional results
     only) is acceptable when the LLM serving layer is impaired.
   * No single data center failure should cause user-visible degradation.

4. **Relevance / Quality**

   * **NDCG@10 ≥ 0.75** on a representative query sample, measured via
     human evaluation.
   * AI answer **faithfulness ≥ 95%** — no more than 5% of generated
     answers contain claims unsupported by retrieved sources, measured via
     automated grounding checks and human evaluation.
   * **Zero tolerance** for AI-generated answers on YMYL (Your Money, Your
     Life) queries that contradict authoritative sources (medical, financial,
     legal).

5. **Cost**

   * LLM inference cost must be **economically viable at 1 billion AI
     answers/day** — the design must articulate the inference cost model
     and the mechanisms (caching, smaller models for simple answers, query
     routing) that keep it feasible.
   * Index storage and serving infrastructure cost must be modeled per
     billion documents.

6. **Privacy and Safety**

   * Query logs anonymized or pseudonymized per regional regulations (GDPR,
     CCPA).
   * Safe search mandatory for minors; configurable for adults.
   * No user PII included in LLM prompts sent to third-party providers (if
     any external LLM is used).
   * Right-to-be-forgotten compliance: remove specific URLs from the index
     and any cached AI answers within a bounded time.

---

### Constraints and Assumptions

* Assume a global infrastructure with data centers in North America, Europe,
  and Asia-Pacific, with users routed to the nearest serving region.
* The LLM for answer generation may be a self-hosted model (70B+ parameters)
  or a mix of self-hosted and API-based models — the design must work with
  both and articulate the cost/latency/quality trade-offs.
* The web is adversarial: SEO spam, content farms, adversarial prompt
  injection in web pages (designed to manipulate the AI answer generator),
  and coordinated manipulation campaigns are all real threats.
* Some queries require real-time data (stock prices, weather, sports scores)
  that cannot come from the crawled web index — assume integration with
  live data APIs as a known requirement.
* Not in scope: advertising/monetization system. However, the SERP layout and
  ranking pipeline must be designed so that ad placement can be added without
  re-architecting.
* Not in scope: building the LLM itself. Assume a capable instruction-
  following model is available; design the retrieval, routing, and generation
  orchestration around it.

---

### What You Should Deliver

1. Requirement clarification and explicit assumptions.
2. High-level architecture: every major system (crawler, indexer, query
   processor, ranker, AI answer generator, serving layer), the data flows
   between them, and the offline vs. online split.
3. Crawling and indexing pipeline: how documents flow from discovery to
   indexed and searchable, including the ML-powered document understanding
   stages.
4. Index architecture: inverted index, vector index, knowledge graph, and
   real-time index — how they're built, sharded, replicated, and queried.
5. Query understanding pipeline: intent classification, query rewriting,
   entity linking — how it works end-to-end with latency budget.
6. Multi-stage ranking system: each stage's model, input features, latency
   budget, and how they chain together.
7. AI answer generation: the RAG pipeline end-to-end, including passage
   retrieval, context assembly, prompt construction, LLM inference, citation
   extraction, and grounding verification.
8. Query routing logic: how the system decides which queries get an AI answer
   vs. traditional results vs. instant answers, and the cost/quality
   trade-offs.
9. Serving architecture: how a query flows through the system from the edge
   to the response, including fan-out, latency budgets per stage, and
   graceful degradation.
10. Capacity estimates with arithmetic: storage for 200B documents (inverted
    index + vector index), serving infrastructure for 60K QPS, LLM inference
    fleet for 1B AI answers/day.
11. Freshness architecture: how breaking news goes from publication to
    searchable in under a minute.
12. Failure walkthroughs: a data center going down, the LLM serving layer
    degrading, a ranking model returning garbage, and a coordinated SEO
    spam attack.
13. Cost model: per-query cost breakdown (serving, LLM inference, bandwidth),
    and the levers to control it.
14. Evolution path: what ships in v1 (vertical search engine for a specific
    domain) vs. full web-scale system.
15. Trade-offs explicitly called out — especially: lexical vs. semantic
    retrieval balance, LLM answer quality vs. latency vs. cost, freshness
    vs. index completeness, and personalization vs. privacy.

---

### Expectations

* **Do the arithmetic.** Index sizes, shard counts, embedding storage, LLM
  inference GPU requirements, and cost-per-query must appear as numbers
  with the calculation shown.
* **Name concrete mechanisms** — BM25, HNSW, PageRank, cross-encoder
  reranking, CLIP embeddings, speculative decoding, KV cache reuse, prompt
  caching — and say what each buys and costs.
* **The AI answer pipeline is the hard part.** Don't hand-wave "we call an
  LLM" — show how passages are selected, how the prompt is constructed,
  how grounding is verified, and how hallucination is bounded.
* **Show the latency budget.** A search query has ≤ 500 ms. Show how that
  time is split across query understanding, retrieval, ranking, and (when
  triggered) answer generation.
* **Cost viability is a requirement, not an afterthought.** At 1B AI
  answers/day, even $0.001/answer is $1M/day. Show the math.
* Prefer a design that can start as a vertical search engine (e.g., a
  specific domain like code search or academic papers) and scale to
  general web search, over one that requires Google-scale infrastructure
  on day one.
* Assume this system will be operated by a team that grows from 10 to 100+
  engineers and must remain debuggable as complexity grows.

---
