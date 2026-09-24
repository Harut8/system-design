# Appendix H — GraphRAG: when a graph pays for itself

> **Why this appendix exists.** GraphRAG is the most-asked "advanced RAG" technique in 2026
> interviews and vendor pitches. It is also the one where costs vary the most. Full GraphRAG can
> cost ~1,000× more to index than vector RAG, and independent benchmarks show it often *loses* on
> ordinary questions. This page covers the query types where a graph really helps, the variants
> and what each costs, the evidence, and an order for adopting it that doesn't bet the budget
> up front.
>
> **Where the neighbouring detail lives:** hybrid retrieval and reranking in
> [`04-retrieval-hybrid-and-reranking.md`](04-retrieval-hybrid-and-reranking.md); query routing,
> decomposition and agentic multi-hop search in
> [`05-query-understanding.md`](05-query-understanding.md) §2 and §5.6; extraction at ingest and
> its fabrication risk in [`02-chunking-and-document-processing.md`](02-chunking-and-document-processing.md)
> §6.5; nugget evaluation for answers with no single reference in
> [`08-evaluation-methodology.md`](08-evaluation-methodology.md) §10.9; authorization inside
> retrieval in `04` §12; provenance and "derived data narrows readers" in
> [`17-safety-guardrails-and-prompt-injection.md`](17-safety-guardrails-and-prompt-injection.md) §4.7.

## Contents

