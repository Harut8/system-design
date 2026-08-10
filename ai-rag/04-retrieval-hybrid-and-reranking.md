# 04 — Retrieval: hybrid and reranking

> **Prerequisites:** [`00-mental-models.md`](00-mental-models.md) (the pipeline as dataflow and the
> recall ceiling — this chapter is that ceiling made operational),
> [`01-embeddings-and-representation.md`](01-embeddings-and-representation.md) (§3 on asymmetric
> embedding especially — a hybrid system that gets `input_type` wrong is measuring the wrong dense
> branch), [`02-chunking-and-document-processing.md`](02-chunking-and-document-processing.md) (§4.3 —
> the two branches want *different analyzed text over the same canonical chunk*, and §10.4's
> retrieval-time near-duplicate suppression lands in §11 here),
> [`03-indexing-and-vector-stores.md`](03-indexing-and-vector-stores.md) (the dense branch is that
> index; §3's operating point is this cascade's first-stage latency budget, and §7's filtered-search
> problem shows up again in §12),
> [`../databases/03-access-methods-and-table-scans.md`](../databases/03-access-methods-and-table-scans.md)
> (two access paths over the same data, combined — the framing is older than RAG and better developed
> there),
> [`../python-mastery/29-async-patterns-and-pitfalls.md`](../python-mastery/29-async-patterns-and-pitfalls.md)
> (§10's parallel branches with timeouts is a bounded-concurrency-with-cancellation problem, and
> getting it wrong is how a p99 becomes a p50).
>
> **Feeds into:** [`05-query-understanding.md`](05-query-understanding.md) (rewriting and
> decomposition are *more branches into the same fusion step* — §5 is the machinery they plug into),
> [`06-context-engineering.md`](06-context-engineering.md) (§6's candidate budget hands off to that
> chapter's token budget; they are the same budget seen from two sides),
> [`08-evaluation-methodology.md`](08-evaluation-methodology.md) (§13's stage-wise ablation protocol
> is the honest version of "did that help?"),
> [`10-llm-observability-and-tracing.md`](10-llm-observability-and-tracing.md) (a span per branch per
> stage — the cascade in §1 *is* the span tree),
> [`12-serving-latency-and-caching.md`](12-serving-latency-and-caching.md) (§10's budget is where
> timeouts, fallback and streaming get their numbers),
> [`13-agents-and-tool-calling.md`](13-agents-and-tool-calling.md) (multi-hop retrieval runs this
> cascade N times, so everything here multiplies).
>
> **THESIS:** retrieval is a **cascade**, and a cascade has exactly one property that governs its
> design: *only the first stage can add candidates; every later stage can only reorder or discard.*
> That makes the first stage's recall a hard ceiling on everything downstream, and it makes every
> later stage a place where you can afford to spend far more per document, because there are far
> fewer documents.
>
> Hybrid retrieval exists because lexical and dense search fail on **disjoint** query classes, so
> unioning them raises the ceiling that no amount of reranking could. Reranking exists because a
> cross-encoder is roughly two orders of magnitude more accurate per comparison and roughly four
> orders more expensive, which is affordable over 100 documents and impossible over 10 million.
>
> Both follow from the same shape as `03` §6.1's quantize-then-rescore: **cheap and wide, then
> expensive and narrow.** So the design question is never "which retriever is best." It is *what is
> the cheapest cascade whose first-stage recall clears my target at my latency budget* — and the
> answer is a set of numbers you measure, not a stack you adopt.

---

## Contents

1. [The cascade, and the one property that governs it](#1-the-cascade-and-the-one-property-that-governs-it)
2. [BM25: what it computes, and why it refuses to die](#2-bm25-what-it-computes-and-why-it-refuses-to-die)
3. [Where dense retrieval fails, and why the failures are complementary](#3-where-dense-retrieval-fails-and-why-the-failures-are-complementary)
4. [Learned sparse: the third branch](#4-learned-sparse-the-third-branch)
5. [Fusion: rank versus score, and RRF in detail](#5-fusion-rank-versus-score-and-rrf-in-detail)
6. [The candidate budget — the parameter nobody reports](#6-the-candidate-budget--the-parameter-nobody-reports)
7. [Reranking with cross-encoders](#7-reranking-with-cross-encoders)
8. [Late interaction and LLM rerankers — the rest of the ladder](#8-late-interaction-and-llm-rerankers--the-rest-of-the-ladder)
9. [The reranker landscape as an interface-and-constraint table](#9-the-reranker-landscape-as-an-interface-and-constraint-table)
10. [The latency budget, as arithmetic](#10-the-latency-budget-as-arithmetic)
11. [Diversity and deduplication at merge time](#11-diversity-and-deduplication-at-merge-time)
12. [Filtering and authorization inside the cascade](#12-filtering-and-authorization-inside-the-cascade)
13. [Evaluating a cascade without fooling yourself](#13-evaluating-a-cascade-without-fooling-yourself)
14. [Cost model for the retrieval layer](#14-cost-model-for-the-retrieval-layer)
15. [Anti-patterns](#15-anti-patterns)
16. [Mental models — the compressed set](#16-mental-models--the-compressed-set)
17. [Lab exercises](#17-lab-exercises)

---

## 1. The cascade, and the one property that governs it

```
    corpus: 10,000,000 chunks
        │
        ├── lexical branch  ──┐    ~50–200 candidates each
        ├── dense branch    ──┤    cost: ~O(log N) per branch
        └── (sparse/rewrite) ─┘
                              │
                     ┌────────▼────────┐
                     │  fusion (§5)    │   union, then rank
                     └────────┬────────┘
                              │   ~50–200 candidates
                     ┌────────▼────────┐
                     │  rerank (§7)    │   cost: O(candidates) forward passes
                     └────────┬────────┘
                              │   ~5–20 passages
                     ┌────────▼────────┐
                     │  dedup / MMR    │   §11
                     └────────┬────────┘
                              │
                          generation (06, 07)
```

### 1.1 The governing property

**Only stage one can add candidates.** Fusion unions what the branches produced. Reranking reorders
what fusion produced. Diversification discards. Nothing downstream can recover a passage that no
branch retrieved.

So:

```
    end_to_end_recall ≤ recall(union of first-stage branches at their candidate depths)
```

That inequality is the entire reason this chapter is organized the way it is. It has three immediate
consequences that between them explain most of what teams get wrong:

1. **First-stage recall is the only place ceiling-raising work can happen.** Adding a reranker to a
   pipeline whose first stage has recall@100 of 0.72 buys you a better ordering of a set that is
   missing 28% of the answers. That is worth something. It is not worth what people expect, and it is
   not where the effort should have gone.
2. **Adding a branch is the only cheap way to raise the ceiling.** More `k` on one branch has
   diminishing returns; a branch that fails on different queries has independent returns (§3.3).
3. **Downstream stages should be evaluated on ordering metrics and upstream stages on recall.**
   Reporting nDCG@10 for a change to your BM25 analyzer is measuring the wrong thing through two
   layers of indirection (§13.2).

### 1.2 The cost asymmetry that makes cascades work

| Stage | Documents scored | Cost per document | Why it can't be the previous stage |
|---|---:|---|---|
| ANN / inverted index | ~10⁷ (implicitly) | sublinear — you never score most of them | — |
| Fusion | ~10² | arithmetic | — |
| Cross-encoder rerank | ~10² | one transformer forward pass over (query ⊕ doc) | there is no index over joint encodings; you cannot precompute a score for a query you haven't seen |
| LLM rerank / generation | ~10¹ | an LLM call | cost and latency |

The middle row is the load-bearing one and worth stating precisely, because "just use the better
model for retrieval" is a recurring bad idea that this kills.

A **bi-encoder** (an embedding model) encodes query and document *independently*. That
independence is what makes indexing possible: document vectors are computed once at ingest, and
query time is one encode plus a graph traversal. The price is that the model never sees the query
and the document together, so it must compress each into a fixed vector that is useful against every
possible counterpart.

A **cross-encoder** encodes the pair *jointly* — full attention between query tokens and document
tokens. It is far more accurate for exactly that reason. And it is unindexable for exactly that
reason: the score depends on the pair, so nothing can be precomputed, so scoring N documents costs N
forward passes. At 10M documents that is not a latency problem, it is a category error.

**The cascade is the resolution of that tension, and it is not a hack.** It is the same structure as
`03` §6.1 (quantized traversal, exact rescore), as a database's index-scan-then-filter, and as a
compiler's fast-path-then-slow-path. Cheap and wide, then expensive and narrow, with each stage's
job being *to hand the next stage a small enough set that the next stage's cost is affordable*.

---

## 2. BM25: what it computes, and why it refuses to die

In 2023 the fashionable position was that lexical search was legacy. It isn't, and understanding
*why* tells you exactly when to lean on it.

### 2.1 The formula, and how to read it

```
                  N        f(qᵢ, D) · (k₁ + 1)
score(D, Q) =    Σ  IDF(qᵢ) · ───────────────────────────────────────
                 i=1          f(qᵢ, D) + k₁ · (1 − b + b · |D|/avgdl)
```

Three parts, each doing a distinguishable job:

- **`IDF(qᵢ)` — term importance *within the corpus*.** Rare terms contribute more. This is what makes
  BM25 handle out-of-domain vocabulary well: a product code that appears in four documents out of ten
  million gets an enormous weight, automatically, with no training and no model.
- **`f(qᵢ, D)` with `k₁` — term frequency, saturating.** `k₁` controls how fast the return on
  repetition flattens. A document mentioning a term twenty times is not twenty times more relevant
  than one mentioning it once. Elasticsearch's default is `k₁ = 1.2`.
- **`|D|/avgdl` with `b` — length normalization.** Long documents accumulate term matches by being
  long; `b` discounts that. Elasticsearch's default is `b = 0.75`, with `b = 0` disabling
  normalization entirely.

Both are per-field settings in Elasticsearch and both are worth touching *once*, deliberately: short
uniform-length chunks (`02`'s output) barely need length normalization, so lowering `b` is
occasionally a real, free improvement.

### 2.2 The sharp observation: over RAG chunks, BM25 is mostly IDF

Qdrant makes a point in its BM42 write-up that reorganizes how you think about the lexical branch in
a chunked corpus:

> The term frequency in the document is always 0 or 1, and the relative length of the document is
> always 1. So, the only part of the BM25 formula that is still relevant for RAG is IDF.

Chunks are short and roughly uniform by construction (`02` §5). Within a 300-token chunk, most query
terms appear zero or one times, and `|D|/avgdl ≈ 1`. The saturation term and the length-normalization
term are both doing approximately nothing. What remains is a rare-term detector.

Two things follow:

- **Tuning `k₁` and `b` on a chunked corpus is mostly wasted effort.** They govern behaviours that
  short uniform documents don't exhibit. Spend the effort on the analyzer instead (§2.4).
- **IDF is corpus statistics, which is an operational problem, not a modelling one.** IDF depends on
  the whole collection. In a distributed or continuously-updated index, keeping it correct requires
  either global statistics or an approximation — this is why Qdrant built IDF computation into the
  engine rather than into the embedding step, so that sparse vectors can stream in while IDF stays
  current. If your lexical branch computes IDF at ingest time from a snapshot, it drifts as the
  corpus grows, and it drifts most for exactly the new rare terms you most wanted it to weight.

### 2.3 What BM25 is actually good at

Not "keyword matching" in the vague sense. Specifically:

| Query class | Why lexical wins |
|---|---|
| Identifiers — SKUs, error codes, CVE numbers, ticket IDs, function names | never in the embedding model's training vocabulary; enormous IDF |
| Rare proper nouns — customer names, internal project codenames | same |
| Exact-phrase requirements — legal, compliance, "the exact wording of clause 4.2" | dense retrieval is *designed* to be paraphrase-invariant, which is the wrong invariance here |
| Negation and precise qualifiers, sometimes | not reliably, but dense often does strictly worse |
| Very long-tail domain jargon | any term the embedding model saw rarely is poorly placed in its space |
| Zero-shot on a brand-new domain | no training, no fine-tuning, no drift, works on day one |

And the operational properties are hard to beat: no GPU, no inference cost per document at ingest,
interpretable scores you can explain to a user, and a debugging story that consists of looking at
which terms matched.

### 2.4 The analyzer is the part that matters

Because §2.2 says the parameters barely matter, the lexical branch's quality lives almost entirely in
tokenization and normalization: casing, stemming or lemmatization, stopwords, how you split
`user_id`, `UserID`, `user-id`, and whether `10-K` survives as a token.

This is `02` §4.3's point arriving with consequences. **Keep one canonical chunk text and derive both
branches' analyzed forms from it.** If the analyzer's output becomes the embedded text, you have
silently degraded the dense branch — stemmed, stopword-stripped text is not what the embedding model
was trained on. That regression is invisible in the lexical metrics and shows up only as
unexplained dense-branch weakness.

---

## 3. Where dense retrieval fails, and why the failures are complementary

### 3.1 The failure list

| Failure | Mechanism |
|---|---|
| Rare identifiers and codes | out-of-vocabulary or near-random position in embedding space (`01` §4) |
| Domain jargon the model didn't see | no meaningful geometry for the term |
| Exact-phrase requirements | the model is trained toward paraphrase invariance — this is the objective working as designed |
| Numeric and temporal precision ("Q3 2024", "> 500 mg") | numbers are poorly represented by text embedding objectives |
| Very short queries | little signal to pool; `01` §2.3's hubness bites hardest here |
| Very long chunks | pooling dilutes (`02` §5.1) |
| Negation | notoriously weak; "not X" often embeds near "X" |
| Multilingual mismatch outside the training pairs | depends entirely on the model (`01` §6) |

### 3.2 What dense wins that lexical structurally cannot

- **Vocabulary mismatch.** "How do I cancel my subscription?" against a document titled *"Terminating
  a recurring plan"* shares almost no terms. This is the single biggest class in real user traffic
  and it is why dense retrieval displaced pure lexical at all.
- **Paraphrase and intent.** The same question asked five ways retrieves the same chunk.
- **Cross-lingual**, with a multilingual model.
- **Conceptual and thematic queries**, where no specific term is the point.
- **Robustness to typos**, partially and unreliably.

### 3.3 The complementarity claim, and how to verify it rather than assume it

The argument for hybrid is not "two is better than one." It is that **the failure sets are largely
disjoint**, so the union's recall is meaningfully higher than either branch's — as opposed to two
dense models, whose failures correlate heavily and whose union buys almost nothing.

This is checkable in an hour and you should check it, because the size of the effect on *your* corpus
decides whether hybrid is worth its complexity:

```python
# Per-query overlap analysis. This is the measurement that decides whether
# hybrid is worth building for you, and it needs no labels beyond a golden set.
def branch_complementarity(golden, lexical_hits, dense_hits, k=50):
    """Each *_hits: {query_id: set(chunk_ids)} at depth k.
    golden: {query_id: set(relevant chunk_ids)}
    """
    both = lex_only = dense_only = neither = 0
    for qid, rel in golden.items():
        L = bool(rel & lexical_hits[qid])
        D = bool(rel & dense_hits[qid])
        both += L and D
        lex_only += L and not D
        dense_only += D and not L
        neither += not L and not D
    n = len(golden)
    return {
        "recall_lexical":  (both + lex_only) / n,
        "recall_dense":    (both + dense_only) / n,
        "recall_union":    (both + lex_only + dense_only) / n,
        "lexical_only":    lex_only / n,   # ← the value of the lexical branch
        "dense_only":      dense_only / n,
        "neither":         neither / n,    # ← your ceiling. Nothing downstream fixes this.
    }
```

Read two cells:

- **`lexical_only`** is what the lexical branch is buying you. If it is 1%, hybrid is a lot of
  machinery for very little; if it is 12%, it is the highest-leverage change available.
- **`neither`** is your first-stage ceiling gap. This is the number to attack, and no reranker
  touches it. If `neither` is large, the fix is upstream — chunking (`02`), the embedding model
  (`01`), or query understanding (`05`) — not here.

Stratify by query type (`02` §11.6). The aggregate hides the structure: complementarity is typically
concentrated in the identifier-and-jargon stratum, and if that stratum is 5% of your traffic but 40%
of your support escalations, the aggregate is actively misleading.

---

## 4. Learned sparse: the third branch

Between "term statistics with no model" and "dense vector with no terms" sits a family that produces
sparse term-weighted vectors *from* a model. Same inverted-index machinery, learned weights.

### 4.1 SPLADE

*SPLADE v2: Sparse Lexical and Expansion Model for Information Retrieval* (Formal, Lassance,
Piwowarski, Clinchant — arXiv 2109.10086). A transformer projects each token onto the vocabulary and
sparsifies, producing a bag-of-words representation with learned weights — **including terms not
present in the text** (expansion). So it captures some vocabulary-mismatch cases while remaining
searchable with an inverted index and remaining interpretable.

Qdrant's critique is worth carrying because it is specific about operational costs rather than
quality:

- **Tokenizer mismatch.** Transformer tokenizers split out-of-vocabulary words into subwords or emit
  `[UNK]`. Fine for language modelling, destructive for retrieval — and identifiers are precisely
  the OOV case where you most wanted the lexical branch to work.
- **Expansion is expensive.** More non-zero terms per document means more storage and slower
  retrieval, and there is no principled stopping point: expand more and you catch more relevant
  terms and more irrelevant ones.
- **Domain and language dependence.** Trained on specific corpora, without corpus statistics to adapt
  with, so a new domain needs fine-tuning. BM25 needs neither.
- **Inference cost.** Encoding every document through a model at ingest, typically wanting a GPU.

### 4.2 BM42, and the more useful lesson in it

Qdrant's BM42 (2024) keeps IDF as the corpus-statistics term and replaces the within-document term
importance with **the attention weights from the `[CLS]` row of the last transformer layer**, averaged
over heads:

```
score(D, Q) = Σ IDF(qᵢ) · Attention(CLS, qᵢ)
```

Attractive properties: it needs no additional training (any transformer gives you the attention
matrix), it is extremely sparse (Qdrant reports an average of 5.6 elements per document on the quora
dataset, with a full sparse index of ~13 MB for ~530k documents at `uint8`), and it keeps IDF's
corpus adaptivity.

**The more valuable part of that article is the correction notice**, and it belongs in this chapter
for methodological reasons rather than for BM42's sake. The published benchmark table now carries:

> Please note that the benchmark section of this article was updated after the publication due to a
> mistake in the evaluation script.

The error was in the *baseline*: incorrect character escaping in the tantivy BM25 setup understated
its recall@10 as 0.71, when correctly configured it is 0.89. Qdrant's own corrected conclusion, on
their own method, in their own article: **"When used properly, BM25 with tantivy achieves the best
results."** (Corrected quora figures: precision@10 — BM25/tantivy 0.45, BM25/sparse 0.45, BM42 0.49;
recall@10 — BM25/tantivy 0.89, BM25/sparse 0.83, BM42 0.85. Qdrant also notes neither works well
alone in production.)

Three things to take from this, none of which are about BM42:

1. **A weak baseline is the most common way to produce a false positive**, and it is much harder to
   notice than a bug in the thing you're proposing, because the result confirms what you expected.
2. **Publishing the scripts is what made the error findable.** Qdrant published theirs, which is why
   this is a story about a correction rather than a story about a claim nobody could check.
3. **Tune the baseline as hard as the challenger.** This is `../python-mastery/31-measurement-methodology.md`'s
   argument, and §13.1 restates it as a rule for this chapter's ablations.

### 4.3 When learned sparse is worth it

Realistically: when your store supports it natively (so it is a configuration change, not a
pipeline), when the domain is close to the model's training distribution, and when you have measured
that plain BM25 plus dense leaves a gap that the third branch closes. In practice most teams should
get BM25 + dense + a reranker working and measured first — that cascade is cheap, well understood,
and captures most of the available gain. Learned sparse is a third-order optimization presented as a
first-order one.

---

## 5. Fusion: rank versus score, and RRF in detail

Two branches returned two ranked lists. Merge them.

### 5.1 Why score-based fusion is harder than it looks

The obvious move — normalize both branches' scores and take a weighted sum — runs into three problems
that are not fixable by trying harder:

- **The scales are incommensurable.** Cosine similarity is bounded in [−1, 1] with a distribution that
  depends on the embedding model's anisotropy (`01` §2.3). BM25 is unbounded above and depends on
  corpus statistics and query length. There is no principled conversion.
- **Min-max normalization over a result set is unstable.** Normalizing by the max and min *of the
  returned candidates* means a document's normalized score depends on which other documents came
  back. Same document, same query, different `k` — different score. This makes scores
  non-comparable across queries, which quietly breaks any threshold you set on them.
- **Distributions shift.** The dense branch's score distribution moves when you change the model
  (`01` §12); the lexical branch's moves as the corpus grows and IDF changes (§2.2). Any tuned
  weighting decays.

Score fusion can be made to work — with per-branch calibration on a dev set, and re-calibration on
every model or corpus change. It is a maintenance commitment, and it should be entered deliberately.

### 5.2 Reciprocal Rank Fusion

RRF (Cormack, Clarke & Buettcher, SIGIR 2009) discards scores and uses only ranks:

```
                    1
RRF(d) =    Σ    ────────────
          q ∈ Q   k + rank_q(d)
```

where `rank_q(d)` is `d`'s 1-based rank in branch `q`'s result list, and `d` contributes nothing from
branches that didn't return it. Elasticsearch documents it in exactly this form and states the design
claim plainly: *"RRF requires no tuning, and the different relevance indicators do not have to be
related to each other to achieve high-quality results."*

```python
def rrf(branch_results: dict[str, list[str]], k: int = 60,
        weights: dict[str, float] | None = None) -> list[tuple[str, float]]:
    """branch_results: {branch_name: [doc_id, ...]} ordered best-first.
    Ranks are 1-based. Documents missing from a branch simply contribute 0 from it.
    """
    scores: dict[str, float] = {}
    for branch, ranked in branch_results.items():
        w = (weights or {}).get(branch, 1.0)
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + w / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])
```

**What `k` actually does.** It is a rank-flattening constant. Small `k` makes the top ranks dominate:
at `k = 1`, rank 1 scores 0.5 and rank 2 scores 0.33 — a single branch's top hit can win outright.
Large `k` flattens the curve, so *agreement across branches* matters more than any one branch's
confidence: at `k = 60`, rank 1 scores 0.0164 and rank 2 scores 0.0161, nearly identical, and a
document at rank 5 in both branches beats a document at rank 1 in one.

The conventional 60 comes from the original paper and is Elasticsearch's default for `rank_constant`
(which must be ≥ 1). It is a reasonable default precisely because it biases toward consensus, which is
what you want when you cannot trust either branch's calibration.

**What RRF gives up.** All magnitude information. RRF cannot distinguish "rank 1 is a perfect match"
from "rank 1 is the least-bad of a uniformly terrible set," because both are rank 1. Two practical
consequences: you cannot threshold RRF scores to decide "we found nothing useful, don't answer"
(a real requirement — see `06` and `07`), and if one branch is much stronger for a query class, RRF
dilutes it by construction.

**Weighting.** The standard fix, and the one Elasticsearch's equal-weight child retrievers do *not*
give you out of the box: weight branches in the sum, as in the code above. Fit the weights on a dev
set, per stratum if your strata differ (they usually do — lexical deserves more weight on the
identifier stratum). Re-fit when a model or the corpus changes, and note in your runbook that you
now own a tuned parameter.

### 5.3 The structural insight: with a reranker, fusion is a recall problem

This is the most useful thing in this section and it collapses a lot of agonizing.

If a reranker follows fusion, **the fused ordering is thrown away**. The reranker re-scores every
candidate jointly and produces its own order. So fusion's only remaining job is to get the right
documents *into* the candidate set. It is a recall problem, not a ranking problem.

Therefore:

- **Evaluate fusion with recall@candidate_depth, not nDCG.** You are measuring set membership.
- **Prefer RRF's robustness over score fusion's precision.** The precision is about to be discarded.
- **Don't spend a week tuning RRF weights if a reranker follows.** Spend it on candidate depth (§6),
  which directly moves the thing fusion is being measured on.
- **If there is no reranker, invert all of the above.** Then fusion *is* your final ranking, magnitude
  information genuinely matters, and calibrated score fusion may earn its maintenance cost.

### 5.4 The rank-window trap

Elasticsearch's `rank_window_size` sets how deep each branch goes before fusion, defaults to the
search's `size`, and must be ≥ `size`. Its docs add the detail that catches people: if a kNN
retriever's `k` exceeds `rank_window_size`, the results are truncated to `rank_window_size`.

Leaving it at the default means fusing top-10 lists, which produces a fused set of at most 20 and
usually fewer — and then feeding a reranker 15 candidates. Almost all of the benefit of the whole
cascade is gone, silently, from one unset parameter. §6 is entirely about not doing this.

---

## 6. The candidate budget — the parameter nobody reports

Every "hybrid beat dense by 4 points" claim has a hidden variable: how deep each branch went. It is
the most under-reported parameter in retrieval and it confounds a large fraction of published
comparisons.

### 6.1 The three depths, which are three different numbers

```
    branch_depth      how many candidates EACH branch returns        (e.g. 100)
    fusion_depth      how many survive fusion into reranking          (e.g. 100)
    final_k           how many passages reach the prompt              (e.g. 10)
```

They are routinely conflated as "k" and they trade off against completely different things:

| Depth | Raising it costs | Raising it buys |
|---|---|---|
| `branch_depth` | index latency (roughly `ef_search`-shaped — `03` §4.2) | first-stage recall — the ceiling |
| `fusion_depth` | reranker latency and dollars, **linearly** | more chances for the reranker to find the right passage |
| `final_k` | prompt tokens, generation latency, distraction | recall in the prompt |

### 6.2 How to choose each

**`branch_depth`:** sweep it against first-stage recall and find the knee. The curve is steep to
~50 and flattens hard after ~100–200 for most corpora, because ANN and BM25 both surface the easy
hits early. Measure yours; the knee is corpus-dependent and it is the single number that sets your
ceiling.

**`fusion_depth`:** bounded above by reranker cost and latency, which is linear in candidates (§10).
This is where a real budget decision happens. Anthropic's contextual-retrieval experiments are the
useful public anchor here: they report that passing **top-20 chunks to the model is more effective
than top-10 or top-5**, and that their best configuration combined contextual embeddings, contextual
BM25, and a reranking step with 20 chunks in the prompt — reducing the top-20 retrieval failure rate
by **67% (5.7% → 1.9%)** when reranking was added. Note that is `final_k = 20` after reranking; the
`fusion_depth` feeding the reranker was larger.

**`final_k`:** this is `06-context-engineering.md`'s decision, not this chapter's, because it is a
token-budget question. The relevant handoff: more passages is not monotonically better — beyond some
point you are spending tokens to add distractors, and `06` covers where that point is. `02` §11.3's
rule applies here with force: **compare configurations at a fixed token budget, not a fixed `k`**,
or chunk size confounds everything.

### 6.3 Report all three, always

```
"recall@100 = 0.94"                                    ← meaningless
"first-stage recall@100 (branch_depth=100 per branch,
 2 branches, RRF k=60, fusion_depth=100) = 0.94"       ← a result
```

Make your retrieval config a serializable object that gets logged with every eval run and attached
to every number you quote. This is the same discipline `02` §11 imposes for chunking and it exists
for the same reason: the numbers are meaningless without it, and six weeks later nobody remembers.

---

## 7. Reranking with cross-encoders

### 7.1 What it is, restated

One transformer forward pass over the concatenated `(query, document)` pair, with full attention
between them, producing a relevance score. §1.2 covered why this cannot be an index. What it buys is
that the model can condition on the query when deciding what matters in the document — which is
exactly the information a bi-encoder threw away at ingest time.

### 7.2 Where it helps most, and where it does nothing

**Helps most:**
- when `fusion_depth` is much larger than `final_k`, so there is real reordering to do;
- when queries are long or multi-clause, where a single pooled query vector was a bad summary;
- when the corpus contains many topically-similar chunks, so bi-encoder scores are compressed into a
  narrow band and small errors reorder heavily;
- when the dense model is generic and the domain is not — the reranker can be domain-tuned much more
  cheaply than the embedding model, because reranking has no O(corpus) migration cost. **This is an
  underrated architectural property:** swapping the reranker is a config change; swapping the
  embedding model is a re-embedding of the corpus (`01` §12).

**Does nothing:**
- when first-stage recall is the binding constraint (§1.1) — reordering a set that lacks the answer;
- when `fusion_depth ≈ final_k` — nothing to reorder;
- when the candidates are genuinely interchangeable (near-duplicates), where §11 is the fix.

### 7.3 The operational details that actually bite

**Chunking inside the reranker.** Rerankers have context limits, and documents that exceed them are
chunked *by the API*, scored per chunk, and combined. Cohere documents the procedure exactly: for
`rerank-v4.0` (pro and fast), documents are broken into 32,764-token chunks (32,768 context minus 4
reserved tokens); for `rerank-v3.5` and `rerank-v3.0`, 4,093-token chunks. Each chunk is scored with
the query prepended, and the document's score is the **max over its chunks**.

Two consequences. First, max-pooling means one strongly-matching chunk carries a long document —
which is usually what you want for retrieval and is worth knowing when a long document ranks
surprisingly high. Second, `max_chunks_per_doc` defaults to 1, so by default long documents are
effectively truncated. If you are reranking full documents rather than `02`-sized chunks, you must set
it — and it counts against your document budget (below).

**Hard request limits.** These are not advisory; they shape your `fusion_depth`:

| Constraint | Cohere Rerank | Voyage rerank-2.5 / 2.5-lite |
|---|---|---|
| Max documents per request | 10,000, and `n_docs × max_chunks_per_doc ≤ 10,000` | 1,000 |
| Context length | 32,768 (v4.0) / 4,096 (v3.5, v3.0) | 32,000 |
| Max query tokens | half the context: 16,384 (v4.0) / 2,048 (v3.5) — longer queries are truncated | 8,000 |
| Aggregate limit | — | `query_tokens × n_docs + Σ doc_tokens ≤ 600,000` |
| Long-document handling | auto-chunk, score each, take max | truncation (default on) |

Voyage's aggregate limit is worth working through, because it is the one that turns into a hard
architectural ceiling:

```
query = 50 tokens, chunks = 512 tokens each
  100 chunks:  50×100  + 100×512  =   5,000 +  51,200 =  56,200   ✓
1,000 chunks:  50×1000 + 1000×512 =  50,000 + 512,000 = 562,000   ✓ (just under)
```

So ~1,000 chunks of 512 tokens is roughly the ceiling in one call. Above that you are batching, and
batching a reranker across requests means the scores come from independent calls — which is fine,
because cross-encoder scores are absolute (per pair) rather than relative to the batch. That is a
genuine and useful property: **you can shard a rerank across requests and merge by score**, unlike
listwise rerankers (§8.2).

**Structured documents.** Both Cohere and Voyage support reranking structured data. Cohere's guidance
is specific and easy to get wrong: pass a list of YAML strings, and **preserve key order**
(`yaml.dump(..., sort_keys=False)`) because long documents get truncated and the keys you care about
must come first. That "order matters because truncation" argument generalizes: whatever you put in
the first 500 tokens of a document is what the reranker is most certain to see.

### 7.4 A note on where the reranker sits relative to filters

Reranking is *after* filtering, always. A reranker that scores documents the user may not see, so
that you can then filter them out, has spent money to rank inaccessible content and has probably
reduced the number of results the user gets. §12 covers this properly; it is listed here because it
is the most common cascade-ordering bug.

---

## 8. Late interaction and LLM rerankers — the rest of the ladder

Cross-encoders are one rung. The ladder has four, and they trade off along the same axis: how much
query-document interaction the model gets, versus how much can be precomputed.

| Rung | Interaction | Precomputable | Cost per candidate | Storage cost |
|---|---|---|---|---|
| Bi-encoder (dense retrieval) | none — independent encodings | everything | ~0 (it's the index) | 1 vector/chunk |
| Late interaction (ColBERT) | per-token, after independent encoding | document token vectors | low — MaxSim over vectors | **~1 vector per token** |
| Cross-encoder | full joint attention | nothing | one forward pass | 0 |
| LLM reranker | full, with instructions and reasoning | nothing | an LLM call | 0 |

### 8.1 Late interaction

*ColBERT: Efficient and Effective Passage Search via Contextualized Late Interaction over BERT*
(Khattab & Zaharia, arXiv 2004.12832) encodes query and document independently into **per-token**
vectors, then scores with MaxSim: for each query token, take the maximum similarity against any
document token, and sum.

```
score(Q, D) = Σ        max      sim(q_i, d_j)
             i∈|Q|   j∈|D|
```

The interaction is deferred to scoring time — hence "late" — so document encodings are still
precomputable, but the model retains token-level matching detail that pooling destroys. That makes it
notably strong on exactly the cases where pooled dense vectors are weakest: long documents, and
queries where one specific term must match.

The cost is storage, and it is large: one vector per token instead of one per chunk. Even with the
aggressive compression the ColBERT line of work has developed, the multiplier is real. The
architectural niche is therefore **first-stage retrieval or cheap second-stage reranking where you
can afford the index**, not a drop-in replacement for a bi-encoder. Treat it as a genuine option to
measure, with §14's cost model applied honestly to the storage line.

### 8.2 LLM rerankers

Ask a model to score or order the candidates. Flexible — you can express criteria that no trained
reranker was built for ("prefer recent policy documents over superseded ones", "prefer primary
sources") — and instruction-following variants are increasingly standard (Voyage's rerank-2.5 line
accepts instructions appended or prepended to the query, and does this natively without an LLM call).

Two design shapes with different properties:

- **Pointwise** — score each candidate independently. Parallelizable, shardable, absolute scores,
  costs one call per candidate.
- **Listwise** — show the model a window of candidates and ask for an ordering. Far cheaper per
  candidate and often better, because the model sees candidates in context and can compare them. The
  catch: **scores are relative to the window**, so you cannot merge across windows by score, and
  ordering can be sensitive to the input order you supplied. Sliding-window schemes address this at
  the cost of more calls.

Practical positioning: an LLM reranker is usually the wrong default (latency and cost) and the right
tool for a *small* final stage — reordering 10–20 candidates where the ordering criterion is
domain-specific and expressible in words. Which is also the shape that `13-agents-and-tool-calling.md`
uses, so it will come back.

---

## 9. The reranker landscape as an interface-and-constraint table

Model rankings rotate every few months. Interfaces and constraints change slowly, and they are what
your architecture actually depends on. Evaluate on your own corpus (§13, lab 5); use this to know
what you are choosing between.

| Axis | Why it decides things |
|---|---|
| **Hosted vs self-hosted** | a hosted reranker adds a network hop to your p99 and a vendor to your availability story; a self-hosted one adds a GPU to your ops |
| **Context length** | decides whether your chunks are scored whole or auto-chunked (§7.3) |
| **Max documents per request** | a hard ceiling on `fusion_depth` per call |
| **Aggregate token limit** | the real ceiling — Voyage's `q×n + Σd ≤ 600K` binds before the document count does at large chunk sizes |
| **Pointwise vs listwise** | decides whether scores are shardable and mergeable (§8.2) |
| **Instruction following** | lets you express ranking criteria without an LLM call |
| **Multilingual coverage** | must match your corpus's actual language distribution, not the benchmark's |
| **License** | decides whether on-prem is legally possible at all — the binding constraint for regulated deployments |
| **Domain variants** | code and legal variants exist; whether they help is a measurement, not a given |

Coarse placement as of 2026, stated at the level that will age acceptably:

- **Cohere Rerank** — `rerank-v4.0-pro` and `rerank-v4.0-fast` (32,768 context) alongside
  `rerank-v3.5` and the v3.0 English/multilingual models (4,096 context). Up to 10,000 documents per
  request subject to `n_docs × max_chunks_per_doc ≤ 10,000`; automatic document chunking with max
  pooling; YAML structured-data support. The broadest documentation, which is a real factor in how
  fast you can integrate it.
- **Voyage** — `rerank-2.5` and `rerank-2.5-lite`, 32,000 context, instruction-following,
  multilingual, ≤1,000 documents, `query_tokens × n_docs + Σ doc_tokens ≤ 600,000`, query ≤8,000
  tokens, `top_k` and `truncation` parameters. The natural pairing if you are already on Voyage
  embeddings, since query-side conventions match (`01` §3.1).
- **Jina, BGE, Qwen3-Reranker and the open family** — the reason to care is self-hosting: latency
  without a network hop, no per-request cost, and licenses that permit on-prem. The tradeoff is that
  you now own GPU capacity planning (`../gpu-observability/14-llm-inference-observability.md` if you
  go there).
- **ColBERT-family late-interaction models** — §8.1's rung, evaluated on the storage axis rather than
  the API axis.

Deliberately absent: a quality ranking. Published reranker leaderboards move faster than this
document can, they are measured on benchmark corpora that are not yours, and §13 gives you a
better answer in an afternoon than any of them can give you at all.

---

## 10. The latency budget, as arithmetic

Retrieval latency is not one number, it is a sum with a critical path, and writing it out is how you
find out where your p99 actually comes from.

### 10.1 The budget

```
    t_total = t_embed_query
            + max(t_lexical, t_dense)      ← parallel branches: the MAX, not the sum
            + t_fusion                      ← microseconds; ignore
            + t_rerank                      ← usually the largest single term
            + t_dedup                       ← microseconds
```

Two structural points that decide most of the outcome:

**Run the branches in parallel.** They are independent I/O. Sequential branches turn a max into a
sum for no reason. This is `../python-mastery/29-async-patterns-and-pitfalls.md` territory, and the
part that gets botched is not the concurrency — it is the **cancellation**: when one branch times
out, the other must be cancelled or awaited with a bounded deadline, or your p99 becomes the slowest
branch's p99 with none of the benefit.

**The reranker is a network call, so its p99 is not its p50.** A hosted reranker sits on your
critical path with someone else's tail latency. Budget for the p99 and design the fallback (§10.3).

### 10.2 The shape of the sum

Illustrative, to show the *structure* — these are not measurements, and the whole point of §17's
labs is that you replace them with yours:

| Stage | Typical order of magnitude | Scales with |
|---|---|---|
| Query embedding (hosted) | tens of ms | network + model |
| Query embedding (local small model) | single-digit ms | model size |
| BM25, in-process | low ms | corpus size, query terms |
| ANN, warm, in-RAM | low ms | `ef_search` (`03` §4.2), corpus size |
| ANN, object-storage cold | hundreds of ms to ~1 s | see `03` §9.1 |
| Fusion | µs | candidates |
| Rerank, hosted, ~100 candidates | tens to low hundreds of ms | **linear in candidates** |
| Rerank, self-hosted GPU | lower, no network hop | batch size, model size |

The lines that dominate are always the same two: the reranker, and — if you chose an
object-storage-backed store — the cold ANN query. Everything else is noise by comparison, which
means most latency optimization effort spent elsewhere is misallocated.

### 10.3 Degradation is a design decision, not an exception handler

Under a fixed budget, decide *in advance* what to drop and in what order. The ordering below is
deliberate — it drops the cheapest-to-lose quality first:

```python
async def retrieve(query, deadline_ms=800):
    async with deadline(deadline_ms) as dl:
        # 1. Branches in parallel, each with its own timeout.
        #    A missing branch degrades recall; it does not fail the request.
        lex, dense = await gather_with_timeouts(
            lexical_search(query, depth=100), timeout_ms=dl.slice(150),
            dense_search(query, depth=100),   timeout_ms=dl.slice(200),
        )
        if lex is None and dense is None:
            raise RetrievalUnavailable          # both gone: fail honestly
        fused = rrf({"lex": lex or [], "dense": dense or []}, k=60)

        # 2. Rerank is the first thing to drop. Fused order is a worse
        #    ordering of the SAME candidate set — a real but bounded loss.
        try:
            return await rerank(query, fused[:100], top_k=20,
                                timeout_ms=dl.remaining())
        except TimeoutError:
            metrics.increment("rerank.timeout")   # ← alert on the RATE
            return fused[:20]
```

Three things this encodes:

- **Losing a branch degrades the ceiling; losing the reranker degrades the ordering.** The former is
  worse, so branches get their own timeouts and the reranker gets the remainder.
- **Degradations must be counted, not just handled.** A silent fallback that fires on 30% of requests
  is a quality regression nobody will attribute correctly. Emit the counter, alert on the rate, and
  record the degradation on the trace (`10-llm-observability-and-tracing.md`) so eval results can be
  segmented by whether the full cascade ran.
- **Failing honestly is a valid outcome.** If both branches are gone, returning an ungrounded answer
  is worse than returning an error.

---

## 11. Diversity and deduplication at merge time

### 11.1 The problem, quantified upstream

`02` §10.4 makes the case: if your top-10 contains six near-copies of the same passage — versioned
docs, boilerplate, syndicated content, overlapping chunks from `02` §5.5 — you have four distinct
passages and you paid for ten. `02`'s lab 8 produces the number ("distinct passages in top-10") that
tells you whether this section is worth building. Run it before building.

### 11.2 MMR

Maximal Marginal Relevance greedily builds a result set balancing relevance and novelty:

```
MMR = argmax [ λ · sim(d, q) − (1−λ) · max sim(d, dⱼ) ]
      d ∉ S                              dⱼ ∈ S
```

`λ = 1` is pure relevance; `λ = 0` is pure diversity. Useful defaults live around 0.5–0.7, and it is
worth sweeping because the right value depends on your query mix: broad synthesis queries want more
diversity, precise factoid queries want almost none.

```python
def mmr(query_vec, cand_vecs, cand_ids, k=10, lam=0.6):
    """cand_vecs: (n, d) L2-normalized, in fused order. Runs after reranking,
    over a small set, so the O(k·n) loop is free."""
    rel = cand_vecs @ query_vec
    selected, remaining = [], list(range(len(cand_ids)))
    while len(selected) < k and remaining:
        if not selected:
            best = max(remaining, key=lambda i: rel[i])
        else:
            sel = cand_vecs[selected]                       # (|S|, d)
            best = max(remaining, key=lambda i:
                       lam * rel[i] - (1 - lam) * float((sel @ cand_vecs[i]).max()))
        selected.append(best)
        remaining.remove(best)
    return [cand_ids[i] for i in selected]
```

### 11.3 Ordering, and the cheaper alternative

**Run diversification after reranking, not before.** Before reranking you would be diversifying a
badly-ordered set and possibly discarding the passage the reranker would have promoted. After
reranking you are choosing among candidates whose relevance you already trust.

**And try exact/near-exact dedup first.** MMR is a tuned heuristic with a λ you now own. Collapsing
identical and near-identical chunks (`02` §10.2's MinHash, applied at merge time over ~100
candidates, which is cheap) captures most of the available gain with no parameter. Reach for MMR when
dedup leaves genuine redundancy — passages that are distinct texts saying the same thing.

**One caution.** Diversity is not free: it demotes relevant passages by construction. For a query
whose answer genuinely requires three similar passages ("what did each of the three amendments
change?"), MMR actively hurts. Measure it (lab 7); don't adopt it because it sounds principled.

---

## 12. Filtering and authorization inside the cascade

### 12.1 Authorization filters go first, and are not negotiable

Access control belongs at the *first* stage, applied inside the index, for three reasons in
increasing order of severity:

1. Post-filtering under-returns (`03` §7.1) — the user gets fewer results than `k`, silently.
2. Reranking documents the user can't see wastes money and latency.
3. **Any later leak is a security bug.** A snippet in a debug log, a document title in a citation, a
   count in an analytics event. If the retrieval path can return an unauthorized document object at
   all, something will eventually surface it.

Treat it as an invariant with a test, not as a filter parameter:

```python
def retrieve(query, principal):
    acl = acl_predicate(principal)      # resolved once, applied to EVERY branch
    lex   = lexical_search(query, filter=acl, depth=100)
    dense = dense_search(query,   filter=acl, depth=100)
    # Belt and braces: assert, so a branch that silently ignores its filter
    # (a real failure mode when one backend's filter DSL can't express the ACL)
    # fails loudly in tests instead of quietly in production.
    for r in lex + dense:
        assert authorized(principal, r), f"ACL leak in retrieval: {r.id}"
    ...
```

The assertion is not paranoia. The realistic failure is heterogeneous backends: your dense store's
payload filter can express `tenant_id = X` but not `group_id IN (...) OR owner = Y`, so someone
implements the hard half in post-filtering "temporarily." The assert catches it in tests. `17` covers
the authorization surface properly; this is the retrieval-shaped part of it.

### 12.2 Every branch must apply the filter, and each one has its own selectivity problem

`03` §7 is about the dense branch, but the lexical branch has the same three-regime structure with
different mechanics (inverted-list intersection rather than graph traversal), and different
thresholds. A filter that is comfortably handled by the lexical branch may be in the dense branch's
broken middle band. Measure per branch (`03` lab 5), not once for "retrieval."

### 12.3 Filters that are really ranking preferences

"Prefer recent documents" is not a filter. Implementing it as `WHERE date > X` throws away a
still-correct 2019 policy document that nothing has superseded. It belongs in ranking — as a branch
weight, a boost, a reranker instruction (§8.2, and Voyage's instruction-following rerankers do this
natively), or a post-rerank tiebreak.

The distinction is worth being pedantic about because it is where a lot of "the search is broken"
reports come from: **a filter is a correctness constraint (the user may not see this / it is not
applicable); a preference is a ranking signal.** Encoding a preference as a filter converts a soft
loss into a hard one, and hard losses are invisible — you never see what you excluded.

---

## 13. Evaluating a cascade without fooling yourself

`08-evaluation-methodology.md` covers evaluation properly. This section covers the traps specific to
multi-stage retrieval, which are not obvious and which invalidate most informal comparisons.

### 13.1 Tune the baseline as hard as the challenger

§4.2's BM42 correction is the case study. An under-configured baseline is the most common source of
false positives in this field, and it is much harder to notice than a bug in the new thing, because
the result agrees with your prior.

Concretely, before claiming hybrid beats dense: sweep the dense branch's `ef_search` and
`branch_depth` (`03` §3.2, §6.2) to *its* best operating point. Before claiming a reranker helps:
make sure the no-reranker baseline gets the same `final_k` and the same token budget. Before claiming
a learned-sparse branch helps: make sure your BM25 analyzer isn't misconfigured, which — per §4.2's
character-escaping bug — is exactly how a strong baseline gets understated.

### 13.2 Measure each stage with the metric that stage controls

| Stage | Metric | Not |
|---|---|---|
| Each branch, alone | recall@`branch_depth` | nDCG — the branch's ordering is about to be discarded |
| Fusion | recall@`fusion_depth` | nDCG, if a reranker follows (§5.3) |
| Reranker | nDCG@`final_k`, MRR@`final_k` | recall — it cannot change recall at fixed `fusion_depth`, by construction |
| Whole cascade | recall@`final_k` **at a fixed token budget**, plus end-to-end answer quality | any single-stage metric |

That third row is worth stating as a rule because it catches a real and common confusion:
**a reranker cannot improve recall@`fusion_depth`.** It reorders a fixed set. It *can* improve
recall@`final_k` (by promoting relevant passages into the top-k) — which is a different quantity,
and the two get conflated constantly.

### 13.3 Ablate one stage at a time, with everything else pinned

The valid ablation table, all rows at the same `final_k` and the same token budget:

| Configuration | recall@`final_k` | nDCG@`final_k` | p50 | p99 | $/1k queries |
|---|---|---|---|---|---|
| dense only | | | | | |
| lexical only | | | | | |
| hybrid (RRF), no rerank | | | | | |
| dense only + rerank | | | | | |
| hybrid + rerank | | | | | |

Row 4 is the one people skip and it is the most informative row in the table. It answers "do we need
the lexical branch *given* that we rerank?" — which is a genuinely open question, because a reranker
recovers some lexical-ish precision on its own. If rows 4 and 5 are within each other's confidence
intervals, you can delete a whole branch and its ingestion path.

Report confidence intervals on every delta (`../python-mastery/31-measurement-methodology.md`).
Retrieval deltas are frequently 1–3 points on golden sets of 50–200 queries, which is squarely inside
the noise band for that sample size. A large fraction of published retrieval improvements are
un-intervalled differences of this magnitude.

### 13.4 Stratify, because the aggregate hides the mechanism

Hybrid's benefit is concentrated in the query strata where the branches disagree (§3.3). An aggregate
"+2 points" can be "+15 on the identifier stratum, +0 elsewhere" — which is a completely different
engineering conclusion, because it tells you the lexical branch is load-bearing for a specific and
identifiable class of traffic that you can now measure the business value of.

Use `02` §11.6's strata and add the ones this chapter implies: identifier/exact-match queries,
paraphrase queries, multi-clause queries, short queries.

---

## 14. Cost model for the retrieval layer

### 14.1 Per query

```
cost_per_query =
      index_cost_amortized          # 03 §12.3 — fixed, whether or not you query
    + query_embedding_tokens × $/token
    + rerank_cost                   # candidates × $/doc, or GPU seconds
    + (llm_rerank_cost)             # if §8.2
```

Two properties shape every decision here:

**The reranker's cost is linear in `fusion_depth`.** Doubling candidates doubles reranking cost *and*
latency, for a recall gain that is sharply diminishing (§6.2). That makes `fusion_depth` the single
highest-leverage cost knob in the cascade, and it is a runtime parameter — free to sweep, per §6.2,
exactly like `03`'s `ef_search`.

**Reranking is usually cheap relative to generation.** Reranking 100 chunks is typically a small
fraction of the cost of generating an answer over 20 of them, which is why "drop the reranker to save
money" is usually the wrong optimization while "drop `final_k` from 20 to 10" might not be. `11` has
the full accounting; the retrieval-side point is that these two knobs live in different chapters and
must be priced together.

### 14.2 The ingestion side

The lexical branch is nearly free to build (no model inference), which is a genuinely underrated
argument for it: it raises the ceiling (§3.3) at a marginal ingest cost of approximately zero, against
a dense branch that costs an embedding call per chunk and a learned-sparse branch that costs a
forward pass per chunk plus a larger index. When you tabulate `02` §12.1's ingest cost, put the three
branches in adjacent rows — the comparison usually surprises people.

### 14.3 What to log per request

For `11-token-accounting-and-cost.md` to work at all, retrieval must emit, per request: branch
latencies, candidate counts at each depth, whether the reranker ran or was dropped (§10.3), reranker
document count and token count, and the degradation flags. Attach it to the trace, not just to a log
line — `10-llm-observability-and-tracing.md` covers the span structure, which mirrors §1's diagram
exactly.

---

## 15. Anti-patterns

**Adding a reranker to fix bad first-stage recall.** Reordering a candidate set that doesn't contain
the answer. Measure `neither` from §3.3 before reaching for a reranker (§1.1).

**Reporting recall without the candidate depths.** "recall@100 = 0.94" is not a result until
`branch_depth`, `fusion_depth`, the branch count and the fusion parameters are attached (§6.3).

**Leaving the fusion window at its default.** Elasticsearch's `rank_window_size` defaults to `size`,
so you fuse top-10 lists and hand your reranker 15 candidates. One unset parameter erases most of the
cascade's value (§5.4).

**`ef_search` below `k` on the dense branch.** `03` §4.2's silent truncation, arriving here as
mysteriously bad hybrid results.

**Normalizing scores with min-max over the result set and calling it fusion.** The normalization
depends on set composition, so the same document scores differently at different `k` (§5.1).

**Tuning RRF weights for a week when a reranker follows.** The fused ordering is discarded. Fusion is
a recall problem in that configuration (§5.3).

**Comparing hybrid against an untuned dense baseline.** The BM42 correction is the canonical example
of how this produces a confident wrong answer (§4.2, §13.1).

**Evaluating fusion with nDCG when a reranker follows, or a reranker with recall@`fusion_depth`.**
Both measure a quantity the stage cannot affect (§13.2).

**Feeding the reranker the analyzer's output instead of canonical chunk text.** Stemmed,
stopword-stripped text is not what a cross-encoder was trained on (§2.4, `02` §4.3).

**Ignoring `max_chunks_per_doc`.** Defaults to 1, so long documents are silently truncated to the
first chunk inside the reranker (§7.3).

**Exceeding the reranker's aggregate token limit and discovering it in production.** Voyage's
`query_tokens × n_docs + Σ doc_tokens ≤ 600,000` binds well before its 1,000-document limit at large
chunk sizes (§7.3, §9).

**Post-filtering for authorization.** Under-returns, wastes reranking, and eventually leaks (§12.1).

**Encoding a ranking preference as a filter.** "Prefer recent" implemented as `WHERE date > X`
silently deletes still-correct old documents, and you never see what you excluded (§12.3).

**Sequential branches.** Turns a `max` into a sum for no reason. And parallel branches without
cancellation turn your p99 into the slowest branch's p99 (§10.1).

**Silent reranker fallback.** If it times out on 30% of requests and nobody counts it, your eval
numbers describe a system that runs on 70% of traffic (§10.3).

**MMR before reranking.** Diversifying a badly-ordered set, and possibly discarding what the reranker
would have promoted (§11.3).

**Adopting MMR without measuring it.** It demotes relevant passages by construction, and for
queries needing several similar passages it strictly hurts (§11.3).

**Quoting a reranker leaderboard as if it were about your corpus.** §13 gives you a better answer in
an afternoon than any benchmark can give you at all (§9).

---

## 16. Mental models — the compressed set

1. **Only the first stage can add candidates.** Everything downstream reorders or discards, so
   first-stage recall is a hard ceiling on end-to-end quality (§1.1).
2. **Cheap and wide, then expensive and narrow.** The cascade is the same structure as `03` §6.1's
   quantize-then-rescore, and once you see the shape you'll recognize it in every retrieval system
   worth studying (§1.2).
3. **A cross-encoder cannot be an index, and that's not an implementation gap.** Joint encoding means
   nothing is precomputable, which is exactly why it's accurate and exactly why it's the second
   stage (§1.2).
4. **Hybrid is justified by disjoint failures, not by "two is better than one."** Two dense models
   fail on the same queries; measure `lexical_only` and `neither` before assuming anything (§3.3).
5. **Over RAG-sized chunks, BM25 is mostly IDF.** Term frequency is 0 or 1 and length is uniform, so
   tune the analyzer, not `k₁` and `b` (§2.2).
6. **IDF is corpus statistics, so it drifts.** Computing it once at ingest degrades exactly for the
   new rare terms you most wanted weighted (§2.2).
7. **RRF's `k` is a consensus knob.** Small `k` lets one branch's top hit win; `k = 60` makes
   agreement across branches matter more than any branch's confidence (§5.2).
8. **With a reranker downstream, fusion is a recall problem, not a ranking problem** — so measure it
   with recall and stop tuning its weights (§5.3).
9. **There are three depths, not one `k`.** `branch_depth` buys ceiling, `fusion_depth` buys reranker
   opportunity linearly in dollars, `final_k` buys prompt recall at the cost of distraction (§6.1).
10. **A reranker cannot improve recall@`fusion_depth`** — it reorders a fixed set. It can improve
    recall@`final_k`. Conflating the two invalidates the evaluation (§13.2).
11. **Swapping a reranker is a config change; swapping an embedding model is an O(corpus)
    migration.** That asymmetry makes the reranker the right place to put domain adaptation (§7.2).
12. **Cross-encoder scores are absolute, listwise scores are relative to their window.** The first
    shards and merges cleanly across requests; the second doesn't (§7.3, §8.2).
13. **Run branches in parallel and get the cancellation right.** Otherwise your p99 is the slowest
    branch's p99 with none of the parallelism's benefit (§10.1).
14. **Decide the degradation order in advance, and count every degradation.** Losing a branch costs
    ceiling; losing the reranker costs ordering. An uncounted fallback is a quality regression nobody
    will attribute correctly (§10.3).
15. **A filter is a correctness constraint; a preference is a ranking signal.** Encoding a preference
    as a filter converts a soft loss into an invisible hard one (§12.3).
16. **Tune the baseline as hard as the challenger.** The BM42 correction — a character-escaping bug
    that understated BM25's recall from 0.89 to 0.71 — is what a weak baseline looks like from the
    inside (§4.2, §13.1).
17. **Stratify, or the mechanism stays hidden.** "+2 points overall" and "+15 points on the 8% of
    queries containing identifiers" are the same measurement and completely different engineering
    conclusions (§13.4).

---

## 17. Lab exercises

Every lab produces an artifact and a number. Every number produced here is **rung 1 — measured**
(README §6): quote it with its corpus, its golden set, its three candidate depths, its token budget,
and its fusion parameters, every time, or don't quote it. This document stays **rung 3 — studied**
until these have been run against a real corpus.

These assume `02` lab 4's span-labeled golden set and `03` lab 1's harness. If you don't have them,
build those first — every measurement below is invalid without them.

**Lab 1 — The complementarity census.**
*Goal:* find out whether hybrid is worth building on your corpus, before building it.
*Steps:* run §3.3's `branch_complementarity` at `branch_depth = 50` and again at 100. Report
`recall_lexical`, `recall_dense`, `recall_union`, `lexical_only`, `dense_only`, `neither`. Stratify by
query type: identifier/exact-match, paraphrase, multi-clause, short.
*Artifact:* the overlap table, overall and per stratum.
*Success criterion:* a defensible answer to "is the lexical branch worth its ingestion path?" — with
"no, `lexical_only` is 0.8%" as a valid and money-saving outcome — plus a stated `neither` figure that
tells you whether the real work is upstream in `01`/`02`/`05`.
*Time:* ~3 hours.
*Unblocks:* every other lab here, and P1.

**Lab 2 — The candidate-depth sweep.**
*Goal:* find your ceiling curve and its knee, which sets every budget downstream.
*Steps:* sweep `branch_depth ∈ {10, 25, 50, 100, 200, 500}` per branch. At each, measure first-stage
recall (union, after fusion at `fusion_depth = branch_depth`) and each branch's p50/p99. Plot recall
vs depth and find the knee. Note where the dense branch's latency starts moving — cross-reference
`03` lab 2's `ef_search` curve, since `branch_depth` and `ef_search` interact.
*Artifact:* a recall-vs-depth curve with latency on a second axis, and a chosen `branch_depth` with a
one-line justification.
*Success criterion:* you can state your first-stage recall ceiling and what it would cost to raise it
by 2 points.
*Time:* ~3 hours.
*Unblocks:* labs 3–6, and `06`'s token budget.

**Lab 3 — RRF versus score fusion versus each branch alone.**
*Goal:* measure fusion as a recall problem (§5.3), and find out whether the fusion method matters at
all once a reranker follows.
*Steps:* at fixed `branch_depth` from lab 2, compare: dense alone, lexical alone, RRF at
`k ∈ {10, 60, 200}`, weighted RRF with weights fitted on a dev split, and min-max score fusion.
Measure recall@`fusion_depth`. Then repeat every configuration **with a reranker after it** and
measure nDCG@`final_k`, to test §5.3's claim that fusion method stops mattering.
*Artifact:* a two-panel table — recall without reranking, nDCG with — plus a stated fusion choice.
*Success criterion:* an evidence-based answer to "does our fusion method matter?" If the reranked
rows are within each other's CIs, you have just saved yourself a tuned parameter and its maintenance.
*Time:* ~half a day.
*Unblocks:* `05-query-understanding.md`, which adds more branches to this same step.

**Lab 4 — The full ablation table.**
*Goal:* the artifact this chapter exists to produce — §13.3's table, filled in, with intervals.
*Steps:* run all five configurations at identical `final_k` and identical token budget (`02` §11.3).
Record recall@`final_k`, nDCG@`final_k`, p50, p99, and cost per 1,000 queries. Bootstrap 95% CIs on
every delta against the dense-only baseline. **Tune each baseline to its own best operating point
first** (§13.1) — sweep the dense branch's `ef_search`, and verify your BM25 analyzer on a handful of
known-answer identifier queries.
*Artifact:* the completed table with CIs, plus a written verdict naming which components earn their
place.
*Success criterion:* row 4 (dense + rerank) versus row 5 (hybrid + rerank) is answered with an
interval, so you know whether the lexical branch survives the presence of a reranker.
*Time:* ~1 day.
*Unblocks:* P1's architecture, and `08`.

**Lab 5 — Reranker bake-off on your corpus.**
*Goal:* replace §9's landscape table with a measurement.
*Steps:* pick two or three rerankers spanning the hosted/self-hosted axis. At fixed `fusion_depth`
from lab 2, measure nDCG@`final_k`, MRR, p50, p99 (including network), and cost per 1,000 queries.
Sweep `fusion_depth ∈ {25, 50, 100, 200}` for the leading candidate to find where the quality gain
stops paying for the linear cost. Verify your requests against the constraint table in §7.3 —
compute your aggregate token count and confirm it's inside the limit.
*Artifact:* a reranker × `fusion_depth` grid of quality, latency and cost; and a chosen operating
point.
*Success criterion:* a reranker and a `fusion_depth` chosen with all three numbers stated — plus the
`fusion_depth` at which additional candidates stopped paying.
*Time:* ~half a day plus API cost.
*Unblocks:* §14's cost model, and `12-serving-latency-and-caching.md`.

**Lab 6 — The latency budget and its degradation path.**
*Goal:* know where your p99 comes from and what happens when it's exceeded.
*Steps:* instrument every stage in §10.1 separately and record the full distribution, not the mean.
Establish the p50 and p99 of each. Then implement §10.3's degradation ladder and *induce* each
failure: kill the lexical branch, kill the dense branch, time out the reranker. Measure recall and
nDCG under each degraded mode, and confirm the counters fire.
*Artifact:* a per-stage latency table (p50/p95/p99) plus a degraded-mode quality table.
*Success criterion:* you can state "if the reranker is down we lose X nDCG points and Y ms" from
measurement, and your degradation counters are wired to alerts.
*Time:* ~half a day.
*Unblocks:* `10`, `12`, and P2.

**Lab 7 — Diversity, measured rather than assumed.**
*Goal:* find out whether MMR helps you, using `02` lab 8's redundancy number as the prior.
*Steps:* three configurations at fixed `final_k` and fixed token budget: (a) rerank only, (b) rerank
+ near-duplicate collapse, (c) rerank + MMR at `λ ∈ {0.5, 0.7, 0.9}`. Measure recall@`final_k` and
end-to-end answer quality if you have a judge (`08`). Report per stratum, and pay attention to the
synthesis stratum specifically, where diversity should help most, and to any stratum whose answers
need multiple similar passages, where it should hurt.
*Artifact:* a three-configuration table by stratum, with the `distinct-passages-in-top-k` count for
each.
*Success criterion:* a decision with evidence — including "dedup was enough, MMR added nothing" as the
most likely and most useful outcome.
*Time:* ~4 hours.
*Unblocks:* `06-context-engineering.md`.

**Lab 8 — Authorization invariant test.**
*Goal:* make §12.1's invariant a test rather than an intention.
*Steps:* build a fixture corpus with documents belonging to three principals with overlapping and
disjoint access. Write a property test that runs the full cascade as each principal over a set of
queries and asserts no unauthorized document appears at *any* stage — branch output, fused set,
reranker input, final result. Then deliberately break one branch's filter and confirm the test
catches it at the branch, not at the end.
*Artifact:* a passing property test in CI, plus a demonstrated catch of an induced leak.
*Success criterion:* the test fails loudly when a filter is dropped from any single branch.
*Time:* ~3 hours.
*Unblocks:* `16-multi-tenancy-and-isolation.md`, `17-safety-guardrails-and-prompt-injection.md`, and
P4.

**Lab 9 — Retrieval config as a logged artifact.**
*Goal:* make §6.3's discipline structural instead of aspirational.
*Steps:* define a serializable `RetrievalConfig` capturing every parameter in this chapter —
branch list, `branch_depth`, fusion method and parameters, `fusion_depth`, reranker model and
`final_k`, MMR λ, filter strategy, and the upstream versions from `01`/`02`/`03`. Log it with every
eval run and emit it on every production trace. Then write the query that answers "which config
produced our best recorded nDCG, and is it what's deployed?"
*Artifact:* the config object, the eval-run join, and that query returning an answer.
*Success criterion:* no number in your eval history exists without its config, and you can diff the
deployed config against the best-measured one.
*Time:* ~3 hours.
*Unblocks:* `09-eval-infrastructure-and-ci.md`, `10`, and P0's regression gate.

---

## Rung ledger

This document is **rung 3 — studied** (README §6). Its mechanisms — why a cross-encoder cannot be
indexed, why RRF's `k` controls consensus versus top-rank dominance, why a reranker cannot change
recall at fixed `fusion_depth`, why min-max normalization over a result set makes scores
query-incomparable, why post-filtering under-returns — are derivable from the definitions and from
the cited primary sources. The latency figures in §10.2 are explicitly labeled as orders of magnitude
for structural orientation, not measurements, and should not be quoted; §17's lab 6 is how you get
yours.

**Verified against primary sources, read directly:** Elasticsearch's reciprocal-rank-fusion reference
(the RRF formula as published, `rank_constant` defaulting to 60 with a minimum of 1,
`rank_window_size` defaulting to `size` with the kNN-`k` truncation behaviour, and the
"requires no tuning" design claim) and its similarity settings reference (BM25 `k₁ = 1.2` and
`b = 0.75` defaults, `discount_overlaps`); Cohere's Rerank model page and reranking best-practices
guide (the `rerank-v4.0-pro` / `rerank-v4.0-fast` / `rerank-v3.5` / v3.0 model table and context
lengths, the 32,764- and 4,093-token chunking procedure with max-pooling across chunks, the
10,000-document limit and the `n_docs × max_chunks_per_doc ≤ 10,000` inequality, `max_chunks_per_doc`
defaulting to 1, the half-context query truncation at 16,384 / 2,048 tokens, and the YAML
`sort_keys=False` structured-data guidance); Voyage's reranker documentation (`rerank-2.5` and
`rerank-2.5-lite` at 32,000 context with instruction following, the 1,000-document limit, the 8,000
token query limit, the `query_tokens × n_docs + Σ doc_tokens ≤ 600,000` aggregate limit, and the
`top_k` / `truncation` parameters); Qdrant's BM42 article (the BM25 decomposition into
corpus-importance and document-importance terms, the observation that term frequency is 0-or-1 and
length normalization is inert over RAG-sized chunks, the SPLADE critique, the
`IDF × Attention(CLS, qᵢ)` formulation, the 5.6-elements-per-document sparsity figure and ~13 MB
index for ~530k quora documents, and — quoted in full because it is the methodological point of §4.2
— the post-publication correction notice, the corrected quora table, and Qdrant's own conclusion that
correctly configured BM25 with tantivy achieved the best results); Anthropic's contextual-retrieval
post (the 67% reduction in top-20 retrieval failure rate from 5.7% to 1.9% with reranking, and the
six summary findings including that top-20 outperformed top-10 and top-5). The arXiv records for
ColBERT (Khattab & Zaharia, 2004.12832) and SPLADE v2 (Formal, Lassance, Piwowarski & Clinchant,
2109.10086) were read for their abstracts and author lists; their internal benchmark numbers are not
quoted here.

**Someone else's rung 1, quoted with conditions attached:** Anthropic's 67% failure-rate reduction
(their corpora, their embedding models, their reranker choice — the Cohere reranker, with Voyage's
untested at the time, per their own note); Qdrant's corrected quora figures (one dataset, a
question-deduplication task with very short texts, explicitly described by its authors as not an
exhaustive evaluation). Both establish that a technique can help under stated conditions. Neither
establishes what it does on your corpus, which §17 exists to determine.

**Deliberately not in this document:** any reranker or embedding-model quality leaderboard, any
BEIR/MTEB score, and any claim that one reranker beats another. Those numbers rotate faster than this
document can be maintained, they are measured on corpora that are not yours, and §13's ablation
protocol produces a better answer in an afternoon. The reranker table in §9 is deliberately an
interface-and-constraint table for that reason: the constraints are what your architecture depends on
and they change slowly.

The labs in §17 are what convert this to **rung 1 — measured**, and their outputs must always travel
with their corpus, golden set, all three candidate depths, token budget, and fusion parameters.