1. [The two questions top-k can't answer](#1-the-two-questions-top-k-cant-answer)
2. [The variants, and where the LLM work happens](#2-the-variants-and-where-the-llm-work-happens)
3. [Cost, as arithmetic](#3-cost-as-arithmetic)
4. [What the benchmarks actually show](#4-what-the-benchmarks-actually-show)
5. [Decision table and adoption order](#5-decision-table-and-adoption-order)
6. [Failure modes specific to graphs](#6-failure-modes-specific-to-graphs)
7. [Evaluating it](#7-evaluating-it)
8. [Interview questions](#8-interview-questions)
9. [Sources](#9-sources)

---

## 1. The two questions top-k can't answer

> **In plain words.** Vector search returns the k passages most similar to the question. That
> fails when the answer isn't *in* any passage: when it is a summary of the whole corpus, or a
> chain of facts spread across documents that don't look like the question.
>
> **Real-world example.** "What are the main complaint themes across 40,000 support tickets this
> year?" No ticket contains the answer. Top-10 retrieval returns 10 tickets about whatever the
> query wording happens to match, and the model summarizes those 10 as if they were the whole
> picture.

| Query type | Example | Why top-k fails | What works |
|---|---|---|---|
| **Global / sensemaking** | "Main themes in this year's tickets?" | The answer is an aggregate over the whole corpus. No k is big enough | Precomputed or on-demand summaries over clusters (GraphRAG communities, RAPTOR trees) |
| **Multi-hop over entities** | "Which suppliers of our EU plants had a recall after onboarding?" | Hop 2's query ("supplier X recall") contains a name only hop 1 reveals | Graph traversal from entities, **or** agentic search (`05` §5.6) |
| **Aggregation over structured facts** | "How many contracts over $1M renew in Q3?" | Counting and filtering are not retrieval | **Not GraphRAG.** Use text-to-SQL/Cypher over the real database |
| Local fact lookup | "What is the refund window?" | It doesn't fail | Vector/hybrid. A graph only adds noise here (§4) |

The third row matters most in practice. If the facts already live in a CRM, ERP or database,
building an LLM-extracted graph from documents *about* that data is a slower, lossier copy of a
system you already have.

## 2. The variants, and where the LLM work happens

> **In plain words.** Every GraphRAG variant chooses where to spend LLM calls: at indexing time
> (expensive, stale when documents change) or at query time (cheap to index, slower to answer).

| Variant | Index build | Query time | LLM cost sits in |
|---|---|---|---|
| **GraphRAG** (Microsoft, Edge et al. 2024, "From Local to Global") | LLM extracts entities + relations from every chunk → Leiden community detection → **LLM writes a report per community**, at several levels | *Local*: entity neighbourhood + chunks. *Global*: map-reduce over community reports | **Indexing.** Every chunk and every community goes through an LLM |
| **LazyGraphRAG** (Microsoft Research, 2024-11) | **NLP noun-phrase extraction** (no LLM) → concept co-occurrence graph → Leiden communities, **no summaries** | LLM writes 3–5 sub-queries, then tests communities and chunks for relevance under a **relevance-test budget** you set | **Query time**, and capped by the budget |
| **LightRAG** (Guo et al. 2024) | LLM extracts entities + relations, keyed for two-level (specific / thematic) lookup. Built to update incrementally | Keyword → entity and relation lookup → chunks | Indexing, cheaper than GraphRAG (no community reports) |
| **HippoRAG / HippoRAG 2** (Gutiérrez et al. 2024–25) | LLM extracts an open knowledge graph (triples) linked to passages | **Personalized PageRank** from query entities spreads relevance across the graph to passages | Indexing (extraction). Query time is cheap graph math |
| **RAPTOR** (Sarthi et al. 2024), a non-graph alternative | Recursively cluster chunks and LLM-summarize each cluster into a tree | Retrieve across all levels of the tree | Indexing (summaries) |
| **KG + Cypher/SQL** | You already have a curated graph or database | LLM writes the query, the database answers | Query time, one call |

## 3. Cost, as arithmetic

Microsoft Research's LazyGraphRAG benchmark (AP News corpus) is the most-cited cost comparison:

| | Indexing cost | Global-query cost | Global-query quality |
|---|---|---|---|
| Vector RAG | ~$1.45 per M corpus tokens (reported) | low | poor on global questions |
| GraphRAG | ~$1,544 per M corpus tokens (reported), ~1,000× vector | high (map-reduce over every community report) | the reference |
| LazyGraphRAG | **same as vector RAG** (~0.1% of GraphRAG) | **>700× lower** than GraphRAG global search | comparable to GraphRAG global search |

The dollar figures used LLM prices from late 2024, so treat them as dated. **The ratios are the
durable part.** Put your own corpus in:

```python
def graphrag_index_cost(corpus_m_tokens: float, per_m_graphrag: float = 1_544.0,
                        per_m_vector: float = 1.45) -> dict[str, float]:
    """One full index build, USD. Re-run the GraphRAG line for every full re-index: community
    reports depend on the whole graph, so a changed document can change many reports."""
    return {"vector": corpus_m_tokens * per_m_vector,
            "graphrag": corpus_m_tokens * per_m_graphrag,
            "lazygraphrag": corpus_m_tokens * per_m_vector}   # NLP extraction, no LLM at index

# 50M-token corpus (e.g. ~40k tickets + a policy wiki):
#   vector ~$73, GraphRAG ~$77,000 per full build, LazyGraphRAG ~$73.
# A weekly full re-index of the GraphRAG line is ~$4M/year: the number that ends most proposals.
```

**Freshness is the hidden cost.** Vector RAG updates incrementally, one chunk ID at a time (`02`
§9). GraphRAG's community reports summarize *groups* of documents. One changed document can
change entity links, community membership and several reports. Either you re-index often and pay
the table above again, or the reports go stale while the chunks underneath are current. The
model then cites fresh chunks next to stale summaries that contradict them.

## 4. What the benchmarks actually show

> **In plain words.** Graphs help on questions that need connecting facts across documents. On
> ordinary questions they are no better, and often worse, because they pull in more loosely
> related text.

- **GraphRAG-Bench** (Xiang et al., *When to use Graphs in RAG*, ICLR 2026): on simple fact
  retrieval, **vanilla RAG was equal or better** than graph methods. Graph methods (LightRAG,
  HippoRAG, …) raised *evidence recall* but cut **context relevance to 36.9–54.6%, vs 62.9% for
  vanilla RAG**. They retrieve more of the right material *and* a lot more noise. Gains showed up
  on tasks that need non-local synthesis (multi-hop, aggregation across documents). Global
  community summaries **lost fine detail** on detail-centric questions.
- **RAG vs GraphRAG, a systematic evaluation** (Han et al., 2025): the two are complementary.
  Each wins different query types, so the combination that works is **routing**, not
  replacement.
- **LazyGraphRAG** (Microsoft): matched GraphRAG global search on global questions at the costs
  in §3. That result is what makes graphs affordable to try.
- **Vendor and blog claims** such as "86% vs 32% on multi-hop" or "vector RAG drops to 0% on 10+
  entities" come from enterprise marketing without public datasets. Don't quote them in a design
  doc. Run §7 on your own questions instead.

## 5. Decision table and adoption order

| Signal in your golden set / logs | Do this |
|---|---|
| Failures are on local lookups | Not a graph problem. Fix chunking, hybrid search, reranking (`02`, `04`) |
| Failures are "summarize across everything" questions | Try **LazyGraphRAG or RAPTOR** on that route |
| Failures are multi-hop, with a strong LLM available | Try **agentic search over your existing index** first (`05` §5.6). No new index |
| Multi-hop at high volume, where agent latency or cost is too high | Graph traversal (HippoRAG-style or LightRAG) on that route |
| The facts are in a database or CRM | Text-to-SQL/Cypher on the real system. No LLM-extracted graph |
| Small, high-value, slow-changing corpus with many global questions (policies, research archive) | Full GraphRAG is justifiable. Budget the re-index |

**Adoption order (MVP → Growth → Scale):**

1. **MVP.** Hybrid vector RAG. Add a query-type label to the golden set: *local*, *multi-hop*,
   *global*, *aggregation* (`08` §3). Measure each slice.
2. **Growth.** Only if the multi-hop or global slices fail *and* those queries matter to users
   (check logs for how often they occur): add a **route** (`05` §2), not a new pipeline for
   everything. Start with the lowest indexing cost: agentic search, then LazyGraphRAG or RAPTOR.
3. **Scale.** Full GraphRAG or LightRAG on the specific corpus that needs it, with a re-index
   budget, per-edge provenance, and ACL-partitioned summaries (§6).

## 6. Failure modes specific to graphs

| Failure | Mechanism | Guard |
|---|---|---|
| **Fabricated edges** | LLM extraction at ingest *generates* relations. A wrong edge ("A acquired B") gets retrieved and cited like source text. This is `02` §6.5's pattern-B risk, applied to a graph | Store the source span for every entity and edge. Cite spans, not edges. Sample-audit extraction precision |
| **Entity-resolution errors** | Two different "ACME"s merged, or one company split into three nodes | Resolution against a canonical ID list where you have one. Track merge counts per entity |
| **ACL leakage through summaries** | A community report summarizes chunks with **different permissions**. A user allowed to see one chunk reads a summary that includes the others | Readers of a summary = **intersection** of its sources' readers (below). If that set is too small, build communities per permission partition |
| **Stale summaries** | Reports lag behind changed chunks (§3) | Version reports with their source chunk hashes. Re-generate or drop reports whose sources changed |
| **Noise on simple queries** | Graph expansion pulls in related but irrelevant text (§4) | Route (§5). Rerank graph output like any other candidate set (`04` §7) |
| **Prompt injection by relation** | A poisoned document plants an entity or edge that later steers multi-hop answers | The corpus is untrusted input (`17` §5, §4.7). Provenance per edge makes the source traceable |

The ACL rule, in code. A summary is derived from all of its sources, so only principals who may
read *every* source may read the summary. This is the same "derived data narrows readers" rule
as `17` §4.7:

```python
def summary_readers(source_readers: list[frozenset[str]]) -> frozenset[str]:
    """Principals allowed to read a community report built from these sources."""
    if not source_readers:
        return frozenset()
    readers = source_readers[0]
    for r in source_readers[1:]:
        readers &= r
    return readers   # empty => nobody may read it: rebuild communities per permission partition
```

## 7. Evaluating it

- **Stratify by query type** (§5 step 1). An aggregate score hides the mechanism: GraphRAG can
  win the global slice and lose the local one, and the average will say "no change" (`04` §13.4).
- **Measure context relevance, not only recall.** GraphRAG-Bench's key finding is higher recall
  with lower relevance. A recall-only eval would call that a win.
- **Global questions have no single reference answer.** Use nugget recall (`08` §10.9): list what
  a good summary must mention and check coverage, plus faithfulness (`08` §10.2) against the
  retrieved reports *and* their source chunks.
- **Report cost per slice**: index cost, re-index cost per week, query tokens. That goes next to
  quality, as in §3.

## 8. Interview questions

1. *"Would you use GraphRAG for our support knowledge base?"* Ask which questions fail today.
   Local lookups: no. "Themes across tickets": try LazyGraphRAG or RAPTOR on a route, since
   indexing costs about the same as vector. Only consider full GraphRAG if the global slice
   still fails and the corpus changes slowly.
2. *"GraphRAG improved recall but answers got worse. Why?"* GraphRAG-Bench's pattern: graph
   expansion adds loosely related text, so context relevance fell (62.9% → 37–55%). Rerank the
   graph candidates and route only multi-hop and global queries to the graph.
3. *"How do permissions work with community summaries?"* A summary's readers are the
   intersection of its sources' readers. If that intersection is empty or too small, partition
   the graph by permission before building communities. Otherwise summaries leak.
4. *"Multi-hop question: graph or agent?"* An agent over the existing index needs no new
   infrastructure, but costs 10–60× per question in tokens and takes tens of seconds
   (`05` §5.6). A graph moves that cost into indexing. Pick by volume, latency budget and how
   fast the corpus changes.

## 9. Sources

Edge et al., *From Local to Global: A Graph RAG Approach to Query-Focused Summarization*
(Microsoft, 2024); Microsoft Research, *LazyGraphRAG: Setting a new standard for quality and cost*
(2024-11); Xiang et al., *When to use Graphs in RAG: A Comprehensive Analysis for Graph
Retrieval-Augmented Generation* (ICLR 2026, GraphRAG-Bench); Han et al., *RAG vs. GraphRAG: A
Systematic Evaluation and Key Insights* (2025); Guo et al., *LightRAG* (2024); Gutiérrez et al.,
*HippoRAG* (2024) and *HippoRAG 2* (2025); Sarthi et al., *RAPTOR* (ICLR 2024). The cost figures
are as reported in the LazyGraphRAG benchmark coverage, dated 2024 prices. Numbers were collected
2026-09-24.
